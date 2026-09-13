"""Configuration, logging and HTTP entry point for Plex upload budgeting."""

import json
import logging
import os
import re
import signal
import sys
import threading
from dataclasses import dataclass, field
from decimal import Decimal
from logging.handlers import RotatingFileHandler
from typing import Optional
from urllib.parse import quote, quote_plus

from flask import Flask, jsonify, request
from controller import StateManager, validate_config
from waitress import serve


@dataclass
class Config:
    plex_url: str = field(default_factory=lambda: os.getenv('PLEX_URL', 'http://plex:32400'))
    plex_token: str = field(default_factory=lambda: os.getenv('PLEX_TOKEN', ''))
    qbt_url: str = field(default_factory=lambda: os.getenv('QBITTORRENT_URL', 'http://qbittorrent:8080'))
    qbt_username: str = field(default_factory=lambda: os.getenv('QBITTORRENT_USERNAME', ''))
    qbt_password: str = field(default_factory=lambda: os.getenv('QBITTORRENT_PASSWORD', ''))
    polling_interval: int = field(default_factory=lambda: os.getenv('POLLING_INTERVAL', '5'))
    debounce_seconds: int = field(default_factory=lambda: os.getenv('DEBOUNCE_SECONDS', '3'))
    stop_delay_seconds: int = field(default_factory=lambda: os.getenv('STOP_DELAY_SECONDS', '30'))
    pause_buffer_delay_seconds: int = field(default_factory=lambda: os.getenv('PAUSE_BUFFER_DELAY_SECONDS', '60'))
    # Read timeouts. Both are polled on every cycle, so an unbounded wait on
    # either would stall the loop for as long as the other end takes to answer.
    plex_timeout: int = field(default_factory=lambda: os.getenv('PLEX_TIMEOUT', '10'))
    qbt_timeout: int = field(default_factory=lambda: os.getenv('QBITTORRENT_TIMEOUT', '10'))
    # How often to re-read qBittorrent purely to detect an external change.
    # Kept deliberately slow: the Web UI is usually the busiest thing here, and
    # this poll earns nothing when no speed change is needed. Changes we
    # initiate are applied immediately regardless of this interval.
    drift_check_seconds: int = field(default_factory=lambda: os.getenv('DRIFT_CHECK_SECONDS', '60'))
    log_level: str = field(default_factory=lambda: os.getenv('LOG_LEVEL', 'INFO'))
    http_port: int = field(default_factory=lambda: os.getenv('HTTP_PORT', '5252'))

    dynamic_upload_enabled: bool = field(default_factory=lambda: os.getenv('DYNAMIC_UPLOAD_ENABLED', 'false'))
    max_upload_mib: Decimal = field(default_factory=lambda: os.getenv('MAX_UPLOAD_MIB', '12'))
    min_upload_mib: Decimal = field(default_factory=lambda: os.getenv('MIN_UPLOAD_MIB', '1'))
    bandwidth_multiplier: Decimal = field(default_factory=lambda: os.getenv('BANDWIDTH_MULTIPLIER', '1'))
    plex_stale_seconds: int = field(default_factory=lambda: os.getenv('PLEX_STALE_SECONDS', '120'))

    def __post_init__(self):
        validate_config(self)
        self.log_level = self.log_level.upper()
        if self.log_level not in ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'):
            raise ValueError('LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL')


class RedactingFormatter(logging.Formatter):
    def __init__(self, cfg):
        super().__init__('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        self.secrets = set()
        for secret in (cfg.plex_token, cfg.qbt_password):
            if secret:
                self.secrets.update((secret, quote(secret, safe=''), quote_plus(secret)))

    def format(self, record):
        text = super().format(record)
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, '[REDACTED]')
        text = re.sub(r'(?i)(X-Plex-Token|password|token)([=:\s]+)[^&\s,]+',
                      r'\1\2[REDACTED]', text)
        return re.sub(r'(https?://)[^/@\s]+:[^/@\s]+@', r'\1[REDACTED]@', text)
def setup_logging(config):
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        if os.path.isdir('/app/logs') and os.access('/app/logs', os.W_OK):
            handlers.append(
                RotatingFileHandler('/app/logs/app.log', maxBytes=5 << 20, backupCount=5)
            )
    except OSError as e:
        print(f"Warning: cannot write to /app/logs ({e}), logging to console only")

    for handler in handlers:
        handler.setFormatter(RedactingFormatter(config))
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=handlers,
        force=True,
    )

    # LOG_LEVEL=DEBUG is for diagnosing session detection, not HTTP plumbing.
    # Left at DEBUG these log every poll's connection and bury the useful lines.
    for noisy in ('urllib3', 'requests', 'plexapi', 'qbittorrentapi'):
        logging.getLogger(noisy).setLevel(logging.WARNING)


logger = logging.getLogger('plex-qbt')

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024
state: Optional["StateManager"] = None  # created in main()


# -- HTTP ---------------------------------------------------------------


def _webhook_payload() -> Optional[dict]:
    """Plex posts multipart form-data with a JSON 'payload' field; allow raw JSON too."""
    if request.is_json:
        return request.get_json(silent=True)
    if request.form.get('payload'):
        return json.loads(request.form['payload'])
    if request.data:
        return json.loads(request.data.decode())
    return None


PLAYBACK_EVENTS = frozenset(
    {'media.play', 'media.resume', 'media.pause', 'media.stop', 'media.buffer'}
)


@app.route('/webhook', methods=['POST'])
def webhook():
    if state is None:
        return jsonify({'error': 'initializing'}), 503

    try:
        payload = _webhook_payload()
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = None

    if not isinstance(payload, dict) or not isinstance(payload.get('event'), str):
        logger.warning(f"Unparseable webhook (Content-Type: {request.content_type})")
        return jsonify({'error': 'no valid payload'}), 400

    event = payload.get('event')
    if any(payload.get(key) is not None and not isinstance(payload[key], dict)
           for key in ('Account', 'Player', 'Metadata', 'Server')):
        return jsonify({'error': 'invalid webhook identity'}), 400

    if event not in PLAYBACK_EVENTS:
        logger.debug(f"Ignoring event {event}")
        return jsonify({'status': 'ignored', 'event': event}), 200

    logger.info('Webhook event=%s received; waking poll loop', event)
    state.poke(payload)

    return jsonify({'status': 'ok', 'event': event}), 200


@app.route('/health')
def health():
    if state is None:
        return jsonify({'status': 'initializing'}), 503
    snapshot = state.status()
    ok = snapshot['plex_connected'] and snapshot['qbt_connected']
    return jsonify({'status': 'healthy' if ok else 'unhealthy', **snapshot}), 200 if ok else 503


@app.route('/status')
def status():
    if state is None:
        return jsonify({'status': 'initializing'}), 503
    return jsonify(state.status())


# -- entry point --------------------------------------------------------


def polling_loop():
    while not state.shutdown:
        # Clear before I/O so an event arriving during refresh gets another pass.
        state.wake.clear()
        try:
            state.refresh()
        except Exception:
            logger.exception('Unexpected polling failure; retry next cycle')
        state.wake.wait(timeout=state.cfg.polling_interval)


def main():
    global state
    try:
        config = Config()
    except ValueError as exc:
        print(f'Invalid configuration: {exc}', file=sys.stderr)
        sys.exit(1)
    setup_logging(config)

    missing = [
        name
        for name, value in (
            ('PLEX_TOKEN', config.plex_token),
            ('QBITTORRENT_USERNAME', config.qbt_username),
            ('QBITTORRENT_PASSWORD', config.qbt_password),
        )
        if not value
    ]
    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    logger.info('Starting dynamic=%s min_mib=%s max_mib=%s multiplier=%s poll=%ss '
                'pause=%ss stop=%ss debounce=%ss drift=%ss stale=%ss Plex_timeout=%ss qbt_timeout=%ss',
                config.dynamic_upload_enabled, config.min_upload_mib, config.max_upload_mib,
                config.bandwidth_multiplier, config.polling_interval, config.pause_buffer_delay_seconds,
                config.stop_delay_seconds, config.debounce_seconds, config.drift_check_seconds,
                config.plex_stale_seconds, config.plex_timeout, config.qbt_timeout)
    state = StateManager(config)

    def handle_signal(signum, _frame):
        logger.info(f"Signal {signum} received, shutting down")
        state.shutdown = True
        state.wake.set()  # break the poll loop out of its wait immediately
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    threading.Thread(target=polling_loop, daemon=True).start()

    logger.info(f"Listening on port {config.http_port} (remote Plex sessions only)")
    serve(app, host='0.0.0.0', port=config.http_port, threads=8)


if __name__ == '__main__':
    main()

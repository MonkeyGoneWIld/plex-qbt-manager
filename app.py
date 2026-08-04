"""Toggle qBittorrent's alternative speed limits based on remote Plex playback.

A session is tracked from the moment it starts playing until its grace timer
expires. Alternative speeds are on whenever at least one session is tracked.
Plex webhooks are the fast path; polling is the fallback and the reconciler.
"""

import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, Optional

from flask import Flask, jsonify, request
from plexapi.exceptions import PlexApiException
from plexapi.server import PlexServer
from qbittorrentapi import Client as QBittorrentClient
from qbittorrentapi.exceptions import APIConnectionError
from waitress import serve


@dataclass
class Config:
    plex_url: str = os.getenv('PLEX_URL', 'http://plex:32400')
    plex_token: str = os.getenv('PLEX_TOKEN', '')
    qbt_url: str = os.getenv('QBITTORRENT_URL', 'http://qbittorrent:8080')
    qbt_username: str = os.getenv('QBITTORRENT_USERNAME', '')
    qbt_password: str = os.getenv('QBITTORRENT_PASSWORD', '')
    polling_interval: int = int(os.getenv('POLLING_INTERVAL', '5'))
    debounce_seconds: int = int(os.getenv('DEBOUNCE_SECONDS', '3'))
    stop_delay_seconds: int = int(os.getenv('STOP_DELAY_SECONDS', '30'))
    pause_buffer_delay_seconds: int = int(os.getenv('PAUSE_BUFFER_DELAY_SECONDS', '60'))
    log_level: str = os.getenv('LOG_LEVEL', 'INFO')
    http_port: int = int(os.getenv('HTTP_PORT', '5252'))


config = Config()
START_TIME = datetime.now()


def setup_logging():
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        if os.path.isdir('/app/logs') and os.access('/app/logs', os.W_OK):
            handlers.append(
                RotatingFileHandler('/app/logs/app.log', maxBytes=5 << 20, backupCount=5)
            )
    except OSError as e:
        print(f"Warning: cannot write to /app/logs ({e}), logging to console only")

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=handlers,
        force=True,
    )

    # LOG_LEVEL=DEBUG is for diagnosing session detection, not HTTP plumbing.
    # Left at DEBUG these log every poll's connection and bury the useful lines.
    for noisy in ('urllib3', 'requests', 'plexapi'):
        logging.getLogger(noisy).setLevel(logging.INFO)


setup_logging()
logger = logging.getLogger('plex-qbt')

app = Flask(__name__)
state: Optional["StateManager"] = None  # created in main()


@dataclass
class Session:
    """A tracked remote Plex session. `since` is set once it stops playing."""

    delay: int
    since: Optional[datetime] = None
    reason: Optional[str] = None
    last_playing: Optional[datetime] = None

    def remaining(self, now: datetime) -> Optional[float]:
        if self.since is None:
            return None
        return max(0.0, self.delay - (now - self.since).total_seconds())

    def expired(self, now: datetime) -> bool:
        return self.since is not None and (now - self.since).total_seconds() >= self.delay

    def start_timer(self, now: datetime, reason: str, delay: int):
        self.since, self.reason, self.delay = now, reason, delay

    def clear_timer(self, now: datetime):
        self.since = self.reason = None
        self.last_playing = now

    def snapshot(self, now: datetime) -> Dict[str, Any]:
        remaining = self.remaining(now)
        return {
            'last_playing': self.last_playing.isoformat() if self.last_playing else None,
            'reason': self.reason,
            'seconds_remaining': round(remaining, 1) if remaining is not None else None,
        }


class StateManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.sessions: Dict[str, Session] = {}
        self.alt_speeds = False
        self.last_change = datetime.now()
        self.shutdown = False
        self.lock = threading.RLock()
        # Serialises refreshes so a webhook and the poll loop can't both read
        # Plex, then both toggle qBittorrent, and cancel each other out.
        self.refresh_lock = threading.Lock()
        self.plex = None
        self.qbt = None
        self._connect()

    # -- connections ----------------------------------------------------

    @staticmethod
    def _retry(name: str, connect, attempts: int = 3):
        for attempt in range(attempts):
            try:
                return connect()
            except Exception as e:
                logger.warning(f"{name} connection attempt {attempt + 1} failed: {e}")
                if attempt < attempts - 1:
                    time.sleep(2 ** attempt)
        logger.error(f"Failed to connect to {name}")
        return None

    def _open_plex(self):
        plex = PlexServer(self.cfg.plex_url, self.cfg.plex_token)
        logger.info(f"Connected to Plex server: {plex.friendlyName}")
        return plex

    def _open_qbt(self):
        qbt = QBittorrentClient(
            host=self.cfg.qbt_url,
            username=self.cfg.qbt_username,
            password=self.cfg.qbt_password,
        )
        qbt.auth_log_in()
        logger.info(f"Connected to qBittorrent: {qbt.app.version}")
        return qbt

    def _connect(self):
        self.plex = self._retry("Plex", self._open_plex)
        self.qbt = self._retry("qBittorrent", self._open_qbt)
        if self.qbt:
            self.alt_speeds = bool(int(self.qbt.transfer.speed_limits_mode))
            logger.info(f"Initial alternative speeds: {'on' if self.alt_speeds else 'off'}")

    def _reconnect(self):
        if self.plex is None:
            self.plex = self._retry("Plex", self._open_plex, attempts=1)
        if self.qbt is None or not getattr(self.qbt, 'is_logged_in', False):
            self.qbt = self._retry("qBittorrent", self._open_qbt, attempts=1)

    # -- session events -------------------------------------------------

    def refresh(self):
        """Re-read Plex, expire timers, apply the result to qBittorrent.

        The only way session state changes. Webhooks call this instead of
        mutating sessions themselves, because a Plex webhook payload has no
        sessionKey and so cannot be matched to the sessions the API reports.
        """
        with self.refresh_lock:
            self.sync()
            self.tick()

    def tick(self):
        """Drop expired sessions and reconcile. Runs even when Plex is unreachable,
        so alternative speeds can't get stuck on because a sync failed."""
        with self.lock:
            now = datetime.now()
            for key in [k for k, s in self.sessions.items() if s.expired(now)]:
                logger.info(f"Session {key} timer expired ({self.sessions[key].delay}s) - dropped")
                del self.sessions[key]
            self._reconcile()

    # -- Plex polling ---------------------------------------------------

    @staticmethod
    def _is_remote(session) -> bool:
        """Plex marks LAN playback local=1. Default to remote so throttling still happens."""
        for obj in (session, *(getattr(session, 'players', None) or [])):
            if hasattr(obj, 'local'):
                return not bool(obj.local)
        logger.warning("No 'local' attribute on session, assuming remote")
        return True

    def sync(self):
        """Reconcile tracked sessions against what Plex currently reports."""
        if not self.plex:
            self._reconnect()
            return

        try:
            playing, idle = set(), set()
            for session in self.plex.sessions():
                players = getattr(session, 'players', None)
                if not players:
                    continue
                user = session.usernames[0] if session.usernames else 'unknown'
                key = f"{session.sessionKey}_{user}"
                remote = self._is_remote(session)
                # Logged for every session, not just ignored ones: the usual
                # question at DEBUG is why a session was judged the way it was.
                logger.debug(
                    f"Session {key}: {'remote' if remote else 'local'}, "
                    f"state={players[0].state}"
                )
                if not remote:
                    continue
                (playing if players[0].state == 'playing' else idle).add(key)
        except PlexApiException as e:
            logger.error(f"Plex API error during sync: {e}")
            self.plex = None
            return
        except Exception as e:
            logger.error(f"Unexpected error during Plex sync: {e}")
            return

        now = datetime.now()
        with self.lock:
            for key in playing:
                session = self.sessions.get(key)
                if session is None:
                    self.sessions[key] = Session(self.cfg.stop_delay_seconds, last_playing=now)
                    logger.info(f"Session {key} seen playing via polling")
                else:
                    if session.since is not None:
                        logger.info(f"Session {key} back to playing - timer cleared")
                    session.clear_timer(now)

            # Only sessions we already track get a pause timer; an idle session we
            # never saw playing isn't ours to throttle for.
            for key in idle:
                session = self.sessions.get(key)
                if session is not None and session.since is None:
                    session.start_timer(now, 'paused/buffered', self.cfg.pause_buffer_delay_seconds)
                    logger.info(f"Session {key} not playing - {session.delay}s timer started")

            # Gone from Plex entirely: treat as stopped.
            for key, session in self.sessions.items():
                if key not in playing and key not in idle and session.since is None:
                    session.start_timer(now, 'stopped', self.cfg.stop_delay_seconds)
                    logger.info(f"Session {key} gone from Plex - {session.delay}s timer started")

    # -- qBittorrent ----------------------------------------------------

    def _read_alt_speeds(self) -> Optional[bool]:
        """Current state from qBittorrent, or None if it can't be reached."""
        if not self.qbt:
            self._reconnect()
            if not self.qbt:
                return None
        try:
            # speed_limits_mode: 1 = alternative limits active, 0 = normal
            return bool(int(self.qbt.transfer.speed_limits_mode))
        except APIConnectionError as e:
            logger.error(f"qBittorrent connection error: {e}")
            self.qbt = None
            return None
        except Exception as e:
            logger.error(f"Error reading alternative speeds: {e}")
            return None

    def _reconcile(self):
        """Alternative speeds on iff any session is tracked. Caller holds the lock.

        qBittorrent is read every cycle rather than trusted from cache, so a
        change made by anything else — the Web UI, another script — is noticed
        and corrected instead of leaving us acting on a stale belief.
        """
        actual = self._read_alt_speeds()
        if actual is None:
            return

        if actual != self.alt_speeds:
            logger.warning(
                f"Alternative speeds changed externally: expected "
                f"{'on' if self.alt_speeds else 'off'}, found {'on' if actual else 'off'}"
            )
            self.alt_speeds = actual

        want = bool(self.sessions)
        if want == actual:
            return

        elapsed = (datetime.now() - self.last_change).total_seconds()
        if elapsed < max(1, self.cfg.debounce_seconds // 2):
            logger.debug(f"Debouncing speed change ({elapsed:.1f}s since last)")
            return

        if self._toggle_alt_speeds(want):
            self.alt_speeds = want
            self.last_change = datetime.now()
            logger.info(
                f"Alternative speeds {'enabled' if want else 'disabled'} "
                f"({len(self.sessions)} tracked sessions)"
            )
        else:
            logger.error("Failed to update alternative speeds")

    def _toggle_alt_speeds(self, enable: bool) -> bool:
        """Flip the toggle and verify. Only called when it's known to be wrong."""
        try:
            self.qbt.transfer.toggle_speed_limits_mode()
            return bool(int(self.qbt.transfer.speed_limits_mode)) == enable
        except APIConnectionError as e:
            logger.error(f"qBittorrent connection error: {e}")
            self.qbt = None
            return False
        except Exception as e:
            logger.error(f"Error setting alternative speeds: {e}")
            return False

    # -- reporting ------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        with self.lock:
            now = datetime.now()
            return {
                'tracked_sessions': len(self.sessions),
                'sessions': {k: s.snapshot(now) for k, s in self.sessions.items()},
                'alt_speeds_enabled': self.alt_speeds,
                'plex_connected': self.plex is not None,
                'qbt_connected': self.qbt is not None
                and bool(getattr(self.qbt, 'is_logged_in', True)),
                'stop_delay_seconds': self.cfg.stop_delay_seconds,
                'pause_buffer_delay_seconds': self.cfg.pause_buffer_delay_seconds,
                'uptime_seconds': round((now - START_TIME).total_seconds(), 1),
            }


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

    if not payload:
        logger.warning(f"Unparseable webhook (Content-Type: {request.content_type})")
        return jsonify({'error': 'no valid payload'}), 400

    event = payload.get('event')
    logger.debug(f"Payload: {json.dumps(payload)}")

    if event not in PLAYBACK_EVENTS:
        logger.debug(f"Ignoring event {event}")
        return jsonify({'status': 'ignored', 'event': event}), 200

    # A Plex webhook has no sessionKey, so it can't be tied to a session from
    # the API. Treat it purely as "something changed, look now" and let the
    # Plex session list stay the single source of truth.
    account = (payload.get('Account') or {}).get('title', 'unknown')
    logger.info(f"Webhook {event} ({account}) - refreshing from Plex")
    state.refresh()

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
    logger.info(
        f"Polling every {config.polling_interval}s "
        f"(stop delay {config.stop_delay_seconds}s, "
        f"pause/buffer delay {config.pause_buffer_delay_seconds}s)"
    )
    while not state.shutdown:
        try:
            state.refresh()
        except Exception as e:
            logger.error(f"Polling error: {e}")
        for _ in range(config.polling_interval):
            if state.shutdown:
                break
            time.sleep(1)


def main():
    global state

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

    state = StateManager(config)

    def handle_signal(signum, _frame):
        logger.info(f"Signal {signum} received, shutting down")
        state.shutdown = True
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    threading.Thread(target=polling_loop, daemon=True).start()

    logger.info(f"Listening on port {config.http_port} (remote Plex sessions only)")
    serve(app, host='0.0.0.0', port=config.http_port, threads=8)


if __name__ == '__main__':
    main()

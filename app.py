import os
import sys
import json
import time
import signal
import logging
import threading
from logging.handlers import RotatingFileHandler
from datetime import datetime
from typing import Optional, Dict, Any
from dataclasses import dataclass

from flask import Flask, request, jsonify
from plexapi.server import PlexServer
from plexapi.exceptions import PlexApiException
from qbittorrentapi import Client as QBittorrentClient
from qbittorrentapi.exceptions import APIConnectionError
from waitress import serve


# Configuration
@dataclass
class Config:
    plex_url: str = os.getenv('PLEX_URL', 'http://plex:32400')
    plex_token: str = os.getenv('PLEX_TOKEN', '')
    qbt_url: str = os.getenv('QBITTORRENT_URL', 'http://qbittorrent:8080')
    qbt_username: str = os.getenv('QBITTORRENT_USERNAME', '')
    qbt_password: str = os.getenv('QBITTORRENT_PASSWORD', '')
    polling_interval: int = int(os.getenv('POLLING_INTERVAL', '5'))
    debounce_seconds: int = int(os.getenv('DEBOUNCE_SECONDS', '3'))
    # Delay after stream ends before disabling alt speeds (default: 30s)
    stop_delay_seconds: int = int(os.getenv('STOP_DELAY_SECONDS', '30'))
    # Delay after pause/buffer before disabling alt speeds (default: 60s)
    pause_buffer_delay_seconds: int = int(os.getenv('PAUSE_BUFFER_DELAY_SECONDS', '60'))
    log_level: str = os.getenv('LOG_LEVEL', 'INFO')
    http_port: int = int(os.getenv('HTTP_PORT', '5252'))


config = Config()
START_TIME = datetime.now()


def setup_logging():
    """Configure logging to stdout, plus a rotating /app/logs/app.log when writable."""
    log_handlers = [logging.StreamHandler(sys.stdout)]

    try:
        if os.path.isdir('/app/logs') and os.access('/app/logs', os.W_OK):
            # Capped at 5 x 5MB; an unrotated handler here grows without bound.
            log_handlers.append(
                RotatingFileHandler(
                    '/app/logs/app.log', maxBytes=5 * 1024 * 1024, backupCount=5
                )
            )
    except (PermissionError, OSError) as e:
        print(f"Warning: cannot write to /app/logs ({e}), logging to console only")

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=log_handlers,
        force=True  # Override any existing configuration
    )


setup_logging()
logger = logging.getLogger(__name__)


app = Flask(__name__)
state_manager = None  # Created in main()


class SessionState:
    """Track state for a single session."""
    def __init__(self, delay_seconds: int = 30):
        self.is_active = True  # Currently counted towards alt-speed activation
        self.not_playing_since: Optional[datetime] = None  # When we first saw it not playing
        self.delay_seconds: int = delay_seconds  # How long to wait before removing
        self.reason: Optional[str] = None  # Why the timer is running
        self.last_seen_playing: Optional[datetime] = None  # Last time we saw it playing


class StateManager:
    def __init__(self, config: Config):
        self.config = config
        # session_key -> SessionState
        self.sessions: Dict[str, SessionState] = {}
        self.alt_speeds_enabled = False
        self.last_state_change = datetime.now()
        self.shutdown_requested = False
        self.lock = threading.RLock()

        # Initialize clients
        self.plex = None
        self.qbt = None
        self._init_clients()

    def _init_clients(self):
        """Initialize Plex and qBittorrent clients with retry logic."""
        max_retries = 3

        # Initialize Plex client
        for attempt in range(max_retries):
            try:
                self.plex = PlexServer(self.config.plex_url, self.config.plex_token)
                logger.info(f"Connected to Plex server: {self.plex.friendlyName}")
                break
            except Exception as e:
                logger.warning(f"Plex connection attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    logger.error("Failed to connect to Plex server")

        # Initialize qBittorrent client
        for attempt in range(max_retries):
            try:
                self.qbt = QBittorrentClient(
                    host=self.config.qbt_url,
                    username=self.config.qbt_username,
                    password=self.config.qbt_password
                )
                self.qbt.auth_log_in()
                logger.info(f"Connected to qBittorrent: {self.qbt.app.version}")

                # Get initial alternative speeds state
                self.alt_speeds_enabled = bool(int(self.qbt.transfer.speed_limits_mode))
                logger.info(
                    f"Initial alternative speeds state: "
                    f"{'enabled' if self.alt_speeds_enabled else 'disabled'}"
                )
                break
            except Exception as e:
                logger.warning(f"qBittorrent connection attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    self.qbt = None
                    logger.error("Failed to connect to qBittorrent")

    def reconnect_clients(self):
        """Attempt to reconnect clients if they're disconnected."""
        try:
            if self.plex is None:
                self.plex = PlexServer(self.config.plex_url, self.config.plex_token)
                logger.info("Reconnected to Plex server")
        except Exception as e:
            logger.error(f"Failed to reconnect to Plex: {e}")

        try:
            if self.qbt is None or not self.qbt.is_logged_in:
                self.qbt = QBittorrentClient(
                    host=self.config.qbt_url,
                    username=self.config.qbt_username,
                    password=self.config.qbt_password
                )
                self.qbt.auth_log_in()
                logger.info("Reconnected to qBittorrent")
        except Exception as e:
            self.qbt = None
            logger.error(f"Failed to reconnect to qBittorrent: {e}")

    def add_remote_session(self, session_key: str):
        """Add or reactivate a remote session (playing)."""
        with self.lock:
            if session_key in self.sessions:
                state = self.sessions[session_key]
                was_not_playing = state.not_playing_since is not None
                state.is_active = True
                state.not_playing_since = None
                state.reason = None
                state.last_seen_playing = datetime.now()
                if was_not_playing:
                    logger.info(f"Session {session_key} resumed playing - timer cleared")
                else:
                    logger.info(
                        f"Session {session_key} playing (active sessions: {self._count_active()})"
                    )
            else:
                state = SessionState(self.config.stop_delay_seconds)
                state.last_seen_playing = datetime.now()
                self.sessions[session_key] = state
                logger.info(
                    f"New session {session_key} playing (active sessions: {self._count_active()})"
                )

            self._update_speeds()

    def mark_remote_not_playing(self, session_key: str, reason: str = "paused/buffered"):
        """Mark a session as not playing with appropriate delay."""
        with self.lock:
            if session_key not in self.sessions:
                # Create if not exists (shouldn't happen but handle gracefully)
                self.sessions[session_key] = SessionState(self.config.stop_delay_seconds)

            state = self.sessions[session_key]

            # Only start timer if not already started
            if state.not_playing_since is None:
                state.not_playing_since = datetime.now()
                state.reason = reason
                # Use stop delay for stop events, pause/buffer delay otherwise
                if reason == "stopped":
                    state.delay_seconds = self.config.stop_delay_seconds
                else:
                    state.delay_seconds = self.config.pause_buffer_delay_seconds
                logger.info(f"Session {session_key} {reason} - started {state.delay_seconds}s timer")

    def _count_active(self) -> int:
        """Count sessions that are currently active (not in delayed removal)."""
        return sum(1 for s in self.sessions.values() if s.is_active)

    def _cleanup_expired_sessions(self) -> bool:
        """Remove sessions whose delay has expired. Returns True if any were removed."""
        now = datetime.now()
        removed = []

        for session_key, state in list(self.sessions.items()):
            if state.not_playing_since is not None:
                elapsed = (now - state.not_playing_since).total_seconds()
                if elapsed >= state.delay_seconds:
                    removed.append(session_key)
                    logger.info(
                        f"Session {session_key} delay expired ({state.delay_seconds}s) - removed"
                    )

        for session_key in removed:
            self.sessions.pop(session_key, None)

        return len(removed) > 0

    def tick(self):
        """Expire timers and reconcile speeds.

        Runs on every poll even when Plex is unreachable, so alternative speeds
        are never left stuck on because the Plex sync failed.
        """
        with self.lock:
            self._cleanup_expired_sessions()
            self._update_speeds()

    def is_session_remote(self, session) -> bool:
        """Determine if a Plex session is remote based on the 'local' attribute."""
        try:
            # Check if the session has the 'local' attribute
            if hasattr(session, 'local'):
                is_local = session.local
                # Remote sessions have local=False or local=0
                is_remote = not bool(is_local)
                logger.debug(
                    f"Session local attribute: {is_local}, "
                    f"treating as {'remote' if is_remote else 'local'}"
                )
                return is_remote

            # Fallback: check player's local attribute if session doesn't have it
            if hasattr(session, 'players') and session.players:
                player = session.players[0]
                if hasattr(player, 'local'):
                    is_local = player.local
                    is_remote = not bool(is_local)
                    logger.debug(
                        f"Player local attribute: {is_local}, "
                        f"treating as {'remote' if is_remote else 'local'}"
                    )
                    return is_remote

            # If no local attribute is found, log warning and default to remote
            # This ensures the speed limiting still works if the attribute is missing
            logger.warning("No 'local' attribute found for session, defaulting to remote")
            return True

        except Exception as e:
            logger.error(f"Error checking if session is remote: {e}")
            # Default to remote on error to be safe
            return True

    def sync_plex_sessions(self):
        """Synchronize active remote sessions with Plex server state."""
        if not self.plex:
            self.reconnect_clients()
            return

        try:
            # Get current Plex sessions
            sessions = self.plex.sessions()
            current_remote_playing = set()
            current_remote_not_playing = set()
            total_sessions = 0
            local_sessions = 0
            remote_playing = 0
            remote_paused = 0
            remote_buffering = 0

            for session in sessions:
                total_sessions += 1

                if hasattr(session, 'players') and session.players:
                    player = session.players[0]
                    username = session.usernames[0] if session.usernames else 'unknown'
                    session_key = f"{session.sessionKey}_{username}"

                    # Check if this is a remote session
                    if self.is_session_remote(session):
                        if player.state == 'playing':
                            current_remote_playing.add(session_key)
                            remote_playing += 1
                            logger.debug(f"Remote playing session: {session_key}")
                        else:
                            # Any non-playing state (paused, buffering, etc.)
                            current_remote_not_playing.add(session_key)
                            if player.state == 'paused':
                                remote_paused += 1
                            elif player.state == 'buffering':
                                remote_buffering += 1
                            logger.debug(
                                f"Remote not-playing session ({player.state}): {session_key}"
                            )
                    else:
                        local_sessions += 1
                        logger.debug(f"Local session: {session_key} (ignored)")

            # Update our tracked remote sessions
            with self.lock:
                # Mark playing sessions as active
                for session_key in current_remote_playing:
                    if session_key in self.sessions:
                        state = self.sessions[session_key]
                        # Was not playing, now playing again - clear timer
                        if state.not_playing_since is not None:
                            state.not_playing_since = None
                            state.reason = None
                            state.is_active = True
                            logger.info(
                                f"Session {session_key} back to playing from polling - timer cleared"
                            )
                        state.last_seen_playing = datetime.now()
                    else:
                        # New session seen via polling
                        state = SessionState(self.config.stop_delay_seconds)
                        state.last_seen_playing = datetime.now()
                        self.sessions[session_key] = state
                        logger.info(f"New session {session_key} seen via polling")

                # Mark not-playing sessions with timer
                for session_key in current_remote_not_playing:
                    if session_key in self.sessions:
                        state = self.sessions[session_key]
                        if state.not_playing_since is None:
                            state.not_playing_since = datetime.now()
                            state.delay_seconds = self.config.pause_buffer_delay_seconds
                            state.reason = 'paused/buffered'
                            logger.info(
                                f"Session {session_key} not-playing from polling - "
                                f"started {state.delay_seconds}s timer"
                            )

                # Sessions that disappeared from Plex entirely - treat as stopped
                all_seen = current_remote_playing | current_remote_not_playing
                for session_key, state in self.sessions.items():
                    if session_key not in all_seen and state.not_playing_since is None:
                        state.not_playing_since = datetime.now()
                        state.delay_seconds = self.config.stop_delay_seconds
                        state.reason = 'stopped'
                        logger.info(
                            f"Session {session_key} disappeared from Plex - "
                            f"started {state.delay_seconds}s stop timer"
                        )

                # Clean up expired sessions
                expired_cleaned = self._cleanup_expired_sessions()

                if expired_cleaned or current_remote_playing or current_remote_not_playing:
                    timer_info = []
                    now = datetime.now()
                    for sk, st in self.sessions.items():
                        if st.not_playing_since is not None:
                            elapsed = (now - st.not_playing_since).total_seconds()
                            remaining = max(0, st.delay_seconds - elapsed)
                            timer_info.append(f"{sk}({remaining:.0f}s)")

                    logger.debug(
                        f"Synced Plex sessions: {total_sessions} total, {local_sessions} local, "
                        f"{remote_playing} playing, {remote_paused} paused, "
                        f"{remote_buffering} buffering | active: {self._count_active()}, "
                        f"timers: {len(timer_info)} [{', '.join(timer_info)}]"
                    )

                # Always reconcile: a session first seen via polling must be able to
                # turn alternative speeds on without waiting for a webhook.
                self._update_speeds()

        except PlexApiException as e:
            logger.error(f"Plex API error during sync: {e}")
            self.plex = None
        except Exception as e:
            logger.error(f"Unexpected error during Plex sync: {e}")

    def _update_speeds(self):
        """Update qBittorrent alternative speeds based on active remote sessions.

        Caller must hold self.lock.
        """
        active_count = self._count_active()
        should_enable = active_count > 0

        if should_enable == self.alt_speeds_enabled:
            return

        # Reduce debounce time or skip it for immediate webhook responses
        time_since_change = datetime.now() - self.last_state_change
        min_debounce_time = max(1, self.config.debounce_seconds // 2)

        if time_since_change.total_seconds() < min_debounce_time:
            logger.debug(
                f"Debouncing speed change (last change {time_since_change.total_seconds():.1f}s ago)"
            )
            return

        if self._set_alternative_speeds(should_enable):
            self.alt_speeds_enabled = should_enable
            self.last_state_change = datetime.now()
            action = "enabled" if should_enable else "disabled"
            if should_enable:
                logger.info(f"Alternative speeds {action} ({active_count} active sessions)")
            else:
                logger.info(f"Alternative speeds {action} (no active sessions after delay expired)")
        else:
            logger.error("Failed to update alternative speeds")

    def _set_alternative_speeds(self, enable: bool) -> bool:
        """Enable or disable qBittorrent alternative-speed limits."""
        if not self.qbt:
            self.reconnect_clients()
            if not self.qbt:
                return False

        try:
            # Toggle only if the current state is the opposite of what we want
            current = int(self.qbt.transfer.speed_limits_mode)  # 1=alt on, 0=alt off
            if bool(current) != enable:
                self.qbt.transfer.toggle_speed_limits_mode()

            # Verify the change
            new_state = int(self.qbt.transfer.speed_limits_mode)
            return bool(new_state) == enable

        except APIConnectionError as e:
            logger.error(f"qBittorrent API connection error: {e}")
            self.qbt = None
            return False
        except Exception as e:
            logger.error(f"Error setting alternative speeds: {e}")
            return False

    def get_status(self) -> Dict[str, Any]:
        """Get current status for health checks and debugging."""
        with self.lock:
            now = datetime.now()
            sessions_info = {}
            for session_key, state in self.sessions.items():
                info = {
                    'is_active': state.is_active,
                    'last_seen_playing': (
                        state.last_seen_playing.isoformat() if state.last_seen_playing else None
                    ),
                }
                if state.not_playing_since is not None:
                    elapsed = (now - state.not_playing_since).total_seconds()
                    remaining = max(0, state.delay_seconds - elapsed)
                    info['not_playing_elapsed'] = round(elapsed, 1)
                    info['not_playing_remaining'] = round(remaining, 1)
                    info['delay_seconds'] = state.delay_seconds
                    info['reason'] = state.reason or 'paused/buffered'
                sessions_info[session_key] = info

            return {
                'active_sessions_count': self._count_active(),
                'total_tracked_sessions': len(self.sessions),
                'sessions_detail': sessions_info,
                'stop_delay_seconds': self.config.stop_delay_seconds,
                'pause_buffer_delay_seconds': self.config.pause_buffer_delay_seconds,
                'alt_speeds_enabled': self.alt_speeds_enabled,
                'plex_connected': self.plex is not None,
                'qbt_connected': self.qbt is not None and bool(
                    getattr(self.qbt, 'is_logged_in', True)
                ),
                'last_state_change': self.last_state_change.isoformat(),
                'uptime_seconds': round((now - START_TIME).total_seconds(), 1),
            }


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint for Docker and monitoring."""
    if not state_manager:
        return jsonify({'status': 'initializing', 'timestamp': datetime.now().isoformat()}), 503

    status = state_manager.get_status()
    is_healthy = status['plex_connected'] and status['qbt_connected']

    return jsonify({
        'status': 'healthy' if is_healthy else 'unhealthy',
        'timestamp': datetime.now().isoformat(),
        **status
    }), 200 if is_healthy else 503


@app.route('/webhook', methods=['POST'])
def plex_webhook():
    """Handle Plex webhook events for remote sessions only."""
    if not state_manager:
        return jsonify({'error': 'Service initializing'}), 503

    try:
        # Handle both JSON and form-encoded payloads (Plex can send either)
        payload = None

        if request.is_json:
            payload = request.get_json(silent=True)
        elif request.form:
            # Plex sends multipart/form-data with a 'payload' field containing JSON
            payload_str = request.form.get('payload')
            if payload_str:
                payload = json.loads(payload_str)
        elif request.data:
            # Try to parse raw data as JSON
            try:
                payload = json.loads(request.data.decode('utf-8'))
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

        if not payload:
            logger.warning(
                f"No valid payload found. Content-Type: {request.content_type}, "
                f"Data: {request.data[:200]}"
            )
            return jsonify({'error': 'No valid payload found'}), 400

        # Extract event information
        event = payload.get('event')
        account = payload.get('Account', {})
        player = payload.get('Player', {})

        session_key = f"{payload.get('sessionKey', 'unknown')}_{account.get('title', 'unknown')}"

        logger.info(f"Received webhook: {event} for session {session_key}")
        logger.debug(f"Webhook payload: {json.dumps(payload, indent=2)}")

        # Check if this is a remote session using Player data
        is_remote = True  # Default to remote for webhook events
        if isinstance(player, dict):
            if 'local' in player:
                is_remote = not bool(player.get('local'))
            elif 'Local' in player:
                is_remote = not bool(player.get('Local'))

        session_type = "remote" if is_remote else "local"
        logger.info(f"Session {session_key} is {session_type}")

        # Only handle remote sessions
        if not is_remote:
            logger.info(f"Ignoring local session: {session_key}")
            return jsonify({'status': 'ignored', 'event': event, 'reason': 'local_session'}), 200

        # Handle different event types for remote sessions only
        if event in ('media.play', 'media.resume'):
            state_manager.add_remote_session(session_key)
        elif event == 'media.stop':
            # Stream ended - start stop delay timer (don't remove immediately!)
            state_manager.mark_remote_not_playing(session_key, reason="stopped")
        elif event == 'media.pause':
            state_manager.mark_remote_not_playing(session_key, reason="paused")
        elif event == 'media.buffer':
            state_manager.mark_remote_not_playing(session_key, reason="buffered")
        else:
            logger.debug(f"Ignoring webhook event: {event}")

        return jsonify({'status': 'success', 'event': event, 'session_type': session_type}), 200

    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error in webhook: {e}")
        return jsonify({'error': 'Invalid JSON payload'}), 400
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/webhook-test', methods=['POST', 'GET'])
def webhook_test():
    """Echo whatever Plex sent, to debug webhook payload formats."""
    logger.info(f"Webhook test - Method: {request.method}")
    logger.info(f"Content-Type: {request.content_type}")
    logger.info(f"Headers: {dict(request.headers)}")

    if request.method == 'POST':
        logger.info(f"Form data: {dict(request.form)}")
        logger.info(f"Raw data: {request.data[:500]}")

        if request.is_json:
            logger.info(f"JSON data: {request.get_json(silent=True)}")
        elif request.form and 'payload' in request.form:
            try:
                logger.info(f"Form payload JSON: {json.loads(request.form['payload'])}")
            except json.JSONDecodeError:
                logger.info(f"Form payload (not JSON): {request.form['payload']}")

    return jsonify({'status': 'test_complete', 'timestamp': datetime.now().isoformat()})


@app.route('/status', methods=['GET'])
def get_status():
    """Get current application status."""
    if not state_manager:
        return jsonify({'status': 'initializing'}), 503
    return jsonify(state_manager.get_status())


def polling_loop():
    """Background polling loop to sync with Plex server."""
    logger.info(
        f"Starting polling loop (interval: {config.polling_interval}s, "
        f"stop delay: {config.stop_delay_seconds}s, "
        f"pause/buffer delay: {config.pause_buffer_delay_seconds}s)"
    )

    while not state_manager.shutdown_requested:
        try:
            state_manager.sync_plex_sessions()
            # Expire timers even if the Plex sync above bailed out, so alternative
            # speeds can't get stuck on when Plex is unreachable.
            state_manager.tick()
        except Exception as e:
            logger.error(f"Error in polling loop: {e}")

        # Sleep with early exit if shutdown requested
        for _ in range(config.polling_interval):
            if state_manager.shutdown_requested:
                break
            time.sleep(1)

    logger.info("Polling loop stopped")


def signal_handler(signum, frame):
    """Handle graceful shutdown signals."""
    logger.info(f"Received signal {signum}, shutting down...")
    if state_manager:
        state_manager.shutdown_requested = True
    sys.exit(0)


def main():
    """Main application entry point."""
    global state_manager

    # Validate required configuration
    if not config.plex_token:
        logger.error("PLEX_TOKEN environment variable is required")
        sys.exit(1)

    if not config.qbt_username or not config.qbt_password:
        logger.error(
            "QBITTORRENT_USERNAME and QBITTORRENT_PASSWORD environment variables are required"
        )
        sys.exit(1)

    state_manager = StateManager(config)

    # Setup signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start background polling thread
    polling_thread = threading.Thread(target=polling_loop, daemon=True)
    polling_thread.start()

    logger.info(
        f"Starting Plex-qBittorrent Speed Manager (remote sessions only) "
        f"on port {config.http_port}"
    )

    try:
        serve(app, host='0.0.0.0', port=config.http_port, threads=8)
    except KeyboardInterrupt:
        logger.info("Received KeyboardInterrupt")
    finally:
        if state_manager:
            state_manager.shutdown_requested = True
        logger.info("Application shutdown complete")


if __name__ == '__main__':
    main()

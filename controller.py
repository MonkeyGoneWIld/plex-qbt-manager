"""Plex session reconciliation and dynamic qBittorrent upload control.

Plex /status/sessions Session/@bandwidth is decimal kbps, as used by Tautulli.
qBittorrent Web API v2 app/preferences alt_up_limit is integer bytes/second.
"""
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from urllib.parse import urlsplit
from theater import Theater

from plexapi.server import PlexServer
from qbittorrentapi import Client as QBittorrentClient

logger = logging.getLogger('plex-qbt')
MIB = 1024 ** 2


def decimal_value(value, name):
    try:
        result = Decimal(str(value))
        if result.is_finite():
            return result
    except InvalidOperation:
        pass
    raise ValueError(f'{name} must be a finite number')


def validate_config(cfg):
    for name in ('polling_interval', 'debounce_seconds', 'stop_delay_seconds',
                 'pause_buffer_delay_seconds', 'plex_timeout', 'qbt_timeout',
                 'drift_check_seconds', 'plex_stale_seconds', 'http_port',
                 'theater_poll_interval_seconds', 'theater_timeout_seconds', 'theater_stale_seconds'):
        value = decimal_value(getattr(cfg, name), name.upper())
        minimum = 0 if name in ('debounce_seconds', 'stop_delay_seconds', 'pause_buffer_delay_seconds') else 1
        if value != int(value) or value < minimum:
            raise ValueError(f'{name.upper()} must be an integer >= {minimum}')
        setattr(cfg, name, int(value))
    if cfg.http_port > 65535:
        raise ValueError('HTTP_PORT must be <= 65535')
    enabled = str(cfg.dynamic_upload_enabled).strip().lower()
    if enabled not in ('true', 'false', '1', '0', 'yes', 'no'):
        raise ValueError('DYNAMIC_UPLOAD_ENABLED must be true or false')
    cfg.dynamic_upload_enabled = enabled in ('true', '1', 'yes')
    for name in ('max_upload_mib', 'min_upload_mib', 'bandwidth_multiplier', 'theater_bandwidth_factor'):
        value = decimal_value(getattr(cfg, name), name.upper())
        if value <= 0:
            raise ValueError(f'{name.upper()} must be greater than zero')
        setattr(cfg, name, value)
    if cfg.theater_url:
        url = urlsplit(cfg.theater_url)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('THEATER_URL must be an HTTP(S) URL without credentials, query or fragment')
        if not cfg.theater_api_key or '\n' in cfg.theater_api_key or '\r' in cfg.theater_api_key:
            raise ValueError('THEATER_API_KEY is required when THEATER_URL is set')
    if cfg.min_upload_mib > cfg.max_upload_mib:
        raise ValueError('MIN_UPLOAD_MIB must not exceed MAX_UPLOAD_MIB')
    cfg.min_upload_bps = int((cfg.min_upload_mib * MIB).to_integral_value(rounding=ROUND_CEILING))
    cfg.max_upload_bps = int(cfg.max_upload_mib * MIB)
    if not 1 <= cfg.min_upload_bps <= cfg.max_upload_bps <= 2 ** 31 - 1:
        raise ValueError('Upload limits must fit positive integer bytes/s (maximum < 2048 MiB/s)')
    if (cfg.min_upload_bps + 1023) // 1024 > cfg.max_upload_bps // 1024:
        raise ValueError('Upload range must contain at least one whole KiB/s (qBittorrent precision)')


def upload_budget(cfg, bandwidth_kbps, unknown=False):
    if unknown:
        return cfg.min_upload_bps
    reduction = Decimal(bandwidth_kbps) * 1000 * cfg.bandwidth_multiplier / 8
    return int(max(cfg.min_upload_bps, min(cfg.max_upload_bps, cfg.max_upload_bps - reduction)))


def qbt_budget(cfg, calculated_bps):
    # qBittorrent accepts B/s but persists these limits in whole KiB/s. Round
    # down for headroom, except the minimum rounds UP to preserve the floor.
    return max((cfg.min_upload_bps + 1023) // 1024, calculated_bps // 1024) * 1024


@dataclass
class Observation:
    key: str
    player_id: str
    rating_key: str
    user: str
    playback: str
    bandwidth_kbps: int | None
    remote: bool = True
    session_id: str = ''
    transcode_key: str = ''
    user_id: str = ''
    theater_revision: tuple | None = None
    state_since: float | None = None
    theater_details: dict | None = None


def read_observations(root, include_local=False):
    """Parse raw XML without lazy metadata requests or additional credentials."""
    if root is None or root.tag != 'MediaContainer':
        raise ValueError('Plex returned an invalid session container')
    result = {}
    for item in root:
        if item.tag not in ('Video', 'Track', 'Photo'):
            continue
        player, session, user = item.find('Player'), item.find('Session'), item.find('User')
        if player is None or not item.get('sessionKey'):
            raise ValueError('Plex session missing Player or sessionKey; retaining previous snapshot')
        key = item.get('sessionKey')
        location = session.get('location', '').lower() if session is not None else ''
        local = player.get('local', '').lower()
        remote = local not in ('1', 'true') if local in ('0', '1', 'true', 'false') else location != 'lan'
        playback = player.get('state', 'unknown').lower()
        raw = session.get('bandwidth') if session is not None else None
        bandwidth = None
        try:
            if raw is not None and int(raw) > 0:
                bandwidth = int(raw)
        except (ValueError, TypeError):
            pass
        logger.debug('Plex session=%r player=%r rating_key=%r state=%r remote=%s location=%r bandwidth_kbps=%r',
                     key, player.get('machineIdentifier'), item.get('ratingKey'), playback, remote, location, raw)
        if not remote and not include_local:
            continue
        if not local and not location:
            logger.warning('Plex session=%r has no locality; assuming remote', key)
        if key in result:
            logger.warning('Duplicate Plex session=%r ignored', key)
            continue
        result[key] = Observation(key, player.get('machineIdentifier', ''), item.get('ratingKey', ''),
                                  user.get('title', '') if user is not None else '', playback, bandwidth,
                                  remote=remote, session_id=session.get('id', '') if session is not None else '',
                                  transcode_key=item.find('TranscodeSession').get('key', '').rstrip('/').split('/')[-1] if item.find('TranscodeSession') is not None else '',
                                  user_id=user.get('id', '') if user is not None else '')
    return result


@dataclass
class Session:
    observation: Observation
    bandwidth_kbps: int | None = None
    bandwidth_source: str = 'unknown'
    playback: str = 'new'
    since: float | None = None
    delay: int = 0
    last_playing: str | None = None

    def remaining(self, now):
        return None if self.since is None else max(0, self.delay - (now - self.since))

    def snapshot(self, now):
        return {'state': self.playback, 'reason': self.playback if self.since is not None else None,
                'last_playing': self.last_playing, 'bandwidth_kbps': float(self.bandwidth_kbps) if self.bandwidth_kbps is not None else None,
                'theater': self.observation.theater_details,
                'bandwidth_mbps': float(self.bandwidth_kbps / 1000) if self.bandwidth_kbps is not None else None,
                'bandwidth_source': self.bandwidth_source, 'seconds_remaining': self.remaining(now)}


@dataclass
class ResumeHint:
    player_id: str
    rating_key: str
    user: str
    server_id: str
    received: float


class StateManager:
    def __init__(self, cfg, clock=time.monotonic):
        validate_config(cfg)
        self.cfg, self.clock = cfg, clock
        self.started = clock()
        self.sessions = {}
        self.lock = threading.RLock()
        self.refresh_lock = threading.Lock()
        self.wake = threading.Event()
        self.hints = deque(maxlen=256)
        self.shutdown = False
        self.plex = self.qbt = None
        self.plex_failures = self.qbt_failures = 0
        self.last_plex_success = self.last_qbt_success = None
        self.last_plex_ok = self.qbt_ok = self.stale = False
        self.alt_speeds = self.applied_upload_bps = None
        self.desired_mode = self.desired_upload_bps = self.calculated_upload_bps = None
        self.last_change = self.last_drift_check = float('-inf')
        self.retry_qbt = True
        self.last_decision = None
        self.cycle = 0
        self.theater = Theater(cfg, clock) if cfg.theater_url else None
        self._connect()

    @staticmethod
    def _retry(name, connect, attempts=3):
        for attempt in range(attempts):
            try:
                return connect()
            except Exception as exc:
                logger.warning('%s connection attempt=%s/%s error=%s: %s',
                               name, attempt + 1, attempts, type(exc).__name__, exc)
                if attempt < attempts - 1:
                    time.sleep(2 ** attempt)
        return None

    def _open_plex(self):
        plex = PlexServer(self.cfg.plex_url, self.cfg.plex_token, timeout=self.cfg.plex_timeout)
        logger.info('Plex connected version=%s', plex.version)
        return plex

    def _open_qbt(self):
        qbt = QBittorrentClient(host=self.cfg.qbt_url, username=self.cfg.qbt_username,
                               password=self.cfg.qbt_password,
                               REQUESTS_ARGS={'timeout': (3.05, self.cfg.qbt_timeout)})
        qbt.auth_log_in()
        logger.info('qBittorrent connected version=%s', qbt.app.version)
        return qbt

    def _connect(self):
        self.plex = self._retry('Plex', self._open_plex)
        self.qbt = self._retry('qBittorrent', self._open_qbt)

    def poke(self, payload=None):
        # Only identity hints are retained; never store entire webhook payloads.
        if payload and payload.get('event') in ('media.play', 'media.resume'):
            player, media = payload.get('Player') or {}, payload.get('Metadata') or {}
            account, server = payload.get('Account') or {}, payload.get('Server') or {}
            if player.get('uuid') and media.get('ratingKey') and account.get('title') and server.get('uuid'):
                hint = ResumeHint(str(player['uuid']), str(media['ratingKey']), str(account['title']),
                                  str(server['uuid']), self.clock())
                with self.lock:
                    if len(self.hints) == self.hints.maxlen:
                        logger.warning('Resume hint queue full; oldest hint discarded')
                    self.hints.append(hint)
                logger.debug('Queued resume hint player=%r rating_key=%r', hint.player_id, hint.rating_key)
            else:
                logger.debug('Webhook has insufficient identity for a resume hint; polling only')
        self.wake.set()

    def _resumed_keys(self, observations, now):
        resumed, pending = set(), deque(maxlen=256)
        ttl = max(10, self.cfg.polling_interval * 2)
        server_id = str(getattr(self.plex, 'machineIdentifier', ''))
        for hint in self.hints:
            if now - hint.received > ttl or hint.server_id != server_id:
                logger.debug('Discarded expired or foreign-server resume hint')
                continue
            matches = [key for key, obs in observations.items()
                       if (obs.player_id, obs.rating_key, obs.user) == (hint.player_id, hint.rating_key, hint.user)]
            if len(matches) == 1:
                resumed.add(matches[0])
                logger.info('Session=%r matched resume webhook; previous grace timer cleared', matches[0])
            elif len(matches) > 1:
                logger.warning('Ambiguous resume hint matches=%s; polling remains authoritative', len(matches))
            else:
                logger.debug('Resume hint has no matching remote session yet; age_seconds=%.1f', now - hint.received)
                pending.append(hint)
        self.hints = pending
        return resumed

    def sync(self):
        if self.plex is None:
            self.plex = self._retry('Plex', self._open_plex, attempts=1)
        try:
            if self.plex is None:
                raise ConnectionError('Plex unavailable')
            observations = read_observations(self.plex.query('/status/sessions'), include_local=self.theater is not None)
        except Exception as exc:
            with self.lock:
                self.last_plex_ok = False
                self.plex_failures += 1
            logger.warning('cycle=%s Plex sync failed count=%s error=%s: %s; retaining last decision',
                           self.cycle, self.plex_failures, type(exc).__name__, exc)
            if self.plex_failures >= 3:
                self.plex = None
            return False
        now = self.clock()
        if self.theater:
            self.theater.poll()
            observations = self.theater.overlay(observations, str(getattr(self.plex, 'machineIdentifier', '')))
            now = self.clock()
        with self.lock:
            if self.plex_failures or self.stale:
                logger.info('Plex recovered after failures=%s; applying fresh snapshot', self.plex_failures)
            self.last_plex_success, self.last_plex_ok = now, True
            self.plex_failures, self.stale = 0, False
            resumed = self._resumed_keys(observations, now)
            if self.theater:
                owned = [entry['base'] for entry in self.theater.owned.values()]
                for key in list(self.sessions):
                    obs = self.sessions[key].observation
                    if obs.theater_revision is None and any(
                            (obs.session_id and obs.session_id == base.session_id) or
                            (obs.transcode_key and obs.transcode_key == base.transcode_key) for base in owned):
                        del self.sessions[key]
                        logger.info('Session=%r transferred to exact Theater ownership; ordinary reservation removed', key)
            for key, obs in observations.items():
                tracked = self.sessions.get(key)
                if tracked and (tracked.observation.player_id, tracked.observation.rating_key, tracked.observation.user) != (obs.player_id, obs.rating_key, obs.user):
                    logger.info('Session=%r identity changed; discarding previous state', key)
                    del self.sessions[key]
                    tracked = None
                active = obs.playback in ('playing', 'buffering')
                theater_changed = obs.theater_revision is not None and (tracked is None or tracked.observation.theater_revision != obs.theater_revision)
                if tracked is None:
                    if not active and key not in resumed and not theater_changed:
                        logger.debug('Session=%r idle and not previously playing; ignored', key)
                        continue
                    if not active and obs.state_since is not None:
                        grace = self.cfg.stop_delay_seconds if obs.playback == 'stopped' else self.cfg.pause_buffer_delay_seconds
                        if now - obs.state_since >= grace:
                            continue
                    tracked = self.sessions[key] = Session(obs)
                tracked.observation = obs
                if theater_changed:
                    tracked.since = None
                if key in resumed:
                    tracked.since = None
                    tracked.playback = 'resumed'
                    tracked.last_playing = datetime.now(timezone.utc).isoformat()
                previous = tracked.playback
                if obs.bandwidth_kbps is not None and (active or tracked.bandwidth_kbps is None):
                    if tracked.bandwidth_kbps != obs.bandwidth_kbps:
                        logger.info('Session=%r bandwidth_kbps %s -> %s (Plex reservation)',
                                    key, tracked.bandwidth_kbps, obs.bandwidth_kbps)
                    tracked.bandwidth_kbps = obs.bandwidth_kbps
                    tracked.bandwidth_source = 'plex_session'
                elif tracked.bandwidth_kbps is not None:
                    tracked.bandwidth_source = 'last_known'
                else:
                    if previous == 'new' or key in resumed:
                        logger.warning('Session=%r bandwidth unavailable; dynamic upload uses minimum', key)
                    tracked.bandwidth_source = 'unknown'
                if active:
                    tracked.since = None
                    if obs.playback == 'playing':
                        tracked.last_playing = datetime.now(timezone.utc).isoformat()
                elif previous != obs.playback or tracked.since is None:
                    tracked.since = obs.state_since if obs.state_since is not None else now
                    tracked.delay = self.cfg.stop_delay_seconds if obs.playback == 'stopped' else self.cfg.pause_buffer_delay_seconds
                tracked.playback = obs.playback
                if previous != tracked.playback:
                    logger.info('Session=%r state=%s -> %s grace_seconds=%s', key, previous,
                                tracked.playback, tracked.remaining(now))
            for key, tracked in self.sessions.items():
                if key not in observations and tracked.playback != 'missing':
                    tracked.playback, tracked.since = 'missing', now
                    if tracked.bandwidth_kbps is not None:
                        tracked.bandwidth_source = 'last_known'
                    tracked.delay = self.cfg.stop_delay_seconds
                    logger.info('Session=%r missing; stop grace=%ss', key, tracked.delay)
        return True

    def refresh(self):
        with self.refresh_lock:
            started = self.clock()
            self.cycle += 1
            fresh = self.sync()
            self.tick(fresh)
            logger.debug('cycle=%s completed duration_ms=%.1f fresh=%s',
                         self.cycle, (self.clock() - started) * 1000, fresh)

    def tick(self, fresh=True):
        with self.lock:
            now = self.clock()
            age = now - (self.last_plex_success if self.last_plex_success is not None else self.started)
            if not fresh and age < self.cfg.plex_stale_seconds:
                # Never infer an empty server from a transient failed request.
                want, limit = self.desired_mode, self.desired_upload_bps
                logger.debug('cycle=%s holding decision mode=%s upload_bps=%s Plex_age_seconds=%.1f',
                             self.cycle, want, limit, age)
                # Freeze actual qBittorrent settings, including pending writes/drift repair.
                return
            else:
                if not fresh:
                    if not self.stale:
                        logger.error('Plex data stale age=%.1fs timeout=%ss; disabling alternative speeds; upload preference unchanged', age, self.cfg.plex_stale_seconds)
                    self.stale = True
                    self.sessions.clear()
                else:
                    for key in [k for k, s in self.sessions.items() if s.remaining(now) == 0]:
                        logger.info('Session=%r grace expired state=%s; released reservation', key, self.sessions[key].playback)
                        del self.sessions[key]
                total = sum(s.bandwidth_kbps or 0 for s in self.sessions.values())
                unknown = any(s.bandwidth_kbps is None for s in self.sessions.values()) or bool(self.theater and self.theater.uncertain)
                want = not self.stale and (bool(self.sessions) or unknown)
                calculated = upload_budget(self.cfg, total, unknown) if want and self.cfg.dynamic_upload_enabled else None
                limit = qbt_budget(self.cfg, calculated) if calculated is not None else None
                self.calculated_upload_bps = calculated
                decision = (want, limit, total, unknown, len(self.sessions))
                log = logger.info if decision != self.last_decision else logger.debug
                log('cycle=%s budget sessions=%s reserved_mbps=%.3f multiplier=%s unknown=%s '
                    'min_mib=%s max_mib=%s calculated_bps=%s target_bps=%s target_mib=%s alt=%s',
                    self.cycle, len(self.sessions), total / 1000, self.cfg.bandwidth_multiplier,
                    unknown, self.cfg.min_upload_mib, self.cfg.max_upload_mib, calculated, limit,
                    round(limit / MIB, 6) if limit is not None else None, want)
                self.last_decision = decision
                self.desired_mode, self.desired_upload_bps = want, limit
            for key, tracked in self.sessions.items():
                logger.debug('cycle=%s session=%r snapshot=%s', self.cycle, key, tracked.snapshot(now))
        if want is not None:
            self._reconcile(want, limit, force=self.stale)

    def _reconcile(self, want, limit, force=False):
        # Serialised by refresh_lock; network I/O never holds the status lock.
        now = self.clock()
        due = now - self.last_drift_check >= self.cfg.drift_check_seconds
        if (not self.retry_qbt and not due and want == self.alt_speeds
                and (limit is None or limit == self.applied_upload_bps)):
            logger.debug('cycle=%s qBittorrent unchanged; drift check in %.1fs',
                         self.cycle, self.cfg.drift_check_seconds - (now - self.last_drift_check))
            return
        action = 'connect'
        try:
            if self.qbt is None:
                self.qbt = self._open_qbt()
            action = 'read'
            actual = bool(int(self.qbt.transfer.speed_limits_mode))
            actual_limit = int(self.qbt.app.preferences['alt_up_limit']) if limit is not None else None
            logger.debug('cycle=%s qBittorrent read mode=%s alt_upload_bps=%s desired_mode=%s desired_bps=%s',
                         self.cycle, actual, actual_limit, want, limit)
            if self.alt_speeds is not None and actual != self.alt_speeds:
                logger.warning('qBittorrent mode changed externally expected=%s actual=%s', self.alt_speeds, actual)
            if actual_limit is not None and self.applied_upload_bps is not None and actual_limit != self.applied_upload_bps:
                logger.warning('qBittorrent upload changed externally expected_bps=%s actual_bps=%s', self.applied_upload_bps, actual_limit)
            with self.lock:
                self.alt_speeds = actual
                if actual_limit is not None:
                    self.applied_upload_bps = actual_limit
            change = actual != want or (limit is not None and actual_limit != limit)
            protective = want and (not actual or (limit is not None and (actual_limit <= 0 or limit < actual_limit)))
            if change and not protective and not force and now - self.last_change < self.cfg.debounce_seconds:
                logger.debug('qBittorrent relaxation debounced remaining=%.2fs', self.cfg.debounce_seconds - (now - self.last_change))
                self.retry_qbt = True
                return
            if limit is not None and actual_limit != limit:
                action = 'set alternative upload'
                logger.info('qBittorrent setting alt_up_limit=%s B/s (%.6f MiB/s)', limit, limit / MIB)
                self.qbt.app.set_preferences({'alt_up_limit': limit})
                verified = int(self.qbt.app.preferences['alt_up_limit'])
                if verified != limit:
                    raise RuntimeError(f'upload readback mismatch requested={limit} actual={verified}')
                with self.lock:
                    self.applied_upload_bps = verified
            if actual != want:
                action = 'set mode'
                self.qbt.transfer.set_speed_limits_mode(intended_state=want)
                verified = bool(int(self.qbt.transfer.speed_limits_mode))
                if verified != want:
                    raise RuntimeError(f'mode readback mismatch requested={want} actual={verified}')
                with self.lock:
                    self.alt_speeds = verified
            with self.lock:
                self.last_drift_check = self.last_qbt_success = self.clock()
                self.qbt_failures, self.qbt_ok, self.retry_qbt = 0, True, False
                if change:
                    self.last_change = self.clock()
            if change:
                logger.info('qBittorrent verified alt=%s upload_bps=%s', want, limit)
        except Exception as exc:
            with self.lock:
                self.qbt_failures += 1
                self.qbt_ok, self.retry_qbt = False, True
            logger.error('qBittorrent action=%s failure=%s error=%s: %s; retry next poll',
                         action, self.qbt_failures, type(exc).__name__, exc)
            if self.qbt_failures >= 3:
                self.qbt = None
                logger.warning('Discarded qBittorrent client; reconnect next poll')

    def status(self):
        with self.lock:
            now = self.clock()
            return {
                'tracked_sessions': len(self.sessions),
                'sessions': {key: value.snapshot(now) for key, value in self.sessions.items()},
                'alt_speeds_enabled': self.alt_speeds,
                'desired_alt_speeds_enabled': self.desired_mode,
                'dynamic_upload_enabled': self.cfg.dynamic_upload_enabled,
                'reserved_bandwidth_mbps': float(sum(s.bandwidth_kbps or 0 for s in self.sessions.values()) / 1000),
                'theater': self.theater.status() if self.theater else {'enabled': False},
                'unknown_bandwidth_sessions': sum(s.bandwidth_kbps is None for s in self.sessions.values()),
                'min_upload_mib': float(self.cfg.min_upload_mib), 'max_upload_mib': float(self.cfg.max_upload_mib),
                'bandwidth_multiplier': float(self.cfg.bandwidth_multiplier),
                'desired_upload_bps': self.desired_upload_bps, 'applied_alt_upload_bps': self.applied_upload_bps,
                'calculated_upload_bps': self.calculated_upload_bps,
                'desired_upload_mib': self.desired_upload_bps / MIB if self.desired_upload_bps is not None else None,
                'plex_connected': self.plex is not None and self.last_plex_ok,
                'qbt_connected': self.qbt is not None and self.qbt_ok,
                'plex_stale': self.stale, 'plex_failures': self.plex_failures, 'qbt_failures': self.qbt_failures,
                'plex_last_success_age_seconds': now - self.last_plex_success if self.last_plex_success is not None else None,
                'qbt_last_success_age_seconds': now - self.last_qbt_success if self.last_qbt_success is not None else None,
                'stop_delay_seconds': self.cfg.stop_delay_seconds,
                'pause_buffer_delay_seconds': self.cfg.pause_buffer_delay_seconds,
                'pending_resume_hints': len(self.hints), 'refresh_count': self.cycle,
                'uptime_seconds': round(now - self.started, 1),
            }

"""Authenticated Theater snapshots and exact Plex-session ownership overlays."""
import json
import logging
import math
from dataclasses import replace
from decimal import Decimal
from urllib.request import Request

logger = logging.getLogger('plex-qbt')


def viewer_weight(count, factor, delivery_mode='direct_p2p'):
    if count == 0:
        return Decimal(0)
    if delivery_mode == 'vps_relay':
        return Decimal(1)
    return Decimal(count) * (factor if count >= 2 else Decimal(1))


def validate_snapshot(value):
    if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] != 1:
        raise ValueError('Unsupported Theater snapshot schema')
    for key in ('instance_id', 'plex_server_id'):
        if not isinstance(value.get(key), str) or not 1 <= len(value[key]) <= 256:
            raise ValueError(f'Invalid Theater {key}')
    if type(value.get('sequence')) is not int or value['sequence'] < 0:
        raise ValueError('Invalid Theater sequence')
    if value.get('delivery_mode') not in ('direct_p2p', 'vps_relay'):
        raise ValueError('Invalid Theater delivery mode')
    streams = value.get('streams')
    if not isinstance(streams, list) or len(streams) > 4096:
        raise ValueError('Invalid Theater streams')
    identities = set()
    for stream in streams:
        if not isinstance(stream, dict):
            raise ValueError('Invalid Theater stream')
        for key in ('room_id', 'variant_id', 'hls_session_id', 'rating_key'):
            if not isinstance(stream.get(key), str) or not 1 <= len(stream[key]) <= 256:
                raise ValueError(f'Invalid Theater stream {key}')
        transcode = stream.get('plex_transcode_key')
        if transcode is not None and (not isinstance(transcode, str) or not 1 <= len(transcode) <= 512):
            raise ValueError('Invalid Theater transcode key')
        if stream['hls_session_id'] in identities:
            raise ValueError('Duplicate Theater session identity')
        identities.add(stream['hls_session_id'])
        if stream.get('state') not in ('playing', 'paused', 'stopped'):
            raise ValueError('Invalid Theater playback state')
        for key in ('viewer_count', 'state_revision'):
            if type(stream.get(key)) is not int or not 0 <= stream[key] <= 2**53 - 1:
                raise ValueError(f'Invalid Theater {key}')
        for key in ('state_age_seconds', 'host_heartbeat_age_seconds'):
            age = stream.get(key)
            if age is None and key == 'host_heartbeat_age_seconds':
                continue
            if type(age) not in (int, float) or not math.isfinite(age) or age < 0:
                raise ValueError(f'Invalid Theater {key}')
    return value


class Theater:
    def __init__(self, cfg, clock):
        self.cfg, self.clock = cfg, clock
        self.snapshot = None
        self.received = None
        self.attempted = float('-inf')
        self.error = None
        self.owned = {}
        self.uncertain = False

    def poll(self):
        now = self.clock()
        if now - self.attempted < self.cfg.theater_poll_interval_seconds:
            return
        self.attempted = now
        try:
            request = Request(self.cfg.theater_url.rstrip('/') + '/api/integrations/qbt-manager/state',
                              headers={'Authorization': 'Bearer ' + self.cfg.theater_api_key,
                                       'Accept': 'application/json'})
            # Disable redirects: never forward the integration credential elsewhere.
            from urllib.request import HTTPRedirectHandler, build_opener
            class NoRedirect(HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None
            with build_opener(NoRedirect).open(request, timeout=self.cfg.theater_timeout_seconds) as response:
                body = response.read(2 * 1024 * 1024 + 1)
            if len(body) > 2 * 1024 * 1024:
                raise ValueError('Theater snapshot exceeds 2 MiB')
            value = validate_snapshot(json.loads(body))
            if self.snapshot and value['instance_id'] == self.snapshot['instance_id']:
                if value['sequence'] <= self.snapshot['sequence']:
                    raise ValueError('Theater sequence did not advance')
            self.snapshot, self.received, self.error = value, self.clock(), None
            logger.debug('Theater snapshot instance=%s sequence=%s variants=%s mode=%s',
                         value['instance_id'], value['sequence'], len(value['streams']), value['delivery_mode'])
        except Exception as exc:
            self.error = type(exc).__name__
            # Do not print response bodies, URLs or headers containing credentials.
            logger.warning('Theater snapshot failed error=%s last_success_age_seconds=%s; retaining last snapshot',
                           self.error, round(self.clock() - self.received, 1) if self.received is not None else None)

    def overlay(self, observations, server_id):
        now = self.clock()
        valid = self.snapshot and self.snapshot['plex_server_id'] == server_id
        fresh = valid and self.received is not None and now - self.received < self.cfg.theater_stale_seconds
        self.uncertain = not fresh
        logger.debug('Theater freshness valid_server=%s age_seconds=%s stale_timeout=%s fresh=%s',
                     bool(valid), now - self.received if self.received is not None else None,
                     self.cfg.theater_stale_seconds, bool(fresh))
        result = {}
        matched = set()
        streams = self.snapshot['streams'] if valid else []
        present = set()
        for stream in streams:
            sid = stream['hls_session_id']
            present.add(sid)
            exact = [o for o in observations.values() if o.session_id == sid]
            if not exact and stream['plex_transcode_key']:
                key = stream['plex_transcode_key'].rstrip('/').split('/')[-1]
                exact = [o for o in observations.values() if o.transcode_key and o.transcode_key == key]
            if len(exact) > 1 or (exact and exact[0].key in matched):
                self.uncertain = True
                logger.warning('Theater ambiguous identity room=%s variant=%s; using minimum budget', stream['room_id'], stream['variant_id'])
                continue
            old = self.owned.get(sid)
            base = exact[0] if exact else (old['base'] if old else None)
            if base and base.bandwidth_kbps is None and old:
                base = replace(base, bandwidth_kbps=old['base'].bandwidth_kbps)
            if exact:
                matched.add(exact[0].key)
            if base is None:
                if stream['viewer_count'] and stream['state'] == 'playing':
                    self.uncertain = True
                continue
            if base.rating_key != stream['rating_key']:
                self.uncertain = True
                continue
            state = stream['state'] if stream['viewer_count'] else 'stopped'
            revision = (self.snapshot['instance_id'], stream['state_revision'])
            weight = viewer_weight(stream['viewer_count'], self.cfg.theater_bandwidth_factor, self.snapshot['delivery_mode'])
            bandwidth = Decimal(base.bandwidth_kbps) * weight if base.bandwidth_kbps is not None else None
            if state != 'playing' and old:
                bandwidth = old['observation'].bandwidth_kbps
            # Zero viewers have their own transition clock, independent of room pause.
            since = now - stream['state_age_seconds'] - (now - self.received)
            if not stream['viewer_count']:
                since = (old['zero_since'] if old['zero_since'] is not None else now) if old else now - self.cfg.stop_delay_seconds
            obs = replace(base, key='theater:' + sid, playback=state, bandwidth_kbps=bandwidth,
                          theater_revision=revision, state_since=since,
                          theater_details={'room_id': stream['room_id'], 'variant_id': stream['variant_id'],
                                           'viewer_count': stream['viewer_count'], 'weight': float(weight),
                                           'plex_user': base.user, 'plex_user_id': base.user_id,
                                           'plex_bandwidth_kbps': base.bandwidth_kbps,
                                           'delivery_mode': self.snapshot['delivery_mode']})
            self.owned[sid] = {'base': base, 'observation': obs, 'zero_since': since if not stream['viewer_count'] else None}
            result[obs.key] = obs
            logger.debug('Theater session=%s user=%r user_id=%r variant=%s viewers=%s factor=%s weight=%s '
                         'base_kbps=%s effective_kbps=%s state=%s revision=%s matched=%s',
                         sid, base.user, base.user_id, stream['variant_id'], stream['viewer_count'],
                         self.cfg.theater_bandwidth_factor, weight, base.bandwidth_kbps, bandwidth, state, revision, bool(exact))
        # Retain stopped ownership while Plex keeps its transcode alive: never count it
        # again as an ordinary stream simply because Theater removed the room.
        for sid, old in list(self.owned.items()):
            if sid in present:
                continue
            base = old['base']
            exact = [o for o in observations.values() if (o.session_id and o.session_id == sid)
                     or (o.transcode_key and o.transcode_key == base.transcode_key)]
            if not exact:
                del self.owned[sid]
                continue
            matched.update(o.key for o in exact)
            obs = old['observation']
            if fresh:
                if obs.playback != 'stopped':
                    obs = replace(obs, playback='stopped', state_since=now)
                    old['observation'] = obs
            result[obs.key] = obs
        for key, obs in observations.items():
            if key not in matched and obs.remote:
                result[key] = obs
        return result

    def status(self):
        return {'enabled': True, 'last_success_age_seconds': self.clock() - self.received if self.received is not None else None,
                'error': self.error, 'uncertain': self.uncertain, 'owned_sessions': len(self.owned),
                'bandwidth_factor': float(self.cfg.theater_bandwidth_factor)}

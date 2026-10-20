"""Authenticated Theater snapshots and exact Plex-session ownership overlays.

Streams Theater's ffmpeg engine makes are nothing Plex serves, so for those
Theater's snapshot gives the bandwidth itself (bandwidth_kbps, decimal kbps).
"""
import json
import logging
import math
from dataclasses import replace
from decimal import Decimal
from urllib.request import Request

logger = logging.getLogger('plex-qbt')


def newest_observation(observations):
    numeric = [(int(obs.key), obs) for obs in observations if str(obs.key).isdigit()]
    return max(numeric)[1] if len(numeric) == len(observations) else observations[-1]


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
        # Only an ffmpeg stream carries these; Plex-engine streams and older Theaters omit them.
        if 'engine' in stream and stream['engine'] not in ('ffmpeg', 'plex'):
            raise ValueError('Invalid Theater stream engine')
        if 'bandwidth_kbps' in stream and (type(stream['bandwidth_kbps']) is not int or
                                           not 1 <= stream['bandwidth_kbps'] <= 10_000_000):
            raise ValueError('Invalid Theater bandwidth_kbps')
        if 'bandwidth_source' in stream and stream['bandwidth_source'] not in ('measured', 'target'):
            raise ValueError('Invalid Theater bandwidth_source')
        client = stream.get('plex_client_id')
        if client is not None and (not isinstance(client, str) or not 1 <= len(client) <= 256):
            raise ValueError('Invalid Theater plex_client_id')
    return value


def overlay_all(theaters, observations, server_id):
    """Merge every instance's owned streams; a Plex row any instance claims is never counted raw."""
    result, matched = {}, set()
    for theater in theaters:
        owned, claimed = theater.claim(observations, server_id)
        if claimed & matched or owned.keys() & result.keys():
            # Distinct Theaters never share a Plex session; this is one Theater behind two URLs.
            theater.uncertain = True
            logger.warning('Theater theater=%s claims Plex sessions owned by another instance; '
                           'not counting it twice', theater.name)
        result.update(owned)
        matched |= claimed
    for key, obs in observations.items():
        if key not in matched and obs.remote:
            result[key] = obs
    return result


class Theater:
    def __init__(self, cfg, clock, name='1', url=None, api_key=None):
        self.cfg, self.clock, self.name = cfg, clock, name
        self.url = cfg.theater_url if url is None else url
        self.api_key = cfg.theater_api_key if api_key is None else api_key
        self.snapshot = None
        self.received = None
        self.attempted = float('-inf')
        self.error = None
        self.owned = {}
        # Reporter players (plex_client_id) of ffmpeg streams retired by a title change or removal.
        self.retired_clients = set()
        self.uncertain = False
        self.server_mismatch = False
        self.ignored = False

    def poll(self):
        now = self.clock()
        if now - self.attempted < self.cfg.theater_poll_interval_seconds:
            return
        self.attempted = now
        try:
            request = Request(self.url.rstrip('/') + '/api/integrations/qbt-manager/state',
                              headers={'Authorization': 'Bearer ' + self.api_key,
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
            logger.debug('Theater snapshot theater=%s instance=%s sequence=%s variants=%s mode=%s',
                         self.name, value['instance_id'], value['sequence'], len(value['streams']), value['delivery_mode'])
        except Exception as exc:
            self.error = type(exc).__name__
            # Do not print response bodies, URLs or headers containing credentials.
            log = logger.debug if self.ignored else logger.warning
            log('Theater snapshot failed theater=%s error=%s last_success_age_seconds=%s; retaining last snapshot',
                self.name, self.error, round(self.clock() - self.received, 1) if self.received is not None else None)

    def claim(self, observations, server_id):
        """Return this instance's owned observations and the raw Plex keys they replace."""
        now = self.clock()
        age = now - self.received if self.received is not None else None
        logger.debug('Theater freshness theater=%s age_seconds=%s stale_timeout=%s',
                     self.name, age, self.cfg.theater_stale_seconds)
        if age is None or age >= self.cfg.theater_stale_seconds:
            # Until the timeout the last snapshot keeps counting. After it, nothing is likely
            # playing on a Theater nobody can reach, so act as if it were not configured.
            if not self.ignored:
                logger.warning('Theater theater=%s unanswered last_success_age_seconds=%s stale_timeout=%ss; '
                               'ignoring it until it answers', self.name,
                               round(age, 1) if age is not None else None, self.cfg.theater_stale_seconds)
            self.ignored, self.uncertain = True, False
            self.owned.clear()
            self.retired_clients.clear()
            return {}, set()
        if self.ignored:
            logger.info('Theater theater=%s answering again; counting its streams', self.name)
            self.ignored = False
        valid = self.snapshot['plex_server_id'] == server_id
        if not valid and not self.server_mismatch:
            logger.warning('Theater theater=%s reports Plex server=%r but PLEX_URL is server=%r; '
                           'not counting its streams until they match', self.name, self.snapshot['plex_server_id'], server_id)
        self.server_mismatch = self.uncertain = not valid
        result = {}
        matched = set()
        handed_off = {}
        streams = self.snapshot['streams'] if valid else []
        # A Theater room can have several audio/subtitle variants for one title.
        # When its rating key changes, the old Plex transcodes can linger briefly;
        # retire their ownership now instead of returning them for stop grace.
        active_titles = {}
        active_sessions = {}
        clients = {stream.get('plex_client_id') for stream in streams} - {None}
        for stream in streams:
            if stream['viewer_count'] and stream['state'] == 'playing':
                active_titles.setdefault(stream['room_id'], set()).add(stream['rating_key'])
                active_sessions.setdefault(stream['room_id'], set()).add(stream['hls_session_id'])
        for sid, old in list(self.owned.items()):
            details = old['observation'].theater_details or {}
            ratings = active_titles.get(details.get('room_id'), set())
            current_sessions = active_sessions.get(details.get('room_id'), set())
            if len(ratings) != 1 or sid in current_sessions:
                continue
            base = old['base']
            matched.update(o.key for o in observations.values() if
                           (o.session_id and o.session_id == sid) or
                           (base.transcode_key and o.transcode_key == base.transcode_key))
            del self.owned[sid]
            handed_off.setdefault(details.get('room_id'), base)
            if old['plex_client_id'] and old['plex_client_id'] not in clients:
                self.retired_clients.add(old['plex_client_id'])
            logger.info('Theater ownership handoff theater=%s room=%r old_session=%s old_rating=%r new_rating=%r; '
                        'lingering Plex session suppressed', self.name, details.get('room_id'), sid,
                        base.rating_key, next(iter(ratings)))
        # The reporter tells Plex an ffmpeg stream stopped only some seconds after it has gone,
        # so the previous title's row lingers on its own player, which no device handoff
        # shares. Until Plex drops it the row is still Theater's, though nothing to reserve.
        self.retired_clients &= {o.player_id for o in observations.values()}
        matched.update(o.key for o in observations.values() if o.player_id in self.retired_clients)
        present = set()
        for stream in streams:
            sid = stream['hls_session_id']
            present.add(sid)
            equivalent_duplicates = False
            # Theater's reporter (ffmpeg/plex-report.ts) shows an ffmpeg stream in Plex's Now
            # Playing on a player of its own. Phase 0 (17 October) saw that row with no Session
            # id to match it by; it is this stream whatever else happens, never an ordinary one.
            client = stream.get('plex_client_id')
            reported = [o for o in observations.values() if client and o.player_id == client]
            matched.update(o.key for o in reported)
            # Plex never sees what ffmpeg makes, so Theater says what one viewer of it downloads.
            measured = stream.get('bandwidth_kbps') if stream.get('engine') == 'ffmpeg' else None
            row = newest_observation(reported) if reported else None
            exact = []
            # Plex makes nothing of an ffmpeg stream, so its reporter's row, matched by player
            # above, is all there is of it. Matched again by a Session id Plex might one day
            # list for it, that row would be found already claimed and the stream ambiguous —
            # also while Theater sends no figure, in the moment between a seek's sessions.
            if measured is None:
                mine = {o.key for o in reported}
                exact = [o for o in observations.values() if o.session_id == sid and o.key not in mine]
                if not exact and stream['plex_transcode_key']:
                    key = stream['plex_transcode_key'].rstrip('/').split('/')[-1]
                    exact = [o for o in observations.values()
                             if o.transcode_key and o.transcode_key == key and o.key not in mine]
            if len(exact) > 1:
                signatures = {(o.player_id, o.rating_key, o.user_id or o.user)
                              for o in exact}
                matched.update(o.key for o in exact)
                if len(signatures) == 1 and exact[0].rating_key == stream['rating_key']:
                    selected = newest_observation(exact)
                    known_bandwidth = [o.bandwidth_kbps for o in exact if o.bandwidth_kbps is not None]
                    if known_bandwidth:
                        selected = replace(selected, bandwidth_kbps=max(known_bandwidth))
                    logger.info('Theater duplicate Plex identity room=%s variant=%s sessions=%r selected=%r; '
                                'equivalent rows collapsed using bandwidth_kbps=%r',
                                stream['room_id'], stream['variant_id'], [o.key for o in exact],
                                selected.key, selected.bandwidth_kbps)
                    exact = [selected]
                    equivalent_duplicates = True
                else:
                    self.uncertain = True
                    logger.warning('Theater ambiguous identity theater=%s room=%s variant=%s sessions=%r; '
                                   'not counted', self.name, stream['room_id'], stream['variant_id'],
                                   [o.key for o in exact])
                    continue
            if exact and exact[0].key in matched and not equivalent_duplicates:
                self.uncertain = True
                logger.warning('Theater ambiguous identity theater=%s room=%s variant=%s; not counted',
                               self.name, stream['room_id'], stream['variant_id'])
                continue
            old = self.owned.get(sid)
            base = exact[0] if exact else (old['base'] if old else None)
            if measured is not None:
                # The reporter's row, when Plex lists it, says whose account the stream plays
                # under; until then (or with reporting off) the stream stands for itself.
                if row is None and base is None:
                    from controller import Observation  # controller imports this module
                    base = Observation(key='theater:' + sid, player_id=client or '', rating_key=stream['rating_key'],
                                       user='', playback=stream['state'], bandwidth_kbps=None, session_id=sid)
                base = replace(row or base, rating_key=stream['rating_key'], bandwidth_kbps=measured,
                               bandwidth_source='theater_snapshot')
            if base is None and stream['room_id'] in handed_off:
                # Plex lists a replacement transcode a poll or two after Theater announces it.
                # Bridge that gap with the stream it replaced rather than counting nothing.
                # An ffmpeg stream played on its reporter's player, which no transcode of
                # Plex's is listed on, so the bridge keeps its figure but not its player.
                replaced = handed_off[stream['room_id']]
                base = replace(replaced, session_id=sid, transcode_key='', rating_key=stream['rating_key'],
                               player_id=replaced.player_id if replaced.bandwidth_source == 'plex_session' else '')
                logger.info('Theater theater=%s session=%s not yet listed by Plex; using replaced stream '
                            'bandwidth_kbps=%s', self.name, sid, base.bandwidth_kbps)
            if base and base.bandwidth_kbps is None and old:
                # A figure carried over keeps saying where it came from.
                base = replace(base, bandwidth_kbps=old['base'].bandwidth_kbps,
                               bandwidth_source=old['base'].bandwidth_source)
            # Theater's own figure for a stream of Plex's it measures, a Direct Stream copy: Plex's
            # for a copy is how fast it hands the segments to Theater over the LAN, not what anyone
            # downloads. On 18 October it was 121 Mbps for a film of 1 to 5, and the budget hit
            # MIN_UPLOAD_MIB. The row is still matched above, and claimed, as any of Plex's.
            from_plex = base.bandwidth_kbps if base and base.bandwidth_source == 'plex_session' else None
            given = stream.get('bandwidth_kbps')
            if measured is None and given is not None:
                if base is None:
                    from controller import Observation  # controller imports this module
                    base = Observation(key='theater:' + sid, player_id='', rating_key=stream['rating_key'],
                                       user='', playback=stream['state'], bandwidth_kbps=None, session_id=sid)
                base = replace(base, bandwidth_kbps=given, bandwidth_source='theater_snapshot')
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
            # Only what Plex itself lists for the stream, never a figure Theater gave.
            if measured is not None:
                plex_kbps = row.bandwidth_kbps if row else None
            else:
                plex_kbps = from_plex
            obs = replace(base, key='theater:' + sid, playback=state, bandwidth_kbps=bandwidth,
                          theater_revision=revision, state_since=since,
                          theater_details={'instance': self.name, 'room_id': stream['room_id'], 'variant_id': stream['variant_id'],
                                           'viewer_count': stream['viewer_count'], 'weight': float(weight),
                                           'plex_user': base.user, 'plex_user_id': base.user_id,
                                           'plex_bandwidth_kbps': plex_kbps,
                                           'engine': stream.get('engine'), 'bandwidth_kbps': stream.get('bandwidth_kbps'),
                                           'bandwidth_source': stream.get('bandwidth_source'), 'plex_client_id': client,
                                           'delivery_mode': self.snapshot['delivery_mode']})
            self.owned[sid] = {'base': base, 'observation': obs, 'zero_since': since if not stream['viewer_count'] else None,
                               'plex_client_id': client}
            result[obs.key] = obs
            logger.debug('Theater theater=%s session=%s user=%r user_id=%r variant=%s viewers=%s factor=%s weight=%s '
                         'base_kbps=%s effective_kbps=%s state=%s revision=%s matched=%s engine=%s bandwidth_source=%s '
                         'plex_client_id=%r', self.name, sid, base.user, base.user_id, stream['variant_id'],
                         stream['viewer_count'], self.cfg.theater_bandwidth_factor, weight, base.bandwidth_kbps,
                         bandwidth, state, revision, bool(exact or reported), stream.get('engine'),
                         base.bandwidth_source if measured is None else stream.get('bandwidth_source'), client)
        # Retain stopped ownership while Plex keeps its transcode alive: never count it
        # again as an ordinary stream simply because Theater removed the room.
        for sid, old in list(self.owned.items()):
            if sid in present:
                continue
            base = old['base']
            # The reporter's row is no transcode Plex keeps alive: it lingers only until the
            # reporter says the stream stopped, some seconds late. Retire it as a title change
            # does, so it stays Theater's while the stop grace starts now, as with reporting
            # off. A seek's new session keeps the reporter's player, so its row is that one's.
            client = old['plex_client_id']
            if client and client not in clients:
                self.retired_clients.add(client)
                matched.update(o.key for o in observations.values() if o.player_id == client)
            # Nor is any row on a reporter's player, whatever Session id Plex lists for it.
            reporters = clients | self.retired_clients
            exact = [o for o in observations.values() if o.player_id not in reporters and (
                (o.session_id and o.session_id == sid) or (o.transcode_key and o.transcode_key == base.transcode_key))]
            if not exact:
                del self.owned[sid]
                continue
            matched.update(o.key for o in exact)
            obs = old['observation']
            if valid:
                if obs.playback != 'stopped':
                    obs = replace(obs, playback='stopped', state_since=now)
                    old['observation'] = obs
            result[obs.key] = obs
        return result, matched

    def status(self):
        return {'name': self.name, 'last_success_age_seconds': self.clock() - self.received if self.received is not None else None,
                'error': self.error, 'ignored': self.ignored, 'uncertain': self.uncertain,
                'plex_server_mismatch': self.server_mismatch,
                'owned_sessions': len(self.owned)}

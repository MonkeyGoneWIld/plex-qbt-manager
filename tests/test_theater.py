import copy
import logging
from decimal import Decimal

import pytest

import app
from app import Config, RedactingFormatter, numbered_theaters
from controller import StateManager, qbt_budget, upload_budget
from theater import Theater, validate_snapshot, viewer_weight
from test_controller import rig, xml_session


def stream(sid='hls-1', viewers=1, state='playing', revision=1, age=0, rating='42', room='room'):
    return dict(room_id=room, variant_id=sid, hls_session_id=sid,
                plex_transcode_key=None, rating_key=rating, state=state,
                state_revision=revision, state_age_seconds=age,
                viewer_count=viewers, host_heartbeat_age_seconds=0)


def snapshot(*streams, server='server-1', instance='boot-1', sequence=1):
    return dict(schema_version=1, instance_id=instance, sequence=sequence,
                plex_server_id=server, delivery_mode='direct_p2p', streams=list(streams))


def plex(sid='hls-1', key='1', bandwidth='12000', local='1', user='Bot', rating='42', player='player-1'):
    item = xml_session(key=key, bandwidth=bandwidth, local=local, user=user, rating=rating, player=player)
    item.find('Session').set('id', sid)
    item.find('User').set('id', '7')
    return item


def ffmpeg(sid='hls-1', viewers=1, kbps=8000, source='measured', client=None, **kwargs):
    s = stream(sid, viewers, **kwargs)
    s.update(engine='ffmpeg', plex_client_id=client)
    if kbps is not None:
        s.update(bandwidth_kbps=kbps, bandwidth_source=source)
    return s


def reporter(client='plex-discord-theater-hls-1', key='1', local='1', bandwidth=None, rating='42'):
    # Theater's Now Playing row for an ffmpeg stream: its own player, no Session id or bandwidth.
    return plex('', key, bandwidth=bandwidth, local=local, rating=rating, player=client)


def enable(rig, monkeypatch, *streams):
    rig.m.cfg.theater_bandwidth_factor = Decimal('0.8')
    rig.m.theaters = [Theater(rig.m.cfg, rig.clock)]
    t = rig.m.theaters[0]
    t.snapshot = snapshot(*streams)
    t.received = rig.clock()
    monkeypatch.setattr(t, 'poll', lambda: None)
    return t


def add_theater(rig, monkeypatch, name, *streams, **kwargs):
    t = Theater(rig.m.cfg, rig.clock, name=name)
    t.snapshot = snapshot(*streams, **kwargs)
    t.received = rig.clock()
    monkeypatch.setattr(t, 'poll', lambda: None)
    rig.m.theaters.append(t)
    return t


@pytest.mark.parametrize('count,expected', [(0, '0'), (1, '1'), (2, '1.6'), (3, '2.4')])
def test_factor_only_for_shared_variant(count, expected):
    assert viewer_weight(count, Decimal('0.8')) == Decimal(expected)


def test_relay_counts_one_feed_without_p2p_discount():
    assert viewer_weight(7, Decimal('.8'), 'vps_relay') == 1


def test_three_track_variants_and_ordinary_same_account(rig, monkeypatch):
    enable(rig, monkeypatch, stream('a', 3), stream('b', 2), stream('c', 1))
    rig.m.cfg.bandwidth_multiplier = Decimal('1.5')
    rig.poll(0, plex('a', '1'), plex('b', '2'), plex('c', '3'),
             plex('ordinary', '4', bandwidth='5000', local='0', player='other-player'))
    # Per-variant P2P estimate: 12*(3*.8 + 2*.8 + 1) + ordinary 5 = 65 Mbps.
    assert rig.m.status()['reserved_bandwidth_mbps'] == 65
    assert len(rig.m.sessions) == 4
    assert rig.q.prefs['alt_up_limit'] == qbt_budget(rig.m.cfg, upload_budget(rig.m.cfg, 65000))
    assert rig.m.status()['sessions']['theater:a']['theater']['plex_user'] == 'Bot'
    assert rig.m.status()['sessions']['theater:a']['theater']['plex_user_id'] == '7'


def test_pause_suppresses_plex_playing_after_grace_and_second_pause(rig, monkeypatch):
    t = enable(rig, monkeypatch, stream(viewers=3))
    rig.poll(0, plex())
    limit = rig.q.prefs['alt_up_limit']
    t.snapshot = snapshot(stream(viewers=3, state='paused', revision=2))
    t.received = 10
    rig.poll(10, plex())
    assert rig.q.prefs['alt_up_limit'] == limit
    for now in (69, 70, 90):
        t.snapshot = snapshot(stream(viewers=3, state='paused', revision=2, age=now-10))
        t.received = now
        rig.poll(now, plex())
        assert rig.q.mode == (now < 70)
    # Resume and second pause both happened between manager polls.
    t.snapshot = snapshot(stream(viewers=3, state='paused', revision=4, age=2))
    t.received = 100
    rig.poll(100, plex())
    assert rig.q.mode
    assert rig.m.status()['sessions']['theater:hls-1']['seconds_remaining'] == 58


def test_viewer_count_updates_and_zero_viewers_stop_grace(rig, monkeypatch):
    t = enable(rig, monkeypatch, stream(viewers=3))
    rig.poll(0, plex())
    assert rig.m.status()['reserved_bandwidth_mbps'] == 28.8
    t.snapshot = snapshot(stream(viewers=1))
    t.received = 5
    rig.poll(5, plex())
    assert rig.m.status()['reserved_bandwidth_mbps'] == 12
    t.snapshot = snapshot(stream(viewers=0))
    t.received = 10
    rig.poll(10, plex())
    assert rig.m.status()['reserved_bandwidth_mbps'] == 12
    t.received = 40
    rig.poll(40, plex())
    assert not rig.q.mode
    t.received = 45
    rig.poll(45, plex())
    assert not rig.q.mode


def test_removed_room_does_not_recount_lingering_remote_plex(rig, monkeypatch):
    t = enable(rig, monkeypatch, stream(viewers=2))
    rig.poll(0, plex(local='0'))
    t.snapshot = snapshot()
    t.received = 10
    rig.poll(10, plex(local='0'))
    t.received = 40
    rig.poll(40, plex(local='0'))
    assert not rig.q.mode and not rig.m.sessions


def test_identity_match_before_lan_filter_and_exact_transcode_fallback(rig, monkeypatch):
    from xml.etree.ElementTree import SubElement
    s = stream()
    s['plex_transcode_key'] = '/video/:/transcode/universal/session/abc'
    enable(rig, monkeypatch, s)
    item = plex('different-id')
    SubElement(item, 'TranscodeSession', key='abc')
    rig.poll(0, item)
    assert rig.m.status()['reserved_bandwidth_mbps'] == 12


def test_late_snapshot_transfers_existing_ordinary_reservation(rig, monkeypatch):
    rig.poll(0, plex(local='0'))
    enable(rig, monkeypatch, stream(viewers=2))
    rig.poll(5, plex(local='0'))
    assert len(rig.m.sessions) == 1
    assert rig.m.status()['reserved_bandwidth_mbps'] == 19.2


def test_episode_handoff_releases_old_room_variants_immediately(rig, monkeypatch):
    t = enable(rig, monkeypatch, stream('old-a', viewers=3), stream('old-b', viewers=1))
    rig.poll(0, plex('old-a', '10'), plex('old-b', '11'))
    assert rig.m.status()['reserved_bandwidth_mbps'] == 40.8

    # Plex exposes the replacement before Theater's next API poll. Keep the old
    # weighted room reservation alone instead of adding the raw replacement.
    rig.poll(4, plex('old-a', '10'), plex('old-b', '11'),
             plex('new-a', '12', rating='43'))
    assert set(rig.m.sessions) == {'theater:old-a', 'theater:old-b'}
    assert rig.m.status()['reserved_bandwidth_mbps'] == 40.8

    t.snapshot = snapshot(stream('new-a', viewers=4, revision=2, rating='43'), sequence=2)
    t.received = 5
    # Plex still exposes both old transcodes while the new episode starts.
    rig.poll(5, plex('old-a', '10'), plex('old-b', '11'),
             plex('new-a', '12', rating='43'))
    assert set(rig.m.sessions) == {'theater:new-a'}
    assert rig.m.status()['reserved_bandwidth_mbps'] == 38.4
    assert set(t.owned) == {'new-a'}


def test_equivalent_duplicate_plex_rows_collapse_to_one_theater_stream(rig, monkeypatch):
    enable(rig, monkeypatch, stream('shared', viewers=2))
    rig.poll(0, plex('shared', '10'), plex('shared', '11', bandwidth='13000'))
    assert set(rig.m.sessions) == {'theater:shared'}
    assert rig.m.status()['reserved_bandwidth_mbps'] == 20.8
    assert not rig.m.theaters[0].uncertain


def test_unmatched_same_device_row_is_suppressed_beside_theater(rig, monkeypatch):
    enable(rig, monkeypatch, stream('owned', viewers=2))
    rig.poll(0, plex('owned', '10'), plex('other', '11'))
    assert set(rig.m.sessions) == {'theater:owned'}
    assert rig.m.status()['reserved_bandwidth_mbps'] == 19.2


def test_same_title_theater_variants_are_not_collapsed(rig, monkeypatch):
    enable(rig, monkeypatch, stream('a', viewers=3), stream('b', viewers=2))
    rig.poll(0, plex('a', '1'), plex('b', '2'))
    assert set(rig.m.sessions) == {'theater:a', 'theater:b'}


def test_replaced_variant_in_same_room_is_released_immediately(rig, monkeypatch):
    t = enable(rig, monkeypatch, stream('old', viewers=2, rating='42'))
    rig.poll(0, plex('old', '10', rating='42'))
    t.snapshot = snapshot(stream('new', viewers=2, revision=2, rating='42'), sequence=2)
    t.received = 5
    rig.poll(5, plex('old', '10', rating='42'), plex('new', '11', rating='42'))
    assert set(rig.m.sessions) == {'theater:new'}
    assert set(t.owned) == {'new'}
    assert rig.m.status()['reserved_bandwidth_mbps'] == 19.2


def test_duplicate_rows_during_episode_handoff_never_force_minimum(rig, monkeypatch):
    t = enable(rig, monkeypatch, stream('old', viewers=2, rating='42'))
    rig.poll(0, plex('old', '10', rating='42'))

    # New Plex rows arrive before Theater changes its snapshot.
    rig.poll(1, plex('old', '10', rating='42'),
             plex('new', '11', rating='43'), plex('new', '12', rating='43'))
    assert set(rig.m.sessions) == {'theater:old'}
    assert not rig.m.theaters[0].uncertain

    # Theater catches up while both equivalent new rows still exist.
    t.snapshot = snapshot(stream('new', viewers=2, revision=2, rating='43'), sequence=2)
    t.received = 5
    rig.poll(5, plex('old', '10', rating='42'),
             plex('new', '11', rating='43'), plex('new', '12', rating='43'))
    assert set(rig.m.sessions) == {'theater:new'}
    assert rig.m.status()['reserved_bandwidth_mbps'] == 19.2
    assert not rig.m.theaters[0].uncertain
    assert rig.q.prefs['alt_up_limit'] != rig.m.cfg.min_upload_bps


@pytest.mark.parametrize('server,streams', [('wrong-server', [stream()]), ('server-1', [stream('unmatched')])])
def test_unmatched_or_foreign_snapshot_never_exempts_account(rig, monkeypatch, server, streams):
    t = enable(rig, monkeypatch)
    t.snapshot = snapshot(*streams, server=server)
    rig.poll(0, plex(local='0'))
    assert '1' in rig.m.sessions
    assert rig.m.theaters[0].uncertain
    # Uncertain is never the minimum: what Theater can't account for counts as nothing, and
    # Plex's own row counts as the ordinary remote stream it is.
    assert rig.q.prefs['alt_up_limit'] == qbt_budget(rig.m.cfg, upload_budget(rig.m.cfg, 12000))
    assert rig.m.status()['theater']['instances'][0]['plex_server_mismatch'] == (server == 'wrong-server')


def test_two_instances_each_count_their_own_viewers(rig, monkeypatch):
    enable(rig, monkeypatch, stream('a', 3))
    add_theater(rig, monkeypatch, '2', stream('b', 2, room='other'), instance='boot-2')
    rig.poll(0, plex('a', '1'), plex('b', '2', player='player-2'),
             plex('ordinary', '3', bandwidth='5000', local='0', player='other-player'))
    # 12*3*.8 from instance 1 + 12*2*.8 from instance 2 + ordinary 5 = 53 Mbps.
    status = rig.m.status()
    assert status['reserved_bandwidth_mbps'] == 53
    assert set(rig.m.sessions) == {'theater:a', 'theater:b', '3'}
    assert status['sessions']['theater:b']['theater']['instance'] == '2'
    assert [i['name'] for i in status['theater']['instances']] == ['1', '2']
    assert rig.q.prefs['alt_up_limit'] == qbt_budget(rig.m.cfg, upload_budget(rig.m.cfg, 53000))


def test_same_room_id_in_two_instances_is_not_one_room(rig, monkeypatch):
    t1 = enable(rig, monkeypatch, stream('a', 2))
    t2 = add_theater(rig, monkeypatch, '2', stream('b', 2), instance='boot-2')
    rig.poll(0, plex('a', '1'), plex('b', '2', player='player-2'))
    # Instance 2 pauses while instance 1 keeps playing under the same room id.
    t2.snapshot = snapshot(stream('b', 2, state='paused', revision=2), instance='boot-2', sequence=2)
    t1.received = t2.received = 5
    rig.poll(5, plex('a', '1'), plex('b', '2', player='player-2'))
    assert set(rig.m.sessions) == {'theater:a', 'theater:b'}
    assert rig.m.status()['sessions']['theater:b']['state'] == 'paused'
    assert rig.m.status()['reserved_bandwidth_mbps'] == 38.4


def test_unreachable_instance_is_ignored_while_other_keeps_counting(rig, monkeypatch):
    t1 = enable(rig, monkeypatch, stream('a', 2))
    add_theater(rig, monkeypatch, '2', stream('b', 3, room='other'), instance='boot-2')
    rows = (plex('a', '1'), plex('b', '2', player='player-2'))
    rig.poll(0, *rows)
    assert rig.m.status()['reserved_bandwidth_mbps'] == 48
    t1.received = 29
    rig.poll(29, *rows)
    assert rig.m.status()['reserved_bandwidth_mbps'] == 48
    t1.received = 30
    rig.poll(30, *rows)
    assert set(rig.m.sessions) == {'theater:a'}
    assert rig.m.status()['reserved_bandwidth_mbps'] == 19.2
    assert [i['ignored'] for i in rig.m.status()['theater']['instances']] == [False, True]
    assert not any(t.uncertain for t in rig.m.theaters)
    assert rig.q.prefs['alt_up_limit'] == qbt_budget(rig.m.cfg, upload_budget(rig.m.cfg, 19200))


def test_one_theater_behind_two_urls_counts_its_streams_once(rig, monkeypatch):
    enable(rig, monkeypatch, stream('a', 2))
    add_theater(rig, monkeypatch, '2', stream('a', 2))
    rig.poll(0, plex('a', '1'))
    assert rig.m.theaters[1].uncertain
    assert rig.m.status()['reserved_bandwidth_mbps'] == 19.2
    assert rig.q.prefs['alt_up_limit'] == qbt_budget(rig.m.cfg, upload_budget(rig.m.cfg, 19200))


def test_numbered_theater_instances_from_environment(monkeypatch):
    env = {'THEATER_URL_3': 'http://three:3000', 'THEATER_API_KEY_3': 'key-3',
           'THEATER_URL_2': 'http://two:3000', 'THEATER_API_KEY_2': 'key-2', 'THEATER_URL_4': ''}
    assert numbered_theaters(env) == [('2', 'http://two:3000', 'key-2'), ('3', 'http://three:3000', 'key-3')]
    cfg = Config(theater_url='http://one:3000', theater_api_key='key-1', theater_extra=numbered_theaters(env))
    assert [tuple(t.values()) for t in cfg.theaters] == [
        ('1', 'http://one:3000', 'key-1'), ('2', 'http://two:3000', 'key-2'), ('3', 'http://three:3000', 'key-3')]
    assert {'key-1', 'key-2', 'key-3'} <= RedactingFormatter(cfg).secrets
    monkeypatch.setattr(StateManager, '_connect', lambda self: None)
    manager = StateManager(cfg)
    assert [(t.name, t.url, t.api_key) for t in manager.theaters] == [tuple(t.values()) for t in cfg.theaters]


def test_second_instance_alone_is_enough():
    cfg = Config(theater_extra=[('2', 'http://two:3000', 'key-2')])
    assert [t['name'] for t in cfg.theaters] == ['2']


@pytest.mark.parametrize('extra,message', [
    ([('2', 'http://two:3000', '')], 'THEATER_API_KEY_2 is required'),
    ([('2', 'ftp://two', 'key')], 'THEATER_URL_2 must be'),
    ([('2', 'http://ONE:3000/', 'key')], 'THEATER_URL_2 repeats'),
    ([('1', 'http://two:3000', 'key')], 'THEATER_URL_2')])
def test_numbered_theater_validation(extra, message):
    with pytest.raises(ValueError, match=message):
        Config(theater_url='http://one:3000', theater_api_key='key-1', theater_extra=extra)


def test_theater_outage_holds_reservation_then_ignores_instance(rig, monkeypatch):
    t = enable(rig, monkeypatch, stream(viewers=2))
    rig.poll(0, plex())
    limit = rig.q.prefs['alt_up_limit']
    # Theater stops answering: its last snapshot keeps the 19.2 Mbps reservation.
    rig.poll(29, plex())
    assert rig.m.status()['reserved_bandwidth_mbps'] == 19.2
    assert rig.q.mode and rig.q.prefs['alt_up_limit'] == limit and not t.uncertain
    # After the timeout it is ignored; its local Plex row is not counted on its own.
    rig.poll(30, plex())
    assert not rig.m.sessions and not rig.q.mode
    assert rig.m.status()['theater']['instances'][0]['ignored']
    t.snapshot = snapshot(stream(viewers=2, revision=2), sequence=2)
    t.received = 35
    rig.poll(35, plex())
    assert rig.m.status()['reserved_bandwidth_mbps'] == 19.2 and rig.q.mode
    assert not rig.m.status()['theater']['instances'][0]['ignored']


def test_never_reached_instance_is_ignored_not_minimum(rig, monkeypatch):
    t = enable(rig, monkeypatch, stream(viewers=2))
    t.snapshot = t.received = None
    rig.poll(0, plex(), plex('remote', '2', bandwidth='5000', local='0', user='Friend', player='phone'))
    assert set(rig.m.sessions) == {'2'} and not t.uncertain
    assert rig.q.prefs['alt_up_limit'] == qbt_budget(rig.m.cfg, upload_budget(rig.m.cfg, 5000))


def test_plex_outage_holds_theater_reservation_then_disables(rig, monkeypatch):
    enable(rig, monkeypatch, stream(viewers=2))
    rig.poll(0, plex())
    limit = rig.q.prefs['alt_up_limit']
    rig.p.query.side_effect = TimeoutError()
    rig.poll(119)
    assert rig.q.mode and rig.q.prefs['alt_up_limit'] == limit
    rig.poll(120)
    assert not rig.q.mode


def test_local_theater_rows_count_once_through_the_api(rig, monkeypatch):
    enable(rig, monkeypatch, stream('a', 3))
    rig.poll(0, plex('a', '1'), plex('not-in-a-room', '2', player='player-9'),
             plex('lan-tv', '3', user='Home', player='tv'),
             plex('remote', '4', bandwidth='5000', local='0', user='Friend', player='phone'))
    # Theater's local row counts once at 12*3*.8; other LAN rows are free; remote rows count.
    assert set(rig.m.sessions) == {'theater:a', '4'}
    assert rig.m.status()['reserved_bandwidth_mbps'] == 33.8


def test_replacement_stream_not_yet_listed_by_plex_keeps_previous_bandwidth(rig, monkeypatch):
    t = enable(rig, monkeypatch, stream('old', viewers=2))
    rig.poll(0, plex('old', '10'))
    t.snapshot = snapshot(stream('new', viewers=2, revision=2), sequence=2)
    t.received = 5
    rig.poll(5, plex('old', '10'))
    assert set(rig.m.sessions) == {'theater:new'} and not t.uncertain
    assert rig.m.status()['reserved_bandwidth_mbps'] == 19.2
    assert rig.q.prefs['alt_up_limit'] != rig.m.cfg.min_upload_bps
    # Bridged from one of Plex's, it plays on the player that one did.
    assert rig.m.sessions['theater:new'].observation.player_id == 'player-1'
    rig.poll(10, plex('new', '11', bandwidth='13000'))
    assert rig.m.status()['reserved_bandwidth_mbps'] == 20.8


def test_plex_outage_freezes_even_pending_qbt_write_and_external_changes(rig):
    rig.q.fail_write = True
    rig.poll(0, xml_session())
    rig.q.fail_write = False
    rig.q.calls.clear()
    rig.p.query.side_effect = TimeoutError()
    rig.poll(119)
    assert rig.q.calls == []
    rig.poll(120)
    assert not rig.q.mode
    assert not any(call[0] == 'set_prefs' for call in rig.q.calls)
    rig.p.query.side_effect = None
    rig.poll(125, xml_session())
    assert rig.q.mode


@pytest.mark.parametrize('field,value', [('viewer_count', -1), ('viewer_count', True),
    ('state_age_seconds', float('nan')), ('state_revision', '3'), ('hls_session_id', ''), ('state', 'bad')])
def test_invalid_snapshot_rejected_atomically(field, value):
    s = stream()
    s[field] = value
    with pytest.raises(ValueError):
        validate_snapshot(snapshot(s))


def test_duplicate_session_rejected():
    with pytest.raises(ValueError):
        validate_snapshot(snapshot(stream(), stream()))


def test_startup_zero_viewers_does_not_enable_alternative_mode(rig, monkeypatch):
    enable(rig, monkeypatch, stream(viewers=0))
    rig.poll(0, plex())
    assert not rig.q.mode and not rig.m.sessions


def test_custom_plex_stale_timeout_overrides_relaxation_debounce(rig):
    rig.m.cfg.plex_stale_seconds = 20
    rig.m.cfg.debounce_seconds = 300
    rig.poll(0, xml_session())
    previous = rig.q.prefs['alt_up_limit']
    rig.p.query.side_effect = TimeoutError()
    rig.poll(19)
    assert rig.q.mode and rig.q.prefs['alt_up_limit'] == previous
    rig.poll(20)
    assert not rig.q.mode and rig.q.prefs['alt_up_limit'] == previous


@pytest.mark.parametrize('kwargs', [dict(theater_url='http://x'), dict(theater_url='ftp://x', theater_api_key='secret'),
    dict(theater_bandwidth_factor=0), dict(theater_bandwidth_factor='NaN'), dict(theater_stale_seconds=0)])
def test_theater_config_validation(kwargs):
    with pytest.raises(ValueError):
        Config(**kwargs)


@pytest.mark.parametrize('field,value', [('engine', 'ffmpeg'), ('engine', 'plex'), ('bandwidth_kbps', 1),
    ('bandwidth_kbps', 10_000_000), ('bandwidth_source', 'measured'), ('bandwidth_source', 'target'),
    ('plex_client_id', None), ('plex_client_id', 'c'), ('plex_client_id', 'c' * 256), ('future_key', {'any': 1})])
def test_ffmpeg_stream_keys_accepted(field, value):
    s = stream()
    s[field] = value
    assert validate_snapshot(snapshot(s))['streams'][0][field] == value


@pytest.mark.parametrize('field,value', [('engine', 'vlc'), ('engine', None), ('engine', ''),
    ('bandwidth_kbps', 0), ('bandwidth_kbps', -1), ('bandwidth_kbps', 10_000_001), ('bandwidth_kbps', True),
    ('bandwidth_kbps', 8000.0), ('bandwidth_kbps', '8000'), ('bandwidth_kbps', None),
    ('bandwidth_source', 'guessed'), ('bandwidth_source', None), ('plex_client_id', ''),
    ('plex_client_id', 'c' * 257), ('plex_client_id', 7), ('plex_client_id', ['c'])])
def test_invalid_ffmpeg_stream_keys_rejected(field, value):
    s = ffmpeg(client='plex-discord-theater-hls-1')
    s[field] = value
    with pytest.raises(ValueError):
        validate_snapshot(snapshot(s))


def test_plex_engine_stream_without_new_keys_is_matched_as_before(rig, monkeypatch):
    t = enable(rig, monkeypatch, stream(viewers=2))
    rig.poll(0, plex())
    session = rig.m.status()['sessions']['theater:hls-1']
    assert session['bandwidth_kbps'] == 19200 and session['bandwidth_source'] == 'plex_session'
    assert session['theater']['plex_bandwidth_kbps'] == 12000 and not t.uncertain
    assert [session['theater'][k] for k in ('engine', 'bandwidth_kbps', 'bandwidth_source', 'plex_client_id')] == [None] * 4


@pytest.mark.parametrize('viewers,mode,kbps', [(1, 'direct_p2p', 8000), (3, 'direct_p2p', 19200), (3, 'vps_relay', 8000)])
def test_ffmpeg_stream_without_plex_row_reserves_theater_bandwidth(rig, monkeypatch, viewers, mode, kbps):
    t = enable(rig, monkeypatch, ffmpeg(viewers=viewers))
    t.snapshot['delivery_mode'] = mode
    rig.poll(0)
    assert set(rig.m.sessions) == {'theater:hls-1'} and not t.uncertain
    assert rig.m.status()['reserved_bandwidth_mbps'] == kbps / 1000
    assert rig.m.status()['unknown_bandwidth_sessions'] == 0
    assert rig.q.prefs['alt_up_limit'] == qbt_budget(rig.m.cfg, upload_budget(rig.m.cfg, kbps))


def test_ffmpeg_reporter_rows_are_claimed_once_and_keep_theater_bandwidth(rig, monkeypatch):
    client = 'plex-discord-theater-hls-1'
    t = enable(rig, monkeypatch, ffmpeg(viewers=2, client=client))
    # Remote, so either row counted raw would add to the budget; the newer one carries the
    # file's bitrate, which is not what ffmpeg makes of it.
    rig.poll(0, reporter(client, '1', local='0'), reporter(client, '2', local='0', bandwidth='20000'),
             plex('ordinary', '3', bandwidth='5000', local='0', user='Friend', player='phone'))
    status = rig.m.status()
    assert set(rig.m.sessions) == {'theater:hls-1', '3'} and not t.uncertain
    # 8 Mbps × 2 viewers × 0.8 + the ordinary 5 Mbps.
    assert status['reserved_bandwidth_mbps'] == 17.8
    assert status['unknown_bandwidth_sessions'] == 0
    details = status['sessions']['theater:hls-1']['theater']
    assert (details['plex_user'], details['plex_user_id'], details['plex_client_id']) == ('Bot', '7', client)
    assert details['plex_bandwidth_kbps'] == 20000 and details['bandwidth_kbps'] == 8000
    assert rig.q.prefs['alt_up_limit'] == qbt_budget(rig.m.cfg, upload_budget(rig.m.cfg, 17800))


def test_ffmpeg_stream_listed_by_plex_a_poll_late_is_not_reset(rig, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger='plex-qbt')
    client = 'plex-discord-theater-hls-1'
    t = enable(rig, monkeypatch, ffmpeg(viewers=2, client=client))
    rig.poll(0)
    assert rig.m.status()['sessions']['theater:hls-1']['theater']['plex_user'] == ''
    t.received = 5
    rig.poll(5, reporter(client))
    session = rig.m.status()['sessions']['theater:hls-1']
    assert session['theater']['plex_user'] == 'Bot' and session['bandwidth_kbps'] == 12800
    assert 'identity changed' not in caplog.text


def test_ffmpeg_pause_holds_reservation_for_pause_grace(rig, monkeypatch):
    t = enable(rig, monkeypatch, ffmpeg(viewers=3))
    rig.poll(0)
    assert rig.m.status()['reserved_bandwidth_mbps'] == 19.2
    # While paused ffmpeg's lead, and so its measurement, changes; what was reserved playing holds.
    for now in (10, 69, 70):
        t.snapshot = snapshot(ffmpeg(viewers=3, kbps=2000, state='paused', revision=2, age=now - 10))
        t.received = now
        rig.poll(now)
        assert rig.q.mode == (now < 70)
        if now < 70:
            assert rig.m.status()['reserved_bandwidth_mbps'] == 19.2
    t.snapshot = snapshot(ffmpeg(viewers=3, kbps=9000, revision=3))
    t.received = 80
    rig.poll(80)
    assert rig.q.mode and rig.m.status()['reserved_bandwidth_mbps'] == 21.6


def test_ffmpeg_zero_viewers_hold_reservation_for_stop_grace(rig, monkeypatch):
    t = enable(rig, monkeypatch, ffmpeg(viewers=3))
    rig.poll(0)
    t.snapshot = snapshot(ffmpeg(viewers=0))
    t.received = 10
    rig.poll(10)
    assert rig.m.status()['reserved_bandwidth_mbps'] == 19.2
    assert rig.m.status()['sessions']['theater:hls-1']['state'] == 'stopped'
    t.received = 39
    rig.poll(39)
    assert rig.q.mode
    t.received = 40
    rig.poll(40)
    assert not rig.q.mode and not rig.m.sessions


def test_ffmpeg_stream_without_bandwidth_falls_back_to_plex_matching(rig, monkeypatch):
    client = 'plex-discord-theater-hls-1'
    t = enable(rig, monkeypatch, ffmpeg(viewers=2, kbps=None, client=client))
    rig.poll(0, reporter(client, local='0'))
    # Nothing Plex lists has its session id: as for any unmatched stream, it counts as nothing,
    # never the minimum. The reporter's row is still Theater's, never an ordinary stream.
    assert t.uncertain and not rig.m.sessions
    assert not rig.q.mode
    t.snapshot = snapshot(ffmpeg(viewers=2, kbps=None))
    t.received = 5
    rig.poll(5, plex())
    assert not t.uncertain and rig.m.status()['reserved_bandwidth_mbps'] == 19.2
    assert rig.m.status()['sessions']['theater:hls-1']['bandwidth_source'] == 'plex_session'


def test_ffmpeg_stream_losing_bandwidth_keeps_its_last_theater_figure(rig, monkeypatch):
    t = enable(rig, monkeypatch, ffmpeg(viewers=2))
    rig.poll(0)
    t.snapshot = snapshot(ffmpeg(viewers=2, kbps=None))
    t.received = 5
    rig.poll(5)
    # Owned already, so matching goes on from what it last had, as a Plex row gone quiet does.
    assert not t.uncertain and rig.m.status()['reserved_bandwidth_mbps'] == 12.8
    assert rig.m.status()['sessions']['theater:hls-1']['bandwidth_source'] == 'theater_snapshot'


def test_ffmpeg_seek_moves_reservation_to_the_new_session(rig, monkeypatch):
    client = 'plex-discord-theater-old'
    t = enable(rig, monkeypatch, ffmpeg('old', viewers=2, client=client))
    rig.poll(0, reporter(client, local='0'))
    # A seek starts a new session; the reporter keeps the stream's first one as its player.
    t.snapshot = snapshot(ffmpeg('new', viewers=2, kbps=9000, source='target', client=client, revision=2), sequence=2)
    t.received = 5
    rig.poll(5, reporter(client, local='0'))
    assert set(rig.m.sessions) == {'theater:new'} and set(t.owned) == {'new'}
    assert rig.m.status()['reserved_bandwidth_mbps'] == 14.4 and not t.uncertain
    assert rig.m.status()['sessions']['theater:new']['theater']['bandwidth_source'] == 'target'


@pytest.mark.parametrize('state', ['playing', 'paused'])
@pytest.mark.parametrize('row', [None, 'reporter', 'reporter_with_session_id'])
def test_removed_ffmpeg_room_gets_its_stop_grace_from_removal(rig, monkeypatch, state, row):
    client = 'plex-discord-theater-hls-1' if row else None

    def rows(now):
        # The reporter tells Plex the stream stopped only some seconds after it has gone,
        # and Plex drops the row inside the stop grace. With reporting off there is none.
        if row is None or now >= 25:
            return []
        item = plex('hls-1' if row == 'reporter_with_session_id' else '', bandwidth=None, local='0', player=client)
        item.find('Player').set('state', state)
        return [item]

    t = enable(rig, monkeypatch, ffmpeg(viewers=2, client=client))
    rig.poll(0, *rows(0))
    if state == 'paused':
        t.snapshot = snapshot(ffmpeg(viewers=2, client=client, state='paused', revision=2), sequence=2)
        t.received = 5
        rig.poll(5, *rows(5))
    t.snapshot = snapshot(sequence=3)
    for now in (10, 20, 25, 39):
        t.received = now
        rig.poll(now, *rows(now))
        # Released as an unlisted stream is, the row never counted as an ordinary one.
        assert set(rig.m.sessions) == {'theater:hls-1'} and not t.owned
        assert rig.m.status()['sessions']['theater:hls-1']['state'] == 'missing'
        assert rig.m.status()['unknown_bandwidth_sessions'] == 0
        assert rig.q.prefs['alt_up_limit'] == qbt_budget(rig.m.cfg, upload_budget(rig.m.cfg, 12800))
    t.received = 40
    rig.poll(40)
    assert not rig.q.mode and not rig.m.sessions and not t.retired_clients


@pytest.mark.parametrize('state', ['playing', 'paused'])
def test_late_snapshot_transfers_raw_reporter_row(rig, monkeypatch, state):
    client = 'plex-discord-theater-hls-1'
    rig.poll(0, reporter(client, local='0'))
    assert rig.m.status()['unknown_bandwidth_sessions'] == 1
    # Paused, the stream owns no device for an unmatched row to be suppressed by.
    enable(rig, monkeypatch, ffmpeg(viewers=2, client=client, state=state))
    item = reporter(client, local='0')
    item.find('Player').set('state', state)
    rig.poll(5, item)
    assert set(rig.m.sessions) == {'theater:hls-1'}
    assert rig.m.status()['unknown_bandwidth_sessions'] == 0
    assert rig.q.prefs['alt_up_limit'] == qbt_budget(rig.m.cfg, upload_budget(rig.m.cfg, 12800))


def test_one_theater_behind_two_urls_with_ffmpeg_counts_its_streams_once(rig, monkeypatch):
    enable(rig, monkeypatch, ffmpeg('a', 2))
    add_theater(rig, monkeypatch, '2', ffmpeg('a', 2))
    rig.poll(0)
    assert rig.m.theaters[1].uncertain
    assert rig.m.status()['reserved_bandwidth_mbps'] == 12.8


def test_status_and_debug_log_show_ffmpeg_engine_source_and_bandwidth(rig, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger='plex-qbt')
    monkeypatch.setattr(app, 'state', rig.m)
    client = 'plex-discord-theater-a'
    enable(rig, monkeypatch, ffmpeg('a', viewers=3, kbps=8450, client=client), stream('b', 2))
    rig.poll(0, reporter(client, '1'), plex('b', '2'))
    status = app.app.test_client().get('/status').get_json()
    measured, matched = status['sessions']['theater:a'], status['sessions']['theater:b']
    # 8.45 Mbps × 3 viewers × 0.8, not the minimum budget.
    assert measured['bandwidth_kbps'] == 20280 and measured['bandwidth_source'] == 'theater_snapshot'
    assert {k: measured['theater'][k] for k in ('engine', 'bandwidth_kbps', 'bandwidth_source', 'plex_client_id')} == {
        'engine': 'ffmpeg', 'bandwidth_kbps': 8450, 'bandwidth_source': 'measured', 'plex_client_id': client}
    assert matched['bandwidth_source'] == 'plex_session' and matched['theater']['engine'] is None
    assert status['reserved_bandwidth_mbps'] == 39.48 and not status['theater']['uncertain']
    assert status['desired_upload_bps'] == qbt_budget(rig.m.cfg, upload_budget(rig.m.cfg, 39480))
    assert f"effective_kbps=20280.0 state=playing revision=('boot-1', 1) matched=True engine=ffmpeg " \
           f"bandwidth_source=measured plex_client_id={client!r}" in caplog.text
    assert 'bandwidth_kbps None -> 20280.0 (Theater reservation)' in caplog.text


def test_ffmpeg_episode_change_keeps_previous_reporter_row_out_of_budget(rig, monkeypatch):
    t = enable(rig, monkeypatch, ffmpeg('ep1', viewers=2, client='plex-discord-theater-ep1'))
    rig.poll(0, reporter('plex-discord-theater-ep1', '1', local='0'))
    t.snapshot = snapshot(ffmpeg('ep2', viewers=2, kbps=6000, client='plex-discord-theater-ep2',
                                 rating='43', revision=2), sequence=2)
    # Plex lists the first episode's row until the reporter says it stopped, polls later.
    for now in (5, 10, 15):
        t.received = now
        rig.poll(now, reporter('plex-discord-theater-ep1', '1', local='0'),
                 reporter('plex-discord-theater-ep2', '2', local='0', rating='43'))
        assert set(rig.m.sessions) == {'theater:ep2'} and not t.uncertain
        assert rig.m.status()['reserved_bandwidth_mbps'] == 9.6
    assert rig.q.prefs['alt_up_limit'] == qbt_budget(rig.m.cfg, upload_budget(rig.m.cfg, 9600))
    t.received = 20
    rig.poll(20, reporter('plex-discord-theater-ep2', '2', local='0', rating='43'))
    assert not t.retired_clients


def test_ffmpeg_seek_while_paused_does_not_keep_the_old_session_owned(rig, monkeypatch):
    client = 'plex-discord-theater-old'
    t = enable(rig, monkeypatch, ffmpeg('old', viewers=2, client=client))
    rig.poll(0, reporter(client))
    t.snapshot = snapshot(ffmpeg('old', viewers=2, client=client, state='paused', revision=2), sequence=2)
    t.received = 5
    rig.poll(5, reporter(client))
    # Paused, so no handoff: the old session gets its stop grace as an unlisted one does today.
    t.snapshot = snapshot(ffmpeg('new', viewers=2, client=client, state='paused', revision=2), sequence=3)
    t.received = 10
    rig.poll(10, reporter(client))
    assert set(t.owned) == {'new'}
    assert rig.m.status()['sessions']['theater:old']['state'] == 'missing'
    t.received = 40
    rig.poll(40, reporter(client))
    assert set(rig.m.sessions) == {'theater:new'}


@pytest.mark.parametrize('state', ['playing', 'paused'])
def test_reporter_row_listed_with_the_first_session_id_is_still_only_the_reporters(rig, monkeypatch, state):
    client = 'plex-discord-theater-hls-1'
    # The reporter's X-Plex-Session-Identifier is the stream's first hls session id. Phase 0
    # saw Plex list no Session id for its row, but nothing promises Plex never will.
    row = lambda: plex('hls-1', bandwidth=None, local='0', player=client)
    t = enable(rig, monkeypatch, ffmpeg(viewers=2, client=client))
    rig.poll(0, row())
    assert set(rig.m.sessions) == {'theater:hls-1'} and not t.uncertain
    assert rig.q.prefs['alt_up_limit'] == qbt_budget(rig.m.cfg, upload_budget(rig.m.cfg, 12800))
    # A seek's new session keeps the reporter's player, and the row its first session id.
    t.snapshot = snapshot(ffmpeg('new', viewers=2, client=client, state=state, revision=2), sequence=2)
    t.received = 5
    rig.poll(5, row())
    assert set(t.owned) == {'new'} and not t.uncertain
    assert rig.m.status()['unknown_bandwidth_sessions'] == 0
    if state == 'playing':
        assert set(rig.m.sessions) == {'theater:new'} and rig.m.status()['reserved_bandwidth_mbps'] == 12.8


def test_reporter_row_with_the_session_id_between_a_seeks_sessions_is_still_only_the_reporters(rig, monkeypatch):
    client = 'plex-discord-theater-hls-1'
    row = lambda: plex('hls-1', bandwidth=None, local='0', player=client)
    t = enable(rig, monkeypatch, ffmpeg(viewers=2, client=client))
    rig.poll(0, row())
    # A seek's old session, its run stopped and the new one not yet announced: Theater sends
    # engine and player but no figure (sync.ts ffmpegStreamFacts), and the stream goes on
    # from what it had. Its reporter row, even listed with this Session id, isn't a second one.
    t.snapshot = snapshot(ffmpeg(viewers=2, kbps=None, client=client), sequence=2)
    t.received = 5
    rig.poll(5, row())
    assert not t.uncertain and set(rig.m.sessions) == {'theater:hls-1'}
    assert rig.m.status()['reserved_bandwidth_mbps'] == 12.8


@pytest.mark.parametrize('engine', [None, 'plex'])
def test_theater_bandwidth_counts_for_a_stream_of_plexs_too(rig, monkeypatch, engine):
    s = stream(viewers=2)
    s.update(bandwidth_kbps=2400, bandwidth_source='measured')
    if engine:
        s['engine'] = engine
    t = enable(rig, monkeypatch, s)
    # A Direct Stream copy: Plex's figure is how fast it hands Theater the segments over the LAN.
    rig.poll(0, plex(bandwidth='120980', local='0'))
    session = rig.m.status()['sessions']['theater:hls-1']
    assert set(rig.m.sessions) == {'theater:hls-1'} and not t.uncertain
    assert session['bandwidth_kbps'] == 3840 and session['bandwidth_source'] == 'theater_snapshot'
    assert session['theater']['plex_bandwidth_kbps'] == 120980
    assert rig.q.prefs['alt_up_limit'] == qbt_budget(rig.m.cfg, upload_budget(rig.m.cfg, 3840))


def test_plex_stream_without_theater_bandwidth_keeps_plexs(rig, monkeypatch):
    enable(rig, monkeypatch, stream(viewers=2))
    rig.poll(0, plex())
    session = rig.m.status()['sessions']['theater:hls-1']
    assert session['bandwidth_kbps'] == 19200 and session['bandwidth_source'] == 'plex_session'


@pytest.mark.parametrize('client', [None, 'plex-discord-theater-hls-1'])
def test_ffmpeg_stream_not_listed_by_plex_plays_on_its_reporters_player(rig, monkeypatch, client):
    enable(rig, monkeypatch, ffmpeg(viewers=2, client=client))
    rig.poll(0)
    obs = rig.m.sessions['theater:hls-1'].observation
    assert (obs.player_id, obs.user, obs.session_id, obs.rating_key) == (client or '', '', 'hls-1', '42')


def test_reporter_row_lagging_behind_the_stream_title_keeps_the_streams(rig, monkeypatch):
    client = 'plex-discord-theater-hls-1'
    t = enable(rig, monkeypatch, ffmpeg(viewers=2, client=client, rating='43'))
    # Plex lists what the reporter last told it, which can trail the stream it reports.
    rig.poll(0, reporter(client, local='0', rating='42'))
    assert set(rig.m.sessions) == {'theater:hls-1'} and not t.uncertain
    assert rig.m.sessions['theater:hls-1'].observation.rating_key == '43'
    assert rig.m.status()['sessions']['theater:hls-1']['theater']['plex_user'] == 'Bot'
    assert rig.m.status()['reserved_bandwidth_mbps'] == 12.8


def test_plex_stream_bridged_from_an_ffmpeg_one_says_whose_figure_it_reserves(rig, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger='plex-qbt')
    client = 'plex-discord-theater-hls-1'
    t = enable(rig, monkeypatch, ffmpeg(viewers=2, kbps=8450, client=client))
    rig.poll(0, reporter(client, '1', local='0', bandwidth='20000'))
    # The room's next title plays through Plex, which lists its transcode a poll or two later.
    t.snapshot = snapshot(stream('next', 2, rating='43', revision=2), sequence=2)
    for now, rows in ((5, ()), (10, (plex('next', '2', bandwidth=None, rating='43'),))):
        t.received = now
        rig.poll(now, reporter(client, '1', local='0', bandwidth='20000'), *rows)
        session = rig.m.status()['sessions']['theater:next']
        assert session['bandwidth_kbps'] == 13520 and session['bandwidth_source'] == 'theater_snapshot'
        assert session['theater']['plex_bandwidth_kbps'] is None and session['theater']['engine'] is None
    t.received = 15
    rig.poll(15, reporter(client, '1', local='0', bandwidth='20000'), plex('next', '2', bandwidth='7000', rating='43'))
    session = rig.m.status()['sessions']['theater:next']
    assert session['bandwidth_kbps'] == 11200 and session['bandwidth_source'] == 'plex_session'
    assert session['theater']['plex_bandwidth_kbps'] == 7000 and not t.uncertain
    assert set(rig.m.sessions) == {'theater:next'} and 'identity changed' not in caplog.text

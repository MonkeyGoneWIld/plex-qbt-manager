import copy
from decimal import Decimal

import pytest

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
    assert rig.q.prefs['alt_up_limit'] == rig.m.cfg.min_upload_bps
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
    t1.received = 119
    rig.poll(119, *rows)
    assert rig.m.status()['reserved_bandwidth_mbps'] == 48
    t1.received = 120
    rig.poll(120, *rows)
    assert set(rig.m.sessions) == {'theater:a'}
    assert rig.m.status()['reserved_bandwidth_mbps'] == 19.2
    assert [i['ignored'] for i in rig.m.status()['theater']['instances']] == [False, True]
    assert not any(t.uncertain for t in rig.m.theaters)
    assert rig.q.prefs['alt_up_limit'] == qbt_budget(rig.m.cfg, upload_budget(rig.m.cfg, 19200))


def test_one_theater_behind_two_urls_uses_minimum_budget(rig, monkeypatch):
    enable(rig, monkeypatch, stream('a', 2))
    add_theater(rig, monkeypatch, '2', stream('a', 2))
    rig.poll(0, plex('a', '1'))
    assert rig.m.theaters[1].uncertain
    assert rig.q.prefs['alt_up_limit'] == rig.m.cfg.min_upload_bps


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
    rig.poll(119, plex())
    assert rig.m.status()['reserved_bandwidth_mbps'] == 19.2
    assert rig.q.mode and rig.q.prefs['alt_up_limit'] == limit and not t.uncertain
    # After the timeout it is ignored; its local Plex row is not counted on its own.
    rig.poll(120, plex())
    assert not rig.m.sessions and not rig.q.mode
    assert rig.m.status()['theater']['instances'][0]['ignored']
    t.snapshot = snapshot(stream(viewers=2, revision=2), sequence=2)
    t.received = 130
    rig.poll(130, plex())
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

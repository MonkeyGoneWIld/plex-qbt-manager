import copy
from decimal import Decimal

import pytest

from app import Config
from controller import qbt_budget, upload_budget
from theater import Theater, validate_snapshot, viewer_weight
from test_controller import rig, xml_session


def stream(sid='hls-1', viewers=1, state='playing', revision=1, age=0):
    return dict(room_id='room', variant_id=sid, hls_session_id=sid,
                plex_transcode_key=None, rating_key='42', state=state,
                state_revision=revision, state_age_seconds=age,
                viewer_count=viewers, host_heartbeat_age_seconds=0)


def snapshot(*streams, server='server-1', instance='boot-1', sequence=1):
    return dict(schema_version=1, instance_id=instance, sequence=sequence,
                plex_server_id=server, delivery_mode='direct_p2p', streams=list(streams))


def plex(sid='hls-1', key='1', bandwidth='12000', local='1', user='Bot'):
    item = xml_session(key=key, bandwidth=bandwidth, local=local, user=user)
    item.find('Session').set('id', sid)
    item.find('User').set('id', '7')
    return item


def enable(rig, monkeypatch, *streams):
    rig.m.cfg.theater_bandwidth_factor = Decimal('0.8')
    rig.m.theater = Theater(rig.m.cfg, rig.clock)
    t = rig.m.theater
    t.snapshot = snapshot(*streams)
    t.received = rig.clock()
    monkeypatch.setattr(t, 'poll', lambda: None)
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
             plex('ordinary', '4', bandwidth='5000', local='0'))
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


@pytest.mark.parametrize('server,streams', [('wrong-server', [stream()]), ('server-1', [stream('unmatched')])])
def test_unmatched_or_foreign_snapshot_never_exempts_account(rig, monkeypatch, server, streams):
    t = enable(rig, monkeypatch)
    t.snapshot = snapshot(*streams, server=server)
    rig.poll(0, plex(local='0'))
    assert '1' in rig.m.sessions
    assert rig.m.theater.uncertain
    assert rig.q.prefs['alt_up_limit'] == rig.m.cfg.min_upload_bps


def test_theater_outage_protects_but_plex_outage_disables(rig, monkeypatch):
    enable(rig, monkeypatch, stream(viewers=2))
    rig.poll(0, plex())
    rig.poll(30, plex())
    assert rig.q.prefs['alt_up_limit'] == rig.m.cfg.min_upload_bps
    rig.p.query.side_effect = TimeoutError()
    rig.poll(150)
    assert not rig.q.mode


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

import json
import logging
import threading
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from xml.etree.ElementTree import Element, SubElement

import pytest

import app
from controller import MIB, StateManager, read_observations, upload_budget, qbt_budget


def xml_session(key='1', state='playing', bandwidth='12000', local='0', player='player-1', rating='42', user='Alice', location='wan'):
    item = Element('Video', sessionKey=key, ratingKey=rating)
    attrs = {'state': state, 'machineIdentifier': player}
    if local is not None:
        attrs['local'] = local
    SubElement(item, 'Player', attrs)
    SubElement(item, 'User', title=user)
    attrs = {'location': location}
    if bandwidth is not None:
        attrs['bandwidth'] = bandwidth
    SubElement(item, 'Session', attrs)
    return item


def container(*items):
    root = Element('MediaContainer', size=str(len(items)))
    root.extend(items)
    return root


class Clock:
    now = 0
    def __call__(self):
        return self.now


class FakeQbt:
    def __init__(self):
        self.mode = False
        self.prefs = {'alt_up_limit': 1024, 'alt_dl_limit': 123456, 'up_limit': 0, 'dl_limit': 0}
        self.calls = []
        self.fail_read = self.fail_write = self.ignore_write = self.fail_toggle = False
        self.transfer = self.app = self

    @property
    def speed_limits_mode(self):
        self.calls.append(('read_mode',))
        if self.fail_read:
            raise TimeoutError('mode timeout')
        return str(int(self.mode))

    @property
    def preferences(self):
        self.calls.append(('read_prefs',))
        return dict(self.prefs)

    def set_preferences(self, values):
        self.calls.append(('set_prefs', values))
        if self.fail_write:
            raise TimeoutError('write timeout')
        if not self.ignore_write:
            self.prefs.update(values)

    def set_speed_limits_mode(self, intended_state):
        self.calls.append(('set_mode', intended_state))
        if self.fail_toggle:
            raise TimeoutError('toggle timeout')
        self.mode = intended_state


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setattr(StateManager, '_connect', lambda self: None)
    cfg = app.Config(dynamic_upload_enabled=True)
    clock, qbt = Clock(), FakeQbt()
    manager = StateManager(cfg, clock=clock)
    plex = SimpleNamespace(machineIdentifier='server-1', query=Mock(return_value=container()))
    manager.plex, manager.qbt = plex, qbt
    monkeypatch.setattr(manager, '_open_plex', lambda: plex)
    monkeypatch.setattr(manager, '_open_qbt', lambda: qbt)

    def poll(at, *items):
        clock.now = at
        plex.query.return_value = container(*items)
        manager.refresh()

    return SimpleNamespace(m=manager, clock=clock, q=qbt, p=plex, poll=poll)


def hint(**overrides):
    payload = {'event': 'media.resume', 'Player': {'uuid': 'player-1'},
               'Metadata': {'ratingKey': 42}, 'Account': {'title': 'Alice'}, 'Server': {'uuid': 'server-1'}}
    payload.update(overrides)
    return payload


@pytest.mark.parametrize('multiplier,expected', [('1', 8582912), ('1.5', 6582912)])
def test_exact_user_example(multiplier, expected):
    cfg = app.Config(bandwidth_multiplier=multiplier)
    assert upload_budget(cfg, 12000 + 5000 + 15000) == expected
    assert upload_budget(cfg, 0) == 12 * MIB
    assert upload_budget(cfg, 99999999) == MIB
    assert upload_budget(cfg, 0, unknown=True) == MIB


def test_fractional_bounds_round_inward():
    cfg = app.Config(min_upload_mib='1.0000001', max_upload_mib='12.0000001')
    assert upload_budget(cfg, 99999999) == MIB + 1
    assert upload_budget(cfg, 0) == 12 * MIB
    assert qbt_budget(cfg, upload_budget(cfg, 99999999)) == MIB + 1024


@pytest.mark.parametrize('multiplier,expected', [('1', 8582144), ('1.5', 6582272)])
def test_qbt_whole_kib_precision(multiplier, expected):
    cfg = app.Config(bandwidth_multiplier=multiplier)
    assert qbt_budget(cfg, upload_budget(cfg, 32000)) == expected


def test_reject_unrepresentable_qbt_range():
    with pytest.raises(ValueError, match='whole KiB'):
        app.Config(min_upload_mib='1.00001', max_upload_mib='1.00002')


@pytest.mark.parametrize('settings', [
    {'min_upload_mib': 0}, {'min_upload_mib': -1}, {'max_upload_mib': 0},
    {'min_upload_mib': 13}, {'max_upload_mib': 2048}, {'bandwidth_multiplier': 0},
    {'bandwidth_multiplier': 'NaN'}, {'max_upload_mib': 'Infinity'}, {'min_upload_mib': 'oops'},
    {'polling_interval': 0}, {'stop_delay_seconds': -1}, {'qbt_timeout': 0},
    {'polling_interval': 1.5}, {'dynamic_upload_enabled': 'maybe'}, {'http_port': 65536},
    {'plex_stale_seconds': 0}, {'log_level': 'oops'},
])
def test_invalid_config(settings):
    with pytest.raises(ValueError):
        app.Config(**settings)


def test_env_loaded_per_instance(monkeypatch):
    monkeypatch.setenv('MAX_UPLOAD_MIB', '9.5')
    assert app.Config().max_upload_mib == Decimal('9.5')
    monkeypatch.setenv('MAX_UPLOAD_MIB', '8')
    assert app.Config().max_upload_mib == 8


@pytest.mark.parametrize('local,location,remote', [('0', 'wan', True), ('1', 'wan', False),
    ('false', '', True), ('true', '', False), (None, 'lan', False), (None, 'wan', True), (None, '', True)])
def test_locality(local, location, remote):
    assert bool(read_observations(container(xml_session(local=local, location=location)))) == remote


@pytest.mark.parametrize('bandwidth', [None, '', '0', '-1', 'NaN', 'bad'])
def test_unknown_bandwidth(bandwidth, rig):
    rig.poll(0, xml_session(bandwidth=bandwidth))
    assert rig.q.prefs['alt_up_limit'] == MIB
    assert rig.m.status()['unknown_bandwidth_sessions'] == 1


def test_sum_dedup_and_token_endpoint(rig):
    rig.poll(0, xml_session(), xml_session(), xml_session('2', bandwidth='5000'),
             xml_session('3', bandwidth='15000'), xml_session('4', local='1', bandwidth='500000'))
    assert rig.m.status()['reserved_bandwidth_mbps'] == 32
    assert rig.q.prefs['alt_up_limit'] == 8582144
    assert rig.q.mode
    rig.p.query.assert_called_once_with('/status/sessions')
    assert rig.q.prefs['up_limit'] == 0 and rig.q.prefs['alt_dl_limit'] == 123456
    assert rig.q.calls.index(('set_prefs', {'alt_up_limit': 8582144})) < rig.q.calls.index(('set_mode', True))


def test_quality_changes_without_toggling(rig):
    rig.poll(0, xml_session(bandwidth='5000'))
    rig.poll(5, xml_session(bandwidth='15000'))
    assert rig.q.prefs['alt_up_limit'] == 10706944
    assert [c for c in rig.q.calls if c[0] == 'set_mode'] == [('set_mode', True)]


def test_repeated_pauses_have_fresh_grace(rig):
    for at, playback in [(0, 'playing'), (10, 'paused'), (20, 'playing'), (25, 'paused'), (70, 'paused')]:
        rig.poll(at, xml_session(state=playback))
    assert rig.m.sessions['1'].remaining(70) == 15
    assert rig.q.mode
    rig.poll(85, xml_session(state='paused'))
    assert not rig.q.mode
    rig.poll(90, xml_session(state='paused'))
    assert not rig.m.sessions


def test_resume_hint_catches_unobserved_resume(rig):
    rig.poll(0, xml_session())
    rig.poll(10, xml_session(state='paused'))
    rig.clock.now = 65
    rig.m.poke(hint())
    rig.m.poke({'event': 'media.pause'})
    rig.poll(65, xml_session(state='paused'))
    rig.poll(70, xml_session(state='paused'))
    assert rig.q.mode and rig.m.sessions['1'].remaining(70) == 55
    rig.poll(125, xml_session(state='paused'))
    assert not rig.q.mode


@pytest.mark.parametrize('overrides', [{'Server': {'uuid': 'wrong'}}, {'Player': {'uuid': 'wrong'}},
    {'Account': {'title': 'Bob'}}, {'Metadata': {'ratingKey': 'other'}}, {'Player': {}}])
def test_unmatched_hint_does_not_reset_other_session(rig, overrides):
    rig.poll(0, xml_session())
    rig.poll(10, xml_session(state='paused'))
    rig.clock.now = 65
    rig.m.poke(hint(**overrides))
    rig.poll(70, xml_session(state='paused'))
    assert not rig.q.mode


def test_ambiguous_hint_not_applied(rig):
    rig.poll(0, xml_session(), xml_session('2'))
    rig.poll(10, xml_session(state='paused'), xml_session('2', state='paused'))
    rig.clock.now = 65
    rig.m.poke(hint())
    rig.poll(70, xml_session(state='paused'), xml_session('2', state='paused'))
    assert not rig.q.mode


def test_expired_hint_not_applied(rig):
    rig.m.poke(hint())
    rig.poll(30, xml_session(state='paused'))
    assert not rig.q.mode


def test_resume_after_pause_expired(rig):
    rig.poll(0, xml_session())
    rig.poll(10, xml_session(state='paused'))
    rig.poll(70, xml_session(state='paused'))
    rig.poll(75, xml_session())
    assert rig.q.mode
    rig.poll(80, xml_session(state='paused'))
    assert rig.m.sessions['1'].remaining(80) == 60


def test_missing_then_paused_uses_full_pause_grace(rig):
    rig.poll(0, xml_session())
    rig.poll(10)
    rig.poll(20, xml_session(state='paused', bandwidth=None))
    rig.poll(40, xml_session(state='paused', bandwidth='0'))
    assert rig.q.mode and rig.m.sessions['1'].remaining(40) == 40
    assert rig.m.sessions['1'].bandwidth_kbps == 12000
    rig.poll(80, xml_session(state='paused'))
    assert not rig.q.mode


def test_pause_then_stop_gets_stop_grace(rig):
    rig.poll(0, xml_session())
    rig.poll(10, xml_session(state='paused'))
    rig.poll(20)
    rig.poll(50)
    assert not rig.q.mode


def test_buffering_remains_protected(rig):
    rig.poll(0, xml_session(state='buffering'))
    rig.poll(200, xml_session(state='buffering', bandwidth='0'))
    assert rig.q.mode and rig.m.sessions['1'].since is None


def test_one_stream_stopping_does_not_disable_others(rig):
    rig.poll(0, xml_session(), xml_session('2', bandwidth='5000'))
    rig.poll(10, xml_session('2', bandwidth='5000'))
    rig.poll(40, xml_session('2', bandwidth='5000'))
    assert rig.q.mode
    assert rig.q.prefs['alt_up_limit'] == 11957248


def test_missing_bandwidth_retains_last_known(rig):
    rig.poll(0, xml_session())
    rig.poll(5, xml_session(bandwidth=None))
    assert rig.m.sessions['1'].bandwidth_source == 'last_known'
    assert rig.q.prefs['alt_up_limit'] == 11082752


def test_plex_failure_holds_then_minimum_then_recovers(rig):
    rig.poll(0, xml_session())
    rig.poll(10, xml_session(state='paused'))
    previous = rig.q.prefs['alt_up_limit']
    rig.p.query.side_effect = TimeoutError('Plex timeout')
    rig.poll(80)
    assert rig.q.mode and rig.q.prefs['alt_up_limit'] == previous
    assert not rig.m.status()['plex_connected']
    rig.poll(130)
    assert rig.m.stale and not rig.m.sessions
    assert rig.q.mode and rig.q.prefs['alt_up_limit'] == MIB
    rig.p.query.side_effect = None
    rig.poll(135)
    assert not rig.m.stale and not rig.q.mode


def test_startup_plex_failure_does_not_assume_empty(rig):
    rig.q.mode = True
    rig.p.query.side_effect = TimeoutError('Plex timeout')
    rig.poll(0)
    assert rig.q.mode and not rig.q.calls
    rig.poll(120)
    assert rig.q.mode and rig.q.prefs['alt_up_limit'] == MIB


def test_malformed_snapshot_is_not_empty(rig):
    rig.poll(0, xml_session())
    rig.p.query.return_value = Element('error')
    rig.m.refresh()
    assert rig.q.mode and not rig.m.last_plex_ok


def test_no_redundant_qbt_calls_and_correct_external_drift(rig):
    rig.poll(0, xml_session())
    count = len(rig.q.calls)
    rig.poll(5, xml_session())
    assert len(rig.q.calls) == count
    rig.q.mode, rig.q.prefs['alt_up_limit'] = False, 0
    rig.poll(60, xml_session())
    assert rig.q.mode and rig.q.prefs['alt_up_limit'] == 11082752


def test_full_debounce_for_relaxation_but_protection_immediate(rig):
    rig.poll(0, xml_session())
    rig.poll(1, xml_session(bandwidth='5000'))
    assert rig.q.prefs['alt_up_limit'] == 11082752
    rig.poll(3, xml_session(bandwidth='5000'))
    assert rig.q.prefs['alt_up_limit'] == 11957248
    rig.poll(4, xml_session(bandwidth='15000'))
    assert rig.q.prefs['alt_up_limit'] == 10706944


@pytest.mark.parametrize('failure', ['fail_read', 'fail_write', 'ignore_write', 'fail_toggle'])
def test_qbt_failures_retry_and_reconnect(rig, failure):
    setattr(rig.q, failure, True)
    for at in (0, 5, 10):
        rig.poll(at, xml_session())
    assert rig.m.qbt is None and rig.m.qbt_failures == 3
    assert not rig.m.qbt_ok
    setattr(rig.q, failure, False)
    rig.poll(15, xml_session())
    assert rig.q.mode and rig.m.qbt_ok and rig.m.qbt_failures == 0


def test_failed_limit_write_never_enables_unverified_limit(rig):
    rig.q.ignore_write = True
    rig.poll(0, xml_session())
    assert not rig.q.mode
    assert not any(c[0] == 'set_mode' for c in rig.q.calls)


def test_legacy_mode_does_not_write_preferences(rig):
    rig.m.cfg.dynamic_upload_enabled = False
    rig.poll(0, xml_session())
    assert rig.q.mode
    assert not any(c[0] in ('set_prefs', 'read_prefs') for c in rig.q.calls)


@pytest.mark.parametrize('payload', [[], 'text', 42, None, {}, {'event': []}, {'event': 'media.play', 'Account': []}])
def test_invalid_webhooks_return_400(rig, monkeypatch, payload):
    monkeypatch.setattr(app, 'state', rig.m)
    result = app.app.test_client().post('/webhook', data=json.dumps(payload), content_type='application/json')
    assert result.status_code == 400


def test_webhook_is_nonblocking_and_health_reports_failures(rig, monkeypatch):
    monkeypatch.setattr(app, 'state', rig.m)
    client = app.app.test_client()
    assert client.post('/webhook', data={'payload': json.dumps(hint())}).status_code == 200
    assert rig.m.wake.is_set() and not rig.p.query.called
    rig.poll(0, xml_session())
    assert client.get('/health').status_code == 200
    snapshot = client.get('/status').get_json()
    assert snapshot['desired_upload_bps'] == 11082752
    assert 'plex_token' not in snapshot and 'qbt_password' not in snapshot
    rig.p.query.side_effect = TimeoutError()
    rig.poll(5)
    assert client.get('/health').status_code == 503


def test_credentials_redacted_in_logs():
    cfg = app.Config(plex_token='secret-token', qbt_password='pass / word')
    record = logging.LogRecord('plex-qbt', logging.ERROR, '', 0,
        'request secret-token pass / word pass%20%2F%20word http://user:pw@host X-Plex-Token=other-secret', (), None)
    result = app.RedactingFormatter(cfg).format(record)
    for secret in ('secret-token', 'pass / word', 'pass%20%2F%20word', 'user:pw', 'other-secret'):
        assert secret not in result


def test_diagnostic_logs_include_budget_and_timer(rig, caplog):
    caplog.set_level(logging.DEBUG, logger='plex-qbt')
    rig.poll(0, xml_session())
    rig.poll(10, xml_session(state='paused'))
    assert 'reserved_mbps=12.000' in caplog.text
    assert 'target_bps=11082752' in caplog.text
    assert 'state=playing -> paused grace_seconds=60' in caplog.text
    assert 'qBittorrent verified' in caplog.text


def test_transcoding_uses_session_bandwidth_not_source_bitrate(rig):
    item = xml_session(bandwidth='5000')
    SubElement(item, 'Media', bitrate='80000')
    SubElement(item, 'TranscodeSession', videoDecision='transcode')
    rig.poll(0, item)
    assert rig.m.status()['reserved_bandwidth_mbps'] == 5
    assert rig.q.prefs['alt_up_limit'] == 11957248


def test_audio_sessions_are_counted(rig):
    item = xml_session(bandwidth='320')
    item.tag = 'Track'
    rig.poll(0, item)
    assert rig.m.status()['reserved_bandwidth_mbps'] == 0.32
    assert rig.q.mode


def test_reused_session_key_does_not_retain_old_bandwidth(rig):
    rig.poll(0, xml_session())
    rig.poll(5, xml_session(rating='new-item', bandwidth=None))
    assert rig.m.sessions['1'].bandwidth_kbps is None
    assert rig.q.prefs['alt_up_limit'] == MIB


def test_status_does_not_block_on_qbt_io(rig):
    entered, release = threading.Event(), threading.Event()

    class SlowQbt(FakeQbt):
        @property
        def speed_limits_mode(self):
            entered.set()
            assert release.wait(3)
            return super().speed_limits_mode

    rig.m.qbt = SlowQbt()
    worker = threading.Thread(target=rig.poll, args=(0, xml_session()), daemon=True)
    worker.start()
    try:
        assert entered.wait(2)
        result = []
        reader = threading.Thread(target=lambda: result.append(rig.m.status()), daemon=True)
        reader.start()
        reader.join(timeout=1)
        assert not reader.is_alive() and result[0]['tracked_sessions'] == 1
    finally:
        release.set()
        worker.join(timeout=3)
    assert not worker.is_alive()


def test_invalid_json_and_oversized_webhooks(rig, monkeypatch):
    monkeypatch.setattr(app, 'state', rig.m)
    client = app.app.test_client()
    assert client.post('/webhook', data='{broken', content_type='application/json').status_code == 400
    assert client.post('/webhook', data={'payload': '{broken'}).status_code == 400
    assert client.post('/webhook', data='x' * (2 * 1024 * 1024 + 1)).status_code == 413


def test_stale_fallback_toggle_only_does_not_overwrite_preferences(rig):
    rig.m.cfg.dynamic_upload_enabled = False
    rig.p.query.side_effect = TimeoutError()
    rig.poll(120)
    assert rig.q.mode and rig.q.prefs['alt_up_limit'] == 1024


def test_duplicate_hints_in_one_cycle_do_not_extend_pause_forever(rig):
    rig.poll(0, xml_session())
    rig.poll(5, xml_session(state='paused'))
    rig.clock.now = 10
    rig.m.poke(hint())
    rig.m.poke(hint())
    rig.poll(10, xml_session(state='paused'))
    rig.poll(70, xml_session(state='paused'))
    assert not rig.q.mode and not rig.m.hints

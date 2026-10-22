"""Exercise real PlexAPI/qbittorrent-api serialization over local HTTP."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from qbittorrentapi import APIConnectionError, LoginFailed
from app import Config
from controller import StateManager


@pytest.fixture
def api_server():
    data = SimpleNamespace(mode=0, prefs={'alt_up_limit': 1024, 'alt_dl_limit': 2048, 'up_limit': 0},
                           requests=[], seen=[], active=True, api_key=None, drop=False)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def route(self):
            """The request's path past qui's /proxy/<key>, or None once it has been refused."""
            path = urlsplit(self.path).path
            data.seen.append((self.command, path, self.headers.get('Authorization')))
            if path.startswith('/proxy/'):
                key, _, path = path[len('/proxy/'):].partition('/')
                if key != 'test-qui-key':
                    self.send_error(401)
                    return None
                return '/' + path
            if path.startswith('/api/v2/') and data.drop:
                # A stalled qBittorrent: the connection ends without an answer.
                self.close_connection = True
                return None
            if path.startswith('/api/v2/') and data.api_key and self.headers.get('Authorization') != f'Bearer {data.api_key}':
                self.send_error(403)
                return None
            return path

        def reply(self, body, content_type='text/plain', headers=None):
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body.encode())

        def do_HEAD(self):
            if self.route() is not None:
                self.reply('')

        def do_GET(self):
            path = self.route()
            if path is None:
                return
            data.requests.append(('GET', path, dict(self.headers)))
            if path.startswith('/api/v2/') and self.headers.get('Connection') == 'close':
                self.close_connection = True
            if path == '/':
                self.reply('<MediaContainer machineIdentifier="server-1" version="1.40.0" friendlyName="Test Plex"/>', 'application/xml')
            elif path == '/status/sessions':
                items = ''.join(f'<Video sessionKey="{i}" ratingKey="42"><User title="Alice"/>'
                                f'<Player state="playing" local="0" machineIdentifier="p{i}"/>'
                                f'<Session bandwidth="{bandwidth}" location="wan"/></Video>'
                                for i, bandwidth in enumerate((12000, 5000, 15000))) if data.active else ''
                self.reply(f'<MediaContainer>{items}</MediaContainer>', 'application/xml')
            elif path == '/api/v2/app/version':
                self.reply('v4.6.7')
            elif path == '/api/v2/app/webapiVersion':
                self.reply('2.11.0')
            elif path == '/api/v2/app/preferences':
                self.reply(json.dumps(data.prefs), 'application/json')
            elif path == '/api/v2/transfer/speedLimitsMode':
                self.reply(str(data.mode))
            else:
                self.send_error(404)

        def do_POST(self):
            body = parse_qs(self.rfile.read(int(self.headers.get('Content-Length', 0))).decode())
            path = self.route()
            if path is None:
                return
            data.requests.append(('POST', path, body))
            if path == '/api/v2/auth/login':
                self.reply('Ok.', headers={'Set-Cookie': 'SID=test-session; Path=/'})
            elif path == '/api/v2/app/setPreferences':
                changes = json.loads(body['json'][0])
                # qBittorrent v4 stores speed limits as whole KiB/s internally.
                data.prefs.update({key: max(1, value // 1024) * 1024 for key, value in changes.items()})
                self.reply('')
            elif path == '/api/v2/transfer/setSpeedLimitsMode':
                data.mode = int(body['mode'][0])
                self.reply('')
            elif path == '/api/v2/transfer/toggleSpeedLimitsMode':
                data.mode = 1 - data.mode
                self.reply('')
            else:
                self.send_error(404)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', data
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize('multiplier,expected', [('1', 8582144), ('1.5', 6582272)])
def test_real_clients_use_token_and_bytes(api_server, multiplier, expected):
    url, data = api_server
    now = [0]
    manager = StateManager(Config(plex_url=url, qbt_url=url, plex_token='test-plex-token',
                                 qbt_username='test-user', qbt_password='test-password',
                                 dynamic_upload_enabled=True, bandwidth_multiplier=multiplier),
                           clock=lambda: now[0])
    manager.refresh()
    assert data.mode == 1
    assert data.prefs == {'alt_up_limit': expected, 'alt_dl_limit': 2048, 'up_limit': 0}
    assert manager.qbt_ok and manager.last_plex_ok
    # No HEAD to guess http or https: a stalled qBittorrent times that out before the cap.
    assert not any(method == 'HEAD' for method, _, _ in data.seen)
    qbt_reads = [headers for method, path, headers in data.requests if method == 'GET' and path.startswith('/api/v2/')]
    assert qbt_reads and all(headers.get('Connection') == 'close' for headers in qbt_reads)
    session_requests = [headers for method, path, headers in data.requests if path == '/status/sessions']
    assert session_requests and session_requests[0]['X-Plex-Token'] == 'test-plex-token'
    writes = [body for method, path, body in data.requests if path == '/api/v2/app/setPreferences']
    assert json.loads(writes[0]['json'][0]) == {'alt_up_limit': expected}
    # After logging in, the cap goes out first; the Web API version is asked once, not per change.
    assert [(method, path) for method, path, _ in data.requests if path.startswith('/api/v2/')] == [
        ('POST', '/api/v2/auth/login'), ('GET', '/api/v2/app/version'), ('GET', '/api/v2/app/webapiVersion'),
        ('POST', '/api/v2/app/setPreferences'), ('GET', '/api/v2/app/preferences'),
        ('POST', '/api/v2/transfer/setSpeedLimitsMode'), ('GET', '/api/v2/transfer/speedLimitsMode')]
    # Exact readback + whole-KiB values prevent endless retry loops.
    requests = len(data.requests)
    now[0] = 5
    manager.refresh()
    assert len(data.requests) == requests + 1
    data.active = False
    now[0] = 10
    manager.refresh()
    now[0] = 40
    manager.refresh()
    assert data.mode == 0 and data.prefs['up_limit'] == 0


@pytest.mark.parametrize('login', ['api_key', 'qui_proxy'])
def test_qbittorrent_without_a_password(api_server, login):
    url, data = api_server
    if login == 'api_key':
        data.api_key = 'qbt_test-key'
        qbt = {'qbt_url': url, 'qbt_api_key': 'qbt_test-key'}
    else:
        qbt = {'qbt_url': 'http://qbittorrent.invalid:8080', 'qui_proxy_url': f'{url}/proxy/test-qui-key'}
    manager = StateManager(Config(plex_url=url, plex_token='test-plex-token', dynamic_upload_enabled=True, **qbt),
                           clock=lambda: 0)
    manager.refresh()
    assert manager.qbt_ok and data.mode == 1 and data.prefs['alt_up_limit'] == 8582144
    assert not any(method == 'HEAD' for method, _, _ in data.seen)
    calls = [(method, path, auth) for method, path, auth in data.seen if '/api/v2/' in path]
    if login == 'api_key':
        # The key goes on every request in place of a login.
        assert calls[0][:2] == ('GET', '/api/v2/app/version')
        assert all(auth == 'Bearer qbt_test-key' for _, _, auth in calls)
        assert not any(path.endswith('/auth/login') for _, path, _ in calls)
    else:
        # qui holds the qBittorrent login; the manager's own is a formality to qui.
        assert calls[0][:2] == ('POST', '/proxy/test-qui-key/api/v2/auth/login')
        assert all(path.startswith('/proxy/test-qui-key/api/v2/') and auth is None for _, path, auth in calls)


def test_api_key_tells_a_stalled_qbittorrent_from_a_refused_key(api_server):
    url, data = api_server
    data.api_key = 'qbt_test-key'
    manager = StateManager(Config(plex_url=url, plex_token='test-plex-token', qbt_url=url,
                                  qbt_api_key='qbt_test-key'), clock=lambda: 0)
    assert manager.qbt is not None
    # qbittorrent-api's own key check reports both as LoginFailed.
    data.drop = True
    with pytest.raises(APIConnectionError):
        manager._open_qbt()
    data.drop, data.api_key = False, 'qbt_another-key'
    with pytest.raises(LoginFailed, match='QBITTORRENT_API_KEY'):
        manager._open_qbt()


def test_qui_proxy_url_must_carry_a_client_key():
    for bad in ('http://qui:7476', 'http://qui:7476/proxy/', 'qui:7476/proxy/key'):
        with pytest.raises(ValueError, match='QUI_PROXY_URL'):
            Config(qui_proxy_url=bad)
    assert Config(qui_proxy_url='http://qui:7476/qui/proxy/abc123/').qui_proxy_key == 'abc123'

"""Exercise real PlexAPI/qbittorrent-api serialization over local HTTP."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from app import Config
from controller import StateManager


@pytest.fixture
def api_server():
    data = SimpleNamespace(mode=0, prefs={'alt_up_limit': 1024, 'alt_dl_limit': 2048, 'up_limit': 0},
                           requests=[], active=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, body, content_type='text/plain', headers=None):
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body.encode())

        def do_GET(self):
            path = urlsplit(self.path).path
            data.requests.append(('GET', path, dict(self.headers)))
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
            path = urlsplit(self.path).path
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
    session_requests = [headers for method, path, headers in data.requests if path == '/status/sessions']
    assert session_requests and session_requests[0]['X-Plex-Token'] == 'test-plex-token'
    writes = [body for method, path, body in data.requests if path == '/api/v2/app/setPreferences']
    assert json.loads(writes[0]['json'][0]) == {'alt_up_limit': expected}
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

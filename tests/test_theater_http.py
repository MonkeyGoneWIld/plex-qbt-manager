import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app import Config
from theater import Theater
from test_controller import Clock
from test_theater import snapshot, stream


def test_authenticated_http_poll_sequence_restart_and_failure_retention():
    calls = []
    response = {'status': 200, 'body': snapshot(stream()), 'redirect': None}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.path, self.headers.get('Authorization')))
            self.send_response(response['status'])
            if response['redirect']:
                self.send_header('Location', response['redirect'])
            self.end_headers()
            self.wfile.write(json.dumps(response['body']).encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        clock = Clock()
        cfg = Config(theater_url=f'http://127.0.0.1:{server.server_port}', theater_api_key='test-secret')
        theater = Theater(cfg, clock)
        theater.poll()
        assert calls == [('/api/integrations/qbt-manager/state', 'Bearer test-secret')]
        assert theater.received == 0 and theater.snapshot['sequence'] == 1
        clock.now = 4
        theater.poll()
        assert len(calls) == 1
        clock.now = 5
        theater.poll()  # Replayed response must not refresh freshness.
        assert theater.received == 0 and theater.error == 'ValueError'
        response['body'] = snapshot(stream(), sequence=2)
        clock.now = 10
        theater.poll()
        assert theater.received == 10 and theater.error is None
        response['body'] = snapshot(stream(), instance='new-boot', sequence=1)
        clock.now = 15
        theater.poll()
        assert theater.received == 15 and theater.snapshot['instance_id'] == 'new-boot'
        response['body'] = {'schema_version': 2}
        clock.now = 20
        theater.poll()
        assert theater.received == 15 and theater.snapshot['instance_id'] == 'new-boot'
        response['status'] = 302
        response['redirect'] = cfg.theater_url + '/must-not-receive-secret'
        clock.now = 25
        theater.poll()
        assert theater.received == 15
        assert all(path == '/api/integrations/qbt-manager/state' for path, _ in calls)
        response['status'] = 401
        clock.now = 30
        theater.poll()
        assert theater.error == 'HTTPError' and theater.received == 15
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_each_instance_polls_its_own_url_with_its_own_key():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.path, self.headers.get('Authorization')))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(snapshot(stream())).encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f'http://127.0.0.1:{server.server_port}'
        cfg = Config(theater_url=base + '/one', theater_api_key='key-one',
                     theater_extra=[('2', base + '/two/', 'key-two')])
        theaters = [Theater(cfg, Clock(), **instance) for instance in cfg.theaters]
        for theater in theaters:
            theater.poll()
        assert calls == [('/one/api/integrations/qbt-manager/state', 'Bearer key-one'),
                         ('/two/api/integrations/qbt-manager/state', 'Bearer key-two')]
        assert all(theater.error is None and theater.received == 0 for theater in theaters)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

"""Daten 2.0 sidecar byte serving (spec §B3, §B7): the ONE Range parser of every
byte route, 206/416/200, HEAD = the same headers without a body, ETag +
``If-None-Match`` → 304, and JSON (never HTML) for every refusal incl. the
transport's own errors and a method other than GET/HEAD.

``daten/http_server.py`` imports only the stdlib, ``contract`` and
``link_tokens`` at module level (its heavy half is loaded when a real server is
built), so it is loaded by path here and served over a real socket with a fake
library that hands out known bytes.
"""

import http.client
import importlib.util
import json
import pathlib
import socket
import threading
import unittest

from timeout_guard import BoundedTestCase  # V1-3: a hang fails within the limit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
HTTP_SERVER_PATH = (REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
                    / 'daten' / 'http_server.py')


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HS = _load(HTTP_SERVER_PATH, '_daten_http_server_under_test')
SECRET = b's' * 32
DATA = bytes(range(256)) * 4            # 1024 bytes, every offset recognisable
SIZE = len(DATA)
DATASET = 'lena-schmidt/omx_f_wuerfel'
ETAG = '"0123456789abcdef-0-0"'


class _Refusal(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class FakeLibrary:
    robot_type = 'omx_f'

    def path_of(self, dataset_id):
        if dataset_id != DATASET:
            raise _Refusal('not_found')
        return pathlib.Path('/nonexistent')

    def clip(self, dataset_id, i, c, gate=None):
        if (i, c) != (0, 0):
            raise _Refusal('unplayable')
        return DATA, ETAG

    def thumb(self, dataset_id, gate=None):
        return DATA[:300], '"0123456789abcdef-thumb"'


def parse_range_cases():
    return [
        ('bytes=0-99', ('partial', (0, 99))),
        ('bytes=-100', ('partial', (SIZE - 100, SIZE - 1))),
        ('bytes=100-', ('partial', (100, SIZE - 1))),
        ('bytes=0-5000', ('partial', (0, SIZE - 1))),          # an end past the size is clamped
        ('bytes=-5000', ('partial', (0, SIZE - 1))),           # a suffix longer than the body: all of it
        (f'bytes={SIZE}-', ('unsatisfiable', None)),
        (f'bytes={SIZE + 7}-{SIZE + 9}', ('unsatisfiable', None)),
        ('bytes=5-2', ('unsatisfiable', None)),
        ('bytes=-0', ('unsatisfiable', None)),
        ('bytes=0-1,5-6', ('full', None)),                     # one range only
        ('bytes=0-1, 5-6', ('full', None)),
        ('bytes=abc', ('full', None)),
        ('bytes=-', ('full', None)),
        ('items=0-5', ('full', None)),
        ('', ('full', None)),
        (None, ('full', None)),
    ]


class ParseRange(BoundedTestCase):

    def test_every_shape(self):
        for header, expected in parse_range_cases():
            with self.subTest(header=header):
                self.assertEqual(HS.parse_range(header, SIZE), expected)

    def test_an_empty_body_satisfies_no_range(self):
        self.assertEqual(HS.parse_range('bytes=0-', 0), ('unsatisfiable', None))
        self.assertEqual(HS.parse_range('bytes=-1', 0), ('unsatisfiable', None))


class TheProductionEntryPoint(BoundedTestCase):
    """V1-2 / G-6: ``main()`` — the sidecar's production entry — installs the
    bounded client factory (HUB_CALL_TIMEOUT_S) BEFORE the server exists, so
    no hub call of the production sidecar is ever unbounded."""

    def test_main_installs_the_bounded_client_factory_before_serving(self):
        events = []
        hub_mod = type('HubMod', (), {'install_client_factory': staticmethod(
            lambda bound_s: events.append(('install', bound_s)))})

        class FakeServer:
            sidecar = type('S', (), {'library': type('L', (), {
                'start_hint_worker': staticmethod(lambda: events.append(('hints',)))})()})()

            def serve_forever(self, poll_interval=None):
                events.append(('serve',))
                raise KeyboardInterrupt

            def server_close(self):
                events.append(('close',))
        saved = HS.load_modules, HS.make_server
        HS.load_modules = lambda: (None, hub_mod)
        HS.make_server = lambda *a, **k: events.append(('make',)) or FakeServer()
        try:
            self.assertEqual(HS.main(), 0)
        finally:
            HS.load_modules, HS.make_server = saved
        self.assertEqual(events, [('install', HS.C.HUB_CALL_TIMEOUT_S), ('make',), ('hints',), ('serve',),
                                  ('close',)])
        self.assertEqual(HS.C.HUB_CALL_TIMEOUT_S, 10)


class LiveServerBase(BoundedTestCase):

    def setUp(self):
        sidecar = HS.Sidecar(SECRET, FakeLibrary(), hub=None)
        self.server = HS.Server(('127.0.0.1', 0), sidecar)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05},
                                       daemon=True)
        self.thread.start()
        self.token = HS.LT.mint(SECRET, HS.LT.dataset_scope(DATASET))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def request(self, method, path, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            conn.request(method, path, headers=headers or {})
            r = conn.getresponse()
            body = r.read()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, body
        finally:
            conn.close()

    def clip_path(self, i=0, c=0):
        return f'{HS.C.API_PREFIX}/ds/{self.token}/episode/{i}/video/{c}.mp4'


class RangeOverTheWire(LiveServerBase):

    def test_bytes_0_99(self):
        status, h, body = self.request('GET', self.clip_path(), {'Range': 'bytes=0-99'})
        self.assertEqual(status, 206)
        self.assertEqual(h['content-range'], f'bytes 0-99/{SIZE}')
        self.assertEqual(body, DATA[:100])
        self.assertEqual(h['content-length'], '100')
        self.assertEqual(h['content-type'], 'video/mp4')

    def test_the_last_100(self):
        status, h, body = self.request('GET', self.clip_path(), {'Range': 'bytes=-100'})
        self.assertEqual(status, 206)
        self.assertEqual(h['content-range'], f'bytes {SIZE - 100}-{SIZE - 1}/{SIZE}')
        self.assertEqual(body, DATA[-100:])

    def test_from_100_to_the_end(self):
        status, h, body = self.request('GET', self.clip_path(), {'Range': 'bytes=100-'})
        self.assertEqual(status, 206)
        self.assertEqual(h['content-range'], f'bytes 100-{SIZE - 1}/{SIZE}')
        self.assertEqual(body, DATA[100:])

    def test_a_start_at_or_past_the_size_is_416(self):
        for header in (f'bytes={SIZE}-', f'bytes={SIZE + 50}-{SIZE + 60}', 'bytes=5-2'):
            with self.subTest(header=header):
                status, h, body = self.request('GET', self.clip_path(), {'Range': header})
                self.assertEqual(status, 416)
                self.assertEqual(h['content-range'], f'bytes */{SIZE}')
                self.assertEqual(json.loads(body), {'error': 'range'})
                self.assertEqual(h['content-type'], 'application/json; charset=utf-8')

    def test_two_ranges_or_garbage_get_the_whole_body(self):
        for header in ('bytes=0-1,5-6', 'bytes=nonsense', 'items=0-5'):
            with self.subTest(header=header):
                status, h, body = self.request('GET', self.clip_path(), {'Range': header})
                self.assertEqual(status, 200)
                self.assertEqual(body, DATA)
                self.assertNotIn('content-range', h)

    def test_no_range_is_200_with_accept_ranges_and_the_media_cache(self):
        status, h, body = self.request('GET', self.clip_path())
        self.assertEqual(status, 200)
        self.assertEqual(body, DATA)
        self.assertEqual(h['accept-ranges'], 'bytes')
        self.assertEqual(h['cache-control'], 'private, max-age=300')
        self.assertEqual(h['etag'], ETAG)
        self.assertEqual(h['x-content-type-options'], 'nosniff')

    def test_head_answers_the_same_headers_without_a_body(self):
        for headers in ({}, {'Range': 'bytes=0-99'}, {'Range': 'bytes=-100'}, {'Range': f'bytes={SIZE}-'}):
            with self.subTest(headers=headers):
                g_status, g_headers, _ = self.request('GET', self.clip_path(), headers)
                h_status, h_headers, h_body = self.request('HEAD', self.clip_path(), headers)
                self.assertEqual(h_status, g_status)
                self.assertEqual(h_body, b'')
                g_headers.pop('date', None)
                h_headers.pop('date', None)
                self.assertEqual(h_headers, g_headers)

    def test_the_thumbnail_uses_the_same_parser(self):
        path = f'{HS.C.API_PREFIX}/ds/{self.token}/thumb.jpg'
        status, h, body = self.request('GET', path, {'Range': 'bytes=10-19'})
        self.assertEqual(status, 206)
        self.assertEqual(body, DATA[10:20])
        self.assertEqual(h['content-type'], 'image/jpeg')
        self.assertEqual(h['content-range'], 'bytes 10-19/300')

    def test_if_none_match_is_304_without_a_body(self):
        for inm in (ETAG, f'"other", {ETAG}', f'W/{ETAG}', '*'):
            with self.subTest(inm=inm):
                status, h, body = self.request('GET', self.clip_path(), {'If-None-Match': inm})
                self.assertEqual(status, 304)
                self.assertEqual(body, b'')
                self.assertEqual(h['etag'], ETAG)
        status, _, body = self.request('GET', self.clip_path(), {'If-None-Match': '"stale-0-0"'})
        self.assertEqual((status, body), (200, DATA))

    def test_an_unplayable_episode_is_409_json(self):
        status, h, body = self.request('GET', self.clip_path(i=3))
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body), {'error': 'unplayable'})
        self.assertEqual(h['cache-control'], 'no-store')


class NeverHtml(LiveServerBase):

    def test_every_other_method_is_405_json(self):
        for method in ('POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'PROPFIND'):
            with self.subTest(method=method):
                status, h, body = self.request(method, self.clip_path())
                self.assertEqual(status, 405)
                self.assertEqual(h['allow'], 'GET, HEAD')
                self.assertEqual(h['content-type'], 'application/json; charset=utf-8')
                self.assertEqual(json.loads(body), {'error': 'invalid'})

    def test_a_malformed_request_line_is_json(self):
        with socket.create_connection(('127.0.0.1', self.port), timeout=10) as s:
            s.sendall(b'GET /x HTTP/1.1 extra words\r\n\r\n')
            raw = b''
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                raw += chunk
        head, _, body = raw.partition(b'\r\n\r\n')
        self.assertIn(b' 400 ', head.split(b'\r\n')[0])
        self.assertIn(b'application/json', head)
        self.assertNotIn(b'<html', raw.lower())
        self.assertEqual(json.loads(body), {'error': 'invalid'})

    def test_an_unknown_route_is_404_json(self):
        for path in ('/', '/daten-api/v1/nothing', f'{HS.C.API_PREFIX}/ds/{self.token}/../../etc/passwd'):
            with self.subTest(path=path):
                status, h, body = self.request('GET', path)
                self.assertEqual(status, 404)
                self.assertEqual(json.loads(body), {'error': 'not_found'})
                self.assertEqual(h['x-content-type-options'], 'nosniff')

    def test_health_needs_no_token(self):
        status, h, body = self.request('GET', f'{HS.C.API_PREFIX}/health')
        self.assertEqual((status, json.loads(body)), (200, {'v': 1}))
        self.assertEqual(h['cache-control'], 'no-store')


if __name__ == '__main__':
    unittest.main()

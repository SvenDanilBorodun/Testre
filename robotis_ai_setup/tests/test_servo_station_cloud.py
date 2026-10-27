"""The Servo-Station's admin upload (``tools/servo_station/station_cloud.py``)
against a fake ``urllib`` opener — deps-free, no network.

Spec S9: no service-role key on the bench PC; an admin JWT in memory only; the
password is sent once and never logged, stored or put into an exception text.
The PostgREST status/code mapping (42501 → 403, 22023/23514 → 400, PGRST202 →
404) is an ASSUMPTION encoded here, not observed (spec §11).
"""

import ast
import io
import json
import logging
import pathlib
import shutil
import tempfile
import unittest
import urllib.error

import servo_station_fakes  # noqa: F401  (puts tools/servo_station on sys.path)

import station_cloud as cloud  # noqa: E402
import station_records as records  # noqa: E402
import station_texts_de as T  # noqa: E402

_URL = 'https://abc.supabase.co'
_KEY = 'anon-public-key'
_PASSWORD = 'Geheim-Passwort-42'
_ACCESS = 'ACCESS.jwt.token'
_REFRESH = 'REFRESH-token'
_USERNAMES = pathlib.Path(__file__).resolve().parents[1] / 'cloud_training_api' / 'app' / 'services' / 'usernames.py'


class _Response:
    def __init__(self, status, body):
        self.status = status
        self._raw = json.dumps(body).encode('utf-8') if body is not None else b''

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Scripted answers per URL path; records every request it was given."""

    def __init__(self):
        self.requests = []
        self.answers = {}

    def answer(self, path, *responses):
        self.answers.setdefault(path, []).extend(responses)

    def __call__(self, request, timeout):
        body = json.loads(request.data.decode('utf-8'))
        headers = {k.lower(): v for k, v in request.header_items()}
        path = request.full_url[len(_URL):]
        self.requests.append((path, headers, body, timeout))
        queue = self.answers.get(path) or []
        if not queue:
            raise AssertionError(f'unscripted request to {path}')
        status, payload = queue.pop(0)
        if isinstance(status, BaseException):
            raise status
        if status >= 400:
            raw = json.dumps(payload).encode('utf-8') if payload is not None else b''
            raise urllib.error.HTTPError(request.full_url, status, 'x', {}, io.BytesIO(raw))
        return _Response(status, payload)

    def paths(self):
        return [p for p, _h, _b, _t in self.requests]


_TOKEN_PW = '/auth/v1/token?grant_type=password'
_TOKEN_RF = '/auth/v1/token?grant_type=refresh_token'


def _tokens(access=_ACCESS, refresh=_REFRESH, expires_in=3600):
    return (200, {'access_token': access, 'refresh_token': refresh, 'expires_in': expires_in,
                  'token_type': 'bearer', 'user': {'id': 'b5200002-0000-0000-0000-000000000002'}})


class _Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def _session(opener, clock=None):
    return cloud.CloudSession(_URL, _KEY, opener=opener, clock=clock or _Clock())


def _record(serial='V1S-000123', checksum='c' * 64):
    return {'schema': records.RECORD_SCHEMA, 'servo_serial': serial, 'checksum': checksum,
            'calibrated_at': '2026-10-27T09:30:00Z'}


class TestSignIn(unittest.TestCase):
    def test_the_password_grant_and_the_tokens_in_memory(self):
        opener = FakeOpener()
        opener.answer(_TOKEN_PW, _tokens())
        session = _session(opener)
        session.sign_in('  Admin  ', _PASSWORD)
        (path, headers, body, timeout), = opener.requests
        self.assertEqual(path, _TOKEN_PW)
        self.assertEqual(headers['apikey'], _KEY)
        self.assertNotIn('authorization', headers)
        self.assertEqual(body, {'email': 'admin@edubotics.local', 'password': _PASSWORD})
        self.assertEqual(timeout, 10)
        self.assertTrue(session.signed_in)
        self.assertEqual(session.username, 'admin')

    def test_a_bad_username_is_refused_without_a_request(self):
        opener = FakeOpener()
        session = _session(opener)
        for name in ('ab', 'a b', '-admin', 'x' * 33, ''):
            with self.subTest(name=name):
                with self.assertRaises(cloud.CloudError) as ctx:
                    session.sign_in(name, _PASSWORD)
                self.assertEqual(ctx.exception.kind, 'bad_username')
                self.assertEqual(ctx.exception.message_de, T.BAD_USERNAME)
        self.assertEqual(opener.requests, [])

    def test_wrong_credentials_are_auth_failed(self):
        opener = FakeOpener()
        opener.answer(_TOKEN_PW, (400, {'error': 'invalid_grant'}))
        with self.assertRaises(cloud.CloudError) as ctx:
            _session(opener).sign_in('admin', _PASSWORD)
        self.assertEqual(ctx.exception.kind, 'auth_failed')
        self.assertEqual(ctx.exception.message_de, T.CLOUD_AUTH_FAILED)

    def test_the_password_and_tokens_appear_in_no_log_repr_or_exception(self):
        captured = io.StringIO()
        handler = logging.StreamHandler(captured)
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, handler)
        self.addCleanup(root.setLevel, old_level)
        texts = []
        opener = FakeOpener()
        opener.answer(_TOKEN_PW, (400, {'msg': 'nope'}), _tokens())
        opener.answer(cloud.RPC_PATH, (403, {'code': '42501'}))
        session = _session(opener)
        with self.assertRaises(cloud.CloudError) as ctx:
            session.sign_in('admin', _PASSWORD)
        texts += [str(ctx.exception), repr(ctx.exception), ctx.exception.message_de]
        session.sign_in('admin', _PASSWORD)
        texts.append(repr(session))
        with self.assertRaises(cloud.CloudError) as ctx:
            session.submit(_record())
        texts += [str(ctx.exception), repr(ctx.exception), ctx.exception.message_de]
        opener.answer(_TOKEN_PW, (urllib.error.URLError(f'refused {_PASSWORD}'), None))
        with self.assertRaises(cloud.CloudError) as ctx:
            session.sign_in('admin', _PASSWORD)
        self.assertIsNone(ctx.exception.__cause__)
        texts += [str(ctx.exception), repr(ctx.exception)]
        texts.append(captured.getvalue())
        for text in texts:
            for secret in (_PASSWORD, _ACCESS, _REFRESH):
                self.assertNotIn(secret, text)

    def test_the_url_must_be_https_or_a_local_stack(self):
        for url in ('https://abc.supabase.co', 'https://abc.supabase.co/', 'http://localhost:54321',
                    'http://127.0.0.1:54321'):
            with self.subTest(url=url):
                cloud.CloudSession(url, _KEY, opener=FakeOpener())
        for url in ('http://abc.supabase.co', 'https://abc.supabase.co/rest', 'ftp://x.y',
                    'https://user@x.y', 'https://x.y?a=b'):
            with self.subTest(url=url):
                with self.assertRaises(cloud.CloudError) as ctx:
                    cloud.CloudSession(url, _KEY, opener=FakeOpener())
                self.assertEqual(ctx.exception.kind, 'bad_url')
        for url, key in (('', _KEY), (_URL, ''), (_URL, '   ')):
            with self.assertRaises(cloud.CloudError) as ctx:
                cloud.CloudSession(url, key, opener=FakeOpener())
            self.assertEqual(ctx.exception.kind, 'config_missing')
            self.assertEqual(ctx.exception.message_de, T.CLOUD_CONFIG_MISSING)

    def test_a_redirect_is_never_followed(self):
        handler = cloud._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, 'Found', {}, 'https://evil.example'))


class TestSubmit(unittest.TestCase):
    def _signed_in(self, clock=None):
        opener = FakeOpener()
        opener.answer(_TOKEN_PW, _tokens())
        session = _session(opener, clock)
        session.sign_in('admin', _PASSWORD)
        return opener, session

    def test_the_rpc_call(self):
        opener, session = self._signed_in()
        opener.answer(cloud.RPC_PATH, (200, {'stored': True, 'replaced': False}))
        self.assertEqual(session.submit(_record()), {'stored': True, 'replaced': False})
        path, headers, body, _t = opener.requests[-1]
        self.assertEqual(path, '/rest/v1/rpc/submit_servo_calibration_record')
        self.assertEqual(headers['apikey'], _KEY)
        self.assertEqual(headers['authorization'], f'Bearer {_ACCESS}')
        self.assertEqual(headers['content-type'], 'application/json')
        self.assertEqual(body, {'p_record': _record()})

    def test_403_and_42501_are_not_admin(self):
        for answer in ((403, {'code': '42501', 'message': 'requires an admin account'}),
                       (403, None), (400, {'code': '42501'})):
            with self.subTest(answer=answer):
                opener, session = self._signed_in()
                opener.answer(cloud.RPC_PATH, answer)
                with self.assertRaises(cloud.CloudError) as ctx:
                    session.submit(_record())
                self.assertEqual(ctx.exception.kind, 'not_admin')
                self.assertEqual(ctx.exception.message_de, T.CLOUD_NOT_ADMIN)

    def test_401_gets_exactly_one_refresh_then_a_retry(self):
        opener, session = self._signed_in()
        opener.answer(cloud.RPC_PATH, (401, {'code': 'PGRST301'}), (200, {'stored': True}))
        opener.answer(_TOKEN_RF, _tokens(access='NEW-ACCESS'))
        self.assertEqual(session.submit(_record()), {'stored': True})
        self.assertEqual(opener.paths(), [_TOKEN_PW, cloud.RPC_PATH, _TOKEN_RF, cloud.RPC_PATH])
        self.assertEqual(opener.requests[2][2], {'refresh_token': _REFRESH})
        self.assertEqual(opener.requests[3][1]['authorization'], 'Bearer NEW-ACCESS')

    def test_a_second_401_is_auth_failed_not_a_loop(self):
        opener, session = self._signed_in()
        opener.answer(cloud.RPC_PATH, (401, None), (401, None))
        opener.answer(_TOKEN_RF, _tokens(access='NEW-ACCESS'))
        with self.assertRaises(cloud.CloudError) as ctx:
            session.submit(_record())
        self.assertEqual(ctx.exception.kind, 'auth_failed')
        self.assertEqual(opener.paths().count(_TOKEN_RF), 1)

    def test_an_expired_token_is_refreshed_before_the_call(self):
        clock = _Clock()
        opener, session = self._signed_in(clock)
        clock.t += 3600
        opener.answer(_TOKEN_RF, _tokens(access='NEW-ACCESS'))
        opener.answer(cloud.RPC_PATH, (200, {'stored': True}))
        session.submit(_record())
        self.assertEqual(opener.paths()[-2:], [_TOKEN_RF, cloud.RPC_PATH])

    def test_404_and_pgrst202_are_not_deployed(self):
        for answer in ((404, {'code': 'PGRST202'}), (404, None), (400, {'code': 'PGRST202'})):
            with self.subTest(answer=answer):
                opener, session = self._signed_in()
                opener.answer(cloud.RPC_PATH, answer)
                with self.assertRaises(cloud.CloudError) as ctx:
                    session.submit(_record())
                self.assertEqual(ctx.exception.kind, 'not_deployed')
                self.assertEqual(ctx.exception.message_de, T.CLOUD_NOT_DEPLOYED)

    def test_400_is_rejected_with_its_code(self):
        opener, session = self._signed_in()
        opener.answer(cloud.RPC_PATH, (400, {'code': '23514', 'message': 'check violation'}))
        with self.assertRaises(cloud.CloudError) as ctx:
            session.submit(_record())
        self.assertEqual((ctx.exception.kind, ctx.exception.code), ('rejected', '23514'))
        self.assertEqual(ctx.exception.message_de, T.CLOUD_REJECTED.format(code='23514'))

    def test_no_answer_and_5xx_are_unreachable(self):
        for answer in ((urllib.error.URLError('no route'), None), (TimeoutError(), None),
                       (OSError('reset'), None), (503, None), (502, {'message': 'bad gateway'})):
            with self.subTest(answer=answer):
                opener, session = self._signed_in()
                opener.answer(cloud.RPC_PATH, answer)
                with self.assertRaises(cloud.CloudError) as ctx:
                    session.submit(_record())
                self.assertEqual(ctx.exception.kind, 'unreachable')
                self.assertEqual(ctx.exception.message_de, T.CLOUD_UNREACHABLE)

    def test_not_signed_in_sends_nothing(self):
        opener = FakeOpener()
        with self.assertRaises(cloud.CloudError) as ctx:
            _session(opener).submit(_record())
        self.assertEqual(ctx.exception.kind, 'auth_failed')
        self.assertEqual(opener.requests, [])


class TestUploadPending(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='servo-station-cloud-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = records.Store(pathlib.Path(self.tmp))
        for n, serial in enumerate(('V1S-000001', 'V1S-000002', 'V1S-000003')):
            self.store.save_record(records.finalize_record(
                {'servo_serial': serial, 'calibrated_at': f'2026-10-27T09:3{n}:00Z'}))
        opener = FakeOpener()
        opener.answer(_TOKEN_PW, _tokens())
        self.opener = opener
        self.session = _session(opener)
        self.session.sign_in('admin', _PASSWORD)

    def test_uploaded_and_rejected_receipts_end_pending(self):
        self.opener.answer(cloud.RPC_PATH, (200, {'stored': True}), (400, {'code': '22023'}),
                           (200, {'stored': False, 'reason': 'newer_record_exists'}))
        result = cloud.upload_pending(self.session, self.store, now=lambda: '2026-10-27T10:00:00Z')
        self.assertEqual(result.uploaded, 2)
        self.assertEqual(result.rejected, [('V1S-000002', '22023')])
        self.assertIsNone(result.stopped)
        self.assertEqual(self.store.pending_records(), [])
        self.assertEqual(self.store.rejected_records(), [('V1S-000002', '22023')])
        receipt = json.loads(self.store.rejected_receipt_path('V1S-000002').read_text(encoding='utf-8'))
        self.assertEqual(receipt['code'], '22023')
        self.assertEqual(receipt['checksum'], self.store.load_record('V1S-000002')['checksum'])

    def test_an_unreachable_cloud_stops_the_round_and_keeps_them_pending(self):
        self.opener.answer(cloud.RPC_PATH, (200, {'stored': True}), (503, None))
        result = cloud.upload_pending(self.session, self.store)
        self.assertEqual(result.uploaded, 1)
        self.assertEqual(result.stopped.kind, 'unreachable')
        self.assertEqual([r['servo_serial'] for r in self.store.pending_records()], ['V1S-000002', 'V1S-000003'])
        self.assertEqual(self.opener.paths().count(cloud.RPC_PATH), 2)

    def test_not_deployed_stops_and_keeps_them_pending(self):
        self.opener.answer(cloud.RPC_PATH, (404, {'code': 'PGRST202'}))
        result = cloud.upload_pending(self.session, self.store)
        self.assertEqual((result.uploaded, result.stopped.kind), (0, 'not_deployed'))
        self.assertEqual(len(self.store.pending_records()), 3)


class TestLockstepWithTheApi(unittest.TestCase):
    def test_domain_and_username_pattern_equal_the_apis(self):
        tree = ast.parse(_USERNAMES.read_text(encoding='utf-8'))
        values = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if name == 'SYNTHETIC_EMAIL_DOMAIN':
                    values[name] = ast.literal_eval(node.value)
                if name == 'USERNAME_RE':
                    values[name] = ast.literal_eval(node.value.args[0])
        self.assertEqual(values['SYNTHETIC_EMAIL_DOMAIN'], cloud.SYNTHETIC_EMAIL_DOMAIN)
        self.assertEqual(values['USERNAME_RE'], cloud.USERNAME_RE.pattern)


if __name__ == '__main__':
    unittest.main()

"""The container's session backend: the four anon RPCs of migration 048 (spec §3.5, §4.2, Appendix A).

The RPC names and argument names are a frozen contract with WP-A's migration; every call
sends ALL arguments (``p_stats`` as null when there are none: PostgREST answers 404 /
PGRST202 for an omitted argument). The trusted Modal session id is checked before any HTTP
call. A transport failure is ``SessionDenied('unavailable')``; only ``invalid`` / ``ended`` /
``replaced`` are verdicts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading

import httpx
import pytest

from conftest import CONN, REPO_ID, REVISION, SESSION_ID, TOKEN
import session_backend as SB

URL = 'https://db.example.test'
ANON = 'anon-key-for-tests'

# Appendix A: the anon RPCs and their argument names, in signature order.
APPENDIX_A = {
    'inference_session_peek': ['p_token', 'p_modal_session_id'],
    'inference_session_connect': ['p_token', 'p_modal_session_id', 'p_conn_id'],
    'inference_session_heartbeat': ['p_token', 'p_modal_session_id', 'p_conn_id', 'p_stats'],
    'inference_session_end': ['p_token', 'p_modal_session_id', 'p_conn_id', 'p_reason', 'p_stats'],
}


class Recorder:
    def __init__(self, answers=None, status=200, raw=None, exc=None):
        self.calls = []
        self.answers = answers or {}
        self.status = status
        self.raw = raw
        self.exc = exc

    def __call__(self, request):
        name = request.url.path.rsplit('/', 1)[-1]
        self.calls.append({'name': name, 'path': request.url.path, 'body': json.loads(request.content),
                           'headers': dict(request.headers), 'method': request.method})
        if self.exc is not None:
            raise self.exc
        if self.raw is not None:
            return httpx.Response(self.status, content=self.raw)
        return httpx.Response(self.status, json=self.answers.get(name, {'status': 'invalid'}))


def backend(rec):
    return SB.SupabaseSessionBackend(URL, ANON, transport=httpx.MockTransport(rec))


def run(coro):
    return asyncio.run(coro)


ACTIVE = {'status': 'active', 'session_id': '9f0c-uuid', 'model_repo': REPO_ID, 'model_revision': REVISION,
          'policy_type': 'act', 'max_end_in_s': 600.000000}


def test_exact_rpc_names_and_arguments():
    rec = Recorder({'inference_session_peek': dict(ACTIVE, status='reserved', max_end_in_s=None),
                    'inference_session_connect': ACTIVE,
                    'inference_session_heartbeat': {'status': 'active', 'max_end_in_s': 590.5},
                    'inference_session_end': {'status': 'ended', 'end_reason': 'robot_stop'}})
    b = backend(rec)

    async def go():
        await b.peek(TOKEN, SESSION_ID)
        await b.connect(TOKEN, SESSION_ID, CONN)
        await b.heartbeat(TOKEN, SESSION_ID, CONN, {'chunks': 3})
        await b.heartbeat(TOKEN, SESSION_ID, CONN, None)
        await b.end(TOKEN, SESSION_ID, CONN, 'robot_stop', None)
        await b.end(TOKEN, SESSION_ID, CONN, 'stall', {'chunks': 9})
    run(go())

    names = [c['name'] for c in rec.calls]
    assert names == ['inference_session_peek', 'inference_session_connect', 'inference_session_heartbeat',
                     'inference_session_heartbeat', 'inference_session_end', 'inference_session_end']
    for c in rec.calls:
        assert c['method'] == 'POST' and c['path'] == f"/rest/v1/rpc/{c['name']}"
        assert list(c['body']) == APPENDIX_A[c['name']], c['name']       # all of them, always, in order
        assert c['body']['p_token'] == TOKEN and c['body']['p_modal_session_id'] == SESSION_ID
    assert rec.calls[2]['body']['p_stats'] == {'chunks': 3}
    assert 'p_stats' in rec.calls[3]['body'] and rec.calls[3]['body']['p_stats'] is None
    assert rec.calls[4]['body']['p_reason'] == 'robot_stop' and rec.calls[4]['body']['p_stats'] is None
    assert rec.calls[1]['body'] == {'p_token': TOKEN, 'p_modal_session_id': SESSION_ID, 'p_conn_id': CONN}
    assert 'p_transport' not in rec.calls[1]['body']                       # 048 has none


def test_headers():
    rec = Recorder({'inference_session_connect': ACTIVE})
    run(backend(rec).connect(TOKEN, SESSION_ID, CONN))
    h = rec.calls[0]['headers']
    assert h['apikey'] == ANON
    assert h['authorization'] == f'Bearer {ANON}'
    assert h['content-type'] == 'application/json'


def test_response_mapping():
    def one(answer, call):
        rec = Recorder({n: answer for n in APPENDIX_A})
        return run(call(backend(rec)))

    g = one(dict(ACTIVE, status='reserved', max_end_in_s=None), lambda b: b.peek(TOKEN, SESSION_ID))
    assert g == SB.Grant(session_id='9f0c-uuid', model_repo=REPO_ID, model_revision=REVISION, policy_type='act',
                         max_end_in_s=None)
    g = one(ACTIVE, lambda b: b.peek(TOKEN, SESSION_ID))           # peek accepts 'active' too
    assert g.max_end_in_s is None and g.model_repo == REPO_ID
    g = one(ACTIVE, lambda b: b.connect(TOKEN, SESSION_ID, CONN))
    assert g.max_end_in_s == 600.0 and g.session_id == '9f0c-uuid'
    assert one({'status': 'active', 'max_end_in_s': 42}, lambda b: b.heartbeat(TOKEN, SESSION_ID, CONN, None)) == 42.0

    cases = [
        ({'status': 'invalid'}, 'auth', None),
        ({'status': 'ended', 'end_reason': 'expired'}, 'ended', 'expired'),
        ({'status': 'ended', 'end_reason': 'superseded'}, 'ended', 'superseded'),
        ({'status': 'ended', 'end_reason': None}, 'ended', None),
        ({'status': 'ended', 'end_reason': 'x' * 65}, 'ended', None),      # never a reason the robot refuses
        ({'status': 'ended', 'end_reason': ['x']}, 'ended', None),
        ({'status': 'replaced'}, 'replaced', None),
        ({'status': 'what'}, 'unavailable', None),
        ({}, 'unavailable', None),
    ]
    for answer, code, reason in cases:
        for call in (lambda b: b.connect(TOKEN, SESSION_ID, CONN), lambda b: b.heartbeat(TOKEN, SESSION_ID, CONN, None)):
            with pytest.raises(SB.SessionDenied) as e:
                one(answer, call)
            assert (e.value.code, e.value.reason) == (code, reason), answer
    with pytest.raises(SB.SessionDenied) as e:
        one({'status': 'ended', 'end_reason': 'never_connected'}, lambda b: b.peek(TOKEN, SESSION_ID))
    assert (e.value.code, e.value.reason) == ('ended', 'never_connected')
    # A grant with a missing or wrongly typed field is not a grant.
    for broken in (dict(ACTIVE, model_repo=None), dict(ACTIVE, model_revision=7), dict(ACTIVE, max_end_in_s='5'),
                   dict(ACTIVE, max_end_in_s=True), dict(ACTIVE, session_id=None)):
        with pytest.raises(SB.SessionDenied) as e:
            one(broken, lambda b: b.connect(TOKEN, SESSION_ID, CONN))
        assert e.value.code == 'unavailable'
    with pytest.raises(SB.SessionDenied) as e:
        one({'status': 'active', 'max_end_in_s': 'soon'}, lambda b: b.heartbeat(TOKEN, SESSION_ID, CONN, None))
    assert e.value.code == 'unavailable'


def test_missing_or_malformed_session_id_is_auth_without_an_http_call():
    rec = Recorder({n: ACTIVE for n in APPENDIX_A})
    b = backend(rec)
    for msid in (None, '', 'sd-', 'xd-abc', 'sd-a b', 'sd-' + 'a' * 129, 7, b'sd-abc'):
        for call in (lambda: b.peek(TOKEN, msid), lambda: b.connect(TOKEN, msid, CONN),
                     lambda: b.heartbeat(TOKEN, msid, CONN, None)):
            with pytest.raises(SB.SessionDenied) as e:
                run(call())
            assert e.value.code == 'auth'
        assert run(b.end(TOKEN, msid, CONN, 'stall', None)) is None
    assert rec.calls == []


def test_http_errors_and_garbage_are_unavailable():
    for rec in (Recorder(status=500), Recorder(status=404), Recorder(status=401), Recorder(raw=b'<html>'),
                Recorder(raw=b'[1,2]'), Recorder(raw=b'null'),
                Recorder(exc=httpx.ConnectError('down')), Recorder(exc=httpx.ReadTimeout('slow'))):
        b = backend(rec)
        for call in (lambda: b.peek(TOKEN, SESSION_ID), lambda: b.connect(TOKEN, SESSION_ID, CONN),
                     lambda: b.heartbeat(TOKEN, SESSION_ID, CONN, None)):
            with pytest.raises(SB.SessionDenied) as e:
                run(call())
            assert e.value.code == 'unavailable'
        assert run(b.end(TOKEN, SESSION_ID, CONN, 'stall', None)) is None      # end never raises


def test_timeout_is_unavailable():
    """A real socket that accepts and never answers: the injected timeout fires -> unavailable."""
    srv = socket.socket()
    srv.bind(('127.0.0.1', 0))
    srv.listen(8)
    port = srv.getsockname()[1]
    held = []
    stop = threading.Event()

    def accept():
        srv.settimeout(0.1)
        while not stop.is_set():
            try:
                held.append(srv.accept()[0])
            except OSError:
                pass
    t = threading.Thread(target=accept, daemon=True)
    t.start()
    try:
        b = SB.SupabaseSessionBackend(f'http://127.0.0.1:{port}', ANON, timeout=0.3)

        async def go():
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            with pytest.raises(SB.SessionDenied) as e:
                await b.heartbeat(TOKEN, SESSION_ID, CONN, None)
            assert e.value.code == 'unavailable'
            assert loop.time() - t0 < 2.0
            assert await b.end(TOKEN, SESSION_ID, CONN, 'stall', None) is None
            await b.aclose()
        run(go())
    finally:
        stop.set()
        t.join(2)
        for c in held:
            c.close()
        srv.close()
    assert SB.RPC_TIMEOUT_S == 5.0


def test_no_token_in_logs(caplog):
    caplog.set_level(logging.DEBUG)
    for rec in (Recorder(status=500), Recorder(exc=httpx.ConnectError(f'down {TOKEN}')), Recorder(raw=b'x'),
                Recorder({'inference_session_connect': {'status': 'invalid'}})):
        b = backend(rec)
        for call in (lambda: b.connect(TOKEN, SESSION_ID, CONN), lambda: b.heartbeat(TOKEN, SESSION_ID, CONN, None)):
            with pytest.raises(SB.SessionDenied):
                run(call())
        run(b.end(TOKEN, SESSION_ID, CONN, 'stall', None))
    text = '\n'.join(r.getMessage() + ' ' + str(r.args) + ' ' + str(r.exc_info) for r in caplog.records)
    assert TOKEN not in text and ANON not in text
    assert all(r.exc_info is None for r in caplog.records)


def test_static_backend_ignores_the_session_id_and_checks_the_token():
    b = SB.StaticSessionBackend(TOKEN, REPO_ID, REVISION)

    async def go():
        for msid in (None, '', 'garbage', SESSION_ID):
            g = await b.peek(TOKEN, msid)
            assert (g.model_repo, g.model_revision, g.max_end_in_s) == (REPO_ID, REVISION, None)
            g = await b.connect(TOKEN, msid, CONN)
            assert g.max_end_in_s == pytest.approx(1800.0, abs=1.0)
            assert await b.heartbeat(TOKEN, msid, CONN, None) <= 1800.0
        for call in (b.peek('V' * 43, None), b.connect('V' * 43, None, CONN), b.heartbeat('V' * 43, None, CONN, None)):
            with pytest.raises(SB.SessionDenied) as e:
                await call
            assert e.value.code == 'auth'
        # A second connection takes the session over; the first one's next check is 'replaced'.
        await b.connect(TOKEN, None, 'ef' * 16)
        with pytest.raises(SB.SessionDenied) as e:
            await b.heartbeat(TOKEN, None, CONN, None)
        assert e.value.code == 'replaced'
        # end records the reason; the ended conn's check answers 'ended'; a new connect is a new run.
        assert await b.end(TOKEN, None, 'ef' * 16, 'robot_stop', None) is None
        with pytest.raises(SB.SessionDenied) as e:
            await b.heartbeat(TOKEN, None, 'ef' * 16, None)
        assert (e.value.code, e.value.reason) == ('ended', 'robot_stop')
        g = await b.connect(TOKEN, None, CONN)
        assert g.max_end_in_s == pytest.approx(1800.0, abs=1.0)
        assert await b.end('V' * 43, None, CONN, 'stall', None) is None       # never raises
    run(go())


def test_static_backend_from_env(monkeypatch):
    monkeypatch.setenv('DEV_SESSION_TOKEN', TOKEN)
    monkeypatch.setenv('DEV_MODEL_REPO', REPO_ID)
    monkeypatch.setenv('DEV_MODEL_REVISION', REVISION)
    b = SB.StaticSessionBackend.from_env()
    assert run(b.connect(TOKEN, None, CONN)).model_repo == REPO_ID
    monkeypatch.setenv('SUPABASE_URL', URL + '/')
    monkeypatch.setenv('SUPABASE_ANON_KEY', ANON)
    sb = SB.SupabaseSessionBackend.from_env()
    rec = Recorder({'inference_session_connect': ACTIVE})
    sb._transport = httpx.MockTransport(rec)
    run(sb.connect(TOKEN, SESSION_ID, CONN))
    assert rec.calls[0]['path'] == '/rest/v1/rpc/inference_session_connect'


def test_session_denied_shape():
    e = SB.SessionDenied('ended', 'expired')
    assert (e.code, e.reason) == ('ended', 'expired')
    assert SB.SessionDenied('auth').reason is None
    assert SB.VERDICT_CODES == frozenset({'auth', 'ended', 'replaced'})

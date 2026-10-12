"""The sessioned server's HTTPS routes (spec §2.2, §3.3, D42 + J-5, O7').

FastAPI ``TestClient`` against ``server_core.build_app`` with a ``FakeEngine``, a programmable
fake backend (and, for H-3, the REAL ``SupabaseSessionBackend`` over a failing fake HTTP) and a
fake session clock. The trusted ``x-modal-server-session-id`` header is set on each request as
Modal's proxy would.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

import httpx
import numpy as np
import pytest

from conftest import (CONN, CONN_B, SESSION_ID, SUMMARY, TOKEN, TOKEN_B, FakeClock, FakeEngine, bearer, headers,
                      hello_frame, obs_frame)
import cloud_protocol as P
import server_core as S
import session_backend as SB


def _err(r):
    assert r.headers['content-type'] == P.CONTENT_TYPE, r.headers
    h, pl = P.decode(r.content)
    return P.parse_error(h, pl)


def _hello(client, **kw):
    return client.post(P.HELLO_PATH, content=hello_frame(P), headers=headers(**kw))


def _infer(client, seq=1, obs_e=0, **kw):
    return client.post(P.INFER_PATH, content=obs_frame(P, seq, obs_e), headers=headers(**kw))


def _wait(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


# ---------------------------------------------------------------- the round trip

def test_hello_infer_bye_round_trip(server):
    client, backend, engine, cache = server()
    with client:
        r = _hello(client)
        assert r.status_code == 200 and r.headers['content-type'] == P.CONTENT_TYPE
        h, pl = P.decode(r.content)
        policy, limits = P.parse_ready(h, pl)
        assert h['server_sha'] == 'sha-test' and h['v'] == 1
        assert policy == SUMMARY
        assert limits == {**S.LIMITS, 'actions_per_chunk': 50, 'max_end_in_s': 600.0}
        assert S.LIMITS == {'cadence_steps': 5, 'actions_per_chunk': 50, 'aggregate_new_weight': 0.7,
                            'max_obs_per_s': 8.0, 'stall_end_s': 15.0, 'resend_after_s': 2.0}
        assert backend.calls == [('connect', TOKEN, SESSION_ID, CONN)]
        assert cache.refcount('EduBotics-Solutions/act-test', 'a' * 40) == 0    # leased and released

        state = np.array([0.5, 1, 2, 3, 4, 5], np.float32)
        r = client.post(P.INFER_PATH, content=obs_frame(P, 3, 15, state=state), headers=headers())
        assert r.status_code == 200 and r.headers['content-type'] == P.CONTENT_TYPE
        h, pl = P.decode(r.content)
        seq, obs_e, actions, gpu_ms = P.parse_chunk(h, pl)
        assert (seq, obs_e, gpu_ms) == (3, 15, 1.5)
        assert actions.shape == (50, 6)
        assert np.array_equal(actions, state[None, :] + np.arange(50, dtype=np.float32)[:, None])
        assert h['queue_ms'] >= 0 and h['decode_ms'] >= 0
        (call,) = engine.calls
        assert call['images'] == {'gripper': (48, 64, 3), 'scene': (48, 64, 3)} and call['n'] == 50
        assert call['thread'].startswith('gpu')
        assert backend.names() == ['connect']                 # the hello's entry served the observation

        r = client.post(P.BYE_PATH, content=P.build_bye('robot_stop', {'rtt_ms_p50': 110}), headers=headers())
        assert r.status_code == 204 and r.content == b''
        end = backend.calls[-1]
        assert end[:5] == ('end', TOKEN, SESSION_ID, CONN, 'robot_stop')
        assert isinstance(end[5], dict) and end[5]['chunks'] == 1 and end[5]['robot'] == {'rtt_ms_p50': 110}
        assert len(json.dumps(end[5])) <= 2048


def test_actions_per_chunk_is_capped_by_the_policy_chunk_size(server):
    engine = FakeEngine(dict(SUMMARY, chunk_size=20, n_action_steps=10))
    client, *_ = server(engine=engine)
    with client:
        _, limits = P.parse_ready(*P.decode(_hello(client).content))
        assert limits['actions_per_chunk'] == 20
        h, _ = P.decode(_infer(client).content)
        assert h['n'] == 20


def test_version_refused_with_server_v(server):
    client, backend, *_ = server()
    with client:
        body = P.encode({'t': 'hello', 'v': 2, 'conn_id': CONN, 'robot_type': 'omx_f', 'robot_profile': 'omx_full',
                         'cameras': [], 'state_dim': 6, 'fps': 30})
        r = client.post(P.HELLO_PATH, content=body, headers=headers())
        assert r.status_code == 400
        assert _err(r) == ('version', False, None, None, (1,))
        assert backend.calls == []


@pytest.mark.parametrize('code,reason,status', [('auth', None, 401), ('ended', 'expired', 410),
                                                ('ended', 'superseded', 410), ('replaced', None, 409),
                                                ('unavailable', None, 503)])
def test_each_session_denied_code_is_its_status_and_frame(server, code, reason, status):
    client, backend, *_ = server()
    backend.connect_result = SB.SessionDenied(code, reason)
    with client:
        for r in (_hello(client), _infer(client)):
            assert r.status_code == status
            assert _err(r) == (code, False, None, reason, None)
    backend.connect_result = RuntimeError('surprise')
    with client:
        r = _hello(client)
        assert r.status_code == 503 and _err(r)[0] == 'unavailable'


@pytest.mark.parametrize('kind', ['model', 'model_type'])
def test_model_refusal_ends_the_session_server_error(server, kind):
    import chunk_engine

    def loader(repo, rev, dev):
        raise chunk_engine.ModelRefused(kind, 'x')
    client, backend, *_ = server(loader=loader)
    with client:
        r = _hello(client)
        assert r.status_code == 422 and _err(r) == (kind, False, None, None, None)
        assert backend.calls[-1][:5] == ('end', TOKEN, SESSION_ID, CONN, 'server_error')


def test_bye_ends_with_its_reason_and_drops_the_entry(server):
    client, backend, *_ = server()
    with client:
        _hello(client)
        r = client.post(P.BYE_PATH, content=P.build_bye('stall'), headers=headers())
        assert r.status_code == 204
        assert backend.calls[-1][:5] == ('end', TOKEN, SESSION_ID, CONN, 'stall')
        _infer(client)
        assert backend.names() == ['connect', 'end', 'connect']        # a cache miss after the bye
        # A stale connection's bye is forwarded (the DB answers 'replaced') but keeps the live entry.
        r = client.post(P.BYE_PATH, content=P.build_bye('robot_error'), headers=headers(conn=CONN_B))
        assert r.status_code == 204 and backend.calls[-1][3:5] == (CONN_B, 'robot_error')
        assert backend.calls[-1][5] is None                              # no server stats of the live entry
        _infer(client, 2, 5)
        assert backend.names() == ['connect', 'end', 'connect', 'end']
        r = client.post(P.BYE_PATH, content=P.build_error('rate'), headers=headers())
        assert r.status_code == 400 and _err(r)[0] == 'bad_frame'


def test_authorization_and_conn_id_rules(server):
    client, backend, *_ = server()
    bad_auth = [None, '', 'Bearer', 'Bearer ', f'Basic {TOKEN}', f'Bearer {TOKEN}x', f'Bearer {TOKEN[:-1]}',
                f'Bearer {TOKEN} ', f'Bearer  {TOKEN}', 'Bearer ' + '!' * 43, TOKEN]
    with client:
        for value in bad_auth:
            h = headers()
            if value is None:
                del h['Authorization']
            else:
                h['Authorization'] = value
            for path, body in ((P.HELLO_PATH, hello_frame(P)), (P.INFER_PATH, obs_frame(P)),
                               (P.BYE_PATH, P.build_bye('stall')), (P.PREFETCH_PATH, b'')):
                r = client.post(path, content=body, headers=h)
                assert r.status_code == 401, (value, path)
                assert _err(r) == ('auth', False, None, None, None)
        for conn in (None, '', 'AB' * 16, 'ab' * 15, 'ab' * 17, 'zz' * 16):
            for path, body in ((P.INFER_PATH, obs_frame(P)), (P.BYE_PATH, P.build_bye('stall'))):
                r = client.post(path, content=body, headers=headers(conn=conn))
                assert r.status_code == 401 and _err(r)[0] == 'auth', (conn, path)
        # The edge token is Modal's business: the container never needs it.
        h = headers()
        del h['Modal-Authorization']
        assert client.post(P.HELLO_PATH, content=hello_frame(P), headers=h).status_code == 200
    assert backend.names() == ['connect']


def test_cache_miss_changed_session_header_or_conn_id_calls_connect(server):
    client, backend, *_ = server()
    with client:
        assert _infer(client).status_code == 200                       # no hello: a restarted container
        assert backend.calls == [('connect', TOKEN, SESSION_ID, CONN)]
        assert _infer(client, 2, 5).status_code == 200
        assert backend.names() == ['connect']
        assert _infer(client, 3, 10, session='sd-other').status_code == 200
        assert backend.calls[-1] == ('connect', TOKEN, 'sd-other', CONN)
        assert _infer(client, 4, 15, session='sd-other', conn=CONN_B).status_code == 200    # F-15
        assert backend.calls[-1] == ('connect', TOKEN, 'sd-other', CONN_B)
        assert _infer(client, 5, 20, session='sd-other', conn=CONN_B).status_code == 200
        assert backend.names() == ['connect'] * 3
        # Two tokens are two sessions.
        assert _infer(client, token=TOKEN_B).status_code == 200
        assert backend.calls[-1] == ('connect', TOKEN_B, SESSION_ID, CONN)


def test_session_id_is_the_request_header_never_a_body_value(server):
    client, backend, *_ = server()
    with client:
        body = P.encode({'t': 'hello', 'v': 1, 'conn_id': CONN, 'robot_type': 'omx_f', 'robot_profile': 'omx_full',
                         'cameras': [], 'state_dim': 6, 'fps': 30, 'modal_session_id': 'sd-evil',
                         'x-modal-server-session-id': 'sd-evil'})
        assert client.post(P.HELLO_PATH, content=body, headers=headers()).status_code == 200
        assert client.post(P.HELLO_PATH, content=body, headers=headers(session=None)).status_code == 200
        client.post(P.PREFETCH_PATH, content=b'', headers=headers(session='sd-p'))
        client.post(P.BYE_PATH, content=P.build_bye('stall'), headers=headers(session='sd-b'))
    seen = [c[2] for c in backend.calls]
    assert seen == [SESSION_ID, None, 'sd-p', 'sd-b']
    assert 'sd-evil' not in repr(backend.calls)


def test_every_non_200_carries_an_octet_stream_error_frame(server):
    import chunk_engine
    client, backend, engine, _ = server()
    with client:
        answers = [
            client.post(P.HELLO_PATH, content=b'garbage', headers=headers()),
            client.post(P.HELLO_PATH, content=hello_frame(P), headers={}),
            client.post(P.INFER_PATH, content=obs_frame(P), headers=headers(conn=None)),
            client.post(P.INFER_PATH, content=obs_frame(P, cams={'gripper': b'\xff\xd8\xff\xd9'}), headers=headers()),
            client.post('/v1/ws', content=b'', headers=headers()),
            client.get(P.INFER_PATH, headers=headers()),
        ]
        backend.connect_result = SB.SessionDenied('ended', 'expired')
        answers.append(_infer(client, token=TOKEN_B))
        backend.connect_result = None
        engine.fail = RuntimeError('cuda')
        answers.append(_infer(client, seq=9))
        engine.fail = None
        for r in answers:
            assert r.status_code != 200
            code, retry, *_ = _err(r)
            assert retry is P.ERROR_CODES[code]
    _ = chunk_engine


def test_health_json(server):
    client, backend, *_ = server()
    with client:
        r = client.get(P.HEALTH_PATH)
        assert r.status_code == 200 and r.json() == {'ok': True, 'v': [1], 'server_sha': 'sha-test'}
    assert backend.calls == []


def test_no_websocket_route(server):
    client, *_ = server()
    with client:
        assert client.post('/v1/ws', content=b'', headers=headers()).status_code == 404
        assert client.get('/v1/ws', headers=headers()).status_code == 404
    paths = sorted({getattr(r, 'path', '') for r in client.app.routes})
    assert paths == ['/v1/bye', '/v1/health', '/v1/hello', '/v1/infer', '/v1/prefetch']
    src = open(S.__file__, encoding='utf-8').read()
    assert 'websocket' not in src.lower() and '/v1/ws' not in src


# ---------------------------------------------------------------- body caps

def _raw_call(app, path, headers_list, chunks):
    """Drive the ASGI app directly so the test sees how many body bytes the server pulled."""
    pulled = []
    sent = []
    queue = list(chunks)

    async def receive():
        if queue:
            body = queue.pop(0)
            pulled.append(len(body))
            return {'type': 'http.request', 'body': body, 'more_body': bool(queue)}
        await asyncio.sleep(3600)

    async def send(message):
        sent.append(message)
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': 'POST', 'scheme': 'http',
             'path': path, 'raw_path': path.encode(), 'query_string': b'', 'root_path': '',
             'headers': [(k.lower().encode(), v.encode()) for k, v in headers_list],
             'client': ('127.0.0.1', 1), 'server': ('testserver', 80)}
    asyncio.run(asyncio.wait_for(app(scope, receive, send), 10))
    start = next(m for m in sent if m['type'] == 'http.response.start')
    body = b''.join(m.get('body', b'') for m in sent if m['type'] == 'http.response.body')
    hdrs = {k.decode(): v.decode() for k, v in start['headers']}
    return start['status'], hdrs, body, pulled


def test_oversize_body_413_without_reading(server):
    client, backend, *_ = server()
    h = list(headers().items())
    status, hdrs, body, pulled = _raw_call(client.app, P.INFER_PATH, h + [('content-length', str(P.MAX_FRAME_BYTES + 1))],
                                           [b'x' * 1024] * 4)
    assert status == 413 and pulled == []                               # refused on the header alone
    assert hdrs['content-type'] == P.CONTENT_TYPE and P.parse_error(*P.decode(body))[0] == 'bad_frame'
    # A chunked body is read to the cap + 1 and refused beyond it.
    chunk = b'x' * 65536
    n = P.MAX_FRAME_BYTES // len(chunk) + 4
    status, hdrs, body, pulled = _raw_call(client.app, P.INFER_PATH, h, [chunk] * n)
    assert status == 413 and sum(pulled) <= P.MAX_FRAME_BYTES + len(chunk) and len(pulled) < n
    status, *_ = _raw_call(client.app, P.HELLO_PATH, h + [('content-length', 'zwölf')], [b''])
    assert status == 400
    assert backend.calls == []


# ---------------------------------------------------------------- the background session check (D42, J-5)

def test_ttl_calls_heartbeat_with_stats(server):
    clock = FakeClock()
    client, backend, *_ = server(clock=clock)
    with client:
        _hello(client)
        _infer(client, 1, 0)
        clock.advance(9.9)
        _infer(client, 2, 5)
        assert backend.heartbeats == 0
        clock.advance(0.1)
        _infer(client, 3, 10)
        assert _wait(lambda: backend.heartbeats == 1)
        hb = [c for c in backend.calls if c[0] == 'heartbeat'][0]
        assert hb[1:4] == (TOKEN, SESSION_ID, CONN) and hb[4]['chunks'] == 2
        _infer(client, 4, 15)
        assert backend.heartbeats == 1                                     # checked_at moved on
        clock.advance(10)
        _infer(client, 5, 20)
        assert _wait(lambda: backend.heartbeats == 2)


@pytest.mark.parametrize('reason', ['expired', 'superseded', 'client_gone', 'user_stop'])
def test_heartbeat_ended_answers_410_with_the_reason_and_evicts(server, reason):
    clock = FakeClock()
    client, backend, *_ = server(clock=clock)

    async def ended(n):
        raise SB.SessionDenied('ended', reason)
    backend.heartbeat_behaviour = ended
    with client:
        _hello(client)
        clock.advance(10)
        assert _infer(client).status_code == 200                          # the check never delays this one
        assert _wait(lambda: backend.heartbeats == 1)
        r = _infer(client, 2, 5)
        assert r.status_code == 410 and _err(r) == ('ended', False, None, reason, None)
        backend.connect_result = SB.SessionDenied('ended', reason)
        r = _infer(client, 3, 10)                                          # evicted: the next one reconnects
        assert backend.names()[-1] == 'connect' and r.status_code == 410


def _slow_check(release, entered, seconds=5.0):
    """A check that takes ``seconds`` (or until the test releases it)."""
    async def behaviour(n):
        entered.set()
        end = time.monotonic() + seconds
        while not release.is_set() and time.monotonic() < end:
            await asyncio.sleep(0.01)
        return 600.0
    return behaviour


def test_heartbeat_never_delays_an_observation(server):
    clock = FakeClock()
    client, backend, *_ = server(clock=clock)
    release, entered = threading.Event(), threading.Event()
    backend.heartbeat_behaviour = _slow_check(release, entered)
    try:
        with client:
            _hello(client)
            clock.advance(10)
            t0 = time.monotonic()
            r = _infer(client)
            assert r.status_code == 200
            assert entered.wait(2)
            for i in range(5):                                              # the check is still running ("5 s")
                assert _infer(client, 2 + i, 5 * (i + 1)).status_code == 200
            assert time.monotonic() - t0 < 2.0 and not release.is_set()
            release.set()
    finally:
        release.set()


def test_heartbeat_single_flight(server):
    clock = FakeClock()
    client, backend, *_ = server(clock=clock)
    release, entered = threading.Event(), threading.Event()
    backend.heartbeat_behaviour = _slow_check(release, entered)
    try:
        with client:
            _hello(client)
            clock.advance(10)
            results = []

            def one(i):
                results.append(_infer(client, i, i).status_code)
            threads = [threading.Thread(target=one, args=(i,)) for i in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)
            assert results.count(200) + results.count(429) == 10
            assert entered.wait(2)
            assert backend.heartbeats == 1
            release.set()
    finally:
        release.set()


def test_short_backend_blip_keeps_the_session(server):
    clock = FakeClock()
    client, backend, *_ = server(clock=clock)

    async def blip(n):
        if n == 1:
            raise SB.SessionDenied('unavailable')
        return 600.0
    backend.heartbeat_behaviour = blip
    with client:
        _hello(client)
        clock.advance(10)
        assert _infer(client, 1, 0).status_code == 200
        assert _wait(lambda: backend.heartbeats == 1)
        clock.advance(1.0)
        assert _infer(client, 2, 5).status_code == 200
        time.sleep(0.05)
        assert backend.heartbeats == 1                                     # the retry waits >= 2 s
        clock.advance(1.0)
        assert _infer(client, 3, 10).status_code == 200
        assert _wait(lambda: backend.heartbeats == 2)
        for i in range(40):                                                # 40 s more of healthy checks
            clock.advance(1.0)
            assert _infer(client, 4 + i, 15 + 5 * i).status_code == 200


def test_30_s_without_a_check_fails_closed(server):
    """Every check fails: 503 'unavailable' exactly HEARTBEAT_FAIL_CLOSED_S after the FIRST failure of the
    run (J-5), the retries >= HEARTBEAT_RETRY_S apart."""
    clock = FakeClock()
    client, backend, *_ = server(clock=clock)
    attempts = []

    async def down(n):
        attempts.append(clock())
        raise RuntimeError('PostgREST down')
    backend.heartbeat_behaviour = down
    with client:
        _hello(client)
        clock.advance(10)
        seq = 0
        status = 200
        while status == 200 and seq < 400:
            seq += 1
            r = _infer(client, seq, 5 * seq)
            status = r.status_code
            time.sleep(0.02)                     # let a check the request started run on the server's loop
            if status == 200:
                clock.advance(0.25)
        assert status == 503 and _err(r) == ('unavailable', False, None, None, None)
        assert attempts[0] == pytest.approx(1010.0)                 # the first check, at the TTL
        assert clock() == pytest.approx(attempts[0] + S.HEARTBEAT_FAIL_CLOSED_S)
        gaps = [b - a for a, b in zip(attempts, attempts[1:])]
        assert gaps and min(gaps) >= S.HEARTBEAT_RETRY_S
        assert len(attempts) >= 10
        # Evicted: the next request reconnects and is served again.
        assert _infer(client, seq + 1, 5 * seq + 5).status_code == 200
        assert backend.names()[-1] in ('connect', 'heartbeat')
    assert (S.HEARTBEAT_FAIL_CLOSED_S, S.HEARTBEAT_RETRY_S, S.HTTPS_CACHE_TTL_S) == (30.0, 2.0, 10.0)


@pytest.mark.parametrize('code', ['replaced', 'auth'])
def test_denied_heartbeat_answers_on_the_next_request(server, code):
    clock = FakeClock()
    client, backend, *_ = server(clock=clock)

    async def denied(n):
        raise SB.SessionDenied(code)
    backend.heartbeat_behaviour = denied
    with client:
        _hello(client)
        clock.advance(10)
        assert _infer(client, 1, 0).status_code == 200
        assert _wait(lambda: backend.heartbeats == 1)
        r = _infer(client, 2, 5)
        assert r.status_code == {'replaced': 409, 'auth': 401}[code] and _err(r)[0] == code


def test_idle_entry_starts_a_check_instead_of_failing_closed(server):
    """H-2: an entry idle for far longer than the fail-closed bound serves and starts ONE check."""
    clock = FakeClock()
    client, backend, *_ = server(clock=clock)
    with client:
        _hello(client)
        clock.advance(2 * S.HEARTBEAT_FAIL_CLOSED_S + 40)
        assert _infer(client).status_code == 200
        assert _wait(lambda: backend.heartbeats == 1)
        assert _infer(client, 2, 5).status_code == 200
        assert backend.heartbeats == 1


def test_idle_entry_then_one_failed_check_keeps_the_run(server):
    """J-5: the fail-closed clock is the length of the CURRENT run of failed checks, not the time since the
    last success: an entry idle for 100 s whose first check fails keeps serving and retries."""
    clock = FakeClock()
    client, backend, *_ = server(clock=clock)

    async def once(n):
        if n == 1:
            raise SB.SessionDenied('unavailable')
        return 600.0
    backend.heartbeat_behaviour = once
    with client:
        _hello(client)
        clock.advance(100.0)
        assert _infer(client, 1, 0).status_code == 200
        assert _wait(lambda: backend.heartbeats == 1)
        clock.advance(S.HEARTBEAT_RETRY_S)
        r = _infer(client, 2, 5)
        assert r.status_code == 200, _err(r)
        assert _wait(lambda: backend.heartbeats == 2)
        clock.advance(S.HEARTBEAT_FAIL_CLOSED_S)
        assert _infer(client, 3, 10).status_code == 200


def test_unexpected_check_exception_is_a_transport_failure(server):
    clock = FakeClock()
    client, backend, *_ = server(clock=clock)

    async def weird(n):
        if n == 1:
            raise KeyError('max_end_in_s')
        return 600.0
    backend.heartbeat_behaviour = weird
    with client:
        _hello(client)
        clock.advance(10)
        assert _infer(client, 1, 0).status_code == 200
        assert _wait(lambda: backend.heartbeats == 1)
        clock.advance(2)
        assert _infer(client, 2, 5).status_code == 200
        assert _wait(lambda: backend.heartbeats == 2)
        clock.advance(1)
        assert _infer(client, 3, 10).status_code == 200


def test_blip_through_the_real_supabase_backend(server):
    """H-3 end to end: PostgREST answers one heartbeat with HTTP 503; the real backend raises
    SessionDenied('unavailable'), which must not end the run."""
    rpc = []

    def handler(request):
        name = request.url.path.rsplit('/', 1)[-1]
        rpc.append(name)
        if name == 'inference_session_connect':
            return httpx.Response(200, json={'status': 'active', 'session_id': 'row', 'policy_type': 'act',
                                             'model_repo': 'EduBotics-Solutions/act-test', 'model_revision': 'a' * 40,
                                             'max_end_in_s': 600})
        if name == 'inference_session_heartbeat' and rpc.count(name) == 1:
            return httpx.Response(503, text='upstream')
        return httpx.Response(200, json={'status': 'active', 'max_end_in_s': 590})
    backend = SB.SupabaseSessionBackend('https://db.test', 'anon', transport=httpx.MockTransport(handler))
    clock = FakeClock()
    client, *_ = server(backend=backend, clock=clock)
    with client:
        assert _hello(client).status_code == 200
        clock.advance(10)
        assert _infer(client, 1, 0).status_code == 200
        assert _wait(lambda: rpc.count('inference_session_heartbeat') == 1)
        clock.advance(2)
        assert _infer(client, 2, 5).status_code == 200
        assert _wait(lambda: rpc.count('inference_session_heartbeat') == 2)
        clock.advance(S.HEARTBEAT_FAIL_CLOSED_S)
        assert _infer(client, 3, 10).status_code == 200
    # Without a Modal-stamped session id the real backend refuses before any RPC.
    rpc.clear()
    client, *_ = server(backend=backend)
    with client:
        r = _hello(client, session=None)
        assert r.status_code == 401 and _err(r)[0] == 'auth' and rpc == []


# ---------------------------------------------------------------- rate, internal, logs

def test_rate_and_internal_keep_the_session(server):
    clock = FakeClock()
    engine = FakeEngine()
    client, backend, *_ = server(clock=clock, engine=engine)
    with client:
        _hello(client)
        codes = [_infer(client, i, i).status_code for i in range(1, 10)]
        assert codes == [200] * 8 + [429]
        r = _infer(client, 10, 10)
        assert r.status_code == 429 and _err(r) == ('rate', True, 10, None, None)
        clock.advance(1.0)
        assert _infer(client, 11, 11).status_code == 200
        engine.fail = RuntimeError('CUDA error: an illegal memory access')
        r = _infer(client, 12, 12)
        assert r.status_code == 500 and _err(r) == ('internal', True, 12, None, None)
        engine.fail = None
        assert _infer(client, 13, 13).status_code == 200
    assert backend.names() == ['connect']


def test_no_token_in_any_log_record(server, caplog):
    caplog.set_level(logging.DEBUG)
    clock = FakeClock()
    engine = FakeEngine()
    import chunk_engine
    calls = {'n': 0}

    def loader(repo, rev, dev):
        calls['n'] += 1
        if calls['n'] == 1:
            raise chunk_engine.ModelRefused('model', f'token {TOKEN}')
        return engine
    client, backend, *_ = server(clock=clock, engine=engine, loader=loader)

    async def flaky(n):
        raise RuntimeError(f'failed for {TOKEN}')
    backend.heartbeat_behaviour = flaky
    edge = 'edge-token-secret-value-42'
    with client:
        _hello(client, edge=edge)
        _hello(client, edge=edge)
        clock.advance(10)
        _infer(client, edge=edge)
        engine.fail = RuntimeError(f'boom {TOKEN}')
        _infer(client, 2, 2, edge=edge)
        client.post(P.INFER_PATH, content=b'junk', headers=headers(edge=edge))
        client.post(P.BYE_PATH, content=P.build_bye('stall'), headers=headers(edge=edge))
        time.sleep(0.1)
    text = '\n'.join(f'{r.name} {r.getMessage()} {r.args} {r.exc_text or ""}' for r in caplog.records)
    assert TOKEN not in text and edge not in text and bearer(TOKEN) not in text
    assert any(r.name.startswith('edubotics.inference') for r in caplog.records)
    assert all(r.exc_info is None for r in caplog.records if r.name.startswith('edubotics.inference'))

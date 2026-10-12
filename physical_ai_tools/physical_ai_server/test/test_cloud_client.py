"""The robot's Cloud-Inferenz HTTPS client (spec §5.6, O7', D24, D30, D38, D43, D44, F-11, H-1, J-1, J-2).

Every test runs the REAL client (its own thread + asyncio loop) against an in-process
``aiohttp.web`` fake on ``http://127.0.0.1`` (``cloud_protocol.url`` allows http:// for tests). The
fake records every request: path, headers, body, and the client's TCP source port, so "one kept-alive
connection" is a fact the server saw, not an inference.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import threading
import time
from pathlib import Path

import numpy as np
import pytest

aiohttp = pytest.importorskip('aiohttp')
from aiohttp import web  # noqa: E402

from physical_ai_server.inference import cloud_client as cc  # noqa: E402
from physical_ai_server.inference import cloud_protocol as cp  # noqa: E402
from physical_ai_server.inference import cloud_schedule as cs  # noqa: E402

TOK = 'SyntheticConnectTokenForTestsOnly_000000001'         # 43 chars, TOKEN_RE
EDGE = 'synthetic-edge-token.for.tests~only!0001'           # EDGE_TOKEN_RE
CONN = '0123456789abcdef0123456789abcdef'
HELLO = {'robot_type': 'omx_f', 'robot_profile': 'omx_full',
         'cameras': [('gripper', 640, 480), ('scene', 640, 480)], 'state_dim': 6, 'fps': 30.0}
POLICY = {'type': 'act', 'expected_cameras': ['gripper', 'scene'],
          'image_shapes': {'gripper': [3, 480, 640], 'scene': [3, 480, 640]}, 'trained_fps': 30.0,
          'chunk_size': 100, 'n_action_steps': 100, 'state_dim': 6, 'action_dim': 6, 'requires_task': False}
LIMITS = {'cadence_steps': 5, 'actions_per_chunk': 50, 'aggregate_new_weight': 0.7, 'max_obs_per_s': 8.0,
          'stall_end_s': 15.0, 'resend_after_s': 2.0, 'max_end_in_s': 3300}
OCTET = 'application/octet-stream'
CAMS = {'gripper': b'\xff\xd8gripper-jpeg', 'scene': b'\xff\xd8scene-jpeg'}
STATE = np.zeros(6, np.float32)

assert cp.TOKEN_RE.fullmatch(TOK) and cp.EDGE_TOKEN_RE.fullmatch(EDGE) and cp.CONN_ID_RE.fullmatch(CONN)


def frame(code, status, **kw):
    return web.Response(status=status, body=cp.build_error(code, **kw), content_type=OCTET)


def ready_response():
    return web.Response(body=cp.build_ready(server_sha='test', policy=POLICY, limits=LIMITS), content_type=OCTET)


def chunk_for(seq, obs_e, n=50):
    a = (np.arange(n * 6, dtype=np.float32).reshape(n, 6) + obs_e).astype(np.float32)
    return web.Response(body=cp.build_chunk(seq, obs_e, a, gpu_ms=12.5), content_type=OCTET)


class Fake:
    """aiohttp.web on its own thread. ``hello``/``infer``/``bye`` are lists of async handlers
    ``(request, body) -> Response``, used one per request (the last one repeats)."""

    def __init__(self):
        self.requests = []
        self.hello = [lambda r, b: ready_response()]
        self.infer = [self.default_infer]
        self.bye = [lambda r, b: web.Response(status=204)]
        self.other = []
        self._counts = {'hello': 0, 'infer': 0, 'bye': 0}
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(10)

    @staticmethod
    def default_infer(request, body):
        h, p = cp.decode(body)
        seq, obs_e, _, _, _ = cp.parse_obs(h, p)
        return chunk_for(seq, obs_e)

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        app = web.Application()
        for name in ('hello', 'infer', 'bye'):
            app.router.add_post(f'/v1/{name}', self._make(name))
        app.router.add_route('*', '/{tail:.*}', self._other)
        self.runner = web.AppRunner(app, access_log=None, shutdown_timeout=1.0)
        self.loop.run_until_complete(self.runner.setup())
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        self.loop.run_until_complete(site.start())
        self.port = site._server.sockets[0].getsockname()[1]
        self.base_url = f'http://127.0.0.1:{self.port}'
        self._ready.set()
        self.loop.run_forever()

    def _make(self, name):
        async def handler(request):
            body = await request.read()
            peer = request.transport.get_extra_info('peername') if request.transport else None
            self.requests.append({'path': request.path, 'url': str(request.url), 'headers': dict(request.headers),
                                  'body': body, 'port': peer[1] if peer else None, 't': time.monotonic()})
            script = getattr(self, name)
            i = min(self._counts[name], len(script) - 1)
            self._counts[name] += 1
            out = script[i](request, body)
            if asyncio.iscoroutine(out):
                out = await out
            return out
        return handler

    async def _other(self, request):
        self.other.append(request.path)
        return web.Response(status=200, body=cp.build_ready(server_sha='x', policy=POLICY, limits=LIMITS),
                            content_type=OCTET)

    def ports(self):
        return {r['port'] for r in self.requests}

    def stop(self):
        async def _down():
            await self.runner.cleanup()
        asyncio.run_coroutine_threadsafe(_down(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(5)
        self.loop.close()


def delayed(seconds, inner):
    async def handler(request, body):
        await asyncio.sleep(seconds)
        return inner(request, body)
    return handler


def drop(request, body):
    request.transport.close()
    return web.Response(status=200)


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.stop()


@pytest.fixture
def clients():
    made = []

    def make(base_url, **kw):
        c = cc.CloudClient(token=TOK, edge_token=EDGE, conn_id=CONN, hello=dict(HELLO), base_url=base_url, **kw)
        made.append(c)
        return c
    yield make
    for c in made:
        c.close(None)
        c.join(5)


def wait_for(pred, timeout=10.0, step=0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    raise AssertionError('timed out waiting')


def ready_client(fake, clients):
    c = clients(fake.base_url)
    c.start()
    wait_for(lambda: c.snapshot().ready is not None)
    return c


def obs(seq, obs_e=0):
    return cp.build_obs(seq, obs_e, CAMS, STATE)


def take_until(c, n, timeout=10.0):
    got = []

    def _have():
        got.extend(c.take_chunks())
        return len(got) >= n
    wait_for(_have, timeout)
    return got


# ── round trip and auth ─────────────────────────────────────────────────────

def test_hello_ready_infer_chunk_bye(fake, clients):
    c = ready_client(fake, clients)
    snap = c.snapshot()
    assert snap.phase == 'ready' and snap.end is None
    policy, limits = snap.ready
    assert policy['expected_cameras'] == ['gripper', 'scene'] and limits['cadence_steps'] == 5
    c.offer(obs(0, 0), 0, 0)
    (seq, obs_e, actions), = take_until(c, 1)
    assert (seq, obs_e) == (0, 0) and actions.dtype == np.float32 and actions.shape == (50, 6)
    c.offer(obs(1, 5), 1, 5)
    (seq, obs_e, actions), = take_until(c, 1)
    assert (seq, obs_e) == (1, 5) and float(actions[0, 0]) == 5.0
    snap = c.snapshot()
    assert snap.rtt_ms_p50 is not None and snap.gpu_ms_p50 == 12.5 and snap.failures == 0
    c.close('robot_stop', {'executed': 7})
    c.join(5)
    byes = [r for r in fake.requests if r['path'] == '/v1/bye']
    assert len(byes) == 1
    assert cp.parse_bye(*cp.decode(byes[0]['body'])) == ('robot_stop', {'executed': 7})
    hello = cp.decode([r for r in fake.requests if r['path'] == '/v1/hello'][0]['body'])
    assert cp.parse_hello(*hello) == {'v': 1, 'conn_id': CONN}
    assert hello[0]['cameras'] == [{'name': 'gripper', 'w': 640, 'h': 480}, {'name': 'scene', 'w': 640, 'h': 480}]


def test_both_tokens_only_as_headers_on_every_request(fake, clients):
    c = ready_client(fake, clients)
    c.offer(obs(0), 0, 0)
    take_until(c, 1)
    c.close('robot_stop')
    c.join(5)
    paths = [r['path'] for r in fake.requests]
    assert paths == ['/v1/hello', '/v1/infer', '/v1/bye']
    for r in fake.requests:
        h = {k.lower(): v for k, v in r['headers'].items()}
        assert h[cp.EDGE_AUTH_HEADER.lower()] == f'Bearer {EDGE}'
        assert h[cp.CONNECT_AUTH_HEADER.lower()] == f'Bearer {TOK}'
        assert h['content-type'] == OCTET
        assert h['user-agent'] == 'edubotics-robot/1'
        assert TOK not in r['url'] and EDGE not in r['url']
        assert TOK.encode() not in r['body'] and EDGE.encode() not in r['body']
        if r['path'] != '/v1/hello':
            assert h[cp.CONN_HEADER.lower()] == CONN
    assert TOK not in repr(c) and EDGE not in repr(c)
    assert TOK not in repr(c.snapshot()) and EDGE not in repr(c.snapshot())


# ── frameless answers (D38, F-11) ───────────────────────────────────────────

def test_no_redirect_followed_and_3xx_counts_cloud_down(fake, clients):
    fake.hello = [lambda r, b: web.Response(status=302, headers={'Location': '/elsewhere'})]
    c = clients(fake.base_url)
    c.start()
    end = wait_for(lambda: c.snapshot().end, timeout=15)
    assert (end.kind, end.code) == ('robot', 'cloud_unavailable')
    assert fake.other == []                                 # the Location was never asked
    assert [r['path'] for r in fake.requests] == ['/v1/hello'] * 3


def test_200_html_counts_cloud_down(fake, clients):
    fake.hello = [lambda r, b: web.Response(status=200, text='<html>Anmelden</html>', content_type='text/html')]
    c = clients(fake.base_url)
    c.start()
    end = wait_for(lambda: c.snapshot().end, timeout=15)
    assert (end.kind, end.code) == ('robot', 'cloud_unavailable')
    # and after ready, a captive-portal page on /v1/infer is a request without a chunk
    fake2 = Fake()
    try:
        fake2.infer = [lambda r, b: web.Response(status=200, text='<html/>', content_type='text/html')]
        c2 = ready_client(fake2, clients)
        c2.offer(obs(0), 0, 0)
        assert take_until(c2, 1) == [(0, 0, None)]
        assert c2.snapshot().end is None and c2.snapshot().failures == 1
    finally:
        fake2.stop()


@pytest.mark.parametrize('status', [401, 404])
def test_frameless_401_or_404_on_hello_ends_session_gone_at_once(fake, clients, status):
    fake.hello = [lambda r, b: web.Response(status=status, text='no such session')]
    c = clients(fake.base_url)
    c.start()
    end = wait_for(lambda: c.snapshot().end)
    assert end == cc.EndInfo('robot', 'session_gone', None, None)
    assert len(fake.requests) == 1                          # no second attempt
    assert c.snapshot().phase == 'ended'


@pytest.mark.parametrize('status', [401, 404])
def test_frameless_401_or_404_on_infer_after_ready_ends_session_gone(fake, clients, status):
    fake.infer = [lambda r, b: web.Response(status=status)]
    c = ready_client(fake, clients)
    c.offer(obs(0), 0, 0)
    end = wait_for(lambda: c.snapshot().end)
    assert end == cc.EndInfo('robot', 'session_gone', 'after_ready', None)


def test_frameless_403_429_5xx_are_transient(fake, clients):
    statuses = [403, 429, 500, 502, 503, 307]
    fake.infer = [lambda r, b, s=s: web.Response(status=s, text='nope') for s in statuses] + [Fake.default_infer]
    c = ready_client(fake, clients)
    for i, _ in enumerate(statuses):
        c.offer(obs(i), i, 0)
        assert take_until(c, 1) == [(i, 0, None)]
    c.offer(obs(9), 9, 0)
    (seq, _, actions), = take_until(c, 1)
    assert seq == 9 and actions is not None
    snap = c.snapshot()
    assert snap.end is None and snap.failures == len(statuses)


def test_hello_attempts_unreachable_vs_cloud_down(clients):
    # a closed port: three refused connects -> unreachable
    import socket
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    c = clients(f'http://127.0.0.1:{port}')
    c.start()
    end = wait_for(lambda: c.snapshot().end, timeout=15)
    assert (end.kind, end.code) == ('robot', 'unreachable')
    # mixed: the tie rule is unreachable >= cloud_down
    for script, expected in (
            ([drop, lambda r, b: web.Response(status=503), lambda r, b: web.Response(status=503)],
             'cloud_unavailable'),
            ([drop, drop, lambda r, b: web.Response(status=503)], 'unreachable')):
        f = Fake()
        try:
            f.hello = script
            c = clients(f.base_url)
            c.start()
            end = wait_for(lambda: c.snapshot().end, timeout=15)
            assert (end.kind, end.code) == ('robot', expected), script
            assert [r['path'] for r in f.requests] == ['/v1/hello'] * 3
        finally:
            f.stop()


def test_hello_error_frame_ends_with_its_code_reason_and_server_v(fake, clients):
    fake.hello = [lambda r, b: frame('version', 400, server_v=[2])]
    c = clients(fake.base_url)
    c.start()
    end = wait_for(lambda: c.snapshot().end)
    assert end == cc.EndInfo('server', 'version', None, (2,))
    assert len(fake.requests) == 1


def test_a_200_octet_stream_that_is_not_a_frame_ends_internal(fake, clients):
    fake.hello = [lambda r, b: web.Response(body=b'\x00\x00\x00\x00garbage', content_type=OCTET)]
    c = clients(fake.base_url)
    c.start()
    assert wait_for(lambda: c.snapshot().end) == cc.EndInfo('robot', 'internal', None, None)


# ── M1 / D43 / D44: a request is never cancelled; every end without a chunk is reported ──

def test_every_end_without_a_chunk_is_reported_as_none(fake, clients):
    fake.infer = [lambda r, b: frame('rate', 429), drop,
                  lambda r, b: web.Response(status=302, headers={'Location': '/x'}),
                  lambda r, b: web.Response(status=500), delayed(5.0, Fake.default_infer),
                  Fake.default_infer]
    c = ready_client(fake, clients)
    c.INFER_HTTP_TIMEOUT_S = 0.6            # the 8 s value has its own test below
    for i in range(5):
        c.offer(obs(i), i, 0)
        assert take_until(c, 1, timeout=5) == [(i, 0, None)], i
    c.offer(obs(5), 5, 0)
    assert take_until(c, 1)[0][2] is not None
    assert c.snapshot().end is None and fake.other == []


def test_infer_timeout_is_8_s_connect_included(fake, clients):
    assert cc.CloudClient.INFER_HTTP_TIMEOUT_S == 8.0
    fake.infer = [delayed(10.0, Fake.default_infer)]
    c = ready_client(fake, clients)
    t0 = time.monotonic()
    c.offer(obs(0), 0, 0)
    assert take_until(c, 1, timeout=12) == [(0, 0, None)]
    elapsed = time.monotonic() - t0
    assert 8.0 <= elapsed < 8.3, elapsed      # J-1: ceil_threshold above the timeout -> never late


class _Blackhole(aiohttp.TCPConnector):
    """A silently dropping firewall: every NEW connection hangs (no RST, no SYN-ACK)."""

    allow = 0

    async def _create_connection(self, req, traces, timeout):
        if _Blackhole.allow > 0:
            _Blackhole.allow -= 1
            return await super()._create_connection(req, traces, timeout)
        await asyncio.sleep(3600)


def test_infer_connect_is_bounded_by_the_request_timeout(fake, clients, monkeypatch):
    # the ready answer closes its connection, so the infer needs a NEW one -> the blackhole
    def ready_close(r, b):
        resp = ready_response()
        resp.force_close()
        return resp
    fake.hello = [ready_close]
    _Blackhole.allow = 1
    monkeypatch.setattr(cc.CloudClient, '_make_connector', lambda self: _Blackhole(limit=2))
    c = ready_client(fake, clients)
    c.INFER_HTTP_TIMEOUT_S = 1.0            # connect included: not the 10 s CONNECT_TIMEOUT_S
    t0 = time.monotonic()
    c.offer(obs(0), 0, 0)
    assert take_until(c, 1, timeout=5) == [(0, 0, None)]
    assert 1.0 <= time.monotonic() - t0 < 1.3


def test_hello_connect_is_bounded_by_connect_timeout(fake, clients, monkeypatch):
    # J-1: a per-request timeout REPLACES the session's connect limit; the hello's must carry it.
    _Blackhole.allow = 0
    monkeypatch.setattr(cc.CloudClient, '_make_connector', lambda self: _Blackhole(limit=2))
    c = clients(fake.base_url)
    c.CONNECT_TIMEOUT_S = 0.4
    t0 = time.monotonic()
    c.start()
    end = wait_for(lambda: c.snapshot().end, timeout=10)
    elapsed = time.monotonic() - t0
    assert (end.kind, end.code) == ('robot', 'unreachable')
    # 3 attempts × 0.4 s + the 0.5 s and 1.0 s backoffs, far below the 90 s hello timeout
    assert 2.6 <= elapsed < 4.0, elapsed
    assert fake.requests == []


def test_the_timeout_helper(monkeypatch):
    c = cc.CloudClient(token=TOK, edge_token=EDGE, conn_id=CONN, hello=dict(HELLO), base_url='http://x')
    hello = c._hello_timeout()
    infer = c._infer_timeout()
    assert (hello.total, hello.connect, hello.ceil_threshold) == (90.0, 10.0, 91.0)
    assert (infer.total, infer.connect, infer.ceil_threshold) == (8.0, None, 9.0)
    assert c._bye_timeout().total == 1.0 and c._bye_timeout().ceil_threshold == 2.0


def test_client_never_cancels_a_request():
    source = Path(inspect.getsourcefile(cc)).read_text(encoding='utf-8')
    assert 'abandon' not in source
    tree = ast.parse(source)
    allowed = {'_shutdown_tasks'}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for n in ast.walk(fn):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == 'cancel':
                    assert fn.name in allowed, f'{fn.name} cancels'
    assert not hasattr(cc.CloudClient, 'abandon')


def _drive(c, seconds, fps=30.0):
    """The node's tick, in miniature: the REAL schedule at a 30 Hz real-time tick."""
    sched, adopted_seqs, actions = None, [], 0
    t_end = time.monotonic() + seconds
    nxt = time.monotonic()
    while time.monotonic() < t_end:
        now = time.monotonic()
        snap = c.snapshot()
        assert snap.end is None, snap.end
        if sched is None and snap.ready is not None:
            policy, limits = snap.ready
            lim = cs.clamp_limits(limits)
            sched = cs.Schedule(lim, lim.actions_per_chunk, policy['action_dim'], start=now)
        if sched is not None:
            chunks = c.take_chunks()
            before = sched.counters['adopted']
            r = sched.step(now, chunks, True)
            assert r.end is None, r.end
            if sched.counters['adopted'] > before:
                adopted_seqs.append([s for s, _, a in chunks if a is not None][-1])
            if r.ticket is not None:
                c.offer(obs(r.ticket.seq, r.ticket.obs_e), r.ticket.seq, r.ticket.obs_e)
            if r.action is not None:
                actions += 1
        nxt += 1.0 / fps
        time.sleep(max(0.0, nxt - time.monotonic()))
    return sched, adopted_seqs, actions


def test_cold_first_request_is_not_cancelled_and_the_run_starts(fake, clients):
    # J-2 (H-1): the first observation on a cold window answers after 2.3 s, the rest after 0.8 s.
    fake.infer = [delayed(2.3, Fake.default_infer), delayed(0.8, Fake.default_infer)]
    c = ready_client(fake, clients)
    sched, adopted_seqs, actions = _drive(c, 5.0)
    assert sched.counters['overdue'] >= 1                # seq 0 marked stale at 2 s ...
    assert sched.counters['stale'] >= 1                  # ... and its late chunk dropped (Q-D)
    assert adopted_seqs and adopted_seqs[0] == 1         # seq 1 is the first plan adopted
    assert 0 not in adopted_seqs
    assert actions > 30
    assert len(fake.ports()) == 1                        # ONE connection: nothing was cancelled


def test_hold_keeps_the_connection(fake, clients):
    # D43: a 1 s round trip > 0.83 s -> the queue runs dry, the pre-hold request stays on the wire
    fake.infer = [delayed(1.0, Fake.default_infer)]
    c = ready_client(fake, clients)
    sched, adopted_seqs, actions = _drive(c, 6.0)
    assert sched.counters['holds'] >= 1
    assert len(adopted_seqs) >= 2
    assert len(fake.ports()) == 1


def test_one_kept_alive_connection(fake, clients):
    c = ready_client(fake, clients)
    for i in range(20):
        c.offer(obs(i, i * 5), i, i * 5)
        (seq, obs_e, a), = take_until(c, 1)
        assert (seq, obs_e) == (i, i * 5) and a is not None
    assert len([r for r in fake.requests if r['path'] == '/v1/infer']) == 20
    assert len(fake.ports()) == 1


def test_a_chunk_echoing_another_request_frees_the_slot(fake, clients):
    fake.infer = [lambda r, b: chunk_for(99, 0), Fake.default_infer]
    c = ready_client(fake, clients)
    c.offer(obs(0), 0, 0)
    assert take_until(c, 1) == [(0, 0, None)]
    assert c.snapshot().end is None


# ── error frames ────────────────────────────────────────────────────────────

@pytest.mark.parametrize('code,status', [('rate', 429), ('internal', 500)])
def test_rate_and_internal_report_none(fake, clients, code, status):
    fake.infer = [lambda r, b: frame(code, status, seq=0), Fake.default_infer]
    c = ready_client(fake, clients)
    c.offer(obs(0), 0, 0)
    assert take_until(c, 1) == [(0, 0, None)]
    assert c.snapshot().end is None
    c.offer(obs(1), 1, 0)
    assert take_until(c, 1)[0][2] is not None


def test_fatal_error_frame_ends(fake, clients):
    fake.infer = [lambda r, b: frame('cameras', 422)]
    c = ready_client(fake, clients)
    c.offer(obs(0), 0, 0)
    assert wait_for(lambda: c.snapshot().end) == cc.EndInfo('server', 'cameras', None, None)
    fake2 = Fake()
    try:
        fake2.infer = [lambda r, b: frame('ended', 410, reason='expired')]
        c2 = ready_client(fake2, clients)
        c2.offer(obs(0), 0, 0)
        assert wait_for(lambda: c2.snapshot().end) == cc.EndInfo('server', 'ended', 'expired', None)
        assert c2.take_chunks() == []
    finally:
        fake2.stop()


def test_close_sends_bye_with_reason(fake, clients):
    c = ready_client(fake, clients)
    c.close('stall', {'executed': 10, 'holds': 2})
    c.join(5)
    (bye,) = [r for r in fake.requests if r['path'] == '/v1/bye']
    assert cp.parse_bye(*cp.decode(bye['body'])) == ('stall', {'executed': 10, 'holds': 2})
    assert bye['headers'][cp.CONN_HEADER] == CONN
    c2 = ready_client(fake, clients)
    n = len(fake.requests)
    c2.close(None)
    c2.join(5)
    assert len(fake.requests) == n                         # the server ended it: no bye
    assert not c2._thread.is_alive()


def test_close_during_a_slow_hello_returns_promptly(fake, clients):
    fake.hello = [delayed(30.0, lambda r, b: ready_response())]
    c = clients(fake.base_url)
    c.start()
    wait_for(lambda: fake.requests, timeout=5)            # the hello is on the wire
    t0 = time.monotonic()
    c.close('robot_stop')
    c.join(5)
    assert not c._thread.is_alive() and time.monotonic() - t0 < 2.5


def test_an_oversized_bye_stats_is_dropped_not_the_bye(fake, clients):
    c = ready_client(fake, clients)
    c.close('robot_stop', {'x': 'y' * 2000})
    c.join(5)
    (bye,) = [r for r in fake.requests if r['path'] == '/v1/bye']
    assert cp.parse_bye(*cp.decode(bye['body'])) == ('robot_stop', None)


# ── D30 / R-9: no token in any log record ───────────────────────────────────

def test_no_token_in_logs_on_https_503_and_401(fake, clients, caplog):
    caplog.set_level(logging.DEBUG)
    fake.hello = [lambda r, b: web.Response(status=503, text='down'), lambda r, b: ready_response()]
    fake.infer = [lambda r, b: web.Response(status=503, text='down'), drop,
                  lambda r, b: web.Response(status=401, text='gone')]
    c = clients(fake.base_url)
    c.start()
    wait_for(lambda: c.snapshot().ready is not None, timeout=10)
    c.offer(obs(0), 0, 0)
    take_until(c, 1)
    c.offer(obs(1), 1, 0)
    take_until(c, 1)
    c.offer(obs(2), 2, 0)
    wait_for(lambda: c.snapshot().end)
    c.close(None)
    c.join(5)
    assert caplog.records, 'the client logged nothing at all'
    for rec in caplog.records:
        text = rec.getMessage() + ' ' + logging.Formatter().format(rec)
        assert TOK not in text and EDGE not in text, rec.name
    assert TOK not in caplog.text and EDGE not in caplog.text


def test_no_websocket_import():
    source = Path(inspect.getsourcefile(cc)).read_text(encoding='utf-8')
    for banned in ('ws_connect', 'WSMsgType', '/v1/ws', 'websocket', 'ws_url', 'TRANSPORTS'):
        assert banned.lower() not in source.lower(), banned


def test_no_repr_of_an_exception_in_the_module():
    source = Path(inspect.getsourcefile(cc)).read_text(encoding='utf-8')
    for banned in ('!r', '%r', 'repr(', 'exc_info', '.exception('):
        assert banned not in source, banned


def test_offer_and_close_before_start_are_harmless():
    c = cc.CloudClient(token=TOK, edge_token=EDGE, conn_id=CONN, hello=dict(HELLO), base_url='http://127.0.0.1:9')
    c.offer(obs(0), 0, 0)
    c.close('robot_stop')
    c.join(0.1)
    assert c.take_chunks() == []
    assert c.snapshot().phase == 'connecting'


def test_the_protocol_caps_the_robot_relies_on():
    # Spec §2.1, frozen contract C1, as literals (test_constant_pins: a referenced cap is pinned).
    assert cp.MAX_FRAME_BYTES == 1_572_864 and cp.MAX_HEADER_BYTES == 4_096
    assert cp.MAX_JPEG_BYTES == 524_288 and cp.MAX_CAMERAS == 4
    assert cp.MAX_STATE_DIM == 32 and cp.MAX_ACTION_DIM == 32 and cp.MAX_CHUNK_N == 100
    assert cp.MAX_SEQ == 9_007_199_254_740_992                  # 2 ** 53
    assert cp.MAX_OBS_E == 2_147_483_647                       # 2 ** 31 - 1
    assert cp.MAX_TASK_CHARS == 512
    assert cp.MAX_JPEG_SCANS == 16 and cp.PROTOCOL_VERSION == 1


def test_constants():
    C = cc.CloudClient
    assert (C.CONNECT_TIMEOUT_S, C.HELLO_HTTP_TIMEOUT_S, C.INFER_HTTP_TIMEOUT_S, C.BYE_TIMEOUT_S) == (10.0, 90.0, 8.0, 1.0)
    assert C.HELLO_ATTEMPTS == 3 and C.BACKOFF_S == (0.5, 1.0, 2.0)
    assert cc.EndInfo._fields == ('kind', 'code', 'reason', 'server_v')
    assert cc.ClientSnapshot._fields == ('phase', 'ready', 'end', 'rtt_ms_p50', 'gpu_ms_p50', 'failures')

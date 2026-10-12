"""R-20: the REAL robot client against the REAL server core, over real HTTP (spec §9.3, §10).

The robot's ``cloud_client`` (aiohttp, its own thread) and ``cloud_schedule`` talk to
``server_core.build_app(StaticSessionBackend, EngineCache(FakeEngine))`` served by an in-process
uvicorn on 127.0.0.1 — the wire both sides speak, not two test doubles agreeing with each other.
A thin ASGI wrapper in front of the app plays Modal's proxy: it stamps the trusted session header,
records which TCP connection each request came on, and can answer a FRAMELESS 401 or 302 the way
Modal's proxy or a school proxy would (D38, F-11).

Covered: hello → ready → infer → chunk → bye round trip driven by the real schedule; a fatal error
frame ends the run; a frameless 401 after ready ends it as ``session_gone``; a 302 is transient
and never followed.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip('aiohttp')
uvicorn = pytest.importorskip('uvicorn')
pytest.importorskip('fastapi')

_REPO = Path(__file__).resolve().parents[3]
_SERVER_PKG = _REPO / 'physical_ai_tools' / 'physical_ai_server'
if str(_SERVER_PKG) not in sys.path:
    sys.path.insert(0, str(_SERVER_PKG))

import server_core as S  # noqa: E402  (conftest put this package on sys.path)
import session_backend as SB  # noqa: E402

from physical_ai_server.inference import cloud_client as cc  # noqa: E402
from physical_ai_server.inference import cloud_protocol as cp  # noqa: E402
from physical_ai_server.inference import cloud_schedule as cs  # noqa: E402
from physical_ai_server.inference import cloud_texts_de as texts  # noqa: E402

from conftest import FakeEngine, H, REPO_ID, REVISION, W, jpeg  # noqa: E402

TOKEN = 'CrossTestConnectTokenSynthetic_000000000001'
EDGE = 'cross-test.edge~token!synthetic-0001'
SESSION_ID = 'sd-crosstest01'
assert cp.TOKEN_RE.fullmatch(TOKEN) and cp.EDGE_TOKEN_RE.fullmatch(EDGE)
HELLO = {'robot_type': 'omx_f', 'robot_profile': 'omx_full',
         'cameras': [('gripper', W, H), ('scene', W, H)], 'state_dim': 6, 'fps': 30.0}
CAMS = {'gripper': jpeg(W, H, seed=1), 'scene': jpeg(W, H, seed=2)}
STATE = np.arange(6, dtype=np.float32)


class ProxyLike:
    """ASGI wrapper standing in for Modal's proxy in front of the container."""

    def __init__(self, app):
        self.app = app
        self.mode = None            # None | '401' | '302'
        self.requests = []          # (path, client port, header names)

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        names = {k.decode('latin-1').lower(): v for k, v in scope['headers']}
        self.requests.append((scope['path'], (scope.get('client') or (None, None))[1], dict(names)))
        if scope['path'] == cp.INFER_PATH and self.mode is not None:
            while True:                              # drain the body like a proxy would
                msg = await receive()
                if not msg.get('more_body'):
                    break
            if self.mode == '401':
                headers = [(b'content-type', b'text/plain')]
                await send({'type': 'http.response.start', 'status': 401, 'headers': headers})
                await send({'type': 'http.response.body', 'body': b'no such session'})
            else:
                headers = [(b'location', b'/elsewhere'), (b'content-length', b'0')]
                await send({'type': 'http.response.start', 'status': 302, 'headers': headers})
                await send({'type': 'http.response.body', 'body': b''})
            return
        # Modal's proxy OVERWRITES the trusted session header (owner-answers-3 fact 7).
        headers = [(k, v) for k, v in scope['headers'] if k.lower() != cp.MODAL_SESSION_HEADER.encode()]
        headers.append((cp.MODAL_SESSION_HEADER.encode(), SESSION_ID.encode()))
        return await self.app(dict(scope, headers=headers), receive, send)

    def ports(self, path=None):
        return {p for (pth, p, _) in self.requests if path is None or pth == path}


class Served:
    def __init__(self):
        self.backend = SB.StaticSessionBackend(TOKEN, REPO_ID, REVISION)
        self.engine = FakeEngine()
        cache = S.EngineCache('cpu', max_models=8, loader=lambda repo, rev, dev: self.engine)
        self.proxy = ProxyLike(S.build_app(self.backend, cache, 'sha-cross'))
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('127.0.0.1', 0))
        self.port = sock.getsockname()[1]
        self.base_url = f'http://127.0.0.1:{self.port}'
        self.server = uvicorn.Server(uvicorn.Config(self.proxy, log_level='warning', access_log=False,
                                                    lifespan='on', ws='none'))   # HTTPS only (O7')
        self.thread = threading.Thread(target=self.server.run, kwargs={'sockets': [sock]}, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, 'uvicorn did not start'
            time.sleep(0.02)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(10)


@pytest.fixture
def served():
    s = Served()
    yield s
    s.stop()


@pytest.fixture
def make_client():
    made = []

    def make(base_url, token=TOKEN):
        c = cc.CloudClient(token=token, edge_token=EDGE, conn_id='5a' * 16, hello=dict(HELLO), base_url=base_url)
        made.append(c)
        c.start()
        return c
    yield make
    for c in made:
        c.close(None)
        c.join(5)


def wait_for(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.01)
    raise AssertionError('timed out waiting')


def drive(client, seconds, fps=30.0, cams=CAMS, stop_on_end=True):
    """The node's tick in miniature: the real schedule at a real 30 Hz tick."""
    sched, actions = None, []
    t_end = time.monotonic() + seconds
    nxt = time.monotonic()
    while time.monotonic() < t_end:
        now = time.monotonic()
        snap = client.snapshot()
        if snap.end is not None and stop_on_end:
            break
        if sched is None and snap.ready is not None:
            policy, limits = snap.ready
            lim = cs.clamp_limits(limits)
            sched = cs.Schedule(lim, lim.actions_per_chunk, policy['action_dim'], start=now)
        if sched is not None:
            r = sched.step(now, client.take_chunks(), True)
            assert r.end is None, r.end
            if r.ticket is not None:
                client.offer(cp.build_obs(r.ticket.seq, r.ticket.obs_e, cams, STATE), r.ticket.seq, r.ticket.obs_e)
            if r.action is not None:
                actions.append(r.action)
        nxt += 1.0 / fps
        time.sleep(max(0.0, nxt - time.monotonic()))
    return sched, actions


def test_hello_infer_bye_round_trip(served, make_client, caplog):
    caplog.set_level(logging.DEBUG)
    c = make_client(served.base_url)
    wait_for(lambda: c.snapshot().ready is not None)
    policy, limits = c.snapshot().ready
    assert policy['expected_cameras'] == ['gripper', 'scene'] and policy['action_dim'] == 6
    assert cs.clamp_limits(limits).actions_per_chunk == 50
    sched, actions = drive(c, 2.0)
    assert c.snapshot().end is None
    assert sched.counters['adopted'] >= 3 and sched.counters['failed'] == 0
    # FakeEngine: row i = state + i; the first executed action is chunk 0 row 0 (§2.4)
    assert np.array_equal(actions[0], STATE)
    assert np.array_equal(actions[1], STATE + 1)
    snap = c.snapshot()
    assert snap.rtt_ms_p50 is not None and snap.gpu_ms_p50 == 1.5
    c.close('robot_stop', sched.facts(time.monotonic()))
    c.join(5)
    paths = [p for p, _, _ in served.proxy.requests]
    assert paths[0] == cp.HELLO_PATH and paths[-1] == cp.BYE_PATH
    assert paths.count(cp.HELLO_PATH) == 1 and cp.HEALTH_PATH not in paths      # D29
    assert len(served.proxy.ports()) == 1                                       # one kept-alive connection
    for _, _, h in served.proxy.requests:
        assert h[cp.EDGE_AUTH_HEADER.lower()] == ('Bearer ' + EDGE).encode()
        assert h[cp.CONNECT_AUTH_HEADER.lower()] == ('Bearer ' + TOKEN).encode()
    assert served.backend._ended is not None and served.backend._ended[1] == 'robot_stop'
    assert TOKEN not in caplog.text and EDGE not in caplog.text


def test_a_fatal_error_frame_ends_the_run(served, make_client):
    c = make_client(served.base_url)
    wait_for(lambda: c.snapshot().ready is not None)
    drive(c, 1.0, cams={'gripper': CAMS['gripper']})          # the policy needs both cameras
    end = wait_for(lambda: c.snapshot().end)
    assert end == cc.EndInfo('server', 'cameras', None, None)
    assert texts.end_sentence(end) == texts.CAMERAS_DE


def test_a_wrong_connect_token_is_an_auth_frame_not_session_gone(served, make_client):
    c = make_client(served.base_url, token='WrongConnectTokenSynthetic_0000000000000001')
    end = wait_for(lambda: c.snapshot().end)
    assert (end.kind, end.code) == ('server', 'auth')
    assert texts.end_sentence(end) == texts.SESSION_INVALID_DE
    assert [p for p, _, _ in served.proxy.requests] == [cp.HELLO_PATH]       # no retry of a verdict


def test_a_frameless_401_after_ready_ends_as_session_gone(served, make_client):
    c = make_client(served.base_url)
    wait_for(lambda: c.snapshot().ready is not None)
    drive(c, 0.5)
    served.proxy.mode = '401'
    drive(c, 2.0)
    end = wait_for(lambda: c.snapshot().end)
    assert end == cc.EndInfo('robot', 'session_gone', 'after_ready', None)
    assert texts.end_sentence(end) == texts.ENDED_DE


def test_a_302_is_transient_and_never_followed(served, make_client):
    c = make_client(served.base_url)
    wait_for(lambda: c.snapshot().ready is not None)
    sched, _ = drive(c, 0.5)
    adopted = sched.counters['adopted']
    served.proxy.mode = '302'
    c.offer(cp.build_obs(10_000, 0, CAMS, STATE), 10_000, 0)
    got = []
    wait_for(lambda: got.extend(c.take_chunks()) or got)
    assert got == [(10_000, 0, None)]                          # the slot is free, the run goes on
    assert c.snapshot().end is None and c.snapshot().failures == 1
    assert '/elsewhere' not in [p for p, _, _ in served.proxy.requests]
    served.proxy.mode = None
    c.offer(cp.build_obs(10_001, 0, CAMS, STATE), 10_001, 0)
    got = []
    wait_for(lambda: got.extend(c.take_chunks()) or got)
    (seq, obs_e, actions), = got
    assert seq == 10_001 and actions is not None and actions.shape == (50, 6)
    assert adopted >= 1

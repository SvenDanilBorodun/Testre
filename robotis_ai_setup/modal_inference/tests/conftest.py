"""Shared test setup for the Cloud-Inferenz Modal app (spec §3.1, §9.2).

Puts this package's directory (``server_core``, ``session_backend``) and the robot
package's ``inference`` directory (the shared ``cloud_protocol``/``chunk_engine``, imported
top-level exactly as in the container's /root) on ``sys.path``, and provides the fakes the
server tests share: a fake clock, a fake engine, small real JPEGs and an app factory.
Nothing here imports Modal, torch or LeRobot.
"""

from __future__ import annotations

from pathlib import Path
import sys
import threading

import numpy as np
import pytest

_PKG = Path(__file__).resolve().parents[1]
_REPO = _PKG.parents[1]
_SHARED = _REPO / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server' / 'inference'
for _p in (_SHARED, _PKG):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

TOKEN = 'T' * 43
TOKEN_B = 'U' * 43
CONN = 'ab' * 16
CONN_B = 'cd' * 16
SESSION_ID = 'sd-abc123'
REPO_ID = 'EduBotics-Solutions/act-test'
REVISION = 'a' * 40
W, H = 64, 48
SUMMARY = {'type': 'act', 'expected_cameras': ['gripper', 'scene'],
           'image_shapes': {'gripper': [3, H, W], 'scene': [3, H, W]},
           'trained_fps': 30, 'chunk_size': 100, 'n_action_steps': 15, 'state_dim': 6, 'action_dim': 6,
           'requires_task': False}


class FakeClock:
    """The session clock ``build_app(clock=...)`` reads; the test moves it."""

    def __init__(self, t=1000.0):
        self.t = t
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return self.t

    def advance(self, dt):
        with self._lock:
            self.t += dt


class FakeEngine:
    """Stands in for ``chunk_engine.ChunkEngine``: row i of the chunk = state + i (float32)."""

    def __init__(self, summary=None, fail=None):
        self.summary = dict(SUMMARY if summary is None else summary)
        self.calls = []
        self.fail = fail
        self.lock = threading.Lock()

    def chunk(self, images, state, n):
        with self.lock:
            self.calls.append({'images': {k: v.shape for k, v in images.items()}, 'n': n,
                               'thread': threading.current_thread().name})
        if self.fail is not None:
            raise self.fail
        a = self.summary['action_dim']
        rows = np.asarray(state, np.float32)[:a][None, :] + np.arange(n, dtype=np.float32)[:, None]
        return rows.astype('<f4'), 1.5


def jpeg(w=W, h=H, seed=0, **params):
    import cv2
    rng = np.random.default_rng(seed)
    img = (rng.random((h, w, 3)) * 255).astype(np.uint8)
    flags = []
    for k, v in params.items():
        flags += [getattr(cv2, k), v]
    return cv2.imencode('.jpg', img, flags)[1].tobytes()


def bearer(token):
    return 'Bearer ' + token


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def fake_engine():
    return FakeEngine()


class FakeBackend:
    """A programmable session backend that records every call (method, token, session id, conn id, ...).

    ``connect_result`` / ``peek_result``: a Grant or an exception to raise. ``heartbeat``: an async
    callable ``(n) -> float`` (n = 1-based call count) or None for 600.0.
    """

    def __init__(self, session_backend_module):
        SB = session_backend_module
        self.SB = SB
        self.calls = []
        self.grant = SB.Grant('row-1', REPO_ID, REVISION, 'act', 600.0)
        self.connect_result = None
        self.peek_result = None
        self.heartbeat_behaviour = None
        self.heartbeats = 0

    async def peek(self, token, modal_session_id):
        self.calls.append(('peek', token, modal_session_id))
        if isinstance(self.peek_result, BaseException):
            raise self.peek_result
        return self.peek_result or self.SB.Grant('row-1', REPO_ID, REVISION, 'act', None)

    async def connect(self, token, modal_session_id, conn_id):
        self.calls.append(('connect', token, modal_session_id, conn_id))
        if isinstance(self.connect_result, BaseException):
            raise self.connect_result
        return self.connect_result or self.grant

    async def heartbeat(self, token, modal_session_id, conn_id, stats):
        self.heartbeats += 1
        self.calls.append(('heartbeat', token, modal_session_id, conn_id, stats))
        if self.heartbeat_behaviour is not None:
            return await self.heartbeat_behaviour(self.heartbeats)
        return 600.0

    async def end(self, token, modal_session_id, conn_id, reason, stats):
        self.calls.append(('end', token, modal_session_id, conn_id, reason, stats))

    def names(self):
        return [c[0] for c in self.calls]


def headers(token=TOKEN, conn=CONN, session=SESSION_ID, edge='edge-token-for-tests-0123'):
    h = {'Authorization': bearer(token), 'Modal-Authorization': bearer(edge)}
    if conn is not None:
        h['X-Conn-Id'] = conn
    if session is not None:
        h['x-modal-server-session-id'] = session
    return h


def hello_frame(P, conn=CONN, **over):
    kw = dict(conn_id=conn, robot_type='omx_f', robot_profile='omx_full',
              cameras=[('gripper', W, H), ('scene', W, H)], state_dim=6, fps=30)
    kw.update(over)
    return P.build_hello(**kw)


def obs_frame(P, seq=1, obs_e=0, cams=None, state=None, w=W, h=H):
    if cams is None:
        cams = {'gripper': jpeg(w, h, seed=1), 'scene': jpeg(w, h, seed=2)}
    if state is None:
        state = np.arange(6, dtype=np.float32)
    return P.build_obs(seq, obs_e, cams, state)


@pytest.fixture
def server():
    """Factory: ``server(backend=None, engine=None, clock=None, ttl=10.0, loader=None, max_models=8)`` ->
    ``(TestClient, backend, engine, cache)``. Use the client as a context manager so one event loop
    (and the background session checks it runs) lives across requests."""
    import session_backend as SB
    import server_core as S
    from fastapi.testclient import TestClient

    def make(backend=None, engine=None, clock=None, ttl=10.0, loader=None, max_models=8):
        backend = backend or FakeBackend(SB)
        engine = engine or FakeEngine()
        cache = S.EngineCache('cpu', max_models=max_models, loader=loader or (lambda repo, rev, dev: engine))
        app = S.build_app(backend, cache, 'sha-test', clock=clock or FakeClock(), cache_ttl_s=ttl)
        return TestClient(app), backend, engine, cache
    return make

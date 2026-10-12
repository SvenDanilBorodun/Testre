"""``POST /v1/prefetch``: the cloud API's model prefetch for a reservation (spec §3.3, D31).

It peeks the session with the REQUEST header's Modal session id, loads the model into the LRU
(lease taken and released: the model stays), answers 204 — also when the load outlasts
``PREFETCH_TIMEOUT_S`` (it finishes in the background and ``hello`` waits for the same per-key
lock) — and changes no DB state: ``peek`` is its only backend call, also on a refusal.
"""

from __future__ import annotations

import threading
import time

import pytest

from conftest import REPO_ID, REVISION, SESSION_ID, TOKEN, FakeEngine, headers, hello_frame
import chunk_engine
import cloud_protocol as P
import server_core as S
import session_backend as SB


def _err(r):
    assert r.headers['content-type'] == P.CONTENT_TYPE
    return P.parse_error(*P.decode(r.content))


def _prefetch(client, **kw):
    return client.post(P.PREFETCH_PATH, content=b'', headers=headers(conn=None, **kw))


class Loader:
    def __init__(self, delay=0.0, fail=None):
        self.delay = delay
        self.fail = fail
        self.calls = 0
        self.done = threading.Event()
        self.engine = FakeEngine()

    def __call__(self, repo, rev, dev):
        self.calls += 1
        time.sleep(self.delay)
        self.done.set()
        if self.fail is not None:
            raise self.fail
        return self.engine


def test_prefetch_peeks_with_the_header_session_id_and_keeps_the_model(server):
    loader = Loader()
    client, backend, _, cache = server(loader=loader)
    with client:
        r = _prefetch(client)
        assert r.status_code == 204 and r.content == b''
        assert backend.calls == [('peek', TOKEN, SESSION_ID)]
        assert cache.loaded() == [(REPO_ID, REVISION)] and cache.refcount(REPO_ID, REVISION) == 0
        assert client.post(P.HELLO_PATH, content=hello_frame(P), headers=headers()).status_code == 200
        assert loader.calls == 1                                            # hello found it in the LRU
    assert S.PREFETCH_TIMEOUT_S == 110.0


@pytest.mark.parametrize('code,reason,status', [('auth', None, 401), ('ended', 'never_connected', 410),
                                                ('ended', 'superseded', 410), ('unavailable', None, 503),
                                                ('replaced', None, 409)])
def test_prefetch_session_denied(server, code, reason, status):
    loader = Loader()
    client, backend, *_ = server(loader=loader)
    backend.peek_result = SB.SessionDenied(code, reason)
    with client:
        r = _prefetch(client)
        assert r.status_code == status and _err(r) == (code, False, None, reason, None)
    assert loader.calls == 0 and backend.names() == ['peek']


@pytest.mark.parametrize('kind', ['model', 'model_type'])
def test_prefetch_model_refused_is_422(server, kind):
    loader = Loader(fail=chunk_engine.ModelRefused(kind, 'x'))
    client, backend, _, cache = server(loader=loader)
    with client:
        r = _prefetch(client)
        assert r.status_code == 422 and _err(r) == (kind, False, None, None, None)
    assert backend.names() == ['peek'] and cache.loaded() == []


def test_slow_load_answers_204_at_the_timeout_and_finishes_in_background(server, monkeypatch):
    monkeypatch.setattr(S, 'PREFETCH_TIMEOUT_S', 0.3)
    loader = Loader(delay=1.0)
    client, backend, _, cache = server(loader=loader)
    with client:
        t0 = time.monotonic()
        r = _prefetch(client)
        took = time.monotonic() - t0
        assert r.status_code == 204 and 0.25 <= took < 0.9
        assert cache.loaded() == []
        assert loader.done.wait(3)
        deadline = time.monotonic() + 2
        while not cache.loaded() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert cache.loaded() == [(REPO_ID, REVISION)] and cache.refcount(REPO_ID, REVISION) == 0
    # A slow load that is then refused does not surface as an unhandled background error either.
    monkeypatch.setattr(S, 'PREFETCH_TIMEOUT_S', 0.1)
    loader = Loader(delay=0.4, fail=chunk_engine.ModelRefused('model', 'x'))
    client, backend, *_ = server(loader=loader)
    with client:
        assert _prefetch(client).status_code == 204
        assert loader.done.wait(3)
        time.sleep(0.1)
    assert backend.names() == ['peek']


def test_prefetch_makes_no_db_state_call_other_than_peek(server):
    client, backend, *_ = server()
    with client:
        for _ in range(3):
            assert _prefetch(client).status_code == 204
        backend.peek_result = RuntimeError('db down')
        r = _prefetch(client)
        assert r.status_code == 503 and _err(r)[0] == 'unavailable'
    assert backend.names() == ['peek'] * 4


def test_prefetch_requires_the_connect_token(server):
    client, backend, *_ = server()
    with client:
        h = headers(conn=None)
        del h['Authorization']
        r = client.post(P.PREFETCH_PATH, content=b'', headers=h)
        assert r.status_code == 401 and _err(r)[0] == 'auth'
    assert backend.calls == []

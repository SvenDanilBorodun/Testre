"""V2-3: a client that disconnects in the middle of its request body.

Starlette raises ``ClientDisconnect`` out of ``request.stream()``. It must end the request quietly: no
``internal`` answer, no ERROR line, no traceback (Starlette's ServerErrorMiddleware re-raises anything
that reaches the ``Exception`` handler, and uvicorn then logs „Exception in ASGI application“ with the
whole stack), just one INFO line naming the exception type. Every route that reads a body is covered;
the last test does it over a real socket (headers + part of a declared body, then close).
"""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
import socket
import threading
import time

import pytest

from conftest import CONN, SESSION_ID, TOKEN, FakeBackend, FakeClock, FakeEngine, bearer
import cloud_protocol as P
import server_core as S
import session_backend as SB

ROUTES = {P.HELLO_PATH: False, P.INFER_PATH: True, P.BYE_PATH: True, P.PREFETCH_PATH: False}
DECLARED = 100_000
SENT = 1_000


def _headers(path):
    h = {'authorization': bearer(TOKEN), 'x-modal-server-session-id': SESSION_ID,
         'content-type': P.CONTENT_TYPE, 'content-length': str(DECLARED)}
    if ROUTES[path]:
        h['x-conn-id'] = CONN
    return h


class Captured(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def logs():
    """Every record of our logger and of uvicorn's, whatever propagation an earlier test configured."""
    handler = Captured()
    loggers = [logging.getLogger(n) for n in ('edubotics.inference.server', 'uvicorn', 'uvicorn.error')]
    before = [(lg, lg.level) for lg in loggers]
    for lg in loggers:
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
    yield handler
    for lg, level in before:
        lg.removeHandler(handler)
        lg.setLevel(level)


def _assert_quiet(records):
    assert [r for r in records if r.levelno >= logging.WARNING] == []
    assert all(r.exc_info is None for r in records)
    ours = [r for r in records if r.name == 'edubotics.inference.server']
    assert [(r.levelno, r.getMessage()) for r in ours] == [(logging.INFO, 'request body cut off: ClientDisconnect')]
    text = ' '.join(r.getMessage() for r in records)
    assert TOKEN not in text and SESSION_ID not in text


def _app():
    backend = FakeBackend(SB)
    engine = FakeEngine()
    cache = S.EngineCache('cpu', loader=lambda r, v, d: engine)
    return S.build_app(backend, cache, 'sha-test', clock=FakeClock()), backend, engine


@pytest.mark.parametrize('path', sorted(ROUTES))
def test_a_disconnect_mid_body_ends_quietly(path, logs):
    app, backend, engine = _app()
    scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.3'}, 'http_version': '1.1',
             'method': 'POST', 'scheme': 'https', 'path': path, 'raw_path': path.encode(), 'query_string': b'',
             'root_path': '', 'client': ('127.0.0.1', 50000), 'server': ('127.0.0.1', 8000),
             'headers': [(k.encode(), v.encode()) for k, v in _headers(path).items()]}
    incoming = [{'type': 'http.request', 'body': b'\0' * SENT, 'more_body': True}, {'type': 'http.disconnect'}]
    sent = []

    async def receive():
        if incoming:
            return incoming.pop(0)
        await asyncio.sleep(3600)                      # never reached: a disconnect is final

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))             # pre-fix: ClientDisconnect escaped here
    statuses = [m['status'] for m in sent if m['type'] == 'http.response.start']
    assert statuses == [400]                           # a frame nobody reads; never 'internal'
    _assert_quiet(logs.records)
    assert backend.calls == [] and engine.calls == []


def test_every_body_read_goes_through_read_capped():
    """The catch lives in ``_read_capped``; no route reads a body any other way."""
    tree = ast.parse((Path(S.__file__)).read_text(encoding='utf-8'))
    readers = set()
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                if (isinstance(node, ast.Attribute) and node.attr in ('stream', 'body', 'json', 'form')
                        and isinstance(node.value, ast.Name) and node.value.id == 'request'):
                    readers.add(fn.name)
    assert readers == {'_read_capped'}


def test_a_disconnect_over_a_real_socket(logs):
    uvicorn = pytest.importorskip('uvicorn')
    app, backend, engine = _app()
    done = threading.Event()
    escaped, statuses = [], []

    async def recorder(scope, receive, send):              # what the app tried to answer, and any escape
        async def watch(message):
            if message['type'] == 'http.response.start':
                statuses.append(message['status'])
            await send(message)
        try:
            await app(scope, receive, watch)
        except BaseException as e:                       # noqa: BLE001 - recorded, then re-raised
            escaped.append(type(e).__name__)
            raise
        finally:
            if scope['type'] == 'http':
                done.set()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(recorder, log_config=None, access_log=False, lifespan='on', ws='none'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline, 'uvicorn did not start'
            time.sleep(0.02)
        head = f'POST {P.INFER_PATH} HTTP/1.1\r\nhost: 127.0.0.1\r\n' + ''.join(
            f'{k}: {v}\r\n' for k, v in _headers(P.INFER_PATH).items()) + '\r\n'
        with socket.create_connection(('127.0.0.1', port), timeout=5) as client:
            client.sendall(head.encode('ascii') + b'\0' * SENT)
            client.shutdown(socket.SHUT_RDWR)
        assert done.wait(5), 'the request never reached the app'
        time.sleep(0.2)                                  # let uvicorn log whatever it would log
    finally:
        server.should_exit = True
        thread.join(10)
    assert escaped == [] and 500 not in statuses
    _assert_quiet(logs.records)
    assert backend.calls == [] and engine.calls == []

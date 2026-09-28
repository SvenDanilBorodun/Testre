#!/usr/bin/env python3
"""``workflow/code_rpc.py`` — every frame on the data socket is hostile (A7.3).

Driven end to end over a real unix socket in a temp dir, with a hand-built
``WorkflowContext`` and, where a real handler would need an arm or a camera,
a fake handler injected through the server's handler table. What is REAL in
every test: the listener, the framing, the token gate, the validation from
``robot_api``, the sleeping token buckets, the one dispatch worker per run,
the handle table and the German replies.

Two structural properties are asserted over the module's own AST, because an
end-to-end test cannot see them once they regress:

- every ``recv``/``sendall`` is preceded in its function by ``settimeout`` —
  no wait on the data socket is unbounded;
- ``struct.pack('>I')`` / ``struct.unpack('>I')`` appear in ``read_frame`` /
  ``write_frame`` and nowhere else in the server package — one framing.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import json
import math
import os
import select
import socket
import struct
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path

import pytest

from physical_ai_server.workflow import code_rpc
from physical_ai_server.workflow import robot_api
from physical_ai_server.workflow.code_rpc import (
    CodeRpcServer,
    FrameError,
    FrameTooLarge,
    read_frame,
    write_frame,
    validate_value,
)
from physical_ai_server.workflow.perception import Detection
from physical_ai_server.workflow.workflow_manager import WorkflowContext


_REPO = Path(__file__).resolve().parents[3]
_PACKAGE_DIR = Path(code_rpc.__file__).resolve().parents[1]
_CODE_RPC_SRC = Path(code_rpc.__file__).read_text(encoding='utf-8')

_L = robot_api.RPC_LIMITS
# The bucket grants a token on ``tokens >= 1.0`` over a float refill; a
# draining client measured against BURST + rate × elapsed may see one or two
# calls of rounding, never a policy's worth.
_RATE_SLACK_CALLS = 2
# The fire-and-forget flood's anti-vacuity probe: how many buffered replies it
# reads back, and how long the server then has to decode that many more frames.
# 100 is the number the old frame-count floor asserted — it is asserted here on
# the same counter, over a window the TEST opens instead of one the host's
# socket buffer happened to close. 100 calls cost 0.25 s measured on both hosts
# (BURST is free, the rest at MAX_CALLS_PER_S); the bound is 12× that.
_FLOOD_PROBE_REPLIES = 100
_FLOOD_PROBE_RESUME_S = 3.0


def _conn_threads() -> int:
    return sum(1 for t in threading.enumerate() if t.name == 'code-rpc-conn')


def _wait_for_no_conn_threads(timeout_s: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_s
    while _conn_threads() and time.monotonic() < deadline:
        time.sleep(0.05)


def _peer_closed(s: socket.socket) -> bool:
    """EOF, or the error a peer that closed with our byte unread leaves."""
    try:
        return s.recv(1) == b''
    except OSError:
        return True


# ══════════════════════════════════════════════════════════════════════════
# harness
# ══════════════════════════════════════════════════════════════════════════

def _ctx(**over) -> WorkflowContext:
    published = []
    logs = []
    kw = dict(publisher=lambda pts: published.append(pts),
              log=lambda m: logs.append(m),
              var_lock=threading.RLock(),
              claim_lock=threading.RLock())
    kw.update(over)
    ctx = WorkflowContext(**kw)
    ctx._test_published = published
    ctx._test_logs = logs
    return ctx


class _Client:
    """A raw-socket client speaking the data protocol (the stub is not used
    here on purpose: the server must treat a hand-rolled client the same)."""

    def __init__(self, path: str, token: str | None, *, hello: bool = True):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self._id = 0
        if hello:
            self.hello_reply = self.call('__hello', [token])

    def send(self, obj) -> None:
        write_frame(self.sock, obj, _L.MAX_FRAME_BYTES, timeout_s=5.0)

    def recv(self, timeout_s: float = 5.0):
        return read_frame(self.sock, _L.MAX_FRAME_BYTES, timeout_s=timeout_s)

    def call(self, method, args, **extra):
        self._id += 1
        req = {'id': self._id, 'm': method, 'a': args}
        req.update(extra)
        self.send(req)
        return self.recv()

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


@pytest.fixture
def sock_dir():
    with tempfile.TemporaryDirectory(prefix='code-rpc-') as d:
        yield d


@pytest.fixture
def server(sock_dir):
    srv = CodeRpcServer(os.path.join(sock_dir, 'rpc.sock'))
    yield srv
    srv.close()


def _fake_handlers(**overrides):
    table = dict(code_rpc.default_handlers())
    table.update(overrides)
    return table


# ══════════════════════════════════════════════════════════════════════════
# constants + structure
# ══════════════════════════════════════════════════════════════════════════

def test_every_constant_of_the_spec_exists_by_name():
    assert code_rpc.CODE_RPC_RECV_TIMEOUT_S == 0.25
    assert code_rpc.CODE_RPC_SEND_TIMEOUT_S == 2.0
    assert code_rpc.CODE_RPC_MAX_CONNECTIONS_PER_RUN == 4
    assert code_rpc.CODE_RPC_MAX_HANDLES == 256
    assert code_rpc.CODE_STATUS_MIN_INTERVAL_S == 0.1
    assert code_rpc.STDOUT_MAX_LINE_BYTES == 2000
    assert code_rpc.STDOUT_MAX_LINES_PER_S == 20
    assert code_rpc.MAX_FRAME_BYTES == _L.MAX_FRAME_BYTES == 65536
    assert code_rpc.CONTROL_MAX_FRAME_BYTES == _L.CONTROL_MAX_FRAME_BYTES == 196608
    assert code_rpc.MAX_CODE_PROJECT_BYTES == robot_api.MAX_CODE_PROJECT_BYTES == 131072
    assert code_rpc.MAX_CODE_FILES == 32 and code_rpc.MAX_CODE_FILE_BYTES == 65536
    assert code_rpc.CODE_PATH_RE == robot_api.CODE_PATH_RE
    assert code_rpc.CONTROL_MAX_FRAME_BYTES >= code_rpc.MAX_CODE_PROJECT_BYTES + 8192


def test_no_edubotics_token_in_code_rpc():
    assert ('EDUBOTICS' + '_') not in _CODE_RPC_SRC


def test_workflow_context_declares_the_three_code_fields():
    names = {f.name for f in dataclasses.fields(WorkflowContext)}
    assert {'rpc_handles', 'student_objects', 'code_status_last_emit'} <= names
    ctx = _ctx()
    assert isinstance(ctx.rpc_handles, OrderedDict) and ctx.rpc_handles == {}
    assert ctx.student_objects == {}
    assert ctx.code_status_last_emit == 0.0


def _calls_in_order(fn: ast.FunctionDef):
    out = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            out.append(((node.lineno, node.col_offset), node.func.attr))
    return sorted(out)


def test_every_recv_and_sendall_carries_a_timeout():
    """No ``.recv(``/``.recv_into(``/``.sendall(`` in the module without a
    ``.settimeout(`` earlier in the same function body."""
    tree = ast.parse(_CODE_RPC_SRC)
    seen_socket_op = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        calls = _calls_in_order(node)
        for pos, attr in calls:
            if attr in ('recv', 'recv_into', 'sendall', 'send'):
                seen_socket_op = True
                assert any(a == 'settimeout' and p < pos for p, a in calls), (
                    f'{node.name}: {attr} without a preceding settimeout')
    assert seen_socket_op


def test_frames_are_read_into_fresh_buffers_never_accumulated():
    """F1: the reader allocates exactly ``length`` bytes and ``recv_into``s
    them; there is no ``buf += data`` / ``buf += sock.recv(…)`` anywhere in
    the reader (an integer offset ``got += k`` is not a buffer)."""
    tree = ast.parse(_CODE_RPC_SRC)
    buffer_names = {'buf', 'buffer', 'data', 'body', 'chunk', 'header'}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ('read_frame', '_recv_exact'):
            for sub in ast.walk(node):
                if not (isinstance(sub, ast.AugAssign) and isinstance(sub.op, ast.Add)):
                    continue
                target = sub.target.id if isinstance(sub.target, ast.Name) else ''
                assert not isinstance(sub.value, ast.Call), (
                    f'{node.name} accumulates a call result with +=')
                assert target not in buffer_names, (
                    f'{node.name} accumulates into {target} with +=')
    assert 'recv_into' in _CODE_RPC_SRC


def test_u32_framing_lives_only_in_read_and_write_frame():
    """AST over every module of the server package: ``struct.pack('>I', …)`` /
    ``struct.unpack('>I', …)`` are called from ``code_rpc.read_frame`` and
    ``code_rpc.write_frame`` and nowhere else. (An AST, not a grep: the
    Python-stub TEMPLATE inside robot_api.py contains the same text as a
    string constant, which is not a call.)"""
    sites = set()
    for path in sorted(_PACKAGE_DIR.rglob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ('pack', 'unpack')
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == 'struct'
                        and node.args and isinstance(node.args[0], ast.Constant)
                        and node.args[0].value == '>I'):
                    sites.add((path.name, fn.name))
    assert sites == {('code_rpc.py', 'read_frame'), ('code_rpc.py', 'write_frame')}


def test_the_dispatcher_takes_no_motion_lock():
    """Acceptance (4): the handlers hold ``ctx.motion_lock`` through
    ``motion._hold_motion_lock``; the dispatcher never touches it (an AST
    attribute check — the module docstring may NAME the lock in prose)."""
    for node in ast.walk(ast.parse(_CODE_RPC_SRC)):
        assert not (isinstance(node, ast.Attribute) and node.attr == 'motion_lock'), (
            f'code_rpc.py touches motion_lock at line {node.lineno}')
        assert not (isinstance(node, ast.Name) and node.id == '_hold_motion_lock')


def test_german_constants_pass_the_shipped_predicate():
    spec = importlib.util.spec_from_file_location(
        'gdl', _REPO / '.github' / 'scripts' / 'german_detail_lint.py')
    gdl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gdl)
    checked = 0
    for name, value in vars(code_rpc).items():
        if name.endswith('_DE') and isinstance(value, str):
            checked += 1
            assert (gdl.GERMAN_CHARS.search(value) or gdl.GERMAN_WORDS.search(value)), (name, value)
            assert not gdl.TRANSLITERATIONS.search(value), (name, value)
    assert checked >= 8


# ══════════════════════════════════════════════════════════════════════════
# framing
# ══════════════════════════════════════════════════════════════════════════

def _umlaut_project(target_bytes: int) -> dict[str, str]:
    """Files whose ``json.dumps(files, ensure_ascii=False)`` measures EXACTLY
    ``target_bytes``; every line carries äöüß so the encoder flag matters."""
    line = 'größe = länge * höhe  # Würfel, Bälle, Ösen, Straße\n'
    files: dict[str, str] = {}
    for i in range(31):
        files[f'modul_{i}.py'] = line * 60
    files['main.py'] = ''
    body = 'x = 1  # ä\n'
    while len(json.dumps(files, ensure_ascii=False).encode('utf-8')) < target_bytes - len(body.encode('utf-8')):
        files['main.py'] += body
    # Pad to the exact byte with ASCII (each char is one byte, JSON-neutral).
    while len(json.dumps(files, ensure_ascii=False).encode('utf-8')) < target_bytes:
        files['main.py'] += '#'
    return files


def test_control_framing_round_trips_a_start_envelope_at_MAX_CODE_PROJECT_BYTES():
    files = _umlaut_project(code_rpc.MAX_CODE_PROJECT_BYTES)
    measured = json.dumps(files, ensure_ascii=False).encode('utf-8')
    assert len(measured) == code_rpc.MAX_CODE_PROJECT_BYTES
    text = ''.join(files.values())
    non_ascii = sum(1 for c in text if ord(c) > 127)
    assert non_ascii / len(text) >= 0.12, 'the filler must be umlaut-heavy (R2-3)'
    envelope = {
        'm': 'start', 'run_id': 'lauf-1', 'token': '0' * 32, 'language': 'python',
        'files': files, 'breakpoints': [f'main.py:L{i}' for i in range(256)],
    }
    # The flag is what the test is about: ASCII-escaped, this envelope would
    # not fit the control bound at all.
    assert len(json.dumps(envelope).encode('utf-8')) > code_rpc.CONTROL_MAX_FRAME_BYTES
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        # The send buffer is shrunk deliberately, and that is what makes the
        # hangup half below mean the same thing on every host. This envelope is
        # 134,862 wire bytes; an AF_UNIX socketpair absorbs 8,192 unread bytes
        # on macOS but 180,224 on Linux (measured), so with the default buffer
        # the writer is genuinely mid-``sendall`` on one platform and long
        # finished on the other — and only the first is the state the EPIPE
        # assertion describes. Held under the frame size on BOTH, it is always
        # the first. The shrink is UNCONDITIONAL and the precondition below
        # hard-fails a host that refuses it, so the absorbed case is EXCLUDED
        # here, not covered — nothing in this test exercises a default buffer.
        a.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
        assert a.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF) < code_rpc.MAX_CODE_PROJECT_BYTES, (
            'this host would not shrink the send buffer under the envelope')
        err = []

        def _send():
            try:
                write_frame(a, envelope, code_rpc.CONTROL_MAX_FRAME_BYTES, timeout_s=5.0)
            except Exception as e:  # noqa: BLE001 — surfaced below
                err.append(e)
        t = threading.Thread(target=_send, daemon=True)
        t.start()
        got = read_frame(b, code_rpc.CONTROL_MAX_FRAME_BYTES, timeout_s=5.0)
        t.join(5.0)
        assert err == []
        assert got == envelope
        # The same envelope is refused by the DATA bound, on both sides.
        with pytest.raises(FrameTooLarge):
            write_frame(a, envelope, code_rpc.MAX_FRAME_BYTES, timeout_s=5.0)
        t = threading.Thread(target=_send, daemon=True)
        t.start()
        with pytest.raises(FrameTooLarge):
            read_frame(b, code_rpc.MAX_FRAME_BYTES, timeout_s=5.0)
        b.close()                      # the refusing reader hangs up; the writer sees EPIPE
        t.join(5.0)
        assert not t.is_alive()
        assert len(err) == 1 and isinstance(err[0], OSError)
        # A refused frame is never quietly delivered. This is an ADDITIONAL and
        # INDEPENDENT property, asserted on every host: the refusing reader's
        # hangup really tore the connection down, so a later write cannot
        # succeed. It does NOT stand in for a default-buffer run — the shrink
        # above forbids one — and it fails loudly against a live peer, where
        # this same write returns normally.
        with pytest.raises(OSError):
            write_frame(a, {'m': 'weiter'}, code_rpc.CONTROL_MAX_FRAME_BYTES, timeout_s=5.0)
    finally:
        a.close()
        b.close()


def test_write_frame_encodes_umlauts_raw_and_compact():
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        write_frame(a, {'m': 'x', 'a': ['Ablage ü', 1.5]}, 4096, timeout_s=2.0)
        header = b.recv(4)
        (n,) = struct.unpack('>I', header)
        body = b.recv(n)
        assert body == '{"m":"x","a":["Ablage ü",1.5]}'.encode('utf-8')
    finally:
        a.close()
        b.close()


def test_read_frame_returns_none_on_clean_eof_and_raises_on_garbage():
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        a.close()
        assert read_frame(b, 4096, timeout_s=1.0) is None
    finally:
        b.close()
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        a.sendall(struct.pack('>I', 3) + b'{x}')
        with pytest.raises(FrameError):
            read_frame(b, 4096, timeout_s=1.0)
        a.sendall(struct.pack('>I', 2) + b'[]')
        with pytest.raises(FrameError):
            read_frame(b, 4096, timeout_s=1.0)   # not an object
    finally:
        a.close()
        b.close()


def test_read_frame_polls_should_continue_between_timeouts():
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        polls = []

        def _cont():
            polls.append(time.monotonic())
            return len(polls) < 3
        t0 = time.monotonic()
        with pytest.raises(code_rpc.ReadAbandoned):
            read_frame(b, 4096, timeout_s=0.05, should_continue=_cont)
        assert len(polls) == 3
        assert 0.1 <= time.monotonic() - t0 < 2.0
    finally:
        a.close()
        b.close()


# ══════════════════════════════════════════════════════════════════════════
# validation
# ══════════════════════════════════════════════════════════════════════════

def _param(kind, **kw):
    return robot_api.ApiParam('wert', kind, 'wert', **kw)


@pytest.mark.parametrize('kind,kw,good,bad', [
    ('float', dict(lo=0.0, hi=300.0), [0, 1.5, 300], [True, 'x', None, -0.1, 300.1, math.nan, math.inf, [1]]),
    ('float', dict(lo=0.0, hi=0.3, lo_open=True), [0.001, 0.3], [0, 0.0, 0.31]),
    ('int', dict(lo=1, hi=15), [1, 15, 3.0], [True, 0, 16, 2.5, '3', None]),
    ('str', dict(max_len=5, pattern=r'^[a-z]+$'), ['abc'], ['', 'abcdef', 'A', 'a\x00', 5, None]),
    ('text', dict(max_len=5), ['abc', 3, 1.5, True, ''], ['abcdef', 'a\nb', None, [1], {}]),
    ('bool', {}, [True, False], [1, 0, 'true', None]),
    ('point', {}, [[0.1, -0.2, 0.0], (0, 0, 1)], [[0.1, 0.2], [1.5, 0, 0], ['a', 0, 0], [0, 0, math.nan], 'x', None]),
    ('target', {}, [[0.1, 0.2, 0.0], 'Ablage 1'], [[2, 0, 0], 'a\tb', 'x' * 41, 5, None]),
    ('ziel', {}, [0, 7], [-1, True, 1.5, 'x', None]),
    ('tagids', {}, [[0], [20, 21], list(range(16))], [[], list(range(17)), [587], [-1], [True], [1.5], 'x', None]),
    ('obj', dict(max_len=24, pattern=robot_api.TYPE_NAME_RE), ['wuerfel', 'Banane_2'], ['', 'würfel', 'a b', 'x' * 25, 3, None]),
    ('dict', dict(max_len=2), [{}, {'a': 1, 'b': [1]}], [{'a': 1, 'b': 2, 'c': 3}, [], 'x', None]),
])
def test_validate_value_by_kind(kind, kw, good, bad):
    p = _param(kind, **kw)
    for v in good:
        assert validate_value(p, v) is None, (kind, v)
    for v in bad:
        msg = validate_value(p, v)
        assert isinstance(msg, str) and msg, (kind, v)


def test_nullable_accepts_none_and_nothing_else_changes():
    p = _param('float', lo=-1.75, hi=1.75, nullable=True)
    assert validate_value(p, None) is None
    assert validate_value(p, 1.0) is None
    assert validate_value(p, 2.0)
    assert validate_value(_param('float', lo=-1.75, hi=1.75), None)


# ══════════════════════════════════════════════════════════════════════════
# the token gate + connection limits
# ══════════════════════════════════════════════════════════════════════════

def test_hello_must_be_the_first_frame(server):
    calls = []
    server.handlers = _fake_handlers(edubotics_home=lambda ctx, args: calls.append('home'))
    session = server.open_run(_ctx())
    c = _Client(server.socket_path, None, hello=False)
    c.send({'id': 1, 'm': 'home', 'a': []})
    assert c.recv() is None            # closed without a reply
    assert calls == []
    server.close_run(session)


def test_wrong_token_is_closed_before_dispatch(server):
    calls = []
    server.handlers = _fake_handlers(edubotics_home=lambda ctx, args: calls.append('home'))
    session = server.open_run(_ctx())
    c = _Client(server.socket_path, 'f' * 32, hello=False)
    c.send({'id': 1, 'm': '__hello', 'a': ['f' * 32]})
    assert c.recv() is None                      # closed: nothing to send to
    assert calls == []
    # …while the right token is greeted and dispatched.
    ok = _Client(server.socket_path, session.token)
    assert ok.hello_reply['ok'] is True
    assert ok.call('home', [])['ok'] is True
    assert calls == ['home']
    ok.close()
    server.close_run(session)


def test_a_token_of_an_ended_run_is_closed(server):
    session = server.open_run(_ctx())
    token = session.token
    server.close_run(session)
    c = _Client(server.socket_path, token, hello=False)
    c.send({'id': 1, 'm': '__hello', 'a': [token]})
    assert c.recv() is None


def test_fifth_connection_of_a_run_is_closed(server):
    session = server.open_run(_ctx())
    clients = [_Client(server.socket_path, session.token)
               for _ in range(code_rpc.CODE_RPC_MAX_CONNECTIONS_PER_RUN)]
    assert all(c.hello_reply['ok'] for c in clients)
    fifth = _Client(server.socket_path, session.token, hello=False)
    fifth.send({'id': 1, 'm': '__hello', 'a': [session.token]})
    assert fifth.recv() is None
    for c in clients:
        c.close()
    server.close_run(session)


def test_ungreeted_connections_are_capped_and_the_rest_closed_at_accept(server):
    """A7.3: a connection that never greets (one byte, then silence) holds a
    reader thread only up to ``CODE_RPC_MAX_PENDING_CONNECTIONS`` process-wide;
    every further one is closed at accept, before a thread exists for it, and
    the slots come back once the hello deadline has closed the silent ones. The
    per-run cap of four is enforced only AFTER a valid ``__hello`` and so cannot
    bound this."""
    assert code_rpc.CODE_RPC_MAX_PENDING_CONNECTIONS == 8
    _wait_for_no_conn_threads()
    assert _conn_threads() == 0
    cap = code_rpc.CODE_RPC_MAX_PENDING_CONNECTIONS
    extra = 6
    socks = []
    for _ in range(cap + extra):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(server.socket_path)
        try:
            s.sendall(b'\x00')
        except OSError:
            pass                       # closed at accept before our byte went out
        socks.append(s)
    try:
        time.sleep(0.3)
        held = _conn_threads()
        assert held <= cap, f'{held} reader threads for {cap + extra} un-greeted connections'
        readable, _, _ = select.select(socks, [], [], 0.5)
        assert len(readable) == extra, 'the connections past the cap must see EOF at once'
        assert all(_peer_closed(s) for s in readable)
        time.sleep(code_rpc.CODE_RPC_HELLO_TIMEOUT_S + 0.5)
        assert _conn_threads() == 0
        readable, _, _ = select.select(socks, [], [], 0.5)
        assert len(readable) == cap + extra, 'the silent ones must be gone after the deadline'
        # …and the slots are free again for a run that greets properly.
        session = server.open_run(_ctx())
        ok = _Client(server.socket_path, session.token)
        assert ok.hello_reply['ok'] is True
        ok.close()
        server.close_run(session)
    finally:
        for s in socks:
            s.close()


def test_the_hello_read_is_bounded_as_a_whole_not_per_byte(server):
    """A client that announces a 2000-byte hello and then drips one byte every
    0.4 s stays inside every per-recv timeout forever; the connection must
    still be closed once ``CODE_RPC_HELLO_TIMEOUT_S`` has passed in total."""
    assert code_rpc.CODE_RPC_HELLO_TIMEOUT_S == 2.0
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(server.socket_path)
    s.sendall(struct.pack('>I', 2000))
    stop = threading.Event()

    def _drip():
        while not stop.wait(0.4):
            try:
                s.sendall(b'x')
            except OSError:
                return
    t = threading.Thread(target=_drip, daemon=True)
    t0 = time.monotonic()
    t.start()
    s.settimeout(code_rpc.CODE_RPC_HELLO_TIMEOUT_S * 2)
    try:
        got = s.recv(1)
    except socket.timeout:
        pytest.fail('the drip kept an un-greeted connection alive past the hello deadline')
    finally:
        stop.set()
    elapsed = time.monotonic() - t0
    t.join(2.0)
    s.close()
    assert got == b''
    assert elapsed <= code_rpc.CODE_RPC_HELLO_TIMEOUT_S + 1.0, f'closed only after {elapsed:.2f} s'


def test_data_frame_over_64kib_closes_the_connection(server):
    session = server.open_run(_ctx())
    c = _Client(server.socket_path, session.token)
    c.sock.sendall(struct.pack('>I', code_rpc.MAX_FRAME_BYTES + 1))
    reply = c.recv()
    assert reply['ok'] is False and 'groß' in reply['e']
    assert c.recv() is None
    server.close_run(session)


# ══════════════════════════════════════════════════════════════════════════
# validation over the wire
# ══════════════════════════════════════════════════════════════════════════

def test_unknown_method_is_refused_in_german(server):
    session = server.open_run(_ctx())
    c = _Client(server.socket_path, session.token)
    r = c.call('hoome', [])
    assert r == {'id': 2, 'ok': False, 'k': 'method',
                 'e': 'robot.hoome gibt es nicht. Meintest du robot.home?'}
    r = c.call('zzzz', [])
    assert r['ok'] is False and r['e'] == 'robot.zzzz gibt es nicht.'
    server.close_run(session)


def test_arity_and_kind_are_validated(server):
    calls = []
    server.handlers = _fake_handlers(
        edubotics_move_to=lambda ctx, args: calls.append(args),
        edubotics_wait_seconds=lambda ctx, args: calls.append(args),
        edubotics_destination_pin=lambda ctx, args: calls.append(args))
    session = server.open_run(_ctx())
    c = _Client(server.socket_path, session.token)
    r = c.call('move_to', [])
    assert r['ok'] is False and r['k'] == 'arity' and 'move_to' in r['e'] and '1' in r['e']
    r = c.call('move_to', [[0.1, 0.0, 0.0], 1.0])
    assert r['ok'] is False and r['k'] == 'arity'
    r = c.call('move_to', [5])
    assert r['ok'] is False and r['k'] == 'value' and r['e'].startswith('robot.move_to: ')
    r = c.call('wait', [-1])
    assert r['ok'] is False and r['k'] == 'value'
    r = c.call('wait', [True])
    assert r['ok'] is False and r['k'] == 'value'
    r = c.call('pin', ['A', 'x', 0, 0])
    assert r['ok'] is False and r['k'] == 'value'
    r = c.call('pin', ['A\n', 0, 0, 0])
    assert r['ok'] is False and r['k'] == 'value'
    assert calls == []
    # …and the handler receives the arg_key the handler reads, not the name.
    assert c.call('move_to', [[0.1, 0.0, 0.0]])['ok'] is True
    assert c.call('move_to', ['Ablage'])['ok'] is True
    assert calls == [{'destination': [0.1, 0.0, 0.0]}, {'destination': 'Ablage'}]
    server.close_run(session)


def test_a_malformed_request_is_a_protocol_refusal_not_a_crash(server):
    server.handlers = _fake_handlers(edubotics_home=lambda ctx, args: None)
    session = server.open_run(_ctx())
    c = _Client(server.socket_path, session.token)
    r = c.call(7, [])
    assert r['ok'] is False and r['k'] == 'protocol'
    r = c.call('home', 'nicht-eine-liste')
    assert r['ok'] is False and r['k'] == 'protocol'
    c.send({'m': 'home', 'a': []})          # no id at all
    r = c.recv()
    assert r['ok'] is True and r['id'] is None
    server.close_run(session)


# ══════════════════════════════════════════════════════════════════════════
# dispatch
# ══════════════════════════════════════════════════════════════════════════

def test_real_counter_handlers_round_trip_through_the_wire(server):
    ctx = _ctx()
    session = server.open_run(ctx)
    c = _Client(server.socket_path, session.token)
    assert c.call('counter_reset', ['Punkte']) == {'id': 2, 'ok': True, 'r': None}
    assert c.call('counter_add', ['Punkte'])['ok'] is True
    assert c.call('counter_add', ['Punkte'])['ok'] is True
    assert c.call('counter_get', ['Punkte']) == {'id': 5, 'ok': True, 'r': 2}
    assert ctx.counters == {'Punkte': 2}
    assert '[CNT:Punkte=2]' in ctx._test_logs
    server.close_run(session)


def test_a_workflow_error_from_a_handler_is_relayed_verbatim(server):
    session = server.open_run(_ctx(trajectories={}))
    c = _Client(server.socket_path, session.token)
    r = c.call('replay', ['gibt_es_nicht', 1.0])
    assert r == {'id': 2, 'ok': False, 'k': 'robot', 'e': 'Unbekannte Aufnahme: gibt_es_nicht'}
    server.close_run(session)


def test_an_unexpected_exception_is_an_internal_error_without_the_traceback(server):
    def boom(ctx, args):
        raise RuntimeError('boom secret detail')
    server.handlers = _fake_handlers(edubotics_home=boom)
    session = server.open_run(_ctx())
    c = _Client(server.socket_path, session.token)
    r = c.call('home', [])
    assert r['ok'] is False and r['k'] == 'internal'
    assert r['e'] == code_rpc.INTERNAL_ERROR_DE
    assert 'boom' not in json.dumps(r)
    # The worker survived: the next call is served.
    server.handlers = _fake_handlers(edubotics_home=lambda ctx, args: None)
    assert c.call('home', [])['ok'] is True
    server.close_run(session)


def test_one_dispatch_worker_per_run_serializes_handlers(server):
    windows = []

    def slow_home(ctx, args):
        t0 = time.monotonic()
        time.sleep(0.3)
        windows.append((t0, time.monotonic()))
    server.handlers = _fake_handlers(edubotics_home=slow_home)
    session = server.open_run(_ctx())
    a = _Client(server.socket_path, session.token)
    b = _Client(server.socket_path, session.token)
    ta = threading.Thread(target=lambda: a.call('home', []), daemon=True)
    tb = threading.Thread(target=lambda: b.call('home', []), daemon=True)
    ta.start()
    tb.start()
    ta.join(5.0)
    tb.join(5.0)
    assert len(windows) == 2
    (s1, e1), (s2, e2) = sorted(windows)
    assert e1 <= s2, 'two handlers overlapped — more than one dispatch thread'
    server.close_run(session)


def test_the_worker_calls_wait_if_paused_before_every_handler(server):
    order = []
    ctx = _ctx(wait_if_paused=lambda: order.append('pause-gate'))
    server.handlers = _fake_handlers(edubotics_home=lambda c, a: order.append('handler'))
    session = server.open_run(ctx)
    c = _Client(server.socket_path, session.token)
    c.call('home', [])
    c.call('home', [])
    assert order == ['pause-gate', 'handler', 'pause-gate', 'handler']
    server.close_run(session)


def test_a_queued_call_is_refused_once_the_run_was_stopped(server):
    """The Blockly contract (``interpreter._exec_chain`` polls
    ``ctx.should_stop()`` before every statement) holds on the wire too: a
    call reaching the worker after Stopp never reaches its handler — a new
    trajectory must not START in the window between the manager's stop event
    and ``close_run``. The second half pins WHERE the poll sits: a stop that
    releases a pause is seen after ``wait_if_paused``, not only before it."""
    ran = []
    server.handlers = _fake_handlers(edubotics_move_to=lambda c, a: ran.append(a))
    session = server.open_run(_ctx(should_stop=lambda: True))
    c = _Client(server.socket_path, session.token)
    r = c.call('move_to', [[0.15, 0.0, 0.05]])
    assert r == {'id': 2, 'ok': False, 'k': 'robot', 'e': code_rpc.RUN_STOPPED_DE}
    assert ran == []
    server.close_run(session)
    flag = {'stop': False}
    ctx = _ctx(should_stop=lambda: flag['stop'],
               wait_if_paused=lambda: flag.update(stop=True))
    session = server.open_run(ctx)
    c = _Client(server.socket_path, session.token)
    r = c.call('move_to', [[0.15, 0.0, 0.05]])
    assert r['ok'] is False and r['e'] == code_rpc.RUN_STOPPED_DE
    assert ran == []
    server.close_run(session)


# ══════════════════════════════════════════════════════════════════════════
# rate budgets (B1 / B2)
# ══════════════════════════════════════════════════════════════════════════

def test_a_draining_client_is_served_at_the_policy_rate(server):
    """THE fence for the global bucket: a client that reads every reply before
    its next call — so socket backpressure cannot be what bounds it — calls
    for 2 s and is served at most ``BURST + MAX_CALLS_PER_S × elapsed`` times.
    (The fire-and-forget sibling below passes on backpressure alone: an
    AF_UNIX send buffer fills after ~200 unread replies, so it cannot tell a
    missing bucket from a present one; this shape measured 43,643 calls/s
    with ``_SleepingBucket.take`` short-circuited.)"""
    server.handlers = _fake_handlers(edubotics_home=lambda ctx, args: None)
    session = server.open_run(_ctx())
    c = _Client(server.socket_path, session.token)
    n = 0
    t0 = time.monotonic()
    while time.monotonic() - t0 < 2.0:
        assert c.call('home', [])['ok'] is True
        n += 1
    elapsed = time.monotonic() - t0
    server.close_run(session)
    c.close()
    allowed = _L.BURST + _L.MAX_CALLS_PER_S * elapsed
    assert n <= allowed + _RATE_SLACK_CALLS, (
        f'{n} calls served in {elapsed:.2f} s; the policy allows {allowed:.0f}')
    assert n >= 100, 'the server was not serving at all'


def test_fire_and_forget_flood_is_decoded_at_the_policy_rate(server):
    """P11 as a test: a client that never reads its replies floods the socket
    for 2 s and the server decodes ≤ 260/s averaged (200/s + the burst). This
    is the non-draining SHAPE of P11 (the reader waits for each reply, so a
    full reply buffer stalls the decode too); the bucket itself is fenced by
    the draining sibling above.

    WHERE the flood stalls is the host's socket buffer, not a policy: the same
    2 s decodes 71 frames on Linux and 200 on macOS (measured). So the fence
    against a vacuous ceiling — ``decoded == 0`` satisfies any rate bound — is
    not a frame count, which would only pin whichever kernel wrote it. It is
    the stall's CAUSE: reading ``_FLOOD_PROBE_REPLIES`` replies frees exactly
    that many reply slots, and a server that is stalled on backpressure — not
    wedged — decodes and answers that many more frames. A reader that died
    never answers them; a slow one does not answer them in time. That is a
    sharper fence than any count here could be: a server wedged at 71 frames
    is indistinguishable from a healthy Linux host by the count alone."""
    server.handlers = _fake_handlers(edubotics_home=lambda ctx, args: None)
    session = server.open_run(_ctx())
    c = _Client(server.socket_path, session.token)
    one = json.dumps({'id': 1, 'm': 'home', 'a': []}, separators=(',', ':')).encode()
    blob = (struct.pack('>I', len(one)) + one) * 20000

    def _flood():
        try:
            c.sock.sendall(blob)
        except OSError:
            pass
    t = threading.Thread(target=_flood, daemon=True)
    t0 = time.monotonic()
    t.start()
    time.sleep(2.0)
    # TIMING, and it is coupled to the rate limits. The reads below must begin
    # while the server is still blocked in ``_write_reply``, which tears the
    # connection down after CODE_RPC_SEND_TIMEOUT_S (2.0 s). The margin is
    # (reply capacity − BURST) / MAX_CALLS_PER_S — measured ~105 ms on Linux
    # (capacity ~71) and ~744 ms on macOS (~200). It held 14/14 under a 2-vCPU
    # quota and 6 CPU burners, because CPU pressure delays the buffer filling
    # as much as the wake-up, but RAISING ``BURST`` toward the reply capacity
    # shrinks it to nothing: BURST=70 is a knife-edge and BURST=80 fails here.
    decoded = session.frames_decoded
    elapsed = time.monotonic() - t0
    read = 0
    try:
        while read < _FLOOD_PROBE_REPLIES and c.recv(timeout_s=2.0) is not None:
            read += 1
    except (OSError, FrameError):
        pass
    resume_by = time.monotonic() + _FLOOD_PROBE_RESUME_S
    while session.frames_decoded < decoded + read and time.monotonic() < resume_by:
        time.sleep(0.01)
    advanced = session.frames_decoded
    server.close_run(session)
    c.close()
    t.join(5.0)
    assert decoded / elapsed <= 260.0, f'{decoded} frames in {elapsed:.2f} s'
    assert read == _FLOOD_PROBE_REPLIES, (
        f'only {read} of {_FLOOD_PROBE_REPLIES} replies were waiting after '
        f'{decoded} frames — either the server stopped answering, or this '
        f'host/BURST left no margin against CODE_RPC_SEND_TIMEOUT_S (see the '
        f'timing note above)')
    assert advanced >= decoded + _FLOOD_PROBE_REPLIES, (
        f'{decoded} frames decoded in {elapsed:.2f} s, then {advanced - decoded} '
        f'more for {_FLOOD_PROBE_REPLIES} replies read — the server was not '
        f'decoding, it was stuck')


def test_perception_calls_have_their_own_bucket(server):
    server.handlers = _fake_handlers(
        edubotics_see_object=lambda ctx, args: True,
        edubotics_home=lambda ctx, args: None)
    session = server.open_run(_ctx())
    c = _Client(server.socket_path, session.token)
    n = 30
    t0 = time.monotonic()
    for _ in range(n):
        assert c.call('sees', ['wuerfel'])['r'] is True
    perception_s = time.monotonic() - t0
    floor = (n - _L.PERCEPTION_BURST) / _L.PERCEPTION_MAX_PER_S
    assert perception_s >= floor - 0.1, f'{perception_s:.2f} s < {floor:.2f} s'
    t0 = time.monotonic()
    for _ in range(n):
        c.call('home', [])
    plain_s = time.monotonic() - t0
    assert plain_s < floor / 2, f'a plain call paid the perception budget: {plain_s:.2f} s'
    server.close_run(session)


# ══════════════════════════════════════════════════════════════════════════
# handles
# ══════════════════════════════════════════════════════════════════════════

def _detection(tag, xyz=(0.15, -0.05, 0.015)):
    return Detection(centroid_px=(1.0, 2.0), bbox_px=(0, 0, 4, 4), confidence=1.0,
                     label='wuerfel', aruco_id=tag, world_xyz_m=xyz)


def test_find_returns_a_handle_the_run_owns_and_object_position_resolves_it(server):
    ctx = _ctx()
    server.handlers = _fake_handlers(edubotics_find_object=lambda c, a: _detection(20))
    session = server.open_run(ctx)
    c = _Client(server.socket_path, session.token)
    r = c.call('find', ['wuerfel'])
    assert r['ok'] is True and r['r'] == {'h': 0}
    assert list(ctx.rpc_handles) == [0]
    # object_position is the REAL handler: it reads ziel.world_xyz_m.
    r = c.call('object_position', [0])
    assert r['ok'] is True and r['r'] == [0.15, -0.05, 0.015]
    server.close_run(session)


def test_find_returning_none_is_null_not_a_handle(server):
    server.handlers = _fake_handlers(edubotics_find_object=lambda c, a: None)
    session = server.open_run(_ctx())
    c = _Client(server.socket_path, session.token)
    assert c.call('find', ['wuerfel']) == {'id': 2, 'ok': True, 'r': None}
    server.close_run(session)


def test_unowned_handle_is_refused(server):
    seen = []
    server.handlers = _fake_handlers(edubotics_mark_done=lambda c, a: seen.append(a['ziel']))
    session = server.open_run(_ctx())
    c = _Client(server.socket_path, session.token)
    r = c.call('mark_done', [99])
    assert r == {'id': 2, 'ok': False, 'k': 'handle', 'e': code_rpc.UNKNOWN_HANDLE_DE}
    assert seen == []
    server.close_run(session)


def test_handles_are_per_run_and_the_table_is_capped(server):
    ctx_a = _ctx()
    ctx_b = _ctx()
    counter = [0]

    def find(c, a):
        counter[0] += 1
        return _detection(counter[0])
    server.handlers = _fake_handlers(
        edubotics_find_object=find,
        edubotics_mark_done=lambda c, a: None)
    sa = server.open_run(ctx_a)
    sb = server.open_run(ctx_b)
    ca = _Client(server.socket_path, sa.token)
    cb = _Client(server.socket_path, sb.token)
    assert ca.call('find', ['wuerfel'])['r'] == {'h': 0}
    assert cb.call('mark_done', [0])['ok'] is False        # b does not own a's 0
    for _ in range(code_rpc.CODE_RPC_MAX_HANDLES + 5):
        ca.call('find', ['wuerfel'])
    assert len(ctx_a.rpc_handles) == code_rpc.CODE_RPC_MAX_HANDLES
    assert 0 not in ctx_a.rpc_handles                      # the oldest was evicted
    assert ca.call('mark_done', [0])['ok'] is False
    server.close_run(sa)
    server.close_run(sb)


# ══════════════════════════════════════════════════════════════════════════
# the four __ methods
# ══════════════════════════════════════════════════════════════════════════

def test_line_status_is_throttled_and_carries_the_position(server):
    statuses = []
    ctx = _ctx()
    session = server.open_run(ctx, on_block_change=lambda *a: statuses.append(a))
    c = _Client(server.socket_path, session.token)
    assert c.call('__line', ['main.py', 3])['ok'] is True
    assert c.call('__line', ['main.py', 4])['ok'] is True
    assert statuses == [('main.py:L3', 'running', 0.0)]
    time.sleep(code_rpc.CODE_STATUS_MIN_INTERVAL_S + 0.05)
    assert c.call('__line', ['utils/helfer.py', 9])['ok'] is True
    assert statuses[-1] == ('utils/helfer.py:L9', 'running', 0.0)
    assert ctx.code_status_last_emit > 0.0
    server.close_run(session)


def test_a_call_with_a_position_emits_running_from_the_worker(server):
    statuses = []
    server.handlers = _fake_handlers(edubotics_home=lambda c, a: None)
    session = server.open_run(_ctx(), on_block_change=lambda *a: statuses.append(a))
    c = _Client(server.socket_path, session.token)
    c.call('home', [], f='main.py', l=12)
    assert statuses == [('main.py:L12', 'running', 0.0)]
    c.call('home', [], f='../evil.py', l=1)       # a bad position is ignored
    c.call('home', [], f='main.py', l='x')
    assert len(statuses) == 1
    server.close_run(session)


def test_exit_info_is_recorded_on_the_session(server):
    session = server.open_run(_ctx())
    c = _Client(server.socket_path, session.token)
    info = {'kind': 'name', 'file': 'main.py', 'line': 7, 'exc_type': 'NameError',
            'name': 'robto', 'detail_line': "NameError: name 'robto' is not defined",
            'unknown_key': 'dropped'}
    assert c.call('__exit', [info])['ok'] is True
    assert session.exit_info == {k: v for k, v in info.items() if k != 'unknown_key'}
    server.close_run(session)


def test_paused_blocks_until_resume_and_emits_vars_and_status(server):
    resume = threading.Event()
    paused = {'v': False}
    statuses = []

    def set_paused(v):
        paused['v'] = bool(v)

    def wait_for_resume():
        resume.wait(5.0)
    ctx = _ctx(set_paused=set_paused, wait_for_resume=wait_for_resume,
               is_paused=lambda: paused['v'])
    session = server.open_run(ctx, on_block_change=lambda *a: statuses.append(a))
    c = _Client(server.socket_path, session.token)
    reply = {}
    t = threading.Thread(
        target=lambda: reply.update(c.call('__paused', ['main.py', 12, {'x': 1, 'liste': [1, 2], 'a=b': 3}])),
        daemon=True)
    t.start()
    time.sleep(0.3)
    assert reply == {}                                   # still blocked
    assert statuses == [('main.py:L12', 'paused', 0.0)]
    assert paused['v'] is True
    assert '[VAR:x=1]' in ctx._test_logs and '[VAR:liste=[1, 2]]' in ctx._test_logs
    assert not any(l.startswith('[VAR:a=b') for l in ctx._test_logs)
    # The Protokoll line: plain German text, no glyph — the client draws the
    # pause icon for it (RunControls.jsx::BREAKPOINT_LOG_PREFIX, lockstep-tested).
    assert 'Haltepunkt erreicht: main.py:L12' in ctx._test_logs
    set_paused(False)
    resume.set()
    t.join(5.0)
    assert reply == {'id': 2, 'ok': True, 'r': 'continue'}
    # A step leaves the run paused after the wait: the reply says so.
    resume.clear()
    reply.clear()
    t = threading.Thread(target=lambda: reply.update(c.call('__paused', ['main.py', 13, {}])), daemon=True)
    t.start()
    time.sleep(0.2)
    resume.set()                                          # paused stays True
    t.join(5.0)
    assert reply['r'] == 'step'
    server.close_run(session)


def test_paused_answers_stop_when_the_run_was_stopped(server):
    ctx = _ctx(should_stop=lambda: True)
    session = server.open_run(ctx)
    c = _Client(server.socket_path, session.token)
    assert c.call('__paused', ['main.py', 1, {}])['r'] == 'stop'
    server.close_run(session)


def test_register_object_is_validated_and_registers(server):
    # WP4 landed the catalog merge (§3.10): a schema-valid registration now
    # SUCCEEDS (the OMX default close is negative, no tag collision), while the
    # per-parameter range check still refuses hoehe_m == 0.0 with k == 'value'.
    # (The full register_object contract lives in test_code_rpc_register_object.)
    session = server.open_run(_ctx())
    c = _Client(server.socket_path, session.token)
    r = c.call('register_object', ['banane', 'Banane', [30, 31], 0.04, 0.015, None, None])
    assert r['ok'] is True, r
    r = c.call('register_object', ['banane', 'Banane', [30, 31], 0.0, 0.015, None, None])
    assert r['ok'] is False and r['k'] == 'value'
    server.close_run(session)


# ══════════════════════════════════════════════════════════════════════════
# lifecycle
# ══════════════════════════════════════════════════════════════════════════

def test_close_run_closes_connections_and_joins_the_worker(server):
    session = server.open_run(_ctx())
    c = _Client(server.socket_path, session.token)
    server.close_run(session)
    assert c.recv(timeout_s=2.0) is None
    assert not session.worker_alive()


def test_close_run_releases_a_reader_waiting_on_a_slow_handler(server):
    gate = threading.Event()

    def slow(ctx, args):
        gate.wait(5.0)
    server.handlers = _fake_handlers(edubotics_home=slow)
    session = server.open_run(_ctx())
    c = _Client(server.socket_path, session.token)
    t = threading.Thread(target=lambda: c.call('home', []), daemon=True)
    t.start()
    time.sleep(0.2)
    t0 = time.monotonic()
    server.close_run(session)
    gate.set()
    t.join(5.0)
    assert not t.is_alive()
    assert time.monotonic() - t0 < 3.0


def test_the_socket_file_is_world_connectable_and_unlinked_on_close(sock_dir):
    path = os.path.join(sock_dir, 'rpc.sock')
    Path(path).write_text('stale')                       # a stale file is replaced
    srv = CodeRpcServer(path)
    assert os.stat(path).st_mode & 0o777 == 0o666
    srv.close()
    assert not os.path.exists(path)

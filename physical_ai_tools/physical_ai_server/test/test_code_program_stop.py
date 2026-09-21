#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
"""The out-of-band stop path (F3 / B3 / A7.2) on the REAL WorkflowManager.

P14(A) — a program blocking in an unbounded recv wedges stop() — turned into
P14(B): a CodeProgram releases the manager within the DERIVED deadline whatever
the runner does. Plus the derived-constant pin, the second-connection kill, a
project at exactly the cap, the no-unbounded-wait AST fence, and the
`emit_student_text` / 'ausgabe' fence (the SOLE fence on the bracket strip).
"""

from __future__ import annotations

import ast
import json
import os
import socket
import tempfile
import threading
import time
import types
from pathlib import Path

import pytest

from physical_ai_server.workflow import code_errors_de
from physical_ai_server.workflow import code_program
from physical_ai_server.workflow import robot_api
from physical_ai_server.workflow.code_rpc import (
    CONTROL_MAX_FRAME_BYTES,
    MAX_CODE_PROJECT_BYTES,
    CodeRpcServer,
    read_frame,
    write_frame,
)
from physical_ai_server.workflow.code_program import CodeProgram
from physical_ai_server.workflow.handlers import output as out
from physical_ai_server.workflow.workflow_manager import WorkflowManager

_CP_SRC = Path(code_program.__file__).read_text(encoding='utf-8')


# ══════════════════════════════════════════════════════════════════════════
# harness
# ══════════════════════════════════════════════════════════════════════════

class FakeSupervisor:
    """Listens on runner.sock and speaks the control protocol. Configurable:
    whether it acks `started`, whether it sends `exited`, whether it acks
    `killed`, and whether it DIES after `started` (closes the start connection
    with no `exited` — the runner container OOM-killed, A15). Records the
    connection id that carried `start` vs `kill`."""

    def __init__(self, path, *, ack_started=True, exited=None, ack_killed=True,
                 die_after_start=False):
        self.path = path
        self.ack_started = ack_started
        self.exited = exited                  # None → hang; else e.g. {'code': 0}
        self.ack_killed = ack_killed
        self.die_after_start = die_after_start
        self.start_cid = None
        self.kill_cid = None
        self.token = None
        self.start_files = None
        self._closed = threading.Event()
        self._cid = 0
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(path)
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self._closed.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._cid += 1
            threading.Thread(target=self._serve, args=(conn, self._cid),
                             daemon=True).start()

    def _serve(self, conn, cid):
        try:
            while not self._closed.is_set():
                try:
                    frame = read_frame(conn, CONTROL_MAX_FRAME_BYTES, timeout_s=0.2,
                                       should_continue=lambda: not self._closed.is_set())
                except Exception:
                    return
                if frame is None:
                    return
                ev = frame.get('ev')
                if ev == 'start':
                    self.start_cid = cid
                    self.token = frame.get('token')
                    self.start_files = frame.get('files')
                    if self.ack_started:
                        write_frame(conn, {'ev': 'started'}, CONTROL_MAX_FRAME_BYTES,
                                    timeout_s=1.0)
                    if self.die_after_start:
                        conn.close()
                        return
                    if self.exited is not None:
                        write_frame(conn, {'ev': 'exited', **self.exited},
                                    CONTROL_MAX_FRAME_BYTES, timeout_s=1.0)
                elif ev == 'kill':
                    self.kill_cid = cid
                    if self.ack_killed:
                        write_frame(conn, {'ev': 'killed'}, CONTROL_MAX_FRAME_BYTES,
                                    timeout_s=1.0)
        except OSError:
            return

    def close(self):
        self._closed.set()
        try:
            self._sock.close()
        except OSError:
            pass


def _flood_data_socket(sock_path, token, stop_flag):
    """A raw client that greets with the run token and spams calls until the
    run closes — the „fake student flooding the data socket" of P14."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(sock_path)
        write_frame(s, {'id': 0, 'm': '__hello', 'a': [token]}, 65536, timeout_s=1.0)
        read_frame(s, 65536, timeout_s=1.0)
        i = 0
        while not stop_flag.is_set():
            i += 1
            write_frame(s, {'id': i, 'm': 'counter_get', 'a': ['x']}, 65536, timeout_s=1.0)
            read_frame(s, 65536, timeout_s=1.0)
    except Exception:
        return


def _manager(sock_dir, finished, statuses=None):
    return WorkflowManager(
        publisher=lambda pts: None,
        emit_status=None if statuses is None else statuses.append,
        on_finished=finished.append,
        get_follower_joints=lambda: [0.0, -1.57, 1.57, 0.0, 0.0, 0.8],
        code_rpc=CodeRpcServer(os.path.join(sock_dir, 'rpc.sock')),
    )


def _code_payload(files=None):
    return json.dumps({'language': 'python',
                       'files': files or {'main.py': 'robot.home()\n'}})


# ══════════════════════════════════════════════════════════════════════════
# derived constant
# ══════════════════════════════════════════════════════════════════════════

def test_the_shipped_stop_constants_are_the_numbers_we_chose():
    # LITERAL pins (test_constant_pins requires it for every referenced numeric).
    assert code_program.CODE_CONTROL_TIMEOUT_S == 2.0
    assert code_program.CODE_RUN_MAX_S == 600.0
    assert code_program._STOP_TEST_SLACK_S == 0.25


def test_stop_deadline_is_the_sum_of_its_three_bounds():
    from physical_ai_server.workflow import code_rpc
    assert code_program.CODE_STOP_DEADLINE_S == (
        code_rpc.CODE_RPC_RECV_TIMEOUT_S + code_program.CODE_CONTROL_TIMEOUT_S
        + code_program.CODE_WORKER_JOIN_S)
    # AST: the constant is the EXPRESSION, not a literal (m1 of the review).
    tree = ast.parse(_CP_SRC)
    rhs = None
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == 'CODE_STOP_DEADLINE_S'):
            rhs = node.value
    assert rhs is not None, 'CODE_STOP_DEADLINE_S assignment not found'
    names = {n.id for n in ast.walk(rhs) if isinstance(n, ast.Name)}
    assert names == {'CODE_RPC_RECV_TIMEOUT_S', 'CODE_CONTROL_TIMEOUT_S',
                     'CODE_WORKER_JOIN_S'}, names
    # …and it is a sum, never a hand-picked float literal.
    assert not any(isinstance(n, ast.Constant) for n in ast.walk(rhs))


# ══════════════════════════════════════════════════════════════════════════
# the wedge, and the fix
# ══════════════════════════════════════════════════════════════════════════

class _BlockingProgram:
    """P14(A): execute() blocks in an UNBOUNDED recv, ignoring should_stop —
    the shape a CodeProgram must NOT be. Used to characterise the wedge the fix
    removes (this passes on any tree; it is not the red-green fence)."""

    def __init__(self, sock_dir):
        self.path = os.path.join(sock_dir, 'hang.sock')
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.path)
        srv.listen(1)
        self._srv = srv
        self._conns = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        try:
            conn, _ = self._srv.accept()
            self._conns.append(conn)
        except OSError:
            pass

    def split_roots(self):
        return [], []

    @property
    def roots(self):
        return []

    def execute(self, ctx, on_block_change):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self.path)
        s.recv(4)     # unbounded — the wedge


def test_the_wedge_reproduces_with_an_unbounded_program():
    """A duck-typed program with an unbounded recv wedges the manager: stop()
    returns but _prev_run_threads_alive stays True and the next start is
    refused. Characterises P14(A) — independent of CodeProgram."""
    with tempfile.TemporaryDirectory(prefix='wedge-') as d:
        finished = []
        mgr = WorkflowManager(publisher=lambda p: None, on_finished=finished.append,
                              get_follower_joints=lambda: [0.0] * 6)
        prog = _BlockingProgram(d)
        import physical_ai_server.workflow.workflow_manager as WM
        real = WM.Interpreter.from_json
        WM.Interpreter.from_json = staticmethod(lambda raw: prog)
        try:
            ok, _msg, _ = mgr.start('{}', 'wedge')
            assert ok
            time.sleep(0.2)
            mgr.stop()
            time.sleep(0.2)
            assert mgr._prev_run_threads_alive() is True
            ok2, msg2, _ = mgr.start('{}', 'wedge2')
            assert ok2 is False and 'läuft noch' in msg2
        finally:
            WM.Interpreter.from_json = real


def test_stop_returns_within_the_derived_deadline_and_releases_the_manager_when_the_runner_never_answers():
    """P14(B): a fake supervisor that acks `started`, never sends `exited`, and
    accepts the kill connection but never acks `killed`; a fake student floods
    the data socket. stop() still returns within CODE_STOP_DEADLINE_S + slack,
    on_finished == ['stopped'], the manager frees, and the next start is
    accepted."""
    with tempfile.TemporaryDirectory(prefix='stop-') as d:
        finished = []
        mgr = _manager(d, finished)
        sup = FakeSupervisor(os.path.join(d, 'runner.sock'),
                             ack_started=True, exited=None, ack_killed=False)
        flood_stop = threading.Event()
        try:
            ok, msg, _ = mgr.start(_code_payload(), 'stopwf')
            assert ok, msg
            # Wait for the supervisor to have the token, then start the flood.
            for _ in range(50):
                if sup.token:
                    break
                time.sleep(0.02)
            assert sup.token, 'supervisor never saw start'
            threading.Thread(target=_flood_data_socket,
                             args=(os.path.join(d, 'rpc.sock'), sup.token, flood_stop),
                             daemon=True).start()
            time.sleep(0.2)
            t0 = time.monotonic()
            ok_stop, _ = mgr.stop()
            dt = time.monotonic() - t0
            flood_stop.set()
            bound = code_program.CODE_STOP_DEADLINE_S + code_program._STOP_TEST_SLACK_S
            assert ok_stop
            assert dt <= bound, f'stop() took {dt:.2f}s > {bound:.2f}s'
            # The manager is free within the same bound.
            deadline = time.monotonic() + bound
            while mgr._prev_run_threads_alive() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert mgr._prev_run_threads_alive() is False
            assert finished == ['stopped'], finished
            ok2, msg2, _ = mgr.start(_code_payload(), 'stopwf2')
            assert ok2, msg2
            mgr.stop()
        finally:
            flood_stop.set()
            sup.close()
            mgr._code_rpc.close()


def test_stop_calls_request_stop_before_the_thread_join():
    """Acceptance (2): stop() sends the out-of-band kill BEFORE waiting on the
    daemon join — the kill must not depend on _run winding down first. A source
    order fence: the behavioural kill test cannot see this (CodeProgram.execute's
    own teardown also sends the kill), so the ordering is fenced structurally."""
    import physical_ai_server.workflow.workflow_manager as WM
    tree = ast.parse(Path(WM.__file__).read_text(encoding='utf-8'))
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == 'stop':
            fn = node
    assert fn is not None
    request_stop_line = None
    join_line = None
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'request_stop'):
            request_stop_line = node.lineno if request_stop_line is None else request_stop_line
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'join'
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == '_thread'):
            join_line = node.lineno if join_line is None else join_line
    assert request_stop_line is not None, 'stop() never calls request_stop'
    assert join_line is not None, 'stop() never joins _thread'
    assert request_stop_line < join_line, (
        f'request_stop (line {request_stop_line}) must precede the _thread.join '
        f'(line {join_line})')


def test_kill_travels_on_a_second_connection():
    """request_stop opens a NEW control connection for `kill` (never the data
    socket, never the `start` connection)."""
    with tempfile.TemporaryDirectory(prefix='kill-') as d:
        finished = []
        mgr = _manager(d, finished)
        sup = FakeSupervisor(os.path.join(d, 'runner.sock'),
                             ack_started=True, exited=None, ack_killed=True)
        try:
            ok, msg, _ = mgr.start(_code_payload(), 'killwf')
            assert ok, msg
            for _ in range(50):
                if sup.start_cid:
                    break
                time.sleep(0.02)
            time.sleep(0.1)
            mgr.stop()
            for _ in range(50):
                if sup.kill_cid:
                    break
                time.sleep(0.02)
            assert sup.start_cid is not None and sup.kill_cid is not None
            assert sup.kill_cid != sup.start_cid, (sup.start_cid, sup.kill_cid)
        finally:
            sup.close()
            mgr._code_rpc.close()


def test_a_runner_that_dies_mid_program_is_reported_as_an_error_never_as_finished():
    """Fix round 1: a supervisor that acks `started` and then CLOSES the start
    connection with no `exited` (the runner container OOM-killed mid-program,
    A15) fell through _drive to the clean-exit verdict — on_finished ==
    ['finished'] and a green „Workflow abgeschlossen." for a program that never
    reached its end. §3.9: never a silent green run. The `eof` outcome is an
    error carrying the German `runner_crashed` sentence, and the kill still
    travels out-of-band so a supervisor alive behind a broken control
    connection ends the run too."""
    with tempfile.TemporaryDirectory(prefix='crash-') as d:
        finished, statuses = [], []
        mgr = _manager(d, finished, statuses)
        sup = FakeSupervisor(os.path.join(d, 'runner.sock'),
                             ack_started=True, die_after_start=True)
        try:
            ok, msg, _ = mgr.start(_code_payload(), 'crashwf')
            assert ok, msg
            deadline = time.monotonic() + 8.0
            while not finished and time.monotonic() < deadline:
                time.sleep(0.02)
            assert finished == ['error'], finished
            errors = [s for s in statuses if s.get('phase') == 'error']
            assert errors, statuses
            assert errors[-1]['error'] == code_errors_de.sentence('runner_crashed')
            for _ in range(50):
                if sup.kill_cid:
                    break
                time.sleep(0.02)
            assert sup.kill_cid is not None, 'no kill after the control eof'
        finally:
            sup.close()
            mgr._code_rpc.close()


# ══════════════════════════════════════════════════════════════════════════
# the project cap end to end
# ══════════════════════════════════════════════════════════════════════════

_PER_FILE_SOFT = 50000   # well under MAX_CODE_FILE_BYTES (65536)


def _proj_bytes(files):
    return len(json.dumps(files, ensure_ascii=False).encode('utf-8'))


def _umlaut_files(target_bytes):
    """A MULTI-file project (each file under the 64 KiB per-file cap) whose
    json.dumps(files, ensure_ascii=False) measures EXACTLY target_bytes; the
    filler carries äöüß so the ensure_ascii=False encoder flag matters."""
    filler = 'x = "äöüß"  # zähle mit\n'
    names = ['main.py', 'mod1.py', 'mod2.py', 'mod3.py', 'mod4.py']
    files = {'main.py': ''}
    oi = 0
    while _proj_bytes(files) < target_bytes - 3000:
        cur = names[oi]
        files.setdefault(cur, '')
        if len(files[cur].encode('utf-8')) >= _PER_FILE_SOFT:
            oi += 1
            continue
        files[cur] += filler
    # Pad main.py with ASCII spaces (1 byte each, no JSON escaping) to hit the
    # EXACT project byte count without touching the multi-byte filler.
    while _proj_bytes(files) < target_bytes:
        files['main.py'] += ' '
    return files


def test_a_project_at_MAX_CODE_PROJECT_BYTES_starts():
    """A project measuring exactly the cap on the ensure_ascii=False encoding
    starts (its start envelope fits CONTROL_MAX_FRAME_BYTES with 256 breakpoint
    ids); one byte over is refused in German BEFORE any socket is opened."""
    files = _umlaut_files(MAX_CODE_PROJECT_BYTES)
    assert len(json.dumps(files, ensure_ascii=False).encode('utf-8')) == MAX_CODE_PROJECT_BYTES
    with tempfile.TemporaryDirectory(prefix='cap-') as d:
        finished = []
        mgr = _manager(d, finished)
        sup = FakeSupervisor(os.path.join(d, 'runner.sock'),
                             ack_started=True, exited={'code': 0}, ack_killed=True)
        try:
            payload = json.dumps({'language': 'python', 'files': files})
            ok, msg, _ = mgr.start(payload, 'capwf')
            assert ok, msg
            deadline = time.monotonic() + 5.0
            while not finished and time.monotonic() < deadline:
                time.sleep(0.02)
            assert finished == ['finished'], finished
            # The start envelope with 256 breakpoints fits the CONTROL bound.
            prog = CodeProgram.from_payload(
                payload, 'capwf', mgr._code_rpc,
                breakpoints={'main.py': list(range(1, 257))})
            envelope = {'ev': 'start', 'run_id': 'x', 'token': 'y',
                        'language': 'python', 'files': prog.files,
                        'breakpoints': prog.breakpoints}
            size = len(json.dumps(envelope, ensure_ascii=False,
                                  separators=(',', ':')).encode('utf-8'))
            assert size <= CONTROL_MAX_FRAME_BYTES, size
            # One byte over the project cap → German refusal before any socket.
            over = dict(files)
            over['main.py'] = over['main.py'] + ' '
            bad = json.dumps({'language': 'python', 'files': over})
            ok2, msg2, _ = mgr.start(bad, 'capwf2')
            assert ok2 is False
            assert 'zu groß' in msg2
        finally:
            sup.close()
            mgr._code_rpc.close()


def test_a_project_over_the_cap_never_opens_a_run():
    """A refusal in from_payload must not have minted a token / opened a run."""
    with tempfile.TemporaryDirectory(prefix='cap2-') as d:
        server = CodeRpcServer(os.path.join(d, 'rpc.sock'))
        try:
            files = {'main.py': 'x' * (MAX_CODE_PROJECT_BYTES + 10)}
            payload = json.dumps({'language': 'python', 'files': files})
            with pytest.raises(Exception):
                CodeProgram.from_payload(payload, 'wf', server)
            assert not server._sessions
        finally:
            server.close()


# ══════════════════════════════════════════════════════════════════════════
# no unbounded wait (acceptance 7)
# ══════════════════════════════════════════════════════════════════════════

def test_execute_has_no_unbounded_wait():
    """Every socket read/write in CodeProgram carries a timeout, and there is no
    bare recv/wait/join without one (A7.2 — no wedge is possible)."""
    tree = ast.parse(_CP_SRC)
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == 'CodeProgram')
    for node in ast.walk(cls):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = (fn.id if isinstance(fn, ast.Name)
                else fn.attr if isinstance(fn, ast.Attribute) else '')
        kwargs = {kw.arg for kw in node.keywords}
        if name in ('read_frame', 'write_frame'):
            assert 'timeout_s' in kwargs, f'{name} without timeout_s at line {node.lineno}'
        if name in ('recv', 'recv_into', 'recvfrom', 'wait', 'join', 'acquire', 'sendall'):
            has_timeout = ('timeout' in kwargs
                           or any(isinstance(a, (ast.Constant, ast.Name, ast.Attribute))
                                  for a in node.args))
            assert has_timeout, f'{name} without a timeout at line {node.lineno}'


# ══════════════════════════════════════════════════════════════════════════
# _prev_run_threads_alive reads _code_threads (acceptance 3) + the docstring
# ══════════════════════════════════════════════════════════════════════════

def test_prev_run_threads_alive_reads_code_threads_and_states_the_bound():
    import physical_ai_server.workflow.workflow_manager as WM
    src = Path(WM.__file__).read_text(encoding='utf-8')
    tree = ast.parse(src)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == '_prev_run_threads_alive':
            fn = node
    assert fn is not None
    # reads _code_threads
    attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    assert '_code_threads' in attrs, '_prev_run_threads_alive does not read _code_threads'
    # the docstring states the derived bound verbatim (§3.3)
    doc = ast.get_docstring(fn) or ''
    assert 'CODE_STOP_DEADLINE_S' in doc
    assert 'out-of-band on its own' in doc
    assert 'CODE_RPC_RECV_TIMEOUT_S' in doc


# ══════════════════════════════════════════════════════════════════════════
# print() → 'ausgabe', log() → 'melde'; the bracket strip on BOTH (acceptance 9)
# ══════════════════════════════════════════════════════════════════════════

def _log_ctx():
    ns = types.SimpleNamespace(msgs=[])
    ns.log = ns.msgs.append
    return ns


def test_print_lines_use_the_ausgabe_kind_and_log_keeps_melde():
    c = _log_ctx()
    # A program's print() line rides emit_student_text(..., 'ausgabe').
    out.emit_student_text(c, '[VAR:x=1] ausgabe', 'ausgabe')
    # „melde" stringifies then routes through the same helper as 'melde'.
    out.log(c, {'message': '[VAR:y=2] melde'})
    # The bracket-sentinel strip applies to BOTH kinds (its SOLE fence).
    assert c.msgs == ['(VAR:x=1) ausgabe', '(VAR:y=2) melde']
    # Per-kind, per-run bucket: both kinds have their own state.
    assert 'ausgabe' in c._output_rate_state
    assert 'melde' in c._output_rate_state


def test_the_ausgabe_kind_has_its_own_budget_and_notice():
    assert out._BURST_BY_KIND['ausgabe'] == out.OUTPUT_BURST_LOG
    notice = out._RATE_LIMIT_NOTICE_DE['ausgabe']
    assert 'Ausgabe' in notice and 'melde' not in notice

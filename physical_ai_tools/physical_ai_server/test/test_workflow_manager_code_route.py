#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
"""WorkflowManager.start routes on `language` (§3.3 / §3.9).

A payload whose language is python/java goes to CodeProgram; everything else to
the Blockly Interpreter, byte-for-byte. The poison block an old image chokes on
loudly (P9) is characterised through the Interpreter directly.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time

import pytest

from physical_ai_server.workflow.code_program import CodeProgram
from physical_ai_server.workflow.code_rpc import (
    CONTROL_MAX_FRAME_BYTES,
    CodeRpcServer,
    read_frame,
    write_frame,
)
from physical_ai_server.workflow.interpreter import Interpreter, InterpreterError
from physical_ai_server.workflow.workflow_manager import WorkflowContext, WorkflowManager


class _MiniSupervisor:
    """Acks `started` then `exited {code:0}` — enough to let a code run finish."""

    def __init__(self, path):
        self._closed = threading.Event()
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
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            while not self._closed.is_set():
                try:
                    frame = read_frame(conn, CONTROL_MAX_FRAME_BYTES, timeout_s=0.2,
                                       should_continue=lambda: not self._closed.is_set())
                except Exception:
                    return
                if frame is None:
                    return
                if frame.get('ev') == 'start':
                    write_frame(conn, {'ev': 'started'}, CONTROL_MAX_FRAME_BYTES, timeout_s=1.0)
                    write_frame(conn, {'ev': 'exited', 'code': 0}, CONTROL_MAX_FRAME_BYTES,
                                timeout_s=1.0)
                elif frame.get('ev') == 'kill':
                    write_frame(conn, {'ev': 'killed'}, CONTROL_MAX_FRAME_BYTES, timeout_s=1.0)
        except OSError:
            return

    def close(self):
        self._closed.set()
        try:
            self._sock.close()
        except OSError:
            pass


def _manager(sock_dir):
    return WorkflowManager(
        publisher=lambda p: None,
        on_finished=lambda phase: None,
        get_follower_joints=lambda: [0.0, -1.57, 1.57, 0.0, 0.0, 0.8],
        code_rpc=CodeRpcServer(os.path.join(sock_dir, 'rpc.sock')),
    )


def test_code_payload_routes_to_code_program_not_the_interpreter():
    with tempfile.TemporaryDirectory(prefix='route-') as d:
        mgr = _manager(d)
        sup = _MiniSupervisor(os.path.join(d, 'runner.sock'))
        try:
            payload = json.dumps({'language': 'python',
                                  'files': {'main.py': 'robot.home()\n'}})
            ok, msg, unreachable = mgr.start(payload, 'wf')
            assert ok, msg
            assert isinstance(mgr._program, CodeProgram)
            assert unreachable == []          # _ik_precheck skipped for code
            mgr.stop()
        finally:
            sup.close()
            mgr._code_rpc.close()


def test_a_java_payload_also_routes_to_code_program():
    with tempfile.TemporaryDirectory(prefix='routej-') as d:
        mgr = _manager(d)
        sup = _MiniSupervisor(os.path.join(d, 'runner.sock'))
        try:
            payload = json.dumps({'language': 'java',
                                  'files': {'Main.java': 'class Main {}\n'}})
            ok, msg, _ = mgr.start(payload, 'wfj')
            assert ok, msg
            assert isinstance(mgr._program, CodeProgram)
            mgr.stop()
        finally:
            sup.close()
            mgr._code_rpc.close()


def test_blockly_payload_path_is_byte_identical():
    """A Blockly payload (no `language`) still goes through Interpreter.from_json
    — the live program is an Interpreter, never a CodeProgram."""
    with tempfile.TemporaryDirectory(prefix='blk-') as d:
        mgr = _manager(d)
        try:
            ok, msg, _ = mgr.start(json.dumps({'blocks': {'blocks': []}}), 'blk')
            assert ok, msg
            assert isinstance(mgr._program, Interpreter)
            assert not isinstance(mgr._program, CodeProgram)
            mgr.stop()
        finally:
            mgr._code_rpc.close()


def test_a_payload_with_an_unknown_language_takes_the_blockly_path():
    """language 'ruby' is not routed to CodeProgram — it falls to the Blockly
    interpreter, which then reports the empty/invalid blocks as it always did."""
    with tempfile.TemporaryDirectory(prefix='ruby-') as d:
        mgr = _manager(d)
        try:
            ok, _msg, _ = mgr.start(
                json.dumps({'language': 'ruby', 'blocks': {'blocks': []}}), 'rb')
            assert ok                      # empty Blockly workspace: starts + finishes
            assert isinstance(mgr._program, Interpreter)
            mgr.stop()
        finally:
            mgr._code_rpc.close()


def test_the_poison_block_still_errors_loudly_through_the_interpreter():
    """P9 / acceptance (6): the poison block an OLD image receives (no language
    routing) raises loudly through Interpreter.execute — run it directly."""
    payload = json.dumps({
        'language': 'python',
        'files': {'main.py': 'robot.home()'},
        'blocks': {'blocks': [{'type': 'edubotics_code_program_v1', 'id': 'code'}]},
    })
    ctx = WorkflowContext(publisher=lambda p: None, log=lambda s: None,
                          motion_lock=threading.RLock(), var_lock=threading.RLock(),
                          claim_lock=threading.RLock(), should_stop=lambda: False)
    interp = Interpreter.from_json(payload)
    with pytest.raises(InterpreterError) as exc:
        interp.execute(ctx, lambda bid, phase, prog: None)
    assert 'edubotics_code_program_v1' in str(exc.value)
    assert 'Unbekannter Block-Typ' in str(exc.value)


def test_a_runner_down_code_payload_is_refused_in_german():
    """With no code_rpc (runner not up), a code payload is refused in German at
    start — from_payload raises InterpreterError(runner_down)."""
    mgr = WorkflowManager(publisher=lambda p: None, code_rpc=None,
                          get_follower_joints=lambda: [0.0] * 6)
    payload = json.dumps({'language': 'python', 'files': {'main.py': 'robot.home()'}})
    ok, msg, _ = mgr.start(payload, 'wf')
    assert ok is False
    assert 'Programmier-Umgebung läuft nicht' in msg

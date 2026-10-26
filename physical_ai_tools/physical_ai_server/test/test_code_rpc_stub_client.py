#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
"""The SHIPPED Python stub (`runner/lib/robot.py`, generated from
`workflow/robot_api.py`) against a REAL `CodeRpcServer` on a unix socket.

What a text program meets before or after its frame reaches the robot:

- C2: a connection the robot ended (EOF, a reply it cannot read) is closed by
  the stub, so the next call reconnects with the run's token — it used to
  keep the dead socket, and every later call (and `__exit`) failed with no
  line; a reconnect keeps the launcher's project root.
- C4: a Greifziel where a Ziel or a point is expected is refused in German
  before anything is sent (it reached `json.dumps` and read as an English
  TypeError).
- C5-b: an argument the encoder cannot write (bytes, a complex, a list nested
  too deep) is refused in German before any byte is sent, and the connection
  still works.
"""

from __future__ import annotations

import importlib.util
import os
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from physical_ai_server.workflow import code_rpc
from physical_ai_server.workflow import robot_api
from physical_ai_server.workflow.code_rpc import CodeRpcServer
from physical_ai_server.workflow.workflow_manager import WorkflowContext

_REPO = Path(__file__).resolve().parents[3]


def _stub_module():
    """A fresh copy of the generated stub (its own `_rpc`)."""
    path = _REPO / robot_api.GENERATED_PATHS['python_stub']
    spec = importlib.util.spec_from_file_location('robot_stub_client_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ctx() -> WorkflowContext:
    logs: list = []
    ctx = WorkflowContext(publisher=lambda pts: None, log=logs.append,
                          var_lock=threading.RLock(), claim_lock=threading.RLock())
    ctx.code_language = 'python'
    ctx._test_logs = logs
    return ctx


@pytest.fixture
def server():
    with tempfile.TemporaryDirectory(prefix='stub-client-') as d:
        srv = CodeRpcServer(os.path.join(d, 'rpc.sock'))
        yield srv
        srv.close()


@pytest.fixture
def run(server, monkeypatch):
    """One run with the stub connected the way the launcher connects it, and
    the run's two environment variables a reconnect reads."""
    ctx = _ctx()
    session = server.open_run(ctx)
    monkeypatch.setenv('CODE_RPC_SOCKET', server.socket_path)
    monkeypatch.setenv('CODE_RUN_TOKEN', session.token)
    stub = _stub_module()
    root = tempfile.mkdtemp(prefix='stub-project-')
    stub._rpc.connect(server.socket_path, session.token, project_root=root)
    yield stub, session, ctx, root
    stub._rpc.close()
    server.close_run(session)


def test_an_eof_closes_the_socket_and_the_next_call_reconnects(server, run):
    stub, session, ctx, root = run

    def cut_the_connection(_ctx, _args):
        with session._lock:
            conns = list(session._conns)
        for conn in conns:
            conn.shutdown(socket.SHUT_RDWR)
    server.handlers = dict(code_rpc.default_handlers(), edubotics_home=cut_the_connection)
    with pytest.raises(stub.RobotError) as raised:
        stub.home()
    assert str(raised.value) == 'Die Verbindung zum Roboter ist abgebrochen.'
    assert stub._rpc._sock is None, 'the dead socket is closed, never kept'
    # The next call reconnects with the run's token and is served.
    stub.log('weiter')
    assert 'weiter' in ctx._test_logs
    assert stub._rpc.project_root == root, 'a reconnect keeps the launcher\'s project root'


def test_a_bad_reply_closes_the_socket_too(server, run):
    stub, session, ctx, _root = run
    # A reply the stub cannot read is the end of that connection.
    stub._rpc._read_reply = lambda: (_ for _ in ()).throw(stub.RobotError('kaputt'))
    with pytest.raises(stub.RobotError):
        stub.log('a')
    assert stub._rpc._sock is None
    del stub._rpc._read_reply
    stub.log('b')
    assert 'b' in ctx._test_logs


def test_a_greifziel_as_a_target_is_refused_in_german(server, run):
    stub, session, _ctx_, _root = run
    with session._lock:
        before = session.frames_decoded
    ziel = stub.Greifziel(0)
    for call in (stub.move_to, stub.pickup, stub.drop_at):
        with pytest.raises(stub.RobotError) as raised:
            call(ziel)
        assert str(raised.value) == robot_api.GREIFZIEL_AS_TARGET_DE, call.__name__
    with session._lock:
        assert session.frames_decoded == before, 'nothing was sent'
    assert robot_api.GREIFZIEL_AS_TARGET_DE == (
        'Ein Greifziel gehört nicht hierher — hier wird der Name eines Ziels oder ein Punkt '
        '(x, y, z) erwartet. Über ein gefundenes Objekt fährt robot.move_above(…), greifen '
        'tut robot.grasp(…), an seiner Stelle ablegen geht mit '
        'robot.drop_at(robot.object_position(…)).')


def _nested(depth):
    value: list = []
    for _ in range(depth):
        value = [value]
    return value


def test_an_unsendable_argument_is_refused_in_german(server, run):
    stub, session, ctx, _root = run
    # 10 000 deep: CPython 3.12's C encoder gives up between 5 000 and 10 000
    # levels (its C recursion limit, measured on the host and in the runner
    # image) with a RecursionError — which read as the `recursion` kind,
    # „Eine Funktion ruft sich ohne Ende selbst auf" (review-2 mi-7).
    for value in (b'x', 1j, {1, 2}, _nested(10_000)):
        with pytest.raises(stub.RobotError) as raised:
            stub.speak(value)
        assert str(raised.value) == robot_api.NOT_SENDABLE_DE, type(value)
    # A list the encoder CAN write reaches the robot, which refuses it with
    # the parameter's own sentence.
    with pytest.raises(stub.RobotError) as raised:
        stub.speak(_nested(2000))
    assert str(raised.value) == 'robot.speak: „text“ muss ein Text oder eine Zahl sein.'
    assert robot_api.NOT_SENDABLE_DE == (
        'Ein Wert in diesem Aufruf kann nicht an den Roboter geschickt werden — erlaubt sind '
        'Zahlen, Texte, Wahrheitswerte und Listen davon.')
    # Nothing was sent, so the connection still works.
    assert stub._rpc._sock is not None
    stub.log('noch da')
    assert 'noch da' in ctx._test_logs

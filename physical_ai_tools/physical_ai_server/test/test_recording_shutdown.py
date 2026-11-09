#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""A recording session survives the node's shutdown (launch plan step 8, 2026-11-10).

Three pieces, each tested where it can be run:

* ``data_processing/recording_shutdown.py`` (stdlib only): the bounded,
  KeyboardInterrupt-proof run of ``DataManager.end_for_shutdown`` on its own
  daemon thread, and the EPIPE-tolerant output streams;
* ``PhysicalAIServer._finalize_recording_on_shutdown``: takes the session over
  (generation bump, ``on_recording`` off under the publish lock) and calls the
  helper — gated on the DataManager, never on ``on_recording``;
* ``main()``: SIGINT set to SIG_DFL BEFORE rclpy.init (so SIGINT, like SIGTERM,
  only shuts the context down), and the finalize FIRST in the ``finally``.

``physical_ai_server.py`` imports rclpy and cannot be imported here, so its
methods are extracted by ``ast`` and exec'd onto a stub node (the
test_sensor_executor idiom). The DataManager side is tested in the deps-free
FSM suite (robotis_ai_setup/tests/test_data_manager_record_fsm.py) and, with
the real LeRobot writer and the real rclpy signal path, by
.github/scripts/record_fsm_smoke.py in the built image.
"""

from __future__ import annotations

import ast
import contextlib
import os
import signal
import sys
import textwrap
import threading
import time
import types
from pathlib import Path

import pytest

from physical_ai_server.data_processing import recording_shutdown as rs

_PKG = Path(__file__).resolve().parents[1] / 'physical_ai_server'
_SERVER_PY = _PKG / 'physical_ai_server.py'
_HELPER_PY = _PKG / 'data_processing' / 'recording_shutdown.py'
_SRC = _SERVER_PY.read_text(encoding='utf-8')
_CLASS = next(n for n in ast.parse(_SRC).body
              if isinstance(n, ast.ClassDef) and n.name == 'PhysicalAIServer')
_MAIN = next(n for n in ast.parse(_SRC).body
             if isinstance(n, ast.FunctionDef) and n.name == 'main')


@pytest.fixture(autouse=True)
def _keep_the_streams():
    saved = sys.stdout, sys.stderr
    yield
    sys.stdout, sys.stderr = saved


class _DataManager:
    """The end_for_shutdown surface, scripted."""

    SHUTDOWN_FINALIZED = 'finalized'
    SHUTDOWN_FAILED = 'failed'
    SHUTDOWN_ALREADY_FINAL = 'already_final'
    SHUTDOWN_NO_DATASET = 'no_dataset'

    def __init__(self, answer='finalized', sleep_s=0.0, raises=None, saved=2):
        self.answer = answer
        self.sleep_s = sleep_s
        self.raises = raises
        self.saved = saved
        self.calls = []

    def end_for_shutdown(self):
        self.calls.append(threading.current_thread().name)
        if self.sleep_s:
            time.sleep(self.sleep_s)
        if self.raises is not None:
            raise self.raises
        return self.answer

    def saved_episode_count(self):
        return self.saved


# ── the helper ───────────────────────────────────────────────────────────────

def test_the_budget_is_the_documented_one():
    assert rs.SHUTDOWN_FINALIZE_WAIT_S == 30.0


def test_the_helper_is_stdlib_only():
    tree = ast.parse(_HELPER_PY.read_text(encoding='utf-8'))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split('.')[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or '').split('.')[0])
    assert imported <= {'sys', 'threading', 'time'}, imported


def test_the_finalize_runs_on_its_own_thread_and_answers():
    dm = _DataManager('finalized', saved=2)
    out = rs.finalize_for_shutdown(dm, 5.0)
    assert out['verdict'] == 'finalized'
    assert out['saved'] == 2
    assert out['error'] == ''
    assert dm.calls == ['recording-shutdown-finalize']


def test_every_dm_answer_passes_through():
    for answer in ('finalized', 'failed', 'already_final', 'no_dataset'):
        assert rs.finalize_for_shutdown(_DataManager(answer), 5.0)['verdict'] == answer


def test_a_hang_ends_at_the_budget():
    dm = _DataManager(sleep_s=2.0)
    started = time.monotonic()
    out = rs.finalize_for_shutdown(dm, 0.2)
    took = time.monotonic() - started
    assert out['verdict'] == rs.VERDICT_TIMEOUT
    assert 0.15 <= took < 1.5
    worker = next(t for t in threading.enumerate() if t.name == 'recording-shutdown-finalize')
    assert worker.daemon            # a hang cannot keep the process alive


def test_an_exception_is_reported_never_raised():
    out = rs.finalize_for_shutdown(_DataManager(raises=RuntimeError('disk full')), 5.0)
    assert out['verdict'] == rs.VERDICT_ERROR
    assert out['error'] == 'RuntimeError: disk full'


def test_a_datamanager_without_the_method_is_unsupported():
    out = rs.finalize_for_shutdown(object(), 5.0)
    assert out['verdict'] == rs.VERDICT_UNSUPPORTED


def test_a_keyboardinterrupt_in_the_waiting_thread_never_cuts_the_finalize(monkeypatch):
    # The node sets SIGINT to SIG_DFL, so no KeyboardInterrupt reaches it; the
    # helper does not rely on that (the smoke test and any other caller).
    monkeypatch.setattr(signal, 'SIGINT', signal.SIGINT)
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        dm = _DataManager(sleep_s=0.6)
        threading.Timer(0.15, lambda: os.kill(os.getpid(), signal.SIGINT)).start()
        out = rs.finalize_for_shutdown(dm, 5.0)
    finally:
        signal.signal(signal.SIGINT, previous)
    assert out['verdict'] == 'finalized'
    assert out['seconds'] >= 0.5


def test_a_broken_output_pipe_cannot_abort_the_finalize():
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    broken = os.fdopen(write_fd, 'w', buffering=1)
    sys.stdout = sys.stderr = broken

    class _Printing(_DataManager):
        def end_for_shutdown(self):
            print('a line on the finalize path', file=sys.stderr, flush=True)
            print('another one')
            return 'finalized'

    out = rs.finalize_for_shutdown(_Printing(), 5.0)
    assert out['verdict'] == 'finalized'
    assert isinstance(sys.stdout, rs._TolerantStream)
    assert isinstance(sys.stderr, rs._TolerantStream)
    with contextlib.suppress(OSError):
        broken.close()


def test_making_output_tolerant_is_idempotent():
    rs.make_output_tolerant()
    first = sys.stdout
    rs.make_output_tolerant()
    assert sys.stdout is first
    assert sys.stdout.write('x') == 1


# ── the node hook ────────────────────────────────────────────────────────────

class _Logger:
    def __init__(self):
        self.lines = []

    def __getattr__(self, level):
        return lambda msg, *a, **k: self.lines.append((level, str(msg)))


class _CountingLock:
    def __init__(self):
        self.entered = 0
        self.held = False

    def __enter__(self):
        self.entered += 1
        self.held = True
        return self

    def __exit__(self, *exc):
        self.held = False
        return False


def _load_methods(names):
    ns = {
        'contextlib': contextlib,
        'recording_shutdown': rs,
        'DataManager': _DataManager,
    }
    for stmt in _CLASS.body:
        if isinstance(stmt, ast.FunctionDef) and stmt.name in names:
            src = textwrap.dedent(ast.get_source_segment(_SRC, stmt))
            exec(compile(src, str(_SERVER_PY), 'exec'), ns)  # noqa: S102
    missing = [n for n in names if n not in ns]
    assert not missing, f'methods not found: {missing}'
    return ns


_METHODS = ('_finalize_recording_on_shutdown', '_log_shutdown_line', '_record_tick_owns_session')
_NS = _load_methods(_METHODS)


class _Node:
    def __init__(self, data_manager=None, on_recording=True, gen=4):
        self.logger = _Logger()
        self.data_manager = data_manager
        self.on_recording = on_recording
        self._record_session_gen = gen
        self._collision_trip_pending = None
        self._record_publish_lock = _CountingLock()
        for name in _METHODS:
            setattr(self, name, types.MethodType(_NS[name], self))

    def get_logger(self):
        return self.logger


def test_no_datamanager_no_work():
    node = _Node(None)
    assert node._finalize_recording_on_shutdown() is None
    assert node._record_session_gen == 4
    assert node.on_recording is True


def test_the_session_is_taken_over_then_finalized():
    dm = _DataManager('finalized', saved=2)
    node = _Node(dm, gen=4)
    assert node._record_tick_owns_session(4)
    out = node._finalize_recording_on_shutdown(wait_s=5.0)
    assert out['verdict'] == 'finalized'
    assert len(dm.calls) == 1
    assert node.on_recording is False
    assert node._record_session_gen == 5
    assert node._record_publish_lock.entered == 1
    # a record tick that started for the old generation adds no frame now (F5)
    assert not node._record_tick_owns_session(4)
    level, line = node.logger.lines[-1]
    assert level == 'warning'
    assert 'finalized' in line and '2 saved episode(s)' in line and 'nothing uploaded' in line


def test_it_does_not_ask_on_recording():
    # A collision clears on_recording while the session lives: the DataManager
    # decides whether there is anything to finalize.
    dm = _DataManager('finalized')
    node = _Node(dm, on_recording=False)
    assert node._finalize_recording_on_shutdown(wait_s=5.0)['verdict'] == 'finalized'
    assert len(dm.calls) == 1


def test_nothing_at_stake_logs_at_debug():
    for answer in ('no_dataset', 'already_final'):
        node = _Node(_DataManager(answer))
        node._finalize_recording_on_shutdown(wait_s=5.0)
        assert node.logger.lines == [('debug', f'Shutdown: recording {answer}')]


def test_a_failure_logs_an_error():
    for dm in (_DataManager('failed'), _DataManager(raises=OSError('disk'))):
        node = _Node(dm)
        node._finalize_recording_on_shutdown(wait_s=5.0)
        assert node.logger.lines[-1][0] == 'error'


def test_a_broken_logger_never_raises():
    node = _Node(_DataManager('finalized'))

    def _broken():
        raise RuntimeError('context gone')
    node.get_logger = _broken
    assert node._finalize_recording_on_shutdown(wait_s=5.0)['verdict'] == 'finalized'


def test_the_hook_uses_the_documented_budget_by_default():
    hook = next(n for n in _CLASS.body if isinstance(n, ast.FunctionDef)
                and n.name == '_finalize_recording_on_shutdown')
    src = ast.get_source_segment(_SRC, hook)
    assert 'recording_shutdown.SHUTDOWN_FINALIZE_WAIT_S if wait_s is None else wait_s' in src
    called = {n.func.attr for n in ast.walk(hook)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not {c for c in called if 'publish' in c}, \
        'nothing is published on the shutdown path: the context is down'


# ── main() ───────────────────────────────────────────────────────────────────

def _main_src():
    return ast.get_source_segment(_SRC, _MAIN)


def test_sigint_is_sig_dfl_before_rclpy_installs_its_handler():
    calls = [ast.unparse(stmt.value.func) for stmt in _MAIN.body
             if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)]
    assert calls[:2] == ['signal.signal', 'rclpy.init'], calls
    first = _MAIN.body[[isinstance(s, ast.Expr) for s in _MAIN.body].index(True)]
    assert ast.unparse(first) == 'signal.signal(signal.SIGINT, signal.SIG_DFL)'


def test_the_finalize_is_the_first_thing_the_finally_does():
    src = _main_src()
    finally_src = src[src.index('finally:'):]
    first_call = next(line.strip() for line in finally_src.splitlines()[1:]
                      if line.strip() and not line.strip().startswith('#'))
    assert first_call == 'node._finalize_recording_on_shutdown()'
    finalize_at = finally_src.index('node._finalize_recording_on_shutdown()')
    for later in ('node._cleanup_hf_api_worker()', 'node._shutdown_sensor_executor()',
                  'executor.shutdown()', 'node.destroy_node()', 'rclpy.shutdown()'):
        assert finalize_at < finally_src.index(later), later


def test_the_sensor_supervisor_finalizes_before_its_exit():
    fail = next(n for n in _CLASS.body if isinstance(n, ast.FunctionDef)
                and n.name == '_note_sensor_failure')
    src = ast.get_source_segment(_SRC, fail)
    assert src.index("'_finalize_recording_on_shutdown'") < src.index('os._exit(')

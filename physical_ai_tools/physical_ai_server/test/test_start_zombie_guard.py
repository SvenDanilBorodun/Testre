#!/usr/bin/env python3
"""A stopped run must stay stopped — every path out of ``start()``.

``ctx.should_stop is self._stop_event.is_set``: ONE event, shared by every
thread of every run, so ``start()``'s ``clear()`` un-stops whatever survived the
last run.  ``stop()`` cannot prevent that — it sets the flag BEFORE joins that
are timeout-bounded (5.0 s main, 2.0 s per hat), and five raw
``acquire(timeout=10.0)`` sites (interpreter.py:1180, handlers/trajectory.py:445,
handlers/perception_blocks.py:232 + :252, handlers/motion.py:2422) can park a
thread for 10 s without ever polling stop.

Measured on the tree that only guarded ``_hat_threads``: a STOPPED run published
104 further waypoints after a new run had started, both writing
``/leader/joint_trajectory``; the old ``_run``'s ``finally`` then re-set the stop
event (killing the new run), nulled ``self._thread`` (so ``stop()`` answered
„Es läuft kein Workflow." for a live run) and fired ``_on_finished`` (releasing
``on_workflow``, so a recording could claim the arm).
"""
from __future__ import annotations

import ast
import json
import textwrap
import threading
import time
from pathlib import Path

import pytest

from physical_ai_server.workflow import workflow_manager as WM
from physical_ai_server.workflow.workflow_manager import WorkflowManager

_WM_PY = Path(WM.__file__)


def _ws(blocks, **siblings):
    payload = {'blocks': {'languageVersion': 0, 'blocks': blocks}}
    payload.update(siblings)
    return json.dumps(payload)


def _replay(name='Bewegung 1'):
    return {'type': 'edubotics_replay_trajectory', 'fields': {'NAME': name}}


def _wait(seconds):
    return {'type': 'edubotics_wait_seconds', 'fields': {'SECONDS': seconds}}


def _traj(n=60, name='Bewegung 1'):
    return {name: {'fps': 25,
                   'points': [[0.3 * i / (n - 1), -1.0, 1.0, 0.0, 0.0, 0.8, i * 0.04]
                              for i in range(n)]}}


def _chain(*blocks):
    head, cur = blocks[0], blocks[0]
    for nxt in blocks[1:]:
        cur['next'] = {'block': nxt}
        cur = nxt
    return head


# ── a zombie MAIN thread refuses the next start ──────────────────────────────

def test_a_zombie_main_thread_refuses_the_next_start():
    """No fake camera needed: handlers/trajectory.py:445 is a RAW
    ``lock.acquire(timeout=10.0)`` that never polls stop, so holding the motion
    lock parks the main daemon past ``stop()``'s 5 s join. ``_hat_threads`` is
    EMPTY there, so a guard that only looks at hats waves the start through and
    ``clear()`` un-stops the previous program."""
    published: list = []
    mgr = WorkflowManager(
        publisher=lambda pts: published.append(
            (time.monotonic(), threading.current_thread().name, len(pts))),
        emit_status=lambda _e: None,
        get_follower_joints=lambda: [0.0, -1.0, 1.0, 0.0, 0.0, 0.8],
    )
    release = threading.Event()
    threading.Thread(
        target=lambda: (mgr._motion_lock.acquire(),
                        release.wait(9.0), mgr._motion_lock.release()),
        daemon=True).start()
    time.sleep(0.2)

    assert mgr.start(_ws([_replay()], trajectories=_traj()), 'run1')[0] is True
    time.sleep(0.5)
    mgr.stop()                                   # the 5 s join TIMES OUT
    assert mgr._thread.is_alive(), 'precondition: the main daemon outlived stop()'
    assert mgr.is_running is False, 'is_running short-circuits on the stop flag'
    assert not any(t.is_alive() for t in mgr._hat_threads), 'no hats here'

    t_start2 = time.monotonic()
    ok, msg, _ = mgr.start(_ws([_chain(_wait(14))]), 'run2')

    assert ok is False, 'a start was accepted while the previous run was still running'
    assert 'Vorheriger Workflow läuft noch' in msg
    assert mgr._stop_event.is_set(), 'the refusal un-stopped the previous run'

    release.set()
    time.sleep(6.0)
    late = [p for p in published if p[0] > t_start2]
    assert late == [], f'{sum(p[2] for p in late)} waypoints published by a stopped run'
    mgr.stop()


# ── every refusal BELOW the clear puts the flag back ─────────────────────────

@pytest.mark.parametrize('kwargs,needle', [
    (dict(perception_factory=lambda: (_ for _ in ()).throw(RuntimeError('x'))),
     'Wahrnehmung'),
    (dict(ik_factory=lambda: (_ for _ in ()).throw(RuntimeError('x'))),
     'IK-Solver'),
])
def test_a_refusal_below_the_clear_leaves_the_stop_flag_alone(kwargs, needle):
    """The two factory refusals sit BELOW ``_stop_event.clear()`` and cannot be
    hoisted above it forever — so the region owns the flag and gives it back.
    ``_build_perception`` deliberately has no silent fallback
    (physical_ai_server.py: "if Perception() raises ... it propagates and
    WorkflowManager.start reports the German message"), so this is a real path."""
    mgr = WorkflowManager(publisher=lambda p: None, emit_status=lambda e: None,
                          **kwargs)
    mgr._stop_event.set()
    ok, msg, _ = mgr.start(_ws([_wait(0.1)]), 'wf')
    assert ok is False and needle in msg
    assert mgr._stop_event.is_set(), 'a refused start cleared the stop flag'


# ── a half-started spawn loop must not leave handlers running ────────────────

def test_a_failed_thread_start_does_not_leave_handlers_running(monkeypatch):
    """Hat threads are started BEFORE the main stack (deliberately — see
    ``start()``), so a ``RuntimeError("can't start new thread")`` part-way through
    the loop leaves handlers live with the flag CLEAR and ``self._thread`` never
    started: ``is_running`` is False, so ``stop()`` answers „Es läuft kein
    Workflow." and can never reach them."""
    real = threading.Thread.start

    def flaky(self):
        if self.name.endswith('-hat-h2'):
            raise RuntimeError("can't start new thread")
        return real(self)

    hats = [{'type': 'edubotics_when_broadcast', 'id': f'h{i}',
             'fields': {'EVENT_NAME': f'e{i}'},
             'next': {'block': _wait(0.05)}} for i in range(4)]
    mgr = WorkflowManager(publisher=lambda p: None, emit_status=lambda e: None)
    monkeypatch.setattr(threading.Thread, 'start', flaky)
    with pytest.raises(RuntimeError):
        mgr.start(_ws(hats), 'wf-flaky')
    monkeypatch.undo()

    assert mgr._stop_event.is_set(), (
        'handlers were left running with the stop flag clear and no way to stop them'
    )
    time.sleep(0.5)
    assert [t.name for t in mgr._hat_threads if t.is_alive()] == []


# ── the structural guard ─────────────────────────────────────────────────────

def test_the_clear_is_inside_a_try_whose_finally_restores_the_flag():
    """A COMMENT is not a guard. The invariant „no path below the clear may
    return or raise without restoring the flag" is enforced by a ``finally``, not
    by reviewer discipline — a static „no ``return`` between X and Y" scan would
    also miss every ``raise``. This pins the ``finally`` itself."""
    tree = ast.parse(_WM_PY.read_text(encoding='utf-8'))
    start = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == 'start')

    def _clears(node):
        return any(isinstance(c, ast.Call)
                   and isinstance(c.func, ast.Attribute) and c.func.attr == 'clear'
                   and isinstance(c.func.value, ast.Attribute)
                   and c.func.value.attr == '_stop_event'
                   for c in ast.walk(node))

    def _sets(node):
        return any(isinstance(c, ast.Call)
                   and isinstance(c.func, ast.Attribute) and c.func.attr == 'set'
                   and isinstance(c.func.value, ast.Attribute)
                   and c.func.value.attr == '_stop_event'
                   for c in ast.walk(node))

    tries = [n for n in ast.walk(start)
             if isinstance(n, ast.Try) and _clears(n) and n.finalbody
             and _sets(ast.Module(body=n.finalbody, type_ignores=[]))]
    assert tries, (
        'start() clears _stop_event outside a try/finally that restores it — '
        'a refusal or an exception below the clear now un-stops whatever '
        'survived the previous run'
    )


def test_the_start_guard_covers_the_main_thread_not_just_the_hats():
    """Pins that the liveness guard reads ``self._thread`` too. Cheap, and it is
    the one line whose removal reopens the 104-waypoint defect above."""
    src = textwrap.dedent(_WM_PY.read_text(encoding='utf-8'))
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == '_prev_run_threads_alive')
    body = ast.get_source_segment(src, fn) or ''
    assert 'self._thread' in body and '_hat_threads' in body

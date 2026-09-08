#!/usr/bin/env python3
"""„Schritt" after a Haltepunkt must execute ONE block, not resume the run.

``_pause_for_breakpoint`` ended with an unconditional ``ctx.set_paused(False)``,
which clears the pause flag AND sets the resume event — un-arming the pause that
``_wait_for_resume`` had just correctly re-armed for the step token.

Measured before the fix, breakpoint on b0 of a 10-block chain, five presses of
„Schritt": blocks executed [10, 0, 0, 0, 0], with the last four answering
„Workflow ist nicht pausiert.". The contract is [1, 1, 1, 1, 1]. 5/5 runs.

Stepping from the PAUSE button was correct throughout ([1, 2, 2, 3, 3] in the
original measurement), which is why this only ever showed up after a breakpoint —
and why the fix must not disturb it.
"""

from __future__ import annotations

import json
import time

from physical_ai_server.workflow.workflow_manager import WorkflowManager


CHAIN_LENGTH = 10


def _program() -> str:
    """A 10-block chain of trivial statements, each with a known id."""
    head = None
    for i in reversed(range(CHAIN_LENGTH)):
        block = {'type': 'variables_set', 'id': f'b{i}',
                 'fields': {'VAR': f'v{i}'},
                 'inputs': {'VALUE': {'block': {'type': 'text',
                                                'fields': {'TEXT': 'x'}}}}}
        if head is not None:
            block['next'] = {'block': head}
        head = block
    return json.dumps({'blocks': {'languageVersion': 0, 'blocks': [head]}})


def _manager():
    status: list[dict] = []
    mgr = WorkflowManager(publisher=lambda _p: None, emit_status=status.append)
    return mgr, status


def _executed(status) -> set[str]:
    return {e.get('current_block_id') for e in status
            if isinstance(e, dict) and e.get('phase') == 'done'
            and e.get('current_block_id')}


def _wait_paused(mgr, cap=5.0) -> bool:
    deadline = time.monotonic() + cap
    while time.monotonic() < deadline:
        if mgr.is_paused:
            return True
        time.sleep(0.01)
    return False


def test_each_step_after_a_breakpoint_executes_exactly_one_block():
    mgr, status = _manager()
    mgr.set_breakpoints(['b0'])
    ok, msg, _ = mgr.start(_program(), 'wf-step')
    assert ok, msg
    try:
        assert _wait_paused(mgr), 'the breakpoint never paused the run'
        time.sleep(0.2)
        counts = []
        for _ in range(5):
            before = len(_executed(status))
            stepped, message = mgr.step()
            assert stepped, message
            time.sleep(0.3)
            counts.append(len(_executed(status)) - before)
        assert counts == [1, 1, 1, 1, 1], (
            f'„Schritt" executed {counts} blocks per press — a value of '
            f'{CHAIN_LENGTH} means it resumed the whole run')
    finally:
        mgr.stop()


def test_the_run_stays_paused_between_steps():
    mgr, status = _manager()
    mgr.set_breakpoints(['b0'])
    mgr.start(_program(), 'wf-step')
    try:
        assert _wait_paused(mgr)
        time.sleep(0.2)
        for _ in range(3):
            assert mgr.is_paused, 'the pause was cleared by the previous step'
            ok, message = mgr.step()
            assert ok, message
            time.sleep(0.3)
        assert mgr.is_paused
    finally:
        mgr.stop()


def test_resume_after_a_breakpoint_still_runs_the_rest():
    """The other exit from _wait_for_resume must be unaffected."""
    mgr, status = _manager()
    mgr.set_breakpoints(['b0'])
    mgr.start(_program(), 'wf-step')
    try:
        assert _wait_paused(mgr)
        time.sleep(0.2)
        ok, message = mgr.resume()
        assert ok, message
        deadline = time.monotonic() + 5.0
        while mgr.is_running and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(_executed(status)) >= CHAIN_LENGTH
        assert not mgr.is_paused
    finally:
        mgr.stop()


def test_stepping_from_the_pause_button_is_unchanged():
    """Pause-button stepping never had the bug; the fix must not break it.

    Paused at a BREAKPOINT first, then resumed, so Pause always has a live run
    to land on — a bare `mgr.pause()` on the 10-block chain races the run to
    completion and would make this skip (i.e. never guard anything)."""
    mgr, status = _manager()
    mgr.set_breakpoints(['b0'])
    mgr.start(_program(), 'wf-step')
    try:
        assert _wait_paused(mgr)
        time.sleep(0.2)
        # Already paused by the breakpoint — pause() is idempotent here and
        # gives us the pause-button state without the race.
        ok, message = mgr.pause()
        assert ok, message
        time.sleep(0.2)
        before = len(_executed(status))
        stepped, message = mgr.step()
        assert stepped, message
        time.sleep(0.3)
        assert len(_executed(status)) - before <= 1
        assert mgr.is_paused
    finally:
        mgr.stop()


def test_stop_while_paused_at_a_breakpoint_still_ends_the_run():
    mgr, status = _manager()
    mgr.set_breakpoints(['b0'])
    mgr.start(_program(), 'wf-step')
    assert _wait_paused(mgr)
    time.sleep(0.2)
    started = time.monotonic()
    mgr.stop()
    assert time.monotonic() - started < 5.0
    assert not mgr.is_running
    terminal = [e.get('phase') for e in status
                if isinstance(e, dict)
                and e.get('phase') in ('finished', 'stopped', 'error')]
    assert terminal and terminal[-1] == 'stopped'


# ── the step TOKEN is ONE permission, not a broadcast ──────────────────────

def test_one_step_press_releases_exactly_one_parked_waiter():
    """`_pause_event.is_set() and _step_event.is_set()` then `.clear()` was a
    test-and-clear with nothing between them, so two threads parked on the gate
    could BOTH pass one press — two ``when_broadcast`` hats on one event, or a
    hat body plus a still-running main stack. Each extra release may run a
    motion block the student did not ask for.

    Measured against the REAL pre-fix bodies with 4 waiters, 120 trials: 0
    bad at the default switch interval, 6-7 bad (run to run) at
    ``sys.setswitchinterval(1e-6)``; the fixed gate is 0 at both. Rare, not
    impossible.

    The trial runs the real ``_wait_if_paused`` on N threads, fires ONE
    ``step()``, and counts the releases that happened BEFORE this trial's own
    stop signal. Timestamping against the stop is essential: ``stop()`` sets
    ``_step_event`` and ``_resume_event`` deliberately, to wake EVERY waiter so
    they can observe the stop flag — count those and any implementation
    "passes".
    """
    import sys
    import threading

    waiters = 4
    trials = 40
    original_interval = sys.getswitchinterval()
    # The race is a two-instruction window; make the scheduler hostile to it.
    sys.setswitchinterval(1e-6)
    try:
        for _ in range(trials):
            mgr, _status = _manager()
            # is_running is what pause()/step() gate on; there is no workflow
            # thread in this trial, only the gate.
            mgr._thread = threading.current_thread()
            assert mgr.is_running

            assert mgr.pause()[0] is True
            released: list[float] = []
            released_lock = threading.Lock()
            ready = threading.Barrier(waiters + 1)

            def waiter():
                ready.wait()
                mgr._wait_if_paused()
                with released_lock:
                    released.append(time.monotonic())

            threads = [threading.Thread(target=waiter, daemon=True)
                       for _ in range(waiters)]
            for t in threads:
                t.start()
            ready.wait()
            # Let every thread reach the gate before the single token lands.
            time.sleep(0.02)

            assert mgr.step()[0] is True
            time.sleep(0.05)
            stop_at = time.monotonic()
            # Release the rest so the trial can join; anything after stop_at is
            # the stop, not the step.
            mgr._stop_event.set()
            mgr._resume_event.set()
            mgr._step_event.set()
            for t in threads:
                t.join(timeout=2.0)
                assert not t.is_alive()

            with released_lock:
                by_the_step = [t for t in released if t <= stop_at]
            assert len(by_the_step) == 1, (
                f'one „Schritt" released {len(by_the_step)} waiters')
    finally:
        sys.setswitchinterval(original_interval)


def test_the_token_is_not_a_counter():
    """Rapid presses must COLLAPSE, not accumulate. A Semaphore would hand out
    one release per press and let a student bank steps; and ``stop()`` must
    still be able to wake EVERY waiter with a single ``_step_event.set()``."""
    import threading

    mgr, _status = _manager()
    mgr._thread = threading.current_thread()
    assert mgr.pause()[0] is True
    for _ in range(5):
        assert mgr.step()[0] is True
    assert mgr._consume_step_token() is True
    assert mgr._consume_step_token() is False


def test_a_step_token_is_only_consumable_while_paused():
    import threading

    mgr, _status = _manager()
    mgr._thread = threading.current_thread()
    assert mgr.step() == (False, 'Workflow ist nicht pausiert.')
    assert mgr._consume_step_token() is False
    mgr.pause()
    mgr.step()
    mgr.resume()
    # „Fortsetzen" cleared the pause; the outstanding token must not fire.
    assert mgr._consume_step_token() is False

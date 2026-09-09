#!/usr/bin/env python3
"""ONE discipline on ``ctx.motion_lock``, at every site, and no bound.

Two findings, one fix, because they are the same one seen from both ends.

**The starvation.** A RE-ENTERING timed acquire loses every handoff to a thread
parked in a BLOCKING acquire. Measured 2026-09-09 on a bare ``threading.RLock``
with no repo code (1 hog holding 1 s at a time, 12 s window)::

    1 blocking waiter + 1 polling waiter, slice 0.05 s -> blocking 12, polling  0
                                          slice 0.50 s -> blocking 12, polling  5
    2 polling waiters (uniform)                        -> 11 and 20
    2 blocking waiters (uniform)                       -> 22

An intermediate revision of this branch converted the composite motions to a
poll loop and left ``workflow_manager._run_hat_handler``'s
``with ctx.motion_lock:`` blocking. That single asymmetry turned an ordinary
event program — one arm-moving „wenn … empfangen" hat plus an arm-moving main
loop — from ``finished`` 3/3 into ``error`` 3/3.

**The stopwatch.** With a bound, a breakpoint inside a hat body pauses while
that handler holds the lock, so the queued main stack died exactly one bound
later (measured 12.54 − 2.53 = 10.01 s, 3/3) — with a German message blaming the
student for something they did not do, while the debugger UI still said
„pausiert". A breakpoint on a MAIN-program block is harmless (still paused at a
60 s cap, 3/3); it is specifically the hat body.

So: every acquire goes through ``motion._hold_motion_lock``; it waits, warns
once and never raises except „Workflow wurde gestoppt."; and it says nothing
while a human has the run paused.

WHY THE AST FENCE IS THE KILLER for "site 8 went back to a blocking ``with``",
and not an end-to-end outcome test: once the bound is gone, the starvation no
longer KILLS the run, it only delays it — so no terminal-phase assertion can see
it any more. The structure is the only thing left that can.
"""

from __future__ import annotations

import ast
import json
import threading
import time
from pathlib import Path

import pytest

from physical_ai_server.workflow import interpreter as INTERP
from physical_ai_server.workflow import workflow_manager as WM
from physical_ai_server.workflow.handlers import motion
from physical_ai_server.workflow.handlers import perception_blocks as PB
from physical_ai_server.workflow.handlers import trajectory as TRAJ
from physical_ai_server.workflow.handlers.motion import WorkflowError
from physical_ai_server.workflow.object_catalog import parse_catalog
from physical_ai_server.workflow.workflow_manager import WorkflowManager


_WORKFLOW_DIR = Path(WM.__file__).parent


# ══════════════════════════════════════════════════════════════════════════
# helpers
# ══════════════════════════════════════════════════════════════════════════

class _Ctx:
    """The minimum a lock-site needs: a lock, a log, a stop and a pause."""

    def __init__(self):
        self.motion_lock = threading.RLock()
        self.logs: list[str] = []
        self._stop = False
        self._paused = False
        self.published: list = []

    # ctx surface
    def log(self, msg):
        self.logs.append(msg)

    def should_stop(self):
        return self._stop

    def is_paused(self):
        return self._paused

    def publisher(self, pts):
        self.published.append(pts)


class _CyclingHog:
    """Takes the lock, holds it, drops it, repeats — ``holds`` times.

    The suite's other holder (``test_motion_robustness._Holder``) takes the lock
    ONCE and keeps it, so no test in the tree ever created a second thread
    RE-taking it. That is exactly why nothing caught the starvation.
    """

    def __init__(self, ctx, holds=6, hold_s=0.1, gap_s=0.01):
        self._ctx, self._holds = ctx, holds
        self._hold_s, self._gap_s = hold_s, gap_s
        self.done = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        for _ in range(self._holds):
            with self._ctx.motion_lock:
                time.sleep(self._hold_s)
            time.sleep(self._gap_s)
        self.done.set()

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *_exc):
        self.done.wait(20.0)
        self._t.join(20.0)
        return False


def _ws(blocks, **siblings):
    payload = {'blocks': {'languageVersion': 0, 'blocks': blocks}}
    payload.update(siblings)
    return json.dumps(payload)


def _chain(*blocks):
    head = cur = blocks[0]
    for nxt in blocks[1:]:
        cur['next'] = {'block': nxt}
        cur = nxt
    return head


def _manager():
    status: list[dict] = []
    published: list = []
    mgr = WorkflowManager(
        publisher=published.append,
        emit_status=status.append,
        get_follower_joints=lambda: [0.0, -1.0, 1.0, 0.0, 0.0, 0.8],
    )
    return mgr, status, published


def _phase(status):
    terminal = [e.get('phase') for e in status
                if isinstance(e, dict)
                and e.get('phase') in ('finished', 'stopped', 'error')]
    return terminal[-1] if terminal else None


def _logs(status):
    return [e['log_message'] for e in status
            if isinstance(e, dict) and e.get('log_message')]


def _wait_paused(mgr, cap=10.0):
    deadline = time.monotonic() + cap
    while time.monotonic() < deadline:
        if mgr.is_paused:
            return True
        time.sleep(0.01)
    return False


# ══════════════════════════════════════════════════════════════════════════
# A-4 — one helper, every site
# ══════════════════════════════════════════════════════════════════════════

def test_every_motion_lock_acquire_goes_through_the_one_helper():
    """THE fence. A ninth site is how this became eight.

    Two shapes are forbidden anywhere in ``workflow/``:
      * ``with <anything>.motion_lock:`` — the blocking waiter that starved
        every polling one (12 acquisitions against 0);
      * ``<lock>.acquire(...)`` where ``<lock>`` was bound from
        ``getattr(ctx, 'motion_lock', ...)``.

    ``.release()`` is deliberately NOT forbidden: ``_release_motion_lock`` and
    the three deliberate release-for-the-wait sites (``wait_seconds``,
    ``_poll_until``, ``_exec_wait_until``) all need it, and releasing has never
    been the hazard.
    """
    offenders: list[str] = []
    for path in sorted(_WORKFLOW_DIR.rglob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for node in ast.walk(tree):
            # (a) `with ... .motion_lock:`
            if isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    expr = item.context_expr
                    if isinstance(expr, ast.Attribute) and expr.attr == 'motion_lock':
                        offenders.append(
                            f'{path.name}:{node.lineno} with ....motion_lock:')
            # (b) a function that binds the lock and then acquires it
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == '_hold_motion_lock':
                    continue          # the ONE sanctioned acquire
                bound: set[str] = set()
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Assign)
                            and isinstance(sub.value, ast.Call)
                            and isinstance(sub.value.func, ast.Name)
                            and sub.value.func.id == 'getattr'
                            and len(sub.value.args) >= 2
                            and isinstance(sub.value.args[1], ast.Constant)
                            and sub.value.args[1].value == 'motion_lock'):
                        for tgt in sub.targets:
                            if isinstance(tgt, ast.Name):
                                bound.add(tgt.id)
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Call)
                            and isinstance(sub.func, ast.Attribute)
                            and sub.func.attr == 'acquire'
                            and isinstance(sub.func.value, ast.Name)
                            and sub.func.value.id in bound):
                        offenders.append(
                            f'{path.name}:{sub.lineno} {node.name}() acquires '
                            'the motion lock directly')
    assert offenders == [], (
        'a motion-lock acquire outside motion._hold_motion_lock — a mixed '
        'discipline on this lock starves the polling waiters completely:\n  '
        + '\n  '.join(offenders))


def test_the_helper_is_used_at_all_eight_sites():
    """The fence above proves nobody acquires it the OLD way; this proves the
    sites still take the lock at all — a site that simply dropped the acquire
    would pass the fence while re-opening the race the lock exists to close."""
    expected = {
        # _publish_motion, _execute_pickup, drop_at, + _reacquire_after_release's
        # `stop_raises=False` wrapper
        'handlers/motion.py': 4,
        'handlers/perception_blocks.py': 2,  # grasp_object, _check_grasp_held_locked
        'handlers/trajectory.py': 1,         # replay_trajectory
        'workflow_manager.py': 1,            # _run_hat_handler  ← THE one that was missing
    }
    for rel, count in expected.items():
        tree = ast.parse((_WORKFLOW_DIR / rel).read_text(encoding='utf-8'))
        calls = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = (fn.id if isinstance(fn, ast.Name)
                    else fn.attr if isinstance(fn, ast.Attribute) else None)
            if name == '_hold_motion_lock':
                calls += 1
        assert calls == count, (
            f'{rel} takes the motion lock {calls}× — expected {count}')


def test_uniform_polling_waiters_all_get_the_lock():
    """Two waiters, both through the helper, against a hog that keeps RE-taking
    the lock. Both make progress. (Mixed disciplines are what do not: 12 vs 0.)"""
    ctx = _Ctx()
    got = {'a': 0, 'b': 0}

    def waiter(key):
        for _ in range(3):
            acq = motion._hold_motion_lock(ctx, notice_s=5.0)
            got[key] += 1
            time.sleep(0.01)
            motion._release_motion_lock(ctx, acq)

    with _CyclingHog(ctx, holds=6, hold_s=0.1):
        ts = [threading.Thread(target=waiter, args=(k,)) for k in ('a', 'b')]
        for t in ts:
            t.start()
        for t in ts:
            t.join(20.0)
    assert got == {'a': 3, 'b': 3}, f'a waiter was starved: {got}'


def test_a_queued_third_motion_completes_instead_of_erroring():
    """The S20 shape. Three motions queue on one lock for far longer than the
    notice; all three complete.

    *Kills:* re-adding a bound driven by ``MOTION_LOCK_NOTICE_S`` — the third
    waiter queues ~0.6 s behind a 0.1 s notice and would die at it."""
    ctx = _Ctx()
    prev = motion.MOTION_LOCK_NOTICE_S
    motion.MOTION_LOCK_NOTICE_S = 0.1
    done: list[str] = []
    errors: list[str] = []

    def motion_thread(name):
        try:
            acq = motion._hold_motion_lock(ctx)
            try:
                time.sleep(0.3)
                done.append(name)
            finally:
                motion._release_motion_lock(ctx, acq)
        except Exception as e:                # noqa: BLE001
            errors.append(f'{name}: {type(e).__name__}: {e}')

    try:
        ts = [threading.Thread(target=motion_thread, args=(n,))
              for n in ('first', 'second', 'third')]
        for t in ts:
            t.start()
            time.sleep(0.02)
        for t in ts:
            t.join(20.0)
    finally:
        motion.MOTION_LOCK_NOTICE_S = prev
    assert errors == [], f'a queued motion died instead of waiting: {errors}'
    assert sorted(done) == ['first', 'second', 'third']


@pytest.mark.parametrize('site', ['helper', 'check_grasp_held', 'replay',
                                  'grasp_object'])
def test_stop_answers_immediately_at_every_lock_site(site):
    """Behind a holder, Stop must answer in well under a second AND say
    „Workflow wurde gestoppt.".

    Before the change: ``grasp_object`` and the hat body were UNBOUNDED and took
    29.70 s behind a 30 s holder; ``_check_grasp_held_locked`` and
    ``replay_trajectory`` capped at the bound and answered a Stop with a
    „Bewegung blockiert" LOCK ERROR advising a restart."""
    ctx = _Ctx()
    ctx.trajectories = {'Bewegung 1': {
        'fps': 25,
        'points': [[0.3 * i / 9, -1.0, 1.0, 0.0, 0.0, 0.8, i * 0.04]
                   for i in range(10)]}}
    ctx.last_full_joints = [0.0, -1.0, 1.0, 0.0, 0.0, 0.8]
    ctx.last_arm_joints = [0.0, -1.0, 1.0, 0.0, 0.0]
    ctx.get_follower_joints = lambda: [0.0, -1.0, 1.0, 0.0, 0.0, 0.8]
    ctx.last_commanded_close_rad = None

    ctx.object_catalog = parse_catalog({
        'tag_size_m': 0.024,
        'types': {'wuerfel': {'label_de': 'Würfel', 'tag_ids': [20, 21],
                              'object_height_m': 0.030, 'grasp_depth_m': 0.015,
                              'gripper_close_rad': -0.5,
                              'approach_clear_m': 0.06}},
    })
    ctx.object_catalog_error = None
    calls = {
        'helper': lambda: motion._hold_motion_lock(ctx),
        'check_grasp_held': lambda: PB._check_grasp_held_locked(ctx),
        'replay': lambda: TRAJ.replay_trajectory(ctx, {'name': 'Bewegung 1'}),
        'grasp_object': lambda: PB.grasp_object(ctx, {'object_type': 'wuerfel'}),
    }
    with _CyclingHog(ctx, holds=1, hold_s=3.0):
        time.sleep(0.1)
        ctx._stop = True
        t0 = time.monotonic()
        with pytest.raises(WorkflowError) as exc:
            calls[site]()
        waited = time.monotonic() - t0
    assert 'gestoppt' in str(exc.value), (
        f'{site} answered a Stop with {str(exc.value)!r}')
    assert waited < 0.6, f'{site} took {waited:.2f} s to answer Stop'


def test_the_reacquire_never_raises_and_never_leaks_a_runtime_error():
    """Sites 6 and 7 — the two ``finally`` re-acquires. Driven end to end, the
    old bounded-then-raise form leaked ``RuntimeError: cannot release
    un-acquired lock`` past ``_run_hat_handler``'s inner
    ``except (WorkflowError, InterpreterError)`` into its OUTER bare
    ``except Exception: return``: hat dead, no message, no error count, no
    retirement warning, run green. And an in-flight Stop was MASKED."""
    prev = motion.MOTION_LOCK_NOTICE_S
    prev_cap = INTERP.WAIT_UNTIL_MAX_SECONDS
    motion.MOTION_LOCK_NOTICE_S = 0.1
    results = {}
    # The hog is DRIVEN, not timed: it takes the lock the moment the site
    # releases it and is still holding when the site tries to take it back.
    # A wall-clock sleep here made the mutation „re-add the bounded raise"
    # SURVIVE — the hog had not taken the lock yet at the reacquire.
    try:
        for label, run in (
            ('poll_until',
             lambda ctx, go: PB._poll_until(ctx, go.is_set, 5.0, 'x')),
            ('exec_wait_until',
             lambda ctx, go: INTERP.Interpreter([])._exec_wait_until(
                 {'type': 'edubotics_wait_until',
                  'inputs': {'BOOL': {'block': {'type': 'logic_boolean',
                                                'fields': {'BOOL': 'FALSE'}}}}},
                 ctx, lambda *a: None)),
        ):
            INTERP.WAIT_UNTIL_MAX_SECONDS = 60.0
            ctx = _Ctx()
            escaped, inner = [], []
            ready = threading.Event()
            go = threading.Event()
            hog_has_it = threading.Event()
            let_go = threading.Event()

            def hog(ctx=ctx):
                with ctx.motion_lock:
                    hog_has_it.set()
                    let_go.wait(20.0)

            def hat_handler(ctx=ctx, run=run, escaped=escaped, inner=inner,
                            ready=ready, go=go):
                try:
                    acquired = motion._hold_motion_lock(ctx)
                    try:
                        ready.set()
                        try:
                            run(ctx, go)
                        except Exception as e:          # noqa: BLE001 — inner arm
                            inner.append(f'{type(e).__name__}: {e}')
                    finally:
                        motion._release_motion_lock(ctx, acquired)
                except Exception as e:                  # noqa: BLE001 — outer arm
                    escaped.append(f'{type(e).__name__}: {e}')

            h = threading.Thread(target=hat_handler)
            h.start()
            assert ready.wait(5.0), f'{label}: the handler never took the lock'
            g = threading.Thread(target=hog)
            g.start()
            assert hog_has_it.wait(5.0), (
                f'{label}: the site never RELEASED the lock for its wait')
            if label == 'exec_wait_until':
                # its condition is a constant FALSE; the cap is what returns it
                INTERP.WAIT_UNTIL_MAX_SECONDS = 0.05
            go.set()
            # …and now the site wants the lock back while the hog still has it,
            # for far longer than any bound anybody might re-add.
            time.sleep(0.6)
            let_go.set()
            h.join(20.0)
            g.join(20.0)
            results[label] = (escaped, inner, list(ctx.logs))
    finally:
        motion.MOTION_LOCK_NOTICE_S = prev
        INTERP.WAIT_UNTIL_MAX_SECONDS = prev_cap

    for label, (escaped, inner, logs) in results.items():
        assert escaped == [], f'{label}: escaped its caller with {escaped}'
        assert inner == [], f'{label}: raised out of its own finally: {inner}'
        assert not any('zurückgewonnen' in m for m in logs), (
            f'{label}: the un-recoverable-lock message is back: {logs}')


def test_the_slow_queue_says_so_once_and_only_when_it_is_true():
    """One [WARNUNG] per wait, not one per 50 ms poll — and none at all when the
    lock is free."""
    ctx = _Ctx()
    acq = motion._hold_motion_lock(ctx, notice_s=0.05)
    motion._release_motion_lock(ctx, acq)
    assert ctx.logs == [], f'an uncontended acquire warned: {ctx.logs}'

    with _CyclingHog(ctx, holds=1, hold_s=0.8):
        time.sleep(0.05)
        acq = motion._hold_motion_lock(ctx, notice_s=0.05)
        motion._release_motion_lock(ctx, acq)
    warns = [m for m in ctx.logs if 'gleichzeitig' in m]
    assert len(warns) == 1, (
        f'0.75 s of waiting at a 0.05 s poll produced {len(warns)} warnings')


# ══════════════════════════════════════════════════════════════════════════
# A-5 — the pause
# ══════════════════════════════════════════════════════════════════════════

def test_no_busy_warning_while_paused():
    """A queue behind a PAUSED program is not a busy arm — it is a human
    looking at their program. Saying „Zwei Teile des Programms wollen den Arm
    gleichzeitig bewegen" there blames the student for pressing Pause."""
    ctx = _Ctx()
    ctx._paused = True
    warned_while_paused = threading.Event()

    def waiter():
        acq = motion._hold_motion_lock(ctx, notice_s=0.05)
        motion._release_motion_lock(ctx, acq)

    with _CyclingHog(ctx, holds=1, hold_s=0.6):
        time.sleep(0.05)
        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.35)
        if any('gleichzeitig' in m for m in ctx.logs):
            warned_while_paused.set()
        ctx._paused = False
        t.join(20.0)
    assert not warned_while_paused.is_set(), (
        f'warned about a busy arm while the run was paused: {ctx.logs}')


def test_the_pause_only_suppresses_the_notice_it_does_not_suppress_stop():
    """A paused run must still answer Stop — otherwise „Pause, then Stopp"
    wedges the thread."""
    ctx = _Ctx()
    ctx._paused = True
    ctx._stop = True
    with _CyclingHog(ctx, holds=1, hold_s=1.0):
        time.sleep(0.05)
        t0 = time.monotonic()
        with pytest.raises(WorkflowError) as exc:
            motion._hold_motion_lock(ctx, notice_s=0.05)
        waited = time.monotonic() - t0
    assert 'gestoppt' in str(exc.value)
    assert waited < 0.6, f'Stop waited {waited:.2f} s behind a paused run'


def test_ctx_is_paused_is_wired_to_the_managers_own_predicate():
    """The ctx field exists AND ``start()`` fills it from the manager's
    non-consuming ``is_paused`` property — not from ``_wait_for_resume`` (which
    calls ``_consume_step_token()``) and not from ``wait_if_paused`` (which
    blocks). ``motion._hold_motion_lock`` asks this every 50 ms while queueing.

    *Kills:* deleting the ctx field, dropping the keyword from ``start()``, and
    re-pointing it at either consuming predicate."""
    from physical_ai_server.workflow.workflow_manager import WorkflowContext
    assert 'is_paused' in WorkflowContext.__dataclass_fields__, (
        'ctx.is_paused is gone — _hold_motion_lock cannot tell a PAUSED run '
        'from a busy arm, and the notice blames the student for pressing Pause')
    assert WorkflowContext(publisher=lambda _p: None).is_paused() is False, (
        'the default must be "not paused" so every non-Roboter-Studio ctx and '
        'every test double behaves exactly as before')

    tree = ast.parse((_WORKFLOW_DIR / 'workflow_manager.py')
                     .read_text(encoding='utf-8'))
    wired = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == 'WorkflowContext'):
            for kw in node.keywords:
                if kw.arg == 'is_paused':
                    wired.append(ast.dump(kw.value))
    assert len(wired) == 1, (
        f'start() wires ctx.is_paused {len(wired)}× — expected exactly once')
    assert "attr='is_paused'" in wired[0], (
        'ctx.is_paused is not the manager\'s own non-consuming predicate: '
        f'{wired[0]}')
    for consuming in ('_wait_for_resume', '_consume_step_token',
                      '_wait_if_paused'):
        assert consuming not in wired[0], (
            f'ctx.is_paused was wired to {consuming}, which EATS the '
            'student\'s „Schritt" token from a 50 ms lock-wait loop')


def test_is_paused_is_wired_and_does_not_consume_a_step_token():
    """``ctx.is_paused`` had to be a NEW predicate: ``wait_for_resume`` calls
    ``_consume_step_token()``, which atomically clears the single „Schritt"
    token AND ``_resume_event``, and ``wait_if_paused`` blocks. Wiring
    ``is_paused`` to either would eat the student's press from a lock-wait loop
    that runs every 50 ms.

    Ten probes, then ONE „Schritt": exactly one block runs."""
    status: list[dict] = []
    mgr = WorkflowManager(publisher=lambda _p: None, emit_status=status.append)
    head = None
    for i in reversed(range(6)):
        blk = {'type': 'variables_set', 'id': f'b{i}', 'fields': {'VAR': f'v{i}'},
               'inputs': {'VALUE': {'block': {'type': 'text',
                                              'fields': {'TEXT': 'x'}}}}}
        if head is not None:
            blk['next'] = {'block': head}
        head = blk
    mgr.set_breakpoints(['b0'])
    ok, msg, _ = mgr.start(_ws([head]), 'wf-pause')
    assert ok, msg
    try:
        assert _wait_paused(mgr), 'the breakpoint never paused the run'
        time.sleep(0.2)
        for _ in range(10):
            assert mgr.is_paused is True
        executed = {e.get('current_block_id') for e in status
                    if isinstance(e, dict) and e.get('phase') == 'done'
                    and e.get('current_block_id')}
        before = len(executed)
        stepped, message = mgr.step()
        assert stepped, message
        time.sleep(0.4)
        executed = {e.get('current_block_id') for e in status
                    if isinstance(e, dict) and e.get('phase') == 'done'
                    and e.get('current_block_id')}
        assert len(executed) - before == 1, (
            'the ten is_paused() probes ate the „Schritt" token — it is '
            'consuming')
    finally:
        mgr.stop()


def test_a_breakpoint_inside_a_hat_does_not_kill_the_main_stack():
    """THE flagship. A single Alt+Click on a block inside a „wenn …"-Block used
    to end the run: the pause is taken while ``_run_hat_handler`` holds
    ``ctx.motion_lock``, and the main stack's next motion died exactly one bound
    later (12.54 − 2.53 = 10.01 s, 3/3) — telling the student not to put arm
    movement in a „wenn …"-Block, which is not what they did.

    A breakpoint on a MAIN-program block was always harmless (still paused at a
    60 s cap, 3/3), so the defect is specifically the hat body.

    *Kills:* re-adding a bound (the notice is 0.3 s here, so a bound driven by
    it would fire long before the 1.5 s pause ends) and un-wiring
    ``ctx.is_paused`` (the „busy arm" warning would fire about a paused run)."""
    prev_notice = motion.MOTION_LOCK_NOTICE_S
    prev_keepalive = WM.HAT_KEEPALIVE_MAX_S
    motion.MOTION_LOCK_NOTICE_S = 0.3
    WM.HAT_KEEPALIVE_MAX_S = 8.0
    mgr, status, published = _manager()
    hat = {'type': 'edubotics_when_broadcast', 'fields': {'EVENT_NAME': 'go'},
           'next': {'block': _chain(
               {'type': 'variables_set', 'id': 'inhat',
                'fields': {'VAR': 'h'},
                'inputs': {'VALUE': {'block': {'type': 'text',
                                               'fields': {'TEXT': 'x'}}}}},
               {'type': 'edubotics_open_gripper'})}}
    main = _chain({'type': 'edubotics_broadcast', 'fields': {'EVENT_NAME': 'go'}},
                  {'type': 'edubotics_wait_seconds', 'fields': {'SECONDS': 0.4}},
                  {'type': 'edubotics_open_gripper'},
                  {'type': 'edubotics_open_gripper'})
    mgr.set_breakpoints(['inhat'])
    try:
        ok, msg, _ = mgr.start(_ws([main, hat]), 'wf-hat-bp')
        assert ok, msg
        assert _wait_paused(mgr), 'the hat-body breakpoint never paused the run'
        # Sit on the breakpoint far longer than the notice — a student reading
        # the Variablen panel. Nothing may die here.
        time.sleep(1.5)
        assert _phase(status) != 'error', (
            'the run died while the student was looking at a breakpoint: '
            f'{[m for m in _logs(status)]}')
        during_pause = _logs(status)
        ok, message = mgr.resume()
        assert ok, message
        deadline = time.monotonic() + 15.0
        while mgr.is_running and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        mgr.stop()
        motion.MOTION_LOCK_NOTICE_S = prev_notice
        WM.HAT_KEEPALIVE_MAX_S = prev_keepalive
    assert _phase(status) != 'error', (
        f'the run errored after the resume: {_logs(status)}')
    assert any('Haltepunkt' in m for m in during_pause), (
        f'the breakpoint was never reported: {during_pause}')
    # A queue AFTER the resume is legitimate and may warn; a queue DURING the
    # pause must not, because the arm is not busy — a human stopped it.
    assert not any('gleichzeitig' in m for m in during_pause), (
        'the queued main stack blamed the student for a busy arm while their '
        f'program was PAUSED: {during_pause}')


def test_the_arm_does_not_move_while_paused():
    """The fix I rejected, pinned so nobody re-derives it.

    „Release the motion lock during the pause" lets the main stack acquire and
    PUBLISH A MOTION while the student believes the program is stopped —
    ``ctx.wait_if_paused`` runs only at BLOCK BOUNDARIES (three sites in
    interpreter.py), so a main stack already inside a motion block would drive
    the arm. Never release the arm to a pause."""
    prev_notice = motion.MOTION_LOCK_NOTICE_S
    prev_keepalive = WM.HAT_KEEPALIVE_MAX_S
    motion.MOTION_LOCK_NOTICE_S = 0.3
    WM.HAT_KEEPALIVE_MAX_S = 8.0
    mgr, status, published = _manager()
    hat = {'type': 'edubotics_when_broadcast', 'fields': {'EVENT_NAME': 'go'},
           'next': {'block': _chain(
               {'type': 'edubotics_open_gripper'},
               {'type': 'variables_set', 'id': 'inhat',
                'fields': {'VAR': 'h'},
                'inputs': {'VALUE': {'block': {'type': 'text',
                                               'fields': {'TEXT': 'x'}}}}},
               {'type': 'edubotics_open_gripper'})}}
    main = _chain({'type': 'edubotics_broadcast', 'fields': {'EVENT_NAME': 'go'}},
                  {'type': 'edubotics_wait_seconds', 'fields': {'SECONDS': 0.4}},
                  {'type': 'edubotics_open_gripper'},
                  {'type': 'edubotics_open_gripper'})
    mgr.set_breakpoints(['inhat'])
    try:
        ok, msg, _ = mgr.start(_ws([main, hat]), 'wf-hat-bp-move')
        assert ok, msg
        assert _wait_paused(mgr)
        time.sleep(0.3)
        frozen = len(published)
        time.sleep(1.2)
        assert len(published) == frozen, (
            f'{len(published) - frozen} waypoint chunks were published while '
            'the program was paused — the lock was released to the pause')
        mgr.resume()
        deadline = time.monotonic() + 15.0
        while mgr.is_running and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        mgr.stop()
        motion.MOTION_LOCK_NOTICE_S = prev_notice
        WM.HAT_KEEPALIVE_MAX_S = prev_keepalive
    assert len(published) > frozen, (
        'nothing moved after the resume — the test proved nothing')

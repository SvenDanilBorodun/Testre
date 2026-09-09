#!/usr/bin/env python3
"""Hat blocks are the program too — the run must be alive while they are.

Every defect guarded here shares one root: ``_run``'s ``finally`` set the stop
event the instant the MAIN stack returned, and hat handlers had no rate floor,
no error recovery and no diagnostics of their own. Measured before the fix,
driving the real WorkflowManager with real threads:

  „sende Ereignis" as block 1 + a matching hat      0 of 20 firings
  a program made only of hats                       'finished' in 0.157 s,
                                                    0 firings, 10/10 per hat type
  a hat body in flight when main ends                cut at 45 of 190 waypoints
  one error in a hat body                            handler silent for the whole
                                                     run, arm LEFT HOLDING a cube,
                                                     phase='finished', 5/5
  a hat that re-broadcasts its own event             570 211 bodies /
                                                     2 851 066 status msgs in 2 s,
                                                     71 % of a core
  16 empty-named hats                                110 % of a core

The tests run against a plain WorkflowManager (no ROS, no arm): the lifecycle is
pure threading, and driving it directly is what makes these deterministic.
"""

from __future__ import annotations

import json
import re
import threading
import time
import types
from pathlib import Path

import pytest

from physical_ai_server.workflow import workflow_manager as WM
from physical_ai_server.workflow.workflow_manager import WorkflowManager


# Keep the post-main keep-alive short enough for a test suite. Production ships
# HAT_KEEPALIVE_MAX_S = 300 s; every assertion below is about the SHAPE (does the
# run stay alive at all, does the body finish), not the exact number.
KEEPALIVE_S = 3.0


@pytest.fixture(autouse=True)
def _short_keepalive(monkeypatch):
    monkeypatch.setattr(WM, 'HAT_KEEPALIVE_MAX_S', KEEPALIVE_S)
    yield


def _ws(blocks: list[dict], **siblings) -> str:
    payload = {'blocks': {'languageVersion': 0, 'blocks': blocks}}
    payload.update(siblings)
    return json.dumps(payload)


def _manager():
    status: list[dict] = []
    mgr = WorkflowManager(publisher=lambda _p: None, emit_status=status.append)
    return mgr, status


def _drain(mgr, status, cap=20.0):
    """Wait for the run to end, then reap. Always stops, so a hung assertion
    can never leave a daemon spinning into the next test."""
    deadline = time.monotonic() + cap
    while mgr.is_running and time.monotonic() < deadline:
        time.sleep(0.01)
    mgr.stop()
    time.sleep(0.2)
    return status


def _logs(status) -> list[str]:
    return [e['log_message'] for e in status
            if isinstance(e, dict) and e.get('log_message')]


def _phase(status) -> str | None:
    terminal = [e.get('phase') for e in status
                if isinstance(e, dict)
                and e.get('phase') in ('finished', 'stopped', 'error')]
    return terminal[-1] if terminal else None


def _set(name: str, value: str) -> dict:
    return {'type': 'variables_set', 'fields': {'VAR': name},
            'inputs': {'VALUE': {'block': {'type': 'text',
                                           'fields': {'TEXT': value}}}}}


def _wait(seconds) -> dict:
    return {'type': 'edubotics_wait_seconds', 'fields': {'SECONDS': seconds}}


def _chain(*blocks: dict) -> dict:
    """Link blocks into one `next` chain and return the head."""
    head = blocks[0]
    cur = head
    for nxt in blocks[1:]:
        cur['next'] = {'block': nxt}
        cur = nxt
    return head


def _send(name='go') -> dict:
    return {'type': 'edubotics_broadcast', 'fields': {'EVENT_NAME': name}}


def _on_broadcast(body: dict, name='go') -> dict:
    return {'type': 'edubotics_when_broadcast', 'fields': {'EVENT_NAME': name},
            'next': {'block': body}}


# ── RS-02 — a broadcast sent before the hat is listening ────────────────────

@pytest.mark.parametrize('trial', range(5))
def test_a_broadcast_from_the_very_first_block_reaches_its_hat(trial):
    """0 of 20 before the fix, and DETERMINISTIC, not racy: start() started the
    main thread first, so the send was always already counted when the handler
    took its baseline — and the baseline defaulted to the current count."""
    mgr, status = _manager()
    mgr.start(_ws([_send(), _on_broadcast(_set('hat', 'FIRED'))]), 'wf')
    _drain(mgr, status)
    assert any('[VAR:hat=' in m for m in _logs(status)), (
        'a „sende Ereignis" as block one must reach its „wenn Ereignis '
        'empfangen"')


def test_a_burst_of_broadcasts_is_queued_not_coalesced():
    """`consumed[tid] = count` swallowed a backlog into a single firing; Scratch
    queues them.

    Counted from the BLOCK-CHANGE stream, not from a „melde" line: the output
    blocks carry their own rate limit, which would cap the visible evidence at
    one message and make this pass for the wrong reason."""
    mgr, status = _manager()
    main = _chain(_wait(0.3), _send(), _send(), _send(), _wait(2.0))
    body = {'type': 'edubotics_counter_add', 'id': 'hat-body',
            'fields': {'NAME': 'c', 'N': 1}}
    mgr.start(_ws([main, _on_broadcast(body)]), 'wf')
    _drain(mgr, status)
    runs = sum(1 for e in status if isinstance(e, dict)
               and e.get('current_block_id') == 'hat-body'
               and e.get('phase') == 'done')
    assert runs == 3, f'expected one firing per send, got {runs}'


def test_a_broadcast_that_happened_before_the_handler_waited_is_not_lost():
    """The baseline half of RS-02, pinned independently of thread ordering.

    TWO separate defects produced the measured 0-of-20, and each masks the
    other: start() started the main thread FIRST (so the send was always
    already counted), and the handler's baseline defaulted to
    ``state['count']`` (so whatever had already been sent was written off).
    Starting the handlers first makes the end-to-end test pass on its own —
    which is exactly why the baseline needs a guard that does not go through
    the threads at all. Without it, a handler that is merely slow to take its
    first baseline still loses the event."""
    mgr, _status = _manager()
    hat = {'type': 'edubotics_when_broadcast', 'fields': {'EVENT_NAME': 'go'}}
    ctx = types.SimpleNamespace(should_stop=lambda: False)
    # The event happens before this handler has ever waited.
    mgr._fire_broadcast('go')
    assert mgr._wait_for_hat_trigger(hat, ctx) is True, (
        'a broadcast that landed before the handler first waited must still '
        'be delivered')


def test_hat_handlers_are_started_before_the_main_stack():
    """Ordering is load-bearing for the keep-alive: Thread.is_alive() is False
    for a thread that has not been started, so a main stack that finishes fast
    would see "no handlers alive" and end the run immediately."""
    mgr, status = _manager()
    mgr.start(_ws([_set('x', '1'), _on_broadcast(_set('hat', 'y'))]), 'wf')
    _drain(mgr, status)
    assert any('Hauptprogramm fertig' in m for m in _logs(status)), (
        'the run ended before its handler threads were even started')


def test_the_shipped_hat_constants_are_the_numbers_we_chose():
    """LITERAL pins. Every other reference to these four is written RELATIVE to
    the constant, which is the shape a mutation walks straight through: measured
    2026-09-09 on the full suite, ``MAX_BROADCAST_BACKLOG = 1_000_000`` and
    ``MAX_HAT_CONSECUTIVE_ERRORS = 1`` both stayed green (1482 passed / 4
    skipped, identical to baseline), and ``MAX_HAT_HANDLERS = 0`` needed a
    FIXED-COUNT test before anything could see it.

    A test may not restate the implementation: never the symbol under test on
    both sides of an assertion.

    Read from the SOURCE, not from the module attribute, for the reason
    ``test_while_empty_window`` documents for env-derived constants — the
    attribute is contaminated by whoever monkeypatched it. This very file's
    autouse fixture sets ``HAT_KEEPALIVE_MAX_S = 3.0`` for every test in it, so
    ``assert WM.HAT_KEEPALIVE_MAX_S == 300.0`` measures the fixture."""
    src = Path(WM.__file__).read_text(encoding='utf-8')
    for name, literal in (('MAX_BROADCAST_BACKLOG', '32'),
                          ('MAX_HAT_CONSECUTIVE_ERRORS', '5'),
                          ('MAX_HAT_HANDLERS', '16'),
                          ('HAT_KEEPALIVE_MAX_S', '300.0'),
                          ('HAT_MIN_CYCLE_S', '0.05')):
        m = re.search(rf'^{name}\s*=\s*([0-9_.]+)\s*$', src, re.M)
        assert m, f'{name} moved, was renamed, or stopped being a plain literal'
        assert m.group(1) == literal, (
            f'{name} shipped as {m.group(1)}, not {literal}')


def test_the_broadcast_backlog_is_bounded():
    """The behavioural half, with LITERAL sizes on both sides.

    It used to seed ``count = MAX_BROADCAST_BACKLOG * 10`` and assert
    ``consumed == count - MAX_BROADCAST_BACKLOG`` — a literal restatement of the
    production line, whose seed makes the `else` branch unreachable for EVERY
    positive cap (cap=32 → 320/288/288 equal; cap=1 000 000 → 10 000 000 /
    9 000 000 / 9 000 000 equal). Fixed numbers are what make it fail."""
    mgr, _status = _manager()
    state = mgr._broadcast_state('go')
    ctx = types.SimpleNamespace(should_stop=lambda: False)
    hat = {'type': 'edubotics_when_broadcast', 'fields': {'EVENT_NAME': 'go'}}
    with state['cond']:
        state['count'] = 320
    assert mgr._wait_for_hat_trigger(hat, ctx) is True
    tid = threading.get_ident()
    assert state['consumed'][tid] == 288, (
        'a 320-deep backlog must be trimmed to the newest 32')


# ── RS-03 — a program made only of hats ────────────────────────────────────

@pytest.mark.parametrize('hat_type,fields', [
    ('edubotics_when_broadcast', {'EVENT_NAME': 'go'}),
    ('edubotics_when_object_seen', {'OBJECT_TYPE': 'wuerfel'}),
    ('edubotics_when_counter_gt', {'NAME': 'c', 'N': 0}),
])
def test_a_hats_only_program_stays_alive(hat_type, fields):
    """It used to report 'finished' in 0.157 s having done nothing at all —
    „Wenn Würfel erkannt: Greife" is the most natural program the block set can
    express."""
    mgr, status = _manager()
    started = time.monotonic()
    mgr.start(_ws([{'type': hat_type, 'fields': fields,
                    'next': {'block': _set('hat', 'FIRED')}}]), 'wf')
    _drain(mgr, status)
    lifetime = time.monotonic() - started
    assert lifetime > KEEPALIVE_S * 0.5, (
        f'a hats-only program ended after {lifetime:.3f} s — it must stay alive '
        f'for its handlers')


def test_a_program_with_no_hats_is_not_delayed():
    """The keep-alive must be invisible to every ordinary single-stack run."""
    mgr, status = _manager()
    started = time.monotonic()
    mgr.start(_ws([_set('x', '1')]), 'wf')
    _drain(mgr, status)
    assert time.monotonic() - started < KEEPALIVE_S * 0.5
    assert _phase(status) == 'finished'


def test_stop_ends_the_keep_alive_promptly():
    mgr, status = _manager()
    mgr.start(_ws([_on_broadcast(_set('hat', 'x'))]), 'wf')
    time.sleep(0.3)
    started = time.monotonic()
    mgr.stop()
    assert time.monotonic() - started < 2.0
    assert _phase(status) == 'stopped'


# ── RS-17 — a hat body in flight when the main stack ends ──────────────────

def test_a_hat_body_in_flight_is_not_truncated():
    """Measured before the fix: a pickup cut at 45 of 190 waypoints — mid
    approach, gripper open, hovering — while the run reported 'finished'."""
    mgr, status = _manager()
    main = _chain(_wait(0.2), _send())
    body = _chain(_wait(1.5), _set('hat', 'COMPLETED'))
    mgr.start(_ws([main, _on_broadcast(body)]), 'wf')
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if any('COMPLETED' in m for m in _logs(status)):
            break
        time.sleep(0.02)
    completed = any('COMPLETED' in m for m in _logs(status))
    mgr.stop()
    assert completed, (
        'the hat body was cut off when the main stack ended')


def test_the_student_is_told_why_the_run_is_still_going():
    mgr, status = _manager()
    mgr.start(_ws([_set('x', '1'), _on_broadcast(_set('hat', 'y'))]), 'wf')
    _drain(mgr, status)
    assert any('Hauptprogramm fertig' in m for m in _logs(status))


# ── RS-16 — an error in a hat body ────────────────────────────────────────

def test_one_failing_body_does_not_silence_the_handler():
    """All four except arms used to `return`. Measured with 2 cubes: one
    grasped, the drop failed, the handler exited, the second cube was never
    touched, the arm was LEFT HOLDING the first, phase='finished', 5/5."""
    mgr, status = _manager()
    main = _chain(_wait(0.2), _send(), _wait(0.5), _send(), _wait(0.8))
    body = {'type': 'evil_unknown_block'}
    mgr.start(_ws([main, _on_broadcast(body)]), 'wf')
    _drain(mgr, status)
    # „WARNUNG" is FILTERED OUT, and that one word is the whole test. Without
    # it the RETIREMENT warning counts as an error: at MAX_HAT_CONSECUTIVE_ERRORS
    # = 5 the handler logs 2 errors, and at MAX = 1 it logs 1 error + the
    # retirement [WARNUNG] — `len(errors) == 2` EITHER WAY, so the mutation
    # `MAX_HAT_CONSECUTIVE_ERRORS = 1` (which retires the handler on its first
    # failure, i.e. the exact defect this test is named for) survived the whole
    # suite. `test_a_body_that_always_fails_is_retired_not_spun_forever` ~30
    # lines below already had the filter; this one did not.
    errors = [m for m in _logs(status)
              if 'Wenn Ereignis empfangen' in m and 'WARNUNG' not in m]
    assert len(errors) >= 2, (
        f'the handler must survive its first error; got {errors}')


def test_a_hat_error_is_german_and_never_names_the_block_type_id():
    mgr, status = _manager()
    main = _chain(_wait(0.2), _send(), _wait(0.6))
    mgr.start(_ws([main, _on_broadcast({'type': 'evil_unknown_block'})]), 'wf')
    _drain(mgr, status)
    logs = _logs(status)
    assert any('Wenn Ereignis empfangen' in m for m in logs), logs
    assert not any('edubotics_when_' in m for m in logs), (
        'the raw block-type id must never reach the Protokoll (Rule §1)')
    assert not any('Hat-Handler' in m for m in logs), (
        'and the half-English label is gone too')


def test_a_body_that_always_fails_is_retired_not_spun_forever():
    mgr, status = _manager()
    sends = [_wait(0.2)]
    for _ in range(8):
        sends += [_send(), _wait(0.3)]
    mgr.start(_ws([_chain(*sends),
                   _on_broadcast({'type': 'evil_unknown_block'})]), 'wf')
    _drain(mgr, status)
    logs = _logs(status)
    assert any('nicht mehr ausgeführt' in m for m in logs), logs
    errors = [m for m in logs
              if 'Wenn Ereignis empfangen' in m and 'WARNUNG' not in m]
    assert len(errors) == WM.MAX_HAT_CONSECUTIVE_ERRORS, errors


# ── RS-04 / RS-55A — hats must not saturate a core ────────────────────────

def test_a_hat_that_rebroadcasts_its_own_event_is_rate_limited():
    """Measured before the rate floor: 570 211 body runs and 2 851 066 status
    messages in 2 s, 71 % of one core, inside the ROS node — starving the 1 Hz
    heartbeat until React reported „Getrennt"."""
    mgr, status = _manager()
    main = _chain(_wait(0.2), _send(), _wait(1.5))
    body = _chain({'type': 'edubotics_counter_add',
                   'fields': {'NAME': 'c', 'N': 1}}, _send())
    mgr.start(_ws([main, _on_broadcast(body)]), 'wf')
    _drain(mgr, status)
    # ~20 Hz ceiling per hat, times 3 status publishes per block, times the run
    # length — thousands, not millions. The pre-fix number was 2.8 million.
    assert len(status) < 20000, (
        f'{len(status)} status messages — the hat rate floor is not holding')


def test_an_empty_named_hat_does_not_spin():
    """16 empty-named hats measured 110 % of one core: both the broadcast and
    the object-seen trigger returned False with no sleep at all."""
    import resource
    mgr, status = _manager()
    hats = [{'type': 'edubotics_when_broadcast', 'fields': {'EVENT_NAME': ''}}
            for _ in range(8)]
    hats += [{'type': 'edubotics_when_object_seen',
              'fields': {'OBJECT_TYPE': ''}} for _ in range(8)]
    before = resource.getrusage(resource.RUSAGE_SELF)
    started = time.monotonic()
    mgr.start(_ws(hats + [_wait(1.0)]), 'wf')
    time.sleep(1.5)
    mgr.stop()
    after = resource.getrusage(resource.RUSAGE_SELF)
    cpu = ((after.ru_utime - before.ru_utime)
           + (after.ru_stime - before.ru_stime))
    wall = time.monotonic() - started
    assert cpu / wall < 0.5, (
        f'16 empty-named hats burned {100 * cpu / wall:.0f} % of a core')


# ── RS-55A — orphan events and unknown object types ──────────────────────

def test_a_hat_whose_event_nobody_sends_is_reported():
    mgr, status = _manager()
    mgr.start(_ws([_on_broadcast(_set('h', 'x'), name='niemand'),
                   _wait(0.3)]), 'wf')
    _drain(mgr, status)
    warnings = [m for m in _logs(status) if 'niemand' in m]
    assert warnings and 'WARNUNG' in warnings[0], _logs(status)
    assert 'wartet' in warnings[0]


def test_an_event_nobody_listens_for_is_reported():
    mgr, status = _manager()
    mgr.start(_ws([_send('niemand')]), 'wf')
    _drain(mgr, status)
    warnings = [m for m in _logs(status) if 'niemand' in m]
    assert warnings and 'WARNUNG' in warnings[0], _logs(status)
    assert 'wartet darauf' in warnings[0]


def test_a_matched_event_pair_is_not_reported():
    mgr, status = _manager()
    mgr.start(_ws([_chain(_wait(0.2), _send()),
                   _on_broadcast(_set('h', 'x'))]), 'wf')
    _drain(mgr, status)
    # Only the ORPHAN diagnostics are under test here — the keep-alive's own
    # „Zeitlimit erreicht" line also mentions Ereignis-Blöcke and is expected.
    assert not any('wartet' in m for m in _logs(status)), _logs(status)


def test_an_unknown_object_type_in_a_hat_is_reported():
    class _Catalog:
        def recipe_for_type(self, name):
            raise KeyError(name)

    status: list[dict] = []
    mgr = WorkflowManager(publisher=lambda _p: None, emit_status=status.append,
                          load_object_catalog=lambda: _Catalog())
    mgr.start(_ws([{'type': 'edubotics_when_object_seen',
                    'fields': {'OBJECT_TYPE': 'banane'},
                    'next': {'block': _set('h', 'x')}}, _wait(0.3)]), 'wf')
    _drain(mgr, status)
    assert any('banane' in m and 'WARNUNG' in m for m in _logs(status)), \
        _logs(status)


def test_a_diagnostic_is_emitted_at_most_once():
    mgr, status = _manager()
    mgr.start(_ws([_on_broadcast(_set('h', 'x'), name='niemand'),
                   _wait(0.5)]), 'wf')
    _drain(mgr, status)
    assert sum(1 for m in _logs(status) if 'niemand' in m) == 1


def test_the_start_time_diagnostics_do_not_deadlock_start():
    """_warn_once runs from inside start(), which holds the manager's
    NON-reentrant self._lock — reusing that lock hung every workflow carrying
    an orphan event."""
    mgr, status = _manager()
    done = threading.Event()

    def _go():
        mgr.start(_ws([_on_broadcast(_set('h', 'x'), name='niemand'),
                       _wait(0.2)]), 'wf')
        done.set()

    threading.Thread(target=_go, daemon=True).start()
    assert done.wait(timeout=5.0), 'start() dead-locked on its own diagnostics'
    _drain(mgr, status)


# ── RS-36 — a follower readback that arrives late ────────────────────────

def test_a_late_joint_readback_still_seeds_the_start_pose():
    """The seed is taken ONCE, synchronously. Measured: a readback arriving
    0.3 s after start — a full 0.7 s BEFORE the program's first motion block —
    still failed the whole run with „Aktuelle Armstellung ist noch nicht
    bekannt …"."""
    ready = {'v': False}
    status: list[dict] = []
    mgr = WorkflowManager(
        publisher=lambda _p: None, emit_status=status.append,
        load_calibration=lambda: {'z_table': 0.0},
        get_follower_joints=lambda: ([0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
                                     if ready['v'] else None))
    threading.Timer(0.3, lambda: ready.__setitem__('v', True)).start()
    mgr.start(_ws([_chain(_wait(1.0),
                          {'type': 'edubotics_open_gripper'})]), 'wf')
    _drain(mgr, status)
    errors = [e.get('error') for e in status
              if isinstance(e, dict) and e.get('phase') == 'error']
    assert errors == [], errors
    assert _phase(status) == 'finished'


def test_no_joint_source_at_all_still_refuses_to_move():
    """The watchdog must not become a way to command from an assumed pose."""
    status: list[dict] = []
    mgr = WorkflowManager(publisher=lambda _p: None, emit_status=status.append,
                          load_calibration=lambda: {'z_table': 0.0})
    mgr.start(_ws([{'type': 'edubotics_open_gripper'}]), 'wf')
    _drain(mgr, status)
    assert _phase(status) == 'error'


def test_the_watchdog_never_overwrites_a_commanded_pose():
    ctx = types.SimpleNamespace(last_full_joints=[0.0, -1.5, 1.5, 0.0, 0.0, 0.8])
    mgr = WorkflowManager(
        publisher=lambda _p: None,
        get_follower_joints=lambda: [9.0] * 6)
    mgr._start_joint_seed_watchdog(ctx)
    time.sleep(0.4)
    assert ctx.last_full_joints == [0.0, -1.5, 1.5, 0.0, 0.0, 0.8]


# ── RS-56A — the client's trajectory payload wins ───────────────────────

def test_a_client_supplied_trajectory_is_not_overridden_by_a_persisted_one():
    """RunControls stamps every saved „Bewegung" with the rig's robot type and
    refuses a cross-profile replay client-side; a persisted entry of the same
    name used to silently override that checked payload."""
    mgr, _status = _manager()
    mgr.set_trajectory('Bewegung 1', {'fps': 25, 'points': [['persisted']]})
    captured: dict = {}
    real_ctx_factory = WM.WorkflowContext

    def _spy(*args, **kwargs):
        captured.update(kwargs)
        return real_ctx_factory(*args, **kwargs)

    WM.WorkflowContext = _spy
    try:
        mgr.start(_ws([_set('x', '1')], trajectories={
            'Bewegung 1': {'fps': 25, 'points': [['client']]}}), 'wf')
    finally:
        WM.WorkflowContext = real_ctx_factory
    _drain(mgr, _status)
    assert captured['trajectories']['Bewegung 1']['points'] == [['client']]


# ── the refused start must not un-stop a surviving hat ───────────────────────
def test_a_refused_start_does_not_re_arm_a_surviving_hat_thread():
    """``start()``'s refusal guards run BEFORE ``_stop_event.clear()``.

    ``ctx.should_stop is self._stop_event.is_set``, so clearing the event
    un-stops every thread still holding it — including a hat that outlived the
    previous run. The clear used to happen first, and a guard that then refused
    the start returned without restoring the flag.

    Measured before the fix, 2 of 2 trials, with a hat parked inside a slow
    ``perception.detect``::

        zombie_after_stop=[True]   stop_event_after_stop=True
        start2=(False, 'Vorheriger Workflow läuft noch — bitte kurz warten.')
        stop_event_after_start2=False          <-- the hat is running again
        late_waypoints_with_no_workflow=90     still_alive=[True]
        start3=(False, ...)                    <-- refused forever

    i.e. an arm driven with no workflow running and ``on_workflow`` already
    released (so a recording or a jog could claim it alongside), ``stop()``
    answering „Es läuft kein Workflow." because it short-circuits on
    ``is_running``, and a Roboter Studio the student cannot restart.

    NOT caused by the hat keep-alive. An earlier version of this docstring said
    so; re-measured on the pre-keep-alive tree with a real 6 s-blocking hat, the
    defect reproduces 2/2 with the same 90 late waypoints — and is arguably worse
    there, because ``stop()`` short-circuits on ``is_running`` and returns in
    0.00 s without even attempting the join or emitting the zombie warning.

    This test does not need a real zombie: it pins the INVARIANT that a refused
    start leaves the stop flag exactly as it found it, which is the property the
    fix restores and the one a future edit would silently break.
    """
    mgr, _status = _manager()

    # Stand in for a hat thread that has not reaped yet, so the zombie guard
    # refuses. A live non-daemon thread would hang the suite; a finished one
    # would not trip the guard — so fake `is_alive`.
    mgr._hat_threads = [types.SimpleNamespace(is_alive=lambda: True)]
    mgr._stop_event.set()

    ok, msg, unreachable = mgr.start(_ws([_set('x', '1')]), 'wf-refused')

    assert ok is False
    assert 'Vorheriger Workflow läuft noch' in msg
    assert unreachable == []
    assert mgr._stop_event.is_set(), (
        'a refused start cleared the stop flag and re-armed the surviving hat'
    )


def test_a_start_refused_for_too_many_hats_also_leaves_the_stop_flag_alone():
    """The same invariant for the second early return (``MAX_HAT_HANDLERS``).

    Both guards sit above the clear; this pins the one that is easy to move back
    down while "only touching the hat-count check".
    """
    mgr, _status = _manager()
    mgr._stop_event.set()

    too_many = [_on_broadcast(_set('n', str(i)), name=f'e{i}')
                for i in range(WM.MAX_HAT_HANDLERS + 1)]
    ok, msg, _unreachable = mgr.start(_ws(too_many), 'wf-too-many')

    assert ok is False
    assert 'Zu viele Ereignis-Blöcke' in msg
    assert mgr._stop_event.is_set(), (
        'the hat-count refusal cleared the stop flag'
    )


def test_an_accepted_start_still_clears_the_stop_flag():
    """The other half: hoisting the guards must not leave a run born stopped.
    Without this, the fix above would trade one wedge for another.

    Asserting ``not _stop_event.is_set()`` directly would be a RACE, not a
    check: a hat-less program finishes in microseconds and ``_run``'s ``finally``
    legitimately re-sets the flag, so the assert reads the post-run state. (It
    failed exactly that way when first written.) The observable property is the
    TERMINAL PHASE: a run that began with the flag still set raises
    „Workflow wurde gestoppt." on its first block and ends 'stopped', so
    'finished' is proof the clear happened.
    """
    mgr, status = _manager()
    mgr._stop_event.set()

    ok, _msg, _unreachable = mgr.start(_ws([_set('x', '1')]), 'wf-ok')
    assert ok is True
    _drain(mgr, status)
    assert _phase(status) == 'finished'


# ── the hat-count DoS bound must actually bound something ────────────────────
# `MAX_HAT_HANDLERS` had ZERO test references (found by mutation testing): every
# value from 0 to a million left the suite green, so the guard was free to rot
# on the next refactor. It is a real bound on an untrusted payload — each hat is
# a daemon thread and /workflow/start authenticates nobody — so both DIRECTIONS
# are pinned here: one over the cap must be refused, and exactly the cap must
# still be ACCEPTED. Testing only the refusal would pass with the constant set
# to 0, which would break every event program in the product.

def test_one_hat_over_the_cap_is_refused_in_german():
    mgr, _status = _manager()
    hats = [_on_broadcast(_set(f'x{i}', '1'), name=f'e{i}')
            for i in range(WM.MAX_HAT_HANDLERS + 1)]
    ok, msg, _ = mgr.start(_ws(hats), 'wf')
    assert ok is False, 'a payload over MAX_HAT_HANDLERS was accepted'
    assert 'Ereignis-Blöcke' in msg, f'refusal is not the German one: {msg!r}'
    # Refused BEFORE any thread was spawned — the whole point of the cap.
    assert not any(t.is_alive() for t in mgr._hat_threads)


def test_a_realistic_event_program_is_not_refused():
    """Pins the VALUE with a fixed count, because the two boundary tests around
    it structurally CANNOT.

    Both of those are written relative to ``MAX_HAT_HANDLERS`` itself, so
    setting it to 0 leaves both green: the refusal test still refuses (1 > 0),
    and the acceptance test degenerates to ``range(0)`` — an EMPTY workspace,
    which is legally accepted. Verified by mutation 2026-09-08: at
    ``MAX_HAT_HANDLERS = 0`` both passed, i.e. the guard could be hardened into
    refusing every event program in the product without a single test noticing.

    Eight hats is plainly inside any classroom program, so this is the
    direction that fails loudly if the constant is ever driven toward zero."""
    mgr, status = _manager()
    hats = [_on_broadcast(_set(f'x{i}', '1'), name=f'e{i}') for i in range(8)]
    ok, msg, _ = mgr.start(_ws(hats), 'wf')
    try:
        assert ok is True, f'an 8-event program was refused: {msg!r}'
    finally:
        _drain(mgr, status)


def test_exactly_the_cap_is_still_accepted():
    """The other direction: the bound must not be so tight it refuses a legal
    workspace at exactly the documented limit."""
    mgr, status = _manager()
    hats = [_on_broadcast(_set(f'x{i}', '1'), name=f'e{i}')
            for i in range(WM.MAX_HAT_HANDLERS)]
    ok, msg, _ = mgr.start(_ws(hats), 'wf')
    try:
        assert ok is True, f'exactly MAX_HAT_HANDLERS hats was refused: {msg!r}'
    finally:
        _drain(mgr, status)

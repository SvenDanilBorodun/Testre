#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""RS-50 — the PRODUCING half of the ``[CNT:name=int]`` („Zähler") sentinel.

The counter blocks wrote ``ctx.counters`` and emitted NOTHING, so the canonical
points lesson — „setze Zähler Punkte auf 0", „erhöhe Zähler Punkte um 1" inside
„Solange sichtbar", „wenn Zähler Punkte größer als 3" — showed
„Noch keine Variablen." in the Debug-Panel while it was visibly counting.

This file owns the three decisions that live on the server:

* WHICH operations emit. ``reset`` and ``add`` WRITE, so they emit; ``get`` and
  ``get_count`` READ, so they are silent. ``get`` is a VALUE block re-evaluated
  once per loop pass (and many times a second inside a ``solange`` condition),
  and ``get_count`` is additionally the ``when_counter_gt`` hat's own 5 Hz poll
  on a separate daemon thread — an emit in either would repeat an unchanged
  number at the loop rate and peg the panel's „Aktualisiert" column at „jetzt"
  for a counter that is standing still.
* WHERE the emit happens relative to ``ctx.var_lock`` — OUTSIDE it, as
  ``Interpreter._set_variable`` does, because ``ctx.log`` is
  ``WorkflowManager``'s status publisher.
* WHAT the bytes are. The React frame is
  ``/^\\[CNT:(.+)=(-?\\d+)\\]$/`` — greedy name, digits-only value — so the name
  goes on the wire VERBATIM and the value as bare digits.

The consuming half is
``physical_ai_manager/src/components/Workshop/__tests__/VariableInspector.counters.test.jsx``.
"""

import re
import threading

from physical_ai_server.workflow.handlers import counters as counter_handlers
from physical_ai_server.workflow.handlers.motion import WorkflowError
import pytest


# The React-side frame, transcribed. If these two ever disagree the sentinel is
# either dropped silently or printed raw into the student's Protokoll.
CNT_FRAME = re.compile(r'^\[CNT:(.+)=(-?\d+)\]$', re.S)


class _TrackingLock:
    """An RLock that remembers how deeply it is currently held.

    The point of the whole fixture: ``ctx.log`` records the depth at CALL time,
    so a test can prove the emit happens with the lock RELEASED. Moving the
    ``_emit`` call inside the ``with`` block would (a) park every
    ``when_counter_gt`` hat thread's ``get_count`` behind a ROS publish and
    (b) introduce a ``var_lock`` → publisher lock ordering edge no other path
    has.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.depth = 0

    def __enter__(self):
        self._lock.acquire()
        self.depth += 1
        return self

    def __exit__(self, *exc):
        self.depth -= 1
        self._lock.release()
        return False

    def acquire(self, *a, **k):
        return self._lock.acquire(*a, **k)

    def release(self):
        return self._lock.release()


class _Ctx:
    """Counter store + a depth-tracking var_lock + a collecting log."""

    def __init__(self):
        self.variables: dict = {}
        self.counters: dict = {}
        self.logs: list[str] = []
        self.log_depths: list[int] = []
        self.var_lock = _TrackingLock()

    def log(self, message):
        self.logs.append(message)
        self.log_depths.append(self.var_lock.depth)


def _sentinels(ctx):
    return [m for m in ctx.logs if m.startswith('[CNT:')]


# ── WHAT the bytes are ───────────────────────────────────────────────────────
def test_add_emits_the_new_value():
    ctx = _Ctx()
    counter_handlers.add(ctx, {'name': 'Punkte'})
    assert ctx.logs == ['[CNT:Punkte=1]']


def test_add_emits_once_per_increment_and_counts_up():
    ctx = _Ctx()
    for _ in range(3):
        counter_handlers.add(ctx, {'name': 'Punkte'})
    assert ctx.logs == ['[CNT:Punkte=1]', '[CNT:Punkte=2]', '[CNT:Punkte=3]']


def test_reset_emits_zero():
    ctx = _Ctx()
    counter_handlers.add(ctx, {'name': 'Punkte'})
    ctx.logs.clear()
    counter_handlers.reset(ctx, {'name': 'Punkte'})
    assert ctx.logs == ['[CNT:Punkte=0]']


def test_the_emitted_value_is_the_value_this_write_produced():
    ctx = _Ctx()
    counter_handlers.add(ctx, {'name': 'a'})
    counter_handlers.add(ctx, {'name': 'b'})
    counter_handlers.add(ctx, {'name': 'a'})
    assert ctx.logs == ['[CNT:a=1]', '[CNT:b=1]', '[CNT:a=2]']


def test_the_name_goes_on_the_wire_exactly_as_typed():
    # German, spaces, hyphens, digits — the student reads back what they typed.
    ctx = _Ctx()
    for name in ('Anzahl Würfel', 'zähler-2', 'Runde 3', 'Größe', 'weiß'):
        counter_handlers.add(ctx, {'name': name})
    assert ctx.logs == [
        '[CNT:Anzahl Würfel=1]',
        '[CNT:zähler-2=1]',
        '[CNT:Runde 3=1]',
        '[CNT:Größe=1]',
        '[CNT:weiß=1]',
    ]


def test_the_trimmed_name_is_emitted_not_the_raw_one():
    ctx = _Ctx()
    counter_handlers.add(ctx, {'name': '  Punkte  '})
    assert ctx.logs == ['[CNT:Punkte=1]']
    assert ctx.counters == {'Punkte': 1}


def test_every_sentinel_parses_with_the_react_frame():
    ctx = _Ctx()
    counter_handlers.reset(ctx, {'name': 'Punkte'})
    counter_handlers.add(ctx, {'name': 'Anzahl Würfel'})
    parsed = [CNT_FRAME.match(m) for m in _sentinels(ctx)]
    assert all(parsed), ctx.logs
    assert [(m.group(1), int(m.group(2))) for m in parsed] == [
        ('Punkte', 0), ('Anzahl Würfel', 1),
    ]


def test_the_value_is_bare_digits_not_json():
    # JSON would add quoting rules for nothing: a counter is an integer by
    # construction, and the consumer's digits-only capture IS the type check.
    ctx = _Ctx()
    counter_handlers.add(ctx, {'name': 'Punkte'})
    assert '"' not in ctx.logs[0]
    assert CNT_FRAME.match(ctx.logs[0]).group(2) == '1'


def test_a_name_containing_eq_still_frames_for_the_consumer():
    # `counterNameValidator` forbids only [\r\n\0[\]], so „Punkte=2" is a name a
    # student can type today and the validator may NOT be tightened (a Blockly
    # field validator also runs during DESERIALIZATION). The React frame splits
    # on the LAST `=`, so this parses back as the full name and is refused
    # THERE — consumed, never printed into the Protokoll.
    ctx = _Ctx()
    counter_handlers.add(ctx, {'name': 'Punkte=2'})
    assert ctx.logs == ['[CNT:Punkte=2=1]']
    m = CNT_FRAME.match(ctx.logs[0])
    assert m and m.group(1) == 'Punkte=2' and m.group(2) == '1'


def test_the_clamp_is_emitted_not_the_unclamped_number():
    ctx = _Ctx()
    ctx.counters['Punkte'] = counter_handlers._COUNTER_MAX
    counter_handlers.add(ctx, {'name': 'Punkte'})
    assert ctx.logs == [f'[CNT:Punkte={counter_handlers._COUNTER_MAX}]']


# ── WHICH operations emit ────────────────────────────────────────────────────
def test_get_is_silent():
    # A VALUE block: „wenn Zähler Punkte größer als 3" re-evaluates it on every
    # pass of the surrounding loop. An emit here reports motion that is not
    # happening.
    ctx = _Ctx()
    counter_handlers.add(ctx, {'name': 'Punkte'})
    ctx.logs.clear()
    for _ in range(50):
        assert counter_handlers.get(ctx, {'name': 'Punkte'}) == 1
    assert ctx.logs == []


def test_get_count_is_silent():
    # The `when_counter_gt` hat's own poll, on a separate daemon thread at
    # ~5 Hz. An emit here would flood the status channel from a second thread
    # with a value the preceding `add` already reported.
    ctx = _Ctx()
    counter_handlers.add(ctx, {'name': 'Punkte'})
    ctx.logs.clear()
    for _ in range(50):
        assert counter_handlers.get_count(ctx, 'Punkte') == 1
    assert ctx.logs == []


def test_reading_an_unset_counter_is_silent_and_creates_nothing():
    ctx = _Ctx()
    assert counter_handlers.get(ctx, {'name': 'gibtsnicht'}) == 0
    assert ctx.logs == []
    assert ctx.counters == {}


def test_a_refused_write_emits_nothing():
    ctx = _Ctx()
    with pytest.raises(WorkflowError):
        counter_handlers.add(ctx, {'name': '   '})
    with pytest.raises(WorkflowError):
        counter_handlers.reset(ctx, {'name': ''})
    assert ctx.logs == []


# ── WHERE the emit happens ───────────────────────────────────────────────────
def test_add_emits_with_the_lock_released():
    ctx = _Ctx()
    counter_handlers.add(ctx, {'name': 'Punkte'})
    assert ctx.log_depths == [0], (
        'the [CNT:] emit must not run inside ctx.var_lock — ctx.log is the ROS '
        'status publisher, and holding the counter lock across it parks every '
        'when_counter_gt hat thread behind a publish'
    )


def test_reset_emits_with_the_lock_released():
    ctx = _Ctx()
    counter_handlers.reset(ctx, {'name': 'Punkte'})
    assert ctx.log_depths == [0]


def test_the_lock_really_is_the_one_being_taken():
    # Guards the guard: if `add` stopped taking var_lock at all, the depth
    # assertion above would pass vacuously.
    ctx = _Ctx()
    seen = []
    original_enter = _TrackingLock.__enter__

    def spy(self):
        seen.append(1)
        return original_enter(self)

    _TrackingLock.__enter__ = spy
    try:
        counter_handlers.add(ctx, {'name': 'Punkte'})
    finally:
        _TrackingLock.__enter__ = original_enter
    assert seen, 'add() no longer serializes its write under ctx.var_lock'


# ── degraded ctx ─────────────────────────────────────────────────────────────
def test_a_ctx_without_var_lock_still_emits():
    class _NoLock:
        def __init__(self):
            self.counters = {}
            self.logs = []

        def log(self, m):
            self.logs.append(m)

    ctx = _NoLock()
    counter_handlers.add(ctx, {'name': 'Punkte'})
    assert ctx.logs == ['[CNT:Punkte=1]']


def test_a_failing_log_never_aborts_the_students_program():
    class _BadLog(_Ctx):
        def log(self, message):
            raise RuntimeError('publisher gone')

    ctx = _BadLog()
    counter_handlers.add(ctx, {'name': 'Punkte'})   # must not raise
    assert ctx.counters == {'Punkte': 1}


def test_a_ctx_without_log_at_all_still_counts():
    class _NoLog:
        def __init__(self):
            self.counters = {}
            self.var_lock = threading.RLock()

    ctx = _NoLog()
    counter_handlers.add(ctx, {'name': 'Punkte'})
    counter_handlers.reset(ctx, {'name': 'Punkte'})
    assert ctx.counters == {'Punkte': 0}

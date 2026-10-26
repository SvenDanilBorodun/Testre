#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""C1 — the two „warte bis …" blocks look before they give up.

``perception_blocks._poll_until`` was ``while monotonic() < deadline: …
predicate()``: a timeout of 0 never evaluated the predicate, so
``robot.wait_until_held(0)`` / „warte bis Greifer hält (max 0 s)" answered
falsch while the gripper held (0 evaluations, reviewer-measured), and with a
positive timeout the last look could be up to 0.2 s before the deadline. The
loop now looks first and takes its last look AT the deadline. The motion-lock
release around the wait and its re-acquire are untouched
(test_motion_lock_policy pins them).
"""

from __future__ import annotations

import threading
import time

from physical_ai_server.workflow.handlers import perception_blocks as pb


class _Ctx:
    def __init__(self):
        self.logs: list[str] = []
        self.motion_lock = threading.RLock()

    def should_stop(self):
        return False

    def log(self, message):
        self.logs.append(message)


def test_a_zero_timeout_with_a_true_predicate_answers_true():
    ctx = _Ctx()
    assert pb._poll_until(ctx, lambda: True, 0.0, 'x', raise_on_timeout=False) is True


def test_a_zero_timeout_with_a_false_predicate_looks_exactly_once():
    ctx = _Ctx()
    calls = []

    def predicate():
        calls.append(time.monotonic())
        return False

    t0 = time.monotonic()
    assert pb._poll_until(ctx, predicate, 0.0, 'x', raise_on_timeout=False) is False
    assert len(calls) == 1, calls
    assert time.monotonic() - t0 < 0.1, 'a zero timeout must not sleep'


def test_a_condition_that_turns_true_just_before_the_deadline_is_seen():
    """True from 0.25 s on, deadline 0.3 s: the old loop looked at 0 and 0.2 s
    and then slept past the deadline; the last look is now AT the deadline."""
    ctx = _Ctx()
    start = time.monotonic()
    assert pb._poll_until(ctx, lambda: time.monotonic() - start >= 0.25, 0.3, 'x',
                          raise_on_timeout=False) is True
    assert time.monotonic() - start < 0.45


def test_a_false_predicate_still_ends_at_its_deadline():
    ctx = _Ctx()
    start = time.monotonic()
    assert pb._poll_until(ctx, lambda: False, 0.3, 'x', raise_on_timeout=False) is False
    elapsed = time.monotonic() - start
    assert 0.29 <= elapsed < 0.45, elapsed


def test_wait_until_held_with_zero_seconds_answers_what_the_gripper_does(monkeypatch):
    """„warte bis Greifer hält (max 0 s)" while it holds: wahr, and no
    „hat nichts gegriffen" warning."""
    ctx = _Ctx()
    monkeypatch.setattr(pb, '_check_grasp_held_locked', lambda _ctx: True)
    assert pb.wait_until_held(ctx, {'timeout': 0}) is True
    assert not [m for m in ctx.logs if m.startswith('[WARNUNG]')], ctx.logs


def test_wait_until_seen_with_zero_seconds_answers_what_the_camera_sees(monkeypatch):
    ctx = _Ctx()
    monkeypatch.setattr(pb, '_detect_named_unclaimed', lambda _ctx, _t: ['eins'])
    monkeypatch.setattr(pb, 'label_for', lambda _ctx, t: t)
    assert pb.wait_until_object_seen(ctx, {'timeout': 0, 'object_type': 'wuerfel'}) is True
    assert not [m for m in ctx.logs if m.startswith('[WARNUNG]')], ctx.logs


def test_the_wait_comes_back_holding_the_motion_lock():
    """The motion-lock policy is unchanged: a caller holding the lock gets it
    back after the wait, whatever the predicate answered."""
    ctx = _Ctx()
    with ctx.motion_lock:
        pb._poll_until(ctx, lambda: True, 0.0, 'x', raise_on_timeout=False)
        assert ctx.motion_lock._is_owned()
        pb._poll_until(ctx, lambda: False, 0.0, 'x', raise_on_timeout=False)
        assert ctx.motion_lock._is_owned()

#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""B7 — what „hebe an" says when it cannot rise any further.

„hebe an" rises at most ``DEFAULT_APPROACH_HEIGHT_M`` (6 cm) per block, less
where the arm cannot get that high. Measured (sim, „bewege zu" a table Ziel
then 1–4 „hebe an"): OMX EE z 0.15 → 0.204 m, then no-ops; V1 0.11 → 0.136 m,
then no-ops. The no-op warning ended „„Greife" hebt das Objekt bereits vom
Tisch ab." in EVERY program — true right after „Greife" (whose hover is already
the top), false in a program that never grasped. The clause now follows only
while an object is carried (``ctx.carried_tag``).

The at-the-top pose is BUILT here (lift until a lift publishes nothing), never
assumed from „after Greife", which reaches the top only on some arms
(review-2 MJ-1). One profile per ``ik_backend`` of the registry.
"""

from __future__ import annotations

import pytest

from physical_ai_server import robot_profiles as rp
from physical_ai_server.workflow.handlers import motion
from rig_emulation import Rig

_AT_TOP = ('[WARNUNG] „hebe an" hat den Arm nicht bewegt — über dieser Stelle ist er '
           'schon so hoch, wie er kommt.')
_GREIFE_CLAUSE = ' „Greife" hebt das Objekt bereits vom Tisch ab.'


def _one_profile_per_ik_backend():
    seen = {}
    for pid, prof in rp.ROBOT_PROFILES.items():
        seen.setdefault(prof.ik_backend, pid)
    return sorted(seen.values())


def _ctx_at_the_top(monkeypatch, pid):
    rig = Rig(monkeypatch, pid, simulated=True)
    ctx = rig.make_ctx(simulated=True)
    published = []
    real = ctx.publisher

    def counting(points):
        published.append(points)
        return real(points)

    ctx.publisher = counting
    ctx.destinations = {'A': {'x': 0.15, 'y': 0.05, 'z': 0.0, 'plane_tracked': True}}
    motion.move_to(ctx, {'destination': 'A'})
    assert published, 'precondition: „bewege zu" moved the arm'
    for _ in range(8):
        before = len(published)
        motion.lift(ctx, {})
        if len(published) == before:
            break
    else:
        raise AssertionError('„hebe an" never reached the top')
    # Precondition: the arm really is at the top over this point.
    _R, t = ctx.ik.fk(ctx.last_full_joints[:motion._n(ctx)])
    xyz = (float(t[0]), float(t[1]), float(t[2]))
    cur_q = motion._try_solve(ctx, xyz)
    assert cur_q is not None
    rise, _q = motion._max_reachable_rise(
        ctx, xyz, motion.DEFAULT_APPROACH_HEIGHT_M, fallback_q=cur_q)
    assert rise <= motion._MIN_APPROACH_CLEARANCE_M, rise
    return rig, ctx, published


def _warnings(rig):
    return [s['log_message'] for s in rig.statuses
            if isinstance(s, dict) and str(s.get('log_message', '')).startswith('[WARNUNG]')]


@pytest.mark.parametrize('pid', _one_profile_per_ik_backend())
def test_at_the_top_the_greife_clause_follows_only_a_carried_object(monkeypatch, pid):
    rig, ctx, published = _ctx_at_the_top(monkeypatch, pid)
    n_before = len(published)

    rig.statuses.clear()
    ctx.carried_tag = 20
    motion.lift(ctx, {})
    carried = _warnings(rig)
    assert carried == [_AT_TOP + _GREIFE_CLAUSE], carried

    rig.statuses.clear()
    ctx.carried_tag = None
    motion.lift(ctx, {})
    empty = _warnings(rig)
    assert empty == [_AT_TOP], empty
    assert 'Greife' not in empty[0]

    assert len(published) == n_before, 'a no-op lift must publish nothing'


def test_the_at_the_top_sentence_is_one_constant():
    assert motion._LIFT_AT_TOP_DE == _AT_TOP

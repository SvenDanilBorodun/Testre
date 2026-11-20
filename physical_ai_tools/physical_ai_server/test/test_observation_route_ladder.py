#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The V1's observation route is a two-rung ladder (spec 40 S6.4; decisions C2, D57 Q2; WP-12).

The direct joint line between the observation pose and a hover beside the tower base is refused by the tower guard
for some hovers (p02: 88 of the 1 216 hovers of the uncut zone grid, at the column). The route therefore takes the
direct line when the guard passes it, else goes via HOME (HOME <-> OBS and HOME <-> every zone hover pass), both
ways: ``go_to_observation_pose`` reaching OBS and the next block's ordinary move leaving it. Nothing is refused, and
the via-HOME rung is taken exactly where the direct line is refused.

Run through the real motion handlers with the real guard (the design envelope, the calibrated residual, the
contract place) on the node's own arm model; ``_publish_motion`` only records the legs.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

import test_fixture_guard as H
import test_tower_guard as T
from physical_ai_server.workflow.handlers import motion as M

GRIPPERS = (H.GR['v1']['open'], H.GR['v1']['holding'])
ROLLS = [0.0, math.pi / 4, math.pi / 2, -math.pi / 4]


@pytest.mark.parametrize('cut,hovers', [(False, 1216), (True, 1080)])
def test_every_v1_hover_reaches_and_leaves_the_observation_pose(cut, hovers, monkeypatch):
    """FAILS FIRST (R4): every one of the 1 216 V1 hovers of the uncut zone grid (1 080 of the cut grid; open and
    holding) reaches OBS (go_to_observation_pose) and leaves it (the next ordinary move), none refused; the 88
    direct refusals take the via-HOME rung, in both directions, and only they do."""
    legs = []
    monkeypatch.setattr(M, '_publish_motion', lambda c, a, b, d, tempo=None: legs.append((list(a), list(b))))
    ik = T._arm_ik('v1')
    spy = T._Spy(T._guard_on('v1', ik))
    obs, home = [float(v) for v in H.OBS['v1']], [float(v) for v in H.HOME['v1']]
    grid = [h for _q, h in T._zone_grid('v1', ROLLS, cut=cut)]
    assert len(grid) * len(GRIPPERS) == hovers

    def through_home():
        return any(np.allclose(b[:5], home, atol=1e-9) for _a, b in legs[:-1])
    detours = set()
    for s in GRIPPERS:
        direct = H.leg_minima(H.guard('v1'), 'v1', [(np.array(obs), h) for h in grid], s) < 0.0
        for i, h in enumerate(grid):
            hover = [float(v) for v in h]
            ctx = T._Run('v1', hover + [s], guard=spy, ik=ik)
            legs.clear()
            M.go_to_observation_pose(ctx)
            assert np.allclose(legs[-1][1][:5], obs, atol=1e-9) and ctx.last_full_joints[:5] == pytest.approx(obs)
            reach = through_home()
            legs.clear()
            M.safe_move(ctx, obs + [s], hover + [s], M.DEFAULT_MOVE_DURATION_S)
            assert np.allclose(legs[-1][1][:5], hover, atol=1e-9)
            leave = through_home()
            assert reach == leave == bool(direct[i]), (i, s)
            if reach:
                detours.add((i, s))
    assert len(detours) == 88
    assert spy.calls >= 2 * hovers

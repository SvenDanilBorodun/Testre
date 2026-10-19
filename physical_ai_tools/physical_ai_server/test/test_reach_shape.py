"""The reach shape the editor draws is the reach the arm has (owner decision
Q6; spec D6 / N6 / N11; principle 6: the sim is never more permissive than the
rig, and reach is never widened from IK alone).

The manifest carries the shape per profile: OMX ``reach_inner_m`` 0.10 /
``reach_outer_m`` 0.25 (the minimum over bearings at the rig's grasp height,
rounded down — the drawn 0.28 overstated the forward reach), the Edu:1 its
``reach_sector_deg`` (−90°, 90°) beside 0.07 / 0.35 (it cannot turn behind
itself). Every point of the advertised shape must be graspable in the simulator
(whose table now equals the rig's, Q13), and the shape must not understate it:
on the OMX a point 2 cm outside the rim at bearing 0 is not graspable; on the
Edu:1 a point outside the sector (bearing ±120°) is not. Radii are measured in
the BASE frame, from its origin — where the editor centres the ring.

The Edu:1 rim is deliberately conservative (its profile comment: r = 0.37 still
solves at the grasp plane but only with a ~19 mm hover, so the advertised outer
stays 0.35), which is why its „outside" half is the sector, not the rim.
"""

from __future__ import annotations

import json
import math

import pytest

from physical_ai_server import robot_profiles as rp

from daten_timeout import per_test_time_limit  # noqa: F401
from rig_emulation import Rig, grasp, program

TEST_TIMEOUT_S = 300.0

# What the editor draws for a profile WITHOUT the keys (simConstants.js
# REACH_INNER_M / REACH_OUTER_M, no sector): the shape advertised before.
_EDITOR_FALLBACK = (0.10, 0.28)


def _shape(pid):
    caps = json.loads(rp.capabilities_json(rp.ROBOT_PROFILES[pid]))
    inner = caps.get('reach_inner_m', _EDITOR_FALLBACK[0])
    outer = caps.get('reach_outer_m', _EDITOR_FALLBACK[1])
    sector = caps.get('reach_sector_deg', [-180, 180])
    return float(inner), float(outer), [float(v) for v in sector]


def _graspable(monkeypatch, pid, r, bearing_deg):
    a = math.radians(bearing_deg)
    obj = {'type': 'wuerfel', 'x': r * math.cos(a), 'y': r * math.sin(a), 'yaw': 0.2}
    rig = Rig(monkeypatch, pid, objects=[obj], simulated=True)
    phase = rig.run(program(grasp()))
    return phase == 'finished' and rig.world.held_key() == 0


@pytest.mark.parametrize('pid', ['omx_full', 'edu1_studio'])
def test_every_point_of_the_advertised_shape_is_graspable_and_2cm_outside_at_bearing_0_is_not(
        monkeypatch, pid):
    inner, outer, (lo, hi) = _shape(pid)
    # Six bearings across the advertised sector (inside its edges, which a
    # full ring shares at ±180°).
    span = hi - lo
    bearings = [lo + span * (k + 0.5) / 6.0 for k in range(6)]
    if span < 360.0:
        bearings = [lo, hi] + bearings[1:-1]
    missing = [(r, b) for r in (inner, 0.5 * (inner + outer), outer) for b in bearings
               if not _graspable(monkeypatch, pid, r, b)]
    assert missing == [], f'{pid}: advertised but not graspable at {missing}'
    if span >= 360.0:
        assert not _graspable(monkeypatch, pid, outer + 0.02, 0.0), (
            f'{pid}: 2 cm outside the advertised rim at bearing 0 is still '
            'graspable — the shape understates the reach')
    else:
        for b in (hi + 30.0, lo - 30.0):
            assert not _graspable(monkeypatch, pid, 0.5 * (inner + outer), b), (
                f'{pid}: bearing {b}° outside the advertised sector is graspable')

"""The reach shape the editor draws is the reach the arm has (owner decision
Q6; spec D6 / N6 / N11; principle 6: the sim is never more permissive than the
rig, and reach is never widened from IK alone).

The manifest carries the shape per profile: OMX ``reach_inner_m`` 0.10 /
``reach_outer_m`` 0.25 (the minimum over bearings at the rig's grasp height,
rounded down — the drawn 0.28 overstated the forward reach) and its
``reach_sector_deg`` (−100°, 100°), the joint-1 window of the camera tower
(spec 40 S6.3, §6.1 O4); the V1 its ``reach_sector_deg`` (−90°, 90°) beside
0.10 / 0.34 (it cannot turn behind itself). Every point of the advertised shape
must be graspable in the simulator (whose table now equals the rig's, Q13), and
the shape must not understate it: a point outside the sector (bearing ±130° on
the OMX, ±120° on the V1) is not. Radii are measured in the BASE frame, from
its origin — where the editor centres the ring. The simulator's arm is the
node's (``arm_model.for_simulator``: the plate's lift and the joint-1 window).

The V1 ring is deliberately conservative on both edges (its profile comment:
the grasp plane reaches 0.3665 but the 60 mm hover only 0.3415, so the
advertised outer is 0.34; the inner 0.10 keeps an open claw off the base tower
although the solver reaches 0.0555), which is why its „outside" half is the
sector, not the rim.
"""

from __future__ import annotations

import json
import math

import pytest

from physical_ai_server import robot_profiles as rp
from physical_ai_server.workflow import arm_model

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
    sim_ik = arm_model.for_simulator(rig.profile.build_ik())
    rig.ik = rig.arm._ik = sim_ik
    rig.mgr._ik_factory = lambda: sim_ik
    phase = rig.run(program(grasp()))
    return phase == 'finished' and rig.world.held_key() == 0


@pytest.mark.parametrize('pid', ['omx_full', 'v1_studio'])
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

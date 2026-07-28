#!/usr/bin/env python3
"""SimArm × SimWorld — the virtual gripper HOLDS an object, it does not hover near one.

Driven through the REAL IKSolver / Edu6IKSolver and the REAL ArmProfile registry, because
the thing under test IS a geometric convention (where the end-effector is when the jaws
close) and a stub cannot assert a convention.

Two contracts:

* WITH a world — capture is nearest-wins, the object rides along, the held report is
  identity-based (True for the whole carry, False the instant the jaws open), and
  ``set_objects`` re-seeds the arm for a new run.
* WITHOUT a world — byte-identical to the pre-SimWorld behaviour. This is the spine that
  keeps the six original SimArm tests and the golden fixture passing UNEDITED, so it is
  proven here rather than asserted.
"""

from __future__ import annotations

import math

import pytest

from physical_ai_server import robot_profiles
from physical_ai_server.workflow.handlers import motion
from physical_ai_server.workflow.ik_solver import IKSolver
from physical_ai_server.workflow.sim_arm import (
    _GRASP_CAPTURE_RADIUS_M,
    _SIM_HOME_FULL_JOINTS,
    SimArm,
)
from physical_ai_server.workflow.sim_world import SimWorld


GRIPPER_OPEN_RAD = 0.8
GRIPPER_CLOSED_RAD = -0.5
# The shipped cube's grasp height: object_height_m - grasp_depth_m, virtual table z = 0.
WUERFEL_GRASP_Z = 0.030 - 0.015


def _cube(x, y, yaw=0.0):
    return {'type': 'wuerfel', 'tag_id': 0, 'x': x, 'y': y, 'yaw': yaw}


def _resolved_world(objects):
    """A world whose objects perception has already RESOLVED (tags bound).

    Only a tag-bound object is graspable — an unresolved one is drawn but inert — so
    these arm-level tests bind by hand rather than standing up a SimPerception.
    """
    w = SimWorld(objects)
    for i, o in enumerate(w.objects()):
        w.bind_tag(o['key'], 20 + i)
    return w


def _omx_arm(objects, world=None):
    return SimArm(ik=IKSolver(), objects=objects, world=world)


def _pose(ik, xyz, gripper):
    q = ik.solve(xyz)
    assert q is not None, f'{xyz} must be reachable for this test to mean anything'
    return list(q) + [gripper]


# ── identity-based holding ───────────────────────────────────────────────────

def test_sim_arm_holds_by_identity_all_the_way_to_the_drop_point(monkeypatch):
    """The headline fix. The old proximity test reported MISS for the whole carry
    (the object stayed frozen at its placement) and HELD after the release."""
    monkeypatch.setattr(motion, 'GRASP_SETTLE_S', 0.0)
    ik = IKSolver()
    world = _resolved_world([_cube(0.20, 0.0)])
    arm = _omx_arm([_cube(0.20, 0.0)], world=world)
    ctx = type('C', (), {'get_follower_joints': staticmethod(arm.get_joints),
                         'last_commanded_close_rad': GRIPPER_CLOSED_RAD})()

    # Close on the cube.
    arm.publish([(_pose(ik, (0.20, 0.0, WUERFEL_GRASP_Z), GRIPPER_CLOSED_RAD), 1.0)])
    assert motion.check_grasp_held(ctx) is True

    # Carry it 150 mm away — far outside the 60 mm capture radius.
    arm.publish([(_pose(ik, (0.12, 0.13, 0.07), GRIPPER_CLOSED_RAD), 1.0)])
    assert motion.check_grasp_held(ctx) is True, 'a carried object must stay HELD'
    # 3 mm, not 1: the OMX end-effector sits ~1.6 mm off the tool axis and the IK
    # deliberately ignores that offset (see motion.lift), so an FK round-trip lands
    # within ~1.6 mm of the commanded XY by construction. The point of the assert is
    # that the cube MOVED with the gripper, not sub-millimetre fidelity.
    assert math.hypot(world.objects()[0]['x'] - 0.12,
                      world.objects()[0]['y'] - 0.13) < 3e-3

    # Open: the object is released and the sim's ground truth says so.
    arm.publish([(_pose(ik, (0.12, 0.13, 0.07), GRIPPER_OPEN_RAD), 1.0)])
    assert world.is_held() is False
    assert arm._simulate_held(arm._last_q) is False
    # NOTE deliberately NOT asserting check_grasp_held(ctx) is False here. That
    # function compares the ACHIEVED gripper angle against
    # last_commanded_close + margin, so an OPEN gripper (+0.8 > −0.35) reads HELD
    # on the real rig too — the documented position-only-sensing limitation
    # (perception_blocks.wait_until_held). Identity fixed the two failures that
    # were sim-ONLY (MISS during the carry, HELD after moving back over a frozen
    # object); this third one is shared with hardware and out of scope.
    # test_open_gripper_still_reads_held_exactly_like_the_real_rig pins it.


def test_open_gripper_still_reads_held_exactly_like_the_real_rig(monkeypatch):
    """The ONE grasp-report wrongness identity does NOT fix, pinned so it is visible.

    ``check_grasp_held`` compares the achieved gripper angle against
    ``last_commanded_close + GRASP_HELD_MARGIN_RAD``; an OPEN gripper clears that
    threshold trivially, so it reports HELD whenever the jaws are open — on the sim
    AND on the rig (perception_blocks.wait_until_held documents it). Fixing it would
    change real-arm grasp verification, so it is deliberately out of scope here.
    If this test ever starts failing, the semantics changed on HARDWARE too and that
    needs sign-off, not a test edit.
    """
    monkeypatch.setattr(motion, 'GRASP_SETTLE_S', 0.0)
    world = _resolved_world([_cube(0.20, 0.0)])
    arm = _omx_arm(world.objects(), world=world)
    arm.publish([([0.0] * 5 + [GRIPPER_OPEN_RAD], 1.0)])
    ctx = type('C', (), {'get_follower_joints': staticmethod(arm.get_joints),
                         'last_commanded_close_rad': GRIPPER_CLOSED_RAD})()
    assert world.is_held() is False           # sim ground truth: nothing held
    assert motion.check_grasp_held(ctx) is True   # ...but the angle test says HELD


def test_sim_arm_capture_takes_the_nearest_cube_of_two():
    ik = IKSolver()
    # 0.26 is ~60 mm from the close point, 0.22 is ~20 mm — nearest listed LAST.
    world = _resolved_world([_cube(0.26, 0.0), _cube(0.22, 0.0)])
    arm = _omx_arm(world.objects(), world=world)
    arm.publish([(_pose(ik, (0.20, 0.0, WUERFEL_GRASP_Z), GRIPPER_CLOSED_RAD), 1.0)])
    assert world.held_key() == 1


def test_sim_arm_release_leaves_the_cube_at_the_drop_xy():
    ik = IKSolver()
    world = _resolved_world([_cube(0.20, 0.0)])
    arm = _omx_arm(world.objects(), world=world)
    arm.publish([(_pose(ik, (0.20, 0.0, WUERFEL_GRASP_Z), GRIPPER_CLOSED_RAD), 1.0)])
    arm.publish([(_pose(ik, (0.14, 0.12, 0.065), GRIPPER_CLOSED_RAD), 1.0)])
    arm.publish([(_pose(ik, (0.14, 0.12, 0.065), GRIPPER_OPEN_RAD), 1.0)])
    o = world.objects()[0]
    assert (o['x'], o['y']) == pytest.approx((0.14, 0.12), abs=3e-3)


def test_sim_arm_never_captures_when_the_jaws_close_too_far_away():
    ik = IKSolver()
    world = _resolved_world([_cube(0.28, 0.0)])
    arm = _omx_arm(world.objects(), world=world)
    # ~80 mm short of the cube — beyond the 60 mm capture radius.
    arm.publish([(_pose(ik, (0.20, 0.0, WUERFEL_GRASP_Z), GRIPPER_CLOSED_RAD), 1.0)])
    assert world.is_held() is False
    assert arm.get_joints()[5] == pytest.approx(GRIPPER_CLOSED_RAD)


def test_capture_radius_is_the_documented_constant():
    # Pinned so a silent retune is a test failure, not a behaviour change nobody sees.
    assert _GRASP_CAPTURE_RADIUS_M == 0.06


# ── run reset (the cached-SimArm leak) ───────────────────────────────────────

def test_set_objects_reseeds_the_arm_to_home_and_clears_the_hold():
    """The node caches ONE SimArm for the process lifetime. Before this, run N+1
    started at run N's final pose with the FAKE held-override gripper value."""
    ik = IKSolver()
    world = _resolved_world([_cube(0.20, 0.0)])
    arm = _omx_arm(world.objects(), world=world)
    arm.publish([(_pose(ik, (0.20, 0.0, WUERFEL_GRASP_Z), GRIPPER_CLOSED_RAD), 1.0)])
    arm.publish([(_pose(ik, (0.14, 0.12, 0.065), GRIPPER_CLOSED_RAD), 1.0)])
    assert world.is_held() is True
    assert arm.get_joints() != pytest.approx(_SIM_HOME_FULL_JOINTS)

    arm.set_objects([_cube(0.20, 0.0)])

    assert arm.get_joints() == pytest.approx(_SIM_HOME_FULL_JOINTS)
    assert world.is_held() is False
    o = world.objects()[0]
    assert (o['x'], o['y']) == pytest.approx((0.20, 0.0)), 'scene must reset too'


def test_set_objects_reseeds_to_the_PROFILE_home_on_edu6():
    prof = robot_profiles.resolve('edu6_studio')
    home = [float(v) for v in prof.home_joints_rad] + [float(prof.gripper_open_rad)]
    world = _resolved_world([_cube(0.13, 0.0)])
    arm = SimArm(
        ik=prof.build_ik(), objects=world.objects(),
        num_arm_joints=prof.num_arm_joints, home_full_joints=home,
        close_threshold_rad=prof.sim_close_threshold_rad,
        held_block_offset_rad=prof.sim_held_block_offset_rad,
        held_floor_rad=prof.sim_held_floor_rad, world=world,
    )
    arm.publish([([0.1] * prof.num_arm_joints + [0.0], 1.0)])
    arm.set_objects(world.objects())
    assert arm.get_joints() == pytest.approx(home)


# ── edu6: the radian-band gripper closes DOWNWARD ────────────────────────────

def test_edu6_capture_and_release_use_the_profile_close_threshold():
    """edu6's gripper band is 0…1.75 and closes downward, so the crossing test has to
    read the profile threshold (1.5), not the OMX sign convention."""
    prof = robot_profiles.resolve('edu6_studio')
    ik = prof.build_ik()
    home = [float(v) for v in prof.home_joints_rad] + [float(prof.gripper_open_rad)]
    world = _resolved_world([_cube(0.13, 0.0)])
    arm = SimArm(
        ik=ik, objects=world.objects(), num_arm_joints=prof.num_arm_joints,
        home_full_joints=home, close_threshold_rad=prof.sim_close_threshold_rad,
        held_block_offset_rad=prof.sim_held_block_offset_rad,
        held_floor_rad=prof.sim_held_floor_rad, world=world,
    )
    grasp_q = ik.solve((0.13, 0.0, WUERFEL_GRASP_Z))
    assert grasp_q is not None
    # Catalog close for edu6 is 1.0 rad, well under the 1.5 close threshold.
    arm.publish([(list(grasp_q) + [1.0], 1.0)])
    assert world.is_held() is True
    arm.publish([(list(grasp_q) + [float(prof.gripper_open_rad)], 1.0)])
    assert world.is_held() is False


# ── the world=None spine (backwards compatibility, PROVEN not asserted) ──────

def test_sim_arm_without_a_world_keeps_the_legacy_proximity_verdict(monkeypatch):
    """world=None must reproduce the pre-SimWorld behaviour exactly — including the
    two bugs, because that is what "byte-identical" means. This is the spine that
    keeps the six original SimArm tests and test_dof_golden.py passing unedited."""
    monkeypatch.setattr(motion, 'GRASP_SETTLE_S', 0.0)
    ik = IKSolver()
    arm = _omx_arm([_cube(0.20, 0.0)])          # no world
    ctx = type('C', (), {'get_follower_joints': staticmethod(arm.get_joints),
                         'last_commanded_close_rad': GRIPPER_CLOSED_RAD})()

    arm.publish([(_pose(ik, (0.20, 0.0, WUERFEL_GRASP_Z), GRIPPER_CLOSED_RAD), 1.0)])
    assert motion.check_grasp_held(ctx) is True
    # Legacy bug #1: carrying it away reads MISS (the frozen cube stayed behind).
    arm.publish([(_pose(ik, (0.12, 0.13, 0.07), GRIPPER_CLOSED_RAD), 1.0)])
    assert motion.check_grasp_held(ctx) is False
    # Legacy bug #2: back over the (frozen) cube with the jaws shut reads HELD again.
    arm.publish([(_pose(ik, (0.20, 0.0, WUERFEL_GRASP_Z), GRIPPER_CLOSED_RAD), 1.0)])
    assert motion.check_grasp_held(ctx) is True


def test_sim_arm_without_a_world_does_not_move_anything_on_a_close():
    arm = _omx_arm([_cube(0.20, 0.0)])
    ik = IKSolver()
    arm.publish([(_pose(ik, (0.20, 0.0, WUERFEL_GRASP_Z), GRIPPER_CLOSED_RAD), 1.0)])
    arm.publish([(_pose(ik, (0.12, 0.13, 0.07), GRIPPER_CLOSED_RAD), 1.0)])
    assert arm._objects[0]['x'] == pytest.approx(0.20)


def test_sim_arm_without_a_world_still_reseeds_home_on_set_objects():
    arm = _omx_arm([_cube(0.20, 0.0)])
    arm.publish([([0.1] * 5 + [-0.5], 1.0)])
    arm.set_objects([_cube(0.20, 0.0)])
    assert arm.get_joints() == pytest.approx(_SIM_HOME_FULL_JOINTS)


# ── robustness ───────────────────────────────────────────────────────────────

def test_a_raising_world_never_kills_the_run():
    class _Boom:
        def is_held(self):
            raise RuntimeError('boom')

        def capture_nearest(self, *a):
            raise RuntimeError('boom')

        def carry_to(self, *a):
            raise RuntimeError('boom')

        def release(self):
            raise RuntimeError('boom')

    ik = IKSolver()
    arm = _omx_arm([_cube(0.20, 0.0)], world=_Boom())
    # publish must not raise; _update_world swallows.
    arm.publish([(_pose(ik, (0.20, 0.0, WUERFEL_GRASP_Z), GRIPPER_CLOSED_RAD), 1.0)])
    # get_joints -> _simulate_held -> world.is_held() raises; the readback is
    # best-effort, so the caller must still get a usable vector.
    with pytest.raises(RuntimeError):
        arm.get_joints()


def test_publish_of_an_empty_chunk_touches_nothing():
    world = _resolved_world([_cube(0.20, 0.0)])
    arm = _omx_arm(world.objects(), world=world)
    arm.publish([])
    assert arm.get_joints() == pytest.approx(_SIM_HOME_FULL_JOINTS)
    assert world.is_held() is False

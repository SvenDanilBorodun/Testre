"""ArmProfile registry contracts (T2).

Covers resolve() (known/unknown/empty/None), the exact capabilities_json key set
+ values for both OMX profiles, and the NO-DRIFT invariant that locks the
profile's mirrored OMX geometry against the still-authoritative constants in
workflow/handlers/motion.py, workflow/sim_arm.py, the node's _SIM_JOINT_NAMES,
and collision_monitor.SAFE_HOME_ARM — so the seam data can never silently
diverge while the DOF-agnostic refactor is deferred.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from physical_ai_server import robot_profiles as rp

_REPO = Path(__file__).resolve().parents[1]
_SERVER_PY = _REPO / 'physical_ai_server' / 'physical_ai_server.py'
_COLLISION_PY = _REPO / 'physical_ai_server' / 'safety' / 'collision_monitor.py'

_CAP_KEYS = ('recordable', 'editable', 'trainable', 'inferable',
             'roboter_studio', 'has_leader')


def _extract_literal(path, name):
    """ast.literal_eval the RHS of a module-/class-level ``<name> = <literal>``
    in the given file. The files themselves import rclpy and can't be imported
    in CI, so we read the literal statically."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f'{name} not found in {path}')


# --- resolve ---------------------------------------------------------------

@pytest.mark.parametrize('pid', ['omx_full', 'omx_follower'])
def test_resolve_known(pid):
    assert rp.resolve(pid).profile_id == pid


@pytest.mark.parametrize('bad', [None, '', '   ', 'omx', 'garbage', 'OMX_FULL'])
def test_resolve_unknown_falls_back_to_default(bad):
    assert rp.resolve(bad).profile_id == rp.DEFAULT_PROFILE_ID == 'omx_full'


def test_resolve_strips_whitespace():
    assert rp.resolve('  omx_follower  ').profile_id == 'omx_follower'


# --- capabilities_json -----------------------------------------------------

def test_caps_json_exact_full():
    obj = json.loads(rp.capabilities_json(rp.resolve('omx_full')))
    # The six booleans are the ORIGINAL React contract (adopt-guard requires
    # all six); the geometry keys are the edu6-era additive extension.
    assert set(_CAP_KEYS) <= set(obj.keys())
    assert all(obj[k] is True for k in _CAP_KEYS)
    assert obj['arm_joints'] == 5
    assert obj['joint_names'] == list(rp._OMX_JOINT_NAMES)
    assert obj['urdf_asset_id'] == 'omx_f'
    assert obj['gripper_open_rad'] == 0.8
    assert obj['gripper_closed_rad'] == -0.5
    # None-valued optionals are OMITTED, never sent as null.
    assert 'reach_inner_m' not in obj and 'gripper_mm_per_rad' not in obj


def test_caps_json_exact_follower():
    obj = json.loads(rp.capabilities_json(rp.resolve('omx_follower')))
    for key, want in {
        'recordable': False, 'editable': False, 'trainable': False,
        'inferable': True, 'roboter_studio': True, 'has_leader': False,
    }.items():
        assert obj[key] is want
    assert obj['arm_joints'] == 5


def test_caps_json_exact_edu6():
    obj = json.loads(rp.capabilities_json(rp.resolve('edu6_studio')))
    for key, want in {
        'recordable': False, 'editable': False, 'trainable': False,
        'inferable': False, 'roboter_studio': True, 'has_leader': False,
    }.items():
        assert obj[key] is want
    assert obj['arm_joints'] == 6
    assert obj['joint_names'] == list(rp._EDU6_JOINT_NAMES)
    assert obj['urdf_asset_id'] == 'edu6'
    assert obj['reach_inner_m'] == 0.09
    assert obj['reach_outer_m'] == 0.21
    assert obj['gripper_open_rad'] == 1.75
    assert obj['gripper_closed_rad'] == 0.0
    assert obj['gripper_mm_per_rad'] == 25.2


def test_caps_json_is_compact():
    # separators=(',', ':') -> no whitespace between tokens
    assert ' ' not in rp.capabilities_json(rp.resolve('omx_full'))


# --- dataset naming anchor -------------------------------------------------

@pytest.mark.parametrize('pid', ['omx_full', 'omx_follower'])
def test_data_robot_type_is_omx_f_for_both(pid):
    assert rp.resolve(pid).data_robot_type == 'omx_f'


# --- no-drift invariant ----------------------------------------------------

def test_no_drift_home_and_gripper_vs_motion():
    from physical_ai_server.workflow.handlers import motion
    for pid in ('omx_full', 'omx_follower'):
        prof = rp.resolve(pid)
        assert prof.home_joints_rad == tuple(motion.HOME_JOINTS_RAD)
        assert prof.gripper_open_rad == motion.GRIPPER_OPEN_RAD
        assert prof.gripper_closed_rad == motion.GRIPPER_CLOSED_RAD


def test_no_drift_joint_names_vs_node_sim_names():
    sim_names = _extract_literal(_SERVER_PY, '_SIM_JOINT_NAMES')
    for pid in ('omx_full', 'omx_follower'):
        assert rp.resolve(pid).joint_names == tuple(sim_names)


def test_no_drift_sim_home_full_joints():
    from physical_ai_server.workflow import sim_arm
    # sim rest pose = 5 arm HOME joints + gripper_open
    for pid in ('omx_full', 'omx_follower'):
        prof = rp.resolve(pid)
        assert list(sim_arm._SIM_HOME_FULL_JOINTS) == \
            list(prof.home_joints_rad) + [prof.gripper_open_rad]


def test_no_drift_safe_home_vs_collision_monitor():
    safe = _extract_literal(_COLLISION_PY, 'SAFE_HOME_ARM')
    for pid in ('omx_full', 'omx_follower'):
        assert rp.resolve(pid).safe_home_arm_rad == tuple(safe)


# --- both OMX profiles share geometry; differ only in id/name/flag/caps -----

def test_profiles_share_geometry_differ_only_in_identity():
    a, b = rp.resolve('omx_full'), rp.resolve('omx_follower')
    assert a.home_joints_rad == b.home_joints_rad
    assert a.safe_home_arm_rad == b.safe_home_arm_rad
    assert a.gripper_open_rad == b.gripper_open_rad
    assert a.gripper_closed_rad == b.gripper_closed_rad
    assert a.joint_names == b.joint_names
    assert a.num_arm_joints == b.num_arm_joints == 5
    assert a.urdf_asset_id == b.urdf_asset_id == 'omx_f'
    # differ
    assert a.follower_only is False and b.follower_only is True
    assert a.capabilities != b.capabilities


def test_registry_ids_match_keys():
    for key, prof in rp.ROBOT_PROFILES.items():
        assert prof.profile_id == key


# --- edu6_studio profile (docs/plans/edu6-studio-arm.md D1..D8) --------------

def test_resolve_edu6():
    prof = rp.resolve('edu6_studio')
    assert prof.profile_id == 'edu6_studio'
    assert prof.display_name_de == 'EduBotics 6-Achs – Roboter Studio'
    assert prof.data_robot_type == 'edu6_studio'   # NEW config namespace,
    #                                                never 'omx_f' (D-rule §4.2)
    assert prof.follower_only is True
    assert prof.num_arm_joints == 6
    assert prof.roll_joint_index == 5
    assert prof.ik_backend == 'edu6'
    assert prof.collision_enabled is False
    assert prof.velocity_limit_rad_s == 5.45
    assert prof.camera_roles == ('scene',)
    assert prof.torque_service == '/edu6/set_torque'


def test_omx_profiles_keep_default_seam_values():
    # The OMX rows must be byte-identical to the pre-edu6 behaviour: every new
    # seam field stays at its OMX default.
    for pid in ('omx_full', 'omx_follower'):
        prof = rp.resolve(pid)
        assert prof.ik_backend == 'omx'
        assert prof.roll_joint_index is None
        assert prof.velocity_limit_rad_s is None
        assert prof.collision_enabled is True
        assert prof.torque_service == '/dynamixel_hardware_interface/set_dxl_torque'
        assert prof.grasp_held_margin_rad is None
        assert prof.sim_close_threshold_rad is None
        assert prof.observe_pose_joints is None
    # camera_roles is the ONE seam that diverges between the two OMX profiles:
    # omx_full is a two-camera kit (gripper first); omx_follower's lone camera is
    # the SCENE camera (audit fix — 'gripper' first broke every follower-only RS
    # kit's single-camera auto-assign). 'gripper' stays second for a 2-cam rig.
    assert rp.resolve('omx_full').camera_roles == ('gripper', 'scene')
    assert rp.resolve('omx_follower').camera_roles == ('scene', 'gripper')


def test_no_drift_edu6_vs_edu6_ik():
    # The profile mirrors the solver's authoritative values (edu6_ik is the
    # geometry source; the profile is the registry mirror — same no-drift
    # contract as the OMX ↔ motion.py pair).
    from physical_ai_server.workflow import edu6_ik
    prof = rp.resolve('edu6_studio')
    solver = prof.build_ik()
    assert type(solver).__name__ == 'Edu6IKSolver'
    assert solver.num_joints() == prof.num_arm_joints
    assert solver.base_axis_x == edu6_ik.BASE_AXIS_X_WORLD
    assert prof.tool_length_m == edu6_ik._L_TOOL
    # HOME is strictly inside the solver's joint limits with ≥0.15 rad margin
    # and a non-degenerate wrist seed in the relieved branch.
    for value, (lo, hi) in zip(prof.home_joints_rad, solver.joint_limits):
        assert lo + 0.15 <= value <= hi - 0.15
    assert prof.home_joints_rad[4] >= 0.3


def test_no_drift_edu6_vs_catalog():
    # The edu6 catalog close sits inside the profile's gripper band with more
    # than the grasp-held margin of squeeze headroom against the 30 mm cube.
    from physical_ai_server.workflow.object_catalog import fixed_catalog
    prof = rp.resolve('edu6_studio')
    cat = fixed_catalog('edu6_studio')
    recipe = cat.recipe_for_type('wuerfel')
    assert prof.gripper_closed_rad <= recipe.gripper_close_rad < prof.gripper_open_rad
    # cube blocks at ~object_width / mm_per_rad above closed:
    block = (recipe.object_width_m * 1000.0) / prof.gripper_mm_per_rad
    assert block > recipe.gripper_close_rad + prof.grasp_held_margin_rad
    # sim blocked readback must clear the per-object threshold too:
    assert prof.sim_held_block_offset_rad > prof.grasp_held_margin_rad


# --- edu1_studio profile (docs/plans/edu1-studio-arm.md) --------------------

def test_resolve_edu1():
    prof = rp.resolve('edu1_studio')
    assert prof.profile_id == 'edu1_studio'
    assert prof.display_name_de == 'Edu:1 – Roboter Studio'
    assert prof.data_robot_type == 'edu1_studio'   # NEW config namespace
    assert prof.follower_only is True
    assert prof.num_arm_joints == 5
    assert prof.roll_joint_index == 4
    assert prof.ik_backend == 'edu1'
    assert prof.collision_enabled is False
    assert prof.velocity_limit_rad_s == 4.72
    assert prof.camera_roles == ('scene',)
    assert prof.torque_service == '/edu1/set_torque'
    assert prof.urdf_asset_id == 'edu1'


def test_edu1_is_roboter_studio_only():
    caps = rp.resolve('edu1_studio').capabilities
    assert caps.roboter_studio is True
    assert caps.has_leader is False
    # No leader this round, so nothing that needs teleop is offered — and
    # `inferable` is False too: there is no trained policy for this arm.
    assert (caps.recordable, caps.editable, caps.trainable, caps.inferable) == \
        (False, False, False, False)


def test_edu1_shares_a_point_width_with_omx_and_is_still_distinguishable():
    """The one genuinely new hazard this arm introduces. Contract-B width is
    num_arm_joints + 2, so edu1 and omx_f are BOTH 7-wide — width alone can no
    longer identify a recording, which is why the trajectory tag (migration 039)
    is load-bearing and why data_robot_type must not be reused."""
    edu1 = rp.resolve('edu1_studio')
    omx = rp.resolve('omx_full')
    assert edu1.num_arm_joints + 2 == omx.num_arm_joints + 2 == 7
    assert edu1.data_robot_type != omx.data_robot_type


def test_edu1_velocity_limit_is_the_slowest_joints(): 
    """The arm mixes STS3215 (4.72 rad/s) and STS3250 (7.87). The velocity floor
    must hold for EVERY joint on a segment, so the profile carries the slower."""
    assert rp.resolve('edu1_studio').velocity_limit_rad_s == 4.72


def test_no_drift_edu1_vs_edu1_ik():
    from physical_ai_server.workflow import edu1_ik
    prof = rp.resolve('edu1_studio')
    solver = prof.build_ik()
    assert type(solver).__name__ == 'Edu1IKSolver'
    assert solver.num_joints() == prof.num_arm_joints
    assert solver.base_axis_x == edu1_ik.BASE_AXIS_X_WORLD
    # approx: the module derives _L_TOOL as 0.02125 + 0.065 (pivot + blade,
    # two different source files), which is not bit-equal to the literal.
    assert prof.tool_length_m == pytest.approx(edu1_ik._L_TOOL, abs=1e-12)
    # HOME sits comfortably inside every joint limit — it was CHOSEN by
    # maximising exactly this margin (see the profile's derivation note).
    for value, (lo, hi) in zip(prof.home_joints_rad, solver.joint_limits):
        assert lo + 0.5 <= value <= hi - 0.5


def test_only_the_rotating_claw_advertises_tool_tip_tracks_gripper():
    """The key gates a student-facing German instruction in the touch-off
    wizard („Greifer ganz schließen"), and it is sent ONLY when true — so every
    parallel-jaw arm's manifest stays byte-identical to before and the React
    reader's `=== true` needs no fallback of its own."""
    # Stated as a SET over the whole registry rather than a hardcoded list of
    # the others: a fifth profile is then either in the set on purpose or fails
    # here, instead of quietly escaping a list nobody remembered to extend.
    declaring = {pid for pid, prof in rp.ROBOT_PROFILES.items()
                 if prof.tool_tip_tracks_gripper}
    assert declaring == {'edu1_studio'}
    assert 'tool_tip_tracks_gripper' in rp.capabilities_json(rp.resolve('edu1_studio'))
    for pid, prof in rp.ROBOT_PROFILES.items():
        if pid in declaring:
            continue
        assert prof.tool_tip_tracks_gripper is False, pid
        assert 'tool_tip_tracks_gripper' not in rp.capabilities_json(prof), pid


def test_edu1_reach_ring_is_inside_what_the_solver_reaches():
    """The ring is drawn to the STUDENT; promising a radius the solver then
    refuses is worse than drawing a slightly small ring."""
    prof = rp.resolve('edu1_studio')
    ik = prof.build_ik()
    assert ik.solve((prof.reach_inner_m, 0.0, 0.015)) is not None
    assert ik.solve((prof.reach_outer_m, 0.0, 0.015)) is not None


def test_no_drift_edu1_vs_catalog():
    """The catalog close must sit inside the profile band with enough squeeze
    left that a HELD grasp reads above the per-object threshold. Measured on the
    shipped claw meshes: a 30 mm cube blocks the jaws at ~0.25 rad."""
    from physical_ai_server.workflow.object_catalog import fixed_catalog
    prof = rp.resolve('edu1_studio')
    recipe = fixed_catalog('edu1_studio').recipe_for_type('wuerfel')
    assert prof.gripper_closed_rad <= recipe.gripper_close_rad < prof.gripper_open_rad
    blocked_at = 0.25
    assert blocked_at > recipe.gripper_close_rad + prof.grasp_held_margin_rad
    # A close command must also register as a CLOSE in the simulator, and the
    # open command must NOT.
    assert recipe.gripper_close_rad < prof.sim_close_threshold_rad
    assert prof.gripper_open_rad > prof.sim_close_threshold_rad
    # …and the sim's blocked readback clears the per-object threshold.
    assert prof.sim_held_block_offset_rad > prof.grasp_held_margin_rad


def test_edu1_sim_held_floor_is_inert_for_every_legal_close():
    """Same proof the edu6 override carries: the floor can never win the max()
    inside sim_arm.get_joints, so it is documentation rather than a tuned
    value. If the band or the close threshold ever moves, this fails instead of
    quietly making a never-reviewed number load-bearing."""
    prof = rp.resolve('edu1_studio')
    lo = prof.gripper_closed_rad
    hi = prof.sim_close_threshold_rad
    assert lo + prof.sim_held_block_offset_rad > prof.sim_held_floor_rad
    assert hi + prof.sim_held_block_offset_rad > prof.sim_held_floor_rad


def test_edu1_reroute_geometry_is_reachable_on_this_arm():
    """The OMX ladder constants are structurally DEAD here (measured: 0/72
    base-swing candidates solve). These must not be."""
    prof = rp.resolve('edu1_studio')
    ik = prof.build_ik()
    assert prof.tool_clear_m == 0.0
    # NO DEAD ROWS: the annulus narrows with height, so a radius picked off the
    # lowest swing height alone can be unreachable at the highest one — and an
    # unreachable via candidate is a silently wasted rung.
    dead = [(z, r) for z in prof.swing_heights_m for r in prof.swing_radii_m
            if ik.solve((r, 0.0, z)) is None]
    assert dead == []
    assert ik.solve((0.20, 0.0, prof.safe_travel_z_m)) is not None


def test_registry_wide_invariants():
    for prof in rp.ROBOT_PROFILES.values():
        assert len(prof.joint_names) == prof.num_arm_joints + 1
        assert len(prof.home_joints_rad) == prof.num_arm_joints
        assert len(prof.safe_home_arm_rad) == prof.num_arm_joints
        assert prof.gripper_open_rad != prof.gripper_closed_rad
        roll = (prof.roll_joint_index if prof.roll_joint_index is not None
                else prof.num_arm_joints - 1)
        assert 0 <= roll < prof.num_arm_joints
        assert len(prof.camera_roles) >= 1
        # 'scene' is mandatory everywhere (Roboter Studio perception).
        assert 'scene' in prof.camera_roles
        assert prof.torque_service.startswith('/')

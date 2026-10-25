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
    # all six); the geometry keys are the additive extension.
    assert set(_CAP_KEYS) <= set(obj.keys())
    assert all(obj[k] is True for k in _CAP_KEYS)
    assert obj['arm_joints'] == 5
    assert obj['joint_names'] == list(rp._OMX_JOINT_NAMES)
    assert obj['urdf_asset_id'] == 'omx_f'
    assert obj['gripper_open_rad'] == 0.8
    assert obj['gripper_closed_rad'] == -0.5
    # None-valued optionals are OMITTED, never sent as null. Intent kept; since
    # the semantics round (D6, N11) the OMX carries its measured reach shape
    # (0.10 / 0.25 m, no sector), so the omitted example is gripper_mm_per_rad.
    assert obj['reach_inner_m'] == 0.10 and obj['reach_outer_m'] == 0.25
    assert 'reach_sector_deg' not in obj
    assert 'gripper_mm_per_rad' not in obj


def test_caps_json_exact_follower():
    obj = json.loads(rp.capabilities_json(rp.resolve('omx_follower')))
    for key, want in {
        'recordable': False, 'editable': False, 'trainable': False,
        'inferable': True, 'roboter_studio': True, 'has_leader': False,
    }.items():
        assert obj[key] is want
    assert obj['arm_joints'] == 5


# The V1 manifest, byte for byte (21 keys; the OMX sends 18).
_V1_MANIFEST = (
    '{"recordable":false,"editable":false,"trainable":false,"inferable":false,'
    '"roboter_studio":true,"has_leader":false,"arm_joints":5,'
    '"joint_names":["joint1","joint2","joint3","joint4","joint5","RL_joint"],'
    '"urdf_asset_id":"v1","gripper_open_rad":0.9,"gripper_closed_rad":0.0,'
    '"display_de":"V1 – Roboter Studio","camera_roles":["scene"],'
    '"code_languages":["python","java"],"cloud_inference":false,'
    '"reach_inner_m":0.1,"reach_outer_m":0.34,"reach_sector_deg":[-90,90],'
    '"sim_close_threshold_rad":0.5,"tool_tip_tracks_gripper":true,'
    '"help_de":"Der 5-Achs-Arm V1 von EduBotics mit einer Szenen-Kamera. '
    'Nur für Roboter Studio (Greifen & Programmieren)."}'
)


def test_caps_json_exact_v1():
    raw = rp.capabilities_json(rp.resolve('v1_studio'))
    obj = json.loads(raw)
    want = json.loads(_V1_MANIFEST)
    assert obj == want
    assert list(obj) == list(want)          # key order too
    assert raw == json.dumps(want, separators=(',', ':'))
    assert len(obj) == 21
    assert 'gripper_mm_per_rad' not in obj  # a rotating claw: degrees, not mm


def test_caps_json_is_compact():
    # separators=(',', ':') -> no whitespace BETWEEN TOKENS. Asserted by
    # round-tripping rather than by `' ' not in ...`: since the manifest gained
    # `display_de`/`help_de`, the payload legitimately contains spaces INSIDE
    # string values ('OMX – Voll'), and the old spelling would have failed on
    # correct output. Re-serialising the parsed object with the same separators
    # and comparing byte-for-byte tests exactly the property the comment claims.
    raw = rp.capabilities_json(rp.resolve('omx_full'))
    assert raw == json.dumps(json.loads(raw), separators=(',', ':'))
    # `ensure_ascii` stays at its default True, so the German text travels as
    # escapes and the wire payload is pure ASCII; JSON.parse restores it in the
    # browser. Asserted rather than assumed: an umlaut surviving the round trip
    # is the whole point of putting German on this wire.
    assert raw.isascii()
    assert json.loads(raw)['display_de'] == 'OMX – Voll'
    assert 'führst' in json.loads(raw)['help_de']


# --- identity keys (Start-page hero) ---------------------------------------

@pytest.mark.parametrize('pid', ['omx_full', 'omx_follower', 'v1_studio'])
def test_caps_json_carries_german_identity(pid):
    """Every profile puts a German NAME and camera roles on the wire.

    React renders `display_de` in the Start-page hero; without it the student
    reads the raw profile id. `help_de` is optional by design (omitted when
    empty), but every shipped profile has one today.
    """
    profile = rp.resolve(pid)
    obj = json.loads(rp.capabilities_json(profile))
    assert obj['display_de'] == profile.display_name_de
    assert obj['display_de'].strip()
    assert obj['camera_roles'] == list(profile.camera_roles)
    assert obj['help_de'] == profile.help_de
    assert obj['help_de'].strip()


def test_caps_json_omits_empty_help():
    """An empty `help_de` is OMITTED, not sent as ''. Same rule as the
    None-valued geometry optionals, so the React side never renders a blank
    paragraph and the manifest of a future terse profile stays tight."""
    import dataclasses
    bare = dataclasses.replace(rp.resolve('omx_full'), help_de='')
    assert 'help_de' not in json.loads(rp.capabilities_json(bare))


def test_german_identity_matches_the_gui_registry():
    """The server, the Windows GUI and the Pi agent must name a robot the same.

    A student meets `display_de` twice — in the setup wizard (GUI/Pi) and now
    on the Start page (server -> React). Two spellings of one robot is exactly
    the confusion this key exists to remove, so the three registries are held
    together here rather than by review. Parsed with `ast.literal_eval` for the
    same reason the GUI file says to keep it literal-parseable.
    """
    import ast
    import pathlib
    repo = pathlib.Path(__file__).resolve().parents[3]
    gui_path = repo / 'robotis_ai_setup' / 'gui' / 'app' / 'constants.py'
    if not gui_path.exists():
        # Only reachable outside a full checkout (the shipped image carries the
        # server package without robotis_ai_setup). Skipping there is right;
        # skipping because the CONTENTS drifted would not be, which is why the
        # assertions below are hard failures.
        pytest.skip('not a full repo checkout — GUI registry unavailable')
    gui = gui_path.read_text(encoding='utf-8')
    start = gui.index('ROBOT_PROFILES = {')
    literal = gui[start + len('ROBOT_PROFILES = '):]
    depth, end = 0, None
    for i, ch in enumerate(literal):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    gui_profiles = ast.literal_eval(literal[:end])
    for pid, row in gui_profiles.items():
        profile = rp.resolve(pid)
        assert profile.profile_id == pid, f'{pid} unknown to the server registry'
        assert profile.display_name_de == row['display_de'], pid
        assert profile.help_de == row['help_de'], pid


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


def test_omx_profiles_keep_default_seam_values():
    # The OMX rows must be byte-identical to the pre-Feetech behaviour: every
    # new seam field stays at its OMX default.
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


# --- v1_studio profile ------------------------------------------------------

def test_resolve_v1():
    prof = rp.resolve('v1_studio')
    assert prof.profile_id == 'v1_studio'
    assert prof.display_name_de == 'V1 – Roboter Studio'
    assert prof.data_robot_type == 'v1_studio'   # NEW config namespace
    assert prof.follower_only is True
    assert prof.num_arm_joints == 5
    assert prof.roll_joint_index == 4
    assert prof.ik_backend == 'v1'
    assert prof.collision_enabled is False
    assert prof.velocity_limit_rad_s == 4.72
    assert prof.camera_roles == ('scene',)
    assert prof.torque_service == '/v1/set_torque'
    assert prof.urdf_asset_id == 'v1'
    assert prof.home_joints_rad == (0.0, 0.64, 1.48, 0.90, 0.0)
    assert prof.safe_home_arm_rad == prof.home_joints_rad
    assert (prof.gripper_open_rad, prof.gripper_closed_rad) == (0.90, 0.0)
    assert prof.tool_length_m == 0.1086
    assert prof.pickup_close_rad is None


def test_v1_is_roboter_studio_only():
    caps = rp.resolve('v1_studio').capabilities
    assert caps.roboter_studio is True
    assert caps.has_leader is False
    # No leader, so nothing that needs teleop is offered — and `inferable` is
    # False too: there is no trained policy for this arm.
    assert (caps.recordable, caps.editable, caps.trainable, caps.inferable) == \
        (False, False, False, False)


def test_v1_shares_a_point_width_with_omx_and_is_still_distinguishable():
    """Contract-B width is num_arm_joints + 2, so v1 and omx_f are BOTH 7-wide —
    width alone cannot identify a recording, which is why the trajectory tag is
    load-bearing and why data_robot_type must not be reused."""
    v1 = rp.resolve('v1_studio')
    omx = rp.resolve('omx_full')
    assert v1.num_arm_joints + 2 == omx.num_arm_joints + 2 == 7
    assert v1.data_robot_type != omx.data_robot_type


def test_v1_velocity_limit_is_the_slowest_joints():
    """The arm mixes STS3215 (4.72 rad/s) and STS3250 (7.87). The velocity floor
    must hold for EVERY joint on a segment, so the profile carries the slower."""
    assert rp.resolve('v1_studio').velocity_limit_rad_s == 4.72


def test_no_drift_v1_vs_v1_ik():
    from physical_ai_server.workflow import v1_ik
    prof = rp.resolve('v1_studio')
    solver = prof.build_ik()
    assert type(solver).__name__ == 'V1IKSolver'
    assert solver.num_joints() == prof.num_arm_joints
    assert solver.base_axis_x == v1_ik.BASE_AXIS_X_WORLD
    # approx: the module derives _L_TOOL as 0.02125 + 0.08735 (pivot + blade,
    # two different source files), which is not bit-equal to the literal.
    assert prof.tool_length_m == pytest.approx(v1_ik._L_TOOL, abs=1e-12)
    # HOME sits comfortably inside every joint limit (binding joint: joint2 at
    # 0.640 rad).
    margins = [min(value - lo, hi - value)
               for value, (lo, hi) in zip(prof.home_joints_rad, solver.joint_limits)]
    assert min(margins) == pytest.approx(0.640, abs=1e-9)
    assert all(m >= 0.5 for m in margins)


def test_only_the_rotating_claw_advertises_tool_tip_tracks_gripper():
    """The key gates a student-facing German instruction in the touch-off
    wizard („Greifer ganz schließen"), and it is sent ONLY when true — so every
    parallel-jaw arm's manifest stays byte-identical to before and the React
    reader's `=== true` needs no fallback of its own."""
    # Stated as a SET over the whole registry rather than a hardcoded list of
    # the others: a new profile is then either in the set on purpose or fails
    # here, instead of quietly escaping a list nobody remembered to extend.
    declaring = {pid for pid, prof in rp.ROBOT_PROFILES.items()
                 if prof.tool_tip_tracks_gripper}
    assert declaring == {'v1_studio'}
    assert 'tool_tip_tracks_gripper' in rp.capabilities_json(rp.resolve('v1_studio'))
    for pid, prof in rp.ROBOT_PROFILES.items():
        if pid in declaring:
            continue
        assert prof.tool_tip_tracks_gripper is False, pid
        assert 'tool_tip_tracks_gripper' not in rp.capabilities_json(prof), pid


def test_v1_reach_ring_is_inside_what_the_solver_reaches():
    """The ring is drawn to the STUDENT; promising a radius the solver then
    refuses is worse than drawing a slightly small ring. It must clear BOTH
    planes a grasp actually visits — the grasp plane and the hover
    ``approach_clear_m`` above it — not just the grasp plane (a grasp-plane-only
    rim gets a silently bisected hover on every rim grasp)."""
    from physical_ai_server.workflow.object_catalog import fixed_catalog
    prof = rp.resolve('v1_studio')
    ik = prof.build_ik()
    recipe = fixed_catalog('v1_studio').recipe_for_type('wuerfel')
    grasp_z = recipe.object_height_m - recipe.grasp_depth_m
    hover_z = grasp_z + recipe.approach_clear_m
    assert (grasp_z, hover_z) == (0.015, 0.075)

    assert prof.reach_inner_m == 0.10
    assert prof.reach_outer_m == 0.34
    for r in (prof.reach_inner_m, prof.reach_outer_m):
        assert ik.solve((r, 0.0, grasp_z)) is not None, r
        assert ik.solve((r, 0.0, hover_z)) is not None, r

    # TIGHT, not merely safe: the grasp plane alone reaches past 0.355, and it
    # is the HOVER that stops the ring there. Without this the outer could be
    # widened back to the grasp-plane edge and the test above would not notice.
    assert ik.solve((0.355, 0.0, grasp_z)) is not None
    assert ik.solve((0.355, 0.0, hover_z)) is None
    # The INNER 0.10 is drawing only (owner decision, rig gate VR15): an open
    # claw meets the base tower below it, but nothing refuses a target there —
    # the solver still reaches well inside the ring.
    assert ik.solve((0.08, 0.0, grasp_z)) is not None
    assert ik.solve((0.08, 0.0, hover_z)) is not None


def test_no_drift_v1_vs_catalog():
    """The catalog close must sit inside the profile band with enough squeeze
    left that a HELD grasp reads above the per-object threshold. On the claw
    meshes a 30 mm cube blocks the claw at 0.195 / 0.206 rad (gear ratio 1 /
    18/19) at the catalog's 15 mm grasp depth (simulated band 0.193–0.220 rad
    over 10–20 mm); the PROVEN lower bound over 10–20 mm is 0.183 rad (an
    independent vertex-in-box check at 10 mm), which still leaves ≥ 0.03 rad
    above the threshold."""
    from physical_ai_server.workflow.object_catalog import fixed_catalog
    prof = rp.resolve('v1_studio')
    recipe = fixed_catalog('v1_studio').recipe_for_type('wuerfel')
    assert recipe.gripper_close_rad == 0.05
    assert prof.gripper_closed_rad <= recipe.gripper_close_rad < prof.gripper_open_rad
    blocked_at = 0.193
    assert blocked_at > recipe.gripper_close_rad + prof.grasp_held_margin_rad
    proven_block_lower_bound = 0.183
    assert (proven_block_lower_bound
            - (recipe.gripper_close_rad + prof.grasp_held_margin_rad)) >= 0.03
    # A close command must also register as a CLOSE in the simulator, and the
    # open command must NOT.
    assert recipe.gripper_close_rad < prof.sim_close_threshold_rad
    assert prof.gripper_open_rad > prof.sim_close_threshold_rad
    # …and the sim's blocked readback clears the per-object threshold and lands
    # inside the simulated block band.
    assert prof.sim_held_block_offset_rad > prof.grasp_held_margin_rad
    assert 0.193 <= recipe.gripper_close_rad + prof.sim_held_block_offset_rad <= 0.220


def test_v1_sim_held_floor_is_inert_for_every_legal_close():
    """The floor can never win the max() inside sim_arm.get_joints, so it is
    documentation rather than a tuned value. If the band or the close threshold
    ever moves, this fails instead of quietly making a never-reviewed number
    load-bearing."""
    prof = rp.resolve('v1_studio')
    lo = prof.gripper_closed_rad
    hi = prof.sim_close_threshold_rad
    assert lo + prof.sim_held_block_offset_rad > prof.sim_held_floor_rad
    assert hi + prof.sim_held_block_offset_rad > prof.sim_held_floor_rad


def test_v1_reroute_geometry_is_reachable_on_this_arm():
    """The OMX ladder constants are sized for a taller arm. These must solve."""
    prof = rp.resolve('v1_studio')
    ik = prof.build_ik()
    assert prof.tool_clear_m == 0.0
    # NO DEAD ROWS: the annulus varies with height, so a radius picked off one
    # swing height alone can be unreachable at another — and an unreachable via
    # candidate is a silently wasted rung.
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


# --- code_languages (Roboter Studio coding suite, WP3 §3.9) ----------------

@pytest.mark.parametrize('pid', ['omx_full', 'omx_follower', 'v1_studio'])
def test_caps_json_advertises_both_code_languages(pid):
    """Every profile's manifest carries code_languages ['python', 'java'] — the
    static image capability React's codeRunBlockReason keys on. Additive: the
    six-boolean adopt-guard is unaffected (a list, not a boolean)."""
    obj = json.loads(rp.capabilities_json(rp.resolve(pid)))
    assert obj['code_languages'] == ['python', 'java']
    # the six-boolean contract still holds beside it.
    assert set(_CAP_KEYS) <= set(obj.keys())


def test_code_languages_matches_the_code_program_twin():
    """The manifest literal is the twin of workflow.code_program.CODE_LANGUAGES
    (kept a literal in robot_profiles rather than imported to avoid pulling the
    RPC/handler stack into the identity module)."""
    from physical_ai_server.workflow.code_program import CODE_LANGUAGES
    obj = json.loads(rp.capabilities_json(rp.resolve('omx_full')))
    assert obj['code_languages'] == list(CODE_LANGUAGES)


# --- cloud_inference (Cloud-Inferenz, spec §5.8, contract C5) ---------------

@pytest.mark.parametrize('pid', sorted(rp.ROBOT_PROFILES))
def test_cloud_inference_equals_inferable(pid):
    """Every profile's manifest carries the additive ``cloud_inference`` key, equal to
    ``inferable`` (a cloud run is an inference run; a Roboter-Studio-only arm has neither).
    It sits right after ``code_languages`` and the six-boolean adopt contract is unchanged."""
    prof = rp.resolve(pid)
    obj = json.loads(rp.capabilities_json(prof))
    assert obj['cloud_inference'] is prof.capabilities.inferable
    assert type(obj['cloud_inference']) is bool
    keys = list(obj)
    assert keys[keys.index('code_languages') + 1] == 'cloud_inference'
    assert set(_CAP_KEYS) <= set(keys)
    # The OMX sends 18 keys (the six booleans, the geometry and identity keys,
    # code_languages, cloud_inference, its reach shape, help_de); the V1 21 (+ its
    # reach sector, sim close threshold and tool_tip_tracks_gripper).
    expected_count = {'omx_full': 18, 'omx_follower': 18, 'v1_studio': 21}[pid]
    assert len(keys) == expected_count, keys


def test_the_v1_manifest_carries_its_reach_sector():
    """N6: the V1 turns ±90° only; the editor draws that sector. A list on the
    wire (JSON); the OMX, which turns further, sends none."""
    obj = json.loads(rp.capabilities_json(rp.resolve('v1_studio')))
    assert obj['reach_sector_deg'] == [-90, 90]
    assert obj['reach_inner_m'] == 0.10 and obj['reach_outer_m'] == 0.34
    omx = json.loads(rp.capabilities_json(rp.resolve('omx_full')))
    assert 'reach_sector_deg' not in omx

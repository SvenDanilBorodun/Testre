"""Phase-3 simulation-runtime tests (Roboter Studio).

Covers the SERVER-side sim seam — ``SimArm`` (virtual arm) + ``SimPerception``
(synthetic AprilTag detections) — and a full sim ``WorkflowManager`` running a
REAL Blockly program through the REAL interpreter + handlers + ``IKSolver``,
exactly as ``physical_ai_server._get_or_create_sim_workflow_manager`` wires it.

The headline contract: a sim ``WorkflowManager`` with swapped callable kwargs
(publisher → ``SimArm.publish``, perception → ``SimPerception``, joints →
``SimArm.get_joints``, calibration → ``{}``) gives byte-identical execution — so
a placed virtual object is actually picked up by driving the same closed-form IK
the rig uses. We assert the published trajectory REACHES the grasp pose.

Deps-free (pure Python + numpy + the real ``IKSolver``); no container / rclpy.
``trajectory_builder.time`` is faked so the 1 s inter-chunk pacing doesn't slow
the suite — the published WAYPOINTS are byte-identical to production.
"""

from __future__ import annotations

import threading
import time
import types

import numpy as np
import pytest

from physical_ai_server.workflow import trajectory_builder
from physical_ai_server.workflow.handlers import motion
from physical_ai_server.workflow.handlers import perception_blocks as pb
from physical_ai_server.workflow.handlers.motion import (
    GRASP_HELD_MAX_RAD,
    GRASP_ROLL_RAD,
    GRIPPER_CLOSED_RAD,
    GRIPPER_OPEN_RAD,
    HOME_JOINTS_RAD,
    _require_seeded_start_pose,
)
from physical_ai_server.workflow.ik_solver import IKSolver
from physical_ai_server.workflow.object_catalog import parse_catalog
from physical_ai_server.workflow.sim_arm import SimArm, _SIM_HOME_FULL_JOINTS
from physical_ai_server.workflow.sim_perception import SimPerception
from physical_ai_server.workflow.tag_pose import grasp_joint5
from physical_ai_server.workflow.workflow_manager import WorkflowManager


# A wuerfel with TWO copies (tags 7, 8) so the aruco_id filter can be exercised.
SIM_CATALOG = parse_catalog({
    'tag_size_m': 0.024,
    'types': {
        'wuerfel': {
            'label_de': 'Würfel',
            'tag_ids': [7, 8],
            'object_height_m': 0.03,
            'grasp_depth_m': 0.012,
            'gripper_close_rad': -0.25,
            'approach_clear_m': 0.06,
        },
    },
})

# grasp z for the wuerfel recipe (virtual table at z=0): height − depth.
WUERFEL_GRASP_Z = 0.03 - 0.012  # 0.018
# A reachable in-annulus placement (matches the motion-handler test family).
OBJ_X, OBJ_Y, OBJ_YAW = 0.20, 0.0, 0.3


@pytest.fixture(autouse=True)
def _fast_chunk_pacing(monkeypatch):
    """Replace ``trajectory_builder.time`` with a fake clock so the 1 s
    inter-chunk sleep doesn't make the suite slow (the published waypoints are
    unchanged — only the wait between chunks is skipped). Mirrors
    test_motion_handlers."""
    state = {'t': 0.0}

    def _monotonic():
        state['t'] += 1000.0
        return state['t']

    fake = types.SimpleNamespace(monotonic=_monotonic, sleep=lambda _s: None)
    monkeypatch.setattr(trajectory_builder, 'time', fake)
    yield


# ── SimArm ────────────────────────────────────────────────────────────────────

def test_sim_arm_seed_gate_returns_home_not_all_zero():
    arm = SimArm()
    q = arm.get_joints()
    assert q == pytest.approx(_SIM_HOME_FULL_JOINTS)
    # HOME has non-zero j2/j3 — never the all-zero "unseeded" sentinel.
    assert not all(abs(v) <= 1e-6 for v in q)
    # The runtime's start-pose gate accepts it (no German "Armstellung … bekannt").
    _require_seeded_start_pose(types.SimpleNamespace(last_full_joints=q))


def test_sim_arm_publish_caches_last_q_and_forwards_each_frame():
    captured: list[list[float]] = []
    arm = SimArm(joint_state_sink=captured.append, ik=IKSolver())
    chunk = [([0.1] * 6, 0.5), ([0.2] * 6, 1.0)]
    arm.publish(chunk)
    # last commanded vector cached (gripper 0.2 ≥ 0 → no held override).
    assert arm.get_joints() == pytest.approx([0.2] * 6)
    # every waypoint forwarded to the virtual joint-state sink, in order.
    assert captured == [[0.1] * 6, [0.2] * 6]


def test_sim_arm_held_when_object_at_grasp_xy(monkeypatch):
    monkeypatch.setattr(motion, 'GRASP_SETTLE_S', 0.0)
    ik = IKSolver()
    arm_q = ik.solve((OBJ_X, OBJ_Y, WUERFEL_GRASP_Z))
    assert arm_q is not None
    arm = SimArm(ik=ik, objects=[
        {'type': 'wuerfel', 'tag_id': 7, 'x': OBJ_X, 'y': OBJ_Y, 'yaw': OBJ_YAW}])
    # Cache a CLOSED grasp pose at the object's XY.
    arm.publish([(list(arm_q) + [GRIPPER_CLOSED_RAD], 1.0)])
    # The jaws are reported partly-blocked (> GRASP_HELD_MAX_RAD) → HELD.
    assert arm.get_joints()[5] > GRASP_HELD_MAX_RAD
    ctx = types.SimpleNamespace(get_follower_joints=arm.get_joints)
    assert motion.check_grasp_held(ctx) is True


def test_sim_arm_empty_close_when_no_object(monkeypatch):
    monkeypatch.setattr(motion, 'GRASP_SETTLE_S', 0.0)
    ik = IKSolver()
    arm_q = ik.solve((OBJ_X, OBJ_Y, WUERFEL_GRASP_Z))
    arm = SimArm(ik=ik, objects=[])  # nothing placed
    arm.publish([(list(arm_q) + [GRIPPER_CLOSED_RAD], 1.0)])
    ctx = types.SimpleNamespace(get_follower_joints=arm.get_joints)
    assert motion.check_grasp_held(ctx) is False


def test_sim_arm_open_gripper_is_never_held_override():
    ik = IKSolver()
    arm_q = ik.solve((OBJ_X, OBJ_Y, WUERFEL_GRASP_Z))
    arm = SimArm(ik=ik, objects=[
        {'type': 'wuerfel', 'tag_id': 7, 'x': OBJ_X, 'y': OBJ_Y, 'yaw': OBJ_YAW}])
    # Object present but gripper OPEN → not a close → no override.
    arm.publish([(list(arm_q) + [GRIPPER_OPEN_RAD], 1.0)])
    assert arm.get_joints()[5] == pytest.approx(GRIPPER_OPEN_RAD)


def test_sim_arm_fk_xyz_round_trips_via_real_ik():
    ik = IKSolver()
    arm_q = ik.solve((OBJ_X, OBJ_Y, 0.05))
    arm = SimArm(ik=ik)
    arm.publish([(list(arm_q) + [GRIPPER_OPEN_RAD], 1.0)])
    xyz = arm.fk_xyz()
    assert xyz is not None
    assert xyz[0] == pytest.approx(OBJ_X, abs=2e-3)
    assert xyz[1] == pytest.approx(OBJ_Y, abs=2e-3)
    assert xyz[2] == pytest.approx(0.05, abs=2e-3)


# ── SimPerception ─────────────────────────────────────────────────────────────

def test_sim_perception_presets_world_and_extras():
    perc = SimPerception(
        [{'type': 'wuerfel', 'tag_id': 7, 'x': OBJ_X, 'y': OBJ_Y, 'yaw': OBJ_YAW}],
        SIM_CATALOG,
    )
    assert perc.apriltag_available() is True
    dets = perc.detect(None, camera='scene', mode='apriltag', aruco_id=None)
    assert len(dets) == 1
    d = dets[0]
    assert d.aruco_id == 7
    assert d.corners_px is None
    assert d.world_xyz_m == pytest.approx((OBJ_X, OBJ_Y, WUERFEL_GRASP_Z))
    assert d.extras['tag_yaw'] == pytest.approx(OBJ_YAW)
    assert d.extras['gripper_close_rad'] == pytest.approx(-0.25)
    assert d.extras['object_type'] == 'wuerfel'


def test_sim_perception_filters_by_aruco_id():
    perc = SimPerception([
        {'type': 'wuerfel', 'tag_id': 7, 'x': 0.20, 'y': 0.0, 'yaw': 0.0},
        {'type': 'wuerfel', 'tag_id': 8, 'x': 0.18, 'y': 0.05, 'yaw': 0.0},
    ], SIM_CATALOG)
    assert len(perc.detect(None, 'scene', 'apriltag', aruco_id=None)) == 2
    only7 = perc.detect(None, 'scene', 'apriltag', aruco_id=7)
    assert [d.aruco_id for d in only7] == [7]


def test_sim_perception_non_apriltag_mode_empty():
    perc = SimPerception(
        [{'type': 'wuerfel', 'tag_id': 7, 'x': 0.2, 'y': 0.0, 'yaw': 0.0}],
        SIM_CATALOG,
    )
    assert perc.detect(None, 'scene', 'color') == []


def test_sim_perception_skips_unknown_type():
    perc = SimPerception(
        [{'type': 'gibtsnicht', 'tag_id': 7, 'x': 0.2, 'y': 0.0, 'yaw': 0.0}],
        SIM_CATALOG,
    )
    assert perc.detect(None, 'scene', 'apriltag') == []


def test_sim_perception_assigns_recipe_tag_ignoring_fe_tag():
    # The front end has no source for real tag ids (GetObjectCatalog exposes only
    # type names), so the SERVER assigns the tag from the type's recipe and
    # IGNORES whatever id the FE sent — a placed object of a known type is always
    # detected with a recipe tag (B1 fix). Here the FE sent a bogus 999.
    perc = SimPerception(
        [{'type': 'wuerfel', 'tag_id': 999, 'x': 0.2, 'y': 0.0, 'yaw': 0.0}],
        SIM_CATALOG,
    )
    dets = perc.detect(None, 'scene', 'apriltag')
    assert len(dets) == 1
    assert dets[0].aruco_id == 7   # recipe.tag_ids[0], not the FE's 999


def test_sim_perception_assigns_per_type_index_and_skips_overflow():
    # wuerfel has TWO tag ids [7, 8]; objects (no FE tag_id at all) get the recipe
    # tags by placement order, and a THIRD wuerfel has no tag to assign so it is
    # skipped (logged) rather than colliding on a reused id.
    perc = SimPerception([
        {'type': 'wuerfel', 'x': 0.20, 'y': 0.00, 'yaw': 0.0},
        {'type': 'wuerfel', 'x': 0.18, 'y': 0.05, 'yaw': 0.0},
        {'type': 'wuerfel', 'x': 0.16, 'y': -0.05, 'yaw': 0.0},
    ], SIM_CATALOG)
    dets = perc.detect(None, 'scene', 'apriltag')
    assert sorted(d.aruco_id for d in dets) == [7, 8]


# ── SimPerception flows through the REAL find_object (no calibration) ──────────

class _SimDetectCtx:
    """A sim-style WorkflowContext stand-in (no calibration), mirroring
    test_grasp_split_blocks._PCtx but driven by SimPerception — proving the
    preset world_xyz_m survives _attach_named_world's no-calib early-return."""

    def __init__(self, objects, catalog=SIM_CATALOG):
        self.ik = IKSolver()
        self.object_catalog = catalog
        self.object_catalog_error = None
        self.perception = SimPerception(objects, catalog)
        self.get_scene_frame = lambda: np.zeros((1, 1, 3), dtype=np.uint8)
        self.get_scene_frame_age = lambda: 0.0
        self.scene_intrinsics = None
        self.scene_extrinsics = None
        self.board_table_z = None
        self.z_table = None
        self.table_plane = None
        self.xy_correction = None
        self.yaw_bias_rad = 0.0
        self.claimed_tags = set()
        self.skipped_tags = set()
        self.absent_since = {}
        self.claim_lock = threading.RLock()
        self.motion_lock = threading.RLock()
        self.last_full_joints = list(HOME_JOINTS_RAD) + [GRIPPER_OPEN_RAD]
        self.last_arm_joints = None
        self.should_stop = lambda: False
        self.logs = []

    def emit_detections(self, dets):
        pass

    def log(self, msg):
        self.logs.append(msg)


def test_find_object_flows_through_sim_perception():
    ctx = _SimDetectCtx(
        [{'type': 'wuerfel', 'tag_id': 7, 'x': OBJ_X, 'y': OBJ_Y, 'yaw': OBJ_YAW}])
    ziel = pb.find_object(ctx, {'object_type': 'wuerfel'})
    assert ziel is not None
    assert ziel.aruco_id == 7
    assert ziel.world_xyz_m == pytest.approx((OBJ_X, OBJ_Y, WUERFEL_GRASP_Z))
    assert ziel.extras['tag_yaw'] == pytest.approx(OBJ_YAW)
    assert ziel.extras['approach_clear_m'] == pytest.approx(0.06)


# ── Full sim WorkflowManager: a placed object is actually grasped ─────────────

def _build_sim_manager(objects, captured_qs, status_events, catalog=SIM_CATALOG):
    """Wire a WorkflowManager exactly like
    physical_ai_server._get_or_create_sim_workflow_manager (publisher →
    SimArm.publish, perception → SimPerception, joints → SimArm, calib → {})."""
    sim_arm = SimArm(joint_state_sink=captured_qs.append, ik=IKSolver(),
                     objects=objects)
    mgr = WorkflowManager(
        publisher=sim_arm.publish,
        ik_factory=lambda: IKSolver(),
        perception_factory=lambda: SimPerception(objects, catalog),
        load_destinations=lambda: {},
        load_calibration=lambda: {},
        emit_status=lambda ev: status_events.append(ev),
        on_finished=lambda phase: status_events.append({'_finished': phase}),
        get_scene_frame=lambda: np.zeros((1, 1, 3), dtype=np.uint8),
        get_scene_frame_age=lambda: 0.0,
        get_current_pose_xyz=lambda: sim_arm.fk_xyz(),
        get_follower_joints=sim_arm.get_joints,
        load_object_catalog=lambda: catalog,
    )
    return mgr, sim_arm


def _terminal_phases(status_events):
    return [e['_finished'] for e in status_events
            if isinstance(e, dict) and '_finished' in e]


def _run_to_completion(mgr, workflow_json, status_events, wid='wf-sim'):
    ok, msg, _unreachable = mgr.start(workflow_json, wid)
    assert ok, msg
    # ``on_finished`` (recorded as a ``_finished`` marker) is the authoritative
    # terminal signal — fired in the daemon's ``finally`` AFTER ``is_running``
    # already flips False, so wait on the marker, not on ``is_running``.
    deadline = time.monotonic() + 15.0
    while not _terminal_phases(status_events) and time.monotonic() < deadline:
        time.sleep(0.01)
    finished = _terminal_phases(status_events)
    assert finished, 'sim workflow did not finish in time'
    errors = [e for e in status_events
              if isinstance(e, dict) and e.get('phase') == 'error']
    assert not errors, f'sim workflow errored: {errors}'
    assert finished[-1] == 'finished', finished


def _arms_match(arm, ref):
    return all(a == pytest.approx(b, abs=1e-9) for a, b in zip(arm, ref))


def _var_get(name):
    return {'type': 'variables_get', 'fields': {'VAR': {'name': name}}}


# A grasp-split program: store „finde Würfel" into a variable, then
# fahre über → senke auf → schließe um → hebe an, each acting on the Greifziel.
_GRASP_SPLIT_PROGRAM = {
    'blocks': {'blocks': [{
        'type': 'variables_set',
        'id': 'set1',
        'fields': {'VAR': {'name': 'Ziel'}},
        'inputs': {'VALUE': {'block': {
            'type': 'edubotics_find_object',
            'id': 'find1',
            'fields': {'OBJECT_TYPE': 'wuerfel'},
        }}},
        'next': {'block': {
            'type': 'edubotics_move_above',
            'id': 'ma1',
            'inputs': {'ZIEL': {'block': _var_get('Ziel')}},
            'next': {'block': {
                'type': 'edubotics_descend_to',
                'id': 'dt1',
                'inputs': {'ZIEL': {'block': _var_get('Ziel')}},
                'next': {'block': {
                    'type': 'edubotics_close_on_object',
                    'id': 'co1',
                    'inputs': {'ZIEL': {'block': _var_get('Ziel')}},
                    'next': {'block': {
                        'type': 'edubotics_lift',
                        'id': 'lift1',
                    }},
                }},
            }},
        }},
    }]},
}


def test_sim_grasp_split_reaches_grasp_via_real_ik():
    import json
    captured: list[list[float]] = []
    status: list = []
    objects = [{'type': 'wuerfel', 'tag_id': 7,
                'x': OBJ_X, 'y': OBJ_Y, 'yaw': OBJ_YAW}]
    mgr, _arm = _build_sim_manager(objects, captured, status)
    _run_to_completion(mgr, json.dumps(_GRASP_SPLIT_PROGRAM), status)

    assert captured, 'no virtual trajectory was published'
    # The split blocks use the ORIENTED roll from the Greifziel's tag yaw.
    ik = IKSolver()
    roll = grasp_joint5(ik.base_yaw(OBJ_X, OBJ_Y), OBJ_YAW, GRASP_ROLL_RAD)
    grasp_ref = ik.solve((OBJ_X, OBJ_Y, WUERFEL_GRASP_Z), roll=roll)
    assert grasp_ref is not None
    # The descend reached the EXACT grasp pose (a published waypoint), gripper
    # still open at that instant (close happens next).
    assert any(_arms_match(q[:5], grasp_ref) and q[5] == pytest.approx(GRIPPER_OPEN_RAD)
               for q in captured), 'descend never reached the grasp pose'
    # End state: the per-object close angle (object grasped + lifted, still held).
    assert captured[-1][5] == pytest.approx(-0.25)


# A canned pickup on a teacher-pinned destination, in sim.
_PICKUP_PIN_PROGRAM = {
    'blocks': {'blocks': [{
        'type': 'edubotics_destination_pin',
        'id': 'pin1',
        'fields': {'NAME': 'A', 'X': '0.20', 'Y': '0.0', 'Z': '0.0'},
        'next': {'block': {
            'type': 'edubotics_pickup',
            'id': 'pick1',
            'inputs': {'TARGET': {'block': {
                'type': 'edubotics_destination_ref',
                'id': 'ref1',
                'fields': {'NAME': 'A'},
            }}},
        }},
    }]},
}


def test_sim_pickup_on_destination_pin_reaches_grasp():
    import json
    captured: list[list[float]] = []
    status: list = []
    mgr, _arm = _build_sim_manager([], captured, status)
    _run_to_completion(mgr, json.dumps(_PICKUP_PIN_PROGRAM), status)

    assert captured, 'no virtual trajectory was published'
    ik = IKSolver()
    # pickup descends to the pin z (0.0) + the conservative grasp clearance.
    grasp_z = 0.0 + motion.GRASP_CLEARANCE_M
    grasp_ref = ik.solve((0.20, 0.0, grasp_z), roll=GRASP_ROLL_RAD)
    assert grasp_ref is not None
    assert any(_arms_match(q[:5], grasp_ref) for q in captured), \
        'pickup never reached the grasp pose'
    # Object held at the end of a canned pickup (full close).
    assert captured[-1][5] == pytest.approx(GRIPPER_CLOSED_RAD)

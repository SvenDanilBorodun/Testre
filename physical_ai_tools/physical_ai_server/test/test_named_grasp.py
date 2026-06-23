"""Named-object detection + grasp tests (Roboter Studio AprilTag grasping).

Drives the perception_blocks named path (_detect_named / see_object /
count_object / grasp_object) against a REAL closed-form IKSolver + a REAL
ObjectCatalog + a stub Perception that returns synthesised tag detections
(corners projected from a known base pose through a known overhead camera). No
container.
"""

from __future__ import annotations

import math
import threading
import types

import numpy as np
import pytest

from physical_ai_server.workflow import trajectory_builder
from physical_ai_server.workflow.handlers import motion
from physical_ai_server.workflow.handlers import perception_blocks as pb
from physical_ai_server.workflow.handlers.motion import (
    GRIPPER_OPEN_RAD,
    HOME_JOINTS_RAD,
    GraspSkip,
    WorkflowError,
)
from physical_ai_server.workflow.ik_solver import IKSolver
from physical_ai_server.workflow.object_catalog import parse_catalog
from physical_ai_server.workflow.perception import Detection
from physical_ai_server.workflow.tag_pose import tag_corner_object_points


# ── fast chunk pacing (same as test_motion_handlers) ─────────────────────────
@pytest.fixture(autouse=True)
def _fast_chunk_pacing(monkeypatch):
    state = {'t': 0.0}

    def _monotonic():
        state['t'] += 1000.0
        return state['t']

    fake = types.SimpleNamespace(monotonic=_monotonic, sleep=lambda _s: None)
    monkeypatch.setattr(trajectory_builder, 'time', fake)
    yield


# ── camera + tag synthesis ───────────────────────────────────────────────────
_K = np.array([[600.0, 0, 320.0], [0, 600.0, 240.0], [0, 0, 1]], dtype=np.float64)


def _overhead_T(cam=(0.20, 0.0, 0.50)):
    R = np.array([[1.0, 0, 0], [0, -1.0, 0], [0, 0, -1.0]], dtype=np.float64)
    T = np.eye(4, dtype=np.float64)
    T[:3, :3] = R
    T[:3, 3] = np.asarray(cam, dtype=np.float64)
    return T


def _rot_z(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=np.float64)


def _project_pts(pts_base, T_cam_to_base):
    T_bc = np.linalg.inv(T_cam_to_base)
    R, t = T_bc[:3, :3], T_bc[:3, 3]
    out = []
    for c in pts_base:
        cam = R @ c + t
        out.append([_K[0, 0] * cam[0] / cam[2] + _K[0, 2],
                    _K[1, 1] * cam[1] / cam[2] + _K[1, 2]])
    return np.array(out, dtype=np.float64)


def _make_detection(tag_id, P, yaw, tag_size, T, object_height):
    """Synthesise a Detection for a tag glued on top of an object: the tag plane
    is at base z = object_height above the table (table at z=0)."""
    z = float(object_height)
    obj = tag_corner_object_points(tag_size)
    corners_base = (_rot_z(yaw) @ obj.T).T + np.array([P[0], P[1], z])
    corners_px = _project_pts(corners_base, T)
    center_px = _project_pts([np.array([P[0], P[1], z])], T)[0]
    return Detection(
        centroid_px=(int(round(center_px[0])), int(round(center_px[1]))),
        bbox_px=(0, 0, 1, 1),
        confidence=1.0,
        label=f'tag{tag_id}',
        aruco_id=tag_id,
        corners_px=corners_px,
    )


class _StubPerception:
    def __init__(self, detections):
        self._dets = detections

    def apriltag_available(self):
        return True

    def detect(self, bgr, camera, mode, color=None, coco_class=None, aruco_id=None):
        if mode != 'apriltag':
            return []
        out = list(self._dets)
        if aruco_id is not None:
            out = [d for d in out if d.aruco_id == aruco_id]
        return out


_CATALOG = parse_catalog({
    'tag_size_m': 0.024,
    'types': {
        'banane': {
            'label_de': 'Banane',
            'tag_ids': [20, 21],
            'object_height_m': 0.040,
            'grasp_depth_m': 0.015,
            'gripper_close_rad': -0.30,
            'approach_clear_m': 0.06,
        },
    },
})


class _Ctx:
    def __init__(self, detections, *, calibrated=True, catalog=_CATALOG):
        self.ik = IKSolver()
        self.perception = _StubPerception(detections)
        self.object_catalog = catalog
        self.object_catalog_error = None
        T = _overhead_T()
        if calibrated:
            self.scene_intrinsics = {'K': _K, 'dist': np.zeros((5, 1))}
            self.scene_extrinsics = T
            self.board_table_z = 0.0
            self.z_table = 0.0
        else:
            self.scene_intrinsics = None
            self.scene_extrinsics = None
            self.board_table_z = None
            self.z_table = None
        self.table_plane = None
        self.motion_lock = threading.RLock()
        self.claim_lock = threading.RLock()
        self.claimed_tags = set()
        self.skipped_tags = set()
        self.should_stop = lambda: False
        self.last_full_joints = list(HOME_JOINTS_RAD) + [GRIPPER_OPEN_RAD]
        self.last_arm_joints = None
        self.destinations = {}
        self.published = []
        self.emitted = []
        self.logs = []

    def get_scene_frame(self):
        return np.zeros((480, 640, 3), dtype=np.uint8)

    def get_scene_frame_age(self):
        return 0.0

    def emit_detections(self, dets):
        self.emitted.append(dets)

    def publisher(self, chunk):
        self.published.append(list(chunk))

    def log(self, msg):
        self.logs.append(msg)

    @property
    def last_commanded(self):
        return self.published[-1][-1][0]


# ── detection filtering + world attach ───────────────────────────────────────
def test_detect_named_filters_to_catalog_ids_and_attaches_world():
    dets = [
        _make_detection(20, (0.20, 0.0), 0.0, 0.024, _overhead_T(), 0.040),
        _make_detection(21, (0.18, 0.06), 0.5, 0.024, _overhead_T(), 0.040),
        _make_detection(99, (0.22, -0.04), 0.0, 0.024, _overhead_T(), 0.040),  # not in catalog
    ]
    ctx = _Ctx(dets)
    out = pb._detect_named(ctx, 'banane')
    ids = sorted(d.aruco_id for d in out)
    assert ids == [20, 21]                          # tag 99 filtered out
    for d in out:
        assert d.world_xyz_m is not None
        # grasp z = z_table + object_height - grasp_depth = 0 + 0.040 - 0.015
        assert d.world_xyz_m[2] == pytest.approx(0.025, abs=1e-6)
        assert d.extras['gripper_close_rad'] == pytest.approx(-0.30)
        assert d.extras['object_type'] == 'banane'
        assert d.extras['tag_yaw'] is not None
    # recovered x,y ≈ true tag-center column
    by_id = {d.aruco_id: d for d in out}
    assert by_id[20].world_xyz_m[0] == pytest.approx(0.20, abs=2e-3)
    assert by_id[20].world_xyz_m[1] == pytest.approx(0.0, abs=2e-3)


def test_detect_named_tag_yaw_tracks_rotation():
    base = _Ctx([_make_detection(20, (0.20, 0.0), 0.2, 0.024, _overhead_T(), 0.040)])
    rot = _Ctx([_make_detection(20, (0.20, 0.0), 0.2 + 0.4, 0.024, _overhead_T(), 0.040)])
    y0 = pb._detect_named(base, 'banane')[0].extras['tag_yaw']
    y1 = pb._detect_named(rot, 'banane')[0].extras['tag_yaw']
    d = (y1 - y0 + math.pi) % (2 * math.pi) - math.pi
    assert abs(d - 0.4) < 1e-3


def test_exclude_ids_drops_claimed():
    dets = [
        _make_detection(20, (0.20, 0.0), 0.0, 0.024, _overhead_T(), 0.040),
        _make_detection(21, (0.18, 0.06), 0.0, 0.024, _overhead_T(), 0.040),
    ]
    ctx = _Ctx(dets)
    out = pb._detect_named(ctx, 'banane', exclude_ids={20})
    assert [d.aruco_id for d in out] == [21]


# ── see / count ──────────────────────────────────────────────────────────────
def test_see_and_count_object():
    dets = [
        _make_detection(20, (0.20, 0.0), 0.0, 0.024, _overhead_T(), 0.040),
        _make_detection(21, (0.18, 0.06), 0.0, 0.024, _overhead_T(), 0.040),
    ]
    assert pb.see_object(_Ctx(dets), {'object_type': 'banane'}) is True
    assert pb.count_object(_Ctx(dets), {'object_type': 'banane'}) == 2
    assert pb.see_object(_Ctx([]), {'object_type': 'banane'}) is False
    assert pb.count_object(_Ctx([]), {'object_type': 'banane'}) == 0


def test_see_object_works_without_grasp_calibration():
    # Visibility does not require touch-off/extrinsic — a tag is "seen" anyway.
    dets = [_make_detection(20, (0.20, 0.0), 0.0, 0.024, _overhead_T(), 0.040)]
    assert pb.see_object(_Ctx(dets, calibrated=False), {'object_type': 'banane'}) is True


# ── grasp_object ─────────────────────────────────────────────────────────────
def test_grasp_object_grasps_nearest_with_live_roll_and_per_object_close():
    near = _make_detection(20, (0.18, 0.0), 0.3, 0.024, _overhead_T(), 0.040)
    far = _make_detection(21, (0.24, 0.05), -0.2, 0.024, _overhead_T(), 0.040)
    ctx = _Ctx([far, near])
    pb.grasp_object(ctx, {'object_type': 'banane'})
    assert ctx.published, 'grasp should publish motion'
    # ends holding the object at the per-object close angle
    assert ctx.last_commanded[5] == pytest.approx(-0.30)
    # the grasp used the live roll for the NEAREST tag (id 20), not the global const
    d = pb._detect_named(_Ctx([near]), 'banane')[0]
    x, y, _z = d.world_xyz_m
    expected_roll = motion.compute_grasp_roll(ctx, x, y, d.extras['tag_yaw'])
    exact = ctx.ik.solve((x, y, 0.025), roll=expected_roll)
    assert exact is not None
    closed_arms = [q[:5] for chunk in ctx.published for q, _t in chunk
                   if q[5] == pytest.approx(-0.30)]
    assert any(all(a == pytest.approx(b, abs=1e-6) for a, b in zip(arm, exact))
               for arm in closed_arms)


def test_grasp_object_claims_on_success():
    ctx = _Ctx([_make_detection(20, (0.20, 0.0), 0.0, 0.024, _overhead_T(), 0.040)])
    pb.grasp_object(ctx, {'object_type': 'banane'})
    assert 20 in ctx.claimed_tags


def test_grasp_object_excludes_claimed_grabs_next():
    near = _make_detection(20, (0.18, 0.0), 0.0, 0.024, _overhead_T(), 0.040)
    far = _make_detection(21, (0.22, 0.04), 0.0, 0.024, _overhead_T(), 0.040)
    ctx = _Ctx([near, far])
    pb.grasp_object(ctx, {'object_type': 'banane'})   # nearest = 20
    assert ctx.claimed_tags == {20}
    pb.grasp_object(ctx, {'object_type': 'banane'})   # 20 excluded -> 21
    assert ctx.claimed_tags == {20, 21}


def test_see_and_count_exclude_claimed():
    dets = [
        _make_detection(20, (0.20, 0.0), 0.0, 0.024, _overhead_T(), 0.040),
        _make_detection(21, (0.18, 0.06), 0.0, 0.024, _overhead_T(), 0.040),
    ]
    ctx = _Ctx(dets)
    assert pb.count_object(ctx, {'object_type': 'banane'}) == 2
    ctx.claimed_tags.add(20)
    assert pb.count_object(ctx, {'object_type': 'banane'}) == 1
    assert pb.see_object(ctx, {'object_type': 'banane'}) is True
    ctx.claimed_tags.add(21)
    assert pb.count_object(ctx, {'object_type': 'banane'}) == 0
    assert pb.see_object(ctx, {'object_type': 'banane'}) is False


def test_wait_until_object_seen_true_when_visible():
    ctx = _Ctx([_make_detection(20, (0.20, 0.0), 0.0, 0.024, _overhead_T(), 0.040)])
    assert pb.wait_until_object_seen(ctx, {'object_type': 'banane', 'timeout': 1}) is True


def test_grasp_object_none_visible_raises_german():
    ctx = _Ctx([])
    with pytest.raises(WorkflowError, match='Banane'):
        pb.grasp_object(ctx, {'object_type': 'banane'})


def test_grasp_object_none_visible_is_recoverable_graspskip():
    # Nothing in view → GraspSkip (recoverable: the loop re-detects next pass),
    # which IS a WorkflowError so a standalone „greife" still fails loud.
    ctx = _Ctx([])
    with pytest.raises(GraspSkip):
        pb.grasp_object(ctx, {'object_type': 'banane'})


def test_grasp_object_orientation_unreadable_skips_instead_of_blind_grasp():
    # A detection whose corners give no yaw (corners_px=None) must NOT be grasped
    # with a blind fixed roll (would mis-pinch an elongated object); it is marked
    # SKIPPED and raises GraspSkip — no motion published, tag not claimed.
    cpx = _project_pts([np.array([0.20, 0.0, 0.040])], _overhead_T())[0]
    d = Detection(
        centroid_px=(int(round(cpx[0])), int(round(cpx[1]))),
        bbox_px=(0, 0, 1, 1), confidence=1.0, label='tag20',
        aruco_id=20, corners_px=None,
    )
    ctx = _Ctx([d])
    with pytest.raises(GraspSkip, match='Ausrichtung'):
        pb.grasp_object(ctx, {'object_type': 'banane'})
    assert 20 in ctx.skipped_tags
    assert 20 not in ctx.claimed_tags
    assert not ctx.published


def test_grasp_object_all_out_of_reach_skips_all_and_raises_graspskip():
    # Every visible instance is outside the reach annulus → skip them ALL (so the
    # loop terminates) and raise GraspSkip; NOT the hard calib error.
    far1 = _make_detection(20, (0.60, 0.0), 0.0, 0.024, _overhead_T(), 0.040)
    far2 = _make_detection(21, (0.62, 0.05), 0.0, 0.024, _overhead_T(), 0.040)
    ctx = _Ctx([far1, far2])
    with pytest.raises(GraspSkip, match='Greifbereich'):
        pb.grasp_object(ctx, {'object_type': 'banane'})
    assert ctx.skipped_tags == {20, 21}
    assert ctx.claimed_tags == set()
    assert not ctx.published


def test_attach_named_world_edge_gate_drops_orientation_on_size_mismatch():
    # A tag physically 0.10 m wide but catalog tag_size_m=0.024 → the
    # back-projected edge is ~4× expected → orientation dropped (yaw None) +
    # German warning, so grasp_object will skip it rather than mis-roll.
    big = _make_detection(20, (0.20, 0.0), 0.3, 0.10, _overhead_T(), 0.040)
    ctx = _Ctx([big])
    out = pb._detect_named(ctx, 'banane')
    assert out[0].extras['tag_yaw'] is None
    assert any('Tag-Größe' in m for m in ctx.logs)


def test_attach_named_world_edge_gate_keeps_orientation_when_size_matches():
    # Control: a correctly-sized tag (0.024 == catalog) keeps its orientation.
    ok = _make_detection(20, (0.20, 0.0), 0.3, 0.024, _overhead_T(), 0.040)
    ctx = _Ctx([ok])
    out = pb._detect_named(ctx, 'banane')
    assert out[0].extras['tag_yaw'] is not None


def test_grasp_object_unknown_type_raises_german():
    ctx = _Ctx([])
    with pytest.raises(WorkflowError, match='Unbekanntes Objekt'):
        pb.grasp_object(ctx, {'object_type': 'giraffe'})


def test_grasp_object_uncalibrated_raises_precise_german():
    dets = [_make_detection(20, (0.20, 0.0), 0.0, 0.024, _overhead_T(), 0.040)]
    ctx = _Ctx(dets, calibrated=False)
    with pytest.raises(WorkflowError, match='kalibriert'):
        pb.grasp_object(ctx, {'object_type': 'banane'})


# ── missing / broken catalog ─────────────────────────────────────────────────
def test_named_block_missing_catalog_raises_german():
    dets = [_make_detection(20, (0.20, 0.0), 0.0, 0.024, _overhead_T(), 0.040)]
    ctx = _Ctx(dets, catalog=None)
    ctx.object_catalog_error = 'Objekt-Katalog nicht gefunden: …'
    with pytest.raises(WorkflowError, match='Katalog'):
        pb.see_object(ctx, {'object_type': 'banane'})

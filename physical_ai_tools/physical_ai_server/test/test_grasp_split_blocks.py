"""Phase-1 grasp-split VALUE/CLAIM block tests (Roboter Studio).

Covers the perception-side split blocks that have non-trivial logic:

- ``find_object`` — detect → select nearest reachable → bake refined yaw into a
  Greifziel; returns ``None`` when nothing graspable is visible, but raises the
  PRECISE German calibration error when instances are visible yet un-projected.
- ``object_position`` — Greifziel → ``{x, y, z}``.
- ``grasp_held`` — Boolean from the gripper readback; RAISES (no silent False)
  when the joint readback is unavailable.
- ``mark_done`` — claims the Greifziel's tag so a "Solange sichtbar" loop using
  the split path still terminates.

A sim-style ctx is used: no calibration on ctx, so ``_attach_named_world``
preserves the pre-set ``world_xyz_m`` exactly as the server-side SIM path will.
Pure Python + a real ``IKSolver`` (no container).
"""

from __future__ import annotations

import threading

import pytest

from physical_ai_server.workflow.handlers import motion
from physical_ai_server.workflow.handlers import perception_blocks as pb
from physical_ai_server.workflow.handlers.motion import (
    GRIPPER_OPEN_RAD,
    HOME_JOINTS_RAD,
    WorkflowError,
)
from physical_ai_server.workflow.ik_solver import IKSolver
from physical_ai_server.workflow.object_catalog import parse_catalog
from physical_ai_server.workflow.perception import Detection


CATALOG = parse_catalog({
    'tag_size_m': 0.024,
    'types': {
        'wuerfel': {
            'label_de': 'Würfel',
            'tag_ids': [7],
            'object_height_m': 0.03,
            'grasp_depth_m': 0.012,
            'gripper_close_rad': -0.25,
            'approach_clear_m': 0.06,
        },
    },
})


def _det(world=(0.20, 0.0, 0.03), tag_yaw=0.3, aruco_id=7):
    return Detection(
        centroid_px=(10, 10),
        bbox_px=(0, 0, 20, 20),
        confidence=1.0,
        label='wuerfel',
        aruco_id=aruco_id,
        world_xyz_m=world,
        corners_px=None,
        extras={'tag_yaw': tag_yaw, 'gripper_close_rad': -0.25},
    )


class _FakePerception:
    def __init__(self, dets):
        self._dets = list(dets)

    def apriltag_available(self):
        return True

    def detect(self, bgr, camera, mode, aruco_id=None):
        if aruco_id is None:
            return list(self._dets)
        return [d for d in self._dets if d.aruco_id == aruco_id]


class _PCtx:
    """A sim-style WorkflowContext stand-in: no calibration (so the named-world
    projection is skipped and the pre-set world_xyz_m is preserved), a fake
    AprilTag perception, and the per-run claim bookkeeping."""

    def __init__(self, dets, follower=None):
        self.ik = IKSolver()
        self.object_catalog = CATALOG
        self.object_catalog_error = None
        self.perception = _FakePerception(dets)
        self.get_scene_frame = lambda: object()
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
        self._follower = follower
        self.logs = []

    def emit_detections(self, dets):
        pass

    def log(self, msg):
        self.logs.append(msg)

    def get_follower_joints(self):
        return self._follower


# ── find_object ──────────────────────────────────────────────────────────────

def test_find_object_returns_greifziel_with_baked_params():
    ctx = _PCtx([_det()])
    ziel = pb.find_object(ctx, {'object_type': 'wuerfel'})
    assert ziel is not None
    assert ziel.aruco_id == 7
    assert ziel.world_xyz_m == (0.20, 0.0, 0.03)
    assert ziel.extras['tag_yaw'] == pytest.approx(0.3)
    assert ziel.extras['approach_clear_m'] == pytest.approx(0.06)


def test_find_object_nothing_visible_returns_none():
    assert pb.find_object(_PCtx([]), {'object_type': 'wuerfel'}) is None


def test_find_object_out_of_reach_returns_none():
    # Far outside the reach annulus → not selectable, but calibrated → None.
    ctx = _PCtx([_det(world=(0.50, 0.0, 0.03))])
    assert pb.find_object(ctx, {'object_type': 'wuerfel'}) is None


def test_find_object_uncalibrated_raises_precise_error():
    # Visible but un-projected (world None) → the PRECISE calib message, not a
    # silent None the student would misread as "nothing there".
    ctx = _PCtx([_det(world=None)])
    with pytest.raises(WorkflowError) as exc:
        pb.find_object(ctx, {'object_type': 'wuerfel'})
    msg = str(exc.value)
    assert 'kalibriert' in msg or 'Kamera' in msg


def test_find_object_no_type_raises():
    with pytest.raises(WorkflowError):
        pb.find_object(_PCtx([_det()]), {})


def test_find_object_unreadable_orientation_skips_and_returns_none():
    # Refined yaw None → SKIP the tag (so a "Solange sichtbar" loop progresses)
    # and return None (so a standalone "finde" null-checks cleanly).
    ctx = _PCtx([_det(tag_yaw=None)])
    assert pb.find_object(ctx, {'object_type': 'wuerfel'}) is None
    assert 7 in ctx.skipped_tags


# ── object_position ──────────────────────────────────────────────────────────

def test_object_position_returns_xyz():
    pos = pb.object_position(_PCtx([]), {'ziel': _det(world=(0.1, 0.2, 0.3))})
    assert pos['x'] == pytest.approx(0.1)
    assert pos['y'] == pytest.approx(0.2)
    assert pos['z'] == pytest.approx(0.3)


def test_object_position_none_raises():
    with pytest.raises(WorkflowError):
        pb.object_position(_PCtx([]), {'ziel': None})


# ── grasp_held ───────────────────────────────────────────────────────────────

def test_grasp_held_true_when_jaws_blocked(monkeypatch):
    monkeypatch.setattr(motion, 'GRASP_SETTLE_S', 0.0)
    assert pb.grasp_held(_PCtx([], follower=[0, 0, 0, 0, 0, -0.1]), {}) is True


def test_grasp_held_false_on_empty_close(monkeypatch):
    monkeypatch.setattr(motion, 'GRASP_SETTLE_S', 0.0)
    assert pb.grasp_held(_PCtx([], follower=[0, 0, 0, 0, 0, -0.5]), {}) is False


def test_grasp_held_raises_when_readback_unavailable(monkeypatch):
    monkeypatch.setattr(motion, 'GRASP_SETTLE_S', 0.0)
    with pytest.raises(WorkflowError):
        pb.grasp_held(_PCtx([], follower=None), {})


# ── mark_done ────────────────────────────────────────────────────────────────

def test_mark_done_claims_tag():
    ctx = _PCtx([])
    pb.mark_done(ctx, {'ziel': _det(aruco_id=7)})
    assert 7 in ctx.claimed_tags


def test_mark_done_none_raises():
    with pytest.raises(WorkflowError):
        pb.mark_done(_PCtx([]), {'ziel': None})

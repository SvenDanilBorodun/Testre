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
    GraspSkip,
    WorkflowError,
    close_on_object,
    move_above,
)
from physical_ai_server.workflow.ik_solver import IKSolver
from physical_ai_server.workflow.interpreter import Interpreter
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


def test_find_object_out_of_reach_skips_and_returns_none():
    # Far outside the reach annulus → not selectable, calibrated → None AND the
    # tag is SKIPPED so a „Solange sichtbar" loop's gate drops to 0 and ends
    # cleanly instead of stalling for 3 passes (#B1).
    ctx = _PCtx([_det(world=(0.50, 0.0, 0.03), aruco_id=7)])
    assert pb.find_object(ctx, {'object_type': 'wuerfel'}) is None
    assert 7 in ctx.skipped_tags


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


# ── per-object grasp-held threshold (motion._held_threshold_rad) ─────────────
# Env-based setup throughout (no sentinel monkeypatching): compose forwards
# EDUBOTICS_GRASP_HELD_MAX_RAD with an EMPTY default (`${…:-}`), so '' is the
# shape EVERY un-tuned rig ships — it must count as UNSET or the per-object
# threshold is dead code fleet-wide (the v2.12.x scar).

def test_check_grasp_held_gentle_close_detects_miss(monkeypatch):
    # A wide-object recipe closes gently (−0.25, ABOVE the legacy global −0.35):
    # an EMPTY close stops at the commanded angle. The per-object threshold
    # (commanded + GRASP_HELD_MARGIN_RAD) reads that as a MISS — the old fixed
    # global threshold silently reported it as held.
    monkeypatch.setattr(motion, 'GRASP_SETTLE_S', 0.0)
    monkeypatch.setenv('EDUBOTICS_GRASP_HELD_MAX_RAD', '')
    ctx = _PCtx([], follower=[0, 0, 0, 0, 0, -0.25])
    ctx.last_commanded_close_rad = -0.25
    assert motion.check_grasp_held(ctx) is False


def test_check_grasp_held_gentle_close_detects_hold(monkeypatch):
    monkeypatch.setattr(motion, 'GRASP_SETTLE_S', 0.0)
    monkeypatch.setenv('EDUBOTICS_GRASP_HELD_MAX_RAD', '')
    ctx = _PCtx([], follower=[0, 0, 0, 0, 0, -0.05])
    ctx.last_commanded_close_rad = -0.25
    assert motion.check_grasp_held(ctx) is True


def test_held_threshold_empty_env_derives_per_object(monkeypatch):
    # THE compose-shipped shape: env present but EMPTY → unset → a −0.25
    # commanded close derives −0.25 + 0.15 = −0.10. Under the old
    # `is not None` sentinel this returned the global −0.35 (dead code).
    monkeypatch.setenv('EDUBOTICS_GRASP_HELD_MAX_RAD', '')
    ctx = _PCtx([])
    ctx.last_commanded_close_rad = -0.25
    assert motion._held_threshold_rad(ctx) == pytest.approx(-0.10)


def test_held_threshold_whitespace_env_counts_as_unset(monkeypatch):
    monkeypatch.setenv('EDUBOTICS_GRASP_HELD_MAX_RAD', '   ')
    ctx = _PCtx([])
    ctx.last_commanded_close_rad = -0.25
    assert motion._held_threshold_rad(ctx) == pytest.approx(-0.10)


def test_held_threshold_for_cube_matches_rig_validated_global(monkeypatch):
    # Shipped cube (full close −0.5): derived threshold −0.5 + 0.15 = −0.35,
    # byte-identical to the previously rig-validated global default — the
    # per-object change must not move the validated cube behaviour.
    monkeypatch.delenv('EDUBOTICS_GRASP_HELD_MAX_RAD', raising=False)
    ctx = _PCtx([])
    ctx.last_commanded_close_rad = -0.5
    assert motion._held_threshold_rad(ctx) == pytest.approx(-0.35)


def test_held_threshold_env_override_wins(monkeypatch):
    # EDUBOTICS_GRASP_HELD_MAX_RAD set to a NUMBER → the fixed global threshold
    # applies everywhere (operator rollback), even after a gentle close.
    monkeypatch.setenv('EDUBOTICS_GRASP_HELD_MAX_RAD', '-0.35')
    ctx = _PCtx([])
    ctx.last_commanded_close_rad = -0.25
    assert motion._held_threshold_rad(ctx) == pytest.approx(motion.GRASP_HELD_MAX_RAD)
    assert motion._held_threshold_rad(ctx) == pytest.approx(-0.35)


def test_held_threshold_no_close_commanded_uses_global(monkeypatch):
    # No close commanded yet this run (last_commanded_close_rad absent/None) →
    # legacy global threshold, preserving the documented open-gripper behaviour
    # of grasp_held / wait_until_held on a fresh run.
    monkeypatch.delenv('EDUBOTICS_GRASP_HELD_MAX_RAD', raising=False)
    ctx = _PCtx([])  # _PCtx never sets last_commanded_close_rad
    assert motion._held_threshold_rad(ctx) == pytest.approx(motion.GRASP_HELD_MAX_RAD)


def test_held_threshold_ignores_boot_seeded_measured_gripper(monkeypatch):
    # Workflow start boot-seeds last_full_joints from the MEASURED follower
    # pose — a still-held gripper (~−0.1 measured) must NOT masquerade as a
    # commanded close. Only the dedicated last_commanded_close_rad (written by
    # motion's close paths) may derive the per-object threshold.
    monkeypatch.setenv('EDUBOTICS_GRASP_HELD_MAX_RAD', '')
    ctx = _PCtx([])
    ctx.last_full_joints = list(HOME_JOINTS_RAD) + [-0.1]  # measured, NOT commanded
    assert motion._held_threshold_rad(ctx) == pytest.approx(motion.GRASP_HELD_MAX_RAD)


# ── mark_done ────────────────────────────────────────────────────────────────

def test_mark_done_claims_tag():
    ctx = _PCtx([])
    pb.mark_done(ctx, {'ziel': _det(aruco_id=7)})
    assert 7 in ctx.claimed_tags


def test_mark_done_none_raises():
    with pytest.raises(WorkflowError):
        pb.mark_done(_PCtx([]), {'ziel': None})


# ── #HIGH-3 / #MED-4: split blocks raise GraspSkip (loop-graceful) on None ────
def test_split_blocks_raise_graspskip_on_none():
    """move_above / close_on_object / mark_done raise GraspSkip (NOT a bare
    WorkflowError) on a missing Greifziel, so an unguarded „Solange sichtbar" loop
    body moves on instead of ABORTING the whole run; standalone still fails loud
    (GraspSkip IS a WorkflowError)."""
    ctx = _PCtx([])
    with pytest.raises(GraspSkip):
        move_above(ctx, {'ziel': None})
    with pytest.raises(GraspSkip):
        close_on_object(ctx, {'ziel': None})
    with pytest.raises(GraspSkip):
        pb.mark_done(ctx, {'ziel': None})


# ── #BUG-4: the ZIEL→ziel dispatch contract (unit tests bypass _build_args) ───
def test_split_block_ziel_input_lowercased_for_handler():
    """The React input NAME 'ZIEL' must reach the handler as args['ziel'] (the
    server↔React contract). _build_args lowercases it; this locks the contract
    the direct-call unit tests bypass."""
    interp = Interpreter([])
    ctx = _PCtx([])
    det = _det()
    ctx.variables = {'Ziel': det}
    block = {
        'type': 'edubotics_move_above',
        'inputs': {'ZIEL': {'block': {
            'type': 'variables_get',
            'fields': {'VAR': {'name': 'Ziel'}},
        }}},
    }
    args = interp._build_args(block, ctx)
    assert 'ziel' in args and args['ziel'] is det

"""CalibrationManager state-machine + persistence smoke tests.

Synthetic ChArUco rendering would require a real `cv2` build with the
contrib aruco module; instead these tests validate the state machine
and YAML round-trip using the manager's public API.

WS4 (2026-06-17): calibration is SCENE-camera-only. The gripper steps
were removed; the scene extrinsic is a single-shot board-on-table solve."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import numpy as np
import pytest


@pytest.fixture
def calib_dir(monkeypatch, tmp_path):
    monkeypatch.setenv('EDUBOTICS_CALIB_DIR', str(tmp_path))
    # Force the module-level constant to pick up the env override.
    from importlib import reload
    from physical_ai_server.workflow import calibration_manager as cm
    reload(cm)
    return tmp_path


def _write_intrinsics(calib_dir, camera):
    import cv2
    K = np.array([[600.0, 0, 320], [0, 600, 240], [0, 0, 1]], dtype=np.float64)
    dist = np.zeros((5, 1), dtype=np.float64)
    fs = cv2.FileStorage(str(calib_dir / f'{camera}_intrinsics.yaml'), cv2.FILE_STORAGE_WRITE)
    fs.write('camera_matrix', K)
    fs.write('distortion_coefficients', dist)
    fs.write('image_width', 640)
    fs.write('image_height', 480)
    fs.release()


def test_start_intrinsic_step_registers_buffer(calib_dir):
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    mgr = CalibrationManager()
    ok, msg = mgr.start_step('scene', 'intrinsic')
    assert ok is True
    assert 'scene' in msg.lower() or 'scene' in msg


def test_gripper_steps_rejected(calib_dir):
    """WS4: the gripper camera is no longer calibrated. Any gripper step is
    rejected with a German pointer to the scene camera."""
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    mgr = CalibrationManager()
    for step in ('intrinsic', 'extrinsic', 'handeye'):
        ok, msg = mgr.start_step('gripper', step)
        assert ok is False, f'gripper {step} should be rejected'
        assert 'Greifer-Kamera' in msg or 'Szenen-Kamera' in msg


def test_start_extrinsic_blocked_without_intrinsics(calib_dir, monkeypatch):
    # Disable the factory default so this exercises the no-intrinsics block.
    monkeypatch.setenv('EDUBOTICS_FACTORY_INTRINSICS', '0')
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    mgr = CalibrationManager()
    ok, msg = mgr.start_step('scene', 'extrinsic')
    assert ok is False
    assert 'intrinsisch' in msg.lower()


def test_factory_intrinsics_seeded_by_default(calib_dir):
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    mgr = CalibrationManager()
    assert mgr.has_intrinsics('scene') is True
    # Persisted so the runtime YAML loader (_load_workflow_calibration) sees it.
    assert (calib_dir / 'scene_intrinsics.yaml').exists()


def test_factory_intrinsics_can_be_disabled(calib_dir, monkeypatch):
    monkeypatch.setenv('EDUBOTICS_FACTORY_INTRINSICS', '0')
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    mgr = CalibrationManager()
    assert mgr.has_intrinsics('scene') is False


def test_real_intrinsics_supersede_factory(calib_dir):
    import cv2
    _write_intrinsics(calib_dir, 'scene')  # a real per-rig YAML already exists
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    CalibrationManager()
    # The factory seed must NOT overwrite a real calibration.
    fs = cv2.FileStorage(str(calib_dir / 'scene_intrinsics.yaml'), cv2.FILE_STORAGE_READ)
    src = fs.getNode('source')
    is_factory = (not src.empty()) and src.string() == 'factory_default'
    fs.release()
    assert is_factory is False


def test_handeye_synonym_accepted_for_scene(calib_dir):
    """A legacy frontend that still sends 'handeye' must keep working — it is
    an accepted synonym for the scene 'extrinsic' step."""
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    _write_intrinsics(calib_dir, 'scene')
    mgr = CalibrationManager()
    ok, _ = mgr.start_step('scene', 'handeye')
    assert ok is True
    assert 'scene' in mgr._handeye_buffers


def test_start_color_profile_no_longer_a_step(calib_dir):
    """The color_profile path has no per-step start_step entry — capture
    is gated by the /calibration/capture_color callback's prerequisite
    check. start_step('color_profile') is now an unknown-step error."""
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    mgr = CalibrationManager()
    ok, msg = mgr.start_step('scene', 'color_profile')
    assert ok is False
    assert 'Unbekannter' in msg


def test_unknown_step_rejected(calib_dir):
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    mgr = CalibrationManager()
    ok, msg = mgr.start_step('scene', 'made-up-step')
    assert ok is False
    assert 'Unbekannter' in msg


def test_solve_without_capture_warns(calib_dir):
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    mgr = CalibrationManager()
    mgr.start_step('scene', 'intrinsic')
    ok, reproj, disagreement, msg = mgr.solve('scene', 'intrinsic')
    assert ok is False
    assert 'fehlen' in msg.lower() or 'Bilder' in msg


def test_capture_without_start_warns(calib_dir):
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    mgr = CalibrationManager()
    fake_frame = np.zeros((480, 640, 3), dtype=np.uint8)
    ok, _, _, _, msg = mgr.capture_frame('scene', bgr=fake_frame)
    assert ok is False
    assert 'aktiv' in msg.lower() or 'starten' in msg.lower()


def test_start_extrinsic_pops_intrinsic_buffer(calib_dir):
    """Regression: start_step('extrinsic', scene) must drop any leftover
    intrinsic capture buffer for that camera. Otherwise capture_frame's
    "if camera in _intrinsic_buffers" precedence routes extrinsic captures
    into the intrinsic buffer and the student never collects a single
    extrinsic sample."""
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    _write_intrinsics(calib_dir, 'scene')

    mgr = CalibrationManager()
    mgr.start_step('scene', 'intrinsic')
    assert 'scene' in mgr._intrinsic_buffers

    ok, _ = mgr.start_step('scene', 'extrinsic')
    assert ok is True
    assert 'scene' not in mgr._intrinsic_buffers, (
        'start_step(extrinsic) must pop the leftover intrinsic buffer'
    )
    assert 'scene' in mgr._handeye_buffers


def test_start_intrinsic_pops_extrinsic_buffer(calib_dir):
    """Symmetric regression: starting intrinsic again drops the extrinsic
    buffer so a re-do of intrinsic doesn't keep stale extrinsic state."""
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    _write_intrinsics(calib_dir, 'scene')

    mgr = CalibrationManager()
    mgr.start_step('scene', 'extrinsic')
    assert 'scene' in mgr._handeye_buffers

    mgr.start_step('scene', 'intrinsic')
    assert 'scene' not in mgr._handeye_buffers
    assert 'scene' in mgr._intrinsic_buffers


def test_cancel_step_clears_buffers(calib_dir):
    """cancel_step() drops every in-flight capture buffer. Persists fake
    intrinsics so the extrinsic start gate passes, then loads up
    scene-intrinsic + scene-extrinsic and asserts cancel sweeps both."""
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    _write_intrinsics(calib_dir, 'scene')
    mgr = CalibrationManager()
    mgr.start_step('scene', 'intrinsic')
    # Re-open scene as extrinsic to get a populated _handeye_buffers entry.
    mgr.start_step('scene', 'extrinsic')
    assert 'scene' in mgr._handeye_buffers, 'precondition: extrinsic buffer populated'
    ok, _ = mgr.cancel_step()
    assert ok is True
    assert mgr._intrinsic_buffers == {}
    assert mgr._handeye_buffers == {}


def test_cancel_step_per_camera_keeps_other(calib_dir):
    """Camera-specific cancel only drops that camera's buffers. (Only the
    scene camera is calibrated now, but cancel must still narrow correctly.)"""
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    _write_intrinsics(calib_dir, 'scene')
    mgr = CalibrationManager()
    mgr.start_step('scene', 'intrinsic')
    # A different in-flight camera key (defensive — cancel must not sweep it).
    mgr._intrinsic_buffers['other'] = mgr._intrinsic_buffers['scene'].__class__()
    ok, _ = mgr.cancel_step('scene')
    assert ok is True
    assert 'scene' not in mgr._intrinsic_buffers
    assert 'other' in mgr._intrinsic_buffers


def test_persisted_intrinsics_load_on_construction(calib_dir):
    """Write a fake YAML and verify that constructing a fresh
    CalibrationManager picks it up."""
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    _write_intrinsics(calib_dir, 'scene')

    mgr = CalibrationManager()
    assert mgr.has_intrinsics('scene') is True
    assert mgr.has_intrinsics('gripper') is False


# ── Board-yaw config + plane fit + touch-off (P3) ────────────────────────────

def _write_extrinsic(calib_dir, board_table_z=0.0):
    """Write a minimal scene extrinsic YAML (camera 1 m above table). The
    supplied board surface height is ``board_table_z`` (NOT z_table — z_table is
    written only by the touch-off)."""
    import cv2
    T = np.eye(4, dtype=np.float64)
    T[2, 3] = 1.0
    fs = cv2.FileStorage(str(calib_dir / 'scene_handeye.yaml'), cv2.FILE_STORAGE_WRITE)
    fs.write('transform', T)
    fs.write('method', 'BOARD_ON_TABLE')
    fs.write('board_table_z', float(board_table_z))
    fs.write('board_origin_x', 0.18)
    fs.write('board_origin_y', -0.075)
    fs.release()


# A clearly non-collinear triangle of touch points (passes the minor-axis gate).
_TRIANGLE_XY = [(0.12, -0.08), (0.24, -0.06), (0.16, 0.09)]

# The FK rotation of a strict-vertical (straight-down) grasp: the EE-link local
# +x (approach axis, = R[:, 0]) points to base −z. This is what ik.fk() returns
# for any reachable top-down target, so it is what a correctly-held touch-off
# tap looks like and must PASS the verticality gate.
_VERTICAL_R = np.array([[0.0, 0.0, 1.0],
                        [0.0, 1.0, 0.0],
                        [-1.0, 0.0, 0.0]], dtype=np.float64)


def _tilt_R(deg, axis='y'):
    """Rotate the vertical-grasp R by ``deg`` about a base axis — simulates a
    student tapping with the gripper tilted off vertical."""
    th = np.radians(deg)
    c, s = np.cos(th), np.sin(th)
    if axis == 'y':
        Rb = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=np.float64)
    else:  # x
        Rb = np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=np.float64)
    return Rb @ _VERTICAL_R


class _PoseSeq:
    """Fake get_gripper_pose returning a scripted sequence of (R, t).

    ``ts`` is the (x, y, z) sequence. ``R`` defaults to a strict-vertical grasp
    rotation (passes the verticality gate); pass a single matrix or a per-point
    list to simulate a tilted gripper."""

    def __init__(self, ts, R=None):
        self._ts = [np.asarray(t, dtype=np.float64) for t in ts]
        if R is None:
            self._Rs = [_VERTICAL_R] * len(self._ts)
        elif isinstance(R, list):
            self._Rs = [np.asarray(r, dtype=np.float64) for r in R]
        else:
            self._Rs = [np.asarray(R, dtype=np.float64)] * len(self._ts)
        self._i = 0

    def __call__(self):
        if self._i >= len(self._ts):
            return None
        t = self._ts[self._i]
        R = self._Rs[self._i]
        self._i += 1
        return R, t


def test_board_default_orientation_flips_z(calib_dir):
    # Rig-found 2026-06-18: OpenCV's ChArUco board +Z exits the BACK, so a
    # printed-side-up board needs an Rx(180°) flip (NOT identity) or the scene
    # camera resolves BELOW the table. The default rotation must therefore be a
    # PROPER rotation that sends board +Z -> base -Z (down) while keeping
    # board +X -> base +X (board is physically in front of the robot).
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    R = CalibrationManager._board_to_base_transform()[:3, :3]
    assert abs(np.linalg.det(R) - 1.0) < 1e-9          # proper rotation
    np.testing.assert_allclose(R @ np.array([0., 0., 1.]), [0, 0, -1], atol=1e-9)
    np.testing.assert_allclose(R @ np.array([1., 0., 0.]), [1, 0, 0], atol=1e-9)
    np.testing.assert_allclose(R @ np.array([0., 1., 0.]), [0, -1, 0], atol=1e-9)


def test_board_yaw_applies_rotation(calib_dir, monkeypatch):
    monkeypatch.setenv('EDUBOTICS_BOARD_YAW_DEG', '90')
    from importlib import reload
    from physical_ai_server.workflow import calibration_manager as cm
    reload(cm)
    T = cm.CalibrationManager._board_to_base_transform()
    # +90° about z: board +X → base +Y.
    np.testing.assert_allclose(T[:3, :3] @ np.array([1.0, 0, 0]), [0, 1, 0], atol=1e-9)


def test_fit_plane_flat(calib_dir):
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    pts = np.array([[0.1, 0.0, 0.05], [0.2, 0.1, 0.05],
                    [0.15, -0.1, 0.05], [0.25, 0.05, 0.05]])
    (a, b, c), rms = CalibrationManager._fit_plane(pts)
    assert abs(a) < 1e-9 and abs(b) < 1e-9 and abs(c - 0.05) < 1e-9
    assert rms < 1e-9


def test_fit_plane_tilted(calib_dir):
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    pts = np.array([[x, y, 0.1 * x + 0.02]
                    for x, y in [(0.1, 0), (0.2, 0.1), (0.15, -0.1), (0.25, 0.05)]])
    (a, b, c), rms = CalibrationManager._fit_plane(pts)
    assert abs(a - 0.1) < 1e-9 and abs(b) < 1e-9 and abs(c - 0.02) < 1e-9
    assert rms < 1e-9


def test_touch_off_requires_extrinsic(calib_dir):
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    mgr = CalibrationManager()
    ok, msg = mgr.start_table_touch()
    assert ok is False
    assert 'Extrinsik' in msg or 'ausrichten' in msg


def test_touch_off_full_flow_persists_measured_plane(calib_dir):
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    import cv2
    _write_extrinsic(calib_dir, board_table_z=0.0)
    poses = _PoseSeq([[x, y, 0.03] for (x, y) in _TRIANGLE_XY])
    mgr = CalibrationManager(get_gripper_pose=poses)
    ok, _ = mgr.start_table_touch()
    assert ok
    for _ in range(3):
        ok, _n, _req, _ = mgr.capture_touch_point()
        assert ok
    assert mgr.has_table_plane('scene') is False  # not solved yet
    ok, rms, msg = mgr.solve_table_plane('scene')
    assert ok, msg
    assert rms < 1e-6
    assert mgr.has_table_plane('scene') is True  # plane now persisted
    fs = cv2.FileStorage(str(calib_dir / 'scene_handeye.yaml'), cv2.FILE_STORAGE_READ)
    z = float(fs.getNode('z_table').real())
    plane = fs.getNode('table_plane').mat()
    transform = fs.getNode('transform').mat()
    fs.release()
    assert abs(z - 0.03) < 1e-6           # measured table height (EE frame)
    assert plane is not None and plane.size == 3
    assert transform is not None          # original extrinsic preserved


def test_touch_off_rejects_clustered_points(calib_dir):
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    _write_extrinsic(calib_dir)
    poses = _PoseSeq([[0.18, 0.0, 0.03], [0.182, 0.001, 0.03], [0.181, -0.001, 0.03]])
    mgr = CalibrationManager(get_gripper_pose=poses)
    mgr.start_table_touch()
    for _ in range(3):
        mgr.capture_touch_point()
    ok, _rms, msg = mgr.solve_table_plane('scene')
    assert ok is False
    assert 'nah' in msg or 'verteilt' in msg


def test_touch_off_rejects_collinear_points(calib_dir):
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    _write_extrinsic(calib_dir)
    # Good radial spread but on a straight line (y const) → zero tilt info.
    poses = _PoseSeq([[0.12, 0.0, 0.03], [0.18, 0.0, 0.03], [0.26, 0.0, 0.03]])
    mgr = CalibrationManager(get_gripper_pose=poses)
    mgr.start_table_touch()
    for _ in range(3):
        mgr.capture_touch_point()
    ok, _rms, msg = mgr.solve_table_plane('scene')
    assert ok is False
    assert 'Linie' in msg or 'verteilt' in msg


def test_touch_off_cross_check_rejects_gross_mismatch(calib_dir):
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    _write_extrinsic(calib_dir, board_table_z=0.0)
    # Touch plane at z=0.5 m vs the board at 0.0 → |Δ| 0.5 > 0.12 → reject.
    poses = _PoseSeq([[x, y, 0.5] for (x, y) in _TRIANGLE_XY])
    mgr = CalibrationManager(get_gripper_pose=poses)
    mgr.start_table_touch()
    for _ in range(3):
        mgr.capture_touch_point()
    ok, _rms, msg = mgr.solve_table_plane('scene')
    assert ok is False
    assert 'Kamerakalibrierung' in msg or 'Tischhöhe' in msg


def test_approach_tilt_deg_vertical_is_zero(calib_dir):
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    assert CalibrationManager._approach_tilt_deg(_VERTICAL_R) == pytest.approx(0.0, abs=1e-6)


def test_approach_tilt_deg_matches_known_tilt(calib_dir):
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    for deg in (5.0, 15.0, 30.0):
        for ax in ('y', 'x'):
            got = CalibrationManager._approach_tilt_deg(_tilt_R(deg, ax))
            assert got == pytest.approx(deg, abs=1e-6)


def test_approach_tilt_deg_handles_bad_rotation(calib_dir):
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    # Zero matrix → degenerate approach axis → None (not a crash).
    assert CalibrationManager._approach_tilt_deg(np.zeros((3, 3))) is None


def test_touch_off_accepts_vertical_tap(calib_dir):
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    _write_extrinsic(calib_dir, board_table_z=0.0)
    poses = _PoseSeq([[x, y, 0.03] for (x, y) in _TRIANGLE_XY])  # R defaults vertical
    mgr = CalibrationManager(get_gripper_pose=poses)
    mgr.start_table_touch()
    for _ in range(3):
        ok, _n, _req, msg = mgr.capture_touch_point()
        assert ok, msg


def test_touch_off_rejects_tilted_tap(calib_dir):
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    _write_extrinsic(calib_dir, board_table_z=0.0)
    # Gripper tilted 25° off vertical (> TABLE_TOUCH_MAX_TILT_DEG) → rejected
    # AT CAPTURE, before any point is recorded.
    poses = _PoseSeq([[x, y, 0.03] for (x, y) in _TRIANGLE_XY],
                     R=_tilt_R(25.0))
    mgr = CalibrationManager(get_gripper_pose=poses)
    mgr.start_table_touch()
    ok, count, _req, msg = mgr.capture_touch_point()
    assert ok is False
    assert count == 0
    assert 'schräg' in msg or 'senkrecht' in msg


def test_touch_off_just_inside_tilt_tolerance_accepted(calib_dir):
    from physical_ai_server.workflow.calibration_manager import (
        CalibrationManager, TABLE_TOUCH_MAX_TILT_DEG)
    _write_extrinsic(calib_dir, board_table_z=0.0)
    poses = _PoseSeq([[x, y, 0.03] for (x, y) in _TRIANGLE_XY],
                     R=_tilt_R(TABLE_TOUCH_MAX_TILT_DEG - 1.0))
    mgr = CalibrationManager(get_gripper_pose=poses)
    mgr.start_table_touch()
    ok, _count, _req, msg = mgr.capture_touch_point()
    assert ok, msg


def test_safe_float_env_falls_back_on_garbage(monkeypatch):
    # A malformed EDUBOTICS_* override (operator typo) must degrade to the
    # default, NEVER raise at import / manager construction.
    from physical_ai_server.workflow.calibration_manager import _safe_float_env
    monkeypatch.setenv('EDUBOTICS_BOARD_YAW_DEG', 'ninety')
    assert _safe_float_env('EDUBOTICS_BOARD_YAW_DEG', 0.0) == 0.0
    monkeypatch.setenv('EDUBOTICS_BOARD_YAW_DEG', '90')
    assert _safe_float_env('EDUBOTICS_BOARD_YAW_DEG', 0.0) == 90.0
    monkeypatch.delenv('EDUBOTICS_BOARD_YAW_DEG', raising=False)
    assert _safe_float_env('EDUBOTICS_BOARD_YAW_DEG', 1.5) == 1.5


def test_safe_int_env_falls_back_on_garbage(monkeypatch):
    from physical_ai_server.workflow.calibration_manager import _safe_int_env
    monkeypatch.setenv('EDUBOTICS_FACTORY_IMG_W', '64x')
    assert _safe_int_env('EDUBOTICS_FACTORY_IMG_W', 640) == 640
    monkeypatch.setenv('EDUBOTICS_FACTORY_IMG_W', '800')
    assert _safe_int_env('EDUBOTICS_FACTORY_IMG_W', 640) == 800

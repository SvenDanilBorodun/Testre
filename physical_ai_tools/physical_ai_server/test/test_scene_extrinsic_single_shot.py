"""WS4 (2026-06-17) — single-shot board-on-table scene extrinsic.

The scene-camera extrinsic is recovered from ONE capture of a ChArUco board
lying flat on the table at a known reference position (no arm motion):

    T_board_to_cam  = [R_t2c | t_t2c]      (from solvePnP at capture)
    T_cam_to_board  = inverse(T_board_to_cam)
    T_cam_to_base   = T_board_to_base @ T_cam_to_board
    z_table         = BOARD_TABLE_Z_M

These tests inject a synthetic ``R_target2cam`` / ``t_target2cam`` directly
into the handeye buffer (the same pattern the legacy disagreement test used to
stub ``calibrateHandEye``), so they need no real ChArUco rendering. They prove
the math, the z_table write, and the camera-above-table sanity guard.
"""

from __future__ import annotations

import numpy as np
import pytest


@pytest.fixture
def calib_dir(monkeypatch, tmp_path):
    monkeypatch.setenv('EDUBOTICS_CALIB_DIR', str(tmp_path))
    from importlib import reload
    from physical_ai_server.workflow import calibration_manager as cm
    reload(cm)
    return tmp_path


def _inject_single_capture(mgr, R_t2c, t_t2c):
    """Put one solvePnP-equivalent sample into the scene extrinsic buffer."""
    mgr._intrinsics['scene'] = {
        'K': np.eye(3, dtype=np.float64),
        'dist': np.zeros((5, 1), dtype=np.float64),
    }
    buf = type(
        'Buf', (), {
            'R_target2cam': [np.asarray(R_t2c, dtype=np.float64)],
            't_target2cam': [np.asarray(t_t2c, dtype=np.float64).reshape(3, 1)],
            'R_gripper2base': [],
            't_gripper2base': [],
        },
    )()
    mgr._handeye_buffers['scene'] = buf


def _camera_looking_straight_down(height_m):
    """A board->cam transform for a camera mounted ``height_m`` above the board,
    looking straight down. With the camera +Z pointing down at the board, the
    board (z=0 plane) is at distance ``height_m`` along the camera's +Z, and the
    camera-frame X/Y mirror the board frame. T_board_to_cam columns:
        cam_x =  board_x,  cam_y = -board_y,  cam_z = -board_z
    so the board origin sits at (0, 0, height) in the camera frame.
    """
    R = np.array([
        [1.0, 0.0, 0.0],
        [0.0, -1.0, 0.0],
        [0.0, 0.0, -1.0],
    ], dtype=np.float64)
    t = np.array([0.0, 0.0, float(height_m)], dtype=np.float64)
    return R, t


def test_single_shot_solves_and_writes_z_table(calib_dir):
    from physical_ai_server.workflow.calibration_manager import (
        CalibrationManager,
        BOARD_ORIGIN_X_M,
        BOARD_ORIGIN_Y_M,
        BOARD_TABLE_Z_M,
    )
    import cv2

    mgr = CalibrationManager()
    R_t2c, t_t2c = _camera_looking_straight_down(0.40)
    _inject_single_capture(mgr, R_t2c, t_t2c)

    ok, reproj, disagreement, msg = mgr.solve('scene', 'extrinsic')
    assert ok is True, msg
    assert disagreement == pytest.approx(0.0)
    assert mgr.has_extrinsic('scene') is True

    # Read back the persisted transform + z_table.
    fs = cv2.FileStorage(str(mgr._handeye_path('scene')), cv2.FILE_STORAGE_READ)
    T = fs.getNode('transform').mat()
    z_table = float(fs.getNode('z_table').real())
    method = fs.getNode('method').string()
    fs.release()

    assert method == 'BOARD_ON_TABLE'
    assert z_table == pytest.approx(BOARD_TABLE_Z_M)

    # The camera origin in base frame should sit the board origin offset plus
    # the 0.40 m height above the table. With a straight-down camera 0.40 m
    # above the board origin, T_cam_to_base translation = board_origin + [0,0,h].
    assert T[0, 3] == pytest.approx(BOARD_ORIGIN_X_M, abs=1e-6)
    assert T[1, 3] == pytest.approx(BOARD_ORIGIN_Y_M, abs=1e-6)
    assert T[2, 3] == pytest.approx(BOARD_TABLE_Z_M + 0.40, abs=1e-6)


def test_single_shot_projection_round_trip(calib_dir):
    """End-to-end: solve the extrinsic, then project the board origin pixel
    back to the table and confirm it lands at the known board origin in base
    frame (the pick-and-place path)."""
    from physical_ai_server.workflow.calibration_manager import (
        CalibrationManager,
        BOARD_ORIGIN_X_M,
        BOARD_ORIGIN_Y_M,
        BOARD_TABLE_Z_M,
    )
    from physical_ai_server.workflow.projection import (
        project_base_to_pixel,
        project_pixel_to_table,
    )

    mgr = CalibrationManager()
    # Realistic intrinsics for the round trip (override the identity stub).
    K = np.array([[600.0, 0.0, 320.0], [0.0, 600.0, 240.0], [0.0, 0.0, 1.0]])
    dist = np.zeros((5, 1))
    R_t2c, t_t2c = _camera_looking_straight_down(0.40)
    _inject_single_capture(mgr, R_t2c, t_t2c)
    mgr._intrinsics['scene'] = {'K': K, 'dist': dist}

    ok, _, _, msg = mgr.solve('scene', 'extrinsic')
    assert ok is True, msg

    import cv2
    fs = cv2.FileStorage(str(mgr._handeye_path('scene')), cv2.FILE_STORAGE_READ)
    T_cam_to_base = fs.getNode('transform').mat()
    z_table = float(fs.getNode('z_table').real())
    fs.release()

    board_origin_base = np.array([BOARD_ORIGIN_X_M, BOARD_ORIGIN_Y_M, BOARD_TABLE_Z_M])
    px = project_base_to_pixel(board_origin_base, K, dist, T_cam_to_base)
    assert px is not None
    back = project_pixel_to_table(px[0], px[1], K, dist, T_cam_to_base, z_table)
    assert back is not None
    assert back[0] == pytest.approx(BOARD_ORIGIN_X_M, abs=1e-3)
    assert back[1] == pytest.approx(BOARD_ORIGIN_Y_M, abs=1e-3)
    assert back[2] == pytest.approx(BOARD_TABLE_Z_M, abs=1e-6)


def test_camera_below_table_rejected(calib_dir):
    """If the recovered camera origin is at/below the table plane (board laid
    upside-down or wrong placement), the solve refuses + drops the buffer."""
    from physical_ai_server.workflow.calibration_manager import CalibrationManager

    mgr = CalibrationManager()
    # Camera at the table plane (height 0) → cam origin z == z_table → reject.
    R_t2c, t_t2c = _camera_looking_straight_down(0.0)
    _inject_single_capture(mgr, R_t2c, t_t2c)

    ok, _, _, msg = mgr.solve('scene', 'extrinsic')
    assert ok is False
    assert 'über dem Tisch' in msg or 'oben' in msg
    assert mgr.has_extrinsic('scene') is False
    assert 'scene' not in mgr._handeye_buffers


def test_solve_without_capture_reports_missing_frame(calib_dir):
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    mgr = CalibrationManager()
    mgr._intrinsics['scene'] = {
        'K': np.eye(3, dtype=np.float64),
        'dist': np.zeros((5, 1), dtype=np.float64),
    }
    ok, _, _, msg = mgr.solve('scene', 'extrinsic')
    assert ok is False
    assert 'Bild' in msg or 'Tafel' in msg


def test_gripper_extrinsic_rejected_at_solve(calib_dir):
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    mgr = CalibrationManager()
    ok, _, _, msg = mgr.solve('gripper', 'extrinsic')
    assert ok is False
    assert 'Szenen-Kamera' in msg

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
    looking straight down — modelling OpenCV's REAL ChArUco convention where the
    board +Z exits the BACK of the board. With the board printed-side-up on the
    table, board +Z therefore points DOWN, the same way the straight-down
    camera's +Z points, so T_board_to_cam rotation is IDENTITY and the board
    origin sits at (0, 0, height) in the camera frame.

    This reproduces the rig-found bug exactly: with the OLD identity
    T_board_to_base this resolves the camera BELOW the table (z = -height); the
    Rx(180°) flip in T_board_to_base corrects it to ABOVE (z = +height).
    """
    R = np.eye(3, dtype=np.float64)
    t = np.array([0.0, 0.0, float(height_m)], dtype=np.float64)
    return R, t


def _board_on_table_capture(yaw_misplace_deg):
    """Synthesize the solvePnP output (``R_target2cam``, ``t_target2cam`` =
    ``T_board_to_cam``) for a board lying flat on the table, rotated
    ``yaw_misplace_deg`` in-plane from the canonical placement, as seen by a
    REALISTIC top-down scene camera mounted above the workspace centre with
    image-"up" facing away from the robot.

    yaw=0 is the correct placement; 90 / 180 / -90 are the gross misplacements
    the orientation gate must reject.
    """
    import math

    def _rz(deg):
        c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
        return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])

    from physical_ai_server.workflow.calibration_manager import (
        BOARD_ORIGIN_X_M, BOARD_ORIGIN_Y_M, BOARD_TABLE_Z_M,
    )
    # T_board_to_base for the misplaced board (Rz(yaw) @ Rx(180°) flip).
    r_flip = np.diag([1.0, -1.0, -1.0])
    T_board_to_base = np.eye(4)
    T_board_to_base[:3, :3] = _rz(yaw_misplace_deg) @ r_flip
    T_board_to_base[:3, 3] = [BOARD_ORIGIN_X_M, BOARD_ORIGIN_Y_M, BOARD_TABLE_Z_M]

    # Physical camera truth: top-down above the workspace centre, image-x along
    # base +X, image-y along base −Y (optical +Z = base −Z, looking down).
    R_cam_to_base = np.array([[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, -1.0]])
    T_cam_to_base = np.eye(4)
    T_cam_to_base[:3, :3] = R_cam_to_base
    T_cam_to_base[:3, 3] = [0.28, 0.0, 0.40]
    T_base_to_cam = np.linalg.inv(T_cam_to_base)

    # What the camera actually sees of the (mis)placed board.
    T_board_to_cam = T_base_to_cam @ T_board_to_base
    return T_board_to_cam[:3, :3].copy(), T_board_to_cam[:3, 3].copy()


def test_correctly_placed_board_passes_orientation_gate(calib_dir):
    """A canonically-placed board (long edge forward, origin near-left) passes
    the orientation sanity gate and the extrinsic is saved."""
    from physical_ai_server.workflow.calibration_manager import CalibrationManager

    mgr = CalibrationManager()
    R_t2c, t_t2c = _board_on_table_capture(0.0)
    _inject_single_capture(mgr, R_t2c, t_t2c)

    ok, _, _, msg = mgr.solve('scene', 'extrinsic')
    assert ok is True, msg
    assert mgr.has_extrinsic('scene') is True


def test_board_rotated_90_NOT_caught_is_a_known_limitation(calib_dir):
    """A 90°/-90° in-plane board rotation is NOT caught by the single-shot gate
    and must NOT be (rig-found 2026-06-18): a scene camera may be mounted at ANY
    roll, so a 90° board yaw is geometrically indistinguishable from a 90° camera
    roll from one top-down view. The earlier forward-axis (cam +X) check that
    "caught" 90° here ALSO false-rejected a correctly-placed board under a
    180°-rolled real camera — so it was removed. The residual 90°/mirror is
    resolved by the rig ground-truth check (object at a measured (x,y)), not by
    this gate. This test pins that the gate does NOT false-reject these (the gate
    must stay roll-independent)."""
    from physical_ai_server.workflow.calibration_manager import CalibrationManager

    for yaw in (90.0, -90.0):
        mgr = CalibrationManager()
        R_t2c, t_t2c = _board_on_table_capture(yaw)
        _inject_single_capture(mgr, R_t2c, t_t2c)
        ok, _, _, _ = mgr.solve('scene', 'extrinsic')
        # The look-point stays inside the workspace band for a 90° yaw about the
        # board origin, so the roll-independent gate accepts it.
        assert ok is True, f'90° gate must stay roll-independent (yaw={yaw})'


def test_board_rotated_180_rejected(calib_dir):
    """A board rotated 180° in-plane IS rejected: the camera's look-point walks
    out of the forward workspace region (roll-independent — this is the gross
    misplacement the gate reliably catches)."""
    from physical_ai_server.workflow.calibration_manager import CalibrationManager

    mgr = CalibrationManager()
    R_t2c, t_t2c = _board_on_table_capture(180.0)
    _inject_single_capture(mgr, R_t2c, t_t2c)

    ok, _, _, msg = mgr.solve('scene', 'extrinsic')
    assert ok is False
    assert ('verdreht' in msg or 'falsch platziert' in msg or 'nach unten' in msg)
    assert mgr.has_extrinsic('scene') is False
    assert 'scene' not in mgr._handeye_buffers


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

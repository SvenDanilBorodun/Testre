"""CalibrationManager state-machine + persistence smoke tests.

Synthetic ChArUco rendering would require a real `cv2` build with the
contrib aruco module; instead these tests validate the state machine
and YAML round-trip using the manager's public API."""

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


def test_start_intrinsic_step_registers_buffer(calib_dir):
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    mgr = CalibrationManager()
    ok, msg = mgr.start_step('gripper', 'intrinsic')
    assert ok is True
    assert 'gripper' in msg.lower() or 'gripper' in msg


def test_start_handeye_blocked_without_intrinsics(calib_dir):
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    mgr = CalibrationManager()
    ok, msg = mgr.start_step('scene', 'handeye')
    assert ok is False
    assert 'intrinsisch' in msg.lower()


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
    ok, msg = mgr.start_step('gripper', 'made-up-step')
    assert ok is False
    assert 'Unbekannter' in msg


def test_solve_without_capture_warns(calib_dir):
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    mgr = CalibrationManager()
    mgr.start_step('gripper', 'intrinsic')
    ok, reproj, disagreement, msg = mgr.solve('gripper', 'intrinsic')
    assert ok is False
    assert 'fehlen' in msg.lower() or 'Bilder' in msg


def test_capture_without_start_warns(calib_dir):
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    mgr = CalibrationManager()
    fake_frame = np.zeros((480, 640, 3), dtype=np.uint8)
    ok, _, _, _, msg = mgr.capture_frame('gripper', bgr=fake_frame)
    assert ok is False
    assert 'aktiv' in msg.lower() or 'starten' in msg.lower()


def test_start_handeye_pops_intrinsic_buffer(calib_dir):
    """Regression: start_step('handeye', camera) must drop any leftover
    intrinsic capture buffer for that camera. Otherwise capture_frame's
    "if camera in _intrinsic_buffers" precedence routes hand-eye captures
    into the intrinsic buffer and the student never collects a single
    hand-eye sample."""
    import cv2
    from physical_ai_server.workflow.calibration_manager import CalibrationManager

    # Persist intrinsics so the handeye start gate passes.
    K = np.array([[600.0, 0, 320], [0, 600, 240], [0, 0, 1]], dtype=np.float64)
    dist = np.zeros((5, 1), dtype=np.float64)
    fs = cv2.FileStorage(str(calib_dir / 'gripper_intrinsics.yaml'), cv2.FILE_STORAGE_WRITE)
    fs.write('camera_matrix', K)
    fs.write('distortion_coefficients', dist)
    fs.write('image_width', 640)
    fs.write('image_height', 480)
    fs.release()

    mgr = CalibrationManager()
    mgr.start_step('gripper', 'intrinsic')
    assert 'gripper' in mgr._intrinsic_buffers

    ok, _ = mgr.start_step('gripper', 'handeye')
    assert ok is True
    assert 'gripper' not in mgr._intrinsic_buffers, (
        'start_step(handeye) must pop the leftover intrinsic buffer'
    )
    assert 'gripper' in mgr._handeye_buffers


def test_start_intrinsic_pops_handeye_buffer(calib_dir):
    """Symmetric regression: starting intrinsic again drops the hand-eye
    buffer so a re-do of intrinsic doesn't keep stale handeye state."""
    import cv2
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    K = np.array([[600.0, 0, 320], [0, 600, 240], [0, 0, 1]], dtype=np.float64)
    dist = np.zeros((5, 1), dtype=np.float64)
    fs = cv2.FileStorage(str(calib_dir / 'gripper_intrinsics.yaml'), cv2.FILE_STORAGE_WRITE)
    fs.write('camera_matrix', K)
    fs.write('distortion_coefficients', dist)
    fs.write('image_width', 640)
    fs.write('image_height', 480)
    fs.release()

    mgr = CalibrationManager()
    mgr.start_step('gripper', 'handeye')
    assert 'gripper' in mgr._handeye_buffers

    mgr.start_step('gripper', 'intrinsic')
    assert 'gripper' not in mgr._handeye_buffers
    assert 'gripper' in mgr._intrinsic_buffers


def test_cancel_step_clears_buffers(calib_dir):
    """cancel_step() drops every in-flight capture buffer across both
    cameras AND both step types. Persists fake intrinsics so the
    handeye start gate passes, then loads up gripper-intrinsic +
    scene-intrinsic + scene-handeye and asserts cancel sweeps all
    three."""
    import cv2
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    K = np.array([[600.0, 0, 320], [0, 600, 240], [0, 0, 1]], dtype=np.float64)
    dist = np.zeros((5, 1), dtype=np.float64)
    for cam in ('gripper', 'scene'):
        fs = cv2.FileStorage(str(calib_dir / f'{cam}_intrinsics.yaml'), cv2.FILE_STORAGE_WRITE)
        fs.write('camera_matrix', K)
        fs.write('distortion_coefficients', dist)
        fs.write('image_width', 640)
        fs.write('image_height', 480)
        fs.release()
    mgr = CalibrationManager()
    mgr.start_step('gripper', 'intrinsic')
    mgr.start_step('scene', 'intrinsic')
    # Re-open scene as handeye to get a populated _handeye_buffers entry.
    mgr.start_step('scene', 'handeye')
    assert 'scene' in mgr._handeye_buffers, 'precondition: handeye buffer populated'
    ok, _ = mgr.cancel_step()
    assert ok is True
    assert mgr._intrinsic_buffers == {}
    assert mgr._handeye_buffers == {}


def test_cancel_step_per_camera_keeps_other(calib_dir):
    """Camera-specific cancel only drops that camera's buffers."""
    import cv2
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    K = np.array([[600.0, 0, 320], [0, 600, 240], [0, 0, 1]], dtype=np.float64)
    dist = np.zeros((5, 1), dtype=np.float64)
    for cam in ('gripper', 'scene'):
        fs = cv2.FileStorage(str(calib_dir / f'{cam}_intrinsics.yaml'), cv2.FILE_STORAGE_WRITE)
        fs.write('camera_matrix', K)
        fs.write('distortion_coefficients', dist)
        fs.write('image_width', 640)
        fs.write('image_height', 480)
        fs.release()
    mgr = CalibrationManager()
    mgr.start_step('gripper', 'intrinsic')
    mgr.start_step('scene', 'intrinsic')
    ok, _ = mgr.cancel_step('gripper')
    assert ok is True
    assert 'gripper' not in mgr._intrinsic_buffers
    assert 'scene' in mgr._intrinsic_buffers


def test_persisted_intrinsics_load_on_construction(calib_dir):
    """Write a fake YAML and verify that constructing a fresh
    CalibrationManager picks it up."""
    import cv2
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    K = np.array([[600.0, 0, 320], [0, 600, 240], [0, 0, 1]], dtype=np.float64)
    dist = np.zeros((5, 1), dtype=np.float64)
    fs = cv2.FileStorage(str(calib_dir / 'gripper_intrinsics.yaml'), cv2.FILE_STORAGE_WRITE)
    fs.write('camera_matrix', K)
    fs.write('distortion_coefficients', dist)
    fs.write('image_width', 640)
    fs.write('image_height', 480)
    fs.release()

    mgr = CalibrationManager()
    assert mgr.has_intrinsics('gripper') is True
    assert mgr.has_intrinsics('scene') is False

"""Audit §3.3 — Hand-Auge solve refuses to persist a transform when the
PARK and TSAI methods disagree by more than the configured threshold.

The v1 ship always wrote the YAML and only appended a German "Achtung:"
to the message. With hand-eye divergence > 2° / 5 mm, the resulting
calibration is unreliable; better to force a re-capture than to ship
a bad transform that silently corrupts every later projection.
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


def _identity_buffer(n: int):
    """A buffer of n identical poses — yields disagreement = 0."""
    R = [np.eye(3) for _ in range(n)]
    t = [np.zeros((3, 1)) for _ in range(n)]
    return R, t


def test_disagreement_under_threshold_persists(calib_dir, monkeypatch):
    from physical_ai_server.workflow.calibration_manager import (
        CalibrationManager,
        HANDEYE_FRAMES_REQUIRED,
    )
    mgr = CalibrationManager()
    mgr._intrinsics['gripper'] = {
        'K': np.eye(3, dtype=np.float64),
        'dist': np.zeros((5, 1), dtype=np.float64),
    }
    R, t = _identity_buffer(HANDEYE_FRAMES_REQUIRED)
    mgr._handeye_buffers['gripper'] = type(
        'Buf', (), {
            'R_target2cam': R, 't_target2cam': t,
            'R_gripper2base': R, 't_gripper2base': t,
        }
    )()

    # Stub cv2.calibrateHandEye so PARK and TSAI agree exactly.
    import cv2 as _cv2
    monkeypatch.setattr(
        _cv2, 'calibrateHandEye',
        lambda *a, **kw: (np.eye(3), np.zeros((3, 1))),
    )
    ok, _reproj, angle, msg = mgr._solve_handeye('gripper')
    assert ok is True, msg
    assert angle == pytest.approx(0.0, abs=1e-6)
    assert mgr.has_handeye('gripper')


def test_disagreement_over_threshold_blocks(calib_dir, monkeypatch):
    from physical_ai_server.workflow.calibration_manager import (
        CalibrationManager,
        HANDEYE_FRAMES_REQUIRED,
    )
    mgr = CalibrationManager()
    mgr._intrinsics['gripper'] = {
        'K': np.eye(3, dtype=np.float64),
        'dist': np.zeros((5, 1), dtype=np.float64),
    }
    R, t = _identity_buffer(HANDEYE_FRAMES_REQUIRED)
    mgr._handeye_buffers['gripper'] = type(
        'Buf', (), {
            'R_target2cam': R, 't_target2cam': t,
            'R_gripper2base': R, 't_gripper2base': t,
        }
    )()

    # Stub PARK and TSAI to differ by 5° rotation around z.
    import cv2 as _cv2
    R_park = np.eye(3)
    theta = np.deg2rad(5.0)
    R_tsai = np.array([
        [np.cos(theta), -np.sin(theta), 0],
        [np.sin(theta),  np.cos(theta), 0],
        [0,              0,             1],
    ])
    sequence = iter([
        (R_park, np.zeros((3, 1))),
        (R_tsai, np.zeros((3, 1))),
    ])
    monkeypatch.setattr(_cv2, 'calibrateHandEye', lambda *a, **kw: next(sequence))

    ok, _reproj, angle, msg = mgr._solve_handeye('gripper')
    assert ok is False
    assert 'abgewiesen' in msg.lower() or 'park' in msg.lower()
    # Above the 2° warn threshold.
    assert angle > 2.0
    # Crucially, no YAML is written when the solve is rejected.
    assert not mgr.has_handeye('gripper')

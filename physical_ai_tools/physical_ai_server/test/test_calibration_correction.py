"""W5 — ground-truth XY correction + yaw bias (P3b accuracy verify).

Covers the pure-math fit (exact similarity recovery, residuals, mirror
detection, circular-mean yaw bias) and the YAML write/read round-trip that
preserves the existing scene-extrinsic keys (transform / z_table / table_plane
/ board_*).
"""

from __future__ import annotations

import math

import numpy as np
import pytest


@pytest.fixture
def cm(monkeypatch, tmp_path):
    monkeypatch.setenv('EDUBOTICS_CALIB_DIR', str(tmp_path))
    from importlib import reload
    from physical_ai_server.workflow import calibration_manager as _cm
    reload(_cm)
    return _cm


# ── fit_xy_correction: exact recovery of a known similarity ──────────────────
def test_fit_exact_similarity_recovery(cm):
    mgr = cm.CalibrationManager()
    rng = np.random.default_rng(1)
    detected = rng.uniform(-0.2, 0.2, size=(6, 2))
    # Known similarity: rotate 15 deg, scale 1.05, translate.
    a = math.radians(15.0)
    s = 1.05
    R = s * np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
    tvec = np.array([0.03, -0.02])
    truth = (R @ detected.T).T + tvec
    res = mgr.fit_xy_correction(truth.tolist(), detected.tolist())
    assert res['ok'], res['message']
    assert res['mirror_detected'] is False
    assert res['point_count'] == 6
    # Residuals essentially zero for an exact similarity.
    assert res['residual_mm_mean'] < 1e-6
    assert res['residual_mm_max'] < 1e-6
    # The recovered affine applied to detected reproduces truth.
    M = res['xy_correction']
    for d, tr in zip(detected, truth):
        cx, cy = mgr.apply_xy_correction(d, M)
        assert abs(cx - tr[0]) < 1e-9 and abs(cy - tr[1]) < 1e-9


def test_fit_residuals_nonzero_with_noise(cm):
    mgr = cm.CalibrationManager()
    rng = np.random.default_rng(2)
    detected = rng.uniform(-0.2, 0.2, size=(8, 2))
    truth = detected + np.array([0.01, 0.01])          # pure translation
    truth += rng.normal(0, 0.002, size=truth.shape)    # 2 mm noise
    res = mgr.fit_xy_correction(truth.tolist(), detected.tolist())
    assert res['ok']
    assert res['residual_mm_mean'] > 0.0
    assert res['residual_mm_max'] >= res['residual_mm_mean']


def test_fit_mirror_detected(cm):
    mgr = cm.CalibrationManager()
    detected = np.array([[0.0, 0.0], [0.1, 0.0], [0.1, 0.1], [0.0, 0.1]])
    # Reflect across the x-axis -> handedness flip the fit must REJECT.
    truth = detected.copy()
    truth[:, 1] = -truth[:, 1]
    res = mgr.fit_xy_correction(truth.tolist(), detected.tolist())
    assert res['ok'] is False
    assert res['mirror_detected'] is True
    assert res['xy_correction'] is None
    assert 'piegel' in res['message'] or 'gespiegelt' in res['message']


def test_fit_too_few_points(cm):
    mgr = cm.CalibrationManager()
    res = mgr.fit_xy_correction([[0.0, 0.0]], [[0.1, 0.1]])
    assert res['ok'] is False
    assert res['mirror_detected'] is False


def test_fit_mismatched_lengths(cm):
    mgr = cm.CalibrationManager()
    res = mgr.fit_xy_correction([[0, 0], [1, 0]], [[0, 0]])
    assert res['ok'] is False


def test_fit_yaw_bias_circular_mean(cm):
    mgr = cm.CalibrationManager()
    detected = np.array([[0.0, 0.0], [0.1, 0.0], [0.0, 0.1], [0.1, 0.1]])
    truth = detected.copy()
    # truth yaw - detected yaw straddles the +/-pi wrap so a plain mean would be
    # wrong; circular mean must land near +0.1 rad.
    det_yaws = [math.pi - 0.05, -math.pi + 0.05, 3.0, -3.0]
    tru_yaws = [y + 0.1 for y in det_yaws]
    res = mgr.fit_xy_correction(truth.tolist(), detected.tolist(),
                                truth_yaws=tru_yaws, detected_yaws=det_yaws)
    assert res['ok']
    assert abs(res['yaw_bias_rad'] - 0.1) < 1e-6


def test_fit_no_yaws_zero_bias(cm):
    mgr = cm.CalibrationManager()
    detected = np.array([[0.0, 0.0], [0.1, 0.0], [0.0, 0.1]])
    res = mgr.fit_xy_correction(detected.tolist(), detected.tolist())
    assert res['ok']
    assert res['yaw_bias_rad'] == 0.0


# ── write / read round-trip preserving existing keys ─────────────────────────
def _seed_scene_handeye(cm):
    """Write a realistic scene_handeye.yaml via the production path so the
    write/read correction round-trip runs against the real key set."""
    import cv2
    mgr = cm.CalibrationManager()
    path = mgr._handeye_path('scene')
    transform = np.eye(4, dtype=np.float64)
    transform[2, 3] = 0.35
    plane = np.array([0.001, -0.002, 0.01], dtype=np.float64)
    fs = cv2.FileStorage(str(path), cv2.FILE_STORAGE_WRITE)
    fs.write('transform', transform)
    fs.write('method', 'BOARD_ON_TABLE+TOUCHOFF')
    fs.write('z_table', 0.012)
    fs.write('table_plane', plane)
    fs.write('board_table_z', 0.0)
    fs.write('board_origin_x', 0.18)
    fs.write('board_origin_y', 0.075)
    fs.release()
    return mgr, transform, plane


def test_write_read_round_trip_preserves_existing_keys(cm):
    import cv2
    mgr, transform, plane = _seed_scene_handeye(cm)
    M = [[1.01, 0.02, 0.005], [-0.02, 1.01, -0.003]]
    ok = mgr.write_verify_correction('scene', M, 0.05, 1.7, 4)
    assert ok

    # New keys readable via the helper.
    rc = mgr.read_verify_correction('scene')
    assert rc is not None
    np.testing.assert_allclose(rc['xy_correction'], np.asarray(M), atol=1e-9)
    assert abs(rc['yaw_bias_rad'] - 0.05) < 1e-9

    # Existing keys preserved byte-for-byte (values).
    fs = cv2.FileStorage(str(mgr._handeye_path('scene')), cv2.FILE_STORAGE_READ)
    np.testing.assert_allclose(fs.getNode('transform').mat(), transform, atol=1e-12)
    np.testing.assert_allclose(fs.getNode('table_plane').mat().reshape(-1), plane, atol=1e-12)
    assert abs(float(fs.getNode('z_table').real()) - 0.012) < 1e-12
    assert abs(float(fs.getNode('board_origin_x').real()) - 0.18) < 1e-12
    assert abs(float(fs.getNode('board_origin_y').real()) - 0.075) < 1e-12
    assert int(fs.getNode('verify_points').real()) == 4
    assert abs(float(fs.getNode('verify_residual_mm').real()) - 1.7) < 1e-9
    fs.release()


def test_read_verify_correction_absent_returns_none(cm):
    mgr, _, _ = _seed_scene_handeye(cm)
    # No verify keys written yet.
    assert mgr.read_verify_correction('scene') is None


def test_read_verify_correction_no_file_returns_none(cm):
    mgr = cm.CalibrationManager()
    assert mgr.read_verify_correction('scene') is None


def test_write_verify_correction_no_file_returns_false(cm):
    mgr = cm.CalibrationManager()
    ok = mgr.write_verify_correction('scene', [[1, 0, 0], [0, 1, 0]], 0.0, 0.0, 3)
    assert ok is False

"""P3a — multi-frame extrinsic averaging (sensor-jitter reduction).

One "Bild erfassen" click internally bursts N reads of the fixed board and
averages the per-read solvePnP poses (translation mean + SO(3) rotation mean).
Tests cover the rotation-average math (proper SO(3), noise reduction) and the
capture_frame burst orchestration (averages live reads, min-pass gate, and the
explicit-bgr single-capture path that the existing single-shot tests rely on).
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


def _rot(axis: str, a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    if axis == 'x':
        return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=np.float64)
    if axis == 'y':
        return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=np.float64)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=np.float64)


# ── rotation average (pure math) ─────────────────────────────────────────────
def test_average_rotations_single_returns_input(cm):
    R = _rot('z', 0.3) @ _rot('y', 0.1)
    out = cm.CalibrationManager._average_rotations([R])
    np.testing.assert_allclose(out, R, atol=1e-9)


def test_average_rotations_proper_so3_and_reduces_noise(cm):
    base = _rot('z', 0.5) @ _rot('y', 0.1)
    rng = np.random.default_rng(0)
    Rs = []
    for _ in range(60):
        noise = (_rot('x', rng.normal(0, 0.012))
                 @ _rot('y', rng.normal(0, 0.012))
                 @ _rot('z', rng.normal(0, 0.012)))
        Rs.append(base @ noise)
    avg = cm.CalibrationManager._average_rotations(Rs)
    # proper rotation (det +1, orthonormal)
    assert abs(float(np.linalg.det(avg)) - 1.0) < 1e-9
    np.testing.assert_allclose(avg @ avg.T, np.eye(3), atol=1e-9)
    # the average is closer to the truth than a typical single noisy sample
    err = lambda A, B: float(np.linalg.norm(A - B))   # noqa: E731 (chordal proxy)
    assert err(avg, base) < np.mean([err(R, base) for R in Rs])


# ── capture_frame burst orchestration ────────────────────────────────────────
def _setup_extrinsic(cm, monkeypatch, burst=5, min_pass=2):
    mgr = cm.CalibrationManager()
    mgr._intrinsics['scene'] = {'K': np.eye(3, dtype=np.float64),
                                'dist': np.zeros((5, 1), dtype=np.float64)}
    mgr.start_step('scene', 'extrinsic')
    assert 'scene' in mgr._handeye_buffers
    monkeypatch.setattr(cm, 'EXTRINSIC_BURST_FRAMES', burst)
    monkeypatch.setattr(cm, 'EXTRINSIC_BURST_INTERVAL_S', 0.0)
    monkeypatch.setattr(cm, 'EXTRINSIC_BURST_MIN_PASS', min_pass)
    mgr._get_frame = lambda camera: np.zeros((10, 10, 3), dtype=np.uint8)
    return mgr


def test_burst_averages_live_reads(cm, monkeypatch):
    mgr = _setup_extrinsic(cm, monkeypatch, burst=5, min_pass=2)
    base_R = _rot('z', 0.2)
    base_t = np.array([0.0, 0.0, 0.40], dtype=np.float64).reshape(3, 1)
    seq = []
    for k in range(4):                                   # 4 reads pass, then None
        seq.append((base_R @ _rot('x', 0.002 * (k - 1.5)),
                    base_t + np.array([0.001 * (k - 1.5), 0, 0]).reshape(3, 1),
                    0.5 + 0.1 * k))
    mgr._board_pose_from_frame = lambda frame, K, dist: (seq.pop(0) if seq else None)

    ok, captured, required, reproj, msg = mgr.capture_frame('scene', bgr=None)
    assert ok, msg
    assert 'gemittelt' in msg
    assert captured == 1 and required == 1
    buf = mgr._handeye_buffers['scene']
    assert len(buf.R_target2cam) == 1
    np.testing.assert_allclose(buf.R_target2cam[0], base_R, atol=5e-3)
    np.testing.assert_allclose(buf.t_target2cam[0].reshape(3), base_t.reshape(3), atol=5e-3)


def test_burst_too_few_passes_fails(cm, monkeypatch):
    mgr = _setup_extrinsic(cm, monkeypatch, burst=5, min_pass=3)
    seq = [(_rot('z', 0.1), np.array([0, 0, 0.4]).reshape(3, 1), 0.5)]  # only 1 passes
    mgr._board_pose_from_frame = lambda frame, K, dist: (seq.pop(0) if seq else None)
    ok, _c, _r, _e, msg = mgr.capture_frame('scene', bgr=None)
    assert ok is False
    assert 'stabil' in msg                                # "nicht stabil erfasst"
    assert len(mgr._handeye_buffers['scene'].R_target2cam) == 0


def test_explicit_bgr_is_single_capture_no_burst(cm, monkeypatch):
    mgr = _setup_extrinsic(cm, monkeypatch, burst=8, min_pass=4)
    calls = {'n': 0}

    def fake(frame, K, dist):
        calls['n'] += 1
        return (_rot('z', 0.1), np.array([0, 0, 0.40]).reshape(3, 1), 0.5)

    mgr._board_pose_from_frame = fake
    # If the explicit-bgr path ever bursts live reads, this would fire:
    mgr._get_frame = lambda camera: (_ for _ in ()).throw(
        AssertionError('explicit bgr must not trigger a live burst'))

    ok, captured, required, _e, msg = mgr.capture_frame(
        'scene', bgr=np.zeros((10, 10, 3), dtype=np.uint8))
    assert ok, msg
    assert calls['n'] == 1                                # exactly one pose, no burst
    assert captured == 1

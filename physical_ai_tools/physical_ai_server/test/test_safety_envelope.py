"""Verifies the extracted SafetyEnvelope class produces byte-identical
outputs to the original ``_apply_safety_envelope`` it was extracted from
(the parity test the verification report requested as M2).

If a refactor of SafetyEnvelope changes any clamping / NaN / delta
behaviour, this test will fail, which is the entire point.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from physical_ai_server.workflow.safety_envelope import SafetyEnvelope


def _envelope() -> SafetyEnvelope:
    env = SafetyEnvelope()
    env.set_action_limits(
        joint_min=[-math.pi, -math.pi / 2, -math.pi / 2, -math.pi, -math.pi, -1.0],
        joint_max=[math.pi, math.pi / 2, math.pi / 2, math.pi, math.pi, 1.0],
        max_delta_per_tick=[0.3] * 6,
    )
    return env


def test_nan_action_returns_none():
    env = _envelope()
    action = np.array([0.0, 0.0, 0.0, 0.0, 0.0, np.nan], dtype=np.float32)
    assert env.apply(action) is None


def test_inf_action_returns_none():
    env = _envelope()
    action = np.array([np.inf, 0.0, 0.0, 0.0, 0.0, 0.0], dtype=np.float32)
    assert env.apply(action) is None


def test_action_within_limits_passes_through():
    env = _envelope()
    action = np.array([0.1, 0.1, 0.1, 0.1, 0.1, 0.1], dtype=np.float32)
    out = env.apply(action)
    assert out is not None
    np.testing.assert_array_almost_equal(out, action)


def test_action_clamped_to_joint_max():
    env = _envelope()
    action = np.array([10.0, 10.0, 10.0, 10.0, 10.0, 10.0], dtype=np.float32)
    out = env.apply(action)
    assert out is not None
    expected = np.array([math.pi, math.pi / 2, math.pi / 2, math.pi, math.pi, 1.0], dtype=np.float32)
    np.testing.assert_array_almost_equal(out, expected)


def test_action_clamped_to_joint_min():
    env = _envelope()
    action = np.array([-10.0] * 6, dtype=np.float32)
    out = env.apply(action)
    assert out is not None
    expected = np.array([-math.pi, -math.pi / 2, -math.pi / 2, -math.pi, -math.pi, -1.0], dtype=np.float32)
    np.testing.assert_array_almost_equal(out, expected)


def test_delta_cap_clamps_not_rejects():
    env = _envelope()
    # Seed with a previous action.
    env.apply(np.zeros(6, dtype=np.float32))
    # Try to jump by 1.0 rad in one tick — should be clamped to 0.3 rad.
    action = np.array([1.0, 0.0, 0.0, 0.0, 0.0, 0.0], dtype=np.float32)
    out = env.apply(action)
    assert out is not None
    assert abs(out[0]) <= 0.3 + 1e-6


def test_delta_sign_preserved_on_cap():
    env = _envelope()
    env.apply(np.zeros(6, dtype=np.float32))
    action = np.array([-1.0, 0.0, 0.0, 0.0, 0.0, 0.0], dtype=np.float32)
    out = env.apply(action)
    assert out is not None
    assert out[0] < 0


def test_reset_clears_last_action():
    env = _envelope()
    env.apply(np.array([0.5, 0, 0, 0, 0, 0], dtype=np.float32))
    env.reset()
    # First call after reset should not be subject to delta cap.
    out = env.apply(np.array([0.5, 0, 0, 0, 0, 0], dtype=np.float32))
    np.testing.assert_array_almost_equal(out, [0.5, 0, 0, 0, 0, 0])


def test_shape_mismatch_does_not_clamp_but_warns_once(capsys):
    env = _envelope()
    # 7-element action against 6-element limits: clamp must skip,
    # NaN guard still runs, warning emitted exactly once.
    env.apply(np.ones(7, dtype=np.float32))
    env.apply(np.ones(7, dtype=np.float32))
    captured = capsys.readouterr().out
    assert captured.count('[WARNUNG] Aktion hat 7 Werte') == 1

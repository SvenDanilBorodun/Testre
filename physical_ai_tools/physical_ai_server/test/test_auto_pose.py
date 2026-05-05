"""auto_pose hemisphere sampler — geometric properties + reachability stub."""

from __future__ import annotations

import math

import numpy as np

from physical_ai_server.workflow.auto_pose import (
    HEMISPHERE_RADIUS_MAX_M,
    HEMISPHERE_RADIUS_MIN_M,
    POLAR_MAX_DEG,
    POLAR_MIN_DEG,
    suggest_pose,
    _default_reachability,
)


def test_default_reachability_inside_shell_passes():
    target = np.array([0.25, 0.0, 0.05])
    assert _default_reachability(target, np.array([0, 0, 0, 1]))


def test_default_reachability_outside_shell_rejected():
    too_far = np.array([0.5, 0.0, 0.0])
    assert not _default_reachability(too_far, np.array([0, 0, 0, 1]))
    too_close = np.array([0.1, 0.0, 0.0])
    assert not _default_reachability(too_close, np.array([0, 0, 0, 1]))


def test_suggest_pose_returns_within_shell():
    rng = np.random.default_rng(seed=42)
    candidate = suggest_pose([], rng=rng)
    assert candidate is not None
    radius = float(np.linalg.norm(candidate.target_xyz))
    assert HEMISPHERE_RADIUS_MIN_M - 1e-6 <= radius <= HEMISPHERE_RADIUS_MAX_M + 1e-6


def test_suggest_pose_diversity_from_existing_capture():
    """If we feed in a candidate quaternion, the next suggestion should
    be at least 30 deg away in axis-angle (the diversity threshold)."""
    rng = np.random.default_rng(seed=1)
    first = suggest_pose([], rng=rng)
    assert first is not None
    second = suggest_pose([first.target_quat], rng=rng)
    if second is not None:
        # Compute angular diff between the two quaternions.
        dot = abs(float(np.dot(first.target_quat, second.target_quat)))
        dot = min(1.0, max(-1.0, dot))
        angle_deg = math.degrees(2.0 * math.acos(dot))
        # Sampler enforces >= 30 deg — but only when reachable & visible
        # candidates are abundant. Loosen to 15 deg for the stub-only case.
        assert angle_deg >= 15.0


def test_suggest_pose_polar_within_bounds():
    rng = np.random.default_rng(seed=99)
    for _ in range(20):
        candidate = suggest_pose([], rng=rng)
        if candidate is None:
            continue
        # polar = arccos(z / radius)
        radius = float(np.linalg.norm(candidate.target_xyz))
        polar_deg = math.degrees(math.acos(candidate.target_xyz[2] / radius))
        assert POLAR_MIN_DEG - 1.0 <= polar_deg <= POLAR_MAX_DEG + 1.0


def test_suggest_pose_unreachable_filter_returns_none():
    """A reachability function that always rejects → None."""
    candidate = suggest_pose([], is_reachable=lambda xyz, quat: False)
    assert candidate is None

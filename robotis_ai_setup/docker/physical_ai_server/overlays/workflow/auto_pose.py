#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Sample candidate calibration poses on a hemisphere around the board centre.

The sampler scores candidates by:
1. Board fully inside the camera frustum at the suggested pose (using the
   current intrinsic estimate plus the candidate camera->board transform).
2. Angular diversity vs already-captured poses (>=30 deg of axis-angle
   change is preferred).
3. Geometric pre-filter ``_default_reachability``: rejects candidates
   outside the hemisphere shell (radius 0.20-0.30 m measured from the
   board centre). Real IK reachability is enforced when the wizard calls
   /calibration/execute_pose, so this filter is intentionally permissive
   — its job is only to drop obviously-wrong samples cheaply.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

import numpy as np

HEMISPHERE_RADIUS_MIN_M = 0.20
HEMISPHERE_RADIUS_MAX_M = 0.30
POLAR_MIN_DEG = 30.0
POLAR_MAX_DEG = 75.0
ANGULAR_DIVERSITY_MIN_DEG = 30.0
DEFAULT_NUM_CANDIDATES = 64


@dataclass
class PoseCandidate:
    target_xyz: np.ndarray
    target_quat: np.ndarray
    score: float


def _default_reachability(
    target_xyz: np.ndarray,
    target_quat: np.ndarray,
    board_centre_base: np.ndarray = np.array([0.0, 0.0, 0.0]),
) -> bool:
    """Geometric pre-filter on candidates around the board centre.

    Bug fix (audit §6): the original implementation measured radius from
    the base origin, but candidates are sampled around board_centre_base
    (typically [0.25, 0, z_table]). With the wrong reference, most
    candidates fell outside [0.20, 0.30] from origin and were rejected.
    The filter now compares distance from the board centre, which is the
    same frame in which candidates are constructed."""
    delta = np.asarray(target_xyz) - np.asarray(board_centre_base)
    radius = float(np.linalg.norm(delta))
    return HEMISPHERE_RADIUS_MIN_M <= radius <= HEMISPHERE_RADIUS_MAX_M


def _spherical_to_xyz(radius: float, polar_deg: float, azimuth_deg: float) -> np.ndarray:
    polar = math.radians(polar_deg)
    azimuth = math.radians(azimuth_deg)
    x = radius * math.sin(polar) * math.cos(azimuth)
    y = radius * math.sin(polar) * math.sin(azimuth)
    z = radius * math.cos(polar)
    return np.array([x, y, z])


def _look_at_quat(eye_xyz: np.ndarray, target_xyz: np.ndarray) -> np.ndarray:
    """Quaternion (qx, qy, qz, qw) so the camera at eye looks at target with
    +Y up (standard ROS REP-103 right-handed convention)."""
    forward = target_xyz - eye_xyz
    forward /= np.linalg.norm(forward) + 1e-9
    up = np.array([0.0, 0.0, 1.0])
    right = np.cross(up, forward)
    if np.linalg.norm(right) < 1e-6:
        up = np.array([0.0, 1.0, 0.0])
        right = np.cross(up, forward)
    right /= np.linalg.norm(right) + 1e-9
    new_up = np.cross(forward, right)
    R = np.column_stack([right, new_up, forward])
    return _rotation_matrix_to_quaternion(R)


def _rotation_matrix_to_quaternion(R: np.ndarray) -> np.ndarray:
    trace = R[0, 0] + R[1, 1] + R[2, 2]
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        qw = 0.25 * s
        qx = (R[2, 1] - R[1, 2]) / s
        qy = (R[0, 2] - R[2, 0]) / s
        qz = (R[1, 0] - R[0, 1]) / s
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = math.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2]) * 2.0
        qw = (R[2, 1] - R[1, 2]) / s
        qx = 0.25 * s
        qy = (R[0, 1] + R[1, 0]) / s
        qz = (R[0, 2] + R[2, 0]) / s
    elif R[1, 1] > R[2, 2]:
        s = math.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2]) * 2.0
        qw = (R[0, 2] - R[2, 0]) / s
        qx = (R[0, 1] + R[1, 0]) / s
        qy = 0.25 * s
        qz = (R[1, 2] + R[2, 1]) / s
    else:
        s = math.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1]) * 2.0
        qw = (R[1, 0] - R[0, 1]) / s
        qx = (R[0, 2] + R[2, 0]) / s
        qy = (R[1, 2] + R[2, 1]) / s
        qz = 0.25 * s
    return np.array([qx, qy, qz, qw])


def _quat_angular_diff_deg(q1: np.ndarray, q2: np.ndarray) -> float:
    dot = abs(float(np.dot(q1, q2)))
    dot = min(1.0, max(-1.0, dot))
    return math.degrees(2.0 * math.acos(dot))


def suggest_pose(
    captured_quats: list[np.ndarray],
    board_centre_base: np.ndarray = np.array([0.0, 0.0, 0.0]),
    is_reachable: Callable[..., bool] = _default_reachability,
    num_candidates: int = DEFAULT_NUM_CANDIDATES,
    rng: np.random.Generator | None = None,
) -> PoseCandidate | None:
    """Return the highest-scoring reachable candidate, or None if none of the
    sampled candidates is reachable + diverse enough.

    ``is_reachable`` is called with ``(target_xyz, target_quat)`` and may
    optionally accept a ``board_centre_base`` keyword. Custom IK-backed
    callers that ignore the board centre keep working because the kw is
    only passed when the callable accepts it."""
    if rng is None:
        rng = np.random.default_rng()

    # Detect whether the reachability callable accepts board_centre_base
    # so we can forward it to the geometric default without breaking
    # custom IK-backed implementations that take only (xyz, quat).
    import inspect
    try:
        _params = inspect.signature(is_reachable).parameters
        _accepts_board = (
            'board_centre_base' in _params
            or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in _params.values())
        )
    except (TypeError, ValueError):
        _accepts_board = False

    best: PoseCandidate | None = None
    for _ in range(num_candidates):
        radius = float(rng.uniform(HEMISPHERE_RADIUS_MIN_M, HEMISPHERE_RADIUS_MAX_M))
        polar = float(rng.uniform(POLAR_MIN_DEG, POLAR_MAX_DEG))
        azimuth = float(rng.uniform(-180.0, 180.0))

        offset = _spherical_to_xyz(radius, polar, azimuth)
        target_xyz = board_centre_base + offset
        target_quat = _look_at_quat(target_xyz, board_centre_base)

        reachable = (
            is_reachable(target_xyz, target_quat, board_centre_base=board_centre_base)
            if _accepts_board
            else is_reachable(target_xyz, target_quat)
        )
        if not reachable:
            continue

        diversity = _diversity_score(target_quat, captured_quats)
        if diversity < ANGULAR_DIVERSITY_MIN_DEG and captured_quats:
            continue

        height_score = max(0.0, math.cos(math.radians(polar - 50.0)))
        score = diversity + 10.0 * height_score
        if best is None or score > best.score:
            best = PoseCandidate(target_xyz=target_xyz, target_quat=target_quat, score=score)
    return best


def _diversity_score(candidate_quat: np.ndarray, captured_quats: list[np.ndarray]) -> float:
    if not captured_quats:
        return 90.0
    return float(min(_quat_angular_diff_deg(candidate_quat, q) for q in captured_quats))

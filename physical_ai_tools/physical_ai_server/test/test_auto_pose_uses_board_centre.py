"""Audit §1.6b — ``suggest_pose`` must respect a non-zero
``board_centre_base``. The v1 ship called ``suggest_pose([])`` with
the default origin (0, 0, 0), so every candidate landed inside the
robot's own footprint and was rejected by IK.
"""

from __future__ import annotations

import math

import numpy as np

from physical_ai_server.workflow.auto_pose import (
    HEMISPHERE_RADIUS_MAX_M,
    HEMISPHERE_RADIUS_MIN_M,
    suggest_pose,
)


def test_candidate_centred_on_board_not_origin():
    """Pass a non-zero board centre and verify each candidate lies on
    the hemisphere shell AROUND that centre (not around the origin)."""
    rng = np.random.default_rng(seed=7)
    board = np.array([0.25, 0.00, 0.05])
    candidate = suggest_pose([], board_centre_base=board, rng=rng)
    assert candidate is not None
    # Distance from the board centre falls within the configured shell.
    offset = np.linalg.norm(candidate.target_xyz - board)
    assert HEMISPHERE_RADIUS_MIN_M - 1e-6 <= offset <= HEMISPHERE_RADIUS_MAX_M + 1e-6
    # And the absolute position is shifted in front of the base, not at it.
    assert candidate.target_xyz[0] > 0.0


def test_default_origin_still_works_for_unit_tests():
    """Backward-compat: leaving board_centre_base unspecified should
    still produce a candidate (used by existing diversity / polar
    tests) — we only fail-loud about the specific server callsite."""
    rng = np.random.default_rng(seed=11)
    candidate = suggest_pose([], rng=rng)
    assert candidate is not None

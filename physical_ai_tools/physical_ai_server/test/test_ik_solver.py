"""Closed-form IK + exact FK tests for the OMX-F arm.

Pure NumPy — runs without a container / PyKDL. The FK∘IK round-trip is the
authoritative CI gate against a silent derivation bug (wrong elbow branch,
offset/sign error, arccos-domain NaN). FK is validated independently against a
pose computed by hand from the URDF chain, so the round-trip can't pass
vacuously on a wrong-but-self-consistent FK.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from physical_ai_server.workflow.ik_solver import IKSolver


def _ik() -> IKSolver:
    return IKSolver()


# ── FK anchors (independent of IK) ───────────────────────────────────────────

def test_fk_zero_pose_matches_urdf_chain():
    # Hand-computed from omx_f.urdf: sum the joint origins at all-zero joints.
    R, t = _ik().fk([0, 0, 0, 0, 0])
    np.testing.assert_allclose(t, [0.31288, -0.0016, 0.21065], atol=1e-5)
    # Tool x-axis points forward (+x) at the zero pose.
    np.testing.assert_allclose(R @ np.array([1.0, 0, 0]), [1, 0, 0], atol=1e-9)


def test_fk_joint1_rotation_is_pure_yaw():
    # Rotating joint1 by +90 deg maps the zero-pose reach to +y.
    R, t = _ik().fk([math.pi / 2, 0, 0, 0, 0])
    assert t[1] > 0.30 and abs(t[0]) < 0.02 and abs(t[2] - 0.21065) < 1e-6


def test_fk_nonzero_pose_matches_independent_calc():
    # Independent anchor at a NON-zero pose (shoulder rotated -90 deg): hand-
    # computed by rotating the j2→EE offset sum by Ry(-pi/2). This catches a
    # wrong-but-self-consistent FK that the FK∘IK round-trip can't (it uses the
    # same FK).
    _R, t = _ik().fk([0, -math.pi / 2, 0, 0, 0])
    np.testing.assert_allclose(t, [-0.1244, -0.0016, 0.42163], atol=1e-4)


def test_fk_requires_five_joints():
    assert _ik().fk([0, 0, 0]) is None


# ── link_points (no-go-zone swept check support) ─────────────────────────────

def test_link_points_requires_five_joints():
    assert _ik().link_points([0, 0, 0]) is None


def test_link_points_includes_base_joint_origins_and_ee():
    ik = _ik()
    q = ik.solve((0.20, 0.0, 0.03))
    assert q is not None
    pts = ik.link_points(q)
    assert pts is not None and len(pts) >= 7
    # the base origin is present
    assert any(float(np.linalg.norm(np.asarray(p))) < 1e-9 for p in pts)
    # the EE link point coincides with fk(q) (the round-trip vs fk)
    _R, t = ik.fk(q)
    assert any(float(np.linalg.norm(np.asarray(p) - t)) < 1e-9 for p in pts)
    # every point is finite
    assert all(np.all(np.isfinite(np.asarray(p))) for p in pts)


def test_link_points_interpolates_along_the_long_links():
    # More samples_per_link → more points, all interpolated ON the chain (so a
    # thin obstacle can't hide between two joints).
    ik = _ik()
    q = ik.solve((0.20, 0.0, 0.03))
    assert len(ik.link_points(q, samples_per_link=2)) < \
        len(ik.link_points(q, samples_per_link=8))


def test_link_points_extend_past_ee_along_tool_axis():
    # The gripper fingers reach past the EE: at least one point lies beyond the
    # EE along the +tool axis (for a top-down grasp that is straight down).
    ik = _ik()
    q = ik.solve((0.20, 0.0, 0.03))
    R, t = ik.fk(q)
    axis = R @ np.array([1.0, 0.0, 0.0])      # tool x-axis in the base frame
    beyond = [p for p in ik.link_points(q)
              if float(np.dot(np.asarray(p) - t, axis)) > 1e-4]
    assert beyond, 'no link point projected past the EE along the tool axis'


# ── FK∘IK round-trip (the CI gate) ───────────────────────────────────────────

def test_fk_ik_roundtrip_over_reachable_grid():
    ik = _ik()
    solved = 0
    total = 0
    max_err = 0.0
    for r in np.linspace(0.12, 0.26, 8):
        for ang in np.linspace(-1.2, 1.2, 9):
            for z in (0.0, 0.03, 0.06):
                total += 1
                x, y = r * math.cos(ang), r * math.sin(ang)
                q = ik.solve((x, y, z))
                if q is None:
                    continue
                solved += 1
                _R, t = ik.fk(q)
                max_err = max(max_err, float(np.linalg.norm(t - np.array([x, y, z]))))
    # The vast majority of the top-down annulus must solve, and every solved
    # point must land within tolerance (the ~1.6 mm structural tool offset).
    assert solved / total > 0.85
    assert max_err <= 3e-3


def test_solution_is_strictly_vertical_top_down():
    ik = _ik()
    for x, y, z in [(0.20, 0.0, 0.02), (0.18, 0.08, 0.0), (0.15, -0.10, 0.05)]:
        q = ik.solve((x, y, z))
        assert q is not None
        R, _t = ik.fk(q)
        approach = R @ np.array([1.0, 0, 0])  # forearm/tool x-axis
        np.testing.assert_allclose(approach, [0, 0, -1.0], atol=1e-6)


# ── determinism + reachability ───────────────────────────────────────────────

def test_solve_is_deterministic():
    ik = _ik()
    sols = {tuple(np.round(ik.solve((0.20, 0.05, 0.03)), 9)) for _ in range(50)}
    assert len(sols) == 1


def test_unreachable_returns_none():
    ik = _ik()
    assert ik.solve((0.50, 0.0, 0.0)) is None      # too far
    assert ik.solve((0.02, 0.0, 0.0)) is None       # too close (inside annulus)
    assert ik.solve((0.0, 0.0, 0.40)) is None        # too high


def test_in_workspace_matches_solve():
    ik = _ik()
    assert ik.in_workspace((0.20, 0.0, 0.02)) is True
    assert ik.in_workspace((0.50, 0.0, 0.0)) is False


# ── joint limits + roll ──────────────────────────────────────────────────────

def test_solutions_respect_dynamixel_limits():
    ik = _ik()
    from physical_ai_server.workflow.ik_solver import _DXL_JOINT_LIMITS_RAD
    for r in np.linspace(0.12, 0.26, 6):
        for ang in np.linspace(-1.0, 1.0, 5):
            q = ik.solve((r * math.cos(ang), r * math.sin(ang), 0.03))
            if q is None:
                continue
            for i, (lo, hi) in enumerate(_DXL_JOINT_LIMITS_RAD):
                assert lo - 1e-6 <= q[i] <= hi + 1e-6


def test_roll_sets_joint5():
    ik = _ik()
    q0 = ik.solve((0.20, 0.0, 0.03), roll=0.0)
    qr = ik.solve((0.20, 0.0, 0.03), roll=0.5)
    assert q0 is not None and qr is not None
    assert abs(qr[4] - 0.5) < 1e-9
    np.testing.assert_allclose(q0[:4], qr[:4], atol=1e-9)


def test_base_yaw_matches_solve_theta1():
    # base_yaw MUST equal the theta1 (joint1) that solve() picks, for every
    # reachable point — they share the same aiming math. This is the contract
    # the named-object orientation formula (joint5 = base_yaw − tag_yaw + const)
    # depends on.
    ik = _ik()
    for x, y, z in [(0.20, 0.0, 0.02), (0.18, 0.08, 0.0),
                    (0.15, -0.10, 0.05), (0.22, 0.05, 0.03)]:
        q = ik.solve((x, y, z))
        assert q is not None
        assert abs(ik.base_yaw(x, y) - q[0]) < 1e-12


def test_base_yaw_uses_j1_axis_offset_and_sign():
    # Exact formula atan2(y, x - _J1_AXIS_X), NOT atan2(y, x) — the sign-trap
    # the plan (§23.1) flags. _J1_AXIS_X is negative, so x - _J1_AXIS_X = x + 0.01125.
    from physical_ai_server.workflow.ik_solver import _J1_AXIS_X
    ik = _ik()
    assert _J1_AXIS_X < 0
    assert abs(ik.base_yaw(0.20, 0.10) - math.atan2(0.10, 0.20 - _J1_AXIS_X)) < 1e-12
    # +y -> positive yaw, -y -> negative yaw, straight ahead -> ~0
    assert ik.base_yaw(0.20, 0.10) > 0
    assert ik.base_yaw(0.20, -0.10) < 0
    assert abs(ik.base_yaw(0.20, 0.0)) < 1e-9
    # range is (-pi, pi]
    for x, y in [(-0.20, 0.0), (0.0, 0.20), (0.0, -0.20), (-0.20, -1e-9)]:
        v = ik.base_yaw(x, y)
        assert -math.pi < v <= math.pi + 1e-12


def test_solve_quat_is_position_only():
    ik = _ik()
    q = ik.solve_quat((0.20, 0.0, 0.03), (0.0, 0.0, 0.0, 1.0))
    assert q is not None
    _R, t = ik.fk(q)
    np.testing.assert_allclose(t, [0.20, 0.0, 0.03], atol=3e-3)


# ── metadata + URDF verification ─────────────────────────────────────────────

def test_backend_and_num_joints():
    ik = _ik()
    assert ik.backend == 'closed-form'
    assert ik.num_joints() == 5


def test_urdf_constants_verify_clean_on_matching_urdf(caplog):
    urdf = """<?xml version="1.0"?><robot name="omx_f">
      <link name="link0"/><link name="link1"/><link name="link2"/>
      <link name="link3"/><link name="link4"/><link name="link5"/>
      <link name="end_effector_link"/>
      <joint name="joint1" type="revolute"><parent link="link0"/><child link="link1"/>
        <origin xyz="-0.01125 0 0.034"/><axis xyz="0 0 1"/>
        <limit effort="1" lower="-6.28" upper="6.28" velocity="4.8"/></joint>
      <joint name="joint2" type="revolute"><parent link="link1"/><child link="link2"/>
        <origin xyz="0 0 0.0635"/><axis xyz="0 1 0"/>
        <limit effort="1" lower="-6.28" upper="6.28" velocity="4.8"/></joint>
      <joint name="joint3" type="revolute"><parent link="link2"/><child link="link3"/>
        <origin xyz="0.0415 0 0.11315"/><axis xyz="0 1 0"/>
        <limit effort="1" lower="-6.28" upper="6.28" velocity="4.8"/></joint>
      <joint name="joint4" type="revolute"><parent link="link3"/><child link="link4"/>
        <origin xyz="0.162 0 0"/><axis xyz="0 1 0"/>
        <limit effort="1" lower="-6.28" upper="6.28" velocity="4.8"/></joint>
      <joint name="joint5" type="revolute"><parent link="link4"/><child link="link5"/>
        <origin xyz="0.0287 0 0"/><axis xyz="1 0 0"/>
        <limit effort="1" lower="-6.28" upper="6.28" velocity="4.8"/></joint>
      <joint name="end_effector_joint" type="fixed"><parent link="link5"/>
        <child link="end_effector_link"/><origin xyz="0.09193 -0.0016 0"/></joint>
    </robot>"""
    try:
        import urdf_parser_py  # noqa: F401
    except ImportError:
        pytest.skip("urdf_parser_py not installed")
    import logging
    with caplog.at_level(logging.ERROR):
        IKSolver(urdf_string=urdf)
    assert not [r for r in caplog.records if 'IK constant mismatch' in r.message]


# ── #HIGH-1: the solver must REFUSE non-finite targets/roll (was fail-open) ────

def test_solve_rejects_nonfinite_target():
    ik = _ik()
    for bad in [(float('nan'), 0.0, 0.02), (float('inf'), 0.0, 0.02),
                (0.20, float('nan'), 0.02), (0.20, 0.0, float('nan')),
                (0.20, 0.0, float('inf'))]:
        assert ik.solve(bad) is None, f'solve({bad}) should be None, not garbage'
        assert ik.in_workspace(bad) is False


def test_solve_rejects_nonfinite_roll():
    ik = _ik()
    assert ik.solve((0.20, 0.0, 0.02), roll=float('nan')) is None
    assert ik.solve((0.20, 0.0, 0.02), roll=float('inf')) is None


def test_solve_still_returns_finite_for_reachable_target():
    sol = _ik().solve((0.20, 0.0, 0.02))
    assert sol is not None
    assert all(math.isfinite(v) for v in sol)

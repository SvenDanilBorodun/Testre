"""PR-2 n=6 legs (§16.4): the DOF-generalised seams exercised with a
TEST-LOCAL synthetic 6-arm-joint profile (NOT the registry — the GUI↔server
lockstep test would fail on a registry entry before PR 4 lands).

Grows one section per slice (2a…2d). The n=5 identity proof is the UNTOUCHED
existing suite + test_dof_golden.py; these tests only cover the new ``n``
parameters with 6-DOF-shaped inputs (7-wide full vectors, 8-wide Contract-B
rows).
"""

from __future__ import annotations

import math

import pytest

from physical_ai_server.workflow import trajectory_builder


# The edu6-shaped synthetic: 6 arm joints + gripper = 7-wide full vectors,
# velocity limit 5.45 (the follower_arm_modified_final1 URDF value).
N6 = 6
N6_VLIMIT = 5.45


# ── slice 2a: trajectory_builder velocity_limit kwarg ────────────────────────

def test_build_segment_accepts_7_wide_vectors():
    seg = trajectory_builder.build_segment(
        [0.0] * (N6 + 1), [0.1] * (N6 + 1), 1.0, velocity_limit=N6_VLIMIT)
    assert seg
    assert all(len(q) == N6 + 1 for q, _t in seg)
    assert seg[-1][0] == pytest.approx([0.1] * (N6 + 1))


def test_velocity_floor_uses_custom_limit():
    # A swing whose floor differs between 4.8 and 5.45 rad/s proves the kwarg
    # is actually consumed (not silently ignored).
    delta = 3.0
    seg_default = trajectory_builder.build_segment(
        [0.0] * 7, [delta] + [0.0] * 6, 0.1)
    seg_n6 = trajectory_builder.build_segment(
        [0.0] * 7, [delta] + [0.0] * 6, 0.1, velocity_limit=N6_VLIMIT)
    t_default = seg_default[-1][1]
    t_n6 = seg_n6[-1][1]
    expect_default = delta * (15.0 / 8.0) / (0.6 * 4.8)
    expect_n6 = delta * (15.0 / 8.0) / (0.6 * N6_VLIMIT)
    assert t_default == pytest.approx(expect_default, rel=0.05)
    assert t_n6 == pytest.approx(expect_n6, rel=0.05)
    assert t_n6 < t_default  # higher limit → shorter floored duration


def test_velocity_floor_default_unchanged():
    # The default path must stay the OMX 4.8 (bit-identical floor arithmetic).
    import numpy as np
    d = np.array([2.0, 0.0])
    assert trajectory_builder._velocity_safe_duration(d, 0.1) == (
        trajectory_builder._velocity_safe_duration(d, 0.1, 4.8))
    assert trajectory_builder._velocity_safe_duration(d, 0.1) == pytest.approx(
        2.0 * (15.0 / 8.0) / (0.6 * 4.8))


# ── slice 2b: handlers (motion ctx accessors + Contract-B width) ─────────────

import types  # noqa: E402

from physical_ai_server.workflow.handlers import motion  # noqa: E402
from physical_ai_server.workflow.handlers.motion import (  # noqa: E402
    WorkflowError,
    _gripper_closed,
    _gripper_open,
    _home_joints,
    _n,
    _roll_idx,
    _velocity_limit,
)
from physical_ai_server.workflow.handlers.trajectory import (  # noqa: E402
    extract_points,
    resegment_trajectory,
)

_EDU6_HOME = (0.0, 0.70, -2.40, 0.0, 0.70, 0.0)


def _n6_ctx(**kw):
    """A TEST-LOCAL synthetic 6-arm-joint profile ctx (NOT the registry)."""
    base = dict(
        num_arm_joints=N6,
        home_joints_rad=_EDU6_HOME,
        gripper_open_rad=1.75,
        gripper_closed_rad=0.0,
        velocity_limit_rad_s=N6_VLIMIT,
        last_full_joints=list(_EDU6_HOME) + [1.75],
        last_arm_joints=None,
        motion_lock=None,
        should_stop=lambda: False,
        zones=None,
        ik=None,
        tempo=1.0,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_ctx_accessors_default_to_omx():
    empty = types.SimpleNamespace()
    assert _n(empty) == 5
    assert _roll_idx(empty) == 4
    assert _home_joints(empty) == list(motion.HOME_JOINTS_RAD)
    assert _gripper_open(empty) == motion.GRIPPER_OPEN_RAD
    assert _gripper_closed(empty) == motion.GRIPPER_CLOSED_RAD
    assert _velocity_limit(empty) == 4.8


def test_ctx_accessors_read_the_synthetic_profile():
    ctx = _n6_ctx()
    assert _n(ctx) == 6
    assert _roll_idx(ctx) == 5
    assert _home_joints(ctx) == list(_EDU6_HOME)
    assert _gripper_open(ctx) == 1.75
    assert _gripper_closed(ctx) == 0.0
    assert _velocity_limit(ctx) == N6_VLIMIT


def test_home_handler_emits_7_wide_vectors():
    published: list = []
    ctx = _n6_ctx(publisher=lambda chunk: published.extend(chunk))
    ctx.last_full_joints = [0.1, 0.9, -2.0, 0.05, 0.9, 0.1, 0.4]  # 7-wide live
    motion.home(ctx, {})
    assert ctx.last_full_joints == list(_EDU6_HOME) + [0.4]  # gripper carried
    assert ctx.last_arm_joints == list(_EDU6_HOME)
    assert all(len(q) == 7 for q, *_ in published)
    assert published[-1][0] == pytest.approx(list(_EDU6_HOME) + [0.4])


def test_gripper_handlers_use_profile_values():
    published: list = []
    ctx = _n6_ctx(publisher=lambda chunk: published.extend(chunk))
    ctx.last_full_joints = list(_EDU6_HOME) + [0.3]
    motion.open_gripper(ctx, {})
    assert ctx.last_full_joints == list(_EDU6_HOME) + [1.75]
    motion.close_gripper(ctx, {})
    assert ctx.last_full_joints == list(_EDU6_HOME) + [0.0]
    assert ctx.last_commanded_close_rad == 0.0


def test_check_grasp_held_reads_index_n():
    ctx = types.SimpleNamespace(
        num_arm_joints=N6,
        get_follower_joints=lambda: list(_EDU6_HOME) + [0.9],
        last_commanded_close_rad=None,
    )
    # 7-wide readback: index 6 is the gripper. With the OMX default threshold
    # (env override unset → GRASP_HELD_MAX_RAD −0.35), 0.9 > −0.35 → held.
    import os
    os.environ.pop('EDUBOTICS_GRASP_HELD_MAX_RAD', None)
    prev = motion.GRASP_SETTLE_S
    motion.GRASP_SETTLE_S = 0.0
    try:
        assert motion.check_grasp_held(ctx) is True
        # a 6-wide readback on a 6-arm-joint rig is TOO SHORT → None
        ctx.get_follower_joints = lambda: list(_EDU6_HOME)
        assert motion.check_grasp_held(ctx) is None
    finally:
        motion.GRASP_SETTLE_S = prev


_B8 = [
    [0.00, 0.70, -2.40, 0.00, 0.70, 0.00, 1.75, 0.00],
    [0.05, 0.80, -2.20, 0.02, 0.80, 0.05, 1.50, 0.30],
    [0.10, 0.90, -2.00, 0.04, 0.90, 0.10, 1.20, 0.60],
]


def test_extract_points_8_wide_for_n6():
    rows = extract_points({'fps': 25, 'points': _B8}, num_arm_joints=N6)
    assert rows == _B8


def test_extract_points_exact_width_rail_both_directions():
    # 7-wide row on a 6-DOF rig → refuse (was: silent acceptance impossible,
    # short refusal existed); 8-wide row on a 5-DOF rig → refuse (was: silent
    # TRUNCATION — the §16.4 rail closes exactly this).
    with pytest.raises(WorkflowError):
        extract_points({'fps': 25, 'points': [[0.0] * 7, [0.0] * 7]},
                       num_arm_joints=N6)
    with pytest.raises(WorkflowError):
        extract_points({'fps': 25, 'points': [[0.0] * 8, [0.0] * 8]},
                       num_arm_joints=5)


def test_resegment_n6_full_stream_is_7_wide():
    out = resegment_trajectory(
        [list(r) for r in _B8], speed=1.0,
        lead_in_from=[0.0, 0.6, -2.5, 0.0, 0.6, 0.0, 1.75],
        num_arm_joints=N6, velocity_limit=N6_VLIMIT)
    assert out
    assert all(len(q) == 7 for q, _t in out)
    assert out[-1][0] == pytest.approx(_B8[-1][:7])

"""Batch 2b — the replay re-segment safety helper + the workflow replay handler.

``handlers/trajectory.py`` has no torch/ROS deps, so it imports cleanly in CI.
Covers the CRITICAL SAFETY property: the recorded timestamps are NEVER published
raw × speed — every consecutive pair is re-segmented through the quintic velocity
floor (build_segment), so a fast ``speed`` on a big joint jump is clamped.
"""

from __future__ import annotations

import pytest

from physical_ai_server.workflow.handlers.motion import WorkflowError
from physical_ai_server.workflow.handlers.trajectory import (
    REPLAY_SPEED_MAX,
    REPLAY_SPEED_MIN,
    clamp_speed,
    extract_points,
    replay_trajectory,
    resegment_trajectory,
)
from physical_ai_server.workflow.trajectory_builder import (
    JOINT_VELOCITY_LIMIT_RAD_S,
    _VELOCITY_SAFETY_FRACTION,
)


def _pt(j, t):
    """Contract-B point: 5 arm joints + gripper + time."""
    return [j, j, j, j, j, 0.0, t]


# --- clamp_speed -----------------------------------------------------------

def test_clamp_speed_bounds_and_defaults():
    assert clamp_speed(1.0) == 1.0
    assert clamp_speed(10.0) == REPLAY_SPEED_MAX
    assert clamp_speed(0.01) == REPLAY_SPEED_MIN
    assert clamp_speed(0.0) == 1.0
    assert clamp_speed(-2.0) == 1.0
    assert clamp_speed(float('nan')) == 1.0
    assert clamp_speed('abc') == 1.0
    assert clamp_speed(None) == 1.0


# --- extract_points validation --------------------------------------------

def test_extract_points_accepts_dict_and_list():
    pts = [_pt(0.0, 0.0), _pt(0.1, 0.5)]
    assert extract_points({'fps': 25, 'points': pts}) == pts
    assert extract_points(pts) == pts


def test_extract_points_rejects_short_or_corrupt():
    with pytest.raises(WorkflowError):
        extract_points({'fps': 25, 'points': [_pt(0.0, 0.0)]})  # < 2 points
    with pytest.raises(WorkflowError):
        extract_points({'points': [[0, 0, 0], [0, 0, 0]]})       # rows too short
    with pytest.raises(WorkflowError):
        extract_points({'points': [_pt(0.0, 0.0), [1, 2, 3, 4, 5, 6, float('nan')]]})
    with pytest.raises(WorkflowError):
        extract_points({'points': [_pt(0.0, 0.0), [1, 2, 'x', 4, 5, 6, 0.5]]})


# --- resegment: velocity floor is the real safety bound --------------------

def _peak_velocity(segmented):
    """Max |dq/dt| across all published waypoint transitions (rad/s)."""
    peak = 0.0
    prev = None
    for q, t in segmented:
        if prev is not None:
            pq, pt = prev
            dt = t - pt
            if dt > 1e-9:
                for a, b in zip(q, pq):
                    peak = max(peak, abs(a - b) / dt)
        prev = (q, t)
    return peak


def test_resegment_velocity_floor_clamps_fast_replay():
    # A big joint jump (2 rad) recorded over 0.2 s at ×3 speed WOULD be ~30 rad/s
    # raw — far past the 4.8 rad/s limit. The velocity floor must clamp it.
    points = [_pt(0.0, 0.0), _pt(2.0, 0.2)]
    segmented = resegment_trajectory(points, speed=3.0,
                                     lead_in_from=[0.0] * 6)
    v_safe = _VELOCITY_SAFETY_FRACTION * JOINT_VELOCITY_LIMIT_RAD_S
    # Peak quintic velocity of a segment ≈ |delta|·1.875/duration; the floor
    # keeps it at/under v_safe. Allow a small numerical margin.
    assert _peak_velocity(segmented) <= v_safe * 1.05


def test_resegment_times_monotonic():
    points = [_pt(0.0, 0.0), _pt(0.3, 0.5), _pt(0.6, 1.0), _pt(0.1, 1.4)]
    segmented = resegment_trajectory(points, speed=1.0, lead_in_from=[0.0] * 6)
    times = [t for _q, t in segmented]
    assert times == sorted(times)
    assert all(t > 0 for t in times)
    assert all(len(q) == 6 for q, _t in segmented)


def test_resegment_lead_in_prepends_from_current_pose():
    # With a lead-in the FIRST published joint vector must be near the current
    # pose, not the recording's start — so the arm is never jumped.
    points = [_pt(1.0, 0.0), _pt(1.2, 0.4)]
    current = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    segmented = resegment_trajectory(points, speed=1.0, lead_in_from=current)
    first_q = segmented[0][0]
    # The first quintic sample is very close to `current` (blend ~0 at s→0).
    assert all(abs(v) < 0.2 for v in first_q[:5])
    # The final published pose is the recording's last point.
    assert segmented[-1][0][:5] == pytest.approx([1.2] * 5)


def test_resegment_no_lead_in_seeds_first_point():
    points = [_pt(0.5, 0.0), _pt(0.7, 0.4)]
    segmented = resegment_trajectory(points, speed=1.0, lead_in_from=None)
    assert segmented[0][0][:5] == pytest.approx([0.5] * 5)


def test_resegment_lead_in_floor_check_refuses_dip():
    # If the synthetic lead-in dips the tool below the floor, refuse (don't drive
    # the tool through the table).
    points = [_pt(1.0, 0.0), _pt(1.2, 0.4)]
    with pytest.raises(WorkflowError) as e:
        resegment_trajectory(points, speed=1.0, lead_in_from=[0.0] * 6,
                             lead_in_floor_check=lambda q6: True)
    assert 'anheben' in str(e.value)


def test_resegment_lead_in_floor_check_passes_when_above():
    points = [_pt(1.0, 0.0), _pt(1.2, 0.4)]
    seg = resegment_trajectory(points, speed=1.0, lead_in_from=[0.0] * 6,
                               lead_in_floor_check=lambda q6: False)
    assert seg  # no refusal


def test_resegment_nonfinite_lead_in_falls_back_to_first_pose():
    # A NaN/Inf follower readback → the lead-in is dropped (never published as NaN)
    # and the stream seeds from the first recorded pose.
    points = [_pt(0.5, 0.0), _pt(0.7, 0.4)]
    seg = resegment_trajectory(points, speed=1.0, lead_in_from=[float('nan')] * 6)
    assert seg[0][0][:5] == pytest.approx([0.5] * 5)
    seg_inf = resegment_trajectory(points, speed=1.0,
                                   lead_in_from=[float('inf')] * 6)
    assert seg_inf[0][0][:5] == pytest.approx([0.5] * 5)


def test_resegment_point_floor_check_refuses_recorded_dip():
    # FIX 5 — a recorded waypoint that dips the tool below the table floor is
    # refused (a corrupt / out-of-frame recording), with its own German message
    # distinct from the lead-in „anheben" refusal.
    points = [_pt(1.0, 0.0), _pt(1.2, 0.4)]
    with pytest.raises(WorkflowError) as e:
        resegment_trajectory(points, speed=1.0, lead_in_from=[0.0] * 6,
                             point_floor_check=lambda q6: True)
    assert 'unter die Tischebene' in str(e.value)


def test_resegment_point_floor_check_none_unchanged():
    # No point_floor_check → recorded points are used exactly as before.
    points = [_pt(0.5, 0.0), _pt(0.7, 0.4)]
    seg = resegment_trajectory(points, speed=1.0, lead_in_from=[0.0] * 6)
    seg2 = resegment_trajectory(points, speed=1.0, lead_in_from=[0.0] * 6,
                                point_floor_check=lambda q6: False)
    assert seg and seg2  # both drive; a passing checker changes nothing
    assert [q for q, _t in seg] == [q for q, _t in seg2]


def test_resegment_point_floor_check_error_does_not_block():
    # A checker that raises must NOT block replay (treated as not-below), exactly
    # like the lead-in check.
    points = [_pt(0.5, 0.0), _pt(0.7, 0.4)]

    def _boom(_q6):
        raise RuntimeError('checker exploded')

    seg = resegment_trajectory(points, speed=1.0, lead_in_from=[0.0] * 6,
                               point_floor_check=_boom)
    assert seg  # drove despite the checker error


def test_resegment_coincident_timestamps_no_zero_duration():
    # Equal timestamps must not yield a zero/negative segment duration.
    points = [_pt(0.0, 1.0), _pt(0.2, 1.0)]
    segmented = resegment_trajectory(points, speed=1.0, lead_in_from=[0.0] * 6)
    times = [t for _q, t in segmented]
    assert all(b > a for a, b in zip(times, times[1:]))


# --- replay_trajectory handler --------------------------------------------

class _Ctx:
    def __init__(self, trajectories, last_full=None, stop=False):
        self.trajectories = trajectories
        self.last_full_joints = last_full if last_full is not None else [0.0] * 6
        self.last_arm_joints = None
        self.motion_lock = None
        self.published = []
        self._stop = stop

    def publisher(self, chunk):
        self.published.extend(chunk)

    def should_stop(self):
        return self._stop


def test_replay_handler_unknown_name_fails_loud():
    ctx = _Ctx({})
    with pytest.raises(WorkflowError) as e:
        replay_trajectory(ctx, {'name': 'X'})
    assert 'Unbekannte Aufnahme' in str(e.value)


def test_replay_handler_empty_name_fails_loud():
    with pytest.raises(WorkflowError):
        replay_trajectory(_Ctx({}), {'name': '   '})


def test_replay_handler_empty_recording_fails_loud():
    ctx = _Ctx({'T': {'fps': 25, 'points': [_pt(0.0, 0.0)]}})
    with pytest.raises(WorkflowError):
        replay_trajectory(ctx, {'name': 'T'})


def test_replay_handler_publishes_and_updates_pose():
    traj = {'fps': 25, 'points': [_pt(0.1, 0.0), _pt(0.2, 0.4), _pt(0.3, 0.8)]}
    ctx = _Ctx({'T': traj}, last_full=[0.0, -1.5, 1.5, 0.0, 0.0, 0.0])
    replay_trajectory(ctx, {'name': 'T', 'speed': 1.0})
    assert ctx.published, 'nothing published'
    # Chained pose = the recording's final point.
    assert ctx.last_full_joints[:5] == pytest.approx([0.3] * 5)
    assert ctx.last_arm_joints == pytest.approx([0.3] * 5)


def test_replay_handler_unseeded_pose_fails_loud():
    traj = {'fps': 25, 'points': [_pt(0.1, 0.0), _pt(0.2, 0.4)]}
    ctx = _Ctx({'T': traj}, last_full=[0.0] * 6)  # all-zero = unseeded sentinel
    with pytest.raises(WorkflowError) as e:
        replay_trajectory(ctx, {'name': 'T'})
    assert 'Armstellung' in str(e.value)


def test_replay_handler_stop_raises_stopped():
    traj = {'fps': 25, 'points': [_pt(0.1, 0.0), _pt(0.9, 1.0)]}
    ctx = _Ctx({'T': traj}, last_full=[0.05] * 6, stop=True)
    with pytest.raises(WorkflowError) as e:
        replay_trajectory(ctx, {'name': 'T'})
    assert 'gestoppt' in str(e.value)


def test_replay_handler_lead_in_below_floor_refuses():
    # The handler wires ctx.ik + the calibrated table height into the lead-in
    # floor check. With an absurdly-high table every lead-in tool-z is below the
    # floor, so the replay refuses (rather than driving the tool through it).
    from physical_ai_server.workflow.ik_solver import IKSolver
    traj = {'fps': 25, 'points': [_pt(0.1, 0.0), _pt(0.2, 0.4)]}
    ctx = _Ctx({'T': traj}, last_full=[0.0, -1.5, 1.5, 0.0, 0.0, 0.0])
    ctx.ik = IKSolver()
    ctx.z_table = 1.0        # table absurdly high → lead-in tool-z below the floor
    ctx.table_plane = None
    with pytest.raises(WorkflowError) as e:
        replay_trajectory(ctx, {'name': 'T'})
    assert 'anheben' in str(e.value)


def test_replay_handler_no_floor_when_uncalibrated():
    # No z_table / table_plane on the ctx → the lead-in floor check is a no-op and
    # the replay drives normally (backward-safe).
    from physical_ai_server.workflow.ik_solver import IKSolver
    traj = {'fps': 25, 'points': [_pt(0.1, 0.0), _pt(0.2, 0.4)]}
    ctx = _Ctx({'T': traj}, last_full=[0.0, -1.5, 1.5, 0.0, 0.0, 0.0])
    ctx.ik = IKSolver()      # ik present but no table height
    replay_trajectory(ctx, {'name': 'T'})
    assert ctx.published


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))

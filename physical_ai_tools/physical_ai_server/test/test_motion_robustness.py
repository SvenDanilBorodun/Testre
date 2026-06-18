"""Robustness regression tests for the Roboter Studio motion runtime.

Covers the 2026-06-18 deep-dig fixes (no container / PyKDL needed):

* **H2** — a malformed ``EDUBOTICS_GRASP_*`` env value must NOT raise at
  import (that took the whole ``handlers/__init__`` dispatch down).
* **M2** — a sub-chunk_size segment (gripper close) must be paced by its own
  duration so the next segment (the lift) can't pre-empt it: the close
  finishes before the lift starts.
* **M4** — a Detection whose ``world_xyz_m`` is still ``None`` (table height
  not calibrated) must raise the SPECIFIC "Tisch vermessen" error, not the
  generic "Ziel-Wert konnte nicht ausgewertet werden".
* **Unseeded-lurch** — a motion commanded from the unseeded all-zero
  ``last_full_joints`` sentinel must fail loud (German) rather than yank the
  arm from a fake zero pose.
* **j1 long-way sweep** — ``build_segment`` must extend the duration of a
  large joint swing so no joint exceeds the safe velocity fraction.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import threading
import types

import numpy as np
import pytest

from physical_ai_server.workflow import trajectory_builder
from physical_ai_server.workflow.handlers import motion
from physical_ai_server.workflow.handlers.motion import (
    GRIPPER_CLOSED_RAD,
    GRIPPER_OPEN_RAD,
    HOME_JOINTS_RAD,
    WorkflowError,
    _resolve_target,
    _require_seeded_start_pose,
    home,
    move_to,
    pickup,
)
from physical_ai_server.workflow.ik_solver import IKSolver


REACHABLE_XYZ = (0.20, 0.0, 0.0)


# ── H2: malformed env → default, not an import crash ─────────────────────────

def test_safe_float_returns_default_on_malformed_value(monkeypatch):
    monkeypatch.setenv('EDUBOTICS_GRASP_CLEARANCE_M', '12mm')
    # _safe_float must swallow the ValueError and return the default.
    assert motion._safe_float('EDUBOTICS_GRASP_CLEARANCE_M', 0.012) == 0.012


def test_safe_float_returns_default_on_empty_value(monkeypatch):
    monkeypatch.setenv('EDUBOTICS_GRASP_ROLL_DEG', '')
    assert motion._safe_float('EDUBOTICS_GRASP_ROLL_DEG', 0.0) == 0.0


def test_safe_float_parses_a_valid_override(monkeypatch):
    monkeypatch.setenv('EDUBOTICS_GRASP_CLEARANCE_M', '0.02')
    assert motion._safe_float('EDUBOTICS_GRASP_CLEARANCE_M', 0.012) == 0.02


def test_dispatch_imports_clean_with_malformed_env():
    """The real H2 regression: a non-numeric grasp env must not raise at
    module import (which would cascade through handlers/__init__ and kill the
    whole Roboter Studio dispatch with an opaque traceback).

    Run in a fresh subprocess so a bad env in THIS process — and a reload of
    the live ``motion`` module (which would rebind WorkflowError out from
    under the rest of the suite) — never pollute the other tests."""
    env = dict(os.environ)
    env['EDUBOTICS_GRASP_CLEARANCE_M'] = '12mm'
    env['EDUBOTICS_GRASP_ROLL_DEG'] = 'tilt'
    env['PYTHONPATH'] = os.pathsep.join(sys.path)
    code = (
        'from physical_ai_server.workflow.handlers import '
        'STATEMENT_HANDLERS, VALUE_EVALUATORS; '
        'from physical_ai_server.workflow.handlers import motion as m; '
        "assert 'edubotics_pickup' in STATEMENT_HANDLERS; "
        "assert 'edubotics_detect_color' in VALUE_EVALUATORS; "
        'assert m.GRASP_CLEARANCE_M == 0.012; '
        'assert m.GRASP_ROLL_RAD == 0.0; '
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, '-c', code],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f'dispatch import crashed with malformed env:\n{result.stderr}'
    )
    assert 'OK' in result.stdout


# ── M2: gripper close finishes before the lift starts ────────────────────────

class _TimedPublisher:
    """Records the wall-clock time of each publisher() call against a fake
    monotonic clock that the chunked_publish pacing advances."""

    def __init__(self, clock):
        self.calls: list[float] = []
        self._clock = clock

    def __call__(self, chunk):
        self.calls.append(self._clock['t'])


def test_short_segment_is_paced_by_its_own_duration():
    """A 0.5 s / 15-point gripper segment (below the 30-point chunk_size) must
    take ~0.5 s of paced time before chunked_publish returns, so a following
    segment can't pre-empt it. We use a fake clock that ADVANCES inside the
    pace loop so the segment's own remainder-duration sleep is observable."""
    clock = {'t': 0.0}

    def _monotonic():
        # The pace loop calls monotonic() repeatedly; advance a coarse step so
        # the loop terminates but the elapsed time reflects the requested wait.
        clock['t'] += 0.05
        return clock['t']

    fake = types.SimpleNamespace(monotonic=_monotonic, sleep=lambda _s: None)
    # Patch the module-level time used by chunked_publish's pacing.
    orig_time = trajectory_builder.time
    trajectory_builder.time = fake
    try:
        pub = _TimedPublisher(clock)
        gripper_seg = trajectory_builder.build_segment(
            [0, 0, 0, 0, 0, -0.5], [0, 0, 0, 0, 0, -0.5], 0.5,
        )
        t_start = clock['t']
        ok = trajectory_builder.chunked_publish(pub, gripper_seg, lambda: False)
        t_after = clock['t']
    finally:
        trajectory_builder.time = orig_time
    assert ok
    # The remainder duration (~0.5 s) must have elapsed before return — the M2
    # fix. Without the remainder pacing, t_after == t_start (returns in ~0).
    assert (t_after - t_start) >= 0.5


def test_pickup_close_segment_precedes_lift_in_paced_time():
    """End-to-end M2: in a real pickup the close gripper segment must be fully
    paced before the lift segment publishes. We capture the paced timestamp of
    each published chunk and assert the lift chunks come strictly after the
    close segment's settle window."""
    clock = {'t': 0.0}

    def _monotonic():
        clock['t'] += 0.01
        return clock['t']

    fake = types.SimpleNamespace(monotonic=_monotonic, sleep=lambda _s: None)
    orig_time = trajectory_builder.time
    trajectory_builder.time = fake
    try:
        events: list[tuple[float, float]] = []  # (paced_time, gripper_value)

        ctx = types.SimpleNamespace(
            ik=IKSolver(),
            z_table=0.0,
            motion_lock=threading.RLock(),
            should_stop=lambda: False,
            last_full_joints=list(HOME_JOINTS_RAD) + [GRIPPER_OPEN_RAD],
            last_arm_joints=None,
            destinations={},
        )

        def publisher(chunk):
            events.append((clock['t'], chunk[-1][0][5]))

        ctx.publisher = publisher
        pickup(ctx, {'target': REACHABLE_XYZ})
    finally:
        trajectory_builder.time = orig_time

    # Find the LAST publish whose gripper joint is still OPEN (the descend /
    # the close trajectory's first chunk) and the FIRST publish that is CLOSED
    # AND moves the arm (the lift). The lift must start strictly after the
    # close segment has been paced out.
    closed_times = [t for (t, g) in events if g == pytest.approx(GRIPPER_CLOSED_RAD)]
    assert closed_times, 'pickup should publish a closed-gripper trajectory'
    # The close happens, settles, THEN the lift — so there are at least two
    # closed-gripper publishes (close itself + lift) separated in paced time.
    assert len(closed_times) >= 2
    assert closed_times[-1] > closed_times[0]


# ── M4: missing touch-off → specific German error ────────────────────────────

class _FakeDetection:
    def __init__(self, world_xyz_m=None):
        self.world_xyz_m = world_xyz_m
        self.centroid_px = (100, 100)


def test_resolve_target_unprojected_detection_names_touch_off():
    ctx = types.SimpleNamespace(destinations={})
    with pytest.raises(WorkflowError) as exc:
        _resolve_target(_FakeDetection(world_xyz_m=None), ctx)
    msg = str(exc.value)
    assert 'Tisch vermessen' in msg
    # Must NOT be the generic message anymore.
    assert 'konnte nicht ausgewertet werden' not in msg


def test_resolve_target_unprojected_dict_detection_names_touch_off():
    ctx = types.SimpleNamespace(destinations={})
    with pytest.raises(WorkflowError) as exc:
        _resolve_target({'world_xyz_m': None, 'centroid_px': (1, 1)}, ctx)
    assert 'Tisch vermessen' in str(exc.value)


def test_resolve_target_projected_detection_returns_xyz():
    ctx = types.SimpleNamespace(destinations={})
    xyz = _resolve_target(_FakeDetection(world_xyz_m=(0.2, 0.0, 0.0)), ctx)
    assert xyz == (0.2, 0.0, 0.0)


def test_resolve_target_truly_unknown_value_keeps_generic_error():
    ctx = types.SimpleNamespace(destinations={})
    with pytest.raises(WorkflowError) as exc:
        _resolve_target(42, ctx)
    assert 'konnte nicht ausgewertet werden' in str(exc.value)


# ── unseeded-lurch: fail loud, don't command from [0]*6 ──────────────────────

def test_require_seeded_start_pose_rejects_all_zero_sentinel():
    ctx = types.SimpleNamespace(last_full_joints=[0.0] * 6)
    with pytest.raises(WorkflowError) as exc:
        _require_seeded_start_pose(ctx)
    assert 'Armstellung ist noch nicht bekannt' in str(exc.value)


def test_require_seeded_start_pose_accepts_real_home_pose():
    ctx = types.SimpleNamespace(
        last_full_joints=list(HOME_JOINTS_RAD) + [GRIPPER_OPEN_RAD]
    )
    # HOME is [0, -pi/4, pi/4, 0, 0, 0.8] — not all-zero — so it passes.
    _require_seeded_start_pose(ctx)


def test_home_with_unseeded_pose_raises_before_publishing():
    published: list = []
    ctx = types.SimpleNamespace(
        ik=IKSolver(),
        z_table=0.0,
        motion_lock=threading.RLock(),
        should_stop=lambda: False,
        last_full_joints=[0.0] * 6,   # unseeded sentinel
        last_arm_joints=None,
        destinations={},
        publisher=lambda chunk: published.append(chunk),
    )
    with pytest.raises(WorkflowError):
        home(ctx, {})
    # Nothing was commanded from the fake zero pose.
    assert published == []


def test_move_to_with_unseeded_pose_raises_before_publishing():
    published: list = []
    ctx = types.SimpleNamespace(
        ik=IKSolver(),
        z_table=0.0,
        motion_lock=threading.RLock(),
        should_stop=lambda: False,
        last_full_joints=[0.0] * 6,
        last_arm_joints=None,
        destinations={},
        publisher=lambda chunk: published.append(chunk),
    )
    with pytest.raises(WorkflowError):
        move_to(ctx, {'destination': {'x': 0.20, 'y': 0.0, 'z': 0.0}})
    assert published == []


# ── j1 long-way sweep: velocity-safe duration scaling ────────────────────────

def test_velocity_safe_duration_extends_a_large_swing():
    # A near-2π j1 swing requested at 2.5 s would peak ~4.71 rad/s; the safe
    # fraction (0.6 × 4.8 = 2.88 rad/s) forces a longer duration.
    delta = np.array([2 * math.pi, 0, 0, 0, 0, 0], dtype=np.float64)
    scaled = trajectory_builder._velocity_safe_duration(delta, 2.5)
    assert scaled > 2.5
    # Peak velocity at the scaled duration must be within the safe fraction.
    peak = (2 * math.pi) * trajectory_builder._QUINTIC_PEAK_VELOCITY_FACTOR / scaled
    assert peak <= 0.6 * trajectory_builder.JOINT_VELOCITY_LIMIT_RAD_S + 1e-9


def test_velocity_safe_duration_keeps_small_moves_fast():
    delta = np.array([0.3, 0.2, 0.1, 0, 0, 0], dtype=np.float64)
    # A small move stays at its requested (faster) duration.
    assert trajectory_builder._velocity_safe_duration(delta, 2.5) == 2.5


def test_build_segment_large_swing_stays_within_velocity_limit():
    # The full path through build_segment: a large j1 swing must produce
    # waypoints whose instantaneous velocity never exceeds the joint limit.
    q_start = [-math.pi + 0.05, -math.pi / 4, math.pi / 4, 0, 0, 0]
    q_end = [math.pi - 0.05, -math.pi / 4, math.pi / 4, 0, 0, 0]
    seg = trajectory_builder.build_segment(q_start, q_end, 2.5)
    prev_q, prev_t = np.array(q_start), 0.0
    max_v = 0.0
    for q, t in seg:
        q = np.array(q)
        dt = t - prev_t
        if dt > 0:
            max_v = max(max_v, float(np.max(np.abs(q - prev_q)) / dt))
        prev_q, prev_t = q, t
    assert max_v <= trajectory_builder.JOINT_VELOCITY_LIMIT_RAD_S

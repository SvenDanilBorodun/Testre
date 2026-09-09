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
* **HIGH-5** — an outer-ring target whose GRASP is reachable but whose
  +approach pose falls outside the shrinking annulus must NOT be refused; the
  approach height is clamped down to the reachable envelope.
* **home-carries-gripper** — ``home`` must keep the current gripper state so a
  ``pickup`` (closed) followed by ``home`` does not drop the held object.
* **L1 tilted floor** — with a calibrated ``table_plane`` the workspace-floor
  refusal must follow the plane z at the target (x, y), not the scalar
  ``z_table`` (else a low corner is falsely refused).
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import threading
import time
import types

import numpy as np
import pytest

from physical_ai_server.workflow import trajectory_builder
from physical_ai_server.workflow.handlers import motion
from physical_ai_server.workflow.handlers.motion import (
    DEFAULT_APPROACH_HEIGHT_M,
    GRASP_CLEARANCE_M,
    GRIPPER_CLOSED_RAD,
    GRIPPER_OPEN_RAD,
    HOME_JOINTS_RAD,
    WORKSPACE_FLOOR_MARGIN_M,
    WorkflowError,
    _resolve_target,
    _require_seeded_start_pose,
    _solve_grasp_and_approach,
    _solve_or_raise,
    drop_at,
    home,
    move_to,
    pickup,
)
from physical_ai_server.workflow.ik_solver import IKSolver


REACHABLE_XYZ = (0.20, 0.0, 0.0)
# Outer-ring target: at z=0 the grasp (z+clearance≈0.012) is reachable but the
# +DEFAULT_APPROACH_HEIGHT_M (0.06) approach pose is NOT (the annulus shrinks
# with height). Rig/solver-verified false-refusal window is ~0.260–0.268 m.
OUTER_RING_XYZ = (0.262, 0.0, 0.0)


class _RecordingCtx:
    """SimpleNamespace-style ctx that captures published chunks and log lines,
    carrying exactly the fields the motion handlers read. ``table_plane``
    defaults to None (flat z_table floor)."""

    def __init__(self, z_table=0.0, table_plane=None):
        self.published: list = []
        self.logs: list[str] = []
        self.ik = IKSolver()
        self.z_table = z_table
        self.table_plane = table_plane
        self.motion_lock = threading.RLock()
        self.should_stop = lambda: False
        self.last_full_joints = list(HOME_JOINTS_RAD) + [GRIPPER_OPEN_RAD]
        self.last_arm_joints = None
        self.destinations: dict = {}

    def publisher(self, chunk):
        self.published.append(list(chunk))

    def log(self, msg):
        self.logs.append(msg)

    @property
    def last_commanded_joints(self):
        assert self.published, 'no motion was published'
        return self.published[-1][-1][0]


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
        'import math; '
        'from physical_ai_server.workflow.handlers import '
        'STATEMENT_HANDLERS, VALUE_EVALUATORS; '
        'from physical_ai_server.workflow.handlers import motion as m; '
        "assert 'edubotics_pickup' in STATEMENT_HANDLERS; "
        "assert 'edubotics_see_object' in VALUE_EVALUATORS; "
        'assert m.GRASP_CLEARANCE_M == 0.012; '
        # malformed EDUBOTICS_GRASP_ROLL_DEG falls back to the 90.0 default
        'assert abs(m.GRASP_ROLL_RAD - math.radians(90.0)) < 1e-9; '
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


def _calibrated_ctx():
    # Camera calibrated (intrinsics/extrinsics/board height present) but the
    # touch-off z_table is still missing → the touch-off step is named (#1).
    return types.SimpleNamespace(
        destinations={}, scene_intrinsics={'K': 1}, scene_extrinsics=1,
        board_table_z=0.0, z_table=None)


def test_resolve_target_unprojected_detection_names_touch_off():
    ctx = _calibrated_ctx()
    with pytest.raises(WorkflowError) as exc:
        _resolve_target(_FakeDetection(world_xyz_m=None), ctx)
    msg = str(exc.value)
    assert 'Tisch vermessen' in msg
    # Must NOT be the generic message anymore.
    assert 'konnte nicht ausgewertet werden' not in msg


def test_resolve_target_unprojected_dict_detection_names_touch_off():
    ctx = _calibrated_ctx()
    with pytest.raises(WorkflowError) as exc:
        _resolve_target({'world_xyz_m': None, 'centroid_px': (1, 1)}, ctx)
    assert 'Tisch vermessen' in str(exc.value)


def test_resolve_target_uncalibrated_camera_names_calibration():
    # #1: when the scene camera is NOT calibrated (no intrinsics), the message
    # points to the camera calibration, not the table measurement.
    ctx = types.SimpleNamespace(destinations={})
    with pytest.raises(WorkflowError) as exc:
        _resolve_target(_FakeDetection(world_xyz_m=None), ctx)
    msg = str(exc.value)
    assert 'kalibriert' in msg and 'Kamera' in msg
    assert 'Tisch vermessen' not in msg


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
    # HOME is [0, -pi/2, pi/2, 0, 0, 0.8] — not all-zero — so it passes.
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


# ── HIGH-5: outer-ring approach-height clamp, no false refusal ────────────────

@pytest.fixture
def _fast_chunk_pacing(monkeypatch):
    """Replace ``trajectory_builder.time`` with a fake clock so the inter-chunk
    pacing sleep doesn't slow these end-to-end pickup/drop tests. monotonic
    jumps a large step each call so the pace while-loop exits at once; sleep is
    a no-op. Published WAYPOINTS are byte-identical to production."""
    state = {'t': 0.0}

    def _monotonic():
        state['t'] += 1000.0
        return state['t']

    fake = types.SimpleNamespace(monotonic=_monotonic, sleep=lambda _s: None)
    monkeypatch.setattr(trajectory_builder, 'time', fake)
    yield


def test_outer_ring_grasp_reachable_but_approach_not_is_the_setup():
    """Guard the test's premise: at OUTER_RING_XYZ the grasp solves but the full
    +DEFAULT_APPROACH_HEIGHT_M approach does NOT. If the solver geometry changes
    so this window closes, this assertion flags it (the HIGH-5 tests below would
    otherwise pass vacuously)."""
    ik = IKSolver()
    gx, gy, gz = OUTER_RING_XYZ[0], OUTER_RING_XYZ[1], OUTER_RING_XYZ[2] + GRASP_CLEARANCE_M
    assert ik.solve((gx, gy, gz)) is not None, 'grasp should be reachable'
    assert ik.solve((gx, gy, gz + DEFAULT_APPROACH_HEIGHT_M)) is None, \
        'full approach should be unreachable (annulus shrinks with height)'


def test_solve_grasp_and_approach_clamps_at_outer_ring():
    """The clamp itself is unchanged; the WARNING became conditional (F6).

    This assertion used to be „a reduction was logged", which is the contract the
    2026-07-26 audit retired: on edu6 the requested 60 mm hover is unreachable at
    EVERY radius, so warn-on-any-reduction fired on 100 % of grasps (936 warnings
    over 780 swept runs) and taught students to ignore the log strip. The clamp is
    now reported only when the achieved clearance drops below
    ``_APPROACH_WARN_FRAC`` (0.25) of the request — for the shipped catalogs that
    is 15 mm, i.e. exactly ``object_height_m − grasp_depth_m``, the clearance at
    which the fingertips stop being above the object's top.

    OUTER_RING_XYZ is measured (this run, OMX, grasp z = 0.012) at **35.6 mm** of
    achieved clearance out of the 60 mm asked for — a real clamp, still comfortably
    above the cube, so it is now SILENT. The second half drives a radius where the
    clearance really does collapse and proves the warning still fires there."""
    ctx = _RecordingCtx(z_table=0.0)
    grasp_xyz = (OUTER_RING_XYZ[0], OUTER_RING_XYZ[1], OUTER_RING_XYZ[2] + GRASP_CLEARANCE_M)
    grasp_q, approach_q = _solve_grasp_and_approach(
        ctx, grasp_xyz, DEFAULT_APPROACH_HEIGHT_M, roll=0.0)
    # Both solutions are valid 5-joint vectors — the grasp was NOT refused.
    assert grasp_q is not None and len(grasp_q) == 5
    assert approach_q is not None and len(approach_q) == 5
    # The clamp HAPPENED (this is the HIGH-5 behaviour, unchanged)...
    ik = IKSolver()
    achieved = float(ik.fk(approach_q)[1][2]) - grasp_xyz[2]
    assert 0.0 < achieved < DEFAULT_APPROACH_HEIGHT_M, 'the approach must be clamped'
    assert achieved == pytest.approx(0.0356, abs=0.0005), (
        'measured 35.6 mm — if the solver geometry moved, re-derive the F6 threshold')
    # ...and it is NOT reported, because 35.6 mm still clears the 30 mm cube.
    assert not [m for m in ctx.logs if 'Anfahrhöhe' in m], (
        'a non-consequential clamp must stay silent (F6)')

    # Further out the clearance collapses to 7.5 mm — below the cube's own
    # 15 mm top clearance — and THAT is worth telling the student about.
    deep = _RecordingCtx(z_table=0.0)
    deep_xyz = (0.268, 0.0, GRASP_CLEARANCE_M)
    _g, deep_q = _solve_grasp_and_approach(
        deep, deep_xyz, DEFAULT_APPROACH_HEIGHT_M, roll=0.0)
    deep_achieved = float(ik.fk(deep_q)[1][2]) - deep_xyz[2]
    assert deep_achieved == pytest.approx(0.0075, abs=0.0005)
    warned = [m for m in deep.logs if 'Anfahrhöhe' in m]
    assert warned, 'a consequential clamp MUST be reported'
    # The reason must be the TRUE one (the arm's height over that point), never
    # the old „Ziel liegt am Rand des Greifbereichs" — which was flatly wrong at
    # mid-band radii on edu6.
    assert 'höher kommt der Arm' in warned[0]
    assert 'Rand des Greifbereichs' not in warned[0]


def test_pickup_outer_ring_does_not_false_refuse(_fast_chunk_pacing):
    """The flagship HIGH-5 regression: a graspable outer-ring object must be
    picked up, not refused with 'Arbeitsbereich'."""
    ctx = _RecordingCtx(z_table=0.0)
    pickup(ctx, {'target': OUTER_RING_XYZ})
    # End state: gripper closed (object held), motion published.
    assert ctx.last_commanded_joints[5] == pytest.approx(GRIPPER_CLOSED_RAD)
    assert ctx.published


def test_drop_at_outer_ring_does_not_false_refuse(_fast_chunk_pacing, monkeypatch):
    # The drop RELEASE height (DROP_HEIGHT_M, 5 cm) deliberately shrinks the
    # reachable radius — at OUTER_RING_XYZ a 5 cm-high drop is genuinely out of
    # reach (documented trade-off: pin a high-release drop spot closer to the
    # base). This test isolates the approach-CLAMPING logic (HIGH-5: a reachable
    # drop must not be false-refused because only its +approach pose fell outside
    # the annulus), so pin the low clearance height here.
    monkeypatch.setattr(motion, 'DROP_HEIGHT_M', GRASP_CLEARANCE_M)
    ctx = _RecordingCtx(z_table=0.0)
    ctx.last_full_joints = list(HOME_JOINTS_RAD) + [GRIPPER_CLOSED_RAD]
    drop_at(ctx, {'destination': OUTER_RING_XYZ})
    assert ctx.last_commanded_joints[5] == pytest.approx(GRIPPER_OPEN_RAD)
    assert ctx.published


def test_drop_at_outer_ring_reduces_the_release_height_instead_of_refusing(
        _fast_chunk_pacing):
    """DELIBERATELY REVERSED 2026-07-26 (was
    ``test_drop_at_high_release_refuses_outer_ring``, which asserted a refusal and
    called it "correctly refused").

    It is not correct. The release clearance is OPTIONAL — it exists to clear a
    container rim — while the hard requirement is placing the object AT the
    target. That is exactly the asymmetry HIGH-5 fixed for the pickup approach,
    and ``_solve_grasp_and_approach``'s own docstring already says the old code
    "refused the whole pickup/DROP even though the object was graspable"; the
    drop POINT was the residual that fix never reached.

    Measured at ``OUTER_RING_XYZ`` (r = 0.2732 m from the joint-1 axis): the
    target itself IS reachable, target + 50 mm is not, and the bisect lands on
    **48.4 mm**. So the old behaviour refused an entire place to avoid losing
    **1.6 mm** of rim clearance. On edu6 the same defect was far worse — it cut
    „lege ab bei (Position von X)" down to ~13 % of the pick band, because
    `object_position` returns the GRASP height and the release stacked 50 mm on
    top, landing 0.5 mm under that arm's absolute ceiling.

    Now: it places, and says so in German."""
    ctx = _RecordingCtx(z_table=0.0)
    ctx.last_full_joints = list(HOME_JOINTS_RAD) + [GRIPPER_CLOSED_RAD]
    drop_at(ctx, {'destination': OUTER_RING_XYZ})
    assert ctx.published, 'the place must actually run'
    # the object is released (gripper ends OPEN) at the outer ring
    assert ctx.last_commanded_joints[5] == pytest.approx(GRIPPER_OPEN_RAD)
    # …and it does NOT nag about it. The bisect lands on 48.4 of the requested
    # 50 mm here — 96.8 % — and a rim clearance that came back at 48 instead of
    # 50 mm still clears every container. The warning used to fire on ANY
    # reduction: measured 2026-09-07, 38 of 120 successful edu6 places (31.7 %)
    # carried it. It is now gated at the same _APPROACH_WARN_FRAC the approach
    # hover uses, so it fires when the clearance is genuinely gone, not when it
    # is 1.6 mm short. (Deliberately reversed from the original assertion; see
    # the class of defect _APPROACH_WARN_FRAC itself was derived to fix.)
    assert not [m for m in ctx.logs if 'Ablegehöhe' in m], (
        f'a 1.6 mm reduction must not be reported — logs were {ctx.logs}')


def test_drop_at_reports_a_release_height_that_is_actually_gone(monkeypatch):
    """The other side of the gate: when the achieved rim clearance really is a
    small fraction of the request, the student IS told, in German, with mm."""
    ctx = _RecordingCtx(z_table=0.0)
    ctx.last_full_joints = list(HOME_JOINTS_RAD) + [GRIPPER_CLOSED_RAD]
    # Ask for a rim clearance far beyond this arm's ceiling so the bisect can
    # only return a small fraction of it.
    monkeypatch.setattr(motion, 'DROP_HEIGHT_M', 0.30)
    drop_at(ctx, {'destination': OUTER_RING_XYZ})
    warn = [m for m in ctx.logs if 'Ablegehöhe' in m]
    assert warn, f'a consequential reduction must be reported — logs {ctx.logs}'
    assert 'mm' in warn[0]


def test_drop_at_truly_unreachable_target_still_refuses():
    """The clamp must only rescue a reachable TARGET. The invariant the reversed
    test above must not weaken: a destination whose own point is outside the
    annulus still raises the German 'Arbeitsbereich' (the drop mirror of
    ``test_pickup_truly_unreachable_grasp_still_refuses``)."""
    ctx = _RecordingCtx(z_table=0.0)
    ctx.last_full_joints = list(HOME_JOINTS_RAD) + [GRIPPER_CLOSED_RAD]
    with pytest.raises(WorkflowError) as exc:
        drop_at(ctx, {'destination': (0.50, 0.0, 0.0)})
    assert 'Arbeitsbereich' in str(exc.value)


def test_pickup_truly_unreachable_grasp_still_refuses():
    """The clamp must only rescue a reachable GRASP. A target whose grasp is
    itself outside the annulus must still raise 'Arbeitsbereich'."""
    ctx = _RecordingCtx(z_table=0.0)
    with pytest.raises(WorkflowError) as exc:
        pickup(ctx, {'target': (0.50, 0.0, 0.0)})
    assert 'Arbeitsbereich' in str(exc.value)


# ── home carries the held gripper state (don't drop the object) ──────────────

def test_home_carries_closed_gripper(_fast_chunk_pacing):
    """pickup (closed) → home must keep the gripper CLOSED so the held object
    stays held. home used to hardcode GRIPPER_OPEN_RAD and drop it."""
    ctx = _RecordingCtx(z_table=0.0)
    # Simulate the post-pickup state: holding an object (gripper closed).
    ctx.last_full_joints = list(HOME_JOINTS_RAD) + [GRIPPER_CLOSED_RAD]
    home(ctx, {})
    assert ctx.last_full_joints[5] == pytest.approx(GRIPPER_CLOSED_RAD)
    assert ctx.last_commanded_joints[5] == pytest.approx(GRIPPER_CLOSED_RAD)


def test_home_carries_open_gripper(_fast_chunk_pacing):
    """The empty-hand case still homes with the gripper open (unchanged)."""
    ctx = _RecordingCtx(z_table=0.0)
    ctx.last_full_joints = [0.1, 0.2, 0.3, 0.0, 0.0, GRIPPER_OPEN_RAD]
    home(ctx, {})
    assert ctx.last_full_joints[5] == pytest.approx(GRIPPER_OPEN_RAD)


def test_pickup_then_home_keeps_object_held(_fast_chunk_pacing):
    """End-to-end flagship-tutorial path: pickup → home, gripper stays closed."""
    ctx = _RecordingCtx(z_table=0.0)
    pickup(ctx, {'target': REACHABLE_XYZ})
    assert ctx.last_full_joints[5] == pytest.approx(GRIPPER_CLOSED_RAD)
    home(ctx, {})
    assert ctx.last_full_joints[5] == pytest.approx(GRIPPER_CLOSED_RAD)
    assert ctx.last_commanded_joints[5] == pytest.approx(GRIPPER_CLOSED_RAD)


# ── L1: tilted table_plane floor follows the plane at (x, y) ──────────────────

def test_solve_or_raise_tilted_plane_allows_low_corner():
    """With a tilted table_plane, a target at a LOW corner (below the scalar
    z_table but ON the plane) must NOT be refused as 'Tischebene'."""
    # Plane tilts down along +x: z = -0.1·x + 0.0 → at x=0.20, plane z = -0.02.
    # The scalar z_table is 0.0, so the old scalar floor would refuse a target
    # at z=-0.02 (below 0.0 - margin). With the plane it is exactly on-surface.
    ctx = _RecordingCtx(z_table=0.0, table_plane=(-0.1, 0.0, 0.0))
    target = (0.20, 0.0, -0.02)        # on the tilted plane at x=0.20
    solution = _solve_or_raise(ctx, target)
    assert solution is not None and len(solution) == 5


def test_solve_or_raise_tilted_plane_still_refuses_below_plane():
    """The floor is still enforced — a target BELOW the tilted plane at its
    (x, y) is refused."""
    ctx = _RecordingCtx(z_table=0.0, table_plane=(-0.1, 0.0, 0.0))
    # plane z at x=0.20 is -0.02; go well below it.
    below = (0.20, 0.0, -0.02 - (WORKSPACE_FLOOR_MARGIN_M + 0.01))
    with pytest.raises(WorkflowError) as exc:
        _solve_or_raise(ctx, below)
    assert 'Tischebene' in str(exc.value)


def test_floor_z_at_uses_plane_when_present():
    ctx = _RecordingCtx(z_table=0.0, table_plane=(0.1, -0.2, 0.05))
    # z = 0.1·x − 0.2·y + 0.05
    assert motion._floor_z_at(ctx, 0.2, 0.1) == pytest.approx(0.1 * 0.2 - 0.2 * 0.1 + 0.05)


def test_floor_z_at_falls_back_to_scalar_without_plane():
    ctx = _RecordingCtx(z_table=0.03, table_plane=None)
    assert motion._floor_z_at(ctx, 0.2, 0.1) == pytest.approx(0.03)


# ═════════════════════════════════════════════════════════════════════════════
# G6 — the motion lock. RS-18 (bounded, stop-aware acquire), RS-27 („warte N
# Sekunden" must not pin the lock), and the two defects found while verifying
# them: a `finally` re-acquire that could return WITHOUT the lock, and an
# unclamped EDUBOTICS_GRASP_SETTLE_S sleeping under the lock with no stop poll.
#
# Every number below was re-measured 2026-09-08 (the harness drove the real
# WorkflowManager with real threads); the comments say which claims reproduced
# and which did not.
# ═════════════════════════════════════════════════════════════════════════════


class _Holder:
    """A thread that takes ctx.motion_lock and keeps it until released."""

    def __init__(self, ctx, hold_s=None):
        self._ctx = ctx
        self._hold_s = hold_s
        self.taken = threading.Event()
        self._let_go = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        with self._ctx.motion_lock:
            self.taken.set()
            if self._hold_s is None:
                self._let_go.wait(30.0)
            else:
                time.sleep(self._hold_s)

    def __enter__(self):
        self._t.start()
        assert self.taken.wait(5.0), 'the holder never got the lock'
        return self

    def __exit__(self, *_exc):
        self._let_go.set()
        self._t.join(30.0)
        return False


def test_the_motion_lock_notice_is_exactly_ten_seconds_and_there_is_no_bound():
    """Pin the SHIPPED VALUES with literals, not with the symbols themselves.

    Every other test here passes an explicit short notice, so the module
    constants are invisible to them: mutation-tested 2026-09-08, setting the
    old bound to 0.0 and to 1e9 both left the rest of the suite green.

    The second assertion is the policy: there is NO bound any more (owner
    decision 2026-09-09 — wait, warn once, never raise), so a
    ``MOTION_LOCK_TIMEOUT_S`` coming back is a silent reversal of it. Waiting
    for the arm is not an error; the only thing left is a NOTICE."""
    assert motion.MOTION_LOCK_NOTICE_S == 10.0
    assert motion._MOTION_LOCK_POLL_S == 0.05
    assert not hasattr(motion, 'MOTION_LOCK_TIMEOUT_S'), (
        'the bound is back — a queued motion must WAIT, not die at N seconds')


def test_the_acquire_waits_for_a_stop_and_never_for_a_bound():
    """It answers Stop, and nothing else ends the wait. Before the policy
    change this test proved the default ARGUMENT was not 0; now it proves there
    is no deadline at all — a 1 s Stop is what returns, not a timeout."""
    ctx = _RecordingCtx()
    stop = {'v': False}
    # Bound BEFORE the call: _hold_motion_lock snapshots ctx.should_stop
    # once, which is right (in production it is a stable bound
    # _stop_event.is_set), so the flag has to be the mutable part.
    ctx.should_stop = lambda: stop['v']
    with _Holder(ctx):
        t0 = time.monotonic()
        raised = None
        stop_after = threading.Timer(1.0, lambda: stop.__setitem__('v', True))
        stop_after.start()
        try:
            motion._hold_motion_lock(ctx)
        except WorkflowError as e:
            raised = str(e)
        finally:
            stop_after.cancel()
        waited = time.monotonic() - t0
    # It waited for the Stop (1 s), not for a 0-second default.
    assert raised is not None and 'gestoppt' in raised, (
        f'something other than the 1 s Stop ended the wait: {raised!r}')
    assert waited > 0.9, (
        f'the wait collapsed to ~{waited:.2f} s — a deadline is back')


def test_the_notice_constant_is_read_at_call_time_not_frozen_at_def_time():
    """It was ``timeout_s: float = MOTION_LOCK_TIMEOUT_S``, and Python binds a
    default ARGUMENT once, when the ``def`` executes. Rebinding the module
    constant therefore did nothing while looking like it worked: an experiment
    that set it to 3.0 and expected a 3 s failure measured 10.6 s (2026-09-08).
    The bound is gone but the NOTICE inherited the shape, so it inherited the
    trap; this is what stops it coming back."""
    ctx = _RecordingCtx()
    prev = motion.MOTION_LOCK_NOTICE_S
    motion.MOTION_LOCK_NOTICE_S = 0.1
    try:
        with _Holder(ctx, hold_s=0.6):
            t0 = time.monotonic()
            acq = motion._hold_motion_lock(ctx)
            waited = time.monotonic() - t0
            motion._release_motion_lock(ctx, acq)
    finally:
        motion.MOTION_LOCK_NOTICE_S = prev
    assert waited > 0.3, 'the holder was not actually holding'
    warn = [m for m in ctx.logs if 'gleichzeitig' in m]
    assert len(warn) == 1, (
        f'the 0.1 s notice never fired across a {waited:.2f} s wait — the '
        f'constant is frozen into the default argument. logs={ctx.logs}')


def test_the_acquire_is_not_bounded_it_waits_and_warns_once():
    """THE policy, inverted from what this test used to assert.

    It used to prove the acquire RAISED „…gleichzeitig bewegen…" past a bound.
    Owner decision 2026-09-09: a queued motion is not an error. The wait now
    ends only when the arm is free (or Stop is pressed), and the student gets
    ONE German [WARNUNG] that says it will carry on — 3 waits, 1 line, not one
    line per 50 ms poll."""
    ctx = _RecordingCtx()
    with _Holder(ctx, hold_s=0.9):
        t0 = time.monotonic()
        acq = motion._hold_motion_lock(ctx, notice_s=0.1)
        waited = time.monotonic() - t0
        motion._release_motion_lock(ctx, acq)
    assert acq is True, 'the acquire gave up instead of waiting'
    assert waited > 0.5, (
        f'returned after {waited:.2f} s of a 0.9 s hold — it did not wait')
    warn = [m for m in ctx.logs if 'gleichzeitig' in m]
    assert len(warn) == 1, f'exactly ONE [WARNUNG], got {ctx.logs}'
    assert warn[0].startswith('[WARNUNG]')
    assert 'es geht weiter' in warn[0], (
        'with no bound, silence reads as a hang — the warning must promise it '
        f'carries on: {warn[0]!r}')
    # The old remedy was measurably wrong: a restart reproduces it identically.
    assert 'neu starten' not in warn[0]


def test_the_acquire_polls_stop_while_it_queues():
    """RS-18's second claim, re-measured: with another thread holding for 4.0 s
    and Stop pressed 0.2 s in, a plain lock.acquire() answered after 3.80 s and
    the sliced acquire answers in 0.00 s. (The finding said 4.04 s; same event,
    measured from t=0 rather than from the Stop.)"""
    ctx = _RecordingCtx()
    with _Holder(ctx):
        ctx.should_stop = lambda: True
        t0 = time.monotonic()
        with pytest.raises(WorkflowError) as exc:
            motion._hold_motion_lock(ctx)
        waited = time.monotonic() - t0
    assert 'gestoppt' in str(exc.value)
    assert waited < 0.5, (
        f'Stop waited {waited:.2f} s behind the lock — the stop poll is gone')


def test_the_acquire_is_sliced_not_one_long_wait():
    """The stop poll only works because the wait is CUT INTO SLICES. A single
    `lock.acquire(timeout=...)` would check should_stop exactly once, at the
    top, and then be deaf for the whole wait — which, with no bound, is
    forever."""
    ctx = _RecordingCtx()
    polls = {'n': 0}
    stop = {'v': False}

    def counting_stop():
        polls['n'] += 1
        return stop['v']

    ctx.should_stop = counting_stop
    with _Holder(ctx):
        stopper = threading.Timer(0.4, lambda: stop.__setitem__('v', True))
        stopper.start()
        try:
            with pytest.raises(WorkflowError):
                motion._hold_motion_lock(ctx)
        finally:
            stopper.cancel()
    assert polls['n'] >= 4, (
        f'should_stop was consulted {polls["n"]}× across a 0.4 s wait at a '
        f'{motion._MOTION_LOCK_POLL_S}s slice — the wait is not sliced')


# ── RS-27: „warte N Sekunden" must not pin the lock ──────────────────────────

def test_wait_seconds_lets_another_thread_move_while_it_waits():
    """Re-measured 2026-09-08 end to end: a hat running „warte 2 s" under the
    lock delayed the main stack's next move to 2.00 s against a 0.20 s
    no-hat baseline — 1.80 s of starvation, against the finding's claimed
    1.89 s. With the release in place: 0.21 s, i.e. gone."""
    ctx = _RecordingCtx()
    got_in_at = {}

    def other_motion_thread():
        t0 = time.monotonic()
        with ctx.motion_lock:
            got_in_at['t'] = time.monotonic() - t0

    with ctx.motion_lock:                     # the hat handler's `with`
        t = threading.Thread(target=other_motion_thread)
        t.start()
        time.sleep(0.05)                      # let it queue
        motion.wait_seconds(ctx, {'seconds': 0.6})
        t.join(5.0)
    assert 't' in got_in_at, 'the other motion thread never got the lock'
    assert got_in_at['t'] < 0.4, (
        f'the other thread waited {got_in_at["t"]:.2f} s of a 0.6 s „warte" — '
        'the lock was held for the wait')


def test_wait_seconds_from_the_main_stack_never_touches_the_lock():
    """The main stack does not hold motion_lock, so the release is a no-op and
    the finally must not try to re-take it (which would leave the main stack
    holding a lock nobody releases)."""
    ctx = _RecordingCtx()
    motion.wait_seconds(ctx, {'seconds': 0.05})
    free = ctx.motion_lock.acquire(blocking=False)
    if free:
        ctx.motion_lock.release()
    assert free, 'wait_seconds left the motion lock held on the main stack'


def test_wait_seconds_always_returns_holding_the_lock_it_released():
    """THE defect the RS-27 fix introduced, found by running it.

    The `finally` re-acquired with a BOUND and RAISED a German error when it
    could not. Measured 2026-09-08 against the real ``_run_hat_handler`` shape
    with another motion thread holding past the bound: the German error was
    caught by that function's inner `except (WorkflowError, InterpreterError)`
    exactly as designed — and then the surrounding `with ctx.motion_lock`
    __exit__ raised ``RuntimeError: cannot release un-acquired lock``, which
    lands in its OUTER bare `except Exception: return`. The hat is silent for
    the rest of the run, with no message, and the run still reports green:
    RS-16's failure mode through a new door.

    (The raise's own comment said it existed "so the caller's `with
    motion_lock` __exit__ always has something to release". On that branch it
    has nothing.)"""
    ctx = _RecordingCtx()
    escaped = []
    inner = []

    def hat_handler():
        try:
            with ctx.motion_lock:                    # _run_hat_handler's `with`
                try:
                    ready.set()
                    motion.wait_seconds(ctx, {'seconds': 0.2})
                except Exception as e:               # noqa: BLE001 — inner arm
                    inner.append(f'{type(e).__name__}: {e}')
        except Exception as e:                       # noqa: BLE001 — outer arm
            escaped.append(f'{type(e).__name__}: {e}')

    ready = threading.Event()
    # Another motion thread that grabs the lock the moment „warte" drops it and
    # keeps it far longer than the re-acquire notice.
    prev = motion.MOTION_LOCK_NOTICE_S
    motion.MOTION_LOCK_NOTICE_S = 0.1
    try:
        h = threading.Thread(target=hat_handler)
        h.start()
        assert ready.wait(5.0)
        time.sleep(0.05)
        with _Holder(ctx, hold_s=0.9):
            h.join(20.0)
    finally:
        motion.MOTION_LOCK_NOTICE_S = prev
    assert not h.is_alive()
    assert escaped == [], (
        f'the hat handler died on {escaped} — wait_seconds returned without '
        'the lock its caller is about to release')
    assert inner == [], f'wait_seconds raised out of its own finally: {inner}'


def test_a_slow_reacquire_is_reported_in_German_and_then_waits():
    """It is not silent, and it does not give up. One [WARNUNG], then the
    student's program carries on when the arm is free again."""
    ctx = _RecordingCtx()
    prev = motion.MOTION_LOCK_NOTICE_S
    motion.MOTION_LOCK_NOTICE_S = 0.1
    ready = threading.Event()
    done = threading.Event()

    def hat_handler():
        with ctx.motion_lock:
            ready.set()
            motion.wait_seconds(ctx, {'seconds': 0.2})
        done.set()

    try:
        h = threading.Thread(target=hat_handler)
        h.start()
        assert ready.wait(5.0)
        time.sleep(0.05)
        with _Holder(ctx, hold_s=0.8):
            assert done.wait(20.0), 'the re-acquire never completed'
    finally:
        motion.MOTION_LOCK_NOTICE_S = prev
        h.join(5.0)
    warn = [m for m in ctx.logs if 'gleichzeitig' in m]
    assert warn, f'no German [WARNUNG] for the slow re-acquire — logs {ctx.logs}'
    assert warn[0].startswith('[WARNUNG]')
    assert len(warn) == 1, f'the warning must be emitted ONCE, got {warn}'
    assert 'es geht weiter' in warn[0]


# ── EDUBOTICS_GRASP_SETTLE_S: a sleep held under the lock ────────────────────

def test_the_shipped_grasp_settle_is_three_tenths_of_a_second():
    """Pins the shipped VALUE and the ceiling. Every existing test monkeypatches
    GRASP_SETTLE_S to 0.0, so neither was covered by anything."""
    assert motion.GRASP_SETTLE_S == 0.3
    assert motion.GRASP_SETTLE_MAX_S == 2.0


@pytest.mark.parametrize('raw,expected', [
    (0.3, 0.3),
    (0.0, 0.0),
    (-1.0, 0.0),           # negative silently disabled the settle
    (10.0, 2.0),           # the deterministic park named by the zombie guard
    (float('inf'), 2.0),   # reached time.sleep(inf) → OverflowError → traceback
    (float('nan'), 0.0),
    ('x', 0.0),
])
def test_grasp_settle_is_folded_into_the_band(raw, expected):
    assert motion._clamped_settle_s(raw) == pytest.approx(expected)


def test_the_env_value_actually_goes_THROUGH_the_clamp():
    """The two tests above are both true of an UNCLAMPED module as well.

    Mutation-tested 2026-09-08: reverting the assignment to a bare
    ``GRASP_SETTLE_S = _safe_float('EDUBOTICS_GRASP_SETTLE_S', 0.3)`` SURVIVED
    the whole suite, because with the env unset the clamp is the identity on
    0.3 and ``_clamped_settle_s`` is still exported and still correct. Only an
    out-of-band env value in a FRESH interpreter can tell the two apart.

    Subprocess, not monkeypatch+reload, for the reason
    ``test_dispatch_imports_clean_with_malformed_env`` already documents: a
    reload of the live ``motion`` module rebinds ``WorkflowError`` out from
    under the rest of the suite."""
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join(sys.path)
    code = (
        'from physical_ai_server.workflow.handlers import motion as m; '
        'import os; '
        'v = os.environ["EDUBOTICS_GRASP_SETTLE_S"]; '
        'print(f"{v}->{m.GRASP_SETTLE_S}")'
    )
    seen = {}
    for raw in ('10', 'inf', '-1', '0.25', 'nonsense'):
        env['EDUBOTICS_GRASP_SETTLE_S'] = raw
        out = subprocess.run([sys.executable, '-c', code], env=env,
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr
        seen[raw] = float(out.stdout.strip().split('->')[1])
    assert seen['10'] == 2.0, (
        f'EDUBOTICS_GRASP_SETTLE_S=10 reached the module as {seen["10"]} — '
        'the env value does not pass through _clamped_settle_s')
    assert seen['inf'] == 2.0
    assert seen['-1'] == 0.0
    assert seen['0.25'] == 0.25       # an in-band rig tuning still works
    assert seen['nonsense'] == 0.3    # _safe_float's default, then the clamp


def test_the_settle_sleep_honours_stop():
    """``workflow_manager.start``'s zombie-guard comment ranks
    EDUBOTICS_GRASP_SETTLE_S FIRST among the ways to park a thread past
    ``stop()``'s joins (5.0 s main / 2.0 s hat), because it landed in a raw
    ``time.sleep`` with no stop poll — and every caller of check_grasp_held
    holds ctx.motion_lock while it sleeps. Measured 2026-09-08 with the value
    at 10: Stop answered after 10.00 s; sliced, 0.23 s.

    The ceiling alone is not enough (2 s still outlasts nothing but is 4× the
    default), and the slicing alone is not enough (the ceiling is what bounds
    how long the arm is OWNED while the run is not stopping)."""
    ctx = _RecordingCtx()
    ctx.get_follower_joints = lambda: list(HOME_JOINTS_RAD) + [0.4]
    ctx.last_commanded_close_rad = None
    prev = motion.GRASP_SETTLE_S
    motion.GRASP_SETTLE_S = 5.0          # as if the clamp had let it through
    stop = {'v': False}
    ctx.should_stop = lambda: stop['v']
    try:
        threading.Timer(0.2, lambda: stop.__setitem__('v', True)).start()
        t0 = time.monotonic()
        motion.check_grasp_held(ctx)
        waited = time.monotonic() - t0
    finally:
        motion.GRASP_SETTLE_S = prev
    assert waited < 1.0, (
        f'the settle slept {waited:.2f} s through a Stop — it is not sliced')


def test_the_settle_still_settles_when_nothing_is_stopping():
    """…and the slicing did not turn the settle into a no-op: the servo still
    gets its full GRASP_SETTLE_S before the readback."""
    ctx = _RecordingCtx()
    ctx.get_follower_joints = lambda: list(HOME_JOINTS_RAD) + [0.4]
    ctx.last_commanded_close_rad = None
    prev = motion.GRASP_SETTLE_S
    motion.GRASP_SETTLE_S = 0.4
    try:
        t0 = time.monotonic()
        motion.check_grasp_held(ctx)
        waited = time.monotonic() - t0
    finally:
        motion.GRASP_SETTLE_S = prev
    assert 0.35 <= waited <= 1.2, f'the settle was skipped ({waited:.2f} s)'


# ── the whole discipline, under contention ──────────────────────────────────

def test_the_lock_discipline_survives_contention():
    """Deadlock / imbalance / total-starvation guard for the shapes that
    actually coexist at runtime: a hat holding the lock for its whole body, a
    hat that RELEASES and re-takes it (RS-27's path), the composite motions'
    nested acquire (RLock re-entry), and the main stack's bounded acquire.

    Asserts the three properties a lock discipline has to have: every thread
    returns, the lock is FREE at the end (every acquire balanced), and nothing
    escaped — in particular no `RuntimeError: cannot release un-acquired
    lock`.

    DELIBERATELY NOT ASSERTED: that the RELEASING waiter (`hat_wait`) makes
    progress. CPython's lock lets a thread that releases and immediately
    re-acquires barge ahead of one that has been queued, and measured over six
    2 s runs the never-releasing hat body took 136–241 turns while the waiting
    hat took 2–14. That asymmetry is the price of the RS-27 release and it is
    real; asserting a margin that thin would be a flaky test rather than a
    property. The deterministic half — that the release ALWAYS gets its lock
    back — is
    ``test_a_slow_reacquire_is_reported_in_German_and_then_waits``."""
    ctx = _RecordingCtx()
    errors: list[str] = []
    progress = {'hat_body': 0, 'hat_wait': 0, 'main': 0, 'composite': 0}
    deadline = time.monotonic() + 2.0

    def loop(name, body):
        while time.monotonic() < deadline:
            try:
                body()
                progress[name] += 1
            except WorkflowError:
                pass
            except Exception as e:              # noqa: BLE001
                errors.append(f'{name}: {type(e).__name__}: {e}')
                return

    def hat_body():
        with ctx.motion_lock:
            time.sleep(0.005)

    def hat_wait():
        with ctx.motion_lock:
            motion.wait_seconds(ctx, {'seconds': 0.02})

    def main_stack():
        acq = motion._hold_motion_lock(ctx, notice_s=1.0)
        try:
            time.sleep(0.005)
        finally:
            motion._release_motion_lock(ctx, acq)

    def composite():
        outer = motion._hold_motion_lock(ctx, notice_s=1.0)
        try:
            inner = motion._hold_motion_lock(ctx, notice_s=1.0)
            motion._release_motion_lock(ctx, inner)
        finally:
            motion._release_motion_lock(ctx, outer)

    threads = [threading.Thread(target=loop, args=(n, f))
               for n, f in (('hat_body', hat_body), ('hat_wait', hat_wait),
                            ('main', main_stack), ('composite', composite))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30.0)

    assert not [t for t in threads if t.is_alive()], 'a thread deadlocked'
    assert errors == [], errors
    free = ctx.motion_lock.acquire(blocking=False)
    if free:
        ctx.motion_lock.release()
    assert free, 'the motion lock was left held — an acquire went unreleased'
    assert all(progress[k] > 0 for k in ('hat_body', 'main', 'composite')), (
        f'a non-releasing waiter made no progress at all: {progress}')

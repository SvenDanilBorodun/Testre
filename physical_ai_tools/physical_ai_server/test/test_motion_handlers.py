"""Motion-handler tests for the Roboter Studio workflow runtime.

These exercise the highest-risk untested path: ``motion.pickup`` /
``drop_at`` / ``move_to`` driving a REAL closed-form ``IKSolver`` (pure
NumPy, no container / PyKDL) through ``build_segment`` +
``chunked_publish`` into a capturing publisher. We assert the END-STATE
gripper position, the number of published motion segments, the
workspace-floor refusal ("Tischebene"), and the unreachable refusal
("Arbeitsbereich") — all in German per Rule §1.

``chunked_publish`` paces real motion with a 1 s inter-chunk sleep so the
ros2 controllers can consume each chunk. That wall-clock pacing is
irrelevant to these logic tests, so ``trajectory_builder.time`` is
replaced with a fake monotonic clock (no-op sleep, fast-forwarding
monotonic) — the published WAYPOINTS are byte-identical to production;
only the wait between chunks is skipped.
"""

from __future__ import annotations

import math
import threading
import types

import pytest

from physical_ai_server.workflow import path_guard
from physical_ai_server.workflow import trajectory_builder
from physical_ai_server.workflow.handlers import motion
from physical_ai_server.workflow.handlers.motion import (
    GRASP_ROLL_RAD,
    GRIPPER_CLOSED_RAD,
    GRIPPER_OPEN_RAD,
    HOME_JOINTS_RAD,
    WORKSPACE_FLOOR_MARGIN_M,
    GraspSkip,
    WorkflowError,
    close_on_object,
    compute_grasp_roll,
    descend_to,
    drop_at,
    lift,
    move_above,
    move_to,
    pickup,
    _execute_pickup,
    _solve_or_raise,
)
from physical_ai_server.workflow.ik_solver import IKSolver
from physical_ai_server.workflow.tag_pose import grasp_joint5


# A reachable strict-vertical grasp point on/near the table plane (well
# inside the ~0.10–0.28 m annulus). Same point family the IK round-trip
# uses. z_table defaults to 0.0 so the +clearance / +approach offsets
# land at positive, reachable heights.
REACHABLE_XYZ = (0.20, 0.0, 0.0)
# Far outside the 2R reach span — IK returns None.
UNREACHABLE_XYZ = (0.50, 0.0, 0.0)


@pytest.fixture(autouse=True)
def _fast_chunk_pacing(monkeypatch):
    """Replace ``trajectory_builder.time`` with a fake clock so the 1 s
    inter-chunk sleep doesn't make the suite slow. ``monotonic`` jumps a
    large step each call so the inter-chunk ``while monotonic() <
    sleep_target`` loop exits on its first re-check; ``sleep`` is a
    no-op."""
    state = {'t': 0.0}

    def _monotonic():
        state['t'] += 1000.0
        return state['t']

    fake = types.SimpleNamespace(monotonic=_monotonic, sleep=lambda _s: None)
    monkeypatch.setattr(trajectory_builder, 'time', fake)
    yield


class _StubCtx:
    """Minimal WorkflowContext stand-in carrying exactly the fields the
    motion handlers read."""

    def __init__(self, ik, z_table=0.0):
        self.published: list[list[tuple[list[float], float]]] = []
        self.ik = ik
        self.z_table = z_table
        self.motion_lock = threading.RLock()
        self.should_stop = lambda: False
        self.last_full_joints = list(HOME_JOINTS_RAD) + [GRIPPER_OPEN_RAD]
        self.last_arm_joints = None
        self.destinations: dict = {}
        self.logs: list[str] = []

    def publisher(self, chunk):
        # chunked_publish hands us one chunk (list of (q, t) waypoints)
        # per call. Record the full chunk so tests can inspect the final
        # commanded joint vector and count published segments.
        self.published.append(list(chunk))

    def log(self, msg):
        self.logs.append(msg)

    @property
    def last_commanded_joints(self):
        """The last joint vector actually published to the arm."""
        assert self.published, 'no motion was published'
        return self.published[-1][-1][0]


def _ctx(z_table=0.0):
    return _StubCtx(IKSolver(), z_table=z_table)


# ── pickup ───────────────────────────────────────────────────────────────────

def test_pickup_ends_gripper_closed_with_motion_published():
    ctx = _ctx()
    pickup(ctx, {'target': REACHABLE_XYZ})
    # The last commanded gripper joint (index 5) must be CLOSED — the
    # object is held at the end of a pickup.
    assert ctx.last_commanded_joints[5] == pytest.approx(GRIPPER_CLOSED_RAD)
    # The ctx end-state mirrors it.
    assert ctx.last_full_joints[5] == pytest.approx(GRIPPER_CLOSED_RAD)
    # The close path RECORDS the commanded close for the per-object grasp-held
    # threshold (motion._held_threshold_rad).
    assert ctx.last_commanded_close_rad == pytest.approx(GRIPPER_CLOSED_RAD)


def test_pickup_publishes_at_least_five_segments():
    ctx = _ctx()
    pickup(ctx, {'target': REACHABLE_XYZ})
    # pickup issues 5 distinct _publish_motion segments (open, above,
    # descend, close, lift); each emits >= 1 chunk, so the publisher is
    # called at least 5 times.
    assert len(ctx.published) >= 5


def test_pickup_unreachable_target_raises_arbeitsbereich():
    ctx = _ctx()
    with pytest.raises(WorkflowError) as exc:
        pickup(ctx, {'target': UNREACHABLE_XYZ})
    assert 'Arbeitsbereich' in str(exc.value)


def test_close_on_object_clamps_corrupt_gripper_rad_high():
    # A corrupt catalog gripper_close_rad far ABOVE the open limit must be CLAMPED
    # to GRIPPER_OPEN_RAD — a huge value otherwise stretches the velocity-floored
    # close to tens of seconds (a wedged run) and commands an unreachable angle.
    ctx = _ctx()
    ziel = types.SimpleNamespace(extras={'gripper_close_rad': 100.0})
    close_on_object(ctx, {'ziel': ziel})
    assert ctx.last_commanded_joints[5] == pytest.approx(GRIPPER_OPEN_RAD)
    assert ctx.last_full_joints[5] == pytest.approx(GRIPPER_OPEN_RAD)


def test_close_on_object_clamps_corrupt_gripper_rad_low():
    ctx = _ctx()
    ziel = types.SimpleNamespace(extras={'gripper_close_rad': -100.0})
    close_on_object(ctx, {'ziel': ziel})
    assert ctx.last_commanded_joints[5] == pytest.approx(GRIPPER_CLOSED_RAD)


def test_close_on_object_valid_rad_unchanged():
    # An in-band catalog value passes through unclamped.
    ctx = _ctx()
    ziel = types.SimpleNamespace(extras={'gripper_close_rad': -0.25})
    close_on_object(ctx, {'ziel': ziel})
    assert ctx.last_commanded_joints[5] == pytest.approx(-0.25)


# ── named-object grasp primitives (live roll, no double-add, per-object close) ─

def test_grasp_roll_default_is_90_degrees():
    # The geometrically-correct top-down jaw constant (fleet default).
    assert GRASP_ROLL_RAD == pytest.approx(math.radians(90.0))


def test_compute_grasp_roll_matches_formula():
    ctx = _ctx()
    x, y, tag_yaw = 0.20, 0.05, 0.3
    roll = compute_grasp_roll(ctx, x, y, tag_yaw)
    expected = grasp_joint5(ctx.ik.base_yaw(x, y), tag_yaw, GRASP_ROLL_RAD)
    assert roll == pytest.approx(expected)


def _closed_arm_configs(ctx):
    """Every published waypoint's arm vector whose gripper is CLOSED."""
    out = []
    for chunk in ctx.published:
        for q, _t in chunk:
            if q[5] == pytest.approx(GRIPPER_CLOSED_RAD):
                out.append(q[:5])
    return out


def test_execute_pickup_descends_to_exact_grasp_z_no_double_add():
    # _execute_pickup must descend to EXACTLY grasp_xyz[2] — NOT
    # grasp_xyz[2] + GRASP_CLEARANCE_M (the §23.7 double-add). Distinguish by
    # comparing the published grasp arm config against the exact-z IK solve vs
    # the +clearance IK solve (they differ).
    ctx = _ctx()
    gx, gy, gz, roll = 0.20, 0.0, 0.03, 0.4
    _execute_pickup(ctx, (gx, gy, gz), 0.06, roll, GRIPPER_CLOSED_RAD)
    exact = ctx.ik.solve((gx, gy, gz), roll=roll)
    plus_clear = ctx.ik.solve((gx, gy, gz + motion.GRASP_CLEARANCE_M), roll=roll)
    assert exact is not None and plus_clear is not None
    arms = _closed_arm_configs(ctx)

    def _matches(arm, ref):
        return all(a == pytest.approx(b, abs=1e-9) for a, b in zip(arm, ref))

    assert any(_matches(arm, exact) for arm in arms), 'grasp not at exact z'
    assert not any(_matches(arm, plus_clear) for arm in arms), 'double-add detected'


def test_execute_pickup_uses_given_close_rad_and_roll():
    ctx = _ctx()
    _execute_pickup(ctx, (0.20, 0.0, 0.03), 0.06, 0.4, -0.25)
    # final gripper is the per-object close angle, not the global GRIPPER_CLOSED_RAD
    assert ctx.last_commanded_joints[5] == pytest.approx(-0.25)
    # joint5 of the descend config reflects the requested roll
    exact = ctx.ik.solve((0.20, 0.0, 0.03), roll=0.4)
    assert exact is not None and exact[4] == pytest.approx(0.4)


# ── drop_at ──────────────────────────────────────────────────────────────────

def test_drop_at_ends_gripper_open():
    ctx = _ctx()
    # Start holding an object: gripper closed at HOME.
    ctx.last_full_joints = list(HOME_JOINTS_RAD) + [GRIPPER_CLOSED_RAD]
    drop_at(ctx, {'destination': REACHABLE_XYZ})
    # After a drop the gripper is OPEN (object released).
    assert ctx.last_commanded_joints[5] == pytest.approx(GRIPPER_OPEN_RAD)
    assert ctx.last_full_joints[5] == pytest.approx(GRIPPER_OPEN_RAD)


# ── move_to ──────────────────────────────────────────────────────────────────

def test_move_to_reachable_destination_dict():
    ctx = _ctx()
    move_to(ctx, {'destination': {'x': 0.20, 'y': 0.0, 'z': 0.0}})
    assert ctx.published, 'move_to should publish a motion'
    # The arm joints are updated to the solved config; the gripper joint
    # is carried over unchanged (move_to doesn't open/close).
    assert ctx.last_arm_joints is not None
    assert len(ctx.last_arm_joints) == 5


def test_move_to_unreachable_destination_raises_arbeitsbereich():
    ctx = _ctx()
    with pytest.raises(WorkflowError) as exc:
        move_to(ctx, {'destination': {'x': 0.50, 'y': 0.0, 'z': 0.0}})
    assert 'Arbeitsbereich' in str(exc.value)


# ── workspace floor (_solve_or_raise) ────────────────────────────────────────

def test_solve_or_raise_below_table_plane_raises_tischebene():
    ctx = _ctx(z_table=0.0)
    below = (0.20, 0.0, -(WORKSPACE_FLOOR_MARGIN_M + 0.01))
    with pytest.raises(WorkflowError) as exc:
        _solve_or_raise(ctx, below)
    assert 'Tischebene' in str(exc.value)


def test_solve_or_raise_at_floor_margin_does_not_raise_tischebene():
    # Exactly at z_table - margin is allowed (the refusal is strictly
    # below the floor). A reachable point at that height solves.
    ctx = _ctx(z_table=0.0)
    at_floor = (0.20, 0.0, -WORKSPACE_FLOOR_MARGIN_M)
    solution = _solve_or_raise(ctx, at_floor)
    assert solution is not None and len(solution) == 5


def test_solve_or_raise_reachable_returns_five_joints():
    ctx = _ctx()
    solution = _solve_or_raise(ctx, REACHABLE_XYZ)
    assert solution is not None and len(solution) == 5


# ── grasp-split motion primitives (Phase 1) ──────────────────────────────────
# The split blocks act on a Greifziel (a Detection-shaped value) and use the
# ORIENTED roll from extras['tag_yaw'] (not the fixed GRASP_ROLL_RAD).

def _greifziel(xyz=(0.20, 0.0, 0.03), tag_yaw=0.3, close=-0.25, approach=0.06,
               aruco_id=7):
    return types.SimpleNamespace(
        world_xyz_m=xyz,
        aruco_id=aruco_id,
        extras={'tag_yaw': tag_yaw, 'gripper_close_rad': close,
                'approach_clear_m': approach},
    )


def test_move_above_uses_oriented_roll_and_hovers():
    ctx = _ctx()
    move_above(ctx, {'ziel': _greifziel()})
    assert ctx.published, 'move_above should publish a motion'
    expected_roll = compute_grasp_roll(ctx, 0.20, 0.0, 0.3)
    # j5 reflects the ORIENTED roll, not the fixed GRASP_ROLL_RAD.
    assert ctx.last_arm_joints[4] == pytest.approx(expected_roll, abs=1e-6)
    # gripper carried unchanged (was open).
    assert ctx.last_full_joints[5] == pytest.approx(GRIPPER_OPEN_RAD)
    # hovered at grasp z + approach.
    ref = ctx.ik.solve((0.20, 0.0, 0.03 + 0.06), roll=expected_roll)
    assert ref is not None
    assert all(a == pytest.approx(b, abs=1e-9)
               for a, b in zip(ctx.last_arm_joints, ref))


def test_descend_to_reaches_grasp_z_with_oriented_roll():
    ctx = _ctx()
    descend_to(ctx, {'ziel': _greifziel()})
    expected_roll = compute_grasp_roll(ctx, 0.20, 0.0, 0.3)
    ref = ctx.ik.solve((0.20, 0.0, 0.03), roll=expected_roll)
    assert ref is not None
    assert all(a == pytest.approx(b, abs=1e-9)
               for a, b in zip(ctx.last_arm_joints, ref))


def test_close_on_object_uses_tuned_close_angle():
    ctx = _ctx()
    close_on_object(ctx, {'ziel': _greifziel(close=-0.25)})
    assert ctx.last_commanded_joints[5] == pytest.approx(-0.25)
    assert ctx.last_full_joints[5] == pytest.approx(-0.25)
    # The tuned close is RECORDED for the per-object grasp-held threshold.
    assert ctx.last_commanded_close_rad == pytest.approx(-0.25)


def test_close_gripper_records_commanded_close():
    # The plain „Greifer schließen" block is a close path too — it must record
    # the commanded close for the per-object grasp-held threshold.
    ctx = _ctx()
    motion.close_gripper(ctx, {})
    assert ctx.last_full_joints[5] == pytest.approx(GRIPPER_CLOSED_RAD)
    assert ctx.last_commanded_close_rad == pytest.approx(GRIPPER_CLOSED_RAD)


def test_open_gripper_does_not_record_a_close():
    # Opening is NOT a close path — it must leave the record untouched.
    ctx = _ctx()
    motion.close_gripper(ctx, {})
    motion.open_gripper(ctx, {})
    assert ctx.last_commanded_close_rad == pytest.approx(GRIPPER_CLOSED_RAD)


def test_close_on_object_falls_back_to_full_close():
    ctx = _ctx()
    g = types.SimpleNamespace(world_xyz_m=(0.20, 0.0, 0.03), aruco_id=7,
                              extras={'tag_yaw': 0.3})  # no gripper_close_rad
    close_on_object(ctx, {'ziel': g})
    assert ctx.last_commanded_joints[5] == pytest.approx(GRIPPER_CLOSED_RAD)


def test_lift_preserves_gripper_and_wrist_roll():
    ctx = _ctx()
    arm = ctx.ik.solve((0.20, 0.0, 0.05), roll=0.4)
    assert arm is not None
    ctx.last_full_joints = list(arm) + [GRIPPER_CLOSED_RAD]
    ctx.last_arm_joints = list(arm)
    lift(ctx, {})
    # gripper carried (a held object stays held).
    assert ctx.last_commanded_joints[5] == pytest.approx(GRIPPER_CLOSED_RAD)
    # wrist roll j5 preserved (don't twist a held object).
    assert ctx.last_arm_joints[4] == pytest.approx(arm[4])
    # EE rose by ~the approach height.
    _r0, t0 = ctx.ik.fk(arm)
    _r1, t1 = ctx.ik.fk(ctx.last_arm_joints)
    assert 0.05 < float(t1[2] - t0[2]) < 0.07


def test_split_block_missing_greifziel_raises():
    ctx = _ctx()
    with pytest.raises(WorkflowError) as exc:
        move_above(ctx, {'ziel': None})
    assert 'Greifziel' in str(exc.value)


def test_split_block_unknown_orientation_raises_graspskip():
    ctx = _ctx()
    with pytest.raises(GraspSkip):
        move_above(ctx, {'ziel': _greifziel(tag_yaw=None)})


# ── no-go zones (Phase 4): safe_move routing / descend exemption ──────────────
# safe_move is the TRANSIT wrapper; it reroutes around no-go zones and is a
# no-op (raw _publish_motion) when ctx has no zones (backward-safe — covered by
# every test above, none of which set ctx.zones).

def test_safe_move_no_zones_is_direct(monkeypatch):
    # Zone-less ctx → exactly ONE _publish_motion (raw direct), like today.
    calls = []
    orig = motion._publish_motion
    monkeypatch.setattr(motion, '_publish_motion',
                        lambda c, a, b, d: (calls.append((a, b, d)), orig(c, a, b, d))[1])
    ctx = _ctx()  # no .zones attribute
    qs = ctx.last_full_joints
    qe = list(ctx.ik.solve((0.20, 0.0, 0.05), roll=GRASP_ROLL_RAD)) + [qs[5]]
    motion.safe_move(ctx, qs, qe, motion.DEFAULT_MOVE_DURATION_S)
    assert len(calls) == 1
    assert calls[0][1] == pytest.approx(qe)


def test_safe_move_direct_when_zone_clear(monkeypatch):
    calls = []
    orig = motion._publish_motion
    monkeypatch.setattr(motion, '_publish_motion',
                        lambda c, a, b, d: (calls.append((a, b, d)), orig(c, a, b, d))[1])
    ctx = _ctx()
    ctx.zones = [{'min': [-0.05, 0.30, 0.0], 'max': [0.05, 0.40, 0.10]}]  # off-path
    qs = ctx.last_full_joints
    qe = list(ctx.ik.solve((0.20, 0.0, 0.05), roll=GRASP_ROLL_RAD)) + [qs[5]]
    motion.safe_move(ctx, qs, qe, motion.DEFAULT_MOVE_DURATION_S)
    assert len(calls) == 1                      # direct — the zone is nowhere near


def test_safe_move_reroutes_blocked_transit(monkeypatch):
    calls = []
    orig = motion._publish_motion
    monkeypatch.setattr(motion, '_publish_motion',
                        lambda c, a, b, d: (calls.append((a, b, d)), orig(c, a, b, d))[1])
    ctx = _ctx()
    # a low wall straddling the straight lateral move from -y to +y
    ctx.zones = [{'min': [0.06, -0.03, 0.0], 'max': [0.22, 0.03, 0.05]}]
    qs = list(ctx.ik.solve((0.14, -0.12, 0.03), roll=GRASP_ROLL_RAD)) + [GRIPPER_OPEN_RAD]
    qe = list(ctx.ik.solve((0.14, 0.12, 0.03), roll=GRASP_ROLL_RAD)) + [GRIPPER_OPEN_RAD]
    ctx.last_full_joints = qs
    motion.safe_move(ctx, qs, qe, motion.DEFAULT_MOVE_DURATION_S)
    assert len(calls) >= 2                      # rerouted into multiple legs
    # the final commanded pose is exactly the requested destination
    assert calls[-1][1] == pytest.approx(qe)
    # a German reroute notice was logged
    assert any('Sperrzone' in m for m in ctx.logs)


def test_safe_move_refuses_when_no_route():
    ctx = _ctx()
    # a giant tall box over the whole workspace → no safe route
    ctx.zones = [{'min': [-0.5, -0.5, 0.0], 'max': [0.5, 0.5, 0.5]}]
    qs = list(ctx.ik.solve((0.14, -0.12, 0.03), roll=GRASP_ROLL_RAD)) + [GRIPPER_OPEN_RAD]
    qe = list(ctx.ik.solve((0.14, 0.12, 0.03), roll=GRASP_ROLL_RAD)) + [GRIPPER_OPEN_RAD]
    ctx.last_full_joints = qs
    with pytest.raises(WorkflowError) as exc:
        motion.safe_move(ctx, qs, qe, motion.DEFAULT_MOVE_DURATION_S)
    assert 'Sperrzone' in str(exc.value)


def test_descend_to_is_exempt_from_zone_check(monkeypatch):
    # descend_to uses RAW _publish_motion — the grasp corridor is NEVER
    # zone-checked, even when the target sits inside a zone. It must publish
    # one segment and never reroute/refuse.
    calls = []
    orig = motion._publish_motion
    monkeypatch.setattr(motion, '_publish_motion',
                        lambda c, a, b, d: (calls.append((a, b, d)), orig(c, a, b, d))[1])
    ctx = _ctx()
    # a zone sitting right on top of the grasp point
    ctx.zones = [{'min': [0.10, -0.10, 0.0], 'max': [0.30, 0.10, 0.10]}]
    descend_to(ctx, {'ziel': _greifziel(xyz=(0.20, 0.0, 0.03), tag_yaw=0.0)})
    assert len(calls) == 1                      # one raw descend, no reroute
    assert not any('Sperrzone' in m for m in ctx.logs)


def test_pickup_descend_exempt_but_approach_reroutes(monkeypatch):
    # In a full pickup, the APPROACH/lift-out transit reroutes around a zone
    # while the descend/grasp corridor stays raw → the pickup still completes
    # (gripper ends CLOSED) instead of refusing because a zone hugs the object.
    ctx = _ctx()
    ctx.zones = [{'min': [0.06, -0.03, 0.0], 'max': [0.22, 0.03, 0.05]}]
    # object on the +y side; the arm starts on the -y side (so the approach
    # transit crosses the wall) but the descend onto the object is exempt.
    ctx.last_full_joints = list(ctx.ik.solve((0.14, -0.12, 0.06),
                                             roll=GRASP_ROLL_RAD)) + [GRIPPER_OPEN_RAD]
    pickup(ctx, {'target': (0.14, 0.12, 0.0)})
    assert ctx.last_commanded_joints[5] == pytest.approx(GRIPPER_CLOSED_RAD)


# ── FIX 1: home / observation-retreat are zone-routed whole-arm transits ──────
# home + go_to_observation_pose now go through safe_move (like move_to / lift),
# so a no-go zone on the path is rerouted instead of swept straight through. With
# no zones safe_move is a pass-through → exactly one direct publish (no
# regression). A blocking zone derived from the actual transit path proves the
# reroute fires.

def _wrap_publish(monkeypatch):
    """Wrap motion._publish_motion to record every (start, end, dur) leg while
    still executing the real publish, returning the call log."""
    calls = []
    orig = motion._publish_motion
    monkeypatch.setattr(
        motion, '_publish_motion',
        lambda c, a, b, d: (calls.append((a, b, d)), orig(c, a, b, d))[1])
    return calls


def test_home_no_zones_single_direct_publish(monkeypatch):
    # No zones → safe_move is a pass-through → exactly one _publish_motion call.
    calls = _wrap_publish(monkeypatch)
    ctx = _ctx()  # no .zones attribute
    # Start at a real forward pose (not HOME) so home actually moves the arm.
    ctx.last_full_joints = list(
        ctx.ik.solve((0.20, 0.0, 0.05), roll=GRASP_ROLL_RAD)) + [GRIPPER_OPEN_RAD]
    motion.home(ctx, {})
    assert len(calls) == 1                       # direct, no reroute
    assert calls[-1][1][:5] == pytest.approx(list(HOME_JOINTS_RAD))
    assert ctx.last_arm_joints == pytest.approx(list(HOME_JOINTS_RAD))


# A start pose + low no-go wall (base-frame metres) whose DIRECT joint-space
# transit to HOME sweeps the wall (via a lower link), but the arm can route
# around it — verified reachable + reroutable against the real IKSolver. HOME's
# EE sits high + central, so a transit-blocking wall that is also reroutable is
# geometrically narrow; this pair is fixed rather than derived so the test is
# deterministic.
_REROUTE_START_XYZ = (0.22, -0.14, 0.03)
_REROUTE_WALL = [{'min': [0.10, -0.05, 0.0], 'max': [0.24, 0.01, 0.06]}]


def test_home_reroutes_around_blocking_zone(monkeypatch):
    calls = _wrap_publish(monkeypatch)
    ctx = _ctx()
    qs = list(ctx.ik.solve(_REROUTE_START_XYZ,
                           roll=GRASP_ROLL_RAD)) + [GRIPPER_OPEN_RAD]
    ctx.last_full_joints = qs
    ctx.zones = _REROUTE_WALL
    home_full = list(HOME_JOINTS_RAD) + [GRIPPER_OPEN_RAD]
    # Precondition: the DIRECT transit to HOME is blocked by the wall.
    assert path_guard.segment_blocked(
        ctx.ik, qs, home_full, _REROUTE_WALL, path_guard.ZONE_MARGIN_M) is True
    motion.home(ctx, {})
    assert len(calls) >= 2                       # a reroute happened (>1 leg)
    # No published leg sweeps the zone.
    for a, b, _d in calls:
        assert path_guard.segment_blocked(
            ctx.ik, a, b, _REROUTE_WALL, path_guard.ZONE_MARGIN_M) is False
    # The final commanded pose is exactly HOME (gripper carried).
    assert calls[-1][1] == pytest.approx(home_full)
    assert ctx.last_arm_joints == pytest.approx(list(HOME_JOINTS_RAD))


def test_go_to_observation_pose_reroutes_around_blocking_zone(monkeypatch):
    # The named-object loop's retreat is the same whole-arm transit and is now
    # zone-routed too (OBSERVE_POSE defaults to HOME).
    calls = _wrap_publish(monkeypatch)
    ctx = _ctx()
    qs = list(ctx.ik.solve(_REROUTE_START_XYZ,
                           roll=GRASP_ROLL_RAD)) + [GRIPPER_OPEN_RAD]
    ctx.last_full_joints = qs
    ctx.zones = _REROUTE_WALL
    observe_full = list(motion.OBSERVE_POSE_JOINTS) + [GRIPPER_OPEN_RAD]
    assert path_guard.segment_blocked(
        ctx.ik, qs, observe_full, _REROUTE_WALL, path_guard.ZONE_MARGIN_M) is True
    motion.go_to_observation_pose(ctx)
    assert len(calls) >= 2                       # rerouted
    for a, b, _d in calls:
        assert path_guard.segment_blocked(
            ctx.ik, a, b, _REROUTE_WALL, path_guard.ZONE_MARGIN_M) is False
    assert calls[-1][1] == pytest.approx(observe_full)

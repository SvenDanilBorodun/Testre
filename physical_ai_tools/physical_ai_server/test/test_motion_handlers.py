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

import threading
import types

import pytest

from physical_ai_server.workflow import trajectory_builder
from physical_ai_server.workflow.handlers import motion
from physical_ai_server.workflow.handlers.motion import (
    GRIPPER_CLOSED_RAD,
    GRIPPER_OPEN_RAD,
    HOME_JOINTS_RAD,
    WORKSPACE_FLOOR_MARGIN_M,
    WorkflowError,
    drop_at,
    move_to,
    pickup,
    _solve_or_raise,
)
from physical_ai_server.workflow.ik_solver import IKSolver


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

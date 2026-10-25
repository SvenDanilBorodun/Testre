"""``motion.home`` / ``motion.go_to_observation_pose`` after the safe-Grundstellung
wiring: the block must plan its route, publish every leg through ``safe_move``,
still carry the gripper, and stay byte-identical on the OMX.
"""

from __future__ import annotations

import math

import pytest

from physical_ai_server import robot_profiles as RP
from physical_ai_server.workflow.v1_ik import V1IKSolver
from physical_ai_server.workflow.handlers import motion as M
from physical_ai_server.workflow.ik_solver import IKSolver

_V1 = RP.ROBOT_PROFILES['v1_studio']
_OMX = RP.ROBOT_PROFILES['omx_full']
_V1_HOME = list(_V1.home_joints_rad)

# The rescued witness from the box-model sweep (test_home_planner): the direct
# glide stays below the floor allowance, a via exists.
_RESCUED = [-0.599, 0.269, 0.313, -1.161, 0.957]
# A 234 mm press on the direct line; no via rescues it.
_UNRESCUABLE = [0.009, 2.885, 0.047, -1.289, -1.384]


class _Ctx:
    """Enough of a WorkflowContext for the motion handlers, recording publishes."""

    def __init__(self, profile, ik, start, follower=None):
        self.ik = ik
        self.zones = []
        self.z_table = 0.0
        self.table_plane = None
        self.tempo = 1.0
        self.motion_lock = None
        self.published: list = []
        self.logs: list[str] = []
        self.log = self.logs.append
        self.should_stop = lambda: False
        self.publisher = lambda pts: self.published.append(pts)
        self.last_full_joints = list(start)
        self.last_arm_joints = list(start[:profile.num_arm_joints])
        self.num_arm_joints = profile.num_arm_joints
        self.roll_joint_index = profile.roll_joint_index
        self.home_joints_rad = profile.home_joints_rad
        self.gripper_open_rad = profile.gripper_open_rad
        self.gripper_closed_rad = profile.gripper_closed_rad
        self.velocity_limit_rad_s = profile.velocity_limit_rad_s
        if follower is not None:
            self.get_follower_joints = lambda: list(follower)


def _v1_ctx(start, follower=None):
    return _Ctx(_V1, V1IKSolver(),
                list(start) + [_V1.gripper_open_rad], follower)


def _omx_ctx(start, follower=None):
    return _Ctx(_OMX, IKSolver(), list(start) + [_OMX.gripper_open_rad],
                follower)


def _legs(monkeypatch):
    """Capture the (q_start, q_end, duration) triples ``home`` hands to
    ``safe_move``, without publishing anything."""
    seen: list = []
    monkeypatch.setattr(
        M, 'safe_move',
        lambda ctx, a, b, d, **kw: seen.append((list(a), list(b), d)))
    return seen


# ── the box-model fix is actually reached from the block ─────────────────────

def test_home_publishes_more_than_one_leg_for_a_pressing_start(monkeypatch):
    """The whole point of the wiring. If ``home`` went straight to ``safe_move``
    again this would be one leg — and that one leg presses the table."""
    seen = _legs(monkeypatch)
    M.home(_v1_ctx(_RESCUED), {})
    assert len(seen) >= 2


def test_home_is_still_exactly_one_leg_when_the_route_is_clean(monkeypatch):
    seen = _legs(monkeypatch)
    ik = V1IKSolver()
    clean = list(ik.solve((ik.base_axis_x + 0.14, 0.0, 0.0),
                          roll=M.GRASP_ROLL_RAD))
    M.home(_v1_ctx(clean), {})
    assert len(seen) == 1
    assert seen[0][2] == M.DEFAULT_HOME_DURATION_S


def test_home_still_carries_the_gripper_across_a_multi_leg_route(monkeypatch):
    """The deliberate 2026-06 behaviour (pickup -> home must not drop the
    object) must survive the reroute. Every leg ends with the START gripper."""
    seen = _legs(monkeypatch)
    ctx = _v1_ctx(_RESCUED)
    closed = 0.25
    ctx.last_full_joints = list(_RESCUED) + [closed]
    M.home(ctx, {})
    assert len(seen) >= 2
    for _a, b, _d in seen:
        assert b[5] == pytest.approx(closed)
    assert ctx.last_full_joints[5] == pytest.approx(closed)


def test_home_records_the_home_pose_as_the_new_state(monkeypatch):
    _legs(monkeypatch)
    ctx = _v1_ctx(_RESCUED)
    M.home(ctx, {})
    assert ctx.last_arm_joints == pytest.approx(_V1_HOME)
    assert ctx.last_full_joints[:5] == pytest.approx(_V1_HOME)


def test_home_refuses_in_german_when_no_route_exists(monkeypatch):
    """A refusal must reach the student, not be swallowed into a direct publish."""
    _legs(monkeypatch)
    with pytest.raises(M.WorkflowError) as excinfo:
        M.home(_v1_ctx(_UNRESCUABLE), {})
    assert 'Tischebene' in str(excinfo.value)


def test_every_leg_goes_through_safe_move_so_zones_still_apply(monkeypatch):
    """Legs must be published via ``safe_move``, not raw ``_publish_motion`` —
    otherwise the reroute would silently drop the Sperrzone guard and the Tempo."""
    seen = _legs(monkeypatch)
    raw: list = []
    monkeypatch.setattr(M, '_publish_motion_t',
                        lambda *a, **k: raw.append(a))
    M.home(_v1_ctx(_RESCUED), {})
    assert len(seen) >= 2
    assert raw == []


# ── the observation-pose retreat gets the same treatment ─────────────────────

def test_the_observation_retreat_is_planned_too(monkeypatch):
    """It defaults to the same target as ``home`` and runs on every pass of a
    „Solange … sichtbar" loop, so leaving it unjudged would keep the defect on
    the hottest path."""
    seen = _legs(monkeypatch)
    M.go_to_observation_pose(_v1_ctx(_RESCUED))
    assert len(seen) >= 2


# ── OMX bit-identity ─────────────────────────────────────────────────────────

def test_omx_home_is_one_untouched_leg(monkeypatch):
    """THE regression guard for the OMX. One leg, original endpoints, original
    duration — exactly the old single ``safe_move`` call."""
    seen = _legs(monkeypatch)
    start = [0.1, -1.2, 1.0, 0.0, 0.0]
    ctx = _omx_ctx(start)
    M.home(ctx, {})
    assert len(seen) == 1
    assert seen[0][0] == list(start) + [_OMX.gripper_open_rad]
    assert seen[0][1] == list(_OMX.home_joints_rad) + [_OMX.gripper_open_rad]
    assert seen[0][2] == M.DEFAULT_HOME_DURATION_S


def test_omx_observation_retreat_is_one_untouched_leg(monkeypatch):
    seen = _legs(monkeypatch)
    M.go_to_observation_pose(_omx_ctx([0.1, -1.2, 1.0, 0.0, 0.0]))
    assert len(seen) == 1
    assert seen[0][2] == M.DEFAULT_MOVE_DURATION_S


# ── arrival verification is warn-only ────────────────────────────────────────

def test_a_missed_arrival_warns_once_and_does_not_raise(monkeypatch):
    """The driver already owns the RE-SEND; duplicating it here would put two
    writers on the same rail. And a workflow may be stopping legitimately, so a
    raise would turn a clean stop into an error."""
    _legs(monkeypatch)
    off = [v + 0.9 for v in _V1_HOME] + [_V1.gripper_open_rad]
    ik = V1IKSolver()
    clean = list(ik.solve((ik.base_axis_x + 0.14, 0.0, 0.0),
                          roll=M.GRASP_ROLL_RAD))
    ctx = _v1_ctx(clean, follower=off)
    M.home(ctx, {})
    hits = [m for m in ctx.logs if 'Grundstellung nicht ganz erreicht' in m]
    assert len(hits) == 1


def test_a_matching_arrival_is_silent(monkeypatch):
    _legs(monkeypatch)
    ik = V1IKSolver()
    clean = list(ik.solve((ik.base_axis_x + 0.14, 0.0, 0.0),
                          roll=M.GRASP_ROLL_RAD))
    ctx = _v1_ctx(clean,
                    follower=list(_V1_HOME) + [_V1.gripper_open_rad])
    M.home(ctx, {})
    assert not any('nicht ganz erreicht' in m for m in ctx.logs)


def test_the_arrival_tolerance_matches_the_drivers(monkeypatch):
    """One number for "did the arm get there", not three. The Feetech driver
    uses BOOT_HOME_VERIFY_TOL_RAD = 0.30 and the OMX entrypoint's Phase-3
    verifier uses the same."""
    assert M.HOME_ARRIVAL_TOL_RAD == 0.30


@pytest.mark.parametrize('follower', [None, [], [0.0, 0.0], 'nonsense'])
def test_an_unusable_joint_readback_is_silently_tolerated(monkeypatch, follower):
    """A diagnostic must never break a home move."""
    _legs(monkeypatch)
    ik = V1IKSolver()
    clean = list(ik.solve((ik.base_axis_x + 0.14, 0.0, 0.0),
                          roll=M.GRASP_ROLL_RAD))
    ctx = _v1_ctx(clean)
    if follower is not None:
        ctx.get_follower_joints = lambda: follower
    M.home(ctx, {})
    assert not any('nicht ganz erreicht' in m for m in ctx.logs)


def test_a_raising_joint_getter_is_tolerated(monkeypatch):
    _legs(monkeypatch)
    ik = V1IKSolver()
    clean = list(ik.solve((ik.base_axis_x + 0.14, 0.0, 0.0),
                          roll=M.GRASP_ROLL_RAD))
    ctx = _v1_ctx(clean)

    def _boom():
        raise RuntimeError('bus down')

    ctx.get_follower_joints = _boom
    M.home(ctx, {})       # must not raise
    assert not any('nicht ganz erreicht' in m for m in ctx.logs)

"""The monotone-escape rung in ``path_guard`` — the fix for the Sperrzone dead
end that traps BOTH arms.

Measured 2026-07-27 over 42 (start, zone) scenarios: 32/42 on edu6 and 28/42 on
the OMX ended in ``_static_overlap_refusal``, i.e. a student who draws a small
box near where the arm is standing makes the arm's own link points fall inside
the 5 cm inflation and from then on NO motion is permitted at all — including
going home — while the German message tells them to redraw a zone that was never
the problem.
"""

from __future__ import annotations

import math

import pytest

from physical_ai_server import robot_profiles as RP
from physical_ai_server.workflow import path_guard as PG
from physical_ai_server.workflow.edu6_ik import Edu6IKSolver
from physical_ai_server.workflow.handlers import motion as M
from physical_ai_server.workflow.ik_solver import IKSolver

_EDU6 = RP.ROBOT_PROFILES['edu6_studio']
_OMX = RP.ROBOT_PROFILES['omx_full']
_INFL = PG.LINK_RADIUS_M + PG.ZONE_MARGIN_M


def _box(cx, cy, cz, half, halfz=None):
    hz = half if halfz is None else halfz
    return {'min': [cx - half, cy - half, cz - hz],
            'max': [cx + half, cy + half, cz + hz]}


class _Ctx:
    def __init__(self, ik, zones):
        self.ik = ik
        self.zones = zones
        self.z_table = 0.0
        self.table_plane = None
        self.last_arm_joints = None
        self.num_arm_joints = ik.num_joints()


# ── the penetration metric ───────────────────────────────────────────────────

def test_penetration_is_zero_for_an_arm_clear_of_every_zone():
    ik = Edu6IKSolver()
    far = [_box(ik.base_axis_x + 1.0, 0.0, 0.0, 0.02)]
    assert PG.zone_penetration(ik, list(_EDU6.home_joints_rad), far,
                               _INFL) == 0.0


def test_penetration_is_positive_when_the_arm_stands_in_a_zone():
    """A box on the base swallows the fixed base column under the 5 cm
    inflation — the exact situation that bricks all motion today."""
    ik = Edu6IKSolver()
    at_base = [_box(0.0, 0.0, 0.02, 0.02)]
    assert PG.zone_penetration(ik, list(_EDU6.home_joints_rad), at_base,
                               _INFL) > 0.0


def test_penetration_never_raises_on_a_solver_that_cannot_answer():
    class _Blind:
        def num_joints(self):
            return 6

        def link_points(self, *_a, **_k):
            raise RuntimeError('no')

    assert PG.zone_penetration(_Blind(), [0.0] * 6,
                               [_box(0, 0, 0, 0.05)], _INFL) == 0.0


def test_penetration_is_zero_for_malformed_zones():
    ik = Edu6IKSolver()
    assert PG.zone_penetration(ik, list(_EDU6.home_joints_rad),
                               [{'min': 'x'}], _INFL) == 0.0


# ── the escape test agrees with segment_blocked wherever it matters ──────────

@pytest.mark.parametrize('profile,solver', [('edu6', Edu6IKSolver),
                                            ('omx', IKSolver)])
def test_outside_every_zone_the_escape_test_equals_not_segment_blocked(
        profile, solver):
    """The equivalence that makes this safe to add: for an arm never inside a
    zone, penetration is 0 throughout, so ``escape_leg_ok`` is EXACTLY
    ``not segment_blocked``. If these ever diverge, the escape rung has started
    permitting transits it should not."""
    ik = solver()
    home = list(RP.ROBOT_PROFILES['edu6_studio' if profile == 'edu6'
                                  else 'omx_full'].home_joints_rad)
    n = ik.num_joints()
    checked = 0
    for r in (0.10, 0.16):
        for h in (0.02, 0.04):
            zones = [_box(ik.base_axis_x + r, 0.0, h, h, h)]
            start = ik.solve((ik.base_axis_x + 0.19, 0.0, 0.0),
                             roll=M.GRASP_ROLL_RAD)
            if start is None:
                continue
            start = list(start)
            if PG.zone_penetration(ik, start, zones, _INFL) > 0.0:
                continue                      # not the population under test
            if PG.zone_penetration(ik, home[:n], zones, _INFL) > 0.0:
                continue
            checked += 1
            blocked = PG.segment_blocked(ik, start, home[:n], zones)
            assert PG.escape_leg_ok(ik, start, home[:n], zones) == (not blocked)
    assert checked > 0


def test_an_arm_standing_in_a_zone_may_leave_it():
    """THE point. ``segment_blocked`` says no (it samples u=0, which is inside);
    the escape test says yes, because the arm only ever gets shallower."""
    ik = Edu6IKSolver()
    home = list(_EDU6.home_joints_rad)
    # a box swallowing the arm where it stands at HOME
    tip = ik.fk(home)[1]
    zones = [_box(float(tip[0]), float(tip[1]), float(tip[2]), 0.02)]
    assert PG.zone_penetration(ik, home, zones, _INFL) > 0.0
    grasp = list(ik.solve((ik.base_axis_x + 0.16, 0.0, 0.0),
                          roll=M.GRASP_ROLL_RAD))
    assert PG.segment_blocked(ik, home, grasp, zones), 'premise: direct blocked'
    assert PG.escape_leg_ok(ik, home, grasp, zones), (
        'the arm must be allowed to LEAVE a zone it is standing in')


def test_a_leg_that_goes_deeper_is_still_refused():
    """The escape must be MONOTONE, not a blanket permission. Reversing the
    escape leg — driving INTO the zone — has to stay refused, or the rung is
    just a hole in the guard."""
    ik = Edu6IKSolver()
    home = list(_EDU6.home_joints_rad)
    tip = ik.fk(home)[1]
    zones = [_box(float(tip[0]), float(tip[1]), float(tip[2]), 0.02)]
    grasp = list(ik.solve((ik.base_axis_x + 0.16, 0.0, 0.0),
                          roll=M.GRASP_ROLL_RAD))
    assert not PG.escape_leg_ok(ik, grasp, home, zones)


# ── plan_safe_route: opt-in, and byte-identical without it ───────────────────

def test_plan_safe_route_default_is_byte_identical():
    """Every existing caller passes nothing, so it must behave exactly as before
    — including still raising the static-overlap refusal."""
    ik = Edu6IKSolver()
    home = list(_EDU6.home_joints_rad)
    tip = ik.fk(home)[1]
    zones = [_box(float(tip[0]), float(tip[1]), float(tip[2]), 0.02)]
    grasp = list(ik.solve((ik.base_axis_x + 0.16, 0.0, 0.0),
                          roll=M.GRASP_ROLL_RAD))
    ctx = _Ctx(ik, zones)
    with pytest.raises(M.WorkflowError) as excinfo:
        PG.plan_safe_route(ctx, home + [1.75], grasp + [1.75], zones, 3.0,
                           roll=0.0)
    assert 'Sperrzone' in str(excinfo.value)


def test_plan_safe_route_with_the_opt_in_returns_one_escape_leg():
    ik = Edu6IKSolver()
    home = list(_EDU6.home_joints_rad)
    tip = ik.fk(home)[1]
    zones = [_box(float(tip[0]), float(tip[1]), float(tip[2]), 0.02)]
    grasp = list(ik.solve((ik.base_axis_x + 0.16, 0.0, 0.0),
                          roll=M.GRASP_ROLL_RAD))
    ctx = _Ctx(ik, zones)
    legs = PG.plan_safe_route(ctx, home + [1.75], grasp + [1.75], zones, 3.0,
                              roll=0.0, allow_monotone_escape=True)
    assert len(legs) == 1
    assert legs[0][0] == home + [1.75]
    assert legs[0][1] == grasp + [1.75]


def test_the_opt_in_still_refuses_a_leg_that_goes_deeper():
    """The opt-in is not a bypass: a caller that asks to escape but actually
    drives further in must still be refused."""
    ik = Edu6IKSolver()
    home = list(_EDU6.home_joints_rad)
    tip = ik.fk(home)[1]
    zones = [_box(float(tip[0]), float(tip[1]), float(tip[2]), 0.02)]
    grasp = list(ik.solve((ik.base_axis_x + 0.16, 0.0, 0.0),
                          roll=M.GRASP_ROLL_RAD))
    ctx = _Ctx(ik, zones)
    with pytest.raises(M.WorkflowError):
        PG.plan_safe_route(ctx, grasp + [1.75], home + [1.75], zones, 3.0,
                           roll=0.0, allow_monotone_escape=True)


# ── the retreat callers opt in; transits do not (BOTH arms) ─────────────────

@pytest.mark.parametrize('handler', ['home', 'observe'])
@pytest.mark.parametrize('profile_id', ['omx_full', 'edu6_studio'])
def test_the_retreat_callers_pass_the_opt_in_on_both_arms(monkeypatch,
                                                          handler, profile_id):
    """The dead end is SHARED (32/42 edu6, 28/42 OMX), so the fix has to reach
    both. This asserts the flag actually arrives at safe_move."""
    seen: list = []
    monkeypatch.setattr(M, 'safe_move',
                        lambda *a, **kw: seen.append(kw.get(
                            'allow_monotone_escape', False)))
    profile = RP.ROBOT_PROFILES[profile_id]
    ik = profile.build_ik()
    n = profile.num_arm_joints

    class _C:
        pass

    ctx = _C()
    ctx.ik = ik
    ctx.zones = []
    ctx.z_table = 0.0
    ctx.table_plane = None
    ctx.num_arm_joints = n
    ctx.roll_joint_index = profile.roll_joint_index
    ctx.home_joints_rad = profile.home_joints_rad
    ctx.gripper_open_rad = profile.gripper_open_rad
    ctx.gripper_closed_rad = profile.gripper_closed_rad
    ctx.velocity_limit_rad_s = profile.velocity_limit_rad_s
    ctx.last_full_joints = list(profile.home_joints_rad) + [
        profile.gripper_open_rad]
    ctx.last_arm_joints = list(profile.home_joints_rad)
    ctx.log = lambda _m: None
    if handler == 'home':
        M.home(ctx, {})
    else:
        M.go_to_observation_pose(ctx)
    assert seen and all(seen), 'the retreat must opt into the escape rung'


def test_a_transit_never_opts_in(monkeypatch):
    """move_to is a TRANSIT — the static-overlap refusal is correct there, and
    silently relaxing it for every move would be a real safety change."""
    import inspect
    src = inspect.getsource(M.move_to)
    assert 'allow_monotone_escape' not in src
    for name in ('move_above', 'descend_to', 'lift', 'drop_at'):
        assert 'allow_monotone_escape' not in inspect.getsource(
            getattr(M, name))

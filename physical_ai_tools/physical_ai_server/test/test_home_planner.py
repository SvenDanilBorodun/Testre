"""The safe-Grundstellung planner (``workflow/home_planner.py``).

Everything here drives the REAL ``Edu6IKSolver`` and the REAL registry profile.
CLAUDE.md's standing warning applies: much n=6 coverage in this repo is
``_FakeIK6`` stub-based, and that is exactly where edu6-only bugs have hidden
twice. A width stub proves nothing about geometry, so none is used.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from physical_ai_server import robot_profiles as RP
from physical_ai_server.workflow import home_planner as HP
from physical_ai_server.workflow.edu6_ik import Edu6IKSolver
from physical_ai_server.workflow.handlers import motion as M
from physical_ai_server.workflow.ik_solver import IKSolver

_EDU6 = RP.ROBOT_PROFILES['edu6_studio']
_OMX = RP.ROBOT_PROFILES['omx_full']
_HOME = list(_EDU6.home_joints_rad)
_HOME_FULL = _HOME + [_EDU6.gripper_open_rad]

# Real witnesses from the 2026-07-27 mesh sweep (600 attainable starts, every
# planned route then verified on the STLs: 588 direct / 4 rescued / 8 refused,
# worst mesh z +0.98 mm, i.e. zero presses).
#
# RESCUED: the direct glide from here presses the table; the planner finds a via.
_RESCUED_START = [0.075, 1.336, -0.882, -2.109, -1.355, -2.621]
_RESCUED_FULL = _RESCUED_START + [_EDU6.gripper_open_rad]
# REFUSED: the deepest press in the whole sweep (a finger 167.9 mm below the
# table on the direct line) and no via in the lattice rescues it.
_PRESSING_START = [-0.131, 2.997, -0.020, 1.964, -0.391, 1.898]
_PRESSING_FULL = _PRESSING_START + [_EDU6.gripper_open_rad]


class _Ctx:
    """What plan_home_route actually reads: ik, zones, z_table/table_plane, log."""

    def __init__(self, ik=None, zones=None, z_table=0.0, table_plane=None):
        self.ik = Edu6IKSolver() if ik is None else ik
        self.zones = zones or []
        self.z_table = z_table
        self.table_plane = table_plane
        self.logs: list[str] = []
        self.log = self.logs.append


def _edu6(**kw):
    return _Ctx(**kw)


def _omx(**kw):
    return _Ctx(ik=IKSolver(), **kw)


# ── L0: the 98 % case must stay exactly what it was ──────────────────────────

def test_a_clean_route_is_one_direct_leg_with_untouched_endpoints():
    """The measured shape of the fix: direct answers 588/600. If the planner
    started rerouting the common case it would change the motion students see on
    every single home block."""
    ctx = _edu6()
    start = list(Edu6IKSolver().solve(
        (Edu6IKSolver().base_axis_x + 0.14, 0.0, 0.0),
        roll=M.GRASP_ROLL_RAD)) + [_EDU6.gripper_open_rad]
    legs = HP.plan_home_route(ctx, start, _HOME_FULL, 3.0)
    assert len(legs) == 1
    assert legs[0][0] == start
    assert legs[0][1] == _HOME_FULL
    assert legs[0][2] == 3.0


def test_home_to_home_is_direct():
    legs = HP.plan_home_route(_edu6(), _HOME_FULL, _HOME_FULL, 3.0)
    assert len(legs) == 1


# ── L1: the escape rung, and the bug it was born from ────────────────────────

def test_a_pressing_start_is_rerouted_and_every_leg_clears_the_floor():
    """THE regression guard. The direct glide from this start presses the table;
    the planner must return a multi-leg route whose EVERY leg clears the
    allowance. Fails if the box table, the swept check, or the via search
    regresses."""
    from physical_ai_server.workflow import arm_geometry as AG
    ctx = _edu6()
    geom = AG.resolve_geometry(ctx)
    floor = HP._floor_fn(ctx)
    # the premise: direct really is unsafe here
    assert geom.swept_floor_clearance(
        _RESCUED_FULL, _HOME_FULL, floor) < -HP.FLOOR_TOL_M
    legs = HP.plan_home_route(ctx, _RESCUED_FULL, _HOME_FULL, 3.0)
    assert len(legs) >= 2, 'a direct route here would press the table'

    # THE CONTRACT, and it is two different bounds on purpose:
    #   leg 1 (the lift OUT of a bad pose) is MONOTONE — it may sit as low as
    #     the arm ALREADY is, because the arm is standing there right now and a
    #     guard must not forbid climbing out of a hole it did not dig;
    #   every LATER leg must clear the allowance outright.
    # Pinning leg 1 to the allowance instead would forbid the rescue entirely.
    start_clearance = geom.floor_clearance(_RESCUED_START, floor)
    leg1_bound = min(start_clearance, -HP.FLOOR_TOL_M) - 1e-9
    first = geom.swept_floor_clearance(legs[0][0], legs[0][1], floor)
    assert first is not None and first >= leg1_bound, (
        f'the lift leg went DEEPER than the start pose: {first:.4f} m '
        f'vs {leg1_bound:.4f} m')
    for q_from, q_to, _dur in legs[1:]:
        swept = geom.swept_floor_clearance(q_from, q_to, floor)
        assert swept is not None
        assert swept >= -HP.FLOOR_TOL_M, (
            f'leg {q_from} -> {q_to} still dips to {swept:.4f} m')


def test_a_rerouted_home_starts_and_ends_exactly_where_it_must():
    legs = HP.plan_home_route(_edu6(), _RESCUED_FULL, _HOME_FULL, 3.0)
    assert legs[0][0] == _RESCUED_FULL
    assert legs[-1][1] == _HOME_FULL


def test_a_rerouted_home_carries_the_gripper_through_every_via():
    """A held object must survive the detour. The via inherits q_start's gripper,
    so the gripper actuates at most once — on the final leg, exactly like the
    direct path."""
    ctx = _edu6()
    closed = 0.2
    legs = HP.plan_home_route(ctx, list(_RESCUED_START) + [closed],
                              _HOME + [closed], 3.0)
    assert len(legs) >= 2
    n = ctx.ik.num_joints()
    for _q_from, q_to, _d in legs:
        assert q_to[n] == pytest.approx(closed)


def test_a_via_keeps_the_students_base_yaw_and_both_rolls():
    """The lattice varies only the three joints that move the arm in its own
    plane. Widening it is a real option, but it must be a deliberate, measured
    change — not something that drifts in."""
    ctx = _edu6()
    legs = HP.plan_home_route(ctx, _RESCUED_FULL, _HOME_FULL, 3.0)
    via = legs[0][1]
    for idx in (0, 3, 5):
        assert via[idx] == pytest.approx(_RESCUED_FULL[idx])


def test_the_first_leg_admissibility_is_monotone_not_absolute():
    """The load-bearing idea, and a bug that was actually shipped in a prototype.

    The FIRST leg may stay below the floor allowance as long as it never goes
    DEEPER than where the arm already is — the arm may always move away from
    trouble. Gating leg 1 on the absolute allowance instead rejects every
    candidate whenever the start is already below it, which is this rung's whole
    population; measured, the rung then fires ZERO times.
    """
    from physical_ai_server.workflow import arm_geometry as AG
    ctx = _edu6()
    geom = AG.resolve_geometry(ctx)
    floor = HP._floor_fn(ctx)
    # a start BELOW the allowance: an absolute gate would have nothing to work with
    start = list(_PRESSING_START)
    assert geom.floor_clearance(start, floor) < -HP.FLOOR_TOL_M + 0.030
    # the monotone rule admits standing still; an absolute rule would not
    assert HP._leg_ok(
        geom, ctx, start, start, floor,
        min(geom.floor_clearance(start, floor), -HP.FLOOR_TOL_M) - 1e-9)
    assert not HP._leg_ok(geom, ctx, start, start, floor, 0.0)


# ── L3: the refusal names the remedy that works ──────────────────────────────

def test_an_unrescuable_start_refuses_rather_than_pressing_the_table():
    """The deepest press in the whole sweep (167.9 mm) and no via rescues it.
    The planner must REFUSE, not fall back to the direct line. Fails if someone
    "helpfully" makes the last rung publish something anyway."""
    ctx = _edu6()
    with pytest.raises(M.WorkflowError) as excinfo:
        HP.plan_home_route(ctx, _PRESSING_FULL, _HOME_FULL, 3.0)
    assert 'Tischebene' in str(excinfo.value)


def test_the_refusal_names_hand_guiding_and_not_the_sperrzone():
    """A refusal is a dead end for a student, so it must name a remedy that
    needs no calibration and exists on every rig. It must NOT reuse the
    Sperrzone wording — that would send the student to redraw a zone that was
    never the problem."""
    from physical_ai_server.workflow import path_guard as PG
    msg = HP._FLOOR_REFUSAL_DE
    assert 'Tischebene' in msg
    assert 'Handführung' in msg
    assert msg != PG._REFUSE_MSG
    assert 'Sperrzone' not in msg


def test_geometry_that_cannot_be_judged_is_never_treated_as_safe():
    """``None`` from the model means UNKNOWN. A leg check must fail closed."""
    from physical_ai_server.workflow import arm_geometry as AG

    class _Blind(AG.ArmGeometry):
        def swept_floor_clearance(self, *_a, **_k):
            return None

    ctx = _edu6()
    blind = _Blind(ctx.ik)
    assert not HP._leg_ok(blind, ctx, _HOME, _HOME, HP._floor_fn(ctx), -0.02)


# ── OMX / legacy bit-identity ────────────────────────────────────────────────

def test_omx_gets_exactly_one_untouched_direct_leg():
    """THE bit-identity guarantee. If this ever returns more than one leg, or
    changed endpoints, the OMX's home behaviour has silently changed."""
    ctx = _omx()
    start = [0.1, -1.2, 1.0, 0.0, 0.0, 0.8]
    end = list(_OMX.home_joints_rad) + [_OMX.gripper_open_rad]
    legs = HP.plan_home_route(ctx, start, end, 3.0)
    assert legs == [(start, end, 3.0)]


def test_a_ctx_without_a_solver_gets_the_direct_leg():
    ctx = _edu6(ik=None)
    ctx.ik = None
    legs = HP.plan_home_route(ctx, _PRESSING_FULL, _HOME_FULL, 3.0)
    assert legs == [(_PRESSING_FULL, _HOME_FULL, 3.0)]


def test_the_floor_tolerance_zero_is_a_full_one_variable_rollback(monkeypatch):
    """``EDUBOTICS_HOME_FLOOR_TOL_M=0`` must restore today's behaviour exactly,
    even for the start that presses the table hardest."""
    monkeypatch.setattr(HP, 'FLOOR_TOL_M', 0.0)
    legs = HP.plan_home_route(_edu6(), _PRESSING_FULL, _HOME_FULL, 3.0)
    assert legs == [(_PRESSING_FULL, _HOME_FULL, 3.0)]


# ── env parsing follows the established convention ───────────────────────────

def test_empty_env_counts_as_unset_because_compose_always_forwards_it(monkeypatch):
    """compose forwards optional knobs as ``${VAR:-}``, so every un-tuned rig
    delivers ''. Treating bare presence as "set" is the
    EDUBOTICS_GRASP_HELD_MAX_RAD dead-code scar."""
    monkeypatch.setenv('EDUBOTICS_HOME_FLOOR_TOL_M', '')
    assert HP._env_float('EDUBOTICS_HOME_FLOOR_TOL_M', 0.020) == 0.020
    monkeypatch.setenv('EDUBOTICS_HOME_FLOOR_TOL_M', '   ')
    assert HP._env_float('EDUBOTICS_HOME_FLOOR_TOL_M', 0.020) == 0.020


def test_a_malformed_or_negative_env_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv('EDUBOTICS_HOME_FLOOR_TOL_M', 'zwanzig')
    assert HP._env_float('EDUBOTICS_HOME_FLOOR_TOL_M', 0.020) == 0.020
    monkeypatch.setenv('EDUBOTICS_HOME_FLOOR_TOL_M', '-0.5')
    assert HP._env_float('EDUBOTICS_HOME_FLOOR_TOL_M', 0.020) == 0.020
    monkeypatch.setenv('EDUBOTICS_HOME_FLOOR_TOL_M', 'nan')
    assert HP._env_float('EDUBOTICS_HOME_FLOOR_TOL_M', 0.020) == 0.020
    monkeypatch.setenv('EDUBOTICS_HOME_FLOOR_TOL_M', '0')
    assert HP._env_float('EDUBOTICS_HOME_FLOOR_TOL_M', 0.020) == 0.0


# ── self-collision stays warn-only ───────────────────────────────────────────

def test_self_collision_never_blocks_a_route(monkeypatch):
    """Rule §2: shipping a REFUSAL on self-collision is out of scope this round.
    A route whose self-clearance is zero must still be returned."""
    from physical_ai_server.workflow import arm_geometry as AG
    monkeypatch.setattr(AG.ArmGeometry, 'swept_self_clearance',
                        lambda *_a, **_k: 0.0)
    ctx = _edu6()
    legs = HP.plan_home_route(ctx, _HOME_FULL, _HOME_FULL, 3.0)
    assert len(legs) == 1
    assert any('sehr nahe' in m for m in ctx.logs)


def test_the_self_collision_warning_is_silent_when_the_route_is_clear():
    ctx = _edu6()
    HP.plan_home_route(ctx, _HOME_FULL, _HOME_FULL, 3.0)
    assert not any('sehr nahe' in m for m in ctx.logs)


def test_the_self_collision_warning_can_be_switched_off(monkeypatch):
    from physical_ai_server.workflow import arm_geometry as AG
    monkeypatch.setattr(AG.ArmGeometry, 'swept_self_clearance',
                        lambda *_a, **_k: 0.0)
    monkeypatch.setattr(HP, 'SELFCOLL_WARN_M', 0.0)
    ctx = _edu6()
    HP.plan_home_route(ctx, _HOME_FULL, _HOME_FULL, 3.0)
    assert not any('sehr nahe' in m for m in ctx.logs)


# ── zones ────────────────────────────────────────────────────────────────────

def test_a_zone_across_the_direct_path_is_not_returned_as_direct():
    """The planner must respect Sperrzonen as well as the table — it replaces
    the zone-aware ``safe_move`` call, so losing zones here would be a
    regression in the OTHER guard."""
    ik = Edu6IKSolver()
    start = list(ik.solve((ik.base_axis_x + 0.16, 0.0, 0.0),
                          roll=M.GRASP_ROLL_RAD)) + [_EDU6.gripper_open_rad]
    wall = [{'min': [ik.base_axis_x + 0.04, -0.08, 0.0],
             'max': [ik.base_axis_x + 0.10, 0.08, 0.25]}]
    ctx = _edu6(zones=wall)
    from physical_ai_server.workflow import path_guard as PG
    assert PG.segment_blocked(ctx.ik, start, _HOME_FULL, wall)
    try:
        legs = HP.plan_home_route(ctx, start, _HOME_FULL, 3.0)
    except Exception as exc:            # a refusal is an acceptable outcome
        assert 'Sperrzone' in str(exc) or 'Tischebene' in str(exc)
        return
    for q_from, q_to, _d in legs:
        assert not PG.segment_blocked(ctx.ik, q_from, q_to, wall)


# ── the tilted table is honoured ─────────────────────────────────────────────

def test_a_tilted_table_plane_raises_the_floor_under_the_arm():
    """``_floor_fn`` must go through ``motion._floor_z_at`` so a touch-off's
    tilted plane is evaluated at each point's own (x, y)."""
    flat = HP._floor_fn(_edu6(z_table=0.0))
    tilted = HP._floor_fn(_edu6(table_plane=(0.2, 0.0, 0.01)))
    assert flat(0.15, 0.0) == pytest.approx(0.0)
    assert tilted(0.15, 0.0) == pytest.approx(0.2 * 0.15 + 0.01)


def test_an_uncalibrated_rig_still_gets_a_floor_at_zero():
    """z = 0 is not a guess on this arm — it is where base_link is bolted. So the
    guard works before „Tisch vermessen", unlike the Cartesian floor."""
    ctx = _edu6(z_table=None)
    assert HP._floor_fn(ctx)(0.1, 0.1) == 0.0

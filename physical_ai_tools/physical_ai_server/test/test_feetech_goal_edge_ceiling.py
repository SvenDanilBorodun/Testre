"""The commanded-goal edge clamp's CEILING, measured against the REAL solver.

WHY THIS FILE EXISTS AT ALL. ``_GOAL_EDGE_MARGIN_MAX_TICKS`` in
``robotis_ai_setup/docker/open_manipulator/feetech_arm_node.py`` fences the
``EDUBOTICS_FEETECH_GOAL_EDGE_MARGIN_TICKS`` override, and its whole
justification is a claim about
:class:`~physical_ai_server.workflow.v1_ik.V1IKSolver`: "a margin this large
still leaves every autonomous grasp bit-identical". The deps-free guard in
``robotis_ai_setup/tests/test_feetech_bus.py`` cannot see the solver (it has no
numpy), so a wrong ceiling would stay green there. A guard that cannot fail on
the bug it was written for is worse than no guard.

The driver lives on the other side of a dependency seam (deps-free, no numpy,
no rclpy) from the solver (numpy). Rather than teach the deps-free suite about
numpy, this file sits on the SOLVER's side and reaches ACROSS the seam the way
``test_robot_profiles.py`` already does for rclpy-importing modules: it
ast-extracts the driver's literals and its two tick-mapping functions, so the
arithmetic under test is the driver's own, not a copy of it.

WHY A BISECTION AND NOT A GRID. ``solve()``'s q2/q3/q4 depend ONLY on
(ρ, ψ), the polar coordinates of the shoulder→wrist vector, and ρ(g) is
SQUARE-ROOT-SINGULAR at full extension (ρmax − ρ ≈ L2·L3·g²/2ρmax). A grid
uniform in ρ (or in xyz) resolves g only as O(√Δρ) and reports a number too far
from the register end. Here the extremum is found by BISECTING the reach
boundary with ``solve()`` itself and walking inward in 5 µm steps, so no grid
pitch has to be trusted.

WHAT IS PINNED
  * q3 = _G_OFFSET − g exactly (g ≥ 0 on the elbow-up branch, the only one ever
    returned), so q3 has an UNCONDITIONAL analytic bound q3 ≤ _G_OFFSET =
    2.938642 rad ⇒ tick 3964, 131 ticks from the 4095 end. The driver's ceiling
    (130, owner decision b1) must sit strictly below it.
  * The measured minimum over the operating envelope (target at or above the
    base plane, z ≥ 0) IS that bound: full extension is reachable at z = 0.
  * The documented SCOPE of the bit-identity claim: a joint CAN be driven onto
    the seam, but only for targets far BELOW the base plane, which the
    workspace floor refuses long before IK is consulted.
"""

from __future__ import annotations

import ast
import math
import textwrap
from pathlib import Path

import pytest

from physical_ai_server.workflow import v1_ik as ik

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DRIVER_PY = (_REPO_ROOT / 'robotis_ai_setup' / 'docker' / 'open_manipulator'
              / 'feetech_arm_node.py')

_WANT_CONSTS = ('TICKS_PER_REV', 'CENTER_TICK', 'RAD_PER_TICK',
                'JOINT_LIMITS_RAD', '_GOAL_EDGE_MARGIN_MAX_TICKS',
                '_DEFAULT_GOAL_EDGE_MARGIN_TICKS')
_WANT_FUNCS = ('rad_to_tick', 'rad_to_command_tick')


def _driver_namespace():
    """ast-extract the driver's tick map + the fenced constants.

    ``feetech_arm_node`` imports rclpy and ``feetech_bus`` at module scope, so
    it cannot be imported here. Extracting the FUNCTIONS (not reimplementing
    them) is the point — a rounding change in the driver's rad→tick map must
    move this test's numbers too."""
    assert _DRIVER_PY.is_file(), (
        f'{_DRIVER_PY} is missing. This test guards a constant in that file; '
        f'it must not be allowed to pass without it.')
    source = _DRIVER_PY.read_text(encoding='utf-8')
    tree = ast.parse(source)
    ns = {'math': math}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in _WANT_CONSTS
                for t in node.targets):
            exec(compile(ast.Module([node], []), str(_DRIVER_PY), 'exec'), ns)  # noqa: S102
        elif isinstance(node, ast.FunctionDef) and node.name in _WANT_FUNCS:
            exec(compile(textwrap.dedent(ast.get_source_segment(source, node)),
                         str(_DRIVER_PY), 'exec'), ns)  # noqa: S102
    missing = [n for n in _WANT_CONSTS + _WANT_FUNCS if n not in ns]
    assert not missing, f'not extracted from the driver: {missing}'
    # rad_to_command_tick falls back to the module-level knob when margin is
    # None. Every call here passes it explicitly; bind the default anyway so a
    # future no-margin call fails on the assertion, not on a NameError.
    ns['GOAL_EDGE_MARGIN_TICKS'] = ns['_DEFAULT_GOAL_EDGE_MARGIN_TICKS']
    return ns


_D = _driver_namespace()
_LAST_TICK = _D['TICKS_PER_REV'] - 1
_CEILING = _D['_GOAL_EDGE_MARGIN_MAX_TICKS']
_DEFAULT = _D['_DEFAULT_GOAL_EDGE_MARGIN_TICKS']


def _edge(tick: int) -> int:
    """Ticks from the NEARER register end — what the clamp actually bounds."""
    return min(tick, _LAST_TICK - tick)


def _analytic_q3_bound_ticks() -> int:
    """``solve()`` sets q3 = _G_OFFSET − g with g ≥ 0 on the elbow-up branch
    (identity pinned below), so the LARGEST q3 any solution can carry is
    _G_OFFSET at full extension. Derived from the solver's OWN import-time
    ``_G_OFFSET`` through the DRIVER's OWN tick map — no hardcoded angle, no
    sweep. Returned as the distance to the register end it approaches."""
    return _edge(_D['rad_to_tick'](ik._G_OFFSET, 1))


# ── the sweep (module-level, computed once — a few thousand solve() calls) ────

_SOLVER = ik.V1IKSolver()
_X0 = ik.BASE_AXIS_X_WORLD


def _solve_rz(r: float, z: float):
    """``solve()`` at world radius ``r`` (from the joint-1 axis) and height
    ``z``, on the y = 0 meridian. q1 = 0 there and q1 does not enter the
    (ρ, ψ) geometry, so this loses nothing."""
    return _SOLVER.solve((_X0 + r, 0.0, z), roll=0.0)


def _boundary(z: float, seed: float, outward: bool) -> float | None:
    """Bisect the reach boundary at height ``z`` starting from a reachable
    ``seed`` radius. Driven purely by ``solve() is not None``."""
    if _solve_rz(seed, z) is None:
        return None
    lo, hi = seed, (0.45 if outward else 0.0)
    for _ in range(50):
        mid = 0.5 * (lo + hi)
        if _solve_rz(mid, z) is None:
            hi = mid
        else:
            lo = mid
    return lo


def _reachable_seed(z: float) -> float | None:
    """Any reachable radius at height ``z`` (coarse scan; the bisection needs a
    starting point)."""
    for k in range(400):
        r = 0.01 + 0.39 * k / 399
        if _solve_rz(r, z) is not None:
            return r
    return None


def _sweep():
    """``(worst, per_joint, seam_zs)`` over the operating envelope z >= 0.

    ``worst`` is ``(edge_distance, record)`` for the tightest approach to a
    register end, a record being ``(edge, joint_1based, radians, tick, r, z)``;
    ``per_joint`` the same keyed by joint; ``seam_zs`` the heights at which ANY
    solution lands within the fenced ceiling of an end."""
    worst = (10 ** 9, None)
    per_joint: dict[int, tuple] = {}
    seam_zs: list[float] = []

    def note(joints, r, z):
        nonlocal worst
        for i, v in enumerate(joints):
            tick = _D['rad_to_tick'](v, 1)
            e = _edge(tick)
            rec = (e, i + 1, v, tick, r, z)
            if e < worst[0]:
                worst = (e, rec)
            if e < per_joint.get(i + 1, (10 ** 9,))[0]:
                per_joint[i + 1] = rec
            if e <= _CEILING:
                seam_zs.append(z)

    # A) coarse global grid over the envelope — nothing outside the reach
    #    boundaries can be tighter, but this is what makes that falsifiable.
    for iz in range(41):
        z = 0.16 * iz / 40
        for ir in range(81):
            r = 0.02 + 0.40 * ir / 80
            j = _solve_rz(r, z)
            if j:
                note(j, r, z)

    # B) the razor edge: bisect BOTH reach boundaries at a ladder of heights
    #    and walk 1 mm inward in 5 µm steps.
    for iz in range(9):
        z = 0.16 * iz / 8
        seed = _reachable_seed(z)
        if seed is None:
            continue
        for outward in (True, False):
            b = _boundary(z, seed, outward)
            if b is None:
                continue
            for k in range(200):
                r = b - k * 5e-6 if outward else b + k * 5e-6
                j = _solve_rz(r, z)
                if j:
                    note(j, r, z)
    return worst, per_joint, seam_zs


_WORST, _PER_JOINT, _SEAM_ZS = _sweep()


# ── the cross-seam premise ───────────────────────────────────────────────────

def test_the_driver_clamps_to_the_same_arm_limits_the_solver_solves_in():
    """The analytic bound is a statement about ``solve()``'s limit set. The
    driver clamps commands in radians to ITS copy before converting, so the two
    must agree or the bound says nothing about what is commanded."""
    driver_arm = tuple(tuple(b) for b in _D['JOINT_LIMITS_RAD'][:5])
    solver_arm = tuple(tuple(b) for b in ik._V1_JOINT_LIMITS_RAD)
    assert driver_arm == solver_arm
    assert len(_D['JOINT_LIMITS_RAD']) == 6          # + the claw


def test_q3_is_g_offset_minus_g_on_the_elbow_up_branch():
    """The identity the analytic bound rests on, driven through the REAL
    solver: every returned q3 equals _G_OFFSET − g for some g >= 0, i.e. q3 is
    bounded above by _G_OFFSET and by nothing else that full extension cannot
    reach."""
    bound = ik._G_OFFSET
    smallest_g = math.inf
    for iz in range(9):
        z = 0.12 * iz / 8
        seed = _reachable_seed(z)
        if seed is None:
            continue
        # Walk INWARD from the bisected outer boundary: g → 0 lives there, and
        # a uniform-in-r grid does not resolve it.
        b = _boundary(z, seed, outward=True)
        for k in range(40):
            j = _solve_rz(b - k * 2e-5 if b is not None else seed, z)
            if j is None:
                continue
            g = bound - j[2]
            assert g >= -1e-12, (z, k, g)
            smallest_g = min(smallest_g, g)
    # Full extension really was approached: g = 0 is the bound, and it is
    # reachable inside the envelope (at the base plane).
    assert smallest_g < 1e-6, smallest_g


def test_no_returned_solution_exceeds_the_analytic_bound():
    """Elbow-down would carry q3 = _G_OFFSET + |g| (past the bound, toward
    tick 4095). It is in limits for a few targets but dominated by elbow-up,
    which ``solve()`` tries first — so no RETURNED q3 ever exceeds the bound,
    over a grid that reaches far below the base plane too."""
    bound = ik._G_OFFSET
    checked = 0
    for iz in range(31):
        z = -0.20 + 0.36 * iz / 30
        for ir in range(81):
            j = _solve_rz(0.01 + 0.40 * ir / 80, z)
            if j is None:
                continue
            checked += 1
            assert j[2] <= bound + 1e-9, (z, j[2])
    assert checked > 800, checked


# ── THE assertion: the ceiling versus the bound ──────────────────────────────

def test_the_ceiling_is_strictly_below_the_analytic_q3_bound():
    """THE guard. Raising ``_GOAL_EDGE_MARGIN_MAX_TICKS`` back to the 246 of
    an earlier arm — or to anything at or above 131 — fails here, because a
    margin >= the bound lets a legal override shift a real solver solution.

    Derived from the solver's ``_G_OFFSET`` and the driver's own rad→tick map,
    NOT from a sweep and NOT from a hardcoded angle."""
    bound = _analytic_q3_bound_ticks()
    assert bound == 131, (
        'the analytic q3 bound moved — re-derive the ceiling from _G_OFFSET, '
        f'got {bound}')
    assert _D['rad_to_tick'](ik._G_OFFSET, 1) == 3964
    assert _CEILING == 130
    assert _CEILING < bound, (
        f'_GOAL_EDGE_MARGIN_MAX_TICKS={_CEILING} is at or above the analytic '
        f'q3 bound of {bound} ticks: a legal margin would shift autonomous '
        f'grasp solutions. Set it below {bound}.')
    # A fence below the shipped default would make the default itself illegal.
    assert _DEFAULT <= _CEILING
    # At sign −1 the same q3 lands 132 ticks from tick 0 — never tighter.
    assert _edge(_D['rad_to_tick'](ik._G_OFFSET, -1)) == 132


def test_the_measured_minimum_over_the_operating_envelope_is_the_bound():
    """The densest MEASURED value, for the record and as a cross-check on the
    analytic bound: full extension (g = 0) is reachable at z = 0, so the
    tightest approach to a register end IS the bound — 131 ticks, joint3.

    Bisected, not gridded."""
    e, joint, rad, tick, r, z = _WORST[0], *_WORST[1][1:]
    assert (e, joint, tick) == (131, 3, 3964), _WORST
    assert rad == pytest.approx(ik._G_OFFSET, abs=1e-6)
    assert r == pytest.approx(0.370821, abs=1e-5)
    assert z == pytest.approx(0.0, abs=1e-6)
    assert _CEILING < e, (
        f'ceiling {_CEILING} >= measured minimum {e}: some real grasp '
        f'solution would be shifted')
    assert e == _analytic_q3_bound_ticks()
    # joint2 stays well clear (284 ticks at that corner).
    assert _PER_JOINT[2][0] > 2 * _CEILING


def test_no_swept_solution_moves_at_the_fenced_maximum_margin():
    """BEHAVIOUR, through the driver's own ``rad_to_command_tick``: at the
    ceiling — the largest margin an operator can legally set — every joint of
    every solution the sweep found is commanded bit-identically."""
    rct, rt = _D['rad_to_command_tick'], _D['rad_to_tick']
    for rec in _PER_JOINT.values():
        _e, _j, rad, tick, _r, _z = rec
        for margin in (_DEFAULT, _CEILING):
            assert rct(rad, 1, margin) == rt(rad, 1) == tick, (rec, margin)


def test_the_first_margin_that_shifts_the_bound_tick_is_two_above_the_ceiling():
    """Why the fence is a fence and not advice — stated precisely, because the
    off-by-one here is easy to get wrong: ``max(m, min(4095 − m, t))`` leaves
    ``t == 4095 − m`` alone, so a margin AT the analytic bound (131) still does
    not shift the bound tick; the first value that does is 132.

    The ceiling is 130 rather than 131 so that every commandable solver tick
    lies STRICTLY inside the band and the fence does not lean on that inclusive
    boundary."""
    rct = _D['rad_to_command_tick']
    bound = _analytic_q3_bound_ticks()
    rad = ik._G_OFFSET                          # the bound pose itself
    bound_tick = _LAST_TICK - bound
    assert _D['rad_to_tick'](rad, 1) == bound_tick
    assert rct(rad, 1, _CEILING) == bound_tick
    assert _CEILING < bound                     # strictly inside
    assert rct(rad, 1, bound) == bound_tick     # inclusive boundary, unshifted
    assert rct(rad, 1, bound + 1) == bound_tick - 1   # the first margin that bites


def test_the_shipped_default_clamps_above_the_bound():
    """The default 128 clamps at tick 3967 = 2.94371 rad, 3.3 ticks of headroom
    above the 2.938642-rad bound: the default and normal motion are unchanged."""
    clamp_tick = _LAST_TICK - _DEFAULT
    assert clamp_tick == 3967
    clamp_rad = (clamp_tick - _D['CENTER_TICK']) * _D['RAD_PER_TICK']
    assert clamp_rad == pytest.approx(2.94371, abs=1e-5)
    assert clamp_rad > ik._G_OFFSET
    assert (clamp_rad - ik._G_OFFSET) / _D['RAD_PER_TICK'] == pytest.approx(3.3, abs=0.05)


# ── the SCOPE of the bit-identity claim ──────────────────────────────────────

def test_the_seam_is_reached_only_far_below_the_base_plane():
    """The caveat that makes the claim honest, stated as a test so it cannot
    quietly stop being true. joint2's upper limit is +π, which maps onto the
    register end, and ``solve()`` CAN return it — but only for targets deep
    below the arm's base plane, where the workspace floor refuses long before
    IK is consulted. Within z >= 0 the sweep never gets within the ceiling of
    an end at all."""
    assert not _SEAM_ZS, (
        'a target at or above the base plane came within the fenced ceiling '
        f'of a register end: {sorted(set(_SEAM_ZS))[:5]}')
    deepest = first_in_band = None
    for iz in range(121):
        z = -0.30 + 0.30 * iz / 120
        seed = _reachable_seed(z)
        if seed is None:
            continue
        for k in range(120):
            j = _solve_rz(seed + 0.4 * k / 119, z)
            if j is None:
                continue
            edges = [_edge(_D['rad_to_tick'](v, 1)) for v in j]
            if min(edges) == 0:
                deepest = z if deepest is None else max(deepest, z)
            if min(edges) <= _CEILING:
                first_in_band = z if first_in_band is None else max(first_in_band, z)
    assert deepest is not None, 'expected the seam to be reachable below z=0'
    assert deepest < -0.20, deepest
    assert first_in_band is not None and first_in_band < -0.10, first_in_band

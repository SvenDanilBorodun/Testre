"""The commanded-goal edge clamp's CEILING, measured against the REAL solver.

WHY THIS FILE EXISTS AT ALL. ``_GOAL_EDGE_MARGIN_MAX_TICKS`` in
``robotis_ai_setup/docker/open_manipulator/edu6_arm_node.py`` fences the
``EDUBOTICS_EDU6_GOAL_EDGE_MARGIN_TICKS`` override, and its whole justification
is a claim about :class:`~physical_ai_server.workflow.edu6_ik.Edu6IKSolver`:
"a margin this large still leaves every autonomous grasp bit-identical". That
number was wrong THREE times in a row (367 → 263 → 256), every time because it
came from a sweep in the wrong coordinates, and every time the deps-free guard
in ``robotis_ai_setup/tests/test_feetech_bus.py`` stayed green — it recomputed
the ceiling from a HARDCODED joint angle, so the wrong INPUT was invisible.
The repo's own rule applies: a guard that cannot fail on the bug it was written
for is worse than no guard.

The driver lives on the other side of a dependency seam (deps-free, no numpy,
no rclpy) from the solver (numpy). Rather than teach the deps-free suite about
numpy, this file sits on the SOLVER's side and reaches ACROSS the seam the way
``test_robot_profiles.py`` already does for rclpy-importing modules: it
ast-extracts the driver's literals and its two tick-mapping functions, so the
arithmetic under test is the driver's own, not a copy of it.

WHY A ρ-SWEEP AND NOT A POINT CLOUD. ``solve()``'s q2/q3/q5 depend ONLY on
(ρ, ψ), the polar coordinates of the shoulder→wrist vector — q1 is bounded to
±90° (ticks 1024…3072), q4 ≡ 0 and q6 is jaw-folded into ±90°. So the joints
that can approach a register end are governed by two numbers, and ρ(γ) is
SQUARE-ROOT-SINGULAR at full extension (ρmax − ρ ≈ L2·L3·γ²/2ρmax). A grid
uniform in ρ (or in xyz) therefore resolves γ only as O(√Δρ): the corner sits at
γ = 0.0102 rad and a 6001-point ρ grid across the whole annulus lands no nearer
than γ = 0.0346, reporting 269 — and throwing points at it improves only as the
square root, which is how a 977,808-point xyz cloud still reported 256. Here the
extremum is found by BISECTING the reach boundary with ``solve()`` itself and
then walking inward in 5 µm steps, so no grid pitch has to be trusted.

WHAT IS PINNED
  * q3 = γ + ALPHA0 − π/2 exactly (γ ≥ 0 on the elbow-up branch), so q3 has an
    UNCONDITIONAL analytic floor of ALPHA0 − π/2 = −158.3317° ⇒ tick 247, and
    the ceiling must sit strictly below it. An analytic bound cannot become a
    fourth sweep artifact.
  * The measured minimum over the operating envelope (target at or above the
    base plane, z ≥ 0) is 253 ticks, at the corner where the z = 0 plane meets
    joint5's +110° relief limit.
  * The documented SCOPE of the bit-identity claim: joint2 CAN be driven onto
    the seam, but only for targets far BELOW the base plane, which the
    workspace floor refuses long before IK is consulted.
"""

from __future__ import annotations

import ast
import math
import textwrap
from pathlib import Path

import pytest

from physical_ai_server.workflow import edu6_ik as ik

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DRIVER_PY = (_REPO_ROOT / 'robotis_ai_setup' / 'docker' / 'open_manipulator'
              / 'edu6_arm_node.py')

_WANT_CONSTS = ('TICKS_PER_REV', 'CENTER_TICK', 'RAD_PER_TICK',
                'JOINT_LIMITS_RAD', '_GOAL_EDGE_MARGIN_MAX_TICKS',
                '_DEFAULT_GOAL_EDGE_MARGIN_TICKS')
_WANT_FUNCS = ('rad_to_tick', 'rad_to_command_tick')


def _driver_namespace():
    """ast-extract the driver's tick map + the fenced constants.

    ``edu6_arm_node`` imports rclpy and ``feetech_bus`` at module scope, so it
    cannot be imported here; the same trick ``test_robot_profiles.py`` uses for
    the rclpy-importing server modules applies. Extracting the FUNCTIONS (not
    reimplementing them) is the point — a rounding change in the driver's
    rad→tick map must move this test's numbers too."""
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


def _analytic_q3_floor_tick() -> int:
    """``solve()`` sets q3 = γ + ALPHA0 − π/2 with γ ≥ 0 on the elbow-up branch
    (identity pinned below), so the smallest q3 any solution can carry is
    ALPHA0 − π/2 at full extension. Derived from the solver's OWN
    import-time-derived ``_ALPHA0`` through the DRIVER's OWN tick map — no
    hardcoded angle, no sweep."""
    return _D['rad_to_tick'](ik._ALPHA0 - math.pi / 2.0, 1)


# ── the sweep (module-level, computed once — ~5 k solve() calls, ~1 s) ────────

_SOLVER = ik.Edu6IKSolver()
_X0 = ik.BASE_AXIS_X_WORLD


def _solve_rz(r: float, z: float):
    """``solve()`` at world radius ``r`` (from the joint-1 axis) and height
    ``z``, on the y = 0 meridian. q1 = 0 there and q1 does not enter the
    (ρ, ψ) geometry, so this loses nothing."""
    return _SOLVER.solve((_X0 + r, 0.0, z), roll=0.0)


def _boundary(z: float, seed: float, outward: bool) -> float | None:
    """Bisect the reach boundary at height ``z`` starting from a reachable
    ``seed`` radius. Driven purely by ``solve() is not None`` — the module
    docstring's whole point is that no grid pitch is trusted here."""
    if _solve_rz(seed, z) is None:
        return None
    lo, hi = seed, (0.35 if outward else 0.0)
    for _ in range(50):
        mid = 0.5 * (lo + hi)
        if _solve_rz(mid, z) is None:
            hi = mid
        else:
            lo = mid
    return lo


def _reachable_seed(z: float) -> float | None:
    """Any reachable radius at height ``z`` (coarse scan; the bisection needs a
    starting point and the reachable band's inner edge rises with z)."""
    for k in range(240):
        r = 0.01 + 0.24 * k / 239
        if _solve_rz(r, z) is not None:
            return r
    return None


def _sweep():
    """``(worst, per_joint, seam_zs)`` over the operating envelope z >= 0 plus a
    below-the-plane probe.

    ``worst`` is ``(edge_distance, joint_index_1based, degrees, tick, r, z)``
    for the tightest approach to a register end; ``per_joint`` the same keyed by
    joint; ``seam_zs`` the heights at which ANY solution lands within the
    fenced ceiling of an end (used to scope the claim, not to bound it)."""
    worst = (10 ** 9, None)
    per_joint: dict[int, tuple] = {}
    seam_zs: list[float] = []

    def note(joints, r, z):
        nonlocal worst
        for i, v in enumerate(joints):
            tick = _D['rad_to_tick'](v, 1)
            e = _edge(tick)
            rec = (e, i + 1, math.degrees(v), tick, r, z)
            if e < worst[0]:
                worst = (e, rec)
            if e < per_joint.get(i + 1, (10 ** 9,))[0]:
                per_joint[i + 1] = rec
            if e <= _CEILING:
                seam_zs.append(z)

    # A) coarse global grid over the envelope — nothing outside the reach
    #    boundaries can be tighter, but this is what makes that falsifiable.
    for iz in range(41):
        z = 0.08 * iz / 40
        for ir in range(61):
            r = 0.02 + 0.22 * ir / 60
            j = _solve_rz(r, z)
            if j:
                note(j, r, z)

    # B) the razor edge: bisect BOTH reach boundaries at a ladder of heights
    #    and walk 1 mm inward in 5 um steps (0.33 ticks of q3 per step).
    for iz in range(11):
        z = 0.066 * iz / 10
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
    """The analytic floor is a statement about ``solve()``'s limit set. The
    driver clamps commands in radians to ITS copy before converting, so the two
    must agree or the floor says nothing about what is commanded."""
    driver_arm = tuple(tuple(b) for b in _D['JOINT_LIMITS_RAD'][:6])
    solver_arm = tuple(tuple(b) for b in ik._EDU6_JOINT_LIMITS_RAD)
    assert driver_arm == solver_arm
    assert len(_D['JOINT_LIMITS_RAD']) == 7          # + the gripper


def test_q3_is_gamma_plus_alpha0_minus_half_pi_on_the_elbow_up_branch():
    """The identity the analytic floor rests on, driven through the REAL solver:
    every returned q3 equals γ + ALPHA0 − π/2 for some γ >= 0, i.e. q3 is
    bounded below by ALPHA0 − π/2 and by nothing else.

    (The identity itself holds on the elbow-DOWN branch too, where γ < 0 would
    push q3 past −180°. That branch is never returned — pinned separately.)"""
    floor = ik._ALPHA0 - math.pi / 2.0
    smallest_gamma = math.inf
    for iz in range(9):
        z = 0.06 * iz / 8
        seed = _reachable_seed(z)
        if seed is None:
            continue
        # Walk INWARD from the bisected outer boundary: γ → 0 lives there, and
        # a uniform-in-r grid does not resolve it (that is the whole lesson).
        b = _boundary(z, seed, outward=True)
        for k in range(40):
            j = _solve_rz(b - k * 2e-5 if b is not None else seed, z)
            if j is None:
                continue
            gamma = j[2] - floor
            assert gamma >= -1e-12, (z, k, gamma)
            smallest_gamma = min(smallest_gamma, gamma)
    # Full extension really was approached: γ = 0 is the floor, and the closest
    # the envelope allows is joint5's relief corner at γ ≈ 0.0102 rad.
    assert smallest_gamma < 0.011, smallest_gamma


def test_the_elbow_down_branch_is_never_returned():
    """Elbow-down is what would break the floor (q3 down toward −180° ⇒ tick
    0). ``solve()`` tries elbow-up first and only falls through on a limit
    violation; over the whole reach annulus that fall-through never yields an
    in-limit pose, so the floor is unconditional."""
    floor = ik._ALPHA0 - math.pi / 2.0
    checked = 0
    for iz in range(31):
        z = -0.20 + 0.28 * iz / 30
        for ir in range(61):
            j = _solve_rz(0.01 + 0.24 * ir / 60, z)
            if j is None:
                continue
            checked += 1
            assert j[2] >= floor - 1e-9, (z, j[2])
    assert checked > 800, checked


# ── THE assertion: the ceiling versus the floor ──────────────────────────────

def test_the_ceiling_is_strictly_below_the_analytic_q3_floor():
    """THE guard. Raising ``_GOAL_EDGE_MARGIN_MAX_TICKS`` back to 256 — or to
    anything at or above 247 — fails here, because a margin >= the floor lets a
    legal override shift a real solver solution.

    Derived from the solver's ``_ALPHA0`` and the driver's own rad→tick map,
    NOT from a sweep and NOT from a hardcoded angle: that is what stops this
    number becoming a fourth artifact."""
    floor = _analytic_q3_floor_tick()
    assert floor == 247, (
        'the analytic q3 floor moved — re-derive the ceiling from ALPHA0, '
        f'got {floor}')
    assert _CEILING < floor, (
        f'_GOAL_EDGE_MARGIN_MAX_TICKS={_CEILING} is at or above the analytic '
        f'q3 floor of {floor} ticks: a legal margin would shift autonomous '
        f'grasp solutions. Set it below {floor}.')
    # A fence below the shipped default would make the default itself illegal.
    assert _DEFAULT <= _CEILING


def test_the_measured_minimum_over_the_operating_envelope_is_253_ticks():
    """The densest MEASURED value, for the record and as a cross-check on the
    analytic floor: 253 ticks, at joint3, where the z = 0 plane meets joint5's
    +110° relief limit near full extension.

    Bisected, not gridded — the previous 367 / 263 / 256 all came from grids
    uniform in the wrong coordinate."""
    e, joint, deg, tick, r, z = _WORST[0], *_WORST[1][1:]
    assert (e, joint, tick) == (253, 3, 253), _WORST
    assert deg == pytest.approx(-157.7493, abs=1e-3)
    assert r == pytest.approx(0.215519, abs=1e-5)
    assert z == pytest.approx(0.0, abs=1e-6)
    assert _CEILING < e, (
        f'ceiling {_CEILING} >= measured minimum {e}: some real grasp '
        f'solution would be shifted')
    # The floor is a genuine bound, not a coincidence, and not slack enough to
    # hide a regression: the measurement sits just above it.
    assert _analytic_q3_floor_tick() <= e <= _analytic_q3_floor_tick() + 16


def test_no_swept_solution_moves_at_the_fenced_maximum_margin():
    """BEHAVIOUR, through the driver's own ``rad_to_command_tick``: at the
    ceiling — the largest margin an operator can legally set — every joint of
    every solution the sweep found is commanded bit-identically."""
    rct, rt = _D['rad_to_command_tick'], _D['rad_to_tick']
    for rec in _PER_JOINT.values():
        _e, _j, deg, tick, _r, _z = rec
        rad = math.radians(deg)
        for margin in (_DEFAULT, _CEILING):
            assert rct(rad, 1, margin) == rt(rad, 1) == tick, (rec, margin)


def test_the_first_margin_that_shifts_the_floor_tick_is_two_above_the_ceiling():
    """Why the fence is a fence and not advice — stated precisely, because the
    off-by-one here is easy to get wrong: ``max(m, min(4095 − m, t))`` leaves
    ``t == m`` alone, so a margin AT the analytic floor (247) still does not
    shift the floor tick; the first value that does is 248.

    The ceiling is 246 rather than 247 so that every commandable solver tick
    lies STRICTLY inside the band and the fence does not lean on that inclusive
    boundary."""
    rct = _D['rad_to_command_tick']
    floor = _analytic_q3_floor_tick()
    rad = ik._ALPHA0 - math.pi / 2.0            # the floor pose itself
    assert _D['rad_to_tick'](rad, 1) == floor
    assert rct(rad, 1, _CEILING) == floor
    assert _CEILING < floor                     # strictly inside
    assert rct(rad, 1, floor) == floor          # inclusive boundary, unshifted
    assert rct(rad, 1, floor + 1) == floor + 1  # the first margin that bites


# ── the SCOPE of the bit-identity claim ──────────────────────────────────────

def test_joint2_reaches_the_seam_but_only_far_below_the_base_plane():
    """The caveat that makes the claim honest, stated as a test so it cannot
    quietly stop being true. joint2's tick CAN reach 4095 exactly — q2's upper
    limit is +π, which maps onto the register end — but only for targets deep
    below the arm's base plane, where the workspace floor refuses long before
    IK is consulted. Within z >= 0 the sweep never gets within the ceiling of
    an end at all."""
    assert not _SEAM_ZS, (
        'a target at or above the base plane came within the fenced ceiling '
        f'of a register end: {sorted(set(_SEAM_ZS))[:5]}')
    deepest = None
    for iz in range(61):
        z = -0.22 + 0.22 * iz / 60
        seed = _reachable_seed(z)
        if seed is None:
            continue
        for k in range(60):
            j = _solve_rz(seed + 0.2 * k / 59, z)
            if j is None:
                continue
            if any(_edge(_D['rad_to_tick'](v, 1)) == 0 for v in j):
                deepest = z if deepest is None else max(deepest, z)
    assert deepest is not None, 'expected the seam to be reachable below z=0'
    assert deepest < -0.07, deepest

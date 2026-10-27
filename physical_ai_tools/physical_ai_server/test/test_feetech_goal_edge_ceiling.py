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

WHAT IS PINNED (since the 2026-10 centring: every joint's encoder zero T0 puts
the middle of its URDF range on tick 2048, see ``feetech_servo_spec.py``; the
tick direction is not measured yet, so every one of the four combinations is
checked with the spec's own signs and T0)
  * q3 = _G_OFFSET − g exactly (g ≥ 0 on the elbow-up branch, the only one ever
    returned), so q3 has an UNCONDITIONAL analytic bound q3 ≤ _G_OFFSET =
    2.938642 rad ⇒ 1156 ticks from a register end when joint3's sign is −1,
    1155 when it is +1. The driver's ceiling (130, owner decision b1) sits far
    below both.
  * The measured minimum over the operating envelope (z ≥ 0) is joint4 at its
    limit: 795 ticks (777 ticks rise CCW) or 796 (CW).
  * No solution at ANY height comes within the ceiling of a register end — the
    seam the old 2048-zero map put at joint2's +π is gone.
"""

from __future__ import annotations

import ast
import importlib
import math
import sys
import textwrap
from pathlib import Path

from physical_ai_server.workflow import v1_ik as ik

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DRIVER_DIR = _REPO_ROOT / 'robotis_ai_setup' / 'docker' / 'open_manipulator'
_DRIVER_PY = _DRIVER_DIR / 'feetech_arm_node.py'

_WANT_CONSTS = ('TICKS_PER_REV', 'RAD_PER_TICK',
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


def _servo_specs():
    """The four pinned tick-direction combinations of the driver's servo spec
    (``feetech_servo_spec.py``, plain Python next to the driver), keyed by
    ``(777 direction, 2825 direction)``."""
    sys.path.insert(0, str(_DRIVER_DIR))
    try:
        fss = importlib.import_module('feetech_servo_spec')
    finally:
        sys.path.remove(str(_DRIVER_DIR))
    return {(a, b): fss.build_spec({777: a, 2825: b}, fss.FACTORY,
                                   fss.MOTION_TEST_WINDOW_MS, fss.MAX_TORQUE,
                                   frozenset())
            for a in (1, -1) for b in (1, -1)}


_D = _driver_namespace()
_SPECS = _servo_specs()
_LAST_TICK = _D['TICKS_PER_REV'] - 1
_CEILING = _D['_GOAL_EDGE_MARGIN_MAX_TICKS']
_DEFAULT = _D['_DEFAULT_GOAL_EDGE_MARGIN_TICKS']


def _edge(tick: int) -> int:
    """Ticks from the NEARER register end — what the clamp actually bounds."""
    return min(tick, _LAST_TICK - tick)


def _tick(spec, joint_index: int, rad: float) -> int:
    """The driver's own rad→tick map with the spec's sign and T0."""
    return _D['rad_to_tick'](rad, spec.signs[joint_index],
                             t0=spec.t0[joint_index])


def _analytic_q3_bound_ticks(spec) -> int:
    """``solve()`` sets q3 = _G_OFFSET − g with g ≥ 0 on the elbow-up branch
    (identity pinned below), so the LARGEST q3 any solution can carry is
    _G_OFFSET at full extension. Derived from the solver's OWN import-time
    ``_G_OFFSET`` through the DRIVER's OWN tick map — no hardcoded angle, no
    sweep. Returned as the distance to the register end it approaches."""
    return _edge(_tick(spec, 2, ik._G_OFFSET))


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


def _solutions():
    """Every solution ``(joints, r, z)`` of the sweep over the operating
    envelope z >= 0."""
    found = []

    # A) coarse global grid over the envelope — nothing outside the reach
    #    boundaries can be tighter, but this is what makes that falsifiable.
    for iz in range(41):
        z = 0.16 * iz / 40
        for ir in range(81):
            r = 0.02 + 0.40 * ir / 80
            j = _solve_rz(r, z)
            if j:
                found.append((j, r, z))

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
                    found.append((j, r, z))
    return found


def _sweep(spec, solutions):
    """``(worst, per_joint, seam_zs)`` of ``solutions`` under ``spec``'s map.

    ``worst`` is ``(edge_distance, record)`` for the tightest approach to a
    register end, a record being ``(edge, joint_1based, radians, tick, r, z)``;
    ``per_joint`` the same keyed by joint; ``seam_zs`` the heights at which ANY
    solution lands within the fenced ceiling of an end."""
    worst = (10 ** 9, None)
    per_joint: dict[int, tuple] = {}
    seam_zs: list[float] = []
    for joints, r, z in solutions:
        for i, v in enumerate(joints):
            tick = _tick(spec, i, v)
            e = _edge(tick)
            rec = (e, i + 1, v, tick, r, z)
            if e < worst[0]:
                worst = (e, rec)
            if e < per_joint.get(i + 1, (10 ** 9,))[0]:
                per_joint[i + 1] = rec
            if e <= _CEILING:
                seam_zs.append(z)
    return worst, per_joint, seam_zs


_SOLUTIONS = _solutions()
_SWEEPS = {key: _sweep(spec, _SOLUTIONS) for key, spec in _SPECS.items()}


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
    an earlier arm is still refused (owner decision b1), and the analytic q3
    bound — derived from the solver's ``_G_OFFSET`` and the driver's own
    rad→tick map with the spec's sign and T0, NOT from a sweep and NOT from a
    hardcoded angle — sits far above it in every combination."""
    assert _CEILING == 130
    assert _DEFAULT <= _CEILING
    for (_d777, d2825), spec in _SPECS.items():
        bound = _analytic_q3_bound_ticks(spec)
        # joint3's sign is −1 × the 2825 direction (its output axis is
        # mirrored against the URDF axis)
        assert spec.signs[2] == -d2825
        assert bound == (1156 if spec.signs[2] == -1 else 1155), (d2825, bound)
        assert _CEILING < bound, (
            f'_GOAL_EDGE_MARGIN_MAX_TICKS={_CEILING} is at or above the '
            f'analytic q3 bound of {bound} ticks: a legal margin would shift '
            f'autonomous grasp solutions.')


def test_the_measured_minimum_over_the_operating_envelope_is_joint4_at_its_limit():
    """The densest MEASURED value over z >= 0, bisected and not gridded: the
    tightest approach to a register end is joint4 at its URDF limit — 795 ticks
    when 777 ticks rise CCW (tick 3300), 796 when they rise CW (tick 796) —
    and per joint joint2 1024 / 1023 and joint3 1156 / 1155 (2825 CCW / CW)."""
    for (d777, d2825), (worst, per_joint, _seam) in _SWEEPS.items():
        e, joint, _rad, tick, _r, _z = worst[1]
        assert (e, joint, tick) == ((795, 4, 3300) if d777 == 1
                                    else (796, 4, 796)), (d777, d2825, worst)
        assert per_joint[2][0] == (1024 if d2825 == 1 else 1023), per_joint[2]
        assert per_joint[3][0] == (1156 if d2825 == 1 else 1155), per_joint[3]
        assert per_joint[3][0] == _analytic_q3_bound_ticks(_SPECS[(d777, d2825)])
        assert _CEILING < e


def test_no_swept_solution_moves_at_the_fenced_maximum_margin():
    """BEHAVIOUR, through the driver's own ``rad_to_command_tick``: at the
    ceiling — the largest margin an operator can legally set — every joint of
    every solution the sweep found is commanded bit-identically, in every
    combination, with each joint's own sign and T0."""
    rct, rt = _D['rad_to_command_tick'], _D['rad_to_tick']
    for key, spec in _SPECS.items():
        for joints, _r, _z in _SOLUTIONS:
            for i, rad in enumerate(joints):
                sign, t0 = spec.signs[i], spec.t0[i]
                plain = rt(rad, sign, t0=t0)
                for margin in (_DEFAULT, _CEILING):
                    assert rct(rad, sign, margin, t0=t0) == plain, (key, i, rad)


def test_the_first_margin_that_shifts_the_bound_tick_is_one_above_the_bound():
    """Why the fence is a fence and not advice — stated precisely, because the
    off-by-one here is easy to get wrong: ``max(m, min(4095 − m, t))`` leaves a
    tick ``bound`` ticks from its end alone at ``m == bound`` (the band
    boundary is inclusive); the first margin that moves it is ``bound + 1``."""
    rct = _D['rad_to_command_tick']
    rad = ik._G_OFFSET                          # the bound pose itself
    for key, spec in _SPECS.items():
        sign, t0 = spec.signs[2], spec.t0[2]
        bound = _analytic_q3_bound_ticks(spec)
        tick = _tick(spec, 2, rad)
        assert _edge(tick) == bound
        assert rct(rad, sign, _CEILING, t0=t0) == tick
        assert rct(rad, sign, bound, t0=t0) == tick, key    # inclusive, unshifted
        moved = rct(rad, sign, bound + 1, t0=t0)
        assert moved != tick and abs(moved - tick) == 1, key  # the first that bites


def test_the_clamp_band_contains_every_centred_window():
    """The default 128 clamps commands into [128, 3967]; every centred EEPROM
    window lies inside that band, so the default never shortens a window."""
    assert (_DEFAULT, _LAST_TICK - _DEFAULT) == (128, 3967)
    for spec in _SPECS.values():
        for lo, hi in spec.windows:
            assert _DEFAULT <= lo and hi <= _LAST_TICK - _DEFAULT, (lo, hi)


# ── the SCOPE of the bit-identity claim ──────────────────────────────────────

def test_no_solution_comes_within_the_ceiling_of_a_register_end_at_any_height():
    """Before the centring joint2's +π limit mapped onto the register end and
    ``solve()`` returned it for targets deep below the base plane (the caveat
    the old claim needed). Now no solution at ANY height in z ∈ [−0.30, 0.16]
    comes within the fenced ceiling of a register end, in any combination."""
    for _key, (_worst, _per, seam) in _SWEEPS.items():
        assert not seam, sorted(set(seam))[:5]
    checked = 0
    for iz in range(185):
        z = -0.30 + 0.46 * iz / 184
        seed = _reachable_seed(z)
        if seed is None:
            continue
        for k in range(120):
            j = _solve_rz(seed + 0.4 * k / 119, z)
            if j is None:
                continue
            checked += 1
            for key, spec in _SPECS.items():
                edges = [_edge(_tick(spec, i, v)) for i, v in enumerate(j)]
                assert min(edges) > _CEILING, (key, z, j, edges)
    assert checked > 1000, checked

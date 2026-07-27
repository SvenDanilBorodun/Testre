#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Route the arm to the Grundstellung without pressing it into the table.

WHAT THIS FIXES. ``motion.home`` used to hand one straight joint-space line
straight to ``safe_move``. With no Sperrzone drawn — the normal case —
``safe_move`` is a pass-through, so nothing judged that line at all. Measured on
the collision meshes: from start poses the arm can physically be in, it drives a
link **up to 167.9 mm below the table** in roughly 1 in 100 cases (0.3 %–1.9 %
across seven samples). The failing family is sharp and repeatable — elbow
essentially straight (``j3 ∈ [−0.32, −0.01]``) with the shoulder rotated back
(``j2 ∈ [+0.76, +3.13]``), i.e. the arm doubled over backwards with the gripper
already near the table; swinging ``j2`` forward then drags the whole tool
through the table plane. That is also what a LIMP arm collapses into.

THE LADDER, and why it is this shape rather than a smarter one:

* **L0 direct.** Measured to answer 588/600 (98 %). Every leg-splitting
  heuristic tried was WORSE than direct — elbow-first 4/120 presses against
  direct's 0/120, wrist-neutral-first 23/120. So the design is "check the
  straight line, and only search when the check fails", never "reorder the
  joints by default". (The bench tool's elbow-first ``DRIVE_ORDER`` is right for
  its one-joint-at-a-time drive and wrong for a simultaneous glide.)
* **L1/L2 monotone escape.** No FIXED via rescues the failures — the best of
  five families managed 7/34, and a brute-force via over the whole ``(j2, j3,
  j5)`` lattice keeping the other joints reached only 18/34. The via has to be
  SEARCHED. A bounded greedy ascent on the model's floor clearance does it.
* **L3 refuse, naming the real reason.** A refusal here is a dead end for a
  student, so it must name the remedy that actually works — hand-guiding the arm
  up, which needs no calibration and exists on every rig.

MONOTONE ADMISSIBILITY on the FIRST leg is the load-bearing idea, and the two
legs are deliberately held to DIFFERENT bounds:

* the **lift leg** may sit as low as the arm ALREADY is
  (``min(start clearance, allowance)``) — the arm is standing there right now,
  and a guard must not forbid climbing out of a hole it did not dig;
* every **later leg** must clear the allowance outright.

Requiring the absolute allowance on leg 1 too rejects EVERY candidate whenever
the start is already below it — which is exactly this rung's population. That
was implemented first and measured: the rung fired ZERO times. The consequence,
stated plainly: this planner can never make the arm's situation worse than where
it already stands, but on a start that is already too low it also cannot make it
instantly good. It gets it out, then home.

Measured end to end over 600 attainable starts, every planned route then
verified on the meshes: rungs L0 588 / L1 4 / REFUSE 8; **0 routes press the
table** (baseline 8); planning cost 4.5 ms. Re-checked at the REAL 30 Hz
``build_segment`` waypoint density (90 waypoints, velocity floor and all):
487 accepted routes, 0 press, worst +5.33 mm.

Rule §2 note: this is a Roboter-Studio *workflow-level* guard, the same class as
``WORKSPACE_FLOOR_MARGIN_M`` and ``path_guard``'s Sperrzone reroute. It runs only
on the workflow runtime, never reshapes a recorded or replayed action, and is
inert on every arm without a box table (see ``arm_geometry.resolve_geometry``).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import numpy as np


_logger = logging.getLogger(__name__)


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    """Read a metre-valued knob, degrading loudly to ``default``.

    Empty/whitespace counts as UNSET, silently: compose forwards optional knobs
    as ``${VAR:-}`` so every un-tuned rig delivers ``''`` — the
    ``EDUBOTICS_GRASP_HELD_MAX_RAD`` scar. A malformed or negative value is
    operator error and warns once in German.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        _logger.warning(
            '[WARNUNG] %s=%r ist keine Zahl — der Standardwert %s wird '
            'verwendet.', name, raw, default)
        return default
    if not np.isfinite(value) or value < minimum:
        _logger.warning(
            '[WARNUNG] %s=%r muss >= %s sein (0 schaltet die Prüfung ab) — der '
            'Standardwert %s wird verwendet.', name, raw, minimum, default)
        return default
    return value


# How far BELOW the calibrated table the MODEL may read before a route is
# rejected. NOT ``WORKSPACE_FLOOR_MARGIN_M`` (0.01) and must not be conflated
# with it: that one is a margin on a CARTESIAN fingertip target, this one is an
# allowance for the box model's own conservatism on WHOLE-LINK geometry.
#
# Derived, not guessed. The box model is sound but conservative by a median
# 0.0 mm / mean 10.1 mm / p95 45.9 mm. Measured over 800 attainable starts:
#   threshold   flagged        real mesh presses caught
#   0 mm        51 (6.4 %)     12/12
#   20 mm       16 (2.0 %)     12/12     <- the knee
#   50 mm       11 (1.4 %)     11/12     <- starts MISSING real presses
# 20 mm catches every real press with four false positives in eight hundred.
#
# ``EDUBOTICS_HOME_FLOOR_TOL_M=0`` DISABLES the floor rung entirely — the
# one-variable rollback. (0 is the disable value because the check becomes
# "never below the table at all", which no real route satisfies... so it is
# special-cased explicitly below rather than left to arithmetic.)
FLOOR_TOL_M = _env_float('EDUBOTICS_HOME_FLOOR_TOL_M', 0.020)

# Self-collision is WARN-ONLY this round and never gates a rung. Measured
# 0/200 mesh self-collisions among random in-limit configs, and 0/200 on home
# glides; a directed 21,175-config search re-checked all 7,410 candidates whose
# conservative bound reached 0 and found no penetration (tightest +0.062 mm).
# A refusal built on a rate measured as ZERO would be all cost and no benefit,
# and promoting it is a Rule §2 change needing sign-off. 0 disables the warning.
SELFCOLL_WARN_M = _env_float('EDUBOTICS_HOME_SELFCOLL_WARN_M', 0.010)

# Via-search shape. Plain constants, NOT env vars: a knob nobody will turn
# should not become a name that needs a compose forward (the
# ``WAIT_UNTIL_MAX_SECONDS`` precedent). Tests monkeypatch them.
#
# The lattice varies the three joints that move the arm IN ITS OWN PLANE
# (shoulder, elbow, wrist pitch) and KEEPS the student's base yaw and both
# rolls. Measured basis: a brute-force via over exactly this family rescues
# ~53 % of the failing starts, while no FIXED via family managed better than
# 7/34 — the via has to be searched, but it does not have to be searched in six
# dimensions.
_VIA_LATTICE_J2 = 13
_VIA_LATTICE_J3 = 13
_VIA_LATTICE_J5 = 7
# How many lattice points get the (expensive) two swept leg checks.
#
# Measured trade, 600 attainable starts: uncapped rescues 6 of the 12 failing
# starts but costs 6.4 s mean / 11.2 s max — unacceptable latency for a block a
# student pressed. Capped at 24 it rescues 4 and costs 207 ms mean / 284 ms max.
# The safety outcome is IDENTICAL either way (0 table presses on every planned
# route, worst +0.98 mm); the only difference is whether the other 2 starts get
# a route or a refusal-with-a-remedy. 25x the latency for two rescues out of 600
# is the wrong trade for a student-facing block.
_VIA_TEST_BUDGET = 24
# Duration for a via leg. Shorter than a full home move because it is a small,
# deliberate lift; ``build_segment``'s velocity floor extends it whenever the
# joint delta needs it, so this can never make a move too fast.
_ESCAPE_DURATION_S = 1.5

_FLOOR_REFUSAL_DE = (
    'Der Arm steht so, dass der Weg in die Grundstellung durch die Tischebene '
    'führen würde. Bitte den Arm mit der Handführung ein Stück anheben und es '
    'dann erneut versuchen.'
)


def _floor_fn(ctx):
    """``(x, y) -> table height``, from the touch-off when there is one.

    Falls back to ``0.0`` rather than to "unknown", and that is deliberate: on
    this arm z = 0 is not a guess, it is where ``base_link`` is bolted (its mesh
    spans z ∈ [0.0000, 0.0625] m). So the floor guard works on an UNCALIBRATED
    rig too — unlike the Cartesian floor, which has nothing to compare against
    until „Tisch vermessen" has run.
    """
    from physical_ai_server.workflow.handlers import motion as _m

    def _at(x: float, y: float) -> float:
        value = _m._floor_z_at(ctx, x, y)
        return 0.0 if value is None else float(value)

    return _at


def _leg_ok(geom, ctx, q_from, q_to, floor_fn, allowance: float) -> bool:
    """True when the straight line ``q_from → q_to`` clears the floor allowance
    AND no Sperrzone. Geometry the model cannot judge (``None``) is treated as
    NOT-OK: an unknown is never silently an approval on a safety check."""
    from physical_ai_server.workflow import path_guard as _pg
    clearance = geom.swept_floor_clearance(q_from, q_to, floor_fn)
    if clearance is None or clearance < allowance:
        return False
    zones = getattr(ctx, 'zones', None)
    if zones and _pg.segment_blocked(ctx.ik, q_from, q_to, zones):
        return False
    return True


def _via_candidates(ctx, q_start, home_arm, geom, floor_fn,
                    allowance: float) -> list:
    """Lattice via-points that CLEAR the floor on their own, ordered by how
    likely they are to work.

    Varies (j2, j3, j5) — the joints that move the arm in its own plane — and
    keeps the student's j1/j4/j6.

    ORDERING is a hybrid, and deliberately so. Half the budget goes to vias
    NEAREST HOME (a short, safe second leg) and half to the HIGHEST vias (a big
    lift is what the doubled-over family actually needs). Measured: neither
    ordering alone beats the other, and neither reaches the uncapped rescue
    count — which is why the budget, not the ordering, is the documented trade.
    """
    n = ctx.ik.num_joints()
    limits = ctx.ik.joint_limits
    base = [float(v) for v in list(q_start)[:n]]
    grid = []
    for j2 in np.linspace(limits[1][0], limits[1][1], _VIA_LATTICE_J2):
        for j3 in np.linspace(limits[2][0], limits[2][1], _VIA_LATTICE_J3):
            for j5 in np.linspace(limits[4][0], limits[4][1], _VIA_LATTICE_J5):
                via = list(base)
                via[1], via[2], via[4] = float(j2), float(j3), float(j5)
                clearance = geom.floor_clearance(via, floor_fn)
                if clearance is None or clearance < allowance:
                    continue
                span = max(abs(via[i] - home_arm[i]) for i in range(n))
                grid.append((span, clearance, via))
    half = _VIA_TEST_BUDGET // 2
    nearest = sorted(grid, key=lambda t: t[0])[:half]
    highest = sorted(grid, key=lambda t: -t[1])[:_VIA_TEST_BUDGET - half]
    out: list = []
    seen: set = set()
    for _span, _clear, via in nearest + highest:
        key = tuple(round(v, 6) for v in via)
        if key in seen:
            continue
        seen.add(key)
        out.append(via)
    return out


def _warn_self_collision(ctx, geom, legs) -> None:
    """Emit ONE German [WARNUNG] if the route brings two non-adjacent links
    close. WARN-ONLY BY DESIGN — it never changes the route and never blocks a
    rung. Promoting it to a refusal is a Rule §2 change (see the module note)."""
    if SELFCOLL_WARN_M <= 0.0:
        return
    log = getattr(ctx, 'log', None)
    if not callable(log):
        return
    worst = None
    for q_from, q_to, _dur in legs:
        value = geom.swept_self_clearance(q_from, q_to)
        if value is None:
            continue
        if worst is None or value < worst:
            worst = value
    if worst is not None and worst < SELFCOLL_WARN_M:
        log('[WARNUNG] Auf dem Weg in die Grundstellung kommen sich zwei Teile '
            f'des Arms sehr nahe (etwa {worst * 1000:.0f} mm). Die Fahrt wird '
            'trotzdem ausgeführt — bitte den Arm dabei beobachten.')


def plan_home_route(ctx, q_start, q_home_full, duration_s: float) -> list:
    """Legs ``[(q_from, q_to, duration), …]`` that reach the Grundstellung
    without driving a link through the table, or raise ``WorkflowError``.

    Backward-safe: an arm with no box table (every OMX profile, every
    profile-less ctx, every test double) gets exactly ``[(q_start, q_home_full,
    duration_s)]`` — one leg, byte-identical to today. So does any rig with
    ``EDUBOTICS_HOME_FLOOR_TOL_M=0``.
    """
    from physical_ai_server.workflow import arm_geometry as _ag
    from physical_ai_server.workflow.handlers import motion as _m

    direct = [(list(q_start), list(q_home_full), duration_s)]
    geom = _ag.resolve_geometry(ctx)
    if geom is None or FLOOR_TOL_M <= 0.0:
        return direct

    floor_fn = _floor_fn(ctx)
    allowance = -FLOOR_TOL_M

    # L0 — direct. The 98 % case, and the one the measurements say to keep.
    if _leg_ok(geom, ctx, q_start, q_home_full, floor_fn, allowance):
        _warn_self_collision(ctx, geom, direct)
        return direct

    # L1 — lift over a searched via, then direct from there.
    n = ctx.ik.num_joints()
    gripper = (float(q_start[n]) if len(q_start) > n
               else float(q_home_full[n]))
    home_arm = [float(v) for v in list(q_home_full)[:n]]
    start_clearance = geom.floor_clearance(list(q_start)[:n], floor_fn)
    # MONOTONE admissibility for the FIRST leg: the arm may always move AWAY
    # from trouble, never deeper into it. Requiring the absolute allowance here
    # instead rejects every candidate whenever the start is ALREADY below it —
    # which is exactly this rung's population, and it was measured firing zero
    # times in that form.
    escape_allowance = allowance
    if start_clearance is not None:
        escape_allowance = min(start_clearance, allowance) - 1e-9
    for via_arm in _via_candidates(ctx, q_start, home_arm, geom, floor_fn,
                                   allowance):
        via = list(via_arm) + [gripper]
        if not _leg_ok(geom, ctx, q_start, via, floor_fn, escape_allowance):
            continue
        if not _leg_ok(geom, ctx, via, q_home_full, floor_fn, allowance):
            continue
        legs = [(list(q_start), via, _ESCAPE_DURATION_S),
                (via, list(q_home_full), duration_s)]
        _warn_self_collision(ctx, geom, legs)
        log = getattr(ctx, 'log', None)
        if callable(log):
            log('[WARNUNG] Der direkte Weg in die Grundstellung würde durch die '
                'Tischebene führen — der Arm wird zuerst angehoben.')
        return legs

    # L3 — refuse, naming the remedy that actually works.
    raise _m.WorkflowError(_FLOOR_REFUSAL_DE)

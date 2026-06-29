#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""No-go zones + swept IK path-guard for the Roboter Studio workflow runtime.

The student draws axis-aligned keep-out boxes ("Sperrzonen") in the scene
editor; they ride a top-level ``zones`` sibling in ``/workflow/start`` and land
on ``WorkflowContext.zones``. This module wraps the closed-form ``IKSolver``
(it does NOT change it) with:

* :class:`NoGoZone` — an axis-aligned base-frame box, **inflated** by
  ``link_radius + margin`` (the Minkowski "fatten the obstacle" trick) so a
  swept test of arm *link points* (centres) is conservative.
* :func:`segment_blocked` — a **tunneling-safe** swept check: it resamples the
  segment's OWN straight joint-space line ``q_start + u·(q_end − q_start)`` at a
  few-mm step (NOT ``build_segment``'s 30 Hz waypoints — at peak those move the
  tip ~2.7 cm/frame, so a thin zone can hide between two). Valid because
  ``quintic_blend`` only reparametrises that same line (identical swept config
  set).
* :func:`plan_safe_route` — a deterministic reroute ladder
  (direct → lift-and-travel → base-swing → refuse).

Rule §2 note: this is a Roboter-Studio *workflow-level* guard — the same class
as the ``WORKSPACE_FLOOR_MARGIN_M`` table-floor refusal in
``handlers/motion.py``. It reroutes/refuses a TRANSIT around user-drawn
obstacles. It is **NOT** an inference safety envelope: it runs only on the
workflow runtime, never reshapes a recorded or replayed action, and is bypassed
entirely when there are no zones.

The reroute solvers live here but reuse ``handlers.motion._try_solve`` /
``WorkflowError`` via a **lazy import inside the call** (``motion`` imports this
module at top for ``safe_move``, so a top-level import back would be circular).
"""

from __future__ import annotations

import logging
import math
from typing import Optional

import numpy as np

from physical_ai_server.workflow.ik_solver import _J1_AXIS_X


_logger = logging.getLogger(__name__)


# ── Tunables (metres) ────────────────────────────────────────────────────────
# Half-thickness of the arm links, used to inflate every zone box (so a
# link-CENTRE containment test conservatively covers the link's physical
# girth). Tuned conservatively; keep zones a little off the table/target.
LINK_RADIUS_M = 0.03
# Extra user safety margin added to the inflation on top of the link radius.
ZONE_MARGIN_M = 0.02
# Joint-space resample step expressed as a max link-point displacement (metres)
# per sample — a few mm, well under any usable zone thickness, so the swept
# check can't tunnel through a thin wall.
STEP_M = 0.003
# Desired cruise height for lift-and-travel (the ladder caps the cruise here;
# zones taller than this can't be lifted over → base-swing).
SAFE_TRAVEL_Z = 0.18
# For a strict-vertical (top-down) cruise pose the gripper points straight
# DOWN, so the lowest link point (the tool-tip sample) sits ~this far below the
# EE. A cruise height must clear the inflated zone top by at least this much, or
# the down-pointing jaws clip the box while the EE itself is above it.
_TOOL_CLEAR_M = 0.05
# Extra clearance above an inflated zone top before we call a cruise height
# "clear".
_CLEAR_EPS_M = 0.005
# Link-body sampling resolution for link_points (passed to ik.link_points).
_LINK_SAMPLES = 5
# Hard cap on joint-space resamples per segment (pathological long swings).
_MAX_SEGMENT_SAMPLES = 256
# Conservative upper bound (metres) on a link point's radius from ANY rotation
# axis — outer strict-vertical reach ~0.30 + tool extension + a safety pad. Used
# for the arc-aware sample-count lower bound in segment_blocked: a single-joint
# rotation θ carries a link point at radius R through an arc of length R·θ, which
# is LONGER than the straight-line chord between the endpoints, so the chord
# count alone can under-sample a big rotation and let a thin wall hide between
# two samples. At the _MAX_SEGMENT_SAMPLES cap the worst-case per-sample tip
# travel is _R_MAX_M·2π/256 ≈ 11 mm — well under the ≥100 mm inflated zone wall
# (LINK_RADIUS_M + ZONE_MARGIN_M = 0.05 m inflation per face), so even a capped
# full-revolution sweep cannot tunnel.
_R_MAX_M = 0.45
# Base-swing candidate geometry (a via-point is a top-down pose at one of these
# heights/radii at a candidate azimuth around the joint-1 axis).
_SWING_HEIGHTS = (0.10, 0.14, 0.16)
_SWING_RADII = (0.16, 0.20, 0.13)
_SWING_AZIMUTHS = 16

_REFUSE_MSG = (
    'Kein sicherer Weg um die Sperrzone gefunden — bitte die Sperrzone '
    'anpassen oder das Ziel verschieben.'
)


# ── Zone geometry ────────────────────────────────────────────────────────────
class NoGoZone:
    """Axis-aligned base-frame keep-out box, inflated by ``inflation`` on every
    face. ``contains(p)`` is a point-in-inflated-AABB test."""

    __slots__ = ('min', 'max')

    def __init__(self, min_xyz, max_xyz, inflation: float = 0.0) -> None:
        self.min = np.array([float(min_xyz[i]) - inflation for i in range(3)],
                            dtype=np.float64)
        self.max = np.array([float(max_xyz[i]) + inflation for i in range(3)],
                            dtype=np.float64)

    def contains(self, p) -> bool:
        return bool(
            self.min[0] <= p[0] <= self.max[0]
            and self.min[1] <= p[1] <= self.max[1]
            and self.min[2] <= p[2] <= self.max[2]
        )


def _parse_minmax(raw) -> Optional[tuple[list[float], list[float]]]:
    """Parse one ``{min:[x,y,z], max:[x,y,z]}`` zone dict into normalised
    ``(min, max)`` float triples, or ``None`` for anything malformed.

    Defensive (Phase-4 risk note): the cloud validator already rejects
    non-finite / wrong-shape zones, but a crafted ``/workflow/start`` payload
    must NEVER crash the runtime — a malformed box is simply skipped."""
    if not isinstance(raw, dict):
        return None
    mn = raw.get('min')
    mx = raw.get('max')
    if not isinstance(mn, (list, tuple)) or not isinstance(mx, (list, tuple)):
        return None
    if len(mn) != 3 or len(mx) != 3:
        return None
    try:
        mnf = [float(v) for v in mn]
        mxf = [float(v) for v in mx]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in mnf + mxf):
        return None
    lo = [min(mnf[i], mxf[i]) for i in range(3)]
    hi = [max(mnf[i], mxf[i]) for i in range(3)]
    return lo, hi


def build_zones(raw_zones, inflation: float) -> list[NoGoZone]:
    """Turn the raw ``ctx.zones`` list into inflated :class:`NoGoZone` boxes,
    skipping any malformed entry (never raises)."""
    out: list[NoGoZone] = []
    if not raw_zones:
        return out
    for raw in raw_zones:
        parsed = _parse_minmax(raw)
        if parsed is None:
            continue
        lo, hi = parsed
        out.append(NoGoZone(lo, hi, inflation))
    return out


def point_in_any_zone(raw_zones, p, inflation: float = 0.0) -> bool:
    """True when point ``p`` (x, y, z) is inside any zone. ``inflation`` 0 tests
    the RAW boxes (used by the start-time pre-flight on concrete pins)."""
    if not raw_zones:
        return False
    for box in build_zones(raw_zones, inflation):
        if box.contains(p):
            return True
    return False


# ── Swept segment check (tunneling-safe) ─────────────────────────────────────
def segment_blocked(
    ik,
    q_start,
    q_end,
    zones,
    margin: float = ZONE_MARGIN_M,
    link_radius: float = LINK_RADIUS_M,
    step_m: float = STEP_M,
) -> bool:
    """True when the straight joint-space move ``q_start → q_end`` sweeps any arm
    link point through an inflated zone.

    Tunneling-safe: the sample count ``N`` is the MAX of a chord-based count
    (``ceil(max_link_point_displacement / step_m)``) and an arc-aware lower
    bound (``ceil(_R_MAX_M · max_joint_delta / step_m)``). The chord term alone
    UNDER-counts a dominant single-joint rotation: a straight JOINT-space move
    traces ARCS, and a link point at radius R rotated by θ travels an arc length
    R·θ — farther than the straight-line chord between its endpoints — so a thin
    wall could hide between two chord-spaced samples. ``N`` is bounded by the
    worst-case ARC displacement, not a few mm of chord, and capped at
    ``_MAX_SEGMENT_SAMPLES`` (≈11 mm/sample worst case, far under the inflated
    wall). FK each of the ``N+1`` samples along the segment's OWN straight
    joint-space line to link points and test every point against every inflated
    box; short-circuits on the first containment. ``False`` when there are no
    (valid) zones, or when the solver can't produce link points (backward-safe —
    never blocks motion it can't reason about)."""
    boxes = build_zones(zones, link_radius + margin)
    if not boxes:
        return False
    qs = np.asarray(list(q_start)[:5], dtype=np.float64)
    qe = np.asarray(list(q_end)[:5], dtype=np.float64)
    lp_s = ik.link_points(qs, samples_per_link=_LINK_SAMPLES)
    lp_e = ik.link_points(qe, samples_per_link=_LINK_SAMPLES)
    if lp_s is None or lp_e is None:
        return False
    max_disp = 0.0
    for a, b in zip(lp_s, lp_e):
        max_disp = max(max_disp, float(np.linalg.norm(np.asarray(a) - np.asarray(b))))
    # Chord-based count: straight-line distance each link point moves.
    n_chord = int(math.ceil(max_disp / max(step_m, 1e-6)))
    # Arc-aware lower bound: a dominant single-joint rotation θ carries a link
    # point at radius R through an arc R·θ, longer than the chord — so size N by
    # the worst-case arc (_R_MAX_M · max joint delta) too, not the chord alone.
    max_joint_delta = float(np.max(np.abs(qe - qs)))
    n_angular = int(math.ceil(_R_MAX_M * max_joint_delta / max(step_m, 1e-6)))
    n = min(_MAX_SEGMENT_SAMPLES, max(1, n_chord, n_angular))
    delta = qe - qs
    for i in range(n + 1):
        u = i / n
        pts = ik.link_points(qs + delta * u, samples_per_link=_LINK_SAMPLES)
        if pts is None:
            continue
        for p in pts:
            for box in boxes:
                if box.contains(p):
                    return True
    return False


# ── Reroute ladder ───────────────────────────────────────────────────────────
def _try_via(ctx, xyz, roll):
    """Solve a top-down via-point like ``motion._try_solve`` but swallow the
    workspace-floor ``WorkflowError`` (a candidate below the table is just an
    invalid candidate, not a fatal error)."""
    from physical_ai_server.workflow.handlers import motion as _m
    try:
        return _m._try_solve(ctx, xyz, roll=roll)
    except _m.WorkflowError:
        return None


def _zone_clear_top(zones, margin, link_radius) -> Optional[float]:
    """Minimum EE cruise height that clears every inflated zone, or ``None`` if
    there are no valid zones. Adds ``_TOOL_CLEAR_M`` so the down-pointing tool
    tip (the lowest link point of a top-down cruise pose) also clears the
    inflated box, not just the EE frame origin."""
    boxes = build_zones(zones, link_radius + margin)
    if not boxes:
        return None
    return max(float(b.max[2]) for b in boxes) + _TOOL_CLEAR_M + _CLEAR_EPS_M


def _reachable_cruise_z(ctx, columns, clear_z, ceil_z, roll) -> Optional[float]:
    """Highest reachable cruise height in ``[clear_z, ceil_z]`` for ALL XY
    columns, or ``None`` if even ``clear_z`` is unreachable.

    Reachability of a fixed XY column is monotone in height (the strict-vertical
    annulus shrinks as z rises — see ``ik_solver`` / the ``_solve_grasp_and_
    approach`` HIGH-5 pattern), so we bisect between ``clear_z`` (the obstacle-
    clearing floor, most reachable) and ``ceil_z`` (the desired cruise)."""
    if clear_z > ceil_z + 1e-9:
        return None

    def _ok(z):
        return all(_try_via(ctx, (cx, cy, z), roll) is not None
                   for (cx, cy) in columns)

    if not _ok(clear_z):
        return None
    if _ok(ceil_z):
        return ceil_z
    lo, hi = clear_z, ceil_z  # lo reachable, hi not
    for _ in range(18):
        mid = 0.5 * (lo + hi)
        if _ok(mid):
            lo = mid
        else:
            hi = mid
    return lo


def _plan_lift_and_travel(ctx, q_start, q_end, zones, margin, link_radius,
                          gripper, roll, final_dur):
    """Lift straight up to a zone-clearing cruise height, traverse in XY, then
    descend onto ``q_end``. ``None`` when the zones can't be cleared at a
    reachable height (→ base-swing)."""
    from physical_ai_server.workflow.handlers import motion as _m
    ik = ctx.ik
    start_fk = ik.fk(list(q_start)[:5])
    end_fk = ik.fk(list(q_end)[:5])
    if start_fk is None or end_fk is None:
        return None
    sx, sy, _sz = (float(v) for v in start_fk[1])
    tx, ty, _tz = (float(v) for v in end_fk[1])
    clear_z = _zone_clear_top(zones, margin, link_radius)
    if clear_z is None:
        return None
    cruise = _reachable_cruise_z(ctx, [(sx, sy), (tx, ty)], clear_z,
                                 SAFE_TRAVEL_Z, roll)
    if cruise is None:
        return None
    lift_q = _try_via(ctx, (sx, sy, cruise), roll)
    travel_q = _try_via(ctx, (tx, ty, cruise), roll)
    if lift_q is None or travel_q is None:
        return None
    move_dur = getattr(_m, 'DEFAULT_MOVE_DURATION_S', 2.5)
    lift_full = list(lift_q) + [gripper]
    travel_full = list(travel_q) + [gripper]
    legs = [
        (list(q_start), lift_full, move_dur),
        (lift_full, travel_full, move_dur),
        (travel_full, list(q_end), final_dur),
    ]
    for a, b, _d in legs:
        if segment_blocked(ik, a, b, zones, margin, link_radius):
            return None
    return legs


def _candidate_azimuths(n: int = _SWING_AZIMUTHS) -> list[float]:
    """Evenly-spaced base azimuths in (−π, π], offset off the cardinal points."""
    return [-math.pi + 2.0 * math.pi * (k + 0.5) / n for k in range(n)]


def _plan_base_swing(ctx, q_start, q_end, zones, margin, link_radius,
                     gripper, roll, final_dur):
    """Route around a tall zone via an intermediate base azimuth: a single
    top-down via-point at a candidate azimuth/radius/height whose two legs both
    miss the zones. ``None`` when no candidate is clear (→ refuse).

    Elbow-branch: the closed-form solver is deterministic (elbow-up first), so
    every via and leg endpoint shares the same branch automatically — we never
    introduce an elbow-up↔down flip that would sweep a large reconfiguration."""
    from physical_ai_server.workflow.handlers import motion as _m
    ik = ctx.ik
    move_dur = getattr(_m, 'DEFAULT_MOVE_DURATION_S', 2.5)
    for z_swing in _SWING_HEIGHTS:
        for r_swing in _SWING_RADII:
            for az in _candidate_azimuths():
                vx = _J1_AXIS_X + r_swing * math.cos(az)
                vy = r_swing * math.sin(az)
                via_q = _try_via(ctx, (vx, vy, z_swing), roll)
                if via_q is None:
                    continue
                via_full = list(via_q) + [gripper]
                legs = [
                    (list(q_start), via_full, move_dur),
                    (via_full, list(q_end), final_dur),
                ]
                if all(not segment_blocked(ik, a, b, zones, margin, link_radius)
                       for a, b, _d in legs):
                    return legs
    return None


def plan_safe_route(ctx, q_start, q_end, zones, duration_s, roll=None,
                    margin: float = ZONE_MARGIN_M):
    """Return a list of ``(q_start, q_end, duration)`` legs that get the arm
    from ``q_start`` to ``q_end`` without sweeping a zone.

    Ladder: direct → lift-and-travel → base-swing → refuse
    (``WorkflowError('… Sperrzone …')``). Every via-point is solved by the
    motion module's reachability-clamping solver and every leg is swept-checked
    before it is returned. ``q_end`` is preserved EXACTLY as the final leg's end
    so the gripper state + wrist roll the caller computed are not lost in the
    reroute; intermediate vias carry ``q_start``'s gripper so it actuates ONCE,
    on the final leg arriving at ``q_end`` (like the direct path)."""
    from physical_ai_server.workflow.handlers import motion as _m
    ik = ctx.ik
    link_radius = LINK_RADIUS_M
    if roll is None:
        roll = float(q_end[4])
    # Gripper continuity: outbound/intermediate vias inherit q_START's gripper so
    # the gripper changes ONCE — on the final leg arriving at q_end (which keeps
    # q_end's gripper) — exactly like the direct path. Stamping every via with
    # q_end's gripper would actuate the gripper during the FIRST transit leg if a
    # caller ever passes q_start[5] != q_end[5]. All current callers are
    # gripper-consistent, so this is latent hardening.
    gripper_via = float(q_start[5]) if len(q_start) > 5 else (
        float(q_end[5]) if len(q_end) > 5 else 0.0)

    # 1. Direct.
    if not segment_blocked(ik, q_start, q_end, zones, margin, link_radius):
        return [(list(q_start), list(q_end), duration_s)]

    # 2. Lift-and-travel (over the zones).
    legs = _plan_lift_and_travel(ctx, q_start, q_end, zones, margin,
                                 link_radius, gripper_via, roll, duration_s)
    if legs is not None:
        return legs

    # 3. Base-swing (around the zones).
    legs = _plan_base_swing(ctx, q_start, q_end, zones, margin,
                            link_radius, gripper_via, roll, duration_s)
    if legs is not None:
        return legs

    # 4. Refuse.
    raise _m.WorkflowError(_REFUSE_MSG)

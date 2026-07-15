#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Motion primitives for the Roboter Studio workflow runtime.

Each handler takes the ``WorkflowContext`` plus the block's args dict.
Args are pre-evaluated by the interpreter — value-block inputs come in
fully resolved (a destination value is already a ``{x, y, z}`` dict, a
detection is the ``Detection`` instance, etc.). Handlers raise
``WorkflowError`` with a German user-facing message on any failure.
"""

from __future__ import annotations

import logging
import math
import os
import time
from typing import Any

from physical_ai_server.workflow.path_guard import (
    ZONE_MARGIN_M,
    plan_safe_route,
    point_in_any_zone,
    segment_blocked,
)
from physical_ai_server.workflow.trajectory_builder import (
    build_segment,
    chunked_publish,
)


_logger = logging.getLogger(__name__)


def _safe_float(env_name: str, default: float) -> float:
    """Read a float from ``os.environ[env_name]`` at import time, falling
    back to ``default`` (with a logged English [WARNUNG]) on a malformed
    value instead of raising ``ValueError``.

    A non-numeric operator override (e.g. ``EDUBOTICS_GRASP_CLEARANCE_M=12mm``
    or an empty string) used to raise at module import — which cascades up
    through ``handlers/__init__.py`` (it imports this module to build the
    dispatch tables) and takes the WHOLE Roboter Studio dispatch down with an
    opaque traceback the student can't act on. Degrade to the tuned default
    and log loudly so the misconfiguration is visible without bricking the
    runtime."""
    raw = os.environ.get(env_name)
    if raw is None:
        return default
    if not raw.strip():
        # Empty/whitespace-only counts as UNSET, silently: docker-compose
        # forwards optional knobs as `${VAR:-}` (EMPTY default), so every
        # un-tuned rig delivers '' — warning about that would spam the log on
        # every boot. Only a genuinely malformed value warns below.
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        _logger.warning(
            '[WARNUNG] %s=%r is not a number — falling back to %s.',
            env_name, raw, default,
        )
        return default


HOME_JOINTS_RAD = [0.0, -math.pi / 2, math.pi / 2, 0.0, 0.0]
DEFAULT_APPROACH_HEIGHT_M = 0.06
GRIPPER_OPEN_RAD = 0.8
GRIPPER_CLOSED_RAD = -0.5

# Generic grasp (object-agnostic): the gripper descends to the measured table
# plane (z_table from the touch-off) PLUS this conservative clearance, so the
# fingertips straddle the lower part of a low-profile object instead of driving
# into the table. Tuned on the rig (env-overridable).
GRASP_CLEARANCE_M = _safe_float('EDUBOTICS_GRASP_CLEARANCE_M', 0.012)
# Release height ABOVE the destination surface for `drop_at`. Deliberately much
# higher than the grasp clearance so the held object is dropped from a safe
# height that clears a low container/bucket rim, instead of pressing the gripper
# down to ~table level (which collides with anything standing at the target).
# Tune via EDUBOTICS_DROP_HEIGHT_M (metres above the surface). NOTE: a higher
# release shrinks the reachable XY radius slightly, so pin the drop spot a bit
# closer to the base if a high drop reports "außerhalb des Arbeitsbereichs".
DROP_HEIGHT_M = _safe_float('EDUBOTICS_DROP_HEIGHT_M', 0.05)
# Tool roll (j5) jaw constant. The OMX-F gripper's jaws separate along the
# end-effector Y axis (URDF: both fingers pivot about EE-Z, offset along EE-Y),
# whose base azimuth is π/2 + joint1 − joint5 — so the geometrically-correct
# top-down jaw constant is 90°, NOT 0° (0° would pinch the perpendicular axis).
# This is the additive const in the live named-object grasp roll
# (joint5 = base_yaw − tag_yaw + GRASP_ROLL) AND the fixed roll the legacy
# pickup/drop_at/move_to use. Default 90°, env-overridable, rig-validated by the
# P0 jaw-check (which may trim a few degrees — the finger pads are not perfectly
# symmetric about EE-Y).
GRASP_ROLL_RAD = math.radians(_safe_float('EDUBOTICS_GRASP_ROLL_DEG', 90.0))
# Workspace floor: never command the end-effector below the table plane.
WORKSPACE_FLOOR_MARGIN_M = 0.01

# Grasp-success check (#2): after the gripper closes, read the achieved gripper
# angle (follower joint index 5). A held object stops the jaws partway open; an
# empty close reaches ≈ the commanded close angle. The HELD/MISS threshold is
# PER-OBJECT: commanded close + GRASP_HELD_MARGIN_RAD (see _held_threshold_rad)
# — a single global threshold silently broke miss-detection for any object whose
# tuned gripper_close_rad sat ABOVE it (an empty close never crossed it, so a
# miss read as "held"). The margin 0.15 makes the shipped cube's derived
# threshold (−0.5 + 0.15 = −0.35) byte-identical to the previously rig-validated
# global value. Setting EDUBOTICS_GRASP_HELD_MAX_RAD to a NUMBER restores the
# fixed global threshold everywhere (operator rollback / rig tuning). Compose
# forwards the env with an EMPTY default on purpose (`${…:-}`): empty/whitespace
# counts as UNSET so the per-object threshold is live on every un-tuned rig — a
# baked compose default would silently disable the per-object path fleet-wide
# (the v2.12.x dead-code scar; see _grasp_held_env_override_set).
# GRASP_RETRY re-tries the whole detect→select→grasp that many times before the
# instance is skipped; GRASP_SETTLE_S lets the servo settle before the readback.
# Rig-tunable; env-forwarding-guard: all three envs are forwarded in
# robotis_ai_setup/docker/docker-compose.yml.
GRASP_HELD_MAX_RAD = _safe_float('EDUBOTICS_GRASP_HELD_MAX_RAD', -0.35)


def _grasp_held_env_override_set() -> bool:
    """True when the operator EXPLICITLY set ``EDUBOTICS_GRASP_HELD_MAX_RAD`` to
    a non-empty value (rig override / one-variable rollback to the fixed global
    threshold). Empty/whitespace counts as UNSET — compose always FORWARDS the
    env (`${EDUBOTICS_GRASP_HELD_MAX_RAD:-}` → '' on an un-tuned rig), so bare
    presence is meaningless (`is not None` shipped the per-object threshold as
    dead code). The override VALUE itself is parsed once at import into
    ``GRASP_HELD_MAX_RAD``; only this set/unset check is call-time, so tests can
    exercise both branches with a plain env monkeypatch instead of poking a
    module-level sentinel."""
    return os.environ.get('EDUBOTICS_GRASP_HELD_MAX_RAD', '').strip() != ''
# Plain constant (NOT env-tunable — an env name would need compose forwarding;
# tune per-rig via EDUBOTICS_GRASP_HELD_MAX_RAD instead). An object must stop
# the jaws at least this far ABOVE its commanded close to count as held, so
# catalog guidance is: command gripper_close_rad at least ~0.15 rad deeper than
# the angle where the jaws meet the body.
GRASP_HELD_MARGIN_RAD = 0.15
GRASP_RETRY = max(0, int(_safe_float('EDUBOTICS_GRASP_RETRY', 1.0)))
GRASP_SETTLE_S = _safe_float('EDUBOTICS_GRASP_SETTLE_S', 0.3)

DEFAULT_HOME_DURATION_S = 3.0
DEFAULT_MOVE_DURATION_S = 2.5
DEFAULT_GRIPPER_DURATION_S = 0.5
DEFAULT_APPROACH_DURATION_S = 1.5
DEFAULT_GRASP_DURATION_S = 1.0

# ── Phase-2 Tempo (global run-bar speed multiplier + optional per-move) ───────
# Tempo is a Roboter-Studio *teaching* speed knob on the workflow runtime — a
# multiplier on every motion DURATION. Rule §2: it is NOT an inference safety
# envelope. It runs ONLY on the workflow runtime, never reshapes a
# recorded/replayed action, and is invisible to the inference path. >1 = faster
# (shorter duration; the build_segment velocity floor still protects a too-fast
# move), <1 = slower (longer duration; capped below the controller goal_time).
# Plain module constants (NOT env vars — a new EDUBOTICS_* knob would need a
# docker-compose forward per the env-forwarding-guard); tests monkeypatch them.
_TEMPO_MIN = 0.5   # „langsam" — half speed (clamp floor)
_TEMPO_MAX = 2.0   # „schnell" — double speed (clamp ceiling)
# Cap on a tempo-slowed (or velocity-floored) SEGMENT duration. A slow tempo
# LENGTHENS a move; this bounds the per-segment duration so a slowed move stays in
# a sane range (the longest default move, HOME at 3.0 s, would otherwise be 6.0 s
# at _TEMPO_MIN). NOTE: the goal_time comparison is MOOT — chunked_publish splits
# every trajectory into <= 1 s chunks, each published with its own
# time_from_start restarted per chunk, so the controller never sees a single
# > goal_time (5.0 s) segment regardless of this cap; it stays purely a
# teaching-speed sanity bound. The build_segment velocity floor independently
# maxes a segment at ~4.1 s for the worst ~2π joint swing.
_MAX_STRETCHED_DURATION_S = 4.5

# Per-move „mit Tempo" dropdown values → speed multiplier. The default option
# value 'global' (and any absent/unknown value) → None = use the workflow-global
# ctx.tempo. Strictly additive: a transit block authored before this field
# carries no `geschwindigkeit`, so every existing saved workflow keeps the global
# tempo. Values mirror the run-bar control + the cloud validator window.
_MOVE_TEMPO_PRESETS = {
    'langsam': _TEMPO_MIN,
    'normal': 1.0,
    'schnell': _TEMPO_MAX,
}


def _parse_observe_pose() -> list[float]:
    """Observation pose ("Beobachtungspose") — the 5 arm joints (rad) the
    named-object loop retreats to between passes so the arm is out of the scene
    camera's view and doesn't occlude the remaining objects during re-detection.
    ``EDUBOTICS_OBSERVE_POSE`` = comma-separated 5 joint radians; default HOME."""
    raw = os.environ.get('EDUBOTICS_OBSERVE_POSE')
    if not raw:
        return list(HOME_JOINTS_RAD)
    try:
        vals = [float(v) for v in raw.split(',')]
    except (TypeError, ValueError):
        _logger.warning(
            '[WARNUNG] EDUBOTICS_OBSERVE_POSE=%r is not 5 comma-separated numbers '
            '— falling back to HOME.', raw)
        return list(HOME_JOINTS_RAD)
    if len(vals) != 5:
        _logger.warning(
            '[WARNUNG] EDUBOTICS_OBSERVE_POSE=%r must have exactly 5 values '
            '— falling back to HOME.', raw)
        return list(HOME_JOINTS_RAD)
    return vals


OBSERVE_POSE_JOINTS = _parse_observe_pose()


class WorkflowError(Exception):
    """Raised by handlers with a German message ready for the editor's
    log strip and toast."""


class GraspSkip(WorkflowError):
    """A per-instance, RECOVERABLE grasp failure: the selected object can't be
    grasped right now — it vanished between detect and grasp, every visible
    instance is out of reach, or its orientation couldn't be read — but OTHER
    instances may be fine. ``grasp_object`` marks the offending tag(s) SKIPPED
    before raising this, so the „Solange <Typ> sichtbar" loop can swallow it and
    make progress on the rest. A standalone „greife" lets it propagate (it IS a
    WorkflowError) and fails loud with the German message. Distinct from a hard
    ``WorkflowError`` (missing calibration, stopped) which MUST abort the loop —
    the loop catches ONLY ``GraspSkip``, never the base class."""


# The dataclass default for ctx.last_full_joints (workflow_manager seeds the
# real follower pose over it at start, best-effort). All-exactly-zero is never
# a real arm pose — HOME alone is [0, -π/2, π/2, 0, 0, …] — so an unchanged
# all-zero vector means the synchronous seed never ran (no follower joints had
# arrived / the joint source was unavailable). Commanding a motion FROM this
# fake zero pose makes the first waypoint ≈ [0]*6, yanking j2/j3 toward 0
# before the arm starts tracking — a lurch. We fail loud instead.
_UNSEEDED_POSE_TOL_RAD = 1e-6


def _require_seeded_start_pose(ctx) -> None:
    """Raise a German error if ``ctx.last_full_joints`` is still the unseeded
    all-zero sentinel, so the first move never commands from a fake pose."""
    pose = getattr(ctx, 'last_full_joints', None)
    if not pose:
        raise WorkflowError(
            'Aktuelle Armstellung ist noch nicht bekannt — bitte kurz warten, '
            'bis der Roboter verbunden ist, und erneut starten.'
        )
    if all(abs(float(v)) <= _UNSEEDED_POSE_TOL_RAD for v in pose):
        raise WorkflowError(
            'Aktuelle Armstellung ist noch nicht bekannt — bitte kurz warten, '
            'bis der Roboter verbunden ist, und erneut starten.'
        )


def _resolve_tempo(ctx, tempo: float | None) -> float:
    """Resolve the effective speed multiplier for one motion.

    Uses the per-move override ``tempo`` when given, else the workflow-global
    ``ctx.tempo`` (default 1.0 — a non-Roboter-Studio ctx or a run with no Tempo
    set). Non-finite / non-positive / non-numeric → 1.0. Clamped to
    ``[_TEMPO_MIN, _TEMPO_MAX]``.

    Rule §2: a teaching speed knob, not an inference safety envelope."""
    raw = tempo if tempo is not None else getattr(ctx, 'tempo', 1.0)
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(val) or val <= 0.0:
        return 1.0
    return max(_TEMPO_MIN, min(_TEMPO_MAX, val))


def _tempo_scaled_duration(ctx, duration_s: float, tempo: float | None) -> float:
    """Scale a REQUESTED motion duration by the effective tempo.

    Faster tempo (>1) SHORTENS the duration; build_segment's velocity floor
    (``_velocity_safe_duration``) then extends it back up if the shrunk move
    would exceed the safe per-joint velocity, so a fast tempo can never drive the
    arm dangerously quickly. A slow tempo (<1) LENGTHENS it; the result is capped
    at ``_MAX_STRETCHED_DURATION_S`` so a slowed move stays strictly below the
    ros2_control goal_time tolerance (5.0 s)."""
    eff = _resolve_tempo(ctx, tempo)
    scaled = duration_s / eff
    if scaled > _MAX_STRETCHED_DURATION_S:
        scaled = _MAX_STRETCHED_DURATION_S
    return scaled


def _move_tempo(args: dict[str, Any]) -> float | None:
    """Resolve an OPTIONAL per-move „mit Tempo" override from a transit block's
    ``geschwindigkeit`` field (interpreter lowercases the field name). Absent /
    'global' / unknown → ``None`` (use the workflow-global ctx.tempo). A named
    preset → its multiplier. Never raises."""
    raw = args.get('geschwindigkeit')
    if raw is None:
        return None
    return _MOVE_TEMPO_PRESETS.get(str(raw).strip().lower())


def _publish_motion_t(ctx, q_start, q_end, duration_s, tempo) -> None:
    """Forward to ``_publish_motion``, passing the per-move ``tempo`` only when
    set. Keeping the no-override call at the original 4-arg arity preserves the
    4-arg ``_publish_motion`` monkeypatches in the motion tests (and is a no-op
    behavioural difference: ``_publish_motion`` applies the global ctx.tempo when
    its ``tempo`` is ``None``)."""
    if tempo is None:
        _publish_motion(ctx, q_start, q_end, duration_s)
    else:
        _publish_motion(ctx, q_start, q_end, duration_s, tempo)


def _publish_motion(ctx, q_start: list[float], q_end: list[float],
                    duration_s: float, tempo: float | None = None) -> None:
    # Phase-2 Tempo: scale the REQUESTED duration by the per-move override
    # (`tempo`) or, when None, the workflow-global ctx.tempo — BEFORE
    # build_segment, so the velocity floor protects a fast tempo and the
    # goal_time cap bounds a slow one. This is the SINGLE choke point every
    # motion passes through, so the global tempo reaches every transit, gripper,
    # and grasp-corridor move with no per-handler wiring. Rule §2: teaching-only.
    duration_s = _tempo_scaled_duration(ctx, duration_s, tempo)
    waypoints = build_segment(q_start, q_end, duration_s)
    # Serialize motion across the main stack and any concurrent hat
    # handler. The hat scheduler holds ctx.motion_lock for its whole
    # body; the main stack acquires it for the publish window so
    # cooperative perception value-blocks (which don't move the arm)
    # are not stalled.
    #
    # We use threading.RLock so a hat handler holding the lock can
    # re-enter via its own motion blocks without deadlocking on the
    # outer body lock. The 10s timeout is the safety upper bound on
    # waiting for the *other* thread to finish a publish chunk.
    # Audit §A2 — previously we proceeded without the lock on
    # timeout, silently re-introducing the race the lock was added to
    # prevent. Now we raise so the student sees a clear German error
    # and the runtime stays correct.
    lock = getattr(ctx, 'motion_lock', None)
    acquired = False
    if lock is not None:
        acquired = lock.acquire(timeout=10.0)
        if not acquired:
            raise WorkflowError(
                'Bewegung blockiert — ein anderer Workflow-Teil hält '
                'die Sperre zu lange. Bitte Workflow neu starten.'
            )
    try:
        ok = chunked_publish(
            publisher=ctx.publisher,
            points=waypoints,
            should_stop=ctx.should_stop,
        )
    finally:
        if acquired and lock is not None:
            try:
                lock.release()
            except RuntimeError:
                pass
    if not ok:
        raise WorkflowError('Workflow wurde gestoppt.')


def safe_move(
    ctx,
    q_start: list[float],
    q_end: list[float],
    duration_s: float,
    roll: float | None = None,
    tempo: float | None = None,
) -> None:
    """Issue a TRANSIT move with no-go-zone (Sperrzone) avoidance.

    ``tempo`` is the OPTIONAL per-move „mit Tempo" override (Phase-2). It is
    forwarded to every published leg (direct OR rerouted) so a whole reroute
    runs at the requested speed; ``None`` falls back to the workflow-global
    ctx.tempo inside ``_publish_motion``.

    Rule §2 note: this is a Roboter-Studio *workflow-level* guard — the SAME
    class as the ``WORKSPACE_FLOOR_MARGIN_M`` table-floor refusal in
    ``_solve_or_raise``/``_try_solve``. It reroutes/refuses a transit around
    user-drawn keep-out boxes. It is NOT an inference safety envelope: it runs
    only on the workflow runtime and never reshapes a recorded/replayed action.

    Backward-safe: zones are read defensively via ``getattr(ctx, 'zones',
    None)``. Empty/None (every non-Roboter-Studio path, and a Roboter-Studio run
    with no zones drawn) → behaves EXACTLY like a raw ``_publish_motion`` — no
    reroute, no extra IK, no regression. Only TRANSIT legs route through here;
    the descend/grasp corridor + gripper-only moves call raw ``_publish_motion``
    (zones are user obstacles, never the table/target — there is deliberately NO
    hard zone backstop inside ``_publish_motion``).
    """
    zones = getattr(ctx, 'zones', None)
    ik = getattr(ctx, 'ik', None)
    if not zones or ik is None or not hasattr(ik, 'link_points'):
        _publish_motion_t(ctx, q_start, q_end, duration_s, tempo)
        return
    if not segment_blocked(ik, q_start, q_end, zones, ZONE_MARGIN_M):
        _publish_motion_t(ctx, q_start, q_end, duration_s, tempo)
        return
    # Direct path crosses a zone — plan a reroute (lift-and-travel → base-swing
    # → refuse). plan_safe_route raises a German WorkflowError when no safe
    # route exists (→ red banner + stop).
    legs = plan_safe_route(ctx, q_start, q_end, zones, duration_s, roll=roll)
    log = getattr(ctx, 'log', None)
    if callable(log):
        log('[WARNUNG] Sperrzone auf dem Weg — Ausweichroute wird gefahren.')
    for leg_start, leg_end, leg_dur in legs:
        _publish_motion_t(ctx, leg_start, leg_end, leg_dur, tempo)


def _point_in_zone(ctx, x: float, y: float, z: float) -> bool:
    """True when (x, y, z) lies inside any (raw, un-inflated) no-go zone on
    ``ctx``. Used by the start-time pre-flight to warn about concrete
    destination pins placed in a Sperrzone. Reads ``getattr(ctx, 'zones',
    None)`` so a zone-less ctx is always False (backward-safe)."""
    zones = getattr(ctx, 'zones', None)
    if not zones:
        return False
    return point_in_any_zone(zones, (float(x), float(y), float(z)))


def _floor_z_at(ctx, x: float, y: float) -> float | None:
    """Return the table-surface z at base-frame (x, y) for the workspace-floor
    refusal, or ``None`` when no table height is known.

    L1 fix: when the touch-off measured a tilted ``table_plane = (a, b, c)``
    (``z = a·x + b·y + c``) the legitimate grasp z VARIES across the table, so
    comparing every target against the SCALAR ``z_table`` would falsely refuse
    a low corner ("Tischebene"). With the plane present we evaluate the plane z
    at the target's own (x, y); otherwise we fall back to the flat ``z_table``.
    """
    plane = getattr(ctx, 'table_plane', None)
    if plane is not None:
        try:
            a, b, c = (float(v) for v in plane)
            return a * float(x) + b * float(y) + c
        except (TypeError, ValueError):
            # A malformed plane must not fail-open the floor; fall through to
            # the scalar z_table so the floor stays enforced.
            pass
    z_table = getattr(ctx, 'z_table', None)
    return None if z_table is None else float(z_table)


def _solve_or_raise(
    ctx,
    target_xyz: tuple[float, float, float],
    free_yaw: bool = True,
    roll: float | None = None,
) -> list[float]:
    if ctx.ik is None:
        raise WorkflowError(
            'Roboter-Beschreibung nicht verfügbar — der Bewegungsrechner (IK) '
            'konnte nicht gestartet werden. Bitte die Umgebung neu starten.'
        )
    # Workspace floor: never drive the end-effector below the table plane. When
    # a tilted table_plane is calibrated the floor follows the plane at the
    # target (x, y), not the scalar z_table (L1).
    floor_z = _floor_z_at(ctx, target_xyz[0], target_xyz[1])
    if floor_z is not None and float(target_xyz[2]) < floor_z - WORKSPACE_FLOOR_MARGIN_M:
        raise WorkflowError('Zielpunkt liegt unter der Tischebene.')
    seed = ctx.last_arm_joints or HOME_JOINTS_RAD
    solution = ctx.ik.solve(target_xyz=target_xyz, seed=seed, free_yaw=free_yaw, roll=roll)
    if solution is None:
        raise WorkflowError(
            'Position außerhalb des Arbeitsbereichs — bitte das Objekt in den '
            'markierten Greifbereich legen (nicht zu nah am Roboter, nicht zu weit).'
        )
    return list(solution)


# How close (metres) the bisected approach height is allowed to converge to the
# grasp height before we accept it. A 2 mm step is below the grasp clearance, so
# the worst-case "shrunk" approach is still a visibly distinct lift above the
# grasp — never an approach that coincides with the grasp.
_APPROACH_BISECT_TOL_M = 0.002


def _solve_grasp_and_approach(
    ctx,
    grasp_xyz: tuple[float, float, float],
    approach_height_m: float,
    roll: float | None = None,
) -> tuple[list[float], list[float]]:
    """Solve the GRASP first, then the APPROACH derived from the reachable
    envelope. Returns ``(grasp_arm_q, approach_arm_q)``.

    HIGH-5 fix: the strict-vertical reach annulus SHRINKS with height, so at the
    outer ring a target whose GRASP is reachable can have its
    ``+approach_height_m`` approach pose fall outside the annulus. The old code
    solved the approach FIRST and refused the whole pickup/drop ("Arbeitsbereich")
    even though the object was graspable. Instead we:

      1. Solve the grasp. If THAT is unreachable, refuse (the object really is
         out of the workspace) — ``_solve_or_raise`` raises the German message.
      2. Try the full requested approach height. If reachable, use it.
      3. Otherwise bisect the lift between the grasp height and the requested
         approach height down to the MAX reachable lift (never below the grasp),
         so the arm still approaches from above — just by a smaller, reachable
         amount — rather than refusing a graspable object.

    Only an unreachable GRASP is refused.
    """
    gx, gy, gz = (float(v) for v in grasp_xyz)
    grasp_arm_q = _solve_or_raise(ctx, (gx, gy, gz), roll=roll)

    # Fast path: the full requested approach height is reachable.
    full_approach = (gx, gy, gz + approach_height_m)
    approach_arm_q = _try_solve(ctx, full_approach, roll=roll)
    if approach_arm_q is not None:
        return grasp_arm_q, approach_arm_q

    # The annulus shrank at this height — bisect the lift down toward the grasp
    # to the largest reachable approach. ``lo`` is always reachable (it's the
    # grasp height, proven above); ``hi`` is the unreachable requested height.
    lo, hi = 0.0, approach_height_m
    best_q = grasp_arm_q          # worst case: approach == grasp (still valid)
    best_lift = 0.0
    while hi - lo > _APPROACH_BISECT_TOL_M:
        mid = 0.5 * (lo + hi)
        q = _try_solve(ctx, (gx, gy, gz + mid), roll=roll)
        if q is not None:
            lo, best_q, best_lift = mid, q, mid
        else:
            hi = mid
    ctx.log(
        f'[WARNUNG] Anfahrhöhe auf {best_lift * 1000:.0f} mm reduziert '
        f'(angefordert: {approach_height_m * 1000:.0f} mm) — Ziel liegt am '
        'Rand des Greifbereichs.'
    )
    return grasp_arm_q, best_q


def _try_solve(
    ctx,
    target_xyz: tuple[float, float, float],
    roll: float | None = None,
) -> list[float] | None:
    """Solve ``target_xyz`` like ``_solve_or_raise`` but return ``None`` (rather
    than raising) when the point is unreachable — used by the approach-height
    bisection where an unreachable lift is an expected, recoverable outcome.

    The workspace-floor refusal still RAISES: an approach point can never be
    below the table (it is always above the grasp), so hitting the floor here
    is a genuine error, not an annulus-edge case to clamp.
    """
    floor_z = _floor_z_at(ctx, target_xyz[0], target_xyz[1])
    if floor_z is not None and float(target_xyz[2]) < floor_z - WORKSPACE_FLOOR_MARGIN_M:
        raise WorkflowError('Zielpunkt liegt unter der Tischebene.')
    if ctx.ik is None:
        raise WorkflowError(
            'Roboter-Beschreibung nicht verfügbar — der Bewegungsrechner (IK) '
            'konnte nicht gestartet werden. Bitte die Umgebung neu starten.'
        )
    seed = ctx.last_arm_joints or HOME_JOINTS_RAD
    solution = ctx.ik.solve(target_xyz=target_xyz, seed=seed, roll=roll)
    return None if solution is None else list(solution)


def _resolve_target(value: Any, ctx) -> tuple[float, float, float]:
    """Turn an evaluated input value into a base-frame (x, y, z) point.

    Accepts: a destination name (str → looked up in ``ctx.destinations``),
    a destination dict, a Detection instance, or an ``(x, y, z)`` tuple.
    """
    if value is None:
        raise WorkflowError('Block hat kein Ziel erhalten.')
    if isinstance(value, str):
        if value not in ctx.destinations:
            raise WorkflowError(f'Unbekanntes Ziel: {value}')
        d = ctx.destinations[value]
        return float(d['x']), float(d['y']), float(d['z'])
    if isinstance(value, dict):
        if 'world_xyz_m' in value and value['world_xyz_m'] is not None:
            x, y, z = value['world_xyz_m']
            return float(x), float(y), float(z)
        if all(k in value for k in ('x', 'y', 'z')):
            return float(value['x']), float(value['y']), float(value['z'])
    if hasattr(value, 'world_xyz_m') and value.world_xyz_m is not None:
        x, y, z = value.world_xyz_m
        return float(x), float(y), float(z)
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return float(value[0]), float(value[1]), float(value[2])
    # A Detection whose world_xyz_m is still None reached a motion block.
    # perception_blocks._attach_named_world leaves it None (silently — so the
    # see/count blocks keep working) when the calibration is incomplete:
    # ctx.z_table / scene_intrinsics / scene_extrinsics is missing. Name the
    # exact missing step instead of the generic "could not evaluate" message,
    # so the student knows to finish the touch-off rather than re-running the
    # detect block. (Both the dict shape with a None world_xyz_m and the
    # Detection object shape land here.)
    has_world_key = (
        (isinstance(value, dict) and 'world_xyz_m' in value)
        or hasattr(value, 'world_xyz_m')
    )
    if has_world_key:
        # Distinguish the two reasons world_xyz_m is unset so the student fixes
        # the RIGHT step. The camera→table projection needs intrinsics +
        # extrinsics + the board surface height; the grasp DESCEND additionally
        # needs the measured touch-off z_table. With per-rig intrinsics now
        # mandatory (#1), "camera not calibrated" is the common early case and
        # must not be mislabelled as a missing table measurement.
        cam_uncalibrated = (
            getattr(ctx, 'scene_intrinsics', None) is None
            or getattr(ctx, 'scene_extrinsics', None) is None
            or getattr(ctx, 'board_table_z', None) is None
        )
        if cam_uncalibrated:
            raise WorkflowError(
                'Die Szenen-Kamera ist noch nicht kalibriert — bitte zuerst die '
                'Kamera-Kalibrierung (intrinsisch + Ausrichtung) abschließen.'
            )
        raise WorkflowError(
            'Für diesen Block muss die Tischhöhe kalibriert sein — bitte '
            'zuerst „Tisch vermessen" abschließen.'
        )
    raise WorkflowError('Ziel-Wert konnte nicht ausgewertet werden.')


def home(ctx, args: dict[str, Any]) -> None:
    _require_seeded_start_pose(ctx)
    q_start = ctx.last_full_joints
    # CARRY the current gripper state (index 5) instead of forcing it open.
    # Behavior change (deliberate): `home` used to hardcode GRIPPER_OPEN_RAD, so
    # a `pickup` (closed) followed by `home` opened the gripper mid-flight and
    # DROPPED the held object — the flagship tutorial does exactly pickup→home.
    # Every other motion handler already carries last_full_joints[5]; home now
    # matches, so a held object stays held across a home. Use `open_gripper`
    # explicitly to release.
    q_end = list(HOME_JOINTS_RAD) + [q_start[5]]
    # TRANSIT (whole-arm move to the Grundstellung) — route around any no-go
    # zone, exactly like move_to/move_above/lift. With no zones drawn safe_move
    # is a pass-through to raw _publish_motion, so behavior is unchanged. A zone
    # that traps the home transit → clean German Sperrzone refusal (correct: the
    # student drew a zone the arm can't get out of). `home` is a pure Blockly
    # block (handlers/__init__ `edubotics_home`), never an un-catchable
    # recovery/teardown, so raising here is safe.
    safe_move(ctx, q_start, q_end, DEFAULT_HOME_DURATION_S)
    ctx.last_arm_joints = list(HOME_JOINTS_RAD)
    ctx.last_full_joints = q_end


def go_to_observation_pose(ctx) -> None:
    """Move the arm to the observation pose (out of the scene-cam view) so the
    named-object loop's re-detection between passes isn't occluded by the arm.
    Carries the current gripper state (index 5) so a held object is NOT dropped.
    Used by the interpreter's ``edubotics_while_visible`` branch — not a Blockly
    block."""
    _require_seeded_start_pose(ctx)
    q_start = ctx.last_full_joints
    arm = list(OBSERVE_POSE_JOINTS)
    q_end = arm + [q_start[5]]
    # TRANSIT (whole-arm retreat out of the scene-cam view) — route around any
    # no-go zone, like the other whole-arm transits. No zones → pass-through, so
    # the named-object loop is unchanged. Both internal call sites (interpreter
    # `edubotics_while_visible` loop body at interpreter.py and the grasp_object
    # retry branch in perception_blocks.py) run on paths where a WorkflowError
    # propagates as a normal German error — NEITHER is a `finally`/un-catchable
    # teardown — so a Sperrzone refusal here is safe (it ends the run/loop
    # cleanly, the intended "trapped arm" behavior).
    safe_move(ctx, q_start, q_end, DEFAULT_MOVE_DURATION_S)
    ctx.last_arm_joints = arm
    ctx.last_full_joints = q_end


def open_gripper(ctx, args: dict[str, Any]) -> None:
    _require_seeded_start_pose(ctx)
    q_start = ctx.last_full_joints
    q_end = q_start[:5] + [GRIPPER_OPEN_RAD]
    _publish_motion(ctx, q_start, q_end, DEFAULT_GRIPPER_DURATION_S)
    ctx.last_full_joints = q_end


def close_gripper(ctx, args: dict[str, Any]) -> None:
    _require_seeded_start_pose(ctx)
    q_start = ctx.last_full_joints
    q_end = q_start[:5] + [GRIPPER_CLOSED_RAD]
    _publish_motion(ctx, q_start, q_end, DEFAULT_GRIPPER_DURATION_S)
    ctx.last_full_joints = q_end
    # Record the COMMANDED close for the per-object grasp-held threshold (only
    # the close paths write this — see _held_threshold_rad).
    ctx.last_commanded_close_rad = GRIPPER_CLOSED_RAD


def move_to(ctx, args: dict[str, Any]) -> None:
    _require_seeded_start_pose(ctx)
    target = _resolve_target(args.get('destination'), ctx)
    arm_q = _solve_or_raise(ctx, target, roll=GRASP_ROLL_RAD)
    q_end = arm_q + [ctx.last_full_joints[5]]
    # TRANSIT — route around any no-go zone (raw _publish_motion when none).
    # Optional per-move „mit Tempo" override; None → workflow-global tempo.
    safe_move(ctx, ctx.last_full_joints, q_end, DEFAULT_MOVE_DURATION_S,
              roll=GRASP_ROLL_RAD, tempo=_move_tempo(args))
    ctx.last_arm_joints = arm_q
    ctx.last_full_joints = q_end


def compute_grasp_roll(ctx, x: float, y: float, tag_yaw: float) -> float:
    """Live tool roll (joint5) for a top-down named-object grasp::

        joint5 = base_yaw(x, y) − tag_yaw + GRASP_ROLL_RAD

    wrapped to (−π, π]. ``tag_yaw`` is the tag's base-frame yaw
    (``tag_pose.tag_yaw_base``); ``GRASP_ROLL_RAD`` is the rig-pinned jaw
    constant. Keeps the const + ``base_yaw`` in this one place (the named-object
    detection handler only carries the raw ``tag_yaw`` on the Detection). Raises
    a German error if IK is unavailable."""
    from physical_ai_server.workflow.tag_pose import grasp_joint5
    if ctx.ik is None:
        raise WorkflowError(
            'Roboter-Beschreibung nicht verfügbar — der Bewegungsrechner (IK) '
            'konnte nicht gestartet werden. Bitte die Umgebung neu starten.'
        )
    base = ctx.ik.base_yaw(float(x), float(y))
    return grasp_joint5(base, float(tag_yaw), GRASP_ROLL_RAD)


def _execute_pickup(
    ctx,
    grasp_xyz: tuple[float, float, float],
    approach_height_m: float,
    roll: float,
    close_rad: float,
    tempo: float | None = None,
) -> None:
    """Open → hover above → descend straight down → close → lift, at the FINAL
    ``grasp_xyz`` (NO extra clearance added here — the caller supplies the exact
    descend point; this is what avoids the §23.7 ``+GRASP_CLEARANCE_M``
    double-add on the named-object path). ``roll`` is joint5; ``close_rad`` is
    the gripper close angle. Shared by the ``pickup`` block (fixed roll +
    table-clearance + full close) and the named-object ``grasp_object`` handler
    (live roll + per-object close).

    HIGH-5: solve the GRASP first; derive the approach from the reachable
    envelope (clamping the lift at the annulus edge) instead of refusing a
    graspable object because its +approach pose fell outside the annulus.
    """
    grasp_arm_q, above_arm_q = _solve_grasp_and_approach(
        ctx, grasp_xyz, approach_height_m, roll=roll)
    lift_arm_q = above_arm_q

    open_q = ctx.last_full_joints[:5] + [GRIPPER_OPEN_RAD]
    above_q = above_arm_q + [GRIPPER_OPEN_RAD]
    grasp_q = grasp_arm_q + [GRIPPER_OPEN_RAD]
    closed_q = grasp_arm_q + [close_rad]
    lift_q = lift_arm_q + [close_rad]

    # Audit round-3 §22+§23: hold motion_lock for the whole pickup
    # sequence so a hat handler cannot interleave between the descend,
    # grasp, and lift sub-motions. Without this, a hat thread that
    # acquires the lock between two _publish_motion calls can move the
    # arm somewhere else mid-grasp. RLock allows _publish_motion's
    # inner acquire to re-enter without deadlock. Also update
    # last_full_joints after EACH successful sub-motion so a mid-
    # sequence failure leaves an accurate record for recovery.
    lock = getattr(ctx, 'motion_lock', None)
    if lock is not None:
        lock.acquire()
    try:
        # Phase-2 Tempo: the per-move override (or global ctx.tempo when None)
        # scales every sub-motion uniformly, so the whole pickup runs at the
        # requested speed.
        # Gripper-only open — arm joints unchanged → EXEMPT (raw).
        _publish_motion_t(ctx, ctx.last_full_joints, open_q, DEFAULT_GRIPPER_DURATION_S, tempo)
        ctx.last_full_joints = open_q
        # APPROACH (free-space hover) — TRANSIT, zone-avoided.
        safe_move(ctx, open_q, above_q, DEFAULT_MOVE_DURATION_S, roll=roll, tempo=tempo)
        ctx.last_full_joints = above_q
        ctx.last_arm_joints = above_arm_q
        # DESCEND onto the target — EXEMPT (raw): zones are user obstacles,
        # never the table/target; the grasp corridor is never zone-checked.
        _publish_motion_t(ctx, above_q, grasp_q, DEFAULT_GRASP_DURATION_S, tempo)
        ctx.last_full_joints = grasp_q
        ctx.last_arm_joints = grasp_arm_q
        # Gripper-only close — EXEMPT (raw).
        _publish_motion_t(ctx, grasp_q, closed_q, DEFAULT_GRIPPER_DURATION_S, tempo)
        ctx.last_full_joints = closed_q
        # Record the COMMANDED close (the recipe's gripper_close_rad on the
        # named-object path) for the per-object grasp-held threshold.
        ctx.last_commanded_close_rad = close_rad
        # LIFT-OUT (carry up to the hover) — TRANSIT, zone-avoided.
        safe_move(ctx, closed_q, lift_q, DEFAULT_APPROACH_DURATION_S, roll=roll, tempo=tempo)
        ctx.last_arm_joints = lift_arm_q
        ctx.last_full_joints = lift_q
    finally:
        if lock is not None:
            try:
                lock.release()
            except RuntimeError:
                pass


def _held_threshold_rad(ctx) -> float:
    """HELD/MISS gripper-angle threshold for :func:`check_grasp_held`.

    Per-object when possible: the last COMMANDED gripper close
    (``ctx.last_commanded_close_rad`` — written ONLY by the close paths
    :func:`_execute_pickup` / :func:`close_gripper` / :func:`close_on_object`,
    where after a named-object close it IS the recipe's ``gripper_close_rad``)
    plus ``GRASP_HELD_MARGIN_RAD`` — an empty close reaches ≈ the commanded
    angle, a held object stops the jaws at least the margin above it. This is
    what lets a wide-object recipe with a gentle close (e.g. −0.25) still detect
    a miss; the old fixed −0.35 read every empty gentle close as "held".

    Deliberately NOT derived from ``ctx.last_full_joints[5]``: workflow start
    boot-seeds that from the MEASURED follower pose, so a still-held gripper
    (~−0.1 measured) masqueraded as a commanded close and skewed the threshold
    before any close was commanded this run.

    Falls back to the global ``GRASP_HELD_MAX_RAD`` when (a) the operator set
    ``EDUBOTICS_GRASP_HELD_MAX_RAD`` to a non-empty value (rig override /
    rollback wins everywhere), or (b) no gripper close has been commanded this
    run (``last_commanded_close_rad`` is None / malformed) — which also keeps
    the documented open-gripper behaviour of ``grasp_held``/``wait_until_held``
    on a fresh run."""
    if _grasp_held_env_override_set():
        return GRASP_HELD_MAX_RAD
    commanded = getattr(ctx, 'last_commanded_close_rad', None)
    if commanded is None:
        return GRASP_HELD_MAX_RAD
    try:
        commanded = float(commanded)
    except (TypeError, ValueError):
        return GRASP_HELD_MAX_RAD
    if not math.isfinite(commanded) or commanded >= 0.0:
        return GRASP_HELD_MAX_RAD
    return commanded + GRASP_HELD_MARGIN_RAD


def check_grasp_held(ctx) -> bool | None:
    """Decide HELD vs EMPTY after a gripper close, from the achieved gripper
    angle (follower joint index 5).

    Returns ``True`` (object held), ``False`` (empty close — the grasp missed),
    or ``None`` when the follower-joint readback is unavailable (the caller then
    falls back to claim-on-completion — no regression). A held object leaves the
    jaws partway open (ABOVE the per-object :func:`_held_threshold_rad`); an
    empty close reaches ≈ the commanded close angle. Settles ``GRASP_SETTLE_S``
    so the servo has reached its blocked/closed angle before the readback."""
    getter = getattr(ctx, 'get_follower_joints', None)
    if not callable(getter):
        return None
    if GRASP_SETTLE_S > 0:
        time.sleep(GRASP_SETTLE_S)
    try:
        joints = getter()
    except Exception:  # noqa: BLE001 — readback is best-effort
        return None
    if not joints or len(joints) < 6:
        return None
    try:
        gripper = float(joints[5])
    except (TypeError, ValueError):
        return None
    return gripper > _held_threshold_rad(ctx)


def pickup(ctx, args: dict[str, Any]) -> None:
    _require_seeded_start_pose(ctx)
    target = _resolve_target(args.get('target'), ctx)
    # Conservative descend: grasp at the measured table plane + clearance so the
    # fingertips straddle the lower part of a low object, not the table itself.
    grasp_xyz = (target[0], target[1], target[2] + GRASP_CLEARANCE_M)
    _execute_pickup(ctx, grasp_xyz, DEFAULT_APPROACH_HEIGHT_M,
                    GRASP_ROLL_RAD, GRIPPER_CLOSED_RAD, tempo=_move_tempo(args))


def drop_at(ctx, args: dict[str, Any]) -> None:
    """Place the held object at ``destination``. Symmetric with ``pickup``:
    approach +DEFAULT_APPROACH_HEIGHT_M above the target with the gripper
    closed, descend to the target, open, then retreat back above. The v1
    ship moved straight to the target XYZ in joint space, which produced
    a swept-arc carry path — adjacent obstacles could be clipped on the
    way in. The bounded-quintic approach is consistent with pickup."""
    _require_seeded_start_pose(ctx)
    target = _resolve_target(args.get('destination'), ctx)
    # Optional per-move „mit Tempo" override; None → workflow-global tempo. Scales
    # every sub-motion of the place uniformly.
    tempo = _move_tempo(args)
    # Release from a safe height above the surface (clears a low container rim),
    # not just the grasp clearance — see DROP_HEIGHT_M.
    drop_xyz = (target[0], target[1], target[2] + DROP_HEIGHT_M)

    # HIGH-5 (symmetric with pickup): solve the DROP first; derive the approach
    # from the reachable envelope so an outer-ring destination whose drop is
    # reachable isn't refused because its +approach pose fell outside the annulus.
    drop_arm_q, above_arm_q = _solve_grasp_and_approach(
        ctx, drop_xyz, DEFAULT_APPROACH_HEIGHT_M, roll=GRASP_ROLL_RAD)

    above_closed_q = above_arm_q + [GRIPPER_CLOSED_RAD]
    drop_closed_q = drop_arm_q + [GRIPPER_CLOSED_RAD]
    drop_open_q = drop_arm_q + [GRIPPER_OPEN_RAD]
    retreat_open_q = above_arm_q + [GRIPPER_OPEN_RAD]

    # Audit round-3 §22+§23 — same atomicity argument as pickup.
    lock = getattr(ctx, 'motion_lock', None)
    if lock is not None:
        lock.acquire()
    try:
        # APPROACH (carry to the hover above the target) — TRANSIT, zone-avoided.
        safe_move(ctx, ctx.last_full_joints, above_closed_q, DEFAULT_MOVE_DURATION_S,
                  roll=GRASP_ROLL_RAD, tempo=tempo)
        ctx.last_full_joints = above_closed_q
        ctx.last_arm_joints = above_arm_q
        # DESCEND to the place point — EXEMPT (raw).
        _publish_motion_t(ctx, above_closed_q, drop_closed_q, DEFAULT_APPROACH_DURATION_S, tempo)
        ctx.last_full_joints = drop_closed_q
        ctx.last_arm_joints = drop_arm_q
        # Gripper-only open (release) — EXEMPT (raw).
        _publish_motion_t(ctx, drop_closed_q, drop_open_q, DEFAULT_GRIPPER_DURATION_S, tempo)
        ctx.last_full_joints = drop_open_q
        # RETREAT back up to the hover — TRANSIT, zone-avoided.
        safe_move(ctx, drop_open_q, retreat_open_q, DEFAULT_APPROACH_DURATION_S,
                  roll=GRASP_ROLL_RAD, tempo=tempo)
        ctx.last_arm_joints = above_arm_q
        ctx.last_full_joints = retreat_open_q
    finally:
        if lock is not None:
            try:
                lock.release()
            except RuntimeError:
                pass


# ── Grasp-split primitives (Phase 1) ─────────────────────────────────────────
# The one-block ``grasp_object`` ("Greife <Typ>") is decomposed into composable
# blocks the student sequences: ``fahre über`` (move_above) → ``senke auf``
# (descend_to) → ``schließe um`` (close_on_object) → ``hebe an`` (lift), each
# acting on a Greifziel (a ``Detection`` produced by perception_blocks.find_object,
# normally latched into a Blockly variable first). They reuse the SAME IK +
# trajectory path as ``_execute_pickup``; each is a single ``_publish_motion``.
# UNLIKE ``pickup``/``move_to`` (fixed ``GRASP_ROLL_RAD``, tag yaw ignored), the
# split blocks use the ORIENTED roll derived from the Greifziel's
# ``extras['tag_yaw']`` so an elongated object is pinched on the correct axis.
# Note (deliberate): each block re-solves its own target, so the HIGH-5
# approach/grasp coupling and cross-step ``motion_lock`` atomicity of the canned
# ``_execute_pickup`` are not shared — acceptable for the teaching path; the
# canned ``Greife`` remains the atomic option (and the loop body in Beginner mode).


def _greifziel_xyz_roll(ctx, ziel) -> tuple[float, float, float, float]:
    """Resolve a Greifziel (a ``Detection`` value) into the grasp ``(x, y, z)``
    plus the oriented wrist roll.

    Raises a ``GraspSkip`` when no Greifziel was supplied (the student forgot
    ``finde`` / didn't guard with ``falls Ziel``) OR when the tag orientation
    couldn't be read (``extras['tag_yaw']`` is ``None``) — both per-instance,
    RECOVERABLE failures, so a "Solange sichtbar" loop swallows them and moves on
    instead of ABORTING the whole run, while a standalone use still fails loud
    (``GraspSkip`` IS a ``WorkflowError``). Raises the PRECISE calibration error
    via ``_resolve_target`` when the detection has no ``world_xyz_m`` yet. Never
    commits a blind fixed-roll grasp that would pinch an elongated object on its
    long axis."""
    if ziel is None:
        raise GraspSkip(
            'Kein Greifziel — bitte „finde …" benutzen, das Ergebnis in einer '
            'Variable speichern und mit „falls" prüfen.'
        )
    x, y, z = _resolve_target(ziel, ctx)   # Detection.world_xyz_m + precise calib error
    tag_yaw = None
    extras = getattr(ziel, 'extras', None)
    if isinstance(extras, dict):
        tag_yaw = extras.get('tag_yaw')
    if tag_yaw is None:
        raise GraspSkip(
            'Die Ausrichtung des Objekts konnte nicht bestimmt werden — bitte den '
            'Tag flach und gut sichtbar aufkleben.'
        )
    roll = compute_grasp_roll(ctx, x, y, float(tag_yaw))
    return float(x), float(y), float(z), roll


def _greifziel_approach(ziel) -> float:
    """Hover/approach height for a Greifziel: the recipe value baked onto the
    detection by ``find_object`` (``extras['approach_clear_m']``), else the
    module default. Never raises."""
    extras = getattr(ziel, 'extras', None)
    if isinstance(extras, dict):
        raw = extras.get('approach_clear_m')
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass
    return DEFAULT_APPROACH_HEIGHT_M


def move_above(ctx, args: dict[str, Any]) -> None:
    """„fahre über <Ziel>" — hover straight above the Greifziel at its approach
    height, with the oriented wrist roll. Gripper state carried unchanged."""
    _require_seeded_start_pose(ctx)
    ziel = args.get('ziel')
    x, y, z, roll = _greifziel_xyz_roll(ctx, ziel)
    approach = _greifziel_approach(ziel)
    # Clamp the hover to the reachable envelope (reuse the canned path's HIGH-5
    # bisection) instead of REFUSING an outer-ring object whose grasp `finde`
    # already accepted but whose +approach pose is just outside the shrinking
    # annulus — solves the grasp first (raises only if THAT is unreachable) and
    # derives the largest reachable hover.
    _grasp_q, arm_q = _solve_grasp_and_approach(ctx, (x, y, z), approach, roll=roll)
    q_end = arm_q + [ctx.last_full_joints[5]]
    # TRANSIT (hover above the object) — route around any no-go zone. Optional
    # per-move „mit Tempo" override; None → workflow-global tempo.
    safe_move(ctx, ctx.last_full_joints, q_end, DEFAULT_MOVE_DURATION_S, roll=roll,
              tempo=_move_tempo(args))
    ctx.last_arm_joints = arm_q
    ctx.last_full_joints = q_end


def descend_to(ctx, args: dict[str, Any]) -> None:
    """„senke auf <Ziel>" — descend straight down to the Greifziel's grasp height
    with the oriented wrist roll. Gripper state carried unchanged (open,
    normally)."""
    _require_seeded_start_pose(ctx)
    x, y, z, roll = _greifziel_xyz_roll(ctx, args.get('ziel'))
    arm_q = _solve_or_raise(ctx, (x, y, z), roll=roll)
    q_end = arm_q + [ctx.last_full_joints[5]]
    _publish_motion(ctx, ctx.last_full_joints, q_end, DEFAULT_GRASP_DURATION_S)
    ctx.last_arm_joints = arm_q
    ctx.last_full_joints = q_end


def close_on_object(ctx, args: dict[str, Any]) -> None:
    """„schließe um <Ziel>" — close the gripper to the Greifziel's tuned close
    angle (``extras['gripper_close_rad']``) at the current arm pose, falling back
    to the full close ``GRIPPER_CLOSED_RAD`` when the angle is missing/malformed."""
    ziel = args.get('ziel')
    # Consistent with the other split blocks: a missing Greifziel is a recoverable
    # skip (a loop moves on; standalone fails loud) — NOT a silent full-close,
    # which could over-close (overload) on a wide object.
    if ziel is None:
        raise GraspSkip(
            'Kein Greifziel — bitte „finde …" benutzen, das Ergebnis in einer '
            'Variable speichern und mit „falls" prüfen.'
        )
    _require_seeded_start_pose(ctx)
    close_rad = GRIPPER_CLOSED_RAD
    extras = getattr(ziel, 'extras', None)
    if isinstance(extras, dict):
        raw = extras.get('gripper_close_rad')
        if raw is not None:
            try:
                parsed = float(raw)
            except (TypeError, ValueError):
                parsed = GRIPPER_CLOSED_RAD
            # NaN/inf passes float() but would command a garbage gripper angle.
            close_rad = parsed if math.isfinite(parsed) else GRIPPER_CLOSED_RAD
            # Clamp a corrupt catalog value into the physical gripper band. A value
            # far outside [GRIPPER_CLOSED_RAD, GRIPPER_OPEN_RAD] otherwise makes
            # build_segment's velocity floor stretch this gripper close to tens of
            # seconds (e.g. close_rad=100 → ~65 s) — a wedged run — and commands a
            # servo angle the hardware can't reach.
            close_rad = max(GRIPPER_CLOSED_RAD, min(GRIPPER_OPEN_RAD, close_rad))
    q_start = ctx.last_full_joints
    q_end = q_start[:5] + [close_rad]
    _publish_motion(ctx, q_start, q_end, DEFAULT_GRIPPER_DURATION_S)
    ctx.last_full_joints = q_end
    # Record the COMMANDED close (the Greifziel's tuned angle) for the
    # per-object grasp-held threshold.
    ctx.last_commanded_close_rad = close_rad


def lift(ctx, args: dict[str, Any]) -> None:
    """„hebe an" — raise the end-effector straight up by the default approach
    height from its CURRENT position, preserving the wrist roll (j5) so a held
    object isn't twisted, and the gripper state so a held object stays held."""
    _require_seeded_start_pose(ctx)
    cur = ctx.last_full_joints
    if ctx.ik is None:
        raise WorkflowError(
            'Roboter-Beschreibung nicht verfügbar — der Bewegungsrechner (IK) '
            'konnte nicht gestartet werden. Bitte die Umgebung neu starten.'
        )
    pose = ctx.ik.fk(cur[:5])
    if pose is None:
        raise WorkflowError('Aktuelle Position ist unbekannt.')
    _R, t = pose
    x, y, z = float(t[0]), float(t[1]), float(t[2])
    # Clamp the lift to the reachable envelope (reuse the canned path's HIGH-5
    # bisection) so an outer-ring grasp is never STRANDED at table level by an
    # unreachable full +approach lift: the current (reachable) pose is the lower
    # bound, the lift bisects up to the max reachable height.
    _grasp_q, above_q = _solve_grasp_and_approach(
        ctx, (x, y, z), DEFAULT_APPROACH_HEIGHT_M)
    # j5 is (very nearly) pure tool roll about the top-down tool axis — the tip
    # sits within ~1.6 mm of that axis (the EE y-offset the IK itself ignores) —
    # so overriding j5 after the solve keeps the position to sub-grasp-tolerance
    # while preserving the held object's orientation.
    arm_q = list(above_q)
    arm_q[4] = cur[4]
    q_end = arm_q + [cur[5]]
    # TRANSIT (straight-up lift) — route around any no-go zone. Optional per-move
    # „mit Tempo" override; None → workflow-global tempo.
    safe_move(ctx, cur, q_end, DEFAULT_APPROACH_DURATION_S, roll=cur[4],
              tempo=_move_tempo(args))
    ctx.last_arm_joints = arm_q
    ctx.last_full_joints = q_end


WAIT_SECONDS_MAX = 300.0  # 5 minutes — anything longer is almost certainly a mistake


def wait_seconds(ctx, args: dict[str, Any]) -> None:
    try:
        duration = float(args.get('seconds', 1.0))
    except (TypeError, ValueError):
        duration = 1.0
    if duration < 0:
        duration = 0.0
    if duration > WAIT_SECONDS_MAX:
        # Hard cap so a student typing 99999 doesn't wedge the
        # workflow for 27 hours. Audit §G4.
        ctx.log(
            f'[WARNUNG] Warte-Dauer auf {WAIT_SECONDS_MAX:.0f} s begrenzt '
            f'(angefordert: {duration:.0f} s).'
        )
        duration = WAIT_SECONDS_MAX
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        if ctx.should_stop():
            raise WorkflowError('Workflow wurde gestoppt.')
        # max(0.0, ...) guards against negative sleep arg in the
        # final iteration where deadline can be < monotonic() by a few
        # microseconds (audit §F10).
        time.sleep(max(0.0, min(0.05, deadline - time.monotonic())))

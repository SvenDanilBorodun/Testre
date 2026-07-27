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


# True iff EDUBOTICS_OBSERVE_POSE was set to a well-formed numeric list (of ANY
# length). Pre-declared so _observe_joints can read it even when the env is unset
# (the parse runs at import and only flips it on a well-formed value).
_OBSERVE_POSE_FROM_ENV = False


def _parse_observe_pose() -> list[float]:
    """Observation pose ("Beobachtungspose") — the arm joints (rad) the
    named-object loop retreats to between passes so the arm is out of the scene
    camera's view and doesn't occlude the remaining objects during re-detection.
    ``EDUBOTICS_OBSERVE_POSE`` = comma-separated joint radians; default HOME.

    Parsed at module IMPORT, where the arm-joint count is unknown (no ctx). The
    VALUE is validated here (a non-numeric env warns + falls back); the per-profile
    LENGTH check is deferred to :func:`_observe_joints`, which knows ``_n(ctx)``."""
    global _OBSERVE_POSE_FROM_ENV
    raw = os.environ.get('EDUBOTICS_OBSERVE_POSE')
    if not raw:
        return list(HOME_JOINTS_RAD)
    try:
        vals = [float(v) for v in raw.split(',')]
    except (TypeError, ValueError):
        _logger.warning(
            '[WARNUNG] EDUBOTICS_OBSERVE_POSE=%r enthält keine kommagetrennten '
            'Zahlen — HOME wird verwendet.', raw)
        return list(HOME_JOINTS_RAD)
    _OBSERVE_POSE_FROM_ENV = True
    return vals


OBSERVE_POSE_JOINTS = _parse_observe_pose()
# Latch so the per-profile length-mismatch warning fires at most once per process.
_OBSERVE_POSE_WARNED = False


# ── ArmProfile ctx accessors (§16.4 slice 2b) ────────────────────────────────
# Every handler reads the arm's DOF/pose/gripper geometry through these instead
# of the OMX module constants. A ctx that carries no profile fields (every
# existing caller, every test SimpleNamespace) gets EXACTLY the old constants —
# OMX behaviour is bit-identical by construction. The node/workflow_manager
# stamp the fields from the resolved ArmProfile (PR 4).

def _n(ctx) -> int:
    """Arm-joint count (gripper index == n; full vector width == n + 1)."""
    try:
        n = int(getattr(ctx, 'num_arm_joints', None) or 0)
    except (TypeError, ValueError):
        n = 0
    return n if n > 0 else 5


def _roll_idx(ctx) -> int:
    """Index of the tool-roll joint (OMX joint5 → 4; edu6 joint6 → 5). Defaults
    to the LAST arm joint — true for both shipped geometries."""
    idx = getattr(ctx, 'roll_joint_index', None)
    if idx is None:
        return _n(ctx) - 1
    try:
        return int(idx)
    except (TypeError, ValueError):
        return _n(ctx) - 1


def _roll_arg(ctx, joints) -> float:
    """The ``roll=`` value that reproduces ``joints``' CURRENT wrist angle.

    NEVER pass ``joints[_roll_idx(ctx)]`` into ``roll=`` directly. The roll
    argument and the roll JOINT are the same number on the OMX
    (``theta5 = roll``) but NOT on edu6 (``q6 = fold(wrap(π − roll))``), so the
    shortcut mirrored the edu6 wrist by up to 178° on every Cartesian move
    (found 2026-07-25). The solver owns the conversion; this is just the
    profile-tolerant lookup, falling back to the old behaviour only for a
    solver/test-double that predates the method (OMX-identical there)."""
    idx = _roll_idx(ctx)
    ik = getattr(ctx, 'ik', None)
    convert = getattr(ik, 'roll_from_joints', None)
    if convert is not None:
        value = convert(joints)
        if value is not None:
            return float(value)
    return float(joints[idx])


def _home_joints(ctx) -> list[float]:
    """The profile HOME arm pose (length ``_n(ctx)``); OMX constant fallback."""
    pose = getattr(ctx, 'home_joints_rad', None)
    if pose is not None and len(pose) == _n(ctx):
        return [float(v) for v in pose]
    return list(HOME_JOINTS_RAD)


def _observe_joints(ctx) -> list[float]:
    """Observation pose. A profile-supplied pose wins; else the import-time
    ``EDUBOTICS_OBSERVE_POSE`` parse when its length matches this profile's
    arm-joint count; else HOME. A set-but-wrong-length env warns ONCE (German,
    with the expected count) instead of being silently ignored on an n≠5 rig."""
    pose = getattr(ctx, 'observe_pose_joints', None)
    if pose is not None and len(pose) == _n(ctx):
        return [float(v) for v in pose]
    if _OBSERVE_POSE_FROM_ENV:
        if len(OBSERVE_POSE_JOINTS) == _n(ctx):
            return list(OBSERVE_POSE_JOINTS)
        global _OBSERVE_POSE_WARNED
        if not _OBSERVE_POSE_WARNED:
            _OBSERVE_POSE_WARNED = True
            _logger.warning(
                '[WARNUNG] EDUBOTICS_OBSERVE_POSE hat %d Werte, benötigt werden '
                '%d — HOME wird verwendet.', len(OBSERVE_POSE_JOINTS), _n(ctx))
    return _home_joints(ctx)


def _gripper_open(ctx) -> float:
    val = getattr(ctx, 'gripper_open_rad', None)
    if val is None:
        return GRIPPER_OPEN_RAD
    try:
        val = float(val)
    except (TypeError, ValueError):
        return GRIPPER_OPEN_RAD
    return val if math.isfinite(val) else GRIPPER_OPEN_RAD


def _gripper_closed(ctx) -> float:
    val = getattr(ctx, 'gripper_closed_rad', None)
    if val is None:
        return GRIPPER_CLOSED_RAD
    try:
        val = float(val)
    except (TypeError, ValueError):
        return GRIPPER_CLOSED_RAD
    return val if math.isfinite(val) else GRIPPER_CLOSED_RAD


def _grasp_held_margin(ctx) -> float:
    """Per-profile grasp-held margin (rad above the commanded close that a
    blocked jaw must read to count as HELD). OMX 0.15; edu6 0.12."""
    val = getattr(ctx, 'grasp_held_margin_rad', None)
    if val is None:
        return GRASP_HELD_MARGIN_RAD
    try:
        val = float(val)
    except (TypeError, ValueError):
        return GRASP_HELD_MARGIN_RAD
    return val if (math.isfinite(val) and val > 0.0) else GRASP_HELD_MARGIN_RAD


def _velocity_limit(ctx) -> float:
    """Per-profile joint velocity limit for ``build_segment``'s floor (OMX 4.8,
    edu6 URDF 5.45)."""
    from physical_ai_server.workflow.trajectory_builder import (
        JOINT_VELOCITY_LIMIT_RAD_S,
    )
    val = getattr(ctx, 'velocity_limit_rad_s', None)
    if val is None:
        return JOINT_VELOCITY_LIMIT_RAD_S
    try:
        val = float(val)
    except (TypeError, ValueError):
        return JOINT_VELOCITY_LIMIT_RAD_S
    return val if (math.isfinite(val) and val > 0.0) else JOINT_VELOCITY_LIMIT_RAD_S


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
    waypoints = build_segment(q_start, q_end, duration_s,
                              velocity_limit=_velocity_limit(ctx))
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
    allow_monotone_escape: bool = False,
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
    # Direct path crosses a zone — plan a reroute (escape → lift-and-travel →
    # base-swing → refuse). plan_safe_route raises a German WorkflowError when
    # no safe route exists (→ red banner + stop).
    #
    # ``allow_monotone_escape`` is passed through for the RETREAT callers only
    # (home / the observation pose). They are the motions that must never be a
    # dead end, and they are the ones for which "the arm is standing inside the
    # zone" is a reason to LEAVE rather than a reason to refuse. Every transit
    # caller leaves it False and is byte-identical.
    legs = plan_safe_route(ctx, q_start, q_end, zones, duration_s, roll=roll,
                           allow_monotone_escape=allow_monotone_escape)
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
    seed = ctx.last_arm_joints or _home_joints(ctx)
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

# Smallest APPROACH clearance that still counts as an approach (metres).
#
# The comment above used to be a WISH, not a guarantee: the bisect below can and
# does converge to exactly 0, and the code then returned ``approach == grasp``
# and called it a success. Measured 2026-07-26 through the real solvers, grasping
# the shipped 30 mm cube (requested clearance 0.06):
#
#   edu6   19 / 704 radii in [0.030, 0.215] bisect to <= 2 mm  (2.7 %),
#          in two bands: r ∈ [0.0325, 0.0347] and r ∈ [0.2062, 0.2082]
#   OMX     2 / 958 radii in [0.020, 0.300]                    (0.2 %),
#          one band at the very lip of its annulus: r ∈ [0.2800, 0.2802]
#
# Three traps in those intervals, all found by re-measuring rather than re-reading:
#   * the NARROWER edu6 pair r ∈ [0.0325, 0.0335] ∪ [0.2075, 0.2082] (9 radii) is
#     a DIFFERENT measurement and must not be quoted for the floor. The COUNTS
#     (19 / 704, 2.7 %) were always exact; the interval labels were not.
#   * the OMX band holds at grasp z = 0.012 (GRASP_CLEARANCE_M). At z = 0.015 it
#     shifts to r ∈ [0.2795, 0.2797]. Quote the grasp HEIGHT with any of these
#     intervals or none of them means anything.
#   * at grasp z = 0.015 every refused edu6 radius rises exactly 1.875 mm — one
#     bisect step — NOT zero. "Zero clearance" is the failure MODE, not
#     universally the measured value; see
#     test_a_real_but_sub_floor_rise_is_refused_not_just_an_exactly_zero_one.
#
# In that state ``above_q == grasp_q`` AND ``lift_q == closed_q``: there is no
# descend and no lift-out, the TCP stays pinned at grasp height for the whole
# „Greife", the arm travels SIDEWAYS into the object at grasp height and knocks
# it over — and ``check_grasp_held`` then reports HELD (the jaws really are
# blocked, just by an object being shoved rather than grasped) so the German
# success line prints. A grasp with no clearance to descend through is not a
# grasp, so we refuse it by name instead of claiming it worked.
#
# WHY 2 mm and not a larger, more physical number: the bisect's own resolution
# is ``_APPROACH_BISECT_TOL_M``, so the smallest non-zero clearance it can even
# REPORT is ~1.9 mm — at or below that, "some clearance" and "no clearance" are
# indistinguishable, and accepting the value would be accepting a number we
# cannot verify. Tying the floor to the tolerance therefore refuses EXACTLY the
# measured non-grasps and nothing else: every radius that previously produced a
# real, non-zero clamped approach still produces the identical one, so the
# reachable band is NOT widened (nor narrowed beyond the non-grasps).
#
# The physically ideal threshold is larger — the fingertips only clear the
# object's top once the clearance reaches ``object_height_m − grasp_depth_m``
# (15 mm for the shipped cube), which would refuse 12.1 % of the edu6 band
# (85 of 704 swept radii — a COUNT of radii below that threshold, not a fire
# rate; see _APPROACH_WARN_FRAC, where the two were once conflated). That is a
# PRODUCT decision (it removes reach the student can see on the mat), not a
# correctness fix, so it is left to the rig gate; below that clearance the run
# now WARNS instead.
_MIN_APPROACH_CLEARANCE_M = _APPROACH_BISECT_TOL_M

# The clamped-approach warning fires only when the achieved clearance falls below
# this FRACTION of the requested one.
#
# It used to fire on ANY reduction, which on edu6 means every single grasp: the
# requested 60 mm hover is unreachable at every radius there (max achieved
# 50.6 mm, at r = 0.120), so the 2026-07-26 sweep logged 936 warnings over 780
# runs — 100 % of grasps. A warning that always fires is not a warning, it is
# noise that teaches students to ignore the log strip.
#
# 0.25 is not a taste value: the requested clearance is the recipe's
# ``approach_clear_m`` (0.060 on BOTH shipped catalogs) and the clearance at
# which the fingertips are exactly level with the object's top is
# ``object_height_m − grasp_depth_m`` = 0.030 − 0.015 = 0.015 m — precisely
# 0.25 × 0.060. So the threshold is "the hover no longer clears the object it is
# about to grasp", expressed relative to the request so it tracks a re-tuned
# recipe instead of hard-coding metres.
#
# Measured NEW fire rates (re-measured independently — do NOT reuse the 12.1 %
# above, which counts radii BELOW the 15 mm threshold, 85 / 704: 19 of those are
# refused outright before any warning can fire, so the two numbers count
# different things and an earlier revision of this comment conflated them):
#
#   edu6   9.4 % of attempts  (was 100 %)
#   OMX    0.8 % — 8 / 965 radii, the outer ~2 mm lip  (was 6.2 %)
#
# So the OMX is far quieter, NOT silent — it still warns at the very lip of its
# annulus, which is correct: that is where its hover really is clamped.
_APPROACH_WARN_FRAC = 0.25


def _max_reachable_rise(
    ctx,
    base_xyz: tuple[float, float, float],
    requested_m: float,
    roll: float | None = None,
    fallback_q: list[float] | None = None,
) -> tuple[float, list[float] | None]:
    """Largest reachable RISE in ``[0, requested_m]`` straight above
    ``base_xyz``, as ``(rise_m, arm_q_at_that_rise)``.

    THE single bisect behind all three "go as high as this arm can" callers —
    the grasp/place approach hover (:func:`_solve_grasp_and_approach`), the
    release rim clearance (:func:`_reachable_release_clearance`) and „hebe an"
    (:func:`lift`). They had three copies of the same loop, which is how the
    approach copy grew a grasp-specific warning that :func:`lift` then emitted
    about a lift (F2).

    Reachability of a fixed XY column is monotone in height (the strict-vertical
    annulus shrinks as z rises), so a bisect between 0 (assumed reachable — every
    caller has already solved its base point) and the requested rise finds the
    maximum. ``fallback_q`` is returned for a rise of 0, i.e. the base point's
    own solution.

    The workspace-floor ``WorkflowError`` is SWALLOWED (a below-table candidate
    is an invalid candidate, not a fatal error — the licence
    ``path_guard._try_via`` already takes). For the two approach callers that is
    provably a no-op: their base point passed ``_solve_or_raise``'s floor check,
    so ``base_z + rise`` with ``rise > 0`` can never be below the floor. For
    ``_reachable_release_clearance`` it is load-bearing — its target may itself
    sit under the table, and at ``rise → 0`` the probe reaches that height."""
    bx, by, bz = (float(v) for v in base_xyz)
    requested = max(0.0, float(requested_m))
    if requested <= 0.0:
        return 0.0, fallback_q

    def _probe(z: float) -> list[float] | None:
        try:
            return _try_solve(ctx, (bx, by, z), roll=roll)
        except WorkflowError:
            return None

    q = _probe(bz + requested)
    if q is not None:
        return requested, q
    # The annulus shrank at this height — bisect down toward the base point.
    # ``lo`` is always reachable (the base point, proven by the caller); ``hi``
    # is the unreachable requested height.
    lo, hi = 0.0, requested
    best_q, best_rise = fallback_q, 0.0
    while hi - lo > _APPROACH_BISECT_TOL_M:
        mid = 0.5 * (lo + hi)
        q = _probe(bz + mid)
        if q is not None:
            lo, best_q, best_rise = mid, q, mid
        else:
            hi = mid
    return best_rise, best_q


def _solve_grasp_and_approach(
    ctx,
    grasp_xyz: tuple[float, float, float],
    approach_height_m: float,
    roll: float | None = None,
    refuse_without_clearance: bool = True,
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
         approach height down to the MAX reachable lift, so the arm still
         approaches from above — just by a smaller, reachable amount — rather
         than refusing a graspable object.
      4. But for a GRASP, a clearance at or below ``_MIN_APPROACH_CLEARANCE_M``
         is NOT an approach: there is nothing to descend through, the whole
         „Greife" would be a lateral slide into the object at grasp height, and it
         would report success. That case is refused by name (see the constant for
         the measurement and for why the floor is the bisect's own resolution).

    An unreachable GRASP, and a grasp with no room to approach from above, are
    refused. Everything in between is clamped — and only reported to the student
    when the clamp is consequential (``_APPROACH_WARN_FRAC``).

    ``refuse_without_clearance=False`` keeps step 4 OFF, and ``drop_at`` is the
    one caller that sets it. A PLACE has neither half of the harm the refusal
    exists to prevent: the object is already in the jaws (nothing to knock over by
    arriving sideways) and nothing claims a grasp succeeded. Its clearance is
    additionally documented as OPTIONAL — the hard requirement is placing the
    object AT the target — and refusing a place over a missing rim clearance is
    precisely the regression the 2026-07-26 release-clearance bisect removed
    (measured: it costs the whole place to save 1.6 mm of clearance at the OMX
    outer ring, and ~87 % of the pick band on edu6). Re-introducing it here would
    undo that fix.
    """
    gx, gy, gz = (float(v) for v in grasp_xyz)
    grasp_arm_q = _solve_or_raise(ctx, (gx, gy, gz), roll=roll)

    requested = max(0.0, float(approach_height_m))
    best_lift, best_q = _max_reachable_rise(
        ctx, (gx, gy, gz), requested, roll=roll, fallback_q=grasp_arm_q)
    if best_lift >= requested:
        # Fast path: the full requested approach height was reachable.
        return grasp_arm_q, best_q

    if (refuse_without_clearance and requested > 0.0
            and best_lift <= _MIN_APPROACH_CLEARANCE_M):
        # GraspSkip, not the base WorkflowError: this is POSITIONAL and
        # per-instance — exactly GraspSkip's documented contract ("the selected
        # object can't be grasped right now … but OTHER instances may be fine").
        # The zero-clearance bands are the innermost and outermost rings of the
        # pick band, and `_select_nearest_reachable` sorts by distance, so a cube
        # pushed near the base is picked FIRST: a hard abort there would kill a
        # 3-cube program over one badly-placed cube while a perfectly good one sat
        # at mid-band. A standalone „Greife" / „fahre über" still fails loud
        # (GraspSkip IS a WorkflowError) with this exact message.
        raise GraspSkip(
            'Kein Platz, um von oben anzufahren — über diesem Punkt ist der Arm '
            'schon ganz ausgestreckt. Er könnte das Objekt nur von der Seite '
            'anschieben und würde es umwerfen. Bitte das Objekt etwas weiter in '
            'die Mitte des markierten Greifbereichs legen.'
        )
    if best_lift < _APPROACH_WARN_FRAC * requested:
        ctx.log(
            f'[WARNUNG] Anfahrhöhe auf {best_lift * 1000:.0f} mm über dem '
            f'Greifpunkt verkleinert (gewünscht: {requested * 1000:.0f} mm) — '
            'höher kommt der Arm über dieser Stelle nicht. Wenn der Greifer das '
            'Objekt beim Anfahren berührt, das Objekt weiter in die Mitte des '
            'Greifbereichs legen.'
        )
    return grasp_arm_q, best_q


def _reachable_release_clearance(
    ctx,
    target_xyz: tuple[float, float, float],
    requested_m: float,
    roll: float | None = None,
) -> float:
    """Largest RELEASE clearance in ``[0, requested_m]`` whose pose is reachable
    above ``target_xyz``. Same shape as the approach bisect above, and for the
    same reason.

    ``drop_at`` releases ``DROP_HEIGHT_M`` ABOVE the target so the jaws clear a
    container rim. That clearance is OPTIONAL — the hard requirement is placing
    the object AT the target — but the old code baked it into the solved point,
    so an unreachable +clearance refused an otherwise perfectly placeable target.

    On the OMX that never showed: its ceiling is ~0.25 m, so target+0.05 is
    reachable everywhere and this returns ``requested_m`` on the fast path,
    byte-identical to before. On edu6 the top-down ceiling is ~0.0655 m, and
    „lege ab bei (Position von X)" stacks TWO clearances — `object_position`
    already returns the GRASP height (0.015 for a 30 mm cube) and the release
    adds 0.05 on top, landing 0.5 mm under the absolute ceiling. Measured
    2026-07-26: that worked for r ∈ [0.1075, 0.1335] only — about 15 % of the
    pick band — and refused everywhere else, on what is the most natural thing a
    student writes.

    Bisecting keeps the FULL rim clearance wherever it is reachable (so nothing
    regresses mid-band) and shrinks it only where the geometry forces it. A
    clearance of 0 is not a failure mode: it releases at exactly the height the
    object was gripped from, which is precisely right for placing back onto a
    flat surface — only a deep container wants more, and that is what the German
    warning tells the student.
    """
    requested = max(0.0, float(requested_m))
    if requested <= 0.0:
        return 0.0
    # Same bisect as the approach hover — ``_max_reachable_rise`` owns it (and
    # swallows the workspace-floor error, which is load-bearing HERE: the drop
    # target may itself sit under the table and at ``rise → 0`` the probe reaches
    # exactly that height, so a raise would start refusing a path that previously
    # accepted it at +clearance. The single refusal point stays the
    # ``_solve_grasp_and_approach`` call below, with its own message).
    clearance, _q = _max_reachable_rise(ctx, target_xyz, requested, roll=roll)
    if clearance >= requested:
        return requested
    ctx.log(
        f'[WARNUNG] Ablegehöhe auf {clearance * 1000:.0f} mm über dem Ziel '
        f'reduziert (angefordert: {requested * 1000:.0f} mm) — höher ist an '
        'dieser Stelle nicht erreichbar. Zum Ablegen IN einen Behälter das Ziel '
        'näher an den Roboter legen.'
    )
    return clearance


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
    seed = ctx.last_arm_joints or _home_joints(ctx)
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


# Arrival tolerance for the workflow-side Grundstellung check. Deliberately the
# SAME 0.30 rad the edu6 driver's boot-home verifier uses
# (edu6_arm_node.BOOT_HOME_VERIFY_TOL_RAD) and the same value the OMX
# entrypoint's Phase-3 verifier uses — one number for "did the arm actually get
# where it was told", not three.
HOME_ARRIVAL_TOL_RAD = 0.30


def _warn_if_home_not_reached(ctx) -> None:
    """Emit ONE German [WARNUNG] if the arm did not actually arrive at HOME.

    WARN-ONLY, and never re-sends. The driver already owns the re-send (its
    boot-home verifier retries once from the MEASURED pose); duplicating that
    here would mean two writers racing the same rail. And a workflow may be
    stopping legitimately, in which case not arriving is correct — raising would
    turn a clean stop into an error.

    Best-effort throughout: no joint source, a short readback or a raising
    getter all mean "cannot tell", and a diagnostic must never break a home.
    """
    getter = getattr(ctx, 'get_follower_joints', None)
    log = getattr(ctx, 'log', None)
    if not callable(getter) or not callable(log):
        return
    try:
        actual = getter()
    except Exception:  # noqa: BLE001 — a diagnostic never breaks the move
        return
    n = _n(ctx)
    if not actual or len(actual) < n:
        return
    target = _home_joints(ctx)
    try:
        worst = max(abs(float(actual[i]) - float(target[i])) for i in range(n))
    except (TypeError, ValueError):
        return
    if not math.isfinite(worst) or worst <= HOME_ARRIVAL_TOL_RAD:
        return
    log('[WARNUNG] Der Arm hat die Grundstellung nicht ganz erreicht '
        f'(Abweichung {worst:.2f} rad). Bitte prüfen, ob etwas im Weg steht.')


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
    q_end = _home_joints(ctx) + [q_start[_n(ctx)]]
    # TRANSIT (whole-arm move to the Grundstellung).
    #
    # The route is PLANNED first (home_planner), then each leg published through
    # safe_move so it still gets the Sperrzone reroute, the velocity floor and
    # the global Tempo exactly as before. Straight to safe_move was NOT enough:
    # with no zones drawn safe_move is a pass-through, so the joint-space line
    # was judged by nothing — and HOME is outside the solver's image, so the
    # Cartesian workspace floor in _solve_or_raise can never see it either.
    # Measured: that line drove a link up to 167.9 mm BELOW the table in roughly
    # 1 in 100 attainable start poses.
    #
    # An arm with no link-box table (both OMX profiles, every profile-less ctx)
    # gets exactly one leg back, byte-identical to the old call.
    #
    # `home` is a pure Blockly block (handlers/__init__ `edubotics_home`), never
    # an un-catchable recovery/teardown, so the planner's German refusal is safe
    # to raise here.
    from physical_ai_server.workflow import home_planner
    legs = home_planner.plan_home_route(ctx, q_start, q_end,
                                        DEFAULT_HOME_DURATION_S)
    for leg_start, leg_end, leg_dur in legs:
        safe_move(ctx, leg_start, leg_end, leg_dur,
                  allow_monotone_escape=True)
    ctx.last_arm_joints = _home_joints(ctx)
    ctx.last_full_joints = q_end
    _warn_if_home_not_reached(ctx)


def go_to_observation_pose(ctx) -> None:
    """Move the arm to the observation pose (out of the scene-cam view) so the
    named-object loop's re-detection between passes isn't occluded by the arm.
    Carries the current gripper state (index 5) so a held object is NOT dropped.
    Used by the interpreter's ``edubotics_while_visible`` branch — not a Blockly
    block."""
    _require_seeded_start_pose(ctx)
    q_start = ctx.last_full_joints
    arm = _observe_joints(ctx)
    q_end = arm + [q_start[_n(ctx)]]
    # TRANSIT (whole-arm retreat out of the scene-cam view) — same class of move
    # as `home`, and by default the same TARGET (`_observe_joints` falls back to
    # HOME), so it gets the same treatment: plan the route, then publish each leg
    # through safe_move for the Sperrzone reroute + velocity floor + Tempo.
    # Without this the retreat is the identical unjudged joint-space line `home`
    # was, and it runs on every pass of a „Solange … sichtbar" loop.
    # No zones and no link-box table → one leg, byte-identical to before.
    #
    # Both internal call sites (interpreter `edubotics_while_visible` loop body
    # and the grasp_object retry branch in perception_blocks.py) run on paths
    # where a WorkflowError propagates as a normal German error — NEITHER is a
    # `finally`/un-catchable teardown — so a refusal here is safe (it ends the
    # run/loop cleanly, the intended "trapped arm" behavior).
    from physical_ai_server.workflow import home_planner
    legs = home_planner.plan_home_route(ctx, q_start, q_end,
                                        DEFAULT_MOVE_DURATION_S)
    for leg_start, leg_end, leg_dur in legs:
        safe_move(ctx, leg_start, leg_end, leg_dur,
                  allow_monotone_escape=True)
    ctx.last_arm_joints = arm
    ctx.last_full_joints = q_end


def open_gripper(ctx, args: dict[str, Any]) -> None:
    _require_seeded_start_pose(ctx)
    q_start = ctx.last_full_joints
    q_end = q_start[:_n(ctx)] + [_gripper_open(ctx)]
    _publish_motion(ctx, q_start, q_end, DEFAULT_GRIPPER_DURATION_S)
    ctx.last_full_joints = q_end


def close_gripper(ctx, args: dict[str, Any]) -> None:
    _require_seeded_start_pose(ctx)
    q_start = ctx.last_full_joints
    q_end = q_start[:_n(ctx)] + [_gripper_closed(ctx)]
    _publish_motion(ctx, q_start, q_end, DEFAULT_GRIPPER_DURATION_S)
    ctx.last_full_joints = q_end
    # Record the COMMANDED close for the per-object grasp-held threshold (only
    # the close paths write this — see _held_threshold_rad).
    ctx.last_commanded_close_rad = _gripper_closed(ctx)


def move_to(ctx, args: dict[str, Any]) -> None:
    _require_seeded_start_pose(ctx)
    target = _resolve_target(args.get('destination'), ctx)
    arm_q = _solve_or_raise(ctx, target, roll=GRASP_ROLL_RAD)
    q_end = arm_q + [ctx.last_full_joints[_n(ctx)]]
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

    open_q = ctx.last_full_joints[:_n(ctx)] + [_gripper_open(ctx)]
    above_q = above_arm_q + [_gripper_open(ctx)]
    grasp_q = grasp_arm_q + [_gripper_open(ctx)]
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
    if not math.isfinite(commanded):
        return GRASP_HELD_MAX_RAD
    closed = _gripper_closed(ctx)
    opened = _gripper_open(ctx)
    if closed < 0.0 and commanded >= 0.0:
        # OMX rule, verbatim: on a negative-close gripper a non-negative
        # "commanded close" is garbage — fall back to the fixed threshold.
        return GRASP_HELD_MAX_RAD
    if not (min(closed, opened) <= commanded < max(closed, opened)):
        # Band sanity for a non-negative-close profile (edu6 closes 0..1.75):
        # a command outside the physical band is equally garbage.
        return GRASP_HELD_MAX_RAD
    return commanded + _grasp_held_margin(ctx)


def check_grasp_held(ctx) -> bool | None:
    """Decide HELD vs EMPTY after a gripper close, from the achieved gripper
    angle (follower joint index ``_n(ctx)`` — 5 on the OMX, 6 on edu6).

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
    if not joints or len(joints) < _n(ctx) + 1:
        return None
    try:
        gripper = float(joints[_n(ctx)])
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
                    GRASP_ROLL_RAD, _gripper_closed(ctx), tempo=_move_tempo(args))


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
    # not just the grasp clearance — see DROP_HEIGHT_M. The clearance is OPTIONAL,
    # so it is bisected down to whatever is reachable here instead of refusing an
    # otherwise placeable target (see _reachable_release_clearance).
    _release_clear = _reachable_release_clearance(
        ctx, target, DROP_HEIGHT_M, roll=GRASP_ROLL_RAD)
    drop_xyz = (target[0], target[1], target[2] + _release_clear)

    # HIGH-5 (symmetric with pickup): solve the DROP first; derive the approach
    # from the reachable envelope so an outer-ring destination whose drop is
    # reachable isn't refused because its +approach pose fell outside the annulus.
    # refuse_without_clearance=False: a place with no room above it still places
    # (see _solve_grasp_and_approach) — refusing here would undo the release-
    # clearance bisect two lines above.
    drop_arm_q, above_arm_q = _solve_grasp_and_approach(
        ctx, drop_xyz, DEFAULT_APPROACH_HEIGHT_M, roll=GRASP_ROLL_RAD,
        refuse_without_clearance=False)

    # Carry at the close the GRASP actually commanded, not at the profile's
    # full-closed value. The two are the same number on the OMX
    # (gripper_closed_rad −0.5 == the catalog close −0.5), so this is a no-op
    # there — but on edu6 the catalog closes at +1.0 inside a 0…1.75 band while
    # gripper_closed_rad is 0.0, so carrying at "closed" demanded 1.0 rad MORE
    # closure than the grasp: 25.2 mm of extra jaw travel into a 30 mm cube that
    # is already blocking the jaws at ≈1.19 rad. That is a hard stall at
    # Max_Torque 150 for the whole carry + descend, on EVERY place (found by the
    # 2026-07-26 sim sweep; invisible on the OMX by construction).
    #
    # None (a `lege ab` with nothing ever grasped this run) falls back to the old
    # value, so that path is unchanged too.
    _carry_close = getattr(ctx, 'last_commanded_close_rad', None)
    if _carry_close is None or not math.isfinite(float(_carry_close)):
        _carry_close = _gripper_closed(ctx)
    _carry_close = float(_carry_close)
    above_closed_q = above_arm_q + [_carry_close]
    drop_closed_q = drop_arm_q + [_carry_close]
    drop_open_q = drop_arm_q + [_gripper_open(ctx)]
    retreat_open_q = above_arm_q + [_gripper_open(ctx)]

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


# The generic „you never ran finde" message — correct ONLY when we have no idea
# why the Greifziel is missing.
_NO_GREIFZIEL_MSG = (
    'Kein Greifziel — bitte „finde …" benutzen, das Ergebnis in einer '
    'Variable speichern und mit „falls" prüfen.'
)


def _no_greifziel_error(ctx) -> GraspSkip:
    """The error for a split-grasp block that got no Greifziel.

    ``find_object`` returns ``None`` for FOUR different reasons — nothing of that
    type visible, visible but out of reach, visible but the orientation could not
    be read, and (the real one) the student never called „finde". Only the last
    is what the generic message describes, yet it was raised for all four: a
    student who did latch „finde" into a variable AND guard it with „falls" was
    told to do exactly the three things they had just done, while the true reason
    („außerhalb des Greifbereichs") appeared only as a ``[WARNUNG]`` log line.

    ``perception_blocks.find_object`` now records the reason on
    ``ctx.last_find_failure`` (a plain attribute, like ``ctx._grasp_check_warned``
    — the ctx is rebuilt per run, so it can never carry over between runs), and
    this promotes it to the error the student actually sees. No reason recorded →
    the generic message, byte-identical to before."""
    reason = getattr(ctx, 'last_find_failure', None)
    if not isinstance(reason, str) or not reason.strip():
        return GraspSkip(_NO_GREIFZIEL_MSG)
    return GraspSkip(
        f'{reason.strip()} (Der Block hat deshalb kein Greifziel bekommen — '
        'mit „falls Ziel" prüfen, bevor der Arm bewegt wird.)'
    )


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
        raise _no_greifziel_error(ctx)
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
    q_end = arm_q + [ctx.last_full_joints[_n(ctx)]]
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
    q_end = arm_q + [ctx.last_full_joints[_n(ctx)]]
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
        raise _no_greifziel_error(ctx)
    _require_seeded_start_pose(ctx)
    close_rad = _gripper_closed(ctx)
    extras = getattr(ziel, 'extras', None)
    if isinstance(extras, dict):
        raw = extras.get('gripper_close_rad')
        if raw is not None:
            try:
                parsed = float(raw)
            except (TypeError, ValueError):
                parsed = _gripper_closed(ctx)
            # NaN/inf passes float() but would command a garbage gripper angle.
            close_rad = parsed if math.isfinite(parsed) else _gripper_closed(ctx)
            # Clamp a corrupt catalog value into the physical gripper band. A value
            # far outside [GRIPPER_CLOSED_RAD, GRIPPER_OPEN_RAD] otherwise makes
            # build_segment's velocity floor stretch this gripper close to tens of
            # seconds (e.g. close_rad=100 → ~65 s) — a wedged run — and commands a
            # servo angle the hardware can't reach.
            close_rad = max(_gripper_closed(ctx), min(_gripper_open(ctx), close_rad))
    q_start = ctx.last_full_joints
    q_end = q_start[:_n(ctx)] + [close_rad]
    _publish_motion(ctx, q_start, q_end, DEFAULT_GRIPPER_DURATION_S)
    ctx.last_full_joints = q_end
    # Record the COMMANDED close (the Greifziel's tuned angle) for the
    # per-object grasp-held threshold.
    ctx.last_commanded_close_rad = close_rad


def lift(ctx, args: dict[str, Any]) -> None:
    """„hebe an" — raise the end-effector straight up by the default approach
    height from its CURRENT position, preserving the wrist roll (j5) so a held
    object isn't twisted, and the gripper state so a held object stays held.

    A lift that cannot rise at all is reported as exactly that and does NOT
    move the arm. It used to borrow :func:`_solve_grasp_and_approach` wholesale,
    which meant two wrong things on edu6 (measured 2026-07-26): ``_execute_pickup``
    already ends AT the hover, which is already clamped at that arm's top-down
    ceiling (~0.0655 m), so a following „hebe an" bisected to 0 mm at EVERY
    radius tested (0.06 / 0.10 / 0.13 / 0.16 / 0.19 — the OMX solves the same
    second +60 mm lift cleanly out to r = 0.20) — and it then blamed „Ziel liegt
    am Rand des Greifbereichs" at r = 0.13, the sweet spot of the pick band, not
    an edge. It also cannot raise :func:`_solve_grasp_and_approach`'s new
    no-clearance refusal: a maxed-out lift is a legitimate no-op, not a failed
    grasp, and aborting the run there would break „Greife" → „hebe an"."""
    _require_seeded_start_pose(ctx)
    cur = ctx.last_full_joints
    if ctx.ik is None:
        raise WorkflowError(
            'Roboter-Beschreibung nicht verfügbar — der Bewegungsrechner (IK) '
            'konnte nicht gestartet werden. Bitte die Umgebung neu starten.'
        )
    pose = ctx.ik.fk(cur[:_n(ctx)])
    if pose is None:
        raise WorkflowError('Aktuelle Position ist unbekannt.')
    _R, t = pose
    x, y, z = float(t[0]), float(t[1]), float(t[2])
    # Step 1 is UNCHANGED from the old _solve_grasp_and_approach call: the CURRENT
    # pose must itself be re-solvable in the strict-vertical family, and that is
    # what refuses a lift from a pose the solver cannot reproduce (or one below
    # the table). Same call, same args (roll=None), same German messages.
    cur_arm_q = _solve_or_raise(ctx, (x, y, z))
    # Clamp the lift to the reachable envelope so an outer-ring grasp is never
    # STRANDED at table level by an unreachable full +approach lift: the current
    # (reachable) pose is the lower bound, the lift bisects up to the max
    # reachable height.
    rise_m, above_q = _max_reachable_rise(
        ctx, (x, y, z), DEFAULT_APPROACH_HEIGHT_M, fallback_q=cur_arm_q)
    if rise_m <= _MIN_APPROACH_CLEARANCE_M:
        # Nothing to gain — say so plainly and publish NOTHING (the old code
        # published a zero-length move and warned about the wrong thing).
        ctx.log(
            '[WARNUNG] „hebe an" hat den Arm nicht bewegt — über dieser Stelle '
            'ist er schon so hoch, wie er kommt. „Greife" hebt das Objekt '
            'bereits vom Tisch ab.'
        )
        ctx.last_arm_joints = list(cur[:_n(ctx)])
        return
    if rise_m < _APPROACH_WARN_FRAC * DEFAULT_APPROACH_HEIGHT_M:
        ctx.log(
            f'[WARNUNG] Nur {rise_m * 1000:.0f} mm angehoben (gewünscht: '
            f'{DEFAULT_APPROACH_HEIGHT_M * 1000:.0f} mm) — höher kommt der Arm '
            'über dieser Stelle nicht.'
        )
    # j5 is (very nearly) pure tool roll about the top-down tool axis — the tip
    # sits within ~1.6 mm of that axis (the EE y-offset the IK itself ignores) —
    # so overriding j5 after the solve keeps the position to sub-grasp-tolerance
    # while preserving the held object's orientation.
    arm_q = list(above_q)
    arm_q[_roll_idx(ctx)] = cur[_roll_idx(ctx)]
    q_end = arm_q + [cur[_n(ctx)]]
    # TRANSIT (straight-up lift) — route around any no-go zone. Optional per-move
    # „mit Tempo" override; None → workflow-global tempo.
    # roll= must be the SOLVER's roll argument, not the joint value (see
    # _roll_arg): a no-go-zone reroute re-solves its via-points with it, and on
    # edu6 the raw joint value would mirror the wrist at every one of them.
    safe_move(ctx, cur, q_end, DEFAULT_APPROACH_DURATION_S,
              roll=_roll_arg(ctx, cur), tempo=_move_tempo(args))
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

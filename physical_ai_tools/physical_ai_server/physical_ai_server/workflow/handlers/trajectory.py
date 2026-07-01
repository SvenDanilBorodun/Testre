#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Replay of hand-guided trajectories on the Roboter Studio workflow runtime.

A recorded trajectory (CONTRACT B: ``{"fps": int, "points": [[j1..j5, grip,
t_s], ...]}``) is played back by ``replay_trajectory`` (a first-class Blockly
statement block) OR by the ``/workshop/replay`` service in ``physical_ai_server``
— both go through the SAME ``resegment_trajectory`` helper here so the safety
story is identical.

CRITICAL SAFETY (Rule §2 divergence, documented): a replay drives the arm along
an ARBITRARY recorded JOINT path, so it deliberately BYPASSES the IK reach
annulus + the workspace-floor refusal that gate every other motion block (those
protect against unreachable Cartesian targets; a recorded joint path is reachable
by construction — the arm was physically hand-guided through it). What it does
NOT bypass is the per-joint VELOCITY floor: the recorded timestamps are never
published raw × speed. Each consecutive recorded pair is re-segmented through
``build_segment`` (whose ``_velocity_safe_duration`` floor extends any segment
that would exceed the safe per-joint velocity), so a ``speed`` of ×2 becomes
"≤×2, clamped where unsafe". A velocity-safe quintic lead-in from the arm's
CURRENT pose to the first recorded point is prepended so playback never jumps the
arm from wherever it is to the recording's start.

This is a WORKFLOW-LEVEL teaching feature (Rule §2), NOT an inference safety
envelope: it runs only on the workflow runtime and never reshapes a
recorded/replayed *inference* action.
"""

from __future__ import annotations

import math
from typing import Any

from physical_ai_server.workflow.handlers.motion import (
    WorkflowError,
    _require_seeded_start_pose,
)
from physical_ai_server.workflow.trajectory_builder import (
    DEFAULT_FPS,
    build_segment,
    chunked_publish,
)


# Playback speed multiplier clamp. Below the floor a replay would crawl; above
# the ceiling the velocity floor would extend most segments anyway (so a higher
# request is meaningless), and we never want a runaway multiplier from a crafted
# workflow_json. The velocity floor is the real safety bound; this just keeps the
# knob sane.
REPLAY_SPEED_MIN = 0.25
REPLAY_SPEED_MAX = 3.0
# Requested duration of the velocity-safe quintic lead-in from the arm's current
# pose to the first recorded waypoint (build_segment extends it if the arm is far
# from the recording's start).
DEFAULT_LEAD_IN_S = 1.5
# Contract-B point layout: [j1, j2, j3, j4, j5, grip, t_s] — 6 joints then time.
_POINT_LEN = 7


def clamp_speed(raw: Any) -> float:
    """Clamp a replay-speed request to ``[REPLAY_SPEED_MIN, REPLAY_SPEED_MAX]``;
    non-numeric / non-finite / non-positive → 1.0 (normal speed)."""
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(val) or val <= 0.0:
        return 1.0
    return max(REPLAY_SPEED_MIN, min(REPLAY_SPEED_MAX, val))


def extract_points(traj: Any) -> list[list[float]]:
    """Validate a CONTRACT-B trajectory (``{"fps", "points"}`` dict OR a bare
    points list) and return the list of ``[j1..j5, grip, t_s]`` rows.

    Raises a German ``WorkflowError`` on a malformed/short/corrupt recording so
    replay fails loud instead of driving garbage."""
    if isinstance(traj, dict):
        points = traj.get('points')
    else:
        points = traj
    if not isinstance(points, (list, tuple)) or len(points) < 2:
        raise WorkflowError('Die Aufnahme enthält keine Bewegung.')
    rows: list[list[float]] = []
    for p in points:
        if not isinstance(p, (list, tuple)) or len(p) < _POINT_LEN:
            raise WorkflowError('Die Aufnahme ist beschädigt.')
        try:
            row = [float(v) for v in p[:_POINT_LEN]]
        except (TypeError, ValueError):
            raise WorkflowError('Die Aufnahme ist beschädigt.')
        if not all(math.isfinite(v) for v in row):
            raise WorkflowError('Die Aufnahme ist beschädigt.')
        rows.append(row)
    return rows


def resegment_trajectory(
    points: list[list[float]],
    speed: float = 1.0,
    lead_in_from: list[float] | None = None,
    lead_in_duration: float = DEFAULT_LEAD_IN_S,
    lead_in_floor_check=None,
    point_floor_check=None,
) -> list[tuple[list[float], float]]:
    """Turn a CONTRACT-B point list into a velocity-safe, monotonic-time waypoint
    stream ready for ``chunked_publish``.

    Each consecutive recorded pair ``(q_i, q_{i+1})`` is re-segmented through
    ``build_segment(q_i, q_{i+1}, dt_i / speed)`` — so the quintic per-joint
    velocity floor bounds the playback speed (a fast ``speed`` on a big recorded
    jump gets clamped). A quintic lead-in from ``lead_in_from`` (the arm's current
    pose) to the first recorded waypoint is prepended so the arm is never jumped
    to the recording's start. Times are made globally increasing so
    ``chunked_publish`` paces each chunk correctly.

    ``lead_in_floor_check`` (optional): a ``callable(q6) -> bool`` that returns
    True when a lead-in waypoint's tool dips BELOW the table floor. The synthetic
    current-pose→recording-start lead-in is a straight JOINT-space interpolation
    that is NOT reach/floor-checked like every other motion (the recorded points
    themselves are reachable-above-table by construction; the lead-in is not). If
    a checker is supplied and any lead-in waypoint dips below the floor we REFUSE
    (raise ``WorkflowError``) rather than drive the tool through the table.
    DECISION: refuse rather than lift-to-safe-Z first — lifting needs IK inside
    this pure helper (too invasive); the student lifts the arm above the table and
    replays again. When no checker is supplied (or the floor is unknown) the
    lead-in behaves exactly as before (backward-safe).

    ``point_floor_check`` (optional): the SAME ``callable(q6) -> bool`` contract as
    ``lead_in_floor_check``, but applied to EACH RECORDED waypoint. A hand-guided
    recording is above-table by construction (you cannot hand-guide the tool
    through a solid table) and the checker uses the same
    ``z < fz - WORKSPACE_FLOOR_MARGIN_M`` slack as every other motion, so this
    never false-refuses a normal recording — it catches a corrupt / out-of-frame
    recording whose joint path dips the tool below the table. Refuses (raises
    ``WorkflowError``) rather than driving the tool through the table. When no
    checker is supplied (or the floor is unknown) the recorded points are used
    exactly as before (backward-safe).

    ``points`` MUST already be validated (see :func:`extract_points`)."""
    speed = clamp_speed(speed)
    seq: list[tuple[list[float], float]] = [
        ([float(v) for v in p[:6]], float(p[6])) for p in points
    ]
    segmented: list[tuple[list[float], float]] = []
    t_offset = 0.0

    # Non-finite guard: a NaN/Inf follower readback would publish NaN straight
    # through the trajectory (garbage arm command) or crash build_segment on an
    # Inf delta. Drop such a lead-in and fall back to the safe first-pose seed.
    use_lead_in = (
        lead_in_from is not None
        and len(lead_in_from) >= 6
        and all(math.isfinite(float(v)) for v in lead_in_from[:6])
    )
    if use_lead_in:
        lead = build_segment([float(v) for v in lead_in_from[:6]], seq[0][0],
                             max(0.0, float(lead_in_duration)))
        if lead_in_floor_check is not None:
            for q, _t in lead:
                below = False
                try:
                    below = bool(lead_in_floor_check(q))
                except Exception:  # noqa: BLE001 — a checker error must not block replay
                    below = False
                if below:
                    raise WorkflowError(
                        'Der Arm ist zu nah an der Tischebene, um sicher zur '
                        'Aufnahme-Startstellung zu fahren. Bitte den Arm zuerst '
                        'anheben und erneut abspielen.'
                    )
        for q, t in lead:
            segmented.append((q, t))
        t_offset = lead[-1][1] if lead else 0.0
    else:
        # No (usable) lead-in supplied: seed the stream with the first recorded
        # pose so the controller interpolates to it from the arm's actual pose
        # over one frame (the caller accepts a small initial jump).
        first_dt = 1.0 / DEFAULT_FPS
        segmented.append((list(seq[0][0]), first_dt))
        t_offset = first_dt

    # FIX 5 — floor-check EACH recorded waypoint (not just the synthetic lead-in):
    # a corrupt / out-of-frame recording could carry a joint path that dips the
    # tool below the table plane. Runs AFTER the lead-in check so a below-floor
    # lead-in keeps its own „anheben" message. Each checker call is guarded — a
    # checker error must not block replay (treat as not-below), exactly like the
    # lead-in check above.
    if point_floor_check is not None:
        for q_i, _t_i in seq:
            below = False
            try:
                below = bool(point_floor_check(q_i))
            except Exception:  # noqa: BLE001 — a checker error must not block replay
                below = False
            if below:
                raise WorkflowError(
                    'Die Aufnahme führt unter die Tischebene — bitte eine neue '
                    'Aufnahme oberhalb des Tisches machen.'
                )

    for i in range(len(seq) - 1):
        q0, t0 = seq[i]
        q1, t1 = seq[i + 1]
        dt = (t1 - t0) / speed
        if not math.isfinite(dt) or dt <= 0.0:
            # Coincident/backwards timestamps (should not happen post-validation)
            # → one frame, so the arm still traverses q0→q1 at the velocity floor.
            dt = 1.0 / DEFAULT_FPS
        seg = build_segment(q0, q1, dt)
        for q, t in seg:
            segmented.append((q, t_offset + t))
        if seg:
            t_offset += seg[-1][1]
    return segmented


def replay_trajectory(ctx, args: dict[str, Any]) -> None:
    """„Aufnahme abspielen" — replay a recorded hand-guided trajectory named
    ``args['name']`` from ``ctx.trajectories`` at ``args['speed']`` (default 1.0).

    Fails loud (German ``WorkflowError``) on an unknown name / empty recording.
    Re-segments through the velocity floor (see :func:`resegment_trajectory`),
    holds ``ctx.motion_lock`` for the publish (so a hat handler can't interleave),
    and polls ``ctx.should_stop`` between chunks so a Stop halts it. Works in sim
    too (``ctx.publisher`` is the SimArm sink)."""
    name = (str(args.get('name') or '')).strip()
    if not name:
        raise WorkflowError('Kein Aufnahme-Name angegeben.')
    trajectories = getattr(ctx, 'trajectories', None) or {}
    traj = trajectories.get(name)
    if traj is None:
        raise WorkflowError(f'Unbekannte Aufnahme: {name}')
    points = extract_points(traj)
    speed = clamp_speed(args.get('speed'))

    # Lead-in from the arm's current pose so the replay never jumps to the
    # recording's start. Requires a seeded start pose (same guard as every other
    # motion block).
    _require_seeded_start_pose(ctx)
    current = list(getattr(ctx, 'last_full_joints', None) or [])
    lead_in = current if len(current) >= 6 else None

    # Floor guard for the synthetic lead-in (the recorded points are reachable-
    # above-table by construction; the current-pose→start interpolation is not).
    # Reuses motion._floor_z_at so the plane/scalar table-height logic matches
    # every other motion. Returns False (no refusal) when the floor is unknown.
    from physical_ai_server.workflow.handlers.motion import (
        WORKSPACE_FLOOR_MARGIN_M as _FLOOR_MARGIN,
        _floor_z_at,
    )

    def _lead_floor(q6):
        ik = getattr(ctx, 'ik', None)
        if ik is None:
            return False
        if getattr(ctx, 'z_table', None) is None and getattr(ctx, 'table_plane', None) is None:
            return False
        try:
            pose = ik.fk([float(v) for v in q6[:5]])
        except Exception:  # noqa: BLE001
            return False
        if pose is None:
            return False
        _R, t = pose
        fz = _floor_z_at(ctx, float(t[0]), float(t[1]))
        if fz is None:
            return False
        return float(t[2]) < fz - _FLOOR_MARGIN

    segmented = resegment_trajectory(
        points, speed, lead_in_from=lead_in, lead_in_floor_check=_lead_floor,
        point_floor_check=_lead_floor)
    if not segmented:
        raise WorkflowError('Die Aufnahme enthält keine Bewegung.')

    # Serialize against the main stack + hat handlers, exactly like
    # motion._publish_motion (RLock, 10 s upper bound → German error, never a
    # silent lock-through).
    lock = getattr(ctx, 'motion_lock', None)
    acquired = False
    if lock is not None:
        acquired = lock.acquire(timeout=10.0)
        if not acquired:
            raise WorkflowError(
                'Bewegung blockiert — ein anderer Workflow-Teil hält die Sperre '
                'zu lange. Bitte Workflow neu starten.'
            )
    try:
        ok = chunked_publish(
            publisher=ctx.publisher,
            points=segmented,
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
    # Chain subsequent motion from the final replayed pose.
    final_q = list(segmented[-1][0])
    ctx.last_full_joints = final_q
    ctx.last_arm_joints = final_q[:5]

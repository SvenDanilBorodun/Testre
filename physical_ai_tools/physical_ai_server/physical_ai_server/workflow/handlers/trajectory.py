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
annulus that gates every other motion block (it protects against unreachable
Cartesian targets; a recorded joint path is reachable by construction — the arm
was physically hand-guided through it). What it does NOT bypass:

* the WORKSPACE FLOOR — every recorded waypoint AND the synthetic lead-in are
  floor-checked (``point_floor_check`` / ``lead_in_floor_check``) and a dip below
  the table REFUSES the replay;
* the SPERRZONEN — this list used to omit them and the code used to match:
  measured 2026-09-07, omx_full, one zone {min [0.125, −0.035, 0.038], max
  [0.175, 0.035, 0.058]}, a replay published 75 waypoints of which 11 consecutive
  pairs swept the zone, with NO zone log line at all. A recorded path cannot be
  REROUTED (there is nothing to re-solve — that is the whole point of a replay),
  so the only correct answer is the one the floor check already gives: REFUSE,
  in German, before publishing anything;
* the per-joint VELOCITY floor: the recorded timestamps are never
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
    _hold_motion_lock,
    _release_motion_lock,
    _require_seeded_start_pose,
    _warn_unreadable_zones,
)
from physical_ai_server.workflow.trajectory_builder import (
    DEFAULT_FPS,
    JOINT_VELOCITY_LIMIT_RAD_S,
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
# Hard bound on a recording's own TIME COLUMN (seconds from its first to its
# last point). It bounds the whole stream ONLY together with extract_points'
# non-decreasing check — alone it measures the endpoints while the cost is
# per-pair; see extract_points for the measured 114-byte bypass. The recorder's
# cap is RECORD_MAX_S = 120 s, so this is ~5x any real recording and refuses
# only a corrupt or hand-crafted one.
MAX_TRAJECTORY_SPAN_S = 600.0
# Contract-B point layout: [j1..jn, grip, t_s] — (num_arm_joints + 2) floats.
# 7 is the OMX width (n=5); extract_points/resegment_trajectory take the arm's
# ``num_arm_joints`` and derive the width, asserting it EXACTLY (§16.4 rail #2).
_POINT_LEN = 7


def _replay_tempo(ctx) -> float:
    """The workflow-global run-bar Tempo as a replay speed multiplier.

    Reads ``ctx.tempo`` through motion's own resolver so the clamp window
    ([_TEMPO_MIN, _TEMPO_MAX]) and the "non-finite / non-positive / non-numeric →
    1.0" rule are the SAME ones every other motion block uses — one definition of
    Tempo, not two. Never raises: any ctx without the field (every
    non-Roboter-Studio path, every test double) resolves to 1.0, i.e. today's
    behaviour."""
    try:
        from physical_ai_server.workflow.handlers.motion import _resolve_tempo
        return float(_resolve_tempo(ctx, None))
    except Exception:  # noqa: BLE001 — a teaching knob never breaks a replay
        return 1.0


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


def extract_points(traj: Any, num_arm_joints: int = 5) -> list[list[float]]:
    """Validate a CONTRACT-B trajectory (``{"fps", "points"}`` dict OR a bare
    points list) and return the list of ``[j1..jn, grip, t_s]`` rows.

    Width is asserted EXACTLY ``num_arm_joints + 2`` (§16.4 permanent rail #2):
    a short row was always refused; a WIDE row used to be silently TRUNCATED
    (``p[:_POINT_LEN]``) — an 8-wide edu6 point on a 5-DOF build would narrow
    silently into a plausible-but-wrong command. Both directions now refuse.

    The TIME COLUMN is bounded too — by MONOTONICITY *and* the endpoint span,
    and the pair is the point. Nothing else bounds it: the cloud's
    ``validate_trajectory`` caps the point count, the width, the JSON size and
    the fps, but never ``t_s``, and ``/workflow/start`` over rosbridge
    authenticates nobody. ``resegment_trajectory`` turns each pair into
    ``round(dt * 30)`` waypoints, so the time column is a direct multiplier on
    memory. Measured 2026-09-07: dt = 1 s → 31 waypoints, dt = 1000 s → 30 001
    (~6 MB), and a TWO-POINT payload of 97 BYTES with dt = 1e9 implies ~3·10^10
    waypoints against a 6 GB container ``mem_limit`` — i.e. an OOM-kill of the
    whole ROS node from a payload smaller than this docstring.

    The endpoint span ALONE did not deliver that (measured 2026-09-08 on a
    working-tree revision — it never reached a student, and neither check exists
    on the pre-round `main`): it bounds ``rows[-1][-1] - rows[0][-1]`` while the cost is
    per-pair, so a payload with an internal time SPIKE and a tiny span sailed
    through — ``[0, 2000, 0.1]`` is 114 bytes and 60 002 waypoints. Requiring the
    column to be NON-DECREASING closes it, because then ``sum(dt)`` IS that span
    and one number bounds the whole stream. Both caps are generous by two orders
    of magnitude against the recorder's own ``RECORD_MAX_S`` of 120 s and its
    strictly-increasing ``time.monotonic()`` stamps, so no real recording can
    reach either.

    Raises a German ``WorkflowError`` on a malformed/short/corrupt recording so
    replay fails loud instead of driving garbage."""
    if isinstance(traj, dict):
        points = traj.get('points')
    else:
        points = traj
    if not isinstance(points, (list, tuple)) or len(points) < 2:
        raise WorkflowError('Die Aufnahme enthält keine Bewegung.')
    point_len = int(num_arm_joints) + 2
    rows: list[list[float]] = []
    for p in points:
        if not isinstance(p, (list, tuple)) or len(p) != point_len:
            raise WorkflowError('Die Aufnahme ist beschädigt.')
        try:
            row = [float(v) for v in p[:point_len]]
        except (TypeError, ValueError):
            raise WorkflowError('Die Aufnahme ist beschädigt.')
        if not all(math.isfinite(v) for v in row):
            raise WorkflowError('Die Aufnahme ist beschädigt.')
        rows.append(row)
    # TWO conditions, and NEITHER is sufficient alone (measured 2026-09-08).
    # The endpoint span was the only check and it bounds the wrong quantity: the
    # cost is driven by the PER-PAIR dt, because resegment_trajectory emits
    # round(dt * 30) waypoints per consecutive pair. A three-point payload whose
    # first and last stamps are close but which contains an internal SPIKE has a
    # tiny span and an enormous per-pair dt:
    #   0 -> 2000  -> 0.1   (114 B)  ->  60 002 waypoints   (span 0.1 s: passed)
    #   0 -> 20000 -> 0.1   (115 B)  -> 600 002 waypoints   (span 0.1 s: passed)
    # scaled to 1e9 that is ~6·10^10 waypoints against the 6 GB container
    # mem_limit — the OOM-kill of the whole ROS node, from a payload under
    # 120 bytes, i.e. exactly what this cap exists to prevent.
    # MONOTONICITY is what makes the endpoint check complete: with every dt >= 0,
    # sum(dt) IS the endpoint span, so one bound covers the total waypoint count
    # (<= 30 * span + pairs). It costs honest recordings nothing —
    # _manual_record_sample stamps t = time.monotonic() - start, strictly
    # increasing by construction. Both failures are one refusal because the
    # sentence already names both ("zu lang ODER ihre Zeitangaben sind
    # beschädigt"): a student cannot act differently on the two.
    times = [r[-1] for r in rows]
    span = times[-1] - times[0]
    monotonic = all(times[i + 1] >= times[i] for i in range(len(times) - 1))
    if not monotonic or not (0.0 <= span <= MAX_TRAJECTORY_SPAN_S):
        raise WorkflowError(
            'Die Aufnahme ist zu lang oder ihre Zeitangaben sind beschädigt — '
            f'bitte eine neue Aufnahme machen (höchstens {MAX_TRAJECTORY_SPAN_S:.0f} '
            'Sekunden).'
        )
    return rows


def _add_finite_diff_velocities(
    segmented: list[tuple[list[float], float]],
) -> list[tuple[list[float], float, list[float]]]:
    """Attach a per-joint velocity to each ``(q, t)`` waypoint via a CENTRAL finite
    difference over the global stream, with ``v = 0`` at the very first + last
    waypoint (start/stop at rest); return ``(q, t, v)`` 3-tuples.

    Replay-only smoothness layer: with velocities present the controller uses a
    cubic spline that keeps velocity continuity ACROSS separately-published chunk
    boundaries (no hold/near-stop at each ~1 s boundary → smooth playback), while
    the swept joint PATH is unchanged. Velocities are bounded by construction — the
    segment times already passed ``build_segment``'s per-joint velocity floor, so a
    central difference cannot exceed the safe per-joint velocity."""
    n = len(segmented)
    out: list[tuple[list[float], float, list[float]]] = []
    for i in range(n):
        q_i = segmented[i][0]
        t_i = segmented[i][1]
        m = len(q_i)
        if i == 0 or i == n - 1:
            v = [0.0] * m
        else:
            q_prev, t_prev = segmented[i - 1][0], segmented[i - 1][1]
            q_next, t_next = segmented[i + 1][0], segmented[i + 1][1]
            dt = t_next - t_prev
            if not math.isfinite(dt) or dt <= 0.0:
                v = [0.0] * m
            else:
                v = [(q_next[j] - q_prev[j]) / dt for j in range(m)]
        out.append((list(q_i), float(t_i), v))
    return out


def resegment_trajectory(
    points: list[list[float]],
    speed: float = 1.0,
    lead_in_from: list[float] | None = None,
    lead_in_duration: float = DEFAULT_LEAD_IN_S,
    lead_in_floor_check=None,
    point_floor_check=None,
    with_velocities: bool = False,
    num_arm_joints: int = 5,
    velocity_limit: float = JOINT_VELOCITY_LIMIT_RAD_S,
) -> list[tuple[list[float], float]] | list[tuple[list[float], float, list[float]]]:
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
    width = int(num_arm_joints) + 1  # full joint vector: n arm joints + gripper
    seq: list[tuple[list[float], float]] = [
        ([float(v) for v in p[:width]], float(p[width])) for p in points
    ]
    segmented: list[tuple[list[float], float]] = []
    t_offset = 0.0

    # Non-finite guard: a NaN/Inf follower readback would publish NaN straight
    # through the trajectory (garbage arm command) or crash build_segment on an
    # Inf delta. Drop such a lead-in and fall back to the safe first-pose seed.
    use_lead_in = (
        lead_in_from is not None
        and len(lead_in_from) >= width
        and all(math.isfinite(float(v)) for v in lead_in_from[:width])
    )
    if use_lead_in:
        lead = build_segment([float(v) for v in lead_in_from[:width]], seq[0][0],
                             max(0.0, float(lead_in_duration)),
                             velocity_limit=velocity_limit)
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
        seg = build_segment(q0, q1, dt, velocity_limit=velocity_limit)
        for q, t in seg:
            segmented.append((q, t_offset + t))
        if seg:
            t_offset += seg[-1][1]
    # Replay opts into per-waypoint velocities (smooth cubic playback); every
    # other caller / the tests get the unchanged (q, t) 2-tuple stream.
    if with_velocities:
        return _add_finite_diff_velocities(segmented)
    return segmented


def _refuse_if_replay_crosses_a_zone(ctx, segmented, num_arm_joints: int) -> None:
    """Refuse, in German, when the resegmented replay would sweep a Sperrzone.

    Checked on the FINAL waypoint stream (lead-in included) and BEFORE anything
    is published, so a refusal costs zero motion — the same shape as the
    workspace-floor refusal above. A replay cannot be REROUTED: its joint path is
    the recording, and there is nothing to re-solve. See the module docstring for
    the measurement (11 of 75 published waypoint pairs swept a zone, silently).

    Backward-safe throughout: no zones, no solver, or a solver without
    ``link_points`` (both OMX profiles never reach here without zones drawn)
    behaves exactly as before. ``segment_blocked`` already reports False for
    geometry it cannot reason about — which is precisely why an UNREADABLE zone
    payload has to be reported before that early return."""
    zones = getattr(ctx, 'zones', None)
    if zones:
        # The warning belongs to the ZONES being unreadable, not to whether
        # THIS path can act on them — and it must sit ABOVE the early return,
        # because an all-malformed payload is TRUTHY: `build_zones` skips every
        # entry silently by design, `segment_blocked` then finds zero boxes to
        # sweep, and the replay ran with no protection and not one word to the
        # student. Of the two paths this is the worse one: „bewege zu"
        # re-solves and reroutes, a replay drives the recorded joint path
        # verbatim — a hand-guided recording through arbitrary space is exactly
        # where an unprotected run matters.
        #
        # The latch lives on the ctx (`_zones_unreadable_warned`), so a run that
        # both MOVES and REPLAYS still emits exactly one warning. That is why it
        # must not become per-function state.
        _warn_unreadable_zones(ctx, zones)
    ik = getattr(ctx, 'ik', None)
    if not zones or ik is None or not callable(getattr(ik, 'link_points', None)):
        return
    from physical_ai_server.workflow.path_guard import segment_blocked
    width = int(num_arm_joints) + 1
    prev = None
    for item in segmented:
        q = list(item[0])[:width]
        if prev is not None:
            try:
                blocked = segment_blocked(ik, prev, q, zones)
            except Exception:  # noqa: BLE001 — never block a replay on a guard error
                blocked = False
            if blocked:
                raise WorkflowError(
                    'Die Aufnahme führt durch eine Sperrzone — eine Aufnahme '
                    'kann nicht um eine Sperrzone herumfahren. Bitte die '
                    'Sperrzone anpassen oder eine neue Aufnahme machen.'
                )
        prev = q


def replay_trajectory(ctx, args: dict[str, Any]) -> None:
    """„Aufnahme abspielen" — replay a recorded hand-guided trajectory named
    ``args['name']`` from ``ctx.trajectories`` at ``args['speed']`` (default 1.0).

    Fails loud (German ``WorkflowError``) on an unknown name / empty recording.
    Re-segments through the velocity floor (see :func:`resegment_trajectory`),
    holds ``ctx.motion_lock`` for the publish (so a hat handler can't interleave),
    and polls ``ctx.should_stop`` between chunks so a Stop halts it. Works in sim
    too (``ctx.publisher`` is the SimArm sink)."""
    from physical_ai_server.workflow.handlers.motion import (
        _n as _num_joints,
        _velocity_limit,
    )
    name = (str(args.get('name') or '')).strip()
    if not name:
        raise WorkflowError('Kein Aufnahme-Name angegeben.')
    trajectories = getattr(ctx, 'trajectories', None) or {}
    traj = trajectories.get(name)
    if traj is None:
        raise WorkflowError(f'Unbekannte Aufnahme: {name}')
    n = _num_joints(ctx)
    points = extract_points(traj, num_arm_joints=n)
    # Run-bar Tempo. „langsam"/„schnell" slow or speed up EVERY other motion
    # block (via motion._publish_motion's single choke point) and had no effect
    # at all here: replay does not go through _publish_motion, and the Blockly
    # block carries no SPEED field, so clamp_speed(args.get('speed')) was always
    # 1.0. Measured 2026-09-07: ctx.tempo 0.5 / 1.0 / 2.0 all produced the same
    # 69 waypoints over the same 0.500 s span. Folding the tempo into the replay
    # speed is safe by the same argument the module docstring already makes for
    # `speed`: build_segment's per-joint VELOCITY FLOOR re-extends any segment a
    # fast request would make unsafe, so a tempo of 2 is "≤×2, clamped where
    # unsafe". clamp_speed bounds the product to [0.25, 3.0].
    speed = clamp_speed(clamp_speed(args.get('speed')) * _replay_tempo(ctx))

    # Lead-in from the arm's current pose so the replay never jumps to the
    # recording's start. Requires a seeded start pose (same guard as every other
    # motion block).
    _require_seeded_start_pose(ctx)
    current = list(getattr(ctx, 'last_full_joints', None) or [])
    lead_in = current if len(current) >= n + 1 else None

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
            pose = ik.fk([float(v) for v in q6[:n]])
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
        point_floor_check=_lead_floor, with_velocities=True,
        num_arm_joints=n, velocity_limit=_velocity_limit(ctx))
    if not segmented:
        raise WorkflowError('Die Aufnahme enthält keine Bewegung.')
    _refuse_if_replay_crosses_a_zone(ctx, segmented, n)

    # Serialize against the main stack + hat handlers through the ONE acquire
    # helper (motion._hold_motion_lock), exactly like motion._publish_motion.
    # This was a raw ``acquire(timeout=10.0)`` that answered a Stop with a
    # „Bewegung blockiert" error naming a RESTART as the remedy — a Stop
    # reported to the student as a lock error, advising the one action that
    # reproduces it identically.
    acquired = _hold_motion_lock(ctx)
    try:
        ok = chunked_publish(
            publisher=ctx.publisher,
            points=segmented,
            should_stop=ctx.should_stop,
        )
    finally:
        _release_motion_lock(ctx, acquired)
    if not ok:
        raise WorkflowError('Workflow wurde gestoppt.')
    # Chain subsequent motion from the final replayed pose.
    final_q = list(segmented[-1][0])
    ctx.last_full_joints = final_q
    ctx.last_arm_joints = final_q[:n]

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
# Fixed tool roll (j5) for the grasp. 0 = the gripper's default orientation,
# which grips cubes / small objects in any orientation. Orientation-from-box
# (rotating j5 across an elongated object's short axis) is a rig-calibrated
# refinement — it needs the camera-yaw + a rotated detection box and is left as
# a tunable offset here rather than a guessed world-angle mapping.
GRASP_ROLL_RAD = math.radians(_safe_float('EDUBOTICS_GRASP_ROLL_DEG', 0.0))
# Workspace floor: never command the end-effector below the table plane.
WORKSPACE_FLOOR_MARGIN_M = 0.01

DEFAULT_HOME_DURATION_S = 3.0
DEFAULT_MOVE_DURATION_S = 2.5
DEFAULT_GRIPPER_DURATION_S = 0.5
DEFAULT_APPROACH_DURATION_S = 1.5
DEFAULT_GRASP_DURATION_S = 1.0


class WorkflowError(Exception):
    """Raised by handlers with a German message ready for the editor's
    log strip and toast."""


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


def _publish_motion(ctx, q_start: list[float], q_end: list[float], duration_s: float) -> None:
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
    # perception_blocks._attach_world_xyz leaves it None (silently — so the
    # count_* blocks keep working) when the calibration is incomplete:
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
    _publish_motion(ctx, q_start, q_end, DEFAULT_HOME_DURATION_S)
    ctx.last_arm_joints = list(HOME_JOINTS_RAD)
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


def move_to(ctx, args: dict[str, Any]) -> None:
    _require_seeded_start_pose(ctx)
    target = _resolve_target(args.get('destination'), ctx)
    arm_q = _solve_or_raise(ctx, target, roll=GRASP_ROLL_RAD)
    q_end = arm_q + [ctx.last_full_joints[5]]
    _publish_motion(ctx, ctx.last_full_joints, q_end, DEFAULT_MOVE_DURATION_S)
    ctx.last_arm_joints = arm_q
    ctx.last_full_joints = q_end


def pickup(ctx, args: dict[str, Any]) -> None:
    _require_seeded_start_pose(ctx)
    target = _resolve_target(args.get('target'), ctx)
    # Conservative descend: grasp at the measured table plane + clearance so the
    # fingertips straddle the lower part of a low object, not the table itself.
    grasp_xyz = (target[0], target[1], target[2] + GRASP_CLEARANCE_M)

    # HIGH-5: solve the GRASP first; derive the approach from the reachable
    # envelope (clamping the lift down at the annulus edge) instead of refusing
    # a graspable object because its +approach pose fell outside the annulus.
    grasp_arm_q, above_arm_q = _solve_grasp_and_approach(
        ctx, grasp_xyz, DEFAULT_APPROACH_HEIGHT_M, roll=GRASP_ROLL_RAD)
    lift_arm_q = above_arm_q

    open_q = ctx.last_full_joints[:5] + [GRIPPER_OPEN_RAD]
    above_q = above_arm_q + [GRIPPER_OPEN_RAD]
    grasp_q = grasp_arm_q + [GRIPPER_OPEN_RAD]
    closed_q = grasp_arm_q + [GRIPPER_CLOSED_RAD]
    lift_q = lift_arm_q + [GRIPPER_CLOSED_RAD]

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
        _publish_motion(ctx, ctx.last_full_joints, open_q, DEFAULT_GRIPPER_DURATION_S)
        ctx.last_full_joints = open_q
        _publish_motion(ctx, open_q, above_q, DEFAULT_MOVE_DURATION_S)
        ctx.last_full_joints = above_q
        ctx.last_arm_joints = above_arm_q
        _publish_motion(ctx, above_q, grasp_q, DEFAULT_GRASP_DURATION_S)
        ctx.last_full_joints = grasp_q
        ctx.last_arm_joints = grasp_arm_q
        _publish_motion(ctx, grasp_q, closed_q, DEFAULT_GRIPPER_DURATION_S)
        ctx.last_full_joints = closed_q
        _publish_motion(ctx, closed_q, lift_q, DEFAULT_APPROACH_DURATION_S)
        ctx.last_arm_joints = lift_arm_q
        ctx.last_full_joints = lift_q
    finally:
        if lock is not None:
            try:
                lock.release()
            except RuntimeError:
                pass


def drop_at(ctx, args: dict[str, Any]) -> None:
    """Place the held object at ``destination``. Symmetric with ``pickup``:
    approach +DEFAULT_APPROACH_HEIGHT_M above the target with the gripper
    closed, descend to the target, open, then retreat back above. The v1
    ship moved straight to the target XYZ in joint space, which produced
    a swept-arc carry path — adjacent obstacles could be clipped on the
    way in. The bounded-quintic approach is consistent with pickup."""
    _require_seeded_start_pose(ctx)
    target = _resolve_target(args.get('destination'), ctx)
    drop_xyz = (target[0], target[1], target[2] + GRASP_CLEARANCE_M)

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
        _publish_motion(ctx, ctx.last_full_joints, above_closed_q, DEFAULT_MOVE_DURATION_S)
        ctx.last_full_joints = above_closed_q
        ctx.last_arm_joints = above_arm_q
        _publish_motion(ctx, above_closed_q, drop_closed_q, DEFAULT_APPROACH_DURATION_S)
        ctx.last_full_joints = drop_closed_q
        ctx.last_arm_joints = drop_arm_q
        _publish_motion(ctx, drop_closed_q, drop_open_q, DEFAULT_GRIPPER_DURATION_S)
        ctx.last_full_joints = drop_open_q
        _publish_motion(ctx, drop_open_q, retreat_open_q, DEFAULT_APPROACH_DURATION_S)
        ctx.last_arm_joints = above_arm_q
        ctx.last_full_joints = retreat_open_q
    finally:
        if lock is not None:
            try:
                lock.release()
            except RuntimeError:
                pass


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

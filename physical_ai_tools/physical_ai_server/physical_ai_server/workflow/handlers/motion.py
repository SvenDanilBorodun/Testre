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

import math
import time
from typing import Any

from physical_ai_server.workflow.trajectory_builder import (
    build_segment,
    chunked_publish,
)


HOME_JOINTS_RAD = [0.0, -math.pi / 4, math.pi / 4, 0.0, 0.0]
DEFAULT_APPROACH_HEIGHT_M = 0.06
GRIPPER_OPEN_RAD = 0.8
GRIPPER_CLOSED_RAD = -0.5

DEFAULT_HOME_DURATION_S = 3.0
DEFAULT_MOVE_DURATION_S = 2.5
DEFAULT_GRIPPER_DURATION_S = 0.5
DEFAULT_APPROACH_DURATION_S = 1.5
DEFAULT_GRASP_DURATION_S = 1.0


class WorkflowError(Exception):
    """Raised by handlers with a German message ready for the editor's
    log strip and toast."""


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


def _solve_or_raise(ctx, target_xyz: tuple[float, float, float], free_yaw: bool = True) -> list[float]:
    if ctx.ik is None:
        raise WorkflowError(
            'Kein IK-Solver verfügbar. Bitte zuerst die Kalibrierung abschließen.'
        )
    seed = ctx.last_arm_joints or HOME_JOINTS_RAD
    solution = ctx.ik.solve(target_xyz=target_xyz, seed=seed, free_yaw=free_yaw)
    if solution is None:
        raise WorkflowError('Position außerhalb des Arbeitsbereichs.')
    return list(solution)


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
    raise WorkflowError('Ziel-Wert konnte nicht ausgewertet werden.')


def home(ctx, args: dict[str, Any]) -> None:
    q_start = ctx.last_full_joints
    q_end = list(HOME_JOINTS_RAD) + [GRIPPER_OPEN_RAD]
    _publish_motion(ctx, q_start, q_end, DEFAULT_HOME_DURATION_S)
    ctx.last_arm_joints = list(HOME_JOINTS_RAD)
    ctx.last_full_joints = q_end


def open_gripper(ctx, args: dict[str, Any]) -> None:
    q_start = ctx.last_full_joints
    q_end = q_start[:5] + [GRIPPER_OPEN_RAD]
    _publish_motion(ctx, q_start, q_end, DEFAULT_GRIPPER_DURATION_S)
    ctx.last_full_joints = q_end


def close_gripper(ctx, args: dict[str, Any]) -> None:
    q_start = ctx.last_full_joints
    q_end = q_start[:5] + [GRIPPER_CLOSED_RAD]
    _publish_motion(ctx, q_start, q_end, DEFAULT_GRIPPER_DURATION_S)
    ctx.last_full_joints = q_end


def move_to(ctx, args: dict[str, Any]) -> None:
    target = _resolve_target(args.get('destination'), ctx)
    arm_q = _solve_or_raise(ctx, target)
    q_end = arm_q + [ctx.last_full_joints[5]]
    _publish_motion(ctx, ctx.last_full_joints, q_end, DEFAULT_MOVE_DURATION_S)
    ctx.last_arm_joints = arm_q
    ctx.last_full_joints = q_end


def pickup(ctx, args: dict[str, Any]) -> None:
    target = _resolve_target(args.get('target'), ctx)
    above = (target[0], target[1], target[2] + DEFAULT_APPROACH_HEIGHT_M)

    above_arm_q = _solve_or_raise(ctx, above)
    grasp_arm_q = _solve_or_raise(ctx, target)
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
    target = _resolve_target(args.get('destination'), ctx)
    above = (target[0], target[1], target[2] + DEFAULT_APPROACH_HEIGHT_M)

    above_arm_q = _solve_or_raise(ctx, above)
    drop_arm_q = _solve_or_raise(ctx, target)

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

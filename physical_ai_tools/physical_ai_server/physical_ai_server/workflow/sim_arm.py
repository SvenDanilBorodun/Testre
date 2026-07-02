#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Virtual arm for the Roboter Studio simulation runtime (Phase 3).

``SimArm`` is a drop-in for the two callables the real arm provides to the
workflow runtime — the trajectory ``publisher`` and the ``get_follower_joints``
readback — so a sim ``WorkflowManager`` (built in ``physical_ai_server`` with
swapped kwargs) runs the SAME interpreter + handlers + IK byte-for-byte against
a virtual joint stream instead of the physical follower.

Two responsibilities:

* ``publish(chunk)`` — accepts a chunk of ``(q, t_from_start_s)`` waypoints
  (the ``ctx.publisher`` contract, ``workflow_manager.WorkflowContext.publisher``)
  and forwards each commanded ``q`` to a virtual joint-state sink (the server's
  ``/sim/joint_states`` publisher). It caches the LAST commanded vector so the
  runtime can chain segments without a ``/joint_states`` subscription — exactly
  what ``physical_ai_server._trajectory_publisher`` caches via
  ``_last_published_joints``. It does NOT sleep: real-time pacing already lives
  in ``trajectory_builder.chunked_publish._pace`` (sleeping here would double-pace).

* ``get_joints()`` — returns the cached 6-vector (arm 5 + gripper), seeded to
  HOME so the runtime's ``_require_seeded_start_pose`` gate passes from the first
  block. It also SIMULATES grasp success for ``motion.check_grasp_held``: after a
  gripper-close command, if a virtual object sits at the current end-effector XY
  the jaws are reported partly-blocked (held); otherwise the commanded full close
  is returned (empty). This is the only place the sim fakes physics — everything
  else is the real runtime.
"""

from __future__ import annotations

import math
import threading
from typing import Any, Callable, Optional


# Sim rest pose: arm 5 joints + gripper. Matches workflow_manager._HOME_FULL_JOINTS
# (handlers.motion.HOME_JOINTS_RAD + GRIPPER_OPEN_RAD). j2/j3 are non-zero so the
# runtime's all-zero "unseeded" sentinel check (_require_seeded_start_pose) passes.
_SIM_HOME_FULL_JOINTS = [0.0, -math.pi / 2, math.pi / 2, 0.0, 0.0, 0.8]

# A commanded gripper angle (index 5) below this is treated as a CLOSE command —
# GRIPPER_OPEN_RAD is +0.8, every grasp close (full -0.5 or per-object e.g. -0.25)
# is negative, so 0.0 cleanly separates the two.
_GRIPPER_CLOSE_THRESHOLD_RAD = 0.0

# Reported jaw angle when a held virtual object blocks the gripper. Must stay
# ABOVE motion.GRASP_HELD_MAX_RAD (default -0.35) so motion.check_grasp_held reads
# it as HELD (a real ~30 mm object leaves the jaws partway open). Kept as a plain
# constant here to avoid importing the ROS-coupled motion module.
_HELD_BLOCKED_GRIPPER_RAD = -0.1

# How close (metres) the virtual end-effector XY must be to a placed object's XY
# for the close to count as "on the object" for the held simulation. Generous so
# IK round-trip rounding + the ~1.6 mm EE y-offset the IK ignores are covered.
_GRASP_CAPTURE_RADIUS_M = 0.06


class SimArm:
    """A virtual OMX-F follower for the sim workflow runtime.

    Construct with a ``joint_state_sink`` (``Callable[[list[float]], None]`` — the
    server's per-frame ``/sim/joint_states`` publish), the real ``IKSolver`` (for
    FK in the held simulation + ``fk_xyz``), and the current placed ``objects``
    (the sim-scene list, each ``{type, tag_id, x, y, yaw}``). ``set_objects``
    refreshes the placed set on each workflow start.
    """

    def __init__(
        self,
        joint_state_sink: Optional[Callable[[list[float]], None]] = None,
        ik: Any | None = None,
        objects: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        self._sink = joint_state_sink
        self._ik = ik
        self._objects: list[dict[str, Any]] = list(objects or [])
        # Seeded to HOME so the very first get_joints() (the start-time seed in
        # WorkflowManager.start) returns a realistic pose, not [0]*6.
        self._last_q: list[float] = list(_SIM_HOME_FULL_JOINTS)
        # publish() runs on the interpreter daemon thread; get_joints()/set_objects
        # may be read from the ROS executor thread — guard the shared cache.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Scene
    # ------------------------------------------------------------------
    def set_objects(self, objects: Optional[list[dict[str, Any]]]) -> None:
        """Replace the placed virtual objects (called on each workflow start)."""
        with self._lock:
            self._objects = list(objects or [])

    # ------------------------------------------------------------------
    # Publisher (ctx.publisher contract)
    # ------------------------------------------------------------------
    def publish(self, chunk: list[tuple[list[float], float]]) -> None:
        """Forward one chunk of ``(q, t)`` waypoints to the virtual joint-state
        sink and cache the last commanded vector. NO sleep — pacing lives in
        ``chunked_publish._pace``."""
        if not chunk:
            return
        last = chunk[-1][0]
        cached = [float(v) for v in last]
        with self._lock:
            self._last_q = cached
        sink = self._sink
        if sink is None:
            return
        for pt in chunk:
            q = pt[0]  # (q, t) or (q, t, v) — replay carries an optional velocity
            try:
                sink([float(v) for v in q])
            except Exception:  # noqa: BLE001 — a publish hiccup must not kill the run
                pass

    # ------------------------------------------------------------------
    # Joint readback (ctx.get_follower_joints contract)
    # ------------------------------------------------------------------
    def get_joints(self) -> list[float]:
        """Return the cached 6-vector, with the gripper (index 5) overridden to a
        blocked angle when the last command was a close AND a virtual object sits
        at the current end-effector XY — so ``motion.check_grasp_held`` reports
        HELD in sim. Otherwise the commanded vector is returned unchanged."""
        with self._lock:
            q = list(self._last_q)
        if self._simulate_held(q):
            q = list(q)
            q[5] = _HELD_BLOCKED_GRIPPER_RAD
        return q

    def fk_xyz(self) -> Optional[tuple[float, float, float]]:
        """End-effector (x, y, z) of the cached pose via the real IK FK, or None
        when no IK is available / FK fails (used by ``destination_current``)."""
        with self._lock:
            q = list(self._last_q)
        return self._fk_xyz(q)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _simulate_held(self, q: list[float]) -> bool:
        if len(q) < 6:
            return False
        # Only a close command can hold an object.
        if q[5] >= _GRIPPER_CLOSE_THRESHOLD_RAD:
            return False
        xyz = self._fk_xyz(q)
        if xyz is None:
            return False
        ex, ey = xyz[0], xyz[1]
        with self._lock:
            objects = list(self._objects)
        for obj in objects:
            try:
                ox = float(obj['x'])
                oy = float(obj['y'])
            except (KeyError, TypeError, ValueError):
                continue
            if math.hypot(ex - ox, ey - oy) <= _GRASP_CAPTURE_RADIUS_M:
                return True
        return False

    def _fk_xyz(self, q: list[float]) -> Optional[tuple[float, float, float]]:
        if self._ik is None or len(q) < 5:
            return None
        try:
            pose = self._ik.fk(q[:5])
        except Exception:  # noqa: BLE001 — FK is best-effort in sim
            return None
        if pose is None:
            return None
        _R, t = pose
        return float(t[0]), float(t[1]), float(t[2])

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
# ABOVE motion.check_grasp_held's per-object threshold (commanded close +
# motion.GRASP_HELD_MARGIN_RAD, 0.15) so a held object reads HELD. The floor
# -0.1 covers the shipped deep closes (cube -0.5 → threshold -0.35); the offset
# covers a future GENTLE close (e.g. -0.25 → threshold -0.10, where a fixed
# -0.1 readback would tie the threshold and read MISS). 0.25 is deliberately
# above the 0.15 margin. Kept as plain constants here to avoid importing the
# ROS-coupled motion module.
_HELD_BLOCKED_GRIPPER_RAD = -0.1
_HELD_BLOCK_OFFSET_RAD = 0.25

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

    Pass ``world=`` a :class:`~workflow.sim_world.SimWorld` to get the MUTABLE
    scene: the arm then actually picks objects up, carries them and puts them
    down, and its held report becomes identity-based. Omit it (``None``) and the
    class behaves exactly as it did before SimWorld existed — a frozen object
    list plus a proximity-based held guess — which is what keeps every
    pre-SimWorld construction (unit tests, the golden fixture) byte-identical.
    """

    def __init__(
        self,
        joint_state_sink: Optional[Callable[[list[float]], None]] = None,
        ik: Any | None = None,
        objects: Optional[list[dict[str, Any]]] = None,
        num_arm_joints: int = 5,
        home_full_joints: Optional[list[float]] = None,
        close_threshold_rad: Optional[float] = None,
        held_block_offset_rad: Optional[float] = None,
        held_floor_rad: Optional[float] = None,
        world: Any | None = None,
    ) -> None:
        self._sink = joint_state_sink
        self._ik = ik
        self._objects: list[dict[str, Any]] = list(objects or [])
        # The MUTABLE virtual scene (workflow.sim_world.SimWorld) when the node
        # supplies one. None keeps the legacy frozen-list behaviour verbatim —
        # every construction that predates SimWorld (unit tests, the golden
        # fixture) therefore behaves byte-identically.
        self._world = world
        # Per-profile grasp-classifier values; None → the OMX module constants
        # (edu6's radian-band gripper 0..1.75 supplies its own: a command below
        # ~1.5 is a close attempt, a held block reads commanded + 0.19).
        self._close_threshold = (float(close_threshold_rad)
                                 if close_threshold_rad is not None
                                 else _GRIPPER_CLOSE_THRESHOLD_RAD)
        self._held_block_offset = (float(held_block_offset_rad)
                                   if held_block_offset_rad is not None
                                   else _HELD_BLOCK_OFFSET_RAD)
        self._held_floor = (float(held_floor_rad)
                            if held_floor_rad is not None
                            else _HELD_BLOCKED_GRIPPER_RAD)
        # Arm-joint count (gripper index == n). 5 = OMX; an ArmProfile-driven
        # sim passes its own (edu6: 6) plus the matching HOME vector.
        self._n = int(num_arm_joints) if int(num_arm_joints) > 0 else 5
        # Seeded to HOME so the very first get_joints() (the start-time seed in
        # WorkflowManager.start) returns a realistic pose, not [0]*6.
        if home_full_joints is not None and len(home_full_joints) == self._n + 1:
            self._home_full_joints: list[float] = [float(v) for v in home_full_joints]
        else:
            self._home_full_joints = list(_SIM_HOME_FULL_JOINTS)
        # Kept so set_objects() can re-seed a NEW run: the node caches ONE SimArm
        # for the whole process lifetime (see physical_ai_server._sim_arm), so
        # without the re-seed run N+1 started wherever run N stopped.
        self._last_q: list[float] = list(self._home_full_joints)
        # publish() runs on the interpreter daemon thread; get_joints()/set_objects
        # may be read from the ROS executor thread — guard the shared cache.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Scene
    # ------------------------------------------------------------------
    def set_objects(self, objects: Optional[list[dict[str, Any]]]) -> None:
        """Replace the placed virtual objects AND re-seed the arm for a NEW run.

        The node caches ONE ``SimArm`` for the whole process lifetime while it
        rebuilds ``SimPerception`` per run, so the two halves disagreed about what
        "a new run" meant: run N+1 started at run N's FINAL pose, and — worse — its
        ``ctx.last_full_joints`` gripper seed was the FAKE held-override readback
        (e.g. −0.0986), a value no motion had ever commanded. The real rig re-seeds
        from live ``/joint_states`` every run; this is the sim's equivalent.
        """
        with self._lock:
            self._objects = list(objects or [])
            self._last_q = list(self._home_full_joints)
        world = self._world
        if world is not None:
            world.reset(objects)

    # ------------------------------------------------------------------
    # Publisher (ctx.publisher contract)
    # ------------------------------------------------------------------
    def publish(self, chunk: list[tuple[list[float], float]]) -> None:
        """Forward one chunk of ``(q, t)`` waypoints to the virtual joint-state
        sink and cache the last commanded vector. NO sleep — pacing lives in
        ``chunked_publish._pace``.

        Also drives the ``SimWorld`` when one is bound: the gripper crossing the
        profile's close threshold CAPTURES the nearest object, a still-closed
        gripper CARRIES it, and crossing back open RELEASES it where it was let
        go. The crossing is judged against the PREVIOUS cached pose, so the first
        close of a run (arriving as an already-closed chunk from a HOME-seeded
        arm) still registers."""
        if not chunk:
            return
        last = chunk[-1][0]
        cached = [float(v) for v in last]
        with self._lock:
            prev = list(self._last_q)
            self._last_q = cached
        self._update_world(prev, cached)
        sink = self._sink
        if sink is None:
            return
        for pt in chunk:
            q = pt[0]  # (q, t) or (q, t, v) — replay carries an optional velocity
            try:
                sink([float(v) for v in q])
            except Exception:  # noqa: BLE001 — a publish hiccup must not kill the run
                pass

    def _update_world(self, prev: list[float], cur: list[float]) -> None:
        """Apply ONE commanded pose to the virtual scene. No-op without a world.

        Three transitions, judged on the gripper channel against the previous
        commanded pose:

        * open → closed: capture the nearest object within the capture radius;
        * closed → closed: carry it to the new end-effector XY;
        * closed → open: release it where it is.

        Wrapped whole: a sim-world hiccup must never kill a running workflow (the
        publisher already treats a sink failure the same way).
        """
        world = self._world
        if world is None or len(cur) < self._n + 1:
            return
        was_closed = (len(prev) > self._n
                      and prev[self._n] < self._close_threshold)
        is_closed = cur[self._n] < self._close_threshold
        try:
            if is_closed:
                xyz = self._fk_xyz(cur)
                if xyz is None:
                    return
                if was_closed:
                    world.carry_to(xyz[0], xyz[1])
                else:
                    world.capture_nearest(xyz[0], xyz[1], _GRASP_CAPTURE_RADIUS_M)
            elif was_closed:
                world.release()
        except Exception:  # noqa: BLE001 — the sim world must never kill a run
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
            # Blocked angle scales with the commanded close so gentle per-object
            # closes still clear check_grasp_held's derived threshold (see the
            # constants above); the floor keeps deep closes at the pinned -0.1.
            q[self._n] = max(self._held_floor,
                             q[self._n] + self._held_block_offset)
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
        if len(q) < self._n + 1:
            return False
        # Only a close command can hold an object.
        if q[self._n] >= self._close_threshold:
            return False
        world = self._world
        if world is not None:
            # IDENTITY, not proximity. The legacy test below asks "is the jaw
            # currently NEAR some placed object", which was wrong in both
            # directions once the object could move: it read MISS for the whole
            # carry (the frozen object stayed behind at its placement) and HELD
            # right after a release (the arm is still standing over the object it
            # just let go). A grasp is a relationship, not a distance.
            return bool(world.is_held())
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
        if self._ik is None or len(q) < self._n:
            return None
        try:
            pose = self._ik.fk(q[:self._n])
        except Exception:  # noqa: BLE001 — FK is best-effort in sim
            return None
        if pose is None:
            return None
        _R, t = pose
        return float(t[0]), float(t[1]), float(t[2])

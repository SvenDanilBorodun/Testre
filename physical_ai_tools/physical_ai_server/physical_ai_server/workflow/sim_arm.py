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
  ``_last_published_joints``. It NEVER sleeps: ``chunked_publish._pace`` already
  paces the CHUNKS, and sleeping here would double-pace them.

* ``get_joints()`` — returns the cached 6-vector (arm 5 + gripper), seeded to
  HOME so the runtime's ``_require_seeded_start_pose`` gate passes from the first
  block. It also SIMULATES grasp success for ``motion.check_grasp_held``: after a
  gripper-close command, if a virtual object sits at the current end-effector XY
  the jaws are reported partly-blocked (held); otherwise the commanded full close
  is returned (empty). This is the only place the sim fakes physics — everything
  else is the real runtime.

REAL-TIME PLAYBACK (``frame_sink=``). ``chunked_publish`` hands over ONE SECOND of
waypoints at a time and then sleeps that second. On the rig that is correct: the
chunk goes to the follower's JointTrajectoryController, which moves through its
points over their ``time_from_start``. Without a controller the sim forwarded all
30 points in a tight loop — measured, a 3 s move left as three bursts of 30 poses
0.02 ms wide, one second apart, and the browser (newest-only, 10 Hz) saw 6 of the
90: the twin jumped up to 48° at once, showed the arm up to 45° AHEAD of the
program, then froze ~0.9 s. With a ``frame_sink`` the arm gets that controller:
``_TrajectoryPlayer`` plays each chunk's points at their own times on a daemon
thread, and ``publish()`` returns at once, as the rig's publish does. Everything
the RUNTIME reads (``get_joints``, ``fk_xyz``, the world the perception sees) is
still updated synchronously inside ``publish()``, so a sim run is exactly as
deterministic as before; only the PICTURE is played in real time. Omit
``frame_sink`` and the synchronous sink loop runs exactly as it always did — that
is what every unit test and the golden fixture construct.
"""

from __future__ import annotations

import math
import threading
import time
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


# Longest single wait inside the player. Only a fence: `Condition.wait` raises
# OverflowError past threading.TIMEOUT_MAX, which would end the playback thread
# for the life of the node, and one absurd time_from_start must not cost every
# later chunk. Also bounds how long a preempt/cancel can wait on a sleeping
# player (they notify it, so this is the belt).
_PLAY_WAIT_SLICE_S = 1.0


def _frame_time(pt: Any) -> float:
    """``time_from_start`` of one chunk point, sanitised. A missing, non-finite or
    negative value plays at the chunk's start instead of never (or, for ``inf``,
    parking the player until the next chunk preempts it)."""
    try:
        t = float(pt[1])
    except (IndexError, TypeError, ValueError):
        return 0.0
    return t if math.isfinite(t) and t > 0.0 else 0.0


class _TrajectoryPlayer:
    """The simulator's stand-in for the follower's JointTrajectoryController.

    ``play(frames)`` installs one chunk — a list of ``(t_from_start_s, q, scene)``
    — and returns at once; a daemon thread emits each frame at ``receipt +
    t_from_start`` through ``sink(q, scene, late_s)``. The semantics are the
    controller's:

    * a NEWER chunk replaces the one in flight. Frames of the old chunk whose time
      is already up are emitted first (the controller has passed through them —
      at a chunk boundary that is the old chunk's last point, due the instant
      ``chunked_publish._pace`` hands over the next one); frames still in the
      future are dropped, and the newest scene among them is carried onto the
      new chunk's first frame so a world change can never be lost with them;
    * ``cancel()`` drops the chunk in flight WITHOUT flushing. It is atomic with
      respect to emission: every emit runs under the same condition lock, and the
      liveness of a chunk is re-checked under that lock right before each emit, so
      once ``cancel()`` returns no frame of a cancelled chunk can still appear.
      That is what lets a reset publish HOME without a trailing waypoint of the
      run it ended dragging the twin straight back off it.

    ``late_s`` is how far behind its schedule a frame was emitted (≥ 0), so the
    sink can stamp the pose with the time it was VALID rather than the time the
    thread happened to wake up.

    Lock order: the sink is called with the condition held, so the sink may take
    its own locks but must never call back into ``play``/``cancel`` — and no
    caller of ``play``/``cancel`` may hold a lock the sink takes.
    """

    def __init__(self, sink: Callable[..., None],
                 name: str = 'sim-arm-player') -> None:
        self._sink = sink
        self._name = name
        self._cv = threading.Condition()
        self._gen = 0              # bumped by every play()
        self._cancel_floor = 0     # chunks with gen <= this are dead
        self._job: Optional[tuple] = None
        self._busy = False
        self._closed = False
        self._thread: Optional[threading.Thread] = None

    def play(self, frames: list[tuple[float, list[float], Any]]) -> None:
        with self._cv:
            if self._closed:
                return
            self._gen += 1
            self._job = (self._gen, time.monotonic(), list(frames))
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name=self._name, daemon=True)
                self._thread.start()
            self._cv.notify_all()

    def cancel(self) -> None:
        with self._cv:
            self._cancel_floor = self._gen
            self._job = None
            self._cv.notify_all()

    @property
    def busy(self) -> bool:
        """True while a chunk is installed or being played."""
        with self._cv:
            return self._busy or self._job is not None

    def close(self, timeout: float = 1.0) -> None:
        """Stop the thread (tests; the node's arm lives for the process)."""
        with self._cv:
            self._closed = True
            self._job = None
            self._cancel_floor = self._gen
            self._cv.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    # -- worker ---------------------------------------------------------------
    def _run(self) -> None:
        carried_scene = None
        # The cancel floor in force when a scene was carried. A cancel since then
        # means it belonged to a run that has been reset, and the next run must
        # not inherit it on its first frame.
        carried_floor = 0
        with self._cv:
            while not self._closed:
                if self._job is None:
                    self._busy = False
                    self._cv.wait()
                    continue
                gen, t0, frames = self._job
                self._job = None
                self._busy = True
                if carried_scene is not None and carried_floor != self._cancel_floor:
                    carried_scene = None
                carried_scene = self._play_locked(gen, t0, frames, carried_scene)
                carried_floor = self._cancel_floor
            self._busy = False

    def _alive(self, gen: int) -> bool:
        return not self._closed and gen > self._cancel_floor

    def _play_locked(self, gen: int, t0: float, frames: list,
                     carried_scene: Any) -> Any:
        """Play one chunk. Returns the scene to carry onto the NEXT chunk (only
        ever non-None after a preemption dropped a frame that carried one)."""
        n = len(frames)
        i = 0
        while i < n:
            due = t0 + frames[i][0]
            while self._alive(gen) and gen == self._gen:
                remaining = due - time.monotonic()
                if remaining <= 0.0:
                    break
                # Sliced: `Condition.wait` raises OverflowError past
                # threading.TIMEOUT_MAX and would kill this thread for the life
                # of the node (every later chunk silently dropped). No caller can
                # produce a time_from_start that large today — the cap is a
                # fence, not a workaround.
                self._cv.wait(min(remaining, _PLAY_WAIT_SLICE_S))
            if not self._alive(gen):
                return None                    # cancelled: nothing more of it
            if gen != self._gen:
                # Preempted by a newer chunk: emit what is already due, drop the
                # rest, and keep the newest scene the dropped frames carried.
                now = time.monotonic()
                while i < n and t0 + frames[i][0] <= now:
                    self._emit_locked(frames[i], t0 + frames[i][0], carried_scene)
                    carried_scene = None
                    i += 1
                for _t, _q, scene in frames[i:]:
                    if scene is not None:
                        carried_scene = scene
                return carried_scene
            self._emit_locked(frames[i], due, carried_scene)
            carried_scene = None
            i += 1
        return None

    def _emit_locked(self, frame: tuple, due: float, carried_scene: Any) -> None:
        """Emit one frame. A scene carried over from a preempted chunk rides on
        the first frame that has none of its own (its own is always newer)."""
        _t, q, scene = frame
        if scene is None:
            scene = carried_scene
        late = max(0.0, time.monotonic() - due)
        try:
            self._sink(list(q), scene, late)
        except Exception:  # noqa: BLE001 — a publish hiccup must not kill playback
            pass


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

    Pass ``frame_sink=`` (``Callable[[q, scene, late_s], None]``) to play every
    chunk in REAL TIME instead of forwarding it in one burst — see the module
    docstring. ``scene`` is the world snapshot AS OF that waypoint when the
    waypoint changed the world, else ``None``; ``late_s`` is how far behind
    schedule the frame went out. With a ``frame_sink`` the ``joint_state_sink``
    is not used.
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
        frame_sink: Optional[Callable[..., None]] = None,
    ) -> None:
        self._sink = joint_state_sink
        # The virtual trajectory controller. None → the legacy synchronous burst
        # (unit tests, the golden fixture); the node always passes a frame_sink.
        self._player: Optional[_TrajectoryPlayer] = (
            _TrajectoryPlayer(frame_sink) if frame_sink is not None else None)
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

        Playback is cancelled FIRST. This is also the reset path
        (``physical_ai_server._reset_sim_scene`` publishes HOME right after it),
        and a waypoint of the run being reset that was still queued would
        otherwise land on top of that HOME.
        """
        player = self._player
        if player is not None:
            player.cancel()
        with self._lock:
            self._objects = list(objects or [])
            self._last_q = list(self._home_full_joints)
        world = self._world
        if world is not None:
            world.reset(objects)

    def close(self) -> None:
        """Stop the playback thread (tests). Idempotent; a no-op without one."""
        player = self._player
        if player is not None:
            player.close()

    @property
    def playing(self) -> bool:
        """True while a chunk is being played in real time."""
        player = self._player
        return player.busy if player is not None else False

    # ------------------------------------------------------------------
    # Publisher (ctx.publisher contract)
    # ------------------------------------------------------------------
    def publish(self, chunk: list[tuple[list[float], float]]) -> None:
        """Take one chunk of ``(q, t)`` waypoints — the ``ctx.publisher``
        contract — and cache the last commanded vector. NEVER sleeps: pacing of
        the chunks lives in ``chunked_publish._pace``.

        Everything the RUNTIME can read is settled before this returns: the
        cached pose is the chunk's end, and the ``SimWorld`` has seen EVERY
        waypoint in order — the gripper crossing the profile's close threshold
        CAPTURES the nearest object, a still-closed gripper CARRIES it, crossing
        back open RELEASES it. Per waypoint, not per chunk: judged once at the
        chunk's END, a replay that closed or opened the jaws while the arm was
        moving captured / dropped at an XY up to a second of motion away from
        where the jaws actually crossed (outside the capture radius the grasp
        simply missed). The first crossing of a run is judged against the
        previous cached pose, so a close arriving from a HOME-seeded arm still
        registers.

        What happens to the poses then depends on the construction: with a
        ``frame_sink`` they are handed to the real-time player (each carrying
        the world snapshot as of that waypoint when it changed it) and this
        returns at once; without one they are forwarded to ``joint_state_sink``
        in a burst, exactly as before real-time playback existed."""
        if not chunk:
            return
        # (q, t) or (q, t, v) — replay carries an optional velocity.
        qs = [[float(v) for v in pt[0]] for pt in chunk]
        with self._lock:
            prev = list(self._last_q)
            self._last_q = list(qs[-1])
        player = self._player
        frames: list[tuple[float, list[float], Any]] = []
        for pt, q in zip(chunk, qs):
            changed = self._update_world(prev, q)
            if player is not None:
                frames.append((_frame_time(pt), q,
                               self._scene_snapshot() if changed else None))
            prev = q
        if player is not None:
            player.play(frames)
            return
        sink = self._sink
        if sink is None:
            return
        for q in qs:
            try:
                sink(list(q))
            except Exception:  # noqa: BLE001 — a publish hiccup must not kill the run
                pass

    def _scene_snapshot(self) -> Optional[dict[str, Any]]:
        world = self._world
        if world is None:
            return None
        try:
            return world.snapshot()
        except Exception:  # noqa: BLE001 — a diagnostic must never kill a run
            return None

    def _update_world(self, prev: list[float], cur: list[float]) -> bool:
        """Apply ONE commanded pose to the virtual scene. No-op without a world.
        Returns True when the scene changed (something captured, carried or
        released), which is when a real-time frame carries a snapshot.

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
            return False
        was_closed = (len(prev) > self._n
                      and prev[self._n] < self._close_threshold)
        is_closed = cur[self._n] < self._close_threshold
        try:
            if is_closed:
                if was_closed and not world.is_held():
                    return False       # closed on nothing: carry_to is a no-op
                if was_closed and list(prev[:self._n]) == list(cur[:self._n]):
                    # The JAWS moved, the arm did not (a gripper-only segment:
                    # tightening a grasp, holding through a wait). `carry_to`
                    # snaps the object onto the gripper's own XY, so calling it
                    # here would jerk a just-captured cube up to ~3 cm sideways
                    # the moment the jaws close — a move the old per-CHUNK
                    # judgement never made either. Carry only what a moving arm
                    # carries.
                    return False
                xyz = self._fk_xyz(cur)
                if xyz is None:
                    return False
                if was_closed:
                    world.carry_to(xyz[0], xyz[1])
                    return True
                before = world.held_key()
                world.capture_nearest(xyz[0], xyz[1], _GRASP_CAPTURE_RADIUS_M)
                return world.held_key() != before
            if was_closed:
                return world.release() is not None
        except Exception:  # noqa: BLE001 — the sim world must never kill a run
            pass
        return False

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

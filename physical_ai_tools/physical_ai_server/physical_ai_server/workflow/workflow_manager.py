#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Daemon-thread runtime for Roboter Studio workflows.

The manager owns:
- the main interpreter thread,
- N hat-block handler threads (one per ``edubotics_when_*`` block at
  the workspace top level),
- a ``motion_lock`` shared via the WorkflowContext to serialize motion
  blocks between handlers (so two simultaneous broadcast handlers can
  not race the arm),
- the broadcast event registry,
- the pause/step/breakpoint plumbing.

Safe-stop on error/stop: the in-flight trajectory is cancelled
(``chunked_publish`` observes ``should_stop`` between chunks) so the arm
HOLDS at the last commanded waypoint — a safe in-place stop with no
surprise auto-motion (auto-homing on an error could drive the arm into
whatever went wrong). The student sends the arm Home explicitly with a
"Heimposition" block when ready. The main thread's ``finally`` reaps the
hat threads and fires ``on_finished``; hat threads exit on ``should_stop``.
"""

from __future__ import annotations

import json
import math
import threading
import time
import traceback
import types
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from physical_ai_server.workflow.handlers.motion import (
    WorkflowError,
    _hold_motion_lock,
    _point_in_zone,
    _release_motion_lock,
    _TEMPO_MAX,
    _TEMPO_MIN,
)
from physical_ai_server.workflow.interpreter import (
    Interpreter,
    InterpreterError,
    event_name_of,
)


# Reference pose used to seed the IK pre-check at workflow start. Matches
# handlers.motion.HOME_JOINTS_RAD + GRIPPER_OPEN_RAD; duplicated here to
# avoid a circular import.
_HOME_FULL_JOINTS = [0.0, -math.pi / 2, math.pi / 2, 0.0, 0.0, 0.8]

# IK pre-check budget. Each concrete destination gets one solver
# attempt; an overall cap so a 100-block workflow doesn't stall start.
_IKPRECHECK_PER_TARGET_TIMEOUT_S = 0.05
_IKPRECHECK_TOTAL_BUDGET_S = 1.0


@dataclass
class WorkflowContext:
    """Runtime state shared between the manager and individual handlers."""

    publisher: Callable[[list[tuple[list[float], float]]], None]
    ik: Any | None = None
    perception: Any | None = None
    destinations: dict[str, dict[str, float]] = field(default_factory=dict)
    # Batch 2b — recorded hand-guided trajectories, name → CONTRACT-B
    # ``{"fps": int, "points": [[j1..j5, grip, t_s], ...]}``. Threaded from the
    # top-level ``trajectories`` sibling of the workflow_json (CONTRACT C) merged
    # with any server-persisted recordings, exactly like ``destinations``. Read by
    # handlers/trajectory.replay_trajectory. Empty {} → the „Aufnahme abspielen"
    # block fails loud with a German "Unbekannte Aufnahme" (backward-safe: a
    # workflow with no replay block never touches it).
    trajectories: dict[str, Any] = field(default_factory=dict)
    # DUAL Z-KEY (don't conflate):
    #   z_table        — MEASURED end-effector grasp height (touch-off). Used by
    #                    motion.pickup as the descend target. NOT a projection plane.
    #   board_table_z  — table SURFACE height (extrinsic solve). The plane the
    #                    camera ray intersects to recover an object's (x, y); the
    #                    object sits on the surface, not at the gripper grasp height.
    z_table: float | None = None
    board_table_z: float | None = None
    scene_intrinsics: dict | None = None
    scene_extrinsics: Any | None = None
    # Touch-off MEASURED EE-frame table tilt (a, b, c) for z = a·x + b·y + c. Used
    # by motion's descend; NOT used for object projection (it is offset above the
    # surface by the finger length, so projecting onto it reintroduces lateral
    # error on a tilted camera — perception projects onto board_table_z instead).
    table_plane: tuple[float, float, float] | None = None
    last_arm_joints: list[float] | None = None
    last_full_joints: list[float] = field(default_factory=lambda: [0.0] * 6)
    # ── ArmProfile geometry (§16.4 slice 2c) ─────────────────────────────────
    # Consumed by the handlers' ctx accessors (motion._n & friends). The
    # defaults (5 / None) make every profile-less construction — all existing
    # tests, every non-Roboter-Studio path — resolve to the OMX module
    # constants, bit-identical.
    num_arm_joints: int = 5
    roll_joint_index: int | None = None
    home_joints_rad: tuple | None = None
    observe_pose_joints: tuple | None = None
    gripper_open_rad: float | None = None
    gripper_closed_rad: float | None = None
    velocity_limit_rad_s: float | None = None
    grasp_held_margin_rad: float | None = None
    # No-go-zone reroute-ladder geometry (None → path_guard's OMX module
    # constants). Arm-sized heights/radii in metres — see ArmProfile for why the
    # OMX values are unreachable on a shorter arm.
    safe_travel_z_m: float | None = None
    tool_clear_m: float | None = None
    swing_heights_m: tuple | None = None
    swing_radii_m: tuple | None = None
    # Grasp-held threshold source: the most recent COMMANDED gripper close (rad)
    # this run — written ONLY by motion's close paths (_execute_pickup,
    # close_gripper, close_on_object), read by motion._held_threshold_rad.
    # Deliberately SEPARATE from last_full_joints: start() boot-seeds THAT from
    # the MEASURED follower pose, so a still-held gripper (~−0.1 measured) would
    # masquerade as a commanded close and skew the per-object threshold. None
    # (fresh each run — re-set explicitly in start()) → the global
    # GRASP_HELD_MAX_RAD fallback.
    last_commanded_close_rad: float | None = None
    should_stop: Callable[[], bool] = field(default_factory=lambda: (lambda: False))
    log: Callable[[str], None] = field(default_factory=lambda: (lambda _: None))
    # Push the most recent perception detection list to the WorkflowStatus
    # publisher so the React editor can render bbox overlays. Each entry
    # is a perception.Detection — the server's _emit_workflow_status
    # adapter packs them into the typed Detection[] on the message.
    emit_detections: Callable[[list], None] = field(default_factory=lambda: (lambda _: None))
    variables: dict[str, Any] = field(default_factory=dict)
    # Named per-run integer counters (name → int) for the Zähler blocks
    # (reset/add/get) + the edubotics_when_counter_gt hat. Fresh per run (seeded
    # in start()); reads/writes serialize under var_lock (see handlers/counters).
    counters: dict[str, int] = field(default_factory=dict)
    get_scene_frame: Callable[[], Any] | None = None
    get_gripper_frame: Callable[[], Any] | None = None
    # Age (seconds) of the latest scene frame, for the perception staleness
    # gate (reject a frozen camera). None → unknown (gate skipped).
    get_scene_frame_age: Callable[[], float | None] | None = None
    get_current_pose_xyz: Callable[[], tuple[float, float, float] | None] | None = None
    # Latest 6-joint follower readback (index 5 = gripper), for the grasp-success
    # check (#2): after the gripper closes, the achieved gripper angle tells HELD
    # (object blocks the jaws, stays partly open) from EMPTY (jaws close fully).
    #
    # None when the joint source is unavailable. That does NOT degrade
    # gracefully, and the docstring used to claim it did ("falls back to
    # claim-on-completion (no regression)"): with no joint source there is also
    # no pose to seed ``last_full_joints``, so motion's
    # _require_seeded_start_pose refuses EVERY motion block on all three
    # profiles. Deliberate — commanding the first waypoint from an assumed pose
    # is the lurch that guard exists to prevent — but it is a refusal, not a
    # fallback. Only the grasp-success CHECK degrades; the arm does not move at
    # all. See _start_joint_seed_watchdog for the late-readback case.
    get_follower_joints: Callable[[], list[float] | None] | None = None
    # Phase-2 additions
    motion_lock: threading.RLock | None = None  # reentrant: hat body + inner publish
    # Re-entrant variable lock so the variables_get/set blocks from main
    # and hat handlers don't race on the plain dict. Audit §A1 —
    # without this, mutation from a `for` loop in main while a hat
    # reads via `variables_get` tears the dict.
    var_lock: threading.RLock | None = None
    # Audit fix #4 — breakpoints used to be a plain set captured by
    # reference; updates from set_breakpoints() were therefore not
    # visible to the interpreter thread which iterates the set on every
    # block dispatch. The new shape is an immutable frozenset bound at
    # start time PLUS an optional get_breakpoints() callable the
    # interpreter prefers (returns the manager's freshest snapshot).
    breakpoints: frozenset[str] = frozenset()
    get_breakpoints: Callable[[], frozenset[str]] | None = None
    fire_broadcast: Callable[[str], None] = field(default_factory=lambda: (lambda _: None))
    wait_if_paused: Callable[[], None] = field(default_factory=lambda: (lambda: None))
    wait_for_resume: Callable[[], None] = field(default_factory=lambda: (lambda: None))
    set_paused: Callable[[bool], None] = field(default_factory=lambda: (lambda _: None))
    # A NON-CONSUMING „is the run paused right now?" probe, and non-consuming is
    # the whole reason it exists. ``wait_for_resume`` calls
    # ``_consume_step_token()``, which atomically clears the single „Schritt"
    # token AND ``_resume_event``; ``wait_if_paused`` blocks. So neither of the
    # two predicates that already existed can be used to ASK the question, and
    # ``motion._hold_motion_lock`` has to ask it: a breakpoint inside a
    # „wenn …"-Block pauses while ``_run_hat_handler`` holds ``motion_lock``,
    # and the queued main stack must not then blame the student for a busy arm.
    is_paused: Callable[[], bool] = field(default_factory=lambda: (lambda: False))
    procedures: dict[str, dict[str, Any]] = field(default_factory=dict)
    call_procedure: Callable[[str, list[Any]], Any] = field(
        default_factory=lambda: (lambda _name, _args: None)
    )
    # Roboter Studio named-object grasping: the loaded ObjectCatalog (tag_id →
    # type → grasp recipe), or None with object_catalog_error holding the German
    # load error. Loaded TOLERANTLY at start() — a workflow with no named-object
    # blocks runs fine without a catalog; a named block fails loud at the block.
    object_catalog: Any | None = None
    object_catalog_error: str | None = None
    # W5 — ground-truth accuracy correction (from the "Genauigkeit prüfen" step).
    # xy_correction is a 2x3 affine (numpy) mapping detected base XY -> corrected
    # base XY (None → no correction / identity); yaw_bias_rad is added to the
    # measured tag yaw before commanding the wrist roll (0.0 → no change). Both
    # are consumed via getattr by perception_blocks (_apply_xy_correction /
    # _apply_yaw_bias), so they are safe to read before/after this attribute
    # exists. Threaded from the calib dict in start(), mirroring z_table.
    xy_correction: Any | None = None
    yaw_bias_rad: float = 0.0
    # Per-run claimed/skipped tag-id sets for the „Solange <Typ> sichtbar" loop:
    # a tag id is CLAIMED after a successful grasp (so a placed object re-entering
    # view is never re-grabbed and the loop terminates) and SKIPPED after a
    # confirmed failed grab (reserved for the future grasp-success heuristic).
    # Guarded by claim_lock — a separate RLock from motion_lock, because the set
    # is a distinct data structure a hat thread can mutate concurrently (§24.3;
    # mirrors var_lock guarding the variables dict).
    claimed_tags: set = field(default_factory=set)
    skipped_tags: set = field(default_factory=set)
    claim_lock: threading.RLock | None = None
    # Per-tag POSITION tracking for the recycled-object reclaim (#1), all
    # guarded by claim_lock (same as claimed_tags/skipped_tags):
    #   claim_release_xy — tag id → the COMMANDED base-frame (x, y) the robot
    #                      released it at (``drop_at``'s own target), or ``None``
    #                      when it was released somewhere the robot did not aim
    #                      for (a bare „öffne Greifer"), which fails the reclaim
    #                      CLOSED for that tag.
    #   claim_pick_xy    — tag id → the (x, y) it was last seen at while still
    #                      UNCLAIMED, i.e. the spot it was picked FROM. The
    #                      reference for a SKIPPED object, which the robot never
    #                      carried anywhere.
    #   carried_tag      — the tag currently IN THE GRIPPER. Never reclaimed: a
    #                      held object's projected position travels with the arm.
    # A later sighting ≥ EDUBOTICS_RELEASE_MOVE_M from the release point means a
    # PERSON moved it, so it is un-claimed and grabbed again.
    #
    # The reference is the COMMANDED release point, never an observation: the
    # sighting-derived anchor this replaced could only arm when the drop
    # destination lay inside the scene camera's view, and on a rig that places
    # objects off-camera it never armed at all. Deriving it from the command also
    # makes a missed AprilTag look irrelevant BY CONSTRUCTION — there is no
    # observation for a miss to corrupt. See
    # handlers/perception_blocks.py::_reclaim_recycled.
    claim_release_xy: dict = field(default_factory=dict)
    carried_tag: int | None = None

    # Token-bucket state for the output rate limiter, per KIND
    # (handlers/output.py::_rate_ok). DECLARED rather than set as an ad-hoc
    # attribute: this is a plain @dataclass today, so `ctx._output_rate_state = …`
    # works — but adding `slots=True` later would make every write raise
    # AttributeError, which _rate_ok's blanket `except: return True` would
    # SWALLOW, leaving the limiter silently inert. That is this round's own
    # "a guard that could not fire" class, one keyword away.
    #
    # Deliberately NOT lock-guarded, unlike `variables`/`var_lock` above. Up to
    # MAX_HAT_HANDLERS hat threads plus the main stack can emit concurrently, so
    # the read-modify-write of the token count can lose an update — the worst
    # case is slight OVER-emission, never a crash (individual dict ops are
    # atomic). A lock on every „melde" would cost more than the miscount.
    _output_rate_state: dict = field(default_factory=dict)
    # Which operator gripper knobs have already been reported as out-of-band
    # THIS RUN (motion._warn_gripper_knob_out_of_band). Declared rather than
    # setattr'd on the fly: `ArmProfile.grasp_held_max_rad` was resolved from a
    # ctx field that did not exist, so the branch never executed — a dead
    # getattr is the failure mode this file has already paid for once.
    gripper_knob_warned: set = field(default_factory=set)
    claim_pick_xy: dict = field(default_factory=dict)
    # Object types already told „alles erledigt" THIS RUN, so the unclaimed view's
    # blind-state notice (perception_blocks._detect_named) says it once per type
    # rather than on every pass of a „Solange sichtbar" loop. Declared for the
    # same reason gripper_knob_warned is — an undeclared ctx field is a branch
    # that silently never runs.
    all_done_notified: set = field(default_factory=set)
    # Phase-4 no-go zones ("Sperrzonen"): a list of axis-aligned base-frame
    # keep-out boxes ``{min:[x,y,z], max:[x,y,z]}`` (metres), parsed in start()
    # from the top-level ``zones`` sibling of the workflow_json (injected for
    # BOTH sim + real runs). None/empty → no zones → motion.safe_move behaves
    # exactly like raw _publish_motion (backward-safe). Read defensively via
    # getattr by motion.safe_move / motion._point_in_zone / path_guard.
    zones: list | None = None
    # Phase-2 Tempo: a global speed multiplier on every motion DURATION, parsed
    # in start() from the top-level ``tempo`` sibling of the workflow_json
    # (injected for BOTH sim + real runs; ignored by Interpreter.from_json, which
    # reads only data['blocks']). Read defensively via getattr(ctx, 'tempo', 1.0)
    # by motion._publish_motion. SET ONCE in start() and NEVER mutated — so it is
    # thread-safe across the main stack + hat handlers with no lock. Rule §2: a
    # teaching speed knob on the workflow runtime, NOT an inference safety
    # envelope (it never reshapes a recorded/replayed action).
    tempo: float = 1.0


MAX_WORKFLOW_JSON_BYTES = 256 * 1024  # 256 KiB; see plan §2.5

# Audit fix #16: cap the number of hat-block handlers a single workflow
# can spawn. Each hat handler is its own daemon thread + broadcast
# Condition; a malicious or buggy workspace with hundreds of when_*
# blocks would otherwise saturate the server. 16 is generous for the
# classroom — the largest pre-existing tutorial uses 3.
MAX_HAT_HANDLERS = 16

# German names for the hat blocks, for every student-facing message. The raw
# block-type id („edubotics_when_object_seen") used to be interpolated straight
# into the Protokoll — an English identifier on a German surface (Rule §1) — and
# the two error paths spelled the same condition two different ways.
_HAT_LABELS_DE: dict[str, str] = {
    'edubotics_when_broadcast': 'Wenn Ereignis empfangen',
    'edubotics_when_object_seen': 'Wenn Objekt erkannt',
    'edubotics_when_counter_gt': 'Wenn Zähler größer als',
}


def _hat_label_de(btype: Any) -> str:
    return _HAT_LABELS_DE.get(btype, 'Ereignis-Block')


# Minimum wall-clock time one hat-handler cycle may take, i.e. a ~20 Hz ceiling
# on how often a single hat can run its body. The exact counterpart of the
# interpreter's FOREVER_MIN_CYCLE_S, and for the same reason: a hat had NO rate
# floor of any kind (MAX_LOOP_ITERATIONS never applies to hats), so a handler
# that re-broadcasts its own event ran 570 211 bodies and published 2 851 066
# status messages in 2 s (549 236 / 2 746 185 on an independent re-run) — 71 % of
# a core, inside the ROS node, starving the 1 Hz heartbeat until React reported
# „Getrennt". Two hats ping-ponging measured 2 248 165 publishes / 86 %.
# MEASURED ON THE INTERMEDIATE TREE — after the keep-alive fix, before this rate
# floor — because on `main` a hats-only run dies immediately and the program is
# inert (0 bodies). Both this floor and MAX_BROADCAST_BACKLOG are necessary,
# proven by removing each. A body doing real work exceeds this floor and pays
# nothing. Plain constant, NOT an EDUBOTICS_* env knob (that would need a
# docker-compose forward per ci.yml's env-forwarding-guard); tests monkeypatch it.
HAT_MIN_CYCLE_S = 0.05

# How long the run stays alive for its hat handlers after the MAIN stack has
# finished. Scratch's model, and the one the blocks imply: „Wenn Würfel erkannt:
# Greife" is a complete program, and it used to finish green in 0.157 s having
# done nothing at all (10/10 for all three hat types), because _run's `finally`
# set the stop event the instant the main stack returned. Students worked around
# it with an empty „wiederhole fortlaufend" keep-alive.
#
# Bounded rather than infinite: a classroom needs a forgotten program to end on
# its own. The student's Stop button is the normal exit and is unaffected. 300 s
# matches WAIT_UNTIL_MAX_SECONDS, the other "a student can wedge the session"
# cap, and like it this is deliberately a plain module constant.
HAT_KEEPALIVE_MAX_S = 300.0

# Consecutive failures a single hat handler may hit before it is retired for the
# rest of the run.
#
# Its body is re-run after an error. On `main` ALL FOUR `except` arms `return`ed,
# so ONE failing body ended the handler THREAD for the rest of the run while the
# run still reported green — STRUCTURALLY, not occasionally: `main`'s own comment
# notes that the LAST object of a „Wenn … erkannt: Greife" handler legitimately
# ends in a GraspSkip, so the ordinary TERMINAL case reached one of those
# `return`s. Measured over four broadcasts with a deterministically-failing last
# block: `main` fires the body ONCE, this tree fires it four times (5/5).
#
# (The arm is left holding on EVERY tree — nothing re-opens the gripper after a
# body dies — so that is not the discriminator, and quoting it as the headline
# measurement is what made this claim look unreproducible. 1 firing vs 4 is.)
#
# A one-off failure must not be terminal; a body that fails EVERY time would
# otherwise spin against the rate floor forever.
MAX_HAT_CONSECUTIVE_ERRORS = 5

# Upper bound on the broadcast backlog one handler may work through. Broadcasts
# are consumed ONE at a time so a burst is queued rather than coalesced (Scratch
# semantics); this stops a producer that outruns its consumer — the
# self-rebroadcasting hat above — from building an unbounded queue.
MAX_BROADCAST_BACKLOG = 32

# Background follower-pose seed retry (see _start_joint_seed_watchdog). Long
# enough to cover a container recreate / node respawn, short enough that the
# thread is gone well inside a lesson.
_JOINT_SEED_WATCHDOG_S = 30.0
_JOINT_SEED_POLL_S = 0.1

# How long a tag must be CONTINUOUSLY unseen before the object hat believes it
# is really gone (see WorkflowManager._debounce_absence). Imported, not
# redefined: perception_blocks._RECLAIM_ABSENT_S (env EDUBOTICS_RECLAIM_ABSENT_S,
# default 1.5 s) is this codebase's already-tuned answer to exactly this
# question, and two constants for one concept is how they drift apart. NOTE: the
# hat is now that constant's ONLY consumer — the recycled-object reclaim moved to
# a POSITION rule (EDUBOTICS_RECLAIM_MOVE_M), because IT samples once per 8–12 s
# loop pass where this hat polls several times a second, so the same seconds mean
# something usable here and nothing usable there.
# Imported lazily-at-module-load with a literal fallback so an import-order
# change can never leave the hat un-debounced (0 would restore the raw-set
# flicker bug wholesale).
try:
    from physical_ai_server.workflow.handlers.perception_blocks import (
        _RECLAIM_ABSENT_S as _HAT_ABSENT_GRACE_S,
    )
except Exception:  # noqa: BLE001 — never let an import shape safety behaviour
    _HAT_ABSENT_GRACE_S = 1.5


class WorkflowManager:
    """Public API used by physical_ai_server.py service callbacks."""

    def __init__(
        self,
        publisher: Callable[[list[tuple[list[float], float]]], None],
        ik_factory: Callable[[], Any] | None = None,
        perception_factory: Callable[[], Any] | None = None,
        load_destinations: Callable[[], dict[str, dict[str, float]]] | None = None,
        load_calibration: Callable[[], dict[str, Any]] | None = None,
        emit_status: Callable[[dict[str, Any]], None] | None = None,
        on_finished: Callable[[str], None] | None = None,
        get_scene_frame: Callable[[], Any] | None = None,
        get_gripper_frame: Callable[[], Any] | None = None,
        get_scene_frame_age: Callable[[], float | None] | None = None,
        get_current_pose_xyz: Callable[[], tuple[float, float, float] | None] | None = None,
        get_follower_joints: Callable[[], list[float] | None] | None = None,
        load_object_catalog: Callable[[], Any] | None = None,
        arm_profile: Any | None = None,
    ) -> None:
        self._publisher = publisher
        self._ik_factory = ik_factory
        self._perception_factory = perception_factory
        self._load_destinations = load_destinations or (lambda: {})
        self._load_calibration = load_calibration or (lambda: {})
        self._emit_status = emit_status or (lambda _: None)
        # Fired exactly once when ``_run`` exits, with the terminal phase
        # ('finished' | 'stopped' | 'error'). Server side uses this to
        # release the on_workflow mutex without a polling timer.
        self._on_finished = on_finished or (lambda _phase: None)
        self._get_scene_frame = get_scene_frame
        self._get_gripper_frame = get_gripper_frame
        self._get_scene_frame_age = get_scene_frame_age
        self._get_current_pose_xyz = get_current_pose_xyz
        # Audit S2: source of the current follower joint state, used at
        # workflow start to seed ctx.last_full_joints with the real arm pose
        # instead of the [0]*6 dataclass default.
        self._get_follower_joints = get_follower_joints
        # Roboter Studio named-object catalog loader (may raise on a
        # missing/corrupt catalog — caught at start(), surfaced at the block).
        self._load_object_catalog = load_object_catalog
        # ArmProfile seam (§16.4 slice 2c): None → OMX geometry, bit-identical.
        # The node passes the resolved profile (PR 4); n drives every width in
        # the seed/precheck below and the ctx profile stamps.
        self._arm_profile = arm_profile
        n = getattr(arm_profile, 'num_arm_joints', None) if arm_profile else None
        try:
            self._num_arm_joints = int(n) if n else 5
        except (TypeError, ValueError):
            self._num_arm_joints = 5
        home = getattr(arm_profile, 'home_joints_rad', None) if arm_profile else None
        g_open = getattr(arm_profile, 'gripper_open_rad', None) if arm_profile else None
        if (home is not None and g_open is not None
                and len(home) == self._num_arm_joints):
            self._home_full_joints = [float(v) for v in home] + [float(g_open)]
        else:
            self._home_full_joints = list(_HOME_FULL_JOINTS)
        self._thread: Optional[threading.Thread] = None
        self._hat_threads: list[threading.Thread] = []
        self._stop_event = threading.Event()
        # Pause/step plumbing. resume_event is set when the workflow
        # is allowed to run; cleared while paused. step_event is set
        # by step() and cleared after one block executes.
        self._pause_event = threading.Event()  # set = paused
        self._resume_event = threading.Event()
        self._resume_event.set()  # not paused at startup
        self._step_event = threading.Event()
        self._broadcast_events: dict[str, threading.Event] = {}
        self._broadcast_lock = threading.Lock()
        # RLock not Lock — a hat handler holds motion_lock for its
        # whole body; motion handlers (`handlers/motion.py:_publish_motion`)
        # then re-acquire it to also serialize against the main stack.
        # A non-reentrant Lock would deadlock for 10s on every motion
        # block executed from inside a hat handler. RLock allows the
        # same thread to recursive-acquire safely.
        self._motion_lock = threading.RLock()
        # Variable mutation lock (Audit §A1). Re-entrant so a procedure
        # call can read/write inside an outer variables_set.
        self._var_lock = threading.RLock()
        # Claimed/skipped tag-id set lock for the named-object loop. Separate
        # from motion_lock (which serializes motion, not set integrity) so a
        # when_object_seen hat that grabs can't tear the set vs the main loop.
        self._claim_lock = threading.RLock()
        self._lock = threading.Lock()
        # Guards the pause/step/resume EVENT TRIO as one unit, so a step token
        # can be tested-and-cleared atomically (see _consume_step_token).
        # Deliberately NOT self._lock, which start() holds for its whole body —
        # the same reason _warn_once has its own _warn_lock. Lock order is
        # self._lock → self._pause_lock, one-directional: the consumers
        # (_wait_if_paused / _wait_for_resume, on the workflow thread) take only
        # this one, so there is no inversion.
        self._pause_lock = threading.Lock()
        # Audit fix #4: store breakpoints as an immutable frozenset that
        # set_breakpoints() rebinds atomically. The interpreter reads via
        # ctx.get_breakpoints() so the reader always sees a stable
        # snapshot for the duration of one block dispatch, and the
        # writer never tears state.
        self._breakpoints: frozenset[str] = frozenset()
        # Keys of the once-per-run German diagnostics already emitted (see
        # _warn_once). Cleared at every start(). Its own lock — _warn_once is
        # called from inside start(), which holds the non-reentrant self._lock.
        self._warned_keys: set[str] = set()
        self._warn_lock = threading.Lock()
        self._workflow_id: str | None = None
        # Persistent destinations across runs — set by mark_destination
        # callbacks in physical_ai_server.py and read into WorkflowContext
        # at start time.
        self._persisted_destinations: dict[str, dict[str, float]] = {}
        # Persistent recorded hand-guided trajectories across runs (Batch 2b) —
        # set by the /workshop record path and read into WorkflowContext at start
        # time, mirroring _persisted_destinations. name → CONTRACT-B dict.
        self._persisted_trajectories: dict[str, Any] = {}

    @property
    def is_running(self) -> bool:
        # Audit fix #5: report False once the stop event has been
        # signalled, even if the daemon thread is still in its finally
        # block. Callers (pause/resume/step + the server's mode-mutex)
        # then refuse to interact with a workflow that's already winding
        # down instead of racing the teardown.
        if self._stop_event.is_set():
            return False
        return self._thread is not None and self._thread.is_alive()

    def _prev_run_threads_alive(self) -> bool:
        """True while ANY thread of the PREVIOUS run is still running.

        ``is_running`` cannot answer this: it short-circuits on
        ``_stop_event.is_set()``, and ``stop()`` sets that flag BEFORE joins that
        are timeout-bounded (5.0 s main, 2.0 s per hat). A thread parked in a
        call that does not poll stop therefore outlives both — five raw
        ``acquire(timeout=10.0)`` sites alone can park one for 10 s
        (interpreter.py, handlers/trajectory.py, handlers/perception_blocks.py
        x2, handlers/motion.py), and ``perception.detect`` under the AprilTag
        lock is the CPU-only-Pi case. ``ctx.should_stop is
        self._stop_event.is_set``, so the next start's ``clear()`` UN-STOPS it.

        The MAIN thread is not optional here. Guarding only ``_hat_threads``
        left a stopped run publishing 104 further waypoints after a new run had
        started, with both writing ``/leader/joint_trajectory``; the old run's
        ``finally`` then re-set the stop event (killing the new run), nulled
        ``self._thread`` (so ``stop()`` answered „Es läuft kein Workflow." for a
        live run) and fired ``_on_finished`` (releasing ``on_workflow`` so a
        recording could claim the arm).

        Bounded by construction: ``_run`` always reaches its ``finally``.
        """
        thread = self._thread
        if thread is not None and thread.is_alive():
            return True
        return any(t.is_alive() for t in self._hat_threads)

    @property
    def is_paused(self) -> bool:
        return self._pause_event.is_set()

    def set_destination(
        self,
        name: str,
        x: float,
        y: float,
        z: float,
        plane_tracked: bool = False,
    ) -> None:
        """Persist a teacher-pinned destination so the next workflow run
        has it available in ``ctx.destinations``.

        ``plane_tracked`` says the z is a CACHED reading off the table plane (a
        camera click) rather than a MEASURED height (a „Position merken" FK
        snapshot), so ``motion.resolve_destination_z`` re-asks the plane in force
        at run time instead of descending to a value the next touch-off
        invalidates. It defaults False — the conservative direction, and what
        every pre-existing caller and every hand-built test dict keeps.

        THE gate every destination writer passes through, and the reason the
        validation lives here rather than at each call site. There are three
        service writers (``mark_destination_callback``, ``capture_pose_callback``
        and the sim seed) plus the three block handlers, and until 2026-09-09
        exactly one of the service writers validated anything:
        ``capture_pose_callback`` checked the name AND ``math.isfinite`` on all
        three coordinates while ``mark_destination_callback``, twenty lines of
        the same feature away, checked neither and stored ``request.label``
        verbatim. rosbridge authenticates nobody, so „the block handlers already
        validate" is not a fence over this dict. One check here closes every
        writer at once and is the only place that cannot be forgotten again.

        Raises ``ValueError`` carrying the SHARED German sentence — the same
        text the block handlers raise as a ``WorkflowError``, from the same pure
        helpers, so the editor and the services cannot come to say two different
        things about one alphabet. The exception type differs because the
        consequence differs: a block handler aborts a RUN, this refuses a
        SERVICE CALL, and each caller turns it into its own response."""
        from physical_ai_server.workflow.handlers.destinations import (
            destination_coordinate_error_de,
            destination_name_error_de,
        )
        if not name:
            # Historical silent no-op for a falsy name, kept: every caller
            # already guards on it (`if wfm is not None and request.label`), so
            # nothing student-visible reaches here. A whitespace-only name is a
            # DIFFERENT case and is refused loudly below — the camera prompt's
            # own regex accepts '   ' and used to store it as a blank key.
            return
        # Strip, like every other writer of this dict already does
        # (`destination_pin`, `destination_current`, `capture_pose_callback`), so
        # a service pin and its Blockly block agree on what the name IS: React's
        # `nameValidator` trims, so ' A ' typed at the camera prompt used to be
        # stored under a key the block could never match.
        if isinstance(name, str):
            name = name.strip()
        message = destination_name_error_de(name)
        if message is not None:
            raise ValueError(message)
        try:
            fx, fy, fz = float(x), float(y), float(z)
        except (TypeError, ValueError):
            raise ValueError(destination_coordinate_error_de(
                name, float('nan'), float('nan'), float('nan')))
        message = destination_coordinate_error_de(name, fx, fy, fz)
        if message is not None:
            raise ValueError(message)
        self._persisted_destinations[name] = {
            'x': fx, 'y': fy, 'z': fz, 'label': name,
            'plane_tracked': bool(plane_tracked),
        }

    def get_destinations(self) -> dict[str, dict[str, float]]:
        return dict(self._persisted_destinations)

    def set_trajectory(self, name: str, trajectory: dict[str, Any]) -> None:
        """Persist a recorded hand-guided trajectory (Batch 2b) so the next
        workflow run has it in ``ctx.trajectories`` and ``/workshop/replay`` can
        resolve it by name. Mirrors ``set_destination``. ``trajectory`` is the
        CONTRACT-B ``{"fps": int, "points": [[j1..j5, grip, t_s], ...]}`` dict.

        NOT WIRED, deliberately, and don't assume otherwise: this method has
        ZERO production callers, so ``_persisted_trajectories`` is always empty
        and ``/workshop/replay``'s by-NAME branch always answers „Keine Aufnahme
        angegeben.". Every real replay arrives as an inline ``points_json``
        payload from RunControls, which is also where the cross-profile refusal
        lives. Kept rather than deleted because the by-name resolution is the
        intended shape for server-persisted recordings — but see ``start()``:
        the client payload deliberately WINS over anything persisted here, so
        wiring this up can never silently bypass that client-side check."""
        if not name or not isinstance(trajectory, dict):
            return
        self._persisted_trajectories[name] = trajectory

    def get_trajectories(self) -> dict[str, Any]:
        return dict(self._persisted_trajectories)

    def set_breakpoints(self, block_ids: list[str]) -> None:
        """Replace the active breakpoint set. Safe to call before, during,
        or after a workflow run; the runtime reads through
        ``ctx.get_breakpoints()`` on every block dispatch.

        Audit fix #4: atomic frozenset rebinding under ``self._lock`` so
        the interpreter's membership test can never observe a torn set.
        ``ctx.get_breakpoints`` returns the manager's latest frozenset, so
        rebinding here propagates to the running workflow without sharing
        a mutable object between threads.
        """
        if not isinstance(block_ids, (list, tuple, set)):
            block_ids = []
        new_set = frozenset(str(b) for b in block_ids if b)
        with self._lock:
            self._breakpoints = new_set

    def pause(self) -> tuple[bool, str]:
        # Audit fix #13: read is_running + mutate the events under the
        # manager lock so concurrent pause/resume/step calls don't
        # interleave their state changes.
        with self._lock:
            if not self.is_running:
                return False, 'Es läuft kein Workflow.'
            # Event mutations under _pause_lock so a consumer can never observe
            # a half-applied pause (see _consume_step_token).
            with self._pause_lock:
                self._pause_event.set()
                self._resume_event.clear()
        return True, 'Workflow pausiert.'

    def resume(self) -> tuple[bool, str]:
        # Audit fix #13: see pause().
        with self._lock:
            if not self.is_running:
                return False, 'Es läuft kein Workflow.'
            with self._pause_lock:
                self._pause_event.clear()
                self._resume_event.set()
        return True, 'Workflow fortgesetzt.'

    def step(self) -> tuple[bool, str]:
        """Allow exactly one block to execute, then re-pause. The
        runtime calls _wait_if_paused after each block; we set
        step_event to signal one-block bypass.

        Audit fix #13: refuse if the workflow isn't paused — stepping a
        running workflow has no defined semantics and previously
        silently set step_event for the next pause to consume.
        """
        with self._lock:
            if not self.is_running:
                return False, 'Es läuft kein Workflow.'
            with self._pause_lock:
                if not self._pause_event.is_set():
                    return False, 'Workflow ist nicht pausiert.'
                # ONE token, not a counter: a Semaphore would let rapid presses
                # ACCUMULATE instead of collapsing, and stop() must be able to
                # wake EVERY waiter with a single _step_event.set().
                self._step_event.set()
                self._resume_event.set()
        return True, 'Schritt ausgeführt.'

    def start(
        self,
        workflow_json: str,
        workflow_id: str,
    ) -> tuple[bool, str, list[dict[str, Any]]]:
        with self._lock:
            if self.is_running:
                return False, 'Es läuft bereits ein Workflow.', []
            # Reject oversized payloads up front so a runaway editor or
            # adversarial client can't pin the daemon thread.
            if len(workflow_json.encode('utf-8')) > MAX_WORKFLOW_JSON_BYTES:
                return False, (
                    f'Workflow-JSON ist zu groß '
                    f'(>{MAX_WORKFLOW_JSON_BYTES // 1024} KiB).'
                ), []
            try:
                interpreter = Interpreter.from_json(workflow_json)
            except InterpreterError as e:
                return False, str(e), []

            # THE REFUSAL GUARDS RUN BEFORE ``_stop_event.clear()``, AND THAT
            # ORDERING IS LOAD-BEARING.
            #
            # ``ctx.should_stop is self._stop_event.is_set``, so clearing the
            # event un-stops EVERY thread that is still holding a reference to
            # it — including a hat thread that survived the previous run. When
            # the clear ran first and a guard below then refused the start, the
            # refusal returned without restoring the flag: measured 2/2, a
            # surviving hat published 90 further waypoints with NO workflow
            # running and ``on_workflow`` already released (so a recording or a
            # jog could claim the arm alongside it), ``stop()`` answered „Es
            # läuft kein Workflow." because it short-circuits on ``is_running``,
            # and every later start was refused forever — an arm the student
            # cannot stop, in a session they cannot restart.
            #
            # The trigger is ANY thread parked in a call that does not poll
            # stop for longer than ``stop()``'s joins (5.0 s main, 2.0 s hat).
            #
            # AN EARLIER REVISION OF THIS COMMENT NAMED ``perception.detect``
            # under the AprilTag lock on the CPU-only Orange Pi. MEASURED AND
            # REFUTED with the real Perception + real pupil_apriltags: 0.6 ms
            # for 640x480 with 2 tags, 2.9 ms for 25 tags at 1280x720, and
            # 84.2 ms for 1920x1080 of pure noise — above any shipped camera and
            # still ~60x short of the 5 s needed, before any Pi scaling. With 17
            # concurrent callers (the shipped 16-hat cap plus main) the worst
            # wait was 9 ms. Do not restore that claim.
            #
            # Producers that ARE named, ranked: (1) ``EDUBOTICS_GRASP_SETTLE_S``
            # — ``motion._safe_float`` does not clamp it and it lands in a raw
            # ``time.sleep`` with no stop poll, so an operator setting 10 gives
            # every rig a deterministic park; (2) a ``Thread.start()`` that
            # fails part-way through the hat spawn loop, which needs no timing
            # at all; (3) five raw ``acquire(timeout=10.0)`` sites, an amplifier
            # rather than a cause — every holder traced releases promptly.
            #
            # So this is a real defect with no demonstrated everyday trigger:
            # the guard below is cheap removal of a whole class, not an
            # emergency. Recording that honestly is the point — the audit this
            # round came from lost a High finding (RS-08) to exactly the
            # opposite habit, verifying a reachable code path and ASSUMING the
            # real-world event that drives it.
            #
            # An earlier revision of this comment claimed the hat keep-alive is
            # "what made it reachable". That was wrong, and re-measuring is what
            # showed it: the defect reproduces on the pre-keep-alive tree with a
            # real 6 s-blocking hat, 2/2 trials, same 90 late waypoints. If
            # anything it was WORSE there — with no keep-alive, ``stop()``
            # short-circuits on ``is_running`` and returns in 0.00 s without even
            # attempting the 2 s join or emitting the zombie ``[WARNUNG]``, so
            # nothing surfaced at all. The keep-alive did not create the
            # exposure; it made ``stop()`` at least try.
            #
            # Both guards need only ``interpreter`` and ``self._hat_threads``,
            # so hoisting them costs nothing. Anything added here that can
            # return early MUST stay above the clear.
            _, hats = interpreter.split_roots()
            # Audit fix #7: refuse to start if a previous run's hat
            # threads haven't fully reaped yet. Re-using a workflow_id
            # while old daemons are still alive leaks broadcast state
            # and can re-fire stale triggers against the new run's ctx.
            if self._prev_run_threads_alive():
                return False, (
                    'Vorheriger Workflow läuft noch — bitte kurz warten.'
                ), []
            # Audit fix #16: cap hat handler count so a buggy or
            # adversarial workspace can't spawn hundreds of daemons.
            if len(hats) > MAX_HAT_HANDLERS:
                return False, (
                    'Zu viele Ereignis-Blöcke (Maximum 16).'
                ), []

            # EVERY path out of here restores the flag unless a run actually
            # took ownership of it — see the ``finally`` below.
            armed = False
            try:
                self._stop_event.clear()
                self._pause_event.clear()
                self._resume_event.set()
                self._step_event.clear()
                self._broadcast_events.clear()
                self._warned_keys = set()
                self._workflow_id = workflow_id

                calib = self._load_calibration() or {}

                # Load the named-object catalog tolerantly: a failure (missing /
                # corrupt / invalid JSON) is carried as a German error string and
                # only raised when a named-object block actually runs, so a workflow
                # with no named blocks is unaffected. Re-read each start so a catalog
                # edit applies on the next run without an environment restart.
                object_catalog = None
                object_catalog_error = None
                if self._load_object_catalog is not None:
                    try:
                        object_catalog = self._load_object_catalog()
                    except Exception as e:  # noqa: BLE001 — surfaced at the block
                        object_catalog = None
                        object_catalog_error = str(e)

                destinations = dict(self._persisted_destinations)
                for k, v in (self._load_destinations() or {}).items():
                    destinations.setdefault(k, v)

                # Batch 2b — recorded trajectories: server-persisted recordings first,
                # then the top-level ``trajectories`` sibling of workflow_json
                # (CONTRACT C) via setdefault (persisted wins, mirroring destinations).
                # PRECEDENCE: the CLIENT payload wins over a server-persisted
                # recording of the same name. It used to be the other way round, and
                # that was a latent cross-profile hole: RunControls stamps every
                # saved „Bewegung" with the rig's robot type and refuses a
                # cross-profile replay client-side, but a persisted entry silently
                # overrode the checked payload and bypassed that refusal. Latent
                # only because `set_trajectory` has no production caller today —
                # `_persisted_trajectories` is always empty, so this changes nothing
                # observable and closes the hole before it opens.
                trajectories = dict(self._parse_trajectories(workflow_json))
                for k, v in self._persisted_trajectories.items():
                    trajectories.setdefault(k, v)

                ik_instance = None
                if self._ik_factory is not None:
                    try:
                        ik_instance = self._ik_factory()
                    except Exception as e:
                        return False, f'IK-Solver konnte nicht initialisiert werden: {e}', []

                perception_instance = None
                if self._perception_factory is not None:
                    try:
                        perception_instance = self._perception_factory()
                    except Exception as e:
                        return False, f'Wahrnehmung konnte nicht initialisiert werden: {e}', []

                # Phase-4 no-go zones ride a top-level `zones` sibling in the
                # workflow_json (Interpreter.from_json reads only data['blocks'], so
                # the sibling is ignored by the interpreter). Parsed defensively and
                # threaded onto ctx.zones; both sim + real managers go through this
                # one start(), so both get zones.
                zones = self._parse_zones(workflow_json)

                # Phase-2 Tempo: parse the top-level ``tempo`` sibling once (clamped /
                # default 1.0) and thread it onto ctx.tempo below. Like zones, it is a
                # workflow_json sibling the interpreter ignores; both the sim + real
                # manager run through this start(), so both honour it.
                tempo = self._parse_tempo(workflow_json)

                # IK pre-check: walk the JSON for concrete destinations and
                # try a quick IK solve on each. Failures become
                # `unreachable_blocks` — non-fatal warnings the React side
                # surfaces as setWarningText on the affected blocks. A concrete pin
                # that sits inside a no-go zone is flagged on the same list. The
                # safety envelope is still the authoritative runtime gate.
                unreachable = self._ik_precheck(interpreter, ik_instance, zones)

                # Audit fix #6: seed ctx.last_full_joints synchronously HERE,
                # before hat threads (or the main daemon) ever spawn. The
                # previous design seeded inside _run on the daemon thread,
                # which meant a hat-block trigger could fire and begin motion
                # before _run had a chance to overwrite the [0,0,0,0,0,0]
                # dataclass default. Best-effort: an unavailable joint source
                # leaves the safe default in place and the first motion call
                # will populate it.
                seeded_joints: list[float] | None = None
                if self._get_follower_joints is not None:
                    try:
                        joints = self._get_follower_joints()
                        width = self._num_arm_joints + 1
                        if joints and len(joints) >= width:
                            vals = [float(x) for x in joints[:width]]
                            # Non-finite guard: a NaN/Inf joint value would seed the
                            # first commanded pose with garbage (NaN published straight
                            # through the trajectory, or Inf crashing build_segment).
                            # Treat it as "no seed" so the safe HOME default is used
                            # and the first real motion re-seeds cleanly.
                            if all(math.isfinite(v) for v in vals):
                                seeded_joints = vals
                    except Exception:  # noqa: BLE001 — best-effort seed
                        seeded_joints = None

                ctx = WorkflowContext(
                    publisher=self._publisher,
                    ik=ik_instance,
                    perception=perception_instance,
                    destinations=destinations,
                    # Batch 2b — recorded hand-guided trajectories (name → CONTRACT-B).
                    trajectories=trajectories,
                    z_table=calib.get('z_table'),
                    board_table_z=calib.get('board_table_z'),
                    scene_intrinsics=calib.get('scene_intrinsics'),
                    scene_extrinsics=calib.get('scene_extrinsics'),
                    table_plane=calib.get('table_plane'),
                    # W5 — ground-truth accuracy correction (None/0.0 when never run).
                    xy_correction=calib.get('xy_correction'),
                    yaw_bias_rad=float(calib.get('yaw_bias_rad', 0.0) or 0.0),
                    should_stop=self._stop_event.is_set,
                    log=lambda msg: self._emit_status({'log_message': msg}),
                    emit_detections=lambda dets: self._emit_status({'detections': dets}),
                    get_scene_frame=self._get_scene_frame,
                    get_gripper_frame=self._get_gripper_frame,
                    get_scene_frame_age=self._get_scene_frame_age,
                    get_current_pose_xyz=self._get_current_pose_xyz,
                    # Grasp-success check (#2): read the achieved gripper angle after
                    # a close to confirm the object is actually held.
                    get_follower_joints=self._get_follower_joints,
                    # Fresh per run: no gripper close commanded yet. NEVER seeded
                    # from the measured follower pose (unlike last_full_joints
                    # below) — only motion's close paths write it.
                    last_commanded_close_rad=None,
                    motion_lock=self._motion_lock,
                    var_lock=self._var_lock,
                    breakpoints=self._breakpoints,
                    # Audit fix #4: getter so the interpreter sees the
                    # manager's freshest frozenset on every block dispatch,
                    # without sharing a mutable object across threads.
                    get_breakpoints=lambda: self._breakpoints,
                    fire_broadcast=self._fire_broadcast,
                    wait_if_paused=self._wait_if_paused,
                    wait_for_resume=self._wait_for_resume,
                    set_paused=self._set_paused,
                    # The manager's existing non-consuming predicate. Wiring
                    # this to _wait_for_resume or _consume_step_token would eat
                    # the student's „Schritt" press from a lock-wait loop.
                    is_paused=lambda: self.is_paused,
                    object_catalog=object_catalog,
                    object_catalog_error=object_catalog_error,
                    # Fresh per-run counter store for the Zähler blocks + the
                    # when_counter_gt hat (never persisted across runs).
                    counters={},
                    # Fresh per-run claimed/skipped sets (never persisted across runs)
                    # + the shared lock guarding them.
                    claimed_tags=set(),
                    skipped_tags=set(),
                    claim_lock=self._claim_lock,
                    # Fresh per-run position trackers for the recycled-object
                    # reclaim (never persisted across runs).
                    claim_release_xy={},
                    claim_pick_xy={},
                    carried_tag=None,
                    all_done_notified=set(),
                    # Phase-4 no-go zones (None/empty → motion behaves as today).
                    zones=zones,
                    # Phase-2 Tempo (global speed multiplier; 1.0 → unchanged speed).
                    tempo=tempo,
                    # ArmProfile geometry stamps (motion._n & friends read these;
                    # None → OMX constants). last_full_joints starts as the width-
                    # correct all-zero sentinel (the seeded overwrite follows below).
                    last_full_joints=[0.0] * (self._num_arm_joints + 1),
                    num_arm_joints=self._num_arm_joints,
                    roll_joint_index=getattr(self._arm_profile, 'roll_joint_index', None)
                    if self._arm_profile else None,
                    home_joints_rad=getattr(self._arm_profile, 'home_joints_rad', None)
                    if self._arm_profile else None,
                    observe_pose_joints=getattr(
                        self._arm_profile, 'observe_pose_joints', None)
                    if self._arm_profile else None,
                    gripper_open_rad=getattr(self._arm_profile, 'gripper_open_rad', None)
                    if self._arm_profile else None,
                    gripper_closed_rad=getattr(
                        self._arm_profile, 'gripper_closed_rad', None)
                    if self._arm_profile else None,
                    velocity_limit_rad_s=getattr(
                        self._arm_profile, 'velocity_limit_rad_s', None)
                    if self._arm_profile else None,
                    grasp_held_margin_rad=getattr(
                        self._arm_profile, 'grasp_held_margin_rad', None)
                    if self._arm_profile else None,
                    # Reroute-ladder geometry (path_guard reads these off ctx; None →
                    # its OMX module constants, so every profile-less run is
                    # unchanged).
                    safe_travel_z_m=getattr(self._arm_profile, 'safe_travel_z_m', None)
                    if self._arm_profile else None,
                    tool_clear_m=getattr(self._arm_profile, 'tool_clear_m', None)
                    if self._arm_profile else None,
                    swing_heights_m=getattr(self._arm_profile, 'swing_heights_m', None)
                    if self._arm_profile else None,
                    swing_radii_m=getattr(self._arm_profile, 'swing_radii_m', None)
                    if self._arm_profile else None,
                )

                # Apply the synchronous seed so hat threads start with a
                # realistic last_full_joints rather than [0]*6.
                if seeded_joints is not None:
                    ctx.last_full_joints = seeded_joints

                # Spawn hat-block handler threads. Each grabs a per-handler
                # event and runs its body whenever the event fires.
                # ``hats`` and both refusal guards were hoisted above
                # ``_stop_event.clear()`` — see the block comment there. Do not move
                # them back down.
                self._hat_threads = []
                for hat in hats:
                    t = threading.Thread(
                        target=self._run_hat_handler,
                        args=(hat, interpreter, ctx),
                        name=f'workflow-{workflow_id}-hat-{hat.get("id", "?")}',
                        daemon=True,
                    )
                    self._hat_threads.append(t)

                # Static, start-time diagnostics: orphan events + unknown object
                # types. Best-effort — a diagnostic must never stop a run.
                try:
                    self._diagnose_events(interpreter, object_catalog)
                except Exception:  # noqa: BLE001
                    pass

                # If the synchronous seed above found no follower pose, keep trying
                # in the background instead of failing the whole run.
                if seeded_joints is None:
                    self._start_joint_seed_watchdog(ctx)

                self._thread = threading.Thread(
                    target=self._run,
                    args=(interpreter, ctx),
                    name=f'workflow-{workflow_id}',
                    daemon=True,
                )
                # ORDER IS LOAD-BEARING: hat threads FIRST, main stack second.
                # ``Thread.is_alive()`` is False for a thread that has not been
                # started, and the main stack can run to completion before a
                # later-started hat thread exists — so _await_hat_handlers saw "no
                # handlers alive" and ended the run immediately, which is the very
                # thing it was added to prevent. Starting the handlers first also
                # means they are already polling when the first main-stack block
                # runs, so a „sende Ereignis" as block one has a listener.
                for t in self._hat_threads:
                    t.start()
                self._thread.start()
                armed = True
                return True, 'Workflow gestartet.', unreachable
            finally:
                if not armed:
                    # No run took the flag: a refusal below the clear, an
                    # exception, or a Thread.start() that failed part-way
                    # through the spawn loop. Put it back exactly as it was
                    # found, so nothing that survived the PREVIOUS run is
                    # silently un-stopped and any handler this call already
                    # started exits at once.
                    self._stop_event.set()

    def _start_joint_seed_watchdog(self, ctx: WorkflowContext) -> None:
        """Keep seeding ``ctx.last_full_joints`` until a real readback arrives.

        The seed in ``start()`` is taken ONCE, synchronously. If the follower's
        first ``/joint_states`` message has not landed by then — routine right
        after „Umgebung starten", after a container recreate, or when the node
        respawns — the all-zero sentinel stays put and motion's
        ``_require_seeded_start_pose`` refuses EVERY motion block for the rest of
        the run. Measured: a readback arriving 0.3 s after start, a full 0.7 s
        BEFORE the program's first motion block, still failed the run with
        „Aktuelle Armstellung ist noch nicht bekannt …".

        Writes ONLY while the pose is still the unseeded sentinel, so it can
        never overwrite a pose a motion handler has already commanded (no real
        HOME on any shipped profile is all-zero). Exits as soon as it seeds, on
        stop, or at the timeout.

        NOT a substitute for the guard: with no joint source at all this never
        seeds and every motion block still refuses, which is correct — the
        alternative is commanding the first waypoint from an assumed pose, i.e.
        exactly the lurch the guard exists to prevent.
        """
        if self._get_follower_joints is None:
            return
        width = self._num_arm_joints + 1

        def _seed_loop() -> None:
            deadline = time.monotonic() + _JOINT_SEED_WATCHDOG_S
            while time.monotonic() < deadline:
                if self._stop_event.is_set():
                    return
                pose = getattr(ctx, 'last_full_joints', None)
                # Someone real has written a pose — nothing left to do.
                if not (pose and all(abs(float(v)) <= 1e-6 for v in pose)):
                    return
                try:
                    joints = self._get_follower_joints()
                    if joints and len(joints) >= width:
                        vals = [float(v) for v in joints[:width]]
                        if (all(math.isfinite(v) for v in vals)
                                and any(abs(v) > 1e-6 for v in vals)):
                            ctx.last_full_joints = vals
                            return
                except Exception:  # noqa: BLE001 — best-effort seed
                    pass
                time.sleep(_JOINT_SEED_POLL_S)

        threading.Thread(
            target=_seed_loop,
            name=f'workflow-{self._workflow_id}-jointseed',
            daemon=True,
        ).start()

    def _diagnose_events(self, interpreter: Interpreter, object_catalog) -> None:
        """German [WARNUNG]s for events and object types that can never pair up.

        None of these were reported at all: a „wenn Ereignis empfangen" whose
        event nobody sends fired 0 times in silence, a „sende Ereignis" nobody
        listens for was equally silent, and a „wenn <Typ> erkannt" naming a type
        absent from the catalog simply never fired — while the SAME type on the
        main stack fails loud with a German catalog error. That asymmetry is
        invisible from the editor, which is what makes it expensive.
        """
        senders: set[str] = set()
        listeners: set[str] = set()
        object_types: set[str] = set()

        def walk(block: Any) -> None:
            if not isinstance(block, dict):
                return
            btype = block.get('type')
            if btype == 'edubotics_broadcast':
                name = event_name_of(block)
                if name:
                    senders.add(name)
            elif btype == 'edubotics_when_broadcast':
                name = event_name_of(block)
                if name:
                    listeners.add(name)
            elif btype == 'edubotics_when_object_seen':
                raw = (block.get('fields') or {}).get('OBJECT_TYPE')
                if isinstance(raw, str) and raw.strip():
                    object_types.add(raw.strip())
            inputs = block.get('inputs')
            if isinstance(inputs, dict):
                for slot in inputs.values():
                    if isinstance(slot, dict):
                        walk(slot.get('block'))
                        walk(slot.get('shadow'))
            nxt = block.get('next')
            if isinstance(nxt, dict):
                walk(nxt.get('block'))

        for root in interpreter.roots:
            walk(root)

        for name in sorted(listeners - senders):
            self._warn_once(f'orphan-listener:{name}', (
                f'[WARNUNG] Auf das Ereignis „{name}" wartet ein Block, aber '
                f'kein Block sendet es — „Wenn Ereignis empfangen" wird nie '
                f'ausgelöst.'
            ))
        for name in sorted(senders - listeners):
            self._warn_once(f'orphan-sender:{name}', (
                f'[WARNUNG] Das Ereignis „{name}" wird gesendet, aber kein '
                f'„Wenn Ereignis empfangen"-Block wartet darauf.'
            ))
        if object_catalog is not None:
            for type_name in sorted(object_types):
                try:
                    object_catalog.recipe_for_type(type_name)
                except Exception:  # noqa: BLE001 — unknown type is the point
                    # This interpolates the INTERNAL catalog KEY („wuerfel"),
                    # not a label_de — unavoidable on this branch, because an
                    # UNKNOWN type by definition has no label. So say what to
                    # do about it rather than leaving a student staring at a
                    # word that appears nowhere in their program.
                    self._warn_once(f'unknown-type:{type_name}', (
                        f'[WARNUNG] „{type_name}" ist kein bekanntes Objekt — '
                        f'„Wenn {type_name} erkannt" wird nie ausgelöst. Bitte '
                        'den Typ im Block neu auswählen.'
                    ))

    def stop(self) -> tuple[bool, str]:
        if not self.is_running:
            return True, 'Es läuft kein Workflow.'
        self._stop_event.set()
        # Wake any paused thread so it can observe the stop flag.
        self._resume_event.set()
        self._step_event.set()
        # Wake every hat handler waiting on a broadcast Condition so
        # they observe the stop flag and exit their loops.
        self._wake_all_broadcasts()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        for t in self._hat_threads:
            t.join(timeout=2.0)
            # Audit fix #7: surface zombie hat handlers so the operator
            # sees them in logs instead of having them silently leak into
            # the next workflow run. The is_alive() check at next start()
            # will then refuse to spawn a new run until the zombie reaps.
            if t.is_alive():
                self._emit_status({
                    'workflow_id': self._workflow_id or '',
                    'log_message': (
                        f'[WARNUNG] Ereignis-Handler '
                        f'"{t.name}" ist noch aktiv nach Stopp.'
                    ),
                })
        return True, 'Stopp angefordert.'

    def _wake_all_broadcasts(self) -> None:
        # Audit fix #8: per-name Conditions now have their own per-name
        # Lock, so we must acquire each Condition's own lock around the
        # notify_all. Snapshot the values under _broadcast_lock first
        # so a concurrent _broadcast_state() insert doesn't break the
        # iteration, then release the dict lock before touching the
        # individual Conditions (avoids nested-lock dead-lock risk).
        with self._broadcast_lock:
            states = list(self._broadcast_events.values())
        for state in states:
            cond = state.get('cond') if isinstance(state, dict) else None
            if cond is None:
                continue
            try:
                with cond:
                    cond.notify_all()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Phase-2 plumbing (broadcast / pause / step)
    # ------------------------------------------------------------------
    # A broadcast counter per name, with a Condition for blocking waits.
    # Hat handlers track a "consumed count" and wake when the published
    # count exceeds it. This avoids the set/clear race the previous
    # implementation had (verifier flagged: a handler that hadn't yet
    # entered wait() would miss a fast set→clear pair).
    #
    # Audit fix #8: each broadcast NAME now owns its own Lock and its
    # own Condition. The previous implementation used a single shared
    # _broadcast_lock under every Condition, which serialised every
    # handler on a different broadcast through one mutex (a notify on
    # "obstacle_seen" would block a waiter for "task_done" because
    # they were both holding cond.lock which == _broadcast_lock). The
    # outer _broadcast_lock now only guards the _broadcast_events dict
    # itself — name discovery / insertion — and is released before
    # touching any individual broadcast's Condition.
    def _broadcast_state(self, name: str) -> dict:
        with self._broadcast_lock:
            state = self._broadcast_events.get(name)
            if state is None:
                per_name_lock = threading.Lock()
                state = {
                    'count': 0,
                    'lock': per_name_lock,
                    'cond': threading.Condition(per_name_lock),
                    # Per-handler thread tracking: each waiter records
                    # the count it last consumed under its own key in
                    # this dict (keyed by id(threading.current_thread)).
                    'consumed': {},
                }
                self._broadcast_events[name] = state
            return state

    def _broadcast_event(self, name: str):  # legacy compat for stop()
        # stop() iterates _broadcast_events and calls .set() — keep that
        # surface working by returning a tiny shim that .set() notifies.
        state = self._broadcast_state(name)
        return _BroadcastShim(state)

    def _fire_broadcast(self, name: str) -> None:
        state = self._broadcast_state(name)
        with state['cond']:
            state['count'] += 1
            state['cond'].notify_all()

    def _consume_step_token(self) -> bool:
        """Atomically take THE single-step token, if one is outstanding.

        „Schritt" grants ONE permission to run ONE block. The old shape was
        ``if self._pause_event.is_set() and self._step_event.is_set():`` then
        ``self._step_event.clear()`` — a test-and-clear with nothing between
        them, so two threads parked on the gate could BOTH pass one press
        (two ``when_broadcast`` hats on one event, or a hat body plus a
        still-running main stack). Measured against the REAL pre-fix bodies
        with 4 waiters, 120 trials: 0 bad at the default switch interval, 6-7
        bad (run to run) at ``sys.setswitchinterval(1e-6)``; this version is 0
        at both. Rare, not impossible — and each extra release may run a motion
        block the student did not ask for.

        ``_resume_event`` is cleared under the SAME lock, because that clear is
        what re-arms the pause for the next block. Doing it outside would let a
        step consumer clobber a concurrent „Fortsetzen" (which sets both
        ``_resume_event`` and clears ``_pause_event``) and leave the run wedged
        on a resume the student already pressed.
        """
        with self._pause_lock:
            if not (self._pause_event.is_set() and self._step_event.is_set()):
                return False
            self._step_event.clear()
            # Re-arm the pause: this single block runs, and the next gate call
            # blocks again.
            self._resume_event.clear()
            return True

    def _wait_if_paused(self) -> None:
        # Fast path: not paused, nothing to do.
        if not self._pause_event.is_set():
            return
        # If a single-step was requested, consume the token and proceed.
        if self._consume_step_token():
            return
        self._wait_for_resume()

    def _wait_for_resume(self) -> None:
        # Polling wait so we can also bail on stop events.
        while not self._stop_event.is_set():
            if self._resume_event.wait(0.1):
                # If still paused but step was requested, consume it.
                if self._consume_step_token():
                    return
                if not self._pause_event.is_set():
                    return

    def _set_paused(self, value: bool) -> None:
        # _pause_lock spans the EVENT MUTATIONS ONLY — never the _emit_status
        # publish below, which reaches the ROS publisher and must not hold a
        # lock a paused workflow thread is polling for.
        if value:
            with self._pause_lock:
                self._pause_event.set()
                self._resume_event.clear()
            self._emit_status({
                'workflow_id': self._workflow_id or '',
                'phase': 'paused',
            })
        else:
            with self._pause_lock:
                self._pause_event.clear()
                self._resume_event.set()

    # ------------------------------------------------------------------
    # IK pre-check
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_zones(workflow_json: str) -> list | None:
        """Extract the top-level ``zones`` sibling injected into the
        ``/workflow/start`` payload (Phase-4 no-go zones). Defensive: a missing
        or non-list ``zones`` → None (run with no zones). The exact value
        returned here is what start() puts on ``ctx.zones`` AND passes to the
        pre-flight, for both the real and the sim manager."""
        try:
            parsed = json.loads(workflow_json)
        except (ValueError, TypeError):
            return None
        if not isinstance(parsed, dict):
            return None
        raw = parsed.get('zones')
        return raw if isinstance(raw, list) else None

    @staticmethod
    def _parse_trajectories(workflow_json: str) -> dict[str, Any]:
        """Extract the top-level ``trajectories`` sibling injected into the
        ``/workflow/start`` payload (Batch 2b, CONTRACT C: ``{"<name>": {"fps":
        int, "points": [[j1..j5, grip, t_s], ...]}, ...}``). Defensive: missing /
        non-dict / non-dict JSON → {} (run with no recordings). Only dict-valued
        entries with string keys are kept; malformed entries are dropped (a bad
        entry surfaces at the „Aufnahme abspielen" block, not at start). Mirrors
        ``_parse_zones`` — ``Interpreter.from_json`` reads only ``data['blocks']``,
        so the sibling is invisible to the interpreter, and both the real and the
        sim manager run through this one start()."""
        try:
            parsed = json.loads(workflow_json)
        except (ValueError, TypeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        raw = parsed.get('trajectories')
        if not isinstance(raw, dict):
            return {}
        out: dict[str, Any] = {}
        for k, v in raw.items():
            if isinstance(k, str) and isinstance(v, dict):
                out[k] = v
        return out

    @staticmethod
    def _parse_tempo(workflow_json: str) -> float:
        """Extract the top-level ``tempo`` speed multiplier injected into the
        ``/workflow/start`` payload (Phase-2 run-bar Tempo + optional per-move).
        Mirrors ``_parse_zones``: ``Interpreter.from_json`` reads only
        ``data['blocks']``, so the sibling is invisible to the interpreter. Both
        the real and the sim manager call this one start(), so both honour Tempo.

        Defensive: missing / non-dict JSON / non-numeric / bool / non-finite /
        non-positive → 1.0 (normal speed); a valid value is CLAMPED to the safe
        ``[_TEMPO_MIN, _TEMPO_MAX]`` window so ctx.tempo is always sane (motion's
        ``_resolve_tempo`` re-clamps defensively too).

        Rule §2: a teaching speed knob, not an inference safety envelope."""
        try:
            parsed = json.loads(workflow_json)
        except (ValueError, TypeError):
            return 1.0
        if not isinstance(parsed, dict):
            return 1.0
        raw = parsed.get('tempo')
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return 1.0
        val = float(raw)
        if not math.isfinite(val) or val <= 0.0:
            return 1.0
        return max(_TEMPO_MIN, min(_TEMPO_MAX, val))

    def _ik_precheck(
        self,
        interpreter: Interpreter,
        ik,
        zones: list | None = None,
    ) -> list[dict[str, Any]]:
        if ik is None or not hasattr(ik, 'solve'):
            # Even without an IK solver we can still flag concrete pins that sit
            # inside a no-go zone (a pure-geometry test).
            if zones:
                return self._zone_precheck_only(interpreter, zones)
            return []
        targets = interpreter.collect_concrete_destinations()
        if not targets:
            return []
        unreachable: list[dict[str, Any]] = []
        # A lightweight ctx-shim carrying only ``zones`` so the pre-flight reuses
        # motion._point_in_zone (which reads getattr(ctx, 'zones', None)).
        zone_ctx = types.SimpleNamespace(zones=zones)
        # Scale the total budget proportionally so a 50-target workflow
        # doesn't silently truncate after the first 20 (audit round-3
        # §24). Hard floor 1 s, ceiling 5 s.
        scaled_budget = max(_IKPRECHECK_TOTAL_BUDGET_S,
                            min(5.0, len(targets) * _IKPRECHECK_PER_TARGET_TIMEOUT_S * 2))
        budget_end = time.monotonic() + scaled_budget
        # Seed from HOME (matches the runtime's first-call seed when
        # last_arm_joints is unset). Audit round-3 §23 — pre-check
        # passing seed=None when runtime seeds from HOME produced
        # false unreachable warnings on reachable destinations.
        precheck_seed = list(self._home_full_joints[:self._num_arm_joints])
        for target in targets:
            if time.monotonic() > budget_end:
                break
            xyz = target['xyz']
            try:
                solution = ik.solve(
                    target_xyz=xyz,
                    seed=precheck_seed,
                    free_yaw=True,
                )
            except Exception:
                solution = None
            if solution is None:
                unreachable.append({
                    'block_id': target['block_id'],
                    'message': 'Diese Position ist außerhalb des Arbeitsbereichs.',
                })
            elif zones and _point_in_zone(zone_ctx, xyz[0], xyz[1], xyz[2]):
                unreachable.append({
                    'block_id': target['block_id'],
                    'message': 'Diese Position liegt in einer Sperrzone.',
                })
        return unreachable

    def _zone_precheck_only(
        self,
        interpreter: Interpreter,
        zones: list,
    ) -> list[dict[str, Any]]:
        """Flag concrete destination pins that sit inside a no-go zone, when no
        IK solver is available (pure geometry)."""
        out: list[dict[str, Any]] = []
        zone_ctx = types.SimpleNamespace(zones=zones)
        for target in interpreter.collect_concrete_destinations():
            xyz = target['xyz']
            if _point_in_zone(zone_ctx, xyz[0], xyz[1], xyz[2]):
                out.append({
                    'block_id': target['block_id'],
                    'message': 'Diese Position liegt in einer Sperrzone.',
                })
        return out

    @staticmethod
    def _debounce_absence(raw: frozenset, seen_at: dict) -> frozenset:
        """Hold a tag in the trigger set until it has been unseen for
        ``_HAT_ABSENT_GRACE_S``.

        Appearances are instant, disappearances are debounced — the asymmetry
        is deliberate. A new object must still fire the hat promptly, while a
        tag the detector merely BLINKED must not read as "gone" and re-arm the
        edge. ``seen_at`` is the caller's per-handler state (one dict per hat
        thread, never shared), mapping tag id → last monotonic time seen.

        A tag that is genuinely consumed (claimed by the body) simply stops
        appearing and ages out of the set after the grace, which is what lets
        „Wenn … gesehen" still fire once per object.
        """
        now = time.monotonic()
        for tag in raw:
            seen_at[tag] = now
        for tag in [t for t, ts in seen_at.items()
                    if now - ts > _HAT_ABSENT_GRACE_S]:
            del seen_at[tag]
        return frozenset(seen_at)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def _run_hat_handler(
        self,
        hat: dict[str, Any],
        interpreter: Interpreter,
        ctx: WorkflowContext,
    ) -> None:
        """Loop forever (until stop) waiting for a hat trigger, then run
        the body once. Motion blocks inside the body acquire ctx.motion_lock
        so they don't race the main stack.

        Audit fix #14: the object-seen perception hat uses EDGE-triggered
        semantics — once it fires, it must observe the condition going false
        (the object leaves frame) before re-arming. A level-triggered design
        would re-run the body every poll while the trigger stayed true, firing
        the handler hundreds of times for an object held in front of the camera.
        Broadcasts are inherently edge-triggered (count > last) so they're
        unaffected.
        """
        btype = hat.get('type')
        # German name for every message this handler emits (Rule §1) — the raw
        # block-type id used to be interpolated straight into the Protokoll.
        label = _hat_label_de(btype)
        consecutive_errors = 0
        # Edge-trigger arming state for the level-triggered perception/counter
        # hats. None means 'first wait, treat as armed'; True means armed
        # (allowed to fire on next true), False means waiting for the condition
        # to clear (object leaves frame / counter drops back to ≤ N).
        _EDGE_HATS = ('edubotics_when_object_seen', 'edubotics_when_counter_gt')
        edge_armed = True
        # For the object hat the trigger is a SET of unclaimed-visible tag ids, not
        # a bare bool, and the edge is on that set CHANGING. Truthiness alone was
        # not enough: with two cubes placed, grasping the first leaves the second
        # visible, so the condition never went false, so edge_armed never re-armed
        # and the hat fired exactly ONCE per run — one cube grasped, one ignored,
        # green „Workflow abgeschlossen". Keying on the set fires once PER OBJECT
        # while keeping the original anti-spin property intact: a static object
        # nobody consumes yields an unchanging set and still fires exactly once.
        # The set compared here is the DEBOUNCED one (see _debounce_absence) —
        # edging on the raw per-poll detections made ordinary AprilTag flicker
        # look like an object leaving and re-entering the scene.
        last_fired_ids: frozenset | None = None
        # ABSENCE DEBOUNCE for the object hat. The raw per-poll detection set
        # flickers: AprilTag detection drops a tag for a frame or two under
        # motion blur, glare or partial occlusion all the time. Edging on the
        # RAW set therefore re-armed on noise — measured, one tag of two missed
        # on alternate polls fired a non-consuming body 30 times in 30 polls,
        # where the invariant is exactly once.
        #
        # A tag is treated as PRESENT until it has been continuously unseen for
        # _HAT_ABSENT_GRACE_S. Appearances count immediately (a new object must
        # still trigger fast); only disappearance is debounced. That asymmetry
        # is the whole fix.
        #
        # The grace is EDUBOTICS_RECLAIM_ABSENT_S (1.5 s), which this hat is now
        # the only consumer of: the „Solange sichtbar" reclaim that once shared
        # it moved to a POSITION rule, because it samples once per 8–12 s loop
        # pass and could never tell a missed look from a student's hand. This
        # hat polls several times a second, so the seconds still answer the
        # question here.
        _seen_at: dict[int, float] = {}
        try:
            while not ctx.should_stop():
                triggered = self._wait_for_hat_trigger(hat, ctx)
                if isinstance(triggered, frozenset):
                    triggered = self._debounce_absence(triggered, _seen_at)
                if not triggered:
                    # For the level-triggered hats, an un-triggered poll cycle
                    # means the condition is currently false; re-arm. MUST list
                    # the SAME hats as the edge-gate below, or a hat fires once
                    # and then never re-arms (edge_armed stuck False).
                    if btype in _EDGE_HATS:
                        edge_armed = True
                    continue
                if ctx.should_stop():
                    return
                # Edge-trigger gate. Skip body execution while we wait
                # for the condition to clear and re-arm.
                if btype in _EDGE_HATS:
                    if isinstance(triggered, frozenset) and triggered != last_fired_ids:
                        # A DIFFERENT object is waiting than the one we last acted
                        # on — that is a fresh edge, not the same one held open.
                        edge_armed = True
                    if not edge_armed:
                        time.sleep(0.1)
                        continue
                    edge_armed = False
                    if isinstance(triggered, frozenset):
                        last_fired_ids = triggered
                # Acquire the motion lock for the entire handler body.
                # This is conservative — even a perception-only handler
                # holds the lock — but it keeps the safety story simple.
                #
                # THROUGH THE ONE HELPER, and that is not tidiness. This was a
                # blocking ``with ctx.motion_lock:`` while the composite
                # motions polled, and a re-entering timed acquire loses EVERY
                # handoff to a thread parked in a blocking one: measured on a
                # bare RLock, blocking waiter 12 acquires, polling waiter 0.
                # That single asymmetry turned an ordinary event program that
                # finishes 3/3 into one that errors 3/3. See
                # motion._hold_motion_lock.
                cycle_start = time.monotonic()
                try:
                    acquired = _hold_motion_lock(ctx)
                except WorkflowError:
                    # „Workflow wurde gestoppt." while queueing for the arm —
                    # the loop condition above says the same thing.
                    return
                try:
                    if ctx.should_stop():
                        return
                    try:
                        interpreter.execute_chain(
                            hat,
                            ctx,
                            self._on_block_change,
                        )
                        consecutive_errors = 0
                    except (WorkflowError, InterpreterError) as e:
                        # KEEP THE HANDLER ALIVE. All four except arms used to
                        # `return`, so ONE failing body silenced the hat for the
                        # whole run while the run still reported green: measured
                        # with 2 cubes, one grasped, the drop failed, the handler
                        # exited, the second cube was never touched, the arm was
                        # LEFT HOLDING the first, phase='finished', 5/5. A
                        # one-off failure (a GraspSkip on an object that rolled
                        # away) must not be terminal.
                        consecutive_errors += 1
                        self._emit_status({
                            'workflow_id': self._workflow_id or '',
                            'log_message': f'„{label}": {e}',
                        })
                    except Exception:
                        consecutive_errors += 1
                        self._emit_status({
                            'workflow_id': self._workflow_id or '',
                            'log_message': (
                                f'„{label}": Interner Fehler im Ereignis-Block.'
                            ),
                        })
                finally:
                    _release_motion_lock(ctx, acquired)
                if consecutive_errors >= MAX_HAT_CONSECUTIVE_ERRORS:
                    # ...but a body that fails EVERY time is a bug, not a
                    # hiccup, and would otherwise spin against the rate floor
                    # for the rest of the run. Retire it, loudly.
                    self._emit_status({
                        'workflow_id': self._workflow_id or '',
                        'log_message': (
                            f'[WARNUNG] „{label}" wurde {consecutive_errors}× '
                            f'hintereinander mit einem Fehler beendet und wird '
                            f'nicht mehr ausgeführt.'
                        ),
                    })
                    return
                # Rate floor: bound how often ONE hat can run its body, so a
                # handler whose body re-triggers the handler cannot saturate a
                # core or flood the status channel (see HAT_MIN_CYCLE_S).
                elapsed = time.monotonic() - cycle_start
                if elapsed < HAT_MIN_CYCLE_S:
                    self._sleep_until_stop(ctx, HAT_MIN_CYCLE_S - elapsed)
        except Exception:
            return

    @staticmethod
    def _sleep_until_stop(ctx, seconds: float) -> None:
        """Sleep up to ``seconds``, waking early on stop. Used by the hat rate
        floor and the post-main keep-alive, both of which must not delay a Stop."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if ctx.should_stop():
                return
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def _warn_once(self, key: str, message: str) -> None:
        """Emit a German [WARNUNG] at most once per run per ``key``.

        Diagnostics on a per-poll or per-firing path must never become their own
        flood — the very failure mode several of them exist to report.

        Guarded by its OWN lock, never ``self._lock``: the start-time diagnostics
        run from inside ``start()``, which already holds ``self._lock`` — and
        that is a plain non-reentrant ``Lock``, so reusing it dead-locked every
        workflow carrying an orphan event (a hats-only program hung on start,
        found by running it)."""
        with self._warn_lock:
            if key in self._warned_keys:
                return
            self._warned_keys.add(key)
        self._emit_status({
            'workflow_id': self._workflow_id or '',
            'log_message': message,
        })

    def _wait_for_hat_trigger(
        self,
        hat: dict[str, Any],
        ctx: WorkflowContext,
    ) -> bool:
        btype = hat.get('type')
        fields = hat.get('fields') or {}
        if btype == 'edubotics_when_broadcast':
            name = event_name_of(hat)
            if not name:
                # An un-named hat used to `return False` with NO sleep, so the
                # handler loop span a tight CPU cycle for the whole run:
                # measured, 16 empty-named hats = 110 % of one core. Match
                # _wait_counter_gt, which already sleeps on this path.
                time.sleep(0.2)
                return False
            state = self._broadcast_state(name)
            tid = threading.get_ident()
            with state['cond']:
                # BASELINE AT ZERO, NOT AT THE CURRENT COUNT. Defaulting to
                # `state['count']` meant a handler's FIRST wait raised its own
                # baseline to whatever had already been broadcast — so
                # „sende Ereignis" as the first block of the main stack, with a
                # „wenn Ereignis empfangen" hat, fired 0 times out of 20. Not a
                # race: start() starts the main thread before the hat threads,
                # so the broadcast is ALWAYS already counted. Measured 0/20 at
                # t=0 versus 20/20 once the send was delayed to t≥0.01 s.
                last = state['consumed'].get(tid, 0)
                # Wait up to 0.25s for a NEW broadcast (count > last).
                deadline = time.monotonic() + 0.25
                while state['count'] <= last and not ctx.should_stop():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    state['cond'].wait(timeout=remaining)
                if state['count'] > last:
                    # Consume exactly ONE, so a burst is QUEUED rather than
                    # coalesced: `consumed[tid] = count` swallowed 5 back-to-back
                    # sends into a single firing, where Scratch queues them.
                    backlog = state['count'] - last
                    if backlog > MAX_BROADCAST_BACKLOG:
                        # The producer is outrunning this consumer. Skip forward
                        # rather than growing an unbounded queue, and say so once.
                        state['consumed'][tid] = state['count'] - MAX_BROADCAST_BACKLOG
                        self._warn_once(
                            f'broadcast-backlog:{name}:{tid}',
                            f'[WARNUNG] Das Ereignis „{name}" wird schneller '
                            f'gesendet als der Ereignis-Block es abarbeiten '
                            f'kann — es werden Ereignisse übersprungen.'
                        )
                    else:
                        state['consumed'][tid] = last + 1
                    return True
                return False
        if btype == 'edubotics_when_object_seen':
            type_name = (fields.get('OBJECT_TYPE') or '').strip()
            if not type_name:
                time.sleep(0.2)
                return False
            return self._wait_object_visible(type_name, ctx)
        if btype == 'edubotics_when_counter_gt':
            return self._wait_counter_gt(fields, ctx)
        return False

    def _wait_counter_gt(self, fields: dict[str, Any], ctx: WorkflowContext) -> bool:
        """Edge-trigger poll for ``edubotics_when_counter_gt``: True when the
        named counter exceeds the threshold N. Polls a short budget (matching the
        other hats' cadence) so the handler loop never spins hot; the per-name
        edge gate in _run_hat_handler stops the body re-firing every poll while
        the counter stays above N (it re-arms once a ``reset`` drops it ≤ N)."""
        name = (fields.get('NAME') or '').strip()
        if not name:
            time.sleep(0.2)
            return False
        try:
            threshold = float(fields.get('N', 0))
        except (TypeError, ValueError):
            threshold = 0.0
        # Lazy import (handlers/__init__ already loads counters; avoids any
        # import-order fragility at module top).
        from physical_ai_server.workflow.handlers import counters as _counters
        for _ in range(2):  # ~0.4 s budget, matching the object-seen hat
            if ctx.should_stop():
                return False
            if _counters.get_count(ctx, name) > threshold:
                return True
            time.sleep(0.2)
        return False

    def _wait_object_visible(self, type_name: str, ctx: WorkflowContext):
        """Edge-trigger poll for ``edubotics_when_object_seen``.

        Returns the ``frozenset`` of currently-visible UNCLAIMED tag ids of
        ``type_name`` (empty / ``False`` when nothing qualifies). Non-empty is
        truthy, so every ``if not triggered`` caller behaves as it did when this
        returned a bare bool — but ``_run_hat_handler`` edges on the SET changing,
        which is what makes the hat fire once per OBJECT instead of once per RUN.

        Resolves the type's tag ids from the object catalog and polls AprilTag
        perception. Catalog/perception errors are swallowed (the hat simply
        doesn't fire) — a named block on the MAIN stack surfaces the German
        catalog error loudly instead.

        THE RETURN TYPE IS LOAD-BEARING, and the two falsy shapes are NOT
        interchangeable:

        * ``frozenset()`` — "I looked (or tried to) and there is nothing".
          ``_run_hat_handler`` runs this through ``_debounce_absence``, so a
          camera that blinks does not read as the object leaving.
        * bare ``False`` — "this is not a detection observation at all"
          (nothing left to look FOR, or the run is stopping). It bypasses the
          debounce and re-arms the edge immediately, which is correct: when
          every tag is claimed the hat MUST go quiet, and holding the claimed
          ids in the debounce would re-fire the body on them.

        Every "could not look" path — no frame, no perception, no catalog —
        used to return the bare ``False``, so it skipped the debounce and
        re-armed the edge. Through the real ``_run_hat_handler`` with a
        non-consuming body over 30 polls, a feed that answers ``None`` every
        3rd poll fires the body 10× and every 2nd poll 20×, against an
        invariant of exactly once.

        WHAT DOES *NOT* CAUSE THAT, corrected 2026-09-08: an ordinary dropped
        camera frame. An earlier revision of this comment claimed it did, and
        that claim is what made the original finding (RS-08) a false positive.
        ``communicator.get_latest_bgr_frame`` reads ``camera_topic_msgs``, the
        latest CACHED message, so a dropped ROS frame yields the PREVIOUS image
        — never ``None``. Real AprilTag flicker on a live frame was already
        handled correctly before this change.

        The ``None`` paths that genuinely exist are narrower and all real: no
        frame has arrived yet (startup, and a hat can poll before the first
        one lands), the camera is not subscribed, and a JPEG that fails to
        decode. Those are what the debounce now absorbs, and they are the whole
        justification for this return type — not a frame drop."""
        if not type_name:
            return False
        for _ in range(2):  # 2 × ~0.5 s budget, matching the other hats
            if ctx.should_stop():
                return False
            try:
                catalog = getattr(ctx, 'object_catalog', None)
                if (catalog is None or ctx.perception is None
                        or ctx.get_scene_frame is None):
                    time.sleep(0.5)
                    return frozenset()
                recipe = catalog.recipe_for_type(type_name)  # ObjectCatalogError on unknown
                # Claim-aware, exactly like every MAIN-stack path (which reaches
                # the same view through _detect_named_unclaimed). A tag already
                # grasped (claimed) or confirmed-failed (skipped) must not keep
                # this level condition true: _run_hat_handler only re-arms
                # edge_armed on an UNtriggered poll, so a permanently-true
                # condition made the hat fire exactly ONCE per run and then never
                # again. On the rig that hid, because the object is physically
                # carried out of frame; in sim the virtual tag never leaves, so
                # „Wenn Würfel gesehen: Greife" grabbed one cube of two and
                # reported a green „Workflow abgeschlossen".
                #
                # Deliberately INLINE rather than calling _detect_named_unclaimed:
                # that helper emits ctx.emit_detections (a ~5 Hz /workflow/status
                # flood from this poll) and runs the recycled-object reclaim from a
                # second thread, neither of which belongs in a trigger check.
                from physical_ai_server.workflow.handlers.perception_blocks import (
                    _excluded_ids,
                )
                wanted = ({int(i) for i in recipe.tag_ids}
                          - {int(i) for i in _excluded_ids(ctx)})
                if not wanted:
                    # Every instance of this type is done — stay un-triggered so
                    # the handler re-arms instead of spinning. A BARE False on
                    # purpose (see the docstring): routing this through the
                    # absence debounce would keep the already-claimed ids in the
                    # trigger set and re-fire the body on them.
                    time.sleep(0.2)
                    return False
                frame = ctx.get_scene_frame()
                if frame is None:
                    # NOT a dropped frame — that returns the cached previous
                    # image (see the docstring). This is "no frame has arrived
                    # yet", "camera not subscribed", or "JPEG failed to decode":
                    # an observation of nothing, which the debounce absorbs.
                    # Must be a frozenset, not a bare False.
                    time.sleep(0.2)
                    return frozenset()
                detections = ctx.perception.detect(
                    frame, camera='scene', mode='apriltag', aruco_id=None,
                )
                seen = frozenset(
                    getattr(d, 'aruco_id', None) for d in (detections or [])
                ) & wanted
                if seen:
                    # The SET, not just True: _run_hat_handler edges on it changing
                    # so the hat fires once per object rather than once per run.
                    # Non-empty => truthy, so every existing `if not triggered`
                    # check behaves exactly as it did with a bool.
                    return seen
            except Exception:
                pass
            time.sleep(0.2)
        return frozenset()

    def _run(self, interpreter: Interpreter, ctx: WorkflowContext) -> None:
        terminal_phase = 'error'
        # Audit fix #6: ctx.last_full_joints is seeded synchronously in
        # start() before any handler thread spawns, so this block no
        # longer needs to redo the work here.
        #
        # Follower-only: the workflow trajectory publisher is the sole writer
        # on /leader/joint_trajectory (no leader broadcaster launched), so no
        # teleop arbitration is needed around the run.
        try:
            self._emit_status({
                'workflow_id': self._workflow_id or '',
                'phase': 'running',
                'progress': 0.0,
                'log_message': 'Workflow läuft.',
            })
            # Wrap the main interpreter call so the motion lock is held
            # by default for every motion block in the main stack. We
            # don't acquire the lock for the whole execute() — that
            # would starve hat handlers — instead motion handlers
            # themselves acquire it (or the chunked_publish does).
            interpreter.execute(ctx, self._on_block_change)
            # The main stack is done — but the hat handlers are the program too.
            self._await_hat_handlers(ctx)
            if ctx.should_stop():
                terminal_phase = 'stopped'
                self._emit_status({
                    'workflow_id': self._workflow_id or '',
                    'phase': 'stopped',
                    'progress': 1.0,
                    'log_message': 'Workflow wurde gestoppt.',
                })
            else:
                terminal_phase = 'finished'
                self._emit_status({
                    'workflow_id': self._workflow_id or '',
                    'phase': 'finished',
                    'progress': 1.0,
                    'log_message': 'Workflow abgeschlossen.',
                })
        except WorkflowError as e:
            # Pre-existing audit fix: a WorkflowError raised because of
            # ctx.should_stop() (the interpreter checks the flag and
            # raises 'Workflow wurde gestoppt.') is a clean stop, not
            # an error. Distinguishing them lets the on_finished
            # callback see the right terminal_phase.
            if ctx.should_stop():
                terminal_phase = 'stopped'
                self._emit_status({
                    'workflow_id': self._workflow_id or '',
                    'phase': 'stopped',
                    'progress': 1.0,
                    'log_message': 'Workflow wurde gestoppt.',
                })
            else:
                terminal_phase = 'error'
                self._emit_status({
                    'workflow_id': self._workflow_id or '',
                    'phase': 'error',
                    'error': str(e),
                    'log_message': str(e),
                })
        except InterpreterError as e:
            terminal_phase = 'error'
            self._emit_status({
                'workflow_id': self._workflow_id or '',
                'phase': 'error',
                'error': str(e),
                'log_message': str(e),
            })
        except Exception:
            terminal_phase = 'error'
            self._emit_status({
                'workflow_id': self._workflow_id or '',
                'phase': 'error',
                'error': 'Interner Fehler — bitte den Lehrer rufen.',
                'log_message': traceback.format_exc(),
            })
        finally:
            # Tell hat handlers to wind down so they can observe the
            # stop flag and exit cleanly.
            self._stop_event.set()
            self._resume_event.set()
            self._wake_all_broadcasts()

            # Reap hat threads with a short timeout.
            for t in self._hat_threads:
                if t.is_alive():
                    t.join(timeout=1.0)

            # Audit fix #5: clear self._thread BEFORE calling
            # _on_finished. If the callback queries is_running on its
            # way out (or the caller starts a new workflow synchronously
            # from inside on_finished), it must observe "not running".
            # Mutate under self._lock so start() can't observe a stale
            # alive() thread between this clear and the daemon's exit.
            try:
                with self._lock:
                    self._thread = None
            except Exception:
                self._thread = None
            try:
                self._on_finished(terminal_phase)
            except Exception:
                pass

    def _await_hat_handlers(self, ctx: WorkflowContext) -> None:
        """Keep the run alive for its hat handlers after the main stack ends.

        WHY. ``_run``'s ``finally`` sets the stop event the instant
        ``interpreter.execute`` returns, and that had two student-visible
        consequences:

        * A program made ONLY of hats — „Wenn Würfel erkannt: Greife", the most
          natural program the block set can express — finished green in 0.157 s
          having fired 0 times. Measured 10/10 for all three hat types. The
          workaround students were driven to is an empty „wiederhole
          fortlaufend" as a keep-alive, which is exactly the accidental
          complexity the hat blocks exist to remove.
        * A hat body already IN FLIGHT was truncated. ON `main` THIS IS
          INVISIBLE and TOTAL: the broadcast from block 1 never reaches its hat,
          so the body publishes 0 of 190 waypoints while the run reports
          'finished'. The 45-of-190 (24 %) cut — mid-approach, gripper open,
          hovering — is the same defect measured on the INTERMEDIATE tree, where
          the hat does fire.

        So: wait. Exits on Stop (the normal case), when every handler thread has
        exited on its own, or at HAT_KEEPALIVE_MAX_S. A run with no hats returns
        immediately, so every existing single-stack workflow is unchanged.
        """
        if not self._hat_threads:
            return
        if ctx.should_stop():
            return
        if not any(t.is_alive() for t in self._hat_threads):
            return
        self._emit_status({
            'workflow_id': self._workflow_id or '',
            'log_message': (
                'Hauptprogramm fertig — die Ereignis-Blöcke laufen weiter. '
                'Zum Beenden auf „Stopp" drücken.'
            ),
        })
        deadline = time.monotonic() + HAT_KEEPALIVE_MAX_S
        while time.monotonic() < deadline:
            if ctx.should_stop():
                return
            if not any(t.is_alive() for t in self._hat_threads):
                # Every handler retired itself (all errored out, or the run is
                # winding down) — nothing left to wait for.
                return
            time.sleep(0.05)
        self._emit_status({
            'workflow_id': self._workflow_id or '',
            'log_message': (
                '[WARNUNG] Zeitlimit erreicht — die Ereignis-Blöcke werden '
                'beendet.'
            ),
        })

    def _on_block_change(self, block_id: str, phase: str, progress: float) -> None:
        self._emit_status({
            'workflow_id': self._workflow_id or '',
            'current_block_id': block_id,
            'phase': phase,
            'progress': float(progress),
        })


class _BroadcastShim:
    """Legacy ``threading.Event``-shaped wrapper around the new
    counter+Condition broadcast state. Only used by callers that
    still iterate ``_broadcast_events`` and call ``.set()`` (currently
    none after the refactor, but we keep the shim so a future audit
    finds it safe rather than an attribute error)."""

    def __init__(self, state: dict) -> None:
        self._state = state

    def set(self) -> None:
        cond = self._state.get('cond')
        if cond is None:
            return
        with cond:
            self._state['count'] = self._state.get('count', 0) + 1
            cond.notify_all()

    def wait(self, timeout: float | None = None) -> bool:
        cond = self._state.get('cond')
        if cond is None:
            return False
        with cond:
            seen = self._state.get('count', 0)
            return cond.wait_for(
                lambda: self._state.get('count', 0) > seen,
                timeout=timeout,
            )

    def clear(self) -> None:
        # No-op — counter-based; consumers track their own consumed count.
        pass

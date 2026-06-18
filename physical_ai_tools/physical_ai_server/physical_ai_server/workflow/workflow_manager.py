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

import math
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from physical_ai_server.workflow.handlers.motion import WorkflowError
from physical_ai_server.workflow.interpreter import (
    Interpreter,
    InterpreterError,
)


# Reference pose used to seed the IK pre-check at workflow start. Matches
# handlers.motion.HOME_JOINTS_RAD + GRIPPER_OPEN_RAD; duplicated here to
# avoid a circular import.
_HOME_FULL_JOINTS = [0.0, -math.pi / 4, math.pi / 4, 0.0, 0.0, 0.8]

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
    z_table: float | None = None
    scene_intrinsics: dict | None = None
    scene_extrinsics: Any | None = None
    # Touch-off measured table plane (a, b, c) for z = a·x + b·y + c; None →
    # projection uses the flat z_table plane.
    table_plane: tuple[float, float, float] | None = None
    last_arm_joints: list[float] | None = None
    last_full_joints: list[float] = field(default_factory=lambda: [0.0] * 6)
    should_stop: Callable[[], bool] = field(default_factory=lambda: (lambda: False))
    log: Callable[[str], None] = field(default_factory=lambda: (lambda _: None))
    # Push the most recent perception detection list to the WorkflowStatus
    # publisher so the React editor can render bbox overlays. Each entry
    # is a perception.Detection — the server's _emit_workflow_status
    # adapter packs them into the typed Detection[] on the message.
    emit_detections: Callable[[list], None] = field(default_factory=lambda: (lambda _: None))
    variables: dict[str, Any] = field(default_factory=dict)
    get_scene_frame: Callable[[], Any] | None = None
    get_gripper_frame: Callable[[], Any] | None = None
    # Age (seconds) of the latest scene frame, for the perception staleness
    # gate (reject a frozen camera). None → unknown (gate skipped).
    get_scene_frame_age: Callable[[], float | None] | None = None
    get_current_pose_xyz: Callable[[], tuple[float, float, float] | None] | None = None
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
    procedures: dict[str, dict[str, Any]] = field(default_factory=dict)
    call_procedure: Callable[[str, list[Any]], Any] = field(
        default_factory=lambda: (lambda _name, _args: None)
    )
    # Cloud-vision configuration (phase 3)
    cloud_vision: dict[str, Any] = field(default_factory=dict)


MAX_WORKFLOW_JSON_BYTES = 256 * 1024  # 256 KiB; see plan §2.5

# Audit fix #16: cap the number of hat-block handlers a single workflow
# can spawn. Each hat handler is its own daemon thread + broadcast
# Condition; a malicious or buggy workspace with hundreds of when_*
# blocks would otherwise saturate the server. 16 is generous for the
# classroom — the largest pre-existing tutorial uses 3.
MAX_HAT_HANDLERS = 16


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
        self._lock = threading.Lock()
        # Audit fix #4: store breakpoints as an immutable frozenset that
        # set_breakpoints() rebinds atomically. The interpreter reads via
        # ctx.get_breakpoints() so the reader always sees a stable
        # snapshot for the duration of one block dispatch, and the
        # writer never tears state.
        self._breakpoints: frozenset[str] = frozenset()
        self._workflow_id: str | None = None
        # Persistent destinations across runs — set by mark_destination
        # callbacks in physical_ai_server.py and read into WorkflowContext
        # at start time.
        self._persisted_destinations: dict[str, dict[str, float]] = {}

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

    @property
    def is_paused(self) -> bool:
        return self._pause_event.is_set()

    def set_destination(self, name: str, x: float, y: float, z: float) -> None:
        """Persist a teacher-pinned destination so the next workflow run
        has it available in ``ctx.destinations``."""
        if not name:
            return
        self._persisted_destinations[name] = {
            'x': float(x), 'y': float(y), 'z': float(z), 'label': name,
        }

    def get_destinations(self) -> dict[str, dict[str, float]]:
        return dict(self._persisted_destinations)

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
            self._pause_event.set()
            self._resume_event.clear()
        return True, 'Workflow pausiert.'

    def resume(self) -> tuple[bool, str]:
        # Audit fix #13: see pause().
        with self._lock:
            if not self.is_running:
                return False, 'Es läuft kein Workflow.'
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
            if not self._pause_event.is_set():
                return False, 'Workflow ist nicht pausiert.'
            self._step_event.set()
            self._resume_event.set()
        return True, 'Schritt ausgeführt.'

    def start(
        self,
        workflow_json: str,
        workflow_id: str,
        cloud_vision: dict[str, Any] | None = None,
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

            self._stop_event.clear()
            self._pause_event.clear()
            self._resume_event.set()
            self._step_event.clear()
            self._broadcast_events.clear()
            self._workflow_id = workflow_id

            calib = self._load_calibration() or {}
            destinations = dict(self._persisted_destinations)
            for k, v in (self._load_destinations() or {}).items():
                destinations.setdefault(k, v)

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

            # IK pre-check: walk the JSON for concrete destinations and
            # try a quick IK solve on each. Failures become
            # `unreachable_blocks` — non-fatal warnings the React side
            # surfaces as setWarningText on the affected blocks. The
            # safety envelope is still the authoritative runtime gate.
            unreachable = self._ik_precheck(interpreter, ik_instance)

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
                    if joints and len(joints) >= 6:
                        seeded_joints = [float(x) for x in joints[:6]]
                except Exception:  # noqa: BLE001 — best-effort seed
                    seeded_joints = None

            ctx = WorkflowContext(
                publisher=self._publisher,
                ik=ik_instance,
                perception=perception_instance,
                destinations=destinations,
                z_table=calib.get('z_table'),
                scene_intrinsics=calib.get('scene_intrinsics'),
                scene_extrinsics=calib.get('scene_extrinsics'),
                table_plane=calib.get('table_plane'),
                should_stop=self._stop_event.is_set,
                log=lambda msg: self._emit_status({'log_message': msg}),
                emit_detections=lambda dets: self._emit_status({'detections': dets}),
                get_scene_frame=self._get_scene_frame,
                get_gripper_frame=self._get_gripper_frame,
                get_scene_frame_age=self._get_scene_frame_age,
                get_current_pose_xyz=self._get_current_pose_xyz,
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
                cloud_vision=dict(cloud_vision or {}),
            )

            # Apply the synchronous seed so hat threads start with a
            # realistic last_full_joints rather than [0]*6.
            if seeded_joints is not None:
                ctx.last_full_joints = seeded_joints

            # Spawn hat-block handler threads. Each grabs a per-handler
            # event and runs its body whenever the event fires.
            _, hats = interpreter.split_roots()
            # Audit fix #7: refuse to start if a previous run's hat
            # threads haven't fully reaped yet. Re-using a workflow_id
            # while old daemons are still alive leaks broadcast state
            # and can re-fire stale triggers against the new run's ctx.
            if any(t.is_alive() for t in self._hat_threads):
                return False, (
                    'Vorheriger Workflow läuft noch — bitte kurz warten.'
                ), []
            # Audit fix #16: cap hat handler count so a buggy or
            # adversarial workspace can't spawn hundreds of daemons.
            if len(hats) > MAX_HAT_HANDLERS:
                return False, (
                    'Zu viele Ereignis-Blöcke (Maximum 16).'
                ), []
            self._hat_threads = []
            for hat in hats:
                t = threading.Thread(
                    target=self._run_hat_handler,
                    args=(hat, interpreter, ctx),
                    name=f'workflow-{workflow_id}-hat-{hat.get("id", "?")}',
                    daemon=True,
                )
                self._hat_threads.append(t)

            self._thread = threading.Thread(
                target=self._run,
                args=(interpreter, ctx),
                name=f'workflow-{workflow_id}',
                daemon=True,
            )
            self._thread.start()
            for t in self._hat_threads:
                t.start()
            return True, 'Workflow gestartet.', unreachable

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

    def _wait_if_paused(self) -> None:
        # Fast path: not paused, nothing to do.
        if not self._pause_event.is_set():
            return
        # If a single-step was requested, consume the token and proceed.
        if self._step_event.is_set():
            self._step_event.clear()
            # Re-arm the pause: this single block will run, and the
            # next call to _wait_if_paused will block again.
            self._resume_event.clear()
            return
        self._wait_for_resume()

    def _wait_for_resume(self) -> None:
        # Polling wait so we can also bail on stop events.
        while not self._stop_event.is_set():
            if self._resume_event.wait(0.1):
                # If still paused but step was requested, consume it.
                if self._pause_event.is_set() and self._step_event.is_set():
                    self._step_event.clear()
                    self._resume_event.clear()
                    return
                if not self._pause_event.is_set():
                    return

    def _set_paused(self, value: bool) -> None:
        if value:
            self._pause_event.set()
            self._resume_event.clear()
            self._emit_status({
                'workflow_id': self._workflow_id or '',
                'phase': 'paused',
            })
        else:
            self._pause_event.clear()
            self._resume_event.set()

    # ------------------------------------------------------------------
    # IK pre-check
    # ------------------------------------------------------------------
    def _ik_precheck(self, interpreter: Interpreter, ik) -> list[dict[str, Any]]:
        if ik is None or not hasattr(ik, 'solve'):
            return []
        targets = interpreter.collect_concrete_destinations()
        if not targets:
            return []
        unreachable: list[dict[str, Any]] = []
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
        precheck_seed = list(_HOME_FULL_JOINTS[:5])
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
        return unreachable

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

        Audit fix #14: marker / color hat handlers use EDGE-triggered
        semantics — once they fire, they must observe the condition
        going false (marker leaves frame / color disappears) before
        re-arming. The previous level-triggered design re-ran the body
        every poll while the trigger stayed true, which means a marker
        held in front of the camera would fire the handler hundreds of
        times. Broadcasts are inherently edge-triggered (count > last)
        so they're unaffected.
        """
        btype = hat.get('type')
        # Edge-trigger arming state for marker / color hats. None means
        # 'first wait, treat as armed'; True means armed (allowed to fire
        # on next true), False means waiting for the condition to clear.
        edge_armed = True
        try:
            while not ctx.should_stop():
                triggered = self._wait_for_hat_trigger(hat, ctx)
                if not triggered:
                    # For perception hats, an un-triggered poll cycle
                    # means the condition is currently false; re-arm.
                    if btype in {'edubotics_when_marker_seen',
                                 'edubotics_when_color_seen'}:
                        edge_armed = True
                    continue
                if ctx.should_stop():
                    return
                # Edge-trigger gate. Skip body execution while we wait
                # for the condition to clear and re-arm.
                if btype in {'edubotics_when_marker_seen',
                             'edubotics_when_color_seen'}:
                    if not edge_armed:
                        time.sleep(0.1)
                        continue
                    edge_armed = False
                # Acquire the motion lock for the entire handler body.
                # This is conservative — even a perception-only handler
                # holds the lock — but it keeps the safety story simple.
                with ctx.motion_lock:
                    if ctx.should_stop():
                        return
                    try:
                        interpreter.execute_chain(
                            hat,
                            ctx,
                            self._on_block_change,
                        )
                    except WorkflowError:
                        return
                    except InterpreterError as e:
                        self._emit_status({
                            'workflow_id': self._workflow_id or '',
                            'log_message': f'Hat-Handler "{btype}": {e}',
                        })
                        return
                    except Exception:
                        self._emit_status({
                            'workflow_id': self._workflow_id or '',
                            'log_message': f'Hat-Handler "{btype}": Fehler.',
                        })
                        return
        except Exception:
            return

    def _wait_for_hat_trigger(
        self,
        hat: dict[str, Any],
        ctx: WorkflowContext,
    ) -> bool:
        btype = hat.get('type')
        fields = hat.get('fields') or {}
        if btype == 'edubotics_when_broadcast':
            name = (fields.get('EVENT_NAME') or '').strip()
            if not name:
                return False
            state = self._broadcast_state(name)
            tid = threading.get_ident()
            with state['cond']:
                last = state['consumed'].get(tid, state['count'])
                # Wait up to 0.25s for a NEW broadcast (count > last).
                deadline = time.monotonic() + 0.25
                while state['count'] <= last and not ctx.should_stop():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    state['cond'].wait(timeout=remaining)
                if state['count'] > last:
                    state['consumed'][tid] = state['count']
                    return True
                return False
        if btype == 'edubotics_when_marker_seen':
            target_id = int(fields.get('MARKER_ID', 0) or 0)
            return self._wait_marker_visible(target_id, ctx)
        if btype == 'edubotics_when_color_seen':
            color = (fields.get('COLOR') or '').strip()
            min_pixels = int(fields.get('MIN_PIXELS', 200) or 200)
            return self._wait_color_visible(color, min_pixels, ctx)
        return False

    def _wait_marker_visible(self, target_id: int, ctx: WorkflowContext) -> bool:
        # Poll perception @ 5 Hz; bail on stop.
        for _ in range(2):  # 2 × 0.5 s = 1 s blocking budget per loop
            if ctx.should_stop():
                return False
            try:
                if ctx.perception is None or ctx.get_scene_frame is None:
                    time.sleep(0.5)
                    return False
                frame = ctx.get_scene_frame()
                if frame is None:
                    time.sleep(0.2)
                    return False
                # Mode name must match Perception.detect() dispatch:
                # 'apriltag' (not 'marker'). Audit round-3 §AI — the
                # earlier `'marker'` literal silently fell through the
                # if/elif and returned `[]`, so the hat block never
                # fired even when a tag was visible.
                detections = ctx.perception.detect(
                    frame, camera='scene', mode='apriltag', aruco_id=target_id,
                )
                if detections:
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

    def _wait_color_visible(self, color: str, min_pixels: int, ctx: WorkflowContext) -> bool:
        for _ in range(2):
            if ctx.should_stop():
                return False
            try:
                if ctx.perception is None or ctx.get_scene_frame is None:
                    time.sleep(0.5)
                    return False
                frame = ctx.get_scene_frame()
                if frame is None:
                    time.sleep(0.2)
                    return False
                detections = ctx.perception.detect(
                    frame, camera='scene', mode='color', color=color,
                )
                # Each detection has a bbox (x, y, w, h); approximate
                # pixel area as w*h.
                total = 0
                for d in detections or []:
                    bbox = getattr(d, 'bbox_px', None)
                    if bbox and len(bbox) == 4:
                        total += int(bbox[2]) * int(bbox[3])
                if total >= min_pixels:
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

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

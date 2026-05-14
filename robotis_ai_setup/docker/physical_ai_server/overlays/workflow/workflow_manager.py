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

Recovery (hold-current → open gripper → return home) runs on the main
thread's ``finally`` block whenever the workflow exits stopped or
errored. Hat threads themselves never run recovery — they exit when
should_stop fires; the main thread handles the cleanup.
"""

from __future__ import annotations

import math
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import numpy as np

from physical_ai_server.workflow.handlers.motion import WorkflowError
from physical_ai_server.workflow.interpreter import (
    Interpreter,
    InterpreterError,
)
from physical_ai_server.workflow.safety_envelope import SafetyEnvelope
from physical_ai_server.workflow.trajectory_builder import build_segment, chunked_publish


# Recovery pose for the auto-home routine in _run's finally block. Must
# match handlers.motion.HOME_JOINTS_RAD + GRIPPER_OPEN_RAD; duplicated
# here to avoid a circular import. Audit §3.16 — a stopped/errored
# workflow used to leave the arm wherever it stopped (potentially
# mid-grasp with the gripper closed on an object).
_HOME_FULL_JOINTS = [0.0, -math.pi / 4, math.pi / 4, 0.0, 0.0, 0.8]
_RECOVERY_HOLD_S = 1.0
_RECOVERY_GRIPPER_S = 0.5
_RECOVERY_HOME_S = 3.0
# Absolute ceiling on the entire recovery routine. Designed total is
# 4.5 s; 15 s caps a stuck recovery (IK fail mid-segment, controller
# wedged) so the daemon can exit. The hold + gripper-open segments
# remain uninterruptible (controller settle + release any held object);
# only the long return-home segment honours the deadline.
_RECOVERY_DEADLINE_S = 15.0

# IK pre-check budget. Each concrete destination gets one solver
# attempt; an overall cap so a 100-block workflow doesn't stall start.
_IKPRECHECK_PER_TARGET_TIMEOUT_S = 0.05
_IKPRECHECK_TOTAL_BUDGET_S = 1.0


@dataclass
class WorkflowContext:
    """Runtime state shared between the manager and individual handlers."""

    publisher: Callable[[list[tuple[list[float], float]]], None]
    safety: SafetyEnvelope
    ik: Any | None = None
    perception: Any | None = None
    destinations: dict[str, dict[str, float]] = field(default_factory=dict)
    z_table: float | None = None
    scene_intrinsics: dict | None = None
    scene_extrinsics: Any | None = None
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
    get_current_pose_xyz: Callable[[], tuple[float, float, float] | None] | None = None
    # Phase-2 additions
    motion_lock: threading.RLock | None = None  # reentrant: hat body + inner publish
    # Re-entrant variable lock so the variables_get/set blocks from main
    # and hat handlers don't race on the plain dict. Audit §A1 —
    # without this, mutation from a `for` loop in main while a hat
    # reads via `variables_get` tears the dict.
    var_lock: threading.RLock | None = None
    breakpoints: set[str] = field(default_factory=set)
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
        self._get_current_pose_xyz = get_current_pose_xyz
        # Audit S2: source of the current follower joint state, used at
        # workflow start to seed the safety envelope's per-tick delta cap
        # AND ctx.last_full_joints (so recovery's hold segment starts at
        # the real arm pose, not the [0]*6 dataclass default).
        self._get_follower_joints = get_follower_joints
        self._safety = SafetyEnvelope()
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
        self._breakpoints: set[str] = set()
        self._workflow_id: str | None = None
        # Persistent destinations across runs — set by mark_destination
        # callbacks in physical_ai_server.py and read into WorkflowContext
        # at start time.
        self._persisted_destinations: dict[str, dict[str, float]] = {}

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_paused(self) -> bool:
        return self._pause_event.is_set()

    def configure_safety(
        self,
        joint_min: list[float],
        joint_max: list[float],
        max_delta_per_tick: list[float],
    ) -> None:
        self._safety.set_action_limits(
            joint_min=joint_min,
            joint_max=joint_max,
            max_delta_per_tick=max_delta_per_tick,
        )

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
        or after a workflow run; the runtime checks the live set on every
        block dispatch.

        Audit fix (round-3): MUST mutate in-place rather than rebind the
        attribute. WorkflowContext.breakpoints is captured by reference at
        ``start()`` (see line ~314 ``breakpoints=self._breakpoints``);
        rebinding the manager attribute leaves ctx pointing at the OLD
        set object and mid-run breakpoint updates would never reach the
        runtime.
        """
        if not isinstance(block_ids, (list, tuple, set)):
            block_ids = []
        new_set = {str(b) for b in block_ids if b}
        self._breakpoints.clear()
        self._breakpoints.update(new_set)

    def pause(self) -> tuple[bool, str]:
        if not self.is_running:
            return False, 'Es läuft kein Workflow.'
        self._pause_event.set()
        self._resume_event.clear()
        return True, 'Workflow pausiert.'

    def resume(self) -> tuple[bool, str]:
        if not self.is_running:
            return False, 'Es läuft kein Workflow.'
        self._pause_event.clear()
        self._resume_event.set()
        return True, 'Workflow fortgesetzt.'

    def step(self) -> tuple[bool, str]:
        """Allow exactly one block to execute, then re-pause. The
        runtime calls _wait_if_paused after each block; we set
        step_event to signal one-block bypass."""
        if not self.is_running:
            return False, 'Es läuft kein Workflow.'
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
            self._safety.reset()
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

            ctx = WorkflowContext(
                publisher=self._publisher,
                safety=self._safety,
                ik=ik_instance,
                perception=perception_instance,
                destinations=destinations,
                z_table=calib.get('z_table'),
                scene_intrinsics=calib.get('scene_intrinsics'),
                scene_extrinsics=calib.get('scene_extrinsics'),
                should_stop=self._stop_event.is_set,
                log=lambda msg: self._emit_status({'log_message': msg}),
                emit_detections=lambda dets: self._emit_status({'detections': dets}),
                get_scene_frame=self._get_scene_frame,
                get_gripper_frame=self._get_gripper_frame,
                get_current_pose_xyz=self._get_current_pose_xyz,
                motion_lock=self._motion_lock,
                var_lock=self._var_lock,
                breakpoints=self._breakpoints,
                fire_broadcast=self._fire_broadcast,
                wait_if_paused=self._wait_if_paused,
                wait_for_resume=self._wait_for_resume,
                set_paused=self._set_paused,
                cloud_vision=dict(cloud_vision or {}),
            )

            # Spawn hat-block handler threads. Each grabs a per-handler
            # event and runs its body whenever the event fires.
            _, hats = interpreter.split_roots()
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
        return True, 'Stopp angefordert.'

    def _wake_all_broadcasts(self) -> None:
        with self._broadcast_lock:
            for state in self._broadcast_events.values():
                cond = state.get('cond') if isinstance(state, dict) else None
                if cond is not None:
                    # We already hold _broadcast_lock which is the same
                    # primitive as the Condition's lock — notify_all is
                    # safe here without a separate `with`.
                    cond.notify_all()

    # ------------------------------------------------------------------
    # Phase-2 plumbing (broadcast / pause / step)
    # ------------------------------------------------------------------
    # A broadcast counter per name, with a Condition for blocking waits.
    # Hat handlers track a "consumed count" and wake when the published
    # count exceeds it. This avoids the set/clear race the previous
    # implementation had (verifier flagged: a handler that hadn't yet
    # entered wait() would miss a fast set→clear pair).
    def _broadcast_state(self, name: str) -> dict:
        with self._broadcast_lock:
            state = self._broadcast_events.get(name)
            if state is None:
                state = {
                    'count': 0,
                    'cond': threading.Condition(self._broadcast_lock),
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
    # Recovery + main loop
    # ------------------------------------------------------------------
    def _run_recovery(self, ctx: WorkflowContext) -> None:
        """Hold-current → open gripper → return home, after a stopped
        or errored workflow.

        Hold + gripper-open run uninterruptibly: the controller must
        settle the in-flight trajectory and any held object must be
        released before we move toward home (otherwise we go home
        carrying it). The home segment honours an absolute deadline
        (_RECOVERY_DEADLINE_S) so a wedged recovery can't hang the
        daemon thread indefinitely.

        Verifier finding (audit §10): recovery must hold ``motion_lock``
        for the duration so a still-running hat handler can't publish
        a competing trajectory while we're going home. Hat handlers
        themselves should observe ``should_stop`` between blocks and
        exit, but a long ``chunked_publish`` segment is uninterrupted
        — the lock is the failsafe.
        """
        deadline = time.monotonic() + _RECOVERY_DEADLINE_S
        deadline_exceeded = lambda: time.monotonic() > deadline
        # Acquire motion_lock for the recovery sequence. Use a bounded
        # acquire so a hat thread wedged inside a C-extension (cv2 /
        # onnxruntime — uninterruptible from Python) can't permanently
        # hang the daemon. If acquire fails we proceed without the lock;
        # the worst case is a competing hat publish, but the daemon
        # exits cleanly. Audit round-3 §20.
        lock = ctx.motion_lock
        acquired = False
        if lock is not None:
            acquired = lock.acquire(timeout=2.0)
            if not acquired:
                self._emit_status({
                    'workflow_id': self._workflow_id or '',
                    'log_message': (
                        'Recovery konnte motion_lock nicht erhalten — '
                        'Bewegung übersprungen.'
                    ),
                })
        try:
            current = list(ctx.last_full_joints) if ctx.last_full_joints else list(_HOME_FULL_JOINTS)
            # Hold-current segment so the controller has time to settle
            # the in-flight trajectory without immediately commanding
            # new motion. Uninterruptible — settling can't be skipped.
            hold = build_segment(current, current, _RECOVERY_HOLD_S)
            chunked_publish(
                publisher=ctx.publisher,
                points=hold,
                safety_apply=ctx.safety.apply if ctx.safety else None,
                should_stop=lambda: False,
            )
            # Open gripper. Uninterruptible — releasing a held object
            # before the home traversal is what prevents "go home with
            # the part still gripped → drag it across the bench".
            opened = list(current[:5]) + [_HOME_FULL_JOINTS[5]]
            open_seg = build_segment(current, opened, _RECOVERY_GRIPPER_S)
            chunked_publish(
                publisher=ctx.publisher,
                points=open_seg,
                safety_apply=ctx.safety.apply if ctx.safety else None,
                should_stop=lambda: False,
            )
            # Return to home pose over 3 seconds. Honours the absolute
            # recovery deadline so a stuck home traversal aborts.
            home_seg = build_segment(opened, list(_HOME_FULL_JOINTS), _RECOVERY_HOME_S)
            chunked_publish(
                publisher=ctx.publisher,
                points=home_seg,
                safety_apply=ctx.safety.apply if ctx.safety else None,
                should_stop=deadline_exceeded,
            )
            ctx.last_full_joints = list(_HOME_FULL_JOINTS)
        except Exception:
            # Recovery itself failing must not raise — we're in the
            # finally block of the daemon thread. Just log via the
            # status emitter and let the on_finished callback run.
            self._emit_status({
                'workflow_id': self._workflow_id or '',
                'log_message': 'Recovery-Bewegung fehlgeschlagen.',
            })
        finally:
            if lock is not None and acquired:
                try:
                    lock.release()
                except RuntimeError:
                    pass

    def _run_hat_handler(
        self,
        hat: dict[str, Any],
        interpreter: Interpreter,
        ctx: WorkflowContext,
    ) -> None:
        """Loop forever (until stop) waiting for a hat trigger, then run
        the body once. Motion blocks inside the body acquire ctx.motion_lock
        so they don't race the main stack."""
        btype = hat.get('type')
        try:
            while not ctx.should_stop():
                triggered = self._wait_for_hat_trigger(hat, ctx)
                if not triggered:
                    continue
                if ctx.should_stop():
                    return
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
        # Audit S2: seed the safety envelope's last-action AND
        # ctx.last_full_joints with the current follower pose, so:
        #   (a) the per-tick velocity cap on the very first motion compares
        #       against where the arm actually is, not the dataclass
        #       default of [0]*6 (which would skip the delta check
        #       silently because _last_action stays None).
        #   (b) recovery's hold-current segment publishes the real pose
        #       instead of a synthetic [0]*6 hold trajectory that would
        #       command the arm to fold across its body.
        # Failing to read the joint state is non-fatal: motion handlers
        # will populate last_full_joints on first use, and safety_envelope
        # falls back to None → no delta cap on the first action (same
        # behaviour as before this seed; we strictly improve the path).
        if self._get_follower_joints is not None:
            try:
                joints = self._get_follower_joints()
                if joints and len(joints) >= 6:
                    j6 = [float(x) for x in joints[:6]]
                    ctx.last_full_joints = j6
                    if self._safety is not None:
                        self._safety.seed_last_action(j6[:5])
            except Exception as _exc:  # noqa: BLE001 — best-effort seed
                self._emit_status({
                    'workflow_id': self._workflow_id or '',
                    'log_message': (
                        f'[WARNUNG] Aktueller Gelenkzustand nicht lesbar — '
                        f'Sicherheits-Hüllkurve startet ohne Seed ({_exc}).'
                    ),
                })
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
            # Tell hat handlers to wind down BEFORE running recovery so
            # they don't fire a new broadcast handler that grabs the
            # motion lock during recovery.
            self._stop_event.set()
            self._resume_event.set()
            self._wake_all_broadcasts()

            # Audit §3.16 — auto-home on stopped/errored exits (not on
            # a clean 'finished' run, which already left the arm where
            # the workflow intended). The recovery sequence matches
            # context/19-roboter-studio.md §5: hold-current → open
            # gripper → return home over 3s. Must run before
            # on_finished fires so the server's on_workflow flag flips
            # only AFTER the recovery motion completes.
            if terminal_phase in ('stopped', 'error'):
                self._run_recovery(ctx)

            # Reap hat threads with a short timeout.
            for t in self._hat_threads:
                if t.is_alive():
                    t.join(timeout=1.0)

            # Always release server-side mutex + clear thread reference,
            # even if the on_finished callback raises. on_finished is
            # the contract that lets the server lift on_workflow without
            # a polling timer (audit §1.2 / §3.5). Wrap in try so a
            # buggy callback can't leak the thread.
            try:
                self._on_finished(terminal_phase)
            except Exception:
                pass
            self._thread = None

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

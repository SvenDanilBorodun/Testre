#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Daemon-thread runtime for Roboter Studio workflows."""

from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from physical_ai_server.workflow.interpreter import Interpreter, InterpreterError
from physical_ai_server.workflow.safety_envelope import SafetyEnvelope


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
    variables: dict[str, Any] = field(default_factory=dict)
    get_scene_frame: Callable[[], Any] | None = None
    get_gripper_frame: Callable[[], Any] | None = None
    get_current_pose_xyz: Callable[[], tuple[float, float, float] | None] | None = None


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
        get_scene_frame: Callable[[], Any] | None = None,
        get_gripper_frame: Callable[[], Any] | None = None,
        get_current_pose_xyz: Callable[[], tuple[float, float, float] | None] | None = None,
    ) -> None:
        self._publisher = publisher
        self._ik_factory = ik_factory
        self._perception_factory = perception_factory
        self._load_destinations = load_destinations or (lambda: {})
        self._load_calibration = load_calibration or (lambda: {})
        self._emit_status = emit_status or (lambda _: None)
        self._get_scene_frame = get_scene_frame
        self._get_gripper_frame = get_gripper_frame
        self._get_current_pose_xyz = get_current_pose_xyz
        self._safety = SafetyEnvelope()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._workflow_id: str | None = None
        # Persistent destinations across runs — set by mark_destination
        # callbacks in physical_ai_server.py and read into WorkflowContext
        # at start time.
        self._persisted_destinations: dict[str, dict[str, float]] = {}

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

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

    def start(self, workflow_json: str, workflow_id: str) -> tuple[bool, str]:
        with self._lock:
            if self.is_running:
                return False, 'Es läuft bereits ein Workflow.'
            try:
                interpreter = Interpreter.from_json(workflow_json)
            except InterpreterError as e:
                return False, str(e)

            self._stop_event.clear()
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
                    return False, f'IK-Solver konnte nicht initialisiert werden: {e}'

            perception_instance = None
            if self._perception_factory is not None:
                try:
                    perception_instance = self._perception_factory()
                except Exception as e:
                    return False, f'Wahrnehmung konnte nicht initialisiert werden: {e}'

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
                get_scene_frame=self._get_scene_frame,
                get_gripper_frame=self._get_gripper_frame,
                get_current_pose_xyz=self._get_current_pose_xyz,
            )

            self._thread = threading.Thread(
                target=self._run,
                args=(interpreter, ctx),
                name=f'workflow-{workflow_id}',
                daemon=True,
            )
            self._thread.start()
            return True, 'Workflow gestartet.'

    def stop(self) -> tuple[bool, str]:
        if not self.is_running:
            return True, 'Es läuft kein Workflow.'
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        return True, 'Stopp angefordert.'

    def _run(self, interpreter: Interpreter, ctx: WorkflowContext) -> None:
        try:
            self._emit_status({
                'workflow_id': self._workflow_id or '',
                'phase': 'running',
                'progress': 0.0,
                'log_message': 'Workflow läuft.',
            })
            interpreter.execute(ctx, self._on_block_change)
            if ctx.should_stop():
                self._emit_status({
                    'workflow_id': self._workflow_id or '',
                    'phase': 'stopped',
                    'progress': 1.0,
                    'log_message': 'Workflow wurde gestoppt.',
                })
            else:
                self._emit_status({
                    'workflow_id': self._workflow_id or '',
                    'phase': 'finished',
                    'progress': 1.0,
                    'log_message': 'Workflow abgeschlossen.',
                })
        except InterpreterError as e:
            self._emit_status({
                'workflow_id': self._workflow_id or '',
                'phase': 'error',
                'error': str(e),
                'log_message': str(e),
            })
        except Exception:
            self._emit_status({
                'workflow_id': self._workflow_id or '',
                'phase': 'error',
                'error': 'Interner Fehler — bitte den Lehrer rufen.',
                'log_message': traceback.format_exc(),
            })

    def _on_block_change(self, block_id: str, phase: str, progress: float) -> None:
        self._emit_status({
            'workflow_id': self._workflow_id or '',
            'current_block_id': block_id,
            'phase': phase,
            'progress': float(progress),
        })

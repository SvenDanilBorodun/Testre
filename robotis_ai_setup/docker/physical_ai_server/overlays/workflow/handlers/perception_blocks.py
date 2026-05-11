#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Perception block handlers.

Detect/count/wait blocks return values to the interpreter; they are
called via ``_eval_value`` rather than ``_exec_statement``. The
returned objects are ``Detection`` instances (or counts / booleans);
motion handlers' ``_resolve_target`` knows how to read
``world_xyz_m`` from them.
"""

from __future__ import annotations

import time
from typing import Any

from physical_ai_server.workflow.handlers.motion import WorkflowError


def _ensure_perception(ctx):
    if ctx.perception is None:
        raise WorkflowError(
            'Wahrnehmung ist nicht initialisiert — bitte zuerst die Kalibrierung abschließen.'
        )


def _attach_world_xyz(ctx, detections: list) -> list:
    """Project pixel centroids of detections to base-frame XYZ on the
    table plane and push the bounding-box list to the WorkflowStatus
    publisher (so the editor can render an overlay).

    Failures are surfaced as ``WorkflowError`` rather than swallowed —
    the motion handlers' ``_resolve_target`` would otherwise read
    ``d.world_xyz_m == None`` and raise the generic "Ziel-Wert konnte
    nicht ausgewertet werden" message instead of pointing the student
    at the missing calibration step (audit §3.1).
    """
    if not detections:
        ctx.emit_detections([])
        return detections
    if ctx.scene_intrinsics is None or ctx.scene_extrinsics is None or ctx.z_table is None:
        # Push the detections so the bbox overlay still renders, but
        # leave world_xyz_m unset; downstream motion handlers will
        # raise a clear German error if they try to act on these.
        ctx.emit_detections(detections)
        return detections
    try:
        from physical_ai_server.workflow.projection import project_pixel_to_table
        K = ctx.scene_intrinsics['K']
        dist = ctx.scene_intrinsics['dist']
        T = ctx.scene_extrinsics
        z = ctx.z_table
        for d in detections:
            cx, cy = d.centroid_px
            point = project_pixel_to_table(cx, cy, K, dist, T, z)
            if point is not None:
                d.world_xyz_m = (float(point[0]), float(point[1]), float(point[2]))
    except Exception as e:
        raise WorkflowError(
            f'Projektion fehlgeschlagen — bitte Kalibrierung prüfen: {e}'
        )
    ctx.emit_detections(detections)
    return detections


def detect_color(ctx, args: dict[str, Any]) -> list:
    _ensure_perception(ctx)
    bgr = ctx.get_scene_frame() if ctx.get_scene_frame else None
    if bgr is None:
        raise WorkflowError('Kein Szenenbild verfügbar.')
    detections = ctx.perception.detect(bgr, camera='scene', mode='color', color=args.get('color'))
    return _attach_world_xyz(ctx, detections)


def detect_object(ctx, args: dict[str, Any]) -> list:
    _ensure_perception(ctx)
    bgr = ctx.get_scene_frame() if ctx.get_scene_frame else None
    if bgr is None:
        raise WorkflowError('Kein Szenenbild verfügbar.')
    detections = ctx.perception.detect(
        bgr, camera='scene', mode='yolo+color',
        coco_class=args.get('class'),
        color=args.get('color'),
    )
    return _attach_world_xyz(ctx, detections)


def detect_marker(ctx, args: dict[str, Any]) -> list:
    _ensure_perception(ctx)
    bgr = ctx.get_scene_frame() if ctx.get_scene_frame else None
    if bgr is None:
        raise WorkflowError('Kein Szenenbild verfügbar.')
    marker_id = args.get('marker_id')
    if marker_id is not None:
        try:
            marker_id = int(marker_id)
        except (TypeError, ValueError):
            raise WorkflowError(f'Ungültige Marker-ID: {marker_id}')
    detections = ctx.perception.detect(
        bgr, camera='scene', mode='apriltag', aruco_id=marker_id,
    )
    return _attach_world_xyz(ctx, detections)


def count_color(ctx, args: dict[str, Any]) -> int:
    return len(detect_color(ctx, args))


def count_objects_class(ctx, args: dict[str, Any]) -> int:
    return len(detect_object(ctx, args))


def _poll_until(ctx, predicate, timeout_s: float, label: str) -> bool:
    """Poll ``predicate`` until it returns truthy or ``timeout_s`` elapses.

    On timeout, raises ``WorkflowError`` so the workflow halts with a
    German message — this is symmetric with the rest of the perception
    handlers and matches what students expect when a "Warte bis …"
    block sees nothing. The previous implementation had a dead
    ``on_timeout='continue'`` branch reading from a block field that
    never existed; if that affordance is wanted later, expose a
    dropdown on the wait_until_* blocks first."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if ctx.should_stop():
            raise WorkflowError('Workflow wurde gestoppt.')
        if predicate():
            return True
        time.sleep(0.2)
    raise WorkflowError(f'Timeout: {label} nicht erkannt.')


def wait_until_color(ctx, args: dict[str, Any]) -> bool:
    timeout_s = float(args.get('timeout', 10))
    color = args.get('color')
    return _poll_until(
        ctx,
        lambda: bool(detect_color(ctx, {'color': color})),
        timeout_s,
        f'Farbe {color}',
    )


def wait_until_object(ctx, args: dict[str, Any]) -> bool:
    timeout_s = float(args.get('timeout', 10))
    coco_class = args.get('class')
    return _poll_until(
        ctx,
        lambda: bool(detect_object(ctx, {'class': coco_class})),
        timeout_s,
        f'Objekt {coco_class}',
    )


def wait_until_marker(ctx, args: dict[str, Any]) -> bool:
    timeout_s = float(args.get('timeout', 10))
    marker_id = args.get('marker_id')
    return _poll_until(
        ctx,
        lambda: bool(detect_marker(ctx, {'marker_id': marker_id})),
        timeout_s,
        f'Marker {marker_id}',
    )


# Phase-3: open-vocabulary detection. Routes German prompts through a
# small synonym dict to YOLOX/D-FINE-N closed-vocab when possible
# (cheap + offline). Falls back to OWLv2 on Modal via the cloud bridge.
#
# The cloud_vision dict on ctx supplies:
#   {
#     'enabled': bool,           # default False
#     'cloud_burst': callable,   # (image_bgr, prompt) -> list[Detection]
#     'translate': dict[str,str] # German prompt -> COCO class label
#   }
# The handler asks ctx.cloud_vision['translate'] first; on miss, if
# cloud_burst is callable, posts the latest scene frame to it.
def detect_open_vocab(ctx, args: dict[str, Any]) -> list:
    _ensure_perception(ctx)
    prompt = args.get('prompt')
    if not prompt:
        raise WorkflowError('Kein Suchbegriff angegeben.')
    prompt = str(prompt).strip()
    cv = getattr(ctx, 'cloud_vision', None) or {}
    translate = (cv.get('translate') or {}) if isinstance(cv, dict) else {}
    # Audit F1: each entry is a dispatch dict
    # ``{'mode':'object'|'color', 'class':<german>, 'color':<rot|...>}``,
    # NOT a bare class string. Forwarding the whole dict to detect_object
    # crashed at ``coco_class in COCO_CLASSES`` with TypeError.
    entry = translate.get(prompt.lower())
    if isinstance(entry, dict):
        mode = entry.get('mode')
        if mode == 'object':
            return detect_object(ctx, {
                'class': entry.get('class'),
                'color': entry.get('color'),
            })
        if mode == 'color':
            return detect_color(ctx, {'color': entry.get('color')})
        # Unknown mode falls through to the cloud path — defensive only.
    elif isinstance(entry, str):
        # Legacy single-string format (older synonym dicts) — treat as
        # an object class. Kept for forward-compat if the dict moves
        # to a YAML loader.
        return detect_object(ctx, {'class': entry})
    # Cloud path. Audit F54: respect the explicit `enabled` flag —
    # decoupling "cloud_burst is bound" from "student has opted in"
    # prevents quota/cost leak once the burst is wired.
    burst = cv.get('cloud_burst') if isinstance(cv, dict) else None
    if not cv.get('enabled') or not callable(burst):
        raise WorkflowError(
            f'Begriff "{prompt}" ist lokal nicht bekannt und Cloud-Erkennung '
            'ist deaktiviert. Bitte aktivieren oder einen bekannten Begriff '
            'verwenden.'
        )
    if ctx.should_stop():
        raise WorkflowError('Workflow wurde gestoppt.')
    bgr = ctx.get_scene_frame() if ctx.get_scene_frame else None
    if bgr is None:
        raise WorkflowError('Kein Szenenbild verfügbar.')
    try:
        detections = burst(bgr, prompt)
    except WorkflowError:
        raise
    except NotImplementedError as e:
        # Audit F56: the stub used to re-wrap its own message into
        # "Cloud-Erkennung fehlgeschlagen: Cloud-Erkennung ist…". Pass
        # the original German message through.
        raise WorkflowError(str(e))
    except Exception as e:
        raise WorkflowError(f'Cloud-Erkennung fehlgeschlagen: {e}')
    return _attach_world_xyz(ctx, detections or [])

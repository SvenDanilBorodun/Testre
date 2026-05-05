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


def _poll_until(ctx, predicate, timeout_s: float, label: str, on_timeout: str) -> bool:
    """Poll ``predicate`` until it returns truthy or ``timeout_s`` elapses.

    ``on_timeout`` is one of ``'error'`` (default — raise WorkflowError
    so the workflow halts with a German message) or ``'continue'``
    (log + return False so the surrounding ``if`` block can branch).
    Audit §3.2 — the v1 ship always returned False silently, so a
    timeout looked indistinguishable from "found 0" to the next block.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if ctx.should_stop():
            raise WorkflowError('Workflow wurde gestoppt.')
        if predicate():
            return True
        time.sleep(0.2)
    msg = f'Timeout: {label} nicht erkannt.'
    if on_timeout == 'continue':
        ctx.log(msg)
        return False
    raise WorkflowError(msg)


def _on_timeout(args: dict[str, Any]) -> str:
    raw = args.get('on_timeout') or args.get('behavior') or 'error'
    return 'continue' if raw == 'continue' else 'error'


def wait_until_color(ctx, args: dict[str, Any]) -> bool:
    timeout_s = float(args.get('timeout', 10))
    color = args.get('color')
    return _poll_until(
        ctx,
        lambda: bool(detect_color(ctx, {'color': color})),
        timeout_s,
        f'Farbe {color}',
        _on_timeout(args),
    )


def wait_until_object(ctx, args: dict[str, Any]) -> bool:
    timeout_s = float(args.get('timeout', 10))
    coco_class = args.get('class')
    return _poll_until(
        ctx,
        lambda: bool(detect_object(ctx, {'class': coco_class})),
        timeout_s,
        f'Objekt {coco_class}',
        _on_timeout(args),
    )


def wait_until_marker(ctx, args: dict[str, Any]) -> bool:
    timeout_s = float(args.get('timeout', 10))
    marker_id = args.get('marker_id')
    return _poll_until(
        ctx,
        lambda: bool(detect_marker(ctx, {'marker_id': marker_id})),
        timeout_s,
        f'Marker {marker_id}',
        _on_timeout(args),
    )

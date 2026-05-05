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
    table plane. The motion handlers' resolver expects ``world_xyz_m``
    to be populated."""
    if not detections:
        return detections
    if ctx.scene_intrinsics is None or ctx.scene_extrinsics is None or ctx.z_table is None:
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
    except Exception:
        # Non-fatal — handlers downstream will raise a specific message
        # if they actually need the world coordinates.
        pass
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
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if ctx.should_stop():
            raise WorkflowError('Workflow wurde gestoppt.')
        if predicate():
            return True
        time.sleep(0.2)
    ctx.log(f'Timeout: {label} nicht erkannt.')
    return False


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

#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Pixel <-> base-frame projection on the table plane.

Uses the camera intrinsics + camera-to-base extrinsic from the scene
calibration to back-project a pixel onto the plane ``z = z_table`` and
return the resulting (x, y, z_table) point in base coordinates. This is
THE path that turns a detected cube pixel into a base-frame grasp point
for ``handlers.motion.pickup`` / ``drop_at``.

``project_base_to_pixel`` (the reverse) has NO live caller — the
gripper-cam grasp refine that would have used it was cancelled with the
WS4 scene-cam-only calibration decision (2026-06-17; the gripper camera
is not modelled in the omx_f URDF). It is retained only as a tested,
self-contained projection-math inverse for possible future use.
"""

from __future__ import annotations

import cv2
import numpy as np


def project_pixel_to_table(
    px: float,
    py: float,
    K: np.ndarray,
    dist: np.ndarray,
    T_cam_to_base: np.ndarray,
    z_table: float,
    plane: tuple[float, float, float] | None = None,
) -> np.ndarray | None:
    """Cast a ray from the camera centre through (px, py) and intersect it
    with the table plane.

    When ``plane = (a, b, c)`` is given (the touch-off measured plane
    ``z = a·x + b·y + c``), the ray is intersected with that TILTED plane —
    so a non-level table projects correctly. Otherwise the ray is intersected
    with the flat plane ``z = z_table``.

    Returns the (x, y, z) base-frame point, or ``None`` if the ray is parallel
    to the plane (camera looking along the horizon) or the intersection is
    behind the camera.
    """
    pixel = np.array([[[px, py]]], dtype=np.float32)
    undistorted = cv2.undistortPoints(pixel, K, dist).reshape(-1)

    # Ray in camera frame (homogeneous); origin at (0, 0, 0).
    direction_cam = np.array([undistorted[0], undistorted[1], 1.0])

    R = T_cam_to_base[:3, :3]
    t = T_cam_to_base[:3, 3]

    direction_base = R @ direction_cam
    origin_base = t

    if plane is not None:
        # Tilted plane a·x + b·y − z + c = 0. Solve origin + s·dir on it.
        a, b, c = (float(v) for v in plane)
        denom = a * direction_base[0] + b * direction_base[1] - direction_base[2]
        if abs(denom) < 1e-9:
            return None
        s = -(a * origin_base[0] + b * origin_base[1] - origin_base[2] + c) / denom
        if s <= 0:
            return None
        return origin_base + s * direction_base

    if abs(direction_base[2]) < 1e-9:
        return None

    s = (z_table - origin_base[2]) / direction_base[2]
    if s <= 0:
        return None

    point_base = origin_base + s * direction_base
    return np.array([point_base[0], point_base[1], z_table])


def project_base_to_pixel(
    point_base: np.ndarray,
    K: np.ndarray,
    dist: np.ndarray,
    T_cam_to_base: np.ndarray,
) -> tuple[float, float] | None:
    """Project a base-frame point back to pixel coordinates of the named
    camera. Returns ``None`` if the point is behind the camera.

    NOTE (WS4, 2026-06-17): no live caller — the gripper-cam grasp refine
    that would have used this was cancelled (scene-cam-only calibration).
    Kept as a tested projection-math inverse (see test_projection.py)."""
    R_cam_to_base = T_cam_to_base[:3, :3]
    t_cam_to_base = T_cam_to_base[:3, 3]
    R_base_to_cam = R_cam_to_base.T
    t_base_to_cam = -R_base_to_cam @ t_cam_to_base

    point_cam = R_base_to_cam @ point_base + t_base_to_cam
    if point_cam[2] <= 0:
        return None

    rvec, _ = cv2.Rodrigues(np.eye(3))
    tvec = np.zeros((3, 1))
    projected, _ = cv2.projectPoints(
        point_cam.reshape(1, 1, 3).astype(np.float32),
        rvec, tvec, K, dist,
    )
    px, py = projected.reshape(-1)
    return float(px), float(py)

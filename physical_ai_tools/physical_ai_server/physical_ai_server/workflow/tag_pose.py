#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Base-frame yaw of a flat AprilTag from its image corners (Roboter Studio
named-object grasping).

A flat tag36h11 glued to the top of a printed object gives the object's
top-down grasp orientation. This module recovers the tag's yaw about the base
**+Z** axis by **back-projecting the 4 tag corners onto the known horizontal
plane the tag lies in** (table height + object height) and reading the yaw from
the back-projected edges. Inputs:

* the 4 sub-pixel tag corners (``perception.Detection.corners_px``, pupil's
  native counter-clockwise order),
* the per-rig camera intrinsics ``K`` + distortion ``dist``,
* the scene extrinsic ``T_cam_to_base`` (4x4 — the SAME transform the table
  projection uses; ``calibration_manager`` stores it cam→base),
* the plane the tag lies in (``plane_z`` = ``board_table_z + object_height``, or
  a tilted ``plane = (a, b, c)``).

**Why this and not solvePnP.** A ~24 mm tag at the overhead distance (~0.4–0.5 m)
is *near-fronto-parallel*, so solvePnP's free tag-tilt estimate is degenerate and
biases the in-plane yaw by ~2.5–3° **even with zero pixel noise** (measured). But
we *know* the tag is flat on a calibrated horizontal plane — imposing that
constraint (corners → known plane → edge angle) removes the bad tilt DOF and
recovers yaw exactly in the same test, is insensitive to a ~1 cm plane-height
error, has no 2-solution planar ambiguity, and reuses the already-trusted
``projection.project_pixel_to_table`` ray→plane math (so orientation and grasp
position share one calibration path). Grasp *position* (x, y) keeps using that
same projection at the tag centre; grasp *z* comes from ``z_table`` + catalog
heights. Real-world yaw error is then dominated by pixel-corner noise (a corner
error δ over a ~32 px tag edge → ~δ/32 rad), addressed by multi-frame averaging.

**CONVENTION / WHAT THE RIG VALIDATES — read before trusting the sign.**
The IK self-check is POSITION-ONLY, so this orientation math has NO runtime
guard: ``test_tag_pose.py`` is its sole automated guard and the P0 rig jaw-check
is the physical guard. The yaw is read from the tag's +x edge direction
(``(c1−c0)+(c2−c3)`` in pupil corner order). pupil's corners always wrap
counter-clockwise, so the **winding is correct** — a winding mismatch would
MIRROR the yaw and is the only unrecoverable error. pupil does not document which
physical corner is index 0, but the AprilTag decoder returns corners in the
tag's canonical order (same printed corner first, regardless of how the tag is
physically rotated), so any start-corner offset is a CONSTANT multiple of 90°,
absorbed downstream by the gluing convention (tag x-axis glued to the object's
pinch axis) + ``EDUBOTICS_GRASP_ROLL_DEG`` + the rig jaw-check. **Guarantee:**
as the tag physically rotates by Δ, the returned yaw changes by Δ (correct sign,
full range); the absolute zero is rig-calibrated.

Pure NumPy + OpenCV — unit-testable without a container (the test synthesises
corners by projecting a known tag pose, validating the full chain
self-consistently; the real pupil corner order is validated on the rig).
"""

from __future__ import annotations

import logging
import math
from typing import Optional

import numpy as np

from physical_ai_server.workflow.projection import project_pixel_to_table

_logger = logging.getLogger(__name__)


def _wrap(a: float) -> float:
    """Wrap an angle to ``(-π, π]`` (matches ik_solver._wrap)."""
    return (a + math.pi) % (2.0 * math.pi) - math.pi


def tag_corner_object_points(tag_size_m: float) -> np.ndarray:
    """The 4 tag corners in the tag's own frame (x right, y up, z out of the
    tag face; origin at the tag centre), in the order this module ASSUMES for
    pupil's ``corners`` (counter-clockwise; see the module docstring)::

        0: (-s/2, +s/2, 0)   # "top-left"
        1: (+s/2, +s/2, 0)   # "top-right"
        2: (+s/2, -s/2, 0)   # "bottom-right"
        3: (-s/2, -s/2, 0)   # "bottom-left"

    The tag's +x axis runs from corner 0→1 (and 3→2); that is the direction the
    yaw is measured from. Used by the tests to synthesise corners and available
    for an edge-length sanity check (back-projected side ≈ tag_size)."""
    h = float(tag_size_m) / 2.0
    return np.array([
        [-h,  h, 0.0],
        [ h,  h, 0.0],
        [ h, -h, 0.0],
        [-h, -h, 0.0],
    ], dtype=np.float64)


def _project_corners_to_plane(
    corners_px,
    K,
    dist,
    T_cam_to_base,
    plane_z: float,
    plane: Optional[tuple[float, float, float]] = None,
) -> Optional[np.ndarray]:
    """Back-project the 4 image corners onto the tag's horizontal plane; return
    a (4, 3) array of base-frame points, or ``None`` on any failure."""
    try:
        img = np.asarray(corners_px, dtype=np.float64).reshape(4, 2)
    except (ValueError, TypeError):
        return None
    if not np.all(np.isfinite(img)):
        return None
    try:
        K = np.asarray(K, dtype=np.float64).reshape(3, 3)
        T = np.asarray(T_cam_to_base, dtype=np.float64).reshape(4, 4)
    except (ValueError, TypeError):
        return None
    dist_arr = None if dist is None else np.asarray(dist, dtype=np.float64)
    pts = np.empty((4, 3), dtype=np.float64)
    for i in range(4):
        p = project_pixel_to_table(
            float(img[i, 0]), float(img[i, 1]), K, dist_arr, T, float(plane_z),
            plane=plane,
        )
        if p is None:
            return None
        pts[i] = np.asarray(p, dtype=np.float64).reshape(3)
    return pts


def tag_yaw_base(
    corners_px,
    K,
    dist,
    T_cam_to_base,
    plane_z: float,
    plane: Optional[tuple[float, float, float]] = None,
) -> Optional[float]:
    """Yaw of the tag about the base **+Z** axis (radians, wrapped to ``(-π, π]``).

    Back-projects the 4 corners onto the tag's plane (``plane_z`` =
    ``board_table_z + object_height``, or a tilted ``plane``) via the trusted
    ``project_pixel_to_table`` and reads the yaw from the tag's +x edge
    direction. Returns ``None`` on any failure. This is the live ``tag_yaw`` in
    ``joint5 = base_yaw(x,y) − tag_yaw + GRASP_ROLL`` (see :func:`grasp_joint5`).
    """
    pts = _project_corners_to_plane(corners_px, K, dist, T_cam_to_base, plane_z, plane)
    if pts is None:
        return None
    # Tag +x ≈ average of the two "horizontal" edges (corner 0→1 and 3→2). The
    # sum is winding-invariant up to a 90° start-corner offset (absorbed on the
    # rig); only the XY components matter (the plane is ~horizontal).
    x_dir = (pts[1] - pts[0]) + (pts[2] - pts[3])
    if abs(float(x_dir[0])) < 1e-12 and abs(float(x_dir[1])) < 1e-12:
        return None
    return _wrap(math.atan2(float(x_dir[1]), float(x_dir[0])))


def tag_edge_length_base(
    corners_px,
    K,
    dist,
    T_cam_to_base,
    plane_z: float,
    plane: Optional[tuple[float, float, float]] = None,
) -> Optional[float]:
    """Mean back-projected side length (metres) of the tag quad on its plane, or
    ``None`` on failure. A sanity gate: a value far from the catalog
    ``tag_size_m`` means the plane height / extrinsic is off (wrong object on
    the pad, mis-calibration). Caller decides the tolerance."""
    pts = _project_corners_to_plane(corners_px, K, dist, T_cam_to_base, plane_z, plane)
    if pts is None:
        return None
    sides = [float(np.linalg.norm(pts[(i + 1) % 4] - pts[i])) for i in range(4)]
    return float(sum(sides) / 4.0)


def grasp_joint5(base_yaw: float, tag_yaw: float, grasp_roll_rad: float) -> float:
    """The live tool-roll (joint5) command for a top-down grasp::

        joint5 = base_yaw(x, y) − tag_yaw + GRASP_ROLL_const

    wrapped to ``(-π, π]`` (joint5's limit is ``(-π, π)`` and the pinch line is
    180°-periodic, so a valid wrist roll always exists). ``grasp_roll_rad`` is
    ``EDUBOTICS_GRASP_ROLL_DEG`` in radians (the rig-pinned jaw constant)."""
    return _wrap(float(base_yaw) - float(tag_yaw) + float(grasp_roll_rad))

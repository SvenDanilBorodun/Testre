#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""AprilTag perception for Roboter Studio named-object grasping.

The only detection mode is ``apriltag`` (``pupil_apriltags``, BSD, tag36h11
family) with an optional ``aruco_id`` filter. A printed object carries a unique
tag whose id maps — via the teacher's object catalog — to a type + grasp recipe;
the named-object blocks (``handlers/perception_blocks``) read the tag id, center,
and sub-pixel corners to recover the grasp point + tag yaw.

The earlier colour (LAB blob) and YOLOX-tiny COCO detection backends were removed
with the legacy colour/object/marker/open-vocabulary Blockly blocks (P4) — the
named-object AprilTag workflow superseded them.

The detector is constructed eagerly in ``__init__``. On any failure (missing
``pupil_apriltags``, init error) the handle stays ``None`` and ``_detect_apriltag``
returns ``[]``; the named-object blocks check ``apriltag_available()`` and raise a
precise German error AT THE BLOCK rather than silently yielding "no detections".
A scene-frame freshness gate (``handlers/perception_blocks._scene_frame``) does
the same for a stale/absent camera frame.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np


@dataclass
class Detection:
    centroid_px: tuple[int, int]
    bbox_px: tuple[int, int, int, int]   # x, y, w, h
    confidence: float
    label: str
    aruco_id: int | None = None
    world_xyz_m: tuple[float, float, float] | None = None
    # AprilTag only: the 4 tag corners in image pixels as a float (4, 2) ndarray,
    # in pupil_apriltags' native counter-clockwise order. Kept as float (the
    # detector also computes an int-cast copy for the bbox) because the
    # downstream tag-pose solvePnP (workflow/tag_pose.py) needs sub-pixel
    # corners — an int cast measurably degrades the recovered yaw of a ~24 mm tag.
    corners_px: Any | None = None
    extras: dict[str, Any] = field(default_factory=dict)


class Perception:
    """Eager-initialised wrapper over the AprilTag backend.

    If the detector can't be constructed (missing ``pupil_apriltags``), the
    handle stays ``None`` and ``_detect_apriltag`` returns an empty list at this
    level. ``apriltag_available()`` exposes that state so the workflow's
    named-object blocks (``handlers/perception_blocks``) can fail LOUD with a
    German error at the block instead of silently yielding "no detections".
    """

    def __init__(self) -> None:
        self._apriltag_detector = None
        # pupil_apriltags.Detector.detect() wraps a C library that is NOT
        # thread-safe. Perception is a SINGLE shared instance and the workflow
        # interpreter spawns one thread per hat block, so two concurrent
        # detect calls (e.g. a when_object_seen hat + the main stack) would race
        # the shared C detector → possible segfault of the whole ROS node.
        # Serialize AprilTag detection with this per-detector lock.
        self._apriltag_lock = threading.Lock()
        self._init_apriltag()

    def apriltag_available(self) -> bool:
        """True when the AprilTag marker detector loaded."""
        return self._apriltag_detector is not None

    def detect(
        self,
        bgr: np.ndarray,
        camera: str,
        mode: str,
        aruco_id: int | None = None,
    ) -> list[Detection]:
        if mode == 'apriltag':
            return self._detect_apriltag(bgr, aruco_id=aruco_id)
        return []

    # ------------------------------------------------------------------
    # AprilTag
    # ------------------------------------------------------------------
    def _init_apriltag(self) -> None:
        """Construct the pupil_apriltags detector. On failure (missing
        dependency, init error) the detector stays None and
        detect_apriltag silently returns []."""
        try:
            from pupil_apriltags import Detector
            self._apriltag_detector = Detector(
                families='tag36h11',
                nthreads=2,
                quad_decimate=1.0,
                quad_sigma=0.0,
                refine_edges=True,
                decode_sharpening=0.25,
                debug=False,
            )
        except Exception:
            self._apriltag_detector = None

    # Sub-pixel corner refinement criteria/window for the AprilTag corners.
    _SUBPIX_CRITERIA = (
        cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.01,
    )
    _SUBPIX_WIN = (5, 5)
    _SUBPIX_ZERO_ZONE = (-1, -1)

    @staticmethod
    def _refine_corners_subpix(gray: np.ndarray, corners_f: np.ndarray) -> np.ndarray:
        """Polish the float tag corners with ``cv2.cornerSubPix`` on the gray
        image. Returns the refined (4, 2) float64 corners in the SAME order;
        falls back to the raw corners on any failure or when the search window
        can't fit (tiny / image-edge tags)."""
        try:
            pts = np.asarray(corners_f, dtype=np.float64).reshape(-1, 2)
            if pts.shape[0] < 1 or not np.all(np.isfinite(pts)):
                return np.asarray(corners_f, dtype=np.float64)
            h, w = gray.shape[:2]
            win_x, win_y = Perception._SUBPIX_WIN
            # cornerSubPix reads a (2*win+1) neighbourhood around each point; if
            # any corner sits within that margin of the image border the call
            # raises — bail to the raw corners rather than crash detection.
            margin_x, margin_y = win_x + 1, win_y + 1
            if (pts[:, 0].min() < margin_x or pts[:, 0].max() > w - 1 - margin_x
                    or pts[:, 1].min() < margin_y or pts[:, 1].max() > h - 1 - margin_y):
                return pts
            corners32 = pts.astype(np.float32).reshape(-1, 1, 2)
            refined = cv2.cornerSubPix(
                gray, corners32, Perception._SUBPIX_WIN,
                Perception._SUBPIX_ZERO_ZONE, Perception._SUBPIX_CRITERIA,
            )
            out = np.asarray(refined, dtype=np.float64).reshape(-1, 2)
            if out.shape != pts.shape or not np.all(np.isfinite(out)):
                return pts
            return out
        except Exception:
            return np.asarray(corners_f, dtype=np.float64)

    def _detect_apriltag(self, bgr: np.ndarray, aruco_id: int | None) -> list[Detection]:
        if self._apriltag_detector is None:
            return []
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        # Serialize the non-thread-safe C detector across concurrent hat-block
        # threads (see __init__). Holding the lock only around .detect() keeps
        # the gray-conversion + result marshalling parallel.
        with self._apriltag_lock:
            results = self._apriltag_detector.detect(gray)
        detections: list[Detection] = []
        for r in results:
            if aruco_id is not None and r.tag_id != aruco_id:
                continue
            cx, cy = int(r.center[0]), int(r.center[1])
            # Sub-pixel float corners (pupil's native CCW order) for the
            # downstream tag-pose math; a separate int-cast copy drives the
            # bbox so the existing overlay/bbox path is byte-unchanged.
            corners_f = np.asarray(r.corners, dtype=np.float64)
            # Sub-pixel corner polish. pupil already runs refine_edges=True, but
            # a cv2.cornerSubPix pass on the gray image squeezes the corners to
            # the local intensity-gradient saddle — yaw error scales as
            # pixel_noise / tag_edge_pixels, so a fraction-of-a-pixel corner
            # improvement directly tightens the recovered wrist roll. cornerSubPix
            # PRESERVES the input point order (it refines each point in place), so
            # pupil's CCW winding is kept. Guard tags too small / against the image
            # edge where the (5,5) search window can't fit — fall back to the raw
            # detector corners on any failure.
            corners_f = self._refine_corners_subpix(gray, corners_f)
            corners_i = corners_f.astype(int)
            xs, ys = corners_i[:, 0], corners_i[:, 1]
            x, y = int(xs.min()), int(ys.min())
            w, h = int(xs.max() - xs.min()), int(ys.max() - ys.min())
            detections.append(Detection(
                centroid_px=(cx, cy),
                bbox_px=(x, y, w, h),
                confidence=float(r.decision_margin) / 100.0,
                label=f'tag{r.tag_id}',
                aruco_id=int(r.tag_id),
                corners_px=corners_f,
            ))
        return detections

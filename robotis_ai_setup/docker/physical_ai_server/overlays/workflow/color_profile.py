#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Per-classroom colour profile capture (LAB-space, audit §1.7a).

Students place a single coloured cube of each colour in the scene
camera's field of view; the manager segments the cube via Otsu
(auto-polarity, falling back to a centre ROI) and records its
LAB-space cluster centroid + per-channel standard deviation in
``color_profile.yaml``. The Perception backend matches incoming
frames against these clusters in LAB.

The v1 implementation used HSV percentiles which silently broke for
red (hue wraps at 180; the 5/95 percentiles spanned the whole wheel
and the resulting range matched every pixel). LAB is uniform and has
no wrap, so circular statistics aren't needed.

YAML schema (per colour):

    rot:
      center:    [L, a, b]                 # mean over the segmented blob
      std:       [sL, sa, sb]              # per-channel standard deviation
      threshold: 3.0                        # k * std → matches when
                                            # all-channel |x - μ| < k*std

The frontend ColorProfileStep renders ``center`` as a swatch and
warns the teacher if any of the std values is > 25 (mixed pixels;
the cube isn't isolated cleanly).
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import cv2
import numpy as np

CALIB_DIR = Path(os.environ.get('EDUBOTICS_CALIB_DIR', '/root/.cache/edubotics/calibration'))
PROFILE_PATH = CALIB_DIR / 'color_profile.yaml'

DEFAULT_COLORS = ('rot', 'gruen', 'blau', 'gelb')

# Minimum blob area in pixels before we trust an Otsu-segmented mask.
# Below this, fall back to a centre ROI.
MIN_BLOB_AREA_PX = 400

# k * std bounds for the match criterion. 3 σ on a Gaussian covers
# 99.7% of in-class pixels; in practice cubes are non-Gaussian so 3.0
# is a starting point — tune if false-positives are high in the
# classroom.
DEFAULT_THRESHOLD_K = 3.0

# Side length (in fraction of frame) of the centre-ROI fallback
# when Otsu fails. 0.25 -> middle 25% of width and height.
CENTRE_ROI_FRACTION = 0.25


class ColorProfileManager:
    """Capture and persist LAB-space colour clusters for the canonical
    classroom colours."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._captured: dict[str, dict[str, list[float] | float]] = self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load(self) -> dict:
        if not PROFILE_PATH.exists():
            return {}
        try:
            fs = cv2.FileStorage(str(PROFILE_PATH), cv2.FILE_STORAGE_READ)
            data: dict = {}
            for color in DEFAULT_COLORS:
                node = fs.getNode(color)
                if node.empty():
                    continue
                center = node.getNode('center').mat()
                std = node.getNode('std').mat()
                if center is None or std is None:
                    continue
                threshold_node = node.getNode('threshold')
                threshold = (
                    float(threshold_node.real())
                    if not threshold_node.empty()
                    else DEFAULT_THRESHOLD_K
                )
                data[color] = {
                    'center': [float(v) for v in center.flatten()],
                    'std': [float(v) for v in std.flatten()],
                    'threshold': threshold,
                }
            fs.release()
            return data
        except Exception:
            return {}

    def _persist(self) -> None:
        CALIB_DIR.mkdir(parents=True, exist_ok=True)
        fs = cv2.FileStorage(str(PROFILE_PATH), cv2.FILE_STORAGE_WRITE)
        for color, data in self._captured.items():
            fs.startWriteStruct(color, cv2.FILE_NODE_MAP)
            fs.write('center', np.asarray(data['center'], dtype=np.float32))
            fs.write('std', np.asarray(data['std'], dtype=np.float32))
            fs.write('threshold', float(data.get('threshold', DEFAULT_THRESHOLD_K)))
            fs.endWriteStruct()
        fs.release()

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------
    def capture(self, color: str, bgr: np.ndarray) -> tuple[bool, str, list[float], list[float]]:
        """Capture one cube sample. Returns (success, message, center, std).

        The ``center`` and ``std`` arrays are LAB triplets in OpenCV's
        scaled LAB convention (L: 0..255, a: 0..255 (offset 128),
        b: 0..255 (offset 128)).
        """
        if color not in DEFAULT_COLORS:
            return False, f'Unbekannte Farbe: {color}', [], []
        with self._lock:
            mask = self._segment_blob(bgr)
            if mask is None:
                return False, (
                    'Kein zusammenhängender Bereich erkannt — bitte '
                    'Würfel mittig vor die Szenen-Kamera halten.'
                ), [], []
            lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
            pixels = lab[mask > 0]
            if pixels.shape[0] < 50:
                return False, 'Bereich war zu klein.', [], []
            center = pixels.mean(axis=0).astype(np.float32)
            std = pixels.std(axis=0).astype(np.float32)
            # Floor std to a small positive value so a perfectly-uniform
            # cube doesn't make every match-test divide-by-zero. 1.0
            # in 0..255 LAB units = ~0.4% of the channel range.
            std = np.maximum(std, 1.0).astype(np.float32)
            self._captured[color] = {
                'center': [float(v) for v in center],
                'std': [float(v) for v in std],
                'threshold': DEFAULT_THRESHOLD_K,
            }
            self._persist()
            return True, f'Farbprofil für {color} gespeichert.', \
                [float(v) for v in center], [float(v) for v in std]

    def _segment_blob(self, bgr: np.ndarray) -> np.ndarray | None:
        """Return a binary mask of the dominant cube.

        Tries Otsu in both polarities, ignores contours whose bounding
        box touches the frame edge (those are the background, not the
        cube), and picks the largest of the remaining contours. Falls
        back to a centre ROI if neither polarity yields an interior
        blob.
        """
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        h, w = gray.shape[:2]

        def _is_interior(contour) -> bool:
            x, y, cw, ch = cv2.boundingRect(contour)
            return x > 0 and y > 0 and (x + cw) < w and (y + ch) < h

        best_mask: np.ndarray | None = None
        best_area = 0
        for thresh_type in (cv2.THRESH_BINARY, cv2.THRESH_BINARY_INV):
            _, thresh = cv2.threshold(
                blurred, 0, 255, thresh_type + cv2.THRESH_OTSU,
            )
            contours, _ = cv2.findContours(
                thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE,
            )
            if not contours:
                continue
            interior = [c for c in contours if _is_interior(c)]
            if not interior:
                continue
            largest = max(interior, key=cv2.contourArea)
            area = cv2.contourArea(largest)
            if area < MIN_BLOB_AREA_PX:
                continue
            if area > best_area:
                best_area = area
                m = np.zeros(gray.shape, dtype=np.uint8)
                cv2.drawContours(m, [largest], -1, 255, thickness=cv2.FILLED)
                best_mask = m

        if best_mask is not None:
            return best_mask

        # Fallback: centre ROI. The student is told to place the cube
        # centrally, so a square at the middle is a reasonable default.
        side_w = int(w * CENTRE_ROI_FRACTION)
        side_h = int(h * CENTRE_ROI_FRACTION)
        x0 = (w - side_w) // 2
        y0 = (h - side_h) // 2
        m = np.zeros(gray.shape, dtype=np.uint8)
        m[y0:y0 + side_h, x0:x0 + side_w] = 255
        return m

    # ------------------------------------------------------------------
    # Lookups for Perception
    # ------------------------------------------------------------------
    def lab_profile(self, color: str) -> dict | None:
        """Return ``{'center', 'std', 'threshold'}`` (np.ndarrays + float)
        for the given colour, or ``None`` if not captured. Used by
        ``Perception.set_color_profile``."""
        with self._lock:
            entry = self._captured.get(color)
            if entry is None:
                return None
            return {
                'center': np.asarray(entry['center'], dtype=np.float32),
                'std': np.asarray(entry['std'], dtype=np.float32),
                'threshold': float(entry.get('threshold', DEFAULT_THRESHOLD_K)),
            }

    def has_all_colors(self) -> bool:
        return all(c in self._captured for c in DEFAULT_COLORS)

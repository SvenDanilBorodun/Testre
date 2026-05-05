#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Per-classroom HSV colour profile capture.

Students place a single coloured cube of each colour in the scene camera's
field of view; the manager extracts the largest blob via Otsu background
subtraction and stores the per-channel 5/95 percentiles as the working
HSV range. The resulting `color_profile.yaml` is consumed by the perception
backend's HSV mode.
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
PERCENTILE_LOW = 5
PERCENTILE_HIGH = 95
MIN_BLOB_AREA_PX = 400


class ColorProfileManager:
    """Capture and persist HSV ranges for the canonical classroom colours."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._captured: dict[str, dict[str, list[int]]] = self._load()

    def _load(self) -> dict[str, dict[str, list[int]]]:
        if not PROFILE_PATH.exists():
            return {}
        try:
            fs = cv2.FileStorage(str(PROFILE_PATH), cv2.FILE_STORAGE_READ)
            data: dict[str, dict[str, list[int]]] = {}
            for color in DEFAULT_COLORS:
                node = fs.getNode(color)
                if node.empty():
                    continue
                lower = node.getNode('lower').mat()
                upper = node.getNode('upper').mat()
                if lower is None or upper is None:
                    continue
                data[color] = {
                    'lower': [int(v) for v in lower.flatten()],
                    'upper': [int(v) for v in upper.flatten()],
                }
            fs.release()
            return data
        except Exception:
            return {}

    def _persist(self) -> None:
        CALIB_DIR.mkdir(parents=True, exist_ok=True)
        fs = cv2.FileStorage(str(PROFILE_PATH), cv2.FILE_STORAGE_WRITE)
        for color, hsv in self._captured.items():
            fs.startWriteStruct(color, cv2.FILE_NODE_MAP)
            fs.write('lower', np.array(hsv['lower'], dtype=np.int32))
            fs.write('upper', np.array(hsv['upper'], dtype=np.int32))
            fs.endWriteStruct()
        fs.release()

    def capture(self, color: str, bgr: np.ndarray) -> tuple[bool, str]:
        if color not in DEFAULT_COLORS:
            return False, f'Unbekannte Farbe: {color}'
        with self._lock:
            mask = self._dominant_blob_mask(bgr)
            if mask is None:
                return False, (
                    'Konnte keinen passenden Bereich erkennen — bitte Würfel '
                    'mittig vor die Szenen-Kamera halten.'
                )
            hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
            pixels = hsv[mask > 0]
            if pixels.size == 0:
                return False, 'Bereich war zu klein.'
            lower = np.percentile(pixels, PERCENTILE_LOW, axis=0).astype(np.int32)
            upper = np.percentile(pixels, PERCENTILE_HIGH, axis=0).astype(np.int32)
            self._captured[color] = {
                'lower': [int(v) for v in lower],
                'upper': [int(v) for v in upper],
            }
            self._persist()
            return True, f'Farbprofil für {color} gespeichert.'

    def _dominant_blob_mask(self, bgr: np.ndarray) -> np.ndarray | None:
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        _, thresh = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None
        largest = max(contours, key=cv2.contourArea)
        if cv2.contourArea(largest) < MIN_BLOB_AREA_PX:
            return None
        mask = np.zeros(gray.shape, dtype=np.uint8)
        cv2.drawContours(mask, [largest], -1, 255, thickness=cv2.FILLED)
        return mask

    def hsv_range(self, color: str) -> tuple[np.ndarray, np.ndarray] | None:
        with self._lock:
            entry = self._captured.get(color)
            if entry is None:
                return None
            return (
                np.array(entry['lower'], dtype=np.uint8),
                np.array(entry['upper'], dtype=np.uint8),
            )

    def has_all_colors(self) -> bool:
        return all(c in self._captured for c in DEFAULT_COLORS)

#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The station zone mask (plan v6.13 §5.4): one choke point for every detection
consumer of a REAL station run.

``ZonePerception`` wraps the run's real perception. ``detect`` keeps only
detections whose tag centre, projected with the run's bound station calibration
(K, dist, ``T_cam_base``) to the table plane at that tag's catalog object height
(read from ``ctx.object_catalog`` at call time, so an object registered in code
is projected at its own height; unknown IDs at z = 0), lies inside the
commissioned accepted zone polygon grown by 10 mm. The polygon arrives already
in the BASE frame (the store converted it with the registration once), so
polygon and projection share one frame. Simulator runs are never wrapped.
"""

import numpy as np

from physical_ai_server.station import check as C
from physical_ai_server.station.commission import ZONE_GROW_M, point_in_polygon


class ZonePerception:
    def __init__(self, inner, calibration, polygon_base, get_catalog):
        self._inner = inner
        intr = calibration['scene_intrinsics']
        self._K = np.asarray(intr['K'], dtype=np.float64)
        self._dist = np.asarray(intr['dist'], dtype=np.float64)
        self._T = np.asarray(calibration['scene_extrinsics'], dtype=np.float64)
        self._z0 = float(calibration.get('board_table_z') or 0.0)
        self._poly = np.asarray(polygon_base, dtype=np.float64).reshape(-1, 2)
        self._get_catalog = get_catalog

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def _height(self, tag_id):
        catalog = self._get_catalog() if self._get_catalog else None
        if catalog is None or tag_id is None:
            return 0.0
        try:
            recipe = catalog.recipe_for_tag(int(tag_id))
        except Exception:  # noqa: BLE001 — an unknown id is projected at z = 0
            return 0.0
        return float(recipe.object_height_m) if recipe is not None else 0.0

    def _centre_px(self, det):
        corners = getattr(det, 'corners_px', None)
        if corners is not None:
            pts = np.asarray(corners, dtype=np.float64).reshape(-1, 2)
            if len(pts):
                return pts.mean(axis=0)
        return np.asarray(getattr(det, 'centroid_px', (np.nan, np.nan)), dtype=np.float64)

    def inside(self, det):
        z = self._z0 + self._height(getattr(det, 'aruco_id', None))
        xy = C.backproject(self._centre_px(det).reshape(1, 2), self._K, self._dist,
                           self._T, z=z)[0]
        if not np.all(np.isfinite(xy)):
            return False
        return point_in_polygon(xy, self._poly, ZONE_GROW_M)

    def detect(self, bgr, camera, mode, aruco_id=None, **kwargs):
        detections = self._inner.detect(bgr, camera, mode, aruco_id=aruco_id, **kwargs)
        return [d for d in detections if self.inside(d)]

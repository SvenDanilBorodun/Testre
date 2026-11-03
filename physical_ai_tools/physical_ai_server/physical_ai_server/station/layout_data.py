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

"""The printed station layouts: the one source the server and the mat generator read.

Plain literals and the stdlib only, so the mat generator can load this file by
path (``tools/generate_omx_station_mat.py``) without the server's dependencies,
and so it ships inside the package (the package installs no data files besides
``launch/`` and ``config/``). Coordinates are millimetres in the OMX base frame
(+x toward the tower, +y robot-left, +z up, z = 0 the mat top).

A layout can be commissioned only while its ``layout_state`` is ``released``;
``OMX-STATION-A`` stays ``draft`` until the measured dry fit is frozen here
(robot and tower footprints, the accepted zone polygon with >= 3 vertices).
Releasing or changing a layout changes its hash, so a station YAML made with
the old geometry stops validating.
"""

import hashlib
import json
import math

LAYOUTS = {
    "OMX-STATION-A": {
        "schema_version": 6,
        "layout_id": "OMX-STATION-A",
        "layout_state": "draft",
        "units": "mm",
        "coordinate_frame": "omx_base (x toward tower, y robot-left, z up; z=0 mat top)",
        "mat": {
            "x_range_mm": [-75, 733],
            "y_range_mm": [-300, 300],
            "thickness_mm": 3,
            "material": "rigid-pvc-matte"
        },
        "robot": {
            # The bounding box of outline_mm. The work zone and B1/B2 derive
            # from it (``_in_nominal_zone``), never from the outline.
            "footprint_mm": {
                "x_range_mm": [-60, 60],
                "y_range_mm": [-75, 75]
            },
            # What touches the mat: the bottom (z = 0) faces of the URDF base
            # mesh physical_ai_tools/physical_ai_manager/public/omx-urdf/meshes/
            # follower_01_base.stl (link0 = this frame, mm), CCW, straight CAD
            # edges. A U-shaped plate: the front cut-out toward +x, chamfered
            # corners and the rear cable channel (roofed 6 mm above the mat).
            # test/test_station_robot_outline.py holds it to the STL.
            "outline_mm": [
                [-52.0, -75.0], [52.0, -75.0], [60.0, -67.0], [60.0, -45.5],
                [52.0, -37.5], [23.0, -37.5], [15.0, -29.5], [15.0, 29.5],
                [23.0, 37.5], [52.0, 37.5], [60.0, 45.5], [60.0, 67.0],
                [52.0, 75.0], [-52.0, 75.0], [-60.0, 67.0], [-60.0, 15.0],
                [-50.0, 5.0], [-30.35, 5.0], [-29.35, 6.0], [-29.35, 7.0],
                [-30.35, 8.0], [-30.85, 8.0], [-30.85, 20.25], [-19.85, 20.25],
                [-19.85, 8.0], [-20.35, 8.0], [-21.35, 7.0], [-21.35, -7.0],
                [-20.35, -8.0], [-19.85, -8.0], [-19.85, -20.25], [-30.85, -20.25],
                [-30.85, -8.0], [-30.35, -8.0], [-29.35, -7.0], [-29.35, -6.0],
                [-30.35, -5.0], [-50.0, -5.0], [-60.0, -15.0], [-60.0, -67.0]
            ],
            # The ten mounting holes in that face (the STL's octagons are these
            # circles): Ø6.5 at x = -50, 0, 50 (counterbore Ø13 from z = 7),
            # Ø5.5 at x = ±25 (counterbore Ø10 from z = 8).
            "mount_holes_mm": [
                [-50.0, -62.5], [-50.0, 62.5], [-25.0, -62.5], [-25.0, 62.5],
                [0.0, -62.5], [0.0, 62.5], [25.0, -62.5], [25.0, 62.5],
                [50.0, -62.5], [50.0, 62.5]
            ],
            "mount_hole_diameters_mm": [6.5, 6.5, 5.5, 5.5, 6.5, 6.5, 5.5, 5.5, 6.5, 6.5],
            "measured": False
        },
        "tower": {
            "footprint_mm": {
                "x_range_mm": [555.71, 700],
                "y_range_mm": [-65, 65]
            },
            "mast_centre_mm": [600, 0],
            "measured": False
        },
        "board": {
            "squares": [7, 5],
            "square_mm": 30,
            "marker_mm": 22,
            "dictionary": "DICT_5X5_250",
            "origin_mm": [180, 75, 0],
            "axis_x": [1, 0, 0],
            "axis_y": [0, -1, 0],
            "ink_mm": {
                "x_range_mm": [180, 390],
                "y_range_mm": [-75, 75]
            },
            "white_margin_mm": {
                "x_range_mm": [165, 405],
                "y_range_mm": [-90, 90]
            }
        },
        "refs": [
            {
                "id": 100,
                "family": "tag36h11",
                "centre_mm": [-42, 262],
                "black_mm": 50,
                "quiet_mm": 6.25
            },
            {
                "id": 101,
                "family": "tag36h11",
                "centre_mm": [-42, -262],
                "black_mm": 50,
                "quiet_mm": 6.25
            },
            {
                "id": 102,
                "family": "tag36h11",
                "centre_mm": [300, 180],
                "black_mm": 50,
                "quiet_mm": 6.25
            },
            {
                "id": 103,
                "family": "tag36h11",
                "centre_mm": [300, -180],
                "black_mm": 50,
                "quiet_mm": 6.25
            }
        ],
        "station_tag_id_range": [100, 199],
        "dots": [
            {
                "id": 1,
                "centre_mm": [100, 120],
                "ring_diameter_mm": 10,
                "cross_width_mm": 1
            },
            {
                "id": 2,
                "centre_mm": [100, -120],
                "ring_diameter_mm": 10,
                "cross_width_mm": 1
            },
            {
                "id": 3,
                "centre_mm": [200, 105],
                "ring_diameter_mm": 10,
                "cross_width_mm": 1
            },
            {
                "id": 4,
                "centre_mm": [200, -105],
                "ring_diameter_mm": 10,
                "cross_width_mm": 1
            }
        ],
        "check_marks": [
            {
                "id": "M1",
                "centre_mm": [109.99, 70.0],
                "outline_mm": 30
            },
            {
                "id": "M2",
                "centre_mm": [109.99, -70.0],
                "outline_mm": 30
            },
            {
                "id": "M3",
                "centre_mm": [24.98, 135.23],
                "outline_mm": 30
            },
            {
                "id": "M4",
                "centre_mm": [24.98, -135.23],
                "outline_mm": 30
            },
            {
                "id": "M5",
                "centre_mm": [123.74, 160.87],
                "outline_mm": 30
            },
            {
                "id": "M6",
                "centre_mm": [123.74, -160.87],
                "outline_mm": 30
            },
            {
                "id": "M7",
                "centre_mm": [24.35, 201.89],
                "outline_mm": 30
            },
            {
                "id": "M8",
                "centre_mm": [24.35, -201.89],
                "outline_mm": 30
            }
        ],
        "label": {
            "text": "EduBotics OMX-Station A · Rev 1 · Marker 100–103",
            "box_mm": {
                "x_range_mm": [425, 520],
                "y_range_mm": [-60, 60]
            }
        },
        "zone": {
            "nominal_centre_mm": [-11.25, 0.0],
            "nominal_radius_mm": [100.0, 250.0],
            "nominal_bearing_deg": [-90, 90],
            "accepted_polygon_mm": [],
            "measured": False
        }
    }
}


def canonical_json(layout):
    """The layout as canonical JSON: sorted keys, no whitespace.

    The single implementation of the layout hash's input; the server and the
    mat generator both call it, never a copy.
    """
    return json.dumps(layout, sort_keys=True, separators=(',', ':'))


def layout_sha256(layout):
    """The sha256 hex digest of ``canonical_json(layout)`` (printed on the mat)."""
    return hashlib.sha256(canonical_json(layout).encode('utf-8')).hexdigest()


# ── derived geometry: one implementation for the server and the mat generator ──
def _in_nominal_zone(layout, x_mm, y_mm):
    """The nominal work zone (§2.2): the forward half-annulus about joint 1,
    minus the robot footprint grown by 10 mm."""
    zone = layout['zone']
    cx, cy = zone['nominal_centre_mm']
    r_lo, r_hi = zone['nominal_radius_mm']
    b_lo, b_hi = zone['nominal_bearing_deg']
    dx, dy = x_mm - cx, y_mm - cy
    r = (dx * dx + dy * dy) ** 0.5
    if not r_lo <= r <= r_hi:
        return False
    bearing = math.degrees(math.atan2(dy, dx))
    if not b_lo <= bearing <= b_hi:
        return False
    fp = layout['robot']['footprint_mm']
    if (fp['x_range_mm'][0] - 10 <= x_mm <= fp['x_range_mm'][1] + 10
            and fp['y_range_mm'][0] - 10 <= y_mm <= fp['y_range_mm'][1] + 10):
        return False
    return True


def board_check_squares(layout):
    """B1, B2 (§2.2): the two BLACK board squares whose centres lie inside the
    nominal work zone and are farthest apart; B1 is the one with the larger y.

    OpenCV's CharucoBoard (non-legacy) is black where (column + row) is even,
    row 0 at board y = 0; board +X = base +X, board +Y = base −Y (the layout's
    axes). Returns ``[('B1', (x, y)), ('B2', (x, y))]`` in mm (mat frame)."""
    board = layout['board']
    nx, ny = board['squares']
    s = board['square_mm']
    ox, oy, _ = board['origin_mm']
    ax, ay = board['axis_x'], board['axis_y']
    centres = []
    for j in range(ny):
        for i in range(nx):
            if (i + j) % 2:
                continue
            bx, by = (i + 0.5) * s, (j + 0.5) * s
            x = ox + bx * ax[0] + by * ay[0]
            y = oy + bx * ax[1] + by * ay[1]
            if _in_nominal_zone(layout, x, y):
                centres.append((round(x, 6), round(y, 6)))
    best = None
    for a in range(len(centres)):
        for b in range(a + 1, len(centres)):
            (x1, y1), (x2, y2) = centres[a], centres[b]
            d = (x1 - x2) ** 2 + (y1 - y2) ** 2
            if best is None or d > best[0] + 1e-9:
                best = (d, centres[a], centres[b])
    if best is None:
        return []
    p, q = sorted((best[1], best[2]), key=lambda c: (-c[1], c[0]))
    return [('B1', p), ('B2', q)]


def world_check_marks(layout):
    """M1…M8 then B1, B2 as ``[(id, (x_mm, y_mm))]`` — the 10 world-check targets."""
    marks = [(m['id'], tuple(m['centre_mm'])) for m in layout['check_marks']]
    return marks + board_check_squares(layout)

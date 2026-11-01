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
            "footprint_mm": {
                "x_range_mm": [-60, 60],
                "y_range_mm": [-75, 75]
            },
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

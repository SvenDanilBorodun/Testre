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

"""Generate the OMX camera-station mat and the portable lens board.

Station spec v6.13, §2.2 (printed layout), §6.1 (portable lens board), §10 and
§11 (generator requirements). The ONLY input is the shipped layout module
``physical_ai_server/station/layout_data.py``, loaded BY FILE PATH (it is
stdlib-only, so this tool needs none of the server's dependencies), and its
``canonical_json`` / ``layout_sha256``: the hash printed on the mat is the one
the server checks a station YAML against, never a copy of the hash function.

Outputs, all 1:1:

* ``<id>_mat.png``  — the mat at 600 dpi, 1-bit (``cv2.imwrite``);
* ``<id>_mat.pdf``  — one page of the mat's real size embedding that same
  raster as a zlib-compressed 1-bit image XObject (written by hand, stdlib);
* ``<id>_mat.svg``  — the same artwork as vector text (tags and board as their
  OpenCV-generated cells);
* ``<id>_mat_a3_trockenprobe.pdf`` — the paper dry fit: the same raster tiled
  on A3 with registration crosses on the shared tile edges, where the mat is
  blank (§2.3 step 2);
* ``<id>.layout.json`` — the layout's canonical JSON, whose sha256 is printed
  in the mat's margin;
* ``lens_board_100-116.{png,pdf,svg}`` — the portable lens board (§6.1): the
  mat board's geometry (7×5, 30/22 mm, DICT_5X5_250) with marker IDs 100–116.

Dependencies: numpy and OpenCV (``opencv-contrib-python-headless``) only. The
reference tags come from ``cv2.aruco.generateImageMarker`` (DICT_APRILTAG_36H11)
and the boards from ``CharucoBoard.generateImage``. Concept images and the old
650×700 draft (``tools/generate_omx_mat.py``) are not inputs.

Coordinates: the layout is in millimetres in the OMX base frame (+x toward the
tower, +y robot-left, z = 0 the mat top). The printed page is u = y_max − y
(rightwards), v = x_max − x (downwards), so the tower is at the top of the page
(§2.1: u = 300 − y, v = 733 − x).

Usage::

    python3 tools/generate_omx_station_mat.py --out build/station_mat
"""

import argparse
import importlib.util
import json
import math
import os
import struct
import sys
import zlib
from xml.sax.saxutils import escape

import cv2
import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAYOUT_DATA_PATH = os.path.join(
    REPO_ROOT, 'physical_ai_tools', 'physical_ai_server', 'physical_ai_server', 'station', 'layout_data.py')

DPI = 600
MM_PER_INCH = 25.4
PT_PER_INCH = 72.0

# Feature drawing (mm). The layout fixes positions and outer sizes; these are
# the line widths of the artwork.
OUTLINE_W = 0.5          # robot outline, tower footprint, world-check marks
HOLE_RING_W = 0.25       # robot mounting hole: a thin ring of the hole's diameter
HOLE_CROSS_ARM = 1.0     # ... with a centre cross of ± this
HOLE_CROSS_W = 0.2
DOT_RING_W = 0.5         # touch-dot ring (outer diameter from the layout)
DOT_CROSS_W = 0.25       # the dot's centre cross (overall size from the layout)
MARK_CROSS_ARM = 3.0     # world-check mark centre cross: ± this
MARK_CROSS_W = 0.3
MAST_CROSS_ARM = 8.0
ZONE_DASH = (6.0, 4.0)   # dashed work-zone outline: on, off
ZONE_W = 0.3
SEPARATION_MM = 5.0      # §2.2 separation rule
LABEL_GAP_MM = 3.0       # an id label's gap to its own feature
LABEL_CAP_MM = 3.5       # cap height of the id labels
LABEL_BOX_CAP_MM = 6.0   # cap height of the people label
MARGIN_CAP_MM = 2.4      # cap height of the hash line in the margin
RULER_LEN_MM = 100.0

# The A3 paper dry fit (§2.3 step 2): 3 × 3 portrait pages, 10 mm overlap.
A3_MM = (297.0, 420.0)
TILE_COLS = 3
TILE_ROWS = 3
TILE_OVERLAP_MM = 10.0
CROSS_ARM_MM = 8.0

LENS_BOARD_IDS = tuple(range(100, 117))   # §6.1: 17 markers, DICT_5X5_250
LENS_PAGE_MM = (297.0, 210.0)             # A4 landscape


# ── the layout ──────────────────────────────────────────────────────────────

def load_layout_module(path=LAYOUT_DATA_PATH):
    """``layout_data.py`` by file path (stdlib only, never the server package)."""
    spec = importlib.util.spec_from_file_location('station_layout_data', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def px_per_mm(dpi):
    return dpi / MM_PER_INCH


class MatPage:
    """Base-frame millimetres → page millimetres (u right, v down)."""

    def __init__(self, layout):
        mat = layout['mat']
        self.x0, self.x1 = (float(v) for v in mat['x_range_mm'])
        self.y0, self.y1 = (float(v) for v in mat['y_range_mm'])
        self.width_mm = self.y1 - self.y0
        self.height_mm = self.x1 - self.x0

    def uv(self, x, y):
        return self.y1 - y, self.x1 - x

    def xy(self, u, v):
        return self.x1 - v, self.y1 - u

    def box_uv(self, box):
        """A base box (xmin, xmax, ymin, ymax) → page (u0, v0, u1, v1)."""
        xmin, xmax, ymin, ymax = box
        return self.y1 - ymax, self.x1 - xmax, self.y1 - ymin, self.x1 - xmin


# ── features: what is printed, as page primitives ───────────────────────────
#
# A feature has an id, a kind, a base-frame bounding box (for the separation
# rule and the test) and a list of page primitives:
#   ('rect', u0, v0, u1, v1)                 filled black
#   ('line', u0, v0, u1, v1, w)              stroke (the dashed zone outline)
#   ('ring', uc, vc, r_out, w)               annulus, outer radius r_out
#   ('outline', [(u, v), …], w)              closed polygon outline: a band of
#                                            width w just OUTSIDE the polygon
#   ('text', u, v, text, cap, anchor)        text; anchor 'l' | 'c'; v = baseline
#   ('bitmap', u0, v0, u1, v1, cells)        a black/white cell image (0 = black)

def _frame(u0, v0, u1, v1, w):
    """A stroked rectangle as four filled bars, the stroke inside the box."""
    return [
        ('rect', u0, v0, u1, v0 + w),
        ('rect', u0, v1 - w, u1, v1),
        ('rect', u0, v0, u0 + w, v1),
        ('rect', u1 - w, v0, u1, v1),
    ]


def _cross(uc, vc, arm, w):
    return [
        ('rect', uc - arm, vc - w / 2, uc + arm, vc + w / 2),
        ('rect', uc - w / 2, vc - arm, uc + w / 2, vc + arm),
    ]


def text_width_mm(text, cap_mm):
    """The width the raster text renderer gives ``text`` (mm), for layout."""
    scale, thick = _hershey(cap_mm * 10.0)
    width_px = 0
    for kind, run in _text_runs(text):
        if kind in ('ascii', 'umlaut'):
            (w, _h), _b = cv2.getTextSize(run, cv2.FONT_HERSHEY_SIMPLEX, scale, thick)
            width_px += w
        else:
            width_px += int(round(_GLYPH_ADVANCE[kind] * cap_mm * 10.0))
    return width_px / 10.0


def board_cells(board, w_mm, h_mm):
    """The board image at an EXACT grid (3.5 px/mm: 30 mm = 105 px, the 4 mm
    marker offset = 14 px, one 22/7 mm marker cell = 11 px), so the vector
    output reproduces CharucoBoard.generateImage cell for cell."""
    k = 3.5
    return board.generateImage((int(round(w_mm * k)), int(round(h_mm * k))), marginSize=0, borderBits=1), k


def black_board_squares(layout, board):
    """The black squares of the mat board: [(i, j, x_centre, y_centre)] in base mm."""
    b = layout['board']
    sx, sy = b['squares']
    sq = float(b['square_mm'])
    img, k = board_cells(board, sx * sq, sy * sq)
    step = int(round(sq * k))
    out = []
    ox, oy = float(b['origin_mm'][0]), float(b['origin_mm'][1])
    for j in range(sy):
        for i in range(sx):
            cell = img[j * step:(j + 1) * step, i * step:(i + 1) * step]
            if float(cell.mean()) < 0.05 * 255:
                # board +X = base +x, board +Y = base −y (§2.2)
                out.append((i, j, ox + (i + 0.5) * sq, oy - (j + 0.5) * sq))
    return out


def zone_contains(layout, x, y):
    """The nominal work zone (§2.2): half-annulus about joint 1 minus the robot
    footprint grown by 10 mm."""
    z = layout['zone']
    cx, cy = (float(v) for v in z['nominal_centre_mm'])
    r0, r1 = (float(v) for v in z['nominal_radius_mm'])
    b0, b1 = (float(v) for v in z['nominal_bearing_deg'])
    dx, dy = x - cx, y - cy
    r = math.hypot(dx, dy)
    bearing = math.degrees(math.atan2(dy, dx))
    if not (r0 <= r <= r1 and b0 <= bearing <= b1):
        return False
    fx = layout['robot']['footprint_mm']['x_range_mm']
    fy = layout['robot']['footprint_mm']['y_range_mm']
    if fx[0] - 10 <= x <= fx[1] + 10 and fy[0] - 10 <= y <= fy[1] + 10:
        return False
    return True


def choose_b_squares(layout, board, layout_module):
    """B1, B2 (§2.2): ``layout_data.board_check_squares`` names them, the one
    source the server's world check reads too. This only maps each onto the
    rendered board and refuses one that does not print as a black square."""
    black = black_board_squares(layout, board)
    picked = []
    for name, (x, y) in layout_module.board_check_squares(layout):
        match = [s for s in black if abs(s[2] - x) < 1e-6 and abs(s[3] - y) < 1e-6]
        if not match:
            raise ValueError(f'{name} at ({x}, {y}) mm is not a black square of the printed board')
        picked.append(match[0])
    if len(picked) != 2:
        raise ValueError('layout_data.board_check_squares must name exactly B1 and B2')
    return picked[0], picked[1]


def _box_gap(a, b):
    """The gap (mm) between two boxes (xmin, xmax, ymin, ymax); 0 if they touch."""
    dx = max(a[0] - b[1], b[0] - a[1], 0.0)
    dy = max(a[2] - b[3], b[2] - a[3], 0.0)
    return math.hypot(dx, dy)


def _label_box_at(page, anchor_x, anchor_y, text, cap):
    """Base box of a centred label whose centre is at (anchor_x, anchor_y)."""
    w = text_width_mm(text, cap)
    # On the page the label is w wide (along u = −y) and cap tall (along v = −x).
    return (anchor_x - cap / 2, anchor_x + cap / 2, anchor_y - w / 2, anchor_y + w / 2)


def _place_label(page, feature_box, text, cap, others, candidates=None):
    """The first candidate position (base mm centre) whose label box keeps
    LABEL_GAP_MM to its own feature and SEPARATION_MM to every other one."""
    xmin, xmax, ymin, ymax = feature_box
    xc, yc = (xmin + xmax) / 2, (ymin + ymax) / 2
    w = text_width_mm(text, cap)
    if candidates is None:
        g = LABEL_GAP_MM
        candidates = [
            (xmax + g + cap / 2, yc),          # page above
            (xmin - g - cap / 2, yc),          # page below
            (xc, ymin - g - w / 2),            # page right
            (xc, ymax + g + w / 2),            # page left
        ]
    for ax, ay in candidates:
        box = _label_box_at(page, ax, ay, text, cap)
        if _box_gap(box, feature_box) < LABEL_GAP_MM - 1e-6:
            continue
        if not _inside_mat(page, box):
            continue
        if all(_box_gap(box, o) >= SEPARATION_MM - 1e-6 for o in others):
            return ax, ay, box
    raise ValueError(f'no place for the label {text!r}')


def _inside_mat(page, box, margin=2.0):
    return (box[0] >= page.x0 + margin and box[1] <= page.x1 - margin
            and box[2] >= page.y0 + margin and box[3] <= page.y1 - margin)


def _text_prims(page, ax, ay, text, cap):
    u, v = page.uv(ax, ay)
    return [('text', u, v + cap / 2, text, cap, 'c')]


def build_mat(layout, layout_module):
    """Every printed feature of the mat. Returns (page, features, meta)."""
    page = MatPage(layout)
    tag_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36H11)
    b = layout['board']
    board_dict = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, b['dictionary']))
    sx, sy = b['squares']
    sq, mk = float(b['square_mm']), float(b['marker_mm'])
    board = cv2.aruco.CharucoBoard((sx, sy), sq, mk, board_dict)
    features = []

    def add(fid, kind, box, prims, **extra):
        features.append({'id': fid, 'kind': kind, 'box': tuple(float(v) for v in box), 'prims': prims, **extra})

    # The robot: its base plate's real contact outline (layout ``outline_mm``,
    # the base mesh's bottom face) drawn as a band just outside it, so the
    # plate's edge is the band's inner edge and the line stays visible around
    # a placed robot, plus its mounting holes, each a thin ring of the hole's
    # diameter with a small centre cross. Nothing else is printed inside the
    # outline; the feature box (separation rule) is the band's outer extent.
    rob = layout['robot']
    outline = [(float(x), float(y)) for x, y in rob['outline_mm']]
    holes = [((float(x), float(y)), float(d))
             for (x, y), d in zip(rob['mount_holes_mm'], rob['mount_hole_diameters_mm'], strict=True)]
    rprims = [('outline', [page.uv(x, y) for x, y in outline], OUTLINE_W)]
    for (hx, hy), d in holes:
        u, v = page.uv(hx, hy)
        rprims += [('ring', u, v, d / 2, HOLE_RING_W)] + _cross(u, v, HOLE_CROSS_ARM, HOLE_CROSS_W)
    xs = [p[0] for p in outline]
    ys = [p[1] for p in outline]
    rbox = (min(xs) - OUTLINE_W, max(xs) + OUTLINE_W, min(ys) - OUTLINE_W, max(ys) + OUTLINE_W)
    add('robot', 'footprint', rbox, rprims, outline=outline, holes=holes)

    # The tower footprint (an outline; the base sits on it).
    ft = layout['tower']['footprint_mm']
    tbox = (ft['x_range_mm'][0], ft['x_range_mm'][1], ft['y_range_mm'][0], ft['y_range_mm'][1])
    mu, mv = page.uv(*layout['tower']['mast_centre_mm'])
    add('tower', 'footprint', tbox, _frame(*page.box_uv(tbox), OUTLINE_W) + _cross(mu, mv, MAST_CROSS_ARM, OUTLINE_W))

    # The ChArUco board (its white margin is the feature box).
    ink = b['ink_mm']
    wm = b['white_margin_mm']
    board_img, k = board_cells(board, sx * sq, sy * sq)
    bu0, bv0 = page.uv(b['origin_mm'][0], b['origin_mm'][1])  # board origin: page top-left after the turn
    # board +X = base +x = page up; board +Y = base −y = page right: the board
    # image turned counter-clockwise is the page patch (rows = v, cols = u).
    add('board', 'board',
        (wm['x_range_mm'][0], wm['x_range_mm'][1], wm['y_range_mm'][0], wm['y_range_mm'][1]),
        [('board', bu0, bv0 - sx * sq, bu0 + sy * sq, bv0, board, board_img, k)],
        ink=(ink['x_range_mm'][0], ink['x_range_mm'][1], ink['y_range_mm'][0], ink['y_range_mm'][1]))

    # The reference tags: 50 mm black edge, one cell (6.25 mm) of quiet zone.
    for ref in layout['refs']:
        cx, cy = (float(v) for v in ref['centre_mm'])
        s = float(ref['black_mm'])
        q = float(ref['quiet_mm'])
        cells = cv2.aruco.generateImageMarker(tag_dict, int(ref['id']), 8)
        u, v = page.uv(cx, cy)
        add(f"ref{ref['id']}", 'ref',
            (cx - s / 2 - q, cx + s / 2 + q, cy - s / 2 - q, cy + s / 2 + q),
            [('tag', u - s / 2, v - s / 2, u + s / 2, v + s / 2, int(ref['id']), cells)],
            tag_id=int(ref['id']), centre=(cx, cy), black_mm=s)

    # Touch dots: a ring of the layout's outer diameter and a small centre cross.
    for dot in layout['dots']:
        cx, cy = (float(v) for v in dot['centre_mm'])
        r = float(dot['ring_diameter_mm']) / 2
        u, v = page.uv(cx, cy)
        arm = float(dot['cross_width_mm']) / 2
        add(f"dot{dot['id']}", 'dot', (cx - r, cx + r, cy - r, cy + r),
            [('ring', u, v, r, DOT_RING_W)] + _cross(u, v, arm, DOT_CROSS_W),
            dot_id=int(dot['id']), centre=(cx, cy))

    # World-check marks: 30 mm outlines (a cube covers them) with a centre cross.
    for mark in layout['check_marks']:
        cx, cy = (float(v) for v in mark['centre_mm'])
        h = float(mark['outline_mm']) / 2
        mbox = (cx - h, cx + h, cy - h, cy + h)
        u, v = page.uv(cx, cy)
        add(mark['id'], 'mark', mbox,
            _frame(*page.box_uv(mbox), OUTLINE_W) + _cross(u, v, MARK_CROSS_ARM, MARK_CROSS_W),
            centre=(cx, cy))

    # The people label (§2.2): text inside its box, one line per part.
    lab = layout['label']
    lbox = (lab['box_mm']['x_range_mm'][0], lab['box_mm']['x_range_mm'][1],
            lab['box_mm']['y_range_mm'][0], lab['box_mm']['y_range_mm'][1])
    parts = [p.strip() for p in lab['text'].split('·')]
    lu0, lv0, lu1, lv1 = page.box_uv(lbox)
    line_h = LABEL_BOX_CAP_MM * 1.8
    top = (lv0 + lv1) / 2 - line_h * (len(parts) - 1) / 2
    label_prims = []
    for n, part in enumerate(parts):
        cap = LABEL_BOX_CAP_MM
        while text_width_mm(part, cap) > (lu1 - lu0) - 4 and cap > 2.0:
            cap -= 0.25
        label_prims.append(('text', (lu0 + lu1) / 2, top + n * line_h + cap / 2, part, cap, 'c'))
    add('label', 'label', lbox, label_prims, text=lab['text'])

    # B1, B2 (§2.2): board squares, so they print nothing new except an id label.
    b1, b2 = choose_b_squares(layout, board, layout_module)
    meta = {'b_squares': {'B1': b1, 'B2': b2}}

    # The margin: the layout's sha256 (and its id and state) along the top
    # edge, and two 100 mm rulers for the dry fit (§2.3).
    sha = layout_module.layout_sha256(layout)
    state = {'draft': 'Entwurf', 'released': 'freigegeben'}.get(layout.get('layout_state'), layout.get('layout_state'))
    hash_text = f"Layout {layout['layout_id']} · {state} · sha256 {sha}"
    hash_w = text_width_mm(hash_text, MARGIN_CAP_MM)
    hash_u0 = 12.0
    hash_v = 14.0
    hx, _hy = page.xy(0, hash_v)
    add('hash', 'margin',
        (hx - MARGIN_CAP_MM / 2, hx + MARGIN_CAP_MM / 2, page.y1 - (hash_u0 + hash_w), page.y1 - hash_u0),
        [('text', hash_u0, hash_v + MARGIN_CAP_MM / 2, hash_text, MARGIN_CAP_MM, 'l')],
        text=hash_text, sha256=sha)
    meta['sha256'] = sha
    meta['hash_text'] = hash_text

    ruler_prims, ruler_box = _ruler(page, 'h', u0=page.width_mm - 30.0 - RULER_LEN_MM, v=8.0)
    add('ruler_h', 'ruler', ruler_box, ruler_prims)
    ruler_prims, ruler_box = _ruler(page, 'v', u0=8.0, v=300.0)
    add('ruler_v', 'ruler', ruler_box, ruler_prims)

    # Id labels for the dots, the marks and B1/B2 (the wizard names them).
    occupied = [f['box'] for f in features]
    for f in [f for f in features if f['kind'] in ('dot', 'mark')]:
        text = str(f['dot_id']) if f['kind'] == 'dot' else f['id']
        others = [o for o in occupied if o != f['box']]
        ax, ay, box = _place_label(page, f['box'], text, LABEL_CAP_MM, others)
        add(f"label_{f['id']}", 'idlabel', box, _text_prims(page, ax, ay, text, LABEL_CAP_MM), of=f['id'])
        occupied.append(box)
    wmx0 = wm['x_range_mm'][0]
    for name, sqr in (('B1', b1), ('B2', b2)):
        # Outside the board's white margin, toward the robot, at the square's y.
        cand = [(wmx0 - SEPARATION_MM - LABEL_CAP_MM / 2 - d, sqr[3]) for d in (0.0, 4.0, 8.0)]
        sbox = (sqr[2] - sq / 2, sqr[2] + sq / 2, sqr[3] - sq / 2, sqr[3] + sq / 2)
        ax, ay, box = _place_label(page, sbox, name, LABEL_CAP_MM, list(occupied), candidates=cand)
        add(f'label_{name}', 'idlabel', box, _text_prims(page, ax, ay, name, LABEL_CAP_MM), of=name,
            square=sqr)
        occupied.append(box)

    # The work zone: the accepted polygon once released, else the nominal zone,
    # dashed, and clipped wherever it would come closer than SEPARATION_MM to
    # another feature (it crosses the board and grazes refs 100/101).
    zone_prims = _zone_dashes(layout, page, occupied)
    add('zone', 'zone', (0, 0, 0, 0), zone_prims)
    return page, features, meta


def _ruler(page, orient, u0, v):
    """A 100 mm ruler (ticks every 10 mm, longer at 0/50/100) with its label."""
    prims = []
    w = 0.3
    if orient == 'h':
        prims.append(('rect', u0, v - w / 2, u0 + RULER_LEN_MM, v + w / 2))
        for t in range(11):
            h = 4.0 if t in (0, 5, 10) else 2.0
            uu = u0 + t * 10.0
            prims.append(('rect', uu - w / 2, v - h, uu + w / 2, v))
        prims.append(('text', u0 + RULER_LEN_MM + 2.0, v + 0.5, '100 mm', 2.4, 'l'))
        u_min, u_max, v_min, v_max = u0 - w, u0 + RULER_LEN_MM + 2.0 + text_width_mm('100 mm', 2.4), v - 4.0, v + 1.5
    else:
        prims.append(('rect', u0 - w / 2, v, u0 + w / 2, v + RULER_LEN_MM))
        for t in range(11):
            h = 4.0 if t in (0, 5, 10) else 2.0
            vv = v + t * 10.0
            prims.append(('rect', u0, vv - w / 2, u0 + h, vv + w / 2))
        prims.append(('text', u0 - 1.0, v + RULER_LEN_MM + 5.0, '100 mm', 2.4, 'l'))
        u_min, u_max = u0 - 1.0, u0 + max(4.0, text_width_mm('100 mm', 2.4))
        v_min, v_max = v - w, v + RULER_LEN_MM + 5.5
    x_hi, y_hi = page.xy(u_min, v_min)
    x_lo, y_lo = page.xy(u_max, v_max)
    return prims, (x_lo, x_hi, y_lo, y_hi)


def _zone_polygons(layout, page, res=0.25):
    """The zone outline(s) as base-frame polylines (from a rasterised mask)."""
    z = layout['zone']
    poly = z.get('accepted_polygon_mm') or []
    w = int(round(page.width_mm / res))
    h = int(round(page.height_mm / res))
    mask = np.zeros((h, w), np.uint8)
    if len(poly) >= 3:
        pts = np.array([[(page.uv(px, py)[0]) / res, (page.uv(px, py)[1]) / res] for px, py in poly], np.int32)
        cv2.fillPoly(mask, [pts], 255)
    else:
        us = (np.arange(w) + 0.5) * res
        vs = (np.arange(h) + 0.5) * res
        uu, vv = np.meshgrid(us, vs)
        xx = page.x1 - vv
        yy = page.y1 - uu
        cx, cy = (float(v) for v in z['nominal_centre_mm'])
        r0, r1 = (float(v) for v in z['nominal_radius_mm'])
        b0, b1 = (float(v) for v in z['nominal_bearing_deg'])
        dx, dy = xx - cx, yy - cy
        rr = np.hypot(dx, dy)
        bb = np.degrees(np.arctan2(dy, dx))
        fx = layout['robot']['footprint_mm']['x_range_mm']
        fy = layout['robot']['footprint_mm']['y_range_mm']
        inside = (rr >= r0) & (rr <= r1) & (bb >= b0) & (bb <= b1)
        inside &= ~((xx >= fx[0] - 10) & (xx <= fx[1] + 10) & (yy >= fy[0] - 10) & (yy <= fy[1] + 10))
        mask[inside] = 255
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    out = []
    for c in contours:
        pts = c[:, 0, :].astype(float) * res
        out.append([page.xy(u, v) for u, v in pts])
    return out


def _zone_dashes(layout, page, occupied):
    prims = []
    on, off = ZONE_DASH
    for poly in _zone_polygons(layout, page):
        # Densify to ~0.5 mm steps, keep only points far enough from every feature.
        pts = []
        for i in range(len(poly)):
            x0, y0 = poly[i]
            x1, y1 = poly[(i + 1) % len(poly)]
            n = max(1, int(math.ceil(math.hypot(x1 - x0, y1 - y0) / 0.5)))
            for t in range(n):
                pts.append((x0 + (x1 - x0) * t / n, y0 + (y1 - y0) * t / n))
        clear = ZONE_W / 2 + SEPARATION_MM
        keep = [all(_box_gap((x, x, y, y), o) >= clear for o in occupied) for x, y in pts]
        walked = 0.0
        for i in range(len(pts)):
            j = (i + 1) % len(pts)
            seg = math.hypot(pts[j][0] - pts[i][0], pts[j][1] - pts[i][1])
            if keep[i] and keep[j] and (walked % (on + off)) < on:
                u0, v0 = page.uv(*pts[i])
                u1, v1 = page.uv(*pts[j])
                prims.append(('line', u0, v0, u1, v1, ZONE_W))
            walked += seg
    return prims


# ── text in the raster (Hershey, plus the three non-ASCII glyphs we print) ──

_GLYPH_ADVANCE = {'dot': 0.8, 'dash': 1.0, 'times': 0.9}
# Umlauts: the Hershey base letter with two dots above (real umlauts, Rule §1).
_UMLAUT_BASE = {'ä': 'a', 'ö': 'o', 'ü': 'u', 'Ä': 'A', 'Ö': 'O', 'Ü': 'U'}


def _text_runs(text):
    runs = []
    buf = ''
    for ch in text:
        kind = {'·': 'dot', '–': 'dash', '×': 'times'}.get(ch)
        if ch in _UMLAUT_BASE:
            if buf:
                runs.append(('ascii', buf))
                buf = ''
            runs.append(('umlaut', _UMLAUT_BASE[ch]))
        elif kind:
            if buf:
                runs.append(('ascii', buf))
                buf = ''
            runs.append((kind, ch))
        elif 32 <= ord(ch) < 127:
            buf += ch
        else:
            raise ValueError(f'the raster text cannot draw {ch!r}')
    if buf:
        runs.append(('ascii', buf))
    return runs


def _hershey(cap_px):
    (_w, h), _b = cv2.getTextSize('H', cv2.FONT_HERSHEY_SIMPLEX, 1.0, 1)
    scale = cap_px / float(h)
    thick = max(1, int(round(cap_px / 9.0)))
    return scale, thick


def draw_text(img, text, u_px, v_px, cap_px, anchor='l'):
    """Black text on a white raster; (u_px, v_px) is the baseline start (or centre)."""
    scale, thick = _hershey(cap_px)
    runs = _text_runs(text)
    widths = []
    for kind, run in runs:
        if kind in ('ascii', 'umlaut'):
            (w, _h), _b = cv2.getTextSize(run, cv2.FONT_HERSHEY_SIMPLEX, scale, thick)
            widths.append(w)
        else:
            widths.append(int(round(_GLYPH_ADVANCE[kind] * cap_px)))
    total = sum(widths)
    x = int(round(u_px - (total / 2 if anchor == 'c' else 0)))
    y = int(round(v_px))
    for (kind, run), w in zip(runs, widths):
        if kind == 'ascii':
            cv2.putText(img, run, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, 0, thick, cv2.LINE_8)
        elif kind == 'umlaut':
            cv2.putText(img, run, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, 0, thick, cv2.LINE_8)
            top = y - int(cap_px * (1.12 if run.isupper() else 0.92))
            rad = max(1, int(round(cap_px * 0.07)))
            cv2.circle(img, (x + int(w * 0.3), top), rad, 0, -1, cv2.LINE_8)
            cv2.circle(img, (x + int(w * 0.7), top), rad, 0, -1, cv2.LINE_8)
        elif kind == 'dot':
            cv2.circle(img, (x + w // 2, y - int(cap_px * 0.45)), max(1, int(round(cap_px * 0.09))), 0, -1, cv2.LINE_8)
        elif kind == 'dash':
            cv2.line(img, (x + int(w * 0.1), y - int(cap_px * 0.42)), (x + int(w * 0.9), y - int(cap_px * 0.42)),
                     0, thick, cv2.LINE_8)
        elif kind == 'times':
            a = int(cap_px * 0.3)
            cxp, cyp = x + w // 2, y - int(cap_px * 0.4)
            cv2.line(img, (cxp - a, cyp - a), (cxp + a, cyp + a), 0, thick, cv2.LINE_8)
            cv2.line(img, (cxp - a, cyp + a), (cxp + a, cyp - a), 0, thick, cv2.LINE_8)
        x += w


# ── raster ──────────────────────────────────────────────────────────────────

def _px(mm, s):
    return int(round(mm * s))


def _draw_outline(img, pts, w, s):
    """A band of width ``w`` mm just outside the closed polygon ``pts`` (page
    mm): a line of width 2w centred on the polygon (round joins), then the
    polygon's inside cleared, in a local patch so no other ink is touched."""
    band = max(1, _px(2 * w, s))
    pix = [(u * s - 0.5, v * s - 0.5) for u, v in pts]   # cv2 draws at pixel centres
    pad = band + 4
    c0 = max(0, int(math.floor(min(x for x, _ in pix))) - pad)
    r0 = max(0, int(math.floor(min(y for _, y in pix))) - pad)
    c1 = min(img.shape[1], int(math.ceil(max(x for x, _ in pix))) + pad + 1)
    r1 = min(img.shape[0], int(math.ceil(max(y for _, y in pix))) + pad + 1)
    local = np.full((r1 - r0, c1 - c0), 255, np.uint8)
    fixed = np.array([[int(round((x - c0) * 16)), int(round((y - r0) * 16))] for x, y in pix], np.int32)
    cv2.polylines(local, [fixed], True, 0, band, cv2.LINE_8, 4)
    cv2.fillPoly(local, [fixed], 255, cv2.LINE_8, 4)
    img[r0:r1, c0:c1] = np.minimum(img[r0:r1, c0:c1], local)


def render_raster(width_mm, height_mm, prims, dpi=DPI):
    """A white 1-bit-valued uint8 raster (0 black, 255 white) of the primitives."""
    s = px_per_mm(dpi)
    img = np.full((_px(height_mm, s), _px(width_mm, s)), 255, np.uint8)
    for p in prims:
        kind = p[0]
        if kind == 'rect':
            _, u0, v0, u1, v1 = p
            img[max(0, _px(v0, s)):max(0, _px(v1, s)), max(0, _px(u0, s)):max(0, _px(u1, s))] = 0
        elif kind == 'line':
            _, u0, v0, u1, v1, w = p
            cv2.line(img, (_px(u0, s), _px(v0, s)), (_px(u1, s), _px(v1, s)), 0, max(1, _px(w, s)), cv2.LINE_8)
        elif kind == 'ring':
            _, uc, vc, r_out, w = p
            thick = max(1, _px(w, s))
            r_mid = r_out * s - thick / 2.0
            cv2.circle(img, (_px(uc, s), _px(vc, s)), int(round(r_mid)), 0, thick, cv2.LINE_8)
        elif kind == 'outline':
            _, pts, w = p
            _draw_outline(img, pts, w, s)
        elif kind == 'text':
            _, u, v, text, cap, anchor = p
            draw_text(img, text, u * s, v * s, cap * s, anchor)
        elif kind == 'tag':
            _, u0, v0, u1, v1, tag_id, _cells = p
            side = _px(u1, s) - _px(u0, s)
            tag_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36H11)
            marker = cv2.aruco.generateImageMarker(tag_dict, tag_id, side)
            img[_px(v0, s):_px(v0, s) + side, _px(u0, s):_px(u0, s) + side] = marker
        elif kind == 'board':
            _, u0, v0, u1, v1, board, _cells, _k = p
            rows = _px(v1, s) - _px(v0, s)
            cols = _px(u1, s) - _px(u0, s)
            # generateImage is board X along columns: (cols = X extent = page rows).
            board_img = board.generateImage((rows, cols), marginSize=0, borderBits=1)
            patch = np.rot90(board_img, 1)
            img[_px(v0, s):_px(v0, s) + patch.shape[0], _px(u0, s):_px(u0, s) + patch.shape[1]] = patch
        else:
            raise ValueError(kind)
    return img


# ── SVG ─────────────────────────────────────────────────────────────────────

def _runs_to_rects(cells):
    """Black (0) cells of a binary image as maximal horizontal runs, then merged
    vertically where identical: [(c0, r0, c1, r1)] in cell units."""
    black = cells < 128
    rows = []
    for r in range(black.shape[0]):
        runs = []
        c = 0
        while c < black.shape[1]:
            if black[r, c]:
                c0 = c
                while c < black.shape[1] and black[r, c]:
                    c += 1
                runs.append((c0, c))
            else:
                c += 1
        rows.append(runs)
    rects = []
    open_runs = {}
    for r, runs in enumerate(rows + [[]]):
        current = set(runs)
        for run in list(open_runs):
            if run not in current:
                r0 = open_runs.pop(run)
                rects.append((run[0], r0, run[1], r))
        for run in runs:
            if run not in open_runs:
                open_runs[run] = r
    return rects


def _fmt(v):
    return f'{v:.4f}'.rstrip('0').rstrip('.')


def render_svg(width_mm, height_mm, prims, title):
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_fmt(width_mm)}mm" height="{_fmt(height_mm)}mm" '
        f'viewBox="0 0 {_fmt(width_mm)} {_fmt(height_mm)}">',
        f'<title>{escape(title)}</title>',
        f'<rect x="0" y="0" width="{_fmt(width_mm)}" height="{_fmt(height_mm)}" fill="#fff"/>',
    ]
    for p in prims:
        kind = p[0]
        if kind == 'rect':
            _, u0, v0, u1, v1 = p
            out.append(f'<rect x="{_fmt(u0)}" y="{_fmt(v0)}" width="{_fmt(u1 - u0)}" height="{_fmt(v1 - v0)}"/>')
        elif kind == 'line':
            _, u0, v0, u1, v1, w = p
            out.append(f'<line x1="{_fmt(u0)}" y1="{_fmt(v0)}" x2="{_fmt(u1)}" y2="{_fmt(v1)}" '
                       f'stroke="#000" stroke-width="{_fmt(w)}"/>')
        elif kind == 'ring':
            _, uc, vc, r_out, w = p
            out.append(f'<circle cx="{_fmt(uc)}" cy="{_fmt(vc)}" r="{_fmt(r_out - w / 2)}" fill="none" '
                       f'stroke="#000" stroke-width="{_fmt(w)}"/>')
        elif kind == 'outline':
            # A 2w line centred on the polygon, clipped to the outside: the
            # raster's band of width w just outside the outline.
            _, pts, w = p
            clip_id = f'outside{len(out)}'
            ring = ' '.join(f'{_fmt(u)},{_fmt(v)}' for u, v in pts)
            path = 'M' + ' L'.join(f'{_fmt(u)} {_fmt(v)}' for u, v in pts) + ' Z'
            out.append(f'<clipPath id="{clip_id}"><path clip-rule="evenodd" '
                       f'd="M0 0 H{_fmt(width_mm)} V{_fmt(height_mm)} H0 Z {path}"/></clipPath>')
            out.append(f'<polygon points="{ring}" fill="none" stroke="#000" stroke-width="{_fmt(2 * w)}" '
                       f'stroke-linejoin="round" clip-path="url(#{clip_id})"/>')
        elif kind == 'text':
            _, u, v, text, cap, anchor = p
            out.append(f'<text x="{_fmt(u)}" y="{_fmt(v)}" font-family="Helvetica, Arial, sans-serif" '
                       f'font-size="{_fmt(cap / 0.72)}" text-anchor="{"middle" if anchor == "c" else "start"}">'
                       f'{escape(text)}</text>')
        elif kind == 'tag':
            _, u0, v0, u1, v1, tag_id, cells = p
            cell = (u1 - u0) / cells.shape[1]
            out.append(f'<g id="tag{tag_id}" transform="translate({_fmt(u0)} {_fmt(v0)}) scale({_fmt(cell)})">')
            for c0, r0, c1, r1 in _runs_to_rects(cells):
                out.append(f'<rect x="{c0}" y="{r0}" width="{c1 - c0}" height="{r1 - r0}"/>')
            out.append('</g>')
        elif kind == 'board':
            _, u0, v0, u1, v1, _board, cells, k = p
            # The board image at 3.5 px/mm; turned so board +X = page up.
            turned = np.rot90(cells, 1)
            out.append(f'<g id="board" transform="translate({_fmt(u0)} {_fmt(v0)}) scale({_fmt(1.0 / k)})">')
            for c0, r0, c1, r1 in _runs_to_rects(turned):
                out.append(f'<rect x="{c0}" y="{r0}" width="{c1 - c0}" height="{r1 - r0}"/>')
            out.append('</g>')
        else:
            raise ValueError(kind)
    out.append('</svg>')
    return '\n'.join(out) + '\n'


# ── PNG and PDF (by hand, stdlib) ───────────────────────────────────────────

def write_png(path, img, dpi=DPI):
    """1-bit PNG via cv2.imwrite, plus a pHYs chunk so viewers know the dpi."""
    ok, buf = cv2.imencode('.png', img, [cv2.IMWRITE_PNG_BILEVEL, 1])
    if not ok:
        raise RuntimeError('PNG encoding failed')
    data = buf.tobytes()
    ppm = int(round(dpi / MM_PER_INCH * 1000.0))
    body = struct.pack('>IIB', ppm, ppm, 1)
    chunk = struct.pack('>I', len(body)) + b'pHYs' + body + struct.pack('>I', zlib.crc32(b'pHYs' + body) & 0xFFFFFFFF)
    ihdr_end = 8 + 4 + 4 + 13 + 4
    with open(path, 'wb') as f:
        f.write(data[:ihdr_end] + chunk + data[ihdr_end:])


def pack_1bit(img):
    """0 → bit 0 (black), anything else → bit 1 (white); rows padded to a byte."""
    return np.packbits(img >= 128, axis=1).tobytes()


def write_pdf(path, pages, dpi=DPI):
    """A PDF whose every page is one image XObject (1-bit, FlateDecode) filling
    the page at its real size. ``pages``: [(width_mm, height_mm, raster)]."""
    objs = {}
    page_ids = []
    n = 3
    for width_mm, height_mm, raster in pages:
        page_id, img_id, content_id = n, n + 1, n + 2
        n += 3
        page_ids.append(page_id)
        w_pt = width_mm / MM_PER_INCH * PT_PER_INCH
        h_pt = height_mm / MM_PER_INCH * PT_PER_INCH
        data = zlib.compress(pack_1bit(raster), 6)
        objs[img_id] = (
            f'<< /Type /XObject /Subtype /Image /Width {raster.shape[1]} /Height {raster.shape[0]} '
            f'/ColorSpace /DeviceGray /BitsPerComponent 1 /Filter /FlateDecode /Length {len(data)} >>'
        ).encode('ascii'), data
        content = f'q {w_pt:.4f} 0 0 {h_pt:.4f} 0 0 cm /Im0 Do Q'.encode('ascii')
        objs[content_id] = f'<< /Length {len(content)} >>'.encode('ascii'), content
        objs[page_id] = (
            f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w_pt:.4f} {h_pt:.4f}] '
            f'/Resources << /XObject << /Im0 {img_id} 0 R >> >> /Contents {content_id} 0 R >>'
        ).encode('ascii'), None
    objs[1] = b'<< /Type /Catalog /Pages 2 0 R >>', None
    kids = ' '.join(f'{p} 0 R' for p in page_ids)
    objs[2] = f'<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>'.encode('ascii'), None
    out = bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
    offsets = {}
    for oid in sorted(objs):
        head, stream = objs[oid]
        offsets[oid] = len(out)
        out += f'{oid} 0 obj\n'.encode('ascii') + head
        if stream is not None:
            out += b'\nstream\n' + stream + b'\nendstream'
        out += b'\nendobj\n'
    xref = len(out)
    out += f'xref\n0 {len(objs) + 1}\n'.encode('ascii')
    out += b'0000000000 65535 f \n'
    for oid in range(1, len(objs) + 1):
        out += f'{offsets[oid]:010d} 00000 n \n'.encode('ascii')
    out += f'trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode('ascii')
    with open(path, 'wb') as f:
        f.write(bytes(out))


def read_pdf_images(path):
    """The 1-bit images of a PDF this module wrote, unpacked to 0/255 uint8."""
    data = open(path, 'rb').read()
    images = []
    pos = 0
    while True:
        at = data.find(b'/Subtype /Image', pos)
        if at < 0:
            break
        dict_start = data.rfind(b'<<', 0, at)
        dict_end = data.find(b'>>', at)
        head = data[dict_start:dict_end].decode('ascii')
        fields = head.replace('<<', ' ').split()
        w = int(fields[fields.index('/Width') + 1])
        h = int(fields[fields.index('/Height') + 1])
        length = int(fields[fields.index('/Length') + 1])
        s = data.find(b'stream\n', dict_end) + len(b'stream\n')
        raw = zlib.decompress(data[s:s + length])
        bits = np.unpackbits(np.frombuffer(raw, np.uint8).reshape(h, -1), axis=1)[:, :w]
        images.append(np.where(bits > 0, 255, 0).astype(np.uint8))
        pos = s + length
    return images


# ── the A3 paper dry fit ────────────────────────────────────────────────────

def registration_points(mat_raster, page, dpi=DPI):
    """Where the dry-fit tiles carry their registration crosses: on every edge
    two neighbouring tiles share, two points (searched from a quarter and three
    quarters along the edge) whose CLEAR_MM neighbourhood of the mat is blank,
    so a cross never covers artwork. The same mat points on both tiles.
    Returns [(u, v)] in mat page mm."""
    s = px_per_mm(dpi)
    clear = CROSS_ARM_MM + 4.0
    su = page.width_mm / TILE_COLS
    sv = page.height_mm / TILE_ROWS

    def blank(u, v):
        u0, u1 = _px(u - clear, s), _px(u + clear, s)
        v0, v1 = _px(v - clear, s), _px(v + clear, s)
        if u0 < 0 or v0 < 0 or u1 > mat_raster.shape[1] or v1 > mat_raster.shape[0]:
            return False
        return bool((mat_raster[v0:v1, u0:u1] >= 128).all())

    def along(edge_points):
        found = []
        for target in (0.25, 0.75):
            n = len(edge_points)
            order = sorted(range(n), key=lambda i: abs(i / max(1, n - 1) - target))
            for i in order:
                u, v = edge_points[i]
                if blank(u, v) and all(math.hypot(u - a, v - b) > 4 * clear for a, b in found):
                    found.append((u, v))
                    break
        return found

    points = []
    for c in range(1, TILE_COLS):          # vertical edges between columns
        for r in range(TILE_ROWS):
            edge = [(c * su, r * sv + t) for t in np.arange(clear, sv - clear, 1.0)]
            points += along(edge)
    for r in range(1, TILE_ROWS):          # horizontal edges between rows
        for c in range(TILE_COLS):
            edge = [(c * su + t, r * sv) for t in np.arange(clear, su - clear, 1.0)]
            points += along(edge)
    return points


def a3_tiles(mat_raster, page, caption, dpi=DPI):
    """The mat raster on 3 × 3 A3 portrait pages, each tile with 10 mm overlap.
    Registration crosses sit on the edges two tiles share, where the mat is
    blank (registration_points): the same mat point on both tiles."""
    s = px_per_mm(dpi)
    a3w, a3h = A3_MM
    su = page.width_mm / TILE_COLS
    sv = page.height_mm / TILE_ROWS
    cw, ch = su + 2 * TILE_OVERLAP_MM, sv + 2 * TILE_OVERLAP_MM
    off_u, off_v = (a3w - cw) / 2, (a3h - ch) / 2
    marks = registration_points(mat_raster, page, dpi)
    pages = []
    for r in range(TILE_ROWS):
        for c in range(TILE_COLS):
            img = np.full((_px(a3h, s), _px(a3w, s)), 255, np.uint8)
            # mat region shown (page mm), clipped to the mat
            mu0 = c * su - TILE_OVERLAP_MM
            mv0 = r * sv - TILE_OVERLAP_MM
            src_u0, src_v0 = max(0.0, mu0), max(0.0, mv0)
            src_u1 = min(page.width_mm, mu0 + cw)
            src_v1 = min(page.height_mm, mv0 + ch)
            su0, sv0, su1, sv1 = _px(src_u0, s), _px(src_v0, s), _px(src_u1, s), _px(src_v1, s)
            du0 = _px(off_u + (src_u0 - mu0), s)
            dv0 = _px(off_v + (src_v0 - mv0), s)
            patch = mat_raster[sv0:sv1, su0:su1]
            img[dv0:dv0 + patch.shape[0], du0:du0 + patch.shape[1]] = patch
            prims = []
            for mu, mv in marks:
                if mu0 <= mu <= mu0 + cw and mv0 <= mv <= mv0 + ch:
                    pu, pv = off_u + (mu - mu0), off_v + (mv - mv0)
                    prims += _cross(pu, pv, CROSS_ARM_MM, 0.3)
                    prims.append(('ring', pu, pv, 3.0, 0.3))
            prims.append(('text', a3w / 2, a3h - 4.0,
                          f'{caption} · Zeile {r + 1}/{TILE_ROWS} · Spalte {c + 1}/{TILE_COLS} · 1:1', 3.0, 'c'))
            img = np.minimum(img, render_raster(a3w, a3h, prims, dpi))
            pages.append((a3w, a3h, img))
    return pages


# ── the lens board (§6.1) ───────────────────────────────────────────────────

def build_lens_board(layout):
    """The portable lens board: the mat board's geometry with marker IDs 100–116,
    centred on an A4 landscape page with its label."""
    b = layout['board']
    sx, sy = b['squares']
    sq, mk = float(b['square_mm']), float(b['marker_mm'])
    d = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, b['dictionary']))
    board = cv2.aruco.CharucoBoard((sx, sy), sq, mk, d, np.array(LENS_BOARD_IDS))
    pw, ph = LENS_PAGE_MM
    bw, bh = sx * sq, sy * sq
    u0, v0 = (pw - bw) / 2, (ph - bh) / 2 - 8.0
    cells, k = board_cells(board, bw, bh)
    prims = [('lensboard', u0, v0, u0 + bw, v0 + bh, board, cells, k)]
    prims.append(('text', pw / 2, v0 + bh + 14.0,
                  f'EduBotics Linsen-Tafel · ChArUco {sx}×{sy} · {int(sq)}/{int(mk)} mm · '
                  f'{b["dictionary"]} · Marker {LENS_BOARD_IDS[0]}–{LENS_BOARD_IDS[-1]}', 4.0, 'c'))
    prims.append(('text', pw / 2, v0 + bh + 22.0,
                  'Nur für die Linsen-Kalibrierung (Station einrichten, Schritt Linse). Flach und steif aufziehen.',
                  2.8, 'c'))
    return board, (pw, ph), prims, (u0, v0)


def render_lens_raster(page_mm, prims, dpi=DPI):
    s = px_per_mm(dpi)
    rest = []
    img = None
    for p in prims:
        if p[0] == 'lensboard':
            _, u0, v0, u1, v1, board, _cells, _k = p
            img = render_raster(page_mm[0], page_mm[1], [], dpi) if img is None else img
            cols = _px(u1, s) - _px(u0, s)
            rows = _px(v1, s) - _px(v0, s)
            board_img = board.generateImage((cols, rows), marginSize=0, borderBits=1)
            img[_px(v0, s):_px(v0, s) + rows, _px(u0, s):_px(u0, s) + cols] = board_img
        else:
            rest.append(p)
    return np.minimum(img, render_raster(page_mm[0], page_mm[1], rest, dpi))


def lens_svg(page_mm, prims, title):
    svg_prims = []
    for p in prims:
        if p[0] == 'lensboard':
            _, u0, v0, u1, v1, board, cells, k = p
            # Upright (board +X = page right): no turn for the portable board.
            svg_prims.append(('lensboard_svg', u0, v0, cells, k))
        else:
            svg_prims.append(p)
    text = render_svg(page_mm[0], page_mm[1], [p for p in svg_prims if p[0] != 'lensboard_svg'], title)
    extra = []
    for p in svg_prims:
        if p[0] == 'lensboard_svg':
            _, u0, v0, cells, k = p
            extra.append(f'<g id="lensboard" transform="translate({_fmt(u0)} {_fmt(v0)}) scale({_fmt(1.0 / k)})">')
            for c0, r0, c1, r1 in _runs_to_rects(cells):
                extra.append(f'<rect x="{c0}" y="{r0}" width="{c1 - c0}" height="{r1 - r0}"/>')
            extra.append('</g>')
    return text.replace('</svg>\n', '\n'.join(extra) + '\n</svg>\n')


# ── the whole run ───────────────────────────────────────────────────────────

def all_prims(features):
    out = []
    for f in features:
        out.extend(f['prims'])
    return out


def separation_violations(features):
    """Pairs of distinct printed features closer than SEPARATION_MM (§2.2). An id
    label is checked against everything but its own feature, at LABEL_GAP_MM to
    that one; the zone outline is clipped by construction and has no box."""
    out = []
    boxed = [f for f in features if f['kind'] != 'zone']
    for i in range(len(boxed)):
        for j in range(i + 1, len(boxed)):
            a, b = boxed[i], boxed[j]
            own = a.get('of') == b['id'] or b.get('of') == a['id']
            need = LABEL_GAP_MM if own else SEPARATION_MM
            gap = _box_gap(a['box'], b['box'])
            if gap < need - 1e-6:
                out.append((a['id'], b['id'], gap))
    return out


def generate(out_dir, layout_id='OMX-STATION-A', dpi=DPI, tiles=True, lens=True, layout_path=LAYOUT_DATA_PATH):
    """Write every output; returns a dict of paths and facts the test reads."""
    module = load_layout_module(layout_path)
    if layout_id not in module.LAYOUTS:
        raise KeyError(f'unknown layout {layout_id!r}')
    layout = module.LAYOUTS[layout_id]
    os.makedirs(out_dir, exist_ok=True)
    page, features, meta = build_mat(layout, module)
    bad = separation_violations(features)
    if bad:
        raise ValueError(f'separation rule violated: {bad}')
    prims = all_prims(features)
    base = os.path.join(out_dir, f'{layout_id}_mat')
    raster = render_raster(page.width_mm, page.height_mm, prims, dpi)
    write_png(base + '.png', raster, dpi)
    write_pdf(base + '.pdf', [(page.width_mm, page.height_mm, raster)], dpi)
    with open(base + '.svg', 'w', encoding='utf-8') as f:
        f.write(render_svg(page.width_mm, page.height_mm, prims, f'EduBotics {layout_id}'))
    layout_json = os.path.join(out_dir, f'{layout_id}.layout.json')
    with open(layout_json, 'w', encoding='utf-8') as f:
        f.write(module.canonical_json(layout))
    result = {
        'layout': layout, 'page': page, 'features': features, 'meta': meta, 'dpi': dpi,
        'png': base + '.png', 'pdf': base + '.pdf', 'svg': base + '.svg', 'layout_json': layout_json,
    }
    if tiles:
        tile_pages = a3_tiles(raster, page, f'Trockenprobe {layout_id} · sha256 {meta["sha256"][:16]}', dpi)
        result['a3_pdf'] = base + '_a3_trockenprobe.pdf'
        write_pdf(result['a3_pdf'], tile_pages, dpi)
        result['a3_pages'] = len(tile_pages)
    del raster
    if lens:
        board, page_mm, lprims, origin = build_lens_board(layout)
        lraster = render_lens_raster(page_mm, lprims, dpi)
        lbase = os.path.join(out_dir, f'lens_board_{LENS_BOARD_IDS[0]}-{LENS_BOARD_IDS[-1]}')
        write_png(lbase + '.png', lraster, dpi)
        write_pdf(lbase + '.pdf', [(page_mm[0], page_mm[1], lraster)], dpi)
        with open(lbase + '.svg', 'w', encoding='utf-8') as f:
            f.write(lens_svg(page_mm, lprims, 'EduBotics Linsen-Tafel 100-116'))
        result.update({
            'lens_png': lbase + '.png', 'lens_pdf': lbase + '.pdf', 'lens_svg': lbase + '.svg',
            'lens_page_mm': page_mm, 'lens_origin_mm': origin,
        })
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--out', required=True, help='output directory')
    parser.add_argument('--layout', default='OMX-STATION-A', help='layout id in layout_data.py')
    parser.add_argument('--dpi', type=int, default=DPI)
    parser.add_argument('--no-tiles', action='store_true', help='skip the A3 paper dry fit')
    parser.add_argument('--no-lens', action='store_true', help='skip the portable lens board')
    args = parser.parse_args(argv)
    res = generate(args.out, args.layout, args.dpi, tiles=not args.no_tiles, lens=not args.no_lens)
    print(json.dumps({
        'layout': args.layout,
        'state': res['layout'].get('layout_state'),
        'sha256': res['meta']['sha256'],
        'files': [res[k] for k in ('png', 'pdf', 'svg', 'layout_json', 'a3_pdf', 'lens_png', 'lens_pdf', 'lens_svg')
                  if k in res],
    }, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())

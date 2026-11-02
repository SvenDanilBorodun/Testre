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

"""The camera-station mat generator decodes its own output (station spec v6.13,
§2.2, §6.1, §10, §11 „Generator requirements (WP-C)").

``tools/generate_omx_station_mat.py`` is run once at 600 dpi into a temporary
directory. Then, from the FILES it wrote (never from its in-memory state):

* the PNG decodes, and the PDF's one embedded image is pixel-for-pixel the PNG;
  the PDF page is the mat's real size;
* all four reference tags are detected (DICT_APRILTAG_36H11), every corner
  within ±0.2 mm of nominal in the §7.1 canonical order;
* all 24 board corners are detected, each within ±0.2 mm of nominal, and the
  portable lens board's detector finds none on the mat;
* every touch dot and world-check mark is printed within ±0.2 mm of nominal;
* the robot is its base plate's contact outline (layout ``outline_mm``), a
  0.5 mm band hugging it from outside within 0.1 mm, and its ten mounting holes
  (centre and diameter within ±0.2 mm); nothing else is printed inside the
  outline (``test_station_robot_outline.py`` holds the outline to the STL);
* the hash printed in the margin is ``layout_data.layout_sha256`` of the
  layout, read from layout_data.py by file path, and the layout JSON written
  beside it is ``layout_data.canonical_json``;
* B1 / B2 are the two black board squares inside the work zone farthest apart;
* the separation rule holds for every printed feature;
* the lens board carries IDs 100–116, its 24 corners are detected at nominal
  positions, and the fixed-board detector finds none on it;
* the SVG reproduces the tags and the board cell for cell, and the A3 dry fit
  is the same raster tiled, its registration crosses on blank mat only.

Owned by WP-C (§10), in WP-B's test directory so the server pytest job runs it.
"""

import ast
import importlib.util
import json
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET

import cv2
import numpy as np
import pytest

_REPO = Path(__file__).resolve().parents[3]
_GENERATOR = _REPO / 'tools' / 'generate_omx_station_mat.py'
_LAYOUT_DATA = _REPO / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server' / 'station' / 'layout_data.py'
_TOL_MM = 0.2
_DOWN = 4  # detection on the 600 dpi raster reduced 4 × (150 dpi)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def gen():
    return _load('generate_omx_station_mat', _GENERATOR)


@pytest.fixture(scope='module')
def layout_data():
    return _load('layout_data_for_test', _LAYOUT_DATA)


@pytest.fixture(scope='module')
def out(gen, tmp_path_factory):
    return gen.generate(str(tmp_path_factory.mktemp('station_mat')), 'OMX-STATION-A', dpi=600)


@pytest.fixture(scope='module')
def mat_png(out):
    img = cv2.imread(out['png'], cv2.IMREAD_GRAYSCALE)
    assert img is not None
    return img


@pytest.fixture(scope='module')
def mat_small(mat_png):
    h, w = mat_png.shape
    crop = mat_png[: h - h % _DOWN, : w - w % _DOWN]
    return cv2.resize(crop, (crop.shape[1] // _DOWN, crop.shape[0] // _DOWN), interpolation=cv2.INTER_AREA)


def _s(dpi=600):
    return dpi / 25.4


def _small_to_page_mm(xs, ys):
    """A pixel-centre coordinate of the reduced image → page millimetres."""
    s = _s()
    return (_DOWN * xs + _DOWN / 2) / s, (_DOWN * ys + _DOWN / 2) / s


def _page_to_base(out, u, v):
    page = out['page']
    return page.x1 - v, page.y1 - u


# ── files ───────────────────────────────────────────────────────────────────

def test_the_png_is_600_dpi_and_the_pdf_embeds_exactly_it(gen, out, mat_png):
    page = out['page']
    s = _s()
    assert mat_png.shape == (round(page.height_mm * s), round(page.width_mm * s))
    assert set(np.unique(mat_png).tolist()) <= {0, 255}
    images = gen.read_pdf_images(out['pdf'])
    assert len(images) == 1
    assert images[0].shape == mat_png.shape
    assert np.array_equal(images[0], mat_png)
    pdf = open(out['pdf'], 'rb').read().decode('latin-1')
    box = [float(v) for v in re.search(r'/MediaBox \[0 0 ([\d.]+) ([\d.]+)\]', pdf).groups()]
    assert box[0] == pytest.approx(page.width_mm / 25.4 * 72, abs=0.01)
    assert box[1] == pytest.approx(page.height_mm / 25.4 * 72, abs=0.01)
    assert page.width_mm == 600 and page.height_mm == 808


def test_the_png_says_600_dpi(out):
    data = open(out['png'], 'rb').read()
    at = data.find(b'pHYs')
    assert at > 0
    ppm_x, ppm_y, unit = __import__('struct').unpack('>IIB', data[at + 4: at + 13])
    assert unit == 1 and ppm_x == ppm_y == round(600 / 25.4 * 1000)


def test_the_printed_hash_is_layout_data_s_own(out, layout_data):
    layout = layout_data.LAYOUTS['OMX-STATION-A']
    sha = layout_data.layout_sha256(layout)
    assert out['meta']['sha256'] == sha
    assert sha in out['meta']['hash_text']
    assert 'OMX-STATION-A' in out['meta']['hash_text']
    assert open(out['layout_json'], encoding='utf-8').read() == layout_data.canonical_json(layout)
    assert json.loads(open(out['layout_json'], encoding='utf-8').read()) == layout


def test_the_generator_reads_layout_data_by_path_and_never_hashes_itself(gen):
    src = _GENERATOR.read_text(encoding='utf-8')
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split('.')[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or '').split('.')[0])
    # numpy and OpenCV only, besides the stdlib (§10); no hash of its own.
    assert imported <= {
        'argparse', 'importlib', 'json', 'math', 'os', 'struct', 'sys', 'zlib', 'xml', 'cv2', 'numpy',
    }, imported
    assert 'hashlib' not in imported
    assert 'physical_ai_server' not in imported
    assert Path(gen.LAYOUT_DATA_PATH) == _LAYOUT_DATA
    assert 'layout_sha256(' in src and 'canonical_json(' in src


# ── the reference tags ──────────────────────────────────────────────────────

def test_all_four_references_are_detected_at_their_nominal_corners(out, mat_small):
    params = cv2.aruco.DetectorParameters()
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    det = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36H11), params)
    corners, ids, _ = det.detectMarkers(mat_small)
    assert ids is not None
    found = {int(i): c.reshape(4, 2) for i, c in zip(ids.ravel(), corners)}
    refs = {r['id']: r for r in out['layout']['refs']}
    assert set(found) == set(refs)
    worst = 0.0
    for rid, quad in found.items():
        cx, cy = refs[rid]['centre_mm']
        h = refs[rid]['black_mm'] / 2
        # §7.1: OpenCV's order is the printed page TL, TR, BR, BL.
        expected = [(cx + h, cy + h), (cx + h, cy - h), (cx - h, cy - h), (cx - h, cy + h)]
        for (xs, ys), (ex, ey) in zip(quad, expected):
            x, y = _page_to_base(out, *_small_to_page_mm(xs, ys))
            err = math.hypot(x - ex, y - ey)
            worst = max(worst, err)
            assert err <= _TOL_MM, (rid, (x, y), (ex, ey), err)
    assert worst > 0.0


# ── the board ───────────────────────────────────────────────────────────────

def _board(layout, ids=None):
    b = layout['board']
    d = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, b['dictionary']))
    args = (tuple(b['squares']), float(b['square_mm']), float(b['marker_mm']), d)
    return cv2.aruco.CharucoBoard(*args, np.array(ids)) if ids is not None else cv2.aruco.CharucoBoard(*args)


def _charuco(board, img):
    det = cv2.aruco.CharucoDetector(board)
    corners, cids, _mc, _mi = det.detectBoard(img)
    if cids is None:
        return np.zeros((0, 2)), np.zeros((0,), int)
    return corners.reshape(-1, 2), cids.ravel()


def test_all_24_board_corners_are_detected_within_tolerance(out, mat_small):
    layout = out['layout']
    board = _board(layout)
    corners, cids = _charuco(board, mat_small)
    assert len(cids) == 24
    assert sorted(cids.tolist()) == list(range(24))
    obj = board.getChessboardCorners()
    ox, oy = layout['board']['origin_mm'][:2]
    for (xs, ys), cid in zip(corners, cids):
        bx, by = obj[cid][0], obj[cid][1]
        ex, ey = ox + bx, oy - by  # board +X = base +x, board +Y = base −y
        x, y = _page_to_base(out, *_small_to_page_mm(xs, ys))
        assert math.hypot(x - ex, y - ey) <= _TOL_MM, (cid, (x, y), (ex, ey))


def test_the_lens_board_detector_finds_nothing_on_the_mat(out, mat_small):
    lens = _board(out['layout'], ids=range(100, 117))
    _corners, cids = _charuco(lens, mat_small)
    assert len(cids) == 0


# ── dots and marks ──────────────────────────────────────────────────────────

def _window(out, mat_png, cx, cy, half_mm):
    """The full-resolution window ±half_mm about (cx, cy), and its pixel origin."""
    u, v = out['page'].uv(cx, cy)
    s = _s()
    c0, r0 = int(math.floor((u - half_mm) * s)), int(math.floor((v - half_mm) * s))
    c1, r1 = int(math.ceil((u + half_mm) * s)), int(math.ceil((v + half_mm) * s))
    return mat_png[r0:r1, c0:c1], (c0, r0)


def _px_to_base(out, col_c, row_c, origin):
    """A pixel-centre coordinate of a window → base mm."""
    s = _s()
    return _page_to_base(out, (origin[0] + col_c + 0.5) / s, (origin[1] + row_c + 0.5) / s)


def _outline_centre(out, mat_png, cx, cy, half_mm):
    """The area centroid of the outermost ink contour in the window: the
    geometric centre of a ring or a square outline, whatever its stroke width
    (an ink-weighted centroid moves 0.3 mm when one 0.5 mm side bar of a 30 mm
    square rounds to one pixel more than the opposite one)."""
    win, origin = _window(out, mat_png, cx, cy, half_mm)
    mask = (win < 128).astype(np.uint8)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    assert contours
    outer = max(contours, key=cv2.contourArea)
    m = cv2.moments(outer)
    return _px_to_base(out, m['m10'] / m['m00'], m['m01'] / m['m00'], origin)


def _ink_centre(out, mat_png, cx, cy, half_mm):
    """The ink centroid within ±half_mm (for the symmetric centre cross alone)."""
    win, origin = _window(out, mat_png, cx, cy, half_mm)
    ink = (win < 128).astype(float)
    assert ink.sum() > 0
    rows, cols = np.mgrid[0:win.shape[0], 0:win.shape[1]]
    return _px_to_base(out, (ink * cols).sum() / ink.sum(), (ink * rows).sum() / ink.sum(), origin)


def test_every_touch_dot_is_printed_within_tolerance(out, mat_png):
    for dot in out['layout']['dots']:
        cx, cy = dot['centre_mm']
        r = dot['ring_diameter_mm'] / 2
        x, y = _outline_centre(out, mat_png, cx, cy, r + 1.5)
        assert math.hypot(x - cx, y - cy) <= _TOL_MM, (dot['id'], 'ring', (x, y))
        # the centre cross, inside the ring
        x, y = _ink_centre(out, mat_png, cx, cy, r - 1.0)
        assert math.hypot(x - cx, y - cy) <= _TOL_MM, (dot['id'], 'cross', (x, y))


def test_every_world_check_mark_is_printed_within_tolerance(out, mat_png):
    for mark in out['layout']['check_marks']:
        cx, cy = mark['centre_mm']
        h = mark['outline_mm'] / 2
        x, y = _outline_centre(out, mat_png, cx, cy, h + 1.5)
        assert math.hypot(x - cx, y - cy) <= _TOL_MM, (mark['id'], 'outline', (x, y))
        x, y = _ink_centre(out, mat_png, cx, cy, h - 2.0)
        assert math.hypot(x - cx, y - cy) <= _TOL_MM, (mark['id'], 'cross', (x, y))
        # A 30 mm cube exactly covers the outline (§2.2): its outer size.
        win, _o = _window(out, mat_png, cx, cy, h + 1.5)
        cols = np.where((win < 128).any(axis=0))[0]
        assert (cols[-1] - cols[0] + 1) / _s() == pytest.approx(mark['outline_mm'], abs=0.1)


def test_the_dot_ring_has_the_layout_s_diameter(out, mat_small):
    # The ring's outer edge: 10 mm across (§2.2), measured on the dark pixels.
    dot = out['layout']['dots'][0]
    u, v = out['page'].uv(*dot['centre_mm'])
    s = _s() / _DOWN
    row = int(round(v * s))
    line = mat_small[row, int((u - 8) * s): int((u + 8) * s)]
    dark = np.where(line < 128)[0]
    width_mm = (dark[-1] - dark[0] + 1) / s
    assert width_mm == pytest.approx(dot['ring_diameter_mm'], abs=0.5)


# ── the robot ───────────────────────────────────────────────────────────────

def _robot_window(out, mat_png, pad_mm=3.0):
    """The full-resolution window around the robot outline (pad_mm beyond its
    box), and the layout polygon filled in that window's pixels (1 = inside)."""
    page = out['page']
    s = _s()
    poly = [(float(x), float(y)) for x, y in out['layout']['robot']['outline_mm']]
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    u0, v0 = page.uv(max(xs) + pad_mm, max(ys) + pad_mm)
    u1, v1 = page.uv(min(xs) - pad_mm, min(ys) - pad_mm)
    c0, r0 = int(math.floor(u0 * s)), int(math.floor(v0 * s))
    c1, r1 = int(math.ceil(u1 * s)), int(math.ceil(v1 * s))
    win = mat_png[r0:r1, c0:c1]
    fixed = np.array([[int(round((u * s - 0.5 - c0) * 16)), int(round((v * s - 0.5 - r0) * 16))]
                      for u, v in (page.uv(x, y) for x, y in poly)], np.int32)
    inside = np.zeros(win.shape, np.uint8)
    cv2.fillPoly(inside, [fixed], 1, cv2.LINE_8, 4)
    return win, inside, (c0, r0)


def test_the_robot_outline_is_printed_where_the_layout_says(gen, out, mat_png):
    """A band of OUTLINE_W just outside the layout polygon: every pixel 0.1 mm
    to OUTLINE_W − 0.1 mm outside the outline is ink, nothing farther out (up
    to 3 mm) is, and no band ink lies more than 0.1 mm inside it."""
    s = _s()
    win, inside, _origin = _robot_window(out, mat_png)
    ink = win < 128
    out_dist = cv2.distanceTransform((1 - inside).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE) / s
    in_dist = cv2.distanceTransform(inside, cv2.DIST_L2, cv2.DIST_MASK_PRECISE) / s
    w = gen.OUTLINE_W
    band = (out_dist >= 0.1) & (out_dist <= w - 0.1)
    assert band.sum() > 0 and ink[band].all(), 'the outline band has a gap'
    far = out_dist >= w + 0.1
    assert not ink[far].any(), 'ink outside the robot outline beyond its band'
    # The band stops at the outline: no ink 0.1–1 mm inside it (the holes are
    # farther in).
    rim = (in_dist >= 0.1) & (in_dist <= 1.0)
    assert not ink[rim].any(), 'the outline band reaches inside the outline'


def test_every_mount_hole_is_printed_within_tolerance(out, mat_png):
    rob = out['layout']['robot']
    assert len(rob['mount_holes_mm']) == len(rob['mount_hole_diameters_mm']) == 10
    for (cx, cy), d in zip(rob['mount_holes_mm'], rob['mount_hole_diameters_mm']):
        r = d / 2
        x, y = _outline_centre(out, mat_png, cx, cy, r + 1.0)
        assert math.hypot(x - cx, y - cy) <= _TOL_MM, ((cx, cy), 'ring', (x, y))
        x, y = _ink_centre(out, mat_png, cx, cy, 1.5)  # the centre cross, inside the ring
        assert math.hypot(x - cx, y - cy) <= _TOL_MM, ((cx, cy), 'cross', (x, y))
        win, _o = _window(out, mat_png, cx, cy, r + 1.0)
        dark = win < 128
        cols = np.where(dark.any(axis=0))[0]
        rows = np.where(dark.any(axis=1))[0]
        assert (cols[-1] - cols[0] + 1) / _s() == pytest.approx(d, abs=_TOL_MM), ((cx, cy), 'width')
        assert (rows[-1] - rows[0] + 1) / _s() == pytest.approx(d, abs=_TOL_MM), ((cx, cy), 'height')


def test_nothing_but_the_mount_holes_is_printed_inside_the_robot_outline(out, mat_png):
    s = _s()
    win, inside, origin = _robot_window(out, mat_png)
    in_dist = cv2.distanceTransform(inside, cv2.DIST_L2, cv2.DIST_MASK_PRECISE) / s
    rows, cols = np.nonzero((win < 128) & (in_dist > 0.1))
    assert len(rows) > 0, 'the mounting holes are printed'
    page = out['page']
    x = page.x1 - (origin[1] + rows + 0.5) / s
    y = page.y1 - (origin[0] + cols + 0.5) / s
    rob = out['layout']['robot']
    near_a_hole = np.zeros(len(rows), bool)
    for (hx, hy), d in zip(rob['mount_holes_mm'], rob['mount_hole_diameters_mm']):
        near_a_hole |= np.hypot(x - hx, y - hy) <= d / 2 + 0.1
    stray = np.flatnonzero(~near_a_hole)
    assert len(stray) == 0, [(float(x[i]), float(y[i])) for i in stray[:5]]


# ── B1 / B2, labels, separation ─────────────────────────────────────────────

def test_b1_b2_are_the_farthest_black_squares_inside_the_zone(gen, out, mat_small):
    layout = out['layout']
    board = _board(layout)
    squares = gen.black_board_squares(layout, board)
    inside = [q for q in squares if gen.zone_contains(layout, q[2], q[3])]
    assert len(inside) >= 2
    best = max(math.hypot(a[2] - b[2], a[3] - b[3]) for a in inside for b in inside)
    b1, b2 = out['meta']['b_squares']['B1'], out['meta']['b_squares']['B2']
    assert math.hypot(b1[2] - b2[2], b1[3] - b2[3]) == pytest.approx(best)
    for q in (b1, b2):
        assert gen.zone_contains(layout, q[2], q[3])
        # really black on the printed mat: the square's interior is solid ink
        u, v = out['page'].uv(q[2], q[3])
        s = _s() / _DOWN
        patch = mat_small[int((v - 12) * s): int((v + 12) * s), int((u - 12) * s): int((u + 12) * s)]
        assert patch.max() < 64
    ids = {f['id'] for f in out['features']}
    assert {'label_B1', 'label_B2', 'label_dot1', 'label_M1', 'label_M8'} <= ids


def test_the_separation_rule_holds_and_everything_is_on_the_mat(gen, out):
    assert gen.separation_violations(out['features']) == []
    page = out['page']
    for f in out['features']:
        if f['kind'] == 'zone':
            continue
        xmin, xmax, ymin, ymax = f['box']
        assert page.x0 <= xmin and xmax <= page.x1, f['id']
        assert page.y0 <= ymin and ymax <= page.y1, f['id']
    # The catalog's object tags (20, 21) and the station references are disjoint (§2.2).
    assert all(100 <= r['id'] <= 199 for r in out['layout']['refs'])


def test_the_zone_outline_keeps_away_from_every_feature(gen, out, mat_small):
    zone = next(f for f in out['features'] if f['id'] == 'zone')
    assert zone['prims'], 'the work zone is printed'
    page = out['page']
    others = [f['box'] for f in out['features'] if f['kind'] != 'zone']
    for _k, u0, v0, u1, v1, _w in zone['prims']:
        for u, v in ((u0, v0), (u1, v1)):
            x, y = page.xy(u, v)
            assert min(gen._box_gap((x, x, y, y), o) for o in others) >= gen.SEPARATION_MM


# ── the lens board ──────────────────────────────────────────────────────────

def test_the_lens_board_carries_ids_100_to_116_and_is_detected(gen, out):
    img = cv2.imread(out['lens_png'], cv2.IMREAD_GRAYSCALE)
    pdf = gen.read_pdf_images(out['lens_pdf'])
    assert len(pdf) == 1 and np.array_equal(pdf[0], img)
    layout = out['layout']
    lens = _board(layout, ids=range(100, 117))
    assert lens.getIds().ravel().tolist() == list(range(100, 117))
    h, w = img.shape
    crop = img[: h - h % _DOWN, : w - w % _DOWN]
    small = cv2.resize(crop, (crop.shape[1] // _DOWN, crop.shape[0] // _DOWN), interpolation=cv2.INTER_AREA)
    corners, cids = _charuco(lens, small)
    assert len(cids) == 24
    obj = lens.getChessboardCorners()
    u0, v0 = out['lens_origin_mm']
    for (xs, ys), cid in zip(corners, cids):
        u, v = _small_to_page_mm(xs, ys)
        assert math.hypot(u - (u0 + obj[cid][0]), v - (v0 + obj[cid][1])) <= _TOL_MM
    # The fixed mat board's detector finds 0 corners on it (§6.1).
    _c, fixed = _charuco(_board(layout), small)
    assert len(fixed) == 0


# ── the SVG and the dry fit ─────────────────────────────────────────────────

def _svg_group_raster(group, shape, origin_px, s):
    """Rasterise a ``<g transform="translate(u v) scale(k)">`` of rects at s px/mm."""
    tu, tv, k = (float(v) for v in re.match(
        r'translate\(([-\d.]+) ([-\d.]+)\) scale\(([-\d.]+)\)', group.get('transform')).groups())
    img = np.full(shape, 255, np.uint8)
    for rect in group:
        x, y = float(rect.get('x')), float(rect.get('y'))
        w, h = float(rect.get('width')), float(rect.get('height'))
        c0 = int(round((tu + x * k) * s)) - origin_px[0]
        r0 = int(round((tv + y * k) * s)) - origin_px[1]
        c1 = int(round((tu + (x + w) * k) * s)) - origin_px[0]
        r1 = int(round((tv + (y + h) * k) * s)) - origin_px[1]
        img[max(0, r0):max(0, r1), max(0, c0):max(0, c1)] = 0
    return img


def test_the_svg_reproduces_the_tags_and_the_board(out, mat_png):
    root = ET.parse(out['svg']).getroot()
    ns = '{http://www.w3.org/2000/svg}'
    assert root.get('width') == '600mm' and root.get('height') == '808mm'
    assert root.get('viewBox') == '0 0 600 808'
    groups = {g.get('id'): g for g in root.iter(ns + 'g')}
    assert {'tag100', 'tag101', 'tag102', 'tag103', 'board'} <= set(groups)
    s = _s()
    for gid, g in groups.items():
        tu, tv = (float(v) for v in re.match(r'translate\(([-\d.]+) ([-\d.]+)\)', g.get('transform')).groups())
        if gid == 'board':
            w_mm, h_mm = 150.0, 210.0
        else:
            w_mm = h_mm = 50.0
        c0, r0 = int(round(tu * s)), int(round(tv * s))
        c1, r1 = int(round((tu + w_mm) * s)), int(round((tv + h_mm) * s))
        crop = mat_png[r0:r1, c0:c1]
        mine = _svg_group_raster(g, crop.shape, (c0, r0), s)
        agree = float((mine == crop).mean())
        assert agree >= 0.99, (gid, agree)
    texts = [t.text for t in root.iter(ns + 'text')]
    assert out['meta']['hash_text'] in texts
    assert 'Marker 100–103' in texts


def test_the_a3_dry_fit_is_the_same_raster_tiled_with_crosses_on_blank_mat(gen, out, mat_png):
    pages = gen.read_pdf_images(out['a3_pdf'])
    assert len(pages) == gen.TILE_ROWS * gen.TILE_COLS == out['a3_pages']
    s = _s()
    a3w, a3h = gen.A3_MM
    pdf = open(out['a3_pdf'], 'rb').read().decode('latin-1')
    boxes = re.findall(r'/MediaBox \[0 0 ([\d.]+) ([\d.]+)\]', pdf)
    assert len(boxes) == len(pages)
    for w_pt, h_pt in boxes:
        assert float(w_pt) == pytest.approx(a3w / 25.4 * 72, abs=0.01)
        assert float(h_pt) == pytest.approx(a3h / 25.4 * 72, abs=0.01)
    page = out['page']
    su, sv = page.width_mm / gen.TILE_COLS, page.height_mm / gen.TILE_ROWS
    cw, ch = su + 2 * gen.TILE_OVERLAP_MM, sv + 2 * gen.TILE_OVERLAP_MM
    off_u, off_v = (a3w - cw) / 2, (a3h - ch) / 2
    # The middle tile: its interior is the mat raster; crosses only ADD ink.
    tile = pages[4]
    mu0, mv0 = su - gen.TILE_OVERLAP_MM, sv - gen.TILE_OVERLAP_MM
    src = mat_png[round(mv0 * s): round((mv0 + ch) * s), round(mu0 * s): round((mu0 + cw) * s)]
    dst = tile[round(off_v * s): round(off_v * s) + src.shape[0], round(off_u * s): round(off_u * s) + src.shape[1]]
    assert dst.shape == src.shape
    assert (dst <= src).all()
    assert float((dst == src).mean()) > 0.995
    # Every registration cross sits on blank mat (it never hides artwork).
    marks = gen.registration_points(mat_png, page)
    assert len(marks) >= 2 * ((gen.TILE_COLS - 1) * gen.TILE_ROWS + (gen.TILE_ROWS - 1) * gen.TILE_COLS) - 2
    arm = gen.CROSS_ARM_MM
    for u, v in marks:
        area = mat_png[round((v - arm) * s): round((v + arm) * s), round((u - arm) * s): round((u + arm) * s)]
        assert area.min() == 255, (u, v)

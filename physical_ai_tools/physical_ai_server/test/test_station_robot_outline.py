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

"""The station mat's robot outline is the OMX base plate's real contact face.

``layout_data.LAYOUTS['OMX-STATION-A']['robot']`` carries ``outline_mm`` (CCW)
and ten mount holes as plain literals; this test derives the same geometry from
the shipped URDF base mesh ``follower_01_base.stl`` (link0 = the station's base
frame, mm, z = 0 the mat top) and holds the literals to it:

* the contact face is the union of the mesh's bottom faces (every vertex within
  0.5 mm of the lowest z); its outer boundary is the outline (area within
  0.3 mm × perimeter, boundary distance ≤ 0.3 mm both ways), and
  ``footprint_mm`` is the outline's bounding box;
* each mount hole matches one circular inner boundary (centre ≤ 0.2 mm,
  diameter ≤ 0.2 mm, the STL's polygon vertices lying on the circle), every
  other inner boundary is a small pocket (< 2.5 mm across) the mat leaves out;
* the zone, B1/B2 and the world-check marks did not move with the outline.

The STL is read from the checkout. A missing file FAILS (it never skips): the
python-tests job always has the checkout.
"""

import collections
import importlib.util
import math
from pathlib import Path
import struct

import pytest

_REPO = Path(__file__).resolve().parents[3]
_LAYOUT_DATA = _REPO / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server' / 'station' / 'layout_data.py'
_STL = (_REPO / 'physical_ai_tools' / 'physical_ai_manager' / 'public' / 'omx-urdf' / 'meshes'
        / 'follower_01_base.stl')

_BOTTOM_BAND_MM = 0.5     # a face belongs to the contact face when all its vertices are this close to min z
_OUTLINE_TOL_MM = 0.3
_HOLE_CENTRE_TOL_MM = 0.2
_HOLE_DIAMETER_TOL_MM = 0.2
_POCKET_MAX_MM = 2.5      # an inner boundary smaller than this is not a mounting hole


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── the STL ─────────────────────────────────────────────────────────────────

def read_stl_triangles(path):
    """Every triangle of a binary STL as ((x, y, z), (x, y, z), (x, y, z)).

    The file starts with ``solid`` like an ASCII STL, so the size decides: a
    binary STL is exactly 84 + 50 × the triangle count bytes."""
    path = Path(path)
    assert path.is_file(), f'the robot base mesh is missing: {path}'
    data = path.read_bytes()
    count = struct.unpack_from('<I', data, 80)[0]
    assert len(data) == 84 + 50 * count, 'not a binary STL'
    tris = []
    for k in range(count):
        v = struct.unpack_from('<9f', data, 84 + 50 * k + 12)
        tris.append(((v[0], v[1], v[2]), (v[3], v[4], v[5]), (v[6], v[7], v[8])))
    return tris


def contact_loops(tris):
    """The boundary loops of the mesh's bottom faces (the contact face), each a
    list of (x, y): the edges used by exactly one bottom face, chained."""
    z_min = min(p[2] for t in tris for p in t)
    bottom = [t for t in tris if all(abs(p[2] - z_min) <= _BOTTOM_BAND_MM for p in t)]
    assert bottom, 'the mesh has no bottom face'

    def key(p):
        return (round(p[0], 4), round(p[1], 4))

    uses = collections.Counter()
    for t in bottom:
        k = [key(p) for p in t]
        for i in range(3):
            uses[tuple(sorted((k[i], k[(i + 1) % 3])))] += 1
    assert max(uses.values()) <= 2, 'the contact face is not a manifold surface'
    adjacent = collections.defaultdict(list)
    for (a, b), n in uses.items():
        if n == 1:
            adjacent[a].append(b)
            adjacent[b].append(a)
    assert all(len(v) == 2 for v in adjacent.values()), 'a boundary vertex is not on exactly one loop'
    loops, seen = [], set()
    for start in adjacent:
        if start in seen:
            continue
        loop, prev, cur = [start], None, start
        seen.add(start)
        while True:
            nxt = [q for q in adjacent[cur] if q != prev][0]
            if nxt == start:
                break
            loop.append(nxt)
            seen.add(nxt)
            prev, cur = cur, nxt
        loops.append(loop)
    return loops


# ── plane geometry ──────────────────────────────────────────────────────────

def signed_area(poly):
    return sum(poly[i][0] * poly[(i + 1) % len(poly)][1] - poly[(i + 1) % len(poly)][0] * poly[i][1]
               for i in range(len(poly))) / 2.0


def perimeter(poly):
    return sum(math.dist(poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly)))


def _point_segment(p, a, b):
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    n = dx * dx + dy * dy
    t = 0.0 if n == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / n))
    return math.hypot(p[0] - ax - t * dx, p[1] - ay - t * dy)


def distance_to_boundary(p, poly):
    return min(_point_segment(p, poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly)))


def boundary_samples(poly, step=0.25):
    out = []
    for i in range(len(poly)):
        a, b = poly[i], poly[(i + 1) % len(poly)]
        n = max(1, int(math.ceil(math.dist(a, b) / step)))
        out += [(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(n)]
    return out


def boundary_distance(p, q):
    """The symmetric (Hausdorff) distance between two closed polylines (mm)."""
    return max(max(distance_to_boundary(s, q) for s in boundary_samples(p)),
               max(distance_to_boundary(s, p) for s in boundary_samples(q)))


def contains(poly, p):
    x, y = p
    inside = False
    for i in range(len(poly)):
        (x0, y0), (x1, y1) = poly[i], poly[(i + 1) % len(poly)]
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
            inside = not inside
    return inside


def _segments_cross(a, b, c, d):
    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    return (orient(a, b, c) * orient(a, b, d) < 0) and (orient(c, d, a) * orient(c, d, b) < 0)


def is_simple(poly):
    n = len(poly)
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue
            if _segments_cross(poly[i], poly[(i + 1) % n], poly[j], poly[(j + 1) % n]):
                return False
    return True


def circle_of(loop):
    """The centre (vertex mean) and the diameter (twice the mean vertex
    radius) of a tessellated circle, plus how far its vertices stray."""
    cx = sum(p[0] for p in loop) / len(loop)
    cy = sum(p[1] for p in loop) / len(loop)
    radii = [math.hypot(p[0] - cx, p[1] - cy) for p in loop]
    r = sum(radii) / len(radii)
    return (cx, cy), 2.0 * r, max(abs(v - r) for v in radii)


def _width(loop):
    return max(max(p[0] for p in loop) - min(p[0] for p in loop),
               max(p[1] for p in loop) - min(p[1] for p in loop))


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(scope='module')
def layout_data():
    return _load('layout_data_outline_test', _LAYOUT_DATA)


@pytest.fixture(scope='module')
def robot(layout_data):
    return layout_data.LAYOUTS['OMX-STATION-A']['robot']


@pytest.fixture(scope='module')
def stl_loops():
    loops = contact_loops(read_stl_triangles(_STL))
    outer = max(loops, key=lambda lp: abs(signed_area(lp)))
    inner = [lp for lp in loops if lp is not outer]
    if signed_area(outer) < 0:
        outer = outer[::-1]
    return outer, inner


# ── the outline ─────────────────────────────────────────────────────────────

def test_the_mesh_is_where_the_test_expects_it():
    # The URDF places link0's mesh at the origin, scaled 0.001: the STL is mm in
    # the station's base frame, its lowest point on the mat (z = 0).
    tris = read_stl_triangles(_STL)
    assert min(p[2] for t in tris for p in t) == pytest.approx(0.0, abs=1e-6)
    urdf = (_REPO / 'physical_ai_tools' / 'physical_ai_manager' / 'public' / 'omx-urdf' / 'omx_f.urdf')
    text = urdf.read_text(encoding='utf-8')
    link0 = text[text.index('<link name="link0">'):]
    link0 = link0[:link0.index('</link>')]
    assert 'meshes/follower_01_base.stl' in link0
    assert 'scale="0.001 0.001 0.001"' in link0
    assert 'xyz="0 0 0"' in link0 and 'rpy="0 0 0"' in link0
    assert '<origin rpy="0 0 0" xyz="0.0 0.0 0.0" />' in text[text.index('<joint name="world_fixed"'):]


def test_the_outline_is_the_contact_face_s_outer_boundary(robot, stl_loops):
    outer, _inner = stl_loops
    poly = [tuple(p) for p in robot['outline_mm']]
    assert len(poly) >= 3 and all(len(p) == 2 for p in poly)
    assert all(isinstance(v, float) and math.isfinite(v) and round(v, 2) == v for p in poly for v in p)
    assert signed_area(poly) > 0, 'outline_mm must be counter-clockwise'
    assert is_simple(poly)
    tol_area = _OUTLINE_TOL_MM * perimeter(outer)
    assert abs(signed_area(poly) - signed_area(outer)) <= tol_area, (signed_area(poly), signed_area(outer))
    assert boundary_distance(poly, outer) <= _OUTLINE_TOL_MM


def test_the_footprint_is_the_outline_s_bounding_box(robot):
    xs = [p[0] for p in robot['outline_mm']]
    ys = [p[1] for p in robot['outline_mm']]
    fp = robot['footprint_mm']
    assert [min(xs), max(xs)] == [float(v) for v in fp['x_range_mm']]
    assert [min(ys), max(ys)] == [float(v) for v in fp['y_range_mm']]
    assert robot['measured'] is False


# ── the holes ───────────────────────────────────────────────────────────────

def test_every_mount_hole_is_a_hole_of_the_contact_face(robot, stl_loops):
    _outer, inner = stl_loops
    holes = [tuple(h) for h in robot['mount_holes_mm']]
    diameters = robot['mount_hole_diameters_mm']
    assert len(holes) == len(diameters) == 10
    assert len(set(holes)) == 10
    circles = [circle_of(lp) for lp in inner if _width(lp) >= _POCKET_MAX_MM]
    assert len(circles) == len(holes), (len(circles), len(holes))
    unmatched = list(circles)
    for (hx, hy), d in zip(holes, diameters):
        best = min(unmatched, key=lambda c: math.hypot(c[0][0] - hx, c[0][1] - hy))
        (cx, cy), cd, stray = best
        assert math.hypot(cx - hx, cy - hy) <= _HOLE_CENTRE_TOL_MM, ((hx, hy), (cx, cy))
        assert abs(cd - d) <= _HOLE_DIAMETER_TOL_MM, ((hx, hy), d, cd)
        assert stray <= 0.01, ((hx, hy), 'the STL hole is not a regular polygon on a circle', stray)
        unmatched.remove(best)
    assert not unmatched


def test_the_only_other_inner_boundaries_are_small_pockets(stl_loops):
    """The mat draws the ten holes and nothing else inside the outline; every
    other inner boundary of the contact face must be a small pocket (today:
    four Ø1.8 mm blind holes under the PCB standoffs)."""
    _outer, inner = stl_loops
    small = [lp for lp in inner if _width(lp) < _POCKET_MAX_MM]
    assert len(small) + 10 == len(inner)
    for lp in small:
        assert _width(lp) < _POCKET_MAX_MM


def test_every_hole_lies_inside_the_outline_with_material_around_it(robot):
    poly = [tuple(p) for p in robot['outline_mm']]
    for (hx, hy), d in zip(robot['mount_holes_mm'], robot['mount_hole_diameters_mm']):
        assert contains(poly, (hx, hy))
        assert distance_to_boundary((hx, hy), poly) > d / 2 + 1.0, (hx, hy)


# ── nothing else moved ──────────────────────────────────────────────────────

def test_the_zone_and_the_check_targets_did_not_move(layout_data):
    """The zone and B1/B2 derive from footprint_mm, which the outline did not
    change; the world-check marks are literals."""
    lay = layout_data.LAYOUTS['OMX-STATION-A']
    assert layout_data.board_check_squares(lay) == [('B1', (195.0, 60.0)), ('B2', (195.0, -60.0))]
    assert layout_data.world_check_marks(lay) == [
        ('M1', (109.99, 70.0)), ('M2', (109.99, -70.0)),
        ('M3', (24.98, 135.23)), ('M4', (24.98, -135.23)),
        ('M5', (123.74, 160.87)), ('M6', (123.74, -160.87)),
        ('M7', (24.35, 201.89)), ('M8', (24.35, -201.89)),
        ('B1', (195.0, 60.0)), ('B2', (195.0, -60.0)),
    ]
    # The zone still excludes the footprint grown by 10 mm, not the outline:
    # (65, 70) lies in the annulus and outside the plate, yet not in the zone.
    assert not layout_data._in_nominal_zone(lay, 65.0, 70.0)
    assert layout_data._in_nominal_zone(lay, 75.0, 70.0)

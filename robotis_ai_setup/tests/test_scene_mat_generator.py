#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The tower kit's print files (spec 40 WP-03, decision D26: kit-supplied prints).

``tools/generate_scene_mat.py`` draws the two scene mats and
``tools/generate_calibration_target.py`` the two clip-on target labels and the
fixture-tag sheet, all from the fixture contract
(``physical_ai_server/workflow/scene_fixture.py``). What is printed is judged
here from the PDF the generator WRITES: the page box, the placement of its one
raster image, the raster itself (decompressed by this file's own reader) and the
text of its content stream.

* each mat at 100 dpi, read with ``cv2.aruco`` (DICT_APRILTAG_36H11): exactly the
  14 contract ids, every centre within 0.5 mm, every tag's top toward +x_F; the
  two ruler marks 100 +- 0.3 mm; the page the sheet (470 x 830 / 380 x 660 mm);
  the notch's cut line at x 54.9 mm and |y| 89.1 mm;
* the printed zone leaves the tower-side corner out (D66): rendered at 600 dpi,
  the zone fill holds (r, zone_az_hi(r) - 0.5 deg) and not (r, zone_az_hi(r) +
  0.5 deg) at five radii per arm inside the cut, and the V1's sector edge beyond
  the cut (r 0.31 m) likewise; the generator reads the cut ONLY through
  ``scene_fixture.zone_az_hi`` / ``in_zone`` (a source scan);
* the title block names the arm, ``FIXTURE_ID`` and the first 8 hex of
  ``contract_hash()``; no tiling option exists (D26: a tiled school print is not
  a supported path);
* ``tools/generate_apriltags.py`` refuses an id of ``RESERVED_TAG_IDS`` in German;
* the clip-on target labels carry their nine ids row-major and the fixture-tag
  sheet 480/481 at 25 mm, every tag's top toward +x of its frame;
* the five PDFs in ``classroom_kit/`` are the generators' output at 300 dpi (page,
  tags and cut; not the printed hash, which WP-11 changes on purpose).

The 600-dpi zone check renders small windows through the same renderer the PDF
is written from (a whole V1 sheet at 600 dpi is 218 million pixels); the same
probes are also checked on the 100-dpi PDF raster.
"""

import ast
import contextlib
import importlib.util
import io
import math
import os
import re
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / 'tools'
SERVER_ROOT = REPO / 'physical_ai_tools' / 'physical_ai_server'
KIT = REPO / 'classroom_kit'
MAT_SOURCE = TOOLS / 'generate_scene_mat.py'
APRILTAGS = TOOLS / 'generate_apriltags.py'

try:  # numpy/cv2 (the contrib build, for aruco) may be absent on a bare runner.
    import cv2
    import numpy as np
    _HAVE_CV2 = hasattr(cv2, 'aruco')
except ImportError:  # pragma: no cover - environment dependent
    _HAVE_CV2 = False


def _load_contract():
    """``scene_fixture`` from its file (standard library only): other tests of this suite
    leave stand-ins for the ``physical_ai_server`` package in ``sys.modules``."""
    spec = importlib.util.spec_from_file_location(
        '_scene_mat_test_contract', SERVER_ROOT / 'physical_ai_server' / 'workflow' / 'scene_fixture.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sf = _load_contract()

PT_PER_MM = 72.0 / 25.4
SHEET_MM = {'v1': (470.0, 830.0), 'omx': (380.0, 660.0)}


def _load_tool(name):
    """Import ``tools/<name>.py`` under its own name (the target generator imports the mat's)."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, TOOLS / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module


# ── a reader for the generator's own PDF ────────────────────────────────────
def _unescape_pdf_string(raw):
    out = bytearray()
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == '\\':
            nxt = raw[i + 1]
            if nxt in '01234567':
                j = i + 1
                while j < len(raw) and j < i + 4 and raw[j] in '01234567':
                    j += 1
                out.append(int(raw[i + 1:j], 8))
                i = j
                continue
            out.append(ord({'n': '\n', 'r': '\r', 't': '\t'}.get(nxt, nxt)))
            i += 2
            continue
        out.append(ord(c))
        i += 1
    return bytes(out).decode('cp1252')


def read_pdf(path):
    """``{'media_mm', 'image_mm', 'index', 'palette', 'texts', 'pages'}`` of a one-image PDF.

    Also checks the cross-reference table: every object starts where the table says.
    """
    data = Path(path).read_bytes()
    text = data.decode('latin-1')
    assert text.startswith('%PDF-1.'), 'not a PDF'
    start = int(re.search(r'startxref\s+(\d+)\s+%%EOF\s*$', text).group(1))
    assert text[start:start + 4] == 'xref', 'startxref does not point at the table'
    head = re.match(r'xref\s+0 (\d+)\s+', text[start:])
    count = int(head.group(1))
    entries = re.findall(r'(\d{10}) (\d{5}) ([nf])', text[start + head.end():])[:count]
    for number, (offset, _gen, kind) in enumerate(entries):
        if kind == 'n':
            assert re.match(rf'{number} 0 obj\b', text[int(offset):]), f'object {number} misplaced'
    image = re.search(r'/Subtype\s*/Image(.*?)>>\s*stream\r?\n', text, re.S)
    before = text[:image.start()]           # the catalog, the page and its content stream; never binary
    pages = len(re.findall(r'/Type\s*/Page\b', before))
    media = [float(v) for v in re.search(r'/MediaBox\s*\[([^\]]*)\]', before).group(1).split()]
    assert media[:2] == [0.0, 0.0], media
    params = image.group(1)
    width = int(re.search(r'/Width (\d+)', params).group(1))
    height = int(re.search(r'/Height (\d+)', params).group(1))
    length = int(re.search(r'/Length (\d+)', params).group(1))
    palette_hex = re.search(r'/Indexed\s*/DeviceRGB\s*(\d+)\s*<([0-9A-Fa-f]+)>', params)
    pal = bytes.fromhex(palette_hex.group(2))
    palette = [tuple(pal[i:i + 3]) for i in range(0, len(pal), 3)]
    assert len(palette) == int(palette_hex.group(1)) + 1
    raw = zlib.decompress(data[image.end():image.end() + length])
    assert len(raw) == width * height, 'image stream size'
    content = re.search(r'/Im0 Do', before)
    cm = re.findall(r'([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+) cm', before[:content.start()])[-1]
    a, b, c, d, e, f = (float(v) for v in cm)
    texts = [_unescape_pdf_string(t) for t in re.findall(r'\(((?:\\.|[^\\)])*)\)\s*Tj', before)]
    return {
        'media_mm': (media[2] / PT_PER_MM, media[3] / PT_PER_MM),
        'image_mm': (a / PT_PER_MM, d / PT_PER_MM),        # the image's printed width and height
        'image_origin_mm': (e / PT_PER_MM, f / PT_PER_MM),  # its lower-left corner on the page
        'image_px': (width, height),
        'image_skew': (b, c),
        'index': raw,
        'palette': palette,
        'texts': texts,
        'pages': pages,
    }


def _index_array(pdf):
    w, h = pdf['image_px']
    return np.frombuffer(pdf['index'], dtype=np.uint8).reshape(h, w)


def _gray(pdf):
    lut = np.array([round(0.299 * r + 0.587 * g + 0.114 * b) for r, g, b in pdf['palette']], dtype=np.uint8)
    return lut[_index_array(pdf)]


def _mm_per_px(pdf):
    (wm, hm), (wp, hp) = pdf['image_mm'], pdf['image_px']
    return wm / wp, hm / hp


def _aruco_read(gray):
    """``{id: (centre (col, row) in OpenCV pixel coordinates, the four corners)}``; ids seen twice are kept twice."""
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36H11)
    params = cv2.aruco.DetectorParameters()
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    corners, ids, _rejected = cv2.aruco.ArucoDetector(dictionary, params).detectMarkers(gray)
    out = []
    for quad, tag_id in zip(corners or (), [] if ids is None else ids.ravel()):
        quad = quad.reshape(4, 2).astype(float)
        out.append((int(tag_id), quad))
    return out


def _f_from_px(pdf, sheet, col, row):
    """A continuous raster position (OpenCV convention: pixel centres on integers) in F, mm."""
    sx, sy = _mm_per_px(pdf)
    (x0, _y0), (_x1, y1) = sheet
    return x0 + (col + 0.5) * sx, y1 - (row + 0.5) * sy


def _px_of_f(pdf, sheet, x_mm, y_mm):
    """The (row, col) index of the pixel that holds the F point (mm)."""
    sx, sy = _mm_per_px(pdf)
    (x0, _y0), (_x1, y1) = sheet
    return int(math.floor((y1 - y_mm) / sy)), int(math.floor((x_mm - x0) / sx))


def _runs(mask):
    """Centroids (in pixel index units) of the runs of True in a 1-D boolean array."""
    out, start = [], None
    for i, on in enumerate(list(mask) + [False]):
        if on and start is None:
            start = i
        elif not on and start is not None:
            out.append((start + i - 1) / 2.0)
            start = None
    return out


def _run_quietly(main, argv):
    """A generator's ``main(argv)`` without its "Wrote …" line on the test output."""
    with contextlib.redirect_stdout(io.StringIO()):
        return main(argv)


def _sheet_mm(arm):
    (x0, y0), (x1, y1) = sf.ARMS[arm]['mat_sheet_f']
    return (x0 * 1000.0, y0 * 1000.0), (x1 * 1000.0, y1 * 1000.0)


def _probe_radii(arm):
    """Five radii spread over the cut's radial range from 5 mm inside its first knot (or the
    zone's inner edge) to 5 mm inside its last (or the outer edge); a radius within 5 mm of a
    knot strictly between those two is moved 5 mm off it."""
    spec = sf.ARMS[arm]
    knots = [k[0] for k in spec['zone_cut_knots']]
    lo = max(knots[0], spec['zone_inner_m']) + 0.005
    hi = min(knots[-1], spec['zone_outer_m']) - 0.005
    radii = []
    for t in (0.0, 0.25, 0.5, 0.75, 1.0):
        r = lo + (hi - lo) * t
        nearest = min(knots, key=lambda k: abs(k - r))
        if abs(nearest - r) < 0.005 and lo < nearest < hi:
            r = nearest - 0.005 if r < nearest else nearest + 0.005
        radii.append(r)
    return radii


def _zone_probes(arm):
    """``(r, az_deg, expected_in_fill, what)``: 0.5 deg inside and outside the tower-side edge."""
    spec = sf.ARMS[arm]
    probes = []
    for r in _probe_radii(arm):
        edge = sf.zone_az_hi(spec, r)
        probes.append((r, edge - 0.5, True, f'r {r:.4f} m, cut edge {edge:.3f} deg - 0.5'))
        probes.append((r, edge + 0.5, False, f'r {r:.4f} m, cut edge {edge:.3f} deg + 0.5'))
    if arm == 'v1':     # beyond the cut the sector edge is the zone's edge
        r = 0.31
        edge = sf.zone_az_hi(spec, r)
        assert edge == spec['zone_sector_deg'][1]
        probes.append((r, edge - 0.5, True, 'r 0.31 m, sector edge - 0.5'))
        probes.append((r, edge + 0.5, False, 'r 0.31 m, sector edge + 0.5'))
    return probes


@unittest.skipUnless(_HAVE_CV2, 'numpy / cv2 (contrib, aruco) not available')
class TheMatsAsPrinted(unittest.TestCase):
    """Each mat written at 100 dpi and judged from the PDF file."""

    @classmethod
    def setUpClass(cls):
        cls.mat = _load_tool('generate_scene_mat')
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pdf = {}
        for arm in ('omx', 'v1'):
            out = Path(cls.tmp.name) / f'scene_mat_{arm}.pdf'
            rc = _run_quietly(cls.mat.main, ['--arm', arm, '--dpi', '100', '--out', str(out)])
            assert rc == 0, rc
            cls.pdf[arm] = read_pdf(out)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_page_is_the_sheet(self):
        for arm, (w, h) in SHEET_MM.items():
            with self.subTest(arm=arm):
                pdf = self.pdf[arm]
                self.assertEqual(pdf['pages'], 1)
                self.assertAlmostEqual(pdf['media_mm'][0], w, delta=0.01)
                self.assertAlmostEqual(pdf['media_mm'][1], h, delta=0.01)
                (x0, y0), (x1, y1) = _sheet_mm(arm)
                self.assertAlmostEqual(x1 - x0, w, delta=1e-6)   # the contract's sheet is that size
                self.assertAlmostEqual(y1 - y0, h, delta=1e-6)
                # the raster is printed at exactly 100 dpi, anchored at the page's top-left corner,
                # covering the whole page (an overhang below one pixel is clipped by the page)
                sx, sy = _mm_per_px(pdf)
                self.assertAlmostEqual(sx, 25.4 / 100, delta=1e-9)
                self.assertAlmostEqual(sy, 25.4 / 100, delta=1e-9)
                self.assertEqual(pdf['image_skew'], (0.0, 0.0))
                self.assertAlmostEqual(pdf['image_origin_mm'][0], 0.0, delta=1e-6)
                self.assertAlmostEqual(pdf['image_origin_mm'][1] + pdf['image_mm'][1], h, delta=1e-3)
                self.assertGreaterEqual(pdf['image_mm'][0], w - 1e-6)
                self.assertGreaterEqual(pdf['image_mm'][1], h - 1e-6)
                self.assertLess(pdf['image_mm'][0] - w, sx)
                self.assertLess(pdf['image_mm'][1] - h, sy)

    def test_exactly_the_contract_tags_at_their_place_top_toward_plus_x(self):
        for arm in ('omx', 'v1'):
            with self.subTest(arm=arm):
                pdf = self.pdf[arm]
                sheet = _sheet_mm(arm)
                found = _aruco_read(_gray(pdf))
                expected = {tid: (x * 1000.0, y * 1000.0) for tid, (x, y) in sf.ARMS[arm]['mat_tags_f'].items()}
                self.assertEqual(len(expected), 14)
                self.assertEqual(sorted(t for t, _q in found), sorted(expected))
                for tid, quad in found:
                    centre = quad.mean(axis=0)
                    x, y = _f_from_px(pdf, sheet, *centre)
                    ex, ey = expected[tid]
                    self.assertLessEqual(math.hypot(x - ex, y - ey), 0.5, f'tag {tid} centre ({x:.2f}, {y:.2f})')
                    # aruco's corners: top-left, top-right, bottom-right, bottom-left of the pattern
                    top_mid = (quad[0] + quad[1]) / 2.0
                    du, dv = top_mid - centre
                    angle = math.degrees(math.atan2(-dv, du))      # page down is -y_F
                    self.assertLess(abs(angle), 2.0, f'tag {tid}: top points {angle:.1f} deg off +x_F')

    def test_the_two_ruler_marks_are_100_mm(self):
        for arm in ('omx', 'v1'):
            layout = self.mat.mat_layout(arm)
            pdf = self.pdf[arm]
            index = _index_array(pdf)
            sheet = _sheet_mm(arm)
            sx, sy = _mm_per_px(pdf)
            self.assertEqual(sorted(r.axis for r in layout.rulers), ['x', 'y'])
            for ruler in layout.rulers:
                with self.subTest(arm=arm, axis=ruler.axis):
                    probe = ruler.end_probe_mm()              # a line crossing the two end ticks only
                    if ruler.axis == 'x':
                        row, _ = _px_of_f(pdf, sheet, probe[0][0], probe[0][1])
                        _, c0 = _px_of_f(pdf, sheet, probe[0][0], probe[0][1])
                        _, c1 = _px_of_f(pdf, sheet, probe[1][0], probe[1][1])
                        runs = _runs(index[row, c0:c1 + 1] == self.mat.LINE)
                        scale = sx
                    else:
                        _, col = _px_of_f(pdf, sheet, probe[0][0], probe[0][1])
                        r0, _ = _px_of_f(pdf, sheet, probe[1][0], probe[1][1])
                        r1, _ = _px_of_f(pdf, sheet, probe[0][0], probe[0][1])
                        runs = _runs(index[r0:r1 + 1, col] == self.mat.LINE)
                        scale = sy
                    self.assertEqual(len(runs), 2, runs)
                    self.assertAlmostEqual((runs[1] - runs[0]) * scale, 100.0, delta=0.3)

    def test_the_notch_is_the_contract_notch(self):
        x_max = sf.MAT_NOTCH_F['x_max'] * 1000.0
        y_abs = sf.MAT_NOTCH_F['y_abs'] * 1000.0
        self.assertEqual((x_max, y_abs), (54.9, 89.1))
        for arm in ('omx', 'v1'):
            with self.subTest(arm=arm):
                pdf = self.pdf[arm]
                index = _index_array(pdf)
                sheet = _sheet_mm(arm)
                (x0, _y0), _ = sheet
                sx, sy = _mm_per_px(pdf)
                # the cut line across the notch's front edge, on the row y = 30 mm
                row, c0 = _px_of_f(pdf, sheet, 40.0, 30.0)
                _, c1 = _px_of_f(pdf, sheet, 70.0, 30.0)
                runs = _runs(index[row, c0:c1 + 1] == self.mat.LINE)
                self.assertEqual(len(runs), 1, runs)
                self.assertAlmostEqual(x0 + (c0 + runs[0] + 0.5) * sx, x_max, delta=0.3)
                # the cut line along the two side edges, on the column x = 30 mm
                r_top, col = _px_of_f(pdf, sheet, 30.0, 110.0)
                r_bot, _ = _px_of_f(pdf, sheet, 30.0, -110.0)
                runs = _runs(index[r_top:r_bot + 1, col] == self.mat.LINE)
                self.assertEqual(len(runs), 2, runs)
                y_hi = sheet[1][1] - (r_top + runs[0] + 0.5) * sy
                y_lo = sheet[1][1] - (r_top + runs[1] + 0.5) * sy
                self.assertAlmostEqual(y_hi, y_abs, delta=0.3)
                self.assertAlmostEqual(y_lo, -y_abs, delta=0.3)
                # nothing but paper and lines inside the cut-out (no tag, no zone)
                ra, ca = _px_of_f(pdf, sheet, x0 + 0.5, y_abs - 0.5)
                rb, cb = _px_of_f(pdf, sheet, x_max - 0.5, -y_abs + 0.5)
                inside = set(np.unique(index[ra:rb + 1, ca:cb + 1]).tolist())
                self.assertLessEqual(inside, {self.mat.PAPER, self.mat.LINE})

    def test_the_title_block_names_the_arm_the_contract_and_its_hash(self):
        for arm in ('omx', 'v1'):
            with self.subTest(arm=arm):
                layout = self.mat.mat_layout(arm)
                title = ' '.join(layout.title_lines)
                self.assertIn(self.mat.ARM_NAMES[arm], title)
                self.assertIn(sf.FIXTURE_ID, title)
                self.assertIn(sf.contract_hash()[:8], title)
                for line in layout.title_lines:
                    self.assertIn(line, self.pdf[arm]['texts'])   # printed as written

    def test_the_printed_texts_are_h4s(self):
        for arm in ('omx', 'v1'):
            with self.subTest(arm=arm):
                texts = ' '.join(self.pdf[arm]['texts'])
                self.assertIn('Matte bis zum Anschlag an die Grundplatte schieben', texts.replace('\n', ' '))
                self.assertIn('Marker nicht abdecken', texts)

    def test_no_text_or_ruler_covers_a_tag_the_zone_or_the_notch(self):
        for arm in ('omx', 'v1'):
            with self.subTest(arm=arm):
                self.assertEqual(self.mat.layout_conflicts(self.mat.mat_layout(arm)), [])

    def test_the_cut_on_the_pdf_raster(self):
        """The zone probes on the 100-dpi PDF itself (0.5 deg is at least 3.8 px there)."""
        for arm in ('omx', 'v1'):
            pdf = self.pdf[arm]
            index = _index_array(pdf)
            sheet = _sheet_mm(arm)
            for r, az, inside, what in _zone_probes(arm):
                with self.subTest(arm=arm, probe=what):
                    x, y = r * 1000.0 * math.cos(math.radians(az)), r * 1000.0 * math.sin(math.radians(az))
                    row, col = _px_of_f(pdf, sheet, x, y)
                    self.assertEqual(index[row, col] == self.mat.ZONE, inside)


@unittest.skipUnless(_HAVE_CV2, 'numpy / cv2 (contrib, aruco) not available')
class TheZoneLeavesTheTowerCornerOut(unittest.TestCase):
    """D66: rendered at 600 dpi, the fill follows ``zone_az_hi``; today's sector fills the corner."""

    @classmethod
    def setUpClass(cls):
        cls.mat = _load_tool('generate_scene_mat')

    def test_the_fill_ends_at_the_cut_at_600_dpi(self):
        for arm in ('omx', 'v1'):
            for r, az, inside, what in _zone_probes(arm):
                with self.subTest(arm=arm, probe=what):
                    x = r * 1000.0 * math.cos(math.radians(az))
                    y = r * 1000.0 * math.sin(math.radians(az))
                    raster = self.mat.render_mat(arm, dpi=600, window=((x - 3.0, y - 3.0), (x + 3.0, y + 3.0)))
                    row, col = raster.pixel_of_f(x, y)
                    self.assertEqual(raster.index[row, col] == self.mat.ZONE, inside)
                    self.assertEqual(sf.in_zone(sf.ARMS[arm], x / 1000.0, y / 1000.0), inside)

    def test_the_outline_is_sampled_every_half_millimetre_on_the_edge(self):
        for arm in ('omx', 'v1'):
            with self.subTest(arm=arm):
                spec = sf.ARMS[arm]
                pts = self.mat.zone_outline_mm(spec)
                steps = [math.dist(a, b) for a, b in zip(pts, pts[1:] + pts[:1])]
                # every edge but the closing radial edge at the sector's low end
                self.assertLessEqual(sorted(steps)[-2], 0.5 + 1e-9)
                for x, y in pts:
                    self.assertTrue(sf.in_zone(spec, x / 1000.0, y / 1000.0), (x, y))

    def test_the_generator_reads_the_cut_only_through_scene_fixture(self):
        source = MAT_SOURCE.read_text(encoding='utf-8')
        tree = ast.parse(source)
        self.assertNotIn('zone_cut_knots', source)
        self.assertIn('zone_az_hi', source)
        knot_values = set()
        for spec in sf.ARMS.values():
            for r, az in spec['zone_cut_knots']:
                knot_values.update((r, az, round(r * 1000.0, 6)))
        literals = {node.value for node in ast.walk(tree)
                    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float))
                    and not isinstance(node.value, bool)}
        self.assertEqual(sorted(literals & knot_values), [])


class NoTilingOption(unittest.TestCase):
    """D26: a tiled school print is not a supported path, so the generators offer none."""

    @unittest.skipUnless(_HAVE_CV2, 'numpy / cv2 (contrib, aruco) not available')
    def test_the_options_are_arm_dpi_and_out(self):
        mat = _load_tool('generate_scene_mat')
        options = {s for action in mat.build_parser()._actions for s in action.option_strings}
        self.assertEqual(options, {'-h', '--help', '--arm', '--dpi', '--out'})
        target = _load_tool('generate_calibration_target')
        options = {s for action in target.build_parser()._actions for s in action.option_strings}
        self.assertEqual(options, {'-h', '--help', '--arm', '--fixture-tags', '--dpi', '--out'})


@unittest.skipUnless(_HAVE_CV2, 'numpy / cv2 (contrib, aruco) not available')
class TheTargetLabelsAndTheFixtureTags(unittest.TestCase):
    """The clip-on target labels (41 H5) and the tower's fixture tags 480/481, as printed."""

    @classmethod
    def setUpClass(cls):
        cls.target = _load_tool('generate_calibration_target')
        cls.mat = _load_tool('generate_scene_mat')
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pdf = {}
        for arm in ('omx', 'v1'):
            out = Path(cls.tmp.name) / f'calibration_target_{arm}.pdf'
            assert _run_quietly(cls.target.main, ['--arm', arm, '--dpi', '100', '--out', str(out)]) == 0
            cls.pdf[arm] = read_pdf(out)
        out = Path(cls.tmp.name) / 'fixture_tags.pdf'
        assert _run_quietly(cls.target.main, ['--fixture-tags', '--dpi', '100', '--out', str(out)]) == 0
        cls.pdf['fixture'] = read_pdf(out)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _tags_in_label_frame(self, pdf, label):
        """``{id: (centre in the label's frame (mm), top direction in it)}``."""
        sx, sy = _mm_per_px(pdf)
        out = {}
        for tid, quad in _aruco_read(_gray(pdf)):
            centre = quad.mean(axis=0)
            u, v = (centre[0] + 0.5) * sx, (centre[1] + 0.5) * sy
            top = (quad[0] + quad[1]) / 2.0 - centre
            self.assertNotIn(tid, out, f'tag {tid} twice')
            out[tid] = (label.frame_of_page(u, v), label.frame_direction_of_page(top[0], top[1]))
        return out

    def test_each_target_carries_its_nine_ids_row_major_top_toward_plus_x(self):
        for arm in ('omx', 'v1'):
            with self.subTest(arm=arm):
                pdf = self.pdf[arm]
                self.assertAlmostEqual(pdf['media_mm'][0], 210.0, delta=0.01)
                self.assertAlmostEqual(pdf['media_mm'][1], 297.0, delta=0.01)
                label = self.target.target_layout(arm).label
                seen = self._tags_in_label_frame(pdf, label)
                expected = {tid: corners for tid, corners in sf.target_tag_corners(sf.ARMS[arm])}
                self.assertEqual(sorted(seen), sorted(expected))
                for tid, ((x, y), (dx, dy)) in seen.items():
                    corners = expected[tid]
                    cx = sum(c[0] for c in corners) / 4.0 * 1000.0
                    cy = sum(c[1] for c in corners) / 4.0 * 1000.0
                    self.assertLessEqual(math.hypot(x - cx, y - cy), 0.3, f'tag {tid}')
                    self.assertLess(abs(math.degrees(math.atan2(dy, dx))), 2.0, f'tag {tid}')
                # the plate's 80 mm and the 20 mm tags at 26 mm pitch, from the contract
                self.assertAlmostEqual(label.size_mm, sf.TARGET['plate_m'] * 1000.0)
                self.assertTrue(any(sf.FIXTURE_ID in t and sf.contract_hash()[:8] in t for t in pdf['texts']))

    def test_the_fixture_sheet_carries_480_and_481_top_toward_plus_x(self):
        pdf = self.pdf['fixture']
        self.assertAlmostEqual(pdf['media_mm'][0], 210.0, delta=0.01)
        self.assertAlmostEqual(pdf['media_mm'][1], 297.0, delta=0.01)
        layout = self.target.fixture_layout()
        found = _aruco_read(_gray(pdf))
        self.assertEqual(sorted(t for t, _q in found), [480, 481])
        sx, sy = _mm_per_px(pdf)
        for tid, quad in found:
            with self.subTest(tag=tid):
                label = layout.labels[tid]
                centre = quad.mean(axis=0)
                u, v = (centre[0] + 0.5) * sx, (centre[1] + 0.5) * sy
                self.assertLessEqual(math.dist(label.frame_of_page(u, v), (0.0, 0.0)), 0.3)
                dx, dy = label.frame_direction_of_page(*((quad[0] + quad[1]) / 2.0 - centre))
                self.assertLess(abs(math.degrees(math.atan2(dy, dx))), 2.0)
                edge = np.linalg.norm(quad[1] - quad[0]) * sx
                self.assertAlmostEqual(edge, sf.FIXTURE_TAG_SIZE_M * 1000.0, delta=0.5)
        self.assertTrue(any(sf.FIXTURE_ID in t and sf.contract_hash()[:8] in t for t in pdf['texts']))

    def test_no_text_or_ruler_covers_a_label(self):
        for arm in ('omx', 'v1'):
            with self.subTest(arm=arm):
                self.assertEqual(self.target.page_conflicts(self.target.target_layout(arm)), [])
        self.assertEqual(self.target.page_conflicts(self.target.fixture_layout()), [])


@unittest.skipUnless(_HAVE_CV2, 'numpy / cv2 (contrib, aruco) not available')
class TheKitFiles(unittest.TestCase):
    """The five PDFs in ``classroom_kit/`` are the generators' output at 300 dpi.

    Judged by what does not move with the contract's hash (WP-11 changes the hash
    on purpose when it enters the measured OMX fingertip offset): the page, the
    tags and the zone's cut. The printed hash is the one of the day they were made.
    """

    def test_the_mats(self):
        mat = _load_tool('generate_scene_mat')
        for arm in ('omx', 'v1'):
            with self.subTest(arm=arm):
                pdf = read_pdf(KIT / f'scene_mat_{arm}.pdf')
                w, h = SHEET_MM[arm]
                self.assertAlmostEqual(pdf['media_mm'][0], w, delta=0.01)
                self.assertAlmostEqual(pdf['media_mm'][1], h, delta=0.01)
                self.assertAlmostEqual(_mm_per_px(pdf)[0], 25.4 / 300, delta=1e-9)
                index = _index_array(pdf)
                sheet = _sheet_mm(arm)
                found = _aruco_read(np.ascontiguousarray(_gray(pdf)[::3, ::3]))   # read at 100 dpi
                expected = {tid: (x * 1000.0, y * 1000.0) for tid, (x, y) in sf.ARMS[arm]['mat_tags_f'].items()}
                self.assertEqual(sorted(t for t, _q in found), sorted(expected))
                for tid, quad in found:
                    col, row = quad.mean(axis=0) * 3.0             # back to the 300-dpi grid
                    x, y = _f_from_px(pdf, sheet, col, row)
                    self.assertLessEqual(math.hypot(x - expected[tid][0], y - expected[tid][1]), 0.5)
                for r, az, inside, what in _zone_probes(arm):
                    x, y = r * 1000.0 * math.cos(math.radians(az)), r * 1000.0 * math.sin(math.radians(az))
                    row, col = _px_of_f(pdf, sheet, x, y)
                    self.assertEqual(index[row, col] == mat.ZONE, inside, what)
                title = ' '.join(pdf['texts'])
                self.assertIn(sf.FIXTURE_ID, title)
                self.assertRegex(title, r'Prüfsumme [0-9a-f]{8}\b')

    def test_the_targets_and_the_fixture_tags(self):
        for name, ids in (('calibration_target_omx', range(520, 529)), ('calibration_target_v1', range(500, 509)),
                          ('fixture_tags', (480, 481))):
            with self.subTest(file=name):
                pdf = read_pdf(KIT / f'{name}.pdf')
                self.assertAlmostEqual(pdf['media_mm'][0], 210.0, delta=0.01)
                self.assertAlmostEqual(pdf['media_mm'][1], 297.0, delta=0.01)
                self.assertAlmostEqual(_mm_per_px(pdf)[0], 25.4 / 300, delta=1e-9)
                self.assertEqual(sorted(t for t, _q in _aruco_read(_gray(pdf))), list(ids))


class GenerateApriltagsRefusesReservedIds(unittest.TestCase):
    """The calibration's tags (400..586) are never printed as object tags (spec 40 §3)."""

    def _run(self, *args, out):
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        return subprocess.run([sys.executable, str(APRILTAGS), *args, '--out', str(out)],
                              capture_output=True, text=True, encoding='utf-8', env=env, timeout=120)

    def test_id_400_is_refused_in_german(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'tags.pdf'
            proc = self._run('--start-id', '400', '--count', '1', out=out)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('Tag-ID 400 ist für die Kalibrierung reserviert (400 bis 586).', proc.stderr)
            self.assertFalse(out.exists())
            self.assertFalse(out.with_suffix('.png').exists())

    def test_allow_reserved_does_not_open_the_calibration_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'tags.pdf'
            proc = self._run('--start-id', '395', '--count', '10', '--allow-reserved', out=out)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('Tag-IDs 400 bis 404 sind für die Kalibrierung reserviert (400 bis 586).', proc.stderr)
            self.assertFalse(out.exists())

    def test_the_range_is_the_contracts(self):
        tags = _load_tool('generate_apriltags')
        self.assertEqual(tags.reserved_ids([0, 399, 400, 586, 587]), [400, 586])


if __name__ == '__main__':
    unittest.main()

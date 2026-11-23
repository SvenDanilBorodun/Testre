#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""Render the scene mat of the tower kit from the fixture contract (spec 40 WP-03, 41 H4).

ONE SHEET PER ARM, PLOTTED BY A PRINT SHOP (decision D26). The PDF page IS the
sheet (V1 470 x 830 mm, OMX 380 x 660 mm): the print shop plots it at 100 %,
laminates it matte and trims it to the outline, the notch around the robot's
base plate included. A print split over several school-printer pages is not a
supported path, so this tool has no option for one.

EVERYTHING COMES FROM THE CONTRACT (``physical_ai_server/workflow/scene_fixture.py``):
the sheet ``mat_sheet_f``, the notch ``MAT_NOTCH_F``, the field tags
``mat_tags_f`` (tag36h11, ``MAT_TAG_SIZE_M`` on a white card of ``MAT_CARD_M``,
every tag upright with its top toward +x_F, the orientation
``scene_fixture.tag_corners_xy`` assumes), and the printed work zone. The zone
is the ring sector between ``zone_inner_m`` and ``zone_outer_m`` from
``zone_sector_deg[0]`` to the tower-side edge ``scene_fixture.zone_az_hi`` (D66:
the corner next to the tower is cut out). This file reads the cut ONLY through
``zone_az_hi`` and checks every outline vertex with ``in_zone``; it names no
knot of the cut, so a cut that grows (D69) reaches the print by regenerating.

The page is frame F seen from above: x_F to the right, y_F up the page, the
robot's notch at the left edge. The graphics are one palette raster at
``--dpi`` (default 300) covering the page; the texts are PDF text in the
built-in Helvetica (WinAnsi, so ä ö ü ß print as they are). The PDF is written
here without a PDF library and carries no date: the same contract gives the
same bytes with the same zlib and OpenCV (the kit files are made with CI's
pins; OpenCV 4.13 fills about 0.002 % fewer zone-edge pixels than 4.10).

Printed besides the tags and the zone: the arm, the contract id and the first
8 hex of ``contract_hash()`` (title block), two 100 mm ruler marks (x and y),
„Matte bis zum Anschlag an die Grundplatte schieben“, „Marker nicht abdecken“,
the notch's cut line and, inside the cut-out, the joint-1 axis (for checking a
proof against the contract, RG-MAT).

Usage:
    python3 tools/generate_scene_mat.py --arm v1            # -> classroom_kit/scene_mat_v1.pdf
    python3 tools/generate_scene_mat.py --arm omx --dpi 600 --out /tmp/omx.pdf
"""

from __future__ import annotations

import argparse
import importlib.util
import math
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

REPO = Path(__file__).resolve().parents[1]
KIT_DIR = REPO / 'classroom_kit'
CONTRACT = REPO / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server' / 'workflow' / 'scene_fixture.py'


def _load_contract():
    """The contract module from its file: it is standard library only, so this needs
    neither the server package nor whatever another importer registered under its name."""
    spec = importlib.util.spec_from_file_location('edubotics_scene_fixture', CONTRACT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sf = _load_contract()

ARMS = ('omx', 'v1')
ARM_NAMES = {'omx': 'OMX', 'v1': 'V1'}
DPI_DEFAULT = 300
DPI_RANGE = (72, 1200)

# Palette indices of the raster (the PDF stores the image as /Indexed).
PAPER, INK, ZONE, LINE, CARD = 0, 1, 2, 3, 4
PALETTE = ((255, 255, 255), (0, 0, 0), (196, 230, 201), (64, 64, 64), (206, 206, 206))
TEXT_INK = (28, 33, 40)
TEXT_SOFT = (90, 98, 108)

_M_TO_MM = 1000.0
_STEP_MM = 0.5            # the zone outline's sampling: every 0.5 mm of its edge (40 WP-03)
_LINE_MM = 0.3            # cut line, card outline
_MARGIN_MM = 5.0          # sheet edge to the nearest text
_CLEAR_MM = 2.0           # text and ruler boxes keep this much from a card, the zone and the notch
_PT_PER_MM = 72.0 / 25.4


# ── text metrics (Adobe's Helvetica and Helvetica-Bold, 1/1000 em) ──────────
# Codes 32..126 in order; used only to place text, never written to the PDF
# (a viewer takes the widths of a built-in font from the font itself).
_HELVETICA = (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
)
_HELVETICA_BOLD = (
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611,
    975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556,
    333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
    611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584,
)
_WIDTH_EXTRA = {   # (regular, bold) of the WinAnsi letters outside ASCII that these sheets print
    'ä': (556, 556), 'ö': (556, 611), 'ü': (556, 611), 'Ä': (667, 722), 'Ö': (778, 778),
    'Ü': (722, 722), 'ß': (611, 611), '„': (333, 500), '“': (333, 500), '–': (556, 556),
    '·': (278, 278), '×': (584, 584), '°': (400, 400),
}
_ASCENT, _DESCENT = 0.93, 0.22      # of the font size: accented capitals included


def text_width_mm(text, size_mm, bold=False):
    """The printed width of ``text`` in Helvetica(-Bold) at ``size_mm``; a letter the
    tables do not know is refused (it would not be printable in WinAnsi either)."""
    table = _HELVETICA_BOLD if bold else _HELVETICA
    total = 0
    for ch in text:
        code = ord(ch)
        if 32 <= code <= 126:
            total += table[code - 32]
        elif ch in _WIDTH_EXTRA:
            total += _WIDTH_EXTRA[ch][1 if bold else 0]
        else:
            raise ValueError(f'no width for {ch!r} in {text!r}')
    return total * size_mm / 1000.0


@dataclass(frozen=True)
class PageText:
    """One line of PDF text, left end of its baseline at page millimetres (u right, v down)."""
    u: float
    v: float
    size_mm: float
    text: str
    bold: bool = False
    colour: tuple = TEXT_INK

    @property
    def width_mm(self):
        return text_width_mm(self.text, self.size_mm, self.bold)

    def box(self):
        """``(u0, v0, u1, v1)`` the line covers on the page."""
        return (self.u, self.v - _ASCENT * self.size_mm, self.u + self.width_mm, self.v + _DESCENT * self.size_mm)


# ── frames and the raster ────────────────────────────────────────────────────
class Frame:
    """A right-handed frame on the page seen from its printed side (z toward the
    reader): origin at page millimetres (u right, v down), x pointing ``'right'``
    or ``'up'``, y a quarter turn counter-clockwise from x."""

    def __init__(self, origin_u, origin_v, x_dir):
        self.origin = (float(origin_u), float(origin_v))
        if x_dir == 'right':
            self.ex, self.ey = (1.0, 0.0), (0.0, -1.0)
        elif x_dir == 'up':
            self.ex, self.ey = (0.0, -1.0), (-1.0, 0.0)
        else:
            raise ValueError(x_dir)

    def page_of(self, x, y):
        ou, ov = self.origin
        return (ou + x * self.ex[0] + y * self.ey[0], ov + x * self.ex[1] + y * self.ey[1])

    def frame_of_page(self, u, v):
        du, dv = u - self.origin[0], v - self.origin[1]
        return self.frame_direction_of_page(du, dv)

    def frame_direction_of_page(self, du, dv):
        return (du * self.ex[0] + dv * self.ex[1], du * self.ey[0] + dv * self.ey[1])

    def page_box(self, x0, y0, x1, y1):
        (ua, va), (ub, vb) = self.page_of(x0, y0), self.page_of(x1, y1)
        return min(ua, ub), min(va, vb), max(ua, ub), max(va, vb)

    def frame_box(self, u0, v0, u1, v1):
        (xa, ya), (xb, yb) = self.frame_of_page(u0, v0), self.frame_of_page(u1, v1)
        return min(xa, xb), min(ya, yb), max(xa, xb), max(ya, yb)


class Raster:
    """A palette-index image of the page window ``[u0, u1) x [v0, v1)`` (page mm):
    pixel (row, col) covers ``[u0 + col/k, u0 + (col+1)/k) x [v0 + row/k, …)``,
    ``k = dpi / 25.4``. A rectangle colours exactly the pixels whose centres lie
    inside it; a polygon is OpenCV's ``fillPoly`` (its boundary pixels included,
    at most one pixel beyond the exact edge)."""

    def __init__(self, dpi, u0, v0, u1, v1):
        self.dpi = dpi
        self.k = dpi / 25.4
        self.u0, self.v0 = float(u0), float(v0)
        width = max(1, math.ceil((u1 - u0) * self.k - 1e-9))
        height = max(1, math.ceil((v1 - v0) * self.k - 1e-9))
        self.index = np.zeros((height, width), dtype=np.uint8)
        self.frame = None

    def rect(self, u0, v0, u1, v1, colour):
        h, w = self.index.shape
        c0 = max(0, math.ceil((u0 - self.u0) * self.k - 0.5))
        c1 = min(w, math.ceil((u1 - self.u0) * self.k - 0.5))
        r0 = max(0, math.ceil((v0 - self.v0) * self.k - 0.5))
        r1 = min(h, math.ceil((v1 - self.v0) * self.k - 0.5))
        if c1 > c0 and r1 > r0:
            self.index[r0:r1, c0:c1] = colour

    def polygon(self, points_uv, colour):
        shift = 8
        scale = float(1 << shift)
        pts = np.array([[round(((u - self.u0) * self.k - 0.5) * scale),
                         round(((v - self.v0) * self.k - 0.5) * scale)] for u, v in points_uv], dtype=np.int32)
        cv2.fillPoly(self.index, [pts], int(colour), lineType=cv2.LINE_8, shift=shift)

    def frame_rect(self, frame, x0, y0, x1, y1, colour):
        self.rect(*frame.page_box(x0, y0, x1, y1), colour)

    def pixel_of_page(self, u, v):
        """``(row, col)`` of the pixel holding page point (u, v)."""
        return math.floor((v - self.v0) * self.k), math.floor((u - self.u0) * self.k)

    def pixel_of_f(self, x_mm, y_mm):
        """``(row, col)`` of the pixel holding the point (mm) of the raster's frame."""
        return self.pixel_of_page(*self.frame.page_of(x_mm, y_mm))


def _tag_bits(tag_id):
    """The 8 x 8 modules of a tag36h11 tag (black border included), row 0 on top; True = black."""
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36H11)
    return dictionary.generateImageMarker(int(tag_id), 8, borderBits=1) < 128


def draw_tag(raster, frame, tag_id, cx, cy, size_mm):
    """A tag36h11 tag of black-square edge ``size_mm`` centred at (cx, cy) of ``frame``,
    upright with its top toward +x of the frame (its right toward -y)."""
    m = size_mm / 8.0
    bits = _tag_bits(tag_id)
    for r in range(8):
        for c in range(8):
            if bits[r, c]:
                raster.frame_rect(frame, cx + (3 - r) * m, cy + (3 - c) * m,
                                  cx + (4 - r) * m, cy + (4 - c) * m, INK)


def draw_outline(raster, frame, x0, y0, x1, y1, width_mm, colour):
    """The four edges of a rectangle of ``frame`` as lines of ``width_mm``, centred on the edges."""
    h = width_mm / 2.0
    raster.frame_rect(frame, x0 - h, y0 - h, x1 + h, y0 + h, colour)
    raster.frame_rect(frame, x0 - h, y1 - h, x1 + h, y1 + h, colour)
    raster.frame_rect(frame, x0 - h, y0 - h, x0 + h, y1 + h, colour)
    raster.frame_rect(frame, x1 - h, y0 - h, x1 + h, y1 + h, colour)


@dataclass(frozen=True)
class Ruler:
    """A 100 mm ruler mark in a frame: its bar along ``axis`` from ``start`` (the 0 mark,
    on the bar's centre line), the ticks toward ``side`` (+1: +y for an x ruler, +x for
    a y ruler). End ticks 6 mm, the 50 mm tick 4 mm, the others every 10 mm 2.5 mm."""
    axis: str
    start: tuple
    side: int
    length: float = 100.0
    bar_mm: float = 0.4
    tick_mm: float = 0.4

    def _ticks(self):
        out = []
        for mark in range(0, int(self.length) + 1, 10):
            reach = 6.0 if mark in (0, int(self.length)) else (4.0 if mark * 2 == int(self.length) else 2.5)
            out.append((float(mark), reach))
        return out

    def rects(self):
        """The bar and the ticks as rectangles ``(x0, y0, x1, y1)`` of the frame."""
        sx, sy = self.start
        half_bar, half_tick = self.bar_mm / 2.0, self.tick_mm / 2.0
        if self.axis == 'x':
            out = [(sx - half_tick, sy - half_bar, sx + self.length + half_tick, sy + half_bar)]
            for mark, reach in self._ticks():
                y1 = sy + self.side * reach
                out.append((sx + mark - half_tick, min(sy, y1), sx + mark + half_tick, max(sy, y1)))
        else:
            out = [(sx - half_bar, sy - half_tick, sx + half_bar, sy + self.length + half_tick)]
            for mark, reach in self._ticks():
                x1 = sx + self.side * reach
                out.append((min(sx, x1), sy + mark - half_tick, max(sx, x1), sy + mark + half_tick))
        return out

    def box(self):
        rects = self.rects()
        return (min(r[0] for r in rects), min(r[1] for r in rects),
                max(r[2] for r in rects), max(r[3] for r in rects))

    def end_probe_mm(self):
        """Two points of the frame on a line that crosses the two end ticks and nothing
        else of the ruler (5 mm from the bar: past the 50 mm tick, short of the ends)."""
        sx, sy = self.start
        if self.axis == 'x':
            y = sy + self.side * 5.0
            return (sx - 3.0, y), (sx + self.length + 3.0, y)
        x = sx + self.side * 5.0
        return (x, sy - 3.0), (x, sy + self.length + 3.0)

    def draw(self, raster, frame):
        for rect in self.rects():
            raster.frame_rect(frame, *rect, LINE)


# ── the PDF ──────────────────────────────────────────────────────────────────
def _pdf_string(text):
    out = []
    for b in text.encode('cp1252'):
        if b in (0x28, 0x29, 0x5C):
            out.append('\\' + chr(b))
        elif 32 <= b < 127:
            out.append(chr(b))
        else:
            out.append(f'\\{b:03o}')
    return ''.join(out)


def write_pdf(path, width_mm, height_mm, raster, texts, title):
    """One page of ``width_mm`` x ``height_mm`` (its MediaBox), the raster as an
    /Indexed image anchored at the page's top-left corner at its own dpi (what
    overhangs the page by less than a pixel is clipped), then the texts. The
    content stream precedes the image, uncompressed; no date, so the same input
    gives the same bytes."""
    page_w, page_h = width_mm * _PT_PER_MM, height_mm * _PT_PER_MM
    rows, cols = raster.index.shape
    img_w, img_h = cols * 72.0 / raster.dpi, rows * 72.0 / raster.dpi
    content = ['q', f'{img_w:.6f} 0 0 {img_h:.6f} 0 {page_h - img_h:.6f} cm', '/Im0 Do', 'Q']
    for t in texts:
        r, g, b = (c / 255.0 for c in t.colour)
        content.append(f'BT /{"F2" if t.bold else "F1"} {t.size_mm * _PT_PER_MM:.4f} Tf '
                       f'{r:.4f} {g:.4f} {b:.4f} rg 1 0 0 1 {t.u * _PT_PER_MM:.4f} '
                       f'{(height_mm - t.v) * _PT_PER_MM:.4f} Tm ({_pdf_string(t.text)}) Tj ET')
    content_bytes = ('\n'.join(content) + '\n').encode('ascii')
    palette_hex = ''.join(f'{r:02x}{g:02x}{b:02x}' for r, g, b in PALETTE)
    image_bytes = zlib.compress(raster.index.tobytes(), 9)
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        (f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_w:.4f} {page_h:.4f}] '
         '/Resources << /XObject << /Im0 5 0 R >> /Font << /F1 6 0 R /F2 7 0 R >> '
         '/ProcSet [/PDF /Text /ImageI] >> /Contents 4 0 R >>').encode('ascii'),
        f'<< /Length {len(content_bytes)} >>\nstream\n'.encode('ascii') + content_bytes + b'endstream',
        (f'<< /Type /XObject /Subtype /Image /Width {cols} /Height {rows} '
         f'/ColorSpace [/Indexed /DeviceRGB {len(PALETTE) - 1} <{palette_hex}>] /BitsPerComponent 8 '
         f'/Filter /FlateDecode /Length {len(image_bytes)} >>\nstream\n').encode('ascii')
        + image_bytes + b'\nendstream',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>',
        f'<< /Title ({_pdf_string(title)}) /Producer (EduBotics kit tools) >>'.encode('ascii'),
    ]
    out = bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f'{number} 0 obj\n'.encode('ascii') + body + b'\nendobj\n'
    xref = len(out)
    out += f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n'.encode('ascii')
    out += b''.join(f'{offset:010d} 00000 n \n'.encode('ascii') for offset in offsets)
    out += (f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info {len(objects)} 0 R >>\n'
            f'startxref\n{xref}\n%%EOF\n').encode('ascii')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path


# ── the mat ──────────────────────────────────────────────────────────────────
def _arc_mm(r_m, az0_deg, az1_deg):
    """Points (mm) of the arc of radius ``r_m`` from az0 to az1, at most 0.5 mm apart."""
    r = r_m * _M_TO_MM
    n = max(1, math.ceil(r * abs(math.radians(az1_deg - az0_deg)) / _STEP_MM))
    out = []
    for i in range(n + 1):
        a = math.radians(az0_deg + (az1_deg - az0_deg) * i / n)
        out.append((r * math.cos(a), r * math.sin(a)))
    return out


def _edge_point_mm(spec, r_m):
    a = math.radians(sf.zone_az_hi(spec, r_m))
    return (r_m * _M_TO_MM * math.cos(a), r_m * _M_TO_MM * math.sin(a))


def _edge_mm(spec, r0_m, r1_m):
    """The tower-side edge (r, zone_az_hi(r)) from r0 to r1, consecutive points at most
    0.5 mm apart (radius steps halved wherever the edge runs steeply in azimuth)."""
    n = max(1, math.ceil((r1_m - r0_m) * _M_TO_MM / _STEP_MM))
    radii = [r0_m + (r1_m - r0_m) * i / n for i in range(n + 1)]
    out = [_edge_point_mm(spec, radii[0])]
    for ra, rb in zip(radii, radii[1:]):
        out.extend(_refined(spec, ra, out[-1], rb, _edge_point_mm(spec, rb), depth=40))
    return out


def _refined(spec, ra, pa, rb, pb, depth):
    if math.dist(pa, pb) <= _STEP_MM:
        return [pb]
    if depth == 0:
        raise RuntimeError(f'the zone edge does not converge between r {ra} and {rb} m')
    rm = (ra + rb) / 2.0
    pm = _edge_point_mm(spec, rm)
    return _refined(spec, ra, pa, rm, pm, depth - 1) + _refined(spec, rm, pm, rb, pb, depth - 1)


def zone_outline_mm(spec):
    """The printed zone as one polygon in F (mm): the inner arc from the sector's low
    edge to the cut, the tower-side edge outward along ``zone_az_hi``, the outer arc
    back, closed along the low edge. Every vertex is checked with ``in_zone``."""
    r_in, r_out = spec['zone_inner_m'], spec['zone_outer_m']
    low = spec['zone_sector_deg'][0]
    pts = _arc_mm(r_in, low, sf.zone_az_hi(spec, r_in))
    pts += _edge_mm(spec, r_in, r_out)[1:-1]
    pts += _arc_mm(r_out, sf.zone_az_hi(spec, r_out), low)
    for x, y in pts:
        if not sf.in_zone(spec, x / _M_TO_MM, y / _M_TO_MM):
            raise RuntimeError(f'zone outline point ({x:.3f}, {y:.3f}) mm is not in_zone')
    return pts


@dataclass
class MatLayout:
    """Everything printed on one arm's mat, in F millimetres (texts in page millimetres)."""
    arm: str
    spec: dict
    sheet: tuple                 # (x0, y0, x1, y1)
    frame: Frame
    notch: tuple                 # (x_max, y_abs)
    tags: dict                   # id -> (x, y)
    tag_mm: float
    card_mm: float
    zone: list
    rulers: list
    texts: list = field(default_factory=list)
    notch_texts: list = field(default_factory=list)
    title_lines: tuple = ()

    @property
    def page_mm(self):
        x0, y0, x1, y1 = self.sheet
        return x1 - x0, y1 - y0


def _hash8():
    return sf.contract_hash()[:8]


def mat_layout(arm):
    spec = sf.ARMS[arm]
    (x0, y0), (x1, y1) = spec['mat_sheet_f']
    sheet = (x0 * _M_TO_MM, y0 * _M_TO_MM, x1 * _M_TO_MM, y1 * _M_TO_MM)
    sx0, sy0, sx1, sy1 = sheet
    frame = Frame(-sx0, sy1, 'right')
    page_w, page_h = sx1 - sx0, sy1 - sy0
    layout = MatLayout(
        arm=arm, spec=spec, sheet=sheet, frame=frame,
        notch=(sf.MAT_NOTCH_F['x_max'] * _M_TO_MM, sf.MAT_NOTCH_F['y_abs'] * _M_TO_MM),
        tags={tid: (x * _M_TO_MM, y * _M_TO_MM) for tid, (x, y) in spec['mat_tags_f'].items()},
        tag_mm=sf.MAT_TAG_SIZE_M * _M_TO_MM, card_mm=sf.MAT_CARD_M * _M_TO_MM,
        zone=zone_outline_mm(spec),
        rulers=[
            # x: across the far corner on the -y side; y: along the far edge, centred on the axis
            Ruler('x', (sx1 - 15.0 - 100.0, sy0 + 12.0), +1),
            Ruler('y', (sx1 - 10.0, -50.0), -1),
        ],
    )

    def at(x, y):
        return frame.page_of(x, y)

    def right_aligned(x_right, y_base, size, text, bold=False):
        u, v = at(x_right, y_base)
        return PageText(u - text_width_mm(text, size, bold), v, size, text, bold)

    # title block: top right corner (the far +y corner), right-aligned, lines going down
    title = (
        (8.0, True, f'Szenen-Matte {ARM_NAMES[arm]}'),
        (4.5, False, f'Kit-Vertrag {sf.FIXTURE_ID} · Prüfsumme {_hash8()}'),
        (4.5, False, f'Roboterprofile: {", ".join(spec["profiles"])}'),
        (3.8, False, f'Blatt {page_w:g} × {page_h:g} mm · Druck 100 % · matt laminiert'),
        (3.8, False, 'Beide 100-mm-Marken mit dem Lineal nachmessen.'),
    )
    y = sy1 - _MARGIN_MM
    for size, bold, text in title:
        y -= _ASCENT * size
        layout.texts.append(right_aligned(sx1 - _MARGIN_MM, y, size, text, bold))
        y -= _DESCENT * size + 1.6
    layout.title_lines = tuple(text for _s, _b, text in title)

    ruler_x, ruler_y = layout.rulers
    mid = ruler_x.start[0] + ruler_x.length / 2.0
    label = '100 mm'
    u, v = at(mid, ruler_x.start[1] + 8.5)
    layout.texts.append(PageText(u - text_width_mm(label, 3.5) / 2.0, v, 3.5, label))
    layout.texts.append(right_aligned(sx1 - _MARGIN_MM, ruler_x.start[1] + 20.0, 4.5,
                                      'Marker nicht abdecken', bold=True))
    u, v = at(ruler_y.start[0] - 7.0, 0.0)          # beside the 50 mm tick, the digits centred on it
    layout.texts.append(PageText(u - text_width_mm(label, 3.5), v + 0.36 * 3.5, 3.5, label))

    # beside the notch, on the tower side (outside the zone, which leaves that corner out)
    for k, (bold, text) in enumerate(((True, 'Matte bis zum Anschlag'), (False, 'an die Grundplatte schieben'))):
        u, v = at(sx0 + _MARGIN_MM, 140.0 - 6.0 * k)
        layout.texts.append(PageText(u, v, 4.2, text, bold))

    # inside the cut-out: for the print shop and for checking a proof (cut away afterwards)
    for k, text in enumerate(('Ausschnitt für die Grundplatte:', 'entlang der Linie ausschneiden.')):
        u, v = at(sx0 + 3.0, 60.0 - 5.0 * k)
        layout.notch_texts.append(PageText(u, v, 3.2, text, colour=TEXT_SOFT))
    u, v = at(8.0, 2.0)
    layout.notch_texts.append(PageText(u, v, 2.6, 'Achse Gelenk 1', colour=TEXT_SOFT))
    return layout


def _box_hits_box(a, b, clear=0.0):
    return a[0] < b[2] + clear and b[0] < a[2] + clear and a[1] < b[3] + clear and b[1] < a[3] + clear


def _box_in_zone(spec, box, clear):
    """True when any point of ``box`` (F mm) grown by ``clear`` lies in the zone (1 mm grid)."""
    x0, y0, x1, y1 = box[0] - clear, box[1] - clear, box[2] + clear, box[3] + clear
    nx, ny = max(1, math.ceil(x1 - x0)), max(1, math.ceil(y1 - y0))
    for i in range(nx + 1):
        for j in range(ny + 1):
            if sf.in_zone(spec, (x0 + (x1 - x0) * i / nx) / _M_TO_MM, (y0 + (y1 - y0) * j / ny) / _M_TO_MM):
                return True
    return False


def layout_conflicts(layout):
    """Every way a text or a ruler of the mat sits where it must not: off the sheet,
    on a tag's card, in the zone, on the notch, on another text or ruler. ``[]`` = none."""
    out = []
    sx0, sy0, sx1, sy1 = layout.sheet
    notch_x, notch_y = layout.notch
    notch = (sx0, -notch_y, notch_x, notch_y)
    half = layout.card_mm / 2.0
    cards = {tid: (x - half, y - half, x + half, y + half) for tid, (x, y) in layout.tags.items()}
    items = [(f'text „{t.text}“', layout.frame.frame_box(*t.box())) for t in layout.texts]
    items += [(f'ruler {r.axis}', r.box()) for r in layout.rulers]
    for name, box in items:
        if box[0] < sx0 + 2.0 or box[1] < sy0 + 2.0 or box[2] > sx1 - 2.0 or box[3] > sy1 - 2.0:
            out.append(f'{name} leaves the sheet')
        for tid, card in cards.items():
            if _box_hits_box(box, card, _CLEAR_MM):
                out.append(f'{name} touches the card of tag {tid}')
        if _box_hits_box(box, notch, _CLEAR_MM):
            out.append(f'{name} touches the notch')
        if _box_in_zone(layout.spec, box, _CLEAR_MM):
            out.append(f'{name} touches the zone')
    for i, (name_a, box_a) in enumerate(items):
        for name_b, box_b in items[i + 1:]:
            if _box_hits_box(box_a, box_b):
                out.append(f'{name_a} overlaps {name_b}')
    for t in layout.notch_texts:
        box = layout.frame.frame_box(*t.box())
        if not (box[0] >= sx0 + 1.0 and box[2] <= notch_x - 1.0 and box[1] >= -notch_y + 1.0
                and box[3] <= notch_y - 1.0):
            out.append(f'text „{t.text}“ leaves the cut-out')
    return out


def render_mat(arm, dpi=DPI_DEFAULT, window=None, layout=None):
    """The mat's raster at ``dpi``: the whole sheet, or only ``window`` =
    ``((x0, y0), (x1, y1))`` in F millimetres, drawn by the same code."""
    layout = layout or mat_layout(arm)
    frame = layout.frame
    sx0, sy0, sx1, sy1 = layout.sheet
    if window is None:
        raster = Raster(dpi, 0.0, 0.0, sx1 - sx0, sy1 - sy0)
    else:
        (wx0, wy0), (wx1, wy1) = window
        raster = Raster(dpi, *frame.page_box(wx0, wy0, wx1, wy1))
    raster.frame = frame
    raster.polygon([frame.page_of(x, y) for x, y in layout.zone], ZONE)
    half = layout.card_mm / 2.0
    for x, y in layout.tags.values():
        draw_outline(raster, frame, x - half, y - half, x + half, y + half, _LINE_MM, CARD)
    # the cut-out's three edges (the rear one is the sheet's own edge)
    notch_x, notch_y = layout.notch
    h = _LINE_MM / 2.0
    raster.frame_rect(frame, sx0, notch_y - h, notch_x + h, notch_y + h, LINE)
    raster.frame_rect(frame, sx0, -notch_y - h, notch_x + h, -notch_y + h, LINE)
    raster.frame_rect(frame, notch_x - h, -notch_y - h, notch_x + h, notch_y + h, LINE)
    raster.frame_rect(frame, -6.0, -h, 6.0, h, LINE)          # the joint-1 axis
    raster.frame_rect(frame, -h, -6.0, h, 6.0, LINE)
    for ruler in layout.rulers:
        ruler.draw(raster, frame)
    for tid, (x, y) in layout.tags.items():
        draw_tag(raster, frame, tid, x, y, layout.tag_mm)
    return raster


def default_out(arm):
    return KIT_DIR / f'scene_mat_{arm}.pdf'


def write_mat(arm, out, dpi=DPI_DEFAULT):
    layout = mat_layout(arm)
    problems = layout_conflicts(layout)
    if problems:
        raise RuntimeError('mat layout: ' + '; '.join(problems))
    raster = render_mat(arm, dpi, layout=layout)
    page_w, page_h = layout.page_mm
    return write_pdf(out, page_w, page_h, raster, layout.texts + layout.notch_texts,
                     f'EduBotics Szenen-Matte {ARM_NAMES[arm]} ({sf.FIXTURE_ID})')


def build_parser():
    parser = argparse.ArgumentParser(
        description='Render the tower kit scene mat of one arm as a print-shop PDF '
                    '(page = sheet, plotted at 100 %).')
    parser.add_argument('--arm', choices=ARMS, required=True, help='which arm the mat is for')
    parser.add_argument('--dpi', type=int, default=DPI_DEFAULT,
                        help=f'raster resolution of the graphics (default {DPI_DEFAULT})')
    parser.add_argument('--out', type=Path, default=None,
                        help='output PDF (default classroom_kit/scene_mat_<arm>.pdf)')
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not DPI_RANGE[0] <= args.dpi <= DPI_RANGE[1]:
        parser.error(f'--dpi must lie in {DPI_RANGE[0]}..{DPI_RANGE[1]}')
    out = write_mat(args.arm, args.out or default_out(args.arm), args.dpi)
    w, h = mat_layout(args.arm).page_mm
    print(f'Wrote {out} ({w:g} x {h:g} mm, {args.dpi} dpi, contract {sf.FIXTURE_ID} {_hash8()})')
    return 0


if __name__ == '__main__':
    sys.exit(main())

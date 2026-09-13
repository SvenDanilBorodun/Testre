#!/usr/bin/env python3
"""Render the AprilTag sheet (tag36h11) for the EduBotics classroom kit.

CATALOG-DRIVEN BY DEFAULT: with no arguments the sheet renders a precision
DIN A4 printable sheet containing the fixed fleet object tags (Cube IDs #20 & #21)
with 30 mm x 30 mm cube top-face cutouts, 24 mm tags, 100 mm scale check ruler,
multiple spares, and mounting instructions.

Manual mode (spare/extra tags): pass ``--start-id`` and ``--count`` to render
an arbitrary id range instead; ``--size-mm`` overrides the tag size in both
modes. A manual range that collides with catalog-reserved ids is REFUSED (an
unlabelled duplicate of a fleet-object tag in circulation breaks the
detector's id->type mapping); pass ``--allow-reserved`` to print the
colliding cells labelled with their owning type instead.

Usage:
    python tools/generate_apriltags.py                       # catalog sheet (A4 PDF)
    python tools/generate_apriltags.py --start-id 40 --count 10 --size-mm 24

Print at 100% scale ("Tatsächliche Größe" / "Actual Size", never fit-to-page)
and measure the 100 mm check line with a physical ruler before cutting. Cut
along the 30x30 mm lines KEEPING the white quiet zone around each black square
(the detector requires it). Glue each tag FLAT on the cube's TOP face.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


QUIET_ZONE_MM = 8.0
TAGS_PER_ROW = 4
TAG_FAMILY = cv2.aruco.DICT_APRILTAG_36H11
DPI = 300
_PX_PER_MM = DPI / 25.4

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SERVER_PKG_ROOT = _REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server'

# ── Color Palette (BGR for OpenCV, RGB for PIL) ────────────────────────────
def _c(r: int, g: int, b: int) -> tuple[int, int, int]:
    return (b, g, r)

PAPER       = _c(255, 255, 255)
INK         = _c(28, 33, 40)
INK_SOFT    = _c(85, 95, 105)
INK_FAINT   = _c(180, 186, 194)
LINE_GREY   = _c(215, 220, 226)

PRIMARY     = _c(15, 76, 129)        # technical blue
AMBER_BG    = _c(254, 243, 199)      # warm amber highlight
AMBER_EDGE  = _c(217, 119, 6)        # vibrant amber
AMBER_DEEP  = _c(180, 83, 9)

BLUE_BG     = _c(224, 238, 255)      # light blue highlight
BLUE_EDGE   = _c(37, 99, 235)        # royal blue
BLUE_DEEP   = _c(29, 78, 216)

GREEN_BG    = _c(220, 245, 225)
GREEN_EDGE  = _c(22, 101, 52)        # forest green

_FACES_REG = (
    '/System/Library/Fonts/Supplemental/Arial.ttf',
    '/System/Library/Fonts/Helvetica.ttc',
    r'C:\Windows\Fonts\arial.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
)
_FACES_BLD = (
    '/System/Library/Fonts/Supplemental/Arial Bold.ttf',
    '/System/Library/Fonts/Helvetica.ttc',
    r'C:\Windows\Fonts\arialbd.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
)


class Canvas:
    """Millimetre-native canvas backed by an OpenCV BGR image and PIL text."""

    def __init__(self, w_mm: float, h_mm: float, dpi: int = DPI):
        self.dpi = dpi
        self.k = dpi / 25.4
        self.w_mm = w_mm
        self.h_mm = h_mm
        self.W = int(round(w_mm * self.k))
        self.H = int(round(h_mm * self.k))
        self.img = np.full((self.H, self.W, 3), 255, dtype=np.uint8)
        self.text_items: list[tuple] = []
        self.rotated_texts: list[tuple] = []

    def mm(self, v: float) -> int:
        return int(round(v * self.k))

    def pt(self, x_mm: float, y_mm: float) -> tuple[int, int]:
        return (int(round(x_mm * self.k)), int(round(y_mm * self.k)))

    def line(self, p0: tuple[float, float], p1: tuple[float, float],
             col: tuple[int, int, int], w_mm: float = 0.3, aa: bool = True):
        cv2.line(self.img, self.pt(*p0), self.pt(*p1), col,
                 max(1, self.mm(w_mm)), cv2.LINE_AA if aa else cv2.LINE_8)

    def dash(self, p0: tuple[float, float], p1: tuple[float, float],
             col: tuple[int, int, int], w_mm: float = 0.3, on_mm: float = 3.0,
             off_mm: float = 2.0):
        a = np.array(p0, float)
        b = np.array(p1, float)
        L = float(np.linalg.norm(b - a))
        if L < 1e-3:
            return
        u = (b - a) / L
        t = 0.0
        while t < L:
            s0 = a + u * t
            s1 = a + u * min(t + on_mm, L)
            self.line((float(s0[0]), float(s0[1])), (float(s1[0]), float(s1[1])), col, w_mm)
            t += on_mm + off_mm

    def dash_rect(self, p0: tuple[float, float], p1: tuple[float, float],
                  col: tuple[int, int, int], w_mm: float = 0.3, on_mm: float = 3.0,
                  off_mm: float = 2.0):
        x0, y0 = p0
        x1, y1 = p1
        self.dash((x0, y0), (x1, y0), col, w_mm, on_mm, off_mm)
        self.dash((x1, y0), (x1, y1), col, w_mm, on_mm, off_mm)
        self.dash((x1, y1), (x0, y1), col, w_mm, on_mm, off_mm)
        self.dash((x0, y1), (x0, y0), col, w_mm, on_mm, off_mm)

    def rect(self, p0: tuple[float, float], p1: tuple[float, float],
              fill: tuple[int, int, int] | None = None,
              edge: tuple[int, int, int] | None = None,
              edge_mm: float = 0.3):
        pt0 = self.pt(*p0)
        pt1 = self.pt(*p1)
        if fill is not None:
            cv2.rectangle(self.img, pt0, pt1, fill, -1)
        if edge is not None:
            cv2.rectangle(self.img, pt0, pt1, edge, max(1, self.mm(edge_mm)), cv2.LINE_AA)

    def rounded_panel(self, x: float, y: float, w: float, h: float, r: float = 3.0,
                      fill: tuple[int, int, int] | None = None,
                      edge: tuple[int, int, int] | None = None,
                      edge_mm: float = 0.4):
        p0 = self.pt(x, y)
        p1 = self.pt(x + w, y + h)
        rr = self.mm(r)
        if fill is not None:
            cv2.rectangle(self.img, (p0[0] + rr, p0[1]), (p1[0] - rr, p1[1]), fill, -1)
            cv2.rectangle(self.img, (p0[0], p0[1] + rr), (p1[0], p1[1] - rr), fill, -1)
            for cx, cy in ((p0[0] + rr, p0[1] + rr), (p1[0] - rr, p0[1] + rr),
                           (p0[0] + rr, p1[1] - rr), (p1[0] - rr, p1[1] - rr)):
                cv2.circle(self.img, (cx, cy), rr, fill, -1, cv2.LINE_AA)
        if edge is not None:
            w_px = max(1, self.mm(edge_mm))
            for a, b in (((p0[0] + rr, p0[1]), (p1[0] - rr, p0[1])),
                         ((p0[0] + rr, p1[1]), (p1[0] - rr, p1[1])),
                         ((p0[0], p0[1] + rr), (p0[0], p1[1] - rr)),
                         ((p1[0], p0[1] + rr), (p1[0], p1[1] - rr))):
                cv2.line(self.img, a, b, edge, w_px, cv2.LINE_AA)
            for cx, cy, a0 in ((p0[0] + rr, p0[1] + rr, 180), (p1[0] - rr, p0[1] + rr, 270),
                               (p1[0] - rr, p1[1] - rr, 0), (p0[0] + rr, p1[1] - rr, 90)):
                cv2.ellipse(self.img, (cx, cy), (rr, rr), 0, a0, a0 + 90, edge, w_px, cv2.LINE_AA)

    def circle(self, center: tuple[float, float], r_mm: float,
               fill: tuple[int, int, int] | None = None,
               edge: tuple[int, int, int] | None = None,
               edge_mm: float = 0.3):
        c = self.pt(*center)
        r = max(1, self.mm(r_mm))
        if fill is not None:
            cv2.circle(self.img, c, r, fill, -1, cv2.LINE_AA)
        if edge is not None:
            cv2.circle(self.img, c, r, edge, max(1, self.mm(edge_mm)), cv2.LINE_AA)

    def text(self, pos: tuple[float, float], s: str, size_mm: float,
             col: tuple[int, int, int] = INK, anchor: str = 'mm',
             bold: bool = False, halo: tuple[int, int, int] | None = None,
             spacing: float | None = None):
        self.text_items.append((pos, s, size_mm, col, anchor, bold, halo, spacing))

    def rotated_text(self, center_mm: tuple[float, float], s: str, size_mm: float,
                     angle_deg: float = 90.0, col: tuple[int, int, int] = INK,
                     bold: bool = False):
        self.rotated_texts.append((center_mm, s, size_mm, angle_deg, col, bold))

    def flush_text(self):
        reg = next((p for p in _FACES_REG if Path(p).exists()), None)
        bld = next((p for p in _FACES_BLD if Path(p).exists()), reg)
        if reg is None:
            for (pos, s, sz, col, anc, bold, halo, sp) in self.text_items:
                p = self.pt(*pos)
                cv2.putText(self.img, s, p, cv2.FONT_HERSHEY_SIMPLEX,
                            self.mm(sz) / 28.0, col, 1, cv2.LINE_AA)
            self.text_items = []
            return

        pil = Image.fromarray(cv2.cvtColor(self.img, cv2.COLOR_BGR2RGB))
        dr = ImageDraw.Draw(pil)
        cache: dict[tuple[float, bool], ImageFont.FreeTypeFont] = {}
        A = {'lt': 'la', 'mt': 'ma', 'rt': 'ra', 'lm': 'lm', 'mm': 'mm',
             'rm': 'rm', 'lb': 'ls', 'mb': 'ms', 'rb': 'rs'}

        for (pos, s, sz, col, anc, bold, halo, sp) in self.text_items:
            key = (sz, bold)
            if key not in cache:
                cache[key] = ImageFont.truetype(bld if bold else reg, max(6, self.mm(sz)))
            kw = {}
            if halo is not None:
                kw['stroke_width'] = max(1, self.mm(sz * 0.16))
                kw['stroke_fill'] = (halo[2], halo[1], halo[0])
            if sp is not None:
                kw['spacing'] = self.mm(sp)
            pt = self.pt(*pos)
            dr.text((pt[0], pt[1]), s, font=cache[key],
                    fill=(col[2], col[1], col[0]), anchor=A.get(anc, 'mm'), **kw)

        for (center_mm, s, sz, angle, col, bold) in self.rotated_texts:
            f = ImageFont.truetype(bld if bold else reg, max(6, self.mm(sz)))
            bbox = f.getbbox(s)
            tw = bbox[2] - bbox[0] + 10
            th = bbox[3] - bbox[1] + 10
            txt_layer = Image.new('RGBA', (tw, th), (255, 255, 255, 0))
            d_layer = ImageDraw.Draw(txt_layer)
            d_layer.text((5, 5), s, font=f, fill=(col[2], col[1], col[0], 255))
            rot = txt_layer.rotate(angle, expand=True, resample=Image.BICUBIC)
            rw, rh = rot.size
            cx_px, cy_px = self.pt(*center_mm)
            paste_pos = (cx_px - rw // 2, cy_px - rh // 2)
            pil.paste(rot, paste_pos, rot)

        self.img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
        self.text_items = []
        self.rotated_texts = []


def _catalog_entries() -> tuple[list[tuple[int, str]], float]:
    """(tag_id, ascii_type_key) pairs + physical tag size (m) from the fixed,
    fleet-wide object set."""
    sys.path.insert(0, str(_SERVER_PKG_ROOT))
    from physical_ai_server.workflow.object_catalog import fixed_catalog
    catalog = fixed_catalog()
    entries: list[tuple[int, str]] = []
    for type_name in catalog.type_names():
        for tag_id in catalog.tag_ids_for_type(type_name):
            entries.append((int(tag_id), type_name))
    return entries, float(catalog.tag_size_m)


def _render_tag_marker(tag_id: int, tag_px: int) -> np.ndarray:
    aruco_dict = cv2.aruco.getPredefinedDictionary(TAG_FAMILY)
    return aruco_dict.generateImageMarker(tag_id, tag_px)


def _draw_crop_marks(canvas: Canvas, x0: float, y0: float, x1: float, y1: float,
                     arm_len: float = 4.0, col: tuple[int, int, int] = INK_SOFT):
    """Draw corner crosshair ticks around a rectangle."""
    # Top-Left
    canvas.line((x0 - arm_len, y0), (x0 - 1.0, y0), col, 0.4)
    canvas.line((x0, y0 - arm_len), (x0, y0 - 1.0), col, 0.4)
    # Top-Right
    canvas.line((x1 + 1.0, y0), (x1 + arm_len, y0), col, 0.4)
    canvas.line((x1, y0 - arm_len), (x1, y0 - 1.0), col, 0.4)
    # Bottom-Left
    canvas.line((x0 - arm_len, y1), (x0 - 1.0, y1), col, 0.4)
    canvas.line((x0, y1 + 1.0), (x0, y1 + arm_len), col, 0.4)
    # Bottom-Right
    canvas.line((x1 + 1.0, y1), (x1 + arm_len, y1), col, 0.4)
    canvas.line((x1, y1 + 1.0), (x1, y1 + arm_len), col, 0.4)


def _draw_cube_card(canvas: Canvas, cx: float, cy: float, tag_id: int, label: str,
                    tag_size_mm: float = 24.0, cube_size_mm: float = 30.0,
                    primary_col: tuple[int, int, int] = AMBER_EDGE,
                    bg_col: tuple[int, int, int] = AMBER_BG,
                    badge_title: str = "WÜRFEL"):
    """Draw a primary cube cutout card with exact 30x30 mm top-face outline,
    24 mm centered tag, crop marks, quiet zone, and dimension callouts."""
    card_w = 90.0
    card_h = 82.0
    x_card = cx - card_w / 2
    y_card = cy - card_h / 2

    canvas.rounded_panel(x_card, y_card, card_w, card_h, r=3.0, fill=PAPER,
                         edge=LINE_GREY, edge_mm=0.5)

    # Header badge
    canvas.rounded_panel(x_card + 4.0, y_card + 4.0, card_w - 8.0, 9.5, r=2.0,
                         fill=bg_col, edge=primary_col, edge_mm=0.5)
    canvas.text((cx, y_card + 8.8), f'[ Tag #{tag_id} ]  {badge_title} ({label})',
                3.2, col=INK, anchor='mm', bold=True)

    # 30 mm Cube top face bounds
    cb_x0 = cx - cube_size_mm / 2
    cb_y0 = cy - 1.5 - cube_size_mm / 2
    cb_x1 = cb_x0 + cube_size_mm
    cb_y1 = cb_y0 + cube_size_mm

    # Outer 30 mm dashed cut box
    canvas.dash_rect((cb_x0, cb_y0), (cb_x1, cb_y1), col=primary_col, w_mm=0.5, on_mm=2.5, off_mm=1.5)
    _draw_crop_marks(canvas, cb_x0, cb_y0, cb_x1, cb_y1, arm_len=5.0, col=primary_col)

    # 24 mm AprilTag placement inside 30 mm box
    tag_x0 = cx - tag_size_mm / 2
    tag_y0 = (cb_y0 + cb_y1) / 2 - tag_size_mm / 2
    tag_px = canvas.mm(tag_size_mm)
    tag_img = _render_tag_marker(tag_id, tag_px)
    px0, py0 = canvas.pt(tag_x0, tag_y0)
    canvas.img[py0:py0 + tag_px, px0:px0 + tag_px] = cv2.cvtColor(tag_img, cv2.COLOR_GRAY2BGR)

    # Tag outer thin border
    canvas.rect((tag_x0, tag_y0), (tag_x0 + tag_size_mm, tag_y0 + tag_size_mm),
                edge=INK, edge_mm=0.3)

    # Center crosshair tick marks on left and right outside the 30mm box
    mid_y = (cb_y0 + cb_y1) / 2
    canvas.line((cb_x0 - 4.0, mid_y), (cb_x0 - 1.0, mid_y), col=primary_col, w_mm=0.4)
    canvas.line((cb_x1 + 1.0, mid_y), (cb_x1 + 4.0, mid_y), col=primary_col, w_mm=0.4)

    # Callouts
    canvas.text((cx, cb_y0 - 4.5), '▲ OBEN / VORNE (AUSRICHTUNG) ▲', 2.1, col=INK_SOFT, anchor='mb', bold=True)
    canvas.text((cx, cb_y1 + 4.5), 'Schnittlinie 30 × 30 mm (Würfel-Oberseite)',
                2.3, col=primary_col, anchor='mt', bold=True)
    canvas.text((cx, y_card + card_h - 4.0),
                f'Tag {tag_size_mm:g} mm · 3 mm weißer Rand (Quiet Zone) · Greiftiefe 15 mm',
                2.1, col=INK_SOFT, anchor='mb')


def _draw_spare_cell(canvas: Canvas, cx: float, cy: float, tag_id: int, label: str,
                     tag_size_mm: float = 24.0, cube_size_mm: float = 30.0,
                     border_col: tuple[int, int, int] = LINE_GREY):
    """Draw a compact spare tag with 30 mm cutting outline and 24 mm tag."""
    cb_x0 = cx - cube_size_mm / 2
    cb_y0 = cy - cube_size_mm / 2
    cb_x1 = cb_x0 + cube_size_mm
    cb_y1 = cb_y0 + cube_size_mm

    # 30 mm dashed cut box
    canvas.dash_rect((cb_x0, cb_y0), (cb_x1, cb_y1), col=border_col, w_mm=0.4, on_mm=2.0, off_mm=1.5)
    _draw_crop_marks(canvas, cb_x0, cb_y0, cb_x1, cb_y1, arm_len=2.5, col=INK_FAINT)

    # 24 mm tag
    tag_x0 = cx - tag_size_mm / 2
    tag_y0 = cy - tag_size_mm / 2
    tag_px = canvas.mm(tag_size_mm)
    tag_img = _render_tag_marker(tag_id, tag_px)
    px0, py0 = canvas.pt(tag_x0, tag_y0)
    canvas.img[py0:py0 + tag_px, px0:px0 + tag_px] = cv2.cvtColor(tag_img, cv2.COLOR_GRAY2BGR)
    canvas.rect((tag_x0, tag_y0), (tag_x0 + tag_size_mm, tag_y0 + tag_size_mm),
                edge=INK, edge_mm=0.25)

    # Label below
    canvas.text((cx, cb_y1 + 1.2), f'#{tag_id} {label}'.strip(), 2.1, col=INK, anchor='mt', bold=True)


def render_a4_catalog_sheet(entries: list[tuple[int, str]], tag_size_mm: float = 24.0,
                            dpi: int = DPI) -> np.ndarray:
    """Render a comprehensive DIN A4 printable PDF sheet for the Roboter Studio
    cube AprilTags (#20 and #21)."""
    canvas = Canvas(210.0, 297.0, dpi=dpi)

    # ── 1. Top Header Block (Y: 6 to 28 mm) ─────────────────────────────────
    canvas.rounded_panel(10.0, 6.0, 190.0, 22.0, r=3.0, fill=PAPER, edge=LINE_GREY, edge_mm=0.5)
    canvas.text((105.0, 11.2), 'EduBotics Roboter Studio — AprilTag-Druckbogen (Würfel)',
                4.6, col=INK, anchor='mm', bold=True)
    canvas.text((105.0, 16.5),
                'AprilTag-Familie: tag36h11 · Tag-IDs: #20 & #21 · Tag-Größe: 24 mm · Würfel: 30 mm',
                2.9, col=PRIMARY, anchor='mm', bold=True)
    canvas.text((105.0, 21.8),
                'WICHTIG: Im Druckdialog „Tatsächliche Größe" (100 %) wählen · KEIN „An Seite anpassen"!',
                2.6, col=AMBER_DEEP, anchor='mm', bold=True)

    # ── 2. Technical Specification & Mounting Banner (Y: 30.5 to 48.5 mm) ───
    canvas.rounded_panel(10.0, 30.5, 190.0, 18.0, r=2.5, fill=AMBER_BG, edge=AMBER_EDGE, edge_mm=0.5)
    canvas.text((14.0, 35.5),
                '• Tag-Größe: 24,0 × 24,0 mm (EDUBOTICS_TAG_SIZE_M = 0.024 m)',
                2.3, col=INK, anchor='lm', bold=True)
    canvas.text((14.0, 42.5),
                '• Schnittmaß: 30,0 × 30,0 mm (inkl. 3,0 mm weißem Rand / Quiet Zone)',
                2.3, col=INK, anchor='lm', bold=True)
    canvas.text((115.0, 35.5),
                '• Montage: Plan & mittig auf OBERSEITE des 30 mm Würfels kleben.',
                2.3, col=INK, anchor='lm', bold=True)
    canvas.text((115.0, 42.5),
                '• Erkennung: In Roboter Studio automatisch als Typ „Würfel" erkannt.',
                2.3, col=PRIMARY, anchor='lm', bold=True)

    # ── 3. Section 1: Main Cube Tags (Y: 51 to 139 mm) ──────────────────────
    canvas.text((10.0, 52.5),
                '1. HAUPT-TAGS — PASSGENAU FÜR 30 mm WÜRFEL (MIT AUSRICHTUNG & SCHNITTLINIEN)',
                3.0, col=INK, anchor='lm', bold=True)

    _draw_cube_card(canvas, cx=57.0, cy=96.0, tag_id=20, label="wuerfel",
                    tag_size_mm=tag_size_mm, cube_size_mm=30.0,
                    primary_col=AMBER_EDGE, bg_col=AMBER_BG, badge_title="WÜRFEL 1 (CUBE 1)")

    _draw_cube_card(canvas, cx=153.0, cy=96.0, tag_id=21, label="wuerfel",
                    tag_size_mm=tag_size_mm, cube_size_mm=30.0,
                    primary_col=BLUE_EDGE, bg_col=BLUE_BG, badge_title="WÜRFEL 2 (CUBE 2)")

    # ── 4. Section 2: Spare Tags (Y: 141 to 226 mm) ─────────────────────────
    canvas.text((10.0, 142.5),
                '2. RESERVE-TAGS & ZUSATZ-CUTOUTS (SPARE TAGS)',
                3.0, col=INK, anchor='lm', bold=True)

    canvas.rounded_panel(10.0, 145.5, 190.0, 81.5, r=3.0, fill=PAPER, edge=LINE_GREY, edge_mm=0.5)

    spare_xs = [32.0, 78.0, 126.0, 172.0]

    # Row 1: 4x Tag #20
    canvas.text((14.0, 149.0), 'Tag #20 Würfel 1 (4x Reserve):', 2.3, col=AMBER_DEEP, anchor='lm', bold=True)
    for sx in spare_xs:
        _draw_spare_cell(canvas, cx=sx, cy=168.5, tag_id=20, label="Würfel 1",
                         tag_size_mm=tag_size_mm, cube_size_mm=30.0, border_col=AMBER_EDGE)

    # Row 2: 4x Tag #21
    canvas.text((14.0, 189.0), 'Tag #21 Würfel 2 (4x Reserve):', 2.3, col=BLUE_DEEP, anchor='lm', bold=True)
    for sx in spare_xs:
        _draw_spare_cell(canvas, cx=sx, cy=208.5, tag_id=21, label="Würfel 2",
                         tag_size_mm=tag_size_mm, cube_size_mm=30.0, border_col=BLUE_EDGE)


    # ── 5. Section 3: 100 mm Verification Scale Bar (Y: 229 to 253 mm) ───────
    ruler_x0 = 55.0
    ruler_y = 229.0
    ruler_w = 100.0
    canvas.rounded_panel(10.0, ruler_y, 190.0, 24.0, r=2.5, fill=PAPER, edge=LINE_GREY, edge_mm=0.4)
    canvas.text((105.0, ruler_y + 4.0),
                '100 mm KONTROLLMASS — NACH DEM DRUCK ZWINGEND MIT LINEAL PRÜFEN (100 % = 100 mm)',
                2.6, col=INK_SOFT, anchor='mm', bold=True)

    ruler_line_y = ruler_y + 13.0
    canvas.line((ruler_x0, ruler_line_y), (ruler_x0 + ruler_w, ruler_line_y), INK, 0.8)
    for mm_tick in range(101):
        tx = ruler_x0 + mm_tick
        if mm_tick % 10 == 0:
            th = 4.0
            canvas.line((tx, ruler_line_y), (tx, ruler_line_y - th), INK, 0.6)
            lbl = f'{mm_tick}' if mm_tick != 100 else '100 mm'
            canvas.text((tx, ruler_line_y + 3.8), lbl, 2.3, col=INK, anchor='mt')
        elif mm_tick % 5 == 0:
            th = 2.4
            canvas.line((tx, ruler_line_y), (tx, ruler_line_y - th), INK_SOFT, 0.4)
        else:
            th = 1.3
            canvas.line((tx, ruler_line_y), (tx, ruler_line_y - th), INK_FAINT, 0.25)

    # ── 6. Section 4: Instructions Guide (Y: 255.5 to 286.5 mm) ──────────────
    canvas.rounded_panel(10.0, 255.5, 190.0, 31.0, r=2.5, fill=PAPER, edge=LINE_GREY, edge_mm=0.4)
    canvas.text((105.0, 260.0), 'KURZANLEITUNG FÜR LEHRKRÄFTE & SCHÜLER', 2.6, col=PRIMARY, anchor='mm', bold=True)

    # 4 columns for steps
    s_xs = [14.0, 62.0, 110.0, 158.0]

    # Step 1
    canvas.text((s_xs[0], 265.5), '1. Maßstab prüfen', 2.4, col=INK, anchor='lm', bold=True)
    canvas.text((s_xs[0], 270.5), '100-mm-Linie oben mit dem\nLineal nachmessen (100 %).', 2.0, col=INK_SOFT, anchor='lt', spacing=1.0)

    # Step 2
    canvas.text((s_xs[1], 265.5), '2. Ausschneiden', 2.4, col=INK, anchor='lm', bold=True)
    canvas.text((s_xs[1], 270.5), 'Entlang der 30×30 mm Linien.\nWeißen Rand NICHT kappen!', 2.0, col=INK_SOFT, anchor='lt', spacing=1.0)

    # Step 3
    canvas.text((s_xs[2], 265.5), '3. Aufkleben', 2.4, col=INK, anchor='lm', bold=True)
    canvas.text((s_xs[2], 270.5), 'Plan & faltenfrei auf die\nOBERSEITE des Würfels kleben.', 2.0, col=INK_SOFT, anchor='lt', spacing=1.0)

    # Step 4
    canvas.text((s_xs[3], 265.5), '4. Roboter Studio', 2.4, col=INK, anchor='lm', bold=True)
    canvas.text((s_xs[3], 270.5), 'Block „Greife Würfel" oder\n„Solange Würfel sichtbar".', 2.0, col=INK_SOFT, anchor='lt', spacing=1.0)

    # ── 7. Footer (Y: 292 mm) ────────────────────────────────────────────────
    canvas.text((105.0, 292.0),
                'EduBotics Roboter Studio · AprilTag-Bogen (tag36h11, IDs #20 & #21) · tools/generate_apriltags.py',
                2.4, col=INK_FAINT, anchor='mm')

    canvas.flush_text()
    return canvas.img




def _render_custom_sheet(entries: list[tuple[int, str]], tag_size_mm: float,
                         out: Path, dpi: int = DPI) -> None:
    """Render a clean grid for an arbitrary list of tags."""
    px_per_mm = dpi / 25.4
    cell_mm = tag_size_mm + 2 * QUIET_ZONE_MM
    cell_px = int(round(cell_mm * px_per_mm))
    tag_px = int(round(tag_size_mm * px_per_mm))
    quiet_px = int(round(QUIET_ZONE_MM * px_per_mm))

    cols = min(TAGS_PER_ROW, len(entries))
    rows = math.ceil(len(entries) / TAGS_PER_ROW)
    canvas = np.full((cell_px * rows, cell_px * cols), 255, dtype=np.uint8)

    for i, (tag_id, label) in enumerate(entries):
        row, col = divmod(i, TAGS_PER_ROW)
        tag_img = _render_tag_marker(tag_id, tag_px)
        x0 = col * cell_px + quiet_px
        y0 = row * cell_px + quiet_px
        canvas[y0:y0 + tag_px, x0:x0 + tag_px] = tag_img
        cv2.putText(
            canvas,
            f'#{tag_id} {label}'.strip(),
            (col * cell_px + 4, (row + 1) * cell_px - 4),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.4,
            0,
            1,
            cv2.LINE_AA,
        )

    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out.with_suffix('.png')), canvas)
    try:
        Image.fromarray(canvas).convert('RGB').save(out, format='PDF', resolution=dpi)
    except ImportError:
        print('Pillow not installed — wrote PNG only.')

    ids = ', '.join(f'#{tid}' for tid, _ in entries)
    print(f'Wrote {out} ({canvas.shape[1]} x {canvas.shape[0]} px @ {dpi} dpi); '
          f'{len(entries)} tags at {tag_size_mm:g} mm: {ids}')


def _render_sheet(entries: list[tuple[int, str]], tag_size_mm: float,
                  out: Path, dpi: int = DPI) -> None:
    tag_ids = [tid for tid, _ in entries]
    # If standard catalog tags (IDs 20 and 21) or cube default, render the full A4 precision sheet
    if set(tag_ids) == {20, 21} or (len(entries) == 2 and 20 in tag_ids and 21 in tag_ids):
        img_bgr = render_a4_catalog_sheet(entries, tag_size_mm=tag_size_mm, dpi=dpi)
        out.parent.mkdir(parents=True, exist_ok=True)
        png_path = out.with_suffix('.png')
        cv2.imwrite(str(png_path), img_bgr)
        pil_img = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
        pil_img.save(str(out), format='PDF', resolution=dpi)
        ids = ', '.join(f'#{tid}' for tid, _ in entries)
        print(f'Wrote {out} (A4 Portrait @ {dpi} DPI); {len(entries)} tags at {tag_size_mm:g} mm: {ids}')
    else:
        _render_custom_sheet(entries, tag_size_mm, out, dpi=dpi)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=Path('classroom_kit/apriltags.pdf'))
    parser.add_argument(
        '--start-id', type=int, default=None,
        help='Manual mode: first tag id (inclusive). Requires --count.')
    parser.add_argument(
        '--count', type=int, default=None,
        help='Manual mode: number of consecutive ids from --start-id.')
    parser.add_argument(
        '--size-mm', type=float, default=None,
        help='Tag black-square edge in mm (default: the catalog tag size, '
             'i.e. EDUBOTICS_TAG_SIZE_M * 1000).')
    parser.add_argument(
        '--allow-reserved', action='store_true',
        help='Manual mode: allow ids reserved by the fixed object catalog; '
             'colliding cells are labelled with the owning type instead of '
             'the range being refused.')
    parser.add_argument(
        '--dpi', type=int, default=DPI,
        help=f'Resolution in DPI (default: {DPI})')
    args = parser.parse_args()

    if (args.start_id is None) != (args.count is None):
        parser.error('--start-id and --count must be given together.')

    if args.start_id is not None:
        if args.count <= 0:
            parser.error('--count must be positive.')
        catalog_entries, catalog_tag_size_m = _catalog_entries()
        reserved = {tid: label for tid, label in catalog_entries}
        requested = list(range(args.start_id, args.start_id + args.count))
        collisions = [i for i in requested if i in reserved]
        if collisions and not args.allow_reserved:
            listing = ', '.join(f'#{i} ({reserved[i]})' for i in collisions)
            parser.error(
                f'requested id range collides with catalog-reserved tag ids: '
                f'{listing}. These ids are assigned to fleet objects '
                '(object_catalog.py::_FIXED_CATALOG). Pick a different '
                '--start-id/--count, or pass --allow-reserved to print the '
                'colliding cells labelled with their catalog type.')
        entries = [(i, reserved.get(i, '')) for i in requested]
        tag_size_mm = args.size_mm
        if tag_size_mm is None:
            tag_size_mm = catalog_tag_size_m * 1000.0
    else:
        entries, tag_size_m = _catalog_entries()
        tag_size_mm = args.size_mm if args.size_mm is not None else tag_size_m * 1000.0

    if tag_size_mm <= 0:
        parser.error('--size-mm must be positive.')
    _render_sheet(entries, tag_size_mm, args.out, dpi=args.dpi)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Render the OMX robot base + ChArUco calibration board alignment mat (DIN A4).

WHY THIS MAT EXISTS:
Roboter Studio extrinsic calibration requires the ChArUco board to sit at a
known, fixed pose relative to the robot base:
    BOARD_ORIGIN_X_M = 0.180 m (180 mm forward from the base frame origin)
    BOARD_ORIGIN_Y_M = 0.075 m (75 mm to the robot's left)
    BOARD_TABLE_Z_M  = 0.0 m (table surface)
    BOARD_YAW_DEG    = 0.0 deg (board square to base)

This DIN A4 printable template (210 mm x 297 mm) is placed directly on the
table UNDER the base of the ROBOTIS OpenManipulator-X (OMX) robot.

It provides a foolproof 3-point matching alignment system:
  [1] ORANGE CORNER (Marker 0 / Vorne Links) with extension crosshairs
  [▲] GREEN CENTERLINE TICK (y = 0)
  [2] BLUE CORNER (Vorne Rechts) with extension crosshairs
  [3] Flush 180.0 mm near-edge stop line (with score/fold-up 3D paper fence option)

Usage:
    python tools/generate_omx_mat.py --out classroom_kit/omx_mat.pdf

Print at 100% scale ("Tatsächliche Größe" / no "fit to page"!) on standard
A4 paper and verify the 100 mm ruler with a physical ruler before use.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

DPI = 300
_PX_PER_MM = DPI / 25.4

# ── Color Palette (BGR for OpenCV, RGB for PIL) ────────────────────────────
def _c(r: int, g: int, b: int) -> tuple[int, int, int]:
    return (b, g, r)

PAPER       = _c(255, 255, 255)
INK         = _c(28, 33, 40)
INK_SOFT    = _c(85, 95, 105)
INK_FAINT   = _c(180, 186, 194)
LINE_GREY   = _c(215, 220, 226)

BASE_FILL   = _c(235, 241, 250)      # light technical blue-grey
BASE_EDGE   = _c(30, 64, 120)        # dark robot outline
BASE_HOLE   = _c(205, 220, 242)

BOARD_FILL  = _c(240, 244, 250)
BOARD_EDGE  = _c(37, 99, 235)        # royal blue

AMBER_BG    = _c(254, 243, 199)      # warm amber highlight
AMBER_EDGE  = _c(217, 119, 6)        # vibrant amber (Corner 1)
AMBER_DEEP  = _c(180, 83, 9)

BLUE_BG     = _c(224, 238, 255)      # light blue highlight
BLUE_EDGE   = _c(37, 99, 235)        # royal blue (Corner 2)
BLUE_DEEP   = _c(29, 78, 216)

GREEN_BG    = _c(220, 245, 225)
GREEN_EDGE  = _c(22, 101, 52)        # forest green (Center)

RED_ACC     = _c(220, 38, 38)
PRIMARY     = _c(15, 76, 129)

# Exact CAD contour of follower_01_base.stl (at z=0) in base coordinates (mm):
# x in [-60, +60], y in [-75, +75].
BASE_CONTOUR: list[tuple[float, float]] = [
    (60.0, -67.0),
    (60.0, -45.5),
    (52.0, -37.5),
    (23.0, -37.5),
    (15.0, -29.5),
    (15.0, 29.5),
    (23.0, 37.5),
    (52.0, 37.5),
    (60.0, 45.5),
    (60.0, 67.0),
    (52.0, 75.0),
    (-52.0, 75.0),
    (-60.0, 67.0),
    (-60.0, 15.0),
    (-50.0, 5.0),
    (-30.35, 5.0),
    (-29.35, 6.0),
    (-29.35, 7.0),
    (-30.35, 8.0),
    (-30.85, 8.0),
    (-30.85, 20.25),
    (-19.85, 20.25),
    (-19.85, 8.0),
    (-20.35, 8.0),
    (-21.35, 7.0),
    (-21.35, -7.0),
    (-20.35, -8.0),
    (-19.85, -8.0),
    (-19.85, -20.25),
    (-30.85, -20.25),
    (-30.85, -8.0),
    (-30.35, -8.0),
    (-29.35, -7.0),
    (-29.35, -6.0),
    (-30.35, -5.0),
    (-50.0, -5.0),
    (-60.0, -15.0),
    (-60.0, -67.0),
    (-52.0, -75.0),
    (52.0, -75.0),
]

# Mounting holes on the base flanges (x, y in base frame, mm)
BASE_HOLES: list[tuple[float, float]] = [
    (-50.0, -62.5), (-25.0, -62.5), (0.0, -62.5), (25.0, -62.5), (50.0, -62.5),
    (-50.0,  62.5), (-25.0,  62.5), (0.0,  62.5), (25.0,  62.5), (50.0,  62.5),
]

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


class Sheet:
    """Millimetre-native A4 canvas with base-frame projection."""

    def __init__(self, dpi: int = DPI):
        self.dpi = dpi
        self.k = dpi / 25.4
        self.w_mm = 210.0
        self.h_mm = 297.0
        self.W = int(round(self.w_mm * self.k))
        self.H = int(round(self.h_mm * self.k))
        self.img = np.full((self.H, self.W, 3), 255, dtype=np.uint8)
        self.text_items: list[tuple] = []
        self.rotated_texts: list[tuple] = []

        # Geometry mapping on A4:
        # Centerline: y_base = 0 is at page X = 105.0 mm.
        # +Y (robot left) is page left (smaller X).
        # Calibration board near edge (x = 180.0 mm) is at page Y = 50.0 mm.
        # Since +X runs UP the page (smaller Y):
        # Page Y = 50.0 + (180.0 - x_mm)
        # So x = 0 (base origin) is at Y = 50.0 + 180.0 = 230.0 mm.
        # x = -60 (rear of base) is at Y = 230.0 + 60.0 = 290.0 mm.
        self.cx_mm = 105.0
        self.board_near_edge_y_mm = 50.0

    def mm(self, v: float) -> int:
        return int(round(v * self.k))

    def pg(self, x_mm: float, y_mm: float) -> tuple[int, int]:
        """Page coordinates in mm (origin top-left) -> pixel (x, y)."""
        return (int(round(x_mm * self.k)), int(round(y_mm * self.k)))

    def base(self, x_mm: float, y_mm: float) -> tuple[int, int]:
        """Convert robot base frame coords (mm) to page pixel coords."""
        pg_x = self.cx_mm - y_mm
        pg_y = self.board_near_edge_y_mm + (180.0 - x_mm)
        return self.pg(pg_x, pg_y)

    def base_pt_mm(self, x_mm: float, y_mm: float) -> tuple[float, float]:
        """Base frame (x, y) mm -> Page mm (x, y from top-left)."""
        return (self.cx_mm - y_mm, self.board_near_edge_y_mm + (180.0 - x_mm))

    def line(self, p0: tuple[int, int] | tuple[float, float],
             p1: tuple[int, int] | tuple[float, float],
             col: tuple[int, int, int], w_mm: float = 0.3, aa: bool = True):
        pt0 = (int(round(p0[0])), int(round(p0[1])))
        pt1 = (int(round(p1[0])), int(round(p1[1])))
        cv2.line(self.img, pt0, pt1, col, max(1, self.mm(w_mm)),
                 cv2.LINE_AA if aa else cv2.LINE_8)

    def dash(self, p0: tuple[int, int] | tuple[float, float],
             p1: tuple[int, int] | tuple[float, float],
             col: tuple[int, int, int], w_mm: float = 0.3, on_mm: float = 4.0,
             off_mm: float = 3.0):
        a = np.array(p0, float)
        b = np.array(p1, float)
        L = np.linalg.norm(b - a)
        if L < 1.0:
            return
        u = (b - a) / L
        on_px = on_mm * self.k
        off_px = off_mm * self.k
        t = 0.0
        while t < L:
            s0 = a + u * t
            s1 = a + u * min(t + on_px, L)
            cv2.line(self.img, tuple(s0.astype(int)), tuple(s1.astype(int)),
                     col, max(1, self.mm(w_mm)), cv2.LINE_AA)
            t += on_px + off_px

    def rect(self, p0: tuple[float, float], p1: tuple[float, float],
             fill: tuple[int, int, int] | None = None,
             edge: tuple[int, int, int] | None = None,
             edge_mm: float = 0.3):
        pt0 = self.pg(*p0)
        pt1 = self.pg(*p1)
        if fill is not None:
            cv2.rectangle(self.img, pt0, pt1, fill, -1)
        if edge is not None:
            cv2.rectangle(self.img, pt0, pt1, edge, max(1, self.mm(edge_mm)), cv2.LINE_AA)

    def panel(self, x_mm: float, y_mm: float, w_mm: float, h_mm: float,
              r_mm: float = 3.0, fill: tuple[int, int, int] | None = None,
              edge: tuple[int, int, int] | None = None, edge_mm: float = 0.4):
        p0 = self.pg(x_mm, y_mm)
        p1 = self.pg(x_mm + w_mm, y_mm + h_mm)
        rr = self.mm(r_mm)
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

    def circle(self, center: tuple[int, int] | tuple[float, float], r_mm: float,
               fill: tuple[int, int, int] | None = None,
               edge: tuple[int, int, int] | None = None,
               edge_mm: float = 0.3):
        c = (int(round(center[0])), int(round(center[1])))
        r = max(1, self.mm(r_mm))
        if fill is not None:
            cv2.circle(self.img, c, r, fill, -1, cv2.LINE_AA)
        if edge is not None:
            cv2.circle(self.img, c, r, edge, max(1, self.mm(edge_mm)), cv2.LINE_AA)

    def text(self, pos_mm: tuple[float, float], s: str, size_mm: float,
             col: tuple[int, int, int] = INK, anchor: str = 'mm',
             bold: bool = False, halo: tuple[int, int, int] | None = None,
             spacing: float | None = None):
        self.text_items.append((pos_mm, s, size_mm, col, anchor, bold, halo, spacing))

    def rotated_text(self, center_mm: tuple[float, float], s: str, size_mm: float,
                     angle_deg: float = 90.0, col: tuple[int, int, int] = INK,
                     bold: bool = False):
        self.rotated_texts.append((center_mm, s, size_mm, angle_deg, col, bold))

    def flush_text(self):
        reg = next((p for p in _FACES_REG if Path(p).exists()), None)
        bld = next((p for p in _FACES_BLD if Path(p).exists()), reg)
        if reg is None:
            for (pos, s, sz, col, anc, bold, halo, sp) in self.text_items:
                p = self.pg(*pos)
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
            pt = self.pg(*pos)
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
            cx_px, cy_px = self.pg(*center_mm)
            paste_pos = (cx_px - rw // 2, cy_px - rh // 2)
            pil.paste(rot, paste_pos, rot)

        self.img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
        self.text_items = []
        self.rotated_texts = []


def draw_header(sh: Sheet) -> None:
    """Draw title header block at page top (Y = 6 to 24 mm)."""
    sh.panel(8.0, 6.0, 194.0, 18.0, r_mm=2.5, fill=PAPER, edge=LINE_GREY, edge_mm=0.4)
    sh.text((105.0, 10.5), 'EduBotics Roboter Studio — OMX Kalibriertafel-Positionierhilfe',
            4.6, col=INK, anchor='mm', bold=True)
    sh.text((105.0, 15.5),
            'DIN A4 Schablone (1:1 Maßstab) · Roboterbasis & ChArUco-Tafel-Ausrichtung',
            3.2, col=PRIMARY, anchor='mm', bold=True)
    sh.text((105.0, 20.0),
            '100 % Tatsächliche Größe drucken (kein Skalieren!) · Vorlage unter die Roboterbasis legen',
            2.6, col=INK_SOFT, anchor='mm')


def draw_calibration_zone(sh: Sheet) -> None:
    """Draw the calibration board alignment area at x = 180.0 mm (Y_page = 50.0 mm)."""
    y_edge_mm = sh.board_near_edge_y_mm  # 50.0 mm
    x_left_mm = 30.0
    x_right_mm = 180.0

    # 20 mm preview of the calibration board squares above the line (Y = 30 to 50 mm)
    y_preview_top = 30.0
    preview_h = y_edge_mm - y_preview_top  # 20.0 mm

    # Background for preview
    sh.panel(x_left_mm, y_preview_top, 150.0, preview_h, r_mm=0.0, fill=BOARD_FILL, edge=BOARD_EDGE, edge_mm=0.6)

    # 5 squares preview across 150 mm (each 30 mm wide)
    for sq_idx, (y0, y1, is_black) in enumerate([
        (75.0, 45.0, True),
        (45.0, 15.0, False),
        (15.0, -15.0, True),
        (-15.0, -45.0, False),
        (-45.0, -75.0, True),
    ]):
        sq_x_left = sh.cx_mm - y0
        if is_black:
            sh.panel(sq_x_left, y_preview_top, 30.0, preview_h, r_mm=0.0,
                     fill=_c(55, 60, 70), edge=INK, edge_mm=0.4)
            if sq_idx == 0:
                sh.text((sq_x_left + 15.0, y_preview_top + preview_h / 2),
                        'Feld (0,0)\n[schwarz]', 2.6, col=PAPER, anchor='mm', bold=True)
        else:
            sh.panel(sq_x_left, y_preview_top, 30.0, preview_h, r_mm=0.0,
                     fill=PAPER, edge=LINE_GREY, edge_mm=0.3)
            if sq_idx == 1:
                sh.panel(sq_x_left + 4.0, y_preview_top + 2.0, 22.0, preview_h - 4.0,
                         r_mm=0.0, fill=PAPER, edge=AMBER_EDGE, edge_mm=0.5)
                sh.text((sq_x_left + 15.0, y_preview_top + preview_h / 2),
                        'Marker 0', 2.6, col=AMBER_DEEP, anchor='mm', bold=True)

    # Bold Near Edge Line at x = 180.0 mm
    pt_l = sh.pg(x_left_mm, y_edge_mm)
    pt_r = sh.pg(x_right_mm, y_edge_mm)
    sh.line(pt_l, pt_r, AMBER_EDGE, 1.2)

    # Scissor Cut / Fold-Up Line across page
    sh.dash(sh.pg(8.0, y_edge_mm), sh.pg(202.0, y_edge_mm), AMBER_DEEP, 0.5, on_mm=4.0, off_mm=2.5)

    # Simple 3-step color matching bar immediately below the line (Y = 51.5 mm)
    sh.text((sh.cx_mm, y_edge_mm + 3.2),
            '[1] Orange auf Orange (links)  ──▶  [▲] Grün auf Grün (Mitte)  ──▶  [2] Blau auf Blau (rechts)',
            2.7, col=PRIMARY, anchor='mt', bold=True)

    # Fold-Up mechanical stop hint
    sh.text((sh.cx_mm, y_edge_mm + 7.5),
            'Tipp: Die Linie x = 180 mm nach oben falten — ergibt einen physischen Anschlag für die Tafel!',
            2.3, col=INK_SOFT, anchor='mt')

    # ── CORNER 1: ORANGE TARGET at (x = 180 mm, y = +75 mm) -> (30.0, 50.0) ──
    oc_pt = sh.pg(x_left_mm, y_edge_mm)
    bracket_len = 16.0

    # L-bracket hugging the corner
    sh.line((oc_pt[0], oc_pt[1] + sh.mm(3.0)), (oc_pt[0] + sh.mm(bracket_len), oc_pt[1] + sh.mm(3.0)), AMBER_EDGE, 1.6)
    sh.line((oc_pt[0] - sh.mm(3.0), oc_pt[1]), (oc_pt[0] - sh.mm(3.0), oc_pt[1] - sh.mm(bracket_len)), AMBER_EDGE, 1.6)
    sh.line((oc_pt[0] - sh.mm(3.0), oc_pt[1] + sh.mm(3.0)), (oc_pt[0] + sh.mm(2.0), oc_pt[1] + sh.mm(3.0)), AMBER_EDGE, 1.6)
    sh.line((oc_pt[0] - sh.mm(3.0), oc_pt[1] + sh.mm(3.0)), (oc_pt[0] - sh.mm(3.0), oc_pt[1] - sh.mm(2.0)), AMBER_EDGE, 1.6)

    # Extension Crosshairs that remain VISIBLE even when board is on top!
    # Runs 18 mm to the left (X = 30 to 12 mm)
    sh.line(sh.pg(x_left_mm - 4.0, y_edge_mm), sh.pg(x_left_mm - 22.0, y_edge_mm), AMBER_EDGE, 0.6)
    # Runs 18 mm downward (Y = 50 to 68 mm)
    sh.line(sh.pg(x_left_mm, y_edge_mm + 4.0), sh.pg(x_left_mm, y_edge_mm + 22.0), AMBER_EDGE, 0.6)

    # Target bullseye badge (Orange)
    sh.circle((oc_pt[0] - sh.mm(10.0), oc_pt[1] - sh.mm(10.0)), 5.5, fill=AMBER_BG, edge=AMBER_EDGE, edge_mm=0.8)
    sh.circle((oc_pt[0] - sh.mm(10.0), oc_pt[1] - sh.mm(10.0)), 1.8, fill=AMBER_DEEP, edge=None)

    # Callout panel for Corner 1 (Y = 62 to 80 mm, X = 8 to 92 mm)
    sh.panel(8.0, 62.0, 84.0, 18.0, r_mm=2.5, fill=AMBER_BG, edge=AMBER_EDGE, edge_mm=0.6)
    sh.text((50.0, 66.5), '[ 1 ] ORANGE ECKE (Marker 0)', 3.4, col=AMBER_DEEP, anchor='mm', bold=True)
    sh.text((50.0, 71.5), 'URSPRUNG (0,0) · VORNE LINKS', 2.8, col=INK, anchor='mm', bold=True)
    sh.text((50.0, 76.0), 'Hier die orange Ecke der Tafel anlegen', 2.3, col=INK_SOFT, anchor='mm')
    sh.line(sh.pg(24.0, 62.0), (oc_pt[0] - sh.mm(2.0), oc_pt[1] + sh.mm(2.0)), AMBER_DEEP, 0.8)

    # ── CORNER 2: BLUE TARGET at (x = 180 mm, y = -75 mm) -> (180.0, 50.0) ──
    rc_pt = sh.pg(x_right_mm, y_edge_mm)

    # L-bracket hugging the corner
    sh.line((rc_pt[0], rc_pt[1] + sh.mm(3.0)), (rc_pt[0] - sh.mm(bracket_len), rc_pt[1] + sh.mm(3.0)), BLUE_EDGE, 1.6)
    sh.line((rc_pt[0] + sh.mm(3.0), rc_pt[1]), (rc_pt[0] + sh.mm(3.0), rc_pt[1] - sh.mm(bracket_len)), BLUE_EDGE, 1.6)
    sh.line((rc_pt[0] + sh.mm(3.0), rc_pt[1] + sh.mm(3.0)), (rc_pt[0] - sh.mm(2.0), rc_pt[1] + sh.mm(3.0)), BLUE_EDGE, 1.6)
    sh.line((rc_pt[0] + sh.mm(3.0), rc_pt[1] + sh.mm(3.0)), (rc_pt[0] + sh.mm(3.0), rc_pt[1] - sh.mm(2.0)), BLUE_EDGE, 1.6)

    # Extension Crosshairs that remain VISIBLE even when board is on top!
    # Runs 18 mm to the right (X = 180 to 198 mm)
    sh.line(sh.pg(x_right_mm + 4.0, y_edge_mm), sh.pg(x_right_mm + 22.0, y_edge_mm), BLUE_EDGE, 0.6)
    # Runs 18 mm downward (Y = 50 to 68 mm)
    sh.line(sh.pg(x_right_mm, y_edge_mm + 4.0), sh.pg(x_right_mm, y_edge_mm + 22.0), BLUE_EDGE, 0.6)

    # Target square badge (Blue)
    sh.rect((x_right_mm + 5.0, y_edge_mm - 16.0), (x_right_mm + 17.0, y_edge_mm - 4.0),
            fill=BLUE_BG, edge=BLUE_EDGE, edge_mm=0.8)
    sh.rect((x_right_mm + 9.0, y_edge_mm - 12.0), (x_right_mm + 13.0, y_edge_mm - 8.0),
            fill=BLUE_DEEP, edge=None)

    # Callout panel for Corner 2 (Y = 62 to 80 mm, X = 114 to 202 mm)
    sh.panel(114.0, 62.0, 88.0, 18.0, r_mm=2.5, fill=BLUE_BG, edge=BLUE_EDGE, edge_mm=0.6)
    sh.text((158.0, 66.5), '[ 2 ] BLAUE ECKE (Vorne Rechts)', 3.4, col=BLUE_DEEP, anchor='mm', bold=True)
    sh.text((158.0, 71.5), 'Kante y = -75 mm · bündig anlegen', 2.8, col=INK, anchor='mm', bold=True)
    sh.text((158.0, 76.0), 'Hier die blaue Ecke der Tafel anlegen', 2.3, col=INK_SOFT, anchor='mm')
    sh.line(sh.pg(186.0, 62.0), (rc_pt[0] + sh.mm(2.0), rc_pt[1] + sh.mm(2.0)), BLUE_DEEP, 0.8)

    # ── CENTER TICK: GREEN (X = 105.0 mm, Y = 50.0 mm) ───────────────────
    # Triangle marker pointing up to the near edge
    tri_pts = [
        sh.pg(sh.cx_mm, y_edge_mm),
        sh.pg(sh.cx_mm - 3.5, y_edge_mm + 6.0),
        sh.pg(sh.cx_mm + 3.5, y_edge_mm + 6.0),
    ]
    cv2.fillPoly(sh.img, [np.array(tri_pts, dtype=np.int32)], GREEN_EDGE, cv2.LINE_AA)
    sh.text((sh.cx_mm, y_edge_mm + 12.0), '[▲] MITTE (y = 0)', 2.6, col=GREEN_EDGE, anchor='mm', bold=True)


def draw_intermediate_span(sh: Sheet) -> None:
    """Draw centerline, ticks, dimension callouts, instructions, and scale ruler."""
    # Centerline from x = -60 mm (Y = 290) to x = +180 mm (Y = 50)
    pt_start = sh.base(-60.0, 0.0)
    pt_end = sh.base(180.0, 0.0)
    sh.line(pt_start, pt_end, INK, 0.5)

    # Millimeter graduation along centerline (x from -60 to +180 mm)
    for x_mm in range(-60, 181):
        pt = sh.base(float(x_mm), 0.0)
        pos_mm = sh.base_pt_mm(float(x_mm), 0.0)
        # Skip ticks near text to keep the drawing uncluttered
        if -10 <= x_mm <= 10 or 155 <= x_mm <= 180:
            continue
        if x_mm % 50 == 0:
            th = 3.5
            sh.line((pt[0] - sh.mm(th), pt[1]), (pt[0] + sh.mm(th), pt[1]), INK, 0.6)
            sh.text((pos_mm[0] - 5.5, pos_mm[1]), f'{x_mm}', 2.5, col=INK_SOFT, anchor='rm')
        elif x_mm % 10 == 0:
            th = 2.2
            sh.line((pt[0] - sh.mm(th), pt[1]), (pt[0] + sh.mm(th), pt[1]), INK_SOFT, 0.4)
            if x_mm in (80, 100, 120, 140):
                sh.text((pos_mm[0] - 4.5, pos_mm[1]), f'{x_mm}', 2.4, col=INK_FAINT, anchor='rm')
        elif x_mm % 5 == 0:
            th = 1.4
            sh.line((pt[0] - sh.mm(th), pt[1]), (pt[0] + sh.mm(th), pt[1]), INK_FAINT, 0.25)

    # ── Dimension Lines (Left Side) ────────────────────────────────────────
    y_x0 = sh.base_pt_mm(0.0, 0.0)[1]     # 230 mm
    y_x60 = sh.base_pt_mm(60.0, 0.0)[1]   # 170 mm
    y_x180 = sh.base_pt_mm(180.0, 0.0)[1] # 50 mm

    # Dimension 180 mm: Base Origin (Y=230) to Board Near Edge (Y=50)
    dim_main_x = 10.0
    sh.line(sh.pg(dim_main_x, y_x0), sh.pg(dim_main_x, y_x180), PRIMARY, 0.6)
    sh.line(sh.pg(dim_main_x - 2.5, y_x0), sh.pg(dim_main_x + 2.5, y_x0), PRIMARY, 0.6)
    sh.line(sh.pg(dim_main_x - 2.5, y_x180), sh.pg(dim_main_x + 2.5, y_x180), PRIMARY, 0.6)
    sh.rotated_text((dim_main_x - 3.5, (y_x0 + y_x180) / 2),
                    '180,0 mm (Basis-Nullpunkt → Tafelkante)', 3.2,
                    angle_deg=90.0, col=PRIMARY, bold=True)

    # Dimension 120 mm: Front Flange (Y=170) to Board Near Edge (Y=50)
    dim_sec_x = 22.0
    sh.line(sh.pg(dim_sec_x, y_x60), sh.pg(dim_sec_x, y_x180), INK_SOFT, 0.4)
    sh.line(sh.pg(dim_sec_x - 2.0, y_x60), sh.pg(dim_sec_x + 2.0, y_x60), INK_SOFT, 0.4)
    sh.line(sh.pg(dim_sec_x - 2.0, y_x180), sh.pg(dim_sec_x + 2.0, y_x180), INK_SOFT, 0.4)
    sh.rotated_text((dim_sec_x + 3.5, (y_x60 + y_x180) / 2),
                    '120,0 mm (Flansch → Tafel)', 2.6,
                    angle_deg=90.0, col=INK_SOFT, bold=False)

    # ── Step-by-Step Instructions Panel (Right Side, Y = 84 to 146 mm) ─────
    sh.panel(110.0, 84.0, 92.0, 62.0, r_mm=2.5, fill=PAPER, edge=LINE_GREY, edge_mm=0.4)
    sh.text((114.0, 89.0), 'Anleitung in 5 Schritten:', 3.2, col=INK, anchor='lm', bold=True)
    steps = [
        '1. Auf A4 mit 100 % Tatsächliche Größe drucken.',
        '2. Kontrollmaß (100 mm) mit Lineal nachmessen.',
        '3. Vorlage flach auf Tisch mit Klebeband fixieren.',
        '4. Roboter auf Sockelumriss stellen (Pfeil nach vorn).',
        '5. ChArUco-Tafel anlegen: Orange auf Orange [1],',
        '   Blau auf Blau [2], Kante bündig an 180 mm!',
    ]
    for i, st in enumerate(steps):
        col = AMBER_DEEP if i >= 4 else INK_SOFT
        bold = i in (0, 4, 5)
        sh.text((114.0, 96.0 + i * 8.0), st, 2.5, col=col, anchor='lm', bold=bold)

    # ── 100 mm Horizontal Scale Check Ruler (Y = 152 to 168 mm) ───────────
    ruler_x0 = 104.0
    ruler_w = 100.0
    ruler_y = 152.0
    sh.panel(ruler_x0 - 3.0, ruler_y - 2.0, ruler_w + 6.0, 16.0, r_mm=2.0, fill=PAPER, edge=LINE_GREY, edge_mm=0.4)
    sh.text((ruler_x0 + ruler_w / 2, ruler_y + 2.0),
            '100 mm Kontrollmaß — mit Lineal prüfen (100 % Maßstab)',
            2.4, col=INK_SOFT, anchor='mm', bold=True)
    ruler_line_y = ruler_y + 8.5
    sh.line(sh.pg(ruler_x0, ruler_line_y), sh.pg(ruler_x0 + ruler_w, ruler_line_y), INK, 0.8)
    for mm_tick in range(101):
        tx = ruler_x0 + mm_tick
        if mm_tick % 10 == 0:
            th = 3.5
            sh.line(sh.pg(tx, ruler_line_y), sh.pg(tx, ruler_line_y - th), INK, 0.6)
            lbl = f'{mm_tick}' if mm_tick != 100 else '100 mm'
            sh.text((tx, ruler_line_y + 3.2), lbl, 2.2, col=INK, anchor='mt')
        elif mm_tick % 5 == 0:
            th = 2.0
            sh.line(sh.pg(tx, ruler_line_y), sh.pg(tx, ruler_line_y - th), INK_SOFT, 0.4)
        else:
            th = 1.0
            sh.line(sh.pg(tx, ruler_line_y), sh.pg(tx, ruler_line_y - th), INK_FAINT, 0.2)


def draw_omx_base(sh: Sheet) -> None:
    """Draw the 1:1 CAD footprint of the ROBOTIS OpenManipulator-X base."""
    # Base contour polygon
    poly_pts = [sh.base(x, y) for x, y in BASE_CONTOUR]
    cv2.fillPoly(sh.img, [np.array(poly_pts, dtype=np.int32)], BASE_FILL, cv2.LINE_AA)
    cv2.polylines(sh.img, [np.array(poly_pts, dtype=np.int32)], True, BASE_EDGE,
                  max(1, sh.mm(0.7)), cv2.LINE_AA)

    # 10 Mounting holes (diameter = 6.4 mm)
    r_hole = 3.2
    for hx, hy in BASE_HOLES:
        c = sh.base(hx, hy)
        cv2.circle(sh.img, c, max(1, sh.mm(r_hole)), BASE_HOLE, -1, cv2.LINE_AA)
        cv2.circle(sh.img, c, max(1, sh.mm(r_hole)), BASE_EDGE, max(1, sh.mm(0.5)), cv2.LINE_AA)
        d = max(1, sh.mm(r_hole + 1.8))
        sh.line((c[0] - d, c[1]), (c[0] + d, c[1]), BASE_EDGE, 0.3)
        sh.line((c[0], c[1] - d), (c[0], c[1] + d), BASE_EDGE, 0.3)

    # Base Frame Origin (link0 origin: x=0, y=0) -> Page Y = 230.0 mm
    c0 = sh.base(0.0, 0.0)
    cv2.circle(sh.img, c0, max(1, sh.mm(5.0)), PAPER, -1, cv2.LINE_AA)
    cv2.circle(sh.img, c0, max(1, sh.mm(5.0)), PRIMARY, max(1, sh.mm(0.8)), cv2.LINE_AA)
    cv2.circle(sh.img, c0, max(1, sh.mm(1.2)), PRIMARY, -1, cv2.LINE_AA)
    sh.line((c0[0] - sh.mm(12.0), c0[1]), (c0[0] + sh.mm(12.0), c0[1]), PRIMARY, 0.5)
    sh.line((c0[0], c0[1] - sh.mm(12.0)), (c0[0], c0[1] + sh.mm(12.0)), PRIMARY, 0.5)

    pos_c0_mm = sh.base_pt_mm(0.0, 0.0)
    sh.text((pos_c0_mm[0] + 10.0, pos_c0_mm[1] - 4.0),
            'Basis-Nullpunkt (x = 0, y = 0)', 3.4, col=PRIMARY, anchor='lm', bold=True)
    sh.text((pos_c0_mm[0] + 10.0, pos_c0_mm[1] + 2.0),
            'link0 Origin · Referenz für 180 mm Abstand', 2.5, col=INK_SOFT, anchor='lm')

    # Joint 1 Rotation Axis (x = -11.25 mm, y = 0) -> Page Y = 241.25 mm
    cj1 = sh.base(-11.25, 0.0)
    cv2.circle(sh.img, cj1, max(1, sh.mm(3.0)), RED_ACC, -1, cv2.LINE_AA)
    cv2.circle(sh.img, cj1, max(1, sh.mm(4.5)), RED_ACC, max(1, sh.mm(0.5)), cv2.LINE_AA)
    pos_j1_mm = sh.base_pt_mm(-11.25, 0.0)
    sh.text((pos_j1_mm[0] - 8.0, pos_j1_mm[1]),
            'Drehachse Gelenk 1 (x = -11.25 mm)', 2.8, col=RED_ACC, anchor='rm', bold=True)

    # Large Forward Arrow inside base (x = 18 to 52 mm) -> Page Y = 212 to 178 mm
    fwd_start = sh.base(18.0, 0.0)
    fwd_end = sh.base(52.0, 0.0)
    cv2.arrowedLine(sh.img, fwd_start, fwd_end, BASE_EDGE, max(1, sh.mm(1.0)),
                    cv2.LINE_AA, tipLength=0.28)
    pos_fwd_mm = sh.base_pt_mm(35.0, 0.0)
    sh.text((pos_fwd_mm[0] + 14.0, pos_fwd_mm[1]),
            '▲ VORNE / FORWARD (+X)', 3.6, col=BASE_EDGE, anchor='lm', bold=True)

    # Flange Labels
    pos_lf_mm = sh.base_pt_mm(-38.0, 62.5)
    sh.text(pos_lf_mm, 'Flansch LINKS (+Y)', 2.6, col=INK_SOFT, anchor='mm')
    pos_rf_mm = sh.base_pt_mm(-38.0, -62.5)
    sh.text(pos_rf_mm, 'Flansch RECHTS (-Y)', 2.6, col=INK_SOFT, anchor='mm')

    # Base Dimensions Callout (Width 150 mm)
    p_rear_l = sh.base_pt_mm(-60.0, 75.0)
    p_rear_r = sh.base_pt_mm(-60.0, -75.0)
    sh.line(sh.pg(p_rear_l[0], p_rear_l[1] + 3.0), sh.pg(p_rear_r[0], p_rear_r[1] + 3.0), INK_FAINT, 0.3)
    sh.text((sh.cx_mm, p_rear_l[1] + 5.5),
            'Sockelbreite 150,0 mm (entspricht exakt der Tafelbreite)',
            2.8, col=INK_SOFT, anchor='mm')


def render_omx_mat(dpi: int = DPI) -> np.ndarray:
    """Render the full A4 OMX alignment mat at the requested DPI."""
    sh = Sheet(dpi=dpi)
    draw_header(sh)
    draw_calibration_zone(sh)
    draw_intermediate_span(sh)
    draw_omx_base(sh)
    sh.flush_text()
    return sh.img


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--out',
        type=Path,
        default=Path('classroom_kit/omx_mat.pdf'),
        help='Output PDF path (default: classroom_kit/omx_mat.pdf)',
    )
    parser.add_argument(
        '--dpi',
        type=int,
        default=DPI,
        help=f'Resolution in DPI (default: {DPI})',
    )
    args = parser.parse_args()

    img_bgr = render_omx_mat(dpi=args.dpi)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    png_path = args.out.with_suffix('.png')
    cv2.imwrite(str(png_path), img_bgr)
    print(f'[OK] PNG: {png_path} ({img_bgr.shape[1]}x{img_bgr.shape[0]} px @ {args.dpi} DPI)')

    pil_img = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
    pil_img.save(str(args.out), format='PDF', resolution=args.dpi)
    print(f'[OK] PDF: {args.out} (A4 Portrait @ {args.dpi} DPI)')


if __name__ == '__main__':
    main()

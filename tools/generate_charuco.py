#!/usr/bin/env python3
"""Render the EduBotics ChArUco calibration board to a precision PDF.

Default specs match `physical_ai_server/workflow/calibration_manager.py`:
    7x5 squares, 30 mm square edge, 22 mm marker edge, DICT_5X5_250.

The board includes unambiguous orientation marks, matching color-coded
corner alignment brackets (Orange on left/Marker 0, Blue on right, Green in
center) that pair directly with the OMX base alignment mat, and a 100 mm ruler
verification bar so students and teachers can place the board with sub-millimeter
precision on the first try.

Usage:
    python tools/generate_charuco.py --out classroom_kit/charuco.pdf

Print the PDF at 100% scale ("Tatsächliche Größe" / no "fit to page"!) and
mount on a rigid foam-board or thick cardboard.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ── Board specification (authoritative constants) ──────────────────────────
SQUARES_X = 7
SQUARES_Y = 5
SQUARE_LENGTH_M = 0.030
MARKER_LENGTH_M = 0.022
ARUCO_DICT = cv2.aruco.DICT_5X5_250

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

PRIMARY     = _c(15, 76, 129)        # technical blue
AMBER_BG    = _c(254, 243, 199)      # warm amber highlight
AMBER_EDGE  = _c(217, 119, 6)        # vibrant amber (Corner 1)
AMBER_DEEP  = _c(180, 83, 9)

BLUE_BG     = _c(224, 238, 255)      # light blue highlight
BLUE_EDGE   = _c(37, 99, 235)        # royal blue (Corner 2)
BLUE_DEEP   = _c(29, 78, 216)

GREEN_BG    = _c(220, 245, 225)
GREEN_EDGE  = _c(22, 101, 52)        # forest green (Center)

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
             col: tuple[int, int, int], w_mm: float = 0.3, on_mm: float = 4.0,
             off_mm: float = 3.0):
        a = np.array(p0, float)
        b = np.array(p1, float)
        L = np.linalg.norm(b - a)
        if L < 1e-3:
            return
        u = (b - a) / L
        t = 0.0
        while t < L:
            s0 = a + u * t
            s1 = a + u * min(t + on_mm, L)
            self.line((s0[0], s0[1]), (s1[0], s1[1]), col, w_mm)
            t += on_mm + off_mm

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


def render_charuco_board_portrait(dpi: int = DPI) -> np.ndarray:
    """Render the ChArUco calibration board in A4 portrait layout.

    Features:
      - 5 squares (150 mm) width along left-right.
      - 7 squares (210 mm) length extending away from the robot.
      - Near edge (180 mm from base) at the bottom.
      - Color-coded matching corners:
          [1] Orange (◎) at Bottom-Left (Origin, Marker 0).
          [2] Blue (■) at Bottom-Right.
          [▲] Green Center Mark on the centerline.
      - Clean vertical rotated margin text.
      - 100 mm scale check ruler.
    """
    canvas = Canvas(210.0, 297.0, dpi=dpi)

    aruco_dict = cv2.aruco.getPredefinedDictionary(ARUCO_DICT)
    board = cv2.aruco.CharucoBoard(
        (SQUARES_X, SQUARES_Y),
        SQUARE_LENGTH_M,
        MARKER_LENGTH_M,
        aruco_dict,
    )

    # In OpenCV native image, width is 7 squares (210mm) and height is 5 squares (150mm).
    board_px_w = int(round(SQUARES_X * SQUARE_LENGTH_M * 1000 * canvas.k))
    board_px_h = int(round(SQUARES_Y * SQUARE_LENGTH_M * 1000 * canvas.k))
    raw_img = board.generateImage((board_px_w, board_px_h))

    # Rotate 90 CCW:
    # 5 squares (150mm) becomes horizontal width.
    # 7 squares (210mm) becomes vertical height.
    # Origin square (0,0) with adjacent Marker 0 moves to BOTTOM-LEFT!
    board_v = cv2.rotate(raw_img, cv2.ROTATE_90_COUNTERCLOCKWISE)

    # Position on A4 sheet:
    # Horizontal center: (210 - 150) / 2 = 30.0 mm margin left and right.
    pat_x0 = 30.0
    pat_x1 = 180.0
    # Vertical position: Y from 36.0 mm to 246.0 mm.
    pat_y0 = 36.0
    pat_y1 = pat_y0 + 210.0  # 246.0 mm
    pat_xc = (pat_x0 + pat_x1) / 2  # 105.0 mm

    # Paste the board pattern
    py0 = canvas.mm(pat_y0)
    px0 = canvas.mm(pat_x0)
    bh, bw = board_v.shape[:2]
    canvas.img[py0:py0 + bh, px0:px0 + bw] = cv2.cvtColor(board_v, cv2.COLOR_GRAY2BGR)

    # Outer border around pattern
    canvas.rect((pat_x0, pat_y0), (pat_x1, pat_y1), edge=INK, edge_mm=0.6)

    # ── Header Block (Top) ─────────────────────────────────────────────────
    canvas.rounded_panel(10.0, 6.0, 190.0, 22.0, r=3.0, fill=PAPER, edge=LINE_GREY, edge_mm=0.5)
    canvas.text((105.0, 11.5), 'EduBotics Roboter Studio — ChArUco-Kalibriertafel',
                5.0, col=INK, anchor='mm', bold=True)
    canvas.text((105.0, 17.0),
                '7×5 Felder (30 mm) · ArUco 22 mm (DICT_5X5_250) · 100 % Tatsächliche Größe drucken',
                3.4, col=PRIMARY, anchor='mm', bold=True)
    canvas.text((105.0, 22.0),
                'WICHTIG: Faltenfrei auf Foam-Board aufziehen · Nicht „an Seite anpassen"!',
                2.8, col=INK_SOFT, anchor='mm')

    # ── Edge Labels and Orientation Guidance ──────────────────────────────
    # FAR EDGE (Top): Away from robot
    canvas.text((105.0, 31.0), '▲   WEG VOM ROBOTER (FERNKANTE · x = 390 mm)   ▲',
                3.6, col=INK_SOFT, anchor='mb', bold=True)
    canvas.line((pat_x0, 33.5), (pat_x1, 33.5), INK_FAINT, 0.4)
    canvas.line((pat_x0, 32.0), (pat_x0, 35.0), INK_FAINT, 0.4)
    canvas.line((pat_x1, 32.0), (pat_x1, 35.0), INK_FAINT, 0.4)

    # NEAR EDGE (Bottom): Towards robot
    canvas.line((pat_x0, 248.5), (pat_x1, 248.5), INK_SOFT, 0.8)
    canvas.line((pat_x0, 247.0), (pat_x0, 250.0), INK_SOFT, 0.8)
    canvas.line((pat_x1, 247.0), (pat_x1, 250.0), INK_SOFT, 0.8)
    canvas.text((105.0, 251.5), '▼   ZUM ROBOTER (NAHKANTE · x = 180 mm)   ▼',
                3.8, col=INK, anchor='mt', bold=True)

    # Simple matching color guide line
    canvas.text((105.0, 256.0),
                '[1] Orange auf Orange (links)  ──▶  [▲] Grün auf Grün (Mitte)  ──▶  [2] Blau auf Blau (rechts)',
                2.8, col=PRIMARY, anchor='mt', bold=True)

    # LEFT EDGE (Robot Left, +Y) — Rotated 90 degrees reading along the edge
    canvas.rotated_text((15.0, 141.0),
                        '▲ ROBOTER-LINKS (+Y) — Lange Kante (210 mm) — Richtung +X (Vorne) ▲',
                        3.4, angle_deg=90.0, col=INK_SOFT, bold=True)

    # RIGHT EDGE (Robot Right, -Y) — Rotated 270 degrees reading along the edge
    canvas.rotated_text((195.0, 141.0),
                        '▼ ROBOTER-RECHTS (-Y) — Lange Kante (210 mm) — Richtung +X (Vorne) ▼',
                        3.4, angle_deg=270.0, col=INK_SOFT, bold=True)

    # ── CORNER 1: ORANGE BADGE (Bottom-Left: pat_x0, pat_y1) ───────────────
    # Origin Corner (Marker 0)
    oc_x, oc_y = pat_x0, pat_y1
    bracket_len = 16.0

    # Corner bracket in amber
    canvas.line((oc_x, oc_y + 3.0), (oc_x + bracket_len, oc_y + 3.0), AMBER_EDGE, 1.6)
    canvas.line((oc_x - 3.0, oc_y), (oc_x - 3.0, oc_y - bracket_len), AMBER_EDGE, 1.6)
    canvas.line((oc_x - 3.0, oc_y + 3.0), (oc_x + 2.0, oc_y + 3.0), AMBER_EDGE, 1.6)
    canvas.line((oc_x - 3.0, oc_y + 3.0), (oc_x - 3.0, oc_y - 2.0), AMBER_EDGE, 1.6)

    # Target bullseye badge (Orange)
    canvas.circle((oc_x - 7.0, oc_y + 7.0), 6.0, fill=AMBER_BG, edge=AMBER_EDGE, edge_mm=0.8)
    canvas.circle((oc_x - 7.0, oc_y + 7.0), 2.0, fill=AMBER_DEEP, edge=None)
    canvas.line((oc_x - 14.0, oc_y + 7.0), (oc_x, oc_y + 7.0), AMBER_EDGE, 0.4)
    canvas.line((oc_x - 7.0, oc_y), (oc_x - 7.0, oc_y + 14.0), AMBER_EDGE, 0.4)

    # Origin callout panel (Bottom-Left)
    canvas.rounded_panel(8.0, 262.0, 84.0, 24.0, r=2.5, fill=AMBER_BG, edge=AMBER_EDGE, edge_mm=0.6)
    canvas.text((50.0, 267.5), '[ 1 ] ORANGE ECKE (Marker 0)', 3.6, col=AMBER_DEEP, anchor='mm', bold=True)
    canvas.text((50.0, 273.0), 'URSPRUNG (0,0) · VORNE LINKS', 3.2, col=INK, anchor='mm', bold=True)
    canvas.text((50.0, 279.0), 'dem Roboter am nächsten (auf seiner linken Seite)', 2.3, col=INK_SOFT, anchor='mm')
    canvas.line((25.0, 262.0), (oc_x - 2.0, oc_y + 2.0), AMBER_DEEP, 0.8)

    # ── CORNER 2: BLUE BADGE (Bottom-Right: pat_x1, pat_y1) ────────────────
    rc_x, rc_y = pat_x1, pat_y1

    # Corner bracket in blue
    canvas.line((rc_x, rc_y + 3.0), (rc_x - bracket_len, rc_y + 3.0), BLUE_EDGE, 1.6)
    canvas.line((rc_x + 3.0, rc_y), (rc_x + 3.0, rc_y - bracket_len), BLUE_EDGE, 1.6)
    canvas.line((rc_x + 3.0, rc_y + 3.0), (rc_x - 2.0, rc_y + 3.0), BLUE_EDGE, 1.6)
    canvas.line((rc_x + 3.0, rc_y + 3.0), (rc_x + 3.0, rc_y - 2.0), BLUE_EDGE, 1.6)

    # Target square badge (Blue)
    canvas.rect((rc_x + 1.0, rc_y + 1.0), (rc_x + 13.0, rc_y + 13.0), fill=BLUE_BG, edge=BLUE_EDGE, edge_mm=0.8)
    canvas.rect((rc_x + 5.0, rc_y + 5.0), (rc_x + 9.0, rc_y + 9.0), fill=BLUE_DEEP, edge=None)

    # ── CENTER TICK: GREEN (pat_xc, pat_y1) ────────────────────────────────
    # Triangle marker pointing up to the centerline
    tri_pts = [
        canvas.pt(pat_xc, pat_y1 + 1.0),
        canvas.pt(pat_xc - 4.0, pat_y1 + 7.0),
        canvas.pt(pat_xc + 4.0, pat_y1 + 7.0),
    ]
    cv2.fillPoly(canvas.img, [np.array(tri_pts, dtype=np.int32)], GREEN_EDGE, cv2.LINE_AA)

    # ── 100 mm Calibration Scale Check (Bottom Right) ─────────────────────
    ruler_x0 = 98.0
    ruler_y = 265.0
    ruler_w = 100.0
    canvas.rounded_panel(ruler_x0 - 4.0, ruler_y - 3.0, ruler_w + 8.0, 24.0,
                         r=2.5, fill=PAPER, edge=LINE_GREY, edge_mm=0.4)
    canvas.text((ruler_x0 + ruler_w / 2, ruler_y + 1.0),
                '100 mm Kontrollmaß — mit Lineal prüfen (100 % Maßstab)',
                2.8, col=INK_SOFT, anchor='mm', bold=True)

    ruler_line_y = ruler_y + 10.0
    canvas.line((ruler_x0, ruler_line_y), (ruler_x0 + ruler_w, ruler_line_y), INK, 0.8)
    for mm_tick in range(101):
        tx = ruler_x0 + mm_tick
        if mm_tick % 10 == 0:
            th = 4.0
            canvas.line((tx, ruler_line_y), (tx, ruler_line_y - th), INK, 0.6)
            lbl = f'{mm_tick}' if mm_tick != 100 else '100 mm'
            canvas.text((tx, ruler_line_y + 3.8), lbl, 2.4, col=INK, anchor='mt')
        elif mm_tick % 5 == 0:
            th = 2.4
            canvas.line((tx, ruler_line_y), (tx, ruler_line_y - th), INK_SOFT, 0.4)
        else:
            th = 1.3
            canvas.line((tx, ruler_line_y), (tx, ruler_line_y - th), INK_FAINT, 0.25)

    # Bottom footer notice
    canvas.text((105.0, 292.0),
                'EduBotics Roboter Studio · Werkzeuge: tools/generate_charuco.py & tools/generate_omx_mat.py',
                2.4, col=INK_FAINT, anchor='mm')

    # ── Crop Marks for Mounting on 210 × 150 mm Board ─────────────────────
    crop_arm = 8.0
    for cx_mm, cy_mm, dx, dy in (
        (pat_x0, pat_y0, -1, -1),
        (pat_x1, pat_y0, 1, -1),
        (pat_x0, pat_y1, -1, 1),
        (pat_x1, pat_y1, 1, 1),
    ):
        canvas.line((cx_mm + dx * 2.0, cy_mm), (cx_mm + dx * (2.0 + crop_arm), cy_mm), INK_FAINT, 0.3)
        canvas.line((cx_mm, cy_mm + dy * 2.0), (cx_mm, cy_mm + dy * (2.0 + crop_arm)), INK_FAINT, 0.3)

    canvas.flush_text()
    return canvas.img


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--out',
        type=Path,
        default=Path('classroom_kit/charuco.pdf'),
        help='Output PDF path (default: classroom_kit/charuco.pdf)',
    )
    parser.add_argument(
        '--dpi',
        type=int,
        default=DPI,
        help=f'Resolution in DPI (default: {DPI})',
    )
    args = parser.parse_args()

    img_bgr = render_charuco_board_portrait(dpi=args.dpi)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    png_path = args.out.with_suffix('.png')
    cv2.imwrite(str(png_path), img_bgr)
    print(f'[OK] PNG: {png_path} ({img_bgr.shape[1]}x{img_bgr.shape[0]} px @ {args.dpi} DPI)')

    pil_img = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
    pil_img.save(str(args.out), format='PDF', resolution=args.dpi)
    print(f'[OK] PDF: {args.out} (A4 Portrait @ {args.dpi} DPI)')


if __name__ == '__main__':
    main()

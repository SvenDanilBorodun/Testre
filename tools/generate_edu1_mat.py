#!/usr/bin/env python3
"""Render the edu1_studio work mat (Arbeits-Matte) for the classroom kit.

WHY A SEPARATE MAT: the edu6 mat is a half fan of radius ~0.21 m with no camera
marking at all. Edu:1 is a DIFFERENT problem in three ways:

* it reaches 0.35 m, not 0.21 m, so the mat is ~3x the area and the scene
  camera can no longer see the whole of it at a useful tag size;
* the usable PLACEMENT zone is therefore NOT the reach ring - it is the
  INTERSECTION of the reach ring with the region where the scene camera still
  resolves a 24 mm AprilTag above the detector's cliff. This mat prints that
  intersection, so it can never promise a placement the perception refuses;
* the camera tower position, height and aim are part of the fixture, so they
  are ON the mat. Moving the tower invalidates the scene extrinsic.

Every dimension is READ FROM THE CODE (robot_profiles + the IK solver +
calibration_manager's board origin + the object catalog) or DERIVED from the
camera model below - never typed in twice.

The one thing this file cannot read from the code is the scene camera's real
horizontal field of view, because nothing in the tree measures it. It is a CLI
argument with a documented default, and the mat prints the assumption it was
generated under in its own title block. See ``--hfov``.

Usage:
    python tools/generate_edu1_mat.py                      # -> edu1_mat.pdf
    python tools/generate_edu1_mat.py --hfov 78 --cam-width 1280 --cam-height 720
    python tools/generate_edu1_mat.py --tiles a1           # 2 x A1 landscape

Print at 100% scale ("Tatsächliche Größe", never fit-to-page) and check the
printed 100 mm ruler mark with a real ruler before use.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import cv2
import numpy as np

DPI_DEFAULT = 200

_BLACK = (0, 0, 0)
_GREY = (165, 165, 165)
_LGREY = (215, 215, 215)
_RED = (60, 60, 220)
_GREEN = (90, 160, 60)
_LGREEN = (185, 225, 185)
_BLUE = (200, 120, 60)
_ORANGE = (40, 140, 235)
_VIOLET = (170, 70, 140)

# ── camera fixture (the thing this mat is FOR) ──────────────────────────────
# Lens standoff is bounded BELOW by the arm's own swept envelope: measured
# through the shipped box model (arm_geometry.EDU1_LINK_BOXES via
# ik.link_frames) over HOME + every solver grasp inside the printed ring + the
# joint-space glides between them, the arm reaches 480 mm in plan radius and
# NO body point passes x = 0.50 m. 0.55 m gives 70 mm of margin on a model that
# is already conservative (AABBs of the meshes).
LENS_X_M = 0.55
LENS_Z_M = 0.45
TAG_PX_MIN = 20.0     # detector cliff measured at 16 px (92 %) / 18 px (100 %)


def _load_geometry(profile_id: str = 'edu1_studio'):
    server = Path(__file__).resolve().parents[1] / 'physical_ai_tools' / 'physical_ai_server'
    sys.path.insert(0, str(server))
    from physical_ai_server import robot_profiles
    from physical_ai_server.workflow import arm_geometry as ag
    from physical_ai_server.workflow import calibration_manager as cm
    from physical_ai_server.workflow.object_catalog import fixed_catalog

    profile = robot_profiles.resolve(profile_id)
    if profile.profile_id != profile_id:
        raise SystemExit(f'unknown robot profile {profile_id!r}')
    if profile.reach_inner_m is None or profile.reach_outer_m is None:
        raise SystemExit(f'profile {profile_id!r} carries no reach bounds')
    solver = profile.build_ik()
    j1_lo, j1_hi = solver.joint_limits[0]
    catalog = fixed_catalog(profile_id)
    # most restrictive catalog object: tallest top face = smallest tag distance
    tag_z = max(float(catalog.recipe_for_type(t).object_height_m)
                for t in catalog.type_names())
    return {
        'profile': profile, 'solver': solver, 'catalog': catalog,
        'inner_m': float(profile.reach_inner_m),
        'outer_m': float(profile.reach_outer_m),
        'axis_x_m': float(solver.base_axis_x),
        'fan_lo': float(j1_lo), 'fan_hi': float(j1_hi),
        'base_box': ag.EDU1_LINK_BOXES[0],
        'link_boxes': ag.EDU1_LINK_BOXES,
        'home': list(profile.home_joints_rad[:5]),
        'board_origin': (cm.BOARD_ORIGIN_X_M, cm.BOARD_ORIGIN_Y_M),
        'board_yaw_deg': cm.BOARD_YAW_DEG,
        'board_sq': cm.CHARUCO_SQUARES_X, 'board_sq_y': cm.CHARUCO_SQUARES_Y,
        'board_sq_m': cm.CHARUCO_SQUARE_LENGTH_M,
        'aim_min_x': cm.SCENE_EXTRINSIC_REGION_X_MIN,
        'aim_max_x': cm.SCENE_EXTRINSIC_REGION_X_MAX,
        'aim_abs_y': cm.SCENE_EXTRINSIC_REGION_Y_ABS,
        'tag_size_m': float(catalog.tag_size_m),
        'tag_z_m': tag_z,
    }


def board_rect(geom):
    """(x0, x1, y0, y1) of the ChArUco board's printed black area, base frame.

    T_board_to_base = Rz(yaw) @ Rx(180°) at BOARD_ORIGIN, so with yaw 0 the
    board's +X is base +X (7 squares, the LONG edge, pointing away from the
    robot) and its +Y is base -Y (5 squares). The origin corner is therefore the
    NEAR (+x-min) corner on the arm's LEFT (+y)."""
    if abs(geom['board_yaw_deg']) > 1e-6:
        raise SystemExit('this mat only draws a board at BOARD_YAW_DEG = 0')
    ox, oy = geom['board_origin']
    w = geom['board_sq'] * geom['board_sq_m']       # along +x
    h = geom['board_sq_y'] * geom['board_sq_m']     # along -y
    return ox, ox + w, oy - h, oy


def camera_pose(lens_x, lens_z, tilt_deg):
    t = math.radians(tilt_deg)
    z_cam = np.array([-math.cos(t), 0.0, -math.sin(t)])   # optical axis, look back
    x_cam = np.array([0.0, 1.0, 0.0])                     # image right = base +y
    return np.array([lens_x, 0.0, lens_z]), x_cam, np.cross(z_cam, x_cam), z_cam


def aim_point(lens_x, lens_z, tilt_deg):
    return lens_x - lens_z / math.tan(math.radians(tilt_deg))


def choose_tilt(geom, lens_x, lens_z, hfov, cam_w, cam_h):
    """The tilt that maximises the margin (px) between the image border and the
    farthest thing that MUST be framed: the whole reach ring at tag height plus
    the ChArUco board. Restricted to tilts whose optical axis lands inside
    calibration_manager's legal aim window, or the extrinsic is refused."""
    fx = (cam_w / 2) / math.tan(math.radians(hfov) / 2)
    must = []
    for r in np.linspace(geom['inner_m'], geom['outer_m'], 12):
        for b in np.linspace(geom['fan_lo'], geom['fan_hi'], 60):
            must.append((geom['axis_x_m'] + r * math.cos(b), r * math.sin(b),
                         geom['tag_z_m']))
    x0, x1, y0, y1 = board_rect(geom)
    for x in np.linspace(x0, x1, 9):
        for y in np.linspace(y0, y1, 7):
            must.append((x, y, 0.0))
    must = np.array(must)
    best = None
    for tenth in range(300, 801):
        tilt = tenth / 10.0
        ax = aim_point(lens_x, lens_z, tilt)
        if not (geom['aim_min_x'] <= ax <= geom['aim_max_x']):
            continue
        if math.sin(math.radians(tilt)) < 0.30:      # the downward-axis gate
            continue
        C, xc, yc, zc = camera_pose(lens_x, lens_z, tilt)
        v = must - C
        c = v @ zc
        if (c <= 0).any():
            continue
        u = fx * (v @ xc) / c + cam_w / 2
        w = fx * (v @ yc) / c + cam_h / 2
        m = min(u.min(), cam_w - u.max(), w.min(), cam_h - w.max())
        if best is None or m > best[1]:
            best = (tilt, m, ax)
    if best is None:
        raise SystemExit('no tilt satisfies the legal aim window at this standoff')
    return best


def _seg_hits_aabb(p0, p1, lo, hi):
    d = p1 - p0
    t0, t1 = 0.0, 1.0
    for i in range(3):
        if abs(d[i]) < 1e-12:
            if p0[i] < lo[i] or p0[i] > hi[i]:
                return False
        else:
            a, b = (lo[i] - p0[i]) / d[i], (hi[i] - p0[i]) / d[i]
            if a > b:
                a, b = b, a
            t0, t1 = max(t0, a), min(t1, b)
            if t0 > t1:
                return False
    return True


def placement_zone(geom, lens_x, lens_z, tilt, hfov, cam_w, cam_h,
                   tag_px_min=TAG_PX_MIN, step=0.002):
    """Boolean mask over a base-frame grid: cells where a catalog object may be
    placed. A cell qualifies only if ALL of

      (a) it is inside the arm's reach ring and joint-1 fan;
      (b) its tag centre projects inside the image;
      (c) the tag's SHORT apparent edge clears ``tag_px_min`` (the foreshortened
          axis binds, so obliquity is charged for);
      (d) the arm standing at HOME does not block the line of sight - HOME is
          the observe pose ``motion._observe_joints`` falls through to, so it is
          exactly where the arm stands while the camera looks.

    Returns (grid Nx2, mask, tag_px)."""
    solver = geom['solver']
    frames = solver.link_frames(geom['home'])
    home_boxes = []
    for T, (lo, hi) in zip(frames, geom['link_boxes']):
        corners = np.array([[lo[0], lo[1], lo[2]], [lo[0], lo[1], hi[2]],
                            [lo[0], hi[1], lo[2]], [lo[0], hi[1], hi[2]],
                            [hi[0], lo[1], lo[2]], [hi[0], lo[1], hi[2]],
                            [hi[0], hi[1], lo[2]], [hi[0], hi[1], hi[2]]])
        w = (T[:3, :3] @ corners.T).T + T[:3, 3]
        home_boxes.append((w.min(0), w.max(0)))

    R = geom['outer_m']
    gx, gy = np.meshgrid(np.arange(-R - step, R + step, step),
                         np.arange(-R - step, R + step, step))
    G = np.column_stack([gx.ravel(), gy.ravel()])
    dx = G[:, 0] - geom['axis_x_m']
    rr = np.hypot(dx, G[:, 1])
    bb = np.arctan2(G[:, 1], dx)
    in_ring = ((rr >= geom['inner_m']) & (rr <= geom['outer_m'])
               & (bb >= geom['fan_lo']) & (bb <= geom['fan_hi']))

    fx = (cam_w / 2) / math.tan(math.radians(hfov) / 2)
    C, xc, yc, zc = camera_pose(lens_x, lens_z, tilt)
    Pw = np.column_stack([G[:, 0], G[:, 1], np.full(len(G), geom['tag_z_m'])])
    v = Pw - C
    c = v @ zc
    with np.errstate(divide='ignore', invalid='ignore'):
        u = fx * (v @ xc) / c + cam_w / 2
        w = fx * (v @ yc) / c + cam_h / 2
    in_frame = (c > 0) & (u >= 0) & (u < cam_w) & (w >= 0) & (w < cam_h)
    d = np.linalg.norm(v, axis=1)
    tag_px = fx * geom['tag_size_m'] * np.abs(v[:, 2]) / d / d
    cand = in_ring & in_frame & (tag_px >= tag_px_min)
    mask = cand.copy()
    for i in np.nonzero(cand)[0]:
        if any(_seg_hits_aabb(C, C + (Pw[i] - C) * 0.995, lo, hi)
               for lo, hi in home_boxes):
            mask[i] = False
    return G, mask, tag_px, in_ring, step


# ── design system ──────────────────────────────────────────────────────────
def _c(r, g, b):
    return (b, g, r)                     # author in RGB, store BGR for cv2

PAPER      = _c(253, 252, 249)
INK        = _c(34, 37, 43)
INK_SOFT   = _c(108, 114, 122)
INK_FAINT  = _c(186, 190, 196)
HAIR       = _c(224, 226, 228)

ZONE_FILL  = _c(211, 232, 214)
ZONE_EDGE  = _c(52, 118, 74)
ZONE_DEEP  = _c(35, 88, 55)

RING_FILL  = _c(241, 240, 236)
RING_EDGE  = _c(178, 180, 176)

STOP_FILL  = _c(249, 227, 224)
STOP_EDGE  = _c(191, 64, 56)

BOARD_EDGE = _c(43, 102, 166)
BOARD_FILL = _c(226, 236, 247)

CAM_EDGE   = _c(129, 58, 137)
CAM_FILL   = _c(240, 228, 242)

PANEL_BG   = _c(255, 255, 255)

MARGIN_MM  = 14.0
HEADER_MM  = 58.0
X_LO, X_HI = -0.130, 0.610
Y_ABS      = 0.375

_FACES_REG = (r'C:\Windows\Fonts\arial.ttf',
              '/System/Library/Fonts/Supplemental/Arial.ttf',
              '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
              '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf')
_FACES_BLD = (r'C:\Windows\Fonts\arialbd.ttf',
              '/System/Library/Fonts/Supplemental/Arial Bold.ttf',
              '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
              '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf')
_ASCII = {'ä': 'ae', 'ö': 'oe', 'ü': 'ue', 'Ä': 'Ae', 'Ö': 'Oe', 'Ü': 'Ue',
          'ß': 'ss', '—': '-', '–': '-', '·': '-', '°': ' Grad', '„': '"',
          '“': '"', '×': 'x', '≥': '>=', '≤': '<=', '→': '->', '²': '2'}


class Sheet:
    """Millimetre-native drawing surface. Everything downstream speaks mm and
    base-frame metres; only this class knows about pixels."""

    def __init__(self, dpi):
        self.dpi = dpi
        self.k = dpi / 25.4                       # px per mm
        self.ppm = dpi / 0.0254                   # px per metre
        self.w_mm = 2 * Y_ABS * 1000 + 2 * MARGIN_MM
        self.h_mm = HEADER_MM + (X_HI - X_LO) * 1000 + MARGIN_MM
        self.W = int(round(self.w_mm * self.k))
        self.H = int(round(self.h_mm * self.k))
        self.img = np.full((self.H, self.W, 3), 0, np.uint8)
        self.img[:] = PAPER
        self.text = []

    # -- coordinates ------------------------------------------------------
    def mm(self, v):
        return int(round(v * self.k))

    def pg(self, x_mm, y_mm):
        """page millimetres (origin top-left) -> pixels"""
        return (int(round(x_mm * self.k)), int(round(y_mm * self.k)))

    def P(self, x, y):
        """base-frame metres -> pixels. +x is UP the sheet, +y is LEFT (ROS:
        +y is the arm's left, and a reader behind the arm shares that left)."""
        return (int(round((MARGIN_MM + (Y_ABS - y) * 1000) * self.k)),
                int(round((HEADER_MM + (X_HI - x) * 1000) * self.k)))

    # -- primitives -------------------------------------------------------
    def line(self, p0, p1, col, w_mm, aa=True):
        cv2.line(self.img, p0, p1, col, max(1, self.mm(w_mm)),
                 cv2.LINE_AA if aa else cv2.LINE_8)

    def dash(self, p0, p1, col, w_mm, on=5.0, off=3.5):
        p0 = np.array(p0, float); p1 = np.array(p1, float)
        L = np.linalg.norm(p1 - p0)
        if L < 1:
            return
        u = (p1 - p0) / L
        on_px, off_px = on * self.k, off * self.k
        t = 0.0
        while t < L:
            a = p0 + u * t
            b = p0 + u * min(t + on_px, L)
            self.line(tuple(a.astype(int)), tuple(b.astype(int)), col, w_mm)
            t += on_px + off_px

    def panel(self, x_mm, y_mm, w, h, r=4.0, fill=PANEL_BG, edge=INK_FAINT,
              edge_mm=0.45):
        p0, p1 = self.pg(x_mm, y_mm), self.pg(x_mm + w, y_mm + h)
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
                cv2.ellipse(self.img, (cx, cy), (rr, rr), 0, a0, a0 + 90, edge,
                            w_px, cv2.LINE_AA)

    def poly_alpha(self, pts_m, fill, alpha):
        a = np.array([self.P(*p) for p in pts_m], np.int32)
        ov = self.img.copy()
        cv2.fillPoly(ov, [a], fill, cv2.LINE_AA)
        cv2.addWeighted(ov, alpha, self.img, 1 - alpha, 0, self.img)

    def poly(self, pts_m, col, w_mm, closed=True, fill=None):
        a = np.array([self.P(*p) for p in pts_m], np.int32)
        if fill is not None:
            cv2.fillPoly(self.img, [a], fill, cv2.LINE_AA)
        if col is not None:
            cv2.polylines(self.img, [a], closed, col, max(1, self.mm(w_mm)), cv2.LINE_AA)

    def say(self, pos, s, size_mm, col=INK, anchor='mm', bold=False, halo=None,
            spacing=None):
        self.text.append((pos, s, size_mm, col, anchor, bold, halo, spacing))

    def flush_text(self):
        from PIL import Image, ImageDraw, ImageFont
        reg = next((p for p in _FACES_REG if Path(p).exists()), None)
        bld = next((p for p in _FACES_BLD if Path(p).exists()), reg)
        if reg is None:
            print('[WARNUNG] keine TrueType-Schrift — ASCII-Ersatz, ohne Umlaute.')
            for (pos, s, sz, col, anc, bold, halo, sp) in self.text:
                t = s
                for a, b in _ASCII.items():
                    t = t.replace(a, b)
                cv2.putText(self.img, t.encode('ascii', 'replace').decode(),
                            (int(pos[0]), int(pos[1] + self.mm(sz))),
                            cv2.FONT_HERSHEY_SIMPLEX, self.mm(sz) / 28.0, col,
                            max(1, self.mm(sz) / 12), cv2.LINE_AA)
            self.text = []
            return
        pil = Image.fromarray(cv2.cvtColor(self.img, cv2.COLOR_BGR2RGB))
        dr = ImageDraw.Draw(pil)
        cache = {}
        A = {'lt': 'la', 'mt': 'ma', 'rt': 'ra', 'lm': 'lm', 'mm': 'mm',
             'rm': 'rm', 'lb': 'ls', 'mb': 'ms', 'rb': 'rs'}
        for (pos, s, sz, col, anc, bold, halo, sp) in self.text:
            key = (sz, bold)
            if key not in cache:
                cache[key] = ImageFont.truetype(bld if bold else reg,
                                                max(6, self.mm(sz)))
            kw = {}
            if halo is not None:
                kw['stroke_width'] = max(1, self.mm(sz * 0.16))
                kw['stroke_fill'] = (halo[2], halo[1], halo[0])
            if sp is not None:
                kw['spacing'] = self.mm(sp)
            dr.text((pos[0], pos[1]), s, font=cache[key],
                    fill=(col[2], col[1], col[0]), anchor=A[anc], **kw)
        self.img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
        self.text = []


# ── plan view ──────────────────────────────────────────────────────────────
def _sector(sh, r_in, r_out, lo, hi, ax, n=360):
    out = [(ax + r_out * math.cos(lo + (hi - lo) * k / n),
            r_out * math.sin(lo + (hi - lo) * k / n)) for k in range(n + 1)]
    inn = [(ax + r_in * math.cos(hi - (hi - lo) * k / n),
            r_in * math.sin(hi - (hi - lo) * k / n)) for k in range(n + 1)]
    return out + inn


def draw_field(sh, geom, zone):
    ax, lo, hi = geom['axis_x_m'], geom['fan_lo'], geom['fan_hi']
    ri, ro = geom['inner_m'], geom['outer_m']
    G, mask, tag_px, in_ring, step = zone

    # reachable-but-not-seen band
    sh.poly(_sector(sh, ri, ro, lo, hi, ax), None, 0, fill=RING_FILL)

    # placement zone, rasterised then smoothed (a 2 mm grid edge drawn literally
    # reads as a manufacturing feature, which it is not)
    n = int(round(math.sqrt(len(G))))
    grid = mask.reshape(n, n)
    gx = G[:, 0].reshape(n, n)[0]
    gy = G[:, 1].reshape(n, n)[:, 0]
    m = np.zeros((sh.H, sh.W), np.uint8)
    for iy in range(n):
        row = grid[iy]
        if not row.any():
            continue
        j = 0
        while j < n:
            if row[j]:
                k = j
                while k + 1 < n and row[k + 1]:
                    k += 1
                p0 = sh.P(gx[j] - step / 2, gy[iy] + step / 2)
                p1 = sh.P(gx[k] + step / 2, gy[iy] - step / 2)
                cv2.rectangle(m, (min(p0[0], p1[0]), min(p0[1], p1[1])),
                              (max(p0[0], p1[0]), max(p0[1], p1[1])), 255, -1)
                j = k + 1
            else:
                j += 1
    kk = max(3, sh.mm(6.0) | 1)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE,
                         cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kk, kk)))
    m = cv2.GaussianBlur(m, (kk, kk), 0)
    m = (m > 127).astype(np.uint8) * 255
    # The rim IS the reach circle, so draw it analytically rather than letting a
    # 2 mm sampling grid decide where it runs: clip the smoothed camera mask
    # against an exact sector. Only the camera-limited edge stays interpolated.
    ring = np.zeros_like(m)
    cv2.fillPoly(ring, [np.array([sh.P(*q) for q in
                                  _sector(sh, ri, ro, lo, hi, ax, 1440)], np.int32)],
                 255, cv2.LINE_AA)
    m = cv2.bitwise_and(m, ring)
    cont, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cont = [c for c in cont if cv2.contourArea(c) > sh.mm(15) ** 2]
    cv2.fillPoly(sh.img, cont, ZONE_FILL, cv2.LINE_AA)
    cv2.drawContours(sh.img, cont, -1, ZONE_EDGE, max(1, sh.mm(1.1)), cv2.LINE_AA)

    # radial grid every 50 mm, labelled every 100 mm
    r = 0.05
    while r < ro - 1e-9:
        if r > ri:
            pts = [sh.P(ax + r * math.cos(lo + (hi - lo) * k / 240),
                        r * math.sin(lo + (hi - lo) * k / 240)) for k in range(241)]
            for k in range(0, 240, 10):
                sh.line(pts[k], pts[k + 5], INK_FAINT, 0.22)
            if int(round(r * 1000)) % 100 == 0:
                b = math.radians(76)
                q = sh.P(ax + r * math.cos(b), r * math.sin(b))
                sh.say(q, f'{r*1000:.0f}', 4.2, INK_SOFT, 'mm', halo=RING_FILL)
        r += 0.05
    for bd in (-75, -60, -45, -30, -15, 0, 15, 30, 45, 60, 75):
        b = math.radians(bd)
        sh.dash(sh.P(ax + ri * math.cos(b), ri * math.sin(b)),
                sh.P(ax + ro * math.cos(b), ro * math.sin(b)), INK_FAINT, 0.22, 3.0, 4.0)

    # ring boundary + fan edges
    sh.poly(_sector(sh, ri, ro, lo, hi, ax), RING_EDGE, 0.7)
    # keep-out
    sh.poly([(ax + ri * math.cos(2 * math.pi * k / 240), ri * math.sin(2 * math.pi * k / 240))
             for k in range(240)], STOP_EDGE, 0.9, fill=STOP_FILL)

    # ring caption
    b = math.radians(66)
    q = sh.P(ax + (ro - 0.026) * math.cos(b), (ro - 0.026) * math.sin(b))
    sh.say(q, f'Reichweite des Arms', 5.0, INK_SOFT, 'mm', halo=RING_FILL)
    sh.say((q[0], q[1] + sh.mm(6)), f'{ro*1000:.0f} mm', 5.0, INK_SOFT, 'mm',
           halo=RING_FILL)

    # zone caption, twice, clear of the board
    for sgn in (+1, -1):
        b = math.radians(48.0 * sgn)
        q = sh.P(ax + 0.288 * math.cos(b), 0.288 * math.sin(b))
        sh.say((q[0], q[1] - sh.mm(4)), 'GREIFZONE', 11.0, ZONE_DEEP, 'mm',
               bold=True, halo=ZONE_FILL)
        sh.say((q[0], q[1] + sh.mm(6)), 'Würfel nur hier hinlegen', 5.4,
               ZONE_EDGE, 'mm', halo=ZONE_FILL)

    # check crosses
    gm = G[mask]
    checks = []
    if len(gm):
        rr = np.hypot(gm[:, 0] - ax, gm[:, 1])
        bg = np.degrees(np.arctan2(gm[:, 1], gm[:, 0] - ax))
        for t in (-58, -35, 0, 35, 58):
            sel = np.abs(bg - t) < 4
            if sel.any():
                checks.append(tuple(gm[np.argmax(np.where(sel, rr, -1))]))
        sel = np.abs(bg) < 4
        if sel.any():
            checks.append(tuple(gm[np.argmin(np.where(sel, rr, 9))]))
    for (cx_, cy_) in checks:
        q = sh.P(cx_, cy_)
        d = sh.mm(4.2)
        cv2.circle(sh.img, q, sh.mm(3.0), PAPER, -1, cv2.LINE_AA)
        cv2.circle(sh.img, q, sh.mm(3.0), ZONE_EDGE, max(1, sh.mm(0.7)), cv2.LINE_AA)
        sh.line((q[0] - d, q[1]), (q[0] + d, q[1]), ZONE_EDGE, 0.7)
        sh.line((q[0], q[1] - d), (q[0], q[1] + d), ZONE_EDGE, 0.7)
    return checks


def draw_board(sh, geom):
    x0, x1, y0, y1 = board_rect(geom)
    pad = 0.010
    sh.poly_alpha([(x0 - pad, y1 + pad), (x1 + pad, y1 + pad),
                   (x1 + pad, y0 - pad), (x0 - pad, y0 - pad)], BOARD_FILL, 0.20)
    for a, b in (((x0 - pad, y1 + pad), (x1 + pad, y1 + pad)),
                 ((x1 + pad, y1 + pad), (x1 + pad, y0 - pad)),
                 ((x1 + pad, y0 - pad), (x0 - pad, y0 - pad)),
                 ((x0 - pad, y0 - pad), (x0 - pad, y1 + pad))):
        sh.dash(sh.P(*a), sh.P(*b), BOARD_EDGE, 0.4, 3.0, 2.5)
    sh.poly_alpha([(x0, y1), (x1, y1), (x1, y0), (x0, y0)], BOARD_FILL, 0.30)
    sh.poly([(x0, y1), (x1, y1), (x1, y0), (x0, y0)], BOARD_EDGE, 0.9)
    for k in range(1, geom['board_sq']):
        x = x0 + k * geom['board_sq_m']
        sh.line(sh.P(x, y1), sh.P(x, y0), _c(150, 186, 226), 0.22)
    for k in range(1, geom['board_sq_y']):
        y = y0 + k * geom['board_sq_m']
        sh.line(sh.P(x0, y), sh.P(x1, y), _c(150, 186, 226), 0.22)
    oc = sh.P(x0, y1)
    cv2.circle(sh.img, oc, sh.mm(4.0), PAPER, -1, cv2.LINE_AA)
    cv2.circle(sh.img, oc, sh.mm(4.0), BOARD_EDGE, max(1, sh.mm(0.9)), cv2.LINE_AA)
    cv2.circle(sh.img, oc, sh.mm(1.5), BOARD_EDGE, -1, cv2.LINE_AA)
    sh.say((oc[0] - sh.mm(6), oc[1] + sh.mm(1)), 'Ursprungs-Ecke', 5.2,
           BOARD_EDGE, 'rm', bold=True, halo=PAPER)
    sh.say((oc[0] - sh.mm(6), oc[1] + sh.mm(7)),
           'obere linke Ecke des Ausdrucks', 4.2, BOARD_EDGE, 'rm', halo=PAPER)
    mid = sh.P((x0 + x1) / 2, (y0 + y1) / 2)
    sh.say((mid[0], mid[1] - sh.mm(30)), 'ChArUco-Tafel', 8.0, BOARD_EDGE, 'mm',
           bold=True, halo=PAPER)
    sh.say((mid[0], mid[1] - sh.mm(23)),
           f'{(x1-x0)*1000:.0f} × {(y1-y0)*1000:.0f} mm · {geom["board_sq"]}×'
           f'{geom["board_sq_y"]} Felder à {geom["board_sq_m"]*1000:.0f} mm',
           4.6, BOARD_EDGE, 'mm', halo=PAPER)
    sh.say((mid[0], mid[1] - sh.mm(17)),
           'lange Kante zeigt vom Roboter weg · nur zum Kalibrieren auflegen',
           4.2, BOARD_EDGE, 'mm', halo=PAPER)


def draw_camera(sh, geom, tilt, aim, lens_x, lens_z):
    tf = sh.P(lens_x, 0.0)
    sh.dash(tf, sh.P(aim, 0.0), CAM_EDGE, 0.6, 6.0, 4.0)
    ap = sh.P(aim, 0.0)
    cv2.circle(sh.img, ap, sh.mm(11.0), PAPER, -1, cv2.LINE_AA)
    for rr, wmm in ((11.0, 0.7), (7.0, 0.7), (3.2, 0.7)):
        cv2.circle(sh.img, ap, sh.mm(rr), CAM_EDGE, max(1, sh.mm(wmm)), cv2.LINE_AA)
    cv2.circle(sh.img, ap, sh.mm(1.2), CAM_EDGE, -1, cv2.LINE_AA)
    for d in (-1, 1):
        sh.line((ap[0] + d * sh.mm(4), ap[1]), (ap[0] + d * sh.mm(15), ap[1]), CAM_EDGE, 0.5)
        sh.line((ap[0], ap[1] + d * sh.mm(4)), (ap[0], ap[1] + d * sh.mm(15)), CAM_EDGE, 0.5)
    sh.say((ap[0] + sh.mm(18), ap[1] - sh.mm(3)), 'Kamera-Zielpunkt', 5.6,
           CAM_EDGE, 'lm', bold=True, halo=PAPER)
    sh.say((ap[0] + sh.mm(18), ap[1] + sh.mm(3.5)),
           'Bildmitte hierher — erst ausrichten, dann Tafel auflegen',
           4.2, CAM_EDGE, 'lm', halo=PAPER)

    cv2.circle(sh.img, tf, sh.mm(26.0), CAM_FILL, -1, cv2.LINE_AA)
    cv2.circle(sh.img, tf, sh.mm(26.0), CAM_EDGE, max(1, sh.mm(0.9)), cv2.LINE_AA)
    cv2.circle(sh.img, tf, sh.mm(15.0), CAM_EDGE, max(1, sh.mm(0.5)), cv2.LINE_AA)
    cv2.circle(sh.img, tf, sh.mm(2.0), CAM_EDGE, -1, cv2.LINE_AA)
    for d in (-1, 1):
        sh.line((tf[0] + d * sh.mm(4), tf[1]), (tf[0] + d * sh.mm(32), tf[1]), CAM_EDGE, 0.5)
        sh.line((tf[0], tf[1] + d * sh.mm(4)), (tf[0], tf[1] + d * sh.mm(32)), CAM_EDGE, 0.5)
    sh.say((tf[0], tf[1] - sh.mm(34)), 'KAMERA-TURM', 8.0, CAM_EDGE, 'mb', bold=True)
    for i, s in enumerate((f'Objektiv {lens_z*1000:.0f} mm über dem Tisch',
                           f'{tilt:.0f}° nach unten, Blick zum Roboter',
                           'nach dem Kalibrieren NICHT mehr bewegen')):
        sh.say((tf[0], tf[1] + sh.mm(32) + i * sh.mm(6.4)), s, 4.8,
               CAM_EDGE if i < 2 else STOP_EDGE, 'mt')


def draw_robot(sh, geom):
    ax = geom['axis_x_m']
    (lo, hi) = geom['base_box']
    sh.poly([(ax + lo[0], hi[1]), (ax + hi[0], hi[1]),
             (ax + hi[0], lo[1]), (ax + lo[0], lo[1])], INK, 1.1, fill=_c(238, 239, 241))
    c0 = sh.P(ax, 0.0)
    cv2.circle(sh.img, c0, sh.mm(7.0), PAPER, -1, cv2.LINE_AA)
    cv2.circle(sh.img, c0, sh.mm(7.0), INK, max(1, sh.mm(0.7)), cv2.LINE_AA)
    for d in (-1, 1):
        sh.line((c0[0] + d * sh.mm(2), c0[1]), (c0[0] + d * sh.mm(12), c0[1]), INK, 0.6)
        sh.line((c0[0], c0[1] + d * sh.mm(2)), (c0[0], c0[1] + d * sh.mm(12)), INK, 0.6)
    cv2.circle(sh.img, c0, sh.mm(1.1), INK, -1, cv2.LINE_AA)
    a0, a1 = sh.P(ax + hi[0] + 0.006, 0), sh.P(ax + hi[0] + 0.040, 0)
    cv2.arrowedLine(sh.img, a0, a1, INK, max(1, sh.mm(1.0)), cv2.LINE_AA, tipLength=0.32)
    sh.say((a1[0] + sh.mm(3.5), a1[1]), 'vorne', 5.0, INK, 'lm', halo=ZONE_FILL)
    rb = sh.P(ax + lo[0] - 0.012, 0)
    sh.say((rb[0], rb[1]), 'ROBOTER  Edu:1', 7.6, INK, 'mt', bold=True)
    sh.say((rb[0], rb[1] + sh.mm(9)),
           f'Sockel {(hi[0]-lo[0])*1000:.0f} × {(hi[1]-lo[1])*1000:.0f} mm · '
           f'Fadenkreuz = Achse Gelenk 1 = Nullpunkt', 4.6, INK_SOFT, 'mt')
    ri = geom['inner_m']
    sh.poly([(ax + ri * math.cos(2 * math.pi * k / 240),
              ri * math.sin(2 * math.pi * k / 240)) for k in range(240)],
            STOP_EDGE, 0.9)
    lp = sh.P(ax + 0.050, -0.120)
    sh.line(sh.P(ax + 0.041, -0.0565), lp, STOP_EDGE, 0.4)
    sh.say((lp[0] + sh.mm(2), lp[1]),
           f'Sperrbereich {geom["inner_m"]*1000:.0f} mm', 4.8, STOP_EDGE, 'lm', halo=PAPER)


def draw_header(sh, geom, tilt, aim, hfov, cw, ch, lens_x, lens_z, zone, tag_px_min):
    G, mask, tag_px, in_ring, step = zone
    area = mask.sum() * step * step * 1e4
    pct = 100.0 * mask.sum() / max(1, in_ring.sum())
    sh.say(sh.pg(MARGIN_MM, 15.0), 'Arbeits-Matte  Edu:1', 15.0, INK, 'lm', bold=True)
    sh.say(sh.pg(MARGIN_MM, 27.5), 'EduBotics · Roboter Studio · 1:1 drucken, '
           'nicht „an Seite anpassen“', 5.4, INK_SOFT, 'lm')
    x = sh.w_mm - MARGIN_MM
    rows = [(f'Greifzone {area:.0f} cm²', f'{pct:.0f} % der Reichweite'),
            (f'Kamera {lens_x*1000:.0f} mm vor der Achse',
             f'{lens_z*1000:.0f} mm hoch · {tilt:.0f}° geneigt'),
            (f'gilt für {cw}×{ch} px, {hfov:.0f}° horizontal',
             f'AprilTag {geom["tag_size_m"]*1000:.0f} mm ≥ {tag_px_min:.0f} px')]
    for i, (a, b) in enumerate(rows):
        sh.say(sh.pg(x - 78, 9.0 + i * 6.6), a, 4.6, INK_SOFT, 'rt')
        sh.say(sh.pg(x, 9.0 + i * 6.6), b, 4.6, INK_SOFT, 'rt')
    sh.line(sh.pg(MARGIN_MM, HEADER_MM - 9.0), sh.pg(sh.w_mm - MARGIN_MM, HEADER_MM - 9.0),
            INK_FAINT, 0.4)


def draw_elevation(sh, geom, tilt, aim, lens_x, lens_z):
    """To-scale side view. The point a builder must see before cutting a tube:
    the arm at HOME stands TALLER than a 450 mm lens."""
    PW, PH = 262.0, 196.0
    px0 = MARGIN_MM + 2.0
    py0 = HEADER_MM + 3.0
    sh.panel(px0, py0, PW, PH, fill=_c(252, 251, 248))
    sh.say(sh.pg(px0 + 9, py0 + 9), 'Seitenansicht', 6.6, INK, 'lt', bold=True)
    sh.say(sh.pg(px0 + PW - 9, py0 + 10), '1 : 4', 5.0, INK_FAINT, 'rt')
    sc = 0.25
    ox, oz = px0 + 42.0, py0 + PH - 30.0

    def Q(x, z):
        return sh.pg(ox + x * 1000 * sc, oz - z * 1000 * sc)

    sh.line(Q(-0.100, 0), Q(0.655, 0), INK, 1.0)
    for t in np.arange(-0.098, 0.655, 0.020):
        sh.line(Q(t, 0), Q(t - 0.013, -0.015), INK_FAINT, 0.35)
    frames = geom['solver'].link_frames(geom['home'])
    tops = []
    for T, (lo, hi) in zip(frames, geom['link_boxes']):
        cs = np.array([[a, b, c] for a in (lo[0], hi[0]) for b in (lo[1], hi[1])
                       for c in (lo[2], hi[2])])
        w = (T[:3, :3] @ cs.T).T + T[:3, 3]
        tops.append(w[:, 2].max())
        p0, p1 = Q(w[:, 0].min(), w[:, 2].max()), Q(w[:, 0].max(), w[:, 2].min())
        cv2.rectangle(sh.img, p0, p1, _c(206, 226, 209), -1)
        cv2.rectangle(sh.img, p0, p1, _c(120, 160, 128), max(1, sh.mm(0.35)))
    ht = max(tops)
    sh.dash(Q(-0.100, ht), Q(0.655, ht), _c(120, 160, 128), 0.4, 4.0, 3.0)
    sh.say(Q(0.655, ht + 0.010), f'Grundstellung {ht*1000:.0f} mm — '
           f'höher als das Objektiv', 4.4, _c(70, 118, 80), 'rb')

    tw = 0.032
    p0, p1 = Q(lens_x - tw / 2, lens_z), Q(lens_x + tw / 2, 0)
    cv2.rectangle(sh.img, p0, p1, CAM_FILL, -1)
    cv2.rectangle(sh.img, p0, p1, CAM_EDGE, max(1, sh.mm(0.6)))
    cv2.rectangle(sh.img, Q(lens_x - 0.075, 0.014), Q(lens_x + 0.075, 0),
                  CAM_FILL, -1)
    cv2.rectangle(sh.img, Q(lens_x - 0.075, 0.014), Q(lens_x + 0.075, 0),
                  CAM_EDGE, max(1, sh.mm(0.6)))
    t = math.radians(tilt)
    d = np.array([-math.cos(t), -math.sin(t)]); nrm = np.array([-d[1], d[0]])
    L = np.array([lens_x, lens_z])
    quad = [L - d * 0.012 + nrm * 0.024, L - d * 0.012 - nrm * 0.024,
            L + d * 0.030 - nrm * 0.024, L + d * 0.030 + nrm * 0.024]
    a = np.array([Q(*q) for q in quad], np.int32)
    cv2.fillPoly(sh.img, [a], _c(226, 205, 231), cv2.LINE_AA)
    cv2.polylines(sh.img, [a], True, CAM_EDGE, max(1, sh.mm(0.6)), cv2.LINE_AA)
    sh.dash(Q(lens_x, lens_z), Q(aim, 0.0), CAM_EDGE, 0.55, 5.0, 3.5)
    sh.dash(Q(lens_x, lens_z), Q(lens_x - 0.175, lens_z), INK_FAINT, 0.4, 4.0, 3.0)
    rr = sh.mm(0.095 * 1000 * sc)
    cv2.ellipse(sh.img, Q(lens_x, lens_z), (rr, rr), 0, 180 - tilt, 180,
                CAM_EDGE, max(1, sh.mm(0.6)), cv2.LINE_AA)
    ha = math.radians(tilt / 2)
    sh.say(Q(lens_x - 0.125 * math.cos(ha), lens_z - 0.125 * math.sin(ha)),
           f'{tilt:.0f}°', 7.0, CAM_EDGE, 'mm', bold=True, halo=PAPER)
    dz = Q(lens_x + 0.098, 0)[0]
    sh.line((dz, Q(0, lens_z)[1]), (dz, Q(0, 0)[1]), INK_SOFT, 0.4)
    for yy in (Q(0, lens_z)[1], Q(0, 0)[1]):
        sh.line((dz - sh.mm(2), yy), (dz + sh.mm(2), yy), INK_SOFT, 0.4)
    sh.say((dz + sh.mm(3), (Q(0, lens_z)[1] + Q(0, 0)[1]) // 2),
           f'{lens_z*1000:.0f}', 5.4, INK, 'lm', bold=True)
    dy = Q(0, -0.062)[1]
    sh.line(Q(0, 0)[0:1] + (dy,), Q(lens_x, 0)[0:1] + (dy,), INK_SOFT, 0.4)
    for xx in (Q(0, 0)[0], Q(lens_x, 0)[0]):
        sh.line((xx, dy - sh.mm(2)), (xx, dy + sh.mm(2)), INK_SOFT, 0.4)
    sh.say(((Q(0, 0)[0] + Q(lens_x, 0)[0]) // 2, dy + sh.mm(3)),
           f'{lens_x*1000:.0f}', 5.4, INK, 'mt', bold=True)
    ap = Q(aim, 0)
    cv2.circle(sh.img, ap, sh.mm(1.8), CAM_EDGE, -1, cv2.LINE_AA)
    sh.say((ap[0], ap[1] + sh.mm(3)), 'Zielpunkt', 4.2, CAM_EDGE, 'mt')
    sh.say(sh.pg(px0 + PW - 9, py0 + PH - 9), 'Maße in mm', 4.0, INK_FAINT, 'rb')


def draw_steps(sh, tilt, lens_z):
    PW, PH = 262.0, 196.0
    px0 = sh.w_mm - MARGIN_MM - 2.0 - PW
    py0 = HEADER_MM + 3.0
    sh.panel(px0, py0, PW, PH, fill=_c(252, 251, 248))
    sh.say(sh.pg(px0 + 9, py0 + 9), 'Aufbau', 6.6, INK, 'lt', bold=True)
    sh.say(sh.pg(px0 + PW - 9, py0 + 10), 'einmal pro Klassenraum', 4.4, INK_FAINT, 'rt')
    steps = [
        'Matte flach und faltenfrei auf den Tisch kleben.',
        'Roboter auf das graue Rechteck stellen — Pfeil zeigt nach vorne,\nFadenkreuz genau auf die Achse von Gelenk 1.',
        f'Turm mittig auf den violetten Kreis stellen,\nObjektiv {lens_z*1000:.0f} mm über der Tischplatte.',
        f'Kamera neigen (≈ {tilt:.0f}°), bis der Zielpunkt genau\nin der Bildmitte liegt.',
        'Turm und Roboter festschrauben oder festkleben.',
        'ChArUco-Tafel auflegen, kalibrieren, Tafel abnehmen.',
        'Prüfen: alle 6 grünen Kreuze sind im Kamerabild zu sehen.',
    ]
    y = py0 + 26.0
    for i, t in enumerate(steps):
        c = sh.pg(px0 + 14, y + 2.6)
        cv2.circle(sh.img, c, sh.mm(3.6), _c(232, 240, 234), -1, cv2.LINE_AA)
        cv2.circle(sh.img, c, sh.mm(3.6), ZONE_EDGE, max(1, sh.mm(0.4)), cv2.LINE_AA)
        sh.say((c[0], c[1] + sh.mm(0.2)), str(i + 1), 4.2, ZONE_DEEP, 'mm', bold=True)
        sh.say(sh.pg(px0 + 21, y), t, 4.9, _c(60, 64, 70), 'lt', spacing=1.6)
        y += 21.0 if '\n' in t else 14.6
    sh.line(sh.pg(px0 + 9, py0 + PH - 20), sh.pg(px0 + PW - 9, py0 + PH - 20),
            INK_FAINT, 0.35)
    sh.say(sh.pg(px0 + 9, py0 + PH - 15),
           'Wird der Turm oder der Roboter später bewegt, muss neu '
           'kalibriert werden.', 4.4, STOP_EDGE, 'lt')


def draw_legend(sh):
    PW, PH = 252.0, 92.0
    px0 = MARGIN_MM + 2.0
    py0 = sh.h_mm - MARGIN_MM - PH
    sh.panel(px0, py0, PW, PH, fill=_c(252, 251, 248))
    sh.say(sh.pg(px0 + 9, py0 + 8), 'Zeichenerklärung', 5.8, INK, 'lt', bold=True)
    items = [(ZONE_FILL, ZONE_EDGE, 'Greifzone — Arm erreicht es UND Kamera erkennt es'),
             (RING_FILL, RING_EDGE, 'Arm erreicht es, Kamera sieht den Tag zu klein'),
             (STOP_FILL, STOP_EDGE, 'Sperrbereich — der Arm faltet auf sich selbst'),
             (BOARD_FILL, BOARD_EDGE, 'ChArUco-Tafel (nur beim Kalibrieren)'),
             (CAM_FILL, CAM_EDGE, 'Kamera-Turm, Sichtlinie und Zielpunkt')]
    for i, (fill, edge, txt) in enumerate(items):
        y = py0 + 21.0 + i * 12.0
        p0, p1 = sh.pg(px0 + 10, y), sh.pg(px0 + 24, y + 7.4)
        cv2.rectangle(sh.img, p0, p1, fill, -1)
        cv2.rectangle(sh.img, p0, p1, edge, max(1, sh.mm(0.5)))
        sh.say(sh.pg(px0 + 29, y + 3.7), txt, 4.7, _c(60, 64, 70), 'lm')


def draw_scale(sh):
    PW, PH = 252.0, 92.0
    px0 = sh.w_mm - MARGIN_MM - 2.0 - PW
    py0 = sh.h_mm - MARGIN_MM - PH
    sh.panel(px0, py0, PW, PH, fill=_c(252, 251, 248))
    sh.say(sh.pg(px0 + 9, py0 + 8), 'Druck prüfen', 5.8, INK, 'lt', bold=True)
    sh.say(sh.pg(px0 + 9, py0 + 19),
           'Diese Strecke muss auf dem Ausdruck genau 100 mm sein.', 4.7,
           _c(60, 64, 70), 'lt')
    bx, by = px0 + 20.0, py0 + 46.0
    sh.line(sh.pg(bx, by), sh.pg(bx + 100, by), INK, 1.2)
    for k in range(11):
        q = sh.pg(bx + k * 10, by)
        h = 4.2 if k in (0, 5, 10) else 2.4
        sh.line((q[0], q[1] - sh.mm(h)), (q[0], q[1]), INK, 0.8 if k in (0, 5, 10) else 0.4)
    for k, lab in ((0, '0'), (5, '50'), (10, '100 mm')):
        q = sh.pg(bx + k * 10, by)
        sh.say((q[0], q[1] + sh.mm(2.5)), lab, 4.2, INK_SOFT,
               'mt' if k == 5 else ('lt' if k == 0 else 'rt'))
    sh.say(sh.pg(px0 + 9, py0 + PH - 20),
           'Stimmt sie nicht, wurde skaliert gedruckt — dann ist jeder Radius '
           'falsch.', 4.4, STOP_EDGE, 'lt')
    sh.say(sh.pg(px0 + PW - 9, py0 + PH - 9),
           'tools/generate_edu1_mat.py', 4.0, INK_FAINT, 'rb')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', type=Path, default=Path('classroom_kit/edu1_mat.pdf'))
    ap.add_argument('--dpi', type=int, default=DPI_DEFAULT)
    ap.add_argument('--hfov', type=float, default=78.0,
                    help='scene-camera HORIZONTAL field of view of the OUTPUT '
                         'image, degrees. 78 = a 4 mm M12 lens (recommended); '
                         'the stock Innomaker 2.8 mm lens is ~102. MEASURE IT.')
    ap.add_argument('--cam-width', type=int, default=1280)
    ap.add_argument('--cam-height', type=int, default=720)
    ap.add_argument('--lens-x', type=float, default=LENS_X_M)
    ap.add_argument('--lens-z', type=float, default=LENS_Z_M)
    ap.add_argument('--tilt', type=float, default=None)
    ap.add_argument('--tag-px', type=float, default=TAG_PX_MIN)
    args = ap.parse_args()

    geom = _load_geometry()
    if args.tilt is None:
        tilt, marg, aim = choose_tilt(geom, args.lens_x, args.lens_z, args.hfov,
                                      args.cam_width, args.cam_height)
        print(f'[INFO] Neigung {tilt:.1f}° (Rand {marg:.0f} px), '
              f'Zielpunkt x={aim*1000:.0f} mm')
    else:
        tilt = args.tilt
        aim = aim_point(args.lens_x, args.lens_z, tilt)
        if not (geom['aim_min_x'] <= aim <= geom['aim_max_x']):
            print(f'[WARNUNG] Zielpunkt x={aim*1000:.0f} mm liegt AUSSERHALB von '
                  f'[{geom["aim_min_x"]*1000:.0f}, {geom["aim_max_x"]*1000:.0f}] mm '
                  f'— die Extrinsik-Kalibrierung lehnt das ab.')
    zone = placement_zone(geom, args.lens_x, args.lens_z, tilt, args.hfov,
                          args.cam_width, args.cam_height, args.tag_px,
                          step=0.001)

    sh = Sheet(args.dpi)
    checks = draw_field(sh, geom, zone)
    draw_board(sh, geom)
    draw_camera(sh, geom, tilt, aim, args.lens_x, args.lens_z)
    draw_robot(sh, geom)
    draw_header(sh, geom, tilt, aim, args.hfov, args.cam_width, args.cam_height,
                args.lens_x, args.lens_z, zone, args.tag_px)
    draw_elevation(sh, geom, tilt, aim, args.lens_x, args.lens_z)
    draw_steps(sh, tilt, args.lens_z)
    draw_legend(sh)
    draw_scale(sh)
    sh.flush_text()

    G, mask, tag_px, in_ring, step = zone
    print(f'[INFO] Greifzone {mask.sum()*step*step*1e4:.0f} cm² '
          f'({100*mask.sum()/max(1,in_ring.sum()):.0f} % der Reichweite), '
          f'Tag {tag_px[mask].min():.0f}–{tag_px[mask].max():.0f} px'
          if mask.any() else '[WARNUNG] Greifzone ist LEER')
    print(f'[INFO] Prüfkreuze ' + ', '.join(f'({x*1000:.0f},{y*1000:.0f})' for x, y in checks))
    print(f'[INFO] Blatt {sh.w_mm:.0f} × {sh.h_mm:.0f} mm @ {args.dpi} dpi')

    args.out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(args.out.with_suffix('.png')), sh.img)
    try:
        from PIL import Image
        Image.fromarray(cv2.cvtColor(sh.img, cv2.COLOR_BGR2RGB)).save(
            args.out, format='PDF', resolution=args.dpi)
        print(f'[OK] {args.out}')
    except ImportError:
        print('[WARNUNG] Pillow fehlt — nur PNG.')


if __name__ == '__main__':
    main()

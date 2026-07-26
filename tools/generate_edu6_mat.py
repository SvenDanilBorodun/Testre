#!/usr/bin/env python3
"""Render the edu6_studio work mat (Arbeits-Matte) for the classroom kit.

WHY A SEPARATE MAT: the OMX mat is a full ring around the base, because the OMX
joint 1 turns +-180 deg and its reach annulus is roughly 0.04-0.26 m. edu6 is a
DIFFERENT shape and the OMX mat is actively wrong for it:

* joint 1 spans only +-90 deg, so the usable area is a HALF fan in front of the
  arm, not a ring. The arm is placed at the mat's straight edge.
* the usable radii are the profile's ``reach_inner_m`` / ``reach_outer_m``.
  The OUTER bound is where the IK stops solving a top-down grasp at table
  height; the INNER bound is a SELF-COLLISION bound, not an IK one - inside it
  the closed-form solve still succeeds but the arm folds onto itself, so the
  inner circle is a keep-out, not merely "no reach".
* the fan is centred on the JOINT-1 AXIS, which is offset from the base-link
  origin by ``base_axis_x`` (edu6: +21.3 mm). Drawing the fan about the base
  origin instead puts every radius ~21 mm out.

Every dimension below is READ FROM THE CODE (robot_profiles + the IK solver +
calibration_manager's board origin), never typed in twice - so the printed mat
cannot drift from what the solver and the calibration actually expect.

Usage:
    python tools/generate_edu6_mat.py                      # -> edu6_mat.pdf
    python tools/generate_edu6_mat.py --out mat.png --margin-mm 15
    python tools/generate_edu6_mat.py --no-board           # omit the board marker

Print at 100% scale ("Tatsaechliche Groesse", never fit-to-page) and check the
printed 100 mm ruler mark with a real ruler before use: a fit-to-page print
silently rescales every radius and the inner keep-out stops matching the arm.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import cv2
import numpy as np

DPI = 300
_PX_PER_M = DPI / 0.0254

# Colours (BGR - OpenCV).
_BLACK = (0, 0, 0)
_GREY = (150, 150, 150)
_RED = (60, 60, 220)
_GREEN = (90, 160, 60)
_BLUE = (200, 120, 60)
_ORANGE = (40, 140, 235)


def _load_geometry(profile_id: str = 'edu6_studio'):
    """Pull every dimension from the shipped code. Returns a plain dict."""
    server = Path(__file__).resolve().parents[1] / 'physical_ai_tools' / 'physical_ai_server'
    sys.path.insert(0, str(server))
    from physical_ai_server import robot_profiles
    from physical_ai_server.workflow import calibration_manager as cm

    profile = robot_profiles.resolve(profile_id)
    if profile.profile_id != profile_id:
        raise SystemExit(
            f'unknown robot profile {profile_id!r} (resolve fell back to '
            f'{profile.profile_id!r})')
    if profile.reach_inner_m is None or profile.reach_outer_m is None:
        raise SystemExit(
            f'profile {profile_id!r} carries no reach_inner_m/reach_outer_m — '
            f'this mat is only meaningful for a profile that does')
    solver = profile.build_ik()
    # joint-1 limit = the fan half-angle. Read it, never assume +-90.
    j1_lo, j1_hi = solver.joint_limits[0]
    return {
        'profile': profile,
        'solver': solver,
        'inner_m': float(profile.reach_inner_m),
        'outer_m': float(profile.reach_outer_m),
        'axis_x_m': float(solver.base_axis_x),
        'fan_lo': float(j1_lo),
        'fan_hi': float(j1_hi),
        'board_origin': (cm.BOARD_ORIGIN_X_M, cm.BOARD_ORIGIN_Y_M),
        'board_yaw_deg': cm.BOARD_YAW_DEG,
    }


def _solvable_at(geom, radius: float, z: float, samples: int) -> int:
    """How many of ``samples`` bearings solve a top-down grasp at (radius, z)."""
    from physical_ai_server.workflow.handlers import motion
    solver = geom['solver']
    ok = 0
    for k in range(samples):
        frac = k / max(1, samples - 1)
        bearing = geom['fan_lo'] + (geom['fan_hi'] - geom['fan_lo']) * frac
        x = geom['axis_x_m'] + radius * math.cos(bearing)
        y = radius * math.sin(bearing)
        if solver.solve((x, y, z),
                        roll=solver.base_yaw(x, y) + motion.GRASP_ROLL_RAD) is not None:
            ok += 1
    return ok


def resolve_drawn_rim(geom, samples: int = 24) -> tuple[float, float]:
    """The outer radius the mat should actually PRINT, and the grasp height it is
    valid at.

    ``ArmProfile.reach_outer_m`` is defined at TABLE LEVEL (measured: edu6 solves
    to 215 mm at z=0, which the profile rounds down to 0.21). But an object is
    grasped ABOVE the table - the TCP descends only to
    ``object_height_m - grasp_depth_m`` - and the strict-vertical annulus shrinks
    as z rises, so the graspable rim for a 30 mm cube is ~208 mm, not 210.

    A mat is a PLACEMENT guide, so printing the table-level number would promise
    2 mm the arm refuses for every catalog object. Print the object-aware rim
    instead, taking the most restrictive catalog grasp height, and never print
    MORE than the profile advertises."""
    from physical_ai_server.workflow.object_catalog import fixed_catalog
    catalog = fixed_catalog(geom['profile'].profile_id)
    heights = []
    for type_name in catalog.type_names():
        recipe = catalog.recipe_for_type(type_name)
        heights.append(float(recipe.object_height_m) - float(recipe.grasp_depth_m))
    z = max(heights) if heights else 0.0
    return _shrink_to_solvable(geom, z, samples), z


def _shrink_to_solvable(geom, z: float, samples: int) -> float:
    """Largest radius <= the advertised outer bound that solves at EVERY bearing
    at height ``z`` (1 mm steps)."""
    rim = geom['outer_m']
    while rim > geom['inner_m']:
        if _solvable_at(geom, rim, z, samples) == samples:
            break
        rim -= 0.001
    return rim


def resolve_drop_rim(geom, samples: int = 24) -> tuple[float, float]:
    """The radius out to which the arm can still ABLEGEN (place), and the height
    that bound is set by.

    ``motion.drop_at`` releases ``DROP_HEIGHT_M`` (0.05 m, env-tunable) ABOVE the
    surface — deliberately higher than the grasp clearance so the jaws clear a
    container rim — and it solves that raised point FIRST and raises on failure.
    Because the strict-vertical annulus shrinks with height, the place rim is
    strictly INSIDE the pick rim.

    On the OMX that difference is ~7 mm and nobody notices. On edu6 it is ~34 mm
    of a ~118 mm band (measured 2026-07-26): pick 208 mm vs place 174 mm. A mat
    that printed only the pick rim would invite a student to place a cube where
    „nimm …" works and „lege ab bei …" then refuses in German — so BOTH rims are
    drawn."""
    from physical_ai_server.workflow.handlers import motion
    z = float(motion.DROP_HEIGHT_M)
    return _shrink_to_solvable(geom, z, samples), z


def _verify_bounds(geom, drawn_rim: float, z_grasp: float,
                   samples: int = 24) -> None:
    """Report the two outer bounds and confirm the DRAWN one never over-promises.

    Only an outer bound is checkable this way - the inner one is a
    SELF-COLLISION limit the closed-form solver knows nothing about, so
    IK-solvable inside it is expected and is not a pass criterion."""
    table_ok = _solvable_at(geom, geom['outer_m'], 0.0, samples)
    print(f'[INFO] profile reach_outer_m {geom["outer_m"]:.3f} m solves at '
          f'{table_ok}/{samples} bearings at TABLE level (z=0) — that is the '
          f'height it is defined at')
    grasp_ok = _solvable_at(geom, geom['outer_m'], z_grasp, samples)
    if grasp_ok < samples:
        print(f'[INFO] ...but only {grasp_ok}/{samples} at the catalog grasp '
              f'height z={z_grasp:.3f} m, because the annulus shrinks with '
              f'height — so the mat prints {drawn_rim:.3f} m instead')
    drawn_ok = _solvable_at(geom, drawn_rim, z_grasp, samples)
    if drawn_ok == samples:
        print(f'[OK] the PRINTED rim {drawn_rim:.3f} m solves at all {samples} '
              f'bearings at z={z_grasp:.3f} m — the mat does not over-promise')
    else:
        print(f'[WARN] the printed rim {drawn_rim:.3f} m solves at only '
              f'{drawn_ok}/{samples} bearings at z={z_grasp:.3f} m')
    print(f'[INFO] inner radius {geom["inner_m"]:.3f} m is a SELF-COLLISION '
          f'keep-out (mesh-measured), not an IK bound — the solver happily '
          f'solves inside it, which is exactly why the mat marks it')


_FONT_CANDIDATES = (
    r'C:\Windows\Fonts\arial.ttf',
    r'C:\Windows\Fonts\segoeui.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
    '/System/Library/Fonts/Helvetica.ttc',
)

# Rule §1 wants literal umlauts on anything a student or teacher reads, but the
# cv2 Hershey fonts are ASCII-only and would print '?' for every one. So text is
# drawn with PIL + a TrueType face; if no face is found anywhere, this is the
# LAST-RESORT transliteration (the legacy `Schueler` spelling convention) so the
# sheet still prints legibly instead of as punctuation.
_ASCII_FALLBACK = {
    'ä': 'ae', 'ö': 'oe', 'ü': 'ue', 'Ä': 'Ae', 'Ö': 'Oe', 'Ü': 'Ue',
    'ß': 'ss', '—': '-', '°': ' Grad', '„': '"', '"': '"', '×': 'x',
}


def _ascii_safe(s: str) -> str:
    for src, dst in _ASCII_FALLBACK.items():
        s = s.replace(src, dst)
    return s.encode('ascii', 'replace').decode('ascii')


def _draw_text(img: np.ndarray, labels) -> np.ndarray:
    """Draw every collected label. PIL + TrueType when available (so ä/ö/ü/ß
    render), else cv2 with a transliteration and a loud warning."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        Image = None
    face = None
    if Image is not None:
        for path in _FONT_CANDIDATES:
            if Path(path).exists():
                face = path
                break
    if Image is None or face is None:
        print('[WARN] no TrueType font (or no Pillow) found — falling back to '
              'the ASCII transliteration for the German labels. Install Pillow '
              'or point _FONT_CANDIDATES at a .ttf for proper ä/ö/ü/ß.')
        for (pos, s, size, colour) in labels:
            scale = size / 30.0
            cv2.putText(img, _ascii_safe(s), (pos[0], pos[1] + size),
                        cv2.FONT_HERSHEY_SIMPLEX, scale, colour,
                        max(1, int(round(scale * 2))), cv2.LINE_AA)
        return img
    pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil)
    for (pos, s, size, colour) in labels:
        font = ImageFont.truetype(face, size)
        draw.text(pos, s, font=font,
                  fill=(colour[2], colour[1], colour[0]))   # BGR -> RGB
    return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)


def render(geom, margin_mm: float, show_board: bool,
           outer_override: float | None = None,
           drop_rim: float | None = None,
           drop_z: float = 0.0) -> np.ndarray:
    inner = geom['inner_m']
    outer = geom['outer_m'] if outer_override is None else float(outer_override)
    margin = margin_mm / 1000.0
    # Mat spans the full fan width and from the straight edge out to the rim.
    half_w = outer + margin
    depth = outer + margin
    w_px = int(round(2 * half_w * _PX_PER_M))
    h_px = int(round(depth * _PX_PER_M))
    img = np.full((h_px, w_px, 3), 255, dtype=np.uint8)

    # The joint-1 AXIS is the fan centre and sits ON the straight edge (bottom).
    cx = w_px / 2.0
    cy = float(h_px) - margin * _PX_PER_M

    def to_px(x_axis_rel, y):
        """base-frame -> pixels. x measured FROM the joint-1 axis, +x away from
        the straight edge (up the image), +y to the arm's LEFT (image right)."""
        return (int(round(cx + y * _PX_PER_M)),
                int(round(cy - x_axis_rel * _PX_PER_M)))

    def arc(radius, colour, thickness, dashed=False):
        pts = []
        steps = 720
        for k in range(steps + 1):
            frac = k / steps
            bearing = geom['fan_lo'] + (geom['fan_hi'] - geom['fan_lo']) * frac
            pts.append(to_px(radius * math.cos(bearing), radius * math.sin(bearing)))
        if dashed:
            for k in range(0, len(pts) - 1, 12):
                cv2.line(img, pts[k], pts[min(k + 6, len(pts) - 1)], colour,
                         thickness, cv2.LINE_AA)
        else:
            cv2.polylines(img, [np.array(pts, dtype=np.int32)], False, colour,
                          thickness, cv2.LINE_AA)

    # Faint 50 mm radius grid inside the usable band.
    r = 0.05
    while r < outer:
        if inner < r < outer:
            arc(r, _GREY, 2, dashed=True)
        r += 0.05

    # The usable band edges + the fan's two straight sides.
    arc(outer, _GREEN, 6)
    arc(inner, _RED, 6)
    # The place (ablegen) rim, strictly inside the pick rim — drop_at releases
    # DROP_HEIGHT_M above the table and the annulus shrinks with height.
    if drop_rim is not None and inner < drop_rim < outer - 1e-9:
        arc(drop_rim, _ORANGE, 5, dashed=True)
    for bearing in (geom['fan_lo'], geom['fan_hi']):
        cv2.line(img,
                 to_px(inner * math.cos(bearing), inner * math.sin(bearing)),
                 to_px(outer * math.cos(bearing), outer * math.sin(bearing)),
                 _GREEN, 4, cv2.LINE_AA)

    # Inner keep-out hatching, so it never reads as "just outside the drawing".
    step = int(round(0.008 * _PX_PER_M))
    for k in range(-h_px, w_px, max(step, 4)):
        p0, p1 = (k, 0), (k + h_px, h_px)
        mask_pts = []
        for t in np.linspace(0.0, 1.0, 240):
            px = int(p0[0] + (p1[0] - p0[0]) * t)
            py = int(p0[1] + (p1[1] - p0[1]) * t)
            dx = (cy - py) / _PX_PER_M
            dy = (px - cx) / _PX_PER_M
            rad = math.hypot(dx, dy)
            bearing = math.atan2(dy, dx)
            if rad <= inner and geom['fan_lo'] <= bearing <= geom['fan_hi']:
                mask_pts.append((px, py))
        if len(mask_pts) > 1:
            cv2.line(img, mask_pts[0], mask_pts[-1], (215, 215, 245), 2,
                     cv2.LINE_AA)

    # Arm base marker: the joint-1 axis dot + the straight edge it sits on.
    cv2.line(img, (0, int(round(cy))), (w_px, int(round(cy))), _BLACK, 3,
             cv2.LINE_AA)
    cv2.circle(img, to_px(0.0, 0.0), 12, _BLACK, -1, cv2.LINE_AA)

    # Text is collected and drawn LAST, through PIL, because this sheet is
    # student/teacher-facing and Rule §1 wants literal ä/ö/ü/ß — which
    # cv2.putText CANNOT render (the Hershey fonts are ASCII-only and emit '?').
    # _draw_text falls back to cv2 with an automatic transliteration when no
    # TrueType font is available, so the strings live here exactly once.
    labels: list[tuple[tuple[int, int], str, int, tuple[int, int, int]]] = [
        ((int(cx) + 20, int(cy) - 30), 'Arm-Drehachse (Gelenk 1)', 26, _BLACK),
        ((20, 20), 'EduBotics 6-Achs — Arbeitsbereich', 40, _BLACK),
        ((20, 72),
         f'grün = GREIFEN bis r={outer * 100:.1f} cm   |   '
         f'rot = Sperrbereich r < {inner * 100:.1f} cm', 26, _BLACK),
        ((20, 108),
         (f'orange (gestrichelt) = ABLEGEN nur bis r={drop_rim * 100:.1f} cm — '
          f'der Greifer öffnet {int(round(drop_z * 1000))} mm über dem Tisch'
          if drop_rim is not None and drop_rim < outer - 1e-9 else
          'Greifen und Ablegen reichen hier gleich weit.'), 26, _BLACK),
        ((20, 144),
         f'Fächer {math.degrees(geom["fan_hi"] - geom["fan_lo"]):.0f}° '
         f'(Gelenk 1) — den Arm an die gerade Kante stellen.', 26, _BLACK),
        ((20, h_px - 108),
         'Objekte nur ZWISCHEN rot und grün platzieren; Ziele zum Ablegen '
         'innerhalb von orange.', 26, _BLACK),
        ((20, h_px - 68),
         'Im 100%-Maßstab drucken, dann die 100-mm-Marke nachmessen!', 26, _RED),
    ]

    # Printed ruler check-mark: an unambiguous 100 mm reference.
    y0 = int(round(cy)) - int(round(0.004 * _PX_PER_M))
    x0 = 40
    x1 = x0 + int(round(0.100 * _PX_PER_M))
    cv2.line(img, (x0, y0), (x1, y0), _BLACK, 3, cv2.LINE_AA)
    for xx in (x0, x1):
        cv2.line(img, (xx, y0 - 14), (xx, y0 + 14), _BLACK, 3, cv2.LINE_AA)
    labels.append(((x0 + 12, y0 - 46), '100 mm', 24, _BLACK))

    # Radius tick labels along the centre line (cm from the joint-1 axis).
    r = 0.05
    while r <= outer + 1e-9:
        if r >= inner:
            px, py = to_px(r, 0.0)
            cv2.line(img, (px - 10, py), (px + 10, py), _BLACK, 2, cv2.LINE_AA)
            labels.append(((px + 16, py - 12), f'{r * 100:.0f} cm', 22, _BLACK))
        r += 0.05

    if show_board:
        bx, by = geom['board_origin']
        # The board's OpenCV origin corner, in base coords, drawn relative to the
        # joint-1 axis. It may fall OUTSIDE this mat - that is information, not a
        # bug, so it is reported rather than clipped silently.
        px, py = to_px(bx - geom['axis_x_m'], by)
        if 0 <= px < w_px and 0 <= py < h_px:
            cv2.drawMarker(img, (px, py), _BLUE, cv2.MARKER_TILTED_CROSS, 40, 4)
            labels.append(((px + 24, py - 14), 'ChArUco-Ecke', 24, _BLUE))
        else:
            print(f'[INFO] the ChArUco origin corner ({bx}, {by}) lies OUTSIDE '
                  f'this mat (the mat only spans the arm\'s reach, r<='
                  f'{outer + 0.0:.3f} m from the axis at x={geom["axis_x_m"]:.4f}). '
                  f'The board is a CAMERA reference, not a grasp target, so it '
                  f'belongs beyond the rim — place it per the L-jig. Tune with '
                  f'EDUBOTICS_BOARD_ORIGIN_X_M/_Y_M if the scene camera cannot '
                  f'frame both.')
    return _draw_text(img, labels)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', default='edu6_mat.pdf',
                    help='output .pdf (vector-wrapped raster) or .png')
    ap.add_argument('--profile', default='edu6_studio',
                    help='robot profile id to read the geometry from')
    ap.add_argument('--margin-mm', type=float, default=10.0,
                    help='white margin beyond the outer rim (mm)')
    ap.add_argument('--no-board', action='store_true',
                    help='omit the ChArUco origin-corner marker')
    args = ap.parse_args()

    geom = _load_geometry(args.profile)
    print(f'profile      : {geom["profile"].profile_id} '
          f'({geom["profile"].display_name_de})')
    print(f'joint-1 axis : x = {geom["axis_x_m"] * 1000:.1f} mm '
          f'(the fan centre — NOT the base-link origin)')
    print(f'fan          : {math.degrees(geom["fan_lo"]):.0f}..'
          f'{math.degrees(geom["fan_hi"]):.0f} deg')
    drawn_rim, z_grasp = resolve_drawn_rim(geom)
    drop_rim, drop_z = resolve_drop_rim(geom)
    print(f'GREIFEN band : r {geom["inner_m"] * 100:.1f} .. '
          f'{drawn_rim * 100:.1f} cm  (profile advertises '
          f'{geom["outer_m"] * 100:.1f} cm at table level)')
    print(f'ABLEGEN rim  : r {drop_rim * 100:.1f} cm  (drop_at releases '
          f'{drop_z * 1000:.0f} mm above the surface, and the annulus shrinks '
          f'with height)')
    if drop_rim < drawn_rim - 1e-9:
        print(f'               -> {(drawn_rim - drop_rim) * 1000:.0f} mm of the '
              f'band is pick-yes / place-no; both rims are printed so a student '
              f'cannot place a cube where „lege ab" will refuse')
    _verify_bounds(geom, drawn_rim, z_grasp)

    img = render(geom, args.margin_mm, not args.no_board,
                 outer_override=drawn_rim, drop_rim=drop_rim, drop_z=drop_z)
    out = Path(args.out)
    h, w = img.shape[:2]
    print(f'mat size     : {w / _PX_PER_M * 100:.1f} x {h / _PX_PER_M * 100:.1f} cm '
          f'({w} x {h} px @ {DPI} dpi)')
    if out.suffix.lower() == '.pdf':
        try:
            from PIL import Image
        except ImportError:
            raise SystemExit('PDF output needs Pillow (pip install Pillow); '
                             'use --out mat.png instead')
        Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB)).save(
            str(out), format='PDF', resolution=DPI)
    else:
        cv2.imwrite(str(out), img)
    print(f'Wrote {out}')


if __name__ == '__main__':
    main()

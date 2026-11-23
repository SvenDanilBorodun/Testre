#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""Render the tower kit's tag labels from the fixture contract (spec 40 WP-03, 41 H3/H5).

Two kinds of A4 sheet, each plotted at 100 % like the mats (decision D26):

``--arm {omx,v1}``  the clip-on calibration target's label: the plate's
    ``TARGET['plate_m']`` square (80 mm) with 3 x 3 tag36h11 tags of
    ``TARGET['tag_size_m']`` at ``TARGET['pitch_m']``, tag (i, j) centred at
    ((i-1)·pitch, (j-1)·pitch) of the target frame with the id
    ``target_tag_ids[3·i + j]`` (``scene_fixture.target_tag_corners``), every tag
    upright with its top toward +x of the target frame. A short bar in the label's
    outer margin marks the edge that faces the clamp: the edge toward the tool
    axis, worked out from ``target_mount_ee``.
``--fixture-tags``  the tower foot's two fixture tags 480 and 481
    (``FIXTURE_TAGS_F``, black square ``FIXTURE_TAG_SIZE_M``), each on a label the
    size of its 35 x 35 mm pocket (41 H3), top toward +x_F (forward, toward the
    mat), the same bar marking the top edge.

Each sheet names the contract id and the first 8 hex of ``contract_hash()`` and
carries two 100 mm ruler marks. The drawing, the raster and the PDF writer are
``generate_scene_mat``'s.

Usage:
    python3 tools/generate_calibration_target.py --arm v1     # -> classroom_kit/calibration_target_v1.pdf
    python3 tools/generate_calibration_target.py --fixture-tags   # -> classroom_kit/fixture_tags.pdf
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

_TOOLS = Path(__file__).resolve().parent
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))
import generate_scene_mat as kit  # noqa: E402
from generate_scene_mat import sf  # noqa: E402

PAGE_MM = (210.0, 297.0)             # A4 portrait
POCKET_MM = 35.0                     # the fixture-tag pockets in the tower foot (41 H3, CAD v3 F8)
_MARK_MM = (6.0, 1.0)                # the edge bar: length along the edge, depth into the label
_CROP_MM = (2.0, 6.0)                # crop marks: gap to the label corner, length


@dataclass
class Label:
    """One square label drawn in a frame on the page (its centre the frame's origin)."""
    frame: kit.Frame
    size_mm: float
    tags: tuple                      # ((id, cx, cy, size_mm), …) in the frame
    mark: tuple                      # the edge bar as a frame rectangle (x0, y0, x1, y1)

    def frame_of_page(self, u, v):
        return self.frame.frame_of_page(u, v)

    def frame_direction_of_page(self, du, dv):
        return self.frame.frame_direction_of_page(du, dv)

    def page_box(self, grow=0.0):
        h = self.size_mm / 2.0 + grow
        return self.frame.page_box(-h, -h, h, h)


@dataclass
class Sheet:
    """Everything on one A4 sheet: the labels (by name: ``'target'`` or the tag id), the
    texts (page mm) and the rulers (page frame)."""
    title: str
    labels: dict
    texts: list = field(default_factory=list)
    rulers: list = field(default_factory=list)

    @property
    def label(self):
        (only,) = self.labels.values()
        return only


PAGE_FRAME = kit.Frame(0.0, PAGE_MM[1], 'right')   # x = u, y = up from the page's bottom edge


def _hash8():
    return sf.contract_hash()[:8]


def clamp_edge(spec):
    """``(axis, sign)`` of the target-frame edge that faces the tool axis (the clamp side):
    the larger in-plane component of the end-effector origin seen from the target frame."""
    m = spec['target_mount_ee']
    rot = [[m[r][c] for c in range(3)] for r in range(3)]
    t = [m[r][3] for r in range(3)]
    # origin of the EE frame in the target frame: -R^T t
    local = [-sum(rot[r][c] * t[r] for r in range(3)) for c in range(3)]
    axis = 0 if abs(local[0]) >= abs(local[1]) else 1
    return ('x', 'y')[axis], 1 if local[axis] > 0 else -1


def _edge_bar(size_mm, axis, sign):
    h = size_mm / 2.0
    along, depth = _MARK_MM[0] / 2.0, _MARK_MM[1]
    if axis == 'x':
        return (h - depth, -along, h, along) if sign > 0 else (-h, -along, -h + depth, along)
    return (-along, h - depth, along, h) if sign > 0 else (-along, -h, along, -h + depth)


def _lines(texts, u, v, rows):
    """Left-aligned rows ``(size, bold, text)``, the first one's top at page ``v``, going down."""
    for size, bold, text in rows:
        v += kit._ASCENT * size
        texts.append(kit.PageText(u, v, size, text, bold))
        v += kit._DESCENT * size + 1.8


def _rulers_and_labels(sheet, x_ruler_v, y_ruler_u, y_ruler_v0):
    """A 100 mm x ruler (ticks up, labelled below) and a 100 mm y ruler (ticks left,
    labelled under its lower end); page millimetres."""
    x_ruler = kit.Ruler('x', (PAGE_MM[0] / 2.0 - 50.0, PAGE_MM[1] - x_ruler_v), +1)
    y_ruler = kit.Ruler('y', (y_ruler_u, PAGE_MM[1] - y_ruler_v0 - 100.0), -1)
    sheet.rulers += [x_ruler, y_ruler]
    label = '100 mm'
    w = kit.text_width_mm(label, 3.5)
    sheet.texts.append(kit.PageText(PAGE_MM[0] / 2.0 - w / 2.0, x_ruler_v + 4.5, 3.5, label))
    sheet.texts.append(kit.PageText(y_ruler_u - w, y_ruler_v0 + 100.0 + 5.5, 3.5, label))


def target_layout(arm):
    spec = sf.ARMS[arm]
    size = sf.TARGET['plate_m'] * 1000.0
    tag_mm = sf.TARGET['tag_size_m'] * 1000.0
    tags = []
    for tid, corners in sf.target_tag_corners(spec):
        cx = sum(c[0] for c in corners) / 4.0 * 1000.0
        cy = sum(c[1] for c in corners) / 4.0 * 1000.0
        tags.append((tid, cx, cy, tag_mm))
    frame = kit.Frame(PAGE_MM[0] / 2.0, 118.0, 'up')       # tags upright on the page
    label = Label(frame, size, tuple(tags), _edge_bar(size, *clamp_edge(spec)))
    ids = spec['target_tag_ids']
    sheet = Sheet(f'EduBotics Kalibrier-Ziel {kit.ARM_NAMES[arm]} ({sf.FIXTURE_ID})', {'target': label})
    _lines(sheet.texts, 15.0, 12.0, (
        (7.0, True, f'Kalibrier-Ziel {kit.ARM_NAMES[arm]} – Etikett'),
        (4.2, False, f'Kit-Vertrag {sf.FIXTURE_ID} · Prüfsumme {_hash8()}'),
        (4.2, False, f'{sf.TAG_FAMILY}: Tags {ids[0]}–{ids[-1]}, {tag_mm:g} mm, Raster '
                     f'{sf.TARGET["pitch_m"] * 1000.0:g} mm, Etikett {size:g} × {size:g} mm'),
    ))
    # the clamp side, named beside the edge it marks
    (u0, v0), (u1, v1) = frame.page_of(*label.mark[:2]), frame.page_of(*label.mark[2:])
    mid_u, mid_v = (u0 + u1) / 2.0, (v0 + v1) / 2.0
    away = size / 2.0 + sum(_CROP_MM) + 2.0            # clear of the label and its crop marks
    note = 'Klammer-Seite'
    width = kit.text_width_mm(note, 3.8, True)
    if abs(mid_u - frame.origin[0]) > abs(mid_v - frame.origin[1]):      # bar on the left or right edge
        u = frame.origin[0] + away if mid_u > frame.origin[0] else frame.origin[0] - away - width
        sheet.texts.append(kit.PageText(u, mid_v + 1.4, 3.8, note, True))
    else:                                                                # on the top or bottom edge
        v = frame.origin[1] - away if mid_v < frame.origin[1] else frame.origin[1] + away + 3.6
        sheet.texts.append(kit.PageText(mid_u - width / 2.0, v, 3.8, note, True))
    _lines(sheet.texts, 15.0, 172.0, (
        (4.0, True, 'So wird das Etikett gemacht'),
        (3.6, False, f'1. Entlang der Linie auf {size:g} × {size:g} mm zuschneiden (mattes Etikett, nicht glänzend).'),
        (3.6, False, '2. Die Kante mit dem kurzen Balken zeigt zur Klammer am Greifer.'),
        (3.6, False, '3. Flach und blasenfrei auf die Platte kleben; keinen Tag verdecken oder knicken.'),
        (3.6, False, '4. Im Maßstab 100 % drucken und beide 100-mm-Marken mit dem Lineal nachmessen.'),
    ))
    _rulers_and_labels(sheet, x_ruler_v=270.0, y_ruler_u=195.0, y_ruler_v0=68.0)
    return sheet


def fixture_layout():
    size = POCKET_MM
    tag_mm = sf.FIXTURE_TAG_SIZE_M * 1000.0
    column = next(c for name, c, _r, _h in sf.TOWER_OBB_F if name == 'column')
    near = min(sf.FIXTURE_TAGS_F, key=lambda t: math.dist(sf.FIXTURE_TAGS_F[t], column[:2]))
    sheet = Sheet(f'EduBotics Turm-Marker ({sf.FIXTURE_ID})', {})
    _lines(sheet.texts, 15.0, 12.0, (
        (7.0, True, 'Turm-Marker ' + ' und '.join(str(t) for t in sorted(sf.FIXTURE_TAGS_F))),
        (4.2, False, f'Kit-Vertrag {sf.FIXTURE_ID} · Prüfsumme {_hash8()}'),
        (4.2, False, f'{sf.TAG_FAMILY}, {tag_mm:g} mm, Etikett {size:g} × {size:g} mm (Maß der Tasche im Turm-Fuß)'),
    ))
    for k, tid in enumerate(sorted(sf.FIXTURE_TAGS_F)):
        frame = kit.Frame(60.0 + 90.0 * k, 105.0, 'up')              # x up = +x_F: forward
        label = Label(frame, size, ((tid, 0.0, 0.0, tag_mm),), _edge_bar(size, 'x', +1))
        sheet.labels[tid] = label
        x, y = (v * 1000.0 for v in sf.FIXTURE_TAGS_F[tid])
        where = 'die Tasche näher am Turm-Profil' if tid == near else 'die Tasche weiter vom Turm-Profil'
        _lines(sheet.texts, frame.origin[0] - 25.0, frame.origin[1] + size / 2.0 + sum(_CROP_MM) + 3.0, (
            (4.2, True, f'Marker {tid}'),
            (3.4, False, where),
            (3.4, False, f'(Mitte bei x = {_signed(x)} mm, y = {_signed(y)} mm)'),
        ))
    _lines(sheet.texts, 15.0, 172.0, (
        (4.0, True, 'So werden die Marker eingeklebt'),
        (3.6, False, f'1. Entlang der Linie ausschneiden ({size:g} × {size:g} mm, das Maß der Tasche).'),
        (3.6, False, '2. Der kurze Balken zeigt nach vorn: in die Blickrichtung des Roboters, zur Matte.'),
        (3.6, False, '3. Flach in die passende Tasche kleben; nichts darüberlegen.'),
        (3.6, False, '4. Im Maßstab 100 % drucken und beide 100-mm-Marken mit dem Lineal nachmessen.'),
    ))
    _rulers_and_labels(sheet, x_ruler_v=270.0, y_ruler_u=195.0, y_ruler_v0=140.0)
    return sheet


def _signed(value):
    """A number as printed: an en dash for the minus (WinAnsi has no minus sign)."""
    return f'{value:g}'.replace('-', '–')


def render_sheet(sheet, dpi=kit.DPI_DEFAULT):
    raster = kit.Raster(dpi, 0.0, 0.0, *PAGE_MM)
    raster.frame = PAGE_FRAME
    for label in sheet.labels.values():
        h = label.size_mm / 2.0
        kit.draw_outline(raster, label.frame, -h, -h, h, h, kit._LINE_MM, kit.LINE)
        gap, length = _CROP_MM
        w = kit._LINE_MM / 2.0
        for sx in (-1, 1):
            for sy in (-1, 1):
                x, y = sx * h, sy * h
                raster.frame_rect(label.frame, *_sorted_box(x + sx * gap, y - w, x + sx * (gap + length), y + w), kit.LINE)
                raster.frame_rect(label.frame, *_sorted_box(x - w, y + sy * gap, x + w, y + sy * (gap + length)), kit.LINE)
        raster.frame_rect(label.frame, *label.mark, kit.INK)
        for tid, cx, cy, size in label.tags:
            kit.draw_tag(raster, label.frame, tid, cx, cy, size)
    for ruler in sheet.rulers:
        ruler.draw(raster, PAGE_FRAME)
    return raster


def _sorted_box(x0, y0, x1, y1):
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def page_conflicts(sheet):
    """Every text or ruler that leaves the page, touches a label (with its crop marks)
    or another text or ruler. ``[]`` = none."""
    out = []
    margin = 3.0
    reach = _CROP_MM[0] + _CROP_MM[1] + 1.0
    labels = [label.page_box(grow=reach) for label in sheet.labels.values()]
    items = [(f'text „{t.text}“', t.box()) for t in sheet.texts]
    items += [(f'ruler {r.axis}', PAGE_FRAME.page_box(*r.box())) for r in sheet.rulers]
    for name, box in items:
        if box[0] < margin or box[1] < margin or box[2] > PAGE_MM[0] - margin or box[3] > PAGE_MM[1] - margin:
            out.append(f'{name} leaves the page')
        for k, lbox in enumerate(labels):
            if kit._box_hits_box(box, lbox):
                out.append(f'{name} touches label {k}')
    for i, (name_a, box_a) in enumerate(items):
        for name_b, box_b in items[i + 1:]:
            if kit._box_hits_box(box_a, box_b):
                out.append(f'{name_a} overlaps {name_b}')
    return out


def default_out(arm=None):
    return kit.KIT_DIR / (f'calibration_target_{arm}.pdf' if arm else 'fixture_tags.pdf')


def write_sheet(sheet, out, dpi=kit.DPI_DEFAULT):
    problems = page_conflicts(sheet)
    if problems:
        raise RuntimeError('sheet layout: ' + '; '.join(problems))
    return kit.write_pdf(out, PAGE_MM[0], PAGE_MM[1], render_sheet(sheet, dpi), sheet.texts, sheet.title)


def build_parser():
    parser = argparse.ArgumentParser(
        description='Render the tower kit tag labels (clip-on target or fixture tags) as an A4 PDF.')
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument('--arm', choices=kit.ARMS, help='the clip-on target label of this arm')
    what.add_argument('--fixture-tags', action='store_true', help='the tower foot fixture tags 480 and 481')
    parser.add_argument('--dpi', type=int, default=kit.DPI_DEFAULT,
                        help=f'raster resolution of the graphics (default {kit.DPI_DEFAULT})')
    parser.add_argument('--out', type=Path, default=None,
                        help='output PDF (default classroom_kit/calibration_target_<arm>.pdf '
                             'or classroom_kit/fixture_tags.pdf)')
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not kit.DPI_RANGE[0] <= args.dpi <= kit.DPI_RANGE[1]:
        parser.error(f'--dpi must lie in {kit.DPI_RANGE[0]}..{kit.DPI_RANGE[1]}')
    sheet = fixture_layout() if args.fixture_tags else target_layout(args.arm)
    out = write_sheet(sheet, args.out or default_out(None if args.fixture_tags else args.arm), args.dpi)
    print(f'Wrote {out} (A4, {args.dpi} dpi, contract {sf.FIXTURE_ID} {_hash8()})')
    return 0


if __name__ == '__main__':
    sys.exit(main())

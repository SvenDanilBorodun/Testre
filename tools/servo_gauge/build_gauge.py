#!/usr/bin/env python3
"""Build the V1 servo gauge: the cradle and the two stepped floating key caps.

    $CADPY tools/servo_gauge/build_gauge.py --out DIR [--self-check]

``$CADPY`` is a Python with ``cadquery-ocp==8.0.1.1.0`` (module ``OCP``) and
numpy; both are imported only inside functions, so CI compiles this file
without them. Writes ``cradle.step/.stl``, ``cap_J15.step/.stl``,
``cap_GR.step/.stl`` and ``gauge_manifest.json`` (every parameter, the sha256 of
each STL, the servo spec digest, the commit). The outputs are not
byte-reproducible (STEP headers carry a timestamp) and are never committed;
the default location ``tools/servo_gauge/out/`` is gitignored.

The cradle holds one STS3215/STS3250 case on three bottom pads, two side pads
and one end pad, pressed onto them by two flexures; a steel dowel at r 42 on the
w = 0 line locks the cap's rotation. A cap rests on the horn top, two pins in the
opposite pair of horn holes locate it, and its stepped marking arm guides ONE
paint stroke over the reference hole, the horn rim and the case strip.

``--self-check`` (exit 1 on any failure, nothing written): (1) every solid valid;
(2) reach — a Ø0.7 probe along the slot meets the horn top and the case strip
with no cap material in the way; (3) guide — the land beside the throat sits
<= 0.30 above the case at the design seat; (4) no interference with the case top
under the arm footprints at every checked seat; (5) pins on the hole circle, the
reference hole free; (6) the dowel slot radial on w = 0; (7) the cradle: pad
apexes on their planes, no shared volume with the case envelope, the bumps reach
their apexes, the cable line free, the P0 arrow present; (8) every STL
watertight. Frame and every number: ``gauge_params.py``.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import os
import struct
import subprocess
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, '..', '..'))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import gauge_params as gp  # noqa: E402

def _ocp():
    """Every OCP name this script uses, imported here and only here."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon,
                                    BRepBuilderAPI_Transform)
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism
    from OCP.BRepTools import BRepTools
    from OCP.GProp import GProp_GProps
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.Message import Message, Message_Gravity
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.StlAPI import StlAPI_Writer
    from OCP.TopAbs import TopAbs_IN, TopAbs_ON, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
    return types.SimpleNamespace(**{k: v for k, v in locals().items()})


def _quiet(o):
    """Keep OCP's STEP translator statistics off the console (failures still print)."""
    printers = o.Message.DefaultMessenger_s().Printers()
    for i in range(1, printers.Length() + 1):
        printers.Value(i).SetTraceLevel(o.Message_Gravity.Message_Fail)


class _Geo:
    """Small solid-modelling helpers over OCP (frame: x = r, y = w, z = n)."""

    def __init__(self, o):
        self.o = o

    def box(self, x0, y0, z0, x1, y1, z1):
        o = self.o
        return o.BRepPrimAPI_MakeBox(o.gp_Pnt(min(x0, x1), min(y0, y1), min(z0, z1)),
                                     o.gp_Pnt(max(x0, x1), max(y0, y1), max(z0, z1))).Shape()

    def cyl(self, point, direction, d, h):
        o = self.o
        return o.BRepPrimAPI_MakeCylinder(o.gp_Ax2(o.gp_Pnt(*point), o.gp_Dir(*direction)), d / 2.0, h).Shape()

    def cyl_z(self, x, y, z0, d, h):
        return self.cyl((x, y, z0), (0.0, 0.0, 1.0), d, h)

    def prism(self, points, vector):
        o = self.o
        poly = o.BRepBuilderAPI_MakePolygon()
        for p in points:
            poly.Add(o.gp_Pnt(*p))
        poly.Close()
        face = o.BRepBuilderAPI_MakeFace(poly.Wire(), True).Face()
        return o.BRepPrimAPI_MakePrism(face, o.gp_Vec(*vector)).Shape()

    def rotz(self, shape, deg):
        o = self.o
        t = o.gp_Trsf()
        t.SetRotation(o.gp_Ax1(o.gp_Pnt(0, 0, 0), o.gp_Dir(0, 0, 1)), math.radians(deg))
        return o.BRepBuilderAPI_Transform(shape, t, True).Shape()

    def fuse(self, a, b):
        return self.o.BRepAlgoAPI_Fuse(a, b).Shape()

    def cut(self, a, b):
        return self.o.BRepAlgoAPI_Cut(a, b).Shape()

    def common_volume(self, a, b):
        return self.volume(self.o.BRepAlgoAPI_Common(a, b).Shape())

    def volume(self, shape):
        g = self.o.GProp_GProps()
        self.o.BRepGProp.VolumeProperties_s(shape, g)
        return g.Mass()

    def inside(self, shape, p):
        o = self.o
        state = o.BRepClass3d_SolidClassifier(shape, o.gp_Pnt(*p), 1e-6).State()
        return state in (o.TopAbs_IN, o.TopAbs_ON)

    def valid(self, shape):
        return self.o.BRepCheck_Analyzer(shape).IsValid()

    def solids(self, shape):
        e = self.o.TopExp_Explorer(shape, self.o.TopAbs_SOLID)
        k = 0
        while e.More():
            k += 1
            e.Next()
        return k


# ── the caps ──

def _vgroove(g, x0, x1, z_land_top, z_top):
    """A slot along +x: the throat from below up to ``z_land_top``, then flanks at
    ``V_HALF_ANGLE_DEG`` opening to the top."""
    half = gp.THROAT_W / 2.0
    throat = g.box(x0, -half, -50.0, x1, half, z_land_top + 1e-3)
    half_top = half + (z_top - z_land_top) * math.tan(math.radians(gp.V_HALF_ANGLE_DEG)) + 0.5
    v = g.prism([(x0, -half, z_land_top), (x0, half, z_land_top), (x0, half_top, z_top + 1.0),
                 (x0, -half_top, z_top + 1.0)], (x1 - x0, 0.0, 0.0))
    return g.fuse(throat, v)


def build_cap(g, cap, seat):
    """The cap ``cap`` resting on a horn top at ``seat``. Its underside steps are
    designed for ``CAP_DESIGN_SEAT_N``; on a higher seat the whole cap rides up."""
    dz = seat - gp.CAP_DESIGN_SEAT_N
    top = seat + gp.CAP_THICKNESS
    ref = gp.MARK_ARM_THETA_DEG[cap]
    slot0, slot1 = gp.SLOT_R[cap]
    steps = gp.MARK_STEPS[cap]
    body = g.cyl_z(0.0, 0.0, seat, 2.0 * gp.DISC_R, gp.CAP_THICKNESS)
    half_d = gp.DOWEL_ARM_WIDTH / 2.0
    body = g.fuse(body, g.rotz(g.box(gp.DISC_R - 1.0, -half_d, gp.DOWEL_ARM_UNDERSIDE_N + dz,
                                     gp.DOWEL_ARM_END_R, half_d, top), gp.DOWEL_ARM_THETA_DEG))
    half_m = gp.MARK_ARM_WIDTH / 2.0
    arm = g.box(gp.DISC_R - 1.0, -half_m, seat, slot1 + 1.5, half_m, top)
    for a, b, underside in steps:
        arm = g.fuse(arm, g.box(a, -half_m, underside + dz, b, half_m, top))
    body = g.fuse(body, g.rotz(arm, ref))
    for th in gp.PIN_THETA_DEG[cap]:
        x, y = gp.PIN_PCD_R * math.cos(math.radians(th)), gp.PIN_PCD_R * math.sin(math.radians(th))
        body = g.fuse(body, g.cyl_z(x, y, seat - gp.PIN_LEN, gp.PIN_D, gp.PIN_LEN + 0.01))
    body = g.cut(body, g.cyl_z(0.0, 0.0, seat - 1.0, gp.CAP_BORE_D, gp.CAP_THICKNESS + 2.0))
    segments = [(slot0, gp.STEP_START_R, seat)]
    segments += [(max(a, slot0), min(b, slot1), u + dz) for a, b, u in steps if a < slot1]
    groove = None
    for a, b, underside in segments:
        piece = _vgroove(g, a, b, underside + gp.LAND_H, top)
        groove = piece if groove is None else g.fuse(groove, piece)
    body = g.cut(body, g.rotz(groove, ref))
    half_s = gp.DOWEL_SLOT_W / 2.0
    body = g.cut(body, g.rotz(g.box(gp.DOWEL_SLOT_R[0], -half_s, -50.0, gp.DOWEL_SLOT_R[1] + 1.0, half_s, 50.0),
                              gp.DOWEL_ARM_THETA_DEG))
    return body


def _case_top(table, r):
    for a, b, h in table:
        if a <= r < b:
            return h
    return None


def check_cap(g, cap, seat, body):
    """(1)–(6) for one cap at one seat. Returns ``(failures, report)``."""
    fails = []
    ref = gp.MARK_ARM_THETA_DEG[cap]
    c, s = math.cos(math.radians(ref)), math.sin(math.radians(ref))

    def along(theta_c, theta_s, r, w, z):
        return (r * theta_c - w * theta_s, r * theta_s + w * theta_c, z)

    if not g.valid(body):
        fails.append('(1) invalid solid')
    slot0, slot1 = gp.SLOT_R[cap]
    strip0, strip1 = gp.CASE_STRIP_R[cap]
    reached = {'horn': 0, 'case': 0}
    worst_gap = 0.0
    k = 0
    while True:
        r = slot0 + gp.REACH_END_MARGIN + k * gp.REACH_STEP
        k += 1
        if r > slot1 - gp.REACH_END_MARGIN + 1e-9:
            break
        if r <= gp.DISC_R - 0.1:
            target, kind = seat, 'horn'
        elif strip0 - 1e-9 <= r <= strip1 + 1e-9 and _case_top(gp.CASE_TOP[cap], r) is not None:
            target, kind = _case_top(gp.CASE_TOP[cap], r), 'case'
        else:
            continue
        probe = g.cyl(along(c, s, r, 0.0, target + 0.02), (0.0, 0.0, 1.0), gp.REACH_PROBE_D, 30.0)
        if g.common_volume(probe, body) > 1e-6:
            fails.append(f'(2) probe blocked at r {r:.2f}')
        else:
            reached[kind] += 1
        if kind == 'case':
            for w in (gp.GUIDE_W, -gp.GUIDE_W):
                j = 0
                while j < 600 and not g.inside(body, along(c, s, r, w, target + j * 0.01)):
                    j += 1
                worst_gap = max(worst_gap, j * 0.01)
    want_horn, want_case = gp.EXPECTED_REACH_COUNTS[cap]
    if reached['horn'] < want_horn or reached['case'] < want_case:
        fails.append(f"(2) reach horn ×{reached['horn']} / case ×{reached['case']}, "
                     f'expected ×{want_horn} / ×{want_case}')
    at_design = abs(seat - gp.CAP_DESIGN_SEAT_N) < 1e-9
    if at_design and worst_gap > gp.LAND_GAP + 1e-6:
        fails.append(f'(3) land {worst_gap:.2f} mm above the case (> {gp.LAND_GAP})')
    r0, r1, step = gp.INTERFERENCE_R
    directions = [(gp.MARK_ARM_THETA_DEG[cap], gp.CASE_TOP[cap])]
    if abs(gp.DOWEL_ARM_THETA_DEG - ref) > 1e-9:
        directions.append((gp.DOWEL_ARM_THETA_DEG, gp.CASE_TOP['J15']))   # the case's long axis
    interference = 0
    k = 0
    while True:
        r = r0 + k * step
        k += 1
        if r > r1 - 1e-9:
            break
        for theta, table in directions:
            h = _case_top(table, r)
            if h is None:
                continue
            tc, ts = math.cos(math.radians(theta)), math.sin(math.radians(theta))
            for w in gp.INTERFERENCE_W:
                if g.inside(body, along(tc, ts, r, w, h + gp.INTERFERENCE_CLEARANCE)):
                    interference += 1
                    if interference <= 3:
                        fails.append(f'(4) cap material {gp.INTERFERENCE_CLEARANCE} above the case at '
                                     f'θ {theta:g} r {r:.2f} w {w:g}')
    offset = gp.HOLE_THETA_OFFSET_DEG[cap]
    for th in gp.PIN_THETA_DEG[cap]:
        k_ = (th - offset) / 90.0
        if abs(k_ - round(k_)) > 1e-9:
            fails.append(f'(5) pin θ {th} not on the hole set')
        x, y = gp.PIN_PCD_R * math.cos(math.radians(th)), gp.PIN_PCD_R * math.sin(math.radians(th))
        if not g.inside(body, (x, y, seat - 1.0)):
            fails.append(f'(5) pin θ {th} missing')
    hole = g.cyl_z(gp.PIN_PCD_R * c, gp.PIN_PCD_R * s, seat - gp.PIN_LEN - 1.0, gp.HORN_HOLE_D, gp.PIN_LEN + 1.0)
    if g.common_volume(hole, body) > 1e-6:
        fails.append('(5) cap material in the reference hole')
    dc, ds = math.cos(math.radians(gp.DOWEL_ARM_THETA_DEG)), math.sin(math.radians(gp.DOWEL_ARM_THETA_DEG))
    z_mid = gp.DOWEL_ARM_UNDERSIDE_N + (seat - gp.CAP_DESIGN_SEAT_N) + 2.0
    half_s = gp.DOWEL_SLOT_W / 2.0
    for r in (gp.DOWEL_SLOT_R[0] + 0.5, gp.DOWEL_AXIS_RW[0], gp.DOWEL_SLOT_R[1] - 0.1):
        for w in (0.0, half_s - 0.06, -(half_s - 0.06)):
            if g.inside(body, along(dc, ds, r, w, z_mid)):
                fails.append(f'(6) dowel slot blocked at r {r:g} w {w:g}')
        for w in (half_s + 0.1, -(half_s + 0.1)):
            if r < gp.DOWEL_SLOT_R[1] - 0.2 and not g.inside(body, along(dc, ds, r, w, z_mid)):
                fails.append(f'(6) dowel slot wider than {gp.DOWEL_SLOT_W} at r {r:g}')
    report = {'seat_n': seat, 'valid': g.valid(body), 'reach_horn': reached['horn'], 'reach_case': reached['case'],
              'worst_land_gap_mm': round(worst_gap, 3), 'interference_points': interference,
              'failures': list(fails)}
    return fails, report


# ── the cradle ──

def build_cradle(g, with_bumps=True):
    bp = gp.BASE_PLATE
    (r0, r1), (w0, w1), (n_bot, n_top) = bp['r'], bp['w'], bp['n']
    body = g.box(r0, w0, n_bot, r1, w1, n_top)
    co = gp.CABLE_OPENING
    body = g.cut(body, g.box(co['r'][0], -co['w_half'], n_bot - 1.0, co['r'][1], co['w_half'], n_top + 1.0))
    fs = gp.FOOT_SIZE
    for fr in ((r0, r0 + fs), (r1 - fs, r1)):
        for fw in ((w0, w0 + fs), (w1 - fs, w1)):
            body = g.fuse(body, g.box(fr[0], fw[0], gp.FOOT_N[0], fr[1], fw[1], gp.FOOT_N[1] + 0.5))
    for pad in gp.BOTTOM_PADS:
        body = g.fuse(body, g.cyl_z(pad['r'], pad['w'], n_top - 0.5, pad['d'], pad['top_n'] - n_top + 0.5))
    t, top, lead = gp.WALL_THICKNESS, gp.WALL_TOP_N, gp.LEAD_IN
    wi = gp.WALL_INNER
    near_out, far_out = wi['near'] - t, wi['far'] + t
    walls = [
        g.prism([(near_out, wi['-w'] - t, n_bot), (near_out, wi['-w'], n_bot), (near_out, wi['-w'], top - lead),
                 (near_out, wi['-w'] - lead, top), (near_out, wi['-w'] - t, top)], (far_out - near_out, 0.0, 0.0)),
        g.prism([(near_out, wi['+w'], n_bot), (near_out, wi['+w'] + t, n_bot), (near_out, wi['+w'] + t, top),
                 (near_out, wi['+w'] + lead, top), (near_out, wi['+w'], top - lead)], (far_out - near_out, 0.0, 0.0)),
        g.prism([(near_out, w0, n_bot), (wi['near'], w0, n_bot), (wi['near'], w0, top - lead),
                 (wi['near'] - lead, w0, top), (near_out, w0, top)], (0.0, w1 - w0, 0.0)),
        g.prism([(wi['far'], w0, n_bot), (far_out, w0, n_bot), (far_out, w0, top), (wi['far'] + lead, w0, top),
                 (wi['far'], w0, top - lead)], (0.0, w1 - w0, 0.0)),
    ]
    for wall in walls:
        body = g.fuse(body, wall)
    h = gp.DATUM_PAD_H
    for pad in gp.SIDE_PADS:
        body = g.fuse(body, g.cyl((pad['r'], wi['-w'] - 0.5, pad['n']), (0.0, 1.0, 0.0), gp.DATUM_PAD_D, h + 0.5))
    body = g.fuse(body, g.cyl((wi['near'] - 0.5, gp.END_PAD['w'], gp.END_PAD['n']), (1.0, 0.0, 0.0),
                              gp.DATUM_PAD_D, h + 0.5))
    sl, ft, root, free = gp.FLEXURE_SLOT, gp.FLEXURE_T, gp.FLEXURE_ROOT_N, gp.FLEXURE_FREE_N
    # +w wall tongue (r FLEXURE_PLUS_W_R, the inner FLEXURE_T of the wall)
    a, b = gp.FLEXURE_PLUS_W_R
    win, wout = wi['+w'], wi['+w'] + t
    for cutter in (g.box(a - sl, win - 0.5, root, a, wout + 0.5, free + sl),
                   g.box(b, win - 0.5, root, b + sl, wout + 0.5, free + sl),
                   g.box(a - sl, win - 0.5, free, b + sl, wout + 0.5, free + sl),
                   g.box(a, win + ft, root, b, wout + 0.5, free + sl)):
        body = g.cut(body, cutter)
    # far-end wall tongue (w FLEXURE_FAR_W)
    a, b = gp.FLEXURE_FAR_W
    rin, rout = wi['far'], wi['far'] + t
    for cutter in (g.box(rin - 0.3, a - sl, root, rout + 0.2, a, free + sl),
                   g.box(rin - 0.3, b, root, rout + 0.2, b + sl, free + sl),
                   g.box(rin - 0.3, a - sl, free, rout + 0.2, b + sl, free + sl),
                   g.box(rin + ft, a, root, rout + 0.2, b, free + sl)):
        body = g.cut(body, cutter)
    if with_bumps:
        br = gp.BUMP_D / 2.0
        axis_w = gp.BUMP_APEX['+w'] + br
        a, b = gp.FLEXURE_PLUS_W_R
        bump = g.cyl((a, axis_w, gp.BUMP_N), (1.0, 0.0, 0.0), gp.BUMP_D, b - a)
        bump = g.o.BRepAlgoAPI_Common(bump, g.box(a, axis_w - br - 0.1, gp.BUMP_N - br - 0.1, b, axis_w,
                                                  gp.BUMP_N + br + 0.1)).Shape()
        body = g.fuse(body, bump)
        axis_r = gp.BUMP_APEX['far'] + br
        a, b = gp.FLEXURE_FAR_W
        bump = g.cyl((axis_r, a, gp.BUMP_N), (0.0, 1.0, 0.0), gp.BUMP_D, b - a)
        bump = g.o.BRepAlgoAPI_Common(bump, g.box(axis_r - br - 0.1, a, gp.BUMP_N - br - 0.1, axis_r, b,
                                                  gp.BUMP_N + br + 0.1)).Shape()
        body = g.fuse(body, bump)
    post = gp.DOWEL_POST
    body = g.fuse(body, g.box(post['r'][0], -post['w_half'], n_bot, post['r'][1], post['w_half'], post['top_n']))
    body = g.cut(body, g.cyl_z(gp.DOWEL_AXIS_RW[0], gp.DOWEL_AXIS_RW[1], gp.DOWEL_HOLE_BOTTOM_N, gp.DOWEL_HOLE_D,
                               post['top_n'] - gp.DOWEL_HOLE_BOTTOM_N + 1.0))
    body = g.cut(body, _p0_arrow(g))
    return body


def _p0_arrow(g):
    """The groove of the P0 direction arrow in the near-end wall top: an arc about
    the axis from θ0 to the head, the head's tip at ``head_at_deg``."""
    ar, head = gp.P0_ARROW, gp.P0_ARROW_HEAD
    radius, half = ar['radius'], ar['width'] / 2.0
    z0, height = ar['top_n'] - ar['depth'], ar['depth'] + 1.0
    tip = ar['head_at_deg']
    base = tip - math.degrees(head['length'] / radius)
    ring = g.cut(g.cyl_z(0.0, 0.0, z0, 2.0 * (radius + half), height),
                 g.cyl_z(0.0, 0.0, z0 - 1.0, 2.0 * (radius - half), height + 2.0))
    big = 2.0 * radius
    th0 = ar['theta_deg'][0]
    rim = [(big * math.cos(math.radians(t)), big * math.sin(math.radians(t)), z0 - 1.0)
           for t in (th0, (th0 + base) / 2.0, base)]
    wedge = g.prism([(0.0, 0.0, z0 - 1.0)] + rim, (0.0, 0.0, height + 2.0))
    shaft = g.o.BRepAlgoAPI_Common(ring, wedge).Shape()
    cb, sb = math.cos(math.radians(base)), math.sin(math.radians(base))
    ct, st = math.cos(math.radians(tip)), math.sin(math.radians(tip))
    hw = head['width'] / 2.0
    tri = g.prism([((radius - hw) * cb, (radius - hw) * sb, z0), ((radius + hw) * cb, (radius + hw) * sb, z0),
                   (radius * ct, radius * st, z0)], (0.0, 0.0, height))
    return g.fuse(shaft, tri)


def check_cradle(g, body, no_bumps):
    fails = []
    if not g.valid(body):
        fails.append('(1) invalid cradle')
    if not g.valid(no_bumps):
        fails.append('(1) invalid cradle without bumps')
    e = gp.PAD_APEX_PROBE

    def apex(label, outside, inside):
        if g.inside(body, outside):
            fails.append(f'(7) {label}: material beyond the apex')
        if not g.inside(body, inside):
            fails.append(f'(7) {label}: no material at the apex')

    for pad in gp.BOTTOM_PADS:
        apex(pad['name'], (pad['r'], pad['w'], pad['top_n'] + e), (pad['r'], pad['w'], pad['top_n'] - e))
    side_apex = gp.WALL_INNER['-w'] + gp.DATUM_PAD_H
    for i, pad in enumerate(gp.SIDE_PADS):
        apex(f'side pad {i + 1}', (pad['r'], side_apex + e, pad['n']), (pad['r'], side_apex - e, pad['n']))
    end_apex = gp.WALL_INNER['near'] + gp.DATUM_PAD_H
    apex('end pad', (end_apex + e, gp.END_PAD['w'], gp.END_PAD['n']), (end_apex - e, gp.END_PAD['w'], gp.END_PAD['n']))
    mid_r = sum(gp.FLEXURE_PLUS_W_R) / 2.0
    apex('+w bump', (mid_r, gp.BUMP_APEX['+w'] - e, gp.BUMP_N), (mid_r, gp.BUMP_APEX['+w'] + e, gp.BUMP_N))
    mid_w = sum(gp.FLEXURE_FAR_W) / 2.0
    apex('far bump', (gp.BUMP_APEX['far'] - e, mid_w, gp.BUMP_N), (gp.BUMP_APEX['far'] + e, mid_w, gp.BUMP_N))
    shared = {}
    for b in gp.CASE_ENVELOPE_BOXES:
        v = g.common_volume(no_bumps, g.box(*b))
        shared[str(b)] = v
        if v >= gp.ENVELOPE_MAX_SHARED_MM3:
            fails.append(f'(7) the cradle shares {v:.4f} mm³ with the case envelope box {b}')
    line = gp.CABLE_LINE
    probe = g.cyl_z(line['r'], line['w'], line['n'][0], 0.1, line['n'][1] - line['n'][0])
    if g.common_volume(probe, body) > 1e-9:
        fails.append('(7) the cable line is blocked')
    ar = gp.P0_ARROW
    p = (-ar['radius'], 0.0)   # θ 180°
    if g.inside(body, (p[0], p[1], ar['top_n'] - ar['depth'] / 2.0)):
        fails.append('(7) the P0 arrow groove is missing')
    if not g.inside(body, (p[0], p[1], ar['top_n'] - ar['depth'] - 0.25)):
        fails.append('(7) no material under the P0 arrow groove')
    return fails, {'valid': g.valid(body), 'envelope_shared_mm3': max(shared.values()), 'failures': list(fails)}


# ── output ──

def _write_step(o, shape, path):
    w = o.STEPControl_Writer()
    w.Transfer(shape, o.STEPControl_AsIs)
    if w.Write(path) != o.IFSelect_RetDone:
        raise RuntimeError(f'STEP write failed: {path}')


def _write_stl(o, shape, path):
    o.BRepTools.Clean_s(shape)
    o.BRepMesh_IncrementalMesh(shape, gp.STL_DEFLECTION, False, gp.STL_ANGULAR, True)
    w = o.StlAPI_Writer()
    w.ASCIIMode = False
    if not w.Write(shape, path):
        raise RuntimeError(f'STL write failed: {path}')


def stl_open_edges(path):
    """Edges not shared by exactly two triangles after welding at 1e-5 mm."""
    import numpy as np
    with open(path, 'rb') as f:
        data = f.read()
    count = struct.unpack('<I', data[80:84])[0]
    tris = np.frombuffer(data[84:84 + count * 50],
                         dtype=np.dtype([('n', '<f4', 3), ('v', '<f4', (3, 3)), ('a', '<u2')]))
    verts = tris['v'].astype(np.float64).reshape(-1, 3)
    _, idx = np.unique(np.round(verts / 1e-5).astype(np.int64), axis=0, return_inverse=True)
    idx = idx.reshape(-1, 3)
    edges = collections.Counter()
    for a, b, c in idx:
        for e in ((a, b), (b, c), (c, a)):
            edges[(min(e), max(e))] += 1
    return count, sum(1 for v in edges.values() if v != 2)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _commit():
    try:
        out = subprocess.run(['git', '-C', _REPO, 'rev-parse', 'HEAD'], capture_output=True, text=True,
                             timeout=10, check=False)
        return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else 'unknown'
    except (OSError, subprocess.SubprocessError):
        return 'unknown'


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _parameters():
    out = {}
    for name in sorted(vars(gp)):
        value = getattr(gp, name)
        if name.isupper() and isinstance(value, (int, float, str, tuple, list, dict)):
            out[name] = _jsonable(value)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description='Build the V1 servo gauge (cradle + two key caps).')
    parser.add_argument('--out', required=True, help='output directory (e.g. tools/servo_gauge/out)')
    parser.add_argument('--self-check', action='store_true', help='run the geometric self-check; exit 1 on a failure')
    args = parser.parse_args(argv)

    o = _ocp()
    _quiet(o)
    g = _Geo(o)
    caps = {cap: build_cap(g, cap, gp.CAP_DESIGN_SEAT_N) for cap in gp.CAP_IDS}
    cradle = build_cradle(g, with_bumps=True)
    report = {}
    failures = []
    if args.self_check:
        for cap in gp.CAP_IDS:
            for seat in gp.CAP_CHECK_SEATS_N[cap]:
                body = caps[cap] if abs(seat - gp.CAP_DESIGN_SEAT_N) < 1e-9 else build_cap(g, cap, seat)
                fails, rep = check_cap(g, cap, seat, body)
                report[f'cap_{cap}@{seat:.2f}'] = rep
                failures += [f'cap {cap} seat {seat:.2f}: {f}' for f in fails]
                print(f"cap {cap} seat {seat:.2f}: valid={rep['valid']} reach horn ×{rep['reach_horn']}, "
                      f"case ×{rep['reach_case']}; land {rep['worst_land_gap_mm']:.2f} mm above the case; "
                      f"interference {rep['interference_points']}; failures: {fails or 'none'}")
        fails, rep = check_cradle(g, cradle, build_cradle(g, with_bumps=False))
        report['cradle'] = rep
        failures += [f'cradle: {f}' for f in fails]
        print(f"cradle: valid={rep['valid']} shared with the case envelope {rep['envelope_shared_mm3']:.6f} mm³; "
              f"failures: {fails or 'none'}")
        if failures:
            for f in failures:
                print(f'[FEHLER] Selbstprüfung: {f}', file=sys.stderr)
            return 1

    os.makedirs(args.out, exist_ok=True)
    files = {}
    stl_report = {}
    for stem, shape in (('cradle', cradle), ('cap_J15', caps['J15']), ('cap_GR', caps['GR'])):
        step_path = os.path.join(args.out, f'{stem}.step')
        stl_path = os.path.join(args.out, f'{stem}.stl')
        _write_step(o, shape, step_path)
        _write_stl(o, shape, stl_path)
        triangles, open_edges = stl_open_edges(stl_path)
        stl_report[stem] = {'triangles': triangles, 'open_edges': open_edges}
        if open_edges:
            failures.append(f'{stem}.stl: {open_edges} edges not shared by exactly two triangles')
        files[f'{stem}.step'] = _sha256(step_path)
        files[f'{stem}.stl'] = _sha256(stl_path)
    if args.self_check and failures:
        for f in failures:
            print(f'[FEHLER] Selbstprüfung: (8) {f}', file=sys.stderr)
        for name in files:
            os.remove(os.path.join(args.out, name))
        return 1
    manifest = {
        'tool': 'tools/servo_gauge/build_gauge.py',
        'commit': _commit(),
        'servo_spec_digest': gp.fss.V1.digest,
        'servo_spec_pinned': gp.fss.V1.pinned,
        'parameters': _parameters(),
        'files': files,
        'stl': stl_report,
        'self_check': report if args.self_check else None,
    }
    with open(os.path.join(args.out, 'gauge_manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=1, sort_keys=True, ensure_ascii=False)
    print(f'wrote {len(files)} files + gauge_manifest.json to {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

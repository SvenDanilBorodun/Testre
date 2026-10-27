#!/usr/bin/env python3
"""Cut the assembly marks into the V1's printed arm parts, from the owner's STEP.

    $CADPY tools/v1_marked_parts/build_marked_parts.py --step FILE.STEP --out DIR \\
        [--self-check] [--accept-placements]

``$CADPY`` is a Python with ``cadquery-ocp==8.0.1.1.0`` (module ``OCP``) and
numpy; both are imported only inside functions, so CI compiles this file
without them. The STEP is the owner's assembly at the URDF zero pose; it is read
only and never copied.

Order: read → frames → zero-pose check → search → placement fence → cut and
verify → outputs.

* frames: every joint's horn (by its holes coaxial with the child part's holes)
  and servo (by its cylinder coaxial with the horn) from the B-rep, never from
  the instance order;
* zero-pose check (``marks_params.pose_findings``): a STEP exported with a joint
  turned refuses the whole run with „STEP nicht in der Nullstellung"; no flag
  overrides it;
* search: per mark the deterministic placement of build spec §6B on the uncut
  part, with exact B-rep clearances;
* placement fence (``marks_params.placement_findings``): an outcome outside
  ``EXPECTED`` refuses unless ``--accept-placements``, which then prints the new
  ``EXPECTED`` block to paste and records ``accepted_placements: true``;
* cut and verify (V1–V8) per part; a part that fails is not written;
* outputs per part: ``<stem>.step``, ``<stem>.stl``, ``<stem>_marks.png``,
  ``<stem>_iso.png``, and ``marked_parts_manifest.json``.

Exit 0 done; 1 any refusal (a pose or placement refusal writes nothing at all).
The outputs are derived from the owner's STEP, re-made after every CAD export
and never committed (the default location ``tools/v1_marked_parts/out/`` is
gitignored). ``--self-check`` adds one warning-only check: the horn top above
the output face (4.70 joints 1–5, 5.70 claw).
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
import time
import types
import zlib

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, '..', '..'))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import marks_params as mp  # noqa: E402

_RAY_FAR = 80.0          # rays start this far above the output-face plane
_DEFAULT_OUT = os.path.join(_HERE, 'out')
_BACKGROUND = 32          # dark preview background: J1's mark colour is white


def _ocp():
    """Every OCP name this script uses, imported here and only here."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism
    from OCP.BRepTools import BRepTools
    from OCP.Bnd import Bnd_Box
    from OCP.GProp import GProp_GProps
    from OCP.GeomAbs import GeomAbs_Cylinder, GeomAbs_Plane
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector
    from OCP.Message import Message, Message_Gravity
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.StlAPI import StlAPI_Writer
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDF import TDF_Label
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopAbs import TopAbs_FACE, TopAbs_IN, TopAbs_ON, TopAbs_REVERSED, TopAbs_SOLID, TopAbs_VERTEX
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    from OCP.collections import Sequence_TDF_Label
    from OCP.gp import gp_Ax2, gp_Dir, gp_Lin, gp_Pnt, gp_Vec
    import numpy
    return types.SimpleNamespace(np=numpy, **{k: v for k, v in locals().items() if k != 'numpy'})


def _quiet(o):
    """Keep OCP's STEP translator statistics off the console (failures still print)."""
    printers = o.Message.DefaultMessenger_s().Printers()
    for i in range(1, printers.Length() + 1):
        printers.Value(i).SetTraceLevel(o.Message_Gravity.Message_Fail)


class _Cad:
    """B-rep queries over OCP."""

    def __init__(self, o):
        self.o = o
        self.np = o.np

    def _cast(self, item, kind):
        td = self.o.TopoDS
        name = {'face': 'Face', 'vertex': 'Vertex'}[kind]
        return getattr(td, name + '_s')(item) if hasattr(td, name + '_s') else getattr(td, name)(item)

    def faces(self, shape):
        out, e = [], self.o.TopExp_Explorer(shape, self.o.TopAbs_FACE)
        while e.More():
            out.append(self._cast(e.Current(), 'face'))
            e.Next()
        return out

    def vertices(self, shape):
        out, e = [], self.o.TopExp_Explorer(shape, self.o.TopAbs_VERTEX)
        while e.More():
            out.append(self.np.array(self.o.BRep_Tool.Pnt_s(self._cast(e.Current(), 'vertex')).Coord()))
            e.Next()
        return self.np.array(out)

    def cylinders(self, shape):
        out = []
        for f in self.faces(shape):
            a = self.o.BRepAdaptor_Surface(f)
            if a.GetType() == self.o.GeomAbs_Cylinder:
                c = a.Cylinder()
                ax = c.Axis()
                g = self.o.GProp_GProps()
                self.o.BRepGProp.SurfaceProperties_s(f, g)
                d = self.np.array(ax.Direction().Coord())
                out.append((self.np.array(ax.Location().Coord()), d / self.np.linalg.norm(d), c.Radius(),
                            self.np.array(g.CentreOfMass().Coord())))
        return out

    def planes(self, shape):
        out = []
        for f in self.faces(shape):
            a = self.o.BRepAdaptor_Surface(f)
            if a.GetType() == self.o.GeomAbs_Plane:
                pl = a.Plane()
                n = self.np.array(pl.Axis().Direction().Coord())
                if f.Orientation() == self.o.TopAbs_REVERSED:
                    n = -n
                g = self.o.GProp_GProps()
                self.o.BRepGProp.SurfaceProperties_s(f, g)
                out.append((n, self.np.array(pl.Location().Coord()), g.Mass()))
        return out

    def volume(self, shape):
        g = self.o.GProp_GProps()
        self.o.BRepGProp.VolumeProperties_s(shape, g)
        return g.Mass()

    def centroid(self, shape):
        g = self.o.GProp_GProps()
        self.o.BRepGProp.VolumeProperties_s(shape, g)
        return self.np.array(g.CentreOfMass().Coord())

    def bbox(self, shape):
        b = self.o.Bnd_Box()
        self.o.BRepBndLib.Add_s(shape, b)
        lo, hi = b.CornerMin(), b.CornerMax()
        return self.np.array([lo.X(), lo.Y(), lo.Z(), hi.X(), hi.Y(), hi.Z()])

    def solids(self, shape):
        e, k = self.o.TopExp_Explorer(shape, self.o.TopAbs_SOLID), 0
        while e.More():
            k += 1
            e.Next()
        return k

    def valid(self, shape):
        return self.o.BRepCheck_Analyzer(shape).IsValid()

    def distance(self, a, b):
        return self.o.BRepExtrema_DistShapeShape(a, b).Value()

    def intersector(self, shape):
        x = self.o.IntCurvesFace_ShapeIntersector()
        x.Load(shape, 1e-6)
        return x

    def ray(self, x, start, direction, length):
        """Hits of the ray ``start + t·direction``, t in [0, length], sorted by t: [(t, face)]."""
        o = self.o
        x.Perform(o.gp_Lin(o.gp_Pnt(*map(float, start)), o.gp_Dir(*map(float, direction))), 0.0, float(length))
        return sorted(((x.WParameter(k), x.Face(k)) for k in range(1, x.NbPnt() + 1)), key=lambda h: h[0])

    def arrow_tool(self, base, nrm, e, q, d, length, width):
        """The arrow pocket tool: tip at ``base + d·e`` pointing at ``base``, from
        TOOL_LEAD above the face to ARROW_DEPTH below it."""
        o, np = self.o, self.np
        hl = mp.ARROW_HEAD_FRAC * length
        s = mp.ARROW_SHAFT_W / 2.0
        poly = o.BRepBuilderAPI_MakePolygon()
        for x, y in ((0.0, 0.0), (hl, width / 2.0), (hl, s), (length, s), (length, -s), (hl, -s), (hl, -width / 2.0)):
            poly.Add(o.gp_Pnt(*map(float, base + (d + x) * e + y * q + mp.TOOL_LEAD * nrm)))
        poly.Close()
        face = o.BRepBuilderAPI_MakeFace(poly.Wire(), True).Face()
        down = -(mp.ARROW_DEPTH + mp.TOOL_LEAD) * np.asarray(nrm)
        return o.BRepPrimAPI_MakePrism(face, o.gp_Vec(*map(float, down))).Shape()

    def dot_tool(self, centre, nrm):
        o = self.o
        top = centre + mp.TOOL_LEAD * nrm
        return o.BRepPrimAPI_MakeCylinder(o.gp_Ax2(o.gp_Pnt(*map(float, top)), o.gp_Dir(*map(float, -nrm))),
                                          mp.DOT_D / 2.0, mp.DOT_DEPTH + mp.TOOL_LEAD).Shape()


# ── reading ──

def read_step(o, path):
    """``[(product name, located shape)]`` of every leaf instance, walking the assembly."""
    doc = o.TDocStd_Document(o.TCollection_ExtendedString('doc'))
    reader = o.STEPCAFControl_Reader()
    reader.SetNameMode(True)
    if reader.ReadFile(path) != o.IFSelect_RetDone:
        raise RuntimeError('unreadable')
    reader.Transfer(doc)
    tool = o.XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())

    def name(label):
        n = o.TDataStd_Name()
        return n.Get().ToExtString() if label.FindAttribute(o.TDataStd_Name.GetID_s(), n) else '?'

    out = []

    def walk(label, loc):
        if tool.IsAssembly_s(label):
            comps = o.Sequence_TDF_Label()
            tool.GetComponents_s(label, comps)
            for i in range(1, comps.Length() + 1):
                c = comps.Value(i)
                ref = o.TDF_Label()
                tool.GetReferredShape_s(c, ref)
                walk(ref, loc.Multiplied(tool.GetLocation_s(c)))
        else:
            out.append((name(label), tool.GetShape_s(label).Moved(loc)))

    roots = o.Sequence_TDF_Label()
    tool.GetFreeShapes(roots)
    for i in range(1, roots.Length() + 1):
        walk(roots.Value(i), o.TopLoc_Location())
    return out


def find_parts(instances):
    """``({product: shape}, [German refusal])`` — each part exactly once."""
    parts, refusals = {}, []
    for product, _stem, match in mp.PARTS:
        hits = [s for n, s in instances if (n.startswith(product) if match == 'prefix' else n == product)]
        if len(hits) != 1:
            refusals.append(f'Teil „{product}" {len(hits)}-mal im STEP gefunden statt genau einmal.')
        else:
            parts[product] = hits[0]
    return parts, refusals


# ── frames ──

def _coaxial(np, p1, d1, p2, d2):
    if abs(abs(float(np.dot(d1, d2))) - 1.0) >= 1.0 - math.cos(math.radians(mp.COAXIAL_DEG)) + 1e-12:
        return False
    v = p2 - p1
    return float(np.linalg.norm(v - np.dot(v, d1) * d1)) <= mp.COAXIAL_MM


def find_frames(cad, instances, parts):
    """Per joint ``{c, n, r, w, servo, servo_name, horn, holes, matched}`` from the
    B-rep; joints whose horn or servo is not unique are left out (and named)."""
    np = cad.np
    horns = [(n, s, cad.cylinders(s)) for n, s in instances if n == mp.HORN_PRODUCT]
    servos = [(n, s) for n, s in instances if n in mp.SERVO_PRODUCTS]
    servo_cyls = [(n, s, cad.cylinders(s)) for n, s in servos]
    hole_r, hole_tol = mp.HORN_HOLE_RADIUS
    lo, hi = mp.CHILD_HOLE_RADIUS
    frames, problems = {}, []
    for joint in mp.JOINTS:
        child = parts.get(mp.CHILD_PART[joint])
        if child is None:
            problems.append(f'{joint}: Teil „{mp.CHILD_PART[joint]}" fehlt')
            continue
        child_cyls = [c for c in cad.cylinders(child) if lo <= c[2] <= hi]
        cands = []
        for _hn, hs, hc in horns:
            axes = []
            for h in (c for c in hc if abs(c[2] - hole_r) < hole_tol):
                if not any(_coaxial(np, h[0], h[1], a[0], a[1]) for a in axes):
                    axes.append(h)
            matched = [a for a in axes if any(_coaxial(np, a[0], a[1], cp, cd) and
                                              abs(float(np.dot(ccm - a[3], a[1]))) <= mp.HORN_CHILD_AXIAL_MM
                                              for cp, cd, _cr, ccm in child_cyls)]
            if len(matched) >= 2:
                cands.append((hs, axes, matched))
        if len(cands) != 1:
            problems.append(f'{joint}: {len(cands)} passende Servohörner statt genau eines')
            continue
        horn, axes, matched = cands[0]
        if len(axes) != 4:
            problems.append(f'{joint}: {len(axes)} Hornlöcher statt 4')
            continue
        d = axes[0][1]
        axis_p = np.mean([a[0] + np.dot(axes[0][3] - a[0], d) * d for a in axes], axis=0)
        sv = [(sn, ss) for sn, ss, sc in servo_cyls
              if any(_coaxial(np, axis_p, d, cp, cd) and abs(float(np.dot(ccm - axis_p, d))) <= mp.SERVO_AXIAL_MM
                     for cp, cd, _cr, ccm in sc)]
        if len(sv) != 1:
            problems.append(f'{joint}: {len(sv)} passende Servos statt genau eines')
            continue
        servo_name, servo = sv[0]
        n = d if np.dot(axis_p - cad.centroid(servo), d) > 0 else -d
        pl = cad.planes(servo)
        big = max(pl, key=lambda x: x[2])
        w0 = big[0] - np.dot(big[0], n) * n
        w0 /= np.linalg.norm(w0)
        r = np.cross(w0, n)
        proj = (cad.vertices(servo) - axis_p) @ r
        if -proj.min() > proj.max():
            r = -r
        w = np.cross(n, r)
        outs = [x for x in pl if np.dot(x[0], n) > math.cos(math.radians(mp.OUTPUT_FACE_TOL_DEG))]
        if not outs:
            problems.append(f'{joint}: keine Abtriebsfläche am Servo')
            continue
        of = max(outs, key=lambda x: x[2])
        c = axis_p + np.dot(of[1] - axis_p, n) * n
        frames[joint] = {'c': c, 'n': n, 'r': r, 'w': w, 'servo': servo, 'servo_name': servo_name, 'horn': horn,
                         'holes': axes, 'matched': matched, 'output_face_mm2': of[2]}
    return frames, problems


def _u(np, f, theta_deg):
    t = math.radians(theta_deg)
    return math.cos(t) * f['r'] + math.sin(t) * f['w']


def _theta_of(np, f, point):
    v = point - f['c']
    return math.degrees(math.atan2(float(np.dot(v, f['w'])), float(np.dot(v, f['r'])))) % 360.0


def measure_pose(np, frames):
    """The zero-pose measurements ``pose_findings`` decides on (s03)."""
    measured = {}
    if 'joint1' not in frames:
        return measured
    f1 = frames['joint1']
    basis = np.stack([f1['r'], f1['w'], f1['n']])
    for i, joint in enumerate(mp.JOINTS):
        f = frames.get(joint)
        if f is None:
            continue
        c, n = f['c'], f['n']
        u = _u(np, f, mp.THETA_REF_DEG[i])
        h = c + mp.HOLE_RADIUS_MM * u

        def on_plane(a):
            return a[0] + np.dot(c - a[0], n) * n

        dists = sorted(float(np.linalg.norm((h - a[0]) - np.dot(h - a[0], n) * n)) for a in f['holes'])
        measured[joint] = {
            'h_dist_mm': round(dists[0], 4),
            'next_hole_mm': round(dists[1], 3),
            'matched_thetas_deg': sorted(round(_theta_of(np, f, on_plane(a)), 2) for a in f['matched']),
            'rel_n': tuple(round(float(x), 6) for x in basis @ n),
            'rel_u': tuple(round(float(x), 6) for x in basis @ u),
        }
    return measured


# ── the placement search (on the uncut part) ──

class _Part:
    def __init__(self, cad, shape):
        self.cad, self.shape = cad, shape
        self.x = cad.intersector(shape)
        self.faces = cad.faces(shape)

    def hit(self, p, out_dir):
        """First hit coming from ``p + _RAY_FAR·out_dir`` along ``−out_dir``:
        ``(face, height of the hit above p along out_dir, material below, outward normal)``
        or None when nothing or a non-planar face is hit first."""
        np, o = self.cad.np, self.cad.o
        hits = self.cad.ray(self.x, p + _RAY_FAR * out_dir, -out_dir, 400.0)
        if not hits:
            return None
        t0, face = hits[0]
        a = o.BRepAdaptor_Surface(face)
        if a.GetType() != o.GeomAbs_Plane:
            return None
        nn = np.array(a.Plane().Axis().Direction().Coord())
        if face.Orientation() == o.TopAbs_REVERSED:
            nn = -nn
        below = hits[1][0] - t0 if len(hits) > 1 else 99.0
        return face, _RAY_FAR - t0, below, nn

    def clearance(self, tool, target):
        return min((self.cad.distance(tool, f) for f in self.faces if not f.IsSame(target)), default=99.0)

    def one_face(self, points, out_dir, min_below, tol_deg):
        """``(face, height)`` when every point is hit first by ONE planar face with
        outward normal within ``tol_deg`` of ``out_dir``, at one height, with
        ``min_below`` of material below; else None."""
        np = self.cad.np
        ref = None
        cos_tol = math.cos(math.radians(tol_deg))
        for p in points:
            h = self.hit(p, out_dir)
            if h is None:
                return None
            face, top, below, nn = h
            if np.dot(nn, out_dir) < cos_tol or below < min_below:
                return None
            if ref is None:
                ref = (face, top)
            elif not (face.IsSame(ref[0]) and abs(top - ref[1]) <= mp.FACE_HEIGHT_TOL):
                return None
        return ref


def _arange(start, stop, step):
    k, out = 0, []
    while True:
        v = round(start + k * step, 6)
        if (step > 0 and v > stop + 1e-9) or (step < 0 and v < stop - 1e-9):
            return out
        out.append(v)
        k += 1


def _arrow_samples(length, width):
    """The arrow footprint grown by FOOTPRINT_GROW, on a SAMPLE_STEP grid: (x along e, y along q)."""
    hl = mp.ARROW_HEAD_FRAC * length
    g = mp.FOOTPRINT_GROW
    pts = []
    for x in _arange(-g, length + g, mp.SAMPLE_STEP):
        half = ((width / 2.0) * (x / hl) if 0.0 <= x < hl else (mp.ARROW_SHAFT_W / 2.0 if x >= hl else 0.0)) + g
        for y in _arange(-half, half, mp.SAMPLE_STEP):
            pts.append((x, y))
    return pts


def _disc(np, centre, a1, a2, radius, step):
    return [centre + x * a1 + y * a2 for x in _arange(-radius, radius, step) for y in _arange(-radius, radius, step)
            if x * x + y * y <= radius * radius + 1e-9]


def search_arrow(cad, part, f, joint_index):
    np = cad.np
    c, n = f['c'], f['n']
    th = mp.THETA_REF_DEG[joint_index]
    h_point = c + mp.HOLE_RADIUS_MM * _u(np, f, th)
    per_size = []
    for length, width in mp.ARROW_SIZES:
        samples = _arrow_samples(length, width)
        best = None
        for dphi in mp.DPHI_LADDER_DEG:
            e = _u(np, f, th + dphi)
            q = np.cross(n, e)
            for d in _arange(mp.D_MIN[joint_index], mp.D_MAX, mp.D_STEP):
                if best is not None and d >= best['d_mm'] - 1e-9:
                    break
                ref = part.one_face([h_point + (d + x) * e + y * q for x, y in samples], n, mp.ARROW_MIN_BELOW,
                                    mp.FACE_NORMAL_TOL_DEG)
                if ref is None:
                    continue
                base = h_point + ref[1] * n
                tool = cad.arrow_tool(base, n, e, q, d, length, width)
                clearance = part.clearance(tool, ref[0])
                if clearance < mp.CLEARANCE_MIN:
                    continue
                best = {'kind': 'arrow', 'size': (length, width), 'dphi_deg': float(dphi), 'd_mm': round(d, 2),
                        'face_n_mm': round(float(ref[1]), 3), 'clearance_mm': round(clearance, 3)}
                break
        if best is not None:
            per_size.append(best)
    near = [b for b in per_size if b['d_mm'] <= mp.D_PREFER_MAX + 1e-9]
    if near:
        return max(near, key=lambda b: b['size'][0])
    return min(per_size, key=lambda b: (b['d_mm'], -b['size'][0])) if per_size else None


def search_dot(cad, part, f, joint_index):
    np = cad.np
    c, n = f['c'], f['n']
    th = mp.THETA_REF_DEG[joint_index]
    u = _u(np, f, th)
    v = np.cross(n, u)
    servo_x = cad.intersector(f['servo'])
    (a0, a1), (b0, b1), step = mp.DOT_GRID
    target = mp.DOT_TARGET_R[joint_index]
    grid = sorted((math.hypot(a - target, b), a, b) for a in _arange(a0, a1, step) for b in _arange(b0, b1, step))
    for rule, band in (('A', mp.DOT_RULE_A_BAND), ('C', None)):
        for _dist, a, b in grid:
            centre = c + a * u + b * v
            h = part.hit(centre, n)
            if h is None or np.dot(h[3], n) < math.cos(math.radians(mp.FACE_NORMAL_TOL_DEG)) or h[2] < mp.DOT_MIN_BELOW:
                continue
            height = float(h[1])
            if band is not None and not (band[0] <= height <= band[1]):
                continue
            ref = part.one_face(_disc(np, centre, u, v, mp.DOT_SAMPLE_R, mp.SAMPLE_STEP), n, mp.DOT_MIN_BELOW,
                                mp.FACE_NORMAL_TOL_DEG)
            if ref is None:
                continue
            on_face = centre + height * n
            blocked = False
            for p in _disc(np, on_face + mp.TOOL_LEAD * n, u, v, mp.DOT_SAMPLE_R, mp.OCCLUSION_STEP):
                if cad.ray(servo_x, p, n, mp.OCCLUSION_RANGE) or cad.ray(part.x, p, n, mp.OCCLUSION_RANGE):
                    blocked = True
                    break
            if blocked:
                continue
            tool = cad.dot_tool(on_face, n)
            clearance = part.clearance(tool, ref[0])
            if clearance < mp.CLEARANCE_MIN:
                continue
            return {'kind': 'dot', 'rule': rule, 'centre_mm': (a, b), 'face_n_mm': round(height, 3),
                    'clearance_mm': round(clearance, 3)}
        if rule == 'A':
            found = _search_wall_dot(cad, part, f, joint_index)
            if found is not None:
                return found
    return None


def _search_wall_dot(cad, part, f, joint_index):
    """Rule B: a dot on an outward wall, a ray from RULE_B_START outside toward the axis."""
    np = cad.np
    c, n = f['c'], f['n']
    th = mp.THETA_REF_DEG[joint_index]
    h0, h1, hs = mp.RULE_B_HEIGHTS
    s0, s1, ss = mp.RULE_B_LATERAL
    for dpsi in mp.PSI_LADDER_DEG:
        psi = th + dpsi
        e = _u(np, f, psi)
        t = np.cross(n, e)
        for hn in _arange(h0, h1, -hs):
            for s in _arange(s0, s1, ss):
                start = c + hn * n + s * t + mp.RULE_B_START * e
                hits = cad.ray(part.x, start, -e, 400.0)
                if not hits:
                    continue
                on_face = start - hits[0][0] * e
                ref = part.one_face(_disc(np, on_face, n, t, mp.DOT_SAMPLE_R, mp.SAMPLE_STEP), e, mp.DOT_MIN_BELOW,
                                    mp.RULE_B_NORMAL_TOL_DEG)
                if ref is None:
                    continue
                tool = cad.dot_tool(on_face, e)
                clearance = part.clearance(tool, ref[0])
                if clearance < mp.CLEARANCE_MIN:
                    continue
                return {'kind': 'dot', 'rule': 'B', 'psi_deg': float(psi), 'height_mm': hn, 'lateral_mm': s,
                        'radius_mm': round(mp.RULE_B_START - hits[0][0], 3), 'clearance_mm': round(clearance, 3)}
    return None


def mark_geometry(np, f, joint_index, outcome):
    """The tool's placement in STEP coordinates: ``(anchor, normal, e, q, footprint samples, floor samples)``."""
    c, n = f['c'], f['n']
    th = mp.THETA_REF_DEG[joint_index]
    if outcome['kind'] == 'arrow':
        length, width = outcome['size']
        e = _u(np, f, th + outcome['dphi_deg'])
        q = np.cross(n, e)
        anchor = c + mp.HOLE_RADIUS_MM * _u(np, f, th) + outcome['face_n_mm'] * n
        d = outcome['d_mm']
        footprint = [anchor + (d + x) * e + y * q for x, y in _arrow_samples(length, width)]
        hl = mp.ARROW_HEAD_FRAC * length
        floor = [anchor + (d + x) * e for x in _arange(0.2, length - 0.2, 0.4)]
        floor += [anchor + (d + hl) * e + y * q for y in (-width / 2.0 + 0.3, width / 2.0 - 0.3)]
        return anchor, n, e, q, footprint, floor
    if outcome['rule'] == 'B':
        e = _u(np, f, outcome['psi_deg'])
        t = np.cross(n, e)
        anchor = c + outcome['radius_mm'] * e + outcome['height_mm'] * n + outcome['lateral_mm'] * t
        return anchor, e, n, t, _disc(np, anchor, n, t, mp.DOT_SAMPLE_R, mp.SAMPLE_STEP), \
            _disc(np, anchor, n, t, mp.DOT_D / 2.0 - 0.3, 0.6)
    u = _u(np, f, th)
    v = np.cross(n, u)
    a, b = outcome['centre_mm']
    anchor = c + a * u + b * v + outcome['face_n_mm'] * n
    return anchor, n, u, v, _disc(np, anchor, u, v, mp.DOT_SAMPLE_R, mp.SAMPLE_STEP), \
        _disc(np, anchor, u, v, mp.DOT_D / 2.0 - 0.3, 0.6)


# ── cut, verify, write ──

def _tool(cad, outcome, geometry):
    anchor, nrm, e, q, _fp, _floor = geometry
    if outcome['kind'] == 'arrow':
        length, width = outcome['size']
        return cad.arrow_tool(anchor, nrm, e, q, outcome['d_mm'], length, width), mp.arrow_volume(length, width)
    return cad.dot_tool(anchor, nrm), mp.dot_volume()


def cut_part(cad, product, original, marks, frames, outcomes):
    """Cut ``marks`` (in order) cumulatively, each check on the cumulative result
    (V1–V6 per mark, V7 per part). Returns ``(shape, per-mark reports, German
    failures, [(tool, joint index)], V7)``; the shape is None on any failure."""
    np = cad.np
    shape = original
    v_orig, bb_orig, solids_orig = cad.volume(original), cad.bbox(original), cad.solids(original)
    uncut = _Part(cad, original)
    reports, failures, tools, expected_sum = [], [], [], 0.0
    for _p, label, _kind, joint_index in marks:
        f = frames[mp.JOINTS[joint_index]]
        outcome = outcomes[(product, label)]
        geometry = mark_geometry(np, f, joint_index, outcome)
        anchor, nrm, _e, _q, footprint, floor = geometry
        tool, expect = _tool(cad, outcome, geometry)
        current = _Part(cad, shape)
        mid = footprint[len(footprint) // 2] if outcome['kind'] != 'arrow' else floor[len(floor) // 2]
        hits = cad.ray(current.x, mid + 5.0 * nrm, -nrm, 10.0)
        target = hits[0][1] if hits else None
        clearance = current.clearance(tool, target) if target is not None else 0.0
        result = cad.o.BRepAlgoAPI_Cut(shape, tool).Shape()
        removed = cad.volume(shape) - cad.volume(result)
        rx = cad.intersector(result)
        walls = []
        for p in floor:
            h = cad.ray(rx, p - (mp.DOT_DEPTH if outcome['kind'] == 'dot' else mp.ARROW_DEPTH) * nrm - 0.02 * nrm,
                        -nrm, 100.0)
            walls.append(h[0][0] + 0.02 if h else 99.0)
        v4 = uncut.one_face(footprint, nrm, mp.DOT_MIN_BELOW if outcome['kind'] == 'dot' else mp.ARROW_MIN_BELOW,
                            mp.RULE_B_NORMAL_TOL_DEG if outcome.get('rule') == 'B' else mp.FACE_NORMAL_TOL_DEG)
        checks = {
            'V1_valid_and_solids_unchanged': cad.valid(result) and cad.solids(result) == solids_orig,
            'V2_removed_volume': abs(removed - expect) / expect < mp.VOLUME_TOL_FRAC,
            'V3_bbox_unchanged': bool(np.allclose(cad.bbox(result), bb_orig, atol=mp.BBOX_TOL)),
            'V4_one_face_on_the_uncut_part': v4 is not None,
            'V5_clearance': clearance >= mp.CLEARANCE_MIN,
            'V6_wall_left': min(walls) >= mp.WALL_MIN,
        }
        reports.append({'mark': label, 'joint': mp.JOINTS[joint_index], 'outcome': _jsonable(outcome),
                        'anchor_step_mm': [round(float(x), 4) for x in anchor],
                        'normal_step': [round(float(x), 6) for x in nrm],
                        'removed_mm3': {'expected': round(expect, 4), 'actual': round(removed, 4)},
                        'wall_left_mm': round(min(walls), 3), 'clearance_mm': round(clearance, 3), 'checks': checks})
        failures += [f'{product} {label}: {name} nicht erfüllt' for name, ok in checks.items() if not ok]
        tools.append((tool, joint_index))
        expected_sum += expect
        shape = result
    v7 = abs(cad.volume(shape) - (v_orig - expected_sum)) <= mp.PART_VOLUME_TOL_FRAC * v_orig
    if not v7:
        failures.append(f'{product}: V7_part_volume nicht erfüllt')
    return (None if failures else shape), reports, failures, tools, v7


def write_stl(cad, shape, path):
    """V8: write the binary STL, halving the deflection up to MESH_RETRIES times
    until it is watertight. Returns ``(deflection used, triangles, open edges)``."""
    o = cad.o
    o.BRepTools.Clean_s(shape)
    deflection = mp.MESH_DEFLECTION
    for attempt in range(mp.MESH_RETRIES + 1):
        if attempt:
            o.BRepTools.Clean_s(shape)
        o.BRepMesh_IncrementalMesh(shape, deflection, False, mp.MESH_ANGULAR, True)
        w = o.StlAPI_Writer()
        w.ASCIIMode = False
        if not w.Write(shape, path):
            raise RuntimeError('STL write failed')
        triangles, open_edges = stl_open_edges(cad.np, path)
        if open_edges == 0 and triangles > 0:
            return deflection, triangles, 0
        deflection /= 2.0
    return deflection * 2.0, triangles, open_edges


def stl_open_edges(np, path):
    with open(path, 'rb') as f:
        data = f.read()
    count = struct.unpack('<I', data[80:84])[0]
    tris = np.frombuffer(data[84:84 + count * 50],
                         dtype=np.dtype([('n', '<f4', 3), ('v', '<f4', (3, 3)), ('a', '<u2')]))
    verts = tris['v'].astype(np.float64).reshape(-1, 3)
    _, idx = np.unique(np.round(verts / mp.MESH_WELD).astype(np.int64), axis=0, return_inverse=True)
    idx = idx.reshape(-1, 3)
    edges = collections.Counter()
    for a, b, c in idx:
        for e in ((a, b), (b, c), (c, a)):
            edges[(min(e), max(e))] += 1
    return count, sum(1 for v in edges.values() if v != 2)


def write_step(o, shape, path):
    w = o.STEPControl_Writer()
    w.Transfer(shape, o.STEPControl_AsIs)
    if w.Write(path) != o.IFSelect_RetDone:
        raise RuntimeError('STEP write failed')


# ── previews ──

def _hex_rgb(hex_colour):
    h = hex_colour.lstrip('#')
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def _triangles(cad, shape, tools):
    """``(triangles [k,3,3], colours [k,3])`` from the shape's current triangulation.
    A triangle whose centroid lies in or on a mark tool takes the joint colour,
    judged per triangle: a large face around a pocket has its node mean inside the
    pocket (its nodes crowd on the pocket's edge), so a per-face test would paint it."""
    np, o = cad.np, cad.o
    tris = []
    for face in cad.faces(shape):
        loc = o.TopLoc_Location()
        tri = o.BRep_Tool.Triangulation_s(face, loc)
        if tri is None:
            continue
        trsf = loc.Transformation()
        pts = np.array([tri.Node(k).Transformed(trsf).Coord() for k in range(1, tri.NbNodes() + 1)])
        idx = np.array([tri.Triangle(k).Get() for k in range(1, tri.NbTriangles() + 1)]) - 1
        tris.append(pts[idx])
    tris = np.concatenate(tris)
    cols = np.full((len(tris), 3), mp.PART_GREY)
    centres = tris.mean(axis=1)
    for tool, joint_index in tools:
        bb = cad.bbox(tool)
        near = np.all((centres >= bb[:3] - 1e-3) & (centres <= bb[3:] + 1e-3), axis=1)
        colour = _hex_rgb(mp.MARK_COLOUR_HEX[joint_index])
        for i in np.nonzero(near)[0]:
            state = o.BRepClass3d_SolidClassifier(tool, o.gp_Pnt(*map(float, centres[i])), 1e-3).State()
            if state in (o.TopAbs_IN, o.TopAbs_ON):
                cols[i] = colour
    return tris, cols


def render(np, tris, cols, view, x_axis, centre, field_mm, size):
    """Orthographic z-buffer render looking along ``view``; ``x_axis`` points right."""
    w_px, h_px = size
    view = view / np.linalg.norm(view)
    x_axis = x_axis - np.dot(x_axis, view) * view
    x_axis = x_axis / np.linalg.norm(x_axis)
    y_axis = np.cross(-view, x_axis)
    scale = w_px / field_mm
    rel = tris - centre
    px = (rel @ x_axis) * scale + w_px / 2.0
    py = -(rel @ y_axis) * scale + h_px / 2.0
    pz = -(rel @ view)
    normals = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    lengths = np.linalg.norm(normals, axis=1)
    img = np.full((h_px, w_px, 3), _BACKGROUND, np.uint8)
    zbuf = np.full((h_px, w_px), -1e18)
    lo_s, k_s = mp.SHADE
    for i in range(len(tris)):
        if lengths[i] == 0.0:
            continue
        xs3, ys3, zs3 = px[i], py[i], pz[i]
        x0, x1 = int(max(0, math.floor(xs3.min()))), int(min(w_px - 1, math.ceil(xs3.max())))
        y0, y1 = int(max(0, math.floor(ys3.min()))), int(min(h_px - 1, math.ceil(ys3.max())))
        if x0 > x1 or y0 > y1:
            continue
        (ax, bx, cx), (ay, by, cy), (az, bz, cz) = xs3, ys3, zs3
        den = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(den) < 1e-12:
            continue
        gx, gy = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
        l1 = ((by - cy) * (gx - cx) + (cx - bx) * (gy - cy)) / den
        l2 = ((cy - ay) * (gx - cx) + (ax - cx) * (gy - cy)) / den
        l3 = 1.0 - l1 - l2
        inside = (l1 >= -1e-9) & (l2 >= -1e-9) & (l3 >= -1e-9)
        z = l1 * az + l2 * bz + l3 * cz
        sub = zbuf[y0:y1 + 1, x0:x1 + 1]
        upd = inside & (z > sub)
        if not upd.any():
            continue
        sub[upd] = z[upd]
        shade = lo_s + k_s * abs(float(np.dot(normals[i] / lengths[i], view)))
        img[y0:y1 + 1, x0:x1 + 1][upd] = (np.array(cols[i]) * shade * 255.0).astype(np.uint8)
    return img


def write_png(np, img, path):
    h_px, w_px, _ = img.shape
    raw = b''.join(b'\x00' + img[y].tobytes() for y in range(h_px))

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)

    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w_px, h_px, 8, 2, 0, 0, 0))
                + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


def previews(cad, shape, marks, frames, outcomes, product, tools, stem_path):
    np = cad.np
    tris, cols = _triangles(cad, shape, tools)
    panels = []
    for _p, label, _kind, joint_index in marks:
        f = frames[mp.JOINTS[joint_index]]
        anchor, nrm, e, _q, footprint, _floor = mark_geometry(np, f, joint_index, outcomes[(product, label)])
        centre = np.mean(footprint, axis=0)
        panels.append(render(np, tris, cols, -nrm, e, centre, mp.PANEL_FIELD_MM[joint_index],
                             (mp.PANEL_PX, mp.PANEL_PX)))
    write_png(np, np.concatenate(panels, axis=1), stem_path + '_marks.png')
    f = frames[mp.JOINTS[marks[0][3]]]
    view = -(f['n'] + f['r'] + f['w']) / math.sqrt(3.0)
    bb = cad.bbox(shape)
    field = mp.ISO_FIELD_FACTOR * float(np.linalg.norm(bb[3:] - bb[:3]))
    write_png(np, render(np, tris, cols, view, f['r'] - f['w'], (bb[:3] + bb[3:]) / 2.0, field, mp.ISO_PX),
              stem_path + '_iso.png')


# ── helpers ──

def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, float):
        return round(value, 6)
    if hasattr(value, 'tolist'):
        return _jsonable(value.tolist())
    return value


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


def _say(text):
    print(text, flush=True)


def _refuse(lines):
    for line in lines:
        print(f'[FEHLER] {line}', file=sys.stderr, flush=True)
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description='Cut the assembly marks into the V1 arm parts from the owner STEP.')
    parser.add_argument('--step', required=True, help="the owner's assembly STEP at the URDF zero pose")
    parser.add_argument('--out', default=_DEFAULT_OUT, help='output directory (default: tools/v1_marked_parts/out)')
    parser.add_argument('--self-check', action='store_true', help='add the warning-only horn-seat check')
    parser.add_argument('--accept-placements', action='store_true',
                        help='take placements that differ from EXPECTED (only after looking at every preview)')
    args = parser.parse_args(argv)

    started = time.time()
    if not os.path.isfile(args.step):
        return _refuse([f'STEP-Datei nicht gefunden: {args.step}'])
    o = _ocp()
    _quiet(o)
    cad = _Cad(o)
    np = cad.np
    _say('Lese STEP …')
    try:
        instances = read_step(o, args.step)
    except RuntimeError:
        return _refuse([f'STEP-Datei nicht lesbar: {args.step}'])
    parts, missing = find_parts(instances)
    if missing:
        return _refuse(missing)
    frames, problems = find_frames(cad, instances, parts)
    for problem in problems:
        print(f'[FEHLER] Gelenkrahmen nicht bestimmbar: {problem}', file=sys.stderr, flush=True)

    measured = measure_pose(np, frames)
    findings = mp.pose_findings(measured)
    if findings:
        return _refuse(findings)
    _say('Nullstellung geprüft: alle drei Prüfungen an allen sechs Gelenken bestanden.')

    if args.self_check:
        for i, joint in enumerate(mp.JOINTS):
            f = frames[joint]
            top = float(max((cad.vertices(f['horn']) - f['c']) @ f['n']))
            if abs(top - mp.HORN_TOP_N[i]) > mp.HORN_TOP_TOL:
                print(f'[WARNUNG] {joint}: Hornoberseite {top:.2f} mm über der Abtriebsfläche statt '
                      f'{mp.HORN_TOP_N[i]:.2f} mm.', flush=True)

    _say('Suche die Platzierungen (etwa zwei Minuten) …')
    outcomes = {}
    unplaced = []
    for product, label, kind, joint_index in mp.MARKS:
        f = frames[mp.JOINTS[joint_index]]
        part = _Part(cad, parts[product])
        found = (search_arrow if kind == 'arrow' else search_dot)(cad, part, f, joint_index)
        if found is None:
            unplaced.append(f'Markierung {product} {label}: keine Platzierung gefunden.')
            continue
        outcomes[(product, label)] = found
        _say(f'  {product} {label}: {mp.describe(found)}')

    drift = mp.placement_findings(outcomes, mp.EXPECTED)
    if drift and not args.accept_placements:
        _refuse(drift + unplaced)
        print('Neue Platzierungen erst nach dem Ansehen aller Vorschaubilder übernehmen: '
              '--accept-placements, dann den ausgegebenen EXPECTED-Block in marks_params.py eintragen.',
              file=sys.stderr, flush=True)
        return 1
    accepted = bool(drift)
    if accepted:
        for line in drift:
            print(f'[WARNUNG] {line}', flush=True)
        print(mp.format_expected(outcomes), flush=True)

    os.makedirs(args.out, exist_ok=True)
    manifest_parts, files, failures = {}, {}, list(unplaced)
    for product, stem, _match in mp.PARTS:
        marks = [m for m in mp.MARKS if m[0] == product]
        if any((product, label) not in outcomes for _p, label, _k, _j in marks):
            failures.append(f'{product}: nicht geschnitten (eine Markierung ohne Platzierung).')
            continue
        shape, reports, part_failures, tools, v7 = cut_part(cad, product, parts[product], marks, frames, outcomes)
        entry = {'stem': stem, 'marks': reports, 'V7_part_volume': v7}
        if shape is None:
            failures += part_failures
            entry['written'] = False
            manifest_parts[product] = entry
            continue
        stem_path = os.path.join(args.out, stem)
        write_step(o, shape, stem_path + '.step')
        deflection, triangles, open_edges = write_stl(cad, shape, stem_path + '.stl')
        entry.update({'V8_watertight': open_edges == 0, 'mesh_deflection_mm': deflection, 'triangles': triangles})
        if open_edges:
            failures.append(f'{product}: V8_watertight nicht erfüllt ({open_edges} offene Kanten).')
            for suffix in ('.step', '.stl'):
                os.remove(stem_path + suffix)
            entry['written'] = False
            manifest_parts[product] = entry
            continue
        previews(cad, shape, marks, frames, outcomes, product, tools, stem_path)
        entry['written'] = True
        for suffix in ('.step', '.stl', '_marks.png', '_iso.png'):
            files[stem + suffix] = _sha256(stem_path + suffix)
        manifest_parts[product] = entry
        _say(f'  {stem}: {len(marks)} Markierung(en) geschnitten, V1–V8 erfüllt, {triangles} Dreiecke '
             f'(Durchbiegung {deflection} mm)')

    with open(args.step, 'rb') as fh:
        step_sha = hashlib.sha256(fh.read()).hexdigest()
    manifest = {
        'tool': 'tools/v1_marked_parts/build_marked_parts.py',
        'commit': _commit(),
        'servo_spec_digest': mp.fss.V1.digest,
        'input': {'path': os.path.abspath(args.step), 'sha256': step_sha, 'instances': len(instances)},
        'pose_checks': {'passed': True, 'measured': _jsonable(measured)},   # a finding refused above
        'frames': {j: {'servo': f['servo_name'], 'c': _jsonable(f['c']), 'n': _jsonable(f['n']),
                       'r': _jsonable(f['r']), 'w': _jsonable(f['w'])} for j, f in frames.items()},
        'accepted_placements': accepted,
        'parts': manifest_parts,
        'files': files,
        'seconds': round(time.time() - started, 1),
    }
    with open(os.path.join(args.out, 'marked_parts_manifest.json'), 'w', encoding='utf-8') as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True, ensure_ascii=False)
    if failures:
        return _refuse(failures)
    _say(f'Fertig: {len(manifest_parts)} Teile, {len(mp.MARKS)} Markierungen, {args.out} '
         f'({manifest["seconds"]} s).')
    return 0


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The tower guard against the meshes, the families and the clock (52 §3.6 tests 1-6, 8, 9; WP-02), and its
wiring (WP-12).

1. the box gap is a lower bound of the mesh gap; 2. every surface point lies in a box;
3. the dense count bounds the travel; 4. the allowance recomputed from the constants;
5. what cannot be judged is refused in German; 6. the ordinary families at B reproduce
``p02_gabsdir``'s counts on the uncut zone and refuse nothing on the cut zone;
8. only the wiring calls it (``legacy`` is today's code path by construction);
9. the per-leg time on ``q20``'s leg set (D72's CI budget, R5-2).
The meshes are read here with this file's OWN STL and URDF parser, not the generator's.

WP-12 (spec 40 S6.2 'Where' and 'Scope', S6.3, S6.4): every program and manual motion goes through the guard —
the publish choke point, the multi-leg routes judged before their first publish, the observation route's ladder,
replays as one polyline, the jog, the manual home glide, the joint-1 window on replays, the provider's scope
(D63) and tower pose (D65), the slow-check notice (D70) and the program's refinement warning (D42).
"""

from __future__ import annotations

import ast
import functools
import hashlib
import importlib.util
import json
import math
import os
import struct
import textwrap
import threading
import time
import types
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest

import test_fixture_guard as H
from physical_ai_server import robot_profiles as RP
from physical_ai_server.workflow import arm_model
from physical_ai_server.workflow import scene_fixture as SF
from physical_ai_server.workflow.autocal import store as STORE
from physical_ai_server.workflow.handlers import motion as M
from physical_ai_server.workflow.handlers import trajectory as TR
from physical_ai_server.workflow.ik_solver import IKSolver
from physical_ai_server.workflow.v1_ik import V1IKSolver

_PKG = Path(__file__).resolve().parents[1] / 'physical_ai_server'
_REPO = Path(__file__).resolve().parents[3]
_PUBLIC = _REPO / 'physical_ai_tools' / 'physical_ai_manager' / 'public'
_ARM_CELLS = _PKG / 'workflow' / 'arm_cells.py'
_SHA_FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'arm_cells.sha256'
_GENERATOR = _REPO / 'tools' / 'export_arm_cells.py'


def fg():
    return H.fg()


# ══════════════════════════════════════════════════════════════════════════
# the generated boxes are a fresh render of the shipped meshes
# ══════════════════════════════════════════════════════════════════════════

def test_arm_cells_is_a_fresh_render_of_the_shipped_meshes():
    spec = importlib.util.spec_from_file_location('_export_arm_cells_under_test', _GENERATOR)
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    text, digests = gen.build()
    assert _ARM_CELLS.read_text(encoding='utf-8') == text
    assert _SHA_FIXTURE.read_text(encoding='utf-8') == gen.render_fixture(text, digests)


def test_the_checksum_fixture_holds_every_input_and_the_module():
    rows = [line.split('  ', 1) for line in _SHA_FIXTURE.read_text(encoding='utf-8').splitlines()]
    paths = [p for _d, p in rows]
    assert 'physical_ai_tools/physical_ai_server/physical_ai_server/workflow/arm_cells.py' in paths
    assert any(p.endswith('v1.urdf') for p in paths) and any(p.endswith('omx_f.urdf') for p in paths)
    assert len(rows) == 2 + 4 + 7 + 1          # URDFs, the V1's 4 gripper meshes, the OMX's 7, the module
    for digest, rel in rows:
        assert hashlib.sha256((_REPO / rel).read_bytes()).hexdigest() == digest, rel


# ══════════════════════════════════════════════════════════════════════════
# the meshes (this file's own reader)
# ══════════════════════════════════════════════════════════════════════════

def _stl(path):
    data = path.read_bytes()
    n = struct.unpack('<I', data[80:84])[0]
    assert 84 + 50 * n == len(data)
    rec = np.dtype([('n', '<f4', 3), ('v', '<f4', (3, 3)), ('a', '<u2')])
    return np.frombuffer(data, dtype=rec, count=n, offset=84)['v'].astype(np.float64)


def _rpy(r, p, y):
    cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(y), math.sin(y)
    rx = np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]])
    ry = np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]])
    rz = np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]])
    return rz @ ry @ rx


def _turn(axis, a):
    k = np.asarray(axis, float) / np.linalg.norm(axis)
    kx = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + math.sin(a) * kx + (1 - math.cos(a)) * (kx @ kx)


def _tf(origin):
    t = np.eye(4)
    if origin is not None:
        t[:3, :3] = _rpy(*[float(v) for v in (origin.get('rpy') or '0 0 0').split()])
        t[:3, 3] = [float(v) for v in (origin.get('xyz') or '0 0 0').split()]
    return t


class _Model:
    """an arm's meshes as surface clouds (<= spacing apart) in each guard group's own frame, and its claw joints."""

    URDF = {'v1': _PUBLIC / 'v1-urdf' / 'v1.urdf', 'omx': _PUBLIC / 'omx-urdf' / 'omx_f.urdf'}
    GROUPS = {'v1': (('link1', ('link1',)), ('link2', ('link2',)), ('link3', ('link3',)), ('link4', ('link4',)),
                     ('housing', ('link5', 'end_effector'))),
              'omx': (('link1', ('link1',)), ('link2', ('link2',)), ('link3', ('link3',)), ('link4', ('link4',)),
                      ('housing', ('link5',)))}
    BLADES = {'v1': (('right_finger', 'RL_joint'), ('left_finger', 'LF_joint')),
              'omx': (('link6', 'gripper_joint_1'), ('link7', 'gripper_joint_2'))}

    def __init__(self, arm, spacing=0.002):
        root = ET.parse(self.URDF[arm]).getroot()
        self.dir = self.URDF[arm].parent
        self.joints = {j.get('name'): j for j in root.findall('joint')}
        self.links = {link.get('name'): link for link in root.findall('link')}
        self.sp = spacing
        self.clouds = {}
        for grp, links in self.GROUPS[arm]:
            parts = []
            for link in links:                  # the housing's links in link 5's frame (fixed joints below it)
                t = self._from_link5(link) if grp == 'housing' else np.eye(4)
                parts.append(self._cloud(self._tris(link) @ t[:3, :3].T + t[:3, 3]))
            self.clouds[grp] = np.concatenate(parts)
        self.blades = {}
        for link, jn in self.BLADES[arm]:
            j = self.joints[jn]
            mimic = j.find('mimic')
            self.blades[link] = dict(cloud=self._cloud(self._tris(link)),
                                     origin=self._from_link5(j.find('parent').get('link')) @ _tf(j.find('origin')),
                                     axis=[float(v) for v in j.find('axis').get('xyz').split()],
                                     mult=float(mimic.get('multiplier')) if mimic is not None else 1.0)

    def _from_link5(self, link):
        """link 5 -> a link hanging below it on fixed joints."""
        t = np.eye(4)
        while link != 'link5':
            joint = next(j for j in self.joints.values() if j.find('child').get('link') == link)
            assert joint.get('type') == 'fixed', link
            t = _tf(joint.find('origin')) @ t
            link = joint.find('parent').get('link')
        return t

    def _tris(self, link):
        out = []
        for v in self.links[link].findall('visual'):
            mesh = v.find('geometry/mesh')
            scale = np.array([float(x) for x in (mesh.get('scale') or '1 1 1').split()])
            t = _tf(v.find('origin'))
            tri = (_stl(self.dir / mesh.get('filename')) * scale).reshape(-1, 3) @ t[:3, :3].T + t[:3, 3]
            out.append(tri.reshape(-1, 3, 3))
        return np.concatenate(out)

    def _cloud(self, tris):
        """every vertex and, per triangle, a grid along its two edges from the vertex that makes the grid smallest,
        each edge cut into steps of at most ``spacing``: every surface point lies within about a spacing of a sample
        (a long thin triangle gets a thin grid, not a square one)."""
        tris = np.asarray(tris, float).reshape(-1, 3, 3)
        pts = [tris.reshape(-1, 3)]
        steps = []
        for k in range(3):                       # the corner the two grid edges leave from
            e1 = tris[:, (k + 1) % 3] - tris[:, k]
            e2 = tris[:, (k + 2) % 3] - tris[:, k]
            steps.append([np.maximum(1, np.ceil(np.linalg.norm(e, axis=1) / self.sp)).astype(int) for e in (e1, e2)])
        m1s, m2s = np.stack([a for a, _b in steps], 1), np.stack([b for _a, b in steps], 1)
        corner = np.argmin(m1s * m2s, axis=1)
        rows = np.arange(len(tris))
        m1, m2 = m1s[rows, corner], m2s[rows, corner]
        origin = tris[rows, corner]
        e1 = tris[rows, (corner + 1) % 3] - origin
        e2 = tris[rows, (corner + 2) % 3] - origin
        for a, b in sorted(set(zip(m1.tolist(), m2.tolist()))):
            sel = (m1 == a) & (m2 == b)
            i, j = np.meshgrid(np.arange(a + 1), np.arange(b + 1), indexing='ij')
            keep = (i / a + j / b) <= 1.0 + 1e-12
            s, t = (i[keep] / a)[None, :, None], (j[keep] / b)[None, :, None]
            pts.append((origin[sel][:, None] + s * e1[sel][:, None] + t * e2[sel][:, None]).reshape(-1, 3))
        return np.concatenate(pts)

    def blade_tf(self, link, claw, mult=None):
        b = self.blades[link]
        t = b['origin'].copy()
        t[:3, :3] = t[:3, :3] @ _turn(b['axis'], (b['mult'] if mult is None else mult) * claw)
        return t

    def world(self, frame, claw, ratio_one=False):
        """{group: points in W} for one pose's guard frames (5, 4, 4) and one claw value."""
        out = {}
        for k, grp in enumerate(('link1', 'link2', 'link3', 'link4', 'housing')):
            f = frame[k]
            out[grp] = np.einsum('pj,ij->pi', self.clouds[grp], f[:3, :3]) + f[:3, 3]
        for link, b in self.blades.items():
            mult = math.copysign(1.0, b['mult']) if ratio_one else None
            t = frame[4] @ self.blade_tf(link, claw, mult)
            out[link] = np.einsum('pj,ij->pi', b['cloud'], t[:3, :3]) + t[:3, 3]
        return out


@functools.lru_cache(maxsize=None)
def model(arm):
    return _Model(arm)


def _obb_distance(points, part):
    loc = np.einsum('pi,ij->pj', points - part.centre, part.axes)
    d = np.maximum(np.abs(loc) - part.half, 0.0)
    return np.sqrt((d * d).sum(1))


def _inside(points, corners, axes, tol=1e-9):
    centre = corners.mean(0)
    half = np.abs((corners - centre) @ axes).max(0)
    return (np.abs(np.einsum('pi,ij->pj', points - centre, axes)) <= half + tol).all(1)


# ══════════════════════════════════════════════════════════════════════════
# 1. the box gap is a lower bound of the mesh gap
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('arm', ['v1', 'omx'])
def test_box_gap_is_a_lower_bound_of_the_mesh_gap(arm):
    """200 seeded random poses per arm and claw value: the smallest _box_gap over the arm's boxes (level 2) to a
    TOWER_OBB_F part is <= the mesh's distance to it (2 mm cloud of the shipped URDF meshes)."""
    F, m = fg(), model(arm)
    g = H.guard(arm)
    rng = np.random.default_rng(101 + (arm == 'omx'))
    lim = np.array(H.solver(arm).joint_limits)
    lo, hi = H.CLAW_WINDOW[arm]
    Q = rng.uniform(lim[:, 0], lim[:, 1], (200, 5))
    claws = rng.uniform(lo, hi, 200)
    frames = H.nominal_frames(arm)(Q)
    u = np.array([[0.0, 0.0, 1.0]])
    compared = 0
    for i in range(200):
        boxes = F.arm_boxes(arm, claws[i], claws[i], level=2)
        CA, RA = F.world_boxes(frames[i:i + 1], boxes)
        clouds = m.world(frames[i], claws[i])
        if arm == 'v1':
            clouds['left_finger@1'] = m.world(frames[i], claws[i], ratio_one=True)['left_finger']
        bounds = {grp: (pts.min(0), pts.max(0)) for grp, pts in clouds.items()}
        for part in g.parts:
            gap = float(F.gap_dir(CA[0], RA[0], np.broadcast_to(part.corners, CA[0].shape),
                                  np.broadcast_to(part.axes, RA[0].shape), np.zeros(len(CA[0])),
                                  np.broadcast_to(u, (len(CA[0]), 3))).min())
            for grp, pts in clouds.items():
                p_lo, p_hi = bounds[grp]
                if np.maximum(part.lo - p_hi, p_lo - part.hi).max() >= gap:
                    continue                            # every point of the group lies at least that far away
                assert gap <= float(_obb_distance(pts, part).min()) + 1e-9, (i, part.name, grp)
                compared += 1
    assert compared > 200


# ══════════════════════════════════════════════════════════════════════════
# 2. every surface point lies in a box
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('arm', ['v1', 'omx'])
def test_every_surface_point_is_inside_a_box(arm):
    """60 seeded poses per arm, 2 mm surface cloud: 0 points outside every arm box (level 2; a claw interval's true
    blade anywhere inside it, the V1's mimic blade at the URDF's 18/19 AND at ratio 1), and every level-2 cell
    inside its level-1 box."""
    F, m = fg(), model(arm)
    rng = np.random.default_rng(202 + (arm == 'omx'))
    lim = np.array(H.solver(arm).joint_limits)
    lo, hi = H.CLAW_WINDOW[arm]
    checked = 0
    for _ in range(60):
        q = rng.uniform(lim[:, 0], lim[:, 1])
        a, b = np.sort(rng.uniform(lo, hi, 2))
        if rng.random() < 0.3:
            b = a
        claw = rng.uniform(a, b)
        frame = H.nominal_frames(arm)(q[None])
        b2 = F.arm_boxes(arm, a, b, level=2)
        b1 = F.arm_boxes(arm, a, b, level=1)
        C2, R2 = F.world_boxes(frame, b2)
        C1, R1 = F.world_boxes(frame, b1)
        for ci, grp in enumerate(b2.group):
            k = list(b1.group).index(grp)
            assert _inside(C2[0, ci], C1[0, k], R1[0, k]).all(), grp
        clouds = m.world(frame[0], claw)
        if arm == 'v1':
            clouds['left_finger@1'] = m.world(frame[0], claw, ratio_one=True)['left_finger']
        for grp, pts in clouds.items():
            name = grp.split('@')[0]
            inside = np.zeros(len(pts), bool)
            for ci in [i for i, gg in enumerate(b2.group) if gg == name]:
                rest = np.nonzero(~inside)[0]
                inside[rest] = _inside(pts[rest], C2[0, ci], R2[0, ci])
            assert inside.all(), (grp, int((~inside).sum()))
            checked += len(pts)
    assert checked > 500000


# ══════════════════════════════════════════════════════════════════════════
# 3. the dense count bounds the travel
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('arm', ['v1', 'omx'])
def test_dense_count_bounds_travel(arm):
    """500 random legs: the largest arm-box corner travel between consecutive dense samples is <= 3 mm."""
    F = fg()
    rng = np.random.default_rng(303 + (arm == 'omx'))
    lim = np.array(H.solver(arm).joint_limits)
    lo, hi = H.CLAW_WINDOW[arm]
    worst = 0.0
    for _ in range(500):
        a, b = rng.uniform(lim[:, 0], lim[:, 1], (2, 5))
        if rng.random() < 0.5:
            b = a + rng.uniform(-0.3, 0.3, 5)
        g = rng.uniform(lo, hi)
        Q = F.dense_samples(arm, a, b)
        assert len(Q) == F.dense_count(arm, a, b) + 1
        CA, _ = F.world_boxes(H.nominal_frames(arm)(Q), F.arm_boxes(arm, g, g, level=1))
        worst = max(worst, float(np.linalg.norm(np.diff(CA, axis=0), axis=-1).max()))
    assert worst <= 2 * SF.TOWER_SAMPLE_M + 1e-12, worst


# ══════════════════════════════════════════════════════════════════════════
# 4. the allowance terms
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('arm', ['v1', 'omx'])
@pytest.mark.parametrize('calibrated', [True, False])
def test_allowance_terms(arm, calibrated):
    """S6.2 revision 4's allowance recomputed from the constants at five heights per part and three deltas,
    equal to 1e-9: R = axis + build + sample + residual [+ rotation for the top parts];
    allowance = R + c * min(z_top(p), (max(z_box, 0) + R + delta) / (1 - c)), c = TOWER_LEAN_AT_LENS_M / LENS_F.z."""
    g = H.guard(arm, calibrated)
    res = (SF.TOWER_RESIDUAL_SERVER_M if calibrated else SF.TOWER_RESIDUAL_UNCALIBRATED_M)[arm]
    assert g.residual_m == res
    c = SF.TOWER_LEAN_AT_LENS_M / SF.LENS_F[2]
    for k, part in enumerate(g.parts):
        z_top = max(z for _x, _y, z in part.corners)
        assert part.z_top == z_top
        for with_sample in (True, False):
            R = (SF.TOWER_AXIS_M + SF.TOWER_BUILD_M + (SF.TOWER_SAMPLE_M if with_sample else 0.0) + res
                 + (SF.TOWER_TOP_ROTATION_M if part.name in SF.TOWER_TOP_PARTS else 0.0))
            for z_box in (-0.02, 0.0, 0.05, 0.3, 0.75):
                for delta in (0.0, 0.004, 0.02):
                    want = R + c * min(z_top, (max(z_box, 0.0) + R + delta) / (1.0 - c))
                    got = float(g.allowance(k, np.array([z_box]), np.array([delta]), with_sample=with_sample)[0])
                    assert got == pytest.approx(want, abs=1e-9), (part.name, z_box, delta, with_sample)


# ══════════════════════════════════════════════════════════════════════════
# 5. what cannot be judged is refused in German
# ══════════════════════════════════════════════════════════════════════════

def test_unjudgeable_refuses_in_german():
    """a NaN joint, a missing part, an unknown profile under tower_v1, a contract-hash mismatch: refused with
    TOWER_UNJUDGEABLE_DE, never passed."""
    F = fg()
    a, b = H.HOME['v1'], H.OBS['v1']
    frames = H.nominal_frames('v1')
    good = F.make_guard('v1_studio', frames, base_axis_x=0.0)
    assert not good.judge_leg(a, b, 0.9, 0.9).refused
    nan = a.copy()
    nan[2] = float('nan')
    bad_guards = (
        F.make_guard('v1_studio', frames, base_axis_x=0.0,
                     tower=tuple(p for p in SF.tower_obbs_w(0.0) if p[0] != 'column')),
        F.make_guard('v1_studio', frames, base_axis_x=0.0, tower=SF.tower_obbs_w(0.0) + SF.tower_obbs_w(0.0)[:1]),
        F.make_guard('v1_studio', frames, base_axis_x=0.0,
                     tower=tuple((n, c, r, (h[0], -1.0, h[2])) for n, c, r, h in SF.tower_obbs_w(0.0))),
        F.make_guard('edu6_studio', frames, base_axis_x=0.0),
        F.make_guard(None, frames, base_axis_x=0.0),
        F.make_guard('v1_studio', frames, base_axis_x=0.0, contract_hash='0' * 64),
        F.make_guard('v1_studio', frames, base_axis_x=float('nan')),
        F.make_guard('v1_studio', frames, base_axis_x=0.0, cells={}),
        F.make_guard('v1_studio', lambda Q: np.full((len(Q), 5, 4, 4), np.nan), base_axis_x=0.0),
        F.make_guard('v1_studio', lambda Q: 1 / 0, base_axis_x=0.0),
    )
    for g in bad_guards:
        for v in (g.judge_leg(a, b, 0.9, 0.9), g.judge_pose(a, 0.9), g.dense_leg(a, b, 0.9, 0.9),
                  g.judge_polyline(np.stack([a, b]), 0.9)):
            assert v.refused and v.reason == 'unjudgeable', g
            assert v.sentence_de == F.TOWER_UNJUDGEABLE_DE
    for leg in ((nan, b, 0.9, 0.9), (a, b, float('nan'), 0.9), (a, b, 0.9, float('inf')), (a[:4], b, 0.9, 0.9),
                (a, 'x', 0.9, 0.9)):
        v = good.judge_leg(*leg)
        assert v.refused and v.sentence_de == F.TOWER_UNJUDGEABLE_DE, leg
    assert good.judge_pose(nan, 0.9).sentence_de == F.TOWER_UNJUDGEABLE_DE
    assert good.judge_polyline(np.stack([a, nan]), 0.9).sentence_de == F.TOWER_UNJUDGEABLE_DE
    assert F.TOWER_UNJUDGEABLE_DE == ('Die Bewegung wird nicht ausgeführt: der Abstand zum Kameraturm kann gerade '
                                      'nicht geprüft werden. Bitte die Umgebung neu starten.')
    assert F.TOWER_TOO_CLOSE_DE == ('Diese Bewegung käme dem Kameraturm zu nahe – bitte ein Ziel weiter vorne oder '
                                    'weiter vom Turm entfernt wählen.')
    assert F.TOWER_DROOP_UNJUDGEABLE_DE == (
        'Dieser Arm hängt zu stark durch, um den Abstand zum Kameraturm sicher zu prüfen – die Bewegung wird nicht '
        'ausgeführt. Bitte den Arm prüfen lassen; der Arm-Check meldet das Durchhängen.')


# ══════════════════════════════════════════════════════════════════════════
# 6. the ordinary families at B
# ══════════════════════════════════════════════════════════════════════════

def _zone_grid(arm, rolls, cut):
    """p02's zone grid: 8 radii x 19 azimuths over the sector, the given tool rolls, grasp at MAT + 15 mm and its
    hover 60 mm above; ``cut`` keeps only points in_zone (the printed zone of revision 4)."""
    sp = H.SPEC[arm]
    zt = SF.MAT_THICKNESS_M + 0.015
    out = []
    az_max = sp['zone_sector_deg'][1]
    for r in np.linspace(sp['zone_inner_m'], sp['zone_outer_m'], 8):
        for az in np.linspace(-az_max, az_max, 19):
            x, y = r * math.cos(math.radians(az)), r * math.sin(math.radians(az))
            if cut and not SF.in_zone(sp, x, y):
                continue
            for ro in rolls:
                q, qh = H.tip_solve(arm, x, y, zt, ro), H.tip_solve(arm, x, y, zt + 0.060, ro)
                if q is not None and qh is not None:
                    out.append((q, qh))
    return out


def _count(g, arm, legs):
    """how many legs ``(a, b, gripper)`` the dense rule refuses (H.leg_minima, batched per gripper value)."""
    n = 0
    for s in sorted({s for _a, _b, s in legs}):
        n += int(np.sum(H.leg_minima(g, arm, [(a, b) for a, b, ss in legs if ss == s], s) < 0.0))
    return n


# (zone grasps open/closing, grasp <-> hover open/holding, OBS <-> hover direct open/holding) on the UNCUT zone
UNCUT = {'v1': (1216, 48, 50, 88), 'omx': (1216, 87, 83, 0)}
CUT_LEGS = {'v1': 1350, 'omx': 2112}
CUT_OBS_DIRECT = {'v1': 110, 'omx': 0}


@pytest.mark.parametrize('arm', ['v1', 'omx'])
def test_ordinary_families_at_b(arm):
    """p02_gabsdir's counts with the seeded populations on the UNCUT zone (V1 zone grasps 48, grasp <-> hover 50,
    OBS direct 88; OMX 87 / 83 / 0), and on the CUT zone with the full roll sets no refusal in any zone family
    (p02_gabscutfull; the V1's 110 refused direct observation lines all pass via HOME)."""
    g = H.guard(arm)
    gr, home, obs = H.GR[arm], H.HOME[arm], H.OBS[arm]
    rolls = ([0.0, math.pi / 4, math.pi / 2, -math.pi / 4] if arm == 'v1'
             else [0.0, math.pi / 4, math.pi / 2, 3 * math.pi / 4])
    grid = _zone_grid(arm, rolls, cut=False)
    n_legs, n_grasp, n_gh, n_obs = UNCUT[arm]
    assert 2 * len(grid) == n_legs
    assert _count(g, arm, [(q, q, s) for q, _h in grid for s in (gr['open'], gr['closed'])]) == n_grasp
    assert _count(g, arm, [(q, h, s) for q, h in grid for s in (gr['open'], gr['holding'])]) == n_gh
    assert _count(g, arm, [(obs, h, s) for _q, h in grid for s in (gr['open'], gr['holding'])]) == n_obs

    rolls = ([-math.pi / 2, -math.pi / 4, 0.0, math.pi / 4, math.pi / 2] if arm == 'v1'
             else [k * math.pi / 4 for k in range(8)])
    grid = _zone_grid(arm, rolls, cut=True)
    assert 2 * len(grid) == CUT_LEGS[arm]
    both = (gr['open'], gr['holding'])
    assert _count(g, arm, [(q, q, s) for q, _h in grid for s in (gr['open'], gr['closed'])]) == 0
    assert _count(g, arm, [(q, h, s) for q, h in grid for s in both]) == 0
    assert _count(g, arm, [(h, home, s) for _q, h in grid for s in both]) == 0
    direct = [(h, s) for s in both
              for (_q, h), m in zip(grid, H.leg_minima(g, arm, [(obs, h) for _q, h in grid], s)) if m < 0.0]
    assert len(direct) == CUT_OBS_DIRECT[arm]
    assert not any(g.judge_leg(obs, home, s, s).refused or g.judge_leg(home, h, s, s).refused for h, s in direct)
    rh = np.random.default_rng(4242 + (0 if arm == 'v1' else 1))
    hovers = [h for _q, h in grid]
    pairs = []
    for _ in range(1500):
        i1, i2 = rh.integers(0, len(hovers)), rh.integers(0, len(hovers))
        if i1 != i2:
            pairs.append((hovers[i1], hovers[i2], gr['holding']))
    assert len(pairs) >= 1495
    assert _count(g, arm, pairs) == 0


# ══════════════════════════════════════════════════════════════════════════
# 8. only the wiring calls the guard (legacy = today's code path)
# ══════════════════════════════════════════════════════════════════════════

def test_only_the_wiring_imports_the_guard():
    """WP-02 shipped the pure half with no caller; WP-12 wires it: the motion handlers (the one publish choke point
    and the multi-leg routes), the manager (the run's guard, the pre-check) and the node (the provider, the manual
    paths). Nothing else imports fixture_guard or arm_cells, so a ``legacy`` run, which gets no guard, takes today's
    code path in every other module by construction."""
    importers = {}
    for path in _PKG.rglob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or '') + '.' + a.name for a in node.names] + [node.module or '']
            for n in names:
                for mod in ('fixture_guard', 'arm_cells', 'scene_fixture'):
                    if n.endswith(mod) or ('.' + mod + '.') in n:
                        importers.setdefault(mod, set()).add(path.relative_to(_PKG).as_posix())
    assert importers.get('fixture_guard', set()) == {
        'workflow/handlers/motion.py', 'workflow/workflow_manager.py', 'physical_ai_server.py'}
    assert importers.get('arm_cells', set()) == {'workflow/fixture_guard.py'}
    # WP-10's calibration core reads the contract too (the tower pose, the lens it carries, the hash); pure
    # modules on no motion path. WP-11's arm model (lift, mat, fingertip, joint-1 window) and the node's
    # calibration dict (the mat's plane) read its constants; neither calls the guard. WP-13's calibration
    # service reads the arm-check poses, the zone and the outside window; its guard_margin gate gets the guard
    # from the node. WP-15's handheld step reads the target plate and the lens band; it moves nothing.
    assert importers.get('scene_fixture', set()) == {
        'workflow/fixture_guard.py', 'workflow/object_catalog.py', 'workflow/autocal/arm_check.py',
        'workflow/autocal/mat_check.py', 'workflow/autocal/store.py', 'workflow/autocal/tags.py',
        'workflow/autocal/service.py', 'workflow/autocal/handheld.py', 'workflow/arm_model.py',
        'physical_ai_server.py'}


def test_the_guard_imports_only_numpy_arm_geometry_arm_cells_and_scene_fixture():
    tree = ast.parse((_PKG / 'workflow' / 'fixture_guard.py').read_text(encoding='utf-8'))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mods.update(f'{node.module}.{a.name}' if node.module == 'physical_ai_server.workflow' else node.module
                        for a in node.names)
    assert mods <= {'__future__', 'math', 'dataclasses', 'typing', 'collections', 'numpy',
                    'physical_ai_server.workflow.arm_geometry', 'physical_ai_server.workflow.arm_cells',
                    'physical_ai_server.workflow.scene_fixture'}, mods
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in ('open', 'eval', 'exec', 'input'), node.func.id
        if isinstance(node, ast.Attribute):
            assert node.attr not in ('environ', 'getenv'), node.attr
    F = fg()
    assert F.TOWER_COARSE_M == 0.030
    assert F.DENSE_CHUNK == 4096
    assert F.DENSE_STEP_M == 0.003
    assert F.TILT_SLACK == 0.03
    assert F.DROOP_SUM_LIMIT == 0.5
    assert F.GRAVITY_M_S2 == 9.81
    assert F.R_J_M == {'v1': (0.589, 0.590, 0.432, 0.209, 0.114), 'omx': (0.430, 0.433, 0.313, 0.151, 0.086)}


# ══════════════════════════════════════════════════════════════════════════
# 9. the per-leg time on q20's leg set (D72's CI budget, R5-2)
# ══════════════════════════════════════════════════════════════════════════

def test_runtime_budget_here():
    """q20's seeded leg set (both arms; zone grasp <-> hover, hover <-> HOME, OBS <-> hover direct, jog steps near
    the tower, student program legs; 200 + 200 legs), each judged at BOTH residuals (calibrated: the server residual
    with the design envelope standing in for the kit's; uncalibrated: the uncalibrated residual at the contract
    place), process time per leg through the travel-bound rule: p99 <= 50 ms (D72; never loosened by a coder, R31).
    The runner's own times are printed first."""
    times, report = [], []
    for arm in ('v1', 'omx'):
        legs = H.q20_legs(arm)
        assert len(legs) == 200
        for calibrated in (True, False):
            g = H.guard(arm, calibrated)
            fam, a, b, s = legs[0]
            g.judge_leg(a, b, s, s)                         # warm the per-interval box cache
            t = []
            for fam, a, b, s in legs:
                t0 = time.process_time()
                g.judge_leg(a, b, s, s)
                t.append(time.process_time() - t0)
            t = np.array(t) * 1000.0
            times.append(t)
            report.append(f'{arm} {"calibrated" if calibrated else "uncalibrated"}: median {np.median(t):.1f} '
                          f'p99 {np.percentile(t, 99):.1f} max {t.max():.1f} ms')
    allt = np.concatenate(times)
    print('tower guard per-leg time (q20, travel-bound rule): ' + '; '.join(report)
          + f'; all {len(allt)} legs p99 {np.percentile(allt, 99):.1f} ms')
    assert len(allt) == 800
    assert np.percentile(allt, 99) <= 50.0, report


# ══════════════════════════════════════════════════════════════════════════
# WP-12: the wiring (spec 40 S6.2 'Where' and 'Scope', S6.3, S6.4; D34, D42, D57, D63, D65, D70, D72)
# ══════════════════════════════════════════════════════════════════════════
# The sentences are written out here, so a test that runs before the wiring exists fails on what the wiring must
# do (it refuses, it says so), never on a missing name.
_TOO_CLOSE_DE = ('Diese Bewegung käme dem Kameraturm zu nahe – bitte ein Ziel weiter vorne oder weiter vom Turm '
                 'entfernt wählen.')
_UNJUDGEABLE_DE = ('Die Bewegung wird nicht ausgeführt: der Abstand zum Kameraturm kann gerade nicht geprüft '
                   'werden. Bitte die Umgebung neu starten.')
_JOINT1_WINDOW_DE = 'Diese Bewegung dreht den Arm zu weit nach hinten zum Kameraturm.'
_CHECK_SLOW_DE = 'Die Bewegung wird auf Abstand zum Kameraturm geprüft – einen Moment …'
_HOME_REFUSED_DE = ('Von hier würde die Fahrt in die Grundstellung dem Kameraturm zu nahe kommen. Bring den Arm erst '
                    'mit „Steuern" nach vorne – dann geht die Grundstellung.')
_NODE_PY = _PKG / 'physical_ai_server.py'
_NODE_SOURCE = _NODE_PY.read_text(encoding='utf-8')
PROFILES = {'v1': RP.ROBOT_PROFILES['v1_studio'], 'omx': RP.ROBOT_PROFILES['omx_full']}
TEETH = {'v1': (1.08, 0.14, 2.79, -1.01, 0.0), 'omx': (-1.08, -1.48, -1.41, -0.3, 0.0)}


def _base_solver(arm):
    return V1IKSolver() if arm == 'v1' else IKSolver()


def _arm_ik(arm):
    """the REAL robot's arm model with nothing calibrated (the node's ``_build_ik_solver`` before an arm check)."""
    return arm_model.for_robot(_base_solver(arm), H.PROFILE[arm], None)


def _guard_on(arm, ik, calibrated=True, pose=(0.0, 0.0, 0.0)):
    """the tower guard on ``ik``'s frames: a valid arm check with the design envelope, or none."""
    check = {'tower_pose': pose, 'droop_envelope_rad': H.DESIGN_ENV[arm]} if calibrated else None
    g = fg().make_guard(H.PROFILE[arm], ik.guard_frames, base_axis_x=ik.base_axis_x, arm_check=check,
                        contract_hash=SF.contract_hash())
    assert isinstance(g, fg().TowerGuard), g
    return g


class _Spy:
    """a guard that counts every call it forwards."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    def __getattr__(self, name):
        attr = getattr(self.inner, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            self.calls += 1
            return attr(*args, **kwargs)
        return call


class _Scripted:
    """a guard that refuses exactly the legs ``refuse(k, a, b)`` names (k = the 1-based count of judged legs)."""

    def __init__(self, refuse=lambda k, a, b: False):
        self.refuse = refuse
        self.legs = 0

    def judge_leg(self, a, b, _ga, _gb):
        self.legs += 1
        if self.refuse(self.legs, a, b):
            return fg().Verdict(True, 'too_close', fg().TOWER_TOO_CLOSE_DE, -0.001, 'column')
        return fg().Verdict(False, 'clear')

    def judge_polyline(self, _w, _g):
        return fg().Verdict(False, 'clear')


class _Run:
    """enough of a WorkflowContext for the motion and replay handlers; what is published is recorded."""

    def __init__(self, arm, start, guard=None, ik=None):
        profile = PROFILES[arm]
        self.ik = ik if ik is not None else _arm_ik(arm)
        if guard is not None:
            self.fixture_guard = guard
        self.zones = []
        self.z_table = SF.MAT_THICKNESS_M + SF.FINGERTIP_BELOW_EE_M[arm]
        self.board_table_z = SF.MAT_THICKNESS_M
        self.table_plane = None
        self.tempo = 1.0
        self.motion_lock = None
        self.published = []
        self.logs = []
        self.log = self.logs.append
        self.should_stop = lambda: False
        self.publisher = lambda pts: self.published.append(
            [[[round(float(v), 12) for v in p[0]], round(float(p[1]), 12)] for p in pts])
        self.last_full_joints = [float(v) for v in start]
        self.last_arm_joints = [float(v) for v in start[:5]]
        self.num_arm_joints = profile.num_arm_joints
        self.roll_joint_index = profile.roll_joint_index
        self.home_joints_rad = profile.home_joints_rad
        self.observe_pose_joints = tuple(float(v) for v in H.OBS[arm])
        self.gripper_open_rad = profile.gripper_open_rad
        self.gripper_closed_rad = profile.gripper_closed_rad
        self.velocity_limit_rad_s = profile.velocity_limit_rad_s
        self.destinations = {}
        self.trajectories = {}


def _fast_publish(publisher, points, should_stop, **_kw):
    """chunked_publish without the real-time pacing: the whole stream in one call."""
    pts = list(points)
    if pts:
        publisher(pts)
    return True


def _hover(arm, r, az, extra=0.0, roll=0.0):
    return H.tip_solve(arm, r * math.cos(math.radians(az)), r * math.sin(math.radians(az)),
                       SF.MAT_THICKNESS_M + 0.075 + extra, roll)


# a start whose way to HOME is two legs (p06: the V1's box ladder lifts over a via, the OMX's point-model rung)
VIA_START = {'v1': [-0.599, 0.269, 0.313, -1.161, 0.957], 'omx': [0.3154, 0.643, 0.9016, 1.5497, 0.4958]}


def _five_sequences(arm, guard):
    """The five callers of the spec's no-provider line — home, go_to_observation_pose, safe_move,
    _move_to_position, replay_trajectory — each from the same start, with ``guard`` (None: no guard): what each
    publishes, in order."""
    gr = H.GR[arm]
    hov_a, hov_b = list(_hover(arm, 0.18, 20.0)), list(_hover(arm, 0.22, -35.0))
    grasp = list(H.tip_solve(arm, 0.2 * math.cos(0.3), 0.2 * math.sin(0.3), SF.MAT_THICKNESS_M + 0.015, 0.0))
    home = [float(v) for v in H.HOME[arm]]
    out = {}

    def run(label, start, fn):
        ctx = _Run(arm, start, guard=guard)
        fn(ctx)
        out[label] = ctx.published

    run('home', hov_a + [gr['open']], lambda c: M.home(c, {}))
    run('home_via', VIA_START[arm] + [gr['holding']], lambda c: M.home(c, {}))
    run('observe', hov_a + [gr['holding']], M.go_to_observation_pose)
    run('safe_move', hov_a + [gr['open']],
        lambda c: M.safe_move(c, hov_a + [gr['open']], hov_b + [gr['open']], M.DEFAULT_MOVE_DURATION_S))
    run('position', home + [gr['open']], lambda c: M._move_to_position(c, 'P', grasp, None))

    def replay(c):
        c.trajectories = {'Take': {'fps': 25, 'points': [hov_a + [gr['open'], 0.0], hov_b + [gr['open'], 1.5],
                                                         grasp + [gr['open'], 3.0]]}}
        TR.replay_trajectory(c, {'name': 'Take'})
    run('replay', home + [gr['open']], replay)
    return out


def _digest(sequences):
    return hashlib.sha256(json.dumps(sequences, sort_keys=True).encode('utf-8')).hexdigest()


# What the five callers published at the parent commit cc5bfd69 (before the wiring), computed by running
# _five_sequences there with guard=None (scratch/build-WP-12/probes/p04_parent_sequences.py).
TODAYS_SEQUENCES_SHA256 = {'v1': '0be782b28fec836491624c0539cc4d69ce8be83a657292cdc5a78f7d2ed51dcf',
                           'omx': '7d87a5d3ee05f1c4241ec4f70dc2522d5f12699c4aea190146c67fb6fe871d8e'}


@pytest.mark.parametrize('arm', ['v1', 'omx'])
def test_wiring_without_a_guard_every_caller_publishes_todays_sequence_and_a_clearing_guard_changes_nothing(
        arm, monkeypatch):
    """FAILS FIRST: without a provider nothing is checked and home, go_to_observation_pose, safe_move,
    _move_to_position and replay_trajectory publish exactly today's sequences (digest of the parent's); a guard
    that clears everything is CONSULTED by each of the five and changes nothing they publish."""
    monkeypatch.setattr(M, 'chunked_publish', _fast_publish)
    monkeypatch.setattr(TR, 'chunked_publish', _fast_publish)
    today = _five_sequences(arm, None)
    assert _digest(today) == TODAYS_SEQUENCES_SHA256[arm]
    for label in today:
        spy = _Spy(_Scripted())
        got = _five_sequences(arm, spy)[label]
        assert got == today[label], label
        assert spy.calls > 0, label


@pytest.mark.parametrize('arm', ['v1', 'omx'])
def test_wiring_a_position_at_the_column_is_refused_and_nothing_moves(arm):
    """FAILS FIRST (r5): with the provider, the Position V1 (1.08, 0.14, 2.79, -1.01, 0) / OMX (-1.08, -1.48,
    -1.41, -0.3, 0) is refused with TOWER_TOO_CLOSE_DE and nothing is published (r01: refused at the column)."""
    ik = _arm_ik(arm)
    for gripper in (H.GR[arm]['open'], H.GR[arm]['closed']):
        ctx = _Run(arm, list(H.HOME[arm]) + [gripper], guard=_guard_on(arm, ik), ik=ik)
        with pytest.raises(M.WorkflowError) as excinfo:
            M._move_to_position(ctx, 'Turm', list(TEETH[arm]), None)
        assert str(excinfo.value) == _TOO_CLOSE_DE
        assert ctx.published == [] and ctx.logs == []


def _wall_route():
    """test_path_guard's low wall as the student's only zone, on the OMX arm model: start, end and the wall."""
    ik = _arm_ik('omx')
    wall = [{'min': [0.06, -0.03, 0.0], 'max': [0.22, 0.03, 0.05]}]
    qs = list(ik.solve((0.14, -0.12, 0.03), roll=M.GRASP_ROLL_RAD)) + [M.GRIPPER_OPEN_RAD]
    qe = list(ik.solve((0.14, 0.12, 0.03), roll=M.GRASP_ROLL_RAD)) + [M.GRIPPER_OPEN_RAD]
    return ik, wall, qs, qe


def test_wiring_a_route_whose_third_leg_is_refused_publishes_nothing(monkeypatch):
    """FAILS FIRST (r5): safe_move around the low wall plans three legs (lift, travel, descend); a guard that
    refuses the third publishes nothing at all — every leg is judged before the first publish."""
    monkeypatch.setattr(M, 'chunked_publish', _fast_publish)
    ik, wall, qs, qe = _wall_route()
    clear = _Scripted()
    ctx = _Run('omx', qs, guard=clear, ik=ik)
    ctx.zones = wall
    M.safe_move(ctx, qs, qe, 2.5)
    assert clear.legs == 3 and len(ctx.published) == 3
    third = _Scripted(lambda k, _a, _b: k == 3)
    ctx = _Run('omx', qs, guard=third, ik=ik)
    ctx.zones = wall
    with pytest.raises(M.WorkflowError) as excinfo:
        M.safe_move(ctx, qs, qe, 2.5)
    assert str(excinfo.value) == _TOO_CLOSE_DE
    assert third.legs == 3 and ctx.published == []


def test_wiring_the_low_wall_stays_the_students_only_zone_with_the_provider(monkeypatch):
    """FAILS FIRST: the low wall of test_plan_safe_route_lift_and_travel as the student's zone, with the provider:
    the route is still three legs (the tower guard judges them, never reroutes) and ctx.zones is exactly the
    payload's list."""
    from physical_ai_server.workflow.workflow_manager import WorkflowManager
    monkeypatch.setattr(M, 'chunked_publish', _fast_publish)
    ik, wall, qs, qe = _wall_route()
    guard = _Spy(_guard_on('omx', ik))
    captured = []
    wm = WorkflowManager(publisher=lambda pts: None, ik_factory=lambda: ik, get_follower_joints=lambda: list(qs),
                         arm_profile=PROFILES['omx'], fixture_guard_provider=lambda: guard)
    wm._run = lambda interpreter, ctx: captured.append(ctx)
    payload = {'blocks': {'languageVersion': 0, 'blocks': []}, 'zones': wall}
    ok, msg, _warnings = wm.start(json.dumps(payload), 'wand')
    assert ok, msg
    wm._thread.join(5.0)
    ctx = captured[0]
    assert ctx.zones == wall and ctx.fixture_guard is guard
    legs = []
    real = M._publish_motion_t
    monkeypatch.setattr(M, '_publish_motion_t', lambda c, a, b, d, t: (legs.append((a, b)), real(c, a, b, d, t)))
    M.safe_move(ctx, qs, qe, 2.5)
    assert len(legs) == 3 and guard.calls >= 3
    assert legs[-1][1] == pytest.approx(qe)
    assert ctx.zones == wall


def _node_fns(*names, extra=None):
    """node methods exec'd from the shipped source (physical_ai_server.py cannot be imported without rclpy)."""
    ns = {'math': math, 'os': os, 'json': json, 'threading': threading, 'time': time, 'robot_profiles': RP}
    ns.update(extra or {})
    found = {}
    for node in ast.walk(ast.parse(_NODE_SOURCE)):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            exec(compile(textwrap.dedent(ast.get_source_segment(_NODE_SOURCE, node)), str(_NODE_PY), 'exec'), ns)
            found[node.name] = ns[node.name]
    return found


class _Camera:
    def __init__(self, profile):
        self._profile = profile

    def profile(self):
        return self._profile


class _Logger:
    def __init__(self):
        self.lines = []

    def warning(self, msg, *a, **k):
        self.lines.append(('warning', str(msg)))

    def error(self, msg, *a, **k):
        self.lines.append(('error', str(msg)))

    def info(self, msg, *a, **k):
        self.lines.append(('info', str(msg)))


class _GuardNode:
    """a stub node with the real ``_fixture_guard`` (when the source has it), the jog target computation and the
    real arm models, for the scope and the manual-path tests."""

    def __init__(self, arm, scene_profile, calib_dir):
        self._arm_profile = PROFILES[arm]
        self.studio_camera = _Camera(scene_profile)
        self.logger = _Logger()
        self._calib_dir = calib_dir
        fns = _node_fns('_fixture_guard', '_compute_jog_target', '_jog_solve_floor', '_build_profile_ik',
                        '_build_ik_solver', '_build_sim_ik_solver')
        for name, fn in fns.items():
            setattr(self, name, types.MethodType(fn, self))

    def has_parameter(self, _name):
        return False

    def get_logger(self):
        return self.logger

    def _load_workflow_calibration(self):
        arm = SF.arm_key(self._arm_profile.profile_id)
        return {'z_table': SF.MAT_THICKNESS_M + SF.FINGERTIP_BELOW_EE_M[arm], 'board_table_z': SF.MAT_THICKNESS_M,
                'table_plane': None}

    def jog_joint(self, start, index, delta):
        request = types.SimpleNamespace(mode='joint', index=index, delta=delta, target_x=0.0, target_y=0.0,
                                        target_z=0.0)
        return self._compute_jog_target('joint', request, list(start))


def test_wiring_a_jog_into_the_block_is_refused(tmp_path, monkeypatch):
    """FAILS FIRST (r5): a jog to V1 (0.82, 0.64, 2.48, -0.14, 0) is refused with TOWER_TOO_CLOSE_DE (WP-02's teeth:
    the block)."""
    monkeypatch.setenv('EDUBOTICS_CALIB_DIR', str(tmp_path))
    node = _GuardNode('v1', 'tower_v1', tmp_path)
    start = [0.82, 0.64, 2.38, -0.14, 0.0, H.GR['v1']['open']]
    with pytest.raises(M.WorkflowError) as excinfo:
        node.jog_joint(start, 2, 0.10)
    assert str(excinfo.value) == _TOO_CLOSE_DE


def _arm_check_record(profile_id, tower_pose=(0.0, 0.0, 0.0)):
    """a complete arm check (store.REQUIRED_KEYS) with zero offsets, tilts and slopes and the design envelope."""
    arm = SF.arm_key(profile_id)
    env = H.DESIGN_ENV[arm]
    return {
        'v': 1, 'fixture_id': SF.FIXTURE_ID, 'contract_hash': SF.contract_hash(), 'profile_id': profile_id,
        'camera_profile_id': '0123456789ab', 'image_size': [1920, 1080], 'created_utc': '2026-11-20T08:00:00Z',
        'kb': {'f': 1037.8, 'aspect': 1.0, 'cx': 959.5, 'cy': 539.5, 'k1': 0.0, 'k2': 0.0}, 'print_scale': 1.0,
        'joint_offsets_rad': [0.0] * 5, 'axis_tilts_rad': [0.0] * 6, 'tilt': [0.0, 0.0],
        'mat_pose': [0.0, 0.0, 0.0], 'camera_pose_mat': {'rvec': [0.0, 0.0, 0.0], 't': [0.0, 0.0, 0.5]},
        'lens_w': list(SF.LENS_F), 'fixture_xy_mat': [[0.0, 0.0]] * 8, 'fixture_px': [[900.0, 400.0]] * 8,
        'mount': {'rvec': [0.0, 0.0, 0.0], 't': [0.0, 0.0, 0.0]},
        'droop_envelope_rad': [0.0, env[0], env[1], env[2], 0.0],
        'tower_pose': list(tower_pose), 'tower_fit_rms_m': 0.0002, 'target_removed': False,
        'droop_warning': None, 'notes': [],
        'quality': {'poses_used': 20, 'rms_px': 0.2, 'holdout_rms_px': 0.3, 'holdout_p95_mm': 0.4,
                    'field_tags': 14, 'sharpness': 80.0},
    }


def _column_gap(arm, q, gripper, tower_pose=None):
    """the level-1 box gap between the arm at ``q`` and the placed column, without any allowance (m)."""
    F = fg()
    frames = H.nominal_frames(arm)(np.array([q], float))
    boxes = F.arm_boxes(arm, gripper, gripper)
    corners, axes = F.world_boxes(frames, boxes)
    column = [p for p in F._parts(SF.tower_obbs_w(H.BASE_X[arm], tower_pose)) if p.name == 'column'][0]
    nb = corners.shape[1]
    gaps = F.gap_dir(corners[0], axes[0], np.repeat(column.corners[None], nb, 0), np.repeat(column.axes[None], nb, 0),
                     np.zeros(nb), np.tile([0.0, 0.0, 1.0], (nb, 1)))
    return float(gaps.min())


def _step_to_gap(arm, gripper, want_m, tower_pose=None):
    """``(start, end)``: a joint-1 jog step of 0.05 rad toward the column whose END leaves the arm ``want_m`` from it
    (bisection between SCOPE_BRACKET's far and near joint-1 angles, where the gap falls monotonically)."""
    far, near = SCOPE_BRACKET[arm]
    q = np.array(SCOPE_SHAPE[arm], float)

    def gap(j1):
        q[0] = j1
        return _column_gap(arm, q, gripper, tower_pose)
    assert gap(far) > want_m > gap(near)
    for _ in range(60):
        mid = 0.5 * (far + near)
        if gap(mid) > want_m:
            far = mid
        else:
            near = mid
    end = np.array(SCOPE_SHAPE[arm], float)
    end[0] = 0.5 * (far + near)
    start = end.copy()
    start[0] -= math.copysign(0.05, SCOPE_BRACKET[arm][1] - SCOPE_BRACKET[arm][0])
    return list(start) + [gripper], list(end) + [gripper]


# a shape that reaches back toward the column as joint 1 turns (p05: the V1 meets it near +0.78 rad, the OMX near
# -0.80 rad), and a joint-1 bracket on its near side over which the column gap falls monotonically
SCOPE_SHAPE = {'v1': (0.0, 0.14, 2.79, -1.01, 0.0), 'omx': (0.0, -1.48, -1.41, -0.3, 0.0)}
SCOPE_BRACKET = {'v1': (0.28, 0.75), 'omx': (-0.30, -0.78)}


@pytest.mark.parametrize('arm', ['v1', 'omx'])
def test_wiring_scope_unknown_profile_guards_the_manual_paths_legacy_does_not(arm, tmp_path, monkeypatch):
    """FAILS FIRST (R4, D63): profile None + no arm check + a jog step ending 5 mm from the column -> refused with
    the uncalibrated residual at the contract place; legacy -> no guard at all, the jog is today's; tower_v1 ->
    the same refusal (every leg judged). The simulator's guard follows the same scope (R5, 58 m-8)."""
    monkeypatch.setenv('EDUBOTICS_CALIB_DIR', str(tmp_path))
    gripper = H.GR[arm]['open']
    start, end = _step_to_gap(arm, gripper, 0.005)
    assert _column_gap(arm, end[:5], gripper) == pytest.approx(0.005, abs=1e-4)
    delta = end[0] - start[0]
    today = _GuardNode(arm, 'legacy', tmp_path)
    assert today._fixture_guard() is None and today._fixture_guard(simulated=True) is None
    q_end, _world = today.jog_joint(start, 0, delta)
    assert q_end == pytest.approx(end)
    for scene in (None, 'tower_v1'):
        node = _GuardNode(arm, scene, tmp_path)
        for simulated in (False, True):
            g = node._fixture_guard(simulated=simulated)
            assert isinstance(g, fg().TowerGuard) and not g.calibrated
            assert g.residual_m == SF.TOWER_RESIDUAL_UNCALIBRATED_M[arm]
        with pytest.raises(M.WorkflowError) as excinfo:
            node.jog_joint(start, 0, delta)
        assert str(excinfo.value) == _TOO_CLOSE_DE


def test_wiring_both_managers_ask_the_node_for_their_guard(tmp_path, monkeypatch):
    """FAILS FIRST (R5, 58 m-8): the real manager's provider is the node's guard and the simulator's the node's
    simulator guard, so the simulator is guarded exactly when the real arm is: under ``tower_v1`` and while the
    profile is unknown with the uncalibrated residual and the tower at the contract place (where the simulator draws
    it, also when the rig's own arm check places the real one elsewhere), under ``legacy`` not at all."""
    fns = _node_fns('_get_or_create_workflow_manager', '_get_or_create_sim_workflow_manager')
    asked = []
    node = types.SimpleNamespace(
        workflow_manager=None, sim_workflow_manager=None, _sim_world=None, _sim_objects=[],
        _arm_profile=PROFILES['v1'], _trajectory_publisher=lambda pts: None, _build_ik_solver=lambda: None,
        _build_sim_ik_solver=lambda: None, _build_perception=lambda: None, _load_workflow_calibration=lambda: {},
        _emit_workflow_status=lambda _payload: None, _on_workflow_finished=lambda *_a: None,
        _on_sim_workflow_finished=lambda *_a: None, _get_current_gripper_xyz=lambda: None,
        _load_object_catalog=lambda: None, _load_object_catalog_tolerant=lambda: None, _ensure_sim_publisher=lambda: None,
        _publish_sim_frame=lambda q, scene=None, late_s=0.0: None, _profile_n=lambda: 5, communicator=None,
        get_logger=_Logger,
        _fixture_guard=lambda simulated=False: (asked.append(simulated), ('guard', simulated))[1])
    real = fns['_get_or_create_workflow_manager'](node)
    sim = fns['_get_or_create_sim_workflow_manager'](node)
    assert real._fixture_guard_provider() == ('guard', False)
    assert sim._fixture_guard_provider() == ('guard', True)
    assert asked == [False, True]
    monkeypatch.setenv('EDUBOTICS_CALIB_DIR', str(tmp_path))
    STORE.Store(str(tmp_path)).write(_arm_check_record('v1_studio', (math.radians(1.5), 0.004, -0.003)))
    contract = [c for n, c, _r, _h in SF.tower_obbs_w(0.0) if n == 'column'][0]
    measured = [c for n, c, _r, _h in SF.tower_obbs_w(0.0, (math.radians(1.5), 0.004, -0.003)) if n == 'column'][0]
    for scene in ('tower_v1', None):
        guard_node = _GuardNode('v1', scene, tmp_path)
        real_guard, sim_guard = guard_node._fixture_guard(), guard_node._fixture_guard(simulated=True)
        column = {id(g): [p for p in g.parts if p.name == 'column'][0].centre for g in (real_guard, sim_guard)}
        assert column[id(real_guard)] == pytest.approx(measured, abs=1e-12)
        assert column[id(sim_guard)] == pytest.approx(contract, abs=1e-12)
        assert real_guard.calibrated and sim_guard.calibrated
    empty = tmp_path / 'leer'
    empty.mkdir()
    monkeypatch.setenv('EDUBOTICS_CALIB_DIR', str(empty))
    for scene in ('tower_v1', None):
        sim_guard = _GuardNode('v1', scene, empty)._fixture_guard(simulated=True)
        assert not sim_guard.calibrated and sim_guard.residual_m == SF.TOWER_RESIDUAL_UNCALIBRATED_M['v1']
        assert [p for p in sim_guard.parts if p.name == 'column'][0].centre == pytest.approx(contract, abs=1e-12)
    legacy = _GuardNode('v1', 'legacy', tmp_path)
    assert legacy._fixture_guard() is None and legacy._fixture_guard(simulated=True) is None


def test_wiring_a_stored_tower_pose_moves_the_jog_refusal_by_the_columns_arc(tmp_path, monkeypatch):
    """FAILS FIRST (R4, D65 (1)): with a valid arm check the provider places the tower by its tower_pose: turned by
    psi = 1.5 deg about the joint-1 axis, the joint-1 angle at which a jog toward the column is first refused moves by
    the same 1.5 deg (the column's arc), in the direction the tower turned."""
    monkeypatch.setenv('EDUBOTICS_CALIB_DIR', str(tmp_path))
    arm, gripper = 'v1', H.GR['v1']['open']
    q0 = np.array(SCOPE_SHAPE[arm], float)
    first = {}
    for psi_deg in (0.0, 1.5):
        STORE.Store(str(tmp_path)).write(_arm_check_record('v1_studio', (math.radians(psi_deg), 0.0, 0.0)))
        node = _GuardNode(arm, 'tower_v1', tmp_path)
        g = node._fixture_guard()
        assert isinstance(g, fg().TowerGuard) and g.calibrated
        column = [p for p in g.parts if p.name == 'column'][0]
        want = SF.tower_obbs_w(0.0, (math.radians(psi_deg), 0.0, 0.0))
        assert column.centre == pytest.approx([c for n, c, _r, _h in want if n == 'column'][0], abs=1e-12)

        def refused(j1):
            start = q0.copy()
            start[0] = j1 - 0.02                         # one small step toward the column (joint 1 growing)
            try:
                node.jog_joint(list(start) + [gripper], 0, 0.02)
            except M.WorkflowError as e:
                assert str(e) == _TOO_CLOSE_DE
                return True
            return False
        lo, hi = SCOPE_BRACKET[arm]                      # passes at the far end, refused at the near one
        assert not refused(lo) and refused(hi)
        for _ in range(30):
            mid = 0.5 * (lo + hi)
            if refused(mid):
                hi = mid
            else:
                lo = mid
        first[psi_deg] = 0.5 * (lo + hi)
    # the tower turned by +psi (counter-clockwise from above): the column's azimuth grows by psi; the V1's joint 1 is
    # minus the azimuth (the arm reaches back over its base here), so the first refused joint-1 angle moves by -psi
    shift = math.degrees(first[1.5] - first[0.0])
    assert shift == pytest.approx(-1.5, abs=0.15), first


def _replay_node(arm, guard_fn=None, ik=None):
    """a stub node for /workshop/replay with the real callback (and ``_fixture_guard`` when given)."""
    fns = _node_fns('workshop_replay_callback')
    node = types.SimpleNamespace()
    node.published, node.replay_runs, node.notices = [], [], []
    node._arm_profile = PROFILES[arm]
    node._manual_record_active = False
    node._manual_replay_thread = None
    node._manual_transient_ops = 0
    node._manual_persistent = False
    node._manual_exit_gen = 0
    node._manual_last_activity_mono = 0.0
    node._mode_lock = threading.Lock()
    node._workflow_traj_publisher = object()
    node.on_manual = False
    node.joints = None
    node.communicator = types.SimpleNamespace(get_latest_follower_joints=lambda: node.joints)
    node._follower_joints_stale = lambda: False
    node._assert_no_other_active = lambda mode: (True, '')
    node._manual_leader_blocks = lambda: (False, '')
    node._profile_n = lambda: 5
    model = ik if ik is not None else _arm_ik(arm)
    node._build_ik_solver = lambda: model
    node._build_replay_lead_floor_check = lambda: None
    node._run_replay = lambda segmented, exit_gen, rebuild=None: node.replay_runs.append(segmented)

    def recompute():
        node.on_manual = bool(node._manual_persistent or node._manual_transient_ops > 0)
    node._recompute_on_manual_locked = recompute
    node.get_logger = _Logger
    if guard_fn is not None:
        node._fixture_guard = guard_fn
    node.callback = types.MethodType(fns['workshop_replay_callback'], node)
    return node


def _replay_request(rows):
    return types.SimpleNamespace(points_json=json.dumps({'fps': 25, 'points': rows}), name='', speed=1.0)


def _response():
    return types.SimpleNamespace(success=None, message=None)


def test_wiring_a_manual_replay_whose_third_waypoint_is_refused_is_refused_before_the_first_publish():
    """FAILS FIRST (r5): /workshop/replay of a take whose third waypoint is the column pose: refused in the service
    call itself, in German, before the first publish (no replay thread, no claim left behind)."""
    arm = 'v1'
    ik = _arm_ik(arm)
    guard = _guard_on(arm, ik)
    node = _replay_node(arm, guard_fn=lambda: guard, ik=ik)
    g = H.GR[arm]['open']
    home = list(H.HOME[arm])
    rows = [home + [g, 0.0], [0.5, 0.4, 1.6, -0.6, 0.0, g, 1.0], list(TEETH[arm]) + [g, 2.0], home + [g, 3.0]]
    node.joints = home + [g]
    resp = node.callback(_replay_request(rows), _response())
    assert resp.success is False and resp.message == _TOO_CLOSE_DE
    assert node.replay_runs == [] and node.published == []
    assert node._manual_transient_ops == 0 and node.on_manual is False
    # the same take without its third waypoint starts (the guard is what refused it)
    resp = node.callback(_replay_request([rows[0], rows[1], rows[3]]), _response())
    assert resp.success is True, resp.message
    assert len(node.replay_runs) == 1


def test_wiring_an_omx_replay_beyond_the_joint1_window_is_refused(monkeypatch):
    """FAILS FIRST: an OMX replay with joint 1 at 2.0 rad is refused with the joint-1 sentence, on both paths (the
    Blockly replay and /workshop/replay), before anything is published (S6.3)."""
    monkeypatch.setattr(TR, 'chunked_publish', _fast_publish)
    arm, g = 'omx', H.GR['omx']['open']
    rows = [[1.2, -1.0, 0.6, 0.4, 0.0, g, 0.0], [2.0, -1.0, 0.6, 0.4, 0.0, g, 1.0], [1.2, -1.0, 0.6, 0.4, 0.0, g, 2.0]]
    ctx = _Run(arm, [1.2, -1.0, 0.6, 0.4, 0.0, g])
    ctx.trajectories = {'Weit': {'fps': 25, 'points': rows}}
    with pytest.raises(M.WorkflowError) as excinfo:
        TR.replay_trajectory(ctx, {'name': 'Weit'})
    assert str(excinfo.value) == _JOINT1_WINDOW_DE
    assert ctx.published == []
    node = _replay_node(arm)
    node.joints = [1.2, -1.0, 0.6, 0.4, 0.0, g]
    resp = node.callback(_replay_request(rows), _response())
    assert resp.success is False and resp.message == _JOINT1_WINDOW_DE
    assert node.replay_runs == []
    # at the window's edge a reading may sit up to 5 mrad outside it (servo readback at a stop): 1.75 rad is
    # 100.27 deg, 4.7 mrad past the edge, and runs; 1.7514 rad (6.1 mrad past) is refused
    assert TR._JOINT1_WINDOW_TOL_RAD == 0.005
    edge = [list(r) for r in rows]
    edge[1][0] = 1.7514
    ctx = _Run(arm, [1.2, -1.0, 0.6, 0.4, 0.0, g])
    ctx.trajectories = {'Rand': {'fps': 25, 'points': edge}}
    with pytest.raises(M.WorkflowError):
        TR.replay_trajectory(ctx, {'name': 'Rand'})
    inside = [list(r) for r in rows]
    inside[1][0] = 1.75
    ctx = _Run(arm, [1.2, -1.0, 0.6, 0.4, 0.0, g])
    ctx.trajectories = {'Weit': {'fps': 25, 'points': inside}}
    TR.replay_trajectory(ctx, {'name': 'Weit'})
    assert ctx.published


def test_wiring_a_slow_replay_check_says_so_once_before_its_first_chunk(monkeypatch):
    """FAILS FIRST (R4, D70): a replay whose tower check takes longer than TOWER_CHECK_SLOW_S (patched to 0.05 s
    here; 1.0 s shipped) logs TOWER_CHECK_SLOW_DE once, in German, while the check runs and before the first chunk;
    a fast check says nothing."""
    assert TR.TOWER_CHECK_SLOW_S == 1.0
    events = []
    monkeypatch.setattr(TR, 'chunked_publish',
                        lambda publisher, points, should_stop, **_k: (events.append('publish'), True)[1])
    monkeypatch.setattr(TR, 'TOWER_CHECK_SLOW_S', 0.05)

    class _Slow(_Scripted):
        def __init__(self, delay):
            super().__init__()
            self.delay = delay

        def judge_polyline(self, w, gr):
            time.sleep(self.delay)
            events.append('judged')
            return super().judge_polyline(w, gr)

        def judge_leg(self, a, b, ga, gb):
            time.sleep(self.delay)
            events.append('judged')
            return super().judge_leg(a, b, ga, gb)
    arm, g = 'v1', H.GR['v1']['open']
    home = list(H.HOME[arm])
    rows = [home + [g, 0.0], [0.3, 0.7, 0.8, 0.0, 0.0, g, 1.0]]
    for delay, said in ((0.4, 1), (0.0, 0)):
        events.clear()
        ctx = _Run(arm, home + [g], guard=_Slow(delay))
        ctx.log = lambda text: events.append(('log', text))
        ctx.trajectories = {'Take': {'fps': 25, 'points': rows}}
        TR.replay_trajectory(ctx, {'name': 'Take'})
        notices = [e for e in events if e == ('log', _CHECK_SLOW_DE)]
        assert len(notices) == said, events
        assert events[-1] == 'publish' and events.count('publish') == 1
        if said:
            assert events.index(notices[0]) < events.index('publish')


def _take(arm, rng, seconds, amp_hi, fr_hi):
    """q35's take model (p12b): a 25 Hz sine take about a random reachable pose, clipped to the arm's limits."""
    lim = np.array(H.solver(arm).joint_limits)
    reach = (0.10, 0.34) if arm == 'v1' else (0.10, 0.25)
    while True:
        r = rng.uniform(*reach)
        a = math.radians(rng.uniform(-60, 60))
        z = SF.MAT_THICKNESS_M + 0.015 + rng.choice([0.0, 0.03, 0.06])
        q0 = H.tip_solve(arm, r * math.cos(a), r * math.sin(a), z, rng.uniform(-1.2, 1.2))
        if q0 is not None:
            break
    t = np.arange(0, seconds, 0.04)
    amp, fr, ph = rng.uniform(0.05, amp_hi, 5), rng.uniform(0.05, fr_hi, 5), rng.uniform(0, 2 * math.pi, 5)
    return np.clip(q0[None] + amp[None] * np.sin(2 * math.pi * fr[None] * t[:, None] + ph[None]), lim[:, 0], lim[:, 1])


@pytest.mark.parametrize('arm', ['v1', 'omx'])
def test_wiring_a_replay_is_one_polyline_with_the_rules_own_verdict_within_the_budget(arm):
    """R5 (D70): a replay is judged as ONE polyline by the travel-bound rule: the wiring's path verdict with a still
    claw is the guard's own judge_polyline verdict on the same waypoints, and a 120 s take of the p81-like model is
    judged within revision 4's 1.5 s of process time here (p12b worst 1.28 s; RG-GT holds the Pi budget). A claw
    that moves is judged per 0.1 rad band, never as one interval."""
    ik = _arm_ik(arm)
    g = _guard_on(arm, ik)
    grip = H.GR[arm]['holding']
    rng = np.random.default_rng(81 + (0 if arm == 'v1' else 1))
    times = []
    for _ in range(3):
        W = _take(arm, rng, 120.0, 0.35, 0.4)
        full = np.concatenate([W, np.full((len(W), 1), grip)], axis=1)
        t0 = time.process_time()
        got = M.tower_path_verdict(g, full, 5)
        times.append(time.process_time() - t0)
        want = g.judge_polyline(W, grip)
        assert (got is not None) == want.refused
        if got is not None:
            assert got.sentence_de == want.sentence_de
    print(f'{arm} 120 s takes, wiring path verdict: process time ' + ', '.join(f'{t:.2f} s' for t in times))
    assert max(times) <= 1.5, times
    # a claw that closes during the take: judged in bands of at most TOWER_CLAW_BAND_RAD
    assert M.TOWER_CLAW_BAND_RAD == 0.1
    seen = []
    spy = _Scripted()
    spy.judge_leg = lambda a, b, ga, gb: (seen.append((ga, gb)), fg().Verdict(False, 'clear'))[1]
    q = list(H.HOME[arm])
    assert M.tower_leg_verdict(spy, q + [H.GR[arm]['open']], q + [H.GR[arm]['closed']], 5) is None
    assert len(seen) >= 2 and all(abs(b - a) <= 0.1 + 1e-12 for a, b in seen)
    assert seen[0][0] == H.GR[arm]['open'] and seen[-1][1] == H.GR[arm]['closed']


def test_wiring_the_handlers_read_the_guard_with_getattr_only():
    """motion.py and trajectory.py read the run's guard only as getattr(ctx, 'fixture_guard', None) (contexts
    without one — every existing test, every path outside Roboter Studio — publish what they always did)."""
    for name in ('motion.py', 'trajectory.py'):
        tree = ast.parse((_PKG / 'workflow' / 'handlers' / name).read_text(encoding='utf-8'))
        reads = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == 'fixture_guard':
                raise AssertionError(f'{name}: fixture_guard read as an attribute at line {node.lineno}')
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'getattr'
                    and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value == 'fixture_guard'):
                assert len(node.args) == 3 and isinstance(node.args[2], ast.Constant) and node.args[2].value is None
                reads += 1
        assert reads >= 1, name


def test_wiring_d42_a_program_warns_once_per_target_per_run_at_the_sector_edge(monkeypatch):
    """FAILS FIRST (D42, WP-11's handover): a program run that drives the V1 to its sector edge (+89.5 deg, a miss of
    about 3 mm in the calibrated model) logs exactly one German refinement warning naming the Ziel; a second command
    to the same target in the same run logs none; a target inside the sector (+88 deg) logs none; a new run warns
    again. The dedupe field is declared on WorkflowContext."""
    from physical_ai_server.workflow import workflow_manager as WMOD
    from physical_ai_server.workflow.autocal import texts_de
    monkeypatch.setattr(M, 'chunked_publish', _fast_publish)
    assert 'refine_warned' in WMOD.WorkflowContext.__dataclass_fields__
    tilt = math.radians(0.3)
    ik = arm_model.CalibratedIK(V1IKSolver(), offsets=(0.0, 0.010, -0.020, 0.015, 0.0),
                                axis_tilts=(tilt, 0.0, -tilt, tilt, tilt, -tilt), slopes=(0.004, -0.003),
                                fingertip_m=0.0, joint1_window_deg=(-90.0, 90.0))
    z = SF.MAT_THICKNESS_M + 0.015

    def point(az):
        return {'x': 0.15 * math.cos(math.radians(az)), 'y': 0.15 * math.sin(math.radians(az)), 'z': z}

    def run():
        ctx = WMOD.WorkflowContext(publisher=lambda pts: None, ik=ik)
        ctx.logs = []
        ctx.log = ctx.logs.append
        ctx.destinations = {'Rand': point(89.5), 'Innen': point(88.0)}
        ctx.home_joints_rad = PROFILES['v1'].home_joints_rad
        ctx.num_arm_joints = 5
        ctx.last_full_joints = list(H.HOME['v1']) + [H.GR['v1']['open']]
        ctx.last_arm_joints = list(H.HOME['v1'])
        return ctx

    ctx = run()
    M.move_to(ctx, {'destination': 'Rand'})
    miss = ik.last_refine_miss_m
    assert miss == pytest.approx(0.003, abs=0.0003)
    warn = '[WARNUNG] ' + texts_de.refinement_warning_de('Rand', miss * 1000.0)
    assert ctx.logs == [warn]
    M.move_to(ctx, {'destination': 'Innen'})
    M.move_to(ctx, {'destination': 'Rand'})
    assert ctx.logs == [warn]
    again = run()
    M.move_to(again, {'destination': 'Rand'})
    assert again.logs == [warn]


@pytest.mark.parametrize('arm', ['v1', 'omx'])
def test_wiring_the_routes_that_must_never_be_refused_run_through_the_guard(arm, monkeypatch):
    """FAILS FIRST (R4, p14): through the wiring, with the guard consulted: the pinned arm-check route (the
    uncalibrated residual at the contract place, claw closed), HOME <-> OBS, HOME <-> every hover of WP-02's two
    hover grids, OBS <-> every hover THROUGH THE LADDER (the V1's direct refusals of p14 take the via-HOME rung, the
    OMX's never), and 200 seeded strict-vertical moves from WP-02 test (4)'s cut-zone grid: none refused."""
    legs = []
    monkeypatch.setattr(M, '_publish_motion', lambda c, a, b, d, tempo=None: legs.append((list(a), list(b))))
    ik = _arm_ik(arm)
    gr = H.GR[arm]
    home, obs = [float(v) for v in H.HOME[arm]], [float(v) for v in H.OBS[arm]]

    def ctx_at(q, guard):
        return _Run(arm, q, guard=guard, ik=ik)

    # the pinned arm-check route, before calibration
    spy = _Spy(_guard_on(arm, ik, calibrated=False))
    route = [home] + [list(p) for p in H.SPEC[arm]['arm_check_poses_rad']] + [home]
    ctx = ctx_at(route[0] + [gr['closed']], spy)
    for a, b in zip(route[:-1], route[1:]):
        M.safe_move(ctx, a + [gr['closed']], b + [gr['closed']], M.DEFAULT_MOVE_DURATION_S)
        ctx.last_full_joints = b + [gr['closed']]
    assert spy.calls >= 21
    spy = _Spy(_guard_on(arm, ik))
    for s in (gr['open'], gr['holding']):
        # HOME <-> OBS
        ctx = ctx_at(home + [s], spy)
        M.go_to_observation_pose(ctx)
        M.home(ctx, {})
        assert ctx.last_full_joints[:5] == pytest.approx(home)
    via_home = set()

    def through_home():
        return any(np.allclose(b[:5], home, atol=1e-9) for _a, b in legs[:-1])
    for extra in (0.0, 0.060):
        for key, q in H._hover_grid(arm, extra):
            hover = list(q)
            for s_name in ('open', 'holding'):
                s = gr[s_name]
                # HOME <-> hover
                ctx = ctx_at(hover + [s], spy)
                M.home(ctx, {})
                legs.clear()
                M.safe_move(ctx, home + [s], hover + [s], M.DEFAULT_MOVE_DURATION_S)
                assert len(legs) == 1
                # hover -> OBS and OBS -> hover, through the ladder
                ctx = ctx_at(hover + [s], spy)
                legs.clear()
                M.go_to_observation_pose(ctx)
                assert np.allclose(legs[-1][1][:5], obs, atol=1e-9)
                reach = through_home()
                legs.clear()
                M.safe_move(ctx, obs + [s], hover + [s], M.DEFAULT_MOVE_DURATION_S)
                assert np.allclose(legs[-1][1][:5], hover, atol=1e-9)
                leave = through_home()
                assert reach == leave, (key, s_name)
                if reach:
                    via_home.add((extra, s_name, key))
    want = {(extra, s_name, key) for (a, extra), refused in H.OBS_REFUSED.items() if a == arm
            for key in refused for s_name in ('open', 'holding')}
    assert via_home == want
    # 200 seeded strict-vertical moves inside the cut zone
    points = []
    for r in np.linspace(0.10, H.REACH[arm], 6):
        for z_tip in (0.0155, 0.0755, 0.1355):
            for az in np.arange(-H.WIN[arm], H.WIN[arm] + 0.01, 5.0):
                x, y = r * math.cos(math.radians(az)), r * math.sin(math.radians(az))
                q = H.tip_solve(arm, x, y, z_tip, 0.0)
                if q is not None and SF.in_zone(H.SPEC[arm], x, y):
                    points.append(list(q))
    rng = np.random.default_rng(2000 + (0 if arm == 'v1' else 1))
    moves = 0
    while moves < 200:
        i, j = rng.integers(0, len(points), 2)
        if i == j:
            continue
        ctx = ctx_at(points[i] + [gr['open']], spy)
        M.safe_move(ctx, points[i] + [gr['open']], points[j] + [gr['open']], M.DEFAULT_MOVE_DURATION_S)
        moves += 1
    assert spy.calls > 1000



def test_wiring_the_ik_precheck_warns_about_a_target_the_guard_refuses():
    """S6.2 'Where': the start-time pre-check adds a non-fatal warning (TOWER_TOO_CLOSE_DE) for a concrete target whose
    solved pose the run's guard refuses (WP-02 (4)'s OMX corner beside the tower base); without a guard the pre-check
    is today's, and a target in front of the arm is never flagged."""
    from physical_ai_server.workflow.workflow_manager import WorkflowManager
    ik = _arm_ik('omx')
    guard = _guard_on('omx', ik)
    home = list(H.HOME['omx'])
    tip = SF.FINGERTIP_BELOW_EE_M['omx']
    near = None
    for az in (100.0, 95.0, 90.0, 85.0):
        xyz = (H.BASE_X['omx'] + 0.1 * math.cos(math.radians(az)), 0.1 * math.sin(math.radians(az)), 0.0155 + tip)
        q = ik.solve(target_xyz=xyz, seed=home, free_yaw=True)
        if q is not None and guard.judge_pose(list(q), H.GR['omx']['open']).refused:
            near = xyz
            break
    assert near is not None
    front = (H.BASE_X['omx'] + 0.18, 0.0, 0.05)

    class _Interp:
        def collect_concrete_destinations(self, extra_destinations=None):
            return [{'block_id': 'turm', 'xyz': near}, {'block_id': 'vorne', 'xyz': front}]
    wm = WorkflowManager(publisher=lambda pts: None, arm_profile=PROFILES['omx'])
    assert wm._ik_precheck(_Interp(), ik) == []
    got = wm._ik_precheck(_Interp(), ik, fixture_guard=guard)
    assert got == [{'block_id': 'turm', 'message': _TOO_CLOSE_DE}]

#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The tower guard against the meshes, the families and the clock (52 §3.6 tests 1-6, 8, 9; WP-02).

1. the box gap is a lower bound of the mesh gap; 2. every surface point lies in a box;
3. the dense count bounds the travel; 4. the allowance recomputed from the constants;
5. what cannot be judged is refused in German; 6. the ordinary families at B reproduce
``p02_gabsdir``'s counts on the uncut zone and refuse nothing on the cut zone;
8. nothing outside the guard calls it yet (``legacy`` is today's code path by construction);
9. the per-leg time on ``q20``'s leg set (D72's CI budget, R5-2).
The meshes are read here with this file's OWN STL and URDF parser, not the generator's.
"""

from __future__ import annotations

import ast
import functools
import hashlib
import importlib.util
import math
import struct
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest

import test_fixture_guard as H
from physical_ai_server.workflow import scene_fixture as SF

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
# 8. nothing outside the guard calls it (legacy = today's code path)
# ══════════════════════════════════════════════════════════════════════════

def test_legacy_is_unchanged_because_nothing_calls_the_guard_yet():
    """WP-02 is the pure half: no server module but the guard itself imports fixture_guard or arm_cells, so every
    profile's motion path is byte for byte today's (the wiring is WP-12's)."""
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
    assert importers.get('fixture_guard', set()) == set()
    assert importers.get('arm_cells', set()) == {'workflow/fixture_guard.py'}
    assert importers.get('scene_fixture', set()) == {'workflow/fixture_guard.py', 'workflow/object_catalog.py'}


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

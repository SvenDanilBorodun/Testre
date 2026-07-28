"""Whole-link geometry (``workflow/arm_geometry.py``) — the table-floor and
self-collision model behind the safe Grundstellung.

The highest-value test here is :func:`test_boxes_contain_every_mesh_vertex`: it
re-derives the shipped boxes from the in-repo URDF and its binary STLs with a
parser that shares no code with the solver — the same discipline as
``test_edu6_ik.py``'s independent FK oracle — so a transcription slip in the 42
numbers cannot verify itself.
"""

from __future__ import annotations

import math
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest

from physical_ai_server import robot_profiles as RP
from physical_ai_server.workflow import arm_geometry as AG
from physical_ai_server.workflow.edu6_ik import Edu6IKSolver
from physical_ai_server.workflow.ik_solver import IKSolver

_URDF_DIR = (Path(__file__).resolve().parents[2]
             / 'physical_ai_manager' / 'public' / 'edu6-urdf')
_URDF = _URDF_DIR / 'edu6.urdf'

_EDU6 = RP.ROBOT_PROFILES['edu6_studio']
_HOME = list(_EDU6.home_joints_rad)
_GRIP_OPEN = _EDU6.gripper_open_rad
_LIMITS = list(Edu6IKSolver().joint_limits)

# The URDF gripper band; link6's box is the union over it.
_GRIP_BAND = (0.0, 1.79)


_UNSET = object()


def _ctx(ik=_UNSET):
    """Minimal ctx stand-in: resolve_geometry only ever reads ``.ik``.

    ``_UNSET`` (the default) means "a real edu6 solver"; an explicit ``None``
    means "a ctx whose ik really is None" and must stay distinguishable — an
    earlier version collapsed the two and silently never tested the None case.
    """
    class _C:
        pass
    c = _C()
    c.ik = Edu6IKSolver() if ik is _UNSET else ik
    return c


# ── independent URDF + STL oracle (shares no code with the solver) ───────────

def _rpy(r, p, y):
    cr, sr = math.cos(r), math.sin(r)
    cp, sp = math.cos(p), math.sin(p)
    cy, sy = math.cos(y), math.sin(y)
    return (np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1.0]])
            @ np.array([[cp, 0, sp], [0, 1.0, 0], [-sp, 0, cp]])
            @ np.array([[1.0, 0, 0], [0, cr, -sr], [0, sr, cr]]))


def _axis_rot(axis, th):
    a = np.asarray(axis, float)
    a = a / np.linalg.norm(a)
    k = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    return np.eye(3) + math.sin(th) * k + (1 - math.cos(th)) * (k @ k)


def _load_stl_vertices(path: Path) -> np.ndarray:
    raw = path.read_bytes()
    count = struct.unpack('<I', raw[80:84])[0]
    out = np.zeros((count * 3, 3), dtype=np.float64)
    off = 84
    for i in range(count):
        vals = struct.unpack('<12f', raw[off:off + 48])
        out[3 * i] = vals[3:6]
        out[3 * i + 1] = vals[6:9]
        out[3 * i + 2] = vals[9:12]
        off += 50
    return out


class _Urdf:
    """Generic URDF chain + collision-mesh reader, straight from the XML."""

    def __init__(self, path: Path = _URDF) -> None:
        root = ET.parse(path).getroot()
        self.joints = {}
        self.parent = {}
        for el in root.findall('joint'):
            name = el.get('name')
            org = el.find('origin')
            ax = el.find('axis')
            mim = el.find('mimic')
            self.joints[name] = {
                'type': el.get('type'),
                'xyz': np.array([float(v) for v in org.get('xyz').split()])
                if org is not None else np.zeros(3),
                'rpy': np.array([float(v) for v in org.get('rpy').split()])
                if org is not None else np.zeros(3),
                'axis': (np.array([float(v) for v in ax.get('xyz').split()])
                         if ax is not None else np.array([0.0, 0.0, 1.0])),
                'mimic': (mim.get('joint'), float(mim.get('multiplier', 1.0)),
                          float(mim.get('offset', 0.0)))
                if mim is not None else None,
            }
            self.parent[el.find('child').get('link')] = (
                el.find('parent').get('link'), name)
        self.links = {}
        for el in root.findall('link'):
            src = el.find('collision')
            if src is None:
                src = el.find('visual')
            mesh = None
            org_xyz = np.zeros(3)
            org_rpy = np.zeros(3)
            if src is not None:
                org = src.find('origin')
                if org is not None:
                    org_xyz = np.array([float(v) for v in org.get('xyz').split()])
                    org_rpy = np.array([float(v) for v in org.get('rpy').split()])
                geo = src.find('geometry')
                node = geo.find('mesh') if geo is not None else None
                if node is not None:
                    mesh = node.get('filename').split('/')[-1]
            self.links[el.get('name')] = (mesh, org_xyz, org_rpy)
        self.root_link = next(n for n in self.links if n not in self.parent)

    def frames(self, qmap: dict) -> dict:
        out = {self.root_link: np.eye(4)}
        changed = True
        while changed:
            changed = False
            for child, (parent, jname) in self.parent.items():
                if child in out or parent not in out:
                    continue
                j = self.joints[jname]
                fixed = np.eye(4)
                fixed[:3, :3] = _rpy(*j['rpy'])
                fixed[:3, 3] = j['xyz']
                th = 0.0
                if j['type'] in ('revolute', 'continuous', 'prismatic'):
                    if j['mimic'] is not None:
                        src, mult, offs = j['mimic']
                        th = qmap.get(src, 0.0) * mult + offs
                    else:
                        th = qmap.get(jname, 0.0)
                mov = np.eye(4)
                if j['type'] == 'prismatic':
                    mov[:3, 3] = np.asarray(j['axis'], float) * th
                else:
                    mov[:3, :3] = _axis_rot(j['axis'], th)
                out[child] = out[parent] @ fixed @ mov
                changed = True
        return out

    def vertices_in_link_frame(self, link: str) -> np.ndarray:
        mesh, org_xyz, org_rpy = self.links[link]
        assert mesh, f'{link} has no collision/visual mesh in {_URDF}'
        path = _URDF_DIR / 'meshes' / mesh
        assert path.exists(), f'mesh missing on disk: {path}'
        return _load_stl_vertices(path) @ _rpy(*org_rpy).T + org_xyz


def _qmap(joints, grip):
    m = {f'joint{i + 1}': float(joints[i]) for i in range(6)}
    m['end_gear_joint'] = float(grip)
    return m


_BOX_LINKS = ('base_link', 'link1', 'link2', 'link3', 'link4', 'link5', 'link6')
# link6's box is the union of the whole gripper cluster (see EDU6_LINK_BOXES).
_GRIPPER_CHILDREN = ('End_effector', 'Claw_right_finger', 'Claw_left_finger')


# ── 1. the boxes really contain the meshes ───────────────────────────────────

def test_boxes_contain_every_mesh_vertex():
    """Every collision-mesh vertex of every link lies inside that link's shipped
    box, for the whole gripper command band.

    THE point of this file. Fails the moment someone edits a number in
    ``EDU6_LINK_BOXES``, or the CAD export changes, or link6's union stops
    covering the fingers at some jaw opening — any of which would silently make
    the floor guard optimistic, which is the one direction it must never be.
    """
    urdf = _Urdf()
    for idx, link in enumerate(_BOX_LINKS):
        lo, hi = (np.asarray(v, float) for v in AG.EDU6_LINK_BOXES[idx])
        pts = urdf.vertices_in_link_frame(link)
        assert (pts >= lo - 1e-9).all(), f'{link} pokes out below its box'
        assert (pts <= hi + 1e-9).all(), f'{link} pokes out above its box'

    # the gripper children, expressed in link6's frame, over the whole band
    lo6, hi6 = (np.asarray(v, float) for v in AG.EDU6_LINK_BOXES[6])
    for grip in np.linspace(_GRIP_BAND[0], _GRIP_BAND[1], 13):
        frames = urdf.frames(_qmap([0.0] * 6, grip))
        t6 = frames['link6']
        for child in _GRIPPER_CHILDREN:
            world = (urdf.vertices_in_link_frame(child)
                     @ frames[child][:3, :3].T + frames[child][:3, 3])
            local = (world - t6[:3, 3]) @ t6[:3, :3]
            assert (local >= lo6 - 1e-9).all(), (
                f'{child} escapes link6\'s box below at grip={grip:.3f}')
            assert (local <= hi6 + 1e-9).all(), (
                f'{child} escapes link6\'s box above at grip={grip:.3f}')


def test_link_frames_match_the_independent_urdf_oracle():
    """``Edu6IKSolver.link_frames`` agrees with the URDF-parsed frames, after the
    world rotation. Guards the frame convention the boxes are expressed in —
    getting this wrong (URDF vs WORLD differ by a 180° z-rotation) is a real bug
    that was hit while deriving the boxes."""
    urdf = _Urdf()
    ik = Edu6IKSolver()
    rz = np.array([[-1.0, 0, 0], [0, -1.0, 0], [0, 0, 1.0]])
    rng = np.random.default_rng(7)
    for _ in range(12):
        q = [float(rng.uniform(lo, hi)) for (lo, hi) in _LIMITS]
        got = ik.link_frames(q)
        want = urdf.frames(_qmap(q, _GRIP_OPEN))
        assert got is not None and len(got) == 7
        for idx, link in enumerate(_BOX_LINKS):
            expect_r = rz @ want[link][:3, :3]
            expect_t = rz @ want[link][:3, 3]
            assert np.allclose(got[idx][:3, :3], expect_r, atol=1e-9)
            assert np.allclose(got[idx][:3, 3], expect_t, atol=1e-9)


def test_link_frames_returns_none_for_a_short_vector():
    assert Edu6IKSolver().link_frames([0.0, 0.0, 0.0]) is None


# ── 2. the floor model is SOUND ──────────────────────────────────────────────

def _mesh_lowest_z(urdf, q, grip):
    frames = urdf.frames(_qmap(q, grip))
    rz = np.array([[-1.0, 0, 0], [0, -1.0, 0], [0, 0, 1.0]])
    lo = math.inf
    for link, (mesh, _x, _r) in urdf.links.items():
        if not mesh or link == 'base_link':
            continue
        world = (urdf.vertices_in_link_frame(link)
                 @ frames[link][:3, :3].T + frames[link][:3, 3]) @ rz.T
        lo = min(lo, float(world[:, 2].min()))
    return lo


def test_floor_clearance_is_a_sound_lower_bound_on_the_real_meshes():
    """The model NEVER reports more clearance than the meshes actually have.

    Soundness is the whole contract: an optimistic model would let the planner
    approve a route that presses the table, which is the defect being fixed.
    Fails if a box shrinks, if the frame mapping regresses, or if link6's union
    stops covering a jaw opening (a random grip is drawn per config on purpose —
    a fixed 1.75 would never exercise the union).
    """
    urdf = _Urdf()
    geom = AG.resolve_geometry(_ctx())
    assert geom is not None
    rng = np.random.default_rng(0xBEEF)
    for _ in range(40):
        q = [float(rng.uniform(lo, hi)) for (lo, hi) in _LIMITS]
        grip = float(rng.uniform(*_GRIP_BAND))
        model = geom.floor_clearance(q, lambda _x, _y: 0.0)
        truth = _mesh_lowest_z(urdf, q, grip)
        assert model is not None
        assert model <= truth + 1e-9, (
            f'model {model:.6f} > mesh truth {truth:.6f} at q={q}')


def test_floor_clearance_is_exact_at_home():
    """At HOME the model equals the mesh truth (+10.1 mm) rather than merely
    bounding it. A regression here means the model has become loose enough to
    start refusing legitimate poses."""
    urdf = _Urdf()
    geom = AG.resolve_geometry(_ctx())
    model = geom.floor_clearance(_HOME, lambda _x, _y: 0.0)
    truth = _mesh_lowest_z(urdf, _HOME, _GRIP_OPEN)
    assert model == pytest.approx(truth, abs=2e-4)
    assert model == pytest.approx(0.0101, abs=5e-4)


def test_floor_clearance_honours_a_tilted_table_plane():
    """``floor_fn`` is evaluated at each corner's OWN (x, y), so a tilted table
    measured by the touch-off is respected per point instead of against one
    scalar. Fails if someone collapses the callback to a constant."""
    geom = AG.resolve_geometry(_ctx())
    flat = geom.floor_clearance(_HOME, lambda _x, _y: 0.0)
    # a plane tilted 0.2 rad about y: the floor rises with x
    tilted = geom.floor_clearance(_HOME, lambda x, _y: 0.2 * x)
    assert tilted is not None and flat is not None
    assert tilted < flat


def test_no_legitimate_table_level_grasp_is_below_the_floor_allowance():
    """Strict-vertical grasps across the whole pick band must stay within the
    20 mm model allowance, or the guard would refuse ordinary grasping.

    Measured basis: over 900 such poses the model's lowest reading is -0.0 mm
    (the fingertip touching the table, by design).
    """
    from physical_ai_server.workflow.handlers import motion as M
    ik = Edu6IKSolver()
    geom = AG.resolve_geometry(_ctx(ik))
    checked = 0
    for r in np.linspace(0.05, 0.20, 16):
        for yaw in np.linspace(-1.3, 1.3, 7):
            q = ik.solve((ik.base_axis_x + r * math.cos(yaw),
                          r * math.sin(yaw), 0.0), roll=M.GRASP_ROLL_RAD)
            if q is None:
                continue
            checked += 1
            assert geom.floor_clearance(q, lambda _x, _y: 0.0) > -0.020
    assert checked > 50


# ── 3. self-collision ────────────────────────────────────────────────────────

def test_self_clearance_is_positive_at_home():
    """HOME's true mesh clearance is 31.4 mm; the box bound is pessimistic but
    must stay positive, or the warn-only check would fire on the rest pose."""
    geom = AG.resolve_geometry(_ctx())
    assert geom.self_clearance(_HOME) > 0.0


def test_the_static_base_is_excluded_from_the_floor_but_kept_for_self_collision():
    """base_link's underside IS the table (z ∈ [0, 0.0625] m). Counting it in the
    floor check would peg every reading at ≈0 and the guard would judge nothing.
    It must still be a self-collision partner — the gripper CAN reach the base."""
    geom = AG.resolve_geometry(_ctx())
    assert geom.floor_clearance(_HOME, lambda _x, _y: 0.0) > 0.005
    base_pairs = [p for p in AG._SELF_PAIRS if p[0] == 0]
    assert base_pairs, 'base_link must remain a self-collision partner'
    assert (0, 6) in base_pairs


def test_self_clearance_never_tests_adjacent_links():
    """Links one joint apart share that joint and their meshes interpenetrate
    there by construction. Testing them would report a permanent collision."""
    assert all(j - i >= 2 for i, j in AG._SELF_PAIRS)
    assert (0, 2) in AG._SELF_PAIRS
    assert (0, 1) not in AG._SELF_PAIRS
    assert (5, 6) not in AG._SELF_PAIRS


# ── 4. the OMX short-circuit — the bit-identity mechanism ────────────────────

def test_resolve_geometry_is_none_for_every_omx_profile_and_solver():
    """The mechanism that keeps the OMX byte-identical. If this ever returns an
    ArmGeometry, every new guard starts judging the OMX too — which is exactly
    the regression the design forbids."""
    assert AG.resolve_geometry(_ctx(IKSolver())) is None
    assert AG.resolve_geometry(IKSolver()) is None
    for pid in ('omx_full', 'omx_follower'):
        profile = RP.ROBOT_PROFILES[pid]
        assert AG.resolve_geometry(_ctx(profile.build_ik())) is None


def test_resolve_geometry_is_none_for_a_profileless_or_stub_ctx():
    class _Stub:
        backend = 'closed-form-edu6'      # right backend, no link_frames
        def num_joints(self):
            return 6
    assert AG.resolve_geometry(_ctx(_Stub())) is None
    assert AG.resolve_geometry(_ctx(None)) is None
    assert AG.resolve_geometry(object()) is None


def test_resolve_geometry_never_raises_on_a_hostile_solver():
    class _Angry:
        def link_frames(self, _joints):
            return None
        @property
        def backend(self):
            raise RuntimeError('boom')
    assert AG.resolve_geometry(_ctx(_Angry())) is None


def test_resolve_geometry_is_live_for_edu6():
    geom = AG.resolve_geometry(_ctx())
    assert geom is not None and geom.num_links == 7


# ── 5. the swept sampling rule is path_guard's, not a second one ─────────────

def test_swept_sampling_reuses_the_path_guard_constants():
    """The tunneling argument must be the SAME argument the zone check already
    makes. Fails if someone re-declares the constants locally, which is how the
    two would silently drift apart."""
    from physical_ai_server.workflow import path_guard as PG
    assert AG._MAX_SEGMENT_SAMPLES is PG._MAX_SEGMENT_SAMPLES
    assert AG._R_MAX_M is PG._R_MAX_M
    assert AG.STEP_M is PG.STEP_M
    # a big swing must be sampled far more finely than a tiny one, and capped
    assert AG._segment_samples([0.0] * 6, [0.0] * 6) == 1
    assert AG._segment_samples([0.0] * 6, [0.0, 0.05, 0, 0, 0, 0]) >= 7
    assert (AG._segment_samples([0.0] * 6, [0.0, 3.1, 0, 0, 0, 0])
            == PG._MAX_SEGMENT_SAMPLES)


def test_swept_floor_clearance_is_the_minimum_over_the_line():
    """The swept value must be no greater than either endpoint's — it is a
    minimum over the whole line, and the line is what gets published."""
    geom = AG.resolve_geometry(_ctx())
    a = [0.0, 2.9, -0.05, 1.9, -0.4, 1.9]
    swept = geom.swept_floor_clearance(a, _HOME, lambda _x, _y: 0.0)
    assert swept is not None
    assert swept <= geom.floor_clearance(a, lambda _x, _y: 0.0) + 1e-12
    assert swept <= geom.floor_clearance(_HOME, lambda _x, _y: 0.0) + 1e-12


def test_the_measured_worst_start_is_detected_as_a_table_press():
    """The real witness from the 2026-07-27 sweep: this start's straight glide to
    HOME drives a finger 167.9 mm below the table on the meshes. The model must
    see it. Fails if the box table or the sweep regresses — i.e. this is the
    test that proves the guard detects the defect it was built for."""
    geom = AG.resolve_geometry(_ctx())
    start = [-0.131, 2.997, -0.020, 1.964, -0.391, 1.898]
    swept = geom.swept_floor_clearance(start, _HOME, lambda _x, _y: 0.0)
    assert swept is not None
    assert swept < -0.10, f'expected a deep press, model saw {swept:.4f} m'

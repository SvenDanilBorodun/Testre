"""``EDU1_LINK_BOXES`` — the Edu:1 half of the whole-link geometry model.

The load-bearing test is the containment one: every collision-mesh vertex of
every link, at every jaw opening, must lie inside that link's shipped box. The
boxes are a SOUND over-approximation or they are nothing — an optimistic box
makes the table-floor guard say "clear" about a link that is pressing into the
desk, which is the one direction it must never be.

Derived independently here (a generic URDF + STL reader) from the constants in
``arm_geometry.py``, so a transcription slip cannot verify itself.
"""

from __future__ import annotations

import itertools
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest

import physical_ai_server.workflow.arm_geometry as AG
from physical_ai_server.workflow.edu1_ik import Edu1IKSolver

_URDF_DIR = (Path(__file__).resolve().parents[2]
             / 'physical_ai_manager' / 'public' / 'edu1-urdf')
_URDF = _URDF_DIR / 'edu1.urdf'

# Index i of EDU1_LINK_BOXES is the frame Edu1IKSolver.link_frames() returns at
# position i. Index 5 additionally absorbs the whole gripper cluster.
_BOX_LINKS = ['base_link', 'link1', 'link2', 'link3', 'link4', 'link5']
_GRIPPER_CHILDREN = ['end_effector', 'right_finger', 'left_finger']
_CLAW_BAND = (0.0, 1.5708)


class _Ctx:
    """Minimal ctx stand-in: resolve_geometry only ever reads ``.ik``."""

    def __init__(self, ik):
        self.ik = ik


def _rpy(r, p, y):
    cr, sr = np.cos(r), np.sin(r)
    cp, sp = np.cos(p), np.sin(p)
    cy, sy = np.cos(y), np.sin(y)
    return (np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]]) @
            np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]]) @
            np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]]))


def _aa(axis, th):
    a = np.asarray(axis, float)
    norm = float(np.linalg.norm(a))
    if norm == 0.0:
        # A FIXED joint (``end_joint``) carries axis "0 0 0". Normalising that
        # yields NaN and poisons every frame downstream of it — which is
        # end_effector and both fingers, i.e. exactly the cluster this file is
        # here to bound.
        return np.eye(3)
    a = a / norm
    k = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    return np.eye(3) + np.sin(th) * k + (1 - np.cos(th)) * (k @ k)


def _load_stl(path: Path) -> np.ndarray:
    raw = path.read_bytes()
    count = struct.unpack('<I', raw[80:84])[0]
    body = np.frombuffer(raw[84:84 + count * 50],
                         dtype=np.uint8).reshape(count, 50)
    return np.frombuffer(body[:, 12:48].tobytes(),
                         dtype='<f4').reshape(-1, 3).astype(np.float64)


class _Urdf:
    """Generic URDF reader — an independent path to the same geometry."""

    def __init__(self, path: Path = _URDF) -> None:
        assert path.exists(), f'in-repo URDF missing at {path}'
        root = ET.parse(path).getroot()
        self.joints = {}
        for el in root.findall('joint'):
            o = el.find('origin')
            ax = el.find('axis')
            self.joints[el.get('name')] = {
                'parent': el.find('parent').get('link'),
                'child': el.find('child').get('link'),
                'xyz': np.array([float(v) for v in o.get('xyz').split()]),
                'rpy': np.array([float(v) for v in o.get('rpy').split()]),
                'axis': (np.array([float(v) for v in ax.get('xyz').split()])
                         if ax is not None else np.array([0.0, 0, 1])),
            }
        self.meshes = {}
        for link in root.findall('link'):
            name = link.get('name')
            for tag in ('collision', 'visual'):
                el = link.find(f'{tag}/geometry/mesh')
                if el is not None:
                    self.meshes[name] = el.get('filename').split('/')[-1]
                    break

    def vertices(self, link: str) -> np.ndarray:
        assert link in self.meshes, f'{link} has no mesh in {_URDF}'
        return _load_stl(_URDF_DIR / 'meshes' / self.meshes[link])

    def frames(self, q: dict) -> dict:
        """``{link: 4x4}`` in the URDF base frame, resolved parent-first."""
        out = {'base_link': np.eye(4)}
        pending = dict(self.joints)
        while pending:
            progressed = False
            for name, j in list(pending.items()):
                if j['parent'] not in out:
                    continue
                f = np.eye(4)
                f[:3, :3] = _rpy(*j['rpy'])
                f[:3, 3] = j['xyz']
                r = np.eye(4)
                r[:3, :3] = _aa(j['axis'], q.get(name, 0.0))
                out[j['child']] = out[j['parent']] @ f @ r
                del pending[name]
                progressed = True
            assert progressed, f'unresolvable joint tree: {list(pending)}'
        return out


def test_boxes_contain_every_mesh_vertex():
    """THE point of this file (see the module docstring)."""
    urdf = _Urdf()
    for idx, link in enumerate(_BOX_LINKS):
        lo, hi = (np.asarray(v, float) for v in AG.EDU1_LINK_BOXES[idx])
        pts = urdf.vertices(link)
        assert (pts >= lo - 1e-9).all(), f'{link} pokes out below its box'
        assert (pts <= hi + 1e-9).all(), f'{link} pokes out above its box'

    # The gripper children, expressed in link5's frame, over the WHOLE claw
    # band: the fingers ride RL_joint (and its LF_joint <mimic>) and are not on
    # the arm's own FK chain, so index 5 has to cover them at every opening.
    lo5, hi5 = (np.asarray(v, float) for v in AG.EDU1_LINK_BOXES[5])
    for claw in np.linspace(_CLAW_BAND[0], _CLAW_BAND[1], 17):
        frames = urdf.frames({'RL_joint': claw, 'LF_joint': claw})
        t5 = frames['link5']
        for child in _GRIPPER_CHILDREN:
            world = (urdf.vertices(child) @ frames[child][:3, :3].T
                     + frames[child][:3, 3])
            local = (world - t5[:3, 3]) @ t5[:3, :3]
            assert (local >= lo5 - 1e-9).all(), (
                f"{child} escapes link5's box below at claw={claw:.3f}")
            assert (local <= hi5 + 1e-9).all(), (
                f"{child} escapes link5's box above at claw={claw:.3f}")


def test_the_box_table_has_one_entry_per_frame_the_solver_returns():
    """A table one entry short or long silently mis-pairs boxes with links."""
    ik = Edu1IKSolver()
    frames = ik.link_frames([0.0] * 5)
    assert len(AG.EDU1_LINK_BOXES) == len(frames) == len(_BOX_LINKS)


def test_link_frames_match_the_independent_urdf_oracle():
    """Guards the frame convention the boxes are expressed in: the solver
    reports WORLD frames (URDF rotated 180° about z) and the boxes are in the
    LINK frames. Comparing the two directly is the bug that was hit while
    deriving the edu6 boxes."""
    urdf = _Urdf()
    ik = Edu1IKSolver()
    rz = np.array([[-1.0, 0, 0], [0, -1.0, 0], [0, 0, 1.0]])
    rng = np.random.default_rng(9)
    for _ in range(12):
        q = [float(rng.uniform(lo, hi)) for (lo, hi) in ik.joint_limits]
        got = ik.link_frames(q)
        want = urdf.frames({f'joint{i + 1}': q[i] for i in range(5)})
        assert got is not None and len(got) == 6
        for idx, link in enumerate(_BOX_LINKS):
            expect = np.eye(4)
            expect[:3, :3] = rz @ want[link][:3, :3]
            expect[:3, 3] = rz @ want[link][:3, 3]
            assert np.allclose(got[idx], expect, atol=1e-9), link


def test_z_zero_is_the_table_for_this_arm():
    """The whole joint-space floor guard rests on it: base_link's own mesh
    starts at z = 0, i.e. the arm is bolted to the table surface."""
    verts = _Urdf().vertices('base_link')
    assert float(verts[:, 2].min()) == pytest.approx(0.0, abs=1e-6)
    assert float(verts[:, 2].max()) < 0.06


def test_resolve_geometry_picks_the_edu1_table_by_backend():
    geom = AG.resolve_geometry(_Ctx(Edu1IKSolver()))
    assert geom is not None
    assert geom.num_links == len(AG.EDU1_LINK_BOXES)


def test_self_pairs_are_sized_off_this_arms_table_not_the_edu6s():
    """A module-level ``_SELF_PAIRS`` sized off the 7-link edu6 table would
    index past the end of a 6-link arm's corner list (or, sized the other way,
    silently skip the edu6's last link)."""
    assert AG._self_pairs(6) == tuple(
        (i, j) for i, j in itertools.combinations(range(6), 2) if j - i >= 2)
    assert (4, 5) not in AG._self_pairs(6)      # adjacent links never tested
    assert (0, 5) in AG._self_pairs(6)          # base vs the gripper cluster
    geom = AG.resolve_geometry(_Ctx(Edu1IKSolver()))
    assert geom.self_clearance([0.0, 0.64, 1.48, 0.90, 0.0]) is not None


def test_floor_clearance_at_home_is_positive_and_finite():
    """HOME is the pose the driver glides to on EVERY boot."""
    geom = AG.resolve_geometry(_Ctx(Edu1IKSolver()))
    gap = geom.floor_clearance([0.0, 0.64, 1.48, 0.90, 0.0], lambda x, y: 0.0)
    assert gap is not None
    assert 0.03 < gap < 0.06, gap


def test_floor_clearance_is_a_sound_lower_bound_on_the_real_meshes():
    """Model ≤ truth over random in-limit poses, at random jaw openings — the
    guarantee the guard is built on. Never optimistic, only pessimistic."""
    urdf = _Urdf()
    ik = Edu1IKSolver()
    geom = AG.resolve_geometry(_Ctx(ik))
    rng = np.random.default_rng(4242)
    rz = np.array([[-1.0, 0, 0], [0, -1.0, 0], [0, 0, 1.0]])
    worst_slack = None
    for _ in range(120):
        q = [float(rng.uniform(lo, hi)) for (lo, hi) in ik.joint_limits]
        claw = float(rng.uniform(*_CLAW_BAND))
        qmap = {f'joint{i + 1}': q[i] for i in range(5)}
        qmap.update({'RL_joint': claw, 'LF_joint': claw})
        frames = urdf.frames(qmap)
        truth = None
        for link in _BOX_LINKS[1:] + _GRIPPER_CHILDREN:
            world = (urdf.vertices(link) @ frames[link][:3, :3].T
                     + frames[link][:3, 3]) @ rz.T
            low = float(world[:, 2].min())
            truth = low if truth is None else min(truth, low)
        model = geom.floor_clearance(q, lambda x, y: 0.0)
        assert model is not None
        assert model <= truth + 1e-9, (
            f'model {model} is OPTIMISTIC against truth {truth} at {q}')
        slack = truth - model
        worst_slack = slack if worst_slack is None else max(worst_slack, slack)
    # Sound is mandatory; USABLE means the pessimism stays small enough that the
    # 20 mm boot-home allowance is not swamped by it. Measured over 400 random
    # in-limit poses at random jaw openings (2026-09-05): sound 400/400,
    # conservatism median 0.0 mm, mean 12.8 mm, p95 51.0 mm, max 71.1 mm — the
    # same shape as the edu6 table's (median 0.0, mean 10.1, p95 45.9). The
    # bound below is the max with headroom, not a target.
    assert worst_slack < 0.09, worst_slack

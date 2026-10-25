"""``V1_LINK_BOXES`` — the V1's whole-link geometry model.

The load-bearing test is the containment one: every collision-mesh vertex of
every link, at every jaw opening, must lie inside that link's shipped box. The
boxes are a SOUND over-approximation or they are nothing — an optimistic box
makes the table-floor guard say "clear" about a link that is pressing into the
desk, which is the one direction it must never be.

Derived independently here (a generic URDF + STL reader) from the constants in
``arm_geometry.py``, so a transcription slip cannot verify itself. The left
blade rides ``RL_joint`` through the URDF's ``<mimic>`` multiplier (−18/19, the
claw gears), read from the URDF; every claw test also runs at multiplier −1,
because the ratio is a CAD value until rig gate VR16 measures it and the boxes
must hold at both.
"""

from __future__ import annotations

import itertools
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest

import physical_ai_server.workflow.arm_geometry as AG
from physical_ai_server.workflow.v1_ik import V1IKSolver

_URDF_DIR = (Path(__file__).resolve().parents[2]
             / 'physical_ai_manager' / 'public' / 'v1-urdf')
_URDF = _URDF_DIR / 'v1.urdf'

# Index i of V1_LINK_BOXES is the frame V1IKSolver.link_frames() returns at
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
            mim = el.find('mimic')
            self.joints[el.get('name')] = {
                'parent': el.find('parent').get('link'),
                'child': el.find('child').get('link'),
                'xyz': np.array([float(v) for v in o.get('xyz').split()]),
                'rpy': np.array([float(v) for v in o.get('rpy').split()]),
                'axis': (np.array([float(v) for v in ax.get('xyz').split()])
                         if ax is not None else np.array([0.0, 0, 1])),
                'mimic': ((mim.get('joint'), float(mim.get('multiplier', 1.0)))
                          if mim is not None else None),
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


_URDF_LF_MULTIPLIER = _Urdf().joints['LF_joint']['mimic'][1]
# The shipped gear ratio and the plain −1 a direct mesh would give: every claw
# test runs at both (see the module docstring).
_LF_MULTIPLIERS = (_URDF_LF_MULTIPLIER, -1.0)


def _claw(q: dict, claw: float, multiplier: float = _URDF_LF_MULTIPLIER) -> dict:
    """``q`` with the claw at ``claw``: RL_joint itself, LF_joint by its mimic."""
    out = dict(q)
    out.update({'RL_joint': claw, 'LF_joint': multiplier * claw})
    return out


def test_boxes_contain_every_mesh_vertex():
    """THE point of this file (see the module docstring)."""
    urdf = _Urdf()
    for idx, link in enumerate(_BOX_LINKS):
        lo, hi = (np.asarray(v, float) for v in AG.V1_LINK_BOXES[idx])
        pts = urdf.vertices(link)
        assert (pts >= lo - 1e-9).all(), f'{link} pokes out below its box'
        assert (pts <= hi + 1e-9).all(), f'{link} pokes out above its box'

    # The gripper children, expressed in link5's frame, over the WHOLE claw
    # band at BOTH gear ratios: the fingers ride RL_joint (and its LF_joint
    # <mimic>) and are not on the arm's own FK chain, so index 5 has to cover
    # them at every opening.
    lo5, hi5 = (np.asarray(v, float) for v in AG.V1_LINK_BOXES[5])
    for multiplier in _LF_MULTIPLIERS:
        for claw in np.linspace(_CLAW_BAND[0], _CLAW_BAND[1], 33):
            frames = urdf.frames(_claw({}, float(claw), multiplier))
            t5 = frames['link5']
            for child in _GRIPPER_CHILDREN:
                world = (urdf.vertices(child) @ frames[child][:3, :3].T
                         + frames[child][:3, 3])
                local = (world - t5[:3, 3]) @ t5[:3, :3]
                assert (local >= lo5 - 1e-9).all(), (
                    f"{child} escapes link5's box below at claw={claw:.3f}, "
                    f'LF multiplier {multiplier}')
                assert (local <= hi5 + 1e-9).all(), (
                    f"{child} escapes link5's box above at claw={claw:.3f}, "
                    f'LF multiplier {multiplier}')


def test_the_box_table_has_one_entry_per_frame_the_solver_returns():
    """A table one entry short or long silently mis-pairs boxes with links."""
    ik = V1IKSolver()
    frames = ik.link_frames([0.0] * 5)
    assert len(AG.V1_LINK_BOXES) == len(frames) == len(_BOX_LINKS)


def test_link_frames_match_the_independent_urdf_oracle():
    """Guards the frame convention the boxes are expressed in: the solver
    reports WORLD frames (URDF rotated 180° about z) and the boxes are in the
    LINK frames. Comparing the two directly is the classic bug when deriving
    such boxes."""
    urdf = _Urdf()
    ik = V1IKSolver()
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
    starts at z = 0, i.e. the arm is bolted to the table surface. (The shipped
    URDF re-origins the CAD's base frame by +6 mm to make that true.)"""
    verts = _Urdf().vertices('base_link')
    assert float(verts[:, 2].min()) == pytest.approx(0.0, abs=1e-6)
    assert float(verts[:, 2].max()) < 0.06
    assert _Urdf().joints['joint1']['xyz'] == pytest.approx((0.0, 0.0, 0.0502))


def test_resolve_geometry_picks_the_v1_table_by_backend():
    geom = AG.resolve_geometry(_Ctx(V1IKSolver()))
    assert geom is not None
    assert geom.num_links == len(AG.V1_LINK_BOXES)


def test_self_pairs_are_sized_off_this_arms_table():
    """Pairs are sized off THIS arm's table: a pair set sized off a longer table
    would index past the end of a 6-link arm's corner list (or, sized the other
    way, silently skip a link)."""
    assert AG._self_pairs(6) == tuple(
        (i, j) for i, j in itertools.combinations(range(6), 2) if j - i >= 2)
    assert (4, 5) not in AG._self_pairs(6)      # adjacent links never tested
    assert (0, 5) in AG._self_pairs(6)          # base vs the gripper cluster
    geom = AG.resolve_geometry(_Ctx(V1IKSolver()))
    assert geom.self_clearance([0.0, 0.64, 1.48, 0.90, 0.0]) is not None


def test_floor_clearance_at_home_is_positive_and_finite():
    """HOME is the pose the driver glides to on EVERY boot."""
    geom = AG.resolve_geometry(_Ctx(V1IKSolver()))
    gap = geom.floor_clearance([0.0, 0.64, 1.48, 0.90, 0.0], lambda x, y: 0.0)
    assert gap is not None
    assert 0.03 < gap < 0.06, gap


def _lowest(urdf, verts, qmap, links=None):
    """Lowest WORLD z of the given links' mesh vertices (the solver's world is
    URDF rotZ(π), which leaves z unchanged: row 2 of each frame)."""
    frames = urdf.frames(qmap)
    return min(float((v @ frames[link][2, :3]).min() + frames[link][2, 3])
               for link, v in verts.items() if links is None or link in links)


def _lowest_both_ratios(urdf, verts, q, claw, links=None):
    """The claw's lowest point over BOTH gear ratios — the truth a floor check
    may rely on while the ratio is unmeasured (rig gate VR16)."""
    base = {f'joint{i + 1}': q[i] for i in range(5)}
    return min(_lowest(urdf, verts, _claw(base, claw, m), links)
               for m in _LF_MULTIPLIERS)


def _unique_verts(urdf):
    # Duplicates only (an STL repeats each vertex per triangle).
    return {link: np.unique(urdf.vertices(link), axis=0)
            for link in _BOX_LINKS[1:] + _GRIPPER_CHILDREN}


def test_floor_clearance_is_exact_at_home():
    """At HOME the model equals the mesh truth (+43.7 mm, link1's structural
    floor) rather than merely bounding it, at any claw opening — the claw
    points up there. A regression here means the model has become loose enough
    to start refusing legitimate poses."""
    urdf = _Urdf()
    verts = _unique_verts(urdf)
    home = [0.0, 0.64, 1.48, 0.90, 0.0]
    geom = AG.resolve_geometry(_Ctx(V1IKSolver()))
    model = geom.floor_clearance(home, lambda x, y: 0.0)
    for claw in (0.0, 0.90):
        assert model == pytest.approx(_lowest_both_ratios(urdf, verts, home, claw),
                                      abs=2e-4)
    assert model == pytest.approx(0.0437, abs=5e-4)


def test_floor_clearance_is_a_sound_lower_bound_on_the_real_meshes():
    """Model ≤ truth over random in-limit poses, at random jaw openings and both
    gear ratios — the guarantee the guard is built on. Never optimistic, only
    pessimistic."""
    urdf = _Urdf()
    ik = V1IKSolver()
    geom = AG.resolve_geometry(_Ctx(ik))
    verts = _unique_verts(urdf)
    rng = np.random.default_rng(4242)
    slacks = []
    for _ in range(120):
        q = [float(rng.uniform(lo, hi)) for (lo, hi) in ik.joint_limits]
        claw = float(rng.uniform(*_CLAW_BAND))
        truth = _lowest_both_ratios(urdf, verts, q, claw)
        model = geom.floor_clearance(q, lambda x, y: 0.0)
        assert model is not None
        assert model <= truth + 1e-9, (
            f'model {model} is OPTIMISTIC against truth {truth} at {q}')
        slacks.append(truth - model)
    # Sound is mandatory; USABLE means the pessimism stays small enough that the
    # 20 mm boot-home allowance is not swamped by it in the common case.
    # Measured 2026-10-25 over 400 random in-limit poses per seed (seeds 4242,
    # 1, 2) at random jaw openings, both gear ratios: sound 1200/1200,
    # conservatism median 0.0 mm, mean 16.8–17.5 mm, p95 61–70 mm, max
    # 89.6–92.0 mm (the open claw's box is large). THIS sample's max is 89.9 mm,
    # just under the bound: re-derive the bound from a larger sample before
    # changing the seed or the count.
    assert max(slacks) < 0.09, max(slacks)


def test_the_position_floor_check_never_refuses_a_pose_the_meshes_clear():
    """``motion._refuse_position_below_table`` judges a taught Position by the
    POINT model (``home_planner.pose_floor_clearance``: the joint points and
    the fingertip at the carried claw opening, never these boxes). Over a
    seeded sample of in-limit poses at random claw openings in the band the
    arm commands (``gripper_closed_rad`` … ``gripper_open_rad``), every pose
    whose real meshes are clear of the table AT THAT OPENING (at both gear
    ratios) passes. Measured 2026-10-25 (seed 1019, 6000 draws): 3849 clear,
    162 of them within 20 mm of the table, claw tilt 0.3–178.6°. A claw opened
    wider by hand is the next test."""
    from physical_ai_server import robot_profiles as rp
    from physical_ai_server.workflow.handlers import motion as M

    class _Ctx:
        ik = V1IKSolver()
        z_table = None
        table_plane = None

    prof = rp.ROBOT_PROFILES['v1_studio']
    claw_band = (float(prof.gripper_closed_rad), float(prof.gripper_open_rad))
    urdf = _Urdf()
    ik = _Ctx.ik
    verts = _unique_verts(urdf)
    rng = np.random.default_rng(1019)
    clear = 0
    near_tilts = []
    for _ in range(6000):
        q = [float(rng.uniform(lo, hi)) for (lo, hi) in ik.joint_limits]
        claw = float(rng.uniform(*claw_band))
        truth = _lowest_both_ratios(urdf, verts, q, claw)
        if truth < 0.0:
            continue
        clear += 1
        try:
            M._refuse_position_below_table(_Ctx(), q + [claw], 'P')
        except M.WorkflowError:
            raise AssertionError(
                f'refused a pose the meshes clear by {truth * 1000:.1f} mm: '
                f'q={q}, claw={claw}') from None
        if truth <= 0.02:
            rot, _t = ik.fk(q)
            axis = np.asarray(rot) @ np.asarray(ik.approach_axis_local)
            near_tilts.append(float(np.degrees(np.arccos(np.clip(-axis[2], -1.0, 1.0)))))
    assert clear > 3000, clear
    assert len(near_tilts) > 100, len(near_tilts)
    # The near-table poses span the tilts the box model refused (10–90°).
    assert min(near_tilts) < 10.0 and max(near_tilts) > 90.0, (min(near_tilts), max(near_tilts))


def test_the_position_floor_check_never_refuses_a_hand_opened_claw_the_meshes_clear():
    """A claw opened by HAND while the arm is limp can stand anywhere up to its
    window's 1.5708 rad and keeps that opening after the re-lock, so a Position
    is judged at claw values past the commanded band too. ``tool_tip_rise_m``
    covers the whole window. Over seeded in-limit poses at claw openings
    0.9–1.5708 rad, judged by the mesh oracle at both gear ratios: no pose clear
    of the flat z = 0 table is refused, and no pose is refused against a flat
    table moved up or down to its OWN lowest mesh point (every draw touching).
    Measured 2026-10-25 (seed 1020, 3000 draws): 1963 clear at z = 0, 0
    refused; 3000 touching, 0 refused."""
    from physical_ai_server.workflow.handlers import motion as M

    class _Ctx:
        def __init__(self, z_table=None):
            self.ik = _IK
            self.z_table = z_table
            self.table_plane = None

    _IK = V1IKSolver()
    urdf = _Urdf()
    verts = _unique_verts(urdf)
    rng = np.random.default_rng(1020)
    clear = 0
    for _ in range(3000):
        q = [float(rng.uniform(lo, hi)) for (lo, hi) in _IK.joint_limits]
        claw = float(rng.uniform(0.9, 1.5708))
        truth = _lowest_both_ratios(urdf, verts, q, claw)
        cases = [(_Ctx(truth), 'touching its own table')]
        if truth >= 0.0:
            cases.append((_Ctx(), 'clear of z = 0'))
        for ctx, what in cases:
            try:
                M._refuse_position_below_table(ctx, q + [claw], 'P')
            except M.WorkflowError:
                raise AssertionError(f'refused a pose {what} (mesh {truth * 1000:.1f} mm '
                                     f'above z = 0): q={q}, claw={claw}') from None
        clear += truth >= 0.0
    assert clear > 1500, clear


def test_the_replay_floor_credit_never_refuses_a_pose_touching_its_table():
    """The two replay floor checks judge the TCP's z plus
    ``motion.tool_tip_rise_world_z_m``: the claw's rise set back ALONG the tool
    axis, the very tip the Position check judges. Over seeded in-limit poses at
    claw openings across the whole window (0–1.5708 rad), judged against a flat
    table at each pose's OWN lowest mesh point (both gear ratios), none is
    refused; and the tilted, hand-opened pose ``test_manual_relock_home_glide``
    pins really has its claw under the table (41.7 mm). Measured 2026-10-25
    (seed 1022, 3000 draws): 0 refused."""
    from physical_ai_server.workflow.handlers import motion as M

    ik = V1IKSolver()
    urdf = _Urdf()
    verts = _unique_verts(urdf)
    rng = np.random.default_rng(1022)
    for _ in range(3000):
        q = [float(rng.uniform(lo, hi)) for (lo, hi) in ik.joint_limits]
        claw = float(rng.uniform(0.0, 1.5708))
        rot, t = ik.fk(q)
        judged = float(t[2]) + M.tool_tip_rise_world_z_m(ik, q + [claw], 5, rot)
        assert judged >= _lowest_both_ratios(urdf, verts, q, claw) - M.WORKSPACE_FLOOR_MARGIN_M, (q, claw)
    q, claw = [-0.027, 2.237, 1.491, 0.096, -0.103], 1.5
    assert _lowest_both_ratios(urdf, verts, q, claw,
                               ('link5', 'end_effector', 'right_finger', 'left_finger')) < -0.03

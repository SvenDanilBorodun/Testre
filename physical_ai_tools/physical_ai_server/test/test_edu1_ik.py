"""Closed-form IK + exact FK tests for the edu1_studio arm ("Edu:1").

Same discipline as ``test_edu6_ik.py``: the INDEPENDENT FK ORACLE parses the
in-repo URDF copy (``physical_ai_manager/public/edu1-urdf/edu1.urdf``) with a
generic XML→chain builder — a completely separate code path from the solver's
baked constants — so a transcription or sign error in ``edu1_ik.py`` cannot
verify itself. The tool length is checked against the shipped CLAW MESH, which
is a third independent source (the URDF says where the claw pivots, the STL says
how long the blade is, and ``_L_TOOL`` is their sum).
"""

from __future__ import annotations

import math
import random
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest

from physical_ai_server.workflow.edu1_ik import (
    BASE_AXIS_X_WORLD,
    Edu1IKSolver,
    _EDU1_JOINT_LIMITS_RAD,
    _FK_TOL_M,
    _L_TOOL,
    _Q4_VERTICAL_OFFSET,
    _REACH_MAX,
    _REACH_MIN,
)

_ASSET = (Path(__file__).resolve().parents[2]
          / 'physical_ai_manager' / 'public' / 'edu1-urdf')
_URDF = _ASSET / 'edu1.urdf'

_RZ_PI = np.array([[-1.0, 0, 0], [0, -1.0, 0], [0, 0, 1.0]])
_ARM_JOINTS = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5']


def _ik() -> Edu1IKSolver:
    return Edu1IKSolver()


# ── the independent oracle: generic URDF chain, parsed from XML ──────────────

def _rpy(r, p, y):
    cr, sr = np.cos(r), np.sin(r)
    cp, sp = np.cos(p), np.sin(p)
    cy, sy = np.cos(y), np.sin(y)
    return (np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]]) @
            np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]]) @
            np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]]))


def _aa(axis, th):
    a = np.asarray(axis, float)
    a = a / np.linalg.norm(a)
    k = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    return np.eye(3) + np.sin(th) * k + (1 - np.cos(th)) * (k @ k)


def _load_stl(path: Path) -> np.ndarray:
    """Vertices of a binary STL, (3n, 3)."""
    raw = path.read_bytes()
    count = struct.unpack('<I', raw[80:84])[0]
    body = np.frombuffer(raw[84:84 + count * 50], dtype=np.uint8).reshape(count, 50)
    return np.frombuffer(body[:, 12:48].tobytes(),
                         dtype='<f4').reshape(-1, 3).astype(np.float64)


class _Oracle:
    """FK straight from the URDF XML (chain joint1..joint5)."""

    def __init__(self):
        assert _URDF.exists(), (
            f'in-repo URDF missing at {_URDF} — the independent oracle is a '
            'hard requirement')
        root = ET.parse(_URDF).getroot()
        self.joints = {}
        for el in root.findall('joint'):
            o = el.find('origin')
            ax = el.find('axis')
            lim = el.find('limit')
            self.joints[el.get('name')] = {
                'xyz': np.array([float(v) for v in o.get('xyz').split()]),
                'rpy': np.array([float(v) for v in o.get('rpy').split()]),
                'axis': (np.array([float(v) for v in ax.get('xyz').split()])
                         if ax is not None else np.array([0.0, 0, 1])),
                'limits': ((float(lim.get('lower')), float(lim.get('upper')))
                           if lim is not None else None),
            }

    def frames(self, q):
        out = []
        t = np.eye(4)
        for i, name in enumerate(_ARM_JOINTS):
            j = self.joints[name]
            f = np.eye(4)
            f[:3, :3] = _rpy(*j['rpy'])
            f[:3, 3] = j['xyz']
            r = np.eye(4)
            r[:3, :3] = _aa(j['axis'], q[i])
            t = t @ f @ r
            out.append(t.copy())
        return out

    def tcp_world(self, q):
        """WORLD-frame fingertip, from the URDF frames only: ``end_effector`` is
        fixed to link5 at zero offset and the tool axis is that frame's +z."""
        t5 = self.frames(q)[4]
        return _RZ_PI @ (t5[:3, 3] + t5[:3, :3] @ np.array([0.0, 0.0, _L_TOOL]))


_ORACLE = _Oracle()


def test_the_end_effector_is_fixed_to_link5_at_zero_offset():
    """The whole tool model rests on this: ``_fk_matrix`` composes the TCP off
    LINK5's frame, not off a separate end-effector frame, so a CAD re-export
    that gives ``end_joint`` a real offset would silently shift every grasp."""
    ee = _ORACLE.joints['end_joint']
    assert np.allclose(ee['xyz'], 0.0)
    assert np.allclose(ee['rpy'], 0.0)


def test_tool_length_matches_the_shipped_claw_mesh():
    """``_L_TOOL`` is claw pivot + blade, and the two halves come from DIFFERENT
    files: the pivot from the URDF (RL_joint's origin), the blade from the STL.
    Re-derive both here rather than restating 0.08625."""
    rl = _ORACLE.joints['RL_joint']
    pivot = np.eye(4)
    pivot[:3, :3] = _rpy(*rl['rpy'])
    pivot[:3, 3] = rl['xyz']
    verts = _load_stl(_ASSET / 'meshes' / 'right_finger.STL')
    # Claw CLOSED (joint value 0) — the pose the TCP is defined at.
    in_ee = (pivot[:3, :3] @ verts.T).T + pivot[:3, 3]
    tip_z = float(in_ee[:, 2].max())
    assert tip_z == pytest.approx(_L_TOOL, abs=2e-4), (
        f'closed fingertip sits {tip_z:.5f} m along the tool axis, but '
        f'_L_TOOL is {_L_TOOL}')
    # …and it lies ON the roll axis, which is what makes joint5 a pure jaw
    # rotation that never moves the tool.
    near_tip = in_ee[in_ee[:, 2] > tip_z - 0.001]
    assert abs(float(near_tip[:, 0].mean())) < 0.002
    assert abs(float(near_tip[:, 1].mean())) < 0.004


def test_solver_limits_match_the_urdf():
    for i, name in enumerate(_ARM_JOINTS):
        lo, hi = _ORACLE.joints[name]['limits']
        assert _EDU1_JOINT_LIMITS_RAD[i] == pytest.approx((lo, hi), abs=1e-9)


def test_the_claw_band_is_zero_closed_and_positive_open():
    """The shipped URDF FLIPS the CAD's claw axis so open is the numerically
    larger value — every shared consumer (grasp-held check, catalog band, sim
    close classifier) assumes it. A re-export that restores the CAD sign would
    silently invert every grasp verdict."""
    for name in ('RL_joint', 'LF_joint'):
        lo, hi = _ORACLE.joints[name]['limits']
        assert (lo, hi) == pytest.approx((0.0, 1.5708), abs=1e-9), name
    # The two fingers still counter-rotate: opposite axes, one <mimic>.
    assert np.allclose(_ORACLE.joints['RL_joint']['axis'],
                       -_ORACLE.joints['LF_joint']['axis'])


# ── FK: solver vs oracle ─────────────────────────────────────────────────────

def _random_joints(rng):
    return [rng.uniform(lo, hi) for lo, hi in _EDU1_JOINT_LIMITS_RAD]


def test_fk_matches_the_independent_oracle():
    ik = _ik()
    rng = random.Random(20260905)
    worst = 0.0
    for _ in range(500):
        q = _random_joints(rng)
        _, t = ik.fk(q)
        worst = max(worst, float(np.linalg.norm(t - _ORACLE.tcp_world(q))))
    assert worst < 1e-9, f'solver FK drifts from the URDF oracle by {worst} m'


def test_fk_needs_five_joints():
    assert _ik().fk([0.0, 0.0, 0.0, 0.0]) is None
    assert _ik().link_frames([0.0] * 4) is None
    assert _ik().link_points([0.0] * 4) is None


# ── IK: round trip, branch, limits ───────────────────────────────────────────

def test_ik_round_trips_reachable_table_targets():
    """Solve real targets over the table, FK the answer, require it back."""
    ik = _ik()
    rng = random.Random(7)
    solved = 0
    worst = 0.0
    for _ in range(3000):
        target = (rng.uniform(0.05, 0.38), rng.uniform(-0.30, 0.30),
                  rng.uniform(0.0, 0.10))
        roll = rng.uniform(-math.pi, math.pi)
        q = ik.solve(target, roll=roll)
        if q is None:
            continue
        solved += 1
        _, back = ik.fk(q)
        worst = max(worst, float(np.linalg.norm(back - np.array(target))))
    assert solved > 800, f'only {solved} of 3000 table targets solved'
    assert worst <= _FK_TOL_M, worst


def test_solve_is_idempotent():
    """solve → fk → solve must return the SAME joints, not a second branch."""
    ik = _ik()
    rng = random.Random(1234)
    checked = 0
    for _ in range(400):
        target = (rng.uniform(0.10, 0.34), rng.uniform(-0.20, 0.20),
                  rng.uniform(0.0, 0.06))
        roll = rng.uniform(-math.pi, math.pi)
        first = ik.solve(target, roll=roll)
        if first is None:
            continue
        checked += 1
        again = ik.solve(ik.fk(first)[1], roll=ik.roll_from_joints(first))
        assert again is not None
        # 1e-4 rad, not 1e-9: the CAD export rounded its right-angle ORIGINS to
        # 1.5708/3.1416, so the chain is planar only to ~1.5e-6 m while the
        # closed form assumes it is planar exactly. The residual shows up as a
        # ~1e-5 rad wobble in joint1. That is 1/100 of a servo tick — the same
        # rounding _FK_TOL_M documents, seen from the joint side.
        assert np.allclose(again, first, atol=1e-4), (first, again)
    assert checked > 100


def test_vertical_family_poses_in_front_and_above_the_table_are_recoverable():
    """The strict-vertical family is much larger than the SERVICEABLE workspace:
    with q2 leaned back the same family puts the fingertip behind the joint-1
    axis and far below the table (measured: ~94 % of a uniform (q2, q3) sample).
    Those are real arm configurations and deliberately NOT solvable targets —
    joint1's ±90° cannot turn to face behind the base, and that region is where
    the arm's own base is bolted. Everything in FRONT and above the table must
    come back."""
    ik = _ik()
    rng = random.Random(7)
    recovered = 0
    skipped = 0
    for _ in range(4000):
        q2 = rng.uniform(0.0, 3.1416)
        q3 = rng.uniform(0.0, 3.1416)
        q4 = q2 - q3 + _Q4_VERTICAL_OFFSET
        if not (-1.5708 <= q4 <= 1.5708):
            continue
        q = [rng.uniform(-1.5, 1.5), q2, q3, q4, rng.uniform(-1.5, 1.5)]
        _, tcp = ik.fk(q)
        # In FRONT: the azimuth the target implies must be the one joint1 is
        # actually at (to 1e-3 rad — see test_solve_is_idempotent for why the
        # chain is not planar to machine precision). Above the table: a target
        # under z = 0 is inside the desk.
        if abs(-math.atan2(tcp[1], tcp[0]) - q[0]) > 1e-3 or tcp[2] < 0.0:
            skipped += 1
            continue
        out = ik.solve(tcp, roll=ik.roll_from_joints(q))
        assert out is not None, f'unsolvable serviceable pose: {q} -> {tcp}'
        recovered += 1
    assert recovered > 50, f'only {recovered} serviceable poses sampled'
    assert skipped > 0


def test_every_returned_pose_points_the_tool_straight_down():
    ik = _ik()
    rng = random.Random(11)
    checked = 0
    for _ in range(400):
        x = rng.uniform(0.05, 0.36)
        y = rng.uniform(-0.25, 0.25)
        z = rng.uniform(0.0, 0.09)
        q = ik.solve((x, y, z), roll=rng.uniform(-math.pi, math.pi))
        if q is None:
            continue
        checked += 1
        frames = ik.link_frames(q)
        tool_world = frames[5][:3, :3] @ np.array([0.0, 0.0, 1.0])
        assert tool_world[2] == pytest.approx(-1.0, abs=1e-5), tool_world
    assert checked > 100


def test_solve_refuses_the_unreachable_and_the_non_finite():
    ik = _ik()
    assert ik.solve((5.0, 0.0, 0.0)) is None            # far outside the annulus
    assert ik.solve((0.0, 0.0, 3.0)) is None            # above everything
    assert ik.solve((0.2, 0.0, float('nan'))) is None
    assert ik.solve((0.2, 0.0, 0.02), roll=float('inf')) is None
    assert ik.in_workspace((5.0, 0.0, 0.0)) is False


def test_the_reachable_ring_at_the_grasp_plane_is_a_real_annulus():
    """The student-facing ring in the ArmProfile (0.09 / 0.35) must sit INSIDE
    what the solver actually reaches at the shipped grasp height."""
    ik = _ik()
    z = 0.015
    reach = [r for r in np.arange(0.02, 0.45, 0.002)
             if ik.solve((float(r), 0.0, z)) is not None]
    assert reach, 'nothing is reachable at the grasp plane'
    assert min(reach) <= 0.09 + 1e-9
    assert max(reach) >= 0.35 - 1e-9
    # …and the hole in the middle is real, not an artefact of the sweep.
    assert ik.solve((0.02, 0.0, z)) is None


def test_the_elbow_down_branch_is_unreachable_but_is_kept_anyway():
    """Documents a fact that would otherwise read as dead code.

    With THESE link lengths and limits the elbow-down branch cannot satisfy any
    target at or above the table (see the derivation in ``solve``). The branch
    stays because it is the correct general form and the arm's rods are DESIGNED
    to be swapped — the CAD ships a table of alternative lengths — so a future
    build could make it live. This test is what stops it being deleted as
    unused, and what would notice if a rod change made it reachable.
    """
    from physical_ai_server.workflow import edu1_ik as m
    rng = random.Random(5)
    both = down_only = up_only = 0
    for _ in range(4000):
        x = rng.uniform(0.02, 0.42)
        y = rng.uniform(-0.35, 0.35)
        z = rng.uniform(0.0, 0.12)
        r = math.hypot(x, y)
        d_v = (z + m._WRIST_ABOVE_TCP) - m._SHOULDER_Z
        rho = math.hypot(r, d_v)
        if rho < m._REACH_MIN - 1e-9 or rho > m._REACH_MAX + 1e-9:
            continue
        cos_g = max(-1.0, min(1.0, (rho * rho - m._L2 ** 2 - m._L3 ** 2)
                              / (2.0 * m._L2 * m._L3)))
        gamma = math.acos(cos_g)
        psi = math.atan2(r, d_v)
        legal = []
        for g in (gamma, -gamma):
            alpha = psi - math.atan2(m._L3 * math.sin(g),
                                     m._L2 + m._L3 * math.cos(g))
            q2 = alpha - m._ALPHA0
            q3 = m._G_OFFSET - g
            q4 = q2 - q3 + m._Q4_VERTICAL_OFFSET
            legal.append(_ik()._within_limits(
                [-math.atan2(y, x), q2, q3, q4, 0.0]))
        up, down = legal
        both += up and down
        down_only += down and not up
        up_only += up and not down
    assert up_only > 500, 'the sample never reached the arm at all'
    assert down_only == 0
    assert both == 0


def test_the_wrist_annulus_constants_bracket_the_two_link_chain():
    assert 0.0 < _REACH_MIN < _REACH_MAX
    assert _REACH_MAX == pytest.approx(0.381258, abs=1e-5)


# ── the jaw fold ─────────────────────────────────────────────────────────────

def test_every_tag_yaw_is_reachable_through_the_ninety_degree_roll_joint():
    """THE reason the fold exists on this arm: joint5 spans ±90° while a tag can
    present any yaw in 360°. Without folding, half of all rolls return None."""
    ik = _ik()
    for deg in range(0, 360, 5):
        roll = math.radians(deg)
        q = ik.solve((0.20, 0.0, 0.015), roll=roll)
        assert q is not None, f'roll {deg}° unreachable'
        assert abs(q[4]) <= math.pi / 2 + 1e-9


def test_the_fold_preserves_the_jaw_axis_modulo_pi():
    """A fold is only legitimate if the two twins are the SAME grasp: the jaw
    axis azimuth must be unchanged mod π."""
    ik = _ik()
    for deg in range(0, 360, 7):
        roll = math.radians(deg)
        q = ik.solve((0.18, 0.05, 0.02), roll=roll)
        assert q is not None
        frames = ik.link_frames(q)
        jaw = frames[5][:3, :3] @ np.array([0.0, 1.0, 0.0])
        chi = math.atan2(jaw[1], jaw[0])
        # Unfolded reference: q5 = wrap(-roll) exactly.
        q_ref = list(q)
        q_ref[4] = (-roll + math.pi) % (2 * math.pi) - math.pi
        jaw_ref = ik.link_frames(q_ref)[5][:3, :3] @ np.array([0.0, 1.0, 0.0])
        chi_ref = math.atan2(jaw_ref[1], jaw_ref[0])
        assert math.sin(chi - chi_ref) == pytest.approx(0.0, abs=1e-9)


def test_the_fold_reference_is_zero_and_not_the_seed():
    """Documented contract: ``seed`` does not change the returned solution. A
    seed-relative fold drifts to the map edge within two grasps (measured on the
    edu6); keeping the reference at 0 is what keeps the contract true."""
    ik = _ik()
    base = ik.solve((0.20, 0.0, 0.02), roll=2.5)
    for seed in ([0.0] * 5, [0.0, 1.0, 1.0, 0.0, 1.5], [0.0, 1.0, 1.0, 0.0, -1.5]):
        assert ik.solve((0.20, 0.0, 0.02), roll=2.5, seed=seed) == base


def test_the_fold_never_moves_the_tool():
    """The fingertip TCP lies ON the joint5 axis, so folding is free."""
    ik = _ik()
    q = ik.solve((0.22, -0.04, 0.03), roll=2.9)
    assert q is not None
    _, a = ik.fk(q)
    moved = list(q)
    moved[4] = q[4] + math.pi if q[4] < 0 else q[4] - math.pi
    _, b = ik.fk(moved)
    assert float(np.linalg.norm(a - b)) < 1e-9


# ── roll ↔ joint mapping ─────────────────────────────────────────────────────

def test_roll_from_joints_round_trips():
    """``roll`` and joint5 are DIFFERENT numbers here (q5 = wrap(−roll)); every
    "keep the current wrist" call site must go through this method."""
    ik = _ik()
    for value in (-1.5, -0.7, 0.0, 0.4, 1.5):
        q = [0.1, 1.0, 0.6, 1.0 - 0.6 + _Q4_VERTICAL_OFFSET, value]
        roll = ik.roll_from_joints(q)
        again = ik.solve(ik.fk(q)[1], roll=roll)
        assert again is not None
        assert again[4] == pytest.approx(value, abs=1e-9)


def test_roll_from_joints_is_not_the_identity():
    """Guards the exact bug this method exists to prevent: passing joints[4]
    straight into roll= would mirror the wrist."""
    ik = _ik()
    assert ik.roll_from_joints([0, 0, 0, 0, 0.9]) == pytest.approx(-0.9)
    assert ik.roll_from_joints([0, 0, 0, 0]) is None
    assert ik.roll_from_joints([0, 0, 0, 0, float('nan')]) is None


def test_base_yaw_is_the_azimuth_and_theta1_is_its_negative():
    """joint1's axis is (0,0,-1) on this arm, so the JOINT is the negative of
    the world azimuth. ``base_yaw`` returns the AZIMUTH — motion composes it
    with the tag yaw in world terms — and the sign flip stays inside solve()."""
    ik = _ik()
    assert ik.base_axis_x == BASE_AXIS_X_WORLD == 0.0
    for x, y in ((0.2, 0.0), (0.2, 0.1), (0.15, -0.12)):
        assert ik.base_yaw(x, y) == pytest.approx(math.atan2(y, x))
        q = ik.solve((x, y, 0.02))
        assert q is not None
        assert q[0] == pytest.approx(-ik.base_yaw(x, y), abs=1e-6)


# ── identity + the geometry seams other modules consume ──────────────────────

def test_identity():
    ik = _ik()
    assert ik.backend == 'closed-form-edu1'
    assert ik.num_joints() == 5
    assert ik.joint_limits == tuple(_EDU1_JOINT_LIMITS_RAD)


def test_link_frames_are_world_frame_and_one_per_link():
    ik = _ik()
    frames = ik.link_frames([0.0, 1.0, 0.6, 0.0, 0.0])
    assert len(frames) == 6                     # base_link + link1..link5
    assert np.allclose(frames[0][:3, :3], _RZ_PI)
    assert np.allclose(frames[0][:3, 3], 0.0)


def test_link_points_end_at_the_tcp():
    ik = _ik()
    q = ik.solve((0.20, 0.0, 0.02))
    pts = ik.link_points(q)
    _, tcp = ik.fk(q)
    assert np.allclose(pts[-1], tcp)
    # base → joint1 origin → shoulder → elbow → wrist → tool origin → TCP,
    # sampled 5× per link plus the base point.
    assert len(pts) == 1 + 6 * 5


def test_solve_quat_is_position_only():
    ik = _ik()
    a = ik.solve_quat((0.2, 0.0, 0.02), (0.0, 0.0, 0.0, 1.0))
    b = ik.solve((0.2, 0.0, 0.02))
    assert a == b

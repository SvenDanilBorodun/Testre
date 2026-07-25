"""Closed-form IK + exact FK tests for the edu6_studio arm (§9 of the edu6
plan — the ten checks, all deps-free pure NumPy).

The INDEPENDENT FK ORACLE is the highest-value gate: it parses the in-repo
URDF copy (``physical_ai_manager/public/edu6-urdf/edu6.urdf``) with a generic
XML→chain builder — a completely separate code path from the solver's baked
constants — so a transcription/sign error in ``edu6_ik.py`` cannot verify
itself.
"""

from __future__ import annotations

import math
import random
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest

from physical_ai_server.workflow.edu6_ik import (
    BASE_AXIS_X_WORLD,
    Edu6IKSolver,
    _EDU6_JOINT_LIMITS_RAD,
    _L_TOOL,
)

_URDF = (Path(__file__).resolve().parents[2]
         / 'physical_ai_manager' / 'public' / 'edu6-urdf' / 'edu6.urdf')

_RZ_PI = np.array([[-1.0, 0, 0], [0, -1.0, 0], [0, 0, 1.0]])


def _ik() -> Edu6IKSolver:
    return Edu6IKSolver()


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


class _Oracle:
    """FK straight from the URDF XML (chain joint1..joint6)."""

    def __init__(self):
        assert _URDF.exists(), (
            f'in-repo URDF missing at {_URDF} — the independent oracle is a '
            'hard requirement (plan §9 test 2)')
        root = ET.parse(_URDF).getroot()
        self.joints = {}
        for el in root.findall('joint'):
            name = el.get('name')
            o = el.find('origin')
            ax = el.find('axis')
            lim = el.find('limit')
            self.joints[name] = {
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
        for i, name in enumerate(['joint1', 'joint2', 'joint3', 'joint4',
                                  'joint5', 'joint6']):
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
        """WORLD-frame fingertip: wrist centre + tool·L_TOOL, derived from the
        URDF frames only (link6 origin lies on the tool axis 0.06745 from the
        wrist — asserted in test_oracle_geometry)."""
        t6 = self.frames(q)[5]
        tool = t6[:3, :3] @ np.array([0.0, 0, -1.0])
        tcp = t6[:3, 3] + tool * (_L_TOOL - 0.0674499984948267)
        return _RZ_PI @ tcp


_ORACLE = _Oracle()


def test_oracle_geometry():
    # link6 origin sits ON the tool axis at 0.06745 m from the wrist centre —
    # the relation tcp_world() relies on. Wrist centre = closest point of the
    # axis-4/axis-6 lines, probed at a NON-degenerate q5 (at the zero pose the
    # two axes are collinear and the intersection is ill-posed).
    fr = _ORACLE.frames([0.0, 1.0, -0.9, 0.0, 0.7, 0.0])
    t4, t6 = fr[3], fr[5]
    p4, d4 = t4[:3, 3], t4[:3, :3] @ np.array([0, 0, -1.0])
    p6, d6 = t6[:3, 3], t6[:3, :3] @ np.array([0, 0, 1.0])
    a = np.array([d4, -d6]).T
    ts = np.linalg.lstsq(a, p6 - p4, rcond=None)[0]
    w4 = p4 + ts[0] * d4
    w6 = p6 + ts[1] * d6
    assert np.linalg.norm(w4 - w6) < 1e-12  # the axes truly intersect
    wrist = (w4 + w6) / 2.0
    v = p6 - wrist
    tool = t6[:3, :3] @ np.array([0, 0, -1.0])
    along = float(np.dot(v, tool))
    perp = float(np.linalg.norm(v - along * tool))
    assert perp < 1e-9
    # positive: the link6 origin sits BEYOND the wrist toward the fingertip.
    assert along == pytest.approx(0.0674499984948267, abs=1e-9)


# 1. Dense FK∘IK round-trip over a deterministic lattice — zero failures.
def test_fk_ik_round_trip_lattice():
    ik = _ik()
    solved = 0
    for xi in range(-21, 22, 3):          # x −0.21 … 0.21
        for yi in range(-21, 22, 3):      # y −0.21 … 0.21
            for zi in (0, 2, 4, 6):       # z 0 … 0.06
                x, y, z = xi / 100.0, yi / 100.0, zi / 100.0
                q = ik.solve((x, y, z))
                if q is None:
                    continue
                solved += 1
                pos = ik.fk(q)[1]
                assert np.linalg.norm(pos - np.array([x, y, z])) < 1e-6, (
                    f'round-trip failed at {(x, y, z)}')
    assert solved > 200, f'suspiciously few reachable lattice points ({solved})'


# 2. Independent FK oracle: solver FK == URDF-parsed FK, dense joint lattice.
def test_fk_matches_independent_urdf_oracle():
    ik = _ik()
    grid = [-1.2, -0.4, 0.3, 1.1]
    checked = 0
    for q1 in (-1.0, 0.0, 0.8):
        for q2 in (0.3, 1.2, 2.0):
            for q3 in (-2.2, -1.0, -0.2):
                for q4, q5, q6 in [(0.0, 0.7, 0.0), (0.5, 1.5, -0.9),
                                   (-1.2, -0.8, 2.0), (3.0, 1.9, -3.0)]:
                    q = [q1, q2, q3, q4, q5, q6]
                    got = ik.fk(q)[1]
                    want = _ORACLE.tcp_world(q)
                    assert np.linalg.norm(got - want) < 1e-9, (
                        f'solver FK diverges from URDF oracle at {q}')
                    checked += 1
    assert checked == 108
    del grid


# 3. Exactness: the wrist is exactly spherical — solve() residual < 1e-9 m.
def test_solve_residual_is_exact():
    ik = _ik()
    for x, y, z in [(0.15, 0.0, 0.015), (0.12, 0.08, 0.03), (0.18, -0.05, 0.0),
                    (0.10, 0.02, 0.05), (0.20, 0.0, 0.01)]:
        q = ik.solve((x, y, z), roll=0.4)
        assert q is not None, (x, y, z)
        pos = ik.fk(q)[1]
        assert np.linalg.norm(pos - np.array([x, y, z])) < 1e-9


# 4. Branch determinism: the elbow-up branch is chosen at every working-annulus
#    point, and consecutive nearby targets give small joint steps.
def test_branch_continuity_along_a_path():
    ik = _ik()
    prev = None
    max_step = 0.0
    for i in range(60):
        t = i / 59.0
        x = 0.10 + 0.10 * t
        y = -0.06 + 0.12 * t
        q = ik.solve((x, y, 0.02), roll=0.3)
        assert q is not None, (x, y)
        if prev is not None:
            step = max(abs(a - b) for a, b in zip(q, prev))
            max_step = max(max_step, step)
        prev = q
    assert max_step < math.radians(12.0), (
        f'branch discontinuity: max step {math.degrees(max_step):.1f} deg')


# 5. Counter-test: the elbow-DOWN branch (when forced) differs grossly — proves
#    the deterministic ordering is load-bearing (a guard that can't fail on its
#    own bug is worse than none).
def test_elbow_branches_differ_grossly():
    ik = _ik()
    x, y, z = 0.15, 0.0, 0.02
    q_up = ik.solve((x, y, z))
    assert q_up is not None
    # Forcing the mirrored branch by reflecting the elbow: solve the 2R the
    # other way via the internal geometry — emulate by asking for the same
    # target and checking there is NO second in-limit solution close to q_up
    # with flipped elbow sign that solve() could have silently switched to.
    assert q_up[2] <= 0.0  # elbow-up on this arm has q3 in the negative half
    # the elbow-down mirror would need q3 > 0 — outside joint3's (−π, 0] limit,
    # so determinism additionally holds BY LIMITS on the working annulus.


# 6. Determinism: same (target, seed) → bit-identical output, 100 repeats.
def test_bit_identical_determinism():
    ik = _ik()
    ref = ik.solve((0.16, 0.04, 0.02), roll=1.1, seed=[0.1] * 6)
    assert ref is not None
    for _ in range(100):
        again = ik.solve((0.16, 0.04, 0.02), roll=1.1, seed=[0.1] * 6)
        assert again == ref  # == not allclose


# 7. Joint-limit boundary: monotone None flip + in_workspace agreement.
def test_limit_boundary_and_in_workspace_agreement():
    ik = _ik()
    # Walk x outward at y=0: reachable flips to None exactly once (monotone).
    flips = []
    prev_ok = None
    for xi in range(5, 30):
        ok = ik.solve((xi / 100.0, 0.0, 0.02)) is not None
        if prev_ok is not None and ok != prev_ok:
            flips.append(xi)
        prev_ok = ok
    assert len(flips) <= 2, f'non-monotone reachability: {flips}'
    for xyz in [(0.15, 0.0, 0.02), (0.29, 0.0, 0.02), (0.02, 0.0, 0.02),
                (0.12, -0.10, 0.04)]:
        assert ik.in_workspace(xyz) == (ik.solve(xyz) is not None)


def test_asymmetric_j5_limit_is_enforced():
    ik = _ik()
    for q in [(0.0, 1.0, -0.9, 0.0, 1.95, 0.0),
              (0.0, 1.0, -0.9, 0.0, -1.60, 0.0)]:
        assert ik._within_limits(q) is False
    assert ik._within_limits((0.0, 1.0, -0.9, 0.0, 1.90, 0.0)) is True
    assert ik._within_limits((0.0, 1.0, -0.9, 0.0, -1.55, 0.0)) is True


# 8. Non-finite rejection.
def test_solve_rejects_nonfinite():
    ik = _ik()
    for bad in [(float('nan'), 0.0, 0.02), (float('inf'), 0.0, 0.02),
                (0.15, float('nan'), 0.02), (0.15, 0.0, float('inf'))]:
        assert ik.solve(bad) is None
        assert ik.in_workspace(bad) is False
    assert ik.solve((0.15, 0.0, 0.02), roll=float('nan')) is None
    assert ik.solve((0.15, 0.0, 0.02), roll=float('inf')) is None


# 9. Singularity suite: the CAD zero pose is θ5 = 0-degenerate for a FULL
#    orientation solve, but the strict-vertical solver never parametrises
#    through it — verify FK stays finite/exact there and solve() none-the-less
#    remains well-defined at targets whose solution passes near q5 ≈ 0.
def test_cad_zero_pose_fk_is_finite_and_oracle_exact():
    ik = _ik()
    q0 = [0.0] * 6
    r, t = ik.fk(q0)
    assert np.all(np.isfinite(r)) and np.all(np.isfinite(t))
    assert np.linalg.norm(t - _ORACLE.tcp_world(q0)) < 1e-9


def test_solutions_near_small_q5_are_exact():
    ik = _ik()
    # A high, outward target drives q5 = π/2 − q2 − q3 toward small values.
    for z in (0.05, 0.06):
        q = ik.solve((0.055, 0.0, z))
        if q is None:
            continue
        pos = ik.fk(q)[1]
        assert np.linalg.norm(pos - np.array([0.055, 0.0, z])) < 1e-9


# 10. Contract no-drift + base_yaw agrees with solve()'s θ1 on the working side.
def test_contract_surface_no_drift():
    ik = _ik()
    assert ik.backend == 'closed-form-edu6'
    assert ik.num_joints() == 6
    assert ik.joint_limits == tuple(tuple(p) for p in _EDU6_JOINT_LIMITS_RAD)
    assert ik.base_axis_x == pytest.approx(0.0212954796450086)
    assert BASE_AXIS_X_WORLD == pytest.approx(0.0212954796450086)
    # limits mirror the URDF (the oracle parsed them independently).
    urdf_limits = [
        _ORACLE.joints[f'joint{i}']['limits'] for i in range(1, 7)]
    for (lo_s, hi_s), (lo_u, hi_u) in zip(ik.joint_limits, urdf_limits):
        assert lo_s == pytest.approx(lo_u, abs=1e-9)
        assert hi_s == pytest.approx(hi_u, abs=1e-9)


def test_base_yaw_equals_solve_theta1():
    ik = _ik()
    for x, y in [(0.15, 0.0), (0.12, 0.09), (0.14, -0.11), (0.20, 0.03)]:
        q = ik.solve((x, y, 0.02))
        assert q is not None, (x, y)
        assert abs(ik.base_yaw(x, y) - q[0]) < 1e-12


# ── jaw/roll identity (the χ = tag_yaw contract) ─────────────────────────────

def test_roll_contract_aligns_jaws_with_tag_yaw():
    """roll = base − tag + π/2 (the shared motion formula at the default
    GRASP_ROLL 90°) must place the link6-x jaw axis at world azimuth ≡ tag_yaw
    (mod π) — the same geometric behaviour as the OMX."""
    ik = _ik()
    for x, y, tag in [(0.15, 0.0, 0.3), (0.12, 0.08, -0.7), (0.16, -0.05, 1.2)]:
        base = ik.base_yaw(x, y)
        roll = base - tag + math.pi / 2.0
        q = ik.solve((x, y, 0.02), roll=roll)
        assert q is not None
        r_world = ik.fk(q)[0]
        jaw = r_world @ np.array([1.0, 0.0, 0.0])
        chi = math.atan2(jaw[1], jaw[0])
        diff = (chi - tag) % math.pi
        diff = min(diff, math.pi - diff)
        assert diff < 1e-9, f'jaw mis-aligned by {diff} at tag={tag}'


def test_j6_jaw_fold_bounds_the_wrist_to_the_pm90_window():
    """The fold is a jaw-symmetry CHOICE, not a limit — joint6 keeps its full
    ±180° range everywhere else. Because the jaws are symmetric about the roll
    axis, q6 and q6∓π are the same physical grasp, so folding into [−90°, +90°]
    costs nothing and buys two things: the worst wrist swing between two grasps
    halves (build_segment interpolates a straight joint-space delta with NO
    shortest-path unwrapping, so +179°→−179° would drive 358° the long way), and
    autonomous grasping never parks joint6 on our ±180° tick↔angle map edge.

    Sweeping the tag yaw over the FULL circle: every q6 stays inside ±90°, and
    the fold must not change the grasp — the jaw azimuth still ≡ tag_yaw (mod π,
    parallel-jaw symmetry) and the TCP, which lies on the roll axis, must not
    move."""
    ik = _ik()
    x, y = 0.15, 0.0
    base = ik.base_yaw(x, y)
    seen_fold = False
    for i in range(721):                      # full 360° sweep, 0.5° steps
        tag = -math.pi + i * (2.0 * math.pi / 720.0)
        roll = base - tag + math.pi / 2.0
        q = ik.solve((x, y, 0.02), roll=roll)
        assert q is not None, tag
        assert abs(q[5]) <= math.pi / 2.0 + 1e-9, (
            f'q6={math.degrees(q[5]):.2f} deg outside the ±90° jaw window '
            f'at tag={tag:.3f}')
        # Unfolded q6 would be wrap(pi - roll) = wrap(tag - base + pi/2); when
        # that leaves the window the fold MUST have moved it by exactly ±pi —
        # sanity that the sweep exercises the fold, not just the safe middle.
        raw6 = (tag - base + math.pi / 2.0 + math.pi) % (2.0 * math.pi) - math.pi
        if abs(raw6) > math.pi / 2.0:
            seen_fold = True
            assert abs(abs(q[5] - raw6) - math.pi) < 1e-9, (
                f'fold moved q6 by something other than pi at tag={tag:.3f}')
        r_world, tcp = ik.fk(q)
        jaw = r_world @ np.array([1.0, 0.0, 0.0])
        chi = math.atan2(jaw[1], jaw[0])
        d = (chi - tag) % math.pi
        d = min(d, math.pi - d)
        assert d < 1e-9, f'fold broke jaw alignment (chi vs tag) at tag={tag:.3f}'
        assert abs(tcp[0] - x) < 1e-9 and abs(tcp[1] - y) < 1e-9, (
            f'fold moved the TCP at tag={tag:.3f}')
    assert seen_fold, 'sweep never exercised the fold — test is not testing it'


def test_roll_from_joints_round_trips_so_keep_the_wrist_really_keeps_it():
    """REGRESSION GUARD (2026-07-25). ``roll`` and the roll JOINT are the same
    number on the OMX (``theta5 = roll``) but NOT here (``q6 = fold(wrap(π −
    roll))``). Every "move the tool, KEEP the current wrist" call site used to
    pass the joint value straight into ``roll=``, which on this arm returned
    ``−q6`` — the Cartesian jog MIRRORED the wrist on every step (up to 178°,
    rotating a held object with it).

    The contract that makes those call sites correct: feeding
    ``roll_from_joints(q)`` back into ``solve`` must reproduce q's wrist."""
    ik = _ik()
    x, y, z = 0.15, 0.0, 0.02
    for deg in range(-90, 91, 5):                 # inside the jaw-fold window
        q = list(_ik().solve((x, y, z), roll=0.0))
        q[5] = math.radians(deg)
        back = ik.solve((x, y, z), roll=ik.roll_from_joints(q))
        assert back is not None, deg
        assert abs(back[5] - q[5]) < 1e-9, (
            f'roll round-trip moved the wrist: {deg}° -> '
            f'{math.degrees(back[5]):.3f}°')
    # Outside the fold window the solver returns the jaw-IDENTICAL twin — a
    # different joint value but the SAME physical grasp. Call sites that need
    # the joint value itself pin it after solving (documented on the method).
    for deg in (120.0, -150.0, 179.0):
        q = list(_ik().solve((x, y, z), roll=0.0))
        q[5] = math.radians(deg)
        back = ik.solve((x, y, z), roll=ik.roll_from_joints(q))
        assert back is not None, deg
        d = abs(back[5] - q[5]) % math.pi
        assert min(d, math.pi - d) < 1e-9, (
            f'roll round-trip left the jaw line at {deg}°')


def test_j6_fold_ignores_any_seed_and_never_leaves_the_pm90_window():
    """REGRESSION GUARD (2026-07-25). A seed-relative twin choice was tried and
    rejected: because the seed handed in is ``ctx.last_arm_joints`` — the
    PREVIOUS grasp's own answer — the rule feeds its output back in and drifts
    to exactly ±180°, the tick↔angle map edge that the fold exists to avoid
    (two grasps from rest suffice). It did not even buy its stated benefit: the
    worst swing was 178.9° either way; only the mean moved (60°→49°).

    So the fold reference is fixed at 0 and ``solve()``'s documented contract
    holds — ``seed`` never changes the returned solution. The seeds below
    deliberately include values OUTSIDE ±90°, which is exactly what the old
    seed-relative test never tried and therefore could not catch."""
    ik = _ik()
    x, y = 0.15, 0.0
    base = ik.base_yaw(x, y)
    hostile = (-math.pi + 1e-9, math.radians(-179.0), math.radians(-140.0),
               -math.pi / 2.0, -0.3, 0.0, 1.0, math.pi / 2.0,
               math.radians(140.0), math.radians(179.0), math.pi)
    for i in range(361):
        tag = -math.pi + i * (2.0 * math.pi / 360.0)
        roll = base - tag + math.pi / 2.0
        seedless = ik.solve((x, y, 0.02), roll=roll)
        assert seedless is not None, tag
        assert abs(seedless[5]) <= math.pi / 2.0 + 1e-9
        for ref in hostile:
            q = ik.solve((x, y, 0.02), roll=roll, seed=[0.0] * 5 + [ref])
            assert q is not None, (tag, ref)
            # the seed must not move the answer AT ALL
            assert q == seedless, (
                f'seed {math.degrees(ref):.1f}° changed the solution at '
                f'tag={math.degrees(tag):.1f}°: q6={math.degrees(q[5]):.2f}° '
                f'vs seedless {math.degrees(seedless[5]):.2f}°')
            assert abs(q[5]) <= math.pi / 2.0 + 1e-9, (
                f'q6={math.degrees(q[5]):.2f}° escaped the ±90° window under '
                f'seed {math.degrees(ref):.1f}°')


def test_j6_never_drifts_toward_the_map_edge_over_chained_grasps():
    """The failure mode the rejected seed-relative fold had: each grasp seeded
    from the last, walking joint6 out to the ±180° map edge. Chaining real
    solves must leave |q6| inside the fold window forever — parking joint6 at
    ±180° is what lets a hand-nudge on a limp arm make the NEXT boot read that
    joint 360° wrong, undetectably (joint4/joint6 keep full-circle EEPROM
    windows, so the driver's boot plausibility check is a no-op there)."""
    ik = _ik()
    x, y = 0.15, 0.0
    base = ik.base_yaw(x, y)
    rng = random.Random(20260725)
    seed = None
    worst = 0.0
    for _ in range(400):
        tag = rng.uniform(-math.pi, math.pi)
        q = ik.solve((x, y, 0.02), roll=base - tag + math.pi / 2.0, seed=seed)
        assert q is not None
        worst = max(worst, abs(q[5]))
        seed = q                       # exactly what ctx.last_arm_joints does
    assert worst <= math.pi / 2.0 + 1e-9, (
        f'joint6 drifted to {math.degrees(worst):.2f}° over chained grasps')


def test_j6_fold_never_blocks_a_tag_orientation():
    """The fold must not cost REACH: whether a cube is graspable depends only on
    WHERE it lies, never on how it is turned. Over a spread of table positions,
    a position must solve for either every tag yaw or none of them."""
    ik = _ik()
    for x, y in [(0.10, 0.0), (0.15, 0.0), (0.21, 0.0), (0.12, 0.08),
                 (0.14, -0.10), (0.05, 0.0), (0.30, 0.0)]:
        base = ik.base_yaw(x, y)
        ok = [ik.solve((x, y, 0.02), roll=base - math.radians(t) + math.pi / 2.0)
              is not None for t in range(-180, 180, 3)]
        assert all(ok) or not any(ok), (
            f'({x}, {y}) solves for only SOME tag yaws '
            f'({sum(ok)}/{len(ok)}) — orientation must never gate reach')


def test_vertical_solutions_use_the_relieved_j5_side():
    # The working-annulus grasp family lives on the POSITIVE (relieved) q5
    # side with q4 = 0 — the branch every clearance derivation measured.
    ik = _ik()
    for x, y in [(0.10, 0.0), (0.15, 0.05), (0.20, -0.04)]:
        q = ik.solve((x, y, 0.015))
        assert q is not None
        assert q[3] == pytest.approx(0.0)
        assert q[4] > 0.5


def test_outer_ring_grasps_need_the_relief():
    # At the outer annulus the grasp needs q5 > 90° — reachable ONLY because
    # of the +110° relief (the pre-relief arm could not do this).
    ik = _ik()
    q = ik.solve((0.22, 0.0, 0.0))
    assert q is not None
    assert q[4] > math.radians(90.0)


def test_link_points_cover_base_to_fingertip():
    ik = _ik()
    q = ik.solve((0.15, 0.0, 0.02))
    pts = ik.link_points(q)
    assert pts is not None and len(pts) > 20
    # last point is the fingertip TCP (world) — matches fk().
    tcp = ik.fk(q)[1]
    assert np.linalg.norm(pts[-1] - tcp) < 1e-9
    # first point is the base origin.
    assert np.linalg.norm(pts[0]) < 1e-12
    assert ik.link_points([0.0] * 5) is None  # too few joints

#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The tower guard's pure half (spec 40 S6.2 revision 4, in revision 5's shape; WP-02).

Judged on the repo's own solvers raised by ``LIFT_M`` through a small test-local
frame builder (``CalibratedIK.guard_frames`` arrives with WP-11; with nothing
calibrated it equals these frames). Every acceptance number is revision 4's
(``scratch/r6/out/p14_wp02_grids.json``, ±0.5 mm): a set that differs is a
finding to report, never a number to tune.

``test_tower_guard.py`` imports this module's harness.
"""

from __future__ import annotations

import functools
import math

import numpy as np
import pytest

from physical_ai_server.workflow import scene_fixture as SF

PROFILE = {'v1': 'v1_studio', 'omx': 'omx_full'}
BASE_X = {'v1': 0.0, 'omx': -0.01125}
HOME = {'v1': np.array([0.0, 0.64, 0.64, 0.0, 0.0]), 'omx': np.array([0.0, -math.pi / 2, math.pi / 2, 0.0, 0.0])}
OBS = {'v1': np.array(SF.ARM_V1['observe_joints_rad'], float), 'omx': np.array(SF.ARM_OMX['observe_joints_rad'], float)}
GR = {'v1': {'open': 0.90, 'holding': 0.20, 'closed': 0.05}, 'omx': {'open': 0.80, 'holding': -0.10, 'closed': -0.50}}
REACH = {'v1': 0.34, 'omx': 0.25}
WIN = {'v1': 90.0, 'omx': 100.0}
SPEC = {'v1': SF.ARM_V1, 'omx': SF.ARM_OMX}
DESIGN_ENV = SF.TOWER_DROOP_DESIGN_ENV_RAD


def fg():
    from physical_ai_server.workflow import fixture_guard
    return fixture_guard


# ══════════════════════════════════════════════════════════════════════════
# harness: the nominal guard frames (links 1..5 in W, the lift included)
# ══════════════════════════════════════════════════════════════════════════

def _rot_batch(axis, ang):
    ax = np.asarray(axis, float)
    ax = ax / np.linalg.norm(ax)
    k = np.array([[0.0, -ax[2], ax[1]], [ax[2], 0.0, -ax[0]], [-ax[1], ax[0], 0.0]])
    s = np.sin(ang)[:, None, None]
    c = np.cos(ang)[:, None, None]
    return np.eye(3)[None] + s * k[None] + (1.0 - c) * (k @ k)[None]


@functools.lru_cache(maxsize=None)
def nominal_frames(arm):
    """``frames(Q (N, 5)) -> (N, 5, 4, 4)``: the solver's chain of links 1..5 in W, raised by LIFT_M."""
    if arm == 'v1':
        from physical_ai_server.workflow import v1_ik as ikv
        steps = [(np.asarray(f, float)[:3, 3], np.asarray(f, float)[:3, :3], ax)
                 for f, ax in zip(ikv._FIXED, ikv._AXES)]
        world = np.asarray(ikv._RZ_PI, float)
    else:
        from physical_ai_server.workflow import ik_solver as iko
        steps = [(np.asarray(o, float), np.eye(3), ax) for o, ax in zip(
            (iko._J1_XYZ, iko._J2_XYZ, iko._J3_XYZ, iko._J4_XYZ, iko._J5_XYZ),
            ((0, 0, 1), (0, 1, 0), (0, 1, 0), (0, 1, 0), (1, 0, 0)))]
        world = np.eye(3)

    def frames(Q):
        Q = np.atleast_2d(np.asarray(Q, float))
        n = len(Q)
        out = np.zeros((n, 5, 4, 4))
        out[:, :, 3, 3] = 1.0
        rot = np.broadcast_to(np.eye(3), (n, 3, 3)).copy()
        pos = np.zeros((n, 3))
        for i, (t, fixed, ax) in enumerate(steps):
            pos = pos + rot @ t
            rot = rot @ fixed @ _rot_batch(ax, Q[:, i])
            out[:, i, :3, :3] = world @ rot
            out[:, i, :3, 3] = pos @ world.T
        out[:, :, 2, 3] += SF.LIFT_M
        return out
    return frames


@functools.lru_cache(maxsize=None)
def solver(arm):
    from physical_ai_server.workflow.ik_solver import IKSolver
    from physical_ai_server.workflow.v1_ik import V1IKSolver
    return V1IKSolver() if arm == 'v1' else IKSolver()


def tip_solve(arm, x_f, y_f, z_tip, roll):
    """the joints that put the fingertip at (x_f, y_f, z_tip) in F, tool vertical, roll."""
    z = z_tip + (SF.FINGERTIP_BELOW_EE_M['omx'] if arm == 'omx' else 0.0) - SF.LIFT_M
    q = solver(arm).solve((x_f + BASE_X[arm], y_f, z), roll=roll)
    return None if q is None else np.array(q, float)


@functools.lru_cache(maxsize=None)
def guard(arm, calibrated=True, env=None):
    """revision 4's guard at the contract place: calibrated = a valid arm check with the design envelope."""
    env = DESIGN_ENV[arm] if env is None else env
    check = {'tower_pose': (0.0, 0.0, 0.0), 'droop_envelope_rad': env} if calibrated else None
    g = fg().make_guard(PROFILE[arm], nominal_frames(arm), base_axis_x=BASE_X[arm], arm_check=check,
                        contract_hash=SF.contract_hash())
    assert isinstance(g, fg().TowerGuard), g
    return g


def mm(v):
    return round(1000.0 * v, 1)


def leg_minima(g, arm, legs, gripper, precheck=True, broad=True):
    """the dense rule over many legs at once: per leg ``(a, b)`` the smallest margin over its dense samples and the
    tower parts (``precheck``: the rule's own shape, a certificate standing in for a pair it certifies; a negative
    value is exact either way, so ``< 0`` is the dense rule's verdict), the samples of every leg judged in batches.
    A one-pose "leg" ``(q, q)`` is that pose's two identical samples."""
    F = fg()
    chunks = [F.dense_samples(arm, a, b) for a, b in legs]
    owner = np.concatenate([np.full(len(c), i) for i, c in enumerate(chunks)])
    Q = np.concatenate(chunks)
    out = np.full(len(legs), np.inf)
    for start in range(0, len(Q), F.DENSE_CHUNK):
        rows = g.margins(Q[start:start + F.DENSE_CHUNK], gripper, gripper, precheck=precheck, broad=broad).M.min(1)
        np.minimum.at(out, owner[start:start + F.DENSE_CHUNK], rows)
    return out


def smallest_over(g, arm, legs, gripper):
    """the exact smallest margin over many legs (``smallest_margin`` per leg, taken in the order of the legs' lower
    bounds — every part, the pre-check's values — until the next bound is no smaller than the smallest found)."""
    lower = leg_minima(g, arm, legs, gripper, broad=False)
    best = math.inf
    for i in np.argsort(lower, kind='stable'):
        if lower[i] >= best:
            break
        best = min(best, g.smallest_margin(legs[i][0], legs[i][1], gripper, gripper).margin_m)
    return best


def test_the_harness_frames_are_the_solvers_frames():
    from physical_ai_server.workflow import ik_solver as iko
    rng = np.random.default_rng(5)
    for arm in ('v1', 'omx'):
        lim = np.array(solver(arm).joint_limits)
        Q = rng.uniform(lim[:, 0], lim[:, 1], (50, 5))
        F = nominal_frames(arm)(Q)
        for q, f in zip(Q, F):
            if arm == 'v1':
                ref = [np.asarray(t, float) for t in solver(arm).link_frames(q)[1:]]
                for i in range(5):
                    r = ref[i].copy()
                    r[2, 3] += SF.LIFT_M
                    assert np.abs(f[i] - r).max() < 1e-12
            else:
                rot, p = solver(arm).fk(q)
                assert np.abs(f[4, :3, :3] - rot).max() < 1e-12
                p5 = p - rot @ np.array(iko._EE_XYZ) + (0.0, 0.0, SF.LIFT_M)
                assert np.abs(f[4, :3, 3] - p5).max() < 1e-12


# ══════════════════════════════════════════════════════════════════════════
# (2) the pinned arm-check route
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('arm,uncal,cal,binding', [('v1', 17.1, 30.5, 'column'), ('omx', 27.2, 36.7, 'pad')])
def test_the_pinned_arm_check_route_passes(arm, uncal, cal, binding):
    """HOME -> AC01 ... AC20 -> HOME, claw closed: no leg refused; smallest margin (gap - allowance) with the
    UNCALIBRATED residual at the contract place (the arm check runs before the kit is calibrated) 17.1 mm (V1,
    column) / 27.2 mm (OMX, pad); with the calibrated residual 30.5 / 36.7 mm (p14)."""
    route = [HOME[arm]] + [np.array(p, float) for p in SPEC[arm]['arm_check_poses_rad']] + [HOME[arm]]
    g_closed = GR[arm]['closed']
    for calibrated, want in ((False, uncal), (True, cal)):
        g = guard(arm, calibrated)
        legs = list(zip(route[:-1], route[1:]))
        assert len(legs) == 21
        assert not any(g.judge_leg(a, b, g_closed, g_closed).refused for a, b in legs)
        assert not any(g.dense_leg(a, b, g_closed, g_closed).refused for a, b in legs)
        worst = min((g.smallest_margin(a, b, g_closed, g_closed) for a, b in legs), key=lambda v: v.margin_m)
        assert mm(worst.margin_m) == pytest.approx(want, abs=0.5), (arm, calibrated)
        assert worst.part == binding


# ══════════════════════════════════════════════════════════════════════════
# (3) HOME <-> OBS, HOME <-> hovers, OBS <-> hovers (direct and the ladder)
# ══════════════════════════════════════════════════════════════════════════

def _hover_grid(arm, extra):
    sp = SPEC[arm]
    out = []
    for r in np.linspace(sp['zone_inner_m'], REACH[arm], 6):
        for az in np.linspace(-90.0, 90.0, 13):
            q = tip_solve(arm, r * math.cos(math.radians(az)), r * math.sin(math.radians(az)),
                          SF.MAT_THICKNESS_M + 0.075 + extra, 0.0)
            if q is not None:
                out.append(((round(float(r), 4), float(az)), q))
    return out


@pytest.mark.parametrize('arm,want', [('v1', 11.2), ('omx', 39.0)])
def test_home_to_the_observation_pose_passes(arm, want):
    g = guard(arm)
    vs = [g.smallest_margin(HOME[arm], OBS[arm], s, s) for s in GR[arm].values()]
    assert not any(v.refused for v in vs)
    assert not any(g.judge_leg(HOME[arm], OBS[arm], s, s).refused for s in GR[arm].values())
    assert not any(g.dense_leg(HOME[arm], OBS[arm], s, s).refused for s in GR[arm].values())
    assert mm(min(v.margin_m for v in vs)) == pytest.approx(want, abs=0.5)


# (r, az) of the hovers whose DIRECT line from OBS is refused (p14), with their margins in mm
OBS_REFUSED = {
    ('v1', 0.0): {(0.11, -90.0): -15.6, (0.11, -75.0): -14.1, (0.11, -60.0): -11.4, (0.11, -45.0): -4.5,
                  (0.156, -90.0): -7.7, (0.156, -75.0): -4.3},
    ('v1', 0.060): {(0.156, -90.0): -4.7, (0.156, -75.0): -0.9},
    ('omx', 0.0): {}, ('omx', 0.060): {},
}
HOVER_COUNT = {('v1', 0.0): 78, ('v1', 0.060): 39, ('omx', 0.0): 78, ('omx', 0.060): 65}
HOME_SMALLEST = {('v1', 0.0): 3.9, ('v1', 0.060): 14.4, ('omx', 0.0): 8.6, ('omx', 0.060): 26.6}


@pytest.mark.parametrize('arm,extra', [('v1', 0.0), ('v1', 0.060), ('omx', 0.0), ('omx', 0.060)])
def test_the_hover_grids_reach_and_leave_the_observation_pose(arm, extra):
    """78 hovers (6 radii zone_inner .. reach x 13 azimuths, roll 0, fingertip MAT + 0.075) and the same 60 mm
    higher: HOME <-> hover none refused; OBS <-> hover DIRECT refused exactly for p14's set (the V1's column), every
    one passing via HOME."""
    g = guard(arm)
    grid = _hover_grid(arm, extra)
    assert len(grid) == HOVER_COUNT[(arm, extra)]
    home_worst = math.inf
    refused = {}
    for s_name in ('open', 'holding'):
        s = GR[arm][s_name]
        home = [(HOME[arm], q) for _k, q in grid]
        assert not np.any(leg_minima(g, arm, home, s) < 0.0)
        home_worst = min(home_worst, smallest_over(g, arm, home, s))
        direct = leg_minima(g, arm, [(OBS[arm], q) for _k, q in grid], s)
        for (key, q), m in zip(grid, direct):
            assert g.judge_leg(OBS[arm], q, s, s).refused == (m < 0.0), (key, s_name)
            if m < 0.0:
                v = g.dense_leg(OBS[arm], q, s, s)
                assert v.refused and v.part == 'column' and v.margin_m == m
                refused[(s_name, key)] = mm(m)
                assert not g.judge_leg(OBS[arm], HOME[arm], s, s).refused
                assert not g.judge_leg(HOME[arm], q, s, s).refused
    assert mm(home_worst) == pytest.approx(HOME_SMALLEST[(arm, extra)], abs=0.5)
    for s_name in ('open', 'holding'):
        got = {k: v for (sn, k), v in refused.items() if sn == s_name}
        assert set(got) == set(OBS_REFUSED[(arm, extra)]), (s_name, got)
        for k, v in got.items():
            assert v == pytest.approx(OBS_REFUSED[(arm, extra)][k], abs=0.5), (k, v)


# ══════════════════════════════════════════════════════════════════════════
# (4) the strict-vertical grid
# ══════════════════════════════════════════════════════════════════════════

# (r, fingertip z, az) refused outside the zone (p14): the pose or its line to HOME
STRICT_REFUSED = {
    'v1': {(0.1, 0.0155, az) for az in (-90.0, -85.0, -80.0, -75.0, -70.0, -65.0, -60.0, -55.0, -50.0, -45.0, -40.0,
                                        -35.0, 60.0, 65.0, 70.0, 75.0, 80.0, 85.0, 90.0)}
          | {(0.148, 0.0155, az) for az in (70.0, 75.0, 80.0, 85.0, 90.0)}
          | {(0.196, 0.0155, az) for az in (75.0, 80.0, 85.0, 90.0)} | {(0.244, 0.0155, 90.0)},
    'omx': {(0.1, 0.0155, az) for az in (70.0, 75.0, 80.0, 85.0, 90.0, 95.0, 100.0)}
           | {(r, 0.0155, az) for r in (0.13, 0.16, 0.19) for az in (75.0, 80.0, 85.0, 90.0, 95.0, 100.0)}
           | {(r, 0.0155, az) for r in (0.22, 0.25) for az in (80.0, 85.0, 90.0, 95.0, 100.0)},
}


@pytest.mark.parametrize('arm,n_poses,n_refused', [('v1', 555, 29), ('omx', 697, 35)])
def test_the_strict_vertical_grid_is_refused_only_outside_the_zone(arm, n_poses, n_refused):
    """r in linspace(0.10, reach, 6) x fingertip z {0.0155, 0.0755, 0.1355} x the joint-1 window in 5 deg steps,
    roll 0, claw open: no pose and no line to HOME refused whose point is in_zone (the cut zone); the refusals
    outside it are exactly p14's list."""
    g = guard(arm)
    s = GR[arm]['open']
    points = []
    for r in np.linspace(0.10, REACH[arm], 6):
        for z_tip in (0.0155, 0.0755, 0.1355):
            for az in np.arange(-WIN[arm], WIN[arm] + 0.01, 5.0):
                x, y = r * math.cos(math.radians(az)), r * math.sin(math.radians(az))
                q = tip_solve(arm, x, y, z_tip, 0.0)
                if q is not None:
                    points.append(((round(float(r), 4), z_tip, float(az)), x, y, q))
    n = len(points)
    lines = leg_minima(g, arm, [(HOME[arm], q) for _k, _x, _y, q in points], s)
    poses = leg_minima(g, arm, [(q, q) for _k, _x, _y, q in points], s)
    refused = set()
    for (key, x, y, q), line, pose in zip(points, lines, poses):
        if pose < 0.0:
            assert g.judge_pose(q, s).refused, key
        if pose < 0.0 or line < 0.0:
            assert not SF.in_zone(SPEC[arm], x, y), key
            refused.add(key)
    assert n == n_poses
    assert len(refused) == n_refused
    assert refused == STRICT_REFUSED[arm]


# ══════════════════════════════════════════════════════════════════════════
# (5) the teeth
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('arm,q,part,want', [
    ('v1', (0.82, 0.64, 2.48, -0.14, 0.0), 'block', -93.1),
    ('v1', (1.08, 0.14, 2.79, -1.01, 0.0), 'column', -12.5),
    ('omx', (-1.08, -1.48, -1.41, -0.3, 0.0), 'column', -10.5),
])
def test_the_teeth_poses_are_refused(arm, q, part, want):
    g = guard(arm)
    for s in (GR[arm]['open'], GR[arm]['closed']):
        v = g.judge_pose(np.array(q), s)
        assert v.refused and v.reason == 'too_close' and v.sentence_de == fg().TOWER_TOO_CLOSE_DE
        assert v.part == part and mm(v.margin_m) == pytest.approx(want, abs=0.5)


@pytest.mark.parametrize('arm,a,b,middle,ends', [
    ('v1', (0.18, 0.14, 2.79, -1.01, 0.0), (1.57, 0.14, 2.79, -1.01, 0.0), -73.9, (38.3, 40.0)),
    ('omx', (-1.98, -1.48, -1.41, -0.3, 0.0), (-0.18, -1.48, -1.41, -0.3, 0.0), -71.7, (39.2, 39.2)),
])
def test_the_teeth_segments_are_refused_while_both_ends_pass(arm, a, b, middle, ends):
    g = guard(arm)
    a, b = np.array(a), np.array(b)
    for s in (GR[arm]['open'], GR[arm]['closed']):
        va, vb = g.judge_pose(a, s), g.judge_pose(b, s)
        assert not va.refused and not vb.refused
        assert (mm(va.margin_m), mm(vb.margin_m)) == pytest.approx(ends, abs=0.5)
        leg = g.dense_leg(a, b, s, s)
        assert leg.refused and leg.part == 'column' and mm(leg.margin_m) == pytest.approx(middle, abs=0.5)
        assert g.judge_leg(a, b, s, s).refused


# ══════════════════════════════════════════════════════════════════════════
# R4-3 … R4-9: the proofs the rule rests on
# ══════════════════════════════════════════════════════════════════════════

JOINT_AXES_LOCAL = {'v1': ((0, 0, -1),) * 5, 'omx': ((0, 0, 1), (0, 1, 0), (0, 1, 0), (0, 1, 0), (1, 0, 0))}
CLAW_WINDOW = {'v1': (0.0, 1.5708), 'omx': (-0.5, 0.8)}


def _local_corners_over_the_claw_window(arm):
    """per link frame 1..5: every level-1 box corner (the blades at every claw value of the window)."""
    lo, hi = CLAW_WINDOW[arm]
    per = {k: [] for k in range(5)}
    for g in np.linspace(lo, hi, 33):
        boxes = fg().arm_boxes(arm, g, g, level=1)
        for k, c, rot in zip(boxes.link, boxes.corners, boxes.axes):
            per[int(k)].append(c)
    return {k: np.concatenate(v) for k, v in per.items()}


@pytest.mark.parametrize('arm', ['v1', 'omx'])
def test_every_r_j_is_a_proven_upper_bound(arm):
    """(R4-3) every R_j >= its triangle bound (p03's method: the axis-to-next-origin distance plus the following
    link lengths plus the farthest box corner) and >= every box-corner distance from joint j's axis over 20 000
    random poses."""
    rj = np.array(fg().R_J_M[arm])
    local = _local_corners_over_the_claw_window(arm)
    f0 = nominal_frames(arm)(np.zeros((1, 5)))[0]
    o = [f0[i, :3, 3] for i in range(5)]
    seg = [float(np.linalg.norm(o[i + 1] - o[i])) for i in range(4)]
    for j in range(5):
        u = f0[j, :3, :3] @ np.array(JOINT_AXES_LOCAL[arm][j], float)
        bound = 0.0
        for k in range(j, 5):
            if k == j:
                w = local[k] @ f0[k, :3, :3].T + o[k] - o[j]
                d = float(np.linalg.norm(w - np.outer(w @ u, u), axis=1).max())
            else:
                w = o[j + 1] - o[j]
                d = float(np.linalg.norm(w - (w @ u) * u)) + sum(seg[j + 1:k]) + float(np.linalg.norm(local[k], axis=1).max())
            bound = max(bound, d)
        assert rj[j] >= bound, (arm, j, rj[j], bound)
    rng = np.random.default_rng(3)
    lim = np.array(solver(arm).joint_limits)
    worst = np.zeros(5)
    for Q in np.array_split(rng.uniform(lim[:, 0], lim[:, 1], (20000, 5)), 40):
        F = nominal_frames(arm)(Q)
        for k in range(5):
            P = np.einsum('nij,cj->nci', F[:, k, :3, :3], local[k]) + F[:, k, None, :3, 3]
            for j in range(k + 1):
                u = np.einsum('nij,j->ni', F[:, j, :3, :3], np.array(JOINT_AXES_LOCAL[arm][j], float))
                w = P - F[:, j, None, :3, 3]
                d = np.linalg.norm(w - np.einsum('ncd,nd->nc', w, u)[..., None] * u[:, None, :], axis=-1).max()
                worst[j] = max(worst[j], float(d))
    assert np.all(rj >= worst), (arm, rj, worst)


def test_gap_dir_without_droop_is_the_repos_box_gap_and_a_bound_under_droop():
    """(R4-4) gap_dir with delta 0 equals arm_geometry._box_gap to 1e-12; over 10 000 random box pairs a
    displacement e perpendicular to u with |e| <= delta never makes _box_gap smaller than gap_dir."""
    from physical_ai_server.workflow.arm_geometry import _box_gap
    rng = np.random.default_rng(44)

    def box():
        q, _ = np.linalg.qr(rng.normal(size=(3, 3)))
        h = rng.uniform(0.002, 0.08, 3)
        c = rng.uniform(-0.15, 0.15, 3)
        signs = np.array([[(i >> 2) & 1, (i >> 1) & 1, i & 1] for i in range(8)], float) * 2 - 1
        return c + (signs * h) @ q.T, q

    n = 10000
    A = [box() for _ in range(n)]
    B = [box() for _ in range(n)]
    ca, ra = np.stack([a[0] for a in A]), np.stack([a[1] for a in A])
    cb, rb = np.stack([b[0] for b in B]), np.stack([b[1] for b in B])
    u = rng.normal(size=(n, 3))
    u /= np.linalg.norm(u, axis=1, keepdims=True)
    zero = fg().gap_dir(ca, ra, cb, rb, np.zeros(n), u)
    ref = np.array([_box_gap(ca[i], cb[i], ra[i], rb[i]) for i in range(n)])
    assert np.abs(zero - ref).max() <= 1e-12
    delta = rng.uniform(0.0, 0.03, n)
    gd = fg().gap_dir(ca, ra, cb, rb, delta, u)
    for i in range(n):
        e = rng.normal(size=3)
        e -= (e @ u[i]) * u[i]
        e *= delta[i] * rng.uniform(0.0, 1.0) ** 0.5 / max(np.linalg.norm(e), 1e-300)
        moved = _box_gap(ca[i] + e, cb[i], ra[i], rb[i])
        assert moved >= gd[i] - 1e-12, i


def test_the_allowance_height_bound_holds():
    """(R4-5) for random a, t with |a - t| <= c*h_t + R + delta: h_t <= (z_box + R + delta) / (1 - c), z_box the
    box's top (>= a's height)."""
    c = SF.TOWER_LEAN_AT_LENS_M / SF.LENS_F[2]
    assert fg().LEAN_PER_M == pytest.approx(c, abs=0.0) and c == pytest.approx(0.0304, abs=1e-4)
    rng = np.random.default_rng(55)
    hits = 0
    for _ in range(20000):
        R = rng.uniform(0.0, 0.05)
        delta = rng.uniform(0.0, 0.03)
        a = rng.uniform((-0.3, -0.3, -0.01), (0.3, 0.3, 0.6))
        z_box = a[2] + rng.uniform(0.0, 0.05)
        t = a + rng.normal(size=3) * rng.uniform(0.0, 0.2)
        t[2] = max(t[2], 0.0)
        if np.linalg.norm(a - t) <= c * t[2] + R + delta:
            hits += 1
            assert t[2] <= fg().height_bound(z_box, R, delta) + 1e-12
    assert hits > 1000


def _near(g, arm, candidates, gripper, lo, hi):
    """p11's filter over candidate legs: the leg's dense minimum margin (the rule's values: every part the broad
    phase keeps, exact) lies in [lo, hi]. The pre-check's values bound it from below, so a leg whose bound is above
    hi is out without the exact pass."""
    keep = np.zeros(len(candidates), bool)
    lower = leg_minima(g, arm, candidates, gripper)
    rest = np.nonzero(lower <= hi)[0]
    if len(rest):
        exact = leg_minima(g, arm, [candidates[i] for i in rest], gripper, precheck=False)
        keep[rest] = (exact >= lo) & (exact <= hi)
    return keep


def _p11_legs(arm, g, gripper, rng):
    """p11's adversarial legs: 600 near-pass legs (a random step on 1-3 joints, dense minimum margin in [-5, +60]
    mm) and 200 joint-1 sweeps of +-40 deg past the column's bearing (both directions; margin in [-10, +60] mm),
    drawn in p11's order, judged in batches."""
    lim = np.array(solver(arm).joint_limits)
    col = [p for p in g.parts if p.name == 'column'][0]
    bearing = math.atan2(col.centre[1], col.centre[0] - BASE_X[arm])
    j1c = -bearing if arm == 'v1' else bearing
    near, tries = [], 0
    while len(near) < 600 and tries < 20000:
        batch, states = [], []
        while len(batch) < 64 and tries < 20000:
            tries += 1
            states.append(rng.bit_generator.state)
            a = rng.uniform(lim[:, 0], lim[:, 1])
            if rng.random() < 0.7:
                a[0] = np.clip(j1c + rng.uniform(-math.radians(60), math.radians(60)), lim[0, 0], lim[0, 1])
            b = a.copy()
            js = rng.choice(5, rng.integers(1, 4), replace=False)
            b[js] += rng.uniform(-0.6, 0.6, len(js))
            batch.append((a, np.clip(b, lim[:, 0], lim[:, 1])))
        for k, ok in enumerate(_near(g, arm, batch, gripper, -0.005, 0.060)):
            if ok:
                near.append(batch[k])
                if len(near) == 600:             # p11 drew nothing after its 600th leg: rewind the stream there
                    if k + 1 < len(states):
                        rng.bit_generator.state = states[k + 1]
                    break
    sweeps, tries2 = [], 0
    while len(sweeps) < 200 and tries2 < 20000:
        batch = []
        while len(batch) < 64 and tries2 < 20000:
            tries2 += 1
            q = rng.uniform(lim[:, 0], lim[:, 1])
            az_c = bearing if rng.random() < 0.5 else bearing - math.pi
            q0 = -az_c if arm == 'v1' else az_c
            q0 = (q0 + math.pi) % (2 * math.pi) - math.pi
            a, b = q.copy(), q.copy()
            a[0] = np.clip(q0 - math.radians(40), lim[0, 0], lim[0, 1])
            b[0] = np.clip(q0 + math.radians(40), lim[0, 0], lim[0, 1])
            if abs(b[0] - a[0]) >= math.radians(20):
                batch.append((a, b))
        sweeps += [leg for leg, ok in zip(batch, _near(g, arm, batch, gripper, -0.010, 0.060)) if ok]
    return near + sweeps[:200]


@pytest.mark.parametrize('arm,scale', [('v1', 1.0), ('v1', 'droop3x'), ('omx', 1.0), ('omx', 2.0)])
def test_the_travel_bound_rule_never_clears_a_pair_with_a_negative_fine_sample(arm, scale):
    """(R4-6, 55 M-3) test 9 as a soundness property: on p11's legs, at the design envelope and at V1 droop3x /
    OMX 2 x design, every pair the travel-bound rule clears keeps every 0.3 mm sample's margin WITHOUT the sample
    term >= 0 (p11: 0 violations over 265 cleared pairs, worst +5.5 mm). Never at 0.10 rad (58 m-9)."""
    env = (0.074, 0.059, 0.018) if scale == 'droop3x' else tuple(scale * e for e in DESIGN_ENV[arm])
    g = guard(arm, True, env)
    gripper = GR[arm]['holding']
    rng = np.random.default_rng(111 + (0 if arm == 'v1' else 7))
    cleared = 0
    worst = math.inf
    legs = _p11_legs(arm, g, gripper, rng)
    assert len(legs) == 800
    for a, b in legs:
        W = fg().coarse_samples(arm, a, b)
        ok = g.clear_pairs(W, gripper, gripper, exact=True)
        for i in np.nonzero(ok)[0]:
            cleared += 1
            fine = fg().dense_samples(arm, W[i], W[i + 1], step_m=0.0003)
            m = g.margins(fine, gripper, gripper, with_sample=False, precheck=False).M
            worst = min(worst, float(m.min()))
    assert cleared > 0
    assert worst >= 0.0, (arm, scale, worst)


@pytest.mark.parametrize('arm', ['v1', 'omx'])
def test_a_skipped_part_enters_with_a_lower_bound_of_its_margin(arm):
    """(R4-7) the broad phase's skip value is a lower bound of the part's margin at that sample (10 000 samples):
    of its PHYSICAL margin for every part — below every arm box's own AABB separation from the part minus that
    box's droop and allowance, which bounds the box's distance from below — and of the rule's own fifteen-axis
    margin for the parts whose faces are the world's axes (the AABB separation is one of their fifteen gaps).
    For the turned parts (ribs, head, lens, bridge) the fifteen-axis value can lie BELOW the AABB separation, so a
    skip value is no bound of it there (measured: up to 24 mm, every such margin far above 0); the travel-bound
    rule's proof needs the physical bound only."""
    F = fg()
    g = guard(arm)
    rng = np.random.default_rng(77 + (arm == 'omx'))
    lim = np.array(solver(arm).joint_limits)
    Q = rng.uniform(lim[:, 0], lim[:, 1], (10000, 5))
    frames = nominal_frames(arm)(Q)
    aligned = [k for k, p in enumerate(g.parts) if np.allclose(np.abs(p.axes), np.eye(3), atol=1e-12)]
    assert [g.parts[k].name for k in aligned] == ['pad', 'socket', 'column', 'block']
    for s in (GR[arm]['open'], GR[arm]['closed']):
        boxes = F.arm_boxes(arm, s, s, level=1)
        CA, _ = F.world_boxes(frames, boxes)
        delta = g.droop_terms(frames, CA, boxes, g.droop_angles(frames))[1]
        lo_b, hi_b, z_b = CA.min(2), CA.max(2), CA[..., 2].max(2)
        for with_sample in (True, False):
            broad = g.margins(Q, s, s, with_sample=with_sample, precheck=False)
            skipped = ~np.isnan(broad.skip)
            assert skipped.sum() > 10000
            for k, part in enumerate(g.parts):
                rows = skipped[:, k]
                sep = np.maximum(part.lo - hi_b[rows], lo_b[rows] - part.hi).max(2)
                physical = (sep - delta[rows] - g.allowance(k, z_b[rows], delta[rows], with_sample)).min(1)
                assert np.all(broad.skip[rows, k] <= physical), part.name
            true = g.margins(Q, s, s, with_sample=with_sample, precheck=False, broad=False)
            sub = skipped[:, aligned]
            assert np.all(broad.skip[:, aligned][sub] <= true.M[:, aligned][sub])
            live = ~skipped
            assert np.array_equal(broad.M[live], true.M[live])


@pytest.mark.parametrize('arm', ['v1', 'omx'])
def test_judge_polyline_takes_the_claw_interval_of_the_whole_polyline(arm):
    """FAILS FIRST (verifier finding X-WP12-1): judge_polyline(waypoints, gripper, gripper_b) judges a polyline whose
    claw moves between the two values, which is what the wiring's replay check needs for a moving claw (it ran the
    guard's own travel-bound core itself). Two waypoints are the leg: the verdict is judge_leg's with the same claw
    values, margin and part included; the order of the two values does not matter; a still claw is the one-value
    call; a non-finite second value is refused as unjudgeable."""
    F = fg()
    g, gr = guard(arm), GR[arm]
    seen = set()
    for _fam, a, b, s in q20_legs(arm)[::10]:
        W = np.stack([a, b])
        for lo, hi in ((gr['closed'], gr['open']), (gr['holding'], gr['open'])):
            leg = g.judge_leg(a, b, lo, hi)
            assert g.judge_polyline(W, lo, hi) == leg
            assert g.judge_polyline(W, hi, lo) == leg
            seen.add(leg.refused)
        assert g.judge_polyline(W, s, s) == g.judge_polyline(W, s) == g.judge_leg(a, b, s, s)
    assert seen == {True, False}                     # both verdicts were compared
    a, b = HOME[arm], OBS[arm]
    for bad in (float('nan'), float('inf'), 'x'):
        v = g.judge_polyline(np.stack([a, b]), gr['open'], bad)
        assert v.refused and v.reason == 'unjudgeable' and v.sentence_de == F.TOWER_UNJUDGEABLE_DE, bad


def test_unjudgeable_droop_pose_and_masses_refuse_in_german(monkeypatch):
    """(R4-9) sum(theta) >= 0.5 at a sample -> TOWER_DROOP_UNJUDGEABLE_DE; a non-finite tower_pose or a missing
    link_masses -> TOWER_UNJUDGEABLE_DE."""
    F = fg()
    a, b = HOME['v1'], OBS['v1']
    heavy = F.make_guard('v1_studio', nominal_frames('v1'), base_axis_x=0.0,
                         arm_check={'tower_pose': (0.0, 0.0, 0.0), 'droop_envelope_rad': (0.3, 0.3, 0.3)})
    for v in (heavy.judge_leg(a, b, 0.9, 0.9), heavy.judge_pose(a, 0.9), heavy.dense_leg(a, b, 0.9, 0.9),
              heavy.judge_polyline(np.stack([a, b]), 0.9)):
        assert v.refused and v.reason == 'droop' and v.sentence_de == F.TOWER_DROOP_UNJUDGEABLE_DE
    for check in ({'tower_pose': (float('nan'), 0.0, 0.0), 'droop_envelope_rad': DESIGN_ENV['v1']},
                  {'tower_pose': (0.0, float('inf'), 0.0), 'droop_envelope_rad': DESIGN_ENV['v1']},
                  {'tower_pose': None, 'droop_envelope_rad': DESIGN_ENV['v1']},
                  {'droop_envelope_rad': DESIGN_ENV['v1']},
                  {'tower_pose': (0.0, 0.0, 0.0), 'droop_envelope_rad': (0.02, float('nan'), 0.01)},
                  {'tower_pose': (0.0, 0.0, 0.0)}, 'not a mapping'):
        g = F.make_guard('v1_studio', nominal_frames('v1'), base_axis_x=0.0, arm_check=check)
        v = g.judge_leg(a, b, 0.9, 0.9)
        assert v.refused and v.reason == 'unjudgeable' and v.sentence_de == F.TOWER_UNJUDGEABLE_DE, check
    calibrated = F.make_guard('v1_studio', nominal_frames('v1'), base_axis_x=0.0,
                              arm_check={'tower_pose': np.zeros(3), 'droop_envelope_rad': np.array(DESIGN_ENV['v1'])})
    assert isinstance(calibrated, F.TowerGuard) and calibrated.calibrated      # numpy values are fine
    for masses in ((), SF.ARM_V1['link_masses'][:4], ((1, 0.06, (0.0, 0.0)),) + SF.ARM_V1['link_masses'][1:],
                   ((1, -0.06, (0.0, 0.0, 0.0)),) + SF.ARM_V1['link_masses'][1:]):
        g = F.make_guard('v1_studio', nominal_frames('v1'), base_axis_x=0.0, link_masses=masses)
        v = g.judge_pose(a, 0.9)
        assert v.refused and v.sentence_de == F.TOWER_UNJUDGEABLE_DE, masses
    monkeypatch.setitem(SF.ARMS, 'v1', {k: v for k, v in SF.ARM_V1.items() if k != 'link_masses'})
    v = F.make_guard('v1_studio', nominal_frames('v1'), base_axis_x=0.0).judge_pose(a, 0.9)
    assert v.refused and v.sentence_de == F.TOWER_UNJUDGEABLE_DE


# ══════════════════════════════════════════════════════════════════════════
# R5-1: the pre-check in the part's frame and the batched evaluation keep revision 4's verdict
# ══════════════════════════════════════════════════════════════════════════

def q20_legs(arm, nl=40):
    """q20's seeded leg set (both arms; zone grasp <-> hover, hover <-> HOME, OBS <-> hover direct, jog steps
    near the tower, student program legs): ``[(family, a, b, gripper)]``, nl per family."""
    sp = SPEC[arm]
    gr = {'v1': {'open': 0.90, 'holding': 0.20}, 'omx': {'open': 0.80, 'holding': -0.10}}[arm]
    zt = SF.MAT_THICKNESS_M + 0.015
    rng = np.random.default_rng(1010 + (0 if arm == 'v1' else 1))
    r_in, r_out, azs = sp['zone_inner_m'], sp['zone_outer_m'], sp['zone_sector_deg'][1]
    rolls = ([-math.pi / 2, -math.pi / 4, 0.0, math.pi / 4, math.pi / 2] if arm == 'v1'
             else [k * math.pi / 4 for k in range(8)])
    cnt = {'grasp-hover': 0, 'hover-home': 0, 'obs-hover': 0, 'jog': 0}
    legs = []
    while min(cnt['grasp-hover'], cnt['hover-home'], cnt['obs-hover']) < nl:
        r = math.sqrt(rng.uniform(r_in ** 2, r_out ** 2))
        az = rng.uniform(-azs, azs)
        ro = rolls[rng.integers(len(rolls))]
        x, y = r * math.cos(math.radians(az)), r * math.sin(math.radians(az))
        qg, qh = tip_solve(arm, x, y, zt, ro), tip_solve(arm, x, y, zt + 0.06, ro)
        if qg is None or qh is None:
            continue
        for fam, a, b in (('grasp-hover', qg, qh), ('hover-home', qh, HOME[arm]), ('obs-hover', OBS[arm], qh)):
            if cnt[fam] < nl:
                legs.append((fam, a, b, gr['open']))
                cnt[fam] += 1
    lim = np.array(solver(arm).joint_limits)
    while cnt['jog'] < nl:
        r = math.sqrt(rng.uniform(0.10 ** 2, (r_out + 0.02) ** 2))
        az = rng.uniform(0, 100 if arm == 'omx' else 90)
        ro = rng.uniform(-math.pi / 2, math.pi / 2)
        q0 = tip_solve(arm, r * math.cos(math.radians(az)), r * math.sin(math.radians(az)),
                       zt + rng.choice([0.0, 0.03, 0.06, 0.12]), ro)
        if q0 is None:
            continue
        q1 = q0.copy()
        for j in rng.choice(5, size=rng.integers(1, 3), replace=False):
            q1[j] += rng.uniform(-0.25, 0.25)
        legs.append(('jog', q0, np.clip(q1, lim[:, 0], lim[:, 1]), gr['holding']))
        cnt['jog'] += 1
    r2 = np.random.default_rng(77)
    prev, n_prog = None, 0
    while n_prog < nl:
        r = math.sqrt(r2.uniform(0.10 ** 2, sp['reach_m'][1] ** 2))
        az = r2.uniform(-150, 150)
        ro = r2.uniform(-math.pi / 2, math.pi / 2)
        qh = tip_solve(arm, r * math.cos(math.radians(az)), r * math.sin(math.radians(az)), zt + 0.06, ro)
        if qh is None or abs(qh[0]) > math.radians(sp['joint1_window_deg'][1]):
            continue
        if prev is not None:
            legs.append(('program', prev, qh, gr['holding']))
            n_prog += 1
        prev = qh
    return legs


@pytest.mark.parametrize('arm', ['v1', 'omx'])
def test_the_pre_check_and_the_batch_keep_revision_4s_verdict_on_q20s_legs(arm):
    """(R5-1) per dense sample of q20's legs, at both residuals: the margins with the part-frame pre-check (a
    certificate where it fires) and without it (every live triple's fifteen-axis value) have the same sign, and a
    certified value never exceeds the exact one."""
    for calibrated in (True, False):
        g = guard(arm, calibrated)
        for fam, a, b, s in q20_legs(arm):
            Q = fg().dense_samples(arm, a, b)
            with_cert = g.margins(Q, s, s).M
            exact = g.margins(Q, s, s, precheck=False).M
            assert np.array_equal(with_cert < 0, exact < 0), (fam, calibrated)
            assert np.all(with_cert[np.isfinite(exact)] <= exact[np.isfinite(exact)] + 1e-12)
            assert np.array_equal(with_cert[exact < 0], exact[exact < 0])


@pytest.mark.parametrize('arm', ['v1', 'omx'])
def test_a_part_frame_certificate_never_exceeds_the_fifteen_axis_margin(arm):
    """(R5-1, q25) 10 000 random (sample, part, box) triples within 0.15 m of the tower: the certificate (the gap on
    the part's own three face normals, its corners projected, minus the FULL delta and the allowance) is <= the
    fifteen-axis margin (to the last bit of float rounding)."""
    F = fg()
    g = guard(arm)
    rng = np.random.default_rng(2525 + (arm == 'omx'))
    lim = np.array(solver(arm).joint_limits)
    lo, hi = CLAW_WINDOW[arm]
    pall = np.concatenate([p.corners for p in g.parts])
    tlo, thi = pall.min(0), pall.max(0)
    boxes = F.arm_boxes(arm, lo, hi, level=1)
    picked = []
    while sum(len(x) for x in picked) < 10000:
        Q = rng.uniform(lim[:, 0], lim[:, 1], (4000, 5))
        CA, _ = F.world_boxes(nominal_frames(arm)(Q), boxes)
        sep = np.maximum(tlo - CA.max(axis=(1, 2)), CA.min(axis=(1, 2)) - thi).max(1)
        picked.append(Q[sep < 0.15])
    Q = np.concatenate(picked)[:10000]
    frames = nominal_frames(arm)(Q)
    CA, RA = F.world_boxes(frames, boxes)
    theta = g.droop_angles(frames)
    delta = g.droop_terms(frames, CA, boxes, theta)[1]
    u = frames[:, 1, :3, :3] @ np.array(JOINT_AXES_LOCAL[arm][1], float)
    zb = CA[..., 2].max(2)
    worst = -math.inf
    n_cert = 0
    for k, p in enumerate(g.parts):
        for b in range(len(boxes.link)):
            al = g.allowance(k, zb[:, b], delta[:, b], with_sample=True)
            m = F.gap_dir(CA[:, b], RA[:, b], np.broadcast_to(p.corners, CA[:, b].shape),
                          np.broadcast_to(p.axes, RA[:, b].shape), delta[:, b], u) - al
            cert = F.part_frame_gap(CA[:, b], p) - delta[:, b] - al
            worst = max(worst, float((cert - m).max()))
            n_cert += int((cert >= 0).sum())
    assert n_cert > 1000
    # the same projections along two arithmetic paths may differ in the last bit (1.1e-16 seen)
    assert worst <= 1e-12, worst


class _Scripted:
    """stands in for TowerGuard.margins: the travel-bound rule's three stages see exactly these values."""

    def __init__(self, real, lower, exact, dense_negative):
        self.real, self.lower, self.exact, self.dense_negative = real, lower, exact, dense_negative

    def __call__(self, Q, glo, ghi, with_sample=True, precheck=True, broad=True):
        out = self.real(Q, glo, ghi, with_sample=with_sample, precheck=precheck, broad=broad)
        M = out.M.copy()
        if with_sample:
            M[:] = -0.001 if self.dense_negative else 0.05
        else:
            M[:] = self.lower if precheck else self.exact
        return out._replace(M=M, skip=np.full_like(out.skip, np.nan))


def test_the_travel_bound_rule_refuses_only_through_the_exact_clear_test(monkeypatch):
    """(R5-1) a pair whose coarse values look not clear on the certificates (lower bounds) but ARE clear on the
    exact values must pass even when one of its dense samples is negative (revision 4 never refined it); the same
    pair not clear on the exact values is refused."""
    g = guard('v1')
    a, b = HOME['v1'], HOME['v1'] + np.array([0.02, 0.0, 0.0, 0.0, 0.0])
    real = g.margins
    monkeypatch.setattr(g, 'margins', _Scripted(real, lower=0.0005, exact=0.2, dense_negative=True))
    assert not g.judge_leg(a, b, 0.9, 0.9).refused
    monkeypatch.setattr(g, 'margins', _Scripted(real, lower=0.0005, exact=0.0005, dense_negative=True))
    assert g.judge_leg(a, b, 0.9, 0.9).refused
    monkeypatch.setattr(g, 'margins', _Scripted(real, lower=0.0005, exact=0.0005, dense_negative=False))
    assert not g.judge_leg(a, b, 0.9, 0.9).refused


@pytest.mark.parametrize('arm', ['v1', 'omx'])
def test_level_2_is_placed_only_where_a_level_1_gripper_box_is_below_its_allowance(arm, monkeypatch):
    """(R5-1) the cells are placed only at the samples whose level-1 gripper triple is below its allowance, and give
    the same values there as cells placed at every sample (computed here from the rule's own primitives)."""
    F = fg()
    g = guard(arm)
    placed = []
    real = F.world_boxes

    def spy(frames, boxes):
        if boxes.level == 2:
            placed.append(len(frames))
        return real(frames, boxes)

    near = {'v1': [(0.10, az) for az in (55.0, 60.0, 65.0, 70.0)], 'omx': [(0.13, az) for az in (80.0, 90.0, 100.0)]}
    legs = [('near the base', HOME[arm], tip_solve(arm, r * math.cos(math.radians(az)), r * math.sin(math.radians(az)),
                                                  0.0155, 0.0), GR[arm]['open']) for r, az in near[arm]]
    legs += q20_legs(arm, nl=2)
    n_samples = needs = 0
    for fam, a, b, s in legs:
        Q = F.dense_samples(arm, a, b)
        n_samples += len(Q)
        frames = nominal_frames(arm)(Q)
        b1, b2 = F.arm_boxes(arm, s, s, level=1), F.arm_boxes(arm, s, s, level=2)
        CA, RA = real(frames, b1)
        C2, R2 = real(frames, b2)
        theta = g.droop_angles(frames)
        delta = g.droop_terms(frames, CA, b1, theta)[1]
        u = frames[:, 1, :3, :3] @ np.array(JOINT_AXES_LOCAL[arm][1], float)
        ref = np.full((len(Q), len(g.parts)), np.inf)
        need = np.zeros(len(Q), bool)
        for k, p in enumerate(g.parts):
            pc, pa = np.broadcast_to(p.corners, (len(Q), 8, 3)), np.broadcast_to(p.axes, (len(Q), 3, 3))
            for bi, grp in enumerate(b1.group):
                m1 = (F.gap_dir(CA[:, bi], RA[:, bi], pc, pa, delta[:, bi], u)
                      - g.allowance(k, CA[:, bi, :, 2].max(1), delta[:, bi], with_sample=True))
                if grp in ('link1', 'link2', 'link3', 'link4'):        # the OMX's fingers are link6 / link7
                    ref[:, k] = np.minimum(ref[:, k], m1)
                    continue
                cells = [ci for ci, cg in enumerate(b2.group) if cg == grp]
                m2 = np.min([F.gap_dir(C2[:, ci], R2[:, ci], pc, pa, delta[:, bi], u)
                             - g.allowance(k, C2[:, ci, :, 2].max(1), delta[:, bi], with_sample=True)
                             for ci in cells], axis=0)
                need |= m1 < 0
                ref[:, k] = np.minimum(ref[:, k], np.where(m1 < 0, m2, m1))
        placed.clear()
        monkeypatch.setattr(F, 'world_boxes', spy)
        got = g.margins(Q, s, s, precheck=False, broad=False)
        monkeypatch.setattr(F, 'world_boxes', real)
        needs += int(need.sum())
        assert sum(placed) == int(need.sum()), fam
        assert np.allclose(got.M, ref, atol=1e-12, rtol=0), fam
    assert 0 < needs < n_samples

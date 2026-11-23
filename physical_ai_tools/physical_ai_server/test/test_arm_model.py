#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""``workflow.arm_model.CalibratedIK`` (spec 40 S6.1, WP-11): the profile's
solver seen in Roboter Studio's world W as ONE rigid model — the chain at the
encoder angles plus the arm check's offsets, with the five axis tilts, then the
lift and the mat tilt as one transform.

The truth is the solvers themselves raised by ``LIFT_M`` (nothing calibrated)
and ``autocal.chain`` transformed by hand (calibrated). The calibrated wrapper
is WP-11's: offsets ``(0, 0.010, −0.020, 0.015, 0)``, tilts 0.3°, slopes
``(0.004, −0.003)``. Numbers from spec 40 WP-11 (probes P-R10, P-R14).
"""

from __future__ import annotations

import math
import threading

import numpy as np
import pytest

from physical_ai_server.workflow import arm_geometry
from physical_ai_server.workflow import arm_model
from physical_ai_server.workflow import home_planner
from physical_ai_server.workflow import ik_solver
from physical_ai_server.workflow import scene_fixture as SF
from physical_ai_server.workflow.autocal import chain
from physical_ai_server.workflow.autocal import store
from physical_ai_server.workflow.autocal import texts_de
from physical_ai_server.workflow.ik_solver import IKSolver
from physical_ai_server.workflow.v1_ik import V1IKSolver
from physical_ai_server.workflow.workflow_manager import WorkflowContext

KINDS = ('omx', 'v1')
PROFILE = {'omx': 'omx_full', 'v1': 'v1_studio'}
D = (0.0, 0.010, -0.020, 0.015, 0.0)
_T = math.radians(0.3)
TILTS = (_T, 0.0, -_T, _T, _T, -_T)
SLOPES = (0.004, -0.003)
# The printed zone's radii and sector per arm (r m, ±az deg); the V1 prints ±85°.
ZONE = {'omx': (0.12, 0.25, 90.0), 'v1': (0.11, 0.34, 85.0)}
LIFT = np.array([0.0, 0.0, SF.LIFT_M])


def _base(kind):
    return IKSolver() if kind == 'omx' else V1IKSolver()


def _plain(kind):
    """Nothing calibrated, the simulator's shape: lift and window, the base fingertip."""
    return arm_model.for_simulator(_base(kind))


def _calibrated(kind, fingertip=True):
    """WP-11's calibrated wrapper with the contract's fingertip and window."""
    return arm_model.CalibratedIK(
        _base(kind), offsets=D, axis_tilts=TILTS, slopes=SLOPES,
        fingertip_m=SF.FINGERTIP_BELOW_EE_M[kind] if fingertip else None,
        joint1_window_deg=SF.ARMS[kind]['joint1_window_deg'])


def _z_table(kind):
    return SF.MAT_THICKNESS_M + SF.FINGERTIP_BELOW_EE_M[kind]


def _poses(kind, n, seed):
    rng = np.random.default_rng([seed, 0 if kind == 'omx' else 1])
    lims = _base(kind).joint_limits
    return [np.array([rng.uniform(lo, hi) for lo, hi in lims]) for _ in range(n)]


def _by_hand(kind):
    """``(points (…, 3) in A -> W, R_t, P0)``: the S6.1 transform written out."""
    rot = chain.tilt_rotation(*SLOPES)
    p0 = np.array([float(_base(kind).base_axis_x), 0.0, SF.MAT_THICKNESS_M])
    return (lambda pts: p0 + (np.asarray(pts, float) + LIFT - p0) @ rot.T), rot, p0


def _target(kind, az_deg, r, z):
    ax = float(_base(kind).base_axis_x)
    a = math.radians(az_deg)
    return (ax + r * math.cos(a), r * math.sin(a), z)


def _omx_urdf_frames(q):
    """The OMX's links 1..5 from ``ik_solver._tf`` (the URDF chain), solver frame."""
    origins = (ik_solver._J1_XYZ, ik_solver._J2_XYZ, ik_solver._J3_XYZ, ik_solver._J4_XYZ, ik_solver._J5_XYZ)
    out, T = [], np.eye(4)
    for xyz, axis, v in zip(origins, ('z', 'y', 'y', 'y', 'x'), q):
        T = T @ ik_solver._tf(xyz, axis, float(v))
        out.append(T.copy())
    return out


def _raised(frames):
    out = []
    for F in frames:
        G = np.array(F, float)
        G[:3, 3] += LIFT
        out.append(G)
    return out


# ══════════════════════════════════════════════════════════════════════════
# nothing calibrated: the base solver, raised by the plate
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('kind', KINDS)
def test_nothing_calibrated_is_the_base_solver_raised_by_the_lift(kind):
    base, w = _base(kind), _plain(kind)
    worst = 0.0
    for q in _poses(kind, 200, 0):
        Rb, tb = base.fk(q)
        Rw, tw = w.fk(q)
        worst = max(worst, float(np.abs(Rw - Rb).max()), float(np.abs(tw - (tb + LIFT)).max()))
        for n in (1, 5):
            ref = np.array(base.link_points(q, samples_per_link=n)) + LIFT
            got = np.array(w.link_points(q, samples_per_link=n))
            assert got.shape == ref.shape
            worst = max(worst, float(np.abs(got - ref).max()))
        if kind == 'v1':
            for F, G in zip(_raised(base.link_frames(q)), w.link_frames(q)):
                worst = max(worst, float(np.abs(np.asarray(G) - F).max()))
    assert worst < 1e-9, worst


@pytest.mark.parametrize('kind', KINDS)
def test_link_frames_and_the_box_model_only_on_the_v1(kind):
    for w in (_plain(kind), _calibrated(kind)):
        assert hasattr(w, 'link_frames') is (kind == 'v1')
        assert (arm_geometry.resolve_geometry(w) is not None) is (kind == 'v1')


@pytest.mark.parametrize('kind', KINDS)
def test_guard_frames_nothing_calibrated_are_the_base_frames_raised(kind):
    base = _base(kind)
    real = arm_model.for_robot(_base(kind), PROFILE[kind], None)
    for w in (_plain(kind), real):
        worst = worst_ee = 0.0
        for q in _poses(kind, 200, 1):
            got = w.guard_frames(q)
            assert np.asarray(got).shape == (5, 4, 4)
            ref = _raised(base.link_frames(q)[1:] if kind == 'v1' else _omx_urdf_frames(q))
            worst = max(worst, max(float(np.abs(np.asarray(g) - r).max()) for g, r in zip(got, ref)))
            if kind == 'omx':
                # link 5 carried to end_effector_link is IKSolver.fk, raised
                E = np.asarray(got[4]) @ ik_solver._tf(ik_solver._EE_XYZ, None, 0.0)
                R, t = base.fk(q)
                worst_ee = max(worst_ee, float(np.abs(E[:3, :3] - R).max()),
                               float(np.abs(E[:3, 3] - (t + LIFT)).max()))
        assert worst < 1e-9, worst
        assert worst_ee < 1e-12, worst_ee
    # the tower guard's batched form: (N, 5) -> (N, 5, 4, 4)
    Q = np.array(_poses(kind, 7, 2))
    batch = _plain(kind).guard_frames(Q)
    assert batch.shape == (7, 5, 4, 4)
    assert np.abs(batch[3] - _plain(kind).guard_frames(Q[3])).max() < 1e-12


@pytest.mark.parametrize('kind', KINDS)
def test_calibrated_frames_are_the_chain_at_q_plus_d_with_the_tilts_transformed(kind):
    w = _calibrated(kind)
    to_w, rot, _p0 = _by_hand(kind)
    d = np.array(D)
    tip = SF.FINGERTIP_BELOW_EE_M[kind] if kind == 'omx' else None
    worst = 0.0
    for q in _poses(kind, 100, 3):
        G = w.guard_frames(q)
        for g, F in zip(G, chain.frames(kind, q + d, TILTS)):
            worst = max(worst, float(np.abs(g[:3, :3] - rot @ F[:3, :3]).max()),
                        float(np.abs(g[:3, 3] - to_w(F[:3, 3])).max()))
        E = chain.ee(kind, q + d, TILTS)
        R, t = w.fk(q)
        worst = max(worst, float(np.abs(R - rot @ E[:3, :3]).max()), float(np.abs(t - to_w(E[:3, 3])).max()))
        pts = np.array(w.link_points(q, samples_per_link=5))
        ref = to_w(np.array(chain.link_points(kind, q + d, TILTS, 5, tip)))
        worst = max(worst, float(np.abs(pts - ref).max()))
        if kind == 'v1':
            for g, F in zip(w.link_frames(q), chain.link_frames(q + d, TILTS)):
                worst = max(worst, float(np.abs(g[:3, :3] - rot @ F[:3, :3]).max()),
                            float(np.abs(g[:3, 3] - to_w(F[:3, 3])).max()))
    assert worst < 1e-12, worst
    assert (arm_geometry.resolve_geometry(_calibrated('omx')) is None)


# ══════════════════════════════════════════════════════════════════════════
# solve: the fixed point, and the D33 / D42 refinement rule
# ══════════════════════════════════════════════════════════════════════════

def test_the_refinement_acceptance_is_half_a_millimetre():
    assert arm_model.IK_REFINE_ACCEPT_M == 0.0005


@pytest.mark.parametrize('kind', KINDS)
def test_solve_then_fk_returns_500_zone_targets_within_a_tenth_of_a_millimetre(kind):
    base, w = _base(kind), _calibrated(kind)
    to_w, rot, p0 = _by_hand(kind)
    d = np.array(D)
    r_in, r_out, az = ZONE[kind]
    rng = np.random.default_rng([11, 0 if kind == 'omx' else 1])
    zt = _z_table(kind)
    solved = 0
    first = []
    worst = 0.0
    for _ in range(500):
        tgt = _target(kind, rng.uniform(-az, az), rng.uniform(r_in, r_out), zt + rng.choice((0.015, 0.05, 0.075)))
        roll = float(rng.uniform(-1.2, 1.2))
        if base.solve(tgt, roll=roll) is None:
            continue
        q = w.solve(tgt, roll=roll)
        assert q is not None, tgt
        solved += 1
        worst = max(worst, float(np.linalg.norm(w.fk(q)[1] - np.array(tgt))))
        # what the first solve alone would leave: the base solve of the target in A, minus d
        aim_a = (np.array(tgt) - p0) @ rot + p0 - LIFT
        q1 = np.array(base.solve(tuple(aim_a), roll=roll)) - d
        miss1 = float(np.linalg.norm(w.fk(q1)[1] - np.array(tgt)))
        first.append(miss1)
    assert solved == 500, solved                 # every seeded zone target is the base's too
    assert worst <= 0.0001, worst
    assert float(np.median(first)) > 0.001, float(np.median(first))


def test_the_v1_sector_edge_answers_its_best_pose_with_its_miss():
    """D33, D42: at azimuth +89.5° the refined aim needs joint 1 beyond −90°, so
    the pass that would refine does not solve: the first pose is returned with
    its miss (P-R14: 3.0 mm), and the caller's warning names it; +88.0° refines."""
    w = _calibrated('v1')
    zt = _z_table('v1')
    edge = _target('v1', 89.5, 0.15, zt + 0.015)
    q = w.solve(edge, roll=0.0)
    assert q is not None
    miss = float(np.linalg.norm(w.fk(q)[1] - np.array(edge)))
    assert w.last_refine_miss_m == pytest.approx(miss, abs=1e-12)
    assert 0.0025 < miss < 0.0035, miss
    lo, hi = w.joint_limits[0]
    assert lo <= q[0] <= hi
    text = arm_model.refinement_warning(w, 'Ablage')
    assert text == '[WARNUNG] ' + texts_de.refinement_warning_de('Ablage', miss * 1000.0)
    assert '3 mm' in text

    inside = _target('v1', 88.0, 0.15, zt + 0.015)
    q = w.solve(inside, roll=0.0)
    assert q is not None
    assert float(np.linalg.norm(w.fk(q)[1] - np.array(inside))) <= 0.0001
    assert w.last_refine_miss_m <= 0.0001
    assert arm_model.refinement_warning(w, 'Ablage') is None


class _FirstAimOnly:
    """A V1 solver that answers the FIRST aim ``off`` metres off in x and no other."""

    def __init__(self, off):
        self._s = V1IKSolver()
        self._off = off
        self.calls = 0

    backend = 'closed-form-v1'
    tool_tip_below_ee_m = 0.0

    @property
    def joint_limits(self):
        return self._s.joint_limits

    @property
    def base_axis_x(self):
        return self._s.base_axis_x

    def num_joints(self):
        return 5

    def link_frames(self, q):
        return self._s.link_frames(q)

    def solve(self, target_xyz, **kw):
        self.calls += 1
        if self.calls > 1:
            return None
        return self._s.solve(np.asarray(target_xyz, float) + (self._off, 0.0, 0.0), **kw)


@pytest.mark.parametrize('off_m, warned', ((0.0003, False), (0.0007, True)))
def test_a_pass_that_does_not_solve_keeps_the_first_pose_never_none(off_m, warned):
    stub = _FirstAimOnly(off_m)
    w = arm_model.CalibratedIK(stub)
    tgt = _target('v1', 20.0, 0.22, _z_table('v1') + 0.015)
    q = w.solve(tgt, roll=0.0)
    assert q is not None
    assert stub.calls == 2                      # the second pass asked and got nothing
    first = V1IKSolver().solve(np.array(tgt) - LIFT + (off_m, 0.0, 0.0), roll=0.0)
    assert np.abs(np.array(q) - first).max() < 1e-12
    assert w.last_refine_miss_m == pytest.approx(off_m, abs=2e-6)
    assert (arm_model.refinement_warning(w, 'A') is not None) is warned


def test_the_miss_is_the_calling_threads_own():
    w = _calibrated('v1')
    zt = _z_table('v1')
    assert w.solve(_target('v1', 89.5, 0.15, zt + 0.015), roll=0.0) is not None
    mine = w.last_refine_miss_m
    seen = {}

    def other():
        w.solve(_target('v1', 10.0, 0.2, zt + 0.015), roll=0.0)
        seen['miss'] = w.last_refine_miss_m

    t = threading.Thread(target=other)
    t.start()
    t.join()
    assert seen['miss'] < 0.0001
    assert w.last_refine_miss_m == mine


def test_an_unsolvable_target_is_none_and_leaves_no_miss():
    w = _calibrated('omx')
    assert w.solve((0.6, 0.0, 0.05), roll=0.0) is None
    assert w.last_refine_miss_m is None
    assert arm_model.refinement_warning(w, 'A') is None
    assert w.solve((float('nan'), 0.0, 0.05)) is None


# ══════════════════════════════════════════════════════════════════════════
# the zone, the joint-1 window, the floor check
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('kind', KINDS)
def test_the_whole_zone_solves_at_grasp_hover_and_release_height(kind):
    w = _calibrated(kind)
    r_in, r_out, az = ZONE[kind]
    zt = _z_table(kind)
    for dz in (0.015, 0.075, 0.05):
        total = ok = 0
        worst = 0.0
        for r in np.linspace(r_in, r_out, 14):
            for a in np.linspace(-az, az, 25):
                for roll in (-1.2, 0.0, 1.2):
                    tgt = _target(kind, float(a), float(r), zt + dz)
                    total += 1
                    q = w.solve(tgt, roll=roll)
                    if q is not None:
                        ok += 1
                        worst = max(worst, float(np.linalg.norm(w.fk(q)[1] - np.array(tgt))))
        assert (ok, total) == (1050, 1050), (kind, dz, ok)
        assert worst <= 0.0001, (kind, dz, worst)


def test_the_omx_joint_1_window_refuses_120_degrees_and_solves_95():
    zt = _z_table('omx')
    for w in (_calibrated('omx'), _plain('omx')):
        assert IKSolver().solve(_target('omx', 120.0, 0.18, zt + 0.015)) is not None
        assert w.solve(_target('omx', 120.0, 0.18, zt + 0.015)) is None
        assert w.solve(_target('omx', -120.0, 0.18, zt + 0.015)) is None
        assert w.solve(_target('omx', 95.0, 0.18, zt + 0.015)) is not None
        lo, hi = w.joint_limits[0]
        assert (lo, hi) == (math.radians(-100.0), math.radians(100.0))
        assert w.joint_limits[1:] == IKSolver().joint_limits[1:]


def test_the_v1_limits_are_unchanged():
    assert _calibrated('v1').joint_limits == V1IKSolver().joint_limits
    assert _plain('v1').joint_limits == V1IKSolver().joint_limits


class _KeepsFortyMm:
    """The REJECTED design (correction 19): the fingertip constant overridden,
    the 40 mm tool points of ``link_points`` kept."""

    def __init__(self, real, padded):
        self._real = real
        self._padded = padded

    def __getattr__(self, name):
        return getattr(self._real, name)

    def link_points(self, joints, samples_per_link=5):
        return self._padded.link_points(joints, samples_per_link=samples_per_link)


def _floor_ctx(ik):
    ctx = WorkflowContext(publisher=lambda _p: None, ik=ik, z_table=_z_table('omx'))
    ctx.table_plane = None
    return ctx


def test_the_position_floor_check_judges_every_pose_on_the_calibrated_omx():
    w = _calibrated('omx')
    ctx = _floor_ctx(w)
    rng = np.random.default_rng(7)
    lims = w.joint_limits
    poses = [[rng.uniform(lo, hi) for lo, hi in lims] + [0.8] for _ in range(300)]
    values = [home_planner.pose_floor_clearance(ctx, q) for q in poses]
    assert all(v is not None and math.isfinite(v) for v in values)
    rejected = _KeepsFortyMm(w, _calibrated('omx', fingertip=False))
    assert rejected.tool_tip_below_ee_m == SF.FINGERTIP_BELOW_EE_M['omx']
    ctx = _floor_ctx(rejected)
    assert [home_planner.pose_floor_clearance(ctx, q) for q in poses] == [None] * 300


# ══════════════════════════════════════════════════════════════════════════
# the real robot's and the simulator's wrapper; the store
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('kind', KINDS)
def test_the_real_fingertip_is_the_contracts_the_simulators_the_base_solvers(kind, tmp_path):
    real = arm_model.for_robot(_base(kind), PROFILE[kind], str(tmp_path))
    sim = arm_model.for_simulator(_base(kind))
    assert real.tool_tip_below_ee_m == SF.FINGERTIP_BELOW_EE_M[kind]
    assert sim.tool_tip_below_ee_m == _base(kind).tool_tip_below_ee_m
    for w, tip in ((real, SF.FINGERTIP_BELOW_EE_M[kind]), (sim, _base(kind).tool_tip_below_ee_m)):
        for q in _poses(kind, 20, 4):
            R, t = w.fk(q)
            pts = w.link_points(q, samples_per_link=1)
            if kind == 'omx':
                axis = R[:, 0]
                assert np.abs(pts[-2] - (t + axis * tip * 0.5)).max() < 1e-12
                assert np.abs(pts[-1] - (t + axis * tip)).max() < 1e-12
            else:
                assert np.abs(pts[-1] - t).max() < 1e-12


def _arm_check(profile_id, **over):
    data = {
        'v': 1, 'fixture_id': SF.FIXTURE_ID, 'contract_hash': SF.contract_hash(), 'profile_id': profile_id,
        'camera_profile_id': 'tower_v1', 'image_size': [1920, 1080], 'created_utc': '2026-11-20T08:00:00Z',
        'kb': {'f': 1031.2, 'aspect': 1.001, 'cx': 961.0, 'cy': 538.2, 'k1': 0.05, 'k2': -0.04},
        'print_scale': 1.0012, 'joint_offsets_rad': list(D), 'axis_tilts_rad': list(TILTS), 'tilt': list(SLOPES),
        'mat_pose': [0.0011, -0.0004, 0.003], 'camera_pose_mat': {'rvec': [2.1, 0.3, -0.2], 't': [0.01, 0.2, 0.55]},
        'lens_w': [-0.1012, 0.0991, 0.5761], 'fixture_xy_mat': [[0.0, 0.0]] * 8, 'fixture_px': [[900.0, 400.0]] * 8,
        'mount': {'rvec': [0.0, 0.1, 0.0], 't': [0.0, -0.095, -0.11]}, 'droop_envelope_rad': [0.0, 0.02, 0.01, 0.004, 0.0],
        'tower_pose': [0.004, 0.0005, -0.0003], 'tower_fit_rms_m': 0.0002, 'target_removed': False,
        'droop_warning': None, 'notes': [],
        'quality': {'poses_used': 20, 'rms_px': 0.21, 'holdout_rms_px': 0.4, 'holdout_p95_mm': 0.4,
                    'field_tags': 14, 'sharpness': 80.0},
    }
    data.update(over)
    return data


@pytest.mark.parametrize('kind', KINDS)
def test_the_store_drives_the_parameters_and_a_new_version_reloads_them(kind, tmp_path):
    real = arm_model.for_robot(_base(kind), PROFILE[kind], str(tmp_path))
    plain = arm_model.for_robot(_base(kind), PROFILE[kind], None)
    calibrated = _calibrated(kind)
    q = _poses(kind, 1, 5)[0]

    def same(a, b):
        return float(np.abs(a.fk(q)[1] - b.fk(q)[1]).max()) < 1e-12 and \
            float(np.abs(np.asarray(a.guard_frames(q)) - np.asarray(b.guard_frames(q))).max()) < 1e-12

    # no arm_check.json: offsets, tilts and slopes are zero
    assert same(real, plain)
    # an arm check is written: the SAME object answers with its parameters at the next call
    store.Store(str(tmp_path)).write(_arm_check(PROFILE[kind]))
    assert same(real, calibrated)
    # a file of another profile (or another contract) reads as absent
    store.Store(str(tmp_path)).write(_arm_check('some_other_arm'))
    assert same(real, plain)
    # a malformed parameter list reads as absent, never half-applied
    store.Store(str(tmp_path)).write(_arm_check(PROFILE[kind], joint_offsets_rad=[0.01, 0.02]))
    assert same(real, plain)


# ══════════════════════════════════════════════════════════════════════════
# forwarding
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('kind', KINDS)
def test_what_is_not_overridden_is_the_base_solvers(kind):
    base, w = _base(kind), _calibrated(kind)
    q = list(_poses(kind, 1, 6)[0])
    assert w.backend == base.backend
    assert w.num_joints() == base.num_joints()
    assert w.base_axis_x == base.base_axis_x
    assert w.approach_axis_local == base.approach_axis_local
    assert w.base_yaw(0.12, 0.07) == base.base_yaw(0.12, 0.07)
    assert w.roll_from_joints(q) == base.roll_from_joints(q)
    assert hasattr(w, 'tool_tip_rise_m') is (kind == 'v1')
    if kind == 'v1':
        assert w.tool_tip_rise_m(0.5) == base.tool_tip_rise_m(0.5)
    # a private name would answer in the solver's frame: never forwarded
    assert not hasattr(w, '_fk_position')
    assert not hasattr(w, '_fk_matrix')


@pytest.mark.parametrize('kind', KINDS)
def test_in_workspace_and_solve_quat_answer_in_w(kind):
    w = _calibrated(kind)
    tgt = _target(kind, 30.0, 0.2, _z_table(kind) + 0.015)
    assert w.solve_quat(tgt, (0.0, 0.0, 0.0, 1.0)) == w.solve(tgt)
    assert w.in_workspace(tgt) is True
    # far outside the annulus in W
    assert w.in_workspace(_target(kind, 30.0, 0.6, 0.05)) is False


def test_a_short_joint_vector_is_none_like_the_solvers():
    w = _calibrated('omx')
    assert w.fk([0.0] * 4) is None
    assert w.link_points([0.0] * 4) is None
    assert w.guard_frames([0.0] * 4) is None
    assert _calibrated('v1').link_frames([0.0] * 4) is None

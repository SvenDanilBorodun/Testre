#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The clip-on target on the arm check's own route (verification finding X-TARGETSTART).

The tower guard models the ARM only (``fixture_guard.arm_boxes``: links 1-4,
the gripper housing, the blades); the calibration target clipped onto the
gripper for the arm check is not in it. So the target's clearance is a property
of the arm check's route itself, held here from the contract alone:

* THE PLATE is a box: ``TARGET['plate_m']`` square, the tag face at z = 0 of
  the target frame and 3 mm thick on the side away from the tags, placed by
  ``ARMS[kind]['target_mount_ee']`` on the nominal end-effector frame of the
  repo's own kinematics (``autocal.chain``, which the real arm model and its
  guard frames read; raised by ``LIFT_M`` into W).
* THE ROUTE is the one ``service.arm_check_program`` drives: straight joint
  lines HOME -> AC01 ... AC20 -> HOME (``test_arm_check_program.py`` holds that
  the program drives exactly these lines), sampled so that no point of the
  plate moves more than 3 mm between samples (a proven bound: per joint the
  chain's link lengths beyond it plus the plate's farthest corner from the end
  effector).
* THE FENCE: the plate keeps at least 30 mm from every ``TOWER_OBB_F`` box at
  its contract place (an exact box-to-box distance) and its lowest corner at
  least 30 mm above the mat's top, after half a sample's travel is taken off.

It holds from HOME and from every corner of the service's start box
(``ARM_CHECK_HOME_TOL_RAD`` on each arm joint), and it FAILS from the
observation pose, where every „Solange sichtbar" program leaves the arm: that
is why ``SceneCalibration.start_arm_check`` refuses anywhere but HOME.

Pure NumPy.
"""

from __future__ import annotations

import itertools
import math

import numpy as np
import pytest

from physical_ai_server import robot_profiles as RP
from physical_ai_server.workflow import arm_model
from physical_ai_server.workflow import scene_fixture as SF
from physical_ai_server.workflow.autocal import chain as CH
from physical_ai_server.workflow.autocal import service as SV

KINDS = ('v1', 'omx')
PROFILE = {'v1': 'v1_studio', 'omx': 'omx_full'}
CLEAR_M = 0.030                  # the fence: from every tower box and above the mat's top
SAMPLE_STEP_M = 0.003            # the most any point of the plate moves between two samples
PLATE_THICK_M = 0.003

_SIGNS = np.array(list(itertools.product((-1.0, 1.0), repeat=3)))
_EDGES = np.array([(i, j) for i in range(8) for j in range(i + 1, 8) if np.sum(_SIGNS[i] != _SIGNS[j]) == 1])


# ── the plate ────────────────────────────────────────────────────────────────
def _plate_half():
    h = SF.TARGET['plate_m'] / 2.0
    return np.array([h, h, PLATE_THICK_M / 2.0])


def _plate_corners_target():
    """The plate's 8 corners in the target frame: the tag face at z = 0, the body behind it."""
    return np.array([0.0, 0.0, -PLATE_THICK_M / 2.0]) + _SIGNS * _plate_half()


def _mount(kind):
    return np.asarray(SF.ARMS[kind]['target_mount_ee'], dtype=np.float64)


def _plate_pose_w(kind, Q):
    """``(N, 4, 4)``: the target frame in W at joint vectors ``Q`` (the solver's end effector, the mount, the lift)."""
    P = CH.ee_batch(kind, np.atleast_2d(np.asarray(Q, dtype=np.float64))) @ _mount(kind)
    P[:, 2, 3] += SF.LIFT_M
    return P


def _plate_corners_w(P):
    return np.einsum('nij,kj->nki', P[:, :3, :3], _plate_corners_target()) + P[:, None, :3, 3]


# ── the tower ────────────────────────────────────────────────────────────────
def _tower(kind):
    """``[(name, centre, axes (columns), half extents)]``: the contract's boxes at the contract place in W."""
    return [(name, np.asarray(c, dtype=np.float64), np.asarray(rows, dtype=np.float64), np.asarray(h, dtype=np.float64))
            for name, c, rows, h in SF.tower_obbs_w(CH.BASE_AXIS_X[kind])]


# ── exact box-to-box distance ────────────────────────────────────────────────
def _box_corners(c, A, h):
    return c + (_SIGNS * h) @ A.T


def _point_box(p, c, A, h):
    """Distance of points ``p (..., 3)`` to the solid box (0 inside)."""
    local = (p - c) @ A
    return np.linalg.norm(np.maximum(np.abs(local) - h, 0.0), axis=-1)


def _segment_segment(p1, q1, p2, q2):
    """Closest distance between segments ``p1 q1`` and ``p2 q2`` (broadcast arrays of points)."""
    d1, d2, r = q1 - p1, q2 - p2, p1 - p2
    a, e = np.sum(d1 * d1, -1), np.sum(d2 * d2, -1)
    b, c, f = np.sum(d1 * d2, -1), np.sum(d1 * r, -1), np.sum(d2 * r, -1)
    denom = a * e - b * b
    safe = denom > 1e-18
    s = np.where(safe, np.clip((b * f - c * e) / np.where(safe, denom, 1.0), 0.0, 1.0), 0.0)
    t = (b * s + f) / e
    s = np.where(t < 0.0, np.clip(-c / a, 0.0, 1.0), np.where(t > 1.0, np.clip((b - c) / a, 0.0, 1.0), s))
    t = np.clip(t, 0.0, 1.0)
    return np.linalg.norm(p1 + d1 * s[..., None] - (p2 + d2 * t[..., None]), axis=-1)


def _overlap(Xa, Aa, Xb, Ab):
    """Per sample: do the plate (corners ``Xa (N, 8, 3)``, axes ``Aa (N, 3, 3)``) and a box overlap — no
    separating axis among the 15."""
    n = Xa.shape[0]
    axes = [Aa[:, :, i] for i in range(3)] + [np.broadcast_to(Ab[:, j], (n, 3)) for j in range(3)]
    axes += [np.cross(Aa[:, :, i], np.broadcast_to(Ab[:, j], (n, 3))) for i in range(3) for j in range(3)]
    separated = np.zeros(n, dtype=bool)
    for ax in axes:
        norm = np.linalg.norm(ax, axis=-1)
        usable = norm > 1e-12
        u = ax / np.where(usable, norm, 1.0)[:, None]
        pa = np.einsum('nkj,nj->nk', Xa, u)
        pb = (Xb @ u.T).T
        gap = np.maximum(pa.min(1) - pb.max(1), pb.min(1) - pa.max(1))
        separated |= usable & (gap > 0.0)
    return ~separated


def _plate_box_distance(P, X, box):
    """Exact distance per sample between the plate (pose ``P``, corners ``X``) and one tower box: 0 when they
    overlap, else the least of corner-to-box both ways and edge-to-edge (the closest features of two convex
    polyhedra)."""
    _name, c, A, h = box
    B = _box_corners(c, A, h)
    to_box = _point_box(X, c, A, h).min(1)
    local = np.einsum('nkj,nji->nki', B[None] - P[:, None, :3, 3], P[:, :3, :3])
    local[..., 2] += PLATE_THICK_M / 2.0              # the plate's centre sits half its thickness behind the face
    to_plate = np.linalg.norm(np.maximum(np.abs(local) - _plate_half(), 0.0), axis=-1).min(1)
    edges = _segment_segment(X[:, _EDGES[:, 0]][:, :, None], X[:, _EDGES[:, 1]][:, :, None],
                             B[_EDGES[:, 0]][None, None], B[_EDGES[:, 1]][None, None])
    d = np.minimum(np.minimum(to_box, to_plate), edges.reshape(X.shape[0], -1).min(1))
    return np.where(_overlap(X, P[:, :3, :3], B, A), 0.0, d)


# ── the route and its samples ────────────────────────────────────────────────
def _reach_bound(kind):
    """``R_j``: a proven bound of any plate point's distance from joint j's axis — the link lengths from joint j
    to the end effector (constant along the chain) plus the plate's farthest corner from the end effector."""
    z = np.zeros((1, 5))
    origins = [f[:3, 3] for f in CH.frames_batch(kind, z)[0]] + [CH.ee_batch(kind, z)[0][:3, 3]]
    corners_ee = _plate_corners_target() @ _mount(kind)[:3, :3].T + _mount(kind)[:3, 3]
    rho = float(np.linalg.norm(corners_ee, axis=1).max())
    return np.array([sum(float(np.linalg.norm(origins[k + 1] - origins[k])) for k in range(j, 5)) + rho
                     for j in range(5)])


def _leg_samples(kind, a, b):
    a, b = np.asarray(a, dtype=np.float64)[:5], np.asarray(b, dtype=np.float64)[:5]
    n = max(1, int(math.ceil(float(np.sum(_reach_bound(kind) * np.abs(b - a))) / SAMPLE_STEP_M)))
    return a + np.linspace(0.0, 1.0, n + 1)[:, None] * (b - a)


def _program_route(kind):
    """The arm check's route as its program drives it: HOME, each „bewege zu" Position's joints, then the
    Grundstellung (``service.arm_check_program``'s own blocks and destinations)."""
    profile = RP.ROBOT_PROFILES[PROFILE[kind]]
    model = arm_model.for_robot(profile.build_ik(), profile.profile_id, None)
    payload = SV.arm_check_program(profile, model.fk)
    joints = {d['name']: d['joints'][:5] for d in payload['destinations']}
    home = [float(v) for v in profile.home_joints_rad]
    route, block = [home], payload['blocks']['blocks'][0]
    while block is not None:
        if block['type'] == 'edubotics_move_to':
            route.append(joints[block['inputs']['DESTINATION']['block']['fields']['NAME']])
        elif block['type'] == 'edubotics_home':
            route.append(home)
        block = (block.get('next') or {}).get('block')
    return [np.asarray(q, dtype=np.float64) for q in route]


def _judge(kind, a, b):
    """``(least tower distance, its part, least mat clearance, largest plate step)`` over a leg's samples."""
    Q = _leg_samples(kind, a, b)
    P = _plate_pose_w(kind, Q)
    X = _plate_corners_w(P)
    best = min(((float(_plate_box_distance(P, X, box).min()), box[0]) for box in _tower(kind)))
    mat = float(X[..., 2].min()) - SF.MAT_THICKNESS_M
    step = float(np.linalg.norm(np.diff(X, axis=0), axis=-1).max()) if len(Q) > 1 else 0.0
    return best[0], best[1], mat, step


def _fence_holds(kind, a, b):
    tower, _part, mat, step = _judge(kind, a, b)
    assert step <= SAMPLE_STEP_M
    return tower - SAMPLE_STEP_M / 2.0 >= CLEAR_M and mat - SAMPLE_STEP_M / 2.0 >= CLEAR_M


# ══════════════════════════════════════════════════════════════════════════
# the model
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('kind', KINDS)
def test_the_plate_rides_the_real_arm_model_s_end_effector(kind):
    """The fence's end effector IS the real arm model's (``arm_model.for_robot`` with nothing calibrated, the
    frames its tower guard reads, raised by LIFT_M) at HOME, the observation pose and every contract pose."""
    profile = RP.ROBOT_PROFILES[PROFILE[kind]]
    model = arm_model.for_robot(profile.build_ik(), profile.profile_id, None)
    poses = [profile.home_joints_rad, SF.ARMS[kind]['observe_joints_rad']] + list(SF.ARMS[kind]['arm_check_poses_rad'])
    for q in poses:
        R, t = model.fk(list(q))
        E = _plate_pose_w(kind, [q])[0] @ np.linalg.inv(_mount(kind))
        np.testing.assert_allclose(E[:3, :3], R, atol=1e-9)
        np.testing.assert_allclose(E[:3, 3], t, atol=1e-9)


@pytest.mark.parametrize('kind', KINDS)
def test_the_plate_box_is_the_contract_s(kind):
    """80 mm square, every printed tag corner on its face at z = 0, 3 mm thick behind it."""
    lo, hi = _plate_corners_target().min(0), _plate_corners_target().max(0)
    np.testing.assert_allclose(hi[:2] - lo[:2], [SF.TARGET['plate_m']] * 2)
    assert (lo[2], hi[2]) == (-PLATE_THICK_M, 0.0)
    for _tid, corners in SF.target_tag_corners(SF.ARMS[kind]):
        assert all(abs(x) <= hi[0] and abs(y) <= hi[1] for x, y in corners)


def test_the_box_distance_is_exact_on_known_cases():
    """The distance helper: overlapping boxes are 0, a face gap, an edge gap and a corner gap are exact."""
    box = ('unit', np.zeros(3), np.eye(3), np.array([0.01, 0.01, 0.01]))
    for offset, want in (((0.0, 0.0, 0.0), 0.0), ((0.0, 0.0, 0.0125), 0.0),
                         ((0.0, 0.0, 0.05), 0.05 - PLATE_THICK_M - 0.01),
                         ((0.07, 0.0, 0.0), 0.07 - 0.04 - 0.01),
                         ((0.07, 0.07, 0.0), math.hypot(0.07 - 0.05, 0.07 - 0.05))):
        P = np.eye(4)[None].copy()
        P[0, :3, 3] = offset
        X = np.array([0.0, 0.0, -PLATE_THICK_M / 2.0]) + _SIGNS * _plate_half() + np.asarray(offset)
        assert _plate_box_distance(P, X[None], box)[0] == pytest.approx(want, abs=1e-12), offset


# ══════════════════════════════════════════════════════════════════════════
# the fence
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('kind', KINDS)
def test_the_route_from_home_keeps_the_target_clear(kind):
    """Every leg of the program's route from HOME keeps the plate at least 30 mm from every tower box and above
    the mat. The route is HOME, the 20 pinned poses in order, HOME."""
    route = _program_route(kind)
    np.testing.assert_allclose(route[0], RP.ROBOT_PROFILES[PROFILE[kind]].home_joints_rad)
    np.testing.assert_allclose(route[1:-1], SF.ARMS[kind]['arm_check_poses_rad'])
    np.testing.assert_allclose(route[-1], route[0])
    for k, (a, b) in enumerate(zip(route[:-1], route[1:])):
        assert _fence_holds(kind, a, b), (kind, k, _judge(kind, a, b))


@pytest.mark.parametrize('kind', KINDS)
def test_every_start_inside_the_home_tolerance_keeps_the_target_clear(kind):
    """FAILS FIRST (X-TARGETSTART): the arm check starts anywhere inside the service's start box, so the first
    leg is judged from each of its 32 corners (every arm joint ``ARM_CHECK_HOME_TOL_RAD`` off HOME) to AC01."""
    home = np.asarray(RP.ROBOT_PROFILES[PROFILE[kind]].home_joints_rad, dtype=np.float64)
    first = _program_route(kind)[1]
    for signs in itertools.product((-1.0, 1.0), repeat=5):
        start = home + SV.ARM_CHECK_HOME_TOL_RAD * np.asarray(signs)
        assert _fence_holds(kind, start, first), (kind, signs, _judge(kind, start, first))


@pytest.mark.parametrize('kind', KINDS)
def test_the_fence_fails_from_the_observation_pose(kind):
    """Why the start must be HOME: from the observation pose the first leg drives the plate into the tower
    column (exact distance 0 to the contract box), while the arm itself is what the guard judges."""
    obs = SF.ARMS[kind]['observe_joints_rad']
    first = _program_route(kind)[1]
    tower, part, _mat, _step = _judge(kind, obs, first)
    assert not _fence_holds(kind, obs, first)
    assert (tower, part) == (0.0, 'column')
    home = RP.ROBOT_PROFILES[PROFILE[kind]].home_joints_rad
    assert _judge(kind, home, obs)[:2] == (0.0, 'column')    # HOME <-> OBS with the target on touches too

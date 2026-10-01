#!/usr/bin/env python3
"""Edu:1 classroom kit: grasp-accuracy budget of the calibration concepts.

Monte-Carlo comparison behind ``docs/edu1-classroom-kit.md``. It answers one
question per concept: "after this classroom setup + calibration, how far from
the cube does the closed claw tip land?" -- as a distribution over setups,
not a single best case.

Everything structural is READ FROM THE REPO, never retyped:

* the Edu:1 kinematics (``robot_profiles`` -> ``edu1_ik``: FK, strict-vertical
  IK, joint limits, the URDF chain constants);
* the scene-camera / tower / placement-zone model of
  ``tools/generate_edu1_mat.py`` (``camera_pose``, ``placement_zone``);
* the runtime projection convention of ``perception_blocks._tag_table_xy``: the
  ray through the tag centre is intersected at ``board_table_z +
  object_height_m`` (NOT at the touch-off plane);
* the extrinsic convention of ``calibration_manager``: ChArUco 7x5 / 30 mm,
  ``T_board_to_base = Rz(yaw) @ Rx(180)`` at ``(0.18, 0.075, z)``, SQPNP +
  ``solvePnPRefineLM``, 8-frame burst;
* the intrinsic model: ``CALIB_RATIONAL_MODEL``.

What is ASSUMED is a named sigma in ``SIGMA`` below. Change one and re-run;
the conclusions in the design doc were checked for robustness against
doubling each of them.

The physical arm is a separate object (``TrueArm``): the CAD chain plus
per-axis link tolerances and a gravity-sag bias at EXECUTION. Calibration
never sees those directly; they are the unmodelled residue any real arm has.

Usage (needs numpy, scipy, opencv-contrib-python, Pillow):
    python3 tools/edu1_kit_calibration_budget.py                 # 60 setups / concept
    python3 tools/edu1_kit_calibration_budget.py --trials 200 --seed 3
    python3 tools/edu1_kit_calibration_budget.py --only K2,K4

Runtime: ~1 min for the intrinsic pool, then ~1 min per concept at 60 trials.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import cv2
import numpy as np
from scipy.optimize import least_squares

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'tools'))
sys.path.insert(0, str(REPO / 'physical_ai_tools' / 'physical_ai_server'))

import generate_edu1_mat as gm  # noqa: E402
from physical_ai_server import robot_profiles  # noqa: E402
import physical_ai_server.workflow.edu1_ik as E1  # noqa: E402

D2R = math.pi / 180.0

# ── fixed design of the kit under test ──────────────────────────────────────
CAM_W, CAM_H, CAM_HFOV = 1920, 1080, 70.0     # recommended scene camera
LENS_X, LENS_Z, TILT = 0.58, 0.38, 45.0       # tower: 38 cm, 45 deg, 58 cm out
OBJ_H, GRASP_DEPTH = 0.030, 0.015             # catalog cube (object_catalog)
TAG_PX_MIN = 20.0                             # detector cliff (generate_edu1_mat)

# Grasp tolerance used for the "within tolerance" column. XY: the cube must
# stay inside the 48.5 mm blade width (edu1_ik docstring) with the 30 mm cube,
# i.e. +-9 mm, minus a 1 mm seating margin. Z: the closed-tip grasp height is
# 15 mm above the table; below -12 mm the tips touch the table, above +12 mm
# the claw closes high on the cube (E5 / the H >= 117 mm cliff in
# KNOWN-ISSUES is +16 mm).
TOL_XY_MM, TOL_Z_MM = 8.0, 12.0

# ── every assumed error source, one place ───────────────────────────────────
SIGMA = {
    # placement by eye on PRINTED mat marks (careful student)
    'robot_on_print_xy_mm': 1.0, 'robot_on_print_yaw_deg': 0.5,
    'board_on_print_xy_mm': 1.0, 'board_on_print_yaw_deg': 0.4,
    'mat_print_scale': 0.002,                    # large-format print / textile
    # mechanical registration (two dowels, reamed holes)
    'dock_xy_mm': 0.15, 'dock_yaw_deg': 0.05,
    'board_thickness_mm': 0.2,                   # defined plate, known thickness
    # tower (only matters through coverage; every concept MEASURES the camera)
    'tower_xyz_mm': 10.0, 'tower_rot_deg': 1.0,
    # joint-zero error after assembly, per joint (deg)
    'zero_jig_deg': 0.15,     # whole arm provisioned on a vendor jig
    'zero_keyed_deg': 0.40,   # pre-provisioned servo + indexed horn + student assembly
    'zero_mark_deg': 2.0,     # pre-provisioned servo + painted mark aligned by eye
    # physical arm vs CAD
    'link_xyz_mm': 0.3,       # per axis per joint origin (printed parts)
    'sag_deg': 0.3,           # j2 sag at full reach under gravity (execution only)
    'exec_noise_deg': 0.05,   # servo settle / deadband per joint
    'read_noise_deg': 0.03,   # Present_Position read (1 tick = 0.088 deg)
    # vision
    'charuco_px': 0.15 / math.sqrt(8),   # 8-frame burst average
    'tag_corner_px': 0.15,               # 40 mm wrist tag, 3-frame average
    'object_center_px': 0.125,
    'object_height_mm': 0.3, 'table_flat_mm': 0.5,
    'wrist_tag_mount_mm': 0.3, 'wrist_tag_mount_deg': 0.3,
    'touch_tap_mm': 1.5,
    # Sockelplatte: robot dowelled to the plate at the factory; UV print on
    # aluminium composite (per-marker placement + global scale)
    'plate_dock_xy_mm': 0.1, 'plate_dock_yaw_deg': 0.03,
    'plate_print_mm': 0.05, 'plate_print_scale': 0.0005,
}


# ── small geometry helpers ──────────────────────────────────────────────────
def T_from(R, t):
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = t
    return T


def rotz(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def small_rot(v):
    R, _ = cv2.Rodrigues(np.asarray(v, dtype=np.float64))
    return R


def cam_nominal():
    C, xc, yc, zc = gm.camera_pose(LENS_X, LENS_Z, TILT)
    return T_from(np.column_stack([xc, yc, zc]), C)          # cam -> base


def project(P, T_c2b, K, d):
    T_b2c = np.linalg.inv(T_c2b)
    rvec, _ = cv2.Rodrigues(T_b2c[:3, :3])
    img, _ = cv2.projectPoints(np.asarray(P, np.float64).reshape(-1, 3), rvec,
                               T_b2c[:3, 3], K, d)
    return img.reshape(-1, 2)


def backproject(px, K, d, T_c2b, z_plane):
    """The runtime's projection.project_pixel_to_table, vectorised."""
    n = cv2.undistortPoints(np.asarray(px, np.float64).reshape(-1, 1, 2), K, d).reshape(-1, 2)
    rays = np.column_stack([n, np.ones(len(n))]) @ T_c2b[:3, :3].T
    o = T_c2b[:3, 3]
    s = (z_plane - o[2]) / rays[:, 2]
    return o + s[:, None] * rays


def pnp_cam_to_obj(obj, img, K, d):
    """calibration_manager._board_pose_from_frame: SQPNP + LM refine.
    Returns cam -> object-frame."""
    ok, rvec, tvec = cv2.solvePnP(obj.astype(np.float64), img.astype(np.float64),
                                  K, d, flags=cv2.SOLVEPNP_SQPNP)
    rvec, tvec = cv2.solvePnPRefineLM(obj.astype(np.float64), img.astype(np.float64),
                                      K, d, rvec, tvec)
    R, _ = cv2.Rodrigues(rvec)
    return np.linalg.inv(T_from(R, tvec.ravel()))


# ── believed arm (the solver) and physical arm ──────────────────────────────
PROFILE = robot_profiles.resolve('edu1_studio')
IK = PROFILE.build_ik()


def ee_T(q):
    """Believed EE-origin frame (link5) in WORLD — FK minus the tool."""
    R, t = IK.fk(list(q))
    R = np.asarray(R)
    return T_from(R, np.asarray(t) - R @ np.array([0.0, 0.0, E1._L_TOOL]))


class TrueArm:
    """The physical arm. Present_Position is read on the output shaft, so a
    READ pose maps through this chain exactly; a COMMANDED pose additionally
    carries the gravity sag (``sag_bias``) and settle noise."""

    def __init__(self, rng, zero_sigma_deg):
        s = SIGMA['link_xyz_mm'] / 1000.0
        self.fixed = []
        for T in E1._FIXED:
            T = T.copy()
            T[:3, 3] = T[:3, 3] + rng.normal(0, s, 3)
            self.fixed.append(T)
        self.l_tool = E1._L_TOOL + rng.normal(0, s)
        self.sag = abs(rng.normal(SIGMA['sag_deg'], SIGMA['sag_deg'] / 3)) * D2R
        self.dq = rng.normal(0, zero_sigma_deg * D2R, 5)    # actual = read + dq

    def T(self, q_act):
        t = np.eye(4)
        for i in range(5):
            t = t @ self.fixed[i] @ E1._tf((0, 0, 0), E1._axis_rot(E1._AXES[i], q_act[i]))
        W = np.eye(4)
        W[:3, :3] = E1._RZ_PI
        return W @ t

    def tcp(self, q_act):
        T = self.T(q_act)
        return T[:3, 3] + T[:3, :3] @ np.array([0.0, 0.0, self.l_tool])

    def sag_bias(self, q_act):
        T = self.T(q_act)
        reach = math.hypot(T[0, 3], T[1, 3]) / 0.35
        return np.array([0.0, 1.0, -0.6, 0.3, 0.0]) * self.sag * reach


# ── calibration targets ─────────────────────────────────────────────────────
SQ = 0.030
BOARD_PTS = np.array([[i * SQ, j * SQ, 0.0] for j in range(1, 5) for i in range(1, 7)])


def T_board_nominal():
    return T_from(rotz(0.0) @ np.diag([1.0, -1.0, -1.0]), [0.18, 0.075, 0.0])


# The Sockelplatte: the robot is factory-bolted (2 dowels) onto a rigid plate
# whose top face is printed with two "wings" of 30 mm tag36h11 markers, left
# and right of the base and BEHIND the joint-1 axis (x < 0), i.e. outside the
# +-90 deg reach fan: 3 x 2 tags per side. Measured (docs/edu1-classroom-kit.md
# 3.1): all 12 are visible from the tower with the arm at HOME, and they cover
# 0.5 % of the grasp zone (a front strip covered 9.5 % at equal accuracy).
# The plate top IS z = 0 (robot base bottom). Corners are the PnP points;
# nothing is placed by a student.
PLATE_TAG = 0.030


def _plate_points():
    centres = [(x, s * y) for s in (-1, 1) for x in (-0.095, -0.055, -0.015) for y in (0.115, 0.150)]
    pts = []
    h = PLATE_TAG / 2
    for cx, cy in centres:
        pts += [(cx - h, cy - h, 0.0), (cx + h, cy - h, 0.0), (cx + h, cy + h, 0.0), (cx - h, cy + h, 0.0)]
    return np.array(pts)


PLATE_PTS = _plate_points()


# 40 mm tag36h11 on the claw-servo housing (link5), facing up-and-out at 45 deg,
# centred 40 mm off the tool axis and 10 mm toward the tip from the EE origin.
TAG = 0.040
TAG_C = np.array([0.0, -0.040, 0.010])
_n = np.array([0.0, -1.0, -1.0])
TAG_N = _n / np.linalg.norm(_n)
_u = np.array([1.0, 0.0, 0.0])
_v = np.cross(TAG_N, _u)
TAG_PTS = np.array([TAG_C + a * TAG / 2 * _u + b * TAG / 2 * _v
                    for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1))])


def tag_world_believed(q):
    T = ee_T(q)
    return (T[:3, :3] @ TAG_PTS.T).T + T[:3, 3]


def calibration_poses(T_c2b, K, d, n_poses=12):
    """``n_poses`` (12, the recommended „Arm einmessen" program) spread evenly
    over a candidate set of three radii x four bearings x two heights, each vertical AND
    with the wrist pitched +-0.35 rad, roll chosen to face the tag at the
    camera; kept only if the whole arm stays >= 30 mm above the table and the
    tag is >= 40 px and inside the image."""
    C = T_c2b[:3, 3]
    out = []
    for r in (0.12, 0.20, 0.28):
        for az in (-60, -25, 25, 60):
            for z in (0.06, 0.16):
                q0 = IK.solve((r * math.cos(az * D2R), r * math.sin(az * D2R), z), roll=0.0)
                if q0 is None:
                    continue
                for dq4 in (0.0, 0.35, -0.35):
                    q = list(q0[:5])
                    q[3] += dq4
                    lo, hi = IK.joint_limits[3]
                    if not lo <= q[3] <= hi:
                        continue
                    best = None
                    for q5 in np.linspace(-1.5, 1.5, 61):
                        q[4] = q5
                        T = ee_T(q)
                        c = T[:3, :3] @ TAG_C + T[:3, 3]
                        n = T[:3, :3] @ TAG_N
                        cosang = n @ (C - c) / np.linalg.norm(C - c)
                        if best is None or cosang > best[0]:
                            best = (cosang, q5)
                    q[4] = best[1]
                    if best[0] < 0.6:
                        continue
                    pts = IK.link_points(q)      # [0:11] is the fixed base column
                    if pts is None or min(float(np.asarray(p)[2]) for p in pts[11:]) < 0.04:
                        continue
                    px = project(tag_world_believed(q), T_c2b, K, d)
                    edge = np.linalg.norm(px[1] - px[0])
                    if edge < 40 or (px < 0).any() or (px[:, 0] > CAM_W).any() or (px[:, 1] > CAM_H).any():
                        continue
                    out.append(q)
    idx = np.unique(np.round(np.linspace(0, len(out) - 1, min(n_poses, len(out)))).astype(int))
    return [out[i] for i in idx]


# ── intrinsic calibration quality (synthetic ChArUco sessions) ──────────────
FX_NOM = (CAM_W / 2) / math.tan(math.radians(CAM_HFOV) / 2)


def true_camera(rng):
    """One physical camera: unit-to-unit spread of a cheap M12 module."""
    f = FX_NOM * (1 + rng.normal(0, 0.03))
    K = np.array([[f, 0, CAM_W / 2 + rng.normal(0, 25)],
                  [0, f, CAM_H / 2 + rng.normal(0, 25)], [0, 0, 1.0]])
    d = np.array([-0.30 + rng.normal(0, 0.02), 0.11 + rng.normal(0, 0.01), 0, 0, -0.02])
    return K, d


def _inner_corners(nx, ny, sq):
    xs, ys = np.meshgrid(np.arange(1, nx) * sq, np.arange(1, ny) * sq)
    return np.column_stack([xs.ravel(), ys.ravel(), np.zeros(xs.size)])


def calibrate_intrinsics(rng, regime, K, d):
    """'student': today's step — 20 hand-held views of the 7x5/30 mm board,
    clustered near the image centre. 'kit': once per camera at kit assembly —
    40 views of a 12x9/40 mm board covering the corners."""
    if regime == 'student':
        obj, n, dist, tilt, full, noise = _inner_corners(7, 5, 0.030), 20, (0.30, 0.70), 40, False, 0.20
    else:
        obj, n, dist, tilt, full, noise = _inner_corners(12, 9, 0.040), 40, (0.45, 0.95), 45, True, 0.10
    objs, imgs = [], []
    while len(objs) < n:
        z = rng.uniform(*dist)
        if full:
            u, v = rng.uniform(-0.85, 0.85), rng.uniform(-0.8, 0.8)
        else:
            u, v = (float(np.clip(rng.normal(0, 0.35), -0.9, 0.9)) for _ in range(2))
        R = small_rot([rng.uniform(-tilt, tilt) * D2R, rng.uniform(-tilt, tilt) * D2R,
                       rng.uniform(-math.pi, math.pi)])
        t = np.array([u * (CAM_W / 2) / K[0, 0] * z, v * (CAM_H / 2) / K[1, 1] * z, z]) - R @ obj.mean(0)
        cam = (R @ obj.T).T + t
        if (cam[:, 2] <= 0.05).any():
            continue
        img = project(obj, np.linalg.inv(T_from(R, t)), K, d)
        inside = (img[:, 0] > 5) & (img[:, 0] < CAM_W - 5) & (img[:, 1] > 5) & (img[:, 1] < CAM_H - 5)
        if inside.sum() < 0.7 * len(obj):
            continue
        objs.append(obj[inside].astype(np.float32))
        imgs.append((img[inside] + rng.normal(0, noise, (inside.sum(), 2))).astype(np.float32))
    _, Ke, de, _, _ = cv2.calibrateCamera(objs, imgs, (CAM_W, CAM_H), None, None,
                                          flags=cv2.CALIB_RATIONAL_MODEL)
    return Ke, de.ravel()


# ── one classroom setup ─────────────────────────────────────────────────────
def planar_err(rng, sxy_mm, syaw_deg):
    return T_from(rotz(rng.normal(0, syaw_deg * D2R)),
                  [rng.normal(0, sxy_mm / 1000), rng.normal(0, sxy_mm / 1000), 0.0])


def solve_arm_tag(rng, arm, poses, T_c2b_true, K, d, Ke, de, T_c2b_fixed, prior_deg):
    """Show the wrist tag in every pose; return (cam->base, joint corrections).
    With ``T_c2b_fixed`` (a docked board already gave the camera) only the
    joint corrections are estimated and joint1 becomes observable too; without
    it, joint1 is a gauge freedom of the camera yaw and stays 0. MAP estimate:
    pixel residuals plus a Gaussian prior on each correction."""
    mount = T_from(small_rot(rng.normal(0, SIGMA['wrist_tag_mount_deg'] * D2R, 3)),
                   rng.normal(0, SIGMA['wrist_tag_mount_mm'] / 1000, 3))
    tag_true = (mount[:3, :3] @ TAG_PTS.T).T + mount[:3, 3]
    reads, img = [], []
    for q in poses:
        q_read = np.asarray(q) + rng.normal(0, SIGMA['read_noise_deg'] * D2R, 5)
        Ta = arm.T(q_read + arm.dq)
        P = (Ta[:3, :3] @ tag_true.T).T + Ta[:3, 3]
        img.append(project(P, T_c2b_true, K, d) + rng.normal(0, SIGMA['tag_corner_px'], (4, 2)))
        reads.append(q_read)
    img = np.vstack(img)
    w_prior = 1.0 / (prior_deg * D2R)
    w_px = 1.0 / SIGMA['tag_corner_px']

    if T_c2b_fixed is not None:
        T_b2c = np.linalg.inv(T_c2b_fixed)
        rvec, _ = cv2.Rodrigues(T_b2c[:3, :3])

        def resid(x):
            Pm = np.vstack([tag_world_believed(q + x) for q in reads])
            pr, _ = cv2.projectPoints(Pm, rvec, T_b2c[:3, 3], Ke, de)
            return np.concatenate([(pr.reshape(-1, 2) - img).ravel() * w_px, x * w_prior])
        sol = least_squares(resid, np.zeros(5), x_scale='jac')
        return T_c2b_fixed, sol.x

    obj = np.vstack([tag_world_believed(q) for q in reads])
    T0 = np.linalg.inv(pnp_cam_to_obj(obj, img, Ke, de))      # base -> cam
    r0, _ = cv2.Rodrigues(T0[:3, :3])

    def resid(x):
        dqe = np.concatenate([[0.0], x[6:10]])
        Pm = np.vstack([tag_world_believed(q + dqe) for q in reads])
        pr, _ = cv2.projectPoints(Pm, x[:3], x[3:6], Ke, de)
        return np.concatenate([(pr.reshape(-1, 2) - img).ravel() * w_px, x[6:10] * w_prior])
    sol = least_squares(resid, np.concatenate([r0.ravel(), T0[:3, 3], np.zeros(4)]), x_scale='jac')
    R, _ = cv2.Rodrigues(sol.x[:3])
    return np.linalg.inv(T_from(R, sol.x[3:6])), np.concatenate([[0.0], sol.x[6:10]])


def run_setup(rng, c, intr_pool, test_pts, poses):
    K, d, Ke, de = intr_pool[c['intr']][rng.integers(len(intr_pool[c['intr']]))]
    arm = TrueArm(rng, SIGMA[f"zero_{c['arm']}_deg"])
    # base frame = the robot. Anything positioned by the MAT moves by inv(robot-on-mat).
    T_robot = planar_err(rng, SIGMA['robot_on_print_xy_mm'], SIGMA['robot_on_print_yaw_deg']) \
        if c['extr'] == 'board_on_mat' else np.eye(4)
    inv_r = np.linalg.inv(T_robot)
    T_tower = T_from(small_rot(rng.normal(0, SIGMA['tower_rot_deg'] * D2R, 3)),
                     rng.normal(0, SIGMA['tower_xyz_mm'] / 1000, 3))
    T_c2b_true = inv_r @ cam_nominal() @ T_tower
    dq_est = np.zeros(5)
    board_bias = c.get('board_bias_mm', 0.0) / 1000
    if c['extr'] in ('board_on_mat', 'board_dock', 'board_dock+tag'):
        if c['extr'] == 'board_on_mat':
            T_bn = T_board_nominal()
            T_bn[:2, 3] *= 1 + rng.normal(0, SIGMA['mat_print_scale'])
            T_bt = inv_r @ T_bn @ planar_err(rng, SIGMA['board_on_print_xy_mm'], SIGMA['board_on_print_yaw_deg'])
        else:
            T_bt = T_board_nominal() @ planar_err(rng, SIGMA['dock_xy_mm'], SIGMA['dock_yaw_deg'])
        T_bt = T_bt.copy()
        T_bt[2, 3] = board_bias + rng.normal(0, SIGMA['board_thickness_mm'] / 1000)
        P = (T_bt[:3, :3] @ BOARD_PTS.T).T + T_bt[:3, 3]
        img = project(P, T_c2b_true, K, d) + rng.normal(0, SIGMA['charuco_px'], (len(P), 2))
        T_c2b_est = T_board_nominal() @ pnp_cam_to_obj(BOARD_PTS, img, Ke, de)
        if c['extr'] == 'board_dock+tag':
            T_c2b_est, dq_est = solve_arm_tag(rng, arm, poses, T_c2b_true, K, d, Ke, de,
                                              T_c2b_est, SIGMA[f"zero_{c['arm']}_deg"])
    elif c['extr'] in ('plate', 'plate+tag'):
        T_pl = planar_err(rng, SIGMA['plate_dock_xy_mm'], SIGMA['plate_dock_yaw_deg'])
        P = PLATE_PTS * (1 + rng.normal(0, SIGMA['plate_print_scale']))
        P = P + np.column_stack([rng.normal(0, SIGMA['plate_print_mm'] / 1000, (len(P), 2)), np.zeros(len(P))])
        P = (T_pl[:3, :3] @ P.T).T + T_pl[:3, 3]
        img = project(P, T_c2b_true, K, d) + rng.normal(0, SIGMA['charuco_px'], (len(P), 2))
        T_c2b_est = np.linalg.inv(np.linalg.inv(pnp_cam_to_obj(PLATE_PTS, img, Ke, de)))
        if c['extr'] == 'plate+tag':
            T_c2b_est, dq_est = solve_arm_tag(rng, arm, poses, T_c2b_true, K, d, Ke, de,
                                              T_c2b_est, SIGMA[f"zero_{c['arm']}_deg"])
    else:
        T_c2b_est, dq_est = solve_arm_tag(rng, arm, poses, T_c2b_true, K, d, Ke, de, None,
                                          SIGMA[f"zero_{c['arm']}_deg"])

    if c['z'] == 'touchoff':          # today: 4 hand-guided taps, limp arm, plane fit
        taps = np.array([[0.15, 0.12], [0.15, -0.12], [0.28, 0.08], [0.28, -0.08]]) + rng.normal(0, 0.02, (4, 2))
        zs = []
        for x, y in taps:
            q = np.array(IK.solve((x, y, 0.0), roll=0.0)[:5]) - arm.dq
            for _ in range(3):              # limp arm: the TRUE tip rests on the table
                z = arm.tcp(q + arm.dq)[2]
                e = np.array([0, 1e-4, 0, 0, 0])
                q[1] -= z / ((arm.tcp(q + arm.dq + e)[2] - z) / 1e-4)
            zs.append(IK.fk(list(q))[1][2] + rng.normal(0, SIGMA['touch_tap_mm'] / 1000))
        plane = np.linalg.lstsq(np.column_stack([taps, np.ones(4)]), np.array(zs), rcond=None)[0]

        def z_table(x, y):
            return plane[0] * x + plane[1] * y + plane[2]
    else:                             # robot and objects stand on the same surface
        def z_table(x, y):
            return 0.0

    out = []
    for x, y in test_pts:
        z_floor = rng.normal(0, SIGMA['table_flat_mm'] / 1000)
        p_true = np.array([x, y, z_floor + OBJ_H + rng.normal(0, SIGMA['object_height_mm'] / 1000)])
        px = project(p_true[None], T_c2b_true, K, d) + rng.normal(0, SIGMA['object_center_px'], (1, 2))
        p_est = backproject(px, Ke, de, T_c2b_est, OBJ_H)[0]
        q = IK.solve((p_est[0], p_est[1], z_table(p_est[0], p_est[1]) + OBJ_H - GRASP_DEPTH), roll=0.0)
        if q is None:
            continue
        q_cmd = np.asarray(q[:5]) - dq_est
        q_act = q_cmd + arm.dq
        q_act = q_act + arm.sag_bias(q_act) + rng.normal(0, SIGMA['exec_noise_deg'] * D2R, 5)
        tip = arm.tcp(q_act)
        out.append((math.hypot(tip[0] - p_true[0], tip[1] - p_true[1]),
                    tip[2] - (z_floor + OBJ_H - GRASP_DEPTH)))
    return np.array(out)


CONCEPTS = {
    'K0': ('Heute (mit neuer Kamera): Schüler-Intrinsik 20 Bilder, Tafel auf 5-mm-Schaumplatte '
           'an Matten-Markierung, Roboter auf Matten-Umriss, 4× Tisch-Tippen',
           dict(intr='student', arm='jig', extr='board_on_mat', board_bias_mm=5.0, z='touchoff')),
    'K1a': ('Vorschlag wörtlich: Werks-Intrinsik, Tafel + Roboter auf Matten-Markierung, '
            'Horn-Strich von Auge ausgerichtet',
            dict(intr='kit', arm='mark', extr='board_on_mat', z='zero')),
    'K1b': ('Wie K1a, aber Index-Horn (Passstift statt Strich)',
            dict(intr='kit', arm='keyed', extr='board_on_mat', z='zero')),
    'K2': ('Tafel mit 2 Passstiften am Roboter-Sockel angedockt, Index-Horn',
           dict(intr='kit', arm='keyed', extr='board_dock', z='zero')),
    'K2j': ('Wie K2, aber Arm komplett im Werks-Jig provisioniert',
            dict(intr='kit', arm='jig', extr='board_dock', z='zero')),
    'K3': ('Ohne Tafel: Arm zeigt Handgelenk-Tag, Kamerapose + Gelenk-Nullpunkte geschätzt',
           dict(intr='kit', arm='keyed', extr='arm_tag', z='zero')),
    'K4': ('Angedockte Tafel (Kamera) + Arm zeigt Handgelenk-Tag (Gelenk-Nullpunkte)',
           dict(intr='kit', arm='keyed', extr='board_dock+tag', z='zero')),
    'K4m': ('K4, aber grob montiert (Horn-Strich von Auge)',
            dict(intr='kit', arm='mark', extr='board_dock+tag', z='zero')),
    'K5p': ('Sockelplatte mit Marker-Flügeln (Kamera), Index-Horn, keine Armbewegung',
            dict(intr='kit', arm='keyed', extr='plate', z='zero')),
    'K5j': ('Sockelplatte, Arm komplett im Werks-Jig provisioniert, keine Armbewegung',
            dict(intr='kit', arm='jig', extr='plate', z='zero')),
    'K5': ('EMPFEHLUNG: Sockelplatte (Kamera) + einmaliges Arm-Einmessen mit Handgelenk-Tag',
           dict(intr='kit', arm='keyed', extr='plate+tag', z='zero')),
    'K5m': ('K5, aber grob montiert (Horn-Strich von Auge)',
            dict(intr='kit', arm='mark', extr='plate+tag', z='zero')),
}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--trials', type=int, default=60, help='classroom setups per concept')
    ap.add_argument('--pool', type=int, default=60, help='intrinsic calibrations per regime')
    ap.add_argument('--seed', type=int, default=11)
    ap.add_argument('--only', default='', help='comma-separated concept keys')
    ap.add_argument('--poses', type=int, default=12, help='„Arm einmessen“ poses')
    args = ap.parse_args()
    rng = np.random.default_rng(args.seed)

    geom = gm._load_geometry()
    G, mask, _tag_px, _ring, _step = gm.placement_zone(geom, LENS_X, LENS_Z, TILT, CAM_HFOV,
                                                       CAM_W, CAM_H, TAG_PX_MIN, step=0.004)
    zone = G[mask]
    test = []
    for x in np.arange(-0.34, 0.35, 0.03):
        for y in np.arange(-0.34, 0.35, 0.03):
            dd = np.hypot(zone[:, 0] - x, zone[:, 1] - y)
            if dd.size and dd.min() < 0.003:
                test.append(zone[dd.argmin()])
    test = np.array(test)

    pool = {}
    for regime in ('student', 'kit'):
        pool[regime] = []
        for _ in range(args.pool):
            K, d = true_camera(rng)
            Ke, de = calibrate_intrinsics(rng, regime, K, d)
            pool[regime].append((K, d, Ke, de))
    K0, d0, _, _ = pool['kit'][0]
    poses = calibration_poses(cam_nominal(), K0, d0, args.poses)
    print(f'[INFO] {len(test)} Prüfpunkte in der Greifzone, {len(poses)} Kalibrier-Posen, '
          f'{args.trials} Aufbauten je Konzept, Kamera {CAM_W}x{CAM_H} {CAM_HFOV:.0f}°, '
          f'Turm {LENS_Z*100:.0f} cm / {TILT:.0f}° / {LENS_X*100:.0f} cm vor der Achse')
    keys = [k for k in CONCEPTS if not args.only or k in args.only.split(',')]
    print(f'{"":5s} {"XY p50":>7s} {"XY p95":>7s} {"XY p99":>7s} {"|Z| p95":>8s} {"in Toleranz":>12s}')
    for key in keys:
        label, c = CONCEPTS[key]
        r = np.vstack([run_setup(rng, c, pool, test, poses) for _ in range(args.trials)])
        exy, ez = r[:, 0] * 1000, np.abs(r[:, 1]) * 1000
        ok = 100 * np.mean((exy <= TOL_XY_MM) & (ez <= TOL_Z_MM))
        print(f'{key:5s} {np.percentile(exy, 50):6.1f}  {np.percentile(exy, 95):6.1f}  '
              f'{np.percentile(exy, 99):6.1f}  {np.percentile(ez, 95):7.1f}  {ok:10.1f} %   {label}')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The fixture contract of the scene-camera tower (spec 40 §3): ONE module,
literals plus pure rules, the single source every other layer reads.

WHAT IT IS. The block between the two marker lines below is the spec's
contract block BYTE FOR BYTE (its sha256 is pinned by
``test/test_scene_fixture.py``; revision 6 of the spec restored revision 4's
block, decision D77; decision D84 then moved the V1's arm-check pose AC12).
It places the tower, the lens, the mats, the fixture tags and the clip-on
target in the fixture frame F (origin on the joint-1 axis at the table
surface, x forward, y left, z up), and carries the tower guard's allowance
constants and both arms' tables. Nothing in it is computed;
to change a value, change the spec, then this block, then re-render the React
copy (``tools/export_scene_fixture.py``).

THE RULES below the block are pure functions of the literals: no I/O, no
clock, no environment, standard library only (``object_catalog`` reads the
reserved tag ids from here, and the setup suite's lockstep test imports it
without numpy).

* ``tower_obbs_w(base_axis_x, pose)`` places the PHYSICAL tower solids in
  Roboter Studio's world W (x_W = x_F + base_axis_x), turned and shifted by the
  tower pose the arm check measured from fixture tags 480/481; nothing is
  grown here — ``fixture_guard`` applies the allowances.
* ``tower_pose_fit(measured, base_axis_x)`` is that measurement: the
  least-squares 2D rigid map of the nominal fixture-tag corners onto the
  measured ones (rotation and translation only, never a mirror or a scale) and
  its RMS. In 2D the SVD solution of the Procrustes problem has a closed form,
  the angle ``atan2(Σ a×b, Σ a·b)`` of the centred point sets, which this uses.
* ``zone_az_hi`` / ``in_zone`` are the ONE predicate of "inside the printed
  work zone" (D66: the corner next to the tower base is cut out); the mat
  generator, the simulator (its JS twin, held equal by
  ``sceneFixture.cases.json``) and the zone tests use it.
* ``contract_hash()`` is stored in every calibration file; a calibration made
  under another contract is refused.
"""

from __future__ import annotations

import hashlib
import json
import math

# ---- BEGIN CONTRACT BLOCK (40 §3; byte for byte, never edit by hand) ----
FIXTURE_ID = "tower_v1"
FRAME = "F: origin on the joint-1 axis at the TABLE surface, x forward, y left, z up; metres, radians"
LIFT_M = 0.0072                      # arm base bottom above the table (plate pocket floor), both arms (decision C4)
MAT_THICKNESS_M = 0.00045            # laminated mat, top surface above the table (design value, decision C5)
FINGERTIP_BELOW_EE_M = {"omx": 0.0026, "v1": 0.0}   # solver EE point above the table when the fingertips touch it, tool vertical, gripper at the opening that holds the 30 mm cube (OMX) / claw closed (V1). OMX: PROVISIONAL mesh value, replaced by the measurement of WP-M1 (decision D23)
LENS_F = (-0.1, 0.1, 0.575)        # entrance pupil of the tower camera (decision D55, placement B)
AIM_F = (0.08278, 0.02924, 0.0)    # the optical axis hits the table here (re-optimised for B; 52 §1)
CAMERA_ROLL = "none"                      # image x = normalise(axis x (0,0,1)), image y (down) = axis x image_x; image top = far side of the table
LENS_FOLLOW_MAX_M = 0.010                 # a solved lens position farther than this from LENS_F refuses real motion (decision C7)
CAPTURE = {"width": 1920, "height": 1080, "fps": 30, "fourcc": "MJPG"}
OUTSIDE = {"window_x0": 320, "window_y0": 60, "window_w": 1280, "window_h": 960, "width": 640, "height": 480}
LENS_NOMINAL = {"model": "kb2", "f_px": 1037.8, "aspect": 1.0, "cx": 959.5, "cy": 539.5, "k1": 0.0, "k2": 0.0}
LENS_BAND = {"f_px": (965.0, 1120.0), "c_px": 40.0, "k": 0.15}
# physical tower solids at B: ORIENTED boxes (name, centre, R row by row — its COLUMNS are the box axes in F —, half extents)
TOWER_OBB_F = (
    ("pad", (-0.09980, 0.13325, 0.00360), ((1.000000, 0.000000, 0.000000), (0.000000, 1.000000, 0.000000), (0.000000, 0.000000, 1.000000)), (0.07290, 0.05435, 0.00360)),
    ("socket", (-0.14550, 0.15020, 0.03200), ((1.000000, 0.000000, 0.000000), (0.000000, 1.000000, 0.000000), (0.000000, 0.000000, 1.000000)), (0.02320, 0.02250, 0.03200)),
    ("rib_a", (-0.10660, 0.12350, 0.02250), ((0.862618, 0.505856, 0.000000), (-0.505856, 0.862618, 0.000000), (0.000000, 0.000000, 1.000000)), (0.01878, 0.00300, 0.02250)),
    ("rib_b", (-0.07420, 0.10450, 0.01425), ((0.862618, 0.505856, 0.000000), (-0.505856, 0.862618, 0.000000), (0.000000, 0.000000, 1.000000)), (0.01878, 0.00300, 0.01425)),
    ("column", (-0.14750, 0.14750, 0.28450), ((1.000000, 0.000000, 0.000000), (0.000000, 1.000000, 0.000000), (0.000000, 0.000000, 1.000000)), (0.01500, 0.01500, 0.28050)),
    ("block", (-0.14550, 0.14950, 0.56500), ((1.000000, 0.000000, 0.000000), (0.000000, 1.000000, 0.000000), (0.000000, 0.000000, 1.000000)), (0.02320, 0.02320, 0.04500)),
    ("head", (-0.10633, 0.10296, 0.59546), ((-0.361047, -0.882677, 0.300877), (-0.932548, 0.341738, -0.116488), (0.000000, -0.322639, -0.946522)), (0.01852, 0.01805, 0.02192)),
    ("lens", (-0.10030, 0.10012, 0.57595), ((-0.361047, -0.882677, 0.300877), (-0.932548, 0.341738, -0.116488), (0.000000, -0.322639, -0.946522)), (0.00850, 0.00850, 0.00550)),   # revision 4: a Ø16 barrel + 0.5 mm (CAD v3 F7: its Ø16 dummy, which the focus collar clamps, poked 0.504 mm out of revision 3's Ø14 + 0.5 box)
    ("bridge", (-0.11845, 0.11845, 0.59500), ((0.707107, 0.707107, 0.000000), (-0.707107, 0.707107, 0.000000), (0.000000, 0.000000, 1.000000)), (0.01609, 0.01500, 0.01500)),
)
TOWER_LEAN_AT_LENS_M = 0.0175     # undetected rigid lean at the lens (LENS_FOLLOW_MAX_M + the 6.6 mm solve error, / cos 18.8 deg)
TOWER_AXIS_M = 0.0012             # column axis +-1.0 mm (41 H3) + the screws' 0.2 mm pull
TOWER_BUILD_M = 0.0005            # printed-part tolerance
TOWER_TOP_ROTATION_M = 0.0026     # block, head, lens, bridge turned within the 3 deg orientation gate
TOWER_SAMPLE_M = 0.0015           # half of the <= 3 mm travel per sample (the dense count)
TOWER_RESIDUAL_SERVER_M = {"v1": 0.0130, "omx": 0.0100}   # calibrated arm vs the tower placed by its tags 480/481 (D65): max over every guard-box corner and every accepted kit of every Monte Carlo cell (r6 p01: 12.25 / 9.35 mm)
TOWER_RESIDUAL_UNCALIBRATED_M = {"v1": 0.0260, "omx": 0.0195}   # nominal kinematics vs the tower at its contract place: before a kit's first arm check, and the V1 driver (r6 p04: 25.4 / 19.1 mm over the design cells); re-sized only by RG-T1R (42)
TOWER_RESIDUAL_DRIVER_M = 0.0260  # = TOWER_RESIDUAL_UNCALIBRATED_M["v1"]: WP-T1's literal twin (equal by test)
TOWER_DROOP_DESIGN_ENV_RAD = {"v1": (0.027, 0.025, 0.010), "omx": (0.061, 0.045, 0.017)}   # joints 2..4: the envelope the arm check measures on the worst kit of the design prior; used until the kit's own (r6 p01)
TOWER_DROOP_KAPPA = {"v1": 2.6, "omx": 1.8}   # the droop bound's factor: max over every accepted kit of every cell of true sag displacement / bound (r6 p01: 2.49 / 1.71)
TOWER_DROOP_TAU_AC_NM = {"v1": (1.2732, 0.9747, 0.1169), "omx": (0.5241, 0.3509, 0.0518)}   # joints 2..4: largest |gravity torque| over the pinned arm-check poses, 45 g target at target_mount_ee, nominal link masses (r6 p06)
TOWER_FIT_MAX_M = 0.0010          # RMS of the fixture-tag fit (8 corners) above which the tags are not where the tower carries them
TOWER_TOP_PARTS = ("block", "head", "lens", "bridge")
TOWER_T1_PARTS = ("block", "head", "lens", "bridge", "column", "socket")   # what WP-T1 judges (C11)
PLATE_OUTLINE_F = ((-0.0596, -0.0886), (0.0544, 0.0886))   # base plate, outer rectangle (CAD)
FIXTURE_TAGS_F = {480: (-0.09, 0.165), 481: (-0.14, 0.11)}   # tag centres on the fixture pad (481 as built)
FIXTURE_TAG_SIZE_M = 0.025; FIXTURE_TAG_Z_M = 0.0072     # black-square edge; tag plane above the table
MAT_TAG_SIZE_M = 0.03; MAT_CARD_M = 0.04
MAT_NOTCH_F = {"x_max": 0.0549, "y_abs": 0.0891}   # cut-out around the plate, open to the rear edge
TARGET = {"tag_size_m": 0.020, "pitch_m": 0.026, "grid": 3, "plate_m": 0.080}   # clip-on target: 3 x 3 tags
TAG_FAMILY = "tag36h11"; RESERVED_TAG_IDS = (400, 586)   # no object may use an id in this closed range

# ---- omx ----
ARM_OMX = {
    "profiles": ('omx_full', 'omx_follower'),
    "reach_m": (0.1, 0.25), "zone_sector_deg": (-90.0, 90.0), "zone_inner_m": 0.12, "zone_outer_m": 0.25,
    "zone_cut_knots": ((0.12, 66.0), (0.1225, 66.0), (0.245, 73.0), (0.25, 74.0)),   # D66: (r m, az deg); the printed zone ends at az <= the polyline through these knots (r6 p09b/p09c/p09d)
    "mat_sheet_f": ((-0.025, -0.33), (0.355, 0.33)),
    "joint1_window_deg": (-100.0, 100.0),
    "observe_joints_rad": (0.0, -1.75, 0.0, -0.3, 0.0),
    "link_masses": ((1, 0.0660, (0.0, 0.0006, 0.0474)), (2, 0.0872, (0.0181, 0.0003, 0.0990)), (3, 0.0838, (0.0874, 0.0004, -0.0036)), (4, 0.0300, (0.0232, 0.0001, 0.0060)), (5, 0.0680, (0.0300, 0.0, 0.0135))),   # (guard frame 1..5, kg, CoM in that frame): omx_f.urdf inertials, fingers lumped on frame 5
    "mat_tags_f": {440: (0.0, -0.3), 441: (0.0776, -0.2898), 442: (0.15, -0.2598), 443: (0.2121, -0.2121), 444: (0.2598, -0.15), 445: (0.2898, -0.0776), 446: (0.3, 0.0), 447: (0.2898, 0.0776), 448: (0.2598, 0.15), 449: (0.2121, 0.2121), 450: (0.15, 0.2598), 451: (0.0776, 0.2898), 452: (0.0, 0.3), 453: (0.076, 0.0)},
    "target_tag_ids": (520, 521, 522, 523, 524, 525, 526, 527, 528),   # row-major over (i, j)
    "target_mount_ee": (
        (-0.34202, 0.0, -0.939693, -0.07),
        (0.0, 1.0, 0.0, 0.095),
        (0.939693, 0.0, -0.34202, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    ),
    "arm_check_poses_rad": (   # PINNED (C10): the +-60 deg list screened at 0.575; never regenerated from the grid rule
        (0.3491, -0.7073, 0.6890, 1.1890, 0.4363),
        (0.3491, -0.2431, 0.2670, 1.5468, 0.0000),
        (0.3491, 0.2570, -0.3624, 2.0762, -0.4363),
        (1.0472, -0.1541, -0.2104, 2.3353, 0.0000),
        (1.0472, -0.6093, 0.2339, 1.9462, -0.4363),
        (0.3491, -0.6093, 0.2339, 1.5462, -0.8727),
        (-0.3491, -0.2431, 0.2670, 1.1468, -0.4363),
        (-0.3491, -0.1541, -0.2104, 1.5353, -0.8727),
        (-0.3491, 0.2570, -0.3624, 1.6762, -0.8727),
        (-1.0472, -0.2431, 0.2670, 1.9468, -0.4363),
        (-1.0472, -0.6093, 0.2339, 1.9462, -0.4363),
        (-1.0472, -0.7073, 0.6890, 1.5890, -0.4363),
        (-0.3491, -0.7073, 0.6890, 1.9890, 0.0000),
        (-0.3491, -0.6093, 0.2339, 2.3462, 0.0000),
        (-1.0472, -0.1541, -0.2104, 1.5353, -0.4363),
        (-1.0472, 0.2570, -0.3624, 1.2762, -0.4363),
        (0.3491, -0.1541, -0.2104, 1.9353, 0.8727),
        (1.0472, -0.2431, 0.2670, 1.9468, 0.4363),
        (1.0472, -0.7073, 0.6890, 1.5890, 0.8727),
        (1.0472, 0.2570, -0.3624, 1.2762, 0.0000),
    ),
}

# ---- v1 ----
ARM_V1 = {
    "profiles": ('v1_studio',),
    "reach_m": (0.1, 0.34), "zone_sector_deg": (-85.0, 85.0), "zone_inner_m": 0.11, "zone_outer_m": 0.34,   # 5 deg inside the joint-1 limit: a joint-1 zero offset turns the mat against the arm
    "zone_cut_knots": ((0.11, 52.0), (0.265, 74.0), (0.297, 83.0), (0.298, 85.0)),   # D66: (r m, az deg); the printed zone ends at az <= the polyline through these knots, no cut beyond r 0.298 (r6 p09b/p09c/p09d)
    "mat_sheet_f": ((-0.025, -0.415), (0.445, 0.415)),
    "joint1_window_deg": (-90.0, 90.0),
    "observe_joints_rad": (0.0, 0.1, 2.2, -1.61, 0.0),
    "link_masses": ((1, 0.060, (0.0014, 0.0, 0.0276)), (2, 0.190, (0.1281, -0.0115, -0.0002)), (3, 0.150, (0.0, 0.1827, 0.0011)), (4, 0.085, (0.0122, 0.0510, 0.0)), (5, 0.140, (0.0, 0.0, 0.045))),   # (guard frame 1..5, kg, CoM in that frame): v1.urdf inertials as WP-T1's V1_LINK_MASSES, claw lumped on frame 5
    "mat_tags_f": {400: (0.0, -0.39), 401: (0.1009, -0.3767), 402: (0.195, -0.3377), 403: (0.2758, -0.2758), 404: (0.3377, -0.195), 405: (0.3767, -0.1009), 406: (0.39, 0.0), 407: (0.3767, 0.1009), 408: (0.3377, 0.195), 409: (0.2758, 0.2758), 410: (0.195, 0.3377), 411: (0.1009, 0.3767), 412: (0.0, 0.39), 413: (0.076, 0.0)},
    "target_tag_ids": (500, 501, 502, 503, 504, 505, 506, 507, 508),   # row-major over (i, j)
    "target_mount_ee": (
        (0.939693, 0.0, -0.34202, 0.0),
        (0.0, -1.0, 0.0, -0.095),
        (-0.34202, 0.0, -0.939693, -0.11),
        (0.0, 0.0, 0.0, 1.0),
    ),
    "arm_check_poses_rad": (   # PINNED (C10): the screened 0.570 list, identical at 0.575; never regenerated from the grid rule; AC12 moved for D84
        (1.0472, 1.3248, 1.2474, -1.0934, 0.4363),
        (0.3491, 0.8936, 0.8943, -1.1715, 0.0000),
        (0.3491, 1.3248, 1.2474, -1.4934, 0.0000),
        (1.0472, 0.8936, 0.8943, -1.5715, -0.4363),
        (1.0472, 0.9448, 1.2022, -1.8282, 0.4363),
        (0.3491, 0.9448, 1.2022, -1.4282, 0.8727),
        (0.3491, 1.3466, 1.5396, -1.7638, 0.8727),
        (0.3491, 1.7391, 1.7097, -1.5414, 0.8727),
        (-0.3491, 1.3466, 1.5396, -1.7638, 0.8727),
        (-0.3491, 1.7882, 2.0447, -1.4274, 0.4363),
        (-1.0472, 1.7391, 1.7097, -1.5414, 0.0000),
        (-1.0472, 1.3466, 1.4523, -1.3638, -0.4363),   # AC12, D84: joint 3 -5 deg (was 1.5396), so its target lies within LENS_FIELD_MIN_RAD / 1.1 of the camera axis (0.695 rad, was 0.734)
        (-1.0472, 0.9448, 1.2022, -1.8282, -0.4363),
        (-0.3491, 1.3248, 1.2474, -1.4934, 0.0000),
        (-0.3491, 0.8936, 0.8943, -1.5715, 0.4363),
        (-1.0472, 0.8936, 0.8943, -1.5715, 0.8727),
        (-1.0472, 1.3248, 1.2474, -1.0934, 0.4363),
        (-1.0472, 1.7882, 2.0447, -1.8274, 0.8727),
        (-0.3491, 1.7391, 1.7097, -1.1414, -0.4363),
        (-0.3491, 0.9448, 1.2022, -1.8282, -0.8727),
    ),
}
# ---- END CONTRACT BLOCK ----

# Every name the block above assigns, in its order (fenced against the block's
# AST by test_scene_fixture.py). contract_hash() and the React render read
# exactly these.
CONTRACT_NAMES = (
    'FIXTURE_ID', 'FRAME', 'LIFT_M', 'MAT_THICKNESS_M', 'FINGERTIP_BELOW_EE_M', 'LENS_F', 'AIM_F',
    'CAMERA_ROLL', 'LENS_FOLLOW_MAX_M', 'CAPTURE', 'OUTSIDE', 'LENS_NOMINAL', 'LENS_BAND', 'TOWER_OBB_F',
    'TOWER_LEAN_AT_LENS_M', 'TOWER_AXIS_M', 'TOWER_BUILD_M', 'TOWER_TOP_ROTATION_M', 'TOWER_SAMPLE_M',
    'TOWER_RESIDUAL_SERVER_M', 'TOWER_RESIDUAL_UNCALIBRATED_M', 'TOWER_RESIDUAL_DRIVER_M',
    'TOWER_DROOP_DESIGN_ENV_RAD', 'TOWER_DROOP_KAPPA', 'TOWER_DROOP_TAU_AC_NM', 'TOWER_FIT_MAX_M',
    'TOWER_TOP_PARTS', 'TOWER_T1_PARTS', 'PLATE_OUTLINE_F', 'FIXTURE_TAGS_F', 'FIXTURE_TAG_SIZE_M',
    'FIXTURE_TAG_Z_M', 'MAT_TAG_SIZE_M', 'MAT_CARD_M', 'MAT_NOTCH_F', 'TARGET', 'TAG_FAMILY',
    'RESERVED_TAG_IDS', 'ARM_OMX', 'ARM_V1',
)

# The two arms by the key the contract's per-arm dicts use.
ARMS = {'omx': ARM_OMX, 'v1': ARM_V1}

# Radius and azimuth tolerance of the zone predicate (§3: "both 1e-9 tolerant").
_ZONE_TOL = 1e-9


# ── arms and tags ────────────────────────────────────────────────────────────
def arm_spec(profile_id):
    """``ARM_OMX`` / ``ARM_V1`` for a robot profile id, ``None`` for any other."""
    key = arm_key(profile_id)
    return None if key is None else ARMS[key]


def arm_key(profile_id):
    """``'omx'`` / ``'v1'`` (the contract's per-arm key) for a profile id, else ``None``."""
    if not isinstance(profile_id, str):
        return None
    for key in ('omx', 'v1'):
        if profile_id in ARMS[key]['profiles']:
            return key
    return None


def is_reserved_tag_id(tag_id) -> bool:
    """True for an id in ``RESERVED_TAG_IDS`` (the mats, the fixture, the targets)."""
    lo, hi = RESERVED_TAG_IDS
    return lo <= tag_id <= hi


def tag_corners_xy(cx, cy, size):
    """The four model corners of a tag printed upright with its top toward +x,
    in the detector's order: ``(cx+h, cy−h), (cx+h, cy+h), (cx−h, cy+h), (cx−h, cy−h)``."""
    h = size / 2.0
    return ((cx + h, cy - h), (cx + h, cy + h), (cx - h, cy + h), (cx - h, cy - h))


def _target_corners():
    pitch, size = TARGET['pitch_m'], TARGET['tag_size_m']
    return tuple((i, j, tag_corners_xy((i - 1) * pitch, (j - 1) * pitch, size))
                 for i in range(TARGET['grid']) for j in range(TARGET['grid']))


# The 36 target corners in the target frame: (i, j, the tag's four corners), row-major.
TARGET_CORNERS = _target_corners()


def target_tag_corners(spec):
    """``((tag_id, corners), …)`` of an arm's clip-on target: tag (i, j) is ``target_tag_ids[3·i + j]``."""
    ids = spec['target_tag_ids']
    return tuple((ids[TARGET['grid'] * i + j], corners) for i, j, corners in TARGET_CORNERS)


def fixture_corners_w(base_axis_x):
    """The 8 nominal fixture-tag corners in W, tag 480 then 481, each in ``tag_corners_xy`` order."""
    out = ()
    for tid in (480, 481):
        cx, cy = FIXTURE_TAGS_F[tid]
        out += tag_corners_xy(cx + base_axis_x, cy, FIXTURE_TAG_SIZE_M)
    return out


# ── the tower in W ───────────────────────────────────────────────────────────
def _pose(pose):
    if pose is None:
        return 0.0, 0.0, 0.0
    try:
        psi, tx, ty = (float(v) for v in pose)
    except (TypeError, ValueError) as exc:
        raise ValueError('tower pose must be (psi, tx, ty)') from exc
    if not all(math.isfinite(v) for v in (psi, tx, ty)):
        raise ValueError('tower pose must be finite')
    return psi, tx, ty


def tower_obbs_w(base_axis_x, pose=None):
    """``TOWER_OBB_F`` placed in W: ``(name, centre, R rows, half extents)`` per box,
    the PHYSICAL solids (no allowance). ``pose`` = ``(psi, tx, ty)`` from
    :func:`tower_pose_fit`: every centre ``c`` goes to
    ``R(psi)·(c_xy + (base_axis_x, 0)) + (tx, ty)``, z unchanged, and its axes to
    ``R(psi)·R_box``; ``None`` is the contract place. Raises ``ValueError`` for a
    malformed or non-finite pose."""
    psi, tx, ty = _pose(pose)
    c, s = math.cos(psi), math.sin(psi)
    out = []
    for name, centre, rows, half in TOWER_OBB_F:
        x, y = centre[0] + base_axis_x, centre[1]
        placed = (c * x - s * y + tx, s * x + c * y + ty, centre[2])
        turned = ((c * rows[0][0] - s * rows[1][0], c * rows[0][1] - s * rows[1][1], c * rows[0][2] - s * rows[1][2]),
                  (s * rows[0][0] + c * rows[1][0], s * rows[0][1] + c * rows[1][1], s * rows[0][2] + c * rows[1][2]),
                  tuple(rows[2]))
        out.append((name, placed, turned, half))
    return tuple(out)


def tower_pose_fit(measured, base_axis_x):
    """``(psi, tx, ty, rms)``: the least-squares 2D rigid map (rotation and
    translation, no scale, no mirror) of ``fixture_corners_w(base_axis_x)`` onto
    the 8 measured corners (W, metres; ``(x, y)`` or ``(x, y, z)`` each), and the
    RMS of the fit. Raises ``ValueError`` unless exactly 8 finite corners arrive."""
    try:
        pts = [(float(p[0]), float(p[1])) for p in measured]
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError('measured corners must be 8 points (x, y)') from exc
    if len(pts) != 8 or not all(math.isfinite(v) for p in pts for v in p):
        raise ValueError('measured corners must be 8 finite points (x, y)')
    nom = fixture_corners_w(base_axis_x)
    n = float(len(nom))
    ax, ay = sum(p[0] for p in nom) / n, sum(p[1] for p in nom) / n
    bx, by = sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n
    dot = cross = 0.0
    for (px, py), (qx, qy) in zip(nom, pts):
        px, py, qx, qy = px - ax, py - ay, qx - bx, qy - by
        dot += px * qx + py * qy
        cross += px * qy - py * qx
    psi = math.atan2(cross, dot)
    c, s = math.cos(psi), math.sin(psi)
    tx, ty = bx - (c * ax - s * ay), by - (s * ax + c * ay)
    sq = 0.0
    for (px, py), (qx, qy) in zip(nom, pts):
        ex, ey = c * px - s * py + tx - qx, s * px + c * py + ty - qy
        sq += ex * ex + ey * ey
    return psi, tx, ty, math.sqrt(sq / n)


def lens_w(base_axis_x, pose):
    """The lens carried by a tower pose: ``(R(psi)·(LENS_F_xy + (base_axis_x, 0)) + (tx, ty), LENS_F.z)``."""
    psi, tx, ty = _pose(pose)
    c, s = math.cos(psi), math.sin(psi)
    x, y = LENS_F[0] + base_axis_x, LENS_F[1]
    return (c * x - s * y + tx, s * x + c * y + ty, LENS_F[2])


# ── the printed work zone (D66) ──────────────────────────────────────────────
def _cut_az(knots, r):
    """The cut's azimuth (deg) at radius ``r``: linear between consecutive knots
    (radii ascending), a knot's own value within the tolerance of its radius,
    the first knot's value below it, +inf beyond the last knot (no cut there)."""
    if not knots or r > knots[-1][0] + _ZONE_TOL:
        return math.inf
    if r <= knots[0][0]:
        return knots[0][1]
    for (r0, a0), (r1, a1) in zip(knots, knots[1:]):
        if r0 - _ZONE_TOL <= r <= r1 + _ZONE_TOL:
            if r1 - r0 <= 1e-12:
                return min(a0, a1)
            t = min(1.0, max(0.0, (r - r0) / (r1 - r0)))
            return a0 + (a1 - a0) * t
    return math.inf


def zone_az_hi(spec, r):
    """The zone's tower-side edge at radius ``r`` (deg): the smaller of the
    sector edge and the cut through ``zone_cut_knots``."""
    return min(spec['zone_sector_deg'][1], _cut_az(spec['zone_cut_knots'], r))


def in_zone(spec, x_f, y_f) -> bool:
    """True when ``(x_f, y_f)`` (F, metres) lies in the printed zone: radius
    within ``[zone_inner_m, zone_outer_m]`` and azimuth (about the joint-1 axis,
    positive toward +y, the tower's side) within ``[zone_sector_deg[0],
    zone_az_hi(r)]``, both 1e-9 tolerant."""
    try:
        x, y = float(x_f), float(y_f)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(x) and math.isfinite(y)):
        return False
    r = math.hypot(x, y)
    if not spec['zone_inner_m'] - _ZONE_TOL <= r <= spec['zone_outer_m'] + _ZONE_TOL:
        return False
    az = math.degrees(math.atan2(y, x))
    return spec['zone_sector_deg'][0] - _ZONE_TOL <= az <= zone_az_hi(spec, r) + _ZONE_TOL


# ── the contract hash ────────────────────────────────────────────────────────
def _canonical(value):
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical(v) for v in value]
    return value


def contract_values() -> dict:
    """``{name: value}`` of every contract constant, JSON-ready (tuples as lists,
    dict keys as text) — what :func:`contract_hash` and the React copy carry."""
    namespace = globals()
    return {name: _canonical(namespace[name]) for name in CONTRACT_NAMES}


def contract_hash() -> str:
    """sha256 over a canonical JSON dump of every contract constant (keys sorted,
    no whitespace). Stored in each calibration file; a calibration made under
    another hash is refused. Entering the measured OMX fingertip offset changes
    it on purpose."""
    text = json.dumps(contract_values(), sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False)
    return hashlib.sha256(text.encode('ascii')).hexdigest()

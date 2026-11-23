#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""Whole-link geometry for the Feetech Roboter-Studio arm (``v1_studio``) — the
DRIVER's copy.

One arm, one spec: :data:`V1` (5 arm joints, 6 link boxes), reachable through
the :data:`SPECS` table. Every public function takes ``spec=V1`` by default.

WHY A SECOND COPY EXISTS. The server package has the same model in
``physical_ai_server/workflow/arm_geometry.py``, but the driver runs in the
``open_manipulator`` container and cannot import the server package. It needs
the model because the boot-home glide (``run_home``) is a high-exposure home
path: it starts from whatever pose a LIMP arm collapsed into, and a straight
joint-space line from such a pose to HOME can drive a link through the table.

The duplication is real and is guarded rather than wished away:
``robotis_ai_setup/tests/test_feetech_geometry.py`` AST-reads BOTH copies of the
box table and the kinematic constants and asserts they are equal, the same
discipline ``test_feetech_bus.py`` already applies to ``HOME_JOINTS_RAD``.

PURE PYTHON ON PURPOSE — no NumPy. The deps-free ``robotis_ai_setup/tests``
suite must be able to import and exercise this directly, and the arithmetic is
tiny (6 links x 8 corners per pose).

WHAT IT IS. One axis-aligned box per link, in that link's OWN frame. A
link-frame box CONTAINS its mesh, so ``lowest_z`` is a SOUND lower bound on the
true lowest point — pessimistic, never optimistic. Orientation-aware, which is
what a floor test needs; a direction-blind per-link RADIUS refuses essentially
everything.

z = 0 IS THE TABLE. Not a guess: the shipped base frame is re-origined to the
base bottom, so ``base_link``'s own mesh spans z ∈ [0.0000, 0.0521] m, i.e. it
is bolted to the table. That is why this module can judge a boot glide with no
calibration at all.

TWO MORE JUDGEMENTS (WP-T1, the scene-camera tower): ``tower_glide_decision``
judges the same glide against the camera tower with the server guard's box
rule and the driver's allowance (the tower's numbers mirrored from the
contract, see the tower section), and ``limp_fall_direction`` says where a
limp arm would fall. Both feed ``run_home`` and the driver's tower-state
topic. Pure Python too, for the same reason; judging one glide against the
tower is not tiny (tens of milliseconds), which is why the driver judges its
live pose on a thread of its own.
"""

from __future__ import annotations

import math

# ── kinematic chain — MIRRORS physical_ai_server/workflow/v1_ik.py ───────────
# (joint origin xyz, fixed rpy, rotation axis). A drift test AST-reads both
# (driver ``_V1_J*`` ↔ server ``_J*``).
_V1_J1_XYZ = (0.0, 0.0, 0.0502)
_V1_J1_RPY = (0.0, 0.0, -1.5708)
_V1_J2_XYZ = (0.0, 0.0, 0.04175)
_V1_J2_RPY = (-1.5708, 0.0, 1.5708)
_V1_J3_XYZ = (0.1555, -0.032, 0.0)
_V1_J3_RPY = (-3.1416, 0.0, -1.5708)
_V1_J4_XYZ = (0.0, 0.2225, 0.0)
_V1_J4_RPY = (0.0, 0.0, 0.0)
_V1_J5_XYZ = (0.0, 0.07195, 0.0)
_V1_J5_RPY = (-1.5708, 0.0, 0.0)
_V1_AXES = ((0, 0, -1), (0, 0, -1), (0, 0, -1), (0, 0, -1), (0, 0, -1))

# ── link boxes — MIRRORS arm_geometry.V1_LINK_BOXES ──────────────────────────
# (min_xyz, max_xyz) in each link's OWN frame, metres; index 0 = base_link,
# 1..5 = link1..link5. Derived from the full-resolution generated meshes and
# rounded OUTWARD to 0.1 mm, so a constant can never be SMALLER than the mesh it
# stands for. Index 5 is the UNION over the whole claw band [0, 1.5708] of link5
# + end_effector + both blades, at both claw gear ratios, so it is valid at
# every jaw opening. base_link spans z in [0.0000, 0.0521] m — z = 0 IS the
# table.
V1_LINK_BOXES = (
    ((-0.0508, -0.0751, +0.0000), (+0.0493, +0.0751, +0.0521)),   # 0 base_link
    ((-0.0200, -0.0153, -0.0065), (+0.0200, +0.0153, +0.0518)),   # 1 link1
    ((-0.0097, -0.0420, -0.0244), (+0.1708, +0.0150, +0.0244)),   # 2 link2
    ((-0.0150, -0.0096, -0.0244), (+0.0150, +0.2326, +0.0244)),   # 3 link3
    ((-0.0126, -0.0097, -0.0244), (+0.0351, +0.0705, +0.0244)),   # 4 link4
    ((-0.0286, -0.0966, -0.0045), (+0.0678, +0.0966, +0.1091)),   # 5 link5 + end_effector + both blades
)

# Corner offsets of a unit box (bit patterns 000..111).
_CORNER_BITS = tuple((i >> 2 & 1, i >> 1 & 1, i & 1) for i in range(8))


def _rpy_matrix(r: float, p: float, y: float):
    cr, sr = math.cos(r), math.sin(r)
    cp, sp = math.cos(p), math.sin(p)
    cy, sy = math.cos(y), math.sin(y)
    # Rz @ Ry @ Rx
    return (
        (cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr),
        (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr),
        (-sp, cp * sr, cp * cr),
    )


def _axis_rot(axis, th: float):
    norm = math.sqrt(sum(float(v) ** 2 for v in axis))
    ax, ay, az = (float(v) / norm for v in axis)
    c, s = math.cos(th), math.sin(th)
    t = 1.0 - c
    return (
        (t * ax * ax + c, t * ax * ay - s * az, t * ax * az + s * ay),
        (t * ax * ay + s * az, t * ay * ay + c, t * ay * az - s * ax),
        (t * ax * az - s * ay, t * ay * az + s * ax, t * az * az + c),
    )


def _mat_mul(a, b):
    return tuple(
        tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3))
        for i in range(3)
    )


def _apply(rot, vec):
    return (
        rot[0][0] * vec[0] + rot[0][1] * vec[1] + rot[0][2] * vec[2],
        rot[1][0] * vec[0] + rot[1][1] * vec[1] + rot[1][2] * vec[2],
        rot[2][0] * vec[0] + rot[2][1] * vec[1] + rot[2][2] * vec[2],
    )


_IDENT = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
# WORLD = URDF rotated pi about z. Only the SIGN of x and y flips, and z — the
# only component this module reads — is untouched, so the rotation is carried
# for clarity rather than necessity.
_RZ_PI = ((-1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, 1.0))

_V1_FIXED = (
    (_V1_J1_XYZ, _rpy_matrix(*_V1_J1_RPY)),
    (_V1_J2_XYZ, _rpy_matrix(*_V1_J2_RPY)),
    (_V1_J3_XYZ, _rpy_matrix(*_V1_J3_RPY)),
    (_V1_J4_XYZ, _rpy_matrix(*_V1_J4_RPY)),
    (_V1_J5_XYZ, _rpy_matrix(*_V1_J5_RPY)),
)


class _Spec:
    """One arm's chain + box table. A tiny value holder rather than a dataclass
    or a dict so the module stays importable under the deps-free unit-test
    loader with no stdlib import beyond ``math``."""

    __slots__ = ('n_joints', 'fixed', 'axes', 'boxes')

    def __init__(self, fixed, axes, boxes):
        self.n_joints = len(fixed)
        self.fixed = fixed
        self.axes = axes
        self.boxes = boxes


V1 = _Spec(_V1_FIXED, _V1_AXES, V1_LINK_BOXES)

# EDUBOTICS_ROBOT_TYPE → spec. An unknown/absent id resolves to V1, the only
# Feetech arm; the DRIVER passes its spec explicitly rather than reading the env
# here, so this module has no environment coupling and stays a pure function of
# its arguments.
SPECS = {'v1_studio': V1}


def spec_for(robot_type):
    """Spec for an ``EDUBOTICS_ROBOT_TYPE`` value; unknown/absent → V1."""
    return SPECS.get((robot_type or '').strip(), V1)


def link_frames(joints, spec=None):
    """``[(rot 3x3, pos 3)]`` per link in the WORLD frame: index 0 = base_link,
    then one per arm joint. ``None`` for fewer joints than the spec has."""
    spec = V1 if spec is None else spec
    n_joints = spec.n_joints
    q = list(joints)
    if len(q) < n_joints:
        return None
    if not all(isinstance(v, (int, float)) and math.isfinite(float(v))
               for v in q[:n_joints]):
        return None
    rot, pos = _IDENT, (0.0, 0.0, 0.0)
    out = [(_RZ_PI, (0.0, 0.0, 0.0))]
    for i in range(n_joints):
        offset, fixed = spec.fixed[i]
        pos = tuple(pos[k] + _apply(rot, offset)[k] for k in range(3))
        rot = _mat_mul(_mat_mul(rot, fixed),
                       _axis_rot(spec.axes[i], float(q[i])))
        out.append((_mat_mul(_RZ_PI, rot), _apply(_RZ_PI, pos)))
    return out


def lowest_z(joints, spec=None):
    """Sound lower bound on the lowest point of any MOVING link, in metres above
    the mounting plane. ``None`` when the pose cannot be evaluated.

    LINK 0 (base_link) is EXCLUDED: it is the fixed mount and its underside IS
    the table, so including it would peg every reading at ~0 and the check would
    judge nothing.
    """
    spec = V1 if spec is None else spec
    frames = link_frames(joints, spec)
    if frames is None:
        return None
    worst = None
    for idx in range(1, len(spec.boxes)):
        rot, pos = frames[idx]
        lo, hi = spec.boxes[idx]
        for bx, by, bz in _CORNER_BITS:
            local = (hi[0] if bx else lo[0],
                     hi[1] if by else lo[1],
                     hi[2] if bz else lo[2])
            z = (rot[2][0] * local[0] + rot[2][1] * local[1]
                 + rot[2][2] * local[2] + pos[2])
            if worst is None or z < worst:
                worst = z
    return worst


# Worst-case radius of a link point from any rotation axis, and the sample step,
# mirroring the server's swept rule so the tunneling argument is the same one.
_R_MAX_M = 0.45
_STEP_M = 0.003
_MAX_SAMPLES = 256


def swept_lowest_z(q_start, q_end, spec=None):
    """:func:`lowest_z` minimised over the straight joint-space line — the line
    ``build_boot_home``'s quintic blend reparametrises, so it is exactly the set
    of poses the glide passes through. ``None`` if any sample is unevaluable."""
    spec = V1 if spec is None else spec
    n_joints = spec.n_joints
    a = [float(v) for v in list(q_start)[:n_joints]]
    b = [float(v) for v in list(q_end)[:n_joints]]
    if len(a) < n_joints or len(b) < n_joints:
        return None
    delta = max(abs(b[i] - a[i]) for i in range(n_joints))
    n = min(_MAX_SAMPLES,
            max(1, int(math.ceil(_R_MAX_M * delta / _STEP_M))))
    worst = None
    for i in range(n + 1):
        u = i / n
        value = lowest_z([a[k] + (b[k] - a[k]) * u for k in range(n_joints)],
                         spec)
        if value is None:
            return None
        if worst is None or value < worst:
            worst = value
    return worst


# ══ the camera tower (WP-T1; spec 40 §3 and S6.2 revision 4) ════════════════
#
# The V1's activation glide (``build_boot_home``: a straight line in joint
# space from the measured pose to HOME, the claw blended to 0.90 with the same
# quintic) is judged against the tower of the scene-camera kit before it is
# driven — refuse-only, no detour (owner decisions D53, D65). The rule is the
# server's box guard (``workflow/fixture_guard.py``) with the DRIVER's
# allowance: the arm is uncalibrated here, so the tower stands at its contract
# place and the residual is the uncalibrated V1's (TOWER_RESIDUAL_DRIVER_M), the
# droop is the V1's design envelope times its κ. The parts judged are
# TOWER_T1_PARTS (C11): the pad and the ribs are the table's business, which the
# floor check above already judges.
#
# Every literal below is a MIRROR. The tower's own numbers are
# ``physical_ai_server/workflow/scene_fixture.py``'s contract
# (tests/test_scene_fixture_lockstep.py holds each equal); the claw's cells are
# ``workflow/arm_cells.py``'s generated V1 table and the guard's own constants
# ``workflow/fixture_guard.py``'s (tests/test_feetech_geometry.py holds those).

LIFT_M = 0.0072                    # arm base bottom above the table (the plate's pocket floor)
LENS_F = (-0.1, 0.1, 0.575)        # the tower camera's entrance pupil (its height sets the lean per metre)
# physical tower solids: ORIENTED boxes (name, centre, R row by row — its COLUMNS are the box axes in F —, half extents)
TOWER_OBB_F = (
    ("pad", (-0.09980, 0.13325, 0.00360), ((1.000000, 0.000000, 0.000000), (0.000000, 1.000000, 0.000000), (0.000000, 0.000000, 1.000000)), (0.07290, 0.05435, 0.00360)),
    ("socket", (-0.14550, 0.15020, 0.03200), ((1.000000, 0.000000, 0.000000), (0.000000, 1.000000, 0.000000), (0.000000, 0.000000, 1.000000)), (0.02320, 0.02250, 0.03200)),
    ("rib_a", (-0.10660, 0.12350, 0.02250), ((0.862618, 0.505856, 0.000000), (-0.505856, 0.862618, 0.000000), (0.000000, 0.000000, 1.000000)), (0.01878, 0.00300, 0.02250)),
    ("rib_b", (-0.07420, 0.10450, 0.01425), ((0.862618, 0.505856, 0.000000), (-0.505856, 0.862618, 0.000000), (0.000000, 0.000000, 1.000000)), (0.01878, 0.00300, 0.01425)),
    ("column", (-0.14750, 0.14750, 0.28450), ((1.000000, 0.000000, 0.000000), (0.000000, 1.000000, 0.000000), (0.000000, 0.000000, 1.000000)), (0.01500, 0.01500, 0.28050)),
    ("block", (-0.14550, 0.14950, 0.56500), ((1.000000, 0.000000, 0.000000), (0.000000, 1.000000, 0.000000), (0.000000, 0.000000, 1.000000)), (0.02320, 0.02320, 0.04500)),
    ("head", (-0.10633, 0.10296, 0.59546), ((-0.361047, -0.882677, 0.300877), (-0.932548, 0.341738, -0.116488), (0.000000, -0.322639, -0.946522)), (0.01852, 0.01805, 0.02192)),
    ("lens", (-0.10030, 0.10012, 0.57595), ((-0.361047, -0.882677, 0.300877), (-0.932548, 0.341738, -0.116488), (0.000000, -0.322639, -0.946522)), (0.00850, 0.00850, 0.00550)),
    ("bridge", (-0.11845, 0.11845, 0.59500), ((0.707107, 0.707107, 0.000000), (-0.707107, 0.707107, 0.000000), (0.000000, 0.000000, 1.000000)), (0.01609, 0.01500, 0.01500)),
)
TOWER_LEAN_AT_LENS_M = 0.0175      # undetected rigid lean at the lens
TOWER_AXIS_M = 0.0012              # column axis
TOWER_BUILD_M = 0.0005             # printed-part tolerance
TOWER_TOP_ROTATION_M = 0.0026      # block, head, lens, bridge turned within the orientation gate
TOWER_SAMPLE_M = 0.0015            # half of the <= 3 mm travel per sample (the dense count)
TOWER_RESIDUAL_DRIVER_M = 0.0260   # nominal kinematics vs the tower at its contract place (the uncalibrated V1)
TOWER_TOP_PARTS = ("block", "head", "lens", "bridge")
TOWER_T1_PARTS = ("block", "head", "lens", "bridge", "column", "socket")
# the V1's entries of the contract's per-arm droop tables (joints 2..4) and its link masses
# ((guard frame 1..5, kg, CoM in that frame); the claw lumped on frame 5)
TOWER_DROOP_DESIGN_ENV_V1_RAD = (0.027, 0.025, 0.010)
TOWER_DROOP_KAPPA_V1 = 2.6
TOWER_DROOP_TAU_AC_V1_NM = (1.2732, 0.9747, 0.1169)
TOWER_DROOP_LINK_MASSES_V1 = ((1, 0.060, (0.0014, 0.0, 0.0276)), (2, 0.190, (0.1281, -0.0115, -0.0002)), (3, 0.150, (0.0, 0.1827, 0.0011)), (4, 0.085, (0.0122, 0.0510, 0.0)), (5, 0.140, (0.0, 0.0, 0.045)))

# MIRROR of fixture_guard: R_J_M['v1'] (proven bounds of any arm-box corner's
# distance from joint j's axis), TILT_SLACK, GRAVITY_M_S2, DROOP_SUM_LIMIT.
TOWER_R_J_M = (0.589, 0.590, 0.432, 0.209, 0.114)
TOWER_TILT_SLACK = 0.03
TOWER_GRAVITY_M_S2 = 9.81
TOWER_DROOP_SUM_LIMIT = 0.5

# MIRROR of arm_cells.ARM_CELLS['v1'] (generated from the shipped meshes): the
# housing fixed to link 5 and both blades, each in its own frame, level 1
# (``box``) and level 2 (``cells``).
V1_ARM_CELLS = {
    'pad_m': 0.0018,
    'link_boxes': None,
    'housing': {
        'box': ((-0.023300000730156898, -0.02104999989569187, -0.00629999701231718), (0.06952392243146896, 0.027550000029802322, 0.04430038042664528)),
        'cells': (
            ((-0.022799999716877937, -0.02104999989569187, -0.00629999701231718), (0.021376460123062133, 0.00043372335676103825, 0.014239197860658168)),
            ((-0.02040000379383564, -0.02104999989569187, 0.007391084653139115), (0.010250000546872615, 0.00013072279579937453, 0.03227980926930905)),
            ((-0.016409089121222496, -0.02104999989569187, 0.02045000038743019), (0.010250000546872615, -0.001259492912143469, 0.03530000083446503)),
            ((-0.022799999716877937, -0.017643840315937995, -0.00629999701231718), (0.0157999995008111, 0.027550000029802322, 0.013800001035630702)),
            ((-0.023300000730156898, -0.008111075296252965, -0.0017999989613970186), (-0.0031000023864209652, 0.01593272228091955, 0.033150001692771915)),
            ((-0.022799999716877937, -0.01766787588894367, 0.01994999937415123), (0.010258823896944522, 0.025661659690737724, 0.03530000083446503)),
            ((-0.022799999716877937, -0.005537308550626039, -0.0017999989613970186), (0.021300000402331352, 0.027550000029802322, 0.02055000260770321)),
            ((-0.023300000730156898, 0.0036499940015375615, -0.0017999989613970186), (0.01024152458012104, 0.027550000029802322, 0.0335491814494133)),
            ((-0.022799999716877937, -0.005544834335520863, 0.02020000174343586), (0.010258823896944522, 0.027550000029802322, 0.03530000083446503)),
            ((0.00042499971278011804, -0.02104999989569187, -0.004214282876998186), (0.04124649174809456, 0.01630000051409006, 0.014050001542270183)),
            ((0.006634970863163472, -0.02104999989569187, 0.008762179610133172), (0.04042864524126053, 0.013737499977648258, 0.0323812917649746)),
            ((0.006626771499216557, -0.020977477806806564, 0.01948000184893608), (0.040798506963253024, -3.913751970976596e-05, 0.04080038078427315)),
            ((0.00042499971278011804, -0.017621609646081924, -0.0042999969173222775), (0.04135831378698349, 0.027550000029802322, 0.014564721177518367)),
            ((0.0126000006839633, -0.01780000075995922, 0.0027000026002526284), (0.03961014680862427, 0.027550000029802322, 0.03440000050067902)),
            ((0.006650000546872616, -0.017621630135178565, 0.020533335131406785), (0.044549553918838504, 0.027550000029802322, 0.0430568710744381)),
            ((0.00042499971278011804, -0.005545362861081957, -0.0017999989613967965), (0.04135831378698349, 0.027550000029802322, 0.01580000136345625)),
            ((0.006636250500380993, -0.003777303996682167, -0.0017999989613967965), (0.04179685868620873, 0.027550000029802322, 0.03341142393946648)),
            ((0.0066189521148800855, -0.005543819892406463, 0.02070000275671482), (0.021530769097805023, 0.027550000029802322, 0.03530000083446503)),
            ((0.03518102757334709, -0.01780000075995922, 0.0027140767470002175), (0.05594276942610741, 0.01623997584283352, 0.015306996445357799)),
            ((0.03391110591292381, -0.01780000075995922, 0.003700001832842827), (0.06952392243146896, 0.014874999675154685, 0.03288741343021393)),
            ((0.03488960928916931, -0.01780000075995922, 0.020510027852654458), (0.06602392278909683, 0.013737499977648258, 0.04327525504231453)),
            ((0.034699999463558194, -0.017081129628419876, 0.0026573736377060414), (0.04514123432636261, 0.017099676917493343, 0.014862180019915103)),
            ((0.03391110591292381, -0.01780000075995922, 0.006932052519917489), (0.06952392243146896, 0.014998872096836566, 0.03322029792070389)),
            ((0.032722313624620435, -0.01780000075995922, 0.020508854386210442), (0.06602392278909683, 0.013737499977648258, 0.04430038042664528)),
            ((0.03490550137758255, 0.008199999776482583, 0.0027246323570609093), (0.05594276942610741, 0.01780000075995922, 0.015306996445357799)),
            ((0.03391110591292381, 0.006609779518842698, 0.007277155962586403), (0.06952392243146896, 0.01780000075995922, 0.029747820723056793)),
            ((0.059891869050264356, 0.0083500004529953, 0.02433353356719017), (0.06602392278909683, 0.01780000075995922, 0.029747820723056793)),
        ),
    },
    'blades': (
        {
            'name': 'right_finger', 'joint': 'RL_joint',
            'origin': ((-3.673205103346574e-06, -3.6732051033217936e-06, 0.9999999999865075, 0.020197), (-0.9999999999932537, 1.3492435731251315e-11, -3.6732051033217936e-06, -0.00925), (0.0, -0.9999999999932537, -3.673205103346574e-06, 0.02125)),
            'axis': (0.0, 0.0, 1.0), 'multiplier': 1.0, 'gear_slack': 0.0,
            'r_max_m': 0.0915558703600325,
            'box': ((-0.011776294822990894, -0.08914530210494995, -0.05053194545507431), (0.014879836033284664, 0.01179100785702467, 0.0067999998882412906)),
            'cells': (
                ((-0.01089256748855114, -0.08914530210494995, -0.03098073534965515), (0.0040171342279762025, -0.0204296379506588, -0.009557799172401429)),
                ((-0.01013074189722538, -0.07634090565443039, -0.0152562524035573), (0.001267642861418426, -0.05147127501368522, -0.009486422610282898)),
                ((-0.010430640804767608, -0.04101632841229439, -0.04556844316720963), (0.005369685155525803, -0.019329362285137177, -0.028814977702498436)),
                ((-0.010450456555187702, -0.07818980448246002, -0.03304238178133965), (0.006311069040745496, -0.019850195121765137, -0.00735499683469534)),
                ((-0.010447809736430645, -0.07634090565443039, -0.020954999405145645), (0.00646516800224781, -0.01853544145524502, 0.003871067923679948)),
                ((-0.011776294822990894, -0.025667838084697723, -0.04849737443327904), (0.0006988385302945971, 0.011486866775155067, -0.03624737329483032)),
                ((-0.010450456555187702, -0.024285949099063873, -0.03304238178133965), (0.01093461111485958, -0.019644518119096756, -0.007355001491308213)),
                ((-0.01175296612381935, -0.026910308080911636, -0.01707078077197075), (0.004040871777758002, 0.01143353645056486, 0.0067999998882412906)),
                ((-0.009170578590780496, -0.08246574484109878, -0.031330061408877376), (0.008210554982721805, -0.04856962032318115, -0.009191437360644341)),
                ((-0.009156784306466579, -0.07548799299001693, -0.015177767987549304), (0.00821268864274025, -0.049906131547689435, -0.009327783916890622)),
                ((-0.009412868677824735, -0.03985079311132431, -0.04681207172870636), (0.011510117243230342, -0.019644287151098252, -0.028917261135578156)),
                ((-0.009412870540469884, -0.05767081983685494, -0.03304237805604935), (0.011510117243230342, -0.020905011069774628, -0.00735499683469534)),
                ((-0.010424006994068622, -0.05730170317292214, -0.020954999405145645), (0.011510056707262992, -0.019362765100598336, 0.005114696019515395)),
                ((-0.010430640804767608, -0.02503191799223423, -0.050322118479013446), (0.011053752417862415, 0.01179100785702467, -0.029442376193404198)),
                ((-0.010430640804767608, -0.023450195121765136, -0.03304238178133965), (0.01093461111485958, -0.019644287151098252, -0.007355002422630787)),
                ((-0.010424006994068622, -0.024526815193891525, -0.010955002422630786), (0.011256070140004158, 0.011784433650970459, 0.0067999998882412906)),
                ((0.0028377037651836873, -0.06439117275476455, -0.030886368158459663), (0.009247564508765935, -0.05300758100748062, -0.009725796726346016)),
                ((0.0028377014368772507, -0.06100534580945969, -0.013706956322491168), (0.0092418680742383, -0.053007562381029126, -0.009656744745373726)),
                ((0.0028186549589037896, -0.03976689957380295, -0.04556844316720963), (0.014879836033284664, -0.017899562340974808, -0.028920628798007966)),
                ((0.002711069040745497, -0.057255736815929416, -0.03304237805604935), (0.014879836033284664, -0.02044196679890156, -0.007354995903372765)),
                ((0.002711069040745497, -0.057255736815929416, -0.020954999405145645), (0.014879774565994739, -0.018693492484092713, 0.003871067923679948)),
                ((-0.0014396445494610816, -0.02719769560098648, -0.05053194545507431), (0.012311467233300209, 0.010147590453922748, -0.02944237060546875)),
                ((0.007312881496548653, -0.02404198542535305, -0.0330423836439848), (0.01334329627752304, -0.019644287151098252, -0.007355002422630787)),
                ((-0.0015869799844920635, -0.027197749617695808, -0.010955002422630786), (0.012311409491300582, 0.00957903152629733, 0.0067999998882412906)),
            ),
        },
        {
            'name': 'left_finger', 'joint': 'LF_joint',
            'origin': ((-3.673205103346574e-06, -3.6732051033217936e-06, 0.9999999999865075, 0.02), (-0.9999999999932537, 1.3492435731251315e-11, -3.6732051033217936e-06, 0.00925), (0.0, -0.9999999999932537, -3.673205103346574e-06, 0.02125)),
            'axis': (0.0, 0.0, 1.0), 'multiplier': -0.947368, 'gear_slack': 0.05263200000000001,
            'r_max_m': 0.09141640302023422,
            'box': ((-0.014822824190557003, -0.08912500672340393, -0.04830000110268593), (0.012288671250641345, 0.012218329387903213, 0.006997375547140836)),
            'cells': (
                ((-0.009123591038584709, -0.06440199844837188, -0.030543255883455276), (-0.0027151435770094395, -0.05301777340173721, -0.009377766136825085)),
                ((-0.009123591038584709, -0.06101906977891922, -0.013342932130396366), (-0.002715145905315876, -0.0530177920281887, -0.00931363247036934)),
                ((-0.014822762723267078, -0.0533628531575203, -0.04537106611132622), (-0.002638916686922312, -0.018712135699391366, -0.027272001576423646)),
                ((-0.014822824190557003, -0.057272210049629214, -0.03284500472545624), (-0.002638916686922312, -0.02046768806576729, -0.007157622572779656)),
                ((-0.014822824190557003, -0.05725448511838913, -0.012977766136825084), (-0.0027103179290890694, -0.01791818320453167, 0.004068443582579493)),
                ((-0.010973647500574588, -0.025050864818692207, -0.04830000110268593), (0.0004718908185139298, 0.007846792957931756, -0.02924499727487564)),
                ((-0.013293483558297157, -0.02406770669221878, -0.03284499913752079), (-0.006784006689488888, -0.019481102672219277, -0.007157616984844208)),
                ((-0.012221296581625938, -0.026293491277098655, -0.010757629092037677), (0.0013690996875055134, 0.009233678023517131, 0.006997375547140836)),
                ((-0.008081792651861906, -0.08245703511238098, -0.03100856326818466), (0.009283189459890127, -0.0485530204296112, -0.008999949988722802)),
                ((-0.007845403890311718, -0.06833261184692382, -0.014052029031515121), (0.009283190391212701, -0.048553012979030606, -0.008869938288629056)),
                ((-0.011450371968746185, -0.0520711795270443, -0.046614694672822955), (0.010472439493238925, -0.019481102672219277, -0.027407801446318627)),
                ((-0.01145043343603611, -0.057680345404148105, -0.03284500286281109), (0.009472910578548908, -0.02091745912730694, -0.00715762536674738)),
                ((-0.01145043343603611, -0.057680345404148105, -0.01301674757450819), (0.009472908715903759, -0.019547515285015107, 0.00531207167841494)),
                ((-0.010384006689488887, -0.024608264940977096, -0.04830000110268593), (0.010472439493238925, 0.00929394902959466, -0.02924499727487564)),
                ((-0.010535137991607189, -0.023430842238664627, -0.03284499913752079), (0.010479081685841083, -0.019481102672219277, -0.0071576188474893575)),
                ((-0.010535137991607189, -0.024616043347120285, -0.010757624435424804), (0.010479081685841083, 0.012218329387903213, 0.006997375547140836)),
                ((-0.0024978732417337596, -0.08912500672340393, -0.030713578018546104), (0.011063334795832633, -0.02041023850142956, -0.009219264347851277)),
                ((-0.003967745726183056, -0.0691944354057312, -0.014126818890869617), (0.01022163898050785, -0.05214424125552177, -0.009219264347851277)),
                ((-0.006238916686922311, -0.060469988065958026, -0.04537106611132622), (0.010497216399013996, -0.01852056264579296, -0.026560920026898385)),
                ((-0.006238916686922311, -0.07817151330709457, -0.03284500286281109), (0.010500191974639892, -0.019827318114042283, -0.0071576188474893575)),
                ((-0.005335418072715401, -0.06134965204000473, -0.013372749353945255), (0.010479081685841083, -0.01852056264579296, 0.00531207167841494)),
                ((-0.001369099658401683, -0.025653246122598648, -0.04830000110268593), (0.009293949495255947, 0.009039038124680519, -0.038235534650087354)),
                ((-0.010384006689488887, -0.02426655523777008, -0.03284499913752079), (0.010500191974639892, -0.019481102672219277, -0.0071576188474893575)),
                ((-0.003674671666882932, -0.025653246122598648, -0.004102624285221099), (0.012288671250641345, 0.011151858869194984, 0.006997375547140836)),
            ),
        },
    ),
}

# The glide's dense count: at most this much box-corner travel between samples.
TOWER_DENSE_STEP_M = 2.0 * TOWER_SAMPLE_M
# The claw is judged in bands of this width (the method spec 40's acceptance
# set was measured with): the blades of every sample are the box of the claw
# band the sample lies in, grown over that band, so the claw's own turn
# between two samples stays inside the box.
TOWER_CLAW_BAND_RAD = 0.1
# The lean per metre of height (0.0304).
_TOWER_LEAN_PER_M = TOWER_LEAN_AT_LENS_M / LENS_F[2]
# Joints 2..4 turn about this axis of their own link frame (parallel on the V1).
_TOWER_DROOP_AXIS_LOCAL = (0.0, 0.0, -1.0)
# The droop joints (indices into joints 2, 3, 4) distal to each box group; a
# blade is distal like the housing.
_TOWER_DISTAL = {'link1': (), 'link2': (0,), 'link3': (0, 1), 'link4': (0, 1, 2), 'housing': (0, 1, 2)}
_TOWER_LINK_GROUPS = ('link1', 'link2', 'link3', 'link4')

TOWER_CLEAR = 'clear'
TOWER_REFUSED = 'refused'
TOWER_UNJUDGEABLE = 'unjudgeable'
# The one robot profile the tower check knows; under the tower every other one
# is unjudgeable.
TOWER_PROFILE = 'v1_studio'


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _box_corners(lo, hi):
    """The 8 corners of ``(lo, hi)`` in bit order (x, y, z) = (i>>2, i>>1, i) & 1."""
    return tuple((hi[0] if bx else lo[0], hi[1] if by else lo[1], hi[2] if bz else lo[2])
                 for bx, by, bz in _CORNER_BITS)


def _columns(rot):
    return ((rot[0][0], rot[1][0], rot[2][0]), (rot[0][1], rot[1][1], rot[2][1]),
            (rot[0][2], rot[1][2], rot[2][2]))


def _projection(points, axis):
    a0, a1, a2 = axis
    values = [x * a0 + y * a1 + z * a2 for x, y, z in points]
    return min(values), max(values)


class _TowerPart:
    """One tower box at its contract place and what the rule reads of it."""

    __slots__ = ('name', 'axes', 'corners', 'z_top', 'own_lo', 'own_hi', 'r_sample', 'group')

    def __init__(self, name, centre, rows, half):
        self.name = name
        self.axes = _columns(rows)          # the box axes are the COLUMNS of R (used as given, like the server)
        self.corners = tuple(
            tuple(centre[i] + sum((2 * b[k] - 1) * half[k] * self.axes[k][i] for k in range(3)) for i in range(3))
            for b in _CORNER_BITS)
        self.z_top = max(c[2] for c in self.corners)
        extents = [_projection(self.corners, axis) for axis in self.axes]
        self.own_lo = tuple(lo for lo, _hi in extents)
        self.own_hi = tuple(hi for _lo, hi in extents)
        rotation = TOWER_TOP_ROTATION_M if name in TOWER_TOP_PARTS else 0.0
        self.r_sample = TOWER_AXIS_M + TOWER_BUILD_M + TOWER_SAMPLE_M + TOWER_RESIDUAL_DRIVER_M + rotation
        self.group = None          # its set of axes in _TOWER_AXES_SETS

    def allowance(self, z_box, delta):
        """``R + c · min(z_top, (max(z_box, 0) + R + δ) / (1 − c))``: a tower point that can touch the box after
        the undetected lean stands at most ``R + δ`` above the box's top. Never smaller for a higher box or a
        larger droop."""
        r = self.r_sample
        return r + _TOWER_LEAN_PER_M * min(self.z_top, (max(z_box, 0.0) + r + delta) / (1.0 - _TOWER_LEAN_PER_M))


_TOWER_PARTS = tuple(_TowerPart(name, centre, rows, half)
                     for name, centre, rows, half in TOWER_OBB_F if name in TOWER_T1_PARTS)
# The parts that share their axes (socket, column and block; head and lens) are
# projected on once per box; with each set its axes' lengths (the contract's
# rows are written to six decimals).
_TOWER_AXES_SETS = []
for _part in _TOWER_PARTS:
    if _part.axes not in _TOWER_AXES_SETS:
        _TOWER_AXES_SETS.append(_part.axes)
    _part.group = _TOWER_AXES_SETS.index(_part.axes)
_TOWER_AXES_SETS = tuple(_TOWER_AXES_SETS)
_TOWER_AXES_NORMS = tuple(tuple(math.sqrt(_dot(a, a)) for a in axes) for axes in _TOWER_AXES_SETS)
del _part


class _ArmBox:
    """One arm box in its link's frame: ``link`` 1..5, its 8 corners and 3 axes there, its group, and the sphere
    around it (its corners' centre and their largest distance from it)."""

    __slots__ = ('link', 'corners', 'axes', 'group', 'centre', 'radius')

    def __init__(self, link, corners, axes, group):
        self.link = link
        self.corners = corners
        self.axes = axes
        self.group = group
        self.centre = tuple(sum(c[i] for c in corners) / 8.0 for i in range(3))
        self.radius = max(math.sqrt(sum((c[i] - self.centre[i]) ** 2 for i in range(3))) for c in corners)


_IDENT_AXES = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
_TOWER_LINK_BOXES = tuple(_ArmBox(k + 1, _box_corners(*V1_LINK_BOXES[k + 1]), _IDENT_AXES, f'link{k + 1}')
                          for k in range(4))
_TOWER_HOUSING = _ArmBox(5, _box_corners(*V1_ARM_CELLS['housing']['box']), _IDENT_AXES, 'housing')
_TOWER_HOUSING_CELLS = tuple(_ArmBox(5, _box_corners(lo, hi), _IDENT_AXES, 'housing')
                             for lo, hi in V1_ARM_CELLS['housing']['cells'])


def _blade_box(blade, rot, pos, grow, lo, hi):
    lo = tuple(v - grow for v in lo)
    hi = tuple(v + grow for v in hi)
    corners = []
    for c in _box_corners(lo, hi):
        r = _apply(rot, c)
        corners.append((r[0] + pos[0], r[1] + pos[1], r[2] + pos[2]))
    return _ArmBox(5, tuple(corners), _columns(rot), blade['name'])


def _blade_boxes(g_lo, g_hi):
    """Both blades for the claw interval ``[g_lo, g_hi]``, level 1 (one box per blade) and level 2 (its cells),
    in link 5's frame: each blade's box at the interval's middle angle, grown by ``r_max`` times half the interval
    and, for the mimic blade, by its gear slack (``fixture_guard.arm_boxes``)."""
    mid, half = 0.5 * (g_lo + g_hi), 0.5 * (g_hi - g_lo)
    level1, level2 = [], []
    for blade in V1_ARM_CELLS['blades']:
        origin = blade['origin']
        rot = _mat_mul(tuple(tuple(row[:3]) for row in origin),
                       _axis_rot(blade['axis'], blade['multiplier'] * mid))
        pos = tuple(row[3] for row in origin)
        grow = blade['r_max_m'] * half + blade['r_max_m'] * blade['gear_slack'] * (abs(mid) + half)
        level1.append(_blade_box(blade, rot, pos, grow, *blade['box']))
        level2.extend(_blade_box(blade, rot, pos, grow, lo, hi) for lo, hi in blade['cells'])
    return tuple(level1), tuple(level2)


_BLADE_CACHE = {}


def _blades_for(g_lo, g_hi):
    key = (g_lo, g_hi)
    boxes = _BLADE_CACHE.get(key)
    if boxes is None:
        if len(_BLADE_CACHE) > 64:
            _BLADE_CACHE.clear()
        boxes = _BLADE_CACHE[key] = _blade_boxes(g_lo, g_hi)
    return boxes


def _placed(box, frame):
    """``(corners, axes)`` of an arm box in W for its link's frame ``(rot, pos)``."""
    rot, pos = frame
    r0, r1, r2 = rot
    corners = tuple((r0[0] * x + r0[1] * y + r0[2] * z + pos[0],
                     r1[0] * x + r1[1] * y + r1[2] * z + pos[1],
                     r2[0] * x + r2[1] * y + r2[2] * z + pos[2]) for x, y, z in box.corners)
    return corners, tuple(_apply(rot, a) for a in box.axes)


def _axis_distance(point, origin, u):
    w0, w1, w2 = point[0] - origin[0], point[1] - origin[1], point[2] - origin[2]
    along = w0 * u[0] + w1 * u[1] + w2 * u[2]
    return math.sqrt(max(w0 * w0 + w1 * w1 + w2 * w2 - along * along, 0.0))


def _gap_dir(corners, axes, part, delta, u):
    """The separating-axis gap (the 3 + 3 face normals and the 9 edge cross products, ``arm_geometry``'s fifteen
    axes; an edge pair too close to parallel gives no axis) with the box's droop displacement (``|e| <= δ``,
    ``e`` ⟂ ``u``) subtracted per axis: ``max_n [sep_n − δ · min(1, √(1 − (n·u)²) + TILT_SLACK)]``
    (``fixture_guard.gap_dir``)."""
    candidates = list(axes) + list(part.axes)
    for a in axes:
        for b in part.axes:
            e = (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
            norm = math.sqrt(_dot(e, e))
            if norm > 1e-9:
                candidates.append((e[0] / norm, e[1] / norm, e[2] / norm))
    best = -math.inf
    for n in candidates:
        alo, ahi = _projection(corners, n)
        blo, bhi = _projection(part.corners, n)
        cu = abs(_dot(n, u))
        gap = (max(blo - ahi, alo - bhi)
               - delta * min(1.0, math.sqrt(max(0.0, 1.0 - cu * cu)) + TOWER_TILT_SLACK))
        if gap > best:
            best = gap
    return best


class _Sample:
    """One glide sample: the pose, its claw band, its droop, and — on demand — its level-1 boxes in W.

    Three values bound a (box, part) margin, each never above the next: the SPHERE bound (the box's sphere
    projected on the part's own face normals, the droop and the height taken at their largest over the sphere),
    the PRE-CHECK (the box's corners on those normals), and the margin itself (all fifteen axes; a housing or
    blade box below its allowance replaced by its cells). Every one of them is a lower bound of the margin, so a
    pair whose bound is not negative is not touched, and a refusal is always the margin's own negative value."""

    __slots__ = ('frames', 'boxes', 'cells', 'theta', 'joints', 'rest', 'u', 'spheres', 'placed', 'delta', 'z_box',
                 'extents')

    def __init__(self, q, g_lo, g_hi):
        frames = link_frames(q, V1)
        if frames is None:
            raise ValueError('pose not evaluable')
        # links 1..5 in W: the arm base on the plate, LIFT_M above the table
        self.frames = [(rot, (pos[0], pos[1], pos[2] + LIFT_M)) for rot, pos in frames[1:]]
        self.joints = []
        for j in range(3):
            rot, pos = self.frames[j + 1]
            self.joints.append((pos, _apply(rot, _TOWER_DROOP_AXIS_LOCAL)))
        self.theta = self._droop_angles()
        total = sum(self.theta)
        if not all(math.isfinite(t) for t in self.theta) or total >= TOWER_DROOP_SUM_LIMIT:
            raise ValueError('the droop bound does not hold')
        self.rest = 1.0 - total
        self.u = self.joints[0][1]
        blades, blade_cells = _blades_for(g_lo, g_hi)
        self.boxes = _TOWER_LINK_BOXES + (_TOWER_HOUSING,) + blades
        self.cells = _TOWER_HOUSING_CELLS + blade_cells
        self.spheres = self.placed = self.delta = self.z_box = self.extents = None

    def _droop_angles(self):
        """θ (joints 2..4): κ · env_j / τ_AC,j times the pose's absolute gravity torque sum
        ``g Σ_{l ≥ j} m_l |(u_j × (c_l − o_j))_z|`` (``fixture_guard.TowerGuard.droop_angles``)."""
        torque = [0.0, 0.0, 0.0]
        for f, mass, com in TOWER_DROOP_LINK_MASSES_V1:
            rot, pos = self.frames[f - 1]
            c = _apply(rot, com)
            for j in range(3):
                if f >= j + 2:
                    origin, u = self.joints[j]
                    w0, w1 = c[0] + pos[0] - origin[0], c[1] + pos[1] - origin[1]
                    torque[j] += TOWER_GRAVITY_M_S2 * mass * abs(u[0] * w1 - u[1] * w0)
        return tuple(TOWER_DROOP_KAPPA_V1 * TOWER_DROOP_DESIGN_ENV_V1_RAD[j] / TOWER_DROOP_TAU_AC_V1_NM[j]
                     * torque[j] for j in range(3))

    def _distal(self, box):
        return _TOWER_DISTAL.get(box.group, _TOWER_DISTAL['housing'])

    def sphere_bound(self, b, part):
        """The sphere bound of box ``b``'s margin against ``part``."""
        if self.spheres is None:
            self.spheres = []
            for box in self.boxes:
                rot, pos = self.frames[box.link - 1]
                c = _apply(rot, box.centre)
                c = (c[0] + pos[0], c[1] + pos[1], c[2] + pos[2])
                reach = sum((_axis_distance(c, *self.joints[j]) + box.radius) * self.theta[j]
                            for j in self._distal(box))
                self.spheres.append((c, box.radius, reach / self.rest,
                                     [[_dot(c, a) for a in axes] for axes in _TOWER_AXES_SETS]))
        c, radius, delta, along = self.spheres[b]
        g = part.group
        gap = max(max(part.own_lo[k] - along[g][k] - radius * _TOWER_AXES_NORMS[g][k],
                      along[g][k] - radius * _TOWER_AXES_NORMS[g][k] - part.own_hi[k]) for k in range(3))
        return gap - delta - part.allowance(c[2] + radius, delta)

    def place(self):
        if self.placed is not None:
            return
        self.placed = [_placed(box, self.frames[box.link - 1]) for box in self.boxes]
        self.delta = []
        for box, (corners, _axes) in zip(self.boxes, self.placed):
            # Σ_{j distal} θ_j · D_j / (1 − Σθ), D_j the box's farthest corner from joint j's axis
            total = 0.0
            for j in self._distal(box):
                (o0, o1, o2), (u0, u1, u2) = self.joints[j]
                far = 0.0
                for x, y, z in corners:
                    w0, w1, w2 = x - o0, y - o1, z - o2
                    along = w0 * u0 + w1 * u1 + w2 * u2
                    sq = w0 * w0 + w1 * w1 + w2 * w2 - along * along
                    if sq > far:
                        far = sq
                total += math.sqrt(far) * self.theta[j]
            self.delta.append(total / self.rest)
        self.z_box = [max(c[2] for c in corners) for corners, _axes in self.placed]
        self.extents = [[[_projection(corners, a) for a in axes] for axes in _TOWER_AXES_SETS]
                        for corners, _axes in self.placed]

    def lower(self, b, part):
        """The pre-check (``fixture_guard``'s, in the part's frame): the gap along the part's own three face normals
        never exceeds the fifteen-axis value, so ``own_gap − δ − allowance`` never exceeds the margin."""
        self.place()
        extents = self.extents[b][part.group]
        gap = max(max(part.own_lo[k] - extents[k][1], extents[k][0] - part.own_hi[k]) for k in range(3))
        delta = self.delta[b]
        return gap - delta - part.allowance(self.z_box[b], delta)

    def margin(self, b, part):
        """Box ``b``'s margin against ``part``: all fifteen axes; a housing or blade box below its allowance is
        replaced by its group's cells (level 2) with the level-1 box's droop."""
        self.place()
        corners, axes = self.placed[b]
        delta = self.delta[b]
        margin = _gap_dir(corners, axes, part, delta, self.u) - part.allowance(self.z_box[b], delta)
        group = self.boxes[b].group
        if margin < 0.0 and group not in _TOWER_LINK_GROUPS:
            margin = math.inf
            for cell in self.cells:
                if cell.group == group:
                    cc, ca = _placed(cell, self.frames[4])
                    m = _gap_dir(cc, ca, part, delta, self.u) - part.allowance(max(c[2] for c in cc), delta)
                    margin = min(margin, m)
        return margin


def tower_dense_count(a, b):
    """``n = ⌈Σ_j R_j |Δq_j| / 3 mm⌉`` (at least 1) for the arm joints ``a → b``: with ``R_j`` a proven bound of
    any box corner's distance from joint j's axis, no corner moves more than 3 mm between two samples."""
    travel = sum(r * abs(y - x) for r, x, y in zip(TOWER_R_J_M, a, b))
    return max(1, int(math.ceil(travel / TOWER_DENSE_STEP_M)))


def _claw_bands(c_start, c_end):
    """The claw intervals of the glide: the ``TOWER_CLAW_BAND_RAD`` grid clipped to its claw range (a claw that
    does not move is its own interval)."""
    c_lo, c_hi = min(c_start, c_end), max(c_start, c_end)
    if c_hi - c_lo <= 1e-12:
        return ((c_lo, c_hi),)
    bands = []
    k = int(math.floor(c_lo / TOWER_CLAW_BAND_RAD + 1e-9))
    while k * TOWER_CLAW_BAND_RAD < c_hi - 1e-12:
        lo = k * TOWER_CLAW_BAND_RAD
        hi = lo + TOWER_CLAW_BAND_RAD
        if hi > c_lo + 1e-12:
            bands.append((max(lo, c_lo), min(hi, c_hi)))
        k += 1
    return tuple(bands)


def _glide_samples(start, end, robot_type, window):
    """The glide's samples ``(arm joints, claw band)`` in order, or ``None`` when it cannot be judged."""
    if robot_type != TOWER_PROFILE:
        return None
    try:
        a = [float(v) for v in start]
        b = [float(v) for v in end]
        limits = [(float(lo), float(hi)) for lo, hi in window]
    except (TypeError, ValueError):
        return None
    nj = V1.n_joints
    if len(a) != nj + 1 or len(b) != nj + 1 or len(limits) != nj + 1:
        return None
    for q in (a, b):
        for v, (lo, hi) in zip(q, limits):
            if not (math.isfinite(v) and lo <= v <= hi):
                return None
    n = tower_dense_count(a[:nj], b[:nj])
    bands = _claw_bands(a[nj], b[nj])
    samples = []
    for i in range(n + 1):
        u = i / n
        q = [x + (y - x) * u for x, y in zip(a, b)]
        # a sample on a band edge is judged in both bands
        samples.extend((q[:nj], band) for band in bands if band[0] - 1e-9 <= q[nj] <= band[1] + 1e-9)
    return samples


def tower_glide_decision(start, end, robot_type, window, exact=False):
    """Pure: may the glide ``start → end`` be driven past the camera tower? (WP-T1; refuse-only.)

    ``start`` / ``end``: the arm's five joints and the claw, radians; the glide is the straight line between them
    (``build_boot_home``'s quintic only re-times it, claw included). ``robot_type``: the robot profile whose arm
    glides (only ``TOWER_PROFILE`` is known). ``window``: per joint, the claw included, the ``(lo, hi)`` of the
    servo's EEPROM window in radians.

    The rule is the dense one: every sample of the line (``tower_dense_count``) with the claw band it lies in,
    every arm box (links 1–4, the housing and both blades; the cells where a box is below its allowance) against
    every part of ``TOWER_T1_PARTS``, margin = the directional separating-axis gap − the part's allowance;
    refused when a margin is negative.

    Returns ``(verdict, margin_m, part)``, ``verdict`` one of ``TOWER_CLEAR``, ``TOWER_REFUSED``,
    ``TOWER_UNJUDGEABLE``. The guard (``exact=False``) stops at the first refusing sample and reports its smallest
    negative margin and that part; a clear glide reports ``(TOWER_CLEAR, None, None)``. ``exact=True``
    (diagnostics and tests) finds the glide's smallest margin and its part — the same verdict, more work.

    Unjudgeable, never passed: another robot type, a missing or non-finite value, a value outside its window, a
    pose whose droop bound does not hold, anything the geometry cannot evaluate."""
    try:
        samples = _glide_samples(start, end, robot_type, window)
        if samples is None:
            return TOWER_UNJUDGEABLE, None, None
        if exact:
            return _exact_glide(samples)
        for q, band in samples:
            sample = _Sample(q, *band)
            worst, worst_part = 0.0, None
            for part in _TOWER_PARTS:
                for b in range(len(sample.boxes)):
                    if sample.sphere_bound(b, part) >= 0.0 or sample.lower(b, part) >= 0.0:
                        continue
                    m = sample.margin(b, part)
                    if m < worst:
                        worst, worst_part = m, part.name
            if worst_part is not None:
                return TOWER_REFUSED, worst, worst_part
        return TOWER_CLEAR, None, None
    except (ArithmeticError, ValueError, TypeError):
        return TOWER_UNJUDGEABLE, None, None


def _exact_glide(samples):
    """the smallest margin of the glide: every (sample, box, part) in increasing order of its sphere bound, refined
    to the pre-check and then to the margin while the bound is below the smallest margin found."""
    placed = [_Sample(q, *band) for q, band in samples]
    order = sorted((sample.sphere_bound(b, part), s, b, p)
                   for s, sample in enumerate(placed)
                   for p, part in enumerate(_TOWER_PARTS)
                   for b in range(len(sample.boxes)))
    worst, worst_part = math.inf, None
    for bound, s, b, p in order:
        if bound >= worst:
            break
        part = _TOWER_PARTS[p]
        if placed[s].lower(b, part) < worst:
            m = placed[s].margin(b, part)
            if m < worst:
                worst, worst_part = m, part.name
    return (TOWER_REFUSED if worst < 0.0 else TOWER_CLEAR), worst, worst_part


# ══ where a limp V1 falls (52 §6) ════════════════════════════════════════════
#
# When torque goes off („Umgebung stoppen"), gravity starts the arm where joint
# 2's torque points: τ_2 > 0 toward larger joint 2 (forward, away from its
# back stop), τ_2 < 0 back — toward the tower. From the URDF's own inertials
# (no axis convention can flip the sign); below the threshold servo friction
# and joint 3 decide, so the answer is ``unknown`` (an ASSUMPTION closed by rig
# gate RG-FALL). Joint 1 is not judged: falling back is the danger whatever the
# azimuth.
#
# These masses are the URDF's (public/v1-urdf/v1.urdf, held equal by
# tests/test_feetech_tower_check.py); the tower check's droop above keeps the
# contract's TOWER_DROOP_LINK_MASSES_V1, the masses its τ_AC and κ were measured
# with. (guard frame 1..5, kg, CoM in that frame); frame 5 is link5 +
# end_effector + both blades at the open claw (0.90), lumped — the claw moves
# τ_2 by at most 0.005 N·m.
V1_LINK_MASSES = (
    (1, 0.253097683962784, (0.0014217215445681, 3.24703152565498e-06, 0.0275743625548369)),
    (2, 0.388301176905213, (0.12811481583066, -0.0115179757353191, -0.000207657247117798)),
    (3, 0.317446031504148, (2.59027387794486e-06, 0.182679465418246, 0.00113446328979427)),
    (4, 0.256065656080656, (0.0122049931515026, 0.0509912087404549, -2.39491178951612e-05)),
    (5, 0.30701261006065406, (0.0015755841586445984, 0.002980658262625324, 0.022707471784525)),
)
LIMP_FALL_THRESHOLD_NM = 0.15


def limp_gravity_torques(joints):
    """``(τ_2, τ_3, τ_4)`` in N·m: gravity's torque on joints 2..4 of a limp arm at ``joints``,
    ``τ_j = −g Σ_{l ≥ j} m_l (u_j × (c_l − o_j))_z`` (positive = toward a larger joint value); ``None`` when the
    pose cannot be evaluated."""
    frames = link_frames(joints, V1)
    if frames is None:
        return None
    out = []
    for j in (2, 3, 4):
        rot_j, origin = frames[j]
        u = _apply(rot_j, _TOWER_DROOP_AXIS_LOCAL)
        tau = 0.0
        for f, mass, com in V1_LINK_MASSES:
            if f >= j:
                rot, pos = frames[f]
                c = _apply(rot, com)
                w0, w1 = c[0] + pos[0] - origin[0], c[1] + pos[1] - origin[1]
                tau -= TOWER_GRAVITY_M_S2 * mass * (u[0] * w1 - u[1] * w0)
        out.append(tau)
    return tuple(out)


def limp_fall_direction(joints):
    """Where a limp arm at ``joints`` starts to fall: ``'forward'`` (τ_2 >= +LIMP_FALL_THRESHOLD_NM), ``'back'``
    (τ_2 <= −LIMP_FALL_THRESHOLD_NM) or ``'unknown'`` (between, or a pose that cannot be evaluated)."""
    try:
        torques = limp_gravity_torques(joints)
    except (TypeError, ValueError):
        torques = None
    if torques is None:
        return 'unknown'
    if torques[0] >= LIMP_FALL_THRESHOLD_NM:
        return 'forward'
    if torques[0] <= -LIMP_FALL_THRESHOLD_NM:
        return 'back'
    return 'unknown'

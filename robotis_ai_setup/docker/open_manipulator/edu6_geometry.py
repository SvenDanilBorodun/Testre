#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Whole-link geometry for the Feetech Roboter-Studio arms — the DRIVER's copy.

Serves BOTH ``edu6_studio`` (6 arm joints, 7 link boxes) and ``edu1_studio``
(5 arm joints, 6 link boxes) through the :data:`SPECS` table. Every public
function takes ``spec=EDU6`` by DEFAULT, so every pre-edu1 caller and every
pre-edu1 test is byte-identical.

WHY A SECOND COPY EXISTS. The server package has the same model in
``physical_ai_server/workflow/arm_geometry.py``, but the driver runs in the
``open_manipulator`` container and cannot import the server package. It needs
the model because ``start_boot_home`` is the HIGHEST-EXPOSURE home path in the
system: it runs on EVERY container boot, from whatever pose a LIMP arm collapsed
into — and the measured failing family (elbow essentially straight, shoulder
rotated back, gripper already near the table) is exactly what a limp arm
collapses into. Measured on the collision meshes, the straight glide to HOME
drives a link up to 167.9 mm BELOW the table from about 1 in 100 attainable
start poses.

The duplication is real and is guarded rather than wished away:
``robotis_ai_setup/tests/test_edu6_geometry.py`` AST-reads BOTH copies of the
box table and the kinematic constants and asserts they are equal, the same
discipline ``test_feetech_bus.py`` already applies to ``HOME_JOINTS_RAD``.

PURE PYTHON ON PURPOSE — no NumPy. The deps-free ``robotis_ai_setup/tests``
suite must be able to import and exercise this directly, and the arithmetic is
tiny (6-7 links x 8 corners per pose).

WHAT IT IS. One axis-aligned box per link, in that link's OWN frame. A
link-frame box CONTAINS its mesh, so ``lowest_z`` is a SOUND lower bound on the
true lowest point — pessimistic, never optimistic. Orientation-aware, which is
what a floor test needs; a direction-blind per-link RADIUS was measured and
refuses essentially everything.

z = 0 IS THE TABLE for both arms. Not a guess: ``base_link``'s own mesh spans
z ∈ [0.0000, 0.0625] m on the edu6 and [0.0000, 0.0481] m on the edu1, i.e. each
is bolted to the table. That is why this module can judge a boot glide with no
calibration at all.
"""

from __future__ import annotations

import math

# ── kinematic chain — MIRRORS physical_ai_server/workflow/edu6_ik.py ─────────
# (joint origin xyz, fixed rpy, rotation axis). A drift test AST-reads both.
_J1_XYZ = (-0.0212954796450086, 0.0, 0.0143999999999997)
_J2_XYZ = (0.0, 0.0177000000000036, 0.0784499962537874)
_J2_RPY = (1.57079632679495, 0.0, 3.14159265358979)
_J3_XYZ = (-0.0943599999999989, 0.037490003746198, 0.0)
_J4_XYZ = (0.0839500018730997, 0.0, -0.0176999999999974)
_J4_RPY = (0.0, 1.5707963267949, 0.0)
_J5_XYZ = (-0.0177000000000006, 0.0, 0.0442499999999998)
_J5_RPY = (0.0, 1.5707963267949, 0.0)
_J6_XYZ = (-0.0674499984948267, 0.0, 0.0176999999999994)
_J6_RPY = (0.0, 1.5707963267949, 0.0)
_AXES = ((0, 0, 1), (0, 0, -1), (0, 0, -1), (0, 0, -1), (0, 0, 1), (0, 0, 1))

# ── link boxes — MIRRORS arm_geometry.EDU6_LINK_BOXES ────────────────────────
# (min_xyz, max_xyz) in each link's OWN frame, metres; index 0 = base_link,
# 1..6 = link1..link6. Derived from the in-repo URDF + its STLs and rounded
# OUTWARD to 0.1 mm, so a constant can never be SMALLER than the mesh it stands
# for. Index 6 is the UNION over the gripper band [0, 1.79] of link6 +
# End_effector + both fingers, so it is valid at every jaw opening.
LINK_BOXES = (
    ((-0.0351, -0.0501, -0.0001), (+0.1328, +0.0500, +0.0626)),   # 0 base_link
    ((-0.0258, -0.0210, -0.0044), (+0.0210, +0.0210, +0.0885)),   # 1 link1
    ((-0.1097, -0.0153, -0.0492), (+0.0097, +0.0475, +0.0138)),   # 2 link2
    ((-0.0097, -0.0213, -0.0492), (+0.0893, +0.0353, +0.0138)),   # 3 link3
    ((-0.0210, -0.0153, -0.0031), (+0.0391, +0.0215, +0.0543)),   # 4 link4
    ((-0.0728, -0.0153, -0.0139), (+0.0097, +0.0353, +0.0492)),   # 5 link5
    ((-0.0400, -0.0353, -0.1050), (+0.0400, +0.0820, +0.0095)),   # 6 link6
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

_FIXED = (
    (_J1_XYZ, _IDENT),
    (_J2_XYZ, _rpy_matrix(*_J2_RPY)),
    (_J3_XYZ, _IDENT),
    (_J4_XYZ, _rpy_matrix(*_J4_RPY)),
    (_J5_XYZ, _rpy_matrix(*_J5_RPY)),
    (_J6_XYZ, _rpy_matrix(*_J6_RPY)),
)


# ── edu1_studio chain — MIRRORS physical_ai_server/workflow/edu1_ik.py ───────
# Same drift test as the edu6 pair above: test_edu6_geometry.py AST-reads BOTH
# copies. joint1 carries a fixed rpy here (the edu6's does not) and joint4
# carries none, so the _E1_FIXED table below is not a re-ordering of the edu6's.
_E1_J1_XYZ = (0.0, 0.0, 0.0492)
_E1_J1_RPY = (0.0, 0.0, -1.5708)
_E1_J2_XYZ = (0.0, 0.0, 0.04075)
_E1_J2_RPY = (-1.5708, 0.0, 1.5708)
_E1_J3_XYZ = (0.1555, -0.032, 0.0)
_E1_J3_RPY = (3.1416, 0.0, -1.5708)
_E1_J4_XYZ = (0.0, 0.222499999999999, 0.0)
_E1_J5_XYZ = (0.0, 0.0609499999999672, 0.0)
_E1_J5_RPY = (-1.5707963267949, 0.0, 0.0)
_E1_AXES = ((0, 0, -1), (0, 0, -1), (0, 0, -1), (0, 0, -1), (0, 0, -1))

# ── edu1 link boxes — MIRRORS arm_geometry.EDU1_LINK_BOXES ───────────────────
# Index 0 = base_link, 1..5 = link1..link5; index 5 is the UNION over the whole
# claw band [0, 1.57] of link5 + end_effector + both fingers, so it is valid at
# every jaw opening. base_link spans z in [0.0000, 0.0481] m — z = 0 IS the
# table for this arm too.
EDU1_LINK_BOXES = (
    ((-0.0708, -0.0751, +0.0000), (+0.0493, +0.0751, +0.0481)),   # 0 base_link
    ((-0.0200, -0.0153, -0.0055), (+0.0200, +0.0153, +0.0508)),   # 1 link1
    ((-0.0097, -0.0420, -0.0244), (+0.1708, +0.0150, +0.0244)),   # 2 link2
    ((-0.0150, -0.0096, -0.0244), (+0.0150, +0.2326, +0.0244)),   # 3 link3
    ((-0.0126, -0.0097, -0.0244), (+0.0351, +0.0595, +0.0244)),   # 4 link4
    ((-0.0279, -0.0744, -0.0045), (+0.0678, +0.0744, +0.0869)),   # 5 link5+claw
)

_E1_FIXED = (
    (_E1_J1_XYZ, _rpy_matrix(*_E1_J1_RPY)),
    (_E1_J2_XYZ, _rpy_matrix(*_E1_J2_RPY)),
    (_E1_J3_XYZ, _rpy_matrix(*_E1_J3_RPY)),
    (_E1_J4_XYZ, _IDENT),
    (_E1_J5_XYZ, _rpy_matrix(*_E1_J5_RPY)),
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


EDU6 = _Spec(_FIXED, _AXES, LINK_BOXES)
EDU1 = _Spec(_E1_FIXED, _E1_AXES, EDU1_LINK_BOXES)

# EDUBOTICS_ROBOT_TYPE → spec. An unknown/absent id resolves to EDU6, which is
# what every pre-edu1 caller already got; the DRIVER passes its spec explicitly
# rather than reading the env here, so this module has no environment coupling
# and stays a pure function of its arguments.
SPECS = {'edu6_studio': EDU6, 'edu1_studio': EDU1}


def spec_for(robot_type):
    """Spec for an ``EDUBOTICS_ROBOT_TYPE`` value; unknown/absent → EDU6."""
    return SPECS.get((robot_type or '').strip(), EDU6)


def link_frames(joints, spec=None):
    """``[(rot 3x3, pos 3)]`` per link in the WORLD frame: index 0 = base_link,
    then one per arm joint. ``None`` for fewer joints than the spec has."""
    spec = EDU6 if spec is None else spec
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
    spec = EDU6 if spec is None else spec
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
    spec = EDU6 if spec is None else spec
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

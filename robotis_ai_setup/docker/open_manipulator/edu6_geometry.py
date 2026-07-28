#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Whole-link geometry for the edu6 arm — the DRIVER's copy.

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
tiny (7 links x 8 corners per pose).

WHAT IT IS. One axis-aligned box per link, in that link's OWN frame. A
link-frame box CONTAINS its mesh, so ``lowest_z`` is a SOUND lower bound on the
true lowest point — pessimistic, never optimistic. Orientation-aware, which is
what a floor test needs; a direction-blind per-link RADIUS was measured and
refuses essentially everything.

z = 0 IS THE TABLE for this arm. Not a guess: ``base_link``'s own mesh spans
z ∈ [0.0000, 0.0625] m, i.e. it is bolted to the table. That is why this module
can judge a boot glide with no calibration at all.
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


def link_frames(joints):
    """``[(rot 3x3, pos 3)]`` per link in the WORLD frame: index 0 = base_link,
    1..6 = link1..link6. ``None`` for fewer than 6 joints."""
    q = list(joints)
    if len(q) < 6:
        return None
    if not all(isinstance(v, (int, float)) and math.isfinite(float(v))
               for v in q[:6]):
        return None
    rot, pos = _IDENT, (0.0, 0.0, 0.0)
    out = [(_RZ_PI, (0.0, 0.0, 0.0))]
    for i in range(6):
        offset, fixed = _FIXED[i]
        pos = tuple(pos[k] + _apply(rot, offset)[k] for k in range(3))
        rot = _mat_mul(_mat_mul(rot, fixed), _axis_rot(_AXES[i], float(q[i])))
        out.append((_mat_mul(_RZ_PI, rot), _apply(_RZ_PI, pos)))
    return out


def lowest_z(joints):
    """Sound lower bound on the lowest point of any MOVING link, in metres above
    the mounting plane. ``None`` when the pose cannot be evaluated.

    LINK 0 (base_link) is EXCLUDED: it is the fixed mount and its underside IS
    the table, so including it would peg every reading at ~0 and the check would
    judge nothing.
    """
    frames = link_frames(joints)
    if frames is None:
        return None
    worst = None
    for idx in range(1, len(LINK_BOXES)):
        rot, pos = frames[idx]
        lo, hi = LINK_BOXES[idx]
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


def swept_lowest_z(q_start, q_end):
    """:func:`lowest_z` minimised over the straight joint-space line — the line
    ``build_boot_home``'s quintic blend reparametrises, so it is exactly the set
    of poses the glide passes through. ``None`` if any sample is unevaluable."""
    a = [float(v) for v in list(q_start)[:6]]
    b = [float(v) for v in list(q_end)[:6]]
    if len(a) < 6 or len(b) < 6:
        return None
    delta = max(abs(b[i] - a[i]) for i in range(6))
    n = min(_MAX_SAMPLES,
            max(1, int(math.ceil(_R_MAX_M * delta / _STEP_M))))
    worst = None
    for i in range(n + 1):
        u = i / n
        value = lowest_z([a[k] + (b[k] - a[k]) * u for k in range(6)])
        if value is None:
            return None
        if worst is None or value < worst:
            worst = value
    return worst

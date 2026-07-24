#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Closed-form analytical inverse kinematics for the edu6_studio arm.

The edu6 ("EduBotics 6-Achs") is a 6-DOF Feetech STS3215 arm
(follower_arm_modified_final1.urdf):

* ``joint1`` — yaw about base z (±90°)
* ``joint2/3`` — shoulder / elbow, parallel axes (the 2R planar chain)
* ``joint4`` — forearm roll (±178°, see the SEAM note below)
* ``joint5`` — wrist pitch (**asymmetric −90°…+110°** — the relieved housing;
  the working-side vertical grasp uses the POSITIVE, relieved direction)
* ``joint6`` — tool roll (±178°; the jaw-alignment joint, see SEAM below)
* (``end_gear_joint`` is the gripper servo, not a positioning DOF)

Same design rules as the OMX ``IKSolver`` (see its module docstring): pure
NumPy, deterministic, closed form, verified by exact FK. The wrist axes 4/5/6
of this URDF intersect EXACTLY (CAD-cleaned; measured spread 0.0 mm), so the
Pieper decomposition is exact — no idealisation, no Newton refinement, no
seed-dependent branch: ``solve()`` computes the strict-vertical (top-down)
configuration in one pass, elbow-up preferred, with the wrist bend always
``q5 = π/2 − q2 − q3`` at ``q4 = 0`` (the q4=π flip maps q5 → −q5, which the
asymmetric limit can never rescue, so it is deliberately not searched).

FRAMES — the one thing to keep straight:

* The URDF's reachable half-disc lies on **−x**. The solver therefore works in
  the **WORLD frame = URDF base_link rotated 180° about Z** (front = +x, like
  the OMX), so every Roboter Studio convention (board placement, destinations,
  zones, German copy) carries over unchanged.
* ``solve()`` takes WORLD coordinates and returns **URDF-native joints** (what
  ``/joint_states``, the trajectory rail and the web twin speak).  ``fk()``
  takes URDF-native joints and returns the WORLD-frame TCP pose.
* The TCP is the **FINGERTIP** (``_L_TOOL`` = 0.1724 m from the wrist centre
  along the tool axis) — the touch-off reads ≈0 at the table, and a grasp
  targets ``z_table + object_height − grasp_depth`` exactly like the OMX.

ROLL contract (jaw alignment): motion's shared formula
``roll = base_yaw − tag_yaw + GRASP_ROLL_RAD`` is consumed here as
``q6 = wrap(π − roll)``.  Derivation: the jaws separate along link6-x, whose
world azimuth at a vertical pose is ``χ = q1 + q6 − π/2``; with the default
``GRASP_ROLL_RAD = π/2`` (same 90° default as the OMX) this yields χ ≡ tag_yaw
— the identical geometric behaviour, absorbed entirely by this one mapping, so
``EDUBOTICS_GRASP_ROLL_DEG`` keeps one fleet-wide default.  (Note the trim
direction flips vs the OMX: ∂q6/∂GRASP_ROLL = −1.)

ENCODER SEAM — why the limits are ±178° and not the URDF's ±180°.
The STS servos carry a SINGLE-TURN absolute encoder: 4096 ticks over one
revolution, so tick 4095 and tick 0 are 0.088° apart physically but 4095 ticks
apart numerically.  A ±180° band spans exactly one revolution, which means its
two ends are the SAME physical measurement — +180° and −180° are
indistinguishable, and one tick of overshoot (or a nudge of the limp wrist
during hand-guiding) reports a 360° jump that the planner then executes as a
full-circle sweep.  A 360° span needs 4097 distinguishable positions from a
4096-position sensor; it cannot be represented.  So every band is pulled 2° in,
leaving ~23 ticks of guard at each end (``test_feetech_bus`` pins the
clearance; the three JOINT_LIMITS copies must move together).

This matters most for ``q6``, because ``wrap(π − roll)`` lands on −180° for the
DEFAULT ``roll = 0`` — i.e. for every position-only solve (``in_workspace``,
``solve_quat``, the reach precheck) — and for one ordinary tag orientation at
every placement.  The BOUNDARY UNWIND in :meth:`solve` spends the parallel
jaw's 180° symmetry (a jaw grasps identically at ``q6`` and ``q6 ± π``) to pull
exactly those back into the band, leaving the live 1:1 tag tracking
(∂q6/∂tag_yaw = +1) untouched everywhere else.  Because the band is 356° wide
and the wrap output lies in [−180°, 180°), one 180° shift ALWAYS lands inside.

Geometry constants are transcribed from
``follower_arm_modified_final1.urdf`` (external CAD export; an in-repo copy
ships at ``physical_ai_manager/public/edu6-urdf/edu6.urdf`` — the independent
FK oracle in ``test_edu6_ik.py`` parses THAT file, so a solver-constant
transcription error cannot verify itself).
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np


# ── Kinematic constants (metres / radians) — URDF parent→child transcription ─
# joint origins (xyz) + fixed rpy; axes after the fixed rotation.
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
# joint rotation axes (in the post-rpy joint frame)
_AXES = ((0, 0, 1), (0, 0, -1), (0, 0, -1), (0, 0, -1), (0, 0, 1), (0, 0, 1))

# Joint-1 axis x offset in the URDF frame; +0.021295 in the WORLD frame.
_J1_X_OFFSET_URDF = _J1_XYZ[0]
BASE_AXIS_X_WORLD = -_J1_X_OFFSET_URDF

# Tool: the wrist-centre → FINGERTIP distance along the tool axis (−z of
# link6). link6's frame origin sits ON the tool axis (perp < 1e-15), exactly
# |_J6_XYZ[0]| from the wrist centre, so the FK composes
# TCP = link6_origin + (_L_TOOL − |_J6_XYZ[0]|)·tool.
_L_TOOL = 0.1724
_WRIST_TO_LINK6 = -_J6_XYZ[0]
_LINK6_TO_TCP = _L_TOOL - _WRIST_TO_LINK6

# FK∘IK acceptance for a returned solve() (float noise only — the wrist is
# exactly spherical and the planar constants are derived from the exact chain
# below, so there is no structural residual).
_FK_TOL_M = 1e-6


# ── URDF joint position limits (radians) — the servo-EEPROM design values ────
_EDU6_JOINT_LIMITS_RAD: list[tuple[float, float]] = [
    (-1.5708, 1.5708),     # joint1
    (0.0, 3.1066),         # joint2
    (-3.1066, 0.0),        # joint3
    (-3.1066, 3.1066),     # joint4
    (-1.5708, 1.9199),     # joint5 — ASYMMETRIC (relieved to +110°)
    (-3.1066, 3.1066),     # joint6
]


def _wrap(a: float) -> float:
    """Wrap an angle to (-pi, pi]."""
    return (a + math.pi) % (2.0 * math.pi) - math.pi


def _rpy_matrix(r: float, p: float, y: float) -> np.ndarray:
    cr, sr = math.cos(r), math.sin(r)
    cp, sp = math.cos(p), math.sin(p)
    cy, sy = math.cos(y), math.sin(y)
    rz = np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]], dtype=np.float64)
    ry = np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]], dtype=np.float64)
    rx = np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]], dtype=np.float64)
    return rz @ ry @ rx


def _axis_rot(axis, th: float) -> np.ndarray:
    a = np.asarray(axis, dtype=np.float64)
    a = a / np.linalg.norm(a)
    k = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]],
                 dtype=np.float64)
    return np.eye(3) + math.sin(th) * k + (1 - math.cos(th)) * (k @ k)


def _tf(xyz, rot: Optional[np.ndarray]) -> np.ndarray:
    t = np.eye(4, dtype=np.float64)
    if rot is not None:
        t[:3, :3] = rot
    t[:3, 3] = xyz
    return t


# Fixed per-joint transforms (origin translation + fixed rpy), composed once.
_FIXED = (
    _tf(_J1_XYZ, None),
    _tf(_J2_XYZ, _rpy_matrix(*_J2_RPY)),
    _tf(_J3_XYZ, None),
    _tf(_J4_XYZ, _rpy_matrix(*_J4_RPY)),
    _tf(_J5_XYZ, _rpy_matrix(*_J5_RPY)),
    _tf(_J6_XYZ, _rpy_matrix(*_J6_RPY)),
)

# World = URDF rotated π about Z.
_RZ_PI = np.array([[-1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, 1.0]],
                  dtype=np.float64)

# Wrist centre: the axis-4/5/6 intersection (exact — CAD-cleaned; measured
# pairwise axis gaps < 2e-15 m). Rigid in the link4 frame at (0, 0, _J5_XYZ_z)
# and in the link5 frame at (0, 0, _J6_XYZ_z).
_WC_LOCAL4 = np.array([0.0, 0.0, _J5_XYZ[2]], dtype=np.float64)
_WC_LOCAL5 = np.array([0.0, 0.0, _J6_XYZ[2]], dtype=np.float64)


def _derive_planar_constants():
    """Derive the exact 2R planar parameters ONCE at import from the chain
    itself (deterministic float64 arithmetic on the baked URDF constants — no
    hand-transcribed derived values, so a rounding slip cannot introduce a
    structural solve residual).

    Returns ``(shoulder_z, L2, L3, alpha0)`` where, in the arm plane with
    ``u`` toward the working direction (URDF −x) and ``v`` up:

    * shoulder = the axis-1/axis-2 intersection, at ``(u=0, v)``;
    * α(q2) = ``alpha0 + q2`` is the shoulder→elbow angle-from-vertical;
    * β(q2, q3) = ``π/2 + q2 + q3`` is the elbow→wrist angle-from-vertical
      (slope exactly 1 on both — verified against the chain in the tests).
    """
    t = np.eye(4, dtype=np.float64)
    frames = []
    for i in range(6):
        t = t @ _FIXED[i] @ _tf((0.0, 0.0, 0.0), _axis_rot(_AXES[i], 0.0))
        frames.append(t.copy())
    shoulder_z = frames[1][2, 3]
    x0 = _J1_XYZ[0]
    elbow = frames[2][:3, 3]
    wrist = (frames[3] @ np.append(_WC_LOCAL4, 1.0))[:3]
    # in-plane coordinates (u toward URDF −x, v up), y drops out (the lateral
    # ±17.7 mm frame offsets cancel — wrist y at zero pose < 1e-14).
    e_u, e_v = -(elbow[0] - x0), elbow[2] - shoulder_z
    w_u, w_v = -(wrist[0] - x0), wrist[2] - shoulder_z
    l2 = math.hypot(e_u, e_v)
    l3 = math.hypot(w_u - e_u, w_v - e_v)
    alpha0 = math.atan2(e_u, e_v)
    return float(shoulder_z), float(l2), float(l3), float(alpha0)


_SHOULDER_Z, _L2, _L3, _ALPHA0 = _derive_planar_constants()

# Strict-vertical reach annulus of the WRIST CENTRE (2R span; the practical
# authoritative bound is always ``solve(...) is not None``).
_REACH_MIN = abs(_L2 - _L3)
_REACH_MAX = _L2 + _L3


class Edu6IKSolver:
    """Closed-form Cartesian→joint solver for the edu6_studio arm.

    Public surface mirrors the OMX :class:`IKSolver` (``solve`` /
    ``solve_quat`` / ``fk`` / ``link_points`` / ``in_workspace`` /
    ``base_yaw`` / ``num_joints`` / ``backend`` / ``joint_limits`` /
    ``base_axis_x``) so ``ArmProfile.build_ik`` swaps it in transparently.
    """

    def __init__(self, urdf_string: Optional[str] = None) -> None:
        # ``urdf_string`` accepted for factory-signature compatibility; the
        # baked constants are verified by the independent URDF-parsing oracle
        # in test_edu6_ik.py instead of a runtime parse.
        self._joint_limits = list(_EDU6_JOINT_LIMITS_RAD)

    # ── identity ─────────────────────────────────────────────────────────────
    @property
    def backend(self) -> str:
        return 'closed-form-edu6'

    def num_joints(self) -> int:
        """Number of arm joints solved (joint1..joint6)."""
        return 6

    @property
    def joint_limits(self) -> tuple[tuple[float, float], ...]:
        return tuple(self._joint_limits)

    @property
    def base_axis_x(self) -> float:
        """WORLD-frame x offset of the joint-1 axis (+0.021295 m)."""
        return BASE_AXIS_X_WORLD

    # ── forward kinematics (URDF-native joints → WORLD TCP pose) ─────────────
    def _fk_chain(self, joints) -> Optional[list[np.ndarray]]:
        j = list(joints)
        if len(j) < 6:
            return None
        frames: list[np.ndarray] = []
        t = np.eye(4, dtype=np.float64)
        for i in range(6):
            t = t @ _FIXED[i] @ _tf((0.0, 0.0, 0.0), _axis_rot(_AXES[i], j[i]))
            frames.append(t.copy())
        return frames

    def _fk_matrix(self, joints) -> Optional[np.ndarray]:
        frames = self._fk_chain(joints)
        if frames is None:
            return None
        t6 = frames[5]
        tcp = np.eye(4, dtype=np.float64)
        tcp[:3, :3] = t6[:3, :3]
        # tool axis = link6 −z; TCP = fingertip.
        tcp[:3, 3] = t6[:3, 3] + t6[:3, :3] @ np.array(
            [0.0, 0.0, -_LINK6_TO_TCP], dtype=np.float64)
        return tcp

    def fk(self, joints) -> Optional[tuple[np.ndarray, np.ndarray]]:
        """``(R 3x3, t 3,)`` of the FINGERTIP TCP in the WORLD frame for the
        given URDF-native joint vector (joint1..joint6; extra entries such as
        the gripper are ignored), or ``None`` for fewer than 6 joints."""
        t = self._fk_matrix(joints)
        if t is None:
            return None
        r_world = _RZ_PI @ t[:3, :3]
        p_world = _RZ_PI @ t[:3, 3]
        return r_world.copy(), p_world.copy()

    def _fk_position(self, joints) -> Optional[np.ndarray]:
        t = self._fk_matrix(joints)
        return None if t is None else (_RZ_PI @ t[:3, 3]).copy()

    # ── link sampling for the swept no-go-zone check (WORLD frame) ───────────
    def link_points(self, joints, samples_per_link: int = 5) -> Optional[list[np.ndarray]]:
        """WORLD-frame points along base → shoulder → elbow → wrist → link6 →
        fingertip, plus ``samples_per_link`` interpolations per link — the same
        contract as the OMX solver (consumed by ``path_guard``)."""
        frames = self._fk_chain(joints)
        if frames is None:
            return None
        t5, t6 = frames[4], frames[5]
        wrist = (t5 @ np.append(_WC_LOCAL5, 1.0))[:3]
        tool_dir = t6[:3, :3] @ np.array([0.0, 0.0, -1.0])
        tcp = t6[:3, 3] + tool_dir * _LINK6_TO_TCP
        origins_urdf = [
            np.zeros(3, dtype=np.float64),            # base
            np.array(_J1_XYZ, dtype=np.float64),      # joint-1 origin
            frames[1][:3, 3].copy(),                  # shoulder (link2 frame)
            frames[2][:3, 3].copy(),                  # elbow (link3 frame)
            wrist,                                    # wrist centre
            t6[:3, 3].copy(),                         # link6 origin
            tcp,                                      # fingertip TCP
        ]
        origins = [_RZ_PI @ p for p in origins_urdf]
        n = max(1, int(samples_per_link))
        pts: list[np.ndarray] = [origins[0]]
        for a, b in zip(origins[:-1], origins[1:]):
            for k in range(1, n + 1):
                pts.append(a + (b - a) * (k / n))
        return pts

    # ── inverse kinematics (closed form, strict vertical top-down) ───────────
    def solve(
        self,
        target_xyz,
        target_rpy: tuple[float, float, float] = (math.pi, 0.0, 0.0),
        seed: Optional[list[float]] = None,
        free_yaw: bool = True,
        roll: Optional[float] = None,
    ) -> Optional[list[float]]:
        """Solve the 6 arm joints for a strict-vertical grasp at WORLD
        ``target_xyz`` (fingertip TCP, metres).

        ``roll`` follows motion's shared jaw formula (``base_yaw − tag_yaw +
        GRASP_ROLL_RAD``) and maps to ``q6 = wrap(π − roll)``; ``None`` falls
        back to ``target_rpy``'s yaw, else 0. ``seed``/``free_yaw`` are
        accepted for call-site compatibility and do not change the
        deterministic elbow-up-first solution. Returns URDF-native joints or
        ``None`` (unreachable / out of limits)."""
        arr = np.asarray(target_xyz, dtype=np.float64).reshape(3)
        x, y, z = (float(v) for v in arr)
        if not all(math.isfinite(v) for v in (x, y, z)):
            return None
        if roll is None:
            roll = float(target_rpy[2]) if len(target_rpy) >= 3 else 0.0
        roll = float(roll)
        if not math.isfinite(roll):
            return None

        # joint1 aims the arm plane; bearing measured from the joint-1 axis.
        dx = x - BASE_AXIS_X_WORLD
        theta1 = math.atan2(y, dx)
        r = math.hypot(dx, y)

        # Wrist centre sits directly above the fingertip TCP by the tool length.
        d_u = r
        d_v = (z + _L_TOOL) - _SHOULDER_Z
        rho = math.hypot(d_u, d_v)
        if rho < _REACH_MIN - 1e-9 or rho > _REACH_MAX + 1e-9:
            return None

        cos_g = (rho * rho - _L2 * _L2 - _L3 * _L3) / (2.0 * _L2 * _L3)
        cos_g = max(-1.0, min(1.0, cos_g))
        gamma_mag = math.acos(cos_g)
        psi = math.atan2(d_u, d_v)   # shoulder→wrist angle-from-vertical

        # Elbow-up first (the working posture — the branch every derivation
        # gate measured clearance for), then elbow-down; deterministic order.
        for gamma in (gamma_mag, -gamma_mag):
            alpha = psi - math.atan2(_L3 * math.sin(gamma),
                                     _L2 + _L3 * math.cos(gamma))
            theta2 = alpha - _ALPHA0
            # forearm angle-from-vertical β = π/2 + q2 + q3  ⇒  q3:
            beta = alpha + gamma
            theta3 = beta - math.pi / 2.0 - theta2
            # strict-vertical wrist: q4 = 0, q5 = π − β = π/2 − q2 − q3.
            theta5 = math.pi - beta
            theta4 = 0.0
            # Tool roll tracks the tag 1:1 (∂q6/∂tag_yaw = +1) across the whole
            # ±178° band — that live tracking is the intended Roboter-Studio
            # behaviour. Only where the wrapped value falls OUTSIDE the band do
            # we spend the jaw's 180° symmetry (a parallel jaw grasps
            # identically at q6 and q6 ± π) to bring it back in range. This is
            # what keeps the ±180° encoder seam unreachable without introducing
            # a dead band of tag orientations — and it is why the default
            # roll = 0 yields a NEUTRAL wrist (q6 = 0) instead of a half turn.
            theta6 = _wrap(math.pi - roll)
            lo6, hi6 = self._joint_limits[5]
            if theta6 > hi6:
                theta6 -= math.pi
            elif theta6 < lo6:
                theta6 += math.pi
            joints = [theta1, theta2, theta3, theta4, theta5, theta6]
            if not all(math.isfinite(v) for v in joints):
                continue
            if not self._within_limits(joints):
                continue
            fk_pos = self._fk_position(joints)
            if fk_pos is None:
                continue
            norm = float(np.linalg.norm(fk_pos - np.array([x, y, z])))
            if not math.isfinite(norm) or norm > _FK_TOL_M:
                continue
            return [float(v) for v in joints]
        return None

    def solve_quat(
        self,
        target_xyz,
        target_quat,
        seed: Optional[list[float]] = None,
        free_yaw: bool = False,
    ) -> Optional[list[float]]:
        """Position-only solve (orientation argument ignored) — the same
        call-site-compat contract as the OMX solver."""
        return self.solve(target_xyz, seed=seed, free_yaw=free_yaw)

    # ── limits / workspace / bearing ─────────────────────────────────────────
    def _within_limits(self, joints, tol: float = 1e-6) -> bool:
        for i in range(min(len(joints), len(self._joint_limits))):
            lo, hi = self._joint_limits[i]
            if joints[i] < lo - tol or joints[i] > hi + tol:
                return False
        return True

    def in_workspace(self, target_xyz) -> bool:
        return self.solve(target_xyz) is not None

    def base_yaw(self, x: float, y: float) -> float:
        """joint1 yaw aiming the arm plane at WORLD ``(x, y)`` — exactly the
        ``theta1`` :meth:`solve` computes (bearing from the joint-1 axis at
        world x = +0.021295, never from the origin)."""
        return math.atan2(float(y), float(x) - BASE_AXIS_X_WORLD)

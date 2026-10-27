#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Closed-form analytical inverse kinematics for the v1_studio arm.

The V1 is a 5-DOF Feetech STS arm with a rotating two-blade claw
(``5dof_assembly_urdf2``, SolidWorks export 2026-10-22, with ``Base_part.2`` as
the base):

* ``joint1`` — yaw about base z (±90°). Its axis is ``(0, 0, −1)``, so the
  JOINT VALUE is the NEGATIVE of the world azimuth it aims at — see
  :meth:`V1IKSolver.base_yaw`, which returns the AZIMUTH, not the joint.
* ``joint2/3/4`` — shoulder / elbow / wrist pitch, all three PARALLEL (a planar
  3R chain). ``joint2``/``joint3`` are [0, π]; ``joint4`` is ±110°.
* ``joint5`` — tool roll (±90°; the jaw-alignment joint). Its axis is
  collinear with the tool axis, so it never moves the TCP.
* (``RL_joint`` is the claw servo, not a positioning DOF; ``LF_joint``
  ``<mimic>``s it through the claw's 18T/19T gear pair, multiplier −18/19.)

WHY THE 3R CHAIN MAKES THIS EXACT. A strict-vertical (top-down) grasp fixes the
tool direction, which on a planar 3R is ONE scalar equation in
``q2 + q3 + q4`` — so ``q4`` is DETERMINED and the remaining 2R
(shoulder→elbow→wrist) is the textbook two-link problem. There is no wrist
sphere to decouple and no idealisation: this arm's geometry is planar by
construction, and the whole solve is one pass, elbow-up preferred.

CONSEQUENCE WORTH KNOWING: the vertical constraint reads
``q4 = q2 − q3 + (BETA0 − π)`` with ``BETA0 ≈ +π/2``, i.e. ``q4 ≈ q2 − q3 −
π/2``. So ``joint4``'s LOWER limit alone sets a floor on ``q2 − q3`` — a
STRUCTURAL restriction on the strict-vertical family, not a tuning choice, and
it is why the reachable top-down band is an annulus and not a disc. At ±110°
the floor is ``q2 ≥ q3 − 0.3490``. (The upper limit never binds: it would need
``q2 ≥ q3 + 3.4906``, and ``q2 ≤ π``.) The strict-vertical TCP ceiling this
leaves is ≈0.146 m.

FRAMES — the one thing to keep straight:

* The URDF's reachable half-disc lies on **−x** (``joint1``'s fixed
  ``rpy = (0, 0, −1.5708)`` plus the ``(0, 0, −1)`` axes put it there). The
  solver therefore works in the **WORLD frame = URDF base_link rotated 180°
  about Z** (front = +x, like the OMX), so every Roboter Studio convention
  (board placement, destinations, zones, German copy) carries over unchanged.
* ``solve()`` takes WORLD coordinates and returns **URDF-native joints** (what
  ``/joint_states``, the trajectory rail and the web twin speak). ``fk()`` takes
  URDF-native joints and returns the WORLD-frame TCP pose.
* ``base_link``'s origin is the base BOTTOM, i.e. the table plane: the shipped
  URDF re-origins the CAD's base frame by +6 mm (``joint1`` sits 50.2 mm above
  the table), so z = 0 is the table here exactly as everywhere else.
* ``joint1``'s axis passes through the base origin, so :attr:`base_axis_x` is
  0 and a bearing is measured from the origin. The property exists anyway
  because ``path_guard`` and ``motion`` read it generically.
* The TCP is the **CLOSED FINGERTIP** (:data:`_L_TOOL` = 0.1086 m below the
  ``end_effector`` origin along the tool axis = the 21.25 mm claw pivot offset +
  the 87.35 mm blade). A grasp targets ``z_table + object_height −
  grasp_depth`` exactly like the OMX.

  TOUCH-OFF NOTE (rig gate VR8): this claw ROTATES, so its tip height depends
  on the jaw opening. „Tisch vermessen" must therefore be taught with the claw
  CLOSED. NOT everywhere pessimistic, measured on the meshes: opening the claw
  first moves the tips DOWN, by up to 0.46 mm, before they rise past the closed
  height at ≈0.22 rad (the blade pivots sit 9.25 mm off the tool axis) — small
  against every floor margin, and :meth:`V1IKSolver.tool_tip_rise_m` reports it
  as a negative rise.

ROLL contract (jaw alignment): motion's shared formula
``roll = base_yaw − tag_yaw + GRASP_ROLL_RAD`` is consumed here as
``q5 = fold(wrap(−roll))``. Derivation: at a vertical pose the jaws separate
along the world azimuth ``χ = π/2 + φ + q5`` where ``φ`` is the arm-plane
azimuth (``= base_yaw``), so ``q5 = −roll`` yields ``χ ≡ tag_yaw`` for the
fleet-wide default ``GRASP_ROLL_RAD = π/2`` — the identical geometric behaviour
to the OMX, absorbed entirely by this one mapping (``∂q5/∂GRASP_ROLL = −1``).

THE JAW FOLD IS MANDATORY HERE, not an optimisation. ``joint5`` is limited to
**±90°** while a tag can present any yaw in 360°, so without folding
``q5`` and ``q5 ∓ π`` into the reachable window HALF of all tag orientations
would simply return ``None``.

IT IS LEGITIMATE: the two blades are mirror images about the claw's own
centreline, and on this claw that centreline lies ON the roll axis (blade band
``x_ee ∈ [−11.045, +11.04] mm`` at the catalog close, i.e. ≈0.00 mm off). A
180° roll maps the blade pair onto itself to within 0.81 mm, so the two folded
twins seat an object at the same place to that tolerance.

THE FOLD'S BEHAVIOUR IS EXACT REGARDLESS. A 180° rotation about ANY axis
parallel to ``z_ee`` maps ±x_ee → ∓x_ee and ±y_ee → ∓y_ee, so both the jaw line
and the jaw-width line are preserved; only their signs flip. A folded twin's jaw
azimuth equals the unfolded one's mod π (``test_v1_ik.py`` pins it).

Geometry constants are transcribed from the shipped in-repo URDF
``physical_ai_manager/public/v1-urdf/v1.urdf`` (generated from the CAD export
by ``tools/build_v1_urdf_assets.py``) — the independent FK oracle in
``test_v1_ik.py`` parses THAT file, so a solver-constant transcription error
cannot verify itself.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np


# ── Kinematic constants (metres / radians) — URDF parent→child transcription ─
# joint origins (xyz) + fixed rpy; axes are the URDF's, applied after the rpy.
# _J1_XYZ is the CAD's 0.0442 + the 6 mm base re-origin (the table plane).
_J1_XYZ = (0.0, 0.0, 0.0502)
_J1_RPY = (0.0, 0.0, -1.5708)
_J2_XYZ = (0.0, 0.0, 0.04175)
_J2_RPY = (-1.5708, 0.0, 1.5708)
_J3_XYZ = (0.1555, -0.032, 0.0)
_J3_RPY = (-3.1416, 0.0, -1.5708)
_J4_XYZ = (0.0, 0.2225, 0.0)
_J4_RPY = (0.0, 0.0, 0.0)
_J5_XYZ = (0.0, 0.07195, 0.0)
_J5_RPY = (-1.5708, 0.0, 0.0)
# Every joint turns about its own frame's −z (SolidWorks exported them that way).
_AXES = ((0, 0, -1), (0, 0, -1), (0, 0, -1), (0, 0, -1), (0, 0, -1))

# joint1's axis passes through the base origin on this arm (x = y = 0 exactly).
# Kept as a named constant because motion and path_guard read
# ``ik.base_axis_x`` generically.
BASE_AXIS_X_WORLD = 0.0

# Tool: ``end_effector`` is FIXED to link5 at zero offset, and the closed claw
# tip sits on the tool axis (+z of the end_effector frame) at the joint pivot
# 0.02125 m plus the 0.08735 m blade (measured on the STLs: the closed tips sit
# at z_ee 108.595 / 108.575 mm, on the roll axis). The claw gear ratio does not
# move it: both blades are at 0 when closed.
_L_TOOL = 0.02125 + 0.08735
# Claw LOWEST-point rise above the closed-tip TCP: (band start rad, min true rise
# in [start, next start) in m, rounded DOWN to 1e-5). Generated from the shipped
# finger STLs through RL_joint/LF_joint and the claw housing (link5.STL +
# end_effector.STL, rigid in the end_effector frame) — see tool_tip_rise_m. Each
# band is the minimum over BOTH claw gear ratios, 1 and the URDF's 18/19: the
# shipped ratio is a CAD value until rig gate VR16 measures it, and the lower of
# the two is the one a floor check may credit. It spans the claw's WHOLE window
# [0, 1.5708], not only the commanded band (0–0.9): a claw opened by hand while
# the arm is limp stays that wide after the re-lock. Negative near 0.1 rad: the
# tips dip before they rise. From ≈1.51 rad the tips ride above the housing's
# own lowest point (42.50 mm below the end_effector origin), so the last band,
# which covers [1.55, 1.5708] and everything past it, is the housing's plateau.
_TIP_RISE_TABLE = (
    (0.00, -0.00034), (0.05, -0.00046), (0.10, -0.00046), (0.15, -0.00040),
    (0.20, -0.00015), (0.25, 0.00030), (0.30, 0.00095), (0.35, 0.00179),
    (0.40, 0.00282), (0.45, 0.00404), (0.50, 0.00545), (0.55, 0.00704),
    (0.60, 0.00881), (0.65, 0.01076), (0.70, 0.01288), (0.75, 0.01517),
    (0.80, 0.01762), (0.85, 0.02022), (0.90, 0.02298), (0.95, 0.02588),
    (1.00, 0.02891), (1.05, 0.03208), (1.10, 0.03538), (1.15, 0.03878),
    (1.20, 0.04230), (1.25, 0.04592), (1.30, 0.04964), (1.35, 0.05343),
    (1.40, 0.05731), (1.45, 0.06125), (1.50, 0.06524), (1.55, 0.06609),
)

# FK∘IK acceptance for a returned solve().
#
# 1e-5, and the decade above float noise is a property of the CAD export rather
# than of this solver: SolidWorks rounded every right angle in this URDF to
# ``1.5708`` / ``3.1416``, so the chain is planar only to ~1e-6 m while the
# closed form assumes it is planar exactly. Measured worst residual over a
# 3000-pose FK→IK→FK round trip: 2.39e-6 m. That is far below one servo tick at
# full reach (4096 ticks/rev over 0.38 m ≈ 0.58 mm), so it is float bookkeeping,
# not model error — but at 1e-6 it would silently reject solves, which reads as
# "the workspace is unreachable". Do not tighten without re-exporting the URDF
# at full precision.
_FK_TOL_M = 1e-5


# ── URDF joint position limits (radians) — the servo-EEPROM design values ────
# NOTE these are the SHIPPED URDF's values, which write every right angle at
# four decimals (1.5708 / 3.1416) where the raw CAD export wrote two. joint5 is
# why that matters and not merely tidy: its window IS the jaw-fold window, so at
# ±1.57 a required roll of exactly ±90° — a cube square-on to the arm's own
# bearing, which is common, not a corner case — would fall 0.0008 rad outside
# BOTH folded twins and the grasp would be refused as unreachable. Half a servo
# tick; nothing physical changes.
#
# joint4 is ±110° (owner decision): at +110° link4 passes link3 at 4.2 mm.
# ``1.9198`` keeps the file's four-decimal convention and rounds INWARD of 110°
# (= 1.91986217… rad) — inward is the safe direction. Treat ±110° as a CEILING
# and never widen past it.
#
# ⚠ CHANGING ANY VALUE HERE IS A HARDWARE-PROVISIONING CHANGE, NOT A SOFTWARE
# ONE. The driver's boot probe compares every servo's EEPROM
# Min/Max_Position_Limit window with the servo spec, so a limit that moves here
# makes every physical arm refuse to boot („Positionsgrenzen") until each servo
# is re-calibrated at the Servo-Station (or ``python3 tools/feetech_provision.py
# --arm v1`` is re-run). The same pair is declared in FOUR places that must move
# together — here, the shipped URDF (physical_ai_manager/public/v1-urdf/v1.urdf),
# the driver node (docker/open_manipulator/feetech_arm_node.py::JOINT_LIMITS_RAD)
# and the servo spec
# (docker/open_manipulator/feetech_servo_spec.py::JOINT_LIMITS_RAD).
_V1_JOINT_LIMITS_RAD: list[tuple[float, float]] = [
    (-1.5708, 1.5708),     # joint1  base yaw
    (0.0, 3.1416),         # joint2  shoulder
    (0.0, 3.1416),         # joint3  elbow
    (-1.9198, 1.9198),     # joint4  wrist pitch — ±110°, owner-stated ceiling
    (-1.5708, 1.5708),     # joint5  tool roll — the jaw-fold window IS ±90°
]

# joint5 jaw-symmetry fold. See the module docstring: on this arm the fold is
# what makes 360° of tag yaw reachable at all through a ±90° roll joint, and it
# is sound because the blade pair is mirror-symmetric about the roll axis (to
# 0.81 mm). Reference is ZERO, never the seed: a seed-relative choice feeds its
# own output back in and drifts toward the map edge, and zero additionally keeps
# solve()'s documented contract that ``seed`` does not change the answer.
_J5_JAW_FOLD_RAD = math.pi / 2.0


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
    _tf(_J1_XYZ, _rpy_matrix(*_J1_RPY)),
    _tf(_J2_XYZ, _rpy_matrix(*_J2_RPY)),
    _tf(_J3_XYZ, _rpy_matrix(*_J3_RPY)),
    _tf(_J4_XYZ, None),
    _tf(_J5_XYZ, _rpy_matrix(*_J5_RPY)),
)

# World = URDF rotated pi about Z.
_RZ_PI = np.array([[-1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, 1.0]],
                  dtype=np.float64)


def _derive_planar_constants():
    """Derive the exact planar 3R parameters ONCE at import from the chain
    itself (deterministic float64 arithmetic on the baked URDF constants — no
    hand-transcribed derived values, so a rounding slip cannot introduce a
    structural solve residual).

    Returns ``(shoulder_z, L2, L3, L4, alpha0, beta0)`` where, in the arm plane
    with ``u`` toward the working direction (WORLD +x = URDF −x) and ``v`` up,
    every angle is measured FROM VERTICAL toward ``u``:

    * shoulder = the axis-1/axis-2 intersection, at ``(u=0, v=shoulder_z)``;
    * ``α(q2) = alpha0 + q2``          shoulder→elbow;
    * ``β(q2,q3) = beta0 + q2 − q3``   elbow→wrist  (wrist = the joint-4 axis);
    * ``γ(q2,q3,q4) = β − q4``         wrist→tool origin, and the TOOL AXIS.

    All three slopes are exactly ±1 (verified against the chain in the tests).
    """
    t = np.eye(4, dtype=np.float64)
    frames = []
    for i in range(5):
        t = t @ _FIXED[i] @ _tf((0.0, 0.0, 0.0), _axis_rot(_AXES[i], 0.0))
        frames.append(t.copy())
    shoulder = frames[1][:3, 3]
    elbow = frames[2][:3, 3]
    wrist = frames[3][:3, 3]
    tool = frames[4][:3, 3]
    l2 = float(np.linalg.norm(elbow - shoulder))
    l3 = float(np.linalg.norm(wrist - elbow))
    l4 = float(np.linalg.norm(tool - wrist))
    # u = −x_urdf (= +x_world); angle measured from +v toward +u.
    alpha0 = math.atan2(-float(elbow[0] - shoulder[0]),
                        float(elbow[2] - shoulder[2]))
    beta0 = math.atan2(-float(wrist[0] - elbow[0]),
                       float(wrist[2] - elbow[2]))
    return (float(shoulder[2]), l2, l3, l4, alpha0, beta0)


_SHOULDER_Z, _L2, _L3, _L4, _ALPHA0, _BETA0 = _derive_planar_constants()

# Strict-vertical wrist constraint: γ = π (tool straight down) ⇒
#     q4 = q2 − q3 + (BETA0 − π)
_Q4_VERTICAL_OFFSET = _BETA0 - math.pi
# Interior 2R angle g = β − α = (BETA0 − ALPHA0) − q3  ⇒  q3 = _G_OFFSET − g.
_G_OFFSET = _BETA0 - _ALPHA0
# The tool origin sits _L4 above the wrist axis and the TCP _L_TOOL beyond it;
# at a strict-vertical pose both are straight DOWN, so the wrist centre is this
# far ABOVE the commanded TCP.
_WRIST_ABOVE_TCP = _L4 + _L_TOOL

# Strict-vertical reach annulus of the WRIST CENTRE (2R span; the practical
# authoritative bound is always ``solve(...) is not None``, because the ±110°
# joint4 window still clips this annulus further — it admits only part of the
# 2R-feasible set).
_REACH_MIN = abs(_L2 - _L3)
_REACH_MAX = _L2 + _L3


class V1IKSolver:
    """Closed-form Cartesian→joint solver for the v1_studio arm.

    Public surface mirrors the OMX :class:`IKSolver` (``solve`` /
    ``solve_quat`` / ``fk`` / ``link_frames`` / ``link_points`` /
    ``in_workspace`` / ``base_yaw`` / ``roll_from_joints`` / ``num_joints`` /
    ``backend`` / ``joint_limits`` / ``base_axis_x``) so
    ``ArmProfile.build_ik`` swaps it in transparently.
    """

    # The EE frame IS the closed fingertip (``_L_TOOL`` ends at the tip), so a
    # touch-off's z_table is the table surface itself (see IKSolver's twin).
    tool_tip_below_ee_m = 0.0

    def __init__(self, urdf_string: Optional[str] = None) -> None:
        # ``urdf_string`` accepted for factory-signature compatibility; the
        # baked constants are verified by the independent URDF-parsing oracle
        # in test_v1_ik.py instead of a runtime parse.
        self._joint_limits = list(_V1_JOINT_LIMITS_RAD)

    # ── identity ─────────────────────────────────────────────────────────────
    @property
    def backend(self) -> str:
        return 'closed-form-v1'

    def num_joints(self) -> int:
        """Number of arm joints solved (joint1..joint5)."""
        return 5

    @property
    def joint_limits(self) -> tuple[tuple[float, float], ...]:
        return tuple(self._joint_limits)

    @property
    def base_axis_x(self) -> float:
        """WORLD-frame x offset of the joint-1 axis — exactly 0 on this arm."""
        return BASE_AXIS_X_WORLD

    # ── forward kinematics (URDF-native joints → WORLD TCP pose) ─────────────
    def _fk_chain(self, joints) -> Optional[list[np.ndarray]]:
        j = list(joints)
        if len(j) < 5:
            return None
        frames: list[np.ndarray] = []
        t = np.eye(4, dtype=np.float64)
        for i in range(5):
            t = t @ _FIXED[i] @ _tf((0.0, 0.0, 0.0), _axis_rot(_AXES[i], j[i]))
            frames.append(t.copy())
        return frames

    def _fk_matrix(self, joints) -> Optional[np.ndarray]:
        frames = self._fk_chain(joints)
        if frames is None:
            return None
        t5 = frames[4]
        tcp = np.eye(4, dtype=np.float64)
        tcp[:3, :3] = t5[:3, :3]
        # ``end_effector`` is fixed to link5 at zero offset and the tool axis is
        # its +z; TCP = closed fingertip.
        tcp[:3, 3] = t5[:3, 3] + t5[:3, :3] @ np.array(
            [0.0, 0.0, _L_TOOL], dtype=np.float64)
        return tcp

    def fk(self, joints) -> Optional[tuple[np.ndarray, np.ndarray]]:
        """``(R 3x3, t 3,)`` of the FINGERTIP TCP in the WORLD frame for the
        given URDF-native joint vector (joint1..joint5; extra entries such as
        the gripper are ignored), or ``None`` for fewer than 5 joints."""
        t = self._fk_matrix(joints)
        if t is None:
            return None
        r_world = _RZ_PI @ t[:3, :3]
        p_world = _RZ_PI @ t[:3, 3]
        return r_world.copy(), p_world.copy()

    def _fk_position(self, joints) -> Optional[np.ndarray]:
        t = self._fk_matrix(joints)
        return None if t is None else (_RZ_PI @ t[:3, 3]).copy()

    # ── per-link frames for the whole-link geometry model (WORLD frame) ──────
    def link_frames(self, joints) -> Optional[list[np.ndarray]]:
        """Per-link 4×4 WORLD-frame transforms:
        ``[base_link, link1, link2, link3, link4, link5]``.

        Consumed by :mod:`physical_ai_server.workflow.arm_geometry`, which
        carries one axis-aligned box per link in that link's OWN frame — the
        table-floor and self-collision model. Index 5 (link5) is the box of the
        WHOLE gripper cluster: link5 + ``end_effector`` + both claw blades,
        unioned over the full jaw band, because the blades ride ``RL_joint`` /
        the ``LF_joint`` ``<mimic>`` and are not on the arm's own FK chain.

        A PUBLIC method on purpose: ``arm_geometry`` must not reach into ``_fk_chain`` across a module
        boundary, and it must not compose the world rotation itself. One owner
        for the frame convention.

        The OMX solver deliberately does NOT get this method: its absence is
        what makes ``arm_geometry.resolve_geometry`` return ``None`` for every
        OMX profile, which keeps OMX behaviour bit-identical.

        ``None`` for fewer than 5 joints (matching :meth:`fk`)."""
        frames = self._fk_chain(joints)
        if frames is None:
            return None
        base = np.eye(4, dtype=np.float64)
        base[:3, :3] = _RZ_PI
        out = [base]
        for t in frames:
            world = np.eye(4, dtype=np.float64)
            world[:3, :3] = _RZ_PI @ t[:3, :3]
            world[:3, 3] = _RZ_PI @ t[:3, 3]
            out.append(world)
        return out

    # ── link sampling for the swept no-go-zone check (WORLD frame) ───────────
    def link_points(self, joints, samples_per_link: int = 5) -> Optional[list[np.ndarray]]:
        """WORLD-frame points along base → joint-1 origin → shoulder → elbow →
        wrist → tool origin → fingertip, plus ``samples_per_link``
        interpolations per link — the same contract as the OMX solver (consumed
        by ``path_guard``)."""
        frames = self._fk_chain(joints)
        if frames is None:
            return None
        t5 = frames[4]
        tcp = t5[:3, 3] + t5[:3, :3] @ np.array([0.0, 0.0, _L_TOOL],
                                                dtype=np.float64)
        origins_urdf = [
            np.zeros(3, dtype=np.float64),            # base
            np.array(_J1_XYZ, dtype=np.float64),      # joint-1 origin
            frames[1][:3, 3].copy(),                  # shoulder (joint-2 axis)
            frames[2][:3, 3].copy(),                  # elbow (joint-3 axis)
            frames[3][:3, 3].copy(),                  # wrist (joint-4 axis)
            t5[:3, 3].copy(),                         # tool origin (joint-5)
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
        """Solve the 5 arm joints for a strict-vertical grasp at WORLD
        ``target_xyz`` (fingertip TCP, metres).

        ``roll`` follows motion's shared jaw formula (``base_yaw − tag_yaw +
        GRASP_ROLL_RAD``) and maps to ``q5 = fold(wrap(−roll))``; ``None`` falls
        back to ``target_rpy``'s yaw, else 0. ``seed``/``free_yaw`` are accepted
        for call-site compatibility and do not change the deterministic
        elbow-up-first solution. Returns URDF-native joints or ``None``
        (unreachable / out of limits)."""
        arr = np.asarray(target_xyz, dtype=np.float64).reshape(3)
        x, y, z = (float(v) for v in arr)
        if not all(math.isfinite(v) for v in (x, y, z)):
            return None
        if roll is None:
            roll = float(target_rpy[2]) if len(target_rpy) >= 3 else 0.0
        roll = float(roll)
        if not math.isfinite(roll):
            return None

        # joint1 aims the arm plane. The AXIS is (0, 0, −1), so the joint value
        # is the NEGATIVE of the azimuth it points at (see base_yaw).
        dx = x - BASE_AXIS_X_WORLD
        theta1 = -math.atan2(y, dx)
        r = math.hypot(dx, y)

        # Wrist centre sits directly above the fingertip TCP by tool + link4.
        d_u = r
        d_v = (z + _WRIST_ABOVE_TCP) - _SHOULDER_Z
        rho = math.hypot(d_u, d_v)
        if rho < _REACH_MIN - 1e-9 or rho > _REACH_MAX + 1e-9:
            return None

        cos_g = (rho * rho - _L2 * _L2 - _L3 * _L3) / (2.0 * _L2 * _L3)
        cos_g = max(-1.0, min(1.0, cos_g))
        gamma_mag = math.acos(cos_g)
        psi = math.atan2(d_u, d_v)   # shoulder→wrist angle-from-vertical

        # Elbow-UP first (the working posture — positive g folds the elbow above
        # the shoulder→wrist line, which is what every clearance figure in the
        # plan was measured on), then elbow-down; deterministic order.
        #
        # The elbow-DOWN branch NEVER YIELDS A RETURNED SOLUTION, and it is kept
        # anyway. The reason is dominance, not a reach contradiction, and the
        # distinction is load-bearing — see below.
        #
        # Write φ(g) = atan2(L3·sin g, L2 + L3·cos g), the 2R triangle's SHOULDER
        # angle. Substituting the loop body's own algebra collapses to
        #
        #     q4_up   = ψ − π + (|g| − |φ|)
        #     q4_down = ψ − π − (|g| − |φ|)
        #
        # where (|g| − |φ|) is that triangle's WRIST interior angle, hence
        # STRICTLY POSITIVE for any non-degenerate g. Likewise
        # q2_down − q2_up = 2|φ| ≥ 0 and q3_down − q3_up = 2|g| ≥ 0. So on every
        # constraint that can bind, elbow-up is weakly better than elbow-down:
        #
        #   1. elbow-down in limits ⟹ q3_down = _G_OFFSET + |g| ≤ π
        #                          ⟹ |g| ≤ π − _G_OFFSET = 0.20295 ⟹ |φ| ≤ 0.11857
        #   2. q2_up = q2_down − 2|φ| ≤ π ✓, and
        #      q2_up = ψ − |φ| − _ALPHA0 ≥ −0.11857 + 1.36784 > 0 ✓
        #   3. q3_up = _G_OFFSET − |g| ∈ [2.7357, 2.93864] ⊂ [0, π] ✓
        #   4. q4_up = q4_down + 2(|g| − |φ|) > q4_down ≥ −LIM ✓, and
        #      q4_up = ψ − π + (|g| − |φ|) ≤ 0.20295 ≤ LIM ✓   (ψ ∈ [0, π])
        #   5. theta1 and theta5 are computed ONCE, above the loop — identical on
        #      both branches — and both branches solve the same target exactly,
        #      so neither is filtered by _FK_TOL_M.
        #
        # ⟹ elbow-down in limits ⟹ elbow-up in limits, and elbow-up is tried
        # FIRST. Holds for ANY joint4 window ≥ ±0.20295 rad and uses no property
        # of z. Note the weaker claim "elbow-down is never even WITHIN LIMITS"
        # is FALSE at ±110°: it is legal over a small region (4 of the 4000
        # targets ``test_v1_ik`` samples) and simply never SELECTED. So the
        # posture the arm adopts is always elbow-up, which is what every
        # clearance figure and path_guard's "no reconfiguration flip" rely on.
        #
        # It stays because it is the CORRECT general form: delete it and a
        # future link-length change (the rods are meant to be swapped — see the
        # CAD's README_rod_lengths.md) silently loses half the solution space.
        theta5 = self._fold_jaw(_wrap(-roll))
        for g in (gamma_mag, -gamma_mag):
            alpha = psi - math.atan2(_L3 * math.sin(g),
                                     _L2 + _L3 * math.cos(g))
            theta2 = alpha - _ALPHA0
            theta3 = _G_OFFSET - g
            theta4 = theta2 - theta3 + _Q4_VERTICAL_OFFSET
            joints = [theta1, theta2, theta3, theta4, theta5]
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

    def _fold_jaw(self, theta5: float) -> float:
        """Choose between the jaw-identical wrist angles ``theta5`` and
        ``theta5 ∓ π`` — the SAME physical grasp (see :data:`_J5_JAW_FOLD_RAD`)
        — taking the one nearest ZERO.

        On this arm the fold is what makes the full 360° of tag yaw reachable
        through a ±90° joint at all: ``_wrap`` lands in (−π, π], and for every
        value outside ±90° exactly one of the two twins falls inside, so the
        fold never loses a target. Takes NO seed on purpose (a seed-relative
        choice drifts toward the map edge). ``min`` keeps the
        FIRST minimum, so an exact tie (|theta5| == π/2) leaves ``theta5``
        unfolded."""
        lo, hi = self._joint_limits[4]
        candidates = [theta5]
        for alt in (theta5 - math.pi, theta5 + math.pi):
            if lo - 1e-9 <= alt <= hi + 1e-9:
                candidates.append(alt)
        return min(candidates, key=abs)

    # ── limits / workspace / bearing ─────────────────────────────────────────
    def _within_limits(self, joints, tol: float = 1e-6) -> bool:
        for i in range(min(len(joints), len(self._joint_limits))):
            lo, hi = self._joint_limits[i]
            if joints[i] < lo - tol or joints[i] > hi + tol:
                return False
        return True

    def in_workspace(self, target_xyz) -> bool:
        return self.solve(target_xyz) is not None

    @property
    def approach_axis_local(self) -> tuple[float, float, float]:
        """The TOOL/approach direction in the frame :meth:`fk` returns — here
        link5 +z, i.e. the ``end_effector`` frame's own +z. See the OMX solver's copy for why this is per-solver."""
        return (0.0, 0.0, 1.0)

    def base_yaw(self, x: float, y: float) -> float:
        """WORLD AZIMUTH of the table point ``(x, y)`` from the joint-1 axis.

        NOT ``theta1``. On the OMX the two happen to be the same number; here ``joint1``'s axis is ``(0, 0, −1)``, so ``solve`` returns
        ``theta1 = −base_yaw(x, y)``. What every caller actually wants is the
        AZIMUTH — ``motion.grasp_joint5`` composes it with the tag yaw in world
        terms (``roll = base_yaw − tag_yaw + GRASP_ROLL``) and never touches a
        joint — so this method keeps the geometric meaning and the sign flip
        stays inside ``solve``. Handing back ``theta1`` here would mirror every
        tag-tracked grasp.

        Returns a value in ``(−π, π]`` (``atan2`` range). Not gated on
        reachability — callers use :meth:`in_workspace` / :meth:`solve`."""
        return math.atan2(float(y), float(x) - BASE_AXIS_X_WORLD)

    def tool_tip_rise_m(self, gripper_rad: float) -> float:
        """Height (m) by which the claw's LOWEST point sits ABOVE the closed-fingertip
        TCP at claw command ``gripper_rad`` — a guaranteed LOWER BOUND, and it can
        be NEGATIVE.

        Read from :data:`_TIP_RISE_TABLE`, which is DERIVED from the shipped finger
        meshes (``right_finger.STL`` / ``left_finger.STL`` through ``RL_joint`` /
        ``LF_joint`` and the ``<mimic>``) and the claw housing (``link5.STL`` +
        ``end_effector.STL``), over the claw's whole window 0–1.5708 rad: each
        0.05 rad band holds the MINIMUM true rise of the claw's lowest part
        anywhere inside it, at BOTH claw gear ratios (1 and the URDF's 18/19),
        rounded DOWN to 0.01 mm. Up to ≈1.51 rad that part is a blade tip; past
        it the housing (66.1 mm above the closed tip), so the last band is a
        plateau and a value past the window is never credited more than the
        housing allows. The curve is not a simple function of the opening: the
        blade pivots sit 9.25 mm either side of the tool axis, so opening the
        claw first moves the tips DOWN (up to 0.46 mm) before they climb past the
        closed height. A negative value therefore makes a check that adds the
        rise straight up STRICTER, never looser; set back along a tilted or
        upturned tool axis (``motion.tool_tip_rise_world_z_m``) it is penalised
        less, by at most twice that dip, which is the true geometry of that
        point. ``test_manual_relock_home_glide``'s V1 claw tip-rise test
        recomputes the true curve from the meshes and fails if any band
        over-credits. Rig gates VR8/VR16 own the physical curve."""
        try:
            g = float(gripper_rad)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(g):
            return 0.0
        value = _TIP_RISE_TABLE[0][1]
        for band_start, rise in _TIP_RISE_TABLE:
            if g >= band_start:
                value = rise
            else:
                break
        return value

    def roll_from_joints(self, joints) -> Optional[float]:
        """INVERSE of the ``roll`` → joint5 mapping: the ``roll`` to hand
        :meth:`solve` so the wrist comes back where ``joints`` has it.

        ``solve`` maps ``q5 = fold(wrap(−roll))``, so the roll argument and the
        joint value are DIFFERENT NUMBERS on this arm — unlike the OMX where ``theta5 = roll`` is the identity. A "move the
        tool, KEEP the current wrist" caller that passed ``joints[4]`` straight
        into ``roll=`` would get ``−q5`` back and MIRROR the wrist on every
        Cartesian jog step. Every such call site goes through this method
        (``motion._current_roll``, ``physical_ai_server``'s Cartesian jog).

        Round-trip is exact for every reachable joint5, because this joint's
        ±90° limit IS the fold window — there is no value the fold could map to
        its twin. The fingertip TCP lies exactly ON the joint5 axis, so joint5
        does not move the tool at all.

        ``None`` for a short/non-finite joint vector."""
        try:
            value = float(joints[4])
        except (TypeError, ValueError, IndexError, KeyError):
            return None
        return _wrap(-value) if math.isfinite(value) else None

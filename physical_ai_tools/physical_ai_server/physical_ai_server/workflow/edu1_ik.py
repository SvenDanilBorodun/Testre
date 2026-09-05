#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Closed-form analytical inverse kinematics for the edu1_studio arm.

The edu1 ("Edu:1") is a 5-DOF Feetech STS arm with a rotating two-blade claw
(``5dof_assembly_urdf2``, SolidWorks export 2026-09-05):

* ``joint1`` — yaw about base z (±90°). Its axis is ``(0, 0, −1)``, so the
  JOINT VALUE is the NEGATIVE of the world azimuth it aims at — see
  :meth:`Edu1IKSolver.base_yaw`, which returns the AZIMUTH, not the joint.
* ``joint2/3/4`` — shoulder / elbow / wrist pitch, all three PARALLEL (a planar
  3R chain). ``joint2``/``joint3`` are [0, π]; ``joint4`` is ±90°.
* ``joint5`` — tool roll (±90°; the jaw-alignment joint). Its axis is
  collinear with the tool axis, so it never moves the TCP.
* (``RL_joint`` is the claw servo, not a positioning DOF; ``LF_joint``
  ``<mimic>``s it.)

WHY THE 3R CHAIN MAKES THIS EXACT. A strict-vertical (top-down) grasp fixes the
tool direction, which on a planar 3R is ONE scalar equation in
``q2 + q3 + q4`` — so ``q4`` is DETERMINED and the remaining 2R
(shoulder→elbow→wrist) is the textbook two-link problem. There is no wrist
sphere to decouple and no idealisation: unlike the edu6 (whose axes 4/5/6 had to
be verified as exactly intersecting) this arm's geometry is planar by
construction, and the whole solve is one pass, elbow-up preferred.

CONSEQUENCE WORTH KNOWING: the vertical constraint reads
``q4 = q2 − q3 + (BETA0 − π)`` with ``BETA0 ≈ +π/2``, i.e. ``q4 ≈ q2 − q3 −
π/2``. With ``q4`` bounded at ±90° that is exactly **``q2 ≥ q3``** — a
STRUCTURAL restriction on the strict-vertical family, not a tuning choice. It is
also why the reachable top-down band is an annulus and not a disc.

FRAMES — the one thing to keep straight:

* The URDF's reachable half-disc lies on **−x** (``joint1``'s fixed
  ``rpy = (0, 0, −1.5708)`` plus the ``(0, 0, −1)`` axes put it there). The
  solver therefore works in the **WORLD frame = URDF base_link rotated 180°
  about Z** (front = +x, like the OMX and like edu6), so every Roboter Studio
  convention (board placement, destinations, zones, German copy) carries over
  unchanged.
* ``solve()`` takes WORLD coordinates and returns **URDF-native joints** (what
  ``/joint_states``, the trajectory rail and the web twin speak). ``fk()`` takes
  URDF-native joints and returns the WORLD-frame TCP pose.
* Unlike the edu6, ``joint1``'s axis passes through the base origin (measured
  offset 0.0 m), so :attr:`base_axis_x` is 0 and a bearing is measured from the
  origin. The property exists anyway because ``path_guard`` and ``motion`` read
  it generically.
* The TCP is the **CLOSED FINGERTIP** (:data:`_L_TOOL` = 0.08625 m below the
  ``end_effector`` origin along the tool axis = the 21.25 mm claw pivot offset +
  the 65 mm blade). A grasp targets ``z_table + object_height − grasp_depth``
  exactly like the OMX and the edu6.

  TOUCH-OFF NOTE (rig gate E8): this claw ROTATES, so its tip height depends on
  the jaw opening — 86.25 mm below the EE origin CLOSED, 68.8 mm at 0.9 rad
  open. „Tisch vermessen" must therefore be taught with the claw CLOSED. The
  model is pessimistic in the safe direction everywhere else: an OPEN claw's
  lowest point is always ABOVE the TCP this file reports.

ROLL contract (jaw alignment): motion's shared formula
``roll = base_yaw − tag_yaw + GRASP_ROLL_RAD`` is consumed here as
``q5 = fold(wrap(−roll))``. Derivation: at a vertical pose the jaws separate
along the world azimuth ``χ = π/2 + φ + q5`` where ``φ`` is the arm-plane
azimuth (``= base_yaw``), so ``q5 = −roll`` yields ``χ ≡ tag_yaw`` for the
fleet-wide default ``GRASP_ROLL_RAD = π/2`` — the identical geometric behaviour
to the OMX, absorbed entirely by this one mapping. (Same trim direction as the
edu6: ``∂q5/∂GRASP_ROLL = −1``.)

THE JAW FOLD IS MANDATORY HERE, not an optimisation. ``joint5`` is limited to
**±90°** while a tag can present any yaw in 360°, so without folding
``q5`` and ``q5 ∓ π`` into the reachable window HALF of all tag orientations
would simply return ``None``.

IT IS LEGITIMATE, AND THE HONEST NUMBERS ARE THESE (re-measured 2026-09-05 on
the shipped meshes: exact point-to-triangle over the grasping band z_ee
40…87 mm, first-hit inner surfaces only — i.e. what an object between the jaws
can actually reach):

* The two blades ARE mirror images, but **about the claw's own centreline, not
  about the tool axis.** The blade band spans ``x_ee ∈ [−25.385, +23.785] mm``
  at the catalog close, i.e. it is centred on **−0.800 mm** — each blade
  individually, not merely the pair. About THAT axis the contacting profiles
  agree to **max 0.52 mm** (p99 0.06, mean 0.005).
* About the **TOOL axis** — which is what "a 180° roll" means, the roll joint's
  axis passing through the EE origin — they agree to **max 1.74 mm** (p99 1.52,
  mean 0.23). That is the number this docstring is about, and it is ~4× the one
  it used to quote.
* Whole-mesh, tool axis: **5.76 mm**, and the ATTRIBUTION is right — the worst
  1 % of vertices sit at z_ee 12.7…26.6 mm, straddling the ``RL_joint`` pivot at
  21.25 mm, i.e. the servo-horn boss on the driven blade. It never touches an
  object; the contacting band never exceeds 1.74 mm.

CONSEQUENCE, and it is a real 1.6 mm rather than a rounding note: the TCP is
MODELLED on the roll axis but the physical closed fingertip sits at
``x_ee = −0.800 mm``, so the two folded twins seat an object
**2 × 0.800 = 1.600 mm apart along the jaw WIDTH axis** (``x_ee``; the blade is
48.5 mm wide there, so the object stays well inside the jaw — it is a seating
bias, not a miss). The jaw CLOSING axis is unaffected: the ``y_ee`` centre
offset is ≤ 0.06 mm. Either twin also carries a fixed ±0.800 mm TCP model error
whether or not the fold fires.

THE FOLD'S BEHAVIOUR IS EXACT REGARDLESS. A 180° rotation about ANY axis
parallel to ``z_ee`` maps ±x_ee → ∓x_ee and ±y_ee → ∓y_ee, so both the jaw line
and the jaw-width line are preserved; only their signs flip. Measured over
20 000 random poses: a folded twin's jaw azimuth differs from the unfolded one's
by **0.0 rad exactly** (mod π), and the roll contract ``jaw azimuth ≡ tag_yaw``
holds to **7.2e-6 rad** — CAD right-angle rounding, the same source as
``_FK_TOL_M`` below.

HISTORICAL NOTE, because the number is quotable and was quoted: the **0.46 mm**
this file used to claim came from comparing the two inner faces' ``|y_ee|`` at
matched ``z_ee``. That metric reads only ``|y|`` and ``z``, so it is
STRUCTURALLY BLIND to an x offset — translating one blade a full METRE along
``x_ee`` leaves it bit-for-bit unchanged — and it has no stable value either
(0.03…0.80 mm depending purely on the z-binning). It was measuring the
CENTRE-axis symmetry and being read as the TOOL-axis symmetry.

Geometry constants are transcribed from ``5dof_assembly_urdf2.urdf`` (external
CAD export; an in-repo copy ships at
``physical_ai_manager/public/edu1-urdf/edu1.urdf`` — the independent FK oracle
in ``test_edu1_ik.py`` parses THAT file, so a solver-constant transcription
error cannot verify itself).
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np


# ── Kinematic constants (metres / radians) — URDF parent→child transcription ─
# joint origins (xyz) + fixed rpy; axes are the URDF's, applied after the rpy.
_J1_XYZ = (0.0, 0.0, 0.0492)
_J1_RPY = (0.0, 0.0, -1.5708)
_J2_XYZ = (0.0, 0.0, 0.04075)
_J2_RPY = (-1.5708, 0.0, 1.5708)
_J3_XYZ = (0.1555, -0.032, 0.0)
_J3_RPY = (3.1416, 0.0, -1.5708)
_J4_XYZ = (0.0, 0.222499999999999, 0.0)
_J4_RPY = (0.0, 0.0, 0.0)
_J5_XYZ = (0.0, 0.0609499999999672, 0.0)
_J5_RPY = (-1.5707963267949, 0.0, 0.0)
# Every joint turns about its own frame's −z (SolidWorks exported them that way).
_AXES = ((0, 0, -1), (0, 0, -1), (0, 0, -1), (0, 0, -1), (0, 0, -1))

# joint1's axis passes through the base origin on this arm (x = y = 0 exactly),
# unlike the edu6's 21.3 mm offset. Kept as a named constant because motion and
# path_guard read ``ik.base_axis_x`` generically.
BASE_AXIS_X_WORLD = 0.0

# Tool: ``end_effector`` is FIXED to link5 at zero offset, and the closed claw
# tip sits on the tool axis (+z of the end_effector frame) at the joint pivot
# 0.02125 m plus the 0.065 m blade. Measured on the STLs, the two tips meet at
# (x_ee −0.8 mm, y_ee 0.0 mm) — 0.8 mm off the axis, which is the same order as
# the OMX's own ~1.6 mm and is ignored for the same reason. NOTE this is the
# SAME 0.800 mm the jaw-fold note below is about: on its own it is a fixed TCP
# model error, and the fold turns it into a 1.600 mm difference BETWEEN the two
# folded twins.
_L_TOOL = 0.02125 + 0.065

# FK∘IK acceptance for a returned solve().
#
# 1e-5, NOT the edu6's 1e-6, and the extra decade is a property of the CAD
# export rather than of this solver: SolidWorks rounded every right angle in
# this URDF to ``1.5708`` / ``3.1416`` (the edu6 export kept full precision), so
# the chain is planar only to ~1.5e-6 m while the closed form assumes it is
# planar exactly. Measured worst residual over a 2000-pose FK→IK→FK round trip:
# 3.5e-6 m. That is 200× below one servo tick at full reach (4096 ticks/rev over
# 0.38 m ≈ 0.58 mm), so it is float bookkeeping, not model error — but at 1e-6
# it silently rejected EVERY solve, which reads as "the whole workspace is
# unreachable". Do not tighten without re-exporting the URDF at full precision.
_FK_TOL_M = 1e-5


# ── URDF joint position limits (radians) — the servo-EEPROM design values ────
# NOTE these are the SHIPPED URDF's values, which write every right angle at
# four decimals (1.5708 / 3.1416) where the raw CAD export wrote two. joint5 is
# why that matters and not merely tidy: its window IS the jaw-fold window, so at
# ±1.57 a required roll of exactly ±90° — a cube square-on to the arm's own
# bearing, which is common, not a corner case — fell 0.0008 rad outside BOTH
# folded twins and the grasp was refused as unreachable. Half a servo tick;
# nothing physical changes.
_EDU1_JOINT_LIMITS_RAD: list[tuple[float, float]] = [
    (-1.5708, 1.5708),     # joint1  base yaw
    (0.0, 3.1416),         # joint2  shoulder
    (0.0, 3.1416),         # joint3  elbow
    (-1.5708, 1.5708),     # joint4  wrist pitch
    (-1.5708, 1.5708),     # joint5  tool roll — the jaw-fold window IS ±90°
]

# joint5 jaw-symmetry fold. See the module docstring: on this arm the fold is
# what makes 360° of tag yaw reachable at all through a ±90° roll joint, and it
# is sound because the CONTACTING blade profiles are mirror-symmetric — about
# the CLAW'S OWN centreline to ≤0.52 mm, and about the ROLL axis (which is what
# the fold actually rotates about) to ≤1.74 mm, the difference being the
# 0.800 mm the claw sits off that axis. A folded grasp therefore seats an object
# 1.600 mm further along the jaw WIDTH than an unfolded one; the jaw LINE is
# identical to float noise. Reference is ZERO, never the seed — same conclusion the
# edu6 reached and for the same reason (a seed-relative choice feeds its own
# output back in and drifts toward the map edge), and here it additionally keeps
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
# authoritative bound is always ``solve(...) is not None``, because the ±90°
# joint4 window clips this annulus further).
_REACH_MIN = abs(_L2 - _L3)
_REACH_MAX = _L2 + _L3


class Edu1IKSolver:
    """Closed-form Cartesian→joint solver for the edu1_studio arm.

    Public surface mirrors the OMX :class:`IKSolver` and the
    :class:`~physical_ai_server.workflow.edu6_ik.Edu6IKSolver` (``solve`` /
    ``solve_quat`` / ``fk`` / ``link_frames`` / ``link_points`` /
    ``in_workspace`` / ``base_yaw`` / ``roll_from_joints`` / ``num_joints`` /
    ``backend`` / ``joint_limits`` / ``base_axis_x``) so
    ``ArmProfile.build_ik`` swaps it in transparently.
    """

    def __init__(self, urdf_string: Optional[str] = None) -> None:
        # ``urdf_string`` accepted for factory-signature compatibility; the
        # baked constants are verified by the independent URDF-parsing oracle
        # in test_edu1_ik.py instead of a runtime parse.
        self._joint_limits = list(_EDU1_JOINT_LIMITS_RAD)

    # ── identity ─────────────────────────────────────────────────────────────
    @property
    def backend(self) -> str:
        return 'closed-form-edu1'

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

        A PUBLIC method on purpose, for the same reason the edu6 solver's is:
        ``arm_geometry`` must not reach into ``_fk_chain`` across a module
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
        interpolations per link — the same contract as the OMX and edu6 solvers
        (consumed by ``path_guard``)."""
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
        # The elbow-DOWN branch is in fact UNREACHABLE for any target at or
        # above the table, and it is kept anyway. Proof sketch, since "dead
        # code" is exactly the conclusion that would get it deleted: g = −|g|
        # gives q3 = _G_OFFSET + |g|, which needs |g| ≤ 0.203 to stay under π,
        # which needs ρ ≥ 0.379; and q4 ≥ −π/2 then forces q2 ≥ q3 ≥ 2.94,
        # hence ψ ≥ 1.47, hence r ≥ 9.9·d_v ≥ 0.57 m — beyond ρ's own 0.381 m
        # ceiling. Contradiction, so no target satisfies both. Measured: 0 of
        # 20 000 sampled targets (test_the_elbow_down_branch_is_unreachable).
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
        call-site-compat contract as the OMX and edu6 solvers."""
        return self.solve(target_xyz, seed=seed, free_yaw=free_yaw)

    def _fold_jaw(self, theta5: float) -> float:
        """Choose between the jaw-identical wrist angles ``theta5`` and
        ``theta5 ∓ π`` — the SAME physical grasp (see :data:`_J5_JAW_FOLD_RAD`)
        — taking the one nearest ZERO.

        On this arm the fold is what makes the full 360° of tag yaw reachable
        through a ±90° joint at all: ``_wrap`` lands in (−π, π], and for every
        value outside ±90° exactly one of the two twins falls inside, so the
        fold never loses a target. Takes NO seed on purpose (a seed-relative
        choice drifts; measured on the edu6, same conclusion). ``min`` keeps the
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

        NOT ``theta1``. On the OMX and the edu6 the two happen to be the same
        number; here ``joint1``'s axis is ``(0, 0, −1)``, so ``solve`` returns
        ``theta1 = −base_yaw(x, y)``. What every caller actually wants is the
        AZIMUTH — ``motion.grasp_joint5`` composes it with the tag yaw in world
        terms (``roll = base_yaw − tag_yaw + GRASP_ROLL``) and never touches a
        joint — so this method keeps the geometric meaning and the sign flip
        stays inside ``solve``. Handing back ``theta1`` here would mirror every
        tag-tracked grasp.

        Returns a value in ``(−π, π]`` (``atan2`` range). Not gated on
        reachability — callers use :meth:`in_workspace` / :meth:`solve`."""
        return math.atan2(float(y), float(x) - BASE_AXIS_X_WORLD)

    def roll_from_joints(self, joints) -> Optional[float]:
        """INVERSE of the ``roll`` → joint5 mapping: the ``roll`` to hand
        :meth:`solve` so the wrist comes back where ``joints`` has it.

        ``solve`` maps ``q5 = fold(wrap(−roll))``, so the roll argument and the
        joint value are DIFFERENT NUMBERS on this arm — as on the edu6, and
        unlike the OMX where ``theta5 = roll`` is the identity. A "move the
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

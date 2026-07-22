#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Closed-form analytical inverse kinematics for the OMX-F arm.

The OMX-F (OpenMANIPULATOR-X follower) is a **5-DOF** arm:

* ``joint1`` — yaw about base z (aims the arm at the target column)
* ``joint2/3/4`` — pitch about y, **coplanar** (shoulder / elbow / wrist-pitch)
* ``joint5`` — roll about the forearm x-axis (tool roll)
* (``gripper`` is a separate open/close servo, not a positioning DOF)

Roboter Studio does **top-down** table-top picking, so this solver computes a
single **strict-vertical (straight-down) grasp** configuration: the gripper
approaches the target pointing straight down, ``joint5`` rolls the tool to a
requested angle (e.g. across an elongated object's short axis), and the elbow
is fixed **elbow-up** for a deterministic, smooth, repeatable posture.

Why closed-form (and nothing else):

* **Deterministic** — same input → bit-identical output. No RNG, no wall-clock
  timeout, no thread race. This is what kills the erratic "crazy motion" that
  orientation-unconstrained numerical IK (KDL-LMA / TRAC-IK free-yaw) produced:
  those flip between contorted configs between consecutive targets.
* **No fragile dependency** — pure Python + NumPy. No ``tracikpy`` SWIG/ROS-lib
  build (the Fast-CDR ABI-crash class), no IKFast generation (which fails to
  generate for this non-spherical-wrist + bent-link geometry).
* **Verifiable** — an exact NumPy forward kinematics (``fk``) is built from the
  same URDF constants, so ``fk(solve(p)) ≈ p`` is a pure-Python CI gate
  (``test_ik_solver.py``) that needs no container / PyKDL.

The arm physically cannot hold an arbitrary 6-DOF orientation (tool yaw is
locked to ``joint1``), and strict-vertical reach is a table-top **annulus**.
The wrist-centre 2R span bounds the reach at ``|L2−L3| ≈ 0.04`` to
``L2+L3 ≈ 0.28`` m from the shoulder; projected onto the table plane (z=0,
straight-down grasp) the inner reachable radius is ~0.04 m and the practical
outer radius is ~0.26 m (rig/CI-verified: ``solve`` returns None at 0.03 m,
reachable from ~0.04 m). The classroom mat marks the usable ring; the
authoritative bound is always ``solve(...) is not None``, not a hard-coded
radius. A target outside the annulus / joint limits returns ``None`` (the
motion handler surfaces a clean German "unreachable" message) — this is a
property of the hardware, not a fallback.

Geometry constants are transcribed from
``open_manipulator/open_manipulator_description/urdf/omx_f/omx_f.urdf`` (parent
→ child joint-origin xyz). When a ``urdf_string`` is supplied the constants are
**verified** against it at construction (a mismatch logs loudly) so a future
URDF edit can't silently invalidate the hand-derived math.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

import numpy as np

_logger = logging.getLogger(__name__)


# ── Kinematic constants (metres) — omx_f.urdf parent→child joint origins ─────
# link0 → joint1 (axis z); link1 → joint2 (axis y); link2 → joint3 (axis y,
# the BENT link); link3 → joint4 (axis y); link4 → joint5 (axis x); link5 →
# end_effector_link (fixed).
_J1_XYZ = (-0.01125, 0.0, 0.034)
_J2_XYZ = (0.0, 0.0, 0.0635)
_J3_XYZ = (0.0415, 0.0, 0.11315)
_J4_XYZ = (0.162, 0.0, 0.0)
_J5_XYZ = (0.0287, 0.0, 0.0)
_EE_XYZ = (0.09193, -0.0016, 0.0)

# Derived planar parameters (in the joint-1 vertical plane):
#   shoulder (joint2) height above base:
_SHOULDER_Z = _J1_XYZ[2] + _J2_XYZ[2]                 # 0.0975 m
#   shoulder horizontal offset along base-x of the joint-1 axis:
_J1_AXIS_X = _J1_XYZ[0]                                # -0.01125 m
#   upper arm joint2→joint3: length + bent-link offset angle from vertical:
_L2 = math.hypot(_J3_XYZ[0], _J3_XYZ[2])              # 0.12053 m
_PHI0 = math.atan2(_J3_XYZ[0], _J3_XYZ[2])            # 0.35155 rad (20.14 deg)
#   forearm joint3→joint4:
_L3 = _J4_XYZ[0]                                       # 0.162 m
#   tool joint4→end_effector along the forearm x-axis (the ~1.6 mm lateral
#   y-offset of the EE is ignored — it is perpendicular to the vertical
#   approach and well under the grasp tolerance):
_L_TOOL = _J5_XYZ[0] + _EE_XYZ[0]                     # 0.12063 m

#   gripper fingers reach ~this far past end_effector_link along the tool
#   x-axis; link_points adds a couple of samples there so the no-go-zone swept
#   check covers the jaws, not just the EE frame origin.
_TOOL_TIP_EXT_M = 0.04

#   strict-vertical reach annulus (wrist-center distance from shoulder must lie
#   within the 2R span); used for an early reachability message.
_REACH_MIN = abs(_L2 - _L3)                            # 0.0415 m
_REACH_MAX = _L2 + _L3                                 # 0.2825 m

# Default downward approach: the cumulative pitch joint2+joint3+joint4 that
# points the forearm/tool straight down (-z) is +pi/2.
_VERTICAL_PITCH_SUM = math.pi / 2.0


# ── Real Dynamixel arm joint position limits (radians) ──────────────────────
# Source: docker/open_manipulator/overlays/omx_f.ros2_control.xacro Min/Max
# Position Limit (encoder ticks, 4096/rev, centre 2048 → 0 rad). joint2/3 carry
# explicit Min/Max Position Limit params (transcribed below); joint1/4/5 carry
# no such param, so here they are clamped to a conservative one-turn range
# (-pi, pi) rather than the URDF's full ±2 pi — a single revolution is more
# than the top-down picker needs and keeps j1 from winding multiple turns.
# Authoritative motion clamp (tighter than the URDF ±2 pi).
def _ticks_to_rad(ticks: float) -> float:
    return (ticks - 2048.0) * (2.0 * math.pi / 4096.0)


_DXL_JOINT_LIMITS_RAD: list[tuple[float, float]] = [
    (-math.pi, math.pi),                                   # joint1 (dxl11)
    (_ticks_to_rad(830), _ticks_to_rad(3129)),             # joint2 (dxl12)
    (_ticks_to_rad(1024), _ticks_to_rad(3140)),            # joint3 (dxl13)
    (-math.pi, math.pi),                                   # joint4 (dxl14)
    (-math.pi, math.pi),                                   # joint5 (dxl15)
]

# FK∘IK acceptance: a returned solve() must place the EE within this many
# metres of the requested target (covers the ~1.6 mm structural tool offset +
# float noise). The CI gate uses the same bound.
_FK_TOL_M = 3e-3


def _rot(axis: str, theta: float) -> np.ndarray:
    c, s = math.cos(theta), math.sin(theta)
    if axis == 'x':
        return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=np.float64)
    if axis == 'y':
        return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=np.float64)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=np.float64)  # z


def _tf(xyz: tuple[float, float, float], axis: Optional[str], theta: float) -> np.ndarray:
    T = np.eye(4, dtype=np.float64)
    if axis is not None:
        T[:3, :3] = _rot(axis, theta)
    T[:3, 3] = xyz
    return T


class IKSolver:
    """Closed-form Cartesian→joint solver for the OMX-F arm.

    Constructed once (optionally with the ``/robot_description`` URDF string,
    used only to *verify* the baked constants). ``solve()`` is called per
    motion primitive; ``fk()`` provides exact forward kinematics for
    calibration touch-off / pose readout.
    """

    def __init__(
        self,
        urdf_string: Optional[str] = None,
        base_link: str = 'link0',
        tip_link: str = 'end_effector_link',
    ) -> None:
        self._base_link = base_link
        self._tip_link = tip_link
        self._joint_limits = list(_DXL_JOINT_LIMITS_RAD)
        if urdf_string:
            self._verify_constants(urdf_string)

    @property
    def backend(self) -> str:
        """Identifies the active solver. Always closed-form analytical."""
        return 'closed-form'

    def num_joints(self) -> int:
        """Number of arm joints solved (joint1..joint5)."""
        return 5

    @property
    def joint_limits(self) -> tuple[tuple[float, float], ...]:
        """Per-arm-joint ``(lower, upper)`` position limits in radians.

        The PUBLIC seam for jog/manual range checks — callers must consume this
        (length == ``num_joints()``) instead of importing the module-private
        ``_DXL_JOINT_LIMITS_RAD``, so a different-DOF solver (ArmProfile
        ``build_ik``) transparently supplies its own limits."""
        return tuple(self._joint_limits)

    @property
    def base_axis_x(self) -> float:
        """Base-frame x offset of the joint-1 (yaw) axis in metres.

        The PUBLIC seam for azimuth geometry (``path_guard`` base-swing via
        points, ``base_yaw`` consumers) — replaces importing the
        module-private ``_J1_AXIS_X``."""
        return _J1_AXIS_X

    # ── verification ────────────────────────────────────────────────────────
    def _verify_constants(self, urdf_string: str) -> None:
        """Best-effort: parse the URDF joint origins and warn loudly if they
        diverge from the baked constants the hand-derived math depends on."""
        try:
            from urdf_parser_py.urdf import URDF
            urdf = URDF.from_xml_string(urdf_string)
            origins = {}
            for j in urdf.joints:
                if j.origin is not None and j.origin.xyz is not None:
                    origins[j.name] = tuple(float(v) for v in j.origin.xyz)
            expect = {
                'joint1': _J1_XYZ, 'joint2': _J2_XYZ, 'joint3': _J3_XYZ,
                'joint4': _J4_XYZ, 'joint5': _J5_XYZ,
                'end_effector_joint': _EE_XYZ,
            }
            for name, exp in expect.items():
                got = origins.get(name)
                if got is not None and not all(
                    abs(a - b) < 1e-4 for a, b in zip(got, exp)
                ):
                    _logger.error(
                        'IK constant mismatch for %s: URDF=%s baked=%s — the '
                        'closed-form IK math assumes the baked geometry; update '
                        'ik_solver.py if the arm changed.', name, got, exp,
                    )
        except Exception:  # noqa: BLE001 — verification is advisory only
            _logger.info('URDF constant verification skipped (parse failed).')

    # ── forward kinematics (exact, pure NumPy) ───────────────────────────────
    def _fk_matrix(self, joints) -> Optional[np.ndarray]:
        j = list(joints)
        if len(j) < 5:
            return None
        T = (
            _tf(_J1_XYZ, 'z', j[0])
            @ _tf(_J2_XYZ, 'y', j[1])
            @ _tf(_J3_XYZ, 'y', j[2])
            @ _tf(_J4_XYZ, 'y', j[3])
            @ _tf(_J5_XYZ, 'x', j[4])
            @ _tf(_EE_XYZ, None, 0.0)
        )
        return T

    def fk(self, joints) -> Optional[tuple[np.ndarray, np.ndarray]]:
        """Forward kinematics: ``(R 3x3, t 3,)`` of ``end_effector_link`` in the
        base frame for the given joint vector (joint1..joint5; extra entries
        are ignored), or ``None`` if fewer than 5 joints are supplied."""
        T = self._fk_matrix(joints)
        if T is None:
            return None
        return T[:3, :3].copy(), T[:3, 3].copy()

    def _fk_position(self, joints) -> Optional[np.ndarray]:
        T = self._fk_matrix(joints)
        return None if T is None else T[:3, 3].copy()

    def link_points(self, joints, samples_per_link: int = 5) -> Optional[list[np.ndarray]]:
        """Sample points along the arm's links in the BASE frame, for a swept
        obstacle (no-go-zone) check.

        Returns a list of base-frame 3-vectors covering: the base origin, every
        joint origin, the end-effector, ``samples_per_link`` points linearly
        interpolated along each consecutive link (so a thin obstacle can't hide
        *between* two joints), and two points projected past the EE along the
        tool x-axis (the gripper fingers reach past ``end_effector_link``).
        ``None`` when fewer than 5 joints are supplied (mirrors :meth:`fk`;
        extra entries are ignored).

        Pure NumPy — it accumulates the SAME cumulative-product chain as
        ``_fk_matrix``, so it is CI-testable exactly like ``fk`` (no container /
        ROS deps). Used by ``workflow/path_guard.py``.
        """
        j = list(joints)
        if len(j) < 5:
            return None
        T1 = _tf(_J1_XYZ, 'z', j[0])
        T2 = T1 @ _tf(_J2_XYZ, 'y', j[1])
        T3 = T2 @ _tf(_J3_XYZ, 'y', j[2])
        T4 = T3 @ _tf(_J4_XYZ, 'y', j[3])
        T5 = T4 @ _tf(_J5_XYZ, 'x', j[4])
        TEE = T5 @ _tf(_EE_XYZ, None, 0.0)

        origins = [
            np.zeros(3, dtype=np.float64),   # base (link0)
            T1[:3, 3].copy(),                # joint1
            T2[:3, 3].copy(),                # joint2 (shoulder)
            T3[:3, 3].copy(),                # joint3 (elbow)
            T4[:3, 3].copy(),                # joint4 (wrist)
            T5[:3, 3].copy(),                # joint5
            TEE[:3, 3].copy(),               # end-effector
        ]
        n = max(1, int(samples_per_link))
        pts: list[np.ndarray] = [origins[0]]
        for a, b in zip(origins[:-1], origins[1:]):
            for k in range(1, n + 1):
                pts.append(a + (b - a) * (k / n))
        # Gripper fingers extend past the EE along the tool x-axis.
        tool_axis = TEE[:3, 0]
        nrm = float(np.linalg.norm(tool_axis))
        if nrm > 1e-9:
            tool_axis = tool_axis / nrm
            ee = TEE[:3, 3]
            pts.append(ee + tool_axis * (_TOOL_TIP_EXT_M * 0.5))
            pts.append(ee + tool_axis * _TOOL_TIP_EXT_M)
        return pts

    # ── inverse kinematics (closed form, strict vertical top-down) ───────────
    def solve(
        self,
        target_xyz: tuple[float, float, float] | np.ndarray,
        target_rpy: tuple[float, float, float] = (math.pi, 0.0, 0.0),
        seed: Optional[list[float]] = None,
        free_yaw: bool = True,
        roll: Optional[float] = None,
    ) -> Optional[list[float]]:
        """Solve the 5 arm joints for a **strict-vertical** (straight-down)
        grasp at ``target_xyz`` (base frame, metres).

        ``roll`` sets joint5 (tool roll about the vertical approach); when
        ``None`` it is taken from ``target_rpy``'s yaw if given, else 0.0.
        ``seed``/``free_yaw``/``target_rpy`` are accepted for call-site
        compatibility but do not change the deterministic position solution
        (the picker is always top-down, elbow-up). Returns the joint list, or
        ``None`` when the point is outside the reachable annulus / joint
        limits.
        """
        x, y, z = (float(v) for v in np.asarray(target_xyz, dtype=np.float64).reshape(3))

        if roll is None:
            # target_rpy = (roll, pitch, yaw); only the yaw maps to tool roll
            # about the vertical approach. Default 0.0.
            roll = float(target_rpy[2]) if len(target_rpy) >= 3 else 0.0

        # joint1 aims the j1 plane at the target. The shoulder sits on the j1
        # axis (offset _J1_AXIS_X along base-x), so the radial direction is
        # measured from that axis.
        dx = x - _J1_AXIS_X
        theta1 = math.atan2(y, dx)
        r = math.hypot(dx, y)                         # radial distance from j1 axis

        # Wrist centre (joint4) sits directly above the grasp by the tool
        # length (tool points straight down).
        w_u = r                                       # radial coord of wrist centre
        w_v = z + _L_TOOL                             # vertical coord of wrist centre

        # 2R chain (bent upper arm _L2 at intrinsic offset _PHI0, forearm _L3)
        # from the shoulder (radial 0, vertical _SHOULDER_Z) to the wrist centre.
        d_u = w_u
        d_v = w_v - _SHOULDER_Z
        rho = math.hypot(d_u, d_v)
        if rho < _REACH_MIN - 1e-9 or rho > _REACH_MAX + 1e-9:
            return None                               # outside the 2R span

        cos_gamma = (rho * rho - _L2 * _L2 - _L3 * _L3) / (2.0 * _L2 * _L3)
        cos_gamma = max(-1.0, min(1.0, cos_gamma))
        gamma_mag = math.acos(cos_gamma)              # elbow bend magnitude

        # Try elbow-up first (the natural picking posture), then elbow-down —
        # deterministic order so a given target always yields the same config.
        for gamma in (gamma_mag, -gamma_mag):
            # A = upper-arm direction from vertical; B = forearm direction.
            A = math.atan2(d_u, d_v) - math.atan2(_L3 * math.sin(gamma),
                                                  _L2 + _L3 * math.cos(gamma))
            B = A + gamma
            theta2 = A - _PHI0
            theta3 = (B - math.pi / 2.0) - theta2
            theta4 = _VERTICAL_PITCH_SUM - (theta2 + theta3)
            theta5 = roll
            joints = [theta1, theta2, theta3, theta4, _wrap(theta5)]
            # HIGH-1: reject a NON-FINITE solution (a NaN target or NaN roll
            # yields NaN joints). Without this the FK-tolerance check below fails
            # OPEN — `NaN > tol` is False, so `solve` would return all-NaN joints
            # instead of None ("unreachable"), and the NaN would be commanded
            # downstream (to /sim/joint_states, or the real follower if an
            # upstream world coordinate ever went non-finite).
            if not all(math.isfinite(v) for v in joints):
                continue
            if not self._within_limits(joints):
                continue
            # Verify with exact FK (catches any derivation/sign error and the
            # tiny structural tool offset).
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
        target_xyz: tuple[float, float, float] | np.ndarray,
        target_quat: tuple[float, float, float, float],
        seed: Optional[list[float]] = None,
        free_yaw: bool = False,
    ) -> Optional[list[float]]:
        """Position-only solve (orientation argument ignored).

        The arm cannot hold an arbitrary board-facing orientation, and the only
        autonomous-motion calibration that used an orientation target
        (auto-pose) is removed in the single-shot calibration redesign. Kept
        for call-site compatibility; resolves the same strict-vertical config
        as ``solve``."""
        return self.solve(target_xyz, seed=seed, free_yaw=free_yaw)

    # ── limits ───────────────────────────────────────────────────────────────
    def _within_limits(self, joints, tol: float = 1e-6) -> bool:
        for i in range(min(len(joints), len(self._joint_limits))):
            lo, hi = self._joint_limits[i]
            if joints[i] < lo - tol or joints[i] > hi + tol:
                return False
        return True

    def in_workspace(self, target_xyz) -> bool:
        """True when ``target_xyz`` yields a reachable strict-vertical grasp.
        Used for the workflow IK pre-check / a clean German message."""
        return self.solve(target_xyz) is not None

    def base_yaw(self, x: float, y: float) -> float:
        """Base joint (joint1) yaw, in radians, that aims the arm's vertical
        plane at the table point ``(x, y)`` (base frame, metres).

        This is **exactly** the ``theta1`` that :meth:`solve` computes (the
        shoulder sits on the joint-1 axis, offset ``_J1_AXIS_X`` along base-x,
        so the bearing is measured from that axis — NOT from the base origin).
        Exposed for the named-object grasp orientation math, where the live
        tool roll is ``joint5 = base_yaw(x, y) − tag_yaw + GRASP_ROLL``.

        Returns a value in ``(−π, π]`` (``atan2`` range). It is **not** gated on
        reachability — callers use :meth:`in_workspace` / :meth:`solve` for
        that. Sign-trap (matches ``solve``): use ``x − _J1_AXIS_X``
        (numerically ``x + 0.01125``), never ``atan2(y, x)``."""
        return math.atan2(float(y), float(x) - _J1_AXIS_X)


def _wrap(a: float) -> float:
    """Wrap an angle to (-pi, pi]."""
    return (a + math.pi) % (2.0 * math.pi) - math.pi

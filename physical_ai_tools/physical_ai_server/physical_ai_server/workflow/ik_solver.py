#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Inverse kinematics for the OMX-F arm: TRAC-IK preferred, PyKDL fallback.

The solver returns the 5 arm joints (joint1..joint5); the gripper is
appended separately by the motion handlers from the workflow primitives.

When ``free_yaw=True`` the bounds on the rotational tolerance around the
end-effector's z-axis (``brz``) are relaxed to a full revolution, which
lets the picker grab table-top objects from any approach angle. The
positional tolerances (``bx,by,bz``) and the planar rotation tolerances
(``brx,bry``) stay tight so the gripper still arrives at the requested
point with the requested approach direction.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

import numpy as np

_logger = logging.getLogger(__name__)


_BX = 1e-3
_BY = 1e-3
_BZ = 1e-3
_BRX = 1e-2
_BRY = 1e-2
_BRZ_FREE_YAW = 2 * math.pi
_BRZ_LOCKED = 1e-2

_DEFAULT_TIMEOUT_S = 0.05
_DEFAULT_RETRIES = 2
_SEED_PERTURBATION_RAD = 0.10


class IKSolver:
    """Cartesian-pose -> joint solver. Constructed once with the URDF
    string; ``solve()`` is called per motion primitive."""

    def __init__(
        self,
        urdf_string: str,
        base_link: str = 'link0',
        tip_link: str = 'end_effector_link',
    ) -> None:
        self._base_link = base_link
        self._tip_link = tip_link
        self._urdf_string = urdf_string
        self._tracik = self._try_init_tracik()
        self._kdl = None
        if self._tracik is None:
            self._kdl = self._try_init_kdl()
        if self._tracik is None and self._kdl is None:
            raise RuntimeError(
                'Neither TRAC-IK nor PyKDL is available — install '
                'ros-jazzy-trac-ik-python or ros-jazzy-python-orocos-kdl-vendor.'
            )

    def _try_init_tracik(self):
        # Distinguish "package not installed" (expected on Jazzy until apt
        # ships ros-jazzy-trac-ik-python) from "URDF/chain misconfigured"
        # (a bug the operator must see). Both reduce to "no TRAC-IK" but
        # only the latter is worth a warning in container logs.
        try:
            from trac_ik_python.trac_ik import IK
        except ImportError:
            _logger.info(
                'TRAC-IK Python bindings not installed; '
                'falling back to PyKDL.'
            )
            return None
        try:
            return IK(
                self._base_link,
                self._tip_link,
                urdf_string=self._urdf_string,
                timeout=_DEFAULT_TIMEOUT_S,
                solve_type='Distance',
            )
        except Exception:
            _logger.exception(
                'TRAC-IK construction failed (base=%s tip=%s) — '
                'falling back to PyKDL. Check the URDF and link names.',
                self._base_link, self._tip_link,
            )
            return None

    def _try_init_kdl(self):
        try:
            import PyKDL
            from urdf_parser_py.urdf import URDF
            from kdl_parser_py.urdf import treeFromUrdfModel
        except ImportError:
            _logger.info(
                'PyKDL or kdl_parser_py not installed; IK disabled.'
            )
            return None
        try:
            urdf = URDF.from_xml_string(self._urdf_string)
            ok, tree = treeFromUrdfModel(urdf)
            if not ok:
                _logger.error(
                    'kdl_parser_py.treeFromUrdfModel returned ok=False — '
                    'URDF is malformed; IK disabled.'
                )
                return None
            chain = tree.getChain(self._base_link, self._tip_link)
            return {
                'PyKDL': PyKDL,
                'chain': chain,
                'fk_solver': PyKDL.ChainFkSolverPos_recursive(chain),
                'ik_solver': PyKDL.ChainIkSolverPos_LMA(chain),
                'num_joints': chain.getNrOfJoints(),
            }
        except Exception:
            _logger.exception(
                'PyKDL chain construction failed (base=%s tip=%s) — '
                'IK disabled. Check the URDF and link names.',
                self._base_link, self._tip_link,
            )
            return None

    def num_joints(self) -> int:
        """Return the number of joints in the IK chain (excludes any extra
        passive/gripper joints)."""
        if self._tracik is not None:
            return int(self._tracik.number_of_joints)
        if self._kdl is not None:
            return int(self._kdl['num_joints'])
        return 0

    def fk(self, joints) -> Optional[tuple[np.ndarray, np.ndarray]]:
        """Forward kinematics: returns ``(R 3x3, t 3,)`` of the end-effector
        in base frame for the given joint vector, or ``None`` when FK is
        unavailable (no PyKDL backend) or the input shape doesn't match
        the IK chain. Always uses the PyKDL FK chain; lazily builds one
        if only TRAC-IK was initialised."""
        if self._kdl is None:
            self._kdl = self._try_init_kdl()
        if self._kdl is None:
            return None
        n = self._kdl['num_joints']
        joints = list(joints)
        if len(joints) < n:
            return None
        PyKDL = self._kdl['PyKDL']
        q = PyKDL.JntArray(n)
        for i in range(n):
            q[i] = float(joints[i])
        frame = PyKDL.Frame()
        rc = self._kdl['fk_solver'].JntToCart(q, frame)
        if rc != 0:
            return None
        R = np.array([
            [frame.M[0, 0], frame.M[0, 1], frame.M[0, 2]],
            [frame.M[1, 0], frame.M[1, 1], frame.M[1, 2]],
            [frame.M[2, 0], frame.M[2, 1], frame.M[2, 2]],
        ], dtype=np.float64)
        t = np.array([frame.p.x(), frame.p.y(), frame.p.z()], dtype=np.float64)
        return R, t

    @staticmethod
    def _rpy_to_rotation(rpy: tuple[float, float, float]) -> np.ndarray:
        roll, pitch, yaw = rpy
        cr, sr = math.cos(roll), math.sin(roll)
        cp, sp = math.cos(pitch), math.sin(pitch)
        cy, sy = math.cos(yaw), math.sin(yaw)
        return np.array([
            [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
            [-sp,     cp * sr,                cp * cr],
        ])

    @staticmethod
    def _quat_to_rotation(quat: tuple[float, float, float, float]) -> np.ndarray:
        """Convert (qx, qy, qz, qw) to a 3x3 rotation matrix."""
        qx, qy, qz, qw = quat
        norm = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
        if norm < 1e-9:
            return np.eye(3)
        qx /= norm
        qy /= norm
        qz /= norm
        qw /= norm
        return np.array([
            [1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw),     2 * (qx * qz + qy * qw)],
            [2 * (qx * qy + qz * qw),     1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw)],
            [2 * (qx * qz - qy * qw),     2 * (qy * qz + qx * qw),     1 - 2 * (qx * qx + qy * qy)],
        ])

    @staticmethod
    def _rotation_to_quaternion(R: np.ndarray) -> tuple[float, float, float, float]:
        trace = R[0, 0] + R[1, 1] + R[2, 2]
        if trace > 0.0:
            s = math.sqrt(trace + 1.0) * 2.0
            qw = 0.25 * s
            qx = (R[2, 1] - R[1, 2]) / s
            qy = (R[0, 2] - R[2, 0]) / s
            qz = (R[1, 0] - R[0, 1]) / s
        elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
            s = math.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2]) * 2.0
            qw = (R[2, 1] - R[1, 2]) / s
            qx = 0.25 * s
            qy = (R[0, 1] + R[1, 0]) / s
            qz = (R[0, 2] + R[2, 0]) / s
        elif R[1, 1] > R[2, 2]:
            s = math.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2]) * 2.0
            qw = (R[0, 2] - R[2, 0]) / s
            qx = (R[0, 1] + R[1, 0]) / s
            qy = 0.25 * s
            qz = (R[1, 2] + R[2, 1]) / s
        else:
            s = math.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1]) * 2.0
            qw = (R[1, 0] - R[0, 1]) / s
            qx = (R[0, 2] + R[2, 0]) / s
            qy = (R[1, 2] + R[2, 1]) / s
            qz = 0.25 * s
        return qx, qy, qz, qw

    def solve(
        self,
        target_xyz: tuple[float, float, float] | np.ndarray,
        target_rpy: tuple[float, float, float] = (math.pi, 0.0, 0.0),
        seed: Optional[list[float]] = None,
        free_yaw: bool = True,
    ) -> Optional[list[float]]:
        target_xyz = np.asarray(target_xyz, dtype=np.float64).reshape(3)
        R = self._rpy_to_rotation(target_rpy)
        qx, qy, qz, qw = self._rotation_to_quaternion(R)

        if self._tracik is not None:
            return self._solve_tracik(target_xyz, (qx, qy, qz, qw), seed, free_yaw)
        return self._solve_kdl(target_xyz, R, seed)

    def solve_quat(
        self,
        target_xyz: tuple[float, float, float] | np.ndarray,
        target_quat: tuple[float, float, float, float],
        seed: Optional[list[float]] = None,
        free_yaw: bool = False,
    ) -> Optional[list[float]]:
        """Quaternion-input variant for the calibration auto-pose flow.

        ``target_quat`` is (qx, qy, qz, qw) in the same convention as
        ``geometry_msgs/Quaternion``. ``free_yaw`` defaults to ``False``
        because calibration captures need the board to face the camera
        from a specific orientation; relaxing yaw would let the gripper
        swing past the board.
        """
        target_xyz = np.asarray(target_xyz, dtype=np.float64).reshape(3)
        R = self._quat_to_rotation(target_quat)
        if self._tracik is not None:
            return self._solve_tracik(target_xyz, target_quat, seed, free_yaw)
        return self._solve_kdl(target_xyz, R, seed)

    def _solve_tracik(
        self,
        xyz: np.ndarray,
        quat: tuple[float, float, float, float],
        seed: Optional[list[float]],
        free_yaw: bool,
    ) -> Optional[list[float]]:
        ik = self._tracik
        num_joints = ik.number_of_joints
        if seed is None or len(seed) != num_joints:
            seed = [0.0] * num_joints

        brz = _BRZ_FREE_YAW if free_yaw else _BRZ_LOCKED
        rng = np.random.default_rng()
        for attempt in range(_DEFAULT_RETRIES + 1):
            try:
                seed_attempt = seed if attempt == 0 else [
                    s + float(rng.uniform(-_SEED_PERTURBATION_RAD, _SEED_PERTURBATION_RAD))
                    for s in seed
                ]
                solution = ik.get_ik(
                    seed_attempt,
                    float(xyz[0]), float(xyz[1]), float(xyz[2]),
                    quat[0], quat[1], quat[2], quat[3],
                    _BX, _BY, _BZ, _BRX, _BRY, brz,
                )
                if solution is not None:
                    return list(solution)
            except Exception:
                continue
        return None

    def _solve_kdl(
        self,
        xyz: np.ndarray,
        R: np.ndarray,
        seed: Optional[list[float]],
    ) -> Optional[list[float]]:
        kdl = self._kdl
        if kdl is None:
            return None
        PyKDL = kdl['PyKDL']
        num_joints = kdl['num_joints']
        if seed is None or len(seed) != num_joints:
            seed = [0.0] * num_joints

        seed_array = PyKDL.JntArray(num_joints)
        for i, s in enumerate(seed):
            seed_array[i] = s

        rot = PyKDL.Rotation(
            R[0, 0], R[0, 1], R[0, 2],
            R[1, 0], R[1, 1], R[1, 2],
            R[2, 0], R[2, 1], R[2, 2],
        )
        target = PyKDL.Frame(rot, PyKDL.Vector(float(xyz[0]), float(xyz[1]), float(xyz[2])))

        result = PyKDL.JntArray(num_joints)
        rc = kdl['ik_solver'].CartToJnt(seed_array, target, result)
        if rc != 0:
            return None
        return [result[i] for i in range(num_joints)]

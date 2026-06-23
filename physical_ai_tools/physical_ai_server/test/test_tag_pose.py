"""Tag-pose (base-frame yaw) tests for the named-object grasp workflow.

The IK self-check is position-only, so this is the SOLE automated guard for the
orientation math (plan §23.0). Each test SYNTHESISES the 4 tag corners by
projecting a KNOWN tag pose through a KNOWN camera (intrinsics + extrinsic), then
asserts the recovered base-frame yaw matches — validating the full chain
(corner projection → known-plane back-projection → edge-angle yaw → wrap)
end-to-end and self-consistently. (Whether the assumed corner order matches
pupil_apriltags' REAL corner order is validated on the rig — the jaw-check — not
here; see tag_pose.py's docstring.)

Pure NumPy + OpenCV — runs without a container.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from physical_ai_server.workflow.ik_solver import IKSolver
from physical_ai_server.workflow.tag_pose import (
    grasp_joint5,
    tag_corner_object_points,
    tag_edge_length_base,
    tag_yaw_base,
)


# ── synthesis helpers ────────────────────────────────────────────────────────
def _K(fx=600.0, fy=600.0, cx=320.0, cy=240.0) -> np.ndarray:
    return np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1]], dtype=np.float64)


def _rot_z(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=np.float64)


def _rot_y(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=np.float64)


def _overhead_extrinsic(cam_xyz=(0.20, 0.0, 0.50)) -> np.ndarray:
    """T_cam_to_base for an overhead camera looking straight down (-z base).

    cam +x = base +x, cam +y = base −y, cam +z = base −z (into the scene/down).
    Proper right-handed rotation (det +1)."""
    R = np.array([[1.0, 0.0, 0.0],
                  [0.0, -1.0, 0.0],
                  [0.0, 0.0, -1.0]], dtype=np.float64)
    T = np.eye(4, dtype=np.float64)
    T[:3, :3] = R
    T[:3, 3] = np.asarray(cam_xyz, dtype=np.float64)
    return T


def _project_tag(P_base, yaw, tag_size, K, T_cam_to_base, tilt_y=0.0):
    """Return the 4 tag corners in image pixels (in tag_corner_object_points
    order), for a tag centred at ``P_base`` with base yaw ``yaw`` (+ optional Ry
    tilt), flat/face-up."""
    R_tag_to_base = _rot_z(yaw) @ _rot_y(tilt_y)
    obj = tag_corner_object_points(tag_size)             # (4,3) tag frame
    corners_base = (R_tag_to_base @ obj.T).T + np.asarray(P_base)  # (4,3)
    T_base_to_cam = np.linalg.inv(T_cam_to_base)
    R_bc, t_bc = T_base_to_cam[:3, :3], T_base_to_cam[:3, 3]
    px = []
    for c in corners_base:
        cam = R_bc @ c + t_bc
        assert cam[2] > 0, 'synthesised corner is behind the camera'
        u = K[0, 0] * cam[0] / cam[2] + K[0, 2]
        v = K[1, 1] * cam[1] / cam[2] + K[1, 2]
        px.append([u, v])
    return np.array(px, dtype=np.float64)


# ── flat-tag yaw round-trip (the core guard) ─────────────────────────────────
def test_flat_tag_yaw_roundtrip_full_range():
    K = _K()
    T = _overhead_extrinsic()
    tag, z = 0.024, 0.05
    for yaw in np.linspace(-math.pi + 0.05, math.pi - 0.05, 25):
        px = _project_tag((0.20, 0.0, z), yaw, tag, K, T)
        rec = tag_yaw_base(px, K, None, T, plane_z=z)
        assert rec is not None
        d = (rec - yaw + math.pi) % (2 * math.pi) - math.pi   # on the circle
        assert abs(d) < 1e-5, f'yaw {yaw}: recovered {rec}'


def test_offcenter_tag_yaw_roundtrip_exact():
    # The regime that biased solvePnP (~2.5–3°): off-axis tags. Known-plane is exact.
    K = _K()
    T = _overhead_extrinsic()
    tag = 0.024
    for P in [(0.12, 0.06, 0.04), (0.28, -0.08, 0.03), (0.16, 0.10, 0.06)]:
        for yaw in (-1.2, -0.3, 0.0, 0.7, 1.5):
            px = _project_tag(P, yaw, tag, K, T)
            rec = tag_yaw_base(px, K, None, T, plane_z=P[2])
            assert rec is not None
            d = (rec - yaw + math.pi) % (2 * math.pi) - math.pi
            assert abs(d) < 1e-5, f'P {P} yaw {yaw}: recovered {rec}'


def test_yaw_changes_one_for_one_with_tag_rotation():
    # The guarantee that matters: rotate the tag by Δ → recovered yaw moves by Δ
    # (correct sign, no mirror).
    K = _K()
    T = _overhead_extrinsic()
    tag, P = 0.024, (0.21, 0.03, 0.05)
    base = tag_yaw_base(_project_tag(P, 0.2, tag, K, T), K, None, T, plane_z=P[2])
    for delta in (0.3, -0.4, 1.0):
        rec = tag_yaw_base(_project_tag(P, 0.2 + delta, tag, K, T), K, None, T, plane_z=P[2])
        moved = (rec - base + math.pi) % (2 * math.pi) - math.pi
        assert abs(moved - delta) < 1e-5


def test_wrong_plane_height_barely_affects_yaw():
    # Known-plane yaw is insensitive to a 1 cm plane-height error (measured).
    K = _K()
    T = _overhead_extrinsic()
    tag = 0.024
    for P in [(0.12, 0.06, 0.04), (0.28, -0.08, 0.03)]:
        for yaw in (-1.0, 0.5):
            px = _project_tag(P, yaw, tag, K, T)
            rec = tag_yaw_base(px, K, None, T, plane_z=P[2] + 0.01)  # wrong by +1 cm
            d = (rec - yaw + math.pi) % (2 * math.pi) - math.pi
            assert abs(d) < 1e-3, f'P {P} yaw {yaw}: {math.degrees(d):.3f} deg off'


# ── edge-length sanity gate ──────────────────────────────────────────────────
def test_edge_length_recovers_tag_size():
    K = _K()
    T = _overhead_extrinsic()
    for tag in (0.020, 0.024, 0.030):
        px = _project_tag((0.20, 0.0, 0.05), 0.4, tag, K, T)
        length = tag_edge_length_base(px, K, None, T, plane_z=0.05)
        assert length == pytest.approx(tag, abs=1e-4)


# ── tag-frame object points ──────────────────────────────────────────────────
def test_object_points_order_and_scale():
    obj = tag_corner_object_points(0.024)
    h = 0.012
    np.testing.assert_allclose(obj, [
        [-h,  h, 0.0],
        [ h,  h, 0.0],
        [ h, -h, 0.0],
        [-h, -h, 0.0],
    ])


# ── grasp_joint5 formula ─────────────────────────────────────────────────────
def test_grasp_joint5_formula_and_wrap():
    assert grasp_joint5(0.5, 0.3, math.pi / 2) == pytest.approx(0.2 + math.pi / 2)
    v = grasp_joint5(math.pi - 0.1, -0.5, math.pi / 2)  # = pi + 0.9 -> wraps
    assert -math.pi < v <= math.pi
    expected = (math.pi - 0.1 - (-0.5) + math.pi / 2 + math.pi) % (2 * math.pi) - math.pi
    assert v == pytest.approx(expected)
    assert grasp_joint5(0.4, 0.4, 0.0) == pytest.approx(0.0)


# ── FK-closed-loop orientation guard (the geometry test, not code==itself) ───
def _line_angle_diff(a: float, b: float) -> float:
    """Smallest absolute angle between two directions treated as 180°-periodic
    LINES (the gripper pinch axis is a line, not a ray)."""
    d = (a - b) % math.pi
    return min(d, math.pi - d)


def test_grasp_joint5_aligns_jaw_axis_with_tag_yaw_via_fk():
    """Closed-loop geometric guard: tag_yaw → joint5 → REAL FK → jaw axis → tag_yaw.

    The IK self-check is POSITION-only (``ik_solver.solve`` verifies only the EE
    translation), so a sign/handedness error in
    ``joint5 = base_yaw − tag_yaw + GRASP_ROLL`` reaches the servos with NO
    runtime guard. ``test_grasp_joint5_formula_and_wrap`` only checks the formula
    against itself. THIS test runs the formula's joint5 through the exact forward
    kinematics and asserts the gripper's jaw-separation axis (``end_effector_link``
    local +Y in the base frame) actually points along the requested ``tag_yaw``
    (mod π — the pinch line is 180°-periodic). It would catch a flipped
    ``base_yaw − tag_yaw`` sign, a wrong ``GRASP_ROLL`` constant, or any future
    FK/formula divergence — the orientation bug class the runtime cannot see.
    (The assumed-vs-real pupil corner winding is still a rig-only check.)"""
    ik = IKSolver()
    grasp_roll = math.radians(90.0)  # EDUBOTICS_GRASP_ROLL_DEG fleet default
    z = 0.05  # a reachable straight-down grasp height
    for (x, y) in [(0.18, 0.0), (0.15, 0.08), (0.12, -0.10), (0.22, 0.04)]:
        base_yaw = ik.base_yaw(x, y)
        for tag_yaw in (0.0, 0.5, -0.7, 1.2, 2.5, -2.9, math.pi / 2):
            j5 = grasp_joint5(base_yaw, tag_yaw, grasp_roll)
            cfg = ik.solve((x, y, z), roll=j5)
            assert cfg is not None, f'({x},{y}) must be reachable for the guard'
            assert cfg[0] == pytest.approx(base_yaw)   # joint1 == base_yaw(x,y)
            assert cfg[4] == pytest.approx(j5)          # joint5 == the formula
            R, _t = ik.fk(cfg)
            jaw_axis = R[:, 1]                           # end_effector_link local +Y
            jaw_az = math.atan2(float(jaw_axis[1]), float(jaw_axis[0]))
            d = _line_angle_diff(jaw_az, tag_yaw)
            assert d < 1e-6, (
                f'({x},{y}) tag_yaw={tag_yaw:.3f}: jaw azimuth {jaw_az:.6f} '
                f'≠ tag_yaw (mod π), off by {math.degrees(d):.4f}°'
            )


def test_grasp_joint5_fk_guard_catches_sign_flip():
    """Negative control: the FK guard above must FAIL for the wrong-sign formula
    (``base_yaw + tag_yaw``) — proving it actually tests geometry, not tautology."""
    ik = IKSolver()
    grasp_roll = math.radians(90.0)
    x, y, z = 0.15, 0.08, 0.05
    base_yaw = ik.base_yaw(x, y)
    tag_yaw = 0.6
    wrong_j5 = grasp_joint5(base_yaw, -tag_yaw, grasp_roll)  # sign flip on tag_yaw
    cfg = ik.solve((x, y, z), roll=wrong_j5)
    assert cfg is not None
    R, _t = ik.fk(cfg)
    jaw_az = math.atan2(float(R[1, 1]), float(R[0, 1]))
    assert _line_angle_diff(jaw_az, tag_yaw) > 0.1  # confidently wrong, as expected


# ── malformed inputs fail safe (None, not crash) ─────────────────────────────
def test_malformed_inputs_return_none():
    K = _K()
    T = _overhead_extrinsic()
    good = _project_tag((0.20, 0.0, 0.05), 0.0, 0.024, K, T)
    assert tag_yaw_base(good[:3], K, None, T, plane_z=0.05) is None       # only 3 corners
    bad = good.copy(); bad[0, 0] = np.nan
    assert tag_yaw_base(bad, K, None, T, plane_z=0.05) is None            # NaN corner
    assert tag_yaw_base(good, K, None, T, plane_z=0.05) is not None       # control: good case works

"""HIGH-3 — object (x, y) must be recovered on the table SURFACE plane
(board_table_z), NOT the gripper grasp height (z_table).

The bug is invisible for a perfectly vertical camera (the ray hits the
same (x, y) on every horizontal plane), so the regression test uses a
TILTED scene-camera extrinsic: there the surface-vs-grasp plane choice
moves the recovered (x, y) by ~cm. We assert _attach_world_xyz lands the
detection at the geometrically-correct surface (x, y) — while keeping the
world_xyz_m Z at z_table so motion's grasp-descend stays unchanged.
"""

from __future__ import annotations

import numpy as np
import pytest

from physical_ai_server.workflow.handlers import perception_blocks
from physical_ai_server.workflow.projection import project_pixel_to_table


def _intrinsics():
    K = np.array([
        [600.0, 0.0, 320.0],
        [0.0, 600.0, 240.0],
        [0.0, 0.0, 1.0],
    ])
    dist = np.zeros((5, 1))
    return K, dist


def _tilted_extrinsic():
    """Scene camera ~0.5 m up but TILTED ~20° about the base X axis, so the
    ray for an off-centre pixel intersects the surface plane and the grasp
    plane at materially different (x, y)."""
    theta = np.deg2rad(20.0)
    # Start from the straight-down convention used in test_projection, then
    # rotate the camera about base X by theta.
    R_down = np.array([
        [1.0, 0.0, 0.0],
        [0.0, -1.0, 0.0],
        [0.0, 0.0, -1.0],
    ])
    Rx = np.array([
        [1.0, 0.0, 0.0],
        [0.0, np.cos(theta), -np.sin(theta)],
        [0.0, np.sin(theta), np.cos(theta)],
    ])
    R = Rx @ R_down
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = [0.0, 0.0, 0.5]
    return T


class _Det:
    """Minimal perception.Detection stand-in carrying just what
    _attach_world_xyz reads/writes."""

    def __init__(self, cx, cy):
        self.centroid_px = (cx, cy)
        self.world_xyz_m = None


class _Ctx:
    def __init__(self, board_table_z, z_table):
        K, dist = _intrinsics()
        self.scene_intrinsics = {'K': K, 'dist': dist}
        self.scene_extrinsics = _tilted_extrinsic()
        self.board_table_z = board_table_z
        self.z_table = z_table
        # A measured EE-frame tilt must NOT be used for object projection.
        self.table_plane = (0.05, -0.02, z_table)
        self.emitted = None

    def emit_detections(self, dets):
        self.emitted = dets


# A clearly off-centre pixel so the tilt matters.
_PX, _PY = 320 + 120, 240 + 40
# Surface at z=0, grasp height ~9 cm above it (the touch-off offset).
_BOARD_Z = 0.0
_Z_TABLE = 0.09


def test_object_xy_is_recovered_on_the_surface_not_the_grasp_height():
    ctx = _Ctx(board_table_z=_BOARD_Z, z_table=_Z_TABLE)
    det = _Det(_PX, _PY)
    out = perception_blocks._attach_world_xyz(ctx, [det])
    assert out[0].world_xyz_m is not None
    gx, gy, gz = out[0].world_xyz_m

    # The geometrically-correct (x, y) is the ray ∩ SURFACE plane.
    K, dist = _intrinsics()
    surf = project_pixel_to_table(_PX, _PY, K, dist, ctx.scene_extrinsics, _BOARD_Z)
    assert surf is not None
    assert gx == pytest.approx(float(surf[0]), abs=1e-6)
    assert gy == pytest.approx(float(surf[1]), abs=1e-6)

    # And the BUGGY (x, y) — ray ∩ the grasp-height plane — must differ by cm
    # on this tilted camera (proves the test actually exercises the bug).
    grasp_plane = project_pixel_to_table(
        _PX, _PY, K, dist, ctx.scene_extrinsics, _Z_TABLE)
    assert grasp_plane is not None
    lateral_err = np.hypot(
        float(grasp_plane[0]) - gx, float(grasp_plane[1]) - gy)
    assert lateral_err > 0.01  # > 1 cm: the bug would have been real here


def test_world_z_stays_at_grasp_height_for_motion_descend():
    # The Z component must remain z_table (motion.pickup descends to
    # world_xyz_m[2] + clearance), even though (x, y) come from the surface.
    ctx = _Ctx(board_table_z=_BOARD_Z, z_table=_Z_TABLE)
    det = _Det(_PX, _PY)
    out = perception_blocks._attach_world_xyz(ctx, [det])
    assert out[0].world_xyz_m[2] == pytest.approx(_Z_TABLE)


def test_missing_grasp_height_leaves_world_xyz_unset():
    # board_table_z present but z_table missing → no world_xyz_m, so motion
    # raises the German "Tisch vermessen zuerst" error rather than grasping
    # against a guessed height.
    ctx = _Ctx(board_table_z=_BOARD_Z, z_table=None)
    det = _Det(_PX, _PY)
    out = perception_blocks._attach_world_xyz(ctx, [det])
    assert out[0].world_xyz_m is None


def test_missing_surface_height_leaves_world_xyz_unset():
    # z_table present but board_table_z missing (old YAML) → still no
    # projection (the surface plane is the (x, y) reference).
    ctx = _Ctx(board_table_z=None, z_table=_Z_TABLE)
    det = _Det(_PX, _PY)
    out = perception_blocks._attach_world_xyz(ctx, [det])
    assert out[0].world_xyz_m is None

"""Projection tests — pixel ↔ table-plane round-trip."""

from __future__ import annotations

import numpy as np

from physical_ai_server.workflow.projection import (
    project_base_to_pixel,
    project_pixel_to_table,
)


def _identity_camera():
    K = np.array([
        [600.0, 0.0, 320.0],
        [0.0, 600.0, 240.0],
        [0.0, 0.0, 1.0],
    ])
    dist = np.zeros((5, 1))
    # Camera at z=0.5 m above the table looking straight down. Frame
    # convention: camera +Z is forward (down toward the table), so the
    # camera-to-base transform reflects the camera, then translates.
    T = np.array([
        [1.0, 0.0, 0.0, 0.0],
        [0.0, -1.0, 0.0, 0.0],
        [0.0, 0.0, -1.0, 0.5],
        [0.0, 0.0, 0.0, 1.0],
    ])
    return K, dist, T


def test_pixel_to_table_returns_z_table():
    K, dist, T = _identity_camera()
    out = project_pixel_to_table(320, 240, K, dist, T, 0.0)
    assert out is not None
    # Centre pixel of a directly-overhead camera maps to (0, 0, 0).
    assert abs(out[0]) < 0.01
    assert abs(out[1]) < 0.01
    assert abs(out[2]) < 1e-6


def test_pixel_to_table_offset_pixel_lands_off_centre():
    K, dist, T = _identity_camera()
    out = project_pixel_to_table(320 + 60, 240, K, dist, T, 0.0)
    assert out is not None
    # 60 px to the right at f=600 px / 0.5 m height ≈ 5 cm in world.
    assert abs(out[0] - 0.05) < 0.01
    assert abs(out[2]) < 1e-6


def test_round_trip_pixel_to_base_to_pixel():
    K, dist, T = _identity_camera()
    px_in, py_in = 380, 240
    base = project_pixel_to_table(px_in, py_in, K, dist, T, 0.0)
    assert base is not None
    pixel_back = project_base_to_pixel(base, K, dist, T)
    assert pixel_back is not None
    assert abs(pixel_back[0] - px_in) < 1.0
    assert abs(pixel_back[1] - py_in) < 1.0

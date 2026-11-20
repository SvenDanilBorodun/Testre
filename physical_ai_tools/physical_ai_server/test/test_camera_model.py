#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The tower camera's lens model and the two consumers that must carry it
unharmed (spec 40 S4 ``camera_model.py``, WP-09; architecture A12, D33's
valid field, review B5).

* ``camera_model.KB`` is a two-coefficient Kannala-Brandt lens inverted by its
  own Newton step; ``unproject`` answers only inside the model's VALID FIELD
  (the slope of the radial map stays at least 0.2) and NaN beyond it, where a
  plain Newton returns nonsense.
* ``projection.project_pixel_to_table`` dispatches on ``KBDistortion`` and
  intersects the model's own ray with the plane.
* ``tag_pose`` passes ``dist`` through untouched, so a ``KBDistortion`` reaches
  the projection instead of raising ``TypeError`` in ``np.asarray``.

The truth in every case is computed here, independently of the code under test:
a point is projected by the closed-form forward map ``r = θ(1 + k1 θ² + k2 θ⁴)``
written out in ``_kb_pixel``, never by ``KB.project``.
"""

from __future__ import annotations

import ast
import dataclasses
import math
import sys
from pathlib import Path

import numpy as np
import pytest

from physical_ai_server.workflow.autocal.camera_model import (
    KB,
    KBDistortion,
    LENS_FIELD_MIN_RAD,
)
from physical_ai_server.workflow.projection import project_pixel_to_table
from physical_ai_server.workflow.tag_pose import (
    tag_corner_object_points,
    tag_edge_length_base,
    tag_yaw_base,
)

_SRC = (Path(__file__).resolve().parents[1] / 'physical_ai_server' / 'workflow'
        / 'autocal' / 'camera_model.py')

# 1080p, the tower camera's stream (A1); the focal length of the contract camera.
_F, _CX, _CY = 1037.8, 959.5, 539.5

# Three lenses whose valid field reaches past 60 degrees, so the round trip can
# span it: an equidistant lens, the manual's kind of lens and one with both
# coefficients set (valid fields 1.571, 1.245 and 1.196 rad).
_ROUND_TRIP_LENSES = [
    KB(f=_F, aspect=1.0, cx=_CX, cy=_CY, k1=0.0, k2=0.0),
    KB(f=1012.0, aspect=1.002, cx=965.0, cy=532.0, k1=0.0, k2=-0.0665),
    KB(f=1050.0, aspect=0.997, cx=951.3, cy=546.8, k1=0.1, k2=-0.12),
]


def _unit(v):
    v = np.asarray(v, dtype=np.float64)
    return v / np.linalg.norm(v, axis=-1, keepdims=True)


def _kb_pixel(p_cam, f, aspect, cx, cy, k1, k2):
    """The forward map, written out: camera-frame point -> pixel."""
    x, y, z = (float(c) for c in p_cam)
    rho = math.hypot(x, y)
    th = math.atan2(rho, z)
    r = th * (1.0 + k1 * th ** 2 + k2 * th ** 4)
    xn, yn = (0.0, 0.0) if rho == 0.0 else (x * r / rho, y * r / rho)
    return f * xn + cx, aspect * f * yn + cy


def _look_at(lens, aim):
    """T_cam_to_base (4x4) of a camera at ``lens`` whose optical axis (+z)
    points at ``aim``; image x to the right of the view, y down."""
    lens = np.asarray(lens, dtype=np.float64)
    fwd = _unit(np.asarray(aim, dtype=np.float64) - lens)
    right = _unit(np.cross(fwd, [0.0, 0.0, 1.0]))
    down = np.cross(fwd, right)
    T = np.eye(4)
    T[:3, :3] = np.stack([right, down, fwd], axis=1)
    T[:3, 3] = lens
    return T


def _to_cam(T_cam_to_base, p_base):
    R, t = T_cam_to_base[:3, :3], T_cam_to_base[:3, 3]
    return R.T @ (np.asarray(p_base, dtype=np.float64) - t)


# The contract's lens place and aim (spec 40 §3, placement B).
_T_CONTRACT = _look_at((-0.100, 0.100, 0.575), (0.08278, 0.02924, 0.0))


# ── the model ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize('lens', _ROUND_TRIP_LENSES, ids=['equidistant', 'manual', 'k1_k2'])
def test_unproject_inverts_project_for_2000_rays_up_to_60_degrees(lens):
    rng = np.random.default_rng(20261120)
    theta = rng.uniform(0.0, math.radians(60.0), 2000)
    theta[0] = 0.0                                  # the optical axis itself
    theta[1] = math.radians(60.0)                   # the edge of the span
    phi = rng.uniform(-math.pi, math.pi, 2000)
    rays = np.stack([np.sin(theta) * np.cos(phi), np.sin(theta) * np.sin(phi), np.cos(theta)], axis=1)
    points = rays * rng.uniform(0.05, 3.0, (2000, 1))
    assert lens.valid_theta() > math.radians(60.0)
    back = lens.unproject(lens.project(points))
    assert back.shape == (2000, 3)
    assert np.max(np.abs(back - rays)) < 1e-9


def test_project_is_the_written_out_forward_map():
    lens = _ROUND_TRIP_LENSES[2]
    rng = np.random.default_rng(7)
    for _ in range(50):
        p = rng.uniform([-0.4, -0.4, 0.2], [0.4, 0.4, 1.0])
        u, v = _kb_pixel(p, lens.f, lens.aspect, lens.cx, lens.cy, lens.k1, lens.k2)
        np.testing.assert_allclose(lens.project(p), [u, v], rtol=0, atol=1e-9)


# ── the valid field (decision D33) ───────────────────────────────────────────
def test_valid_field_of_the_strongest_lens_and_what_lies_beyond_it():
    lens = KB(f=_F, aspect=1.0, cx=_CX, cy=_CY, k1=0.0, k2=-0.25)
    assert lens.valid_theta() == pytest.approx(0.894, abs=0.001)
    # Inside the field: a normalised radius of 0.70, in two directions.
    for phi in (0.0, math.radians(30.0)):
        px = (_CX + 0.70 * _F * math.cos(phi), _CY + 0.70 * _F * math.sin(phi))
        ray = lens.unproject(px)
        assert ray.shape == (3,)
        assert math.atan2(math.hypot(ray[0], ray[1]), ray[2]) == pytest.approx(0.7659, abs=1e-4)
        assert math.atan2(ray[1], ray[0]) == pytest.approx(phi, abs=1e-12)
    # Beyond the field's normalised radius of 0.7512 (a plain Newton ends at
    # 1.93 rad and -4.03 rad here): no answer, and no table point either.
    intr = lens.to_intrinsics_dict()
    K, dist = intr['K'], intr['dist']
    T_down = np.array([[1.0, 0.0, 0.0, 0.0], [0.0, -1.0, 0.0, 0.0],       # straight down, 0.5 m up
                       [0.0, 0.0, -1.0, 0.5], [0.0, 0.0, 0.0, 1.0]])
    for r in (0.78, 0.85):
        px = (_CX + r * _F, _CY)
        assert np.all(np.isnan(lens.unproject(px)))
        assert project_pixel_to_table(px[0], px[1], K, dist, T_down, 0.0) is None
    # Control: the same camera locates a pixel inside the field.
    assert project_pixel_to_table(_CX + 0.70 * _F, _CY, K, dist, T_down, 0.0) is not None


@pytest.mark.parametrize('k1, k2, expected', [
    (0.0, -0.0665, 1.245),         # the manual's kind of lens
    (-0.19, -0.10, 0.904),
    (-0.24, -0.24, 0.754),
])
def test_valid_field_of_the_named_lenses(k1, k2, expected):
    lens = KB(f=_F, aspect=1.0, cx=_CX, cy=_CY, k1=k1, k2=k2)
    assert lens.valid_theta() == pytest.approx(expected, abs=0.001)


def test_valid_field_of_an_undistorted_lens_is_the_whole_hemisphere():
    assert KB(f=_F, aspect=1.0, cx=_CX, cy=_CY, k1=0.0, k2=0.0).valid_theta() >= 1.57


def test_valid_field_is_where_the_slope_stays_at_least_0_2():
    """The largest grid angle up to which ``1 + 3 k1 θ² + 5 k2 θ⁴ >= 0.2``."""
    for k1, k2 in ((0.0, -0.25), (-0.19, -0.10), (-0.24, -0.24), (0.1, -0.12)):
        vt = KB(f=_F, aspect=1.0, cx=_CX, cy=_CY, k1=k1, k2=k2).valid_theta()
        grid = np.arange(0.0, vt + 1e-9, 0.001)
        assert np.all(1 + 3 * k1 * grid ** 2 + 5 * k2 * grid ** 4 >= 0.2)
        nxt = vt + 0.001
        assert 1 + 3 * k1 * nxt ** 2 + 5 * k2 * nxt ** 4 < 0.2


def test_the_lens_gate_asks_for_a_field_of_0_80_rad():
    assert LENS_FIELD_MIN_RAD == 0.80


# ── the intrinsics dict ──────────────────────────────────────────────────────
def test_to_intrinsics_dict_carries_k_and_a_frozen_distortion():
    lens = KB(f=1000.0, aspect=1.01, cx=950.0, cy=540.0, k1=-0.02, k2=-0.05)
    intr = lens.to_intrinsics_dict()
    np.testing.assert_array_equal(intr['K'], [[1000.0, 0.0, 950.0], [0.0, 1010.0, 540.0], [0.0, 0.0, 1.0]])
    assert intr['dist'] == KBDistortion(k1=-0.02, k2=-0.05)
    with pytest.raises(dataclasses.FrozenInstanceError):
        intr['dist'].k1 = 0.0
    # Code that still coerces the distortion to an array fails loudly instead
    # of silently dropping the model.
    with pytest.raises(TypeError):
        np.asarray(intr['dist'], dtype=np.float64)


def test_camera_model_is_pure_numpy_and_the_stdlib():
    tree = ast.parse(_SRC.read_text(encoding='utf-8'))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots.add((node.module or '').split('.')[0])
    assert roots - {'numpy'} <= set(sys.stdlib_module_names)
    assert roots


# ── projection.project_pixel_to_table with a KBDistortion ───────────────────
def _table_points(n, seed):
    rng = np.random.default_rng(seed)
    return np.stack([rng.uniform(0.05, 0.35, n), rng.uniform(-0.20, 0.20, n)], axis=1)


@pytest.mark.parametrize('k1, k2', [(0.0, -0.0665), (-0.03, -0.10), (0.0, -0.25)])
def test_project_pixel_to_table_is_the_analytic_ray_plane_intersection(k1, k2):
    lens = KB(f=_F, aspect=1.003, cx=962.0, cy=536.0, k1=k1, k2=k2)
    intr = lens.to_intrinsics_dict()
    worst = 0.0
    for z_table in (0.0, 0.03):
        for x, y in _table_points(150, 11):
            truth = np.array([x, y, z_table])
            px = _kb_pixel(_to_cam(_T_CONTRACT, truth), lens.f, lens.aspect, lens.cx, lens.cy, k1, k2)
            got = project_pixel_to_table(px[0], px[1], intr['K'], intr['dist'], _T_CONTRACT, z_table)
            assert got is not None
            worst = max(worst, float(np.max(np.abs(np.asarray(got) - truth))))
    assert worst < 1e-7


def test_project_pixel_to_table_with_a_kb_lens_meets_the_tilted_plane():
    lens = KB(f=_F, aspect=1.0, cx=_CX, cy=_CY, k1=-0.03, k2=-0.10)
    intr = lens.to_intrinsics_dict()
    a, b, c = 0.02, -0.015, 0.004
    for x, y in _table_points(100, 12):
        truth = np.array([x, y, a * x + b * y + c])
        px = _kb_pixel(_to_cam(_T_CONTRACT, truth), lens.f, lens.aspect, lens.cx, lens.cy, lens.k1, lens.k2)
        got = project_pixel_to_table(px[0], px[1], intr['K'], intr['dist'], _T_CONTRACT, 0.0, plane=(a, b, c))
        assert got is not None
        assert np.max(np.abs(np.asarray(got) - truth)) < 1e-7


# ── tag_pose with KB intrinsics (review B5) ──────────────────────────────────
def _rot_z(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def _kb_tag_corners(lens, T, centre, yaw, size):
    corners = (_rot_z(yaw) @ tag_corner_object_points(size).T).T + np.asarray(centre)
    return np.array([_kb_pixel(_to_cam(T, c), lens.f, lens.aspect, lens.cx, lens.cy, lens.k1, lens.k2)
                     for c in corners])


_TAGS = [((0.12, 0.06, 0.045), 0.3), ((0.28, -0.10, 0.045), -1.2), ((0.20, 0.15, 0.03), 2.6),
         ((0.33, 0.02, 0.03), -2.9), ((0.08, -0.14, 0.045), 1.1)]


@pytest.mark.parametrize('k1, k2', [(0.0, -0.0665), (0.0, -0.25)])
def test_tag_pose_carries_the_kb_lens(k1, k2):
    lens = KB(f=_F, aspect=1.0, cx=_CX, cy=_CY, k1=k1, k2=k2)
    intr = lens.to_intrinsics_dict()
    for centre, yaw in _TAGS:
        px = _kb_tag_corners(lens, _T_CONTRACT, centre, yaw, 0.024)
        got_yaw = tag_yaw_base(px, intr['K'], intr['dist'], _T_CONTRACT, plane_z=centre[2])
        got_edge = tag_edge_length_base(px, intr['K'], intr['dist'], _T_CONTRACT, plane_z=centre[2])
        assert got_yaw is not None and got_edge is not None
        d = (got_yaw - yaw + math.pi) % (2 * math.pi) - math.pi
        assert abs(math.degrees(d)) < 0.2, f'{centre}: yaw {math.degrees(d):.4f} deg off'
        assert abs(got_edge - 0.024) < 0.0002, f'{centre}: edge {got_edge * 1000:.4f} mm'


def test_tag_pose_still_reads_a_plain_distortion_vector():
    """The OpenCV path is unchanged: a vector given as a list is still
    coerced (cv2 refuses a list as distCoeffs)."""
    K = np.array([[600.0, 0.0, 320.0], [0.0, 600.0, 240.0], [0.0, 0.0, 1.0]])
    T = np.array([[1.0, 0.0, 0.0, 0.20], [0.0, -1.0, 0.0, 0.0],
                  [0.0, 0.0, -1.0, 0.50], [0.0, 0.0, 0.0, 1.0]])
    corners = (_rot_z(0.4) @ tag_corner_object_points(0.024).T).T + np.array([0.21, 0.03, 0.05])
    px = [[600.0 * c[0] / c[2] + 320.0, 600.0 * c[1] / c[2] + 240.0] for c in (_to_cam(T, q) for q in corners)]
    for dist in ([0.0, 0.0, 0.0, 0.0, 0.0], np.zeros(5), np.zeros((5, 1), dtype=np.float32)):
        assert tag_yaw_base(px, K, dist, T, plane_z=0.05) == pytest.approx(0.4, abs=1e-5)


def test_dropping_the_kb_lens_would_miss_the_edge():
    """Negative control: the same pixels read through an undistorted lens are
    far outside the tolerance above, so the test above can see a lens that is
    silently dropped."""
    lens = KB(f=_F, aspect=1.0, cx=_CX, cy=_CY, k1=0.0, k2=-0.25)
    K = lens.to_intrinsics_dict()['K']
    centre, yaw = _TAGS[1]
    px = _kb_tag_corners(lens, _T_CONTRACT, centre, yaw, 0.024)
    edge = tag_edge_length_base(px, K, np.zeros(5), _T_CONTRACT, plane_z=centre[2])
    assert abs(edge - 0.024) > 0.001

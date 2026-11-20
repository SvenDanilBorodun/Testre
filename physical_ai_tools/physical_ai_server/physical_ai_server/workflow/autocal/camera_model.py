#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The tower camera's lens: a two-coefficient Kannala-Brandt model (A12).

A camera-frame ray at field angle ``θ`` from the optical axis lands at the
normalised radius ``r(θ) = θ (1 + k1 θ² + k2 θ⁴)`` (the model ``cv2.fisheye``
uses, cut to two coefficients), in pixels ``u = f·xn + cx``,
``v = aspect·f·yn + cy``. It is inverted by our own Newton step, never by the
default five-iteration ``cv2.undistortPoints`` on a rational fit (A12).

**The valid field** (the amendment for decision D33). The model is trusted
only up to the largest field angle on a 0.001 rad grid (0 … π/2) up to which
the radial map keeps a slope ``1 + 3 k1 θ² + 5 k2 θ⁴ >= 0.2``. Beyond it the
map flattens and folds, and a plain Newton answers nonsense (for
``k2 = -0.25`` at normalised radii 0.78 and 0.85: 1.93 rad and -4.03 rad), so
``unproject`` answers NaN for a pixel whose normalised radius lies beyond the
field. Everything the software unprojects on purpose lies within 0.69 rad of
the axis (spec 40 S4, P-R19); the lens gate asks for a field of
``LENS_FIELD_MIN_RAD``.

``KBDistortion`` is what ``to_intrinsics_dict`` hands on as ``dist``: a frozen
dataclass, so code that still coerces it with ``np.asarray`` fails loudly
instead of silently dropping the model. ``projection.project_pixel_to_table``
dispatches on its type.

Pure NumPy and the standard library.
"""

from __future__ import annotations

from dataclasses import dataclass
import functools
import math

import numpy as np

# What the lens gate asks of ``valid_theta()``: the field must cover 0.80 rad.
LENS_FIELD_MIN_RAD = 0.80

# The valid field: the slope of the radial map stays at least this ...
VALID_FIELD_MIN_SLOPE = 0.2
# ... judged on this grid from the axis to π/2.
VALID_FIELD_STEP_RAD = 0.001
_VALID_FIELD_GRID = np.arange(0.0, math.pi / 2 + 1e-9, VALID_FIELD_STEP_RAD)

# Newton steps of the inverse (each one kept inside the valid field).
UNPROJECT_NEWTON_STEPS = 25


@dataclass(frozen=True)
class KBDistortion:
    """The lens' two radial coefficients, as an intrinsics dict carries them."""

    k1: float
    k2: float


def radial_map(theta, k1: float, k2: float):
    """Normalised image radius of a ray at field angle ``theta``."""
    t2 = np.square(theta)
    return theta * (1.0 + k1 * t2 + k2 * t2 * t2)


def radial_slope(theta, k1: float, k2: float):
    """``d r / d θ`` of :func:`radial_map`."""
    t2 = np.square(theta)
    return 1.0 + 3.0 * k1 * t2 + 5.0 * k2 * t2 * t2


@functools.lru_cache(maxsize=64)
def valid_theta(k1: float, k2: float) -> float:
    """The largest grid angle up to which the slope stays at least
    ``VALID_FIELD_MIN_SLOPE``; π/2 when it never drops below."""
    ok = radial_slope(_VALID_FIELD_GRID, float(k1), float(k2)) >= VALID_FIELD_MIN_SLOPE
    bad = np.flatnonzero(~ok)
    if bad.size == 0:
        return math.pi / 2
    return float(_VALID_FIELD_GRID[bad[0] - 1]) if bad[0] > 0 else 0.0


def angle_of_radius(r, k1: float, k2: float):
    """Field angle of a normalised radius, or NaN beyond the valid field.

    Newton on ``radial_map(θ) = r``, started at ``θ = r`` and kept inside the
    field: the root is bracketed by ``[0, valid_theta]``, where the map rises
    with a slope of at least 0.2, and a step that would leave the bracket is
    replaced by its midpoint."""
    r = np.asarray(r, dtype=np.float64)
    vt = valid_theta(float(k1), float(k2))
    inside = (r >= 0.0) & (r <= radial_map(vt, k1, k2))
    rr = np.where(inside, r, 0.0)
    lo = np.zeros_like(rr)
    hi = np.full_like(rr, vt)
    th = np.clip(rr, 0.0, vt)
    for _ in range(UNPROJECT_NEWTON_STEPS):
        g = radial_map(th, k1, k2) - rr
        lo = np.where(g <= 0.0, th, lo)
        hi = np.where(g >= 0.0, th, hi)
        step = th - g / radial_slope(th, k1, k2)
        th = np.where((step >= lo) & (step <= hi), step, 0.5 * (lo + hi))
    return np.where(inside, th, np.nan)


def unproject_normalised(xn, yn, k1: float, k2: float) -> np.ndarray:
    """Unit camera-frame rays (``..., 3``) through normalised image points;
    NaN rows beyond the valid field."""
    xn = np.asarray(xn, dtype=np.float64)
    yn = np.asarray(yn, dtype=np.float64)
    r = np.hypot(xn, yn)
    th = angle_of_radius(r, k1, k2)
    s = np.divide(np.sin(th), r, out=np.zeros_like(r), where=r > 0.0)
    s = np.where(np.isnan(th), np.nan, s)
    return np.stack([xn * s, yn * s, np.cos(th)], axis=-1)


@dataclass(frozen=True)
class KB:
    """A Kannala-Brandt camera: focal length ``f`` (px), ``aspect`` = fy / fx,
    principal point ``(cx, cy)`` and the radial coefficients ``k1``, ``k2``."""

    f: float
    aspect: float
    cx: float
    cy: float
    k1: float
    k2: float

    def valid_theta(self) -> float:
        """The model's valid field (rad); see the module docstring."""
        return valid_theta(float(self.k1), float(self.k2))

    def project(self, points_cam) -> np.ndarray:
        """Pixels (``..., 2``) of camera-frame points (``..., 3``; +z forward)."""
        p = np.asarray(points_cam, dtype=np.float64)
        x, y, z = p[..., 0], p[..., 1], p[..., 2]
        rho = np.hypot(x, y)
        r = radial_map(np.arctan2(rho, z), self.k1, self.k2)
        s = np.divide(r, rho, out=np.zeros_like(rho), where=rho > 0.0)
        return np.stack([self.f * x * s + self.cx, self.aspect * self.f * y * s + self.cy], axis=-1)

    def unproject(self, pixels) -> np.ndarray:
        """Unit camera-frame rays (``..., 3``) through pixels (``..., 2``);
        NaN for a pixel beyond the valid field."""
        uv = np.asarray(pixels, dtype=np.float64)
        xn = (uv[..., 0] - self.cx) / self.f
        yn = (uv[..., 1] - self.cy) / (self.aspect * self.f)
        return unproject_normalised(xn, yn, self.k1, self.k2)

    def to_intrinsics_dict(self) -> dict:
        """``{'K': 3x3, 'dist': KBDistortion}``, the shape the projection takes."""
        K = np.array([
            [self.f, 0.0, self.cx],
            [0.0, self.aspect * self.f, self.cy],
            [0.0, 0.0, 1.0],
        ], dtype=np.float64)
        return {'K': K, 'dist': KBDistortion(k1=float(self.k1), k2=float(self.k2))}

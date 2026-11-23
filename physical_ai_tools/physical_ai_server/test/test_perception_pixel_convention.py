#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The detector's pixel convention (architecture A13, WP-09).

``pupil_apriltags`` reports its centre and its raw corners about +0.4 px off
OpenCV's convention (pixel centres on the integers): +0.36 ... +0.50 px,
depending on blur. ``Perception`` therefore reports a tag's centre as the
INTERSECTION OF THE DIAGONALS of its ``cornerSubPix``-refined corners, which is
unbiased; where the refinement cannot run (a tag at the image edge) the raw
corners are shifted by -0.4 px instead.

The truth is always OpenCV's convention: a square drawn over the pixels
``a … a+n-1`` spans ``[a - 0.5, a + n - 0.5]``. The real-detector cases need
``pupil_apriltags`` (``pupil_required.require_pupil``).
"""

from __future__ import annotations

import threading

import cv2
import numpy as np
import pytest

from physical_ai_server.workflow.perception import Perception
from pupil_required import require_pupil


class _FakeTagResult:
    """One pupil_apriltags result, as the C detector returns it."""

    def __init__(self, center, corners, tag_id=20, decision_margin=90.0):
        self.center = np.asarray(center, dtype=np.float64)
        self.corners = np.asarray(corners, dtype=np.float64)
        self.tag_id = tag_id
        self.decision_margin = decision_margin


class _FakeDetector:
    def __init__(self, results):
        self._results = results

    def detect(self, gray):            # noqa: D401 - mimics pupil's signature
        return self._results


def _perception_with(results):
    p = Perception.__new__(Perception)
    p._apriltag_lock = threading.Lock()
    p._apriltag_detector = _FakeDetector(results)
    return p


def _diagonal_intersection(c):
    """Where the line through corners 0 and 2 meets the line through 1 and 3,
    by Cramer's rule on ``c0 + s (c2 - c0) = c1 + t (c3 - c1)``."""
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = (tuple(map(float, row)) for row in c)
    ax, ay = x2 - x0, y2 - y0
    bx, by = x3 - x1, y3 - y1
    det = ax * (-by) - ay * (-bx)
    s = ((x1 - x0) * (-by) - (y1 - y0) * (-bx)) / det
    return x0 + s * ax, y0 + s * ay


# A general quadrilateral: its diagonal intersection is neither the mean of the
# corners nor the detector's own centre.
_QUAD = np.array([[212.0, 96.0], [101.0, 104.0], [92.0, 231.0], [237.0, 186.0]])


def test_centre_is_the_diagonal_intersection_of_the_refined_corners(monkeypatch):
    raw = _QUAD + 0.4
    p = _perception_with([_FakeTagResult(center=(150.0, 150.0), corners=raw)])
    monkeypatch.setattr(Perception, '_refine_corners_subpix', staticmethod(lambda gray, c: _QUAD.copy()))
    dets = p.detect(np.zeros((480, 640, 3), dtype=np.uint8), 'scene', 'apriltag')
    assert len(dets) == 1
    want = _diagonal_intersection(_QUAD)
    assert np.hypot(*(np.asarray(want) - np.mean(_QUAD, axis=0))) > 1.0    # the quad tells them apart
    assert dets[0].centroid_px == pytest.approx(want, abs=1e-9)
    assert all(isinstance(v, float) for v in dets[0].centroid_px)
    np.testing.assert_array_equal(dets[0].corners_px, _QUAD)


def _square_image(a, n, blur):
    img = np.full((400, 400), 235, np.uint8)
    img[a:a + n, a:a + n] = 25
    if blur:
        img = cv2.GaussianBlur(img, (0, 0), blur)
    return img


@pytest.mark.parametrize('a, n, blur', [(120, 160, 0.0), (170, 60, 0.7), (100, 40, 1.2)])
def test_refined_centre_of_a_drawn_square_is_on_the_opencv_convention(a, n, blur):
    """The real ``cornerSubPix`` refinement, fed the raw corners the way pupil
    reports them (+0.4 px) and its centre (their diagonal intersection)."""
    gray = _square_image(a, n, blur)
    truth = np.array([[a + n - 0.5, a - 0.5], [a - 0.5, a - 0.5],
                      [a - 0.5, a + n - 0.5], [a + n - 0.5, a + n - 0.5]])
    raw = truth + 0.4
    p = _perception_with([_FakeTagResult(center=_diagonal_intersection(raw), corners=raw)])
    dets = p.detect(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), 'scene', 'apriltag')
    centre = a + n / 2 - 0.5
    assert np.max(np.abs(np.asarray(dets[0].centroid_px) - centre)) < 0.15


def test_unrefined_corners_at_the_image_edge_are_shifted_by_minus_0_4_px():
    """Where cornerSubPix's search window does not fit, the raw corners are
    kept, moved onto OpenCV's convention by -0.4 px, and the centre is their
    diagonal intersection."""
    raw = np.array([[44.4, 2.4], [2.4, 2.4], [2.4, 44.4], [44.4, 44.4]])
    p = _perception_with([_FakeTagResult(center=(23.4, 23.4), corners=raw)])
    dets = p.detect(np.full((120, 160, 3), 235, np.uint8), 'scene', 'apriltag')
    np.testing.assert_allclose(dets[0].corners_px, raw - 0.4, rtol=0, atol=1e-12)
    assert dets[0].centroid_px == pytest.approx((23.0, 23.0), abs=1e-9)


def test_a_centre_the_corners_cannot_give_falls_back_to_the_shifted_detector_centre():
    """Corners whose diagonals do not cross (here: all four on one line) give
    no centre; the detector's own centre, moved by -0.4 px, stands in."""
    raw = np.array([[2.4, 30.4], [12.4, 30.4], [22.4, 30.4], [32.4, 30.4]])
    p = _perception_with([_FakeTagResult(center=(17.4, 30.6), corners=raw)])
    dets = p.detect(np.full((120, 160, 3), 235, np.uint8), 'scene', 'apriltag')
    assert dets[0].centroid_px == pytest.approx((17.0, 30.2), abs=1e-9)


# ── the real detector ────────────────────────────────────────────────────────
def _tag_image(n, blur, tag_id=7):
    dic = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36H11)
    img = np.full((400, 400), 255, np.uint8)
    a = 200 - n // 2
    img[a:a + n, a:a + n] = cv2.aruco.generateImageMarker(dic, tag_id, n)
    if blur:
        img = cv2.GaussianBlur(img, (0, 0), blur)
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


@pytest.mark.parametrize('n, blur', [(160, 0.0), (160, 0.7), (80, 0.0), (80, 1.2)])
def test_the_real_detector_reports_a_tag_centred_at_199_5_within_0_15_px(n, blur):
    require_pupil()
    p = Perception()
    assert p.apriltag_available()
    dets = p.detect(_tag_image(n, blur), 'scene', 'apriltag')
    assert [d.aruco_id for d in dets] == [7]
    assert np.max(np.abs(np.asarray(dets[0].centroid_px) - 199.5)) < 0.15

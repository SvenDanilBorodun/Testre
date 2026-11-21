"""RS-45 / RS-56 — perception centroid precision and the profile-aware catalog
response.

Each class pins a defect that was reproduced by execution first:

* RS-45 ``perception._detect_apriltag`` stored ``centroid_px`` as
  ``int(r.center[...])``. ``int()`` TRUNCATES, so the resulting grasp point was
  biased in a fixed direction rather than merely noisy — measured against the
  real ``projection.project_pixel_to_table`` (102° HFOV / 640 px → fx 259 px,
  camera 0.50 m up, 350 poses): mean dx −0.975 mm, dy +0.927 mm, mean|err|
  1.449 mm, max 2.654 mm, and ``mean|dx| == |mean dx|`` exactly, which is the
  signature of a pure bias. The real detector genuinely carries the discarded
  information (a rendered tag36h11 came back .857/.880 px past the grid).

* RS-56 ``object_catalog.build_object_catalog_response`` called the
  un-parameterised ``fixed_catalog()``, so ``GetObjectCatalog`` always shipped
  the OMX variant.
"""

from __future__ import annotations

import math

import numpy as np
import pytest


# ── RS-45: sub-pixel AprilTag centroid ───────────────────────────────────────
class _FakeTagResult:
    """Mimics one pupil_apriltags result so the detector path can be exercised
    without the optional C dependency."""

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


# A tag whose true centre sits deliberately OFF the integer pixel grid. The
# reported centre is where the corners' diagonals cross (A13), so the corners
# are placed for their diagonals to cross exactly there, as pupil's are.
_TRUE_CX, _TRUE_CY = 279.857040324862, 179.8803672052664
_CORNERS = [[_TRUE_CX - 19.6, _TRUE_CY - 19.6], [_TRUE_CX + 19.6, _TRUE_CY - 19.55],
            [_TRUE_CX + 19.6, _TRUE_CY + 19.6], [_TRUE_CX - 19.6, _TRUE_CY + 19.55]]


def _detect_one(monkeypatch):
    from physical_ai_server.workflow.perception import Perception
    p = Perception.__new__(Perception)
    import threading
    p._apriltag_lock = threading.Lock()
    p._apriltag_detector = _FakeDetector(
        [_FakeTagResult((_TRUE_CX, _TRUE_CY), _CORNERS)]
    )
    # Skip the cv2.cornerSubPix polish: it needs a real gradient image and this
    # test is about the CENTRE, not the corners.
    monkeypatch.setattr(
        Perception, '_refine_corners_subpix',
        staticmethod(lambda gray, c: np.asarray(c, dtype=np.float64)),
    )
    bgr = np.zeros((480, 640, 3), dtype=np.uint8)
    dets = p.detect(bgr, 'scene', 'apriltag')
    assert len(dets) == 1
    return dets[0]


def test_centroid_keeps_the_detector_sub_pixel_centre(monkeypatch):
    d = _detect_one(monkeypatch)
    cx, cy = d.centroid_px
    assert cx == pytest.approx(_TRUE_CX, abs=1e-12)
    assert cy == pytest.approx(_TRUE_CY, abs=1e-12)
    # The specific regression: an int() cast would have thrown away .857/.880 px.
    assert cx != int(cx)
    assert cy != int(cy)


def test_centroid_is_float_not_int(monkeypatch):
    d = _detect_one(monkeypatch)
    assert isinstance(d.centroid_px[0], float)
    assert isinstance(d.centroid_px[1], float)


def test_bbox_stays_integer(monkeypatch):
    """The bbox is a separate int-cast copy of the CORNERS and is unchanged —
    Detection.msg types x/y/w/h as int32."""
    d = _detect_one(monkeypatch)
    assert all(isinstance(v, int) for v in d.bbox_px)


def test_both_ros_wire_consumers_still_coerce(monkeypatch):
    """physical_ai_server.py unpacks ``cx, cy = d.centroid_px`` in two places and
    coerces at its own call site — ``det.cx = int(cx)`` for the int32
    Detection.msg field and ``float(cx)`` for the verify-point projection. Both
    must keep working on a float centroid BY CONSTRUCTION, because that file is
    not touched by this change."""
    d = _detect_one(monkeypatch)
    cx, cy = d.centroid_px
    assert int(cx) == 279 and int(cy) == 179          # int32 msg field
    assert float(cx) == pytest.approx(_TRUE_CX)       # verify-point projection


def test_truncation_would_bias_the_grasp_point_millimetres():
    """The measurement behind the fix, re-run as an assertion: truncating the
    centroid moves the projected grasp point by ~1.4 mm on average with a
    CONSISTENT sign, while the float centre is exact."""
    from physical_ai_server.workflow.projection import project_pixel_to_table

    w, h = 640, 480
    fx = (w / 2.0) / math.tan(math.radians(51.0))     # 102° HFOV over 640 px
    K = np.array([[fx, 0, w / 2.0], [0, fx, h / 2.0], [0, 0, 1.0]], dtype=np.float64)
    dist = np.zeros((5, 1), dtype=np.float64)
    T = np.eye(4)
    T[:3, :3] = np.array([[1.0, 0, 0], [0, -1.0, 0], [0, 0, -1.0]])
    T[:3, 3] = [0.0, 0.0, 0.50]                       # camera 0.50 m above table

    rng = np.random.default_rng(20260907)
    dxs, errs = [], []
    for _ in range(350):
        px = float(rng.uniform(60, w - 60))
        py = float(rng.uniform(60, h - 60))
        exact = project_pixel_to_table(px, py, K, dist, T, 0.0)
        trunc = project_pixel_to_table(float(int(px)), float(int(py)), K, dist, T, 0.0)
        assert exact is not None and trunc is not None
        dxs.append((trunc[0] - exact[0]) * 1000.0)
        errs.append(math.hypot(trunc[0] - exact[0], trunc[1] - exact[1]) * 1000.0)

    dxs = np.asarray(dxs)
    assert np.mean(errs) > 1.0          # ~1.4 mm mean displacement
    assert np.max(errs) > 2.0           # ~2.7 mm worst case
    # SYSTEMATIC, not zero-mean: truncation always pulls the same way, so the
    # mean of the signed error equals the mean of its absolute value.
    assert abs(np.mean(dxs)) == pytest.approx(np.mean(np.abs(dxs)), rel=1e-9)


# ── RS-56: profile-aware catalog response ────────────────────────────────────
class TestCatalogResponseProfile:
    def test_omitting_the_profile_is_byte_identical_to_before(self):
        """The parameter is OPTIONAL precisely so the un-wired ROS call site in
        physical_ai_server.py keeps its exact previous payload."""
        from physical_ai_server.workflow.object_catalog import (
            build_object_catalog_response, catalog_response_fields, fixed_catalog)
        baseline = catalog_response_fields(fixed_catalog())
        got = build_object_catalog_response()
        for key, value in baseline.items():
            assert got[key] == value
        assert got['success'] is True
        assert got['message'] == ''
        assert build_object_catalog_response(None) == got

    def test_the_profile_id_is_forwarded_to_fixed_catalog(self, monkeypatch):
        """The actual defect: the builder called the UN-parameterised
        fixed_catalog(), so a per-profile catalog could never reach the wire."""
        from physical_ai_server.workflow import object_catalog as oc
        seen = []
        real = oc.fixed_catalog
        monkeypatch.setattr(
            oc, 'fixed_catalog',
            lambda profile_id=None: (seen.append(profile_id), real(profile_id))[1],
        )
        oc.build_object_catalog_response('v1_studio')
        assert seen == ['v1_studio']

    @pytest.mark.parametrize(
        'profile_id', ['omx_full', 'omx_follower', 'v1_studio'])
    def test_every_shipped_profile_answers_with_the_six_arrays(self, profile_id):
        from physical_ai_server.workflow.object_catalog import (
            build_object_catalog_response)
        got = build_object_catalog_response(profile_id)
        arrays = ('type_names', 'labels_de', 'object_height_m',
                  'object_width_m', 'color_hex', 'max_instances')
        assert got['success'] is True
        lengths = {len(got[k]) for k in arrays}
        assert len(lengths) == 1 and lengths != {0}

    def test_unknown_profile_falls_back_like_fixed_catalog(self):
        from physical_ai_server.workflow.object_catalog import (
            build_object_catalog_response)
        assert build_object_catalog_response('bogus') == build_object_catalog_response()

    def test_the_wire_payload_is_profile_independent_today(self):
        """Documents WHY this was latent rather than a live wrong answer: the
        profile only changes gripper_close_rad, which is not one of the six wire
        arrays. If this assertion ever fails, a per-profile catalog has started
        differing in a WIRE field and the ROS call site in physical_ai_server.py
        MUST be wired to pass the resolved profile id."""
        from physical_ai_server.workflow.object_catalog import (
            build_object_catalog_response, fixed_catalog)
        payloads = [build_object_catalog_response(p)
                    for p in ('omx_full', 'v1_studio')]
        assert payloads[0] == payloads[1]
        # ... while the catalogs themselves genuinely DO differ.
        closes = {fixed_catalog(p).recipe_for_type('wuerfel').gripper_close_rad
                  for p in ('omx_full', 'v1_studio')}
        assert len(closes) == 2

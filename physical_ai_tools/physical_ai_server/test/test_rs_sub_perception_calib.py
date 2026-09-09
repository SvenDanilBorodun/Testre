"""RS-45 / RS-31 / RS-56 — perception centroid precision, ground-truth
accuracy-check plausibility gates, and the profile-aware catalog response.

Each class pins a defect that was reproduced by execution first:

* RS-45 ``perception._detect_apriltag`` stored ``centroid_px`` as
  ``int(r.center[...])``. ``int()`` TRUNCATES, so the resulting grasp point was
  biased in a fixed direction rather than merely noisy — measured against the
  real ``projection.project_pixel_to_table`` (102° HFOV / 640 px → fx 259 px,
  camera 0.50 m up, 350 poses): mean dx −0.975 mm, dy +0.927 mm, mean|err|
  1.449 mm, max 2.654 mm, and ``mean|dx| == |mean dx|`` exactly, which is the
  signature of a pure bias. The real detector genuinely carries the discarded
  information (a rendered tag36h11 came back .857/.880 px past the grid).

* RS-31 ``calibration_manager.fit_xy_correction`` accepted two points and
  reported „Restfehler 0.0 mm" — zero BY CONSTRUCTION for a 4-DOF similarity
  given 4 equations (500 random unrelated 2-point sets: 249 accepted, worst
  residual 0.000000 mm), while nothing bounded the recovered rotation or scale
  (a consistent 90° mis-pairing fitted at 0.0 mm residual on 3 AND 4 points).

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


# A tag whose true centre sits deliberately OFF the integer pixel grid.
_TRUE_CX, _TRUE_CY = 279.857040324862, 179.8803672052664
_CORNERS = [[260.4, 160.3], [299.6, 160.4], [299.5, 199.7], [260.3, 199.6]]


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


# ── RS-31: accuracy-check plausibility gates ─────────────────────────────────
@pytest.fixture
def cm(monkeypatch, tmp_path):
    monkeypatch.setenv('EDUBOTICS_CALIB_DIR', str(tmp_path))
    from importlib import reload
    from physical_ai_server.workflow import calibration_manager as _cm
    reload(_cm)
    return _cm


_SPREAD = np.array([[0.20, 0.0], [0.0, 0.20], [0.15, 0.15], [0.10, -0.05]])
_RESULT_KEYS = {
    'ok', 'message', 'xy_correction', 'yaw_bias_rad',
    'residual_mm_mean', 'residual_mm_max', 'mirror_detected', 'point_count',
}


def _rot(deg):
    a = math.radians(deg)
    return np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])


class TestVerifyMinPoints:
    def test_two_points_are_refused(self, cm):
        """At n=2 the fit is exactly determined, so the reported residual is 0.0
        mm for ANY input and cannot express quality. Measured: 500 random
        unrelated 2-point sets, worst reported residual 0.000000 mm."""
        mgr = cm.CalibrationManager()
        res = mgr.fit_xy_correction([[0.2, 0.0], [0.0, 0.2]],
                                    [[0.203, 0.002], [0.002, 0.203]])
        assert res['ok'] is False
        assert res['xy_correction'] is None
        assert res['mirror_detected'] is False
        # The floor is INTERPOLATED from VERIFY_MIN_POINTS now, and both layers
        # say „Prüfpunkte" — the node's own gate used to say „Mindestens zwei
        # Prüfpunkte" while this one said „Mindestens drei Referenzpunkte",
        # i.e. two German numbers and two German words for one floor.
        assert f'Mindestens {cm.VERIFY_MIN_POINTS} Prüfpunkte' in res['message']
        assert 'Referenzpunkte' not in res['message']

    def test_the_refusal_says_why_in_german(self, cm):
        mgr = cm.CalibrationManager()
        msg = mgr.fit_xy_correction([[0.2, 0.0], [0.0, 0.2]],
                                    [[0.2, 0.0], [0.0, 0.2]])['message']
        assert 'Restfehler' in msg and '0 mm' in msg
        assert 'ä' in msg or 'ö' in msg or 'ü' in msg or 'ß' in msg

    def test_three_points_are_still_allowed(self, cm):
        """Three is the smallest honest count — 6 equations for 4 unknowns, so
        the residual becomes informative (200 random 3-point sets gave a mean of
        97.2 mm and a MINIMUM of 7.7 mm)."""
        mgr = cm.CalibrationManager()
        pts = _SPREAD[:3]
        res = mgr.fit_xy_correction(pts.tolist(), pts.tolist())
        assert res['ok'] is True
        assert res['point_count'] == 3

    def test_min_points_constant_is_three(self, cm):
        assert cm.VERIFY_MIN_POINTS == 3


class TestVerifyRotationBound:
    @pytest.mark.parametrize('deg', [45.0, 90.0, 135.0, 180.0])
    def test_gross_rotation_is_refused(self, cm, deg):
        """A consistent index mis-pairing IS an exact similarity, so it fits with
        a genuinely 0.0 mm residual on any number of points — no residual gate
        can ever catch it, only a bound on the rotation."""
        mgr = cm.CalibrationManager()
        det = (_rot(deg) @ _SPREAD.T).T
        res = mgr.fit_xy_correction(_SPREAD.tolist(), det.tolist())
        assert res['ok'] is False
        assert res['xy_correction'] is None
        # NOT flagged as a mirror — that stays reserved for a real reflection.
        assert res['mirror_detected'] is False
        assert 'dreht' in res['message']

    def test_a_legitimate_small_rotation_is_kept(self, cm):
        """test_fit_exact_similarity_recovery pins a 15° / 1.05 recovery; the
        bound must sit well clear of it."""
        mgr = cm.CalibrationManager()
        truth = (1.05 * _rot(15.0) @ _SPREAD.T).T + np.array([0.03, -0.02])
        res = mgr.fit_xy_correction(truth.tolist(), _SPREAD.tolist())
        assert res['ok'] is True
        assert res['xy_correction'] is not None

    def test_bound_edges(self, cm):
        mgr = cm.CalibrationManager()
        inside = (_rot(29.0) @ _SPREAD.T).T
        outside = (_rot(31.0) @ _SPREAD.T).T
        assert mgr.fit_xy_correction(_SPREAD.tolist(), inside.tolist())['ok'] is True
        assert mgr.fit_xy_correction(_SPREAD.tolist(), outside.tolist())['ok'] is False
        assert cm.VERIFY_MAX_ROTATION_DEG == 30.0


class TestVerifyScaleBound:
    @pytest.mark.parametrize('factor', [0.5, 2.0])
    def test_gross_scale_is_refused(self, cm, factor):
        mgr = cm.CalibrationManager()
        res = mgr.fit_xy_correction(_SPREAD.tolist(), (_SPREAD * factor).tolist())
        assert res['ok'] is False
        assert res['xy_correction'] is None
        assert res['mirror_detected'] is False
        assert 'skaliert' in res['message']

    def test_a_small_scale_correction_is_kept(self, cm):
        mgr = cm.CalibrationManager()
        res = mgr.fit_xy_correction((_SPREAD * 1.05).tolist(), _SPREAD.tolist())
        assert res['ok'] is True

    def test_bounds_are_reciprocal(self, cm):
        # A 25% shrink and the matching 33% growth must be treated alike, so the
        # gate cannot be one-sided.
        assert cm.VERIFY_SCALE_MAX == pytest.approx(1.0 / cm.VERIFY_SCALE_MIN)

    def test_the_bounds_are_literally_0_75_and_4_3(self, cm):
        """Pinned as LITERALS, not relative to each other. The reciprocal test
        above passes for ANY value of VERIFY_SCALE_MIN, including 0.999 (which
        refuses every real correction) and 1e-9 (which refuses none) — a test
        written relative to the constant under test cannot kill a mutation of
        it."""
        assert cm.VERIFY_SCALE_MIN == 0.75
        assert cm.VERIFY_SCALE_MAX == pytest.approx(4.0 / 3.0)

    @pytest.mark.parametrize('factor', [0.80, 1.25])
    def test_a_scale_just_inside_the_bound_is_accepted(self, cm, factor):
        """The acceptance half, with LITERAL factors either side of 1.0. A
        VERIFY_SCALE_MIN of 0.999 (or a max of 1.001) fails here; the refusal
        tests alone would not notice."""
        mgr = cm.CalibrationManager()
        det = (_SPREAD * factor).tolist()
        assert mgr.fit_xy_correction(_SPREAD.tolist(), det)['ok'] is True

    @pytest.mark.parametrize('factor', [0.70, 1.45])
    def test_a_scale_just_outside_the_bound_is_refused(self, cm, factor):
        """The refusal half, also LITERAL — a VERIFY_SCALE_MIN of 0 accepts
        these."""
        mgr = cm.CalibrationManager()
        det = (_SPREAD * factor).tolist()
        res = mgr.fit_xy_correction(_SPREAD.tolist(), det)
        assert res['ok'] is False
        assert 'skaliert' in res['message']


class TestVerifyNonFiniteInputs:
    """``fit_xy_correction`` promises a dict on every path. Measured 2026-09-08
    on HEAD: a NaN/inf in TRUTH RAISED ``LinAlgError('SVD did not converge')``
    out of ``_umeyama_similarity_2d``, and a NaN/inf in DETECTED came back
    mis-diagnosed as „entartet (alle gleich)" with a numpy RuntimeWarning on
    stderr. HONEST SCOPE — there is no ordinary student route: AccuracyVerifyStep
    refuses a NaN truth value with its own German toast before the service is
    called. The reachable route is a direct /calibration/verify rosbridge call,
    which authenticates nobody."""

    @pytest.mark.parametrize('bad', [float('nan'), float('inf'), float('-inf')])
    @pytest.mark.parametrize('side', ['truth', 'detected'])
    def test_a_non_finite_point_is_refused_not_raised(self, cm, bad, side):
        mgr = cm.CalibrationManager()
        truth = _SPREAD.copy()
        det = _SPREAD.copy()
        (truth if side == 'truth' else det)[0, 0] = bad
        res = mgr.fit_xy_correction(truth.tolist(), det.tolist())
        assert res['ok'] is False
        assert res['xy_correction'] is None
        assert res['mirror_detected'] is False
        assert set(res.keys()) == _RESULT_KEYS
        assert 'Zahlenwert' in res['message']

    def test_it_does_not_mis_diagnose_as_degenerate(self, cm):
        """The old answer named the wrong cause, which sent the teacher off to
        spread the points further when the real problem was an unusable value."""
        mgr = cm.CalibrationManager()
        det = _SPREAD.copy()
        det[2, 1] = float('nan')
        assert 'entartet' not in mgr.fit_xy_correction(
            _SPREAD.tolist(), det.tolist())['message']

    def test_finite_inputs_are_completely_unaffected(self, cm):
        mgr = cm.CalibrationManager()
        assert mgr.fit_xy_correction(_SPREAD.tolist(), _SPREAD.tolist())['ok'] is True


class TestVerifyMirrorUnweakened:
    def test_mirror_is_still_detected_and_keeps_its_own_message(self, cm):
        """The new bounds run AFTER the mirror test, so a reflected capture keeps
        the more specific German message and its mirror_detected flag."""
        mgr = cm.CalibrationManager()
        truth = _SPREAD.copy()
        truth[:, 1] = -truth[:, 1]
        res = mgr.fit_xy_correction(truth.tolist(), _SPREAD.tolist())
        assert res['ok'] is False
        assert res['mirror_detected'] is True
        assert 'gespiegelt' in res['message']


class TestVerifyContract:
    def test_returned_key_set_is_unchanged_on_every_path(self, cm):
        """The ROS layer reads these by name (VerifyCalibration.srv), so no path
        may drop or rename one."""
        mgr = cm.CalibrationManager()
        mirrored = _SPREAD.copy()
        mirrored[:, 1] = -mirrored[:, 1]
        paths = [
            (_SPREAD.tolist(), _SPREAD.tolist()),                       # ok
            ([[0.2, 0.0], [0.0, 0.2]], [[0.2, 0.0], [0.0, 0.2]]),       # too few
            ([[0, 0], [1, 0], [0, 1]], [[0, 0]]),                       # mismatch
            (mirrored.tolist(), _SPREAD.tolist()),                      # mirror
            (_SPREAD.tolist(), (_rot(90.0) @ _SPREAD.T).T.tolist()),    # rotation
            (_SPREAD.tolist(), (_SPREAD * 2.0).tolist()),               # scale
            ([[0.1, 0.1]] * 4, [[0.1, 0.1]] * 4),                       # degenerate
        ]
        for truth, det in paths:
            assert set(mgr.fit_xy_correction(truth, det).keys()) == _RESULT_KEYS

    def test_a_refusal_never_reports_a_quality_number(self, cm):
        mgr = cm.CalibrationManager()
        res = mgr.fit_xy_correction(_SPREAD.tolist(),
                                    (_rot(90.0) @ _SPREAD.T).T.tolist())
        assert res['ok'] is False
        assert res['residual_mm_mean'] == 0.0
        assert res['residual_mm_max'] == 0.0
        assert res['yaw_bias_rad'] == 0.0

    def test_garbage_no_longer_fits_at_zero_residual(self, cm):
        """Before the bounds, random point sets were accepted at ~55%. Whatever
        still gets through must at least report a NON-zero residual."""
        mgr = cm.CalibrationManager()
        rng = np.random.default_rng(5)
        accepted = 0
        for _ in range(300):
            n = int(rng.integers(3, 7))
            truth = rng.uniform(-0.25, 0.25, (n, 2))
            det = rng.uniform(-0.25, 0.25, (n, 2))
            res = mgr.fit_xy_correction(truth.tolist(), det.tolist())
            if res['ok']:
                accepted += 1
                assert res['residual_mm_mean'] > 0.0
        assert accepted < 60          # was ~165/300 before the bounds

    def test_near_collinear_layouts_are_still_accepted(self, cm):
        """A teacher placing the points in a ROW is NOT refused: probing 300
        such layouts under 2 mm of measurement noise, the recovered rotation
        never exceeded 2.05° and the scale stayed within [0.961, 1.039], so the
        fit is well conditioned and the new bounds must not fire on it."""
        mgr = cm.CalibrationManager()
        rng = np.random.default_rng(11)
        s = np.linspace(-0.12, 0.12, 4)
        base = np.stack([s, np.zeros_like(s)], axis=1)
        ok = 0
        for _ in range(60):
            det = base + rng.normal(0, 0.002, base.shape)
            if mgr.fit_xy_correction(base.tolist(), det.tolist())['ok']:
                ok += 1
        assert ok > 30


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
        oc.build_object_catalog_response('edu6_studio')
        assert seen == ['edu6_studio']

    @pytest.mark.parametrize(
        'profile_id', ['omx_full', 'omx_follower', 'edu6_studio', 'edu1_studio'])
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
                    for p in ('omx_full', 'edu6_studio', 'edu1_studio')]
        assert payloads[0] == payloads[1] == payloads[2]
        # ... while the catalogs themselves genuinely DO differ.
        closes = {fixed_catalog(p).recipe_for_type('wuerfel').gripper_close_rad
                  for p in ('omx_full', 'edu6_studio', 'edu1_studio')}
        assert len(closes) == 3

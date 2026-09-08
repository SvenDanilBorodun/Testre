"""G11 — the table touch-off tap floor and the scene-YAML preservation contract.

Two defects, both reproduced by EXECUTION before anything was changed, both in
``workflow/calibration_manager.py``:

* **The touch-off residual gate was mathematically vacuous.** A plane has three
  parameters, so at exactly ``TABLE_TOUCH_POINTS_REQUIRED = 3`` taps the
  least-squares fit passes through all three taps and
  ``TABLE_TOUCH_MAX_RESIDUAL_M`` could not fire for any input. Measured over
  3000 accepted 3-tap captures at 3 mm of tap noise: rms was exactly 0.0000 mm
  in 3000/3000, and one deliberately bad tap was still accepted at 200 mm of
  error. At 4 taps the same draw gives rms mean 1.18 mm (non-zero 3000/3000) and
  a single bad tap is refused from 33 mm up. The grasp-height tail moves too:
  flat table, 3 mm tap noise, random accepted student layout, object 12 cm from
  the tap centroid, P(commanded fingertip below the true surface) 1.73 % at 3
  taps → 0.75 % at 4.

* **A second touch-off silently destroyed the ground-truth correction.**
  ``cv2.FileStorage`` cannot append, so ``_write_table_plane`` rewrites the whole
  scene YAML; it read back the transform and the board keys but NOT
  ``xy_correction`` / ``yaw_bias_rad``. ``EDUBOTICS_FORCE_RECALIBRATION`` ships
  ON, so the touch-off is mandatory on every container start while
  „Genauigkeit prüfen" is not — measured: after the next boot's touch-off,
  ``read_verify_correction()`` answered ``None`` with a green „Tisch vermessen"
  message, and every grasp ran on the uncorrected extrinsic.

Both halves are pinned with LITERAL values in BOTH directions (a refusal test
and an acceptance test), because a test written purely relative to the constant
under test survives that constant being mutated to a useless value.
"""

from __future__ import annotations

import numpy as np
import pytest


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture
def cm(monkeypatch, tmp_path):
    """A freshly imported calibration_manager bound to a temp CALIB_DIR."""
    monkeypatch.setenv('EDUBOTICS_CALIB_DIR', str(tmp_path))
    from importlib import reload
    from physical_ai_server.workflow import calibration_manager as _cm
    reload(_cm)
    return _cm


# The FK rotation of a strict-vertical grasp: EE-link local +x (the OMX approach
# axis, which is what _approach_axis_local falls back to) points at base −z.
_VERTICAL_R = np.array([[0.0, 0.0, 1.0],
                        [0.0, 1.0, 0.0],
                        [-1.0, 0.0, 0.0]], dtype=np.float64)

# Four well-separated taps: radial spread 0.1015 m (gate 0.06) and minor-axis
# spread 0.0437 m (gate 0.015), so the layout itself is never what is under
# test — only the COUNT is.
_TAPS_XY = [(0.12, -0.08), (0.24, -0.06), (0.16, 0.09), (0.19, 0.01)]
_TABLE_Z = 0.03


class _PoseSeq:
    """Scripted ``get_gripper_pose`` returning (R, t) in order, last repeating."""

    def __init__(self, ts, R=None):
        self._ts = [np.asarray(t, dtype=np.float64) for t in ts]
        self._R = _VERTICAL_R if R is None else np.asarray(R, dtype=np.float64)
        self._i = 0

    def __call__(self):
        t = self._ts[min(self._i, len(self._ts) - 1)]
        self._i += 1
        return self._R, t


def _write_extrinsic(cm, calib_dir, board_table_z: float = 0.0):
    """A minimal but REALISTIC scene_handeye.yaml: camera 0.55 m above the table
    looking straight down, which is what _solve_scene_extrinsic would have
    written. The touch-off refuses without one."""
    import cv2
    T = np.eye(4, dtype=np.float64)
    T[:3, :3] = np.array([[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, -1.0]])
    T[:3, 3] = [0.18, 0.0, 0.55]
    fs = cv2.FileStorage(str(calib_dir / 'scene_handeye.yaml'),
                         cv2.FILE_STORAGE_WRITE)
    fs.write('transform', T)
    fs.write('method', 'BOARD_ON_TABLE')
    fs.write('board_table_z', float(board_table_z))
    fs.write('board_origin_x', float(cm.BOARD_ORIGIN_X_M))
    fs.write('board_origin_y', float(cm.BOARD_ORIGIN_Y_M))
    fs.release()
    return T


def _tap(cm, calib_dir, n_taps: int, zs=None, tmp_path=None):
    """Run a touch-off with exactly ``n_taps`` taps. Returns (mgr, solve result)."""
    _write_extrinsic(cm, calib_dir)
    zs = [_TABLE_Z] * n_taps if zs is None else zs
    poses = _PoseSeq([[x, y, z] for (x, y), z in zip(_TAPS_XY[:n_taps], zs)])
    mgr = cm.CalibrationManager(get_gripper_pose=poses)
    ok, _msg = mgr.start_table_touch()
    assert ok
    for _ in range(n_taps):
        cap_ok, _n, _req, cap_msg = mgr.capture_touch_point()
        assert cap_ok, cap_msg
    return mgr, mgr.solve_table_plane('scene')


# ── the tap floor ────────────────────────────────────────────────────────────
class TestTapCountFloor:
    def test_three_taps_are_refused(self, cm, tmp_path):
        """LITERAL 3. At three taps the plane interpolates every tap, so the
        residual gate is 0.0 mm for ANY input and cannot express quality — the
        capture must not be accepted at all."""
        mgr, (ok, _rms, msg) = _tap(cm, tmp_path, 3)
        assert ok is False
        assert mgr.has_table_plane('scene') is False   # nothing persisted
        assert 'Tisch-Punkt' in msg

    def test_four_taps_are_accepted(self, cm, tmp_path):
        """LITERAL 4 — the acceptance half. Without this a floor of 99 would
        pass every refusal test in this file."""
        mgr, (ok, rms, msg) = _tap(cm, tmp_path, 4)
        assert ok is True, msg
        assert mgr.has_table_plane('scene') is True
        assert rms < 1e-9                              # a genuinely flat capture

    def test_the_required_count_is_four(self, cm):
        assert cm.TABLE_TOUCH_POINTS_REQUIRED == 4

    def test_the_wire_reports_four_so_react_can_count(self, cm, tmp_path):
        """React's TableTouchStep reads ``frames_required`` off every successful
        capture and drives its „x / y" pill from it, so the third element of the
        capture tuple is a contract, not a debug value."""
        _write_extrinsic(cm, tmp_path)
        poses = _PoseSeq([[x, y, _TABLE_Z] for (x, y) in _TAPS_XY])
        mgr = cm.CalibrationManager(get_gripper_pose=poses)
        mgr.start_table_touch()
        for expected in (1, 2, 3, 4):
            ok, count, required, msg = mgr.capture_touch_point()
            assert ok and count == expected
            assert required == 4
            assert f'{expected}/4' in msg

    def test_the_refusal_says_how_many_and_what_to_do(self, cm, tmp_path):
        """Rule §1: student-facing, literal umlauts, and ACTIONABLE — it has to
        say what to do differently, not just that something is missing."""
        _write_extrinsic(cm, tmp_path)
        poses = _PoseSeq([[x, y, _TABLE_Z] for (x, y) in _TAPS_XY])
        mgr = cm.CalibrationManager(get_gripper_pose=poses)
        mgr.start_table_touch()
        # 0 of 4 -> the plural branch, which must name the total.
        _ok, _rms, msg0 = mgr.solve_table_plane('scene')
        assert 'Es fehlen noch 4 Tisch-Punkte' in msg0
        assert '4 gut verteilte Tipp-Stellen' in msg0
        assert 'Arbeitsfläche' in msg0
        for _ in range(3):
            mgr.capture_touch_point()
        # 3 of 4 -> the singular branch. „1 Tisch-Punkte" would be broken German.
        _ok, _rms, msg3 = mgr.solve_table_plane('scene')
        assert 'Es fehlt noch 1 Tisch-Punkt —' in msg3
        assert 'Tisch-Punkte' not in msg3
        assert 'Punkt erfassen' in msg3

    def test_the_other_touch_off_gates_still_hold_at_four(self, cm, tmp_path):
        """Raising the count must not have moved the spread / collinearity /
        camera cross-check gates onto a different code path."""
        # (a) clustered: four taps 2 mm apart.
        _write_extrinsic(cm, tmp_path)
        mgr = cm.CalibrationManager(get_gripper_pose=_PoseSeq(
            [[0.18, 0.0, 0.03], [0.182, 0.001, 0.03],
             [0.181, -0.001, 0.03], [0.179, 0.002, 0.03]]))
        mgr.start_table_touch()
        for _ in range(4):
            mgr.capture_touch_point()
        ok, _rms, msg = mgr.solve_table_plane('scene')
        assert ok is False and ('nah' in msg or 'Linie' in msg)

        # (b) collinear: four taps on one line (tilt unconstrained).
        mgr = cm.CalibrationManager(get_gripper_pose=_PoseSeq(
            [[0.12, 0.0, 0.03], [0.16, 0.0, 0.03],
             [0.21, 0.0, 0.03], [0.26, 0.0, 0.03]]))
        mgr.start_table_touch()
        for _ in range(4):
            mgr.capture_touch_point()
        ok, _rms, msg = mgr.solve_table_plane('scene')
        assert ok is False and 'Linie' in msg

        # (c) camera cross-check: a touch plane 0.5 m off the board height.
        mgr, (ok, _rms, msg) = _tap(cm, tmp_path, 4, zs=[0.5] * 4)
        assert ok is False
        assert 'Kamerakalibrierung' in msg or 'Tischhöhe' in msg

        # (d) per-tap verticality still refuses BEFORE recording anything.
        _write_extrinsic(cm, tmp_path)
        tilt = np.array([[0.0, 0.0, 1.0], [0.0, 1.0, 0.0], [-1.0, 0.0, 0.0]])
        # 25° about base y, well past TABLE_TOUCH_MAX_TILT_DEG (12°).
        a = np.radians(25.0)
        ry = np.array([[np.cos(a), 0.0, np.sin(a)],
                       [0.0, 1.0, 0.0],
                       [-np.sin(a), 0.0, np.cos(a)]])
        mgr = cm.CalibrationManager(get_gripper_pose=_PoseSeq(
            [[x, y, _TABLE_Z] for (x, y) in _TAPS_XY], R=ry @ tilt))
        mgr.start_table_touch()
        ok, count, required, msg = mgr.capture_touch_point()
        assert ok is False and count == 0 and required == 4
        assert 'schräg' in msg or 'senkrecht' in msg


class TestResidualGateIsNoLongerVacuous:
    """The reason the count moved, asserted directly rather than described."""

    def test_a_three_point_fit_has_zero_residual_for_any_input(self, cm):
        """Independent of the constant: three points, three plane parameters.
        Whatever heights the student taps, the fit reproduces them exactly, so
        TABLE_TOUCH_MAX_RESIDUAL_M cannot fire."""
        rng = np.random.default_rng(20260908)
        for _ in range(200):
            xy = np.stack([rng.uniform(0.10, 0.28, 3),
                           rng.uniform(-0.14, 0.14, 3)], axis=1)
            z = rng.uniform(-0.5, 0.5, 3)          # deliberately absurd heights
            pts = np.column_stack([xy, z])
            _coeff, rms = cm.CalibrationManager._fit_plane(pts)
            assert rms < 1e-9

    def test_a_four_point_fit_reports_a_real_residual(self, cm):
        rng = np.random.default_rng(20260908)
        worst = 0.0
        for _ in range(200):
            xy = np.stack([rng.uniform(0.10, 0.28, 4),
                           rng.uniform(-0.14, 0.14, 4)], axis=1)
            z = rng.normal(0.0, 0.003, 4)
            _coeff, rms = cm.CalibrationManager._fit_plane(np.column_stack([xy, z]))
            worst = max(worst, rms)
        assert worst > 1e-4        # measured mean 1.18 mm, max 5.36 mm

    def test_one_bad_tap_is_now_refused(self, cm, tmp_path):
        """LITERAL: a 4-tap capture whose last tap is 60 mm high (the student
        tapped a book / never reached the table) is refused with the flatness
        message. At three taps the identical mistake was accepted silently."""
        mgr, (ok, rms, msg) = _tap(
            cm, tmp_path, 4, zs=[_TABLE_Z, _TABLE_Z, _TABLE_Z, _TABLE_Z + 0.060])
        assert ok is False
        assert 'ebenen Fläche' in msg
        assert rms > cm.TABLE_TOUCH_MAX_RESIDUAL_M
        assert mgr.has_table_plane('scene') is False

    def test_a_small_tap_error_is_still_accepted(self, cm, tmp_path):
        """The acceptance half with a LITERAL value — a 4 mm sloppy tap is
        ordinary hand-guiding and must not cost the student the whole capture."""
        mgr, (ok, rms, msg) = _tap(
            cm, tmp_path, 4, zs=[_TABLE_Z, _TABLE_Z, _TABLE_Z, _TABLE_Z + 0.004])
        assert ok is True, msg
        assert 0.0 < rms < cm.TABLE_TOUCH_MAX_RESIDUAL_M


# ── the scene-YAML preservation contract ─────────────────────────────────────
_SPREAD = [[0.20, 0.0], [0.0, 0.20], [0.15, 0.15], [0.10, -0.05]]


def _solve_a_correction(mgr):
    """Fit and persist a small, legitimate ground-truth correction."""
    detected = [[x * 0.98 + 0.001, y * 0.98 - 0.001] for x, y in _SPREAD]
    res = mgr.fit_xy_correction(_SPREAD, detected,
                                truth_yaws=[0.30], detected_yaws=[0.25])
    assert res['ok'] is True, res['message']
    assert mgr.write_verify_correction(
        'scene', res['xy_correction'], float(res['yaw_bias_rad']),
        float(res['residual_mm_mean']), int(res['point_count'])) is True
    return res


class TestVerifyCorrectionSurvivesARepeatTouchOff:
    def test_a_second_touch_off_keeps_the_correction(self, cm, tmp_path):
        """THE defect. EDUBOTICS_FORCE_RECALIBRATION ships ON, so the touch-off
        is re-run at the start of every lesson while „Genauigkeit prüfen" is
        not — the correction used to be gone by the second lesson."""
        mgr, (ok, _rms, msg) = _tap(cm, tmp_path, 4)
        assert ok, msg
        fitted = _solve_a_correction(mgr)
        before = mgr.read_verify_correction('scene')
        assert before is not None and before['xy_correction'] is not None

        # Next lesson: the container restarted, so the teacher re-runs ONLY
        # „Tisch vermessen" — at a slightly different measured height.
        poses = _PoseSeq([[x, y, _TABLE_Z + 0.001] for (x, y) in _TAPS_XY])
        mgr2 = cm.CalibrationManager(get_gripper_pose=poses)
        mgr2.start_table_touch()
        for _ in range(4):
            mgr2.capture_touch_point()
        ok2, _rms2, msg2 = mgr2.solve_table_plane('scene')
        assert ok2, msg2

        after = mgr2.read_verify_correction('scene')
        assert after is not None, 'the ground-truth correction was destroyed'
        np.testing.assert_allclose(
            np.asarray(after['xy_correction'], dtype=np.float64),
            np.asarray(before['xy_correction'], dtype=np.float64), atol=1e-12)
        assert after['yaw_bias_rad'] == pytest.approx(
            float(fitted['yaw_bias_rad']), abs=1e-12)

    def test_the_second_touch_off_still_wins_on_the_table_height(self, cm, tmp_path):
        """Carrying the correction forward must NOT carry the OLD table height
        forward — z_table is what every grasp descends to and the re-measurement
        is the whole point of re-running the step."""
        import cv2
        mgr, (ok, _rms, _msg) = _tap(cm, tmp_path, 4)
        assert ok
        _solve_a_correction(mgr)
        poses = _PoseSeq([[x, y, _TABLE_Z + 0.004] for (x, y) in _TAPS_XY])
        mgr2 = cm.CalibrationManager(get_gripper_pose=poses)
        mgr2.start_table_touch()
        for _ in range(4):
            mgr2.capture_touch_point()
        assert mgr2.solve_table_plane('scene')[0]
        fs = cv2.FileStorage(str(tmp_path / 'scene_handeye.yaml'),
                             cv2.FILE_STORAGE_READ)
        z = float(fs.getNode('z_table').real())
        fs.release()
        assert z == pytest.approx(_TABLE_Z + 0.004, abs=1e-9)

    def test_the_extrinsic_transform_is_still_carried_forward(self, cm, tmp_path):
        """The pre-existing half of the contract, kept under test so the new
        read-back cannot quietly drop it."""
        import cv2
        _write_extrinsic(cm, tmp_path)
        fs = cv2.FileStorage(str(tmp_path / 'scene_handeye.yaml'),
                             cv2.FILE_STORAGE_READ)
        original = fs.getNode('transform').mat()
        fs.release()
        mgr, (ok, _rms, _msg) = _tap(cm, tmp_path, 4)
        assert ok
        fs = cv2.FileStorage(str(tmp_path / 'scene_handeye.yaml'),
                             cv2.FILE_STORAGE_READ)
        after = fs.getNode('transform').mat()
        board_z = float(fs.getNode('board_table_z').real())
        fs.release()
        np.testing.assert_allclose(after, original, atol=1e-12)
        assert board_z == pytest.approx(0.0)

    def test_a_rig_that_never_verified_gets_no_invented_correction(self, cm, tmp_path):
        """Fail-safe direction: absent keys must stay absent, never be written
        as an identity/zero correction that later reads as 'verified'."""
        mgr, (ok, _rms, _msg) = _tap(cm, tmp_path, 4)
        assert ok
        assert mgr.read_verify_correction('scene') is None

    def test_a_fresh_extrinsic_deliberately_DOES_clear_the_correction(
            self, cm, tmp_path):
        """The asymmetry is intended and must not be 'harmonised' away: the
        correction is fitted against one T_cam_to_base, so re-aiming the camera
        invalidates it. _write_table_plane preserves (it never touches the
        transform); _solve_scene_extrinsic truncates (it replaces it)."""
        T_cam_to_base = _write_extrinsic(cm, tmp_path)
        mgr, (ok, _rms, _msg) = _tap(cm, tmp_path, 4)
        assert ok
        _solve_a_correction(mgr)
        assert mgr.read_verify_correction('scene') is not None

        # Feed the extrinsic solver a board pose that reproduces exactly the
        # camera placement above: T_board_to_cam = inv(T_cam_to_base) @ T_board_to_base.
        T_board_to_base = cm.CalibrationManager._board_to_base_transform()
        T_board_to_cam = np.linalg.inv(T_cam_to_base) @ T_board_to_base
        buf = cm.HandEyeCaptureBuffer()
        buf.R_target2cam = [T_board_to_cam[:3, :3]]
        buf.t_target2cam = [T_board_to_cam[:3, 3].reshape(3, 1)]
        mgr._handeye_buffers['scene'] = buf
        ok2, _e, _d, msg2 = mgr._solve_scene_extrinsic('scene')
        assert ok2, msg2
        assert mgr.read_verify_correction('scene') is None
        assert mgr.has_table_plane('scene') is False

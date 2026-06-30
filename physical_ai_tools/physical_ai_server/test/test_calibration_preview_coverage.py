"""Live ChArUco preview + capture coverage/quality unit tests.

Covers the Phase-2 wizard additions to ``CalibrationManager``:
  * ``detect_preview`` — the LOCK-FREE live corner overlay used by the intrinsic
    wizard (must detect a rendered board, return parallel corner arrays + a
    board-area-fill %, and must NEVER block on ``self._lock``);
  * the ``_coverage_cell`` / ``_quality_from_rms`` / ``_charuco_centroid_px``
    helpers and the ``last_intrinsic_capture_meta`` accessor that feed the
    CalibrationCaptureFrame ``coverage_cell`` / ``quality`` response fields.

unittest-style (no pytest fixtures) so it runs under ``python -m unittest``.
Renders a synthetic ChArUco board with the real cv2 contrib aruco build the
container ships, so it exercises the genuine detection path."""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from importlib import reload

import numpy as np

try:
    import cv2
    _HAVE_CV2 = True
except Exception:  # pragma: no cover - cv2 always present in the container
    _HAVE_CV2 = False


def _aruco_detect_supported() -> bool:
    """The container ships opencv-contrib 4.10 whose ``cv2.aruco.detectMarkers``
    legacy free function the detector uses. A newer host opencv-python (4.13)
    drops that free function, so the detection-RENDERING tests skip there while
    the deterministic helper/lock tests still run."""
    if not _HAVE_CV2:
        return False
    try:
        d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_250)
        params = cv2.aruco.DetectorParameters()
        cv2.aruco.detectMarkers(np.zeros((64, 64), dtype=np.uint8), d, parameters=params)
        return True
    except Exception:
        return False


_DETECT_OK = _aruco_detect_supported()


@unittest.skipUnless(_HAVE_CV2, 'cv2 (contrib aruco) required')
class CalibrationPreviewCoverageTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._prev_env = os.environ.get('EDUBOTICS_CALIB_DIR')
        os.environ['EDUBOTICS_CALIB_DIR'] = self._tmp.name
        from physical_ai_server.workflow import calibration_manager as cm
        reload(cm)
        self.cm = cm
        self.CalibrationManager = cm.CalibrationManager

    def tearDown(self):
        if self._prev_env is None:
            os.environ.pop('EDUBOTICS_CALIB_DIR', None)
        else:
            os.environ['EDUBOTICS_CALIB_DIR'] = self._prev_env
        self._tmp.cleanup()

    # -- helpers --------------------------------------------------------
    def _board_frame(self, w=640, h=480):
        """Render the manager's ChArUco board to a BGR frame the detector can
        find. The board is rendered at the manager's own square/marker sizes."""
        board, _ = self.cm._build_charuco_board()
        gray = board.generateImage((w, h))
        return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)

    def _write_intrinsics(self, camera='scene'):
        K = np.array([[600.0, 0, 320], [0, 600, 240], [0, 0, 1]], dtype=np.float64)
        dist = np.zeros((5, 1), dtype=np.float64)
        fs = cv2.FileStorage(
            os.path.join(self._tmp.name, f'{camera}_intrinsics.yaml'),
            cv2.FILE_STORAGE_WRITE,
        )
        fs.write('camera_matrix', K)
        fs.write('distortion_coefficients', dist)
        fs.write('image_width', 640)
        fs.write('image_height', 480)
        fs.release()

    # -- coverage / quality helpers -------------------------------------
    def test_coverage_cell_grid_mapping(self):
        f = self.CalibrationManager._coverage_cell
        size = (640, 480)  # (W, H) → cols 0..3 at 160px, rows 0..3 at 120px
        self.assertEqual(f(10, 10, size), 0)         # top-left
        self.assertEqual(f(639, 479, size), 15)      # bottom-right
        self.assertEqual(f(320, 240, size), 10)      # centre → row1*4 + col2
        # Out-of-range clamps into the grid (never raises / returns >15).
        self.assertEqual(f(5000, 5000, size), 15)
        self.assertEqual(f(-5, -5, size), 0)
        # Degenerate image size → cell 0, no crash.
        self.assertEqual(f(100, 100, (0, 0)), 0)

    def test_quality_from_rms_thresholds(self):
        f = self.CalibrationManager._quality_from_rms
        self.assertEqual(f(0.5), 3)     # GOOD
        self.assertEqual(f(1.5), 2)     # OK
        self.assertEqual(f(3.0), 1)     # POOR
        self.assertEqual(f(None), 0)    # unknown
        self.assertEqual(f(float('nan')), 0)
        self.assertEqual(f(float('inf')), 0)

    def test_charuco_centroid_px(self):
        corners = np.array([[[0.0, 0.0]], [[10.0, 20.0]], [[20.0, 40.0]]],
                           dtype=np.float32)
        cx, cy = self.CalibrationManager._charuco_centroid_px(corners)
        self.assertAlmostEqual(cx, 10.0, places=5)
        self.assertAlmostEqual(cy, 20.0, places=5)

    # -- detect_preview -------------------------------------------------
    def test_detect_preview_none(self):
        # The None path returns early before any detection call — runs anywhere.
        mgr = self.CalibrationManager()
        self.assertEqual(mgr.detect_preview(None), (False, [], [], 0))

    @unittest.skipUnless(_DETECT_OK, 'cv2.aruco.detectMarkers (legacy free fn) required')
    def test_detect_preview_black_frame(self):
        mgr = self.CalibrationManager()
        black = np.zeros((480, 640, 3), dtype=np.uint8)
        detected, xs, ys, pct = mgr.detect_preview(black)
        self.assertFalse(detected)
        self.assertEqual(xs, [])
        self.assertEqual(ys, [])
        self.assertEqual(pct, 0)

    @unittest.skipUnless(_DETECT_OK, 'cv2.aruco.detectMarkers (legacy free fn) required')
    def test_detect_preview_finds_rendered_board(self):
        mgr = self.CalibrationManager()
        frame = self._board_frame()
        detected, xs, ys, pct = mgr.detect_preview(frame)
        self.assertTrue(detected, 'rendered ChArUco board must be detected')
        self.assertGreaterEqual(len(xs), 4)
        self.assertEqual(len(xs), len(ys))            # parallel arrays
        self.assertTrue(all(isinstance(v, float) for v in xs))
        self.assertGreater(pct, 0)
        self.assertLessEqual(pct, 100)

    def test_detect_preview_is_lock_free(self):
        """A poll must never queue behind a multi-second solve — detect_preview
        must run to completion while ``self._lock`` is held by another thread.
        Asserts COMPLETION (not detection content) so it runs on any cv2 build:
        even when the detector free fn is absent and detect_preview raises, it
        does so WITHOUT acquiring the lock, so the worker still completes."""
        mgr = self.CalibrationManager()
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        result_box = {}

        def worker():
            try:
                result_box['r'] = mgr.detect_preview(frame)
            except Exception as e:  # detector free fn absent on a newer host
                result_box['err'] = e
            finally:
                result_box['done'] = True

        with mgr._lock:
            t = threading.Thread(target=worker)
            t.start()
            t.join(timeout=3.0)
            completed = result_box.get('done', False)
        # After the `with` block releases the lock, a (hypothetically) blocked
        # worker would finish; join so it never leaks past the test.
        t.join(timeout=3.0)
        self.assertTrue(completed,
                        'detect_preview blocked on self._lock (not lock-free)')

    # -- capture meta (coverage_cell / quality) -------------------------
    def test_last_intrinsic_capture_meta_zero_before_capture(self):
        mgr = self.CalibrationManager()
        self.assertEqual(mgr.last_intrinsic_capture_meta('scene'), (0, 0))
        mgr.start_step('scene', 'intrinsic')
        # buffer exists but no capture yet → (0, 0)
        self.assertEqual(mgr.last_intrinsic_capture_meta('scene'), (0, 0))

    @unittest.skipUnless(_DETECT_OK, 'cv2.aruco.detectMarkers (legacy free fn) required')
    def test_intrinsic_capture_sets_coverage_and_quality(self):
        mgr = self.CalibrationManager()
        mgr.start_step('scene', 'intrinsic')
        frame = self._board_frame()
        ok, captured, required, _rms, _msg = mgr.capture_frame('scene', bgr=frame)
        self.assertTrue(ok, 'rendered board capture should succeed')
        self.assertEqual(captured, 1)
        cell, quality = mgr.last_intrinsic_capture_meta('scene')
        self.assertTrue(0 <= cell <= 15)
        self.assertTrue(0 <= quality <= 3)
        # The board is centred in the frame → a non-edge cell.
        self.assertIn(cell, (5, 6, 9, 10))

    def test_meta_zero_for_extrinsic_step(self):
        """During an extrinsic step there is no intrinsic buffer for the camera,
        so the meta accessor returns (0, 0) (the capture callback then leaves
        coverage_cell/quality at 0 for an extrinsic capture)."""
        self._write_intrinsics('scene')
        mgr = self.CalibrationManager()
        ok, _ = mgr.start_step('scene', 'extrinsic')
        self.assertTrue(ok)
        self.assertEqual(mgr.last_intrinsic_capture_meta('scene'), (0, 0))


if __name__ == '__main__':
    unittest.main()

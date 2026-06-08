"""Tests for the camera-preview frame encoder in gui_app.

The shipped .exe runs on Python 3.11 → Tcl/Tk 8.6, which cannot reliably
render raw binary PPM through the image -data option (NUL bytes truncate the
Tcl string). The fix encodes each frame to base64-PNG (binary-safe, renders on
Tk 8.6 and 9.0). These tests cover `_frame_to_photo_data` directly.

cv2 + numpy are required to exercise the encoder. If they are unavailable the
encoder-dependent tests are skipped (the function returns None defensively, but
asserting the PNG signature needs a real encode).

Loading strategy: import `_frame_to_photo_data` from gui_app if that import
works headless; otherwise load just the function from the source file via
importlib so a runner without tkinter/webview can still run these.
"""

import base64
import importlib.util
import os
import unittest

try:  # numpy/cv2 may be absent on a bare runner.
    import numpy as np
    import cv2  # noqa: F401
    _HAVE_CV2 = True
except Exception:  # noqa: BLE001
    _HAVE_CV2 = False


def _load_frame_to_photo_data():
    """Return the `_frame_to_photo_data` callable.

    Prefers a normal package import (`from gui.app import gui_app`); falls back
    to loading the single function from the source file when importing the full
    module fails headless (no tkinter/webview).
    """
    try:
        from gui.app import gui_app
        return gui_app._frame_to_photo_data
    except Exception:  # noqa: BLE001 — headless fallback
        src = os.path.join(
            os.path.dirname(__file__), "..", "gui", "app", "gui_app.py")
        spec = importlib.util.spec_from_file_location("gui_app_probe", src)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception:  # noqa: BLE001
            # Even module exec failed (tkinter import at top). Re-parse the
            # function body in isolation as a last resort.
            with open(src, "r", encoding="utf-8") as fh:
                source = fh.read()
            marker = "def _frame_to_photo_data("
            start = source.index(marker)
            # Capture until the next top-level `def `/`class ` after it.
            rest = source[start:]
            end = len(rest)
            for token in ("\ndef ", "\nclass "):
                idx = rest.find(token, len(marker))
                if idx != -1:
                    end = min(end, idx)
            snippet = rest[:end]
            ns = {"base64": base64}
            exec(snippet, ns)  # noqa: S102 — trusted in-repo source
            return ns["_frame_to_photo_data"]
        return module._frame_to_photo_data


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@unittest.skipUnless(_HAVE_CV2, "cv2/numpy not available")
class TestFrameToPhotoData(unittest.TestCase):

    def setUp(self):
        self.encode = _load_frame_to_photo_data()

    def test_bgr_frame_returns_base64_png(self):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        frame[:, :, 2] = 255  # red in BGR
        data = self.encode(frame)
        self.assertIsNotNone(data)
        self.assertTrue(len(data) > 0)
        decoded = base64.b64decode(data)
        self.assertTrue(decoded.startswith(PNG_SIGNATURE))

    def test_grayscale_frame_encodes(self):
        frame = np.zeros((480, 640), dtype=np.uint8)
        frame[100:200, 100:200] = 200
        data = self.encode(frame)
        self.assertIsNotNone(data)
        decoded = base64.b64decode(data)
        self.assertTrue(decoded.startswith(PNG_SIGNATURE))

    def test_bgra_frame_encodes(self):
        frame = np.zeros((480, 640, 4), dtype=np.uint8)
        frame[:, :, 3] = 255  # opaque alpha
        data = self.encode(frame)
        self.assertIsNotNone(data)
        decoded = base64.b64decode(data)
        self.assertTrue(decoded.startswith(PNG_SIGNATURE))

    def test_return_is_pure_ascii_base64(self):
        frame = np.full((480, 640, 3), 128, dtype=np.uint8)
        data = self.encode(frame)
        self.assertIsNotNone(data)
        # No raw NUL bytes — that is the whole point vs raw PPM.
        self.assertNotIn(b"\x00", data)
        # Decodes cleanly as ascii and round-trips through base64.
        text = data.decode("ascii")  # raises if non-ascii
        self.assertEqual(base64.b64encode(base64.b64decode(text)), data)


if __name__ == "__main__":
    unittest.main()

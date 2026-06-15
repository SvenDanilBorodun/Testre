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
import textwrap
import time
import types
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

    def test_full_capture_frame_downscaled_for_preview(self):
        # Regression: the full 640x480 capture frame must be downscaled to a
        # display width that fits TWO previews side-by-side in the 700px setup
        # window. A full-res 640-wide preview overflowed and clipped the second
        # camera + the gripper/scene role combos (defeating the disambiguation).
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        data = self.encode(frame)
        self.assertIsNotNone(data)
        decoded = cv2.imdecode(
            np.frombuffer(base64.b64decode(data), dtype=np.uint8),
            cv2.IMREAD_COLOR)
        self.assertLessEqual(decoded.shape[1], 320)
        # Aspect ratio preserved (4:3 -> 320x240).
        self.assertEqual(
            decoded.shape[0],
            int(round(decoded.shape[1] * 480 / 640)))

    def test_small_frame_not_upscaled(self):
        # Sub-threshold frames pass through unchanged (no upscaling, and the
        # isolated-snippet test loader stays self-contained).
        frame = np.zeros((48, 64, 3), dtype=np.uint8)
        data = self.encode(frame)
        self.assertIsNotNone(data)
        decoded = cv2.imdecode(
            np.frombuffer(base64.b64decode(data), dtype=np.uint8),
            cv2.IMREAD_COLOR)
        self.assertEqual(decoded.shape[1], 64)

    def test_return_is_pure_ascii_base64(self):
        frame = np.full((480, 640, 3), 128, dtype=np.uint8)
        data = self.encode(frame)
        self.assertIsNotNone(data)
        # No raw NUL bytes — that is the whole point vs raw PPM.
        self.assertNotIn(b"\x00", data)
        # Decodes cleanly as ascii and round-trips through base64.
        text = data.decode("ascii")  # raises if non-ascii
        self.assertEqual(base64.b64encode(base64.b64decode(text)), data)


def _load_stop_camera_previews():
    """Return the `_stop_camera_previews` method as a standalone callable.

    Prefers the real class; falls back to extracting + dedenting the method
    source so this runs on a headless runner without tkinter/webview (the same
    constraint the rest of this file works around).
    """
    try:
        from gui.app.gui_app import EduBoticsApp
        return EduBoticsApp._stop_camera_previews
    except Exception:  # noqa: BLE001 — headless fallback (no tkinter/webview)
        src = os.path.join(
            os.path.dirname(__file__), "..", "gui", "app", "gui_app.py")
        with open(src, "r", encoding="utf-8") as fh:
            source = fh.read()
        marker = "    def _stop_camera_previews(self):"
        start = source.index(marker)
        rest = source[start:]
        end = rest.find("\n    def ", len(marker))
        snippet = textwrap.dedent(rest[: end if end != -1 else len(rest)])
        ns = {}
        exec(compile(snippet, src, "exec"), ns)  # noqa: S102 — in-repo source
        return ns["_stop_camera_previews"]


class _FakeRoot:
    def after_cancel(self, _id):  # pragma: no cover — trivial stub
        pass


class TestPreviewLifecycleInit(unittest.TestCase):
    """Regression: the FIRST preview of the process must not silently stall.

    `_stop_camera_previews` initialises every preview-state attribute; it used
    to set `_preview_after_id` ONLY inside an `if ... is not None` block, so on
    the first preview the attribute never existed and `_install`'s
    `if self._preview_after_id is None` raised AttributeError into Tk's callback
    handler (silent in the packaged .exe) — the poll was never scheduled and the
    label stayed on "Vorschau lädt ..." forever with the cameras visibly free.
    """

    def test_stop_initialises_after_id_on_first_call(self):
        stop = _load_stop_camera_previews()
        fake = types.SimpleNamespace(root=_FakeRoot())
        # First-ever call: no _preview_after_id exists yet.
        self.assertFalse(hasattr(fake, "_preview_after_id"))
        stop(fake)
        # The attribute must now EXIST and be None ...
        self.assertTrue(hasattr(fake, "_preview_after_id"))
        self.assertIsNone(fake._preview_after_id)
        # ... so the exact _install scheduling guard cannot raise.
        try:
            should_schedule = fake._preview_after_id is None
        except AttributeError:  # pragma: no cover
            self.fail("_preview_after_id missing — _install would AttributeError")
        self.assertTrue(should_schedule)

    def test_stop_cancels_and_clears_existing_after_id(self):
        # A second teardown with a live scheduled id must cancel it and reset
        # to None (so a stale id can't survive into the next preview session).
        stop = _load_stop_camera_previews()
        cancelled = []
        root = types.SimpleNamespace(after_cancel=cancelled.append)
        fake = types.SimpleNamespace(root=root, _preview_after_id="after#42")
        stop(fake)
        self.assertEqual(cancelled, ["after#42"])
        self.assertIsNone(fake._preview_after_id)


def _load_module_func(func_name, extra_ns=None):
    """Load a module-level function from gui_app headless (snippet-extract)."""
    try:
        from gui.app import gui_app
        return getattr(gui_app, func_name)
    except Exception:  # noqa: BLE001 — headless fallback
        src = os.path.join(
            os.path.dirname(__file__), "..", "gui", "app", "gui_app.py")
        with open(src, "r", encoding="utf-8") as fh:
            source = fh.read()
        marker = f"def {func_name}("
        start = source.index(marker)
        rest = source[start:]
        end = len(rest)
        for token in ("\ndef ", "\nclass "):
            idx = rest.find(token, len(marker))
            if idx != -1:
                end = min(end, idx)
        ns = dict(extra_ns or {})
        exec(compile(rest[:end], src, "exec"), ns)  # noqa: S102 — in-repo src
        return ns[func_name]


def _load_method(method_name):
    """Load a class method from gui_app as a standalone callable (dedented)."""
    try:
        from gui.app.gui_app import EduBoticsApp
        return getattr(EduBoticsApp, method_name)
    except Exception:  # noqa: BLE001 — headless fallback
        src = os.path.join(
            os.path.dirname(__file__), "..", "gui", "app", "gui_app.py")
        with open(src, "r", encoding="utf-8") as fh:
            source = fh.read()
        marker = f"    def {method_name}(self"
        start = source.index(marker)
        rest = source[start:]
        end = rest.find("\n    def ", len(marker))
        snippet = textwrap.dedent(rest[: end if end != -1 else len(rest)])
        ns = {}
        exec(compile(snippet, src, "exec"), ns)  # noqa: S102 — in-repo source
        return ns[method_name]


class _FakeVar:
    def __init__(self, value=""):
        self._v = value

    def get(self):
        return self._v

    def set(self, value):
        self._v = value


class _FakeCam:
    def __init__(self, name):
        self.name = name
        self.role = None


class TestPreviewRoleAssignment(unittest.TestCase):
    """The per-preview role picker (replaces the old gripper/scene dropdowns)."""

    def _make(self):
        fn = _load_method("_on_preview_role_changed")
        cams = [_FakeCam("A"), _FakeCam("B")]
        owner = types.SimpleNamespace(
            _preview_role_vars=[_FakeVar("Greifer"), _FakeVar("Szene")],
            hardware=types.SimpleNamespace(cameras=[]),
            _log=lambda *a, **k: None,
        )
        return fn, owner, cams

    def test_default_keeps_gripper_first(self):
        fn, owner, cams = self._make()
        fn(owner, cams, 0)  # camera 1 = Greifer
        self.assertEqual(cams[0].role, "gripper")
        self.assertEqual(cams[1].role, "scene")
        self.assertEqual(owner.hardware.cameras, [cams[0], cams[1]])

    def test_picking_greifer_on_second_flips_first_and_reorders(self):
        fn, owner, cams = self._make()
        owner._preview_role_vars[1].set("Greifer")  # user picks Greifer on cam 2
        fn(owner, cams, 1)
        self.assertEqual(cams[1].role, "gripper")
        self.assertEqual(cams[0].role, "scene")
        # The other selector auto-flips to the opposite role ...
        self.assertEqual(owner._preview_role_vars[0].get(), "Szene")
        # ... and hardware.cameras stays gripper-first.
        self.assertEqual(owner.hardware.cameras, [cams[1], cams[0]])


class _FakeCapBacklog:
    """grab() returns instantly while a backlog exists, then 'waits' for fresh."""

    def __init__(self, backlog):
        self._remaining = backlog
        self.grab_calls = 0
        self.retrieve_calls = 0
        self.read_calls = 0

    def grab(self):
        self.grab_calls += 1
        if self._remaining > 0:
            self._remaining -= 1
            return True  # instant — pulled from the backlog
        time.sleep(0.02)  # >12 ms → modelled as waiting for a fresh capture
        return True

    def retrieve(self):
        self.retrieve_calls += 1
        return True, ("fresh", self.grab_calls)

    def read(self):
        self.read_calls += 1
        return True, ("plain",)


class TestReadFreshestFrame(unittest.TestCase):
    """Draining the backlog is what fixes the 'lags very much' preview delay."""

    def test_drains_backlog_then_decodes_once(self):
        read = _load_module_func("_read_freshest_frame", {"time": time})
        cap = _FakeCapBacklog(backlog=5)
        ok, frame = read(cap)
        self.assertTrue(ok)
        self.assertEqual(frame[0], "fresh")
        self.assertEqual(cap.retrieve_calls, 1)  # decode exactly once
        self.assertEqual(cap.grab_calls, 6)      # 5 backlog + 1 fresh
        self.assertEqual(cap.read_calls, 0)      # never the slow plain read

    def test_drain_is_bounded(self):
        read = _load_module_func("_read_freshest_frame", {"time": time})
        cap = _FakeCapBacklog(backlog=100)
        ok, _frame = read(cap)
        self.assertTrue(ok)
        self.assertLessEqual(cap.grab_calls, 12)  # bounded — can't spin forever
        self.assertEqual(cap.retrieve_calls, 1)

    def test_falls_back_to_plain_read_without_grab(self):
        read = _load_module_func("_read_freshest_frame", {"time": time})

        class _NoGrab:
            def __init__(self):
                self.read_calls = 0

            def grab(self):
                return False

            def retrieve(self):  # pragma: no cover — must not be reached
                raise AssertionError("retrieve must not run when grab fails")

            def read(self):
                self.read_calls += 1
                return True, ("plain",)

        cap = _NoGrab()
        ok, frame = read(cap)
        self.assertTrue(ok)
        self.assertEqual(frame[0], "plain")
        self.assertEqual(cap.read_calls, 1)


if __name__ == "__main__":
    unittest.main()

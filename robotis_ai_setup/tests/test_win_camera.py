"""Tests for win_camera.py (native Windows camera enumeration).

Platform-agnostic: cv2 is imported lazily, and the PnP query is mocked, so
these run on Linux CI and a Windows workstation alike.
"""

import os
import types
import unittest
from unittest.mock import patch

from gui.app import constants, win_camera


def _fake_completed(stdout="", returncode=0):
    class _R:
        pass
    r = _R()
    r.stdout = stdout
    r.stderr = ""
    r.returncode = returncode
    return r


class TestCameraInfo(unittest.TestCase):

    def test_device_key_prefers_location(self):
        info = win_camera.CameraInfo(index=2, name="Cam", location="USB-Port_#0003",
                                     instance_id="USB\\VID_x")
        self.assertEqual(info.device_key(), "USB-Port_#0003")

    def test_device_key_falls_back_to_instance_then_index(self):
        self.assertEqual(
            win_camera.CameraInfo(2, "Cam", "", "INST").device_key(), "INST")
        self.assertEqual(
            win_camera.CameraInfo(2, "Cam", "", "").device_key(), "index:2")


class TestPnpParsing(unittest.TestCase):

    def test_parses_name_location_instance(self):
        out = (
            "Innomaker-U20CAM-720P|Port_#0003.Hub_#0001|USB\\VID_0C45&PID_6367\\A\n"
            "Innomaker-U20CAM-720P|Port_#0004.Hub_#0001|USB\\VID_0C45&PID_6367\\B\n"
        )
        with patch("gui.app.win_camera.sys") as msys, \
             patch("gui.app.win_camera.subprocess.run", return_value=_fake_completed(out)):
            msys.platform = "win32"
            details = win_camera._pnp_camera_details()
        self.assertEqual(len(details), 2)
        self.assertEqual(details[0]["name"], "Innomaker-U20CAM-720P")
        self.assertEqual(details[0]["location"], "Port_#0003.Hub_#0001")
        self.assertEqual(details[1]["location"], "Port_#0004.Hub_#0001")

    def test_drops_non_usb_imaging_devices(self):
        # A networked HP LaserJet MFP's WSD scanner enumerates under the
        # Camera,Image PnP class set (Image class) and, sorting first, used to
        # mislabel the real Innomaker camera with the printer's name via the
        # positional name/location zip in list_windows_cameras(). The USB-only
        # filter must drop it (and the phantom ROOT\IMAGE SCSI scanner).
        out = (
            "NPIAC5861 (HP LaserJet MFP M140w)|http://192.168.178.49:3911/|"
            "SWD\\DAFWSDPROVIDER\\URN:UUID:38BA/SCANNER1\n"
            "Innomaker-U20CAM-720P|Port_#0003.Hub_#0001|"
            "USB\\VID_0C45&PID_6367&MI_00\\7&2BD3AF6B&0&0000\n"
            "SCSI-Scangeraet||ROOT\\IMAGE\\0000\n"
        )
        with patch("gui.app.win_camera.sys") as msys, \
             patch("gui.app.win_camera.subprocess.run", return_value=_fake_completed(out)):
            msys.platform = "win32"
            details = win_camera._pnp_camera_details()
        # Only the real USB camera survives; the printer/SCSI scanners are gone.
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]["name"], "Innomaker-U20CAM-720P")
        self.assertEqual(details[0]["location"], "Port_#0003.Hub_#0001")

    def test_non_windows_returns_empty(self):
        with patch("gui.app.win_camera.sys") as msys:
            msys.platform = "linux"
            self.assertEqual(win_camera._pnp_camera_details(), [])

    def test_powershell_failure_returns_empty(self):
        with patch("gui.app.win_camera.sys") as msys, \
             patch("gui.app.win_camera.subprocess.run", return_value=_fake_completed("", 1)):
            msys.platform = "win32"
            self.assertEqual(win_camera._pnp_camera_details(), [])


class TestCv2Guard(unittest.TestCase):

    def test_import_cv2_raises_german_when_missing(self):
        import builtins
        real_import = builtins.__import__

        def _fake_import(name, *a, **k):
            if name == "cv2":
                raise ImportError("no cv2")
            return real_import(name, *a, **k)

        with patch("builtins.__import__", side_effect=_fake_import):
            with self.assertRaises(win_camera.CameraUnavailableError) as ctx:
                win_camera._import_cv2()
        # German, user-facing (Rule §1).
        self.assertIn("Kamera", str(ctx.exception))


class _FakeFrame:
    """Only `.shape` is read by the probe — no numpy needed."""

    def __init__(self, w, h):
        self.shape = (h, w, 3)


class _FakeCap:
    def __init__(self, opened=True, frames=(), clock=None, dt=1 / 30.0):
        self.opened = opened
        self.sets = []
        self.released = 0
        self._frames = list(frames)
        self._clock = clock
        self._dt = dt

    def isOpened(self):  # noqa: N802 — OpenCV API
        return self.opened

    def set(self, prop, value):
        self.sets.append((prop, value))
        return True

    def get(self, prop):
        return 0

    def read(self):
        if self._clock is not None:
            self._clock.t += self._dt
        if not self._frames:
            return False, None
        return True, self._frames.pop(0)

    def release(self):
        self.released += 1


def _fake_cv2(cap):
    calls = []

    def _vc(index, backend):
        calls.append((index, backend))
        return cap
    cv2 = types.SimpleNamespace(
        VideoCapture=_vc, CAP_MSMF=1400, CAP_DSHOW=700,
        CAP_PROP_FOURCC=6, CAP_PROP_FRAME_WIDTH=3, CAP_PROP_FRAME_HEIGHT=4,
        CAP_PROP_FPS=5, CAP_PROP_BUFFERSIZE=38,
        VideoWriter_fourcc=lambda *c: "".join(c))
    return cv2, calls


class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class TestCaptureBackendName(unittest.TestCase):

    def test_msmf_unless_the_dshow_rollback_is_set(self):
        for value, expected in ((None, "msmf"), ("", "msmf"), ("msmf", "msmf"),
                                ("DShow ", "dshow"), ("dshow", "dshow"),
                                ("other", "msmf")):
            env = {} if value is None else {"EDUBOTICS_CAMERA_BACKEND": value}
            with self.subTest(value=value), patch.dict(os.environ, env, clear=False):
                if value is None:
                    os.environ.pop("EDUBOTICS_CAMERA_BACKEND", None)
                self.assertEqual(win_camera.capture_backend_name(), expected)

    def test_capture_backend_reads_the_same_rule(self):
        cv2, _ = _fake_cv2(_FakeCap())
        with patch.dict(os.environ, {"EDUBOTICS_CAMERA_BACKEND": "dshow"}):
            self.assertEqual(win_camera._capture_backend(cv2), cv2.CAP_DSHOW)
        with patch.dict(os.environ, {"EDUBOTICS_CAMERA_BACKEND": "msmf"}):
            self.assertEqual(win_camera._capture_backend(cv2), cv2.CAP_MSMF)


class TestOpenStationCapture(unittest.TestCase):

    def _open(self, method, cap):
        cv2, calls = _fake_cv2(cap)
        with patch.object(win_camera, "_import_cv2", return_value=cv2), \
                patch.object(constants, "STATION_OPEN_METHOD", method), \
                patch.dict(os.environ, {"EDUBOTICS_CAMERA_BACKEND": "msmf"}):
            out = win_camera.open_station_capture(3)
        return out, cv2, calls

    def test_fourcc_only_sets_no_size_and_no_fps(self):
        cap = _FakeCap()
        (got, info), cv2, calls = self._open("fourcc_only", cap)
        self.assertIs(got, cap)
        self.assertEqual(calls, [(3, cv2.CAP_MSMF)])
        props = [p for p, _v in cap.sets]
        self.assertIn((cv2.CAP_PROP_FOURCC, "MJPG"), cap.sets)
        for forbidden in (cv2.CAP_PROP_FRAME_WIDTH, cv2.CAP_PROP_FRAME_HEIGHT,
                          cv2.CAP_PROP_FPS):
            self.assertNotIn(forbidden, props)
        self.assertTrue(info["msmf"])

    def test_fourcc_size_fps_requests_the_native_mode(self):
        cap = _FakeCap()
        (_got, _info), cv2, _calls = self._open("fourcc_size_fps", cap)
        self.assertIn((cv2.CAP_PROP_FOURCC, "MJPG"), cap.sets)
        self.assertIn((cv2.CAP_PROP_FRAME_WIDTH, 1920), cap.sets)
        self.assertIn((cv2.CAP_PROP_FRAME_HEIGHT, 1080), cap.sets)
        self.assertIn((cv2.CAP_PROP_FPS, 30), cap.sets)

    def test_an_unknown_method_is_refused_in_german(self):
        with self.assertRaises(win_camera.CameraUnavailableError) as ctx:
            self._open("fourcc_and_hope", _FakeCap())
        self.assertIn("Stationskamera", str(ctx.exception))

    def test_a_device_that_does_not_open_is_released_and_refused(self):
        cap = _FakeCap(opened=False)
        with self.assertRaises(win_camera.CameraUnavailableError) as ctx:
            self._open("fourcc_only", cap)
        self.assertEqual(cap.released, 1)
        self.assertIn("nicht geöffnet", str(ctx.exception))

    def test_the_shipped_method_is_one_of_the_two(self):
        self.assertIn(constants.STATION_OPEN_METHOD,
                      ("fourcc_only", "fourcc_size_fps"))
        self.assertEqual(constants.STATION_OPEN_METHODS,
                         ("fourcc_only", "fourcc_size_fps"))


class TestStationProbe(unittest.TestCase):

    def _probe(self, frames, dt=1 / 30.0, opened=True, **kw):
        clock = _Clock()
        cap = _FakeCap(opened=opened, frames=frames, clock=clock, dt=dt)
        cv2, _ = _fake_cv2(cap)
        with patch.object(win_camera, "_import_cv2", return_value=cv2):
            result = win_camera.probe_station_camera(0, clock=clock, **kw)
        return result, cap

    def test_sixty_native_frames_at_thirty_fps_pass(self):
        result, cap = self._probe([_FakeFrame(1920, 1080)] * 60)
        self.assertTrue(result.ok, result.error_de)
        self.assertEqual(result.frames, 60)
        self.assertAlmostEqual(result.fps, 30.0, places=3)
        self.assertEqual(result.error_de, "")
        self.assertEqual(cap.released, 1)

    def test_a_720p_camera_is_refused_naming_its_size(self):
        result, cap = self._probe([_FakeFrame(1280, 720)] * 60)
        self.assertFalse(result.ok)
        self.assertIn("1280×720", result.error_de)
        self.assertIn("1920×1080", result.error_de)
        self.assertEqual(cap.released, 1)

    def test_a_640x480_default_mode_is_refused(self):
        result, _cap = self._probe([_FakeFrame(640, 480)] * 60)
        self.assertFalse(result.ok)
        self.assertIn("640×480", result.error_de)

    def test_too_slow_is_refused(self):
        result, _cap = self._probe([_FakeFrame(1920, 1080)] * 60, dt=1 / 20.0)
        self.assertFalse(result.ok)
        self.assertLess(result.fps, 29.0)
        self.assertIn("Bildern pro Sekunde", result.error_de)

    def test_just_above_the_floor_passes(self):
        result, _cap = self._probe([_FakeFrame(1920, 1080)] * 60, dt=1 / 29.5)
        self.assertTrue(result.ok, result.error_de)

    def test_too_few_frames_before_the_timeout_is_refused(self):
        result, cap = self._probe([_FakeFrame(1920, 1080)] * 10, timeout_s=1.0)
        self.assertFalse(result.ok)
        self.assertIn("10 von 60", result.error_de)
        self.assertEqual(cap.released, 1)

    def test_a_camera_that_does_not_open_is_a_refusal_not_a_raise(self):
        result, _cap = self._probe([], opened=False)
        self.assertFalse(result.ok)
        self.assertIn("nicht geöffnet", result.error_de)

    def test_the_probe_thresholds_are_the_plan_s(self):
        self.assertEqual(constants.STATION_PROBE_FRAMES, 60)
        self.assertEqual(constants.STATION_PROBE_MIN_FPS, 29.0)
        self.assertEqual((constants.STATION_NATIVE_WIDTH,
                          constants.STATION_NATIVE_HEIGHT), (1920, 1080))


if __name__ == "__main__":
    unittest.main()

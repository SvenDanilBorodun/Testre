"""Tests for win_camera.py (native Windows camera enumeration).

Platform-agnostic: cv2 is imported lazily, and the PnP query is mocked, so
these run on Linux CI and a Windows workstation alike.
"""

import unittest
from unittest.mock import patch

from gui.app import win_camera


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


if __name__ == "__main__":
    unittest.main()

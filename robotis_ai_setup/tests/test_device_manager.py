"""Tests for device_manager module (unit tests — no real hardware needed).

Windows-only: the module under test calls usbipd.exe and parses its
output. Import works on Linux, but behaviour diverges. Skip on non-Windows
CI rather than let import-time side effects or flaky assertions fail the
whole suite; the maintainer runs these locally on the Windows dev box.
"""

import sys
import unittest

if sys.platform != "win32":
    raise unittest.SkipTest("Windows-only tests; skipped on non-Windows CI.")

from unittest.mock import patch, MagicMock

from gui.app.device_manager import (
    USBDevice,
    ArmDevice,
    CameraDevice,
    HardwareConfig,
    list_robotis_devices,
    list_unbound_robotis,
    hardware_ids_for_devices,
)


class TestUSBDevice(unittest.TestCase):

    def test_usb_device_creation(self):
        dev = USBDevice(
            busid="1-3",
            vid_pid="2F5D:0103",
            description="OpenRB-150",
            state="Not shared",
        )
        self.assertEqual(dev.busid, "1-3")
        self.assertEqual(dev.vid_pid, "2F5D:0103")


class TestHardwareConfig(unittest.TestCase):

    def test_is_complete_requires_both_arms(self):
        cfg = HardwareConfig()
        self.assertFalse(cfg.is_complete)

        cfg.leader = ArmDevice("1-3", "/dev/ttyACM0", "leader", "test")
        self.assertFalse(cfg.is_complete)

        cfg.follower = ArmDevice("1-4", "/dev/ttyACM1", "follower", "test")
        self.assertTrue(cfg.is_complete)

    def test_cameras_are_optional(self):
        cfg = HardwareConfig(
            leader=ArmDevice("1-3", "/dev/ttyACM0", "leader", "test"),
            follower=ArmDevice("1-4", "/dev/ttyACM1", "follower", "test"),
        )
        self.assertTrue(cfg.is_complete)
        self.assertEqual(cfg.cameras, [])


class TestListRobotisDevices(unittest.TestCase):

    @patch("gui.app.device_manager.list_usb_devices")
    def test_filters_by_vid(self, mock_list):
        mock_list.return_value = [
            USBDevice("1-1", "2F5D:0103", "OpenRB-150", "Not shared"),
            USBDevice("1-2", "046D:0825", "Logitech Webcam", "Not shared"),
            USBDevice("1-3", "2F5D:0104", "OpenRB-150", "Shared"),
        ]
        result = list_robotis_devices()
        self.assertEqual(len(result), 2)
        self.assertTrue(all(d.vid_pid.startswith("2F5D") for d in result))


class TestListUnboundRobotis(unittest.TestCase):

    @patch("gui.app.device_manager.list_usb_devices")
    def test_only_not_shared_or_unknown_robotis(self, mock_list):
        mock_list.return_value = [
            USBDevice("1-1", "2F5D:0103", "OpenRB-150", "Not shared"),  # unbound
            USBDevice("1-2", "2F5D:0104", "OpenRB-150", "Shared"),      # already bound
            USBDevice("1-3", "2F5D:2202", "OpenRB-150", "Attached"),    # already attached
            USBDevice("1-4", "2F5D:0105", "OpenRB-150", "Unknown"),     # relaxed-parse -> unbound
            USBDevice("1-5", "046D:0825", "Logitech Webcam", "Not shared"),  # not ROBOTIS
        ]
        result = list_unbound_robotis()
        busids = sorted(d.busid for d in result)
        self.assertEqual(busids, ["1-1", "1-4"])

    @patch("gui.app.device_manager.list_usb_devices")
    def test_hardware_ids_dedup_lowercased(self, mock_list):
        mock_list.return_value = [
            USBDevice("1-1", "2F5D:0103", "OpenRB-150", "Not shared"),
            USBDevice("1-2", "2F5D:0103", "OpenRB-150", "Not shared"),  # same VID:PID
        ]
        ids = hardware_ids_for_devices(list_unbound_robotis())
        self.assertEqual(ids, ["2f5d:0103"])


if __name__ == "__main__":
    unittest.main()

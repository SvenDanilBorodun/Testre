"""Tests for device_manager module (unit tests — no real hardware needed)."""

import unittest
from unittest.mock import patch, MagicMock

from gui.app.device_manager import (
    USBDevice,
    ArmDevice,
    CameraDevice,
    HardwareConfig,
    list_robotis_devices,
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

    def test_camera_is_optional(self):
        cfg = HardwareConfig(
            leader=ArmDevice("1-3", "/dev/ttyACM0", "leader", "test"),
            follower=ArmDevice("1-4", "/dev/ttyACM1", "follower", "test"),
        )
        self.assertTrue(cfg.is_complete)
        self.assertIsNone(cfg.camera)


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


if __name__ == "__main__":
    unittest.main()

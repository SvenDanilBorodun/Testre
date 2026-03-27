"""Tests for config_generator module."""

import os
import tempfile
import unittest

from gui.app.config_generator import generate_env_file
from gui.app.device_manager import ArmDevice, CameraDevice, HardwareConfig


class TestConfigGenerator(unittest.TestCase):

    def test_generate_env_with_camera(self):
        config = HardwareConfig(
            leader=ArmDevice(
                busid="1-3",
                serial_path="/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_Leader123",
                role="leader",
                description="OpenRB-150",
            ),
            follower=ArmDevice(
                busid="1-4",
                serial_path="/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_Follower456",
                role="follower",
                description="OpenRB-150",
            ),
            camera=CameraDevice(path="/dev/video0", name="Logitech C920"),
        )

        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            tmp_path = f.name

        try:
            content = generate_env_file(config, output_path=tmp_path)
            self.assertIn("FOLLOWER_PORT=/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_Follower456", content)
            self.assertIn("LEADER_PORT=/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_Leader123", content)
            self.assertIn("CAMERA_DEVICE=/dev/video0", content)
            self.assertIn("CAMERA_NAME=camera1", content)
            self.assertIn("ROS_DOMAIN_ID=30", content)

            # Verify file was written
            with open(tmp_path) as f:
                file_content = f.read()
            self.assertEqual(content, file_content)
        finally:
            os.unlink(tmp_path)

    def test_generate_env_without_camera(self):
        config = HardwareConfig(
            leader=ArmDevice(
                busid="1-3",
                serial_path="/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_Leader123",
                role="leader",
                description="OpenRB-150",
            ),
            follower=ArmDevice(
                busid="1-4",
                serial_path="/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_Follower456",
                role="follower",
                description="OpenRB-150",
            ),
            camera=None,
        )

        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            tmp_path = f.name

        try:
            content = generate_env_file(config, output_path=tmp_path)
            # Should default to /dev/video0
            self.assertIn("CAMERA_DEVICE=/dev/video0", content)
        finally:
            os.unlink(tmp_path)

    def test_hardware_config_is_complete(self):
        config = HardwareConfig()
        self.assertFalse(config.is_complete)

        config.leader = ArmDevice("1-3", "/dev/ttyACM0", "leader", "test")
        self.assertFalse(config.is_complete)

        config.follower = ArmDevice("1-4", "/dev/ttyACM1", "follower", "test")
        self.assertTrue(config.is_complete)


if __name__ == "__main__":
    unittest.main()

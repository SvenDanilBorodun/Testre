"""Tests for config_generator module."""

import os
import tempfile
import unittest

from gui.app.config_generator import generate_env_file
from gui.app.device_manager import ArmDevice, CameraDevice, HardwareConfig


class TestConfigGenerator(unittest.TestCase):

    def test_generate_env_with_cameras(self):
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
            cameras=[
                CameraDevice(path="/dev/video0", name="Gripper Cam"),
                CameraDevice(path="/dev/video2", name="Scene Cam"),
            ],
        )

        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            tmp_path = f.name

        try:
            content = generate_env_file(config, output_path=tmp_path)
            self.assertIn("FOLLOWER_PORT=/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_Follower456", content)
            self.assertIn("LEADER_PORT=/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_Leader123", content)
            self.assertIn("CAMERA_DEVICE_1=/dev/video0", content)
            self.assertIn("CAMERA_NAME_1=camera1", content)
            self.assertIn("CAMERA_DEVICE_2=/dev/video2", content)
            self.assertIn("CAMERA_NAME_2=camera2", content)
            self.assertIn("ROS_DOMAIN_ID=30", content)

            with open(tmp_path) as f:
                file_content = f.read()
            self.assertEqual(content, file_content)
        finally:
            os.unlink(tmp_path)

    def test_generate_env_without_cameras(self):
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
        )

        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            tmp_path = f.name

        try:
            content = generate_env_file(config, output_path=tmp_path)
            # No camera vars should be present
            self.assertNotIn("CAMERA_DEVICE", content)
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

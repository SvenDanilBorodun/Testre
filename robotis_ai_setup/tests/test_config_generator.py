"""Tests for config_generator module."""

import os
import tempfile
import unittest

from gui.app.config_generator import generate_env_file, generate_cloud_only_env
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
                CameraDevice(path="/dev/video0", name="Gripper Cam", role="gripper"),
                CameraDevice(path="/dev/video2", name="Scene Cam", role="scene"),
            ],
        )

        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            tmp_path = f.name

        try:
            content = generate_env_file(config, output_path=tmp_path)
            # Values are now double-quoted so compose handles paths with
            # spaces (e.g. "/mnt/c/Users/Max Muster/...").
            self.assertIn('FOLLOWER_PORT="/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_Follower456"', content)
            self.assertIn('LEADER_PORT="/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_Leader123"', content)
            self.assertIn('CAMERA_DEVICE_1="/dev/video0"', content)
            self.assertIn('CAMERA_NAME_1="gripper"', content)
            self.assertIn('CAMERA_DEVICE_2="/dev/video2"', content)
            self.assertIn('CAMERA_NAME_2="scene"', content)
            # ROS_DOMAIN_ID is now machine-derived, not a hardcoded 30 —
            # just verify the line is present and is a legal DDS domain.
            import re
            m = re.search(r'ROS_DOMAIN_ID=(\d+)', content)
            self.assertIsNotNone(m, "ROS_DOMAIN_ID line missing")
            self.assertTrue(0 <= int(m.group(1)) <= 232)

            with open(tmp_path) as f:
                file_content = f.read()
            self.assertEqual(content, file_content)
        finally:
            os.unlink(tmp_path)

    def test_domain_id_override(self):
        """EDUBOTICS_ROS_DOMAIN env var pins a specific domain id."""
        import os as _os
        prev = _os.environ.get('EDUBOTICS_ROS_DOMAIN')
        try:
            _os.environ['EDUBOTICS_ROS_DOMAIN'] = '42'
            with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
                tmp_path = f.name
            try:
                content = generate_cloud_only_env(output_path=tmp_path)
                self.assertIn('ROS_DOMAIN_ID=42', content)
            finally:
                _os.unlink(tmp_path)
        finally:
            if prev is None:
                _os.environ.pop('EDUBOTICS_ROS_DOMAIN', None)
            else:
                _os.environ['EDUBOTICS_ROS_DOMAIN'] = prev

    def test_paths_with_spaces_are_quoted(self):
        """Paths with spaces must survive docker-compose env parsing."""
        config = HardwareConfig(
            leader=ArmDevice(
                busid="1-3",
                serial_path="/mnt/c/Users/Max Muster/leader",
                role="leader",
                description="OpenRB-150",
            ),
            follower=ArmDevice(
                busid="1-4",
                serial_path="/mnt/c/Users/Max Muster/follower",
                role="follower",
                description="OpenRB-150",
            ),
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            tmp_path = f.name
        try:
            content = generate_env_file(config, output_path=tmp_path)
            self.assertIn('FOLLOWER_PORT="/mnt/c/Users/Max Muster/follower"', content)
            self.assertIn('LEADER_PORT="/mnt/c/Users/Max Muster/leader"', content)
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

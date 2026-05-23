"""Tests for config_generator module."""

import os
import tempfile
import unittest

from gui.app.config_generator import generate_env_file, generate_cloud_only_env
from gui.app.device_manager import ArmDevice, CameraDevice, HardwareConfig


class TestConfigGenerator(unittest.TestCase):

    def setUp(self):
        # These tests assert the usb_cam .env layout (CAMERA_DEVICE carries the
        # /dev/video path). Pin the camera source so the result is identical on
        # Linux CI and a Windows workstation (where native_bridge is the
        # default and would empty CAMERA_DEVICE). The native_bridge layout is
        # covered by TestConfigGeneratorNativeBridge below.
        self._prev_src = os.environ.get("EDUBOTICS_CAMERA_SOURCE")
        os.environ["EDUBOTICS_CAMERA_SOURCE"] = "usb_cam"

    def tearDown(self):
        if self._prev_src is None:
            os.environ.pop("EDUBOTICS_CAMERA_SOURCE", None)
        else:
            os.environ["EDUBOTICS_CAMERA_SOURCE"] = self._prev_src

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


class TestConfigGeneratorNativeBridge(unittest.TestCase):
    """native_bridge mode: cameras captured on Windows, container CAMERA_DEVICE
    stays empty, EDUBOTICS_CAMERA_SOURCE=native_bridge is emitted."""

    def setUp(self):
        self._prev_src = os.environ.get("EDUBOTICS_CAMERA_SOURCE")
        os.environ["EDUBOTICS_CAMERA_SOURCE"] = "native_bridge"

    def tearDown(self):
        if self._prev_src is None:
            os.environ.pop("EDUBOTICS_CAMERA_SOURCE", None)
        else:
            os.environ["EDUBOTICS_CAMERA_SOURCE"] = self._prev_src

    def _config(self):
        return HardwareConfig(
            leader=ArmDevice("1-3", "/dev/serial/by-id/leader", "leader", "OpenRB-150"),
            follower=ArmDevice("1-4", "/dev/serial/by-id/follower", "follower", "OpenRB-150"),
            cameras=[
                CameraDevice(path="Index 0", name="Gripper Cam", role="gripper", win_index=0),
                CameraDevice(path="Index 1", name="Scene Cam", role="scene", win_index=1),
            ],
        )

    def test_native_bridge_empties_camera_device_and_sets_source(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            tmp_path = f.name
        try:
            content = generate_env_file(self._config(), output_path=tmp_path)
            # Container does not capture from /dev/video* — device stays empty.
            self.assertIn('CAMERA_DEVICE_1=""', content)
            self.assertIn('CAMERA_DEVICE_2=""', content)
            # Roles still drive the published topic names.
            self.assertIn('CAMERA_NAME_1="gripper"', content)
            self.assertIn('CAMERA_NAME_2="scene"', content)
            # Source emitted so the container/healthcheck branch correctly.
            self.assertIn("EDUBOTICS_CAMERA_SOURCE=native_bridge", content)
        finally:
            os.unlink(tmp_path)

    def test_operator_usb_cam_override_is_preserved(self):
        """A hand-edited EDUBOTICS_CAMERA_SOURCE=usb_cam must survive regen
        (one-variable rollback) and we must not duplicate the key."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            tmp_path = f.name
            f.write("EDUBOTICS_CAMERA_SOURCE=usb_cam\n")
        try:
            content = generate_env_file(self._config(), output_path=tmp_path)
            self.assertEqual(content.count("EDUBOTICS_CAMERA_SOURCE="), 1)
            self.assertIn("EDUBOTICS_CAMERA_SOURCE=usb_cam", content)
            self.assertNotIn("EDUBOTICS_CAMERA_SOURCE=native_bridge", content)
        finally:
            os.unlink(tmp_path)


if __name__ == "__main__":
    unittest.main()

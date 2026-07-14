"""Deps-free unit tests for pi_agent.config_generator.

Cross-platform: no docker daemon, no root, no /etc or /var writes (every
generate_* call is pointed at a tempfile, and EDUBOTICS_ROS_DOMAIN is pinned
so ROS_DOMAIN_ID resolution never touches the persist/machine-id files unless
a test explicitly redirects them).

Mirrors robotis_ai_setup/tests/test_config_generator.py (the GUI suite) +
the Jetson tests' import convention (add the package parent to sys.path, then
`from pi_agent import …`).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Make `pi_agent` importable as a package (its modules use relative imports).
SETUP_DIR = Path(__file__).resolve().parents[2]  # robotis_ai_setup/
sys.path.insert(0, str(SETUP_DIR))

from pi_agent import constants  # noqa: E402
from pi_agent import config_generator as cg  # noqa: E402
from pi_agent.config_generator import (  # noqa: E402
    ArmDevice,
    CameraDevice,
    HardwareConfig,
    generate_cloud_only_env,
    generate_env_file,
    read_env_var,
    upsert_env_var,
)


_DEFAULT_CAMS = object()  # sentinel so an explicit [] (no cameras) is honoured


def _both_arms(cameras=_DEFAULT_CAMS):
    if cameras is _DEFAULT_CAMS:
        cameras = [
            CameraDevice(path="/dev/video0", role="gripper"),
            CameraDevice(path="/dev/video2", role="scene"),
        ]
    return HardwareConfig(
        leader=ArmDevice(serial_path="/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_LEADER"),
        follower=ArmDevice(serial_path="/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_FOLLOWER"),
        cameras=cameras,
    )


class _TmpEnvBase(unittest.TestCase):
    def setUp(self):
        # Pin the ROS domain so resolution returns immediately without touching
        # /var/lib (persist) or /etc/machine-id.
        self._prev_domain = os.environ.get("EDUBOTICS_ROS_DOMAIN")
        os.environ["EDUBOTICS_ROS_DOMAIN"] = "42"
        fd, self.path = tempfile.mkstemp(suffix=".env")
        os.close(fd)
        os.unlink(self.path)  # start with no file so first write is a create

    def tearDown(self):
        if self._prev_domain is None:
            os.environ.pop("EDUBOTICS_ROS_DOMAIN", None)
        else:
            os.environ["EDUBOTICS_ROS_DOMAIN"] = self._prev_domain
        for p in (self.path, self.path + ".tmp"):
            try:
                os.unlink(p)
            except OSError:
                pass


class TestGenerateEnvBothArms(_TmpEnvBase):
    def test_both_arms_layout(self):
        content = generate_env_file(_both_arms(), self.path)
        # Ports quoted so compose handles a space in a by-id path.
        self.assertIn(
            'FOLLOWER_PORT="/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_FOLLOWER"', content)
        self.assertIn(
            'LEADER_PORT="/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_LEADER"', content)
        # Both-arms emits FOLLOWER_ONLY=0 EXPLICITLY (unlike the GUI).
        self.assertIn("EDUBOTICS_FOLLOWER_ONLY=0", content)
        # usb_cam path: CAMERA_DEVICE carries the /dev/video path.
        self.assertIn('CAMERA_DEVICE_1="/dev/video0"', content)
        self.assertIn('CAMERA_NAME_1="gripper"', content)
        self.assertIn('CAMERA_DEVICE_2="/dev/video2"', content)
        self.assertIn('CAMERA_NAME_2="scene"', content)
        self.assertIn("ROS_DOMAIN_ID=42", content)
        self.assertIn(f"REGISTRY={constants.REGISTRY}", content)
        self.assertIn(f"REGISTRY_FALLBACK={constants.REGISTRY_FALLBACK}", content)
        self.assertIn(f"IMAGE_TAG={constants.IMAGE_TAG}", content)
        # The Pi is ALWAYS usb_cam, emitted explicitly.
        self.assertIn("EDUBOTICS_CAMERA_SOURCE=usb_cam", content)

    def test_lan_open_default_open(self):
        content = generate_env_file(_both_arms(), self.path)
        self.assertIn("EDUBOTICS_LAN_OPEN=1", content)
        self.assertIn("EDUBOTICS_BIND_HOST=0.0.0.0", content)

    def test_lan_open_false_binds_loopback(self):
        content = generate_env_file(_both_arms(), self.path, lan_open=False)
        self.assertIn("EDUBOTICS_LAN_OPEN=0", content)
        self.assertIn("EDUBOTICS_BIND_HOST=127.0.0.1", content)

    def test_default_ros_net_subnet_and_gateway(self):
        content = generate_env_file(_both_arms(), self.path)
        self.assertIn("EDUBOTICS_ROS_NET_SUBNET=172.28.0.0/24", content)
        # Gateway is DERIVED (first host of the subnet).
        self.assertIn("EDUBOTICS_ROS_NET_GATEWAY=172.28.0.1", content)

    def test_subnet_override_derives_gateway(self):
        content = generate_env_file(_both_arms(), self.path, ros_net_subnet="10.99.0.0/24")
        self.assertIn("EDUBOTICS_ROS_NET_SUBNET=10.99.0.0/24", content)
        self.assertIn("EDUBOTICS_ROS_NET_GATEWAY=10.99.0.1", content)

    def test_invalid_subnet_falls_back_to_default(self):
        content = generate_env_file(_both_arms(), self.path, ros_net_subnet="not-a-subnet")
        self.assertIn(f"EDUBOTICS_ROS_NET_SUBNET={constants.DEFAULT_ROS_NET_SUBNET}", content)

    def test_missing_leader_raises_german(self):
        cfg = _both_arms()
        cfg.leader = None
        with self.assertRaises(ValueError) as ctx:
            generate_env_file(cfg, self.path)
        self.assertIn("Leader-Arm", str(ctx.exception))

    def test_missing_follower_raises_german(self):
        cfg = _both_arms()
        cfg.follower = None
        with self.assertRaises(ValueError) as ctx:
            generate_env_file(cfg, self.path)
        self.assertIn("Follower-Arm", str(ctx.exception))

    def test_invalid_camera_role_raises_german(self):
        cfg = _both_arms(cameras=[CameraDevice(path="/dev/video0", role="camera1")])
        with self.assertRaises(ValueError) as ctx:
            generate_env_file(cfg, self.path)
        self.assertIn("Kamera ohne gültige Rolle", str(ctx.exception))

    def test_no_cameras_ok(self):
        cfg = _both_arms(cameras=[])
        content = generate_env_file(cfg, self.path)
        self.assertNotIn("CAMERA_DEVICE_1", content)

    def test_no_tmp_file_left_behind(self):
        generate_env_file(_both_arms(), self.path)
        self.assertFalse(os.path.exists(self.path + ".tmp"))


class TestGenerateEnvFollowerOnly(_TmpEnvBase):
    def test_follower_only_omits_leader(self):
        content = generate_env_file(_both_arms(), self.path, follower_only=True)
        self.assertIn("EDUBOTICS_FOLLOWER_ONLY=1", content)
        self.assertNotIn("LEADER_PORT=", content)

    def test_follower_only_allows_missing_leader(self):
        cfg = _both_arms()
        cfg.leader = None
        # Follower-only mode does NOT require a leader.
        content = generate_env_file(cfg, self.path, follower_only=True)
        self.assertIn("EDUBOTICS_FOLLOWER_ONLY=1", content)


class TestStickyAndPreserved(_TmpEnvBase):
    def test_hf_token_survives_regenerate(self):
        generate_env_file(_both_arms(), self.path)
        upsert_env_var("HF_TOKEN", "hf_secret123", self.path)
        # A hardware re-scan regenerate must preserve HF_TOKEN (UNMANAGED).
        generate_env_file(_both_arms(), self.path)
        self.assertEqual(read_env_var("HF_TOKEN", self.path), "hf_secret123")

    def test_operator_override_preserved(self):
        generate_env_file(_both_arms(), self.path)
        with open(self.path, "a") as fh:
            fh.write("EDUBOTICS_CAMERA_PIXEL_FORMAT=mjpeg2rgb\n")
        content = generate_env_file(_both_arms(), self.path)
        self.assertIn("EDUBOTICS_CAMERA_PIXEL_FORMAT=mjpeg2rgb", content)
        # The preserve marker must appear exactly once (no compounding).
        self.assertEqual(content.count(cg._PRESERVE_MARKER), 1)

    def test_lan_open_is_sticky(self):
        generate_env_file(_both_arms(), self.path, lan_open=False)
        # A regenerate with lan_open=None must carry the kiosk toggle forward.
        content = generate_env_file(_both_arms(), self.path)
        self.assertIn("EDUBOTICS_LAN_OPEN=0", content)
        self.assertIn("EDUBOTICS_BIND_HOST=127.0.0.1", content)

    def test_subnet_is_sticky(self):
        generate_env_file(_both_arms(), self.path, ros_net_subnet="10.42.0.0/24")
        content = generate_env_file(_both_arms(), self.path)  # None → carry forward
        self.assertIn("EDUBOTICS_ROS_NET_SUBNET=10.42.0.0/24", content)
        self.assertIn("EDUBOTICS_ROS_NET_GATEWAY=10.42.0.1", content)

    def test_stale_managed_value_superseded(self):
        # A copied .env carrying a stale native_bridge source + a stale leader
        # port must be SUPERSEDED (managed keys are dropped on re-read).
        with open(self.path, "w") as fh:
            fh.write(
                "EDUBOTICS_CAMERA_SOURCE=native_bridge\n"
                "LEADER_PORT=\"/dev/stale\"\n"
                "REGISTRY=stale/registry\n"
            )
        content = generate_env_file(_both_arms(), self.path, follower_only=True)
        self.assertIn("EDUBOTICS_CAMERA_SOURCE=usb_cam", content)
        self.assertNotIn("native_bridge", content)
        self.assertNotIn("/dev/stale", content)
        self.assertNotIn("stale/registry", content)
        self.assertIn(f"REGISTRY={constants.REGISTRY}", content)


class TestUpsertAndRead(_TmpEnvBase):
    def test_upsert_insert_then_replace_then_remove(self):
        upsert_env_var("HF_TOKEN", "first", self.path)
        self.assertEqual(read_env_var("HF_TOKEN", self.path), "first")
        upsert_env_var("HF_TOKEN", "second", self.path)
        self.assertEqual(read_env_var("HF_TOKEN", self.path), "second")
        # Empty value removes the key.
        upsert_env_var("HF_TOKEN", "", self.path)
        self.assertIsNone(read_env_var("HF_TOKEN", self.path))

    def test_read_env_var_missing_file(self):
        self.assertIsNone(read_env_var("HF_TOKEN", self.path + ".does-not-exist"))

    def test_read_env_var_unquotes(self):
        upsert_env_var("MY_KEY", 'val "with" quotes', self.path)
        self.assertEqual(read_env_var("MY_KEY", self.path), 'val "with" quotes')


class TestCloudOnlyEnv(_TmpEnvBase):
    def test_cloud_only_layout(self):
        content = generate_cloud_only_env(self.path)
        self.assertIn('FOLLOWER_PORT=""', content)
        self.assertIn('LEADER_PORT=""', content)
        self.assertIn("EDUBOTICS_FOLLOWER_ONLY=0", content)
        # Manager-facing network keys must be present so the always-on manager
        # can render its /api/system proxy target.
        self.assertIn("EDUBOTICS_ROS_NET_GATEWAY=172.28.0.1", content)
        self.assertIn("EDUBOTICS_BIND_HOST=0.0.0.0", content)
        self.assertIn(f"REGISTRY={constants.REGISTRY}", content)
        self.assertIn("EDUBOTICS_CAMERA_SOURCE=usb_cam", content)


class TestGatewayDerivation(unittest.TestCase):
    def test_derive_gateway(self):
        self.assertEqual(cg._derive_gateway("172.28.0.0/24"), "172.28.0.1")
        self.assertEqual(cg._derive_gateway("10.0.0.0/16"), "10.0.0.1")
        self.assertEqual(cg._derive_gateway("192.168.50.0/24"), "192.168.50.1")

    def test_derive_gateway_bad_input(self):
        self.assertEqual(cg._derive_gateway("garbage"), constants.DEFAULT_ROS_NET_GATEWAY)

    def test_valid_subnet(self):
        self.assertEqual(cg._valid_subnet("10.1.0.0/24"), "10.1.0.0/24")
        self.assertIsNone(cg._valid_subnet("nope"))
        self.assertIsNone(cg._valid_subnet(None))


class TestRosDomainDerivation(unittest.TestCase):
    def setUp(self):
        self._prev_domain = os.environ.pop("EDUBOTICS_ROS_DOMAIN", None)
        self._prev_mid = cg.MACHINE_ID_FILE
        self._prev_dom_file = cg.ROS_DOMAIN_FILE
        self._mid = tempfile.NamedTemporaryFile(mode="w", suffix=".mid", delete=False)
        self._mid.write("abc123def456abc123def456abc12345\n")
        self._mid.close()
        fd, self._dom_file = tempfile.mkstemp(suffix=".rosdom")
        os.close(fd)
        os.unlink(self._dom_file)
        cg.MACHINE_ID_FILE = self._mid.name
        cg.ROS_DOMAIN_FILE = self._dom_file

    def tearDown(self):
        cg.MACHINE_ID_FILE = self._prev_mid
        cg.ROS_DOMAIN_FILE = self._prev_dom_file
        if self._prev_domain is not None:
            os.environ["EDUBOTICS_ROS_DOMAIN"] = self._prev_domain
        for p in (self._mid.name, self._dom_file):
            try:
                os.unlink(p)
            except OSError:
                pass

    def test_env_override_wins(self):
        os.environ["EDUBOTICS_ROS_DOMAIN"] = "77"
        try:
            self.assertEqual(cg._resolve_ros_domain_id(), 77)
        finally:
            os.environ.pop("EDUBOTICS_ROS_DOMAIN", None)

    def test_derives_from_machine_id_deterministic_and_persists(self):
        first = cg._resolve_ros_domain_id()
        self.assertTrue(0 <= first <= 232)
        # Persisted to the domain file, and a second call reads it back equal.
        self.assertTrue(os.path.exists(self._dom_file))
        second = cg._resolve_ros_domain_id()
        self.assertEqual(first, second)

    def test_persisted_value_read_first(self):
        with open(self._dom_file, "w") as fh:
            fh.write("123\n")
        self.assertEqual(cg._resolve_ros_domain_id(), 123)


if __name__ == "__main__":
    unittest.main()

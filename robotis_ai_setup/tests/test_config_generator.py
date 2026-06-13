"""Tests for config_generator module."""

import os
import tempfile
import unittest

from gui.app.config_generator import (
    generate_env_file,
    generate_cloud_only_env,
    read_env_var,
    upsert_env_var,
)
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


class TestImageTagPinning(unittest.TestCase):
    """IMAGE_TAG is a MANAGED key: emitted from constants.IMAGE_TAG (the
    EDUBOTICS_IMAGE_TAG env > docker/versions.env > latest resolution) and
    superseding any stale hand-pinned operator line — so compose can never
    silently run :latest on a pinned install, nor chase a dead local-only
    tag (the 2026-06-05 collision-validate incident)."""

    def _config(self):
        return HardwareConfig(
            leader=ArmDevice("1-3", "/dev/serial/by-id/leader", "leader", "OpenRB-150"),
            follower=ArmDevice("1-4", "/dev/serial/by-id/follower", "follower", "OpenRB-150"),
        )

    def test_image_tag_emitted_from_constants(self):
        from gui.app.constants import IMAGE_TAG
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            tmp_path = f.name
        try:
            content = generate_env_file(self._config(), output_path=tmp_path)
            self.assertIn(f"IMAGE_TAG={IMAGE_TAG}", content)
            self.assertEqual(content.count("IMAGE_TAG="), 1)
        finally:
            os.unlink(tmp_path)

    def test_cloud_only_emits_image_tag(self):
        from gui.app.constants import IMAGE_TAG
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            tmp_path = f.name
        try:
            content = generate_cloud_only_env(output_path=tmp_path)
            self.assertIn(f"IMAGE_TAG={IMAGE_TAG}", content)
            self.assertEqual(content.count("IMAGE_TAG="), 1)
        finally:
            os.unlink(tmp_path)

    def test_stale_operator_image_tag_is_superseded(self):
        # Regression for 2026-06-05: a validation session left
        # IMAGE_TAG=collision-validate as a hand-edited line. The pre-fix
        # GUI preserved it as an unmanaged operator override forever, and
        # after an installer upgrade wiped the local image, compose chased
        # a tag that existed nowhere -> "manifest unknown" on every start.
        # Managed means regeneration replaces it with the pinned tag.
        from gui.app.constants import IMAGE_TAG
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            tmp_path = f.name
            f.write("IMAGE_TAG=collision-validate\n")
        try:
            content = generate_env_file(self._config(), output_path=tmp_path)
            self.assertNotIn("collision-validate", content)
            self.assertEqual(content.count("IMAGE_TAG="), 1)
            self.assertIn(f"IMAGE_TAG={IMAGE_TAG}", content)
        finally:
            os.unlink(tmp_path)


class TestEnvVarHelpers(unittest.TestCase):
    """read_env_var / upsert_env_var — the GUI's HF_TOKEN persistence path."""

    def _seed(self, body):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False)
        f.write(body)
        f.close()
        self.addCleanup(lambda: os.path.exists(f.name) and os.unlink(f.name))
        return f.name

    @staticmethod
    def _read(p):
        with open(p, encoding="utf-8") as f:
            return f.read()

    def test_insert_and_read_round_trip(self):
        p = self._seed("")
        upsert_env_var("HF_TOKEN", "hf_abc123", p)
        self.assertEqual(read_env_var("HF_TOKEN", p), "hf_abc123")

    def test_other_lines_preserved(self):
        p = self._seed('FOLLOWER_PORT="/dev/ttyUSB0"\n# note\nEDUBOTICS_CAMERA_SOURCE=usb_cam\n')
        upsert_env_var("HF_TOKEN", "hf_abc123", p)
        body = self._read(p)
        self.assertIn('FOLLOWER_PORT="/dev/ttyUSB0"', body)
        self.assertIn("# note", body)
        self.assertIn("EDUBOTICS_CAMERA_SOURCE=usb_cam", body)

    def test_update_does_not_duplicate(self):
        p = self._seed("")
        upsert_env_var("HF_TOKEN", "hf_one", p)
        upsert_env_var("HF_TOKEN", "hf_two", p)
        self.assertEqual(self._read(p).count("HF_TOKEN="), 1)
        self.assertEqual(read_env_var("HF_TOKEN", p), "hf_two")

    def test_empty_value_removes_key(self):
        p = self._seed('FOLLOWER_PORT="/dev/ttyUSB0"\n')
        upsert_env_var("HF_TOKEN", "hf_abc", p)
        upsert_env_var("HF_TOKEN", "", p)
        self.assertIsNone(read_env_var("HF_TOKEN", p))
        self.assertIn("FOLLOWER_PORT", self._read(p))

    def test_read_missing_file_returns_none(self):
        self.assertIsNone(read_env_var("HF_TOKEN", "/no/such/path/.env"))

    def test_value_with_space_round_trips(self):
        p = self._seed("")
        upsert_env_var("HF_TOKEN", "a b", p)
        self.assertEqual(read_env_var("HF_TOKEN", p), "a b")

    def test_token_survives_generate_env_file(self):
        # The real invariant: a token saved by the GUI must persist across a
        # hardware re-scan (generate_env_file rewrite), because HF_TOKEN is an
        # unmanaged key carried through by _read_unmanaged_lines().
        p = self._seed("")
        upsert_env_var("HF_TOKEN", "hf_persist_me", p)
        config = HardwareConfig(
            leader=ArmDevice("1-3", "/dev/serial/by-id/leader", "leader", "OpenRB-150"),
            follower=ArmDevice("1-4", "/dev/serial/by-id/follower", "follower", "OpenRB-150"),
        )
        content = generate_env_file(config, output_path=p)
        self.assertIn("HF_TOKEN=", content)
        self.assertEqual(content.count("HF_TOKEN="), 1)
        self.assertEqual(read_env_var("HF_TOKEN", p), "hf_persist_me")

    def test_repeated_regenerate_does_not_compound(self):
        # A token must survive many hardware re-scans without the .env
        # accumulating duplicate HF_TOKEN lines or "Operator overrides
        # preserved" markers.
        p = self._seed("")
        upsert_env_var("HF_TOKEN", "hf_keepme", p)
        config = HardwareConfig(
            leader=ArmDevice("1-3", "/dev/serial/by-id/leader", "leader", "OpenRB-150"),
            follower=ArmDevice("1-4", "/dev/serial/by-id/follower", "follower", "OpenRB-150"),
        )
        for _ in range(3):
            generate_env_file(config, output_path=p)
        body = self._read(p)
        self.assertEqual(body.count("HF_TOKEN="), 1, body)
        self.assertLessEqual(body.count("Operator overrides preserved"), 1, body)
        self.assertEqual(read_env_var("HF_TOKEN", p), "hf_keepme")


class TestRosDomainPersistence(unittest.TestCase):
    """ROS_DOMAIN_ID must stay STABLE across sessions (uuid.getnode() is not
    reliable on multi-NIC / VPN PCs). The first-resolved value is persisted to
    constants.ROS_DOMAIN_FILE and reused thereafter; an explicit env override
    always wins and is never persisted."""

    def setUp(self):
        from gui.app import config_generator as cg
        self._cg = cg
        self._orig_file = cg.ROS_DOMAIN_FILE
        self._tmpdir = tempfile.mkdtemp()
        # Point the persistence file at a fresh temp path (module-global lookup
        # at call time, so reassigning here is honoured by the helpers).
        cg.ROS_DOMAIN_FILE = os.path.join(self._tmpdir, ".ros_domain_id")
        self._prev_override = os.environ.pop("EDUBOTICS_ROS_DOMAIN", None)

    def tearDown(self):
        self._cg.ROS_DOMAIN_FILE = self._orig_file
        if self._prev_override is not None:
            os.environ["EDUBOTICS_ROS_DOMAIN"] = self._prev_override
        else:
            os.environ.pop("EDUBOTICS_ROS_DOMAIN", None)
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_first_resolve_persists_then_reuses(self):
        # No file yet -> derive + persist.
        first = self._cg._resolve_ros_domain_id()
        self.assertTrue(0 <= first <= 232)
        self.assertTrue(os.path.isfile(self._cg.ROS_DOMAIN_FILE))
        with open(self._cg.ROS_DOMAIN_FILE) as f:
            self.assertEqual(int(f.read().strip()), first)
        # A later run reads the SAME value WITHOUT re-deriving — prove it by
        # making getnode() raise: if it were consulted, we'd hit the fallback.
        import uuid
        orig = uuid.getnode
        uuid.getnode = lambda: (_ for _ in ()).throw(RuntimeError("nic gone"))
        try:
            second = self._cg._resolve_ros_domain_id()
        finally:
            uuid.getnode = orig
        self.assertEqual(second, first)

    def test_env_override_wins_and_is_not_persisted(self):
        os.environ["EDUBOTICS_ROS_DOMAIN"] = "42"
        self.assertEqual(self._cg._resolve_ros_domain_id(), 42)
        # Override must not leave a persisted file behind (it's a per-run knob).
        self.assertFalse(os.path.isfile(self._cg.ROS_DOMAIN_FILE))

    def test_persisted_value_is_used_over_getnode(self):
        with open(self._cg.ROS_DOMAIN_FILE, "w") as f:
            f.write("117\n")
        self.assertEqual(self._cg._resolve_ros_domain_id(), 117)

    def test_corrupt_or_out_of_range_file_falls_back_to_derive(self):
        for bad in ("not-a-number", "999", "-1", ""):
            with open(self._cg.ROS_DOMAIN_FILE, "w") as f:
                f.write(bad)
            self.assertIsNone(self._cg._read_persisted_ros_domain_id())
            # _resolve still returns a legal domain (derives + re-persists).
            resolved = self._cg._resolve_ros_domain_id()
            self.assertTrue(0 <= resolved <= 232)


if __name__ == "__main__":
    unittest.main()

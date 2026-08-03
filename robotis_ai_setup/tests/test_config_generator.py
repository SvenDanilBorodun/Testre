"""Tests for config_generator module."""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from gui.app.config_generator import (
    generate_env_file,
    generate_cloud_only_env,
    read_env_var,
    upsert_env_var,
)
from gui.app.device_manager import ArmDevice, CameraDevice, HardwareConfig

_GUI_APP_DIR = os.path.join(os.path.dirname(__file__), "..", "gui", "app")


def _read_source(name):
    """The literal text of a gui/app module, for the few invariants no stub can
    reach (a registry-redirection flag, an import-time binding)."""
    with open(os.path.join(_GUI_APP_DIR, name), encoding="utf-8") as fh:
        return fh.read()


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


class TestRosDomainDerivation(unittest.TestCase):
    """ROS_DOMAIN_ID must be PER MACHINE and identical on every launch.

    Both properties now come from ONE place, the seed: ``_read_machine_id()``
    (registry MachineGuid + the computer name) with ``uuid.getnode()` only as
    the last resort. There is no cache — see ``_resolve_ros_domain_id``'s
    docstring for why a cache on Windows can be copy-proof OR a getnode()
    stabiliser but not both, and why the Pi twin's cache is nevertheless right.

    The machine identity is driven through the ``EDUBOTICS_MACHINE_ID`` env var,
    which the resolver reads at CALL time, so the suite stays cross-platform and
    needs no module-attribute patching."""

    # Machine ids the tests pretend to be. Two distinct ones so the cloned-fleet
    # case has a real "other machine" to be distinguished from. Deliberately
    # NUL-free: os.environ refuses an embedded null byte, so the override can
    # never reproduce the real probe's ``\x00``-joined composite shape — it does
    # not have to, it only has to be a stable per-PC string. The composite
    # spelling is fenced by test_ros_domain_twin_lockstep instead.
    MACHINE_A = "00000000-aaaa-4aaa-8aaa-aaaaaaaaaaaa|PC-A"
    MACHINE_B = "00000000-bbbb-4bbb-8bbb-bbbbbbbbbbbb|PC-B"

    def setUp(self):
        from gui.app import config_generator as cg
        self._cg = cg
        self._prev = {k: os.environ.get(k)
                      for k in ("EDUBOTICS_ROS_DOMAIN", "EDUBOTICS_MACHINE_ID")}
        os.environ.pop("EDUBOTICS_ROS_DOMAIN", None)
        os.environ["EDUBOTICS_MACHINE_ID"] = self.MACHINE_A

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ── helpers ──────────────────────────────────────────────────────────
    @staticmethod
    def _expected(machine_id):
        """The domain id _resolve_ros_domain_id must derive for ``machine_id``.

        Recomputed here rather than read back from the module so a change to the
        derivation has to be made deliberately in both places."""
        import hashlib
        digest = hashlib.sha256(machine_id.encode()).digest()
        return int.from_bytes(digest[:2], "big") % 233

    # ── 1. the documented rollback wins ──────────────────────────────────
    def test_env_override_wins_and_is_clamped_to_the_dds_range(self):
        os.environ["EDUBOTICS_ROS_DOMAIN"] = "42"
        self.assertEqual(self._cg._resolve_ros_domain_id(), 42)
        os.environ["EDUBOTICS_ROS_DOMAIN"] = "999"
        self.assertEqual(self._cg._resolve_ros_domain_id(), 232)
        # Non-numeric is not an override at all — fall through to the derive.
        os.environ["EDUBOTICS_ROS_DOMAIN"] = "nope"
        self.assertEqual(self._cg._resolve_ros_domain_id(),
                         self._expected(self.MACHINE_A))

    # ── 2. same machine, every launch ────────────────────────────────────
    def test_the_same_machine_derives_the_same_value_every_time(self):
        first = self._cg._resolve_ros_domain_id()
        self.assertEqual(first, self._expected(self.MACHINE_A))
        self.assertEqual(self._cg._resolve_ros_domain_id(), first)
        self.assertEqual(self._cg._resolve_ros_domain_id(), first)

    # ── 3. THE regression: two PCs must not share a domain ───────────────
    def test_two_machines_land_on_different_domains(self):
        """The whole point. Two students on one school Wi-Fi sharing a DDS
        domain means ``/leader/joint_trajectory`` — the arm command rail —
        is shared, so one student's teleop drives the other's arm."""
        a = self._cg._resolve_ros_domain_id()
        os.environ["EDUBOTICS_MACHINE_ID"] = self.MACHINE_B
        b = self._cg._resolve_ros_domain_id()
        self.assertEqual(a, self._expected(self.MACHINE_A))
        self.assertEqual(b, self._expected(self.MACHINE_B))
        self.assertNotEqual(a, b)

    # ── 4. nothing is written to disk any more ───────────────────────────
    def test_resolving_writes_no_cache_file(self):
        """Decision 1b: the Windows persist is GONE. A cache under a
        user-profile path travels with a roaming/FSLogix/redirected profile and
        with a golden image, and a copy-proof cache can only return what
        re-deriving returns — so it bought nothing and was removed. If a persist
        is ever re-added, this test is the one that will say so."""
        tmpdir = tempfile.mkdtemp()
        try:
            with patch.dict(os.environ, {"LOCALAPPDATA": tmpdir}):
                self._cg._resolve_ros_domain_id()
            self.assertEqual(os.listdir(tmpdir), [],
                             "_resolve_ros_domain_id wrote to %LOCALAPPDATA%")
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        for gone in ("_read_persisted_ros_domain_id", "_persist_ros_domain_id",
                     "_machine_fingerprint"):
            self.assertFalse(hasattr(self._cg, gone),
                             f"{gone} is back — see the no-cache rationale in "
                             "_resolve_ros_domain_id's docstring first")

    # ── 5. no machine identity at all ────────────────────────────────────
    def test_without_a_machine_id_the_getnode_fallback_still_derives(self):
        """The last-resort seed. Must NOT quietly land on the legacy default
        (constants.ROS_DOMAIN_ID = 30) — that value on every PC is the original
        cross-talk bug."""
        import hashlib
        import uuid
        with patch.object(self._cg, "_read_machine_id", return_value=None):
            resolved = self._cg._resolve_ros_domain_id()
        expected = int.from_bytes(
            hashlib.sha256(str(uuid.getnode()).encode()).digest()[:2],
            "big") % 233
        self.assertEqual(resolved, expected)

    def test_a_raising_seed_falls_back_to_the_legacy_default(self):
        from gui.app.constants import ROS_DOMAIN_ID
        with patch.object(self._cg, "_read_machine_id",
                          side_effect=RuntimeError("boom")):
            self.assertEqual(self._cg._resolve_ros_domain_id(),
                             int(ROS_DOMAIN_ID))

    @unittest.skipIf(sys.platform == "win32",
                     "the registry probe genuinely resolves an id on Windows")
    def test_no_override_off_win32_resolves_no_machine_id(self):
        """The platform gate that keeps this module importable (and this suite
        green) off Windows: no registry, so no identity, so getnode()."""
        os.environ["EDUBOTICS_MACHINE_ID"] = ""
        self.assertIsNone(self._cg._read_machine_id())

    def test_override_is_stripped_and_empty_means_absent(self):
        os.environ["EDUBOTICS_MACHINE_ID"] = "  PC-A  "
        self.assertEqual(self._cg._read_machine_id(), "PC-A")
        os.environ["EDUBOTICS_MACHINE_ID"] = "   "
        # Falls through to the platform probe; off win32 that is None.
        if sys.platform != "win32":
            self.assertIsNone(self._cg._read_machine_id())

    def test_the_override_is_read_at_call_time_not_at_import_time(self):
        """It used to be ``constants.MACHINE_ID_OVERRIDE``, frozen at import and
        then value-imported into this module — two bindings, only one of which a
        test could patch. Reading ``os.environ`` inside the function makes the
        knob behave like EDUBOTICS_ROS_DOMAIN, which is the whole point of
        calling it an ops escape hatch."""
        os.environ["EDUBOTICS_MACHINE_ID"] = self.MACHINE_A
        self.assertEqual(self._cg._resolve_ros_domain_id(),
                         self._expected(self.MACHINE_A))
        os.environ["EDUBOTICS_MACHINE_ID"] = self.MACHINE_B
        self.assertEqual(self._cg._resolve_ros_domain_id(),
                         self._expected(self.MACHINE_B))
        from gui.app import constants
        self.assertFalse(hasattr(constants, "MACHINE_ID_OVERRIDE"),
                         "the import-time constant is back")


def _fake_winreg(guid=None, computer=None):
    """A stand-in ``winreg`` module. Only the three names ``_read_machine_id``
    touches exist; a probe for a value the caller did not supply raises OSError,
    exactly as a missing key does on a real machine."""
    import types

    class _Key:
        def __init__(self, tag):
            self.tag = tag

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def open_key(root, path, reserved=0, access=0):
        if "Cryptography" in path:
            if guid is None:
                raise OSError("no MachineGuid")
            return _Key("guid")
        if "ComputerName" in path:
            if computer is None:
                raise OSError("no ComputerName")
            return _Key("name")
        raise OSError(f"unexpected key {path}")

    def query_value_ex(key, name):
        return ({"guid": guid, "name": computer}[key.tag], 1)

    return types.SimpleNamespace(
        HKEY_LOCAL_MACHINE=object(),
        KEY_READ=0x20019,
        KEY_WOW64_64KEY=0x0100,
        OpenKey=open_key,
        QueryValueEx=query_value_ex,
    )


class TestMachineIdChain(unittest.TestCase):
    """The REAL ``_read_machine_id`` chain — registry, %COMPUTERNAME%, hostname.

    Every other ROS_DOMAIN_ID test drives ``EDUBOTICS_MACHINE_ID``, which
    returns before the chain even starts, so the chain itself was untested. That
    matters more than usual here: with no cache on Windows, seed stability is
    the ONLY thing pinning a PC to one domain, and the chain's three sources can
    each spell the same machine differently."""

    GUID = "9f1c2d3e-4a5b-6c7d-8e9f-0a1b2c3d4e5f"

    def setUp(self):
        from gui.app import config_generator as cg
        self._cg = cg
        self._env = {k: os.environ.get(k) for k in
                     ("EDUBOTICS_MACHINE_ID", "EDUBOTICS_ROS_DOMAIN",
                      "COMPUTERNAME")}
        for k in self._env:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _read(self, *, guid=None, registry_name=None, env_name=None,
              hostname="fallback-host"):
        """Run the chain with the three name sources controlled independently."""
        if env_name is None:
            os.environ.pop("COMPUTERNAME", None)
        else:
            os.environ["COMPUTERNAME"] = env_name
        fake = _fake_winreg(guid=guid, computer=registry_name)
        with patch.object(sys, "platform", "win32"), \
                patch.dict(sys.modules, {"winreg": fake}), \
                patch.object(self._cg.socket, "gethostname",
                             return_value=hostname):
            return self._cg._read_machine_id()

    # ── the canonicaliser, in isolation ──────────────────────────────────
    def test_canonical_name_folds_the_spellings_one_machine_can_have(self):
        for raw in ("PC-A12", "pc-a12", "  PC-A12  ", "PC-A12.schule.local",
                    "pc-a12.SCHULE.local"):
            with self.subTest(raw):
                self.assertEqual(self._cg._canonical_computer_name(raw), "PC-A12")
        self.assertEqual(self._cg._canonical_computer_name(""), "")
        self.assertEqual(self._cg._canonical_computer_name("   "), "")

    # ── the chain composes both halves ───────────────────────────────────
    def test_the_chain_composes_the_guid_and_the_computer_name(self):
        got = self._read(guid=self.GUID, registry_name="PC-A12")
        self.assertEqual(got, f"{self.GUID}\x00PC-A12")

    # ── THE property: one machine, one domain, whichever source answers ──
    def test_every_name_source_and_spelling_yields_the_SAME_domain(self):
        """A stale ``%COMPUTERNAME%`` in a running shell, a DNS-shaped
        ``gethostname()``, and the registry's own casing must not move the PC
        between domains — that is the failure the deleted cache used to hide."""
        seeds = [
            self._read(guid=self.GUID, registry_name="PC-A12"),
            self._read(guid=self.GUID, registry_name="pc-a12"),
            self._read(guid=self.GUID, registry_name="PC-A12.schule.local"),
            # registry unreadable -> %COMPUTERNAME%
            self._read(guid=self.GUID, env_name="  pc-a12 "),
            # neither -> socket.gethostname()
            self._read(guid=self.GUID, hostname="pc-a12.schule.local"),
        ]
        self.assertEqual(len(set(seeds)), 1, f"seeds diverged: {seeds}")

        domains = set()
        for seed in seeds:
            with patch.object(self._cg, "_read_machine_id", return_value=seed):
                domains.add(self._cg._resolve_ros_domain_id())
        self.assertEqual(len(domains), 1)

    # ── the ORDER of the three name sources ──────────────────────────────
    # The order is a decision with a named reason in _read_machine_id's
    # docstring, and it matters because there is no cache: the seed IS the pin,
    # so any source switch that changes the STRING moves the PC to a new DDS
    # domain, and `/leader/joint_trajectory` is the arm command rail.
    # Canonicalisation folds case and the DNS suffix, so it cannot fold the one
    # case that survives it — two genuinely DIFFERENT names for one machine.
    #
    # The two hops were NOT equally covered before, and the difference is worth
    # stating rather than assuming. Measured:
    #   * registry-before-%COMPUTERNAME% was fenced by NOTHING — swapping it left
    #     all 849 tests green, because `_read` above supplies only ONE name
    #     source per call, which is precisely the arrangement in which order
    #     cannot matter.
    #   * %COMPUTERNAME%-before-gethostname was already caught, but only as a
    #     SIDE EFFECT: test_every_name_source_and_spelling_yields_the_SAME_domain
    #     leaves `hostname` at its distinct "fallback-host" default in the
    #     `env_name` case, so a swap makes its seeds diverge. That is a real
    #     catch with an unrelated failure message; the test below makes the
    #     property a direct, named assertion.
    def test_the_registry_name_wins_over_a_stale_COMPUTERNAME(self):
        """THE case canonicalisation cannot fix: a shell started BEFORE a machine
        rename still carries the old ``%COMPUTERNAME%`` for its whole lifetime,
        while ``ActiveComputerName`` is the OS's own record of the name the
        machine answers to NOW. Two different names, one machine — so the
        registry has to be asked first, or the domain depends on how old the
        process's environment block is."""
        self.assertEqual(
            self._read(guid=self.GUID, registry_name="PC-NEU",
                       env_name="PC-ALT"),
            f"{self.GUID}\x00PC-NEU")

    def test_COMPUTERNAME_wins_over_gethostname(self):
        """The second step, for the same reason in the other direction:
        ``socket.gethostname()`` can hand back a DNS- or NetBIOS-shaped variant,
        so it is the source LEAST likely to agree with the other two and is
        deliberately last. (Canonicalisation folds a plain suffix; it cannot fold
        a genuinely different label, which is what this pins.)"""
        self.assertEqual(
            self._read(guid=self.GUID, registry_name=None, env_name="PC-ALT",
                       hostname="PC-DNS.schule.local"),
            f"{self.GUID}\x00PC-ALT")

    # ── and two real machines still separate ─────────────────────────────
    def test_the_same_guid_with_a_different_name_is_a_different_machine(self):
        """The non-generalized-clone case the composite exists for: identical
        MachineGuid, different computer name."""
        a = self._read(guid=self.GUID, registry_name="PC-A12")
        b = self._read(guid=self.GUID, registry_name="PC-B07")
        self.assertNotEqual(a, b)

    # ── nothing readable at all ──────────────────────────────────────────
    def test_no_guid_and_no_name_yields_None_so_getnode_takes_over(self):
        got = self._read(guid=None, registry_name=None, hostname="")
        self.assertIsNone(got)

    def test_a_name_with_no_guid_is_still_an_identity(self):
        self.assertEqual(self._read(registry_name="PC-A12"), "PC-A12")

    def test_the_machineguid_open_is_wow64_64bit(self):
        """`MachineGuid` lives under a WOW64-REDIRECTED key.

        Without ``KEY_WOW64_64KEY`` a 32-bit interpreter reads the
        ``WOW6432Node`` view and sees a different value — or none — so the seed,
        and therefore ROS_DOMAIN_ID, would depend on the bitness of the Python
        that happened to run. A source assertion because the ``winreg`` stub in
        these tests cannot model redirection: measured, dropping the flag left
        the whole suite green, which is exactly the shape this file exists to
        close.
        """
        src = _read_source("config_generator.py")
        self.assertIn("KEY_WOW64_64KEY", src)
        self.assertIn("winreg.KEY_READ | winreg.KEY_WOW64_64KEY", src)


class TestHfTokenMachineBinding(unittest.TestCase):
    """The HuggingFace token is bound to the PC it was entered on.

    ``%LOCALAPPDATA%\\EduBotics\\.env`` TRAVELS — roaming profile, FSLogix
    container, AppData redirection, or a golden image captured after a first
    launch — and it carries ``HF_TOKEN``, which compose forwards into
    physical_ai_server out of the ``--env-file``. On the shared-Windows-account
    fleet this whole change set exists for, a copied profile therefore uploaded
    one student's recordings under another student's HuggingFace account.

    ``HF_TOKEN_MACHINE`` (a sha256 of ``_read_machine_id()``, the SAME identity
    notion ROS_DOMAIN_ID uses) is stored beside it, and the pair is judged by the
    single predicate ``hf_token_is_foreign``. Driven through
    ``EDUBOTICS_MACHINE_ID``, read at call time, so these tests are
    platform-independent — on a real non-Windows host the registry probe returns
    None and every branch below would fail open.
    """

    MACHINE_A = "00000000-aaaa-4aaa-8aaa-aaaaaaaaaaaa|PC-A12"
    MACHINE_B = "00000000-bbbb-4bbb-8bbb-bbbbbbbbbbbb|PC-B07"

    _ENV_KEYS = ("EDUBOTICS_MACHINE_ID", "EDUBOTICS_HF_TOKEN_ANY_MACHINE",
                 "EDUBOTICS_CAMERA_SOURCE", "EDUBOTICS_ROS_DOMAIN")

    def setUp(self):
        from gui.app import config_generator as cg
        self.cg = cg
        self._prev = {k: os.environ.get(k) for k in self._ENV_KEYS}
        os.environ.pop("EDUBOTICS_HF_TOKEN_ANY_MACHINE", None)
        os.environ.pop("EDUBOTICS_ROS_DOMAIN", None)
        # Pin the camera source so the generated layout is stable cross-platform.
        os.environ["EDUBOTICS_CAMERA_SOURCE"] = "usb_cam"
        self._on(self.MACHINE_A)

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ── helpers ──────────────────────────────────────────────────────────
    @staticmethod
    def _on(machine_id):
        """Pretend the code is running on ``machine_id``."""
        os.environ["EDUBOTICS_MACHINE_ID"] = machine_id

    def _tmp(self):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False)
        f.close()
        self.addCleanup(lambda: os.path.exists(f.name) and os.unlink(f.name))
        return f.name

    @staticmethod
    def _body(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def _hardware(self):
        return HardwareConfig(
            leader=ArmDevice("1-3", "/dev/serial/by-id/leader", "leader",
                             "OpenRB-150"),
            follower=ArmDevice("1-4", "/dev/serial/by-id/follower", "follower",
                               "OpenRB-150"),
        )

    def _regenerate(self, path):
        return generate_env_file(self._hardware(), output_path=path)

    def _seed_on_machine_a(self, token="hf_studentA"):
        """A .env exactly as machine A's GUI would leave it."""
        path = self._tmp()
        self._on(self.MACHINE_A)
        self.cg.write_hf_token(token, path)
        return path

    # ── 1. a stored token always carries a stamp ─────────────────────────
    def test_write_hf_token_stamps_the_machine_in_the_same_breath(self):
        """Stamping at WRITE time is what makes the adopt-legacy branch mean
        only "written by a build that predates this feature". If the stamp were
        added lazily on the next launch, a profile copied in between would look
        legacy on the clone and be adopted there."""
        path = self._seed_on_machine_a()
        self.assertEqual(read_env_var("HF_TOKEN", path), "hf_studentA")
        stamp = read_env_var("HF_TOKEN_MACHINE", path)
        self.assertTrue(stamp)
        self.assertEqual(stamp, self.cg._hf_token_fingerprint())

    def test_the_machine_identifier_itself_never_lands_in_the_file(self):
        """A HASH, not the id: the .env is the artefact support collects."""
        path = self._seed_on_machine_a()
        body = self._body(path)
        self.assertNotIn(self.MACHINE_A, body)
        self.assertNotIn("PC-A12", body)

    def test_the_two_keys_do_not_shadow_each_other(self):
        """``HF_TOKEN`` is a prefix of ``HF_TOKEN_MACHINE``, so an inexact key
        match would make the reader return the stamp for the token (or the
        writer overwrite one with the other)."""
        path = self._seed_on_machine_a()
        self.assertEqual(read_env_var("HF_TOKEN", path), "hf_studentA")
        self.assertNotEqual(read_env_var("HF_TOKEN_MACHINE", path),
                            "hf_studentA")
        self.assertEqual(self._body(path).count("HF_TOKEN="), 1)
        self.assertEqual(self._body(path).count("HF_TOKEN_MACHINE="), 1)

    # ── 2. the token's whole purpose survives on ITS OWN machine ─────────
    def test_the_same_machine_keeps_the_token_across_regenerates(self):
        """THE thing that must not break. A hardware re-scan, a camera change and
        the Roboter-Studio leader toggle all regenerate the .env; the token is
        unmanaged precisely so it survives that."""
        path = self._seed_on_machine_a()
        self.assertEqual(self.cg.bind_hf_token_to_this_machine(path),
                         self.cg.HF_TOKEN_OK)
        for _ in range(3):
            content = self._regenerate(path)
            self.assertIn('HF_TOKEN="hf_studentA"', content)
            self.assertEqual(content.count("HF_TOKEN="), 1)
        self.assertEqual(read_env_var("HF_TOKEN", path), "hf_studentA")
        self.assertEqual(read_env_var("HF_TOKEN_MACHINE", path),
                         self.cg._hf_token_fingerprint())

    def test_binding_the_same_machine_repeatedly_writes_nothing(self):
        """The OK verdict must be a pure read — a rewrite on every launch would
        churn the file the .env's atomic-write contract protects."""
        path = self._seed_on_machine_a()
        before = self._body(path)
        with patch.object(self.cg, "_atomic_write") as writer:
            self.assertEqual(self.cg.bind_hf_token_to_this_machine(path),
                             self.cg.HF_TOKEN_OK)
        writer.assert_not_called()
        self.assertEqual(self._body(path), before)

    def test_factory_reset_cannot_touch_the_token(self):
        """Factory Reset wipes the three DOCKER VOLUMES and nothing else, which
        is the whole reason the token lives in the host .env: a student who
        clears their datasets must not have to re-enter it.

        Both halves are pinned — no .env write escapes docker_manager's reset
        (patching config_generator's single writer catches it whichever path
        constant a regression would use), and the GUI's own reset method names
        nothing token-related."""
        from gui.app import docker_manager

        path = self._seed_on_machine_a()
        before = self._body(path)

        class _Result:
            returncode = 0
            stdout = "robotis_ai_setup_ai_workspace\nsomething_else\n"
            stderr = ""

        with patch.object(self.cg, "_atomic_write") as writer, \
                patch.object(docker_manager, "stop_containers"), \
                patch.object(docker_manager.subprocess, "run",
                             return_value=_Result()):
            ok, _msg = docker_manager.factory_reset()
        self.assertTrue(ok)
        writer.assert_not_called()
        self.assertEqual(self._body(path), before)
        self.assertEqual(read_env_var("HF_TOKEN", path), "hf_studentA")

        gui_src = _read_source("gui_app.py")
        start = gui_src.index("    def _factory_reset(self")
        end = gui_src.index("\n    def ", start + 1)
        reset_src = gui_src[start:end]
        for forbidden in ("HF_TOKEN", "write_hf_token", "upsert_env_var",
                          "ENV_FILE"):
            self.assertNotIn(
                forbidden, reset_src,
                f"gui_app._factory_reset now names {forbidden!r} — Factory "
                f"Reset deletes docker volumes, never the host .env")

    # ── 3. THE regression: a copied profile ──────────────────────────────
    def test_a_copied_env_has_its_credential_deleted_not_merely_ignored(self):
        """Refusing to USE it is not enough: HF_TOKEN is unmanaged, so an ignored
        token stays in the file and compose forwards it on the next start."""
        path = self._seed_on_machine_a()
        self._on(self.MACHINE_B)
        self.assertEqual(self.cg.bind_hf_token_to_this_machine(path),
                         self.cg.HF_TOKEN_FOREIGN)
        self.assertIsNone(read_env_var("HF_TOKEN", path))
        self.assertNotIn("hf_studentA", self._body(path))
        # The stamp goes with it: left behind, it would judge the token the
        # student is about to enter against the previous owner's fingerprint.
        self.assertIsNone(read_env_var("HF_TOKEN_MACHINE", path))

    def test_a_mismatch_reports_the_token_as_absent_so_schritt_d_re_prompts(self):
        """The GUI's Schritt D status label reads the .env through read_env_var,
        so "deleted" and "never entered" have to be the same state."""
        path = self._seed_on_machine_a()
        self._on(self.MACHINE_B)
        self.cg.bind_hf_token_to_this_machine(path)
        fresh = self._tmp()
        self.assertEqual(read_env_var("HF_TOKEN", path),
                         read_env_var("HF_TOKEN", fresh))

    def test_a_foreign_token_never_reaches_the_generated_env(self):
        """The structural half, tested WITHOUT the purge having run: the .env
        compose reads is one a generator just wrote, and generate_env_file has
        callers that never pass through the GUI's startup —
        gui_app.py::_rs_set_leader_mode regenerates at RUNTIME, on the :8769
        control-server thread, for the Roboter-Studio leader toggle."""
        path = self._seed_on_machine_a()
        self._on(self.MACHINE_B)
        content = self._regenerate(path)
        self.assertNotIn("HF_TOKEN=", content)
        self.assertNotIn("hf_studentA", content)
        self.assertNotIn("HF_TOKEN_MACHINE=", content)
        # ... and it is gone from disk too, because that IS the file compose reads.
        self.assertIsNone(read_env_var("HF_TOKEN", path))

    def test_cloud_only_regeneration_drops_it_too(self):
        path = self._seed_on_machine_a()
        self._on(self.MACHINE_B)
        content = generate_cloud_only_env(output_path=path)
        self.assertNotIn("HF_TOKEN=", content)
        self.assertNotIn("hf_studentA", content)

    def test_a_foreign_purge_keeps_every_other_operator_line(self):
        """Only the credential is removed. An operator's own overrides are not
        collateral."""
        path = self._seed_on_machine_a()
        upsert_env_var("EDUBOTICS_CAMERA_PIXEL_FORMAT", "mjpeg2rgb", path)
        self._on(self.MACHINE_B)
        self.cg.bind_hf_token_to_this_machine(path)
        self.assertEqual(read_env_var("EDUBOTICS_CAMERA_PIXEL_FORMAT", path),
                         "mjpeg2rgb")
        content = self._regenerate(path)
        self.assertIn("EDUBOTICS_CAMERA_PIXEL_FORMAT=", content)

    def test_the_purge_removes_the_token_before_the_stamp(self):
        """Write ORDER, and it is load-bearing. A crash between the two writes
        must leave "no token + stale stamp", which the nothing-stored branch
        cleans up. Reversed, the window leaves an UNSTAMPED foreign token — which
        the adopt branch would claim as this machine's own."""
        path = self._seed_on_machine_a()
        self._on(self.MACHINE_B)
        seen = []
        real = self.cg.upsert_env_var

        def _spy(key, value, p=path):
            seen.append((key, value))
            return real(key, value, p)

        with patch.object(self.cg, "upsert_env_var", side_effect=_spy):
            self.cg.bind_hf_token_to_this_machine(path)
        self.assertEqual(seen, [("HF_TOKEN", ""), ("HF_TOKEN_MACHINE", "")])

    def test_a_stamp_left_without_a_token_is_cleaned_up(self):
        """The crash window above, replayed. It must NOT be adoptable: a token
        entered later would otherwise be judged against a stranger's stamp."""
        path = self._tmp()
        upsert_env_var("HF_TOKEN_MACHINE", "deadbeef", path)
        self.assertEqual(self.cg.bind_hf_token_to_this_machine(path),
                         self.cg.HF_TOKEN_OK)
        self.assertIsNone(read_env_var("HF_TOKEN_MACHINE", path))

    def test_clearing_the_token_clears_the_stamp(self):
        path = self._seed_on_machine_a()
        self.cg.write_hf_token("", path)
        self.assertIsNone(read_env_var("HF_TOKEN", path))
        self.assertIsNone(read_env_var("HF_TOKEN_MACHINE", path))

    # ── 4. ABSENT stamp is LEGACY, not foreign ───────────────────────────
    def test_a_token_with_no_stamp_is_adopted_never_refused(self):
        """Every install in the field has a token and no stamp. Reading absence
        as a mismatch would re-prompt Schritt D on 100 % of the fleet.

        This is the OPPOSITE call from the deleted ROS_DOMAIN_ID cache, whose
        legacy handling was to simply re-derive — because re-deriving a domain id
        costs nothing and nobody notices, while re-prompting costs every student
        a trip to huggingface.co for a token they already entered once."""
        path = self._tmp()
        upsert_env_var("HF_TOKEN", "hf_legacy", path)   # no stamp: a pre-upgrade .env
        self.assertFalse(self.cg.hf_token_is_foreign(None))
        self.assertEqual(self.cg.bind_hf_token_to_this_machine(path),
                         self.cg.HF_TOKEN_ADOPTED)
        self.assertEqual(read_env_var("HF_TOKEN", path), "hf_legacy")
        self.assertEqual(read_env_var("HF_TOKEN_MACHINE", path),
                         self.cg._hf_token_fingerprint())

    def test_an_unstamped_token_survives_a_regenerate_unadopted(self):
        """The upgrade is not required to have happened yet: a regenerate that
        runs before the first bind must still carry a legacy token through."""
        path = self._tmp()
        upsert_env_var("HF_TOKEN", "hf_legacy", path)
        content = self._regenerate(path)
        self.assertIn('HF_TOKEN="hf_legacy"', content)

    def test_adoption_is_idempotent(self):
        path = self._tmp()
        upsert_env_var("HF_TOKEN", "hf_legacy", path)
        self.assertEqual(self.cg.bind_hf_token_to_this_machine(path),
                         self.cg.HF_TOKEN_ADOPTED)
        self.assertEqual(self.cg.bind_hf_token_to_this_machine(path),
                         self.cg.HF_TOKEN_OK)
        self.assertEqual(self._body(path).count("HF_TOKEN_MACHINE="), 1)

    def test_an_adopted_token_is_then_refused_on_another_machine(self):
        """Adopt-then-protect: the legacy fail-open is a one-time upgrade step,
        not a permanent hole."""
        path = self._tmp()
        upsert_env_var("HF_TOKEN", "hf_legacy", path)
        self.cg.bind_hf_token_to_this_machine(path)
        self._on(self.MACHINE_B)
        self.assertEqual(self.cg.bind_hf_token_to_this_machine(path),
                         self.cg.HF_TOKEN_FOREIGN)

    # ── 5. the documented one-variable escape hatch ──────────────────────
    def test_the_opt_out_accepts_a_foreign_token(self):
        """The legitimate workflow: one service-account token baked into a golden
        image and cloned onto 30 PCs. Without an opt-out every clone re-prompts
        Schritt D."""
        path = self._seed_on_machine_a()
        self._on(self.MACHINE_B)
        os.environ["EDUBOTICS_HF_TOKEN_ANY_MACHINE"] = "1"
        self.assertFalse(self.cg.hf_token_is_foreign("some-other-stamp"))
        self.assertEqual(self.cg.bind_hf_token_to_this_machine(path),
                         self.cg.HF_TOKEN_OK)
        self.assertEqual(read_env_var("HF_TOKEN", path), "hf_studentA")
        self.assertIn('HF_TOKEN="hf_studentA"', self._regenerate(path))

    def test_the_opt_out_is_read_at_call_time_and_never_persisted(self):
        """Same contract as EDUBOTICS_ROS_DOMAIN: an env knob, not a stored
        setting — so withdrawing it resumes protection on the next launch."""
        path = self._seed_on_machine_a()
        self._on(self.MACHINE_B)
        os.environ["EDUBOTICS_HF_TOKEN_ANY_MACHINE"] = "1"
        self.cg.bind_hf_token_to_this_machine(path)
        self.assertNotIn("ANY_MACHINE", self._body(path))
        os.environ.pop("EDUBOTICS_HF_TOKEN_ANY_MACHINE")
        self.assertEqual(self.cg.bind_hf_token_to_this_machine(path),
                         self.cg.HF_TOKEN_FOREIGN)

    def test_the_opt_out_only_fires_on_a_truthy_value(self):
        for value, expected in (("1", False), ("true", False), ("YES", False),
                                ("0", True), ("", True), ("nope", True)):
            with self.subTest(value=value):
                os.environ["EDUBOTICS_HF_TOKEN_ANY_MACHINE"] = value
                self.assertEqual(
                    self.cg.hf_token_is_foreign("some-other-stamp"), expected)

    # ── 6. refuse on PROOF only ──────────────────────────────────────────
    def test_an_unresolvable_fingerprint_keeps_the_token(self):
        """On a PC where the registry probe fails there is nothing to judge
        against, and deleting the student's credential would be the worse
        answer — the same refuse-only-on-proof stance
        device_manager.serial_path_family_conflict takes."""
        path = self._seed_on_machine_a()
        with patch.object(self.cg, "_read_machine_id", return_value=None):
            self.assertFalse(self.cg.hf_token_is_foreign("some-other-stamp"))
            self.assertEqual(self.cg.bind_hf_token_to_this_machine(path),
                             self.cg.HF_TOKEN_OK)
            self.assertEqual(read_env_var("HF_TOKEN", path), "hf_studentA")
            self.assertIn("HF_TOKEN=", self._regenerate(path))

    def test_writing_without_a_resolvable_fingerprint_stamps_nothing(self):
        """No stamp at all rather than an empty one: an empty value would read as
        legacy anyway, and a key with no value is noise in the file."""
        path = self._tmp()
        with patch.object(self.cg, "_read_machine_id", return_value=None):
            self.cg.write_hf_token("hf_x", path)
        self.assertEqual(read_env_var("HF_TOKEN", path), "hf_x")
        self.assertIsNone(read_env_var("HF_TOKEN_MACHINE", path))

    # ── 7. the identity notion is SHARED with ROS_DOMAIN_ID ──────────────
    def test_the_fingerprint_reuses_read_machine_id_and_is_not_a_domain_cache(self):
        """One machine-identity notion for the whole GUI. And the name matters:
        ``_machine_fingerprint`` is asserted ABSENT by
        test_ros_domain_twin_lockstep and by TestRosDomainDerivation, because a
        fingerprint under that name is how the deleted ROS_DOMAIN_ID cache would
        come back. This one must stay unreachable from the resolver."""
        with patch.object(self.cg, "_read_machine_id",
                          return_value="sentinel") as probe:
            self.cg._hf_token_fingerprint()
        probe.assert_called_once_with()
        self.assertFalse(hasattr(self.cg, "_machine_fingerprint"))
        src = _read_source("config_generator.py")
        resolver = src[src.index("def _resolve_ros_domain_id("):
                       src.index("def _atomic_write(")]
        self.assertNotIn("_hf_token", resolver,
                         "the ROS_DOMAIN_ID resolver now reaches the token "
                         "fingerprint — test_ros_domain_twin_lockstep's "
                         "no-cache call-graph walk would follow it")


class TestConfigGeneratorFollowerOnly(unittest.TestCase):
    """Roboter Studio follower-only mode (EDUBOTICS_FOLLOWER_ONLY=1): no leader
    launched, so the workflow/calibration publisher is the sole writer on
    /leader/joint_trajectory (no teleop broadcaster to clobber it)."""

    def setUp(self):
        self._prev_src = os.environ.get("EDUBOTICS_CAMERA_SOURCE")
        os.environ["EDUBOTICS_CAMERA_SOURCE"] = "usb_cam"

    def tearDown(self):
        if self._prev_src is None:
            os.environ.pop("EDUBOTICS_CAMERA_SOURCE", None)
        else:
            os.environ["EDUBOTICS_CAMERA_SOURCE"] = self._prev_src

    def _config(self, with_leader):
        leader = None
        if with_leader:
            leader = ArmDevice(
                busid="1-3", serial_path="/dev/serial/by-id/leader",
                role="leader", description="OpenRB-150")
        return HardwareConfig(
            leader=leader,
            follower=ArmDevice(
                busid="1-4", serial_path="/dev/serial/by-id/follower",
                role="follower", description="OpenRB-150"),
            cameras=[CameraDevice(path="/dev/video2", name="Scene Cam", role="scene")],
        )

    def _tmp(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            return f.name

    def test_follower_only_emits_flag_and_omits_leader(self):
        tmp_path = self._tmp()
        try:
            content = generate_env_file(
                self._config(with_leader=False), output_path=tmp_path,
                follower_only=True)
            self.assertIn("EDUBOTICS_FOLLOWER_ONLY=1", content)
            self.assertIn('FOLLOWER_PORT="/dev/serial/by-id/follower"', content)
            self.assertNotIn("LEADER_PORT=", content)
        finally:
            os.unlink(tmp_path)

    def test_follower_only_tolerates_missing_leader(self):
        # No leader scanned — must NOT raise in follower-only mode.
        tmp_path = self._tmp()
        try:
            generate_env_file(
                self._config(with_leader=False), output_path=tmp_path,
                follower_only=True)
        finally:
            os.unlink(tmp_path)

    def test_recording_mode_still_requires_leader(self):
        tmp_path = self._tmp()
        try:
            with self.assertRaises(ValueError):
                generate_env_file(
                    self._config(with_leader=False), output_path=tmp_path,
                    follower_only=False)
        finally:
            os.unlink(tmp_path)

    def test_follower_only_flag_is_managed_and_superseded(self):
        # A follower-only .env regenerated as a recording session must DROP the
        # stale EDUBOTICS_FOLLOWER_ONLY=1 (MANAGED key) and restore LEADER_PORT.
        tmp_path = self._tmp()
        try:
            c1 = generate_env_file(
                self._config(with_leader=False), output_path=tmp_path,
                follower_only=True)
            self.assertIn("EDUBOTICS_FOLLOWER_ONLY=1", c1)
            c2 = generate_env_file(
                self._config(with_leader=True), output_path=tmp_path,
                follower_only=False)
            self.assertNotIn("EDUBOTICS_FOLLOWER_ONLY", c2)
            self.assertIn("LEADER_PORT=", c2)
        finally:
            os.unlink(tmp_path)


class TestSingleSceneCamera(unittest.TestCase):
    """The Roboter Studio kit is ONE scene camera (no gripper cam). The .env it
    produces must be a valid SINGLE-camera config: CAMERA_NAME_1=scene with NO
    CAMERA_NAME_2 / CAMERA_DEVICE_2 line, so the native_bridge healthcheck (which
    gates its 2nd-camera probe on a non-empty CAMERA_NAME_2) passes on the scene
    topic alone. The 2-camera classroom path is covered by the tests above."""

    def setUp(self):
        self._prev_src = os.environ.get("EDUBOTICS_CAMERA_SOURCE")
        os.environ["EDUBOTICS_CAMERA_SOURCE"] = "native_bridge"

    def tearDown(self):
        if self._prev_src is None:
            os.environ.pop("EDUBOTICS_CAMERA_SOURCE", None)
        else:
            os.environ["EDUBOTICS_CAMERA_SOURCE"] = self._prev_src

    def _single_scene_config(self):
        # One scene camera, no leader — the Roboter Studio follower-only kit.
        return HardwareConfig(
            follower=ArmDevice(
                "1-4", "/dev/serial/by-id/follower", "follower", "OpenRB-150"),
            cameras=[
                CameraDevice(path="Index 1", name="Scene Cam",
                             role="scene", win_index=1),
            ],
        )

    def test_single_scene_camera_env_is_valid(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            tmp_path = f.name
        try:
            content = generate_env_file(
                self._single_scene_config(), output_path=tmp_path,
                follower_only=True)
            # Exactly ONE camera slot, named scene; native bridge so device is empty.
            self.assertIn('CAMERA_NAME_1="scene"', content)
            self.assertIn('CAMERA_DEVICE_1=""', content)
            # No phantom 2nd slot — the healthcheck must not wait on a /scene_2.
            self.assertNotIn("CAMERA_NAME_2", content)
            self.assertNotIn("CAMERA_DEVICE_2", content)
            # Follower-only Roboter Studio kit: no leader.
            self.assertIn("EDUBOTICS_FOLLOWER_ONLY=1", content)
            self.assertNotIn("LEADER_PORT=", content)
        finally:
            os.unlink(tmp_path)

    def test_single_scene_camera_healthcheck_topic_is_scene(self):
        # The container env-var CAMERA_NAME_1 the healthcheck probes is exactly
        # the camera role, so a single scene camera maps to /scene/.../compressed.
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            tmp_path = f.name
        try:
            content = generate_env_file(
                self._single_scene_config(), output_path=tmp_path,
                follower_only=True)
            name1 = read_env_var("CAMERA_NAME_1", tmp_path)
            self.assertEqual(name1, "scene")
            # CAMERA_NAME_2 absent from the file -> container default empties it
            # -> healthcheck skips the 2nd-camera probe.
            self.assertIsNone(read_env_var("CAMERA_NAME_2", tmp_path))
        finally:
            os.unlink(tmp_path)


class TestConfigGeneratorRobotType(unittest.TestCase):
    """EDUBOTICS_ROBOT_TYPE is the GUI-hardset ArmProfile id (MANAGED). It must
    be emitted by BOTH generators, DERIVE the initial EDUBOTICS_FOLLOWER_ONLY
    when follower_only is left None, honour an explicit follower_only override
    (the RS runtime toggle), and supersede a stale hand-pinned value."""

    def setUp(self):
        # Pin usb_cam so CAMERA_DEVICE layout is stable cross-platform.
        self._prev_src = os.environ.get("EDUBOTICS_CAMERA_SOURCE")
        os.environ["EDUBOTICS_CAMERA_SOURCE"] = "usb_cam"

    def tearDown(self):
        if self._prev_src is None:
            os.environ.pop("EDUBOTICS_CAMERA_SOURCE", None)
        else:
            os.environ["EDUBOTICS_CAMERA_SOURCE"] = self._prev_src

    def _config(self, with_leader):
        leader = None
        if with_leader:
            leader = ArmDevice(
                busid="1-3", serial_path="/dev/serial/by-id/leader",
                role="leader", description="OpenRB-150")
        return HardwareConfig(
            leader=leader,
            follower=ArmDevice(
                busid="1-4", serial_path="/dev/serial/by-id/follower",
                role="follower", description="OpenRB-150"),
        )

    def _tmp(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            return f.name

    def test_robot_type_emitted_once(self):
        p = self._tmp()
        try:
            content = generate_env_file(
                self._config(with_leader=True), output_path=p,
                robot_type="omx_full")
            self.assertIn("EDUBOTICS_ROBOT_TYPE=omx_full", content)
            self.assertEqual(content.count("EDUBOTICS_ROBOT_TYPE="), 1)
        finally:
            os.unlink(p)

    def test_omx_follower_derives_follower_only_without_leader(self):
        # THE high-risk item: omx_follower has no leader, so the derive-first
        # ordering must set follower_only=True BEFORE the leader-null guard —
        # otherwise this raises ValueError at every start.
        p = self._tmp()
        try:
            content = generate_env_file(
                self._config(with_leader=False), output_path=p,
                robot_type="omx_follower")  # follower_only left as None → derive
            self.assertIn("EDUBOTICS_ROBOT_TYPE=omx_follower", content)
            self.assertIn("EDUBOTICS_FOLLOWER_ONLY=1", content)
            self.assertNotIn("LEADER_PORT=", content)
        finally:
            os.unlink(p)

    def test_omx_full_derives_both_arms(self):
        p = self._tmp()
        try:
            content = generate_env_file(
                self._config(with_leader=True), output_path=p,
                robot_type="omx_full")  # follower_only None → derive False
            self.assertIn("EDUBOTICS_ROBOT_TYPE=omx_full", content)
            self.assertNotIn("EDUBOTICS_FOLLOWER_ONLY", content)
            self.assertIn("LEADER_PORT=", content)
        finally:
            os.unlink(p)

    def test_explicit_follower_only_override_wins_over_type(self):
        # The RS runtime toggle switches an omx_full session to follower-only
        # WITHOUT changing the type: explicit follower_only=True must win, and
        # EDUBOTICS_ROBOT_TYPE must stay omx_full.
        p = self._tmp()
        try:
            content = generate_env_file(
                self._config(with_leader=True), output_path=p,
                robot_type="omx_full", follower_only=True)
            self.assertIn("EDUBOTICS_ROBOT_TYPE=omx_full", content)
            self.assertIn("EDUBOTICS_FOLLOWER_ONLY=1", content)
            self.assertNotIn("LEADER_PORT=", content)
        finally:
            os.unlink(p)

    def test_explicit_follower_only_false_on_follower_type_is_refused(self):
        # A follower-only profile (omx_follower) has NO leader, so explicitly
        # re-arming one (follower_only=False) is contradictory — it would emit a
        # both-arms .env demanding a LEADER_PORT the rig never scanned. It must
        # be refused in German, not silently accepted (audit fix). No live caller
        # hits this: the GUI passes follower_only=None and _rs_set_leader_mode
        # refuses an omx_follower type before it ever regenerates the .env.
        p = self._tmp()
        try:
            with self.assertRaises(ValueError) as ctx:
                generate_env_file(
                    self._config(with_leader=True), output_path=p,
                    robot_type="omx_follower", follower_only=False)
            self.assertIn("omx_follower", str(ctx.exception))
        finally:
            os.unlink(p)

    def test_stale_robot_type_is_superseded(self):
        # EDUBOTICS_ROBOT_TYPE is MANAGED → a stale hand-pinned value is dropped
        # and replaced on regenerate (R3 lockstep).
        p = self._tmp()
        with open(p, "w") as f:
            f.write("EDUBOTICS_ROBOT_TYPE=some_old_type\n")
        try:
            content = generate_env_file(
                self._config(with_leader=True), output_path=p,
                robot_type="omx_full")
            self.assertNotIn("some_old_type", content)
            self.assertEqual(content.count("EDUBOTICS_ROBOT_TYPE="), 1)
            self.assertIn("EDUBOTICS_ROBOT_TYPE=omx_full", content)
        finally:
            os.unlink(p)

    def test_cloud_only_emits_robot_type(self):
        p = self._tmp()
        try:
            content = generate_cloud_only_env(
                output_path=p, robot_type="omx_follower")
            self.assertIn("EDUBOTICS_ROBOT_TYPE=omx_follower", content)
            self.assertEqual(content.count("EDUBOTICS_ROBOT_TYPE="), 1)
        finally:
            os.unlink(p)

    def test_cloud_only_default_robot_type(self):
        from gui.app.constants import DEFAULT_ROBOT_PROFILE
        p = self._tmp()
        try:
            content = generate_cloud_only_env(output_path=p)
            self.assertIn(f"EDUBOTICS_ROBOT_TYPE={DEFAULT_ROBOT_PROFILE}", content)
        finally:
            os.unlink(p)


if __name__ == "__main__":
    unittest.main()

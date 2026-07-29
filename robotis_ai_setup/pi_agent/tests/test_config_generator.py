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

    def test_env_file_is_0600_on_write_and_regenerate(self):
        # The .env holds the student's HF_TOKEN. It must be 0600 on the first
        # write AND stay 0600 after a regenerate — os.replace adopts the tmp
        # file's mode, so without an explicit chmod a rewrite silently re-widens
        # the secret to world-readable (the H1 regression this guards).
        generate_env_file(_both_arms(), self.path)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        # Simulate a pre-existing world-readable file, then regenerate.
        os.chmod(self.path, 0o644)
        generate_env_file(_both_arms(), self.path)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)


class TestRobotType(_TmpEnvBase):
    """EDUBOTICS_ROBOT_TYPE is a MANAGED key emitted by BOTH generators; agent
    callers carry the on-disk value forward (a regenerate must never rewrite the
    selected profile silently — the wizard's POST /robot-type owns it)."""

    def test_default_robot_type_emitted_once(self):
        content = generate_env_file(_both_arms(), self.path)
        self.assertIn("EDUBOTICS_ROBOT_TYPE=omx_full", content)
        self.assertEqual(content.count("EDUBOTICS_ROBOT_TYPE="), 1)

    def test_explicit_robot_type_emitted(self):
        content = generate_env_file(_both_arms(), self.path,
                                    robot_type="omx_follower")
        self.assertIn("EDUBOTICS_ROBOT_TYPE=omx_follower", content)

    def test_stale_robot_type_superseded(self):
        # MANAGED → a stale hand-pinned value is dropped, not preserved.
        with open(self.path, "w") as fh:
            fh.write("EDUBOTICS_ROBOT_TYPE=some_old_type\n")
        content = generate_env_file(_both_arms(), self.path,
                                    robot_type="omx_full")
        self.assertNotIn("some_old_type", content)
        self.assertEqual(content.count("EDUBOTICS_ROBOT_TYPE="), 1)
        self.assertIn("EDUBOTICS_ROBOT_TYPE=omx_full", content)

    def test_cloud_only_emits_robot_type(self):
        content = generate_cloud_only_env(self.path, robot_type="omx_follower")
        self.assertIn("EDUBOTICS_ROBOT_TYPE=omx_follower", content)
        self.assertEqual(content.count("EDUBOTICS_ROBOT_TYPE="), 1)

    def test_cloud_only_default_robot_type(self):
        content = generate_cloud_only_env(self.path)
        self.assertIn("EDUBOTICS_ROBOT_TYPE=omx_full", content)

    def test_managed_keys_lockstep_all_emitted(self):
        # THE lockstep guard: every MANAGED_KEY must be emitted by a both-arms
        # generate AND by the cloud-only generate — orphaning a key in the set
        # while dropping its emit line would leak stale values verbatim across
        # regenerates (the `manifest unknown` scar class).
        both = generate_env_file(_both_arms(), self.path)
        for key in cg.MANAGED_KEYS:
            self.assertIn(f"{key}=", both, f"{key} missing from both-arms .env")
        cloud = generate_cloud_only_env(self.path)
        for key in cg.MANAGED_KEYS:
            self.assertIn(f"{key}=", cloud, f"{key} missing from cloud-only .env")


class TestFollowerOnlyDerivedFromProfile(_TmpEnvBase):
    """`follower_only=None` (the new default) DERIVES from the robot profile.

    Without the derive a follower-only rig — which by definition never scans a
    leader — hit the leader-null guard and raised „Der Leader-Arm muss
    konfiguriert sein" on every single .env write, which is why neither
    omx_follower nor edu6_studio could be provisioned on a Pi at all.
    """

    def _follower_only_rig(self):
        cfg = _both_arms()
        cfg.leader = None  # a follower-only kit HAS no leader
        return cfg

    def test_omx_full_derives_both_arms(self):
        content = generate_env_file(_both_arms(), self.path, robot_type="omx_full")
        self.assertIn("EDUBOTICS_FOLLOWER_ONLY=0", content)
        self.assertIn("LEADER_PORT=", content)

    def test_each_follower_only_profile_derives_follower_only(self):
        for profile in ("omx_follower", "edu6_studio"):
            with self.subTest(profile=profile):
                content = generate_env_file(self._follower_only_rig(), self.path,
                                            robot_type=profile)
                self.assertIn("EDUBOTICS_FOLLOWER_ONLY=1", content)
                self.assertNotIn("LEADER_PORT=", content)
                self.assertIn(f"EDUBOTICS_ROBOT_TYPE={profile}", content)

    def test_the_derive_runs_BEFORE_the_leader_null_guard(self):
        """THE ordering assertion. A leaderless rig on a follower-only profile
        must generate cleanly. Move the derive below the `not follower_only and
        config.leader is None` guard and this raises instead."""
        for profile in ("omx_follower", "edu6_studio"):
            with self.subTest(profile=profile):
                content = generate_env_file(self._follower_only_rig(), self.path,
                                            robot_type=profile)
                self.assertIn("EDUBOTICS_FOLLOWER_ONLY=1", content)

    def test_the_leader_guard_still_bites_on_a_both_arms_profile(self):
        # The ordering fix must not have DISABLED the guard: omx_full with no
        # leader is still a refusal, in German.
        cfg = self._follower_only_rig()
        with self.assertRaises(ValueError) as ctx:
            generate_env_file(cfg, self.path, robot_type="omx_full")
        self.assertIn("Leader-Arm", str(ctx.exception))

    def test_explicit_true_still_overrides_on_a_both_arms_profile(self):
        # The Roboter-Studio leader toggle: omx_full + explicit True.
        content = generate_env_file(_both_arms(), self.path, robot_type="omx_full",
                                    follower_only=True)
        self.assertIn("EDUBOTICS_FOLLOWER_ONLY=1", content)
        self.assertNotIn("LEADER_PORT=", content)

    def test_explicit_false_on_a_follower_only_profile_is_a_german_refusal(self):
        # Contradiction: the profile has no leader to re-arm. Refuse loudly
        # rather than silently write a both-arms .env (GUI twin).
        for profile in ("omx_follower", "edu6_studio"):
            with self.subTest(profile=profile):
                with self.assertRaises(ValueError) as ctx:
                    generate_env_file(_both_arms(), self.path,
                                      robot_type=profile, follower_only=False)
                msg = str(ctx.exception)
                self.assertIn("Robotertyp", msg)
                self.assertIn(profile, msg)
                self.assertIn("Leader-Betrieb", msg)

    def test_the_refusal_message_matches_the_windows_twin(self):
        # Same wording on both platforms — a student comparing a Pi rig with a
        # Windows rig must not get two different explanations of one refusal.
        from gui.app import config_generator as win_cg
        from gui.app.device_manager import (
            ArmDevice as WinArm, CameraDevice as WinCam, HardwareConfig as WinHW,
        )
        win_hw = WinHW(
            leader=WinArm(busid="1-1", serial_path="COM3", role="leader",
                          description="leader"),
            follower=WinArm(busid="1-2", serial_path="COM4", role="follower",
                            description="follower"),
            cameras=[WinCam(path="0", name="cam", role="scene")],
        )
        with self.assertRaises(ValueError) as pi_ctx:
            generate_env_file(_both_arms(), self.path,
                              robot_type="edu6_studio", follower_only=False)
        with self.assertRaises(ValueError) as win_ctx:
            win_cg.generate_env_file(win_hw, self.path,
                                     robot_type="edu6_studio", follower_only=False)
        self.assertEqual(str(pi_ctx.exception), str(win_ctx.exception))

    def test_an_unknown_robot_type_falls_back_to_the_default_profile(self):
        # The documented one-variable rollback: a typo'd/hand-edited id must
        # keep today's OMX behaviour, never raise and never write itself back.
        content = generate_env_file(_both_arms(), self.path,
                                    robot_type="nonsense_profile")
        self.assertIn(
            f"EDUBOTICS_ROBOT_TYPE={constants.DEFAULT_ROBOT_PROFILE}", content)
        self.assertNotIn("nonsense_profile", content)
        self.assertIn("EDUBOTICS_FOLLOWER_ONLY=0", content)

    def test_an_unknown_robot_type_is_sanitised_in_the_cloud_only_env_too(self):
        content = generate_cloud_only_env(self.path, robot_type="nonsense_profile")
        self.assertNotIn("nonsense_profile", content)
        self.assertIn(
            f"EDUBOTICS_ROBOT_TYPE={constants.DEFAULT_ROBOT_PROFILE}", content)

    def test_surrounding_whitespace_on_the_id_is_tolerated(self):
        content = generate_env_file(self._follower_only_rig(), self.path,
                                    robot_type=" edu6_studio ")
        self.assertIn("EDUBOTICS_ROBOT_TYPE=edu6_studio", content)
        self.assertIn("EDUBOTICS_FOLLOWER_ONLY=1", content)

    def test_the_derive_reads_the_registry_not_a_hardcoded_id_list(self):
        """Every follower-only profile in the registry must derive True — a new
        one added to constants.ROBOT_PROFILES has to work with no edit here."""
        for pid, row in constants.ROBOT_PROFILES.items():
            with self.subTest(profile=pid):
                cfg = _both_arms() if not row["follower_only"] else self._follower_only_rig()
                content = generate_env_file(cfg, self.path, robot_type=pid)
                expected = "1" if row["follower_only"] else "0"
                self.assertIn(f"EDUBOTICS_FOLLOWER_ONLY={expected}", content)


class TestExplicitFollowerOnlyZeroEmitIsDeliberate(_TmpEnvBase):
    """The Pi emits `EDUBOTICS_FOLLOWER_ONLY=0` EXPLICITLY where Windows omits
    the key. That divergence is load-bearing, not an oversight: the Roboter-
    Studio leader toggle regenerates exactly this key and reads it back as its
    rollback `prev_val`, and handle_cameras_roles reads it to carry the live
    session mode forward — an absent key reads as None there, not as 0."""

    def test_the_pi_emits_the_zero_and_windows_omits_it(self):
        from gui.app import config_generator as win_cg
        from gui.app.device_manager import (
            ArmDevice as WinArm, CameraDevice as WinCam, HardwareConfig as WinHW,
        )
        pi_content = generate_env_file(_both_arms(), self.path,
                                       robot_type="omx_full")
        self.assertIn("EDUBOTICS_FOLLOWER_ONLY=0", pi_content)

        win_path = self.path + ".win"
        self.addCleanup(lambda: os.path.exists(win_path) and os.unlink(win_path))
        win_content = win_cg.generate_env_file(
            WinHW(leader=WinArm(busid="1-1", serial_path="COM3", role="leader",
                                description="leader"),
                  follower=WinArm(busid="1-2", serial_path="COM4",
                                  role="follower", description="follower"),
                  cameras=[WinCam(path="0", name="cam", role="scene")]),
            win_path, robot_type="omx_full")
        self.assertNotIn("EDUBOTICS_FOLLOWER_ONLY", win_content)

    def test_it_survives_the_derive(self):
        # The derive path (follower_only=None) must reach the same emit as the
        # old explicit False did — otherwise the toggle loses its anchor key.
        content = generate_env_file(_both_arms(), self.path, robot_type="omx_full",
                                    follower_only=None)
        self.assertIn("EDUBOTICS_FOLLOWER_ONLY=0", content)
        self.assertEqual(content.count("EDUBOTICS_FOLLOWER_ONLY="), 1)


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

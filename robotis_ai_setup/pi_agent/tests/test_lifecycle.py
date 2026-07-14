"""Deps-free lifecycle tests for pi_agent.agent.

Covers the two-tier / never-`compose down` lifecycle rules as seen from the
AGENT layer (docker_manager's own command construction is covered in
test_docker_manager):

  - the agent delegates every teardown to docker_manager's TARGETED stop /
    factory_reset (which are `stop` + `rm -f` only) — it NEVER issues a
    `compose down`, and its source carries no `down` command literal
  - scan-arms frees the Dynamixel bus first, fast-rehydrates on revisit, and
    persists both arms' by-id paths to the managed .env
  - „Umgebung starten" regenerates a BOTH-arms .env and starts the robot tier
  - the Roboter-Studio leader toggle delegates to set_leader_mode under a busy
    lock; factory-reset requires a double confirmation

Mirrors the sibling pi_agent tests' import convention.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SETUP_DIR = Path(__file__).resolve().parents[2]  # robotis_ai_setup/
sys.path.insert(0, str(SETUP_DIR))

from pi_agent import agent  # noqa: E402
from pi_agent import config_generator as cg  # noqa: E402
from pi_agent.config_generator import ArmDevice, CameraDevice, HardwareConfig  # noqa: E402


class _EnvTempBase(unittest.TestCase):
    """Give the app a real temp .env and a fixed ROS_DOMAIN so generate_env_file
    is deterministic and never touches /var/lib."""

    def setUp(self):
        self._prev_domain = os.environ.get("EDUBOTICS_ROS_DOMAIN")
        os.environ["EDUBOTICS_ROS_DOMAIN"] = "30"
        fd, self.env_path = tempfile.mkstemp(suffix=".env")
        os.close(fd)
        os.unlink(self.env_path)  # start with no file (fresh Pi)
        self.app = agent.AgentApp(env_file=self.env_path)

    def tearDown(self):
        if self._prev_domain is None:
            os.environ.pop("EDUBOTICS_ROS_DOMAIN", None)
        else:
            os.environ["EDUBOTICS_ROS_DOMAIN"] = self._prev_domain
        for p in (self.env_path, self.env_path + ".tmp"):
            try:
                os.unlink(p)
            except OSError:
                pass


# ── scan-arms ────────────────────────────────────────────────────────────────


class TestScanArms(_EnvTempBase):
    def _leader(self):
        return ArmDevice(serial_path="/dev/serial/by-id/usb-ROBOTIS_LEADER", role="leader")

    def _follower(self):
        return ArmDevice(serial_path="/dev/serial/by-id/usb-ROBOTIS_FOLLOWER", role="follower")

    def test_scan_frees_bus_and_persists_both_arms(self):
        with patch.object(agent.docker_manager, "ensure_environment_stopped") as ensure, \
             patch.object(agent.identify_arm, "scan_and_identify_arms",
                          return_value=(self._leader(), self._follower())) as scan:
            code, payload = self.app.handle_scan_arms({})
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        # The bus is freed (robot tier stopped) BEFORE the scan.
        ensure.assert_called_once()
        scan.assert_called_once()
        # Both by-id paths persisted as managed keys, both-arms mode.
        self.assertEqual(cg.read_env_var("FOLLOWER_PORT", self.env_path),
                         "/dev/serial/by-id/usb-ROBOTIS_FOLLOWER")
        self.assertEqual(cg.read_env_var("LEADER_PORT", self.env_path),
                         "/dev/serial/by-id/usb-ROBOTIS_LEADER")
        self.assertEqual(cg.read_env_var("EDUBOTICS_FOLLOWER_ONLY", self.env_path), "0")

    def test_fast_rehydrate_on_revisit_skips_full_scan(self):
        # Seed a both-arms .env so the saved paths exist.
        cg.generate_env_file(
            HardwareConfig(leader=self._leader(), follower=self._follower()),
            self.env_path, follower_only=False)
        with patch.object(agent.docker_manager, "ensure_environment_stopped"), \
             patch.object(agent.identify_arm, "fast_rehydrate_arms",
                          return_value=(self._leader(), self._follower())) as fast, \
             patch.object(agent.identify_arm, "scan_and_identify_arms") as full:
            code, payload = self.app.handle_scan_arms({})
        self.assertEqual(code, 200)
        fast.assert_called_once()
        full.assert_not_called()  # revisit takes the fast path

    def test_force_skips_rehydrate(self):
        cg.generate_env_file(
            HardwareConfig(leader=self._leader(), follower=self._follower()),
            self.env_path, follower_only=False)
        with patch.object(agent.docker_manager, "ensure_environment_stopped"), \
             patch.object(agent.identify_arm, "fast_rehydrate_arms") as fast, \
             patch.object(agent.identify_arm, "scan_and_identify_arms",
                          return_value=(self._leader(), self._follower())) as full:
            code, _ = self.app.handle_scan_arms({"force": True})
        self.assertEqual(code, 200)
        fast.assert_not_called()
        full.assert_called_once()

    def test_only_follower_found_is_409(self):
        with patch.object(agent.docker_manager, "ensure_environment_stopped"), \
             patch.object(agent.identify_arm, "scan_and_identify_arms",
                          return_value=(None, self._follower())):
            code, payload = self.app.handle_scan_arms({})
        self.assertEqual(code, 409)
        self.assertIn("Leader", payload["message"])

    def test_no_arms_found_is_404(self):
        with patch.object(agent.docker_manager, "ensure_environment_stopped"), \
             patch.object(agent.identify_arm, "scan_and_identify_arms",
                          return_value=(None, None)):
            code, payload = self.app.handle_scan_arms({})
        self.assertEqual(code, 404)


# ── camera roles ─────────────────────────────────────────────────────────────


class TestCameraRoles(_EnvTempBase):
    def test_roles_assigned_and_persisted_with_arms(self):
        self.app._hardware = HardwareConfig(
            leader=ArmDevice(serial_path="/dev/l"),
            follower=ArmDevice(serial_path="/dev/f"))
        code, payload = self.app.handle_cameras_roles({
            "cameras": [{"path": "/dev/video0", "role": "gripper"},
                        {"path": "/dev/video2", "role": "scene"}]})
        self.assertEqual(code, 200)
        self.assertEqual(cg.read_env_var("CAMERA_NAME_1", self.env_path), "gripper")
        self.assertEqual(cg.read_env_var("CAMERA_DEVICE_2", self.env_path), "/dev/video2")

    def test_invalid_role_rejected(self):
        code, payload = self.app.handle_cameras_roles({
            "cameras": [{"path": "/dev/video0", "role": "bogus"}]})
        self.assertEqual(code, 400)


# ── environment start / stop (targeted, never down) ──────────────────────────


class TestEnvironmentLifecycle(_EnvTempBase):
    def test_start_requires_both_arms(self):
        self.app._hardware = HardwareConfig(follower=ArmDevice(serial_path="/dev/f"))  # no leader
        with patch.object(agent.docker_manager, "start_robot_tier") as start:
            code, payload = self.app.handle_environment_start({})
        self.assertEqual(code, 400)
        start.assert_not_called()

    def test_start_regenerates_both_arms_env_and_starts_tier(self):
        self.app._hardware = HardwareConfig(
            leader=ArmDevice(serial_path="/dev/l"),
            follower=ArmDevice(serial_path="/dev/f"),
            cameras=[CameraDevice(path="/dev/video0", role="gripper")])
        with patch.object(agent.docker_manager, "start_robot_tier", return_value=True) as start:
            code, payload = self.app.handle_environment_start({})
        self.assertEqual(code, 200)
        start.assert_called_once()
        # Env-start ALWAYS regenerates both-arms (FOLLOWER_ONLY=0, LEADER_PORT set).
        self.assertEqual(cg.read_env_var("EDUBOTICS_FOLLOWER_ONLY", self.env_path), "0")
        self.assertEqual(cg.read_env_var("LEADER_PORT", self.env_path), "/dev/l")

    def test_cloud_only_start_is_noop(self):
        with patch.object(agent.docker_manager, "start_robot_tier") as start:
            code, payload = self.app.handle_environment_start({"cloud_only": True})
        self.assertEqual(code, 200)
        start.assert_not_called()

    def test_stop_uses_targeted_stop_robot_tier(self):
        with patch.object(agent.docker_manager, "stop_robot_tier", return_value=True) as stop:
            code, payload = self.app.handle_environment_stop()
        self.assertEqual(code, 200)
        stop.assert_called_once()


# ── Roboter-Studio leader toggle ─────────────────────────────────────────────


class TestRoboterStudio(_EnvTempBase):
    def test_leader_disable_delegates_to_set_leader_mode(self):
        self.app._hardware = HardwareConfig(
            leader=ArmDevice(serial_path="/dev/l"),
            follower=ArmDevice(serial_path="/dev/f"))
        with patch.object(agent.docker_manager, "set_leader_mode",
                          return_value=(True, "Roboter Studio bereit.")) as slm:
            code, payload = self.app.handle_rs_set_mode(True)
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        slm.assert_called_once()
        # Delegates with the agent's scanned hardware + follower_only=True.
        args = slm.call_args[0]
        self.assertIs(args[0], self.app._hardware)
        self.assertTrue(args[1])

    def test_concurrent_switch_is_409(self):
        self.app._rs_busy = True  # a switch is already in flight
        with patch.object(agent.docker_manager, "set_leader_mode") as slm:
            code, payload = self.app.handle_rs_set_mode(False)
        self.assertEqual(code, 409)
        slm.assert_not_called()

    def test_rs_status_shape(self):
        cg.upsert_env_var("EDUBOTICS_FOLLOWER_ONLY", "1", self.env_path)
        with patch.object(agent.docker_manager, "get_container_status",
                          return_value={"open_manipulator": "running"}):
            code, payload = self.app.handle_rs_status()
        self.assertEqual(code, 200)
        self.assertTrue(payload["follower_only"])
        self.assertIn("busy", payload)
        self.assertIn("ready", payload)


# ── factory reset (double-confirm) ───────────────────────────────────────────


class TestFactoryReset(_EnvTempBase):
    def test_single_confirm_refused(self):
        with patch.object(agent.docker_manager, "factory_reset") as fr:
            code, payload = self.app.handle_factory_reset({"confirm": True})
        self.assertEqual(code, 400)
        fr.assert_not_called()

    def test_double_confirm_wipes(self):
        with patch.object(agent.docker_manager, "factory_reset",
                          return_value=(True, "2 Volumes gelöscht.")) as fr:
            code, payload = self.app.handle_factory_reset(
                {"confirm": True, "confirm_again": True})
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        fr.assert_called_once()


# ── the never-`compose down` invariant (agent layer) ─────────────────────────


class TestNeverComposeDown(unittest.TestCase):
    def test_agent_source_has_no_compose_down_literal(self):
        src = (SETUP_DIR / "pi_agent" / "agent.py").read_text(encoding="utf-8")
        # The agent must never construct a compose `down`. (Comments/docstrings
        # mention it descriptively; a real command arg would be a quoted token.)
        self.assertNotIn('"down"', src)
        self.assertNotIn("'down'", src)

    def test_docker_manager_exposes_no_down_helper(self):
        # There is no `down`/`compose_down` the agent could call — the whole
        # module is stop + rm -f only.
        down_like = [n for n in dir(agent.docker_manager)
                     if callable(getattr(agent.docker_manager, n))
                     and "down" in n.lower()]
        self.assertEqual(down_like, [])

    def test_teardown_paths_only_use_targeted_helpers(self):
        # Spy every docker_manager callable; drive both teardown handlers and
        # assert only the TARGETED helpers were touched (no surprise call).
        app = agent.AgentApp()
        called = set()

        real = {n: getattr(agent.docker_manager, n) for n in dir(agent.docker_manager)}
        spies = {}
        for name, obj in real.items():
            if callable(obj) and not name.startswith("__"):
                spies[name] = patch.object(
                    agent.docker_manager, name,
                    MagicMock(side_effect=lambda *a, _n=name, **k: called.add(_n) or
                              ((True, "ok") if _n == "factory_reset" else True)))
        for p in spies.values():
            p.start()
        try:
            app.handle_environment_stop()
            app.handle_factory_reset({"confirm": True, "confirm_again": True})
        finally:
            for p in spies.values():
                p.stop()
        # Only the sanctioned teardown helpers ran; none named with 'down'.
        self.assertIn("stop_robot_tier", called)
        self.assertIn("factory_reset", called)
        self.assertFalse(any("down" in n.lower() for n in called))


if __name__ == "__main__":
    unittest.main()

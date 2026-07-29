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


class TestScanArmFamily(_EnvTempBase):
    """The scan's arm family comes from the on-disk managed EDUBOTICS_ROBOT_TYPE
    (the WP-3 bridge). It scopes the /dev/serial/by-id filter AND selects the
    in-container prober's protocol — an edu6 rig scanned as `omx` is invisible.
    """

    def _edu6_arm(self):
        return ArmDevice(
            serial_path="/dev/serial/by-id/usb-1a86_USB_Single_Serial_5A68010132-if00",
            role="follower")

    def _scan_with_robot_type(self, robot_type, result=(None, None)):
        if robot_type is not None:
            cg.upsert_env_var("EDUBOTICS_ROBOT_TYPE", robot_type, self.env_path)
        with patch.object(agent.docker_manager, "ensure_environment_stopped"), \
             patch.object(agent.identify_arm, "scan_and_identify_arms",
                          return_value=result) as scan:
            code, payload = self.app.handle_scan_arms({})
        return code, payload, scan

    def test_an_edu6_env_scans_the_edu6_family(self):
        _, _, scan = self._scan_with_robot_type("edu6_studio")
        self.assertEqual(scan.call_args.kwargs.get("arm_family"), "edu6")

    def test_a_default_env_scans_the_omx_family(self):
        _, _, scan = self._scan_with_robot_type(None)
        self.assertEqual(scan.call_args.kwargs.get("arm_family"), "omx")

    def test_omx_follower_still_scans_the_omx_family(self):
        _, _, scan = self._scan_with_robot_type("omx_follower")
        self.assertEqual(scan.call_args.kwargs.get("arm_family"), "omx")

    def test_an_unknown_robot_type_keeps_scanning_for_omx(self):
        _, _, scan = self._scan_with_robot_type("kein_solcher_typ")
        self.assertEqual(scan.call_args.kwargs.get("arm_family"), "omx")

    def test_the_single_edu6_arm_is_reported_in_the_follower_slot(self):
        arm = self._edu6_arm()
        code, payload, _ = self._scan_with_robot_type("edu6_studio", (None, arm))
        # WP-2 stops here on purpose: the leader-less scan is still a 409 and
        # „Umgebung starten" still demands both arms — that is WP-3/WP-5.
        self.assertEqual(code, 409)
        self.assertEqual(payload["follower"], arm.serial_path)
        self.assertIsNone(payload["leader"])
        self.assertIs(self.app._hardware.follower, arm)
        self.assertIsNone(self.app._hardware.leader)

    def test_the_rehydrate_path_carries_the_family_too(self):
        # Seed a BOTH-ports .env (the fast-rehydrate path only runs when both
        # LEADER_PORT and FOLLOWER_PORT are saved), then re-type the rig — the
        # shape a rig has right after POST /robot-type but before a rescan.
        # Generating directly with robot_type='edu6_studio' would derive
        # follower_only=True and omit LEADER_PORT, so the rehydrate would never
        # be reached.
        cg.generate_env_file(
            HardwareConfig(
                leader=ArmDevice(serial_path="/dev/serial/by-id/usb-A", role="leader"),
                follower=ArmDevice(serial_path="/dev/serial/by-id/usb-B", role="follower")),
            self.env_path, robot_type="omx_full")
        cg.upsert_env_var("EDUBOTICS_ROBOT_TYPE", "edu6_studio", self.env_path,
                          quote=False)
        with patch.object(agent.docker_manager, "ensure_environment_stopped"), \
             patch.object(agent.identify_arm, "fast_rehydrate_arms",
                          return_value=(None, None)) as fast, \
             patch.object(agent.identify_arm, "scan_and_identify_arms",
                          return_value=(None, None)):
            self.app.handle_scan_arms({})
        self.assertEqual(fast.call_args.kwargs.get("arm_family"), "edu6")


class TestScanNotice(_EnvTempBase):
    """A failed scan carries the one-sentence German diagnosis of the most
    likely setup mistake instead of the generic „Kein Arm gefunden"."""

    def _scan_leaving_notice(self, notice, result=(None, None)):
        def _fake_scan(image, arm_family="omx"):
            agent.identify_arm.LAST_SCAN_NOTICE = notice
            return result

        prev = agent.identify_arm.LAST_SCAN_NOTICE
        self.addCleanup(setattr, agent.identify_arm, "LAST_SCAN_NOTICE", prev)
        with patch.object(agent.docker_manager, "ensure_environment_stopped"), \
             patch.object(agent.identify_arm, "scan_and_identify_arms",
                          side_effect=_fake_scan):
            return self.app.handle_scan_arms({})

    def test_the_notice_replaces_the_generic_message(self):
        text = ("Der Arm wurde gefunden, aber kein Servo antwortet — ist das "
                "12-V-Netzteil des Arms eingesteckt und eingeschaltet?")
        code, payload = self._scan_leaving_notice(text)
        self.assertEqual(code, 404)
        self.assertEqual(payload["notice"], text)
        self.assertEqual(payload["message"], text)
        self.assertNotIn("Kein Arm gefunden", payload["message"])

    def test_no_notice_keeps_the_generic_message(self):
        code, payload = self._scan_leaving_notice("")
        self.assertEqual(code, 404)
        self.assertEqual(payload["notice"], "")
        self.assertIn("Kein Arm gefunden", payload["message"])

    def test_the_notice_is_written_to_the_protokoll(self):
        """`_log` feeds the redacted Protokoll ring the System tab streams — the
        diagnosis must survive there even if the caller drops the response."""
        text = "Nur 3 von 7 Servos antworten — bitte die Steckverbindungen prüfen."
        with patch.object(agent, "logger") as log:
            self._scan_leaving_notice(text)
        self.assertIn(text, [c.args[0] for c in log.info.call_args_list])

    def test_a_notice_about_some_other_port_never_rides_a_SUCCESS(self):
        """Drives the REAL scanner, not a stub — the clearing lives inside
        `scan_and_identify_arms` (only it knows whether a family has a leader),
        and this is the seam that proves the agent surfaces the result of that
        decision rather than a value of its own.

        Scenario: a stray CH34x dongle sorts before the real edu6 arm, answers
        no Feetech ping and sets the 12-V sentence; the arm then identifies
        fine. Reporting that sends the student hunting a working power supply.
        """
        DONGLE = "/dev/serial/by-id/usb-1a86_USB2.0-Serial-if00"
        ARM = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5A68-if00"
        cg.upsert_env_var("EDUBOTICS_ROBOT_TYPE", "edu6_studio", self.env_path)

        def run(argv, **kw):
            if argv[:2] == ["docker", "exec"]:
                silent = DONGLE in argv[5]
                return MagicMock(stdout="feetech_silent\n" if silent else "edu6\n",
                                 stderr="", returncode=1 if silent else 0)
            return MagicMock(stdout="cid", stderr="", returncode=0)

        ia = agent.identify_arm
        prev = ia.LAST_SCAN_NOTICE
        self.addCleanup(setattr, ia, "LAST_SCAN_NOTICE", prev)
        with patch.object(agent.docker_manager, "ensure_environment_stopped"), \
             patch.object(ia, "list_serial_by_id", return_value=[DONGLE, ARM]), \
             patch.object(ia.subprocess, "run", side_effect=run), \
             patch.object(ia.time, "sleep"):
            code, payload = self.app.handle_scan_arms({"force": True})

        self.assertEqual(payload["follower"], ARM)
        self.assertEqual(payload["notice"], "")
        self.assertNotIn("12-V", payload["message"])

    def test_the_notice_is_kept_on_a_partial_scan(self):
        """A half-found rig is exactly where the diagnosis is still relevant."""
        follower = ArmDevice(serial_path="/dev/serial/by-id/usb-ROBOTIS_F", role="follower")
        text = "Nur 3 von 7 Servos antworten — bitte die Steckverbindungen prüfen."
        code, payload = self._scan_leaving_notice(text, result=(None, follower))
        self.assertEqual(code, 409)
        self.assertEqual(payload["notice"], text)

    def test_a_stale_notice_never_rides_a_fast_rehydrate(self):
        """fast_rehydrate does not run a scan, so LAST_SCAN_NOTICE there is a
        sentence about some EARLIER attempt — reporting it would blame a rig
        that just succeeded."""
        leader = ArmDevice(serial_path="/dev/serial/by-id/usb-ROBOTIS_L", role="leader")
        follower = ArmDevice(serial_path="/dev/serial/by-id/usb-ROBOTIS_F", role="follower")
        cg.generate_env_file(HardwareConfig(leader=leader, follower=follower),
                             self.env_path, follower_only=False)
        prev = agent.identify_arm.LAST_SCAN_NOTICE
        self.addCleanup(setattr, agent.identify_arm, "LAST_SCAN_NOTICE", prev)
        agent.identify_arm.LAST_SCAN_NOTICE = "eine alte Meldung"
        with patch.object(agent.docker_manager, "ensure_environment_stopped"), \
             patch.object(agent.identify_arm, "fast_rehydrate_arms",
                          return_value=(leader, follower)):
            code, payload = self.app.handle_scan_arms({})
        self.assertEqual(code, 200)
        self.assertEqual(payload["notice"], "")


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

    def test_roles_preserve_live_follower_only_mode(self):
        # A camera-role edit during a live follower-only (Roboter Studio)
        # session must NOT silently rewrite EDUBOTICS_FOLLOWER_ONLY=0 — that
        # would re-arm the never-scanned leader on the next container recreate.
        cg.generate_env_file(
            HardwareConfig(follower=ArmDevice(serial_path="/dev/f")),
            self.env_path, follower_only=True)
        self.app.rehydrate_hardware()
        code, _ = self.app.handle_cameras_roles({
            "cameras": [{"path": "/dev/video0", "role": "scene"}]})
        self.assertEqual(code, 200)
        self.assertEqual(cg.read_env_var("EDUBOTICS_FOLLOWER_ONLY", self.env_path), "1")
        self.assertIsNone(cg.read_env_var("LEADER_PORT", self.env_path))
        # The roles landed regardless of the preserved mode.
        self.assertEqual(cg.read_env_var("CAMERA_NAME_1", self.env_path), "scene")

    def test_roles_preserve_both_arms_mode(self):
        # The inverse: a both-arms .env stays both-arms after a role edit.
        cg.generate_env_file(
            HardwareConfig(leader=ArmDevice(serial_path="/dev/l"),
                           follower=ArmDevice(serial_path="/dev/f")),
            self.env_path, follower_only=False)
        self.app.rehydrate_hardware()
        code, _ = self.app.handle_cameras_roles({
            "cameras": [{"path": "/dev/video0", "role": "gripper"}]})
        self.assertEqual(code, 200)
        self.assertEqual(cg.read_env_var("EDUBOTICS_FOLLOWER_ONLY", self.env_path), "0")
        self.assertEqual(cg.read_env_var("LEADER_PORT", self.env_path), "/dev/l")

    def test_roles_preserve_robot_type(self):
        # EDUBOTICS_ROBOT_TYPE is MANAGED — the role-edit regenerate must carry
        # the on-disk value through instead of resetting it to the default.
        cg.generate_env_file(
            HardwareConfig(follower=ArmDevice(serial_path="/dev/f")),
            self.env_path, follower_only=True, robot_type="omx_follower")
        self.app.rehydrate_hardware()
        code, _ = self.app.handle_cameras_roles({
            "cameras": [{"path": "/dev/video0", "role": "scene"}]})
        self.assertEqual(code, 200)
        self.assertEqual(cg.read_env_var("EDUBOTICS_ROBOT_TYPE", self.env_path),
                         "omx_follower")


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

    def test_start_preserves_robot_type(self):
        # Env-start regenerates the .env — the managed EDUBOTICS_ROBOT_TYPE
        # must be carried through, not reset to the default.
        self.app._hardware = HardwareConfig(
            leader=ArmDevice(serial_path="/dev/l"),
            follower=ArmDevice(serial_path="/dev/f"))
        # Seed via the DERIVE: omx_follower is the only non-default id (so the
        # only one that can prove "carried, not defaulted") and it is
        # follower-only, which an explicit follower_only=False now contradicts.
        cg.generate_env_file(self.app._hardware, self.env_path,
                             robot_type="omx_follower")
        with patch.object(agent.docker_manager, "start_robot_tier", return_value=True):
            code, _ = self.app.handle_environment_start({})
        self.assertEqual(code, 200)
        self.assertEqual(cg.read_env_var("EDUBOTICS_ROBOT_TYPE", self.env_path),
                         "omx_follower")


# ── update-in-flight fast-fail (503, never a silent block) ───────────────────


class TestUpdateBusyFastFail(_EnvTempBase):
    """While an update job holds _lifecycle_lock for minutes, every mutating
    lifecycle endpoint must fast-fail 503 with a distinguishing German message
    instead of silently queueing behind the update."""

    def setUp(self):
        super().setUp()
        with self.app._update_lock:
            self.app._update_busy = True

    def _assert_busy(self, code, payload):
        self.assertEqual(code, 503)
        self.assertIn("Aktualisierung läuft", payload["message"])

    def test_scan_arms_fast_fails(self):
        with patch.object(agent.docker_manager, "ensure_environment_stopped") as ensure, \
             patch.object(agent.identify_arm, "scan_and_identify_arms") as scan:
            code, payload = self.app.handle_scan_arms({})
        self._assert_busy(code, payload)
        ensure.assert_not_called()
        scan.assert_not_called()

    def test_environment_start_fast_fails(self):
        self.app._hardware = HardwareConfig(
            leader=ArmDevice(serial_path="/dev/l"),
            follower=ArmDevice(serial_path="/dev/f"))
        with patch.object(agent.docker_manager, "start_robot_tier") as start:
            code, payload = self.app.handle_environment_start({})
        self._assert_busy(code, payload)
        start.assert_not_called()

    def test_environment_stop_fast_fails(self):
        with patch.object(agent.docker_manager, "stop_robot_tier") as stop:
            code, payload = self.app.handle_environment_stop()
        self._assert_busy(code, payload)
        stop.assert_not_called()

    def test_factory_reset_fast_fails(self):
        with patch.object(agent.docker_manager, "factory_reset") as fr:
            code, payload = self.app.handle_factory_reset(
                {"confirm": True, "confirm_again": True})
        self._assert_busy(code, payload)
        fr.assert_not_called()

    def test_cameras_roles_fast_fails(self):
        code, payload = self.app.handle_cameras_roles(
            {"cameras": [{"path": "/dev/video0", "role": "scene"}]})
        self._assert_busy(code, payload)

    def test_hf_token_fast_fails(self):
        code, payload = self.app.handle_hf_token({"token": "hf_x"})
        self._assert_busy(code, payload)

    def test_read_only_endpoints_stay_available(self):
        # /status and /update/status must keep answering during the update.
        with patch.object(agent.docker_manager, "get_container_status", return_value={}):
            code, _ = self.app.handle_status()
        self.assertEqual(code, 200)
        code, _ = self.app.handle_update_status("nope")
        self.assertEqual(code, 404)  # not 503 — read path untouched


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

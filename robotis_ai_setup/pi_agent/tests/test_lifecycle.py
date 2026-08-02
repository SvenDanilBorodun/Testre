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
from pi_agent.constants import (  # noqa: E402
    CAMERA_ROLE_LABELS_DE,
    ROBOT_PROFILES,
)


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
        # WP-5: a leader-less profile SUCCEEDS with one arm. Until then this was
        # a 409 whose German named a Leader-Arm the rig does not have.
        self.assertEqual(code, 200)
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


class TestLeaderLessScanGating(_EnvTempBase):
    """WP-5 — a profile with ``scan_requires_leader=False`` succeeds with ONE
    arm, and never mentions a Leader-Arm it does not have.

    The gate is the PROFILE, not the arm family: ``omx_follower`` shares the
    two-arm ``omx`` family, and it was unreachable on a Pi for exactly this
    reason long before edu6 existed.
    """

    LEADER = "/dev/serial/by-id/usb-ROBOTIS_LEADER"
    FOLLOWER = "/dev/serial/by-id/usb-ROBOTIS_FOLLOWER"

    def _arm(self, path, role):
        return ArmDevice(serial_path=path, role=role)

    def _scan(self, robot_type, result, **kw):
        cg.upsert_env_var("EDUBOTICS_ROBOT_TYPE", robot_type, self.env_path,
                          quote=False)
        # Patching out scan_and_identify_arms also removes the ONE place that
        # clears the module-global LAST_SCAN_NOTICE (identify_arm.py: `global
        # LAST_SCAN_NOTICE; LAST_SCAN_NOTICE = ""` on entry). The 404 message
        # is `notice or "Kein Arm gefunden …"`, so a notice left behind by ANY
        # earlier test in the process would silently become the message these
        # tests assert on — an order-dependent failure that whole-suite
        # collection order happens to hide today. Reproduce the real function's
        # side effect instead of inheriting whoever ran last.
        agent.identify_arm.LAST_SCAN_NOTICE = ""
        with patch.object(agent.docker_manager, "ensure_environment_stopped"), \
             patch.object(agent.identify_arm, "scan_and_identify_arms",
                          return_value=result):
            return self.app.handle_scan_arms(kw)

    # ── the leader-less success path ────────────────────────────────────────

    def test_edu6_succeeds_with_one_arm(self):
        arm = self._arm("/dev/serial/by-id/usb-1a86_x", "follower")
        code, payload = self._scan("edu6_studio", (None, arm))
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])

    def test_omx_follower_succeeds_with_one_arm_too(self):
        # The pre-existing parity gap, independent of edu6.
        arm = self._arm(self.FOLLOWER, "follower")
        code, payload = self._scan("omx_follower", (None, arm))
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])

    def test_the_success_message_never_promises_both_arms(self):
        arm = self._arm(self.FOLLOWER, "follower")
        _, payload = self._scan("omx_follower", (None, arm))
        self.assertEqual(payload["message"], "Roboterarm erkannt und gespeichert.")
        self.assertNotIn("Beide Arme", payload["message"])

    def test_a_stray_second_arm_does_not_resurrect_the_both_arms_wording(self):
        # An omx_follower rig can physically have two arms plugged in; the
        # generated .env still has no LEADER_PORT, so the message must not
        # claim otherwise.
        code, payload = self._scan(
            "omx_follower",
            (self._arm(self.LEADER, "leader"), self._arm(self.FOLLOWER, "follower")))
        self.assertEqual(code, 200)
        self.assertEqual(payload["message"], "Roboterarm erkannt und gespeichert.")

    def test_the_persisted_env_is_leader_less_and_follower_only(self):
        arm = self._arm(self.FOLLOWER, "follower")
        self._scan("omx_follower", (None, arm))
        self.assertEqual(cg.read_env_var("FOLLOWER_PORT", self.env_path), self.FOLLOWER)
        self.assertIsNone(cg.read_env_var("LEADER_PORT", self.env_path))
        self.assertEqual(cg.read_env_var("EDUBOTICS_FOLLOWER_ONLY", self.env_path), "1")

    # ── the 409 wording this closes ─────────────────────────────────────────

    # The 409 message that fires here DIAGNOSES the wrong arm; it must never
    # collapse into the „nothing found" wording, because the scan SUCCEEDED —
    # sending that student to check a USB cable is a false lead.
    WRONG_ARM = ("Es wurde der Leader-Arm erkannt — dieser Robotertyp braucht "
                 "den Follower-Arm. Bitte den Follower-Arm anschließen.")
    NOTHING_FOUND = ("Kein Arm gefunden — USB-Verbindung und Stromversorgung "
                     "der Arme prüfen.")

    def test_a_wrong_arm_on_a_leader_less_rig_is_diagnosed_not_blamed_on_usb(self):
        # THE reachable state: `omx_follower` shares the two-arm `omx` family,
        # so its dxl scan really can identify a leader while the follower is
        # unplugged. `leader is not None` is guaranteed here — the both-None
        # case returns 404 one branch earlier.
        code, payload = self._scan(
            "omx_follower", (self._arm(self.LEADER, "leader"), None))
        self.assertEqual(code, 409)
        self.assertEqual(payload["message"], self.WRONG_ARM)

    def test_the_wrong_arm_message_is_not_the_nothing_found_message(self):
        # The two states are distinct and must stay distinct: one means „you
        # plugged in the wrong arm", the other „no arm answered at all".
        _, wrong = self._scan(
            "omx_follower", (self._arm(self.LEADER, "leader"), None))
        _, nothing = self._scan("omx_follower", (None, None))
        self.assertEqual(wrong["message"], self.WRONG_ARM)
        self.assertEqual(nothing["message"], self.NOTHING_FOUND)
        self.assertNotEqual(wrong["message"], nothing["message"])

    def test_the_wrong_arm_message_never_asks_for_a_leader_arm(self):
        # It NAMES the leader (accurate — one was found) but must not send a
        # leader-less rig off to connect one: the ask is the FOLLOWER.
        _, payload = self._scan(
            "omx_follower", (self._arm(self.LEADER, "leader"), None))
        self.assertIn("Follower-Arm anschließen", payload["message"])
        self.assertNotIn("Leader-Arm anschließen", payload["message"])
        self.assertNotIn("Leader-Arm fehlt", payload["message"])

    def test_the_wrong_arm_message_does_not_claim_nothing_was_detected(self):
        # The exact regression: an arm WAS detected, so „wurde nicht erkannt"
        # (and any bare „USB prüfen" hunt) is a false lead.
        _, payload = self._scan(
            "omx_follower", (self._arm(self.LEADER, "leader"), None))
        self.assertNotIn("nicht erkannt", payload["message"])
        self.assertNotIn("USB prüfen", payload["message"])

    def test_edu6_takes_the_same_branch_defensively(self):
        # NOT physically reachable: the edu6 family scans with protocol
        # 'feetech', whose prober can never return the role 'leader'
        # (identify_arm.scan_and_identify_arms), so a real edu6 rig with no
        # follower hits the 404 above. Pinned anyway — the branch is shared.
        code, payload = self._scan(
            "edu6_studio", (self._arm(self.LEADER, "leader"), None))
        self.assertEqual(code, 409)
        self.assertEqual(payload["message"], self.WRONG_ARM)

    # ── omx_full is untouched ───────────────────────────────────────────────

    def test_omx_full_still_409s_on_a_missing_leader(self):
        arm = self._arm(self.FOLLOWER, "follower")
        code, payload = self._scan("omx_full", (None, arm))
        self.assertEqual(code, 409)
        self.assertEqual(payload["message"],
                         "Nur der Follower-Arm wurde erkannt — der Leader-Arm "
                         "fehlt. Bitte USB prüfen.")

    def test_omx_full_still_409s_on_a_missing_follower(self):
        code, payload = self._scan(
            "omx_full", (self._arm(self.LEADER, "leader"), None))
        self.assertEqual(code, 409)
        self.assertEqual(payload["message"],
                         "Nur der Leader-Arm wurde erkannt — der Follower-Arm "
                         "fehlt. Bitte USB prüfen.")

    def test_omx_full_still_says_beide_Arme_on_success(self):
        code, payload = self._scan(
            "omx_full",
            (self._arm(self.LEADER, "leader"), self._arm(self.FOLLOWER, "follower")))
        self.assertEqual(code, 200)
        self.assertEqual(payload["message"], "Beide Arme erkannt und gespeichert.")

    def test_no_arms_at_all_is_still_404_on_every_profile(self):
        for pid in ("omx_full", "omx_follower", "edu6_studio"):
            with self.subTest(pid):
                code, _ = self._scan(pid, (None, None))
                self.assertEqual(code, 404)

    # ── the fast-rehydrate gate ─────────────────────────────────────────────

    def _rehydrate(self, robot_type, saved_leader, saved_follower, fast_result):
        if saved_leader:
            cg.upsert_env_var("LEADER_PORT", saved_leader, self.env_path)
        cg.upsert_env_var("FOLLOWER_PORT", saved_follower, self.env_path)
        cg.upsert_env_var("EDUBOTICS_ROBOT_TYPE", robot_type, self.env_path,
                          quote=False)
        with patch.object(agent.docker_manager, "ensure_environment_stopped"), \
             patch.object(agent.identify_arm, "fast_rehydrate_arms",
                          return_value=fast_result) as fast, \
             patch.object(agent.identify_arm, "scan_and_identify_arms",
                          return_value=(None, None)) as full:
            code, payload = self.app.handle_scan_arms({})
        return code, payload, fast, full

    def test_a_leader_less_rig_takes_the_fast_path_without_a_saved_leader(self):
        arm = self._arm(self.FOLLOWER, "follower")
        code, _, fast, full = self._rehydrate(
            "edu6_studio", None, self.FOLLOWER, (None, arm))
        self.assertEqual(code, 200)
        fast.assert_called_once()
        self.assertIs(fast.call_args.kwargs.get("require_leader"), False)
        # …and does NOT fall through to the slow scanner container.
        full.assert_not_called()

    def test_a_both_arms_rig_still_needs_a_saved_leader_for_the_fast_path(self):
        _, _, fast, full = self._rehydrate(
            "omx_full", None, self.FOLLOWER, (None, None))
        fast.assert_not_called()
        full.assert_called_once()

    def test_the_both_arms_fast_path_still_requires_a_leader(self):
        _, _, fast, _ = self._rehydrate(
            "omx_full", self.LEADER, self.FOLLOWER,
            (self._arm(self.LEADER, "leader"), self._arm(self.FOLLOWER, "follower")))
        self.assertIs(fast.call_args.kwargs.get("require_leader"), True)


class TestCameraRolesRequireAnExplicitRole(_EnvTempBase):
    """EVERY camera must carry an explicit ``gripper``/``scene`` role — there is
    deliberately no lone-camera auto-assign, because the only client
    (``SystemPage.js::handleSaveRoles``) filters role-less cameras OUT of the
    request, so such a default was unreachable code pretending to work.

    ``PROFILES`` is swept everywhere a default would have differed between them
    (``camera_roles[0]`` is ``gripper`` on ``omx_full`` and ``scene`` on both
    follower-only profiles). Note what catches what: a PROFILE-DERIVED default
    is caught by ``test_no_profile_supplies_a_default_role`` (the three answers
    stop agreeing); a CONSTANT default agrees across profiles and slips past
    that one, so it is the two ``…is_a_400_on_every_profile`` sweeps that catch
    it. Both kinds are covered — but only by different tests."""

    PROFILES = ("omx_full", "omx_follower", "edu6_studio")

    def _roles(self, robot_type, cameras):
        cg.upsert_env_var("EDUBOTICS_ROBOT_TYPE", robot_type, self.env_path,
                          quote=False)
        return self.app.handle_cameras_roles({"cameras": cameras})

    def test_a_role_less_lone_camera_is_a_german_400_on_every_profile(self):
        for pid in self.PROFILES:
            with self.subTest(pid):
                code, payload = self._roles(pid, [{"path": "/dev/video0"}])
                self.assertEqual(code, 400)
                self.assertIn("Ungültige Rolle", payload["message"])

    def test_an_empty_string_role_is_a_400_on_every_profile(self):
        for pid in self.PROFILES:
            with self.subTest(pid):
                code, payload = self._roles(
                    pid, [{"path": "/dev/video0", "role": ""}])
                self.assertEqual(code, 400)
                self.assertIn("Ungültige Rolle", payload["message"])

    def test_no_profile_supplies_a_default_role(self):
        # The sharp form: a role-less camera produces the IDENTICAL answer on
        # all three profiles. Any profile-derived default (`camera_roles[0]`)
        # would make omx_full and the follower-only pair disagree here.
        answers = {pid: self._roles(pid, [{"path": "/dev/video0"}])
                   for pid in self.PROFILES}
        self.assertEqual(len(set(map(repr, answers.values()))), 1, answers)

    def test_two_role_less_cameras_are_still_a_german_400(self):
        # Identical-serial Innomakers: only the live preview tells them apart,
        # so guessing which is which would silently corrupt every dataset.
        code, payload = self._roles(
            "omx_full", [{"path": "/dev/video0"}, {"path": "/dev/video1"}])
        self.assertEqual(code, 400)
        self.assertIn("Ungültige Rolle", payload["message"])

    def test_an_invalid_non_empty_role_is_still_a_400_even_when_alone(self):
        code, payload = self._roles(
            "edu6_studio", [{"path": "/dev/video0", "role": "phone"}])
        self.assertEqual(code, 400)
        self.assertIn("Ungültige Rolle", payload["message"])

    def test_every_role_a_profile_declares_is_accepted_verbatim(self):
        # The whole UI-producible surface: `handleSaveRoles` only ever sends
        # roles the profile declares, and those are byte-unchanged.
        for pid in self.PROFILES:
            for role in ROBOT_PROFILES[pid]["camera_roles"]:
                with self.subTest(pid=pid, role=role):
                    code, payload = self._roles(
                        pid, [{"path": "/dev/video0", "role": role}])
                    self.assertEqual(code, 200)
                    self.assertEqual(payload["cameras"],
                                     [{"path": "/dev/video0", "role": role}])

    def test_two_explicitly_assigned_cameras_are_unchanged(self):
        code, payload = self._roles("omx_full", [
            {"path": "/dev/video0", "role": "gripper"},
            {"path": "/dev/video1", "role": "scene"},
        ])
        self.assertEqual(code, 200)
        self.assertEqual(payload["cameras"], [
            {"path": "/dev/video0", "role": "gripper"},
            {"path": "/dev/video1", "role": "scene"},
        ])

    def test_a_path_less_entry_is_still_dropped_before_validation(self):
        # `{"path": ""}` rows never reach the role check, so a well-formed
        # camera beside one is still accepted.
        code, payload = self._roles(
            "edu6_studio", [{"path": ""},
                            {"path": "/dev/video0", "role": "scene"}])
        self.assertEqual(code, 200)
        self.assertEqual(payload["cameras"], [{"path": "/dev/video0",
                                               "role": "scene"}])

    def test_an_empty_camera_list_is_still_an_accepted_no_op(self):
        # What the UI actually POSTs when nothing is assigned — unchanged.
        for pid in self.PROFILES:
            with self.subTest(pid):
                code, payload = self._roles(pid, [])
                self.assertEqual(code, 200)
                self.assertEqual(payload["cameras"], [])


class TestCameraRoleMustSuitTheProfile(_EnvTempBase):
    """A well-formed role the SELECTED profile does not declare is a German 400.

    The scar: ``edu6_studio`` declares ONE camera topic server-side
    (``scene:/scene/image_raw/compressed``), so naming its only camera
    ``gripper`` publishes a topic nothing subscribes to — while the opi compose
    healthcheck greps ``/$${CAMERA_NAME_1:-gripper}/image_raw/compressed``,
    i.e. the topic the STUDENT just named. It therefore goes green and
    „Umgebung starten" succeeds, with the failure surfacing much later as an
    empty Roboter Studio.

    This is the SERVER half of a redundant pair (the SPA also stops offering
    the role). Neither half is sufficient: a stale cached bundle offers it
    anyway, and a hand-crafted POST bypasses the SPA entirely.

    The behavioural delta is EXACTLY one cell of the profile x role grid —
    ``test_the_delta_is_exactly_one_cell_of_the_grid`` asserts that shape
    directly, so widening or narrowing the allowlist anywhere fails here.
    """

    PROFILES = ("omx_full", "omx_follower", "edu6_studio")
    ROLES = ("gripper", "scene")

    def _roles(self, robot_type, cameras):
        cg.upsert_env_var("EDUBOTICS_ROBOT_TYPE", robot_type, self.env_path,
                          quote=False)
        return self.app.handle_cameras_roles({"cameras": cameras})

    def test_the_delta_is_exactly_one_cell_of_the_grid(self):
        # The sharp form. Accepted iff the profile declares the role — computed
        # from the registry, never restated, so a registry edit moves this test
        # with it instead of silently disagreeing.
        accepted = {
            (pid, role): self._roles(
                pid, [{"path": "/dev/video0", "role": role}])[0] == 200
            for pid in self.PROFILES for role in self.ROLES
        }
        self.assertEqual(
            accepted,
            {(pid, role): role in ROBOT_PROFILES[pid]["camera_roles"]
             for pid in self.PROFILES for role in self.ROLES})
        # …and stated absolutely, so a registry that drifted the other way
        # cannot make the comparison above vacuously true.
        self.assertEqual([k for k, v in accepted.items() if not v],
                         [("edu6_studio", "gripper")])

    def test_gripper_on_edu6_is_refused_in_german_naming_role_type_and_remedy(self):
        code, payload = self._roles(
            "edu6_studio", [{"path": "/dev/video0", "role": "gripper"}])
        self.assertEqual(code, 400)
        msg = payload["message"]
        self.assertIn("Greifer", msg)          # the role they picked
        self.assertIn("EduBotics 6-Achs", msg)  # the type that rules it out
        self.assertIn("Szene", msg)             # what to pick instead
        self.assertIn("/dev/video0", msg)       # which camera
        self.assertFalse(payload["ok"])
        # Distinct from the shape refusal — a student who picked a REAL role
        # from a REAL dropdown must not be told it is „ungültig".
        self.assertNotIn("Ungültige Rolle", msg)

    def test_a_role_of_the_wrong_shape_still_gets_the_shape_message(self):
        # Byte-unchanged: the profile gate is reached only by well-formed
        # roles, so an unknown one keeps its original wording on every profile.
        for pid in self.PROFILES:
            for role in ("phone", "", None, 0, "Scene"):
                with self.subTest(pid=pid, role=role):
                    code, payload = self._roles(
                        pid, [{"path": "/dev/video0", "role": role}])
                    self.assertEqual(code, 400)
                    self.assertEqual(
                        payload["message"],
                        "Ungültige Rolle für /dev/video0 (nur Greifer/Szene).")

    def test_nothing_is_persisted_when_a_role_is_refused(self):
        # The refusal returns BEFORE `_hardware.cameras` is replaced and before
        # the .env regenerate — a rejected save must not half-apply.
        self.app._hardware = HardwareConfig(
            follower=ArmDevice(serial_path="/dev/f"))
        code, _ = self._roles("edu6_studio", [
            {"path": "/dev/video0", "role": "scene"},
            {"path": "/dev/video2", "role": "gripper"},
        ])
        self.assertEqual(code, 400)
        self.assertEqual(self.app._hardware.cameras, [])
        self.assertIsNone(cg.read_env_var("CAMERA_NAME_1", self.env_path))

    def test_an_unreadable_robot_type_falls_back_to_the_permissive_default(self):
        # `_current_robot_type` is forgiving by design (the documented
        # one-variable rollback). A rig with no/garbage type must keep the
        # pre-change dropdown, not lose a role it was using.
        for raw in ("", "   ", "totally_unknown", "OMX_FULL"):
            with self.subTest(raw):
                cg.upsert_env_var("EDUBOTICS_ROBOT_TYPE", raw, self.env_path,
                                  quote=False)
                for role in self.ROLES:
                    code, _ = self.app.handle_cameras_roles(
                        {"cameras": [{"path": "/dev/video0", "role": role}]})
                    self.assertEqual(code, 200, (raw, role))

    def test_the_first_offending_camera_decides_and_it_is_not_reordered(self):
        # Two bad rows, different reasons: the SHAPE check runs per-camera in
        # order, so the first row's message wins even though the second row is
        # the profile-specific one.
        code, payload = self._roles("edu6_studio", [
            {"path": "/dev/videoA", "role": "phone"},
            {"path": "/dev/videoB", "role": "gripper"},
        ])
        self.assertEqual(code, 400)
        self.assertIn("/dev/videoA", payload["message"])
        self.assertNotIn("/dev/videoB", payload["message"])

    def test_every_profiles_allowlist_is_a_subset_of_the_known_roles(self):
        # A registry role outside CAMERA_ROLE_LABELS_DE could be OFFERED by the
        # SPA yet never accepted here (the shape check runs first), and would
        # render label-less in the refusal message.
        for pid, row in ROBOT_PROFILES.items():
            with self.subTest(pid):
                self.assertTrue(row["camera_roles"])
                self.assertLessEqual(set(row["camera_roles"]),
                                     set(CAMERA_ROLE_LABELS_DE))


class TestStatusShipsTheCameraRoleAllowlist(_EnvTempBase):
    """`/status.robot_profiles[].camera_roles` is what lets the SPA stop
    OFFERING a role the agent would refuse. Without it on the wire the
    dropdown structurally cannot narrow, which is how the defect survived
    three work packages: every layer was locally right."""

    def _profiles(self):
        with patch.object(agent.docker_manager, "get_container_status",
                          return_value={}):
            _, payload = self.app.handle_status()
        return {p["id"]: p for p in payload["robot_profiles"]}

    def test_every_profile_carries_its_allowlist_verbatim(self):
        rows = self._profiles()
        self.assertEqual(set(rows), set(ROBOT_PROFILES))
        for pid, row in ROBOT_PROFILES.items():
            with self.subTest(pid):
                self.assertEqual(rows[pid]["camera_roles"],
                                 list(row["camera_roles"]))

    def test_the_allowlist_is_a_json_list_not_a_tuple(self):
        # The payload is json-serialised; spelling the conversion at the
        # boundary keeps the dict honest for every in-process consumer too
        # (the tests here, and any future one that compares payloads).
        for row in self._profiles().values():
            self.assertIsInstance(row["camera_roles"], list)

    def test_order_is_carried_through_unchanged(self):
        # The GUI's lone-camera default is `camera_roles[0]`, so ORDER is not
        # decorative even though the Pi tests membership. omx_follower's
        # reversed tuple is the one that would show a silent re-sort.
        self.assertEqual(self._profiles()["omx_follower"]["camera_roles"],
                         ["scene", "gripper"])

    def test_the_pre_existing_profile_keys_are_untouched(self):
        rows = self._profiles()
        for pid, row in ROBOT_PROFILES.items():
            with self.subTest(pid):
                self.assertEqual(rows[pid]["display_de"], row["display_de"])
                self.assertEqual(rows[pid]["scan_requires_leader"],
                                 row["scan_requires_leader"])


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

    def test_a_leader_less_success_drops_a_notice_the_family_clear_cannot_see(self):
        """The scanner clears the notice against the arm FAMILY, and
        `omx_follower` shares the two-arm `omx` family — so a successful ONE-arm
        scan there kept a sentence ending „… und erneut scannen" attached to a
        SUCCESS (MEASURED with a stray CH34x device plugged in). The 409 this
        used to be was the only thing hiding it.

        Not logged either: the Protokoll is what a teacher reads when a rig
        misbehaves, and „Robotertyp wählen und erneut scannen" printed under a
        green scan is the same contradiction one line lower.
        """
        follower = ArmDevice(serial_path="/dev/serial/by-id/usb-ROBOTIS_F", role="follower")
        text = ('Es wurde ein „EduBotics 6-Achs"-Arm gefunden, aber ein '
                'OMX-Robotertyp ist ausgewählt. Bitte den Robotertyp oben '
                'passend zum angeschlossenen Arm wählen und erneut scannen.')
        cg.upsert_env_var("EDUBOTICS_ROBOT_TYPE", "omx_follower", self.env_path,
                          quote=False)
        with patch.object(agent, "logger") as log:
            code, payload = self._scan_leaving_notice(text, result=(None, follower))
        self.assertEqual(code, 200)
        self.assertEqual(payload["notice"], "")
        self.assertEqual(payload["message"], "Roboterarm erkannt und gespeichert.")
        self.assertNotIn(text, [c.args[0] for c in log.info.call_args_list])

    def test_a_both_arms_success_drops_it_too(self):
        # Byte-identical in effect on omx_full — the scanner's own clear already
        # fired there — but pinned so the agent-side rule cannot be dropped as
        # "redundant" and quietly re-open the omx_follower case above.
        leader = ArmDevice(serial_path="/dev/serial/by-id/usb-ROBOTIS_L", role="leader")
        follower = ArmDevice(serial_path="/dev/serial/by-id/usb-ROBOTIS_F", role="follower")
        code, payload = self._scan_leaving_notice("irgendeine Meldung",
                                                  result=(leader, follower))
        self.assertEqual(code, 200)
        self.assertEqual(payload["notice"], "")

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


class TestCrossFamilyInvalidatesTheScan(_EnvTempBase):
    """Changing the robot type ACROSS arm families invalidates the scan.

    MEASURED before this landed, on a real AgentApp: scan an OMX pair, then
    ``POST /robot-type {"robot_type": "edu6_studio"}`` answered
    ``200 {'hardware_ready': True}``; the .env then named a ROBOTIS OpenRB-150
    as the FOLLOWER_PORT of a Feetech rig with EDUBOTICS_FOLLOWER_ONLY=1, while
    /status still reported a leader on a profile that has none; and „Umgebung
    starten" went straight through into a ~300 s ``service_healthy`` block
    whose only student-visible outcome was the generic „Die Roboter-Umgebung
    konnte nicht gestartet werden — bitte das Protokoll prüfen" (the driver's
    real reason is in the CONTAINER's stdout, which the Protokoll ring does not
    carry).

    ``arms_identified`` deliberately keeps reporting what was scanned — it
    answers a different question — so the change is confined to
    ``hardware_ready`` and the start gate.
    """

    OMX_L = "/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_LEADER-if00"
    OMX_F = "/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_FOLLOWER-if00"
    EDU6 = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5A68010132-if00"

    def _seed_omx_pair(self):
        with patch.object(agent.docker_manager, "ensure_environment_stopped"), \
             patch.object(agent.identify_arm, "scan_and_identify_arms",
                          return_value=(ArmDevice(serial_path=self.OMX_L, role="leader"),
                                        ArmDevice(serial_path=self.OMX_F, role="follower"))):
            self.app.handle_scan_arms({})

    def _seed_edu6(self):
        cg.upsert_env_var("EDUBOTICS_ROBOT_TYPE", "edu6_studio", self.env_path,
                          quote=False)
        with patch.object(agent.docker_manager, "ensure_environment_stopped"), \
             patch.object(agent.identify_arm, "scan_and_identify_arms",
                          return_value=(None, ArmDevice(serial_path=self.EDU6,
                                                        role="follower"))):
            self.app.handle_scan_arms({})

    def _set_type(self, robot_type):
        with patch.object(agent.docker_manager, "get_container_status", return_value={}):
            return self.app.handle_set_robot_type({"robot_type": robot_type})

    def _status(self):
        with patch.object(agent.docker_manager, "get_container_status", return_value={}), \
             patch.object(agent.docker_manager, "get_last_pull_status", return_value={}):
            return self.app.handle_status()[1]

    # ── the defect, both directions ─────────────────────────────────────────

    def test_omx_scan_then_edu6_is_not_ready(self):
        self._seed_omx_pair()
        code, payload = self._set_type("edu6_studio")
        self.assertEqual(code, 200)
        self.assertFalse(payload["hardware_ready"])
        self.assertFalse(self._status()["hardware_ready"])

    def test_edu6_scan_then_omx_follower_is_not_ready(self):
        # The reverse, and the one a leader-less OMX profile makes reachable:
        # omx_follower needs only a follower, so PRESENCE alone said ready.
        self._seed_edu6()
        code, payload = self._set_type("omx_follower")
        self.assertEqual(code, 200)
        self.assertFalse(payload["hardware_ready"])

    def test_the_start_gate_refuses_and_never_reaches_docker(self):
        self._seed_omx_pair()
        self._set_type("edu6_studio")
        with patch.object(agent.docker_manager, "start_robot_tier") as start:
            code, payload = self.app.handle_environment_start({})
        self.assertEqual(code, 400)
        start.assert_not_called()
        # NOT the generic „bitte zuerst … scannen": the arms ARE scanned.
        self.assertEqual(payload["message"], agent._ARM_FAMILY_MISMATCH_DE)
        self.assertIn("anderen Robotertyp", payload["message"])

    def test_the_student_is_told_in_the_protokoll_at_the_moment_it_happens(self):
        # The wizard's own reaction is a pill going grey beside a serial path
        # that is still on screen — indistinguishable from a glitch.
        self._seed_omx_pair()
        with patch.object(self.app, "_log") as log:
            self._set_type("edu6_studio")
        lines = [str(c[0][0]) for c in log.call_args_list]
        self.assertTrue(any(agent._ARM_FAMILY_MISMATCH_DE in ln for ln in lines), lines)

    def test_the_eager_env_write_never_names_a_wrong_family_port(self):
        # The Pi writes the .env at SELECTOR time (Windows defers to start), so
        # this is the one platform that could persist the chimera.
        self._seed_omx_pair()
        self._set_type("edu6_studio")
        self.assertEqual(cg.read_env_var("EDUBOTICS_ROBOT_TYPE", self.env_path),
                         "edu6_studio")
        # The LAST COHERENT scan survives untouched — clearing it would destroy
        # a valid OMX scan just because a student looked at the dropdown.
        self.assertEqual(cg.read_env_var("FOLLOWER_PORT", self.env_path), self.OMX_F)
        self.assertEqual(cg.read_env_var("LEADER_PORT", self.env_path), self.OMX_L)
        self.assertEqual(cg.read_env_var("EDUBOTICS_FOLLOWER_ONLY", self.env_path), "0")

    def test_the_eager_write_judges_the_LEADER_it_emits_too(self):
        """The other half of the write guard, and it survived a mutation round:
        narrowing ``emitted`` to ``[hw.follower]`` left both suites green.

        Reachable only through a hand-edited .env that mixes families (a scan
        is family-scoped, so it cannot produce this) — but the guard's claim is
        "never write a port from another family", and half a guard is the
        failure mode this branch keeps being audited for.
        """
        cg.upsert_env_var("EDUBOTICS_ROBOT_TYPE", "omx_full", self.env_path,
                          quote=False)
        self.app._hardware = HardwareConfig(
            leader=ArmDevice(serial_path=self.EDU6, role="leader"),
            follower=ArmDevice(serial_path=self.OMX_F, role="follower"))
        self.app._persist_env_if_ready()
        self.assertIsNone(cg.read_env_var("LEADER_PORT", self.env_path))
        self.assertIsNone(cg.read_env_var("FOLLOWER_PORT", self.env_path))
        # …and the same rig is not startable, for the same reason.
        self.assertFalse(self.app._hardware_ready())

    def test_a_follower_only_write_ignores_a_wrong_family_leader_it_omits(self):
        """The complement: ``follower_only`` drops LEADER_PORT from the output,
        so a leftover wrong-family leader is not part of what gets written and
        must not block the write."""
        cg.upsert_env_var("EDUBOTICS_ROBOT_TYPE", "omx_follower", self.env_path,
                          quote=False)
        self.app._hardware = HardwareConfig(
            leader=ArmDevice(serial_path=self.EDU6, role="leader"),
            follower=ArmDevice(serial_path=self.OMX_F, role="follower"))
        self.app._persist_env_if_ready()
        self.assertEqual(cg.read_env_var("FOLLOWER_PORT", self.env_path), self.OMX_F)
        self.assertIsNone(cg.read_env_var("LEADER_PORT", self.env_path))

    def test_arms_identified_still_reports_what_was_scanned(self):
        self._seed_omx_pair()
        self._set_type("edu6_studio")
        arms = self._status()["arms_identified"]
        self.assertEqual(arms["follower"], self.OMX_F)
        self.assertEqual(arms["leader"], self.OMX_L)
        self.assertTrue(arms["both"])

    def test_a_restart_re_derives_the_verdict_from_the_env_alone(self):
        # No new persisted state: the recorded PORT is the family tag, so a
        # fresh AgentApp over the same .env must reach the same verdict.
        self._seed_omx_pair()
        self._set_type("edu6_studio")
        fresh = agent.AgentApp(env_file=self.env_path)
        fresh.rehydrate_hardware()  # what the boot path does (agent.py: start())
        self.assertEqual(fresh._current_robot_type(), "edu6_studio")
        self.assertEqual(fresh._hardware.follower.serial_path, self.OMX_F)
        self.assertFalse(fresh._hardware_ready())

    # ── what must NOT change ────────────────────────────────────────────────

    def test_a_same_family_switch_stays_ready(self):
        # omx_full -> omx_follower shares the omx family: no rescan, and the
        # eager write still runs (LEADER_PORT dropped, FOLLOWER_ONLY=1).
        self._seed_omx_pair()
        code, payload = self._set_type("omx_follower")
        self.assertEqual(code, 200)
        self.assertTrue(payload["hardware_ready"])
        self.assertIsNone(cg.read_env_var("LEADER_PORT", self.env_path))
        self.assertEqual(cg.read_env_var("EDUBOTICS_FOLLOWER_ONLY", self.env_path), "1")

    def test_an_ordinary_omx_rig_is_completely_unaffected(self):
        self._seed_omx_pair()
        self.assertTrue(self._status()["hardware_ready"])
        with patch.object(agent.docker_manager, "start_robot_tier", return_value=True) as start:
            code, _ = self.app.handle_environment_start({})
        self.assertEqual(code, 200)
        start.assert_called_once()

    def test_an_ordinary_edu6_rig_is_completely_unaffected(self):
        self._seed_edu6()
        self.assertTrue(self._status()["hardware_ready"])
        with patch.object(agent.docker_manager, "start_robot_tier", return_value=True) as start:
            code, _ = self.app.handle_environment_start({})
        self.assertEqual(code, 200)
        start.assert_called_once()

    def test_an_unattributable_port_is_not_refused(self):
        # A hand-edited .env, or an edu6 board revision _EDU6_BYID_MARKERS has
        # not caught up with (it is a guess pending rig gate R1). "Cannot
        # prove" must not become "wrong" — that would brick the rig the
        # markers were meant to find.
        cg.upsert_env_var("EDUBOTICS_ROBOT_TYPE", "edu6_studio", self.env_path,
                          quote=False)
        self.app._hardware = HardwareConfig(
            follower=ArmDevice(serial_path="/dev/serial/by-id/usb-Mystery_Board-if00"))
        self.assertTrue(self.app._hardware_ready())

    def test_a_missing_scan_still_gets_the_missing_scan_wording(self):
        # The two 400s must stay distinguishable: nothing scanned is not the
        # same problem as the wrong thing scanned.
        cg.upsert_env_var("EDUBOTICS_ROBOT_TYPE", "edu6_studio", self.env_path,
                          quote=False)
        self.app._hardware = HardwareConfig()
        with patch.object(agent.docker_manager, "start_robot_tier") as start:
            code, payload = self.app.handle_environment_start({})
        self.assertEqual(code, 400)
        start.assert_not_called()
        self.assertEqual(payload["message"], "Bitte zuerst den Roboterarm scannen.")

    def test_a_leftover_wrong_family_leader_does_not_refuse_a_follower_only_rig(self):
        # omx_follower never launches a leader, so judging one would refuse a
        # rig over a record nothing reads.
        cg.upsert_env_var("EDUBOTICS_ROBOT_TYPE", "omx_follower", self.env_path,
                          quote=False)
        self.app._hardware = HardwareConfig(
            leader=ArmDevice(serial_path=self.EDU6, role="leader"),
            follower=ArmDevice(serial_path=self.OMX_F, role="follower"))
        self.assertTrue(self.app._hardware_ready())


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


class TestRoboterStudioLeaderLess(_EnvTempBase):
    """WP-5 — the leader toggle is REFUSED on a profile that has no leader.

    On Windows a follower-only rig never constructs the :8769 bridge at all, so
    the toggle simply cannot be reached. The Pi serves the same contract from
    the always-on agent, so the lockout has to be a German 4xx belt here, behind
    the React hide."""

    def _set_type(self, robot_type):
        cg.upsert_env_var("EDUBOTICS_ROBOT_TYPE", robot_type, self.env_path,
                          quote=False)

    def test_both_directions_are_refused_on_every_leader_less_profile(self):
        for pid in ("omx_follower", "edu6_studio"):
            for follower_only in (True, False):
                with self.subTest(profile=pid, follower_only=follower_only):
                    self._set_type(pid)
                    with patch.object(agent.docker_manager,
                                      "set_leader_mode") as slm:
                        code, payload = self.app.handle_rs_set_mode(follower_only)
                    self.assertEqual(code, 409)
                    self.assertFalse(payload["ok"])
                    self.assertEqual(
                        payload["message"],
                        "Dieser Robotertyp hat keinen Leader-Arm — Roboter "
                        "Studio ist hier dauerhaft aktiv.")
                    # Never recreates the arm container for a no-op switch.
                    slm.assert_not_called()

    def test_the_refusal_reports_the_rigs_true_mode_not_the_request(self):
        self._set_type("edu6_studio")
        with patch.object(agent.docker_manager, "set_leader_mode"):
            _, payload = self.app.handle_rs_set_mode(False)
        self.assertTrue(payload["follower_only"])

    def test_the_refusal_does_not_leave_the_busy_flag_set(self):
        # It returns BEFORE taking _rs_busy_lock, so a refused click must not
        # wedge the toggle into „Ein Moduswechsel läuft bereits".
        self._set_type("edu6_studio")
        with patch.object(agent.docker_manager, "set_leader_mode"):
            self.app.handle_rs_set_mode(True)
        self.assertFalse(self.app._rs_busy)
        self.assertFalse(self.app._rs_switch_in_flight)

    def test_omx_full_still_delegates_in_both_directions(self):
        self._set_type("omx_full")
        self.app._hardware = HardwareConfig(
            leader=ArmDevice(serial_path="/dev/l"),
            follower=ArmDevice(serial_path="/dev/f"))
        for follower_only in (True, False):
            with self.subTest(follower_only=follower_only):
                with patch.object(agent.docker_manager, "set_leader_mode",
                                  return_value=(True, "ok")) as slm:
                    code, _ = self.app.handle_rs_set_mode(follower_only)
                self.assertEqual(code, 200)
                slm.assert_called_once()

    def test_an_unknown_robot_type_keeps_the_omx_toggle(self):
        # `_current_robot_type` degrades an unknown id to omx_full — the
        # documented one-variable rollback must not lock the toggle out.
        self._set_type("kein_solcher_typ")
        self.app._hardware = HardwareConfig(
            leader=ArmDevice(serial_path="/dev/l"),
            follower=ArmDevice(serial_path="/dev/f"))
        with patch.object(agent.docker_manager, "set_leader_mode",
                          return_value=(True, "ok")) as slm:
            code, _ = self.app.handle_rs_set_mode(True)
        self.assertEqual(code, 200)
        slm.assert_called_once()

    def test_rs_status_reports_has_leader_per_profile(self):
        for pid, expected in (("omx_full", True), ("omx_follower", False),
                              ("edu6_studio", False)):
            with self.subTest(pid):
                self._set_type(pid)
                with patch.object(agent.docker_manager, "get_container_status",
                                  return_value={"open_manipulator": "running"}):
                    code, payload = self.app.handle_rs_status()
                self.assertEqual(code, 200)
                self.assertIs(payload["has_leader"], expected)

    def test_has_leader_is_reported_on_the_busy_short_circuit_too(self):
        # The busy branch returns early; a toggle that hides on has_leader
        # must not un-hide itself just because another switch is in flight.
        self._set_type("edu6_studio")
        self.app._rs_busy = True
        code, payload = self.app.handle_rs_status()
        self.assertEqual(code, 200)
        self.assertTrue(payload["busy"])
        self.assertIs(payload["has_leader"], False)


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

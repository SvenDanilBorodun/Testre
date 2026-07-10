"""Platform-agnostic contract tests for fast_rehydrate_arms (PR-4).

The launch-time rehydration must (a) NEVER ping-identify or start the
scanner container (those are the slow stages it exists to skip), (b) trust
the saved path↔role binding ONLY when both saved by-id paths are present
and distinct, and (c) fall back to (None, None) on every mismatch so the
caller runs the full scan. All usbipd/WSL touchpoints are patched — runs
on any OS (mirrors test_device_manager_attach_argv.py).
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gui"))

from app import device_manager  # noqa: E402

LEADER = "/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_AAA1-if00"
FOLLOWER = "/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_BBB2-if00"


def _attached():
    return [
        device_manager.USBDevice(
            busid="1-3", vid_pid="2F5D:0103",
            description="OpenRB-150 AAA1", state="Shared"),
        device_manager.USBDevice(
            busid="1-4", vid_pid="2F5D:0103",
            description="OpenRB-150 BBB2", state="Shared"),
    ]


class FastRehydrateArmsTest(unittest.TestCase):

    def _run(self, leader_path, follower_path, serial_paths,
             attached=None):
        with patch.object(device_manager, "self_heal_wsl_serial") as heal, \
                patch.object(device_manager, "attach_all_robotis_devices",
                             return_value=attached if attached is not None
                             else _attached()) as attach, \
                patch.object(device_manager, "find_serial_paths_for_robotis",
                             return_value=serial_paths) as find, \
                patch.object(device_manager, "start_scanner_container",
                             MagicMock()) as scanner, \
                patch.object(device_manager, "identify_arm_via_docker",
                             MagicMock()) as identify, \
                patch("time.sleep"):
            result = device_manager.fast_rehydrate_arms(
                leader_path, follower_path)
            return result, heal, attach, find, scanner, identify

    def test_happy_path_rebuilds_both_arms_without_slow_stages(self):
        (leader, follower), heal, attach, find, scanner, identify = self._run(
            LEADER, FOLLOWER, [LEADER, FOLLOWER])
        self.assertIsNotNone(leader)
        self.assertIsNotNone(follower)
        self.assertEqual(leader.role, "leader")
        self.assertEqual(follower.role, "follower")
        self.assertEqual(leader.serial_path, LEADER)
        self.assertEqual(follower.serial_path, FOLLOWER)
        # busid recovered by the SAME description-part heuristic the full
        # scan uses (first substring match) — informational only post-attach,
        # so we assert presence, not pairing precision.
        self.assertIn(leader.busid, {"1-3", "1-4"})
        self.assertIn(follower.busid, {"1-3", "1-4"})
        heal.assert_called_once()
        attach.assert_called_once()
        # THE contract: no scanner container, no serial pings.
        scanner.assert_not_called()
        identify.assert_not_called()

    def test_missing_path_falls_back(self):
        (leader, follower), *_ = self._run(
            LEADER, FOLLOWER, [LEADER])  # follower path absent
        self.assertIsNone(leader)
        self.assertIsNone(follower)

    def test_equal_or_empty_saved_paths_bail_before_attach(self):
        for args in ((LEADER, LEADER), ("", FOLLOWER), (LEADER, ""), ("", "")):
            with patch.object(device_manager, "attach_all_robotis_devices") \
                    as attach:
                result = device_manager.fast_rehydrate_arms(*args)
            self.assertEqual(result, (None, None), args)
            attach.assert_not_called()

    def test_attach_failure_falls_back(self):
        (leader, follower), *_ = self._run(
            LEADER, FOLLOWER, [LEADER, FOLLOWER], attached=[])
        self.assertIsNone(leader)
        self.assertIsNone(follower)


class FollowerOnlyRehydrateTest(unittest.TestCase):
    """require_leader=False (a Roboter Studio follower-only .env has no
    LEADER_PORT): an empty leader path is legal and yields (None, follower);
    the follower is still mandatory (a follower mismatch → (None, None))."""

    def _run(self, leader_path, follower_path, serial_paths,
             require_leader, attached=None):
        with patch.object(device_manager, "self_heal_wsl_serial"), \
                patch.object(device_manager, "attach_all_robotis_devices",
                             return_value=attached if attached is not None
                             else _attached()), \
                patch.object(device_manager, "find_serial_paths_for_robotis",
                             return_value=serial_paths), \
                patch.object(device_manager, "start_scanner_container",
                             MagicMock()) as scanner, \
                patch.object(device_manager, "identify_arm_via_docker",
                             MagicMock()) as identify, \
                patch("time.sleep"):
            result = device_manager.fast_rehydrate_arms(
                leader_path, follower_path, require_leader=require_leader)
            return result, scanner, identify

    def test_accepts_missing_leader(self):
        (leader, follower), scanner, identify = self._run(
            "", FOLLOWER, [FOLLOWER], require_leader=False)
        self.assertIsNone(leader)
        self.assertIsNotNone(follower)
        self.assertEqual(follower.serial_path, FOLLOWER)
        self.assertEqual(follower.role, "follower")
        # Still the light path — no scanner container, no serial pings.
        scanner.assert_not_called()
        identify.assert_not_called()

    def test_still_fails_on_follower_mismatch(self):
        # Follower absent from the serial list — mandatory, so (None, None).
        (leader, follower), *_ = self._run(
            "", FOLLOWER, [], require_leader=False)
        self.assertIsNone(leader)
        self.assertIsNone(follower)

    def test_empty_follower_bails_before_attach(self):
        with patch.object(device_manager, "attach_all_robotis_devices") as attach:
            result = device_manager.fast_rehydrate_arms(
                "", "", require_leader=False)
        self.assertEqual(result, (None, None))
        attach.assert_not_called()

    def test_stray_leader_equal_to_follower_bails(self):
        # A corrupt mapping (leader path == follower path) must still bail.
        with patch.object(device_manager, "attach_all_robotis_devices") as attach:
            result = device_manager.fast_rehydrate_arms(
                FOLLOWER, FOLLOWER, require_leader=False)
        self.assertEqual(result, (None, None))
        attach.assert_not_called()


if __name__ == "__main__":
    unittest.main()

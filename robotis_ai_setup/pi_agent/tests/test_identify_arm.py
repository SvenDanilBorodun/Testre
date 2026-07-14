"""Tests for the host-side arm scanner (pi_agent.identify_arm).

Deps-free: every ``docker`` subprocess call and the serial-port enumeration are
mocked, so no docker daemon, no OpenRB board and no dynamixel_sdk are needed.
``time.sleep`` is patched out so the retry/settle waits don't slow the suite.

Covers the TWO-arm role disambiguation (servo-ID ping in the scanner container)
and the serial-anchored persistence contract the agent relies on.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parents[2]  # robotis_ai_setup
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pi_agent import identify_arm  # noqa: E402


LEADER = "/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_LEAD11-if00"
FOLLOWER = "/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_FOLL22-if00"


def _proc(stdout="", returncode=0, stderr=""):
    return MagicMock(stdout=stdout, stderr=stderr, returncode=returncode)


class TestFindRobotisSerialPaths(unittest.TestCase):
    def test_filters_to_robotis_and_openrb(self):
        with patch.object(identify_arm, "list_serial_by_id", return_value=[
            "/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_LEAD11-if00",
            "/dev/serial/by-id/usb-SomeVendor_Widget_XYZ-if00",
            "/dev/serial/by-id/usb-OpenRB_150_FOLL22-if00",
        ]):
            paths = identify_arm.find_robotis_serial_paths()
        self.assertEqual(len(paths), 2)
        self.assertNotIn("Widget", "".join(paths))

    def test_missing_dir_returns_empty(self):
        with patch("pi_agent.identify_arm.os.listdir", side_effect=OSError("no such dir")):
            self.assertEqual(identify_arm.list_serial_by_id(), [])


class TestIdentifyArmViaDocker(unittest.TestCase):
    def test_parses_leader_stdout(self):
        with patch("pi_agent.identify_arm.subprocess.run", return_value=_proc("leader\n")):
            self.assertEqual(identify_arm.identify_arm_via_docker(LEADER), "leader")

    def test_nonzero_returns_error(self):
        with patch("pi_agent.identify_arm.subprocess.run",
                   return_value=_proc("", returncode=1, stderr="cannot open")):
            self.assertTrue(
                identify_arm.identify_arm_via_docker(LEADER).startswith("error:"))

    def test_timeout_returns_error(self):
        import subprocess
        with patch("pi_agent.identify_arm.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("docker", 15)):
            self.assertTrue(
                identify_arm.identify_arm_via_docker(LEADER).startswith("error:"))

    def test_exec_targets_in_image_script(self):
        with patch("pi_agent.identify_arm.subprocess.run", return_value=_proc("follower\n")) as run:
            identify_arm.identify_arm_via_docker(FOLLOWER)
        argv = run.call_args[0][0]
        self.assertEqual(argv[:2], ["docker", "exec"])
        self.assertIn(identify_arm.IN_CONTAINER_SCRIPT, argv)
        self.assertIn(FOLLOWER, argv)


class TestScanAndIdentifyArms(unittest.TestCase):
    def setUp(self):
        # Kill all sleeps so retries/settles don't slow the test.
        p = patch("pi_agent.identify_arm.time.sleep")
        self.addCleanup(p.stop)
        p.start()

    def test_identifies_both_arms_by_ping(self):
        with patch.object(identify_arm, "_poll_serial_paths",
                          return_value=[LEADER, FOLLOWER]), \
                patch.object(identify_arm, "start_scanner_container", return_value=True), \
                patch.object(identify_arm, "stop_scanner_container") as stop, \
                patch.object(identify_arm, "identify_arm_via_docker",
                             side_effect=lambda p: "leader" if p == LEADER else "follower"):
            leader, follower = identify_arm.scan_and_identify_arms("img-opi:latest")
        self.assertIsNotNone(leader)
        self.assertIsNotNone(follower)
        self.assertEqual(leader.serial_path, LEADER)
        self.assertEqual(leader.role, "leader")
        self.assertEqual(follower.serial_path, FOLLOWER)
        self.assertEqual(follower.role, "follower")
        # Stable by-id path is the anchor persisted as LEADER_PORT/FOLLOWER_PORT.
        self.assertTrue(leader.serial_path.startswith("/dev/serial/by-id/"))
        stop.assert_called_once()  # scanner container always torn down

    def test_retries_once_on_unknown(self):
        # First ping returns "unknown", second returns the real role.
        seq = {LEADER: iter(["unknown", "leader"]),
               FOLLOWER: iter(["error:flaky", "follower"])}

        def fake_ident(path):
            return next(seq[path])

        with patch.object(identify_arm, "_poll_serial_paths",
                          return_value=[LEADER, FOLLOWER]), \
                patch.object(identify_arm, "start_scanner_container", return_value=True), \
                patch.object(identify_arm, "stop_scanner_container"), \
                patch.object(identify_arm, "identify_arm_via_docker", side_effect=fake_ident):
            leader, follower = identify_arm.scan_and_identify_arms("img")
        self.assertEqual(leader.role, "leader")
        self.assertEqual(follower.role, "follower")

    def test_no_serial_paths_returns_none_none(self):
        with patch.object(identify_arm, "_poll_serial_paths", return_value=[]), \
                patch.object(identify_arm, "start_scanner_container") as start:
            self.assertEqual(identify_arm.scan_and_identify_arms("img"), (None, None))
        start.assert_not_called()

    def test_scanner_start_failure_returns_none_none(self):
        with patch.object(identify_arm, "_poll_serial_paths",
                          return_value=[LEADER, FOLLOWER]), \
                patch.object(identify_arm, "start_scanner_container", return_value=False):
            self.assertEqual(identify_arm.scan_and_identify_arms("img"), (None, None))

    def test_only_follower_present(self):
        with patch.object(identify_arm, "_poll_serial_paths", return_value=[FOLLOWER]), \
                patch.object(identify_arm, "start_scanner_container", return_value=True), \
                patch.object(identify_arm, "stop_scanner_container"), \
                patch.object(identify_arm, "identify_arm_via_docker", return_value="follower"):
            leader, follower = identify_arm.scan_and_identify_arms("img")
        self.assertIsNone(leader)
        self.assertIsNotNone(follower)


class TestFastRehydrateArms(unittest.TestCase):
    def setUp(self):
        p = patch("pi_agent.identify_arm.time.sleep")
        self.addCleanup(p.stop)
        p.start()

    def test_both_present_rebuilds_binding(self):
        with patch.object(identify_arm, "find_robotis_serial_paths",
                          return_value=[LEADER, FOLLOWER]):
            leader, follower = identify_arm.fast_rehydrate_arms(LEADER, FOLLOWER)
        self.assertEqual(leader.serial_path, LEADER)
        self.assertEqual(leader.role, "leader")
        self.assertEqual(follower.serial_path, FOLLOWER)
        self.assertEqual(follower.role, "follower")

    def test_missing_path_falls_back_to_full_scan(self):
        # Only the leader reappears → (None, None) so the caller full-scans.
        with patch.object(identify_arm, "find_robotis_serial_paths",
                          return_value=[LEADER]):
            self.assertEqual(
                identify_arm.fast_rehydrate_arms(LEADER, FOLLOWER), (None, None))

    def test_identical_saved_paths_rejected(self):
        self.assertEqual(
            identify_arm.fast_rehydrate_arms(LEADER, LEADER), (None, None))

    def test_empty_saved_paths_rejected(self):
        self.assertEqual(
            identify_arm.fast_rehydrate_arms("", FOLLOWER), (None, None))


if __name__ == "__main__":
    unittest.main()

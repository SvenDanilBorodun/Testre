"""Tests for the host-side arm scanner (pi_agent.identify_arm).

Deps-free: every ``docker`` subprocess call and the serial-port enumeration are
mocked, so no docker daemon, no OpenRB board and no dynamixel_sdk are needed.
``time.sleep`` is patched out so the retry/settle waits don't slow the suite.

Covers the TWO-arm role disambiguation (servo-ID ping in the scanner container)
and the serial-anchored persistence contract the agent relies on.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
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


EDU6 = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5A68010132-if00"


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


class TestFamilyScopedSerialFilter(unittest.TestCase):
    """The by-id marker filter is the WHOLE of the Pi's family scoping — there
    is no usbipd attach to scope, unlike Windows. If it does not match, the arm
    is invisible and the student is told no ports were found."""

    ALL = [
        "/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_LEAD11-if00",
        "/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_FOLL22-if00",
        EDU6,
        "/dev/serial/by-id/usb-SomeVendor_Widget_XYZ-if00",
    ]

    def test_edu6_family_finds_the_ch343p_bridge(self):
        with patch.object(identify_arm, "list_serial_by_id", return_value=self.ALL):
            self.assertEqual(identify_arm.find_serial_paths_for_arms("edu6"), [EDU6])

    def test_omx_family_never_matches_the_ch343p(self):
        with patch.object(identify_arm, "list_serial_by_id", return_value=self.ALL):
            hits = identify_arm.find_serial_paths_for_arms("omx")
        self.assertNotIn(EDU6, hits)
        self.assertEqual(len(hits), 2)

    def test_edu6_family_never_matches_an_openrb(self):
        with patch.object(identify_arm, "list_serial_by_id", return_value=self.ALL):
            hits = identify_arm.find_serial_paths_for_arms("edu6")
        self.assertNotIn("ROBOTIS", "".join(hits).upper())

    def test_unknown_family_falls_back_to_omx(self):
        """A .env with a robot type this agent does not know must keep scanning
        for OMX arms, never become a family that matches nothing."""
        with patch.object(identify_arm, "list_serial_by_id", return_value=self.ALL):
            self.assertEqual(identify_arm.find_serial_paths_for_arms("nonsense"),
                             identify_arm.find_serial_paths_for_arms("omx"))

    def test_markers_are_verbatim_the_windows_tuple(self):
        """COPIED, not invented: the Windows tuple is a six-way guess that rig
        gate R1 will replace with the measured string. Both platforms must widen
        together or a board revision that works on Windows stays invisible here."""
        from gui.app import device_manager as win_dm
        self.assertEqual(identify_arm._FEETECH_BYID_MARKERS,
                         win_dm._FEETECH_BYID_MARKERS)
        self.assertEqual(identify_arm._ARM_MARKERS["omx"], ("ROBOTIS", "OPENRB"))

    def test_both_feetech_families_share_one_marker_tuple(self):
        """edu6 and edu1 sit on the SAME Waveshare adapter, so their by-id
        strings are indistinguishable. Sharing the tuple is the honest encoding
        of that; the servo COUNT is what actually separates them."""
        self.assertIs(identify_arm._ARM_MARKERS["edu6"],
                      identify_arm._ARM_MARKERS["edu1"])
        self.assertEqual(identify_arm._FAMILY_SERVO_COUNT,
                         {"edu6": 7, "edu1": 6})

    def setUp(self):
        identify_arm._LAST_DIAG_CANDIDATES = None

    def test_unmatched_edu6_scan_logs_the_candidates_for_rig_gate_r1(self):
        """The escape hatch: the real CH343P by-id string can only be recorded
        from hardware, so an unmatched edu6 scan must dump what it DID see."""
        others = ["/dev/serial/by-id/usb-Mystery_Board_0001-if00"]
        with patch.object(identify_arm, "list_serial_by_id", return_value=others), \
                patch.object(identify_arm.logger, "warning") as warn:
            self.assertEqual(identify_arm.find_serial_paths_for_arms("edu6"), [])
        self.assertTrue(warn.called)
        self.assertIn("Mystery_Board_0001", str(warn.call_args))

    def test_the_candidate_dump_is_not_repeated_once_per_poll(self):
        """This logger feeds the 800-line Protokoll ring a student reads, and the
        scan polls ten times — one dump per call put eleven identical lines in
        front of them for every failed edu6 scan."""
        others = ["/dev/serial/by-id/usb-Mystery_Board_0001-if00"]
        with patch.object(identify_arm, "list_serial_by_id", return_value=others), \
                patch.object(identify_arm.logger, "warning") as warn:
            for _ in range(identify_arm._SERIAL_POLL_ATTEMPTS):
                identify_arm.find_serial_paths_for_arms("edu6")
        self.assertEqual(warn.call_count, 1)

    def test_a_changed_bus_is_reported_again(self):
        """Keyed on the candidate SET, not a bool: a student who plugs something
        in mid-poll must still produce the observation rig gate R1 needs."""
        first = ["/dev/serial/by-id/usb-Mystery_Board_0001-if00"]
        then = first + ["/dev/serial/by-id/usb-Another_Board_0002-if00"]
        with patch.object(identify_arm.logger, "warning") as warn:
            with patch.object(identify_arm, "list_serial_by_id", return_value=first):
                identify_arm.find_serial_paths_for_arms("edu6")
            with patch.object(identify_arm, "list_serial_by_id", return_value=then):
                identify_arm.find_serial_paths_for_arms("edu6")
        self.assertEqual(warn.call_count, 2)
        self.assertIn("Another_Board_0002", str(warn.call_args))

    def test_no_candidate_dump_when_nothing_is_plugged_in(self):
        with patch.object(identify_arm, "list_serial_by_id", return_value=[]), \
                patch.object(identify_arm.logger, "warning") as warn:
            self.assertEqual(identify_arm.find_serial_paths_for_arms("edu6"), [])
        warn.assert_not_called()

    def test_the_candidate_dump_carries_no_maintainer_jargon(self):
        """This logger is attached to the ROOT logger, so every line lands in
        the Protokoll a student reads (the Windows twin writes to a file). The
        candidate list is the payload; symbol names and file paths are not."""
        with patch.object(identify_arm, "list_serial_by_id",
                          return_value=["/dev/serial/by-id/usb-Mystery-if00"]), \
                patch.object(identify_arm.logger, "warning") as warn:
            identify_arm.find_serial_paths_for_arms("edu6")
        line = warn.call_args[0][0]
        for jargon in ("_FEETECH_BYID_MARKERS", "gui/app", "rig gate",
                       "lockstep"):
            self.assertNotIn(jargon, line)
        self.assertLess(len(line), 80, line)

    def test_omx_scan_never_dumps_candidates(self):
        """OMX must stay byte-identical to the pre-edu6 behaviour."""
        with patch.object(identify_arm, "list_serial_by_id",
                          return_value=["/dev/serial/by-id/usb-Mystery-if00"]), \
                patch.object(identify_arm.logger, "warning") as warn:
            identify_arm.find_serial_paths_for_arms("omx")
        warn.assert_not_called()


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


class TestProtocolArgv(unittest.TestCase):
    """Without --protocol the in-image script defaults to `dxl` and pings
    Dynamixel IDs on a Feetech bus — the edu6 arm can never be identified."""

    def test_feetech_protocol_is_passed_through(self):
        with patch("pi_agent.identify_arm.subprocess.run",
                   return_value=_proc("edu6\n", returncode=0)) as run:
            verdict = identify_arm.identify_arm_via_docker(EDU6, "feetech")
        self.assertEqual(verdict, "edu6")
        self.assertIn("--protocol=feetech", run.call_args[0][0])

    def test_dxl_is_the_default_and_passes_no_flag(self):
        """OMX must be byte-identical to the pre-edu6 argv."""
        with patch("pi_agent.identify_arm.subprocess.run",
                   return_value=_proc("leader\n")) as run:
            identify_arm.identify_arm_via_docker(LEADER)
        argv = run.call_args[0][0]
        self.assertFalse([a for a in argv if a.startswith("--")])

    def test_explicit_dxl_also_passes_no_flag(self):
        with patch("pi_agent.identify_arm.subprocess.run",
                   return_value=_proc("leader\n")) as run:
            identify_arm.identify_arm_via_docker(LEADER, "dxl")
        self.assertFalse([a for a in run.call_args[0][0] if a.startswith("--")])


class TestStdoutVerdictWinsOverExitCode(unittest.TestCase):
    """LOAD-BEARING. The in-image script prints its verdict and then exits 1 for
    every result that is not the expected arm (identify_arm.py:133 feetech,
    :141 dxl) — INCLUDING the informational tokens the whole diagnosis is made
    of. Keying on returncode == 0 turns each of them into a bare "error:" and
    every scan-notice branch goes dead."""

    def _verdict(self, stdout, rc=1, stderr=""):
        with patch("pi_agent.identify_arm.subprocess.run",
                   return_value=_proc(stdout, returncode=rc, stderr=stderr)):
            return identify_arm.identify_arm_via_docker(EDU6, "feetech")

    def test_feetech_silent_survives_exit_1(self):
        self.assertEqual(self._verdict("feetech_silent\n"), "feetech_silent")

    def test_partial_survives_exit_1(self):
        self.assertEqual(self._verdict("partial:3\n"), "partial:3")

    def test_cross_probe_token_survives_exit_1(self):
        self.assertEqual(self._verdict("omx_arm_found\n"), "omx_arm_found")

    def test_edu6_arm_found_survives_exit_1_on_the_dxl_path(self):
        with patch("pi_agent.identify_arm.subprocess.run",
                   return_value=_proc("edu6_arm_found\n", returncode=1)):
            self.assertEqual(identify_arm.identify_arm_via_docker(EDU6),
                             "edu6_arm_found")

    def test_unknown_survives_exit_1(self):
        self.assertEqual(self._verdict("unknown\n"), "unknown")

    def test_empty_stdout_still_reports_the_stderr_error(self):
        """The exit code/stderr still decide when there IS no verdict — a
        genuine crash (dead container, missing script) must not be swallowed."""
        v = self._verdict("", rc=1, stderr="No such container: robotis_arm_scanner")
        self.assertTrue(v.startswith("error:"))
        self.assertIn("No such container", v)

    def test_a_verdict_printed_with_exit_0_is_unchanged(self):
        self.assertEqual(self._verdict("edu6\n", rc=0), "edu6")


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
                             side_effect=lambda p, *_: "leader" if p == LEADER else "follower"):
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

        def fake_ident(path, *_):
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

    def test_omx_scan_asks_for_the_dxl_protocol(self):
        with patch.object(identify_arm, "_poll_serial_paths", return_value=[LEADER]), \
                patch.object(identify_arm, "start_scanner_container", return_value=True), \
                patch.object(identify_arm, "stop_scanner_container"), \
                patch.object(identify_arm, "identify_arm_via_docker",
                             return_value="leader") as ident:
            identify_arm.scan_and_identify_arms("img")
        self.assertEqual(ident.call_args[0][1], "dxl")


class TestEdu6Scan(unittest.TestCase):
    """The edu6 family: one arm, probed with the Feetech protocol, slotted as
    the FOLLOWER (it drives FOLLOWER_PORT — there is no leader)."""

    def setUp(self):
        p = patch("pi_agent.identify_arm.time.sleep")
        self.addCleanup(p.stop)
        p.start()
        identify_arm.LAST_SCAN_NOTICE = ""

    def _scan(self, verdict, paths=(EDU6,)):
        with patch.object(identify_arm, "_poll_serial_paths", return_value=list(paths)), \
                patch.object(identify_arm, "start_scanner_container", return_value=True), \
                patch.object(identify_arm, "stop_scanner_container"), \
                patch.object(identify_arm, "identify_arm_via_docker",
                             return_value=verdict) as ident:
            result = identify_arm.scan_and_identify_arms("img", arm_family="edu6")
        return result, ident

    def test_edu6_scan_asks_for_the_feetech_protocol(self):
        _, ident = self._scan("edu6")
        self.assertEqual(ident.call_args[0][1], "feetech")

    def test_edu6_lands_in_the_follower_slot_with_no_leader(self):
        (leader, follower), _ = self._scan("edu6")
        self.assertIsNone(leader)
        self.assertIsNotNone(follower)
        self.assertEqual(follower.serial_path, EDU6)
        self.assertEqual(follower.role, "follower")

    def test_the_family_scopes_the_by_id_poll(self):
        with patch.object(identify_arm, "_poll_serial_paths",
                          return_value=[EDU6]) as poll, \
                patch.object(identify_arm, "start_scanner_container", return_value=True), \
                patch.object(identify_arm, "stop_scanner_container"), \
                patch.object(identify_arm, "identify_arm_via_docker", return_value="edu6"):
            identify_arm.scan_and_identify_arms("img", arm_family="edu6")
        self.assertEqual(poll.call_args.kwargs.get("arm_family"), "edu6")

    # ── the German notices (Rule §1: literal ä ö ü ß) ────────────────────────

    def test_silent_bus_names_the_12_v_supply(self):
        """The single most likely edu6 setup mistake: USB enumerates the port
        but does not power the servos. A bare „Kein Arm gefunden" sends the
        student hunting the cable that is already fine."""
        (leader, follower), _ = self._scan("feetech_silent")
        self.assertIsNone(follower)
        self.assertIn("12-V-Netzteil", identify_arm.LAST_SCAN_NOTICE)
        self.assertIn("Servo", identify_arm.LAST_SCAN_NOTICE)

    def test_partial_bus_names_the_answering_servo_count(self):
        (leader, follower), _ = self._scan("partial:3")
        self.assertIsNone(follower)
        self.assertIn("Nur 3 von 7 Servos", identify_arm.LAST_SCAN_NOTICE)
        self.assertIn("Steckverbindungen", identify_arm.LAST_SCAN_NOTICE)

    def _scan_family(self, verdict, arm_family):
        identify_arm.LAST_SCAN_NOTICE = ""
        with patch.object(identify_arm, "_poll_serial_paths",
                          return_value=[EDU6]), \
                patch.object(identify_arm, "start_scanner_container",
                             return_value=True), \
                patch.object(identify_arm, "stop_scanner_container"), \
                patch.object(identify_arm, "identify_arm_via_docker",
                             return_value=verdict):
            return identify_arm.scan_and_identify_arms("img",
                                                       arm_family=arm_family)

    # ── A1: a bus LONGER than any supported arm ─────────────────────────────
    def test_an_overlong_bus_never_says_more_of_fewer(self):
        """`feetech_bus_length` walks one id PAST the longest arm, so the count
        can EXCEED the selected family's length. As a `partial:` it rendered
        „Nur 8 von 7 Servos antworten" — a fraction greater than one, sending
        the student to the cable checklist for the OPPOSITE fault."""
        for family, count in (("edu6", 7), ("edu1", 6)):
            with self.subTest(family):
                leader, follower = self._scan_family("bus_too_long:8", family)
                self.assertIsNone(follower)
                note = identify_arm.LAST_SCAN_NOTICE
                self.assertNotIn(f"Nur 8 von {count}", note)
                self.assertIn("mindestens 8", note)
                self.assertNotIn("Steckverbindungen", note)

    def test_a_partial_that_meets_the_selected_length_reads_as_a_stale_image(self):
        leader, follower = self._scan_family("partial:6", "edu1")
        self.assertIsNone(follower)
        note = identify_arm.LAST_SCAN_NOTICE
        self.assertNotIn("Nur 6 von 6", note)
        self.assertIn("Abbild", note)

    def test_a_genuine_partial_names_the_selected_arms_length(self):
        self._scan_family("partial:3", "edu1")
        self.assertIn("Nur 3 von 6 Servos", identify_arm.LAST_SCAN_NOTICE)

    def test_bus_length_notice_is_byte_identical_to_the_windows_twin(self):
        from gui.app import device_manager as win_dm
        for role in ("partial:3", "partial:6", "partial:7", "bus_too_long:8",
                     "partial:x", "edu6"):
            for servos in (6, 7, None):
                with self.subTest(role=role, servos=servos):
                    self.assertEqual(
                        identify_arm.feetech_bus_length_notice(role, servos),
                        win_dm.feetech_bus_length_notice(role, servos))
        for name in ("_TOO_LONG_NOTICE_DE", "_STALE_PROBER_NOTICE_DE",
                     "_PARTIAL_NOTICE_DE"):
            self.assertEqual(getattr(identify_arm, name),
                             getattr(win_dm, name), name)
            # Rule §1: literal umlauts, never transliterations.
            for bad in ("ue", "ae", "oe"):
                self.assertNotIn(bad, getattr(identify_arm, name))

    # ── A9: the host must not accept the WRONG family's success token ───────
    def test_the_other_familys_success_token_is_refused(self):
        """Not reachable through the CURRENT prober, but pinning IMAGE_TAG is a
        documented rollback and the stdout-verdict-wins contract lets a stale
        prober's bare family name land here."""
        for selected, reported, needle in (("edu1", "edu6", "EduBotics 6-Achs"),
                                           ("edu6", "edu1", "Edu:1")):
            with self.subTest(selected=selected, reported=reported):
                leader, follower = self._scan_family(reported, selected)
                self.assertIsNone(follower)
                self.assertIsNone(leader)
                self.assertIn(needle, identify_arm.LAST_SCAN_NOTICE)

    def test_the_selected_familys_own_token_is_still_the_follower(self):
        for family in ("edu6", "edu1"):
            with self.subTest(family):
                leader, follower = self._scan_family(family, family)
                self.assertIsNotNone(follower)
                self.assertEqual(follower.role, "follower")
                self.assertIsNone(leader)
                self.assertEqual(identify_arm.LAST_SCAN_NOTICE, "")

    def test_partial_token_is_ignored_on_the_dxl_path(self):
        """`partial:N` is a Feetech-only diagnosis; the dxl prober never emits
        it, so an OMX scan must not grow a Feetech sentence."""
        with patch.object(identify_arm, "_poll_serial_paths", return_value=[LEADER]), \
                patch.object(identify_arm, "start_scanner_container", return_value=True), \
                patch.object(identify_arm, "stop_scanner_container"), \
                patch.object(identify_arm, "identify_arm_via_docker",
                             return_value="partial:3"):
            identify_arm.scan_and_identify_arms("img")
        self.assertEqual(identify_arm.LAST_SCAN_NOTICE, "")

    def test_cross_probe_omx_arm_while_edu6_selected(self):
        self._scan("omx_arm_found")
        self.assertIn("OMX-Arm", identify_arm.LAST_SCAN_NOTICE)
        self.assertIn("Robotertyp", identify_arm.LAST_SCAN_NOTICE)

    def test_cross_probe_feetech_arm_while_omx_selected(self):
        with patch.object(identify_arm, "_poll_serial_paths", return_value=[LEADER]), \
                patch.object(identify_arm, "start_scanner_container", return_value=True), \
                patch.object(identify_arm, "stop_scanner_container"), \
                patch.object(identify_arm, "identify_arm_via_docker",
                             return_value="edu6_arm_found"):
            identify_arm.scan_and_identify_arms("img")
        self.assertIn("EduBotics-Arm", identify_arm.LAST_SCAN_NOTICE)
        self.assertIn("OMX-Robotertyp", identify_arm.LAST_SCAN_NOTICE)

    def test_cross_probe_names_the_feetech_family_the_prober_found(self):
        """The probe CAN tell the two Feetech arms apart (it counts servos), so
        its sentence names them — unlike the USB-presence path, which cannot."""
        for token, needle in (("edu1_arm_found", "Edu:1"),
                              ("edu6_arm_found", "EduBotics 6-Achs")):
            with self.subTest(token):
                selected = "edu6" if token.startswith("edu1") else "edu1"
                with patch.object(identify_arm, "_poll_serial_paths",
                                  return_value=[EDU6]), \
                        patch.object(identify_arm, "start_scanner_container",
                                     return_value=True), \
                        patch.object(identify_arm, "stop_scanner_container"), \
                        patch.object(identify_arm, "identify_arm_via_docker",
                                     return_value=token):
                    identify_arm.scan_and_identify_arms("img",
                                                        arm_family=selected)
                self.assertIn(needle, identify_arm.LAST_SCAN_NOTICE)

    def test_six_answering_servos_owns_its_ambiguity(self):
        """A healthy Edu:1 and a 6-axis arm whose gripper servo dropped off the
        chain are THE SAME six servos. The sentence must offer both readings —
        naming only the likelier one strands the student when it is wrong."""
        text = identify_arm._CROSS_NOTICE_EDU1_WHILE_EDU6
        self.assertIn("Edu:1", text)
        self.assertIn("EduBotics 6-Achs", text)
        self.assertIn("siebte Servo", text)
        # …and it must OFFER the two readings, not ASSERT one and walk it back.
        # The old wording opened „das ist ein Edu:1-Arm" and only afterwards
        # said „falls es doch der 6-Achs-Arm ist", which reads as a verdict.
        self.assertIn("Zwei Möglichkeiten", text)
        self.assertNotIn("das ist ein", text)
        self.assertLess(text.index("entweder"), text.index("Edu:1"))

    def test_notices_are_copied_verbatim_from_the_windows_twin(self):
        from gui.app import device_manager as win_dm
        self.assertEqual(identify_arm._CROSS_NOTICES, win_dm._CROSS_NOTICES)
        self.assertEqual(identify_arm.cross_family_notice("omx", "edu1"),
                         win_dm.cross_family_notice("omx", "edu1"))
        self.assertEqual(identify_arm.cross_family_notice("x", "y"), "")

    def test_notices_use_literal_umlauts_never_transliterations(self):
        """Rule §1 — the notice reaches the System tab verbatim."""
        for text in identify_arm._CROSS_NOTICES.values():
            for bad in ("ue", "ae", "oe", "ss "):
                self.assertNotIn(bad, text.replace("EduBotics", ""))
        self._scan("feetech_silent")
        self.assertNotIn("pruefen", identify_arm.LAST_SCAN_NOTICE)

    def test_a_clean_scan_leaves_no_stale_notice(self):
        identify_arm.LAST_SCAN_NOTICE = "eine alte Meldung"
        self._scan("edu6")
        self.assertEqual(identify_arm.LAST_SCAN_NOTICE, "")

    def test_a_FAILED_scan_never_reports_the_PREVIOUS_scan_s_notice(self):
        """The end-of-scan clear only fires on success, so the entry clear is
        what stops a failure inheriting an older diagnosis: the student unplugs
        the OMX arm, plugs in the edu6 one, rescans, gets nothing — and must not
        be told about the OMX arm that is no longer there."""
        identify_arm.LAST_SCAN_NOTICE = identify_arm._CROSS_NOTICE_OMX_WHILE_FEETECH
        with patch.object(identify_arm, "_poll_serial_paths", return_value=[EDU6]), \
                patch.object(identify_arm, "start_scanner_container", return_value=True), \
                patch.object(identify_arm, "stop_scanner_container"), \
                patch.object(identify_arm, "identify_arm_via_docker",
                             return_value="unknown"):
            leader, follower = identify_arm.scan_and_identify_arms(
                "img", arm_family="edu6")
        self.assertIsNone(follower)
        self.assertEqual(identify_arm.LAST_SCAN_NOTICE, "")

    def test_a_dongle_that_sorts_first_does_not_poison_a_found_arm(self):
        """A stray CH34x dongle matches the by-id markers, answers no Feetech
        ping, and sorts BEFORE the real arm — so it sets the 12-V sentence and
        the arm then identifies fine. Reporting that would send the student
        hunting a power supply that is working."""
        dongle = "/dev/serial/by-id/usb-1a86_USB2.0-Serial-if00"
        seq = {dongle: "feetech_silent", EDU6: "edu6"}
        with patch.object(identify_arm, "_poll_serial_paths",
                          return_value=[dongle, EDU6]), \
                patch.object(identify_arm, "start_scanner_container", return_value=True), \
                patch.object(identify_arm, "stop_scanner_container"), \
                patch.object(identify_arm, "identify_arm_via_docker",
                             side_effect=lambda p, *_: seq[p]):
            leader, follower = identify_arm.scan_and_identify_arms(
                "img", arm_family="edu6")
        self.assertEqual(follower.serial_path, EDU6)
        self.assertEqual(identify_arm.LAST_SCAN_NOTICE, "")

    def test_a_failed_edu6_scan_still_keeps_its_notice(self):
        """The clear is conditional on the arm being FOUND — the whole point of
        the notice is the case where it was not."""
        self._scan("feetech_silent")
        self.assertIn("12-V-Netzteil", identify_arm.LAST_SCAN_NOTICE)

    def test_a_half_found_omx_rig_keeps_its_notice(self):
        """omx HAS a leader, so follower-only is not success there and the
        diagnosis of the other port stays relevant."""
        seq = {LEADER: "edu6_arm_found", FOLLOWER: "follower"}
        with patch.object(identify_arm, "_poll_serial_paths",
                          return_value=[LEADER, FOLLOWER]), \
                patch.object(identify_arm, "start_scanner_container", return_value=True), \
                patch.object(identify_arm, "stop_scanner_container"), \
                patch.object(identify_arm, "identify_arm_via_docker",
                             side_effect=lambda p, *_: seq[p]):
            leader, follower = identify_arm.scan_and_identify_arms("img")
        self.assertIsNone(leader)
        self.assertIsNotNone(follower)
        self.assertIn("EduBotics-Arm", identify_arm.LAST_SCAN_NOTICE)


class TestCrossFamilyPresenceNotice(unittest.TestCase):
    """The by-id filter is family-scoped, so a wrong-family arm never reaches
    the prober and the probe-token branches cannot fire for it in the real
    flow (audit M4). The presence check is the escape hatch."""

    def setUp(self):
        p = patch("pi_agent.identify_arm.time.sleep")
        self.addCleanup(p.stop)
        p.start()
        identify_arm.LAST_SCAN_NOTICE = ""

    # The presence check is PID-pinned via sysfs, so a fake bus is a
    # {by-id path: (VID, PID)} map. `usb_ids_for_serial_path` returns UPPERCASE
    # (sysfs itself is lowercase) — see TestUsbIdsFromSysfs for that seam.
    OPENRB = ("2F5D", "0103")
    CH343P = ("1A86", "55D3")
    CH340 = ("1A86", "7523")

    def _empty_scan(self, arm_family, bus):
        with patch.object(identify_arm, "list_serial_by_id", return_value=list(bus)), \
                patch.object(identify_arm, "usb_ids_for_serial_path",
                             side_effect=lambda p: bus.get(p)), \
                patch.object(identify_arm, "start_scanner_container") as start:
            result = identify_arm.scan_and_identify_arms("img", arm_family=arm_family)
        start.assert_not_called()
        return result

    def test_edu6_selected_but_an_omx_arm_is_plugged_in(self):
        self._empty_scan("edu6", {LEADER: self.OPENRB})
        self.assertIn("OMX-Arm", identify_arm.LAST_SCAN_NOTICE)

    def test_omx_selected_but_a_feetech_arm_is_plugged_in(self):
        self._empty_scan("omx", {EDU6: self.CH343P})
        # GENERIC about which EduBotics arm: this path only sees the USB ids,
        # and edu6 and edu1 share one adapter.
        self.assertIn("EduBotics-Arm", identify_arm.LAST_SCAN_NOTICE)
        self.assertNotIn("6-Achs", identify_arm.LAST_SCAN_NOTICE)

    def test_edu1_selected_but_an_omx_arm_is_plugged_in(self):
        self._empty_scan("edu1", {LEADER: self.OPENRB})
        self.assertIn("OMX-Arm", identify_arm.LAST_SCAN_NOTICE)

    def test_nothing_plugged_in_says_nothing_extra(self):
        self._empty_scan("edu6", {})
        self.assertEqual(identify_arm.LAST_SCAN_NOTICE, "")

    def test_an_unrelated_serial_device_is_not_an_arm(self):
        self._empty_scan("omx", {"/dev/serial/by-id/usb-Some_GPS_0001-if00":
                                 ("0403", "6001")})
        self.assertEqual(identify_arm.LAST_SCAN_NOTICE, "")

    def test_a_ch34x_dongle_next_to_a_real_arm_does_not_mask_it(self):
        """PID-pinning must not become so strict it misses the arm when a dongle
        shares the bus — the notice is about the ARM being present."""
        self._empty_scan("omx", {"/dev/serial/by-id/usb-1a86_USB2.0-Serial-if00":
                                 self.CH340,
                                 EDU6: self.CH343P})
        self.assertIn("EduBotics-Arm", identify_arm.LAST_SCAN_NOTICE)

    def test_a_commodity_ch34x_dongle_is_not_reported_as_an_edu6_arm(self):
        """The by-id markers include a bare "1A86", which matches every CH340 /
        CH341 / CH9102 dongle in a school cupboard. The presence check ASSERTS a
        fact to the student, so it is PID-pinned — telling someone to change the
        robot type because an Arduino clone is plugged in is worse than the
        generic „Kein Arm gefunden" it replaces."""
        dongle = "/dev/serial/by-id/usb-1a86_USB2.0-Serial-if00"
        # It DOES match the by-id markers — that is the whole point.
        with patch.object(identify_arm, "list_serial_by_id", return_value=[dongle]):
            self.assertEqual(identify_arm.find_serial_paths_for_arms("edu6"), [dongle])
        self._empty_scan("omx", {dongle: self.CH340})
        self.assertEqual(identify_arm.LAST_SCAN_NOTICE, "")

    def test_unreadable_sysfs_claims_nothing(self):
        """`None` means "cannot prove", never "no match" — and never a claim.
        An unreadable sysfs degrades to the generic „Kein Arm gefunden"."""
        self._empty_scan("omx", {EDU6: None})
        self.assertEqual(identify_arm.LAST_SCAN_NOTICE, "")

    def test_an_omx_board_matches_any_pid_under_its_vid(self):
        """ARM_USB_IDS["omx"] is (("2F5D", None),) — the OpenRB ships 0103 and
        2202 firmware, and both must count."""
        for pid in ("0103", "2202", "9999"):
            with patch.object(identify_arm, "list_serial_by_id", return_value=[LEADER]), \
                    patch.object(identify_arm, "usb_ids_for_serial_path",
                                 return_value=("2F5D", pid)):
                self.assertEqual(identify_arm.find_arm_devices_by_usb_id("omx"),
                                 [LEADER], pid)

    def test_a_non_ascii_sysfs_byte_degrades_instead_of_failing_the_scan(self):
        """The REAL escape this `except Exception` exists for, driven end to end.

        `usb_ids_for_serial_path` opens `idVendor`/`idProduct` with
        `encoding="ascii"` and catches only `OSError` — but a decode failure
        raises `UnicodeDecodeError`, which is a `ValueError`, NOT an `OSError`.
        So it escapes that helper, escapes `find_arm_devices_by_usb_id` (which
        has no handler at all), and lands in
        `_set_cross_family_presence_notice`, whose broad `except` is the ONLY
        thing between a corrupt sysfs byte and a 500 on a scan that had already
        correctly decided „kein Arm gefunden".

        Built on a REAL temporary sysfs tree, like TestUsbIdsFromSysfs: the
        previous version of this test patched `find_serial_paths_for_arms` —
        which is not on this code path at all — AND patched `_poll_serial_paths`
        over it, so it asserted nothing. Neutering the try/except left all 548
        pi_agent tests green.
        """
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        dev = os.path.join(root, "dev")
        os.makedirs(dev)
        node = os.path.join(dev, "ttyACM0")
        open(node, "w").close()
        # An edu6-marker by-id name, so the OTHER-family lookup enumerates it
        # while the selected `omx` family's markers do not match.
        by_id = os.path.join(dev, "usb-1a86_USB_Single_Serial_5A68010132-if00")
        os.symlink(node, by_id)
        sysroot = os.path.join(root, "sys")
        usbdev = os.path.join(sysroot, "usb1", "1-1")
        iface = os.path.join(usbdev, "lvl0")
        os.makedirs(iface)
        # The corrupt byte. 0xFF is not valid ASCII and not valid UTF-8 either.
        with open(os.path.join(usbdev, "idVendor"), "wb") as f:
            f.write(b"\xff\xfe1a86\n")
        with open(os.path.join(usbdev, "idProduct"), "wb") as f:
            f.write(b"55d3\n")
        ttydir = os.path.join(sysroot, "class", "tty", "ttyACM0")
        os.makedirs(ttydir)
        os.symlink(iface, os.path.join(ttydir, "device"))

        with patch.object(identify_arm, "_SYS_TTY_DIR",
                          os.path.join(sysroot, "class", "tty")), \
                patch.object(identify_arm, "list_serial_by_id", return_value=[by_id]), \
                patch.object(identify_arm, "start_scanner_container") as start:
            # Sanity: the raise is REAL and reaches the guarded call unhandled.
            # Without this the test could pass on a tree where the decode was
            # quietly fixed upstream and the guard had become decorative.
            with self.assertRaises(UnicodeDecodeError):
                identify_arm.find_arm_devices_by_usb_id("edu6")
            self.assertEqual(
                identify_arm.scan_and_identify_arms("img", arm_family="omx"),
                (None, None))
        start.assert_not_called()
        self.assertEqual(identify_arm.LAST_SCAN_NOTICE, "")

    def test_the_notice_guard_is_broad_enough_for_a_non_oserror(self):
        """Belt for the breadth of the handler, independent of today's decode.

        Narrowing it to `except OSError` would still pass every other test in
        this class — every one of them supplies ids that decode. The class of
        exception a diagnostic must survive is „anything", because it runs
        AFTER the scan has decided and can only change the WORDING.
        """
        for exc in (ValueError("not an OSError"),
                    RuntimeError("udev exploded"),
                    KeyError("missing")):
            with self.subTest(type(exc).__name__):
                identify_arm.LAST_SCAN_NOTICE = "stale"
                with patch.object(identify_arm, "_poll_serial_paths", return_value=[]), \
                        patch.object(identify_arm, "find_arm_devices_by_usb_id",
                                     side_effect=exc):
                    self.assertEqual(
                        identify_arm.scan_and_identify_arms("img", arm_family="edu6"),
                        (None, None))
                # `scan_and_identify_arms` clears the notice on entry, so the
                # degraded path leaves the generic „kein Arm gefunden" wording.
                self.assertEqual(identify_arm.LAST_SCAN_NOTICE, "")


class TestUsbIdsFromSysfs(unittest.TestCase):
    """The one genuinely new hardware-facing primitive. Built against a REAL
    temporary directory tree rather than mocks, so the two things that would
    actually bite on a Pi are exercised: the walk up from the cdc_acm interface
    to the USB device that carries the ids, and sysfs's lowercase hex."""

    def _fake_bus(self, vid, pid, depth=2, write_ids=True):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        dev = os.path.join(root, "dev")
        os.makedirs(dev)
        node = os.path.join(dev, "ttyACM0")
        open(node, "w").close()
        by_id = os.path.join(dev, "usb-Some_Arm_0001-if00")
        os.symlink(node, by_id)

        sysroot = os.path.join(root, "sys")
        usbdev = os.path.join(sysroot, "usb1", "1-1")
        iface = os.path.join(usbdev, *[f"lvl{i}" for i in range(depth)])
        os.makedirs(iface)
        if write_ids:
            for name, val in (("idVendor", vid), ("idProduct", pid)):
                with open(os.path.join(usbdev, name), "w") as f:
                    f.write(val + "\n")
        ttydir = os.path.join(sysroot, "class", "tty", "ttyACM0")
        os.makedirs(ttydir)
        os.symlink(iface, os.path.join(ttydir, "device"))

        p = patch.object(identify_arm, "_SYS_TTY_DIR",
                         os.path.join(sysroot, "class", "tty"))
        self.addCleanup(p.stop)
        p.start()
        return by_id

    def test_reads_the_ids_and_uppercases_them(self):
        """sysfs writes `1a86`; ARM_USB_IDS holds `1A86`. A case mismatch here
        would silently make every PID comparison fail."""
        by_id = self._fake_bus("1a86", "55d3")
        self.assertEqual(identify_arm.usb_ids_for_serial_path(by_id), ("1A86", "55D3"))

    def test_walks_up_from_the_interface_to_the_usb_device(self):
        """/sys/class/tty/<tty>/device is the cdc_acm INTERFACE; the ids live on
        a parent, and how many levels up depends on the topology."""
        for depth in (0, 1, 2, 3):
            with self.subTest(depth=depth):
                by_id = self._fake_bus("2f5d", "0103", depth=depth)
                self.assertEqual(identify_arm.usb_ids_for_serial_path(by_id),
                                 ("2F5D", "0103"))

    def test_gives_up_rather_than_guessing_when_the_ids_are_too_far_up(self):
        by_id = self._fake_bus("2f5d", "0103", depth=identify_arm._USB_ID_WALK_LEVELS + 2)
        self.assertIsNone(identify_arm.usb_ids_for_serial_path(by_id))

    def test_missing_id_files_are_none_not_an_exception(self):
        by_id = self._fake_bus("", "", write_ids=False)
        self.assertIsNone(identify_arm.usb_ids_for_serial_path(by_id))

    def test_a_path_that_does_not_exist_is_none(self):
        self._fake_bus("1a86", "55d3")
        self.assertIsNone(
            identify_arm.usb_ids_for_serial_path("/dev/serial/by-id/usb-nope-if00"))

    def test_no_sysfs_at_all_is_none(self):
        """A dev box or a container without /sys/class/tty must degrade, not
        raise — the caller treats None as 'cannot prove'."""
        with patch.object(identify_arm, "_SYS_TTY_DIR", "/nonexistent/class/tty"):
            self.assertIsNone(identify_arm.usb_ids_for_serial_path("/dev/ttyACM0"))


class TestFastRehydrateArms(unittest.TestCase):
    def setUp(self):
        p = patch("pi_agent.identify_arm.time.sleep")
        self.addCleanup(p.stop)
        p.start()

    def test_both_present_rebuilds_binding(self):
        with patch.object(identify_arm, "find_serial_paths_for_arms",
                          return_value=[LEADER, FOLLOWER]):
            leader, follower = identify_arm.fast_rehydrate_arms(LEADER, FOLLOWER)
        self.assertEqual(leader.serial_path, LEADER)
        self.assertEqual(leader.role, "leader")
        self.assertEqual(follower.serial_path, FOLLOWER)
        self.assertEqual(follower.role, "follower")

    def test_missing_path_falls_back_to_full_scan(self):
        # Only the leader reappears → (None, None) so the caller full-scans.
        with patch.object(identify_arm, "find_serial_paths_for_arms",
                          return_value=[LEADER]):
            self.assertEqual(
                identify_arm.fast_rehydrate_arms(LEADER, FOLLOWER), (None, None))

    def test_identical_saved_paths_rejected(self):
        self.assertEqual(
            identify_arm.fast_rehydrate_arms(LEADER, LEADER), (None, None))

    def test_empty_saved_paths_rejected(self):
        self.assertEqual(
            identify_arm.fast_rehydrate_arms("", FOLLOWER), (None, None))

    def test_family_scopes_the_rehydrate_poll(self):
        with patch.object(identify_arm, "find_serial_paths_for_arms",
                          return_value=[LEADER, FOLLOWER]) as find:
            identify_arm.fast_rehydrate_arms(LEADER, FOLLOWER, arm_family="edu6")
        self.assertEqual(find.call_args[0][0], "edu6")

    def test_omx_is_the_default_family(self):
        with patch.object(identify_arm, "find_serial_paths_for_arms",
                          return_value=[LEADER, FOLLOWER]) as find:
            identify_arm.fast_rehydrate_arms(LEADER, FOLLOWER)
        self.assertEqual(find.call_args[0][0], "omx")


class TestFastRehydrateLeaderLess(unittest.TestCase):
    """A follower-only profile (Roboter-Studio kit, edu6) has NO ``LEADER_PORT``
    in its .env at all, so requiring one sent every such rig down the slow
    scanner-container path on every revisit. ``require_leader=False`` makes the
    saved follower alone sufficient — the follower stays mandatory."""

    def setUp(self):
        p = patch("pi_agent.identify_arm.time.sleep")
        self.addCleanup(p.stop)
        p.start()

    def test_an_empty_saved_leader_is_legal_and_yields_none_plus_follower(self):
        with patch.object(identify_arm, "find_serial_paths_for_arms",
                          return_value=[FOLLOWER]):
            leader, follower = identify_arm.fast_rehydrate_arms(
                "", FOLLOWER, require_leader=False)
        self.assertIsNone(leader)
        self.assertEqual(follower.serial_path, FOLLOWER)
        self.assertEqual(follower.role, "follower")

    def test_the_follower_is_still_mandatory(self):
        # Bails BEFORE any device poll — an empty follower path is not a thing
        # to go looking for, and polling for it would burn the retry budget.
        with patch.object(identify_arm, "find_serial_paths_for_arms",
                          return_value=[LEADER]) as find:
            self.assertEqual(
                identify_arm.fast_rehydrate_arms("", "", require_leader=False),
                (None, None))
        find.assert_not_called()

    def test_a_missing_follower_still_falls_back_to_the_full_scan(self):
        with patch.object(identify_arm, "find_serial_paths_for_arms",
                          return_value=[LEADER]):
            self.assertEqual(
                identify_arm.fast_rehydrate_arms("", FOLLOWER, require_leader=False),
                (None, None))

    def test_a_stray_saved_leader_is_ignored_not_waited_on(self):
        # A hand-edited follower-only .env can still carry a LEADER_PORT. The
        # Windows twin's `want_leader` note: keying on the SAVED path would burn
        # the whole presence-retry budget on an arm this profile never uses.
        with patch.object(identify_arm, "find_serial_paths_for_arms",
                          return_value=[FOLLOWER]) as find:
            leader, follower = identify_arm.fast_rehydrate_arms(
                LEADER, FOLLOWER, require_leader=False)
        self.assertIsNone(leader)
        self.assertEqual(follower.serial_path, FOLLOWER)
        # Only the follower was ever expected — one poll, no retry loop.
        self.assertEqual(find.call_count, 1)

    def test_a_leader_equal_to_the_follower_is_still_a_corrupt_mapping(self):
        # The device MUST be patched present. Without it the real discovery
        # returns [] on any dev host and the call bails at the later
        # `expected.issubset(...)` check instead — so the test passed with the
        # guard deleted (MEASURED), while on a REAL Pi a hand-edited
        # LEADER_PORT == FOLLOWER_PORT would have been TRUSTED as a binding.
        with patch.object(identify_arm, "find_serial_paths_for_arms",
                          return_value=[FOLLOWER]) as find:
            self.assertEqual(
                identify_arm.fast_rehydrate_arms(FOLLOWER, FOLLOWER,
                                                 require_leader=False),
                (None, None))
        # Bails at the guard, before any device is polled.
        find.assert_not_called()

    def test_a_leader_equal_to_the_follower_is_corrupt_on_a_both_arms_rig_too(self):
        with patch.object(identify_arm, "find_serial_paths_for_arms",
                          return_value=[FOLLOWER]) as find:
            self.assertEqual(
                identify_arm.fast_rehydrate_arms(FOLLOWER, FOLLOWER), (None, None))
        find.assert_not_called()

    def test_require_leader_defaults_true_so_omx_is_unchanged(self):
        # The default must keep the both-arms contract: an empty saved leader
        # bails before any device is polled.
        with patch.object(identify_arm, "find_serial_paths_for_arms",
                          return_value=[LEADER, FOLLOWER]) as find:
            self.assertEqual(
                identify_arm.fast_rehydrate_arms("", FOLLOWER), (None, None))
        find.assert_not_called()


if __name__ == "__main__":
    unittest.main()

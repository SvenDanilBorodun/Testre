"""Cross-boundary lockstep + behaviour for ``serial_path_family_conflict``.

The predicate exists twice — once for Windows
(``gui/app/device_manager.py``) and once natively for the Orange Pi
(``pi_agent/identify_arm.py``) — for the same reason ``fast_rehydrate_arms``
does: neither platform can import the other. Same coupling rule applies, so
this file asserts BOTH implementations answer identically over one table.

What the predicate is FOR: a robot-type change must invalidate a scan that was
made for the other arm family. Measured before it existed — a Pi seeded with an
OMX pair and switched to ``edu6_studio`` answered
``200 {'hardware_ready': True}``, wrote an OpenRB-150 as the FOLLOWER_PORT of a
Feetech rig, and let „Umgebung starten" through into a ~300 s
``service_healthy`` block whose only student-visible outcome was the generic
„konnte nicht gestartet werden". Windows had the identical hole in both
directions (`_hardware_ready('edu6_studio')` True on an OMX pair;
`_hardware_ready('omx_follower')` True on an edu6 arm).

The evidence is the recorded by-id path itself — the scan only ever stores a
path that matched that family's markers — so nothing new is persisted and the
attribution survives an agent restart for free.
"""

import sys
import unittest
from pathlib import Path

_SETUP_DIR = Path(__file__).resolve().parent.parent  # robotis_ai_setup/
if str(_SETUP_DIR) not in sys.path:
    sys.path.insert(0, str(_SETUP_DIR))

from gui.app import device_manager as win  # noqa: E402
from pi_agent import identify_arm as pi  # noqa: E402


# (path, family, expected conflict, why)
_TABLE = [
    # ── the two real rigs, each judged against its own family ───────────────
    ("/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_A0-if00", "omx", False,
     "the OMX arm on an OMX profile"),
    ("/dev/serial/by-id/usb-1a86_USB_Single_Serial_5A68010132-if00", "edu6", False,
     "the measured edu6 CH343P (rig gate R1) on the edu6 profile"),

    # ── the defect this predicate exists for, both directions ───────────────
    ("/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_A0-if00", "edu6", True,
     "an OMX arm recorded, edu6 selected — a Dynamixel board on a Feetech bus"),
    ("/dev/serial/by-id/usb-1a86_USB_Single_Serial_5A68010132-if00", "omx", True,
     "an edu6 arm recorded, OMX selected — the reverse, equally wrong"),

    # ── every other edu6 marker still attributes ────────────────────────────
    ("/dev/serial/by-id/usb-WCH.CN_USB_Single_Serial_0001-if00", "omx", True, "WCH"),
    ("/dev/serial/by-id/usb-1a86_CH343_0001-if00", "omx", True, "CH343"),
    ("/dev/serial/by-id/usb-1a86_USB2.0-Ser_-if00", "omx", True, "USB2.0-SER"),
    ("/dev/serial/by-id/usb-Vendor_USB Single Serial_0001-if00", "omx", True,
     "the space-separated spelling"),
    ("/dev/serial/by-id/usb-OpenRB-150_x-if00", "edu6", True, "OPENRB alone"),

    # ── NO evidence is not evidence: these must NOT refuse ───────────────────
    ("/dev/serial/by-id/usb-Mystery_Board_0001-if00", "omx", False,
     "matches no family — _EDU6_BYID_MARKERS is a guess, so an unattributable "
     "path means the marker list is incomplete, not that the arm is wrong"),
    ("/dev/serial/by-id/usb-Mystery_Board_0001-if00", "edu6", False,
     "same, from the other side"),
    ("", "omx", False, "an empty path claims nothing"),
    (None, "edu6", False, "a None path claims nothing"),
    ("/dev/serial/by-id/usb-ROBOTIS_1a86_weird-if00", "omx", False,
     "matches BOTH — ambiguity is not evidence AGAINST the family we need"),
    ("/dev/serial/by-id/usb-ROBOTIS_1a86_weird-if00", "edu6", False,
     "same, from the other side"),
    ("/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_A0-if00", "nonsense_family", False,
     "a family with NO markers is unjudgeable, not universally conflicting: "
     "the fail-CLOSED reading would make a future ROBOT_PROFILES entry whose "
     "family this table has not caught up with refuse EVERY arm, i.e. one "
     "forgotten checklist line bricks the profile. Unreachable today — both "
     "platforms resolve the family through the registry — but the default is "
     "what a future family inherits"),

    # ── case: by-id names arrive lowercase from udev ─────────────────────────
    ("/dev/serial/by-id/usb-robotis_openrb-150_a0-if00", "edu6", True,
     "the markers are uppercase; the path is not"),
]


class TestSerialPathFamilyConflict(unittest.TestCase):
    def test_both_twins_agree_on_every_case(self):
        for path, family, expected, why in _TABLE:
            with self.subTest(f"{why} :: {path!r} vs {family!r}"):
                w = win.serial_path_family_conflict(path, family)
                p = pi.serial_path_family_conflict(path, family)
                self.assertEqual(w, expected, f"windows: {why}")
                self.assertEqual(p, expected, f"pi: {why}")
                self.assertEqual(w, p, "the two twins disagree")

    def test_the_table_actually_exercises_both_verdicts(self):
        """A table that only ever expects False would pass against
        ``return False`` — the single most likely regression here."""
        verdicts = {expected for _, _, expected, _ in _TABLE}
        self.assertEqual(verdicts, {True, False})
        self.assertGreaterEqual(sum(1 for *_, e, _ in _TABLE if e), 6)

    def test_every_registry_family_has_markers_on_both_platforms(self):
        """The other half of the fail-open above.

        ``serial_path_family_conflict`` cannot judge a family it has no markers
        for, and it deliberately answers "no conflict" there rather than
        refusing every arm. That is the right RUNTIME default and the wrong
        thing to ship: it would silently restore the pre-fix behaviour for the
        new profile. So the gap is caught here instead — add a
        ``ROBOT_PROFILES`` entry and this fails until ``_ARM_MARKERS`` grows
        the family on BOTH platforms.
        """
        from gui.app.constants import ROBOT_PROFILES as win_profiles
        from pi_agent.constants import ROBOT_PROFILES as pi_profiles
        families = {row["arm_family"] for row in win_profiles.values()}
        families |= {row["arm_family"] for row in pi_profiles.values()}
        self.assertTrue(families)
        for family in sorted(families):
            with self.subTest(family):
                self.assertTrue(win._ARM_MARKERS.get(family),
                                f"gui/app/device_manager.py::_ARM_MARKERS has no "
                                f"entry for the registry family {family!r} — the "
                                f"conflict check silently no-ops for that profile")
                self.assertTrue(pi._ARM_MARKERS.get(family),
                                f"pi_agent/identify_arm.py::_ARM_MARKERS has no "
                                f"entry for the registry family {family!r}")

    def test_the_marker_tables_are_the_same_on_both_platforms(self):
        """The predicate is only as good as the markers, and they are copies.
        ``_EDU6_BYID_MARKERS`` is already lockstepped in
        ``pi_agent/tests/test_identify_arm.py``; this pins the whole table,
        which is what the conflict predicate iterates."""
        self.assertEqual(win._ARM_MARKERS, pi._ARM_MARKERS)
        self.assertEqual(pi._ARM_MARKERS["omx"], ("ROBOTIS", "OPENRB"))

    def test_the_windows_table_selects_exactly_what_its_finder_selects(self):
        """`_ARM_MARKERS` is a SECOND spelling of the literals inside
        ``find_serial_paths_for_robotis``, which was deliberately left alone
        (it is the pre-edu6 discovery path and must stay byte-identical). This
        is what stops the two spellings drifting apart."""
        paths = [p for p, _, _, _ in _TABLE if p]
        for family in ("omx", "edu6"):
            with self.subTest(family):
                expected = [p for p in paths
                            if any(m in p.upper() for m in win._ARM_MARKERS[family])]
                import unittest.mock as mock
                with mock.patch.object(win.wsl_bridge, "list_serial_devices",
                                       return_value=paths):
                    self.assertEqual(win.find_serial_paths_for_arms(family), expected)

    def test_the_pi_table_selects_exactly_what_its_finder_selects(self):
        paths = [p for p, _, _, _ in _TABLE if p]
        for family in ("omx", "edu6"):
            with self.subTest(family):
                expected = [p for p in paths
                            if any(m in p.upper() for m in pi._ARM_MARKERS[family])]
                import unittest.mock as mock
                with mock.patch.object(pi, "list_serial_by_id", return_value=paths):
                    self.assertEqual(pi.find_serial_paths_for_arms(family), expected)

    def test_a_scanned_path_can_never_conflict_with_the_family_it_was_scanned_for(self):
        """THE self-consistency property the whole design rests on: the scan
        only stores paths its own family filter selected, so re-judging a
        stored path against that same family can never refuse. If this can
        fail, a rig that scanned successfully could be told to rescan forever.
        """
        paths = [p for p, _, _, _ in _TABLE if p]
        import unittest.mock as mock
        for family in ("omx", "edu6"):
            with mock.patch.object(pi, "list_serial_by_id", return_value=paths):
                selected = pi.find_serial_paths_for_arms(family)
            self.assertTrue(selected, family)
            for path in selected:
                with self.subTest(f"{family} :: {path}"):
                    self.assertFalse(pi.serial_path_family_conflict(path, family))
                    self.assertFalse(win.serial_path_family_conflict(path, family))


if __name__ == "__main__":
    unittest.main()

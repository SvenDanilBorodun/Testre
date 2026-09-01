"""Per-profile Modus help text, and the SINGULAR scan card that goes with it.

Two defects this fences, both measured by rendering the shipped `EduBoticsApp`
headless against each of the three robot types:

  * the two static Modus paragraphs named „OMX – Voll" and „OMX – Roboter Studio
    (nur Follower)" on EVERY profile — including `edu6_studio`, a 6-axis Feetech
    arm that is neither — and the second described a leader toggle
    `_start_rs_control_server` never even constructs on a follower-only profile;
  * `_apply_robot_type_labels`'s follower-only branch hardcoded
    `omx_follower`'s display label and left Schritt A on screen titled
    „Schritt A: Leader-Arm (für diesen Typ nicht nötig)" — wrapped around the
    only button the student must press.

`help_de` lives in `constants.ROBOT_PROFILES` rather than a gui_app-local map so
that „every robot type has help text" is testable. Nothing fenced a PARTIAL
addition before this file: `test_robot_profile_lockstep` compares a fixed key
set and has no „no extra keys" assertion anywhere, and adding `help_de` to only
ONE of the three GUI rows was measured GREEN across the whole suite.

`_apply_robot_type_labels` is source-extracted and driven against fake widgets
(the idiom `test_gui_robot_type` / `test_shutdown_teardown` already use): the
real method needs tkinter, and the point is to exercise the shipped statements
rather than a paraphrase of them.
"""

import ast
import os
import textwrap
import types
import unittest

from gui.app.constants import ROBOT_PROFILES

_GUI_SRC = os.path.join(os.path.dirname(__file__), "..", "gui", "app", "gui_app.py")

# The 24 transliteration stems `ci.yml::german-strings-lint` greps for, plus the
# ones this surface can realistically produce. That lint only scans lines
# carrying a [FEHLER]/[WARNUNG]/[STOPP] marker, so NOTHING in CI reads the
# strings in this file's scope — Rule §1 is enforced here or nowhere.
_TRANSLITERATIONS = (
    "frueh", "kuerzer", "schliessen", "Zeitluecken", "Groesste", "Luecken",
    "Moegliche", "laeuft", "haengt", "pruefen", "Pruefung", "ueberschritten",
    "Schueler", "benoetigen", "Oberflaeche", "uebersprungen", "enthaelt",
    "zusaetzliche", "Verfuegbar", "geaendert", "beschaedigt", "koennte",
    "faehige", "Bildaufloesung",
    # this surface's own risks
    "fuer", "Fuer", "noetig", "moeglich", "waehlen", "gewaehlt", "muessen",
    "zurueck", "Groesse", "anschliessen",
)


def _method_source(name):
    """The dedented source of `EduBoticsApp.<name>`, as shipped."""
    with open(_GUI_SRC, "r", encoding="utf-8") as fh:
        source = fh.read()
    marker = f"    def {name}(self"
    start = source.index(marker)
    rest = source[start:]
    end = rest.find("\n    def ", len(marker))
    return textwrap.dedent(rest[: end if end != -1 else len(rest)])


def _load_method(name, ns):
    exec(compile(_method_source(name), _GUI_SRC, "exec"), ns)  # noqa: S102
    return ns[name]


class _FakeWidget:
    """Records `configure`/`pack`/`pack_forget` without a display."""

    def __init__(self, name, rec):
        self.name = name
        self._rec = rec
        self.packed = None
        self.text = None

    def configure(self, **kw):
        if "text" in kw:
            self.text = kw["text"]
        self._rec.append((self.name, "configure", kw))

    def pack(self, **kw):
        self.packed = True
        self._rec.append((self.name, "pack", kw))

    def pack_forget(self):
        self.packed = False
        self._rec.append((self.name, "pack_forget", {}))


class _FakeVar:
    def __init__(self):
        self.value = None

    def set(self, value):
        self.value = value


class HelpTextIsCompleteAndGerman(unittest.TestCase):
    """The fence M1b showed was missing: a partial addition was fully green."""

    def test_every_robot_type_has_help_text(self):
        for pid, row in ROBOT_PROFILES.items():
            with self.subTest(pid):
                self.assertIn(
                    "help_de", row,
                    f"robot type {pid!r} has no Modus help text — the label "
                    f"under the selector renders EMPTY for it")
                self.assertIsInstance(row["help_de"], str)
                self.assertTrue(
                    row["help_de"].strip(),
                    f"robot type {pid!r} has an empty help_de")

    def test_the_help_text_is_literal_german(self):
        """Rule §1: ä/ö/ü/ß, never ae/oe/ue/ss."""
        for pid, row in ROBOT_PROFILES.items():
            text = row.get("help_de", "")
            for bad in _TRANSLITERATIONS:
                with self.subTest(pid=pid, stem=bad):
                    self.assertNotIn(bad, text)

    def test_no_help_text_names_a_DIFFERENT_robot_type(self):
        """The defect in one assertion: text shown on A must not describe B.

        `edu6_studio`'s paragraph naming „OMX – Roboter Studio (nur Follower)"
        is exactly what shipped, and it is how a student ends up reading about
        a robot that is not on their desk.
        """
        for pid, row in ROBOT_PROFILES.items():
            text = row.get("help_de", "")
            for other_id, other in ROBOT_PROFILES.items():
                if other_id == pid:
                    continue
                with self.subTest(pid=pid, other=other_id):
                    self.assertNotIn(
                        other["display_de"], text,
                        f"{pid}'s help text names {other_id}'s display label")

    def test_the_registry_stays_literal_eval_able(self):
        """`test_robot_profile_lockstep` parses this dict with ast.literal_eval.

        An f-string, a `dict()` call or a `|` merge would break the
        cross-boundary lockstep, not this file — so assert it here, where the
        key that motivated a reformat was added.
        """
        import pathlib
        src = pathlib.Path(_GUI_SRC).parent / "constants.py"
        tree = ast.parse(src.read_text(encoding="utf-8"))
        for node in tree.body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "ROBOT_PROFILES"):
                parsed = ast.literal_eval(node.value)
                self.assertEqual(set(parsed), set(ROBOT_PROFILES))
                return
        self.fail("ROBOT_PROFILES is no longer a module-level assignment")


class TheScanCardGoesSingularOnALeaderLessType(unittest.TestCase):
    """OD-1: hide Schritt A, move the button, say „Roboterarm" — not „Leader".

    Windows and the Pi now AGREE on this. The Pi's `SystemPage.js` hid its
    Leader tile and went singular first, and `CLAUDE.md` recorded that as a
    deliberate divergence Windows must not copy; the owner reversed that call
    because the reason it was made — a permanently empty „Leader —" makes a
    Roboter-Studio kit look half-broken — is just as true on Windows, and worse
    on `edu6_studio`, which has no leader concept at all.
    """

    def _apply(self, profile):
        rec = []
        ns = {"ROBOT_PROFILES": ROBOT_PROFILES,
              "tk": types.SimpleNamespace(X="x", LEFT="left")}
        method = _load_method("_apply_robot_type_labels", ns)
        owner = types.SimpleNamespace(
            _selected_robot_profile=lambda: profile,
            robot_help_var=_FakeVar(),
            leader_frame=_FakeWidget("leader_frame", rec),
            follower_frame=_FakeWidget("follower_frame", rec),
            camera_frame=_FakeWidget("camera_frame", rec),
            camera_hint_label=_FakeWidget("camera_hint_label", rec),
            btn_scan_camera=_FakeWidget("btn_scan_camera", rec),
            leader_hint_label=_FakeWidget("leader_hint_label", rec),
            follower_hint_label=_FakeWidget("follower_hint_label", rec),
            btn_scan_leader=_FakeWidget("btn_scan_leader", rec),
            btn_scan_arm=_FakeWidget("btn_scan_arm", rec),
            follower_status_label=_FakeWidget("follower_status_label", rec),
        )
        method(owner)
        return owner

    # ── omx_full: unchanged, and that is the point ──────────────────────────

    def test_a_both_arms_type_keeps_both_steps_and_the_plural_button(self):
        owner = self._apply("omx_full")
        self.assertTrue(owner.leader_frame.packed)
        self.assertEqual(owner.leader_frame.text, "Schritt A: Leader-Arm")
        self.assertEqual(owner.follower_frame.text, "Schritt B: Follower-Arm")
        self.assertTrue(owner.btn_scan_leader.packed)
        self.assertFalse(owner.btn_scan_arm.packed)
        self.assertEqual(owner.camera_frame.text, "Schritt C: Kameras (bis zu 2)")
        self.assertEqual(owner.btn_scan_camera.text, "Kameras scannen")

    # ── the leader-less types: one step, singular ───────────────────────────

    def test_a_leaderless_type_hides_the_leader_step_entirely(self):
        for profile in ("omx_follower", "edu6_studio"):
            with self.subTest(profile):
                owner = self._apply(profile)
                self.assertFalse(
                    owner.leader_frame.packed,
                    "Schritt A is still on screen for a robot type with no leader "
                    "— retitling it as not needed is what wrapped the only "
                    "scan button the student must press in that very phrase")

    def test_the_surviving_step_is_named_Roboterarm_and_scans_singular(self):
        for profile in ("omx_follower", "edu6_studio"):
            with self.subTest(profile):
                owner = self._apply(profile)
                self.assertEqual(owner.follower_frame.text,
                                 "Schritt A: Roboterarm")
                self.assertTrue(
                    owner.btn_scan_arm.packed,
                    "the scan button was hidden along with Schritt A — the "
                    "student cannot scan at all")
                self.assertFalse(owner.btn_scan_leader.packed)

    def test_the_hint_no_longer_names_omx_followers_label_on_edu6(self):
        owner = self._apply("edu6_studio")
        self.assertNotIn("Roboter Studio (nur Follower)",
                         owner.follower_hint_label.text or "")
        self.assertIn("Arm scannen", owner.follower_hint_label.text or "")

    def test_a_one_camera_type_says_Kamera_everywhere_it_says_Kameras(self):
        """Schritt C's title, its hint and the scan button all follow
        `camera_roles` — „Schritt C: Kamera" over „Kameras scannen" is exactly
        the half-migrated wording this overhaul exists to remove."""
        owner = self._apply("edu6_studio")
        self.assertEqual(owner.camera_frame.text, "Schritt C: Kamera")
        self.assertEqual(owner.btn_scan_camera.text, "Kamera scannen")
        self.assertTrue((owner.camera_hint_label.text or "").startswith(
            "Kamera anschließen"))

    def test_the_help_paragraph_follows_the_selector(self):
        for profile, row in ROBOT_PROFILES.items():
            with self.subTest(profile):
                owner = self._apply(profile)
                self.assertEqual(owner.robot_help_var.value, row["help_de"])

    def test_an_unknown_profile_does_not_crash_and_falls_back_to_both_arms(self):
        """`ROBOT_PROFILES.get(...)` → {} is the same fallback the rest of the
        GUI takes; an empty help paragraph is correct there, not a raise."""
        owner = self._apply("kein_profil")
        self.assertTrue(owner.leader_frame.packed)
        self.assertEqual(owner.robot_help_var.value, "")

    # ── source fences ───────────────────────────────────────────────────────

    def test_the_follower_branch_hardcodes_no_profile_label(self):
        src = _method_source("_apply_robot_type_labels")
        for row in ROBOT_PROFILES.values():
            with self.subTest(row["display_de"]):
                self.assertNotIn(row["display_de"], src)

    def test_the_static_modus_paragraphs_are_gone_from_build_ui(self):
        """Both named profiles that may not be selected; one is now dynamic."""
        src = _method_source("_build_ui")
        self.assertNotIn("braucht nur den Follower-Arm", src)
        self.assertNotIn("schaltest du den Leader-Arm dort bei Bedarf", src)
        self.assertIn("self.robot_help_var", src)


if __name__ == "__main__":
    unittest.main()

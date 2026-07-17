"""Regression guards for the Windows install-completion lifecycle (W11).

The incident-fix surface added for the v2.13.0 pilot — the reboot-pending
routing, the runtime rootfs<->image version handshake, and the elevated-finalize
success/reboot/fail discrimination — had ZERO coverage. These pin it.

Like ``test_gui_robot_type``, the EduBoticsApp methods are extracted from
gui_app.py and exec'd into an injected namespace of test doubles, so a runner
without tkinter/webview still exercises them. Methods that do a local
``from .constants import INSTALL_DIR`` resolve it against the REAL
``gui.app.constants`` by setting ``__package__ = "gui.app"`` in the exec
namespace (the tests already put ``gui`` on sys.path); we then patch
``constants.INSTALL_DIR`` to a temp dir to drive the file-flag logic.

Covered:
  * _reboot_required_pending — True on either the production or dev-layout flag
    path, False when neither exists.
  * _rootfs_rebuild_required — True ONLY on a positive mismatch (both stamps
    present + differ); fails OPEN (False) on match, missing-shipped,
    unreadable-distro, and absent-shipped-file.
  * _prompt_finalize_install / _run_elevated — the EXIT-CODE routing for all
    five finalize outcomes (done / reboot-still-needed / failed / consent-refused
    / UAC-cancelled), the rootfs-mismatch destructive-consent that threads
    -AllowDestructiveReimport, and the decline path (no elevation).
  * FinalizeExitContractTest — the exit codes agree across all three files that
    speak them (import_edubotics_wsl.ps1 -> finalize_install.ps1 -> gui_app.py).

The routing tests exist because the 2026-07-17 incident was a SPLIT CONTRACT:
finalize_install.ps1 correctly started keeping .reboot_required set until full
success (so the flag means "not finished"), while gui_app.py still read that flag
as "reboot needed" AHEAD of the exit code. Every failure — dead pull, refused
consent, failed import — then told the student to reboot, forever. The old suite
covered only (exit 0, flag set) and (exit 1, flag CLEAR); the whole bug lived in
the (exit != 0, flag SET) cell, which nothing tested. Several tests below
deliberately set reboot_pending=True on non-reboot outcomes: that combination is
the regression guard, and re-introducing a flag-first branch must fail them.
"""

import os
import re
import sys
import tempfile
import textwrap
import types
import unittest
from unittest.mock import patch

# Make the `gui` package importable from the repo-root layout used by CI.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from gui.app import constants  # noqa: E402

_GUI_SRC = os.path.join(os.path.dirname(__file__), "..", "gui", "app", "gui_app.py")
_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "installer", "scripts")
_FINALIZE_PS1 = os.path.join(_SCRIPTS, "finalize_install.ps1")
_IMPORT_PS1 = os.path.join(_SCRIPTS, "import_edubotics_wsl.ps1")


def _read(path, encoding="utf-8"):
    with open(path, "r", encoding=encoding) as fh:
        return fh.read()


def _gui_exit_codes():
    """The REAL FINALIZE_EXIT_* values, parsed out of gui_app.py.

    Deliberately not hand-written 0/10/12 in the exec namespace: injecting
    literals would shadow gui_app.py's own constants, so a wrong value there
    would sail through every routing test below. Parse the real ones, and pin
    their values separately in FinalizeExitContractTest."""
    found = {
        m.group(1): int(m.group(2))
        for m in re.finditer(r"^(FINALIZE_EXIT_[A-Z]+)\s*=\s*(\d+)",
                             _read(_GUI_SRC), re.M)
    }
    if not found:
        raise AssertionError("no FINALIZE_EXIT_* constants found in gui_app.py")
    return found


def _ps1_exit_codes(path):
    """`$EXIT_NAME = <int>` assignments from a PowerShell script (BOM-tolerant)."""
    return {
        m.group(1): int(m.group(2))
        for m in re.finditer(r"^\$EXIT_([A-Z]+)\s*=\s*(\d+)",
                             _read(path, encoding="utf-8-sig"), re.M)
    }


def _elevate_fn_src():
    """Source of the module-level `_elevate_and_wait()` from gui_app.py."""
    src = _read(_GUI_SRC)
    start = src.index("def _elevate_and_wait(")
    end = src.index("\ndef ", start + 1)
    return src[start:end]


def _load_method(method_name, ns):
    """Extract `method_name` from gui_app.py and exec it into ``ns``.

    ``ns`` becomes the function's globals; set ``ns["__package__"]`` so a local
    ``from .constants import ...`` resolves. Returns the callable."""
    with open(_GUI_SRC, "r", encoding="utf-8") as fh:
        source = fh.read()
    marker = f"    def {method_name}(self"
    start = source.index(marker)
    rest = source[start:]
    end = rest.find("\n    def ", len(marker))
    snippet = textwrap.dedent(rest[: end if end != -1 else len(rest)])
    exec(compile(snippet, _GUI_SRC, "exec"), ns)  # noqa: S102 — in-repo source
    return ns[method_name]


class _SyncThread:
    """threading.Thread stand-in that runs target() synchronously on start()."""

    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        if self._target is not None:
            self._target()


class RebootRequiredPendingTest(unittest.TestCase):
    """_reboot_required_pending reads the machine-wide {app}\\scripts flag."""

    def _run(self, install_dir):
        ns = {"os": os, "__package__": "gui.app"}
        method = _load_method("_reboot_required_pending", ns)
        with patch.object(constants, "INSTALL_DIR", install_dir):
            return method(types.SimpleNamespace())

    def test_true_on_production_layout_flag(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "scripts"))
            open(os.path.join(d, "scripts", ".reboot_required"), "w").close()
            self.assertTrue(self._run(d))

    def test_true_on_dev_layout_flag(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "installer", "scripts"))
            open(os.path.join(d, "installer", "scripts", ".reboot_required"),
                 "w").close()
            self.assertTrue(self._run(d))

    def test_false_when_no_flag(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(self._run(d))


class RootfsRebuildRequiredTest(unittest.TestCase):
    """_rootfs_rebuild_required blocks ONLY on a positive stamp mismatch and
    fails OPEN on every ambiguity (the classroom-hiccup guard)."""

    def _run(self, install_dir, shipped, distro):
        """shipped=None -> don't write the file at all; distro=None -> distro
        stamp unreadable."""
        if shipped is not None:
            os.makedirs(os.path.join(install_dir, "wsl_rootfs"), exist_ok=True)
            with open(os.path.join(install_dir, "wsl_rootfs", "ROOTFS_VERSION"),
                      "w", encoding="utf-8") as fh:
                fh.write(shipped)
        fake_dm = types.SimpleNamespace(read_rootfs_version=lambda: distro)
        ns = {"os": os, "docker_manager": fake_dm, "__package__": "gui.app"}
        method = _load_method("_rootfs_rebuild_required", ns)
        with patch.object(constants, "INSTALL_DIR", install_dir):
            return method(types.SimpleNamespace())

    def test_positive_mismatch_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(self._run(d, shipped="2", distro="1"))

    def test_matching_versions_pass(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(self._run(d, shipped="1", distro="1"))

    def test_unreadable_distro_stamp_fails_open(self):
        with tempfile.TemporaryDirectory() as d:
            # A pre-2.6.1 distro / transient wsl error -> read_rootfs_version None.
            self.assertFalse(self._run(d, shipped="2", distro=None))

    def test_absent_shipped_file_fails_open(self):
        with tempfile.TemporaryDirectory() as d:
            # Dev build without wsl_rootfs/ROOTFS_VERSION -> can't determine.
            self.assertFalse(self._run(d, shipped=None, distro="1"))

    def test_empty_shipped_stamp_fails_open(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(self._run(d, shipped="  \n", distro="1"))


class PromptFinalizeInstallTest(unittest.TestCase):
    """_prompt_finalize_install + _run_elevated: dialog routing, the
    flag/exit-code success discrimination, and the rootfs destructive consent."""

    def _make(self, *, reason=None, consent=True, elevate=(0, False, None),
              reboot_pending=False, distro_registered=True, script="finalize.ps1"):
        calls = {"elevate": [], "log": [], "status": [], "prereq": 0,
                 "showinfo": [], "showwarning": [], "showerror": []}

        def _fake_elevate(exe, args):
            calls["elevate"].append(args)
            return elevate

        fake_mb = types.SimpleNamespace(
            askyesno=lambda *a, **k: consent,
            showinfo=lambda *a, **k: calls["showinfo"].append(a),
            showwarning=lambda *a, **k: calls["showwarning"].append(a),
            showerror=lambda *a, **k: calls["showerror"].append(a),
            NO="no",
        )
        fake_dm = types.SimpleNamespace(
            is_distro_registered=lambda: distro_registered)
        ns = {
            "os": os,
            "messagebox": fake_mb,
            "_elevate_and_wait": _fake_elevate,
            "docker_manager": fake_dm,
            "threading": types.SimpleNamespace(Thread=_SyncThread),
            "__package__": "gui.app",
        }
        # The real exit-code constants, not literals — see _gui_exit_codes().
        ns.update(_gui_exit_codes())
        method = _load_method("_prompt_finalize_install", ns)
        owner = types.SimpleNamespace(
            _resolve_finalize_script=lambda: script,
            _reboot_required_pending=lambda: reboot_pending,
            _finalize_completed=False,
            _log=calls["log"].append,
            _set_status=calls["status"].append,
            _run_prerequisite_checks=lambda: calls.__setitem__(
                "prereq", calls["prereq"] + 1),
            # Run after-callbacks synchronously so showinfo / prereq-checks fire.
            root=types.SimpleNamespace(
                after=lambda _ms, fn=None: fn() if fn is not None else None),
        )
        return method, owner, calls

    def _run(self, method, owner, reason=None):
        # Isolate the marker/transcript temp writes _run_elevated performs.
        with tempfile.TemporaryDirectory() as tmp, \
                patch("tempfile.gettempdir", return_value=tmp):
            method(owner, reason=reason)

    def test_missing_script_shows_error_no_elevation(self):
        method, owner, calls = self._make(script=None)
        self._run(method, owner)
        self.assertEqual(calls["elevate"], [])
        self.assertTrue(calls["showerror"])

    def test_decline_does_not_elevate(self):
        method, owner, calls = self._make(consent=False)
        self._run(method, owner)
        self.assertEqual(calls["elevate"], [])
        self.assertTrue(any("verschoben" in m for m in calls["log"]))

    # ── The five finalize outcomes ──────────────────────────────────────
    # Outcome 1/5: done.
    def test_success_exit0_reruns_prereqs(self):
        method, owner, calls = self._make(
            elevate=(0, False, None), reboot_pending=False,
            distro_registered=True)
        self._run(method, owner)
        self.assertEqual(len(calls["elevate"]), 1)
        self.assertEqual(calls["prereq"], 1)
        self.assertTrue(any("abgeschlossen" in m for m in calls["log"]))
        # No reboot lie on the happy path.
        self.assertFalse(any("Neustart" in m for m in calls["log"]))

    # Outcome 2/5: a host reboot is genuinely still required. finalize signals
    # this with exit 10 and leaves the flag set; BOTH facts are present here, so
    # this test alone cannot tell exit-code routing from flag routing — that is
    # what the two tests below are for.
    def test_exit10_shows_reboot_notice(self):
        method, owner, calls = self._make(
            elevate=(10, False, None), reboot_pending=True)
        self._run(method, owner)
        self.assertEqual(calls["prereq"], 0)  # did NOT proceed
        self.assertTrue(any("Neustart erforderlich" in m for m in calls["log"]))
        self.assertTrue(calls["showinfo"])

    # Outcome 3/5: the deferred pull/import FAILED. THE regression cell: exit != 0
    # AND the flag still set (finalize clears it only on full success). The
    # student must see the real cause, never "reboot" — rebooting cannot fix a
    # blocked proxy or a full disk, so the old flag-first routing looped forever.
    def test_exit1_with_flag_set_reports_failure_not_reboot(self):
        method, owner, calls = self._make(
            elevate=(1, False, None), reboot_pending=True,
            distro_registered=True)
        self._run(method, owner)
        self.assertEqual(calls["prereq"], 0)
        self.assertTrue(any("fehlgeschlagen" in m for m in calls["log"]))
        self.assertFalse(any("Neustart erforderlich" in m for m in calls["log"]))
        self.assertEqual(calls["showinfo"], [])  # no reboot modal
        self.assertTrue(any("fehlgeschlagen" in s for s in calls["status"]))

    # Outcome 4/5: import refused a destructive re-import for want of consent
    # (exit 12, flag still set). Rebooting can NEVER fix this; the one remedy is
    # re-running the installer, so it must not collapse into either the reboot
    # branch or a generic failure.
    def test_exit12_with_flag_set_reports_consent_remedy(self):
        method, owner, calls = self._make(
            elevate=(12, False, None), reboot_pending=True,
            distro_registered=True)
        self._run(method, owner)
        self.assertEqual(calls["prereq"], 0)
        self.assertFalse(any("Neustart erforderlich" in m for m in calls["log"]))
        self.assertEqual(calls["showinfo"], [])
        self.assertTrue(any("Installer erneut" in m for m in calls["log"]),
                        f"expected the re-run-the-installer remedy: {calls['log']}")
        self.assertTrue(calls["showwarning"], "student needs a modal, not just a log line")
        self.assertTrue(any("Neuaufbau" in s for s in calls["status"]))

    # Outcome 5/5: the student refused the UAC prompt. Checked FIRST, before any
    # exit code (there is no exit code to read).
    def test_cancelled_reports_abgebrochen(self):
        # The err string deliberately does NOT contain "abgebrochen". The old
        # fixture passed "UAC abgebrochen", so the assertion below matched the
        # generic `UAC-Fehler: {err}` echo rather than the cancelled branch —
        # deleting that branch entirely kept this test green. Mirror what
        # _elevate_and_wait actually returns now.
        method, owner, calls = self._make(
            elevate=(None, True, "UAC-Zustimmung verweigert"), reboot_pending=True)
        self._run(method, owner)
        self.assertEqual(calls["prereq"], 0)
        self.assertTrue(any("abgebrochen" in m for m in calls["log"]),
                        f"a UAC decline must be reported as abgebrochen: {calls['log']}")
        self.assertTrue(any("abgebrochen" in s for s in calls["status"]),
                        f"the status line must say abgebrochen too: {calls['status']}")
        self.assertFalse(any("Neustart erforderlich" in m for m in calls["log"]))

    def test_failed_exit_with_flag_clear_reports_fehlgeschlagen(self):
        method, owner, calls = self._make(
            elevate=(1, False, None), distro_registered=True)
        self._run(method, owner)
        self.assertEqual(calls["prereq"], 0)
        self.assertTrue(any("fehlgeschlagen" in m for m in calls["log"]))

    # The flag is NOT a reboot discriminator: an exit code finalize never emits
    # for a reboot must not produce a reboot notice just because the flag is set.
    def test_unknown_nonzero_exit_with_flag_set_is_a_failure(self):
        method, owner, calls = self._make(
            elevate=(3, False, None), reboot_pending=True,
            distro_registered=True)
        self._run(method, owner)
        self.assertTrue(any("fehlgeschlagen" in m for m in calls["log"]))
        self.assertFalse(any("Neustart erforderlich" in m for m in calls["log"]))

    def test_success_latches_finalize_completed_even_if_flag_stuck(self):
        # finalize warns + still exits 0 when it cannot delete .reboot_required.
        # The exit code is the authority, so this is a SUCCESS — and the latch is
        # what stops _run_prerequisite_checks from routing straight back into
        # finalize (an endless UAC loop) off the stuck flag.
        method, owner, calls = self._make(
            elevate=(0, False, None), reboot_pending=True,
            distro_registered=True)
        self._run(method, owner)
        self.assertEqual(calls["prereq"], 1)
        self.assertTrue(owner._finalize_completed)

    def test_non_success_never_latches_finalize_completed(self):
        for code in (1, 10, 12):
            with self.subTest(exit_code=code):
                method, owner, calls = self._make(
                    elevate=(code, False, None), reboot_pending=True,
                    distro_registered=True)
                self._run(method, owner)
                self.assertFalse(owner._finalize_completed)

    def test_exit0_but_distro_missing_reports_fehlgeschlagen(self):
        # is_distro_registered() alone must NOT be trusted as success (the W2
        # upgrade wrinkle): exit 0 with the distro absent is a failure.
        method, owner, calls = self._make(
            elevate=(0, False, None), distro_registered=False)
        self._run(method, owner)
        self.assertEqual(calls["prereq"], 0)
        self.assertTrue(any("fehlgeschlagen" in m for m in calls["log"]))

    def test_rootfs_mismatch_consent_threads_destructive_flag(self):
        method, owner, calls = self._make(
            reason="rootfs_mismatch", consent=True, elevate=(0, False, None),
            distro_registered=True)
        self._run(method, owner, reason="rootfs_mismatch")
        self.assertEqual(len(calls["elevate"]), 1)
        self.assertIn("-AllowDestructiveReimport", calls["elevate"][0])

    def test_rootfs_mismatch_decline_does_not_elevate(self):
        method, owner, calls = self._make(reason="rootfs_mismatch", consent=False)
        self._run(method, owner, reason="rootfs_mismatch")
        self.assertEqual(calls["elevate"], [])

    def test_non_rootfs_success_omits_destructive_flag(self):
        method, owner, calls = self._make(
            reason=None, consent=True, elevate=(0, False, None),
            distro_registered=True)
        self._run(method, owner)
        self.assertEqual(len(calls["elevate"]), 1)
        self.assertNotIn("-AllowDestructiveReimport", calls["elevate"][0])


class FinalizeExitContractTest(unittest.TestCase):
    """The finalize exit codes must agree across all three files that speak them.

    The 2026-07-17 incident was exactly this contract drifting apart while each
    half looked locally correct. The chain is:
        import_edubotics_wsl.ps1  exit 12  (refuses an unconsented wipe)
          -> finalize_install.ps1 $EXIT_CONSENT  (passes it through)
            -> gui_app.py FINALIZE_EXIT_CONSENT  (routes it to the German remedy)
    """

    def test_ps1_and_gui_exit_codes_match(self):
        ps1 = _ps1_exit_codes(_FINALIZE_PS1)
        gui = _gui_exit_codes()
        for short, long in (("DONE", "FINALIZE_EXIT_DONE"),
                            ("REBOOT", "FINALIZE_EXIT_REBOOT"),
                            ("CONSENT", "FINALIZE_EXIT_CONSENT")):
            self.assertIn(short, ps1, f"$EXIT_{short} missing from finalize_install.ps1")
            self.assertIn(long, gui, f"{long} missing from gui_app.py")
            self.assertEqual(
                ps1[short], gui[long],
                f"$EXIT_{short}={ps1[short]} but {long}={gui[long]} — the GUI would "
                f"misroute this outcome")

    def test_wire_values_are_pinned(self):
        # Pin the actual integers: the routing tests read these constants, so
        # without this a wrong-but-consistent value would pass everything.
        self.assertEqual(_gui_exit_codes(), {
            "FINALIZE_EXIT_DONE": 0,
            "FINALIZE_EXIT_REBOOT": 10,
            "FINALIZE_EXIT_CONSENT": 12,
        })

    def test_failed_code_is_distinct_from_the_routed_ones(self):
        ps1 = _ps1_exit_codes(_FINALIZE_PS1)
        self.assertIn("FAILED", ps1)
        # EXIT_FAILED has no GUI mirror on purpose — it is the else branch. It
        # must not collide with a code that routes somewhere specific.
        self.assertNotIn(ps1["FAILED"],
                         [ps1["DONE"], ps1["REBOOT"], ps1["CONSENT"]])

    def test_import_consent_refusal_code_matches_finalize(self):
        # import_edubotics_wsl.ps1 emits the bare literal `exit 12`; finalize
        # keys its $EXIT_CONSENT pass-through off that exact number.
        import_src = _read(_IMPORT_PS1, encoding="utf-8-sig")
        consent = _ps1_exit_codes(_FINALIZE_PS1)["CONSENT"]
        self.assertRegex(
            import_src, rf"(?m)^\s*exit {consent}\s*$",
            f"import_edubotics_wsl.ps1 no longer exits {consent} on a refused "
            f"destructive re-import — finalize's $EXIT_CONSENT mapping is dead")

    def test_every_reboot_announcement_exits_the_reboot_code(self):
        """Telling the student to reboot and exiting a non-reboot code is the N1
        bug in miniature: the GUI would then say "Einrichtung fehlgeschlagen" on
        a genuine pending reboot. Pin the German announcement to the exit code —
        a mutation swapping both `exit $EXIT_REBOOT` sites for $EXIT_FAILED
        survived the suite until this test existed."""
        lines = _read(_FINALIZE_PS1, encoding="utf-8-sig").splitlines()
        announcements = [i for i, ln in enumerate(lines)
                         if "NEUSTART ERFORDERLICH" in ln]
        self.assertTrue(announcements,
                        "finalize no longer announces a required reboot at all")
        for i in announcements:
            following = next((ln.strip() for ln in lines[i + 1:] if ln.strip()), "")
            self.assertEqual(
                following, "exit $EXIT_REBOOT",
                f"line {i + 1} tells the student to reboot but is followed by "
                f"{following!r} — the GUI routes on the exit code, so this "
                f"outcome would be reported as something else")

    def test_finalize_consent_branch_actually_exits_the_consent_code(self):
        """finalize must PROPAGATE import's refusal, not flatten it into 1.

        Asserting only that the `$importRc -eq 12` branch exists is vacuous: a
        mutation swapping its `exit $EXIT_CONSENT` back to `exit $EXIT_FAILED`
        survived the whole suite, which is precisely how the GUI stopped being
        able to tell "needs consent" from "broke" in the first place. Pin the
        branch condition and its exit TOGETHER."""
        src = _read(_FINALIZE_PS1, encoding="utf-8-sig")
        consent = _ps1_exit_codes(_FINALIZE_PS1)["CONSENT"]
        m = re.search(r"if \(\$importRc -eq (\d+)\) \{(.*?)^    \}",
                      src, re.S | re.M)
        self.assertIsNotNone(
            m, "finalize_install.ps1 no longer maps import's consent-refusal code")
        self.assertEqual(
            int(m.group(1)), consent,
            "the branch tests a different rc than $EXIT_CONSENT propagates")
        self.assertIn(
            "exit $EXIT_CONSENT", m.group(2),
            "the consent-refusal branch must exit $EXIT_CONSENT — anything else "
            "collapses the 'run the installer again' remedy into a generic "
            "failure the student cannot act on")


class ScriptTerminalExitTest(unittest.TestCase):
    """The chained installer scripts must end with an explicit `exit 0`.

    Each of these ends with a COSMETIC native probe (`docker --version`,
    `usbipd --version`). Under EAP=Continue a non-throwing native failure never
    enters the catch and never resets $LASTEXITCODE, and the trailing cmdlets
    don't either — so without a terminal `exit 0` the probe's rc silently BECOMES
    the script's exit code, and finalize reports a failed import / failed
    prerequisites over a run that actually succeeded. pull_images.ps1 has guarded
    this for a while; these two did not."""

    def test_scripts_end_with_explicit_exit_zero(self):
        for name in ("import_edubotics_wsl.ps1", "install_prerequisites.ps1",
                     "pull_images.ps1"):
            with self.subTest(script=name):
                src = _read(os.path.join(_SCRIPTS, name), encoding="utf-8-sig")
                lines = [ln.strip() for ln in src.splitlines() if ln.strip()]
                self.assertTrue(
                    lines and lines[-1] == "exit 0",
                    f"{name} must end with an explicit `exit 0`; last statement "
                    f"is {lines[-1]!r}")


class RootCauseGuardTest(unittest.TestCase):
    """Guards the three LOAD-BEARING fixes that a final review proved were
    completely unprotected: deleting any of them left the whole suite green.

    The pattern that made this necessary is worth naming: every SECONDARY fix in
    this change set got a strong guard, while the PRIMARY one — the actual root
    cause of the v2.13.0 pilot incident — had none. A regression here reproduces
    the original outage, silently.
    """

    @staticmethod
    def _code(name):
        """Script source with COMMENT LINES STRIPPED.

        Load-bearing: a fix's own rationale necessarily names the thing it
        guards against (`dism /enable-feature`, `NTAccount("Users")`, …), so an
        un-stripped source guard pins the DOCUMENTATION and reports a false
        ordering. This exact trap produced several vacuous checks while this
        change set was written."""
        src = _read(os.path.join(_SCRIPTS, name), encoding="utf-8-sig")
        return "\n".join(ln for ln in src.splitlines()
                         if not ln.lstrip().startswith("#"))

    # ── 1. The root cause itself ────────────────────────────────────────────
    # install_prerequisites.ps1 must STATE-CHECK the WSL/VMP features before
    # touching dism. Running the enable unconditionally is what produced the
    # incident: with an unrelated Windows-Update servicing op pending, a no-op
    # enable of an ALREADY-ENABLED feature returns rc=3010, which was read as
    # "our feature needs a reboot" -> a spurious .reboot_required -> the .iss
    # gates skipped the image pull + distro import.
    def test_dism_is_state_checked_before_enable(self):
        code = self._code("install_prerequisites.ps1")
        self.assertIn("Get-WindowsOptionalFeature", code,
                      "the feature STATE must be probed before dism, or an "
                      "unrelated pending servicing op resurrects the spurious "
                      "rc=3010 -> .reboot_required -> skipped pull")
        self.assertLess(code.index("Get-WindowsOptionalFeature"),
                        code.index("/enable-feature"),
                        "the state probe must PRECEDE dism /enable-feature")
        self.assertRegex(code, r'\$featureState -eq "Enabled"',
                         "an already-Enabled feature must skip dism entirely")

    def test_unreadable_feature_store_does_not_manufacture_a_reboot(self):
        # The pilot's exact state: Get-WindowsOptionalFeature throws a
        # COMException while servicing is pending. That must NOT fall through to
        # dism (which would 3010) — `wsl --status` is authoritative instead.
        code = self._code("install_prerequisites.ps1")
        catch = code.index("Get-WindowsOptionalFeature failed")
        enable = code.index("/enable-feature")
        self.assertLess(catch, enable)
        self.assertIn("continue", code[catch:enable],
                      "an unreadable feature store must `continue`, never fall "
                      "through to dism")

    # ── 2. The GUI's reboot-pending routing ─────────────────────────────────
    def test_gui_routes_reboot_pending_to_finalize_with_latch(self):
        src = _read(_GUI_SRC)
        self.assertIn(
            "if self._reboot_required_pending() and not self._finalize_completed:",
            src,
            "the prereq check must route a pending-reboot install to finalize, "
            "AND honour the _finalize_completed latch — without the latch a "
            "flag finalize merely failed to delete loops UAC forever")

    # ── 3. The diagnostics ACL ──────────────────────────────────────────────
    # Two independent ways to break this, BOTH silent (the block is wrapped in
    # `catch { }`): a localized account name throws on German Windows (the log
    # then never writes), and dropping NoPropagateInherit lets Users:Modify
    # inherit onto %ProgramData%\EduBotics\wsl\ext4.vhdx — i.e. every student on
    # a lab PC could tamper with the distro image.
    _ACL_SCRIPTS = ("install_prerequisites.ps1", "migrate_from_docker_desktop.ps1",
                    "verify_system.ps1", "configure_usbipd.ps1", "bind_devices.ps1")

    def test_acl_uses_the_well_known_sid_never_a_localized_name(self):
        for name in self._ACL_SCRIPTS:
            code = self._code(name)
            with self.subTest(script=name):
                self.assertIn('SecurityIdentifier("S-1-5-32-545")', code,
                              "must use the well-known Users SID — this ships on "
                              "German Windows, where NTAccount('Users') throws "
                              "and the catch{} swallows it, so the log silently "
                              "never writes")
                self.assertNotIn('NTAccount("Users")', code)
                self.assertNotIn("NTAccount('Users')", code)

    def test_acl_cannot_inherit_onto_the_distro_vhdx(self):
        for name in self._ACL_SCRIPTS:
            code = self._code(name)
            with self.subTest(script=name):
                self.assertIn("NoPropagateInherit", code,
                              "without NoPropagateInherit the Users:Modify grant "
                              "inherits onto %ProgramData%\\EduBotics\\wsl\\"
                              "ext4.vhdx — any standard user could tamper with "
                              "the distro image")
                self.assertNotIn("ContainerInherit,ObjectInherit", code,
                                 "ContainerInherit propagates into wsl\\ — that "
                                 "was the bug")


class UacCancelDetectionTest(unittest.TestCase):
    """The UAC-decline DETECTION — which the routing tests structurally cannot see.

    ctypes.get_last_error() reports a Win32 error ONLY for calls made through a
    handle loaded with use_last_error=True. gui_app used the cached, flag-LESS
    `ctypes.windll.shell32`, so get_last_error() returned 0 unconditionally, the
    ERROR_CANCELLED branch was DEAD CODE, and a student who DECLINED the UAC
    prompt was told "Einrichtung fehlgeschlagen (exit None)" — a crash report for
    a choice they made themselves.

    Why assert on SOURCE rather than behaviour: `_elevate_and_wait` early-returns
    off win32, and `ctypes.wintypes` cannot even be imported on Linux — so a
    behavioural test would SKIP on the Linux CI runner, i.e. exactly where the
    regression would land unnoticed. The routing tests above mock this function's
    RETURN value, so they can never catch a detection bug either. Between them,
    the bug had nowhere to be caught; this class is that place.
    """

    def test_shell32_is_loaded_with_use_last_error(self):
        self.assertIn(
            'ctypes.WinDLL("shell32", use_last_error=True)', _elevate_fn_src(),
            "shell32 must be loaded with use_last_error=True, or "
            "ctypes.get_last_error() always returns 0 and the ERROR_CANCELLED "
            "branch is unreachable")

    def test_flagless_windll_shell32_is_not_used(self):
        # Strip comments first: the fix's own rationale necessarily NAMES the
        # old API to explain the bug, and matching that would fail on the
        # documentation rather than the code.
        code = "\n".join(ln for ln in _elevate_fn_src().splitlines()
                         if not ln.lstrip().startswith("#"))
        self.assertNotIn(
            "windll.shell32", code,
            "ctypes.windll.shell32 is the CACHED, flag-less handle — using it "
            "silently disables ctypes.get_last_error() for ShellExecuteExW")

    def test_error_cancelled_maps_to_cancelled_true(self):
        self.assertRegex(
            _elevate_fn_src(),
            r"if err == ERROR_CANCELLED:\s*\n\s*return None, True,",
            "ERROR_CANCELLED must map to cancelled=True so the caller can "
            "report 'abgebrochen' instead of a generic failure")

    def test_cancel_error_text_cannot_fake_the_routing_assertion(self):
        # Guards the vacuity that hid this bug: the routing test asserts on
        # "abgebrochen" in the log, and the caller echoes `UAC-Fehler: {err}`.
        # If this function's cancel text ever contains "abgebrochen" again, that
        # echo alone satisfies the routing test and the branch can rot away.
        m = re.search(r"return None, True, \"([^\"]+)\"", _elevate_fn_src())
        self.assertIsNotNone(m, "cancel return not found")
        self.assertNotIn("abgebrochen", m.group(1).lower())


if __name__ == "__main__":
    unittest.main()

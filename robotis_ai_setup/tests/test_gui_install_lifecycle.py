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
    unreadable-distro, empty-distro, and absent-shipped-file.
  * _prompt_finalize_install / _run_elevated — the EXIT-CODE routing for all
    six finalize outcomes (done / done-but-distro-absent / reboot-still-needed /
    failed / consent-refused / UAC-cancelled), the rootfs-mismatch destructive
    consent that threads -AllowDestructiveReimport, and the decline path.
  * FinalizeExitContractTest — the exit codes agree across all three files that
    speak them (import_edubotics_wsl.ps1 -> finalize_install.ps1 -> gui_app.py).
  * PrerequisiteLifecycleTeardownTest — ensure_environment_stopped (the SOLE
    lifecycle-enforcement point) runs before EVERY early return that follows a
    distro boot, the rootfs gate included.
  * PrerequisiteReentrancyTest — two concurrent prerequisite scans (two 15-30 min
    pulls + two racing prunes) are refused, and the guard survives a raise.
  * DiagnosticsSinkTest — the GUI and device_manager resolve the SAME diagnostics
    directory, and it falls back on UNWRITABILITY, not just an unset var.

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

import contextlib
import os
import re
import shutil
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

from gui.app import constants, device_manager  # noqa: E402

_GUI_SRC = os.path.join(os.path.dirname(__file__), "..", "gui", "app", "gui_app.py")
_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "installer", "scripts")
_FINALIZE_PS1 = os.path.join(_SCRIPTS, "finalize_install.ps1")
_IMPORT_PS1 = os.path.join(_SCRIPTS, "import_edubotics_wsl.ps1")
_VIRT_PS1 = os.path.join(_SCRIPTS, "virtualization_ready.ps1")


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


def _ps1_function_body(code, name):
    r"""Body of `function <name> { ... }` from (comment-stripped) .ps1 source.

    Brace-counting, not a regex: these bodies contain nested `try`/`foreach`
    blocks, and a lazy `.*?\}` would stop at the first inner closing brace and
    silently shrink the window an ordering assertion is made inside."""
    start = code.index("function %s {" % name)
    i = code.index("{", start)
    depth = 0
    for j in range(i, len(code)):
        if code[j] == "{":
            depth += 1
        elif code[j] == "}":
            depth -= 1
            if depth == 0:
                return code[i:j + 1]
    raise AssertionError("unbalanced braces in %s" % name)


def _module_fn_src(name):
    """Source of a module-level `def <name>(...)` from gui_app.py."""
    src = _read(_GUI_SRC)
    start = src.index(f"def {name}(")
    end = src.index("\ndef ", start + 1)
    return src[start:end]


def _elevate_fn_src():
    """Source of the module-level `_elevate_and_wait()` from gui_app.py."""
    return _module_fn_src("_elevate_and_wait")


@contextlib.contextmanager
def _env(**overrides):
    """Temporarily set / REMOVE (value None) environment variables."""
    with patch.dict(os.environ, {}, clear=False):
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield


def _method_src(method_name):
    """Dedented source of one EduBoticsApp method from gui_app.py."""
    source = _read(_GUI_SRC)
    marker = f"    def {method_name}(self"
    start = source.index(marker)
    rest = source[start:]
    end = rest.find("\n    def ", len(marker))
    return textwrap.dedent(rest[: end if end != -1 else len(rest)])


def _load_method(method_name, ns):
    """Extract `method_name` from gui_app.py and exec it into ``ns``.

    ``ns`` becomes the function's globals; set ``ns["__package__"]`` so a local
    ``from .constants import ...`` resolves. Returns the callable."""
    snippet = _method_src(method_name)
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
    fails OPEN on every ambiguity (the classroom-hiccup guard).

    The four cells below are THE contract, and both halves of the handshake must
    agree on them: the GUI here and the .iss RootfsReimportNeeded(). They
    diverged once — the .iss treated an unreadable stamp as "needs re-import"
    (fail CLOSED) while the GUI proceeded (fail OPEN), so on exactly the state a
    broken rig is in, one half offered a destructive volume-wiping rebuild the
    other said was unnecessary. Converged on fail-open; do not move one side
    alone."""

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

    def test_empty_distro_stamp_fails_open(self):
        # read_rootfs_version() normally returns None, but a "" return (a stamp
        # file that exists and is blank) must fail open through the same branch
        # — `not distro` covers both, and an `is None` check would not.
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(self._run(d, shipped="2", distro=""))

    def test_both_stamps_unreadable_fails_open(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(self._run(d, shipped=None, distro=None))

    def test_docstring_no_longer_claims_the_stale_iss_mirror(self):
        """The docstring is load-bearing here: it is the ONLY place the
        cross-file contract with the .iss is written down, and it went stale
        while the two halves silently disagreed. Pin the false claim out."""
        doc = _method_src("_rootfs_rebuild_required")
        self.assertNotIn(
            "ShouldImportDistro version comparison", doc,
            "the .iss gate this claimed to mirror is RootfsReimportNeeded(), and "
            "the 'both '' -> skip' equivalence was false — say what the contract "
            "actually is")
        self.assertIn("fail", doc.lower())
        self.assertIn("positive mismatch", doc.lower())


class PromptFinalizeInstallTest(unittest.TestCase):
    """_prompt_finalize_install + _run_elevated: dialog routing, the
    flag/exit-code success discrimination, and the rootfs destructive consent."""

    def _make(self, *, reason=None, consent=True, elevate=(0, False, None),
              reboot_pending=False, distro_registered=True, script="finalize.ps1",
              marker=None, marker_encoding="utf-8-sig", marker_as_dir=False,
              transcript=None, diag_dir=None):
        calls = {"elevate": [], "log": [], "status": [], "prereq": 0,
                 "showinfo": [], "showwarning": [], "showerror": [],
                 "askyesno": [], "fallback": []}

        def _fake_elevate(exe, args, show=1):
            calls["elevate"].append(args)
            # Stand in for finalize_install.ps1 writing its marker. The path is
            # taken out of the command line the GUI actually built, so a rename
            # of -MarkerPath breaks the fixture instead of silently making every
            # marker-reading test read nothing. `marker=None` = never launched /
            # marker deleted, which is what _run_elevated leaves behind.
            #
            # The DEFAULT encoding is utf-8-sig because that is the byte shape
            # PS 5.1's `Set-Content -Encoding UTF8` produces (it emits a BOM).
            # `marker_encoding="cp1252"` reproduces the pre-fix ANSI write.
            #
            # `marker_as_dir=True` puts a DIRECTORY where the marker belongs —
            # the cheapest reproduction of an OSError that is NOT
            # FileNotFoundError (IsADirectoryError on POSIX, PermissionError on
            # Windows). See test_an_unreadable_marker_path_cannot_raise.
            # Stand in for finalize_install.ps1's Start-Transcript. Taken out of
            # the command line the GUI actually built, like the marker, so a
            # rename of -LogPath breaks the fixture instead of silently leaving
            # the transcript branch unexercised — which is exactly the state this
            # harness was in: no fixture ever created the log file, so the
            # _transcript_excerpt call inside _run_elevated was never executed and
            # a NameError there would have shipped.
            if transcript is not None:
                m = re.search(r'-LogPath "([^"]+)"', args)
                assert m, f"no -LogPath in the built args: {args}"
                with open(m.group(1), "w", encoding="utf-8") as fh:
                    fh.write(transcript)
            if marker_as_dir:
                m = re.search(r'-MarkerPath "([^"]+)"', args)
                assert m, f"no -MarkerPath in the built args: {args}"
                os.makedirs(m.group(1), exist_ok=True)
            elif marker is not None:
                m = re.search(r'-MarkerPath "([^"]+)"', args)
                assert m, f"no -MarkerPath in the built args: {args}"
                with open(m.group(1), "w", encoding=marker_encoding,
                          errors="replace") as fh:
                    fh.write(marker)
            return elevate

        def _fake_askyesno(*a, **k):
            calls["askyesno"].append(a)
            return consent

        fake_mb = types.SimpleNamespace(
            askyesno=_fake_askyesno,
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
            # The merged elevation surface: _prompt_finalize_install invokes
            # _run_privileged (direct when already elevated, UAC otherwise);
            # stub both names to the same recorder so the routing tests keep
            # exercising the SAME return contract either way.
            "_elevate_and_wait": _fake_elevate,
            "_run_privileged": _fake_elevate,
            "_is_elevated": lambda: False,
            "_edubotics_diag_dir": lambda: diag_dir or tempfile.gettempdir(),
            "docker_manager": fake_dm,
            "threading": types.SimpleNamespace(Thread=_SyncThread),
            "__package__": "gui.app",
        }
        # The real exit-code constants, not literals — see _gui_exit_codes().
        ns.update(_gui_exit_codes())
        # The REAL _transcript_excerpt, exec'd from gui_app.py: _load_method only
        # execs the method, so a module-level helper it calls must be supplied or
        # the call site raises NameError at runtime and never in the suite.
        exec(compile(_module_fn_src("_transcript_excerpt"), _GUI_SRC, "exec"), ns)
        method = _load_method("_prompt_finalize_install", ns)
        owner = types.SimpleNamespace(
            _resolve_finalize_script=lambda: script,
            _reboot_required_pending=lambda: reboot_pending,
            _finalize_completed=False,
            _finalize_in_progress=False,
            _elevation_prewarn=lambda: False,
            _show_manual_elevation_fallback=lambda title, cmd: calls[
                "fallback"].append((title, cmd)),
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

    def test_default_path_auto_runs_without_dialog(self):
        # The non-destructive path runs AUTOMATICALLY (no consent dialog — the
        # student never clicks a "finish setup" button). consent=False proves
        # the outcome cannot depend on a dialog answer: no dialog is shown.
        method, owner, calls = self._make(consent=False,
                                          elevate=(0, False, None),
                                          distro_registered=True)
        self._run(method, owner)
        self.assertEqual(calls["askyesno"], [], "no dialog on the default path")
        self.assertEqual(len(calls["elevate"]), 1)

    def test_rootfs_mismatch_decline_logs_verschoben_and_clears_guard(self):
        # The ONE remaining dialog: destructive rootfs re-import. Decline must
        # not elevate, must log the German deferral, and must clear the
        # re-entrancy guard so a later attempt is not locked out.
        method, owner, calls = self._make(reason="rootfs_mismatch",
                                          consent=False)
        self._run(method, owner, reason="rootfs_mismatch")
        self.assertEqual(calls["elevate"], [])
        self.assertTrue(any("verschoben" in m for m in calls["log"]))
        self.assertFalse(owner._finalize_in_progress)

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

    # The previous attempt's transcript must SURVIVE the next launch. Before
    # 2026-09-11 this method deleted edubotics_finalize.log before starting
    # finalize, so finalize_install.ps1's own rotation found nothing at -LogPath
    # and `.prev.log` never existed on the one production path that passes it.
    def _diag_with_previous_attempt(self):
        diag = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, diag, True)
        with open(os.path.join(diag, "edubotics_finalize.log"), "w",
                  encoding="utf-8") as fh:
            fh.write("VORHERIGER VERSUCH: HCS_E_SERVICE_NOT_AVAILABLE\n")
        return diag

    def test_the_previous_transcript_is_rotated_not_deleted(self):
        diag = self._diag_with_previous_attempt()
        method, owner, calls = self._make(
            elevate=(10, False, None), reboot_pending=True, diag_dir=diag,
            transcript="NEUER VERSUCH\n")
        self._run(method, owner)
        rotated = os.path.join(diag, "edubotics_finalize.prev.log")
        self.assertTrue(os.path.isfile(rotated),
                        "the attempt BEFORE this one is the only record of what "
                        "changed on a rig that loops")
        with open(rotated, encoding="utf-8") as fh:
            self.assertIn("VORHERIGER VERSUCH", fh.read())
        logged = "\n".join(calls["log"])
        self.assertIn("NEUER VERSUCH", logged)
        self.assertNotIn("VORHERIGER VERSUCH", logged,
                         "the rotated file is evidence for support, never this "
                         "launch's output")

    def test_a_launch_that_never_ran_shows_no_stale_transcript(self):
        """Emptying -LogPath before the launch is what keeps a stale
        transcript from being presented as this run's result; rotating must
        preserve that, not trade it for the evidence."""
        diag = self._diag_with_previous_attempt()
        method, owner, calls = self._make(
            elevate=(None, False, "Direkter Start fehlgeschlagen"),
            reboot_pending=True, diag_dir=diag)
        self._run(method, owner)
        self.assertNotIn("VORHERIGER VERSUCH", "\n".join(calls["log"]))
        self.assertTrue(os.path.isfile(
            os.path.join(diag, "edubotics_finalize.prev.log")))

    def test_gui_and_script_agree_on_the_rotated_name(self):
        """Two rotators, one name. finalize spells it with .NET's
        ChangeExtension($LogPath, '.prev.log'); the GUI with splitext. For the
        one -LogPath the GUI passes, both must land on the SAME file, or support
        reads a .prev.log the other side never writes."""
        fin = _read(_FINALIZE_PS1, encoding="utf-8-sig")
        self.assertIn("[System.IO.Path]::ChangeExtension($LogPath, '.prev.log')",
                      fin)
        gui = _method_src("_prompt_finalize_install")
        self.assertIn('os.path.splitext(log_file)[0] + ".prev.log"', gui)
        self.assertIn('"edubotics_finalize.log"', gui)
        # ChangeExtension replaces the LAST extension, exactly like splitext.
        self.assertEqual(
            os.path.splitext(r"C:\ProgramData\EduBotics\logs\edubotics_finalize.log")[0]
            + ".prev.log",
            r"C:\ProgramData\EduBotics\logs\edubotics_finalize.prev.log")

    def test_the_transcript_excerpt_call_site_actually_resolves(self):
        """Walk the transcript branch of _run_elevated for real.

        Every other test here leaves the log file absent, so `os.path.isfile`
        short-circuits and the excerpt call is never executed. A source grep
        cannot see a NameError; this does."""
        header = ("**********************\n"
                  "Benutzername: SCHULE\\schueler01\n"
                  "Computer: PC-RAUM-12 (Microsoft Windows NT 10.0.26100.0)\n"
                  "WSManStackVersion: 3.0\n"
                  "**********************\n")
        body = header + "".join(f"Zeile {i}\n" for i in range(120))
        method, owner, calls = self._make(
            elevate=(1, False, None), reboot_pending=True, transcript=body)
        self._run(method, owner)
        logged = "\n".join(calls["log"])
        self.assertIn("── Setup-Protokoll ──", logged)
        self.assertIn("Benutzername: SCHULE\\schueler01", logged,
                      "the invocation header must reach the Protokoll — that is "
                      "the whole point of the head half")
        self.assertIn("Zeile 119", logged, "the tail must still be the tail")
        self.assertNotIn("WSManStackVersion", logged,
                         "and the noise must still be dropped")
        self.assertTrue(any("ausgelassen" in m for m in calls["log"]))

    # Outcome 6/6 (2026-09-11): the PC has no usable hypervisor. Rebooting MIGHT
    # fix it and enabling VT-x in the BIOS/UEFI might be what is actually needed —
    # `wsl` reports the same HCS_E_SERVICE_NOT_AVAILABLE for both — so the student
    # gets both remedies, reboot first. Before this code existed the outcome fell
    # into the generic else: "Einrichtung fehlgeschlagen (exit 1)" plus advice to
    # check free disk space, on a machine whose 20 GB precheck had just passed.
    def test_exit11_shows_the_virtualization_remedy(self):
        method, owner, calls = self._make(
            elevate=(11, False, None), reboot_pending=True,
            distro_registered=False)
        self._run(method, owner)
        self.assertEqual(calls["prereq"], 0)  # did NOT proceed
        self.assertTrue(calls["showwarning"],
                        "the student needs a modal, not only a log line")
        self.assertEqual(calls["showinfo"], [],
                         "must NOT reuse the plain reboot modal — the BIOS half "
                         "of the remedy would be lost")
        joined = " ".join(calls["log"])
        self.assertIn("Virtualisierung", joined)
        self.assertIn("BIOS/UEFI", joined,
                      "the second remedy must be named; a student cannot reboot "
                      "their way out of a disabled BIOS setting")
        self.assertIn("neu starten", joined,
                      "the FREE remedy must still be offered, and first")
        self.assertFalse(
            any("fehlgeschlagen" in m for m in calls["log"]),
            "this must not read as a generic failure — it has two concrete "
            "remedies")
        self.assertTrue(any("Virtualisierung" in st for st in calls["status"]))

    def test_exit11_is_not_mistaken_for_a_plain_reboot(self):
        """10 and 11 must stay distinguishable at the GUI.

        Folding 11 into the reboot branch is the tempting simplification and it
        is wrong: the reboot modal never mentions the BIOS, so a student on a
        VT-x-disabled PC would reboot forever."""
        m10, o10, reboot_calls = self._make(elevate=(10, False, None),
                                           reboot_pending=True)
        self._run(m10, o10)
        m11, o11, virt_calls = self._make(elevate=(11, False, None),
                                          reboot_pending=True)
        self._run(m11, o11)
        self.assertNotIn("BIOS", " ".join(reboot_calls["log"]))
        self.assertIn("BIOS", " ".join(virt_calls["log"]))

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

    # Outcome 6/6: exit 0 but the distro is STILL absent. is_distro_registered()
    # alone must NOT be trusted as success (the W2 upgrade wrinkle), so this is
    # NOT a success — but it must not fall through to the generic else either,
    # which printed the self-contradicting "Einrichtung fehlgeschlagen (exit 0)".
    def test_exit0_but_distro_missing_reports_the_contradiction_honestly(self):
        method, owner, calls = self._make(
            elevate=(0, False, None), distro_registered=False)
        self._run(method, owner)
        self.assertEqual(calls["prereq"], 0)
        self.assertFalse(owner._finalize_completed)
        self.assertTrue(
            any("meldet Erfolg" in m and "fehlt weiterhin" in m
                for m in calls["log"]),
            f"expected the honest 'reports success but the environment is still "
            f"missing' message: {calls['log']}")
        # The old wording is a contradiction the student cannot act on.
        self.assertFalse(
            any("(exit 0)" in m for m in calls["log"]),
            f"'fehlgeschlagen (exit 0)' is self-contradictory: {calls['log']}")
        # ... and it must carry a concrete next step, not just a diagnosis.
        self.assertTrue(any("neu starten" in m for m in calls["log"]))
        self.assertTrue(any("Installer erneut" in m for m in calls["log"]))

    # ── exit 0 + distro invisible: WHICH of the two causes? ─────────────
    # WSL2 registers distros PER WINDOWS USER (HKCU\...\Lxss) and the installer
    # is PrivilegesRequired=admin, so on a managed school PC where a DIFFERENT
    # admin elevates, the import lands in the admin's hive and the student's
    # un-elevated GUI cannot see it. FINALIZE_EXIT_DONE proves finalize's own
    # Test-DistroRegistered PASSED (every other outcome exits through
    # Fail-WithNextAction), so exit 0 + invisible + a different `user=` in the
    # marker IS the per-account split — and rebooting, which the generic message
    # advises, can never fix it. Same user / no marker keeps the old message.
    _SUCCESS_MARKER = "SUCCESS 2026-08-02T10:00:00.0000000+02:00 user={0} distro=EduBotics"

    def _run_exit0_invisible(self, *, marker, username,
                             marker_encoding="utf-8-sig"):
        method, owner, calls = self._make(
            elevate=(0, False, None), distro_registered=False, marker=marker,
            marker_encoding=marker_encoding)
        with _env(USERNAME=username):
            self._run(method, owner)
        return calls

    @staticmethod
    def _said_wrong_account(calls):
        return any("Windows-Konto" in m for m in calls["log"])

    @staticmethod
    def _said_generic(calls):
        return any("meldet Erfolg" in m and "fehlt weiterhin" in m
                   for m in calls["log"])

    def test_marker_user_matching_current_user_keeps_the_old_message(self):
        calls = self._run_exit0_invisible(
            marker=self._SUCCESS_MARKER.format("student"), username="student")
        self.assertTrue(self._said_generic(calls), calls["log"])
        self.assertFalse(self._said_wrong_account(calls), calls["log"])

    def test_marker_user_differing_reports_the_per_account_cause(self):
        calls = self._run_exit0_invisible(
            marker=self._SUCCESS_MARKER.format("schuladmin"), username="student")
        self.assertFalse(self._said_generic(calls), calls["log"])
        self.assertTrue(self._said_wrong_account(calls), calls["log"])
        # It must NAME both accounts — "wrong account" without saying WHICH one
        # is not actionable on a PC the student did not set up.
        self.assertTrue(any("schuladmin" in m and "student" in m
                            for m in calls["log"]), calls["log"])
        # ... and must not repeat the reboot advice, which cannot help here.
        self.assertFalse(any("neu starten" in m for m in calls["log"]),
                         calls["log"])
        self.assertTrue(any("Windows-Konto" in s for s in calls["status"]),
                        calls["status"])

    def test_account_names_are_compared_case_insensitively(self):
        # Windows account names are case-insensitive; a case difference is the
        # SAME account and must not be reported as a mismatch.
        calls = self._run_exit0_invisible(
            marker=self._SUCCESS_MARKER.format("Student"), username="student")
        self.assertTrue(self._said_generic(calls), calls["log"])
        self.assertFalse(self._said_wrong_account(calls), calls["log"])

    def test_a_username_with_spaces_survives_the_distro_suffix(self):
        # %USERNAME% can contain spaces and `user=` is NOT last on the SUCCESS
        # line, so the parse has to cut at " distro=" rather than at whitespace.
        calls = self._run_exit0_invisible(
            marker=self._SUCCESS_MARKER.format("Max Muster"), username="student")
        self.assertTrue(any("Max Muster" in m for m in calls["log"]),
                        calls["log"])
        self.assertFalse(any("distro=" in m for m in calls["log"]),
                         calls["log"])

    # ── the ENCODING pairing: -Encoding UTF8 written, utf-8-sig read ────
    # The target population is German schools, so an umlaut in %USERNAME% is
    # ordinary. Both halves are pinned: the .ps1 must write UTF-8, and the
    # reader must refuse a name that came back with a replacement character
    # rather than treat it as "a different account".
    def test_an_umlaut_username_round_trips_and_is_named_verbatim(self):
        calls = self._run_exit0_invisible(
            marker=self._SUCCESS_MARKER.format("Jörg Müller"),
            username="student")
        self.assertTrue(self._said_wrong_account(calls), calls["log"])
        self.assertTrue(any("Jörg Müller" in m for m in calls["log"]),
                        calls["log"])
        self.assertFalse(any("�" in m for m in calls["log"]),
                         "a replacement character reached the student")

    def test_the_same_umlaut_account_is_not_reported_as_a_mismatch(self):
        # The bug in its purest form: one account, written and read as itself.
        calls = self._run_exit0_invisible(
            marker=self._SUCCESS_MARKER.format("Müller"), username="Müller")
        self.assertTrue(self._said_generic(calls), calls["log"])
        self.assertFalse(self._said_wrong_account(calls), calls["log"])

    def test_an_ansi_written_marker_falls_back_instead_of_accusing(self):
        """A cp1252 „Müller" decodes to „M�ller" — which casefold-compares
        UNEQUAL to every real %USERNAME%, so the per-account branch fired on the
        SAME account and replaced correct reboot advice with advice that cannot
        help. A U+FFFD anywhere in the parsed name means "unparseable", never
        "someone else": accusing a student of the wrong Windows login on a PC
        they did not set up is worse than the generic message."""
        calls = self._run_exit0_invisible(
            marker=self._SUCCESS_MARKER.format("Müller"), username="Müller",
            marker_encoding="cp1252")
        self.assertTrue(self._said_generic(calls), calls["log"])
        self.assertFalse(self._said_wrong_account(calls), calls["log"])

    def test_finalize_writes_every_marker_as_utf8(self):
        """The .ps1 half. PS 5.1's Set-Content default is the system ANSI
        codepage, so an explicit -Encoding is the only thing that makes the
        GUI's UTF-8 read correct. All THREE writes, not just the one the GUI
        parses today — one file, one reader, one encoding."""
        src = _read(_FINALIZE_PS1, encoding="utf-8-sig")
        writes = [ln.strip() for ln in src.splitlines()
                  if "Set-Content" in ln and "$MarkerPath" in ln]
        self.assertEqual(len(writes), 3,
                         f"expected the started/FAILED/SUCCESS writes, got "
                         f"{writes}")
        for ln in writes:
            self.assertIn("-Encoding UTF8", ln,
                          f"marker write without an explicit encoding: {ln}")

    def test_the_legacy_started_marker_shape_is_still_parsed(self):
        # An install that has not yet taken the new finalize_install.ps1 leaves
        # the startup stamp behind: "started <iso> pid=<n> user=<name>", where
        # user= runs to end-of-line. It carries the same fact.
        calls = self._run_exit0_invisible(
            marker="started 2026-08-02T10:00:00Z pid=4711 user=schuladmin",
            username="student")
        self.assertTrue(self._said_wrong_account(calls), calls["log"])

    def test_missing_marker_keeps_the_old_message(self):
        calls = self._run_exit0_invisible(marker=None, username="student")
        self.assertTrue(self._said_generic(calls), calls["log"])
        self.assertFalse(self._said_wrong_account(calls), calls["log"])

    def test_an_unreadable_marker_path_cannot_raise(self):
        """_marker_user's docstring says it NEVER raises, and that is a promise
        about the whole finalize report, not about the parse.

        It is called from `_finalize_worker`, whose only wrapper is
        `_run_elevated`'s try/FINALLY — there is no `except`. So an escaping
        exception skips the German verdict, the transcript echo and the
        re-check, and on a synchronous path takes the caller with it: the
        student gets nothing at all instead of a diagnosis.
        `except FileNotFoundError` looks like the same thing and is not — a
        PermissionError (AV lock, a GPO-tightened ProgramData leaf) or an
        IsADirectoryError (`wsl --import` created the name as a directory, the
        way Docker auto-creates a missing mount target) both escape it. A
        directory at the marker path is the cheapest way to produce exactly
        that: IsADirectoryError on POSIX, PermissionError on Windows, and
        neither is a FileNotFoundError."""
        method, owner, calls = self._make(
            elevate=(0, False, None), distro_registered=False,
            marker_as_dir=True)
        with _env(USERNAME="student"):
            self._run(method, owner)
        # Unreadable is not "somebody else" — it must fall through to the
        # generic message, exactly like an absent marker.
        self.assertTrue(self._said_generic(calls), calls["log"])
        self.assertFalse(self._said_wrong_account(calls), calls["log"])

    def test_garbage_marker_keeps_the_old_message_without_raising(self):
        for junk in ("", "\x00\x01\x02", "FAILED 2026-08-02\nirgendwas\n",
                     "user=", "started pid=1 user=   "):
            with self.subTest(junk=junk):
                calls = self._run_exit0_invisible(marker=junk,
                                                  username="student")
                self.assertTrue(self._said_generic(calls), calls["log"])
                self.assertFalse(self._said_wrong_account(calls), calls["log"])

    def test_an_unreadable_username_env_keeps_the_old_message(self):
        # No %USERNAME% (non-Windows, or a stripped environment) means there is
        # nothing to compare against — never accuse the student's account then.
        calls = self._run_exit0_invisible(
            marker=self._SUCCESS_MARKER.format("schuladmin"), username=None)
        self.assertTrue(self._said_generic(calls), calls["log"])
        self.assertFalse(self._said_wrong_account(calls), calls["log"])

    def test_the_per_account_branch_never_fires_on_the_success_path(self):
        # exit 0 AND the distro visible is a plain success. A differing marker
        # user there is ordinary (the admin ran finalize once, correctly), so it
        # must not produce a warning.
        method, owner, calls = self._make(
            elevate=(0, False, None), distro_registered=True,
            marker=self._SUCCESS_MARKER.format("schuladmin"))
        with _env(USERNAME="student"):
            self._run(method, owner)
        self.assertEqual(calls["prereq"], 1)
        self.assertFalse(self._said_wrong_account(calls), calls["log"])

    def test_finalize_stamps_the_success_marker_it_is_parsed_from(self):
        """The GUI half above is useless if the .ps1 stops writing the field.

        finalize_install.ps1 used to leave the marker reading "started …" on the
        success path, which is exactly why this branch could not exist before."""
        src = _read(_FINALIZE_PS1, encoding="utf-8-sig")
        self.assertIn("SUCCESS {0} user={1} distro={2}", src,
                      "the success path no longer stamps the marker — the GUI's "
                      "per-account diagnosis has nothing to read")
        # It must be the LAST write before the successful exit, not somewhere a
        # later failure path could overwrite with a stale verdict.
        self.assertLess(src.index("SUCCESS {0} user={1} distro={2}"),
                        src.rindex("exit $EXIT_DONE"))

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


class PrerequisiteLifecycleTeardownTest(unittest.TestCase):
    """ensure_environment_stopped() is the SOLE lifecycle-enforcement point, so
    it must run before every early return that can follow a distro boot.

    THE bug this pins: the prerequisite scan boots the distro, then the rootfs
    gate `return`s ahead of the teardown. The precondition is positively
    correlated, not hypothetical — a rootfs mismatch MEANS the distro is old and
    was never re-imported, i.e. exactly the population whose persisted container
    configs still carry `restart: unless-stopped`, which dockerd honours the
    instant the distro boots. The follower then torques up and runs its boot
    quintic to HOME, holding the Dynamixel bus at 100 Hz, while the GUI shows a
    destructive-rebuild consent dialog over a live arm — and a consent runs
    `wsl --unregister` against a live VM."""

    def _make(self, *, rootfs_mismatch, docker_running=True):
        events = []

        def _teardown(log=None):
            events.append("teardown")
            return False

        def _rootfs_gate():
            events.append("rootfs_gate")
            return rootfs_mismatch

        fake_dm = types.SimpleNamespace(
            is_distro_registered=lambda: True,
            start_keepalive=lambda: True,
            is_docker_running=lambda: docker_running,
            start_edubotics_distro=lambda: events.append("distro_boot"),
            wait_for_docker=lambda callback=None: True,
            ensure_environment_stopped=_teardown,
            images_exist=lambda: {"img": True},
            pull_images=lambda **kw: True,
            check_for_updates=lambda log=None: False,
            get_last_pull_status=lambda: {"age_days": 0, "digests": {}},
            has_gpu=lambda: False,
        )
        fake_devm = types.SimpleNamespace(
            usbipd_reachable=lambda: True,
            usbipd_path=lambda: r"C:\usbipd.exe",
        )
        ns = {
            "os": os,
            "sys": types.SimpleNamespace(frozen=False),
            "device_manager": fake_devm,
            "docker_manager": fake_dm,
            "IMAGE_TAG": "2.13.0",
            "__package__": "gui.app",
        }
        method = _load_method("_run_prerequisite_checks_body", ns)
        owner = types.SimpleNamespace(
            _log=lambda _m: None,
            _set_status=lambda _m: None,
            _reboot_required_pending=lambda: False,
            _finalize_completed=False,
            _prompt_finalize_install=lambda reason=None: events.append(
                f"finalize({reason})"),
            _rootfs_rebuild_required=_rootfs_gate,
            _prerequisites_done=False,
            _update_start_button=lambda: None,
            _try_rehydrate_arms=lambda: None,
            progress=types.SimpleNamespace(start=lambda *_a: None,
                                           stop=lambda *_a: None),
            root=types.SimpleNamespace(
                after=lambda _ms, fn=None: fn() if fn is not None else None),
        )
        return method, owner, events

    def test_teardown_precedes_the_rootfs_gate(self):
        method, owner, events = self._make(rootfs_mismatch=True)
        method(owner)
        self.assertIn("teardown", events,
                      "the rootfs gate returned without ever tearing the stack "
                      "down — a resurrected follower is left driving")
        self.assertLess(events.index("teardown"), events.index("rootfs_gate"))
        self.assertEqual(events[-1], "finalize(rootfs_mismatch)")

    def test_teardown_still_runs_on_the_happy_path(self):
        method, owner, events = self._make(rootfs_mismatch=False)
        method(owner)
        self.assertEqual(events[0], "teardown")
        self.assertTrue(owner._prerequisites_done)

    def test_teardown_is_issued_exactly_once(self):
        # Hoisting it must MOVE the call, not duplicate it: two `compose down`s
        # would double the startup wait on every launch.
        for mismatch in (False, True):
            with self.subTest(rootfs_mismatch=mismatch):
                method, owner, events = self._make(rootfs_mismatch=mismatch)
                method(owner)
                self.assertEqual(events.count("teardown"), 1, events)

    def test_teardown_follows_the_docker_ready_gate(self):
        # It has to run at the EARLIEST point dockerd is known reachable —
        # earlier and `docker ps` fails, so the teardown is a silent no-op.
        method, owner, events = self._make(rootfs_mismatch=True,
                                           docker_running=False)
        method(owner)
        self.assertLess(events.index("distro_boot"), events.index("teardown"))


class PrerequisiteReentrancyTest(unittest.TestCase):
    """Two concurrent prerequisite scans must be impossible.

    Four call sites re-run the scan on a worker thread; the two post-elevation
    ones were moved onto workers to fix a UI freeze, which made a SECOND scan
    reachable while the first is still inside pull_images (press "Einrichtung
    abschliessen (Administrator)" again once _finalize_in_progress clears).
    That is two 15-30 min image pulls plus two prune_superseded_tags racing on
    the same tags."""

    def _make(self, body):
        calls = {"log": []}
        method = _load_method("_run_prerequisite_checks", {"__package__": "gui.app"})
        owner = types.SimpleNamespace(_log=calls["log"].append,
                                      _run_prerequisite_checks_body=body)
        return method, owner, calls

    def test_a_reentrant_call_is_refused(self):
        seen = []

        def _body():
            seen.append("body")
            method(owner)  # a second worker arrives mid-pull
        method, owner, calls = self._make(lambda: _body())
        method(owner)
        self.assertEqual(len(seen), 1, "a second concurrent scan ran")
        self.assertTrue(any("läuft bereits" in m for m in calls["log"]),
                        calls["log"])

    def test_flag_is_cleared_after_a_normal_run(self):
        method, owner, _calls = self._make(lambda: None)
        method(owner)
        self.assertFalse(owner._prereq_in_progress)
        method(owner)  # a later, sequential re-check must still be allowed
        self.assertFalse(owner._prereq_in_progress)

    def test_flag_is_cleared_when_the_body_raises(self):
        def _boom():
            raise RuntimeError("docker exploded")
        method, owner, _calls = self._make(_boom)
        with self.assertRaises(RuntimeError):
            method(owner)
        self.assertFalse(
            owner._prereq_in_progress,
            "a stranded flag permanently disables re-checks until a GUI restart "
            "— the guard needs the same try/finally shape _finalize_in_progress "
            "has")


class DiagnosticsSinkTest(unittest.TestCase):
    """ONE diagnostics directory, shared by the GUI and device_manager.

    Before this, the sink split three ways: device_manager appended to
    %ProgramData%\\EduBotics while the GUI wrote its elevated-repair transcripts
    (finalize / repair_usbipd / bind_devices + the finalize marker) to
    %LOCALAPPDATA%\\EduBotics — so support asked for install_diagnostics.log, got
    it, and it was missing exactly the failed-setup evidence they needed.

    The nastier half is the fallback rule. The installer applies the
    Users:Modify ACL inside a bare `catch { }`; if Set-Acl is refused (GPO-locked
    ProgramData, AV lock, or `wsl --import` having created the directory first)
    the leaf stays admin-only. Falling back only on an UNSET %ProgramData% —
    which never happens on Windows — meant a standard-user student's diagnostics
    then wrote NOWHERE, silently (the appender swallows OSError), while the GUI
    still printed a path to a file that does not exist."""

    def test_prefers_the_programdata_logs_leaf(self):
        with tempfile.TemporaryDirectory() as d, _env(PROGRAMDATA=d):
            resolved = constants.diagnostics_dir()
            self.assertEqual(resolved, os.path.join(d, "EduBotics", "logs"))
            self.assertTrue(os.path.isdir(resolved), "the sink must be created")

    def test_leaf_shape_matches_the_installer_acl_target(self):
        # The six installer .ps1 grant Users:Modify on this exact LEAF. A grant
        # on the parent would inherit onto %ProgramData%\EduBotics\wsl\ext4.vhdx.
        with tempfile.TemporaryDirectory() as d, _env(PROGRAMDATA=d):
            self.assertTrue(constants.diagnostics_dir().endswith(
                os.path.join("EduBotics", "logs")))

    def test_falls_back_when_the_leaf_is_unwritable(self):
        with tempfile.TemporaryDirectory() as pd, \
                tempfile.TemporaryDirectory() as la, \
                _env(PROGRAMDATA=pd, LOCALAPPDATA=la):
            deny = os.path.join(pd, "EduBotics", "logs")
            with patch.object(constants, "_dir_is_writable",
                              side_effect=lambda p: p != deny):
                resolved = constants.diagnostics_dir()
            self.assertEqual(resolved, os.path.join(la, "EduBotics"),
                             "a refused ACL must drop to a directory the student "
                             "always owns, not write nowhere")

    def test_returns_a_path_even_when_nothing_is_writable(self):
        with tempfile.TemporaryDirectory() as pd, _env(PROGRAMDATA=pd):
            with patch.object(constants, "_dir_is_writable", return_value=False):
                resolved = constants.diagnostics_dir()
        # The GUI PRINTS this path to the student, so it must never be empty.
        self.assertTrue(resolved)
        self.assertEqual(resolved, constants._diagnostics_dir_candidates()[-1])

    def test_unset_programdata_uses_localappdata(self):
        with tempfile.TemporaryDirectory() as la, \
                _env(PROGRAMDATA=None, LOCALAPPDATA=la):
            self.assertEqual(constants.diagnostics_dir(),
                             os.path.join(la, "EduBotics"))

    def test_write_probe_detects_a_real_refusal(self):
        # os.access(W_OK) reports the read-only ATTRIBUTE on Windows, not the
        # DACL, so only a real create proves the sink works.
        self.assertFalse(constants._dir_is_writable(
            os.path.join(tempfile.gettempdir(), "edubotics_no_such_dir_xyz", "a")))

    def test_write_probe_leaves_nothing_behind(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(constants._dir_is_writable(d))
            self.assertEqual(os.listdir(d), [], "the probe file was not removed")

    def test_device_manager_resolves_the_shared_sink(self):
        with tempfile.TemporaryDirectory() as d, _env(PROGRAMDATA=d):
            self.assertEqual(device_manager._diagnostics_log_path(),
                             constants.diagnostics_log_path())
            # And the public accessor the GUI prints to the student is the SAME
            # resolver — never a literal that can disagree with it.
            self.assertEqual(device_manager.get_diagnostics_log_path(),
                             os.path.join(d, "EduBotics", "logs",
                                          "install_diagnostics.log"))

    def test_gui_diag_dir_delegates_instead_of_re_deriving_a_base(self):
        # Behavioural coverage is impossible here (importing gui_app needs
        # tkinter), and re-deriving a base is exactly how the split happened.
        src = _module_fn_src("_edubotics_diag_dir")
        self.assertIn("return diagnostics_dir()", src)
        self.assertNotIn("LOCALAPPDATA", src,
                         "_edubotics_diag_dir must not resolve a base of its "
                         "own — that is what split the sink from "
                         "device_manager._diagnostics_log_path")

    def test_elevated_transcripts_land_in_the_shared_sink(self):
        src = _read(_GUI_SRC)
        for artifact in ("edubotics_finalize.log", "edubotics_finalize.marker",
                         "edubotics_repair_usbipd.log",
                         "edubotics_bind_devices.log"):
            self.assertIn(artifact, src)
        # Each is joined onto _edubotics_diag_dir(), never %TEMP% (the elevated
        # child runs as ADMIN, whose %TEMP% is not the student's) or a literal.
        for marker in ('os.path.join(_edubotics_diag_dir(), "edubotics_repair_usbipd.log")',
                       'os.path.join(_edubotics_diag_dir(), "edubotics_bind_devices.log")',
                       "diag = _edubotics_diag_dir()"):
            self.assertIn(marker, src)


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
                            ("CONSENT", "FINALIZE_EXIT_CONSENT"),
                            ("VIRT", "FINALIZE_EXIT_VIRT")):
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
            # 11 = no usable hypervisor. Its own code because "reboot" and
            # "enable virtualization in the BIOS/UEFI" are different remedies
            # and a student cannot reboot their way out of the second.
            "FINALIZE_EXIT_VIRT": 11,
        })

    def test_failed_code_is_distinct_from_the_routed_ones(self):
        ps1 = _ps1_exit_codes(_FINALIZE_PS1)
        self.assertIn("FAILED", ps1)
        # EXIT_FAILED has no GUI mirror on purpose — it is the else branch. It
        # must not collide with a code that routes somewhere specific.
        self.assertNotIn(ps1["FAILED"],
                         [ps1["DONE"], ps1["REBOOT"], ps1["CONSENT"],
                          ps1["VIRT"]])

    def test_import_consent_refusal_code_matches_finalize(self):
        # import_edubotics_wsl.ps1 emits the bare literal `exit 12`; finalize
        # keys its $EXIT_CONSENT pass-through off that exact number.
        import_src = _read(_IMPORT_PS1, encoding="utf-8-sig")
        consent = _ps1_exit_codes(_FINALIZE_PS1)["CONSENT"]
        self.assertRegex(
            import_src, rf"(?m)^\s*exit {consent}\s*$",
            f"import_edubotics_wsl.ps1 no longer exits {consent} on a refused "
            f"destructive re-import — finalize's $EXIT_CONSENT mapping is dead")

    def test_import_hypervisor_refusal_code_matches_finalize(self):
        """import classifies the dead-hypervisor failure itself and exits 11.

        Mirrors test_import_consent_refusal_code_matches_finalize: import emits a
        bare literal, finalize keys $EXIT_VIRT off that exact number. This is the
        BACKSTOP for a verdict of Unknown — the pre-import gate proceeds without
        proof by design, so if nothing downstream classified the real failure the
        2026-09-07 outcome (a hypervisor fault reported as a disk/AV problem)
        comes straight back."""
        import_src = _read(_IMPORT_PS1, encoding="utf-8-sig")
        virt = _ps1_exit_codes(_FINALIZE_PS1)["VIRT"]
        self.assertRegex(
            import_src, rf'if \(\$failClass -eq "hypervisor"\) \{{ exit {virt} \}}',
            "import must exit the same code finalize propagates as $EXIT_VIRT")
        fin = _read(_FINALIZE_PS1, encoding="utf-8-sig")
        self.assertIn(
            "if ($importRc -eq $EXIT_VIRT) {", fin,
            "finalize must PROPAGATE import's hypervisor refusal, not flatten it "
            "into $EXIT_FAILED — the GUI shows the remedy as its own message")

    def test_the_consent_branch_is_matched_before_the_virt_branch(self):
        """Both branches read $importRc; the CONSENT one must come first.

        test_finalize_consent_branch_actually_exits_the_consent_code regexes the
        FIRST `if ($importRc -eq N)` block in the file, so ordering them the other
        way round would make that guard assert against the wrong branch and pass
        vacuously. Pin the order rather than leaving it to luck."""
        src = _read(_FINALIZE_PS1, encoding="utf-8-sig")
        self.assertLess(src.index("if ($importRc -eq 12) {"),
                        src.index("if ($importRc -eq $EXIT_VIRT) {"))

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
                     "pull_images.ps1", "migrate_from_docker_desktop.ps1"):
            with self.subTest(script=name):
                src = _read(os.path.join(_SCRIPTS, name), encoding="utf-8-sig")
                lines = [ln.strip() for ln in src.splitlines() if ln.strip()]
                self.assertTrue(
                    lines and lines[-1] == "exit 0",
                    f"{name} must end with an explicit `exit 0`; last statement "
                    f"is {lines[-1]!r}")


class PreflightAccountScopeTest(unittest.TestCase):
    """preflight_system.ps1 check 5: the distro IMAGE is on disk but the distro
    is not registered for THIS Windows account.

    WSL2 registers distros per Windows user (HKCU\\...\\Lxss) while the installer
    is PrivilegesRequired=admin, so on a managed school PC a different admin's
    `wsl --import` is invisible to the student. Disk-yes / list-no is the
    SYMPTOM of that split — but not proof of it: the importer's post-failure
    `Remove-Item ext4.vhdx` runs -ErrorAction SilentlyContinue and commonly
    fails while the WSL service or an AV scanner holds the handle, leaving the
    identical signature with no account problem at all. So the check must
    DISCRIMINATE before it accuses, and these pin that it still does.

    Source-level checks because the file is PowerShell: the tests that can RUN
    it live on a Windows rig, and what rots silently is the cross-file wiring."""

    _PREFLIGHT = os.path.join(_SCRIPTS, "preflight_system.ps1")

    def _src(self):
        return _read(self._PREFLIGHT, encoding="utf-8-sig")

    def test_the_check_exists_and_tests_both_halves_of_the_split(self):
        src = self._src()
        self.assertIn("$vhdxPresent -and (-not $distroPresent)", src,
                      "check 5 must fire on disk-YES + registered-NO; either "
                      "half alone is a normal state (a fresh PC, or a healthy "
                      "install) and would false-positive on every rig")

    def test_it_reuses_the_check4_enumeration_instead_of_re_running_wsl(self):
        src = self._src()
        # Real INVOCATIONS only — `wsl --list` also appears in comments, and
        # counting those would make this test unable to fail for the reason it
        # exists.
        invocations = re.findall(r"^\s*(?:\$\w+\s*=\s*)?wsl --list", src, re.M)
        self.assertEqual(
            len(invocations), 1,
            "check 5 must reuse $distroPresent from check 4 — a second "
            "enumeration can disagree with the first and costs a wsl.exe spawn "
            f"on the GUI's startup path (found {invocations})")

    def test_neither_probe_can_abort_the_diagnostic(self):
        src = self._src()
        for anchor, label in (
            (r"\$vhdxPresent = \$false\n(.*?)\n\n", "the Test-Path probe"),
            (r"\$markerUser = \"\"\n(.*?)\nif \(\$markerUser\.IndexOf",
             "the marker read"),
        ):
            m = re.search(anchor, src, re.S)
            self.assertIsNotNone(m, f"{label} block moved or vanished")
            self.assertIn("try {", m.group(1), label)
            self.assertIn("} catch {", m.group(1), label)
        # This script is a DIAGNOSTIC. It runs -Quiet from the .iss [Run] and
        # from the GUI, and always exits 0.
        lines = [ln.strip() for ln in src.splitlines() if ln.strip()]
        self.assertEqual(lines[-1], "exit 0",
                         "preflight must still be non-fatal")
        self.assertIn('$ErrorActionPreference = "Continue"', src)

    def test_the_vhdx_path_matches_the_importers_default_install_root(self):
        """THE cross-file contract, and the one that can rot silently.

        The check's signal is only a signal while it looks at the file
        `import_edubotics_wsl.ps1` actually writes. Change -InstallRoot's default
        and this probe starts answering "no image on disk" on every rig — the
        check would go quiet rather than wrong, which is worse."""
        importer = _read(_IMPORT_PS1, encoding="utf-8-sig")
        m = re.search(r'\$InstallRoot\s*=\s*"([^"]+)"', importer)
        self.assertIsNotNone(m, "import_edubotics_wsl.ps1 has no -InstallRoot default")
        # PowerShell path, e.g. "$env:ProgramData\EduBotics\wsl" -> the leaf the
        # preflight Join-Path reproduces.
        leaf = m.group(1).split("ProgramData", 1)[-1].lstrip("\\")
        self.assertTrue(leaf, f"unexpected -InstallRoot shape: {m.group(1)!r}")
        src = self._src()
        self.assertIn(f'Join-Path $env:ProgramData "{leaf}\\ext4.vhdx"', src,
                      f"check 5 probes a different path than the importer "
                      f"writes ({m.group(1)}\\ext4.vhdx)")

    # ── the discriminator ────────────────────────────────────────────────
    def test_the_per_account_verdict_requires_a_differing_marker_user(self):
        """Only a marker `user=` that DIFFERS from %USERNAME% may produce the
        FEHLER. Without that gate the leftover-VHDX case (a failed import whose
        cleanup could not delete the file) tells a student on a managed PC to
        log in as somebody else — advice they cannot act on and that would not
        help if they could."""
        src = self._src()
        self.assertIn('$accountSplit = ($markerUser -ne "") -and '
                      '($env:USERNAME) -and ($markerUser -ine $env:USERNAME)',
                      src,
                      "the split predicate must require a marker user, a "
                      "current user, and that the two DIFFER (case-insensitively "
                      "— Windows account names are)")
        # The FEHLER is inside the $accountSplit branch; the ambiguous case gets
        # a WARNUNG naming both causes.
        m = re.search(r"if \(\$accountSplit\) \{\n(.*?)\n    \} else \{\n(.*?)\n    \}",
                      src, re.S)
        self.assertIsNotNone(m, "the accountSplit if/else moved or vanished")
        self.assertIn("Emit FEHLER", m.group(1))
        self.assertIn("Emit WARNUNG", m.group(2),
                      "the ambiguous case must WARN, not accuse")
        for cause in ("anderen Windows-Konto", "abgebrochen"):
            self.assertIn(cause, m.group(2),
                          "the WARNUNG must name BOTH possible causes")

    # ── the two properties of the READ itself ────────────────────────────
    # Both are asserted in this script's own comments and in CLAUDE.md, and
    # until now BOTH were fenced by nothing: a mutation violating either left
    # the whole deps-free suite green while producing the exact bug check 5
    # exists to eliminate — a wrong-account accusation on a healthy machine.
    # Simulated against the real bytes finalize_install.ps1 writes (UTF-8 with
    # BOM, `user=Müller distro=EduBotics`):
    #     shipped                  -> 'Müller'                  fail-safe
    #     no -Encoding UTF8        -> 'MÃ¼ller'                 FEHLER, wrong
    #     no " distro=" cut        -> 'Müller distro=EduBotics'  FEHLER, wrong
    # The U+FFFD refusal cannot catch either: a cp1252 decode of UTF-8 yields
    # VALID characters, not replacement characters, so the guard that protects
    # the Python side is structurally blind to the PowerShell side's failure
    # mode. Same class as the KEY_WOW64_64KEY hole fenced in
    # tests/test_config_generator.py.
    #
    # Source-level, because there is no PowerShell on the CI runner. Both are
    # phrased as a property of the CALL rather than as an exact substring, so a
    # differently-spelled violation (`-Encoding Default`, `.IndexOf(" ")`) fails
    # too — pinning the literal text would only catch deletion.
    _MARKER_CUT_DELIMITER = " distro="

    def _marker_read_block(self):
        """Check 5's marker read, COMMENT LINES STRIPPED.

        Stripping is load-bearing for the same reason RootCauseGuardTest._code
        strips: the block's own rationale necessarily names both
        ``-Encoding UTF8`` and ``" distro="`` to explain why they are there, so
        an un-stripped guard would pin the DOCUMENTATION and pass over a read
        that no longer does either."""
        src = self._src()
        m = re.search(
            r'^\$MarkerPath\s*=\s*Join-Path \$DiagDir '
            r'"edubotics_finalize\.marker"(.*?)^\$accountSplit',
            src, re.S | re.M)
        self.assertIsNotNone(
            m, "check 5's marker-read block moved or vanished — every "
               "assertion below would pass vacuously")
        block = "\n".join(ln for ln in m.group(1).splitlines()
                          if not ln.lstrip().startswith("#"))
        self.assertIn("$markerUser", block,
                      "the stripped block contains no marker parse at all")
        return block

    def test_the_marker_read_names_a_utf8_encoding(self):
        """finalize writes the marker ``-Encoding UTF8``; this side must decode
        it as UTF-8 or a German name comes back mojibake.

        The failure is silent AND wrong-way-round: „Müller" read as cp1252 is
        „MÃ¼ller", which contains no U+FFFD (so the unparseable-marker refusal
        never fires) and can never ``-ine``-match %USERNAME% (so the per-account
        FEHLER fires on the SAME account, replacing correct advice with advice
        that cannot help). Asserted on the encoding ARGUMENT, so dropping the
        parameter and naming a non-UTF-8 codepage both fail."""
        block = self._marker_read_block()
        reads = [ln.strip() for ln in block.splitlines()
                 if re.search(r"Get-Content|StreamReader|ReadAll(?:Text|Lines|Bytes)",
                              ln)]
        self.assertEqual(
            len(reads), 1,
            f"expected exactly ONE file read in the marker block, got {reads}")
        read = reads[0]
        self.assertIn(
            "Get-Content", read,
            f"the marker read switched API to {read!r} — this guard can only "
            f"reason about Get-Content's -Encoding contract")
        enc = re.search(r"-Encoding\s+([A-Za-z0-9]+)", read)
        self.assertIsNotNone(
            enc,
            f"the marker read declares no -Encoding: {read!r}. PS 5.1's "
            f"default is the system ANSI codepage, and finalize_install.ps1 "
            f"writes UTF-8")
        # Any UTF-8 spelling is accepted: the property is "decodes as UTF-8",
        # not a literal. PS 5.1 only has `UTF8`; the PS 7 aliases are listed so a
        # future toolchain move is not a false positive.
        self.assertIn(
            enc.group(1).lower(), {"utf8", "utf8bom", "utf8nobom"},
            f"the marker read decodes as {enc.group(1)!r}, not UTF-8 — "
            f"finalize_install.ps1 writes it -Encoding UTF8")

    def test_the_marker_user_value_is_bounded_by_the_distro_delimiter(self):
        """``user=`` is NOT last on the SUCCESS line, so the value has to be cut
        at ``" distro="``.

        Without the cut the parsed name is „Müller distro=EduBotics", which
        again carries no U+FFFD and again can never match %USERNAME% — the same
        wrong accusation by a different route. %USERNAME% may contain spaces, so
        cutting at whitespace is not an alternative; the delimiter is the only
        bound, and the Python reader has to use the SAME one."""
        block = self._marker_read_block()
        searched = re.findall(
            r'(?:IndexOf|Split|-split)\s*\(?\s*"([^"]*)"', block)
        self.assertIn(
            self._MARKER_CUT_DELIMITER, searched,
            f"nothing in the marker read searches for "
            f"{self._MARKER_CUT_DELIMITER!r} (found {searched}) — the extracted "
            f"name would carry the distro suffix")
        self.assertRegex(
            block, r"Substring\(\s*0\s*,|-split|\.Split\(",
            "the delimiter's index must reach a TRUNCATION — merely locating "
            "it leaves the whole rest of the line in $markerUser")
        # Cross-language: the Python reader cuts on the same literal. The two
        # parses are independent implementations (recorded in
        # docs/KNOWN-ISSUES.md), so the delimiter is the one piece of their
        # semantics cheap enough to compare directly.
        self.assertIn(
            f'find("{self._MARKER_CUT_DELIMITER}")', _read(_GUI_SRC),
            "gui_app.py::_marker_user cuts on a different delimiter than the "
            "preflight does — one reader would name an account the other does "
            "not")

    def test_the_marker_path_is_the_one_the_gui_writes_and_parses(self):
        """Cross-file: the preflight and gui_app must read the SAME marker.

        Both resolve it inside the machine-wide diagnostics leaf — the .ps1 as
        `$DiagDir`, the GUI as `_edubotics_diag_dir()`. A rename on either side
        makes the preflight silently stop discriminating (every case becomes the
        ambiguous WARNUNG) rather than fail."""
        src = self._src()
        self.assertIn('$MarkerPath = Join-Path $DiagDir "edubotics_finalize.marker"',
                      src)
        gui = _read(_GUI_SRC)
        self.assertIn('os.path.join(diag, "edubotics_finalize.marker")', gui)
        # ... and the same U+FFFD refusal, for the same reason.
        self.assertIn("[char]0xFFFD", src,
                      "an ANSI-written (mojibake) marker must be treated as "
                      "unparseable, never as a different account")

    def test_the_two_readers_agree_on_the_primary_dir_and_diverge_below_it(self):
        """The DIRECTORY resolution is the twin half this file can still reach.

        Both sides name the same PRIMARY leaf. Below it they diverge on purpose
        and that divergence is asserted positively, the way the other twin
        lockstep tests in this repo assert theirs: ``constants.diagnostics_dir``
        falls back (%LOCALAPPDATA% then ~) on an unwritable ProgramData, while
        the .ps1 hard-codes ProgramData with none. Consequence, recorded in
        docs/KNOWN-ISSUES.md: on an unwritable leaf the GUI writes the marker
        where the preflight never looks and check 5 degrades to its ambiguous —
        and therefore fail-safe — WARNUNG. Giving the PS side the same probe
        would duplicate a resolver in a second language, which is the drift class
        the marker already suffers from.
        """
        src = self._src()
        self.assertIn('$DiagDir = Join-Path $env:ProgramData "EduBotics\\logs"', src)
        # No fallback on the PS side, and the shape of that assertion matters.
        # The previous form was `^\$DiagDir\s*=` (anchored at column 0) plus a
        # `$env:LOCALAPPDATA` name check — and the natural PowerShell fallback is
        # an INDENTED reassignment inside `if (-not (Test-Path …)) { … }` under
        # any env var, which passed both. Mutation-proven: an indented
        # `$DiagDir = Join-Path $env:TEMP "EduBotics"` survived. So: count
        # assignments at ANY indentation, and allowlist the env vars the script
        # may read at all. Comments stripped first — the sink block names
        # %LOCALAPPDATA% precisely to say the sink is NOT there.
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.lstrip().startswith("#"))
        assignments = re.findall(r"^\s*\$DiagDir\s*=", code, re.M)
        self.assertEqual(
            1, len(assignments),
            f"found {len(assignments)} assignments to $DiagDir — one assignment "
            f"means one resolution, i.e. no fallback, at ANY indentation")
        self.assertEqual(
            sorted({m for m in re.findall(r"\$env:(\w+)", code)}),
            ["ProgramData", "USERNAME"],
            "the preflight may read only %ProgramData% (the machine-wide sink "
            "and the VHDX probe) and %USERNAME% (the account discriminator). A "
            "third environment variable is a fallback base by any other name")
        consts = _read(os.path.join(
            os.path.dirname(__file__), "..", "gui", "app", "constants.py"))
        self.assertIn('_DIAGNOSTICS_SUBDIR = ("EduBotics", "logs")', consts)
        # ... and the GUI's fallback chain is real, not aspirational.
        self.assertIn("LOCALAPPDATA", consts)
        self.assertIn("_dir_is_writable", consts)

    def test_the_german_messages_use_literal_umlauts(self):
        """The unittest is the ONLY guard on this. It does NOT mirror
        ci.yml::german-strings-lint: that grep step needs a LITERAL
        [FEHLER]/[WARNUNG]/[STOPP] token in the source line, and `Emit` builds
        the tag at runtime from its -Level parameter, so not one line in this
        file is in the grep's scope (measured: the whole installer tree has a
        single file with a literal tag, and it is a comment)."""
        src = self._src()
        emits = [ln for ln in src.splitlines()
                 if re.search(r"Emit (FEHLER|WARNUNG)\b", ln)
                 and "Windows-Konto" in ln]
        self.assertEqual(len(emits), 2,
                         "expected check 5's FEHLER + WARNUNG lines")
        for line in emits:
            for bad in ("fuer", "gehoert", "waehrend", "ausfuehren",
                        "durchgefuehrt", "pruefen", "zurueck", "Faelle",
                        "Datentraeger"):
                self.assertNotIn(bad, line,
                                 f"use literal ä/ö/ü/ß, not {bad!r}")
            self.assertTrue(any(ch in line for ch in "äöüß"),
                            "a German student-facing line with no umlaut at "
                            "all is almost certainly transliterated")


class HfTokenBindingWiringTest(unittest.TestCase):
    """gui_app's half of the machine-bound HuggingFace token.

    The DECISION and the deletion are ``config_generator``'s and are covered by
    ``tests/test_config_generator.py::TestHfTokenMachineBinding``. What can only
    be pinned here is the WIRING, and all three parts of it rot silently:

      * ``_bind_hf_token`` runs BEFORE ``_build_ui``, or Schritt D's status label
        reads a .env the purge has not touched yet and reports a deleted token as
        saved — i.e. no re-prompt;
      * removing a credential the student entered is REPORTED in German, because
        a silent deletion is indistinguishable from a bug;
      * the two GUI writers go through ``write_hf_token``, so a stored token can
        never exist without its HF_TOKEN_MACHINE stamp.
    """

    _NS_KEYS = ("config_generator", "ENV_FILE")

    def _make(self, verdict=None, raises=None, env_file="/tmp/x/.env"):
        """`_bind_hf_token` bound to a stub owner, with a stubbed generator."""
        calls = {"log": [], "paths": []}

        def _bind(path):
            calls["paths"].append(path)
            if raises is not None:
                raise raises
            return verdict

        cg = types.SimpleNamespace(
            bind_hf_token_to_this_machine=_bind,
            HF_TOKEN_OK="ok", HF_TOKEN_ADOPTED="adopted",
            HF_TOKEN_FOREIGN="foreign",
        )
        ns = {"config_generator": cg, "ENV_FILE": env_file,
              "__package__": "gui.app"}
        method = _load_method("_bind_hf_token", ns)
        owner = types.SimpleNamespace(_log=calls["log"].append)
        return method, owner, calls

    def test_a_foreign_token_deletion_is_reported_in_german(self):
        method, owner, calls = self._make(verdict="foreign")
        method(owner)
        self.assertEqual(len(calls["log"]), 1, calls["log"])
        line = calls["log"][0]
        self.assertIn("[WARNUNG]", line)
        # It must say what happened AND what to do — a student who is not told to
        # re-enter the token just sees recordings stop uploading.
        for phrase in ("anderen PC", "gelöscht", "Schritt D"):
            self.assertIn(phrase, line)
        # ...and it must not be transliterated (CLAUDE.md §1). This is also the
        # only guard: ci.yml::german-strings-lint's grep sees this line (it
        # carries a literal [WARNUNG]) but only for its own word denylist.
        for bad in ("geloescht", "fuer", "gehoert", "pruefen", "ueber "):
            self.assertNotIn(bad, line)
        self.assertTrue(any(ch in line for ch in "äöüß"))

    def test_a_legacy_adoption_is_reported_without_alarming_anyone(self):
        """Once per install, on the first launch after the upgrade. It explains a
        new .env key — but nothing was lost, so it must not be a warning."""
        method, owner, calls = self._make(verdict="adopted")
        method(owner)
        self.assertEqual(len(calls["log"]), 1, calls["log"])
        line = calls["log"][0]
        self.assertNotIn("[WARNUNG]", line)
        self.assertIn("HuggingFace-Token", line)
        self.assertTrue(any(ch in line for ch in "äöüß"))

    def test_the_ordinary_case_says_nothing(self):
        """Every launch on the student's own PC takes this path. A line here
        would be noise on 100 % of starts."""
        method, owner, calls = self._make(verdict="ok")
        method(owner)
        self.assertEqual(calls["log"], [])
        self.assertEqual(calls["paths"], ["/tmp/x/.env"])

    def test_a_raising_check_cannot_block_the_launch(self):
        """It runs from ``__init__`` with no wrapper of its own: an escaping
        exception would take the whole window with it, so an unreadable or
        unwritable .env has to degrade to a German line."""
        method, owner, calls = self._make(raises=OSError("kein Zugriff"))
        method(owner)   # must not raise
        self.assertEqual(len(calls["log"]), 1, calls["log"])
        self.assertIn("[WARNUNG]", calls["log"][0])
        self.assertIn("kein Zugriff", calls["log"][0])

    def test_it_runs_before_the_ui_that_reads_the_token_status(self):
        """Statement ORDER inside ``__init__``, via AST — never a string index.
        ``ast`` drops comments, and the comment above the call names ``_build_ui``
        precisely to explain the ordering, so an ``index()`` comparison would be
        asserting something about the prose (the trap
        test_ros_domain_twin_lockstep documents)."""
        import ast
        tree = ast.parse(_read(_GUI_SRC))
        init = next(
            fn for cls in tree.body
            if isinstance(cls, ast.ClassDef) and cls.name == "EduBoticsApp"
            for fn in cls.body
            if isinstance(fn, ast.FunctionDef) and fn.name == "__init__")
        order = {}
        for i, stmt in enumerate(init.body):
            text = ast.unparse(stmt)
            for name in ("self._bind_hf_token()", "self._build_ui()"):
                if name in text:
                    order.setdefault(name, i)
        self.assertEqual(sorted(order), ["self._bind_hf_token()",
                                         "self._build_ui()"],
                         f"__init__ no longer calls both: {order}")
        self.assertLess(
            order["self._bind_hf_token()"], order["self._build_ui()"],
            "Schritt D's status label is built inside _build_ui and reads the "
            ".env — the token must already be judged, or a deleted token is "
            "still reported as saved and the student is never re-prompted")

    def test_no_gui_writer_stores_the_token_without_its_stamp(self):
        """``upsert_env_var("HF_TOKEN", …)`` is the underlying writer, but a GUI
        call site using it directly leaves an UNSTAMPED token — which reads as
        legacy on every machine that copies the profile, i.e. the whole binding
        silently off. AST over the whole file, so a comment mentioning either
        name (there is one) cannot satisfy or break it."""
        import ast
        tree = ast.parse(_read(_GUI_SRC))
        raw_writes, stamped_writes = [], []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (node.func.attr if isinstance(node.func, ast.Attribute)
                    else getattr(node.func, "id", None))
            if name == "write_hf_token":
                stamped_writes.append(node.lineno)
            elif name == "upsert_env_var" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and first.value == "HF_TOKEN":
                    raw_writes.append(node.lineno)
        self.assertEqual(raw_writes, [],
                         "gui_app writes HF_TOKEN through upsert_env_var — use "
                         "config_generator.write_hf_token, which stamps "
                         "HF_TOKEN_MACHINE in the same breath")
        self.assertGreaterEqual(
            len(stamped_writes), 2,
            'expected both GUI token writers (Schritt D\'s „Token speichern" '
            'and the „Umgebung starten" persist) to call write_hf_token; found '
            f"{stamped_writes}")


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
    # a lab PC could tamper with the distro image. All SIX scripts that write
    # the diagnostics log are listed: preflight_system.ps1 joined the ProgramData
    # sink last (it used to write into %LOCALAPPDATA%, splitting the artifact).
    _ACL_SCRIPTS = ("install_prerequisites.ps1", "migrate_from_docker_desktop.ps1",
                    "verify_system.ps1", "configure_usbipd.ps1", "bind_devices.ps1",
                    "preflight_system.ps1")

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

    def test_every_script_writes_the_same_leaf_the_gui_resolves(self):
        """The .ps1 half of the shared diagnostics-sink contract.

        The GUI (constants.diagnostics_dir) and these six scripts must name the
        SAME directory or the support artifact splits again — and the leaf
        matters twice over: the ACL grant above must land on
        %ProgramData%\\EduBotics\\logs, never on the parent, whose subtree
        contains wsl\\ext4.vhdx."""
        for name in self._ACL_SCRIPTS:
            with self.subTest(script=name):
                self.assertRegex(
                    self._code(name),
                    r'\$DiagDir\s*=\s*Join-Path \$env:ProgramData "EduBotics\\logs"',
                    "must resolve %ProgramData%\\EduBotics\\logs — the same leaf "
                    "constants.diagnostics_dir() resolves; anything else splits "
                    "the support artifact or ACLs the wrong directory")

    # ── 4. verify_system's reboot-pending branch ────────────────────────────
    # verify_system.ps1 has a branch that reports a benign "Neustart steht noch
    # aus" and exits 0. Routing it on the mere EXISTENCE of .reboot_required is
    # wrong, because finalize deliberately KEEPS the flag set on every
    # unfinished outcome — including a hard failure (Fail-WithNextAction /
    # $EXIT_FAILED). A 14 GB-disk install whose import fails therefore reported
    # "reboot pending" + exit 0 forever, and Inno's [Run] step read the verify
    # as successful: the Audit-H23 regression reintroduced one file over. The
    # flag must be discriminated against last-boot-time, and an unreadable clock
    # must fall toward FAILED rather than manufacture a benign result.
    def test_verify_does_not_route_on_the_bare_reboot_flag(self):
        code = self._code("verify_system.ps1")
        self.assertNotRegex(
            code, r"\$rebootPending\s*=\s*Test-Path",
            "the flag's mere existence must NOT mean 'a reboot is pending' — "
            "finalize keeps it set on every unfinished outcome, including a "
            "hard failure, so this branch would exit 0 over a broken install")
        # The TIME comparison itself moved into virtualization_ready.ps1 on
        # 2026-09-11, because finalize_install.ps1 had a DIFFERENT answer to the
        # same question and the weaker one guarded the import. What this file
        # must now prove is that it ASKS the shared predicate rather than
        # growing a third opinion.
        self.assertRegex(
            code, r"\$rebootPending\s*=\s*Test-RebootOutstanding\s+-State",
            "verify must consume the SHARED predicate, not re-derive it")
        self.assertIn("virtualization_ready.ps1", code,
                      "verify must dot-source the one predicate file")
        self.assertNotIn(
            "LastBootUpTime", code,
            "a SECOND copy of the boot-time comparison is the defect this "
            "change removed — ask Test-RebootOutstanding instead")

    def test_verify_unreadable_clock_reports_failed_not_pending(self):
        code = self._code("verify_system.ps1")
        init = re.search(r"\$rebootPending\s*=\s*\$false", code)
        self.assertIsNotNone(
            init, "$rebootPending must DEFAULT to $false so an unreadable clock "
                  "(or an absent helper) reports FAILED — the safe direction — "
                  "instead of a benign pending reboot")
        self.assertLess(
            init.start(), code.index("Test-RebootOutstanding"),
            "the $false default must precede the predicate it guards")

    def test_verify_dot_source_is_test_path_guarded(self):
        """An unguarded dot-source of a missing file THROWS.

        Controlled Folder Access can leave a partially-copied {app}\\scripts —
        a state this codebase already anticipates — and a throw here would turn
        "Installation prüfen" into a crash instead of a FAIL verdict."""
        code = self._code("verify_system.ps1")
        guard = code.index("if (Test-Path $rebootHelper)")
        self.assertLess(guard, code.index(". $rebootHelper"),
                        "Test-Path must precede the dot-source")

    # ── 5. finalize's custody of .reboot_required across the prereq child ────
    # finalize deliberately calls install_prerequisites.ps1 WITHOUT
    # -PreserveExistingRebootFlag (a preserved STALE flag re-arms the reboot
    # loop). But that child deletes the flag whenever IT concludes no reboot is
    # needed — including a "dd-uninstall" flag whose reboot has NOT happened,
    # written by migrate_from_docker_desktop.ps1, whose reason the feature-store
    # probe is blind to. Losing it makes finalize skip Test-RebootStillPending
    # and import the distro next to a half-removed Docker Desktop: precisely the
    # entanglement the flag exists to prevent. So finalize takes custody.
    def test_finalize_takes_custody_of_the_flag_across_the_prereq_child(self):
        code = self._code("finalize_install.ps1")
        call = re.search(
            r"&\s*\(Join-Path \$PSScriptRoot \"install_prerequisites\.ps1\"\)",
            code)
        self.assertIsNotNone(call, "prereq child invocation not found")
        snap = code.index("$flagSnapshot")
        self.assertLess(
            snap, call.start(),
            "the flag must be snapshotted BEFORE the child can delete it")
        after = code[call.end():]
        self.assertIn(
            "Set-Content", after,
            "a flag the child deleted but did not own must be RESTORED after "
            "the call, or a pending Docker-Desktop removal is erased into a "
            "distro import")
        self.assertRegex(
            after, r"-not \(Test-Path \$flagPath\)",
            "the restore must be conditional on the child having deleted it — "
            "an unconditional rewrite would re-arm the reboot loop")

    def test_finalize_does_not_preserve_a_stale_flag_instead(self):
        # The tempting one-line 'fix' (always pass -PreserveExistingRebootFlag)
        # is wrong in the other direction: a STALE flag then survives forever and
        # every launch dead-ends on "Neustart erforderlich". Custody, not
        # preservation, is the contract.
        code = self._code("finalize_install.ps1")
        call = re.search(
            r"&\s*\(Join-Path \$PSScriptRoot \"install_prerequisites\.ps1\"\)",
            code)
        window = code[max(0, call.start() - 1200):call.start()]
        self.assertNotIn(
            "PreserveExistingRebootFlag = $true", window,
            "finalize must NOT blanket-preserve the flag — a stale flag would "
            "re-arm the reboot loop it exists to close")


class DockerDesktopRebootReasonTest(unittest.TestCase):
    """The .reboot_required CONTENT contract between migrate and finalize.

    migrate_from_docker_desktop.ps1 writes the flag when Docker Desktop's
    uninstaller returns 3010 (removal completes on the next boot). finalize's
    Test-RebootStillPending can only interrogate the WSL/VMP feature store,
    which reads Enabled throughout a pending DD removal — so with a bare "1" it
    declared the reboot done and imported the distro next to a half-removed DD,
    the exact entanglement the flag exists to prevent. The fix threads a REASON
    through the flag content ("dd-uninstall") and discriminates on flag-mtime vs
    last-boot-time. Both halves live in different scripts with no shared symbol;
    these pin the seam. Comment lines are stripped first (the fixes' own
    rationale necessarily names "dd-uninstall"), reusing RootCauseGuardTest's
    helper.
    """

    _code = staticmethod(RootCauseGuardTest._code)

    def test_migrate_writes_the_dd_reason_not_a_bare_1(self):
        code = self._code("migrate_from_docker_desktop.ps1")
        self.assertRegex(
            code, r'Set-Content -Path \$RebootFlag -Value "dd-uninstall"',
            "migrate must write the dd-uninstall REASON into the flag — a bare "
            '"1" makes finalize blind to the pending Docker-Desktop removal '
            "(the WSL/VMP feature store reads Enabled throughout it)")

    def test_finalize_discriminates_the_dd_reason_by_boot_time(self):
        # The predicate moved to virtualization_ready.ps1 on 2026-09-11 (finalize
        # and verify_system each had their own, and they disagreed). The
        # INVARIANT is unchanged and is asserted against its new home; finalize
        # must no longer carry a private copy.
        code = RootCauseGuardTest._code("virtualization_ready.ps1")
        self.assertIn("dd-uninstall", code,
                      "the dd-uninstall reason is no longer read — the "
                      "feature-store probe alone cannot see a pending DD removal")
        self.assertIn("LastBootUpTime", code,
                      "the dd-uninstall reason must be settled by comparing the "
                      "flag's write time against the last boot time — any other "
                      "signal either loops the student (a lingering registry "
                      "entry) or trusts the honor system (the dialog)")
        # The dd discrimination must outrank the feature-store signal, so a
        # not-yet-rebooted DD removal defers even though the features read
        # Enabled. It used to be provable by FILE ORDER (the dd branch sat above
        # the Get-WindowsOptionalFeature loop in one function). The facts are now
        # gathered once and the PRIORITY is expressed as rung order inside the
        # two policy functions, so assert that directly — which is strictly
        # stronger than the old positional check. Since 2026-09-11 each rung is
        # a CALL to a named proof predicate (neither policy spells a proof
        # itself — see OneRebootPredicateTest), so the order is read off those
        # call sites.
        self.assertIn("dd-uninstall",
                      _ps1_function_body(code, "Test-DdUninstallOutstanding"),
                      "the dd-uninstall reason must be read by the proof "
                      "predicate both policies share")
        for fn in ("Test-RebootOutstanding", "Get-VirtualizationVerdict"):
            with self.subTest(policy=fn):
                body = _ps1_function_body(code, fn)
                self.assertLess(
                    body.index("Test-DdUninstallOutstanding"),
                    body.index("Test-FeatureEnablePending"),
                    f"{fn} must test the dd-uninstall reason BEFORE EnablePending "
                    f"— the feature store reads Enabled throughout a pending "
                    f"Docker-Desktop removal, so the cheaper signal would win and "
                    f"the import would run next to a half-removed Docker Desktop")
        verdict = _ps1_function_body(code, "Get-VirtualizationVerdict")
        self.assertLess(
            verdict.index("Test-DdUninstallOutstanding"),
            verdict.index("HypervisorPresent"),
            "a LIVE hypervisor must not short-circuit the half-removed "
            "Docker-Desktop guard — that is why rung 1 is rung 1")
        fin = RootCauseGuardTest._code("finalize_install.ps1")
        self.assertNotIn(
            "Get-WindowsOptionalFeature", fin,
            "finalize must not keep a private feature-store probe — that copy IS "
            "the 2026-09-07 defect")

    def test_prereqs_never_clobber_an_existing_flag_reason(self):
        # Under -PreserveExistingRebootFlag the flag may carry migrate's
        # "dd-uninstall"; the Summary re-write with "1" would erase the reason
        # AND refresh the mtime finalize compares against the last boot.
        code = self._code("install_prerequisites.ps1")
        self.assertRegex(
            code,
            r'if \(-not \(Test-Path \$FlagPath\)\) \{\s*'
            r'Set-Content -Path \$FlagPath -Value "1"',
            "install_prerequisites must write the reboot flag only when ABSENT "
            "— an existing flag keeps its content (migrate's dd-uninstall "
            "reason and its write time must survive)")

    def test_migrate_reboot_branch_precedes_the_still_present_branch(self):
        # After rc=3010 the Uninstall registry entry legitimately lingers until
        # the next boot, so "still present" is the EXPECTED state on that path.
        # Checking it first told the student to remove Docker Desktop manually
        # when the honest instruction is "reboot, the uninstaller finishes then".
        code = self._code("migrate_from_docker_desktop.ps1")
        self.assertLess(
            code.index("if ($rebootPending)"), code.index("if ($stillPresent)"),
            "migrate must route the rc=3010 case to the reboot message BEFORE "
            "the still-present manual-removal message")


class OneRebootPredicateTest(unittest.TestCase):
    """virtualization_ready.ps1 — the ONE reboot / virtualization verdict.

    THE INCIDENT (2026-09-07, German school PC). finalize_install.ps1 and
    verify_system.ps1 each answered "is a reboot outstanding" their own way, and
    the weaker answer guarded `wsl --import`. finalize asked the WSL/VMP feature
    store for EnablePending; verify compared flag-mtime against
    Win32_OperatingSystem.LastBootUpTime. They agree on every state EXCEPT the one
    a fresh install lands in — install_prerequisites.ps1 sets .reboot_required
    because `wsl --install --no-distribution` RAN, after which both features read
    Enabled — so finalize printed "Neustart bereits erfolgt" and imported into a
    hypervisor that was not running:

        Wsl/Service/RegisterDistro/CreateVm/HCS/HCS_E_SERVICE_NOT_AVAILABLE

    forever, across GUI launches, while the student was told to check their free
    disk space. These tests pin the one predicate, its LADDER ORDER (every rung is
    load-bearing and two of them exist only because the obvious order is wrong),
    and the dot-source contract that keeps a helper from exiting its caller.

    Since 2026-09-11 they also pin the PROOF layer. The two policies are ladders
    over the same three proofs, and each policy originally spelled all three
    inline — two copies of every proof inside the one file whose header claims
    its policies cannot drift apart. One state reader was never enough; one
    spelling per proof is the other half.
    """

    _code = staticmethod(RootCauseGuardTest._code)

    def _virt(self):
        return self._code("virtualization_ready.ps1")

    # ── The file exists and both consumers ask it ───────────────────────────
    def test_both_consumers_dot_source_the_one_predicate(self):
        for name in ("finalize_install.ps1", "verify_system.ps1"):
            with self.subTest(script=name):
                self.assertIn("virtualization_ready.ps1", self._code(name))

    def test_finalize_keeps_no_private_copy(self):
        code = self._code("finalize_install.ps1")
        self.assertNotIn("function Test-RebootStillPending", code,
                         "the private predicate must be GONE, not kept alongside "
                         "— a fifth implementation is not a fix")
        for probe in ("Get-WindowsOptionalFeature", "LastBootUpTime",
                      "HypervisorPresent"):
            self.assertNotIn(
                probe, code,
                f"finalize must not read {probe} itself; that copy IS the defect")

    def test_finalize_dot_source_is_test_path_guarded(self):
        code = self._code("finalize_install.ps1")
        self.assertLess(code.index("if (-not (Test-Path $virtHelper))"),
                        code.index(". $virtHelper"),
                        "an unguarded dot-source of a missing file THROWS, and a "
                        "partially-copied {app}\\scripts is a state this codebase "
                        "already anticipates (Controlled Folder Access)")
        self.assertLess(
            code.index("function Fail-WithNextAction"),
            code.index("$virtHelper = Join-Path"),
            "the guard must be able to report in German via Fail-WithNextAction; "
            "above it, a missing file reaches the GUI as an EMPTY transcript")

    # ── The dot-source contract ────────────────────────────────────────────
    def test_the_helper_cannot_exit_or_poison_its_caller(self):
        raw = _read(_VIRT_PS1, encoding="utf-8-sig")
        code = self._virt()
        self.assertNotRegex(
            code, r"(?m)^\s*(exit|throw)\b",
            "a dot-sourced file that exits terminates its CALLER — and "
            "ps-readiness-retry-lint discovers hard-exit helpers cross-file")
        self.assertNotRegex(
            code, r"\$ErrorActionPreference\s*=",
            "assigning EAP in a dot-sourced file poisons the caller; every "
            "consumer must keep EAP=Continue (ci.yml::powershell-native-stderr)")
        for native in ("wsl ", "docker "):
            self.assertNotIn(
                native, code,
                "the helper makes NO native calls — a readiness probe here would "
                "trip ps-readiness-retry-lint and could not be retried")
        self.assertTrue(raw.startswith("#"),
                        "BOM-stripped source must open with the header comment")

    def test_the_helper_is_not_in_the_terminal_exit_list(self):
        """ScriptTerminalExitTest's list must NOT grow to include this file.

        Those scripts end with `exit 0` because their exit code is a contract.
        This one is DOT-SOURCED: an `exit 0` at its end would end the caller
        mid-run, silently reporting success."""
        src = _read(os.path.join(os.path.dirname(__file__),
                                 "test_gui_install_lifecycle.py"))
        block = src[src.index("def test_scripts_end_with_explicit_exit_zero"):]
        block = block[:block.index("class ")]
        self.assertNotIn("virtualization_ready.ps1", block)

    # ── One spelling per proof ─────────────────────────────────────────────
    # Both policies are ladders over the SAME three proofs. Each originally
    # spelled all three INLINE, so every proof existed twice in one file — the
    # drift this file exists to prevent, one scope smaller. The state is read
    # once (Get-RebootState) AND each proof is written once (below); these pin
    # the second half, in the house style of tests/test_ros_domain_twin_lockstep
    # .py and test_activation_agent.py::TestGateParserLockstep — drive every
    # reader off one table, and assert the intended divergence POSITIVELY.
    #
    # SOURCE-LEVEL on purpose. A Python re-implementation of the ladder would be
    # a THIRD copy, free to drift from the .ps1 exactly like the two it replaced
    # (and PowerShell is not installed in CI, so it could never be executed
    # against the real thing either).
    _PROOFS = ("Test-DdUninstallOutstanding", "Test-FeatureEnablePending",
               "Test-NoBootSinceFlag")
    # What a RE-INLINED proof must necessarily contain: a read of the proof's own
    # state field, or the dd reason literal. HypervisorPresent and
    # VirtFirmwareEnabled are deliberately absent — those two rungs are the
    # verdict's OWN and must stay spelled there. Note the fence cannot be the
    # bare token "NoBootSinceFlag"/"EnablePending": both are substrings of the
    # predicate NAMES that replaced them, so it is the `$State.` read that is
    # forbidden.
    _INLINED_PROOF_TOKENS = ("$State.FlagReason", "$State.FlagPresent",
                             "$State.TimeReadable", "$State.NoBootSinceFlag",
                             "$State.EnablePending", "dd-uninstall")
    _POLICIES = ("Test-RebootOutstanding", "Get-VirtualizationVerdict")

    def test_every_proof_is_a_named_predicate(self):
        code = self._virt()
        for fn in self._PROOFS:
            with self.subTest(proof=fn):
                self.assertIn(
                    "function %s {" % fn, code,
                    f"{fn} must exist as ONE named predicate — an inline proof "
                    f"is a copy, and the next edit to a copy does not follow on "
                    f"the other policy, which is how finalize and verify came to "
                    f"disagree about the state a fresh install lands in")

    def test_neither_policy_re_spells_a_proof(self):
        """The anti-re-inlining fence, in both directions.

        If a policy stops CALLING a predicate, or starts reading a proof's state
        field itself, the file is back to two spellings of one proof and the
        2026-09-07 class of failure is reachable again inside a single file."""
        code = self._virt()
        for policy in self._POLICIES:
            body = _ps1_function_body(code, policy)
            for fn in self._PROOFS:
                with self.subTest(policy=policy, proof=fn):
                    self.assertIn(
                        fn, body,
                        f"{policy} must COMPOSE {fn}, not re-derive it — the "
                        f"proofs are the shared layer, and a policy that stops "
                        f"asking has forked it")
            for token in self._INLINED_PROOF_TOKENS:
                with self.subTest(policy=policy, inlined=token):
                    self.assertNotIn(
                        token, body,
                        f"{policy} reads {token} directly: that is a proof "
                        f"spelled a second time. Move it into one of "
                        f"{self._PROOFS} and call that instead, or the two "
                        f"policies can silently disagree again")

    def test_the_proof_predicates_honour_the_caller_contract(self):
        """The header's contract binds EVERY function here, not just the two the
        callers name. A $null state answers $false rather than throwing (the
        callers pass Get-RebootState's result straight through), no proof falls
        off the end returning nothing, and nothing exits the caller."""
        code = self._virt()
        for fn in self._PROOFS:
            body = _ps1_function_body(code, fn)
            with self.subTest(proof=fn):
                self.assertIn("if ($null -eq $State) { return $false }", body,
                              f"{fn} must answer $false on a $null state — "
                              f"Get-RebootState's callers pass its result "
                              f"through unguarded")
                self.assertIn("return $false", body,
                              f"{fn} must answer on NO PROOF, not fall off the "
                              f"end returning nothing")
                self.assertNotRegex(
                    body, r"(?m)^\s*(exit|throw)\b",
                    f"{fn} is dot-sourced into its caller; an exit there ends "
                    f"the INSTALLER mid-run")

    # ── The ladder. ORDER IS THE WHOLE DESIGN. ─────────────────────────────
    def test_the_verdict_returns_exactly_the_four_vocabulary_words(self):
        body = _ps1_function_body(self._virt(), "Get-VirtualizationVerdict")
        found = set(re.findall(r'return "([A-Za-z]+)"', body))
        self.assertEqual(
            found, {"Ready", "RebootRequired", "VirtualizationDisabled", "Unknown"},
            "the verdict vocabulary is the contract the .ps1 branches and the GUI "
            "exit codes are built on")

    def test_enable_pending_outranks_the_hypervisor_shortcut(self):
        """EnablePending is PROOF a feature enable waits on a reboot.

        It is also the only signal the old finalize ever looked at. A PC running
        a hypervisor for Hyper-V's own sake would skip it if HypervisorPresent
        came first — trading one blind spot for another."""
        body = _ps1_function_body(self._virt(), "Get-VirtualizationVerdict")
        self.assertLess(
            body.index("Test-FeatureEnablePending"), body.index("HypervisorPresent"),
            "EnablePending must be tested BEFORE the HypervisorPresent shortcut")

    def test_ground_truth_outranks_the_boot_time_inference(self):
        """HypervisorPresent is the thing the reboot was FOR; flag-mtime is a proxy.

        Windows Fast Startup makes LastBootUpTime unreliable, so trusting the
        proxy over ground truth tells a working PC to reboot forever."""
        body = _ps1_function_body(self._virt(), "Get-VirtualizationVerdict")
        # Rung 1 also rests on "no boot since the flag", so anchor on rung 4's
        # OWN call — the bare flag-time rung, the one that must NOT outrank
        # ground truth. (Before 2026-09-11 this anchored on the inline clause
        # `$State.FlagPresent -and …`, which the predicate extraction replaced.)
        rung4 = "Test-NoBootSinceFlag -State $State"
        self.assertIn(rung4, body, "rung 4's call changed shape")
        self.assertLess(
            body.index("HypervisorPresent"), body.index(rung4),
            "the Ready rung must precede the flag-time RebootRequired rung")

    def test_virtualization_firmware_is_only_read_below_the_hypervisor_rung(self):
        """A RUNNING hypervisor commonly MASKS VirtualizationFirmwareEnabled to
        $false — which is exactly why install_prerequisites.ps1 ORs the two. The
        refusal is sound ONLY because it is unreachable while HypervisorPresent is
        $true."""
        body = _ps1_function_body(self._virt(), "Get-VirtualizationVerdict")
        self.assertLess(
            body.index("HypervisorPresent"), body.index("VirtFirmwareEnabled"),
            "reading the firmware flag above the hypervisor rung would refuse "
            "working machines whose hypervisor masks it")

    def test_only_a_genuine_false_refuses(self):
        """`-eq $false`, never `-ne $true`.

        PowerShell evaluates `$null -eq $false` as $false, so an absent or
        unreadable property can never produce VirtualizationDisabled. `-ne $true`
        would refuse on every $null — fail-CLOSED on a WMI hiccup, which bricks
        working PCs."""
        body = _ps1_function_body(self._virt(), "Get-VirtualizationVerdict")
        self.assertRegex(body, r"\$State\.VirtFirmwareEnabled -eq \$false")
        self.assertNotIn("VirtFirmwareEnabled -ne $true", body)
        self.assertRegex(body, r"\$State\.HypervisorPresent -eq \$true")

    def test_unknown_is_the_fall_through_not_a_refusal(self):
        body = _ps1_function_body(self._virt(), "Get-VirtualizationVerdict")
        self.assertTrue(
            body.rstrip().rstrip("}").rstrip().endswith('return "Unknown"'),
            "Unknown must be the LAST rung: refuse only on proof, and let a WMI "
            "hiccup proceed to the import, which classifies its own failure")

    def test_reboot_outstanding_is_proof_only(self):
        code = self._virt()
        body = _ps1_function_body(code, "Test-RebootOutstanding")
        self.assertIn("return $false", body,
                      "every unreadable state must answer $false — that is "
                      "verify_system's documented SAFE direction")
        # The clock guard moved WITH the proofs on 2026-09-11 (this policy now
        # composes them), so assert it in both time-dependent predicates rather
        # than in the policy that no longer spells them.
        for fn in ("Test-DdUninstallOutstanding", "Test-NoBootSinceFlag"):
            with self.subTest(proof=fn):
                self.assertIn(
                    "TimeReadable", _ps1_function_body(code, fn),
                    f"{fn} compares times: it may only count when the clock was "
                    f"actually read, or an unreadable clock manufactures a "
                    f"benign pending-reboot verdict over a broken install")

    # ── The classifier reads CODES, never German prose ─────────────────────
    def test_the_failure_classifier_matches_codes_not_messages(self):
        body = _ps1_function_body(self._virt(), "Get-WslFailureClass")
        self.assertIn("HCS_E_SERVICE_NOT_AVAILABLE", body,
                      "the code the field log actually carried")
        self.assertIn("0x80370102", body)
        # The 2026-09-07 transcript carried a GERMAN sentence beside an ASCII code
        # token. Matching prose is the mistake install_prerequisites.ps1 already
        # corrected when it deleted its English-only `systeminfo | Select-String
        # "Hyper-V Requirements"` probe, which never matched on a German PC.
        for german in ("Vorgang", "erforderliches Feature", "Fehlercode"):
            self.assertNotIn(
                german, body,
                "classify on the CODE token; a localized message cannot be "
                "matched on the student PCs this ships to")

    def test_the_classifier_survives_out_string_wrapping(self):
        """`Fehlercode: Wsl/.../HCS_E_SERVICE_NOT_AVAILABLE` is 79 characters.

        Out-String wraps at the host width, so the token is one character from
        being split in half. The caller passes -Width 4096 AND the classifier
        strips whitespace — belt and brace, because a caller that forgets the
        width must still classify."""
        body = _ps1_function_body(self._virt(), "Get-WslFailureClass")
        # Whitespace AND NUL: the NUL half is executed byte-for-byte in
        # test_installer_pwsh_executed.ClassifierExecutedTest, which is the test
        # that actually bites if either half is dropped.
        self.assertRegex(body, r"\$flat = \(\$Text -replace '\[\\s\\x00\]', ''\)")
        self.assertIn("$flat -like", body,
                      "the match must run against the stripped copy, or the "
                      "normalisation is decoration")
        self.assertIn("Out-String -Width 4096",
                      _read(_IMPORT_PS1, encoding="utf-8-sig"))

    # ── The import actually captures what it classifies ───────────────────
    def test_import_captures_and_echoes_the_wsl_output(self):
        code = self._code("import_edubotics_wsl.ps1")
        self.assertIn("$importOut = ", code,
                      "the import must CAPTURE wsl's words to classify them")
        self.assertIn("$importExit = $LASTEXITCODE", code,
                      "the exit code must be snapshotted before any later native "
                      "call can overwrite $LASTEXITCODE")
        self.assertIn("Write-Host $importOut.TrimEnd()", code,
                      "and echo them VERBATIM — capturing without echoing would "
                      "DELETE the evidence the field log carried")
        # Anchored on the IMPORT's classify call: since 2026-09-11 the stamp
        # read of an existing distro is classified too, earlier in the file.
        self.assertLess(code.index("$importOut = "),
                        code.index("Get-WslFailureClass -Text $importOut"),
                        "capture before classify")

    def test_import_keeps_the_old_triad_for_non_hypervisor_failures(self):
        code = self._code("import_edubotics_wsl.ps1")
        self.assertIn("Antivirus-Ausnahme, genug Speicherplatz, WSL2 aktiviert", code,
                      "the disk/AV wording is still RIGHT for the failures it "
                      "fits; only the hypervisor class was mis-served by it")
        self.assertIn('if ($virtHelperOk) { $failClass = Get-WslFailureClass', code,
                      "a missing helper must degrade to the old wording, never "
                      "hard-fail the installer's own [Run] Step 4")

    # ── The misleading sentence is gone for good ──────────────────────────
    def test_the_false_reassurance_is_deleted(self):
        code = self._code("finalize_install.ps1")
        self.assertNotIn(
            "Neustart bereits erfolgt", code,
            "this is the line the field log printed immediately before importing "
            "into a dead hypervisor; it asserts a fact the script cannot know")

    def test_the_verdict_gate_is_not_gated_on_the_flag(self):
        """The virtualization rungs are flag-INDEPENDENT.

        Wrapping the gate in `if (Test-Path $flagPath)` — as the old reboot check
        was — means a PC with no flag and a dead hypervisor walks straight into
        the import."""
        code = self._code("finalize_install.ps1")
        gate = code.index("Get-VirtualizationVerdict -State $rebootState")
        window = code[max(0, gate - 400):gate]
        self.assertNotIn("if (Test-Path $flagPath) {", window,
                         "the verdict gate must run unconditionally")

    def test_there_is_one_marker_writer(self):
        """$EXIT_VIRT goes out through Fail-WithNextAction, not a second
        Set-Content of the FAILED marker shape — the copy would be the one that
        forgets -Encoding UTF8 (see the marker paragraph in the .ps1 header)."""
        code = self._code("finalize_install.ps1")
        self.assertEqual(
            code.count('Value ("FAILED {0}'), 1,
            "exactly one writer of the FAILED marker shape")
        self.assertIn("[int]$ExitCode = $EXIT_FAILED", code,
                      "the routed codes reuse that writer via a parameter")
        self.assertEqual(code.count("function Fail-WithNextAction"), 1)

    def test_the_virt_remedy_is_declared_once(self):
        """A duplicated German remedy is the class this change set removes.

        Both paths that exit 11 — the pre-import verdict and import's own
        classification — must say the SAME thing, and the way the old code got
        „Prüfen Sie: Antivirus-Ausnahme, genug Speicherplatz" printed over a
        hypervisor fault was exactly one remedy written in one place and used
        for every cause."""
        code = self._code("finalize_install.ps1")
        self.assertEqual(code.count("$VIRT_PROBLEM_DE  ="), 1,
                         "declared exactly once")
        self.assertEqual(code.count("Fail-WithNextAction $VIRT_PROBLEM_DE "), 2,
                         "and used by BOTH exit-11 paths")
        self.assertNotIn(
            'Fail-WithNextAction "Die Virtualisierung', code,
            "no inline copy of the German remedy may remain")
        for half in ("neu starten", "BIOS/UEFI"):
            self.assertIn(half, code, "both remedies must be named")

    def test_finalize_rotates_its_log_instead_of_destroying_it(self):
        code = self._code("finalize_install.ps1")
        self.assertIn(".prev.log", code,
                      "the previous attempt's transcript is the only thing that "
                      "can show what CHANGED on a rig that loops identically")
        self.assertLess(code.index(".prev.log"), code.index("Start-Transcript"),
                        "rotate before the new transcript opens")


class TranscriptExcerptTest(unittest.TestCase):
    """_transcript_excerpt — HEAD + TAIL, because a bare tail lost the evidence.

    The 2026-09-07 field log is the proof. `lines[-25:]` / `lines[-30:]` kept the
    LAST lines of the elevated transcripts, so the
    `Start-Transcript -IncludeInvocationHeader` block — Windows account, computer
    name, Windows build, PSVersion — was cut, along with
    install_prerequisites.ps1's "Checking virtualization support..." output. What
    the window DID keep was the three lines `WSManStackVersion:`,
    `PSRemotingProtocolVersion:`, `SerializationVersion:`, which say nothing
    about any machine. That is the shape of the fix: keep the head, drop the
    noise, and keep the timestamps.
    """

    @staticmethod
    def _fn():
        ns = {"os": os}
        exec(compile(_module_fn_src("_transcript_excerpt"), _GUI_SRC, "exec"), ns)
        return ns["_transcript_excerpt"]

    def _write(self, body):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "t.log")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        self.addCleanup(shutil.rmtree, tmp, True)
        return path

    # A faithful miniature of the real transcript shape, including the three
    # noise lines that the old tail kept and the header lines it dropped.
    _HEADER = (
        "**********************\n"
        "Windows PowerShell-Transkript, Start\n"
        "Startzeit: 20260907141240\n"
        "Benutzername: SCHULE\\schueler01\n"
        "Computer: PC-RAUM-12 (Microsoft Windows NT 10.0.26100.0)\n"
        "PSVersion: 5.1.26100.1234\n"
        "WSManStackVersion: 3.0\n"
        "PSRemotingProtocolVersion: 2.3\n"
        "SerializationVersion: 1.1.0.1\n"
        "**********************\n"
    )

    def test_the_invocation_header_survives(self):
        body = self._HEADER + "".join(f"Zeile {i}\n" for i in range(200))
        out = self._fn()(self._write(body), head=12, tail=30)
        joined = "\n".join(out)
        for evidence in ("Benutzername: SCHULE\\schueler01",
                         "Computer: PC-RAUM-12",
                         "PSVersion: 5.1.26100.1234",
                         "Startzeit: 20260907141240"):
            self.assertIn(evidence, joined,
                          "this is exactly what support needs and what the bare "
                          "tail threw away")

    def test_the_noise_lines_are_dropped(self):
        body = self._HEADER + "".join(f"Zeile {i}\n" for i in range(200))
        out = self._fn()(self._write(body), head=12, tail=30)
        joined = "\n".join(out)
        for noise in ("WSManStackVersion:", "PSRemotingProtocolVersion:",
                      "SerializationVersion:"):
            self.assertNotIn(noise, joined,
                             "these three were the ONLY header lines the old "
                             "tail kept, and they describe no machine")
        self.assertNotIn("**********", joined)

    def test_the_tail_is_still_the_tail(self):
        body = self._HEADER + "".join(f"Zeile {i}\n" for i in range(200))
        out = self._fn()(self._write(body), head=12, tail=30)
        self.assertEqual(out[-1], "Zeile 199",
                         "the failure and its German remedy live at the END")

    def test_the_elision_is_announced(self):
        body = self._HEADER + "".join(f"Zeile {i}\n" for i in range(200))
        out = self._fn()(self._write(body), head=12, tail=30)
        self.assertTrue(
            any("ausgelassen" in ln for ln in out),
            "nobody may read an excerpt as the whole file")

    def test_a_short_transcript_is_returned_whole(self):
        body = "Zeile A\nZeile B\n\nZeile C\n"
        out = self._fn()(self._write(body), head=12, tail=30)
        self.assertEqual(out, ["Zeile A", "Zeile B", "Zeile C"],
                         "blank lines dropped, nothing elided, no marker")

    def test_the_endzeit_timestamp_is_kept(self):
        body = self._HEADER + "".join(f"Zeile {i}\n" for i in range(200)) + (
            "**********************\n"
            "Ende der Windows PowerShell-Aufzeichnung\n"
            "Endzeit: 20260907141249\n"
            "**********************\n")
        out = self._fn()(self._write(body), head=12, tail=30)
        self.assertIn("Endzeit: 20260907141249", "\n".join(out),
                      "a transcript's only timestamps; on a rig that loops, WHEN "
                      "each attempt ran is the question being asked")

    def test_all_three_call_sites_use_the_shared_excerpt(self):
        """One implementation, three transcripts.

        The repair transcript is the one that carried
        install_prerequisites.ps1's virtualization and feature output, so leaving
        any call site on a bare tail keeps the evidence loss alive."""
        src = _read(_GUI_SRC)
        self.assertEqual(
            src.count("_transcript_excerpt(log_file"), 3,
            "Setup-, Reparatur- and Freigabe-Protokoll must all use it")
        self.assertNotRegex(
            src, r"tail = lines\[-\d+:\]",
            "no bare tail slice may remain")


class RebootAwareScanDiagnosisTest(unittest.TestCase):
    """The arm-scan diagnosis must not contradict the GUI's own entry check.

    In the field log the GUI printed „Ein ausstehender Windows-Neustart hat die
    Einrichtung unterbrochen." and then, seconds later in the same session,
    „Die EduBotics-WSL-Umgebung ist nicht registriert. Bitte den Installer erneut
    ausführen." — two stories about one machine, and the second sends the student
    to a remedy that cannot help.
    """

    def test_the_distro_missing_diagnosis_is_reboot_aware(self):
        src = _method_src("_scan_arms")
        self.assertIn("wsl_distro_missing", src,
                      "the decision must key on the STRUCTURED diagnosis flag, "
                      "never on matching the German text")
        self.assertIn("self._reboot_required_pending()", src,
                      "and on the ONE flag predicate the GUI already owns — "
                      "device_manager knows nothing about {app}\\scripts")

    def test_the_override_does_not_claim_a_pending_reboot(self):
        """.reboot_required means "the deferred work is not finished", which is
        true of EVERY failed finalize. Asserting a reboot is the exact conflation
        the finalize header warns about."""
        src = _method_src("_scan_arms")
        start = src.index("message_de = (")
        block = src[start:start + 600]
        self.assertIn("noch nicht abgeschlossen", block)
        self.assertNotIn("Neustart steht", block)
        self.assertIn("Einrichtung abschließen", block,
                      "the actionable remedy is finishing setup, not re-running "
                      "the installer")

    def test_the_short_status_follows_the_same_message(self):
        """A status bar that still quotes diag.message_de would show the
        contradicted sentence while the log shows the corrected one."""
        src = _method_src("_scan_arms")
        self.assertIn("short_status = message_de.splitlines()[0]", src)
        self.assertNotIn("short_status = diag.message_de", src)

    def test_the_technical_details_are_still_surfaced(self):
        """Overriding the STUDENT sentence must not hide the English details
        support reads — they are what proved the distro was absent."""
        src = _method_src("_scan_arms")
        self.assertIn("diag.details", src)


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

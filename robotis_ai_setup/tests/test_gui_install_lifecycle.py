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
              reboot_pending=False, distro_registered=True, script="finalize.ps1"):
        calls = {"elevate": [], "log": [], "status": [], "prereq": 0,
                 "showinfo": [], "showwarning": [], "showerror": [],
                 "askyesno": [], "fallback": []}

        def _fake_elevate(exe, args, show=1):
            calls["elevate"].append(args)
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
            "_edubotics_diag_dir": lambda: tempfile.gettempdir(),
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
                     "pull_images.ps1", "migrate_from_docker_desktop.ps1"):
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
        self.assertIn(
            "LastBootUpTime", code,
            "must discriminate flag-mtime vs last-boot-time, not existence")
        self.assertRegex(
            code, r"\$rebootPending\s*=\s*\(\$bootTime\s+-le\s+\$flagTime\)",
            "pending means: no boot has happened since the flag was written")

    def test_verify_unreadable_clock_reports_failed_not_pending(self):
        code = self._code("verify_system.ps1")
        init = re.search(r"\$rebootPending\s*=\s*\$false", code)
        self.assertIsNotNone(
            init, "$rebootPending must DEFAULT to $false so a throwing "
                  "Get-CimInstance/Get-Item reports FAILED — the safe "
                  "direction — instead of a benign pending reboot")
        self.assertLess(
            init.start(), code.index("LastBootUpTime"),
            "the $false default must precede the probe it guards")

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
        code = self._code("finalize_install.ps1")
        self.assertIn("dd-uninstall", code,
                      "finalize no longer reads the dd-uninstall reason — the "
                      "feature-store probe alone cannot see a pending DD removal")
        self.assertIn("LastBootUpTime", code,
                      "the dd-uninstall reason must be settled by comparing the "
                      "flag's write time against the last boot time — any other "
                      "signal either loops the student (a lingering registry "
                      "entry) or trusts the honor system (the dialog)")
        # The dd discrimination must run BEFORE the feature-store loop, so a
        # not-yet-rebooted DD removal defers even though the features read
        # Enabled.
        self.assertLess(code.index("dd-uninstall"),
                        code.index("Get-WindowsOptionalFeature"))

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

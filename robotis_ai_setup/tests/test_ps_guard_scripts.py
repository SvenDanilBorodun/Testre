"""Fixture tests for the four PowerShell CI guard scripts (.github/scripts/).

Each guard ships with the fixture that reproduces the ORIGINAL bug it was
written for, so "the guard flags the original pattern" is pinned by CI instead
of being a claim in a PR description. This matters: the first version of
ps_readiness_retry_lint used a 10-line keyword lookbehind for its "enclosing
loop" check, and the real original bug (pull_images.ps1's single-shot
`docker info` probe) sat 9 lines below an UNRELATED, ALREADY-CLOSED `foreach`
— the lookbehind mistook it for an enclosing loop and the guard silently
passed the very file it was built for. The readiness fixtures below therefore
reproduce that exact shape (closed foreach directly above the probe).

Deps-free and cross-platform: the guards are plain Python run via
sys.executable with PS_LINT_ROOT pointed at a throwaway fixture tree.
"""

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

_SCRIPTS_DIR = (
    pathlib.Path(__file__).resolve().parents[2] / ".github" / "scripts"
)

_ALL_GUARDS = (
    "ps_readiness_retry_lint.py",
    "ps_temp_cmdlet_lint.py",
    "ps_cleanup_exit_guard.py",
    "ps_temp_path_normalize_lint.py",
)


def _run_guard(script_name, fixture_files):
    """Run one guard against a temp tree containing `fixture_files`.

    fixture_files: {relative_path: content}. Returns (returncode, stdout).
    """
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        for rel, content in fixture_files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        env = dict(os.environ, PS_LINT_ROOT=str(root))
        proc = subprocess.run(
            [sys.executable, str(_SCRIPTS_DIR / script_name)],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        return proc.returncode, proc.stdout


# The original F3 bug, byte-shaped like main's pull_images.ps1: a single-shot
# `docker info` probe with `exit 1`, sitting a few lines below an unrelated
# foreach loop that has ALREADY CLOSED. A naive lookbehind sees the `foreach`
# and stays silent — the regression this fixture pins.
_ORIGINAL_F3 = """\
$listed = $false
try {
    $out = wsl --list --quiet 2>&1
    foreach ($line in $out) {
        if (($line -replace "`0", "").Trim() -eq $DistroName) { $listed = $true; break }
    }
} catch { }
if (-not $listed) {
    Write-Host "ERROR: distro not found" -ForegroundColor Red
    exit 1
}

wsl -d $DistroName -- docker info *>$null 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Docker engine not running" -ForegroundColor Red
    exit 1
}
"""

# The proven-safe shape: the probe polls inside a bounded while loop.
_FIXED_F3 = """\
$elapsed = 0
while ($elapsed -lt 60) {
    & wsl -d $DistroName -- docker info 1>$null 2>$errFile
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Seconds 2
    $elapsed += 2
}
if (-not $ready) {
    Write-Host "ERROR: not ready"
    exit 1
}
"""

# A comment mentioning "for"/"while" above the probe must NOT suppress the
# finding (the other half of the lookbehind failure mode).
_F3_COMMENT_DECOY = """\
# We wait for docker here; while the VM boots this can take a moment.
wsl -d EduBotics -- docker info *>$null 2>&1
if ($LASTEXITCODE -ne 0) {
    exit 1
}
"""


class TestReadinessRetryLint(unittest.TestCase):
    SCRIPT = "ps_readiness_retry_lint.py"

    def test_flags_original_bug_below_closed_foreach(self):
        rc, out = _run_guard(
            self.SCRIPT, {"robotis_ai_setup/pull_images.ps1": _ORIGINAL_F3}
        )
        self.assertEqual(rc, 1, out)
        self.assertIn("docker info", out)

    def test_comment_loop_keywords_do_not_suppress(self):
        rc, out = _run_guard(
            self.SCRIPT, {"robotis_ai_setup/decoy.ps1": _F3_COMMENT_DECOY}
        )
        self.assertEqual(rc, 1, out)

    def test_passes_probe_inside_retry_loop(self):
        rc, out = _run_guard(
            self.SCRIPT, {"robotis_ai_setup/fixed.ps1": _FIXED_F3}
        )
        self.assertEqual(rc, 0, out)

    def test_allow_comment_respected(self):
        allowed = _ORIGINAL_F3.replace(
            "wsl -d $DistroName -- docker info *>$null 2>&1",
            "wsl -d $DistroName -- docker info *>$null 2>&1  "
            "# ps-readiness-retry-lint: allow",
        )
        rc, out = _run_guard(
            self.SCRIPT, {"robotis_ai_setup/allowed.ps1": allowed}
        )
        self.assertEqual(rc, 0, out)

    def test_probe_without_hard_exit_not_flagged(self):
        diagnostic = (
            "wsl -d EduBotics -- docker info *>$null 2>&1\n"
            "if ($LASTEXITCODE -eq 0) { Write-Host OK } "
            "else { Write-Host FAIL }\n"
        )
        rc, out = _run_guard(
            self.SCRIPT, {"robotis_ai_setup/diag.ps1": diagnostic}
        )
        self.assertEqual(rc, 0, out)

    # ── Hard-exit shapes that a literal-`exit`-only matcher walked past. ──

    def test_flags_throw_as_hard_exit(self):
        """`throw` terminates as hard as `exit 1`."""
        fixture = (
            "wsl -d EduBotics -- docker info *>$null 2>&1\n"
            "if ($LASTEXITCODE -ne 0) {\n"
            '    throw "Docker-Engine laeuft nicht."\n'
            "}\n"
        )
        rc, out = _run_guard(
            self.SCRIPT, {"robotis_ai_setup/throws.ps1": fixture}
        )
        self.assertEqual(rc, 1, out)

    def test_flags_hard_exit_helper_call(self):
        """The house idiom: a helper whose body ends in `exit 1`.

        finalize_install.ps1 defines Fail-WithNextAction (4 call sites) and it
        is the ONLY way those scripts abort — a matcher that only knew the
        literal `exit` keyword was blind to every one of them. The helper set is
        DISCOVERED by scanning function bodies, not allowlisted.
        """
        fixture = (
            "function Fail-WithNextAction {\n"
            "    param([string]$msg)\n"
            "    Write-Host $msg\n"
            "    exit 1\n"
            "}\n"
            "wsl -d EduBotics -- docker info *>$null 2>&1\n"
            "if ($LASTEXITCODE -ne 0) { Fail-WithNextAction "
            '"Docker-Engine laeuft nicht." }\n'
        )
        rc, out = _run_guard(
            self.SCRIPT, {"robotis_ai_setup/helper.ps1": fixture}
        )
        self.assertEqual(rc, 1, out)
        self.assertIn("Fail-WithNextAction", out)

    def test_hard_exit_helper_discovered_across_files(self):
        """These scripts dot-source each other, so the scan is cross-file."""
        rc, out = _run_guard(
            self.SCRIPT,
            {
                "robotis_ai_setup/lib.ps1": (
                    "function Fail-Hard { param([string]$m); exit 1 }\n"
                ),
                "robotis_ai_setup/user.ps1": (
                    ". $PSScriptRoot\\lib.ps1\n"
                    "wsl -d EduBotics -- docker info *>$null 2>&1\n"
                    'if ($LASTEXITCODE -ne 0) { Fail-Hard "kaputt" }\n'
                ),
            },
        )
        self.assertEqual(rc, 1, out)
        self.assertIn("user.ps1", out)

    def test_do_while_terminator_does_not_mark_the_next_block_a_loop(self):
        """`} while (...)` CLOSES a loop; it must not tag the following `{`.

        The dangling-keyword tail scan saw the `while` of a do-loop terminator
        and marked the next `{` — here the `if` block holding the probe — as a
        loop body, silencing the finding. Only the ADJACENT form was affected
        (a blank line in between reset the flag), which is exactly the kind of
        gap that survives review.
        """
        fixture = (
            "$ready = $false\n"
            "$elapsed = 0\n"
            "do {\n"
            "    Start-Sleep -Seconds 1\n"
            "    $elapsed += 1\n"
            "} while ($elapsed -lt 3)\n"
            "if (-not $ready) {\n"
            "    wsl -d EduBotics -- docker info *>$null 2>&1\n"
            "    exit 1\n"
            "}\n"
        )
        rc, out = _run_guard(
            self.SCRIPT, {"robotis_ai_setup/dowhile.ps1": fixture}
        )
        self.assertEqual(rc, 1, out)

    def test_allman_brace_loop_header_still_suppresses(self):
        """The tail_veto must not break a genuine dangling loop header."""
        fixture = (
            "$elapsed = 0\n"
            "while ($elapsed -lt 60)\n"
            "{\n"
            "    wsl -d EduBotics -- docker info *>$null 2>&1\n"
            "    if ($LASTEXITCODE -eq 0) { break }\n"
            "    Start-Sleep -Seconds 2\n"
            "    $elapsed += 2\n"
            "}\n"
            "if (-not $ready) { exit 1 }\n"
        )
        rc, out = _run_guard(
            self.SCRIPT, {"robotis_ai_setup/allman.ps1": fixture}
        )
        self.assertEqual(rc, 0, out)

    def test_exit_in_a_sibling_block_is_out_of_scope(self):
        """install_prerequisites.ps1's shape: the `exit 1` belongs to the LATER
        `wsl --install` failure path, not to the probe.

        It passed only because that `exit 1` happened to land 9 lines below an
        8-line window — one line of churn would have flipped a correct pattern
        into a false positive. Here it sits 7 lines below, inside the raw
        window; the depth bound (the probe's `try` closes first) is what keeps
        it out.
        """
        fixture = (
            "$wslInstalled = $false\n"
            "try {\n"
            "    $wslStatus = wsl --status 2>&1\n"
            "    if ($LASTEXITCODE -eq 0) { $wslInstalled = $true }\n"
            "} catch { }\n"
            "if (-not $wslInstalled) {\n"
            "    wsl --install --no-distribution\n"
            "    if ($LASTEXITCODE -ne 0) {\n"
            '        Write-Host "ERROR: WSL2 installation failed."\n'
            "        exit 1\n"
            "    }\n"
            "}\n"
        )
        rc, out = _run_guard(
            self.SCRIPT, {"robotis_ai_setup/scoped.ps1": fixture}
        )
        self.assertEqual(rc, 0, out)


# The original F2 bug: a $env:TEMP-derived path bound to -LiteralPath outside
# try/catch (import_edubotics_wsl.ps1 pre-fix).
_ORIGINAL_F2 = """\
$dockerErrFile = Join-Path $env:TEMP "edubotics_dockerinfo.err"
$lastErr = (Get-Content -LiteralPath $dockerErrFile -Raw -ErrorAction SilentlyContinue)
Remove-Item -LiteralPath $dockerErrFile -Force -ErrorAction SilentlyContinue
"""

_FIXED_F2 = """\
$dockerErrFile = [System.IO.Path]::GetTempFileName()
$lastErr = ""; try { $lastErr = Get-Content -LiteralPath $dockerErrFile -Raw -ErrorAction Stop } catch { }
try { Remove-Item -LiteralPath $dockerErrFile -Force -ErrorAction SilentlyContinue } catch { }
exit 0
"""


class TestTempCmdletLint(unittest.TestCase):
    SCRIPT = "ps_temp_cmdlet_lint.py"

    def test_flags_original_unguarded_temp_cmdlets(self):
        rc, out = _run_guard(
            self.SCRIPT, {"robotis_ai_setup/orig.ps1": _ORIGINAL_F2}
        )
        self.assertEqual(rc, 1, out)
        # Both the Get-Content and the Remove-Item must be flagged.
        self.assertEqual(out.count("::error"), 2, out)

    def test_passes_try_catch_guarded_form(self):
        rc, out = _run_guard(
            self.SCRIPT, {"robotis_ai_setup/fixed.ps1": _FIXED_F2}
        )
        self.assertEqual(rc, 0, out)

    # ── The three misfires the shared _ps_lex preprocessing closes. ──

    def test_trailing_comment_ending_in_try_does_not_suppress(self):
        """FALSE NEGATIVE: `$retries = 3   # one more try`.

        The dangling-`try` tail scan ran on the RAW line, so prose ending in
        the word "try" marked the next `{` as a try-body and silenced every
        temp cmdlet inside it. Change the comment to "# one more attempt" and
        the identical file fired — the same class 8c36c42 fixed in the
        readiness lint ("prose cannot suppress a finding").
        """
        fixture = (
            "$retries = 3   # one more try\n"
            "if ($retries -gt 0) {\n"
            '    $tmpLog = Join-Path $env:TEMP "edubotics.log"\n'
            "    Remove-Item -LiteralPath $tmpLog -Force "
            "-ErrorAction SilentlyContinue\n"
            "}\n"
        )
        rc, out = _run_guard(
            self.SCRIPT, {"robotis_ai_setup/comment_try.ps1": fixture}
        )
        self.assertEqual(rc, 1, out)

    def test_closing_brace_inside_a_string_does_not_pop_the_try(self):
        """FALSE POSITIVE: a `}` in prose popped a real, still-open try-brace."""
        fixture = (
            '$tmpLog = Join-Path $env:TEMP "edubotics.log"\n'
            "try {\n"
            '    Write-Host "closing brace in prose: }"\n'
            "    Remove-Item -LiteralPath $tmpLog -Force "
            "-ErrorAction SilentlyContinue\n"
            "} catch { }\n"
        )
        rc, out = _run_guard(
            self.SCRIPT, {"robotis_ai_setup/brace_string.ps1": fixture}
        )
        self.assertEqual(rc, 0, out)

    def test_cmdlet_named_only_inside_a_string_is_not_a_call(self):
        """FALSE POSITIVE: a cmdlet quoted in a German hint is not executed."""
        fixture = (
            'Write-Host "Bitte loeschen: Remove-Item '
            '$env:TEMP\\edubotics.log"\n'
        )
        rc, out = _run_guard(
            self.SCRIPT, {"robotis_ai_setup/prose.ps1": fixture}
        )
        self.assertEqual(rc, 0, out)

    def test_temp_path_inside_a_string_is_still_a_finding(self):
        """The other half: blanking strings must NOT hide the PATH.

        `Remove-Item "$env:TEMP\\x"` is a real finding even though the env var
        lives inside the quotes — which is why the guard lexes each line twice
        (strings blanked for "is this code", strings kept for "on what path").
        """
        fixture = 'Remove-Item -LiteralPath "$env:TEMP\\edubotics.log" -Force\n'
        rc, out = _run_guard(
            self.SCRIPT, {"robotis_ai_setup/instring.ps1": fixture}
        )
        self.assertEqual(rc, 1, out)


# The REAL pull_images.ps1 tail, with ONLY its trailing `exit 0` removed — the
# exact line ps_cleanup_exit_guard's docstring says it exists to protect. Note
# what follows the prune: two Write-* CMDLETS and three comment lines. Cmdlets
# do NOT reset $LASTEXITCODE, so the hazard is fully live, yet the original
# "the last non-comment LINE is a cleanup" rule saw a `Write-Step` at the tail
# and reported OK on the very file it was written for. The old fixture was a
# 2-line synthetic where the prune WAS literally the last line — it passed
# while resembling nothing in the tree.
_PULL_IMAGES_TAIL_NO_EXIT = """\
foreach ($img in $images) {
    wsl -d $DistroName -- docker pull $img
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: pull failed"
        exit 1
    }
}

# Reclaim the layers the superseded tags were holding.
wsl -d $DistroName -- docker image prune -f *>$null
Write-OK   "Aufraeumen abgeschlossen"
Write-Step "All $total images pulled successfully!"

# Explicit success: don't let the script's exit code fall through to the prune's
# $LASTEXITCODE. A transient non-zero prune would otherwise make the finalize
# (post-reboot) caller falsely report a failed image step.
"""


class TestCleanupExitGuard(unittest.TestCase):
    SCRIPT = "ps_cleanup_exit_guard.py"

    def test_flags_prune_followed_only_by_cmdlets_and_comments(self):
        rc, out = _run_guard(
            self.SCRIPT,
            {"robotis_ai_setup/pull_images.ps1": _PULL_IMAGES_TAIL_NO_EXIT},
        )
        self.assertEqual(rc, 1, out)
        self.assertIn("docker image prune", out)

    def test_passes_when_the_exit0_is_restored(self):
        rc, out = _run_guard(
            self.SCRIPT,
            {
                "robotis_ai_setup/pull_images.ps1":
                    _PULL_IMAGES_TAIL_NO_EXIT + "exit 0\n"
            },
        )
        self.assertEqual(rc, 0, out)

    def test_flags_terminal_cleanup_without_exit0(self):
        fixture = (
            "wsl -d EduBotics -- docker pull img\n"
            "wsl -d EduBotics -- docker image prune -f\n"
        )
        rc, out = _run_guard(
            self.SCRIPT, {"robotis_ai_setup/prune_last.ps1": fixture}
        )
        self.assertEqual(rc, 1, out)

    def test_passes_with_explicit_exit0(self):
        fixture = (
            "wsl -d EduBotics -- docker pull img\n"
            "wsl -d EduBotics -- docker image prune -f\n"
            "exit 0\n"
        )
        rc, out = _run_guard(
            self.SCRIPT, {"robotis_ai_setup/prune_exit0.ps1": fixture}
        )
        self.assertEqual(rc, 0, out)

    def test_flags_cleanup_sharing_line_with_exit1(self):
        fixture = "Remove-Item $tmpFile -Force; exit 1\n" "exit 0\n"
        rc, out = _run_guard(
            self.SCRIPT, {"robotis_ai_setup/inline.ps1": fixture}
        )
        self.assertEqual(rc, 1, out)

    def test_erroraction_is_not_a_scratch_path(self):
        """FALSE POSITIVE: SCRATCH_TOKEN was `(temp|tmp|err)` ANYWHERE on the
        line, so the `Err` of `-ErrorAction` classified a delete of a real
        STATE file as a scratch cleanup — flatly contradicting the guard's own
        carve-out for finalize_install.ps1's `.reboot_required` flag.
        """
        fixture = (
            '$flagPath = Join-Path $PSScriptRoot ".reboot_required"\n'
            "if (Test-Path $flagPath) { Remove-Item $flagPath -Force "
            "-ErrorAction SilentlyContinue; exit 1 }\n"
            "exit 0\n"
        )
        rc, out = _run_guard(
            self.SCRIPT, {"robotis_ai_setup/statefile.ps1": fixture}
        )
        self.assertEqual(rc, 0, out)

    def test_real_scratch_file_still_flagged(self):
        """TRUE POSITIVE the narrowed token must keep: a *file identifier."""
        fixture = (
            "$dockerErrFile = [System.IO.Path]::GetTempFileName()\n"
            "Remove-Item -LiteralPath $dockerErrFile -Force; exit 1\n"
            "exit 0\n"
        )
        rc, out = _run_guard(
            self.SCRIPT, {"robotis_ai_setup/scratch.ps1": fixture}
        )
        self.assertEqual(rc, 1, out)

    def test_cleanup_named_only_in_prose_is_not_a_finding(self):
        fixture = (
            "# Manual recovery: wsl -d EduBotics -- docker image prune -f\n"
            'Write-Host "Aufraeumen: wsl -d EduBotics -- docker image '
            'prune -f"\n'
        )
        rc, out = _run_guard(
            self.SCRIPT, {"robotis_ai_setup/prose.ps1": fixture}
        )
        self.assertEqual(rc, 0, out)

    def test_dot_sourced_helper_library_without_exit_is_not_a_finding(self):
        """wsl_docker_ready.ps1 is a FUNCTION LIBRARY: no `exit` anywhere, by
        design. Its `Remove-Item $errFile` is a CMDLET and can never set
        $LASTEXITCODE, so rule (a) is native-command-only.
        """
        fixture = (
            "function Wait-DockerReady {\n"
            "    $errFile = [System.IO.Path]::GetTempFileName()\n"
            "    try { Remove-Item -LiteralPath $errFile -Force "
            "-ErrorAction SilentlyContinue } catch { }\n"
            "    return $true\n"
            "}\n"
        )
        rc, out = _run_guard(
            self.SCRIPT, {"robotis_ai_setup/wsl_docker_ready.ps1": fixture}
        )
        self.assertEqual(rc, 0, out)


class TestTempPathNormalizeLint(unittest.TestCase):
    SCRIPT = "ps_temp_path_normalize_lint.py"

    def test_advisory_warns_but_never_fails(self):
        fixture = '$p = Join-Path $env:TEMP "x.log"\n'
        rc, out = _run_guard(
            self.SCRIPT, {"robotis_ai_setup/raw.ps1": fixture}
        )
        self.assertEqual(rc, 0, out)  # advisory: exit 0 even with findings
        self.assertIn("::warning", out)

    def test_normalized_form_silent(self):
        fixture = (
            "$p = Join-Path ([System.IO.Path]::GetTempPath()) 'x.log'\n"
        )
        rc, out = _run_guard(
            self.SCRIPT, {"robotis_ai_setup/norm.ps1": fixture}
        )
        self.assertEqual(rc, 0, out)
        self.assertNotIn("::warning", out)


class TestGuardsCleanOnRealTree(unittest.TestCase):
    """ALL FOUR guards must be green on the actual repo tree.

    ps_temp_path_normalize_lint used to be excluded here AND referenced by no
    workflow — its ::warning annotations reached nothing and nothing pinned it.
    It passes on the tree, so the exclusion bought exactly nothing.
    """

    def test_real_tree_green(self):
        repo_root = pathlib.Path(__file__).resolve().parents[2]
        env = dict(os.environ, PS_LINT_ROOT=str(repo_root))
        for script in _ALL_GUARDS:
            proc = subprocess.run(
                [sys.executable, str(_SCRIPTS_DIR / script)],
                capture_output=True,
                text=True,
                env=env,
                timeout=120,
            )
            self.assertEqual(
                proc.returncode, 0, f"{script} failed:\n{proc.stdout}"
            )


class TestGuardsRefuseAnEmptyScan(unittest.TestCase):
    """A guard that read ZERO files must NOT report a pass.

    Every guard did `if not base.exists(): continue`, so a renamed scan
    directory — or a wrong PS_LINT_ROOT — printed `… OK` and exited 0 having
    parsed nothing. That is false confidence, not a green build. The advisory
    lint exits non-zero here too: a zero-file scan is a CONFIGURATION failure,
    not an advisory finding.
    """

    def test_zero_files_scanned_is_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            (pathlib.Path(td) / "somewhere_else").mkdir()
            env = dict(os.environ, PS_LINT_ROOT=td)
            for script in _ALL_GUARDS:
                proc = subprocess.run(
                    [sys.executable, str(_SCRIPTS_DIR / script)],
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=60,
                )
                self.assertEqual(
                    proc.returncode, 1,
                    f"{script} passed an empty scan:\n{proc.stdout}",
                )
                self.assertIn("scanned 0 .ps1 files", proc.stdout)


class TestGuardsHandleShippedFileEncoding(unittest.TestCase):
    """Shipped .ps1 carry a UTF-8 BOM and CRLF (.gitattributes + the
    powershell-encoding CI job). A guard that chokes on either would be silently
    blind to every real file, so pin it on a finding that must still fire."""

    def test_bom_and_crlf_do_not_hide_a_finding(self):
        body = (
            "wsl -d EduBotics -- docker image prune -f\r\n"
            'Write-Host "fertig"\r\n'
        )
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            p = root / "robotis_ai_setup" / "bom.ps1"
            p.parent.mkdir(parents=True)
            p.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
            proc = subprocess.run(
                [sys.executable, str(_SCRIPTS_DIR / "ps_cleanup_exit_guard.py")],
                capture_output=True,
                text=True,
                env=dict(os.environ, PS_LINT_ROOT=str(root)),
                timeout=60,
            )
            self.assertEqual(proc.returncode, 1, proc.stdout)
            self.assertIn("docker image prune", proc.stdout)


if __name__ == "__main__":
    unittest.main()

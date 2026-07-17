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


class TestCleanupExitGuard(unittest.TestCase):
    SCRIPT = "ps_cleanup_exit_guard.py"

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
    """The three failing guards must be green on the actual repo tree."""

    def test_real_tree_green(self):
        repo_root = pathlib.Path(__file__).resolve().parents[2]
        env = dict(os.environ, PS_LINT_ROOT=str(repo_root))
        for script in (
            "ps_readiness_retry_lint.py",
            "ps_temp_cmdlet_lint.py",
            "ps_cleanup_exit_guard.py",
        ):
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


if __name__ == "__main__":
    unittest.main()

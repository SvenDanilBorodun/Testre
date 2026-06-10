"""Self-test for .github/scripts/ps_native_stderr_lint.py.

The real bug it guards against is a Windows-PowerShell-5.1 RUNTIME behavior
(native stderr promoted to a terminating NativeCommandError under EAP=Stop) that
a Linux unittest cannot execute. So this proves the *static guard* itself fires
on the bad pattern and does not false-positive on the safe one — otherwise the
guard could silently rot the way overlay-guard once did.

Cross-platform: pure subprocess + temp files, no PowerShell needed.
"""
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
LINT = REPO / ".github" / "scripts" / "ps_native_stderr_lint.py"

BAD = (
    '$ErrorActionPreference = "Stop"\n'
    '$x = (wsl -d EduBotics -- docker info 2>&1 | Out-String)\n'
    'if ($LASTEXITCODE -eq 0) { "ready" }\n'
)
GOOD_CONTINUE = (
    '$ErrorActionPreference = "Continue"\n'
    'wsl -d EduBotics -- docker info *>$null 2>&1\n'
    'if ($LASTEXITCODE -eq 0) { "ready" }\n'
)
GOOD_NO_NATIVE = (
    '$ErrorActionPreference = "Stop"\n'
    'Move-Item $a $b -Force\n'
)
ALLOWED = (
    '$ErrorActionPreference = "Stop"\n'
    '$x = (wsl -d EduBotics -- docker info 2>&1)  # ps-native-stderr-lint: allow\n'
)


class TestPsNativeStderrLint(unittest.TestCase):
    def _run(self, **scripts) -> int:
        with tempfile.TemporaryDirectory() as d:
            sub = pathlib.Path(d) / "robotis_ai_setup"
            sub.mkdir()
            for name, body in scripts.items():
                (sub / f"{name}.ps1").write_text(body, encoding="utf-8")
            env = dict(os.environ, PS_LINT_ROOT=d)
            return subprocess.run([sys.executable, str(LINT)], env=env,
                                  capture_output=True, text=True).returncode

    def test_guard_exists(self):
        self.assertTrue(LINT.is_file(), f"missing lint script: {LINT}")

    def test_flags_eap_stop_native_stderr(self):
        self.assertEqual(1, self._run(bad=BAD))

    def test_passes_eap_continue(self):
        self.assertEqual(0, self._run(good=GOOD_CONTINUE))

    def test_passes_stop_without_native_calls(self):
        self.assertEqual(0, self._run(good=GOOD_NO_NATIVE))

    def test_allow_comment_suppresses(self):
        self.assertEqual(0, self._run(allowed=ALLOWED))

    def test_flags_bad_even_alongside_good(self):
        self.assertEqual(1, self._run(bad=BAD, good=GOOD_CONTINUE))

    def test_real_repo_scripts_pass(self):
        # The shipped installer scripts must already satisfy the guard.
        rc = subprocess.run([sys.executable, str(LINT)],
                            capture_output=True, text=True).returncode
        self.assertEqual(0, rc, "shipped .ps1 scripts violate ps-native-stderr-lint")


if __name__ == "__main__":
    unittest.main()

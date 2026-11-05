"""CI supply-chain pins and the public release channel (2026-11-05).

Pinned here, across every workflow:

* **Railway CLI.** Installed only by the pinned, SHA-256-checked release-binary
  step, all copies identical; never ``npm install -g @railway/cli``.
  ``RAILWAY_TOKEN`` lives on the steps that run ``railway``, never at workflow
  or job level, so no install step runs with the token in scope (security
  review 2026-10-25, P0-2).
* **Supabase CLI.** ``supabase/setup-cli`` gets one exact version everywhere,
  never ``latest``.
* **PyInstaller.** The installer build pins it exactly.
* **Release channel.** Both asset uploads (the installer and the Pi tarball)
  and W6's ``GIT_REPO`` resolve the target repository from
  ``vars.RELEASES_REPO`` with this repository as the fallback, so the downloads
  can leave this repository before it goes private
  (docs/deploy/PRIVATE-REPOSITORY.md).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WF = REPO / ".github" / "workflows"

RAILWAY_STEP_NAME = "Install Railway CLI (pinned release binary, SHA-256 checked)"
RELEASES_REPO_EXPR = "${{ vars.RELEASES_REPO || github.repository }}"
RELEASES_TOKEN_EXPR = "${{ vars.RELEASES_REPO && secrets.RELEASES_REPO_TOKEN || secrets.GITHUB_TOKEN }}"
SOFTPROPS = "softprops/action-gh-release@"


# A step that RUNS the Railway CLI: `railway <subcommand>` in command position,
# or the teacher-web script executed with bash. A step that only names
# railway-deploy.sh (shellcheck's file list) does not count.
_RAILWAY_CALL = re.compile(r"(^|[\s;&|(])railway\s+[a-z]", re.M)
_RAILWAY_SCRIPT = re.compile(r"\bbash\s+\S*railway-deploy\.sh\b")


def _calls_railway(step: dict) -> bool:
    if step.get("name") == RAILWAY_STEP_NAME:
        return False
    run = step.get("run", "")
    return bool(_RAILWAY_CALL.search(run) or _RAILWAY_SCRIPT.search(run))


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _workflows():
    return sorted(WF.glob("*.yml"))


def _steps(doc: dict):
    for job_id, job in (doc.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            yield job_id, job, step


class RailwayCliPin(unittest.TestCase):
    def test_no_npm_install_of_the_cli_anywhere(self):
        for path in _workflows():
            with self.subTest(workflow=path.name):
                self.assertNotIn("@railway/cli", path.read_text(encoding="utf-8"))

    def test_every_job_that_runs_railway_installs_the_one_pinned_binary(self):
        install_steps = []
        for path in _workflows():
            doc = _load(path)
            for job_id, job in (doc.get("jobs") or {}).items():
                steps = job.get("steps") or []
                runs_railway = any(_calls_railway(s) for s in steps)
                installs = [s for s in steps if s.get("name") == RAILWAY_STEP_NAME]
                if runs_railway:
                    with self.subTest(job=f"{path.name}::{job_id}"):
                        self.assertEqual(len(installs), 1, "a job that runs railway installs it exactly once")
                        first_railway = next(i for i, s in enumerate(steps) if _calls_railway(s))
                        self.assertLess(steps.index(installs[0]), first_railway, "install before first use")
                install_steps.extend((path.name, job_id, s) for s in installs)
        self.assertGreaterEqual(len(install_steps), 4, "the four Railway jobs")
        canon = install_steps[0][2]
        for name, job_id, step in install_steps:
            with self.subTest(job=f"{name}::{job_id}"):
                self.assertEqual(step, canon, "every copy of the install step is identical")
        env = canon["env"]
        self.assertRegex(env["RAILWAY_CLI_VERSION"], r"^\d+\.\d+\.\d+$")
        self.assertRegex(env["RAILWAY_CLI_SHA256"], r"^[0-9a-f]{64}$")
        self.assertIn("sha256sum -c -", canon["run"])
        self.assertNotIn("RAILWAY_TOKEN", canon["run"])
        self.assertNotIn("RAILWAY_TOKEN", env)

    def test_the_token_is_never_workflow_or_job_scoped(self):
        for path in _workflows():
            doc = _load(path)
            with self.subTest(workflow=path.name):
                self.assertNotIn("RAILWAY_TOKEN", (doc.get("env") or {}))
                for job_id, job in (doc.get("jobs") or {}).items():
                    self.assertNotIn("RAILWAY_TOKEN", (job.get("env") or {}), job_id)

    def test_every_step_that_runs_railway_gets_the_token_itself(self):
        for path in _workflows():
            for job_id, _, step in _steps(_load(path)):
                if not _calls_railway(step):
                    continue
                with self.subTest(step=f"{path.name}::{job_id}::{step.get('name')}"):
                    self.assertEqual(
                        (step.get("env") or {}).get("RAILWAY_TOKEN"),
                        "${{ secrets.RAILWAY_TOKEN }}",
                    )


class SupabaseCliPin(unittest.TestCase):
    def test_one_exact_version_everywhere(self):
        versions = []
        for path in _workflows():
            for job_id, _, step in _steps(_load(path)):
                if str(step.get("uses", "")).startswith("supabase/setup-cli@"):
                    versions.append((path.name, job_id, str((step.get("with") or {}).get("version"))))
        self.assertTrue(versions, "supabase/setup-cli is used")
        for name, job_id, version in versions:
            with self.subTest(job=f"{name}::{job_id}"):
                self.assertRegex(version, r"^\d+\.\d+\.\d+$", "an exact version, never latest")
        self.assertEqual(len({v for _, _, v in versions}), 1, "the same version in every job")


class PyInstallerPin(unittest.TestCase):
    def test_the_installer_build_pins_pyinstaller(self):
        text = (WF / "release-installer.yml").read_text(encoding="utf-8")
        installs = re.findall(r"pip install[^\n]*\bpyinstaller\b[^\n]*", text)
        self.assertTrue(installs)
        for line in installs:
            with self.subTest(line=line):
                self.assertRegex(line, r"\bpyinstaller==\d+\.\d+\.\d+\b")
                self.assertRegex(line, r"\bpyinstaller-hooks-contrib==[\d.]+\b")


class ReleaseChannel(unittest.TestCase):
    def _uploads(self):
        found = []
        for path in (WF / "release-installer.yml", WF / "release.yml"):
            for job_id, job, step in _steps(_load(path)):
                if str(step.get("uses", "")).startswith(SOFTPROPS):
                    found.append((path.name, job_id, job, step))
        return found

    def test_both_uploads_use_the_channel_and_its_token(self):
        uploads = self._uploads()
        self.assertEqual(
            sorted((n, j) for n, j, _, _ in uploads),
            [("release-installer.yml", "build-installer"), ("release.yml", "pi-agent-tarball")],
            "exactly two uploads: the installer and the Pi tarball",
        )
        for name, job_id, _, step in uploads:
            with self.subTest(job=f"{name}::{job_id}"):
                w = step.get("with") or {}
                self.assertEqual(w.get("repository"), RELEASES_REPO_EXPR)
                self.assertEqual(w.get("token"), RELEASES_TOKEN_EXPR)

    def test_each_upload_is_preceded_by_the_channel_check(self):
        for name, job_id, job, step in self._uploads():
            steps = job["steps"]
            checks = [
                s for s in steps[: steps.index(step)]
                if (s.get("env") or {}).get("RELEASES_REPO") == "${{ vars.RELEASES_REPO }}"
            ]
            with self.subTest(job=f"{name}::{job_id}"):
                self.assertEqual(len(checks), 1)
                check = checks[0]
                self.assertEqual(check["env"].get("RELEASES_REPO_TOKEN"), "${{ secrets.RELEASES_REPO_TOKEN }}")
                self.assertEqual(check.get("if"), "startsWith(github.ref, 'refs/tags/v')")
                self.assertIn("exit 1", check["run"])

    def test_w6_advertises_the_same_repository(self):
        job = _load(WF / "release.yml")["jobs"]["publish-gui-version"]
        envs = [(s.get("env") or {}) for s in job["steps"]]
        repos = [e["GIT_REPO"] for e in envs if "GIT_REPO" in e]
        self.assertEqual(repos, [RELEASES_REPO_EXPR])

    def test_a_broken_channel_stops_the_release_before_w1(self):
        jobs = _load(WF / "release.yml")["jobs"]
        steps = [
            s for s in jobs["version-preflight"]["steps"]
            if (s.get("env") or {}).get("REPO_PRIVATE") == "${{ github.event.repository.private }}"
        ]
        self.assertEqual(len(steps), 1, "version-preflight checks the channel exactly once")
        step = steps[0]
        self.assertEqual(step.get("if"), "startsWith(github.ref, 'refs/tags/v')")
        self.assertEqual(step["env"].get("RELEASES_REPO"), "${{ vars.RELEASES_REPO }}")
        self.assertEqual(step["env"].get("RELEASES_REPO_TOKEN"), "${{ secrets.RELEASES_REPO_TOKEN }}")
        run = step["run"]
        # private repo + no channel refuses; fails CLOSED on an unknown visibility
        self.assertIn('if [ "${REPO_PRIVATE}" != "false" ]; then', run)
        # the token is proven live against the channel, which must be public
        self.assertIn("https://api.github.com/repos/${RELEASES_REPO}", run)
        self.assertIn('if [ "$private" != "false" ]; then', run)
        self.assertGreaterEqual(run.count("exit 1"), 5)
        # W1 (and so every deploy) waits for the preflight
        self.assertIn("version-preflight", jobs["supabase"]["needs"])


@unittest.skipUnless(shutil.which("bash"), "bash is not installed")
class ReleaseChannelScripts(unittest.TestCase):
    """The channel checks, executed under bash with a stand-in curl."""

    FAKE_CURL = r"""#!/usr/bin/env bash
out=""
while [ $# -gt 0 ]; do
  case "$1" in -o) out="$2"; shift 2 ;; *) shift ;; esac
done
[ -n "$out" ] && printf '{"private": %s}' "${FAKE_PRIVATE:-false}" > "$out"
printf '%s' "${FAKE_CODE:-200}"
"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        curl = self.tmp / "curl"
        curl.write_text(self.FAKE_CURL, encoding="utf-8")
        curl.chmod(0o755)

    def _run(self, script, **env):
        full = {"PATH": f"{self.tmp}:{os.environ.get('PATH', '')}", "RELEASES_REPO": "", "RELEASES_REPO_TOKEN": ""}
        full.update(env)
        r = subprocess.run(["bash", "-c", script], env=full, capture_output=True, text=True, timeout=30)
        return r.returncode, r.stdout + r.stderr

    def test_the_preflight(self):
        jobs = _load(WF / "release.yml")["jobs"]
        (step,) = [s for s in jobs["version-preflight"]["steps"] if "REPO_PRIVATE" in (s.get("env") or {})]
        script = step["run"]
        TOKEN = "SECRET-TOKEN-VALUE-7f3a"
        ok, bad = 0, 1
        cases = [
            ("public repo, no channel", ok, dict(REPO_PRIVATE="false")),
            ("private repo, no channel", bad, dict(REPO_PRIVATE="true")),
            ("unknown visibility, no channel", bad, dict(REPO_PRIVATE="")),
            ("malformed channel", bad, dict(REPO_PRIVATE="true", RELEASES_REPO="not a repo", RELEASES_REPO_TOKEN=TOKEN)),
            ("channel without token", bad, dict(REPO_PRIVATE="true", RELEASES_REPO="o/r")),
            ("expired token", bad, dict(REPO_PRIVATE="true", RELEASES_REPO="o/r", RELEASES_REPO_TOKEN=TOKEN, FAKE_CODE="401")),
            ("private channel", bad, dict(REPO_PRIVATE="true", RELEASES_REPO="o/r", RELEASES_REPO_TOKEN=TOKEN, FAKE_PRIVATE="true")),
            ("public channel, valid token", ok, dict(REPO_PRIVATE="true", RELEASES_REPO="o/r", RELEASES_REPO_TOKEN=TOKEN)),
        ]
        for label, want, env in cases:
            with self.subTest(case=label):
                code, out = self._run(script, **env)
                self.assertEqual(code == 0, want == ok, out)
                self.assertNotIn(TOKEN, out, "a check never prints the token")

    def test_the_per_upload_checks(self):
        for path, job_id in ((WF / "release-installer.yml", "build-installer"), (WF / "release.yml", "pi-agent-tarball")):
            steps = _load(path)["jobs"][job_id]["steps"]
            (step,) = [s for s in steps if (s.get("env") or {}).get("RELEASES_REPO") == "${{ vars.RELEASES_REPO }}"]
            with self.subTest(job=job_id):
                self.assertEqual(self._run(step["run"])[0], 0, "no channel: upload to this repository")
                self.assertNotEqual(self._run(step["run"], RELEASES_REPO="o/r")[0], 0, "channel without token")
                self.assertNotEqual(self._run(step["run"], RELEASES_REPO="bad repo", RELEASES_REPO_TOKEN="t")[0], 0)
                self.assertEqual(self._run(step["run"], RELEASES_REPO="o/r", RELEASES_REPO_TOKEN="t")[0], 0)


if __name__ == "__main__":
    unittest.main()

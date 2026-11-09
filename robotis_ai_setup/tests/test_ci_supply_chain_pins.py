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
* **The cloud API stack and Dependabot (2026-11-09).** Dependabot proposes
  patch releases and security fixes for the internet-facing cloud API's
  libraries: their ``ignore`` entries name only minor updates, never a bare
  name (which would block security updates too). ci.yml installs the same
  versions by hand, and must install exactly what requirements.txt ships, so a
  Dependabot patch PR is red until ci.yml's pins move with it.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WF = REPO / ".github" / "workflows"

RAILWAY_STEP_NAME = "Install Railway CLI (pinned release binary, SHA-256 checked)"
DEPENDABOT = REPO / ".github" / "dependabot.yml"
API_REQUIREMENTS = REPO / "robotis_ai_setup" / "cloud_training_api" / "requirements.txt"
# The internet-facing cloud API's libraries (owner decision 2026-11-09: patch
# releases and security fixes, never a minor or major version update). aiohttp
# is in no manifest Dependabot reads today; its entry is there for when it is.
API_STACK = ("fastapi", "httpx", "uvicorn", "cryptography", "boto3", "botocore", "huggingface-hub", "aiohttp",
             # 2026-11-10: the API verifies access tokens locally (services/jwt_auth.py).
             "pyjwt")
# The ci.yml jobs that pip-install cloud API libraries by version.
API_CI_JOBS = ("python-tests", "modal-import-validate")
_PIN = re.compile(r"(?<![\w.-])'?([A-Za-z][A-Za-z0-9_.-]*)(?:\[[^\]]*\])?==([0-9][0-9A-Za-z.+!-]*)")
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


def _norm(name: str) -> str:
    """PEP 503: huggingface_hub and huggingface-hub are one project."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_pins(path: Path) -> dict[str, str]:
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        match = _PIN.fullmatch(line)
        if match:
            pins[_norm(match[1])] = match[2]
    return pins


def _ci_pins(job_id: str) -> dict[str, set[str]]:
    job = _load(WF / "ci.yml")["jobs"][job_id]
    runs = "\n".join(step.get("run", "") for step in job.get("steps") or [])
    pins: dict[str, set[str]] = {}
    for name, version in _PIN.findall(runs):
        pins.setdefault(_norm(name), set()).add(version)
    return pins


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


class CloudApiPinsFollowRequirements(unittest.TestCase):
    def test_the_api_stack_gets_patch_and_security_updates(self):
        pip = next(u for u in _load(DEPENDABOT)["updates"] if u["package-ecosystem"] == "pip")
        entries: dict[str, list[dict]] = {}
        for entry in pip.get("ignore") or []:
            entries.setdefault(_norm(entry["dependency-name"]), []).append(entry)
        self.assertIn({"dependency-name": "*", "update-types": ["version-update:semver-major"]}, pip["ignore"])
        for name in API_STACK:
            with self.subTest(dependency=name):
                self.assertTrue(entries.get(name), "listed, so a minor update is never proposed")
                for entry in entries[name]:
                    # A bare name (or a `versions` range) also blocks security updates.
                    self.assertEqual(set(entry), {"dependency-name", "update-types"})
                    self.assertEqual(entry["update-types"], ["version-update:semver-minor"])

    def test_ci_installs_exactly_the_api_versions_dependabot_may_move(self):
        shipped = _requirement_pins(API_REQUIREMENTS)
        moved = [n for n in API_STACK if n in shipped]
        self.assertGreaterEqual(len(moved), 8, "requirements.txt pins the stack with ==")
        jobs = {job_id: _ci_pins(job_id) for job_id in API_CI_JOBS}
        for name in moved:
            with self.subTest(dependency=name):
                self.assertIn(name, jobs["python-tests"], "the test suites run against the shipped version")
                for job_id, pins in jobs.items():
                    if name in pins:
                        self.assertEqual(pins[name], {shipped[name]},
                                         f"ci.yml::{job_id} installs what requirements.txt ships")


SERVER_DOCKERFILE = REPO / "robotis_ai_setup" / "docker" / "physical_ai_server" / "Dockerfile"
# ci.yml::python-tests pins its server-side test deps to the server image's
# versions (2026-11-10: unpinned, numpy and opencv had floated to 2.5.4 and
# 5.0.0.93). CI package -> the server Dockerfile's package of the same release.
SERVER_LOCKSTEP = {"numpy": "numpy", "opencv-contrib-python-headless": "opencv-contrib-python"}


def _python_tests_installs() -> list[list[str]]:
    """The package arguments of every `pip install` in ci.yml::python-tests."""
    job = _load(WF / "ci.yml")["jobs"]["python-tests"]
    runs = "\n".join(step.get("run", "") for step in job.get("steps") or [])
    installs = []
    for line in runs.replace("\\\n", " ").splitlines():
        line = line.split("#", 1)[0].strip()
        if re.search(r"\bpip install\b", line):
            args = shlex.split(line.split("pip install", 1)[1])
            installs.append([a for a in args if not a.startswith("-")])
    return installs


class PythonTestsPinsEveryPackage(unittest.TestCase):
    def test_every_package_the_job_installs_is_pinned_exactly(self):
        installs = _python_tests_installs()
        self.assertGreaterEqual(len(installs), 2, "pip itself, then the test stack")
        packages = [a for args in installs for a in args]
        pinned = {_norm(m[1]) for m in map(_PIN.fullmatch, packages) if m}
        self.assertTrue({"pip", "pytest", "pyyaml", "numpy", "opencv-contrib-python-headless"} <= pinned, pinned)
        for arg in packages:
            with self.subTest(package=arg):
                self.assertIsNotNone(_PIN.fullmatch(arg), "an exact name==version, never a bare name or a range")

    def test_the_server_side_test_deps_equal_the_server_image(self):
        server = {}
        for name, version in _PIN.findall(SERVER_DOCKERFILE.read_text(encoding="utf-8")):
            server.setdefault(_norm(name), set()).add(version)
        ci = _ci_pins("python-tests")
        for ci_name, image_name in SERVER_LOCKSTEP.items():
            with self.subTest(package=ci_name):
                self.assertEqual(len(server.get(image_name, set())), 1, f"the image pins {image_name} once")
                self.assertEqual(ci.get(ci_name), server[image_name])

    def test_dependabot_never_moves_the_lockstep_pins_alone(self):
        pip = next(u for u in _load(DEPENDABOT)["updates"] if u["package-ecosystem"] == "pip")
        bare = {_norm(e["dependency-name"]) for e in pip.get("ignore") or [] if set(e) == {"dependency-name"}}
        for name in (*SERVER_LOCKSTEP, "pyarrow", "av"):
            with self.subTest(dependency=name):
                self.assertIn(name, bare)


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
        # and that it can WRITE there: a draft release created and deleted
        self.assertIn('"draft\\":true', run)
        self.assertIn('if [ "$code" != "201" ]; then', run)
        self.assertGreaterEqual(run.count("exit 1"), 5)
        # W1 (and so every deploy) waits for the preflight
        self.assertIn("version-preflight", jobs["supabase"]["needs"])


@unittest.skipUnless(shutil.which("bash"), "bash is not installed")
class ReleaseChannelScripts(unittest.TestCase):
    """The channel checks, executed under bash with a stand-in curl."""

    FAKE_CURL = r"""#!/usr/bin/env bash
out=""; method=GET
while [ $# -gt 0 ]; do
  case "$1" in -o) out="$2"; shift 2 ;; -X) method="$2"; shift 2 ;; *) shift ;; esac
done
case "$method" in
  POST)   [ -n "$out" ] && printf '{"id": 4242}' > "$out"; printf '%s' "${FAKE_POST_CODE:-201}" ;;
  DELETE) printf '%s' "${FAKE_DELETE_CODE:-204}" ;;
  *)      [ -n "$out" ] && printf '{"private": %s}' "${FAKE_PRIVATE:-false}" > "$out"; printf '%s' "${FAKE_CODE:-200}" ;;
esac
"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        curl = self.tmp / "curl"
        curl.write_text(self.FAKE_CURL, encoding="utf-8")
        curl.chmod(0o755)

    def _run(self, script, **env):
        full = {"PATH": f"{self.tmp}:{os.environ.get('PATH', '')}", "RELEASES_REPO": "", "RELEASES_REPO_TOKEN": "",
                "GITHUB_RUN_ID": "123"}
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
            ("read-only token", bad, dict(REPO_PRIVATE="true", RELEASES_REPO="o/r", RELEASES_REPO_TOKEN=TOKEN, FAKE_POST_CODE="403")),
            ("public channel, token can write", ok, dict(REPO_PRIVATE="true", RELEASES_REPO="o/r", RELEASES_REPO_TOKEN=TOKEN)),
            ("probe draft not deleted: warn, go on", ok, dict(REPO_PRIVATE="true", RELEASES_REPO="o/r", RELEASES_REPO_TOKEN=TOKEN, FAKE_DELETE_CODE="500")),
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

# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""W6, run by hand (launch item E6, 2026-11-15): its steps, executed.

publish-gui-version.yml advertises a release to every Windows PC and Orange
Pi. Each step's own `run:` script is executed here under bash, with a stand-in
`curl` (the GitHub API, the release downloads, /version and the two registries)
and a stand-in `railway` that only records its arguments, so what is proven is
the shipped shell, not a copy of it:

* the check step refuses, before anything changes, a run outside `main`, a
  malformed version or tested hash, a private channel, a missing, draft or
  incomplete release, an asset that does not download anonymously, a download
  whose SHA-256 differs from GitHub's recorded digest or from the file the
  owner tested, and a Pi tarball that carries another version;
* the -opi gate withholds only the Pi hash, never the run;
* the Railway step sets the five variables in their order, GUI_VERSION last
  and the only one that redeploys, and sets nothing from unchecked values;
* the verify step accepts only the values that were set.

`tests/test_deploy_approval_gate.py::W6IsManual` holds the workflow's shape
(trigger, job, step order); this file holds its behaviour.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
W6 = REPO / ".github" / "workflows" / "publish-gui-version.yml"
CHANNEL = "Owner/edubotics-releases"
VERSION = "2.99.0"
TOKEN = "ghs-SECRET-TOKEN-VALUE-1b2c"
RAILWAY_TOKEN = "RAILWAY-SECRET-9d8e"

FAKE_CURL = r'''#!/usr/bin/env python3
import json, os, re, shutil, sys
cfg = json.load(open(os.environ["FAKE_CONFIG"], encoding="utf-8"))
args = sys.argv[1:]
out = fmt = None
fail = False
headers = []
url = None
i = 0
while i < len(args):
    a = args[i]
    if a in ("-o", "-w", "-H", "--max-time", "--retry", "--retry-max-time"):
        v = args[i + 1]
        if a == "-o": out = v
        elif a == "-w": fmt = v
        elif a == "-H": headers.append(v)
        i += 2
        continue
    if a.startswith("-"):
        if not a.startswith("--") and "f" in a:
            fail = True
        i += 1
        continue
    url = a
    i += 1
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"url": url, "auth": any(h.lower().startswith("authorization:") for h in headers),
                          "headers": headers}) + "\n")

def reply(code, body=None, src=None):
    if src is not None and out:
        shutil.copyfile(src, out)
    elif body is not None:
        data = body if isinstance(body, str) else json.dumps(body)
        if out and out != "/dev/null":
            open(out, "w", encoding="utf-8").write(data)
        elif not out:
            sys.stdout.write(data)
    if fmt:
        sys.stdout.write(fmt.replace("%{http_code}", str(code)))
    sys.exit(22 if (fail and code >= 400) else 0)

def unreachable():
    sys.stderr.write("curl: (6) Could not resolve host\n")
    if fmt:
        sys.stdout.write("000")
    sys.exit(6)

m = re.fullmatch(r"https://api\.github\.com/repos/([^/]+/[^/]+)", url)
if m:
    r = cfg["repo"]
    reply(r["code"], r["body"])
m = re.fullmatch(r"https://api\.github\.com/repos/([^/]+/[^/]+)/releases/tags/(.+)", url)
if m:
    r = cfg["release"]
    reply(r["code"] if m.group(2) == cfg["tag"] else 404, r["body"])
m = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/releases/download/([^/]+)/(.+)", url)
if m:
    src = cfg["assets"].get(m.group(3))
    if m.group(1) != cfg["channel"] or m.group(2) != cfg["tag"] or not src:
        reply(404, "Not Found")
    reply(200, src=src)
if url.endswith("/version"):
    v = cfg.get("version_now")
    if v is None:
        unreachable()
    reply(200, v)
m = re.match(r"https://(ghcr\.io|auth\.docker\.io)/token\?.*repository:[a-z]+/([a-z0-9-]+):pull", url)
if m:
    reg = "ghcr" if m.group(1) == "ghcr.io" else "hub"
    if f"{reg}:{m.group(2)}" in cfg.get("token_fail", []):
        unreachable()
    reply(200, {"token": "t"})
m = re.match(r"https://(ghcr\.io|registry-1\.docker\.io)/v2/[a-z]+/([a-z0-9-]+)/manifests/", url)
if m:
    reg = "ghcr" if m.group(1) == "ghcr.io" else "hub"
    reply(cfg.get("registry", {}).get(f"{reg}:{m.group(2)}", 200), "")
sys.stderr.write(f"fake curl: no route for {url}\n")
sys.exit(7)
'''

FAKE_RAILWAY = """#!/usr/bin/env bash
if [ "$1" = "--version" ]; then echo "railway 5.71.0"; exit 0; fi
printf '%s\\n' "$*" >> "$RAILWAY_LOG"
[ -n "$RAILWAY_TOKEN" ] || { echo "no token" >&2; exit 1; }
"""

OPI = ("physical-ai-server-opi", "physical-ai-manager-opi", "open-manipulator-opi", "code-runner-opi")


def _steps():
    job = yaml.safe_load(W6.read_text(encoding="utf-8"))["jobs"]["publish-gui-version"]
    return {s["name"]: s for s in job["steps"]}


def _tarball(version: str, image_tag: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in (("pi_agent/VERSION", version + "\n"),
                           ("pi_agent/docker/versions.env", f"# generated\nIMAGE_TAG={image_tag}\n"),
                           ("pi_agent/agent.py", "print('hi')\n")):
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@unittest.skipUnless(shutil.which("bash") and shutil.which("tar"), "bash and tar are needed")
class W6Steps(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, text in (("curl", FAKE_CURL), ("railway", FAKE_RAILWAY)):
            path = self.bin / name
            path.write_text(text, encoding="utf-8")
            path.chmod(0o755)
        if not shutil.which("sha256sum"):  # macOS: the runner has coreutils
            shim = self.bin / "sha256sum"
            shim.write_text('#!/bin/sh\nexec shasum -a 256 "$@"\n', encoding="utf-8")
            shim.chmod(0o755)
        self.steps = _steps()
        self.exe = self.tmp / "EduBotics_Setup.exe"
        self.exe.write_bytes(b"MZ" + os.urandom(4096))
        self.tgz = self.tmp / "edubotics-pi-agent.tar.gz"
        self.tgz.write_bytes(_tarball(VERSION, VERSION))

    # ── the harness ────────────────────────────────────────────────────────
    @staticmethod
    def _sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _release(self, **over):
        assets = [
            {"name": "EduBotics_Setup.exe", "state": "uploaded", "digest": "sha256:" + self._sha(self.exe)},
            {"name": "edubotics-pi-agent.tar.gz", "state": "uploaded", "digest": "sha256:" + self._sha(self.tgz)},
        ]
        body = {"tag_name": "v" + VERSION, "draft": False, "assets": assets}
        body.update(over)
        return body

    def _config(self, **over):
        cfg = {
            "channel": CHANNEL,
            "tag": "v" + VERSION,
            "repo": {"code": 200, "body": {"private": False}},
            "release": {"code": 200, "body": self._release()},
            "assets": {"EduBotics_Setup.exe": str(self.exe), "edubotics-pi-agent.tar.gz": str(self.tgz)},
            "version_now": {"version": "2.98.0"},
            "registry": {},
            "token_fail": [],
        }
        cfg.update(over)
        return cfg

    def _run(self, step: str, env: dict, cfg: dict | None = None):
        cfg_path = self.tmp / "cfg.json"
        cfg_path.write_text(json.dumps(cfg or self._config()), encoding="utf-8")
        run_tmp = Path(tempfile.mkdtemp(dir=self.tmp))
        out, summary, log, rlog = (run_tmp / n for n in ("out", "summary", "curl.log", "railway.log"))
        for f in (out, summary, log, rlog):
            f.write_text("", encoding="utf-8")
        full = {
            "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
            "HOME": str(self.tmp),
            "RUNNER_TEMP": str(run_tmp),
            "GITHUB_OUTPUT": str(out),
            "GITHUB_STEP_SUMMARY": str(summary),
            "GITHUB_REF": "refs/heads/main",
            "FAKE_CONFIG": str(cfg_path),
            "FAKE_LOG": str(log),
            "RAILWAY_LOG": str(rlog),
        }
        full.update(env)
        r = subprocess.run(["bash", "-c", self.steps[step]["run"]], env=full,
                           capture_output=True, text=True, timeout=60)
        outputs = dict(line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines() if "=" in line)
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        return r.returncode, r.stdout + r.stderr, outputs, calls, summary.read_text(encoding="utf-8"), \
            rlog.read_text(encoding="utf-8").splitlines()

    CHECK = "Check the release in the channel and hash its two assets"

    def _check(self, cfg=None, **env):
        base = {"INPUT_VERSION": VERSION, "EXPECTED_INSTALLER_SHA256": "", "EXPECTED_PI_AGENT_SHA256": "",
                "GIT_REPO": CHANNEL, "GH_API_TOKEN": TOKEN,
                "VERSION_URL": "https://example.test/version"}
        base.update(env)
        return self._run(self.CHECK, base, cfg)

    # ── the check step ─────────────────────────────────────────────────────
    def test_a_good_release_is_checked_and_handed_on(self):
        code, log, outputs, calls, summary, _ = self._check()
        self.assertEqual(code, 0, log)
        self.assertEqual(outputs, {
            "version": VERSION, "tag": "v" + VERSION, "git_repo": CHANNEL,
            "download_url": f"https://github.com/{CHANNEL}/releases/download/v{VERSION}/EduBotics_Setup.exe",
            "installer_sha256": self._sha(self.exe), "pi_agent_sha256": self._sha(self.tgz),
        })
        self.assertNotIn(TOKEN, log, "the API token is never printed")
        self.assertIn("nothing changed yet", summary)
        downloads = [c for c in calls if "/releases/download/" in c["url"]]
        self.assertEqual(len(downloads), 2)
        self.assertFalse(any(c["auth"] for c in downloads), "the fleet downloads anonymously, so does W6")
        api = [c for c in calls if c["url"].startswith("https://api.github.com/")]
        self.assertTrue(api and all(c["auth"] for c in api))

    def test_blanks_and_a_leading_v_are_forgiven(self):
        code, log, outputs, *_ = self._check(INPUT_VERSION=f"  v{VERSION}\n")
        self.assertEqual(code, 0, log)
        self.assertEqual(outputs["version"], VERSION)

    def test_the_tested_hashes_are_compared_when_given(self):
        good = dict(EXPECTED_INSTALLER_SHA256=self._sha(self.exe).upper(),
                    EXPECTED_PI_AGENT_SHA256=" " + self._sha(self.tgz) + " ")
        code, log, _, _, summary, _ = self._check(**good)
        self.assertEqual(code, 0, log)
        self.assertIn("the file you tested", summary)
        for field in ("EXPECTED_INSTALLER_SHA256", "EXPECTED_PI_AGENT_SHA256"):
            with self.subTest(other_bytes=field):
                code, log, outputs, *_ = self._check(**{**good, field: "ab" * 32})
                self.assertNotEqual(code, 0)
                self.assertIn("is not the file you tested", log)
                self.assertEqual(outputs, {})
            with self.subTest(malformed=field):
                code, log, outputs, *_ = self._check(**{**good, field: "not-a-hash"})
                self.assertNotEqual(code, 0)
                self.assertEqual(outputs, {})

    def test_every_refusal_changes_nothing(self):
        other = self.tmp / "other.exe"
        other.write_bytes(b"MZ-other")
        wrong_version = self.tmp / "wrong-version.tgz"
        wrong_version.write_bytes(_tarball("2.98.0", VERSION))
        wrong_tag = self.tmp / "wrong-tag.tgz"
        wrong_tag.write_bytes(_tarball(VERSION, "latest"))
        rel = self._release()
        cases = {
            "not from main": (None, dict(GITHUB_REF="refs/heads/feature/x")),
            "version without patch": (None, dict(INPUT_VERSION="2.99")),
            "a second line in the version": (None, dict(INPUT_VERSION=f"{VERSION}\nx")),
            "a malformed channel": (None, dict(GIT_REPO="not a repo")),
            "the channel answers 404": (self._config(repo={"code": 404, "body": {}}), {}),
            "a private channel": (self._config(repo={"code": 200, "body": {"private": True}}), {}),
            "no such release": (self._config(release={"code": 404, "body": {}}), {}),
            "a draft": (self._config(release={"code": 200, "body": {**rel, "draft": True}}), {}),
            "no Pi tarball": (self._config(release={"code": 200, "body": {**rel, "assets": rel["assets"][:1]}}), {}),
            "an upload still running": (self._config(release={"code": 200, "body": {**rel, "assets": [
                {**rel["assets"][0], "state": "starter"}, rel["assets"][1]]}}), {}),
            "no recorded digest": (self._config(release={"code": 200, "body": {**rel, "assets": [
                {**rel["assets"][0], "digest": None}, rel["assets"][1]]}}), {}),
            "the .exe does not download": (self._config(assets={"edubotics-pi-agent.tar.gz": str(self.tgz)}), {}),
            "other bytes than GitHub recorded": (self._config(assets={
                "EduBotics_Setup.exe": str(other), "edubotics-pi-agent.tar.gz": str(self.tgz)}), {}),
            "the tarball carries another version": (self._config(
                release={"code": 200, "body": {**rel, "assets": [rel["assets"][0], {
                    **rel["assets"][1], "digest": "sha256:" + self._sha(wrong_version)}]}},
                assets={"EduBotics_Setup.exe": str(self.exe), "edubotics-pi-agent.tar.gz": str(wrong_version)}), {}),
            "the tarball pins another IMAGE_TAG": (self._config(
                release={"code": 200, "body": {**rel, "assets": [rel["assets"][0], {
                    **rel["assets"][1], "digest": "sha256:" + self._sha(wrong_tag)}]}},
                assets={"EduBotics_Setup.exe": str(self.exe), "edubotics-pi-agent.tar.gz": str(wrong_tag)}), {}),
        }
        for label, (cfg, env) in cases.items():
            with self.subTest(label):
                code, log, outputs, _, summary, rlog = self._check(cfg, **env)
                self.assertNotEqual(code, 0, log)
                self.assertIn("::error::", log)
                self.assertEqual(outputs, {}, "a refused run hands nothing to the Railway step")
                self.assertEqual(summary, "")
                self.assertEqual(rlog, [])
                self.assertNotIn(TOKEN, log)

    def test_what_is_advertised_now_is_logged_and_never_stops_the_run(self):
        code, log, *_ = self._check(self._config(version_now={"version": "2.100.0", "installer_sha256": "ab" * 32}))
        self.assertEqual(code, 0, log)
        self.assertIn("is OLDER than what /version advertises now", log)
        self.assertIn("now: installer_sha256=" + "ab" * 32, log)
        code, log, *_ = self._check(self._config(version_now={"version": VERSION}))
        self.assertEqual(code, 0, log)
        self.assertIn("already advertises", log)
        code, log, *_ = self._check(self._config(version_now=None))
        self.assertEqual(code, 0, log)
        self.assertIn("::warning::Could not read", log)

    # ── the -opi gate ──────────────────────────────────────────────────────
    GATE = "Gate the Pi update on the four -opi images"

    def _gate(self, **cfg):
        return self._run(self.GATE, {"VERSION": VERSION, "PI_SHA": "cd" * 32}, self._config(**cfg))

    def test_the_gate_withholds_only_the_pi_hash(self):
        cases = {
            "all four pullable": ({}, "cd" * 32, None),
            "GHCR missing, Hub twin pullable": ({"registry": {f"ghcr:{OPI[1]}": 404}}, "cd" * 32, "Docker Hub twin"),
            "missing on both": ({"registry": {f"ghcr:{OPI[3]}": 404, f"hub:{OPI[3]}": 404}}, "",
                                "pullable on NEITHER"),
            "private on GHCR, Hub unreachable": ({"registry": {f"ghcr:{OPI[0]}": 401}, "token_fail": [f"hub:{OPI[0]}"]},
                                                 "", "could not VERIFY"),
        }
        for label, (cfg, want, warning) in cases.items():
            with self.subTest(label):
                code, log, outputs, _, summary, _ = self._gate(**cfg)
                self.assertEqual(code, 0, log)
                self.assertEqual(outputs, {"pi_agent_sha256": want})
                self.assertIn("withheld" if not want else "advertised", summary)
                if warning:
                    self.assertIn(warning, log)

    # ── the Railway step ───────────────────────────────────────────────────
    SET = "Set GUI auto-update env vars on the cloud-api service"

    def _set(self, **env):
        base = {"GIT_REPO": CHANNEL, "VERSION": VERSION, "TAG": "v" + VERSION, "SHA": "ab" * 32,
                "PI_SHA": "cd" * 32, "RAILWAY_TOKEN": RAILWAY_TOKEN}
        base.update(env)
        return self._run(self.SET, base)

    def test_the_five_variables_in_their_order_one_redeploy(self):
        code, log, _, _, _, calls = self._set()
        self.assertEqual(code, 0, log)
        url = f"https://github.com/{CHANNEL}/releases/download/v{VERSION}/EduBotics_Setup.exe"
        tail = "--service scintillating-empathy --environment production"
        self.assertEqual(calls, [
            f"variable set GUI_RELEASE_REPO={CHANNEL} {tail} --skip-deploys",
            f"variable set GUI_DOWNLOAD_URL={url} {tail} --skip-deploys",
            f"variable set GUI_INSTALLER_SHA256={'ab' * 32} {tail} --skip-deploys",
            f"variable set PI_AGENT_SHA256={'cd' * 32} {tail} --skip-deploys",
            f"variable set GUI_VERSION={VERSION} {tail}",
        ])
        self.assertNotIn(RAILWAY_TOKEN, log)

    def test_a_withheld_pi_update_sets_the_hash_empty(self):
        code, log, _, _, _, calls = self._set(PI_SHA="")
        self.assertEqual(code, 0, log)
        self.assertIn("variable set PI_AGENT_SHA256= --service", calls[3])

    def test_unchecked_values_set_nothing(self):
        for label, env in {
            "no version": dict(VERSION=""),
            "tag and version differ": dict(TAG="v2.98.0"),
            "no installer hash": dict(SHA=""),
            "a malformed Pi hash": dict(PI_SHA="xyz"),
            "a malformed channel": dict(GIT_REPO="a b"),
        }.items():
            with self.subTest(label):
                code, log, _, _, _, calls = self._set(**env)
                self.assertNotEqual(code, 0, log)
                self.assertEqual(calls, [])

    # ── the verify step ────────────────────────────────────────────────────
    VERIFY = "Verify /version advertises the new release"

    def _verify(self, served: dict, pi=("cd" * 32)):
        url = f"https://github.com/{CHANNEL}/releases/download/v{VERSION}/EduBotics_Setup.exe"
        env = {"EXPECTED": VERSION, "DOWNLOAD_URL": url, "SHA": "ab" * 32, "PI_SHA": pi,
               "VERSION_URL": "https://example.test/version"}
        body = {"version": VERSION, "download_url": url, "installer_sha256": "ab" * 32,
                "pi_agent_download_url": "https://github.com/x/y/releases/download/v1/p.tgz",
                "pi_agent_sha256": "cd" * 32}
        body.update(served)
        return self._run(self.VERIFY, env, self._config(version_now=body))

    def test_verify_accepts_only_what_was_set(self):
        code, log, _, _, summary, _ = self._verify({})
        self.assertEqual(code, 0, log)
        self.assertIn("Advertised", summary)
        code, log, *_ = self._verify({"pi_agent_sha256": None}, pi="")
        self.assertEqual(code, 0, log)
        for label, served in {
            "another installer hash": {"installer_sha256": "ef" * 32},
            "another download URL": {"download_url": "https://github.com/x/y/releases/download/v1/E.exe"},
            "no Pi URL": {"pi_agent_download_url": None},
            "another Pi hash": {"pi_agent_sha256": "ef" * 32},
        }.items():
            with self.subTest(label):
                code, log, *_ = self._verify(served)
                self.assertNotEqual(code, 0, log)
                self.assertIn("::error::", log)


if __name__ == "__main__":
    unittest.main()

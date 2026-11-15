# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""W2 and W3 refuse to roll production back (2026-11-15).

The two deploy workflows run in two concurrency lanes (a push to main, a
tagged release), so a release whose deploy waited for its approval while a
later merge went live deployed the OLDER tag commit over it, and its health
gate still passed. `.github/scripts/refuse_rollback.sh` now decides, in a real
git history built here, and both deploy jobs run it after the approval and
before `railway up`.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / ".github" / "scripts" / "refuse_rollback.sh"
WORKFLOWS = {
    "railway-deploy-cloud-api.yml": "/health",
    "railway-deploy-teacher-web.yml": "/version.json",
}


def _git(cwd, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "needs git and bash")
class TheDecision(unittest.TestCase):
    """main: A - B - C; a branch from A: D."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="refuse-rollback-")
        _git(cls.dir, "init", "-q", "-b", "main")
        cls.sha = {}
        for name in ("A", "B", "C"):
            _git(cls.dir, "commit", "-q", "--allow-empty", "-m", name)
            cls.sha[name] = _git(cls.dir, "rev-parse", "HEAD")
        _git(cls.dir, "checkout", "-q", "-b", "side", cls.sha["A"])
        _git(cls.dir, "commit", "-q", "--allow-empty", "-m", "D")
        cls.sha["D"] = _git(cls.dir, "rev-parse", "HEAD")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def run_it(self, served, deploy, allow="false"):
        proc = subprocess.run(["bash", str(SCRIPT), served, deploy, allow], cwd=self.dir,
                              capture_output=True, text=True)
        return proc.returncode, proc.stdout + proc.stderr

    def test_forward_and_the_same_commit_deploy(self):
        self.assertEqual(self.run_it(self.sha["A"], self.sha["C"])[0], 0)
        self.assertEqual(self.run_it(self.sha["B"], self.sha["C"])[0], 0)
        self.assertEqual(self.run_it(self.sha["C"], self.sha["C"])[0], 0)

    def test_an_older_commit_is_refused(self):
        code, out = self.run_it(self.sha["C"], self.sha["B"])
        self.assertEqual(code, 1)
        self.assertIn("::error::", out)
        self.assertIn("allow_rollback=true", out)

    def test_a_commit_that_does_not_contain_production_is_refused(self):
        self.assertEqual(self.run_it(self.sha["D"], self.sha["C"])[0], 1)
        self.assertEqual(self.run_it(self.sha["C"], self.sha["D"])[0], 1)
        # a commit this checkout does not know at all
        self.assertEqual(self.run_it("f" * 40, self.sha["C"])[0], 1)

    def test_a_deliberate_rollback_passes_with_a_warning(self):
        code, out = self.run_it(self.sha["C"], self.sha["A"], "true")
        self.assertEqual(code, 0)
        self.assertIn("::warning::", out)

    def test_an_unreadable_production_commit_is_not_judged(self):
        for served in ("", "dev", self.sha["C"][:12], "G" * 40):
            with self.subTest(served=served):
                code, out = self.run_it(served, self.sha["A"])
                self.assertEqual(code, 0)
                self.assertIn("::warning::", out)


class BothDeployJobsRunIt(unittest.TestCase):
    def test_after_a_full_history_checkout_and_before_railway(self):
        for name, path in WORKFLOWS.items():
            with self.subTest(workflow=name):
                wf = yaml.safe_load((REPO / ".github" / "workflows" / name).read_text(encoding="utf-8"))
                steps = wf["jobs"]["deploy"]["steps"]
                checkout = steps[0]
                self.assertTrue(checkout["uses"].startswith("actions/checkout@"))
                self.assertEqual(checkout["with"]["fetch-depth"], 0)
                self.assertIs(checkout["with"]["persist-credentials"], False)
                guard = [i for i, s in enumerate(steps) if "refuse_rollback.sh" in s.get("run", "")]
                self.assertEqual(len(guard), 1)
                first_railway = min(i for i, s in enumerate(steps)
                                    if "railway " in s.get("run", "") or "railway-deploy.sh" in s.get("run", ""))
                self.assertLess(guard[0], first_railway)
                env = steps[guard[0]]["env"]
                self.assertTrue(env["SERVED_URL"].endswith(path))
                self.assertEqual(env["DEPLOY_SHA"], "${{ github.sha }}")
                self.assertEqual(env["ALLOW_ROLLBACK"], "${{ inputs.allow_rollback }}")
                self.assertNotIn("${{", steps[guard[0]]["run"], "values reach the script through env only")
                # the override exists only for a hand dispatch, off by default
                dispatch = wf[True]["workflow_dispatch"]["inputs"]["allow_rollback"]
                self.assertEqual((dispatch["type"], dispatch["default"]), ("boolean", False))
                self.assertNotIn("inputs", wf[True].get("workflow_call") or {})


if __name__ == "__main__":
    unittest.main()

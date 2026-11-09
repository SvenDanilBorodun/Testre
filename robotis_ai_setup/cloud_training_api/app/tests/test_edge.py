"""Runs app/tests/edge_cases.py (the API's edge, 2026-11-10) in a child
interpreter, where ``fastapi`` is the real package and not one of the stubs
other suites of this package install during discovery."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest

API = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


class EdgeInAFreshInterpreter(unittest.TestCase):
    def test_edge_cases(self):
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "app.tests.edge_cases", "-v"],
            cwd=API, env=env, capture_output=True, text=True, timeout=300,
        )
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, out[-6000:])
        self.assertRegex(out, r"Ran \d+ tests")
        self.assertNotIn("skipped", out.splitlines()[-1])


if __name__ == "__main__":
    unittest.main()

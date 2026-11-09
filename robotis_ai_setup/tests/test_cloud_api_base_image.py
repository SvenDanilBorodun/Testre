"""The cloud API's production image runs the Python version CI tests it on (2026-11-10).

Until then the Railway image was `python:3.11-slim`, a floating tag, while
ci.yml::python-tests ran the API's suite on 3.12 and W2's import probe on 3.11.
Pinned here:

* cloud_training_api/Dockerfile builds on `python:3.12.<patch>-slim@sha256:<64 hex>`:
  a digest, never a floating tag, and exactly one FROM.
* W2's probe job (railway-deploy-cloud-api.yml::schema-probe) and
  ci.yml::python-tests set up the image's minor version.

Deliberately stdlib + PyYAML (ci.yml's python-tests installs it).
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "robotis_ai_setup" / "cloud_training_api" / "Dockerfile"
WF = REPO / ".github" / "workflows"
FROM = re.compile(r"^FROM\s+(\S+)", re.M)
PINNED = re.compile(r"^python:(3\.(\d+))\.(\d+)-slim@sha256:[0-9a-f]{64}$")


def _setup_python(workflow: str, job: str) -> str:
    steps = yaml.safe_load((WF / workflow).read_text(encoding="utf-8"))["jobs"][job]["steps"]
    versions = [str((s.get("with") or {}).get("python-version")) for s in steps
                if str(s.get("uses", "")).startswith("actions/setup-python@")]
    assert len(versions) == 1, (workflow, job, versions)
    return versions[0]


class CloudApiBaseImage(unittest.TestCase):
    def test_one_digest_pinned_python_312_base(self):
        froms = FROM.findall(DOCKERFILE.read_text(encoding="utf-8"))
        self.assertEqual(len(froms), 1, froms)
        match = PINNED.fullmatch(froms[0])
        self.assertIsNotNone(match, f"not a digest-pinned python:3.x.y-slim: {froms[0]}")
        self.assertEqual(match.group(1), "3.12")

    def test_w2_and_the_test_job_run_the_images_minor_version(self):
        minor = PINNED.fullmatch(FROM.findall(DOCKERFILE.read_text(encoding="utf-8"))[0]).group(1)
        self.assertEqual(_setup_python("railway-deploy-cloud-api.yml", "schema-probe"), minor)
        self.assertEqual(_setup_python("ci.yml", "python-tests"), minor)


if __name__ == "__main__":
    unittest.main()

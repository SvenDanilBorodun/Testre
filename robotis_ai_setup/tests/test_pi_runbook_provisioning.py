# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""The Pi runbook's provisioning block fails closed (2026-11-09).

``docs/ORANGE_PI_TEACHER_RUNBOOK.md`` „Schritt 2" is pasted by a teacher as ONE
block into a root-capable shell. It must stop before anything is unpacked or
run as root when the cloud answers nothing usable, a field is empty, the
download fails or the checksum does not match (a TLS-inspecting middlebox is
the named case). The block's previous form ran ``sudo ./pi_agent/setup.sh``
after ``sha256sum`` printed FAILED, because a pasted block has no errexit.

The block is executed here exactly as pasted (a shell reading it line by line),
with ``curl`` and ``sudo`` replaced by fakes and a real ``tar``/``sha256sum``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RUNBOOK = REPO / "docs" / "ORANGE_PI_TEACHER_RUNBOOK.md"
TOOLS = ("bash", "sha256sum", "tar", "python3")


def provisioning_block() -> str:
    """The first ```bash block under „## Schritt 2"."""
    lines = RUNBOOK.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("## Schritt 2"))
    opening = next(i for i in range(start, len(lines)) if lines[i] == "```bash")
    closing = next(i for i in range(opening + 1, len(lines)) if lines[i] == "```")
    return "\n".join(lines[opening + 1:closing]) + "\n"


def _package(path: Path, marker: str) -> None:
    src = path.parent / f"src-{marker}"
    (src / "pi_agent").mkdir(parents=True)
    setup = src / "pi_agent" / "setup.sh"
    setup.write_text(f"#!/bin/bash\necho {marker}\n", encoding="utf-8")
    setup.chmod(0o755)
    with tarfile.open(path, "w:gz") as tar:
        tar.add(src / "pi_agent", arcname="pi_agent")


@unittest.skipUnless(os.name == "posix" and all(shutil.which(t) for t in TOOLS),
                     "needs a POSIX shell with " + ", ".join(TOOLS))
class ProvisioningBlockFailsClosed(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        (self.tmp / "home").mkdir()
        (self.tmp / "bin").mkdir()
        self.good = self.tmp / "good.tar.gz"
        self.tampered = self.tmp / "tampered.tar.gz"
        _package(self.good, "GENUINE-SETUP-RAN")
        _package(self.tampered, "TAMPERED-SETUP-RAN")
        self.good_sha = subprocess.run(["sha256sum", str(self.good)], capture_output=True, text=True,
                                       check=True).stdout.split()[0]

    def _run(self, *, version, serve=None, version_fails=False, download_fails=False):
        (self.tmp / "version.json").write_text(version, encoding="utf-8")
        curl = self.tmp / "bin" / "curl"
        curl.write_text(
            "#!/bin/bash\n"
            'out=""; url=""\n'
            'while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift 2;; -*) shift;; *) url="$1"; shift;; esac; done\n'
            'case "$url" in\n'
            f'  */version) {"exit 22" if version_fails else "cp " + str(self.tmp / "version.json") + " \"$out\""} ;;\n'
            '  "") exit 3 ;;\n'
            f'  *) {"exit 22" if download_fails else "cp " + str(serve or self.good) + " \"$out\""} ;;\n'
            "esac\n",
            encoding="utf-8",
        )
        sudo = self.tmp / "bin" / "sudo"
        sudo.write_text('#!/bin/bash\necho SUDO-CALLED\nexec "$@"\n', encoding="utf-8")
        curl.chmod(0o755)
        sudo.chmod(0o755)
        env = {**os.environ, "HOME": str(self.tmp / "home"),
               "PATH": f"{self.tmp / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"}
        result = subprocess.run(["bash", "-s"], input=provisioning_block(), capture_output=True, text=True,
                                env=env, cwd=self.tmp / "home", timeout=60)
        unpacked = (self.tmp / "home" / "edubotics-install" / "pi_agent").exists()
        return result.stdout + result.stderr, unpacked

    def _version(self, url="https://example.invalid/edubotics-pi-agent.tar.gz", sha=None):
        return json.dumps({"pi_agent_download_url": url,
                           "pi_agent_sha256": self.good_sha if sha is None else sha})

    def assertStoppedBeforeRoot(self, out, unpacked):
        self.assertIn("ABBRUCH: ", out)
        self.assertNotIn("SUDO-CALLED", out)
        self.assertNotIn("SETUP-RAN", out)
        self.assertFalse(unpacked, "nothing may be unpacked")

    def test_a_tampered_package_is_never_unpacked_or_run(self):
        out, unpacked = self._run(version=self._version(), serve=self.tampered)
        self.assertIn("FAILED", out)
        self.assertStoppedBeforeRoot(out, unpacked)
        self.assertFalse((self.tmp / "home" / "edubotics-install" / "edubotics-pi-agent.tar.gz").exists())

    def test_an_empty_field_stops(self):
        for version in (self._version(sha=""), self._version(url=""), json.dumps({"version": "x"})):
            with self.subTest(version=version):
                self.assertStoppedBeforeRoot(*self._run(version=version))

    def test_an_unreadable_or_unreachable_cloud_stops(self):
        self.assertStoppedBeforeRoot(*self._run(version="<html>Captive portal</html>"))
        self.assertStoppedBeforeRoot(*self._run(version=self._version(), version_fails=True))

    def test_a_failed_download_stops(self):
        self.assertStoppedBeforeRoot(*self._run(version=self._version(), download_fails=True))

    def test_the_genuine_package_is_provisioned_as_root(self):
        out, unpacked = self._run(version=self._version())
        self.assertIn("edubotics-pi-agent.tar.gz: OK", out)
        self.assertIn("SUDO-CALLED", out)
        self.assertIn("GENUINE-SETUP-RAN", out)
        self.assertNotIn("ABBRUCH", out)
        self.assertTrue(unpacked)


if __name__ == "__main__":
    unittest.main()

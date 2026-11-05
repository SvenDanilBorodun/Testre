"""The third-party notice generators and the installer's licence payload (2026-11-05).

* ``.github/scripts/third_party_python_notices.py`` (the GUI's Python packages;
  run by ``release-installer.yml`` into the PyInstaller dist folder) is driven
  against real ``*.dist-info`` directories written to a temp dir, read through
  ``importlib.metadata`` exactly as in CI.
* ``physical_ai_manager/scripts/write-third-party-licenses.mjs`` (the web app's
  production npm packages, run by ``npm run build``) is driven with ``node``
  against a fixture lockfile + ``node_modules``. Skipped only when ``node`` is
  absent (the CI runner has it).
* The installer ships the EULA, ``THIRD_PARTY_NOTICES.txt`` and the licence
  texts the notice cites, and the notice carries the GPL/LGPL source offer with
  the licensor's contact.
"""

from __future__ import annotations

import importlib.metadata as md
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PY_GEN = REPO / ".github" / "scripts" / "third_party_python_notices.py"
JS_GEN = REPO / "physical_ai_tools" / "physical_ai_manager" / "scripts" / "write-third-party-licenses.mjs"
INSTALLER = REPO / "robotis_ai_setup" / "installer"
ISS = INSTALLER / "robotis_ai_setup.iss"

LICENSOR = "Sven Danil Borodun"
CONTACT = "svendanilborodun@gmail.com"


def _load_py_gen():
    spec = importlib.util.spec_from_file_location("third_party_python_notices", PY_GEN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_dist(root: Path, name: str, version: str, *, metadata_extra: str = "", files: dict[str, str] | None = None):
    info = root / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n{metadata_extra}", encoding="utf-8"
    )
    record = [f"{info.name}/METADATA,,"]
    for rel, text in (files or {}).items():
        p = info / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        record.append(f"{info.name}/{rel},,")
    (info / "RECORD").write_text("\n".join(record) + "\n", encoding="utf-8")


class PythonNoticeGenerator(unittest.TestCase):
    def setUp(self):
        self.gen = _load_py_gen()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def _entries(self):
        return self.gen.collect(md.distributions(path=[str(self.tmp)]))

    def test_declared_licence_and_every_licence_file(self):
        _write_dist(
            self.tmp, "alpha-pkg", "1.2.3",
            metadata_extra="License-Expression: MIT\nClassifier: License :: OSI Approved :: MIT License\n",
            files={"licenses/LICENSE": "MIT text alpha", "LICENSE-3RD-PARTY.txt": "bundled ffmpeg LGPL"},
        )
        (entry,) = self._entries()
        self.assertEqual(entry["name"], "alpha-pkg")
        self.assertEqual(entry["version"], "1.2.3")
        self.assertEqual(entry["declared"], ["MIT", "MIT License"])
        texts = dict(entry["files"])
        self.assertIn("MIT text alpha", texts.values())
        self.assertIn("bundled ffmpeg LGPL", texts.values())
        out = self.gen.render([entry], ("LICENSE.txt", "PSF text"))
        for needle in ("alpha-pkg 1.2.3", "Lizenz / License: MIT; MIT License", "MIT text alpha", "bundled ffmpeg LGPL", "PSF text"):
            self.assertIn(needle, out)

    def test_a_pasted_licence_text_in_the_license_field_is_kept_as_text(self):
        long_text = "Copyright (c) Someone\n" + "Permission is hereby granted " * 20
        _write_dist(self.tmp, "beta", "0.1", metadata_extra="License: " + long_text.replace("\n", "\n        ") + "\n")
        (entry,) = self._entries()
        self.assertEqual(entry["declared"], [])
        self.assertTrue(entry["files"])
        self.assertIn("Permission is hereby granted", entry["files"][0][1])

    def test_a_package_without_licence_files_says_so(self):
        _write_dist(self.tmp, "gamma", "2.0", metadata_extra="License: UNKNOWN\n", files={"README": "not a licence"})
        (entry,) = self._entries()
        self.assertEqual(entry["declared"], [])
        self.assertEqual(entry["files"], [])
        out = self.gen.render([entry], None)
        self.assertIn("nicht angegeben / not declared", out)
        self.assertIn("no licence file in the package metadata", out)

    def test_main_writes_the_real_environment_and_refuses_an_empty_one(self):
        out = self.tmp / "o" / "THIRD_PARTY_PYTHON.txt"
        self.assertEqual(self.gen.main(["--out", str(out)]), 0)
        text = out.read_text(encoding="utf-8")
        self.assertIn("EduBotics – Python-Bestandteile", text)
        self.assertGreater(text.count("\nLizenz / License: "), 0)
        empty = self.gen
        orig = empty.md.distributions
        try:
            empty.md.distributions = lambda: iter(())
            self.assertEqual(empty.main(["--out", str(self.tmp / "empty.txt")]), 2)
        finally:
            empty.md.distributions = orig


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class WebNoticeGenerator(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        lock = {
            "name": "app", "lockfileVersion": 3,
            "packages": {
                "": {"name": "app"},
                "node_modules/react": {"version": "19.0.0", "license": "MIT"},
                "node_modules/@scope/lib": {"version": "1.0.0", "license": "Apache-2.0"},
                "node_modules/@scope/lib/node_modules/nested": {"version": "0.1.0", "license": "ISC"},
                "node_modules/vitest": {"version": "3.0.0", "dev": True, "license": "MIT"},
                "node_modules/missing-optional": {"version": "1.0.0", "optional": True},
            },
        }
        (self.tmp / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
        for key, lic_file, text in (
            ("node_modules/react", "LICENSE", "MIT react text"),
            ("node_modules/@scope/lib", "LICENSE.md", "Apache scope text"),
            ("node_modules/@scope/lib/node_modules/nested", "LICENCE", "ISC nested text"),
            ("node_modules/vitest", "LICENSE", "DEV ONLY TEXT"),
        ):
            d = self.tmp / key
            d.mkdir(parents=True)
            name = key.split("node_modules/")[-1]
            (d / "package.json").write_text(json.dumps({"name": name, "version": "x"}), encoding="utf-8")
            (d / lic_file).write_text(text, encoding="utf-8")
        (self.tmp / "build").mkdir()

    def _run(self, *args):
        return subprocess.run(["node", str(JS_GEN), *args], capture_output=True, text=True, timeout=60)

    def test_production_packages_with_their_texts_and_no_dev_package(self):
        out = self.tmp / "build" / "third-party-licenses.txt"
        r = self._run(str(self.tmp), str(out))
        self.assertEqual(r.returncode, 0, r.stderr)
        text = out.read_text(encoding="utf-8")
        for needle in ("react 19.0.0", "MIT react text", "@scope/lib 1.0.0", "Apache-2.0", "Apache scope text",
                       "nested 0.1.0", "ISC nested text"):
            self.assertIn(needle, text)
        self.assertNotIn("DEV ONLY TEXT", text)
        self.assertNotIn("vitest", text)
        self.assertNotIn("missing-optional", text)

    def test_an_empty_closure_fails_the_build(self):
        (self.tmp / "package-lock.json").write_text(
            json.dumps({"lockfileVersion": 3, "packages": {"": {}}}), encoding="utf-8"
        )
        r = self._run(str(self.tmp), str(self.tmp / "build" / "x.txt"))
        self.assertNotEqual(r.returncode, 0)

    def test_the_build_script_runs_it_after_vite(self):
        pkg = json.loads((JS_GEN.parent.parent / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(pkg["scripts"]["build"], "vite build && node scripts/write-third-party-licenses.mjs")


class InstallerLicencePayload(unittest.TestCase):
    def setUp(self):
        self.iss = ISS.read_text(encoding="utf-8")

    def test_the_eula_names_the_licensor_and_is_no_longer_apache(self):
        eula = (INSTALLER / "assets" / "license.txt").read_text(encoding="utf-8")
        self.assertIn(LICENSOR, eula)
        self.assertIn(CONTACT, eula)
        self.assertIn("Unsuhrstrasse 10, 45325 Essen", eula)
        self.assertNotIn("Lizenziert unter der Apache License", eula)
        self.assertNotIn("Docker Desktop", eula)
        self.assertIn("THIRD_PARTY_NOTICES.txt", eula)

    def test_the_notice_carries_the_source_offer_and_names_every_licence_file(self):
        notice = (INSTALLER / "assets" / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
        self.assertIn(CONTACT, notice)
        self.assertIn("Unsuhrstrasse 10, 45325 Essen", notice)
        self.assertIn("drei Jahre", notice)
        texts = sorted(p.name for p in (INSTALLER / "assets" / "licenses").glob("*.txt"))
        self.assertEqual(texts, ["Apache-2.0.txt", "GPL-2.0-only.txt", "GPL-3.0-only.txt",
                                 "LGPL-2.1-only.txt", "LGPL-3.0-only.txt"])
        for name in texts:
            with self.subTest(text=name):
                self.assertIn(f"licenses\\{name}", notice)

    def test_the_installer_ships_the_licence_files_and_speaks_german(self):
        self.assertIn('Source: "assets\\license.txt"; DestDir: "{app}"; DestName: "LICENSE.txt"', self.iss)
        self.assertIn('Source: "assets\\THIRD_PARTY_NOTICES.txt"; DestDir: "{app}"', self.iss)
        self.assertIn('Source: "assets\\licenses\\*"; DestDir: "{app}\\licenses"', self.iss)
        self.assertIn('MessagesFile: "compiler:Languages\\German.isl"', self.iss)

    def test_the_installer_build_writes_the_python_notices_into_the_dist_folder(self):
        text = (REPO / ".github" / "workflows" / "release-installer.yml").read_text(encoding="utf-8")
        self.assertIn(
            "python ../../.github/scripts/third_party_python_notices.py --out dist/EduBotics/THIRD_PARTY_PYTHON.txt",
            text,
        )
        self.assertIn('Source: "..\\gui\\dist\\EduBotics\\*"; DestDir: "{app}\\gui"', self.iss)


if __name__ == "__main__":
    unittest.main()

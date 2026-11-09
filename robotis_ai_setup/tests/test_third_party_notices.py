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
* Until the owner settles the legal entity (2026-11-09), every licence file
  names the licensor, its address and its contact by German placeholders, and
  no personal contact data (a home address, a personal mail address, a named
  court town) appears in any of them. The check describes the SHAPE of such
  data, never a value, and proves on invented examples that it still bites.
"""

from __future__ import annotations

import importlib.metadata as md
import importlib.util
import json
import re
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

LICENSOR = "[Anbieter – wird nach Klärung der Rechtsform ergänzt]"
ADDRESS = "[Anschrift – wird ergänzt]"
CONTACT = "[Kontakt-E-Mail – wird ergänzt]"
# Personal contact data must never stand in a licence file. These patterns
# describe its shape only — never a real value, and never a hash of one (a
# five-digit postcode's hash is reversed by trying all 100 000). A legal-entity
# address that is meant to be published replaces the placeholders later; adjust
# the patterns in the same change.
_STREET_SUFFIX = r"(?i:stra(?:ß|ss)e|str\.|weg|allee|platz|gasse|ring|damm|ufer|chaussee|markt)"
_HOUSE_NUMBER = r"\s*\d{1,4}(?:\s?[a-zA-Z])?\b"
PERSONAL_DATA_PATTERNS = {
    "a personal e-mail address": re.compile(
        r"[\w.+-]+@(?:gmail|googlemail|gmx|web|t-online|outlook|hotmail|live|yahoo|icloud|me|mac|aol"
        r"|freenet|posteo|mailbox|proton|protonmail|arcor|online)\.[a-z]{2,}\b",
        re.IGNORECASE,
    ),
    # „Musterstraße 12", „Am Markt 3a", „Berliner Straße 7".
    "a street and house number": re.compile(
        r"\b[A-ZÄÖÜ][\w-]*?" + _STREET_SUFFIX + _HOUSE_NUMBER
        + r"|\b[A-ZÄÖÜ][\w-]*\s+" + _STREET_SUFFIX + _HOUSE_NUMBER
    ),
    # „12345 Musterstadt", „D-12345 Musterstadt".
    "a postcode and town": re.compile(r"(?<![\w.,-])(?:D-)?\d{5}\s+[A-ZÄÖÜ][a-zäöüß]{2,}"),
    # „Gerichtsstand Musterstadt", „Gerichtsstand ist Musterstadt", „Amtsgericht Musterstadt".
    # The EULA's „Gerichtsstand der Sitz des Lizenzgebers" names no town and passes.
    "a court in a named town": re.compile(
        r"\bGerichtsstand\b\s*:?\s*(?:ist\s+)?(?:in\s+)?[A-ZÄÖÜ][a-zäöüß]+"
        r"|\b(?:Amts|Land)gericht\s+[A-ZÄÖÜ][a-zäöüß]+"
    ),
}
# Invented examples (Erika Mustermann, Musterstadt): each must be caught, so a
# pattern that stops matching fails here instead of passing every file.
INVENTED_PERSONAL_DATA = {
    "a personal e-mail address": ("Kontakt: erika.mustermann@gmail.com", "E-Mail: e.m+lizenz@GMX.de"),
    "a street and house number": ("Musterstraße 12", "Musterstrasse 4a", "Am Markt 3", "Berliner Straße 7",
                                  "Lindenweg 1"),
    "a postcode and town": ("12345 Musterstadt", "D-12345 Musterstadt"),
    "a court in a named town": ("Gerichtsstand Musterstadt", "Gerichtsstand ist Musterstadt.",
                                "Gerichtsstand: Musterstadt", "Amtsgericht Musterstadt"),
}
LICENCE_FILES = (
    REPO / "LICENSE",
    REPO / "NOTICE",
    INSTALLER / "assets" / "license.txt",
    INSTALLER / "assets" / "THIRD_PARTY_NOTICES.txt",
    ISS,
)


def personal_data_findings(text: str) -> list[tuple[str, int]]:
    """(kind, line number) of every match. The matched text is never returned,
    so a failure message cannot copy personal data into a CI log."""
    found = []
    for kind, pattern in PERSONAL_DATA_PATTERNS.items():
        for match in pattern.finditer(text):
            found.append((kind, text.count("\n", 0, match.start()) + 1))
    return found


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
                "node_modules/tailwindcss": {"version": "3.4.17", "dev": True, "license": "MIT"},
                "node_modules/missing-optional": {"version": "1.0.0", "optional": True},
            },
        }
        (self.tmp / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
        for key, lic_file, text in (
            ("node_modules/react", "LICENSE", "MIT react text"),
            ("node_modules/@scope/lib", "LICENSE.md", "Apache scope text"),
            ("node_modules/@scope/lib/node_modules/nested", "LICENCE", "ISC nested text"),
            ("node_modules/vitest", "LICENSE", "DEV ONLY TEXT"),
            ("node_modules/tailwindcss", "LICENSE", "TAILWIND BUNDLED TEXT"),
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
                       "nested 0.1.0", "ISC nested text", "tailwindcss 3.4.17", "TAILWIND BUNDLED TEXT"):
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
        self.assertIn(ADDRESS, eula)
        self.assertNotIn("Lizenziert unter der Apache License", eula)
        self.assertNotIn("Docker Desktop", eula)
        self.assertIn("THIRD_PARTY_NOTICES.txt", eula)

    def test_the_notice_carries_the_source_offer_and_names_every_licence_file(self):
        notice = (INSTALLER / "assets" / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
        self.assertIn(CONTACT, notice)
        self.assertIn(ADDRESS, notice)
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

    def test_the_installer_names_its_copyright_holder(self):
        self.assertIn(f"AppCopyright=© 2025–2026 {LICENSOR} (EduBotics)", self.iss)

    def test_no_personal_contact_data_in_any_licence_file(self):
        for path in LICENCE_FILES:
            with self.subTest(file=path.name):
                self.assertEqual(personal_data_findings(path.read_text(encoding="utf-8")), [],
                                 "personal contact data (kind, line) in a licence file")
        for path in (REPO / "LICENSE", REPO / "NOTICE"):
            with self.subTest(file=path.name):
                self.assertIn(f"Copyright (c) 2025-2026 {LICENSOR} (EduBotics)", path.read_text(encoding="utf-8"))

    def test_the_personal_data_check_catches_every_kind_and_spares_the_placeholders(self):
        self.assertEqual(set(INVENTED_PERSONAL_DATA), set(PERSONAL_DATA_PATTERNS))
        for kind, examples in INVENTED_PERSONAL_DATA.items():
            for example in examples:
                with self.subTest(kind=kind, example=example):
                    self.assertIn(kind, [k for k, _ in personal_data_findings(f"Lizenzgeber:\n  {example}\n")])
        for clean in (LICENSOR, ADDRESS, CONTACT, "ist Gerichtsstand der Sitz des Lizenzgebers.",
                      "usbipd-win 5.3.0 (GNU General Public License 3.0)", "Stand: 9. November 2026"):
            with self.subTest(clean=clean):
                self.assertEqual(personal_data_findings(clean), [])

    def test_usbipd_win_is_named_under_its_own_licence(self):
        # usbipd-win 5.3.0: COPYING.md is the GPL v3 and every source file says
        # `SPDX-License-Identifier: GPL-3.0-only` (github.com/dorssel/usbipd-win,
        # tag v5.3.0, checked 2026-11-09). PR #49 had written version 2.
        eula = (INSTALLER / "assets" / "license.txt").read_text(encoding="utf-8")
        notice = (INSTALLER / "assets" / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
        self.assertIn("usbipd-win steht\n    unter der GNU General Public License, Version 3.", eula)
        self.assertIn("2.1 usbipd-win 5.3.0 (GNU General Public License 3.0).", notice)

    def test_the_pi_tarball_carries_the_licence(self):
        import yaml
        job = yaml.safe_load((REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))["jobs"]["pi-agent-tarball"]
        run = next(s for s in job["steps"] if s.get("name") == "Build edubotics-pi-agent.tar.gz")["run"]
        self.assertIn("cp robotis_ai_setup/installer/assets/license.txt robotis_ai_setup/pi_agent/LICENSE.txt", run)
        self.assertIn("cp robotis_ai_setup/installer/assets/THIRD_PARTY_NOTICES.txt robotis_ai_setup/pi_agent/THIRD_PARTY_NOTICES.txt", run)
        self.assertIn("rm -f robotis_ai_setup/pi_agent/LICENSE.txt robotis_ai_setup/pi_agent/THIRD_PARTY_NOTICES.txt", run)
        self.assertIn("for lic in LICENSE.txt THIRD_PARTY_NOTICES.txt; do", run)

    def test_the_installer_build_writes_the_python_notices_into_the_dist_folder(self):
        text = (REPO / ".github" / "workflows" / "release-installer.yml").read_text(encoding="utf-8")
        self.assertIn(
            "python ../../.github/scripts/third_party_python_notices.py --out dist/EduBotics/THIRD_PARTY_PYTHON.txt",
            text,
        )
        self.assertIn('Source: "..\\gui\\dist\\EduBotics\\*"; DestDir: "{app}\\gui"', self.iss)


if __name__ == "__main__":
    unittest.main()

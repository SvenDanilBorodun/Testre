"""Licence headers match the licence (2026-11-05).

EduBotics' own code is proprietary (repository-root LICENSE, the installer's
EULA); the code derived from ROBOTIS Co., Ltd. stays Apache-2.0 with its
copyright kept (Apache-2.0 section 4(c)) and a notice that EduBotics changed it
(section 4(b)). Before this change 749 EduBotics files carried an Apache-2.0
header, 74 of them shipped as source inside the public robot image, and a 2026
rebrand had replaced the ROBOTIS copyright line of 46 derived web files with
"Copyright 2025 EduBotics".

Rules, over every source file under the shipped roots:

1. A header with an EduBotics copyright carries no Apache-2.0 grant unless the
   same header keeps a ROBOTIS copyright (a derived file).
2. A "Modifications copyright … (EduBotics)" line sits directly under a ROBOTIS
   copyright line, in a header that keeps the Apache-2.0 text.
3. A proprietary header names the holder in full and points at LICENSE, and is
   never mixed with a ROBOTIS copyright or an upstream `Author:` line.
4. A ROBOTIS header keeps the whole Apache-2.0 notice.
5. Every font the web app ships has its licence beside it.
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOTS = ["physical_ai_tools", "open_manipulator", "robotis_ai_setup", "tools"]
EXTENSIONS = {".py", ".js", ".jsx", ".mjs", ".css", ".cpp", ".hpp", ".h", ".sh"}
SKIP_DIRS = {"node_modules", "build", "dist", ".git", "__pycache__", "output"}
HEAD_LINES = 30

# Header lines only: a comment line (#, //, /* or *), never prose that quotes one.
EDU_COPYRIGHT = re.compile(r"^\s*(?:#|//|/\*|\*)\s*Copyright\b[^\n]*\bEduBotics\b", re.M)
ROBOTIS_COPYRIGHT = re.compile(r"^\s*(?:#|//|/\*|\*)\s*Copyright\b[^\n]*\bROBOTIS\b", re.M)
APACHE = "Licensed under the Apache License"
MODIFICATIONS = "Modifications copyright 2026 Sven Danil Borodun (EduBotics)"
PROPRIETARY_FIRST = re.compile(r"Copyright \d{4} Sven Danil Borodun \(EduBotics\)\. All rights reserved\.")
PROPRIETARY_TEXT = "Licence Agreement; see LICENSE at the repository root."


def _sources():
    for root in ROOTS:
        for dirpath, dirnames, filenames in os.walk(REPO / root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                path = Path(dirpath) / name
                if path.suffix in EXTENSIONS:
                    yield path


def _head(path: Path) -> list[str]:
    try:
        with path.open(encoding="utf-8") as fh:
            return [next(fh, "") for _ in range(HEAD_LINES)]
    except UnicodeDecodeError:
        return []


class LicenceHeaders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.heads = {p.relative_to(REPO).as_posix(): _head(p) for p in _sources()}

    def test_the_scan_sees_the_tree(self):
        self.assertGreater(len(self.heads), 1000)

    def test_no_apache_grant_on_edubotics_own_code(self):
        bad = []
        for rel, head in self.heads.items():
            text = "".join(head)
            if EDU_COPYRIGHT.search(text) and APACHE in text and not ROBOTIS_COPYRIGHT.search(text):
                bad.append(rel)
        self.assertEqual(bad, [], "EduBotics code with an Apache-2.0 header")

    def test_modification_lines_sit_under_a_kept_robotis_copyright(self):
        count = 0
        for rel, head in self.heads.items():
            for i, line in enumerate(head):
                if MODIFICATIONS in line:
                    count += 1
                    with self.subTest(file=rel):
                        self.assertTrue(i > 0 and ROBOTIS_COPYRIGHT.search(head[i - 1]), "ROBOTIS copyright directly above")
                        self.assertIn(APACHE, "".join(head), "the Apache-2.0 text stays")
        # 2026-11-05: 61 = the ROBOTIS files EduBotics changed (by content against the
        # absorbed b0b601fa tree) plus StudentApp.js, split out of App.js. A ROBOTIS
        # file that is byte-identical to upstream carries NO modification line.
        # 2026-11-09: 51 — the local-inference UI the Inferenz page no longer
        # renders was deleted (ControlPanel, InferencePanel, PolicyDownloadModal,
        # the file browser and its modal, CompactSystemStatus, SystemStatus,
        # EpisodeStatus, FullTaskStatus, ProgressBar, constants/paths.js).
        self.assertGreaterEqual(count, 51)

    def test_proprietary_headers_are_complete_and_never_mixed(self):
        count = 0
        for rel, head in self.heads.items():
            text = "".join(head)
            if PROPRIETARY_FIRST.search(text):
                count += 1
                with self.subTest(file=rel):
                    self.assertIn(PROPRIETARY_TEXT, text)
                    self.assertNotIn(APACHE, text)
                    self.assertIsNone(ROBOTIS_COPYRIGHT.search(text))
            elif EDU_COPYRIGHT.search(text) and MODIFICATIONS not in text:
                with self.subTest(file=rel):
                    self.fail("an EduBotics copyright without the proprietary header or a modification line")
        # 2026-11-09: 690 — 705 minus the 15 proprietary React files of the retired
        # classroom Jetson and the deleted local-inference UI.
        self.assertGreaterEqual(count, 690)

    def test_a_proprietary_header_never_sits_beside_an_upstream_author(self):
        """An `Author:` line comes from ROBOTIS code (EduBotics files carry none):
        such a file is derived and keeps the ROBOTIS copyright and Apache text."""
        for rel, head in self.heads.items():
            text = "".join(head)
            if PROPRIETARY_FIRST.search(text):
                with self.subTest(file=rel):
                    self.assertIsNone(re.search(r"^\s*(?:#|//|\*)\s*Author:", text, re.M))

    def test_a_robotis_header_keeps_the_whole_apache_notice(self):
        count = 0
        for rel, head in self.heads.items():
            text = "".join(head)
            if ROBOTIS_COPYRIGHT.search(text):
                count += 1
                with self.subTest(file=rel):
                    self.assertIn(APACHE, text)
                    self.assertIn("limitations under the License.", text)
        self.assertGreaterEqual(count, 120)


UPSTREAM = "b0b601fa"  # the commit that absorbed open_manipulator/ and physical_ai_tools/


def _upstream(rel: str):
    """The file's bytes at the absorb commit, or None (no history, or no such path)."""
    import subprocess
    r = subprocess.run(["git", "-C", str(REPO), "show", f"{UPSTREAM}:{rel}"], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def _history_available() -> bool:
    import subprocess
    return subprocess.run(["git", "-C", str(REPO), "cat-file", "-e", f"{UPSTREAM}^{{commit}}"],
                          capture_output=True).returncode == 0


@unittest.skipUnless(_history_available(), f"needs git history with {UPSTREAM} (CI's shallow checkout has none)")
class ModificationLinesAreTrue(unittest.TestCase):
    """A modification line means EduBotics changed the file's CONTENT: with the
    line taken out, the file must differ from its absorbed upstream version."""

    def test_no_modification_line_on_an_unchanged_upstream_file(self):
        checked = 0
        for path in _sources():
            raw = path.read_bytes()
            if MODIFICATIONS.encode() not in raw:
                continue
            rel = path.relative_to(REPO).as_posix()
            old = _upstream(rel)
            if old is None:  # a file split out of, or copied from, an upstream file elsewhere
                continue
            checked += 1
            without = b"\n".join(l for l in raw.split(b"\n") if MODIFICATIONS.encode() not in l)
            with self.subTest(file=rel):
                self.assertNotEqual(without, old, "byte-identical to upstream: no modification line")
        # 2026-11-09: 47 — the deleted local-inference UI took ten modified
        # upstream files with it (see test_modification_lines_… above).
        self.assertGreaterEqual(checked, 47)


class FontLicences(unittest.TestCase):
    def test_every_shipped_font_has_its_licence(self):
        fonts = REPO / "physical_ai_tools" / "physical_ai_manager" / "public" / "fonts"
        licences = {p.stem[len("OFL-"):].lower() for p in fonts.glob("OFL-*.txt")}
        faces = sorted(fonts.glob("*.woff2")) + sorted(fonts.glob("*.woff")) + sorted(fonts.glob("*.ttf"))
        self.assertTrue(faces)
        for face in faces:
            family = re.split(r"-latin", face.name)[0].replace("-", "").lower()
            with self.subTest(font=face.name):
                self.assertIn(family, licences, f"{face.name} ships without OFL-*.txt")


if __name__ == "__main__":
    unittest.main()

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
   never mixed with a ROBOTIS copyright.
4. Every font the web app ships has its licence beside it.
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
        self.assertGreaterEqual(count, 94, "the 46 restored and 48 modified ROBOTIS files")

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
        self.assertGreaterEqual(count, 703)


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

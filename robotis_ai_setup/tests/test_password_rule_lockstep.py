# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The password rule is spelled three times and must say the same thing
(owner decision 2026-11-10: new and reset passwords ≥ 12 for teachers and
admins, ≥ 8 for students, ≤ 128):

* the cloud API, which refuses (cloud_training_api/app/services/passwords.py),
* the teacher web, which refuses first (physical_ai_manager/src/utils/passwordRules.js),
* the first admin's bootstrap script (scripts/bootstrap_admin.py).
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
API = ROOT / "cloud_training_api" / "app" / "services" / "passwords.py"
WEB = REPO / "physical_ai_tools" / "physical_ai_manager" / "src" / "utils" / "passwordRules.js"
BOOTSTRAP = ROOT / "scripts" / "bootstrap_admin.py"


def _py_constants(path: Path) -> dict:
    out = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                out[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                pass
    return out


def _js_constants(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    return {m[1]: int(m[2]) for m in re.finditer(r"^export const ([A-Z_]+) = (\d+);$", text, re.M)}


class PasswordRuleLockstep(unittest.TestCase):
    def test_the_three_spellings_agree(self):
        api, web, boot = _py_constants(API), _js_constants(WEB), _py_constants(BOOTSTRAP)
        self.assertEqual((api["STAFF_MIN_LENGTH"], api["STUDENT_MIN_LENGTH"], api["MAX_LENGTH"]), (12, 8, 128))
        self.assertEqual(
            (web["STAFF_PASSWORD_MIN"], web["STUDENT_PASSWORD_MIN"], web["PASSWORD_MAX"]),
            (api["STAFF_MIN_LENGTH"], api["STUDENT_MIN_LENGTH"], api["MAX_LENGTH"]),
        )
        self.assertEqual((boot["ADMIN_PASSWORD_MIN_LENGTH"], boot["PASSWORD_MAX_LENGTH"]),
                         (api["STAFF_MIN_LENGTH"], api["MAX_LENGTH"]))

    def test_the_sentences_agree(self):
        api = API.read_text(encoding="utf-8")
        web = WEB.read_text(encoding="utf-8")
        for fragment in ("Das Passwort muss für", "Zeichen lang sein.", "Lehrkräfte und Admins",
                         "Schülerinnen und Schüler", "Das Passwort darf höchstens"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, api)
                self.assertIn(fragment, web)

    def test_the_forms_use_the_rule_not_a_literal(self):
        comps = REPO / "physical_ai_tools" / "physical_ai_manager" / "src" / "components"
        for rel, audience in (("admin/CreateTeacherModal.js", "'staff'"),
                              ("teacher/CreateStudentModal.js", "'student'"),
                              ("teacher/PasswordResetModal.js", "audience")):
            with self.subTest(form=rel):
                text = (comps / rel).read_text(encoding="utf-8")
                self.assertIn(f"passwordErrorDe(password, {audience})", text)
                self.assertNotRegex(text, r"password\.length < \d")
        row = (comps / "admin" / "TeacherRow.js").read_text(encoding="utf-8")
        self.assertIn('audience="staff"', row)


if __name__ == "__main__":
    unittest.main()

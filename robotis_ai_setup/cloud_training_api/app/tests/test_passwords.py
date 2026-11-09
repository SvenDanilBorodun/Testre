# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The password rule (owner decision 2026-11-10): a new or reset password is at
least 12 characters for a teacher or an admin and 8 for a student, at most 128,
refused in German BEFORE Supabase is asked; Supabase Auth's own refusal (its
project minimum, the leaked-password check) is a German 400, not a bare 500."""

from __future__ import annotations

import ast
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.tests.test_dataset_identity_routes import _ensure_stubs

_ensure_stubs()

from fastapi import HTTPException  # noqa: E402
from app.routes import admin as admin_route  # noqa: E402
from app.routes import teacher as teacher_route  # noqa: E402
from app.services import passwords  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
STAFF_SHORT_DE = "Das Passwort muss für Lehrkräfte und Admins mindestens 12 Zeichen lang sein."
STUDENT_SHORT_DE = "Das Passwort muss für Schülerinnen und Schüler mindestens 8 Zeichen lang sein."


class AuthWeakPasswordError(Exception):
    """gotrue's class name and code, without importing gotrue."""

    code = "weak_password"


class _Query:
    def __init__(self, sb, table):
        self.sb, self.table = sb, table

    def __getattr__(self, _name):
        return lambda *a, **k: self

    def execute(self):
        self.sb.calls.append(("table", self.table))
        return SimpleNamespace(data=self.sb.rows.get(self.table, []), count=0)


class FakeSupabase:
    def __init__(self, create_error=None, update_error=None):
        self.calls = []
        self.rows = {}
        self.create_error = create_error
        self.update_error = update_error
        self.auth = SimpleNamespace(admin=SimpleNamespace(
            create_user=self._create_user, update_user_by_id=self._update_user, delete_user=lambda uid: None))

    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, args):
        self.calls.append(("rpc", name))
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=[]))

    def _create_user(self, attrs):
        self.calls.append(("create_user", attrs["password"]))
        if self.create_error:
            raise self.create_error
        return SimpleNamespace(user=SimpleNamespace(id="new-user"))

    def _update_user(self, uid, attrs):
        self.calls.append(("update_user", attrs["password"]))
        if self.update_error:
            raise self.update_error
        return SimpleNamespace(user=SimpleNamespace(id=uid))


def refusal(call):
    try:
        call()
    except HTTPException as exc:
        return exc.status_code, exc.detail
    return None


class TheRule(unittest.TestCase):
    def test_the_numbers_and_sentences(self):
        self.assertEqual((passwords.STAFF_MIN_LENGTH, passwords.STUDENT_MIN_LENGTH, passwords.MAX_LENGTH),
                         (12, 8, 128))
        self.assertEqual(passwords.password_error_de("x" * 11, "teacher"), STAFF_SHORT_DE)
        self.assertEqual(passwords.password_error_de("x" * 11, "admin"), STAFF_SHORT_DE)
        self.assertIsNone(passwords.password_error_de("x" * 12, "teacher"))
        self.assertEqual(passwords.password_error_de("x" * 7, "student"), STUDENT_SHORT_DE)
        self.assertIsNone(passwords.password_error_de("x" * 8, "student"))
        self.assertEqual(passwords.password_error_de("x" * 129, "student"),
                         "Das Passwort darf höchstens 128 Zeichen lang sein.")
        self.assertEqual(passwords.password_error_de(None, "student"), STUDENT_SHORT_DE)
        self.assertTrue(passwords.is_weak_password_error(AuthWeakPasswordError()))
        self.assertFalse(passwords.is_weak_password_error(RuntimeError("Password should be")))


class AdminRoutes(unittest.TestCase):
    ADMIN = {"id": "a1", "role": "admin"}

    def create(self, password, sb):
        req = admin_route.TeacherCreate(username="frau.k", password=password, full_name="Frau K", credits=0)
        with patch.object(admin_route, "get_supabase", lambda: sb), \
                patch.object(admin_route, "teacher_summary", lambda tid: {}), \
                patch.object(admin_route, "pool_fields", lambda s: {k: 0 for k in (
                    "pool_total", "allocated_total", "pool_available", "student_count", "hours_spent",
                    "compensated")}):
            return refusal(lambda: admin_route.create_teacher(req, admin=self.ADMIN))

    def test_a_short_teacher_password_is_refused_before_supabase(self):
        sb = FakeSupabase()
        self.assertEqual(self.create("elf-zeichen", sb), (400, STAFF_SHORT_DE))
        self.assertEqual(sb.calls, [])

    def test_twelve_characters_reach_supabase(self):
        sb = FakeSupabase()
        self.assertIsNone(self.create("zwoelf-zeich", sb))
        self.assertIn(("create_user", "zwoelf-zeich"), sb.calls)

    def test_supabases_own_refusal_is_german(self):
        sb = FakeSupabase(create_error=AuthWeakPasswordError("Password is known to be weak"))
        self.assertEqual(self.create("passwort1234", sb), (400, passwords.WEAK_PASSWORD_DE))

    def test_a_teacher_reset(self):
        sb = FakeSupabase()
        sb.rows["users"] = [{"role": "teacher"}]
        reset = admin_route.PasswordReset(new_password="kurz")
        with patch.object(admin_route, "get_supabase", lambda: sb):
            self.assertEqual(refusal(lambda: admin_route.reset_teacher_password("t1", reset, admin=self.ADMIN)),
                             (400, STAFF_SHORT_DE))
            self.assertEqual(sb.calls, [])
            ok = admin_route.PasswordReset(new_password="zwoelf-zeich")
            self.assertEqual(admin_route.reset_teacher_password("t1", ok, admin=self.ADMIN), {"ok": True})
            sb.update_error = AuthWeakPasswordError()
            self.assertEqual(refusal(lambda: admin_route.reset_teacher_password("t1", ok, admin=self.ADMIN)),
                             (400, passwords.WEAK_PASSWORD_DE))


class TeacherRoutes(unittest.TestCase):
    TEACHER = {"id": "t1", "role": "teacher"}

    def test_a_short_student_password_is_refused_before_supabase(self):
        sb = FakeSupabase()
        req = teacher_route.StudentCreate(username="max.m", password="sieben7", full_name="Max", initial_credits=0)
        with patch.object(teacher_route, "get_supabase", lambda: sb), \
                patch.object(teacher_route, "_assert_classroom_owned", lambda t, c: {}):
            self.assertEqual(refusal(lambda: teacher_route.create_student("c1", req, teacher=self.TEACHER)),
                             (400, STUDENT_SHORT_DE))
        self.assertEqual(sb.calls, [])

    def test_eight_characters_reach_supabase_and_a_weak_one_is_german(self):
        sb = FakeSupabase(create_error=AuthWeakPasswordError())
        req = teacher_route.StudentCreate(username="max.m", password="achtacht", full_name="Max", initial_credits=0)
        with patch.object(teacher_route, "get_supabase", lambda: sb), \
                patch.object(teacher_route, "_assert_classroom_owned", lambda t, c: {}):
            self.assertEqual(refusal(lambda: teacher_route.create_student("c1", req, teacher=self.TEACHER)),
                             (400, passwords.WEAK_PASSWORD_DE))
        self.assertIn(("create_user", "achtacht"), sb.calls)

    def test_a_student_reset(self):
        sb = FakeSupabase()
        with patch.object(teacher_route, "get_supabase", lambda: sb), \
                patch.object(teacher_route, "_assert_student_owned", lambda t, s: {}):
            short = teacher_route.PasswordReset(new_password="sieben7")
            self.assertEqual(refusal(lambda: teacher_route.reset_student_password("s1", short, teacher=self.TEACHER)),
                             (400, STUDENT_SHORT_DE))
            self.assertEqual(sb.calls, [])
            ok = teacher_route.PasswordReset(new_password="achtacht")
            self.assertEqual(teacher_route.reset_student_password("s1", ok, teacher=self.TEACHER), {"ok": True})
            self.assertEqual(sb.calls, [("update_user", "achtacht")])


class NoPydanticLengthOnAPassword(unittest.TestCase):
    """pydantic's own 422 is English and echoes the password back: the four
    password fields carry no Field constraint any more."""

    def test_the_models(self):
        for rel, fields in (("routes/admin.py", {"password", "new_password"}),
                            ("routes/teacher.py", {"password", "new_password"})):
            with open(os.path.join(APP, rel), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            seen = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) in fields:
                    seen.add(node.target.id)
                    self.assertIsNone(node.value, f"{rel}: {node.target.id} has a pydantic constraint")
            self.assertEqual(seen, fields, rel)


if __name__ == "__main__":
    unittest.main()

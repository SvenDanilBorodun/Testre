# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""Every account the cloud API creates carries the provisioning marker (migration
058 refuses a new auth.users row without app_metadata.edubotics_provisioned):
the teacher's student creation and the admin's teacher creation both send it to
``auth.admin.create_user``, through ``services/provisioning.py``."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.tests.test_dataset_identity_routes import _ensure_stubs

_ensure_stubs()

from app.routes import admin as admin_route  # noqa: E402
from app.routes import teacher as teacher_route  # noqa: E402
from app.services import provisioning  # noqa: E402

MARKER = {"edubotics_provisioned": True}


class _Query:
    def __init__(self, sb, table):
        self.sb, self.table = sb, table

    def __getattr__(self, _name):
        return lambda *a, **k: self

    def execute(self):
        return SimpleNamespace(data=self.sb.rows.get(self.table, []), count=0)


class FakeSupabase:
    def __init__(self):
        self.created = []
        self.rows = {}
        self.auth = SimpleNamespace(admin=SimpleNamespace(
            create_user=self._create_user, delete_user=lambda uid: None))

    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, args):
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=[]))

    def _create_user(self, attrs):
        self.created.append(attrs)
        return SimpleNamespace(user=SimpleNamespace(id="00000000-0000-0000-0000-0000000000aa"))


class TheHelper(unittest.TestCase):
    def test_the_attributes(self):
        self.assertEqual(provisioning.new_account_attributes("max@edubotics.local", "achtacht"), {
            "email": "max@edubotics.local",
            "password": "achtacht",
            "email_confirm": True,
            "app_metadata": MARKER,
        })

    def test_each_call_gets_its_own_dict(self):
        a = provisioning.new_account_attributes("a@x", "p")
        a["app_metadata"]["edubotics_provisioned"] = False
        self.assertEqual(provisioning.new_account_attributes("b@x", "p")["app_metadata"], MARKER)


class CreatePaths(unittest.TestCase):
    def test_the_teachers_student_creation_sends_the_marker(self):
        sb = FakeSupabase()
        req = teacher_route.StudentCreate(username="max.m", password="achtacht", full_name="Max", initial_credits=0)
        with patch.object(teacher_route, "get_supabase", lambda: sb), \
                patch.object(teacher_route, "_assert_classroom_owned", lambda t, c: {}):
            try:
                teacher_route.create_student("c1", req, teacher={"id": "t1", "role": "teacher"})
            except Exception:  # noqa: BLE001 — only the create_user call matters here
                pass
        self.assertEqual(len(sb.created), 1)
        self.assertEqual(sb.created[0]["app_metadata"], MARKER)
        self.assertEqual(sb.created[0]["email"], "max.m@edubotics.local")

    def test_the_admins_teacher_creation_sends_the_marker(self):
        sb = FakeSupabase()
        req = admin_route.TeacherCreate(username="frau.k", password="zwoelf-zeich", full_name="Frau K", credits=0)
        with patch.object(admin_route, "get_supabase", lambda: sb), \
                patch.object(admin_route, "teacher_summary", lambda tid: {}), \
                patch.object(admin_route, "pool_fields", lambda s: {k: 0 for k in (
                    "pool_total", "allocated_total", "pool_available", "student_count", "hours_spent",
                    "compensated")}):
            try:
                admin_route.create_teacher(req, admin={"id": "a1", "role": "admin"})
            except Exception:  # noqa: BLE001
                pass
        self.assertEqual(len(sb.created), 1)
        self.assertEqual(sb.created[0]["app_metadata"], MARKER)


if __name__ == "__main__":
    unittest.main()

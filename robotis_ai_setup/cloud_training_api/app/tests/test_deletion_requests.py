# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""GDPR Art. 17 deletion requests are carried out (launch plan step 5, F-25).

Owner decision: the TEACHER confirms. A student's ``POST /me/delete`` records the
request (the FIRST time is kept, Art. 12(3)'s one month runs from it); the
teacher of the student's classroom lists the open requests and confirms one,
which runs the existing full deletion; the admin sees every open request and
may confirm too; the requester may withdraw an open request. Every route is
IDOR-gated (Rule §4): the teacher's by ``_assert_student_owned`` first, the
lists by the JWT-verified caller, the withdrawal by the caller's own row.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.tests.test_dataset_identity_routes import _ensure_stubs

_ensure_stubs()

from fastapi import HTTPException  # noqa: E402

from app.routes import admin as adminroute  # noqa: E402
from app.routes import me as meroute  # noqa: E402
from app.routes import teacher as teacherroute  # noqa: E402
from app.services import deletion_requests as dr  # noqa: E402
from app.tests._managed_fakes import Clock, FakeDB  # noqa: E402

T1, T2 = "teacher-1", "teacher-2"
C1, C1B, C2 = "class-1", "class-1b", "class-2"


def _user(uid, role="student", classroom_id=None, requested=None, **over):
    row = {"id": uid, "email": f"{uid}@edubotics.local", "role": role, "username": uid,
           "full_name": uid.title(), "classroom_id": classroom_id, "workgroup_id": None, "created_by": None,
           "created_at": "2026-09-01T08:00:00+00:00", "deletion_requested_at": requested, "hf_username": None,
           "training_credits": 0, "vision_quota_per_term": None, "vision_used_per_term": 0}
    row.update(over)
    return row


def _db():
    db = FakeDB(Clock())
    db.tables["users"] = [
        _user(T1, role="teacher", full_name="Frau Lehrer"),
        _user(T2, role="teacher", full_name="Herr Anders"),
        _user("anna", classroom_id=C1, requested="2026-11-03T10:00:00+00:00"),
        _user("ben", classroom_id=C1B, requested="2026-10-31T09:00:00+00:00"),
        _user("cara", classroom_id=C1),                                           # no request
        _user("dora", classroom_id=C2, requested="2026-11-01T10:00:00+00:00"),   # another teacher's
        _user("emil", classroom_id=None, requested="2026-10-20T10:00:00+00:00"), # orphaned
        _user("t3", role="teacher", requested="2026-11-05T12:00:00+00:00", full_name="Gegangen"),
        _user("adm", role="admin"),
    ]
    db.tables["classrooms"] = [
        {"id": C1, "teacher_id": T1, "name": "7a", "created_at": "2026-09-01T00:00:00+00:00"},
        {"id": C1B, "teacher_id": T1, "name": "7b", "created_at": "2026-09-01T00:00:00+00:00"},
        {"id": C2, "teacher_id": T2, "name": "8c", "created_at": "2026-09-01T00:00:00+00:00"},
    ]
    deleted = []
    db.deleted_users = deleted
    db.auth = SimpleNamespace(admin=SimpleNamespace(delete_user=lambda uid: deleted.append(uid)))
    return db


class _Patched:
    def __init__(self, db):
        self.db = db
        self._cms = [patch.object(m, "get_supabase", lambda: db) for m in (teacherroute, adminroute, meroute)]

    def __enter__(self):
        for cm in self._cms:
            cm.__enter__()
        return self.db

    def __exit__(self, *exc):
        for cm in reversed(self._cms):
            cm.__exit__(*exc)


def _status(call, *a, **k):
    try:
        call(*a, **k)
    except HTTPException as exc:
        return exc.status_code, exc.detail
    return 200, None


class Deadline(unittest.TestCase):
    def test_one_calendar_month_clamped_to_the_month_end(self):
        def at(y, m, d):
            return datetime(y, m, d, 9, 30, tzinfo=timezone.utc)

        self.assertEqual(dr.one_month_after(at(2026, 11, 11)), at(2026, 12, 11))
        self.assertEqual(dr.one_month_after(at(2026, 12, 15)), at(2027, 1, 15))
        self.assertEqual(dr.one_month_after(at(2027, 1, 31)), at(2027, 2, 28))
        self.assertEqual(dr.one_month_after(at(2028, 1, 31)), at(2028, 2, 29))
        self.assertEqual(dr.one_month_after(at(2026, 3, 31)), at(2026, 4, 30))

    def test_postgrest_timestamps_parse_and_garbage_is_none(self):
        self.assertEqual(dr.due_by("2026-11-11T10:00:00.123456+00:00"), "2026-12-11T10:00:00.123456+00:00")
        self.assertEqual(dr.due_by("2026-11-11T10:00:00Z"), "2026-12-11T10:00:00+00:00")
        self.assertEqual(dr.due_by("2026-11-11T11:00:00+01:00"), "2026-12-11T10:00:00+00:00")
        for bad in (None, "", "gestern", 17, "2026-13-01T00:00:00+00:00"):
            self.assertIsNone(dr.due_by(bad), bad)

    def test_every_sentence_is_german_with_literal_umlauts(self):
        for name in ("NOT_OPEN_DE", "LIST_FAILED_DE", "WITHDRAW_FAILED_DE", "WITHDRAWN_DE",
                     "NOTHING_TO_WITHDRAW_DE", "ADMIN_CANNOT_REQUEST_DE"):
            text = getattr(dr, name)
            self.assertNotRegex(text, r"(?i)loesch|zurueck|koennen|muessen|ae\b", name)
        self.assertIn("zurückgezogen", dr.NOT_OPEN_DE)
        self.assertIn("Löschanfrage", dr.WITHDRAWN_DE)


class TeacherQueue(unittest.TestCase):
    def test_the_list_holds_only_open_requests_of_the_callers_own_classrooms_oldest_first(self):
        with _Patched(_db()):
            rows = teacherroute.list_deletion_requests(teacher={"id": T1})
        self.assertEqual([r.student_id for r in rows], ["ben", "anna"])
        ben = rows[0]
        self.assertEqual((ben.classroom_name, ben.requested_at), ("7b", "2026-10-31T09:00:00+00:00"))
        self.assertEqual(ben.due_by, "2026-11-30T09:00:00+00:00")
        with _Patched(_db()):
            self.assertEqual([r.student_id for r in teacherroute.list_deletion_requests(teacher={"id": T2})],
                             ["dora"])

    def test_a_teacher_without_classrooms_reads_no_user_row(self):
        db = _db()
        with _Patched(db):
            self.assertEqual(teacherroute.list_deletion_requests(teacher={"id": "t3"}), [])
        self.assertNotIn("users", [op[1] for op in db.ops])

    def test_the_list_takes_no_id_from_the_request(self):
        params = list(inspect.signature(teacherroute.list_deletion_requests).parameters)
        self.assertEqual(params, ["teacher"])
        params = list(inspect.signature(adminroute.list_deletion_requests).parameters)
        self.assertEqual(params, ["admin"])

    def test_a_read_failure_is_a_german_500(self):
        db = _db()
        db.failures["select:users"] = 1
        with _Patched(db):
            self.assertEqual(_status(teacherroute.list_deletion_requests, teacher={"id": T1}),
                             (500, dr.LIST_FAILED_DE))

    def test_the_roster_shows_an_open_request(self):
        summary = teacherroute._student_summary(_user("anna", classroom_id=C1, requested="2026-11-03T10:00:00+00:00"),
                                                account={}, group_name_lookup={})
        self.assertEqual(summary.deletion_requested_at, "2026-11-03T10:00:00+00:00")
        self.assertIsNone(teacherroute._student_summary(_user("cara", classroom_id=C1), account={},
                                                        group_name_lookup={}).deletion_requested_at)
        src = inspect.getsource(teacherroute.get_classroom)
        self.assertIn("deletion_requested_at", src)


class TeacherConfirm(unittest.TestCase):
    def _confirm(self, db, student_id, teacher_id=T1):
        erased = []
        with _Patched(db), patch.object(teacherroute, "erase_student_account",
                                        lambda sid: erased.append(sid) or {"ok": True, "hf_erasure_complete": True}):
            status = _status(teacherroute.confirm_deletion_request, student_id, teacher={"id": teacher_id})
        return status, erased

    def test_an_open_request_of_an_own_student_runs_the_full_deletion(self):
        status, erased = self._confirm(_db(), "anna")
        self.assertEqual(status, (200, None))
        self.assertEqual(erased, ["anna"])

    def test_another_teachers_student_is_a_404_and_nothing_is_erased(self):
        status, erased = self._confirm(_db(), "dora")
        self.assertEqual(status[0], 404)
        self.assertEqual(erased, [])

    def test_a_withdrawn_request_is_a_409_and_nothing_is_erased(self):
        status, erased = self._confirm(_db(), "cara")
        self.assertEqual(status, (409, dr.NOT_OPEN_DE))
        self.assertEqual(erased, [])

    def test_the_ownership_assert_is_the_first_statement(self):
        tree = ast.parse(inspect.getsource(teacherroute.confirm_deletion_request).lstrip())
        body = [s for s in tree.body[0].body
                if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        self.assertIn("_assert_student_owned", ast.unparse(body[0]))
        self.assertIn("open_deletion_request", ast.unparse(body[1]))

    def test_the_confirmation_is_the_existing_deletion_with_its_five_outcomes(self):
        # erase_student_account IS delete_student's body: the old route calls it after its assert.
        tree = ast.parse(inspect.getsource(teacherroute.delete_student).lstrip())
        body = tree.body[0].body
        self.assertEqual(len(body), 2)
        self.assertIn("_assert_student_owned", ast.unparse(body[0]))
        self.assertEqual(ast.unparse(body[1]), "return erase_student_account(student_id)")
        src = inspect.getsource(teacherroute.erase_student_account)
        for outcome in ("status_code=503", "status_code=500", '"hf_erasure_complete": False',
                        '"hf_erasure_complete": True', "hf_attempted == 0"):
            self.assertIn(outcome, src)

    def test_end_to_end_the_account_is_deleted(self):
        db = _db()
        db.tables["datasets"] = []
        db.tables["trainings"] = []
        with _Patched(db), patch.dict("os.environ", {"HF_TOKEN": ""}):
            res = teacherroute.confirm_deletion_request("anna", teacher={"id": T1})
        self.assertEqual(db.deleted_users, ["anna"])
        self.assertIs(res["hf_erasure_complete"], False)  # HF_TOKEN unset: honestly not complete


class AdminQueue(unittest.TestCase):
    def test_every_open_request_with_who_normally_confirms_it(self):
        with _Patched(_db()):
            rows = adminroute.list_deletion_requests(admin={"id": "adm"})
        self.assertEqual([r.user_id for r in rows], ["emil", "ben", "dora", "anna", "t3"])
        by = {r.user_id: r for r in rows}
        self.assertEqual((by["ben"].role, by["ben"].classroom_name, by["ben"].teacher_name), ("student", "7b", "Frau Lehrer"))
        self.assertEqual(by["dora"].teacher_name, "Herr Anders")
        self.assertEqual((by["emil"].classroom_name, by["emil"].teacher_name), (None, None))
        self.assertEqual((by["t3"].role, by["t3"].classroom_name), ("teacher", None))
        self.assertEqual(by["emil"].due_by, "2026-11-20T10:00:00+00:00")

    def _confirm(self, db, user_id):
        erased = []
        with _Patched(db), patch.object(adminroute, "erase_student_account",
                                        lambda sid: erased.append(sid) or {"ok": True, "hf_erasure_complete": True}):
            status = _status(adminroute.confirm_deletion_request, user_id, admin={"id": "adm"})
        return status, erased

    def test_a_students_request_runs_the_teachers_full_deletion(self):
        for uid in ("emil", "dora"):
            with self.subTest(uid=uid):
                status, erased = self._confirm(_db(), uid)
                self.assertEqual(status, (200, None))
                self.assertEqual(erased, [uid])

    def test_a_teachers_request_uses_the_teacher_rule(self):
        db = _db()
        status, erased = self._confirm(db, "t3")
        self.assertEqual(status, (200, None))
        self.assertEqual((erased, db.deleted_users), ([], ["t3"]))
        # a teacher who still has classrooms: 409, nothing deleted
        db = _db()
        db.tables["users"][0]["deletion_requested_at"] = "2026-11-05T12:00:00+00:00"
        status, erased = self._confirm(db, T1)
        self.assertEqual(status[0], 409)
        self.assertEqual(db.deleted_users, [])

    def test_nobody_without_an_open_request_is_deleted(self):
        for uid, code in (("cara", 409), ("adm", 404), ("nobody", 404)):
            with self.subTest(uid=uid):
                db = _db()
                status, erased = self._confirm(db, uid)
                self.assertEqual(status[0], code)
                self.assertEqual((erased, db.deleted_users), ([], []))
        self.assertEqual(self._confirm(_db(), "cara")[0], (409, dr.NOT_OPEN_DE))

    def test_the_admin_delete_teacher_route_still_refuses_a_non_teacher(self):
        with _Patched(_db()):
            self.assertEqual(_status(adminroute.delete_teacher, "anna", admin={"id": "adm"})[0], 404)


class Requester(unittest.IsolatedAsyncioTestCase):
    def _profile(self, db, uid):
        return dict(next(r for r in db.tables["users"] if r["id"] == uid))

    async def test_asking_again_keeps_the_first_request_time(self):
        db = _db()
        db.tables["user_hf_credentials"] = []
        db.tables["workgroup_memberships"] = []
        with _Patched(db):
            res = await meroute.delete_my_account(profile=self._profile(db, "anna"))
        self.assertEqual(res["requested_at"], "2026-11-03T10:00:00+00:00")
        self.assertEqual(res["due_by"], "2026-12-03T10:00:00+00:00")
        self.assertIn("03.12.2026", res["message"])
        self.assertEqual(self._profile(db, "anna")["deletion_requested_at"], "2026-11-03T10:00:00+00:00")
        self.assertIn("zurückziehen", res["message"])

    async def test_a_first_request_records_now(self):
        db = _db()
        db.tables["user_hf_credentials"] = []
        with _Patched(db):
            res = await meroute.delete_my_account(profile=self._profile(db, "cara"))
        stored = self._profile(db, "cara")["deletion_requested_at"]
        self.assertTrue(stored)
        self.assertEqual(res["requested_at"], stored)
        self.assertEqual(res["due_by"], dr.due_by(stored))
        self.assertIs(res["deletion_performed"], False)

    def test_withdrawing_clears_only_the_callers_own_request(self):
        db = _db()
        with _Patched(db):
            res = meroute.withdraw_deletion_request(profile=self._profile(db, "anna"))
            again = meroute.withdraw_deletion_request(profile=self._profile(db, "anna"))
        self.assertEqual((res["withdrawn"], res["message"]), (True, dr.WITHDRAWN_DE))
        self.assertEqual((again["withdrawn"], again["message"]), (False, dr.NOTHING_TO_WITHDRAW_DE))
        self.assertIsNone(self._profile(db, "anna")["deletion_requested_at"])
        for other in ("ben", "dora", "emil", "t3"):
            self.assertIsNotNone(self._profile(db, other)["deletion_requested_at"], other)
        (update,) = [op for op in db.ops if op[0] == "update" and op[1] == "users"][:1]
        self.assertIn(("eq", "id", "anna", False), update[2])
        self.assertEqual(update[3], {"deletion_requested_at": None})

    def test_the_withdrawal_takes_no_id_and_refuses_an_admin(self):
        self.assertEqual(list(inspect.signature(meroute.withdraw_deletion_request).parameters), ["profile"])
        db = _db()
        with _Patched(db):
            self.assertEqual(_status(meroute.withdraw_deletion_request, profile=self._profile(db, "adm")),
                             (400, dr.ADMIN_CANNOT_REQUEST_DE))

    def test_a_failed_withdrawal_is_a_german_500(self):
        db = _db()
        db.failures["update:users"] = 1
        with _Patched(db):
            self.assertEqual(_status(meroute.withdraw_deletion_request, profile=self._profile(db, "anna")),
                             (500, dr.WITHDRAW_FAILED_DE))

    def test_get_me_shows_an_open_request_and_its_deadline(self):
        with _Patched(_db()):
            me = meroute._build_my_profile({"id": "x", "role": "admin", "username": "x", "full_name": "X",
                                            "deletion_requested_at": "2026-11-03T10:00:00+00:00"})
        self.assertEqual(me.deletion_requested_at, "2026-11-03T10:00:00+00:00")
        self.assertEqual(me.deletion_due_by, "2026-12-03T10:00:00+00:00")
        # the profile every route reads carries the column (app/auth.py may be stubbed here: read the file)
        import os

        auth_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "auth.py")
        with open(auth_py, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        node = next(n for n in tree.body if isinstance(n, ast.Assign)
                    and getattr(n.targets[0], "id", None) == "_PROFILE_COLUMNS")
        columns = [c.strip() for c in ast.literal_eval(node.value).split(",")]
        self.assertIn("deletion_requested_at", columns)
        self.assertNotIn("*", columns)


if __name__ == "__main__":
    unittest.main()

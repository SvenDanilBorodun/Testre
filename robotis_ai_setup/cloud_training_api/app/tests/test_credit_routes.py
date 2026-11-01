"""The training-hour routes of migration 052 (spec §8.2–§8.7, §12.3).

Driven directly (fastapi stubbed, house pattern) against ``CreditDB``, the managed
FakeDB plus the tables and the hour functions the teacher, workgroup and admin routes
read (``credit_assign`` / ``credit_reclaim`` / ``credit_teacher_summary`` /
``credit_set_member_limit`` / ``credit_admin_*`` / ``credit_erase_reason``, answering
in 052's shapes: a table for the first three, a scalar for the rest).

Pinned: the start's order (role → dispatch → policy → key replay → quota, failing
fast BEFORE the dataset check) and every ``start_training_safe`` P-code with its
refusal record; the response keys; the quota shape with the legacy keys and the
``member_left`` floor; „Abbrechen" through each ``stop_training`` outcome; a teacher's
assign / reclaim / limit / stop / preview / history behind the German 404 for a foreign
student and for another student's training; the admin's grant / revoke and its
compensation (every validation sentence in its order, each P-code, the reason in the
teacher's history); the removed PATCH; a non-admin's 403; the teacher summary's
``compensated``; the per-user rate key for ``/teacher/`` and ``/admin/``. The GDPR
export's keys are pinned in test_me_export.py.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import uuid
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.tests.test_managed_start import USER, StartBase, request  # noqa: F401  (stubs installed there)

from fastapi import HTTPException  # noqa: E402
from app.routes import admin as admin_route  # noqa: E402
from app.routes import me as me_route  # noqa: E402
from app.routes import teacher as teacher_route  # noqa: E402
from app.routes import training as route  # noqa: E402
from app.routes import workgroups as wg_route  # noqa: E402
from app.services import credit_texts_de as C  # noqa: E402
from app.services import hf_publish, stop_actions, training_dispatch as D  # noqa: E402
from app.services import training_texts_de as T  # noqa: E402
from app.tests._managed_fakes import Clock, FakeDB, FakeModal, FakeRunpod, PgError  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)

TEACHER = "11111111-1111-4111-8111-111111111111"
OTHER_TEACHER = "22222222-2222-4222-8222-222222222222"
ADMIN = "33333333-3333-4333-8333-333333333333"
STUDENT = "44444444-4444-4444-8444-444444444444"
FOREIGN_STUDENT = "55555555-5555-4555-8555-555555555555"
GROUP = "66666666-6666-4666-8666-666666666666"
FOREIGN_GROUP = "77777777-7777-4777-8777-777777777777"
REQ = "88888888-8888-4888-8888-888888888888"


def dump(obj):
    return obj.model_dump() if hasattr(obj, "model_dump") else dict(obj)


def err(code, details=None):
    e = PgError(code)
    if details is not None:
        e.details = details
    return e


class CreditDB(FakeDB):
    """FakeDB + users / classrooms / workgroups / credit_accounts / credit_ledger and the
    hour functions the teacher, workgroup and admin routes call."""

    def __init__(self, clock):
        super().__init__(clock)
        for t in ("users", "classrooms", "workgroups", "credit_accounts", "credit_ledger"):
            self.tables[t] = []
        self.summary: dict = {}       # teacher id -> credit_teacher_summary's o_* row
        self.pool: dict = {}          # teacher id -> free pool hours
        self.erased: set = set()

    def user(self, uid, role="student", classroom_id=None, workgroup_id=None, **kw):
        row = {"id": uid, "username": f"u{uid[:4]}", "full_name": f"Name {uid[:4]}", "role": role,
               "classroom_id": classroom_id, "workgroup_id": workgroup_id, **kw}
        self.tables["users"].append(row)
        self.roles[uid] = role
        return row

    def account(self, kind, owner, free=0, reserved=0, spent=0, limit=None, parent=TEACHER):
        row = {"kind": kind, "owner_id": owner, "parent_teacher_id": parent, "free": free, "reserved": reserved,
               "spent": spent, "member_hour_limit": limit, "closed_at": None}
        self.tables["credit_accounts"].append(row)
        return row

    def acct(self, kind, owner):
        return next((a for a in self.tables["credit_accounts"] if a["kind"] == kind and a["owner_id"] == owner), None)

    def _move(self, p_teacher_id, p_target_kind, p_target_id, p_hours, sign):
        a = self.acct(p_target_kind, p_target_id) or self.account(p_target_kind, p_target_id)
        a["free"] += sign * p_hours
        self.pool[p_teacher_id] = self.pool.get(p_teacher_id, 50) - sign * p_hours
        return [{"o_target_free": a["free"], "o_teacher_free": self.pool[p_teacher_id]}]

    def _rpc_credit_assign(self, p_teacher_id, p_target_kind, p_target_id, p_hours, p_request_id):
        return self._move(p_teacher_id, p_target_kind, p_target_id, p_hours, +1)

    def _rpc_credit_reclaim(self, p_teacher_id, p_target_kind, p_target_id, p_hours, p_request_id):
        return self._move(p_teacher_id, p_target_kind, p_target_id, p_hours, -1)

    def _rpc_credit_teacher_summary(self, p_teacher_id):
        base = {"o_free": 0, "o_assigned_free": 0, "o_assigned_reserved": 0, "o_spent": 0, "o_compensated": 0,
                "o_student_count": 0, "o_group_count": 0, "o_group_hours": 0}
        return [dict(base, **self.summary.get(p_teacher_id, {}))]

    def _rpc_credit_set_member_limit(self, p_teacher_id, p_group_id, p_hours):
        return p_hours                                       # RETURNS INT

    def _rpc_credit_admin_grant(self, p_admin_id, p_teacher_id, p_hours, p_reason, p_request_id):
        self.pool[p_teacher_id] = self.pool.get(p_teacher_id, 0) + p_hours
        return self.pool[p_teacher_id]                       # RETURNS INT

    def _rpc_credit_admin_revoke(self, p_admin_id, p_teacher_id, p_hours, p_reason, p_request_id):
        self.pool[p_teacher_id] = self.pool.get(p_teacher_id, 0) - p_hours
        return self.pool[p_teacher_id]

    def _rpc_credit_admin_compensate(self, p_admin_id, p_target_kind, p_target_id, p_hours, p_reason,
                                     p_training_id=None, p_request_id=None):
        a = self.acct(p_target_kind, p_target_id) or self.account(p_target_kind, p_target_id)
        a["free"] += p_hours
        return a["free"]

    def _rpc_credit_erase_reason(self, p_admin_id, p_reason_id):
        new = p_reason_id not in self.erased
        self.erased.add(p_reason_id)
        return new                                           # RETURNS BOOLEAN


def ledger_row(i, kind, hours, *, frm=(None, None), to=(None, None), **kw):
    row = {"id": i, "at": f"2026-10-{i:02d}T10:00:00+00:00", "kind": kind, "hours": hours,
           "from_kind": frm[0], "from_id": frm[1], "from_bucket": None, "to_kind": to[0], "to_id": to[1],
           "to_bucket": None, "training_id": None, "member_id": None, "actor_id": TEACHER, "actor_role": "teacher",
           "reason_id": None, "reason": None, "meta": {}}
    row.update(kw)
    return row


class RouteBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        D.reset_state_for_tests()
        self.clock = Clock()
        self.db = CreditDB(self.clock)
        self.rp = FakeRunpod()
        self.modal = FakeModal()
        self.published = []
        self.db.tables["classrooms"] += [{"id": "c1", "teacher_id": TEACHER, "name": "7a"},
                                         {"id": "c2", "teacher_id": OTHER_TEACHER, "name": "7b"}]
        self.db.tables["workgroups"] += [{"id": GROUP, "classroom_id": "c1", "name": "Rot"},
                                         {"id": FOREIGN_GROUP, "classroom_id": "c2", "name": "Blau"}]
        self.db.user(TEACHER, role="teacher")
        self.db.user(OTHER_TEACHER, role="teacher")
        self.db.user(ADMIN, role="admin")
        self.db.user(STUDENT, classroom_id="c1")
        self.db.user(FOREIGN_STUDENT, classroom_id="c2")
        sb = lambda: self.db  # noqa: E731
        self._cms = [patch.object(m, "get_supabase", sb) for m in (teacher_route, wg_route, admin_route, route, me_route)]
        self._cms += [patch.object(stop_actions, "_sb", sb), patch.object(D, "_sb", sb), patch.object(D, "_now", self.clock),
                      patch.object(hf_publish, "schedule_publish", lambda t, n: self.published.append((t, n)) or True),
                      self.rp.installed(), self.modal.installed()]
        for cm in self._cms:
            cm.__enter__()

    async def asyncTearDown(self):
        for cm in reversed(self._cms):
            cm.__exit__(None, None, None)

    async def refused(self, coro):
        with self.assertRaises(HTTPException) as ctx:
            await coro
        return ctx.exception.status_code, ctx.exception.detail


T_ = {"id": TEACHER, "role": "teacher"}
A_ = {"id": ADMIN, "role": "admin"}


# ---------------------------------------------------------------------------
# POST /trainings/start (§8.2) and GET /trainings/quota
# ---------------------------------------------------------------------------


class Start(StartBase):
    def keyed(self, key=REQ, **params):
        req = request(**params)
        req.idempotency_key = uuid.UUID(key)
        return req

    async def refused(self, req=None):
        with self.assertRaises(HTTPException) as ctx:
            await self.start(req)
        return ctx.exception.status_code, ctx.exception.detail

    async def test_a_teacher_is_refused_first_and_recorded(self):
        self.db.roles["u-1"] = "teacher"
        self.assertEqual(await self.refused(), (403, C.ROLE_REFUSED_DE))
        self.assertEqual([(r["code"], r["request_kind"]) for r in self.db.refusals], [("role", "start")])
        self.assertEqual((self.preflight_calls, self.db.calls("start_training_safe")), ([], []))

    async def test_the_response_keys(self):
        resp = dump(await self.start())
        self.assertEqual(set(resp), {"training_id", "model_name", "status", "reserved_hours", "replay"})
        self.assertEqual((resp["status"], resp["reserved_hours"], resp["replay"]), ("queued", 3, False))

    async def test_the_key_replays_the_same_start_and_refuses_another(self):
        first = dump(await self.start(self.keyed()))
        again = dump(await self.start(self.keyed()))
        self.assertEqual((again["training_id"], again["replay"], again["reserved_hours"]),
                         (first["training_id"], True, 3))
        self.assertEqual(len(self.db.calls("start_training_safe")), 1, "a replay reserves nothing")
        self.assertEqual(await self.refused(self.keyed(steps=10000)), (409, C.REQUEST_REUSED_DE))
        self.assertEqual(self.db.refusals, [], "a reused key is not a refused start")

    async def test_no_hours_fail_fast_before_the_dataset_check(self):
        self.db.hours[("student", "u-1")] = 0
        self.assertEqual(await self.refused(), (403, C.NO_HOURS_DE))
        self.assertEqual((self.preflight_calls, self.resolve_calls), ([], []), "no 60 s Hub check for a refusal")
        self.assertEqual([r["code"] for r in self.db.refusals], ["no_hours"])

    async def test_a_used_up_member_limit_names_the_limit(self):
        self.db.workgroup_of["u-1"] = "g1"
        self.db.member_limit["g1"], self.db.member_used[("g1", "u-1")] = 3, 3
        self.assertEqual(await self.refused(), (403, C.MEMBER_LIMIT_DE))
        self.assertEqual([r["code"] for r in self.db.refusals], ["member_limit"])
        self.assertEqual(self.preflight_calls, [])

    async def test_every_p_code_of_start_training_safe(self):
        cases = {"P0003": (403, C.NO_HOURS_DE, "no_hours"), "P0063": (403, C.MEMBER_LIMIT_DE, "member_limit"),
                 "P0060": (403, C.ROLE_REFUSED_DE, "role"), "P0061": (400, C.POLICY_REFUSED_DE, "policy"),
                 "P0071": (409, C.REQUEST_REUSED_DE, None), "P0002": (404, "Benutzerprofil nicht gefunden.", None)}
        for code, (status, detail, recorded) in cases.items():
            with self.subTest(code=code):
                self.db.refusals.clear()
                self.db.errors["start_training_safe"] = [PgError(code)]
                self.assertEqual(await self.refused(request(steps=40000 + len(self.db.rpcs))), (status, detail))
                self.assertEqual([r["code"] for r in self.db.refusals], [recorded] if recorded else [])
                self.assertTrue(all(r["request_kind"] == "start" for r in self.db.refusals))

    async def test_the_call_carries_the_key_and_the_deploys_ceiling(self):
        await self.start(self.keyed())
        (call,) = self.db.calls("start_training_safe")
        self.assertEqual((call["p_idempotency_key"], call["p_max_hours"], call["p_managed"]), (REQ, 3, True))

    async def test_a_failing_refusal_record_never_blocks_the_answer(self):
        self.db.roles["u-1"] = "admin"
        self.db.errors["record_training_refusal"] = [RuntimeError("down")]
        self.assertEqual(await self.refused(), (403, C.ROLE_REFUSED_DE))


class Quota(StartBase):
    async def quota(self):
        return dump(await route.get_quota(user=USER))

    async def test_the_shape_with_the_legacy_keys(self):
        self.db.hours[("student", "u-1")] = 7
        self.db.reserved[("student", "u-1")] = 3
        self.db.spent[("student", "u-1")] = 2
        q = await self.quota()
        self.assertEqual(set(q), {"account", "workgroup_name", "hours_free", "hours_reserved", "hours_spent",
                                  "member_limit_hours", "member_used_hours", "member_left_hours",
                                  "next_reservation_hours", "max_hours_per_training", "training_credits",
                                  "trainings_used", "remaining"})
        self.assertEqual((q["account"], q["hours_free"], q["hours_reserved"], q["hours_spent"]), ("student", 7, 3, 2))
        self.assertEqual((q["next_reservation_hours"], q["max_hours_per_training"]), (3, 3))
        self.assertEqual((q["training_credits"], q["trainings_used"], q["remaining"]), (12, 5, 7))
        self.assertIsNone(q["member_left_hours"])

    async def test_member_left_is_floored_at_zero_and_caps_the_legacy_keys(self):
        # review 3 m12: a limit set below what the member already used never reads negative
        self.db.workgroup_of["u-1"] = "g1"
        self.db.tables["workgroups"] = [{"id": "g1", "name": "Rot"}]
        self.db.hours[("group", "g1")] = 20
        self.db.member_limit["g1"], self.db.member_used[("g1", "u-1")] = 2, 5
        q = await self.quota()
        self.assertEqual((q["account"], q["workgroup_name"], q["member_limit_hours"], q["member_used_hours"],
                          q["member_left_hours"]), ("group", "Rot", 2, 5, 0))
        self.assertEqual((q["remaining"], q["next_reservation_hours"]), (0, 0))

    async def test_a_teacher_reads_zero_and_cannot_start(self):
        self.db.roles["u-1"] = "teacher"
        q = await self.quota()
        self.assertEqual((q["account"], q["hours_free"], q["next_reservation_hours"], q["remaining"]),
                         (None, 0, 0, 0))


# ---------------------------------------------------------------------------
# „Abbrechen" through each stop_training outcome (§8.4)
# ---------------------------------------------------------------------------


class StopOutcomes(RouteBase):
    def running(self, owner=STUDENT, **kw):
        row = self.db.add_training(user_id=owner, status="running", current_attempt=1, credit_scheme="ledger",
                                   reserved_hours=3, worker_token="tok", **kw)
        self.db.add_attempt(row["id"], 1, state="running", backend="runpod_secure", cloud_ref=f"pod{row['id']}",
                            first_heartbeat_at=self.clock.iso(self.clock.t - 1200))
        self.rp.add_pod(id=f"pod{row['id']}", status="RUNNING")
        return row

    async def cancel(self, tid, user=STUDENT):
        return await route.cancel_training(SimpleNamespace(training_id=tid), user=SimpleNamespace(id=user))

    async def test_publish_with_a_ready_checkpoint(self):
        row = self.running()
        self.db.add_checkpoint(row["id"], 1, 10000, state="ready", manifest={})
        out = await self.cancel(row["id"])
        self.assertEqual(out, {"status": "running", "training_id": row["id"], "outcome": "publishing",
                               "checkpoint_step": 10000, "charged_hours": None})
        a = self.db.attempt(row["id"], 1)
        self.assertEqual((a["state"], a["end_reason"]), ("ended", "stopped"))
        self.assertEqual(self.published, [(row["id"], 1)])
        self.assertIn(f"pod{row['id']}", self.rp.deletes())

    async def test_publish_final_with_a_ready_final(self):
        row = self.running()
        self.db.add_checkpoint(row["id"], 1, 50000, state="ready", manifest={})
        out = await self.cancel(row["id"])
        self.assertEqual((out["outcome"], out["checkpoint_step"]), ("publishing", 50000))
        self.assertEqual(self.db.attempt(row["id"], 1)["end_reason"], "succeeded")

    async def test_cancel_without_a_checkpoint_charges_the_started_hour(self):
        row = self.running()
        out = await self.cancel(row["id"])
        self.assertEqual((out["status"], out["outcome"], out["charged_hours"]), ("canceled", "canceled", 1))
        self.assertEqual(self.published, [])

    async def test_the_three_refusals(self):
        self.assertEqual(await self.refused(self.cancel(999)), (404, T.TRAINING_NOT_FOUND_DE))
        row = self.running()
        self.assertEqual(await self.refused(self.cancel(row["id"], user=FOREIGN_STUDENT)),
                         (404, T.TRAINING_NOT_FOUND_DE))
        done = self.db.add_training(user_id=STUDENT, status="succeeded")
        self.assertEqual(await self.refused(self.cancel(done["id"])), (400, T.TRAINING_NOT_ACTIVE_DE))
        self.db.attempt(row["id"], 1).update(state="ended", end_reason="capped")
        self.assertEqual(await self.refused(self.cancel(row["id"])), (409, T.S9))


# ---------------------------------------------------------------------------
# Teacher: assign / reclaim / history / stop / preview (§8.3)
# ---------------------------------------------------------------------------


class TeacherHours(RouteBase):
    async def change(self, delta, sid=STUDENT, **body):
        resp = await teacher_route.adjust_credits(sid, body={"delta": delta, **body}, teacher=T_)
        return dump(resp)

    async def test_assign_and_reclaim(self):
        self.db.account("student", STUDENT, free=2, reserved=3, spent=1)
        out = await self.change(5, request_id=REQ)
        (call,) = self.db.calls("credit_assign")
        self.assertEqual(call, {"p_teacher_id": TEACHER, "p_target_kind": "student", "p_target_id": STUDENT,
                                "p_hours": 5, "p_request_id": REQ})
        self.assertEqual(out, {"new_amount": 7, "pool_available": 45, "hours_free": 7, "hours_reserved": 3,
                               "hours_spent": 1})
        out = await self.change(-4)
        (call,) = self.db.calls("credit_reclaim")
        self.assertEqual((call["p_hours"], call["p_request_id"]), (4, None))
        self.assertEqual((out["new_amount"], out["pool_available"]), (3, 49))

    async def test_validation_comes_before_any_call(self):
        cases = [({"delta": 0}, (400, C.DELTA_ZERO_DE)), ({"delta": 1001}, (400, C.TEACHER_DELTA_RANGE_DE)),
                 ({"delta": -1001}, (400, C.TEACHER_DELTA_RANGE_DE)), ({"delta": "5"}, (400, C.TEACHER_DELTA_RANGE_DE)),
                 ({"delta": True}, (400, C.TEACHER_DELTA_RANGE_DE)), ({"delta": 2.0}, (400, C.TEACHER_DELTA_RANGE_DE)),
                 ({"delta": 3, "request_id": "nope"}, (400, C.CREDIT_CHANGE_FAILED_DE))]
        for body, want in cases:
            with self.subTest(body=body):
                self.assertEqual(await self.refused(teacher_route.adjust_credits(STUDENT, body=body, teacher=T_)), want)
        self.assertEqual(await self.refused(teacher_route.adjust_credits(STUDENT, body=None, teacher=T_)),
                         (400, C.CREDIT_CHANGE_FAILED_DE))
        self.assertEqual(self.db.calls("credit_assign") + self.db.calls("credit_reclaim"), [])

    async def test_every_p_code(self):
        cases = {"P0065": (409, C.POOL_TOO_SMALL_DE), "P0064": (409, C.RECLAIM_TOO_MUCH_DE),
                 "P0023": (409, C.GROUPED_STUDENT_DE), "P0071": (409, C.REQUEST_REUSED_DE),
                 "P0066": (404, C.STUDENT_NOT_FOUND_DE), "P0002": (404, C.STUDENT_NOT_FOUND_DE),
                 "XX000": (500, C.CREDIT_CHANGE_FAILED_DE)}
        for code, want in cases.items():
            with self.subTest(code=code):
                self.db.errors["credit_assign"] = [PgError(code)]
                self.assertEqual(await self.refused(teacher_route.adjust_credits(STUDENT, body={"delta": 1},
                                                                                 teacher=T_)), want)

    async def test_a_foreign_student_is_the_german_404_everywhere(self):
        for coro in (teacher_route.adjust_credits(FOREIGN_STUDENT, body={"delta": 1}, teacher=T_),
                     teacher_route.student_credit_history(FOREIGN_STUDENT, before=None, limit=None, teacher=T_),
                     teacher_route.stop_student_training(FOREIGN_STUDENT, 1, teacher=T_),
                     teacher_route.student_training_stop_preview(FOREIGN_STUDENT, 1, teacher=T_)):
            self.assertEqual(await self.refused(coro), (404, "Klassenzimmer nicht gefunden"))
        self.assertEqual(await self.refused(teacher_route.adjust_credits("nobody", body={"delta": 1}, teacher=T_)),
                         (404, C.STUDENT_NOT_FOUND_DE))
        self.assertEqual([n for n, _ in self.db.rpcs], [], "no RPC before the assert")


class TeacherHistory(RouteBase):
    async def test_entries_and_refusals(self):
        t = self.db.add_training(user_id=STUDENT, status="succeeded", model_name="EduBotics-Solutions/m1")
        self.db.ledger += [
            ledger_row(1, "assign", 5, frm=("teacher", TEACHER), to=("student", STUDENT)),
            ledger_row(2, "charge", 2, frm=("student", STUDENT), training_id=t["id"], actor_role="system",
                       meta={"stopped_by": "student", "status": "canceled", "lost_seconds": 120,
                             "losses": ["pod_gone"], "waiver": None}),
            ledger_row(3, "compensate", 4, to=("student", STUDENT), actor_id=ADMIN, actor_role="admin",
                       reason_id=9, reason="GPU-Ausfall am Dienstag"),
            ledger_row(4, "compensate", 1, to=("student", STUDENT), actor_role="admin", reason_id=10, reason=None),
            ledger_row(5, "assign", 9, frm=("teacher", TEACHER), to=("student", FOREIGN_STUDENT)),
        ]
        self.db.tables["training_start_refusals"].append(
            {"id": 1, "at": "t", "user_id": STUDENT, "role": "student", "request_kind": "start", "code": "no_hours",
             "model_type": "act"})
        out = await teacher_route.student_credit_history(STUDENT, before=None, limit=None, teacher=T_)
        self.assertEqual(set(out), {"entries", "refusals"})
        self.assertEqual([e["id"] for e in out["entries"]], [4, 3, 2, 1], "newest first, only this student")
        by_id = {e["id"]: e for e in out["entries"]}
        self.assertEqual(set(by_id[1]), {"id", "at", "kind", "hours", "account", "training_id", "model_name", "actor",
                                         "reason", "reason_erased", "stopped_by", "status", "lost_seconds", "losses",
                                         "waiver", "limit_old", "limit_new"})
        self.assertEqual((by_id[2]["model_name"], by_id[2]["stopped_by"], by_id[2]["lost_seconds"],
                          by_id[2]["losses"]), ("EduBotics-Solutions/m1", "student", 120, ["pod_gone"]))
        # the admin's reason reaches the teacher; an erased one reads erased
        self.assertEqual((by_id[3]["reason"], by_id[3]["reason_erased"], by_id[3]["actor"]),
                         ("GPU-Ausfall am Dienstag", False, "admin"))
        self.assertEqual((by_id[4]["reason"], by_id[4]["reason_erased"]), (None, True))
        self.assertEqual(out["refusals"], [{"at": "t", "request_kind": "start", "code": "no_hours", "model_type": "act"}])
        text = repr(out)
        for foreign in (ADMIN, FOREIGN_STUDENT):
            self.assertNotIn(foreign, text, "a teacher's history names no other user's id")
        (call,) = self.db.calls("credit_history")
        self.assertEqual((call["p_scope"], call["p_id"], call["p_limit"]), ("student", STUDENT, 100))

    async def test_paging_and_the_own_pool(self):
        self.db.ledger += [ledger_row(i, "grant", 10, to=("teacher", TEACHER), actor_role="admin") for i in (1, 2, 3)]
        out = await teacher_route.own_credit_history(before=3, limit=500, teacher=T_)
        self.assertEqual([e["id"] for e in out["entries"]], [2, 1])
        (call,) = self.db.calls("credit_history")
        self.assertEqual((call["p_scope"], call["p_id"], call["p_before"], call["p_limit"]), ("teacher", TEACHER, 3, 200))
        self.assertEqual(out["refusals"], [], "refusals only for a student's history")
        self.assertEqual(await self.refused(teacher_route.own_credit_history(before="x", limit=None, teacher=T_)),
                         (400, C.HISTORY_FAILED_DE))
        self.db.errors["credit_history"] = [RuntimeError("down")]
        self.assertEqual(await self.refused(teacher_route.own_credit_history(before=None, limit=None, teacher=T_)),
                         (500, C.HISTORY_FAILED_DE))


class TeacherStop(RouteBase):
    def running(self, owner):
        row = self.db.add_training(user_id=owner, status="running", current_attempt=1, credit_scheme="ledger",
                                   reserved_hours=2)
        self.db.add_attempt(row["id"], 1, state="running", backend="runpod_secure", cloud_ref=f"p{row['id']}",
                            first_heartbeat_at=self.clock.iso(self.clock.t - 600))
        return row

    async def test_the_teachers_stop_is_the_students_path_with_the_teacher_role(self):
        row = self.running(STUDENT)
        self.db.add_checkpoint(row["id"], 1, 2500, state="ready", manifest={})
        out = await teacher_route.stop_student_training(STUDENT, row["id"], teacher=T_)
        self.assertEqual((out["outcome"], out["checkpoint_step"]), ("publishing", 2500))
        (call,) = self.db.calls("stop_training")
        self.assertEqual(call, {"p_training_id": row["id"], "p_user_id": STUDENT, "p_actor_id": TEACHER,
                                "p_actor_role": "teacher", "p_note": T.S32})
        self.assertEqual(self.db.row(row["id"])["stop_requested_by_role"], "teacher")

    async def test_another_students_training_of_an_owned_student_is_404(self):
        row = self.running(FOREIGN_STUDENT)
        self.assertEqual(await self.refused(teacher_route.stop_student_training(STUDENT, row["id"], teacher=T_)),
                         (404, T.TRAINING_NOT_FOUND_DE))
        self.assertEqual(self.db.row(row["id"])["status"], "running")
        self.assertEqual(await self.refused(teacher_route.student_training_stop_preview(STUDENT, row["id"],
                                                                                        teacher=T_)),
                         (404, T.TRAINING_NOT_FOUND_DE))

    async def test_the_preview(self):
        row = self.running(STUDENT)
        out = await teacher_route.student_training_stop_preview(STUDENT, row["id"], teacher=T_)
        self.assertEqual(out, {"training_id": row["id"], "trained": True, "checkpoint_step": None,
                               "next_checkpoint_step": 2500, "final_ready": False, "reserved_hours": 2,
                               "charged_hours_now": 1, "hours_back_now": 1, "outcome": "cancel"})
        self.db.add_checkpoint(row["id"], 1, 2500, state="ready", manifest={})
        out = await teacher_route.student_training_stop_preview(STUDENT, row["id"], teacher=T_)
        self.assertEqual((out["outcome"], out["checkpoint_step"]), ("publish", 2500))
        self.assertEqual(self.db.calls("stop_training"), [], "a preview changes nothing")


# ---------------------------------------------------------------------------
# Workgroups: hours, member limit, history
# ---------------------------------------------------------------------------


class WorkgroupHours(RouteBase):
    async def test_assign_to_a_group_and_the_foreign_404(self):
        out = dump(await wg_route.adjust_workgroup_credits(GROUP, body={"delta": 6}, teacher=T_))
        (call,) = self.db.calls("credit_assign")
        self.assertEqual((call["p_target_kind"], call["p_target_id"], call["p_hours"]), ("group", GROUP, 6))
        self.assertEqual(out["new_amount"], 6)
        self.assertEqual(await self.refused(wg_route.adjust_workgroup_credits(FOREIGN_GROUP, body={"delta": 6},
                                                                              teacher=T_)),
                         (404, "Arbeitsgruppe nicht gefunden"))
        self.db.errors["credit_reclaim"] = [PgError("P0066")]
        self.assertEqual(await self.refused(wg_route.adjust_workgroup_credits(GROUP, body={"delta": -1}, teacher=T_)),
                         (404, C.WORKGROUP_NOT_FOUND_DE))

    async def test_the_member_limit(self):
        self.assertEqual(await wg_route.set_member_limit(GROUP, body={"hours": 4}, teacher=T_),
                         {"member_limit_hours": 4})
        self.assertEqual(await wg_route.set_member_limit(GROUP, body={"hours": None}, teacher=T_),
                         {"member_limit_hours": None})
        self.assertEqual([c["p_hours"] for c in self.db.calls("credit_set_member_limit")], [4, None])
        for body in ({"hours": 0}, {"hours": 1001}, {"hours": "4"}, {"hours": True}, {}, None):
            with self.subTest(body=body):
                self.assertEqual(await self.refused(wg_route.set_member_limit(GROUP, body=body, teacher=T_)),
                                 (400, C.LIMIT_RANGE_DE))
        for code, want in (("22023", (400, C.LIMIT_RANGE_DE)), ("P0066", (404, C.WORKGROUP_NOT_FOUND_DE)),
                           ("XX000", (500, C.CREDIT_CHANGE_FAILED_DE))):
            self.db.errors["credit_set_member_limit"] = [PgError(code)]
            self.assertEqual(await self.refused(wg_route.set_member_limit(GROUP, body={"hours": 2}, teacher=T_)), want)
        self.assertEqual(await self.refused(wg_route.set_member_limit(FOREIGN_GROUP, body={"hours": 2}, teacher=T_)),
                         (404, "Arbeitsgruppe nicht gefunden"))

    async def test_the_group_history_carries_limit_changes(self):
        self.db.ledger.append(ledger_row(1, "limit_change", 0, to=("group", GROUP), meta={"old": None, "new": 4}))
        out = await wg_route.workgroup_credit_history(GROUP, before=None, limit=None, teacher=T_)
        (e,) = out["entries"]
        self.assertEqual((e["kind"], e["limit_old"], e["limit_new"], e["account"]), ("limit_change", None, 4, "group"))
        self.assertEqual(self.db.calls("credit_history")[0]["p_scope"], "group")


# ---------------------------------------------------------------------------
# Admin: grant / revoke, compensation, reasons, history, refusals (§8.5)
# ---------------------------------------------------------------------------


class AdminPool(RouteBase):
    async def change(self, delta, tid=TEACHER, **body):
        return await admin_route.change_teacher_credits(tid, body={"delta": delta, **body}, admin=A_)

    async def test_grant_and_revoke_answer_the_free_pool(self):
        self.assertEqual(await self.change(10, reason="  Schuljahr  ", request_id=REQ),
                         {"teacher_id": TEACHER, "delta": 10, "pool_available": 10})
        (call,) = self.db.calls("credit_admin_grant")
        self.assertEqual(call, {"p_admin_id": ADMIN, "p_teacher_id": TEACHER, "p_hours": 10, "p_reason": "Schuljahr",
                                "p_request_id": REQ})
        self.assertEqual(await self.change(-4), {"teacher_id": TEACHER, "delta": -4, "pool_available": 6})
        self.assertIsNone(self.db.calls("credit_admin_revoke")[0]["p_reason"])

    async def test_validation(self):
        cases = [({"delta": 0}, (400, C.DELTA_ZERO_DE)), ({"delta": 10001}, (400, C.ADMIN_DELTA_RANGE_DE)),
                 ({"delta": "1"}, (400, C.ADMIN_DELTA_RANGE_DE)), ({"delta": 1, "reason": 5}, (400, C.REASON_TOO_LONG_DE)),
                 ({"delta": 1, "reason": "x" * 201}, (400, C.REASON_TOO_LONG_DE)),
                 ({"delta": 1, "request_id": "r"}, (400, C.CREDIT_CHANGE_FAILED_DE))]
        for body, want in cases:
            with self.subTest(body=body):
                self.assertEqual(await self.refused(admin_route.change_teacher_credits(TEACHER, body=body, admin=A_)),
                                 want)
        self.assertEqual(await self.refused(admin_route.change_teacher_credits(TEACHER, body=None, admin=A_)),
                         (400, C.ADMIN_DELTA_RANGE_DE))
        self.assertEqual(await self.refused(self.change(1, tid="not-a-uuid")), (404, C.TEACHER_NOT_FOUND_DE))
        self.assertEqual(self.db.calls("credit_admin_grant"), [])

    async def test_every_p_code(self):
        self.db.errors["credit_admin_revoke"] = [err("P0062", details="7")]
        self.assertEqual(await self.refused(self.change(-50)), (409, C.revoke_too_much(7)))
        self.assertIn("7 Stunden", C.revoke_too_much(7))
        # without a DETAIL the teacher's free pool is read back
        self.db.summary[TEACHER] = {"o_free": 1}
        self.db.errors["credit_admin_revoke"] = [err("P0062")]
        self.assertEqual(await self.refused(self.change(-50)), (409, C.revoke_too_much(1)))
        for code, want in (("P0002", (404, C.TEACHER_NOT_FOUND_DE)), ("P0067", (404, C.TEACHER_NOT_FOUND_DE)),
                           ("P0071", (409, C.REQUEST_REUSED_DE)), ("XX000", (500, C.CREDIT_CHANGE_FAILED_DE))):
            with self.subTest(code=code):
                self.db.errors["credit_admin_grant"] = [PgError(code)]
                self.assertEqual(await self.refused(self.change(3)), want)

    def test_the_old_patch_route_is_gone(self):
        # FastAPI answers 405 for PATCH on a path that only has a POST
        with open(os.path.join(APP, "routes", "admin.py"), encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call) and getattr(dec.func, "attr", None) == "patch":
                        self.fail(f"admin.py still declares a PATCH route: {node.name}")
        self.assertIn('@router.post("/teachers/{teacher_id}/credits")', src)


class AdminCompensation(RouteBase):
    BODY = {"target_kind": "student", "target_id": STUDENT, "hours": 3, "reason": "GPU-Ausfall"}

    async def grant(self, **over):
        return await admin_route.compensate(body={**self.BODY, **over}, admin=A_)

    async def test_a_compensation_and_its_reason_in_the_teachers_history(self):
        out = await self.grant(training_id=12, request_id=REQ, reason="  GPU-Ausfall  ")
        self.assertEqual(out, {"target_kind": "student", "target_id": STUDENT, "hours_free": 3})
        (call,) = self.db.calls("credit_admin_compensate")
        self.assertEqual(call, {"p_admin_id": ADMIN, "p_target_kind": "student", "p_target_id": STUDENT, "p_hours": 3,
                                "p_reason": "GPU-Ausfall", "p_training_id": 12, "p_request_id": REQ})
        # the ledger row the function wrote reaches the teacher with its reason
        self.db.ledger.append(ledger_row(1, "compensate", 3, to=("student", STUDENT), actor_id=ADMIN,
                                         actor_role="admin", reason_id=1, reason=call["p_reason"], training_id=12))
        hist = await teacher_route.student_credit_history(STUDENT, before=None, limit=None, teacher=T_)
        self.assertEqual((hist["entries"][0]["reason"], hist["entries"][0]["kind"]), ("GPU-Ausfall", "compensate"))

    async def test_every_validation_sentence_in_order(self):
        # each case also breaks every LATER rule, so only the order explains the sentence
        cases = [
            ({"target_kind": "teacher", "hours": 0, "reason": ""}, (400, C.COMPENSATION_TARGET_KIND_DE)),
            ({"hours": 0, "reason": ""}, (400, C.COMPENSATION_HOURS_RANGE_DE)),
            ({"hours": 101, "reason": ""}, (400, C.COMPENSATION_HOURS_RANGE_DE)),
            ({"hours": "3", "reason": ""}, (400, C.COMPENSATION_HOURS_RANGE_DE)),
            ({"reason": "   ", "target_id": "x"}, (400, C.COMPENSATION_REASON_REQUIRED_DE)),
            ({"reason": None, "target_id": "x"}, (400, C.COMPENSATION_REASON_REQUIRED_DE)),
            ({"reason": "x" * 201, "target_id": "x"}, (400, C.REASON_TOO_LONG_DE)),
            ({"target_id": "x", "training_id": 0}, (404, C.STUDENT_NOT_FOUND_DE)),
            ({"target_kind": "group", "target_id": "x"}, (404, C.WORKGROUP_NOT_FOUND_DE)),
            ({"training_id": 0}, (409, C.COMPENSATION_TRAINING_MISMATCH_DE)),
            ({"training_id": "12"}, (409, C.COMPENSATION_TRAINING_MISMATCH_DE)),
            ({"request_id": "r"}, (400, C.CREDIT_CHANGE_FAILED_DE)),
        ]
        for over, want in cases:
            with self.subTest(over=over):
                self.assertEqual(await self.refused(self.grant(**over)), want)
        self.assertEqual(await self.refused(admin_route.compensate(body=None, admin=A_)),
                         (400, C.COMPENSATION_TARGET_KIND_DE))
        self.assertEqual(self.db.calls("credit_admin_compensate"), [])

    async def test_every_p_code(self):
        cases = {"P0002": (404, C.STUDENT_NOT_FOUND_DE), "P0023": (409, C.COMPENSATION_GROUPED_DE),
                 "P0066": (409, C.COMPENSATION_NO_ACCOUNT_DE), "P0070": (409, C.COMPENSATION_TRAINING_MISMATCH_DE),
                 "P0071": (409, C.REQUEST_REUSED_DE), "P0067": (500, C.CREDIT_CHANGE_FAILED_DE)}
        for code, want in cases.items():
            with self.subTest(code=code):
                self.db.errors["credit_admin_compensate"] = [PgError(code)]
                self.assertEqual(await self.refused(self.grant()), want)
        self.db.errors["credit_admin_compensate"] = [PgError("P0002")]
        self.assertEqual(await self.refused(self.grant(target_kind="group", target_id=GROUP)),
                         (404, C.WORKGROUP_NOT_FOUND_DE))


class AdminReadsAndReasons(RouteBase):
    async def test_erase_a_reason(self):
        for rid in ("abc", "0", "-3"):
            self.assertEqual(await self.refused(admin_route.erase_reason(rid, admin=A_)), (404, C.REASON_NOT_FOUND_DE))
        self.assertEqual(await admin_route.erase_reason("5", admin=A_), {"erased": True})
        self.assertEqual(await admin_route.erase_reason("5", admin=A_), {"erased": False})
        self.assertEqual(self.db.calls("credit_erase_reason")[0], {"p_admin_id": ADMIN, "p_reason_id": 5})

    async def test_the_admin_history_carries_the_ids_a_compensation_needs(self):
        self.db.ledger.append(ledger_row(1, "charge", 2, frm=("group", GROUP), member_id=STUDENT, training_id=None,
                                         actor_role="system", reason_id=None))
        out = await admin_route.admin_credit_history(scope=None, teacher_id=None, before=None, limit=None, admin=A_)
        (e,) = out["entries"]
        self.assertEqual((e["account_kind"], e["account_id"], e["member_id"], e["actor_role"], e["reason_id"]),
                         ("group", GROUP, STUDENT, "system", None))
        await admin_route.admin_credit_history(scope="teacher_all", teacher_id=TEACHER, before=None, limit=None,
                                               admin=A_)
        self.assertEqual(self.db.calls("credit_history")[-1]["p_scope"], "teacher_all")
        self.assertEqual(await self.refused(admin_route.admin_credit_history(scope="student", teacher_id=None,
                                                                             before=None, limit=None, admin=A_)),
                         (400, C.HISTORY_FAILED_DE))
        self.assertEqual(await self.refused(admin_route.admin_credit_history(scope="teacher_all", teacher_id="x",
                                                                             before=None, limit=None, admin=A_)),
                         (404, C.TEACHER_NOT_FOUND_DE))

    async def test_the_refusal_list(self):
        rows = self.db.tables["training_start_refusals"]
        for i, uid in enumerate((STUDENT, TEACHER, STUDENT), start=1):
            rows.append({"id": i, "at": f"t{i}", "user_id": uid, "role": "student", "request_kind": "start",
                         "code": "no_hours", "model_type": "act"})
        out = await admin_route.admin_refusals(user_id=STUDENT, limit=None, admin=A_)
        self.assertEqual([r["id"] for r in out["refusals"]], [3, 1])
        self.assertEqual(await admin_route.admin_refusals(user_id="not-a-uuid", limit=None, admin=A_), {"refusals": []})
        self.assertEqual(len((await admin_route.admin_refusals(user_id=None, limit=None, admin=A_))["refusals"]), 3)

    async def test_the_teacher_summary_carries_compensated(self):
        self.db.summary[TEACHER] = {"o_free": 5, "o_assigned_free": 3, "o_assigned_reserved": 2, "o_spent": 9,
                                    "o_compensated": 4, "o_student_count": 6, "o_group_count": 1, "o_group_hours": 2}
        self.assertEqual(teacher_route.pool_fields(teacher_route.teacher_summary(TEACHER)),
                         {"pool_total": 10, "allocated_total": 5, "pool_available": 5, "student_count": 6,
                          "group_count": 1, "group_credits_total": 2, "hours_spent": 9, "compensated": 4})
        self.db.tables["credit_ledger"].append({"to_id": TEACHER, "to_kind": "teacher", "kind": "migrate",
                                                "meta": {"old_credits": 30}})
        rows = [dump(r) for r in await admin_route.list_teachers(admin=A_)]
        me = next(r for r in rows if r["id"] == TEACHER)
        self.assertEqual((me["compensated"], me["hours_spent"], me["old_credits"], me["pool_available"]), (4, 9, 30, 5))


class AdminOnly(unittest.IsolatedAsyncioTestCase):
    """get_current_admin answers a teacher or a student 403 „Nur für Administratoren erlaubt."
    (the house stub replaces app.auth, so the real module is loaded by path)."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("_auth_under_test", os.path.join(APP, "auth.py"))
        cls.auth = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.auth)

    async def test_non_admins_are_refused(self):
        for role in ("teacher", "student"):
            with self.subTest(role=role), patch.object(self.auth, "get_user_profile",
                                                       lambda uid, role=role: {"id": uid, "role": role}):
                with self.assertRaises(HTTPException) as ctx:
                    await self.auth.get_current_admin(user=SimpleNamespace(id="u"))
                self.assertEqual((ctx.exception.status_code, ctx.exception.detail),
                                 (403, "Nur für Administratoren erlaubt."))
        with patch.object(self.auth, "get_user_profile", lambda uid: {"id": uid, "role": "admin"}):
            self.assertEqual((await self.auth.get_current_admin(user=SimpleNamespace(id="a")))["role"], "admin")

    def test_every_credit_route_of_admin_py_depends_on_get_current_admin(self):
        with open(os.path.join(APP, "routes", "admin.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        names = {"change_teacher_credits", "compensate", "erase_reason", "admin_credit_history", "admin_refusals"}
        seen = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name in names:
                seen.add(node.name)
                defaults = [ast.unparse(d) for d in node.args.defaults + node.args.kw_defaults if d is not None]
                self.assertIn("Depends(get_current_admin)", defaults, node.name)
        self.assertEqual(seen, names)


class RateKeys(unittest.TestCase):
    def test_teacher_and_admin_routes_are_keyed_per_user(self):
        with open(os.path.join(APP, "main.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        value = next(n.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                     and any(getattr(t, "id", None) == "_PER_USER_RATE_LIMIT_PREFIXES" for t in n.targets))
        prefixes = ast.literal_eval(value)
        self.assertIn("/teacher/", prefixes)
        self.assertIn("/admin/", prefixes)


if __name__ == "__main__":
    unittest.main()

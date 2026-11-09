"""/me/export and /me/delete with managed trainings (spec §4.4, E16, FR-19).

The GDPR export names every trainings column except worker_token (the 20
columns 047 grants plus 049's seven, 050's three and 052's ten except
idempotency_key, §8.6) and lists, per managed training, where
each attempt ran — provider, cloud, data centre, GPU, start, end — never the
pod or call id. /me/delete cancels a managed row through the same
compare-and-set as Cancel and leaves a publishing one to finish; legacy rows
keep today's direct update.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.tests.test_dataset_identity_routes import _ensure_stubs

_ensure_stubs()

from app.routes import me as route  # noqa: E402
from app.services import training_dispatch as D  # noqa: E402
from app.tests._managed_fakes import Clock, FakeDB, FakeModal, FakeRunpod  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
MIG = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "supabase", "migrations")
PROFILE = {"id": "u-1", "role": "student", "workgroup_id": None, "classroom_id": None}


def content(resp):
    return json.loads(resp.body) if hasattr(resp, "body") else resp.content


def _user_row(uid, **over):
    """Every column of a public.users row (FakeDB refuses a select of a missing one)."""
    row = {"id": uid, "email": f"{uid}@edubotics.local", "role": "student", "username": uid, "full_name": "Anna",
           "classroom_id": None, "workgroup_id": None, "created_by": "teacher-9", "created_at": "2026-09-01T08:00:00+00:00",
           "deletion_requested_at": None, "hf_username": None, "training_credits": 0,
           "vision_quota_per_term": None, "vision_used_per_term": 0}
    row.update(over)
    return row


def _db():
    db = FakeDB(Clock())
    for t in ("workgroup_memberships", "workflows", "datasets", "workflow_versions", "tutorial_progress",
              "progress_entries", "users", "user_hf_credentials", "classrooms", "workflow_trajectories",
              "workflow_submissions", "inference_sessions", "inference_runs", "credit_accounts"):
        db.tables[t] = []
    db.tables["users"].append(_user_row("u-1"))
    db.tables["users"].append(_user_row("t-1", role="teacher"))
    db.auth = SimpleNamespace(admin=SimpleNamespace(get_user_by_id=lambda uid: SimpleNamespace(user=None)))
    return db


class Export(unittest.TestCase):
    def test_the_columns_are_every_column_but_the_token(self):
        cols = [c.strip() for c in route._EXPORT_TRAINING_COLUMNS.split(",")]
        self.assertNotIn("worker_token", cols)
        def code(name):
            with open(os.path.join(MIG, name), encoding="utf-8") as fh:
                return "\n".join(line.split("--", 1)[0] for line in fh.read().splitlines())

        m047 = re.search(r"GRANT SELECT \((.*?)\) ON public\.trainings", code("20261020120000_047_hide_worker_token.sql"), re.S)
        m049 = re.search(r"GRANT SELECT \((.*?)\)\s+ON public\.trainings", code("20261020130000_049_managed_training.sql"), re.S)
        m050 = re.search(r"GRANT SELECT \((.*?)\) ON public\.trainings", code("20261021120000_050_training_continuation.sql"), re.S)
        m052 = re.search(r"GRANT SELECT \((.*?)\)\s+ON public\.trainings", code("20261028120000_052_credit_ledger.sql"), re.S)
        granted = ([c.strip() for c in m047.group(1).split(",")] + [c.strip() for c in m049.group(1).split(",")]
                   + [c.strip() for c in m050.group(1).split(",")] + [c.strip() for c in m052.group(1).split(",")])
        # 052's columns the request roles do not read, every one but idempotency_key (§8.6)
        not_granted_052 = ["credit_scheme", "settled_at", "stop_requested_at", "lost_reasons", "charge_waiver"]
        self.assertEqual(sorted(cols), sorted(granted + not_granted_052))
        self.assertEqual(len(cols), 39)
        self.assertNotIn("idempotency_key", cols)
        self.assertNotIn("stop_requested_by", cols, "no column of the row names another person")

    def test_the_export_carries_attempts_without_ids(self):
        db = _db()
        managed = db.add_training(user_id="u-1", status="succeeded", worker_token="should-not-leak", current_attempt=3)
        db.add_attempt(managed["id"], 1, backend="runpod_community", cloud_ref="pod-secret", data_center="EU-RO-1",
                       gpu_type="NVIDIA RTX A5000", started_at="2026-10-18T10:00:00+00:00",
                       ended_at="2026-10-18T11:00:00+00:00", state="lost")
        db.add_attempt(managed["id"], 2, backend="modal", cloud_ref="fc-secret", state="ended",
                       started_at="2026-10-18T11:05:00+00:00", ended_at="2026-10-18T12:00:00+00:00")
        db.add_attempt(managed["id"], 3, backend=None, state="ended")  # never placed: ran nowhere
        legacy = db.add_training(user_id="u-1", status="failed", managed=False, worker_token="x")
        db.add_training(user_id="someone-else", status="queued")
        from app.services import workgroups

        with patch.object(route, "get_supabase", lambda: db), patch.object(workgroups, "get_supabase", lambda: db):
            resp = route.export_my_data(profile=dict(PROFILE))
        bundle = content(resp)
        text = json.dumps(bundle)
        for secret in ("should-not-leak", "pod-secret", "fc-secret", "worker_token", "cloud_ref"):
            self.assertNotIn(secret, text)
        by_id = {t["id"]: t for t in bundle["trainings"]}
        self.assertEqual(set(by_id), {managed["id"], legacy["id"]})
        self.assertEqual(by_id[managed["id"]]["attempts"], [
            {"provider": "runpod", "cloud": "community", "data_center": "EU-RO-1",
             "requested_data_centers": [], "gpu_type": "NVIDIA RTX A5000",
             "started_at": "2026-10-18T10:00:00+00:00", "ended_at": "2026-10-18T11:00:00+00:00"},
            {"provider": "modal", "cloud": None, "data_center": None, "requested_data_centers": [], "gpu_type": None,
             "started_at": "2026-10-18T11:05:00+00:00", "ended_at": "2026-10-18T12:00:00+00:00"},
        ])
        self.assertEqual(by_id[legacy["id"]]["attempts"], [])
        self.assertEqual(resp.headers["Cache-Control"], "no-store")
        self.assertEqual(bundle["credit_ledger"], [])
        self.assertEqual(bundle["training_start_refusals"], [])

    def _bundle(self, db, profile):
        from app.services import workgroups

        with patch.object(route, "get_supabase", lambda: db), patch.object(workgroups, "get_supabase", lambda: db):
            return content(route.export_my_data(profile=dict(profile)))

    def test_the_ledger_view_names_no_other_person(self):
        # §8.6 / §13: {at, kind, hours, account_kind, training_id, actor_role}; the reason only
        # on a compensation into the caller's own account (and a teacher's own grant/revoke)
        db = _db()
        db.ledger = [
            {"id": 1, "at": "t1", "kind": "assign", "hours": 3, "from_kind": "teacher", "from_id": "teacher-9",
             "to_kind": "student", "to_id": "u-1", "training_id": None, "member_id": None, "actor_id": "teacher-9",
             "actor_role": "teacher", "reason_id": None, "reason": None, "meta": {}},
            {"id": 2, "at": "t2", "kind": "reserve", "hours": 3, "from_kind": "student", "from_id": "u-1",
             "to_kind": "student", "to_id": "u-1", "training_id": 7, "member_id": "u-1", "actor_id": "u-1",
             "actor_role": "student", "reason_id": None, "reason": None, "meta": {}},
            {"id": 3, "at": "t3", "kind": "compensate", "hours": 1, "from_kind": None, "from_id": None,
             "to_kind": "student", "to_id": "u-1", "training_id": 7, "member_id": "u-1", "actor_id": "admin-1",
             "actor_role": "admin", "reason_id": 4, "reason": "GPU-Ausfall", "meta": {}},
        ]
        db.tables["training_start_refusals"].append({"id": 1, "at": "t9", "user_id": "u-1", "role": "student",
                                                     "request_kind": "start", "code": "no_hours", "model_type": "act"})
        bundle = self._bundle(db, PROFILE)
        self.assertEqual([e["kind"] for e in bundle["credit_ledger"]], ["compensate", "reserve", "assign"])
        for e in bundle["credit_ledger"]:
            self.assertLessEqual(set(e), {"at", "kind", "hours", "account_kind", "training_id", "actor_role", "reason"})
        self.assertEqual(bundle["credit_ledger"][0]["reason"], "GPU-Ausfall")
        self.assertNotIn("reason", bundle["credit_ledger"][2])
        text = json.dumps(bundle["credit_ledger"])
        for other in ("teacher-9", "admin-1"):
            self.assertNotIn(other, text)
        self.assertEqual(bundle["training_start_refusals"],
                         [{"at": "t9", "request_kind": "start", "code": "no_hours", "model_type": "act"}])
        (call,) = db.calls("credit_history")
        self.assertEqual((call["p_scope"], call["p_id"]), ("student", "u-1"))

    def test_no_other_reason_text_reaches_the_export(self):
        # a compensation into the student's GROUP (member_id = the student) and a reserve whose
        # row carries a reason: neither text is about this person's own account
        db = _db()
        db.ledger = [
            {"id": 1, "at": "t1", "kind": "compensate", "hours": 2, "from_kind": None, "from_id": None,
             "to_kind": "group", "to_id": "g-1", "training_id": 7, "member_id": "u-1", "actor_id": "admin-1",
             "actor_role": "admin", "reason_id": 5, "reason": "Ausfall der Gruppe Rot", "meta": {}},
            {"id": 2, "at": "t2", "kind": "reserve", "hours": 3, "from_kind": "student", "from_id": "u-1",
             "to_kind": "student", "to_id": "u-1", "training_id": 7, "member_id": "u-1", "actor_id": "u-1",
             "actor_role": "student", "reason_id": 6, "reason": "interne Notiz", "meta": {}},
        ]
        bundle = self._bundle(db, PROFILE)
        self.assertEqual([e["kind"] for e in bundle["credit_ledger"]], ["reserve", "compensate"])
        for e in bundle["credit_ledger"]:
            self.assertNotIn("reason", e)
        text = json.dumps(bundle)
        for reason in ("Ausfall der Gruppe Rot", "interne Notiz"):
            self.assertNotIn(reason, text)
        # a teacher's export: only the teacher's own grant/revoke reasons
        db = _db()
        db.ledger = [{"id": 1, "at": "t", "kind": "assign", "hours": 2, "from_kind": "teacher", "from_id": "t-1",
                      "to_kind": "student", "to_id": "s-1", "training_id": None, "member_id": None,
                      "actor_id": "t-1", "actor_role": "teacher", "reason_id": 3, "reason": "Schülerin X", "meta": {}}]
        bundle = self._bundle(db, {"id": "t-1", "role": "teacher", "workgroup_id": None, "classroom_id": None})
        self.assertNotIn("Schülerin X", json.dumps(bundle))

    def test_a_teacher_exports_the_pool_history(self):
        db = _db()
        db.ledger = [{"id": 1, "at": "t", "kind": "grant", "hours": 20, "from_kind": None, "from_id": None,
                      "to_kind": "teacher", "to_id": "t-1", "training_id": None, "member_id": None,
                      "actor_id": "admin-1", "actor_role": "admin", "reason_id": 2, "reason": "Schuljahr",
                      "meta": {}}]
        bundle = self._bundle(db, {"id": "t-1", "role": "teacher", "workgroup_id": None, "classroom_id": None})
        self.assertEqual(bundle["credit_ledger"], [{"at": "t", "kind": "grant", "hours": 20, "account_kind": "teacher",
                                                    "training_id": None, "actor_role": "admin",
                                                    "reason": "Schuljahr"}])


# Every column of each table, read from a database replayed 000 → 055 with psql
# (information_schema.columns, 2026-11-11). The export names each one except the
# credentials and other people's ids listed in EXCLUDED.
TABLE_COLUMNS = {
    "users": {"id", "email", "training_credits", "created_at", "role", "username", "full_name", "classroom_id",
              "created_by", "deletion_requested_at", "workgroup_id", "vision_quota_per_term",
              "vision_used_per_term", "hf_username"},
    "workflows": {"id", "owner_user_id", "classroom_id", "name", "description", "blockly_json", "is_template",
                  "created_at", "updated_at", "workgroup_id", "sim_scene", "code_language", "code_files"},
    "workflow_versions": {"id", "workflow_id", "blockly_json", "note", "created_at", "saved_by", "code_files",
                          "code_language"},
    "datasets": {"id", "owner_user_id", "workgroup_id", "hf_repo_id", "name", "description", "episode_count",
                 "total_frames", "fps", "robot_type", "created_at", "updated_at", "discovered_via_sweep"},
    "workflow_trajectories": {"id", "workflow_id", "owner_user_id", "name", "samples", "point_count", "duration_s",
                              "fps", "created_at", "updated_at", "robot_profile"},
    "workflow_submissions": {"id", "workflow_id", "student_user_id", "classroom_id", "name", "code_language",
                             "code_files", "blockly_json", "sim_scene", "note", "submitted_at"},
    "inference_sessions": {"id", "user_id", "classroom_id", "workgroup_id", "training_id", "model_repo",
                           "model_revision", "policy_type", "token_hash", "status", "conn_id", "modal_session_id",
                           "edge_token_enc", "edge_attached_at", "edge_terminated_at", "session_s", "reserve_until",
                           "created_at", "connected_at", "last_server_heartbeat_at", "client_heartbeat_at",
                           "max_end_at", "ended_at", "end_reason", "active_seconds", "stats", "run_logged_at"},
    "inference_runs": {"id", "student_user_id", "classroom_id", "policy_repo", "started_at", "duration_s",
                       "exit_reason", "error_message_de", "created_at", "backend"},
    "user_hf_credentials": {"user_id", "token_ciphertext", "token_fp", "token_hint", "hf_username", "token_role",
                            "validated_at", "created_at", "updated_at"},
    "progress_entries": {"id", "classroom_id", "student_id", "entry_date", "note", "created_at", "updated_at",
                         "workgroup_id"},
    "credit_accounts": {"kind", "owner_id", "parent_teacher_id", "free", "reserved", "spent", "member_hour_limit",
                        "closed_at", "created_at"},
}
EXCLUDED = {
    "users": set(),
    "workflows": set(),
    "workflow_versions": set(),   # saved_by is read, then replaced by saved_by_me
    "datasets": set(),
    "workflow_trajectories": set(),
    "workflow_submissions": set(),
    "inference_sessions": {"token_hash", "conn_id", "modal_session_id", "edge_token_enc"},
    "inference_runs": set(),
    "user_hf_credentials": {"user_id", "token_ciphertext", "token_fp", "token_hint"},
    "progress_entries": set(),
    "credit_accounts": {"owner_id", "parent_teacher_id"},
}
COLUMN_LISTS = {
    "users": "_EXPORT_PROFILE_COLUMNS",
    "workflows": "_EXPORT_WORKFLOW_COLUMNS",
    "workflow_versions": "_EXPORT_VERSION_COLUMNS",
    "datasets": "_EXPORT_DATASET_COLUMNS",
    "workflow_trajectories": "_EXPORT_TRAJECTORY_COLUMNS",
    "workflow_submissions": "_EXPORT_SUBMISSION_COLUMNS",
    "inference_sessions": "_EXPORT_INFERENCE_SESSION_COLUMNS",
    "inference_runs": "_EXPORT_INFERENCE_RUN_COLUMNS",
    "user_hf_credentials": "_EXPORT_HF_CREDENTIAL_COLUMNS",
    "progress_entries": "_EXPORT_PROGRESS_COLUMNS",
    "credit_accounts": "_EXPORT_CREDIT_ACCOUNT_COLUMNS",
}


def _row(table, **over):
    row = {c: None for c in TABLE_COLUMNS[table]}
    row.update(over)
    return row


class ExportCompleteness(unittest.TestCase):
    """Launch plan step 5 (F-56): the tables the export used to miss, explicit columns everywhere."""

    def _bundle(self, db, profile=PROFILE):
        from app.services import workgroups

        with patch.object(route, "get_supabase", lambda: db), patch.object(workgroups, "get_supabase", lambda: db):
            resp = route.export_my_data(profile=dict(profile))
        return content(resp)

    def test_every_column_list_is_the_table_minus_its_credentials(self):
        for table, name in COLUMN_LISTS.items():
            with self.subTest(table=table):
                cols = [c.strip() for c in getattr(route, name).split(",")]
                self.assertEqual(len(cols), len(set(cols)))
                self.assertEqual(set(cols), TABLE_COLUMNS[table] - EXCLUDED[table])

    def test_no_read_of_the_export_is_select_star(self):
        import ast

        for fn in (route.export_my_data, route._export_attempts, route._export_credit_account,
                   route._export_auth_account, route._export_ledger):
            tree = ast.parse(inspect.getsource(fn).lstrip())
            selects = [n for n in ast.walk(tree)
                       if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "select"]
            for call in selects:
                with self.subTest(fn=fn.__name__, line=call.lineno):
                    # one argument: a literal column list or one of the module's column constants
                    self.assertEqual(len(call.args), 1)
                    arg = call.args[0]
                    if isinstance(arg, ast.Constant):
                        self.assertNotIn("*", arg.value)
                    else:
                        self.assertIsInstance(arg, ast.Name)
                        self.assertNotIn("*", getattr(route, arg.id))
        self.assertGreaterEqual(len([n for n in ast.walk(ast.parse(inspect.getsource(route.export_my_data).lstrip()))
                                     if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "select"]), 16)

    def _seeded(self):
        db = _db()
        me, other = "u-1", "u-2"
        db.tables["users"].append(_user_row(other))
        db.tables["users"][0].update(deletion_requested_at="2026-11-11T09:00:00+00:00", vision_used_per_term=3)
        db.tables["workflows"] += [_row("workflows", id="wf-1", owner_user_id=me, name="Mein Programm"),
                                   _row("workflows", id="wf-2", owner_user_id=other, name="Fremd")]
        db.tables["workflow_versions"] += [
            _row("workflow_versions", id="v-1", workflow_id="wf-1", saved_by=me),
            _row("workflow_versions", id="v-2", workflow_id="wf-1", saved_by="sibling-7"),
            _row("workflow_versions", id="v-3", workflow_id="wf-2", saved_by=other)]
        db.tables["workflow_trajectories"] += [
            _row("workflow_trajectories", id="tr-1", workflow_id="wf-1", owner_user_id=me, name="Winken",
                 samples={"fps": 25, "points": [[0, 0, 0, 0, 0, 0, 0]]}),
            _row("workflow_trajectories", id="tr-2", workflow_id="wf-2", owner_user_id=other, name="Fremd")]
        db.tables["workflow_submissions"] += [
            _row("workflow_submissions", id="sub-1", workflow_id="wf-1", student_user_id=me, name="Abgabe"),
            _row("workflow_submissions", id="sub-2", workflow_id="wf-2", student_user_id=other)]
        db.tables["inference_sessions"] += [
            _row("inference_sessions", id="is-1", user_id=me, model_repo="EduBotics-Solutions/m",
                 token_hash="hash-must-not-leak", conn_id="conn-must-not-leak",
                 modal_session_id="sd-must-not-leak", edge_token_enc="edge-must-not-leak", status="ended",
                 stats={"chunks": 3}),
            _row("inference_sessions", id="is-2", user_id=other, token_hash="other-hash")]
        db.tables["inference_runs"] += [
            _row("inference_runs", id=1, student_user_id=me, policy_repo="EduBotics-Solutions/m", backend="cloud"),
            _row("inference_runs", id=2, student_user_id=other, policy_repo="other/repo")]
        db.tables["user_hf_credentials"] += [
            _row("user_hf_credentials", user_id=me, token_ciphertext="v1.cipher-must-not-leak",
                 token_fp="fp-must-not-leak", token_hint="hf_…hint", hf_username="anna-hf", token_role="write",
                 validated_at="2026-11-01T10:00:00+00:00"),
            _row("user_hf_credentials", user_id=other, token_ciphertext="v1.other", hf_username="bob-hf")]
        db.tables["credit_accounts"] += [
            _row("credit_accounts", kind="student", owner_id=me, parent_teacher_id="teacher-9", free=2, reserved=1,
                 spent=4),
            _row("credit_accounts", kind="student", owner_id=other, parent_teacher_id="teacher-9", free=9)]
        return db

    def test_the_new_keys_carry_exactly_the_callers_rows(self):
        bundle = self._bundle(self._seeded())
        self.assertEqual([r["id"] for r in bundle["workflow_trajectories"]], ["tr-1"])
        self.assertEqual(bundle["workflow_trajectories"][0]["samples"], {"fps": 25, "points": [[0, 0, 0, 0, 0, 0, 0]]})
        self.assertEqual([r["id"] for r in bundle["workflow_submissions"]], ["sub-1"])
        self.assertEqual([r["id"] for r in bundle["inference_sessions"]], ["is-1"])
        self.assertEqual(bundle["inference_sessions"][0]["stats"], {"chunks": 3})
        self.assertEqual([r["id"] for r in bundle["inference_runs"]], [1])
        self.assertEqual(bundle["hf_credential"], {"hf_username": "anna-hf", "token_role": "write",
                                                   "validated_at": "2026-11-01T10:00:00+00:00",
                                                   "created_at": None, "updated_at": None})
        self.assertEqual(bundle["credit_account"], {"kind": "student", "free": 2, "reserved": 1, "spent": 4,
                                                    "member_hour_limit": None, "closed_at": None,
                                                    "created_at": None})
        self.assertEqual([w["id"] for w in bundle["workflows"]], ["wf-1"])

    def test_no_credential_and_no_other_persons_data_reaches_the_bundle(self):
        text = json.dumps(self._bundle(self._seeded()))
        for secret in ("hash-must-not-leak", "conn-must-not-leak", "sd-must-not-leak", "edge-must-not-leak",
                       "cipher-must-not-leak", "fp-must-not-leak", "hf_…hint", "token_hash", "token_ciphertext",
                       "edge_token_enc", "parent_teacher_id"):
            self.assertNotIn(secret, text)
        for foreign in ("u-2", "other-hash", "v1.other", "bob-hf", "other/repo", "Fremd", "sibling-7"):
            self.assertNotIn(foreign, text)

    def test_the_profile_is_every_column_of_the_own_row(self):
        bundle = self._bundle(self._seeded())
        self.assertEqual(set(bundle["profile"]), TABLE_COLUMNS["users"])
        self.assertEqual(bundle["profile"]["id"], "u-1")
        self.assertEqual(bundle["profile"]["deletion_requested_at"], "2026-11-11T09:00:00+00:00")
        self.assertEqual(bundle["profile"]["vision_used_per_term"], 3)

    def test_a_version_saved_by_a_sibling_says_only_that_it_was_not_me(self):
        bundle = self._bundle(self._seeded())
        by_id = {v["id"]: v for v in bundle["workflow_versions"]}
        self.assertEqual(set(by_id), {"v-1", "v-2"})
        self.assertIs(by_id["v-1"]["saved_by_me"], True)
        self.assertIs(by_id["v-2"]["saved_by_me"], False)
        for v in by_id.values():
            self.assertNotIn("saved_by", v)

    def test_the_auth_account_is_exported_without_ids_and_with_a_note_when_unreadable(self):
        db = self._seeded()
        factor = SimpleNamespace(id="factor-id-must-not-leak", factor_type="totp", friendly_name="Handy",
                                 status="verified", created_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
                                 secret="totp-secret-must-not-leak")
        user = SimpleNamespace(id="u-1", email="anna@edubotics.local",
                               created_at=datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc),
                               updated_at=None, last_sign_in_at=datetime(2026, 11, 10, 7, 30, tzinfo=timezone.utc),
                               email_confirmed_at=None, confirmed_at=None, factors=[factor],
                               app_metadata={"provider": "email"}, user_metadata={})
        asked = []
        db.auth = SimpleNamespace(admin=SimpleNamespace(
            get_user_by_id=lambda uid: asked.append(uid) or SimpleNamespace(user=user)))
        bundle = self._bundle(db)
        self.assertEqual(asked, ["u-1"])
        self.assertEqual(bundle["auth_account"]["email"], "anna@edubotics.local")
        self.assertEqual(bundle["auth_account"]["last_sign_in_at"], "2026-11-10T07:30:00+00:00")
        self.assertEqual(bundle["auth_account"]["mfa_factors"], [
            {"factor_type": "totp", "friendly_name": "Handy", "status": "verified",
             "created_at": "2026-09-02T00:00:00+00:00"}])
        self.assertNotIn("auth_account_note", bundle)
        text = json.dumps(bundle)
        self.assertNotIn("factor-id-must-not-leak", text)
        self.assertNotIn("totp-secret-must-not-leak", text)

        def broken(_uid):
            raise RuntimeError("GoTrue 503")

        db.auth = SimpleNamespace(admin=SimpleNamespace(get_user_by_id=broken))
        bundle = self._bundle(db)
        self.assertIsNone(bundle["auth_account"])
        self.assertEqual(bundle["auth_account_note"], route.AUTH_ACCOUNT_UNREADABLE_DE)
        self.assertIn("später", bundle["auth_account_note"])
        self.assertNotIn("GoTrue", json.dumps(bundle))
        # an answer for another account is never exported
        db.auth = SimpleNamespace(admin=SimpleNamespace(
            get_user_by_id=lambda uid: SimpleNamespace(user=SimpleNamespace(id="u-2", email="b@x"))))
        bundle = self._bundle(db)
        self.assertIsNone(bundle["auth_account"])

    def test_a_teacher_gets_no_student_account_and_their_own_pool_account(self):
        db = self._seeded()
        db.tables["credit_accounts"].append(_row("credit_accounts", kind="teacher", owner_id="t-1", free=40))
        bundle = self._bundle(db, {"id": "t-1", "role": "teacher", "workgroup_id": None, "classroom_id": None})
        self.assertEqual(bundle["credit_account"]["kind"], "teacher")
        self.assertEqual(bundle["credit_account"]["free"], 40)
        self.assertEqual(bundle["workflow_submissions"], [])
        self.assertIsNone(bundle["hf_credential"])


class Delete(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        D.reset_state_for_tests()
        self.db = _db()
        self.rp = FakeRunpod()
        self.modal = FakeModal()
        self.legacy_cancel = AsyncMock()
        self._cms = [patch.object(route, "get_supabase", lambda: self.db), patch.object(D, "_sb", lambda: self.db),
                     patch.object(route, "cancel_training_job", self.legacy_cancel), self.rp.installed(),
                     self.modal.installed()]
        for cm in self._cms:
            cm.__enter__()

    async def asyncTearDown(self):
        for cm in reversed(self._cms):
            cm.__exit__(None, None, None)

    async def test_managed_rows_go_through_the_cas(self):
        queued = self.db.add_training(user_id="u-1", status="queued")
        running = self.db.add_training(user_id="u-1", status="running", current_attempt=1)
        self.db.add_attempt(running["id"], 1, state="running", backend="runpod_secure", cloud_ref="pod-r")
        self.rp.add_pod(id="pod-r")
        publishing = self.db.add_training(user_id="u-1", status="running", current_attempt=1)
        self.db.add_attempt(publishing["id"], 1, state="ended", end_reason="succeeded", backend="modal", cloud_ref="fc")
        legacy = self.db.add_training(user_id="u-1", status="running", managed=False, cloud_job_id="fc-L")
        resp = await route.delete_my_account(profile=dict(PROFILE))
        body = resp if isinstance(resp, dict) else content(resp)
        self.assertEqual(sorted(body["canceled_trainings"]), sorted([queued["id"], running["id"], legacy["id"]]))
        self.assertEqual(self.db.row(queued["id"])["status"], "canceled")
        self.assertEqual(self.db.row(running["id"])["status"], "canceled")
        self.assertIn("pod-r", self.rp.deletes())
        self.assertEqual(self.db.row(publishing["id"])["status"], "running", "P0051: its publish finishes")
        self.assertEqual(self.db.row(legacy["id"])["status"], "canceled")
        self.assertEqual(self.db.row(legacy["id"])["error_message"], "Automatisch abgebrochen: Kontolöschung angefordert.")
        self.legacy_cancel.assert_awaited_once_with("fc-L")
        # no direct status write on a managed row
        for op in self.db.ops:
            if op[0] == "update" and op[1] == "trainings":
                ids = [v for k, c, v, _ in op[2] if c == "id"]
                self.assertNotIn(queued["id"], ids)
                self.assertNotIn(running["id"], ids)
        names = [n for n, _ in self.db.rpcs]
        self.assertEqual(names.count("cancel_request_training"), 3)
        self.assertEqual(names.count("complete_cancel_training"), 2)
        # 052: the first stop request records who asked (§4.10)
        self.assertEqual({c.get("p_actor_role") for c in self.db.calls("cancel_request_training")}, {"student"})
        self.assertEqual(self.db.row(running["id"])["stop_requested_by_role"], "student")


if __name__ == "__main__":
    unittest.main()

"""The boot-time schema fingerprint knows migration 052 (the training-hour ledger), and a
42501 fails the boot (review B1).

PostgREST resolves an RPC on (name, argument-name set), so every new or widened probe
names EXACTLY the parameters of 052's ``CREATE OR REPLACE FUNCTION public.<name>(…)``
(read from A's migration file, never retyped), and its values (dummy ids, training 0)
answer P0002 / P0066 / P0067 / an empty result and write nothing (proven through
PostgREST on the review-2 build, spec §12.1). A database without 052 answers PGRST202.
An RPC the service role cannot execute (42501, „permission denied" — an INVOKER caller
of a revoked internal helper) used to read as „exists" and passed the deploy gate; the
probe now treats it like a missing function. 052's fix round 2 adds ``mark_checkpoint_ready``
(V3-M1), probed with training 0 (false, nothing written). The 23 / 23 / 56 totals are pinned in
``test_schema_probe_040.py``.
"""

from __future__ import annotations

import ast
import os
import re
import unittest
from types import SimpleNamespace

from app.tests.test_schema_probe_040 import MAIN_PY, _probe_tuples

HERE = os.path.dirname(os.path.abspath(__file__))
MIGRATION = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "supabase", "migrations",
                         "20261028120000_052_credit_ledger.sql")
_DUMMY = "00000000-0000-0000-0000-000000000000"
NEW_RPCS = ("credit_quota", "credit_admin_grant", "credit_admin_revoke", "credit_assign", "credit_reclaim",
            "credit_set_member_limit", "credit_teacher_summary", "credit_history", "stop_training",
            "record_training_refusal", "credit_reconcile", "credit_admin_compensate", "credit_erase_reason")
WIDENED_RPCS = ("start_training_safe", "start_continuation_safe", "finish_training", "cancel_request_training")
DROPPED_RPCS = ("get_remaining_credits", "get_teacher_credit_summary", "adjust_student_credits",
                "adjust_workgroup_credits")


def _sql() -> str:
    with open(MIGRATION, encoding="utf-8") as fh:
        return fh.read()


def _params(sql: str, name: str) -> list:
    m = re.search(rf"CREATE OR REPLACE FUNCTION public\.{name}\((.*?)\)\s*RETURNS", sql, re.S)
    assert m, name
    body = m.group(1).strip()
    return [p.split()[0] for p in body.split(",")] if body else []


def _value(v):
    if isinstance(v, ast.Name) and v.id == "dummy":
        return _DUMMY
    if isinstance(v, ast.BinOp):
        return None
    return ast.literal_eval(v)


class TestSchemaProbeKnows052(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        probe = _probe_tuples()
        cls.tables = [ast.literal_eval(e) for e in probe["required_tables"].elts]
        cls.columns = [ast.literal_eval(e) for e in probe["required_columns"].elts]
        cls.rpcs = {ast.literal_eval(e.elts[0]): {ast.literal_eval(k): _value(v)
                                                 for k, v in zip(e.elts[1].keys, e.elts[1].values)}
                    for e in probe["required_rpcs"].elts}
        cls.sql = _sql()

    def test_the_four_ledger_tables_are_probed(self):
        for t in ("credit_accounts", "credit_ledger", "credit_reasons", "training_start_refusals"):
            self.assertIn(t, self.tables)

    def test_the_five_column_sets_and_config_complaints(self):
        sets = dict.fromkeys(self.columns)
        expected = [
            ("trainings", "credit_scheme, reserved_hours, charged_hours, settled_at, idempotency_key, "
                          "stop_requested_at, stop_requested_by_role, lost_seconds, lost_reasons, charge_waiver"),
            ("credit_accounts", "kind, owner_id, parent_teacher_id, free, reserved, spent, member_hour_limit, "
                                "closed_at, created_at"),
            ("credit_ledger", "id, at, kind, hours, from_kind, from_id, from_bucket, to_kind, to_id, to_bucket, "
                              "training_id, member_id, actor_id, actor_role, request_id, reason_id, meta"),
            ("credit_reasons", "id, text, subject_kind, subject_id, created_at"),
            ("training_start_refusals", "id, at, user_id, role, request_kind, code, model_type"),
        ]
        for pair in expected:
            self.assertIn(pair, sets)
        ckpt = [c for t, c in self.columns if t == "training_checkpoints"]
        self.assertEqual(len(ckpt), 1)
        self.assertTrue(ckpt[0].endswith("config_complaints"))

    def test_every_probed_052_column_exists_in_the_migration(self):
        for table, cols in self.columns:
            names = [c.strip() for c in cols.split(",")]
            if table in ("credit_accounts", "credit_ledger", "credit_reasons", "training_start_refusals"):
                pass
            elif table == "trainings" and names[0] == "credit_scheme":
                pass
            elif table == "training_checkpoints":
                names = ["config_complaints"]
            else:
                continue
            for col in names:
                with self.subTest(table=table, col=col):
                    self.assertRegex(self.sql, rf"(?m)(^|[\s(,]){re.escape(col)}\s+"
                                               r"(TEXT|UUID|INT|BIGINT|TIMESTAMPTZ|JSONB|BOOLEAN)\b")

    def test_the_thirteen_new_rpcs_name_exactly_052s_parameters(self):
        for name in NEW_RPCS:
            with self.subTest(rpc=name):
                self.assertIn(name, self.rpcs)
                self.assertEqual(sorted(self.rpcs[name]), sorted(_params(self.sql, name)))

    def test_the_widened_rpcs_name_052s_parameters(self):
        expected_len = {"start_training_safe": 10, "start_continuation_safe": 4, "finish_training": 9,
                        "cancel_request_training": 3}
        for name in WIDENED_RPCS:
            with self.subTest(rpc=name):
                self.assertEqual(sorted(self.rpcs[name]), sorted(_params(self.sql, name)))
                self.assertEqual(len(self.rpcs[name]), expected_len[name])

    def test_the_four_pre_052_credit_rpcs_are_no_longer_probed(self):
        for name in DROPPED_RPCS:
            self.assertNotIn(name, self.rpcs)

    def test_the_probe_values_write_nothing(self):
        r = self.rpcs
        self.assertEqual(r["stop_training"]["p_training_id"], 0)
        self.assertEqual(r["stop_training"]["p_actor_role"], "student")
        self.assertEqual(r["credit_history"], {"p_scope": "student", "p_id": _DUMMY, "p_before": None, "p_limit": 1})
        self.assertEqual(r["credit_reconcile"], {})
        self.assertEqual(r["credit_erase_reason"]["p_reason_id"], 0)
        self.assertEqual(r["credit_admin_compensate"]["p_reason"], "_probe")
        self.assertEqual(r["start_training_safe"]["p_idempotency_key"], None)
        self.assertEqual(r["start_training_safe"]["p_max_hours"], 3)
        self.assertEqual(r["start_continuation_safe"]["p_max_hours"], 3)
        self.assertIsNone(r["finish_training"]["p_charge_waiver"])
        self.assertIsNone(r["cancel_request_training"]["p_actor_role"])
        for name in ("credit_admin_grant", "credit_admin_revoke", "credit_admin_compensate", "credit_erase_reason"):
            self.assertEqual(r[name]["p_admin_id"], _DUMMY, name)  # a dummy admin: P0067
        for name in ("credit_assign", "credit_reclaim", "credit_set_member_limit"):
            self.assertEqual(r[name]["p_teacher_id"], _DUMMY, name)  # a dummy teacher: P0066

    def test_mark_checkpoint_ready_is_probed_and_writes_nothing(self):
        # V3-M1: training 0 never exists, so the call answers false and writes nothing.
        # The contract is (p_training_id INT, p_attempt INT, p_step INT) RETURNS BOOLEAN;
        # once 052 defines it, the probe must name exactly its parameters.
        self.assertEqual(self.rpcs["mark_checkpoint_ready"], {"p_training_id": 0, "p_attempt": 1, "p_step": 1})
        if re.search(r"CREATE OR REPLACE FUNCTION public\.mark_checkpoint_ready\(", self.sql):
            self.assertEqual(sorted(self.rpcs["mark_checkpoint_ready"]),
                             sorted(_params(self.sql, "mark_checkpoint_ready")))
            self.assertRegex(self.sql, r"FUNCTION public\.mark_checkpoint_ready\(p_training_id INT, p_attempt INT, "
                                       r"p_step INT\)\s*RETURNS BOOLEAN")

    def test_every_probed_rpc_is_a_callable_052_function_or_older(self):
        # a function 052 defines must not be one of its internal helpers (revoked from the
        # service role: its probe would answer 42501 and stop the boot by design)
        for name in self.rpcs:
            self.assertFalse(name.startswith("_credit"), name)


class TestFortyTwoFiveOhOneFailsTheBoot(unittest.TestCase):
    """The RPC verdict: PGRST202 / „does not exist" = missing, 42501 / „permission denied"
    = not executable; both stop the boot naming the function; any other answer = exists.
    ``_validate_required_schema`` itself is executed (extracted from main.py with ast,
    which needs the full app deps to import) against a fake Supabase client."""

    @classmethod
    def setUpClass(cls):
        import logging
        import re as _re

        with open(MAIN_PY, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=MAIN_PY)
        wanted = {"_probe_rpc_verdict", "_validate_required_schema", "_sanitize_probe_error"}
        fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
        assert {f.name for f in fns} == wanted
        ns: dict = {"os": os, "re": _re, "logger": logging.getLogger("probe-test")}
        exec(compile(ast.Module(body=fns, type_ignores=[]), MAIN_PY, "exec"), ns)
        cls.ns = ns

    class _Err(Exception):
        def __init__(self, msg, code=None):
            super().__init__(msg)
            self.code = code

    def _client(self, rpc_errors: dict):
        class _Q:
            def select(self, *a, **k):
                return self

            def limit(self, *a):
                return self

            def execute(self):
                return SimpleNamespace(data=[])

        class _Call:
            def __init__(self, exc):
                self.exc = exc

            def execute(self):
                if self.exc is not None:
                    raise self.exc
                return SimpleNamespace(data=[])

        calls = []

        def rpc(name, args):
            calls.append(name)
            return _Call(rpc_errors.get(name))

        return SimpleNamespace(table=lambda name: _Q(), rpc=rpc), calls

    def _run(self, rpc_errors):
        import sys
        import types
        from unittest.mock import patch

        sb, calls = self._client(rpc_errors)
        mod = types.ModuleType("app.services.supabase_client")
        mod.get_supabase = lambda: sb
        with patch.dict(sys.modules, {"app.services.supabase_client": mod}), \
                patch.dict(os.environ, {"EDUBOTICS_SKIP_SCHEMA_CHECK": ""}):
            self.ns["_validate_required_schema"]()
        return calls

    def test_the_verdicts(self):
        E = self._Err
        cases = [
            (None, "ok"),
            (E("{'code': 'PGRST202', 'message': 'Could not find the function'}"), "missing"),
            (E("function public.credit_quota(uuid, integer) does not exist"), "missing"),
            (E("{'code': '42501', 'message': 'permission denied for function _credit_startable'}"), "denied"),
            (E("permission denied for function credit_quota"), "denied"),
            (E("x", code="42501"), "denied"),
            (E("{'code': 'P0002', 'message': 'not found'}"), "ok"),
            (E("{'code': 'P0067', 'message': 'not an admin'}"), "ok"),
            (E("{'code': 'P0066'}"), "ok"),
            (E("{'code': 'P0049'}"), "ok"),
        ]
        sb_for = lambda exc: SimpleNamespace(rpc=lambda n, a: SimpleNamespace(  # noqa: E731
            execute=lambda: (_ for _ in ()).throw(exc) if exc is not None else None))
        for exc, want in cases:
            with self.subTest(exc=str(exc)):
                self.assertEqual(self.ns["_probe_rpc_verdict"](sb_for(exc), "credit_quota", {}), want)

    def test_a_healthy_database_boots_and_every_rpc_is_asked(self):
        calls = self._run({})
        self.assertEqual(len(calls), 56)
        self.assertIn("mark_checkpoint_ready", calls)
        for name in NEW_RPCS + WIDENED_RPCS:
            self.assertIn(name, calls)

    def test_42501_fails_the_boot_naming_the_function(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._run({"credit_quota": self._Err("{'code': '42501', 'message': 'permission denied for function "
                                                 "_credit_startable'}", code="42501")})
        self.assertIn("credit_quota", str(ctx.exception))
        self.assertIn("42501", str(ctx.exception))

    def test_a_missing_052_function_fails_the_boot_too(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._run({"stop_training": self._Err("{'code': 'PGRST202'}")})
        self.assertIn("stop_training", str(ctx.exception))

    def test_the_expected_refusals_of_the_probe_values_pass(self):
        errs = {"credit_admin_grant": self._Err("{'code': 'P0067'}"),
                "credit_assign": self._Err("{'code': 'P0066'}"),
                "stop_training": self._Err("{'code': 'P0002'}"),
                "finish_training": self._Err("{'code': 'P0049'}")}
        self.assertEqual(len(self._run(errs)), 56)


if __name__ == "__main__":
    unittest.main()

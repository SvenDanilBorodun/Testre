"""The boot-time schema fingerprint knows migration 050 (the capped publish + „Fortsetzen").

A deploy that lands the 050 routes before the migration would pass ``/health``
and 500 on the first cap, continuation or 8-argument finish; the probe aborts
the Railway deploy with a named cause instead. PostgREST resolves an RPC on
(name, argument-name set), so the two new RPCs name EXACTLY 050's parameters,
and their values hit training id 0 / a dummy user, which never exist, so each
answers P0049 / P0002 and writes nothing (proven through PostgREST on a local
stack, §16 P5). The 22 / 22 / 45 totals are pinned in ``test_schema_probe_040.py``. Migration
052 widened start_continuation_safe (p_max_hours) and finish_training (p_charge_waiver); both
probes name 052's shape, which keeps 050's parameters as a subset.
"""

from __future__ import annotations

import ast
import os
import re
import unittest

from app.tests.test_schema_probe_040 import _probe_tuples

HERE = os.path.dirname(os.path.abspath(__file__))
MIGRATION = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "supabase", "migrations",
                         "20261021120000_050_training_continuation.sql")
MIGRATION_052 = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "supabase", "migrations",
                             "20261028120000_052_credit_ledger.sql")
_DUMMY = "00000000-0000-0000-0000-000000000000"
NEW_RPCS = ("cap_training_attempt", "start_continuation_safe")


def _sql() -> str:
    with open(MIGRATION, encoding="utf-8") as fh:
        return fh.read()


def _params(sql: str, name: str) -> list:
    m = re.search(rf"CREATE OR REPLACE FUNCTION public\.{name}\((.*?)\)\s*RETURNS", sql, re.S)
    assert m, name
    return [p.split()[0] for p in m.group(1).split(",")]


class TestSchemaProbeKnows050(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        probe = _probe_tuples()
        cls.tables = [ast.literal_eval(e) for e in probe["required_tables"].elts]
        cls.columns = [ast.literal_eval(e) for e in probe["required_columns"].elts]
        cls.rpcs = {}
        for e in probe["required_rpcs"].elts:
            args = {}
            for k, v in zip(e.elts[1].keys, e.elts[1].values):
                if isinstance(v, ast.Name) and v.id == "dummy":
                    args[ast.literal_eval(k)] = _DUMMY
                elif isinstance(v, ast.BinOp):
                    args[ast.literal_eval(k)] = None
                else:
                    args[ast.literal_eval(k)] = ast.literal_eval(v)
            cls.rpcs[ast.literal_eval(e.elts[0])] = args
        cls.sql = _sql()

    def test_the_kept_table_is_probed(self):
        self.assertIn("training_kept_checkpoints", self.tables)

    def test_the_two_column_sets_are_probed(self):
        self.assertIn(("trainings", "capped_step, continuable_until, continued_from"), self.columns)
        self.assertIn(("training_kept_checkpoints",
                       "training_id, step, manifest, state, kept_at, expires_at, refused_reason, bad_attempts"),
                      self.columns)

    def test_the_two_new_rpcs_name_exactly_the_latest_parameters(self):
        with open(MIGRATION_052, encoding="utf-8") as fh:
            sql052 = fh.read()
        latest = {"cap_training_attempt": self.sql, "start_continuation_safe": sql052}
        for name in NEW_RPCS:
            with self.subTest(rpc=name):
                self.assertIn(name, self.rpcs)
                self.assertEqual(sorted(self.rpcs[name]), sorted(_params(latest[name], name)))
                self.assertLessEqual(set(_params(self.sql, name)), set(self.rpcs[name]), "050's still resolve")
        # cap_training_attempt kept its signature in 052
        self.assertEqual(sorted(_params(sql052, "cap_training_attempt")), sorted(_params(self.sql, "cap_training_attempt")))

    def test_finish_training_is_probed_with_all_nine_arguments(self):
        with open(MIGRATION_052, encoding="utf-8") as fh:
            sql052 = fh.read()
        self.assertEqual(sorted(self.rpcs["finish_training"]), sorted(_params(sql052, "finish_training")))
        self.assertEqual(len(self.rpcs["finish_training"]), 9)
        self.assertLessEqual(set(_params(self.sql, "finish_training")), set(self.rpcs["finish_training"]))

    def test_the_probe_values_write_nothing(self):
        self.assertEqual(self.rpcs["cap_training_attempt"]["p_training_id"], 0)
        self.assertEqual(self.rpcs["cap_training_attempt"]["p_reason"], "capped")
        self.assertEqual(self.rpcs["start_continuation_safe"]["p_parent_id"], 0)
        self.assertEqual(self.rpcs["start_continuation_safe"]["p_user_id"], _DUMMY)


if __name__ == "__main__":
    unittest.main()

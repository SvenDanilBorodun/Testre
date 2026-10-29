"""The boot-time schema fingerprint knows migration 049 (managed ACT training).

A deploy that lands the managed routes before 049 would pass ``/health`` and
500 on the first managed start or worker call; the probe aborts the Railway
deploy with a named cause instead. PostgREST resolves an RPC on (name,
argument-name set), so every probe names EXACTLY the migration's parameters
(else every deploy reports the function missing), and its values hit training
id 0, which never exists, so each call answers P0002, P0049 or false and
writes nothing (proven through PostgREST on a local stack, spec §3.1).

The 23 / 23 / 55 totals are pinned in ``test_schema_probe_040.py``. Migration 052
re-created start_training_safe (ten arguments), finish_training (nine) and
cancel_request_training (three), so those three probes name 052's parameters
(LATEST_DEFINITION; the new ones are checked in ``test_schema_probe_052.py``).
"""

from __future__ import annotations

import ast
import os
import re
import unittest

from app.tests.test_schema_probe_040 import _probe_tuples

HERE = os.path.dirname(os.path.abspath(__file__))
MIGRATION = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "supabase", "migrations",
                         "20261020130000_049_managed_training.sql")
MIGRATION_050 = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "supabase", "migrations",
                             "20261021120000_050_training_continuation.sql")
MIGRATION_052 = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "supabase", "migrations",
                             "20261028120000_052_credit_ledger.sql")
# 050 widened finish_training to 8 arguments, 052 to 9 (p_charge_waiver) and re-created
# start_training_safe (10) and cancel_request_training (3, p_actor_role); the probe names
# the latest shape, so its parameter source is the 052 text.
LATEST_DEFINITION = {"finish_training": MIGRATION_052, "start_training_safe": MIGRATION_052,
                     "cancel_request_training": MIGRATION_052}
NEW_RPCS = ("claim_training_attempt", "attach_training_attempt", "release_training_attempt", "requeue_training",
            "end_training_attempt", "finish_training", "cancel_request_training", "complete_cancel_training",
            "claim_training_publish", "claim_checkpoint_mirror")
_DUMMY = "00000000-0000-0000-0000-000000000000"


def _sql() -> str:
    with open(MIGRATION, encoding="utf-8") as fh:
        return fh.read()


def _params(sql: str, name: str) -> list:
    m = re.search(rf"CREATE OR REPLACE FUNCTION public\.{name}\((.*?)\)\s*RETURNS", sql, re.S)
    assert m, name
    return [p.split()[0] for p in m.group(1).split(",")]


class TestSchemaProbeKnows049(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        probe = _probe_tuples()
        cls.tables = [ast.literal_eval(e) for e in probe["required_tables"].elts]
        cls.columns = [ast.literal_eval(e) for e in probe["required_columns"].elts]
        cls.rpcs = {}
        for e in probe["required_rpcs"].elts:
            name = ast.literal_eval(e.elts[0])
            args = {}
            for k, v in zip(e.elts[1].keys, e.elts[1].values):
                if isinstance(v, ast.Name) and v.id == "dummy":
                    args[ast.literal_eval(k)] = _DUMMY
                elif isinstance(v, ast.BinOp):  # 048's "e1.00000000." + "A" * 24
                    args[ast.literal_eval(k)] = None
                else:
                    args[ast.literal_eval(k)] = ast.literal_eval(v)
            cls.rpcs[name] = args
        cls.sql = _sql()

    def test_the_two_tables_are_probed(self):
        self.assertIn("training_attempts", self.tables)
        self.assertIn("training_checkpoints", self.tables)

    def test_the_three_column_sets(self):
        self.assertIn(("trainings", "managed, current_attempt, train_seconds_used, status_note, step_advanced_at, "
                                    "dataset_fps, unplaced_since"), self.columns)
        self.assertIn(("training_attempts",
                       "training_id, attempt_no, backend, cloud_ref, gpu_type, data_center, cuda_version, cost_per_hr, "
                       "state, created_at, started_at, first_heartbeat_at, ended_at, end_reason, resume_from_step, "
                       "resume_from_attempt, allowed_dcs, publish_claimed_at, publish_owner, publish_first_at, "
                       "publish_tries"), self.columns)
        # 052 (R3-2) appended config_complaints to the 049 set
        self.assertIn(("training_checkpoints",
                       "training_id, attempt_no, step, state, manifest, created_at, ready_at, mirrored_at, mirror_tries, "
                       "refused_reason, refused_by, crash_attempts, mirror_claimed_at, mirror_owner, "
                       "config_complaints"), self.columns)

    def test_every_probed_column_exists_in_the_migration(self):
        for table, cols in self.columns:
            if table not in ("training_attempts", "training_checkpoints") and not cols.startswith("managed"):
                continue
            for col in (c.strip() for c in cols.split(",")):
                if col == "config_complaints":
                    continue  # 052's (test_schema_probe_052.py)
                with self.subTest(table=table, col=col):
                    self.assertRegex(self.sql, rf"\n  (ADD COLUMN IF NOT EXISTS )?{col}\s+[A-Z]")

    def test_the_ten_rpcs_name_exactly_the_migrations_parameters(self):
        for name in NEW_RPCS:
            with self.subTest(rpc=name):
                self.assertIn(name, self.rpcs)
                if name in LATEST_DEFINITION:
                    with open(LATEST_DEFINITION[name], encoding="utf-8") as fh:
                        sql = fh.read()
                else:
                    sql = self.sql
                self.assertEqual(sorted(self.rpcs[name]), sorted(_params(sql, name)))

    def test_start_training_safe_probes_the_ten_argument_shape(self):
        with open(MIGRATION_052, encoding="utf-8") as fh:
            sql = fh.read()
        self.assertEqual(sorted(self.rpcs["start_training_safe"]), sorted(_params(sql, "start_training_safe")))
        self.assertEqual(len(self.rpcs["start_training_safe"]), 10)
        self.assertIs(self.rpcs["start_training_safe"]["p_managed"], False)
        # 049's eight still resolve against 052 (an older API's call): a subset
        self.assertLessEqual(set(_params(self.sql, "start_training_safe")), set(self.rpcs["start_training_safe"]))

    def test_the_probe_values_write_nothing(self):
        for name in NEW_RPCS:
            self.assertEqual(self.rpcs[name]["p_training_id"], 0, name)
        self.assertEqual(self.rpcs["cancel_request_training"]["p_user_id"], _DUMMY)
        self.assertIn(self.rpcs["attach_training_attempt"]["p_backend"], ("runpod_community", "runpod_secure", "modal"))
        self.assertEqual(self.rpcs["finish_training"]["p_status"], "failed")
        self.assertEqual(self.rpcs["complete_cancel_training"]["p_final"], "canceled")


if __name__ == "__main__":
    unittest.main()

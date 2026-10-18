"""The boot-time schema fingerprint knows migration 049 (managed ACT training).

A deploy that lands the managed routes before 049 would pass ``/health`` and
500 on the first managed start or worker call; the probe aborts the Railway
deploy with a named cause instead. PostgREST resolves an RPC on (name,
argument-name set), so every probe names EXACTLY the migration's parameters
(else every deploy reports the function missing), and its values hit training
id 0, which never exists, so each call answers P0002, P0049 or false and
writes nothing (proven through PostgREST on a local stack, spec §3.1).

The 18 / 16 / 44 totals are pinned in ``test_schema_probe_040.py``.
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
        self.assertIn(("training_checkpoints",
                       "training_id, attempt_no, step, state, manifest, created_at, ready_at, mirrored_at, mirror_tries, "
                       "refused_reason, refused_by, crash_attempts, mirror_claimed_at, mirror_owner"), self.columns)

    def test_every_probed_column_exists_in_the_migration(self):
        for table, cols in self.columns:
            if table not in ("training_attempts", "training_checkpoints") and not cols.startswith("managed"):
                continue
            for col in (c.strip() for c in cols.split(",")):
                with self.subTest(table=table, col=col):
                    self.assertRegex(self.sql, rf"\n  (ADD COLUMN IF NOT EXISTS )?{col}\s+[A-Z]")

    def test_the_ten_rpcs_name_exactly_the_migrations_parameters(self):
        for name in NEW_RPCS:
            with self.subTest(rpc=name):
                self.assertIn(name, self.rpcs)
                self.assertEqual(sorted(self.rpcs[name]), sorted(_params(self.sql, name)))

    def test_start_training_safe_probes_the_eight_argument_shape(self):
        self.assertEqual(sorted(self.rpcs["start_training_safe"]), sorted(_params(self.sql, "start_training_safe")))
        self.assertIs(self.rpcs["start_training_safe"]["p_managed"], False)

    def test_the_probe_values_write_nothing(self):
        for name in NEW_RPCS:
            self.assertEqual(self.rpcs[name]["p_training_id"], 0, name)
        self.assertEqual(self.rpcs["cancel_request_training"]["p_user_id"], _DUMMY)
        self.assertIn(self.rpcs["attach_training_attempt"]["p_backend"], ("runpod_community", "runpod_secure", "modal"))
        self.assertEqual(self.rpcs["finish_training"]["p_status"], "failed")
        self.assertEqual(self.rpcs["complete_cancel_training"]["p_final"], "canceled")


if __name__ == "__main__":
    unittest.main()

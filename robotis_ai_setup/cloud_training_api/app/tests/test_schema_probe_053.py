"""The boot-time schema fingerprint knows migration 053 (the classroom Jetson is retired).

053 drops public.jetsons, its eleven RPCs and inference_runs.jetson_id. The probe
must name none of them: an API whose probe still asked for them would SIGTERM itself
on every database the migration reached. And every object the probe used to name for
the Jetson is dropped by 053 — the two lists are the same set, read from the files,
never retyped. The 22 / 22 / 45 totals are pinned in ``test_schema_probe_040.py``.
"""

from __future__ import annotations

import ast
import os
import re
import unittest

from app.tests.test_schema_probe_040 import _probe_tuples

HERE = os.path.dirname(os.path.abspath(__file__))
SUPABASE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "supabase")
MIGRATION_053 = os.path.join(SUPABASE, "migrations", "20261109120000_053_retire_jetson.sql")
ROLLBACK_053 = os.path.join(SUPABASE, "rollback", "20261109120000_053_retire_jetson_rollback.sql")

# The eleven RPCs migrations 019, 020 and 022 created, which the probe named until 053.
JETSON_RPCS = {
    "claim_jetson", "release_jetson", "heartbeat_jetson", "agent_heartbeat_jetson",
    "pair_jetson", "claim_pair_intent", "force_release_jetson", "sweep_jetson_locks",
    "agent_release_jetson", "regenerate_pairing_code", "unpair_jetson",
}


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _code(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


class TestSchemaProbeKnows053(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        probe = _probe_tuples()
        cls.tables = [ast.literal_eval(e) for e in probe["required_tables"].elts]
        cls.columns = [ast.literal_eval(e) for e in probe["required_columns"].elts]
        cls.rpcs = [ast.literal_eval(e.elts[0]) for e in probe["required_rpcs"].elts]

    def test_the_probe_names_no_jetson_object(self) -> None:
        self.assertNotIn("jetsons", self.tables)
        self.assertEqual([c for c in self.columns if c[0] == "jetsons" or "jetson" in c[1]], [])
        self.assertEqual(JETSON_RPCS & set(self.rpcs), set())

    def test_inference_runs_stays_probed(self) -> None:
        self.assertIn(("inference_runs", "id, student_user_id, policy_repo, exit_reason"), self.columns)
        self.assertIn(("inference_runs", "backend"), self.columns)

    def test_053_drops_every_jetson_rpc_the_table_and_the_column(self) -> None:
        code = _code(_read(MIGRATION_053))
        dropped = set(re.findall(r"DROP FUNCTION IF EXISTS public\.(\w+)\(", code))
        self.assertEqual(dropped, JETSON_RPCS)
        self.assertIn("DROP TABLE IF EXISTS public.jetsons;", code)
        self.assertIn("DROP COLUMN IF EXISTS jetson_id;", code)
        self.assertIn("ALTER PUBLICATION supabase_realtime DROP TABLE public.jetsons;", code)
        # inference_runs and its backend CHECK stay
        self.assertNotIn("DROP TABLE IF EXISTS public.inference_runs", code)
        self.assertNotIn("DROP CONSTRAINT", code)

    def test_the_rollback_recreates_every_rpc_053_drops(self) -> None:
        code = _code(_read(ROLLBACK_053))
        created = set(re.findall(r"CREATE OR REPLACE FUNCTION public\.(\w+)\(", code))
        self.assertEqual(created, JETSON_RPCS)
        self.assertIn("CREATE TABLE IF NOT EXISTS public.jetsons (", code)
        self.assertIn("ADD COLUMN IF NOT EXISTS jetson_id UUID REFERENCES public.jetsons(id) ON DELETE SET NULL;", code)
        self.assertIn("ALTER PUBLICATION supabase_realtime ADD TABLE public.jetsons;", code)
        self.assertEqual(code.count("CREATE POLICY"), 4)


if __name__ == "__main__":
    unittest.main()

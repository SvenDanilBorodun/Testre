"""The boot-time schema fingerprint (``main.py::_validate_required_schema``)
knows migration 048 (Cloud-Inferenz).

A deploy that lands /inference/* before the migration would pass ``/health``
and 500 on the first reservation (the c56c012 class); the probe is what aborts
the Railway deploy with a named cause instead. PostgREST resolves an RPC on
(name, argument-name set), so every probe must name EXACTLY the migration's
parameters, or every deploy reports the function missing. The probe values are
the ones proved through PostgREST v14.10 against the applied 048 to answer
without PGRST202 and to write nothing (spec §4.3).

The 16 / 13 / 34 counts are pinned in ``test_schema_probe_040.py``.
"""

from __future__ import annotations

import ast
import unittest

from app.tests.test_inference_migration_sql import (
    ANON_RPCS, MIGRATION, SERVICE_RPCS, _read, _strip_comments, migration_functions,
)
from app.tests.test_schema_probe_040 import _probe_tuples

_SESSION_COLUMNS = (
    "id, user_id, training_id, model_repo, model_revision, status, token_hash, modal_session_id, "
    "edge_token_enc, edge_terminated_at, session_s, reserve_until, connected_at, max_end_at, ended_at, "
    "end_reason, active_seconds, run_logged_at"
)
_DUMMY = "00000000-0000-0000-0000-000000000000"


class TestSchemaProbeKnows048(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        probe = _probe_tuples()
        cls.tables = [ast.literal_eval(e) for e in probe["required_tables"].elts]
        cls.columns = [ast.literal_eval(e) for e in probe["required_columns"].elts]
        cls.rpcs = {}
        for e in probe["required_rpcs"].elts:
            name = ast.literal_eval(e.elts[0])
            args_node = e.elts[1]
            args = {}
            for k, v in zip(args_node.keys, args_node.values):
                key = ast.literal_eval(k)
                if isinstance(v, ast.Name) and v.id == "dummy":
                    args[key] = _DUMMY
                else:
                    try:
                        args[key] = ast.literal_eval(v)
                    except ValueError:
                        args[key] = eval(compile(ast.Expression(v), "<probe>", "eval"))  # "e1.00000000." + "A" * 24
            cls.rpcs[name] = args
        cls.migration = migration_functions(_strip_comments(_read(MIGRATION)))

    def test_schema_probe_048(self) -> None:
        self.assertIn("inference_sessions", self.tables)
        self.assertIn(("inference_sessions", _SESSION_COLUMNS), self.columns)
        self.assertIn(("inference_runs", "backend"), self.columns)
        for name in {**SERVICE_RPCS, **ANON_RPCS}:
            self.assertIn(name, self.rpcs)
            self.assertEqual(sorted(self.rpcs[name]), sorted(p for p, _ in self.migration[name]), name)

    def test_every_probed_column_exists_in_the_migration(self) -> None:
        sql = _read(MIGRATION)
        for col in (c.strip() for c in _SESSION_COLUMNS.split(",")):
            self.assertRegex(sql, rf"\n  {col}\s+(UUID|INT|TEXT|TIMESTAMPTZ)", col)

    def test_the_probe_values_write_nothing(self) -> None:
        # The ids are the dummy (P0002 / `missing` / false), the anon tokens fail the
        # shape test (`invalid`), the reads are reads.
        self.assertEqual(self.rpcs["create_inference_session_safe"]["p_user_id"], _DUMMY)
        self.assertEqual(self.rpcs["create_inference_session_safe"]["p_reserve_s"], 60)
        self.assertEqual(self.rpcs["attach_inference_edge"]["p_session_id"], _DUMMY)
        self.assertRegex(self.rpcs["attach_inference_edge"]["p_edge_token_enc"], r"\Ae1\.[0-9a-f]{8}\.[A-Za-z0-9_-]{24,}\Z")
        self.assertRegex(self.rpcs["attach_inference_edge"]["p_modal_session_id"], r"\Asd-[A-Za-z0-9_-]{1,128}\Z")
        self.assertEqual(self.rpcs["arm_inference_session"]["p_token_hash"], "0" * 64)
        self.assertEqual(self.rpcs["inference_edges_due"], {"p_min_age_s": 3600, "p_limit": 1})
        self.assertEqual(self.rpcs["sweep_inference_sessions"], {})
        for name in ANON_RPCS:
            self.assertEqual(self.rpcs[name]["p_token"], "_probe")  # not 43 chars: `invalid` before any read
        # p_stats is ALWAYS sent (omitting it makes PostgREST answer 404/PGRST202)
        self.assertIn("p_stats", self.rpcs["inference_session_heartbeat"])
        self.assertIn("p_stats", self.rpcs["inference_session_end"])


if __name__ == "__main__":
    unittest.main()

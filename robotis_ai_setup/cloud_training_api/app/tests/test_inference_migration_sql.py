"""Migration 048 (Cloud-Inferenz sessions): the shape the API and the Modal app rely on.

Read from the SQL text (stdlib only; the behaviour is proved by the hand-run
``supabase/tests/048_inference_sessions_assertions.sql`` on a local stack):

  * the file sits at the timestamped name after 046, with its rollback beside it
  * ``public.inference_sessions`` has RLS on, every privilege revoked from the
    request roles, no policy, no table grant and no publication (Rule §4)
  * the thirteen RPCs carry exactly the frozen argument names of spec §4.2, in
    order (PostgREST resolves a call on the name set: a renamed argument is a
    missing function for every caller)
  * anon executes exactly the four anon RPCs, the nine service RPCs are granted
    to service_role only, the three internal helpers to nobody
  * the end_reason CHECK names the eleven reasons; the rollback drops every
    function the migration creates, the table and the new column
"""

from __future__ import annotations

import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SUPABASE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "supabase")
MIGRATION = os.path.join(SUPABASE, "migrations", "20261011120000_048_inference_sessions.sql")
ROLLBACK = os.path.join(SUPABASE, "rollback", "20261011120000_048_inference_sessions_rollback.sql")
ASSERTIONS = os.path.join(SUPABASE, "tests", "048_inference_sessions_assertions.sql")

# spec §4.2 — names and argument names are a frozen contract (C3)
SERVICE_RPCS = {
    "create_inference_session_safe": [
        "p_user_id", "p_training_id", "p_model_repo", "p_model_revision", "p_policy_type",
        "p_token_hash", "p_max_session_s", "p_daily_cap_s", "p_global_cap", "p_reserve_s",
    ],
    "attach_inference_edge": ["p_session_id", "p_modal_session_id", "p_edge_token_enc"],
    "arm_inference_session": ["p_session_id", "p_user_id", "p_token_hash"],
    "end_inference_session": ["p_session_id", "p_user_id", "p_reason"],
    "heartbeat_inference_session_client": ["p_session_id", "p_user_id"],
    "inference_usage": ["p_user_id", "p_daily_cap_s"],
    "sweep_inference_sessions": [],
    "inference_edges_due": ["p_min_age_s", "p_limit"],
    "mark_inference_edge_terminated": ["p_session_id"],
}
ANON_RPCS = {
    "inference_session_peek": ["p_token", "p_modal_session_id"],
    "inference_session_connect": ["p_token", "p_modal_session_id", "p_conn_id"],
    "inference_session_heartbeat": ["p_token", "p_modal_session_id", "p_conn_id", "p_stats"],
    "inference_session_end": ["p_token", "p_modal_session_id", "p_conn_id", "p_reason", "p_stats"],
}
INTERNAL = {"_inference_session_finish", "_inference_session_expire_if_due", "_inference_used_today_s"}
END_REASONS = {
    "user_stop", "robot_stop", "robot_error", "stall", "superseded", "client_gone", "expired",
    "server_error", "server_gone", "never_connected", "edge_failed",
}


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _strip_comments(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def migration_functions(sql: str) -> dict[str, list[tuple[str, str]]]:
    """{name: [(param, type), ...]} for every CREATE FUNCTION public.<name>(...)."""
    out: dict[str, list[tuple[str, str]]] = {}
    for m in re.finditer(r"CREATE FUNCTION public\.(\w+)\((.*?)\)\s*RETURNS", sql, re.S):
        params = []
        for part in m.group(2).split(","):
            part = part.strip()
            if part:
                name, typ = part.split(None, 1)
                params.append((name, typ.strip().upper()))
        out[m.group(1)] = params
    return out


def end_reason_check(sql: str) -> set[str]:
    m = re.search(r"end_reason\s+TEXT CHECK \(end_reason IS NULL OR end_reason IN \((.*?)\)\)", sql, re.S)
    assert m is not None, "end_reason CHECK not found"
    return set(re.findall(r"'([a-z_]+)'", m.group(1)))


class TestMigration048Shape(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = _read(MIGRATION)
        cls.sql = _strip_comments(cls.raw)
        cls.fns = migration_functions(cls.sql)

    def test_migration_048_shape(self) -> None:
        names = sorted(os.listdir(os.path.join(SUPABASE, "migrations")))
        mine = "20261011120000_048_inference_sessions.sql"
        self.assertIn(mine, names)
        self.assertGreater(mine, "20261004150000_046_close_direct_student_writes.sql")
        # 048 was the newest migration when it shipped; the only files sorted
        # after it are the RunPod training build's 047 (written later, the
        # number it had been earmarked), 049 and v2.33.0's 050, in that order.
        self.assertEqual(names[names.index(mine) + 1:], [
            "20261020120000_047_hide_worker_token.sql",
            "20261020130000_049_managed_training.sql",
            "20261021120000_050_training_continuation.sql",
        ])
        self.assertTrue(os.path.isfile(ROLLBACK))
        self.assertTrue(os.path.isfile(ASSERTIONS))
        self.assertIn("037 stays reserved", self.raw)
        self.assertIn("047 is earmarked", self.raw)
        self.assertTrue(self.sql.lstrip().startswith("BEGIN;"))
        self.assertTrue(self.sql.rstrip().endswith("COMMIT;"))

    def test_the_table_is_service_role_only(self) -> None:
        self.assertIn("ALTER TABLE public.inference_sessions ENABLE ROW LEVEL SECURITY;", self.sql)
        self.assertIn("REVOKE ALL ON public.inference_sessions FROM PUBLIC, anon, authenticated;", self.sql)
        self.assertNotRegex(self.sql, r"CREATE POLICY[^;]*inference_sessions")
        self.assertNotRegex(self.sql, r"GRANT [^;]* ON (TABLE )?public\.inference_sessions")
        self.assertNotIn("ALTER PUBLICATION", self.sql)
        # the self-check aborts the migration if any of it survives
        self.assertIn("has_table_privilege(v_role, 'public.inference_sessions', v_priv)", self.sql)
        self.assertIn("pg_publication_tables", self.sql)

    def test_the_rpc_contract_is_frozen(self) -> None:
        public = {n: [p for p, _ in ps] for n, ps in self.fns.items() if not n.startswith("_")}
        self.assertEqual(public, {**SERVICE_RPCS, **ANON_RPCS})
        self.assertEqual({n for n in self.fns if n.startswith("_")}, INTERNAL)

    def test_grants_follow_the_roles(self) -> None:
        for name in ANON_RPCS:
            self.assertRegex(self.sql, rf"GRANT EXECUTE ON FUNCTION public\.{name}\([^)]*\) TO anon, service_role;")
        for name in SERVICE_RPCS:
            grants = re.findall(rf"GRANT EXECUTE ON FUNCTION public\.{name}\([^)]*\) TO ([^;]+);", self.sql)
            self.assertEqual(grants, ["service_role"], name)
        for name in INTERNAL:
            self.assertNotRegex(self.sql, rf"GRANT [^;]*public\.{name}\(")
            self.assertRegex(
                self.sql,
                rf"REVOKE ALL ON FUNCTION public\.{name}\([^)]*\) FROM PUBLIC, anon, authenticated, service_role;",
            )
        for name in {**SERVICE_RPCS, **ANON_RPCS}:
            self.assertRegex(
                self.sql, rf"REVOKE ALL ON FUNCTION public\.{name}\([^)]*\) FROM PUBLIC, anon, authenticated;"
            )

    def test_every_rpc_is_security_definer_with_a_pinned_search_path(self) -> None:
        for m in re.finditer(r"CREATE FUNCTION public\.(\w+)\(.*?\)\s*RETURNS(.*?)AS \$\$", self.sql, re.S):
            name, head = m.group(1), m.group(2)
            self.assertIn("SET search_path = public, pg_temp", head, name)
            if name in INTERNAL:
                self.assertNotIn("SECURITY DEFINER", head, name)
            else:
                self.assertIn("SECURITY DEFINER", head, name)

    def test_the_end_reason_check_names_the_eleven_reasons(self) -> None:
        self.assertEqual(end_reason_check(self.sql), END_REASONS)

    def test_the_backend_column(self) -> None:
        self.assertIn(
            "ADD COLUMN IF NOT EXISTS backend TEXT NOT NULL DEFAULT 'jetson'\n"
            "  CONSTRAINT inference_runs_backend_check CHECK (backend IN ('jetson', 'cloud'));",
            self.sql,
        )

    def test_the_rollback_drops_everything_the_migration_creates(self) -> None:
        rb = _strip_comments(_read(ROLLBACK))
        for name, params in self.fns.items():
            types = ", ".join(t for _, t in params)
            self.assertIn(f"DROP FUNCTION IF EXISTS public.{name}({types});", rb, name)
        self.assertIn("DROP TABLE IF EXISTS public.inference_sessions;", rb)
        self.assertIn("ALTER TABLE public.inference_runs DROP COLUMN IF EXISTS backend;", rb)
        self.assertIn("Roll the cloud API back FIRST", _read(ROLLBACK))

    def test_the_assertions_header_counts_58(self) -> None:
        text = _read(ASSERTIONS)
        self.assertRegex(text, r"(?m)^-- Expected: 58 PASS\b")
        self.assertEqual(len(set(re.findall(r"PASS (T\d+[a-z]?)\b", text))), 58)


if __name__ == "__main__":
    unittest.main()

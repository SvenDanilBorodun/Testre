"""Migrations 047 and 049 and their rollbacks, read as text (spec §3.1, §6 WP-A, SR-26).

The behaviour is proven on a local stack by the hand-run assertion files
(``supabase/tests/047_*`` 10 PASS, ``049_*`` 66 PASS); what is pinned here,
deps-free and in CI, is the shape a careless edit would break: the ten
functions' signatures and SECURITY INVOKER, their grants and the self-check
naming each, the FR-1 CASE in update_training_progress with 032's terminal
guard verbatim, the new columns, and the rollbacks restoring the baseline's
start_training_safe and 032's update_training_progress byte for byte.
"""

from __future__ import annotations

import pathlib
import re
import unittest

_SUPABASE = pathlib.Path(__file__).resolve().parents[1] / "supabase"
_MIG = _SUPABASE / "migrations"
M047 = _MIG / "20261020120000_047_hide_worker_token.sql"
M049 = _MIG / "20261020130000_049_managed_training.sql"
R047 = _SUPABASE / "rollback" / "20261020120000_047_hide_worker_token_rollback.sql"
R049 = _SUPABASE / "rollback" / "20261020130000_049_managed_training_rollback.sql"
A047 = _SUPABASE / "tests" / "047_hide_worker_token_assertions.sql"
A049 = _SUPABASE / "tests" / "049_managed_training_assertions.sql"
BASELINE = _MIG / "00000000000000_baseline.sql"
M032 = _MIG / "20260615120000_032_cancel_requested_progress_guard.sql"

NEW_FUNCTIONS = {
    "claim_training_attempt": ["p_training_id int", "p_expected_attempt int"],
    "attach_training_attempt": ["p_training_id int", "p_attempt int", "p_backend text", "p_cloud_ref text"],
    "release_training_attempt": ["p_training_id int", "p_attempt int", "p_note text"],
    "requeue_training": ["p_training_id int", "p_attempt int", "p_reason text", "p_note text",
                         "p_resume_step int", "p_train_seconds int"],
    "end_training_attempt": ["p_training_id int", "p_attempt int", "p_reason text", "p_note text",
                             "p_train_seconds int"],
    "finish_training": ["p_training_id int", "p_attempt int", "p_status text", "p_message text", "p_log_url text"],
    "cancel_request_training": ["p_training_id int", "p_user_id uuid"],
    "complete_cancel_training": ["p_training_id int", "p_final text", "p_message text"],
    "claim_training_publish": ["p_training_id int", "p_attempt int", "p_lease_s int", "p_owner text"],
    "claim_checkpoint_mirror": ["p_training_id int", "p_attempt int", "p_step int", "p_lease_s int",
                                "p_owner text"],
}
SQL_TYPES = {"int": "INT", "text": "TEXT", "uuid": "UUID"}
REGPROC_TYPES = {"int": "integer", "text": "text", "uuid": "uuid"}


def _read(p) -> str:
    return p.read_text(encoding="utf-8")


def _code(sql: str) -> str:
    """SQL without -- comments (strings here never contain --)."""
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def _bodies(sql: str, name: str) -> list:
    out, i = [], 0
    key = f"CREATE OR REPLACE FUNCTION public.{name}("
    while (i := sql.find(key, i)) >= 0:
        j = sql.index("$$;", i) + 3
        out.append(sql[i:j])
        i = j
    return out


def _signature(body: str) -> list:
    m = re.match(r"CREATE OR REPLACE FUNCTION public\.\w+\((.*?)\)\s*RETURNS", body, re.S)
    assert m, body[:80]
    return [" ".join(p.split()[:2]).lower() for p in m.group(1).split(",")]


def _regproc(name: str) -> str:
    return f"public.{name}(" + ",".join(REGPROC_TYPES[a.split()[1]] for a in NEW_FUNCTIONS[name]) + ")"


def _sqlsig(name: str) -> str:
    return f"public.{name}(" + ", ".join(SQL_TYPES[a.split()[1]] for a in NEW_FUNCTIONS[name]) + ")"


class FilesAndOrder(unittest.TestCase):
    def test_every_file_exists(self):
        for p in (M047, M049, R047, R049, A047, A049):
            self.assertTrue(p.is_file(), p)

    def test_047_and_049_sort_after_048_in_this_order(self):
        names = sorted(p.name for p in _MIG.glob("*.sql"))
        i = names.index("20261011120000_048_inference_sessions.sql")
        self.assertEqual(names[i + 1:i + 3], [M047.name, M049.name])
        self.assertFalse(any("_037_" in n for n in names))

    def test_one_transaction_each(self):
        for p in (M047, M049, R049):
            code = _code(_read(p))
            self.assertTrue(code.strip().startswith("BEGIN;"), p.name)
            self.assertTrue(code.rstrip().endswith("COMMIT;"), p.name)
            self.assertEqual(code.count("BEGIN;"), 1, p.name)


class Migration047(unittest.TestCase):
    def setUp(self):
        self.code = _code(_read(M047))

    def test_the_table_grant_goes_and_twenty_columns_come_back(self):
        self.assertIn("REVOKE SELECT ON public.trainings FROM anon, authenticated;", self.code)
        m = re.search(r"GRANT SELECT \((.*?)\) ON public\.trainings TO anon, authenticated;", self.code, re.S)
        self.assertIsNotNone(m)
        cols = [c.strip() for c in m.group(1).split(",")]
        self.assertEqual(len(cols), 20)
        self.assertNotIn("worker_token", cols)
        for c in ("id", "user_id", "status", "current_step", "total_steps", "loss_history", "log_url",
                  "error_message", "workgroup_id"):
            self.assertIn(c, cols)

    def test_the_self_check_judges_the_privilege(self):
        check = self.code[self.code.index("DO $$"):]
        self.assertIn("has_column_privilege(v_role, 'public.trainings', 'worker_token', 'SELECT')", check)
        self.assertIn("column_name <> 'worker_token'", check)
        self.assertIn("RAISE EXCEPTION", check)

    def test_the_rollback_reopens_only_the_table_grant(self):
        code = _code(_read(R047))
        self.assertIn("GRANT SELECT ON public.trainings TO anon, authenticated;", code)
        self.assertNotIn("REVOKE", code)


class Migration049(unittest.TestCase):
    def setUp(self):
        self.code = _code(_read(M049))

    def test_the_ten_functions_signatures_and_security_invoker(self):
        for name, args in NEW_FUNCTIONS.items():
            with self.subTest(fn=name):
                bodies = _bodies(self.code, name)
                self.assertEqual(len(bodies), 1)
                self.assertEqual(_signature(bodies[0]), args)
                header = bodies[0][:bodies[0].index("AS $$")]
                self.assertIn("SECURITY INVOKER", header)
                self.assertNotIn("SECURITY DEFINER", header)
                self.assertIn("SET search_path = public", header)

    def test_every_new_function_is_service_role_only(self):
        for name in NEW_FUNCTIONS:
            with self.subTest(fn=name):
                self.assertIn(f"REVOKE ALL ON FUNCTION {_sqlsig(name)} FROM PUBLIC, anon, authenticated;", self.code)
                self.assertIn(f"GRANT EXECUTE ON FUNCTION {_sqlsig(name)} TO service_role;", self.code)
                self.assertNotRegex(self.code, rf"GRANT[^;]*{name}[^;]*TO[^;]*(anon|authenticated)")

    def test_the_self_check_names_every_function_and_both_tables(self):
        check = self.code[self.code.rindex("DO $$"):]
        for name in NEW_FUNCTIONS:
            self.assertGreaterEqual(check.count(f"'{_regproc(name)}'"), 2, name)  # denied to students, granted to service_role
        self.assertIn("'public.start_training_safe(uuid,text,text,text,jsonb,integer,uuid,boolean)'", check)
        for table in ("public.training_attempts", "public.training_checkpoints"):
            self.assertIn(f"'{table}'", check)
        self.assertIn("has_column_privilege(v_role, 'public.trainings', 'worker_token', 'SELECT')", check)
        self.assertIn("pg_publication_tables", check)
        self.assertIn("proname = 'start_training_safe'", check)

    def test_the_tables_are_service_role_only(self):
        for table in ("training_attempts", "training_checkpoints"):
            self.assertIn(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY;", self.code)
            self.assertIn(f"REVOKE ALL ON public.{table} FROM PUBLIC, anon, authenticated;", self.code)
            self.assertNotRegex(self.code, rf"CREATE POLICY[^;]*{table}")
        self.assertNotIn("ALTER PUBLICATION", self.code)

    def test_the_new_columns(self):
        for col in ("managed", "current_attempt", "train_seconds_used", "status_note", "step_advanced_at",
                    "dataset_fps", "unplaced_since"):
            self.assertRegex(self.code, rf"ADD COLUMN IF NOT EXISTS {col}\s")
        self.assertIn("GRANT SELECT (managed, current_attempt, train_seconds_used, status_note, step_advanced_at, "
                      "dataset_fps, unplaced_since)\n  ON public.trainings TO anon, authenticated;", self.code)
        for col in ("allowed_dcs        TEXT[]", "mirror_claimed_at TIMESTAMPTZ", "mirror_owner   TEXT",
                    "crash_attempts INT[] NOT NULL DEFAULT '{}'", "publish_owner      TEXT",
                    "resume_from_attempt INT"):
            self.assertIn(col, self.code)

    def test_start_training_safe_gains_p_managed_and_keeps_the_credit_logic(self):
        (body,) = _bodies(self.code, "start_training_safe")
        self.assertEqual(_signature(body)[-1], "p_managed boolean")
        self.assertIn("p_managed         BOOLEAN DEFAULT false", body)
        self.assertIn("SECURITY DEFINER", body[:body.index("AS $$")])
        self.assertIn("DROP FUNCTION IF EXISTS public.start_training_safe(UUID, TEXT, TEXT, TEXT, JSONB, INT, UUID);",
                      self.code)
        base = _bodies(_read(BASELINE), "start_training_safe")[1]
        # The credit logic is the baseline's: only the INSERT gains `managed`.
        norm = lambda s: re.sub(r"--[^\n]*", "", s)  # noqa: E731
        self.assertEqual(norm(body).count("FOR UPDATE"), norm(base).count("FOR UPDATE"))
        for line in ("AND status NOT IN ('failed', 'canceled');", "IF v_used >= v_credits THEN",
                     "RAISE EXCEPTION 'No training credits remaining' USING ERRCODE = 'P0003';"):
            self.assertEqual(body.count(line), base.count(line), line)
        self.assertIn("REVOKE ALL ON FUNCTION public.start_training_safe(UUID, TEXT, TEXT, TEXT, JSONB, INT, UUID, "
                      "BOOLEAN) FROM PUBLIC, anon, authenticated;", self.code)

    def test_update_training_progress_fr1_and_032s_guard(self):
        (body,) = _bodies(self.code, "update_training_progress")
        header = body[:body.index("AS $$")]
        self.assertIn("SECURITY DEFINER", header)
        self.assertIn("total_steps   = CASE WHEN managed THEN total_steps   ELSE COALESCE(p_total_steps,   total_steps)   END,",
                      body)
        self.assertIn("error_message = CASE WHEN managed THEN error_message ELSE COALESCE(p_error_message, error_message) END,",
                      body)
        guard = ("  WHERE id = p_training_id\n    AND worker_token = p_token\n"
                 "    AND status NOT IN ('succeeded','failed','canceled','cancel_requested');")
        self.assertIn(guard, body)
        self.assertIn(guard, _bodies(_read(M032), "update_training_progress")[0])
        self.assertIn("RAISE EXCEPTION 'Managed training: status % is set by the API', p_status USING ERRCODE = '22023';",
                      body)
        self.assertIn("GRANT EXECUTE ON FUNCTION public.update_training_progress(INT, UUID, TEXT, INT, INT, REAL, TEXT, TEXT) "
                      "TO anon, authenticated;", self.code)

    def test_the_cas_error_codes(self):
        for name in ("claim_training_attempt", "attach_training_attempt", "release_training_attempt",
                     "requeue_training", "end_training_attempt", "finish_training", "complete_cancel_training"):
            self.assertIn("ERRCODE = 'P0049'", _bodies(self.code, name)[0], name)
        cancel = _bodies(self.code, "cancel_request_training")[0]
        for code in ("P0002", "P0050", "P0051"):
            self.assertIn(f"ERRCODE = '{code}'", cancel)


class Rollback049(unittest.TestCase):
    def setUp(self):
        self.code = _code(_read(R049))

    def test_it_refuses_while_a_managed_row_is_active(self):
        first = self.code[:self.code.index("DROP FUNCTION")]
        self.assertIn("WHERE managed AND status IN ('queued','running','cancel_requested')", first)
        self.assertIn("RAISE EXCEPTION", first)

    def test_it_drops_the_ten_functions_and_both_tables(self):
        for name in NEW_FUNCTIONS:
            self.assertIn(f"DROP FUNCTION IF EXISTS {_sqlsig(name)};", self.code, name)
        self.assertIn("DROP TABLE IF EXISTS public.training_checkpoints;", self.code)
        self.assertIn("DROP TABLE IF EXISTS public.training_attempts;", self.code)
        for col in ("managed", "current_attempt", "train_seconds_used", "status_note", "step_advanced_at",
                    "dataset_fps", "unplaced_since"):
            self.assertIn(f"DROP COLUMN IF EXISTS {col}", self.code)

    def test_start_training_safe_is_the_baselines_and_service_role_only(self):
        raw = _read(R049)
        self.assertEqual(_bodies(raw, "start_training_safe")[0], _bodies(_read(BASELINE), "start_training_safe")[1])
        self.assertIn("REVOKE ALL ON FUNCTION public.start_training_safe(UUID, TEXT, TEXT, TEXT, JSONB, INT, UUID) "
                      "FROM PUBLIC, anon, authenticated;", self.code)
        self.assertIn("GRANT EXECUTE ON FUNCTION public.start_training_safe(UUID, TEXT, TEXT, TEXT, JSONB, INT, UUID) "
                      "TO service_role;", self.code)

    def test_update_training_progress_is_032s_verbatim(self):
        raw = _read(R049)
        self.assertEqual(_bodies(raw, "update_training_progress")[0], _bodies(_read(M032), "update_training_progress")[0])
        self.assertIn("GRANT EXECUTE ON FUNCTION public.update_training_progress(INT, UUID, TEXT, INT, INT, REAL, TEXT, TEXT) "
                      "TO anon, authenticated;", self.code)

    def test_it_ends_with_its_privilege_self_check(self):
        tail = self.code[self.code.rindex("DO $$"):]
        self.assertTrue(tail.rstrip().endswith("COMMIT;"))
        self.assertIn("has_function_privilege(v_role, 'public.start_training_safe(uuid,text,text,text,jsonb,integer,uuid)', 'EXECUTE')",
                      tail)
        self.assertIn("has_function_privilege('service_role', 'public.start_training_safe(uuid,text,text,text,jsonb,integer,uuid)', 'EXECUTE')",
                      tail)
        self.assertIn("'public.update_training_progress(integer,uuid,text,integer,integer,real,text,text)'", tail)
        self.assertIn("has_column_privilege(v_role, 'public.trainings', 'worker_token', 'SELECT')", tail)
        self.assertIn("to_regclass('public.training_attempts') IS NOT NULL", tail)


if __name__ == "__main__":
    unittest.main()

"""Migration 050 (the capped publish + „Fortsetzen") and its rollback, read as text.

The behaviour is proven on a local stack by
``supabase/tests/050_training_continuation_assertions.sql`` (30 PASS); what is
pinned here, deps-free and in CI, is the shape a careless edit would break: the
three granted columns, the service-role-only kept table and two new functions,
cap_training_attempt's SECURITY INVOKER and start_continuation_safe's SECURITY
DEFINER + SET search_path + OUT names (an unqualified ``training_id`` would raise
42702 at call time), finish_training's 8-argument signature with the 5-argument
one dropped, that requeue_training charges only to the newest own ready
checkpoint (E50), that start_continuation_safe CALLS start_training_safe and
copies no INSERT, and the rollback restoring 049's four bodies byte for byte.
"""

from __future__ import annotations

import pathlib
import re
import unittest

_SUPABASE = pathlib.Path(__file__).resolve().parents[1] / "supabase"
_MIG = _SUPABASE / "migrations"
M049 = _MIG / "20261020130000_049_managed_training.sql"
M050 = _MIG / "20261021120000_050_training_continuation.sql"
R050 = _SUPABASE / "rollback" / "20261021120000_050_training_continuation_rollback.sql"
A050 = _SUPABASE / "tests" / "050_training_continuation_assertions.sql"


def _read(p) -> str:
    return p.read_text(encoding="utf-8")


def _code(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def _bodies(sql: str, name: str) -> list:
    out, i = [], 0
    key = f"CREATE OR REPLACE FUNCTION public.{name}("
    while (i := sql.find(key, i)) >= 0:
        j = sql.index("$$;", i) + 3
        out.append(sql[i:j])
        i = j
    return out


class FilesAndOrder(unittest.TestCase):
    def test_every_file_exists(self):
        for p in (M050, R050, A050):
            self.assertTrue(p.is_file(), p)

    def test_050_sorts_after_049(self):
        self.assertGreater(M050.name, M049.name)

    def test_one_transaction(self):
        for p in (M050, R050):
            code = _code(_read(p))
            self.assertEqual(code.count("BEGIN;"), 1, p.name)
            self.assertTrue(code.rstrip().endswith("COMMIT;"), p.name)


class Migration050(unittest.TestCase):
    def setUp(self):
        self.code = _code(_read(M050))

    def test_the_three_columns_and_their_grant(self):
        for col in ("capped_step", "continuable_until", "continued_from"):
            self.assertIn(col, self.code)
        self.assertIn("GRANT SELECT (capped_step, continuable_until, continued_from) ON public.trainings "
                      "TO anon, authenticated;", self.code)

    def test_the_kept_table_is_service_role_only(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS public.training_kept_checkpoints", self.code)
        self.assertIn("ALTER TABLE public.training_kept_checkpoints ENABLE ROW LEVEL SECURITY;", self.code)
        self.assertIn("REVOKE ALL ON public.training_kept_checkpoints FROM PUBLIC, anon, authenticated;", self.code)

    def test_cap_training_attempt_is_security_invoker_service_role_only(self):
        (body,) = _bodies(self.code, "cap_training_attempt")
        self.assertIn("SECURITY INVOKER", body)
        self.assertIn("REVOKE ALL ON FUNCTION public.cap_training_attempt(INT, INT, INT, TEXT, TEXT, INT) "
                      "FROM PUBLIC, anon, authenticated;", self.code)
        self.assertIn("GRANT EXECUTE ON FUNCTION public.cap_training_attempt(INT, INT, INT, TEXT, TEXT, INT) "
                      "TO service_role;", self.code)
        self.assertIn("'capped'", body)
        self.assertIn("'wall_capped'", body)

    def test_start_continuation_safe_is_security_definer_with_the_right_shape(self):
        (body,) = _bodies(self.code, "start_continuation_safe")
        self.assertIn("SECURITY DEFINER", body)
        self.assertIn("SET search_path = public", body)
        self.assertIn("RETURNS TABLE(o_training_id INT, o_remaining INT)", body)
        # review m4: an unqualified `training_id` anywhere in the body raises 42702 at call time.
        self.assertNotRegex(body, r"(?<![._a-z])training_id\b(?!\s*\))")
        # the credit rule is start_training_safe itself, called — never copied.
        self.assertIn("public.start_training_safe(", body)
        self.assertNotIn("INSERT INTO public.trainings", body)
        self.assertNotIn("CREATE OR REPLACE FUNCTION public.start_training_safe", self.code)
        self.assertIn("REVOKE ALL ON FUNCTION public.start_continuation_safe(UUID, INT, TEXT) "
                      "FROM PUBLIC, anon, authenticated;", self.code)

    def test_finish_training_is_eight_arguments_and_the_five_arg_is_dropped(self):
        (body,) = _bodies(self.code, "finish_training")
        self.assertIn("p_note TEXT DEFAULT NULL", body)
        self.assertIn("p_final_step INT DEFAULT NULL", body)
        self.assertIn("p_continuable_until TIMESTAMPTZ DEFAULT NULL", body)
        self.assertIn("DROP FUNCTION IF EXISTS public.finish_training(INT, INT, TEXT, TEXT, TEXT);", self.code)

    def test_publish_and_cancel_accept_a_capped_attempt(self):
        for name in ("claim_training_publish", "cancel_request_training"):
            (body,) = _bodies(self.code, name)
            self.assertIn("'capped'", body, name)
            self.assertIn("'wall_capped'", body, name)

    def test_requeue_training_keeps_its_signature_and_charges_to_the_checkpoint(self):
        (body,) = _bodies(self.code, "requeue_training")
        self.assertRegex(body, r"public\.requeue_training\(\s*\n?\s*p_training_id INT, p_attempt INT, "
                               r"p_reason TEXT, p_note TEXT, p_resume_step INT, p_train_seconds INT\)")
        self.assertIn("LEAST(", body)
        self.assertIn("max(c.created_at)", body)
        self.assertIn("c.state = 'ready'", body)

    def test_the_self_check(self):
        tail = self.code[self.code.rindex("DO $$"):]
        self.assertIn("has_table_privilege(v_role, 'public.training_kept_checkpoints', v_priv)", tail)
        self.assertIn("finish_training must have exactly one overload", tail)
        self.assertIn("start_training_safe must have exactly one overload", tail)
        self.assertIn("is in a publication", tail)


class Rollback050(unittest.TestCase):
    def setUp(self):
        self.code = _code(_read(R050))

    def test_it_refuses_while_a_managed_row_is_active(self):
        first = self.code[:self.code.index("DROP FUNCTION")]
        self.assertIn("WHERE managed AND status IN ('queued','running','cancel_requested')", first)
        self.assertIn("RAISE EXCEPTION", first)

    def test_it_restores_049s_four_bodies_verbatim(self):
        raw050, raw049 = _read(R050), _read(M049)
        for name in ("finish_training", "requeue_training", "claim_training_publish", "cancel_request_training"):
            self.assertEqual(_bodies(raw050, name)[0], _bodies(raw049, name)[0], name)

    def test_it_drops_the_two_new_functions_and_the_kept_table(self):
        self.assertIn("DROP FUNCTION IF EXISTS public.start_continuation_safe(UUID, INT, TEXT);", self.code)
        self.assertIn("DROP FUNCTION IF EXISTS public.cap_training_attempt(INT, INT, INT, TEXT, TEXT, INT);", self.code)
        self.assertIn("DROP FUNCTION IF EXISTS public.finish_training(INT, INT, TEXT, TEXT, TEXT, TEXT, INT, "
                      "TIMESTAMPTZ);", self.code)
        self.assertIn("DROP TABLE IF EXISTS public.training_kept_checkpoints;", self.code)
        for col in ("capped_step", "continuable_until", "continued_from"):
            self.assertIn(f"DROP COLUMN IF EXISTS {col}", self.code)

    def test_it_restores_the_five_argument_grant(self):
        self.assertIn("REVOKE ALL ON FUNCTION public.finish_training(INT, INT, TEXT, TEXT, TEXT) "
                      "FROM PUBLIC, anon, authenticated;", self.code)
        self.assertIn("GRANT EXECUTE ON FUNCTION public.finish_training(INT, INT, TEXT, TEXT, TEXT) "
                      "TO service_role;", self.code)

    def test_its_self_check_keeps_worker_token_unreadable(self):
        tail = self.code[self.code.rindex("DO $$"):]
        self.assertIn("worker_token", tail)
        self.assertIn("finish_training must have exactly one overload", tail)


if __name__ == "__main__":
    unittest.main()

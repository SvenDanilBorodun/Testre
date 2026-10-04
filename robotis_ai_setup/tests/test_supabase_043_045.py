"""Migrations 043-045 (2026-10-04, the review of the per-student Hugging Face
token): what the SQL files must keep saying, read as text. The behaviour itself
is proven by the hand-run supabase/tests/04[345]_*_assertions.sql files (never
CI: they need a database); this file fences the parts a later edit could break
without anybody re-running them. Deliberately stdlib-only.

* 043 changes ONE condition of register_dataset_safe; its rollback restores
  026's function, comment and grants byte for byte.
* 044's users guard must stay SECURITY INVOKER (a definer trigger would see its
  owner as current_user and let every student through), judge exactly the five
  protected columns, and fire before the other BEFORE UPDATE triggers.
* 045 drops 042's six-argument writer before creating the seven-argument one
  (one overload only), and its rollback restores 042's writer byte for byte.
"""

import pathlib
import re
import unittest

_SUPABASE = pathlib.Path(__file__).resolve().parents[1] / 'supabase'
_MIG = _SUPABASE / 'migrations'
_RB = _SUPABASE / 'rollback'

M026 = _MIG / '20260520133426_20260520140000_026_register_dataset_safe.sql'
M042 = _MIG / '20261003120000_042_user_hf_credentials.sql'
M043 = _MIG / '20261004120000_043_register_dataset_proven_anchor.sql'
M044 = _MIG / '20261004130000_044_users_protected_columns.sql'
M045 = _MIG / '20261004140000_045_hf_credential_expected_fp.sql'
R043 = _RB / '20261004120000_043_register_dataset_proven_anchor_rollback.sql'
R044 = _RB / '20261004130000_044_users_protected_columns_rollback.sql'
R045 = _RB / '20261004140000_045_hf_credential_expected_fp_rollback.sql'

PROTECTED = ('role', 'training_credits', 'hf_username', 'classroom_id', 'workgroup_id')


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding='utf-8')


def _function_body(sql: str, name: str) -> str:
    start = sql.index(f'CREATE OR REPLACE FUNCTION public.{name}(')
    return sql[start:sql.index('$$;', start) + 3]


def _signature(sql: str, name: str) -> list:
    m = re.search(rf'CREATE OR REPLACE FUNCTION public\.{name}\((.*?)\)\s*RETURNS', sql, re.S)
    assert m is not None, name
    return [' '.join(p.split()[:2]).lower() for p in m.group(1).split(',')]


class FilesAndOrder(unittest.TestCase):
    def test_every_file_exists_and_sorts_after_042(self):
        for path in (M043, M044, M045, R043, R044, R045):
            self.assertTrue(path.is_file(), path)
        names = sorted(p.name for p in _MIG.glob('*.sql'))
        self.assertEqual(names[-4:], [M042.name, M043.name, M044.name, M045.name])

    def test_037_is_still_skipped(self):
        self.assertFalse(any('_037_' in p.name for p in _MIG.glob('*.sql')))

    def test_every_migration_has_its_assertion_file(self):
        for label in ('043', '044', '045'):
            self.assertEqual(len(list((_SUPABASE / 'tests').glob(f'{label}_*_assertions.sql'))), 1, label)


class RegisterDatasetSafe043(unittest.TestCase):
    def test_the_signature_is_026s(self):
        self.assertEqual(_signature(_read(M043), 'register_dataset_safe'),
                         _signature(_read(M026), 'register_dataset_safe'))
        self.assertIn('RETURNS public.datasets', _function_body(_read(M043), 'register_dataset_safe'))

    def test_the_proven_name_is_a_second_anchor_and_the_oldest_row_stays(self):
        body = _function_body(_read(M043), 'register_dataset_safe')
        self.assertIn('FROM public.user_hf_credentials', body)
        self.assertIn('ORDER BY created_at ASC', body)
        self.assertRegex(
            body,
            r"lower\(v_anchor_author\) <> lower\(p_hf_author\)\s+AND \(v_proven_author IS NULL OR "
            r"lower\(v_proven_author\) <> lower\(p_hf_author\)\)")
        self.assertIn("ERRCODE = 'P0034'", body)
        self.assertLess(body.index('FOR UPDATE'), body.index("ERRCODE = 'P0002'"))

    def test_grants_are_service_role_only(self):
        sql = _read(M043)
        for role in ('PUBLIC', 'anon', 'authenticated'):
            self.assertRegex(sql, rf'REVOKE EXECUTE ON FUNCTION public\.register_dataset_safe\([^)]*\)\s+FROM {role};')
        self.assertRegex(sql, r'GRANT EXECUTE ON FUNCTION public\.register_dataset_safe\([^)]*\)\s+TO service_role;')

    def test_the_rollback_restores_026_byte_for_byte(self):
        src = _read(M026)
        block = src[src.index('CREATE OR REPLACE FUNCTION public.register_dataset_safe('):
                    src.index('COMMIT;', src.index('CREATE OR REPLACE FUNCTION public.register_dataset_safe('))]
        self.assertIn(block.rstrip() + '\n', _read(R043))


class UsersGuard044(unittest.TestCase):
    def setUp(self):
        self.sql = _read(M044)
        self.body = _function_body(self.sql, 'users_guard_protected_columns')

    def test_the_guard_is_security_invoker_with_an_empty_search_path(self):
        self.assertNotIn('SECURITY DEFINER', self.body)
        self.assertIn("SET search_path = ''", self.body)

    def test_rule_1_judges_the_request_role_and_exactly_the_five_columns(self):
        self.assertIn("current_user IN ('authenticated', 'anon')", self.body)
        judged = re.findall(r'NEW\.(\w+)\s+IS DISTINCT FROM OLD\.(\w+)', self.body)
        rule1 = [a for a, b in judged if a == b]
        self.assertEqual(sorted(set(rule1)), sorted(PROTECTED))
        self.assertIn("ERRCODE = '42501'", self.body)

    def test_rule_2_compares_with_the_stored_credential_for_every_caller(self):
        rule2 = self.body[self.body.index('(2)'):]
        self.assertNotIn('current_user', rule2)
        self.assertIn('FROM public.user_hf_credentials', rule2)
        self.assertIn("ERRCODE = 'P0044'", rule2)

    def test_the_trigger_fires_first_and_before_update_for_each_row(self):
        self.assertRegex(
            self.sql,
            r'CREATE TRIGGER trg_0_users_protected_columns\s+BEFORE UPDATE ON public\.users\s+'
            r'FOR EACH ROW EXECUTE FUNCTION public\.users_guard_protected_columns\(\);')
        self.assertLess('trg_0_users_protected_columns', 'trg_classroom_capacity')

    def test_the_rollback_drops_both(self):
        rb = _read(R044)
        self.assertIn('DROP TRIGGER IF EXISTS trg_0_users_protected_columns ON public.users;', rb)
        self.assertIn('DROP FUNCTION IF EXISTS public.users_guard_protected_columns();', rb)

    def test_the_042_writer_passes_rule_2_by_construction(self):
        # credential first, then users.hf_username to the same name
        body = _function_body(_read(M045), 'store_user_hf_credential')
        self.assertLess(body.index('INSERT INTO public.user_hf_credentials'),
                        body.index('UPDATE public.users SET hf_username = p_hf_username'))


class StoreWriter045(unittest.TestCase):
    def test_one_overload_seven_parameters_the_last_defaulted(self):
        sql = _read(M045)
        self.assertLess(
            sql.index('DROP FUNCTION IF EXISTS public.store_user_hf_credential(uuid, text, text, text, text, text);'),
            sql.index('CREATE OR REPLACE FUNCTION public.store_user_hf_credential('))
        self.assertEqual(_signature(sql, 'store_user_hf_credential'),
                         _signature(_read(M042), 'store_user_hf_credential') + ['p_expected_fp text'])
        self.assertRegex(sql, r'p_expected_fp\s+text DEFAULT NULL')

    def test_the_expectation_is_compared_under_the_credential_row_lock(self):
        body = _function_body(_read(M045), 'store_user_hf_credential')
        check = body[body.index('IF p_expected_fp IS NOT NULL THEN'):body.index("ERRCODE = 'P0045'")]
        self.assertIn('FROM public.user_hf_credentials', check)
        self.assertIn('FOR UPDATE', check)
        self.assertIn('IF NOT FOUND OR v_current_fp IS DISTINCT FROM p_expected_fp THEN', check)

    def test_grants_follow_042_for_the_seven_argument_signature(self):
        sql = _read(M045)
        sig = r'public\.store_user_hf_credential\(uuid, text, text, text, text, text, text\)'
        for role in ('PUBLIC', 'anon', 'authenticated'):
            self.assertRegex(sql, rf'REVOKE EXECUTE ON FUNCTION {sig} FROM {role};')
        self.assertRegex(sql, rf'GRANT EXECUTE ON FUNCTION {sig} TO service_role;')

    def test_the_rollback_restores_042s_writer_byte_for_byte(self):
        src = _read(M042)
        start = src.index('-- ---------------------------------------------------------------------------\n-- the one writer')
        block = src[start:src.index('COMMIT;', start)].rstrip() + '\n'
        rb = _read(R045)
        self.assertIn(block, rb)
        self.assertLess(
            rb.index('DROP FUNCTION IF EXISTS public.store_user_hf_credential(uuid, text, text, text, text, text, text);'),
            rb.index(block))


if __name__ == '__main__':
    unittest.main()

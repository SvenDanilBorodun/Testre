"""Migration 046 (2026-10-04): students write no table directly. What the SQL
files must keep saying, read as text; the behaviour itself is proven by the
hand-run supabase/tests/046_close_direct_student_writes_assertions.sql (never
CI: it needs a database). Deliberately stdlib-only.

* 046 revokes every write privilege of anon and authenticated on exactly the
  four tables, drops exactly their twelve own-row write policies, and touches
  no SELECT policy, no publication and no service_role grant.
* Its rollback re-grants the same privileges and re-creates the twelve
  policies exactly as 024 created them (nothing between 024 and 046 touched
  them), so a rollback really restores the pre-046 state.
* No migration after 024 re-creates one of those policies (else the rollback
  would restore a stale definition).
"""

import pathlib
import re
import unittest

_SUPABASE = pathlib.Path(__file__).resolve().parents[1] / 'supabase'
_MIG = _SUPABASE / 'migrations'
_RB = _SUPABASE / 'rollback'

M024 = _MIG / '20260520130522_024_advisor_fixes.sql'
M045 = _MIG / '20261004140000_045_hf_credential_expected_fp.sql'
M046 = _MIG / '20261004150000_046_close_direct_student_writes.sql'
R046 = _RB / '20261004150000_046_close_direct_student_writes_rollback.sql'
A046 = _SUPABASE / 'tests' / '046_close_direct_student_writes_assertions.sql'

TABLES = ('trainings', 'datasets', 'workflows', 'tutorial_progress')
WRITE_PRIVILEGES = 'INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'
POLICIES = {
    'trainings': ('Users insert own trainings', 'Users update own trainings', 'Users delete own trainings'),
    'datasets': ('Owner inserts own datasets', 'Owner updates own datasets', 'Owner deletes own datasets'),
    'workflows': ('Owner inserts own workflows', 'Owner updates own workflows', 'Owner deletes own workflows'),
    'tutorial_progress': ('Tutorial progress owner insert', 'Tutorial progress owner update',
                          'Tutorial progress owner delete'),
}
_ON_TABLES = ', '.join(f'public.{t}' for t in TABLES)


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding='utf-8')


def _code(sql: str) -> str:
    """The SQL without its comment lines (the header names every policy too)."""
    return '\n'.join(line for line in sql.splitlines() if not line.lstrip().startswith('--'))


def _create_policy(sql: str, name: str) -> str:
    """The CREATE POLICY statement for `name`, whitespace-normalised."""
    m = re.search(rf'CREATE POLICY "{re.escape(name)}"(.*?);', sql, re.S)
    assert m is not None, name
    return ' '.join(m.group(0).split())


class FileAndOrder(unittest.TestCase):
    def test_the_files_exist_and_046_sorts_right_after_045(self):
        for path in (M046, R046, A046):
            self.assertTrue(path.is_file(), path)
        names = sorted(p.name for p in _MIG.glob('*.sql'))
        self.assertEqual(names[names.index(M045.name) + 1], M046.name)
        self.assertFalse(any('_037_' in p.name for p in _MIG.glob('*.sql')))


class Migration046(unittest.TestCase):
    def setUp(self):
        self.code = _code(_read(M046))

    def test_one_transaction(self):
        self.assertTrue(self.code.lstrip().startswith('BEGIN;'))
        self.assertTrue(self.code.rstrip().endswith('COMMIT;'))

    def test_every_write_privilege_goes_from_both_request_roles_on_the_four_tables(self):
        self.assertRegex(
            self.code,
            rf'REVOKE {WRITE_PRIVILEGES}\s+ON {re.escape(_ON_TABLES)}\s+FROM anon, authenticated;')

    def test_select_and_service_role_are_never_touched(self):
        self.assertNotRegex(self.code, r'REVOKE[^;]*SELECT')
        self.assertNotIn('service_role', self.code)
        self.assertNotRegex(self.code, r'\bGRANT\b')
        self.assertNotIn('PUBLICATION', self.code.upper())

    def test_exactly_the_twelve_write_policies_are_dropped(self):
        dropped = re.findall(r'DROP POLICY IF EXISTS "([^"]+)"\s+ON public\.(\w+);', self.code)
        expected = sorted((name, table) for table, names in POLICIES.items() for name in names)
        self.assertEqual(sorted(dropped), expected)
        self.assertNotIn('read consolidated', self.code)
        self.assertNotIn('CREATE POLICY', self.code)

    def test_the_self_check_judges_the_privilege_not_the_grant(self):
        check = self.code[self.code.index('DO $$'):]
        self.assertIn('has_table_privilege(v_role, v_table, v_priv)', check)
        for word in ("'anon'", "'authenticated'", "'INSERT'", "'UPDATE'", "'DELETE'", 'RAISE EXCEPTION'):
            self.assertIn(word, check)
        for table in TABLES:
            self.assertIn(f"'public.{table}'", check)
        self.assertIn("cmd <> 'SELECT'", check)


class Rollback046(unittest.TestCase):
    def setUp(self):
        self.code = _code(_read(R046))

    def test_the_same_privileges_come_back_to_the_same_roles(self):
        self.assertRegex(
            self.code,
            rf'GRANT {WRITE_PRIVILEGES}\s+ON {re.escape(_ON_TABLES)}\s+TO anon, authenticated;')

    def test_every_policy_is_024s_definition(self):
        m024 = _read(M024)
        for table, names in POLICIES.items():
            for name in names:
                with self.subTest(policy=name):
                    self.assertEqual(_create_policy(self.code, name), _create_policy(m024, name))
                    self.assertIn(f'DROP POLICY IF EXISTS "{name}"', self.code)
                    self.assertLess(self.code.index(f'DROP POLICY IF EXISTS "{name}"'),
                                    self.code.index(f'CREATE POLICY "{name}"'))

    def test_no_migration_after_024_redefined_them(self):
        later = sorted(p for p in _MIG.glob('*.sql') if p.name > M024.name and p != M046)
        self.assertTrue(later)
        for path in later:
            code = _code(_read(path))
            for names in POLICIES.values():
                for name in names:
                    self.assertNotIn(f'"{name}"', code, f'{path.name} touches {name}')


if __name__ == '__main__':
    unittest.main()

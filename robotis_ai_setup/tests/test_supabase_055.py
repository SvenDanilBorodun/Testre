# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""Migration 055 (2026-11-11, launch plan step 5): the users guard's rule 1
judges the WHOLE row. What the SQL files must keep saying, read as text; the
behaviour is proven by the hand-run
supabase/tests/055_users_identity_guard_assertions.sql (never CI: it needs a
database). Deliberately stdlib-only.

* The guard stays SECURITY INVOKER with an empty search_path (a definer trigger
  would see its owner as current_user and let every student through — 044's
  lesson) and rule 1 refuses the request roles ANY change of a users row: the
  five 044 columns, deletion_requested_at, username, full_name, email,
  created_by, both vision-quota columns, created_at and every later column.
* Rule 2 (P0044) is byte-identical to 044's.
* The rollback restores 044's function byte for byte, and its comment.
* No API-visible shape changes, so the boot schema probe is untouched; the API
  writes deletion_requested_at with the service role, which passes.
* The vision-quota columns have no reader outside the migrations (the
  coordinator's check, kept: a reader would have to be justified here).
"""

import pathlib
import re
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SUPABASE = _REPO / 'robotis_ai_setup' / 'supabase'
_MIG = _SUPABASE / 'migrations'
M044 = _MIG / '20261004130000_044_users_protected_columns.sql'
M055 = _MIG / '20261111120000_055_users_identity_guard.sql'
R055 = _SUPABASE / 'rollback' / '20261111120000_055_users_identity_guard_rollback.sql'
A055 = _SUPABASE / 'tests' / '055_users_identity_guard_assertions.sql'
A044 = _SUPABASE / 'tests' / '044_users_protected_columns_assertions.sql'
MAIN = _REPO / 'robotis_ai_setup' / 'cloud_training_api' / 'app' / 'main.py'

GUARD = 'users_guard_protected_columns'


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding='utf-8')


def _function(sql: str) -> str:
    start = sql.index(f'CREATE OR REPLACE FUNCTION public.{GUARD}()')
    return sql[start:sql.index('$$;', start) + 3]


def _comment(sql: str) -> str:
    start = sql.index(f'COMMENT ON FUNCTION public.{GUARD}()')
    return sql[start:sql.index(';', sql.index("caller). SECURITY INVOKER", start)) + 1]


def _rule2(body: str) -> str:
    return body[body.index('-- (2)'):body.index('RETURN NEW;')]


class Migration055(unittest.TestCase):
    def test_the_files_exist_and_055_is_the_newest(self):
        for path in (M055, R055, A055):
            self.assertTrue(path.is_file(), path)
        names = sorted(p.name for p in _MIG.glob('*.sql'))
        self.assertEqual(names[-1], M055.name)
        self.assertEqual(names[-2], '20261110120000_054_advisor_cleanup.sql')

    def test_one_transaction(self):
        code = '\n'.join(line.split('--', 1)[0] for line in _read(M055).splitlines())
        self.assertEqual(code.count('BEGIN;'), 1)
        self.assertEqual(code.count('COMMIT;'), 1)

    def test_the_guard_stays_an_invoker_with_an_empty_search_path(self):
        body = _function(_read(M055))
        head = body[:body.index('AS $$')]
        self.assertNotIn('SECURITY DEFINER', head)
        self.assertIn("SET search_path = ''", head)

    def test_rule_1_judges_the_whole_row_for_the_request_roles(self):
        body = _function(_read(M055))
        rule1 = body[body.index('-- (1)'):body.index('-- (2)')]
        self.assertIn("IF current_user IN ('authenticated', 'anon') AND NEW IS DISTINCT FROM OLD THEN", rule1)
        self.assertIn("ERRCODE = '42501'", rule1)
        # no column list any more: a column list is what left eight columns open
        self.assertNotRegex(rule1, r'NEW\.\w+\s+IS DISTINCT FROM')
        # the German sentence is 044's
        self.assertIn("'Diese Angaben deines Kontos kann nur der EduBotics-Server ändern.'", rule1)
        self.assertIn("'Diese Angaben deines Kontos kann nur der EduBotics-Server ändern.'", _read(M044))

    def test_rule_2_is_044s_byte_for_byte(self):
        self.assertEqual(_rule2(_function(_read(M055))), _rule2(_function(_read(M044))))

    def test_the_trigger_and_the_revokes_are_044s(self):
        sql = _read(M055)
        self.assertRegex(
            sql,
            r'CREATE TRIGGER trg_0_users_protected_columns\s+BEFORE UPDATE ON public\.users\s+'
            r'FOR EACH ROW EXECUTE FUNCTION public\.users_guard_protected_columns\(\);')
        for role in ('PUBLIC', 'anon', 'authenticated'):
            self.assertIn(f'REVOKE EXECUTE ON FUNCTION public.{GUARD}() FROM {role};', sql)
        code = '\n'.join(line.split('--', 1)[0] for line in sql.splitlines())
        self.assertNotRegex(code, r'(?i)\bGRANT\b')

    def test_the_self_check_refuses_a_definer_a_column_list_and_a_late_trigger(self):
        sql = _read(M055)
        self.assertIn('NOT prosecdef', sql)
        self.assertIn("proconfig = ARRAY['search_path=\"\"']", sql)
        self.assertIn('AND NEW IS DISTINCT FROM OLD', sql[sql.index('DO $$'):])
        self.assertIn("t.tgname < 'trg_0_users_protected_columns'", sql)
        self.assertIn('055 self-check passed', sql)

    def test_the_rollback_restores_044s_function_and_comment(self):
        rb, m044 = _read(R055), _read(M044)
        self.assertEqual(_function(rb), _function(m044))
        self.assertEqual(_comment(rb), _comment(m044))
        self.assertNotIn('DROP TRIGGER', rb)
        self.assertIn('055 rollback self-check passed', rb)

    def test_the_boot_probe_is_untouched(self):
        main = _read(MAIN)
        self.assertNotIn(GUARD, main)
        self.assertNotIn('055', main)

    def test_the_vision_quota_columns_have_no_reader_outside_the_migrations(self):
        hits = []
        roots = [_REPO / 'robotis_ai_setup' / 'cloud_training_api' / 'app',
                 _REPO / 'robotis_ai_setup' / 'modal_training',
                 _REPO / 'robotis_ai_setup' / 'runpod_training',
                 _REPO / 'robotis_ai_setup' / 'modal_inference',
                 _REPO / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server',
                 _REPO / 'physical_ai_tools' / 'physical_ai_manager' / 'src']
        for root in roots:
            for path in root.rglob('*'):
                if path.suffix not in ('.py', '.js', '.jsx') or 'node_modules' in path.parts:
                    continue
                if path.name == pathlib.Path(__file__).name:
                    continue
                text = path.read_text(encoding='utf-8', errors='replace')
                if re.search(r'vision_(quota|used)_per_term|(consume|refund|reset)_vision_quota', text):
                    hits.append(str(path.relative_to(_REPO)))
        self.assertEqual(hits, [], 'a vision-quota reader appeared: 055 made the columns server-only')

    def test_the_044_header_names_the_assertion_055_breaks(self):
        header = ' '.join(line[2:].strip() for line in _read(A044).splitlines() if line.startswith('--'))
        self.assertIn('055 BREAKS', header)
        self.assertIn('T7', header)


if __name__ == '__main__':
    unittest.main()

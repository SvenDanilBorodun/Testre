# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""Migration 056 (2026-11-12, wave-A verification VA-04): an admin reads other
people's rows only in a two-factor session. What the SQL files must keep saying,
read as text; the behaviour is proven by the hand-run
supabase/tests/056_admin_requires_aal2_assertions.sql (never CI: it needs a
database). Deliberately stdlib-only.

* public.rls_is_admin() keeps 031's signature, SECURITY DEFINER and
  `SET search_path = public` and requires `auth.jwt() ->> 'aal' = 'aal2'`.
* The nine inline admin branches become `(SELECT public.rls_is_admin())` by
  ALTER POLICY, and EVERY OTHER branch of each predicate stays byte-for-byte what
  024/034/040 wrote (compared here after whitespace normalisation), so no
  teacher or student path moves.
* The rollback restores 031's helper and those nine predicates verbatim.
* Not API-visible: the boot schema probe does not name it.
"""

import pathlib
import re
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SUPABASE = _REPO / 'robotis_ai_setup' / 'supabase'
_MIG = _SUPABASE / 'migrations'
M024 = _MIG / '20260520130522_024_advisor_fixes.sql'
M031 = _MIG / '20260609140000_031_fix_rls_recursion.sql'
M034 = _MIG / '20260701120000_034_workflow_trajectories.sql'
M040 = _MIG / '20260920120000_040_code_programs.sql'
M055 = _MIG / '20261111120000_055_users_identity_guard.sql'
M056 = _MIG / '20261112120000_056_admin_requires_aal2.sql'
R056 = _SUPABASE / 'rollback' / '20261112120000_056_admin_requires_aal2_rollback.sql'
A056 = _SUPABASE / 'tests' / '056_admin_requires_aal2_assertions.sql'
MAIN = _REPO / 'robotis_ai_setup' / 'cloud_training_api' / 'app' / 'main.py'
CLAUDE = _REPO / 'CLAUDE.md'

# policy name -> (table, the migration that last wrote its USING)
POLICIES = {
    'Progress entries read consolidated': ('progress_entries', M024),
    'Workflows read consolidated': ('workflows', M024),
    'Workgroups read consolidated': ('workgroups', M024),
    'Memberships read consolidated': ('workgroup_memberships', M024),
    'Datasets read consolidated': ('datasets', M024),
    'Workflow versions read consolidated': ('workflow_versions', M024),
    'Tutorial progress read consolidated': ('tutorial_progress', M024),
    'Admin reads all workflow trajectories': ('workflow_trajectories', M034),
    'Admin reads all submissions': ('workflow_submissions', M040),
}
HELPER_BRANCH = '(SELECT public.rls_is_admin())'
INLINE_ADMIN = re.compile(
    r"EXISTS\s*\(\s*SELECT 1 FROM public\.users\s+WHERE id = \(SELECT auth\.uid\(\)\) AND role = 'admin'\s*\)")


def _read(path):
    return path.read_text(encoding='utf-8')


def _code(sql):
    return '\n'.join(line.split('--', 1)[0] for line in sql.splitlines())


def _norm(text):
    return ' '.join(text.split())


def _balanced(text, start):
    """The text inside the parentheses that open at ``start``."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == '(':
            depth += 1
        elif text[i] == ')':
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
    raise AssertionError('unbalanced parentheses')


def _using(code, statement, name):
    """The USING expression of `<statement> "<name>" …`."""
    m = re.search(statement + r'\s+"' + re.escape(name) + r'"[^;]*?USING\s*\(', code, re.S)
    assert m, f'{statement} "{name}" not found'
    return _balanced(code, m.end() - 1)


def _function_body(code, name='public.rls_is_admin'):
    m = re.search(r'CREATE OR REPLACE FUNCTION ' + re.escape(name) + r'\(\)(.*?)AS \$\$(.*?)\$\$;', code, re.S)
    assert m, name
    return m.group(1), m.group(2)


class FilesAndOrder(unittest.TestCase):
    def test_every_file_exists_and_056_follows_055(self):
        for p in (M056, R056, A056):
            self.assertTrue(p.is_file(), p)
        names = sorted(p.name for p in _MIG.glob('*.sql'))
        # 056 follows 055 directly; after it come the deep audit's 057 (request
        # roles default-deny), 058 (refuse self-registration) and 059 (reserved
        # Hugging Face namespaces), 2026-11-13.
        self.assertEqual(names[names.index(M055.name):], [
            M055.name, M056.name,
            '20261113120000_057_request_role_default_deny.sql',
            '20261113130000_058_refuse_self_signup.sql',
            '20261113140000_059_hf_reserved_namespaces.sql',
        ])

    def test_one_transaction_each(self):
        for p in (M056, R056):
            code = _code(_read(p))
            self.assertEqual(code.count('BEGIN;'), 1, p.name)
            self.assertTrue(code.strip().startswith('BEGIN;'), p.name)
            self.assertTrue(code.rstrip().endswith('COMMIT;'), p.name)


class TheHelper(unittest.TestCase):
    def test_056_requires_admin_and_aal2_and_keeps_definer_and_search_path(self):
        head, body = _function_body(_code(_read(M056)))
        self.assertIn('SECURITY DEFINER', head)
        self.assertIn('SET search_path = public', head)
        self.assertIn('RETURNS boolean', head)
        self.assertIn("(SELECT u.role FROM public.users u WHERE u.id = (SELECT auth.uid())) = 'admin'", body)
        self.assertIn("AND (SELECT auth.jwt() ->> 'aal') = 'aal2'", body)
        self.assertIn('false', body)            # COALESCE(…, false): a missing claim is no admin

    def test_the_rollback_restores_031s_helper_verbatim(self):
        h031, b031 = _function_body(_code(_read(M031)))
        hr, br = _function_body(_code(_read(R056)))
        self.assertEqual(_norm(br), _norm(b031))
        self.assertEqual(_norm(hr), _norm(h031))
        self.assertNotIn('aal', br)


class TheNinePolicies(unittest.TestCase):
    def test_056_swaps_only_the_admin_branch(self):
        code056 = _code(_read(M056))
        for name, (table, source) in POLICIES.items():
            with self.subTest(policy=name):
                original = _using(_code(_read(source)), 'CREATE POLICY', name)
                self.assertEqual(len(INLINE_ADMIN.findall(original)), 1, 'the source has one admin branch')
                expected = INLINE_ADMIN.sub(HELPER_BRANCH, original)
                new = _using(code056, 'ALTER POLICY', name)
                self.assertEqual(_norm(new), _norm(expected))
                self.assertRegex(code056, r'ALTER POLICY "' + re.escape(name) + r'" ON public\.' + table + r'\b')

    def test_the_rollback_restores_each_predicate_verbatim(self):
        code_r = _code(_read(R056))
        for name, (table, source) in POLICIES.items():
            with self.subTest(policy=name):
                original = _using(_code(_read(source)), 'CREATE POLICY', name)
                self.assertEqual(_norm(_using(code_r, 'ALTER POLICY', name)), _norm(original))

    def test_no_inline_admin_check_is_left_in_056(self):
        code056 = _code(_read(M056))
        outside_function = re.sub(r'AS \$\$.*?\$\$;', '', code056, flags=re.S)
        outside_checks = re.sub(r'DO \$\$.*?\$\$;', '', outside_function, flags=re.S)
        self.assertNotRegex(outside_checks, r"role\s*=\s*'admin'")
        self.assertEqual(outside_checks.count('ALTER POLICY'), len(POLICIES))
        self.assertNotIn('DROP POLICY', outside_checks)       # same names, commands, roles, comments
        self.assertNotIn('CREATE POLICY', outside_checks)

    def test_the_self_check_pins_the_eleven(self):
        code056 = _code(_read(M056))
        check = re.search(r'DO \$\$(.*?)\$\$;', code056, re.S).group(1)
        for name, (table, _src) in POLICIES.items():
            self.assertIn(f"'{table}/{name}'", check)
        self.assertIn("'users/Users read consolidated'", check)
        self.assertIn("'classrooms/Classrooms read consolidated'", check)
        self.assertIn("has_function_privilege(v_role, 'public.rls_is_admin()', 'EXECUTE')", check)

    def test_the_header_lists_the_inventory(self):
        header = _read(M056).split('BEGIN;', 1)[0]
        for name in list(POLICIES) + ['Users read consolidated', 'Classrooms read consolidated',
                                      'Trainings read consolidated']:
            self.assertIn(name, header)
        for fn in ('credit_admin_grant', 'credit_erase_reason', 'consume_vision_quota',
                   'rls_teacher_classroom_ids', 'rls_user_classroom_id'):
            self.assertIn(fn, header)


class NotApiVisible(unittest.TestCase):
    def test_the_boot_probe_is_untouched(self):
        self.assertNotIn('rls_is_admin', _read(MAIN))

    def test_claude_md_describes_056(self):
        # 056 was the latest until the deep audit's 057-059 (2026-11-13).
        self.assertIn('**056** (`admin_requires_aal2`, file `20261112120000_056_admin_requires_aal2.sql`', _read(CLAUDE))


if __name__ == '__main__':
    unittest.main()

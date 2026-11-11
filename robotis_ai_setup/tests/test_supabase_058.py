# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""Migration 058 (2026-11-13, security audit SA-03, code half): a new auth.users
row commits only with app_metadata {"edubotics_provisioned": true}. What the
files must keep saying, read as text; the behaviour is proven by the hand-run
supabase/tests/058_refuse_self_signup_assertions.sql. Deliberately stdlib-only.

* The check is a DEFERRED constraint trigger on public.users (Supabase Auth
  writes app_metadata with an UPDATE after its INSERT, so an immediate check
  would refuse the server's own accounts); 058 never drops or replaces a trigger
  on auth.users.
* ONE marker: the migration, cloud_training_api/app/services/provisioning.py and
  scripts/bootstrap_admin.py spell the same key.
* EVERY create path sends it: every ``auth.admin.create_user(...)`` call in the
  shipped Python passes ``provisioning.new_account_attributes(...)`` or a dict
  literal whose ``app_metadata`` carries the marker; every hand-run assertion
  file's auth.users fixture carries it.
"""

import ast
import pathlib
import re
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_ROOT = _REPO / 'robotis_ai_setup'
_SUPABASE = _ROOT / 'supabase'
M058 = _SUPABASE / 'migrations' / '20261113130000_058_refuse_self_signup.sql'
R058 = _SUPABASE / 'rollback' / '20261113130000_058_refuse_self_signup_rollback.sql'
A058 = _SUPABASE / 'tests' / '058_refuse_self_signup_assertions.sql'
PROVISIONING = _ROOT / 'cloud_training_api' / 'app' / 'services' / 'provisioning.py'
BOOTSTRAP = _ROOT / 'scripts' / 'bootstrap_admin.py'
KEY = 'edubotics_provisioned'


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding='utf-8')


def _code(sql: str) -> str:
    return '\n'.join(line.split('--', 1)[0] for line in sql.splitlines())


def _shipped_python():
    for path in sorted(_REPO.rglob('*.py')):
        rel = path.relative_to(_REPO).parts
        if 'node_modules' in rel or 'tests' in rel or 'test' in rel or rel[0].startswith('.'):
            continue
        yield path


class Migration058(unittest.TestCase):
    def test_the_files_exist_and_058_follows_057(self):
        for path in (M058, R058, A058, PROVISIONING):
            self.assertTrue(path.is_file(), path)
        names = sorted(p.name for p in (_SUPABASE / 'migrations').glob('*.sql'))
        self.assertEqual(names[names.index(M058.name) - 1], '20261113120000_057_request_role_default_deny.sql')

    def test_a_deferred_constraint_trigger_on_public_users_and_nothing_on_auth_users(self):
        code = _code(_read(M058))
        flat = ' '.join(code.split())
        self.assertIn('CREATE CONSTRAINT TRIGGER trg_users_require_provisioned AFTER INSERT ON public.users '
                      'DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION '
                      'public.users_require_provisioned_account()', flat)
        self.assertNotRegex(code, r'(?i)(DROP|CREATE)\s+(OR\s+REPLACE\s+)?(CONSTRAINT\s+)?TRIGGER[^;]*ON\s+auth\.users')
        self.assertNotRegex(code, r'(?i)CREATE\s+OR\s+REPLACE\s+FUNCTION\s+public\.handle_new_user')
        self.assertIn("SECURITY DEFINER\nSET search_path = ''", _read(M058))
        # It re-reads the row at COMMIT, and only a JSON true counts.
        self.assertIn('FROM auth.users AS u WHERE u.id = NEW.id', code)
        self.assertIn(f"(v_app -> '{KEY}') IS DISTINCT FROM 'true'::jsonb", code)
        self.assertIn('REVOKE ALL ON FUNCTION public.users_require_provisioned_account() FROM PUBLIC, anon, authenticated, service_role',
                      flat)

    def test_the_rollback_drops_exactly_what_058_added(self):
        code = ' '.join(_code(_read(R058)).split())
        self.assertIn('DROP TRIGGER IF EXISTS trg_users_require_provisioned ON public.users', code)
        self.assertIn('DROP FUNCTION IF EXISTS public.users_require_provisioned_account()', code)
        self.assertIn('RE-OPENS SELF-REGISTRATION', _read(R058))


class OneMarker(unittest.TestCase):
    def test_the_three_spellings_agree(self):
        prov = ast.parse(_read(PROVISIONING))
        boot = ast.parse(_read(BOOTSTRAP))

        def const(tree, name):
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(getattr(t, 'id', None) == name for t in node.targets):
                    return node.value.value
            self.fail(name)

        self.assertEqual(const(prov, 'PROVISIONED_KEY'), KEY)
        self.assertEqual(const(boot, 'PROVISIONED_KEY'), KEY)
        self.assertIn(KEY, _read(M058))

    def test_every_create_user_call_sends_the_marker(self):
        calls = []
        for path in _shipped_python():
            try:
                tree = ast.parse(_read(path))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == 'create_user'):
                    calls.append((path, node))
        self.assertGreaterEqual(len(calls), 3, 'teacher.py, admin.py and bootstrap_admin.py')
        for path, node in calls:
            arg = node.args[0] if node.args else None
            ok = False
            if isinstance(arg, ast.Call):
                fn = arg.func
                ok = (getattr(fn, 'attr', None) or getattr(fn, 'id', None)) == 'new_account_attributes'
            elif isinstance(arg, ast.Dict):
                for k, v in zip(arg.keys, arg.values):
                    if isinstance(k, ast.Constant) and k.value == 'app_metadata' and isinstance(v, ast.Dict):
                        ok = any((isinstance(kk, ast.Constant) and kk.value == KEY)
                                 or (isinstance(kk, ast.Name) and kk.id == 'PROVISIONED_KEY')
                                 for kk in v.keys)
            self.assertTrue(ok, f'{path.relative_to(_REPO)}:{node.lineno} creates an account without the marker')

    def test_every_assertion_fixture_carries_the_marker(self):
        head = 'INSERT INTO auth.users ('
        for path in sorted((_SUPABASE / 'tests').glob('*_assertions.sql')):
            if path == A058:
                continue  # 058's own file inserts unmarked rows on purpose
            text = _read(path)
            i = 0
            while (j := text.find(head, i)) >= 0:
                stmt = text[j:text.index(';', j)]
                with self.subTest(file=path.name, at=j):
                    self.assertIn('raw_app_meta_data', stmt.split(')', 1)[0])
                    self.assertIn(f'"{KEY}": true', stmt)
                i = j + 1


if __name__ == '__main__':
    unittest.main()

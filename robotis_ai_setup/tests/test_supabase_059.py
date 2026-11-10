# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""Migration 059 (2026-11-13, security audit SA-08): the platform's Hugging Face
namespaces are never a student's. What the files must keep saying, read as
text; the behaviour is proven by the hand-run
supabase/tests/059_hf_reserved_namespaces_assertions.sql. Deliberately
stdlib-only.

* ONE organisation constant: the seed equals ``hf_identity.HF_ORG`` lowercased,
  and ``training_dispatch`` imports the constant instead of spelling it.
* Both triggers refuse for EVERY caller (no current_user test), case-
  insensitively (``lower(btrim(...))``), with the codes the API already maps
  (P0059 for the token save, P0034 = register_dataset_safe's own).
* The users trigger sorts AFTER 044's guard (``trg_0_...`` < ``trg_1_...``).
* The table is service-role only and never published; the API never deletes
  from it (no DELETE grant).
* The rollback drops exactly what 059 added.
"""

import ast
import pathlib
import re
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_ROOT = _REPO / 'robotis_ai_setup'
_SUPABASE = _ROOT / 'supabase'
M059 = _SUPABASE / 'migrations' / '20261113140000_059_hf_reserved_namespaces.sql'
R059 = _SUPABASE / 'rollback' / '20261113140000_059_hf_reserved_namespaces_rollback.sql'
A059 = _SUPABASE / 'tests' / '059_hf_reserved_namespaces_assertions.sql'
HF_IDENTITY = _ROOT / 'cloud_training_api' / 'app' / 'services' / 'hf_identity.py'
DISPATCH = _ROOT / 'cloud_training_api' / 'app' / 'services' / 'training_dispatch.py'


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding='utf-8')


def _code(sql: str) -> str:
    return '\n'.join(line.split('--', 1)[0] for line in sql.splitlines())


def _flat(sql: str) -> str:
    return ' '.join(_code(sql).split())


def _hf_org() -> str:
    tree = ast.parse(_read(HF_IDENTITY))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == 'HF_ORG' for t in node.targets):
            return node.value.value
    raise AssertionError('hf_identity.HF_ORG not found')


class Migration059(unittest.TestCase):
    def test_the_files_exist_and_059_follows_058(self):
        for path in (M059, R059, A059):
            self.assertTrue(path.is_file(), path)
        names = sorted(p.name for p in (_SUPABASE / 'migrations').glob('*.sql'))
        self.assertEqual(names[names.index(M059.name) - 1], '20261113130000_058_refuse_self_signup.sql')

    def test_the_seed_is_the_one_organisation_constant(self):
        org = _hf_org()
        self.assertEqual(org, 'EduBotics-Solutions')
        self.assertIn(f"VALUES ('{org.lower()}', 'organisation')", _read(M059))
        # training_dispatch takes the constant; it does not spell it.
        dispatch = ast.parse(_read(DISPATCH))
        self.assertFalse([n for n in ast.walk(dispatch) if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == 'HF_ORG' for t in n.targets)])
        self.assertIn('from app.services.hf_identity import HF_ORG', _read(DISPATCH))

    def test_the_table_is_service_role_only(self):
        flat = _flat(_read(M059))
        self.assertIn('ALTER TABLE public.hf_reserved_namespaces ENABLE ROW LEVEL SECURITY', flat)
        self.assertIn('REVOKE ALL ON public.hf_reserved_namespaces FROM PUBLIC, anon, authenticated, service_role', flat)
        self.assertIn('GRANT SELECT, INSERT, UPDATE ON public.hf_reserved_namespaces TO service_role', flat)
        self.assertNotRegex(flat, r'(?i)GRANT[^;]*hf_reserved_namespaces[^;]*TO[^;]*(anon|authenticated|PUBLIC)')
        self.assertNotRegex(flat, r'(?i)supabase_realtime[^;]*hf_reserved_namespaces')
        self.assertNotRegex(flat, r'(?i)CREATE POLICY[^;]*hf_reserved_namespaces')

    def test_both_triggers_refuse_every_caller_case_insensitively(self):
        code = _code(_read(M059))
        flat = _flat(_read(M059))
        self.assertNotIn('current_user', code)
        self.assertIn('WHERE r.name_lower = lower(btrim(NEW.hf_username))', code)
        self.assertIn("WHERE r.name_lower = lower(btrim(split_part(NEW.hf_repo_id, '/', 1)))", code)
        self.assertIn("USING ERRCODE = 'P0059'", code)
        self.assertIn("USING ERRCODE = 'P0034'", code)
        self.assertIn('CREATE TRIGGER trg_1_users_hf_username_not_reserved BEFORE INSERT OR UPDATE OF hf_username '
                      'ON public.users FOR EACH ROW EXECUTE FUNCTION public.users_hf_username_not_reserved()', flat)
        self.assertIn('CREATE TRIGGER trg_datasets_namespace_not_reserved BEFORE INSERT OR UPDATE OF hf_repo_id '
                      'ON public.datasets FOR EACH ROW EXECUTE FUNCTION public.datasets_namespace_not_reserved()', flat)
        self.assertLess('trg_0_users_protected_columns', 'trg_1_users_hf_username_not_reserved')
        for fn in ('users_hf_username_not_reserved', 'datasets_namespace_not_reserved'):
            self.assertRegex(_read(M059), rf"FUNCTION public\.{fn}\(\)\nRETURNS trigger\nLANGUAGE plpgsql\n"
                                          rf"SECURITY DEFINER\nSET search_path = ''")
            self.assertIn(f'REVOKE ALL ON FUNCTION public.{fn}() FROM PUBLIC, anon, authenticated, service_role', flat)

    def test_the_api_maps_both_codes_to_german(self):
        route = _read(_ROOT / 'cloud_training_api' / 'app' / 'routes' / 'hf_token.py')
        self.assertIn('"P0059" in str(exc)', route)
        datasets = _read(_ROOT / 'cloud_training_api' / 'app' / 'routes' / 'datasets.py')
        self.assertIn('P0034', datasets)

    def test_the_data_fix_clears_only_unbacked_forged_names(self):
        code = _code(_read(M059))
        self.assertRegex(code, r'UPDATE public\.users u\s+SET hf_username = NULL')
        self.assertIn('AND NOT EXISTS (SELECT 1 FROM public.user_hf_credentials c WHERE c.user_id = u.id)', code)

    def test_it_runs_in_one_transaction_and_checks_itself(self):
        code = _code(_read(M059))
        self.assertTrue(code.lstrip().startswith('BEGIN;'))
        self.assertTrue(code.rstrip().endswith('COMMIT;'))
        self.assertIn("RAISE NOTICE '059 self-check passed'", code)

    def test_the_rollback_drops_exactly_what_059_added(self):
        flat = _flat(_read(R059))
        for stmt in ('DROP TRIGGER IF EXISTS trg_1_users_hf_username_not_reserved ON public.users',
                     'DROP TRIGGER IF EXISTS trg_datasets_namespace_not_reserved ON public.datasets',
                     'DROP FUNCTION IF EXISTS public.users_hf_username_not_reserved()',
                     'DROP FUNCTION IF EXISTS public.datasets_namespace_not_reserved()',
                     'DROP TABLE IF EXISTS public.hf_reserved_namespaces'):
            self.assertIn(stmt, flat)
        self.assertIn('RE-OPENS', _read(R059))
        self.assertEqual(len(re.findall(r'(?i)\bDROP\b', _code(_read(R059)))), 5)

    def test_the_assertion_fixture_carries_the_provisioning_marker(self):
        self.assertIn('"edubotics_provisioned": true', _read(A059))
        self.assertIn('-- Expected: 13 PASS', _read(A059))

    def test_the_schema_probe_does_not_name_the_table(self):
        # Best effort by design: an API on a database without 059 must boot.
        main = _read(_ROOT / 'cloud_training_api' / 'app' / 'main.py')
        self.assertNotIn('"hf_reserved_namespaces"', main)


if __name__ == '__main__':
    unittest.main()

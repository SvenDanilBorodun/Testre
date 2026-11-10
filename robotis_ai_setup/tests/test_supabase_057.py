# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""Migration 057 (2026-11-13, security audit SA-10 + the owner's scope
extension): the request roles anon and authenticated are default-deny for
reads, writes and EXECUTE in schema public. What the files must keep saying,
read as text; the behaviour is proven by the hand-run
supabase/tests/057_request_role_default_deny_assertions.sql and, in CI, by
.github/scripts/supabase_request_role_check.py after a replay of every
migration (ci.yml::migrations-replay). Deliberately stdlib-only.

* ONE allowlist: supabase/request_role_allowlist.json. 057's two temp-table
  lists (reads, EXECUTE) equal it; an entry the JSON gains later must be granted
  by name in a later migration; the CI check reads the JSON itself.
* The trainings read is exactly the SPA's LIVE_COLUMNS plus user_id (the
  Realtime and REST filter column): changing LIVE_COLUMNS needs a migration.
* 057 revokes everything (tables, columns, sequences, routines, default
  privileges, the global PUBLIC EXECUTE) and grants only from its lists; it
  never revokes from service_role.
* The rollback restores the pre-057 grants and says it re-opens the hole.
"""

import json
import pathlib
import re
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SUPABASE = _REPO / 'robotis_ai_setup' / 'supabase'
_MIG = _SUPABASE / 'migrations'
M057 = _MIG / '20261113120000_057_request_role_default_deny.sql'
R057 = _SUPABASE / 'rollback' / '20261113120000_057_request_role_default_deny_rollback.sql'
A057 = _SUPABASE / 'tests' / '057_request_role_default_deny_assertions.sql'
ALLOW = _SUPABASE / 'request_role_allowlist.json'
CHECK = _REPO / '.github' / 'scripts' / 'supabase_request_role_check.py'
STUB = _REPO / '.github' / 'scripts' / 'supabase_replay_stub.sql'
LIVE_JS = _REPO / 'physical_ai_tools' / 'physical_ai_manager' / 'src' / 'hooks' / 'useSupabaseTrainings.js'
TRAINING_GRANT_MIGRATIONS = [
    _MIG / '20261020120000_047_hide_worker_token.sql',
    _MIG / '20261020130000_049_managed_training.sql',
    _MIG / '20261021120000_050_training_continuation.sql',
    _MIG / '20261028120000_052_credit_ledger.sql',
]


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding='utf-8')


def _code(sql: str) -> str:
    """The SQL without its comments."""
    return '\n'.join(line.split('--', 1)[0] for line in sql.splitlines())


def _statements(code: str) -> list[str]:
    """Top-level statements outside DO blocks, whitespace-normalised."""
    outside = re.sub(r'DO \$\$.*?\$\$;', '', code, flags=re.S)
    return [' '.join(s.split()) for s in outside.split(';') if s.strip()]


def _array(text: str) -> list[str]:
    return re.findall(r"'([^']*)'", text)


def _057_reads(sql: str) -> dict:
    """057's _057_read_allow rows: table -> (roles, columns)."""
    block = sql[sql.index('INSERT INTO _057_read_allow VALUES'):]
    block = block[:block.index(';')]
    rows = {}
    for m in re.finditer(r"\('(public\.\w+)',\s*ARRAY\[([^\]]*)\],\s*ARRAY\[([^\]]*)\]\)", block, re.S):
        rows[m.group(1)] = (sorted(_array(m.group(2))), sorted(_array(m.group(3))))
    return rows


def _057_exec(sql: str) -> dict:
    """057's _057_exec_allow rows: function signature -> roles."""
    block = sql[sql.index('INSERT INTO _057_exec_allow VALUES'):]
    block = block[:block.index(';')]
    return {m.group(1): sorted(_array(m.group(2)))
            for m in re.finditer(r"\('(public\.[^']*)',\s*ARRAY\[([^\]]*)\]\)", block)}


def _json() -> dict:
    return json.loads(_read(ALLOW))


def _live_columns() -> list[str]:
    m = re.search(r"export const LIVE_COLUMNS =\s*((?:'[^']*'\s*\+?\s*)+);", _read(LIVE_JS))
    assert m, "useSupabaseTrainings.js: no `export const LIVE_COLUMNS = '…' + '…';`"
    return ''.join(re.findall(r"'([^']*)'", m.group(1))).split(',')


def _later_migrations() -> list[pathlib.Path]:
    return [p for p in sorted(_MIG.glob('*.sql')) if p.name > M057.name]


class Files(unittest.TestCase):
    def test_the_files_exist_and_057_sorts_after_055(self):
        for path in (M057, R057, A057, ALLOW, CHECK, STUB):
            self.assertTrue(path.is_file(), path)
        names = sorted(p.name for p in _MIG.glob('*.sql'))
        self.assertGreater(M057.name, '20261111120000_055_users_identity_guard.sql')
        # 056 (admin aal2) is written in parallel and sorts between 055 and 057.
        self.assertEqual([n for n in names[names.index(M057.name) + 1:]],
                         ['20261113130000_058_refuse_self_signup.sql'])

    def test_one_transaction_each(self):
        for path in (M057, R057):
            code = _code(_read(path))
            self.assertEqual(code.count('BEGIN;'), 1, path.name)
            self.assertTrue(code.rstrip().endswith('COMMIT;'), path.name)


class OneAllowlist(unittest.TestCase):
    def test_the_reads_equal_the_json(self):
        json_reads = {r['table']: (sorted(r['roles']), sorted(r['columns'])) for r in _json()['reads']}
        self.assertEqual(_057_reads(_read(M057)), json_reads)

    def test_the_functions_equal_the_json(self):
        json_fns = {f['function']: sorted(f['roles']) for f in _json()['functions']}
        mig = _057_exec(_read(M057))
        later = '\n'.join(_code(_read(p)) for p in _later_migrations())
        for fn, roles in json_fns.items():
            if fn in mig:
                self.assertEqual(mig[fn], roles, fn)
            else:
                # Added after 057: a later migration must grant it by name.
                name = fn.split('(')[0]
                self.assertRegex(later, rf'GRANT\s+EXECUTE\s+ON\s+FUNCTION\s+{re.escape(name)}\s*\(', fn)
        self.assertEqual(sorted(set(mig) - set(json_fns)), [])

    def test_every_entry_says_why_and_names_only_request_roles(self):
        doc = _json()
        for entry in doc['reads'] + doc['functions']:
            self.assertGreater(len(entry['why']), 40, entry)
            self.assertTrue(set(entry['roles']) <= {'anon', 'authenticated'}, entry)
        # anon reads nothing: every read is an authenticated session.
        self.assertEqual({r for e in doc['reads'] for r in e['roles']}, {'authenticated'})
        # The rest stays empty until a migration adds to it on purpose.
        for key in ('tables', 'sequences', 'storage_policies', 'public_buckets'):
            self.assertFalse(doc[key], key)

    def test_the_trainings_read_is_live_columns_plus_user_id(self):
        reads = {r['table']: r for r in _json()['reads']}
        live = _live_columns()
        self.assertEqual(sorted(reads['public.trainings']['columns']), sorted(set(live) | {'user_id'}))
        for hidden in ('worker_token', 'credit_scheme', 'settled_at', 'idempotency_key', 'stop_requested_at',
                       'lost_reasons', 'charge_waiver'):
            self.assertNotIn(hidden, reads['public.trainings']['columns'])

    def test_the_ci_check_reads_the_json(self):
        text = _read(CHECK)
        self.assertIn('request_role_allowlist.json', text)
        self.assertNotRegex(text, r"rls_is_admin|inference_session_peek|'hf_repo_id'",
                            'the CI check holds no allowlist of its own')


class Migration057(unittest.TestCase):
    def test_everything_is_revoked_from_the_request_roles_and_public(self):
        statements = _statements(_code(_read(M057)))
        for stmt in ('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC, anon, authenticated',
                     'REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, anon, authenticated',
                     'REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA public FROM PUBLIC, anon, authenticated',
                     'DROP POLICY IF EXISTS "Trigger inserts workflow versions" ON public.workflow_versions'):
            self.assertIn(stmt, statements)
        code = _code(_read(M057))
        # The defaults, per schema and globally (the built-in PUBLIC EXECUTE).
        self.assertIn("IN SCHEMA public REVOKE ALL ON TABLES FROM PUBLIC, anon, authenticated", code)
        self.assertIn("FOR ROLE %I REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC, anon, authenticated", code)
        self.assertIn("FOR ROLE %I GRANT EXECUTE ON FUNCTIONS TO service_role", code)
        # The column-level grants go too (SELECT included).
        self.assertIn('REVOKE SELECT (%2$I), INSERT (%2$I), UPDATE (%2$I), REFERENCES (%2$I) ON %1$s FROM PUBLIC, anon, authenticated',
                      code)

    def test_the_only_grants_come_from_the_lists_and_service_role_is_never_revoked(self):
        code = _code(_read(M057))
        outside_do = ' '.join(_statements(code))
        self.assertNotRegex(outside_do, r'\bGRANT\b')
        self.assertNotRegex(code, r'REVOKE[^;\n]*FROM[^;\n]*service_role')
        grants = re.findall(r"EXECUTE format\('GRANT ([^']*)'", code)
        self.assertEqual(sorted(grants), sorted([
            'EXECUTE ON FUNCTION %s TO service_role',   # 3a: what service_role reached through PUBLIC
            'SELECT (%s) ON %s TO %s',                   # the read allowlist
            'EXECUTE ON FUNCTION %s TO %s',              # the function allowlist
        ]))

    def test_the_storage_drift_and_pg_stat_statements(self):
        code = _code(_read(M057))
        self.assertIn('''DROP POLICY "Service role upload to releases" ON storage.objects''', code)
        self.assertIn("UPDATE storage.buckets SET public = false WHERE id = ''releases'' AND public", code)
        self.assertIn("REVOKE SELECT ON %s FROM anon, authenticated", code)
        self.assertNotRegex(code, r'DELETE\s+FROM\s+storage')

    def test_the_self_check_judges_reads_writes_execute_defaults_and_storage(self):
        sql = _read(M057)
        check = sql[sql.index('-- 8. The self-check.'):]
        for needle in ("aclexplode(c.relacl)", "has_column_privilege(r.rolname, c.oid, a.attnum, 'SELECT')",
                       "is not on the read allowlist", "which is not on the allowlist",
                       "has_any_column_privilege(rr.rolname, pol.polrelid, 'SELECT')",
                       "CREATE TABLE public._057_probe", "public storage bucket", "supabase_realtime",
                       "service_role privileges changed"):
            self.assertIn(needle, check)


class Rollback057(unittest.TestCase):
    def test_it_says_it_reopens_the_hole(self):
        self.assertIn('THIS RE-OPENS THE HOLE 057 CLOSED', _read(R057))

    def test_it_restores_the_047_to_052_trainings_columns(self):
        granted = set()
        for path in TRAINING_GRANT_MIGRATIONS:
            sql = _code(_read(path))
            for m in re.finditer(r"GRANT\s+SELECT\s*\(([^)]*)\)\s*ON\s+(?:public\.)?trainings\s+TO\s+anon\s*,\s*authenticated",
                                 sql, re.I):
                granted.update(c.strip() for c in m.group(1).split(',') if c.strip())
        m = re.search(r'GRANT SELECT \(([^)]*)\)\s*ON public\.trainings TO anon, authenticated;', _code(_read(R057)))
        self.assertIsNotNone(m)
        self.assertEqual({c.strip() for c in m.group(1).split(',')}, granted)

    def test_it_does_not_restore_the_storage_drift(self):
        code = _code(_read(R057))
        self.assertNotIn('Service role upload to releases', code)
        self.assertNotRegex(code, r'storage\.buckets')


if __name__ == '__main__':
    unittest.main()

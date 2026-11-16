# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""Migration 060 (2026-11-16, security audit SA-22 closed): the server's query
statistics are not readable by the request roles. What the files must keep
saying, read as text; the behaviour is proven by the hand-run
supabase/tests/060_query_stats_not_public_assertions.sql and, on every push, by
ci.yml::migrations-replay (.github/scripts/supabase_request_role_check.py
reports any request-role read of them). Deliberately stdlib-only.

* PUBLIC's grants go, not only the request roles' direct ones (057 revoked
  those and left PUBLIC's, the read that remained).
* The monitoring role keeps the two readers; nobody new may reset them.
* The self-check aborts where this role can act for the owner and only warns
  where it cannot (a database it does not own must not stop a release).
* The rollback restores PUBLIC's grants and nothing else.
"""

import pathlib
import re
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SUPABASE = _REPO / 'robotis_ai_setup' / 'supabase'
M060 = _SUPABASE / 'migrations' / '20261116120000_060_query_stats_not_public.sql'
R060 = _SUPABASE / 'rollback' / '20261116120000_060_query_stats_not_public_rollback.sql'
A060 = _SUPABASE / 'tests' / '060_query_stats_not_public_assertions.sql'
CHECK = _REPO / '.github' / 'scripts' / 'supabase_request_role_check.py'


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding='utf-8')


def _code(sql: str) -> str:
    """The SQL without its comments."""
    return '\n'.join(line.split('--', 1)[0] for line in sql.splitlines())


class Migration060(unittest.TestCase):
    def test_the_files_exist_and_060_follows_059(self):
        for path in (M060, R060, A060):
            self.assertTrue(path.is_file(), path)
        names = sorted(p.name for p in (_SUPABASE / 'migrations').glob('*.sql'))
        self.assertEqual(names[names.index(M060.name) - 1], '20261113140000_059_hf_reserved_namespaces.sql')
        self.assertEqual(names[-1], M060.name)

    def test_one_transaction(self):
        code = _code(_read(M060)).strip()
        self.assertTrue(code.startswith('BEGIN;'), code[:40])
        self.assertTrue(code.endswith('COMMIT;'), code[-40:])

    def test_public_loses_the_read_with_the_request_roles(self):
        code = _code(_read(M060))
        self.assertIn("'REVOKE SELECT ON %s FROM PUBLIC, anon, authenticated'", code)
        self.assertIn("'REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC, anon, authenticated'", code)
        # both views, and every function of the extension by name (the reset's
        # signature differs by extension version).
        self.assertIn("c.relname IN ('pg_stat_statements', 'pg_stat_statements_info')", code)
        self.assertIn("p.proname IN ('pg_stat_statements', 'pg_stat_statements_info', 'pg_stat_statements_reset')",
                      code)
        self.assertIn("to_regnamespace('extensions')", code)

    def test_the_monitoring_role_keeps_the_readers_and_gains_no_reset(self):
        code = _code(_read(M060))
        self.assertIn("'GRANT SELECT ON %s TO pg_read_all_stats'", code)
        self.assertIn("'GRANT EXECUTE ON FUNCTION %s TO pg_read_all_stats'", code)
        self.assertIn("p.proname <> 'pg_stat_statements_reset'", code)
        grants = re.findall(r"GRANT \w+ ON (?:FUNCTION )?%s TO (\w+)", code)
        self.assertEqual(set(grants), {'pg_read_all_stats'})
        # the reset grant is guarded by the reader flag.
        fn_grant = code.index("'GRANT EXECUTE ON FUNCTION %s TO pg_read_all_stats'")
        self.assertIn('IF v_rec.reader THEN', code[code.rindex('ELSE', 0, fn_grant):fn_grant])

    def test_the_self_check_aborts_only_where_this_role_acts_for_the_owner(self):
        code = _code(_read(M060))
        check = code[code.index('v_can_fix := '):]
        self.assertIn("v_can_fix := v_super OR pg_has_role(current_user, v_rec.owner, 'USAGE');", check)
        self.assertIn("has_table_privilege(v_role, v_rec.oid, 'SELECT')", check)
        self.assertIn("has_function_privilege(v_role, v_rec.oid, 'EXECUTE')", check)
        self.assertIn("IF v_open AND v_can_fix THEN\n                RAISE EXCEPTION", check)
        self.assertIn('ELSIF v_open THEN\n                RAISE WARNING', check)
        self.assertIn('aclexplode(v_rec.acl) a WHERE a.grantee = 0', check)
        self.assertIn("RAISE EXCEPTION '060: pg_read_all_stats cannot use %'", check)
        self.assertIn("RAISE EXCEPTION '060: pg_read_all_stats may reset the statistics through %'", check)
        # a NULL acl is read as the default (a function's default is PUBLIC EXECUTE).
        self.assertIn("coalesce(p.proacl, acldefault('f', p.proowner))", code)
        self.assertIn("coalesce(c.relacl, acldefault('r', c.relowner))", code)

    def test_the_rollback_restores_public_and_drops_the_monitoring_grants(self):
        code = _code(_read(R060))
        self.assertIn("'GRANT SELECT ON %s TO PUBLIC'", code)
        self.assertIn("'GRANT EXECUTE ON FUNCTION %s TO PUBLIC'", code)
        self.assertIn("'REVOKE SELECT ON %s FROM pg_read_all_stats'", code)
        self.assertIn("'REVOKE EXECUTE ON FUNCTION %s FROM pg_read_all_stats'", code)
        # the reset never had a PUBLIC grant and gets none.
        self.assertIn("p.proname IN ('pg_stat_statements', 'pg_stat_statements_info')", code)
        self.assertNotIn('pg_stat_statements_reset', code)

    def test_the_replay_check_guards_it(self):
        source = _read(CHECK)
        self.assertIn("out['query_stats']", source)
        self.assertIn("for obj, role in f['query_stats']:", source)


if __name__ == '__main__':
    unittest.main()

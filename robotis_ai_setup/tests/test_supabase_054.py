"""Migration 054 (2026-11-10): the Supabase advisor clean-up. What the SQL files
must keep saying, read as text; the behaviour is proven by the hand-run
supabase/tests/054_advisor_cleanup_assertions.sql (never CI: it needs a
database). Deliberately stdlib-only.

* 054 changes exactly four functions and only through ALTER FUNCTION ... SET
  search_path (the two 052 trigger functions) and REVOKE EXECUTE ... FROM
  PUBLIC, anon, authenticated (the two prune trigger functions): no body is
  re-created, nothing becomes SECURITY DEFINER, service_role keeps its grant.
* trainings_guard_credit_columns() stays SECURITY INVOKER: it judges
  current_user, and a definer would see its owner (044's lesson).
* Its rollback undoes exactly that (RESET search_path; EXECUTE back to PUBLIC,
  anon and authenticated, in their original order) and checks the result.
* The cloud API's boot schema probe names none of the four (no API-visible
  shape changes, so main.py is untouched).
"""

import pathlib
import re
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SUPABASE = _REPO / 'robotis_ai_setup' / 'supabase'
_MIG = _SUPABASE / 'migrations'
M052 = _MIG / '20261028120000_052_credit_ledger.sql'
M054 = _MIG / '20261110120000_054_advisor_cleanup.sql'
R054 = _SUPABASE / 'rollback' / '20261110120000_054_advisor_cleanup_rollback.sql'
A054 = _SUPABASE / 'tests' / '054_advisor_cleanup_assertions.sql'
MAIN = _REPO / 'robotis_ai_setup' / 'cloud_training_api' / 'app' / 'main.py'

PINNED = ('credit_ledger_immutable', 'trainings_guard_credit_columns')
REVOKED = ('prune_workflow_trajectories', 'prune_workflow_submissions')


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding='utf-8')


def _code(sql: str) -> str:
    """The SQL without its comments."""
    return '\n'.join(line.split('--', 1)[0] for line in sql.splitlines())


def _statements(code: str) -> list[str]:
    """Top-level statements outside DO blocks, whitespace-normalised."""
    outside = re.sub(r'DO \$\$.*?\$\$;', '', code, flags=re.S)
    return [' '.join(s.split()) for s in outside.split(';') if s.strip()]


class Migration054(unittest.TestCase):
    def test_the_files_exist_and_054_follows_053(self):
        for path in (M054, R054, A054):
            self.assertTrue(path.is_file(), path)
        names = sorted(p.name for p in _MIG.glob('*.sql'))
        # 054 was the newest until 055 (2026-11-11, the users guard), which
        # touches none of 054's four functions.
        self.assertEqual(names[names.index(M054.name) - 1], '20261109120000_053_retire_jetson.sql')
        # 057 (2026-11-13, the request roles default-deny) revokes EXECUTE on every
        # public routine and grants back an allowlist the four functions are not on,
        # which changes nothing for them; 058 refuses self-registration.
        self.assertEqual(names[names.index(M054.name) + 1:], ['20261111120000_055_users_identity_guard.sql',
                                                              '20261113120000_057_request_role_default_deny.sql',
                                                              '20261113130000_058_refuse_self_signup.sql',
                                                              '20261113140000_059_hf_reserved_namespaces.sql'])

    def test_exactly_the_four_changes_in_one_transaction(self):
        self.assertEqual(_statements(_code(_read(M054))), [
            'BEGIN',
            'ALTER FUNCTION public.credit_ledger_immutable() SET search_path = public',
            'ALTER FUNCTION public.trainings_guard_credit_columns() SET search_path = public',
            'REVOKE EXECUTE ON FUNCTION public.prune_workflow_trajectories() FROM PUBLIC, anon, authenticated',
            'REVOKE EXECUTE ON FUNCTION public.prune_workflow_submissions() FROM PUBLIC, anon, authenticated',
            'COMMIT',
        ])

    def test_no_body_is_recreated_and_nothing_becomes_a_definer(self):
        code = _code(_read(M054))
        self.assertNotRegex(code, r'CREATE\s+(OR\s+REPLACE\s+)?FUNCTION')
        # Outside the self-check (whose messages name SECURITY DEFINER): no
        # SECURITY clause, no GRANT, and nothing revoked from service_role.
        changes = ' '.join(_statements(code))
        self.assertNotRegex(changes, r'(?i)\bSECURITY\b')
        self.assertNotRegex(changes, r'(?i)\bGRANT\b')
        self.assertNotRegex(changes, r'(?i)service_role')

    def test_the_guard_was_and_stays_an_invoker(self):
        # 052 created it SECURITY INVOKER; 054 only pins its path and its self-check
        # aborts if it became a definer.
        m052 = _code(_read(M052))
        head = m052[m052.index('CREATE OR REPLACE FUNCTION public.trainings_guard_credit_columns()'):]
        head = head[:head.index('AS $$')]
        self.assertIn('SECURITY INVOKER', head)
        self.assertNotIn('SET search_path', head)
        self.assertIn("became SECURITY DEFINER", _read(M054))

    def test_the_self_check_names_all_four_and_the_five_triggers(self):
        text = _read(M054)
        for fn in PINNED + REVOKED:
            self.assertIn(f"'{fn}'", text)
        for trigger in ('credit_ledger_no_change', 'credit_ledger_no_truncate', 'trg_0_trainings_credit_guard',
                        'trg_workflow_trajectories_prune', 'trg_workflow_submissions_prune'):
            self.assertIn(f"'{trigger}'", text)
        self.assertIn('054 self-check passed', text)

    def test_the_rollback_undoes_exactly_that(self):
        code = _code(_read(R054))
        self.assertIn("ALTER FUNCTION public.%I() RESET search_path", code)
        self.assertIn("REVOKE EXECUTE ON FUNCTION public.%I() FROM service_role", code)
        self.assertIn("GRANT EXECUTE ON FUNCTION public.%I() TO PUBLIC, anon, authenticated, service_role", code)
        self.assertNotRegex(code, r'CREATE\s+(OR\s+REPLACE\s+)?FUNCTION')
        for fn in PINNED + REVOKED:
            self.assertIn(f"'{fn}'", code)
        self.assertIn('054 rollback self-check passed', code)

    def test_the_boot_probe_names_none_of_the_four(self):
        main = _read(MAIN)
        for fn in PINNED + REVOKED:
            self.assertNotIn(fn, main)


if __name__ == '__main__':
    unittest.main()

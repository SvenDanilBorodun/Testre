# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""The judge of .github/scripts/supabase_request_role_check.py (ci.yml::migrations-replay)
has teeth: fed the catalog facts the allowlist describes it reports nothing, and
every class of hole SA-10 named makes it report. The replay itself needs a
PostgreSQL and runs in CI (and by hand); this file needs none. Stdlib-only."""

import copy
import importlib.util
import json
import re
import pathlib
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / '.github' / 'scripts' / 'supabase_request_role_check.py'
_ALLOW = _REPO / 'robotis_ai_setup' / 'supabase' / 'request_role_allowlist.json'
_CI = _REPO / '.github' / 'workflows' / 'ci.yml'


def _load():
    spec = importlib.util.spec_from_file_location('supabase_request_role_check', _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CHECK = _load()
ALLOW = json.loads(_ALLOW.read_text(encoding='utf-8'))


def clean_facts() -> dict:
    """The catalog the allowlist describes, as facts() would read it."""
    column_acl = [[r['table'], c, role, 'SELECT'] for r in ALLOW['reads'] for role in r['roles'] for c in r['columns']]
    return {
        'relation_acl': [],
        'column_acl': column_acl,
        'columns': [[r['table'], c] for r in ALLOW['reads'] for c in r['columns']]
                   + [['public.users', 'email'], ['public.progress_entries', 'note']],
        'function_exec': [[e['function'], role] for e in ALLOW['functions'] for role in e['roles']],
        'functions': [e['function'] for e in ALLOW['functions']] + ['public.touch_updated_at()'],
        'public_exec': [],
        'write_policies': [],
        'policy_needs': [['public.trainings', 'Trainings read consolidated', 'authenticated', 'column',
                          'public.users.role', 'true']],
        'storage_policies': [],
        'public_buckets': [],
        'default_acl': [['postgres', 'public', 'r', 'service_role', 'SELECT'],
                        ['postgres', '-', 'f', 'service_role', 'EXECUTE']],
        'default_roles': ['postgres'],
        'global_function_default': ['postgres'],
        'probe': [['anon', 'false', 'false', 'false', 'false'],
                  ['authenticated', 'false', 'false', 'false', 'false']],
    }


class TheJudge(unittest.TestCase):
    def test_the_allowlisted_catalog_passes(self):
        self.assertEqual(CHECK.judge(clean_facts(), ALLOW), [])

    def _reports(self, mutate, needle):
        f = clean_facts()
        mutate(f)
        problems = CHECK.judge(f, ALLOW)
        self.assertTrue(any(needle in p for p in problems), (needle, problems))

    def test_a_table_level_write_or_read(self):
        self._reports(lambda f: f['relation_acl'].append(['public.workgroups', 'r', 'authenticated', 'INSERT']),
                      'authenticated holds table-level INSERT on public.workgroups')
        self._reports(lambda f: f['relation_acl'].append(['public.users', 'r', 'anon', 'SELECT']),
                      'anon holds table-level SELECT on public.users')
        self._reports(lambda f: f['relation_acl'].append(['public.users', 'r', 'PUBLIC', 'MAINTAIN']),
                      'PUBLIC holds MAINTAIN on public.users')

    def test_a_sequence_privilege(self):
        self._reports(lambda f: f['relation_acl'].append(['public.trainings_id_seq', 'S', 'authenticated', 'UPDATE']),
                      'authenticated holds UPDATE on sequence public.trainings_id_seq')

    def test_a_read_outside_the_allowlist_and_a_missing_one(self):
        self._reports(lambda f: f['column_acl'].append(['public.users', 'email', 'authenticated', 'SELECT']),
                      'may read public.users.email')
        self._reports(lambda f: f['column_acl'].append(['public.trainings', 'id', 'anon', 'SELECT']),
                      'anon may read public.trainings.id')
        self._reports(lambda f: f['column_acl'].remove(['public.trainings', 'status', 'authenticated', 'SELECT']),
                      'may not read allowlisted public.trainings.status')

    def test_a_column_write(self):
        self._reports(lambda f: f['column_acl'].append(['public.users', 'role', 'authenticated', 'UPDATE']),
                      'authenticated holds UPDATE on public.users.role')

    def test_execute(self):
        self._reports(lambda f: f['function_exec'].append(['public.touch_updated_at()', 'anon']),
                      'anon may execute public.touch_updated_at()')
        self._reports(lambda f: f['function_exec'].remove(['public.rls_is_admin()', 'authenticated']),
                      'may not execute allowlisted public.rls_is_admin()')
        self._reports(lambda f: f['public_exec'].append('public.touch_updated_at()'),
                      'PUBLIC holds EXECUTE on public.touch_updated_at()')

    def test_policies(self):
        self._reports(lambda f: f['write_policies'].append(['public.workflow_versions', 'Trigger inserts', 'a']),
                      'write policy "Trigger inserts"')
        self._reports(lambda f: f['policy_needs'].append(['public.datasets', 'Datasets read', 'authenticated',
                                                           'function', 'public.rls_new()', 'false']),
                      'calls public.rls_new(), which authenticated may not use')

    def test_storage(self):
        self._reports(lambda f: f['storage_policies'].append(['storage.objects', 'Service role upload to releases']),
                      'storage policy "Service role upload to releases"')
        self._reports(lambda f: f['public_buckets'].append('releases'), 'storage bucket "releases" is public')

    def test_default_privileges(self):
        self._reports(lambda f: f['default_acl'].append(['postgres', 'public', 'r', 'anon', 'SELECT']),
                      'grants SELECT to anon')
        self._reports(lambda f: f['global_function_default'].remove('postgres'),
                      'postgres has no global function default')
        self._reports(lambda f: f['probe'].__setitem__(0, ['anon', 'true', 'false', 'false', 'false']),
                      'anon reads a table created after the last migration')
        self._reports(lambda f: f['probe'].__setitem__(1, ['authenticated', 'false', 'false', 'false', 'true']),
                      'authenticated executes a function created after the last migration')

    def test_an_allowlisted_table_write_is_accepted(self):
        allow = copy.deepcopy(ALLOW)
        allow['tables'] = {'public.workgroups': {'authenticated': ['INSERT']}}
        f = clean_facts()
        f['relation_acl'].append(['public.workgroups', 'r', 'authenticated', 'INSERT'])
        self.assertEqual(CHECK.judge(f, allow), [])


class TheJob(unittest.TestCase):
    def test_ci_runs_the_replay_with_teeth_on_a_pinned_postgres(self):
        ci = _CI.read_text(encoding='utf-8')
        start = ci.index('\n  migrations-replay:\n')
        nxt = re.search(r'\n  [a-z][a-z0-9-]*:\n', ci[start + 1:])
        job = ci[start:start + 1 + nxt.start()] if nxt else ci[start:]
        self.assertRegex(job, r'image: postgres:16\.\d+@sha256:[0-9a-f]{64}')
        self.assertIn('python3 .github/scripts/supabase_request_role_check.py --teeth-before 20261113120000', job)
        self.assertIn('persist-credentials: false', job)


if __name__ == '__main__':
    unittest.main()

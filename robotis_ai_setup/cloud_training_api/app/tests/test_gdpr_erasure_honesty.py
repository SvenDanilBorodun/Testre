"""GDPR Art. 17 erasure reports what it actually did (2026-08-06).

Two defects, both of which made the platform CLAIM an erasure it had not
performed — the worst possible failure mode for a deletion right exercised on
behalf of a minor:

  1. ``teacher.py::_delete_student_hf_artifacts`` enumerated the ``trainings``
     table ONLY. A recording that was never used for training has no row there,
     so it was never even looked for. The ``datasets`` registry — where a plain
     recording lands — is now enumerated too.

  2. It authenticates with the PLATFORM ``HF_TOKEN`` against repos in STUDENT
     namespaces, where the delete 403s. That 403 was swallowed by a bare
     ``except Exception`` while ``DELETE /teacher/students/{id}`` answered a
     bare ``{"ok": true}``. Failures are now RETURNED and surfaced.

This is a PATCH, not a fix. Erasure cannot be made to work while datasets live
in per-student HuggingFace accounts the platform token does not own — that
needs the org-account / per-student-sub-namespace change, which is a
docs/plans one-pager and explicitly not code this round.
"""

from __future__ import annotations

import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.tests.test_dataset_identity_routes import _ensure_stubs


class _Table:
    """Minimal supabase table stub: records the table name and filters."""

    def __init__(self, store, name):
        self._store = store
        self._name = name
        self._eq = {}

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._eq[col] = val
        return self

    def execute(self):
        return SimpleNamespace(data=self._store.get(self._name, []))


class _Supabase:
    def __init__(self, store):
        self._store = store
        self.deleted_user = None
        self.auth = SimpleNamespace(
            admin=SimpleNamespace(delete_user=self._delete_user))

    def _delete_user(self, uid):
        self.deleted_user = uid

    def table(self, name):
        return _Table(self._store, name)


class _Api:
    """HfApi stub. `fail_on` repo ids raise, mimicking a 403."""

    def __init__(self, fail_on=(), **_k):
        self.deleted = []
        self.fail_on = set(fail_on)

    def delete_repo(self, repo_id, repo_type, missing_ok=False):
        if repo_id in self.fail_on:
            raise RuntimeError(
                f"403 Forbidden: you do not have permission on {repo_id}")
        self.deleted.append((repo_id, repo_type))


class ErasureEnumeratesRecordingsNotJustTrainings(unittest.TestCase):

    def setUp(self):
        _ensure_stubs()
        from app.routes import teacher
        self.teacher = teacher

    def _run(self, store, fail_on=()):
        api = _Api(fail_on=fail_on)
        sb = _Supabase(store)
        with patch.dict('os.environ', {'HF_TOKEN': 'hf_platform'}), \
                patch.object(self.teacher, 'get_supabase', lambda: sb), \
                patch.object(self.teacher, 'HfApi', lambda **k: api):
            failures = self.teacher._delete_student_hf_artifacts('stu1')
        return api, failures

    def test_a_recording_never_used_for_training_is_now_found(self):
        """THE defect: no trainings row, so the old code looked at nothing."""
        store = {
            'trainings': [],
            'datasets': [{'hf_repo_id': 'alice/omx_f_pick',
                          'owner_user_id': 'stu1', 'workgroup_id': None}],
        }
        api, failures = self._run(store)
        self.assertIn(
            ('alice/omx_f_pick', 'dataset'), api.deleted,
            'a plain recording is still invisible to erasure')
        self.assertEqual(failures, [])

    def test_training_derived_repos_are_still_erased(self):
        store = {
            'trainings': [{'dataset_name': 'alice/ds', 'model_name': 'org/mdl',
                           'workgroup_id': None}],
            'datasets': [],
        }
        api, failures = self._run(store)
        self.assertIn(('alice/ds', 'dataset'), api.deleted)
        self.assertIn(('org/mdl', 'model'), api.deleted)
        self.assertEqual(failures, [])

    def test_workgroup_shared_datasets_are_still_skipped(self):
        """Siblings still depend on them — the migration-011 rule stands."""
        store = {
            'trainings': [],
            'datasets': [{'hf_repo_id': 'alice/shared',
                          'owner_user_id': 'stu1', 'workgroup_id': 'wg1'}],
        }
        api, failures = self._run(store)
        self.assertEqual(api.deleted, [])
        self.assertEqual(failures, [])

    def test_a_repo_is_not_deleted_twice(self):
        store = {
            'trainings': [{'dataset_name': 'alice/ds', 'model_name': None,
                           'workgroup_id': None}],
            'datasets': [{'hf_repo_id': 'alice/ds',
                          'owner_user_id': 'stu1', 'workgroup_id': None}],
        }
        api, _ = self._run(store)
        self.assertEqual(api.deleted.count(('alice/ds', 'dataset')), 1)


class AlreadyGoneIsSuccessNotFailure(unittest.TestCase):
    """`RepositoryNotFoundError` is the DESIRED END STATE, not an error.

    D6 — this distinction was written into the code and fenced by nothing:
    collapsing the `except RepositoryNotFoundError: continue` into the generic
    `except Exception` handler below it left the whole suite green, while
    turning every already-erased repo into a reported failure. A teacher
    exercising the SAME Art. 17 request twice would then be told, in German,
    that data still sits on HuggingFace when it does not — the exact
    dishonesty this file exists to prevent, only inverted.

    It is a REAL possibility, not a hypothetical: `delete_repo` is called with
    `missing_ok=True`, `trainings` and `datasets` can name the same repo, and a
    teacher can re-run a deletion.
    """

    def setUp(self):
        _ensure_stubs()
        from app.routes import teacher
        self.teacher = teacher
        from huggingface_hub.utils import RepositoryNotFoundError
        self.NotFound = RepositoryNotFoundError

    def _run(self, store, missing=()):
        outer = self

        class _MissingApi(_Api):
            def delete_repo(self, repo_id, repo_type, missing_ok=False):
                if repo_id in missing:
                    raise outer.NotFound(f'404 Repo not found: {repo_id}')
                return super().delete_repo(repo_id, repo_type, missing_ok)

        api = _MissingApi()
        sb = _Supabase(store)
        with patch.dict('os.environ', {'HF_TOKEN': 'hf_platform'}), \
                patch.object(self.teacher, 'get_supabase', lambda: sb), \
                patch.object(self.teacher, 'HfApi', lambda **k: api):
            failures = self.teacher._delete_student_hf_artifacts('stu1')
        return api, failures

    def test_an_already_deleted_repo_is_not_reported_as_a_failure(self):
        store = {
            'trainings': [],
            'datasets': [{'hf_repo_id': 'alice/gone',
                          'owner_user_id': 'stu1', 'workgroup_id': None}],
        }
        api, failures = self._run(store, missing={'alice/gone'})
        self.assertEqual(
            failures, [],
            'a repo that is already gone was reported as NOT erased, so the '
            'teacher is told data remains that does not')
        self.assertEqual(api.deleted, [])

    def test_a_real_403_beside_it_is_STILL_reported(self):
        """Not vacuous: the two outcomes must stay distinguishable."""
        store = {
            'trainings': [],
            'datasets': [
                {'hf_repo_id': 'alice/gone',
                 'owner_user_id': 'stu1', 'workgroup_id': None},
                {'hf_repo_id': 'bob/forbidden',
                 'owner_user_id': 'stu1', 'workgroup_id': None},
            ],
        }
        outer = self

        class _MixedApi(_Api):
            def delete_repo(self, repo_id, repo_type, missing_ok=False):
                if repo_id == 'alice/gone':
                    raise outer.NotFound('404')
                if repo_id == 'bob/forbidden':
                    raise RuntimeError('403 Forbidden')
                self.deleted.append((repo_id, repo_type))

        api = _MixedApi()
        sb = _Supabase(store)
        with patch.dict('os.environ', {'HF_TOKEN': 'hf_platform'}), \
                patch.object(self.teacher, 'get_supabase', lambda: sb), \
                patch.object(self.teacher, 'HfApi', lambda **k: api):
            failures = self.teacher._delete_student_hf_artifacts('stu1')
        self.assertEqual([f['repo_id'] for f in failures], ['bob/forbidden'])

    def test_a_not_found_does_not_abort_the_repos_after_it(self):
        store = {
            'trainings': [],
            'datasets': [
                {'hf_repo_id': 'alice/gone',
                 'owner_user_id': 'stu1', 'workgroup_id': None},
                {'hf_repo_id': 'alice/present',
                 'owner_user_id': 'stu1', 'workgroup_id': None},
            ],
        }
        api, failures = self._run(store, missing={'alice/gone'})
        self.assertIn(('alice/present', 'dataset'), api.deleted)
        self.assertEqual(failures, [])


class ARegistryQueryFailureIsReportedNotSwallowed(unittest.TestCase):
    """D6 — the second unfenced behaviour.

    The `datasets` registry is now BOTH the shared-repo skip list and a source
    of repos to erase. If that query fails, two things are true at once and
    only one of them is obvious: we cannot enumerate the student's recordings,
    AND we cannot tell which repos are workgroup-shared. So the failure has to
    reach the teacher; deleting the `failures.append(...)` in that `except`
    left the whole suite green while the endpoint answered
    `hf_erasure_complete: true` on a run that had enumerated nothing.
    """

    def setUp(self):
        _ensure_stubs()
        from app.routes import teacher
        self.teacher = teacher

    def _run_with_broken_registry(self, store):
        class _BrokenSupabase(_Supabase):
            def table(self, name):
                if name == 'datasets':
                    raise RuntimeError('PostgREST 503')
                return super().table(name)

        api = _Api()
        sb = _BrokenSupabase(store)
        with patch.dict('os.environ', {'HF_TOKEN': 'hf_platform'}), \
                patch.object(self.teacher, 'get_supabase', lambda: sb), \
                patch.object(self.teacher, 'HfApi', lambda **k: api):
            return api, self.teacher._delete_student_hf_artifacts('stu1')

    def test_the_failure_is_reported(self):
        store = {'trainings': [{'dataset_name': 'alice/ds', 'model_name': None,
                                'workgroup_id': None}]}
        _, failures = self._run_with_broken_registry(store)
        self.assertTrue(
            failures,
            'the registry query failed and nothing was reported — the teacher '
            'is told the erasure completed on a run that enumerated nothing')
        self.assertEqual(failures[0]['repo_type'], 'dataset')

    def test_the_reason_is_German(self):
        store = {'trainings': []}
        _, failures = self._run_with_broken_registry(store)
        self.assertIn('Datensatz-Register', failures[0]['reason'])

    def test_it_still_erases_what_the_trainings_table_DID_name(self):
        """Best-effort: a broken registry must not abort the whole pass."""
        store = {'trainings': [{'dataset_name': 'alice/ds',
                                'model_name': 'org/mdl',
                                'workgroup_id': None}]}
        api, _ = self._run_with_broken_registry(store)
        self.assertIn(('alice/ds', 'dataset'), api.deleted)
        self.assertIn(('org/mdl', 'model'), api.deleted)

    def test_it_reports_even_when_there_is_nothing_else_to_erase(self):
        """The early `if not rows and not registry_repos: return failures`.

        Returning a BARE `[]` there would drop the registry failure on exactly
        the run where it is the only thing there is to say.
        """
        _, failures = self._run_with_broken_registry({'trainings': []})
        self.assertTrue(failures)


class ErasureFailuresAreReportedNotSwallowed(unittest.TestCase):

    def setUp(self):
        _ensure_stubs()
        from app.routes import teacher
        self.teacher = teacher

    def _run(self, store, fail_on=()):
        api = _Api(fail_on=fail_on)
        sb = _Supabase(store)
        with patch.dict('os.environ', {'HF_TOKEN': 'hf_platform'}), \
                patch.object(self.teacher, 'get_supabase', lambda: sb), \
                patch.object(self.teacher, 'HfApi', lambda **k: api):
            failures = self.teacher._delete_student_hf_artifacts('stu1')
        return api, failures

    def test_a_403_on_a_student_namespace_is_REPORTED(self):
        """The realistic case: the platform token does not own alice/*."""
        store = {
            'trainings': [],
            'datasets': [{'hf_repo_id': 'alice/omx_f_pick',
                          'owner_user_id': 'stu1', 'workgroup_id': None}],
        }
        _, failures = self._run(store, fail_on={'alice/omx_f_pick'})
        self.assertEqual(len(failures), 1, 'the 403 was swallowed again')
        self.assertEqual(failures[0]['repo_id'], 'alice/omx_f_pick')
        self.assertIn('403', failures[0]['reason'])

    def test_a_missing_HF_TOKEN_is_reported_rather_than_a_silent_noop(self):
        with patch.dict('os.environ', {'HF_TOKEN': ''}):
            failures = self.teacher._delete_student_hf_artifacts('stu1')
        self.assertTrue(failures, 'no token used to mean a silent no-op')

    def test_one_failure_does_not_abort_the_others(self):
        store = {
            'trainings': [],
            'datasets': [
                {'hf_repo_id': 'alice/a', 'owner_user_id': 'stu1', 'workgroup_id': None},
                {'hf_repo_id': 'alice/b', 'owner_user_id': 'stu1', 'workgroup_id': None},
            ],
        }
        api, failures = self._run(store, fail_on={'alice/a'})
        self.assertIn(('alice/b', 'dataset'), api.deleted)
        self.assertEqual([f['repo_id'] for f in failures], ['alice/a'])

    def test_nothing_to_erase_is_an_empty_failure_list(self):
        api, failures = self._run({'trainings': [], 'datasets': []})
        self.assertEqual(api.deleted, [])
        self.assertEqual(failures, [])


class DeleteStudentTellsTheTeacherTheTruth(unittest.TestCase):
    """`{"ok": true}` alone let a teacher believe data was erased."""

    def setUp(self):
        _ensure_stubs()
        from app.routes import teacher
        self.teacher = teacher

    def _delete(self, failures):
        import asyncio
        sb = _Supabase({})
        with patch.object(self.teacher, '_assert_student_owned', lambda *a: {}), \
                patch.object(self.teacher, '_delete_student_hf_artifacts',
                             lambda _sid: failures), \
                patch.object(self.teacher, 'get_supabase', lambda: sb):
            return asyncio.run(
                self.teacher.delete_student('stu1', teacher={'id': 't1'})), sb

    def test_a_clean_erasure_says_so_explicitly(self):
        res, sb = self._delete([])
        self.assertTrue(res['ok'])
        self.assertIs(res['hf_erasure_complete'], True)
        self.assertEqual(sb.deleted_user, 'stu1')

    def test_a_failed_erasure_is_NOT_reported_as_complete(self):
        failures = [{'repo_id': 'alice/x', 'repo_type': 'dataset',
                     'reason': '403 Forbidden'}]
        res, sb = self._delete(failures)
        self.assertIs(
            res['hf_erasure_complete'], False,
            'the endpoint still claims a complete erasure')
        self.assertEqual(res['hf_failures'], failures)
        # The ACCOUNT deletion must still have happened — erasure being
        # best-effort is deliberate; an HF outage must not leave a dangling
        # auth user.
        self.assertTrue(res['ok'])
        self.assertEqual(sb.deleted_user, 'stu1')

    def test_the_failure_detail_is_german_with_literal_umlauts(self):
        res, _ = self._delete([{'repo_id': 'alice/x', 'repo_type': 'dataset',
                                'reason': '403'}])
        detail = res['detail']
        self.assertIn('gelöscht', detail)
        self.assertNotIn('geloescht', detail)
        # It must say the data is STILL THERE, not merely that something failed.
        self.assertIn('NICHT', detail)


class SelfServiceDeletionDoesNotOverstateWhatHappened(unittest.TestCase):
    """`/me/delete` deletes nothing and nothing drains its queue."""

    def setUp(self):
        _ensure_stubs()
        from app.routes import me
        self.me = me

    def _message_text(self):
        """The `message` VALUE, extracted by AST.

        Scanning raw source would also read the comments — and the comment
        beside this string legitimately QUOTES the old English wording to
        explain why it was replaced, which would make a naive substring
        assertion pass or fail for the wrong reason.
        """
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(self.me.delete_my_account).lstrip())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == 'message':
                    # Concatenated literals + one f-string piece.
                    return ''.join(
                        n.value for n in ast.walk(value)
                        if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    )
        self.fail('no `message` key found in delete_my_account')

    def test_the_message_is_german(self):
        text = self._message_text()
        self.assertIn('Löschanfrage', text)
        # Rule §1 — it used to be English on a student-facing GDPR endpoint.
        self.assertNotIn('Your deletion request was recorded', text)
        self.assertNotIn('geloescht', text)  # literal umlauts only

    def test_it_states_plainly_that_nothing_was_deleted_yet(self):
        self.assertIn('KEINE Daten gelöscht', self._message_text())

    def test_it_does_not_promise_an_automatic_30_day_process(self):
        """Nothing drains the queue — no sweeper, no cron, no admin surface."""
        text = self._message_text()
        self.assertNotIn('within 30 days', text)
        self.assertNotIn('30 Tagen', text)

    def test_it_names_what_DID_take_effect(self):
        # Honest must not mean useless: the two real side effects are named.
        text = self._message_text()
        self.assertIn('abgebrochen', text)
        self.assertIn('Arbeitsgruppen-Zuordnung', text)

    def test_the_response_carries_an_explicit_deletion_performed_flag(self):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(self.me.delete_my_account).lstrip())
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (isinstance(key, ast.Constant)
                            and key.value == 'deletion_performed'):
                        self.assertIs(value.value, False)
                        found = True
        self.assertTrue(found, 'no deletion_performed flag in the response')


if __name__ == '__main__':
    unittest.main()

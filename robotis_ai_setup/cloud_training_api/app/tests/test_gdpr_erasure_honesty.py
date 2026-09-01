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
from pathlib import Path
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
            failures, _attempted = self.teacher._delete_student_hf_artifacts('stu1')
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
            failures, _attempted = self.teacher._delete_student_hf_artifacts('stu1')
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
            failures, _attempted = self.teacher._delete_student_hf_artifacts('stu1')
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
            failures, _attempted = self.teacher._delete_student_hf_artifacts('stu1')
        return api, failures

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

    def test_a_broken_registry_never_deletes_a_DATASET(self):
        """The measured P0: a 503 turned "skip shared repos" into "delete them".

        This test REPLACES `test_it_still_erases_what_the_trainings_table_DID_name`,
        which asserted the opposite and therefore pinned the defect open. That
        test read as a best-effort virtue ("a broken registry must not abort the
        whole pass") while the registry is the ONLY source of the shared-repo
        skip list — so with it unreadable, `shared_dataset_repos` is empty and
        every dataset named by a surviving `trainings` row looks unshared.

        Measured 2026-08-08 against the real route: with the datasets query
        answering 503, a workgroup-shared repo was observably passed to
        `delete_repo`. The sibling group's data is gone and unrecoverable, so
        this half must fail CLOSED.
        """
        store = {'trainings': [{'dataset_name': 'group/shared-ds',
                                'model_name': 'org/mdl',
                                'workgroup_id': None}]}
        api, _ = self._run_with_broken_registry(store)
        self.assertNotIn(
            ('group/shared-ds', 'dataset'), api.deleted,
            'a dataset was deleted while the shared-repo skip list was '
            'unavailable — this is the workgroup-data-destruction path')

    def test_a_broken_registry_still_erases_MODELS(self):
        """Fail-closed is scoped to datasets, deliberately.

        A pooled training is already skipped by its own row's `workgroup_id`,
        which does not come from the failed query — so model erasure loses no
        safety here, and widening the refusal would stop erasing data we can
        prove is unshared.
        """
        store = {'trainings': [{'dataset_name': 'group/shared-ds',
                                'model_name': 'org/mdl',
                                'workgroup_id': None}]}
        api, _ = self._run_with_broken_registry(store)
        self.assertIn(('org/mdl', 'model'), api.deleted)

    def test_every_undeleted_dataset_is_NAMED_to_the_teacher(self):
        """The generic '?' entry alone makes the data loss invisible.

        The teacher is warned either way, but a warning that never mentions the
        datasets reads as "a registry hiccup" rather than "these recordings were
        not erased" — so each refused repo gets its own entry.
        """
        store = {'trainings': [{'dataset_name': 'group/shared-ds',
                                'model_name': None,
                                'workgroup_id': None}]}
        _, failures = self._run_with_broken_registry(store)
        named = [f['repo_id'] for f in failures]
        self.assertIn('group/shared-ds', named)

    def test_the_registry_failure_reason_does_not_leak_the_raw_exception(self):
        """`f'...: {e}'` put raw English PostgREST text in a German field."""
        _, failures = self._run_with_broken_registry({'trainings': []})
        generic = [f for f in failures if f['repo_id'] == '?']
        self.assertTrue(generic)
        self.assertNotIn('PostgREST', generic[0]['reason'])
        self.assertNotIn('503', generic[0]['reason'])

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
            failures, _attempted = self.teacher._delete_student_hf_artifacts('stu1')
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
            failures, _attempted = self.teacher._delete_student_hf_artifacts('stu1')
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

    def _delete(self, failures, attempted=1):
        """`attempted` defaults to 1 = "HuggingFace WAS contacted".

        It is a parameter because the erasure helper now returns
        `(failures, attempted)` and the two carry different claims: an empty
        failure list after ZERO attempts is not a completed erasure. Tests that
        are about the failure wording pass the default; the honesty of the
        zero-attempt case has its own class below.
        """
        import asyncio
        sb = _Supabase({})
        with patch.object(self.teacher, '_assert_student_owned', lambda *a: {}), \
                patch.object(self.teacher, '_delete_student_hf_artifacts',
                             lambda _sid, _enum=None: (failures, attempted)), \
                patch.object(self.teacher, 'get_supabase', lambda: sb):
            return asyncio.run(
                self.teacher.delete_student('stu1', teacher={'id': 't1'})), sb

    def test_a_clean_erasure_says_so_explicitly(self):
        """Only when something was actually erased — see `attempted=1`.

        Before 2026-08-31 this passed with NOTHING attempted, which is how the
        endpoint came to answer `hf_erasure_complete: true` on a run that never
        called HuggingFace at all.
        """
        res, sb = self._delete([], attempted=1)
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


class TheDestructiveEndpointIsOwnershipGated(unittest.TestCase):
    """Rule §4, on the single most destructive route in the API.

    Service-role bypasses RLS, so `_assert_student_owned` is the ONLY thing
    standing between teacher A and teacher B's student. It was live in
    production the whole time — but fenced by nothing: measured 2026-08-08,
    DELETING the assert and MOVING it after the destructive calls each left all
    253 tests green. Every other test in this file patches the assert out, so
    they are structurally incapable of noticing.

    Both properties are asserted, because they fail differently: an absent
    assert is a cross-tenant IDOR, and a late one still erases HuggingFace repos
    and deletes the auth user before raising 404.
    """

    def setUp(self):
        _ensure_stubs()
        from app.routes import teacher
        self.teacher = teacher

    def test_a_foreign_student_is_refused_before_anything_is_destroyed(self):
        import asyncio
        touched = []

        def _refuse(_tid, _sid):
            raise self.teacher.HTTPException(status_code=404,
                                             detail='Schüler nicht gefunden')

        sb = _Supabase({})
        with patch.object(self.teacher, '_assert_student_owned', _refuse), \
                patch.object(self.teacher, '_delete_student_hf_artifacts',
                             lambda sid, _enum=None: (touched.append(sid) or [], 0)), \
                patch.object(self.teacher, '_enumerate_student_hf_artifacts',
                             lambda sid: (touched.append(sid) or [], True)), \
                patch.object(self.teacher, 'get_supabase', lambda: sb):
            with self.assertRaises(self.teacher.HTTPException) as caught:
                asyncio.run(self.teacher.delete_student(
                    'not-mine', teacher={'id': 't1'}))
        self.assertEqual(caught.exception.status_code, 404)
        # Enumeration is tracked too, not just erasure: it reads the `datasets`
        # and `trainings` rows of the named student, so running it above the
        # ownership assert is a cross-tenant READ even though it destroys
        # nothing.
        self.assertEqual(
            touched, [],
            'HF enumeration/erasure ran for a student this teacher does not own')
        self.assertIsNone(
            sb.deleted_user,
            'the auth user was deleted for a student this teacher does not own')

    def test_the_assert_is_the_FIRST_statement_of_the_handler(self):
        """Ordering, read structurally.

        The behavioural test above catches a DELETED assert. It cannot catch a
        MISPLACED one on its own — a raising assert short-circuits either way —
        so the position is pinned too. Docstring stripped: `ast.unparse` keeps
        it and it names the symbol being asserted on.
        """
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(self.teacher.delete_student).lstrip())
        fn = tree.body[0]
        body = list(fn.body)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)):
            body = body[1:]
        self.assertTrue(body, 'delete_student has an empty body — test is stale')
        first = ast.unparse(body[0])
        self.assertIn(
            '_assert_student_owned', first,
            f'the ownership assert is no longer the first statement of '
            f'delete_student (found {first!r}) — anything above it runs for a '
            f'student the caller may not own')



class TheRouteIsWiredEndToEnd(unittest.TestCase):
    """Route-level harness: the REAL enumerate + erase helpers, stubbed edges.

    Every other `delete_student` test in this file patches the erasure helper
    out, which is what let the ORDER of the handler's steps go unfenced for so
    long. These drive `delete_student` with only `get_supabase` and `HfApi`
    replaced, so enumeration, the 503 gate, the account delete and the HF loop
    all really run, in the order the handler puts them in.
    """

    def setUp(self):
        _ensure_stubs()
        from app.routes import teacher
        self.teacher = teacher

    def _route(self, store, broken_registry=False, delete_user_raises=False,
               fail_on=(), token='hf_platform'):
        import asyncio

        class _Sb(_Supabase):
            def table(self, name):
                if broken_registry and name == 'datasets':
                    raise RuntimeError('PostgREST 503')
                return super().table(name)

            def _delete_user(self, uid):
                if delete_user_raises:
                    # The measured production shape: migration 038 exists
                    # because `trainings.user_id` had no ON DELETE clause, so
                    # Postgres raised 23503 here for any student who had ever
                    # trained.
                    raise RuntimeError(
                        'foreign key violation 23503 trainings_user_id_fkey')
                return super()._delete_user(uid)

        api = _Api(fail_on=fail_on)
        sb = _Sb(store)
        with patch.dict('os.environ', {'HF_TOKEN': token}), \
                patch.object(self.teacher, '_assert_student_owned', lambda *a: {}), \
                patch.object(self.teacher, 'get_supabase', lambda: sb), \
                patch.object(self.teacher, 'HfApi', lambda **k: api):
            try:
                res = asyncio.run(
                    self.teacher.delete_student('stu1', teacher={'id': 't1'}))
            except self.teacher.HTTPException as exc:
                return exc, sb, api
        return res, sb, api

    # ---- the registry outage is now RETRYABLE ---------------------------

    def test_a_registry_outage_refuses_with_503_and_deletes_nothing(self):
        """The unfinishable-request path.

        Before 2026-08-31 an unreadable `datasets` registry deleted the account
        anyway. `public.users` cascades, the `datasets` rows go with it, and the
        retry then hits `_assert_student_owned` -> 404: a lawful Art. 17 request
        that met a registry outage could NEVER be completed. Refusing while the
        student is still there is the whole fix, so all three properties are
        asserted together.
        """
        store = {'trainings': [{'dataset_name': 'group/shared-ds',
                                'model_name': 'org/mdl',
                                'workgroup_id': None}]}
        res, sb, api = self._route(store, broken_registry=True)
        self.assertIsInstance(res, self.teacher.HTTPException)
        self.assertEqual(res.status_code, 503)
        self.assertIsNone(
            sb.deleted_user,
            'the account was deleted during a registry outage — the retry '
            'now 404s and the erasure can never be completed')
        self.assertEqual(
            api.deleted, [],
            'HuggingFace repos were erased during a registry outage')

    def test_the_503_detail_is_german_and_tells_the_teacher_to_retry(self):
        res, _, _ = self._route({'trainings': []}, broken_registry=True)
        detail = res.detail
        self.assertIn('gelöscht', detail)
        self.assertNotIn('geloescht', detail)
        # It must say BOTH halves: nothing happened, and trying again helps.
        self.assertIn('nichts', detail)
        self.assertIn('erneut', detail)
        # Rule §6 — no repo id / path / raw driver text echoed back.
        self.assertNotIn('PostgREST', detail)
        self.assertNotIn('stu1', detail)

    # ---- nothing irreversible happens before the reversible step --------

    def test_an_account_delete_failure_erases_no_huggingface_data(self):
        """THE ordering defect that migration 038 made routine.

        HF erasure used to run FIRST. With `trainings.user_id` at NO ACTION,
        every student who had ever trained hit 23503 on the account delete —
        AFTER their datasets were already gone from the Hub. The teacher was
        told only „Konto konnte nicht gelöscht werden", and no retry could
        succeed. Erasure must now be unreachable unless the account delete won.
        """
        store = {
            'trainings': [],
            'datasets': [{'hf_repo_id': 'alice/omx_f_pick',
                          'owner_user_id': 'stu1', 'workgroup_id': None}],
        }
        res, sb, api = self._route(store, delete_user_raises=True)
        self.assertIsInstance(res, self.teacher.HTTPException)
        self.assertEqual(res.status_code, 500)
        self.assertEqual(
            api.deleted, [],
            'the student\'s HuggingFace data was erased on a request that then '
            'failed to delete the account — irreversible loss on a failed call')

    def test_the_account_delete_failure_message_is_unchanged_german(self):
        res, _, _ = self._route({'trainings': []}, delete_user_raises=True)
        self.assertEqual(res.detail, 'Konto konnte nicht gelöscht werden')

    # ---- zero attempts is not a completed erasure ------------------------

    def test_a_student_with_nothing_registered_is_NOT_told_erasure_completed(self):
        """The one place this pass had REGRESSED honesty.

        `{"ok": true, "hf_erasure_complete": true}` after contacting
        HuggingFace zero times is a positive claim about a service that was
        never called — and a recording pushed under a name that never reached
        the `datasets` registry is invisible here, so the claim is not even
        conservative.
        """
        res, sb, api = self._route({'trainings': [], 'datasets': []})
        self.assertEqual(sb.deleted_user, 'stu1')
        self.assertEqual(api.deleted, [])
        self.assertTrue(res['ok'])
        self.assertIs(res['hf_erasure_complete'], False)
        self.assertEqual(res['hf_failures'], [])

    def test_that_detail_is_german_and_says_what_was_and_was_not_established(self):
        res, _, _ = self._route({'trainings': [], 'datasets': []})
        detail = res['detail']
        self.assertIn('gelöscht', detail)          # the account WAS
        self.assertIn('nicht', detail)             # HF was NOT contacted
        self.assertIn('HuggingFace', detail)
        for bad in ('geloescht', 'ueberprueft', 'geprueft', 'Schueler'):
            self.assertNotIn(bad, detail)

    def test_hf_failures_is_present_and_empty_so_the_SPA_renders_sensibly(self):
        """`StudentRow.handleDelete` reads `hf_failures` only for `.length`.

        The field must exist in EVERY `hf_erasure_complete: false` branch, or
        the SPA's `Array.isArray(res.hf_failures) ? ... : 0` fallback silently
        becomes the load-bearing path.
        """
        res, _, _ = self._route({'trainings': [], 'datasets': []})
        self.assertIn('hf_failures', res)
        self.assertIsInstance(res['hf_failures'], list)

    def test_a_workgroup_only_student_is_also_not_told_erasure_completed(self):
        """Nothing DELETABLE was found either — same honest answer.

        The one shared dataset is deliberately kept for the surviving group
        members, so HuggingFace is never contacted and the wording („keine …
        die hier gelöscht werden können") has to be true for this case too.
        """
        store = {
            'trainings': [],
            'datasets': [{'hf_repo_id': 'alice/shared',
                          'owner_user_id': 'stu1', 'workgroup_id': 'wg1'}],
        }
        res, sb, api = self._route(store)
        self.assertEqual(sb.deleted_user, 'stu1')
        self.assertEqual(api.deleted, [])
        self.assertIs(res['hf_erasure_complete'], False)

    # ---- the ordinary flows are unchanged --------------------------------

    def test_a_real_erasure_still_reports_complete(self):
        store = {
            'trainings': [],
            'datasets': [{'hf_repo_id': 'alice/omx_f_pick',
                          'owner_user_id': 'stu1', 'workgroup_id': None}],
        }
        res, sb, api = self._route(store)
        self.assertEqual(api.deleted, [('alice/omx_f_pick', 'dataset')])
        self.assertEqual(sb.deleted_user, 'stu1')
        self.assertIs(res['hf_erasure_complete'], True)
        self.assertNotIn('hf_failures', res)

    def test_a_403_still_reports_the_partial_outcome(self):
        store = {
            'trainings': [],
            'datasets': [{'hf_repo_id': 'alice/omx_f_pick',
                          'owner_user_id': 'stu1', 'workgroup_id': None}],
        }
        res, sb, _ = self._route(store, fail_on={'alice/omx_f_pick'})
        self.assertEqual(sb.deleted_user, 'stu1')
        self.assertIs(res['hf_erasure_complete'], False)
        self.assertEqual([f['repo_id'] for f in res['hf_failures']],
                         ['alice/omx_f_pick'])

    def test_the_enumeration_is_not_run_twice(self):
        """One read, handed down — not a second query after the cascade.

        The handler enumerates BEFORE `auth.admin.delete_user`, which cascades
        `public.users` and takes the `trainings` + `datasets` rows with it. A
        second enumeration inside the erasure helper would therefore run
        against rows that no longer exist and quietly erase nothing.
        """
        store = {
            'trainings': [],
            'datasets': [{'hf_repo_id': 'alice/omx_f_pick',
                          'owner_user_id': 'stu1', 'workgroup_id': None}],
        }
        reads = []

        class _CountingSb(_Supabase):
            def table(self, name):
                reads.append(name)
                return super().table(name)

        import asyncio
        api = _Api()
        sb = _CountingSb(store)
        with patch.dict('os.environ', {'HF_TOKEN': 'hf_platform'}), \
                patch.object(self.teacher, '_assert_student_owned', lambda *a: {}), \
                patch.object(self.teacher, 'get_supabase', lambda: sb), \
                patch.object(self.teacher, 'HfApi', lambda **k: api):
            asyncio.run(self.teacher.delete_student('stu1', teacher={'id': 't1'}))
        self.assertEqual(reads.count('datasets'), 1, reads)
        self.assertEqual(reads.count('trainings'), 1, reads)
        self.assertEqual(api.deleted, [('alice/omx_f_pick', 'dataset')])


class AttemptedCountsWhatHuggingFaceWasActuallyAsked(unittest.TestCase):
    """The new second return value, fenced on its own.

    `attempted` is the only thing separating „nothing failed" from „erasure
    complete", so a version of it that is always 0 or always len(candidates)
    would re-open the exact overstatement. Both edges are pinned.
    """

    def setUp(self):
        _ensure_stubs()
        from app.routes import teacher
        self.teacher = teacher

    def _run(self, store, fail_on=(), broken_registry=False):
        class _Sb(_Supabase):
            def table(self, name):
                if broken_registry and name == 'datasets':
                    raise RuntimeError('PostgREST 503')
                return super().table(name)

        api = _Api(fail_on=fail_on)
        sb = _Sb(store)
        with patch.dict('os.environ', {'HF_TOKEN': 'hf_platform'}), \
                patch.object(self.teacher, 'get_supabase', lambda: sb), \
                patch.object(self.teacher, 'HfApi', lambda **k: api):
            return self.teacher._delete_student_hf_artifacts('stu1')

    def test_nothing_registered_means_zero_attempts(self):
        _, attempted = self._run({'trainings': [], 'datasets': []})
        self.assertEqual(attempted, 0)

    def test_a_deleted_repo_counts_as_one_attempt(self):
        store = {'trainings': [],
                 'datasets': [{'hf_repo_id': 'alice/a',
                               'owner_user_id': 'stu1', 'workgroup_id': None}]}
        _, attempted = self._run(store)
        self.assertEqual(attempted, 1)

    def test_a_403_still_counts_as_an_attempt(self):
        """We DID ask HuggingFace — it said no. That is a different claim from
        never asking, and the failure list already carries the refusal."""
        store = {'trainings': [],
                 'datasets': [{'hf_repo_id': 'alice/a',
                               'owner_user_id': 'stu1', 'workgroup_id': None}]}
        failures, attempted = self._run(store, fail_on={'alice/a'})
        self.assertEqual(attempted, 1)
        self.assertEqual(len(failures), 1)

    def test_a_missing_token_is_zero_attempts(self):
        with patch.dict('os.environ', {'HF_TOKEN': ''}):
            failures, attempted = self.teacher._delete_student_hf_artifacts('stu1')
        self.assertEqual(attempted, 0)
        self.assertTrue(failures)

    def test_a_fail_closed_dataset_is_NOT_counted_as_attempted(self):
        """It was refused locally; HuggingFace never heard about it."""
        store = {'trainings': [{'dataset_name': 'group/shared-ds',
                                'model_name': 'org/mdl',
                                'workgroup_id': None}]}
        _, attempted = self._run(store, broken_registry=True)
        self.assertEqual(attempted, 1, 'only the model should have been tried')

    def test_a_workgroup_shared_dataset_is_NOT_counted_as_attempted(self):
        store = {'trainings': [],
                 'datasets': [{'hf_repo_id': 'alice/shared',
                               'owner_user_id': 'stu1', 'workgroup_id': 'wg1'}]}
        _, attempted = self._run(store)
        self.assertEqual(attempted, 0)

    def test_the_caller_hands_its_enumeration_down_unchanged(self):
        """The `enumeration` parameter is what makes enumerate-then-delete
        possible; a version that ignored it would silently re-query after the
        account (and its rows) were gone."""
        api = _Api()
        with patch.dict('os.environ', {'HF_TOKEN': 'hf_platform'}), \
                patch.object(self.teacher, 'HfApi', lambda **k: api), \
                patch.object(self.teacher, 'get_supabase',
                             lambda: self.fail('the enumeration was re-queried')):
            failures, attempted = self.teacher._delete_student_hf_artifacts(
                'stu1', ([('alice/handed-in', 'dataset')], True))
        self.assertEqual(api.deleted, [('alice/handed-in', 'dataset')])
        self.assertEqual((failures, attempted), ([], 1))


class TheTrainingsForeignKeyNoLongerBlocksAnErasure(unittest.TestCase):
    """Migration 038, fenced as a file-level invariant.

    `public.trainings.user_id` was the ONLY foreign key to `public.users` in
    the schema with no ON DELETE clause (baseline.sql:62), so Postgres defaulted
    it to NO ACTION and a student who had ever started one training could not be
    deleted AT ALL: `auth.admin.delete_user` cascaded `auth.users` ->
    `public.users` and then raised 23503.

    Verified against a real `postgres:16-alpine` on 2026-08-31: constraint name
    `trainings_user_id_fkey`, `confdeltype='a'` before, `'c'` after, DELETE
    blocked before and cascading after. That verification cannot live in this
    suite (no database), so what IS pinned here is that the migration and its
    rollback twin exist and say the right thing — deleting either file turns
    this red.
    """

    MIGRATIONS = Path(__file__).resolve().parents[3] / 'supabase' / 'migrations'
    ROLLBACK = Path(__file__).resolve().parents[3] / 'supabase' / 'rollback'
    CONSTRAINT = 'trainings_user_id_fkey'

    def _forward_files(self):
        return [p for p in sorted(self.MIGRATIONS.glob('*.sql'))
                if p.name != '00000000000000_baseline.sql'
                and self.CONSTRAINT in p.read_text(encoding='utf-8')]

    def test_a_migration_gives_the_constraint_ON_DELETE_CASCADE(self):
        files = self._forward_files()
        self.assertEqual(
            len(files), 1,
            f'expected exactly one migration touching {self.CONSTRAINT}, '
            f'found {[p.name for p in files]}')
        text = files[0].read_text(encoding='utf-8')
        self.assertIn(f'DROP CONSTRAINT {self.CONSTRAINT}', text)
        self.assertRegex(
            text,
            r'ADD CONSTRAINT trainings_user_id_fkey\s+FOREIGN KEY \(user_id\)\s+'
            r'REFERENCES public\.users\(id\) ON DELETE CASCADE')

    def test_the_baseline_is_left_alone(self):
        """The delta is a migration, not an edit to already-applied SQL.

        `baseline.sql` describes what production ALREADY has. "Fixing" the FK
        there would make the repo disagree with the live schema and the new
        constraint would never actually be applied anywhere.
        """
        baseline = (self.MIGRATIONS / '00000000000000_baseline.sql').read_text(
            encoding='utf-8')
        self.assertIn('user_id UUID NOT NULL REFERENCES public.users(id),',
                      baseline)

    def test_the_migration_has_a_rollback_twin(self):
        stem = self._forward_files()[0].stem
        twin = self.ROLLBACK / f'{stem}_rollback.sql'
        self.assertTrue(twin.is_file(), f'no rollback twin at {twin}')
        text = twin.read_text(encoding='utf-8')
        self.assertIn(f'DROP CONSTRAINT {self.CONSTRAINT}', text)
        # It must restore the NO ACTION spelling, i.e. NOT re-add the cascade.
        self.assertNotIn('ON DELETE CASCADE', text)


if __name__ == '__main__':
    unittest.main()

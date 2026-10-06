#!/usr/bin/env python3
"""Daten 2.0, D14 — the recording Start against the hub (spec §C4, §J.A step 9).

Every verdict of ``DataManager._check_dataset_exists`` with a fake hub (the call
shapes hub_sync uses: whoami, list_repo_refs, list_repo_tree top-level and
recursive, repo_exists; folders content-addressed, an LFS entry's blob id the git
sha1 of its pointer) and a fake ``_sync_download`` handle:

* synced: current / changed resume; newer → the sync download of ``revision =
  head`` and ``_sync_base = head``; conflict → SYNC_CONFLICT_DE; a marker repair →
  current with the record repaired;
* record-less: equal → resume WITHOUT a record (the Start never writes one: a
  record-less dataset is remembered by the library's background step, T2-1, and
  the session's own upload writes one after its commit); descendant → resume;
  ancestor → the sync download; diverged → SYNC_UNKNOWN_DE;
* the sync download's failures (disk with the worker's numbers, stalled, a hub
  dataset this robot cannot open, anything else), FINISH while it runs, and no
  Daten at all;
* no_token / token_refused → records without upload; auth_refused →
  HUB_CHECK_AUTH_DE; unreachable / 5xx / black-holed → records offline within
  the bound; upload off never asks the hub;
* the lease: an upload WAITS and continues, FINISH while waiting abandons the
  Start silently, edit/delete/download refuse, a lease that raises is ignored;
* the create record (display name + visibility only);
* the node's ``_enqueue_dataset_upload`` adds ``expected_hub_sha`` iff the Start
  checked; the HF worker passes it only when the request has the key, and
  forwards the upload's status extras;
* no upload worker wired → UPLOAD_NOT_STARTED_DE and nothing pushed.

``data_manager`` is loaded by path with the record-FSM test's stubs (A18).
"""

import ast
import datetime
import hashlib
import importlib.util
import json
import pathlib
import queue
import shutil
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
from unittest import mock

from timeout_guard import BoundedTestCase  # V1-3: a hang fails within the limit

import test_data_manager_record_fsm as F

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PKG = REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
NODE_PATH = PKG / 'physical_ai_server.py'
WORKER_PATH = PKG / 'data_processing' / 'hf_api_worker.py'
REPO = 'maxmuster/omx_f_Wuerfel-in-die-Schale'
MOD = None
S = None


def setUpModule():
    global MOD, S
    MOD = F._load_data_manager_module()          # a fresh copy with the real `time`
    MOD.DataManager._rig_hf_namespaces = classmethod(lambda cls: None)
    MOD.START_UPLOAD_POLL_S = 0.01
    S = MOD.dataset_sync


def texts():
    return MOD.record_texts_de


class RepositoryNotFoundError(Exception):
    """Named like huggingface_hub's: hub_sync classifies by class name."""


def http_error(code):
    error = RuntimeError(f'HTTP {code}')
    error.response = types.SimpleNamespace(status_code=code)
    return error


# ── the fake hub ──────────────────────────────────────────────────────────────

SESSION1 = {
    'data/chunk-000/file-000.parquet': b'data-1' * 50,
    'meta/episodes/chunk-000/file-000.parquet': b'episodes-1' * 20,
    'videos/observation.images.scene/chunk-000/file-000.mp4': b'\x00video-1' * 100,
    'meta/info.json': b'{"codebase_version": "v3.0", "fps": 30, "total_episodes": 3}',
    'meta/stats.json': b'{"s": 1}',
    'meta/tasks.parquet': b'tasks',
}
SESSION2 = {
    'data/chunk-000/file-001.parquet': b'data-2' * 50,
    'meta/episodes/chunk-000/file-001.parquet': b'episodes-2' * 20,
    'videos/observation.images.scene/chunk-000/file-001.mp4': b'\x00video-2' * 100,
    'meta/info.json': b'{"codebase_version": "v3.0", "fps": 30, "total_episodes": 5}',
    'meta/stats.json': b'{"s": 2}',
}
OTHER2 = {
    'data/chunk-000/file-001.parquet': b'data-X' * 50,
    'meta/episodes/chunk-000/file-001.parquet': b'episodes-X' * 20,
    'videos/observation.images.scene/chunk-000/file-001.mp4': b'\x00video-X' * 100,
    'meta/info.json': b'{"codebase_version": "v3.0", "fps": 30, "total_episodes": 4}',
    'meta/stats.json': b'{"s": 3}',
}


def layered(*layers):
    out = {}
    for layer in layers:
        out.update(layer)
    return out


def write_tree(root, files):
    root = pathlib.Path(root)
    for p, data in files.items():
        f = root / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)


def _entry(path, data):
    if path.endswith(('.parquet', '.mp4')):
        sha = hashlib.sha256(data).hexdigest()
        return {'size': len(data), 'blob_id': S.git_sha1_bytes(S.lfs_pointer(sha, len(data))), 'lfs': sha}
    return {'size': len(data), 'blob_id': S.git_sha1_bytes(data), 'lfs': None}


class FakeHub:
    """One dataset repo whose content is ``files``; every call counted. Called
    as ``HfApi()`` it returns itself."""

    def __init__(self, account='maxmuster'):
        self.account = account
        self.repo = None                      # {'head', 'files', 'title', 'date'}
        self.calls = []
        self.whoami_error = None
        self.view_error = None
        self.files_error = None
        self.exists_error = None
        self.hang = None

    def __call__(self, *a, **k):
        return self

    def put(self, files, head='a' * 40, title='EduBotics: upload', date='2026-10-01T10:00:00+00:00'):
        self.repo = {'head': head, 'files': {p: _entry(p, d) for p, d in files.items()},
                     'title': title, 'date': date}
        return head

    def trees(self):
        out = {}
        for d in S.SYNC_DIRS:
            items = sorted((p, e['blob_id']) for p, e in self.repo['files'].items() if p.startswith(d + '/'))
            out[d] = hashlib.sha1(json.dumps(items).encode()).hexdigest() if items else None
        return out

    def whoami(self):
        self.calls.append('whoami')
        if self.hang is not None:
            self.hang.wait(10)
        if self.whoami_error:
            raise self.whoami_error
        return {'name': self.account, 'orgs': [{'name': 'schule'}]}

    def repo_exists(self, repo_id, repo_type=None):
        self.calls.append('repo_exists')
        if self.exists_error:
            raise self.exists_error
        return self.repo is not None

    def list_repo_refs(self, repo_id, repo_type=None):
        self.calls.append('list_repo_refs')
        if self.view_error:
            raise self.view_error
        if self.repo is None:
            raise RepositoryNotFoundError(f'404 {repo_id}')
        return types.SimpleNamespace(branches=[types.SimpleNamespace(name='main',
                                                                     target_commit=self.repo['head'])])

    def list_repo_tree(self, repo_id, repo_type=None, revision=None, expand=False, recursive=False):
        assert revision == self.repo['head']
        if recursive:
            self.calls.append('files')
            if self.files_error:
                raise self.files_error
            return [types.SimpleNamespace(path=p, size=e['size'], blob_id=e['blob_id'],
                                          lfs=types.SimpleNamespace(sha256=e['lfs']) if e['lfs'] else None)
                    for p, e in sorted(self.repo['files'].items())]
        self.calls.append('tree')
        date = datetime.datetime.fromisoformat(self.repo['date'])
        lc = types.SimpleNamespace(oid=self.repo['head'], title=self.repo['title'], date=date)
        return [types.SimpleNamespace(path=d, tree_id=t, last_commit=lc) for d, t in self.trees().items() if t]


class Handle:
    """A fake sync-download handle: answers ``result`` after ``polls`` polls,
    or runs ``on_poll`` on every poll."""

    def __init__(self, result, polls=2, on_poll=None):
        self.result = result
        self.polls = polls
        self.on_poll = on_poll
        self.cancelled = False
        self.n = 0

    def poll(self):
        self.n += 1
        if self.on_poll:
            self.on_poll(self.n)
        if self.result is None or self.n < self.polls:
            return None
        return self.result

    def cancel(self):
        self.cancelled = True


class D14Case(BoundedTestCase):

    def setUp(self):
        self.hub = FakeHub()
        saved = (MOD.HfApi, MOD.LeRobotDatasetWrapper, MOD._resume_compatibility_check)
        self.addCleanup(self._restore, saved)
        MOD.HfApi = self.hub
        self.opened = []
        MOD.LeRobotDatasetWrapper = lambda repo, root: self.opened.append(repo) or F._OpenedDataset()
        MOD._resume_compatibility_check = lambda *a, **k: None
        self.root = pathlib.Path(tempfile.mkdtemp(prefix='dm_d14_'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.dm = MOD.DataManager(self.root, 'omx_f', F._TaskInfo(), upload_callback=lambda *a: None)
        self.created = []
        self.dm._create_dataset = lambda repo, images, joints: self.created.append(repo) or F._OpenedDataset()
        self.downloads = []
        self.handle = None
        self.path = self.dm._save_path

    @staticmethod
    def _restore(saved):
        MOD.HfApi, MOD.LeRobotDatasetWrapper, MOD._resume_compatibility_check = saved

    # -- helpers ------------------------------------------------------------------

    def local(self, *layers):
        write_tree(self.path, layered(*layers))

    def synced(self, head, *layers):
        """A local copy that our last upload left equal to the hub at ``head``."""
        self.local(*layers)
        self.hub.put(layered(*layers), head=head)
        S.write_record(self.path, {'v': 1, 'repo_id': REPO, 'hub_sha': head, 'hub_trees': self.hub.trees(),
                                   'local_digest': S.meta_digest(self.path),
                                   'files': S.files_manifest(self.path), 'synced_at': S.now_iso()})

    def inject(self, result, **kw):
        def start(repo_id, revision, root, fp):
            self.downloads.append((repo_id, revision, root, fp))
            self.handle = Handle(result, **kw)
            return self.handle
        self.dm._sync_download = start

    def check(self):
        return self.dm.check_lerobot_dataset({'scene': F._Img()}, ['j1'])

    def warning(self):
        return self.dm._last_warning_message

    def assert_resumed(self, base):
        self.assertTrue(self.check())
        self.assertEqual(self.opened, [REPO])
        self.assertEqual(self.created, [])
        self.assertEqual(self.dm._sync_base, base)

    def assert_refused(self, message):
        self.assertFalse(self.check())
        self.assertEqual(self.warning(), message)
        self.assertEqual((self.opened, self.created), ([], []))
        self.assertIsNone(self.dm._lerobot_dataset)


class SyncedVerdicts(D14Case):

    def test_current_resumes_at_the_head(self):
        self.synced('1' * 40, SESSION1)
        self.assert_resumed('1' * 40)
        self.assertEqual(self.downloads, [])
        self.assertNotIn('files', self.hub.calls, 'a synced record decides without the recursive listing')

    def test_changed_resumes_at_the_head(self):
        self.synced('1' * 40, SESSION1)
        self.local(SESSION2)                                  # a session recorded here since
        self.assert_resumed('1' * 40)

    def test_newer_is_downloaded_first(self):
        self.synced('1' * 40, SESSION1)
        self.hub.put(layered(SESSION1, OTHER2), head='2' * 40)
        self.inject({'ok': True, 'revision': '2' * 40})
        self.assert_resumed('2' * 40)
        self.assertEqual(self.downloads, [(REPO, '2' * 40, str(self.path), '')])

    def test_conflict_is_refused(self):
        self.synced('1' * 40, SESSION1)
        self.local(SESSION2)
        self.hub.put(layered(SESSION1, OTHER2), head='2' * 40)
        self.inject({'ok': True, 'revision': '2' * 40})
        self.assert_refused(texts().SYNC_CONFLICT_DE)
        self.assertEqual(self.downloads, [])

    def test_our_own_lost_record_write_is_repaired(self):
        self.synced('1' * 40, SESSION1)
        self.local(SESSION2)
        digest = S.meta_digest(self.path)
        self.hub.put(layered(SESSION1, SESSION2), head='2' * 40, title=f'{S.marker(digest)} EduBotics: x')
        self.assert_resumed('2' * 40)
        record = S.read_record(self.path)
        self.assertEqual((record['hub_sha'], record['hub_trees']), ('2' * 40, self.hub.trees()))


class RecordlessVerdicts(D14Case):

    def test_equal_resumes_and_writes_no_record(self):
        self.local(SESSION1)
        self.hub.put(SESSION1, head='1' * 40)
        self.assert_resumed('1' * 40)
        self.assertFalse(S.record_path(self.path).exists(),
                         'the Start writes no record (T2-1: only the library\'s background step does)')

    def test_a_descendant_resumes(self):
        self.local(SESSION1, SESSION2)
        self.hub.put(SESSION1, head='1' * 40)
        self.assert_resumed('1' * 40)

    def test_an_ancestor_is_downloaded_first(self):
        self.local(SESSION1)
        self.hub.put(layered(SESSION1, SESSION2), head='2' * 40)
        self.inject({'ok': True, 'revision': '2' * 40})
        self.assert_resumed('2' * 40)
        self.assertEqual([d[1] for d in self.downloads], ['2' * 40])

    def test_diverged_copies_are_refused(self):
        self.local(SESSION1, SESSION2)
        self.hub.put(layered(SESSION1, OTHER2), head='2' * 40)
        self.assert_refused(texts().SYNC_UNKNOWN_DE)

    def test_an_absent_repo_resumes_with_no_dataset_online(self):
        self.local(SESSION1)
        self.assert_resumed(None)

    def test_an_absent_repo_of_another_namespace_proves_nothing(self):
        self.hub.account = 'someone-else'
        self.local(SESSION1)
        self.assert_resumed(MOD.SYNC_UNCHECKED)


class TheSyncDownload(D14Case):

    def setUp(self):
        super().setUp()
        self.synced('1' * 40, SESSION1)
        self.hub.put(layered(SESSION1, OTHER2), head='2' * 40)

    def test_each_failure_is_its_sentence(self):
        cases = [
            ({'ok': False, 'code': 'disk', 'free': 1_200_000_000, 'need': 5_400_000_000},
             texts().sync_disk_de(1_200_000_000, 5_400_000_000)),
            ({'ok': False, 'code': 'stalled'}, texts().DOWNLOAD_STALL_DE),
            ({'ok': False, 'code': 'timeout'}, texts().DOWNLOAD_STALL_DE),
            ({'ok': False, 'code': 'other_robot'}, texts().SYNC_HUB_UNUSABLE_DE),
            ({'ok': False, 'code': 'old_format'}, texts().SYNC_HUB_UNUSABLE_DE),
            ({'ok': False, 'code': 'unsupported'}, texts().SYNC_HUB_UNUSABLE_DE),
            ({'ok': False, 'code': 'auth'}, texts().HF_AUTH_ERROR_DE),
            ({'ok': False, 'code': 'unreachable'}, texts().HF_NETWORK_ERROR_DE),
            ({'ok': False, 'code': 'token_changed'}, texts().SYNC_DOWNLOAD_FAILED_DE),
            ({'ok': False, 'code': 'broken'}, texts().SYNC_DOWNLOAD_FAILED_DE),
            ({'ok': False, 'code': 'cancelled'}, texts().SYNC_DOWNLOAD_FAILED_DE),
        ]
        self.assertIn('1,2 GB', cases[0][1])
        for result, message in cases:
            with self.subTest(code=result['code']):
                self.setUp()
                self.inject(result)
                self.assert_refused(message)
                self.assertEqual(self.dm._sync_base, MOD.SYNC_UNCHECKED)

    def test_finish_while_it_runs_abandons_the_start_silently(self):
        self.inject(None, on_poll=lambda n: n == 3 and self.dm.request_end('finish'))
        self.assertFalse(self.check())
        self.assertTrue(self.handle.cancelled)
        self.assertTrue(self.dm._start_abandoned)
        self.assertEqual((self.opened, self.created, self.warning()), ([], [], ''))

    def test_without_daten_the_start_is_refused(self):
        self.dm._sync_download = None
        self.assert_refused(texts().SYNC_DOWNLOAD_FAILED_DE)

    def test_the_d7_download_asks_for_mains_head(self):
        shutil.rmtree(self.path)
        S.record_path(self.path).unlink()
        self.inject({'ok': True, 'revision': '2' * 40})
        self.assert_resumed('2' * 40)
        self.assertEqual([d[1] for d in self.downloads], [None], 'the worker resolves the head')
        self.assertEqual(self.hub.calls[:2], ['whoami', 'repo_exists'])

    def test_a_d7_download_stub_without_a_revision_leaves_the_base_unchecked(self):
        shutil.rmtree(self.path)
        self.inject({'ok': True, 'revision': None})
        self.assert_resumed(MOD.SYNC_UNCHECKED)


class AskingTheHub(D14Case):

    def setUp(self):
        super().setUp()
        self.synced('1' * 40, SESSION1)

    def test_no_token_and_a_refused_token_record_without_upload(self):
        no_token = type('LocalTokenNotFoundError', (Exception,), {})('Token is required')
        for error, notice in ((no_token, texts().UPLOAD_OFF_NO_TOKEN_DE),
                              (http_error(401), texts().UPLOAD_OFF_TOKEN_INVALID_DE)):
            with self.subTest(error=error):
                self.setUp()
                self.hub.whoami_error = error
                self.assert_resumed(MOD.SYNC_UNCHECKED)
                self.assertFalse(self.dm._task_info.push_to_hub)
                self.assertEqual(self.dm.get_current_record_status().error, '[WARNUNG] ' + notice)

    def test_a_refused_repo_query_names_the_token(self):
        self.hub.view_error = http_error(403)
        self.assert_refused(texts().HUB_CHECK_AUTH_DE)

    def test_an_unreachable_hub_records_offline(self):
        for where, error in (('whoami', ConnectionError('refused')), ('view', http_error(503)),
                             ('view', http_error(429)), ('files', TimeoutError('timed out'))):
            with self.subTest(where=where, error=error):
                self.setUp()
                if where == 'files':
                    S.record_path(self.path).unlink()               # record-less: the listing decides
                    self.hub.put(SESSION1, head='1' * 40)
                    self.hub.files_error = error
                else:
                    setattr(self.hub, f'{where}_error', error)
                self.assert_resumed(MOD.SYNC_UNCHECKED)
                self.assertTrue(self.dm._task_info.push_to_hub)
                self.assertEqual(self.dm.get_current_record_status().error,
                                 '[WARNUNG] ' + texts().OFFLINE_START_DE)

    def test_a_black_holed_hub_records_offline_within_the_bound(self):
        saved = MOD.HUB_CHECK_TIMEOUT_S
        self.assertEqual(saved, 15.0)
        MOD.HUB_CHECK_TIMEOUT_S = 0.3
        self.hub.hang = threading.Event()
        try:
            t0 = time.monotonic()
            self.assert_resumed(MOD.SYNC_UNCHECKED)
            elapsed = time.monotonic() - t0
        finally:
            MOD.HUB_CHECK_TIMEOUT_S = saved
            self.hub.hang.set()
        self.assertLess(elapsed, 1.5)
        self.assertEqual(self.dm.get_current_record_status().error, '[WARNUNG] ' + texts().OFFLINE_START_DE)

    def test_upload_off_never_asks_the_hub(self):
        self.dm._task_info.push_to_hub = False
        self.assert_resumed(MOD.SYNC_UNCHECKED)
        self.assertEqual(self.hub.calls, [])


class TheLease(D14Case):

    def setUp(self):
        super().setUp()
        self.synced('1' * 40, SESSION1)

    def test_an_upload_of_this_dataset_is_waited_for(self):
        answers = ['upload', 'upload', None]
        roots = []
        self.dm._dataset_lease = lambda root: roots.append(root) or answers.pop(0)
        self.assert_resumed('1' * 40)
        self.assertEqual(len(roots), 3)
        self.assertEqual(roots[0], self.path)

    def test_finish_while_waiting_abandons_the_start_silently(self):
        self.dm._dataset_lease = lambda root: 'upload'
        threading.Timer(0.1, lambda: self.dm.request_end('finish')).start()
        self.assertFalse(self.check())
        self.assertTrue(self.dm._start_abandoned)
        self.assertEqual((self.opened, self.created, self.warning(), self.hub.calls), ([], [], '', []))

    def test_an_edit_a_delete_or_a_download_refuses(self):
        for kind in ('edit', 'delete', 'download'):
            with self.subTest(kind=kind):
                self.setUp()
                self.dm._dataset_lease = lambda root, kind=kind: kind
                self.assert_refused(texts().DATASET_BUSY_START_DE)
                self.assertEqual(self.hub.calls, [])

    def test_a_lease_that_raises_is_no_lease(self):
        def boom(root):
            raise RuntimeError('registry broken')
        self.dm._dataset_lease = boom
        self.assert_resumed('1' * 40)
        self.dm._dataset_lease = None
        self.assertIsNone(self.dm._lease_reason(self.path))


class TheOldPageDownloadGuards(BoundedTestCase):
    """V1-2: the old page's ``/huggingface/control`` dataset download
    (``DataManager.download_huggingface_repo``) never mixes files into an
    existing dataset (DOWNLOAD_EXISTS_DE) and never lands outside the dataset
    root (a ``repo_id`` off the wire, safe_under) — refused before any fetch."""

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix='dm_olddl_')).resolve()
        self.outside = pathlib.Path(tempfile.mkdtemp(prefix='dm_olddl_out_')).resolve()
        for p in (self.root, self.outside):
            self.addCleanup(shutil.rmtree, p, ignore_errors=True)
        saved = (MOD.dataset_paths.dataset_root, MOD.snapshot_download)
        self.addCleanup(self._restore, saved)
        MOD.dataset_paths.dataset_root = lambda: self.root
        self.fetched = []
        MOD.snapshot_download = lambda **kw: self.fetched.append(kw) or str(kw['local_dir'])

    @staticmethod
    def _restore(saved):
        MOD.dataset_paths.dataset_root, MOD.snapshot_download = saved

    def download(self, repo_id):
        return MOD.DataManager.download_huggingface_repo(repo_id, 'dataset')

    def test_an_existing_dataset_is_refused_before_any_fetch(self):
        (self.root / 'lena' / 'omx_f_a' / 'meta').mkdir(parents=True)
        self.assertFalse(self.download('lena/omx_f_a'))
        self.assertEqual(MOD.DataManager._last_hf_failure_reason_de, MOD._daten_texts().DOWNLOAD_EXISTS_DE)
        self.assertEqual(self.fetched, [])

    def test_a_repo_id_that_leaves_the_root_is_refused_before_any_fetch(self):
        escape = f'../{self.outside.name}/evil'
        for repo_id in (escape, str(self.outside / 'evil2'), 'lena/../../evil3'):
            with self.subTest(repo_id=repo_id):
                self.assertFalse(self.download(repo_id))
                self.assertTrue(MOD.DataManager._last_hf_failure_reason_de)
                self.assertEqual(self.fetched, [])
        self.assertEqual(list(self.outside.iterdir()), [], 'nothing was created outside the root')

    def test_a_new_dataset_is_fetched_into_its_own_folder(self):
        self.assertEqual(self.download('lena/omx_f_b'), str(self.root / 'lena' / 'omx_f_b'))
        self.assertEqual([pathlib.Path(kw['local_dir']) for kw in self.fetched], [self.root / 'lena' / 'omx_f_b'])


class TheCreateRecord(D14Case):

    def test_a_created_dataset_records_its_name_and_visibility_only(self):
        self.dm._task_info.push_to_hub = False

        def create(repo, images, joints):
            self.path.mkdir(parents=True)
            self.created.append(repo)
            return F._OpenedDataset()
        self.dm._create_dataset = create
        self.assertTrue(self.check())
        self.assertEqual(S.read_record(self.path), {'v': 1, 'repo_id': REPO,
                                                    'display_name': 'Würfel in die Schale', 'private': True})


class NoUploadWorker(D14Case):

    def test_nothing_is_pushed_and_the_session_says_so(self):
        self.dm._upload_callback = None
        pushed = []
        self.dm._lerobot_dataset = types.SimpleNamespace(push_to_hub=lambda **k: pushed.append(k))
        self.dm._upload_dataset(tags=[], private=True)
        self.assertEqual(pushed, [])
        self.assertEqual(self.dm._upload_blocked_reason_de, texts().UPLOAD_NOT_STARTED_DE)
        fn = next(n for n in ast.walk(ast.parse(pathlib.Path(MOD.__file__).read_text(encoding='utf-8')))
                  if isinstance(n, ast.FunctionDef) and n.name == '_upload_dataset')
        self.assertNotIn('push_to_hub', {getattr(n, 'attr', None) for n in ast.walk(fn)})


# ── the node and the HF worker ────────────────────────────────────────────────

def _node_function(name):
    """A method of physical_ai_server.py, compiled alone (the module needs rclpy)."""
    tree = ast.parse(NODE_PATH.read_text(encoding='utf-8'))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace = {'SYNC_UNCHECKED': MOD.SYNC_UNCHECKED, 'record_texts_de': MOD.record_texts_de, 'json': json}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(NODE_PATH), 'exec'), namespace)
    return namespace[name]


class _Worker:
    def __init__(self):
        self.requests = []

    def is_alive(self):
        return True

    def is_busy(self):
        return False

    def send_request(self, data):
        self.requests.append(data)
        return True


class TheNodeHandsTheBaseToTheUpload(BoundedTestCase):

    def _node(self, base):
        node = types.SimpleNamespace(hf_api_worker=_Worker(), get_logger=lambda: mock.Mock(),
                                     _init_hf_api_worker=lambda: None,
                                     _publish_synthetic_upload_failed=mock.Mock())
        if base is not None:
            node.data_manager = types.SimpleNamespace(_sync_base=base[0])
        return node

    def test_expected_hub_sha_is_added_iff_the_start_checked(self):
        enqueue = _node_function('_enqueue_dataset_upload')
        for base, expected in (((MOD.SYNC_UNCHECKED,), 'absent'), (('3' * 40,), '3' * 40), ((None,), None),
                               (None, 'absent')):
            with self.subTest(base=base):
                node = self._node(base)
                enqueue(node, REPO, '/data/x', True)
                request = node.hf_api_worker.requests[0]
                self.assertEqual(request.get('expected_hub_sha', 'absent'), expected)
                self.assertEqual((request['mode'], request['repo_type'], request['private']),
                                 ('upload', 'dataset', True))

    def test_the_auto_upload_sentences_are_the_constants(self):
        enqueue = _node_function('_enqueue_dataset_upload')
        node = self._node(None)
        node.hf_api_worker.is_busy = lambda: True
        enqueue(node, REPO, '/data/x', True)
        node._publish_synthetic_upload_failed.assert_called_once_with(REPO, texts().AUTO_UPLOAD_BUSY_DE)
        node = self._node(None)
        node.hf_api_worker.send_request = lambda data: False
        enqueue(node, REPO, '/data/x', True)
        node._publish_synthetic_upload_failed.assert_called_once_with(REPO, texts().AUTO_UPLOAD_REFUSED_DE)
        node = self._node(None)
        node.hf_api_worker.send_request = mock.Mock(side_effect=RuntimeError('pipe'))
        enqueue(node, REPO, '/data/x', True)
        node._publish_synthetic_upload_failed.assert_called_once_with(REPO, texts().AUTO_UPLOAD_FAILED_DE)
        node = self._node(None)
        node.hf_api_worker.is_alive = lambda: False
        enqueue(node, REPO, '/data/x', True)
        node._publish_synthetic_upload_failed.assert_called_once_with(REPO, texts().AUTO_UPLOAD_NO_WORKER_DE)

    def test_the_status_carries_repo_type_and_info_json(self):
        publish = _node_function('_publish_hf_operation_status_msg')
        sent = []
        msg_cls = type('HFOperationStatus', (), {})
        node = types.SimpleNamespace(hf_status_publisher=types.SimpleNamespace(publish=sent.append))
        with mock.patch.dict(publish.__globals__, {'HFOperationStatus': msg_cls}):
            publish(node, {'operation': 'upload', 'status': 'Success', 'repo_id': REPO, 'repo_type': 'dataset',
                           'info_json': {'fps': 30, 'display_name': 'Würfel'}, 'progress': {}})
            publish(node, {'operation': 'upload', 'status': 'Uploading', 'progress': {}})
        self.assertEqual(sent[0].repo_type, 'dataset')
        self.assertEqual(json.loads(sent[0].info_json), {'fps': 30, 'display_name': 'Würfel'})
        self.assertEqual((sent[1].repo_type, sent[1].info_json), ('', ''))

    def test_an_abandoned_start_returns_silently_and_daten_is_injected_only_when_it_runs(self):
        src = NODE_PATH.read_text(encoding='utf-8')
        self.assertIn("if getattr(data_manager, '_start_abandoned', False):", src)
        tree = ast.parse(src)
        init = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                    and n.name == 'init_robot_control_parameters_from_user_task')
        body = ast.unparse(init)
        self.assertIn("daten = getattr(self, 'daten', None)", body)
        self.assertIn('self.data_manager._dataset_lease = daten.claim_record_lease', body)
        self.assertIn('self.data_manager._sync_download = daten.start_sync_download', body)


class _WorkerDataManager:
    calls = []
    _last_hf_failure_reason_de = None
    _last_upload_extras = None
    result = True

    @staticmethod
    def set_progress_queue(_q):
        return None

    @classmethod
    def upload_huggingface_repo(cls, **kw):
        cls.calls.append(kw)
        cls._last_upload_extras = {'repo_type': 'dataset', 'info_json': {'fps': 30, 'private': False}}
        return cls.result


class TheHfWorker(BoundedTestCase):

    @classmethod
    def setUpClass(cls):
        with mock.patch.dict(sys.modules):
            for name in ('physical_ai_server', 'physical_ai_server.data_processing'):
                sys.modules[name] = types.ModuleType(name)
            dm = types.ModuleType('physical_ai_server.data_processing.data_manager')
            dm.DataManager = _WorkerDataManager
            sys.modules[dm.__name__] = dm
            spec = importlib.util.spec_from_file_location('_hf_worker_d14_test', str(WORKER_PATH))
            cls.W = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.W)

    def setUp(self):
        _WorkerDataManager.calls = []

    def _run(self, *requests):
        inq, outq, progq = queue.Queue(), queue.Queue(), queue.Queue()
        for r in requests:
            inq.put(r)
        inq.put(None)
        with mock.patch.dict(sys.modules):
            sys.modules.pop('physical_ai_server.data_processing.hf_token_store', None)
            sys.modules.pop('physical_ai_server.data_processing.progress_tracker', None)
            self.W.HfApiWorker._worker_process_loop(inq, outq, progq)
        out, prog = [], []
        while not outq.empty():
            out.append(outq.get_nowait())
        while not progq.empty():
            prog.append(progq.get_nowait())
        return out, prog

    def test_expected_hub_sha_only_when_the_request_names_it(self):
        base = {'mode': 'upload', 'repo_id': REPO, 'repo_type': 'dataset', 'local_dir': '/d', 'private': True}
        self._run(dict(base), dict(base, expected_hub_sha=None), dict(base, expected_hub_sha='4' * 40))
        calls = _WorkerDataManager.calls
        self.assertNotIn('expected_hub_sha', calls[0])
        self.assertIn('expected_hub_sha', calls[1])
        self.assertIsNone(calls[1]['expected_hub_sha'])
        self.assertEqual(calls[2]['expected_hub_sha'], '4' * 40)
        src = WORKER_PATH.read_text(encoding='utf-8')
        self.assertNotIn('_UNSET', src, 'the worker never imports the sentinel (A18)')

    def test_the_extras_reach_the_parent_and_the_tuples_keep_their_shape(self):
        out, prog = self._run({'mode': 'upload', 'repo_id': REPO, 'repo_type': 'dataset', 'local_dir': '/d'})
        self.assertEqual(out, [('success', f'Hugging Face-Upload abgeschlossen: {REPO}')])
        extras = [p for p in prog if isinstance(p, dict) and p.get('type') == self.W.UPLOAD_EXTRAS_ITEM_TYPE]
        self.assertEqual(extras[0]['extras']['info_json'], {'fps': 30, 'private': False})
        # the parent side: the status gets repo_type + info_json, even when the
        # extras item arrives after the result tuple
        worker = self.W.HfApiWorker.__new__(self.W.HfApiWorker)
        worker.logger = self.W.logging.getLogger('d14')
        worker.logger.disabled = True
        worker.is_processing = True
        worker.current_task = {'mode': 'upload', 'repo_id': REPO, 'repo_type': 'dataset'}
        worker.current_progress = {'current': 0, 'total': 0, 'percentage': 0.0}
        worker.last_logged_current_progress = -1
        worker.is_alive = lambda: True
        worker.progress_queue = queue.Queue()
        worker.stall_watch = self.W.UploadStallWatch(time.monotonic())
        worker.get_result = lambda block=False, timeout=0.1: ('success', 'fertig')
        threading.Timer(0.1, lambda: worker.progress_queue.put(extras[0])).start()
        status = worker.check_task_status()
        self.assertEqual((status['status'], status['message']), ('Success', 'fertig'))
        self.assertEqual(status['repo_type'], 'dataset')
        self.assertEqual(status['info_json'], {'fps': 30, 'private': False})

    def test_an_unconfirmed_upload_says_so_on_success(self):
        saved = _WorkerDataManager.upload_huggingface_repo

        def unconfirmed(**kw):
            _WorkerDataManager._last_upload_extras = {'repo_type': 'dataset', 'message_de': 'nicht bestätigt'}
            return True
        _WorkerDataManager.upload_huggingface_repo = staticmethod(unconfirmed)
        try:
            out, _ = self._run({'mode': 'upload', 'repo_id': REPO, 'repo_type': 'dataset', 'local_dir': '/d'})
        finally:
            _WorkerDataManager.upload_huggingface_repo = saved
        self.assertEqual(out, [('success', 'nicht bestätigt')])


if __name__ == '__main__':
    unittest.main()

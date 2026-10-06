"""Daten 2.0 — the node side, ``daten/node_service.py::DatenService`` (spec §D3,
§E3, §E10, §J.3, §J.5).

A fake node, fake processes (``Popen``) and a fake HF worker; the real stdlib
modules (contract, texts, dataset_sync, hub_sync, dataset_paths, the edit
worker's line protocol) loaded by path. What is proven:

* every action's validation and codes, in the spec's order (``stale`` before
  the indices; ``keep_both``'s own list; ``replace`` / ``delete_dataset``
  without a digest → ``invalid``, with another one → ``stale`` and nothing
  touched, U-4); ``link`` refuses more than 200 ids;
* the leases per kind (``record`` is the node's state and ends with it;
  ``upload`` the HF worker's task; ``download``/``edit``/``delete`` jobs), and
  the lease lock is never held across a file-system call or a call into the HF
  worker (an instrumented lock + an audit hook);
* job lifecycle: progress lines, the timeout kill, the failure message; a
  download job's byte progress; a token change kills the download's process
  group within 2 polls with ``.tmp_sync`` gone (``token_changed``), recovery
  running BEFORE the tmp is removed (U-1); a stall; a cancel;
* ``keep_both``: its three stages, each stage's process taking the dataset
  lock itself (the job holds none, G-3), the edit → upload hand-over with no
  gap, ``hub_changed`` keeping the result locally, a stage 3 that cannot be
  enqueued → ``unavailable``;
* boot recovery on a temp root for every rule (the split journal both ways,
  ``.tmp_keep``, ``.tmp_base``, a broken swap both ways, a cut-short delete, a
  held lock skipped), holding an ``edit`` lease while it works and skipping a
  dataset with any lease (U-5);
* ``start_sync_download``: queued behind a running download, started when it
  ends, cancelled by its handle, shown as a download job while the busy kind
  stays ``record``;
* no ``HfApi`` in the module (no network in a callback).
"""

import ast
import fcntl
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import queue
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest

from timeout_guard import BoundedTestCase  # V1-3: a hang fails within the limit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PKG = REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
NODE_SERVICE_PATH = PKG / 'daten' / 'node_service.py'


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_edit_worker():
    """The real edit worker module (its line protocol), with the package stub
    the other deps-free loaders use."""
    saved = {k: sys.modules.get(k) for k in ('physical_ai_server', 'physical_ai_server.data_processing',
                                              'physical_ai_server.data_processing.data_editor_v3',
                                              'physical_ai_server.data_processing.dataset_paths')}
    try:
        pkg = sys.modules.get('physical_ai_server') or types.ModuleType('physical_ai_server')
        sys.modules['physical_ai_server'] = pkg
        dp = sys.modules.get('physical_ai_server.data_processing') or \
            types.ModuleType('physical_ai_server.data_processing')
        sys.modules['physical_ai_server.data_processing'] = dp
        for name in ('dataset_paths', 'data_editor_v3'):
            full = f'physical_ai_server.data_processing.{name}'
            if not hasattr(sys.modules.get(full), '__file__'):
                _load(full, PKG / 'data_processing' / f'{name}.py')
            setattr(dp, name, sys.modules[full])
        return _load('_edubotics_ns_data_processing_edit_worker', PKG / 'data_processing' / 'edit_worker.py')
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


EW = _load_edit_worker()
NS = _load('_daten_node_service_under_test', NODE_SERVICE_PATH)
C, T, R, S, HS = NS.C, NS.T, NS.R, NS.S, NS.HS
STORE = NS._mod('data_processing', 'hf_token_store')
TOKEN_A = 'hf_' + 'a' * 34
TOKEN_B = 'hf_' + 'b' * 34
HEAD = '1' * 40
BASE = '2' * 40

# ── the lease lock, instrumented ──────────────────────────────────────────────

_VIOLATIONS = []
_WATCH = {'lock': None}


class InstrumentedLock:
    def __init__(self):
        self._lock = threading.Lock()
        self.holder = None

    def __enter__(self):
        self._lock.acquire()
        self.holder = threading.get_ident()
        return self

    def __exit__(self, *exc):
        self.holder = None
        self._lock.release()
        return False

    def held_here(self):
        return self.holder == threading.get_ident()


def _audit(event, args):
    lock = _WATCH['lock']
    if lock is not None and lock.held_here() and event in (
            'open', 'os.rename', 'os.remove', 'os.listdir', 'os.scandir', 'shutil.rmtree', 'os.mkdir',
            'os.symlink', 'os.link', 'fcntl.flock', 'subprocess.Popen'):
        _VIOLATIONS.append((event, str(args)[:120]))


sys.addaudithook(_audit)


def assert_not_locked():
    lock = _WATCH['lock']
    if lock is not None and lock.held_here():
        _VIOLATIONS.append(('hf-worker', 'called under the lease lock'))


# ── fakes ─────────────────────────────────────────────────────────────────────

class _Stdin(io.StringIO):
    def __init__(self, proc):
        super().__init__()
        self.proc = proc

    def close(self):
        data = self.getvalue()
        super().close()
        self.proc.on_stdin(data)


class FakeProc:
    pids = iter(range(40000, 50000))

    def __init__(self, cmd, kw, script):
        self.cmd, self.kw = cmd, kw
        self.pid = next(FakeProc.pids)
        self.lines = queue.Queue()
        self.returncode = None
        self.killed = False
        self.ended = threading.Event()
        self.script = script
        self.request = None
        self.stdin = _Stdin(self)
        self.stdout = iter(self.lines.get, None)

    def on_stdin(self, data):
        self.request = data
        if self.script is not None:                  # the "process" runs beside its supervisor
            threading.Thread(target=self.script, args=(self, data), daemon=True).start()

    def emit(self, line):
        self.lines.put(line + '\n')

    def end(self, code=0):
        if self.returncode is None:
            self.returncode = code
            self.lines.put(None)
            self.ended.set()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.ended.wait(timeout if timeout is not None else 30)
        return self.returncode

    def kill(self):
        self.killed = True
        self.end(-9)


class FakeHf:
    def __init__(self):
        self.is_processing = False
        self.current_task = None
        self.sent = []
        self.alive = True
        self.busy = False
        self.on_send = None
        self.accept = True

    def is_alive(self):
        assert_not_locked()
        return self.alive

    def is_busy(self):
        assert_not_locked()
        return self.busy or self.is_processing

    def send_request(self, request):
        assert_not_locked()
        self.sent.append(request)
        if self.on_send:
            self.on_send(request)
        if not self.accept:
            return False
        self.is_processing = True
        self.current_task = request
        return True


def wait_for(pred, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return False


class ServiceCase(BoundedTestCase):

    def setUp(self):
        self.root = pathlib.Path(os.path.realpath(tempfile.mkdtemp(prefix='d2_ns_')))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.hf = FakeHf()
        self.node = types.SimpleNamespace(on_recording=False, data_manager=None, hf_api_worker=self.hf,
                                          robot_type='omx_f', _init_hf_api_worker=lambda: None,
                                          get_logger=lambda: types.SimpleNamespace(info=lambda *a: None))
        self.cleanups = []
        self.node._cleanup_hf_api_worker_with_threading = lambda: self.cleanups.append(1)
        self.token = TOKEN_A
        self.states = {}
        self.account = 'lena'
        self.free = 10 ** 12
        self.procs = []
        self.scripts = {'edit': lambda proc, payload: (
            proc.emit(EW.RESULT_MARKER + json.dumps({'success': True, 'code': '', 'message': 'ok'})), proc.end())}
        self.svc = NS.DatenService(
            self.node, root=self.root, ros=False, start_threads=False, popen=self.popen,
            kill=lambda proc: proc.kill(), token_reader=lambda: self.token,
            state_reader=lambda p: self.states.get(pathlib.Path(p).name, 'ok'),
            name_rule=lambda n: n.replace(' ', '-'), namespace_reader=lambda: {self.account} if self.account else None,
            disk_free=lambda p: self.free, secret=b's' * 32)
        self.svc.download_poll_s = 0.01
        self.lock = InstrumentedLock()
        self.svc._lock = self.lock
        _WATCH['lock'] = self.lock
        _VIOLATIONS.clear()
        self.addCleanup(lambda: _WATCH.__setitem__('lock', None))

    def tearDown(self):
        for proc in self.procs:
            proc.end()
        self.assertEqual(_VIOLATIONS, [], 'the lease lock was held across a file-system or HF-worker call')

    def popen(self, cmd, **kw):
        kind = 'download' if NS.DOWNLOAD_WORKER_MODULE in cmd else 'edit'
        proc = FakeProc(cmd, kw, self.scripts.get(kind))
        self.procs.append(proc)
        return proc

    def dataset(self, dataset_id, episodes=5, extra=None):
        path = self.root / dataset_id
        (path / 'meta').mkdir(parents=True)
        (path / 'meta' / 'info.json').write_text(json.dumps({'total_episodes': episodes, 'fps': 30}))
        (path / 'data').mkdir()
        (path / 'data' / 'f.parquet').write_bytes(b'x' * 100)
        for rel, data in (extra or {}).items():
            (path / rel).parent.mkdir(parents=True, exist_ok=True)
            (path / rel).write_bytes(data)
        return path

    def cmd(self, action, **args):
        return self.svc.command(action, json.dumps(args))

    def refused(self, out, code, message=None):
        self.assertFalse(out['success'], out)
        self.assertEqual(out['code'], code, out)
        self.assertEqual(out['result'], {})
        if message is not None:
            self.assertEqual(out['message'], message)
        self.assertNotIn('"', out['message'])

    def job(self, job_id):
        return next(j for j in self.svc.state_payload()['jobs'] if j['job_id'] == job_id)

    def wait_job(self, job_id, timeout=10):
        self.assertTrue(wait_for(lambda: self.job(job_id)['state'] != 'running', timeout), self.job(job_id))
        return self.job(job_id)

    def digest(self, path):
        return S.meta_digest(path)


# ── the surface ───────────────────────────────────────────────────────────────

class TheSurface(ServiceCase):

    def test_malformed_requests_are_invalid(self):
        self.refused(self.svc.command('edit', 'not json'), 'invalid')
        self.refused(self.svc.command('edit', '[1]'), 'invalid')
        self.refused(self.cmd('nonsense'), 'invalid')
        self.refused(self.cmd('edit', op='rename', dataset='lena/a'), 'invalid')
        self.refused(self.cmd('edit', op='delete', dataset='../x', meta_digest='d', episodes=[0]), 'invalid')
        self.refused(self.cmd('edit', op='delete', dataset='lena/a.tmp_sync', meta_digest='d', episodes=[0]),
                     'invalid')
        self.refused(self.cmd('cancel', what='everything'), 'invalid')

    def test_link_mints_tokens_for_what_exists(self):
        self.dataset('lena/omx_f_a')
        out = self.cmd('link', library=True, datasets=['lena/omx_f_a', 'lena/omx_f_gone'])
        self.assertTrue(out['success'])
        r = out['result']
        self.assertEqual(r['ttl_s'], 1800)
        self.assertEqual(r['missing'], ['lena/omx_f_gone'])
        self.assertEqual(NS.LT.read_scope(b's' * 32, r['tokens']['lena/omx_f_a'])[0], 'ds:lena/omx_f_a')
        self.assertEqual(NS.LT.read_scope(b's' * 32, r['library_token'])[0], 'lib')
        self.assertIsNone(self.cmd('link', library=False, datasets=[])['result']['library_token'])
        self.refused(self.cmd('link', library=True, datasets=[f'lena/d{k}' for k in range(201)]), 'invalid')

    def test_state_is_the_topic_payload(self):
        out = self.cmd('state')
        self.assertEqual(set(out['result']), {'v', 'seq', 'busy', 'jobs', 'transfer'})

    def test_no_hub_client_in_the_module(self):
        tree = ast.parse(NODE_SERVICE_PATH.read_text(encoding='utf-8'))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
            {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | \
            {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        self.assertFalse({'HfApi', 'huggingface_hub', 'snapshot_download', 'hf_hub_download'} & names)


class EditValidation(ServiceCase):

    def setUp(self):
        super().setUp()
        self.path = self.dataset('lena/omx_f_a')
        self.d = self.digest(self.path)

    def test_stale_is_checked_before_the_indices(self):
        self.refused(self.cmd('edit', op='delete', dataset='lena/omx_f_a', meta_digest='old', episodes=[99]),
                     'stale', T.STALE_DE)
        self.refused(self.cmd('edit', op='delete', dataset='lena/omx_f_a', meta_digest='', episodes=[0]),
                     'invalid')

    def test_existence_and_state(self):
        self.refused(self.cmd('edit', op='delete', dataset='lena/omx_f_none', meta_digest='x', episodes=[0]),
                     'not_found', T.NOT_FOUND_DE)
        for state, message in (('in_session', T.IN_SESSION_DE), ('old_format', T.OLD_FORMAT_DE),
                               ('unsupported', T.UNSUPPORTED_DE), ('incomplete', T.INCOMPLETE_DE)):
            self.states['omx_f_a'] = state
            self.refused(self.cmd('edit', op='delete', dataset='lena/omx_f_a', meta_digest=self.d, episodes=[0]),
                         state, message)

    def test_the_indices(self):
        for episodes in ([], [5], [-1], ['1'], [True], [0, 1, 2, 3, 4], 'x'):
            with self.subTest(episodes=episodes):
                self.refused(self.cmd('edit', op='delete', dataset='lena/omx_f_a', meta_digest=self.d,
                                      episodes=episodes), 'invalid', T.INVALID_EPISODES_DE)

    def test_the_names_and_the_namespace(self):
        base = dict(op='split', dataset='lena/omx_f_a', meta_digest=self.d, episodes=[0])
        self.refused(self.cmd('edit', **base, new_name='  ', owner_ns='lena'), 'invalid')
        self.refused(self.cmd('edit', **base, new_name='b', owner_ns='../x'), 'invalid')
        self.refused(self.cmd('edit', **base, new_name='b', owner_ns='partner'), 'namespace', T.NAMESPACE_EDIT_DE)
        self.dataset('lena/omx_f_b')
        self.refused(self.cmd('edit', **base, new_name='b', owner_ns='lena'), 'exists', T.EXISTS_DE)
        self.account = None                                             # cache unknown: never refused
        self.assertTrue(self.cmd('edit', **base, new_name='neu', owner_ns='partner')['success'])

    def test_merge_needs_two_distinct_current_datasets(self):
        self.dataset('lena/omx_f_b')
        db = self.digest(self.root / 'lena/omx_f_b')
        self.refused(self.cmd('edit', op='merge', datasets=[{'id': 'lena/omx_f_a', 'meta_digest': self.d}],
                              new_name='m', owner_ns='lena'), 'invalid')
        self.refused(self.cmd('edit', op='merge', datasets=[{'id': 'lena/omx_f_a', 'meta_digest': self.d}] * 2,
                              new_name='m', owner_ns='lena'), 'invalid')
        self.refused(self.cmd('edit', op='merge', datasets=[{'id': 'lena/omx_f_a', 'meta_digest': self.d},
                                                            {'id': 'lena/omx_f_b', 'meta_digest': 'old'}],
                              new_name='m', owner_ns='lena'), 'stale')
        out = self.cmd('edit', op='merge', datasets=[{'id': 'lena/omx_f_a', 'meta_digest': self.d},
                                                     {'id': 'lena/omx_f_b', 'meta_digest': db}],
                       new_name='m m', owner_ns='lena')
        self.assertEqual(out['result']['outputs'], ['lena/omx_f_m-m'])

    def test_disk(self):
        self.free = 1_000_000_000 + 50                                  # below the critical floor after the copy
        out = self.cmd('edit', op='delete', dataset='lena/omx_f_a', meta_digest=self.d, episodes=[0])
        self.refused(out, 'disk')
        self.assertIn('GB frei', out['message'])


class EditJobs(ServiceCase):

    def setUp(self):
        super().setUp()
        self.path = self.dataset('lena/omx_f_a')
        self.d = self.digest(self.path)

    def test_a_job_reports_its_progress_and_result(self):
        def script(proc, payload):
            req = json.loads(payload)
            self.assertEqual((req['mode'], req['delete_episode_num']), ('delete', [1, 3]))
            proc.emit(EW.PROGRESS_MARKER + json.dumps({'stage': 'copy', 'done': 1, 'total': 3}))
            self.assertTrue(wait_for(lambda: self.job(self.job_id)['stage'] == 'copy'))
            self.assertEqual(self.svc.busy_kind(self.path), 'edit')
            proc.emit('some library noise')
            proc.emit(EW.RESULT_MARKER + json.dumps({'success': True, 'code': '', 'episodes': 3}))
            proc.end(0)
        self.scripts['edit'] = script
        self.job_id = None
        hold = threading.Event()
        self.svc._spawn = lambda fn, *a: threading.Thread(target=lambda: (hold.wait(5), fn(*a))).start()
        out = self.cmd('edit', op='delete', dataset='lena/omx_f_a', meta_digest=self.d, episodes=[3, 1, 3])
        self.job_id = out['result']['job_id']
        self.assertEqual(out['result']['outputs'], ['lena/omx_f_a'])
        self.refused(self.cmd('edit', op='delete', dataset='lena/omx_f_a', meta_digest=self.d, episodes=[0]),
                     'busy_edit', T.BUSY_EDIT_DE)
        hold.set()
        job = self.wait_job(self.job_id)
        self.assertEqual((job['state'], job['episodes'], job['code']), ('done', 3, ''))
        self.assertIsNone(self.svc.busy_kind(self.path))
        cmd = self.procs[0].cmd
        self.assertEqual(cmd[:3], ['nice', '-n', '19'])
        self.assertTrue(self.procs[0].kw['start_new_session'])

    def test_a_failed_edit_carries_its_german_message(self):
        self.scripts['edit'] = lambda proc, payload: (
            proc.emit(EW.RESULT_MARKER + json.dumps({'success': False, 'code': 'unaligned',
                                                     'message': T.UNALIGNED_DE})), proc.end(1))
        out = self.cmd('edit', op='delete', dataset='lena/omx_f_a', meta_digest=self.d, episodes=[0])
        job = self.wait_job(out['result']['job_id'])
        self.assertEqual((job['state'], job['code'], job['message']), ('failed', 'unaligned', T.UNALIGNED_DE))
        self.scripts['edit'] = lambda proc, payload: (
            proc.emit(EW.RESULT_MARKER + json.dumps({'success': False, 'code': 'weird', 'message': 'X'})),
            proc.end(1))
        job = self.wait_job(self.cmd('edit', op='delete', dataset='lena/omx_f_a', meta_digest=self.d,
                                     episodes=[0])['result']['job_id'])
        self.assertEqual(job['code'], 'internal')
        self.scripts['edit'] = lambda proc, payload: proc.end(1)               # died without a result
        job = self.wait_job(self.cmd('edit', op='delete', dataset='lena/omx_f_a', meta_digest=self.d,
                                     episodes=[0])['result']['job_id'])
        self.assertEqual((job['code'], job['message']), ('internal', T.RUN_EDIT_FAILED_DE))

    def test_the_timeout_kills_the_edit(self):
        self.svc.edit_timeout_s = 0.2
        self.scripts['edit'] = lambda proc, payload: None                       # never answers
        job = self.wait_job(self.cmd('edit', op='delete', dataset='lena/omx_f_a', meta_digest=self.d,
                                     episodes=[0])['result']['job_id'])
        self.assertEqual(job['code'], 'timeout')
        self.assertTrue(self.procs[0].killed)
        self.assertIsNone(self.svc.busy_kind(self.path))

    def test_the_old_dataset_edit_shares_the_slot_and_the_leases(self):
        self.node.on_recording = True
        self.node.data_manager = types.SimpleNamespace(_save_path=self.path)
        out = self.svc.run_edit_blocking({'mode': 'delete', 'delete_dataset_path': str(self.path),
                                          'delete_episode_num': [0]})
        self.assertEqual((out['success'], out['message']), (False, T.BUSY_RECORD_DE))
        self.node.on_recording = False
        self.scripts['edit'] = lambda proc, payload: (
            proc.emit(EW.RESULT_MARKER + json.dumps({'success': True, 'message': 'ok', 'code': ''})), proc.end())
        self.assertTrue(self.svc.run_edit_blocking({'mode': 'delete', 'delete_dataset_path': str(self.path),
                                                    'delete_episode_num': [0]})['success'])
        self.assertIsNone(self.svc._edit_slot)


class Leases(ServiceCase):

    def test_each_kind(self):
        path = self.dataset('lena/omx_f_a')
        self.assertIsNone(self.svc.busy_kind(path))
        self.node.data_manager = types.SimpleNamespace(_save_path=path)
        self.node.on_recording = True
        self.assertEqual(self.svc.busy_kind(path), 'record')
        self.assertIsNone(self.svc.claim_record_lease(path), 'the recorder never conflicts with itself')
        self.assertEqual(self.svc.state_payload()['busy'], [{'id': 'lena/omx_f_a', 'kind': 'record'}])
        self.hf.is_processing, self.hf.current_task = True, {'mode': 'upload', 'local_dir': str(path),
                                                              'repo_id': 'lena/omx_f_a'}
        self.assertEqual(self.svc.state_payload()['busy'], [{'id': 'lena/omx_f_a', 'kind': 'record'},
                                                            {'id': 'lena/omx_f_a', 'kind': 'upload'}],
                         'a Start waiting for this dataset\'s upload: the page reads the upload entry (§G11)')
        self.hf.is_processing, self.hf.current_task = False, None
        self.node.on_recording = False                                   # the record lease ends with the session
        self.assertIsNone(self.svc.busy_kind(path))
        self.hf.is_processing, self.hf.current_task = True, {'mode': 'upload', 'local_dir': str(path),
                                                              'repo_id': 'lena/omx_f_a'}
        self.assertEqual(self.svc.busy_kind(path), 'upload')
        self.assertEqual(self.svc.claim_record_lease(path), 'upload')
        self.assertEqual(self.svc.state_payload()['transfer'],
                         {'kind': 'upload', 'repo_id': 'lena/omx_f_a', 'target': 'lena/omx_f_a'})
        self.hf.current_task = {'mode': 'download', 'repo_type': 'dataset', 'repo_id': 'lena/omx_f_a'}
        self.assertEqual(self.svc.busy_kind(path), 'download')
        self.hf.is_processing = False
        for kind in ('edit', 'delete', 'download'):
            self.svc._leases[self.svc._key(path)] = kind
            self.assertEqual(self.svc.claim_record_lease(path), kind)
        self.svc._leases.clear()


class DeleteDataset(ServiceCase):

    def test_digest_rules_u4(self):
        path = self.dataset('lena/omx_f_a')
        self.refused(self.cmd('delete_dataset', dataset='lena/omx_f_a'), 'invalid')
        self.refused(self.cmd('delete_dataset', dataset='lena/omx_f_a', meta_digest='old'), 'stale',
                     T.STALE_ACTION_DE)
        self.assertTrue(path.is_dir(), 'nothing renamed')
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_the_whole_dataset_and_its_siblings_go(self):
        path = self.dataset('lena/omx_f_a')
        S.write_record(path, {'v': 1, 'repo_id': 'lena/omx_f_a'})
        S.session_marker_path(path).write_text('{}')
        self.svc._spawn = lambda fn, *a: fn(*a)
        out = self.cmd('delete_dataset', dataset='lena/omx_f_a', meta_digest=self.digest(path))
        job = self.job(out['result']['job_id'])
        self.assertEqual(job['state'], 'done')
        self.assertEqual(list(path.parent.iterdir()), [])

    def test_a_dataset_changed_after_the_callback_is_not_deleted(self):
        path = self.dataset('lena/omx_f_a')
        d = self.digest(path)
        held = []
        self.svc._spawn = lambda fn, *a: held.append((fn, a))
        out = self.cmd('delete_dataset', dataset='lena/omx_f_a', meta_digest=d)
        (path / 'meta' / 'stats.json').write_text('{}')                   # a session finished meanwhile
        fn, a = held[0]
        fn(*a)
        job = self.job(out['result']['job_id'])
        self.assertEqual((job['state'], job['code'], job['message']), ('failed', 'stale', T.STALE_ACTION_DE))
        self.assertTrue(path.is_dir())


class Uploads(ServiceCase):

    def test_validation_and_the_request(self):
        path = self.dataset('lena/omx_f_a')
        self.refused(self.cmd('upload', dataset='lena/omx_f_a', expected_hub_sha='zz'), 'invalid')
        self.states['omx_f_a'] = 'in_session'
        self.refused(self.cmd('upload', dataset='lena/omx_f_a'), 'in_session', R.UPLOAD_IN_SESSION_DE)
        self.states['omx_f_a'] = 'incomplete'
        self.refused(self.cmd('upload', dataset='lena/omx_f_a'), 'incomplete', R.UPLOAD_BROKEN_DE)
        self.states.clear()
        self.dataset('partner/omx_f_b')
        self.refused(self.cmd('upload', dataset='partner/omx_f_b'), 'namespace', R.NAMESPACE_REFUSED_DE)
        self.hf.busy = True
        self.refused(self.cmd('upload', dataset='lena/omx_f_a'), 'unavailable', T.UNAVAILABLE_DE)
        self.hf.busy = False
        seen = []
        self.hf.on_send = lambda req: seen.append(self.svc.busy_kind(path))
        out = self.cmd('upload', dataset='lena/omx_f_a', expected_hub_sha=None, private=True)
        self.assertEqual(out['result'], {'repo_id': 'lena/omx_f_a'})
        self.assertEqual(seen, ['upload'], 'the dataset is leased while the request is handed over')
        req = self.hf.sent[-1]
        self.assertEqual(req, {'mode': 'upload', 'repo_id': 'lena/omx_f_a', 'local_dir': str(path),
                               'repo_type': 'dataset', 'author': '', 'private': True, 'expected_hub_sha': None})
        self.hf.is_processing = False
        self.cmd('upload', dataset='lena/omx_f_a')
        self.assertNotIn('expected_hub_sha', self.hf.sent[-1], 'an absent key decides at upload time')
        self.assertEqual(self.svc._leases, {})


# ── downloads ─────────────────────────────────────────────────────────────────

class Downloads(ServiceCase):

    def start(self, **over):
        args = dict(repo_id='lehrer/omx_f_demo', revision=HEAD, target='lena/omx_f_demo', mode='new',
                    display_name='Demo')
        args.update(over)
        return self.cmd('download', **args)

    def test_validation(self):
        self.dataset('lena/omx_f_have')
        self.refused(self.start(revision='main'), 'invalid')
        self.refused(self.start(mode='sync'), 'invalid')
        self.refused(self.start(target='lena/omx_f_have', mode='replace'), 'invalid')     # no digest
        self.refused(self.start(target='lena/omx_f_have', mode='replace', meta_digest='old'), 'stale',
                     T.STALE_ACTION_DE)
        self.refused(self.start(target='lena/omx_f_none', mode='replace', meta_digest='x'), 'not_found')
        self.refused(self.start(target='lena/omx_f_have'), 'exists', T.DOWNLOAD_EXISTS_DE)
        self.refused(self.start(target='lena/omx_f_have', mode='copy'), 'exists', T.EXISTS_DE)
        self.token = None
        self.refused(self.start(), 'unavailable', R.HF_TOKEN_NONE_DE)

    def test_bytes_progress_and_success(self):
        out = self.start()
        self.assertTrue(out['success'])
        job_id = out['result']['job_id']
        self.assertTrue(wait_for(lambda: self.procs and self.procs[0].request))
        proc = self.procs[0]
        self.assertEqual(proc.cmd, ['nice', '-n', '10', sys.executable, '-m', NS.DOWNLOAD_WORKER_MODULE])
        self.assertEqual(proc.kw['env']['HF_HUB_DISABLE_XET'], '1')
        self.assertTrue(proc.kw['start_new_session'])
        req = json.loads(proc.request)
        self.assertEqual((req['mode'], req['revision'], req['token_fp']), ('new', HEAD, STORE.fingerprint(TOKEN_A)))
        self.assertEqual(req['target_dir'], str(self.root / 'lena' / 'omx_f_demo'))
        self.assertEqual(self.svc.busy_kind(self.root / 'lena/omx_f_demo'), 'download')
        self.refused(self.start(target='lena/omx_f_other'), 'busy_download', T.BUSY_DOWNLOAD_DE)
        proc.emit('DL_PROGRESS::' + json.dumps({'stage': 'download', 'total': 4000}))
        tmp = self.root / 'lena' / 'omx_f_demo.tmp_sync'
        (tmp / 'data').mkdir(parents=True)
        (tmp / 'data' / 'a').write_bytes(b'x' * 1500)
        self.assertTrue(wait_for(lambda: self.job(job_id)['done'] == 1500 and self.job(job_id)['total'] == 4000))
        self.assertEqual((self.job(job_id)['unit'], self.job(job_id)['outputs']), ('bytes', ['lena/omx_f_demo']))
        proc.emit('DL_RESULT::' + json.dumps({'ok': True, 'revision': HEAD}))
        proc.end(0)
        job = self.wait_job(job_id)
        self.assertEqual(job['state'], 'done')
        self.assertIsNone(self.svc.busy_kind(self.root / 'lena/omx_f_demo'))

    def test_a_token_change_kills_within_two_polls_and_recovers_before_the_tmp_goes(self):
        polls = []
        real = self.svc._slot_fp
        self.svc._slot_fp = lambda: (polls.append(1), real())[1]
        recovered = []
        real_recover = HS.recover
        HS.recover = lambda target: (recovered.append(pathlib.Path(f'{target}.tmp_sync').exists()),
                                     real_recover(target))[1]
        self.addCleanup(setattr, HS, 'recover', real_recover)
        job_id = self.start()['result']['job_id']
        self.assertTrue(wait_for(lambda: self.procs and self.procs[0].request))
        tmp = self.root / 'lena' / 'omx_f_demo.tmp_sync'
        tmp.mkdir(parents=True)
        (tmp / 'x').write_bytes(b'1')
        self.assertTrue(wait_for(lambda: len(polls) >= 3))
        n = len(polls)
        self.token = TOKEN_B
        job = self.wait_job(job_id)
        self.assertLessEqual(len(polls) - n, 2)
        self.assertTrue(self.procs[0].killed)
        self.assertEqual((job['code'], job['message']), ('token_changed', T.DOWNLOAD_TOKEN_CHANGED_DE))
        self.assertFalse(tmp.exists())
        self.assertEqual(recovered, [True], 'recover ran while the tmp still told the swap state (U-1)')

    def test_a_result_behind_an_unfinished_progress_bar_is_still_the_result(self):
        """V1-1: the worker's own watch printed its result while
        snapshot_download's bar (stderr, merged into the same pipe, no newline)
        stood unfinished: the line read ``<bar>DL_RESULT::{...}``. It is the
        result, not a worker that ended without one (``internal``)."""
        bar = 'Fetching 9 files:  22%|██▏       | 2/9 [00:01<00:04,  1.60it/s]'
        job_id = self.start()['result']['job_id']
        self.assertTrue(wait_for(lambda: self.procs and self.procs[0].request))
        proc = self.procs[0]
        proc.emit(bar + 'DL_PROGRESS::' + json.dumps({'stage': 'download', 'total': 4000}))
        self.assertTrue(wait_for(lambda: self.job(job_id)['total'] == 4000))
        proc.emit(bar + 'DL_RESULT::' + json.dumps({'ok': False, 'code': 'token_changed'}))
        proc.end(3)
        job = self.wait_job(job_id)
        self.assertEqual((job['state'], job['code'], job['message']),
                         ('failed', 'token_changed', T.DOWNLOAD_TOKEN_CHANGED_DE))

    def test_parse_marked(self):
        r = 'DL_RESULT::'
        ok = {'ok': False, 'code': 'token_changed'}
        cases = [('DL_RESULT::' + json.dumps(ok), ok),
                 ('Fetching 9 files:  22%|██▏ | 2/9 [...]DL_RESULT::' + json.dumps(ok) + '\n', ok),
                 ('DL_RESULT::' + json.dumps(ok) + ' 22%|██▏ | 2/9', ok),            # anything after the object
                 ('DL_RESULT::{"code": "a"}DL_RESULT::{"code": "b"}', {'code': 'b'}),   # the last marker
                 ('DL_RESULT::{"ok": tru', None), ('DL_RESULT::[1, 2]', None), ('DL_RESULT:: {}', None),
                 ('DL_PROGRESS::{"total": 1}', None), ('', None), ('Fetching 9 files', None)]
        for line, want in cases:
            with self.subTest(line=line):
                self.assertEqual(NS.parse_marked(line, r), want)

    def test_the_worker_writes_each_protocol_line_on_its_own_line_in_one_write(self):
        dw = _load('_daten_download_worker_under_test', PKG / 'daten' / 'download_worker.py')
        self.assertEqual((dw.RESULT_PREFIX, dw.PROGRESS_PREFIX), (NS.DL_RESULT, NS.DL_PROGRESS))
        writes = []

        class Out:
            def write(self, text):
                writes.append(text)

            def flush(self):
                writes.append(None)
        saved = sys.stdout
        sys.stdout = Out()
        try:
            dw._say(dw.RESULT_PREFIX, {'ok': False, 'code': 'token_changed'})
        finally:
            sys.stdout = saved
        self.assertEqual(writes, ['\nDL_RESULT::{"ok": false, "code": "token_changed"}\n', None])

    def test_a_stall_and_a_cancel(self):
        self.svc.download_stall_s = 0.15
        job = self.wait_job(self.start()['result']['job_id'])
        self.assertEqual((job['code'], job['message']), ('stalled', R.DOWNLOAD_STALL_DE))
        self.svc.download_stall_s = 60
        job_id = self.start()['result']['job_id']
        self.assertTrue(wait_for(lambda: len(self.procs) == 2 and self.procs[1].request))
        self.assertTrue(self.cmd('cancel', what='download')['success'])
        job = self.wait_job(job_id)
        self.assertEqual(job['code'], 'cancelled')
        self.assertTrue(self.procs[1].killed)

    def test_each_worker_failure_is_its_sentence(self):
        cases = [({'code': 'disk', 'free': 2_000_000_000, 'need': 7_000_000_000}, 'disk',
                  T.download_disk_de(2_000_000_000, 7_000_000_000)),
                 ({'code': 'other_robot'}, 'other_robot', T.DOWNLOAD_OTHER_ROBOT_DE),
                 ({'code': 'broken'}, 'broken', T.DOWNLOAD_BROKEN_DE),
                 ({'code': 'not_found'}, 'not_found', T.DOWNLOAD_NOT_FOUND_DE),
                 ({'code': 'stale'}, 'stale', T.STALE_ACTION_DE),
                 ({'code': 'auth'}, 'auth', R.HF_AUTH_ERROR_DE),
                 ({'code': 'unreachable'}, 'unreachable', R.HF_NETWORK_ERROR_DE),
                 ({'code': 'weird'}, 'internal', T.DOWNLOAD_FAILED_DE)]
        for result, code, message in cases:
            with self.subTest(code=result['code']):
                self.assertEqual(self.svc._download_failure(dict(result, ok=False)), (code, message))


class DatenResolvesTheAccount(ServiceCase):
    """V2-16: the ``namespace`` refusals of ``keep_both`` and ``upload`` no
    longer depend on a cache only the recorder fills: Daten looks the slot
    token's account up itself, in the background, once per fingerprint."""

    def service(self, resolver, clock=time.monotonic):
        svc = NS.DatenService(
            self.node, root=self.root, ros=False, start_threads=False, popen=self.popen,
            kill=lambda proc: proc.kill(), token_reader=lambda: self.token,
            state_reader=lambda p: self.states.get(pathlib.Path(p).name, 'ok'),
            name_rule=lambda n: n.replace(' ', '-'), account_resolver=resolver,
            disk_free=lambda p: self.free, secret=b's' * 32, clock=clock)
        svc._lock = self.lock
        return svc

    def keep_both(self, svc, dataset_id):
        path = self.root / dataset_id
        return svc.command('keep_both', json.dumps({'dataset': dataset_id, 'expected_hub_sha': HEAD,
                                                    'meta_digest': S.meta_digest(path)}))

    def test_a_partners_keep_both_and_upload_are_refused_namespace_at_once(self):
        calls = []
        svc = self.service(lambda: calls.append(1) or ['lena'])
        self.dataset('max/omx_f_x')
        self.dataset('lena/omx_f_y')
        svc._refresh_account()                                  # the 1 Hz tick
        self.assertTrue(wait_for(lambda: svc._account() == 'lena'))
        self.refused(self.keep_both(svc, 'max/omx_f_x'), 'namespace', R.NAMESPACE_REFUSED_DE)
        self.refused(svc.command('upload', json.dumps({'dataset': 'max/omx_f_x'})), 'namespace',
                     R.NAMESPACE_REFUSED_DE)
        self.assertEqual((self.procs, self.hf.sent, svc.state_payload()['jobs']), ([], [], []))
        self.assertTrue(self.keep_both(svc, 'lena/omx_f_y')['success'], 'the own dataset still goes')
        self.assertEqual(len(calls), 1, 'one lookup per token')

    def test_the_lookup_never_runs_inside_a_command(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        svc = self.service(lambda: gate.wait(10) and ['lena'])
        self.dataset('max/omx_f_x')
        t0 = time.monotonic()
        out = self.keep_both(svc, 'max/omx_f_x')                 # unknown yet: refused on proof only
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertTrue(out['success'], out)
        gate.set()
        self.assertTrue(wait_for(lambda: svc._account() == 'lena'))

    def test_an_answer_for_a_token_that_changed_meanwhile_is_dropped(self):
        def resolver():
            self.token = TOKEN_B                                 # the student changed during the whoami
            return ['lena']
        svc = self.service(resolver)
        svc._refresh_account()
        self.assertTrue(wait_for(lambda: not svc._resolved['running']))
        self.assertIsNone(svc._resolved['account'])
        svc._account_resolver = lambda: ['max']
        self.assertTrue(wait_for(lambda: svc._account() == 'max'))

    def test_a_failed_lookup_is_asked_again_after_the_retry_time(self):
        now = [100.0]
        calls = []

        def resolver():
            calls.append(1)
            raise RuntimeError('hub unreachable')
        svc = self.service(resolver, clock=lambda: now[0])
        svc._refresh_account()
        self.assertTrue(wait_for(lambda: len(calls) == 1 and not svc._resolved['running']))
        self.assertIsNone(svc._account())
        self.assertEqual(len(calls), 1, 'not again within the retry time')
        now[0] += NS.ACCOUNT_RETRY_S + 1
        svc._refresh_account()
        self.assertTrue(wait_for(lambda: len(calls) == 2))


class TheOldPageUpload(ServiceCase):
    """V1-6: ``/huggingface/control``'s upload goes through ``send_control_upload``:
    the busy check and the transient ``upload`` lease of a Daten upload."""

    def request(self, path):
        return {'mode': 'upload', 'repo_id': 'lena/omx_f_a', 'local_dir': str(path), 'repo_type': 'dataset',
                'author': ''}

    def test_refused_while_an_edit_a_delete_or_a_download_holds_the_dataset(self):
        path = self.dataset('lena/omx_f_a')
        key = self.svc._key(path)
        for kind, message in (('edit', T.BUSY_EDIT_DE), ('delete', T.BUSY_EDIT_DE),
                              ('download', T.BUSY_DOWNLOAD_DE)):
            with self.subTest(kind=kind):
                self.svc._claim([key], kind)
                try:
                    self.assertEqual(self.svc.send_control_upload(str(path), self.request(path)), message)
                finally:
                    self.svc._release([key])
                self.assertEqual(self.hf.sent, [])

    def test_refused_while_a_recording_writes_it(self):
        path = self.dataset('lena/omx_f_a')
        self.node.on_recording = True
        self.node.data_manager = types.SimpleNamespace(_save_path=path)
        self.assertEqual(self.svc.send_control_upload(str(path), self.request(path)), T.BUSY_RECORD_DE)
        self.assertEqual(self.hf.sent, [])

    def test_a_free_dataset_is_handed_over_under_the_upload_lease(self):
        path = self.dataset('lena/omx_f_a')
        key = self.svc._key(path)
        seen = []
        self.hf.on_send = lambda request: seen.append(self.svc._leases.get(key))
        self.assertIsNone(self.svc.send_control_upload(str(path), self.request(path)))
        self.assertEqual((self.hf.sent, seen), ([self.request(path)], ['upload']))
        self.assertEqual(self.svc.busy_kind(path), 'upload', 'from now on the worker task holds it')
        self.refused(self.cmd('edit', op='delete', dataset='lena/omx_f_a', meta_digest=self.digest(path),
                              episodes=[0]), 'busy_upload', T.BUSY_UPLOAD_DE)

    def test_a_worker_that_cannot_take_it_is_unavailable(self):
        path = self.dataset('lena/omx_f_a')
        self.hf.accept = False
        self.assertEqual(self.svc.send_control_upload(str(path), self.request(path)), T.UNAVAILABLE_DE)
        self.assertIsNone(self.svc.busy_kind(path), 'the transient lease is released')


class SyncDownloads(ServiceCase):

    def test_queued_behind_a_running_download_and_shown_while_recording(self):
        path = self.dataset('lena/omx_f_rec')
        self.node.data_manager = types.SimpleNamespace(_save_path=path)
        self.node.on_recording = True
        page = self.cmd('download', repo_id='lehrer/omx_f_demo', revision=HEAD, target='lena/omx_f_demo',
                        mode='new')['result']['job_id']
        self.assertTrue(wait_for(lambda: self.procs and self.procs[0].request))
        handle = self.svc.start_sync_download('lena/omx_f_rec', None, str(path), STORE.fingerprint(TOKEN_A))
        self.assertIsNone(handle.poll())
        self.assertEqual(len(self.procs), 1, 'queued, not started')
        sync = self.job(handle.job_id)
        self.assertEqual((sync['op'], sync['datasets'], sync['outputs'], sync['unit']),
                         ('download', ['lena/omx_f_rec'], ['lena/omx_f_rec'], 'bytes'))
        self.assertEqual(self.svc.busy_kind(path), 'record', 'the recorder\'s lease covers it')
        self.procs[0].emit('DL_RESULT::' + json.dumps({'ok': True, 'revision': HEAD}))
        self.procs[0].end()
        self.wait_job(page)
        self.assertTrue(wait_for(lambda: len(self.procs) == 2 and self.procs[1].request))
        req = json.loads(self.procs[1].request)
        self.assertEqual((req['mode'], req['revision'], req['target_dir']), ('sync', None, str(path)))
        self.procs[1].emit('DL_RESULT::' + json.dumps({'ok': True, 'revision': BASE}))
        self.procs[1].end()
        self.assertTrue(wait_for(lambda: handle.poll() is not None))
        self.assertEqual(handle.poll(), {'ok': True, 'revision': BASE})

    def test_cancelled_by_its_handle(self):
        path = self.dataset('lena/omx_f_rec')
        self.cmd('download', repo_id='lehrer/omx_f_demo', revision=HEAD, target='lena/omx_f_demo', mode='new')
        queued = self.svc.start_sync_download('lena/omx_f_rec', HEAD, str(path), 'fp')
        queued.cancel()
        self.assertEqual(queued.poll(), {'ok': False, 'code': 'cancelled'})
        self.procs[0].end()
        self.assertTrue(wait_for(lambda: self.svc._download_slot is None))
        self.assertEqual(len(self.procs), 1, 'a cancelled queued download never starts')
        running = self.svc.start_sync_download('lena/omx_f_rec', HEAD, str(path), STORE.fingerprint(TOKEN_A))
        self.assertTrue(wait_for(lambda: len(self.procs) == 2 and self.procs[1].request))
        running.cancel()
        self.assertTrue(wait_for(lambda: running.poll() is not None))
        self.assertEqual(running.poll()['code'], 'cancelled')
        self.assertTrue(self.procs[1].killed)


# ── keep_both ─────────────────────────────────────────────────────────────────

class KeepBoth(ServiceCase):

    def setUp(self):
        super().setUp()
        self.path = self.dataset('lena/omx_f_a')
        S.write_record(self.path, {'v': 1, 'repo_id': 'lena/omx_f_a', 'hub_sha': BASE, 'private': True})
        self.d = self.digest(self.path)
        self.stage_locks = []

        def download(proc, payload):
            req = json.loads(payload)
            fd = HS.stage_lock(req['target_dir'])          # the stage's own process takes the lock (G-3)
            self.stage_locks.append(req['mode'])
            HS.release_lock(fd)
            HS.tmp_path_of(req['target_dir'], req['mode']).mkdir(parents=True, exist_ok=True)
            proc.emit('DL_RESULT::' + json.dumps({'ok': True, 'revision': req['revision'],
                                                  'trees': {'data': 't'}, 'no_base': False}))
            proc.end()

        def union(proc, payload):
            req = json.loads(payload)
            fd = HS.stage_lock(req['dataset_path'])
            self.stage_locks.append(req['mode'])
            HS.release_lock(fd)
            self.union_req = req
            proc.emit(EW.PROGRESS_MARKER + json.dumps({'stage': 'verify', 'done': 1, 'total': 1}))
            proc.emit(EW.RESULT_MARKER + json.dumps({'success': True, 'code': '', 'episodes': 9}))
            proc.end()
        self.scripts['download'] = download
        self.scripts['edit'] = union

    def start(self, **over):
        args = dict(dataset='lena/omx_f_a', expected_hub_sha=HEAD, meta_digest=self.d)
        args.update(over)
        return self.cmd('keep_both', **args)

    def test_validation(self):
        self.refused(self.start(expected_hub_sha='main'), 'invalid')
        self.refused(self.start(meta_digest='old'), 'stale', T.STALE_DE)
        self.states['omx_f_a'] = 'in_session'
        self.refused(self.start(), 'in_session', T.IN_SESSION_DE)
        self.states.clear()
        self.dataset('partner/omx_f_b')
        self.refused(self.start(dataset='partner/omx_f_b', meta_digest=self.digest(self.root / 'partner/omx_f_b')),
                     'namespace', R.NAMESPACE_REFUSED_DE)
        self.free = 1_000_000_000 + 150
        self.refused(self.start(), 'disk')
        self.free = 10 ** 12
        self.token = None
        self.refused(self.start(), 'unavailable', R.HF_TOKEN_NONE_DE)
        self.token = TOKEN_A
        self.hf.is_processing = True
        self.refused(self.start(), 'unavailable', T.UNAVAILABLE_DE)
        self.assertEqual(self.procs, [], 'nothing fetched')

    def test_three_stages_and_the_edit_to_upload_hand_over(self):
        kinds = []
        self.hf.on_send = lambda req: kinds.append(self.svc.busy_kind(self.path))
        job_id = self.start()['result']['job_id']
        self.assertTrue(wait_for(lambda: self.hf.sent))
        self.assertEqual(kinds, ['edit'], 'the edit lease stands while the upload is enqueued')
        self.assertTrue(wait_for(lambda: self.svc._leases == {}))
        self.assertEqual(self.svc.busy_kind(self.path), 'upload', 'then the upload holds it: no gap')
        self.assertEqual(self.stage_locks, ['keep', 'base', 'union'])
        self.assertEqual(self.union_req, {'mode': 'union', 'dataset_path': str(self.path),
                                          'hub_copy_path': str(HS.tmp_path_of(self.path, 'keep')),
                                          'base_copy_path': str(HS.tmp_path_of(self.path, 'base')),
                                          'hub_sha': HEAD, 'hub_trees': {'data': 't'}})
        self.assertEqual(self.hf.sent[0]['expected_hub_sha'], HEAD)
        self.assertEqual(self.job(job_id)['stage'], 'upload')
        self.svc.on_hf_status({'operation': 'upload', 'status': 'Success', 'repo_id': 'lena/omx_f_a'})
        job = self.wait_job(job_id)
        self.assertEqual((job['state'], job['episodes']), ('done', 9))
        self.assertFalse(HS.tmp_path_of(self.path, 'keep').exists())

    def test_no_base_download_when_the_record_is_the_head(self):
        S.write_record(self.path, {'v': 1, 'repo_id': 'lena/omx_f_a', 'hub_sha': HEAD})
        job_id = self.start(meta_digest=self.digest(self.path))['result']['job_id']
        self.assertTrue(wait_for(lambda: self.hf.sent))
        self.assertEqual(self.stage_locks, ['keep', 'union'])
        self.assertIsNone(self.union_req['base_copy_path'])
        self.svc.on_hf_status({'operation': 'upload', 'status': 'Success', 'repo_id': 'lena/omx_f_a'})
        self.wait_job(job_id)

    def test_hub_changed_keeps_the_result_locally(self):
        job_id = self.start()['result']['job_id']
        self.assertTrue(wait_for(lambda: self.hf.sent))
        self.svc.on_hf_status({'operation': 'upload', 'status': 'Failed', 'repo_id': 'lena/omx_f_a',
                               'upload_code': 'hub_changed', 'message': R.HUB_CHANGED_SINCE_CHECK_DE})
        job = self.wait_job(job_id)
        self.assertEqual((job['code'], job['message'], job['episodes']),
                         ('hub_changed', R.HUB_CHANGED_SINCE_CHECK_DE, 9))
        self.assertTrue(self.path.is_dir())

    def test_a_stage_3_that_cannot_be_enqueued_is_unavailable(self):
        self.hf.accept = False
        job = self.wait_job(self.start()['result']['job_id'])
        self.assertEqual((job['code'], job['message']), ('unavailable', T.UNAVAILABLE_DE))
        self.assertIsNone(self.svc.busy_kind(self.path))

    def test_a_crashed_local_copy_fails_before_anything_is_fetched(self):
        self.scripts['download'] = lambda proc, payload: (
            proc.emit('DL_RESULT::' + json.dumps({'ok': False, 'code': 'in_session'})), proc.end(1))
        job = self.wait_job(self.start()['result']['job_id'])
        self.assertEqual((job['code'], job['message']), ('broken', R.UPLOAD_IN_SESSION_DE))
        self.assertEqual(self.hf.sent, [])


# ── boot recovery ─────────────────────────────────────────────────────────────

class BootRecovery(ServiceCase):

    def tree(self, path, tag):
        (path / 'meta').mkdir(parents=True, exist_ok=True)
        (path / 'meta' / 'info.json').write_text(tag)
        return path

    def test_every_rule(self):
        ns = self.root / 'lena'
        # split, rolled BACK (the new output never promoted)
        src, new = ns / 'omx_f_s', ns / 'omx_f_n'
        self.tree(src, 'rest')
        self.tree(pathlib.Path(f'{src}.bak_edit'), 'original')
        self.tree(pathlib.Path(f'{new}.tmp_edit'), 'moved')
        S.write_json_atomic(S.journal_path(src), {'op': 'split', 'path': str(src), 'new': str(new),
                                                  'records': {'path': {'v': 1, 'repo_id': 'lena/omx_f_s'},
                                                              'new': {'v': 1, 'repo_id': 'lena/omx_f_n'}}})
        # split, rolled FORWARD (both promoted)
        src2, new2 = ns / 'omx_f_s2', ns / 'omx_f_n2'
        self.tree(src2, 'rest2')
        self.tree(new2, 'moved2')
        self.tree(pathlib.Path(f'{src2}.bak_edit'), 'original2')
        S.write_json_atomic(S.journal_path(src2), {'op': 'split', 'path': str(src2), 'new': str(new2),
                                                   'records': {'path': {'v': 1, 'repo_id': 'lena/omx_f_s2',
                                                                        'display_name': 'R'},
                                                               'new': {'v': 1, 'repo_id': 'lena/omx_f_n2',
                                                                       'display_name': 'N'}}})
        # tmp leftovers of keep_both / downloads / a trash
        k = self.tree(ns / 'omx_f_k', 'k')
        for suf in ('.tmp_keep', '.tmp_base', '.tmp_sync', '.tmp_edit'):
            self.tree(pathlib.Path(f'{k}{suf}'), suf)
        # a swap that broke between its renames
        b = ns / 'omx_f_b'
        self.tree(pathlib.Path(f'{b}.bak_sync'), 'old-b')
        S.write_json_atomic(S.record_next_path(b), {'record': {'v': 1, 'repo_id': 'lena/omx_f_b'},
                                                    'tmp': '.tmp_sync', 'bak': '.bak_sync'})
        # a swap whose second rename happened
        p = self.tree(ns / 'omx_f_p', 'new-p')
        S.session_marker_path(p).write_text('{}')
        S.write_json_atomic(S.record_next_path(p), {'record': {'v': 1, 'repo_id': 'lena/omx_f_p', 'hub_sha': HEAD},
                                                    'tmp': '.tmp_sync', 'bak': '.bak_sync'})
        # a delete cut short
        d = ns / 'omx_f_d'
        self.tree(pathlib.Path(f'{d}.trash_edit'), 'trash')
        S.write_record(d, {'v': 1, 'repo_id': 'lena/omx_f_d'})
        # a held lock is skipped
        h = self.tree(ns / 'omx_f_h', 'h')
        self.tree(pathlib.Path(f'{h}.tmp_sync'), 'busy')
        held = os.open(S.lock_path(h), os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, held)

        done = dict(self.svc.recover_all())
        self.assertEqual((src / 'meta/info.json').read_text(), 'original')
        self.assertFalse(pathlib.Path(f'{new}.tmp_edit').exists())
        self.assertFalse(new.exists())
        self.assertEqual(done['lena/omx_f_s'], 'back')
        self.assertEqual(done['lena/omx_f_s2'], 'forward')
        self.assertEqual(S.read_record(new2)['display_name'], 'N')
        self.assertFalse(pathlib.Path(f'{src2}.bak_edit').exists())
        for suf in ('.tmp_keep', '.tmp_base', '.tmp_sync', '.tmp_edit'):
            self.assertFalse(pathlib.Path(f'{k}{suf}').exists(), suf)
        self.assertEqual((b / 'meta/info.json').read_text(), 'old-b')
        self.assertFalse(S.record_next_path(b).exists())
        self.assertEqual(S.read_record(p)['hub_sha'], HEAD)
        self.assertFalse(S.session_marker_path(p).exists())
        self.assertFalse(pathlib.Path(f'{d}.trash_edit').exists())
        self.assertFalse(S.record_path(d).exists())
        self.assertTrue(pathlib.Path(f'{h}.tmp_sync').exists(), 'a held lock is skipped')
        self.assertNotIn('lena/omx_f_h', done)
        for journal in (S.journal_path(src), S.journal_path(src2)):
            self.assertFalse(journal.exists())

    def test_recovery_holds_an_edit_lease_and_skips_a_leased_dataset(self):
        ns = self.root / 'lena'
        x = self.tree(ns / 'omx_f_x', 'x')
        self.tree(pathlib.Path(f'{x}.tmp_sync'), 'tmp')
        y = self.tree(ns / 'omx_f_y', 'y')
        self.tree(pathlib.Path(f'{y}.tmp_sync'), 'tmp')
        self.svc._leases[self.svc._key(y)] = 'download'
        seen = []
        real = HS.recover

        def spy(target):
            seen.append((pathlib.Path(target).name, self.svc.busy_kind(target), self.svc.claim_record_lease(target)))
            return real(target)
        HS.recover = spy
        self.addCleanup(setattr, HS, 'recover', real)
        self.svc.recover_all()
        self.assertEqual(seen, [('omx_f_x', 'edit', 'edit')], 'a Start meanwhile is refused (U-5)')
        self.assertTrue(pathlib.Path(f'{y}.tmp_sync').exists())
        self.assertIsNone(self.svc.busy_kind(x))


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The node side of Daten 2.0: ``DatenService`` (spec §D3, §J.3, §J.5).

Constructed in ``PhysicalAIServer.__init__`` right after ``_init_ros_service``
(a failure leaves ``node.daten = None``: the node lives on without Daten). It
owns:

* ``/daten/command`` (``DatenCommand.srv``, its own MutuallyExclusive group):
  ``link`` | ``edit`` | ``delete_dataset`` | ``upload`` | ``download`` |
  ``keep_both`` | ``cancel`` | ``state``. Every action is local and fast — the
  callbacks make no network call; network work runs in the HF worker or the
  download worker, edits in the edit worker, each its own process;
* ``/edubotics/daten_state`` (``std_msgs/String`` JSON, RELIABLE +
  TRANSIENT_LOCAL depth 1, 1 Hz from its own group and at once on every job
  change);
* the LEASE registry: one ``threading.Lock`` held only around in-memory reads
  and dict writes, never across a file-system call or a call into a worker
  (R-18). ``busy_kind(path)``: ``record`` (the node records into it — the
  recorder's lease IS that state, so no session-end path needs a hook),
  ``upload``/``download`` (the HF worker's task), ``edit``/``delete``/
  ``download`` (a Daten job). The recorder's ``_dataset_lease`` is
  ``claim_record_lease``; its sync download is ``start_sync_download``;
* the edit job runner (one edit-class job at a time: edit, delete_dataset,
  keep_both, and the old ``/dataset/edit`` through ``run_edit_blocking``), the
  download job runner (one download at a time; the recorder's sync downloads
  queue behind a running one), the per-dataset lock files taken by each
  stage's own process (never by a job across stages, G-3), and boot recovery.
"""

from __future__ import annotations

import collections
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid


def _mod(package, name):
    """A module of this package: by name in the image, by file path for the
    deps-free loaders (whose package stub has no ``__path__``)."""
    dotted = f'physical_ai_server.{package}.{name}' if package else f'physical_ai_server.{name}'
    try:
        return importlib.import_module(dotted)
    except ImportError:
        key = f'_edubotics_ns_{package or "pkg"}_{name}'
        module = sys.modules.get(key)
        if module is None:
            base = Path(__file__).resolve().parent.parent
            path = (base / package / f'{name}.py') if package else (base / f'{name}.py')
            spec = importlib.util.spec_from_file_location(key, str(path))
            module = importlib.util.module_from_spec(spec)
            sys.modules[key] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(key, None)
                raise
        return module


C = _mod('daten', 'contract')
LT = _mod('daten', 'link_tokens')
T = _mod('daten', 'texts_de')
R = _mod('data_processing', 'record_texts_de')
S = _mod('data_processing', 'dataset_sync')
HS = _mod('data_processing', 'hub_sync')
DP = _mod('data_processing', 'dataset_paths')
SIG = _mod(None, 'signal_status')

_PART = re.compile(C.DATASET_PART_RE)
_REPO = re.compile(C.REPO_ID_RE)
_SHA = re.compile(r'^[0-9a-f]{40}$')
DOWNLOAD_WORKER_MODULE = 'physical_ai_server.daten.download_worker'
DL_RESULT = 'DL_RESULT::'             # = download_worker.RESULT_PREFIX / PROGRESS_PREFIX
DL_PROGRESS = 'DL_PROGRESS::'
UPLOAD_STATUS_GRACE_S = 3.0          # the HF worker idle with no status for this long: the upload is lost
ACCOUNT_RETRY_S = 30.0               # a failed account lookup is asked again after this long

# (code, message) of each busy kind
_BUSY = {'record': ('busy_record', T.BUSY_RECORD_DE), 'upload': ('busy_upload', T.BUSY_UPLOAD_DE),
         'download': ('busy_download', T.BUSY_DOWNLOAD_DE), 'edit': ('busy_edit', T.BUSY_EDIT_DE),
         'delete': ('busy_edit', T.BUSY_EDIT_DE)}
# a dataset state that refuses an edit-class action
_STATE_REFUSAL = {'in_session': T.IN_SESSION_DE, 'old_format': T.OLD_FORMAT_DE,
                  'unsupported': T.UNSUPPORTED_DE, 'incomplete': T.INCOMPLETE_DE}


class Refusal(Exception):
    def __init__(self, code, message):
        super().__init__(code)
        self.code = code
        self.message = message


def _invalid(message=T.UNKNOWN_MODE_DE):
    return Refusal('invalid', message)


def valid_id(dataset_id) -> bool:
    if not isinstance(dataset_id, str) or dataset_id.count('/') != 1:
        return False
    return all(_PART.fullmatch(p) and not p.endswith(C.RESERVED_SUFFIXES) for p in dataset_id.split('/'))


def _now_iso():
    return S.now_iso()


def parse_marked(line, marker):
    """The JSON object after the LAST ``marker`` in one output line, else None.

    The worker's stderr shares the pipe, and the hub library's progress bar
    (``\\r`` + text, never a newline while it runs) can stand unfinished on the
    line the worker's own watch prints its result into: ``Fetching 9 files:
    22%|██▏ | 2/9 [...]DL_RESULT::{...}`` (V1-1). So the marker counts wherever
    it stands, and anything after the object is ignored."""
    i = line.rfind(marker)
    if i < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(line, i + len(marker))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _tree_bytes(path):
    n = 0
    for dp, _, files in os.walk(path):
        for f in files:
            try:
                n += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return n


def _default_account_resolver():
    """The slot token's account (``DataManager.get_huggingface_user_id``: one
    whoami bounded at 8 s, the token read from the slot); None when it cannot
    be asked here (the deps-free loaders)."""
    try:
        from physical_ai_server.data_processing.data_manager import DataManager
    except Exception:  # noqa: BLE001
        return None
    return DataManager.get_huggingface_user_id()


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


class SyncHandle:
    """The recorder's handle on its sync download (§C4): ``poll()`` → None
    while queued or running, else the result dict ``{'ok', 'revision'|'code',
    'free'?, 'need'?}``; ``cancel()``."""

    def __init__(self, service, job_id):
        self._service = service
        self.job_id = job_id

    def poll(self):
        return self._service._job_result(self.job_id)

    def cancel(self):
        self._service._cancel_job(self.job_id)


class DownloadProcess:
    """One download worker process and its supervisor rules (§E3): token change
    → kill; no progress (bytes in the tmp, or the check's heartbeat) for
    ``stall_s`` → kill; overall ``timeout_s``; every kill recovers the dataset
    BEFORE the tmp is removed (U-1)."""

    def __init__(self, service, request, on_progress=None):
        self.svc = service
        self.req = request
        self.tmp = HS.tmp_path_of(request['target_dir'], request['mode'])
        self.on_progress = on_progress
        self.result = None
        self.total = 0
        self.verify_done = 0
        cmd = ['nice', '-n', '10', sys.executable, '-m', DOWNLOAD_WORKER_MODULE]
        self.p = service._popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, start_new_session=True,
                                env={**os.environ, 'HF_HUB_DISABLE_XET': '1'})
        try:
            self.p.stdin.write(json.dumps(request) + '\n')
            self.p.stdin.flush()
            self.p.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass
        self._reader = threading.Thread(target=self._read, daemon=True, name='daten-download-reader')
        self._reader.start()

    def _read(self):
        try:
            for line in self.p.stdout:
                result = parse_marked(line, DL_RESULT)
                if result is not None:
                    self.result = result
                    continue
                p = parse_marked(line, DL_PROGRESS)
                if p is None:
                    continue
                if 'total' in p:
                    self.total = int(p.get('total') or 0)
                if 'done' in p:
                    self.verify_done = int(p.get('done') or 0)
        except (OSError, ValueError, TypeError):
            pass

    def kill(self, code):
        """Kill the worker's process group and wait; then, under the dataset's
        lock, recover the dataset — while the tmp still tells the swap's state
        (U-1) — and only then remove the tmp (the no-lock fallback)."""
        self.svc._kill(self.p)
        try:
            self.p.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass
        try:
            fd = HS.stage_lock(self.req['target_dir'])
        except (BlockingIOError, OSError):
            fd = None                                        # another stage owns it: boot recovery later
        if fd is not None:
            try:
                HS.recover(self.req['target_dir'])
            except Exception as e:  # noqa: BLE001
                self.svc._log(f'recover after a killed download failed: {e!r}')
            finally:
                HS.release_lock(fd)
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.result = {'ok': False, 'code': code}
        return self.result

    def run(self, cancel_event):
        """Supervise until the worker ends; returns its result dict."""
        t0 = last_growth = self.svc._clock()
        last_bytes, last_verify, last_total = -1, -1, -1
        while True:
            if self.p.poll() is not None:
                self._reader.join(2)
                result = self.result or {'ok': False, 'code': 'internal'}
                if result.get('ok') and self.on_progress and self.total:
                    self.on_progress(self.total, self.total)         # done: the job shows it whole
                return result
            if cancel_event.is_set():
                return self.kill('cancelled')
            if self.svc._slot_fp() != self.req.get('token_fp'):
                return self.kill('token_changed')
            now = self.svc._clock()
            b = _tree_bytes(self.tmp)
            if b != last_bytes or self.verify_done != last_verify:   # bytes arriving, or bytes checked
                last_bytes, last_verify, last_growth = b, self.verify_done, now
                if self.on_progress:
                    self.on_progress(b, self.total)
                last_total = self.total
            elif now - last_growth > self.svc.download_stall_s:
                return self.kill('stalled')
            elif self.total != last_total and self.on_progress:      # the size became known
                last_total = self.total
                self.on_progress(b, self.total)
            if now - t0 > self.svc.download_timeout_s:
                return self.kill('timeout')
            self.svc._sleep(self.svc.download_poll_s)


class DatenService:

    def __init__(self, node, *, root=None, ros=True, start_threads=True, popen=None, kill=None,
                 token_reader=None, state_reader=None, name_rule=None, namespace_reader=None,
                 account_resolver=None, disk_free=None, secret=None, spawn=None, clock=time.monotonic,
                 sleep=time.sleep):
        self.node = node
        self.root = Path(os.path.realpath(root if root is not None else DP.dataset_root()))
        self._lock = threading.Lock()
        self._leases = {}                     # key -> 'edit' | 'delete' | 'download' | 'upload'
        self._jobs = collections.OrderedDict()
        self._edit_slot = None                # the running edit-class job id ('blocking' for /dataset/edit)
        self._download_slot = None            # the running download job id
        self._download_queue = collections.deque()
        self._upload_waiters = {}             # repo_id -> {'event', 'status'}
        self._seq = 0
        self.last_state = None
        self._popen = popen or subprocess.Popen
        self._kill = kill or _kill_group
        self._token_reader = token_reader
        self._state_reader = state_reader
        self._name_rule = name_rule
        self._namespace_reader = namespace_reader
        self._account_resolver = account_resolver or _default_account_resolver
        self._resolved = {'fp': None, 'account': None, 'at': None, 'running': False}   # V2-16
        self._disk_free = disk_free or SIG.disk_free_bytes
        self._spawn = spawn or (lambda fn, *a: threading.Thread(target=fn, args=a, daemon=True,
                                                                  name='daten-job').start())
        self._clock = clock
        self._sleep = sleep
        self.edit_timeout_s = C.EDIT_TIMEOUT_S
        self.download_poll_s = C.DOWNLOAD_POLL_S
        self.download_stall_s = C.DOWNLOAD_STALL_S
        self.download_timeout_s = C.DOWNLOAD_TIMEOUT_S
        self.secret = secret if secret is not None else LT.load_or_create_secret()
        self._pub = None
        if ros:
            self._wire_ros()
        if start_threads:
            threading.Thread(target=self.recover_all, daemon=True, name='daten-recovery').start()

    # ── ROS ────────────────────────────────────────────────────────────────

    def _wire_ros(self):
        from physical_ai_interfaces.srv import DatenCommand
        from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
        from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
        from std_msgs.msg import String
        self._String = String
        self._service_group = MutuallyExclusiveCallbackGroup()
        self._timer_group = MutuallyExclusiveCallbackGroup()
        qos = QoSProfile(depth=1, history=HistoryPolicy.KEEP_LAST, reliability=ReliabilityPolicy.RELIABLE,
                         durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self._pub = self.node.create_publisher(String, C.STATE_TOPIC, qos)
        self.node.create_service(DatenCommand, C.COMMAND_SERVICE, self._command_callback,
                                 callback_group=self._service_group)
        self.node.create_timer(1.0, self._tick, callback_group=self._timer_group)
        self.publish_state()

    def _tick(self):
        self.publish_state()
        self._refresh_account()

    def _command_callback(self, request, response):
        out = self.command(request.action, request.args_json)
        response.success = out['success']
        response.code = out['code']
        response.message = out['message']
        response.result_json = json.dumps(out['result'], ensure_ascii=False)
        return response

    def _log(self, text):
        try:
            self.node.get_logger().info(f'[daten] {text}')
        except Exception:  # noqa: BLE001
            print(f'[daten] {text}', file=sys.stderr, flush=True)

    # ── small readers (each injectable for the tests) ───────────────────────

    def _token(self):
        if self._token_reader is not None:
            return self._token_reader()
        return _mod('data_processing', 'hf_token_store').read()

    def _slot_fp(self):
        token = self._token()
        return _mod('data_processing', 'hf_token_store').fingerprint(token) if token else None

    def _state(self, path):
        if self._state_reader is not None:
            return self._state_reader(path)
        return _mod('daten', 'library').dataset_state(path)

    def _task_name(self, name):
        if self._name_rule is not None:
            return self._name_rule(name)
        from physical_ai_server.data_processing.data_manager import safe_dataset_task_name
        return safe_dataset_task_name(name)

    def _account(self):
        """The slot token's account, or None when unknown (refuse on proof only;
        never a network call here). Daten resolves it itself (V2-16): the account
        ``_refresh_account`` found for the token NOW in the slot (its fingerprint
        must match); else the recorder's namespace cache."""
        if self._namespace_reader is not None:
            names = self._namespace_reader()
        else:
            fp = self._slot_fp()
            with self._lock:
                resolved = dict(self._resolved)
            if fp and resolved['fp'] == fp and resolved['account']:
                return resolved['account']
            self._refresh_account()
            try:
                from physical_ai_server.data_processing.data_manager import DataManager
                names = DataManager._hf_namespace_cache
            except Exception:  # noqa: BLE001
                names = None
        names = sorted(names or ())
        return names[0] if len(names) == 1 else None

    def _refresh_account(self):
        """V2-16: look up the account of the token now in the slot IN THE
        BACKGROUND (one bounded whoami per new fingerprint, never inside a
        command), so ``namespace`` refusals are immediate without relying on a
        cache only the recorder fills. A failed lookup is asked again after
        ``ACCOUNT_RETRY_S``; called by the 1 Hz state tick and by ``_account``."""
        fp = self._slot_fp()
        now = self._clock()
        with self._lock:
            r = self._resolved
            if not fp or r['running']:
                return
            if r['fp'] == fp and (r['account'] or (r['at'] is not None and now - r['at'] < ACCOUNT_RETRY_S)):
                return
            r.update(fp=fp, account=None, at=now, running=True)
        threading.Thread(target=self._resolve_account, args=(fp,), daemon=True, name='daten-account').start()

    def _resolve_account(self, fp):
        account = None
        try:
            names = sorted(self._account_resolver() or ())
            account = names[0] if len(names) == 1 else None
        except Exception:  # noqa: BLE001 — no token, the hub unreachable: unknown, asked again later
            account = None
        current = self._slot_fp()                    # outside the lock (R-18: it reads the slot)
        with self._lock:
            r = self._resolved
            r['running'] = False
            if r['fp'] == fp and current == fp:      # the token changed meanwhile: the answer is stale
                r['account'] = account
                r['at'] = self._clock()

    def _robot_type(self):
        return getattr(self.node, 'robot_type', None) or 'omx_f'

    # ── paths and ids ───────────────────────────────────────────────────────

    def _key(self, path):
        return os.path.abspath(os.path.normpath(str(path)))

    def _id_of(self, key):
        p = Path(key)
        if p.parent.parent == self.root:
            return f'{p.parent.name}/{p.name}'
        return None

    def path_of(self, dataset_id):
        """The confined folder of a dataset id (Refusal invalid / outside)."""
        if not valid_id(dataset_id):
            raise _invalid()
        ns, name = dataset_id.split('/')
        if (self.root / ns).is_symlink() or (self.root / ns / name).is_symlink():
            raise Refusal('outside', DP.OUTSIDE_ROOT_DE)
        try:
            return Path(DP.safe_child(DP.safe_child(self.root, ns), name))
        except DP.DatasetPathError:
            raise Refusal('outside', DP.OUTSIDE_ROOT_DE)

    def _existing(self, dataset_id):
        path = self.path_of(dataset_id)
        if not path.is_dir():
            raise Refusal('not_found', T.NOT_FOUND_DE)
        return path

    # ── leases (R-18: the lock never spans a file-system call or a worker call) ──

    def _hf_task(self):
        worker = getattr(self.node, 'hf_api_worker', None)
        if worker is None or not getattr(worker, 'is_processing', False):
            return None
        task = getattr(worker, 'current_task', None)
        return task if isinstance(task, dict) else None

    def _busy_locked(self, key, include_record=True):
        node = self.node
        if include_record and getattr(node, 'on_recording', False):
            dm = getattr(node, 'data_manager', None)
            save = getattr(dm, '_save_path', None) if dm is not None else None
            if save is not None and self._key(save) == key:
                return 'record'
        kind = self._leases.get(key)
        if kind:
            return kind
        task = self._hf_task()
        if task:
            if task.get('mode') == 'upload' and task.get('local_dir') and self._key(task['local_dir']) == key:
                return 'upload'
            if task.get('mode') == 'download' and task.get('repo_type') == 'dataset' and task.get('repo_id') \
                    and self._key(self.root / str(task['repo_id'])) == key:
                return 'download'
        return None

    def busy_kind(self, path):
        key = self._key(path)
        with self._lock:
            return self._busy_locked(key)

    def claim_record_lease(self, root):
        """The recorder's ``_dataset_lease`` (§C4): the kind of another holder of
        this dataset, or None. The ``record`` lease itself is the node's state
        (this DataManager recording into ``root``), alive exactly as long as the
        session, so the recorder never conflicts with itself."""
        key = self._key(root)
        with self._lock:
            return self._busy_locked(key, include_record=False)

    def _claim(self, keys, kind, slot=None, job_id=None):
        """Check-and-register under the lock: every key free (and the slot), then
        each key leased to ``kind``. Raises the busy Refusal."""
        with self._lock:
            for key in keys:
                busy = self._busy_locked(key)
                if busy:
                    raise Refusal(*_BUSY[busy])
            if slot == 'edit' and self._edit_slot is not None:
                raise Refusal(*_BUSY['edit'])
            if slot == 'download' and (self._download_slot is not None or self._download_queue):
                raise Refusal(*_BUSY['download'])
            for key in keys:
                self._leases[key] = kind
            if slot == 'edit':
                self._edit_slot = job_id or 'blocking'
            if slot == 'download':
                self._download_slot = job_id

    def _release(self, keys, slot=None, job_id=None):
        with self._lock:
            for key in keys:
                self._leases.pop(key, None)
            if slot == 'edit' and self._edit_slot == (job_id or 'blocking'):
                self._edit_slot = None
            if slot == 'download' and self._download_slot == job_id:
                self._download_slot = None

    # ── jobs and the state topic ────────────────────────────────────────────

    def _new_job(self, op, datasets, outputs, unit='steps'):
        job_id = uuid.uuid4().hex[:12]
        job = {'job_id': job_id, 'op': op, 'state': 'running', 'datasets': list(datasets),
               'outputs': list(outputs), 'stage': 'prepare', 'done': 0, 'total': 0, 'unit': unit,
               'code': '', 'message': '', 'started_at': _now_iso(), 'finished_at': None}
        with self._lock:
            self._jobs[job_id] = {'public': job, 'result': None, 'cancel': threading.Event(),
                                  'finished_mono': None}
        return job_id

    def _job_update(self, job_id, **fields):
        with self._lock:
            slot = self._jobs.get(job_id)
            if slot is None:
                return
            slot['public'].update(fields)
        self.publish_state()

    def _job_finish(self, job_id, ok, code='', message='', result=None, **extra):
        with self._lock:
            slot = self._jobs.get(job_id)
            if slot is None:
                return
            slot['public'].update(state='done' if ok else 'failed', code='' if ok else (code or 'internal'),
                                  message='' if ok else (message or ''), finished_at=_now_iso(), **extra)
            slot['result'] = result if result is not None else {'ok': ok, 'code': code}
            slot['finished_mono'] = self._clock()
            self._prune_locked()
        self.publish_state()

    def _prune_locked(self):
        now = self._clock()
        finished = [(jid, s) for jid, s in self._jobs.items() if s['finished_mono'] is not None]
        for jid, s in finished:
            if now - s['finished_mono'] > C.JOB_KEEP_S:
                self._jobs.pop(jid, None)
        finished = [jid for jid, s in self._jobs.items() if s['finished_mono'] is not None]
        for jid in finished[:max(0, len(finished) - C.JOB_KEEP_MAX)]:
            self._jobs.pop(jid, None)

    def _job_result(self, job_id):
        with self._lock:
            slot = self._jobs.get(job_id)
            return None if slot is None else slot['result']

    def _cancel_job(self, job_id):
        queued = False
        with self._lock:
            slot = self._jobs.get(job_id)
            if slot is None:
                return
            slot['cancel'].set()
            if job_id in [q[0] for q in self._download_queue]:
                self._download_queue = collections.deque(q for q in self._download_queue if q[0] != job_id)
                queued = True
        if queued:
            self._job_finish(job_id, False, 'cancelled', '', result={'ok': False, 'code': 'cancelled'})

    def state_payload(self):
        """§J.5. ``busy`` lists every (id, kind) that holds a dataset; one id may
        appear with two kinds — a Start that waits for this dataset's upload is
        ``record`` AND ``upload`` (the Aufnahme page reads the ``upload`` entry of
        the repo it is starting, §G11), listed in that order."""
        with self._lock:
            self._prune_locked()
            busy = []
            dm = getattr(self.node, 'data_manager', None)
            if getattr(self.node, 'on_recording', False) and dm is not None and getattr(dm, '_save_path', None):
                busy.append((self._key(dm._save_path), 'record'))
            busy += list(self._leases.items())
            task = self._hf_task()
            transfer = None
            if task and task.get('mode') == 'upload':
                key = self._key(task['local_dir']) if task.get('local_dir') else None
                if key:
                    busy.append((key, 'upload'))
                transfer = {'kind': 'upload', 'repo_id': task.get('repo_id') or '',
                            'target': self._id_of(key) if key else None}
            elif task and task.get('mode') == 'download' and task.get('repo_type') == 'dataset':
                busy.append((self._key(self.root / str(task.get('repo_id') or '')), 'download'))
            jobs = [dict(s['public']) for s in self._jobs.values()]
            self._seq += 1
            seq = self._seq
        out_busy = []
        for key, kind in busy:
            entry = {'id': self._id_of(key), 'kind': kind}
            if entry['id'] and entry not in out_busy:
                out_busy.append(entry)
        return {'v': C.SCHEMA_VERSION, 'seq': seq, 'busy': out_busy, 'jobs': jobs, 'transfer': transfer}

    def publish_state(self):
        try:
            payload = self.state_payload()
            self.last_state = payload
            if self._pub is not None:
                msg = self._String()
                msg.data = json.dumps(payload, ensure_ascii=False)
                self._pub.publish(msg)
        except Exception as e:  # noqa: BLE001 — a status topic never takes the node down
            self._log(f'daten_state not published: {e!r}')

    # ── the command surface (§J.3) ──────────────────────────────────────────

    def command(self, action, args_json):
        """``{'success', 'code', 'message', 'result'}``; never raises."""
        try:
            try:
                args = json.loads(args_json or '{}')
            except (TypeError, ValueError):
                raise _invalid()
            if not isinstance(args, dict):
                raise _invalid()
            handler = {'link': self._link, 'edit': self._edit, 'delete_dataset': self._delete_dataset,
                       'upload': self._upload, 'download': self._download, 'keep_both': self._keep_both,
                       'cancel': self._cancel, 'state': lambda a: self.state_payload()}.get(action)
            if handler is None:
                raise _invalid()
            return {'success': True, 'code': '', 'message': '', 'result': handler(args)}
        except Refusal as r:
            return {'success': False, 'code': r.code, 'message': r.message, 'result': {}}
        except Exception as e:  # noqa: BLE001
            self._log(f'command {action!r} failed: {e!r}')
            message = T.DOWNLOAD_FAILED_DE if action in ('download', 'keep_both') else T.RUN_EDIT_FAILED_DE
            return {'success': False, 'code': 'internal', 'message': message, 'result': {}}

    # link
    def _link(self, args):
        ids = args.get('datasets', [])
        library = args.get('library', False)
        if not isinstance(ids, list) or len(ids) > C.MAX_LINK_DATASETS or not isinstance(library, bool) \
                or not all(valid_id(i) for i in ids):
            raise _invalid()
        tokens, missing = {}, []
        for dataset_id in dict.fromkeys(ids):
            try:
                if self._existing(dataset_id):
                    tokens[dataset_id] = LT.mint(self.secret, LT.dataset_scope(dataset_id))
            except Refusal:
                missing.append(dataset_id)
        return {'ttl_s': C.TOKEN_TTL_S, 'library_token': LT.mint(self.secret, LT.LIB_SCOPE) if library else None,
                'tokens': tokens, 'missing': missing}

    # shared checks
    def _digest_ok(self, path, digest, message=T.STALE_DE):
        if not isinstance(digest, str) or not digest:
            raise _invalid()
        if S.meta_digest(path) != digest:
            raise Refusal('stale', message)

    def _state_ok(self, path):
        state = self._state(path)
        if state != 'ok':
            raise Refusal(state if state in _STATE_REFUSAL else 'unsupported',
                          _STATE_REFUSAL.get(state, T.UNSUPPORTED_DE))

    def _total_episodes(self, path):
        try:
            info = json.loads((Path(path) / 'meta' / 'info.json').read_text(encoding='utf-8'))
            return int(info['total_episodes'])
        except Exception:  # noqa: BLE001
            raise Refusal('incomplete', T.INCOMPLETE_DE)

    @staticmethod
    def _indices(episodes, total, keep_one):
        if not isinstance(episodes, list) or not episodes or \
                any(type(i) is not int or not 0 <= i < total for i in episodes):
            raise Refusal('invalid', T.INVALID_EPISODES_DE)
        out = sorted(set(episodes))
        if keep_one and len(out) >= total:
            raise Refusal('invalid', T.INVALID_EPISODES_DE)
        return out

    def _target(self, args, sources):
        new_name, owner_ns = args.get('new_name'), args.get('owner_ns')
        if not isinstance(new_name, str) or not new_name.strip() or not isinstance(owner_ns, str) \
                or not _PART.fullmatch(owner_ns):
            raise _invalid()
        account = self._account()
        if account is not None and owner_ns != account:
            raise Refusal('namespace', T.NAMESPACE_EDIT_DE)
        target_id = f'{owner_ns}/{self._robot_type()}_{self._task_name(new_name.strip())}'
        if not valid_id(target_id):
            raise _invalid()
        path = self.path_of(target_id)
        if path.exists() or any(self._key(path) == self._key(s) for s in sources):
            raise Refusal('exists', T.EXISTS_DE)
        return target_id, path, new_name.strip()

    def _disk_for(self, sources, factor=1):
        need = factor * sum(_tree_bytes(p) for p in sources)
        free = self._disk_free(self.root)
        if free is not None and free - need < SIG.DISK_CRITICAL_FLOOR_BYTES:
            return free, need
        return None

    # edit
    def _edit(self, args):
        op = args.get('op')
        if op not in C.EDIT_OPS:
            raise _invalid()
        if op == 'merge':
            items = args.get('datasets')
            if not isinstance(items, list) or len(items) < 2 or not all(isinstance(x, dict) for x in items):
                raise _invalid()
            ids = [x.get('id') for x in items]
            if not all(valid_id(i) for i in ids) or len(set(ids)) != len(ids):
                raise _invalid()
            digests = [x.get('meta_digest') for x in items]
        else:
            ids = [args.get('dataset')]
            if not valid_id(ids[0]):
                raise _invalid()
            digests = [args.get('meta_digest')]
        paths = [self._existing(i) for i in ids]                       # existence
        for p, d in zip(paths, digests):                               # stale, checked FIRST of the rest
            self._digest_ok(p, d)
        for p in paths:                                                # state
            self._state_ok(p)
        keys = [self._key(p) for p in paths]
        with self._lock:                                               # busy (a cheap pre-check; the claim re-checks)
            for key in keys:
                busy = self._busy_locked(key)
                if busy:
                    raise Refusal(*_BUSY[busy])
            if self._edit_slot is not None:
                raise Refusal(*_BUSY['edit'])
        if op in ('delete', 'split'):                                  # indices
            episodes = self._indices(args.get('episodes'), self._total_episodes(paths[0]), keep_one=True)
        target_id, target_path, display = (None, None, None)
        if op in ('split', 'merge'):                                   # names
            target_id, target_path, display = self._target(args, paths)
        short = self._disk_for(paths)                                  # disk
        if short:
            raise Refusal('disk', T.disk_edit_de(*short))
        outputs = [ids[0]] if op == 'delete' else ([ids[0], target_id] if op == 'split' else [target_id])
        out_keys = keys + ([self._key(target_path)] if target_path else [])
        job_id = self._new_job(op, ids, outputs)
        try:
            self._claim(out_keys, 'edit', slot='edit', job_id=job_id)
        except Refusal:
            with self._lock:
                self._jobs.pop(job_id, None)
            raise
        if op == 'delete':
            payload = {'mode': 'delete', 'delete_dataset_path': str(paths[0]), 'delete_episode_num': episodes}
        elif op == 'split':
            payload = {'mode': 'split', 'dataset_path': str(paths[0]), 'new_path': str(target_path),
                       'episodes': episodes, 'display_name': display}
        else:
            payload = {'mode': 'merge', 'merge_dataset_list': [str(p) for p in paths],
                       'output_path': str(target_path), 'display_name': display}
        self.publish_state()
        self._spawn(self._edit_job, job_id, payload, out_keys)
        return {'job_id': job_id, 'outputs': outputs}

    def _edit_job(self, job_id, payload, keys):
        try:
            res = self.run_edit_process(payload, job_id=job_id)
            if res.get('success'):
                extra = {k: res[k] for k in ('episodes', 'kept', 'moved') if k in res}
                self._job_finish(job_id, True, **extra)
            else:
                code = res.get('code') if res.get('code') in C.JOB_FAIL_CODES else 'internal'
                self._job_finish(job_id, False, code, res.get('message') or T.RUN_EDIT_FAILED_DE)
        except Exception as e:  # noqa: BLE001
            self._log(f'edit job failed: {e!r}')
            self._job_finish(job_id, False, 'internal', T.RUN_EDIT_FAILED_DE)
        finally:
            self._release(keys, slot='edit', job_id=job_id)
            self.publish_state()

    def run_edit_process(self, payload, job_id=None):
        """The edit worker as a process (nice 19, its own session): the payload
        on stdin, progress lines into the job, the result line back; killed
        after ``edit_timeout_s``."""
        ew = _mod('data_processing', 'edit_worker')
        proc = self._popen(ew.build_command(sys.executable), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, env=os.environ.copy(), start_new_session=True)
        timed_out = threading.Event()

        def _timeout():
            timed_out.set()
            self._kill(proc)
        timer = threading.Timer(self.edit_timeout_s, _timeout)
        timer.daemon = True
        timer.start()
        result = None
        try:
            try:
                proc.stdin.write(json.dumps(payload))
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
            for line in proc.stdout:
                p = ew.parse_progress(line)
                if p is not None and job_id is not None:
                    stage = p.get('stage') if p.get('stage') in C.JOB_STAGES else None
                    fields = {'done': int(p.get('done') or 0), 'total': int(p.get('total') or 0)}
                    if stage:
                        fields['stage'] = stage
                    self._job_update(job_id, **fields)
                    continue
                parsed = ew.parse_output(line)
                if parsed is not None:
                    result = parsed
            proc.wait()
        finally:
            timer.cancel()
        if timed_out.is_set():
            return {'success': False, 'code': 'timeout', 'message': T.RUN_EDIT_FAILED_DE}
        return result or {'success': False, 'code': 'internal', 'message': T.RUN_EDIT_FAILED_DE}

    def run_edit_blocking(self, payload):
        """The old ``/dataset/edit``: the same single flight, the same leases,
        the same engine; answers ``{'success', 'message'}`` in German."""
        if not isinstance(payload, dict):
            return {'success': False, 'message': T.UNKNOWN_MODE_DE, 'code': 'invalid'}
        paths = list(payload.get('merge_dataset_list') or []) + \
            [payload.get('output_path'), payload.get('delete_dataset_path')]
        keys = [self._key(p) for p in paths if isinstance(p, str) and p]
        try:
            self._claim(keys, 'edit', slot='edit')
        except Refusal as r:
            return {'success': False, 'message': r.message, 'code': r.code}
        self.publish_state()
        try:
            return self.run_edit_process(payload)
        finally:
            self._release(keys, slot='edit')
            self.publish_state()

    # delete_dataset
    def _delete_dataset(self, args):
        dataset_id = args.get('dataset')
        if not valid_id(dataset_id) or not isinstance(args.get('meta_digest'), str) or not args['meta_digest']:
            raise _invalid()
        path = self._existing(dataset_id)
        self._digest_ok(path, args['meta_digest'], T.STALE_ACTION_DE)
        key = self._key(path)
        job_id = self._new_job('delete_dataset', [dataset_id], [])
        try:
            self._claim([key], 'delete', slot='edit', job_id=job_id)
        except Refusal:
            with self._lock:
                self._jobs.pop(job_id, None)
            raise
        self.publish_state()
        self._spawn(self._delete_job, job_id, path, args['meta_digest'], key)
        return {'job_id': job_id}

    def _delete_job(self, job_id, path, digest, key):
        fd = None
        try:
            try:
                fd = HS.stage_lock(path)
            except BlockingIOError:
                self._job_finish(job_id, False, 'internal', T.BUSY_EDIT_DE)
                return
            if not path.exists():
                self._job_finish(job_id, False, 'not_found', T.NOT_FOUND_DE)
                return
            if S.meta_digest(path) != digest:                              # U-4, under the lock
                self._job_finish(job_id, False, 'stale', T.STALE_ACTION_DE)
                return
            self._job_update(job_id, stage='swap')
            trash = Path(f'{path}{HS.TRASH_SUFFIX}')
            shutil.rmtree(trash, ignore_errors=True)
            os.rename(path, trash)
            for sibling in (S.record_path(path), S.session_marker_path(path), S.record_next_path(path),
                            S.journal_path(path)):
                Path(sibling).unlink(missing_ok=True)
            shutil.rmtree(trash, ignore_errors=True)
            self._job_finish(job_id, True)
        except Exception as e:  # noqa: BLE001
            self._log(f'delete_dataset failed: {e!r}')
            self._job_finish(job_id, False, 'internal', T.RUN_EDIT_FAILED_DE)
        finally:
            if fd is not None:
                HS.release_lock(fd)
                try:
                    S.lock_path(path).unlink()
                except OSError:
                    pass
            self._release([key], slot='edit', job_id=job_id)
            self.publish_state()

    # upload
    def _upload(self, args):
        dataset_id = args.get('dataset')
        if not valid_id(dataset_id) or not isinstance(args.get('private', False), bool):
            raise _invalid()
        expected = args.get('expected_hub_sha', HS.UNSET)
        if expected is not HS.UNSET and expected is not None and not (isinstance(expected, str)
                                                                       and _SHA.fullmatch(expected)):
            raise _invalid()
        path = self._existing(dataset_id)
        state = self._state(path)
        if state == 'in_session':
            raise Refusal('in_session', R.UPLOAD_IN_SESSION_DE)
        if state == 'incomplete':
            raise Refusal('incomplete', R.UPLOAD_BROKEN_DE)
        if state in ('unsupported', 'old_format'):
            raise Refusal('unsupported', T.OLD_FORMAT_DE if state == 'old_format' else T.UNSUPPORTED_DE)
        account = self._account()
        if account is not None and dataset_id.split('/')[0] != account:
            raise Refusal('namespace', R.NAMESPACE_REFUSED_DE)
        request = {'mode': 'upload', 'repo_id': dataset_id, 'local_dir': str(path), 'repo_type': 'dataset',
                   'author': '', 'private': bool(args.get('private', False))}
        if expected is not HS.UNSET:
            request['expected_hub_sha'] = expected
        self._enqueue_upload(self._key(path), request)
        return {'repo_id': dataset_id}

    def send_control_upload(self, local_dir, request):
        """The old page's ``/huggingface/control`` upload (V1-6): handed to the
        HF worker under the same transient ``upload`` lease and busy check as a
        Daten upload, so it never starts on a dataset a Daten edit, delete or
        download (or a recording) holds. Returns None once the worker took it,
        else the German refusal."""
        try:
            self._enqueue_upload(self._key(local_dir), request)
        except Refusal as e:
            return e.message
        return None

    def _hf_worker_ready(self):
        """The HF worker, started when absent (outside the registry lock); None
        when it cannot take a task now."""
        worker = getattr(self.node, 'hf_api_worker', None)
        if worker is None or not worker.is_alive():
            init = getattr(self.node, '_init_hf_api_worker', None)
            if init is not None:
                init()
            worker = getattr(self.node, 'hf_api_worker', None)
        if worker is None or not worker.is_alive() or worker.is_busy():
            return None
        return worker

    def _enqueue_upload(self, key, request, release_after=None):
        """A transient ``upload`` lease while the request is handed over (the HF
        worker's task holds the dataset from then on); ``release_after`` (a
        keep_both job's ``edit`` key) is released only after the enqueue, so the
        busy kind goes ``edit`` → ``upload`` with no gap."""
        with self._lock:
            busy = self._busy_locked(key)
            if busy and not (release_after and busy == 'edit' and self._leases.get(key) == 'edit'):
                if busy == 'record':                     # T1-3: an upload is not an edit
                    raise Refusal('busy_record', T.BUSY_RECORD_UPLOAD_DE)
                raise Refusal(*_BUSY[busy])
            if not release_after:
                self._leases[key] = 'upload'
        try:
            worker = self._hf_worker_ready()
            if worker is None or not worker.send_request(request):
                raise Refusal('unavailable', T.UNAVAILABLE_DE)
        finally:
            with self._lock:
                if self._leases.get(key) == 'upload':
                    self._leases.pop(key, None)
                if release_after:
                    self._leases.pop(release_after, None)
            self.publish_state()

    # download
    def _download(self, args):
        repo_id, revision, target_id = args.get('repo_id'), args.get('revision'), args.get('target')
        mode, display = args.get('mode'), args.get('display_name')
        if not isinstance(repo_id, str) or not _REPO.fullmatch(repo_id) or not isinstance(revision, str) \
                or not _SHA.fullmatch(revision) or not valid_id(target_id) or mode not in C.DOWNLOAD_MODES \
                or (display is not None and not isinstance(display, str)):
            raise _invalid()
        digest = args.get('meta_digest')
        if mode == 'replace' and (not isinstance(digest, str) or not digest):
            raise _invalid()
        path = self.path_of(target_id)
        if mode in ('new', 'copy') and path.exists():
            raise Refusal('exists', T.DOWNLOAD_EXISTS_DE if mode == 'new' else T.EXISTS_DE)
        if mode == 'replace':
            if not path.is_dir():
                raise Refusal('not_found', T.NOT_FOUND_DE)
            self._digest_ok(path, digest, T.STALE_ACTION_DE)                # T-1 c; the worker re-checks
        token_fp = self._slot_fp()
        if not token_fp:
            raise Refusal('unavailable', R.HF_TOKEN_NONE_DE)
        key = self._key(path)
        job_id = self._new_job('download', [], [target_id], unit='bytes')
        try:
            self._claim([key], 'download', slot='download', job_id=job_id)
        except Refusal:
            with self._lock:
                self._jobs.pop(job_id, None)
            raise
        request = {'repo_id': repo_id, 'revision': revision, 'target_dir': str(path), 'mode': mode,
                   'display_name': display, 'robot_type': self._robot_type(), 'token_fp': token_fp,
                   'meta_digest': digest if mode == 'replace' else None}
        self.publish_state()
        self._spawn(self._download_job, job_id, request, [key])
        return {'job_id': job_id, 'target': target_id}

    def start_sync_download(self, repo_id, revision, root, token_fp):
        """The recorder's D14 „newer" / D7 „exists" download (§C4, S-2): a
        ``sync`` job on the download slot (queued behind a running download);
        shown as ``op: download`` while the dataset's busy kind stays
        ``record``. Returns a SyncHandle."""
        path = Path(root)
        dataset_id = self._id_of(self._key(path)) or repo_id
        job_id = self._new_job('download', [dataset_id], [dataset_id], unit='bytes')
        request = {'repo_id': repo_id, 'revision': revision, 'target_dir': str(path), 'mode': 'sync',
                   'display_name': None, 'robot_type': self._robot_type(), 'token_fp': token_fp,
                   'meta_digest': None}
        start = False
        with self._lock:
            if self._download_slot is None and not self._download_queue:
                self._download_slot = job_id
                start = True
            else:
                self._download_queue.append((job_id, request))
        self.publish_state()
        if start:
            self._spawn(self._download_job, job_id, request, [])
        return SyncHandle(self, job_id)

    def _download_job(self, job_id, request, keys):
        try:
            with self._lock:
                cancel = self._jobs[job_id]['cancel']
            self._job_update(job_id, stage='download')
            result = DownloadProcess(self, request, on_progress=lambda done, total: self._job_update(
                job_id, done=done, total=total)).run(cancel)
            if result.get('ok'):
                self._job_finish(job_id, True, result=result)
            else:
                code, message = self._download_failure(result)
                self._job_finish(job_id, False, code, message, result=result)
        except Exception as e:  # noqa: BLE001
            self._log(f'download job failed: {e!r}')
            self._job_finish(job_id, False, 'internal', T.DOWNLOAD_FAILED_DE,
                             result={'ok': False, 'code': 'internal'})
        finally:
            nxt = None
            with self._lock:
                for key in keys:
                    self._leases.pop(key, None)
                if self._download_slot == job_id:
                    self._download_slot = None
                    if self._download_queue:
                        nxt = self._download_queue.popleft()
                        self._download_slot = nxt[0]
            self.publish_state()
            if nxt is not None:
                self._spawn(self._download_job, nxt[0], nxt[1], [])

    @staticmethod
    def _download_failure(result):
        """A download worker's code → (job code, German message)."""
        code = result.get('code') or 'internal'
        hf = R.HF_ERROR_SENTENCES_DE
        table = {
            'disk': ('disk', None), 'old_format': ('old_format', T.DOWNLOAD_OLD_FORMAT_DE),
            'other_robot': ('other_robot', T.DOWNLOAD_OTHER_ROBOT_DE),
            'unsupported': ('unsupported', T.DOWNLOAD_UNSUPPORTED_DE), 'broken': ('broken', T.DOWNLOAD_BROKEN_DE),
            'exists': ('exists', T.DOWNLOAD_EXISTS_DE), 'not_found': ('not_found', T.DOWNLOAD_NOT_FOUND_DE),
            'token_changed': ('token_changed', T.DOWNLOAD_TOKEN_CHANGED_DE),
            'orphaned': ('token_changed', T.DOWNLOAD_TOKEN_CHANGED_DE),
            'stalled': ('stalled', R.DOWNLOAD_STALL_DE), 'timeout': ('timeout', R.DOWNLOAD_STALL_DE),
            'stale': ('stale', T.STALE_ACTION_DE), 'auth': ('auth', hf['auth']),
            'unreachable': ('unreachable', hf['network']), 'cancelled': ('cancelled', ''),
            'in_session': ('broken', R.UPLOAD_IN_SESSION_DE), 'local_broken': ('broken', R.UPLOAD_BROKEN_DE),
            'namespace': ('auth', R.NAMESPACE_REFUSED_DE),
        }
        job_code, message = table.get(code, ('internal', T.DOWNLOAD_FAILED_DE))
        if code == 'disk':
            message = T.download_disk_de(result.get('free') or 0, result.get('need') or 0)
        return job_code, message

    # keep_both (§E10)
    def _keep_both(self, args):
        dataset_id, head, digest = args.get('dataset'), args.get('expected_hub_sha'), args.get('meta_digest')
        if not valid_id(dataset_id) or not isinstance(head, str) or not _SHA.fullmatch(head):
            raise _invalid()
        path = self._existing(dataset_id)
        self._digest_ok(path, digest)
        self._state_ok(path)
        account = self._account()
        if account is not None and dataset_id.split('/')[0] != account:      # H-3, refused on proof
            raise Refusal('namespace', R.NAMESPACE_REFUSED_DE)
        key = self._key(path)
        with self._lock:
            busy = self._busy_locked(key)
            if busy:
                raise Refusal(*_BUSY[busy])
            if self._edit_slot is not None:
                raise Refusal(*_BUSY['edit'])
        short = self._disk_for([path], factor=2)
        if short:
            raise Refusal('disk', T.keep_both_disk_de(*short))
        token_fp = self._slot_fp()
        if not token_fp:
            raise Refusal('unavailable', R.HF_TOKEN_NONE_DE)
        worker = getattr(self.node, 'hf_api_worker', None)
        if worker is not None and getattr(worker, 'is_processing', False):
            raise Refusal('unavailable', T.UNAVAILABLE_DE)
        job_id = self._new_job('keep_both', [dataset_id], [dataset_id])
        try:
            self._claim([key], 'edit', slot='edit', job_id=job_id)
        except Refusal:
            with self._lock:
                self._jobs.pop(job_id, None)
            raise
        self.publish_state()
        self._spawn(self._keep_both_job, job_id, dataset_id, path, head, token_fp)
        return {'job_id': job_id}

    def _keep_both_job(self, job_id, dataset_id, path, head, token_fp):
        key = self._key(path)
        keep_tmp, base_tmp = HS.tmp_path_of(path, 'keep'), HS.tmp_path_of(path, 'base')
        leased = True
        try:
            with self._lock:
                cancel = self._jobs[job_id]['cancel']
            base_req = {'repo_id': dataset_id, 'target_dir': str(path), 'display_name': None,
                        'robot_type': self._robot_type(), 'token_fp': token_fp, 'meta_digest': None}
            self._job_update(job_id, stage='download')
            r = DownloadProcess(self, dict(base_req, revision=head, mode='keep')).run(cancel)
            if not r.get('ok'):
                self._job_finish(job_id, False, *self._download_failure(r))
                return
            trees = r.get('trees')
            base_sha = S.own_record(path, dataset_id).get('hub_sha')
            base_dir = None
            if base_sha and base_sha != head:
                rb = DownloadProcess(self, dict(base_req, revision=base_sha, mode='base')).run(cancel)
                if not rb.get('ok'):
                    self._job_finish(job_id, False, *self._download_failure(rb))
                    return
                if not rb.get('no_base'):
                    base_dir = str(base_tmp)
            self._job_update(job_id, stage='copy')
            res = self.run_edit_process({'mode': 'union', 'dataset_path': str(path),
                                         'hub_copy_path': str(keep_tmp), 'base_copy_path': base_dir,
                                         'hub_sha': head, 'hub_trees': trees}, job_id=job_id)
            if not res.get('success'):
                code = res.get('code') if res.get('code') in C.JOB_FAIL_CODES else 'internal'
                self._job_finish(job_id, False, code, res.get('message') or T.RUN_EDIT_FAILED_DE)
                return
            episodes = res.get('episodes')
            # the upload stage: ENQUEUED FIRST, the edit lease released right after
            self._job_update(job_id, stage='upload', done=0, total=0)
            waiter = {'event': threading.Event(), 'status': None}
            with self._lock:
                self._upload_waiters[dataset_id] = waiter
            record = S.own_record(path, dataset_id)
            request = {'mode': 'upload', 'repo_id': dataset_id, 'local_dir': str(path), 'repo_type': 'dataset',
                       'author': '', 'private': bool(record.get('private', False)), 'expected_hub_sha': head}
            try:
                self._enqueue_upload(key, request, release_after=key)
                leased = False
            except Refusal:
                leased = False                                  # released by _enqueue_upload's finally
                self._job_finish(job_id, False, 'unavailable', T.UNAVAILABLE_DE, episodes=episodes)
                return
            status = self._await_upload(waiter, cancel)
            if status is None:
                self._job_finish(job_id, False, 'cancelled' if cancel.is_set() else 'internal',
                                 '' if cancel.is_set() else T.UNAVAILABLE_DE, episodes=episodes)
            elif status.get('status') == 'Success':
                self._job_finish(job_id, True, episodes=episodes)
            else:
                self._job_finish(job_id, False, self._upload_failure(status), status.get('message') or '',
                                 episodes=episodes)
        except Exception as e:  # noqa: BLE001
            self._log(f'keep_both job failed: {e!r}')
            self._job_finish(job_id, False, 'internal', T.RUN_EDIT_FAILED_DE)
        finally:
            with self._lock:
                self._upload_waiters.pop(dataset_id, None)
            shutil.rmtree(keep_tmp, ignore_errors=True)
            shutil.rmtree(base_tmp, ignore_errors=True)
            self._release([key] if leased else [], slot='edit', job_id=job_id)
            self.publish_state()

    @staticmethod
    def _upload_failure(status):
        code = status.get('upload_code') or ''
        if code in ('hub_changed', 'hub_differs', 'auth', 'unreachable'):
            return code
        if code in ('in_session', 'local_broken'):
            return 'broken'
        if status.get('message') == R.UPLOAD_STALL_DE:
            return 'stalled'
        return 'internal'

    def _await_upload(self, waiter, cancel):
        """The HF worker's result for the keep_both upload (``on_hf_status``), or
        None when it was cancelled or the worker stopped without one."""
        idle_since = None
        while not waiter['event'].wait(0.5):
            if cancel.is_set():
                return None
            worker = getattr(self.node, 'hf_api_worker', None)
            busy = worker is not None and getattr(worker, 'is_processing', False)
            if busy:
                idle_since = None
            elif idle_since is None:
                idle_since = self._clock()
            elif self._clock() - idle_since > UPLOAD_STATUS_GRACE_S:
                return None
        return waiter['status']

    def on_hf_status(self, status):
        """Called by the node's HF status timer with every status it publishes:
        delivers a finished upload to the keep_both job waiting for it."""
        if not isinstance(status, dict) or status.get('status') not in ('Success', 'Failed'):
            return
        if status.get('operation') not in ('upload', None):
            return
        with self._lock:
            waiter = self._upload_waiters.get(status.get('repo_id'))
        if waiter is not None:
            waiter['status'] = status
            waiter['event'].set()

    # cancel
    def _cancel(self, args):
        what = args.get('what')
        if what not in C.CANCEL_WHAT:
            raise _invalid()
        if what == 'upload':
            cleanup = getattr(self.node, '_cleanup_hf_api_worker_with_threading', None)
            if cleanup is not None:
                cleanup()
            with self._lock:
                waiting = [s for jid, s in self._jobs.items()
                           if s['finished_mono'] is None and s['public']['op'] == 'keep_both'
                           and s['public']['stage'] == 'upload']
            for slot in waiting:
                slot['cancel'].set()
        else:
            with self._lock:
                running = self._download_slot
            if running is not None:
                self._cancel_job(running)
        return {}

    # ── boot recovery (§D3) ──────────────────────────────────────────────────

    def recover_all(self):
        """Per ``<ns>/<X>`` with leftovers, only when X has no lease and X's lock
        file can be taken: the split journal both ways, else ``hub_sync.recover``;
        tmp/bak/trash directories removed. Holds an ``edit`` lease on X while it
        works (U-5). Journals FIRST: a split's new output must not lose its
        ``.tmp_edit`` to its own recovery before the journal decided the split."""
        if not self.root.is_dir():
            return []
        found = []
        for ns in sorted(self.root.iterdir()):
            if ns.is_dir() and not ns.is_symlink() and _PART.fullmatch(ns.name):
                found += [ns / name for name in sorted(self._leftover_names(ns))]
        with_journal = [p for p in found if S.journal_path(p).exists()]
        done = []
        for path in with_journal + [p for p in found if p not in with_journal]:
            outcome = self._recover_one(path)
            if outcome:
                done.append((f'{path.parent.name}/{path.name}', outcome))
        self.publish_state()
        return done

    @staticmethod
    def _leftover_names(ns):
        names = set()
        suffixes = HS.TMP_SUFFIXES + HS.BAK_SUFFIXES + (HS.TRASH_SUFFIX,)
        for entry in os.listdir(ns):
            for suf in suffixes:
                if entry.endswith(suf) and len(entry) > len(suf):
                    names.add(entry[:-len(suf)])
            if entry.startswith('.') and entry.endswith('.journal.json'):
                names.add(entry[1:-len('.journal.json')])
            if entry.startswith('.') and entry.endswith('.sync.next.json'):
                names.add(entry[1:-len('.sync.next.json')])
        return {n for n in names if _PART.fullmatch(n)}

    def _recover_one(self, path):
        partner = None
        try:
            journal = json.loads(S.journal_path(path).read_text(encoding='utf-8'))
            if isinstance(journal, dict) and journal.get('new'):
                partner = Path(journal['new'])
        except (OSError, ValueError):
            pass
        paths = [path] + ([partner] if partner is not None else [])
        keys = [self._key(p) for p in paths]
        with self._lock:
            if any(self._busy_locked(k) or k in self._leases for k in keys):
                return None
            for k in keys:
                self._leases[k] = 'edit'
        fds = []
        try:
            try:
                for p in paths:
                    fds.append(HS.stage_lock(p))
            except BlockingIOError:
                return None                                        # a surviving worker owns it
            outcome = 'recovered'
            if partner is not None or S.journal_path(path).exists():
                outcome = _mod('data_processing', 'data_editor_v3').recover_split(path) or outcome
            HS.recover(path)
            trash = Path(f'{path}{HS.TRASH_SUFFIX}')
            if trash.exists():
                shutil.rmtree(trash, ignore_errors=True)
                if not path.exists():                              # a whole-dataset delete that was cut short
                    for sibling in (S.record_path(path), S.session_marker_path(path)):
                        Path(sibling).unlink(missing_ok=True)
            self._log(f'recovered {path.parent.name}/{path.name}: {outcome}')
            return outcome
        except Exception as e:  # noqa: BLE001 — one dataset never stops the rest
            self._log(f'recovery of {path.name} failed: {e!r}')
            return None
        finally:
            for fd in fds:
                HS.release_lock(fd)
            with self._lock:
                for k in keys:
                    if self._leases.get(k) == 'edit':
                        self._leases.pop(k, None)

"""F7: the Hugging Face upload stall watchdog (spec-r5-final §7.4).

``upload_large_folder`` retries a failing LFS pre-upload or commit FOREVER, and
its report line does not move while one large file is on the wire, so a dead
network used to leave the page at „Hochladen … 30 %" for good. The rule:

* the child forwards every ERROR record of ``huggingface_hub._upload_large_folder``
  (its public „Failed to preupload LFS / to commit / to get upload mode / to
  compute sha256" lines) as ``{'type': 'upload_error'}`` on the progress queue;
* the parent drains ALL queue items; progress = (current, total, percentage)
  changed; a stall = no progress for ``UPLOAD_STALL_S`` (120 s) with at least
  one upload error in that window, or no progress for ``UPLOAD_HARD_STALL_S``
  (1800 s) at all;
* then the child is terminated and ONE ``Failed`` with ``UPLOAD_STALL_DE`` is
  emitted; the worker reports Idle afterwards. The local dataset is untouched
  (the worker only ever reads it).

``hf_api_worker`` imports ``DataManager`` (a large ROS/cv2/HF tree), so it is
loaded by path with that one import stubbed for the duration of each test.
"""

from __future__ import annotations

import importlib.util
import logging
import queue
import sys
import types
from pathlib import Path

import pytest

from physical_ai_server.data_processing import record_texts_de

_DP = Path(__file__).resolve().parents[1] / 'physical_ai_server' / 'data_processing'


class _FakeDataManager:
    _last_hf_failure_reason_de = None

    @staticmethod
    def set_progress_queue(_q):
        return None


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def time(self):
        return self.t

    def sleep(self, _s):
        return None


class _Proc:
    def __init__(self):
        self.alive = True
        self.killed = False
        self.pid = 4242

    def is_alive(self):
        return self.alive

    def join(self, _timeout=None):
        return None

    def kill(self):
        self.killed = True
        self.alive = False

    terminate = kill


@pytest.fixture
def mod(monkeypatch):
    dm = types.ModuleType('physical_ai_server.data_processing.data_manager')
    dm.DataManager = _FakeDataManager
    monkeypatch.setitem(sys.modules, 'physical_ai_server.data_processing.data_manager', dm)
    spec = importlib.util.spec_from_file_location('_hf_api_worker_stall_test',
                                                  _DP / 'hf_api_worker.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    clock = _Clock()
    monkeypatch.setattr(module, 'time', clock)
    module._test_clock = clock
    return module


def _worker(mod, mode='upload'):
    w = mod.HfApiWorker()
    w.progress_queue = queue.Queue()
    w.output_queue = queue.Queue()
    w.input_queue = queue.Queue()
    w.process = _Proc()
    assert w.send_request({'mode': mode, 'repo_id': 'alice/wuerfel', 'local_dir': '/data/x'})
    return w


def _progress(w, current, total=10, pct=None):
    w.progress_queue.put({'type': 'upload_progress', 'current': current, 'total': total,
                          'percentage': float(pct if pct is not None else 10.0 * current)})


def _error(w):
    w.progress_queue.put({'type': 'upload_error'})


def _poll(mod, w, seconds, step=0.5, each=None):
    """The node's 2 Hz status timer for `seconds`; returns every status."""
    out = []
    n = int(round(seconds / step))
    for i in range(n):
        mod._test_clock.t += step
        if each is not None:
            each(i)
        out.append(w.check_task_status())
    return out


def test_the_contract_constants(mod):
    assert mod.UPLOAD_STALL_S == 120.0
    assert mod.UPLOAD_HARD_STALL_S == 1800.0
    assert mod.UPLOAD_STALL_DE == record_texts_de.UPLOAD_STALL_DE


def test_errors_and_no_progress_for_120_s_fail_once_with_the_german_sentence(mod):
    w = _worker(mod)
    _progress(w, 3)
    statuses = _poll(mod, w, 119.0, each=lambda i: _error(w) if i % 20 == 0 else None)
    assert all(s['status'] == 'Uploading' for s in statuses)
    assert w.process is not None and not w.process.killed
    proc = w.process
    statuses = _poll(mod, w, 2.0, each=lambda i: _error(w))
    failed = [s for s in statuses if s['status'] == 'Failed']
    assert len(failed) == 1
    assert failed[0]['message'] == record_texts_de.UPLOAD_STALL_DE
    assert failed[0]['operation'] == 'upload'
    assert failed[0]['repo_id'] == 'alice/wuerfel'
    assert proc.killed is True                       # the child is terminated
    assert w.is_busy() is False
    # afterwards the worker is idle (the node starts a new one on the next request)
    assert w.check_task_status()['status'] == 'Idle'


def test_progress_resets_the_window(mod):
    w = _worker(mod)
    count = {'n': 0}

    def each(i):
        _error(w)
        if i % 100 == 0:            # a new report every 50 s
            count['n'] += 1
            _progress(w, count['n'])

    statuses = _poll(mod, w, 400.0, each=each)
    assert all(s['status'] == 'Uploading' for s in statuses)


def test_errors_older_than_the_window_do_not_count(mod):
    w = _worker(mod)
    _progress(w, 1)
    _error(w)
    _poll(mod, w, 5.0)
    _progress(w, 2)          # it moved again after that error
    # 195 s without progress: when the 120 s are up, the only error is 125 s old
    statuses = _poll(mod, w, 195.0)
    assert all(s['status'] == 'Uploading' for s in statuses)
    # … and one fresh error then ends it
    _error(w)
    statuses = _poll(mod, w, 1.0)
    assert [s['status'] for s in statuses].count('Failed') == 1


def test_slow_progress_without_errors_is_not_killed_before_1800_s(mod):
    w = _worker(mod)
    _progress(w, 1)
    statuses = _poll(mod, w, 1799.0, step=1.0)
    assert all(s['status'] == 'Uploading' for s in statuses)
    assert not w.process.killed
    statuses = _poll(mod, w, 2.0, step=1.0)
    assert [s['status'] for s in statuses].count('Failed') == 1
    assert w.process is None or w.process.killed
    assert w.check_task_status()['status'] == 'Idle'


def test_all_queue_items_are_drained_and_an_error_behind_progress_is_seen(mod):
    w = _worker(mod)
    _progress(w, 1)
    _poll(mod, w, 1.0)
    # progress first, then an error, then stale repeats of the same progress
    _progress(w, 2)
    _error(w)
    _progress(w, 2)
    _progress(w, 2)
    status = w.check_task_status()
    assert w.progress_queue.empty()
    assert status['progress']['current'] == 2
    # the error was not dropped by "keep only the latest"
    assert w.stall_watch.error_times == [mod._test_clock.t]
    assert w.stall_watch.last_progress_mono == mod._test_clock.t


def test_a_result_that_arrives_wins_over_the_stall(mod):
    w = _worker(mod)
    _progress(w, 1)
    _poll(mod, w, 100.0, each=lambda i: _error(w))
    mod._test_clock.t += 30.0
    w.output_queue.put(('success', 'Hugging Face-Upload abgeschlossen: alice/wuerfel'))
    status = w.check_task_status()
    assert status['status'] == 'Success'
    assert not w.process.killed


def test_downloads_are_not_judged(mod):
    w = _worker(mod, mode='download')
    w.progress_queue.put({'current': 1, 'total': 10, 'percentage': 10.0, 'is_downloading': True})
    statuses = _poll(mod, w, 2000.0, step=10.0, each=lambda i: _error(w))
    assert all(s['status'] == 'Downloading' for s in statuses)


def test_a_new_request_starts_a_fresh_window(mod):
    w = _worker(mod)
    _poll(mod, w, 119.0, each=lambda i: _error(w))
    w.output_queue.put(('success', 'ok'))
    assert w.check_task_status()['status'] == 'Success'
    assert w.send_request({'mode': 'upload', 'repo_id': 'alice/b', 'local_dir': '/data/b'})
    statuses = _poll(mod, w, 60.0, each=lambda i: _error(w))
    assert all(s['status'] == 'Uploading' for s in statuses)


# ── the child's forwarder ────────────────────────────────────────────────────

@pytest.fixture
def tracker(monkeypatch):
    if importlib.util.find_spec('tqdm') is None:          # the deps-free CI suite
        stub = types.ModuleType('tqdm')
        stub.tqdm = type('tqdm', (), {'__init__': lambda self, *a, **k: None})
        monkeypatch.setitem(sys.modules, 'tqdm', stub)
    spec = importlib.util.spec_from_file_location('_progress_tracker_stall_test',
                                                  _DP / 'progress_tracker.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    lib = logging.getLogger('huggingface_hub')
    for h in list(lib.handlers):
        if getattr(h, 'edubotics_upload_error_forwarder', False):
            lib.removeHandler(h)


def test_the_child_forwards_only_upload_large_folder_errors(tracker):
    q = queue.Queue()
    tracker.install_upload_error_forwarder(q)
    upl = logging.getLogger('huggingface_hub._upload_large_folder')
    upl.error('Failed to preupload LFS: [Errno 111] Connection refused')
    upl.error('Failed to commit: 503 Server Error')
    upl.warning('Failed to commit 50 files at once. Will retry with less files in next batch.')
    upl.info('hashed 3/10')
    logging.getLogger('huggingface_hub.file_download').error('Failed to download')
    logging.getLogger('somebody.else').error('Failed to preupload LFS')
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    assert items == [{'type': 'upload_error'}, {'type': 'upload_error'}]


def test_the_forwarder_attaches_once_and_follows_the_new_queue(tracker):
    q1, q2 = queue.Queue(), queue.Queue()
    tracker.install_upload_error_forwarder(q1)
    tracker.install_upload_error_forwarder(q2)
    lib = logging.getLogger('huggingface_hub')
    assert sum(1 for h in lib.handlers if getattr(h, 'edubotics_upload_error_forwarder', False)) == 1
    logging.getLogger('huggingface_hub._upload_large_folder').error('Failed to get upload mode: x')
    assert q1.empty() and q2.get_nowait() == {'type': 'upload_error'}


def test_a_verbosity_above_error_still_lets_the_errors_through(tracker):
    lib = logging.getLogger('huggingface_hub')
    upl = logging.getLogger('huggingface_hub._upload_large_folder')
    before = (lib.level, upl.level)
    lib.setLevel(logging.CRITICAL)            # HF_HUB_VERBOSITY=critical
    upl.setLevel(logging.NOTSET)
    try:
        q = queue.Queue()
        tracker.install_upload_error_forwarder(q)
        upl.error('Failed to compute sha256: x')
        assert q.get_nowait() == {'type': 'upload_error'}
    finally:
        lib.setLevel(before[0])
        upl.setLevel(before[1])


def test_a_full_queue_never_breaks_the_upload(tracker):
    q = queue.Queue(maxsize=1)
    q.put('occupied')
    tracker.install_upload_error_forwarder(q)
    logging.getLogger('huggingface_hub._upload_large_folder').error('Failed to commit: x')
    assert q.qsize() == 1

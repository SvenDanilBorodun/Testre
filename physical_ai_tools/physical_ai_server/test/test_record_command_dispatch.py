"""`/task/command` on the record path (Aufnahme 2.0, spec §2.6 item 5).

Extracts ``user_interaction_callback`` by ``ast`` and execs it onto a stub node
(physical_ai_server.py imports rclpy and cannot be imported here). Covers:

* MOVE_TO_NEXT's four single-task outcomes and their German answers;
* wire RERECORD accepted/refused;
* START_RECORD below the 3 GB disk floor refused BEFORE the `_mode_lock` claim;
* every changed message German when ``task_info.task_type == 'record'`` and
  byte-for-byte HEAD's text otherwise (the Inferenz page's ControlPanel reaches
  FINISH and the not-recording answer too, and must see nothing new);
* round 5 (O6): every record-command branch takes the recorder lock with a
  bounded try-acquire — a held lock answers BUSY_DE within ~0.25 s and changes
  nothing, never blocking the default callback group; F9: START sets the
  data-gate clock BEFORE the timer starts and moves the session generation.
"""

from __future__ import annotations

import ast
import contextlib
import textwrap
import threading
import time
import types
from pathlib import Path

import pytest

from physical_ai_server import signal_status as real_signal_status

_SERVER_PY = (
    Path(__file__).resolve().parents[1] / 'physical_ai_server' / 'physical_ai_server.py'
)


class _Req:
    IDLE, START_RECORD, START_INFERENCE, STOP, MOVE_TO_NEXT = 0, 1, 2, 3, 4
    RERECORD, FINISH, SKIP_TASK = 5, 6, 7
    RESUME_TELEOP, HOME_FOLLOWER, FORCE_RESUME_TELEOP = 8, 9, 10


_SendCommand = types.SimpleNamespace(Request=_Req)


class _Disk:
    free = 50_000_000_000
    calls = 0


def _disk_free_bytes(path):
    _Disk.calls += 1
    return _Disk.free


def _signal_status_ns():
    ns = types.SimpleNamespace(**{k: getattr(real_signal_status, k)
                                  for k in dir(real_signal_status)
                                  if not k.startswith('__')})
    ns.disk_free_bytes = _disk_free_bytes
    return ns


def _module_constant(name):
    tree = ast.parse(_SERVER_PY.read_text(encoding='utf-8'))
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == name):
            return ast.literal_eval(node.value)
    raise AssertionError(f'{name} not found')


_BUSY_DE = 'Die Aufnahme ist gerade beschäftigt. Bitte versuch es gleich noch einmal.'


def _load(names=('user_interaction_callback', '_record_command_lock')):
    source = _SERVER_PY.read_text(encoding='utf-8')
    tree = ast.parse(source)
    ns = {
        'SendCommand': _SendCommand,
        'time': time,
        'threading': threading,
        'contextlib': contextlib,
        'signal_status': _signal_status_ns(),
        'COMMAND_LOCK_TIMEOUT_S': _module_constant('COMMAND_LOCK_TIMEOUT_S'),
        'BUSY_DE': _BUSY_DE,
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            src = textwrap.dedent(ast.get_source_segment(source, node))
            exec(compile(src, str(_SERVER_PY), 'exec'), ns)  # noqa: S102
    missing = [n for n in names if n not in ns]
    assert not missing, f'not found: {missing}'
    return ns


_NS = _load()
_CALLBACK = _NS['user_interaction_callback']


class _Logger:
    def __init__(self):
        self.lines = []

    def info(self, msg, *a, **k):
        self.lines.append(('info', str(msg)))

    def warning(self, msg, *a, **k):
        self.lines.append(('warning', str(msg)))

    warn = warning

    def error(self, msg, *a, **k):
        self.lines.append(('error', str(msg)))


class _CountingLock:
    def __init__(self):
        self.entered = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *exc):
        return False


class _DM:
    def __init__(self, early='save', rerecord=True, raise_on=None):
        self.early = early
        self.rerecord = rerecord
        self.raise_on = raise_on
        self.calls = []
        self._session_marker_enabled = False
        self.lock = threading.RLock()

    @contextlib.contextmanager
    def try_locked(self, timeout):
        # The DataManager's contract: True while held, False after `timeout`.
        if not self.lock.acquire(timeout=timeout):
            yield False
            return
        try:
            yield True
        finally:
            self.lock.release()

    def _call(self, name, ret=None):
        self.calls.append(name)
        if self.raise_on == name:
            raise RuntimeError('boom')
        return ret

    def record_early_save(self):
        return self._call('record_early_save', self.early)

    def rerecord_from_command(self):
        return self._call('rerecord_from_command', self.rerecord)

    def re_record(self):
        return self._call('re_record')

    def record_stop(self):
        return self._call('record_stop')

    def record_finish(self):
        return self._call('record_finish')

    def record_skip_task(self):
        return self._call('record_skip_task')

    def record_next_episode(self):
        return self._call('record_next_episode')


class _Node:
    DEFAULT_SAVE_ROOT_PATH = Path('/nonexistent/edubotics/datasets')

    def __init__(self, *, recording=False, inferring=False, dm=None):
        self.communicator = object()
        self.on_recording = recording
        self.on_inference = inferring
        self.data_manager = dm
        self._mode_lock = _CountingLock()
        self._record_publish_lock = _CountingLock()
        self._record_session_gen = 0
        self.start_recording_time = 0.0
        self.logger = _Logger()
        self.init_calls = []
        self.init_saw = []
        self.operation_mode = 'collection'
        self._record_command_lock = types.MethodType(_NS['_record_command_lock'], self)

    def get_logger(self):
        return self.logger

    def _assert_no_other_active(self, mode):
        return True, ''

    def init_robot_control_parameters_from_user_task(self, task_info):
        # What the record timer's first tick would read when init starts it.
        self.init_saw.append((self.start_recording_time, self._record_session_gen))
        self.init_calls.append(task_info)
        self.data_manager = _DM()


def _request(command, task_type='record', instructions=1):
    task_info = types.SimpleNamespace(
        task_type=task_type,
        task_instruction=['Greife den Würfel.'] * instructions,
    )
    return types.SimpleNamespace(command=command, task_info=task_info)


def _call(node, request):
    response = types.SimpleNamespace(success=None, message=None)
    return types.MethodType(_CALLBACK, node)(request, response)


@pytest.fixture(autouse=True)
def _reset_disk():
    _Disk.free = 50_000_000_000
    _Disk.calls = 0
    yield


# ── MOVE_TO_NEXT ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize('outcome,success,message', [
    ('run', True, 'Die Aufnahme startet jetzt.'),
    ('save', True, 'Die Episode wird gespeichert.'),
    ('too_early', False, 'Die Episode läuft erst seit weniger als einer Sekunde.'),
    ('', False, 'Gerade gibt es nichts zu überspringen oder zu speichern.'),
])
def test_move_to_next_single_task_outcomes(outcome, success, message):
    node = _Node(recording=True, dm=_DM(early=outcome))
    r = _call(node, _request(_Req.MOVE_TO_NEXT))
    assert (r.success, r.message) == (success, message)
    assert node.data_manager.calls == ['record_early_save']


def test_move_to_next_multi_task_is_unchanged():
    node = _Node(recording=True, dm=_DM())
    r = _call(node, _request(_Req.MOVE_TO_NEXT, instructions=2))
    assert (r.success, r.message) == (True, 'Moved to next episode')
    assert node.data_manager.calls == ['record_next_episode']


def test_move_to_next_outside_the_record_page_keeps_heads_text():
    node = _Node(inferring=True, dm=_DM(early=''))
    r = _call(node, _request(_Req.MOVE_TO_NEXT, task_type='inference'))
    assert (r.success, r.message) == (True, 'Moved to next episode')


# ── RERECORD ─────────────────────────────────────────────────────────────────

def test_rerecord_accepted():
    node = _Node(recording=True, dm=_DM(rerecord=True))
    r = _call(node, _request(_Req.RERECORD))
    assert (r.success, r.message) == (True, 'Die Episode wird wiederholt.')
    assert node.data_manager.calls == ['rerecord_from_command']


def test_rerecord_refused_once_committed():
    node = _Node(recording=True, dm=_DM(rerecord=False))
    r = _call(node, _request(_Req.RERECORD))
    assert (r.success, r.message) == (
        False, 'Die Episode ist schon gespeichert und kann nicht mehr verworfen werden.')


def test_rerecord_outside_the_record_page_keeps_heads_path():
    node = _Node(inferring=True, dm=_DM())
    r = _call(node, _request(_Req.RERECORD, task_type='inference'))
    assert (r.success, r.message) == (True, 'Re-recording current episode')
    assert node.data_manager.calls == ['re_record']


# ── START_RECORD + disk floor ────────────────────────────────────────────────

def test_start_below_the_disk_floor_is_refused_before_the_mode_lock():
    _Disk.free = 2_400_000_000
    node = _Node()
    r = _call(node, _request(_Req.START_RECORD))
    assert r.success is False
    assert r.message == (
        'Nur noch 2,4 GB frei. Zum Aufnehmen sind mindestens 3,0 GB nötig. '
        'Lösche alte Datensätze im Tab Daten.')
    assert node._mode_lock.entered == 0
    assert node.on_recording is False
    assert node.init_calls == []


def test_start_above_the_floor_claims_and_answers_in_german():
    node = _Node()
    r = _call(node, _request(_Req.START_RECORD))
    assert (r.success, r.message) == (True, 'Aufnahme gestartet.')
    assert node._mode_lock.entered == 1
    assert node.on_recording is True
    assert node.data_manager._session_marker_enabled is True
    assert _Disk.calls == 1


def test_start_with_an_unreadable_disk_is_allowed():
    _Disk.free = None
    node = _Node()
    r = _call(node, _request(_Req.START_RECORD))
    assert r.success is True


def test_start_heads_text_for_a_non_record_task_type():
    node = _Node()
    r = _call(node, _request(_Req.START_RECORD, task_type=''))
    assert (r.success, r.message) == (True, 'Recording started')


@pytest.mark.parametrize('task_type,message', [
    ('record', 'Die laufende Episode wird neu aufgenommen.'),
    ('', 'Restarting the recording.'),
])
def test_start_while_recording_restarts(task_type, message):
    _Disk.free = 1          # the restart branch comes BEFORE the disk check
    node = _Node(recording=True, dm=_DM())
    r = _call(node, _request(_Req.START_RECORD, task_type=task_type))
    assert (r.success, r.message) == (True, message)
    assert node.data_manager.calls == ['re_record']


# ── the rest of the record commands: German iff record ────────────────────────

@pytest.mark.parametrize('command,german,head', [
    (_Req.STOP, 'Aufnahme gestoppt.', 'Recording stopped'),
    (_Req.FINISH, 'Wird beendet.', 'All operations terminated'),
    (_Req.SKIP_TASK, 'Aufgabe übersprungen.', 'Task skipped successfully'),
])
def test_record_commands_german_iff_record(command, german, head):
    node = _Node(recording=True, dm=_DM())
    r = _call(node, _request(command))
    assert (r.success, r.message) == (True, german)
    node = _Node(inferring=True, dm=_DM())
    r = _call(node, _request(command, task_type='inference'))
    assert (r.success, r.message) == (True, head)


def test_finish_still_clears_on_inference():
    node = _Node(inferring=True, dm=_DM())
    _call(node, _request(_Req.FINISH, task_type='inference'))
    assert node.on_inference is False


def test_not_recording_answer():
    r = _call(_Node(dm=_DM()), _request(_Req.FINISH))
    assert (r.success, r.message) == (False, 'Gerade läuft keine Aufnahme.')
    r = _call(_Node(dm=_DM()), _request(_Req.FINISH, task_type='inference'))
    assert (r.success, r.message) == (False, 'Not currently recording')


def test_outer_except_is_german_without_the_exception_text():
    node = _Node(recording=True, dm=_DM(raise_on='record_finish'))
    r = _call(node, _request(_Req.FINISH))
    assert r.success is False
    assert r.message == (
        'Der Befehl konnte nicht ausgeführt werden. Bitte versuch es noch einmal.')
    assert 'boom' not in r.message
    assert any('boom' in line for _, line in node.logger.lines)


def test_outer_except_keeps_heads_text_outside_the_record_page():
    node = _Node(inferring=True, dm=_DM(raise_on='record_finish'))
    r = _call(node, _request(_Req.FINISH, task_type='inference'))
    assert (r.success, r.message) == (False, 'Error in user interaction: boom')


# ── round 5: the busy answer (O6) and F9 ──────────────────────────────────────

def test_the_command_lock_timeout_is_a_quarter_second():
    COMMAND_LOCK_TIMEOUT_S = _module_constant('COMMAND_LOCK_TIMEOUT_S')
    assert COMMAND_LOCK_TIMEOUT_S == 0.25
    assert _module_constant('BUSY_DE') == _BUSY_DE


def _hold(dm):
    held, release = threading.Event(), threading.Event()

    def _holder():
        with dm.lock:
            held.set()
            release.wait(5)
    t = threading.Thread(target=_holder)
    t.start()
    assert held.wait(5)
    return t, release


@pytest.mark.parametrize('command', [
    _Req.STOP, _Req.MOVE_TO_NEXT, _Req.RERECORD, _Req.FINISH, _Req.SKIP_TASK])
def test_a_held_recorder_lock_answers_busy_and_changes_nothing(command):
    dm = _DM()
    node = _Node(recording=True, dm=dm)
    t, release = _hold(dm)
    try:
        started = time.monotonic()
        r = _call(node, _request(command))
        elapsed = time.monotonic() - started
    finally:
        release.set()
        t.join(5)
    assert (r.success, r.message) == (False, _BUSY_DE)
    assert elapsed < 0.3
    assert dm.calls == []


def test_start_while_recording_answers_busy_and_changes_nothing():
    dm = _DM()
    node = _Node(recording=True, dm=dm)
    t, release = _hold(dm)
    try:
        r = _call(node, _request(_Req.START_RECORD))
    finally:
        release.set()
        t.join(5)
    assert (r.success, r.message) == (False, _BUSY_DE)
    assert dm.calls == []


def test_a_free_lock_is_held_for_the_transition():
    dm = _DM()
    seen = []
    dm.record_finish = lambda: seen.append(dm.lock._is_owned())
    node = _Node(recording=True, dm=dm)
    r = _call(node, _request(_Req.FINISH))
    assert r.success is True
    assert seen == [True]
    assert not dm.lock._is_owned()


def test_a_data_manager_without_try_locked_still_works():
    dm = _DM()
    dm.try_locked = None
    node = _Node(recording=True, dm=dm)
    r = _call(node, _request(_Req.STOP))
    assert (r.success, r.message) == (True, 'Aufnahme gestoppt.')


def test_start_sets_the_gate_clock_before_the_timer_and_moves_the_generation():
    node = _Node()
    before = time.perf_counter()
    r = _call(node, _request(_Req.START_RECORD))
    assert r.success is True
    (clock_at_init, gen_at_init), = node.init_saw
    assert clock_at_init >= before          # F9: set BEFORE init starts the timer
    assert gen_at_init == 1                 # the new session owns /task/status
    assert node._record_publish_lock.entered == 1
    assert node.start_recording_time == clock_at_init


def test_degraded_boot_guard_is_unchanged():
    node = _Node()
    node.communicator = None
    r = _call(node, _request(_Req.START_RECORD))
    assert r.success is False
    assert r.message.startswith('Roboter-Initialisierung fehlgeschlagen')

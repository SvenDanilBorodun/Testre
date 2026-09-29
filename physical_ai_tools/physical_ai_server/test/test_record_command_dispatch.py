"""`/task/command` on the record path (Aufnahme 2.0, spec §2.6 item 5).

Extracts ``user_interaction_callback`` by ``ast`` and execs it onto a stub node
(physical_ai_server.py imports rclpy and cannot be imported here). Covers:

* MOVE_TO_NEXT's four single-task outcomes and their German answers;
* wire RERECORD accepted/refused;
* START_RECORD below the 3 GB disk floor refused BEFORE the `_mode_lock` claim;
* every changed message German when ``task_info.task_type == 'record'`` and
  byte-for-byte HEAD's text otherwise (the Inferenz page's ControlPanel reaches
  FINISH and the not-recording answer too, and must see nothing new).
"""

from __future__ import annotations

import ast
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


def _load():
    source = _SERVER_PY.read_text(encoding='utf-8')
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (isinstance(node, ast.FunctionDef)
                and node.name == 'user_interaction_callback'):
            src = textwrap.dedent(ast.get_source_segment(source, node))
            ns = {
                'SendCommand': _SendCommand,
                'time': time,
                'threading': threading,
                'signal_status': _signal_status_ns(),
            }
            exec(compile(src, str(_SERVER_PY), 'exec'), ns)  # noqa: S102
            return ns['user_interaction_callback']
    raise AssertionError('user_interaction_callback not found')


_CALLBACK = _load()


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
        self.logger = _Logger()
        self.init_calls = []
        self.operation_mode = 'collection'

    def get_logger(self):
        return self.logger

    def _assert_no_other_active(self, mode):
        return True, ''

    def init_robot_control_parameters_from_user_task(self, task_info):
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


def test_degraded_boot_guard_is_unchanged():
    node = _Node()
    node.communicator = None
    r = _call(node, _request(_Req.START_RECORD))
    assert r.success is False
    assert r.message.startswith('Roboter-Initialisierung fehlgeschlagen')

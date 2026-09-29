"""The record timer tick (Aufnahme 2.0, spec §2.6 item 4 + the German table).

Extracts ``_data_collection_timer_callback`` and ``_check_recording_disk_floor``
by ``ast`` and execs them onto a stub node. Covers:

* H14 — a FINISHING session (DataManager status 'finish'/'stop') writes no
  frames, so it skips every sensor gate and calls ``record(None, None, None)``;
  a camera unplugged right after „Beenden“ can no longer turn the FINISH into a
  5-s error stop without finalize;
* the terminating READY tick carries ``[WARNUNG] <reason>`` iff the upload was
  blocked (``_upload_blocked_reason_de``), and ``''`` otherwise;
* the 1 Hz critical-disk check: throttled, only in warmup/run/reset, skipped
  while finishing, never raises;
* every error stop and the camera-rate warning in German (no exception text).
"""

from __future__ import annotations

import ast
import copy
import textwrap
import types
from pathlib import Path

import pytest

from physical_ai_server import signal_status as real_signal_status

_SERVER_PY = (
    Path(__file__).resolve().parents[1] / 'physical_ai_server' / 'physical_ai_server.py'
)


class _TaskStatus:
    READY, WARMING_UP, RESETTING, RECORDING, SAVING, STOPPED = 0, 1, 2, 3, 4, 5

    def __init__(self):
        self.phase = -1
        self.error = ''
        self.total_time = 0
        self.proceed_time = 0


class _Clock:
    t = 1000.0

    @classmethod
    def perf_counter(cls):
        return cls.t

    monotonic = perf_counter


class _Disk:
    free = 50_000_000_000
    calls = 0


def _disk_free_bytes(path):
    _Disk.calls += 1
    return _Disk.free


def _camera_name_de(name):
    return {'gripper': 'Greifer-Kamera', 'scene': 'Szenen-Kamera'}.get(
        name, f'Kamera „{name}“')


def _load(name, ns):
    source = _SERVER_PY.read_text(encoding='utf-8')
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            src = textwrap.dedent(ast.get_source_segment(source, node))
            exec(compile(src, str(_SERVER_PY), 'exec'), ns)  # noqa: S102
            return ns[name]
    raise AssertionError(f'{name} not found')


def _namespace():
    ss = types.SimpleNamespace(**{k: getattr(real_signal_status, k)
                                  for k in dir(real_signal_status)
                                  if not k.startswith('__')})
    ss.disk_free_bytes = _disk_free_bytes
    return {'TaskStatus': _TaskStatus, 'time': _Clock, 'signal_status': ss,
            'camera_name_de': _camera_name_de}


_NS = _namespace()
_TICK = _load('_data_collection_timer_callback', _NS)
_DISK = _load('_check_recording_disk_floor', _NS)


class _Logger:
    def __init__(self):
        self.lines = []

    def info(self, msg, *a, **k):
        self.lines.append(str(msg))

    warning = warn = error = info


class _Comm:
    def __init__(self, data=('cams', 'follower', 'leader')):
        self.data = data
        self.published = []
        self.get_latest_data_calls = 0
        self.camera_topic_msgs = {'gripper': None, 'scene': None}
        self.observed_hz = None
        self.joystick_state = {'updated': False, 'mode': None}

    def get_latest_data(self):
        self.get_latest_data_calls += 1
        return self.data

    def publish_status(self, status):
        # A real publish serialises NOW; the node mutates the same object into
        # the terminating READY right after, so keep a snapshot.
        self.published.append(copy.copy(status))

    def get_camera_observed_hz(self, name, window_s):
        return self.observed_hz


class _DM:
    def __init__(self, status='run', completes=False, blocked=''):
        self.status = status
        self.completes = completes
        self._upload_blocked_reason_de = blocked
        self._last_warning_message = ''
        self.record_calls = []
        self.low_disk = []
        self.convert_raises = False
        self.dataset_ok = True
        self.record_raises = False

    def get_status(self):
        return self.status

    def convert_msgs_to_raw_datas(self, cams, follower, order, leader, joint_order):
        if self.convert_raises:
            raise ValueError('bad msg 0xdead')
        return 'camera_data', 'follower_data', 'leader_data'

    def check_lerobot_dataset(self, camera_data, order):
        return self.dataset_ok

    def record(self, images, state, action):
        self.record_calls.append((images, state, action))
        if self.record_raises:
            raise RuntimeError('encoder thread died: /dev/full')
        return self.completes

    def get_current_record_status(self):
        st = _TaskStatus()
        st.phase = _TaskStatus.SAVING if self.status in ('finish', 'stop', 'save') \
            else _TaskStatus.RECORDING
        st.error = '[WARNUNG] vorher' if self.completes else ''
        return st

    def should_record_rosbag2(self):
        return False

    def finish_for_low_disk(self, message_de):
        self.low_disk.append(message_de)
        return True


class _Node:
    DEFAULT_TOPIC_TIMEOUT = 5.0
    DEFAULT_SAVE_ROOT_PATH = Path('/nonexistent/edubotics/datasets')

    def __init__(self, dm, comm=None):
        self.data_manager = dm
        self.communicator = comm or _Comm()
        self.on_recording = True
        self.operation_mode = 'collection'
        self.start_recording_time = _Clock.t
        self.task_info = types.SimpleNamespace(fps=30)
        self.total_joint_order = ['j1']
        self.joint_order = {'joint_order.leader': ['j1']}
        self.timer_stops = []
        self.timer_manager = types.SimpleNamespace(
            stop=lambda timer_name: self.timer_stops.append(timer_name))
        self.logger = _Logger()
        self._check_recording_disk_floor = types.MethodType(_DISK, self)

    def get_logger(self):
        return self.logger

    def handle_joystick_trigger(self, joystick_mode):
        pass

    def handle_rosbag_recording(self):
        pass


def _tick(node):
    types.MethodType(_TICK, node)()


@pytest.fixture(autouse=True)
def _reset():
    _Clock.t = 1000.0
    _Disk.free = 50_000_000_000
    _Disk.calls = 0
    yield


# ── H14: a finishing session never waits for sensor data ─────────────────────

@pytest.mark.parametrize('status', ['finish', 'stop'])
def test_finishing_session_skips_every_gate_and_records_none(status):
    dm = _DM(status=status, completes=True)
    node = _Node(dm, _Comm(data=(None, None, None)))     # camera unplugged
    _Clock.t += 10.0                                      # past the 5 s timeout
    _tick(node)
    assert node.communicator.get_latest_data_calls == 0
    assert dm.record_calls == [(None, None, None)]
    assert _Disk.calls == 0                               # no disk check either
    last = node.communicator.published[-1]
    assert last.phase == _TaskStatus.READY
    assert last.error == ''
    assert node.on_recording is False
    assert node.timer_stops == ['collection']


def test_finishing_tick_before_completion_publishes_the_record_status():
    dm = _DM(status='finish', completes=False)
    node = _Node(dm, _Comm(data=(None, None, None)))
    _tick(node)
    assert [s.phase for s in node.communicator.published] == [_TaskStatus.SAVING]
    assert node.on_recording is True
    assert node.timer_stops == []


def test_terminating_ready_carries_the_blocked_upload_reason():
    dm = _DM(status='finish', completes=True,
             blocked='Upload abgelehnt: Der Roboter darf nicht …')
    node = _Node(dm)
    _tick(node)
    last = node.communicator.published[-1]
    assert last.phase == _TaskStatus.READY
    assert last.error == '[WARNUNG] Upload abgelehnt: Der Roboter darf nicht …'
    assert last.total_time == 0 and last.proceed_time == 0


def test_terminating_ready_without_a_blocked_upload_carries_no_warning():
    # The SAVING tick before it may carry a warning; the READY no longer
    # inherits it (only a blocked upload rides the terminating tick).
    dm = _DM(status='run', completes=True)
    node = _Node(dm)
    _tick(node)
    saving, ready = node.communicator.published[-2:]
    assert saving.error == '[WARNUNG] vorher'
    assert ready.phase == _TaskStatus.READY
    assert ready.error == ''


# ── disk floor ───────────────────────────────────────────────────────────────

def test_disk_check_below_critical_finishes_with_the_sentence():
    _Disk.free = 900_000_000
    dm = _DM(status='run')
    node = _Node(dm)
    _tick(node)
    assert dm.low_disk == [real_signal_status.disk_critical_stop_de(900_000_000)]
    # The tick still records this frame; the finish rides the next ticks.
    assert dm.record_calls == [('camera_data', 'follower_data', 'leader_data')]


def test_disk_check_is_throttled_to_1_hz():
    dm = _DM(status='run')
    node = _Node(dm)
    _tick(node)
    _Clock.t += 0.5
    _tick(node)
    assert _Disk.calls == 1
    _Clock.t += 0.6
    _tick(node)
    assert _Disk.calls == 2


@pytest.mark.parametrize('status', ['save'])
def test_disk_check_only_in_warmup_run_reset(status):
    _Disk.free = 1
    dm = _DM(status=status)
    node = _Node(dm)
    _tick(node)
    assert dm.low_disk == []


@pytest.mark.parametrize('status', ['warmup', 'run', 'reset'])
def test_disk_check_active_states(status):
    _Disk.free = 1
    dm = _DM(status=status)
    node = _Node(dm)
    _tick(node)
    assert len(dm.low_disk) == 1


def test_disk_check_never_raises():
    def _boom(path):
        raise RuntimeError('statvfs exploded')

    original = _NS['signal_status'].disk_free_bytes
    _NS['signal_status'].disk_free_bytes = _boom
    try:
        dm = _DM(status='run')
        node = _Node(dm)
        _tick(node)                       # must not raise
        assert dm.record_calls
    finally:
        _NS['signal_status'].disk_free_bytes = original


def test_unreadable_disk_does_not_stop_the_recording():
    _Disk.free = None
    dm = _DM(status='run')
    _tick(_Node(dm))
    assert dm.low_disk == []


# ── German error stops ───────────────────────────────────────────────────────

@pytest.mark.parametrize('data,sentence', [
    ((None, None, None),
     'Die Kameras senden keine Bilder, die Aufnahme wurde gestoppt. Prüfe die '
     'Kabel und starte die Umgebung neu, wenn es so bleibt.'),
    (('cams', None, None),
     'Der Follower-Arm sendet keine Daten, die Aufnahme wurde gestoppt. Prüfe '
     'Kabel und Stromversorgung des Follower-Arms.'),
    (('cams', 'follower', None),
     'Der Leader-Arm sendet keine Daten, die Aufnahme wurde gestoppt. Ist der '
     'Roboter auf der Startseite aktiviert und der Leader-Arm eingeschaltet?'),
])
def test_missing_source_after_the_timeout(data, sentence):
    dm = _DM(status='warmup')
    node = _Node(dm, _Comm(data=data))
    _tick(node)                           # inside the 5 s window: wait silently
    assert node.communicator.published == []
    _Clock.t += 5.5
    _tick(node)
    last = node.communicator.published[-1]
    assert last.phase == _TaskStatus.READY
    assert last.error == sentence
    assert node.on_recording is False
    assert node.timer_stops == ['collection']


def test_convert_failure_is_german_without_the_exception():
    dm = _DM()
    dm.convert_raises = True
    node = _Node(dm)
    _tick(node)
    last = node.communicator.published[-1]
    assert last.error == (
        'Die Sensordaten konnten nicht gelesen werden, die Aufnahme wurde '
        'gestoppt. Bitte starte die Umgebung neu.')
    assert any('0xdead' in line for line in node.logger.lines)


def test_dataset_init_fallback_is_cause_neutral_german():
    dm = _DM()
    dm.dataset_ok = False
    node = _Node(dm)
    _tick(node)
    assert node.communicator.published[-1].error == (
        'Der Datensatz konnte nicht angelegt oder geöffnet werden. Prüfe die '
        'Internetverbindung, schalte unter „Erweitert“ das Hochladen aus oder '
        'wähle einen anderen Aufgabennamen.')


def test_dataset_init_prefers_a_specific_german_warning():
    dm = _DM()
    dm.dataset_ok = False
    dm._last_warning_message = 'Etwas Bestimmtes.'
    node = _Node(dm)
    _tick(node)
    assert node.communicator.published[-1].error == 'Etwas Bestimmtes.'
    assert dm._last_warning_message == ''


def test_record_exception_is_german_without_the_exception():
    dm = _DM()
    dm.record_raises = True
    node = _Node(dm)
    _tick(node)
    err = node.communicator.published[-1].error
    assert err.startswith('Aufnahme gestoppt: Frame konnte nicht gespeichert werden. ')
    assert 'Häufige Ursachen' in err
    assert '/dev/full' not in err
    assert any('/dev/full' in line for line in node.logger.lines)


def test_camera_rate_warning_is_german():
    dm = _DM()
    comm = _Comm()
    comm.camera_topic_msgs = {'gripper': None}
    comm.observed_hz = 11.2
    node = _Node(dm, comm)
    node.start_recording_time = _Clock.t - 2.0
    _tick(node)
    assert dm._last_warning_message == (
        'Die Greifer-Kamera liefert nur 11 statt 30 Bilder pro Sekunde. Der '
        'Datensatz enthält wiederholte Bilder. Steck die Kamera direkt am PC ein '
        'oder prüfe das Kabel.')

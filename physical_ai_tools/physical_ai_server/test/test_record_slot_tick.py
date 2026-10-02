"""The record tick with the slot sampler (Aufnahme 2.0 round 5, spec §3.3, §6).

``physical_ai_server.py`` imports rclpy and cannot be imported here, so the tick
and its helpers are extracted by ``ast`` and exec'd onto a stub node (the
test_record_timer_finishing idiom). Covers:

* during a take, ONE record() per slot the sampler decided at or after the
  take's run entry, in order, from the decision's own messages — the latest
  frames are not decoded; the loop stops when the take ends or a collision
  trip is pending;
* the sampler is asked before the recorder lock, never under it;
* the take's capture integrity restarts at every run entry, its gap verdict is
  handed to the DataManager after every frame, and its German C5 warning rides
  only the tick that committed the take and only when no other warning waits;
* R5-2 rule 1: a required source silent >= SOURCE_STOPPED_S ends the session,
  judged only on an ON-TIME tick in warm-up/run/reset; rule 3: the data-gate
  timeout once a dataset exists ends it the same way.
"""

from __future__ import annotations

import ast
import contextlib
import copy
import json
import textwrap
import threading
import types
from pathlib import Path

import pytest

from physical_ai_server import signal_status as real_signal_status
from physical_ai_server.data_processing import record_texts_de

_SERVER_PY = (
    Path(__file__).resolve().parents[1] / 'physical_ai_server' / 'physical_ai_server.py'
)
_SRC = _SERVER_PY.read_text(encoding='utf-8')


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


class _SlotConvertError(Exception):
    pass


def _module_constant(name):
    for node in ast.parse(_SRC).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == name):
            return ast.literal_eval(node.value)
    raise AssertionError(f'{name} not found')


def _namespace():
    ss = types.SimpleNamespace(**{k: getattr(real_signal_status, k)
                                  for k in dir(real_signal_status)
                                  if not k.startswith('__')})
    ss.disk_free_bytes = lambda path: 50_000_000_000
    return {'TaskStatus': _TaskStatus, 'time': _Clock, 'signal_status': ss,
            'contextlib': contextlib, 'record_texts_de': record_texts_de,
            '_SlotConvertError': _SlotConvertError, 'json': json,
            'ON_TIME_TICK_PERIODS': _module_constant('ON_TIME_TICK_PERIODS')}


_NS = _namespace()
_NAMES = ('_data_collection_timer_callback', '_check_recording_disk_floor',
          '_record_tick_owns_session', '_publish_record_status', '_end_record_with_error',
          '_record_tick_on_time', '_decide_due_slots', '_stopped_required_source',
          '_missing_source_name', '_open_take_if_new', '_close_take_if_done', '_log_take',
          '_record_decided_slots')
for _node in ast.walk(ast.parse(_SRC)):
    if isinstance(_node, ast.FunctionDef) and _node.name in _NAMES:
        exec(compile(textwrap.dedent(ast.get_source_segment(_SRC, _node)),  # noqa: S102
                     str(_SERVER_PY), 'exec'), _NS)
assert all(name in _NS for name in _NAMES)


def test_the_on_time_rule_is_one_and_a_half_periods():
    ON_TIME_TICK_PERIODS = _module_constant('ON_TIME_TICK_PERIODS')
    assert ON_TIME_TICK_PERIODS == 1.5


# ── stubs ────────────────────────────────────────────────────────────────────

def _decision(g, seq, *, lea=True):
    """A decision in the sampler's shape; the messages are tokens."""
    return {
        'k': seq, 'g': g, 'r': g + 0.01,
        'cams': {'scene': {'item': (seq, g, g, f'scene#{seq}'), 'repeat': False,
                           'skip': 0, 'locked': True, 'rate': 30.0, 'step': 1}},
        'fol': (seq, g, g + 0.005, f'fol#{seq}'),
        'lea': (seq, g, g + 0.006, f'lea#{seq}') if lea else None,
        'timed_out': False, 'late': False, 'decided_at': g + 0.2,
        'lost_before': [], 'gaps': {},
    }


class _Sampler:
    def __init__(self, dm, scripts):
        self.dm = dm
        self.scripts = list(scripts)
        self.calls = []
        self.lost = 0

    def decide_due(self, now, cams, follower, leader):
        self.calls.append((now, self.dm.lock._is_owned()))
        return self.scripts.pop(0) if self.scripts else []


class _Integrity:
    def __init__(self):
        self.resets = []
        self.added = []
        self.frames = 0
        self.gap = None
        self.warning = ''

    def reset(self, start_mono=None):
        self.resets.append(start_mono)
        self.frames = 0

    def add(self, decision, late=None):
        self.added.append(decision['g'])
        self.frames += 1

    def gap_source(self):
        return self.gap

    def warning_de(self, episode):
        return self.warning.format(n=episode)

    def as_log_dict(self):
        return {'frames': self.frames}


class _DM:
    """The DataManager surface the tick uses, with a tiny take FSM: 'run' adds
    one frame per record() with images until n_target, then 'save'; the next
    record() commits (or discards when told to)."""

    def __init__(self, status='run', n_target=3):
        self.status = status
        self.n_target = n_target
        self._lerobot_dataset = object()
        self._last_warning_message = ''
        self._upload_blocked_reason_de = ''
        self._on_saving = False
        self.run_entered_mono = None
        self.lock = threading.RLock()
        self.frames = []
        self.converted = []
        self.gap_notes = []
        self.source_stops = []
        self.commits = 0
        self.discard_on_save = False
        self.saved = 0
        self._added = False

    @contextlib.contextmanager
    def locked(self):
        with self.lock:
            yield self

    def get_status(self):
        return self.status

    def convert_msgs_to_raw_datas(self, cams, follower, order, leader, joint_order):
        self.converted.append((cams, follower, leader))
        if cams == 'latest-cams':
            return 'latest-images', 'latest-state', 'latest-action'
        return dict(cams), list(follower.values()), list((leader or {}).values())

    def check_lerobot_dataset(self, camera_data, order):
        return True

    def cancel_pending_discard(self):
        return False

    def record(self, images, state, action):
        self._added = False
        if self.status == 'run' and images is not None:
            self.frames.append(images)
            self._added = True
            if len(self.frames) >= self.n_target:
                self.status = 'save'
        elif self.status == 'save' and not self._on_saving:
            if self.discard_on_save:
                self.status = 'reset'
                self._last_warning_message = 'Signalaussetzer: andere Warnung.'
            else:
                self.commits += 1
                self._on_saving = True
        return False

    def added_frame(self):
        return self._added

    def commit_count(self):
        return self.commits

    def saved_episode_count(self):
        return self.saved

    def note_take_gap(self, gap):
        self.gap_notes.append(gap)

    def end_for_source_stop(self, sentence):
        self.source_stops.append(sentence)
        self.status = 'finish'
        return True

    def finish_for_low_disk(self, message):
        return False

    def end_after_error(self):
        return None                 # the D5 stub: as without a dataset

    def get_current_record_status(self):
        st = _TaskStatus()
        st.phase = (_TaskStatus.SAVING if self.status in ('save', 'finish', 'stop')
                    else _TaskStatus.RECORDING)
        if self._last_warning_message:
            st.error = f'[WARNUNG] {self._last_warning_message}'
            self._last_warning_message = ''
        return st

    def should_record_rosbag2(self):
        return False


class _Comm:
    def __init__(self):
        self.published = []
        self.camera_topic_msgs = {'scene': 'latest-scene'}
        self.follower_topic_msgs = {'follower': 'latest-fol'}
        self.leader_topic_msgs = {'leader': 'latest-lea'}
        self.joystick_state = {'updated': False, 'mode': None}
        self.counters = []
        self.latest = ('latest-cams', 'latest-fol', 'latest-lea')

    def get_latest_data(self):
        return self.latest

    def history_snapshots(self):
        return {'scene': []}, [], []

    def history_source_names(self):
        return 'follower', 'leader'

    def source_counters(self):
        return [dict(c) for c in self.counters]

    def publish_status(self, status):
        self.published.append(copy.copy(status))


class _Logger:
    def __init__(self):
        self.lines = []

    def info(self, msg, *a, **k):
        self.lines.append(str(msg))

    warning = warn = error = info


class _Node:
    DEFAULT_TOPIC_TIMEOUT = 5.0
    DEFAULT_SAVE_ROOT_PATH = Path('/nonexistent/edubotics/datasets')

    def __init__(self, dm, scripts=()):
        self.data_manager = dm
        self.communicator = _Comm()
        self.on_recording = True
        self.operation_mode = 'collection'
        self.start_recording_time = _Clock.t
        self.total_joint_order = ['j1']
        self.joint_order = {'joint_order.leader': ['j1']}
        self.timer_stops = []
        self.timer_manager = types.SimpleNamespace(
            stop=lambda timer_name: self.timer_stops.append(timer_name))
        self.logger = _Logger()
        self._record_publish_lock = threading.Lock()
        self._record_session_gen = 0
        self._collision_trip_pending = None
        self._record_fps = 30.0
        self._slot_sampler = _Sampler(dm, scripts)
        self._take_integrity = _Integrity()
        self._disk_check_last_mono = _Clock.t + 1e9      # no disk probing here
        for name in _NAMES[1:]:
            setattr(self, name, types.MethodType(_NS[name], self))

    def get_logger(self):
        return self.logger

    def handle_joystick_trigger(self, joystick_mode):
        pass

    def handle_rosbag_recording(self):
        pass

    def tick(self, dt=1 / 30):
        _Clock.t += dt
        types.MethodType(_NS['_data_collection_timer_callback'], self)()


@pytest.fixture(autouse=True)
def _reset():
    _Clock.t = 1000.0
    yield


def _running(n_target=3, scripts=(), entry=1000.0):
    dm = _DM(status='run', n_target=n_target)
    dm.run_entered_mono = entry
    return dm, _Node(dm, scripts)


# ── the take's frames come from the decided slots ────────────────────────────

def test_one_record_per_decided_slot_at_or_after_the_run_entry():
    dm, node = _running(n_target=10, scripts=[
        [_decision(999.95, 1), _decision(1000.0, 2), _decision(1000.0333, 3)]])
    node.tick()
    assert dm.frames == [{'scene': 'scene#2'}, {'scene': 'scene#3'}]
    assert node._take_integrity.added == [1000.0, 1000.0333]


def test_the_latest_frames_are_not_decoded_during_a_take():
    dm, node = _running(n_target=10, scripts=[[_decision(1000.0, 1)], [], []])
    for _ in range(3):
        node.tick()
    assert all(cams != 'latest-cams' for cams, _f, _l in dm.converted)
    cams, follower, leader = dm.converted[0]
    assert (cams, follower, leader) == ({'scene': 'scene#1'}, {'follower': 'fol#1'},
                                        {'leader': 'lea#1'})


def test_outside_a_take_the_latest_frames_are_recorded_as_before():
    dm = _DM(status='warmup')
    node = _Node(dm, scripts=[[_decision(1000.0, 1)]])
    node.tick()
    assert dm.converted[0][0] == 'latest-cams'
    assert dm.frames == []                         # warm-up adds no frame


def test_the_sampler_is_asked_every_tick_and_never_under_the_recorder_lock():
    dm, node = _running(n_target=10, scripts=[[], [], []])
    dm.status = 'reset'
    node.tick()
    dm.status = 'run'
    node.tick()
    node.tick()
    assert len(node._slot_sampler.calls) == 3
    assert all(owned is False for _now, owned in node._slot_sampler.calls)


def test_the_loop_stops_when_the_take_reaches_its_frame_count():
    dm, node = _running(n_target=2, scripts=[
        [_decision(1000.0, 1), _decision(1000.0333, 2)], [_decision(1000.0667, 3)]])
    node.tick()
    assert len(dm.frames) == 2 and dm.status == 'save'
    node.tick()                                     # 'save': commits, no frame
    assert len(dm.frames) == 2


def test_the_loop_stops_at_a_pending_collision_trip():
    dm, node = _running(n_target=10, scripts=[
        [_decision(1000.0, 1), _decision(1000.0333, 2)]])
    original = dm.record

    def _record_then_trip(images, state, action):
        result = original(images, state, action)
        node._collision_trip_pending = object()      # latched on the sensor thread
        return result
    dm.record = _record_then_trip
    node.tick()
    assert len(dm.frames) == 1


def test_a_failed_sampler_falls_back_to_the_latest_frame():
    dm, node = _running(n_target=10)

    def _boom(*a, **k):
        raise RuntimeError('sampler bug')
    node._slot_sampler.decide_due = _boom
    node.tick()
    assert dm.frames == ['latest-images']
    assert any('slot sampler failed' in line for line in node.logger.lines)


def test_a_slot_that_cannot_be_converted_is_the_sensor_data_error_stop():
    dm, node = _running(n_target=10, scripts=[[_decision(1000.0, 1)]])

    def _bad(cams, follower, order, leader, joint_order):
        raise ValueError('corrupt jpeg')
    dm.convert_msgs_to_raw_datas = _bad
    node.tick()
    assert node.communicator.published[-1].error == (
        'Die Sensordaten konnten nicht gelesen werden, die Aufnahme wurde gestoppt. '
        'Bitte starte die Umgebung neu.')
    assert node.on_recording is False


# ── the take's integrity ─────────────────────────────────────────────────────

def test_the_integrity_restarts_at_every_run_entry():
    dm, node = _running(n_target=10, scripts=[[_decision(1000.0, 1)], [], []])
    node.tick()
    assert node._take_integrity.resets == [1000.0]
    node.tick()
    assert node._take_integrity.resets == [1000.0]   # the same take
    dm.frames = []
    entry = dm.run_entered_mono = _Clock.t            # „Wiederholen“, a new take
    node.tick()
    assert node._take_integrity.resets == [1000.0, entry]


def test_the_gap_verdict_is_handed_over_after_every_frame():
    dm, node = _running(n_target=10, scripts=[[_decision(1000.0, 1), _decision(1000.0333, 2)]])
    node._take_integrity.gap = ('leader', None)
    node.tick()
    assert dm.gap_notes == [('leader', None), ('leader', None)]


def _committed_take(warning=''):
    dm, node = _running(n_target=1, scripts=[[_decision(1000.0, 1)], [], []])
    node._take_integrity.warning = warning
    node.tick()                                       # the take's frame -> 'save'
    return dm, node


def test_the_c5_warning_rides_the_tick_that_committed_the_take():
    dm, node = _committed_take('Episode {n}: Die Armdaten kamen zeitweise verspätet an.')
    assert node.communicator.published[-1].error == ''       # not before the commit
    node.tick()                                               # save() commits
    assert node.communicator.published[-1].phase == _TaskStatus.SAVING
    assert node.communicator.published[-1].error == (
        '[WARNUNG] Episode 1: Die Armdaten kamen zeitweise verspätet an.')
    assert any(line.startswith('capture take ep=1 ') and '"outcome": "saved"' in line
               for line in node.logger.lines)


def test_the_c5_warning_never_overwrites_another_warning():
    dm, node = _committed_take('Episode {n}: Der Rechner kam nicht hinterher.')
    original = dm.record

    def _record_with_own_warning(images, state, action):
        result = original(images, state, action)
        dm._last_warning_message = 'Signalaussetzer: Die Szenen-Kamera … gespeichert.'
        return result
    dm.record = _record_with_own_warning
    node.tick()
    assert node.communicator.published[-1].error == (
        '[WARNUNG] Signalaussetzer: Die Szenen-Kamera … gespeichert.')


def test_a_discarded_take_carries_no_c5_warning():
    dm, node = _committed_take('Episode {n}: Der Rechner kam nicht hinterher.')
    dm.discard_on_save = True
    node.tick()
    assert node.communicator.published[-1].error == '[WARNUNG] Signalaussetzer: andere Warnung.'
    assert any('"outcome": "discarded"' in line for line in node.logger.lines)


# ── R5-2: a source that stops ends the session like „Beenden“ ────────────────

def _with_stopped_camera(node, age=3.0):
    node.communicator.counters = [
        {'id': 'camera:scene', 'kind': 'camera', 'name': 'scene', 'last_mono': _Clock.t - age},
        {'id': 'follower:follower', 'kind': 'follower', 'name': 'follower',
         'last_mono': _Clock.t},
    ]


def test_a_stopped_source_ends_the_session_on_an_on_time_tick():
    dm, node = _running(n_target=100, scripts=[[], [], []])
    _with_stopped_camera(node)
    node.tick()                                      # the first tick is never on time
    assert dm.source_stops == []
    _with_stopped_camera(node)
    node.tick(dt=1 / 30)
    assert dm.source_stops == [record_texts_de.source_stop_de('camera', 'scene',
                                                              take_dropped=True)]


def test_a_late_tick_judges_nothing():
    dm, node = _running(n_target=100, scripts=[[], [], []])
    node.tick()
    _with_stopped_camera(node)
    node.tick(dt=0.2)                                # > 1.5 periods: a stall of ours
    assert dm.source_stops == []
    _with_stopped_camera(node)
    node.tick(dt=1 / 30)                             # still silent, on time: ends
    assert len(dm.source_stops) == 1


@pytest.mark.parametrize('status,dropped', [('warmup', False), ('reset', False)])
def test_warmup_and_reset_end_without_dropping_a_take(status, dropped):
    dm, node = _running(n_target=100, scripts=[[], []])
    dm.status = status
    node.tick()
    _with_stopped_camera(node)
    node.tick()
    assert dm.source_stops == [record_texts_de.source_stop_de('camera', 'scene',
                                                              take_dropped=dropped)]


def test_saving_is_not_judged():
    dm, node = _running(n_target=100, scripts=[[], []])
    dm.status = 'save'
    node.tick()
    _with_stopped_camera(node)
    node.tick()
    assert dm.source_stops == []


def test_a_short_silence_is_not_a_stop():
    dm, node = _running(n_target=100, scripts=[[], []])
    node.tick()
    _with_stopped_camera(node, age=1.9)
    node.tick()
    assert dm.source_stops == []


def test_the_data_gate_with_a_dataset_ends_like_beenden_naming_the_source():
    dm, node = _running(n_target=100, scripts=[[], []])
    dm.status = 'reset'
    node.communicator.latest = (None, None, None)
    node.communicator.camera_topic_msgs = {'gripper': 'x', 'scene': None}
    _Clock.t += 6.0                                  # past the 5 s data gate
    node.tick()
    assert dm.source_stops == [record_texts_de.source_stop_de('camera', 'scene',
                                                              take_dropped=False)]
    assert node.on_recording is True                 # the finish completes the session


def test_the_data_gate_without_a_dataset_is_the_old_error_stop():
    dm, node = _running(n_target=100, scripts=[[], []])
    dm._lerobot_dataset = None
    dm.status = 'warmup'
    node.communicator.latest = ('cams', 'fol', None)
    _Clock.t += 6.0
    node.tick()
    assert dm.source_stops == []
    assert node.communicator.published[-1].error.startswith('Der Leader-Arm sendet keine Daten')
    assert node.on_recording is False

"""`/edubotics/signal_status` in the node (Aufnahme 2.0, spec §2.4 / §2.6 items 1-3).

``physical_ai_server.py`` imports rclpy and cannot be imported here, so the two
methods are extracted by ``ast`` and exec'd onto stub nodes:

* ``_init_ros_publisher`` creates the String publisher on ``signal_status.TOPIC``
  with the latched QoS (depth 1, RELIABLE, TRANSIENT_LOCAL — the
  activation_agent pattern) and a 1 Hz timer on its OWN
  MutuallyExclusiveCallbackGroup (the heartbeat-starvation rule), at boot;
* ``_signal_status_tick`` publishes schema v1, nothing while the Communicator is
  not wired (degraded boot), lists the leader only when the profile has one,
  and never raises (its failure log is throttled to one per 30 s).
"""

from __future__ import annotations

import ast
import json
import textwrap
import types
from pathlib import Path

import pytest

from physical_ai_server import signal_status as real_signal_status

_SERVER_PY = (
    Path(__file__).resolve().parents[1] / 'physical_ai_server' / 'physical_ai_server.py'
)
_SRC = _SERVER_PY.read_text(encoding='utf-8')


class _Clock:
    t = 100.0

    @classmethod
    def monotonic(cls):
        return cls.t

    perf_counter = monotonic


class _String:
    def __init__(self):
        self.data = ''


class _Group:
    instances = 0

    def __init__(self):
        type(self).instances += 1


def _qos(**kw):
    return dict(kw)


_ENUMS = dict(
    ReliabilityPolicy=types.SimpleNamespace(RELIABLE='RELIABLE', BEST_EFFORT='BEST_EFFORT'),
    DurabilityPolicy=types.SimpleNamespace(TRANSIENT_LOCAL='TRANSIENT_LOCAL',
                                           VOLATILE='VOLATILE'),
    HistoryPolicy=types.SimpleNamespace(KEEP_LAST='KEEP_LAST'),
)


class _Disk:
    free = 52_345_678_901


def _signal_status_ns():
    ns = types.SimpleNamespace(**{k: getattr(real_signal_status, k)
                                  for k in dir(real_signal_status)
                                  if not k.startswith('__')})
    ns.disk_free_bytes = lambda path: _Disk.free
    return ns


def _load(name):
    tree = ast.parse(_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            src = textwrap.dedent(ast.get_source_segment(_SRC, node))
            ns = {
                'time': _Clock, 'String': _String, 'Empty': object,
                'signal_status': _signal_status_ns(),
                'MutuallyExclusiveCallbackGroup': _Group,
                'QoSProfile': _qos,
                'TrainingStatus': object, 'WorkflowStatus': object,
                'SensorSnapshot': object,
                **_ENUMS,
            }
            exec(compile(src, str(_SERVER_PY), 'exec'), ns)  # noqa: S102
            return ns[name]
    raise AssertionError(f'{name} not found')


_INIT_PUB = _load('_init_ros_publisher')
_TICK = _load('_signal_status_tick')


# ── imports ──────────────────────────────────────────────────────────────────

def test_node_imports_string_and_the_module():
    tree = ast.parse(_SRC)
    std_names, has_module = set(), False
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == 'std_msgs.msg':
            std_names.update(a.name for a in node.names)
        if (isinstance(node, ast.ImportFrom) and node.module == 'physical_ai_server'
                and any(a.name == 'signal_status' for a in node.names)):
            has_module = True
    assert {'Empty', 'String'} <= std_names
    assert has_module


# ── publisher + timer at boot ────────────────────────────────────────────────

class _PubNode:
    def __init__(self):
        self.publishers = []
        self.timers = []

    def get_logger(self):
        return types.SimpleNamespace(info=lambda *a, **k: None)

    def create_publisher(self, msg_type, topic, qos):
        pub = types.SimpleNamespace(msg_type=msg_type, topic=topic, qos=qos, sent=[])
        pub.publish = pub.sent.append
        self.publishers.append(pub)
        return pub

    def create_timer(self, period, callback, callback_group=None):
        timer = types.SimpleNamespace(period=period, callback=callback,
                                      group=callback_group)
        self.timers.append(timer)
        return timer

    def _heartbeat_timer_callback(self):
        pass

    def _idle_status_tick(self):
        pass

    def _sensor_snapshot_timer_callback(self):
        pass

    def _signal_status_tick(self):
        pass


def test_publisher_is_latched_and_the_timer_has_its_own_group():
    node = _PubNode()
    _Group.instances = 0
    types.MethodType(_INIT_PUB, node)()
    pub = next(p for p in node.publishers if p.topic == '/edubotics/signal_status')
    assert pub.msg_type is _String
    assert pub.qos['depth'] == 1
    assert pub.qos['reliability'] == 'RELIABLE'
    assert pub.qos['durability'] == 'TRANSIENT_LOCAL'
    assert node._signal_status_pub is pub
    timer = next(t for t in node.timers if t.callback == node._signal_status_tick)
    assert timer.period == real_signal_status.PUBLISH_PERIOD_S
    assert isinstance(timer.group, _Group)
    # Its own group: not the heartbeat's, not the idle tick's.
    other_groups = [t.group for t in node.timers if t is not timer]
    assert all(g is not timer.group for g in other_groups)
    assert node._signal_status_seq == 0
    assert node._signal_status_boot_mono == _Clock.t
    assert isinstance(node._signal_rates, real_signal_status.RateTracker)


# ── the tick ─────────────────────────────────────────────────────────────────

_COUNTERS = [
    {'id': 'camera:gripper', 'kind': 'camera', 'name': 'gripper',
     'topic': '/gripper/image_raw', 'count': 0, 'last_mono': None},
    {'id': 'camera:scene', 'kind': 'camera', 'name': 'scene',
     'topic': '/scene/image_raw', 'count': 0, 'last_mono': None},
    {'id': 'follower:follower', 'kind': 'follower', 'name': 'follower',
     'topic': '/joint_states', 'count': 0, 'last_mono': None},
    {'id': 'leader:leader', 'kind': 'leader', 'name': 'leader',
     'topic': '/leader/joint_trajectory', 'count': 0, 'last_mono': None},
]


class _Comm:
    def __init__(self):
        self.counters = [dict(c) for c in _COUNTERS]
        self.raise_next = False

    def advance(self, dt, per_source):
        _Clock.t += dt
        for c in self.counters:
            n = per_source.get(c['id'], 0)
            if n:
                c['count'] += n
                c['last_mono'] = _Clock.t

    def source_counters(self):
        if self.raise_next:
            raise RuntimeError('counter read failed')
        return [dict(c) for c in self.counters]


class _TickNode:
    DEFAULT_SAVE_ROOT_PATH = Path('/nonexistent/edubotics/datasets')

    def __init__(self, *, has_leader=True, comm=True):
        self.communicator = _Comm() if comm else None
        self.on_recording = False
        self._arm_profile = types.SimpleNamespace(
            capabilities=types.SimpleNamespace(has_leader=has_leader))
        self.sent = []
        self._signal_status_pub = types.SimpleNamespace(publish=self.sent.append)
        self._signal_status_seq = 0
        self._signal_status_boot_mono = _Clock.t
        self._signal_rates = real_signal_status.RateTracker()
        self.logged = []

    def get_logger(self):
        log = self.logged.append
        return types.SimpleNamespace(warning=log, warn=log, error=log, info=log)


def _tick(node):
    types.MethodType(_TICK, node)()


def _payloads(node):
    return [json.loads(m.data) for m in node.sent]


@pytest.fixture(autouse=True)
def _reset():
    _Clock.t = 100.0
    _Disk.free = 52_345_678_901
    yield


def test_no_publish_while_the_communicator_is_not_wired():
    node = _TickNode(comm=False)
    _tick(node)
    assert node.sent == []


def test_publishes_schema_v1_with_rates_after_a_second():
    node = _TickNode()
    _tick(node)
    node.communicator.advance(1.0, {'camera:gripper': 30, 'camera:scene': 11,
                                    'follower:follower': 99})
    node.on_recording = True
    _tick(node)
    first, second = _payloads(node)
    assert first['v'] == 1 and second['v'] == 1
    assert second['seq'] == first['seq'] + 1
    assert second['uptime_s'] == 1.0
    assert second['recording'] is True and first['recording'] is False
    by_name = {s['name']: s for s in second['sources']}
    assert by_name['gripper']['hz'] == 30.0
    assert by_name['scene']['hz'] == 11.0
    assert by_name['follower']['hz'] == 99.0
    assert by_name['leader'] == {'kind': 'leader', 'name': 'leader',
                                 'topic': '/leader/joint_trajectory',
                                 'hz': 0.0, 'age_s': None}
    assert by_name['gripper']['age_s'] == 0.0
    assert second['disk'] == {'free_bytes': 52_345_678_901,
                              'start_floor_bytes': 3_000_000_000,
                              'critical_floor_bytes': 1_000_000_000}


def test_no_ingest_key_without_a_sensor_executor():
    node = _TickNode()
    _tick(node)
    assert 'ingest' not in _payloads(node)[-1]


def test_the_sensor_threads_liveness_rides_as_the_trailing_ingest_key():
    # Round 5 (F3): additive, trailing; v stays 1 and the v1 keys keep their order.
    node = _TickNode()
    node._sensor_executor = object()
    node._sensor_alive_mono = _Clock.t - 0.01
    _tick(node)
    payload = _payloads(node)[-1]
    assert payload['v'] == 1
    assert list(payload)[-1] == 'ingest'
    assert payload['ingest'] == {'alive': True, 'age_s': 0.01}
    node._sensor_alive_mono = _Clock.t - 2.5
    _tick(node)
    assert _payloads(node)[-1]['ingest'] == {'alive': False, 'age_s': 2.5}


def test_compact_utf8_json():
    node = _TickNode()
    _tick(node)
    text = node.sent[0].data
    assert ', ' not in text and ': ' not in text


def test_no_leader_on_a_leaderless_profile():
    node = _TickNode(has_leader=False)
    _tick(node)
    kinds = [s['kind'] for s in _payloads(node)[0]['sources']]
    assert 'leader' not in kinds and kinds.count('camera') == 2


def test_no_leader_while_the_profile_is_unknown():
    node = _TickNode()
    node._arm_profile = None
    _tick(node)
    assert 'leader' not in [s['kind'] for s in _payloads(node)[0]['sources']]


def test_disk_null_when_statvfs_failed():
    _Disk.free = None
    node = _TickNode()
    _tick(node)
    assert _payloads(node)[0]['disk'] is None


def test_the_tick_never_raises_and_logs_at_most_every_30_s():
    node = _TickNode()
    node.communicator.raise_next = True
    _tick(node)
    _Clock.t += 10.0
    _tick(node)
    assert node.sent == []
    assert len(node.logged) == 1
    _Clock.t += 25.0
    _tick(node)
    assert len(node.logged) == 2

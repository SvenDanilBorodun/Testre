"""Communicator source counters feeding `/edubotics/signal_status` (Aufnahme 2.0).

Every camera / follower / leader subscription the Communicator actually creates
is registered as a source (`camera:<name>`, `follower:<name>`, `leader:<name>`,
in registration order) and each message callback counts one arrival and stamps
its monotonic time FIRST. `source_counters()` is what the node's 1 Hz signal
tick turns into rates and ages. A camera's topic is reported without the
trailing `/compressed` (the page names the stream, not the transport).

communicator.py imports ROS modules at import time; they are stubbed (only when
missing — a real module already in sys.modules is never mutated) and the file is
loaded under a throwaway name. The collaborators the ctor builds are replaced on
THAT copy's globals, so nothing leaks into other tests.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMM_PATH = REPO_ROOT / 'physical_ai_server' / 'communication' / 'communicator.py'


def _stub(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    sys.modules[name] = mod
    for k, v in attrs.items():
        if not hasattr(mod, k):
            setattr(mod, k, v)
    return mod


def _install_stubs():
    _ph = type('_Placeholder', (), {})
    _stub('geometry_msgs')
    _stub('geometry_msgs.msg', Twist=_ph)
    _stub('nav_msgs')
    _stub('nav_msgs.msg', Odometry=_ph)
    _stub('physical_ai_interfaces')
    _stub('physical_ai_interfaces.msg', BrowserItem=_ph, DatasetInfo=_ph, TaskStatus=_ph)
    _stub('physical_ai_interfaces.srv',
          BrowseFile=_ph, EditDataset=_ph, GetDatasetInfo=_ph, GetImageTopicList=_ph)
    _stub('rclpy')
    _stub('rclpy.node', Node=_ph)
    _stub('rclpy.qos',
          HistoryPolicy=types.SimpleNamespace(KEEP_LAST=1),
          QoSProfile=lambda **kw: kw,
          ReliabilityPolicy=types.SimpleNamespace(RELIABLE=1, BEST_EFFORT=2))
    _stub('rclpy.callback_groups', MutuallyExclusiveCallbackGroup=_ph)
    _stub('rosbag_recorder')
    _stub('rosbag_recorder.srv', SendCommand=_ph)
    _stub('sensor_msgs')
    _stub('sensor_msgs.msg', CompressedImage=_ph, JointState=_ph)
    _stub('std_msgs')
    _stub('std_msgs.msg', String=_ph)
    _stub('trajectory_msgs')
    _stub('trajectory_msgs.msg', JointTrajectory=_ph)
    _stub('physical_ai_server.communication.multi_subscriber', MultiSubscriber=_ph)
    _stub('physical_ai_server.data_processing.data_editor', DataEditor=_ph)
    _stub('physical_ai_server.utils.parameter_utils',
          parse_topic_list=lambda *_a, **_k: [],
          parse_topic_list_with_names=lambda *_a, **_k: {})


class _Logger:
    def info(self, *a, **k):
        pass

    debug = warning = warn = error = info


class _Client:
    def wait_for_service(self, timeout_sec=None):
        return True


class _Node:
    def __init__(self):
        self.subscriptions = []

    def get_logger(self):
        return _Logger()

    def create_subscription(self, msg_type, topic, callback, *a, **k):
        self.subscriptions.append(topic)
        return object()

    def create_publisher(self, *a, **k):
        return object()

    def create_service(self, *a, **k):
        return object()

    def create_client(self, *a, **k):
        return _Client()

    def create_timer(self, *a, **k):
        return object()


class _FakeMultiSubscriber:
    """Real is_source_enabled semantics; records what was subscribed."""

    def __init__(self, node, enabled_sources=None):
        self._node = node
        self._enabled = enabled_sources
        self.added = []

    def is_source_enabled(self, category):
        return self._enabled is None or category in self._enabled

    def add_subscriber(self, category, name, topic, msg_type, callback=None,
                       qos_profile=None):
        if not self.is_source_enabled(category):
            return
        self.added.append((category, name, topic))

    def cleanup(self):
        pass


def _parse_with_names(topic_list):
    out = {}
    for entry in topic_list or []:
        if ':' in entry:
            key, value = entry.split(':', 1)
            out[key] = value
    return out


class _Clock:
    t = 500.0

    @classmethod
    def monotonic(cls):
        return cls.t


def _load():
    _install_stubs()
    spec = importlib.util.spec_from_file_location(
        '_edubotics_communicator_counters_test', COMM_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.MultiSubscriber = _FakeMultiSubscriber
    module.DataEditor = lambda *a, **k: object()
    module.FileBrowseUtils = lambda *a, **k: object()
    module.parse_topic_list_with_names = _parse_with_names
    module.parse_topic_list = lambda topic_list: list(topic_list or [])
    module.time = types.SimpleNamespace(monotonic=_Clock.monotonic,
                                        time=_Clock.monotonic,
                                        perf_counter=_Clock.monotonic)
    return module


COMM = _load()

_PARAMS = {
    'camera_topic_list': ['gripper:/gripper/image_raw/compressed',
                          'scene:/scene/image_raw/compressed'],
    'joint_topic_list': ['follower:/joint_states', 'leader:/leader/joint_trajectory'],
    'rosbag_extra_topic_list': [],
}


def _comm(mode='collection'):
    return COMM.Communicator(node=_Node(), operation_mode=mode, params=dict(_PARAMS))


class _Msg:
    header = types.SimpleNamespace(stamp=types.SimpleNamespace(sec=1, nanosec=0))


def test_every_subscribed_source_is_registered_in_order():
    c = _comm('collection')
    assert c.source_counters() == [
        {'id': 'camera:gripper', 'kind': 'camera', 'name': 'gripper',
         'topic': '/gripper/image_raw', 'count': 0, 'last_mono': None},
        {'id': 'camera:scene', 'kind': 'camera', 'name': 'scene',
         'topic': '/scene/image_raw', 'count': 0, 'last_mono': None},
        {'id': 'follower:follower', 'kind': 'follower', 'name': 'follower',
         'topic': '/joint_states', 'count': 0, 'last_mono': None},
        {'id': 'leader:leader', 'kind': 'leader', 'name': 'leader',
         'topic': '/leader/joint_trajectory', 'count': 0, 'last_mono': None},
    ]


def test_a_source_that_is_not_subscribed_is_not_registered():
    # Inference mode subscribes no leader.
    c = _comm('inference')
    assert [s['id'] for s in c.source_counters()] == [
        'camera:gripper', 'camera:scene', 'follower:follower']


def test_callbacks_count_arrivals_and_stamp_the_time():
    c = _comm()
    _Clock.t = 600.0
    c._camera_callback('scene', _Msg())
    c._camera_callback('scene', _Msg())
    _Clock.t = 601.5
    c._follower_callback('follower', _Msg())
    c._leader_callback('leader', _Msg())
    by_id = {s['id']: s for s in c.source_counters()}
    assert by_id['camera:scene']['count'] == 2
    assert by_id['camera:scene']['last_mono'] == 600.0
    assert by_id['camera:gripper']['count'] == 0
    assert by_id['follower:follower'] == {
        'id': 'follower:follower', 'kind': 'follower', 'name': 'follower',
        'topic': '/joint_states', 'count': 1, 'last_mono': 601.5}
    assert by_id['leader:leader']['count'] == 1
    # The callbacks still do their original job.
    assert c.follower_topic_msgs['follower'] is not None
    assert c.leader_topic_msgs['leader'] is not None


def test_clear_latest_data_keeps_the_counters():
    c = _comm()
    c._camera_callback('gripper', _Msg())
    c.clear_latest_data()
    by_id = {s['id']: s for s in c.source_counters()}
    assert by_id['camera:gripper']['count'] == 1


def test_source_counters_returns_copies():
    c = _comm()
    snapshot = c.source_counters()
    snapshot[0]['count'] = 999
    assert c.source_counters()[0]['count'] == 0


def test_callbacks_on_a_communicator_without_counters_do_not_raise():
    # Tests elsewhere build a Communicator via object.__new__ without __init__.
    c = object.__new__(COMM.Communicator)
    c.follower_topic_msgs = {}
    c.leader_topic_msgs = {}
    c._follower_callback('follower', _Msg())
    c._leader_callback('leader', _Msg())
    assert c.follower_topic_msgs['follower'] is not None


@pytest.mark.parametrize('topic,expected', [
    ('/scene/image_raw/compressed', '/scene/image_raw'),
    ('/scene/image_raw', '/scene/image_raw'),
])
def test_camera_topic_drops_only_a_trailing_compressed(topic, expected):
    params = dict(_PARAMS)
    params['camera_topic_list'] = [f'scene:{topic}']
    c = COMM.Communicator(node=_Node(), operation_mode='inference', params=params)
    assert c.source_counters()[0]['topic'] == expected

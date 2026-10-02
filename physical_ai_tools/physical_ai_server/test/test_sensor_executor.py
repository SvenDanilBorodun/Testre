"""The supervised sensor-ingest executor (Aufnahme 2.0 round 5, spec §2.1/§2.2).

The recorder's camera/follower/leader subscriptions and the collision
monitor's three subscriptions live on a second, lightweight node
(``physical_ai_server_sensors``) spun by a SingleThreadedExecutor on one daemon
thread. A sensor callback never raises into that executor; a supervisor loop
counts executor failures and, after SENSOR_FAIL_LIMIT inside
SENSOR_FAIL_WINDOW_S, ends the PROCESS (``os._exit(70)``) so s6 respawns the
node, instead of leaving recording stalled and the collision detector deaf
behind a node that still answers its heartbeat.

``physical_ai_server.py`` imports rclpy and cannot be imported here, so the
methods are extracted by ``ast`` and exec'd onto a stub node (the
test_signal_status_node_wiring idiom). The Communicator is loaded with its ROS
imports stubbed (the test_communicator_source_counters idiom).
"""

from __future__ import annotations

import ast
import collections
import importlib.util
import sys
import textwrap
import threading
import types
from functools import partial
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parents[1] / 'physical_ai_server'
_SERVER_PY = _PKG / 'physical_ai_server.py'
_COMM_PY = _PKG / 'communication' / 'communicator.py'
_SRC = _SERVER_PY.read_text(encoding='utf-8')


# ── stub ROS surface for the extracted node methods ─────────────────────────

class _Clock:
    t = 1000.0

    @classmethod
    def monotonic(cls):
        return cls.t

    perf_counter = monotonic


class _Exited(BaseException):
    """os._exit replacement: never returns, so it must not be an Exception."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class _Os:
    exits = []

    @classmethod
    def _exit(cls, code):
        cls.exits.append(code)
        raise _Exited(code)


class _SensorNode:
    def __init__(self, name, **kwargs):
        self.name = name
        self.kwargs = kwargs
        self.destroyed = False

    def destroy_node(self):
        self.destroyed = True


class _Rclpy:
    created = []
    is_ok = True

    @classmethod
    def create_node(cls, name, **kwargs):
        node = _SensorNode(name, **kwargs)
        cls.created.append(node)
        return node

    @classmethod
    def ok(cls):
        return cls.is_ok


class _ExternalShutdownException(Exception):
    pass


class _ScriptedExecutor:
    """spin_once runs a script: 'ok' returns, an Exception instance raises,
    'stop' sets the host's stop flag (the end of the test)."""

    script = []

    def __init__(self):
        self.nodes = []
        self.calls = 0
        self.shut_down = False
        self.host = None

    def add_node(self, node):
        self.nodes.append(node)

    def spin_once(self, timeout_sec=None):
        self.calls += 1
        step = self.script.pop(0) if self.script else 'stop'
        if isinstance(step, tuple):
            dt, step = step
            _Clock.t += dt
        if step == 'stop':
            self.host._sensor_stop = True
            return
        if isinstance(step, BaseException):
            raise step

    def shutdown(self, timeout_sec=None):
        self.shut_down = True


class _NoStartThread:
    """threading.Thread stand-in: records the target, never starts a thread
    (the tests drive _sensor_loop on their own thread)."""

    started = []

    def __init__(self, target=None, name=None, daemon=None):
        self.target = target
        self.name = name
        self.daemon = daemon

    def start(self):
        type(self).started.append(self)


class _Logger:
    def __init__(self):
        self.lines = []

    def _log(self, level, msg):
        self.lines.append((level, str(msg)))

    def info(self, msg, *a, **k):
        self._log('info', msg)

    def warning(self, msg, *a, **k):
        self._log('warning', msg)

    def error(self, msg, *a, **k):
        self._log('error', msg)

    def fatal(self, msg, *a, **k):
        self._log('fatal', msg)


_CLASS = next(n for n in ast.parse(_SRC).body
              if isinstance(n, ast.ClassDef) and n.name == 'PhysicalAIServer')


def _class_constant(name):
    for stmt in _CLASS.body:
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and stmt.targets[0].id == name):
            return ast.literal_eval(stmt.value)
    raise AssertionError(f'class constant {name} not found')


def _load(names):
    ns = {
        'rclpy': _Rclpy,
        'SingleThreadedExecutor': _ScriptedExecutor,
        'ExternalShutdownException': _ExternalShutdownException,
        'time': _Clock,
        'os': _Os,
        'threading': types.SimpleNamespace(Thread=_NoStartThread, Lock=threading.Lock),
        'collections': collections,
    }
    for stmt in _CLASS.body:
        if isinstance(stmt, ast.FunctionDef) and stmt.name in names:
            src = textwrap.dedent(ast.get_source_segment(_SRC, stmt))
            exec(compile(src, str(_SERVER_PY), 'exec'), ns)  # noqa: S102
    missing = [n for n in names if n not in ns]
    assert not missing, f'methods not found: {missing}'
    return ns


_METHODS = ('_ensure_sensor_executor', '_sensor_loop', '_note_sensor_failure',
            '_shutdown_sensor_executor')
_NS = _load(_METHODS)


class _Host:
    SENSOR_FAIL_LIMIT = _class_constant('SENSOR_FAIL_LIMIT')
    SENSOR_FAIL_WINDOW_S = _class_constant('SENSOR_FAIL_WINDOW_S')
    SENSOR_EXIT_CODE = _class_constant('SENSOR_EXIT_CODE')

    def __init__(self):
        self.logger = _Logger()
        for name in _METHODS:
            setattr(self, name, types.MethodType(_NS[name], self))

    def get_logger(self):
        return self.logger


@pytest.fixture(autouse=True)
def _reset():
    _Clock.t = 1000.0
    _Os.exits = []
    _Rclpy.created = []
    _Rclpy.is_ok = True
    _ScriptedExecutor.script = []
    _NoStartThread.started = []
    yield


def _host_with_executor(script):
    host = _Host()
    host._ensure_sensor_executor()
    host._sensor_executor.host = host
    _ScriptedExecutor.script = list(script)
    return host


# ── the sensor node ──────────────────────────────────────────────────────────

def test_the_contract_constants():
    assert _Host.SENSOR_FAIL_LIMIT == 3
    assert _Host.SENSOR_FAIL_WINDOW_S == 10.0
    assert _Host.SENSOR_EXIT_CODE == 70


def test_the_sensor_node_ignores_the_launch_remap():
    # Without use_global_arguments=False the launch's
    # `-r __node:=physical_ai_server` renames this node too: two nodes, one name.
    host = _Host()
    host._ensure_sensor_executor()
    assert len(_Rclpy.created) == 1
    node = _Rclpy.created[0]
    assert node.name == 'physical_ai_server_sensors'
    assert node.kwargs == {'use_global_arguments': False}
    assert host._sensor_node is node
    assert host._sensor_executor.nodes == [node]


def test_one_daemon_thread_runs_the_supervisor_loop():
    host = _Host()
    host._ensure_sensor_executor()
    assert len(_NoStartThread.started) == 1
    thread = _NoStartThread.started[0]
    assert thread.daemon is True
    assert thread.name == 'sensor-exec'
    assert thread.target == host._sensor_loop


def test_ensure_is_idempotent_for_the_degraded_boot_re_init():
    host = _Host()
    host._ensure_sensor_executor()
    first = host._sensor_node
    host._ensure_sensor_executor()
    assert host._sensor_node is first
    assert len(_Rclpy.created) == 1
    assert len(_NoStartThread.started) == 1


def test_a_failing_node_creation_degrades_to_the_main_node():
    host = _Host()

    def _boom(name, **kwargs):
        raise RuntimeError('rcl context invalid')

    original = _Rclpy.create_node
    _Rclpy.create_node = staticmethod(_boom)
    try:
        host._ensure_sensor_executor()       # must not raise out of __init__
    finally:
        _Rclpy.create_node = original
    assert host._sensor_node is None
    assert any(level == 'error' for level, _ in host.logger.lines)


# ── supervision (F3) ─────────────────────────────────────────────────────────

def test_three_failures_inside_the_window_exit_the_process_for_respawn():
    host = _host_with_executor([
        RuntimeError('a'), (2.0, RuntimeError('b')), (2.0, RuntimeError('c')),
        'ok', 'ok'])
    with pytest.raises(_Exited):
        host._sensor_loop()
    assert _Os.exits == [70]
    errors = [m for level, m in host.logger.lines if level == 'error']
    assert len(errors) == 3
    assert errors[0].startswith('sensor executor failure 1')
    assert any(level == 'fatal' and 'respawn' in m for level, m in host.logger.lines)


def test_two_failures_keep_running():
    host = _host_with_executor([RuntimeError('a'), 'ok', RuntimeError('b'), 'ok', 'stop'])
    host._sensor_loop()                      # returns when the test stops it
    assert _Os.exits == []
    assert host._sensor_executor.calls == 5


def test_failures_spread_beyond_the_window_keep_running():
    host = _host_with_executor([
        RuntimeError('a'), (6.0, RuntimeError('b')), (6.0, RuntimeError('c')),
        (6.0, RuntimeError('d')), 'stop'])
    host._sensor_loop()
    assert _Os.exits == []


def test_a_shutdown_ends_the_loop_quietly():
    host = _host_with_executor([_ExternalShutdownException(), RuntimeError('never')])
    host._sensor_loop()
    assert _Os.exits == []
    assert host._sensor_executor.calls == 1


def test_the_loop_stops_when_rclpy_is_shut_down():
    host = _host_with_executor(['ok'])
    _Rclpy.is_ok = False
    host._sensor_loop()
    assert host._sensor_executor.calls == 0


def test_the_loop_stamps_its_liveness_every_turn():
    host = _host_with_executor([(0.5, 'ok'), (0.5, 'ok'), 'stop'])
    host._sensor_loop()
    assert host._sensor_alive_mono == _Clock.t


def test_shutdown_stops_the_executor_then_destroys_the_node():
    host = _Host()
    host._ensure_sensor_executor()
    node = host._sensor_node
    executor = host._sensor_executor
    host._shutdown_sensor_executor()
    assert host._sensor_stop is True
    assert executor.shut_down is True
    assert node.destroyed is True
    host._shutdown_sensor_executor()     # idempotent, never raises


def test_main_shuts_the_sensor_executor_down_before_the_node():
    main = next(n for n in ast.parse(_SRC).body
                if isinstance(n, ast.FunctionDef) and n.name == 'main')
    src = ast.get_source_segment(_SRC, main)
    assert 'MultiThreadedExecutor(num_threads=8)' in src
    assert src.index('gc.freeze()') < src.index('executor.spin()')
    finally_src = src[src.index('finally:'):]
    assert finally_src.index('_shutdown_sensor_executor()') < finally_src.index(
        'node.destroy_node()')


def test_init_creates_the_sensor_executor_before_the_collision_monitor():
    init = next(n for n in _CLASS.body
                if isinstance(n, ast.FunctionDef) and n.name == '__init__')
    order = [s.value.func.attr for s in init.body
             if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)
             and isinstance(s.value.func, ast.Attribute)]
    assert order.index('_ensure_sensor_executor') < order.index('_init_collision_monitor')


def test_init_creates_the_collision_group_before_the_collision_monitor():
    # Round 6 (F2): the trip hand-over and the watchdog run in their own group,
    # which must exist when _init_collision_monitor creates them.
    init = next(n for n in _CLASS.body
                if isinstance(n, ast.FunctionDef) and n.name == '__init__')
    src = ast.get_source_segment(_SRC, init)
    assert ('self._collision_cb_group = MutuallyExclusiveCallbackGroup()' in src)
    assert src.index('self._collision_cb_group =') < src.index('self._init_collision_monitor()')


# ── the Communicator's subscriptions live on the sensor node ────────────────

def _stub(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    sys.modules[name] = mod
    for k, v in attrs.items():
        if not hasattr(mod, k):
            setattr(mod, k, v)
    return mod


def _load_communicator():
    ph = type('_Placeholder', (), {})
    _stub('geometry_msgs')
    _stub('geometry_msgs.msg', Twist=ph)
    _stub('nav_msgs')
    _stub('nav_msgs.msg', Odometry=ph)
    _stub('physical_ai_interfaces')
    _stub('physical_ai_interfaces.msg', BrowserItem=ph, DatasetInfo=ph, TaskStatus=ph)
    _stub('physical_ai_interfaces.srv',
          BrowseFile=ph, EditDataset=ph, GetDatasetInfo=ph, GetImageTopicList=ph)
    _stub('rclpy')
    _stub('rclpy.node', Node=ph)
    _stub('rclpy.qos',
          HistoryPolicy=types.SimpleNamespace(KEEP_LAST=1),
          QoSProfile=lambda **kw: kw,
          ReliabilityPolicy=types.SimpleNamespace(RELIABLE=1, BEST_EFFORT=2))
    _stub('rclpy.callback_groups', MutuallyExclusiveCallbackGroup=ph)
    _stub('rosbag_recorder')
    _stub('rosbag_recorder.srv', SendCommand=ph)
    _stub('sensor_msgs')
    _stub('sensor_msgs.msg', CompressedImage=ph, JointState=ph)
    _stub('std_msgs')
    _stub('std_msgs.msg', String=ph)
    _stub('trajectory_msgs')
    _stub('trajectory_msgs.msg', JointTrajectory=ph)
    _stub('physical_ai_server.communication.multi_subscriber', MultiSubscriber=ph)
    _stub('physical_ai_server.data_processing.data_editor', DataEditor=ph)
    _stub('physical_ai_server.utils.parameter_utils',
          parse_topic_list=lambda *_a, **_k: [],
          parse_topic_list_with_names=lambda *_a, **_k: {})
    spec = importlib.util.spec_from_file_location(
        '_edubotics_communicator_sensor_test', _COMM_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _CommNode:
    def __init__(self, name):
        self.name = name
        self.subscriptions = []
        self.logger = _Logger()

    def get_logger(self):
        return self.logger

    def create_subscription(self, msg_type, topic, callback, qos, **k):
        self.subscriptions.append(topic)
        return object()

    def create_publisher(self, *a, **k):
        return object()

    def create_service(self, *a, **k):
        return object()

    def create_client(self, *a, **k):
        return types.SimpleNamespace(wait_for_service=lambda timeout_sec=None: True)

    def create_timer(self, *a, **k):
        return object()


class _RecordingMultiSubscriber:
    instances = []

    def __init__(self, node, enabled_sources=None):
        self.node = node
        self.enabled = enabled_sources
        self.added = []
        type(self).instances.append(self)

    def is_source_enabled(self, category):
        return self.enabled is None or category in self.enabled

    def add_subscriber(self, category, name, topic, msg_type, callback=None,
                       qos_profile=None):
        if self.is_source_enabled(category):
            self.added.append((category, name, topic, qos_profile))

    def cleanup(self):
        pass


def _parse_with_names(topic_list):
    return dict(entry.split(':', 1) for entry in topic_list or [] if ':' in entry)


@pytest.fixture(scope='module')
def comm_module():
    module = _load_communicator()
    module.MultiSubscriber = _RecordingMultiSubscriber
    module.DataEditor = lambda *a, **k: object()
    module.FileBrowseUtils = lambda *a, **k: object()
    module.parse_topic_list_with_names = _parse_with_names
    module.parse_topic_list = lambda topic_list: list(topic_list or [])
    return module


_PARAMS = {
    'camera_topic_list': ['gripper:/gripper/image_raw/compressed',
                          'scene:/scene/image_raw/compressed'],
    'joint_topic_list': ['follower:/joint_states', 'leader:/leader/joint_trajectory'],
    'rosbag_extra_topic_list': [],
}


def test_recorder_subscriptions_live_on_the_sensor_node_with_depth_32(comm_module):
    _RecordingMultiSubscriber.instances = []
    main_node, sensor_node = _CommNode('main'), _CommNode('sensors')
    comm = comm_module.Communicator(node=main_node, operation_mode='collection',
                                    params=dict(_PARAMS), sensor_node=sensor_node)
    subscriber = _RecordingMultiSubscriber.instances[-1]
    assert subscriber.node is sensor_node
    assert comm.sensor_node is sensor_node
    assert [a[:3] for a in subscriber.added] == [
        ('camera', 'gripper', '/gripper/image_raw/compressed'),
        ('camera', 'scene', '/scene/image_raw/compressed'),
        ('follower', 'follower', '/joint_states'),
        ('leader', 'leader', '/leader/joint_trajectory'),
    ]
    assert comm_module.SENSOR_QOS_DEPTH == 32
    for category, name, _topic, qos in subscriber.added:
        assert qos['depth'] == 32, (category, name)
        assert qos['reliability'] == 2               # BEST_EFFORT
        assert qos['history'] == 1                   # KEEP_LAST


def test_without_a_sensor_node_the_main_node_subscribes(comm_module):
    _RecordingMultiSubscriber.instances = []
    main_node = _CommNode('main')
    comm = comm_module.Communicator(node=main_node, operation_mode='collection',
                                    params=dict(_PARAMS))
    assert _RecordingMultiSubscriber.instances[-1].node is main_node
    assert comm.sensor_node is main_node


class _BadMsg:
    @property
    def header(self):
        raise ValueError('malformed header')


@pytest.mark.parametrize('callback,name', [
    ('_camera_callback', 'scene'),
    ('_follower_callback', 'follower'),
    ('_leader_callback', 'leader'),
])
def test_a_sensor_callback_never_raises_into_the_executor(comm_module, callback, name):
    comm = comm_module.Communicator(node=_CommNode('main'), operation_mode='collection',
                                    params=dict(_PARAMS), sensor_node=_CommNode('s'))
    body = callback + '_body'

    def _boom(*a, **k):
        raise RuntimeError('body failed')

    setattr(comm, body, _boom)
    for _ in range(250):
        getattr(comm, callback)(name, object())            # must not raise
    logged = [m for level, m in comm.node.get_logger().lines if level == 'error']
    # The first five, then every 100th (the 100th and 200th failure).
    assert len(logged) == 7
    assert all('body failed' in m for m in logged)


def test_guarded_callbacks_still_do_their_job(comm_module):
    comm = comm_module.Communicator(node=_CommNode('main'), operation_mode='collection',
                                    params=dict(_PARAMS), sensor_node=_CommNode('s'))
    msg = types.SimpleNamespace(header=types.SimpleNamespace(
        stamp=types.SimpleNamespace(sec=1, nanosec=0)))
    comm._camera_callback('scene', msg)
    comm._follower_callback('follower', msg)
    comm._leader_callback('leader', msg)
    assert comm.camera_topic_msgs['scene'] is msg
    assert comm.follower_topic_msgs['follower'] is msg
    assert comm.leader_topic_msgs['leader'] is msg


# ── the timestamped histories (spec §2.3) ────────────────────────────────────

def test_every_registered_source_has_a_pre_created_history(comm_module):
    comm = comm_module.Communicator(node=_CommNode('main'), operation_mode='collection',
                                    params=dict(_PARAMS), sensor_node=_CommNode('s'))
    assert sorted(comm._histories) == ['camera:gripper', 'camera:scene',
                                       'follower:follower', 'leader:leader']
    assert comm_module.CAMERA_HISTORY_LEN == 64
    assert comm_module.JOINT_HISTORY_LEN == 256
    assert comm._histories['camera:scene']._items.maxlen == 64
    assert comm._histories['follower:follower']._items.maxlen == 256
    assert comm._histories['leader:leader']._items.maxlen == 256


def test_inference_has_no_leader_history(comm_module):
    comm = comm_module.Communicator(node=_CommNode('main'), operation_mode='inference',
                                    params=dict(_PARAMS), sensor_node=_CommNode('s'))
    cams, follower, leader = comm.history_snapshots()
    assert sorted(cams) == ['gripper', 'scene']
    assert follower == []
    assert leader is None


def _stamped(sec, nanosec=0):
    return types.SimpleNamespace(header=types.SimpleNamespace(
        stamp=types.SimpleNamespace(sec=sec, nanosec=nanosec)))


def test_callbacks_append_arrival_first_then_the_old_work(comm_module):
    comm = comm_module.Communicator(node=_CommNode('main'), operation_mode='collection',
                                    params=dict(_PARAMS), sensor_node=_CommNode('s'))
    clock = types.SimpleNamespace(t=100.0)
    real_time = comm_module.time
    comm_module.time = types.SimpleNamespace(
        monotonic=lambda: clock.t, time=lambda: clock.t, perf_counter=lambda: clock.t)
    try:
        cam_msg, fol_msg, lea_msg = _stamped(50), _stamped(50, 5_000_000), _stamped(0)
        comm._camera_callback('scene', cam_msg)
        clock.t = 100.004
        comm._follower_callback('follower', fol_msg)
        clock.t = 100.010
        comm._leader_callback('leader', lea_msg)
    finally:
        comm_module.time = real_time
    cams, follower, leader = comm.history_snapshots()
    (seq, arrival, t, msg), = cams['scene']
    assert (seq, arrival, msg) == (1, 100.0, cam_msg)
    assert t == 100.0                     # first sample: the stamp maps to its arrival
    assert follower[0][1] == 100.004 and follower[0][3] is fol_msg
    assert leader[0][1:] == (100.010, 100.010, lea_msg)   # unstamped: t = arrival
    assert cams['gripper'] == []
    assert comm.camera_topic_msgs['scene'] is cam_msg      # the old work still runs
    assert comm.history_source_names() == ('follower', 'leader')


def test_clear_latest_data_clears_the_histories(comm_module):
    comm = comm_module.Communicator(node=_CommNode('main'), operation_mode='collection',
                                    params=dict(_PARAMS), sensor_node=_CommNode('s'))
    comm._camera_callback('scene', _stamped(1))
    comm._leader_callback('leader', _stamped(0))
    comm.clear_latest_data()
    cams, follower, leader = comm.history_snapshots()
    assert cams['scene'] == [] and leader == []
    comm._camera_callback('scene', _stamped(2))
    assert comm.history_snapshots()[0]['scene'][0][0] == 2   # seq keeps counting


def test_a_communicator_built_without_init_still_takes_messages(comm_module):
    comm = object.__new__(comm_module.Communicator)
    comm.follower_topic_msgs = {}
    comm._follower_callback('follower', _stamped(1))
    assert comm.follower_topic_msgs['follower'] is not None


def test_partial_is_still_how_the_callbacks_are_bound():
    # A guard must wrap the body, not the partial: init_subscribers binds the
    # public callback names, which stay the ROS entry points.
    src = _COMM_PY.read_text(encoding='utf-8')
    assert 'partial(self._camera_callback, name)' in src
    assert 'partial(self._follower_callback, name)' in src
    assert 'partial(self._leader_callback, name)' in src
    assert partial  # noqa: B018 — imported for the reader

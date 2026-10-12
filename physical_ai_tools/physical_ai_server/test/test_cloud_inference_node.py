"""The node's Cloud-Inferenz path (spec §5.5, §5.11; D2, D18, D20, D23, D38; R-1, R-13, R-14, R-15, F-7, F-14).

physical_ai_server.py imports rclpy and cannot be imported here. As in test_record_command_dispatch.py,
the methods are exec'd from the AST into a namespace holding the REAL cloud_schedule, cloud_protocol,
cloud_texts_de and cloud_contract modules plus stubs, and run as methods of a small stub node. The
cloud transport is a scriptable FakeCloudClient (snapshot, chunks incl. ``(seq, obs_e, None)``); the
Communicator is a fake holding REAL JPEG bytes from ``cv2.imencode``; time is a fake monotonic clock.
"""

from __future__ import annotations

import array
import ast
import contextlib
import json
import os
import textwrap
import threading
import types
from pathlib import Path

import cv2
import numpy as np
import pytest

from physical_ai_server import robot_profiles as rp
from physical_ai_server.data_processing import record_texts_de
from physical_ai_server.inference import cloud_client as real_cloud_client
from physical_ai_server.inference import cloud_contract, cloud_protocol, cloud_schedule, cloud_texts_de

_ROOT = Path(__file__).resolve().parents[1] / 'physical_ai_server'
_SERVER_PY = _ROOT / 'physical_ai_server.py'
_SOURCE = _SERVER_PY.read_text(encoding='utf-8')
_TREE = ast.parse(_SOURCE)

TOK = 'SyntheticConnectTokenForTestsOnly_000000001'
EDGE = 'synthetic-edge-token.for.tests~only!0001'
assert cloud_protocol.TOKEN_RE.fullmatch(TOK) and cloud_protocol.EDGE_TOKEN_RE.fullmatch(EDGE)

POLICY = {'type': 'act', 'expected_cameras': ['gripper', 'scene'],
          'image_shapes': {'gripper': [3, 480, 640], 'scene': [3, 480, 640]}, 'trained_fps': 30.0,
          'chunk_size': 100, 'n_action_steps': 100, 'state_dim': 6, 'action_dim': 6, 'requires_task': False}
LIMITS = {'cadence_steps': 5, 'actions_per_chunk': 50, 'aggregate_new_weight': 0.7, 'max_obs_per_s': 8.0,
          'stall_end_s': 15.0, 'resend_after_s': 2.0, 'max_end_in_s': 3300}
JOINTS = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1']


# ── stubs ───────────────────────────────────────────────────────────────────

class Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


CLOCK = Clock()


class TaskStatus:
    READY, WARMING_UP, RESETTING, RECORDING, SAVING, STOPPED, INFERENCING = 0, 1, 2, 3, 4, 5, 6

    def __init__(self):
        self.phase = None
        self.error = ''
        self.task_info = None
        self.robot_type = ''


class _Req:
    IDLE, START_RECORD, START_INFERENCE, STOP, MOVE_TO_NEXT = 0, 1, 2, 3, 4
    RERECORD, FINISH, SKIP_TASK = 5, 6, 7


def _jpeg(w=640, h=480, quality=80):
    img = (np.random.default_rng(w + h).random((h, w, 3)) * 255).astype(np.uint8)
    ok, enc = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    assert ok
    return enc.tobytes()


JPEG_640 = _jpeg()
JPEG_1280 = _jpeg(1280, 720)


class Stamp:
    def __init__(self, sec):
        self.sec, self.nanosec = sec, 0


class CamMsg:
    def __init__(self, data, sec=1):
        self.data = array.array('B', data)
        self.header = types.SimpleNamespace(stamp=Stamp(sec))
        self.format = 'jpeg'


class JointMsg:
    def __init__(self, positions):
        self.positions = list(positions)


class FakeComm:
    def __init__(self, cams=('gripper', 'scene')):
        self.camera_topic_msgs = {n: CamMsg(JPEG_640) for n in cams}
        self.follower_topic_msgs = {'follower': JointMsg([0.1 * i for i in range(6)])}
        self.actions = []
        self.statuses = []
        self.raise_on_latest = None

    def get_latest_data(self):
        if self.raise_on_latest is not None:
            raise self.raise_on_latest
        cams = dict(self.camera_topic_msgs)
        if any(m is None for m in cams.values()):
            return None, None, None
        if any(v is None for v in self.follower_topic_msgs.values()):
            return cams, None, None
        return cams, dict(self.follower_topic_msgs), None

    def publish_action(self, joint_msg_datas):
        self.actions.append(joint_msg_datas)

    def publish_status(self, status):
        self.statuses.append(status)

    def get_publisher_msg_types(self):
        return {'leader': 'JointTrajectory'}

    def clear_latest_data(self):
        pass


class FakeConverter:
    def __init__(self):
        self.calls = []

    def tensor_array2joint_msgs(self, action, joint_topic_types, joint_order):
        self.calls.append((np.array(action), joint_topic_types, joint_order))
        return {'leader': ('JointTrajectory', np.array(action))}


class FakeDataManager:
    instances = []

    def __init__(self, **kw):
        self.kw = kw
        self.data_converter = FakeConverter()
        self._stale_halt_threshold_s = 5.0
        FakeDataManager.instances.append(self)

    def get_current_record_status(self):
        s = TaskStatus()
        s.task_info = self.kw.get('task_info')
        return s

    def joint_msgs2tensor_array(self, msg, joint_order=None):
        return list(msg.positions)


class FakeCloudClient:
    instances = []
    raise_on_init = None

    def __init__(self, *, token, edge_token, conn_id, hello, **kw):
        if FakeCloudClient.raise_on_init is not None:
            raise FakeCloudClient.raise_on_init
        self.__token = token
        self.__edge = edge_token
        self.conn_id = conn_id
        self.hello = hello
        self.ready = None
        self.end = None
        self.chunks = []
        self.offers = []
        self.closed = []
        self.joined = []
        self.started = False
        FakeCloudClient.instances.append(self)

    def tokens(self):
        return self.__token, self.__edge

    def start(self):
        self.started = True

    def snapshot(self):
        return real_cloud_client.ClientSnapshot(
            phase='ended' if self.end else ('ready' if self.ready else 'connecting'),
            ready=self.ready, end=self.end, rtt_ms_p50=101.0, gpu_ms_p50=23.0, failures=0)

    def take_chunks(self):
        out, self.chunks = self.chunks, []
        return out

    def offer(self, frame, seq, obs_e):
        self.offers.append((frame, seq, obs_e))

    def close(self, reason, stats=None):
        self.closed.append((reason, stats))

    def join(self, timeout):
        self.joined.append(timeout)


class Timer:
    def __init__(self, period, cb, group):
        self.period, self.callback, self.group = period, cb, group
        self.destroyed = False

    def destroy(self):
        self.destroyed = True


class Pub:
    def __init__(self, msg_type, topic, qos):
        self.msg_type, self.topic, self.qos = msg_type, topic, qos
        self.sent = []

    def publish(self, msg):
        self.sent.append(msg)


class String:
    def __init__(self, data=''):
        self.data = data


class Group:
    pass


class QoSProfile:
    def __init__(self, **kw):
        self.kw = kw


class Log:
    def __init__(self):
        self.lines = []

    def _add(self, level, m):
        self.lines.append((level, str(m)))

    def info(self, m, *a, **k):
        self._add('info', m)

    def warning(self, m, *a, **k):
        self._add('warning', m)

    warn = warning

    def error(self, m, *a, **k):
        self._add('error', m)

    def text(self):
        return '\n'.join(m for _, m in self.lines)


# ── the exec'd node methods ─────────────────────────────────────────────────

def _segment(node):
    return textwrap.dedent(ast.get_source_segment(_SOURCE, node))


def _module_constant(name):
    for node in _TREE.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == name):
            return ast.literal_eval(node.value)
    raise AssertionError(name)


def _parse_topic_list_with_names(topic_list):
    out = {}
    for entry in topic_list or []:
        if ':' in entry:
            k, v = entry.split(':', 1)
            out[k] = v
    return out


_TM_SRC = (_ROOT / 'timer' / 'timer_manager.py').read_text(encoding='utf-8')

NAMES = ('start_cloud_inference_callback', '_cloud_inference_tick', '_cloud_inference_tick_body',
         '_cloud_preflight', '_cloud_build_obs', '_cloud_publish_status', '_end_inference',
         'cloud_inference_stop_callback', '_preflight_contract', '_available_camera_names',
         'user_interaction_callback', '_record_command_lock', '_request_record_end',
         'init_robot_control_parameters_from_user_task', '_inference_timer_callback',
         '_cloud_leader_active', '_cloud_missing_source', '_cloud_hello_fields', '_publish_cloud_state',
         '_write_cloud_marker', '_remove_cloud_marker', '_shutdown_cloud_client',
         '_init_cloud_inference_state', '_assert_no_other_active', '_setup_timer_callbacks')

NS = {
    'cloud_client': types.SimpleNamespace(CloudClient=FakeCloudClient, EndInfo=real_cloud_client.EndInfo),
    'cloud_contract': cloud_contract, 'cloud_protocol': cloud_protocol, 'cloud_schedule': cloud_schedule,
    'cloud_texts_de': cloud_texts_de,
    'secrets': __import__('secrets'), 'time': CLOCK, 'math': __import__('math'), 'threading': threading, 'np': np, 'os': os,
    'json': json, 'contextlib': contextlib, 'TaskStatus': TaskStatus,
    'SendCommand': types.SimpleNamespace(Request=_Req), 'record_texts_de': record_texts_de,
    'COMMAND_LOCK_TIMEOUT_S': _module_constant('COMMAND_LOCK_TIMEOUT_S'),
    'parse_topic_list_with_names': _parse_topic_list_with_names,
    'DataManager': FakeDataManager, 'SlotSampler': None, 'TakeIntegrity': None,
    'MutuallyExclusiveCallbackGroup': Group, 'QoSProfile': QoSProfile, 'String': String,
    'ReliabilityPolicy': types.SimpleNamespace(RELIABLE='RELIABLE'),
    'DurabilityPolicy': types.SimpleNamespace(TRANSIENT_LOCAL='TRANSIENT_LOCAL'),
    'InferenceManager': None,
    'print': print,
}
exec(compile(textwrap.dedent(next(
    ast.get_source_segment(_TM_SRC, n) for n in ast.walk(ast.parse(_TM_SRC))
    if isinstance(n, ast.ClassDef) and n.name == 'TimerManager')), 'timer_manager.py', 'exec'), NS)  # noqa: S102
for _n in _TREE.body:
    if isinstance(_n, ast.ClassDef) and _n.name == '_CloudRun':
        exec(compile(_segment(_n), str(_SERVER_PY), 'exec'), NS)  # noqa: S102
_cls = next(n for n in _TREE.body if isinstance(n, ast.ClassDef) and n.name == 'PhysicalAIServer')
for _n in _cls.body:
    if isinstance(_n, ast.FunctionDef) and _n.name in NAMES:
        exec(compile(_segment(_n), str(_SERVER_PY), 'exec'), NS)  # noqa: S102
assert all(n in NS for n in NAMES + ('_CloudRun', 'TimerManager')), [n for n in NAMES if n not in NS]


class Node:
    PHASE_INFERENCE_LOADING = 10
    DEFAULT_SAVE_ROOT_PATH = '/nonexistent'

    def __init__(self, cams=('gripper', 'scene'), profile='omx_full'):
        self.communicator = FakeComm(cams)
        self.on_recording = self.on_inference = self.on_calibration = self.on_workflow = False
        self.on_manual = self.on_leader_teach = self.is_training = False
        self._mode_lock = threading.Lock()
        self._cloud_run = None
        self._cloud_end_lock = threading.Lock()
        self._cloud_state_lock = threading.Lock()
        self._cloud_state_seq = 0
        self._cloud_last_end_text = None
        self._cloud_state_pub = Pub(String, cloud_contract.STATE_TOPIC, None)
        self._arm_profile = rp.resolve(profile)
        self.robot_type = 'omx_f'
        self.robot_profile = profile
        self.params = {'camera_topic_list': [f'{c}:/{c}/image_raw/compressed' for c in cams]}
        self.total_joint_order = list(JOINTS)
        self.joint_order = {'joint_order.leader': list(JOINTS)}
        self.timer_manager = None
        self.timers = []
        self.publishers = []
        self.data_manager = None
        self.operation_mode = 'collection'
        self.leader_live = False
        self.log = Log()
        self._record_cb_group = Group()
        self._setup_timer_callbacks()

    def leader_appears_active(self):
        if isinstance(self.leader_live, BaseException):
            raise self.leader_live
        return self.leader_live

    def create_timer(self, period, cb, callback_group=None):
        t = Timer(period, cb, callback_group)
        self.timers.append(t)
        return t

    def create_publisher(self, msg_type, topic, qos):
        p = Pub(msg_type, topic, qos)
        self.publishers.append(p)
        return p

    def get_logger(self):
        return self.log

    def _data_collection_timer_callback(self):
        pass

    def _enqueue_dataset_upload(self, *a, **k):
        raise AssertionError('a cloud run never uploads a dataset')


for _name in NAMES:
    setattr(Node, _name, NS[_name])


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    CLOCK.t = 1000.0
    FakeCloudClient.instances = []
    FakeCloudClient.raise_on_init = None
    FakeDataManager.instances = []
    contract = types.SimpleNamespace(**{k: getattr(cloud_contract, k) for k in dir(cloud_contract)
                                        if not k.startswith('__')})
    contract.ACTIVE_MARKER_PATH = str(tmp_path / 'edubotics-cloud-inference.active')
    monkeypatch.setitem(NS, 'cloud_contract', contract)
    yield contract


def task(fps=30, instr=('',), record=False):
    return types.SimpleNamespace(task_name='', task_type='inference', user_id='', task_instruction=list(instr),
                                 policy_path='SvenBorodun/act_omx', fps=fps, record_inference_mode=False
                                 if not record else True, push_to_hub=False)


def start(node, tok=TOK, edge=EDGE, info=None):
    req = types.SimpleNamespace(task_info=info or task(), session_token=tok, edge_token=edge)
    return node.start_cloud_inference_callback(req, types.SimpleNamespace(success=None, message=None))


def client():
    return FakeCloudClient.instances[-1]


def tick(node, n=1, dt=1.0 / 30):
    for _ in range(n):
        node._cloud_inference_tick()
        CLOCK.t += dt


def chunk(obs_e=0, n=50, value=None):
    a = np.arange(n * 6, dtype=np.float32).reshape(n, 6) / 100.0 + obs_e
    if value is not None:
        a[:] = value
    return a.astype(np.float32)


def answer_all(c):
    """Answer every offered observation that has no answer yet (an instant cloud)."""
    done = getattr(c, '_answered', set())
    for _, seq, obs_e in c.offers:
        if seq not in done:
            done.add(seq)
            c.chunks.append((seq, obs_e, chunk(obs_e)))
    c._answered = done


def running_node(**kw):
    """A started run whose client is ready and whose first chunk was adopted."""
    node = Node(**kw)
    r = start(node)
    assert r.success, r.message
    c = client()
    c.ready = (dict(POLICY), dict(LIMITS))
    tick(node)                                          # schedule built + first ticket
    (_, seq, obs_e), = c.offers
    c.chunks = [(seq, obs_e, chunk(obs_e))]
    tick(node)                                          # adopted, first action published
    return node, c


def end_status(node):
    s = node.communicator.statuses[-1]
    assert s.phase == TaskStatus.READY
    return s.error


# ── the start ────────────────────────────────────────────────────────────────

def test_start_claims_mode_lock(_env):
    node = Node()
    seen = []

    class SpyLock:
        def __enter__(self):
            seen.append(('enter', node.on_inference))

        def __exit__(self, *a):
            seen.append(('exit', node.on_inference))

    node._mode_lock = SpyLock()
    r = start(node)
    assert r.success and r.message == cloud_texts_de.STARTED_DE
    assert seen == [('enter', False), ('exit', True)]        # check + flag set under the lock
    assert node.on_inference and node._cloud_run is not None
    assert node.operation_mode == 'inference'
    c = client()
    assert c.started and c.tokens() == (TOK, EDGE)
    assert cloud_protocol.CONN_ID_RE.fullmatch(c.conn_id)
    assert c.hello == {'robot_type': 'omx_f', 'robot_profile': 'omx_full',
                       'cameras': [('gripper', 640, 480), ('scene', 640, 480)], 'state_dim': 6, 'fps': 30.0}
    (timer,) = node.timers
    assert timer.callback == node._cloud_inference_tick and timer.group is None
    assert list(node.timer_manager._timer) == ['inference']
    assert node.communicator.statuses[-1].phase == node.PHASE_INFERENCE_LOADING


def test_start_refused_when_busy():
    for flag, sentence in (('on_recording', 'Aufnahme läuft gerade — bitte zuerst stoppen.'),
                           ('on_inference', 'Inferenz läuft gerade — bitte zuerst stoppen.'),
                           ('on_workflow', 'Ein Workflow läuft gerade — bitte zuerst stoppen.')):
        node = Node()
        setattr(node, flag, True)
        r = start(node)
        assert (r.success, r.message) == (False, sentence)
        assert FakeCloudClient.instances == [] and node._cloud_run is None
    # a second start while a cloud run is live is refused in German
    node, c = running_node()
    r = start(node)
    assert (r.success, r.message) == (False, 'Inferenz läuft gerade — bitte zuerst stoppen.')
    assert node._cloud_run is not None and len(FakeCloudClient.instances) == 1


def test_start_refuses_non_inferable_profile():
    node = Node(cams=('scene',), profile='edu6_studio')
    r = start(node)
    assert (r.success, r.message) == (False, cloud_texts_de.NOT_INFERABLE_DE)
    assert not node.on_inference and FakeCloudClient.instances == []


def test_start_refuses_a_degraded_node():
    node = Node()
    node.communicator = None
    r = start(node)
    assert r.success is False and r.message == (
        'Roboter-Initialisierung fehlgeschlagen — bitte die Umgebung neu starten (Details im Protokoll).')


@pytest.mark.parametrize('tok,edge', [
    (TOK[:-1], EDGE), (TOK + 'x', EDGE), (TOK[:-1] + '!', EDGE), ('', EDGE),
    (TOK, ''), (TOK, 'has a space-and-more-chars'), (TOK, 'cr\r\nlf-0123456789abc'), (TOK, 'x' * 4097),
    (TOK, 'short'), (None, EDGE), (TOK, 7)])
def test_start_refuses_a_bad_connect_or_edge_token_without_echo(tok, edge):
    node = Node()
    r = start(node, tok=tok, edge=edge)
    assert (r.success, r.message) == (False, cloud_texts_de.BAD_SESSION_DE)
    assert not node.on_inference and FakeCloudClient.instances == []
    for value in (tok, edge):
        if isinstance(value, str) and value:
            assert value not in r.message and value not in node.log.text()


def test_start_refused_while_leader_active():
    node = Node()
    node.leader_live = True
    r = start(node)
    assert (r.success, r.message) == (False, cloud_texts_de.LEADER_ACTIVE_DE)
    assert not node.on_inference                              # the claim is released
    assert FakeCloudClient.instances == [] and node.timers == []
    assert node.communicator.actions == []


def test_leader_check_exception_does_not_block():
    node = Node()
    node.leader_live = RuntimeError('broken monitor')
    r = start(node)
    assert r.success
    assert any(level == 'warning' and 'RuntimeError' in m for level, m in node.log.lines)
    client().ready = (dict(POLICY), dict(LIMITS))
    tick(node, 40)
    assert node._cloud_run is not None


def test_start_preflight_runs_the_summary_free_checks(_env):
    node = Node()
    r = start(node, info=task(fps=0))
    assert r.success is False and r.message.startswith('Ungültige fps-Einstellung (0).')
    node = Node()
    r = start(node, info=task(record=True))
    assert r.success is False and r.message.startswith('Aufnahme während der Inferenz')
    assert not node.on_inference and FakeCloudClient.instances == []
    assert not os.path.exists(_env.ACTIVE_MARKER_PATH)


def test_start_failure_releases_everything_and_logs_only_the_type(_env):
    FakeCloudClient.raise_on_init = RuntimeError(f'boom {TOK} {EDGE}')
    node = Node()
    r = start(node)
    assert (r.success, r.message) == (False, cloud_texts_de.START_FAILED_DE)
    assert not node.on_inference and node._cloud_run is None
    assert not os.path.exists(_env.ACTIVE_MARKER_PATH)
    assert 'RuntimeError' in node.log.text()
    assert TOK not in node.log.text() and EDGE not in node.log.text()


# ── the leader (D23, R-1) ───────────────────────────────────────────────────

def test_leader_turning_live_ends_the_run(_env):
    node, c = running_node()
    published = len(node.communicator.actions)
    node.leader_live = True
    for _ in range(31):                                       # at most one 1 s check later
        tick(node)
        if node._cloud_run is None:
            break
    assert node._cloud_run is None
    assert end_status(node) == cloud_texts_de.LEADER_BECAME_ACTIVE_DE
    assert c.closed == [('robot_error', c.closed[0][1])]
    n = len(node.communicator.actions)
    assert n - published <= 31
    tick(node, 10)
    assert len(node.communicator.actions) == n               # nothing after the end
    assert not os.path.exists(_env.ACTIVE_MARKER_PATH)
    assert not node.on_inference


# ── ends: FINISH / STOP / stop service / idempotent ─────────────────────────

def _command(node, cmd):
    req = types.SimpleNamespace(command=cmd, task_info=task())
    return node.user_interaction_callback(req, types.SimpleNamespace(success=None, message=None))


def test_finish_and_stop_end_the_cloud_run(_env):
    for cmd in (_Req.FINISH, _Req.STOP):
        node, c = running_node()
        r = _command(node, cmd)
        assert (r.success, r.message) == (True, 'All operations terminated')
        assert node._cloud_run is None and not node.on_inference
        assert c.closed[0][0] == 'robot_stop'
        assert end_status(node) == ''
        assert not os.path.exists(_env.ACTIVE_MARKER_PATH)
        assert node.timers[0].destroyed


def test_finish_from_the_record_page_answers_in_german():
    node, c = running_node()
    req = types.SimpleNamespace(command=_Req.FINISH, task_info=types.SimpleNamespace(task_type='record'))
    r = node.user_interaction_callback(req, types.SimpleNamespace(success=None, message=None))
    assert (r.success, r.message) == (True, 'Wird beendet.') and node._cloud_run is None


def test_finish_with_silent_cameras_ends():
    node, c = running_node()
    node.communicator.camera_topic_msgs['scene'] = None    # nothing arrives any more
    r = _command(node, _Req.FINISH)
    assert r.success and node._cloud_run is None and c.closed[0][0] == 'robot_stop'


def test_stop_service_ends_only_a_cloud_run():
    node, c = running_node()
    r = node.cloud_inference_stop_callback(None, types.SimpleNamespace(success=None, message=None))
    assert (r.success, r.message) == (True, cloud_texts_de.STOPPED_DE)
    assert c.closed[0][0] == 'robot_stop'
    r = node.cloud_inference_stop_callback(None, types.SimpleNamespace(success=None, message=None))
    assert (r.success, r.message) == (False, cloud_texts_de.NOT_RUNNING_DE)
    # a LOCAL inference is not touched
    local = Node()
    local.on_inference = True
    r = local.cloud_inference_stop_callback(None, types.SimpleNamespace(success=None, message=None))
    assert (r.success, r.message) == (False, cloud_texts_de.NOT_RUNNING_DE)
    assert local.on_inference is True


def test_end_inference_idempotent():
    node, c = running_node()
    assert node._end_inference('', end_reason='robot_stop') is True
    assert node._end_inference('', end_reason='robot_stop') is False
    assert len(c.closed) == 1
    reason, stats = c.closed[0]
    assert reason == 'robot_stop' and stats['executed'] >= 1 and 'adopted' in stats
    assert len([s for s in node.communicator.statuses if s.phase == TaskStatus.READY]) == 1


def test_end_inference_steps_are_individually_guarded():
    node, c = running_node()
    c.close = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x'))
    node.timer_manager = None
    assert node._end_inference('Text', end_reason='robot_error') is True
    assert node._cloud_run is None and not node.on_inference
    assert end_status(node) == 'Text'


# ── server / transport ends ─────────────────────────────────────────────────

@pytest.mark.parametrize('end,sentence', [
    (('server', 'ended', 'superseded', None), cloud_texts_de.SUPERSEDED_DE),
    (('server', 'ended', 'expired', None), cloud_texts_de.EXPIRED_DE),
    (('server', 'ended', 'client_gone', None), cloud_texts_de.CLIENT_GONE_DE),
    (('server', 'ended', 'user_stop', None), ''),
    (('server', 'cameras', None, None), cloud_texts_de.CAMERAS_DE),
    (('server', 'version', None, (2,)), cloud_texts_de.VERSION_ROBOT_OLD_DE),
    (('robot', 'unreachable', None, None), cloud_texts_de.UNREACHABLE_DE),
    (('robot', 'cloud_unavailable', None, None), cloud_texts_de.CLOUD_UNAVAILABLE_DE)])
def test_server_end_reasons_map_to_german(end, sentence):
    node, c = running_node()
    c.end = real_cloud_client.EndInfo(*end)
    tick(node)
    assert node._cloud_run is None
    assert end_status(node) == sentence
    assert c.closed == [(None, c.closed[0][1])]              # the server ended it: no bye


def test_session_gone_sentences():
    node = Node()
    start(node)
    client().end = real_cloud_client.EndInfo('robot', 'session_gone', None, None)
    tick(node)
    assert end_status(node) == cloud_texts_de.SESSION_INVALID_DE
    node, c = running_node()
    c.end = real_cloud_client.EndInfo('robot', 'session_gone', 'after_ready', None)
    tick(node)
    assert end_status(node) == cloud_texts_de.ENDED_DE


# ── the ready preflight ─────────────────────────────────────────────────────

def _ready_end(policy=None, limits=None, info=None, cams=('gripper', 'scene')):
    node = Node(cams=cams)
    assert start(node, info=info).success
    client().ready = (dict(POLICY, **(policy or {})), dict(LIMITS, **(limits or {})))
    tick(node)
    assert node._cloud_run is None
    return end_status(node), node


def test_preflight_against_ready_uses_the_local_sentences():
    expected = NS['_preflight_contract'](None, {'expected_cameras': ['gripper', 'scene', 'wrist'],
                                                'requires_task': False, 'trained_fps': 30.0},
                                         task(), {'gripper', 'scene'})[0]
    sentence, node = _ready_end({'expected_cameras': ['gripper', 'scene', 'wrist'],
                                 'image_shapes': dict(POLICY['image_shapes'], wrist=[3, 480, 640])})
    assert sentence == expected and sentence.startswith('Dieses Modell erwartet die Kamera(s) wrist')
    assert client().closed[0][0] == 'robot_error'
    sentence, _ = _ready_end({'trained_fps': 15.0})
    assert sentence.startswith('Dieses Modell wurde mit 15 fps trainiert, die Inferenz ist auf 30 fps')
    sentence, _ = _ready_end({'requires_task': True})
    assert sentence.startswith('Dieses Modell benötigt eine Aufgabenanweisung')
    assert _ready_end({'type': 'diffusion'})[0] == cloud_texts_de.MODEL_TYPE_DE
    assert _ready_end({'state_dim': 7})[0] == cloud_texts_de.state_dim_de(7, 6)
    assert _ready_end({'action_dim': 7})[0] == cloud_texts_de.action_dim_de(7, 6)
    assert _ready_end({'chunk_size': 20})[0] == cloud_texts_de.MALFORMED_DE
    # a chunk_size of at least actions_per_chunk is fine
    node = Node()
    start(node)
    client().ready = (dict(POLICY, chunk_size=50), dict(LIMITS))
    tick(node)
    assert node._cloud_run is not None and node._cloud_run.schedule is not None


# ── observations ────────────────────────────────────────────────────────────

def _first_ticket_end(node):
    start(node)
    client().ready = (dict(POLICY), dict(LIMITS))
    tick(node)
    return node


def test_image_size_from_sof_refused():
    node = Node()
    node.communicator.camera_topic_msgs['scene'] = CamMsg(JPEG_1280)
    _first_ticket_end(node)
    assert node._cloud_run is None
    assert end_status(node) == cloud_texts_de.image_size_de('scene', 1280, 720, 640, 480)
    node = Node()
    node.communicator.camera_topic_msgs['gripper'] = CamMsg(b'\x89PNG\r\n\x1a\n' + b'0' * 100)
    _first_ticket_end(node)
    assert end_status(node) == cloud_texts_de.not_jpeg_de('gripper')
    big = (JPEG_640[:2] + (b'\xff\xef' + (65533).to_bytes(2, 'big') + b'\x00' * 65531) * 9 + JPEG_640[2:])
    assert cloud_protocol.jpeg_size(big) == (640, 480) and len(big) > cloud_protocol.MAX_JPEG_BYTES
    node = Node()
    node.communicator.camera_topic_msgs['gripper'] = CamMsg(big)
    _first_ticket_end(node)
    assert end_status(node) == cloud_texts_de.image_too_big_de('gripper')


def test_cloud_tick_never_decodes(monkeypatch):
    def _no(*a, **k):
        raise AssertionError('the cloud tick decoded an image')
    monkeypatch.setattr(cv2, 'imdecode', _no)
    node, c = running_node()
    for _ in range(60):
        answer_all(c)
        tick(node)
    assert node._cloud_run is not None and len(c.offers) >= 5
    (frame, seq, obs_e) = c.offers[0]
    header, payload = cloud_protocol.decode(frame)
    s, e, cams, state, _ = cloud_protocol.parse_obs(header, payload)
    assert (s, e) == (seq, obs_e)
    assert list(cams) == ['gripper', 'scene'] and cams['gripper'] == JPEG_640
    assert state.dtype == np.float32 and state.tolist() == pytest.approx([0.1 * i for i in range(6)])


def test_extra_cameras_are_not_sent_and_warned_once(capsys):
    node = Node(cams=('gripper', 'scene', 'wrist'))
    start(node)
    client().ready = (dict(POLICY), dict(LIMITS))
    tick(node)
    (frame, _, _), = client().offers
    _, _, cams, _, _ = cloud_protocol.parse_obs(*cloud_protocol.decode(frame))
    assert list(cams) == ['gripper', 'scene']
    out = capsys.readouterr().out + capsys.readouterr().err
    assert out.count('[WARNUNG] Zusätzliche Kameras werden ignoriert:') == 1


def test_a_bad_state_ends_with_the_state_sentence():
    node = Node()
    node.communicator.follower_topic_msgs['follower'] = JointMsg([0.0, float('nan'), 0, 0, 0, 0])
    _first_ticket_end(node)
    assert end_status(node) == cloud_texts_de.STATE_DE
    node = Node()
    node.communicator.follower_topic_msgs['follower'] = JointMsg([0.0] * 5)
    _first_ticket_end(node)
    assert end_status(node) == cloud_texts_de.STATE_DE


def test_stale_camera_warns_and_the_run_continues(capsys):
    node, c = running_node()                        # the fake cameras never change their stamp
    for _ in range(30 * 7):
        answer_all(c)
        tick(node)
    assert node._cloud_run is not None
    assert node._cloud_run.stale_camera in ('gripper', 'scene')
    out = capsys.readouterr()
    assert (out.out + out.err).count('liefert seit über 5s dasselbe Bild') == 1   # ≤ every 5 s
    node._publish_cloud_state()
    state = json.loads(node._cloud_state_pub.sent[-1].data)
    assert state['stale_camera'] in ('gripper', 'scene')


# ── actions, hold, status rate ──────────────────────────────────────────────

def test_actions_go_through_tensor_array2joint_msgs():
    node, c = running_node()
    conv = node.data_manager.data_converter
    action, types_, order = conv.calls[0]
    assert np.array_equal(action, chunk(0)[0])                # chunk 0 row 0 is the first action
    assert types_ == {'leader': 'JointTrajectory'} and order == node.joint_order
    assert node.communicator.actions[0]['leader'][0] == 'JointTrajectory'
    assert np.array_equal(node.communicator.actions[0]['leader'][1], chunk(0)[0])


def test_hold_publishes_nothing():
    node, c = running_node()
    tick(node, 60)                                            # no further chunk ever arrives
    n = len(node.communicator.actions)
    assert n == 50
    tick(node, 30)
    assert len(node.communicator.actions) == 50
    assert node._cloud_run.schedule.holding
    node._publish_cloud_state()
    state = json.loads(node._cloud_state_pub.sent[-1].data)
    assert state['phase'] == 'holding' and state['hold_s'] is not None and state['active'] is True


def test_requests_that_end_without_a_chunk_free_the_slot():
    node, c = running_node()
    tick(node, 6)
    (_, seq, obs_e) = c.offers[-1]
    before = len(c.offers)
    tick(node, 10)                                            # still on the wire: nothing new
    assert len(c.offers) == before
    c.chunks = [(seq, obs_e, None)]                           # it ended without a chunk
    for _ in range(6):                                        # the next cadence point sends again
        tick(node)
        if len(c.offers) > before:
            break
    assert len(c.offers) == before + 1 and node._cloud_run is not None
    assert c.offers[-1][1] == seq + 1


def test_task_status_at_most_2_hz():
    node, c = running_node()
    n0 = len(node.communicator.statuses)
    tick(node, 90)                                            # 3 s at 30 Hz, one phase
    n = len(node.communicator.statuses) - n0
    assert 5 <= n <= 7
    phases = [s.phase for s in node.communicator.statuses]
    assert node.PHASE_INFERENCE_LOADING in phases and TaskStatus.INFERENCING in phases
    for s in node.communicator.statuses:
        assert s.task_info is None or not hasattr(s.task_info, 'session_token')


def test_a_phase_change_publishes_at_once():
    node = Node()
    start(node)
    c = client()
    c.ready = (dict(POLICY), dict(LIMITS))
    tick(node)
    (_, seq, obs_e), = c.offers
    c.chunks = [(seq, obs_e, chunk(obs_e))]
    n = len(node.communicator.statuses)
    tick(node)
    assert len(node.communicator.statuses) == n + 1
    assert node.communicator.statuses[-1].phase == TaskStatus.INFERENCING


# ── state topic + marker ────────────────────────────────────────────────────

def test_state_payload_keys_in_order():
    node = Node()
    node._publish_cloud_state()
    idle = json.loads(node._cloud_state_pub.sent[-1].data, object_pairs_hook=list)
    assert [k for k, _ in idle] == list(cloud_contract.STATE_KEYS)
    idle = dict(idle)
    assert cloud_contract.STATE_SCHEMA_VERSION == 1
    assert idle['v'] == 1 and idle['active'] is False
    assert idle['phase'] == 'idle' and idle['end_text_de'] is None
    node, c = running_node()
    node._publish_cloud_state()
    live = json.loads(node._cloud_state_pub.sent[-1].data)
    assert list(live) == list(cloud_contract.STATE_KEYS)
    assert live['phase'] == 'running' and live['active'] is True
    assert live['rtt_ms_p50'] == 101.0 and live['gpu_ms_p50'] == 23.0
    assert live['plan_age_ms'] is not None and live['hold_s'] is None
    assert 3290 <= live['ends_in_s'] <= 3300
    seqs = [json.loads(m.data)['seq'] for m in node._cloud_state_pub.sent]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    node._end_inference(cloud_texts_de.ENDED_DE, end_reason=None)
    ended = json.loads(node._cloud_state_pub.sent[-1].data)
    assert ended['phase'] == 'ended' and ended['end_text_de'] == cloud_texts_de.ENDED_DE
    assert ended['active'] is False
    assert all(p in cloud_contract.PHASES for p in (idle['phase'], live['phase'], ended['phase']))
    assert node._cloud_state_pub.sent[-1].data == json.dumps(ended, ensure_ascii=False)


def test_publish_cloud_state_never_raises():
    node = Node()
    node._cloud_state_pub = types.SimpleNamespace(publish=lambda m: (_ for _ in ()).throw(RuntimeError('x')))
    node._publish_cloud_state()
    node._cloud_state_pub = None
    node._publish_cloud_state()


def test_marker_written_and_removed(_env):
    marker = _env.ACTIVE_MARKER_PATH
    node, c = running_node()
    assert os.path.exists(marker)
    assert open(marker).read() == ''                          # nothing in it, no token
    node.cloud_inference_stop_callback(None, types.SimpleNamespace(success=None, message=None))
    assert not os.path.exists(marker)
    # a stale marker of a crashed node is removed at boot
    open(marker, 'w').close()
    fresh = Node()
    fresh._init_cloud_inference_state()
    assert not os.path.exists(marker)


def test_init_cloud_inference_state_publishes_latched_on_its_own_group():
    node = Node()
    node._cloud_state_pub = None
    node._init_cloud_inference_state()
    (pub,) = node.publishers
    assert pub.topic == cloud_contract.STATE_TOPIC and pub.msg_type is String
    assert pub.qos.kw == {'depth': 1, 'reliability': 'RELIABLE', 'durability': 'TRANSIENT_LOCAL'}
    (timer,) = node.timers
    assert timer.period == cloud_contract.PUBLISH_PERIOD_S and timer.callback == node._publish_cloud_state
    assert isinstance(timer.group, Group)
    assert len(pub.sent) == 1                                  # published once at boot


def test_shutdown_closes_the_client(_env):
    node, c = running_node()
    node._shutdown_cloud_client()
    assert c.closed[0][0] == 'robot_stop' and c.joined == [1.0]
    assert not os.path.exists(_env.ACTIVE_MARKER_PATH)
    Node()._shutdown_cloud_client()                           # nothing live: a no-op
    main = next(n for n in _TREE.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
    tries = [n for n in ast.walk(main) if isinstance(n, ast.Try) and n.finalbody]
    first = ast.unparse(tries[0].finalbody[0])
    assert first == 'node._shutdown_cloud_client()', first


# ── tokens never leak (D4, R-9) ─────────────────────────────────────────────

def test_tokens_absent_from_task_status_logs_and_state():
    node, c = running_node()
    tick(node, 40)
    node._publish_cloud_state()
    _command(node, _Req.FINISH)
    for s in node.communicator.statuses:
        blob = repr(vars(s)) + repr(vars(s.task_info) if s.task_info is not None else '')
        assert TOK not in blob and EDGE not in blob
    for m in node._cloud_state_pub.sent:
        assert TOK not in m.data and EDGE not in m.data
    assert TOK not in node.log.text() and EDGE not in node.log.text()
    run = NS['_CloudRun'](task_info=task(), started=0.0, conn_id='x')
    for k, v in vars(run).items():
        assert TOK not in repr(v) and EDGE not in repr(v), k
    for dm in FakeDataManager.instances:
        assert TOK not in repr(vars(dm.kw['task_info'])) and EDGE not in repr(vars(dm.kw['task_info']))
    assert TOK not in repr(c) and EDGE not in repr(c)


# ── stall / loading / internal (R-13, R-14, R-15, F-7) ──────────────────────

def test_stall_sentence_uses_the_clamped_value():
    for served, shown, when in ((60.0, 15, 15.0), (4.0, 4, 4.0)):
        node = Node()
        start(node)
        c = client()
        c.ready = (dict(POLICY), dict(LIMITS, stall_end_s=served))
        tick(node)
        (_, seq, obs_e), = c.offers
        c.chunks = [(seq, obs_e, chunk(obs_e))]
        tick(node)
        t_adopt = CLOCK.t - 1.0 / 30
        while node._cloud_run is not None:
            tick(node)                              # nothing is ever answered again
            assert CLOCK.t - t_adopt < when + 1.0
        assert end_status(node) == cloud_texts_de.stall_de(float(shown))
        assert f'Seit {shown} Sekunden' in end_status(node)
        assert c.closed[0][0] == 'stall'


def test_stall_with_robot_data_missing_names_the_source():
    node, c = running_node()
    tick(node, 3)
    node.communicator.camera_topic_msgs['scene'] = None        # the latest data was cleared
    while node._cloud_run is not None:
        tick(node)
    assert end_status(node) == cloud_texts_de.no_data_de('Kamera „scene“')
    assert c.closed[0][0] == 'robot_error'
    node, c = running_node()
    node.communicator.follower_topic_msgs['follower'] = None
    while node._cloud_run is not None:
        tick(node)
    assert end_status(node) == cloud_texts_de.no_data_de('Der Follower-Arm')


def test_missing_source_when_get_latest_data_is_none():
    node = Node(cams=('gripper', 'scene', 'wrist'))
    node.communicator.camera_topic_msgs['wrist'] = None       # configured, unused by the model
    start(node)
    client().ready = (dict(POLICY), dict(LIMITS))
    while node._cloud_run is not None:
        tick(node, dt=0.5)
    assert end_status(node) == cloud_texts_de.no_data_de('Kamera „wrist“')
    run = NS['_CloudRun'](task_info=task(), started=0.0, conn_id='x')
    run.policy = dict(POLICY)
    node2 = Node()
    node2.communicator.camera_topic_msgs['gripper'] = None
    assert node2._cloud_missing_source(run, None, None) == 'Kamera „gripper“'
    node3 = Node()
    assert node3._cloud_missing_source(run, None, None) == 'Der Follower-Arm'


def test_loading_timeout_without_data_names_the_source():
    node = Node()
    node.communicator.camera_topic_msgs['gripper'] = None
    start(node)
    client().ready = (dict(POLICY), dict(LIMITS))
    t0 = CLOCK.t
    while node._cloud_run is not None:
        tick(node, dt=0.5)
    assert CLOCK.t - t0 == pytest.approx(cloud_schedule.LOADING_BUDGET_S, abs=1.0)
    assert end_status(node) == cloud_texts_de.no_data_de('Kamera „gripper“')
    assert client().closed[0][0] == 'robot_error'


def test_loading_timeout_ends_as_robot_error():
    node = Node()
    start(node)
    t0 = CLOCK.t
    while node._cloud_run is not None:                         # the cloud never answers ready
        tick(node, dt=0.5)
    assert CLOCK.t - t0 == pytest.approx(cloud_schedule.LOADING_BUDGET_S, abs=1.0)
    assert end_status(node) == cloud_texts_de.LOADING_TIMEOUT_DE
    assert client().closed[0][0] == 'robot_error'
    # with ready but the robot data present and no chunk ever: the schedule's loading end
    node = Node()
    start(node)
    client().ready = (dict(POLICY), dict(LIMITS))
    while node._cloud_run is not None:
        tick(node, dt=0.5)
    assert end_status(node) == cloud_texts_de.LOADING_TIMEOUT_DE
    assert client().closed[0][0] == 'robot_error'


def test_tick_exception_ends_in_german():
    node, c = running_node()
    node.communicator.raise_on_latest = RuntimeError('kaputt')
    tick(node)
    assert node._cloud_run is None
    assert end_status(node) == cloud_texts_de.INTERNAL_DE
    assert c.closed[0][0] == 'robot_error'
    assert any('RuntimeError' in m and 'kaputt' in m for _, m in node.log.lines)


def test_malformed_and_nonfinite_chunks_end():
    for bad, sentence in ((chunk(0, n=49), cloud_texts_de.MALFORMED_DE),
                          (chunk(0, value=np.nan), cloud_texts_de.NONFINITE_DE)):
        node = Node()
        start(node)
        c = client()
        c.ready = (dict(POLICY), dict(LIMITS))
        tick(node)
        (_, seq, obs_e), = c.offers
        c.chunks = [(seq, obs_e, bad)]
        tick(node)
        assert end_status(node) == sentence
        assert node.communicator.actions == []                 # never clamped, never published


def test_a_stray_tick_after_the_end_stops_the_timer():
    node, c = running_node()
    timer = node.timers[0]
    node._end_inference('', end_reason='robot_stop')
    node.timer_manager.start(timer_name='inference')           # pretend the timer fired again
    node._cloud_inference_tick()
    assert node.timer_manager._timer['inference'] is None


def test_on_inference_cleared_elsewhere_ends_the_run():
    node, c = running_node()
    node.on_inference = False
    tick(node)
    assert node._cloud_run is None and c.closed[0][0] == 'robot_stop'


# ── wiring fences ───────────────────────────────────────────────────────────

def test_no_timer_callback_dict_entry():
    fn = next(n for n in _cls.body if isinstance(n, ast.FunctionDef) and n.name == '_setup_timer_callbacks')
    d = next(n for n in ast.walk(fn) if isinstance(n, ast.Dict))
    assert [k.value for k in d.keys] == ['collection', 'inference']
    node, _ = running_node()
    assert set(node.timer_callback_dict) == {'collection', 'inference'}
    assert node.timers[0].callback == node._cloud_inference_tick


def test_the_two_services_are_registered_on_the_default_group():
    fn = next(n for n in _cls.body if isinstance(n, ast.FunctionDef) and n.name == '_init_ros_service')
    entries = {ast.unparse(e.elts[0]): e for e in ast.walk(fn)
               if isinstance(e, ast.Tuple) and e.elts and ast.unparse(e.elts[0]).startswith('cloud_contract.')}
    assert set(entries) == {'cloud_contract.START_SERVICE', 'cloud_contract.STOP_SERVICE'}
    start_e = entries['cloud_contract.START_SERVICE']
    stop_e = entries['cloud_contract.STOP_SERVICE']
    assert len(start_e.elts) == 3 and len(stop_e.elts) == 3
    assert ast.unparse(start_e.elts[1]) == 'StartCloudInference'
    assert ast.unparse(start_e.elts[2]) == 'self.start_cloud_inference_callback'
    assert ast.unparse(stop_e.elts[1]) == 'Trigger'
    assert ast.unparse(stop_e.elts[2]) == 'self.cloud_inference_stop_callback'


def test_init_wires_the_state_after_daten_and_before_the_profile():
    init = next(n for n in _cls.body if isinstance(n, ast.FunctionDef) and n.name == '__init__')
    stmts = [ast.unparse(s) for s in init.body]
    i = next(k for k, s in enumerate(stmts) if 'self._init_cloud_inference_state()' in s)
    daten = next(k for k, s in enumerate(stmts) if 'DatenService(self)' in s)
    assert daten < i
    assert isinstance(init.body[i], ast.Try)
    assert 'type(e).__name__' in stmts[i]
    for field in ('self._cloud_run = None', 'self._cloud_end_lock = threading.Lock()',
                  'self._cloud_state_lock = threading.Lock()', 'self._cloud_state_seq = 0',
                  'self._cloud_last_end_text = None', 'self._cloud_state_pub = None'):
        k = stmts.index(field)
        assert k < stmts.index('self._init_core_components()'), field


def test_the_imports():
    src = _SOURCE
    assert ('from physical_ai_server.inference import cloud_client, cloud_contract, cloud_protocol, '
            'cloud_schedule, cloud_texts_de') in src
    assert '\nimport secrets\n' in src and '\nimport numpy as np\n' in src
    assert '\nfrom std_srvs.srv import Trigger\n' in src
    srv = next(n for n in _TREE.body if isinstance(n, ast.ImportFrom) and n.module == 'physical_ai_interfaces.srv')
    names = [a.name for a in srv.names]
    assert 'StartCloudInference' in names and names == sorted(names)

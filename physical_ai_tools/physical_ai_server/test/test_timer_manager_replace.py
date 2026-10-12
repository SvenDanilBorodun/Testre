"""Two local-inference fixes the cloud path relies on (spec §5.5 items 6 + 7, D19).

1. ``init_robot_control_parameters_from_user_task`` stops the PREVIOUS TimerManager's timers before
   it replaces the manager: a second START used to leave the first run's timer registered on the
   node (a timer nobody could stop any more). It also takes an optional ``timer_callback``, which
   the cloud start uses to drive the ``'inference'`` timer with its own tick (F-14: no new
   ``timer_callback_dict`` entry, the timer keeps its name).
2. ``_inference_timer_callback`` checks ``on_inference`` FIRST (after the loading branch): a
   FINISH with silent cameras used to return at „Waiting for camera data..." on every tick and
   never tear down.

Exec'd from the AST onto stubs (rclpy is not importable here); the REAL TimerManager class runs
against a fake node.
"""

from __future__ import annotations

import ast
import textwrap
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1] / 'physical_ai_server'
_SERVER_PY = _ROOT / 'physical_ai_server.py'
_TIMER_PY = _ROOT / 'timer' / 'timer_manager.py'


def _exec(path, kind, names, ns):
    source = path.read_text(encoding='utf-8')
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, kind) and node.name in names:
            exec(compile(textwrap.dedent(ast.get_source_segment(source, node)), str(path), 'exec'), ns)  # noqa: S102
    assert all(n in ns for n in names), names
    return ns


_TM = _exec(_TIMER_PY, ast.ClassDef, ('TimerManager',), {'Node': object})['TimerManager']


class _Timer:
    def __init__(self, period, cb, group):
        self.period, self.callback, self.group = period, cb, group
        self.destroyed = False

    def destroy(self):
        self.destroyed = True


class _Phase:
    READY = 0
    INFERENCING = 6


class _Status:
    def __init__(self):
        self.phase = None
        self.error = ''


class _DM:
    def __init__(self, **kw):
        self.kw = kw
        self._dataset_lease = None

    def get_current_record_status(self):
        return _Status()


_NS = _exec(_SERVER_PY, ast.FunctionDef,
            ('init_robot_control_parameters_from_user_task', '_inference_timer_callback'),
            {'DataManager': _DM, 'TimerManager': _TM, 'TaskStatus': types.SimpleNamespace(
                READY=0, INFERENCING=6, __call__=None), 'SlotSampler': None, 'TakeIntegrity': None})
_NS['TaskStatus'] = type('TaskStatus', (_Status,), {'READY': 0, 'INFERENCING': 6})
INIT = _NS['init_robot_control_parameters_from_user_task']
TICK = _NS['_inference_timer_callback']


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, m, *a, **k):
        self.lines.append(str(m))

    error = warning = info


class _Node:
    DEFAULT_SAVE_ROOT_PATH = '/nonexistent'
    PHASE_INFERENCE_LOADING = 10

    def __init__(self):
        self.timers = []
        self.timer_manager = None
        self.operation_mode = 'inference'
        self.robot_type = 'omx_f'
        self._record_cb_group = object()
        self.communicator = types.SimpleNamespace(
            clear_latest_data=lambda: None, camera_topic_msgs={},
            published=[], get_latest_data=lambda: (None, None, None))
        self.communicator.publish_status = lambda status: self.communicator.published.append(status)
        self.timer_callback_dict = {'collection': self.collect, 'inference': self.infer}
        self.log = _Log()
        self._enqueue_dataset_upload = None
        self.cleared = 0
        self.inference_manager = types.SimpleNamespace(clear_policy=self._clear, policy=None)
        self._inference_policy_loading = False
        self.on_inference = True
        self.params = {'camera_topic_list': ['gripper:/g', 'scene:/s']}

    def _clear(self):
        self.cleared += 1

    def collect(self):
        pass

    def infer(self):
        pass

    def create_timer(self, period, cb, callback_group=None):
        t = _Timer(period, cb, callback_group)
        self.timers.append(t)
        return t

    def get_logger(self):
        return self.log


def _task(fps=30):
    return types.SimpleNamespace(fps=fps)


def test_second_init_stops_the_first_timers():
    node = _Node()
    INIT(node, _task())
    first_mgr = node.timer_manager
    (first,) = node.timers
    assert first.callback == node.infer and not first.destroyed
    INIT(node, _task())
    assert node.timer_manager is not first_mgr
    assert first.destroyed, 'the first run\'s timer is still registered on the node'
    second = node.timers[-1]
    assert not second.destroyed and second.callback == node.infer
    # the collision monitor's resume uses the NEW manager, under the unchanged name
    assert node.timer_manager._timer['inference'] is second


def test_timer_callback_parameter_drives_the_inference_timer_under_its_name():
    node = _Node()
    tick = object()
    INIT(node, _task(), timer_callback=tick)
    (timer,) = node.timers
    assert timer.callback is tick
    assert list(node.timer_manager._timer) == ['inference']
    assert timer.group is None                    # the default group, like local inference
    assert abs(timer.period - 1 / 30) < 1e-12


def test_local_inference_finish_tears_down_without_camera_data():
    node = _Node()
    INIT(node, _task())
    node.data_manager = _DM()
    node.on_inference = False                     # FINISH cleared it; the cameras are silent
    TICK(node)
    assert node.cleared == 1
    assert node.timers[0].destroyed
    assert node.communicator.published and node.communicator.published[-1].phase == 0
    assert 'Waiting for camera data...' not in node.log.lines


def test_loading_branch_still_comes_first():
    node = _Node()
    INIT(node, _task())
    node.data_manager = _DM()
    node._inference_policy_loading = True
    node.on_inference = False
    TICK(node)
    assert node.cleared == 0 and not node.timers[0].destroyed
    assert node.communicator.published[-1].phase == node.PHASE_INFERENCE_LOADING


def test_the_teardown_block_exists_once_and_before_get_latest_data():
    source = _SERVER_PY.read_text(encoding='utf-8')
    fn = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef)
              and n.name == '_inference_timer_callback')
    seg = ast.get_source_segment(source, fn)
    assert seg.count('if not self.on_inference:') == 1
    assert seg.index('if not self.on_inference:') < seg.index('get_latest_data()')
    assert seg.index('if self._inference_policy_loading:') < seg.index('if not self.on_inference:')

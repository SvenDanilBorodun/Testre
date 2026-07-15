"""Idle identity tick (T2 / D8).

Extracts ``_idle_status_tick`` by ``ast`` and execs it onto a stub node. Verifies
it publishes a bare READY TaskStatus (carrying the stamped identity) ONLY when
the node is fully idle, and stays SILENT during every active mode — in
particular the on_inference gate that covers the 10-30 s INFERENCE_LOADING
window, the _collision_active gate that keeps it from flickering against the
5 Hz collision watchdog, and the 3 s belt after any /task/status publish.
"""

from __future__ import annotations

import ast
import textwrap
import time
import types
from pathlib import Path

import pytest

_SERVER_PY = (
    Path(__file__).resolve().parents[1] / 'physical_ai_server' / 'physical_ai_server.py'
)


class _TaskStatus:
    READY = 0

    def __init__(self):
        self.phase = -1
        self.robot_type = ''
        self.robot_profile = ''
        self.capabilities_json = ''


def _load():
    source = _SERVER_PY.read_text(encoding='utf-8')
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == '_idle_status_tick':
            src = textwrap.dedent(ast.get_source_segment(source, node))
            ns = {'time': time, 'TaskStatus': _TaskStatus}
            exec(compile(src, str(_SERVER_PY), 'exec'), ns)  # noqa: S102
            return ns['_idle_status_tick']
    raise AssertionError('_idle_status_tick not found')


_idle = _load()


class _Comm:
    """Emulates communicator.publish_status: stamps identity from the node and
    updates the silence timestamp (so a real fire re-arms the 3 s belt)."""

    def __init__(self, node):
        self.node = node
        self.published = []

    def publish_status(self, status):
        if hasattr(status, 'robot_type') and not status.robot_type:
            status.robot_type = getattr(self.node, 'robot_type', '') or ''
        if hasattr(status, 'robot_profile') and not status.robot_profile:
            status.robot_profile = getattr(self.node, 'robot_profile', '') or ''
        if hasattr(status, 'capabilities_json') and not status.capabilities_json:
            status.capabilities_json = getattr(self.node, 'capabilities_json', '') or ''
        self.published.append(status)
        self.node._last_task_status_mono = time.monotonic()


class _Node:
    def __init__(self, **flags):
        self.on_recording = flags.get('on_recording', False)
        self.on_inference = flags.get('on_inference', False)
        self.on_workflow = flags.get('on_workflow', False)
        self.on_manual = flags.get('on_manual', False)
        self.on_calibration = flags.get('on_calibration', False)
        self._collision_active = flags.get('_collision_active', False)
        self._last_task_status_mono = flags.get('_last_task_status_mono', 0.0)
        self.robot_type = 'omx_f'
        self.robot_profile = 'omx_follower'
        self.capabilities_json = '{"inferable":true}'
        self.communicator = _Comm(self) if flags.get('has_comm', True) else None


def _tick(node):
    types.MethodType(_idle, node)()


def test_fires_when_fully_idle():
    node = _Node()
    _tick(node)
    assert len(node.communicator.published) == 1
    assert node.communicator.published[0].phase == _TaskStatus.READY


def test_tick_carries_identity_fields():
    node = _Node()
    _tick(node)
    st = node.communicator.published[0]
    assert st.robot_type == 'omx_f'
    assert st.robot_profile == 'omx_follower'
    assert st.capabilities_json == '{"inferable":true}'


@pytest.mark.parametrize('flag', [
    'on_recording', 'on_inference', 'on_workflow', 'on_manual', 'on_calibration',
    '_collision_active',
])
def test_suppressed_during_active_mode(flag):
    # on_inference specifically covers the INFERENCE_LOADING silent window;
    # on_calibration keeps READY out of a touch-off/extrinsic capture (audit
    # fix 3 — parity with _assert_no_other_active);
    # _collision_active keeps READY from fighting the 5 Hz collision watchdog.
    node = _Node(**{flag: True})
    _tick(node)
    assert node.communicator.published == []


def test_suppressed_within_3s_of_last_publish():
    node = _Node(_last_task_status_mono=time.monotonic())
    _tick(node)
    assert node.communicator.published == []


def test_fires_again_after_3s_belt_expires():
    node = _Node(_last_task_status_mono=time.monotonic() - 5.0)
    _tick(node)
    assert len(node.communicator.published) == 1


def test_no_crash_and_no_publish_without_communicator():
    node = _Node(has_comm=False)
    _tick(node)   # must not raise
    # nothing to assert beyond "did not raise"

"""Identity-stamp / no-wipe round-trip (T2 / H1, server side).

Two independent /task/status publish paths must stamp robot_type / robot_profile
/ capabilities_json so a bare tick never wipes the React-side identity:
  (1) communicator.publish_status — covers its 15 call sites incl. the idle tick
      and the notice branches;
  (2) the collision monitor's OWN publisher, which BYPASSES publish_status and
      emits bare TaskStatus() at up to 5 Hz during a trip — hence H1 needs
      server-side stamping in BOTH places, verified here on a collision-style
      bare tick.
Both are extracted by ``ast`` and run on stubs (the source files import rclpy).
"""

from __future__ import annotations

import ast
import textwrap
import time
import types
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_COMMUNICATOR_PY = _REPO / 'physical_ai_server' / 'communication' / 'communicator.py'
_COLLISION_PY = _REPO / 'physical_ai_server' / 'safety' / 'collision_monitor.py'


class _TaskStatus:
    READY = 0

    def __init__(self):
        self.phase = 0
        self.robot_type = ''
        self.robot_profile = ''
        self.capabilities_json = ''


class _BareStatus:
    """A pre-rebuild compiled interface: only the OLD fields exist."""

    def __init__(self):
        self.phase = 0
        self.robot_type = ''      # robot_type predates this change; profile/caps do not


def _load(path, name, ns_extra=None):
    source = path.read_text(encoding='utf-8')
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            src = textwrap.dedent(ast.get_source_segment(source, node))
            ns = {'time': time, 'TaskStatus': _TaskStatus}
            if ns_extra:
                ns.update(ns_extra)
            exec(compile(src, str(path), 'exec'), ns)  # noqa: S102
            return ns[name]
    raise AssertionError(f'{name} not found in {path}')


_publish_status = _load(_COMMUNICATOR_PY, 'publish_status')
_stamp_identity = _load(_COLLISION_PY, '_stamp_identity')


# --- communicator.publish_status -------------------------------------------

class _Publisher:
    def __init__(self):
        self.published = []

    def publish(self, msg):
        self.published.append(msg)


class _CommNode:
    robot_type = 'omx_f'
    robot_profile = 'omx_full'
    capabilities_json = '{"recordable":true}'


class _Comm:
    def __init__(self):
        self.node = _CommNode()
        self.status_publisher = _Publisher()


def _publish(comm, status):
    types.MethodType(_publish_status, comm)(status)


def test_publish_status_stamps_identity_on_bare_tick():
    comm = _Comm()
    _publish(comm, _TaskStatus())
    pub = comm.status_publisher.published[-1]
    assert pub.robot_type == 'omx_f'
    assert pub.robot_profile == 'omx_full'
    assert pub.capabilities_json == '{"recordable":true}'


def test_publish_status_updates_silence_timestamp():
    comm = _Comm()
    _publish(comm, _TaskStatus())
    assert comm.node._last_task_status_mono > 0.0


def test_publish_status_preserves_preset_robot_type():
    comm = _Comm()
    st = _TaskStatus()
    st.robot_type = 'preset'
    _publish(comm, st)
    assert comm.status_publisher.published[-1].robot_type == 'preset'


def test_publish_status_survives_pre_rebuild_interface():
    # No robot_profile / capabilities_json fields -> must not raise, and must not
    # grow the missing fields.
    comm = _Comm()
    st = _BareStatus()
    _publish(comm, st)          # must not raise
    assert st.robot_type == 'omx_f'
    assert not hasattr(st, 'robot_profile')
    assert not hasattr(st, 'capabilities_json')


# --- collision monitor's bare tick (bypasses publish_status) ---------------

class _Host:
    robot_type = 'omx_f'
    robot_profile = 'omx_follower'
    capabilities_json = '{"inferable":true}'


def _stamp(host, status):
    types.MethodType(_stamp_identity, host)(status)


def test_collision_bare_tick_carries_identity():
    st = _TaskStatus()
    _stamp(_Host(), st)
    assert st.robot_type == 'omx_f'
    assert st.robot_profile == 'omx_follower'
    assert st.capabilities_json == '{"inferable":true}'


def test_collision_stamp_skips_missing_fields():
    st = _BareStatus()
    _stamp(_Host(), st)         # must not raise
    assert st.robot_type == 'omx_f'
    assert not hasattr(st, 'robot_profile')
    assert not hasattr(st, 'capabilities_json')


def test_collision_stamp_preserves_preset():
    st = _TaskStatus()
    st.robot_profile = 'preset'
    _stamp(_Host(), st)
    assert st.robot_profile == 'preset'

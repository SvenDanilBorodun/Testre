"""Boot-time robot-profile self-init (T2 / D1).

Extracts ``_init_robot_profile`` by ``ast`` and execs it onto a stub node — the
deps-free pattern used across this suite (physical_ai_server.py imports rclpy and
cannot be imported in CI). Verifies the node comes up with identity + a live
communicator on success, self-heals to the default profile on a bad env value,
and — critically — NEVER raises out of __init__ (a raise = respawn crash-loop),
tearing down a half-built communicator on the degraded path.
"""

from __future__ import annotations

import ast
import os
import textwrap
import types
from pathlib import Path

from physical_ai_server import robot_profiles

_SERVER_PY = (
    Path(__file__).resolve().parents[1] / 'physical_ai_server' / 'physical_ai_server.py'
)


def _load(name):
    source = _SERVER_PY.read_text(encoding='utf-8')
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            src = textwrap.dedent(ast.get_source_segment(source, node))
            ns = {'robot_profiles': robot_profiles, 'os': os}
            exec(compile(src, str(_SERVER_PY), 'exec'), ns)  # noqa: S102
            return ns[name]
    raise AssertionError(f'{name} not found')


_init_robot_profile = _load('_init_robot_profile')


class _Logger:
    def __init__(self):
        self.errors = []

    def error(self, msg):
        self.errors.append(msg)

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


class _FakeComm:
    def __init__(self):
        self.cleaned = False

    def cleanup(self):
        self.cleaned = True


class _Node:
    def __init__(self, init_impl):
        self.communicator = None
        self.params = None
        self._logger = _Logger()
        self._init_impl = init_impl

    def get_logger(self):
        return self._logger

    def init_ros_params(self, robot_type):
        self._init_impl(self, robot_type)


def _run(node):
    types.MethodType(_init_robot_profile, node)()


def _success(node, robot_type):
    node.params = {'camera_topic_list': ['gripper:/x'], 'joint_list': ['leader']}
    node.communicator = _FakeComm()


# --- happy path ------------------------------------------------------------

def test_boot_defaults_to_omx_full_when_env_absent(monkeypatch):
    monkeypatch.delenv('EDUBOTICS_ROBOT_TYPE', raising=False)
    node = _Node(_success)
    _run(node)
    assert node.operation_mode == 'collection'
    assert node.robot_profile == 'omx_full'
    assert node.robot_type == 'omx_f'
    assert node.capabilities_json == \
        robot_profiles.capabilities_json(robot_profiles.resolve('omx_full'))
    assert node._arm_profile.profile_id == 'omx_full'
    assert isinstance(node.communicator, _FakeComm)   # built, NOT torn down
    assert node._logger.errors == []


def test_boot_reads_omx_follower_but_keeps_omx_f_data_type(monkeypatch):
    monkeypatch.setenv('EDUBOTICS_ROBOT_TYPE', 'omx_follower')
    node = _Node(_success)
    _run(node)
    assert node.robot_profile == 'omx_follower'
    assert node.robot_type == 'omx_f'     # dataset repo naming must not shift


def test_boot_garbage_env_defaults_without_crash(monkeypatch):
    monkeypatch.setenv('EDUBOTICS_ROBOT_TYPE', 'not-a-real-type')
    node = _Node(_success)
    _run(node)
    assert node.robot_profile == 'omx_full'
    assert isinstance(node.communicator, _FakeComm)


# --- degraded paths (must never raise) -------------------------------------

def test_boot_degraded_when_init_raises_before_communicator(monkeypatch):
    monkeypatch.delenv('EDUBOTICS_ROBOT_TYPE', raising=False)

    def _raise_before(node, robot_type):
        raise RuntimeError('boom')

    node = _Node(_raise_before)
    _run(node)   # must NOT raise
    assert node.communicator is None
    assert node.robot_type == 'omx_f'     # identity still stamped
    assert node.robot_profile == 'omx_full'
    assert any('[FEHLER]' in e for e in node._logger.errors)


def test_boot_degraded_tears_down_partial_communicator(monkeypatch):
    monkeypatch.delenv('EDUBOTICS_ROBOT_TYPE', raising=False)
    comm = _FakeComm()

    def _raise_after_comm(node, robot_type):
        node.communicator = comm     # half-initialized
        raise RuntimeError('boom after comm')

    node = _Node(_raise_after_comm)
    _run(node)   # must NOT raise
    assert comm.cleaned is True       # torn down
    assert node.communicator is None  # unambiguous degraded state
    assert any('[FEHLER]' in e for e in node._logger.errors)


def test_boot_logs_incomplete_config_but_keeps_running(monkeypatch):
    monkeypatch.delenv('EDUBOTICS_ROBOT_TYPE', raising=False)

    def _empty_params(node, robot_type):
        node.params = {'camera_topic_list': [], 'joint_list': []}
        node.communicator = _FakeComm()

    node = _Node(_empty_params)
    _run(node)
    assert any('unvollständig' in e for e in node._logger.errors)
    assert isinstance(node.communicator, _FakeComm)   # degraded, not torn down


def test_boot_logs_incomplete_config_for_declared_default(monkeypatch):
    """The value production ACTUALLY produces on a missing config YAML:
    the params are declared with default_value=[''] and load_parameters
    returns that — a NON-empty list of one blank string. The naive
    truthiness check silently passed it; the blank-aware check must not."""
    monkeypatch.delenv('EDUBOTICS_ROBOT_TYPE', raising=False)

    def _default_params(node, robot_type):
        node.params = {'camera_topic_list': [''], 'joint_list': ['']}
        node.communicator = _FakeComm()

    node = _Node(_default_params)
    _run(node)
    assert any('unvollständig' in e for e in node._logger.errors)
    assert isinstance(node.communicator, _FakeComm)   # degraded, not torn down


def test_boot_degraded_cancels_armed_stale_notice_timer(monkeypatch):
    """A raise AFTER init_ros_params armed the stale-session poll timer must
    cancel it in the teardown (symmetry with the communicator teardown — a
    leaked 5 s timer keeps polling a degraded node forever)."""
    monkeypatch.delenv('EDUBOTICS_ROBOT_TYPE', raising=False)

    class _FakeTimer:
        def __init__(self):
            self.canceled = False

        def cancel(self):
            self.canceled = True

    timer = _FakeTimer()

    def _raise_after_timer(node, robot_type):
        node._stale_notice_timer = timer
        node.communicator = _FakeComm()
        raise RuntimeError('boom after timer armed')

    node = _Node(_raise_after_timer)
    _run(node)   # must NOT raise
    assert timer.canceled is True
    assert node._stale_notice_timer is None
    assert node.communicator is None

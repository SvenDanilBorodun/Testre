"""D15 and D16 (plan v6.13 §0, owner sign-offs 2026-11-01), on the shipped code.

D15: ``_set_follower_torque(False)`` that is not confirmed (unavailable, timed
out, refused, an exception) turns ``_follower_torque_on`` from True into None;
a False stays False; a confirmed torque-off sets False; the return value is
unchanged. The idle watchdog then re-torques where a stale True let it skip.

D16: ``workshop_hand_guide_callback`` (on) and ``workshop_record_callback``
(``start``) re-check ``_manual_persistent`` under ``_mode_lock`` after acquiring
``_manual_lock``; if „Arm festsetzen" ended the claim meanwhile they refuse with
D16's sentence, restore nothing and switch no torque.

The methods are extracted by ``ast`` (physical_ai_server.py imports rclpy) and
exec'd onto a fake node, the pattern of test_workshop_manual_callbacks.py.
"""

from __future__ import annotations

import ast
import json
import math
import sys
import textwrap
import threading
import time
import types
from pathlib import Path

import pytest

_SERVER_PY = (Path(__file__).resolve().parents[1]
              / 'physical_ai_server' / 'physical_ai_server.py')
_D16_DE = 'Der Arm wurde gerade festgesetzt – bitte noch einmal versuchen.'


def _load(names, extra):
    source = _SERVER_PY.read_text(encoding='utf-8')
    ns = dict(extra)
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            src = textwrap.dedent(ast.get_source_segment(source, node))
            exec(compile(src, str(_SERVER_PY), 'exec'), ns)  # noqa: S102
    return {n: ns[n] for n in names}


class _Log:
    def warning(self, *a, **k):
        pass

    info = error = warning


# ── D15 ───────────────────────────────────────────────────────────────────────
class _Future:
    def __init__(self, result, done=True, raise_on_result=False):
        self._result = result
        self._done = done
        self._raise = raise_on_result

    def done(self):
        return self._done

    def result(self):
        if self._raise:
            raise RuntimeError('boom after sending')
        return self._result


class _Client:
    def __init__(self, mode):
        self.mode = mode
        self.calls = []

    def wait_for_service(self, timeout_sec=None):
        return self.mode != 'unavailable'

    def call_async(self, req):
        self.calls.append(req.data)
        if self.mode == 'timeout':
            return _Future(None, done=False)
        if self.mode == 'refused':
            return _Future(types.SimpleNamespace(success=False, message='nein'))
        if self.mode == 'exception':
            return _Future(None, raise_on_result=True)
        return _Future(types.SimpleNamespace(success=True, message=''))


@pytest.fixture
def setbool(monkeypatch):
    class SetBool:
        class Request:
            data = False
    srv = types.ModuleType('std_srvs.srv')
    srv.SetBool = SetBool
    pkg = types.ModuleType('std_srvs')
    pkg.srv = srv
    monkeypatch.setitem(sys.modules, 'std_srvs', pkg)
    monkeypatch.setitem(sys.modules, 'std_srvs.srv', srv)


def _torque_node(mode, flag):
    clock = [0.0]

    def monotonic():
        clock[0] += 1.0       # a timed-out call ends at once
        return clock[0]

    fake_time = types.SimpleNamespace(monotonic=monotonic, sleep=lambda s: None)
    fn = _load(['_set_follower_torque'], {'time': fake_time, 'math': math})[
        '_set_follower_torque']
    node = types.SimpleNamespace()
    node._arm_profile = None
    node._dxl_torque_lock = threading.Lock()
    node._dxl_torque_client = _Client(mode)
    node._follower_torque_on = flag
    node.get_logger = lambda: _Log()
    node._follower_rail_is_ros2_control = lambda: True
    node._hold_follower_at_measured_pose = lambda allow_stale=False: True
    node.note_collision_resettle = lambda: None
    node.on_leader_teach = False
    return node, fn


@pytest.mark.parametrize('mode', ['unavailable', 'timeout', 'refused', 'exception'])
def test_an_unconfirmed_torque_off_turns_true_into_none(setbool, mode):
    node, fn = _torque_node(mode, True)
    assert fn(node, False) is False
    assert node._follower_torque_on is None


@pytest.mark.parametrize('mode', ['unavailable', 'timeout', 'refused', 'exception'])
def test_an_unconfirmed_torque_off_leaves_false_and_none_alone(setbool, mode):
    for flag in (False, None):
        node, fn = _torque_node(mode, flag)
        assert fn(node, False) is False
        assert node._follower_torque_on is flag


def test_a_confirmed_torque_off_sets_false(setbool):
    node, fn = _torque_node('ok', True)
    assert fn(node, False) is True
    assert node._follower_torque_on is False


@pytest.mark.parametrize('mode', ['unavailable', 'timeout', 'refused', 'exception'])
def test_an_unconfirmed_torque_on_changes_nothing(setbool, mode):
    for flag in (True, False, None):
        node, fn = _torque_node(mode, flag)
        assert fn(node, True) is False
        assert node._follower_torque_on is flag


def test_the_idle_watchdog_retorques_where_a_stale_true_let_it_skip():
    consts = dict(_MANUAL_IDLE_RETORQUE_S=30.0)
    fn = _load(['_manual_idle_watchdog_cb'], {'time': time, **consts})[
        '_manual_idle_watchdog_cb']
    calls = []
    node = types.SimpleNamespace(
        on_manual=True, _manual_record_active=False, _manual_replay_thread=None,
        _manual_last_activity_mono=0.0, _manual_lock=threading.Lock(),
        _mode_lock=threading.Lock(), _manual_persistent=True,
        _manual_transient_ops=0, _manual_torque_fail_count=0,
        _follower_torque_on=None,          # what D15 leaves after a timed-out off
        get_logger=lambda: _Log())
    node._set_follower_torque = lambda enabled: (calls.append(enabled), False)[1]
    node._recompute_on_manual_locked = lambda: setattr(
        node, 'on_manual', node._manual_persistent or node._manual_transient_ops > 0)
    fn(node)
    assert calls == [True]              # re-torque attempted
    assert node.on_manual is True       # kept when it fails


# ── D16 ───────────────────────────────────────────────────────────────────────
_CONSTS = dict(_MANUAL_LOCK_TIMEOUT_S=2.0, _MANUAL_RECORD_FPS=25,
               _MANUAL_IDLE_RETORQUE_S=30.0)
_CB = _load(['workshop_hand_guide_callback', 'workshop_record_callback',
             '_last_torque_reason'],
            {'time': time, 'json': json, 'threading': threading, 'math': math,
             **_CONSTS})


class _RacingLock:
    """A _manual_lock whose acquire runs „Arm festsetzen" first (the race)."""

    def __init__(self, on_acquire):
        self._lock = threading.Lock()
        self._on_acquire = on_acquire

    def acquire(self, timeout=-1, blocking=True):
        if self._on_acquire is not None:
            self._on_acquire()
        return self._lock.acquire()

    def release(self):
        self._lock.release()


def _node(race):
    node = types.SimpleNamespace()
    node._mode_lock = threading.Lock()
    node._manual_persistent = False
    node._manual_transient_ops = 0
    node.on_manual = False
    node._manual_last_activity_mono = 0.0
    node._manual_record_active = False
    node._handguide_buffer = []
    node._manual_record_cap_reached = False
    node._manual_record_timer = None
    node.communicator = object()
    node.torque_calls = []
    node._follower_torque_last_message = ''

    def end_session():
        with node._mode_lock:
            node._manual_persistent = False
            node._recompute_on_manual_locked()

    node._manual_lock = _RacingLock(end_session if race else None)
    node._assert_no_other_active = lambda mode: (True, '')
    node._manual_leader_blocks = lambda: (False, '')
    node._recompute_on_manual_locked = lambda: setattr(
        node, 'on_manual', node._manual_persistent or node._manual_transient_ops > 0)
    node._set_follower_torque = lambda enabled: (node.torque_calls.append(enabled), True)[1]
    node._last_torque_reason = lambda: _CB['_last_torque_reason'](node)
    node._manual_record_sample = lambda: None
    node.create_timer = lambda period, cb: object()
    node.get_logger = lambda: _Log()
    return node


def _resp():
    return types.SimpleNamespace(success=None, message=None, sample_count=None,
                                 duration_s=None, points_json=None)


def test_hand_guide_on_whose_claim_was_ended_refuses_and_switches_nothing():
    node = _node(race=True)
    resp = _CB['workshop_hand_guide_callback'](node, types.SimpleNamespace(enabled=True), _resp())
    assert resp.success is False and resp.message == _D16_DE
    assert node.torque_calls == []
    assert node._manual_persistent is False and node.on_manual is False


def test_hand_guide_on_with_its_claim_held_frees_the_arm_as_before():
    node = _node(race=False)
    resp = _CB['workshop_hand_guide_callback'](node, types.SimpleNamespace(enabled=True), _resp())
    assert resp.success is True and node.torque_calls == [False]
    assert node._manual_persistent is True


def test_record_start_whose_claim_was_ended_refuses_and_switches_nothing():
    node = _node(race=True)
    resp = _CB['workshop_record_callback'](node, types.SimpleNamespace(action='start'), _resp())
    assert resp.success is False and resp.message == _D16_DE
    assert node.torque_calls == [] and node._manual_record_active is False
    assert node._manual_persistent is False


def test_record_start_with_its_claim_held_starts_as_before():
    node = _node(race=False)
    resp = _CB['workshop_record_callback'](node, types.SimpleNamespace(action='start'), _resp())
    assert resp.success is True and node.torque_calls == [False]
    assert node._manual_record_active is True


def test_the_d16_recheck_sits_after_manual_lock_and_before_the_torque_off():
    source = _SERVER_PY.read_text(encoding='utf-8')
    for name in ('workshop_hand_guide_callback', 'workshop_record_callback'):
        fn = next(n for n in ast.walk(ast.parse(source))
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        seg = ast.get_source_segment(source, fn)
        i_acq = seg.index('self._manual_lock.acquire(timeout=_MANUAL_LOCK_TIMEOUT_S)')
        i_chk = seg.index('claim_held = self._manual_persistent', i_acq)
        i_off = seg.index('self._set_follower_torque(False)', i_acq)
        assert i_acq < i_chk < i_off, name

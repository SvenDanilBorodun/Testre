"""Roboter Studio Phase-5 block tests — toast / counters / wait_until_held.

Deps-free contract tests (no container, no ROS hardware): drive the three new
block families directly with a minimal stub ctx, mirroring test_phase2_blocks.py
and test_interpreter.py.

  - ``edubotics_toast``            output sentinel ``[TOAST:level:sec:text]``.
  - ``edubotics_counter_*``        reset / add (statement) + get (value) + the
                                   ``edubotics_when_counter_gt`` hat.
  - ``edubotics_wait_until_held``  perception Boolean with the no-silent-False
                                   contract (raises on missing joint readback).
"""

from __future__ import annotations

import json
import threading

import pytest

from physical_ai_server.workflow.handlers import (
    STATEMENT_HANDLERS,
    VALUE_EVALUATORS,
)
from physical_ai_server.workflow.handlers import counters as counter_handlers
from physical_ai_server.workflow.handlers import motion as motion_handlers
from physical_ai_server.workflow.handlers import output as output_handlers
from physical_ai_server.workflow.handlers import perception_blocks as pb
from physical_ai_server.workflow.handlers.motion import WorkflowError
from physical_ai_server.workflow.interpreter import HAT_BLOCK_TYPES, Interpreter


class _Ctx:
    """Minimal stub: variables + counters + a real var_lock + collecting log."""

    def __init__(self):
        self.variables: dict = {}
        self.counters: dict = {}
        self.logs: list[str] = []
        self.var_lock = threading.RLock()
        self._stop = False
        self.should_stop = lambda: self._stop

    def log(self, m):
        self.logs.append(m)


def _ws(blocks_list):
    return json.dumps({'blocks': {'languageVersion': 0, 'blocks': blocks_list}})


# ── toast (output sentinel) ───────────────────────────────────────────────────
def test_toast_emits_sentinel_with_clamped_seconds_and_level():
    ctx = _Ctx()
    output_handlers.toast(ctx, {'text': 'Hallo', 'level': 'success', 'seconds': 99})
    assert ctx.logs == ['[TOAST:success:15:Hallo]']  # seconds clamped to 15


def test_toast_unknown_level_falls_back_to_info_and_clamps_min():
    ctx = _Ctx()
    output_handlers.toast(ctx, {'text': 'x', 'level': 'lila', 'seconds': 0})
    assert ctx.logs == ['[TOAST:info:1:x]']  # unknown→info, seconds clamped to 1


def test_toast_strips_brackets_and_newlines():
    ctx = _Ctx()
    output_handlers.toast(ctx, {'text': 'a[SOUND]\nb', 'level': 'warning', 'seconds': 3})
    assert ctx.logs == ['[TOAST:warning:3:a(SOUND) b]']


def test_toast_caps_long_text():
    ctx = _Ctx()
    output_handlers.toast(ctx, {'text': 'q' * 5000, 'level': 'error', 'seconds': 2})
    body = ctx.logs[0]
    assert body.startswith('[TOAST:error:2:')
    assert body.count('q') == output_handlers.MAX_TOAST_CHARS  # 240


def test_toast_missing_text_emits_empty():
    ctx = _Ctx()
    output_handlers.toast(ctx, {'level': 'info', 'seconds': 4})
    assert ctx.logs == ['[TOAST:info:4:]']


def test_toast_registered_as_statement():
    assert STATEMENT_HANDLERS.get('edubotics_toast') is output_handlers.toast


# ── counters (reset / add / get) ──────────────────────────────────────────────
def test_counter_add_and_get_roundtrip():
    ctx = _Ctx()
    counter_handlers.add(ctx, {'name': 'Punkte'})
    counter_handlers.add(ctx, {'name': 'Punkte'})
    assert counter_handlers.get(ctx, {'name': 'Punkte'}) == 2
    assert counter_handlers.get_count(ctx, 'Punkte') == 2


def test_counter_reset_zeros():
    ctx = _Ctx()
    counter_handlers.add(ctx, {'name': 'a'})
    counter_handlers.reset(ctx, {'name': 'a'})
    assert counter_handlers.get_count(ctx, 'a') == 0


def test_counter_get_unset_is_zero():
    assert counter_handlers.get(_Ctx(), {'name': 'nope'}) == 0


def test_counter_name_is_trimmed():
    ctx = _Ctx()
    counter_handlers.add(ctx, {'name': '  Punkte  '})
    assert counter_handlers.get_count(ctx, 'Punkte') == 1


def test_counter_empty_name_raises():
    with pytest.raises(WorkflowError, match='Zähler'):
        counter_handlers.add(_Ctx(), {'name': '   '})
    with pytest.raises(WorkflowError, match='Zähler'):
        counter_handlers.reset(_Ctx(), {'name': ''})


def test_counter_lazy_store_on_ctx_without_field():
    # A unit-test ctx that doesn't pre-declare .counters still works.
    class _Bare:
        def __init__(self):
            self.var_lock = None
    ctx = _Bare()
    counter_handlers.add(ctx, {'name': 'k'})
    assert counter_handlers.get_count(ctx, 'k') == 1


def test_counters_registered():
    assert STATEMENT_HANDLERS.get('edubotics_counter_reset') is counter_handlers.reset
    assert STATEMENT_HANDLERS.get('edubotics_counter_add') is counter_handlers.add
    assert VALUE_EVALUATORS.get('edubotics_counter_get') is counter_handlers.get


def test_counter_blocks_dispatch_via_execute():
    # reset → add → add → latch counter_get into a variable, all via the real
    # dispatch tables (proves the btype ladder routes them).
    payload = _ws([
        {'type': 'edubotics_counter_reset', 'fields': {'NAME': 'k'},
         'next': {'block': {
             'type': 'edubotics_counter_add', 'fields': {'NAME': 'k'},
             'next': {'block': {
                 'type': 'edubotics_counter_add', 'fields': {'NAME': 'k'},
                 'next': {'block': {
                     'type': 'variables_set',
                     'fields': {'VAR': {'name': 'out'}},
                     'inputs': {'VALUE': {'block': {
                         'type': 'edubotics_counter_get', 'fields': {'NAME': 'k'},
                     }}},
                 }},
             }},
         }}},
    ])
    ctx = _Ctx()
    Interpreter.from_json(payload).execute(ctx, lambda *a: None)
    assert ctx.counters['k'] == 2
    assert ctx.variables['out'] == 2


# ── counter hat (edubotics_when_counter_gt) ───────────────────────────────────
def test_counter_hat_in_hat_block_types():
    assert 'edubotics_when_counter_gt' in HAT_BLOCK_TYPES


def test_counter_hat_split_into_hats():
    interp = Interpreter([
        {'type': 'edubotics_when_counter_gt', 'fields': {'NAME': 'k', 'N': 3}},
        {'type': 'edubotics_home'},
    ])
    main, hats = interp.split_roots()
    assert [h['type'] for h in hats] == ['edubotics_when_counter_gt']
    assert [m['type'] for m in main] == ['edubotics_home']


def test_wait_counter_gt_threshold_crossing(monkeypatch):
    from physical_ai_server.workflow import workflow_manager as wm_mod
    monkeypatch.setattr(wm_mod.time, 'sleep', lambda *_a, **_k: None)
    mgr = wm_mod.WorkflowManager(publisher=lambda _pts: None)
    ctx = _Ctx()
    fields = {'NAME': 'k', 'N': 3}
    ctx.counters['k'] = 3            # == threshold → not greater → False
    assert mgr._wait_counter_gt(fields, ctx) is False
    ctx.counters['k'] = 4            # > threshold → True
    assert mgr._wait_counter_gt(fields, ctx) is True


def test_wait_counter_gt_empty_name_is_false(monkeypatch):
    from physical_ai_server.workflow import workflow_manager as wm_mod
    monkeypatch.setattr(wm_mod.time, 'sleep', lambda *_a, **_k: None)
    mgr = wm_mod.WorkflowManager(publisher=lambda _pts: None)
    assert mgr._wait_counter_gt({'NAME': '', 'N': 1}, _Ctx()) is False


# ── wait_until_held (perception, no-silent-False) ─────────────────────────────
def test_wait_until_held_raises_when_no_joint_readback(monkeypatch):
    monkeypatch.setattr(motion_handlers, 'GRASP_SETTLE_S', 0.0)
    ctx = _Ctx()
    ctx.get_follower_joints = None  # → check_grasp_held returns None
    with pytest.raises(WorkflowError, match='Gelenkdaten'):
        pb.wait_until_held(ctx, {'timeout': 1})


def test_wait_until_held_true_when_object_held(monkeypatch):
    monkeypatch.setattr(motion_handlers, 'GRASP_SETTLE_S', 0.0)
    ctx = _Ctx()
    # gripper angle 0.0 > GRASP_HELD_MAX_RAD (-0.35) → held.
    ctx.get_follower_joints = lambda: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    assert pb.wait_until_held(ctx, {'timeout': 1}) is True


def test_wait_until_held_registered():
    assert VALUE_EVALUATORS.get('edubotics_wait_until_held') is pb.wait_until_held

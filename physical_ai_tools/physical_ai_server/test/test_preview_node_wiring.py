#!/usr/bin/env python3
"""A simulator run (every Sammlung preview is one) never reaches a real-arm writer.

Nothing fenced this before: a copy of the node with ``publisher=self._trajectory_publisher``
in ``_get_or_create_sim_workflow_manager`` plus a ``self._set_follower_torque(True)`` in
the sim branch of ``_launch_workflow`` left every node-reading test green. The fence
has two halves. The AST half catches the un-wiring cheaply; the behavioural half
extracts the REAL ``workflow_start_callback`` + ``_launch_workflow`` +
``_get_or_create_sim_workflow_manager`` onto a fake node with a LIVE leader (the sim
branch bypasses the leader gate) and spies on the real publisher, the torque call and
the real manager — all three must stay at zero calls.

Also here: the IK pre-check on STORED destinations (S2) — a ``destination_ref`` that
no pin/current statement sets is checked against the run's merged destinations.
``physical_ai_server.py`` cannot be imported without rclpy, hence ``ast`` + ``exec``.
"""

from __future__ import annotations

import ast
import json
import math
import pathlib
import threading
import time
import types

import pytest

from physical_ai_server.workflow import trajectory_builder
from physical_ai_server.workflow.ik_solver import IKSolver
from physical_ai_server.workflow.interpreter import Interpreter
from physical_ai_server.workflow.workflow_manager import WorkflowManager


_NODE = (pathlib.Path(__file__).resolve().parents[1]
         / 'physical_ai_server' / 'physical_ai_server.py')
_TREE = ast.parse(_NODE.read_text(encoding='utf-8'))


@pytest.fixture(autouse=True)
def _fast_chunk_pacing(monkeypatch):
    state = {'t': 0.0}

    def _monotonic():
        state['t'] += 1000.0
        return state['t']

    monkeypatch.setattr(trajectory_builder, 'time',
                        types.SimpleNamespace(monotonic=_monotonic,
                                              sleep=lambda _s: None))
    yield


def _method(name):
    hits = [n for n in ast.walk(_TREE)
            if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(hits) == 1, f'expected exactly one {name}, found {len(hits)}'
    return hits[0]


# ── AST half ──────────────────────────────────────────────────────────────────

def test_the_sim_manager_publishes_through_the_sim_arm_only():
    fn = _method('_get_or_create_sim_workflow_manager')
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == 'WorkflowManager']
    assert len(calls) == 1, f'expected one WorkflowManager(...), found {len(calls)}'
    publisher = [k for k in calls[0].keywords if k.arg == 'publisher']
    assert len(publisher) == 1
    assert ast.unparse(publisher[0].value) == 'sim_arm.publish'


_REAL_WRITERS = ('_set_follower_torque', 'torque_on(',
                 '_get_or_create_workflow_manager', '_trajectory_publisher')


def test_the_sim_branch_names_no_real_arm_writer():
    fn = _method('_launch_workflow')
    branches = [n for n in ast.walk(fn) if isinstance(n, ast.If)
                and isinstance(n.test, ast.Name) and n.test.id == 'sim_enabled']
    assert len(branches) == 1, 'the `if sim_enabled:` branch moved or was renamed'
    offending = []
    for stmt in branches[0].body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                text = ast.unparse(node)
                offending += [(w, text) for w in _REAL_WRITERS if w in text]
    assert offending == [], offending


# ── behaviour half ────────────────────────────────────────────────────────────

_EXTRACTED = ('workflow_start_callback', '_launch_workflow',
              '_get_or_create_sim_workflow_manager')


def _real_methods():
    module = ast.Module(body=[_method(name) for name in _EXTRACTED], type_ignores=[])
    namespace = {'json': json, 'math': math, 'time': time, 'threading': threading}
    exec(compile(module, str(_NODE), 'exec'), namespace)
    return {name: namespace[name] for name in _EXTRACTED}


class _Spy:
    def __init__(self, result=None):
        self.calls = []
        self._result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self._result


def _fake_node():
    logger = types.SimpleNamespace(error=lambda *_a, **_k: None,
                                   warning=lambda *_a, **_k: None,
                                   info=lambda *_a, **_k: None)
    frames = []
    finished = []
    node = types.SimpleNamespace(
        _mode_lock=threading.Lock(),
        on_workflow=False,
        sim_workflow_manager=None,
        workflow_manager=None,
        _sim_world=None,
        _sim_arm=None,
        _sim_objects=[],
        _assert_no_other_active=lambda _mode: (True, ''),
        _active_workflow_manager=lambda: None,
        leader_appears_active=lambda: True,           # a LIVE leader
        _trajectory_publisher=_Spy(),
        _set_follower_torque=_Spy(result=True),
        _get_or_create_workflow_manager=_Spy(result=None),
        _warn_sim_objects_beyond_tag_capacity=lambda _objs: None,
        _publish_sim_objects=lambda **_k: None,
        _ensure_sim_publisher=lambda: None,
        _publish_sim_frame=lambda q, scene=None, late_s=0.0: frames.append(q),
        _build_ik_solver=IKSolver,
        _profile_n=lambda: 5,
        _arm_profile=None,
        _load_object_catalog_tolerant=lambda: None,
        _load_object_catalog=None,
        _emit_workflow_status=lambda _payload: None,
        _on_sim_workflow_finished=finished.append,
        _last_torque_reason=lambda: '',
        get_logger=lambda: logger,
    )
    for name, fn in _real_methods().items():
        setattr(node, name, types.MethodType(fn, node))
    return node, frames, finished


def _recording_preview(seconds):
    return {
        'blocks': {'languageVersion': 0, 'blocks': [{
            'type': 'edubotics_replay_trajectory', 'id': 'vorschau-1',
            'fields': {'NAME': 'Greifen links'},
            'next': {'block': {'type': 'edubotics_wait_seconds', 'id': 'vorschau-2',
                               'inputs': {'SECONDS': {'shadow': {
                                   'type': 'math_number', 'fields': {'NUM': seconds}}}}}},
        }]},
        'sim': {'enabled': True, 'objects': []},
        'zones': [],
        'tempo': 1.0,
        'trajectories': {'Greifen links': {'fps': 25, 'points': [
            [0.0, -1.5708, 1.5708, 0.0, 0.0, 0.8, 0.0],
            [0.2, -1.5708, 1.5708, 0.0, 0.0, 0.8, 1.0],
        ]}},
    }


def test_a_sim_preview_with_a_live_leader_never_touches_the_real_arm():
    node, frames, finished = _fake_node()
    request = types.SimpleNamespace(
        workflow_json=json.dumps(_recording_preview(0.1)),
        workflow_id='vorschau-aufnahme-1234abcd')
    response = node.workflow_start_callback(request, types.SimpleNamespace())
    try:
        assert response.success is True, response.message
        deadline = time.monotonic() + 20.0
        while not finished and time.monotonic() < deadline:
            time.sleep(0.01)
        assert finished == ['finished']
        # The real-arm spies first, so a regression names the writer it reached.
        assert node._trajectory_publisher.calls == []
        assert node._set_follower_torque.calls == []
        assert node._get_or_create_workflow_manager.calls == []
        while not frames and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(frames) > 0, 'the sim arm played nothing'
    finally:
        if node._sim_arm is not None:
            node._sim_arm.set_objects([])   # cancels the real-time player thread


# ── S2: the IK pre-check on stored destinations ───────────────────────────────

def _ref(name):
    return {'block': {'type': 'edubotics_destination_ref', 'id': 'ref',
                      'fields': {'NAME': name}}}


def _program(*head, consumer_disabled=False):
    """[head statements...] → „bewege zu <Ablage>" (block id ``mv``)."""
    move = {'type': 'edubotics_move_to', 'id': 'mv',
            'inputs': {'DESTINATION': _ref('Ablage')}}
    if consumer_disabled:
        move['disabledReasons'] = ['manual']
    chain = move
    for stmt in reversed(head):
        chain = dict(stmt, next={'block': chain})
    return {'blocks': {'languageVersion': 0, 'blocks': [chain]}}


_PIN = {'type': 'edubotics_destination_pin', 'id': 'pin',
        'fields': {'NAME': 'Ablage', 'X': '0.2', 'Y': '0', 'Z': '0.05'}}
_CURRENT = {'type': 'edubotics_destination_current', 'id': 'cur',
            'fields': {'NAME': 'Ablage'}}
_EXTRA = {'Ablage': (5.0, 0.0, 0.0)}


def _collect(program, extra=None):
    interp = Interpreter.from_json(json.dumps(program))
    if extra is None:
        return interp.collect_concrete_destinations()
    return interp.collect_concrete_destinations(extra_destinations=extra)


def test_a_ref_no_statement_sets_resolves_against_the_stored_destinations():
    assert _collect(_program(), _EXTRA) == [
        {'block_id': 'mv', 'block_type': 'edubotics_move_to', 'xyz': (5.0, 0.0, 0.0)}]


def test_a_pin_statement_beats_a_stored_destination():
    found = _collect(_program(_PIN), _EXTRA)
    assert [t['xyz'] for t in found] == [(0.2, 0.0, 0.05)]


def test_a_current_statement_makes_the_name_unknowable_before_the_run():
    assert _collect(_program(_CURRENT), _EXTRA) == []


def test_a_disabled_consumer_is_never_checked():
    assert _collect(_program(consumer_disabled=True), _EXTRA) == []


def test_without_extra_destinations_nothing_changes():
    assert _collect(_program()) == []
    assert [t['xyz'] for t in _collect(_program(_PIN))] == [(0.2, 0.0, 0.05)]
    assert _collect(_program(_CURRENT)) == []


def _start_with_payload_destination(x):
    status = []
    mgr = WorkflowManager(
        publisher=lambda *_a, **_k: None,
        ik_factory=lambda: IKSolver(),
        load_destinations=lambda: {},
        load_calibration=lambda: {'z_table': 0.0},
        emit_status=status.append,
        on_finished=lambda phase: status.append({'_finished': phase}),
    )
    mgr._run = lambda interpreter, ctx: None     # the pre-check is all we look at
    program = _program()
    program['destinations'] = [{'name': 'Ablage', 'kind': 'pin', 'x': x, 'y': 0.0, 'z': 0.05}]
    ok, msg, unreachable = mgr.start(json.dumps(program), 'wf')
    assert ok, msg
    return unreachable


def test_start_flags_an_unreachable_stored_destination():
    assert _start_with_payload_destination(5.0) == [
        {'block_id': 'mv', 'message': 'Diese Position ist außerhalb des Arbeitsbereichs.'}]


def test_start_does_not_flag_a_reachable_stored_destination():
    assert _start_with_payload_destination(0.2) == []


_PLANE_CALIB = {'z_table': 0.0, 'table_plane': (0.0, 0.0, 0.02)}


def test_precheck_points_re_ask_the_plane_for_a_pin_only():
    points = WorkflowManager._precheck_destination_points({
        'pin': {'x': 0.2, 'y': 0.0, 'z': 0.05, 'plane_tracked': True},
        'pose': {'x': 0.2, 'y': 0.0, 'z': 0.05, 'plane_tracked': False},
        'nan': {'x': float('nan'), 'y': 0.0, 'z': 0.05, 'plane_tracked': False},
        'broken': {'x': 0.2},
    }, _PLANE_CALIB)
    assert points == {'pin': (0.2, 0.0, pytest.approx(0.02)), 'pose': (0.2, 0.0, 0.05)}


def test_precheck_points_without_calibration_keep_the_stored_z():
    entry = {'x': 0.2, 'y': 0.0, 'z': 0.05, 'plane_tracked': True}
    assert WorkflowManager._precheck_destination_points({'pin': entry}, None) == {
        'pin': (0.2, 0.0, 0.05)}
    assert WorkflowManager._precheck_destination_points(None, None) == {}

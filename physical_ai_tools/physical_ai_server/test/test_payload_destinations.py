#!/usr/bin/env python3
"""The run payload's ``destinations`` sibling (the student's Sammlung document).

``WorkflowManager._parse_destinations`` turns ``[{name, kind, x, y, z}, ...]`` into
``ctx.destinations`` entries, deciding ``plane_tracked`` from a CLOSED kind enum in
code. A payload that carries the sibling is AUTHORITATIVE: robot-local
``_persisted_destinations`` are consulted only for an older client that sends no
sibling at all. Also pins the OverflowError guard on every /workflow/start
sibling parser — ``json.loads`` accepts a 401-digit integer that ``float()``
refuses with OverflowError, which none of the parsers used to catch.
"""

from __future__ import annotations

import json
import time
import types

import numpy as np
import pytest

from physical_ai_server.workflow import path_guard, trajectory_builder
from physical_ai_server.workflow.handlers.destinations import destination_name_error_de
from physical_ai_server.workflow.ik_solver import IKSolver
from physical_ai_server.workflow.object_catalog import fixed_catalog
from physical_ai_server.workflow.sim_arm import SimArm
from physical_ai_server.workflow.sim_perception import SimPerception
from physical_ai_server.workflow.sim_world import SimWorld
from physical_ai_server.workflow.workflow_manager import (
    MAX_DESTINATION_SKIP_REASONS,
    MAX_PAYLOAD_DESTINATIONS,
    WorkflowManager,
)


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


def _sim_manager(objects, status, calib):
    world = SimWorld(objects)
    arm = SimArm(ik=IKSolver(), objects=objects, world=world)
    mgr = WorkflowManager(
        publisher=arm.publish,
        ik_factory=lambda: IKSolver(),
        perception_factory=lambda: SimPerception(objects, fixed_catalog(), world),
        load_destinations=lambda: {},
        load_calibration=lambda: dict(calib),
        emit_status=status.append,
        on_finished=lambda phase: status.append({'_finished': phase}),
        get_scene_frame=lambda: np.zeros((1, 1, 3), dtype=np.uint8),
        get_scene_frame_age=lambda: 0.0,
        get_current_pose_xyz=lambda: arm.fk_xyz(),
        get_follower_joints=arm.get_joints,
        load_object_catalog=fixed_catalog,
    )
    return mgr, arm, world


def _run(mgr, program, status, wid='wf'):
    ok, msg, _ = mgr.start(json.dumps(program), wid)
    assert ok, msg
    return _wait_finished(status)


def _wait_finished(status):
    deadline = time.monotonic() + 20.0
    done = lambda: [e for e in status if isinstance(e, dict) and '_finished' in e]
    while not done() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert done(), 'workflow did not finish'
    return done()[-1]['_finished']


def _errors(status):
    return [e.get('error', '') for e in status
            if isinstance(e, dict) and e.get('phase') == 'error']


def _warnings(status):
    return [e['log_message'] for e in status
            if isinstance(e, dict) and str(e.get('log_message', '')).startswith('[WARNUNG]')]


def _move_to_ref(name='P', block_id='mv'):
    return {'blocks': {'languageVersion': 0, 'blocks': [{
        'type': 'edubotics_move_to', 'id': block_id,
        'inputs': {'DESTINATION': {'block': {
            'type': 'edubotics_destination_ref', 'fields': {'NAME': name}}}},
    }]}}


def _parse(payload):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return WorkflowManager._parse_destinations(text)


def _entry(name, kind='pin', x=0.2, y=0.0, z=0.05):
    return {'name': name, 'kind': kind, 'x': x, 'y': y, 'z': z}


_BIG = '9' * 401   # a JSON integer float() refuses with OverflowError


# ── parse ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('text', [
    'not json', '[1, 2]', '"x"', json.dumps({'blocks': {}}),
    json.dumps({'destinations': {'A': 1}}), json.dumps({'destinations': None}),
])
def test_no_sibling_list_is_not_a_document(text):
    assert WorkflowManager._parse_destinations(text) == ({}, [], False)


def test_a_list_is_a_document_even_when_empty():
    assert _parse({'destinations': []}) == ({}, [], True)


def test_the_kind_decides_plane_tracked_in_code():
    out, skipped, present = _parse({'destinations': [
        _entry('A', 'pin'), _entry('B', 'pose')]})
    assert present is True and skipped == []
    assert out['A']['plane_tracked'] is True
    assert out['B']['plane_tracked'] is False


def test_stored_entry_shape_is_exactly_the_runtime_shape():
    out, _, _ = _parse({'destinations': [_entry('A', x=1, y=0, z=0)]})
    assert set(out['A']) == {'x', 'y', 'z', 'label', 'plane_tracked'}
    assert all(type(out['A'][k]) is float for k in ('x', 'y', 'z'))
    assert out['A']['label'] == 'A'


@pytest.mark.parametrize('kind', [None, 'cam', 'PIN', 1, True])
def test_an_unknown_or_missing_kind_is_skipped_never_defaulted(kind):
    item = _entry('C')
    if kind is None:
        item.pop('kind')
    else:
        item['kind'] = kind
    out, skipped, present = _parse({'destinations': [item]})
    assert out == {} and present is True
    assert skipped == ['„C" hat eine unbekannte Art.']


def test_a_bad_name_is_skipped_with_the_shared_name_sentence():
    out, skipped, _ = _parse({'destinations': [_entry('A!')]})
    assert out == {}
    assert skipped == [destination_name_error_de('A!')]


def test_the_name_is_stripped():
    out, skipped, _ = _parse({'destinations': [_entry(' E ')]})
    assert list(out) == ['E'] and skipped == []
    assert out['E']['label'] == 'E'


@pytest.mark.parametrize('name,field,value', [
    ('F', 'x', 'NaN'), ('G', 'x', True), ('H', 'x', '1'), ('F', 'z', None),
])
def test_a_non_number_coordinate_is_skipped(name, field, value):
    item = _entry(name)
    item[field] = value
    out, skipped, _ = _parse({'destinations': [item]})
    assert out == {}
    assert skipped == [f'„{name}" hat keine gültigen Koordinaten.']


@pytest.mark.parametrize('token', ['NaN', 'Infinity', '1e400', _BIG])
def test_non_finite_and_overflowing_json_numbers_are_skipped(token):
    # Built as TEXT: json.dumps can neither write a bare NaN token portably
    # nor keep a 401-digit literal as written.
    text = ('{"destinations": [{"name": "F", "kind": "pin", '
            f'"x": {token}, "y": 0.0, "z": 0.05}}]}}')
    out, skipped, present = WorkflowManager._parse_destinations(text)
    assert out == {} and present is True
    assert skipped == ['„F" hat keine gültigen Koordinaten.']


def test_the_coordinate_sentence_names_no_block_and_no_camera():
    _, skipped, _ = _parse({'destinations': [_entry('F', x='x')]})
    assert 'Block' not in skipped[0]
    assert 'Szenen-Kamera' not in skipped[0]


def test_a_duplicate_name_keeps_the_first():
    out, skipped, _ = _parse({'destinations': [
        _entry('A', x=0.1), _entry('A', x=0.3)]})
    assert out['A']['x'] == 0.1
    assert skipped == ['„A" gibt es doppelt.']


def test_the_cap_keeps_64_and_says_so_once():
    items = [_entry(f'Z{i}') for i in range(MAX_PAYLOAD_DESTINATIONS + 1)]
    out, skipped, _ = _parse({'destinations': items})
    assert len(out) == 64
    assert skipped == ['Mehr als 64 Ziele — der Rest wurde nicht übernommen.']


def test_a_non_dict_item_is_named_by_its_position():
    items = [_entry(f'Z{i}') for i in range(9)] + ['kaputt']
    out, skipped, _ = _parse({'destinations': items})
    assert len(out) == 9
    assert skipped == ['Ziel Nr. 10 ist ungültig.']


# ── precedence: the document is authoritative ─────────────────────────────────

def _arm_tcp(arm):
    return tuple(float(v) for v in arm.fk_xyz())


def test_a_payload_entry_beats_a_persisted_one_of_the_same_name():
    status = []
    mgr, arm, _w = _sim_manager([], status, {'z_table': 0.0})
    mgr.set_destination('A', 0.18, -0.12, 0.08)
    program = _move_to_ref('A')
    program['destinations'] = [_entry('A', 'pose', 0.15, 0.10, 0.10)]
    assert _run(mgr, program, status) == 'finished', _errors(status)
    x, y, z = _arm_tcp(arm)
    assert abs(x - 0.15) < 0.005 and abs(y - 0.10) < 0.005 and abs(z - 0.10) < 0.005
    # No write-through: the run left the robot-local entry exactly as it was.
    assert mgr.get_destinations()['A'] == {
        'x': 0.18, 'y': -0.12, 'z': 0.08, 'label': 'A', 'plane_tracked': False}
    assert set(mgr.get_destinations()) == {'A'}


def test_with_the_sibling_present_a_robot_local_name_is_NOT_consulted():
    """Another student's „Position 1" on the same rig must not answer for a Ziel
    this student's document does not have."""
    status = []
    mgr, _arm, _w = _sim_manager([], status, {'z_table': 0.0})
    mgr.set_destination('Position 1', 0.18, -0.12, 0.08)
    program = _move_to_ref('Position 1')
    program['destinations'] = []
    assert _run(mgr, program, status) == 'error'
    assert any(e.startswith('Unbekanntes Ziel: „Position 1"') for e in _errors(status)), (
        _errors(status))


def test_without_the_sibling_the_legacy_robot_local_merge_is_unchanged():
    status = []
    mgr, arm, _w = _sim_manager([], status, {'z_table': 0.0})
    mgr.set_destination('Position 1', 0.18, -0.12, 0.08)
    assert _run(mgr, _move_to_ref('Position 1'), status) == 'finished', _errors(status)
    x, y, z = _arm_tcp(arm)
    assert abs(x - 0.18) < 0.005 and abs(y + 0.12) < 0.005 and abs(z - 0.08) < 0.005


# ── overflow: no sibling parser lets OverflowError escape start() ─────────────

def test_an_overflowing_destination_coordinate_starts_and_warns():
    status = []
    mgr, _arm, _w = _sim_manager([], status, {'z_table': 0.0})
    text = ('{"blocks": {"languageVersion": 0, "blocks": []}, '
            '"destinations": [{"name": "K", "kind": "pin", '
            f'"x": {_BIG}, "y": 0.0, "z": 0.0}}]}}')
    ok, msg, _ = mgr.start(text, 'wf')
    assert ok, msg
    _wait_finished(status)
    assert _warnings(status) == [
        '[WARNUNG] Ziel aus der Sammlung übersprungen: „K" hat keine gültigen Koordinaten.']


def test_an_overflowing_tempo_falls_back_to_normal_speed():
    status = []
    mgr, _arm, _w = _sim_manager([], status, {'z_table': 0.0})
    captured = []
    mgr._run = lambda interpreter, ctx: captured.append(ctx)
    text = f'{{"blocks": {{"languageVersion": 0, "blocks": []}}, "tempo": {_BIG}}}'
    ok, msg, _ = mgr.start(text, 'wf')
    assert ok, msg
    deadline = time.monotonic() + 5.0
    while not captured and time.monotonic() < deadline:
        time.sleep(0.01)
    assert captured and captured[0].tempo == 1.0


def test_parse_minmax_treats_an_overflowing_bound_as_malformed():
    big = int(_BIG)
    assert path_guard._parse_minmax({'min': [big, -0.1, 0.0], 'max': [0.3, 0.1, 0.1]}) is None
    assert path_guard._parse_minmax({'min': [0.1, -0.1, 0.0], 'max': [0.3, 0.1, 0.1]}) == (
        [0.1, -0.1, 0.0], [0.3, 0.1, 0.1])


def test_an_overflowing_zone_next_to_a_concrete_pin_still_starts():
    status = []
    mgr, _arm, _w = _sim_manager([], status, {'z_table': 0.0})
    program = {'blocks': {'languageVersion': 0, 'blocks': [{
        'type': 'edubotics_destination_pin', 'id': 'pin',
        'fields': {'NAME': 'P', 'X': '0.20', 'Y': '0.0', 'Z': '0.05'},
        'next': {'block': _move_to_ref('P')['blocks']['blocks'][0]},
    }]}}
    text = json.dumps(program)[:-1] + (
        f', "zones": [{{"min": [{_BIG}, -0.1, 0.0], "max": [0.3, 0.1, 0.1]}}]}}')
    ok, msg, _ = mgr.start(text, 'wf')
    assert ok, msg
    _wait_finished(status)


# ── skip warnings ─────────────────────────────────────────────────────────────

def _empty_program(destinations):
    return {'blocks': {'languageVersion': 0, 'blocks': []}, 'destinations': destinations}


def test_one_bad_entry_emits_exactly_one_status_warning():
    status = []
    mgr, _arm, _w = _sim_manager([], status, {'z_table': 0.0})
    _run(mgr, _empty_program([_entry('C', 'cam'), _entry('D')]), status)
    warns = [e for e in status if isinstance(e, dict)
             and str(e.get('log_message', '')).startswith('[WARNUNG]')]
    assert len(warns) == 1
    assert warns[0]['log_message'] == (
        '[WARNUNG] Ziel aus der Sammlung übersprungen: „C" hat eine unbekannte Art.')


def test_the_skip_reasons_are_bounded_with_one_summary_line():
    assert MAX_DESTINATION_SKIP_REASONS == 8
    items = [0] * 5000
    out, skipped, present = _parse({'destinations': items})
    assert present and out == {}
    assert skipped == [f'Ziel Nr. {i + 1} ist ungültig.'
                       for i in range(MAX_DESTINATION_SKIP_REASONS)] + [
        'Weitere ungültige Ziele werden nicht einzeln aufgeführt.']


def test_a_flood_of_bad_entries_emits_a_bounded_number_of_warnings():
    status = []
    mgr, _arm, _w = _sim_manager([], status, {'z_table': 0.0})
    bad = [_entry(f'N{i}', x='kaputt') for i in range(3000)]
    _run(mgr, _empty_program([_entry('P')] + bad), status)
    assert len(_warnings(status)) == MAX_DESTINATION_SKIP_REASONS + 1


def test_an_identical_reason_is_emitted_once():
    status = []
    mgr, _arm, _w = _sim_manager([], status, {'z_table': 0.0})
    _run(mgr, _empty_program([_entry('A!'), _entry('A!')]), status)
    assert _warnings(status) == [
        f'[WARNUNG] Ziel aus der Sammlung übersprungen: {destination_name_error_de("A!")}']


def test_a_refused_start_emits_no_skip_warning():
    status = []

    def _boom():
        raise RuntimeError('kaputt')

    mgr = WorkflowManager(
        publisher=lambda *_a, **_k: None,
        ik_factory=_boom,
        load_destinations=lambda: {},
        load_calibration=lambda: {'z_table': 0.0},
        emit_status=status.append,
        on_finished=lambda phase: status.append({'_finished': phase}),
    )
    ok, _msg, _ = mgr.start(json.dumps(_empty_program([_entry('C', 'cam')])), 'wf')
    assert ok is False
    assert _warnings(status) == []


# ── sim resolution: provenance decides the height ─────────────────────────────

def test_a_pin_entry_below_the_virtual_table_re_asks_the_table():
    status = []
    mgr, _arm, _w = _sim_manager([], status, {'z_table': 0.0})
    program = _move_to_ref('P')
    program['destinations'] = [_entry('P', 'pin', 0.2, 0.0, -0.03)]
    assert _run(mgr, program, status) == 'finished', _errors(status)


def test_a_pose_entry_below_the_virtual_table_keeps_its_z_and_is_refused():
    status = []
    mgr, _arm, _w = _sim_manager([], status, {'z_table': 0.0})
    program = _move_to_ref('P')
    program['destinations'] = [_entry('P', 'pose', 0.2, 0.0, -0.03)]
    assert _run(mgr, program, status) == 'error'
    assert any('Tischebene' in e for e in _errors(status)), _errors(status)


# ── preview contract (manager level) ──────────────────────────────────────────

def _wait_block(block_id, seconds=1.5):
    return {'block': {'type': 'edubotics_wait_seconds', 'id': block_id,
                      'inputs': {'SECONDS': {'shadow': {
                          'type': 'math_number', 'fields': {'NUM': seconds}}}}}}


def destination_preview_payload():
    return {
        'blocks': {'languageVersion': 0, 'blocks': [{
            'type': 'edubotics_move_to', 'id': 'vorschau-1',
            'inputs': {'DESTINATION': {'block': {
                'type': 'edubotics_destination_ref', 'id': 'vorschau-2',
                'fields': {'NAME': 'Ablage'}}}},
            'next': _wait_block('vorschau-3'),
        }]},
        'sim': {'enabled': True, 'objects': []},
        'zones': [],
        'tempo': 1.0,
        'destinations': [{'name': 'Ablage', 'kind': 'pin', 'x': 0.20, 'y': 0.0, 'z': 0.05}],
    }


def recording_preview_payload(seconds=1.5):
    return {
        'blocks': {'languageVersion': 0, 'blocks': [{
            'type': 'edubotics_replay_trajectory', 'id': 'vorschau-1',
            'fields': {'NAME': 'Greifen links'},
            'next': _wait_block('vorschau-2', seconds),
        }]},
        'sim': {'enabled': True, 'objects': []},
        'zones': [],
        'tempo': 1.0,
        'trajectories': {'Greifen links': {'fps': 25, 'points': [
            [0.0, -1.5708, 1.5708, 0.0, 0.0, 0.8, 0.0],
            [0.2, -1.5708, 1.5708, 0.0, 0.0, 0.8, 1.0],
        ]}},
    }


@pytest.mark.parametrize('payload', [destination_preview_payload(),
                                     recording_preview_payload()])
def test_both_preview_payloads_start_and_finish_in_the_simulator(payload):
    status = []
    mgr, _arm, _w = _sim_manager([], status, {'z_table': 0.0})
    assert _run(mgr, payload, status, wid='vorschau-test') == 'finished', _errors(status)

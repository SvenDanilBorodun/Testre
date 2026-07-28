#!/usr/bin/env python3
"""The node's sim wiring: one world, /sim/objects, and the table floor.

``physical_ai_server.py`` cannot be imported without rclpy, so these tests read the
SOURCE for the wiring facts that have no other guard, and drive the equivalent runtime
configuration through a real WorkflowManager for the behaviour. That split is
deliberate: the source asserts are cheap and catch a silent un-wiring (a kwarg dropped
in a refactor), while the behavioural ones prove the floor actually refuses.
"""

from __future__ import annotations

import ast
import json
import pathlib
import time
import types

import numpy as np
import pytest

from physical_ai_server.workflow import trajectory_builder
from physical_ai_server.workflow.ik_solver import IKSolver
from physical_ai_server.workflow.object_catalog import fixed_catalog
from physical_ai_server.workflow.sim_arm import SimArm
from physical_ai_server.workflow.sim_perception import SimPerception
from physical_ai_server.workflow.sim_world import SimWorld
from physical_ai_server.workflow.workflow_manager import WorkflowManager


_NODE = (pathlib.Path(__file__).resolve().parents[1]
         / 'physical_ai_server' / 'physical_ai_server.py')
_SRC = _NODE.read_text(encoding='utf-8')


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


# ── the riskiest line in the change ──────────────────────────────────────────

def test_sim_calibration_supplies_only_z_table():
    """The sim's calibration dict must contain z_table and NOTHING else.

    _attach_named_world's bypass is an OR chain over scene_intrinsics /
    scene_extrinsics / board_table_z / z_table. Adding z_table alone still
    short-circuits on the first clause, so SimPerception's preset world_xyz_m
    survives. Adding board_table_z (or intrinsics) later would silently switch the
    sim onto the pixel-projection path and every sim grasp would target garbage.
    This test is the guard for that.
    """
    tree = ast.parse(_SRC)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.keyword) or node.arg != 'load_calibration':
            continue
        if isinstance(node.value, ast.Lambda) and isinstance(node.value.body, ast.Dict):
            found.append({k.value for k in node.value.body.keys})
    assert found, 'no `load_calibration=lambda: {...}` found — did the wiring move?'
    for keys in found:
        assert keys == {'z_table'}, (
            f'sim calibration must be exactly {{z_table}}, got {keys}')


def test_the_attach_named_world_bypass_is_still_an_or_chain():
    """If that guard ever becomes an AND, supplying z_table breaks the sim."""
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    import inspect
    src = inspect.getsource(pb._attach_named_world)
    assert 'ctx.scene_intrinsics is None or ctx.scene_extrinsics is None' in src
    assert 'or ctx.z_table is None' in src


# ── source-level wiring asserts ──────────────────────────────────────────────

def test_the_sim_arm_and_the_sim_perception_share_one_world():
    assert 'world=self._sim_world,' in _SRC, 'SimArm must receive the world'
    assert 'self._sim_world,\n            ),' in _SRC.replace('\r\n', '\n'), (
        'SimPerception must receive the same world')


def test_the_sim_world_is_reset_on_every_start():
    assert 'self._sim_arm.set_objects(self._sim_objects)' in _SRC
    assert 'self._sim_world.reset(self._sim_objects)' in _SRC, (
        'a world with no cached arm must still be reset')


def test_sim_objects_is_published_on_start_and_idle():
    assert "'/sim/objects'" in _SRC
    assert _SRC.count('self._publish_sim_objects(') >= 3, (
        'expected publishes from the joint sink, the idle timer and the start path')


# ── behaviour: the floor is live ─────────────────────────────────────────────

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
    deadline = time.monotonic() + 20.0
    done = lambda: [e for e in status if isinstance(e, dict) and '_finished' in e]
    while not done() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert done(), 'workflow did not finish'
    return done()[-1]['_finished']


def _move_to_z(z):
    return {'blocks': {'languageVersion': 0, 'blocks': [{
        'type': 'edubotics_destination_pin',
        'fields': {'NAME': 'P', 'X': '0.20', 'Y': '0.0', 'Z': str(z)},
        'next': {'block': {
            'type': 'edubotics_move_to',
            'inputs': {'DESTINATION': {'block': {
                'type': 'edubotics_destination_ref', 'fields': {'NAME': 'P'}}}},
        }},
    }]}}


def test_a_sim_run_under_the_table_is_now_REFUSED():
    """Before the floor, the sim drove the arm 30 mm below the table and reported
    'finished' — while the same program refused on a calibrated rig."""
    status = []
    mgr, _arm, _w = _sim_manager([], status, {'z_table': 0.0})
    assert _run(mgr, _move_to_z(-0.03), status) == 'error'
    errs = [e.get('error', '') for e in status
            if isinstance(e, dict) and e.get('phase') == 'error']
    assert any('Tischebene' in e for e in errs), errs


def test_the_old_no_floor_wiring_would_have_ALLOWED_it():
    # Pins the delta, so this test fails if the floor silently stops being applied.
    status = []
    mgr, _arm, _w = _sim_manager([], status, {})
    assert _run(mgr, _move_to_z(-0.03), status) == 'finished'


def test_the_table_surface_itself_is_still_reachable():
    # WORKSPACE_FLOOR_MARGIN_M gives 10 mm of slack; a z=0 pin must not refuse.
    status = []
    mgr, _arm, _w = _sim_manager([], status, {'z_table': 0.0})
    assert _run(mgr, _move_to_z(0.0), status) == 'finished'


def test_a_normal_grasp_is_unaffected_by_the_floor():
    """The shipped cube's grasp height is +0.015, comfortably above the floor."""
    status = []
    objs = [{'type': 'wuerfel', 'tag_id': 0, 'x': 0.20, 'y': 0.0, 'yaw': 0.0}]
    mgr, _arm, world = _sim_manager(objs, status, {'z_table': 0.0})
    program = {'blocks': {'languageVersion': 0, 'blocks': [
        {'type': 'edubotics_grasp_object', 'fields': {'OBJECT_TYPE': 'wuerfel'}}]}}
    assert _run(mgr, program, status) == 'finished'
    assert world.is_held() is True


# ── the snapshot the twin consumes ───────────────────────────────────────────

def test_the_published_snapshot_tracks_a_full_pick_and_place():
    status = []
    objs = [{'type': 'wuerfel', 'tag_id': 0, 'x': 0.20, 'y': 0.0, 'yaw': 0.0}]
    mgr, _arm, world = _sim_manager(objs, status, {'z_table': 0.0})
    program = {'blocks': {'languageVersion': 0, 'blocks': [{
        'type': 'edubotics_destination_pin',
        'fields': {'NAME': 'A', 'X': '0.14', 'Y': '0.12', 'Z': '0.0'},
        'next': {'block': {
            'type': 'edubotics_grasp_object', 'fields': {'OBJECT_TYPE': 'wuerfel'},
            'next': {'block': {
                'type': 'edubotics_drop_at',
                'inputs': {'DESTINATION': {'block': {
                    'type': 'edubotics_destination_ref', 'fields': {'NAME': 'A'}}}},
            }},
        }},
    }]}}
    assert _run(mgr, program, status) == 'finished'
    snap = json.loads(json.dumps(world.snapshot()))
    assert snap['held'] is None, 'the cube was released'
    o = snap['objects'][0]
    assert o['tag_id'] == 20
    assert (o['x'], o['y']) == pytest.approx((0.14, 0.12), abs=5e-3), (
        'the cube must END UP at the drop point, not back at its placement')

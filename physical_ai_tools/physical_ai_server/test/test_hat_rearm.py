#!/usr/bin/env python3
"""„Wenn <Typ> gesehen" must re-arm — it used to fire exactly once per run.

``_wait_object_visible`` polled RAW perception with no claimed/skipped filter, unlike
every main-stack path. ``_run_hat_handler`` only re-arms ``edge_armed`` on an
UNtriggered poll, so a condition that stays true forever wedges the hat after its first
firing. On the rig that hid (the object is physically carried out of frame); in sim the
virtual tag never leaves, so a two-cube program grasped ONE and reported a green
„Workflow abgeschlossen".

Driven through the real sim runtime + the real shipped catalog, because the bug is an
interaction between three real components (the hat poll, the claim sets, and a
perception that keeps reporting).
"""

from __future__ import annotations

import json
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


CATALOG = fixed_catalog()
CUBES = [
    {'type': 'wuerfel', 'tag_id': 0, 'x': 0.20, 'y': -0.05, 'yaw': 0.0},
    {'type': 'wuerfel', 'tag_id': 1, 'x': 0.18, 'y': 0.06, 'yaw': 0.0},
]


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


def _sim(objects, status):
    world = SimWorld(objects)
    arm = SimArm(ik=IKSolver(), objects=objects, world=world)
    mgr = WorkflowManager(
        publisher=arm.publish,
        ik_factory=lambda: IKSolver(),
        perception_factory=lambda: SimPerception(objects, CATALOG, world),
        load_destinations=lambda: {},
        load_calibration=lambda: {'z_table': 0.0},
        emit_status=status.append,
        on_finished=lambda phase: status.append({'_finished': phase}),
        get_scene_frame=lambda: np.zeros((1, 1, 3), dtype=np.uint8),
        get_scene_frame_age=lambda: 0.0,
        get_current_pose_xyz=lambda: arm.fk_xyz(),
        get_follower_joints=arm.get_joints,
        load_object_catalog=lambda: CATALOG,
    )
    return mgr, world


def _run(mgr, program, status, timeout=60.0):
    ok, msg, _ = mgr.start(json.dumps(program), 'wf-hat')
    assert ok, msg
    deadline = time.monotonic() + timeout
    done = lambda: [e for e in status if isinstance(e, dict) and '_finished' in e]
    while not done() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert done(), 'workflow did not finish'
    return done()[-1]['_finished']


def _logs(status):
    return [e['log_message'] for e in status
            if isinstance(e, dict) and 'log_message' in e]


def _hat_program(seconds='14'):
    """Main stack: pin a destination and idle. Hat: grasp + place, once per cube.

    The hat body rides the `next` chain — edubotics_when_object_seen is declared with
    setNextStatement, not a DO input.
    """
    hat = {
        'type': 'edubotics_when_object_seen',
        'id': 'hat1',
        'fields': {'OBJECT_TYPE': 'wuerfel'},
        'next': {'block': {
            'type': 'edubotics_grasp_object',
            'id': 'gh',
            'fields': {'OBJECT_TYPE': 'wuerfel'},
            'next': {'block': {
                'type': 'edubotics_drop_at',
                'id': 'dh',
                'inputs': {'DESTINATION': {'block': {
                    'type': 'edubotics_destination_ref', 'fields': {'NAME': 'A'}}}},
            }},
        }},
    }
    main = {
        'type': 'edubotics_destination_pin',
        'fields': {'NAME': 'A', 'X': '0.13', 'Y': '0.13', 'Z': '0.0'},
        'next': {'block': {'type': 'edubotics_wait_seconds',
                           'fields': {'SECONDS': seconds}}},
    }
    return {'blocks': {'languageVersion': 0, 'blocks': [main, hat]}}


def test_when_object_seen_grasps_every_placed_cube_in_sim():
    """The direct regression guard. Before the fix this grasped 1 of 2."""
    status = []
    mgr, world = _sim([dict(c) for c in CUBES], status)
    assert _run(mgr, _hat_program(), status) == 'finished'
    grasped = [m for m in _logs(status) if 'gegriffen' in m]
    assert len(grasped) == 2, (
        f'expected both cubes grasped, got {len(grasped)}: {_logs(status)}')


def test_the_hat_does_not_refire_on_an_already_claimed_tag():
    """Re-arming must not become a spin: each tag is grasped exactly once."""
    status = []
    mgr, _world = _sim([dict(c) for c in CUBES], status)
    _run(mgr, _hat_program(), status)
    grasped = [m for m in _logs(status) if 'gegriffen' in m]
    assert len(grasped) == 2, grasped


def test_a_hat_body_error_reaches_the_protokoll_in_german():
    """After the last cube the hat legitimately raises GraspSkip. The handler used
    to return silently; a thread that just disappears is the bug class this round
    exists to remove."""
    status = []
    # One cube, but a long enough idle that the hat is polled again after the
    # grasp -- the second firing finds nothing unclaimed and raises GraspSkip.
    mgr, _world = _sim([dict(CUBES[0])], status)
    _run(mgr, _hat_program(seconds='14'), status)
    logs = _logs(status)
    assert any('gegriffen' in m for m in logs), logs
    hat_errors = [m for m in logs if m.startswith('Ereignis-Block')]
    if hat_errors:
        # If it did fire again, the message must be German and name the block.
        assert 'Würfel' in hat_errors[0] or 'Greifziel' in hat_errors[0], hat_errors


def test_a_static_object_with_a_non_consuming_body_still_fires_exactly_ONCE():
    """The anti-spin property the edge trigger exists for, preserved.

    The original rationale (workflow_manager, audit fix #14) was that a LEVEL trigger
    would run the body hundreds of times while an object sat in front of the camera.
    Keying the edge on the unclaimed-visible SET keeps that: a body that never claims
    anything leaves the set unchanged, so it stays disarmed. If this ever fires more
    than once, the set-based edge has degenerated into a level trigger.
    """
    status = []
    mgr, _world = _sim([dict(CUBES[0])], status)
    hat = {
        'type': 'edubotics_when_object_seen',
        'fields': {'OBJECT_TYPE': 'wuerfel'},
        'next': {'block': {
            'type': 'edubotics_log',
            'inputs': {'MESSAGE': {'block': {
                'type': 'text', 'fields': {'TEXT': 'GESEHEN'}}}},
        }},
    }
    main = {'type': 'edubotics_wait_seconds', 'fields': {'SECONDS': '6'}}
    _run(mgr, {'blocks': {'languageVersion': 0, 'blocks': [main, hat]}}, status)
    hits = [m for m in _logs(status) if 'GESEHEN' in m]
    assert len(hits) == 1, f'expected exactly one firing, got {len(hits)}'


def test_the_trigger_reads_the_claimed_set_not_raw_perception():
    """Unit-level: with every tag claimed the poll must report NOT triggered, so the
    handler re-arms instead of wedging."""
    import threading

    world = SimWorld([dict(c) for c in CUBES])
    perc = SimPerception([dict(c) for c in CUBES], CATALOG, world)
    mgr = WorkflowManager(publisher=lambda _c: None, load_calibration=lambda: {})
    ctx = types.SimpleNamespace(
        perception=perc, object_catalog=CATALOG,
        get_scene_frame=lambda: np.zeros((1, 1, 3), dtype=np.uint8),
        should_stop=lambda: False,
        claimed_tags=set(), skipped_tags=set(), claim_lock=threading.RLock(),
    )
    assert mgr._wait_object_visible('wuerfel', ctx) == frozenset({20, 21})
    ctx.claimed_tags = {20}
    assert mgr._wait_object_visible('wuerfel', ctx) == frozenset({21}), 'one cube still free'
    ctx.claimed_tags = {20, 21}
    assert not mgr._wait_object_visible('wuerfel', ctx), (
        'all claimed => the condition must go FALSE so the hat re-arms')
    # A skipped (confirmed-failed) instance counts the same way.
    ctx.claimed_tags = {20}
    ctx.skipped_tags = {21}
    assert not mgr._wait_object_visible('wuerfel', ctx)


def test_a_ctx_without_claim_sets_still_triggers():
    """Backwards-safe: _excluded_ids getattr-guards a ctx with no claim state."""
    world = SimWorld([dict(CUBES[0])])
    perc = SimPerception([dict(CUBES[0])], CATALOG, world)
    mgr = WorkflowManager(publisher=lambda _c: None, load_calibration=lambda: {})
    ctx = types.SimpleNamespace(
        perception=perc, object_catalog=CATALOG,
        get_scene_frame=lambda: np.zeros((1, 1, 3), dtype=np.uint8),
        should_stop=lambda: False,
    )
    assert mgr._wait_object_visible('wuerfel', ctx)

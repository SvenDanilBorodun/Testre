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
from physical_ai_server.workflow import workflow_manager as WM
from physical_ai_server.workflow.workflow_manager import WorkflowManager


CATALOG = fixed_catalog()
CUBES = [
    {'type': 'wuerfel', 'tag_id': 0, 'x': 0.20, 'y': -0.05, 'yaw': 0.0},
    {'type': 'wuerfel', 'tag_id': 1, 'x': 0.18, 'y': 0.06, 'yaw': 0.0},
]


@pytest.fixture(autouse=True)
def _short_hat_keepalive(monkeypatch):
    """A workflow WITH hat handlers now stays alive after its main stack ends,
    until Stop or ``HAT_KEEPALIVE_MAX_S`` — that IS the fix for „a program made
    only of hats finishes instantly having done nothing", and it is why every
    program in this file used to need a long ``wait_seconds`` in the main stack
    as a keep-alive. Shorten the cap so ``_run`` still terminates in a test.
    """
    monkeypatch.setattr(WM, 'HAT_KEEPALIVE_MAX_S', 3.0)
    yield


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


@pytest.mark.parametrize('miss_every, label', [
    (2, 'one tag of two missed on ALTERNATE polls'),
    (6, 'one tag of two missed 1 poll in 6'),
    (0, 'a TOTAL detection dropout every 6th poll'),
])
def test_detection_flicker_does_not_re_arm_the_edge(miss_every, label):
    """THE flicker guard for ``_run_hat_handler``, and it must be able to fail.

    SCOPE, because this test used to claim more than it checked: it drives
    ``_run_hat_handler`` against a SYNTHETIC trigger, so it fixes the handler's
    debounce logic ONLY. It says nothing about what the real
    ``_wait_object_visible`` returns — and for a camera dropout that was a bare
    ``False``, which is not a frozenset, so ``_debounce_absence`` was skipped
    entirely and ``if not triggered: edge_armed = True`` re-armed immediately.
    Modelling a dropout as ``frozenset()`` here (which IS debounced) meant the
    shipped guard could never fail on the shipped bug: measured through this
    very harness, ``frozenset()`` → 1 firing, ``False`` → 10.

    The return type is now pinned separately, at the source, by
    ``test_a_camera_dropout_returns_a_frozenset_not_a_bare_bool`` and
    ``test_a_flickering_camera_fires_the_hat_exactly_once_end_to_end`` below.

    ``test_a_static_object_...ONCE`` above places ONE cube, and a single-element
    set cannot oscillate — so it is structurally incapable of catching this. Two
    cubes CAN: real AprilTag detection drops a tag for a frame or two under
    motion blur, glare or partial occlusion constantly, and edging on the RAW
    per-poll set read that as the object leaving and re-entering the scene.

    Measured before the fix, driving the real ``_run_hat_handler``: a
    non-consuming body fired 30 times in 30 polls at ``miss_every=2`` and 10 at
    ``miss_every=6``. The invariant is EXACTLY ONCE.

    Mutation-checked: neutering ``_debounce_absence`` (returning ``raw``) turns
    all three cases red.
    """
    import threading

    mgr = WorkflowManager(publisher=lambda _c: None, load_calibration=lambda: {})
    fires = {'n': 0}
    state = {'i': 0}
    polls = 30

    def fake_trigger(_hat, _ctx):
        i = state['i']
        state['i'] += 1
        if i >= polls:
            ctx.stop = True
            return frozenset()
        if miss_every == 0:                       # total dropout
            return frozenset() if i % 6 == 5 else frozenset({20, 21})
        return (frozenset({20}) if i % miss_every == miss_every - 1
                else frozenset({20, 21}))

    class _Interp:
        @staticmethod
        def execute_chain(_h, _c, _cb):
            fires['n'] += 1                       # body claims NOTHING

    ctx = types.SimpleNamespace(motion_lock=threading.RLock(), stop=False)
    ctx.should_stop = lambda: ctx.stop
    mgr._wait_for_hat_trigger = fake_trigger
    mgr._emit_status = lambda *_a, **_k: None
    mgr._on_block_change = None
    mgr._workflow_id = 'w'
    mgr._run_hat_handler({'type': 'edubotics_when_object_seen'}, _Interp(), ctx)

    assert fires['n'] == 1, (
        f'{label}: fired {fires["n"]}x in {polls} polls — flicker re-armed the '
        f'edge; a body that claims nothing must fire exactly once')


def test_a_camera_dropout_returns_a_frozenset_not_a_bare_bool():
    """THE guard the synthetic flicker test structurally cannot provide.

    ``_run_hat_handler`` applies ``_debounce_absence`` only
    ``if isinstance(triggered, frozenset)``, so the RETURN TYPE of
    ``_wait_object_visible`` decides whether a dropped frame is debounced at all.
    Every "could not look" path returned a bare ``False`` and therefore skipped
    the debounce entirely.

    The two falsy shapes are NOT interchangeable and both are asserted here:
    "I looked and saw nothing" / "I could not look" must be a frozenset, while
    "there is nothing left to look FOR" must stay a bare False (routing that
    through the debounce would keep the already-claimed ids in the trigger set
    and re-fire the body on them)."""
    import threading

    world = SimWorld([dict(c) for c in CUBES])
    perc = SimPerception([dict(c) for c in CUBES], CATALOG, world)
    mgr = WorkflowManager(publisher=lambda _c: None, load_calibration=lambda: {})

    def _ctx(**over):
        base = dict(perception=perc, object_catalog=CATALOG,
                    get_scene_frame=lambda: np.zeros((1, 1, 3), dtype=np.uint8),
                    should_stop=lambda: False,
                    claimed_tags=set(), skipped_tags=set(),
                    claim_lock=threading.RLock())
        base.update(over)
        return types.SimpleNamespace(**base)

    # A dropped camera frame — the ordinary AprilTag flicker case.
    result = mgr._wait_object_visible('wuerfel', _ctx(get_scene_frame=lambda: None))
    assert isinstance(result, frozenset), (
        f'a dropped frame returned {result!r} ({type(result).__name__}); a '
        f'non-frozenset bypasses _debounce_absence and re-arms the edge')

    # Perception / catalog unavailable — also "could not look".
    assert isinstance(mgr._wait_object_visible('wuerfel', _ctx(perception=None)),
                      frozenset)
    assert isinstance(
        mgr._wait_object_visible('wuerfel', _ctx(object_catalog=None)), frozenset)

    # Nothing left to look FOR must stay a bare False (see the docstring).
    every_tag = {int(i) for i in CATALOG.recipe_for_type('wuerfel').tag_ids}
    exhausted = mgr._wait_object_visible('wuerfel', _ctx(claimed_tags=every_tag))
    assert exhausted is False, (
        'an exhausted type must bypass the debounce so the hat re-arms')


def test_a_flickering_camera_fires_the_hat_exactly_once_end_to_end():
    """The same invariant as the synthetic flicker test, but through the REAL
    ``_wait_object_visible``, so the return type is part of what is measured.

    Before the fix this fired 10 times in 30 polls with a dropout every third
    poll (and 20 with every second)."""
    import threading

    world = SimWorld([dict(c) for c in CUBES])
    perc = SimPerception([dict(c) for c in CUBES], CATALOG, world)
    mgr = WorkflowManager(publisher=lambda _c: None, load_calibration=lambda: {})
    fires = {'n': 0}
    polls = {'n': 0}

    def _frame():
        polls['n'] += 1
        # Every third poll the camera hands back nothing at all.
        if polls['n'] % 3 == 0:
            return None
        return np.zeros((1, 1, 3), dtype=np.uint8)

    ctx = types.SimpleNamespace(
        perception=perc, object_catalog=CATALOG, get_scene_frame=_frame,
        claimed_tags=set(), skipped_tags=set(), claim_lock=threading.RLock(),
        motion_lock=threading.RLock(), stop=False)
    ctx.should_stop = lambda: ctx.stop

    calls = {'n': 0}
    real_trigger = mgr._wait_for_hat_trigger

    def _trigger(hat, c):
        calls['n'] += 1
        if calls['n'] > 20:
            ctx.stop = True
            return frozenset()
        return real_trigger(hat, c)

    class _Interp:
        @staticmethod
        def execute_chain(_h, _c, _cb):
            fires['n'] += 1               # a body that CLAIMS nothing

    mgr._wait_for_hat_trigger = _trigger
    mgr._emit_status = lambda *_a, **_k: None
    mgr._on_block_change = None
    mgr._workflow_id = 'w'
    mgr._run_hat_handler({'type': 'edubotics_when_object_seen',
                          'fields': {'OBJECT_TYPE': 'wuerfel'}}, _Interp(), ctx)

    assert fires['n'] == 1, (
        f'a camera dropping every third frame fired the hat {fires["n"]}x; the '
        f'invariant is exactly once for a body that consumes nothing')


def test_the_absence_debounce_still_lets_a_consumed_tag_age_out(monkeypatch):
    """The debounce must not become a way to never notice progress.

    Holding a tag forever would wedge the hat after its first firing — the very
    bug this file exists for. A tag the body CLAIMED stops appearing and must
    leave the set once the grace elapses, which is what makes „Wenn … gesehen"
    fire once per OBJECT rather than once per run.
    """
    from physical_ai_server.workflow import workflow_manager as WM

    clock = {'t': 1000.0}
    monkeypatch.setattr(WM.time, 'monotonic', lambda: clock['t'])
    seen: dict = {}
    assert WM.WorkflowManager._debounce_absence(frozenset({20, 21}), seen) == \
        frozenset({20, 21})
    # 20 gets claimed and stops appearing; inside the grace it is still held.
    clock['t'] += WM._HAT_ABSENT_GRACE_S * 0.5
    assert WM.WorkflowManager._debounce_absence(frozenset({21}), seen) == \
        frozenset({20, 21}), 'a blink must not read as gone'
    # Past the grace it ages out, so the set CHANGES and the edge re-arms.
    clock['t'] += WM._HAT_ABSENT_GRACE_S
    assert WM.WorkflowManager._debounce_absence(frozenset({21}), seen) == \
        frozenset({21}), 'a genuinely consumed tag must age out'
    # A brand-new tag is admitted IMMEDIATELY (appearances are not debounced).
    assert WM.WorkflowManager._debounce_absence(frozenset({21, 22}), seen) == \
        frozenset({21, 22})


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

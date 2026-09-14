#!/usr/bin/env python3
"""Three ways the recycled-object reclaim could enter a state it never left.

ONE SHAPE, THREE DOORS: a filter applied with no corresponding un-filter, so a
student's physical action could no longer reach the program. All three were
reproduced by execution before they were fixed (2026-09-14), and each test below
pins BOTH halves — the dead end is gone, AND the guard it relaxes still holds.

A  „Wenn <Typ> gesehen" never ran the reclaim, on ANY path.
   ``workflow_manager._wait_object_visible`` filters the claimed/skipped ids
   INLINE, and that filter has two silent shapes, not one: with every tag id of
   the type excluded it returns a bare ``False`` before it ever grabs a frame,
   and with some id merely never placed it grabs a frame and then filters the
   claimed tag out of ``seen``. A program made only of hats therefore went quiet
   for the rest of the run — measured through the real manager: two cubes
   grasped and placed, both moved 250 mm, eight further seconds of polling, zero
   re-grasps, while the SAME program plus one „falls sehe ich …" on the main
   stack recovered both.

B  A SKIPPED tag with no recorded pick position was unreclaimable at ANY
   distance, forever. ``claim_pick_xy`` is written only by the unclaimed branch
   of the reclaim, which needs the tag VISIBLE to a LOOKING block; the skip
   writers need neither — ``grasp_object`` detects through the NON-reclaim
   ``_detect_named``. A tag occluded at a „Solange sichtbar" gate and visible to
   the grasp a moment later got skipped with no reference. This WAS a regression:
   against de0924a the same scenario self-healed on the student's second nudge.

C  „öffne Greifer" stored ``claim_release_xy[tag] = None``, which makes the
   ``tag in release_xy`` branch TRUE and then fails every comparison. Two blocks
   („Greife Würfel" → „öffne Greifer") left that cube unreclaimable for the rest
   of the run.

The harness (synthetic overhead camera, real IK solver, real catalog, real
handlers, real ``_reclaim_recycled``) is shared with test_while_visible_loop.py
rather than copied, like test_release_referenced_reclaim.py already does.
"""

from __future__ import annotations

import time
import types

import pytest

from physical_ai_server.workflow import workflow_manager as WM
from physical_ai_server.workflow.workflow_manager import WorkflowManager
from physical_ai_server.workflow.handlers import motion as _motion
from physical_ai_server.workflow.handlers import perception_blocks as pb
from physical_ai_server.workflow.object_catalog import parse_catalog
from test_while_visible_loop import (
    _Ctx, _StubPerception, _det, _reclaim_lines)


_PICK_20 = (0.18, 0.00)
_PICK_21 = (0.20, 0.05)
_DROP = (0.13, 0.13)
_MOVED_20 = (0.24, -0.20)          # 340 mm from the drop point
_MOVED_21 = (0.16, -0.24)


def _catalog(tag_ids):
    """A catalog whose type declares EXACTLY ``tag_ids``.

    The two hat shapes differ only in whether the catalog has an id the student
    never placed, so the test has to own that number rather than inherit the
    shared three-id fixture."""
    return parse_catalog({'tag_size_m': 0.024, 'types': {'banane': {
        'label_de': 'Banane', 'tag_ids': list(tag_ids), 'object_height_m': 0.040,
        'grasp_depth_m': 0.015, 'gripper_close_rad': -0.30,
        'approach_clear_m': 0.06}}})


@pytest.fixture
def _no_poll_sleep(monkeypatch):
    """Drop the trigger poll's own sleeps, keeping a real monotonic.

    Patched on the MODULE, never on the stdlib ``time`` object — the debounce and
    the rate floor both read ``monotonic`` and a global patch would reach far
    outside this file."""
    import time as _time
    monkeypatch.setattr(WM, 'time', types.SimpleNamespace(
        sleep=lambda _s: None, monotonic=_time.monotonic))
    yield


def _poll(ctx, type_name='banane'):
    """ONE real „Wenn <Typ> gesehen" trigger poll, through the shipped method on
    a real manager (constructed with only its one required argument)."""
    mgr = WorkflowManager(publisher=lambda _chunk: None)
    return mgr._wait_object_visible(type_name, ctx)


def _placed(ctx, tag, at, world=None):
    """A full composite grasp ending in a commanded release at ``at`` — what
    „Greife <Typ>" followed by „ablegen bei" records.

    ``world`` is the test's scene dict; the cube is MOVED there, because the
    reclaim's whole question is whether an object is where the robot put it, and
    a fixture that leaves it at its pick spot reclaims on the very first look for
    a reason that has nothing to do with the student."""
    pb._claim_tag(ctx, tag)
    _motion.note_object_picked_up(ctx, tag)
    ctx.carried_tag = tag
    _motion.note_object_released(ctx, at)
    if world is not None:
        world[tag] = at


# ── A: the „Wenn <Typ> gesehen" hat ──────────────────────────────────────────
def test_A_the_hat_recovers_when_every_tag_id_of_the_type_is_claimed(_no_poll_sleep):
    """Shape 1: the bare-``False`` branch, which grabs no frame at all.

    This is the branch that must pay for a camera read of its own, and the one a
    hats-only program could never leave."""
    world = {20: _PICK_20, 21: _PICK_21}
    ctx = _Ctx(_StubPerception(lambda _c: [_det(t, p) for t, p in sorted(world.items())]))
    ctx.object_catalog = _catalog([20, 21])       # EXACTLY the placed ids

    _placed(ctx, 20, _DROP, world)
    _placed(ctx, 21, _DROP, world)
    assert _poll(ctx) is False, 'all-claimed must still answer a BARE False'

    world[20], world[21] = _MOVED_20, _MOVED_21   # the student moves both

    # Polled like the real handler does, and the deadline is the claim this fix
    # makes to a student: a cube you move is picked up again within about a
    # second. That bound IS the rate floor — the first poll above already spent
    # this type's budget, so the recovery cannot be instant and must not be
    # asserted as such.
    deadline = time.monotonic() + 3 * pb._HAT_RECLAIM_MIN_INTERVAL_S
    while ctx.claimed_tags and time.monotonic() < deadline:
        _poll(ctx)
        time.sleep(0.05)
    assert ctx.claimed_tags == set(), (
        f'both cubes were moved 340 mm and stayed claimed: {ctx.claimed_tags}')
    assert len(_reclaim_lines(ctx)) == 2, _reclaim_lines(ctx)
    assert _poll(ctx) == frozenset({20, 21}), 'the hat must fire again'


def test_A_the_hat_recovers_when_a_tag_id_is_never_placed(_no_poll_sleep):
    """Shape 2: ``wanted`` never empties, so the poll DOES look — and used to
    filter the claimed tag straight out of ``seen`` without reclaiming.

    The shipped „Würfel" declares two tag ids, so a classroom that puts out one
    cube is permanently in this shape. Costs no extra camera read, so it is
    deliberately not rate-limited — and the reclaimed tag must come back in the
    SAME poll, which is what the re-read of the exclusions buys."""
    world = {20: _PICK_20}
    ctx = _Ctx(_StubPerception(lambda _c: [_det(t, p) for t, p in sorted(world.items())]))
    ctx.object_catalog = _catalog([20, 21, 22])   # 21 and 22 never placed

    _placed(ctx, 20, _DROP, world)
    assert _poll(ctx) == frozenset(), (
        'the cube is where the robot put it, so nothing of the wanted set is '
        'visible')

    world[20] = _MOVED_20
    assert _poll(ctx) == frozenset({20}), (
        'the reclaim must un-claim the moved cube AND the same poll must report '
        'it, or the hat stays quiet for another cycle about an object it has '
        'already decided is available again')
    assert 20 not in ctx.claimed_tags


def test_A_the_trigger_poll_never_emits_detections(_no_poll_sleep):
    """The guard the inline filter exists for, and it must survive the fix.

    ``ctx.emit_detections`` is a ``/workflow/status`` publish; at the poll's
    ~5 Hz that is a flood. Both hat shapes are driven here, including the one
    that now grabs a frame of its own."""
    emitted = []
    world = {20: _PICK_20}
    ctx = _Ctx(_StubPerception(lambda _c: [_det(t, p) for t, p in sorted(world.items())]))
    ctx.emit_detections = emitted.append
    ctx.object_catalog = _catalog([20, 21])

    for _ in range(4):                     # shape 2: wanted non-empty, looks
        _poll(ctx)
    _placed(ctx, 20, _DROP, world)
    pb._claim_tag(ctx, 21)
    for _ in range(4):                     # shape 1: all claimed, bare False
        _poll(ctx)
    assert emitted == [], (
        f'the trigger poll published {len(emitted)} detection frames')


def test_A_the_all_claimed_branch_looks_at_most_once_per_second(monkeypatch):
    """The cost this fix adds, bounded. The all-claimed branch performs ZERO
    camera reads without the reclaim, so giving it one is real CPU on an Orange
    Pi — hence the floor, and hence that it is keyed by object TYPE rather than
    by hat thread."""
    # LITERAL PIN (see test_constant_pins.py, rule B-0). The bounds asserted
    # below are DERIVED from this number, so a mutation of the shipped value
    # would move both sides of the comparison together and pass — which is
    # exactly the blind spot that guard exists to close. It is a plain constant,
    # not env-derived, so a plain literal is the right pin shape.
    assert pb._HAT_RECLAIM_MIN_INTERVAL_S == 1.0
    clock = {'t': 1000.0}
    ctx = _Ctx(_StubPerception(lambda _c: [_det(20, _PICK_20)]))
    ctx.object_catalog = _catalog([20])
    pb._claim_tag(ctx, 20)
    base = ctx.perception.calls

    # monkeypatch, never a hand-rolled save/restore: this module reads other
    # attributes of ``time`` elsewhere, and putting a two-attribute stand-in back
    # would leave the whole suite with a crippled module.
    monkeypatch.setattr(pb, 'time', types.SimpleNamespace(
        monotonic=lambda: clock['t'], sleep=lambda _s: None))
    for _ in range(50):                    # 50 polls across 5 simulated seconds
        pb.reclaim_only(ctx, 'banane')
        clock['t'] += 0.1
    looks = ctx.perception.calls - base
    assert looks <= 6, (
        f'{looks} camera reads across 5 s — the {pb._HAT_RECLAIM_MIN_INTERVAL_S} s '
        'floor is not holding')
    assert looks >= 4, f'{looks} camera reads across 5 s — the floor is too coarse'


def test_A_the_two_falsy_shapes_are_still_not_interchangeable(_no_poll_sleep):
    """``_wait_object_visible``'s documented contract, which the fix must not
    touch: a bare ``False`` bypasses the absence debounce and re-arms the edge,
    a ``frozenset()`` does not."""
    ctx = _Ctx(_StubPerception(lambda _c: []))
    ctx.object_catalog = _catalog([20, 21])
    assert _poll(ctx) == frozenset(), 'looked, saw nothing'
    assert _poll(ctx) is not False
    pb._claim_tag(ctx, 20)
    pb._claim_tag(ctx, 21)
    assert _poll(ctx) is False, 'nothing left to look FOR'


# ── B: a skipped tag with no pick record ─────────────────────────────────────
def _skipped_with_no_pick_record():
    """The reachable state: tag 20 occluded at the gate (so the reclaim records
    no pick position for it) and skipped by ``grasp_object``'s own non-reclaim
    detect a moment later. Tag 21 is the in-run CONTROL — seen at the gate, so
    recorded."""
    state = {'occlude20': True, 'p20': (0.60, 0.00), 'p21': (0.62, 0.03)}

    def scene(_c):
        d = [] if state['occlude20'] else [_det(20, state['p20'])]
        return d + [_det(21, state['p21'])]

    ctx = _Ctx(_StubPerception(scene))
    pb.count_unclaimed_visible(ctx, 'banane')      # the „Solange sichtbar" gate
    state['occlude20'] = False
    with pytest.raises(pb.GraspSkip):              # both are out of reach
        pb.grasp_object(ctx, {'object_type': 'banane'})
    assert ctx.skipped_tags == {20, 21}
    assert 20 not in ctx.claim_pick_xy and 21 in ctx.claim_pick_xy
    return ctx, state


def test_B_a_skipped_tag_with_no_pick_record_is_reclaimable_after_a_move():
    ctx, state = _skipped_with_no_pick_record()
    state['p20'], state['p21'] = (0.18, 0.00), (0.20, 0.04)   # slid into reach
    for _ in range(3):
        pb.count_unclaimed_visible(ctx, 'banane')
    assert 21 not in ctx.skipped_tags, 'the control must reclaim (it always did)'
    state['p20'] = (0.18, 0.06)                    # the student nudges it again
    for _ in range(3):
        pb.count_unclaimed_visible(ctx, 'banane')
    assert 20 not in ctx.skipped_tags, (
        'a skipped tag with no pick record stayed stuck at every distance — the '
        'pre-2026-09-14 fall-through un-stuck it on exactly this second nudge')


def test_B_adopting_a_reference_never_reclaims_on_the_FIRST_sighting():
    """The guard the adopt must not weaken. The first look after a skip only
    RECORDS where the object lies; the rule still needs a real
    ``_RECLAIM_MOVE_M`` move afterwards."""
    ctx, state = _skipped_with_no_pick_record()
    state['p20'] = (0.18, 0.00)                    # moved, then LEFT ALONE
    for _ in range(20):
        pb.count_unclaimed_visible(ctx, 'banane')
    assert 20 in ctx.skipped_tags, (
        'adopting the sighting must not itself be a verdict — tag 20 was seen 20 '
        'times in one place and must stay skipped')
    assert ctx.claim_pick_xy.get(20) is not None, 'but it must have been recorded'


def test_B_a_move_below_the_threshold_still_does_not_reclaim():
    ctx, state = _skipped_with_no_pick_record()
    state['p20'] = (0.18, 0.00)
    pb.count_unclaimed_visible(ctx, 'banane')      # adopt
    state['p20'] = (0.18 + 0.5 * pb._RECLAIM_MOVE_M, 0.00)   # half the threshold
    for _ in range(5):
        pb.count_unclaimed_visible(ctx, 'banane')
    assert 20 in ctx.skipped_tags


def test_B_an_unlocatable_sighting_records_no_reference(monkeypatch):
    """Fail-closed, unchanged: a tag that is SEEN but cannot be LOCATED must not
    become a reference a later look is judged against."""
    ctx, state = _skipped_with_no_pick_record()
    monkeypatch.setattr(pb, '_tag_table_xy', lambda *_a, **_k: None)
    state['p20'] = (0.18, 0.00)
    for _ in range(5):
        pb.count_unclaimed_visible(ctx, 'banane')
    assert 20 not in ctx.claim_pick_xy
    assert 20 in ctx.skipped_tags


# ── C: „öffne Greifer" ───────────────────────────────────────────────────────
def _grasped_then(ctx, release):
    """„Greife Banane" through the real composite, then ``release(ctx)``."""
    pb.grasp_object(ctx, {'object_type': 'banane'})
    assert ctx.claimed_tags == {20} and ctx.carried_tag == 20
    release(ctx)


def test_C_open_gripper_records_where_it_let_go():
    """The two-block program: „Greife Würfel" then „öffne Greifer" instead of
    „ablegen bei". The robot knows where it was standing, so the cube must be
    reclaimable once a student moves it."""
    world = {'p': _PICK_20}
    ctx = _Ctx(_StubPerception(lambda _c: [_det(20, world['p'])]))
    _grasped_then(ctx, lambda c: _motion.open_gripper(c, {}))

    xy = ctx.claim_release_xy.get(20)
    assert xy is not None, '„öffne Greifer" recorded no release point'
    assert abs(xy[0] - _PICK_20[0]) < 0.02 and abs(xy[1] - _PICK_20[1]) < 0.02, (
        f'the release point {xy} is not where the arm was standing')

    world['p'] = (0.18, -0.40)                     # the student moves it 400 mm
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1
    assert 20 not in ctx.claimed_tags
    assert len(_reclaim_lines(ctx)) == 1, _reclaim_lines(ctx)


def test_C_a_pose_fk_cannot_read_still_stores_None_and_fails_closed():
    """The guard: never guess a position. With no solver there is no commanded
    (x, y) to know, so ``note_object_released``'s documented ``None`` meaning —
    „released somewhere the robot did not aim for" — is still reached, and that
    tag is simply never re-grasped."""
    world = {'p': _PICK_20}
    ctx = _Ctx(_StubPerception(lambda _c: [_det(20, world['p'])]))
    pb.grasp_object(ctx, {'object_type': 'banane'})
    ik, ctx.ik = ctx.ik, None                      # solver gone after the grasp
    try:
        _motion.open_gripper(ctx, {})
    finally:
        ctx.ik = ik
    assert 20 in ctx.claim_release_xy and ctx.claim_release_xy[20] is None

    world['p'] = (0.18, -0.40)
    for _ in range(5):
        assert pb.count_unclaimed_visible(ctx, 'banane') == 0
    assert 20 in ctx.claimed_tags
    assert _reclaim_lines(ctx) == []


def test_C_a_raising_fk_also_stores_None():
    """Same fail-closed answer for a solver that raises, not just a missing one."""
    ctx = _Ctx(_StubPerception(lambda _c: [_det(20, _PICK_20)]))
    pb.grasp_object(ctx, {'object_type': 'banane'})

    class _Boom:
        def fk(self, _j):
            raise RuntimeError('no')

    ik, ctx.ik = ctx.ik, _Boom()
    try:
        _motion.open_gripper(ctx, {})
    finally:
        ctx.ik = ik
    assert ctx.claim_release_xy[20] is None


def test_C_a_bare_open_gripper_with_nothing_carried_changes_nothing():
    """Unchanged behaviour: a program that never grasped anything must record
    nothing at all — ``note_object_released`` is a no-op with no carried tag."""
    ctx = _Ctx(_StubPerception(lambda _c: [_det(20, _PICK_20)]))
    _motion.open_gripper(ctx, {})
    assert ctx.claim_release_xy == {}
    assert ctx.carried_tag is None

#!/usr/bin/env python3
"""The recycled-object reclaim, judged against the ROBOT'S OWN COMMAND.

THE DEFECT THIS FILE PINS. The reclaim used to infer „where the robot left it"
from a camera SIGHTING taken after the claim. Two consequences, both measured on
the owner's rig:

* when the drop destination lies OUTSIDE the scene camera's view the sighting
  never happens, the reference never arms, and the only surviving rule was the
  PICK rule — „back within 20 mm of the spot it was picked FROM" — which fires on
  exactly one destination out of every destination a student could choose. In
  practice: unrecoverable.
* the reclaim ran from ONE call site, the „Solange sichtbar" loop gate. Every
  VALUE block read through a reclaim-free twin, so a loop nested inside „falls
  sehe ich Würfel" deadlocked permanently: once all tags were claimed
  ``_detect_named`` returned ``[]`` WITHOUT looking at the camera, „sehe ich"
  was False forever, and the only reclaiming block was unreachable.

The replacement asks ``drop_at`` — which DROVE to a commanded (x, y) before it
opened the jaws — instead of asking the camera. That yields both behaviours with
no special-casing: a release point outside the view is far from EVERY visible
position, so any position reclaims; a release point inside the view sits under
the untouched object, so nothing reclaims. And it is immune to a missed AprilTag
look BY CONSTRUCTION — there is no observation for a miss to corrupt.

The harness (synthetic overhead camera, real IK solver, real catalog, real
``_reclaim_recycled``) is shared with test_while_visible_loop.py rather than
copied — cross-test-module imports are an established pattern in this suite.
"""

from __future__ import annotations

import pytest

from physical_ai_server.workflow.handlers import motion as _motion
from physical_ai_server.workflow.handlers import perception_blocks as pb
from test_while_visible_loop import (
    _Ctx, _StubPerception, _det, _reclaim_lines, _scripted_ctx)


# The scene camera watches a patch around the arm's pick band. „In view" is any
# position these tests hand to _det(); „out of view" is a release point the
# camera never sees — which is the whole point: it is a COMMAND, so the robot
# knows it without looking.
_PICK = (0.18, 0.00)
_DROP_IN_VIEW = (0.22, 0.06)
_OFF_CAMERA = (0.05, -0.55)      # a bin beside the table, far from anything seen
_SETTLE_15MM = (0.22 + 0.015, 0.06)
_NUDGE_1 = (0.22 + 0.030, 0.06)
_NUDGE_2 = (0.22 + 0.060, 0.06)
_MOVED_150MM = (0.22, 0.06 + 0.150)


def _place(ctx, tag, xy):
    """What ``motion.drop_at`` records after its release publish."""
    ctx.carried_tag = tag
    _motion.note_object_released(ctx, xy)


def _grasped_and_placed(ctx, tag, xy):
    """A full composite grasp: claim, pick up, then place at commanded ``xy``."""
    pb._claim_tag(ctx, tag)
    _motion.note_object_picked_up(ctx, tag)
    _place(ctx, tag, xy)


def _look(ctx):
    return pb.count_unclaimed_visible(ctx, 'banane')


# ── 1-4: the destination is INSIDE the camera's view ─────────────────────────
def test_1_an_untouched_object_that_settled_15mm_from_the_aim_point_is_not_reclaimed():
    """The reason the release threshold is its own, LARGER number.

    ``motion.DROP_HEIGHT_M`` is 0.05 m, so the jaws open a full drop height above
    the surface and the object tumbles. A threshold at the 20 mm skip value would
    read this settle as „moved" on the very next look and the arm would shuffle
    the same cube around the table for the rest of the run."""
    ctx = _scripted_ctx([[_det(20, _PICK)], [_det(20, _SETTLE_15MM)]])
    assert _look(ctx) == 1
    _grasped_and_placed(ctx, 20, _DROP_IN_VIEW)
    for _ in range(3):
        assert _look(ctx) == 0
    assert 20 in ctx.claimed_tags
    assert _reclaim_lines(ctx) == []


def test_2_one_missed_look_then_seen_at_the_drop_point_is_not_reclaimed():
    """THE regression the sighting-derived design had, and the reason this one
    cannot have it: with no observation feeding the reference, a miss is not an
    input to anything. The old design needed a prior-absence precondition and an
    „anchor AFTER the claim" trap to approximate this."""
    ctx = _scripted_ctx([[_det(20, _PICK)], [], [_det(20, _DROP_IN_VIEW)],
                         [_det(20, _DROP_IN_VIEW)]])
    assert _look(ctx) == 1
    _grasped_and_placed(ctx, 20, _DROP_IN_VIEW)
    assert _look(ctx) == 0       # ONE missed look
    assert _look(ctx) == 0       # back, on the spot it was put
    assert _look(ctx) == 0
    assert 20 in ctx.claimed_tags
    assert _reclaim_lines(ctx) == []


def test_3_an_object_moved_150mm_from_the_drop_point_is_reclaimed():
    ctx = _scripted_ctx([[_det(20, _PICK)], [_det(20, _MOVED_150MM)]])
    assert _look(ctx) == 1
    _grasped_and_placed(ctx, 20, _DROP_IN_VIEW)
    assert _look(ctx) == 1
    assert 20 not in ctx.claimed_tags
    lines = _reclaim_lines(ctx)
    assert len(lines) == 1 and 'liegt jetzt woanders' in lines[0], lines


def test_4_two_successive_30mm_nudges_are_judged_against_the_COMMANDED_point():
    """The reference does NOT walk with the object, and the outcome is stated
    deliberately rather than inherited.

    Each nudge is 30 mm, below the 50 mm release threshold on its own. Because
    the reference is the fixed commanded point and never the last sighting, the
    SECOND nudge is judged at 60 mm from it — so it DOES reclaim. A design that
    re-anchored on every sighting would instead let a student walk an object to
    the far side of the table 30 mm at a time and never notice; a design that
    accumulated distance would reclaim on the first nudge. This one reclaims when
    the object is far from where the robot put it, which is the question the
    student is actually asking."""
    ctx = _scripted_ctx([[_det(20, _PICK)], [_det(20, _NUDGE_1)], [_det(20, _NUDGE_2)]])
    assert _look(ctx) == 1
    _grasped_and_placed(ctx, 20, _DROP_IN_VIEW)
    assert _look(ctx) == 0, 'one 30 mm nudge is inside the 50 mm settle allowance'
    assert 20 in ctx.claimed_tags
    assert _look(ctx) == 1, 'a second 30 mm nudge is 60 mm from the COMMANDED point'
    assert 20 not in ctx.claimed_tags


# ── 5: the destination is OUTSIDE the camera's view — the owner's rig ────────
@pytest.mark.parametrize('where', [
    (0.18, 0.00), (0.22, 0.06), (0.16, -0.05), (0.24, 0.02), (0.20, 0.10)])
def test_5_an_off_camera_drop_reclaims_wherever_the_object_reappears(where):
    """The rig that motivated the redesign: the program drops into a bin the
    scene camera cannot see. The sighting-derived anchor never armed there, so the
    object could only be reclaimed by being put back within 20 mm of its original
    pick spot. A commanded release point is known without looking, so every
    visible position is far from it and ANY placement counts."""
    ctx = _scripted_ctx([[_det(20, _PICK)], [_det(20, where)]])
    assert _look(ctx) == 1
    _grasped_and_placed(ctx, 20, _OFF_CAMERA)
    assert _look(ctx) == 1, f'put back at {where} after an off-camera drop'
    assert 20 not in ctx.claimed_tags


# ── 6: two objects, one destination ──────────────────────────────────────────
def test_6_two_tags_released_at_the_same_destination_are_both_reclaimed():
    """The reference is PER TAG, not per destination — a stacking program places
    every cube at the same spot, and putting both back must bring both back."""
    ctx = _scripted_ctx([
        [_det(20, _PICK), _det(21, (0.16, -0.05))],
        [_det(20, _MOVED_150MM), _det(21, (0.14, -0.08))],
    ])
    assert _look(ctx) == 2
    _grasped_and_placed(ctx, 20, _DROP_IN_VIEW)
    _grasped_and_placed(ctx, 21, _DROP_IN_VIEW)
    assert _look(ctx) == 2
    assert ctx.claimed_tags == set()
    assert len(_reclaim_lines(ctx)) == 2, ctx.logs


# ── 7: mid-carry ─────────────────────────────────────────────────────────────
def test_7a_a_carried_object_is_not_reclaimed_by_the_loop_gate():
    """A held object's projected position travels with the gripper, so every
    sighting of it is „somewhere else". ``ctx.carried_tag`` is what makes it safe
    for every looking block to run the reclaim."""
    ctx = _Ctx(_StubPerception([_det(20, _MOVED_150MM)]))
    _grasped_and_placed(ctx, 20, _DROP_IN_VIEW)
    _motion.note_object_picked_up(ctx, 20)        # …picked up again
    assert ctx.carried_tag == 20
    assert _look(ctx) == 0
    assert 20 in ctx.claimed_tags
    assert _reclaim_lines(ctx) == []


def test_7b_a_carried_object_is_not_reclaimed_by_find_object():
    """„finde" is called MID-CARRY by the taught split-grasp pattern (finde →
    fahre über → senke → schließe → hebe an → lege ab). It used to be routed
    through a reclaim-free twin for exactly this; the twin is gone and the guard
    is the carried tag itself."""
    ctx = _Ctx(_StubPerception([_det(20, _MOVED_150MM)]))
    _grasped_and_placed(ctx, 20, _DROP_IN_VIEW)
    _motion.note_object_picked_up(ctx, 20)
    assert pb.find_object(ctx, {'object_type': 'banane'}) is None
    assert pb.see_object(ctx, {'object_type': 'banane'}) is False
    assert pb.count_object(ctx, {'object_type': 'banane'}) == 0
    assert 20 in ctx.claimed_tags
    assert _reclaim_lines(ctx) == []


def test_7c_the_split_path_marks_the_carry_at_close_on_object():
    """``close_on_object`` is the split path's pick-up event. It does NOT claim
    (that is „merke … als erledigt"), and a carried_tag for an unclaimed tag is
    harmless — the reclaim only consults claimed or skipped tags — but without it
    a mid-carry „finde" would judge the held object against its previous release
    point."""
    ctx = _Ctx(_StubPerception([_det(20, _PICK)]))
    _place(ctx, 20, _DROP_IN_VIEW)                 # a previous „ablegen bei"
    ziel = _det(20, _PICK)
    ziel.world_xyz_m = (0.18, 0.0, 0.025)
    ziel.extras['gripper_close_rad'] = -0.30
    _motion.close_on_object(ctx, {'ziel': ziel})
    assert ctx.carried_tag == 20
    assert 20 not in ctx.claim_release_xy, (
        'picking an object up must drop the stale release point')


# ── 8: the master rollback ───────────────────────────────────────────────────
def test_8_reclaim_move_zero_disables_both_halves(monkeypatch):
    """``EDUBOTICS_RECLAIM_MOVE_M=0`` is the one-variable rollback and it covers
    the release rule too — a fall-through would be worse than useless, since
    ``distance >= 0`` is true of every sighting."""
    monkeypatch.setattr(pb, '_RECLAIM_MOVE_M', 0.0)
    ctx = _scripted_ctx([[_det(20, _PICK)], [_det(20, _MOVED_150MM)]])
    assert _look(ctx) == 1
    _grasped_and_placed(ctx, 20, _OFF_CAMERA)      # the strongest reclaim case…
    for _ in range(3):
        assert _look(ctx) == 0                     # …and it stays claimed
    assert 20 in ctx.claimed_tags
    assert _reclaim_lines(ctx) == []


def test_8b_a_non_positive_release_threshold_falls_back_to_the_skip_threshold():
    """A source fence on the pair. The two knobs must never end up with the
    claimed half silently disabled while the skip half still fires: turning the
    reclaim OFF is one knob, ``EDUBOTICS_RECLAIM_MOVE_M``."""
    import inspect
    src = inspect.getsource(pb)
    assert 'if not (_RELEASE_MOVE_M > 0.0):' in src, src[:0]
    assert '_RELEASE_MOVE_M = _RECLAIM_MOVE_M' in src
    assert pb._RELEASE_MOVE_M > 0.0


# ── the blind state speaks, once per type per run ────────────────────────────
def test_the_all_done_notice_fires_once_per_type_per_run():
    """„sehe ich Würfel" answering False is TRUE and useless when the cubes are
    lying right there, already done. The unclaimed view is the only place that
    knows the difference — and after the reclaim, saying so is actionable.

    Deduped, because every looking block reaches here now, including „warte bis"
    at ~5 Hz."""
    ctx = _Ctx(_StubPerception([_det(20, _PICK), _det(21, (0.16, -0.05))]))
    ctx.claimed_tags |= {20, 21, 22}
    for _ in range(5):
        assert _look(ctx) == 0
        assert pb.see_object(ctx, {'object_type': 'banane'}) is False
    notices = [m for m in ctx.logs if 'schon erledigt' in m]
    assert len(notices) == 1, notices
    assert 'Alle „Banane" sind schon erledigt' in notices[0]
    assert '[WARNUNG]' not in notices[0], 'a finished table is not a fault'
    assert ctx.all_done_notified == {'banane'}


def test_the_all_done_notice_stops_once_an_object_is_reclaimed():
    """It is a statement about the CURRENT state, so it must not be emitted while
    something is graspable again."""
    ctx = _scripted_ctx([[_det(20, _PICK)], [_det(20, _MOVED_150MM)]])
    assert _look(ctx) == 1
    _grasped_and_placed(ctx, 20, _DROP_IN_VIEW)
    ctx.logs.clear()
    assert _look(ctx) == 1                  # reclaimed → something to grasp
    assert not any('schon erledigt' in m for m in ctx.logs), ctx.logs


# ── the end-to-end property that fixes the owner's deadlock ──────────────────
def test_see_object_recovers_after_a_reclaim():
    """THE reason change 4 exists. A „Solange sichtbar" loop nested inside „falls
    sehe ich Würfel" could never restart: „sehe ich" read through a reclaim-free
    twin, and once every tag was claimed ``_detect_named`` returned ``[]`` without
    reading the camera at all, so the condition was False forever and the only
    reclaiming block was unreachable. „sehe ich" now runs the reclaim itself."""
    ctx = _scripted_ctx([[_det(20, _PICK)], [_det(20, _DROP_IN_VIEW)],
                         [_det(20, _MOVED_150MM)]])
    assert pb.see_object(ctx, {'object_type': 'banane'}) is True
    _grasped_and_placed(ctx, 20, _DROP_IN_VIEW)
    assert pb.see_object(ctx, {'object_type': 'banane'}) is False   # done, untouched
    assert pb.see_object(ctx, {'object_type': 'banane'}) is True    # a person moved it
    assert 20 not in ctx.claimed_tags


def test_the_simulator_reclaim_stays_a_no_op():
    """A sim run has no scene intrinsics, so ``_tag_table_xy`` returns ``None``
    for every tag and every comparison fails closed. Pinned here because the
    reclaim now runs from five call sites instead of one."""
    ctx = _Ctx(_StubPerception([_det(20, _MOVED_150MM)]))
    ctx.scene_intrinsics = None
    ctx.scene_extrinsics = None
    ctx.board_table_z = None
    _grasped_and_placed(ctx, 20, _OFF_CAMERA)
    for _ in range(3):
        assert _look(ctx) == 0
    assert 20 in ctx.claimed_tags
    assert _reclaim_lines(ctx) == []

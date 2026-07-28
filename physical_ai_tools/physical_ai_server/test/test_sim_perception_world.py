#!/usr/bin/env python3
"""SimPerception × SimWorld — report where an object IS, not where it was placed.

SimPerception resolved the placed objects ONCE in ``__init__`` and served that frozen
snapshot for the whole run, so after a pick-and-place the detector still pointed at the
vacated spot and the arm drove to empty space. With a world bound it reads live.

Driven through the REAL shipped catalog (``fixed_catalog()``) rather than a bespoke test
catalog wherever the tag-id assignment is what is under test — the per-type placement
order + ``len(tag_ids)`` cap IS the behaviour, and the shipped „Würfel" has exactly two
tags, which is the number that produces the interesting cases.
"""

from __future__ import annotations

import pytest

from physical_ai_server.workflow.object_catalog import fixed_catalog
from physical_ai_server.workflow.sim_perception import SimPerception
from physical_ai_server.workflow.sim_world import MAX_SIM_OBJECTS, SimWorld


CATALOG = fixed_catalog()
# The shipped cube: object_height_m 0.030 − grasp_depth_m 0.015, virtual table z = 0.
WUERFEL_GRASP_Z = 0.015


def _cube(x, y, yaw=0.0):
    return {'type': 'wuerfel', 'tag_id': 0, 'x': x, 'y': y, 'yaw': yaw}


def _detect(perc, aruco_id=None):
    return perc.detect(None, 'scene', 'apriltag', aruco_id)


# ── live positions ───────────────────────────────────────────────────────────

def test_sim_perception_reports_the_carried_position_not_the_placed_one():
    """The ghost-cube fix, at the detector."""
    world = SimWorld([_cube(0.20, 0.0)])
    perc = SimPerception(world.objects(), CATALOG, world)

    d = _detect(perc)[0]
    assert d.world_xyz_m == pytest.approx((0.20, 0.0, WUERFEL_GRASP_Z))

    world.capture_nearest(0.20, 0.0, 0.06)
    world.carry_to(0.13, 0.11)
    world.release()

    d = _detect(perc)[0]
    assert d.world_xyz_m == pytest.approx((0.13, 0.11, WUERFEL_GRASP_Z)), (
        'the detector must follow the object, not the placement')
    # The grasp HEIGHT is a property of the recipe, not of the carry — it must not
    # drift when the object moves in XY.
    assert d.world_xyz_m[2] == pytest.approx(WUERFEL_GRASP_Z)


def test_sim_perception_reports_the_live_yaw():
    world = SimWorld([_cube(0.20, 0.0, yaw=0.3)])
    perc = SimPerception(world.objects(), CATALOG, world)
    assert _detect(perc)[0].extras['tag_yaw'] == pytest.approx(0.3)


def test_a_held_object_stays_detectable_so_the_reclaim_never_fires():
    """Load-bearing, and the opposite of the intuitive design.

    Making a carried object invisible would start perception_blocks' per-tag absence
    clock; _reclaim_recycled un-claims a tag that was absent >= EDUBOTICS_RECLAIM_ABSENT_S
    (1.5 s, far shorter than any real carry) and then reappeared — so every placed cube
    would be un-claimed at the drop point and „Solange sichtbar" would never terminate.
    """
    world = SimWorld([_cube(0.20, 0.0)])
    perc = SimPerception(world.objects(), CATALOG, world)
    world.capture_nearest(0.20, 0.0, 0.06)
    world.carry_to(0.10, 0.10)
    dets = _detect(perc)
    assert len(dets) == 1, 'a held object must remain visible'
    assert dets[0].world_xyz_m == pytest.approx((0.10, 0.10, WUERFEL_GRASP_Z))


def test_detect_builds_fresh_extras_per_call_even_with_a_world():
    world = SimWorld([_cube(0.20, 0.0)])
    perc = SimPerception(world.objects(), CATALOG, world)
    first = _detect(perc)[0]
    first.extras['tag_yaw'] = 99.0
    assert _detect(perc)[0].extras['tag_yaw'] == pytest.approx(0.0)


def test_aruco_id_filter_still_applies_with_a_world():
    world = SimWorld([_cube(0.20, 0.0), _cube(0.10, 0.0)])
    perc = SimPerception(world.objects(), CATALOG, world)
    ids = sorted(d.aruco_id for d in _detect(perc))
    assert ids == [20, 21]
    assert [d.aruco_id for d in _detect(perc, aruco_id=21)] == [21]


# ── tag binding ──────────────────────────────────────────────────────────────

def test_sim_perception_binds_the_assigned_tag_back_into_the_world():
    """The front end's tag_id is a local counter the runtime ignores; this is the one
    place the editor's placement slot and the catalog tag id are joined, and it is what
    lets the /sim/objects snapshot name an object the way the program does."""
    world = SimWorld([_cube(0.20, 0.0), _cube(0.10, 0.0)])
    SimPerception(world.objects(), CATALOG, world)
    assert [o['tag_id'] for o in world.objects()] == [20, 21]


def test_an_overflow_object_is_bound_to_nothing():
    # The shipped „Würfel" has exactly two tag ids; a third placement is skipped.
    world = SimWorld([_cube(0.20, 0.0), _cube(0.10, 0.0), _cube(0.15, 0.08)])
    perc = SimPerception(world.objects(), CATALOG, world)
    assert len(_detect(perc)) == 2
    assert [o['tag_id'] for o in world.objects()] == [20, 21, None]


def test_an_object_perception_never_emits_is_not_graspable():
    """Otherwise the arm reports HELD on an invisible object while the intended cube
    stays detectable at its original position — the ghost bug in a new form."""
    objs = [_cube(0.20, 0.0), _cube(0.15, 0.0), _cube(0.10, 0.0)]   # 3rd exceeds the cap
    world = SimWorld(objs)
    SimPerception(objs, CATALOG, world)
    assert [o['tag_id'] for o in world.objects()] == [20, 21, None]
    # Closing right on the un-tagged third cube grabs nothing.
    assert world.capture_nearest(0.10, 0.0, 0.02) is None
    assert world.is_held() is False
    # ...while a tagged one in the same call still works.
    assert world.capture_nearest(0.15, 0.0, 0.02) == 1


def test_an_unknown_type_is_drawn_but_inert():
    objs = [{'type': 'nonexistent', 'x': 0.15, 'y': 0.0, 'yaw': 0.0}]
    world = SimWorld(objs)
    perc = SimPerception(objs, CATALOG, world)
    assert len(world.objects()) == 1, 'the twin should still draw what was placed'
    assert _detect(perc) == []
    assert world.capture_nearest(0.15, 0.0, 0.06) is None


def test_the_world_and_the_detector_agree_on_the_object_cap():
    # A world entry the detector never emits would be capturable but invisible.
    many = [_cube(0.20, 0.0) for _ in range(MAX_SIM_OBJECTS + 5)]
    world = SimWorld(many)
    assert len(world.objects()) == MAX_SIM_OBJECTS


def test_skipped_rows_do_not_shift_the_join_key():
    """A malformed row must not renumber the rows after it — both halves key on the
    index in the list the editor SENT."""
    objs = [
        'not-a-dict',                 # dropped by both
        _cube(0.20, 0.0),             # slot 1
        {'type': 'nonexistent', 'x': 0.1, 'y': 0.0, 'yaw': 0.0},   # unknown type
        _cube(0.10, 0.0),             # slot 3
    ]
    world = SimWorld(objs)
    perc = SimPerception(objs, CATALOG, world)
    by_key = {o['key']: o for o in world.objects()}
    # The non-dict row is dropped by BOTH; the unknown-type row survives in the
    # scene (the student did place it, and the twin should draw it) but carries no
    # tag, so it is neither detectable nor graspable.
    assert sorted(by_key) == [1, 2, 3]
    assert by_key[2]['tag_id'] is None
    assert by_key[1]['tag_id'] == 20 and by_key[3]['tag_id'] == 21
    world.capture_nearest(0.10, 0.0, 0.02)
    world.carry_to(0.05, 0.05)
    moved = {d.aruco_id: d.world_xyz_m for d in _detect(perc)}
    assert moved[21][:2] == pytest.approx((0.05, 0.05))
    assert moved[20][:2] == pytest.approx((0.20, 0.0))


# ── the sensor-snapshot label ────────────────────────────────────────────────

def test_label_is_tag_id_so_debug_sensoren_is_not_blank_in_sim():
    """physical_ai_server's sensor snapshot parses visible_apriltag_ids out of labels
    shaped 'tag<digits>'. A type-named label made that list empty for every sim run."""
    world = SimWorld([_cube(0.20, 0.0)])
    perc = SimPerception(world.objects(), CATALOG, world)
    d = _detect(perc)[0]
    assert d.label == 'tag20'
    assert d.label.startswith('tag') and d.label[3:].isdigit()
    # The type name is still carried where the handlers actually read it.
    assert d.extras['object_type'] == 'wuerfel'


# ── the world=None spine ─────────────────────────────────────────────────────

def test_sim_perception_without_a_world_serves_the_frozen_snapshot():
    objs = [_cube(0.20, 0.0)]
    perc = SimPerception(objs, CATALOG)          # no world
    objs[0]['x'] = 0.05                          # mutate the caller's list
    assert _detect(perc)[0].world_xyz_m == pytest.approx((0.20, 0.0, WUERFEL_GRASP_Z))


def test_a_raising_world_degrades_to_the_frozen_snapshot():
    class _Boom:
        def objects(self):
            raise RuntimeError('boom')

        def bind_tag(self, *a):
            raise RuntimeError('boom')

    perc = SimPerception([_cube(0.20, 0.0)], CATALOG, _Boom())
    assert _detect(perc)[0].world_xyz_m == pytest.approx((0.20, 0.0, WUERFEL_GRASP_Z))

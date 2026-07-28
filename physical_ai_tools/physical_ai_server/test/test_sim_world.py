#!/usr/bin/env python3
"""SimWorld — the mutable virtual scene for the Roboter Studio simulation.

Before SimWorld the sim scene was frozen at run start, so a grasped-and-placed cube was
still reported at its ORIGINAL position and the arm drove to empty space on the next
pass. These tests pin the four properties that fix depends on: a run-start reset, a
NEAREST-wins capture, carry/release actually moving the object, and a snapshot the
React twin can render.

Pure stdlib — no numpy, no rclpy, no container.
"""

from __future__ import annotations

import json

import pytest

from physical_ai_server.workflow.sim_world import SimWorld


def _cube(x, y, yaw=0.0, type_name='wuerfel'):
    return {'type': type_name, 'tag_id': 0, 'x': x, 'y': y, 'yaw': yaw}


def _resolved_world(objects):
    """A world whose objects have all been RESOLVED by perception.

    Only an object with a bound tag id is graspable (see capture_nearest), so a bare
    SimWorld is deliberately inert until SimPerception has bound the catalog tags.
    These unit tests bind them by hand to isolate SimWorld from the catalog.
    """
    w = SimWorld(objects)
    for i, o in enumerate(w.objects()):
        w.bind_tag(o['key'], 20 + i)
    return w


# ── reset ────────────────────────────────────────────────────────────────────

def test_reset_restores_placed_coords_and_bumps_epoch():
    # A second run must start from the PLACEMENT, not from wherever run 1 left the
    # object — otherwise the student's scene silently mutates between runs.
    w = _resolved_world([_cube(0.20, 0.0)])
    epoch0 = w.epoch()
    w.capture_nearest(0.20, 0.0, 0.06)
    w.carry_to(0.10, 0.15)
    w.release()
    assert w.objects()[0]['x'] == pytest.approx(0.10)

    w.reset([_cube(0.20, 0.0)])
    o = w.objects()[0]
    assert (o['x'], o['y']) == pytest.approx((0.20, 0.0))
    assert (o['x'], o['y']) == pytest.approx((o['placed_x'], o['placed_y']))
    assert w.held_key() is None
    assert w.epoch() > epoch0


def test_reset_drops_malformed_and_nonfinite_entries():
    # /workflow/start is an untrusted boundary and json.loads accepts literal NaN.
    # SimPerception drops the same rows, so both halves must agree on what exists.
    w = SimWorld([
        _cube(0.20, 0.0),
        'not-a-dict',
        {'type': 'wuerfel', 'x': float('nan'), 'y': 0.0, 'yaw': 0.0},
        {'type': 'wuerfel', 'x': 0.1, 'y': float('inf'), 'yaw': 0.0},
        {'type': 'wuerfel', 'x': 'zwei', 'y': 0.0, 'yaw': 0.0},
    ])
    assert [o['key'] for o in w.objects()] == [0]


def test_reset_with_none_empties_the_scene():
    w = SimWorld([_cube(0.20, 0.0)])
    w.reset(None)
    assert w.objects() == []
    assert w.held_key() is None


# ── capture ──────────────────────────────────────────────────────────────────

def test_capture_picks_the_nearest_not_the_first():
    # Listed NEAREST-LAST on purpose: a first-match or any-match implementation
    # passes a "two cubes" test by accident unless the near one is last.
    w = _resolved_world([_cube(0.25, 0.0), _cube(0.22, 0.0)])   # 50 mm away, then 20 mm
    key = w.capture_nearest(0.20, 0.0, 0.06)
    assert key == 1, 'capture must be NEAREST-wins, not first-match'
    assert w.objects()[1]['key'] == 1


def test_capture_outside_radius_returns_none_and_holds_nothing():
    w = _resolved_world([_cube(0.30, 0.0)])
    assert w.capture_nearest(0.20, 0.0, 0.06) is None
    assert w.is_held() is False


def test_capture_is_a_noop_while_something_is_held():
    # A second close while carrying must never swap objects.
    w = _resolved_world([_cube(0.20, 0.0), _cube(0.10, 0.0)])
    first = w.capture_nearest(0.20, 0.0, 0.06)
    again = w.capture_nearest(0.10, 0.0, 0.06)
    assert first == again == 0


def test_capture_rejects_nonfinite_without_holding():
    w = _resolved_world([_cube(0.20, 0.0)])
    assert w.capture_nearest(float('nan'), 0.0, 0.06) is None
    assert w.is_held() is False


# ── carry / release ──────────────────────────────────────────────────────────

def test_carry_then_release_leaves_the_object_at_the_release_point():
    w = _resolved_world([_cube(0.20, 0.0)])
    w.capture_nearest(0.20, 0.0, 0.06)
    w.carry_to(0.14, 0.12)
    assert w.is_held() is True
    released = w.release()
    assert released == 0
    assert w.is_held() is False
    o = w.objects()[0]
    assert (o['x'], o['y']) == pytest.approx((0.14, 0.12))
    # The placement is remembered for the next reset, not overwritten by the carry.
    assert (o['placed_x'], o['placed_y']) == pytest.approx((0.20, 0.0))


def test_carry_without_a_hold_moves_nothing():
    w = _resolved_world([_cube(0.20, 0.0)])
    w.carry_to(0.05, 0.05)
    assert (w.objects()[0]['x'], w.objects()[0]['y']) == pytest.approx((0.20, 0.0))


def test_release_with_nothing_held_is_none():
    assert _resolved_world([_cube(0.20, 0.0)]).release() is None


# ── tag binding + wire ───────────────────────────────────────────────────────

def test_bind_tag_joins_the_front_end_slot_to_the_catalog_tag():
    # The front end's tag_id is a local counter the runtime ignores; SimPerception
    # assigns the real catalog tag by per-type placement order and binds it back.
    w = SimWorld([_cube(0.20, 0.0), _cube(0.10, 0.0)])
    w.bind_tag(0, 20)
    w.bind_tag(1, 21)
    assert [o['tag_id'] for o in w.objects()] == [20, 21]


def test_bind_tag_ignores_an_unknown_slot_and_junk():
    w = SimWorld([_cube(0.20, 0.0)])
    w.bind_tag(7, 20)
    w.bind_tag('x', 20)
    assert w.objects()[0]['tag_id'] is None


def test_snapshot_is_json_round_trippable_and_carries_epoch_held_tag_id():
    w = SimWorld([_cube(0.20, 0.0, yaw=0.3)])
    w.bind_tag(0, 20)
    w.capture_nearest(0.20, 0.0, 0.06)
    snap = json.loads(json.dumps(w.snapshot()))
    assert snap['held'] == 0
    assert snap['epoch'] == w.epoch()
    assert snap['objects'] == [
        {'key': 0, 'type': 'wuerfel', 'tag_id': 20, 'x': 0.2, 'y': 0.0, 'yaw': 0.3},
    ]


def test_objects_returns_copies_not_the_internal_dicts():
    w = SimWorld([_cube(0.20, 0.0)])
    w.objects()[0]['x'] = 99.0
    assert w.objects()[0]['x'] == pytest.approx(0.20)

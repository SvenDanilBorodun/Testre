#!/usr/bin/env python3
"""A pinned destination stores WHERE, and re-asks HOW HIGH on every run.

Audit `docs/plans/audit-G10-G12.md` §9.2(a) / §9.3 answer (2), owner-approved.

A camera pin's z was baked into TWO carriers — the saved workflow's Z field and
``WorkflowManager._persisted_destinations``, which only ``set_destination``
writes and nothing ever clears — while ``EDUBOTICS_FORCE_RECALIBRATION`` ships 1
and re-draws the table plane at the start of every lesson. Measured (§9.2(a)): a
persisted plane-evaluated pin carries ~2.3x the re-calibration variance of a
persisted scalar at a 12 cm lever, growing linearly with it. So the pin keeps
(x, y) and its height is re-asked from the plane in force, exactly as
``perception_blocks._attach_named_world`` already does for a named object.

PROVENANCE decides, not geometry: a MEASURED height („Position merken", FK)
survives verbatim. These tests drive both kinds through the REAL
``WorkflowManager``, the REAL ``IKSolver`` and the REAL ``SimArm``, and read the
height the arm actually reached.
"""

from __future__ import annotations

import json
import math
import time
import types

import numpy as np
import pytest

from physical_ai_server.workflow.handlers import destinations as dst
from physical_ai_server.workflow.handlers import motion as M
from physical_ai_server.workflow.ik_solver import IKSolver
from physical_ai_server.workflow.object_catalog import fixed_catalog
from physical_ai_server.workflow.sim_arm import SimArm
from physical_ai_server.workflow.sim_world import SimWorld
from physical_ai_server.workflow import trajectory_builder
from physical_ai_server.workflow.workflow_manager import WorkflowManager

# The touch-off writes z_table = a·cx + b·cy + c at the tap CENTROID, so a plane
# tilted about +x through (0.18, 0) has z_table == 0 there.
_CENTROID_X = 0.10
_PIN_X = 0.22       # 12 cm further out — the lever §9.2(a) measured at.
                    # Both inside the OMX reach annulus (0.0415..0.2825 m);
                    # only the LEVER sets the height error, not where it sits.


def _plane(degrees: float) -> tuple[float, float, float]:
    a = math.tan(math.radians(degrees))
    return (a, 0.0, -a * _CENTROID_X)


def _surface_mm(degrees: float, x: float) -> float:
    a, b, c = _plane(degrees)
    return (a * x + b * 0.0 + c) * 1000.0


@pytest.fixture(autouse=True)
def _fast_chunk_pacing(monkeypatch):
    """The published trajectory is paced by its own span; skip the wall clock."""
    state = {'t': 0.0}

    def _monotonic():
        state['t'] += 1000.0
        return state['t']

    monkeypatch.setattr(trajectory_builder, 'time',
                        types.SimpleNamespace(monotonic=_monotonic,
                                              sleep=lambda _s: None))
    yield


def _manager(calib: dict):
    world = SimWorld([])
    arm = SimArm(ik=IKSolver(), objects=[], world=world)
    status: list = []
    mgr = WorkflowManager(
        publisher=arm.publish,
        ik_factory=lambda: IKSolver(),
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
    return mgr, arm, status


def _run(mgr, program, status):
    ok, msg, _ = mgr.start(json.dumps(program), 'wf')
    assert ok, msg
    deadline = time.monotonic() + 20.0
    done = lambda: [e for e in status if isinstance(e, dict) and '_finished' in e]
    while not done() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert done(), 'workflow did not finish'
    return done()[-1]['_finished']


def _pin_then_move(z_field: float):
    """Exactly the bytes a pre-change editor saved: a pin block carrying X/Y/Z,
    then „bewege zu P"."""
    return {'blocks': {'languageVersion': 0, 'blocks': [{
        'type': 'edubotics_destination_pin',
        'fields': {'NAME': 'P', 'X': str(_PIN_X), 'Y': '0.0', 'Z': str(z_field)},
        'next': {'block': {
            'type': 'edubotics_move_to',
            'inputs': {'DESTINATION': {'block': {
                'type': 'edubotics_destination_ref', 'fields': {'NAME': 'P'}}}},
        }},
    }]}}


def _move_ref_only():
    return {'blocks': {'languageVersion': 0, 'blocks': [{
        'type': 'edubotics_move_to',
        'inputs': {'DESTINATION': {'block': {
            'type': 'edubotics_destination_ref', 'fields': {'NAME': 'P'}}}},
    }]}}


# ── the headline: one saved workflow, two lessons, two table planes ─────────

@pytest.mark.parametrize('degrees,expected_mm', [
    (0, 0.0),
    (4, 8.39),
    (8, 16.86),
    (11, 23.33),
])
def test_the_same_saved_pin_descends_to_whichever_plane_is_in_force(
        degrees, expected_mm):
    """The Z field says 0.000 — the value a click on a LEVEL table produced.
    Next lesson the touch-off finds the table tilted, and the run must follow it.
    """
    mgr, arm, status = _manager(
        {'z_table': 0.0, 'table_plane': _plane(degrees)})
    assert _run(mgr, _pin_then_move(0.0), status) == 'finished'
    reached_mm = arm.fk_xyz()[2] * 1000.0
    assert reached_mm == pytest.approx(expected_mm, abs=0.05), (
        f'{degrees}deg: the arm went to {reached_mm:.2f} mm, the local table '
        f'surface is at {_surface_mm(degrees, _PIN_X):.2f} mm')
    # And that IS the surface, not a number that merely differs from the field.
    assert reached_mm == pytest.approx(_surface_mm(degrees, _PIN_X), abs=0.05)


def test_the_baked_field_no_longer_decides_anything_on_a_calibrated_rig():
    """Two workflows whose ONLY difference is the baked Z reach the same height.
    Before, the field was the answer; now it is only the fallback."""
    reached = []
    for baked in (0.0, 0.045, -0.030):
        mgr, arm, status = _manager({'z_table': 0.0, 'table_plane': _plane(8)})
        assert _run(mgr, _pin_then_move(baked), status) == 'finished'
        reached.append(round(arm.fk_xyz()[2] * 1000.0, 2))
    assert reached[0] == pytest.approx(16.86, abs=0.05)
    assert len(set(reached)) == 1, reached


# ── the SERVICE carrier — a click that never runs the pin block ─────────────

def test_a_service_pinned_destination_follows_a_later_touch_off():
    """`_persisted_destinations` is written only by `set_destination` and never
    cleared, so a mid-session „Tisch vermessen" used to leave it stale."""
    mgr, arm, status = _manager({'z_table': 0.0, 'table_plane': _plane(11)})
    # The click happened while the table was still believed level.
    mgr.set_destination('P', _PIN_X, 0.0, 0.0, plane_tracked=True)
    assert _run(mgr, _move_ref_only(), status) == 'finished'
    assert arm.fk_xyz()[2] * 1000.0 == pytest.approx(23.33, abs=0.05)


def test_a_measured_capture_is_NOT_rerouted_through_the_plane():
    """„Position merken" persists an FK reading at that very point — a
    measurement, not a reading off a model. It must survive verbatim."""
    mgr, arm, status = _manager({'z_table': 0.0, 'table_plane': _plane(11)})
    mgr.set_destination('P', _PIN_X, 0.0, 0.060)   # plane_tracked defaults False
    assert _run(mgr, _move_ref_only(), status) == 'finished'
    assert arm.fk_xyz()[2] * 1000.0 == pytest.approx(60.0, abs=0.05)


# ── the fallbacks ──────────────────────────────────────────────────────────

def test_an_uncalibrated_rig_keeps_the_stored_height():
    """No z_table and no plane → the pin keeps the only height anybody gave it.
    NOT a z_table = 0.0 fallback: nothing is invented."""
    mgr, arm, status = _manager({})
    assert _run(mgr, _pin_then_move(0.045), status) == 'finished'
    assert arm.fk_xyz()[2] * 1000.0 == pytest.approx(45.0, abs=0.05)


@pytest.mark.parametrize('from_plane,expected_mm', [
    (True, 4.19),    # the plane evaluated at the pin's own (x, y)
    (False, 0.00),   # the scalar the touch-off measured at the tap centroid
])
def test_the_one_variable_rollback_moves_both_call_sites_and_nothing_else(
        monkeypatch, from_plane, expected_mm):
    """EDUBOTICS_GRASP_Z_FROM_PLANE is the SHARED knob; no second one was
    invented for this change.

    The knob chooses PLANE-vs-SCALAR. The staleness fix is a different axis and
    is live in BOTH positions: the baked Z field of 0.045 decides nothing either
    way, because on a calibrated rig it is a cached reading, not a measurement.

    A 2 deg tilt is used deliberately: with the knob OFF the commanded scalar and
    the still-plane-aware floor guard disagree, and above ~4.76 deg over this
    lever that guard refuses the pin outright — the pre-existing knob-off
    behaviour §9.2(b) describes, not something this change introduced."""
    monkeypatch.setattr(M, 'GRASP_Z_FROM_PLANE', from_plane)
    mgr, arm, status = _manager({'z_table': 0.0, 'table_plane': _plane(2)})
    assert _run(mgr, _pin_then_move(0.045), status) == 'finished'
    assert arm.fk_xyz()[2] * 1000.0 == pytest.approx(expected_mm, abs=0.05)


# ── the pure helper, with literal expectations ─────────────────────────────

def test_resolve_destination_z_is_the_one_rule():
    ctx = types.SimpleNamespace(z_table=0.0, table_plane=_plane(8))
    tracked = {'x': _PIN_X, 'y': 0.0, 'z': 0.0, 'plane_tracked': True}
    measured = {'x': _PIN_X, 'y': 0.0, 'z': 0.0, 'plane_tracked': False}
    unmarked = {'x': _PIN_X, 'y': 0.0, 'z': 0.0}
    assert M.resolve_destination_z(ctx, tracked) * 1000.0 == pytest.approx(
        16.86, abs=0.02)
    assert M.resolve_destination_z(ctx, measured) == 0.0
    # An entry with no flag at all — an old in-memory entry, a hand-built dict —
    # must keep its stored z. Backward-safe by default.
    assert M.resolve_destination_z(ctx, unmarked) == 0.0


def test_resolve_destination_z_never_returns_a_non_finite_height():
    ctx = types.SimpleNamespace(z_table=0.012, table_plane=(float('nan'), 0.0, 0.0))
    entry = {'x': _PIN_X, 'y': 0.0, 'z': 0.031, 'plane_tracked': True}
    got = M.resolve_destination_z(ctx, entry)
    assert math.isfinite(got)
    # A malformed plane falls back to the scalar, the same rule _floor_z_at uses.
    assert got == pytest.approx(0.012)


def test_the_two_block_handlers_disagree_about_provenance_on_purpose():
    class _Ctx:
        def __init__(self):
            self.destinations = {}
            self.z_table = 0.05
            self.table_plane = None
            self.log = lambda _m: None
            self.get_current_pose_xyz = lambda: (0.2, 0.0, 0.187)

    ctx = _Ctx()
    dst.destination_pin(ctx, {'name': 'P', 'x': 0.2, 'y': 0.0, 'z': 0.0})
    dst.destination_current(ctx, {'name': 'M'})
    assert ctx.destinations['P']['plane_tracked'] is True
    assert ctx.destinations['P']['z'] == pytest.approx(0.05)
    assert ctx.destinations['M']['plane_tracked'] is False
    assert ctx.destinations['M']['z'] == pytest.approx(0.187)


def test_the_pin_log_line_reports_the_height_the_run_will_use():
    """A student reading „gespeichert (…, …, 0.045)" and then watching the arm
    descend to 0.050 would have no way to reconcile the two."""
    lines: list[str] = []

    class _Ctx:
        destinations: dict = {}
        z_table = 0.05
        table_plane = None
        log = staticmethod(lines.append)

    dst.destination_pin(_Ctx(), {'name': 'P', 'x': 0.2, 'y': 0.0, 'z': 0.045})
    assert lines == ['Ziel "P" gespeichert (0.200, 0.000, 0.050).']

#!/usr/bin/env python3
"""Pinned destinations: they must reach the SIMULATOR, and carry the right height.

``physical_ai_server.py`` cannot be imported without rclpy, so this file follows
the split ``test_sim_node_wiring.py`` established: parse the SOURCE for the
wiring facts that have no other guard, and drive the equivalent runtime
configuration through the real WorkflowManager / the real motion helper for the
behaviour.

RS-40 — ``_get_or_create_sim_workflow_manager`` passes ``load_destinations=lambda:
{}`` and owns a SEPARATE ``_persisted_destinations``, while ``set_destination`` is
only ever called on the REAL manager. Breakpoints ARE explicitly copied onto the
sim manager twenty lines away; destinations were not. Measured on all 3 profiles:
„bewege zu P" (pinned by service, no pin block) finished on the real manager and
failed in sim with „Unbekanntes Ziel: „P". Bitte das Ziel zuerst in der
Szenen-Kamera anklicken (pinnen)." — advice the student had already followed.

RS-07 (pinned-destination half) — ``mark_destination_callback`` stored the SCALAR
``z_table``, which is the plane evaluated at the touch-off CENTROID, for every pin
wherever it sat. Measured (tap centroid (0.18, 0), z_table 0 there, pin 12 cm
further out):

     tilt   local surface   stored (scalar)   commanded   floor    refused?
      4°       +8.39 mm         0.00 mm       +12.00 mm   +8.39     no
      8°      +16.86 mm         0.00 mm       +12.00 mm  +16.86     YES
     11°      +23.33 mm         0.00 mm       +12.00 mm  +23.33     YES

i.e. below ~7° the grasp height is silently wrong by up to a centimetre, and above
it EVERY pin on the high side of the table is refused with „Zielpunkt liegt unter
der Tischebene." — a message the student cannot act on, because the destination
they pinned is on the table. The mirror image happens on the low side.
"""

from __future__ import annotations

import ast
import json
import math
import pathlib
import time
import types

import pytest

from physical_ai_server.workflow.handlers import motion as M
from physical_ai_server.workflow.workflow_manager import WorkflowManager


_NODE = (pathlib.Path(__file__).resolve().parents[1]
         / 'physical_ai_server' / 'physical_ai_server.py')
_SRC = _NODE.read_text(encoding='utf-8')
_TREE = ast.parse(_SRC)


def _function(name: str) -> ast.FunctionDef:
    for node in ast.walk(_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f'{name} not found in physical_ai_server.py')


def _calls(fn: ast.FunctionDef) -> list[ast.Call]:
    return [n for n in ast.walk(fn) if isinstance(n, ast.Call)]


def _call_names(fn: ast.FunctionDef) -> set[str]:
    out: set[str] = set()
    for call in _calls(fn):
        f = call.func
        if isinstance(f, ast.Name):
            out.add(f.id)
        elif isinstance(f, ast.Attribute):
            out.add(f.attr)
    return out


# ── RS-40 — the sim manager must inherit the pinned destinations ────────────

def test_the_sim_start_branch_copies_the_pinned_destinations():
    """Structural guard: the copy must live next to the breakpoint copy it was
    missing beside, and must go onto the SIM manager (`manager`), not the real
    one (`self.workflow_manager`)."""
    fn = _function('_launch_workflow')
    seeds = [c for c in _calls(fn)
             if isinstance(c.func, ast.Attribute)
             and c.func.attr == 'set_destination'
             and isinstance(c.func.value, ast.Name)
             and c.func.value.id == 'manager']
    assert seeds, (
        'the sim manager never receives the teacher-pinned destinations; '
        'breakpoints are copied a few lines away and destinations were not')


def test_the_sim_seed_reads_from_the_real_manager():
    fn = _function('_launch_workflow')
    reads = [c for c in _calls(fn)
             if isinstance(c.func, ast.Attribute)
             and c.func.attr == 'get_destinations']
    assert reads, 'the seed must read the REAL manager\'s persisted pins'


def test_the_sim_seed_is_best_effort():
    """A diagnostic-grade convenience must never stop a run from starting."""
    fn = _function('_launch_workflow')
    guarded = any(
        isinstance(h, ast.ExceptHandler)
        for t in ast.walk(fn) if isinstance(t, ast.Try)
        for h in t.handlers
        if any(isinstance(c.func, ast.Attribute)
               and c.func.attr == 'set_destination'
               for c in ast.walk(t) if isinstance(c, ast.Call)))
    assert guarded, 'the destination seed must be wrapped in try/except'


def test_a_pinned_destination_resolves_once_the_sim_manager_has_it():
    """Behavioural half: the same reference that failed in sim resolves once the
    pins are copied across, and still fails when they are not."""
    program = json.dumps({'blocks': {'languageVersion': 0, 'blocks': [
        {'type': 'edubotics_move_to', 'inputs': {'DESTINATION': {'block': {
            'type': 'edubotics_destination_ref',
            'fields': {'NAME': 'P'}}}}}]}})

    def _run(seed: bool) -> list[str]:
        real = WorkflowManager(publisher=lambda _p: None,
                               load_destinations=lambda: {})
        real.set_destination('P', 0.15, 0.0, 0.0)
        status: list[dict] = []
        sim = WorkflowManager(publisher=lambda _p: None,
                              load_destinations=lambda: {},
                              emit_status=status.append)
        if seed:
            # Exactly the loop _launch_workflow now runs.
            for name, d in (real.get_destinations() or {}).items():
                sim.set_destination(name, d.get('x', 0.0), d.get('y', 0.0),
                                    d.get('z', 0.0))
        sim.start(program, 'wf-sim')
        deadline = time.monotonic() + 5.0
        while sim.is_running and time.monotonic() < deadline:
            time.sleep(0.01)
        sim.stop()
        return [e.get('error') or '' for e in status
                if isinstance(e, dict) and e.get('phase') == 'error']

    unseeded = _run(seed=False)
    assert any('Unbekanntes Ziel' in e for e in unseeded), unseeded
    seeded = _run(seed=True)
    assert not any('Unbekanntes Ziel' in e for e in seeded), seeded


# ── RS-07 — a pin stores the table height at its OWN (x, y) ────────────────

_CENTROID_X = 0.18


def _plane(degrees: float) -> tuple[float, float, float]:
    """A table tilted `degrees` about +x, with z_table == 0 at the tap centroid
    (which is what solve_table_plane writes: z_table = a·cx + b·cy + c)."""
    a = math.tan(math.radians(degrees))
    return (a, 0.0, -a * _CENTROID_X)


def test_mark_destination_asks_the_shared_helper_for_the_height():
    fn = _function('mark_destination_callback')
    assert 'table_z_at' in _call_names(fn), (
        'the pin height must come from motion.table_z_at — the ONE shared '
        '"how high is the table here" answer')


def test_mark_destination_evaluates_the_plane_at_the_pin_not_the_centroid():
    fn = _function('mark_destination_callback')
    calls = [c for c in _calls(fn)
             if isinstance(c.func, ast.Name) and c.func.id == 'table_z_at']
    assert calls, 'table_z_at is not called'
    args = [a.id for a in calls[0].args if isinstance(a, ast.Name)]
    assert args[-2:] == ['corr_x', 'corr_y'], (
        f'table_z_at must be asked about the CORRECTED pin coordinates, got '
        f'{args}')


def test_mark_destination_reads_the_measured_plane_from_the_yaml():
    fn = _function('mark_destination_callback')
    reads = [c for c in _calls(fn)
             if isinstance(c.func, ast.Attribute) and c.func.attr == 'getNode'
             and c.args and isinstance(c.args[0], ast.Constant)
             and c.args[0].value == 'table_plane']
    assert reads, 'the callback never reads table_plane'


def test_the_response_and_the_persisted_pin_agree():
    """The click feedback the student sees and the value the next run descends
    to must be the same number."""
    fn = _function('mark_destination_callback')
    persisted = [c for c in _calls(fn)
                 if isinstance(c.func, ast.Attribute)
                 and c.func.attr == 'set_destination']
    assert persisted, 'the pin is not persisted'
    names = [a.id for a in persisted[0].args if isinstance(a, ast.Name)]
    assert names[-1] == 'pin_z', names
    assigned = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Attribute) and t.attr == 'world_z'
                        for t in n.targets)]
    assert any(isinstance(a.value, ast.Name) and a.value.id == 'pin_z'
               for a in assigned), (
        'response.world_z must carry the same pin_z that is persisted')


def test_no_second_rollback_knob_was_invented():
    """The rollback is motion's single EDUBOTICS_GRASP_Z_FROM_PLANE, honoured
    inside table_z_at. A second knob here would be a second thing to forward
    through two composes and keep in lockstep."""
    fn = _function('mark_destination_callback')
    env_reads = [c for c in _calls(fn)
                 if isinstance(c.func, ast.Attribute)
                 and c.func.attr in ('getenv', 'get')
                 and any(isinstance(a, ast.Constant)
                         and isinstance(a.value, str)
                         and a.value.startswith('EDUBOTICS_')
                         for a in c.args)]
    assert env_reads == [], 'the callback must not read its own env knob'


def test_capture_pose_does_not_reroute_through_the_plane():
    """„Position merken" persists a MEASURED FK z at that very point, which is
    already the right answer — snapping it to a plane would replace a
    measurement with a model."""
    fn = _function('capture_pose_callback')
    assert 'table_z_at' not in _call_names(fn)


@pytest.mark.parametrize('degrees,pin_x,expected_mm', [
    (0, 0.30, 0.0),
    (4, 0.30, 8.39),
    (8, 0.30, 16.86),
    (11, 0.30, 23.33),
    (4, 0.06, -8.39),     # the low side of the table — the mirror image
    (8, 0.06, -16.86),
])
def test_the_helper_follows_the_plane_at_the_pin(degrees, pin_x, expected_mm):
    holder = types.SimpleNamespace(table_plane=_plane(degrees), z_table=0.0)
    got_mm = M.table_z_at(holder, pin_x, 0.0) * 1000.0
    assert got_mm == pytest.approx(expected_mm, abs=0.02), (
        f'{degrees}° at x={pin_x}: expected {expected_mm} mm, got {got_mm}')


def test_an_untilted_table_is_byte_identical_to_the_scalar():
    """Every rig without a measured plane — and every old extrinsic YAML —
    must behave exactly as before."""
    holder = types.SimpleNamespace(table_plane=None, z_table=0.0)
    assert M.table_z_at(holder, 0.30, 0.0) == 0.0
    holder = types.SimpleNamespace(table_plane=None, z_table=0.015)
    assert M.table_z_at(holder, 0.30, 0.0) == 0.015


def test_a_malformed_plane_falls_back_to_the_scalar_rather_than_failing_open():
    for bad in ('nonsense', (1.0,), (float('nan'), 0.0, 0.0), ()):
        holder = types.SimpleNamespace(table_plane=bad, z_table=0.015)
        assert M.table_z_at(holder, 0.30, 0.0) == 0.015, bad


def test_no_table_height_at_all_is_reported_as_none():
    """The callback keeps its own „Tisch vermessen" refusal for this case."""
    holder = types.SimpleNamespace(table_plane=None, z_table=None)
    assert M.table_z_at(holder, 0.30, 0.0) is None

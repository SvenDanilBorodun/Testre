#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""B8 — out-of-reach sentences in the simulator, and for a Ziel or a point.

Three places raised the rig's wording without ``motion.sim_greifbereich_de``:
the composite „Greife" out of reach, the no-approach-room skip of
``_solve_grasp_and_approach`` and ``_solve_or_raise``. The last one was also
what „bewege zu" / „aufnehmen" / „ablegen bei" said for an unreachable ZIEL —
„bitte das Objekt in den markierten Greifbereich legen", naming neither the
Ziel nor the fix. Measured at 47aea6fa on omx_full and v1_studio, rig and sim.

One profile per ``ik_backend`` of the registry.
"""

from __future__ import annotations

import pytest

from physical_ai_server import robot_profiles as rp
from physical_ai_server.workflow.handlers import motion as M
from rig_emulation import B, Rig, cube, program, ref
from test_rs_impl_b import Ctx, band_xyz

WEIT = {'name': 'Weit', 'kind': 'pin', 'x': 0.60, 'y': 0.0, 'z': 0.0}
_ZIEL_RIG = ('Das Ziel „Weit“ liegt außerhalb des Arbeitsbereichs — bitte das Ziel neu '
             'setzen, nicht zu nah am Roboter und nicht zu weit weg.')
_ZIEL_SIM = ('Das Ziel „Weit“ liegt außerhalb des Arbeitsbereichs — bitte das Ziel auf '
             'dem Sim-Tisch im Greifbereich neu setzen.')
_POINT = ('Dieser Punkt liegt außerhalb des Arbeitsbereichs — bitte einen Punkt im '
          'Greifbereich wählen, nicht zu nah am Roboter und nicht zu weit weg.')


def _one_profile_per_ik_backend():
    seen = {}
    for pid, prof in rp.ROBOT_PROFILES.items():
        seen.setdefault(prof.ik_backend, pid)
    return sorted(seen.values())


PROFILES = _one_profile_per_ik_backend()


@pytest.mark.parametrize('pid', PROFILES)
def test_greife_out_of_reach_in_the_simulator_names_the_sim_table(monkeypatch, pid):
    rig = Rig(monkeypatch, pid, objects=[cube(0.60, 0.0)], simulated=True)
    phase = rig.run(program(B('edubotics_grasp_object', fields={'OBJECT_TYPE': 'wuerfel'})))
    assert phase == 'error', (phase, rig.logs()[-4:])
    err = rig.errors()[-1]
    assert 'außerhalb des Greifbereichs' in err or 'zu weit weg' in err, err
    assert 'auf dem Sim-Tisch' in err, err
    assert 'markierten' not in err, err


@pytest.mark.parametrize('pid', PROFILES)
@pytest.mark.parametrize('simulated', [False, True], ids=['rig', 'sim'])
@pytest.mark.parametrize('block', ['edubotics_move_to', 'edubotics_pickup', 'edubotics_drop_at'])
def test_an_unreachable_ziel_is_named(monkeypatch, pid, simulated, block):
    socket = {'edubotics_move_to': 'DESTINATION', 'edubotics_pickup': 'TARGET',
              'edubotics_drop_at': 'DESTINATION'}[block]
    rig = Rig(monkeypatch, pid, simulated=simulated)
    phase = rig.run(program(B(block, inputs={socket: ref('Weit')}), destinations=[WEIT]))
    assert phase == 'error', (phase, rig.logs()[-4:])
    err = rig.errors()[-1]
    assert err == (_ZIEL_SIM if simulated else _ZIEL_RIG), err
    assert 'Objekt' not in err


@pytest.mark.parametrize('pid', PROFILES)
@pytest.mark.parametrize('simulated', [False, True], ids=['rig', 'sim'])
def test_an_unreachable_point_says_dieser_punkt(monkeypatch, pid, simulated):
    """The shape a text program's point arrives in: a list (review mi-11)."""
    rig = Rig(monkeypatch, pid, simulated=simulated)
    ctx = rig.make_ctx(simulated=simulated)
    with pytest.raises(M.WorkflowError) as exc:
        M.move_to(ctx, {'destination': [0.6, 0.0, 0.05]})
    assert str(exc.value) == _POINT
    with pytest.raises(M.WorkflowError) as exc:
        M.pickup(ctx, {'target': (0.6, 0.0, 0.05)})
    assert str(exc.value) == _POINT


@pytest.mark.parametrize('simulated', [True, False], ids=['sim', 'rig'])
def test_the_no_room_sentence_is_worded_for_the_simulator(monkeypatch, simulated):
    """review-3 MJ-C: ``sim_greifbereich_de`` replaces exactly „in den markierten
    Greifbereich", so the old „… in die Mitte des markierten Greifbereichs legen."
    reached the simulator unchanged."""
    ctx = Ctx('omx_full')
    ctx.simulated = simulated
    monkeypatch.setattr(M, '_max_reachable_rise', lambda *a, **k: (0.0, None))
    with pytest.raises(M.GraspSkip) as exc:
        M._solve_grasp_and_approach(ctx, band_xyz(ctx), M.DEFAULT_APPROACH_HEIGHT_M)
    text = str(exc.value)
    assert text.startswith('Kein Platz, um von oben anzufahren'), text
    if simulated:
        assert 'Sim-Tisch' in text and 'markierten' not in text, text
    else:
        assert 'markierten Greifbereich' in text, text


@pytest.mark.parametrize('simulated', [True, False], ids=['sim', 'rig'])
def test_an_unreachable_object_keeps_the_object_sentence(simulated):
    """``_solve_or_raise`` with no caller sentence (the composite „Greife" path)
    keeps its object sentence, worded for the mode."""
    ctx = Ctx('omx_full')
    ctx.simulated = simulated
    with pytest.raises(M.WorkflowError) as exc:
        M._solve_or_raise(ctx, (0.6, 0.0, 0.0))
    text = str(exc.value)
    assert text.startswith('Position außerhalb des Arbeitsbereichs — bitte das Objekt'), text
    assert ('auf dem Sim-Tisch' in text) == simulated, text
    assert ('markierten' in text) != simulated, text

"""One sentence for an unknown Ziel, and the simulator's own words (spec D4:
F22, N5, N10).

An unknown Ziel used to read „Unbekanntes Ziel: „X". Bitte … pinnen" from the
Blockly reference block and the bare „Unbekanntes Ziel: X" from
``robot.move_to("X")`` — two sentences for one fault, neither saying where a
Ziel is made. In the simulator the advice named the scene camera, which the
simulator does not have; the find-failure wording promised a „markierten
Greifbereich" there is none of.
"""

from __future__ import annotations

import pytest

from physical_ai_server.workflow.handlers import destinations as dst
from physical_ai_server.workflow.handlers import motion as M
from physical_ai_server.workflow.handlers import perception_blocks as pb
from physical_ai_server.workflow.workflow_manager import WorkflowContext

from rig_emulation import B, Rig, chain, program, var, vget, vset

RIG = ('Das Ziel „X" gibt es in diesem Programm nicht — lege es in der Sammlung '
       'unter „Ziele" an (Vormachen oder Klick in die Szenen-Kamera).')
SIM = ('Das Ziel „X" gibt es in diesem Programm nicht — lege es in der Sammlung '
       'unter „Ziele" an (im Simulator: auf den Tisch tippen).')


def _ctx(simulated, language=None):
    ctx = WorkflowContext(publisher=lambda _p: None, simulated=simulated,
                          code_language=language)
    ctx.last_full_joints = [0.0, -1.5707963, 1.5707963, 0.0, 0.0, 0.8]
    return ctx


@pytest.mark.parametrize('simulated,expected', [(False, RIG), (True, SIM)])
def test_unknown_destination_one_sentence_both_paths(simulated, expected):
    """The Blockly reference block and a text program's ``move_to("X")`` say
    the SAME sentence (``motion.unknown_destination_de``)."""
    assert M.unknown_destination_de(_ctx(simulated), 'X') == expected
    with pytest.raises(M.WorkflowError) as blockly:
        dst.destination_ref(_ctx(simulated), {'name': 'X'})
    assert str(blockly.value) == expected
    with pytest.raises(M.WorkflowError) as code:
        M.move_to(_ctx(simulated, 'python'), {'destination': 'X'})
    assert str(code.value) == expected
    with pytest.raises(M.WorkflowError) as drop:
        M.drop_at(_ctx(simulated, 'java'), {'destination': 'X'})
    assert str(drop.value) == expected


def test_sim_wording_names_no_camera(monkeypatch):
    """N5 / N10: in the simulator no sentence sends the student to a camera, and
    a move that got no Greifziel names the Sim-Tisch's Greifbereich."""
    sim = _ctx(True)
    assert 'Kamera' not in M.unknown_destination_de(sim, 'X')
    with pytest.raises(M.WorkflowError) as unpinned:
        dst.destination_pin(sim, {'name': 'A', 'x': '—', 'y': '—', 'z': '—'})
    assert 'Kamera' not in str(unpinned.value)
    assert 'Simulator' in str(unpinned.value)
    # The rig keeps its camera instruction.
    with pytest.raises(M.WorkflowError) as rig_unpinned:
        dst.destination_pin(_ctx(False), {'name': 'A', 'x': '—', 'y': '—', 'z': '—'})
    assert 'Szenen-Kamera' in str(rig_unpinned.value)

    # „finde" with nothing on the Sim-Tisch, then „fahre über" the empty
    # result: the reason the move reports names the Sim-Tisch, no camera, no
    # „markierten" area.
    rig = Rig(monkeypatch, 'omx_full', objects=[], simulated=True)
    payload = program(chain(vset('Z', B('edubotics_find_object', fields={'OBJECT_TYPE': 'wuerfel'})),
                            B('edubotics_move_above', inputs={'ZIEL': vget('Z')})),
                      variables=[var('Z')])
    assert rig.run(payload) == 'error'
    err = rig.errors()[-1]
    assert 'Greifbereich auf dem Sim-Tisch' in err
    assert 'markierten' not in err and 'Kamera' not in err


def test_out_of_reach_find_failure_names_the_sim_tisch(monkeypatch):
    """N10: an unreachable cube's recorded reason, in the simulator, points at
    the Sim-Tisch's Greifbereich too."""
    rig = Rig(monkeypatch, 'omx_full', objects=[{'type': 'wuerfel', 'x': 0.40, 'y': 0.0, 'yaw': 0.2}],
              simulated=True)
    payload = program(chain(vset('Z', B('edubotics_find_object', fields={'OBJECT_TYPE': 'wuerfel'})),
                            B('edubotics_move_above', inputs={'ZIEL': vget('Z')})),
                      variables=[var('Z')])
    assert rig.run(payload) == 'error'
    err = rig.errors()[-1]
    assert 'markierten Greifbereich' not in err
    assert 'Greifbereich auf dem Sim-Tisch' in err


def test_the_rig_keeps_the_marked_greifbereich():
    """The rig's find-failure wording is unchanged."""
    ctx = _ctx(False)
    pb._note_find_failure(ctx, 'Kein „Würfel" sichtbar — bitte das Objekt in den '
                               'markierten Greifbereich legen.')
    assert ctx.last_find_failure.endswith('in den markierten Greifbereich legen.')

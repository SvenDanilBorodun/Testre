"""Every promising sentence runs its promise (spec principle 3): what a block,
its tooltip or a refusal tells the student is what the robot does.

Driven through the real manager, interpreter, handlers and solvers on the
simulator (``rig_emulation.Rig``), the camera-dependent cases on a CALIBRATED
RIG EMULATION (``Rig(simulated=False)`` with a perfect table projection — on a
real rig a person can put a cube back, and the reclaim must see it). Grasps are
counted from the direct emit trace (``Rig.grasps``), never from a summary.
"""

from __future__ import annotations

import pytest

from physical_ai_server.workflow.handlers import motion as M
from physical_ai_server.workflow.handlers import perception_blocks as pb

from daten_timeout import per_test_time_limit  # noqa: F401 — a hang fails, never stalls
from rig_emulation import (ABLAGE, B, PICK, Rig, chain, cube, drop, forever, grasp,
                           log, num, program, ref, sees, text, var, vget, vset, wait)

TEST_TIMEOUT_S = 180.0


def _find(t='wuerfel'):
    return B('edubotics_find_object', fields={'OBJECT_TYPE': t})


# ── D2: a table Ziel is approached from above (Q3, F21) ────────────────────

@pytest.mark.parametrize('pid', ['omx_full', 'edu1_studio'])
def test_table_ziel_is_approached_from_above(monkeypatch, pid):
    """„bewege zu <Tisch-Ziel>" with a cube in the jaws stops ABOVE the Ziel at
    the release height: the held cube's bottom never goes below the table (it
    used to be pressed ~15 mm into it)."""
    rig = Rig(monkeypatch, pid, objects=[cube(*PICK[0])], simulated=True)
    trace = rig.trace_waypoints()
    payload = program(chain(grasp(), B('edubotics_move_to', inputs={'DESTINATION': ref('Ablage')}),
                            B('edubotics_open_gripper')), destinations=[ABLAGE])
    assert rig.run(payload) == 'finished', rig.errors()
    held = [xyz[2] for xyz, key in trace if key is not None]
    assert held, 'the cube was never held'
    capture_z = held[0]          # at capture the cube's bottom is on the table
    bottoms = [z - capture_z for z in held]
    assert min(bottoms) >= -1e-6
    # And the release happens at the hover above Ablage, not on the table.
    released_at = [xyz for xyz, key in trace if key is None][-1]
    assert released_at[2] - capture_z >= 0.02


def _hover_z(monkeypatch, pid):
    """EE height „fahre über <Ziel>" reaches for the first cube (reference)."""
    rig = Rig(monkeypatch, pid, objects=[cube(*PICK[0])], simulated=True)
    payload = program(chain(vset('Z', _find()),
                            B('edubotics_move_above', inputs={'ZIEL': vget('Z')})),
                      variables=[var('Z')])
    assert rig.run(payload) == 'finished', rig.errors()
    return rig.ee_z()


@pytest.mark.parametrize('pid', ['omx_full', 'edu1_studio'])
def test_descend_without_hover_never_swings_low(monkeypatch, pid):
    """F21: „senke auf <Ziel>" from elsewhere first goes to the hover, then
    straight down — the arm never travels sideways below the hover."""
    hover = _hover_z(monkeypatch, pid)
    rig = Rig(monkeypatch, pid, objects=[cube(*PICK[0])], simulated=True)
    trace = rig.trace_waypoints()
    payload = program(chain(vset('Z', _find()),
                            B('edubotics_descend_to', inputs={'ZIEL': vget('Z')})),
                      variables=[var('Z')])
    assert rig.run(payload) == 'finished', rig.errors()
    x, y = PICK[0]
    far = [xyz[2] for xyz, _k in trace
           if ((xyz[0] - x) ** 2 + (xyz[1] - y) ** 2) ** 0.5 > 0.005]
    assert far, 'no transit was published'
    assert min(far) >= hover - 0.005

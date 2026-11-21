"""The tool axis is a per-solver frame convention.

The OMX's FK points the tool along local +x, the V1's along +z, so nothing may
assume one arm's column for the other: each solver declares its own
``approach_axis_local``, and the code that needs the tool direction
(``motion``'s fingertip along the tool, ``home_planner``'s Position floor
check) asks it rather than assumes.
"""

from __future__ import annotations

import pytest

from physical_ai_server.workflow.ik_solver import IKSolver
from physical_ai_server.workflow.v1_ik import V1IKSolver

# (solver, a reachable strict-vertical target, expected local axis)
_ARMS = [
    (IKSolver, (0.18, 0.0, 0.02), (1.0, 0.0, 0.0)),
    (V1IKSolver, (0.18, 0.0, 0.02), (0.0, 0.0, 1.0)),
]


@pytest.mark.parametrize('cls,target,axis', _ARMS)
def test_each_solver_declares_its_own_tool_axis(cls, target, axis):
    assert cls().approach_axis_local == axis

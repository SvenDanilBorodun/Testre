"""The touch-off verticality gate, and the frame convention it depends on.

„Tisch vermessen" records the FK end-effector z as the table height, which is
only self-consistent if the student taps with the tool pointing STRAIGHT DOWN —
so each tap is gated on the angle between the tool axis and base −z.

WHICH axis that is, is a per-solver frame convention. The gate hardcoded the OMX
answer (FK rotation column 0) and therefore read 90° on both Feetech arms, whose
FK frames point the tool along −z and +z. At a 12° limit that rejected EVERY
correct vertical tap with „Greifer steht schräg (90°)" — the touch-off simply
could not be completed, and with no touch-off there is no ``z_table``, so every
grasp refuses in German. This file pins the fix from both ends: each solver
declares its own axis, and the gate asks rather than assumes.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from physical_ai_server.workflow.calibration_manager import (
    CalibrationManager,
    TABLE_TOUCH_MAX_TILT_DEG,
)
from physical_ai_server.workflow.edu1_ik import Edu1IKSolver
from physical_ai_server.workflow.edu6_ik import Edu6IKSolver
from physical_ai_server.workflow.ik_solver import IKSolver
import physical_ai_server.workflow.calibration_manager as cm


@pytest.fixture(autouse=True)
def _calib_dir(monkeypatch, tmp_path):
    """Constructing a manager creates CALIB_DIR; the default is a container
    path. Same redirect the other calibration suites use."""
    monkeypatch.setattr(cm, 'CALIB_DIR', tmp_path)

# (solver, a reachable strict-vertical target, expected local axis)
_ARMS = [
    (IKSolver, (0.18, 0.0, 0.02), (1.0, 0.0, 0.0)),
    (Edu6IKSolver, (0.14, 0.0, 0.02), (0.0, 0.0, -1.0)),
    (Edu1IKSolver, (0.18, 0.0, 0.02), (0.0, 0.0, 1.0)),
]


@pytest.mark.parametrize('cls,target,axis', _ARMS)
def test_each_solver_declares_its_own_tool_axis(cls, target, axis):
    assert cls().approach_axis_local == axis


@pytest.mark.parametrize('cls,target,axis', _ARMS)
def test_a_vertical_grasp_pose_reads_zero_tilt_on_every_arm(cls, target, axis):
    """THE regression. Every solve() result is strict-vertical by construction,
    so the gate must read ~0° for all three — not 0° for one and 90° for two."""
    ik = cls()
    q = ik.solve(target)
    assert q is not None, f'{cls.__name__} cannot reach its own test target'
    rot, _t = ik.fk(q)
    tilt = CalibrationManager._approach_tilt_deg(rot, ik.approach_axis_local)
    assert tilt is not None
    assert tilt == pytest.approx(0.0, abs=0.05), (
        f'{cls.__name__} reads {tilt:.1f}° at a strictly vertical pose')
    assert tilt < TABLE_TOUCH_MAX_TILT_DEG


@pytest.mark.parametrize('cls,target,axis', _ARMS)
def test_the_old_hardcoded_column_zero_would_still_fail_two_of_them(cls, target, axis):
    """Keeps the bug legible: column 0 is the OMX's answer and nobody else's.
    If a future refactor 'simplifies' back to it, this is what says why not."""
    ik = cls()
    rot, _t = ik.fk(ik.solve(target))
    naive = CalibrationManager._approach_tilt_deg(rot, (1.0, 0.0, 0.0))
    if cls is IKSolver:
        assert naive == pytest.approx(0.0, abs=0.05)
    else:
        assert naive == pytest.approx(90.0, abs=0.5)


def test_a_genuinely_tilted_tap_is_still_rejected():
    """The gate must not have been widened into uselessness by the fix."""
    ik = Edu1IKSolver()
    rot, _t = ik.fk(ik.solve((0.18, 0.0, 0.02)))
    twenty = np.array([[math.cos(0.35), 0.0, math.sin(0.35)],
                       [0.0, 1.0, 0.0],
                       [-math.sin(0.35), 0.0, math.cos(0.35)]])
    tilt = CalibrationManager._approach_tilt_deg(twenty @ rot,
                                                 ik.approach_axis_local)
    assert tilt == pytest.approx(math.degrees(0.35), abs=0.05)
    assert tilt > TABLE_TOUCH_MAX_TILT_DEG


def test_the_default_axis_is_the_omx_one_so_old_callers_are_unchanged():
    """`_approach_tilt_deg` keeps a default, and a manager built with no
    provider resolves to it — the capability probe constructs a bare one."""
    rot, _t = IKSolver().fk(IKSolver().solve((0.18, 0.0, 0.02)))
    assert (CalibrationManager._approach_tilt_deg(rot)
            == pytest.approx(CalibrationManager._approach_tilt_deg(rot, (1.0, 0.0, 0.0))))
    assert CalibrationManager()._approach_axis_local() == (1.0, 0.0, 0.0)


@pytest.mark.parametrize('bad', [
    None, (), (1.0, 0.0), (1.0, 0.0, 0.0, 0.0), ('x', 'y', 'z'),
    (float('nan'), 0.0, 0.0), (0.0, 0.0, float('inf')), 42,
])
def test_an_unusable_provider_answer_falls_back_to_the_omx_axis(bad):
    """The fallback direction is the SAFE one: it is what this gate did for its
    whole life, so a broken provider degrades to the old behaviour rather than
    to a random axis that would pass or fail taps at random."""
    mgr = CalibrationManager(get_approach_axis=lambda: bad)
    assert mgr._approach_axis_local() == (1.0, 0.0, 0.0)


def test_a_raising_provider_never_fails_the_capture():
    def boom():
        raise RuntimeError('no solver yet')
    assert CalibrationManager(get_approach_axis=boom)._approach_axis_local() \
        == (1.0, 0.0, 0.0)


def test_a_malformed_rotation_is_unknown_not_vertical():
    assert CalibrationManager._approach_tilt_deg(np.zeros((2, 2))) is None
    assert CalibrationManager._approach_tilt_deg(np.zeros((3, 3))) is None

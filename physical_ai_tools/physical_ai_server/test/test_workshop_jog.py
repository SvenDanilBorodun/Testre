"""Batch 2b — the /workshop/jog target computation (_compute_jog_target +
_jog_solve_floor). Extracted by ``ast`` and exec'd onto a stub self using the
REAL closed-form IKSolver, so the clamp/refuse logic is exercised end-to-end
without ROS. Verifies out-of-range is REFUSED (never silently clamped into a
wall), pitch/yaw is rejected in Cartesian mode, and the workspace floor holds.
"""

from __future__ import annotations

import ast
import textwrap
import types
from pathlib import Path

import pytest

from physical_ai_server.workflow.handlers.motion import WorkflowError
from physical_ai_server.workflow.ik_solver import IKSolver

_SERVER_PY = (
    Path(__file__).resolve().parents[1]
    / 'physical_ai_server' / 'physical_ai_server.py'
)


def _load(names):
    source = _SERVER_PY.read_text(encoding='utf-8')
    tree = ast.parse(source)
    ns: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            src = textwrap.dedent(ast.get_source_segment(source, node))
            exec(compile(src, str(_SERVER_PY), 'exec'), ns)  # noqa: S102
    return {n: ns[n] for n in names}


_fns = _load(['_compute_jog_target', '_jog_solve_floor'])


# HOME-ish live joint vector [j1..j5, grip]; NOT all-zero (a real pose).
_HOME = [0.0, -1.5, 1.5, 0.0, 0.0, 0.0]


class _Stub:
    def __init__(self, calib=None):
        self._calib = calib or {}
        self._jog_solve_floor = types.MethodType(_fns['_jog_solve_floor'], self)

    def _build_ik_solver(self):
        return IKSolver()

    def _load_workflow_calibration(self):
        return self._calib


def _req(mode='joint', index=0, delta=0.0, tx=0.0, ty=0.0, tz=0.0):
    return types.SimpleNamespace(
        mode=mode, index=index, delta=delta,
        target_x=tx, target_y=ty, target_z=tz,
    )


def _compute(stub, request, joints=None):
    return _fns['_compute_jog_target'](stub, request.mode, request, joints or _HOME)


# --- joint mode ------------------------------------------------------------

def test_joint_nudge_within_limits():
    q_end, world = _compute(_Stub(), _req('joint', index=1, delta=0.1))
    assert q_end[1] == pytest.approx(-1.4)
    assert q_end[5] == pytest.approx(0.0)   # gripper carried
    assert len(q_end) == 6 and len(world) == 3


def test_joint_over_limit_refused_not_clamped():
    # j2 (index 1) upper limit ≈ 1.658; -1.5 + 4.0 = 2.5 → REFUSE (don't clamp).
    with pytest.raises(WorkflowError) as e:
        _compute(_Stub(), _req('joint', index=1, delta=4.0))
    assert 'Gelenk 2' in str(e.value)


def test_joint_gripper_band():
    # gripper band is [GRIPPER_CLOSED_RAD, GRIPPER_OPEN_RAD] = [-0.5, 0.8].
    q_end, _w = _compute(_Stub(), _req('joint', index=5, delta=0.5))
    assert q_end[5] == pytest.approx(0.5)
    with pytest.raises(WorkflowError) as e:
        _compute(_Stub(), _req('joint', index=5, delta=1.0))   # 1.0 > 0.8
    assert 'Greifer' in str(e.value)


def test_joint_bad_index_refused():
    with pytest.raises(WorkflowError) as e:
        _compute(_Stub(), _req('joint', index=9, delta=0.1))
    assert 'Gelenk-Index' in str(e.value)


# --- cartesian mode --------------------------------------------------------

def test_cartesian_roll_index_3():
    q_end, _w = _compute(_Stub(), _req('cartesian', index=3, delta=0.5))
    assert q_end[4] == pytest.approx(0.5)   # j5 (tool roll)


def test_cartesian_roll_over_limit_refused():
    # j5 limit ±π; 0.0 + 4.0 > π → refuse.
    with pytest.raises(WorkflowError) as e:
        _compute(_Stub(), _req('cartesian', index=3, delta=4.0))
    assert 'Drehung' in str(e.value)


def test_cartesian_pitch_yaw_rejected():
    with pytest.raises(WorkflowError) as e:
        _compute(_Stub(), _req('cartesian', index=4, delta=0.1))
    assert 'Neigung' in str(e.value)


def test_cartesian_xyz_reachable():
    # Home FK ≈ (0.161, -0.0016, 0.147); nudge x by -0.02 → reachable.
    q_end, world = _compute(_Stub(), _req('cartesian', index=0, delta=-0.02))
    assert len(q_end) == 6
    assert world[0] == pytest.approx(0.141, abs=3e-3)


def test_cartesian_xyz_unreachable_refused():
    with pytest.raises(WorkflowError) as e:
        _compute(_Stub(), _req('cartesian', index=0, delta=1.0))   # x → ~1.16 m
    assert 'Arbeitsbereich' in str(e.value)


def test_cartesian_below_floor_refused():
    # z_table = 0.10 → a downward z jog past it refuses "Tischebene".
    stub = _Stub(calib={'z_table': 0.10})
    with pytest.raises(WorkflowError) as e:
        _compute(stub, _req('cartesian', index=2, delta=-0.10))   # z 0.147 → 0.047
    assert 'Tischebene' in str(e.value)


# --- drive_to mode ---------------------------------------------------------

def test_drive_to_reachable():
    q_end, world = _compute(
        _Stub(calib={'z_table': 0.0}), _req('drive_to', tx=0.15, ty=0.0, tz=0.05))
    assert len(q_end) == 6
    assert world == pytest.approx((0.15, 0.0, 0.05), abs=3e-3)


def test_drive_to_unreachable_refused():
    with pytest.raises(WorkflowError) as e:
        _compute(
            _Stub(calib={'z_table': 0.0}), _req('drive_to', tx=0.5, ty=0.5, tz=0.5))
    assert 'Arbeitsbereich' in str(e.value)


def test_drive_to_refused_when_floor_unknown():
    # FIX 5: drive_to requires a measured table floor; an uncalibrated scene
    # (no z_table / table_plane) refuses so the tool can't be driven into the
    # table.
    with pytest.raises(WorkflowError) as e:
        _compute(_Stub(), _req('drive_to', tx=0.15, ty=0.0, tz=0.05))
    assert 'vermessen' in str(e.value)


def test_unknown_mode_refused():
    with pytest.raises(WorkflowError) as e:
        _compute(_Stub(), _req('spin'))
    assert 'Handbetrieb-Modus' in str(e.value)


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))

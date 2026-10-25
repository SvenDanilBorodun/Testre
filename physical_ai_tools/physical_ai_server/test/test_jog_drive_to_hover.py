"""The pin block's „fahre dorthin" (``/workshop/jog`` drive_to) stops where a
program stops over the same table Ziel (owner decision Q11, spec D2 / M4).

It used to drive to the z baked into the block, i.e. onto the table the pin
was clicked on — so the student saw the tool touch down where the program
would hover, and on a re-measured table even below or above it. Now the jog
asks the plane IN FORCE at the pin's (x, y) and adds the program's hover
(``motion.table_ziel_hover``).

``_compute_jog_target`` / ``_jog_solve_floor`` are ast-extracted from the node
and exec'd onto a stub ``self`` with the real solver, like ``test_workshop_jog``.
"""

from __future__ import annotations

import ast
import textwrap
import threading
import types
from pathlib import Path

import pytest

from physical_ai_server import robot_profiles as rp
from physical_ai_server.workflow.handlers import motion as M
from physical_ai_server.workflow.workflow_manager import WorkflowContext

from rig_emulation import fast_pacing

_SERVER_PY = Path(__file__).resolve().parents[1] / 'physical_ai_server' / 'physical_ai_server.py'


def _load(names):
    source = _SERVER_PY.read_text(encoding='utf-8')
    ns: dict = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            exec(compile(textwrap.dedent(ast.get_source_segment(source, node)),  # noqa: S102
                         str(_SERVER_PY), 'exec'), ns)
    return {n: ns[n] for n in names}


_fns = _load(['_compute_jog_target', '_jog_solve_floor'])


class _Stub:
    def __init__(self, calib, notices=None):
        self._calib = calib
        self._jog_solve_floor = types.MethodType(_fns['_jog_solve_floor'], self)
        if notices is not None:
            self._publish_manual_notice = notices.append

    def _build_ik_solver(self):
        return rp.ROBOT_PROFILES['omx_full'].build_ik()

    def _load_workflow_calibration(self):
        return self._calib


def _drive_to(stub, x, y, z):
    req = types.SimpleNamespace(mode='drive_to', index=0, delta=0.0,
                                target_x=x, target_y=y, target_z=z)
    home = list(rp.ROBOT_PROFILES['omx_full'].home_joints_rad) + [0.8]
    return _fns['_compute_jog_target'](stub, 'drive_to', req, home)


def _program_hover(monkeypatch, calib, x, y, stale_z):
    """EE xyz where „bewege zu <pin>" stops in a program under ``calib``."""
    fast_pacing(monkeypatch)
    prof = rp.ROBOT_PROFILES['omx_full']
    ctx = WorkflowContext(
        publisher=lambda _pts: None, ik=prof.build_ik(),
        destinations={'A': {'x': x, 'y': y, 'z': stale_z, 'label': 'A',
                            'plane_tracked': True, 'kind': 'pin'}},
        z_table=calib.get('z_table'), table_plane=calib.get('table_plane'),
        motion_lock=threading.RLock(), num_arm_joints=5,
        home_joints_rad=prof.home_joints_rad, gripper_open_rad=0.8, gripper_closed_rad=-0.5)
    ctx.last_full_joints = list(prof.home_joints_rad) + [0.8]
    M.move_to(ctx, {'destination': 'A'})
    _R, t = prof.build_ik().fk(ctx.last_full_joints[:5])
    return tuple(float(v) for v in t)


def test_fahre_dorthin_stops_at_program_hover_height(monkeypatch):
    """The pin's STORED z (clicked on an older table) is not used: the jog stops
    over the plane in force, at exactly the program's hover height."""
    calib = {'z_table': 0.04, 'table_plane': (0.02, 0.0, 0.035)}
    x, y, stale_z = 0.17, 0.03, 0.0
    _q, world = _drive_to(_Stub(calib), x, y, stale_z)
    expected = _program_hover(monkeypatch, calib, x, y, stale_z)
    plane = 0.02 * x + 0.035
    assert world == pytest.approx(expected, abs=1e-6)
    assert world[2] == pytest.approx(plane + M.DROP_HEIGHT_M, abs=2e-3)
    assert abs(world[2] - stale_z) > 0.05


def test_reduced_hover_warning_reaches_the_notice():
    """At the rim of the reach the hover cannot be reached in full; the helper's
    nameless reduced-height sentence reaches the student as a notice instead of
    raising (the shim's ``log`` resolves ``_publish_manual_notice`` lazily)."""
    notices: list[str] = []
    stub = _Stub({'z_table': 0.04}, notices)
    ik = stub._build_ik_solver()
    # Walk out along +x to the last radius whose table point still solves.
    r = 0.20
    while ik.solve(target_xyz=(r + 0.001, 0.0, 0.04), roll=M.GRASP_ROLL_RAD) is not None:
        r += 0.001
    _q, world = _drive_to(stub, r, 0.0, 0.0)
    assert world[2] - 0.04 < M._APPROACH_WARN_FRAC * M.DROP_HEIGHT_M
    assert len(notices) == 1
    assert notices[0].startswith('Anfahrhöhe über dem Ziel auf ')
    assert notices[0].endswith(' mm reduziert — höher ist hier nicht erreichbar.')


def test_a_stub_without_the_notice_method_still_drives():
    """r3-m7: the shim's log is resolved LAZILY — a node stub without
    ``_publish_manual_notice`` (the other jog tests' stubs) must not break."""
    stub = _Stub({'z_table': 0.04})
    ik = stub._build_ik_solver()
    r = 0.20
    while ik.solve(target_xyz=(r + 0.001, 0.0, 0.04), roll=M.GRASP_ROLL_RAD) is not None:
        r += 0.001
    _q, world = _drive_to(stub, r, 0.0, 0.0)
    assert world[2] >= 0.04 - 1e-6

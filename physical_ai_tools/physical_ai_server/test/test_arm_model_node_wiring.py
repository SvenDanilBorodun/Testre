#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The node's arm models and the real calibration dict (spec 40 S5, S6.1, WP-11).

* ``_build_ik_solver`` returns the REAL robot's arm model: the profile's solver
  in W with the arm check of ``$EDUBOTICS_CALIB_DIR/calibration/arm_check.json``
  and the contract's fingertip offset; ``_build_sim_ik_solver`` a second one
  with the lift and the joint-1 window only; ``_get_or_create_sim_workflow_manager``
  hands the simulator's to ``SimArm`` AND to its manager.
* ``_load_workflow_calibration`` carries the fixture's plane constants: the
  grasp height is the mat's top plus the fingertip offset, by design (D29: the
  touch-off no longer sets it).
* A manual drive's solve that keeps a model miss above ``IK_REFINE_ACCEPT_M``
  tells the student once, in German (D42).

``physical_ai_server.py`` cannot be imported without rclpy, hence ``ast`` +
``exec`` onto stub nodes, as in ``test_preview_node_wiring.py``.
"""

from __future__ import annotations

import ast
import math
import os
import pathlib
import textwrap
import types

import numpy as np
import pytest

from physical_ai_server import robot_profiles as rp
from physical_ai_server.workflow import scene_fixture as SF
from physical_ai_server.workflow.autocal import store
from physical_ai_server.workflow.autocal import texts_de
from physical_ai_server.workflow.ik_solver import IKSolver
from physical_ai_server.workflow.v1_ik import V1IKSolver

_NODE = pathlib.Path(__file__).resolve().parents[1] / 'physical_ai_server' / 'physical_ai_server.py'
_SOURCE = _NODE.read_text(encoding='utf-8')
LIFT = np.array([0.0, 0.0, SF.LIFT_M])
ARM = {'omx_full': 'omx', 'omx_follower': 'omx', 'v1_studio': 'v1'}


def _fns(*names):
    """The named node methods, exec'd from the source (a name the node does not define maps to None)."""
    ns: dict = {'robot_profiles': rp, 'os': os, 'math': math}
    found = {}
    for node in ast.walk(ast.parse(_SOURCE)):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            exec(compile(textwrap.dedent(ast.get_source_segment(_SOURCE, node)), str(_NODE), 'exec'), ns)  # noqa: S102
            found[node.name] = ns[node.name]
    return {n: found.get(n) for n in names}


class _Logger:
    def __init__(self):
        self.lines = []

    def error(self, msg):
        self.lines.append(('error', str(msg)))

    def warning(self, msg):
        self.lines.append(('warning', str(msg)))

    def info(self, msg):
        self.lines.append(('info', str(msg)))


class _Node:
    def __init__(self, profile_id, *names):
        self._arm_profile = rp.ROBOT_PROFILES[profile_id] if profile_id else None
        self.logger = _Logger()
        for name, fn in _fns(*names).items():
            if fn is not None:
                setattr(self, name, types.MethodType(fn, self))

    def has_parameter(self, _name):
        return False

    def get_logger(self):
        return self.logger


_BUILDERS = ('_build_profile_ik', '_build_ik_solver', '_build_sim_ik_solver')


def _arm_check(profile_id):
    t = math.radians(0.3)
    return {
        'v': 1, 'fixture_id': SF.FIXTURE_ID, 'contract_hash': SF.contract_hash(), 'profile_id': profile_id,
        'camera_profile_id': 'tower_v1', 'image_size': [1920, 1080], 'created_utc': '2026-11-20T08:00:00Z',
        'kb': {'f': 1031.2, 'aspect': 1.001, 'cx': 961.0, 'cy': 538.2, 'k1': 0.05, 'k2': -0.04},
        'print_scale': 1.0, 'joint_offsets_rad': [0.0, 0.010, -0.020, 0.015, 0.0],
        'axis_tilts_rad': [t, 0.0, -t, t, t, -t], 'tilt': [0.004, -0.003],
        'mat_pose': [0.0, 0.0, 0.0], 'camera_pose_mat': {'rvec': [2.1, 0.3, -0.2], 't': [0.01, 0.2, 0.55]},
        'lens_w': [-0.1, 0.1, 0.575], 'fixture_xy_mat': [[0.0, 0.0]] * 8, 'fixture_px': [[900.0, 400.0]] * 8,
        'mount': {'rvec': [0.0, 0.1, 0.0], 't': [0.0, -0.095, -0.11]}, 'droop_envelope_rad': [0.0, 0.02, 0.01, 0.004, 0.0],
        'tower_pose': [0.0, 0.0, 0.0], 'tower_fit_rms_m': 0.0002, 'target_removed': False,
        'droop_warning': None, 'notes': [],
        'quality': {'poses_used': 20, 'rms_px': 0.21, 'holdout_rms_px': 0.4, 'holdout_p95_mm': 0.4,
                    'field_tags': 14, 'sharpness': 80.0},
    }


def _base(profile_id):
    return V1IKSolver() if ARM[profile_id] == 'v1' else IKSolver()


Q = [0.2, 0.9, 1.1, -0.4, 0.3]


# ══════════════════════════════════════════════════════════════════════════
# the two arm models
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('profile_id', ('omx_full', 'v1_studio'))
def test_the_real_solver_is_the_arm_model_with_the_arm_check_and_the_fingertip(profile_id, tmp_path, monkeypatch):
    monkeypatch.setenv('EDUBOTICS_CALIB_DIR', str(tmp_path))
    node = _Node(profile_id, *_BUILDERS)
    ik = node._build_ik_solver()
    assert ik is not None, node.logger.lines
    assert node._build_ik_solver() is ik                         # built once
    base = _base(profile_id)
    # no arm check: the base solver raised by the plate
    assert np.abs(ik.fk(Q)[1] - (base.fk(Q)[1] + LIFT)).max() < 1e-12
    assert ik.tool_tip_below_ee_m == SF.FINGERTIP_BELOW_EE_M[ARM[profile_id]]
    lo, hi = SF.ARMS[ARM[profile_id]]['joint1_window_deg']
    assert ik.joint_limits[0][0] >= math.radians(lo) - 1e-4 and ik.joint_limits[0][1] <= math.radians(hi) + 1e-4
    # the arm check of this rig takes effect at the next call, same object
    store.Store(str(tmp_path)).write(_arm_check(profile_id))
    moved = float(np.linalg.norm(ik.fk(Q)[1] - (base.fk(Q)[1] + LIFT)))
    assert moved > 0.001, moved


@pytest.mark.parametrize('profile_id', ('omx_full', 'v1_studio'))
def test_the_simulator_has_its_own_model_with_lift_and_window_only(profile_id, tmp_path, monkeypatch):
    monkeypatch.setenv('EDUBOTICS_CALIB_DIR', str(tmp_path))
    store.Store(str(tmp_path)).write(_arm_check(profile_id))      # the rig's arm check exists
    node = _Node(profile_id, *_BUILDERS)
    assert getattr(node, '_build_sim_ik_solver', None) is not None, 'the node builds no simulator arm model'
    sim = node._build_sim_ik_solver()
    assert sim is not None, node.logger.lines
    assert node._build_sim_ik_solver() is sim
    assert sim is not node._build_ik_solver()
    base = _base(profile_id)
    # the simulator never reads the rig's arm check
    assert np.abs(sim.fk(Q)[1] - (base.fk(Q)[1] + LIFT)).max() < 1e-12
    assert sim.tool_tip_below_ee_m == base.tool_tip_below_ee_m
    lo, _hi = SF.ARMS[ARM[profile_id]]['joint1_window_deg']
    assert sim.joint_limits[0][0] >= math.radians(lo) - 1e-4


def test_the_simulator_manager_and_sim_arm_get_the_simulator_model():
    fn = _fns('_get_or_create_sim_workflow_manager')['_get_or_create_sim_workflow_manager']
    real, sim = IKSolver(), IKSolver()
    node = types.SimpleNamespace(
        sim_workflow_manager=None,
        _sim_world=None,
        _sim_objects=[],
        _arm_profile=rp.ROBOT_PROFILES['omx_full'],
        _ensure_sim_publisher=lambda: None,
        _publish_sim_frame=lambda q, scene=None, late_s=0.0: None,
        _build_ik_solver=lambda: real,
        _build_sim_ik_solver=lambda: sim,
        _profile_n=lambda: 5,
        _load_object_catalog_tolerant=lambda: None,
        _load_object_catalog=lambda: None,
        _emit_workflow_status=lambda _payload: None,
        _on_sim_workflow_finished=lambda *_a: None,
        get_logger=_Logger,
    )
    manager = fn(node)
    assert manager is not None
    assert node._sim_arm._ik is sim
    assert manager._ik_factory() is sim
    # a node without the simulator builder (the stub nodes of older tests) keeps working
    node2 = types.SimpleNamespace(**{k: v for k, v in vars(node).items() if k != '_build_sim_ik_solver'})
    node2.sim_workflow_manager = None
    node2._sim_world = None
    assert fn(node2)._ik_factory() is real


# ══════════════════════════════════════════════════════════════════════════
# the real calibration dict: the fixture's plane constants (S5)
# ══════════════════════════════════════════════════════════════════════════

def _legacy_yaml(calib_dir):
    """A touch-off of the legacy calibration: z_table 0.04 and a tilted plane."""
    import cv2
    path = os.path.join(calib_dir, 'scene_handeye.yaml')
    fs = cv2.FileStorage(path, cv2.FILE_STORAGE_WRITE)
    fs.write('transform', np.eye(4))
    fs.write('z_table', 0.04)
    fs.write('board_table_z', 0.0)
    fs.write('table_plane', np.array([[0.01], [0.002], [0.04]]))
    fs.release()


@pytest.mark.parametrize('profile_id', ('omx_full', 'omx_follower', 'v1_studio', None))
def test_the_real_calibration_dict_carries_the_fixtures_plane_constants(profile_id, tmp_path, monkeypatch):
    monkeypatch.setenv('EDUBOTICS_CALIB_DIR', str(tmp_path))
    _legacy_yaml(str(tmp_path))
    node = _Node(profile_id, '_load_workflow_calibration')
    node._get_or_create_calibration_manager = lambda: None
    calib = node._load_workflow_calibration()
    arm = ARM.get(profile_id, 'omx')
    assert calib['board_table_z'] == SF.MAT_THICKNESS_M
    assert calib['z_table'] == SF.MAT_THICKNESS_M + SF.FINGERTIP_BELOW_EE_M[arm]
    assert 'table_plane' in calib and calib['table_plane'] is None


def test_an_arm_the_contract_does_not_know_gets_no_table(tmp_path, monkeypatch):
    """Fail closed: no fixture for that arm, so no grasp height to descend to."""
    monkeypatch.setenv('EDUBOTICS_CALIB_DIR', str(tmp_path))
    node = _Node(None, '_load_workflow_calibration')
    node._arm_profile = types.SimpleNamespace(profile_id='some_new_arm')
    node._get_or_create_calibration_manager = lambda: None
    calib = node._load_workflow_calibration()
    assert {'board_table_z', 'z_table', 'table_plane'}.isdisjoint(calib)


def test_the_constants_hold_without_any_calibration_file(tmp_path, monkeypatch):
    monkeypatch.setenv('EDUBOTICS_CALIB_DIR', str(tmp_path))
    node = _Node('v1_studio', '_load_workflow_calibration')
    node._get_or_create_calibration_manager = lambda: None
    calib = node._load_workflow_calibration()
    assert (calib['board_table_z'], calib['z_table'], calib['table_plane']) == (
        SF.MAT_THICKNESS_M, SF.MAT_THICKNESS_M + SF.FINGERTIP_BELOW_EE_M['v1'], None)


# ══════════════════════════════════════════════════════════════════════════
# the refinement warning of a manual drive (D42)
# ══════════════════════════════════════════════════════════════════════════

def _jog_node(notices):
    node = _Node('v1_studio', '_jog_solve_floor')
    node._publish_manual_notice = notices.append
    z_table = SF.MAT_THICKNESS_M + SF.FINGERTIP_BELOW_EE_M['v1']
    node._load_workflow_calibration = lambda: {'z_table': z_table, 'table_plane': None}
    return node, z_table


def _v1_calibrated():
    from physical_ai_server.workflow import arm_model
    t = math.radians(0.3)
    return arm_model.CalibratedIK(
        V1IKSolver(), offsets=(0.0, 0.010, -0.020, 0.015, 0.0), axis_tilts=(t, 0.0, -t, t, t, -t),
        slopes=(0.004, -0.003), fingertip_m=0.0, joint1_window_deg=SF.ARM_V1['joint1_window_deg'])


def test_a_manual_drive_to_the_sector_edge_says_how_close_it_gets_once():
    notices = []
    node, z_table = _jog_node(notices)
    ik = _v1_calibrated()
    az = math.radians(89.5)
    target = (0.15 * math.cos(az), 0.15 * math.sin(az), z_table + 0.015)
    q = node._jog_solve_floor(ik, target, roll=0.0)
    assert q is not None and len(q) == 5
    miss = float(np.linalg.norm(ik.fk(q)[1] - np.array(target)))
    assert miss > 0.0005
    assert notices == [texts_de.refinement_warning_de('Ziel', miss * 1000.0)]


def test_a_manual_drive_inside_the_sector_says_nothing():
    notices = []
    node, z_table = _jog_node(notices)
    ik = _v1_calibrated()
    az = math.radians(88.0)
    target = (0.15 * math.cos(az), 0.15 * math.sin(az), z_table + 0.015)
    node._jog_solve_floor(ik, target, roll=0.0)
    assert notices == []

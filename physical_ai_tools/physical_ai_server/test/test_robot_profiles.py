"""ArmProfile registry contracts (T2).

Covers resolve() (known/unknown/empty/None), the exact capabilities_json key set
+ values for both OMX profiles, and the NO-DRIFT invariant that locks the
profile's mirrored OMX geometry against the still-authoritative constants in
workflow/handlers/motion.py, workflow/sim_arm.py, the node's _SIM_JOINT_NAMES,
and collision_monitor.SAFE_HOME_ARM — so the seam data can never silently
diverge while the DOF-agnostic refactor is deferred.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from physical_ai_server import robot_profiles as rp

_REPO = Path(__file__).resolve().parents[1]
_SERVER_PY = _REPO / 'physical_ai_server' / 'physical_ai_server.py'
_COLLISION_PY = _REPO / 'physical_ai_server' / 'safety' / 'collision_monitor.py'

_CAP_KEYS = ('recordable', 'editable', 'trainable', 'inferable',
             'roboter_studio', 'has_leader')


def _extract_literal(path, name):
    """ast.literal_eval the RHS of a module-/class-level ``<name> = <literal>``
    in the given file. The files themselves import rclpy and can't be imported
    in CI, so we read the literal statically."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f'{name} not found in {path}')


# --- resolve ---------------------------------------------------------------

@pytest.mark.parametrize('pid', ['omx_full', 'omx_follower'])
def test_resolve_known(pid):
    assert rp.resolve(pid).profile_id == pid


@pytest.mark.parametrize('bad', [None, '', '   ', 'omx', 'garbage', 'OMX_FULL'])
def test_resolve_unknown_falls_back_to_default(bad):
    assert rp.resolve(bad).profile_id == rp.DEFAULT_PROFILE_ID == 'omx_full'


def test_resolve_strips_whitespace():
    assert rp.resolve('  omx_follower  ').profile_id == 'omx_follower'


# --- capabilities_json -----------------------------------------------------

def test_caps_json_exact_full():
    obj = json.loads(rp.capabilities_json(rp.resolve('omx_full')))
    assert set(obj.keys()) == set(_CAP_KEYS)
    assert all(obj[k] is True for k in _CAP_KEYS)


def test_caps_json_exact_follower():
    obj = json.loads(rp.capabilities_json(rp.resolve('omx_follower')))
    assert obj == {
        'recordable': False, 'editable': False, 'trainable': False,
        'inferable': True, 'roboter_studio': True, 'has_leader': False,
    }


def test_caps_json_is_compact():
    # separators=(',', ':') -> no whitespace between tokens
    assert ' ' not in rp.capabilities_json(rp.resolve('omx_full'))


# --- dataset naming anchor -------------------------------------------------

@pytest.mark.parametrize('pid', ['omx_full', 'omx_follower'])
def test_data_robot_type_is_omx_f_for_both(pid):
    assert rp.resolve(pid).data_robot_type == 'omx_f'


# --- no-drift invariant ----------------------------------------------------

def test_no_drift_home_and_gripper_vs_motion():
    from physical_ai_server.workflow.handlers import motion
    for pid in ('omx_full', 'omx_follower'):
        prof = rp.resolve(pid)
        assert prof.home_joints_rad == tuple(motion.HOME_JOINTS_RAD)
        assert prof.gripper_open_rad == motion.GRIPPER_OPEN_RAD
        assert prof.gripper_closed_rad == motion.GRIPPER_CLOSED_RAD


def test_no_drift_joint_names_vs_node_sim_names():
    sim_names = _extract_literal(_SERVER_PY, '_SIM_JOINT_NAMES')
    for pid in ('omx_full', 'omx_follower'):
        assert rp.resolve(pid).joint_names == tuple(sim_names)


def test_no_drift_sim_home_full_joints():
    from physical_ai_server.workflow import sim_arm
    # sim rest pose = 5 arm HOME joints + gripper_open
    for pid in ('omx_full', 'omx_follower'):
        prof = rp.resolve(pid)
        assert list(sim_arm._SIM_HOME_FULL_JOINTS) == \
            list(prof.home_joints_rad) + [prof.gripper_open_rad]


def test_no_drift_safe_home_vs_collision_monitor():
    safe = _extract_literal(_COLLISION_PY, 'SAFE_HOME_ARM')
    for pid in ('omx_full', 'omx_follower'):
        assert rp.resolve(pid).safe_home_arm_rad == tuple(safe)


# --- both OMX profiles share geometry; differ only in id/name/flag/caps -----

def test_profiles_share_geometry_differ_only_in_identity():
    a, b = rp.resolve('omx_full'), rp.resolve('omx_follower')
    assert a.home_joints_rad == b.home_joints_rad
    assert a.safe_home_arm_rad == b.safe_home_arm_rad
    assert a.gripper_open_rad == b.gripper_open_rad
    assert a.gripper_closed_rad == b.gripper_closed_rad
    assert a.joint_names == b.joint_names
    assert a.num_arm_joints == b.num_arm_joints == 5
    assert a.urdf_asset_id == b.urdf_asset_id == 'omx_f'
    # differ
    assert a.follower_only is False and b.follower_only is True
    assert a.capabilities != b.capabilities


def test_registry_ids_match_keys():
    for key, prof in rp.ROBOT_PROFILES.items():
        assert prof.profile_id == key

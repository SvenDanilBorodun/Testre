#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The observation pose of both arms and the OMX's sector (spec 40 S6.4, S8, §6.1 O4; WP-13; D34, D57 Q2).

The profiles carry the contract's observation pose as literals (``robot_profiles`` is parsed by the
registry lockstep, so no import from the contract there); the OMX exports its joint-1 window as its
reach sector. The retreat of a „Solange sichtbar" loop with the REAL profile's pose takes WP-12's ladder
(direct, else via HOME) and passes the tower guard: on the V1 exactly WP-02 (3)'s hovers go via HOME
(6 of the 78 grid hovers, 2 of the 39 higher ones), on the OMX none.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

import test_fixture_guard as H
import test_tower_guard as TG
from physical_ai_server import robot_profiles as RP
from physical_ai_server.workflow import scene_fixture as SF
from physical_ai_server.workflow.handlers import motion as M
from physical_ai_server.workflow.workflow_manager import WorkflowContext

PROFILE_ARM = {'omx_full': 'omx', 'omx_follower': 'omx', 'v1_studio': 'v1'}


@pytest.mark.parametrize('pid', sorted(PROFILE_ARM))
def test_the_profiles_observation_poses_are_the_contract_s(pid):
    """FAILS FIRST (S6.4, D34): every profile's observe_pose_joints equals the contract's observe_joints_rad."""
    want = tuple(SF.ARMS[PROFILE_ARM[pid]]['observe_joints_rad'])
    assert RP.ROBOT_PROFILES[pid].observe_pose_joints == want


@pytest.mark.parametrize('pid', ['omx_full', 'omx_follower'])
def test_the_omx_exports_its_joint_1_window_as_its_reach_sector(pid):
    """FAILS FIRST (§6.1 O4, S8): the OMX profiles' reach_sector_deg is (-100, 100), the contract's joint-1
    window, on the manifest as a list; the V1 keeps (-90, 90)."""
    prof = RP.ROBOT_PROFILES[pid]
    assert prof.reach_sector_deg == tuple(SF.ARM_OMX['joint1_window_deg']) == (-100.0, 100.0)
    assert json.loads(RP.capabilities_json(prof))['reach_sector_deg'] == [-100, 100]
    assert RP.ROBOT_PROFILES['v1_studio'].reach_sector_deg == (-90, 90)


def _ctx(pid, start, guard, ik):
    """A run's context stamped from the REAL profile, exactly as WorkflowManager.start stamps it."""
    prof = RP.ROBOT_PROFILES[pid]
    arm = PROFILE_ARM[pid]
    ctx = WorkflowContext(
        publisher=lambda _pts: None, ik=ik, z_table=SF.MAT_THICKNESS_M + SF.FINGERTIP_BELOW_EE_M[arm],
        board_table_z=SF.MAT_THICKNESS_M, table_plane=None, zones=[], fixture_guard=guard,
        last_full_joints=[float(v) for v in start], num_arm_joints=prof.num_arm_joints,
        roll_joint_index=prof.roll_joint_index, home_joints_rad=prof.home_joints_rad,
        observe_pose_joints=prof.observe_pose_joints, gripper_open_rad=prof.gripper_open_rad,
        gripper_closed_rad=prof.gripper_closed_rad, velocity_limit_rad_s=prof.velocity_limit_rad_s)
    ctx.last_arm_joints = [float(v) for v in start[:5]]
    return ctx


@pytest.mark.parametrize('pid,extra', [('v1_studio', 0.0), ('v1_studio', 0.060), ('omx_full', 0.0),
                                       ('omx_full', 0.060)])
def test_the_observation_retreat_with_the_real_profiles_takes_the_ladder_and_passes_the_guard(
        pid, extra, monkeypatch):
    """FAILS FIRST (R4): from every hover of WP-02 (3)'s grids the retreat ends at the contract's observation
    pose, refused nowhere; it goes via HOME exactly for the hovers whose direct line the guard refuses (V1: the
    6 + 2 of WP-02 (3); OMX: none), and leaving it again takes the same rung."""
    arm = PROFILE_ARM[pid]
    legs = []
    monkeypatch.setattr(M, '_publish_motion', lambda c, a, b, d, tempo=None: legs.append((list(a), list(b))))
    ik = TG._arm_ik(arm)
    guard = TG._guard_on(arm, ik)
    obs = [float(v) for v in SF.ARMS[arm]['observe_joints_rad']]
    home = [float(v) for v in RP.ROBOT_PROFILES[pid].home_joints_rad]
    gripper = H.GR[arm]['open']
    grid = H._hover_grid(arm, extra)
    assert len(grid) == H.HOVER_COUNT[(arm, extra)]
    via_home = set()
    for key, q in grid:
        hover = [float(v) for v in q]
        ctx = _ctx(pid, hover + [gripper], guard, ik)
        legs.clear()
        M.go_to_observation_pose(ctx)
        assert np.allclose(legs[-1][1][:5], obs, atol=1e-9), key
        assert ctx.last_full_joints[:5] == pytest.approx(obs)
        reach = any(np.allclose(b[:5], home, atol=1e-9) for _a, b in legs[:-1])
        legs.clear()
        M.safe_move(ctx, obs + [gripper], hover + [gripper], M.DEFAULT_MOVE_DURATION_S)
        assert np.allclose(legs[-1][1][:5], hover, atol=1e-9), key
        leave = any(np.allclose(b[:5], home, atol=1e-9) for _a, b in legs[:-1])
        assert reach == leave, key
        if reach:
            via_home.add(key)
    assert via_home == set(H.OBS_REFUSED[(arm, extra)])
    assert len(via_home) == {('v1', 0.0): 6, ('v1', 0.060): 2}.get((arm, extra), 0)

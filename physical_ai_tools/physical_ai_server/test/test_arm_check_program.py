#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The arm check is an ordinary program (spec 40 S4, S6.5; WP-13; decisions D8, D34).

``autocal.service.arm_check_program`` builds it; here it runs through the real
``WorkflowManager.start`` on a fake arm (what is published is where the arm
goes, the measured joints are the last point) with the real arm model, the
table floor and the tower guard as before a kit's first arm check (the
uncalibrated residual at the contract place, WP-02 (2)). Pacing and waits run
on a fake clock.
"""

from __future__ import annotations

import json
import math
import time
import types

import numpy as np
import pytest

from physical_ai_server import robot_profiles as RP
from physical_ai_server.workflow import arm_model, fixture_guard, trajectory_builder
from physical_ai_server.workflow import scene_fixture as SF
from physical_ai_server.workflow.autocal import service as SV
from physical_ai_server.workflow.handlers import motion as M
from physical_ai_server.workflow.workflow_manager import WorkflowManager

PROFILES = ('v1_studio', 'omx_full', 'omx_follower')


@pytest.fixture(autouse=True)
def _fake_time(monkeypatch):
    """Chunk pacing and „warte" on a clock that jumps instead of sleeping."""
    state = {'t': 0.0}

    def monotonic():
        state['t'] += 0.01
        return state['t']

    def sleep(seconds):
        state['t'] += max(0.0, float(seconds))
    fake = types.SimpleNamespace(monotonic=monotonic, sleep=sleep, time=time.time)
    monkeypatch.setattr(trajectory_builder, 'time', fake)
    monkeypatch.setattr(M, 'time', fake)
    yield


class _Arm:
    """The fake arm: every published point is where it is; its joints are the last one."""

    def __init__(self, profile, start=None):
        arm = profile.home_joints_rad if start is None else start
        self.q = [float(v) for v in arm] + [float(profile.gripper_open_rad)]
        self.points = []
        self.chunks = 0

    def publish(self, points):
        self.chunks += 1
        for p in points:
            self.points.append([float(v) for v in p[0]])
        if points:
            self.q = [float(v) for v in points[-1][0]]

    def joints(self):
        return list(self.q)


def _calibration(profile):
    kind = SF.arm_key(profile.profile_id)
    return {'board_table_z': SF.MAT_THICKNESS_M, 'z_table': SF.MAT_THICKNESS_M + SF.FINGERTIP_BELOW_EE_M[kind],
            'table_plane': None}


def _run(pid, payload, workflow_id='armcheck-1790000000', start=None):
    profile = RP.ROBOT_PROFILES[pid]
    arm = _Arm(profile, start)
    ik = arm_model.for_robot(profile.build_ik(), pid, None)
    guards = []

    def provider():
        g = fixture_guard.make_guard(pid, ik.guard_frames, base_axis_x=ik.base_axis_x, arm_check=None,
                                     contract_hash=SF.contract_hash())
        guards.append(g)
        return g
    status, finished = [], []
    mgr = WorkflowManager(
        publisher=arm.publish, ik_factory=lambda: ik, load_calibration=lambda: _calibration(profile),
        emit_status=status.append, on_finished=finished.append, get_follower_joints=arm.joints,
        arm_profile=profile, fixture_guard_provider=provider)
    ok, msg, _unreachable = mgr.start(json.dumps(payload), workflow_id)
    assert ok, msg
    deadline = time.time() + 120.0
    while not finished and time.time() < deadline:
        time.sleep(0.01)
    assert finished, 'the program did not end'
    return finished[0], arm, status, guards


def _program(pid):
    profile = RP.ROBOT_PROFILES[pid]
    ik = arm_model.for_robot(profile.build_ik(), pid, None)
    return SV.arm_check_program(profile, ik.fk)


@pytest.mark.parametrize('pid', PROFILES)
def test_the_arm_check_program_visits_the_20_poses_in_order_claw_closed_home_last(pid):
    """FAILS FIRST: through WorkflowManager.start on a fake arm the program ends ``finished``; the claw closes
    first and stays closed; the arm stands at AC01 ... AC20 in order, each one exactly the contract's pinned
    pose (C10); the last pose is the Grundstellung; every leg was judged by the tower guard."""
    profile = RP.ROBOT_PROFILES[pid]
    kind = SF.arm_key(pid)
    phase, arm, status, guards = _run(pid, _program(pid))
    assert phase == 'finished', [s for s in status if s.get('phase') == 'error']
    closed = float(profile.gripper_closed_rad)
    first_closed = next(k for k, p in enumerate(arm.points) if abs(p[5] - closed) < 1e-9)
    assert all(abs(p[5] - closed) < 1e-9 for p in arm.points[first_closed:])
    assert all(np.allclose(p[:5], arm.points[0][:5]) for p in arm.points[:first_closed + 1])
    poses = [np.asarray(p, float) for p in SF.ARMS[kind]['arm_check_poses_rad']]
    visits = []
    for p in arm.points:
        for i, q in enumerate(poses):
            if np.allclose(p[:5], q, atol=1e-9) and (not visits or visits[-1] != i):
                visits.append(i)
    assert visits == list(range(20))
    assert np.allclose(arm.points[-1][:5], profile.home_joints_rad, atol=1e-9)
    assert guards and isinstance(guards[0], fixture_guard.TowerGuard) and not guards[0].calibrated
    blocks = [s['current_block_id'] for s in status
              if s.get('phase') == 'running' and str(s.get('current_block_id', '')).startswith('armcheck-')]
    order = []
    for b in blocks:
        if not order or order[-1] != b:
            order.append(b)
    want = ['armcheck-close'] + [x for i in range(1, 21) for x in (f'armcheck-move-{i:02d}', f'armcheck-wait-{i:02d}')]
    assert [b for b in order if not b.startswith('armcheck-ref-')] == want + ['armcheck-home']


@pytest.mark.parametrize('pid', PROFILES)
def test_the_program_is_s4_s(pid):
    """Tempo 0.5, no zone; „warte" 4.0 s after AC01 and 2.5 s after the others; every pose a Position taught on
    THIS arm (its joints = the contract's pinned list, the closed claw, the profile's joint names and robot
    type), so every leg is the joint route; the identity check's way home holds only the Grundstellung."""
    profile = RP.ROBOT_PROFILES[pid]
    kind = SF.arm_key(pid)
    payload = _program(pid)
    assert payload['tempo'] == 0.5 and payload['zones'] == []
    dests = payload['destinations']
    assert [d['name'] for d in dests] == [f'AC{i:02d}' for i in range(1, 21)]
    for d, q in zip(dests, SF.ARMS[kind]['arm_check_poses_rad']):
        assert d['kind'] == 'pose' and d['joints'] == list(q) + [float(profile.gripper_closed_rad)]
        assert d['joint_names'] == list(profile.joint_names) and d['robot_type'] == profile.data_robot_type
        assert M.position_joint_route(d, profile) == 'joints'
        assert all(math.isfinite(d[k]) for k in 'xyz')
    chain, b = [], payload['blocks']['blocks'][0]
    while b is not None:
        chain.append(b)
        b = (b.get('next') or {}).get('block')
    assert [c['type'] for c in chain[:3]] == ['edubotics_close_gripper', 'edubotics_move_to', 'edubotics_wait_seconds']
    waits = [c['inputs']['SECONDS']['shadow']['fields']['NUM'] for c in chain if c['type'] == 'edubotics_wait_seconds']
    assert waits == [4.0] + [2.5] * 19
    refs = [c['inputs']['DESTINATION']['block']['fields']['NAME'] for c in chain if c['type'] == 'edubotics_move_to']
    assert refs == [d['name'] for d in dests]
    assert chain[-1]['type'] == 'edubotics_home' and len(chain) == 42
    home = SV.home_program()
    assert [x['type'] for x in home['blocks']['blocks']] == ['edubotics_home']
    assert 'next' not in home['blocks']['blocks'][0]


@pytest.mark.parametrize('pid', ['v1_studio', 'omx_full'])
def test_the_service_follows_the_program_s_own_blocks(pid, tmp_path):
    """The status the real program emits drives the capture's pose and dwell: a move block names its pose, a
    wait block its dwell, the Grundstellung neither."""
    phase, _arm, status, _guards = _run(pid, _program(pid))
    assert phase == 'finished'
    profile = RP.ROBOT_PROFILES[pid]
    camera = types.SimpleNamespace(profile=lambda: 'tower_v1', camera_state=lambda: {'state': 'streaming'},
                                   hold=lambda: None, release=lambda: None)
    started = []
    ik = arm_model.for_robot(profile.build_ik(), pid, None)
    svc = SV.SceneCalibration(profile, camera, str(tmp_path), detect=lambda f, r=None: None,
                              get_joints=lambda: list(profile.home_joints_rad), fk=ik.fk, guard_parts=None,
                              start_program=lambda j, w: (started.append(w) or (True, '')),
                              stop_program=lambda w: None, wall=lambda: 1_790_000_000,
                              start_thread=lambda target, name: None)
    assert svc.start('arm_check')[0] and started == ['armcheck-1790000000']
    seen = []
    for s in status:
        svc.on_workflow_status(s)
        seen.append((svc._run.pose, svc._run.dwell))
    dwells = []
    for pose, dwell in seen:
        if dwell and (not dwells or dwells[-1] != pose):
            dwells.append(pose)
    assert dwells == list(range(20))
    assert seen[-1] == (19, False)          # the Grundstellung's block is no dwell


def _walks_the_lines(points, waypoints, tol=1e-6):
    """True when the published arm joints walk the straight joint lines between consecutive ``waypoints`` in
    order, reaching each one and ending on the last (a point may only stay on its line or step onto the next)."""
    W = [np.asarray(w, float) for w in waypoints]
    m = 0

    def on(p, k):
        a, d = W[k], W[k + 1] - W[k]
        s = min(1.0, max(0.0, float(np.dot(p - a, d) / np.dot(d, d))))
        return float(np.max(np.abs(a + s * d - p))) <= tol
    for point in points:
        p = np.asarray(point[:5], float)
        if on(p, m):
            continue
        if m + 2 < len(W) and on(p, m + 1):
            m += 1
            continue
        return False
    return m == len(W) - 2 and np.allclose(points[-1][:5], W[-1], atol=tol)


@pytest.mark.parametrize('pid', ['v1_studio', 'omx_full'])
@pytest.mark.parametrize('corner', ['home', 'plus', 'minus'])
def test_the_program_drives_straight_joint_lines_from_any_start_in_the_home_box(pid, corner):
    """The route the target fence judges (``test_arm_check_target_fence.py``: straight joint lines start -> AC01
    … AC20 -> HOME) is the route the program drives, from HOME and from two corners of the service's start box
    (every arm joint ``ARM_CHECK_HOME_TOL_RAD`` off HOME): no via is inserted anywhere."""
    profile = RP.ROBOT_PROFILES[pid]
    kind = SF.arm_key(pid)
    home = np.asarray(profile.home_joints_rad, float)
    sign = {'home': 0.0, 'plus': 1.0, 'minus': -1.0}[corner]
    start = home + sign * SV.ARM_CHECK_HOME_TOL_RAD * np.array([1.0, -1.0, 1.0, -1.0, 1.0])
    phase, arm, status, _guards = _run(pid, _program(pid), start=list(start))
    assert phase == 'finished', [s for s in status if s.get('phase') == 'error']
    route = [start] + [np.asarray(p, float) for p in SF.ARMS[kind]['arm_check_poses_rad']] + [home]
    assert _walks_the_lines(arm.points, route)

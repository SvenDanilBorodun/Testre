#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""``/edubotics/home_glide_state``: the server's verdict on the manual home glide for the V1's live pose (spec 40
WP-12 revision 2, 52 §5.1; decisions D56, D57).

``{"v": 1, "glide": "clear" | "refused" | "unjudgeable", "gen": n}``, latched and re-sent at 2 Hz while the V1's
studio is idle under ``tower_v1``, computed from the same planner call the press of „Grundstellung" makes
(``_plan_manual_home_legs``, the tower guard as a rung condition in ``home_planner``). The OMX, ``legacy`` and an
unknown profile get no publisher at all. ``physical_ai_server.py`` cannot be imported without rclpy, hence
``ast`` + ``exec`` onto a stub node, as in ``test_arm_model_node_wiring.py``.
"""

from __future__ import annotations

import ast
import json
import math
import types

import numpy as np
import pytest

import test_tower_guard as T
from physical_ai_server import robot_profiles as RP
from physical_ai_server.workflow import home_planner as HP
from physical_ai_server.workflow.handlers import motion as M


def _module_consts(names):
    out = {}
    for node in ast.parse(T._NODE_SOURCE).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in names:
                out[node.targets[0].id] = eval(compile(ast.Expression(body=node.value), str(T._NODE_PY), 'eval'),
                                               {'math': math}, dict(out))
    assert set(out) == set(names), set(names) - set(out)
    return out


_CONSTS = _module_consts(['_HOME_GLIDE_STATE_TOPIC', '_HOME_GLIDE_STATE_PERIOD_S', '_HOME_GLIDE_REJUDGE_RAD',
                          '_MANUAL_HOME_MIN_DURATION_S'])


@pytest.mark.parametrize('name,value', [('_HOME_GLIDE_STATE_PERIOD_S', 0.5),
                                        ('_HOME_GLIDE_REJUDGE_RAD', 0.0030679615757712823)])
def test_the_shipped_rate_and_resolution(name, value):
    """2 Hz (the driver's tower state's rate) and two servo ticks (its re-judge resolution), pinned as literals."""
    assert _CONSTS[name] == pytest.approx(value, abs=1e-15)


class _String:
    def __init__(self):
        self.data = ''


class _Publisher:
    def __init__(self, topic, qos):
        self.topic, self.qos, self.sent = topic, qos, []

    def publish(self, msg):
        self.sent.append(json.loads(msg.data))


_NAMES = ('_init_home_glide_state', '_home_glide_state_tick', '_home_glide_verdict', '_home_glide_idle',
          '_plan_manual_home_legs', '_fixture_guard', '_build_profile_ik', '_build_ik_solver', '_build_sim_ik_solver')


class _Node:
    def __init__(self, profile_id, scene, calib_dir):
        self._arm_profile = RP.ROBOT_PROFILES[profile_id]
        self.studio_camera = T._Camera(scene)
        self.logger = T._Logger()
        self.joints = None
        self.stale = False
        self.communicator = types.SimpleNamespace(get_latest_follower_joints=lambda: self.joints)
        self.on_workflow = self.on_recording = self.on_inference = False
        self._manual_transient_ops = 0
        self._manual_record_active = False
        self.timers, self.publishers = [], []
        extra = dict(_CONSTS, String=_String,
                     QoSProfile=lambda **kw: types.SimpleNamespace(**kw),
                     ReliabilityPolicy=types.SimpleNamespace(RELIABLE='reliable'),
                     DurabilityPolicy=types.SimpleNamespace(TRANSIENT_LOCAL='transient_local'),
                     MutuallyExclusiveCallbackGroup=object)
        for name, fn in T._node_fns(*_NAMES, extra=extra).items():
            setattr(self, name, types.MethodType(fn, self))

    def has_parameter(self, _name):
        return False

    def get_logger(self):
        return self.logger

    def _profile_n(self):
        return 5

    def _follower_joints_stale(self):
        return self.stale

    def _load_workflow_calibration(self):
        return {'z_table': T.SF.MAT_THICKNESS_M, 'board_table_z': T.SF.MAT_THICKNESS_M, 'table_plane': None}

    def create_timer(self, period, callback, callback_group=None):
        self.timers.append((period, callback))
        return object()

    def create_publisher(self, msg_type, topic, qos):
        assert msg_type is _String
        pub = _Publisher(topic, qos)
        self.publishers.append(pub)
        return pub

    def tick(self):
        self._home_glide_state_tick()
        return self.publishers[0].sent[-1] if self.publishers and self.publishers[0].sent else None


def _press_verdict(node, joints):
    """what the press of „Grundstellung" gets for ``joints``, classified: the planner's legs, its tower refusal
    (too close / unjudgeable), or any other refusal (the floor: not the tower's verdict)."""
    try:
        node._plan_manual_home_legs(joints)
    except HP.TowerRefusal as e:
        assert str(e) in (T._HOME_REFUSED_DE, T._UNJUDGEABLE_DE,
                          T.fg().TOWER_DROOP_UNJUDGEABLE_DE)
        return 'refused' if str(e) == T._HOME_REFUSED_DE else 'unjudgeable'
    except M.WorkflowError:
        return 'clear'
    return 'clear'


@pytest.mark.parametrize('profile_id,scene', [('omx_full', 'tower_v1'), ('omx_follower', 'tower_v1'),
                                              ('omx_full', None), ('v1_studio', 'legacy'), ('v1_studio', None)])
def test_no_publisher_for_the_omx_legacy_or_an_unknown_profile(profile_id, scene, tmp_path, monkeypatch):
    """FAILS FIRST (revision 2, D1): the OMX profiles get no timer and no publisher; the V1 under ``legacy`` or
    while the profile is unknown creates no publisher, however long it ticks."""
    monkeypatch.setenv('EDUBOTICS_CALIB_DIR', str(tmp_path))
    node = _Node(profile_id, scene, tmp_path)
    node._init_home_glide_state()
    if profile_id != 'v1_studio':
        assert node.timers == [] and node.publishers == []
        return
    assert [p for p, _cb in node.timers] == [_CONSTS['_HOME_GLIDE_STATE_PERIOD_S']]
    node.joints = [0.0, 0.64, 0.64, 0.0, 0.0, 0.9]
    for _ in range(5):
        node.timers[0][1]()
    assert node.publishers == []


def test_the_published_verdict_is_the_planners_for_200_seeded_live_poses(tmp_path, monkeypatch):
    """FAILS FIRST (revision 2): on the V1 under ``tower_v1``, for 200 seeded live poses (uniform over the joint
    limits and the claw's band, kept where the arm stands above the table, as a live arm does) the published verdict
    equals what the press would get from the same planner call; the message is ``{v, glide, gen}``, latched, gen
    strictly increasing; refusals and clear verdicts both occur."""
    monkeypatch.setenv('EDUBOTICS_CALIB_DIR', str(tmp_path))
    node = _Node('v1_studio', 'tower_v1', tmp_path)
    node._init_home_glide_state()
    lim = np.array(RP.ROBOT_PROFILES['v1_studio'].build_ik().joint_limits)
    table = types.SimpleNamespace(ik=node._build_ik_solver(), z_table=T.SF.MAT_THICKNESS_M, table_plane=None)
    rng = np.random.default_rng(56)
    seen = {}
    gens = []
    while len(gens) < 200:
        q = [float(v) for v in rng.uniform(lim[:, 0], lim[:, 1])] + [float(rng.uniform(0.0, 0.9))]
        clearance = HP.pose_floor_clearance(table, q)
        if clearance is None or clearance < 0.0:
            continue
        node.joints = q
        msg = node.tick()
        assert set(msg) == {'v', 'glide', 'gen'} and msg['v'] == 1
        assert msg['glide'] == _press_verdict(node, q), q
        seen[msg['glide']] = seen.get(msg['glide'], 0) + 1
        gens.append(msg['gen'])
    assert gens == sorted(gens) and len(set(gens)) == len(gens)
    assert seen.get('refused', 0) >= 10 and seen.get('clear', 0) >= 10, seen
    pub = node.publishers[0]
    assert pub.topic == '/edubotics/home_glide_state' == _CONSTS['_HOME_GLIDE_STATE_TOPIC']
    assert pub.qos.durability == 'transient_local' and pub.qos.reliability == 'reliable' and pub.qos.depth == 1


def test_the_state_follows_the_studio_and_the_reading(tmp_path, monkeypatch):
    """Published only while the studio is idle (no manual drive, recording, program or inference); a pose that cannot
    be read (none, stale) is ``unjudgeable``; a reading that moved less than _HOME_GLIDE_REJUDGE_RAD keeps its
    verdict (the driver's hint resolution); the profile turning ``legacy`` stops it."""
    monkeypatch.setenv('EDUBOTICS_CALIB_DIR', str(tmp_path))
    node = _Node('v1_studio', 'tower_v1', tmp_path)
    node._init_home_glide_state()
    node.joints = [0.0, 0.64, 0.64, 0.0, 0.0, 0.9]
    assert node.tick()['glide'] == 'clear'
    sent = len(node.publishers[0].sent)
    for flag in ('on_workflow', 'on_recording', 'on_inference', '_manual_record_active'):
        setattr(node, flag, True)
        node._home_glide_state_tick()
        setattr(node, flag, False)
    node._manual_transient_ops = 1
    node._home_glide_state_tick()
    node._manual_transient_ops = 0
    assert len(node.publishers[0].sent) == sent
    node.stale = True
    assert node.tick()['glide'] == 'unjudgeable'
    node.stale = False
    node.joints = None
    assert node.tick()['glide'] == 'unjudgeable'
    node.joints = [0.0, 0.64, 0.64, 0.0, 0.0, 0.9]
    assert node.tick()['glide'] == 'clear'
    calls = []
    real = node._plan_manual_home_legs
    node._plan_manual_home_legs = lambda joints, quiet=False: (calls.append(joints), real(joints, quiet=quiet))[1]
    step = 0.5 * _CONSTS['_HOME_GLIDE_REJUDGE_RAD']
    node.joints = [step, 0.64, 0.64, 0.0, 0.0, 0.9]
    node.tick()
    assert calls == []
    node.joints = [4 * step, 0.64, 0.64, 0.0, 0.0, 0.9]
    node.tick()
    assert len(calls) == 1
    node.studio_camera = T._Camera('legacy')
    n = len(node.publishers[0].sent)
    node._home_glide_state_tick()
    assert len(node.publishers[0].sent) == n

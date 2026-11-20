#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The calibration service (spec 40 S4; WP-13): the mat check, the arm check as
a program with its pose-1 check, the capture rule, the solve hook with the
``guard_margin`` gate, the program-start gate and the state.

Driven on a simulated kit (``autocal_sim``: the true arm with droop and
encoders, the true camera, mat, fixture tags and clip-on target) through the
service's own seams: the camera (profile, state, frames), the detector (the
kit's tags in view, inside the region asked for), the measured joints, the
program start / stop and a fake clock. The program is played by the test: the
blocks the service built are announced as ``/workflow/status`` would, the arm
is put on each pose and the capture ticks run at 10 Hz of fake time. The
program itself runs through the real ``WorkflowManager`` in
``test_arm_check_program.py``. The last section holds the node's wiring:
``physical_ai_server.py`` needs rclpy, so its methods are exec'd from the
source onto stub nodes.
"""

from __future__ import annotations

import ast
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import textwrap
import threading
import time
import types

import numpy as np
import pytest

import autocal_sim as S
from physical_ai_server import robot_profiles as RP
from physical_ai_server.workflow import fixture_guard as FG
from physical_ai_server.workflow import scene_fixture as SF
from physical_ai_server.workflow.autocal import arm_check as AC
from physical_ai_server.workflow.autocal import service as SV
from physical_ai_server.workflow.autocal import store as ST
from physical_ai_server.workflow.autocal import tags as TG
from physical_ai_server.workflow.autocal import texts_de as T

KINDS = ('v1', 'omx')
PROFILE = {'v1': 'v1_studio', 'omx': 'omx_full'}
# Quiet kits (no axis misalignment, no encoder non-linearity, the camera at the contract pose, no decentering,
# the tower at its place) at autocal_sim's full pixel noise, 0.15 px per corner and look: the capture's 0.4 px
# stillness rule judges each tag's centre (conductor D89-2; judged per corner it needed a third of the noise).
QUIET = replace(S.Options(), skew_deg=0.0, inl_deg=0.0, cam_lens_m=0.0, cam_rot_deg=0.0, decenter=0.0,
                tower_place=False)
NO_DROOP = replace(QUIET, droop_scale=0.0)
CAMERA_STATE = {'v': 1, 'profile': 'tower_v1', 'state': 'streaming', 'width': 1920, 'height': 1080, 'fps': 30.0,
                'fps_low': False, 'passthrough': True, 'outside': SF.OUTSIDE, 'profile_id': '0123456789ab',
                'platform': 'windows', 'fault': None, 'dropped': 0}


# ══════════════════════════════════════════════════════════════════════════
# the simulated kit seen through the service's seams
# ══════════════════════════════════════════════════════════════════════════

class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class _Frame:
    """A stand-in for a decoded studio frame: only its size and identity matter."""

    shape = (1080, 1920, 3)

    def __init__(self, seq):
        self.seq = seq


class _Camera:
    def __init__(self, profile='tower_v1', state=CAMERA_STATE):
        self._profile = profile
        self.state = None if state is None else dict(state)
        self.holds = 0
        self.frames = 0

    def profile(self):
        return self._profile

    def camera_state(self):
        return None if self.state is None else dict(self.state)

    def latest_frame(self, max_age_s, wait_s=2.0):
        self.frames += 1
        return _Frame(self.frames), 0.0

    def hold(self):
        self.holds += 1

    def release(self):
        self.holds -= 1


class _Env:
    """The kit, its arm and what the camera sees; the seams the service is built with."""

    def __init__(self, kind, opt=QUIET, seed=3, camera=None, calib_dir=None, guard_parts=None):
        self.kind = kind
        self.opt = opt
        self.rng = np.random.default_rng([seed, 0])
        self.kit = S.sample_kit(kind, self.rng, opt)
        self.look_rng = np.random.default_rng([seed, 5])
        self.profile = RP.ROBOT_PROFILES[PROFILE[kind]]
        self.poses = [np.asarray(p, float) for p in SF.ARMS[kind]['arm_check_poses_rad']]
        self.clock = _Clock()
        self.camera = camera or _Camera()
        self.target_ids = SF.ARMS[kind]['target_tag_ids']
        self.target_attached = True        # the clip-on target on the gripper
        self.target_hidden_at = set()      # pose indices at which the target is not seen
        self.foreign_target = False        # the OTHER arm's target is clipped on
        self.mat_visible = True
        self.jitter = 0.0                  # encoder jitter per read (a moving arm)
        self.a = None                      # true shaft angles
        self.enc = None                    # encoder readings
        self.at = None                     # the pose index the arm stands at
        self.programs = []                 # (workflow_json dict, workflow_id) started
        self.stops = []
        self.logs = []
        self.states = []
        self.detects = []                  # roi of every detector call
        self.svc = None
        self.svc = SV.SceneCalibration(
            self.profile, self.camera, str(calib_dir), detect=self.detect, get_joints=self.joints,
            fk=_nominal_model(PROFILE[kind]).fk, guard_parts=guard_parts or _node_guard_parts(PROFILE[kind]),
            start_program=self.start_program, stop_program=self.stop_program, log=self.logs.append,
            on_state=self.states.append, clock=self.clock, wall=lambda: 1_790_000_000,
            start_thread=self.start_thread)
        self.threads = []

    # -- the service's seams --
    def start_thread(self, target, name):
        self.threads.append(name)
        if name != SV.CAPTURE_THREAD:
            target()

    def start_program(self, workflow_json, workflow_id):
        gate = self.svc.program_start_gate(workflow_json, workflow_id)
        if gate is not None:
            return False, gate
        self.programs.append((json.loads(workflow_json), workflow_id))
        return True, 'Workflow gestartet.'

    def stop_program(self, workflow_id):
        self.stops.append(workflow_id)
        self.svc.on_program_end(workflow_id, 'stopped')

    def joints(self):
        if self.enc is None:
            return None
        q = self.enc + (self.rng.normal(0.0, self.jitter, 5) if self.jitter else 0.0)
        return [float(v) for v in q] + [float(self.profile.gripper_closed_rad)]

    def detect(self, frame, roi=None):
        self.detects.append(roi)
        kit, rng = self.kit, self.look_rng
        noise = S.SIG_PX * self.opt.noise
        found = {}
        if self.a is not None and self.target_attached and self.at not in self.target_hidden_at:
            uv = kit.cam.project(S.target_points(kit, self.a + kit.dzero)) + rng.normal(0, noise, (36, 2))
            other = SF.ARMS['omx' if self.kind == 'v1' else 'v1']['target_tag_ids']
            ids = other if self.foreign_target else self.target_ids
            for k, tid in enumerate(ids):
                c = uv[4 * k:4 * k + 4]
                if S.visible(c) and _inside(c, roi):
                    found[tid] = TG.TagObs(c, 80.0)
        if self.mat_visible:
            for tid, P in S.mat_points(kit, kit.mat).items():
                c = kit.cam.project(P) + rng.normal(0, noise, (4, 2))
                if S.visible(c) and _inside(c, roi):
                    found[tid] = TG.TagObs(c, 80.0)
            fx = kit.cam.project(kit.fixture_w) + rng.normal(0, noise / math.sqrt(5), (8, 2))
            for k, tid in enumerate(TG.FIXTURE_IDS):
                c = fx[4 * k:4 * k + 4]
                if S.visible(c) and _inside(c, roi):
                    found[tid] = TG.TagObs(c, 80.0)
        return TG.Frame(S.WIDTH, S.HEIGHT, 120.0, found)

    # -- the world --
    def put(self, i):
        """The arm comes to rest at contract pose ``i`` (its droop, backlash and any extra sag)."""
        kit = self.kit
        q = self.poses[i]
        tau = S.gravity_torques(self.kind, q + kit.dzero, kit.masses, kit.mount, self.opt.target_mass)[0]
        self.a = q - tau / kit.stiff + kit.lash(self.rng) + kit.extra_sag.get(i, 0.0)
        self.enc = np.round((self.a + kit.inl_of(self.a)) / S.TICK) * S.TICK
        self.at = i

    def home(self):
        self.a = np.asarray(self.profile.home_joints_rad, float)
        self.enc = self.a.copy()
        self.at = None

    def status(self, block_id, workflow_id=None):
        self.svc.on_workflow_status({'workflow_id': workflow_id or self.wid, 'current_block_id': block_id,
                                     'phase': 'running', 'progress': 0.0})

    @property
    def wid(self):
        return self.programs[-1][1]

    def play(self, upto=None, finish=True):
        """Play the arm-check program: each pose's move, its dwell at 10 Hz, then HOME and the end.
        Stops early when the service stopped the program."""
        n = len(self.poses) if upto is None else upto
        wid = self.wid
        for i in range(n):
            self.status(f'armcheck-move-{i + 1:02d}', wid)
            self.put(i)
            self.status(f'armcheck-wait-{i + 1:02d}', wid)
            dwell = SV.FIRST_DWELL_S if i == 0 else SV.DWELL_S
            for _ in range(int(round(dwell / SV.CAPTURE_PERIOD_S))):
                self.clock.advance(SV.CAPTURE_PERIOD_S)
                self.svc.capture_tick()
                if self.stops:
                    return 'stopped'
        if finish:
            self.status('armcheck-home', wid)
            self.home()
            self.svc.on_program_end(wid, 'finished')
        return 'finished'


def _nominal_model(pid):
    from physical_ai_server.workflow import arm_model
    return arm_model.for_robot(RP.ROBOT_PROFILES[pid].build_ik(), pid, None)


def _node_guard_parts(pid):
    """The node's own guard_margin parts (``_calibration_guard_parts``, exec'd from the source)."""
    node = _StubNode('_calibration_guard_parts', '_build_profile_ik', profile_id=pid)
    node.has_parameter = lambda _name: False
    return node._calibration_guard_parts


def _inside(corners, roi):
    if roi is None:
        return True
    x0, y0, x1, y1 = roi
    c = np.asarray(corners, float)
    return bool(np.all((c[:, 0] >= x0) & (c[:, 0] <= x1) & (c[:, 1] >= y0) & (c[:, 1] <= y1)))


def _stored(env):
    return ST.Store(env.svc.calib_dir).read(PROFILE[env.kind], (1920, 1080))


def _plate_moved(ids, own, k, motion, step_px=0.5):
    """The target's corners ``own`` (``{tag id: (4, 2)}``) after ``k`` looks of a plate moving ``step_px`` per
    look: ``'shift'`` along the image's u, or ``'turn'`` about the plate centre (the centre tag ``ids[4]`` of the
    3 × 3 plate) so that the nearest outer tags' centres move ``step_px`` (the corner tags √2 as far)."""
    if motion == 'shift':
        return {tid: np.asarray(c, float) + (k * step_px, 0.0) for tid, c in own.items()}
    pivot = np.mean(own[ids[4]], axis=0)
    d = min(float(np.linalg.norm(np.mean(c, axis=0) - pivot)) for tid, c in own.items() if tid != ids[4])
    a = k * 2.0 * math.asin(step_px / 2.0 / d)
    turn = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
    return {tid: (np.asarray(c, float) - pivot) @ turn.T + pivot for tid, c in own.items()}


# ══════════════════════════════════════════════════════════════════════════
# starting the arm check
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('kind', KINDS)
def test_the_arm_check_starts_with_no_tag_visible(kind, tmp_path):
    """FAILS FIRST: at the Grundstellung the target faces away (review B4) — the start needs no tag, only a
    streaming camera; it builds the internal program, holds the studio camera and grabs no frame."""
    env = _Env(kind, calib_dir=tmp_path)
    env.home()
    env.target_attached = False
    env.mat_visible = False
    ok, msg = env.svc.start('arm_check')
    assert ok, msg
    assert msg == T.arm_check_running_de(1, 20)
    assert len(env.programs) == 1
    payload, wid = env.programs[0]
    assert wid == 'armcheck-1790000000'
    assert env.detects == [] and env.camera.frames == 0
    assert env.camera.holds == 1
    assert SV.CAPTURE_THREAD in env.threads
    state = env.svc.state()
    assert state['state'] == 'arm_check_running' and state['arm_check'] == {'pose': 1, 'poses': 20, 'captured': 0}


@pytest.mark.parametrize('kind', KINDS)
def test_the_arm_check_is_refused_only_without_a_streaming_tower_camera(kind, tmp_path):
    """A camera state that is not streaming refuses; so do no statement and a legacy statement (each with S4's
    sentence); a second start while one runs is refused."""
    env = _Env(kind, calib_dir=tmp_path, camera=_Camera(state=dict(CAMERA_STATE, state='fault')))
    assert env.svc.start('arm_check') == (False, T.CAMERA_MISSING_DE)
    assert env.programs == []
    assert _Env(kind, calib_dir=tmp_path, camera=_Camera(profile=None)).svc.start('arm_check') == (
        False, T.PROFILE_UNKNOWN_DE)
    assert _Env(kind, calib_dir=tmp_path, camera=_Camera(profile='legacy')).svc.start('arm_check') == (
        False, T.CAMERA_NOT_TOWER_DE)
    env = _Env(kind, calib_dir=tmp_path)
    assert env.svc.start('arm_check')[0]
    assert env.svc.start('arm_check') == (False, SV.ARM_CHECK_BUSY_DE)
    assert len(env.programs) == 1


@pytest.mark.parametrize('kind', KINDS)
def test_a_refused_program_start_releases_everything(kind, tmp_path):
    """What refuses any real program start (mode arbitration, a live leader, torque) refuses the arm check too:
    its sentence is the answer, the camera is released, nothing runs."""
    env = _Env(kind, calib_dir=tmp_path)
    env.start_program = lambda _json, _wid: (False, 'Ein Workflow läuft gerade — bitte zuerst stoppen.')
    env.svc._start_program = env.start_program
    assert env.svc.start('arm_check') == (False, 'Ein Workflow läuft gerade — bitte zuerst stoppen.')
    assert env.camera.holds == 0
    assert env.svc.state()['state'] != 'arm_check_running'
    assert env.svc.start('arm_check')[0] is False   # still refused, never "already running"


# ══════════════════════════════════════════════════════════════════════════
# pose 1: the identity check
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('kind', KINDS)
def test_pose_1_without_a_target_stops_the_program_homes_and_says_why(kind, tmp_path):
    """FAILS FIRST (review B4): still at AC01 for 3.0 s with fewer than 6 target tags in its region -> the
    service stops the program, starts a second program holding only the Grundstellung, and the German
    sentence is the state; nothing is written."""
    env = _Env(kind, calib_dir=tmp_path)
    env.target_attached = False
    assert env.svc.start('arm_check')[0]
    assert env.play() == 'stopped'
    assert env.stops == ['armcheck-1790000000']
    assert len(env.programs) == 2
    home, home_wid = env.programs[1]
    assert home_wid.startswith(SV.HOME_PROGRAM_PREFIX)
    root = home['blocks']['blocks']
    assert [b['type'] for b in root] == ['edubotics_home'] and 'next' not in root[0]
    state = env.svc.state()
    assert state['message_de'] == T.TARGET_NOT_SEEN_DE and state['code'] == 'target_not_seen'
    assert state['state'] == 'needs_arm_check'
    assert _stored(env).status == 'none'
    # the stop came 3.0 s after the arm came to rest at AC01, inside its 4.0 s dwell
    env.svc.on_program_end(home_wid, 'finished')
    assert env.camera.holds == 0
    assert env.svc.state()['message_de'] == T.TARGET_NOT_SEEN_DE


@pytest.mark.parametrize('kind', KINDS)
def test_pose_1_with_the_other_arm_s_target_names_it(kind, tmp_path):
    """FAILS FIRST: the other arm's target on the gripper -> „Am Greifer steckt das Kalibrier-Ziel des anderen
    Roboters.", the program stopped and homed, nothing written."""
    env = _Env(kind, calib_dir=tmp_path)
    env.foreign_target = True
    assert env.svc.start('arm_check')[0]
    assert env.play() == 'stopped'
    assert env.svc.state()['message_de'] == T.FOREIGN_TARGET_DE
    assert env.svc.state()['code'] == 'foreign_target'
    assert len(env.programs) == 2 and env.programs[1][1].startswith(SV.HOME_PROGRAM_PREFIX)
    assert _stored(env).status == 'none'


@pytest.mark.parametrize('kind', KINDS)
def test_pose_1_with_the_mat_covered_says_the_mat_sentence(kind, tmp_path):
    """Fewer than 6 field tags at pose 1 -> the mat sentence (tags_hidden), stopped and homed."""
    env = _Env(kind, calib_dir=tmp_path)
    env.mat_visible = False
    assert env.svc.start('arm_check')[0]
    assert env.play() == 'stopped'
    assert env.svc.state()['code'] == 'tags_hidden'
    assert env.svc.state()['message_de'] == T.TAGS_HIDDEN_DE


@pytest.mark.parametrize('kind', KINDS)
def test_pose_1_passes_once_the_target_is_seen(kind, tmp_path):
    """The identity check passes: no stop at pose 1, the program goes on (two poses played)."""
    env = _Env(kind, calib_dir=tmp_path)
    assert env.svc.start('arm_check')[0]
    assert env.play(upto=2, finish=False) == 'finished'
    assert env.stops == [] and len(env.programs) == 1
    assert env.svc.state()['arm_check'] == {'pose': 2, 'poses': 20, 'captured': 2}


# ══════════════════════════════════════════════════════════════════════════
# the capture rule
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('kind', KINDS)
def test_a_pose_is_captured_only_when_still_and_seen(kind, tmp_path):
    """A pose whose target is hidden is not captured; an arm that does not come to rest (encoders moving more
    than 0.002 rad within 0.3 s) is not captured either, and is no rested record."""
    env = _Env(kind, calib_dir=tmp_path)
    env.target_hidden_at = {3}
    assert env.svc.start('arm_check')[0]
    env.play(upto=5, finish=False)
    run = env.svc._run
    assert sorted(run.captured) == [0, 1, 2, 4]
    assert 3 in run.rested and run.rested[3] is not None
    jittery = _Env(kind, calib_dir=tmp_path / 'b')
    assert jittery.svc.start('arm_check')[0]
    jittery.play(upto=1, finish=False)
    jittery.jitter = 0.004
    wid = jittery.wid
    jittery.status('armcheck-move-02', wid)
    jittery.put(1)
    jittery.status('armcheck-wait-02', wid)
    for _ in range(25):
        jittery.clock.advance(0.1)
        jittery.svc.capture_tick()
    assert 1 not in jittery.svc._run.captured and 1 not in jittery.svc._run.rested


@pytest.mark.parametrize('kind', KINDS)
def test_every_pose_is_captured_at_full_detector_noise(kind, tmp_path):
    """FAILS FIRST (S4, conductor D89-2): at autocal_sim's full pixel noise (0.15 px per corner and look) a
    still target is captured at every pose within its dwell, in each of 20 seeded runs. Judged by the largest
    of a tag's four corner moves, the same runs captured 61 % of the poses and none of them all 20."""
    assert QUIET.noise == 1.0
    for seed in range(20):
        env = _Env(kind, seed=seed, calib_dir=tmp_path / str(seed))
        assert env.svc.start('arm_check')[0]
        assert env.play(finish=False) == 'finished'
        assert sorted(env.svc._run.captured) == list(range(20)), seed


@pytest.mark.parametrize('motion', ('shift', 'turn'))
@pytest.mark.parametrize('kind', KINDS)
def test_a_target_moving_half_a_pixel_per_look_is_never_captured(kind, motion, tmp_path):
    """The centre rule keeps its grip on a real motion: with the arm at rest at every pose, a target that moves
    0.5 px per look — shifted, or turned about the plate centre so that the outer tags' centres move 0.5 px —
    is captured at no pose, at full detector noise."""
    env = _Env(kind, calib_dir=tmp_path)
    looks = {}
    real = env.detect

    def detect(frame, roi=None):
        found = real(frame, roi)
        if roi is None:                                 # the full-frame look of the mat and the fixture
            return found
        k = looks[env.at] = looks.get(env.at, -1) + 1
        own = {tid: obs.corners for tid, obs in found.tags.items() if tid in env.target_ids}
        tags = dict(found.tags)
        tags.update({tid: TG.TagObs(c, 80.0) for tid, c in _plate_moved(env.target_ids, own, k, motion).items()})
        return TG.Frame(found.width, found.height, found.mean_grey, tags)
    env.svc._detect = detect
    assert env.svc.start('arm_check')[0]
    assert env.play(finish=False) == 'finished'
    run = env.svc._run
    assert run.captured == {} and sorted(run.rested) == list(range(20))
    assert min(looks.values()) >= SV.CAPTURE_LOOKS                  # every pose was looked at often enough


# ══════════════════════════════════════════════════════════════════════════
# the solve hook
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('kind', KINDS)
def test_thirteen_captured_poses_are_too_few_and_nothing_is_written(kind, tmp_path):
    """FAILS FIRST: 13 of 20 poses captured -> the German too-few-poses result and no file."""
    env = _Env(kind, calib_dir=tmp_path)
    env.target_hidden_at = set(range(13, 20))
    assert env.svc.start('arm_check')[0]
    assert env.play() == 'finished'
    state = env.svc.state()
    assert state['code'] == 'too_few_poses' and state['state'] == 'needs_arm_check'
    assert state['message_de'] == T.too_few_poses_de(13, 20)
    assert _stored(env).status == 'none'
    assert env.camera.holds == 0


@pytest.mark.parametrize('kind', KINDS)
def test_a_good_run_writes_the_arm_check_and_real_starts_wait_for_the_target_removed_step(kind, tmp_path):
    """FAILS FIRST: a good run writes arm_check.json with target_removed false and asks for the target to be
    taken off; until the step target_removed every real program start is refused; the step sets the flag and
    runs the mat check; a target tag seen by a later mat check clears the flag again."""
    env = _Env(kind, calib_dir=tmp_path)
    assert env.svc.start('arm_check')[0]
    assert env.play() == 'finished'
    got = _stored(env)
    assert got.status == 'ok', env.svc.state()
    assert got.data['target_removed'] is False
    assert got.data['quality']['poses_used'] == 20
    state = env.svc.state()
    assert state['state'] == 'confirm_target_removed' and state['message_de'] == T.CONFIRM_TARGET_REMOVED_DE
    assert state['quality']['poses_used'] == 20
    assert env.camera.holds == 0
    # the solve ran on a worker thread, never on the thread that reported the program's end
    assert env.threads.count(SV.WORK_THREAD) == 1
    # a real program is refused until the target is confirmed removed (the target is off already)
    env.target_attached = False
    assert env.svc.program_start_gate('{}', 'student-1') == T.TARGET_NOT_CONFIRMED_DE
    ok, msg = env.svc.start('target_removed')
    assert (ok, msg) == (True, T.OK_DE)
    assert _stored(env).data['target_removed'] is True
    assert env.svc.state()['state'] == 'ok'
    assert env.svc.program_start_gate('{}', 'student-2') is None
    assert env.camera.holds == 1                   # the real run holds the studio picture ...
    env.svc.on_program_end('student-2', 'finished')
    assert env.camera.holds == 0                   # ... until it ends
    # the target is clipped on again and in view: the next mat check sees it and clears the flag
    env.put(0)
    env.target_attached = True
    ok, msg = env.svc.start('mat_check')
    assert (ok, msg) == (False, T.TARGET_ATTACHED_DE)
    assert _stored(env).data['target_removed'] is False
    env.target_attached = False
    assert env.svc.program_start_gate('{}', 'student-3') == T.TARGET_NOT_CONFIRMED_DE


@pytest.mark.parametrize('kind', KINDS)
def test_a_drooping_pose_is_not_captured_yet_reaches_the_solve_as_a_rested_record(kind, tmp_path, monkeypatch):
    """FAILS FIRST (D33, D42): at one pose the arm stands still 0.20 rad from its contract pose (joint 2): not
    captured, yet its joint error reaches the solve as a rested record; the run (every other gate passing)
    writes arm_check.json with droop_warning and shows the German droop warning — never a refusal."""
    env = _Env(kind, opt=NO_DROOP, calib_dir=tmp_path)
    env.kit.extra_sag = {i: np.array([0.0, -0.03, 0.0, 0.0, 0.0]) for i in range(20)}
    env.kit.extra_sag[7] = np.array([0.0, -0.20, 0.0, 0.0, 0.0])
    seen = {}
    real_run = AC.run

    def spy(records, looks, **kw):
        seen['records'] = list(records)
        return real_run(records, looks, **kw)
    monkeypatch.setattr(AC, 'run', spy)
    assert env.svc.start('arm_check')[0]
    assert env.play() == 'finished'
    rec7 = [r for r in seen['records'] if r.index == 7]
    assert len(rec7) == 1 and rec7[0].target is None
    assert abs(rec7[0].commanded[1] - rec7[0].measured[1]) == pytest.approx(0.20, abs=0.003)
    assert sum(r.target is not None for r in seen['records']) == 19
    got = _stored(env)
    assert got.status == 'ok', env.svc.state()
    assert got.data['droop_warning'] == {'joint': 2, 'deg': 11}
    warning = T.droop_warning_de(2, 11)
    state = env.svc.state()
    assert state['state'] == 'confirm_target_removed'
    assert state['droop_warning'] == {'joint': 2, 'deg': 11, 'message_de': warning}
    assert '[WARNUNG] ' + warning in env.logs
    # every later Roboter-Studio start carries it while the stored calibration has the flag
    env.target_attached = False
    env.home()
    assert env.svc.start('target_removed')[0]
    env.logs.clear()
    assert env.svc.program_start_gate('{}', 'student-1') is None
    assert '[WARNUNG] ' + warning in env.logs


@pytest.mark.parametrize('kind', KINDS)
def test_the_records_carry_the_encoders_never_the_commanded_joints(kind, tmp_path, monkeypatch):
    """FAILS FIRST (D84, the hold-out rule): every PoseRecord.measured is the arm's encoder reading. On this kit
    a run whose records carried the commanded joints as measured would be refused (the RMS / hold-out gates
    catch it only on measured joints); the service's run is stored. (A kit at three times the assumed droop,
    where the commanded joints are far from the encoders.)"""
    env = _Env(kind, opt=replace(QUIET, droop_scale=3.0), seed=6, calib_dir=tmp_path)
    seen = {}
    real_run = AC.run

    def spy(records, looks, **kw):
        seen['records'], seen['looks'] = list(records), list(looks)
        return real_run(records, looks, **kw)
    monkeypatch.setattr(AC, 'run', spy)
    encoders = {}
    original_put = env.put

    def put(i):
        original_put(i)
        encoders[i] = env.enc.copy()
    env.put = put
    assert env.svc.start('arm_check')[0]
    assert env.play() == 'finished'
    assert _stored(env).status == 'ok', env.svc.state()
    for r in seen['records']:
        assert np.allclose(r.measured, encoders[r.index], atol=1e-12)
        assert np.allclose(r.commanded, env.poses[r.index], atol=1e-12)
    commanded = [r._replace(measured=tuple(r.commanded)) for r in seen['records']]
    assert not real_run(commanded, seen['looks'], profile_id=PROFILE[kind]).ok


# ══════════════════════════════════════════════════════════════════════════
# guard_margin (S4 revision 4: the box guard with the ladder)
# ══════════════════════════════════════════════════════════════════════════

class _StubGuard:
    """A guard that refuses exactly the legs ``refuse(a, b)`` names (joints compared to 1e-6)."""

    def __init__(self, refuse):
        self.refuse = refuse
        self.legs = []

    def judge_leg(self, a, b, ga, gb):
        a, b = np.asarray(a, float)[:5], np.asarray(b, float)[:5]
        self.legs.append((a, b))
        if self.refuse(a, b):
            return FG.Verdict(True, 'too_close', FG.TOWER_TOO_CLOSE_DE, -0.001, 'column')
        return FG.Verdict(False, 'clear')


def _same(a, b):
    return np.allclose(np.asarray(a, float)[:5], np.asarray(b, float)[:5], atol=1e-6)


def _pair(x, y, a, b):
    return (_same(a, x) and _same(b, y)) or (_same(a, y) and _same(b, x))


@pytest.mark.parametrize('kind', KINDS)
def test_guard_margin_refuses_home_to_the_observation_pose(kind, tmp_path):
    """FAILS FIRST (R4): with a guard stub that refuses HOME <-> OBS the arm check fails with guard_margin
    and writes nothing."""
    prof = RP.ROBOT_PROFILES[PROFILE[kind]]
    home, obs = prof.home_joints_rad, SF.ARMS[kind]['observe_joints_rad']
    stubs = []
    real = _node_guard_parts(PROFILE[kind])

    def parts(calibration):
        model, _guard = real(calibration)
        stubs.append(_StubGuard(lambda a, b: _pair(home, obs, a, b)))
        return model, stubs[-1]
    env = _Env(kind, calib_dir=tmp_path, guard_parts=parts)
    assert env.svc.start('arm_check')[0]
    assert env.play() == 'finished'
    assert stubs and any(_pair(home, obs, a, b) for a, b in stubs[0].legs)
    state = env.svc.state()
    assert state['code'] == 'guard_margin' and state['message_de'] == T.GUARD_MARGIN_DE
    assert _stored(env).status == 'none'


@pytest.mark.parametrize('kind', KINDS)
def test_guard_margin_refuses_a_hover_with_neither_rung(kind, tmp_path):
    """FAILS FIRST (R4): with a stub that refuses both the direct line and the HOME rung of one hover the arm
    check fails with guard_margin and writes nothing; refusing only the direct line passes (the ladder)."""
    prof = RP.ROBOT_PROFILES[PROFILE[kind]]
    home, obs = prof.home_joints_rad, SF.ARMS[kind]['observe_joints_rad']
    hover = {}
    real = _node_guard_parts(PROFILE[kind])

    def factory(both):
        def parts(calibration):
            model, _guard = real(calibration)
            if 'q' not in hover:
                hover['q'] = SV.guard_margin_hovers(model, PROFILE[kind])[17]
            q = hover['q']

            def refuse(a, b):
                return _pair(obs, q, a, b) or (both and _pair(q, home, a, b))
            return model, _StubGuard(refuse)
        return parts
    env = _Env(kind, calib_dir=tmp_path / 'direct', guard_parts=factory(False))
    assert env.svc.start('arm_check')[0]
    assert env.play() == 'finished'
    assert _stored(env).status == 'ok', env.svc.state()
    env = _Env(kind, calib_dir=tmp_path / 'both', guard_parts=factory(True))
    assert env.svc.start('arm_check')[0]
    assert env.play() == 'finished'
    assert env.svc.state()['code'] == 'guard_margin'
    assert _stored(env).status == 'none'


def _nominal_calibration(kind, offsets=(0.0,) * 5):
    env = SF.TOWER_DROOP_DESIGN_ENV_RAD[kind]
    return {'joint_offsets_rad': list(offsets), 'axis_tilts_rad': [0.0] * 6, 'tilt': [0.0, 0.0],
            'tower_pose': [0.0, 0.0, 0.0], 'droop_envelope_rad': [0.0, env[0], env[1], env[2], 0.0],
            'contract_hash': SF.contract_hash()}


@pytest.mark.parametrize('kind', KINDS)
def test_guard_margin_passes_the_nominal_arms_with_the_real_guard(kind):
    """On the nominal arm (the design envelope, the calibrated residual, the contract place) the real guard
    passes (a), (b) and (c); the V1's 6 refused direct hovers pass via HOME (p16, WP-02 (3)); so do two kits
    with joint zero offsets of about 1 deg."""
    pid = PROFILE[kind]
    parts = _node_guard_parts(pid)
    ok, detail = SV.guard_margin(_nominal_calibration(kind), RP.ROBOT_PROFILES[pid], parts)
    assert ok, detail
    assert detail['hovers'] == 78
    assert detail['via_home'] == (6 if kind == 'v1' else 0)
    for d in ((0.0, 0.017, -0.015, 0.012, 0.0), (0.0, -0.02, 0.018, -0.01, 0.0)):
        ok, detail = SV.guard_margin(_nominal_calibration(kind, d), RP.ROBOT_PROFILES[pid], parts)
        assert ok, (d, detail)
    model, guard = parts(_nominal_calibration(kind))
    assert guard.calibrated and guard.residual_m == SF.TOWER_RESIDUAL_SERVER_M[kind]
    assert model.tool_tip_below_ee_m == SF.FINGERTIP_BELOW_EE_M[kind]
    # the nominal arm's smallest margins (exact, dense) are at least p16's smallest over 240 kits at the design
    # envelope: V1 HOME <-> OBS 9.4 mm, route 28.2 mm, hover ladder 3.0 mm; OMX 38.3 / 36.7 / 21.0 mm
    prof = RP.ROBOT_PROFILES[pid]
    home, obs = list(prof.home_joints_rad), list(prof.observe_pose_joints)
    g_open, g_closed = prof.gripper_open_rad, prof.gripper_closed_rad

    def margin(a, b, g):
        return guard.smallest_margin(a, b, g, g).margin_m
    route = [home] + [list(q) for q in SF.ARMS[kind]['arm_check_poses_rad']] + [home]
    got = (min(margin(home, obs, g) for g in (g_open, g_closed)),
           min(margin(a, b, g_closed) for a, b in zip(route[:-1], route[1:])),
           min(max(margin(obs, h, g_open), margin(h, home, g_open)) for h in SV.guard_margin_hovers(model, pid)))
    floors = {'v1': (0.0094, 0.0282, 0.0030), 'omx': (0.0383, 0.0367, 0.0210)}[kind]
    assert all(m >= f - 0.0005 for m, f in zip(got, floors)), (got, floors)


def test_the_gate_judges_build_never_droop():
    """D42: the gate's droop envelope is the kit's own, capped at the design envelope (an arm sagging more
    than designed is warned, never refused here)."""
    cal = _nominal_calibration('v1')
    cal['droop_envelope_rad'] = [0.0, 0.20, 0.004, 0.03, 0.0]
    assert SV.guard_margin_envelope(cal, 'v1') == [0.027, 0.004, 0.01]
    _model, guard = _node_guard_parts('v1_studio')(cal)
    assert guard.envelope_rad == (0.027, 0.004, 0.01)


def test_the_hover_grid_is_s4_s():
    """(c)'s grid: 6 radii zone_inner .. reach x 13 azimuths -90 .. +90 deg, roll 0, fingertip MAT + 0.075."""
    assert SV.GUARD_HOVER_RADII == 6 and SV.GUARD_HOVER_AZIMUTHS == 13
    assert SV.GUARD_HOVER_ABOVE_MAT_M == 0.075


# ══════════════════════════════════════════════════════════════════════════
# the mat check and the program-start gate
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('kind', KINDS)
def test_a_mat_check_with_no_statement_answers_profile_unknown(kind, tmp_path):
    """FAILS FIRST (R4, D63, 55 m-11): no statement yet -> profile_unknown with S4's sentence, at once (revision
    3 waited 3 s and answered camera_not_tower); a legacy statement -> camera_not_tower. No frame is grabbed."""
    env = _Env(kind, calib_dir=tmp_path, camera=_Camera(profile=None))
    assert env.svc.start('mat_check') == (False, T.PROFILE_UNKNOWN_DE)
    state = env.svc.state()
    assert state['code'] == 'profile_unknown' and state['state'] == 'blocked'
    assert env.camera.frames == 0
    assert env.clock.t == 1000.0
    env = _Env(kind, calib_dir=tmp_path, camera=_Camera(profile='legacy'))
    assert env.svc.start('mat_check') == (False, T.CAMERA_NOT_TOWER_DE)
    assert env.svc.state()['code'] == 'camera_not_tower'


@pytest.mark.parametrize('kind', KINDS)
def test_the_mat_check_takes_three_frames_and_keeps_its_result(kind, tmp_path):
    """Three full-frame looks, nothing more (the bound of a calibration callback on the default group); the
    result goes to mat_check_last.json; no arm check yet -> needs_arm_check/none."""
    env = _Env(kind, calib_dir=tmp_path)
    env.home()
    env.target_attached = False
    assert env.svc.start('mat_check') == (False, T.NEEDS_ARM_CHECK_NONE_DE)
    assert env.detects == [None] * SV.MAT_CHECK_FRAMES
    assert env.svc.state()['state'] == 'needs_arm_check'
    last = ST.Store(str(tmp_path)).read_mat_check_last()
    assert last['code'] == 'needs_arm_check/none'


def _calibrated(kind, tmp_path):
    """A kit whose stored calibration is its ideal one, target removed."""
    env = _Env(kind, calib_dir=tmp_path)
    cal = S.ideal_calibration(env.kit)
    cal['profile_id'] = PROFILE[kind]
    ST.Store(str(tmp_path)).write(cal)
    env.home()
    env.target_attached = False
    return env


@pytest.mark.parametrize('kind', KINDS)
def test_a_program_start_with_hidden_tags_uses_the_last_ok_result_once_warned_and_refuses_without_one(
        kind, tmp_path):
    """FAILS FIRST: tags_hidden at a program start with no ok mat check this boot -> refused with the sentence;
    after an ok one -> the start goes on with that result and one [WARNUNG]."""
    env = _calibrated(kind, tmp_path)
    env.mat_visible = False
    assert env.svc.program_start_gate('{}', 'p1') == T.FIXTURE_HIDDEN_DE
    env.mat_visible = True
    assert env.svc.program_start_gate('{}', 'p2') is None
    first = env.svc.run_calibration()
    assert first is not None and first['image_size'] == (1920, 1080)
    env.svc.on_program_end('p2', 'finished')
    env.mat_visible = False
    env.logs.clear()
    assert env.svc.program_start_gate('{}', 'p3') is None
    assert env.logs == [T.LAST_RESULT_WARNING_DE]
    assert np.array_equal(env.svc.run_calibration()['T_cam_to_w'], first['T_cam_to_w'])


@pytest.mark.parametrize('kind', KINDS)
def test_the_gate_lets_the_arm_check_program_through_and_nothing_else(kind, tmp_path):
    """The internal program starts without the mat check (its own gate is the camera state); a student start
    with the same id and text is gated as any real start."""
    env = _Env(kind, calib_dir=tmp_path)
    assert env.svc.start('arm_check')[0]
    payload, wid = env.programs[0]
    assert env.detects == []
    env.svc.on_program_end(wid, 'stopped')
    assert env.svc.program_start_gate(json.dumps(payload), wid) == T.NEEDS_ARM_CHECK_NONE_DE
    legacy = _Env(kind, calib_dir=tmp_path, camera=_Camera(profile='legacy'))
    assert legacy.svc.program_start_gate('{}', 'x') is None and legacy.svc.run_calibration() is None
    unknown = _Env(kind, calib_dir=tmp_path, camera=_Camera(profile=None))
    assert unknown.svc.program_start_gate('{}', 'x') == T.PROFILE_UNKNOWN_DE


# ══════════════════════════════════════════════════════════════════════════
# stop, cancel and the state
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('kind', KINDS)
def test_stop_or_cancel_writes_nothing_and_leaves_the_arm(kind, tmp_path):
    """„Stopp" (the program ends stopped) or /calibration/cancel: nothing written, no home program, the
    stopped sentence."""
    env = _Env(kind, calib_dir=tmp_path)
    assert env.svc.start('arm_check')[0]
    env.play(upto=3, finish=False)
    env.svc.on_program_end(env.wid, 'stopped')
    assert env.svc.state()['message_de'] == T.STOPPED_DE and len(env.programs) == 1
    assert _stored(env).status == 'none' and env.camera.holds == 0
    env = _Env(kind, calib_dir=tmp_path / 'b')
    assert env.svc.start('arm_check')[0]
    env.play(upto=2, finish=False)
    assert env.svc.cancel() == (True, T.STOPPED_DE)
    assert env.stops == [env.wid] and len(env.programs) == 1
    assert env.svc.cancel() is None                # nothing of the service's runs any more


def test_the_state_carries_s4_s_keys(tmp_path):
    env = _Env('v1', calib_dir=tmp_path)
    state = env.svc.state()
    assert list(state)[:3] == ['v', 'state', 'code']
    assert {'v', 'state', 'code', 'message_de', 'notes', 'camera', 'arm_check', 'handheld', 'quality',
            'fixture'} <= set(state)
    assert state['v'] == 1 and state['fixture'] == {'id': 'tower_v1', 'lift_m': 0.0072}
    assert state['camera']['state'] == 'streaming'
    assert json.loads(env.svc.state_json()) == state
    assert SV.STATE_TOPIC == '/edubotics/calibration_state'


def test_the_click_maps_the_outside_pixel_to_the_studio_pixel():
    """A17: u_full = 2u + 320.5, v_full = 2v + 60.5 (from the contract's OUTSIDE window), and back."""
    assert SV.studio_pixel(100, 200) == (520.5, 460.5)
    assert SV.outside_pixel(520.5, 460.5) == (100.0, 200.0)


class _FakePerception:
    """Reports one tag at fixed corners of whatever image it is handed, and the images it got."""

    def __init__(self):
        self.images = []

    def detect(self, image, camera, mode):
        self.images.append((image.shape, camera, mode))
        corners = [[10.0, 20.0], [30.0, 20.0], [30.0, 40.0], [10.0, 40.0]]
        return [types.SimpleNamespace(aruco_id=7, corners_px=corners, confidence=0.42),
                types.SimpleNamespace(aruco_id=None, corners_px=corners, confidence=0.9)]


def test_detect_tags_reports_a_region_s_tags_in_full_frame_pixels():
    """A region is cut out of the frame (clipped to it) and its corners come back offset by the region's
    origin; the margin is the detection's confidence × 100; a tag without an id is dropped; the frame's mean
    grey only for a full-frame look."""
    frame = np.full((1080, 1920, 3), 200, dtype=np.uint8)
    p = _FakePerception()
    whole = SV.detect_tags(p, frame)
    assert (whole.width, whole.height, whole.mean_grey) == (1920, 1080, 200.0)
    np.testing.assert_array_equal(whole.tags[7].corners, [[10, 20], [30, 20], [30, 40], [10, 40]])
    assert set(whole.tags) == {7} and whole.tags[7].margin == pytest.approx(42.0)
    part = SV.detect_tags(p, frame, roi=(500.4, 300.6, 700.2, 400.0))
    np.testing.assert_array_equal(part.tags[7].corners, [[510, 320], [530, 320], [530, 340], [510, 340]])
    assert (part.width, part.height, part.mean_grey) == (1920, 1080, 0.0)
    edge = SV.detect_tags(p, frame, roi=(-50.0, 1000.0, 2000.0, 1200.0))
    np.testing.assert_array_equal(edge.tags[7].corners[0], [10, 1020])
    assert [s for s, _c, _m in p.images] == [(1080, 1920, 3), (101, 202, 3), (80, 1920, 3)]
    assert {(c, m) for _s, c, m in p.images} == {('scene', 'apriltag')}


def test_detect_tags_with_the_real_detector_finds_a_tag_at_the_same_pixels_in_a_region():
    """The production detector (``Perception``: pupil_apriltags + cornerSubPix) reads a printed tag36h11 tag
    on a full-resolution frame; looked at through a region around it, the corners land on the same full-frame
    pixels."""
    import cv2
    from pupil_required import require_pupil
    require_pupil()
    from physical_ai_server.workflow.perception import Perception
    perception = Perception()
    assert perception.apriltag_available()
    marker = cv2.aruco.generateImageMarker(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36H11),
                                           21, 160, borderBits=1)
    frame = np.full((1080, 1920, 3), 230, dtype=np.uint8)
    frame[513:673, 1203:1363] = marker[..., None]
    whole = SV.detect_tags(perception, frame)
    part = SV.detect_tags(perception, frame, roi=(1100.0, 450.0, 1450.0, 750.0))
    assert set(whole.tags) == set(part.tags) == {21}
    np.testing.assert_allclose(part.tags[21].corners, whole.tags[21].corners, rtol=0, atol=0.05)
    centre = whole.tags[21].corners.mean(axis=0)
    np.testing.assert_allclose(centre, (1203 + 79.5, 513 + 79.5), rtol=0, atol=0.5)


def test_the_new_sentence_follows_the_german_rules():
    import importlib.util
    from pathlib import Path
    repo = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location('_gdl_service', repo / '.github' / 'scripts' / 'german_detail_lint.py')
    lint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lint)
    for text in (SV.ARM_CHECK_BUSY_DE, SV.CHECK_FAILED_DE):
        assert not lint.TRANSLITERATIONS.search(text)
        assert lint.GERMAN_CHARS.search(text) or lint.GERMAN_WORDS.search(text)
        assert not lint.ENGLISH_WORDS.search(text) or lint.GERMAN_CHARS.search(text)
        assert '"' not in text and "'" not in text
    assert SV.ARM_CHECK_BUSY_DE == 'Der Arm-Check läuft gerade – bitte warten, bis er fertig ist.'
    assert SV.CHECK_FAILED_DE == 'Die Kalibrierung konnte gerade nicht geprüft werden. Bitte noch einmal versuchen.'


# S4's numbers of the service, written as literals (the capture rule, the program, guard_margin's grid).
_SERVICE_NUMBERS = (
    ('CAPTURE_PERIOD_S', 0.1), ('STILL_WINDOW_S', 0.3), ('STILL_TOL_RAD', 0.002), ('CAPTURE_LOOKS', 3),
    ('LOOK_STABLE_PX', 0.4), ('REGION_MARGIN_PX', 120.0), ('POSE1_CHECK_S', 3.0), ('MAT_CHECK_FRAMES', 3),
    ('ARM_CHECK_TEMPO', 0.5), ('FIRST_DWELL_S', 4.0), ('DWELL_S', 2.5), ('GUARD_HOVER_RADII', 6),
    ('GUARD_HOVER_AZIMUTHS', 13), ('GUARD_HOVER_ABOVE_MAT_M', 0.075),
)


@pytest.mark.parametrize('name,value', _SERVICE_NUMBERS)
def test_the_service_s_numbers_are_s4_s(name, value):
    assert getattr(SV, name) == value


@pytest.mark.parametrize('kind', KINDS)
def test_a_check_that_raises_answers_in_german_and_never_raises(kind, tmp_path):
    """A detector that raises: the step answers the check_failed sentence, a program start is refused with it."""
    env = _Env(kind, calib_dir=tmp_path)

    def broken(_frame, _roi=None):
        raise RuntimeError('decoder')
    env.svc._detect = broken
    assert env.svc.start('mat_check') == (False, SV.CHECK_FAILED_DE)
    assert env.svc.state()['code'] == 'check_failed'
    assert env.svc.program_start_gate('{}', 'p1') == SV.CHECK_FAILED_DE


# ══════════════════════════════════════════════════════════════════════════
# the node's wiring (physical_ai_server.py needs rclpy: its methods are exec'd
# from the source onto stub nodes)
# ══════════════════════════════════════════════════════════════════════════

_NODE_PY = Path(SV.__file__).resolve().parents[2] / 'physical_ai_server.py'
_NODE_SRC = _NODE_PY.read_text(encoding='utf-8')
_STUDIO = 'scene_studio'


def _node_fns(*names, extra=None):
    ns = {'math': math, 'os': os, 'json': json, 'threading': threading, 'time': time, 'robot_profiles': RP,
          'STUDIO_CAMERA': _STUDIO}
    ns.update(extra or {})
    found = {}
    for node in ast.walk(ast.parse(_NODE_SRC)):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            exec(compile(textwrap.dedent(ast.get_source_segment(_NODE_SRC, node)), str(_NODE_PY), 'exec'), ns)
            found[node.name] = ns[node.name]
    assert set(found) == set(names), set(names) - set(found)
    return found


class _Logger:
    def __init__(self):
        self.lines = []

    def __getattr__(self, level):
        return lambda msg, *a, **k: self.lines.append((level, str(msg)))


class _StubNode:
    def __init__(self, *names, extra=None, profile_id='v1_studio'):
        self._arm_profile = RP.ROBOT_PROFILES[profile_id]
        self.logger = _Logger()
        for name, fn in _node_fns(*names, extra=extra).items():
            setattr(self, name, types.MethodType(fn, self))

    def get_logger(self):
        return self.logger


def _tower_calibration(kind='v1'):
    """What the gate approves for a run: the contract camera with the nominal lens."""
    from physical_ai_server.workflow.autocal import chain as CH
    from physical_ai_server.workflow.autocal import mat_check as MC
    from physical_ai_server.workflow.autocal.camera_model import KB
    R, t = MC.contract_camera(CH.BASE_AXIS_X[kind])
    T = np.eye(4)
    T[:3, :3] = R.T
    T[:3, 3] = -R.T @ t
    lens = SF.LENS_NOMINAL
    kb = KB(lens['f_px'], lens['aspect'], lens['cx'], lens['cy'], 0.03, -0.02)
    return {'intrinsics': kb.to_intrinsics_dict(), 'T_cam_to_w': T, 'image_size': (1920, 1080)}


class _ApprovingService:
    def __init__(self, calibration, scope=True):
        self.calibration, self.scope = calibration, scope

    def run_calibration(self):
        return self.calibration

    def tower_scope(self):
        return self.scope


def _legacy_yamls(calib_dir):
    import cv2
    fs = cv2.FileStorage(os.path.join(calib_dir, 'scene_intrinsics.yaml'), cv2.FILE_STORAGE_WRITE)
    fs.write('camera_matrix', np.array([[600.0, 0, 320], [0, 600.0, 240], [0, 0, 1]]))
    fs.write('distortion_coefficients', np.zeros((1, 5)))
    fs.write('source', 'charuco')
    fs.release()
    fs = cv2.FileStorage(os.path.join(calib_dir, 'scene_handeye.yaml'), cv2.FILE_STORAGE_WRITE)
    fs.write('transform', np.eye(4))
    fs.release()


def test_load_workflow_calibration_takes_the_tower_calibration_the_gate_approved(tmp_path, monkeypatch):
    """FAILS FIRST: a run the gate approved gets the tower camera (its own picture), the arm check's lens, the
    mat check's pose and image_size, and no legacy YAML or ground-truth correction; under the tower's scope
    without an approval no legacy YAML either; under legacy today's YAMLs."""
    from physical_ai_server.workflow.autocal.camera_model import KBDistortion
    monkeypatch.setenv('EDUBOTICS_CALIB_DIR', str(tmp_path))
    _legacy_yamls(str(tmp_path))
    cal = _tower_calibration()
    node = _StubNode('_load_workflow_calibration')
    node._get_or_create_calibration_manager = lambda: None
    node.scene_calibration = _ApprovingService(cal)
    got = node._load_workflow_calibration()
    assert isinstance(got['scene_intrinsics']['dist'], KBDistortion)
    assert got['scene_extrinsics'] is cal['T_cam_to_w'] and got['image_size'] == (1920, 1080)
    assert node._workflow_scene_camera == _STUDIO
    assert got['board_table_z'] == SF.MAT_THICKNESS_M and got['xy_correction'] is None
    node.scene_calibration = _ApprovingService(None)
    got = node._load_workflow_calibration()
    assert 'scene_intrinsics' not in got and 'scene_extrinsics' not in got and 'image_size' not in got
    assert node._workflow_scene_camera == 'scene'
    node.scene_calibration = _ApprovingService(None, scope=False)
    got = node._load_workflow_calibration()
    assert got['scene_intrinsics']['K'][0, 0] == 600.0 and 'image_size' not in got


class _Perception:
    def apriltag_available(self):
        return True

    def detect(self, bgr, camera, mode, aruco_id=None):
        return []


def _count_program():
    return {'blocks': {'languageVersion': 0, 'blocks': [{
        'type': 'edubotics_log', 'id': 'log',
        'inputs': {'MESSAGE': {'block': {'type': 'edubotics_count_object', 'id': 'n',
                                          'fields': {'OBJECT_TYPE': 'wuerfel'}}}}}]},
        'destinations': []}


def _run_manager(load_calibration, frame):
    from physical_ai_server.workflow.object_catalog import fixed_catalog
    from physical_ai_server.workflow.workflow_manager import WorkflowManager
    status, finished = [], []
    mgr = WorkflowManager(
        publisher=lambda _pts: None, perception_factory=_Perception, load_calibration=load_calibration,
        emit_status=status.append, on_finished=finished.append, get_scene_frame=lambda: frame,
        get_scene_frame_age=lambda: 0.0, get_follower_joints=lambda: [0.0, 0.64, 0.64, 0.0, 0.0, 0.9],
        load_object_catalog=fixed_catalog)
    ok, msg, _ = mgr.start(json.dumps(_count_program()), 'g4')
    assert ok, msg
    deadline = time.time() + 20.0
    while not finished and time.time() < deadline:
        time.sleep(0.01)
    return finished[0], [s.get('error') for s in status if s.get('phase') == 'error']


def test_a_640x480_frame_against_the_1920x1080_calibration_is_refused_in_german(tmp_path, monkeypatch):
    """FAILS FIRST (guard G4 with its mutation): a run with the tower calibration (image_size 1920 x 1080, through
    the node's own _load_workflow_calibration) is handed a 640 x 480 frame -> the looking block refuses with the
    German image-size sentence; a 1920 x 1080 frame is looked at."""
    monkeypatch.setenv('EDUBOTICS_CALIB_DIR', str(tmp_path))
    node = _StubNode('_load_workflow_calibration')
    node._get_or_create_calibration_manager = lambda: None
    node.scene_calibration = _ApprovingService(_tower_calibration())
    phase, errors = _run_manager(node._load_workflow_calibration, np.zeros((480, 640, 3), np.uint8))
    assert phase == 'error' and errors == [T.IMAGE_SIZE_DE]
    phase, errors = _run_manager(node._load_workflow_calibration, np.zeros((1080, 1920, 3), np.uint8))
    assert phase == 'finished' and errors == []


def test_a_legacy_run_looks_at_a_frame_of_any_size(tmp_path, monkeypatch):
    monkeypatch.setenv('EDUBOTICS_CALIB_DIR', str(tmp_path))
    phase, errors = _run_manager(lambda: {}, np.zeros((480, 640, 3), np.uint8))
    assert phase == 'finished' and errors == []


class _Wfm:
    def __init__(self):
        self.pins = []

    def set_destination(self, name, x, y, z, plane_tracked=False):
        self.pins.append((name, x, y, z, plane_tracked))


def _click_node(svc, wfm):
    node = _StubNode('mark_destination_callback')
    node.scene_calibration = svc
    node._get_or_create_calibration_manager = lambda: None
    node._get_or_create_workflow_manager = lambda: wfm
    return node


def test_a_click_on_the_scene_stream_is_projected_from_the_studio_pixel(tmp_path, monkeypatch):
    """FAILS FIRST (A17): a click at (100, 200) on camera='scene' is projected from studio pixel (520.5, 460.5)
    with the tower calibration onto the mat; its height is the fixture's grasp height; the Ziel is stored."""
    from physical_ai_server.workflow import projection
    cal = _tower_calibration()
    svc = SV.SceneCalibration(RP.ROBOT_PROFILES['v1_studio'], _Camera(), str(tmp_path), detect=None,
                              get_joints=None, fk=None, guard_parts=None, start_program=None, stop_program=None)
    monkeypatch.setattr(svc, '_usable_calibration', lambda: (cal, [], None))
    calls = []
    real = projection.project_pixel_to_table

    def spy(px, py, K, dist, T, z, plane=None):
        calls.append((px, py, z))
        return real(px, py, K, dist, T, z, plane)
    monkeypatch.setattr(projection, 'project_pixel_to_table', spy)
    wfm = _Wfm()
    node = _click_node(svc, wfm)
    req = types.SimpleNamespace(camera='scene', pixel_x=100, pixel_y=200, label='Ablage')
    res = node.mark_destination_callback(req, types.SimpleNamespace())
    assert res.success, res.message
    assert calls == [(520.5, 460.5, SF.MAT_THICKNESS_M)]
    want = real(520.5, 460.5, cal['intrinsics']['K'], cal['intrinsics']['dist'], cal['T_cam_to_w'],
                SF.MAT_THICKNESS_M)
    assert (res.world_x, res.world_y) == pytest.approx((want[0], want[1]), abs=1e-12)
    assert res.world_z == SF.MAT_THICKNESS_M + SF.FINGERTIP_BELOW_EE_M['v1']
    assert wfm.pins == [('Ablage', res.world_x, res.world_y, res.world_z, True)]
    # refused with the calibration's own sentence when the mat check refuses
    monkeypatch.setattr(svc, '_usable_calibration', lambda: (None, [], T.NEEDS_ARM_CHECK_NONE_DE))
    res = node.mark_destination_callback(req, types.SimpleNamespace())
    assert not res.success and res.message == T.NEEDS_ARM_CHECK_NONE_DE and len(wfm.pins) == 1
    # under legacy the click is today's (here: no legacy calibration manager)
    svc_legacy = SV.SceneCalibration(RP.ROBOT_PROFILES['v1_studio'], _Camera(profile='legacy'), str(tmp_path),
                                     detect=None, get_joints=None, fk=None, guard_parts=None, start_program=None,
                                     stop_program=None)
    res = _click_node(svc_legacy, wfm).mark_destination_callback(req, types.SimpleNamespace())
    assert not res.success and res.message == 'Kamera scene ist nicht kalibriert.'


def test_the_real_manager_asks_the_gate_and_reports_the_run_s_end():
    """FAILS FIRST: WorkflowManager(start_gate=…) refuses a start with the gate's sentence (nothing runs, no end
    reported); a start the gate approved reports its end after on_finished, and one that never became a run
    reports (id, None)."""
    from physical_ai_server.workflow.workflow_manager import WorkflowManager
    order, finished = [], []
    gate = {'answer': T.TARGET_NOT_CONFIRMED_DE}

    def make(ik_factory=None):
        return WorkflowManager(publisher=lambda _p: None, ik_factory=ik_factory,
                               start_gate=lambda j, w: (order.append(('gate', w)) or gate['answer']),
                               on_finished=lambda phase: (order.append(('finished', phase)), finished.append(phase)),
                               on_run_end=lambda w, phase: order.append(('end', w, phase)),
                               get_follower_joints=lambda: [0.0, 0.64, 0.64, 0.0, 0.0, 0.9])
    program = json.dumps({'blocks': {'languageVersion': 0, 'blocks': []}})
    mgr = make()
    assert mgr.start(program, 'w1') == (False, T.TARGET_NOT_CONFIRMED_DE, [])
    assert order == [('gate', 'w1')] and not mgr.is_running
    gate['answer'] = None
    order.clear()
    ok, _msg, _ = mgr.start(program, 'w2')
    assert ok
    deadline = time.time() + 10.0
    while len(order) < 3 and time.time() < deadline:
        time.sleep(0.01)
    assert order == [('gate', 'w2'), ('finished', 'finished'), ('end', 'w2', 'finished')]
    assert mgr.workflow_id == 'w2'
    order.clear()

    def broken():
        raise RuntimeError('no solver')
    ok, _msg, _ = make(ik_factory=broken).start(program, 'w3')
    assert not ok and order == [('gate', 'w3'), ('end', 'w3', None)]


def _keywords(fn_name):
    fn = next(n for n in ast.walk(ast.parse(_NODE_SRC)) if isinstance(n, ast.FunctionDef) and n.name == fn_name)
    call = next(n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, 'id', None) == 'WorkflowManager')
    return {k.arg: ast.unparse(k.value) for k in call.keywords}


def test_only_the_real_manager_has_the_gate_and_the_run_end():
    real = _keywords('_get_or_create_workflow_manager')
    assert real['start_gate'] == "getattr(self, '_workflow_start_gate', None)"
    assert real['on_run_end'] == "getattr(self, '_on_workflow_run_end', None)"
    sim = _keywords('_get_or_create_sim_workflow_manager')
    assert 'start_gate' not in sim and 'on_run_end' not in sim


def test_a_tower_run_reads_the_studio_picture_and_its_end_returns_to_the_outside_stream():
    """The real manager's frame and age getters read the run's scene camera: the tower camera's own picture
    while a run has the tower calibration, the outside stream again once it ended."""
    from physical_ai_server.workflow.workflow_manager import WorkflowManager
    asked = []
    node = _StubNode('_get_or_create_workflow_manager', '_on_workflow_run_end',
                     extra={'WorkflowManager': WorkflowManager})
    node.workflow_manager = None
    for name in ('_trajectory_publisher', '_build_ik_solver', '_build_perception', '_load_workflow_calibration',
                 '_emit_workflow_status', '_on_workflow_finished', '_get_current_gripper_xyz',
                 '_load_object_catalog', '_workflow_start_gate'):
        setattr(node, name, lambda *a, **k: None)
    node.communicator = None
    node._get_latest_camera_frame = lambda camera: asked.append(('frame', camera))
    node._get_camera_frame_age = lambda camera: asked.append(('age', camera))
    ended = []
    node.scene_calibration = types.SimpleNamespace(on_program_end=lambda w, p: ended.append((w, p)))
    mgr = node._get_or_create_workflow_manager()
    mgr._get_scene_frame()
    node._workflow_scene_camera = _STUDIO
    mgr._get_scene_frame()
    mgr._get_scene_frame_age()
    node._on_workflow_run_end('w', 'finished')
    mgr._get_scene_frame()
    assert asked == [('frame', 'scene'), ('frame', _STUDIO), ('age', _STUDIO), ('frame', 'scene')]
    assert ended == [('w', 'finished')]


class _Msg(types.SimpleNamespace):
    pass


def test_the_overlay_gets_outside_pixels_from_a_tower_run():
    """A17: the page shows the outside stream, so a detection of a run on the tower camera's own picture reaches
    the overlay in outside pixels; the outside stream's own detections are unchanged."""
    from physical_ai_server.workflow.perception import Detection
    published = []
    node = _StubNode('_emit_workflow_status', extra={'WorkflowStatus': _Msg, 'Detection': _Msg})
    node.workflow_status_publisher = types.SimpleNamespace(publish=published.append)
    seen = []
    node.scene_calibration = types.SimpleNamespace(on_workflow_status=seen.append)
    det = Detection(centroid_px=(520.5, 460.5), bbox_px=(500, 440, 40, 30), confidence=0.8, label='tag20',
                    aruco_id=20)
    node._workflow_scene_camera = _STUDIO
    node._emit_workflow_status({'detections': [det]})
    d = published[-1].active_detections[0]
    assert (d.cx, d.cy, d.w, d.h) == (100, 200, 20, 15)
    node._workflow_scene_camera = 'scene'
    node._emit_workflow_status({'detections': [det]})
    d = published[-1].active_detections[0]
    assert (d.cx, d.cy, d.w, d.h) == (520, 460, 40, 30)
    assert len(seen) == 2


class _Service:
    STEPS = SV.STEPS

    def __init__(self, cancel=None):
        self.calls = []
        self._cancel = cancel

    def start(self, step):
        self.calls.append(step)
        return True, f'step {step}'

    def cancel(self):
        return self._cancel

    def status(self):
        return True, T.OK_DE


def test_the_calibration_services_route_the_tower_steps_to_the_service():
    """/calibration/start: the three tower steps go to the service (no on_calibration, no legacy manager);
    /calibration/cancel stops the service's arm check first; /calibration/status under tower_v1 answers the
    arm check's validity with the state's sentence."""
    node = _StubNode('calibration_start_callback', 'calibration_cancel_callback', 'calibration_status_callback')
    node.scene_calibration = _Service(cancel=(True, T.STOPPED_DE))
    node.studio_camera = _Camera()
    node._assert_no_other_active = lambda mode: pytest.fail('the tower steps take no calibration mode')
    for step in SV.STEPS:
        res = node.calibration_start_callback(types.SimpleNamespace(camera='scene', step=step),
                                              types.SimpleNamespace())
        assert (res.success, res.message) == (True, f'step {step}')
    assert node.scene_calibration.calls == list(SV.STEPS)
    res = node.calibration_cancel_callback(types.SimpleNamespace(camera=''), types.SimpleNamespace())
    assert (res.success, res.message) == (True, T.STOPPED_DE)
    res = node.calibration_status_callback(types.SimpleNamespace(), types.SimpleNamespace(has_table_plane=False))
    assert res.success and res.has_scene_intrinsics and res.has_scene_handeye and res.has_table_plane
    assert res.message == T.OK_DE
    # a legacy step still reaches the legacy path
    node._assert_no_other_active = lambda mode: (False, 'refused by the arbiter')
    res = node.calibration_start_callback(types.SimpleNamespace(camera='scene', step='intrinsic'),
                                          types.SimpleNamespace())
    assert (res.success, res.message) == (False, 'refused by the arbiter')


def test_the_state_topic_is_latched_republished_and_published_on_change():
    """/edubotics/calibration_state: RELIABLE + TRANSIENT_LOCAL depth 1, a 1 Hz timer on its own callback group,
    and the service's state JSON on every publish."""
    made = {}

    class _QoS(types.SimpleNamespace):
        pass
    ns = {'QoSProfile': _QoS, 'HistoryPolicy': types.SimpleNamespace(KEEP_LAST='keep_last'),
          'ReliabilityPolicy': types.SimpleNamespace(RELIABLE='reliable'),
          'DurabilityPolicy': types.SimpleNamespace(TRANSIENT_LOCAL='transient_local'),
          'String': _Msg, 'MutuallyExclusiveCallbackGroup': lambda: 'own-group'}
    node = _StubNode('_init_calibration_state', '_publish_calibration_state', extra=ns)
    published = []
    node.scene_calibration = types.SimpleNamespace(state_json=lambda: '{"v":1}')
    node.create_publisher = lambda typ, topic, qos: made.update(topic=topic, qos=qos) or types.SimpleNamespace(
        publish=published.append)
    node.create_timer = lambda period, cb, callback_group=None: made.update(period=period, cb=cb,
                                                                              group=callback_group)
    node._init_calibration_state()
    assert made['topic'] == SV.STATE_TOPIC
    assert (made['qos'].depth, made['qos'].reliability, made['qos'].durability) == (1, 'reliable', 'transient_local')
    assert made['period'] == 1.0 and made['group'] == 'own-group'
    assert [m.data for m in published] == ['{"v":1}']
    made['cb']()
    assert len(published) == 2


def test_the_internal_program_takes_the_ordinary_start_and_only_its_own_run_is_stopped():
    node = _StubNode('_start_calibration_program', '_stop_calibration_program')
    seen = []

    def start(request, response):
        seen.append((request.workflow_json, request.workflow_id))
        response.success, response.message = True, 'Workflow gestartet.'
        return response
    node.workflow_start_callback = start
    assert node._start_calibration_program('{"x":1}', 'armcheck-1') == (True, 'Workflow gestartet.')
    assert seen == [('{"x":1}', 'armcheck-1')]
    stopped = []
    node.workflow_manager = types.SimpleNamespace(is_running=True, workflow_id='student-7',
                                                  stop=lambda: stopped.append(1))
    node._stop_calibration_program('armcheck-1')
    assert stopped == []
    node.workflow_manager.workflow_id = 'armcheck-1'
    node._stop_calibration_program('armcheck-1')
    assert stopped == [1]

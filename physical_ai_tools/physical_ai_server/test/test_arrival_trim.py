#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The arrival trim (spec 40 S6.6; decisions C8, D33, D34, D42; WP-14).

``motion._execute_pickup`` descends to the grasp pose and, before the gripper
closes, compares the MEASURED joints 2..4 with the model pose and commands the
remaining error once more — at most two rounds, inside the pickup's own
motion-lock hold. It is skipped with one German ``[WARNUNG]`` when an error is
beyond ``min(2 × droop envelope + 0.010, 0.15)`` (bound (a)) or when the
calibrated model says the correction would LOWER the fingertip by more than
0.5 mm (raise-only, bound (b)). Never in the simulator, only with a valid arm
check, and ``EDUBOTICS_ARM_TRIM=0`` is its rollback.

The arm is a fake follower: its measured joints are the last commanded pose
minus a sag (a function of the command). The droop population of D33 comes
from ``autocal_sim``'s kits.
"""

from __future__ import annotations

import ast
from dataclasses import replace
import logging
import math
import os
import pathlib
import textwrap
import threading
import types

import numpy as np
import pytest

import autocal_sim as S
from physical_ai_server import robot_profiles as RP
from physical_ai_server.workflow import arm_model
from physical_ai_server.workflow import scene_fixture as SF
from physical_ai_server.workflow.autocal import arm_check
from physical_ai_server.workflow.autocal import store as STORE
from physical_ai_server.workflow.handlers import motion as M
from physical_ai_server.workflow.workflow_manager import WorkflowContext, WorkflowManager

_REPO = pathlib.Path(__file__).resolve().parents[3]
_NODE = pathlib.Path(__file__).resolve().parents[1] / 'physical_ai_server' / 'physical_ai_server.py'
PROFILE = {'omx': 'omx_full', 'v1': 'v1_studio'}
SKIPPED_DE = '[WARNUNG] Der Arm ist nicht genau angekommen – die Feinkorrektur wurde ausgelassen.'
TWO_TICKS = 0.0031
ENVELOPE = (0.0, 0.02, 0.02, 0.01, 0.0)       # bound (a): 0.05, 0.05, 0.03 rad
TRIM_LOGGER = 'physical_ai_server.workflow.handlers.motion'


# ── the fake arm, the run and the clock ──────────────────────────────────────
class _Clock:
    """``motion.time`` without the wall clock: ``sleep`` advances ``monotonic``."""

    def __init__(self):
        self.t = 0.0
        self.sleeps = []
        self.on_sleep = None

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += max(0.0, float(seconds))
        if self.on_sleep is not None:
            self.on_sleep()


@pytest.fixture(autouse=True)
def _clocks(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(M, 'time', clock)
    monkeypatch.setattr(M, 'chunked_publish', _fast_publish)
    monkeypatch.delenv('EDUBOTICS_ARM_TRIM', raising=False)
    return clock


def _fast_publish(publisher, points, should_stop, **_kw):
    """chunked_publish without the real-time pacing: the whole stream in one call."""
    if should_stop():
        return False
    pts = list(points)
    if pts:
        publisher(pts)
    return True


class _Arm:
    """A follower whose measured arm joints are the last commanded pose minus
    ``sag(command)``; the gripper reads what it was commanded."""

    def __init__(self, start, sag):
        self.command = [float(v) for v in start]
        self.sag = sag
        self.reads = 0

    def publish(self, chunk):
        self.command = [float(v) for v in chunk[-1][0]]

    def measured(self):
        s = self.sag(list(self.command))
        return [self.command[i] - float(s[i]) for i in range(5)] + [self.command[5]]

    def joints(self):
        self.reads += 1
        return self.measured()


def _model(kind):
    """The real robot's arm model with nothing calibrated (the contract's fingertip and joint-1 window)."""
    return arm_model.CalibratedIK(S.solver(kind), fingertip_m=SF.FINGERTIP_BELOW_EE_M[kind],
                                  joint1_window_deg=SF.ARMS[kind]['joint1_window_deg'])


def _grasp_xyz(kind, ik, r=0.20, az_deg=0.0):
    """The end effector's target for the cube's grasp height at radius ``r`` from the joint-1 axis."""
    z = SF.MAT_THICKNESS_M + S.OBJ_HEIGHT_M - S.GRASP_DEPTH_M + ik.tool_tip_below_ee_m
    a = math.radians(az_deg)
    return (ik.base_axis_x + r * math.cos(a), r * math.sin(a), z)


class _Run:
    """A run's WorkflowContext (built as ``WorkflowManager.start`` builds it) on the fake arm; the trim's fields
    are set as attributes, so the same context drives the code before and after the trim exists."""

    def __init__(self, kind, sag=None, envelope=ENVELOPE, simulated=False, ik=None):
        prof = RP.ROBOT_PROFILES[PROFILE[kind]]
        self.kind = kind
        self.ik = ik or _model(kind)
        start = list(prof.home_joints_rad) + [prof.gripper_open_rad]
        self.arm = _Arm(start, sag or (lambda q: [0.0] * 5))
        self.logs = []
        self.stop = False
        self.ctx = WorkflowContext(
            publisher=self.arm.publish, ik=self.ik, motion_lock=threading.RLock(),
            should_stop=lambda: self.stop, log=self.logs.append, get_follower_joints=self.arm.joints,
            z_table=SF.MAT_THICKNESS_M + self.ik.tool_tip_below_ee_m, board_table_z=SF.MAT_THICKNESS_M,
            last_full_joints=list(start), num_arm_joints=prof.num_arm_joints,
            roll_joint_index=prof.roll_joint_index, home_joints_rad=prof.home_joints_rad,
            observe_pose_joints=prof.observe_pose_joints, gripper_open_rad=prof.gripper_open_rad,
            gripper_closed_rad=prof.gripper_closed_rad, velocity_limit_rad_s=prof.velocity_limit_rad_s,
            grasp_held_margin_rad=prof.grasp_held_margin_rad, simulated=simulated)
        self.ctx.arm_trim_envelope_rad = None if envelope is None else list(envelope)
        self.closed = prof.gripper_closed_rad
        self.open = prof.gripper_open_rad
        self.legs = []

    def pickup(self, monkeypatch, xyz=None, roll=0.0):
        """``_execute_pickup`` with every published leg recorded: ``(start, end, duration, tempo, measured
        arm joints when it was published, ctx.last_full_joints then)``."""
        real = M._publish_motion

        def spy(ctx, q_start, q_end, duration_s, tempo=None):
            self.legs.append((list(q_start), list(q_end), duration_s, tempo, self.arm.measured(),
                              list(ctx.last_full_joints)))
            if tempo is None:
                return real(ctx, q_start, q_end, duration_s)
            return real(ctx, q_start, q_end, duration_s, tempo)

        monkeypatch.setattr(M, '_publish_motion', spy)
        xyz = xyz or _grasp_xyz(self.kind, self.ik)
        M._execute_pickup(self.ctx, xyz, M.DEFAULT_APPROACH_HEIGHT_M, roll, self.closed)
        return self

    def warnings(self):
        return [m for m in self.logs if m.startswith('[WARNUNG]')]


def _todays_legs(kind, xyz=None, roll=0.0):
    """Today's five pickup legs, from the model poses alone (open, approach, descend, close, lift)."""
    run = _Run(kind)
    xyz = xyz or _grasp_xyz(kind, run.ik)
    grasp, above = M._solve_grasp_and_approach(run.ctx, xyz, M.DEFAULT_APPROACH_HEIGHT_M, roll=roll)
    seed = list(run.ctx.last_full_joints)
    open_q = seed[:5] + [run.open]
    above_q, grasp_q = list(above) + [run.open], list(grasp) + [run.open]
    closed_q, lift_q = list(grasp) + [run.closed], list(above) + [run.closed]
    return [(seed, open_q, M.DEFAULT_GRIPPER_DURATION_S), (open_q, above_q, M.DEFAULT_MOVE_DURATION_S),
            (above_q, grasp_q, M.DEFAULT_GRASP_DURATION_S), (grasp_q, closed_q, M.DEFAULT_GRIPPER_DURATION_S),
            (closed_q, lift_q, M.DEFAULT_APPROACH_DURATION_S)]


def _legs(run):
    return [(a, b, d) for a, b, d, _t, _m, _l in run.legs]


def _close_leg(run):
    """The one leg in which only the gripper moves from open to closed."""
    found = [leg for leg in run.legs if leg[0][5] == run.open and leg[1][5] == run.closed]
    assert len(found) == 1, found
    return found[0]


def _gravity_sag(kind, size):
    """A constant sag of ``size`` on joints 2 and 3 in gravity's direction (the arm hangs lower)."""
    def sag(q):
        tau = S.gravity_torques(kind, np.asarray(q[:5]), S._masses(kind))[0]
        s = [0.0] * 5
        s[1] = math.copysign(size, tau[1])
        s[2] = math.copysign(size, tau[2])
        return s
    return sag


def _tip_z(ik, q):
    R, t = ik.fk(list(q)[:5])
    axis = np.asarray(R) @ np.asarray(ik.approach_axis_local, dtype=float)
    return float(t[2] + ik.tool_tip_below_ee_m * axis[2] / np.linalg.norm(axis))


def _trim_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == TRIM_LOGGER
            and r.getMessage().startswith('arrival trim:')]


# ══════════════════════════════════════════════════════════════════════════
# the trim on a sagging arm
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('kind', ('omx', 'v1'))
def test_a_sag_of_0_02_rad_on_joints_2_and_3_ends_within_two_ticks_before_the_close(kind, monkeypatch, caplog):
    """FAILS FIRST: a constant sag of 0.02 rad on joints 2 and 3 (the fingertip hangs lower) — the measured
    joints 2..4 are within 0.0031 rad of the model pose when the close is published, with at most two extra
    publishes; ``ctx.last_full_joints`` stays the model pose and the close and the lift start from what was
    really commanded."""
    caplog.set_level(logging.INFO, logger=TRIM_LOGGER)
    today = _todays_legs(kind)
    run = _Run(kind, sag=_gravity_sag(kind, 0.02))
    grasp_q = today[2][1]
    sagged = run.arm.sag(grasp_q)
    assert _tip_z(run.ik, [g - s for g, s in zip(grasp_q, sagged)]) < _tip_z(run.ik, grasp_q) - 0.004   # it hangs
    run.pickup(monkeypatch)
    extra = len(run.legs) - len(today)
    assert 1 <= extra <= 2
    close = _close_leg(run)
    measured = close[4]
    assert max(abs(grasp_q[j] - measured[j]) for j in (1, 2, 3)) <= TWO_TICKS
    assert close[5] == grasp_q                                     # ctx.last_full_joints: the model pose
    trimmed = close[0][:5]
    assert trimmed != grasp_q[:5] and close[1] == trimmed + [run.closed]
    lift = run.legs[-1]
    assert lift[0] == trimmed + [run.closed] and lift[1] == today[4][1]
    assert run.ctx.last_full_joints == today[4][1] and run.ctx.last_arm_joints == today[4][1][:5]
    assert run.warnings() == []
    lines = _trim_lines(caplog)
    assert len(lines) == 1 and 'applied=True' in lines[0] and 'rounds=' in lines[0] and 'errors_rad=[' in lines[0]


def _yielding_sag(kind, size, give):
    """The sag grows with the correction: ``size`` plus ``give`` × (command − the model pose), joints 2 and 3."""
    grasp = _todays_legs(kind)[2][1]
    base = _gravity_sag(kind, size)

    def sag(q):
        s = base(q)
        for j in (1, 2, 3):
            s[j] += give * (q[j] - grasp[j])
        return s
    return sag


def test_an_arm_that_needs_two_rounds_gets_two_and_never_a_third(monkeypatch, caplog):
    """FAILS FIRST: an arm that gives way by a fifth of each correction ends within two ticks after exactly two
    extra publishes; one that gives way by half is still off after two rounds and gets no third publish."""
    caplog.set_level(logging.INFO, logger=TRIM_LOGGER)
    today = _todays_legs('omx')
    grasp_q = today[2][1]
    run = _Run('omx', sag=_yielding_sag('omx', 0.02, 0.2)).pickup(monkeypatch)
    assert len(run.legs) - len(today) == 2
    assert max(abs(grasp_q[j] - _close_leg(run)[4][j]) for j in (1, 2, 3)) <= TWO_TICKS
    caplog.clear()
    stubborn = _Run('omx', sag=_yielding_sag('omx', 0.02, 0.5)).pickup(monkeypatch)
    assert len(stubborn.legs) - len(today) == 2
    assert max(abs(grasp_q[j] - _close_leg(stubborn)[4][j]) for j in (1, 2, 3)) > TWO_TICKS
    lines = _trim_lines(caplog)
    assert len(lines) == 1 and 'rounds=2' in lines[0] and 'applied=True' in lines[0]
    assert stubborn.warnings() == []


def test_a_correction_that_would_lower_the_fingertip_by_3_mm_publishes_nothing_and_warns_once(monkeypatch, caplog):
    """FAILS FIRST (review M5, decision C8): the finger rests on a cube — the arm stands HIGHER than the model
    pose, so the correction would lower the fingertip by 3 mm: nothing extra is published (today's legs exactly),
    one ``[WARNUNG]``, and the log names the raise-only rule."""
    caplog.set_level(logging.INFO, logger=TRIM_LOGGER)
    today = _todays_legs('omx')
    grasp_q = today[2][1]
    ik = _model('omx')
    probe = list(grasp_q)
    probe[1] += 0.001
    slope = (_tip_z(ik, probe) - _tip_z(ik, grasp_q)) / 0.001       # metres per radian of joint 2
    held = 0.003 / slope                                             # joint 2 where the tip stands 3 mm high
    probe = list(grasp_q)
    probe[1] += held
    assert abs(_tip_z(ik, probe) - _tip_z(ik, grasp_q) - 0.003) < 1e-4
    assert abs(held) < min(2 * ENVELOPE[1] + 0.010, 0.15)            # bound (a) is not what skips it

    def resting(q):
        s = [0.0] * 5
        s[1] = -held                                                 # measured = command + held
        return s

    run = _Run('omx', sag=resting).pickup(monkeypatch)
    assert _legs(run) == today
    assert run.warnings() == [SKIPPED_DE]
    lines = _trim_lines(caplog)
    assert len(lines) == 1 and 'applied=False' in lines[0] and 'skipped=lowers' in lines[0]


@pytest.mark.parametrize('envelope_rad, applied', ((0.02, False), (0.05, True)))
def test_the_skip_bound_follows_the_droop_envelope(envelope_rad, applied, monkeypatch, caplog):
    """FAILS FIRST (decisions C8, D33): an error of 0.08 rad on joint 2 (the arm hangs) is skipped with an
    envelope of 0.02 rad (bound 0.05) and applied with 0.05 rad (bound 0.11) — the limit follows the envelope."""
    caplog.set_level(logging.INFO, logger=TRIM_LOGGER)
    today = _todays_legs('omx')
    grasp_q = today[2][1]

    def hangs(q):
        tau = S.gravity_torques('omx', np.asarray(q[:5]), S._masses('omx'))[0]
        s = [0.0] * 5
        s[1] = math.copysign(0.08, tau[1])
        return s

    run = _Run('omx', sag=hangs, envelope=(0.0, envelope_rad, envelope_rad, envelope_rad, 0.0))
    run.pickup(monkeypatch)
    lines = _trim_lines(caplog)
    assert len(lines) == 1
    if applied:
        assert len(run.legs) == len(today) + 1 and run.warnings() == []
        assert max(abs(grasp_q[j] - _close_leg(run)[4][j]) for j in (1, 2, 3)) <= TWO_TICKS
        assert 'applied=True' in lines[0]
    else:
        assert _legs(run) == today and run.warnings() == [SKIPPED_DE]
        assert 'applied=False' in lines[0] and 'skipped=bound' in lines[0]


# ══════════════════════════════════════════════════════════════════════════
# when the trim does not run, and how it waits
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('switch', ('simulator', 'no_arm_check', 'rollback_key'))
def test_the_simulator_no_arm_check_and_the_rollback_key_publish_todays_sequence(switch, monkeypatch, caplog):
    """FAILS FIRST: the same sagging arm with everything on gets extra publishes (the control); in the
    simulator, without an arm check, or with ``EDUBOTICS_ARM_TRIM=0`` the pickup publishes today's five legs
    exactly, never reads the follower for a trim and logs no trim line."""
    caplog.set_level(logging.INFO, logger=TRIM_LOGGER)
    today = _todays_legs('omx')
    control = _Run('omx', sag=_gravity_sag('omx', 0.02)).pickup(monkeypatch)
    assert len(control.legs) > len(today)
    caplog.clear()
    if switch == 'rollback_key':
        monkeypatch.setenv('EDUBOTICS_ARM_TRIM', '0')
    run = _Run('omx', sag=_gravity_sag('omx', 0.02), simulated=(switch == 'simulator'),
               envelope=None if switch == 'no_arm_check' else ENVELOPE).pickup(monkeypatch)
    assert _legs(run) == today
    assert [(t, d) for _a, _b, d, t, _m, _l in run.legs] == [(None, d) for _a, _b, d in today]
    assert run.arm.reads == 0 and run.warnings() == [] and _trim_lines(caplog) == []


@pytest.mark.parametrize('name, value', (
    ('ARM_TRIM_ROUNDS', 2), ('ARM_TRIM_SETTLE_S', 0.25), ('ARM_TRIM_DONE_RAD', 0.0031),
    ('ARM_TRIM_ENVELOPE_FACTOR', 2.0), ('ARM_TRIM_ENVELOPE_MARGIN_RAD', 0.010), ('ARM_TRIM_CAP_RAD', 0.15),
    ('ARM_TRIM_LOWER_MAX_M', 0.0005), ('ARM_TRIM_MOVE_S', 0.5),
    # today's pickup legs, which _todays_legs rebuilds (DEFAULT_MOVE_DURATION_S is on
    # test_constant_pins.py's Tier-2 allowlist)
    ('DEFAULT_GRIPPER_DURATION_S', 0.5), ('DEFAULT_GRASP_DURATION_S', 1.0), ('DEFAULT_APPROACH_DURATION_S', 1.5)))
def test_the_trims_numbers_are_spec_40_s6_6s(name, value):
    """S6.6: two rounds, a quarter second each, done at two ticks (2 × 2π/4096 ≤ 0.0031 rad), bound (a)
    ``min(2 × envelope + 0.010, 0.15)``, raise-only beyond 0.5 mm."""
    assert getattr(M, name) == value
    assert 2 * 2 * math.pi / 4096 <= M.ARM_TRIM_DONE_RAD < 3 * 2 * math.pi / 4096


@pytest.mark.parametrize('value, on', (('', True), ('1', True), ('0', False), ('0.0', False), (' 0 ', False)))
def test_the_rollback_key_is_read_when_the_pickup_runs(value, on, monkeypatch):
    monkeypatch.setenv('EDUBOTICS_ARM_TRIM', value)
    assert M.arm_trim_enabled() is on


def test_stop_during_the_settle_wait_raises(monkeypatch, _clocks):
    """FAILS FIRST: Stop pressed while the trim waits for the arm to settle raises „Workflow wurde gestoppt."
    before the follower is read and before the gripper closes."""
    run = _Run('omx', sag=_gravity_sag('omx', 0.02))

    def press_stop():
        run.stop = True

    _clocks.on_sleep = press_stop
    with pytest.raises(M.WorkflowError) as excinfo:
        run.pickup(monkeypatch)
    assert str(excinfo.value) == 'Workflow wurde gestoppt.'
    assert _clocks.sleeps and run.arm.reads == 0
    assert all(leg[1][5] != run.closed for leg in run.legs)            # the gripper never closed
    assert len(run.legs) == 3                                         # open, approach, descend


def test_the_settle_wait_is_a_quarter_second_in_stop_polled_slices(monkeypatch, _clocks):
    run = _Run('omx', sag=_gravity_sag('omx', 0.02)).pickup(monkeypatch)
    assert run.arm.reads == 2
    assert math.isclose(sum(_clocks.sleeps), 2 * M.ARM_TRIM_SETTLE_S, abs_tol=1e-9)
    assert M.ARM_TRIM_SETTLE_S == 0.25 and max(_clocks.sleeps) <= M._MOTION_LOCK_POLL_S + 1e-12


def test_the_trim_runs_inside_the_pickups_motion_lock_hold(monkeypatch):
    """FAILS FIRST: while the trim reads the arm, the pickup's thread holds the motion lock — another thread
    cannot take it (the trim takes no hold of its own; test_motion_lock_policy counts the sites)."""
    run = _Run('omx', sag=_gravity_sag('omx', 0.02))
    taken = []
    read = run.arm.joints

    def joints():
        box = []
        t = threading.Thread(target=lambda: box.append(run.ctx.motion_lock.acquire(blocking=False)))
        t.start()
        t.join(2.0)
        if box and box[0]:
            run.ctx.motion_lock.release()
        taken.append(box[0] if box else None)
        return read()

    run.ctx.get_follower_joints = joints
    run.pickup(monkeypatch)
    assert taken and all(v is False for v in taken)


def test_no_readback_trims_nothing_and_says_nothing_to_the_student(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=TRIM_LOGGER)
    today = _todays_legs('omx')
    run = _Run('omx', sag=_gravity_sag('omx', 0.02))
    run.ctx.get_follower_joints = lambda: None
    run.pickup(monkeypatch)
    assert _legs(run) == today and run.warnings() == []
    lines = _trim_lines(caplog)
    assert len(lines) == 1 and 'applied=False' in lines[0] and 'skipped=no_readback' in lines[0]


@pytest.mark.parametrize('envelope', ([0.0, float('nan'), 0.02, 0.01, 0.0], [0.0, -0.02, 0.02, 0.01, 0.0],
                                      [0.0, 0.02], 'abc', None))
def test_an_envelope_that_is_not_a_valid_arm_checks_turns_the_trim_off(envelope):
    assert M.arm_trim_bounds(envelope) is None


def test_the_correction_never_commands_beyond_the_joint_limits(monkeypatch):
    """FAILS FIRST: a correction that would push joint 2 past the arm model's limit is commanded up to the limit
    and no further; the second round has nothing left to command and publishes nothing."""
    run = _Run('omx', sag=_gravity_sag('omx', 0.02))
    today = _todays_legs('omx')
    grasp_q = today[2][1]
    limits = [list(lim) for lim in run.ik.joint_limits]
    sign = math.copysign(1.0, run.arm.sag(grasp_q)[1])               # the correction's direction on joint 2
    edge = grasp_q[1] + sign * 0.005
    limits[1][1 if sign > 0 else 0] = edge
    real = run.ik
    run.ctx.ik = types.SimpleNamespace(
        joint_limits=tuple(tuple(v) for v in limits),
        **{name: getattr(real, name) for name in ('fk', 'approach_axis_local', 'tool_tip_below_ee_m', 'solve',
                                                  'base_axis_x', 'num_joints', 'link_points', 'base_yaw')})
    run.pickup(monkeypatch)
    assert len(run.legs) == len(today) + 1
    trim = run.legs[3]
    assert trim[0] == grasp_q and trim[1][1] == pytest.approx(edge, abs=1e-12)
    assert all(limits[1][0] - 1e-12 <= leg[1][1] <= limits[1][1] + 1e-12 for leg in run.legs[2:5])   # to the close


# ══════════════════════════════════════════════════════════════════════════
# decisions D33, D42: the droop warning against the trim's cap
# ══════════════════════════════════════════════════════════════════════════

def test_the_droop_warning_sits_well_below_the_trims_cap():
    """FAILS FIRST (D33): ``arm_check.DROOP_ENVELOPE_MAX_RAD × 1.4 ≤`` the trim's cap (0.15 rad), both read from
    their modules (the largest free-arm error of a kit without the warning is 6.8°, 1.19 × the warning's value)."""
    assert M.ARM_TRIM_CAP_RAD == 0.15
    assert arm_check.DROOP_ENVELOPE_MAX_RAD * 1.4 <= M.ARM_TRIM_CAP_RAD
    e = arm_check.DROOP_ENVELOPE_MAX_RAD
    assert M.arm_trim_bounds([0.0, e, e, e, 0.0]) == pytest.approx((min(2 * e + 0.010, 0.15),) * 3)
    assert M.arm_trim_bounds([0.0, 0.02, 0.05, 0.001, 0.0]) == pytest.approx((0.05, 0.11, 0.012))


def _zone_grid(kind):
    """The grasp poses of the zone: 10 radii × 9 azimuths × 3 rolls at the cube's grasp height (P-R15)."""
    spec = SF.ARMS[kind]
    ik = _model(kind)
    lo, hi = spec['zone_sector_deg']
    Q, radii = [], []
    for r in np.linspace(spec['zone_inner_m'], spec['zone_outer_m'], 10):
        for az in np.linspace(lo, hi, 9):
            x, y = r * math.cos(math.radians(az)), r * math.sin(math.radians(az))
            if not SF.in_zone(spec, x, y):
                continue
            for roll in (-1.2, 0.0, 1.2):
                q = ik.solve(_grasp_xyz(kind, ik, r, az), roll=roll)
                if q is not None:
                    Q.append(q)
                    radii.append(float(r))
    return np.asarray(Q), np.asarray(radii)


def _free_arm_error(kit, Q, rng):
    """``|commanded − encoder|`` of a free arm at each pose of ``Q`` (gravity droop, backlash, encoder
    non-linearity and ticks — ``autocal_sim.grasps``' arrival)."""
    tau = S.gravity_torques(kit.kind, Q + kit.dzero, kit.masses)
    out = []
    for i, q in enumerate(Q):
        a0 = q - tau[i] / kit.stiff + kit.lash(rng)
        r0 = a0 + kit.inl_of(a0)
        out.append(np.abs(q - np.round(r0 / S.TICK) * S.TICK))
    return np.asarray(out)


@pytest.mark.parametrize('kind, least_accepted', (('omx', 15), ('v1', 35)))
def test_no_free_arm_arrival_of_an_arm_without_the_droop_warning_exceeds_the_skip_bound(kind, least_accepted):
    """FAILS FIRST (decisions D33, D42; probe P-R15): over 40 ``autocal_sim`` kits per arm at three times the
    assumed droop (seed fixed), every kit whose envelope stays below the droop warning reaches every grasp pose
    of the zone with errors on joints 2..4 inside the trim's bound (a), at least 1.7° under the cap."""
    Q, _radii = _zone_grid(kind)
    assert len(Q) >= 200
    opt = replace(S.Options(), droop_scale=3.0)
    accepted, worst = 0, 0.0
    for k in range(40):
        rng = np.random.default_rng([2026, k])
        kit = S.sample_kit(kind, rng, opt)
        env = arm_check.droop_envelope(S.arm_check_inputs(kit, rng, opt).records)
        if np.any(env[1:4] > arm_check.DROOP_ENVELOPE_MAX_RAD):
            continue                                       # warned at the arm check (D42), never refused
        accepted += 1
        bounds = np.asarray(M.arm_trim_bounds(env))
        errors = _free_arm_error(kit, Q, np.random.default_rng([2026, k, 7]))[:, 1:4]
        assert np.all(errors <= bounds), (k, np.degrees(errors.max(axis=0)), np.degrees(bounds))
        worst = max(worst, float(errors.max()))
    assert accepted >= least_accepted
    assert M.ARM_TRIM_CAP_RAD - worst >= math.radians(1.7)


def _sagging_omx_kit(sag_rad=0.18):
    """An OMX kit whose joints 2..4 sag at most ``sag_rad`` over the arm-check poses (with the clip-on target)."""
    rng = np.random.default_rng([2026, 99, 0])
    kit = S.sample_kit('omx', rng, replace(S.Options(), droop_scale=1.0))
    P = np.asarray(SF.ARMS['omx']['arm_check_poses_rad'], dtype=float)
    tau = S.gravity_torques('omx', P + kit.dzero, kit.masses, kit.mount, 0.045)
    kit.stiff = kit.stiff * (np.abs(tau / kit.stiff)[:, 1:4].max() / sag_rad)
    return kit, rng


def test_an_omx_built_to_sag_0_18_rad_at_the_arm_check_poses_exceeds_the_cap_at_a_far_edge_grasp(monkeypatch, caplog):
    """FAILS FIRST (the cliff the droop warning announces; D42: warned, not refused): an OMX kit built to sag
    0.18 rad at the arm-check poses is warned at the arm check; its free arm exceeds the cap at every far-edge
    grasp pose of the zone and nowhere else; and the trim, handed that kit's envelope, skips such a grasp with
    its ``[WARNUNG]`` and publishes today's legs."""
    caplog.set_level(logging.INFO, logger=TRIM_LOGGER)
    kit, rng = _sagging_omx_kit()
    P = np.asarray(SF.ARMS['omx']['arm_check_poses_rad'], dtype=float)
    built = np.abs(S.gravity_torques('omx', P + kit.dzero, kit.masses, kit.mount, 0.045) / kit.stiff)[:, 1:4]
    assert built.max() == pytest.approx(0.18)
    env = arm_check.droop_envelope(S.arm_check_inputs(kit, rng).records)
    assert env[1] > arm_check.DROOP_ENVELOPE_MAX_RAD                   # the droop warning
    bounds = np.asarray(M.arm_trim_bounds(env))
    assert bounds[0] == M.ARM_TRIM_CAP_RAD
    Q, radii = _zone_grid('omx')
    errors = _free_arm_error(kit, Q, np.random.default_rng(5))[:, 1:4]
    over = np.any(errors > bounds, axis=1)
    far = radii >= SF.ARMS['omx']['zone_outer_m'] - 1e-9
    assert over[far].all() and not over[~far].any()

    def droops(q):
        return list(S.gravity_torques('omx', np.asarray(q[:5]) + kit.dzero, kit.masses)[0] / kit.stiff)

    xyz = _grasp_xyz('omx', _model('omx'), SF.ARMS['omx']['zone_outer_m'])
    today = _todays_legs('omx', xyz)
    run = _Run('omx', sag=droops, envelope=list(env)).pickup(monkeypatch, xyz)
    assert abs(today[2][1][1] - _close_leg(run)[4][1]) > M.ARM_TRIM_CAP_RAD   # the far-edge error itself
    assert _legs(run) == today and run.warnings() == [SKIPPED_DE]
    assert 'skipped=bound' in _trim_lines(caplog)[0]


# ══════════════════════════════════════════════════════════════════════════
# the wiring: the node hands the arm check's envelope to the run's context
# ══════════════════════════════════════════════════════════════════════════

def test_the_workflow_context_declares_the_trims_envelope():
    """FAILS FIRST: the field is declared (an undeclared ctx field is a branch that silently never runs)."""
    assert 'arm_trim_envelope_rad' in WorkflowContext.__dataclass_fields__
    assert WorkflowContext(publisher=lambda _c: None).arm_trim_envelope_rad is None


@pytest.mark.parametrize('calib, expected', (({'droop_envelope_rad': [0.0, 0.03, 0.02, 0.01, 0.0]},
                                              [0.0, 0.03, 0.02, 0.01, 0.0]), ({}, None)))
def test_the_manager_puts_the_calibrations_envelope_on_the_runs_context(calib, expected):
    """FAILS FIRST: ``start()`` hands the run's calibration's ``droop_envelope_rad`` to the context."""
    captured = []
    wm = WorkflowManager(publisher=lambda _pts: None, get_follower_joints=lambda: [0.0, -1.5, 1.5, 0.0, 0.0, 0.8],
                         load_calibration=lambda: dict(calib))
    wm._run = lambda interpreter, ctx: captured.append(ctx)
    ok, msg, _warnings = wm.start('{"blocks": {"languageVersion": 0, "blocks": []}}', 'trim')
    assert ok, msg
    wm._thread.join(5.0)
    assert captured[0].arm_trim_envelope_rad == expected


def _node_fns(*names):
    source = _NODE.read_text(encoding='utf-8')
    ns = {'os': os, 'math': math, 'robot_profiles': RP, 'STUDIO_CAMERA': 'scene_studio'}
    found = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            exec(compile(textwrap.dedent(ast.get_source_segment(source, node)), str(_NODE), 'exec'), ns)  # noqa: S102
            found[node.name] = ns[node.name]
    assert set(found) == set(names), set(names) - set(found)
    return found


class _Logger:
    def __getattr__(self, _level):
        return lambda *_a, **_k: None


class _Service:
    def __init__(self, approved, scope=True):
        self.approved, self.scope = approved, scope

    def run_calibration(self):
        return self.approved

    def tower_scope(self):
        return self.scope


class _Node:
    def __init__(self, profile_id, service):
        self._arm_profile = RP.ROBOT_PROFILES[profile_id]
        self.scene_calibration = service
        self._get_or_create_calibration_manager = lambda: None
        for name, fn in _node_fns('_load_workflow_calibration').items():
            setattr(self, name, types.MethodType(fn, self))

    def get_logger(self):
        return _Logger()


def _stored_arm_check(profile_id, envelope):
    return {
        'v': 1, 'fixture_id': SF.FIXTURE_ID, 'contract_hash': SF.contract_hash(), 'profile_id': profile_id,
        'camera_profile_id': 'tower_v1', 'image_size': [1920, 1080], 'created_utc': '2026-11-20T08:00:00Z',
        'kb': {'f': 1031.2, 'aspect': 1.001, 'cx': 961.0, 'cy': 538.2, 'k1': 0.05, 'k2': -0.04},
        'print_scale': 1.0, 'joint_offsets_rad': [0.0] * 5, 'axis_tilts_rad': [0.0] * 6, 'tilt': [0.0, 0.0],
        'mat_pose': [0.0, 0.0, 0.0], 'camera_pose_mat': {'rvec': [2.1, 0.3, -0.2], 't': [0.01, 0.2, 0.55]},
        'lens_w': [-0.1, 0.1, 0.575], 'fixture_xy_mat': [[0.0, 0.0]] * 8, 'fixture_px': [[900.0, 400.0]] * 8,
        'mount': {'rvec': [0.0, 0.1, 0.0], 't': [0.0, -0.095, -0.11]}, 'droop_envelope_rad': list(envelope),
        'tower_pose': [0.0, 0.0, 0.0], 'tower_fit_rms_m': 0.0002, 'target_removed': True,
        'droop_warning': None, 'notes': [],
        'quality': {'poses_used': 20, 'rms_px': 0.21, 'holdout_rms_px': 0.4, 'holdout_p95_mm': 0.4,
                    'field_tags': 14, 'sharpness': 80.0},
    }


@pytest.mark.parametrize('profile_id', ('omx_full', 'v1_studio'))
def test_the_node_hands_a_run_the_arm_checks_envelope_only_with_the_tower_calibration(profile_id, tmp_path,
                                                                                         monkeypatch):
    """FAILS FIRST: a real run the calibration gate approved gets the stored arm check's droop envelope; without
    the approval, under ``legacy``, without a stored arm check or with another profile's, it gets none."""
    monkeypatch.setenv('EDUBOTICS_CALIB_DIR', str(tmp_path))
    envelope = [0.001, 0.031, 0.022, 0.009, 0.002]
    tower = {'intrinsics': {'K': np.eye(3), 'dist': np.zeros(5)}, 'T_cam_to_w': np.eye(4), 'image_size': (1920, 1080)}
    assert 'droop_envelope_rad' not in _Node(profile_id, _Service(tower))._load_workflow_calibration()
    STORE.Store(str(tmp_path)).write(_stored_arm_check(profile_id, envelope))
    assert _Node(profile_id, _Service(tower))._load_workflow_calibration()['droop_envelope_rad'] == envelope
    assert 'droop_envelope_rad' not in _Node(profile_id, _Service(None))._load_workflow_calibration()
    assert 'droop_envelope_rad' not in _Node(profile_id, _Service(None, scope=False))._load_workflow_calibration()
    other = 'v1_studio' if profile_id != 'v1_studio' else 'omx_full'
    assert 'droop_envelope_rad' not in _Node(other, _Service(tower))._load_workflow_calibration()


# ══════════════════════════════════════════════════════════════════════════
# the rollback key reaches the container (decision D32)
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('rel', ('robotis_ai_setup/docker/docker-compose.yml',
                                 'robotis_ai_setup/docker/docker-compose.opi.yml',
                                 'robotis_ai_setup/pi_agent/docker/docker-compose.opi.yml'))
def test_the_rollback_key_is_forwarded_to_physical_ai_server(rel):
    """FAILS FIRST: ``EDUBOTICS_ARM_TRIM`` (default on) on ``physical_ai_server`` in the three composes, and on no
    other service."""
    import yaml
    services = yaml.safe_load((_REPO / rel).read_text(encoding='utf-8'))['services']
    for name, service in services.items():
        keys = [str(e).split('=', 1)[0] for e in (service.get('environment') or [])]
        if name == 'physical_ai_server':
            assert 'EDUBOTICS_ARM_TRIM=${EDUBOTICS_ARM_TRIM:-1}' in service['environment']
        else:
            assert 'EDUBOTICS_ARM_TRIM' not in keys, name

"""A Position is the taught ARM pose (owner decisions Q2, Q9, Q12; spec D1).

„bewege zu <Position>" sends the arm to the joints it was taught at, in joint
space, through the floor planner (``home_planner``) and the Sperrzonen — never
through a fresh Cartesian solve of the stored point, which on the Edu:1 refused
every taught Position and on the OMX picked another wrist. A Position taught on
ANOTHER robot (or one without joints) keeps the point path. „ablegen bei" and
„aufnehmen" refuse a Position (Q9); „setze X = aktuelle Position" stays a Ziel
(Q12).

Driven through the real manager, the real interpreter and the real solvers
(``rig_emulation.Rig``, the simulator wired like the node).
"""

from __future__ import annotations

import math
import random
import threading
import types

import pytest

from physical_ai_server import robot_profiles as rp
from physical_ai_server.workflow import arm_geometry as ag
from physical_ai_server.workflow import home_planner as hp
from physical_ai_server.workflow.handlers import motion as M
from physical_ai_server.workflow.workflow_manager import WorkflowContext

from daten_timeout import per_test_time_limit  # noqa: F401 — a hang fails, never stalls
from rig_emulation import B, Rig, chain, program, ref, vget, vset, var

TEST_TIMEOUT_S = 240.0


def _entry(pid, name, q, gripper, *, robot_type=None, joint_names=None, joints=True):
    """A payload Position exactly as the Sammlung sends it (kind 'pose')."""
    prof = rp.ROBOT_PROFILES[pid]
    _R, t = prof.build_ik().fk(list(q))
    out = {'name': name, 'kind': 'pose',
           'x': float(t[0]), 'y': float(t[1]), 'z': float(t[2])}
    if joints:
        out['joints'] = [float(v) for v in q] + [float(gripper)]
        out['joint_names'] = list(joint_names if joint_names is not None else prof.joint_names)
        out['robot_type'] = robot_type if robot_type is not None else prof.data_robot_type
    return out


def _move(name):
    return B('edubotics_move_to', inputs={'DESTINATION': ref(name)})


def _arm(rig):
    return list(rig.arm.get_joints())[:rig.profile.num_arm_joints]


# A taught pose per arm, deliberately with a NON-default wrist roll so the old
# point path (which re-solves at GRASP_ROLL_RAD) can never land on it.
def _taught_pose(pid):
    prof = rp.ROBOT_PROFILES[pid]
    ik = prof.build_ik()
    tip = float(getattr(ik, 'tool_tip_below_ee_m', 0.0))
    q = ik.solve(target_xyz=(0.17, 0.05, tip + 0.05),
                 seed=list(prof.home_joints_rad), roll=M.GRASP_ROLL_RAD + 0.4)
    assert q is not None
    return list(q)


@pytest.mark.parametrize('pid', ['omx_full', 'edu1_studio'])
def test_taught_pose_is_reached_exactly(monkeypatch, pid):
    """The arm ends in the taught joints; the gripper is CARRIED, never taken
    from the Position (C1: driving it would drop a carried cube)."""
    rig = Rig(monkeypatch, pid, simulated=True)
    q = _taught_pose(pid)
    other_gripper = float(rig.profile.gripper_closed_rad)
    start_gripper = float(rig.arm.get_joints()[rig.profile.num_arm_joints])
    assert rig.run(program(_move('P'), destinations=[_entry(pid, 'P', q, other_gripper)])) == 'finished', rig.errors()
    assert _arm(rig) == pytest.approx(q, abs=1e-6)
    assert rig.arm.get_joints()[rig.profile.num_arm_joints] == pytest.approx(start_gripper, abs=1e-9)


def test_other_robot_pose_takes_the_point_and_warns(monkeypatch):
    """A Position taught on another robot keeps the point path and says so once."""
    rig = Rig(monkeypatch, 'omx_full', simulated=True)
    q = _taught_pose('omx_full')
    edu1 = rp.ROBOT_PROFILES['edu1_studio']
    foreign = _entry('omx_full', 'P', q, 0.0, robot_type=edu1.data_robot_type,
                     joint_names=list(edu1.joint_names))
    phase = rig.run(program(chain(_move('P'), _move('P')), destinations=[foreign]))
    assert phase == 'finished', rig.errors()
    warn = ('[WARNUNG] Die Position „P" wurde an einem anderen Roboter vorgemacht — '
            'der Arm fährt nur zu ihrem Punkt, nicht in ihre Armstellung.')
    assert rig.logs().count(warn) == 1
    # The point path: the end effector sits on the stored point, the wrist is
    # the fixed jaw roll — not the taught one.
    _R, t = rig.ik.fk(_arm(rig))
    assert [float(v) for v in t] == pytest.approx([foreign['x'], foreign['y'], foreign['z']], abs=2e-3)
    assert _arm(rig) != pytest.approx(q, abs=1e-3)


def _clearance_fn(pid, ik):
    if pid == 'omx_full':
        start = hp._moving_point_start(ik)
        return lambda q: hp._point_clearance(ik, q, lambda _x, _y: 0.0, start)
    prof = rp.ROBOT_PROFILES[pid]
    geom = ag.resolve_geometry(types.SimpleNamespace(ik=ik, arm_profile=prof))
    return lambda q: geom.floor_clearance(q, lambda _x, _y: 0.0)


def _grasp_like(pid, count, seed):
    """Strict-vertical poses whose fingertip is 0–50 mm above the table."""
    prof = rp.ROBOT_PROFILES[pid]
    ik = prof.build_ik()
    tip = float(getattr(ik, 'tool_tip_below_ee_m', 0.0))
    clear = _clearance_fn(pid, ik)
    rnd = random.Random(seed)
    out = []
    while len(out) < count:
        r = rnd.uniform(0.10, 0.25)
        a = rnd.uniform(-1.4, 1.4)
        z = tip + rnd.uniform(0.0, 0.05)
        q = ik.solve(target_xyz=(r * math.cos(a), r * math.sin(a), z),
                     seed=list(prof.home_joints_rad), roll=M.GRASP_ROLL_RAD)
        if q is None:
            continue
        q = list(q)
        c = clear(q)
        if c is None or c < 0.0:
            continue
        out.append(q)
    return out


@pytest.mark.parametrize('pid', ['omx_full', 'edu1_studio'])
def test_grasp_like_near_table_positions_never_refuse(monkeypatch, pid):
    """The acceptance bound: grasp-like Positions are never refused (HOME→P 40,
    A→B 20, seeded)."""
    poses = _grasp_like(pid, 40, seed=7)
    g = float(rp.ROBOT_PROFILES[pid].gripper_open_rad)
    refused = []
    for i, q in enumerate(poses):
        rig = Rig(monkeypatch, pid, simulated=True)
        if rig.run(program(_move('P'), destinations=[_entry(pid, 'P', q, g)])) != 'finished':
            refused.append(('HOME→P', i, rig.errors()))
    for i in range(0, 40, 2):
        rig = Rig(monkeypatch, pid, simulated=True)
        payload = program(chain(_move('A'), _move('B')),
                          destinations=[_entry(pid, 'A', poses[i], g),
                                        _entry(pid, 'B', poses[i + 1], g)])
        if rig.run(payload) != 'finished':
            refused.append(('A→B', i, rig.errors()))
    assert refused == []


def test_refusal_names_the_position(monkeypatch):
    """A Position whose every route would press a link through the table is
    refused with ITS name, never the Grundstellung's sentence."""
    rig = Rig(monkeypatch, 'omx_full', simulated=True)
    # A hand pose with every moving point above the table (found by the seeded
    # near-table sweep): the straight joint line from HOME, the reversed line
    # and both halves via the Grundstellung all dip a link below z = 0.
    q = [-0.6729, 0.2959, 1.1375, 1.5913, 0.5832]
    assert 0.0 <= _clearance_fn('omx_full', rig.ik)(q) <= 0.03
    phase = rig.run(program(_move('Tief'), destinations=[_entry('omx_full', 'Tief', q, 0.8)]))
    assert phase == 'error'
    assert rig.errors()[-1] == ('Der Weg zur Position „Tief" würde durch die Tischebene führen — '
                                'bitte die Position etwas höher neu vormachen.')


def test_limit_tolerance_5_mrad(monkeypatch):
    """Within 5 mrad of a joint limit the pose is clamped and driven; beyond it
    the Position is refused in German."""
    rig = Rig(monkeypatch, 'edu1_studio', simulated=True)
    lims = rig.ik.joint_limits
    q = _taught_pose('edu1_studio')
    near = list(q)
    near[0] = lims[0][1] + 0.004
    phase = rig.run(program(_move('P'), destinations=[_entry('edu1_studio', 'P', near, 0.0)]))
    assert phase == 'finished', rig.errors()
    assert _arm(rig)[0] == pytest.approx(lims[0][1], abs=1e-9)
    far = list(q)
    far[0] = lims[0][1] + 0.02
    rig = Rig(monkeypatch, 'edu1_studio', simulated=True)
    phase = rig.run(program(_move('P'), destinations=[_entry('edu1_studio', 'P', far, 0.0)]))
    assert phase == 'error'
    assert rig.errors()[-1] == 'Die Position „P" liegt außerhalb der Gelenkgrenzen dieses Arms.'


_Q9_BLOCKLY = ('„P" ist eine Position — dieser Block braucht ein Ziel auf dem Tisch. '
               'Für eine Position bitte „bewege zu" und dann „öffne Greifer" bzw. '
               '„schließe Greifer" benutzen.')


@pytest.mark.parametrize('with_joints', [True, False])
@pytest.mark.parametrize('block', ['edubotics_drop_at', 'edubotics_pickup'])
def test_drop_at_and_pickup_refuse_a_position(monkeypatch, block, with_joints):
    """Q9: a taught Position (with or without joints) in „ablegen bei" /
    „aufnehmen" is refused with the hint to use „bewege zu" + the gripper."""
    rig = Rig(monkeypatch, 'omx_full', simulated=True)
    key = 'DESTINATION' if block == 'edubotics_drop_at' else 'TARGET'
    entry = _entry('omx_full', 'P', _taught_pose('omx_full'), 0.8, joints=with_joints)
    phase = rig.run(program(B(block, inputs={key: ref('P')}), destinations=[entry]))
    assert phase == 'error'
    assert rig.errors()[-1] == _Q9_BLOCKLY


def test_pin_current_is_a_ziel_and_never_refused(monkeypatch):
    """Q12: „setze C = aktuelle Position" stores a Ziel (xyz only); „ablegen bei
    C" keeps working. The program first moves the arm into reach (at HOME on the
    Edu:1 the place would be „außerhalb des Arbeitsbereichs")."""
    for pid in ('omx_full', 'edu1_studio'):
        rig = Rig(monkeypatch, pid, simulated=True)
        q = _taught_pose(pid)
        payload = program(chain(
            _move('P'),
            B('edubotics_destination_current', fields={'NAME': 'C'}),
            B('edubotics_drop_at', inputs={'DESTINATION': ref('C')})),
            destinations=[_entry(pid, 'P', q, float(rig.profile.gripper_open_rad))])
        assert rig.run(payload) == 'finished', (pid, rig.errors())


def _code_ctx(language, entry):
    """A bare run context for one handler call in a text program."""
    prof = rp.ROBOT_PROFILES['omx_full']
    ctx = WorkflowContext(publisher=lambda _pts: None, ik=prof.build_ik(),
                          destinations={'P': entry}, code_language=language)
    ctx.last_full_joints = list(prof.home_joints_rad) + [0.8]
    return ctx


@pytest.mark.parametrize('language,expected', [
    (None, _Q9_BLOCKLY),
    ('python', '„P" ist eine Position — drop_at braucht ein Ziel auf dem Tisch. '
               'Für eine Position bitte move_to und dann open_gripper bzw. close_gripper benutzen.'),
    ('java', '„P" ist eine Position — dropAt braucht ein Ziel auf dem Tisch. '
             'Für eine Position bitte moveTo und dann openGripper bzw. closeGripper benutzen.'),
])
def test_q9_sentence_follows_the_language(language, expected):
    """The refusal names the method the student wrote, in the run's language."""
    entry = {'x': 0.17, 'y': 0.0, 'z': 0.05, 'label': 'P', 'plane_tracked': False, 'kind': 'pose'}
    with pytest.raises(M.WorkflowError) as exc:
        M.drop_at(_code_ctx(language, entry), {'destination': 'P'})
    assert str(exc.value) == expected


def test_pickup_refuses_by_its_target_key():
    """``pickup`` reads its argument under ``target`` (r3-m6: keyed on
    ``destination`` it refused nothing)."""
    entry = {'x': 0.17, 'y': 0.0, 'z': 0.05, 'label': 'P', 'plane_tracked': False, 'kind': 'pose'}
    with pytest.raises(M.WorkflowError) as exc:
        M.pickup(_code_ctx('python', entry), {'target': 'P'})
    assert str(exc.value).startswith('„P" ist eine Position — pickup braucht')


def test_a_non_direct_route_logs_one_line(monkeypatch):
    """A route over a via logs exactly ONE line naming the Position; the
    planners' own Grundstellung notes never reach the student."""
    prof = rp.ROBOT_PROFILES['omx_full']
    ik = prof.build_ik()
    clear = _clearance_fn('omx_full', ik)
    rnd = random.Random(11)
    found = None
    for _ in range(4000):
        q = [rnd.uniform(-1.2, 1.2), rnd.uniform(-0.3, 1.5), rnd.uniform(-1.4, 1.2),
             rnd.uniform(-1.6, 1.6), rnd.uniform(-1.0, 1.0)]
        c = clear(q)
        if c is None or not (0.0 <= c <= 0.03):
            continue
        rig = Rig(monkeypatch, 'omx_full', simulated=True)
        if rig.run(program(_move('P'), destinations=[_entry('omx_full', 'P', q, 0.8)])) != 'finished':
            continue
        lines = [m for m in rig.logs() if 'Position' in m or 'Grundstellung' in m or 'angehoben' in m]
        if lines:
            found = lines
            break
    assert found is not None, 'no near-table pose took a via route'
    assert len(found) == 1
    assert found[0] in ('Fahre zur Position „P" über einen Zwischenpunkt.',
                        'Fahre zur Position „P" über die Grundstellung.')


def test_a_zone_blocked_position_names_the_sperrzone(monkeypatch):
    """A Sperrzone around the target is refused with path_guard's zone sentence,
    never the floor sentence (r4-M2: only home_planner.FloorRefusal falls
    through the ladder). A regression guard: main names the zone too."""
    rig = Rig(monkeypatch, 'edu1_studio', simulated=True)
    ik = rig.ik
    q = ik.solve(target_xyz=(0.20, 0.05, 0.03), seed=list(rig.profile.home_joints_rad),
                 roll=M.GRASP_ROLL_RAD)
    assert q is not None
    zone = {'min': [0.17, 0.02, 0.0], 'max': [0.23, 0.08, 0.06]}
    phase = rig.run(program(_move('P'), destinations=[_entry('edu1_studio', 'P', list(q), 0.0)],
                            zones=[zone]))
    assert phase == 'error'
    err = rig.errors()[-1]
    assert 'Sperrzone' in err
    assert 'Tischebene' not in err


def test_route_is_planned_under_the_lock(monkeypatch):
    """M2: the route's first leg starts from the pose read INSIDE the motion
    lock. A thread that moves the arm between ``move_to``'s entry and its lock
    must not leave a first leg the floor ladder never judged."""
    import json
    import time as _time

    from physical_ai_server.workflow.workflow_manager import WorkflowManager
    from rig_emulation import fast_pacing

    fast_pacing(monkeypatch)
    prof = rp.ROBOT_PROFILES['omx_full']
    q_target = _taught_pose('omx_full')
    dests, _skip, _present = WorkflowManager._parse_destinations(
        json.dumps({'destinations': [_entry('omx_full', 'P', q_target, 0.8)]}))
    lock = threading.RLock()
    ctx = WorkflowContext(publisher=lambda _pts: None, ik=prof.build_ik(),
                          destinations=dests, motion_lock=lock,
                          num_arm_joints=prof.num_arm_joints,
                          home_joints_rad=prof.home_joints_rad,
                          gripper_open_rad=prof.gripper_open_rad,
                          gripper_closed_rad=prof.gripper_closed_rad)
    ctx.last_full_joints = list(prof.home_joints_rad) + [0.8]
    legs = []
    real_safe_move = M.safe_move
    monkeypatch.setattr(M, 'safe_move', lambda c, a, b, d, **kw: (
        legs.append((list(a), list(b))), real_safe_move(c, a, b, d, **kw)))
    moved_to = [0.3, -1.0, 0.9, 0.2, 0.1, 0.8]
    lock.acquire()                           # another thread holds the arm …
    t = threading.Thread(target=M.move_to, args=(ctx, {'destination': 'P'}))
    t.start()
    _time.sleep(0.3)                         # … move_to has entered and queues
    ctx.last_full_joints = list(moved_to)    # the holder moved the arm meanwhile
    lock.release()
    t.join(10)
    assert not t.is_alive()
    assert legs, 'nothing was published'
    assert legs[0][0] == pytest.approx(moved_to, abs=1e-9)
    assert list(ctx.last_full_joints)[:5] == pytest.approx(q_target, abs=1e-6)


def test_edu6_keeps_the_xyz_path(monkeypatch):
    """m12: Positions on edu6 are a non-goal — the point path, no warning."""
    rig = Rig(monkeypatch, 'edu6_studio', simulated=True)
    ik = rig.ik
    q = ik.solve(target_xyz=(0.15, 0.02, 0.04), seed=list(rig.profile.home_joints_rad),
                 roll=M.GRASP_ROLL_RAD + 0.3)
    assert q is not None
    entry = _entry('edu6_studio', 'P', list(q), 0.0)
    assert rig.run(program(_move('P'), destinations=[entry])) == 'finished', rig.errors()
    assert not [m for m in rig.logs() if 'Position' in m]
    _R, t = ik.fk(_arm(rig))
    assert [float(v) for v in t] == pytest.approx([entry['x'], entry['y'], entry['z']], abs=2e-3)


def test_precheck_skips_a_joint_route_position(monkeypatch):
    """The start-time IK pre-check judges a POINT; a Position that takes the
    joint route is not a point it has to reach, so it is not warned about. (The
    Edu:1 HOME-like pose is outside the strict-vertical image: the old pre-check
    flagged „außerhalb des Arbeitsbereichs" for a move that runs fine.)"""
    rig = Rig(monkeypatch, 'edu1_studio', simulated=True)
    q = [0.3, 0.64, 1.48, 0.90, 0.0]          # upright, claw up: no vertical solve
    entry = _entry('edu1_studio', 'P', q, 0.0)
    assert rig.ik.solve(target_xyz=(entry['x'], entry['y'], entry['z']),
                        seed=q, free_yaw=True) is None
    warnings = rig.start(program(_move('P'), destinations=[entry]))
    assert rig.done.wait(30)
    assert warnings == []
    assert rig.final['phase'] == 'finished', rig.errors()
    # A Position taught on another robot keeps the point path, so its point is
    # still pre-checked.
    rig = Rig(monkeypatch, 'edu1_studio', simulated=True)
    foreign = _entry('edu1_studio', 'P', q, 0.0, robot_type='omx_f')
    warnings = rig.start(program(_move('P'), destinations=[foreign]))
    assert rig.done.wait(30)
    assert [w['message'] for w in warnings] == ['Diese Position ist außerhalb des Arbeitsbereichs.']


# ── fix round: a Position taught below THIS rig's table is refused ─────────
# The reversed rung of ``_pose_route`` lets the TARGET set its own floor
# allowance, so a pose taught below the table used to be driven to (OMX EE
# z −0.031 with a link at −0.067, Edu:1 EE z −0.032, both „finished"). The
# taught pose's own clearance is now judged first (``home_planner.
# pose_floor_clearance``) against the touch-off plane minus the solver's
# ``tool_tip_below_ee_m``, else z = 0; more than ``FLOOR_TOL_M`` below refuses.

_BELOW_TABLE = ('Die Position „P" liegt unter der Tischebene — '
                'bitte die Position an diesem Roboter neu vormachen.')


def _below_table_pose(pid):
    """A taught pose with the end effector ~3 cm BELOW the base plane: a
    grasp-like solve, then joint2 pushed towards the table (the verifier's
    probe)."""
    prof = rp.ROBOT_PROFILES[pid]
    ik = prof.build_ik()
    pt = {'omx_full': (0.20, 0.0, 0.06), 'edu1_studio': (0.20, 0.0, 0.03)}[pid]
    q = [float(v) for v in ik.solve(target_xyz=pt, seed=list(prof.home_joints_rad),
                                    roll=M.GRASP_ROLL_RAD)][:prof.num_arm_joints]
    lo, hi = ik.joint_limits[1]
    for step in range(121):
        qq = list(q)
        qq[1] += step * 0.01
        assert lo <= qq[1] <= hi, 'ran out of joint2 before the EE went below the table'
        _R, t = ik.fk(qq)
        if float(t[2]) < -0.03:
            return qq
    raise AssertionError('no below-table pose found')


def _touching_pose(pid, fingertip_z):
    """A strict-vertical pose whose closed FINGERTIP sits at ``fingertip_z``."""
    prof = rp.ROBOT_PROFILES[pid]
    ik = prof.build_ik()
    tip = float(getattr(ik, 'tool_tip_below_ee_m', 0.0))
    q = ik.solve(target_xyz=(0.18, 0.02, tip + fingertip_z),
                 seed=list(prof.home_joints_rad), roll=M.GRASP_ROLL_RAD)
    assert q is not None
    return [float(v) for v in q]


def _count_moves(monkeypatch):
    moves = []
    real = M.safe_move
    monkeypatch.setattr(M, 'safe_move', lambda c, a, b, d, **kw: (
        moves.append((list(a), list(b))), real(c, a, b, d, **kw)))
    return moves


def _plain_ctx(pid, *, z_table=None, table_plane=None, ik=None):
    prof = rp.ROBOT_PROFILES[pid]
    ctx = WorkflowContext(publisher=lambda _pts: None, ik=ik or prof.build_ik(),
                          z_table=z_table)
    ctx.table_plane = table_plane
    return ctx


@pytest.mark.parametrize('pid', ['omx_full', 'edu1_studio'])
def test_a_position_below_the_table_is_refused_and_nothing_moves(monkeypatch, pid):
    """The verifier's case on both joint-route arms: the same point a Ziel
    refuses („Zielpunkt liegt unter der Tischebene.") is refused as a Position
    too, with its own sentence, before any leg is published."""
    q = _below_table_pose(pid)
    tip = float(getattr(rp.ROBOT_PROFILES[pid].build_ik(), 'tool_tip_below_ee_m', 0.0))
    clearance = hp.pose_floor_clearance(_plain_ctx(pid, z_table=tip), q)
    assert clearance is not None and clearance < -hp.FLOOR_TOL_M
    rig = Rig(monkeypatch, pid, simulated=True)
    before = _arm(rig)
    moves = _count_moves(monkeypatch)
    phase = rig.run(program(_move('P'), destinations=[_entry(pid, 'P', q, 0.0)]))
    assert phase == 'error'
    assert rig.errors()[-1] == _BELOW_TABLE
    assert moves == []
    assert _arm(rig) == pytest.approx(before, abs=1e-12)
    assert not [m for m in rig.logs() if 'Zwischenpunkt' in m or 'Grundstellung' in m]


@pytest.mark.parametrize('pid', ['omx_full', 'edu1_studio'])
@pytest.mark.parametrize('fingertip_z', [0.0, -0.003])
def test_a_position_taught_touching_the_table_still_runs(monkeypatch, pid, fingertip_z):
    """Fingertips on the table (and a few mm of readback below it) is the
    ordinary taught Position near the table: ``FLOOR_TOL_M`` covers it."""
    q = _touching_pose(pid, fingertip_z)
    rig = Rig(monkeypatch, pid, simulated=True)
    phase = rig.run(program(_move('P'), destinations=[_entry(pid, 'P', q, 0.0)]))
    assert phase == 'finished', rig.errors()
    assert _arm(rig) == pytest.approx(q, abs=1e-6)


@pytest.mark.parametrize('pid', ['omx_full', 'edu1_studio'])
def test_a_riser_kit_is_judged_by_its_own_measured_table(monkeypatch, pid):
    """An arm on a 5 cm riser: its touch-off measures the table BELOW z = 0. A
    Position touching that table runs; the same Position on a rig whose table
    is the base plane is below it and refused."""
    riser = -0.05
    tip = float(getattr(rp.ROBOT_PROFILES[pid].build_ik(), 'tool_tip_below_ee_m', 0.0))
    q = _touching_pose(pid, riser)
    measured = Rig(monkeypatch, pid, simulated=True, z_table=tip + riser)
    phase = measured.run(program(_move('P'), destinations=[_entry(pid, 'P', q, 0.0)]))
    assert phase == 'finished', measured.errors()
    assert _arm(measured) == pytest.approx(q, abs=1e-6)
    flat = Rig(monkeypatch, pid, simulated=True)
    phase = flat.run(program(_move('P'), destinations=[_entry(pid, 'P', q, 0.0)]))
    assert phase == 'error'
    assert flat.errors()[-1] == _BELOW_TABLE


def test_the_threshold_is_floor_tol_below_the_surface():
    """On the OMX the lowest moving point of a vertical pose is its fingertip
    sample; 1 mm inside ``FLOOR_TOL_M`` passes, 1 mm beyond refuses. The OMX
    touch-off's ``z_table`` is the EE height (fingertip + 0.04), so the surface
    is ``z_table − tool_tip_below_ee_m``; a tilted plane is honoured per point."""
    tol = hp.FLOOR_TOL_M
    assert tol == pytest.approx(0.020)
    inside = _touching_pose('omx_full', -tol + 0.001)
    beyond = _touching_pose('omx_full', -tol - 0.001)
    ctx = _plain_ctx('omx_full', z_table=0.04)
    assert hp.pose_floor_clearance(ctx, inside) == pytest.approx(-tol + 0.001, abs=2e-4)
    M._refuse_position_below_table(ctx, inside + [0.8], 'P')
    with pytest.raises(M.WorkflowError) as exc:
        M._refuse_position_below_table(ctx, beyond + [0.8], 'P')
    assert str(exc.value) == _BELOW_TABLE
    # No table measured → the base plane z = 0: the same answer.
    M._refuse_position_below_table(_plain_ctx('omx_full'), inside + [0.8], 'P')
    # A measured plane wins over the scalar: lowered 10 mm under the pose, the
    # beyond pose passes.
    plane = (0.0, 0.0, 0.04 - 0.010)
    M._refuse_position_below_table(_plain_ctx('omx_full', z_table=0.04, table_plane=plane),
                                   beyond + [0.8], 'P')


def test_geometry_that_cannot_be_judged_is_refused():
    """An unknown is never an approval: a solver whose link sampling fails, or a
    non-finite table, refuses the Position."""
    ik = rp.ROBOT_PROFILES['omx_full'].build_ik()
    q = _touching_pose('omx_full', 0.01)

    class _Broken:
        backend = ik.backend
        joint_limits = ik.joint_limits
        tool_tip_below_ee_m = ik.tool_tip_below_ee_m

        def num_joints(self):
            return ik.num_joints()

        def link_points(self, _q):
            raise ValueError('no geometry')

    for ctx in (_plain_ctx('omx_full', ik=_Broken()),
                _plain_ctx('omx_full', z_table=float('nan'))):
        assert hp.pose_floor_clearance(ctx, q) is None
        with pytest.raises(M.WorkflowError) as exc:
            M._refuse_position_below_table(ctx, q + [0.8], 'P')
        assert str(exc.value) == _BELOW_TABLE


@pytest.mark.parametrize('pid', ['omx_full', 'edu1_studio'])
def test_floor_tol_zero_rolls_the_check_back(monkeypatch, pid):
    """``EDUBOTICS_HOME_FLOOR_TOL_M=0`` (``FLOOR_TOL_M <= 0``) disables the check
    like the ladder: the below-table Position is driven as before."""
    monkeypatch.setattr(hp, 'FLOOR_TOL_M', 0.0)
    q = _below_table_pose(pid)
    rig = Rig(monkeypatch, pid, simulated=True)
    phase = rig.run(program(_move('P'), destinations=[_entry(pid, 'P', q, 0.0)]))
    assert phase == 'finished', rig.errors()
    assert _arm(rig) == pytest.approx(q, abs=1e-6)


def test_one_reversed_leg_is_a_direct_move_and_says_nothing(monkeypatch):
    """Fix 2: the reversed rung is labelled by leg count like the forward one.
    A Position 1 mm under the table refuses forward (the point model allows
    nothing below z = 0 from HOME) and plans ONE reversed leg: a straight move,
    never „über einen Zwischenpunkt"."""
    pid = 'omx_full'
    prof = rp.ROBOT_PROFILES[pid]
    q = _touching_pose(pid, -0.001)
    ctx = _plain_ctx(pid, z_table=0.04)
    home = list(prof.home_joints_rad) + [0.8]
    with pytest.raises(hp.FloorRefusal):
        hp.plan_floor_checked_home_route(ctx, home, q + [0.8], M.DEFAULT_MOVE_DURATION_S)
    legs, rung = M._pose_route(ctx, home, q + [0.8], 'P')
    assert (rung, len(legs)) == ('direct', 1)
    rig = Rig(monkeypatch, pid, simulated=True)
    moves = _count_moves(monkeypatch)
    phase = rig.run(program(_move('P'), destinations=[_entry(pid, 'P', q, 0.8)]))
    assert phase == 'finished', rig.errors()
    assert len(moves) == 1
    assert not [m for m in rig.logs() if 'Position' in m]

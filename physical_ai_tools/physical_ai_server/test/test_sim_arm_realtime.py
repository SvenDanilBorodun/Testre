#!/usr/bin/env python3
"""SimArm plays every chunk in REAL TIME, the way the rig's controller does.

``chunked_publish`` hands over one second of waypoints and then sleeps that second.
The follower's JointTrajectoryController moves through them over their
``time_from_start``; the simulator had no controller and forwarded all 30 in a tight
loop. Measured before this change: a 3 s move left as three 30-pose bursts 0.02 ms
wide, one second apart — the browser saw 6 of 90 poses, the twin jumped up to 48° and
showed the arm up to 45° ahead of the program, then froze ~0.9 s.

Timing assertions here are deliberately loose (tens of ms): they separate "played
over the span" from "dumped in a burst", which is a difference of three orders of
magnitude, without betting on a loaded CI runner's scheduler.
"""

from __future__ import annotations

import threading
import time

import pytest

from physical_ai_server import robot_profiles
from physical_ai_server.workflow import trajectory_builder
from physical_ai_server.workflow.ik_solver import IKSolver
from physical_ai_server.workflow.sim_arm import SimArm
from physical_ai_server.workflow.sim_world import SimWorld


GRIPPER_OPEN_RAD = 0.8
GRIPPER_CLOSED_RAD = -0.5
WUERFEL_GRASP_Z = 0.030 - 0.015


class _Recorder:
    """A frame sink that timestamps every frame on arrival."""

    def __init__(self):
        self.lock = threading.Lock()
        self.frames = []          # (monotonic, q, scene, late_s)

    def __call__(self, q, scene, late_s):
        with self.lock:
            self.frames.append((time.monotonic(), list(q), scene, late_s))

    def snapshot(self):
        with self.lock:
            return list(self.frames)


def _wait_for(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def _chunk(n, dt, start=0.0, joint=0):
    """n waypoints dt apart, joint `joint` ramping from `start` (6-wide OMX)."""
    out = []
    for i in range(1, n + 1):
        q = [0.0, -1.5708, 1.5708, 0.0, 0.0, GRIPPER_OPEN_RAD]
        q[joint] = start + 0.01 * i
        out.append((q, i * dt))
    return out


@pytest.fixture
def rec_arm():
    rec = _Recorder()
    arm = SimArm(frame_sink=rec)
    yield rec, arm
    arm.close()


# ── the player ───────────────────────────────────────────────────────────────

def test_publish_returns_at_once_and_the_frames_follow_their_own_times(rec_arm):
    rec, arm = rec_arm
    chunk = _chunk(10, 0.04)                 # 0.40 s of motion
    t0 = time.monotonic()
    arm.publish(chunk)
    assert time.monotonic() - t0 < 0.05, 'publish() must not block like a sleep'
    assert _wait_for(lambda: len(rec.snapshot()) == 10)
    frames = rec.snapshot()
    offsets = [f[0] - t0 for f in frames]
    # Each frame no EARLIER than its time_from_start (a burst fails this at once) …
    for (_, t), off in zip(chunk, offsets):
        assert off >= t - 0.01, f'frame due at {t:.2f}s went out at {off:.3f}s'
    # … and the whole chunk spread over its span, not over microseconds.
    assert offsets[-1] - offsets[0] >= 0.30
    # Order and content preserved.
    assert [f[1][0] for f in frames] == pytest.approx([q[0] for q, _ in chunk])
    assert all(f[3] >= 0.0 for f in frames), 'lateness is never negative'


def test_the_runtime_sees_the_chunk_END_before_a_single_frame_is_played(rec_arm):
    """What the runtime READS stays synchronous: the cached commanded pose is the
    chunk's end the moment publish() returns, as on the rig (_last_published_joints)."""
    rec, arm = rec_arm
    chunk = _chunk(10, 0.05)
    arm.publish(chunk)
    assert arm.get_joints() == pytest.approx(chunk[-1][0])
    assert len(rec.snapshot()) <= 1
    assert arm.playing


def test_a_newer_chunk_replaces_the_one_in_flight(rec_arm):
    """Controller semantics: frames of the old chunk already due are emitted, the
    rest are dropped, and the new chunk plays from its own receipt."""
    rec, arm = rec_arm
    old = _chunk(10, 0.05, start=0.0)        # 0.5 s, joint1 0.01 … 0.10
    new = _chunk(5, 0.04, start=1.0)         # joint1 1.01 … 1.05
    t1 = time.monotonic()
    arm.publish(old)
    time.sleep(0.16)
    arm.publish(new)
    elapsed = time.monotonic() - t1
    assert _wait_for(lambda: any(abs(f[1][0] - 1.05) < 1e-9 for f in rec.snapshot()))
    time.sleep(0.5)                           # past the old chunk's whole span
    j1 = [f[1][0] for f in rec.snapshot()]
    olds = [v for v in j1 if v < 0.5]
    due = sum(1 for _q, t in old if t <= elapsed)
    # Exactly the frames that were due by the preemption (± one at the edge) …
    assert due - 1 <= len(olds) <= due + 1, f'due {due}, emitted {olds}'
    # … and never the ones still in the future.
    assert len(olds) < len(old), 'the old chunk played to its end after preemption'
    assert olds == pytest.approx([0.01 * (i + 1) for i in range(len(olds))])
    assert j1[len(olds):] == pytest.approx([1.01, 1.02, 1.03, 1.04, 1.05])


def test_set_objects_cancels_playback_so_a_reset_can_never_be_overwritten(rec_arm):
    """The reset publishes HOME right after set_objects(). A queued waypoint of the
    run it ended must not land on top of it — the class of bug where the twin sat
    off HOME with the heartbeat pinning the wrong pose."""
    rec, arm = rec_arm
    arm.publish(_chunk(10, 0.05))
    time.sleep(0.12)
    arm.set_objects([])
    after_cancel = len(rec.snapshot())
    time.sleep(0.6)
    assert len(rec.snapshot()) == after_cancel, 'a cancelled chunk kept playing'
    assert not arm.playing


def test_the_player_thread_survives_a_raising_sink():
    calls = []

    def sink(q, scene, late):
        calls.append(q[0])
        raise RuntimeError('publisher gone')

    arm = SimArm(frame_sink=sink)
    try:
        arm.publish(_chunk(3, 0.01))
        assert _wait_for(lambda: len(calls) == 3)
        arm.publish(_chunk(2, 0.01, start=1.0))
        assert _wait_for(lambda: len(calls) == 5)
    finally:
        arm.close()


def test_without_a_frame_sink_the_burst_path_is_unchanged():
    """Every unit test and the golden fixture construct SimArm this way."""
    captured = []
    arm = SimArm(joint_state_sink=captured.append)
    chunk = _chunk(5, 0.2)
    arm.publish(chunk)
    assert captured == [pytest.approx(q) for q, _ in chunk], (
        'the legacy sink must still get every point, synchronously, in order')
    assert not arm.playing


def test_an_absurd_waypoint_TIME_cannot_kill_the_player(rec_arm):
    """A finite time_from_start above threading.TIMEOUT_MAX makes
    `Condition.wait` raise OverflowError, which would end the playback thread for
    the life of the node — every later chunk silently dropped, `playing` stuck
    True. No caller can produce one today; the sliced wait is the fence."""
    rec, arm = rec_arm
    q = [0.0, -1.5708, 1.5708, 0.0, 0.0, GRIPPER_OPEN_RAD]
    arm.publish([(list(q), 1e12)])
    time.sleep(0.05)
    arm.publish(_chunk(3, 0.02, start=1.0))
    assert _wait_for(lambda: len(rec.snapshot()) == 3), 'the player thread died'
    assert [f[1][0] for f in rec.snapshot()] == pytest.approx([1.01, 1.02, 1.03])


def test_a_non_finite_time_plays_at_once_instead_of_never(rec_arm):
    rec, arm = rec_arm
    q = [0.0, -1.5708, 1.5708, 0.0, 0.0, GRIPPER_OPEN_RAD]
    arm.publish([(list(q), float('inf')), (list(q), float('nan')), (list(q), -1.0)])
    assert _wait_for(lambda: len(rec.snapshot()) == 3, timeout=1.0)


# ── end to end through the real chunked_publish ──────────────────────────────

def test_the_waypoint_rate_the_twin_is_tuned_for_is_30_hz():
    """The React twin subscribes to /sim/joint_states with a 20 ms throttle
    (SimScene::SIM_JOINT_THROTTLE_MS) so that every waypoint of a 30 Hz stream
    gets through, and interpolates with a 100 ms delay sized for ~33 ms spacing.
    A faster waypoint rate would be silently thinned by that throttle; a much
    slower one would outrun the delay. Change them together."""
    assert trajectory_builder.DEFAULT_FPS == 30


def test_chunked_publish_now_streams_evenly_instead_of_in_bursts(rec_arm):
    """The measurement that started this, as a test: the same kind of move that
    left in three 30-pose bursts now leaves as an even ~30 Hz stream."""
    rec, arm = rec_arm
    pts = trajectory_builder.build_segment(
        [0.0, -1.5708, 1.5708, 0.0, 0.0, GRIPPER_OPEN_RAD],
        [1.2, -1.0, 1.0, 0.3, 0.2, GRIPPER_OPEN_RAD], 1.2)   # 36 waypoints, 2 chunks
    t0 = time.monotonic()
    assert trajectory_builder.chunked_publish(arm.publish, pts, lambda: False)
    run = time.monotonic() - t0
    # EVERY waypoint, with no slack: the last point of each chunk is due exactly
    # when `_pace` hands over the next one, and it survives only because the
    # player FLUSHES what is already due before switching. A "len - 1" allowance
    # here is precisely the hole a lost chunk-final frame slips through.
    assert _wait_for(lambda: len(rec.snapshot()) == len(pts))
    frames = rec.snapshot()
    gaps = [b[0] - a[0] for a, b in zip(frames, frames[1:])]
    dt = 1.0 / trajectory_builder.DEFAULT_FPS
    # The burst path froze ~0.9 s between chunks; allow a slow runner 0.25 s.
    assert max(gaps) < 0.25, f'a {max(gaps) * 1000:.0f} ms freeze — still bursting?'
    # A burst puts ~30 frames inside 1 ms; spread means the median gap is ~dt.
    assert sorted(gaps)[len(gaps) // 2] > 0.5 * dt
    # The runtime's own pacing is untouched: same wall time as the rig path.
    assert run == pytest.approx(1.2, abs=0.3)
    assert len(frames) == len(pts)


# ── the world, per waypoint ──────────────────────────────────────────────────

def _cube_world():
    w = SimWorld([{'type': 'wuerfel', 'tag_id': 0, 'x': 0.20, 'y': 0.0, 'yaw': 0.0}])
    w.bind_tag(0, 20)
    return w


def _pose(ik, xyz, gripper):
    q = ik.solve(xyz)
    assert q is not None, f'{xyz} must be reachable for this test to mean anything'
    return list(q) + [gripper]


def test_a_close_WHILE_MOVING_captures_where_the_jaws_actually_crossed():
    """A replay can close the gripper while the arm travels. Judged once at the
    chunk's END the capture looked for the cube a whole chunk of motion away from
    where the jaws crossed — outside the 6 cm radius it simply missed."""
    ik = IKSolver()
    world = _cube_world()
    arm = SimArm(ik=ik, world=world)
    at_cube = _pose(ik, (0.20, 0.0, WUERFEL_GRASP_Z), GRIPPER_OPEN_RAD)
    closed_at_cube = _pose(ik, (0.20, 0.0, WUERFEL_GRASP_Z), GRIPPER_CLOSED_RAD)
    far = _pose(ik, (0.12, 0.14, 0.08), GRIPPER_CLOSED_RAD)
    arm.publish([(at_cube, 0.1), (closed_at_cube, 0.2), (far, 0.3)])
    assert world.is_held(), 'the jaws closed ON the cube and missed it'
    o = world.objects()[0]
    xyz = ik.fk(far[:5])[1]
    assert (o['x'], o['y']) == pytest.approx((xyz[0], xyz[1]), abs=1e-9), (
        'a held cube rides to the chunk end')


def test_an_open_WHILE_MOVING_drops_the_cube_where_the_jaws_opened():
    ik = IKSolver()
    world = _cube_world()
    arm = SimArm(ik=ik, world=world)
    arm.publish([(_pose(ik, (0.20, 0.0, WUERFEL_GRASP_Z), GRIPPER_CLOSED_RAD), 0.1)])
    assert world.is_held()
    drop = _pose(ik, (0.14, 0.12, 0.05), GRIPPER_CLOSED_RAD)
    opened_at_drop = _pose(ik, (0.14, 0.12, 0.05), GRIPPER_OPEN_RAD)
    away = _pose(ik, (0.22, -0.08, 0.10), GRIPPER_OPEN_RAD)
    arm.publish([(drop, 0.1), (opened_at_drop, 0.2), (away, 0.3)])
    assert not world.is_held()
    o = world.objects()[0]
    xyz = ik.fk(drop[:5])[1]
    assert (o['x'], o['y']) == pytest.approx((xyz[0], xyz[1]), abs=1e-9), (
        'the cube must land where the jaws opened, not where the chunk ended')


def test_each_frame_carries_the_scene_AS_OF_its_own_waypoint():
    """The world is settled at publish time, a chunk ahead of the picture. A frame
    carries a snapshot exactly when its waypoint changed the world, so the twin
    sees the grasp when its jaws close — not a second early."""
    ik = IKSolver()
    world = _cube_world()
    rec = _Recorder()
    arm = SimArm(ik=ik, world=world, frame_sink=rec)
    try:
        at_cube = _pose(ik, (0.20, 0.0, WUERFEL_GRASP_Z), GRIPPER_OPEN_RAD)
        closing = [list(at_cube[:5]) + [g] for g in (0.6, 0.3, -0.1, -0.5)]
        arm.publish([(at_cube, 0.02)] + [(q, 0.04 + 0.02 * i)
                                         for i, q in enumerate(closing)])
        assert world.is_held(), 'the runtime must see the capture immediately'
        assert _wait_for(lambda: len(rec.snapshot()) == 5)
        scenes = [f[2] for f in rec.snapshot()]
        # Frames: open, 0.6, 0.3 (still open), -0.1 (CROSSES 0.0 → the capture),
        # -0.5 (still closed, arm unmoved → nothing changed).
        assert scenes[0] is None and scenes[1] is None and scenes[2] is None
        assert scenes[3] is not None and scenes[3]['held'] == 0
        assert scenes[4] is None, 'a gripper-only frame changed the scene'
    finally:
        arm.close()


def test_closing_the_jaws_does_not_DRAG_the_cube_onto_the_gripper():
    """Per-waypoint updates must not move an object the arm is not moving.
    `SimWorld.carry_to` snaps the held object onto the gripper's own XY, so
    carrying on a gripper-only waypoint jerked a just-captured cube up to ~3 cm
    sideways the moment the jaws closed (measured 31.6 mm) — a move the old
    per-CHUNK judgement never made, and one a later `capture_nearest` can see."""
    ik = IKSolver()
    world = _cube_world()
    arm = SimArm(ik=ik, world=world)
    at_cube = _pose(ik, (0.20, 0.0, WUERFEL_GRASP_Z), GRIPPER_OPEN_RAD)
    closed = list(at_cube[:5]) + [GRIPPER_CLOSED_RAD]
    before = (world.objects()[0]['x'], world.objects()[0]['y'])
    arm.publish([(at_cube, 0.03), (closed, 0.06), (list(closed), 0.09)])
    assert world.is_held()
    after = (world.objects()[0]['x'], world.objects()[0]['y'])
    assert after == pytest.approx(before, abs=1e-12), (
        'the cube moved while only the jaws did')
    # …and a MOVING closed arm still carries it (the other half of the rule).
    away = _pose(ik, (0.14, 0.12, 0.06), GRIPPER_CLOSED_RAD)
    arm.publish([(away, 0.03)])
    xyz = ik.fk(away[:5])[1]
    o = world.objects()[0]
    assert (o['x'], o['y']) == pytest.approx((xyz[0], xyz[1]), abs=1e-9)


@pytest.mark.parametrize('profile_id', sorted(robot_profiles.ROBOT_PROFILES))
def test_every_arm_plays_its_own_width_in_real_time(profile_id):
    """The player is width-agnostic: an edu6 frame is 7 wide, edu1 and the OMX 6."""
    prof = robot_profiles.resolve(profile_id)
    n = prof.num_arm_joints
    home = list(prof.home_joints_rad) + [float(prof.gripper_open_rad)]
    rec = _Recorder()
    arm = SimArm(frame_sink=rec, num_arm_joints=n, home_full_joints=home,
                 close_threshold_rad=prof.sim_close_threshold_rad)
    try:
        end = list(home)
        end[0] += 0.3
        pts = trajectory_builder.build_segment(home, end, 0.3)
        arm.publish(pts)
        assert _wait_for(lambda: len(rec.snapshot()) == len(pts))
        assert all(len(f[1]) == n + 1 for f in rec.snapshot())
        assert rec.snapshot()[-1][1] == pytest.approx(end)
    finally:
        arm.close()

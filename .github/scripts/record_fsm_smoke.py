#!/usr/bin/env python3
"""Aufnahme 2.0 — record-FSM smoke test against the REAL LeRobot 0.5.1 writer.

Runs INSIDE the built physical-ai-server image (docker-publish.yml::smoke-test,
with /opt/ros/jazzy and /root/ros2_ws sourced), so everything below is the
shipped code: DataManager, LeRobotDatasetWrapper, LeRobot's DatasetWriter and
its streaming h264 encoder (PyAV). No network: push_to_hub is False.

Each scenario drives DataManager.record() in real time with synthetic frames
and asserts what ends up ON DISK, which is the one thing the stub-based unit
tests cannot see: FINISH in the first warm-up, FINISH < 1 s into a run (Q3),
MOVE_TO_NEXT skipping the warm-up, „Verwerfen und beenden“ (Q4),
„Wiederholen“ and a collision discard with Zurücksetzen 0 (no discarded frame
kept; the next take starts without a real-time hole — the discarded take's
encoder is cancelled before it), one normal episode.
Round 5 adds: a take built by the slot sampler from synthetic timestamped
histories with burnt-in frame counters (two cameras off 30 Hz, jitter, a
300 ms process stall), a forced encoder frame drop detected through LeRobot's
PUBLIC warning (re-recorded twice, the third ends the session — C7), an old
client's multi-task MOVE_TO_NEXT in the first warm-up (no phantom count), the
source-gap re-record (two redos, the third kept), and the official discard's
duration.
Exit code 0 = all scenarios passed.

Robust on a slow or noisy runner by construction:
  * ticks are paced by DEADLINE on a monotonic 30 Hz schedule (a slow record()
    call eats into the next wait instead of stretching every period; a runner
    that falls more than one period behind re-anchors instead of bursting);
  * frame counts are compared with the frames the writer was actually FED (a
    counter around add_frame), never with a wall-clock guess;
  * every timing precondition a scenario relies on (still in the warm-up; a run
    shorter / longer than EARLY_SAVE_MIN_S; inside the RERECORD window) is read
    off the DataManager's own clocks right before the command and, if a stall
    broke it, the scenario is re-run on a fresh dataset (up to ATTEMPTS times)
    instead of asserting something it did not set up.
"""
import glob
import json
import logging
import math
import os
import random
import sys
import tempfile
import time
from pathlib import Path

# LeRobot resolves its dataset home at import time; DataManager's save root and
# LeRobot's must be the same directory (production: ~/.cache/huggingface/lerobot).
ROOT = Path(os.environ.get('EDUBOTICS_SMOKE_ROOT') or tempfile.mkdtemp(prefix='edubotics_smoke_'))
os.environ['HF_LEROBOT_HOME'] = str(ROOT)

import numpy as np  # noqa: E402

from physical_ai_interfaces.msg import TaskInfo  # noqa: E402
from physical_ai_server.communication import capture_timeline  # noqa: E402
from physical_ai_server.data_processing import data_manager as dm_module  # noqa: E402
from physical_ai_server.data_processing import record_texts_de  # noqa: E402
from physical_ai_server.data_processing.data_manager import DataManager  # noqa: E402
from physical_ai_server.data_processing.lerobot_dataset_wrapper import (  # noqa: E402
    LeRobotDatasetWrapper,
)

FPS = 30
PERIOD_S = 1.0 / FPS
H, W = 120, 160          # small frames keep the smoke test fast; same code path
JOINTS = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1']
EARLY_SAVE_MIN_S = dm_module.EARLY_SAVE_MIN_S
RERECORD_FINISH_WINDOW_S = dm_module.RERECORD_FINISH_WINDOW_S
ATTEMPTS = 3
FAILURES = []


def task(name, *, warmup, episode, reset, n, instructions=('Smoke test',)):
    t = TaskInfo()
    t.task_name = name
    t.task_type = 'record'
    t.user_id = 'smoke'
    t.task_instruction = list(instructions)
    t.fps = FPS
    t.tags = []
    t.warmup_time_s = warmup
    t.episode_time_s = episode
    t.reset_time_s = reset
    t.num_episodes = n
    t.push_to_hub = False
    t.private_mode = True
    t.use_optimized_save_mode = True
    t.record_rosbag2 = False
    return t


def frame(i):
    rng = np.random.default_rng(i)
    img = rng.integers(0, 255, (H, W, 3), dtype=np.uint8)
    return {'gripper': img, 'scene': img[::-1].copy()}


class Session:
    def __init__(self, root, name, dataset_kwargs=None, frame_fn=None, **timing):
        self.dm = DataManager(root, 'omx_f', task(name, **timing), upload_callback=None)
        self.dm._session_marker_enabled = True
        self.i = 0
        self.completed = False
        self.frame_fn = frame_fn or frame
        self.warnings = []
        if dataset_kwargs:
            # A dataset made through the wrapper with extra PUBLIC create()
            # arguments (the frame-drop scenario), installed as the node's
            # record tick would install its own.
            dataset = LeRobotDatasetWrapper.create(
                repo_id=self.dm._save_repo_name, fps=FPS,
                features=self.dm._dataset_features(self.frame_fn(0), JOINTS),
                root=self.dm._save_path, use_videos=True, **dataset_kwargs)
            dataset.set_robot_type('omx_f')
            self.dm._lerobot_dataset = dataset
        # DataManager builds its LeRobot dataset lazily, exactly like the node's
        # record tick does before calling record().
        assert self.dm.check_lerobot_dataset(self.frame_fn(0), JOINTS), 'dataset init failed'
        # Count the frames the writer really receives (recording path only),
        # and when: (tick number, monotonic time the feed started, returned).
        self.fed = 0
        self.frame_log = []
        self.tick_log = []        # (tick number, started, ended)
        dataset = self.dm._lerobot_dataset
        add_frame = dataset.add_frame_without_write_image

        def counted(*args, **kwargs):
            self.fed += 1
            started = time.monotonic()
            result = add_frame(*args, **kwargs)
            self.frame_log.append((self.i, started, time.monotonic()))
            return result

        dataset.add_frame_without_write_image = counted
        self._next_tick = time.monotonic()

    def tick(self, paced=True):
        self.i += 1
        started = time.monotonic()
        state = [0.01 * self.i] * len(JOINTS)
        r = self.dm.record(images=self.frame_fn(self.i), state=state, action=state)
        self.tick_log.append((self.i, started, time.monotonic()))
        warning = self.dm.get_current_record_status().error
        if warning:
            self.warnings.append(warning)
        if r == DataManager.RECORD_COMPLETED:
            self.completed = True
        if not paced:
            return r
        # Deadline pacing: target 30 Hz on a monotonic schedule.
        self._next_tick += PERIOD_S
        delay = self._next_tick - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        elif delay < -PERIOD_S:
            self._next_tick = time.monotonic()
        return r

    def run_for(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.completed:
            self.tick()

    def run_while(self, condition, limit_s=15.0):
        end = time.monotonic() + limit_s
        while condition() and not self.completed and time.monotonic() < end:
            self.tick()
        return not condition()

    def until(self, status, limit_s=15.0):
        return self.run_while(lambda: self.dm.get_status() != status, limit_s)

    def run_age(self):
        return self.dm._run_age_s()

    def finish(self):
        self.dm.record_finish()
        end = time.monotonic() + 15.0
        while not self.completed and time.monotonic() < end:
            self.tick()
        return self.completed

    def on_disk(self):
        root = Path(self.dm._lerobot_dataset.root)
        info = json.loads((root / 'meta' / 'info.json').read_text())
        vids = sorted(p.stat().st_size for p in (root / 'videos').rglob('*.mp4')) if (root / 'videos').exists() else []
        return {'episodes': int(info.get('total_episodes', 0)),
                'frames': int(info.get('total_frames', 0)), 'mp4_sizes': vids}


def check(name, cond, detail):
    print(f'[{"OK" if cond else "FAIL"}] {name}: {detail}', flush=True)
    if not cond:
        FAILURES.append(name)


def scenario(name, body):
    """Run body(attempt) -> (precondition_met, passed, detail); re-run on a fresh
    dataset while a stall broke the precondition, then report."""
    detail = ''
    for attempt in range(1, ATTEMPTS + 1):
        precondition_met, passed, detail = body(attempt)
        if precondition_met:
            check(name, passed, detail + (f' (attempt {attempt})' if attempt > 1 else ''))
            return
        print(f'[RETRY] {name}: precondition not met ({detail})', flush=True)
    check(name, False, f'precondition not met in {ATTEMPTS} attempts: {detail}')


def finish_in_warmup(attempt):
    # FINISH during the FIRST warm-up (HEAD raised ValueError from save_episode).
    s = Session(ROOT, f'smoke finish warmup {attempt}', warmup=5, episode=10, reset=2, n=3)
    s.run_for(0.3)
    if s.dm.get_status() != 'warmup':
        return False, False, f'status {s.dm.get_status()} before FINISH'
    ok = s.finish()
    d = s.on_disk()
    return True, (ok and s.dm._record_episode_count == 0 and s.fed == 0
                  and d['episodes'] == 0 and d['frames'] == 0), \
        f'completed={ok} count={s.dm._record_episode_count} fed={s.fed} disk={d}'


def finish_under_1s(attempt):
    # FINISH while a run with real frames in flight is younger than
    # EARLY_SAVE_MIN_S: the run is dropped, nothing is kept (Q3).
    s = Session(ROOT, f'smoke finish short {attempt}', warmup=0, episode=10, reset=2, n=3)
    s.until('run')
    s.run_while(lambda: s.fed < 5)
    age = s.run_age()
    if s.dm.get_status() != 'run' or s.fed < 5 or age >= EARLY_SAVE_MIN_S:
        return False, False, f'status={s.dm.get_status()} fed={s.fed} run_age={age:.3f}s'
    fed = s.fed
    ok = s.finish()
    d = s.on_disk()
    return True, (ok and s.dm._record_episode_count == 0 and d['episodes'] == 0
                  and d['frames'] == 0), \
        f'completed={ok} fed_in_dropped_run={fed} run_age={age:.3f}s ' \
        f'count={s.dm._record_episode_count} disk={d}'


def skip_then_keep(attempt):
    # MOVE_TO_NEXT in the warm-up starts the run; a run of 45 fed frames
    # (1.5 s at 30 fps) is kept by FINISH, every fed frame on disk.
    s = Session(ROOT, f'smoke skip {attempt}', warmup=5, episode=10, reset=2, n=3)
    s.run_for(0.3)
    if s.dm.get_status() != 'warmup':
        return False, False, f'status {s.dm.get_status()} before MOVE_TO_NEXT'
    outcome = s.dm.record_early_save()
    s.run_while(lambda: s.fed < 45)
    age = s.run_age()
    if s.dm.get_status() != 'run' or s.fed < 45 or age < EARLY_SAVE_MIN_S:
        return False, False, f'status={s.dm.get_status()} fed={s.fed} run_age={age:.3f}s'
    fed = s.fed
    ok = s.finish()
    d = s.on_disk()
    return True, (outcome == 'run' and ok and s.dm._record_episode_count == 1
                  and d['episodes'] == 1 and d['frames'] == fed
                  and len(d['mp4_sizes']) == 2 and min(d['mp4_sizes']) > 0), \
        f'outcome={outcome} completed={ok} fed={fed} run_age={age:.3f}s ' \
        f'count={s.dm._record_episode_count} disk={d}'


def discard_then_finish(attempt):
    # „Verwerfen und beenden“ with Zurücksetzen = 0 s and a SLOW link: RERECORD,
    # a new run that is already OLDER than EARLY_SAVE_MIN_S (so Q3 alone would
    # keep it), then FINISH inside the RERECORD window — the run that started
    # after the RERECORD must not be kept (Q4, reviewer probe E).
    s = Session(ROOT, f'smoke discard finish {attempt}', warmup=0, episode=10, reset=0, n=3)
    s.until('run')
    s.run_while(lambda: s.fed < 30)
    accepted = s.dm.rerecord_from_command()
    fed_before = s.fed
    s.run_while(lambda: s.dm.get_status() != 'run'
                or s.run_age() < EARLY_SAVE_MIN_S + 0.2)
    age = s.run_age()
    since_rerecord = time.perf_counter() - s.dm._wire_rerecord_at
    new_run_fed = s.fed - fed_before
    if (s.dm.get_status() != 'run' or age < EARLY_SAVE_MIN_S or new_run_fed < 1
            or since_rerecord >= RERECORD_FINISH_WINDOW_S
            or s.dm._run_entered_at < s.dm._wire_rerecord_at):
        return False, False, (f'status={s.dm.get_status()} run_age={age:.3f}s '
                              f'since_rerecord={since_rerecord:.3f}s new_run_fed={new_run_fed}')
    ok = s.finish()
    d = s.on_disk()
    return True, (accepted and ok and s.dm._record_episode_count == 0
                  and d['episodes'] == 0 and d['frames'] == 0), \
        f'accepted={accepted} completed={ok} run_age={age:.3f}s ' \
        f'since_rerecord={since_rerecord:.3f}s new_run_fed={new_run_fed} ' \
        f'count={s.dm._record_episode_count} disk={d}'


def take_start_timing(s, discard_index):
    """How the take that starts after a discard begins, from the logs.

    LeRobot's encoder threads notice a cancel only after their 1 s queue
    timeout. The discarded take's encoder must be cancelled in the reset /
    discard tick (no frame), not lazily inside the new take's first frame, where
    it left a ~0.9 s real-time hole the dataset's index/fps timestamps do not
    know about. Returns a dict of seconds:
      first_fed_delay  new take's first frame fed, after the previous tick ENDED
      frame0_tick      the tick that fed that first frame
      gap01            real-time gap between the take's frames 0 and 1
      later_gap        largest gap between consecutive later frames
      later_tick       slowest tick that fed a later frame
      cancel_tick      slowest tick between the discard and the first frame
                       (where the cancel is expected to land; informational)
    The dataset says 1/30 s for every gap.
    """
    new_take = s.frame_log[discard_index:]
    ticks = {n: (start, end) for n, start, end in s.tick_log}

    def duration(n):
        return ticks[n][1] - ticks[n][0] if n in ticks else 0.0

    first_tick, _, first_fed = new_take[0]
    starts = [started for _, started, _ in new_take]
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    later_ticks = {n for n, _, _ in new_take[1:]} - {first_tick}
    old_last_tick = s.frame_log[discard_index - 1][0]
    between = [duration(n) for n in range(old_last_tick + 1, first_tick)]
    return {
        'first_fed_delay': first_fed - ticks[first_tick - 1][1],
        'frame0_tick': duration(first_tick),
        'gap01': gaps[0] if gaps else 0.0,
        'later_gap': max(gaps[1:], default=0.0),
        'later_tick': max((duration(n) for n in later_ticks), default=0.0),
        'cancel_tick': max(between, default=0.0),
    }


# A take start with a hole AT FRAME 0 is the defect this scenario exists for
# and always fails. A too-slow frame LATER in the take is the runner stalling
# (nothing is cancelled there); it earns ONE re-run of the scenario.
TIMING_LIMIT_S = 0.2
STALL_RETRIES_LEFT = {'redo_then_keep': 1}


def _frame0_ok(t):
    return (t['first_fed_delay'] < TIMING_LIMIT_S and t['frame0_tick'] < TIMING_LIMIT_S
            and t['gap01'] < TIMING_LIMIT_S)


def _later_ok(t):
    return t['later_gap'] < TIMING_LIMIT_S and t['later_tick'] < TIMING_LIMIT_S


def _timing_text(label, t):
    return f'{label}: ' + ' '.join(f'{k}={v:.3f}s' for k, v in t.items())


def redo_then_keep(attempt):
    # Two re-records with Zurücksetzen = 0 s, each followed by a take that is
    # kept (MOVE_TO_NEXT saves it): a wire RERECORD („Wiederholen“) in the FIRST
    # episode of a new dataset, and the collision-path re_record() in the
    # second. On disk: exactly the frames fed after each discard. (A fresh
    # LeRobotDataset is falsy — len() counts saved frames — and a truthiness
    # test once kept the discarded frames in the writer's buffer.) And each new
    # take starts without a real-time hole (see take_start_timing).
    s = Session(ROOT, f'smoke redo keep {attempt}', warmup=0, episode=10, reset=0, n=3)
    s.until('run')
    s.run_while(lambda: s.fed < 30)
    old_take = [started for _, started, _ in s.frame_log]
    baseline_gap = max(b - a for a, b in zip(old_take, old_take[1:]))
    if baseline_gap >= 0.2:
        return False, False, f'runner too slow: baseline frame gap {baseline_gap:.3f}s'
    kept = []
    timings = []
    accepted = None
    outcomes = []
    for episode in (1, 2):
        discard_index = s.fed
        if episode == 1:
            accepted = s.dm.rerecord_from_command()
        else:
            s.dm.re_record()
        s.run_while(lambda: s.dm.get_status() != 'run' or s.fed - discard_index < 45)
        age = s.run_age()
        if s.dm.get_status() != 'run' or age < EARLY_SAVE_MIN_S:
            return False, False, f'episode {episode}: status={s.dm.get_status()} run_age={age:.3f}s'
        kept.append(s.fed - discard_index)
        timings.append(take_start_timing(s, discard_index))
        outcomes.append(s.dm.record_early_save())
        s.run_while(lambda: s.dm.get_status() != 'reset', limit_s=10.0)
        if episode == 1:
            s.run_while(lambda: s.dm.get_status() != 'run')
            before = s.fed
            s.run_while(lambda: s.fed - before < 30)          # the take the collision discards
    ok = s.finish()
    d = s.on_disk()
    timing_text = ' | '.join(_timing_text(f'redo{n}', t) for n, t in enumerate(timings, 1))
    frame0_ok = all(_frame0_ok(t) for t in timings)
    later_ok = all(_later_ok(t) for t in timings)
    if frame0_ok and not later_ok and STALL_RETRIES_LEFT['redo_then_keep'] > 0:
        STALL_RETRIES_LEFT['redo_then_keep'] -= 1
        return False, False, f'runner stall after frame 0 (one re-run): {timing_text}'
    passed = (accepted and outcomes == ['save', 'save'] and ok
              and s.dm._record_episode_count == 2 and d['episodes'] == 2
              and d['frames'] == sum(kept)
              and len(d['mp4_sizes']) == 2 and min(d['mp4_sizes']) > 0
              and frame0_ok and later_ok)
    return True, passed, (
        f'accepted={accepted} outcomes={outcomes} kept={kept} completed={ok} '
        f'count={s.dm._record_episode_count} disk={d} baseline_gap={baseline_gap:.3f}s | '
        + timing_text)


def normal_episode(attempt):
    # One ordinary episode to completion: finalize writes a readable dataset
    # holding every fed frame.
    s = Session(ROOT, f'smoke normal {attempt}', warmup=0, episode=2, reset=0, n=1)
    end = time.monotonic() + 20
    while not s.completed and time.monotonic() < end:
        s.tick()
    d = s.on_disk()
    return True, (s.completed and s.dm._record_episode_count == 1 and d['episodes'] == 1
                  and s.fed >= 10 and d['frames'] == s.fed
                  and len(d['mp4_sizes']) == 2 and min(d['mp4_sizes']) > 0), \
        f'completed={s.completed} count={s.dm._record_episode_count} fed={s.fed} disk={d}'


# ── round 5 ──────────────────────────────────────────────────────────────────

def _state_columns(dm):
    """observation.state rows of every saved frame, in dataset order."""
    import pyarrow.parquet as pq
    root = Path(dm._lerobot_dataset.root)
    rows = []
    for path in sorted(glob.glob(str(root / 'data' / '*' / '*.parquet'))):
        table = pq.read_table(path, columns=['observation.state', 'frame_index', 'episode_index'])
        rows += [list(map(float, v)) for v in table.column('observation.state').to_pylist()]
    return rows


def _camera_events(hz, dur, *, phase, jit, rnd, offset):
    """(arrival, stamp, counter) of a free-running camera with its own clock."""
    out, prev, i = [], -1.0, 0
    while True:
        nominal = phase + i / hz
        if nominal > dur:
            return out
        capture = nominal + max(-0.4 / hz, min(0.4 / hz, rnd.gauss(0.0, jit)))
        arrival = max(capture + 0.015 + rnd.expovariate(1 / 0.001), prev + 1e-4)
        prev = arrival
        out.append((arrival, capture + offset, i))
        i += 1


def _joint_events(dur, *, rnd, stamped):
    out, phase, i = [], rnd.random() / 100.0, 0
    while True:
        capture = phase + i / 100.0
        if capture > dur:
            return out
        out.append((capture + 0.001 + rnd.expovariate(1 / 0.0005),
                    capture if stamped else 0.0, i))
        i += 1


def _stalled(events, a, b):
    """A process stall [a, b): every callback inside it runs at b, in order."""
    out, n = [], 0
    for arrival, stamp, counter in events:
        if a <= arrival < b:
            arrival, n = b + 0.00002 * n, n + 1
        out.append((arrival, stamp, counter))
    return sorted(out)


def slot_sampler_take(attempt):
    # A 10 s take (300 slots) built exactly like the node's record tick builds
    # it: the slot sampler decides every frame from timestamped histories, and
    # each decided frame goes through DataManager.record() into the REAL
    # LeRobot writer. Two free-running cameras off the nominal rate (29.97 and
    # 30.02 Hz, 3 ms jitter, their own clock domains) and a 300 ms process
    # stall that bunches every callback. Each camera's frame counter is burnt
    # into observation.state, so the dataset itself proves which frame each
    # slot used: the take has exactly 300 frames, no slot is lost, and a
    # counter repeats or skips only for the cameras' honest rate mismatch.
    rnd = random.Random(1000 + attempt)
    take_start, take_s, sim_end = 1.0, 10.0, 12.5
    stall = (6.0, 6.3)
    cams = {'gripper': _stalled(_camera_events(29.97, sim_end, phase=0.004, jit=0.003,
                                               rnd=rnd, offset=3.217), *stall),
            'scene': _stalled(_camera_events(30.02, sim_end, phase=0.019, jit=0.003,
                                             rnd=rnd, offset=-41.5), *stall)}
    follower = _stalled(_joint_events(sim_end, rnd=rnd, stamped=True), *stall)
    leader = _stalled(_joint_events(sim_end, rnd=rnd, stamped=False), *stall)
    events = sorted(
        [(a, name, s, c) for name, evs in cams.items() for a, s, c in evs]
        + [(a, 'follower', s, c) for a, s, c in follower]
        + [(a, 'leader', 0.0, c) for a, _s, c in leader])
    histories = {name: capture_timeline.SourceHistory(capture_timeline.CAMERA_HISTORY_LEN)
                 for name in cams}
    histories['follower'] = capture_timeline.SourceHistory(capture_timeline.JOINT_HISTORY_LEN)
    histories['leader'] = capture_timeline.SourceHistory(capture_timeline.JOINT_HISTORY_LEN)
    sampler = capture_timeline.SlotSampler(FPS, list(cams))
    integrity = capture_timeline.TakeIntegrity(list(cams), FPS)
    integrity.reset(start_mono=take_start)

    s = Session(ROOT, f'smoke slot take {attempt}', warmup=0, episode=take_s, reset=0, n=1)
    s.dm.record(images=None, state=None, action=None)            # warm-up 0 -> run
    if s.dm.get_status() != 'run':
        return False, False, f'status {s.dm.get_status()} after the warm-up'
    pool = [frame(i) for i in range(4)]
    recorded, ei, k, stall_ticked = 0, 0, 0, False
    while s.dm.get_status() == 'run':
        k += 1
        now = k * PERIOD_S + abs(rnd.gauss(0.0, 0.002))
        if stall[0] <= now < stall[1]:
            if stall_ticked:
                continue                       # the timer skips the periods it missed
            stall_ticked, now = True, stall[1] + 0.002
        if now > sim_end:
            return True, False, f'the take did not end by {sim_end} s ({recorded} frames)'
        while ei < len(events) and events[ei][0] <= now:
            arrival, name, stamp, counter = events[ei]
            histories[name].append(arrival, stamp, counter)
            ei += 1
        decisions = sampler.decide_due(
            now, {name: histories[name].snapshot() for name in cams},
            histories['follower'].snapshot(), histories['leader'].snapshot())
        for d in decisions:
            if d['g'] < take_start or s.dm.get_status() != 'run':
                continue
            counters = [float(d['cams']['gripper']['item'][3]),
                        float(d['cams']['scene']['item'][3])]
            state = counters + [float(d['fol'][3]), float(d['lea'][3]), 0.0, 0.0]
            s.dm.record(images=pool[recorded % len(pool)], state=state, action=state)
            if s.dm.added_frame():
                recorded += 1
                integrity.add(d)
    ok = s.finish()
    d = s.on_disk()
    rows = _state_columns(s.dm)
    steps = {name: [int(b[i] - a[i]) for a, b in zip(rows, rows[1:])]
             for i, name in enumerate(('gripper', 'scene'))}
    reps = {name: v.count(0) for name, v in steps.items()}
    skips = {name: sum(1 for x in v if x == 2) for name, v in steps.items()}
    odd = {name: sorted(set(v) - {0, 1, 2}) for name, v in steps.items()}
    passed = (ok and recorded == 300 and d['frames'] == 300 and d['episodes'] == 1
              and integrity.lost == 0 and not any(odd.values())
              and reps['gripper'] <= 1 and skips['gripper'] == 0
              and skips['scene'] <= 1 and reps['scene'] == 0)
    return True, passed, (f'completed={ok} recorded={recorded} disk={d} lost={integrity.lost} '
                          f'repeats={reps} skips={skips} odd_steps={odd} '
                          f'warning={integrity.warning_de(1)!r}')


DROP_H, DROP_W = 1080, 1920


def _drop_pool():
    rng = np.random.default_rng(7)
    pool = [rng.integers(0, 255, (DROP_H, DROP_W, 3), dtype=np.uint8) for _ in range(6)]
    return lambda i: {'gripper': pool[i % len(pool)], 'scene': pool[(i + 3) % len(pool)]}


def frame_drop_rerecord(attempt):
    # The encoder made slower than the producer through the PUBLIC
    # create(encoder_queue_maxsize=1), hevc, full-HD noise fed back to back: it
    # drops frames. The drop is detected through LeRobot's public "Encoder queue
    # full" warning (the wrapper's frame-drop watch), never a private counter;
    # the take is re-recorded twice (FRAME_LOSS_REDO_DE), and the third loss of
    # the same episode ends the session like „Beenden“ (C7): nothing saved,
    # the dataset finalized.
    s = Session(ROOT, f'smoke frame drop {attempt}', warmup=0, episode=3, reset=0, n=1,
                dataset_kwargs={'vcodec': 'hevc', 'encoder_queue_maxsize': 1},
                frame_fn=_drop_pool())
    end = time.monotonic() + 240.0
    while not s.completed and time.monotonic() < end:
        s.tick(paced=False)
    redo = record_texts_de.frame_loss_redo_de(1)
    final = record_texts_de.frame_loss_end_de(1)
    redos = sum(1 for w in s.warnings if w == f'[WARNUNG] {redo}')
    ends = sum(1 for w in s.warnings if w == f'[WARNUNG] {final}')
    d = s.on_disk()
    if redos == 0 and s.dm._record_episode_count == 1:
        return False, False, f'no frame drop on this runner (disk={d})'
    passed = (s.completed and redos == 2 and ends == 1 and s.dm._record_episode_count == 0
              and d['episodes'] == 0 and d['frames'] == 0)
    return True, passed, (f'completed={s.completed} redos={redos} ends={ends} '
                          f'count={s.dm._record_episode_count} disk={d} '
                          f'warnings={[w[:60] for w in s.warnings]}')


def multitask_next_first_warmup(attempt):
    # An old client's multi-task MOVE_TO_NEXT in the FIRST warm-up latches a
    # save that commits nothing; it must not count (HEAD: robot 2 / disk 1).
    s = Session(ROOT, f'smoke multitask {attempt}', warmup=5, episode=10, reset=2, n=3,
                instructions=('Aufgabe A', 'Aufgabe B'))
    s.run_for(0.2)
    if s.dm.get_status() != 'warmup':
        return False, False, f'status {s.dm.get_status()} before MOVE_TO_NEXT'
    s.dm.record_next_episode()
    s.run_while(lambda: s.dm.get_status() != 'run', limit_s=5.0)
    count_after_empty = s.dm._record_episode_count
    s.run_while(lambda: s.fed < 30)
    s.dm.record_next_episode()                      # a real take, saved
    s.run_while(lambda: s.dm.get_status() != 'run', limit_s=5.0)
    ok = s.finish()
    d = s.on_disk()
    passed = (ok and count_after_empty == 0 and s.dm._record_episode_count == d['episodes'] == 1
              and d['frames'] == 30)
    return True, passed, (f'completed={ok} count_after_empty_save={count_after_empty} '
                          f'count={s.dm._record_episode_count} disk={d}')


def source_gap_redo(attempt):
    # O2: a take whose source was silent >= SOURCE_GAP_S (the node's capture
    # integrity notes it) is re-recorded — twice; the third gapped take of the
    # same episode is kept with GAP_KEPT_DE. On disk: exactly the third take.
    s = Session(ROOT, f'smoke source gap {attempt}', warmup=0, episode=1.5, reset=0, n=1)
    takes = []
    for _ in range(3):
        if not s.until('run'):
            return False, False, f'status {s.dm.get_status()} waiting for a take'
        start = s.fed
        s.run_while(lambda: s.fed - start < 10)
        s.dm.note_take_gap(('leader', None))
        s.run_while(lambda: s.dm.get_status() == 'run')
        takes.append(s.fed - start)
        s.run_while(lambda: s.dm.get_status() not in ('run',) and not s.completed,
                    limit_s=5.0)
    s.run_while(lambda: not s.completed, limit_s=10.0)
    d = s.on_disk()
    gap = f'[WARNUNG] {record_texts_de.source_gap_de("leader", None, 1)}'
    kept = f'[WARNUNG] {record_texts_de.source_gap_kept_de("leader", None, 1)}'
    passed = (s.completed and takes == [45, 45, 45] and s.warnings.count(gap) == 2
              and s.warnings.count(kept) == 1 and s.dm._record_episode_count == 1
              and d['episodes'] == 1 and d['frames'] == 45)
    return True, passed, (f'completed={s.completed} takes={takes} count='
                          f'{s.dm._record_episode_count} disk={d} '
                          f'warnings={[w[:50] for w in s.warnings]}')


OFFICIAL_DISCARD_LIMIT_S = 1.5


def official_discard_timing(attempt):
    # O6: a discard is LeRobot's public clear_episode_buffer() (it waits for
    # the encoder threads' 1 s queue timeout). It runs in the record step after
    # the discard — no frame there — and must finish well inside the limit.
    s = Session(ROOT, f'smoke discard timing {attempt}', warmup=0, episode=10, reset=5, n=3)
    s.until('run')
    s.run_while(lambda: s.fed < 30)
    s.dm.rerecord_from_command()
    fed = s.fed
    s.tick()
    _n, started, ended = s.tick_log[-1]
    took = ended - started
    discarded = not s.dm._discard_pending and s.fed == fed
    return True, discarded and took < OFFICIAL_DISCARD_LIMIT_S, (
        f'discard step took {took:.3f}s (limit {OFFICIAL_DISCARD_LIMIT_S}s) '
        f'no_frame_in_it={s.fed == fed}')


def main():
    # LeRobot's encoder warnings must reach the frame-drop watch (WARNING).
    logging.getLogger('lerobot.datasets.video_utils').setLevel(logging.WARNING)
    scenario('finish_in_warmup', finish_in_warmup)
    scenario('finish_under_1s', finish_under_1s)
    scenario('skip_then_keep', skip_then_keep)
    scenario('discard_then_finish', discard_then_finish)
    scenario('redo_then_keep', redo_then_keep)
    scenario('normal_episode', normal_episode)
    scenario('slot_sampler_take', slot_sampler_take)
    scenario('multitask_next_first_warmup', multitask_next_first_warmup)
    scenario('source_gap_redo', source_gap_redo)
    scenario('official_discard_timing', official_discard_timing)
    scenario('frame_drop_rerecord', frame_drop_rerecord)
    print('SMOKE RESULT:', 'PASS' if not FAILURES else f'FAIL {FAILURES}', flush=True)
    return 0 if not FAILURES else 1


if __name__ == '__main__':
    sys.exit(main())

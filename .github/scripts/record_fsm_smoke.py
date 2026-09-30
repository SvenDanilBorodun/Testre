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
„Wiederholen“ in the first episode of a new dataset, one normal episode.
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
import json
import os
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
from physical_ai_server.data_processing import data_manager as dm_module  # noqa: E402
from physical_ai_server.data_processing.data_manager import DataManager  # noqa: E402

FPS = 30
PERIOD_S = 1.0 / FPS
H, W = 120, 160          # small frames keep the smoke test fast; same code path
JOINTS = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1']
EARLY_SAVE_MIN_S = dm_module.EARLY_SAVE_MIN_S
RERECORD_FINISH_WINDOW_S = dm_module.RERECORD_FINISH_WINDOW_S
ATTEMPTS = 3
FAILURES = []


def task(name, *, warmup, episode, reset, n):
    t = TaskInfo()
    t.task_name = name
    t.task_type = 'record'
    t.user_id = 'smoke'
    t.task_instruction = ['Smoke test']
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
    def __init__(self, root, name, **timing):
        self.dm = DataManager(root, 'omx_f', task(name, **timing), upload_callback=None)
        self.dm._session_marker_enabled = True
        self.i = 0
        self.completed = False
        # DataManager builds its LeRobot dataset lazily, exactly like the node's
        # record tick does before calling record().
        assert self.dm.check_lerobot_dataset(frame(0), JOINTS), 'dataset init failed'
        # Count the frames the writer really receives (recording path only).
        self.fed = 0
        dataset = self.dm._lerobot_dataset
        add_frame = dataset.add_frame_without_write_image

        def counted(*args, **kwargs):
            self.fed += 1
            return add_frame(*args, **kwargs)

        dataset.add_frame_without_write_image = counted
        self._next_tick = time.monotonic()

    def tick(self):
        self.i += 1
        state = [0.01 * self.i] * len(JOINTS)
        r = self.dm.record(images=frame(self.i), state=state, action=state)
        if r == DataManager.RECORD_COMPLETED:
            self.completed = True
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


def redo_then_keep(attempt):
    # „Wiederholen“ in the FIRST episode of a new dataset, then the redone run is
    # saved by MOVE_TO_NEXT: only the frames fed AFTER the RERECORD may be on
    # disk. (A fresh LeRobotDataset is falsy — len() counts saved frames — and a
    # truthiness test once kept the discarded frames in the writer's buffer.)
    s = Session(ROOT, f'smoke redo keep {attempt}', warmup=0, episode=10, reset=0, n=3)
    s.until('run')
    s.run_while(lambda: s.fed < 30)
    discarded = s.fed
    accepted = s.dm.rerecord_from_command()
    s.run_while(lambda: s.dm.get_status() != 'run' or s.fed - discarded < 45)
    age = s.run_age()
    if s.dm.get_status() != 'run' or age < EARLY_SAVE_MIN_S:
        return False, False, f'status={s.dm.get_status()} run_age={age:.3f}s'
    kept = s.fed - discarded
    outcome = s.dm.record_early_save()
    s.run_while(lambda: s.dm.get_status() != 'reset', limit_s=10.0)
    ok = s.finish()
    d = s.on_disk()
    return True, (accepted and outcome == 'save' and ok and s.dm._record_episode_count == 1
                  and d['episodes'] == 1 and d['frames'] == kept
                  and len(d['mp4_sizes']) == 2 and min(d['mp4_sizes']) > 0), \
        f'accepted={accepted} outcome={outcome} discarded={discarded} kept={kept} ' \
        f'completed={ok} count={s.dm._record_episode_count} disk={d}'


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


def main():
    scenario('finish_in_warmup', finish_in_warmup)
    scenario('finish_under_1s', finish_under_1s)
    scenario('skip_then_keep', skip_then_keep)
    scenario('discard_then_finish', discard_then_finish)
    scenario('redo_then_keep', redo_then_keep)
    scenario('normal_episode', normal_episode)
    print('SMOKE RESULT:', 'PASS' if not FAILURES else f'FAIL {FAILURES}', flush=True)
    return 0 if not FAILURES else 1


if __name__ == '__main__':
    sys.exit(main())

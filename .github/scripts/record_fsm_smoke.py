#!/usr/bin/env python3
"""Aufnahme 2.0 — record-FSM smoke test against the REAL LeRobot 0.5.1 writer.

Runs INSIDE the built physical-ai-server image (docker-publish.yml::smoke-test,
with /opt/ros/jazzy and /root/ros2_ws sourced), so everything below is the
shipped code: DataManager, LeRobotDatasetWrapper, LeRobot's DatasetWriter and
its streaming h264 encoder (PyAV). No network: push_to_hub is False.

Each scenario drives DataManager.record() in real time with synthetic frames
and asserts what ends up ON DISK, which is the one thing the stub-based unit
tests cannot see. Exit code 0 = all scenarios passed.
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
from physical_ai_server.data_processing.data_manager import DataManager  # noqa: E402

FPS = 30
H, W = 120, 160          # small frames keep the smoke test fast; same code path
JOINTS = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1']
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

    def tick(self):
        self.i += 1
        state = [0.01 * self.i] * len(JOINTS)
        r = self.dm.record(images=frame(self.i), state=state, action=state)
        if r == DataManager.RECORD_COMPLETED:
            self.completed = True
        time.sleep(1.0 / FPS)
        return r

    def run_for(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.completed:
            self.tick()

    def until(self, status, limit_s=15.0):
        end = time.monotonic() + limit_s
        while self.dm.get_status() != status and time.monotonic() < end:
            self.tick()
        return self.dm.get_status() == status

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


def main():
    root = ROOT

    # 1. FINISH during the FIRST warm-up (HEAD raised ValueError from save_episode).
    s = Session(root, 'smoke finish warmup', warmup=5, episode=10, reset=2, n=3)
    s.run_for(0.3)
    ok = s.finish()
    d = s.on_disk()
    check('finish_in_warmup', ok and s.dm._record_episode_count == 0 and d['episodes'] == 0,
          f'completed={ok} count={s.dm._record_episode_count} disk={d}')

    # 2. FINISH less than 1 s into a run: the stub is dropped, nothing kept.
    s = Session(root, 'smoke finish short', warmup=0, episode=10, reset=2, n=3)
    s.until('run')
    s.run_for(0.4)
    ok = s.finish()
    d = s.on_disk()
    check('finish_under_1s', ok and s.dm._record_episode_count == 0 and d['episodes'] == 0,
          f'completed={ok} count={s.dm._record_episode_count} disk={d}')

    # 3. MOVE_TO_NEXT in warm-up skips to run; a 1.5 s run kept by FINISH.
    s = Session(root, 'smoke skip', warmup=5, episode=10, reset=2, n=3)
    s.run_for(0.3)
    outcome = s.dm.record_early_save()
    s.run_for(1.5)
    ok = s.finish()
    d = s.on_disk()
    check('skip_then_keep', outcome == 'run' and ok and s.dm._record_episode_count == 1
          and d['episodes'] == 1 and d['frames'] >= 30 and len(d['mp4_sizes']) == 2
          and min(d['mp4_sizes']) > 0,
          f'outcome={outcome} completed={ok} count={s.dm._record_episode_count} disk={d}')

    # 4. „Verwerfen und beenden" with Zurücksetzen = 0 s and a SLOW link:
    #    RERECORD, then FINISH 2.2 s later — the run that started after the
    #    RERECORD must not be kept (reviewer probe E).
    s = Session(root, 'smoke discard finish', warmup=0, episode=10, reset=0, n=3)
    s.until('run')
    s.run_for(1.5)
    accepted = s.dm.rerecord_from_command()
    s.run_for(2.2)
    ok = s.finish()
    d = s.on_disk()
    check('discard_then_finish', accepted and ok and s.dm._record_episode_count == 0
          and d['episodes'] == 0,
          f'accepted={accepted} completed={ok} count={s.dm._record_episode_count} disk={d}')

    # 5. One ordinary episode to completion: finalize writes a readable dataset.
    s = Session(root, 'smoke normal', warmup=0, episode=2, reset=0, n=1)
    end = time.monotonic() + 20
    while not s.completed and time.monotonic() < end:
        s.tick()
    d = s.on_disk()
    check('normal_episode', s.completed and s.dm._record_episode_count == 1 and d['episodes'] == 1
          and len(d['mp4_sizes']) == 2,
          f'completed={s.completed} count={s.dm._record_episode_count} disk={d}')

    print('SMOKE RESULT:', 'PASS' if not FAILURES else f'FAIL {FAILURES}', flush=True)
    return 0 if not FAILURES else 1


if __name__ == '__main__':
    sys.exit(main())

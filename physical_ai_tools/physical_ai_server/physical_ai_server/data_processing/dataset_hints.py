#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""„Auffälligkeiten" — per-episode hints from the recorded data (spec §F6).

Hints only: they drive the amber line, the list flags, the scrubber bands and
the „Nur mit Hinweisen" filter, and never block anything. Computed in the Daten
sidecar (numpy only), cached there by the dataset's meta digest.

Inputs per episode: the follower state ``S`` and the leader action ``A``
(``n × J`` radians, joints in the dataset's own order), ``fps``, and — for
``short``/``long`` — the dataset's median episode length and episode count. The
columns come from the joint NAMES, never from a fixed slice: the arm is every
joint but the last, the gripper the last when it is named ``gripper*`` (else
there is no gripper and every joint is arm).

Thresholds calibrated on 489 real public OMX episodes (P12): leading idle p99
2.86 s, trailing idle p99 4.85 s; the smallest max joint range 38.9° (10° has a
3.9× margin); the smallest gripper range 26.5° (→ 8°); duration ratios
0.70–1.44 (→ 0.6/1.6). Idle is judged on net displacement over a window, not on
summed absolute steps (an encoder flicker of one tick on five joints at 30 fps
would read ~13°/s). The lag residual is corrected for a constant
leader/follower offset AND the best delay, because two real datasets carry a
constant ~16° offset that a raw error rule would flag on every episode.
"""

from __future__ import annotations

import numpy as np

ALGO = 'hints-v1'
# idle: leader speed below IDLE_DEG_S over a ±WIN_S window
IDLE_DEG_S = 2.0
WIN_S = 0.25
IDLE_START_S = 3.0
IDLE_END_S = 4.0
# still: the largest arm-joint range of the leader below STILL_DEG
STILL_DEG = 10.0
# no_grasp: the gripper range of the leader below GRIP_DEG
GRIP_DEG = 8.0
# lag: a sustained (LAG_WIN_S) follower-vs-leader residual of at least LAG_DEG
# after removing a constant offset and the best delay up to LAG_MAX_SHIFT_S
LAG_DEG = 20.0
LAG_WIN_S = 0.5
LAG_MAX_SHIFT_S = 0.6
# short/long: length / median outside [SHORT_RATIO, LONG_RATIO], with at least
# MIN_EPISODES episodes in the dataset
SHORT_RATIO = 0.6
LONG_RATIO = 1.6
MIN_EPISODES = 4


def split_columns(names):
    """``(arm column indices, gripper column index or None)`` from the joint names."""
    names = [str(n) for n in (names or [])]
    if names and names[-1].lower().startswith('gripper'):
        return list(range(len(names) - 1)), len(names) - 1
    return list(range(len(names))), None


def _r3(x):
    return round(float(x), 3)


def episode_hints(state, action, fps, names, median_len=None, n_episodes=0):
    """The hints of one episode: a list of dicts (contract.HINT_TYPES)."""
    S = np.asarray(state, dtype=np.float64)
    A = np.asarray(action, dtype=np.float64)
    n = int(min(len(S), len(A)))
    fps = float(fps)
    if n < 2 or fps <= 0 or S.ndim != 2 or A.ndim != 2:
        return []
    S, A = S[:n], A[:n]
    arm, grip = split_columns(names)
    arm = [k for k in arm if k < A.shape[1] and k < S.shape[1]]
    out = []
    if arm:
        Ad = np.degrees(A[:, arm])
        rng = float((Ad.max(axis=0) - Ad.min(axis=0)).max())
        if rng < STILL_DEG:
            out.append({'type': 'still', 'value_deg': round(rng, 1)})
        else:
            w = max(1, int(round(WIN_S * fps)))
            pad = np.pad(Ad, ((w, w), (0, 0)), mode='edge')
            v = np.abs(pad[2 * w:] - pad[:-2 * w]).max(axis=1) / (2 * w / fps)
            moving = np.nonzero(v >= IDLE_DEG_S)[0]
            b = int(moving[0]) if len(moving) else n
            e = int(moving[-1]) + 1 if len(moving) else 0
            if b / fps >= IDLE_START_S:
                out.append({'type': 'idle_start', 'from_s': 0.0, 'to_s': _r3(b / fps)})
            if (n - e) / fps >= IDLE_END_S:
                out.append({'type': 'idle_end', 'from_s': _r3(e / fps), 'to_s': _r3(n / fps)})
            if grip is not None and grip < A.shape[1]:
                g = np.degrees(A[:, grip])
                grange = float(g.max() - g.min())
                if grange < GRIP_DEG:
                    out.append({'type': 'no_grasp', 'value_deg': round(grange, 1)})
        lag = _lag(S[:, arm], A[:, arm], fps)
        if lag is not None:
            at_s, joint, value = lag
            out.append({'type': 'lag', 'at_s': _r3(at_s), 'joint': arm[joint], 'value_deg': int(round(value))})
    if median_len and n_episodes >= MIN_EPISODES:
        ratio = n / float(median_len)
        if ratio < SHORT_RATIO:
            out.append({'type': 'short', 'ratio': round(ratio, 2), 'median_s': _r3(median_len / fps)})
        if ratio > LONG_RATIO:
            out.append({'type': 'long', 'ratio': round(ratio, 2), 'median_s': _r3(median_len / fps)})
    return out


def _lag(S, A, fps):
    """``(at_s, arm joint index, value_deg)`` of a sustained lag, else None."""
    n = len(A)
    off = np.median(S - A, axis=0)
    best = (np.inf, 0)
    for sh in range(0, int(round(LAG_MAX_SHIFT_S * fps)) + 1):
        if n - sh < 2:
            break
        m = float(np.abs(S[sh:] - A[:n - sh] - off).mean())
        if m < best[0]:
            best = (m, sh)
    sh = best[1]
    r = np.degrees(np.abs(S[sh:] - A[:n - sh] - off))
    win = max(1, int(round(LAG_WIN_S * fps)))
    if len(r) < win:
        return None
    per_frame = r.max(axis=1)
    sustained = np.lib.stride_tricks.sliding_window_view(per_frame, win).min(axis=1)
    i = int(np.argmax(sustained))
    if sustained[i] < LAG_DEG:
        return None
    joint = int(np.argmax(r[i:i + win].min(axis=0)))
    return (i + sh) / fps, joint, float(sustained[i])


def dataset_hints(episodes, fps, names):
    """``episodes``: a list of ``(state, action)``; returns one hint list per
    episode (the median length and the count from the dataset itself)."""
    lengths = [int(min(len(s), len(a))) for s, a in episodes]
    median_len = float(np.median(lengths)) if lengths else 0.0
    return [episode_hints(s, a, fps, names, median_len, len(lengths)) for s, a in episodes]

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

"""Bench gate B4: Studio perception load on live 1080p station frames
(inside ``physical_ai_server``; dev only; run via ``container_run.py``).

Reproduces the plan's case with the server's REAL perception code: one
``workflow.perception.Perception`` instance (pupil_apriltags tag36h11 behind
its one ``threading.Lock``, exactly what the node shares between a program's
threads), fed live ``/station_scene`` frames decoded the way the node does
(``cv2.imdecode(…, IMREAD_COLOR)`` of the newest JPEG):

* a „Wenn … gesehen" hat thread polls like ``workflow_manager``: decode the
  newest frame, detect every tag, then sleep 0.2 s when none of the hat's tags
  is seen (``_wait_object_visible``) or 0.1 s when one is (the object is in
  view and the hat has already fired for it: ``_run_hat_handler``'s edge gate
  sleeps 0.1 s and polls again), repeat;
* a composite „Greife"'s yaw sampling, like ``perception_blocks.
  _multiframe_tag_yaw``: up to ``2 * _TAG_YAW_FRAMES`` (14) detects with the
  tag filter, ``_TAG_YAW_FRAME_INTERVAL_S`` (0.03 s) apart — read from the
  server package when it imports, else the mirrored defaults.

Per trial it reports the time the 14 detects take (pass ≤ 2 s) and the hat's
poll rate while they run: pass ≥ 2 polls/s judged on the WORST gap (no gap
above 0.5 s; the mean interval alone would hide a stall), plus a single-thread
1080p detect baseline (v6.3's informational p95 ≤ 200 ms).

Honest limits: this runs in its OWN process, beside the node. It contends with
the node for CPU (the real situation) but not for the node's own detector
lock; the hat-versus-grasp contention is modelled inside this one instance,
exactly as the node would see it. The motion, the claims, the yaw maths and
the reclaim are not run (they cost microseconds next to a 1080p detect). The
hat's FIRST sighting runs its body under the motion lock (not modelled: the
hat is taken to have fired already), and claimed tags are not filtered out
(no program ran, so none are claimed).
Run it with NO program running on the robot, or say which one ran.
Read-only. SIGINT ends it with a summary.
"""

import argparse
import os
import signal
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(1, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bench_common as B  # noqa: E402

# workflow_manager._wait_object_visible: ``time.sleep(0.2)`` after a look that
# saw none of the hat's tags.
HAT_POLL_SLEEP_S = 0.2
# workflow_manager._run_hat_handler's edge gate: seen, already fired →
# ``time.sleep(0.1)`` and poll again.
HAT_SEEN_SLEEP_S = 0.1
# object_catalog: the shipped „Würfel"'s tag ids (both profiles).
HAT_TAG_IDS_DEFAULT = (20, 21)
YAW_FRAMES_DEFAULT = 7
YAW_INTERVAL_DEFAULT_S = 0.03
TRIAL_FIELDS = ['trial', 'grasp_s', 'detects', 'grasp_tag_hits', 'hat_polls_in_window',
                'hat_polls_per_s_count', 'hat_polls_per_s_interval', 'hat_max_gap_s',
                'hat_seen_polls', 'pass_grasp', 'pass_hat']


def hat_sleep_s(seen):
    """The hat's sleep after one look: 0.1 s when one of its tags was seen
    (fired already; the edge gate), else 0.2 s."""
    return HAT_SEEN_SLEEP_S if seen else HAT_POLL_SLEEP_S


def yaw_constants():
    """(frames, interval_s, source) from the server package, else defaults."""
    try:
        from physical_ai_server.workflow.handlers import perception_blocks as pb  # noqa: PLC0415
        return int(pb._TAG_YAW_FRAMES), float(pb._TAG_YAW_FRAME_INTERVAL_S), 'server package'
    except Exception as e:  # noqa: BLE001
        return YAW_FRAMES_DEFAULT, YAW_INTERVAL_DEFAULT_S, f'defaults ({type(e).__name__})'


def trial_numbers(t_start, t_end, poll_ends):
    """Pure: the hat's rate during ``[t_start, t_end]`` from its poll end times.

    * count: polls that ended inside the window / window length;
    * interval: 1 / mean of the poll intervals that overlap the window (the
      sustained rate; less quantised on a ~1 s window);
    * max gap: the longest interval overlapping the window (≥ 2 polls/s means
      every gap ≤ 0.5 s).
    """
    win = max(1e-9, t_end - t_start)
    inside = [t for t in poll_ends if t_start <= t <= t_end]
    ts = sorted(poll_ends)
    overl = [(a, b) for a, b in zip(ts, ts[1:]) if b >= t_start and a <= t_end]
    ivals = [b - a for a, b in overl]
    gaps = list(ivals)
    if not ts:
        gaps.append(win)                      # the hat never finished a poll
    elif ts[-1] < t_end:                      # a poll still running at the end
        gaps.append(t_end - max(ts[-1], t_start))
    rate = (len(ivals) / sum(ivals)) if ivals and sum(ivals) > 0 else None
    worst = max(gaps) if gaps else None
    return {
        'hat_polls_in_window': len(inside),
        'hat_polls_per_s_count': len(inside) / win,
        'hat_polls_per_s_interval': rate,
        'hat_max_gap_s': worst,
        # >= 2 polls/s means every gap <= 0.5 s: the worst gap decides.
        'pass_hat': (rate is not None and rate >= B.B4_HAT_MIN_POLLS_PER_S
                     and worst is not None and worst <= B.B4_HAT_MAX_GAP_S),
    }


def build_parser():
    p = argparse.ArgumentParser(description='B4: hat poll rate during a grasp\'s yaw sampling, '
                                            'real perception on live 1080p station frames.')
    p.add_argument('--out', required=True)
    p.add_argument('--trials', type=int, default=10)
    p.add_argument('--tag-id', type=int, default=20, help='the grasp\'s tag filter')
    p.add_argument('--hat-tag-id', type=int, action='append', default=None,
                   help='a tag of the hat\'s object type (repeatable; default the '
                        '„Würfel" ids 20, 21): seen → 0.1 s sleep, else 0.2 s')
    p.add_argument('--baseline-frames', type=int, default=20)
    p.add_argument('--wait-s', type=float, default=15.0, help='wait for the first frame')
    p.add_argument('--topic', default=B.STATION_TOPIC)
    p.add_argument('--label', default='')
    p.add_argument('--stamp', default=None, help='file-name stamp (container_run passes the '
                                                 'Windows one)')
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    import rclpy  # noqa: PLC0415
    from rclpy.signals import SignalHandlerOptions  # noqa: PLC0415
    from rclpy.executors import SingleThreadedExecutor  # noqa: PLC0415
    from sensor_msgs.msg import CompressedImage  # noqa: PLC0415

    from physical_ai_server.workflow.perception import Perception  # noqa: PLC0415

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    frames, interval_s, source = yaw_constants()
    attempts = 2 * frames

    latest = {'data': None, 'n': 0}
    lock = threading.Lock()
    # Our own SIGINT handler (set above) ends the loop and writes the summary;
    # rclpy's would shut the context down under the running loop instead.
    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    node = rclpy.create_node('station_bench_perception')

    def on_frame(msg):
        with lock:
            latest['data'] = bytes(msg.data)
            latest['n'] += 1

    # The node's own station-store subscription (BEST_EFFORT, depth 2): the
    # frames the node's Perception would see.
    node.create_subscription(CompressedImage, args.topic, on_frame,
                             B.rclpy_qos(B.image_qos(args.topic)))
    ex = SingleThreadedExecutor()
    ex.add_node(node)

    def spin():
        while not stop.is_set():
            ex.spin_once(timeout_sec=0.1)

    threading.Thread(target=spin, daemon=True).start()

    def decode():
        with lock:
            data = latest['data']
        if data is None:
            return None
        return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)

    t_wait = time.monotonic() + args.wait_s
    while decode() is None and time.monotonic() < t_wait and not stop.is_set():
        time.sleep(0.1)
    first = decode()
    if first is None:
        print(f'no frame on {args.topic} within {args.wait_s:.0f} s — is a station session '
              f'active (the page turns the Studio stream on)?', flush=True)
        stop.set()
        ex.shutdown()
        rclpy.shutdown()
        return 3
    perception = Perception()
    if not perception.apriltag_available():
        print('pupil_apriltags did not load in this image', flush=True)
        stop.set()
        ex.shutdown()
        rclpy.shutdown()
        return 3
    h, w = first.shape[:2]
    print(f'frames {w}x{h}; yaw sampling {attempts} detects {interval_s}s apart ({source})',
          flush=True)

    # 1) single-thread baseline: decode + detect on fresh frames.
    detect_s, decode_s = [], []
    for _ in range(args.baseline_frames):
        if stop.is_set():
            break
        t0 = time.perf_counter()
        img = decode()
        t1 = time.perf_counter()
        perception.detect(img, camera='scene', mode='apriltag', aruco_id=None)
        t2 = time.perf_counter()
        decode_s.append(t1 - t0)
        detect_s.append(t2 - t1)
        time.sleep(0.07)

    # 2) the hat thread: the 0.2 s / 0.1 s cadence by what it saw.
    hat_tags = set(args.hat_tag_id or HAT_TAG_IDS_DEFAULT)
    poll_ends = []
    poll_seen = []
    plock = threading.Lock()

    def hat():
        while not stop.is_set():
            img = decode()
            seen = False
            if img is not None:
                dets = perception.detect(img, camera='scene', mode='apriltag', aruco_id=None)
                seen = any(getattr(d, 'aruco_id', None) in hat_tags for d in (dets or []))
            with plock:
                poll_ends.append(time.monotonic())
                poll_seen.append(seen)
            time.sleep(hat_sleep_s(seen))

    ht = threading.Thread(target=hat, daemon=True)
    ht.start()
    time.sleep(3.0)
    with plock:
        idle = list(poll_ends)
    hat_idle_rate = B.rate_hz(idle)

    run_stamp = args.stamp or B.stamp()
    tlog = B.CsvLog(B.out_path(args.out, 'perception_load', run_stamp, '_trials.csv'),
                    TRIAL_FIELDS)
    trials = []
    for k in range(1, args.trials + 1):
        if stop.is_set():
            break
        time.sleep(1.0)
        t_start = time.monotonic()
        hits = 0
        n = 0
        for a in range(attempts):
            if a:
                time.sleep(interval_s)
            img = decode()
            dets = perception.detect(img, camera='scene', mode='apriltag', aruco_id=args.tag_id)
            n += 1
            hits += 1 if any(d.aruco_id == args.tag_id for d in dets) else 0
        t_end = time.monotonic()
        time.sleep(HAT_POLL_SLEEP_S + max(detect_s or [0.0]) + 0.1)   # let the hat's last poll end
        with plock:
            ends = list(poll_ends)
            seen_flags = list(poll_seen)
        rec = {'trial': k, 'grasp_s': t_end - t_start, 'detects': n, 'grasp_tag_hits': hits}
        rec.update(trial_numbers(t_start, t_end, ends))
        rec['hat_seen_polls'] = sum(1 for t, f in zip(ends, seen_flags)
                                    if t_start <= t <= t_end and f)
        rec['pass_grasp'] = rec['grasp_s'] <= B.B4_GRASP_YAW_MAX_S
        rate = rec['hat_polls_per_s_interval']
        tlog.write(rec)
        trials.append(rec)
        shown = None if rate is None else round(rate, 2)
        print(f"trial {k}: grasp {rec['grasp_s']:.2f} s, hat {shown}"
              f" polls/s (max gap {rec['hat_max_gap_s']}), tag {args.tag_id} seen {hits}/{n}",
              flush=True)
    stop.set()
    ht.join(timeout=5.0)
    tlog.close()
    ex.shutdown()
    node.destroy_node()
    rclpy.shutdown()
    summary = {
        'gate': 'B4 (hat >= 2 polls/s during the grasp; yaw sampling <= 2 s)',
        'label': args.label, 'frame_size': [w, h], 'yaw_attempts': attempts,
        'yaw_interval_s': interval_s, 'yaw_constants_from': source,
        'detect_1080p_s': B.summarize(detect_s), 'decode_1080p_s': B.summarize(decode_s),
        'hat_rate_idle_per_s': hat_idle_rate,
        'grasp_s': B.summarize([t['grasp_s'] for t in trials]),
        'hat_polls_per_s_during_grasp': B.summarize(
            [t['hat_polls_per_s_interval'] for t in trials
             if t['hat_polls_per_s_interval'] is not None]),
        'trials': len(trials),
        'verdicts': {
            'B4 yaw sampling <= 2 s (every trial)': (
                'UNKNOWN' if not trials else
                'PASS' if all(t['pass_grasp'] for t in trials) else 'FAIL'),
            'B4 hat >= 2 polls/s during the grasp (every trial)': (
                'UNKNOWN' if not trials else
                'PASS' if all(t['pass_hat'] for t in trials) else 'FAIL'),
            'v6.3 1080p detect p95 <= 200 ms (informational)': B.verdict(
                B.summarize(detect_s)['p95'], B.B4_DETECT_P95_S, at_least=False),
        },
        'hat_tag_ids': sorted(hat_tags),
        'hat_cadence_s': {'unseen': HAT_POLL_SLEEP_S, 'seen_and_fired': HAT_SEEN_SLEEP_S},
        'notes': ['Separate process beside the node: CPU contention is real, the node\'s own '
                  'detector lock is not shared. Lens-solve time (B4\'s first half) runs inside '
                  'the commission service call and is timed from the wizard (README).',
                  'The hat sleeps 0.2 s after a look without its tags and 0.1 s after one '
                  'with them (taken to have fired already). Its first firing (the body under '
                  'the motion lock) and claims are not modelled.',
                  'pass_hat judges the worst gap (<= 0.5 s) as well as the mean interval.'],
    }
    jp = B.out_path(args.out, 'perception_load', run_stamp, '_summary.json')
    B.write_json(jp, summary)
    print(f'wrote {jp}', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())

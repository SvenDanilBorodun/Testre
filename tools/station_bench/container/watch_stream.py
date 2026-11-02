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

"""In-container watcher for the B1 run logger (dev only; runs inside
``physical_ai_server``, started by ``run_logger.py``).

Prints one JSON object per line on stdout:

* ``{"kind": "signal", ...}`` for every ``/edubotics/signal_status`` message
  (latched + 1 Hz; subscribed TRANSIENT_LOCAL so the first line comes at once);
* ``{"kind": "session", ...}`` once per ``--session-every`` seconds: the
  read-only ``/station/session`` ``status`` answer (state, generation, …);
* ``{"kind": "arrival", "topic": …}`` once per second per ``--topic``: the
  frames that arrived in that second and the largest gap between two of them,
  measured on THIS container's clock (a recording-stream gap shows here).

Read-only: it never calls ``enter``/``leave``, never commands motion and never
switches torque. SIGINT ends it cleanly.
"""

import argparse
import json
import os
import signal
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(1, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _emit(obj):
    sys.stdout.write(json.dumps(obj, separators=(',', ':')) + '\n')
    sys.stdout.flush()


def build_parser():
    p = argparse.ArgumentParser(description='Station bench watcher (B1): signal_status, '
                                            'session status and per-second arrival gaps.')
    p.add_argument('--session-every', type=float, default=1.0,
                   help='seconds between /station/session status calls (0 = off)')
    p.add_argument('--topic', action='append', default=[],
                   help='image topic to watch for arrival gaps (repeatable)')
    p.add_argument('--duration', type=float, default=0.0, help='seconds (0 = until SIGINT)')
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    import rclpy  # noqa: PLC0415 — only in the container
    from rclpy.signals import SignalHandlerOptions  # noqa: PLC0415
    from rclpy.qos import (DurabilityPolicy, HistoryPolicy, QoSProfile,  # noqa: PLC0415
                           ReliabilityPolicy)
    from sensor_msgs.msg import CompressedImage  # noqa: PLC0415
    from std_msgs.msg import String  # noqa: PLC0415

    import bench_common as B  # noqa: PLC0415

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    # Our own SIGINT handler (set above) ends the loop and writes the summary;
    # rclpy's would shut the context down under the running loop instead.
    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    node = rclpy.create_node('station_bench_watch')

    # signal_status is published latched (TRANSIENT_LOCAL) and at 1 Hz.
    latched = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                         durability=DurabilityPolicy.TRANSIENT_LOCAL,
                         history=HistoryPolicy.KEEP_LAST)

    def on_signal(msg):
        recv = time.time_ns()
        try:
            data = json.loads(msg.data)
        except ValueError:
            data = None
        _emit({'kind': 'signal', 'recv_ns': recv, 'data': data})

    node.create_subscription(String, B.SIGNAL_TOPIC, on_signal, latched)

    # The image topics: the publisher's own QoS (camera_ingest_node.py).
    img_qos = QoSProfile(depth=5, reliability=ReliabilityPolicy.RELIABLE,
                         history=HistoryPolicy.KEEP_LAST)
    lock = threading.Lock()
    arrivals = {t: [] for t in args.topic}
    last_seen = {t: None for t in args.topic}

    def make_cb(topic):
        def cb(_msg):
            now = time.monotonic()
            with lock:
                arrivals[topic].append(now)
        return cb

    for t in args.topic:
        node.create_subscription(CompressedImage, t, make_cb(t), img_qos)

    session_client = None
    session_req_type = None
    if args.session_every > 0:
        try:
            from physical_ai_interfaces.srv import StationSession  # noqa: PLC0415
            session_client = node.create_client(StationSession, '/station/session')
            session_req_type = StationSession.Request
        except ImportError:
            _emit({'kind': 'session', 'error': 'this image has no StationSession service'})

    t_end = time.monotonic() + args.duration if args.duration > 0 else None
    next_session = time.monotonic()
    next_arrival = time.monotonic() + 1.0
    pending = None
    while not stop.is_set() and (t_end is None or time.monotonic() < t_end):
        rclpy.spin_once(node, timeout_sec=0.05)
        now = time.monotonic()
        if session_client is not None and pending is None and now >= next_session:
            next_session = now + args.session_every
            if session_client.service_is_ready():
                req = session_req_type()
                req.command = 'status'          # read-only; never enter/leave
                pending = (session_client.call_async(req), now)
            else:
                _emit({'kind': 'session', 'recv_ns': time.time_ns(), 'state': None,
                       'error': 'service /station/session not available'})
        if pending is not None:
            fut, t0 = pending
            if fut.done():
                pending = None
                r = fut.result()
                if r is None:
                    _emit({'kind': 'session', 'recv_ns': time.time_ns(), 'state': None,
                           'error': 'no answer'})
                else:
                    _emit({'kind': 'session', 'recv_ns': time.time_ns(),
                           'rtt_ms': round((now - t0) * 1000.0, 1),
                           'state': r.state, 'generation': int(r.generation),
                           'camera_check_epoch': int(r.camera_check_epoch),
                           'station_configured': bool(r.station_configured),
                           'source_ok': bool(r.source_ok), 'camera_ok': bool(r.camera_ok),
                           'robot_ok': bool(r.robot_ok), 'verified': bool(r.verified),
                           'workflow_active': bool(r.workflow_active)})
            elif now - t0 > 10.0:
                pending = None
                _emit({'kind': 'session', 'recv_ns': time.time_ns(), 'state': None,
                       'error': 'timeout'})
        if now >= next_arrival:
            next_arrival += 1.0
            with lock:
                snap = {t: list(v) for t, v in arrivals.items()}
                for t in arrivals:
                    arrivals[t] = arrivals[t][-1:]   # keep the last for the next gap
            for t, ts in snap.items():
                prev = last_seen[t]
                fresh = [x for x in ts if prev is None or x > prev]
                seq = ([prev] if prev is not None else []) + fresh
                gap = max(B.gaps(seq)) if len(seq) >= 2 else None
                if fresh:
                    last_seen[t] = fresh[-1]
                since = (now - last_seen[t]) if last_seen[t] is not None else None
                _emit({'kind': 'arrival', 'topic': t, 'recv_ns': time.time_ns(),
                       'frames': len(fresh),
                       'max_gap_ms': round(gap * 1000.0, 1) if gap is not None else None,
                       'since_last_ms': round(since * 1000.0, 1) if since is not None else None})
    node.destroy_node()
    rclpy.shutdown()
    return 0


if __name__ == '__main__':
    sys.exit(main())

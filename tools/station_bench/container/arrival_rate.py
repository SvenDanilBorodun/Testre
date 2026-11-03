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

"""Bench gates B2 / B3: arrival rate and age of the station frames at the
server (inside ``physical_ai_server``; dev only; run via ``container_run.py``).

Subscribes ``/station_scene/image_raw/compressed`` (and any other ``--topic``)
with the SERVER's own QoS for that topic (``bench_common.image_qos``: the
station store's BEST_EFFORT, KEEP_LAST 2; the recorder's BEST_EFFORT,
KEEP_LAST 32 for ``/scene``), so it counts what reaches the server, not the
retransmissions a RELIABLE subscriber would get. ``--reliable`` subscribes
with the publisher's RELIABLE, KEEP_LAST 5 instead, for comparison. It records
per frame the receive time on this container's clock and the
``header.stamp``, which is the WINDOWS capture time (``capture_ns``).

* B2: rate ≥ 14 fps and arrival-age p95 ≤ 0.3 s. The age is only honest with
  the clock offset (``clock_offset.py``): ``--offset-ns``/``--offset-unc-ns``
  correct it, and the summary states the residual uncertainty. Without them
  the raw age is reported and the verdict is UNKNOWN.
* B3 (``--qr``): decodes ``time_target.py``'s QR code from the frames and
  reports capture-to-arrival latency (p95 ≤ 0.35 s) against the time the
  target SHOWED, which includes the camera's own pipeline (the stamp does not).

Read-only. SIGINT or ``--duration`` ends it; the summary is written either way.
"""

import argparse
import os
import queue
import signal
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(1, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bench_common as B  # noqa: E402

FRAME_FIELDS = ['topic', 'i', 'recv_ns', 'stamp_ns', 'frame_id', 'bytes', 'raw_age_ms',
                'age_ms']
QR_FIELDS = ['recv_ns', 'stamp_ns', 'shown_ns', 'latency_ms', 'shown_to_stamp_ms']


def summarize_topic(recs, offset_ns, unc_ns, is_station):
    """Pure: the B2 numbers of one topic from ``[(recv_ns, stamp_ns)]``."""
    recv_s = [r[0] / 1e9 for r in recs]
    raw = [(r[0] - r[1]) / 1e6 for r in recs if r[1] > 0]
    corr = ([B.corrected_age_ns(r[0], r[1], offset_ns) / 1e6 for r in recs if r[1] > 0]
            if offset_ns is not None else [])
    out = {
        'frames': len(recs),
        'rate_fps': B.rate_hz(recv_s),
        'gap_ms': B.summarize([g * 1000.0 for g in B.gaps(recv_s)]),
        'raw_age_ms': B.summarize(raw),
        'age_ms': B.summarize(corr) if corr else None,
        'age_uncertainty_ms': (unc_ns / 1e6) if unc_ns is not None else None,
    }
    if is_station:
        out['B2 rate >= 14 fps'] = B.verdict(out['rate_fps'], B.B2_MIN_FPS, at_least=True)
        p95 = out['age_ms']['p95'] if corr else None
        if p95 is None:
            out['B2 age p95 <= 0.3 s'] = 'UNKNOWN (no clock offset given)'
        else:
            worst = p95 + (unc_ns / 1e6 if unc_ns else 0.0)
            best = p95 - (unc_ns / 1e6 if unc_ns else 0.0)
            v = B.verdict(worst / 1000.0, B.B2_AGE_P95_S, at_least=False)
            if v == 'FAIL' and best / 1000.0 <= B.B2_AGE_P95_S:
                v = 'UNDECIDED (inside the clock uncertainty)'
            out['B2 age p95 <= 0.3 s'] = v
    return out


def summarize_qr(rows, unc_ns):
    lat = [r['latency_ms'] for r in rows if r.get('latency_ms') is not None]
    s = B.summarize(lat)
    out = {'decoded': len(lat), 'latency_ms': s,
           'uncertainty_ms': (unc_ns / 1e6) if unc_ns is not None else None,
           'shown_to_stamp_ms': B.summarize([r['shown_to_stamp_ms'] for r in rows
                                             if r.get('shown_to_stamp_ms') is not None])}
    if s['p95'] is None:
        out['B3 latency p95 <= 0.35 s'] = 'UNKNOWN (nothing decoded)'
    else:
        u = unc_ns / 1e6 if unc_ns else 0.0
        v = B.verdict((s['p95'] + u) / 1000.0, B.B3_LATENCY_P95_S, at_least=False)
        if v == 'FAIL' and (s['p95'] - u) / 1000.0 <= B.B3_LATENCY_P95_S:
            v = 'UNDECIDED (inside the clock uncertainty)'
        out['B3 latency p95 <= 0.35 s'] = v
    return out


def build_parser():
    p = argparse.ArgumentParser(description='B2/B3: station frame arrival rate, age and latency.')
    p.add_argument('--out', required=True, help='output folder (inside the container)')
    p.add_argument('--topic', action='append', default=None,
                   help=f'topic (repeatable; default {B.STATION_TOPIC})')
    p.add_argument('--duration', type=float, default=60.0)
    p.add_argument('--offset-ns', type=int, default=None,
                   help='container = windows + offset (clock_offset.py)')
    p.add_argument('--offset-unc-ns', type=int, default=None)
    p.add_argument('--qr', action='store_true', help='decode the B3 time target')
    p.add_argument('--reliable', action='store_true',
                   help='comparison only: subscribe RELIABLE, KEEP_LAST 5 (the publisher\'s '
                        'QoS) instead of the server\'s BEST_EFFORT subscription')
    p.add_argument('--label', default='')
    p.add_argument('--stamp', default=None, help='file-name stamp (container_run passes the '
                                                 'Windows one)')
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    topics = args.topic or [B.STATION_TOPIC]
    import rclpy  # noqa: PLC0415
    from rclpy.signals import SignalHandlerOptions  # noqa: PLC0415
    from sensor_msgs.msg import CompressedImage  # noqa: PLC0415

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    run_stamp = args.stamp or B.stamp()
    flog = B.CsvLog(B.out_path(args.out, 'arrival_rate', run_stamp, '_frames.csv'),
                    FRAME_FIELDS)
    qlog = (B.CsvLog(B.out_path(args.out, 'arrival_rate', run_stamp, '_qr.csv'), QR_FIELDS)
            if args.qr else None)
    recs = {t: [] for t in topics}
    qr_rows = []
    qr_q = queue.Queue(maxsize=4)
    qr_dropped = [0]

    def qr_worker():
        import cv2  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
        det = cv2.QRCodeDetector()
        while not stop.is_set() or not qr_q.empty():
            try:
                recv_ns, stamp_ns, data = qr_q.get(timeout=0.2)
            except queue.Empty:
                continue
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
            if img is None:
                continue
            text, _, _ = det.detectAndDecode(img)
            shown = B.parse_qr_payload(text)
            if shown is None:
                continue
            row = {'recv_ns': recv_ns, 'stamp_ns': stamp_ns, 'shown_ns': shown,
                   'shown_to_stamp_ms': (stamp_ns - shown) / 1e6,
                   'latency_ms': (B.corrected_age_ns(recv_ns, shown, args.offset_ns) / 1e6
                                  if args.offset_ns is not None else None)}
            qlog.write(row)
            qr_rows.append(row)

    worker = None
    if args.qr:
        worker = threading.Thread(target=qr_worker, daemon=True)
        worker.start()

    # Our own SIGINT handler (set above) ends the loop and writes the summary;
    # rclpy's would shut the context down under the running loop instead.
    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    node = rclpy.create_node('station_bench_arrival')
    qos_specs = {t: B.image_qos(t, reliable=args.reliable) for t in topics}

    def make_cb(topic):
        def cb(msg):
            recv = time.time_ns()                 # first: the arrival on this clock
            stamp_ns = msg.header.stamp.sec * 1_000_000_000 + msg.header.stamp.nanosec
            i = len(recs[topic])
            recs[topic].append((recv, stamp_ns))
            raw = (recv - stamp_ns) / 1e6 if stamp_ns > 0 else None
            age = (B.corrected_age_ns(recv, stamp_ns, args.offset_ns) / 1e6
                   if stamp_ns > 0 and args.offset_ns is not None else None)
            flog.write({'topic': topic, 'i': i, 'recv_ns': recv, 'stamp_ns': stamp_ns,
                        'frame_id': msg.header.frame_id, 'bytes': len(msg.data),
                        'raw_age_ms': raw, 'age_ms': age})
            if args.qr and topic == B.STATION_TOPIC:
                try:
                    qr_q.put_nowait((recv, stamp_ns, bytes(msg.data)))
                except queue.Full:
                    qr_dropped[0] += 1
        return cb

    for t in topics:
        node.create_subscription(CompressedImage, t, make_cb(t), B.rclpy_qos(qos_specs[t]))
        print(f'{t}: {qos_specs[t][2]}', flush=True)
    print(f'listening on {topics} for {args.duration:.0f} s …', flush=True)
    t_end = time.monotonic() + args.duration
    next_print = time.monotonic() + 5.0
    while not stop.is_set() and time.monotonic() < t_end:
        rclpy.spin_once(node, timeout_sec=0.1)
        if time.monotonic() >= next_print:
            next_print += 5.0
            print('  ' + ', '.join(f'{t}: {len(v)} frames' for t, v in recs.items()), flush=True)
    stop.set()
    node.destroy_node()
    rclpy.shutdown()
    if worker is not None:
        worker.join(timeout=30.0)
    flog.close()
    if qlog:
        qlog.close()
    summary = {
        'gate': 'B2 (rate, age)' + (' / B3 (latency)' if args.qr else ''),
        'label': args.label, 'duration_s': args.duration,
        'clock_offset_ns': args.offset_ns, 'clock_offset_uncertainty_ns': args.offset_unc_ns,
        'qos': {t: {'reliability': q[0], 'depth': q[1], 'source': q[2]}
                for t, q in qos_specs.items()},
        'topics': {t: summarize_topic(v, args.offset_ns, args.offset_unc_ns,
                                      t == B.STATION_TOPIC) for t, v in recs.items()},
        'notes': ['recv is this container\'s clock; stamp is the Windows capture time '
                  '(capture_ns). Age = recv - (stamp + offset).',
                  'An extra subscriber adds one more DDS copy of every 1080p JPEG; a '
                  'browser preview is another. Report which were open.',
                  'Subscribed with the qos above: the server\'s own BEST_EFFORT subscription '
                  'unless --reliable was given (then a comparison, not the B2 number).'],
    }
    if args.qr:
        summary['qr'] = summarize_qr(qr_rows, args.offset_unc_ns)
        summary['qr']['dropped_unprocessed'] = qr_dropped[0]
    jp = B.out_path(args.out, 'arrival_rate', run_stamp, '_summary.json')
    B.write_json(jp, summary)
    print(f'wrote {jp}', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())

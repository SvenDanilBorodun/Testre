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

"""Bench gates S1 / S2 / S3 / S5: a read-only log of every station check
(inside ``physical_ai_server``; dev only; run via ``container_run.py``).

Polls, every ``--interval`` seconds, the two read-only ``status`` commands —
the same calls as ``ros2 service call /station/check
physical_ai_interfaces/srv/StationCheck "{command: status}"`` and
``/station/session … StationSession``, made through an rclpy client so the
answer needs no text parsing — and appends:

* every NEW check result to ``check_log_<stamp>_checks.csv`` (time, kind,
  result, rms / max / centroid / rotation, dot id / dxy / dz, generation, the
  per-reference residuals, the half-threshold verdict), with a running tally
  against S1 (camera: all OK, every metric ≤ half its threshold; robot: all
  OK). A re-solve's stored result is its own kind, ``resolve`` (``OK`` /
  ``RESOLVE_REFUSED``, no ref ids, no dot);
* every change of the owner's last re-solve job (``job_id`` / ``job_state``
  / ``message``) to ``…_jobs.csv``: a re-solve that fails before its own
  check (too few frames, the board solve, the write) stores no check result
  and shows only there;
* every session change to ``…_session.csv`` (state, generation, camera check
  epoch, camera_ok, robot_ok, verified, …) — the S5 evidence (a reload keeps
  the generation and ``robot_ok``; the camera re-checks) and the timing of
  „entry plus both checks" (new generation → verified, p50 / p95).

Limits, stated: the server keeps ONE last check per session generation with
no counter, so a check is seen only if the poll comes before the next check
replaces it, and two consecutive checks with exactly equal numbers count once.
``NO_FRAME`` / ``WRONG_SIZE`` / ``BUSY`` / ``NOT_OWNER`` answers are never
stored by the server and never appear here. Read-only: ``status`` only.
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

CHECK_FIELDS = ['t_iso', 'recv_ns', 'kind', 'result', 'success', 'generation', 'rms_mm',
                'max_mm', 'centroid_mm', 'rotation_deg', 'dot_id', 'dot_dxy_mm', 'dot_dz_mm',
                'ref_ids', 'ref_residual_mm', 'half_threshold_ok', 'camera_ok', 'robot_ok',
                'verified', 'job_id', 'job_state', 'message']
JOB_FIELDS = ['t_iso', 'recv_ns', 'job_id', 'job_state', 'generation', 'result', 'message']
SESSION_FIELDS = ['t_iso', 'recv_ns', 'state', 'generation', 'camera_check_epoch',
                  'station_configured', 'source_ok', 'camera_ok', 'robot_ok', 'verified',
                  'workflow_active', 'layout_id', 'calibration_digest', 'entry_to_verified_s']
# The StationCheck response fields read here (physical_ai_interfaces/srv/StationCheck.srv).
CHECK_RESPONSE_FIELDS = ('result', 'success', 'generation', 'rms_mm', 'max_mm', 'centroid_mm',
                         'rotation_deg', 'dot_id', 'dot_dxy_mm', 'dot_dz_mm', 'ref_ids',
                         'ref_residual_mm', 'camera_ok', 'robot_ok', 'verified', 'job_id',
                         'job_state', 'message')
SESSION_RESPONSE_FIELDS = ('state', 'generation', 'camera_check_epoch', 'station_configured',
                           'source_ok', 'camera_ok', 'robot_ok', 'verified', 'workflow_active',
                           'layout_id', 'calibration_digest')


def to_dict(resp, fields):
    out = {}
    for f in fields:
        v = getattr(resp, f)
        if isinstance(v, (list, tuple)) or type(v).__name__ == 'array':
            v = list(v)
        out[f] = v
    return out


class Recorder:
    """Pure bookkeeping (unit-tested without ROS): new checks, session changes."""

    def __init__(self):
        self.last_fp = None
        self.last_session = None
        self.last_job = None
        self.tally = B.CheckTally()
        self.entry = B.EntryTimer()
        self.checks = []

    def check(self, d):
        """Returns the new check's kind, or None when it is not new."""
        if not d.get('result'):
            return None
        fp = B.check_fingerprint(d)
        if fp == self.last_fp:
            return None
        self.last_fp = fp
        kind = B.check_kind(d)
        self.tally.add(kind, d)
        self.checks.append((kind, d))
        return kind or 'unknown'

    def job(self, d):
        """True when the owner's last job (id, state, message) changed; job 0
        (no job of this session's owner) is never logged."""
        key = (int(d.get('job_id') or 0), d.get('job_state') or '', d.get('message') or '')
        if key[0] == 0 or key == self.last_job:
            return False
        self.last_job = key
        return True

    def session(self, t_s, d):
        """Returns (changed, entry_to_verified_s or None)."""
        dur = self.entry.update(t_s, d.get('state') or '', int(d.get('generation') or 0),
                                bool(d.get('verified')))
        key = tuple(d.get(k) for k in SESSION_RESPONSE_FIELDS)
        changed = key != self.last_session
        self.last_session = key
        return changed, dur


def build_parser():
    p = argparse.ArgumentParser(description='S1/S2/S3/S5: log every station check (read-only).')
    p.add_argument('--out', required=True)
    p.add_argument('--interval', type=float, default=1.0)
    p.add_argument('--duration', type=float, default=0.0, help='seconds (0 = until Ctrl+C)')
    p.add_argument('--label', default='')
    p.add_argument('--stamp', default=None, help='file-name stamp (container_run passes the '
                                                 'Windows one)')
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    import rclpy  # noqa: PLC0415
    from rclpy.signals import SignalHandlerOptions  # noqa: PLC0415
    from physical_ai_interfaces.srv import StationCheck, StationSession  # noqa: PLC0415

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    run_stamp = args.stamp or B.stamp()
    clog = B.CsvLog(B.out_path(args.out, 'check_log', run_stamp, '_checks.csv'), CHECK_FIELDS)
    slog = B.CsvLog(B.out_path(args.out, 'check_log', run_stamp, '_session.csv'),
                    SESSION_FIELDS)
    jlog = B.CsvLog(B.out_path(args.out, 'check_log', run_stamp, '_jobs.csv'), JOB_FIELDS)
    rec = Recorder()
    # Our own SIGINT handler (set above) ends the loop and writes the summary;
    # rclpy's would shut the context down under the running loop instead.
    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    node = rclpy.create_node('station_bench_checklog')
    cc = node.create_client(StationCheck, '/station/check')
    sc = node.create_client(StationSession, '/station/session')

    def call(client, req_type):
        req = req_type.Request()
        req.command = 'status'             # read-only; never camera / robot / enter / leave
        fut = client.call_async(req)
        rclpy.spin_until_future_complete(node, fut, timeout_sec=5.0)
        return fut.result() if fut.done() else None

    t0 = time.monotonic()
    t_end = t0 + args.duration if args.duration > 0 else None
    warned = False
    print('logging station checks (Ctrl+C to stop) …', flush=True)
    while not stop.is_set() and (t_end is None or time.monotonic() < t_end):
        t_loop = time.monotonic()
        if not (cc.service_is_ready() and sc.service_is_ready()):
            if not warned:
                print('waiting for /station/check and /station/session …', flush=True)
                warned = True
            time.sleep(args.interval)
            rclpy.spin_once(node, timeout_sec=0.0)
            continue
        s = call(sc, StationSession)
        c = call(cc, StationCheck)
        now_ns = time.time_ns()
        if s is not None:
            sd = to_dict(s, SESSION_RESPONSE_FIELDS)
            changed, dur = rec.session(time.monotonic() - t0, sd)
            if changed or dur is not None:
                slog.write(dict(sd, t_iso=B.iso_now(), recv_ns=now_ns,
                                calibration_digest=(sd.get('calibration_digest') or '')[:12],
                                entry_to_verified_s=dur))
                print(f"session {sd['state']} gen {sd['generation']} camera_ok "
                      f"{sd['camera_ok']} robot_ok {sd['robot_ok']} verified {sd['verified']}"
                      + (f' — entry to verified {dur:.1f} s' if dur is not None else ''),
                      flush=True)
        if c is not None:
            cd = to_dict(c, CHECK_RESPONSE_FIELDS)
            if rec.job(cd):
                jlog.write(dict(cd, t_iso=B.iso_now(), recv_ns=now_ns))
                print(f"re-solve job {cd['job_id']}: {cd['job_state']}"
                      + (f" — {cd['message']}" if cd['message'] else ''), flush=True)
            kind = rec.check(cd)
            if kind is not None:
                clog.write(dict(cd, t_iso=B.iso_now(), recv_ns=now_ns, kind=kind,
                                half_threshold_ok=B.half_threshold_ok(kind, cd)))
                if kind == 'robot':
                    what = (f"dot {cd['dot_id']} dxy {cd['dot_dxy_mm']:.2f} dz "
                            f"{cd['dot_dz_mm']:.2f} mm")
                elif kind == 'resolve':
                    what = (f"re-solve, its check rms {cd['rms_mm']:.2f} max "
                            f"{cd['max_mm']:.2f} centroid {cd['centroid_mm']:.2f} mm rot "
                            f"{cd['rotation_deg']:.3f}° (all 0 when its check found no refs)")
                else:
                    what = (f"rms {cd['rms_mm']:.2f} max {cd['max_mm']:.2f} centroid "
                            f"{cd['centroid_mm']:.2f} mm rot {cd['rotation_deg']:.3f}°")
                print(f"{kind} check {cd['result']}: {what}", flush=True)
                print('  ' + rec.tally.line(), flush=True)
        time.sleep(max(0.0, args.interval - (time.monotonic() - t_loop)))
    node.destroy_node()
    rclpy.shutdown()
    clog.close()
    slog.close()
    jlog.close()
    durs = rec.entry.durations
    summary = {
        'gates': 'S1 (camera: all OK at half the thresholds; robot: all OK), S2 (MOVED / '
                 'ROBOT_MOVED), S3 (re-solve: kind resolve, OK / RESOLVE_REFUSED, and the '
                 'jobs CSV), S5 (session continuity), entry plus both checks',
        'label': args.label, 'checks': rec.tally.summary(),
        'entry_to_verified_s': B.summarize(durs),
        'entry_typical_le_120_s': B.verdict(B.percentile(durs, 50), B.ENTRY_TYPICAL_S,
                                            at_least=False),
        'half_thresholds': {'camera_mm_mm_mm_deg': [B.CAMERA_OK_RMS_MM / 2,
                                                    B.CAMERA_OK_MAX_MM / 2,
                                                    B.CAMERA_OK_CENTROID_MM / 2,
                                                    B.CAMERA_OK_ROTATION_DEG / 2],
                            'robot_dxy_dz_mm': [B.ROBOT_OK_DXY_MM / 2, B.ROBOT_OK_DZ_MM / 2]},
        'notes': ['One stored check per generation, no counter: a check replaced before the '
                  'next poll is missed; identical consecutive values count once.',
                  'A re-solve stores its own check as OK / RESOLVE_REFUSED (kind resolve); one '
                  'that fails before that check (too few frames, the board solve, the write) '
                  'stores nothing and shows only in the jobs CSV (job_state failed).',
                  'entry_to_verified times only sessions entered after logging started.',
                  'entry_to_verified is measured from the first poll that sees a new session '
                  'generation (ENTERING/ACTIVE) to the first poll with verified=true; '
                  'resolution = the poll interval. The click before „Station starten" is '
                  'not included; use pick_tally.py stopwatch for that.'],
    }
    jp = B.out_path(args.out, 'check_log', run_stamp, '_summary.json')
    B.write_json(jp, summary)
    print(f'wrote {jp}', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())

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

"""Windows ↔ container clock offset for B2/B3 (Windows host; dev only).

The station frame's ``header.stamp`` is the WINDOWS capture time
(``capture_ns = time.time_ns()`` in the GUI); the receiver in the container
stamps arrival with the WSL kernel clock. An arrival age is only meaningful
once the two clocks' offset is known, so this measures it.

One long-lived pipe: ``wsl -d EduBotics -- docker exec -i physical_ai_server
bash -s`` running ``while read; do date +%s%N; done`` (or the distro itself
with ``--target distro``; the container shares the WSL kernel clock). Each
round trip is ``(t_send, remote_ns, t_recv)`` on the Windows ``time.time_ns``
clock; the fastest round trip bounds the offset to ±RTT/2. ``--repeat`` takes
several estimates spaced ``--every`` seconds apart, to see drift.

Writes ``<out>/clock_offset_<stamp>.json`` (pass it to
``container_run.py arrival_rate --offset-json …``) and the raw samples CSV.
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bench_common as B  # noqa: E402
import wsl_docker as W  # noqa: E402

LOOP = b'while IFS= read -r l; do date +%s%N; done\n'


def sample_round_trips(proc, n):
    out = []
    for _ in range(n):
        t_send = time.time_ns()
        proc.stdin.write(b'x\n')
        proc.stdin.flush()
        line = proc.stdout.readline()
        t_recv = time.time_ns()
        if not line:
            raise RuntimeError('the clock pipe closed')
        remote = int(line.strip())
        out.append((t_send, remote, t_recv))
    return out


def open_pipe(target, distro):
    if target == 'container':
        argv = W.bash_stdin_argv(distro)
    else:
        argv = W.wsl_argv(['bash', '-s'], distro)
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, creationflags=W._CREATE_NO_WINDOW)
    proc.stdin.write(LOOP)
    proc.stdin.flush()
    return proc


def build_parser():
    p = argparse.ArgumentParser(description='Measure the Windows <-> container clock offset.')
    p.add_argument('--out', required=True)
    p.add_argument('--target', choices=('container', 'distro'), default='container')
    p.add_argument('--samples', type=int, default=200, help='round trips per estimate')
    p.add_argument('--repeat', type=int, default=1, help='estimates (drift check)')
    p.add_argument('--every', type=float, default=30.0, help='seconds between estimates')
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    run_stamp = B.stamp()
    log = B.CsvLog(B.out_path(args.out, 'clock_offset', run_stamp, '_samples.csv'),
                   ['estimate', 't_send_ns', 'remote_ns', 't_recv_ns', 'rtt_us', 'offset_ns'])
    proc = open_pipe(args.target, W.distro_name())
    estimates = []
    try:
        sample_round_trips(proc, 5)          # warm the relay
        for k in range(args.repeat):
            if k:
                time.sleep(args.every)
            s = sample_round_trips(proc, args.samples)
            for a, r, b in s:
                log.write({'estimate': k, 't_send_ns': a, 'remote_ns': r, 't_recv_ns': b,
                           'rtt_us': (b - a) / 1000.0, 'offset_ns': r - (a + b) // 2})
            est = B.estimate_clock_offset(s)
            est['at_iso'] = B.iso_now()
            estimates.append(est)
            print(f"offset {est['offset_ns'] / 1e6:+.3f} ms ± {est['uncertainty_ns'] / 1e6:.3f} ms "
                  f"(fastest RTT {est['rtt_min_ns'] / 1e6:.3f} ms, n={est['n']})")
    finally:
        try:
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        proc.kill()
        log.close()
    best = min(estimates, key=lambda e: e['uncertainty_ns'])
    drift = (estimates[-1]['offset_ns'] - estimates[0]['offset_ns']) if len(estimates) > 1 else None
    out = {'serves': 'B2 / B3 (the offset that makes an arrival age or a latency honest)',
           'target': args.target, 'estimate': best, 'all_estimates': estimates,
           'drift_ns_first_to_last': drift,
           'notes': ['remote_ns = windows_ns + offset_ns; corrected age = '
                     'recv_container - (capture_ns_windows + offset_ns), true within '
                     '± uncertainty_ns (plus any drift since the estimate).',
                     'Re-measure right before and after a B2/B3 run; WSL2 re-syncs its clock '
                     'with the host only now and then (after sleep it can be off by seconds).']}
    jp = B.out_path(args.out, 'clock_offset', run_stamp, '.json')
    B.write_json(jp, out)
    print(f'wrote {jp}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

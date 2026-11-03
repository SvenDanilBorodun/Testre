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

"""Bench gate G8 tally and a lap stopwatch (any host; dev only; stdlib).

``tally``: one key per supervised pick — ``s`` success (first attempt),
``f`` fail, ``u`` unsafe contact; ``n`` adds a note to the last pick, ``z``
undoes it, ``q`` ends. Every pick is time-stamped and appended to
``pick_tally_<stamp>.csv`` at once; the summary checks G8 (≥ 50 picks,
≥ 95 % first-attempt, no unsafe contact).

``stopwatch``: Enter starts a lap, Enter ends it (``q`` + Enter quits), for
„entry plus both checks" (typical ≤ 2 min, p50 / p95) and for B4's lens solve
(click „Linse berechnen" → the result line; ≤ 3 s). Reaction time is in every
lap (about ±0.2 s), which matters for the 3 s gate and not for the 2 min one.

Talks to nothing: no robot, no network.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bench_common as B  # noqa: E402


def getch():
    """One key without Enter (Windows msvcrt, else termios)."""
    if sys.platform == 'win32':
        import msvcrt  # noqa: PLC0415
        ch = msvcrt.getwch()
        return ch
    import termios  # noqa: PLC0415
    import tty  # noqa: PLC0415
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


class Tally:
    """Pure state of a pick session (unit-tested)."""

    def __init__(self):
        self.picks = []          # dicts: n, t_iso, t_s, outcome, note

    def key(self, ch, t_s, t_iso):
        ch = (ch or '').lower()
        if ch in B.PICK_KEYS:
            p = {'n': len(self.picks) + 1, 't_iso': t_iso, 't_s': round(t_s, 3),
                 'outcome': B.PICK_KEYS[ch], 'note': ''}
            self.picks.append(p)
            return p
        if ch == 'z' and self.picks:
            return {'undo': self.picks.pop()}
        return None

    def summary(self):
        return B.pick_summary([p['outcome'] for p in self.picks])


def run_tally(args):
    run_stamp = B.stamp()
    csv_path = B.out_path(args.out, 'pick_tally', run_stamp, '.csv')
    t = Tally()
    t0 = time.monotonic()
    print('keys: s = success (first attempt), f = fail, u = unsafe contact, '
          'n = note on the last pick, z = undo, q = quit')
    while True:
        ch = getch()
        if ch in ('q', 'Q', '\x03'):
            break
        if ch in ('n', 'N') and t.picks:
            note = input('\nnote: ')
            t.picks[-1]['note'] = note
            continue
        r = t.key(ch, time.monotonic() - t0, B.iso_now())
        if r is None:
            continue
        s = t.summary()
        what = (f"undo pick {r['undo']['n']}" if 'undo' in r else
                f"pick {r['n']}: {r['outcome']}")
        print(f"\r{what} — {s['success']}/{s['picks']} ok, {s['unsafe']} unsafe"
              + (f", {100 * s['first_attempt_rate']:.1f} %" if s['first_attempt_rate'] is not None
                 else ''))
        log = B.CsvLog(csv_path, ['n', 't_iso', 't_s', 'outcome', 'note'])
        for p in t.picks:                     # rewritten whole: undo and notes stay right
            log.write(p)
        log.close()
    s = t.summary()
    jp = B.out_path(args.out, 'pick_tally', run_stamp, '_summary.json')
    B.write_json(jp, dict(s, label=args.label,
                          gate='G8: >= 50 supervised picks over the accepted zone, >= 95 % '
                               'first-attempt, no unsafe contact'))
    print(f"\nG8: {s['g8_verdict']} — {s['picks']} picks, {s['success']} first-attempt, "
          f"{s['unsafe']} unsafe\nwrote {csv_path}\n      {jp}")
    return 0


def run_stopwatch(args):
    run_stamp = B.stamp()
    log = B.CsvLog(B.out_path(args.out, 'stopwatch', run_stamp, '.csv'),
                   ['lap', 'start_iso', 'seconds', 'label'])
    laps = []
    print('Enter = start, Enter = stop; q + Enter = quit')
    try:
        while True:
            if input(f'lap {len(laps) + 1}: Enter to start … ').strip().lower() == 'q':
                break
            start_iso, t = B.iso_now(), time.perf_counter()
            if input('  running — Enter to stop … ').strip().lower() == 'q':
                break
            d = time.perf_counter() - t
            laps.append(d)
            log.write({'lap': len(laps), 'start_iso': start_iso, 'seconds': d,
                       'label': args.label})
            s = B.summarize(laps)
            print(f'  {d:.2f} s — p50 {s["p50"]:.2f} s, p95 {s["p95"]:.2f} s over {s["n"]}')
    except (KeyboardInterrupt, EOFError):
        pass
    log.close()
    s = B.summarize(laps)
    out = {'label': args.label, 'laps_s': laps, 'summary': s,
           'gate': {'entry': 'entry plus both checks: typical <= 2 min (p50 / p95)',
                    'lens': 'B4: lens solve on 30 views <= 3 s'}.get(args.gate, '')}
    if args.gate == 'entry':
        out['entry typical <= 120 s (p50)'] = B.verdict(
            s['p50'], B.ENTRY_TYPICAL_S, at_least=False)
    elif args.gate == 'lens':
        out['B4 lens solve <= 3 s (max)'] = B.verdict(s['max'], B.B4_LENS_SOLVE_S,
                                                      at_least=False)
    jp = B.out_path(args.out, 'stopwatch', run_stamp, '_summary.json')
    B.write_json(jp, out)
    print(f'wrote {jp}')
    return 0


def build_parser():
    p = argparse.ArgumentParser(description='G8 pick tally and a lap stopwatch.')
    sub = p.add_subparsers(dest='mode', required=True)
    t = sub.add_parser('tally', help='G8: one key per supervised pick')
    t.add_argument('--out', required=True)
    t.add_argument('--label', default='')
    w = sub.add_parser('stopwatch', help='laps with p50 / p95')
    w.add_argument('--out', required=True)
    w.add_argument('--label', default='')
    w.add_argument('--gate', choices=('entry', 'lens', 'none'), default='none')
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return run_tally(args) if args.mode == 'tally' else run_stopwatch(args)


if __name__ == '__main__':
    sys.exit(main())

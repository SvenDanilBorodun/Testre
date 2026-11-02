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

"""Bench gate B1 run logger (Windows host, environment RUNNING; dev only).

Logs, every ``--interval`` seconds, into ``<out>/run_logger_<stamp>.csv``:

* the GUI bridge's ``GET http://127.0.0.1:8769/station/status``:
  ``enabled``, ``stream``, ``scene_fps`` (the station crop, i.e. the recording
  stream's capture rate over the bridge's 2 s window), ``station_fps`` (the
  Studio stream's send rate under id 200) and ``error_de``;
* the GUI process CPU (``GetProcessTimes`` through ctypes: psutil is not a GUI
  requirement, ``wmic`` is gone from Windows 11 24H2, and ``typeperf``'s
  instance names cannot tell the GUI from its WebView2 child, both being
  ``EduBotics.exe``);
* the gripper and scene camera rates the SERVER measures
  (``/edubotics/signal_status``), read by an in-container watcher
  (``container/watch_stream.py``) that also polls ``/station/session status``
  and reports per-second arrival gaps of ``/scene/image_raw/compressed``.

``--toggle N`` switches the Studio stream on and off N times through
``POST /station/stream`` (with the page's Origin), each ``on`` phase well
inside the bridge's 10 s lease, ONLY while no station session exists (checked
read-only before and during the run). It records, per cycle, what the bridge
exposes about a camera reopen (its German ``error_de`` — the bridge has no
reopen counter) and the largest arrival gap of the recording stream seen in
the container. Ctrl+C always switches the stream off.

Read-only towards the robot: never ``enter``/``leave``, no motion, no torque.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import pathlib
import subprocess
import sys
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bench_common as B  # noqa: E402
import wsl_docker as W  # noqa: E402

FIELDS = ['t_iso', 't_s', 'label', 'bridge_http', 'enabled', 'stream', 'scene_fps',
          'station_fps', 'error_de', 'gui_pid', 'gui_cpu_pct_one_core', 'gui_cpu_pct_all_cores',
          'gripper_hz', 'gripper_age_s', 'scene_hz', 'scene_age_s', 'signal_seq',
          'signal_recv_age_s', 'session_state', 'session_generation', 'scene_max_gap_ms_1s',
          'scene_frames_1s', 'toggle_cycle', 'toggle_phase']
TOGGLE_FIELDS = ['cycle', 't_on_iso', 'on_http', 'on_ok', 'off_http', 'scene_fps_min',
                 'station_fps_max', 'error_de_seen', 'reopen_suspected',
                 'scene_max_gap_ms', 'session_state']


# ── GUI process CPU (Windows, ctypes) ────────────────────────────────────────
class _FILETIME(ctypes.Structure):
    _fields_ = [('lo', ctypes.c_uint32), ('hi', ctypes.c_uint32)]


def _ft(ft):
    return ((ft.hi << 32) | ft.lo) / 1e7     # 100 ns ticks → seconds


class ProcessCpu:
    """CPU seconds of one process from GetProcessTimes; rate between samples."""

    def __init__(self, pid):
        self.pid = int(pid)
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        self._k32 = k32
        self._h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, self.pid)
        if not self._h:
            raise OSError(f'OpenProcess({self.pid}) failed')
        self._last = None

    def cpu_s(self):
        c, e, k, u = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
        if not self._k32.GetProcessTimes(self._h, ctypes.byref(c), ctypes.byref(e),
                                         ctypes.byref(k), ctypes.byref(u)):
            return None
        return _ft(k) + _ft(u)

    def sample(self):
        """(% of one core, % of all cores) since the previous sample."""
        now, cpu = time.monotonic(), self.cpu_s()
        if cpu is None:
            return None, None
        prev, self._last = self._last, (now, cpu)
        if prev is None or now <= prev[0]:
            return None, None
        one = 100.0 * (cpu - prev[1]) / (now - prev[0])
        return one, one / max(1, os.cpu_count() or 1)


def find_gui_pid():
    """The GUI's PID: ``EduBotics.exe`` without ``--webview`` (the student
    window is a child of the same exe), else a ``python … main.py`` under
    ``robotis_ai_setup\\gui``. One PowerShell CIM query; ``--gui-pid`` skips it."""
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -in "
          "@('EduBotics.exe','python.exe','pythonw.exe') } | "
          "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress")
    try:
        r = subprocess.run(['powershell', '-NoProfile', '-Command', ps], capture_output=True,
                           timeout=30, creationflags=0x08000000)
        rows = json.loads(r.stdout.decode('utf-8', 'replace') or '[]')
    except Exception:  # noqa: BLE001
        return None
    return pick_gui_pid(rows if isinstance(rows, list) else [rows])


def pick_gui_pid(rows):
    """Pure: the GUI process among CIM rows ``{ProcessId, Name, CommandLine}``."""
    for r in rows:
        cl = (r.get('CommandLine') or '')
        if (r.get('Name') or '').lower() == 'edubotics.exe' and '--webview' not in cl:
            return int(r['ProcessId'])
    for r in rows:
        cl = (r.get('CommandLine') or '').replace('/', '\\').lower()
        if 'main.py' in cl and '\\gui' in cl and '--webview' not in cl:
            return int(r['ProcessId'])
    return None


# ── the watcher's latest values ──────────────────────────────────────────────
class Latest:
    def __init__(self):
        self.lock = threading.Lock()
        self.signal = None
        self.signal_t = None
        self.session = None
        self.session_t = None
        self.scene = {'frames_1s': None, 'max_gap_ms_1s': None}
        self.scene_gap_window_max = 0.0     # reset by the toggle loop per cycle
        self.lines = 0

    def feed(self, line):
        try:
            d = json.loads(line)
        except ValueError:
            return
        now = time.monotonic()
        with self.lock:
            self.lines += 1
            k = d.get('kind')
            if k == 'signal' and isinstance(d.get('data'), dict):
                try:
                    self.signal = B.parse_signal_status(json.dumps(d['data']))
                    self.signal_t = now
                except ValueError:
                    pass
            elif k == 'session':
                self.session, self.session_t = d, now
            elif k == 'arrival' and d.get('topic') == B.SCENE_TOPIC:
                self.scene = {'frames_1s': d.get('frames'), 'max_gap_ms_1s': d.get('max_gap_ms')}
                for g in (d.get('max_gap_ms'), d.get('since_last_ms')):
                    if g is not None:
                        self.scene_gap_window_max = max(self.scene_gap_window_max, float(g))

    def session_state(self):
        with self.lock:
            if self.session is None:
                return None, None
            return self.session.get('state'), time.monotonic() - self.session_t


def build_parser():
    p = argparse.ArgumentParser(description='B1 run logger: bridge /station/status, GUI CPU, '
                                            'server-side camera rates; optional stream toggles.')
    p.add_argument('--out', required=True, help='folder for the CSV/JSON (created)')
    p.add_argument('--duration', type=float, default=600.0, help='seconds (default 600 = 10 min)')
    p.add_argument('--interval', type=float, default=1.0, help='seconds between rows')
    p.add_argument('--label', default='', help='free text stored in every row, e.g. '
                                               'baseline / stream-off / stream-on')
    p.add_argument('--gui-pid', type=int, default=None, help='skip the PID search')
    p.add_argument('--no-cpu', action='store_true')
    p.add_argument('--no-container', action='store_true',
                   help='do not start the in-container watcher (no signal_status, no gaps)')
    p.add_argument('--gripper-baseline-hz', type=float, default=None,
                   help='the no-station gripper rate (from a baseline run) for the B1 verdict')
    p.add_argument('--toggle', type=int, default=0, help='switch the Studio stream N times')
    p.add_argument('--on-s', type=float, default=2.0, help='seconds on per toggle (<= 8)')
    p.add_argument('--off-s', type=float, default=2.0, help='seconds off per toggle')
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.toggle:
        ok, why = B.toggle_timing_ok(args.on_s, args.off_s)
        if not ok:
            print(f'refused: {why}')
            return 2
    run_stamp = B.stamp()
    csv_path = B.out_path(args.out, 'run_logger', run_stamp, '.csv')
    log = B.CsvLog(csv_path, FIELDS)
    tlog = B.CsvLog(B.out_path(args.out, 'run_logger', run_stamp, '_toggles.csv'),
                    TOGGLE_FIELDS) if args.toggle else None

    cpu = None
    gui_pid = args.gui_pid
    if not args.no_cpu and sys.platform == 'win32':
        gui_pid = gui_pid or find_gui_pid()
        if gui_pid:
            try:
                cpu = ProcessCpu(gui_pid)
                cpu.sample()
            except OSError as e:
                print(f'GUI CPU not available: {e}')
        else:
            print('GUI process not found (pass --gui-pid); CPU is not logged.')

    latest = Latest()
    stream = None
    if not args.no_container:
        topics = ['--topic', B.SCENE_TOPIC]
        stream = W.ContainerStream(HERE / 'container' / 'watch_stream.py',
                                   ['--session-every', '1.0'] + topics,
                                   distro=W.distro_name())
        stream.start()
        threading.Thread(target=lambda: [latest.feed(x) for x in stream.lines()],
                         daemon=True).start()

    rows = []
    toggles = []
    stop = threading.Event()
    sent_on = []        # an `on` was sent: only then does the final `off` belong to us
    t0 = time.monotonic()

    def write_row(cycle='', phase=''):
        code, st = W.bridge_get('/station/status', timeout=1.0)
        st = st or {}
        one, allc = cpu.sample() if cpu else (None, None)
        with latest.lock:
            sig = latest.signal
            sig_age = (time.monotonic() - latest.signal_t) if latest.signal_t else None
            sess = latest.session or {}
            scene = dict(latest.scene)
        cams = (sig or {}).get('cameras', {})
        row = {
            't_iso': B.iso_now(), 't_s': round(time.monotonic() - t0, 3), 'label': args.label,
            'bridge_http': code, 'enabled': st.get('enabled'), 'stream': st.get('stream'),
            'scene_fps': st.get('scene_fps'), 'station_fps': st.get('station_fps'),
            'error_de': st.get('error_de'), 'gui_pid': gui_pid,
            'gui_cpu_pct_one_core': one, 'gui_cpu_pct_all_cores': allc,
            'gripper_hz': (cams.get('gripper') or {}).get('hz'),
            'gripper_age_s': (cams.get('gripper') or {}).get('age_s'),
            'scene_hz': (cams.get('scene') or {}).get('hz'),
            'scene_age_s': (cams.get('scene') or {}).get('age_s'),
            'signal_seq': (sig or {}).get('seq'), 'signal_recv_age_s': sig_age,
            'session_state': sess.get('state'), 'session_generation': sess.get('generation'),
            'scene_max_gap_ms_1s': scene.get('max_gap_ms_1s'),
            'scene_frames_1s': scene.get('frames_1s'),
            'toggle_cycle': cycle, 'toggle_phase': phase,
        }
        log.write(row)
        rows.append(row)
        return row

    def toggle_cycle(i):
        if stream:
            state, age = latest.session_state()
        else:
            state, age = W.session_state_via_cli()[0], 0.0
        _, bridge = W.bridge_get('/station/status', timeout=1.0)
        ok, why = B.toggle_guard(state, bridge, age)
        if not ok:
            print(f'toggle stopped before cycle {i}: {why}')
            return False
        with latest.lock:
            latest.scene_gap_window_max = 0.0
        rec = {'cycle': i, 't_on_iso': B.iso_now(), 'session_state': state}
        sent_on.append(i)
        code, ans = W.bridge_stream(True)
        rec['on_http'], rec['on_ok'] = code, bool(ans and ans.get('ok'))
        mins, maxs, errs = [], [], []
        try:
            t_end = time.monotonic() + args.on_s
            while time.monotonic() < t_end and not stop.is_set():
                r = write_row(i, 'on')
                if r['scene_fps'] is not None:
                    mins.append(float(r['scene_fps']))
                if r['station_fps'] is not None:
                    maxs.append(float(r['station_fps']))
                if r['error_de']:
                    errs.append(r['error_de'])
                time.sleep(0.25)
        finally:
            rec['off_http'], _ = W.bridge_stream(False)
        t_end = time.monotonic() + args.off_s
        while time.monotonic() < t_end and not stop.is_set():
            r = write_row(i, 'off')
            if r['scene_fps'] is not None:
                mins.append(float(r['scene_fps']))
            if r['error_de']:
                errs.append(r['error_de'])
            time.sleep(0.25)
        with latest.lock:
            gap = latest.scene_gap_window_max if stream else None
        rec.update(scene_fps_min=min(mins) if mins else None,
                   station_fps_max=max(maxs) if maxs else None,
                   error_de_seen=' / '.join(sorted(set(errs))),
                   reopen_suspected=any(B.reopen_suspected(e) for e in errs),
                   scene_max_gap_ms=gap)
        tlog.write(rec)
        toggles.append(rec)
        print(f"toggle {i}: on {rec['on_http']} off {rec['off_http']} crop min "
              f"{rec['scene_fps_min']} fps, studio max {rec['station_fps_max']} fps, "
              f"recording gap {gap} ms{', REOPEN?' if rec['reopen_suspected'] else ''}")
        return True

    try:
        if stream:
            # Give the watcher a moment for its first session answer.
            deadline = time.monotonic() + 15.0
            while latest.session_state()[0] is None and time.monotonic() < deadline:
                time.sleep(0.25)
        if args.toggle:
            for i in range(1, args.toggle + 1):
                if not toggle_cycle(i):
                    break
        else:
            t_end = t0 + args.duration
            while time.monotonic() < t_end:
                write_row()
                time.sleep(max(0.0, args.interval - 0.05))
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if sent_on:
            # Never leave OUR stream on. Not sent after a refusal: a page that
            # holds a session owns the stream then, and an `off` would fight it.
            W.bridge_stream(False)
        if stream:
            stream.stop()
        log.close()
        if tlog:
            tlog.close()
    summary = summarize(rows, toggles, args)
    json_path = B.out_path(args.out, 'run_logger', run_stamp, '_summary.json')
    B.write_json(json_path, summary)
    print(json.dumps(summary.get('verdicts', {}), indent=2))
    print(f'wrote {csv_path}\n      {json_path}')
    return 0


def summarize(rows, toggles, args):
    """Pure summary of the logged rows (unit-tested)."""
    def col(name, pred=lambda r: True):
        return [float(r[name]) for r in rows
                if r.get(name) not in (None, '') and pred(r)]
    # Not 'on' counts as off: a baseline rig without the station may have no
    # bridge at all (omx_follower), and its rows still carry the gripper rate.
    off = lambda r: r.get('stream') != 'on'    # noqa: E731
    on = lambda r: r.get('stream') == 'on'     # noqa: E731
    crop_off, crop_on = col('scene_fps', off), col('scene_fps', on)
    studio_on = col('station_fps', on)
    grip_off, grip_on = col('gripper_hz', off), col('gripper_hz', on)
    out = {
        'gate': 'B1: crop >= 29 fps (stream off and on), Studio >= 14 fps under id 200, gripper '
                'within 1 fps of its no-station baseline, GUI CPU recorded, 200 switches without '
                'a camera reopen or a recording-stream gap',
        'label': args.label, 'rows': len(rows),
        'stream_off': {'crop_fps': B.summarize(crop_off), 'gripper_hz': B.summarize(grip_off),
                       'gui_cpu_pct_one_core': B.summarize(col('gui_cpu_pct_one_core', off)),
                       'crop_windows_below_29': sum(1 for v in crop_off if v < B.B1_CROP_MIN_FPS)},
        'stream_on': {'crop_fps': B.summarize(crop_on), 'studio_fps': B.summarize(studio_on),
                      'gripper_hz': B.summarize(grip_on),
                      'gui_cpu_pct_one_core': B.summarize(col('gui_cpu_pct_one_core', on)),
                      'crop_windows_below_29': sum(1 for v in crop_on if v < B.B1_CROP_MIN_FPS),
                      'studio_windows_below_14': sum(1 for v in studio_on
                                                     if v < B.B1_STUDIO_MIN_FPS)},
        'errors_de': sorted({r['error_de'] for r in rows if r.get('error_de')}),
        'notes': [
            'scene_fps is the bridge capture rate over its own 2 s window; the gate says '
            '"the station crop >= 29 fps" without a window, so the mean and every window '
            'below 29 are both reported.',
            'gripper_hz is the server-side rate (signal_status, ~2 s window); compare with a '
            'baseline run on the same PC WITHOUT the station (--gripper-baseline-hz).',
        ],
    }
    v = {}
    if crop_off:
        v['B1 crop >= 29 fps, stream off (mean)'] = B.verdict(
            out['stream_off']['crop_fps']['mean'], B.B1_CROP_MIN_FPS, at_least=True)
    if crop_on:
        v['B1 crop >= 29 fps, stream on (mean)'] = B.verdict(
            out['stream_on']['crop_fps']['mean'], B.B1_CROP_MIN_FPS, at_least=True)
    if studio_on:
        v['B1 Studio >= 14 fps under id 200 (mean)'] = B.verdict(
            out['stream_on']['studio_fps']['mean'], B.B1_STUDIO_MIN_FPS, at_least=True)
    base = getattr(args, 'gripper_baseline_hz', None)
    for name, vals in (('stream off', grip_off), ('stream on', grip_on)):
        if vals and base is not None:
            mean = sum(vals) / len(vals)
            v[f'B1 gripper within 1 fps of baseline, {name}'] = B.verdict(
                base - mean, B.B1_GRIPPER_MAX_DROP_FPS, at_least=False)
    if toggles:
        n = len(toggles)
        reopen = sum(1 for t in toggles if t.get('reopen_suspected'))
        out['toggles'] = {'cycles': n, 'reopen_suspected': reopen,
                          'on_refused': sum(1 for t in toggles if not t.get('on_ok')),
                          'scene_max_gap_ms': B.summarize(
                              [t['scene_max_gap_ms'] for t in toggles
                               if t.get('scene_max_gap_ms') is not None])}
        v['B1 200 switches without a reopen (bridge evidence)'] = (
            'PENDING' if n < B.B1_TOGGLES else ('PASS' if reopen == 0 else 'FAIL'))
        out['toggles']['note'] = (
            'A recording-stream gap is the container-side arrival gap of /scene; judge it '
            'against the same measure from a run without toggling (the camera\'s own jitter).')
    out['verdicts'] = v
    return out


if __name__ == '__main__':
    sys.exit(main())

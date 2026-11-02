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

"""Bench gates B5 / B1: open the station camera exactly as the GUI does
(Windows host; environment STOPPED and the GUI CLOSED; dev only).

Imports the GUI's own ``app.win_camera.open_station_capture`` from this
checkout — never a copy — so the open is byte for byte the GUI's: the same
backend (``_capture_backend``: MSMF unless the DSHOW rollback is set), the
MJPG fourcc, and the method in ``app.constants.STATION_OPEN_METHOD``.

For each variant it records the negotiated read-back (fourcc, width, height,
fps as the driver reports them), the open time, the time to the first frame,
and the MEASURED rate and frame sizes over ``--duration`` seconds. With
``--variants all`` it opens every method in ``STATION_OPEN_METHODS``
(today ``fourcc_only`` and ``fourcc_size_fps``), each on a fresh capture,
by setting the GUI constant for that one open — the comparison B5 asks for.
``--probe`` also runs the GUI's enrollment probe (``probe_station_camera``).

Caveat (also in the README): on MSMF the fourcc read-back is garbage (the GUI
says so in ``open_capture``), so "MJPG" cannot be proven from it; the frame
size and the measured rate are what count.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bench_common as B  # noqa: E402
import wsl_docker as W  # noqa: E402

FRAME_FIELDS = ['variant', 'open_no', 'i', 't_s', 'ok', 'width', 'height']


def summarize_frames(frames, want=(1920, 1080)):
    """Pure: per-variant numbers from ``[(t_s, ok, w, h)]``."""
    good = [f for f in frames if f[1] and (f[2], f[3]) == want]
    sizes = {}
    for f in frames:
        if f[1]:
            key = f'{f[2]}x{f[3]}'
            sizes[key] = sizes.get(key, 0) + 1
    ts = [f[0] for f in good]
    gaps_ms = [g * 1000.0 for g in B.gaps(ts)]
    return {
        'frames_read': sum(1 for f in frames if f[1]),
        'read_failures': sum(1 for f in frames if not f[1]),
        'frames_native_size': len(good),
        'sizes_seen': sizes,
        'measured_fps_native': B.rate_hz(ts),
        'interval_ms': B.summarize(gaps_ms),
        'first_frame_s': next((f[0] for f in frames if f[1]), None),
    }


def open_once(win_camera, constants, index, method, duration_s):
    old = constants.STATION_OPEN_METHOD
    constants.STATION_OPEN_METHOD = method
    rec = {'method': method}
    t0 = time.perf_counter()
    try:
        cap, info = win_camera.open_station_capture(index)
    except Exception as e:  # noqa: BLE001 — CameraUnavailableError carries German text
        rec.update(open_s=time.perf_counter() - t0, error=str(e))
        return rec, []
    finally:
        constants.STATION_OPEN_METHOD = old
    rec['open_s'] = time.perf_counter() - t0
    rec['readback'] = info
    try:
        rec['backend'] = cap.getBackendName()
    except Exception:  # noqa: BLE001
        rec['backend'] = None
    frames = []
    t_read0 = time.perf_counter()
    try:
        while time.perf_counter() - t_read0 < duration_s:
            ok, frame = cap.read()
            t = time.perf_counter() - t_read0
            if ok and frame is not None:
                h, w = frame.shape[:2]
                frames.append((t, True, int(w), int(h)))
            else:
                frames.append((t, False, 0, 0))
                time.sleep(0.01)
    finally:
        cap.release()
    rec.update(summarize_frames(frames, (constants.STATION_NATIVE_WIDTH,
                                         constants.STATION_NATIVE_HEIGHT)))
    rec['b1_native_rate_ok'] = B.verdict(rec['measured_fps_native'], B.B1_CROP_MIN_FPS,
                                         at_least=True)
    return rec, frames


def build_parser():
    p = argparse.ArgumentParser(description='B5/B1: open the station camera exactly as the '
                                            'GUI does and measure what it delivers.')
    p.add_argument('--out', required=True, help='folder for the JSON/CSV (created)')
    p.add_argument('--index', type=int, default=None, help='OpenCV camera index (see --list)')
    p.add_argument('--list', action='store_true', help='list cameras as the GUI enumerates them')
    p.add_argument('--duration', type=float, default=10.0, help='seconds of reading per open')
    p.add_argument('--variants', choices=('gui', 'all'), default='gui',
                   help='gui = the GUI constant only; all = every STATION_OPEN_METHODS entry')
    p.add_argument('--repeat', type=int, default=1, help='opens per variant')
    p.add_argument('--pause', type=float, default=3.0, help='seconds between opens')
    p.add_argument('--probe', action='store_true', help="also run the GUI's enrollment probe")
    p.add_argument('--force', action='store_true',
                   help='run even though the GUI bridge answers (not recommended)')
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    W.gui_import_path()
    from app import constants, win_camera  # noqa: PLC0415 — the GUI's own modules
    import cv2  # noqa: PLC0415

    if args.list:
        for cam in win_camera.list_windows_cameras():
            print(f'{cam.index}: {cam.name}  {cam.location}')
        return 0
    if args.index is None:
        print('refused: --index is required (use --list first)')
        return 2
    code, _ = W.bridge_get('/station/status', timeout=0.5)
    if code is not None and not args.force:
        print('refused: the EduBotics GUI is running (its bridge on 127.0.0.1:8769 answers). '
              'Close the GUI (its camera bridge holds the camera) and run again.')
        return 2

    run_stamp = B.stamp()
    methods = ([constants.STATION_OPEN_METHOD] if args.variants == 'gui'
               else list(constants.STATION_OPEN_METHODS))
    flog = B.CsvLog(B.out_path(args.out, 'camera_open', run_stamp, '_frames.csv'), FRAME_FIELDS)
    results = []
    try:
        for method in methods:
            for n in range(1, args.repeat + 1):
                if results:
                    time.sleep(args.pause)
                print(f'opening index {args.index} with {method} (#{n}) …')
                rec, frames = open_once(win_camera, constants, args.index, method,
                                        args.duration)
                rec['open_no'] = n
                for i, f in enumerate(frames):
                    flog.write({'variant': method, 'open_no': n, 'i': i, 't_s': f[0],
                                'ok': int(f[1]), 'width': f[2], 'height': f[3]})
                results.append(rec)
                print(json.dumps({k: rec.get(k) for k in (
                    'method', 'open_s', 'readback', 'frames_native_size', 'sizes_seen',
                    'measured_fps_native', 'error')}, default=str))
        probe = None
        if args.probe:
            time.sleep(args.pause)
            r = win_camera.probe_station_camera(args.index)
            probe = {'ok': r.ok, 'frames': r.frames, 'fps': r.fps, 'width': r.width,
                     'height': r.height, 'error_de': r.error_de,
                     'criterion': f'{constants.STATION_PROBE_FRAMES} frames of exactly '
                                  f'{constants.STATION_NATIVE_WIDTH}x'
                                  f'{constants.STATION_NATIVE_HEIGHT} at >= '
                                  f'{constants.STATION_PROBE_MIN_FPS} fps'}
            print('enrollment probe:', probe)
    finally:
        flog.close()
    summary = {
        'gate': 'B5 (open method) / B1 (native rate)', 'index': args.index,
        'gui_station_open_method': constants.STATION_OPEN_METHOD,
        'backend_setting': win_camera.capture_backend_name(),
        'opencv_version': cv2.__version__, 'duration_s': args.duration,
        'opens': results, 'enrollment_probe': probe,
        'notes': ['On MSMF the fourcc read-back is not meaningful; frame size and measured '
                  'rate are what count.',
                  'B1 asks the station CROP for >= 29 fps under the running bridge; this '
                  'measures the native capture rate the crop is made from (closed GUI).'],
    }
    jp = B.out_path(args.out, 'camera_open', run_stamp, '.json')
    B.write_json(jp, summary)
    print(f'wrote {jp}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

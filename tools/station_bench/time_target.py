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

"""Bench gate B3: a time-changing target for the station camera (Windows
host, on a screen the station camera films; dev only).

Shows a QR code carrying the Windows clock (``EBT1:<time.time_ns()>``) taken
the moment it is drawn, redrawn every ``--period-ms``, plus the same time as
large text. ``container/arrival_rate.py --qr`` decodes it from the station
frames and computes capture-to-arrival latency as
``recv_container − (shown_windows + offset)``.

What that number includes: the display's own latency (draw → scan-out, about
one or two refreshes), the camera's exposure and pipeline, MSMF, the GUI's
encode and send, the ingest node and DDS. It overestimates the camera-side
part by the display latency and by up to one ``--period-ms`` (the code stays
on screen that long), so a PASS is safe and a near-miss should be re-run with
a shorter period. Esc or q closes the window.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bench_common as B  # noqa: E402


def render(cv2, np, text, size_px):
    enc = cv2.QRCodeEncoder.create()
    qr = enc.encode(text)                      # 1 px per module, black on white
    qr = cv2.resize(qr, (size_px, size_px), interpolation=cv2.INTER_NEAREST)
    pad = size_px // 8
    canvas = np.full((size_px + 2 * pad + 120, size_px + 2 * pad), 255, np.uint8)
    canvas[pad:pad + size_px, pad:pad + size_px] = qr
    ms = int(text[len(B.QR_PREFIX):]) // 1_000_000
    cv2.putText(canvas, f'{ms % 100000:05d} ms', (pad, pad + size_px + 90),
                cv2.FONT_HERSHEY_SIMPLEX, 2.5, 0, 6, cv2.LINE_AA)
    return canvas


def build_parser():
    p = argparse.ArgumentParser(description='B3 time target: a QR code of the Windows clock.')
    p.add_argument('--period-ms', type=float, default=33.0, help='redraw period')
    p.add_argument('--size', type=int, default=640, help='QR size in screen pixels')
    p.add_argument('--fullscreen', action='store_true')
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    if not hasattr(cv2, 'QRCodeEncoder'):
        print('refused: this OpenCV has no QRCodeEncoder (needs opencv-python >= 4.5.4)')
        return 2
    win = 'EduBotics station B3 time target'
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    if args.fullscreen:
        cv2.setWindowProperty(win, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    period = max(0.005, args.period_ms / 1000.0)
    next_t = time.monotonic()
    while True:
        img = render(cv2, np, B.qr_payload(time.time_ns()), args.size)
        cv2.imshow(win, img)
        next_t += period
        wait_ms = max(1, int((next_t - time.monotonic()) * 1000))
        key = cv2.waitKey(wait_ms) & 0xFF
        if key in (27, ord('q')):
            break
        if time.monotonic() > next_t + period:
            next_t = time.monotonic()
    cv2.destroyAllWindows()
    return 0


if __name__ == '__main__':
    sys.exit(main())

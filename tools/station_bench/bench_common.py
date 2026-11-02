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

"""Pure, stdlib-only helpers shared by the station bench tools (dev only).

Runs on the Windows bench PC and, copied flat next to a container tool, inside
the ``physical_ai_server`` container (Python 3.12). Nothing here touches a
camera, a socket, ROS or the robot: output files, statistics, the clock-offset
estimate, the parsers and the read-only guards. Every function is unit-tested
in ``robotis_ai_setup/tests/test_station_bench_toolkit.py`` without hardware.

The thresholds mirrored here are the plan's bench gates (v6.13 §12.2 / §12.3)
and the server's own check constants (``station/check.py``); the unit test
holds the mirrored check constants equal to the server's.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import time
from datetime import datetime
from typing import Iterable, Optional, Sequence

# ── The gates (plan v6.13 §12.2 / §12.3) ─────────────────────────────────────
B1_CROP_MIN_FPS = 29.0          # station crop (recording stream) >= 29 fps
B1_STUDIO_MIN_FPS = 14.0        # Studio stream sent under id 200 >= 14 fps
B1_GRIPPER_MAX_DROP_FPS = 1.0   # gripper within 1 fps of its no-station baseline
B1_TOGGLES = 200                # on/off switches without a reopen or a gap
B2_MIN_FPS = 14.0               # id-200 frames at the server >= 14 fps
B2_AGE_P95_S = 0.3              # arrival age p95 <= 0.3 s
B3_LATENCY_P95_S = 0.35         # capture-to-arrival p95 <= 0.35 s
B4_LENS_SOLVE_S = 3.0           # lens solve on 30 views <= 3 s
B4_HAT_MIN_POLLS_PER_S = 2.0    # the hat keeps >= 2 polls/s during the grasp
B4_GRASP_YAW_MAX_S = 2.0        # the grasp's yaw sampling ends within 2 s
B4_DETECT_P95_S = 0.2           # (v6.3 note) 1080p detect p95 <= 200 ms, informational
G8_MIN_PICKS = 50
G8_MIN_FIRST_ATTEMPT = 0.95
S1_CAMERA_CHECKS = 50
S1_ROBOT_CHECKS = 30
ENTRY_TYPICAL_S = 120.0         # entry plus both checks: typical <= 2 min

# The server's check thresholds (station/check.py; held equal by the unit test).
CAMERA_OK_RMS_MM = 3.0
CAMERA_OK_MAX_MM = 6.0
CAMERA_OK_CENTROID_MM = 3.0
CAMERA_OK_ROTATION_DEG = 0.5
ROBOT_OK_DXY_MM = 4.0
ROBOT_OK_DZ_MM = 3.0

# The Studio-stream lease (gui/app/roboter_studio_control.py::STATION_LEASE_S).
# A toggle's "on" phase must end well inside it, so the stream never relies on
# the lease to switch off.
STATION_LEASE_S = 10.0
TOGGLE_ON_MAX_S = 8.0

STATION_TOPIC = '/station_scene/image_raw/compressed'
SCENE_TOPIC = '/scene/image_raw/compressed'
GRIPPER_TOPIC = '/gripper/image_raw/compressed'
SIGNAL_TOPIC = '/edubotics/signal_status'

# Time-target QR payload (time_target.py → container/arrival_rate.py --qr).
QR_PREFIX = 'EBT1:'


# ── Output files ─────────────────────────────────────────────────────────────
def stamp(now: Optional[datetime] = None) -> str:
    """Local wall-clock stamp for file names, e.g. ``20261102-142530``."""
    return (now or datetime.now()).strftime('%Y%m%d-%H%M%S')


def iso_now() -> str:
    """Local time with milliseconds and the UTC offset, for CSV rows."""
    return datetime.now().astimezone().isoformat(timespec='milliseconds')


def out_path(out_dir: str, tool: str, run_stamp: str, suffix: str) -> str:
    """``<out_dir>/<tool>_<stamp><suffix>``; the folder is created."""
    if not out_dir:
        raise ValueError('an output folder is required (--out)')
    os.makedirs(out_dir, exist_ok=True)
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', tool)
    return os.path.join(out_dir, f'{safe}_{run_stamp}{suffix}')


class CsvLog:
    """Append-only CSV with a fixed header, flushed after every row so a
    crash or a Ctrl+C keeps everything written so far."""

    def __init__(self, path: str, fields: Sequence[str]):
        self.path = path
        self.fields = list(fields)
        self._fh = open(path, 'w', newline='', encoding='utf-8')
        self._w = csv.DictWriter(self._fh, fieldnames=self.fields, extrasaction='ignore')
        self._w.writeheader()
        self._fh.flush()
        self.rows = 0

    def write(self, row: dict) -> None:
        self._w.writerow({k: _cell(row.get(k)) for k in self.fields})
        self._fh.flush()
        self.rows += 1

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:  # noqa: BLE001
            pass


def _cell(v):
    if v is None:
        return ''
    if isinstance(v, float):
        if not math.isfinite(v):
            return ''
        return f'{v:.6g}'
    if isinstance(v, (list, tuple)):
        return ' '.join(str(_cell(x)) for x in v)
    return v


def write_json(path: str, obj) -> None:
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False, sort_keys=False, default=_json_default)
        fh.write('\n')
    os.replace(tmp, path)


def _json_default(o):
    try:
        return float(o)
    except Exception:  # noqa: BLE001
        return str(o)


# ── Statistics ───────────────────────────────────────────────────────────────
def percentile(values: Iterable[float], p: float) -> Optional[float]:
    """Linear-interpolated percentile (NumPy's default), None when empty."""
    xs = sorted(float(v) for v in values if v is not None and math.isfinite(float(v)))
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * (float(p) / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return xs[int(k)]
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summarize(values: Iterable[float]) -> dict:
    xs = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not xs:
        return {'n': 0, 'min': None, 'p50': None, 'p95': None, 'max': None, 'mean': None}
    return {
        'n': len(xs),
        'min': min(xs),
        'p50': percentile(xs, 50),
        'p95': percentile(xs, 95),
        'max': max(xs),
        'mean': sum(xs) / len(xs),
    }


def rate_hz(times_s: Sequence[float]) -> Optional[float]:
    """Events per second between the first and the last event (n-1 intervals)."""
    ts = sorted(times_s)
    if len(ts) < 2 or ts[-1] <= ts[0]:
        return None
    return (len(ts) - 1) / (ts[-1] - ts[0])


def gaps(times_s: Sequence[float]) -> list:
    ts = sorted(times_s)
    return [b - a for a, b in zip(ts, ts[1:])]


def verdict(value: Optional[float], limit: float, *, at_least: bool) -> str:
    """``PASS`` / ``FAIL`` against a gate, ``UNKNOWN`` when not measured."""
    if value is None or not math.isfinite(float(value)):
        return 'UNKNOWN'
    ok = value >= limit if at_least else value <= limit
    return 'PASS' if ok else 'FAIL'


# ── Windows ↔ WSL clock offset ───────────────────────────────────────────────
def estimate_clock_offset(samples: Sequence[tuple]) -> dict:
    """Offset of the remote (WSL / container) clock against the local
    (Windows) clock from round trips ``(t_send_ns, remote_ns, t_recv_ns)``,
    both local times from the SAME clock as the camera stamp (``time.time_ns``).

    The remote time was read somewhere inside [t_send, t_recv], so for each
    sample ``offset = remote - (t_send + t_recv) / 2`` within ±RTT/2. The
    sample with the smallest RTT gives the tightest bound; that is the
    estimate. ``remote_time = local_time + offset_ns``.
    """
    good = [(int(a), int(r), int(b)) for a, r, b in samples if int(b) >= int(a)]
    if not good:
        raise ValueError('no valid round trips')
    best = min(good, key=lambda s: s[2] - s[0])
    a, r, b = best
    rtts = [s[2] - s[0] for s in good]
    offsets = [s[1] - (s[0] + s[2]) / 2.0 for s in good]
    # The offsets of the fastest decile: their spread shows whether the bound is
    # honest (a stable clock) or the relay was asymmetric/noisy.
    order = sorted(range(len(good)), key=lambda i: rtts[i])
    top = [offsets[i] for i in order[:max(1, len(good) // 10)]]
    return {
        'offset_ns': int(round(r - (a + b) / 2.0)),
        'uncertainty_ns': int(math.ceil((b - a) / 2.0)),
        'rtt_min_ns': int(b - a),
        'rtt_p50_ns': percentile(rtts, 50),
        'n': len(good),
        'fast_decile_offset_spread_ns': (max(top) - min(top)) if top else None,
        'meaning': 'remote_ns = windows_ns + offset_ns (± uncertainty_ns)',
    }


def corrected_age_ns(recv_remote_ns: int, stamp_local_ns: int, offset_ns: int) -> int:
    """Arrival age with the stamp moved onto the receiver's clock:
    ``recv - (stamp + offset)``. ``stamp`` is the Windows capture time
    (``capture_ns``), ``recv`` the container clock."""
    return int(recv_remote_ns) - (int(stamp_local_ns) + int(offset_ns))


def parse_offset_json(text: str) -> tuple:
    """``(offset_ns, uncertainty_ns)`` from clock_offset.py's JSON."""
    d = json.loads(text)
    est = d.get('estimate', d)
    return int(est['offset_ns']), int(est['uncertainty_ns'])


# ── signal_status (physical_ai_server/signal_status.py, schema v1) ───────────
def parse_signal_status(text: str) -> dict:
    """``{'seq', 'recording', 'cameras': {name: {'hz', 'age_s', 'topic'}},
    'follower': {...}, 'leader': {...}}`` from the topic's JSON. Unknown or
    absent values stay None; never raises on a well-formed JSON object."""
    d = json.loads(text)
    if not isinstance(d, dict):
        raise ValueError('signal_status is not a JSON object')
    out = {'seq': d.get('seq'), 'recording': d.get('recording'), 'cameras': {},
           'follower': None, 'leader': None}
    for src in d.get('sources') or []:
        if not isinstance(src, dict):
            continue
        entry = {'hz': src.get('hz'), 'age_s': src.get('age_s'), 'topic': src.get('topic')}
        kind, name = src.get('kind'), src.get('name')
        if kind == 'camera' and isinstance(name, str):
            out['cameras'][name] = entry
        elif name in ('follower', 'leader'):
            out[name] = entry
    return out


# ── ros2 CLI response text ───────────────────────────────────────────────────
def parse_ros2_response_field(text: str, field: str) -> Optional[str]:
    """The value of ``field`` in a ``ros2 service call`` response repr, e.g.
    ``...Response(accepted=True, state='ACTIVE', ...)``. Strings are returned
    without quotes; None when the field is absent."""
    m = re.search(rf"\b{re.escape(field)}=('([^']*)'|\"([^\"]*)\"|([^,()\s]+))", text)
    if not m:
        return None
    for g in (m.group(2), m.group(3), m.group(4)):
        if g is not None:
            return g
    return None


# ── The toggle guard (run_logger.py --toggle) ────────────────────────────────
def toggle_guard(session_state: Optional[str], bridge: Optional[dict],
                 session_age_s: Optional[float] = None, max_age_s: float = 5.0) -> tuple:
    """``(allowed, reason)`` for switching the Studio stream from the bench.

    Allowed only with NO station session (``IDLE``, or ``OFF`` on a rig that is
    not a station: then the bridge answers enabled=false anyway), a fresh
    session answer, the station enabled on the GUI and the stream OFF (a page
    that holds a session renews ``on`` every 2 s; toggling under it would fight
    the page). Everything unknown refuses (fail closed).
    """
    if session_state is None:
        return False, 'station session state unknown (no answer from /station/session)'
    if session_age_s is None or session_age_s > max_age_s:
        return False, 'station session state is stale'
    if session_state not in ('IDLE',):
        return False, f'a station session is not idle (state {session_state})'
    if not isinstance(bridge, dict):
        return False, 'GUI bridge /station/status did not answer'
    if not bridge.get('enabled'):
        return False, 'the camera station is not enabled on the GUI'
    if bridge.get('stream') != 'off':
        return False, 'the Studio stream is on (a page holds it)'
    return True, ''


def toggle_timing_ok(on_s: float, off_s: float) -> tuple:
    if not (0.2 <= on_s <= TOGGLE_ON_MAX_S):
        return False, f'--on-s must be within 0.2..{TOGGLE_ON_MAX_S} s (lease {STATION_LEASE_S} s)'
    if off_s < 0.2:
        return False, '--off-s must be at least 0.2 s'
    return True, ''


REOPEN_HINT = 'neu geöffnet'   # camera_bridge's German reopen sentence


def reopen_suspected(error_de: str) -> bool:
    """The bridge's only reopen evidence: its German error mentions a reopen
    (``… wird neu geöffnet …``) or a wrong frame size."""
    e = error_de or ''
    return REOPEN_HINT in e or 'statt' in e


# ── Station checks (StationCheck status) ─────────────────────────────────────
CAMERA_RESULTS = ('OK', 'MOVED', 'WRONG_MAT', 'REFS_MISSING')
ROBOT_RESULTS = ('OK', 'ROBOT_MOVED', 'NO_DOT', 'NOT_VERTICAL')


def check_kind(d: dict) -> str:
    """``'robot'`` / ``'camera'`` / ``''`` for a StationCheck status answer.
    A robot result carries ``dot_id`` 1..4; a camera result the ref ids."""
    result = d.get('result') or ''
    if not result:
        return ''
    if int(d.get('dot_id') or 0) > 0 or result in ('ROBOT_MOVED', 'NO_DOT', 'NOT_VERTICAL'):
        return 'robot'
    if d.get('ref_ids') or result in ('MOVED', 'WRONG_MAT', 'REFS_MISSING'):
        return 'camera'
    return ''


def check_fingerprint(d: dict) -> tuple:
    """Identity of one stored check result. The server keeps ONE last check per
    session generation with no counter, so two consecutive checks with exactly
    equal values are indistinguishable (live metrics practically never are)."""
    def f(k):
        v = d.get(k)
        return round(float(v), 9) if v is not None else None
    return (d.get('generation'), d.get('result'), f('rms_mm'), f('max_mm'),
            f('centroid_mm'), f('rotation_deg'), int(d.get('dot_id') or 0),
            f('dot_dxy_mm'), f('dot_dz_mm'),
            tuple(round(float(x), 9) for x in (d.get('ref_residual_mm') or [])))


def half_threshold_ok(kind: str, d: dict) -> Optional[bool]:
    """S1's stricter criterion: an OK check whose every metric is at most half
    its threshold. None for a non-OK or unknown result."""
    if d.get('result') != 'OK':
        return None
    if kind == 'camera':
        return (float(d.get('rms_mm', 0)) <= CAMERA_OK_RMS_MM / 2
                and float(d.get('max_mm', 0)) <= CAMERA_OK_MAX_MM / 2
                and float(d.get('centroid_mm', 0)) <= CAMERA_OK_CENTROID_MM / 2
                and float(d.get('rotation_deg', 0)) <= CAMERA_OK_ROTATION_DEG / 2)
    if kind == 'robot':
        return (float(d.get('dot_dxy_mm', 0)) <= ROBOT_OK_DXY_MM / 2
                and float(d.get('dot_dz_mm', 0)) <= ROBOT_OK_DZ_MM / 2)
    return None


class CheckTally:
    """Running counts per kind: results, OK, OK at half the thresholds."""

    def __init__(self):
        self.counts = {'camera': {}, 'robot': {}}
        self.half = {'camera': 0, 'robot': 0}

    def add(self, kind: str, d: dict) -> None:
        if kind not in self.counts:
            return
        r = d.get('result') or '?'
        self.counts[kind][r] = self.counts[kind].get(r, 0) + 1
        if half_threshold_ok(kind, d):
            self.half[kind] += 1

    def total(self, kind: str) -> int:
        return sum(self.counts.get(kind, {}).values())

    def line(self) -> str:
        parts = []
        for kind, target in (('camera', S1_CAMERA_CHECKS), ('robot', S1_ROBOT_CHECKS)):
            c = self.counts[kind]
            parts.append(
                f"{kind}: {self.total(kind)} checks (S1 target {target}), OK {c.get('OK', 0)}, "
                f"OK at half the thresholds {self.half[kind]}, "
                + ', '.join(f'{k} {v}' for k, v in sorted(c.items()) if k != 'OK'))
        return ' | '.join(p.rstrip(', ') for p in parts)

    def summary(self) -> dict:
        out = {}
        for kind, target in (('camera', S1_CAMERA_CHECKS), ('robot', S1_ROBOT_CHECKS)):
            n = self.total(kind)
            ok = self.counts[kind].get('OK', 0)
            out[kind] = {
                'checks': n, 'by_result': dict(self.counts[kind]), 'ok': ok,
                'ok_at_half_thresholds': self.half[kind], 's1_target': target,
                # S1 (static): all OK and every metric at most half its threshold.
                's1_static_verdict': ('PENDING' if n < target else
                                      'PASS' if (ok == n and self.half[kind] == n) else 'FAIL'),
            }
        return out


# ── Session timeline (entry plus both checks) ────────────────────────────────
class EntryTimer:
    """From the first sight of a new session generation (ENTERING or ACTIVE)
    to the first ``verified`` in that generation. Resolution: the poll period."""

    def __init__(self):
        self._gen = None
        self._t0 = None
        self.durations = []

    def update(self, t_s: float, state: str, generation: int, verified: bool) -> Optional[float]:
        if state in ('ENTERING', 'ACTIVE') and generation != self._gen:
            self._gen, self._t0 = generation, t_s
        if state not in ('ENTERING', 'ACTIVE'):
            self._t0 = None
            self._gen = None
            return None
        if verified and self._t0 is not None and generation == self._gen:
            d = t_s - self._t0
            self.durations.append(d)
            self._t0 = None
            return d
        return None


# ── QR time target ───────────────────────────────────────────────────────────
def qr_payload(ns: int) -> str:
    return f'{QR_PREFIX}{int(ns)}'


def parse_qr_payload(text: str) -> Optional[int]:
    if not isinstance(text, str) or not text.startswith(QR_PREFIX):
        return None
    digits = text[len(QR_PREFIX):]
    if not digits.isdigit() or len(digits) > 20:
        return None
    return int(digits)


# ── Pick tally (G8) ──────────────────────────────────────────────────────────
PICK_KEYS = {'s': 'success', 'f': 'fail', 'u': 'unsafe'}


def pick_summary(outcomes: Sequence[str]) -> dict:
    n = len(outcomes)
    ok = sum(1 for o in outcomes if o == 'success')
    unsafe = sum(1 for o in outcomes if o == 'unsafe')
    rate = (ok / n) if n else None
    if n < G8_MIN_PICKS:
        v = 'PENDING'
    elif unsafe:
        v = 'FAIL'
    else:
        v = 'PASS' if rate >= G8_MIN_FIRST_ATTEMPT else 'FAIL'
    return {'picks': n, 'success': ok, 'fail': n - ok - unsafe, 'unsafe': unsafe,
            'first_attempt_rate': rate, 'g8_target_picks': G8_MIN_PICKS,
            'g8_target_rate': G8_MIN_FIRST_ATTEMPT, 'g8_verdict': v}


def monotonic_s() -> float:
    return time.monotonic()

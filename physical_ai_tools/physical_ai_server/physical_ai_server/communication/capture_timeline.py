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

"""The capture timeline of a recording (Aufnahme 2.0 round 5, spec §2.3, §3, §6.5).

Stdlib only, so every rule here is testable without ROS. Four pieces:

* ``ClockMap`` maps a source's header stamp onto the server's monotonic clock
  (running minimum of ``arrival - stamp`` over a window), so a sample is placed
  where it really happened even when its callback ran late.
* ``SourceHistory`` is a ring per source of ``(seq, arrival, t, msg)`` with its
  own lock. The Communicator pre-creates one per registered source.
* ``SlotSampler`` decides dataset frame k ONCE, from the histories: per camera
  the newest frame with ``t`` at or before the camera's own boundary (half a
  camera period after its tracked arrival phase), the observation instant ``R``
  is the newest chosen image's ``t``, state and action are the newest samples
  at or before ``R`` (the unstamped leader back-dated inside a delivery burst).
* ``TakeIntegrity`` collects the facts of one take for the English log line, the
  student-facing warning (F3: warn only) and the per-source gap rule (O2/C6).

A decision (``SlotSampler.decide_due``) is a dict::

    {'k': int, 'g': slot time, 'r': observation instant,
     'cams': {name: {'item': (seq, arrival, t, msg), 'repeat': bool, 'skip': int,
                     'locked': bool, 'rate': float | None, 'step': int}},
     'fol': (seq, arrival, t, msg), 'lea': (seq, arrival, t_backdated, msg) | None,
     'timed_out': bool, 'late': bool, 'decided_at': now,
     'lost_before': [slot times given up since the previous decision],
     'gaps': {source_key: (gap_s, t_from, t_to)}}

``source_key`` is ``'camera:<name>'``, ``'follower'``, ``'leader'`` or the
internal ``'follower_arrival'`` (the follower's delivery timeline, used only to
tell a server stall from a leader dropout).
"""

from __future__ import annotations

from collections import deque
import importlib.util
import math
import os
import threading
from typing import Any, Dict, Iterator, List, Optional, Tuple

# Ring sizes the Communicator pre-creates (spec §2.3): 64 camera frames are 2.1 s
# at 30 Hz; 256 joint messages are 2.56 s at 100 Hz.
CAMERA_HISTORY_LEN = 64
JOINT_HISTORY_LEN = 256

# The per-source gap rule (spec §6.2); lockstep-tested against
# ``signal_status.SOURCE_GAP_S``.
SOURCE_GAP_S = 0.25

# C5 warning thresholds (spec §6.5).
EXCESS_REPEAT_WARN_FRAC = 0.02
EXCESS_REPEAT_WARN_MIN = 2
ARM_DATA_LATE_WARN_S = 0.1

Item = Tuple[int, float, float, Any]


def _finite(x) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


class ClockMap:
    """Map a header stamp onto the server's monotonic clock.

    ``t = stamp + min(arrival - stamp)`` over the last ``WINDOW_S``, clamped to
    ``[arrival - MAX_LAG_S, arrival]`` and non-decreasing. A stamp of 0/None (or a
    non-finite one) means "no stamp": ``t = arrival``. A late callback only raises
    ``arrival - stamp``, which the minimum ignores; a forward clock step is adopted
    at once, a backward step is bounded by the clamp and forgotten after the window.
    """

    WINDOW_S = 2.0
    MAX_LAG_S = 0.5

    def __init__(self) -> None:
        self._dq: deque = deque()
        self._last_t: Optional[float] = None

    def map(self, arrival: float, stamp_s: Optional[float]) -> float:
        arrival = float(arrival)
        if not stamp_s or not _finite(stamp_s):
            t = arrival
        else:
            stamp_s = float(stamp_s)
            off = arrival - stamp_s
            dq = self._dq
            while dq and dq[-1][1] >= off:
                dq.pop()
            dq.append((arrival, off))
            while dq and dq[0][0] < arrival - self.WINDOW_S:
                dq.popleft()
            t = stamp_s + dq[0][1]
            t = min(arrival, max(t, arrival - self.MAX_LAG_S))
        if self._last_t is not None and t < self._last_t:
            t = self._last_t
        self._last_t = t
        return t


class SourceHistory:
    """Ring of ``(seq, arrival, t, msg)`` for one source, with its own lock.

    ``seq`` keeps counting across ``clear()`` so a consumer that remembers the
    last seq it used never mistakes a new message for an old one.
    """

    def __init__(self, maxlen: int) -> None:
        self._items: deque = deque(maxlen=int(maxlen))
        self._lock = threading.Lock()
        self._seq = 0
        self._clock = ClockMap()

    def append(self, arrival: float, stamp_s: Optional[float], msg: Any) -> int:
        with self._lock:
            self._seq += 1
            t = self._clock.map(arrival, stamp_s)
            self._items.append((self._seq, float(arrival), t, msg))
            return self._seq

    def snapshot(self) -> List[Item]:
        with self._lock:
            return list(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


def _median_period(items: List[Item], n: int = 200,
                   burst_frac: float = 0.25) -> Optional[float]:
    """The source's period: the median interval of its last ``n`` samples,
    ignoring the intervals inside a delivery burst (shorter than ``burst_frac``
    x the mean interval). A burst after a long stall can hold most of a short
    window's samples; the mean over the window is what it is regardless."""
    ts = [it[2] for it in items[-(n + 1):]]
    if len(ts) < 2 or ts[-1] <= ts[0]:
        return None
    mean = (ts[-1] - ts[0]) / (len(ts) - 1)
    d = sorted(b - a for a, b in zip(ts, ts[1:]) if b - a >= burst_frac * mean)
    return d[len(d) // 2] if d else mean


def _newest_le(items: List[Item], t_max: float, arrived_by: float) -> Optional[Item]:
    for it in reversed(items):
        if it[1] > arrived_by:
            continue
        if it[2] <= t_max:
            return it
    return None


def _backdated(items: List[Item], arrived_by: float, frac: float) -> Iterator[Tuple[Item, float]]:
    """Newest-first ``(item, t)`` of an UNSTAMPED periodic source (the leader).

    A sample that arrived less than ``frac`` x the source's median period before
    its successor belongs to a burst delivered late after a stall; it is
    back-dated to one period before its successor."""
    p = _median_period(items, burst_frac=frac)
    later_t = None
    later_arr = None
    for it in reversed(items):
        if it[1] > arrived_by:
            continue
        t = it[2]
        if p and later_t is not None and (later_arr - it[1]) < frac * p:
            t = min(t, later_t - p)
        yield it, t
        later_t, later_arr = t, it[1]


def _newest_le_backdated(items: List[Item], t_max: float, arrived_by: float,
                         frac: float) -> Optional[Item]:
    for it, t in _backdated(items, arrived_by, frac):
        if t <= t_max:
            return (it[0], it[1], t, it[3])
    return None


def _max_gap(times_newest_first: Iterator[float], cursor: Optional[float],
             upto: float) -> Optional[Tuple[float, float, float]]:
    """Largest interval between consecutive times in ``(cursor, upto]``, the
    cursor itself being the left end. None when nothing new was consumed."""
    ts: List[float] = []
    for t in times_newest_first:
        if t > upto:
            continue
        if cursor is not None and t <= cursor:
            break
        ts.append(t)
    if not ts:
        return None
    ts.reverse()
    seq = ([cursor] if cursor is not None else []) + ts
    best = None
    for a, b in zip(seq, seq[1:]):
        gap = b - a
        if best is None or gap > best[0]:
            best = (gap, a, b)
    return best


class SlotSampler:
    """Decide every dataset frame from timestamped history (spec §3.2).

    The constant names are the contract. ``decide_due`` is the only entry point.
    """

    ALPHA = 0.05
    HYST = 0.25
    LOCK_TOL = 0.06
    RATE_FRAMES = 61
    RATE_MIN_FRAMES = 20
    MARGIN_S = 0.10
    MAX_SLOTS_PER_TICK = 2
    CATCHUP_MAX_S = 0.25
    LEADER_BURST_FRAC = 0.25
    # a pause this long in a camera's own timeline (a resume, a dead cable)
    # restarts its rate estimate
    RATE_RESET_GAP_S = 0.5

    def __init__(self, fps: float, cameras: List[str]) -> None:
        self.fps = float(fps)
        if not (self.fps > 0 and math.isfinite(self.fps)):
            raise ValueError(f'fps must be positive, got {fps!r}')
        self.T = 1.0 / self.fps
        self.early = self.T * (1.0 + self.HYST)
        self.delay = self.early + self.MARGIN_S
        self.cameras = list(cameras)
        self.t0: Optional[float] = None
        self.next_k = 0
        self.phi: Dict[str, Optional[float]] = {c: None for c in self.cameras}
        self.locked: Dict[str, bool] = {c: False for c in self.cameras}
        self.last_seq: Dict[str, int] = {c: 0 for c in self.cameras}
        self._last_item: Dict[str, Optional[Item]] = {c: None for c in self.cameras}
        # slots given up: more than CATCHUP_MAX_S overdue, or not fillable at all
        self.lost = 0
        self._pending_lost: List[float] = []
        # gap cursors: the last consumed t per source key
        self._cursor: Dict[str, Optional[float]] = {}
        # per camera the t of its recent samples, independent of the ring's
        # capacity (the rate of a slot decided late must equal its on-time rate)
        self._tseries: Dict[str, deque] = {
            c: deque(maxlen=self.RATE_FRAMES + CAMERA_HISTORY_LEN) for c in self.cameras}
        self._tseries_seq: Dict[str, int] = {c: 0 for c in self.cameras}

    def slot_time(self, k: int) -> float:
        return self.t0 + k * self.T

    # ── per camera ────────────────────────────────────────────────────────────

    def _ingest(self, cams: Dict[str, List[Item]]) -> None:
        for c in self.cameras:
            items = cams.get(c) or []
            last = self._tseries_seq[c]
            new = []
            for it in reversed(items):
                if it[0] <= last:
                    break
                new.append(it)
            if not new:
                continue
            series = self._tseries[c]
            for it in reversed(new):
                if series and it[2] - series[-1] > self.RATE_RESET_GAP_S:
                    series.clear()
                series.append(it[2])
            self._tseries_seq[c] = new[0][0]

    def _cam_rate(self, c: str, upto: float) -> Optional[float]:
        """Rate from the camera's last ``RATE_FRAMES`` samples with ``t <= upto``.

        Bounded by the slot time, not by what has arrived when the slot is
        decided, so a slot decided late (after a stall) is decided exactly as
        it would have been on time."""
        series = self._tseries.get(c)
        if not series:
            return None
        end = len(series)
        while end > 0 and series[end - 1] > upto:
            end -= 1
        if end < self.RATE_MIN_FRAMES:
            return None
        first = series[max(0, end - self.RATE_FRAMES)]
        span = series[end - 1] - first
        if span <= 0:
            return None
        return (min(end, self.RATE_FRAMES) - 1) / span

    def _window(self, c: str, items: List[Item], g: float):
        """(f_c, locked, half, boundary) of camera c for slot time g."""
        f_c = self._cam_rate(c, g)
        half = self.T / 2.0
        # provisionally locked at the nominal rate while the camera's rate is unknown
        locked = f_c is None
        if f_c:
            m = round(f_c * self.T)
            tol = self.LOCK_TOL * (2.0 if self.locked[c] else 1.0) * max(1, m)
            if m >= 1 and abs(f_c * self.T - m) <= tol:
                locked = True
                half = 0.5 / f_c
        ph = self.phi[c] if (locked and self.phi[c] is not None) else 0.0
        return f_c, locked, half, g + ph + half

    # ── one slot ──────────────────────────────────────────────────────────────

    def _complete(self, k: int, now: float, cams, follower, leader) -> bool:
        g = self.slot_time(k)
        r = None
        for c in self.cameras:
            items = cams.get(c) or []
            if not items:
                return False
            _f, _l, _h, b = self._window(c, items, g)
            if items[-1][2] <= b:
                return False   # nothing beyond the boundary yet: a frame may be in flight
            it = _newest_le(items, b, now)
            if it is not None:
                r = it[2] if r is None else max(r, it[2])
        if r is None:
            return True
        for hist in (follower, leader):
            if hist is None:
                continue
            if not hist or hist[-1][2] <= r:
                return False
        return True

    def _decide(self, k: int, now: float, cams, follower, leader) -> Optional[dict]:
        g = self.slot_time(k)
        chosen = {}
        for c in self.cameras:
            items = cams.get(c) or []
            f_c, locked, half, b = self._window(c, items, g)
            self.locked[c] = locked
            it = _newest_le(items, b, now)
            if it is None:
                return None
            last = self._last_item[c]
            if last is not None and it[0] < last[0]:
                # never step back in time: a boundary that moved back by the
                # phase wrap repeats the last frame instead
                it = last
            chosen[c] = (it, locked, half, f_c)
        r = max(v[0][2] for v in chosen.values()) if chosen else g
        fol = _newest_le(follower or [], r, now)
        if fol is None:
            return None
        lea = None
        if leader is not None:
            lea = _newest_le_backdated(leader, r, now, self.LEADER_BURST_FRAC)
            if lea is None:
                return None
        out = {'k': k, 'g': g, 'r': r, 'cams': {}, 'fol': fol, 'lea': lea}
        gaps: Dict[str, Tuple[float, float, float]] = {}
        for c, (it, locked, half, f_c) in chosen.items():
            seq = it[0]
            last_seq = self.last_seq[c]
            rep = seq <= last_seq
            step = max(1, round(f_c * self.T)) if f_c else 1
            skip = max(0, seq - last_seq - step) if last_seq else 0
            out['cams'][c] = {'item': it, 'repeat': rep, 'skip': skip,
                              'locked': locked, 'rate': f_c, 'step': step}
            key = f'camera:{c}'
            gap = _max_gap((x[2] for x in reversed(cams.get(c) or []) if x[1] <= now),
                           self._cursor.get(key), it[2])
            if gap is not None:
                gaps[key] = gap
                self._cursor[key] = it[2]
            if not rep:
                e = it[2] - g
                P = 2.0 * half
                if self.phi[c] is None or not locked:
                    self.phi[c] = (e + P / 2) % P - P / 2
                else:
                    # second-order tracking: predict the per-slot phase advance
                    # from the measured rate, then correct
                    if f_c:
                        m_ = max(1, round(f_c * self.T))
                        self.phi[c] += (m_ / f_c - self.T)
                    self.phi[c] += self.ALPHA * (e - self.phi[c])
                    if self.phi[c] > P * (0.5 + self.HYST):
                        self.phi[c] -= P
                    elif self.phi[c] < -P * (0.5 + self.HYST):
                        self.phi[c] += P
                self.last_seq[c] = seq
                self._last_item[c] = it
        gap = _max_gap((x[2] for x in reversed(follower) if x[1] <= now),
                       self._cursor.get('follower'), fol[2])
        if gap is not None:
            gaps['follower'] = gap
            self._cursor['follower'] = fol[2]
        gap = _max_gap((x[1] for x in reversed(follower) if x[1] <= now),
                       self._cursor.get('follower_arrival'), fol[1])
        if gap is not None:
            gaps['follower_arrival'] = gap
            self._cursor['follower_arrival'] = fol[1]
        if lea is not None:
            gap = _max_gap((t for _it, t in _backdated(leader, now, self.LEADER_BURST_FRAC)),
                           self._cursor.get('leader'), lea[2])
            if gap is not None:
                gaps['leader'] = gap
                self._cursor['leader'] = lea[2]
        out['gaps'] = gaps
        return out

    # ── the entry point ───────────────────────────────────────────────────────

    def decide_due(self, now: float, cams: Dict[str, List[Item]], follower: List[Item],
                   leader: Optional[List[Item]]) -> List[dict]:
        """Decide every slot that is due and PROVABLY complete.

        The first call anchors ``t0`` and returns ``[]``. Slots more than
        ``CATCHUP_MAX_S`` past their deadline are given up (``lost``). Then, in
        order and at most ``MAX_SLOTS_PER_TICK``, a slot with ``G + early <= now``
        is decided once every camera has a sample beyond its boundary and the
        follower and leader each have one beyond ``R`` (streams arrive in order,
        so nothing earlier can still come), or ``MARGIN_S`` after ``G + early``
        (timeout: a silent source repeats). ``leader=None`` means the session
        reads no leader."""
        now = float(now)
        self._ingest(cams)
        if self.t0 is None:
            self.t0 = now
            return []
        out: List[dict] = []
        while now - (self.slot_time(self.next_k) + self.delay) > self.CATCHUP_MAX_S:
            self.lost += 1
            self._pending_lost.append(self.slot_time(self.next_k))
            self.next_k += 1
        while len(out) < self.MAX_SLOTS_PER_TICK:
            k = self.next_k
            g = self.slot_time(k)
            if g + self.early > now:
                break
            timed_out = now >= g + self.delay
            if not timed_out and not self._complete(k, now, cams, follower, leader):
                break
            d = self._decide(k, now, cams, follower, leader)
            self.next_k += 1
            if d is None:
                # nothing to fill the slot with (no sample at or before it yet)
                self.lost += 1
                self._pending_lost.append(g)
                continue
            d['timed_out'] = timed_out
            d['late'] = (now - g) > self.delay + self.T
            d['decided_at'] = now
            d['lost_before'] = self._pending_lost
            self._pending_lost = []
            out.append(d)
        return out


def _load_texts():
    """``record_texts_de`` (the one module of German record sentences).

    Imported by package name in the image; by file path when this module was
    loaded on its own (the deps-free test loaders give the package no
    ``__path__``)."""
    try:
        from physical_ai_server.data_processing import record_texts_de
        return record_texts_de
    except ImportError:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'data_processing', 'record_texts_de.py')
        spec = importlib.util.spec_from_file_location('_edubotics_record_texts_de', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


class TakeIntegrity:
    """The facts of one take (spec §6.5; F3: warn only, never re-records).

    ``reset(start_mono)`` at every new take (``start_mono`` = the take's run
    entry on the monotonic clock; without it the take starts one period before
    its first frame), ``add(decision)`` per recorded frame, then ``warning_de(n)``,
    ``as_log_dict()`` and the gap rule (``max_gap_s`` / ``gap_source()``)."""

    def __init__(self, cameras: List[str], fps: float,
                 gap_threshold_s: float = SOURCE_GAP_S) -> None:
        self.cameras = list(cameras)
        self.fps = float(fps)
        self.T = 1.0 / self.fps
        self.gap_threshold_s = float(gap_threshold_s)
        self.reset()

    def reset(self, start_mono: Optional[float] = None) -> None:
        self.start = None if start_mono is None else float(start_mono)
        self.frames = 0
        self.lost = 0
        self.late = 0
        self.timed_out = 0
        self.repeats = {c: 0 for c in self.cameras}
        self.skips = {c: 0 for c in self.cameras}
        self._rate_sum = {c: 0.0 for c in self.cameras}
        self._rate_n = {c: 0 for c in self.cameras}
        self._last_seq: Dict[str, Optional[int]] = {c: None for c in self.cameras}
        self.max_state_age = 0.0
        self.max_action_age = 0.0
        self._spreads: List[float] = []
        self._max_gap: Dict[str, Tuple[float, float, float]] = {}
        self._intervals: Dict[str, List[Tuple[float, float]]] = {}
        self._last_t: Dict[str, float] = {}
        self._last_ref: Optional[float] = None
        self._last_decided: Optional[float] = None

    # ── input ─────────────────────────────────────────────────────────────────

    def _note_gap(self, key: str, a: float, b: float) -> None:
        a = max(a, self.start)
        gap = b - a
        if gap <= 0:
            return
        best = self._max_gap.get(key)
        if best is None or gap > best[0]:
            self._max_gap[key] = (gap, a, b)
        if gap >= self.gap_threshold_s / 2.0:
            lst = self._intervals.setdefault(key, [])
            if len(lst) < 64:
                lst.append((a, b))

    def _note_last(self, key: str, t: float) -> None:
        prev = self._last_t.get(key)
        self._last_t[key] = t if prev is None else max(prev, t)

    def add(self, d: dict, late: Optional[bool] = None) -> None:
        if self.start is None:
            self.start = float(d['g']) - self.T
        self.frames += 1
        self.lost += sum(1 for g in d.get('lost_before') or () if g >= self.start)
        if late if late is not None else d.get('late'):
            self.late += 1
        if d.get('timed_out'):
            self.timed_out += 1
        ts = []
        for c, v in d['cams'].items():
            if c not in self.repeats:
                continue
            seq = v['item'][0]
            last = self._last_seq[c]
            if last is not None:
                if seq <= last:
                    self.repeats[c] += 1
                else:
                    self.skips[c] += max(0, seq - last - int(v.get('step') or 1))
            self._last_seq[c] = seq if last is None else max(last, seq)
            rate = v.get('rate')
            if rate:
                self._rate_sum[c] += float(rate)
                self._rate_n[c] += 1
            ts.append(v['item'][2])
            self._note_last(f'camera:{c}', v['item'][2])
        if len(ts) >= 2:
            self._spreads.append(max(ts) - min(ts))
        r = float(d['r'])
        fol = d.get('fol')
        if fol is not None:
            self.max_state_age = max(self.max_state_age, r - fol[2])
            self._note_last('follower', fol[2])
            self._note_last('follower_arrival', fol[1])
        lea = d.get('lea')
        if lea is not None:
            self.max_action_age = max(self.max_action_age, r - lea[2])
            self._note_last('leader', lea[2])
        for key, (_gap, a, b) in (d.get('gaps') or {}).items():
            self._note_gap(key, a, b)
        self._last_ref = max(float(d['g']), r)
        self._last_decided = float(d.get('decided_at', self._last_ref))

    # ── facts ─────────────────────────────────────────────────────────────────

    def rate_hz(self, camera: str) -> Optional[float]:
        n = self._rate_n.get(camera, 0)
        return self._rate_sum[camera] / n if n else None

    def excess_repeats(self, camera: str) -> float:
        rate = self.rate_hz(camera)
        honest = 0.0 if rate is None else max(0.0, self.frames * (1.0 - rate / self.fps))
        return self.repeats.get(camera, 0) - honest

    def spread_p95_s(self) -> Optional[float]:
        if not self._spreads:
            return None
        s = sorted(self._spreads)
        return s[min(len(s) - 1, int(math.ceil(0.95 * len(s))) - 1)]

    def _open_gap(self, key: str) -> Optional[Tuple[float, float]]:
        last = self._last_t.get(key)
        if last is None:
            return None
        ref = self._last_decided if key == 'follower_arrival' else self._last_ref
        if ref is None or ref <= last:
            return None
        return (max(last, self.start), ref)

    def _source_keys(self) -> List[str]:
        keys = [f'camera:{c}' for c in self.cameras]
        for key in ('follower', 'leader'):
            if key in self._last_t or key in self._max_gap:
                keys.append(key)
        return keys

    @property
    def max_gap_s(self) -> Dict[str, float]:
        """Per source (``'camera:<name>'``, ``'follower'``, ``'leader'``) the
        largest interval between consecutive ``t`` inside the take, an open gap
        at the end included."""
        out = {}
        for key in self._source_keys():
            best = self._max_gap.get(key, (0.0, 0.0, 0.0))[0]
            og = self._open_gap(key)
            if og is not None:
                best = max(best, og[1] - og[0])
            out[key] = best
        return out

    def _intervals_with_open(self, key: str, min_len: float) -> List[Tuple[float, float]]:
        out = [iv for iv in self._intervals.get(key, ()) if iv[1] - iv[0] >= min_len]
        og = self._open_gap(key)
        if og is not None and og[1] - og[0] >= min_len:
            out.append(og)
        return out

    def gap_source(self) -> Optional[Tuple[str, Optional[str]]]:
        """The source whose gap makes this take a re-record (O2/C6), or None.

        ``('camera', name)``, ``('follower', None)`` or ``('leader', None)``. A
        leader gap counts only when the follower's delivery shows no gap
        overlapping it: a stall of the server process delays both, a real
        leader dropout does not."""
        thr = self.gap_threshold_s
        gaps = self.max_gap_s
        for c in self.cameras:
            if gaps.get(f'camera:{c}', 0.0) >= thr:
                return ('camera', c)
        if gaps.get('follower', 0.0) >= thr:
            return ('follower', None)
        stalls = self._intervals_with_open('follower_arrival', thr / 2.0)
        for a, b in self._intervals_with_open('leader', thr):
            if not any(fa < b and a < fb for fa, fb in stalls):
                return ('leader', None)
        return None

    def warning_de(self, episode: int) -> str:
        """The German per-take warning (C5), '' for a clean take: a lost slot,
        a camera's excess repeats above 2 % of the frames (and at least 2), or
        state/action data older than 0.1 s at the observation instant."""
        texts = _load_texts()
        if self.lost > 0:
            return texts.take_lost_slots_de(episode, self.lost, self.frames + self.lost,
                                            self.lost * self.T)
        for c in self.cameras:
            excess = self.excess_repeats(c)
            if (excess >= EXCESS_REPEAT_WARN_MIN
                    and excess > EXCESS_REPEAT_WARN_FRAC * self.frames):
                pct = 100.0 * self.repeats[c] / max(1, self.frames)
                return texts.take_excess_repeats_de(episode, c, pct)
        late = max(self.max_state_age, self.max_action_age)
        if late > ARM_DATA_LATE_WARN_S:
            return texts.take_arm_late_de(episode, late * 1000.0)
        return ''

    def as_log_dict(self) -> dict:
        """English facts for the per-take log line (``capture take ep=… {json}``)."""
        spread = self.spread_p95_s()
        return {
            'frames': self.frames,
            'lost': self.lost,
            'late': self.late,
            'timed_out': self.timed_out,
            'cameras': {
                c: {
                    'rep': self.repeats[c],
                    'skip': self.skips[c],
                    'excess_rep': round(self.excess_repeats(c), 2),
                    'hz': None if self.rate_hz(c) is None else round(self.rate_hz(c), 2),
                }
                for c in self.cameras
            },
            'state_max_s': round(self.max_state_age, 4),
            'action_max_s': round(self.max_action_age, 4),
            'spread_p95_s': None if spread is None else round(spread, 4),
            'max_gap_s': {k: round(v, 4) for k, v in self.max_gap_s.items()},
        }

    # the prototype's name
    as_dict = as_log_dict

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

"""`/edubotics/signal_status` — what the recorder's sources are doing (Aufnahme 2.0).

The node publishes FACTS, never verdicts: for every configured source (each
camera, the follower, the leader when the profile has one) the measured
callback rate over ~2 s and the age of its last message, plus the free bytes on
the dataset filesystem with both disk floors. The Aufnahme page decides what is
slow or stalled, because only it knows the fps being recorded; an older image
without this topic simply shows no badge and no banner.

Schema v1 (``std_msgs/String``, JSON in ``.data``; published latched AND at
1 Hz, always — idle, recording, collision)::

    {"v": 1, "seq": 42, "uptime_s": 12.0, "recording": false,
     "sources": [{"kind": "camera", "name": "scene", "topic": "/scene/image_raw",
                  "hz": 11.2, "age_s": 0.08}, ...],
     "disk": {"free_bytes": 52345678901, "start_floor_bytes": 3000000000,
              "critical_floor_bytes": 1000000000}}

``hz`` is null while not measurable yet; ``age_s`` is null while nothing has
arrived since boot; ``disk`` is null when statvfs failed.

Round 5 adds ONE trailing key, ``"ingest": {"alive": bool, "age_s": float}``:
the liveness of the sensor-ingest thread (``alive`` = its loop ran within the
last second; ``age_s`` null before it ran once). ``v`` stays 1 and the keys
before it keep their order; a page that does not know the key ignores it.

The two recorder thresholds live here too, because the page judges „steht“ with
the same 2 s (``utils/signalStatus.js::STALLED_AFTER_S``, lockstep-tested):
``SOURCE_STOPPED_S`` (a required source silent this long ends the session like
„Beenden“) and ``SOURCE_GAP_S`` (a gap this long inside a take re-records it).

Disk floors (measured, see the Aufnahme 2.0 spec §2.5): two cameras record at
about 1 MB/s (worst case 3.6 MB/s), and a save re-muxes a 200 MB video chunk
through /tmp (~0.45 GB transient). The CRITICAL floor covers that transient
plus a whole 120 s episode at the worst rate; the START floor adds about half an
hour of recording on top. In WSL2 the container sees the free space of the
distro's virtual disk, not of C: (a known limitation).

Stdlib only, so the schema, the rate maths and the German sentences are
testable without ROS.
"""

from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import shutil
from typing import Dict, Iterable, List, Optional

TOPIC = '/edubotics/signal_status'
SCHEMA_VERSION = 1
PUBLISH_PERIOD_S = 1.0
RATE_WINDOW_S = 2.0
DISK_START_FLOOR_BYTES = 3_000_000_000
DISK_CRITICAL_FLOOR_BYTES = 1_000_000_000
DISK_CHECK_INTERVAL_S = 1.0

# Recorder thresholds (spec-r5 §2.2, final spec §6.1/§6.2).
SOURCE_STOPPED_S = 2.0
SOURCE_GAP_S = 0.25

# The sensor-ingest thread counts as alive when its loop ran this recently.
INGEST_ALIVE_S = 1.0

# A rate over less than this many seconds is too noisy to publish.
_MIN_RATE_SPAN_S = 0.9
_SNAPSHOT_HISTORY = 16


def disk_free_bytes(path) -> Optional[int]:
    """Free bytes on the filesystem holding ``path`` (or its nearest existing
    ancestor — the dataset root may not exist before the first recording).
    None when the filesystem cannot be asked."""
    try:
        p = Path(path)
        while not p.exists():
            parent = p.parent
            if parent == p:
                break
            p = parent
        return int(shutil.disk_usage(str(p)).free)
    except (OSError, ValueError, TypeError):
        return None


def format_gb_de(n: int) -> str:
    """Decimal gigabytes, one decimal, German comma: 3_000_000_000 -> '3,0 GB'."""
    return f'{n / 1e9:.1f}'.replace('.', ',') + ' GB'


def disk_start_refusal_de(free: int) -> str:
    return (f'Nur noch {format_gb_de(free)} frei. Zum Aufnehmen sind mindestens '
            f'{format_gb_de(DISK_START_FLOOR_BYTES)} nötig. Lösche alte Datensätze '
            f'im Tab Daten.')


def disk_critical_stop_de(free: int) -> str:
    # Phase-neutral on purpose: the stop can come in a warm-up, a reset or a run
    # shorter than a second, where nothing new was saved.
    return (f'Der Speicher ist fast voll (nur noch {format_gb_de(free)} frei), '
            f'deshalb wurde die Aufnahme beendet. Bereits gespeicherte Episoden '
            f'bleiben erhalten. Lösche alte Datensätze im Tab Daten.')


class RateTracker:
    """Callback rates from monotonically growing per-source counters.

    ``update(now, counts)`` records one snapshot and returns ``{id: hz}`` for
    every id in ``counts``, measured against the OLDEST snapshot not older than
    ``window_s + 0.5`` s that holds that id. ``hz`` is None while the span is
    under 0.9 s or the id has no history; a source that stayed silent measures
    0.0. A counter that went DOWN (the Communicator was rebuilt) drops that
    id's history.
    """

    def __init__(self, window_s: float = RATE_WINDOW_S):
        self.window_s = float(window_s)
        self._snapshots: deque = deque(maxlen=_SNAPSHOT_HISTORY)

    def update(self, now: float, counts: Dict[str, int]) -> Dict[str, Optional[float]]:
        now = float(now)
        while self._snapshots and now - self._snapshots[0][0] > self.window_s + 1.0:
            self._snapshots.popleft()
        in_window = [(t, snap) for t, snap in self._snapshots
                     if now - t <= self.window_s + 0.5]
        rates: Dict[str, Optional[float]] = {}
        for source_id, count in counts.items():
            rates[source_id] = None
            reference = next(
                ((t, snap[source_id]) for t, snap in in_window if source_id in snap),
                None)
            if reference is None:
                continue
            ref_t, ref_count = reference
            delta = int(count) - int(ref_count)
            if delta < 0:
                for _, snap in self._snapshots:
                    snap.pop(source_id, None)
                continue
            span = now - ref_t
            if span < _MIN_RATE_SPAN_S:
                continue
            rates[source_id] = round(delta / span, 1)
        self._snapshots.append((now, {k: int(v) for k, v in counts.items()}))
        return rates


def source_entries(counters: Iterable[dict], rates: Dict[str, Optional[float]],
                   now: float, *, include_leader: bool) -> List[dict]:
    """Schema-v1 ``sources[]`` from Communicator.source_counters() + rates.

    The leader is listed only when the robot profile has one (the server cannot
    know about the runtime follower-only flip; the page asks the bridge)."""
    entries = []
    for counter in counters:
        kind = counter.get('kind')
        if kind == 'leader' and not include_leader:
            continue
        last = counter.get('last_mono')
        age = None if last is None else round(max(0.0, float(now) - float(last)), 2)
        hz = rates.get(counter.get('id'))
        entries.append({
            'kind': kind,
            'name': counter.get('name'),
            'topic': counter.get('topic'),
            'hz': None if hz is None else round(float(hz), 1),
            'age_s': age,
        })
    return entries


def ingest_status(last_alive_mono: Optional[float], now: float) -> dict:
    """``{"alive": bool, "age_s": float | None}`` of the sensor-ingest loop."""
    if last_alive_mono is None:
        return {'alive': False, 'age_s': None}
    age = max(0.0, float(now) - float(last_alive_mono))
    return {'alive': age < INGEST_ALIVE_S, 'age_s': round(age, 2)}


def build_payload(*, seq: int, uptime_s: float, recording: bool,
                  sources: Iterable[dict], free_bytes: Optional[int],
                  ingest: Optional[dict] = None) -> dict:
    """Exactly schema v1 (key order included), plus the optional trailing
    ``ingest`` key when the caller knows the sensor thread's liveness."""
    disk = None
    if free_bytes is not None:
        disk = {
            'free_bytes': int(free_bytes),
            'start_floor_bytes': DISK_START_FLOOR_BYTES,
            'critical_floor_bytes': DISK_CRITICAL_FLOOR_BYTES,
        }
    payload = {
        'v': SCHEMA_VERSION,
        'seq': int(seq),
        'uptime_s': round(float(uptime_s), 1),
        'recording': bool(recording),
        'sources': [
            {
                'kind': src.get('kind'),
                'name': src.get('name'),
                'topic': src.get('topic'),
                'hz': src.get('hz'),
                'age_s': src.get('age_s'),
            }
            for src in sources
        ],
        'disk': disk,
    }
    if ingest is not None:
        payload['ingest'] = {
            'alive': bool(ingest.get('alive')),
            'age_s': ingest.get('age_s'),
        }
    return payload


def encode_payload(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':'))

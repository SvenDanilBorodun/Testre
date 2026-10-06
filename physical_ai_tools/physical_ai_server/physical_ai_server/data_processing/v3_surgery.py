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

"""Lossless episode-level assembly of LeRobot v3.0 datasets (Daten 2.0, spec §D1).

Delete, split, merge and „Beide behalten" are ONE primitive, :func:`assemble`:
an output that holds exactly the given ``(Source, episode)`` parts, built by
STREAM COPY at episode boundaries — data rows copied verbatim (three index
columns renumbered), video packets cut out in decode order and re-muxed without
a re-encode. Proven bit-exact against LeRobot's own loader at the train-time
decode tolerance (spec P1–P4, P18–P21).

Safety rules this module enforces before anything is written (R-2, P19): a
dataset is only opened when its ``info.json`` carries exactly LeRobot's default
``data_path``/``video_path`` templates and every feature key matches
``FEATURE_KEY_RE``; every file it will make us open is built from the DEFAULT
templates with integer indices and confined under the dataset root (symlinks
followed) — a path a dataset names itself is never opened. Every output is
written with the default templates.

Only LeRobot's PUBLIC API is used (``LeRobotDatasetMetadata.create /
save_episode_tasks / save_episode / finalize / get_task_index``,
``LeRobotDataset``, ``video_utils.decode_video_frames``), and LeRobot is
imported function-locally, so :func:`cut_episode`, :func:`check_layout`,
:func:`confine` and :class:`Source` import with ``av``/``pyarrow``/``numpy``
only — the Daten sidecar uses them and must not pay LeRobot's import (nor reach
``torch``). The image pins :data:`DEFAULT_DATA_PATH`/:data:`DEFAULT_VIDEO_PATH`
equal to ``lerobot.datasets.utils``'s (the thin Dockerfile's Daten gate).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time

import av
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

# R-2: only LeRobot 0.5.1's DEFAULT templates are accepted and every output is
# written with them. The Dockerfile's Daten gate asserts equality in the image.
DEFAULT_DATA_PATH = 'data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet'
DEFAULT_VIDEO_PATH = 'videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4'
# = daten/contract.py FEATURE_KEY_RE / MAX_INDEX (lockstep-tested).
FEATURE_KEY_RE = re.compile(r'^[A-Za-z0-9._-]+$')
MAX_INDEX = 10 ** 6
TASKS_PATH = 'meta/tasks.parquet'
INFO_PATH = 'meta/info.json'
EPISODES_DIR = 'meta/episodes'
# Columns a copy renumbers; every OTHER data column is payload and copied verbatim.
INDEX_COLUMNS = ('index', 'episode_index', 'task_index')
# contract.MERGE_CHECKS, in that order.
MERGE_CHECKS = ('version', 'robot', 'fps', 'cameras', 'joints', 'video', 'stats')
_VIDEO_INFO_KEYS = ('video.codec', 'video.pix_fmt', 'video.height', 'video.width', 'video.fps')
# LeRobot's own defaults for a source info.json that omits a size setting.
_DEFAULT_CHUNKS_SIZE = 1000
_DEFAULT_DATA_FILES_MB = 100
_DEFAULT_VIDEO_FILES_MB = 200
# verify() decodes the first and the last frame of every output episode at this
# tolerance: the train-time FrameTimestampError condition (P1).
DECODE_TOLERANCE_S = 1e-4


class SurgeryError(RuntimeError):
    """A refusal of the engine. ``code`` is one of ``unsupported``, ``layout``,
    ``unaligned``, ``incompatible``, ``exists``, ``verify_failed``, ``broken``
    (contract.JOB_FAIL_CODES); ``detail`` is English, for the log only."""

    def __init__(self, code, detail=''):
        super().__init__(f'{code}: {detail}' if detail else code)
        self.code = code
        self.detail = detail


def _noop_progress(stage, done, total):
    return None


# ── layout + confinement (R-2) ──────────────────────────────────────────────────

def confine(root, rel):
    """The path ``rel`` under ``root``, symlinks followed and judged; else
    ``SurgeryError('unsupported')`` (P19: a dataset never makes us open a path
    outside itself)."""
    base = Path(root).resolve()
    p = (base / rel).resolve()
    if p != base and base not in p.parents:
        raise SurgeryError('unsupported', 'path outside the dataset')
    return p


def video_keys_of(info) -> list:
    return [k for k, v in (info.get('features') or {}).items()
            if isinstance(v, dict) and v.get('dtype') == 'video']


def check_layout(info, episodes):
    """``info.json`` names LeRobot's default templates, every feature key is
    safe, every chunk/file index of every episode row is an int in
    ``[0, MAX_INDEX)``. Raises ``SurgeryError('unsupported')``."""
    if not isinstance(info, dict) or info.get('data_path') != DEFAULT_DATA_PATH:
        raise SurgeryError('unsupported', 'data_path')
    features = info.get('features')
    if not isinstance(features, dict) or not features:
        raise SurgeryError('unsupported', 'features')
    if not all(isinstance(k, str) and FEATURE_KEY_RE.fullmatch(k) for k in features):
        raise SurgeryError('unsupported', 'feature key')
    videos = video_keys_of(info)
    if videos and info.get('video_path') != DEFAULT_VIDEO_PATH:
        raise SurgeryError('unsupported', 'video_path')
    cols = ['data/chunk_index', 'data/file_index'] + [
        f'videos/{k}/{c}' for k in videos for c in ('chunk_index', 'file_index')]
    for r in episodes:
        for c in cols:
            v = r.get(c)
            if type(v) is not int or not 0 <= v < MAX_INDEX:
                raise SurgeryError('unsupported', c)


def read_info(root) -> dict:
    """``meta/info.json`` (confined); raises ``SurgeryError('layout')``."""
    try:
        info = json.loads(confine(root, INFO_PATH).read_text(encoding='utf-8'))
    except SurgeryError:
        raise
    except Exception as e:  # noqa: BLE001 — every read error is a layout error (G-15)
        raise SurgeryError('layout', type(e).__name__) from e
    if not isinstance(info, dict):
        raise SurgeryError('layout', 'info.json is not an object')
    return info


def _read_task_table(path) -> dict:
    """``meta/tasks.parquet`` → {task_index: task string}. LeRobot writes it from
    a pandas frame indexed by the task string, which pyarrow returns as the
    column ``task`` (older writers: ``__index_level_0__``); no pandas needed."""
    rows = pq.read_table(path).to_pylist()
    out = {}
    for r in rows:
        task = r.get('task', r.get('__index_level_0__'))
        if not isinstance(task, str) or 'task_index' not in r:
            raise SurgeryError('layout', 'tasks.parquet')
        out[int(r['task_index'])] = task
    return out


class Source:
    """One dataset opened for reading: ``info``, the episodes table (sorted,
    contiguous ``0..N-1``), the task table, and confined paths to every file it
    will make us open — all checked before any output exists. Every error is a
    ``SurgeryError``: ``unsupported`` for a layout we refuse to open, ``layout``
    for anything unreadable (a footer-less parquet, a missing key — never a raw
    exception, G-15)."""

    def __init__(self, root):
        try:
            self._open(root)
        except SurgeryError:
            raise
        except Exception as e:  # noqa: BLE001 — G-15: production never lets a raw error out
            raise SurgeryError('layout', type(e).__name__) from e

    def _open(self, root):
        self.root = Path(root)
        self.info = read_info(self.root)
        base = self.root.resolve()
        ep_dir = confine(self.root, EPISODES_DIR)
        files = sorted(ep_dir.rglob('*.parquet')) if ep_dir.is_dir() else []
        if not files:
            raise SurgeryError('layout', 'no meta/episodes')
        rows = []
        for f in files:
            rows.extend(pq.read_table(confine(self.root, f.relative_to(base))).to_pylist())
        rows.sort(key=lambda r: r['episode_index'])
        if [r['episode_index'] for r in rows] != list(range(len(rows))):
            raise SurgeryError('layout', 'episode indices not contiguous')
        check_layout(self.info, rows)
        self.episodes = rows
        self.task_by_index = _read_task_table(confine(self.root, TASKS_PATH))
        self.video_keys = video_keys_of(self.info)
        self._data_cache = {}
        # R-2: every file this dataset will make us open is confined NOW (a
        # symlink to outside the dataset is refused here, not mid-write).
        for ep in range(len(rows)):
            self.data_file(ep)
            for k in self.video_keys:
                self.video_file(ep, k)

    @property
    def fps(self):
        return self.info.get('fps')

    def data_file(self, ep):
        r = self.episodes[ep]
        return confine(self.root, DEFAULT_DATA_PATH.format(
            chunk_index=r['data/chunk_index'], file_index=r['data/file_index']))

    def video_file(self, ep, key):
        r = self.episodes[ep]
        return confine(self.root, DEFAULT_VIDEO_PATH.format(
            video_key=key, chunk_index=r[f'videos/{key}/chunk_index'],
            file_index=r[f'videos/{key}/file_index']))

    def data_rows(self, ep) -> pa.Table:
        """The rows of episode ``ep`` (its data file read once, lazily)."""
        r = self.episodes[ep]
        key = (r['data/chunk_index'], r['data/file_index'])
        t = self._data_cache.get(key)
        if t is None:
            try:
                t = pq.read_table(self.data_file(ep))
            except SurgeryError:
                raise
            except Exception as e:  # noqa: BLE001 — G-15: a footer-less / unreadable data file
                raise SurgeryError('layout', type(e).__name__) from e
            self._data_cache[key] = t
        try:
            return t.filter(pc.equal(t['episode_index'], ep))
        except Exception as e:  # noqa: BLE001 — a data file without episode_index
            raise SurgeryError('layout', type(e).__name__) from e

    def stats(self, ep) -> dict:
        """``{feature: {stat: np.array}}`` from the per-episode ``stats/<feature>/<stat>`` columns."""
        out = {}
        for col, val in self.episodes[ep].items():
            if not col.startswith('stats/'):
                continue
            if col.count('/') != 2:
                # feature names contain dots, never slashes: stats/<feature>/<stat>
                raise SurgeryError('layout', f'unexpected stats column {col}')
            _, feat, stat = col.split('/', 2)
            out.setdefault(feat, {})[stat] = np.array(val)
        return out

    def task_of(self, ep) -> str:
        tasks = self.episodes[ep].get('tasks') or []
        return tasks[0] if tasks else ''


# ── compatibility (merge, „Beide behalten") ─────────────────────────────────────

def _comparable_features(info):
    f = {}
    for k, v in (info.get('features') or {}).items():
        f[k] = {'dtype': v.get('dtype'), 'shape': list(v.get('shape') or []), 'names': v.get('names')}
        if v.get('dtype') == 'video':
            vi = v.get('info') or {}
            f[k]['video'] = {x: vi.get(x) for x in _VIDEO_INFO_KEYS}
    return f


def stat_names(src) -> dict:
    """``{feature: sorted stat names}`` from the per-episode stats columns."""
    out = {}
    for col in (src.episodes[0] if src.episodes else {}):
        if col.startswith('stats/') and col.count('/') == 2:
            _, feat, stat = col.split('/', 2)
            out.setdefault(feat, set()).add(stat)
    return {k: sorted(v) for k, v in out.items()}


def check_compatible(sources) -> list:
    """``[(ok, check_id)]`` with exactly ``MERGE_CHECKS``' ids, in that order.
    ``stats``: the per-episode statistics carry the same names per feature — a
    LeRobot that writes no quantiles ``q01…q99`` cannot be merged with one that
    does (LeRobot's own metadata writer would crash, P21)."""
    a = sources[0]
    fa = _comparable_features(a.info)

    def vids(f):
        return {k: v['video'] for k, v in f.items() if 'video' in v}

    def joints(f):
        return {k: v for k, v in f.items() if 'video' not in v}

    fs = [_comparable_features(s.info) for s in sources]
    return [
        (all(str(s.info.get('codebase_version', '')).startswith('v3') for s in sources), 'version'),
        (all(s.info.get('robot_type') == a.info.get('robot_type') for s in sources), 'robot'),
        (all(s.info.get('fps') == a.info.get('fps') for s in sources), 'fps'),
        (all(set(vids(f)) == set(vids(fa)) for f in fs), 'cameras'),
        (all(joints(f) == joints(fa) for f in fs), 'joints'),
        (all(vids(f) == vids(fa) for f in fs), 'video'),
        (all(stat_names(s) == stat_names(a) for s in sources), 'stats'),
    ]


# ── the ONE cutter (R-23) ───────────────────────────────────────────────────────

def cut_episode(container, stream, from_ts, to_ts, length):
    """The packets of one episode, in DECODE order, by stream copy.

    Boundaries are found in decode order: from the keyframe whose pts is the
    episode's first frame up to (excluding) the next KEYFRAME at or after the
    episode's end, or EOF (stopping at the first packet with pts >= end drops
    trailing B-frames of an unaligned source: 5 wrong clips in P20). Refuses
    (``SurgeryError('unaligned')``) unless the first packet is a keyframe at
    exactly ``from_pts``, every packet's pts lies in ``[from_pts, to_pts)``, the
    pts are distinct and there are exactly ``length`` of them. Returns
    ``(packets, from_pts, to_pts)``."""
    tb = stream.time_base
    from_pts = round(from_ts / tb)
    to_pts = round(to_ts / tb)
    container.seek(from_pts, stream=stream, backward=True, any_frame=False)
    pkts = []
    started = False
    for pkt in container.demux(stream):
        if pkt.dts is None or pkt.pts is None:
            continue
        if not started:
            if pkt.is_keyframe and pkt.pts == from_pts:
                started = True
            elif pkt.pts > from_pts and pkt.is_keyframe:
                break                      # passed the start without a keyframe on it
            else:
                continue
        elif pkt.is_keyframe and pkt.pts >= to_pts:
            break
        pkts.append(pkt)
    pts = [p.pts for p in pkts]
    if (not pkts or not pkts[0].is_keyframe or pkts[0].pts != from_pts
            or min(pts) < from_pts or max(pts) >= to_pts or len(set(pts)) != len(pts)
            or len(pts) != length):
        raise SurgeryError('unaligned', f'{len(pkts)} packets for {length} frames')
    return pkts, from_pts, to_pts


def cut_episode_file(path, from_ts, to_ts, length):
    """:func:`cut_episode` on a file: ``(payload bytes per packet, relative pts,
    stream template info)``. The container is always closed."""
    c = av.open(str(path))
    try:
        s = c.streams.video[0]
        pkts, a, _ = cut_episode(c, s, from_ts, to_ts, length)
        return [bytes(p) for p in pkts], [p.pts - a for p in pkts]
    finally:
        c.close()


def write_clip(src_path, from_ts, to_ts, length, out_path):
    """One episode's clip by stream copy into a faststart MP4 at ``out_path``
    (the sidecar's player clips: a clip starts at 0 and ENDS at the episode
    end). Raises ``SurgeryError('unaligned')`` exactly when the engine would."""
    cin = av.open(str(src_path))
    try:
        s = cin.streams.video[0]
        pkts, a, _ = cut_episode(cin, s, from_ts, to_ts, length)
        out = av.open(str(out_path), 'w', format='mp4', options={'movflags': 'faststart'})
        try:
            os_ = out.add_stream_from_template(template=s, opaque=True)
            os_.time_base = s.time_base
            for p in pkts:
                p.pts -= a
                p.dts -= a
                p.stream = os_
                out.mux(p)
        finally:
            out.close()
        return len(pkts)
    finally:
        cin.close()


class _VideoOut:
    """One camera's output video files, rolled like LeRobot's writer."""

    def __init__(self, out_root, key, size_limit_mb, chunks_size):
        self.out_root, self.key = Path(out_root), key
        self.limit = float(size_limit_mb) * 1024 * 1024
        self.chunks_size = int(chunks_size)
        self.chunk, self.file = 0, -1
        self.container = None
        self.stream = None
        self.extradata = None
        self.tb = None
        self.offset = 0          # next pts (in tb) in the current file
        self.bytes = 0

    def _path(self):
        return self.out_root / DEFAULT_VIDEO_PATH.format(
            video_key=self.key, chunk_index=self.chunk, file_index=self.file)

    def _roll(self, in_stream):
        self.close()
        self.file += 1
        if self.file >= self.chunks_size:
            self.chunk, self.file = self.chunk + 1, 0
        p = self._path()
        p.parent.mkdir(parents=True, exist_ok=True)
        self.container = av.open(str(p), 'w', options={'movflags': 'faststart'})
        self.stream = self.container.add_stream_from_template(template=in_stream, opaque=True)
        self.stream.time_base = in_stream.time_base
        self.extradata = bytes(in_stream.codec_context.extradata or b'')
        self.tb = in_stream.time_base
        self.offset = 0
        self.bytes = 0

    def append_episode(self, src_path, from_ts, to_ts, length):
        cin = av.open(str(src_path))
        try:
            s = cin.streams.video[0]
            tb = s.time_base
            pkts, from_pts, to_pts = cut_episode(cin, s, from_ts, to_ts, length)
            size = sum(p.size for p in pkts)
            if (self.container is None or bytes(s.codec_context.extradata or b'') != self.extradata
                    or tb != self.tb or (self.bytes and self.bytes + size >= self.limit)):
                self._roll(s)
            out_from = self.offset
            shift = out_from - from_pts
            for pkt in pkts:
                pkt.pts += shift
                pkt.dts += shift
                pkt.stream = self.stream
                self.container.mux(pkt)
            self.bytes += size
            dur_pts = to_pts - from_pts
            self.offset = out_from + dur_pts
            return {
                f'videos/{self.key}/chunk_index': self.chunk,
                f'videos/{self.key}/file_index': self.file,
                f'videos/{self.key}/from_timestamp': float(out_from * tb),
                f'videos/{self.key}/to_timestamp': float((out_from + dur_pts) * tb),
            }, len(pkts)
        finally:
            cin.close()

    def close(self):
        if self.container is not None:
            self.container.close()
            self.container = None


def _setting(info, key, default):
    value = info.get(key)
    return default if value is None else value


def assemble(out_root, parts, repo_id, *, progress=None) -> dict:
    """Build a NEW dataset at ``out_root`` holding exactly ``parts`` — the ordered
    list of ``(Source, source_episode_index)`` — by stream copy. The output must
    not exist. Compatibility is checked first (``incompatible``); the settings
    (fps, features, chunk and file sizes) are the first source's. Progress:
    ``progress('copy', k, n)`` per output episode."""
    progress = progress or _noop_progress
    t0 = time.time()
    out_root = Path(out_root)
    if out_root.exists():
        raise SurgeryError('exists')
    if not parts:
        raise SurgeryError('layout', 'nothing to assemble')
    srcs = list({id(s): s for s, _ in parts}.values())
    bad = [c for ok, c in check_compatible(srcs) if not ok]
    if bad:
        raise SurgeryError('incompatible', ','.join(bad))
    from lerobot.datasets.dataset_metadata import LeRobotDatasetMetadata   # function-local (sidecar fence)
    first = srcs[0].info
    chunks_size = int(_setting(first, 'chunks_size', _DEFAULT_CHUNKS_SIZE))
    data_mb = _setting(first, 'data_files_size_in_mb', _DEFAULT_DATA_FILES_MB)
    video_mb = _setting(first, 'video_files_size_in_mb', _DEFAULT_VIDEO_FILES_MB)
    meta = LeRobotDatasetMetadata.create(
        repo_id=repo_id, fps=first['fps'], features=dict(first['features']),
        robot_type=first.get('robot_type'), root=out_root, use_videos=bool(srcs[0].video_keys),
        chunks_size=first.get('chunks_size'), data_files_size_in_mb=first.get('data_files_size_in_mb'),
        video_files_size_in_mb=first.get('video_files_size_in_mb'))
    # tasks, in first-seen order
    seen = []
    for s, ep in parts:
        for t in s.episodes[ep]['tasks']:
            if t not in seen:
                seen.append(t)
    meta.save_episode_tasks(seen)
    vouts = {k: _VideoOut(out_root, k, video_mb, chunks_size) for k in srcs[0].video_keys}
    data_writer = data_schema = data_fh = None
    data_chunk, data_file = 0, -1
    data_limit = float(data_mb) * 1024 * 1024
    global_index = 0
    packets = 0
    total = len(parts)
    try:
        for new_ep, (s, ep) in enumerate(parts):
            rec = s.episodes[ep]
            length = int(rec['length'])
            # ---- data rows: three index columns renumbered, the payload verbatim
            t = s.data_rows(ep)
            if t.num_rows != length:
                raise SurgeryError('layout', 'data rows != length')
            new_ti = [meta.get_task_index(s.task_by_index[i]) for i in t['task_index'].to_pylist()]
            for col, values in (('episode_index', [new_ep] * length),
                                ('index', range(global_index, global_index + length)),
                                ('task_index', new_ti)):
                i = t.schema.get_field_index(col)
                t = t.set_column(i, t.schema.field(col), pa.array(values, type=t.schema.field(col).type))
            if data_writer is None or data_fh.tell() >= data_limit:     # G-15: roll the data file like LeRobot
                if data_writer is not None:
                    data_writer.close()
                    data_fh.close()
                data_file += 1
                if data_file >= chunks_size:
                    data_chunk, data_file = data_chunk + 1, 0
                if data_schema is None:
                    data_schema = t.schema
                p = out_root / DEFAULT_DATA_PATH.format(chunk_index=data_chunk, file_index=data_file)
                p.parent.mkdir(parents=True, exist_ok=True)
                data_fh = open(p, 'wb')
                data_writer = pq.ParquetWriter(data_fh, schema=data_schema, compression='snappy',
                                               use_dictionary=True)
            # F-7: a source's column order may differ from the first source's
            data_writer.write_table(t.select(data_schema.names).cast(data_schema))
            ep_meta = {'data/chunk_index': data_chunk, 'data/file_index': data_file,
                       'dataset_from_index': global_index, 'dataset_to_index': global_index + length}
            global_index += length
            # ---- videos: the episode's packets, cut in decode order
            for k, vo in vouts.items():
                vm, n = vo.append_episode(s.video_file(ep, k), rec[f'videos/{k}/from_timestamp'],
                                          rec[f'videos/{k}/to_timestamp'], length)
                if n != length:
                    raise SurgeryError('unaligned', f'{k}: {n} packets for {length} frames')
                packets += n
                ep_meta.update(vm)
            meta.save_episode(new_ep, length, list(rec['tasks']), s.stats(ep), ep_meta)
            progress('copy', new_ep + 1, total)
    finally:
        if data_writer is not None:
            data_writer.close()
            data_fh.close()
        for vo in vouts.values():
            vo.close()
    meta.finalize()
    return {'episodes': len(parts), 'frames': global_index, 'packets': packets,
            'secs': round(time.time() - t0, 3)}


# ── identity, verification, integrity ──────────────────────────────────────────

def _episode_digest(path, from_ts, to_ts, length):
    """(sha256 of the payload bytes, packet count, sha256 of the relative pts)."""
    payloads, rel = cut_episode_file(path, from_ts, to_ts, length)
    h = hashlib.sha256()
    for b in payloads:
        h.update(b)
    return h.hexdigest(), len(payloads), hashlib.sha256(json.dumps(rel).encode()).hexdigest()


def _col_digest(table, cols):
    h = hashlib.sha256()
    for c in cols:
        h.update(c.encode())
        h.update(json.dumps(table[c].to_pylist()).encode())
    return h.hexdigest()


def payload_columns(table) -> list:
    return sorted(c for c in table.column_names if c not in INDEX_COLUMNS)


def episode_identity(src, ep) -> str:
    """„Beide behalten"'s episode identity (G-16, audit m1): sha256 over the
    data-row payload (every column but the three index columns, by sorted name)
    AND, per camera by sorted key, the episode's packet bytes and relative pts.
    Two takes with bit-identical joint rows but different video (a still arm)
    are two episodes; the same take is the same id in every copy (assemble
    copies rows and packets verbatim). ``SurgeryError('unaligned')`` for an
    episode the cutter refuses."""
    t = src.data_rows(ep)
    h = hashlib.sha256(_col_digest(t, payload_columns(t)).encode())
    r = src.episodes[ep]
    for k in sorted(src.video_keys):
        d, _, rel = _episode_digest(src.video_file(ep, k), r[f'videos/{k}/from_timestamp'],
                                    r[f'videos/{k}/to_timestamp'], int(r['length']))
        h.update(f'\0{k}\0{d}\0{rel}'.encode())
    return h.hexdigest()


def _stats_equal(a, b):
    if set(a) != set(b):
        return False
    for feat in a:
        if set(a[feat]) != set(b[feat]):
            return False
        for st in a[feat]:
            x, y = np.asarray(a[feat][st]), np.asarray(b[feat][st])
            if x.shape != y.shape or not np.array_equal(x, y, equal_nan=x.dtype.kind == 'f'):
                return False
    return True


def _physical_index(root) -> list:
    """The ``index`` column of every data file in (chunk, file) order."""
    phys = []
    for f in sorted((Path(root) / 'data').rglob('*.parquet')):
        phys += pq.read_table(f, columns=['index'])['index'].to_pylist()
    return phys


def verify(out_root, parts) -> list:
    """Production verify of an assembled output against its ``parts`` (P3,
    R-22, G-15). NEVER raises: every failure — a ``SurgeryError`` raised inside
    (never surfaced as ``unaligned``), a decode error, any exception — is a
    problem. Returns the list of problems (empty = verified):
    LeRobot's own loader loads the output; the physical row order (every data
    file's ``index`` in (chunk, file) order is ``0..total_frames-1``); per output
    episode and camera the span, the packet payload digest and relative pts
    equal to the source's, the first and last frame decode at the train-time
    tolerance (two calls); per episode the data rows (count, contiguous
    ``index``, ``frame_index``, one ``episode_index``, the payload columns and
    the task strings equal to the source's) and the per-episode statistics;
    totals."""
    try:
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
        from lerobot.datasets.video_utils import decode_video_frames
        out = Source(out_root)
    except SurgeryError as e:
        return [('source', e.code)]
    except Exception as e:  # noqa: BLE001
        return [('load', type(e).__name__)]
    try:
        phys = _physical_index(out_root)
    except Exception as e:  # noqa: BLE001
        return [('data', type(e).__name__)]
    if phys != list(range(int(out.info.get('total_frames') or 0))):
        return [('data', 'row_order')]
    try:
        # LeRobot's own loader, LAST (our checks above already proved every file
        # it reads, so its hub fallback is never reached); a fake repo id, so a
        # fallback download could only fail, never fetch into the folder.
        LeRobotDataset('verify/check', root=out_root)
    except Exception as e:  # noqa: BLE001
        return [('load', type(e).__name__)]
    fps = out.info['fps']
    problems = []
    for new_ep, (s, ep) in enumerate(parts):
        try:
            ro, rs = out.episodes[new_ep], s.episodes[ep]
            length = int(ro['length'])
            if length != int(rs['length']):
                problems.append((new_ep, 'length'))
            for k in out.video_keys:
                fo, to = ro[f'videos/{k}/from_timestamp'], ro[f'videos/{k}/to_timestamp']
                if abs((to - fo) - length / fps) > 0.5 / fps:
                    problems.append((new_ep, k, 'span'))
                if _episode_digest(out.video_file(new_ep, k), fo, to, length) != _episode_digest(
                        s.video_file(ep, k), rs[f'videos/{k}/from_timestamp'],
                        rs[f'videos/{k}/to_timestamp'], length):
                    problems.append((new_ep, k, 'packets'))
                for ts in (fo, fo + (length - 1) / fps):    # two calls (one decodes the whole episode)
                    decode_video_frames(out.video_file(new_ep, k), [ts],
                                        tolerance_s=DECODE_TOLERANCE_S, backend='pyav')
            to_, ts_ = out.data_rows(new_ep), s.data_rows(ep)
            if to_.num_rows != length:
                problems.append((new_ep, 'rows'))
            if to_['index'].to_pylist() != list(range(ro['dataset_from_index'], ro['dataset_to_index'])):
                problems.append((new_ep, 'index'))
            if to_['frame_index'].to_pylist() != list(range(length)):
                problems.append((new_ep, 'frame_index'))
            if set(to_['episode_index'].to_pylist()) != {new_ep}:
                problems.append((new_ep, 'episode_index'))
            payload = payload_columns(ts_)
            if payload_columns(to_) != payload or _col_digest(to_, payload) != _col_digest(ts_, payload):
                problems.append((new_ep, 'data'))
            if ([out.task_by_index[i] for i in to_['task_index'].to_pylist()]
                    != [s.task_by_index[i] for i in ts_['task_index'].to_pylist()]):
                problems.append((new_ep, 'task_index'))
            if not _stats_equal(out.stats(new_ep), s.stats(ep)):
                problems.append((new_ep, 'stats'))
        except SurgeryError as e:
            problems.append((new_ep, 'verify:' + e.code))
        except Exception as e:  # noqa: BLE001 — a decode error is a problem, not a crash
            problems.append((new_ep, 'error:' + type(e).__name__))
    if out.info.get('total_episodes') != len(parts):
        problems.append('total_episodes')
    if out.info.get('total_frames') != sum(int(s.episodes[e]['length']) for s, e in parts):
        problems.append('total_frames')
    return problems


def verify_or_raise(out_root, parts):
    """:func:`verify`, raising ``SurgeryError('verify_failed')`` on any problem."""
    problems = verify(out_root, parts)
    if problems:
        raise SurgeryError('verify_failed', str(problems[:5]))


def integrity(root, *, known_good=frozenset()) -> 'Source':
    """The local gate of EVERY dataset upload (audit M3/m2, §E2 step 5b): the
    dataset loads, ``info.json``'s totals equal the episodes table, every data
    file under ``data/`` reads (LeRobot loads them by glob) and their ``index``
    columns concatenated are ``0..total_frames-1``, every episode has ``length``
    data rows, and every video file the episodes reference demuxes to its end
    with at least as many frames as its episodes claim — except the files in
    ``known_good`` (byte-equal to the record's ``files``, checked by the caller),
    so the cost follows what changed since the last sync. LeRobot's own loader
    runs LAST (a fake repo id; after our checks its hub fallback is never
    reached). Raises ``SurgeryError('broken', <what>)``."""
    try:
        src = Source(root)
        if src.info.get('total_episodes') != len(src.episodes):
            raise SurgeryError('broken', 'total_episodes')
        if src.info.get('total_frames') != sum(int(r['length']) for r in src.episodes):
            raise SurgeryError('broken', 'total_frames')
        if _physical_index(root) != list(range(int(src.info['total_frames']))):
            raise SurgeryError('broken', 'data index')
        need = {}
        for ep, r in enumerate(src.episodes):
            if src.data_rows(ep).num_rows != int(r['length']):
                raise SurgeryError('broken', f'rows {ep}')
            for k in src.video_keys:
                p = src.video_file(ep, k)
                need[p] = need.get(p, 0) + int(r['length'])
        root_r = Path(root).resolve()
        for path, frames in need.items():
            if path.relative_to(root_r).as_posix() in known_good:
                continue
            c = av.open(str(path))
            try:
                n = sum(1 for p in c.demux(c.streams.video[0]) if p.size)
            finally:
                c.close()
            if n < frames:
                raise SurgeryError('broken', f'{path.name}: {n} < {frames} frames')
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
        LeRobotDataset('integrity/check', root=root)
        return src
    except SurgeryError as e:
        if e.code == 'broken':
            raise
        raise SurgeryError('broken', f'{e.code}: {e.detail}') from e
    except Exception as e:  # noqa: BLE001
        raise SurgeryError('broken', type(e).__name__) from e

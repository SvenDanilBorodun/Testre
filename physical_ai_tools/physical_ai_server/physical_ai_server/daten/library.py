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

"""The local dataset library of the Daten sidecar (spec §B4, §B6, §C1, §J.4).

Everything here READS: the dataset folders of the requested namespaces, their
``meta/``, data rows and video packets. Nothing is written but the sidecar's
own temp clips (under ``/tmp/edubotics-daten``, deleted at once).

Confinement (§B6): a dataset id is ``<ns>/<name>``, each part one component
matching ``DATASET_PART_RE``, the name without a reserved suffix; its folder is
``safe_child(safe_child(root, ns), name)``. Inside a dataset nothing is opened
by a path the dataset names: it is read only when its ``info.json`` carries
LeRobot's default templates (``v3_surgery.check_layout``/``Source``, which build
and confine every path themselves); anything else is ``unsupported``.

Caches: per dataset the meta digest, memoised on the ``(st_mtime_ns, st_size)``
of its ``meta/`` files; the summary, hints and thumbnail by ``(id, digest)``;
the clips in an LRU byte cache of ``CLIP_CACHE_MAX_BYTES`` keyed ``(id,
digest, episode, camera)``, refused clips remembered per key. An edit changes
the digest and so invalidates every cached item of that dataset.

Never imports ROS or LeRobot (the sidecar's import fence): ``v3_surgery`` keeps
LeRobot inside its writing/verifying functions, which this module never calls.
"""

from __future__ import annotations

import collections
import contextlib
import datetime
from fractions import Fraction
import json
import os
from pathlib import Path
import queue
import re
import tempfile
import threading
from typing import Optional

import av
import numpy as np

from physical_ai_server.daten import contract as C
from physical_ai_server.data_processing import dataset_hints as hints_mod
from physical_ai_server.data_processing import dataset_paths
from physical_ai_server.data_processing import dataset_sync as S
from physical_ai_server.data_processing import v3_surgery as V

_PART = re.compile(C.DATASET_PART_RE)
_UNREAD = object()
THUMB_WIDTH = 320
CAMERA_PREFIX = 'observation.images.'


class LibraryError(Exception):
    """A refusal with a ``contract.HTTP_ERRORS`` code (never a path)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def valid_part(part) -> bool:
    return (isinstance(part, str) and bool(_PART.match(part))
            and not part.endswith(C.RESERVED_SUFFIXES))


def split_id(dataset_id):
    """``(ns, name)`` of a valid dataset id, else LibraryError('invalid')."""
    if not isinstance(dataset_id, str) or dataset_id.count('/') != 1:
        raise LibraryError('invalid')
    ns, name = dataset_id.split('/')
    if not valid_part(ns) or not valid_part(name):
        raise LibraryError('invalid')
    return ns, name


def is_valid_id(dataset_id) -> bool:
    try:
        split_id(dataset_id)
    except LibraryError:
        return False
    return True


def dataset_state(path, info=_UNREAD) -> str:
    """A local dataset's state (§J.4.1, H-1): ``in_session`` FIRST (a running
    first session has no meta/episodes yet), then ``old_format``,
    ``unsupported``, ``incomplete``, ``ok``. The library and the DatenService's
    refusals ask this one function."""
    path = Path(path)
    if S.session_marker_path(path).exists():
        return 'in_session'
    if info is _UNREAD:
        info = _read_info(path)
    if info is None:
        return 'incomplete'
    if str(info.get('codebase_version', '')).startswith('v2'):
        return 'old_format'
    try:
        V.check_layout(info, [])
    except V.SurgeryError:
        return 'unsupported'
    if not isinstance(info.get('total_episodes'), int) or info.get('total_episodes') < 1:
        return 'incomplete'
    episodes = path / 'meta' / 'episodes'
    if not episodes.is_dir() or not any(episodes.rglob('*.parquet')):
        return 'incomplete'
    try:
        V.Source(path)
    except V.SurgeryError as e:
        return 'unsupported' if e.code == 'unsupported' else 'incomplete'
    return 'ok'


def _iso(ts) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _read_info(path: Path):
    try:
        info = json.loads((path / 'meta' / 'info.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def camera_list(info) -> list:
    """The summary's ``cameras``: one entry per video key, in ``info.json``
    order; a camera is addressed by its INDEX here, never by a string."""
    out = []
    for i, key in enumerate(V.video_keys_of(info or {})):
        vi = ((info.get('features') or {}).get(key) or {}).get('info') or {}
        out.append({'index': i, 'key': key,
                    'name': key[len(CAMERA_PREFIX):] if key.startswith(CAMERA_PREFIX) else key,
                    'width': vi.get('video.width'), 'height': vi.get('video.height'),
                    'codec': vi.get('video.codec'), 'pix_fmt': vi.get('video.pix_fmt'),
                    'fps': vi.get('video.fps')})
    return out


def joint_names(info) -> dict:
    feats = (info or {}).get('features') or {}
    return {'state': list((feats.get('observation.state') or {}).get('names') or []),
            'action': list((feats.get('action') or {}).get('names') or [])}


class Library:
    """The local half of the sidecar."""

    def __init__(self, root=None, robot_type='omx_f', clip_tmp_dir=C.CLIP_TMP_DIR,
                 clip_cache_max=C.CLIP_CACHE_MAX_BYTES):
        self.root = Path(root) if root is not None else dataset_paths.dataset_root()
        self.robot_type = robot_type
        self.clip_tmp_dir = Path(clip_tmp_dir)
        self.clip_cache_max = int(clip_cache_max)
        self._lock = threading.Lock()
        self._digests = {}                       # path -> (meta signature, digest)
        self._static = {}                        # (id, digest) -> static entry fields
        self._summaries = {}                     # (id, digest) -> summary
        self._hints = {}                         # (id, digest) -> [hints per episode]
        self._thumbs = {}                        # (id, digest) -> jpeg bytes
        self._clips = collections.OrderedDict()  # (id, digest, i, c) -> bytes
        self._clip_bytes = 0
        self._unplayable = set()
        self._hint_queue = queue.Queue()
        self._hint_pending = set()
        self._hint_thread = None
        self._inflight = {}                      # build key -> {'event', 'result', 'error'}
        self.follow_wait_s = 3 * C.MEDIA_WAIT_S  # a request waiting for another's identical build

    # ── paths ─────────────────────────────────────────────────────────────

    def path_of(self, dataset_id) -> Path:
        """The confined folder of an EXISTING dataset; LibraryError('invalid'
        | 'not_found')."""
        ns, name = split_id(dataset_id)
        if (self.root / ns).is_symlink() or (self.root / ns / name).is_symlink():
            raise LibraryError('invalid')        # the library never lists one; never read through one
        try:
            path = dataset_paths.safe_child(dataset_paths.safe_child(self.root, ns), name)
        except dataset_paths.DatasetPathError:
            raise LibraryError('invalid')
        if not path.is_dir():
            raise LibraryError('not_found')
        return path

    @staticmethod
    def _refuse_in_session(path):
        """A dataset with a session marker is never read for media (§J.4.3)."""
        if S.session_marker_path(path).exists():
            raise LibraryError('in_session')

    def _build_once(self, key, gate, fn):
        """Run ``fn`` under ``gate`` (the media pool) ONCE per ``key`` at a time:
        a concurrent request for the same item waits for the first one's answer
        (bounded by ``follow_wait_s``; past it ``overloaded``) and never takes a
        pool slot of its own, so N parallel GETs of one thumbnail build it once."""
        with self._lock:
            slot = self._inflight.get(key)
            leader = slot is None
            if leader:
                slot = {'event': threading.Event(), 'result': None, 'error': None}
                self._inflight[key] = slot
        if not leader:
            if not slot['event'].wait(self.follow_wait_s):
                raise LibraryError('overloaded')
            if slot['error'] is not None:
                raise slot['error']
            return slot['result']
        try:
            with gate:
                slot['result'] = fn()
            return slot['result']
        except BaseException as e:
            slot['error'] = e
            raise
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            slot['event'].set()

    def records(self, local_entries) -> dict:
        """``{id: the full sync record or None}`` of the scanned entries (the hub
        part's head fast path reads ``hub_trees``, which the entry's short
        ``record`` does not carry)."""
        out = {}
        for e in local_entries:
            try:
                out[e['id']] = S.own_record(self.path_of(e['id']), e['id'])
            except LibraryError:
                continue
        return out

    def namespaces_present(self, namespaces):
        out = []
        for ns in namespaces:
            if valid_part(ns) and ns not in out:
                out.append(ns)
        return out

    def dataset_names(self, ns) -> list:
        """The dataset folders of one namespace: valid names only (hidden
        siblings, records, markers and tmp/bak/trash directories never match)."""
        try:
            base = dataset_paths.safe_child(self.root, ns)
        except dataset_paths.DatasetPathError:
            return []
        if not base.is_dir():
            return []
        out = []
        for child in sorted(base.iterdir()):
            if valid_part(child.name) and child.is_dir() and not child.is_symlink():
                out.append(child.name)
        return out

    # ── the meta digest, memoised ─────────────────────────────────────────

    def meta_digest(self, path: Path) -> str:
        meta = path / 'meta'
        sig = []
        if meta.is_dir():
            for p in sorted(meta.rglob('*')):
                if p.is_file():
                    st = p.stat()
                    sig.append((p.relative_to(path).as_posix(), st.st_mtime_ns, st.st_size))
        sig = tuple(sig)
        key = str(path)
        with self._lock:
            hit = self._digests.get(key)
            if hit and hit[0] == sig:
                return hit[1]
        digest = S.meta_digest(path)
        with self._lock:
            self._digests[key] = (sig, digest)
        return digest

    # ── the library ───────────────────────────────────────────────────────

    def state_of(self, path: Path, info) -> str:
        return dataset_state(path, info)

    def _static_entry(self, dataset_id, path, digest):
        key = (dataset_id, digest)
        with self._lock:
            hit = self._static.get(key)
        if hit is not None:
            return hit
        info = _read_info(path)
        state = self.state_of(path, info)
        fps = (info or {}).get('fps')
        total_frames = (info or {}).get('total_frames')
        entry = {
            'state': state, 'codebase_version': (info or {}).get('codebase_version'),
            'robot_type': (info or {}).get('robot_type'), 'fps': fps,
            'total_episodes': (info or {}).get('total_episodes'), 'total_frames': total_frames,
            'duration_s': (round(total_frames / fps, 3)
                           if isinstance(total_frames, int) and isinstance(fps, (int, float)) and fps else None),
            'cameras': camera_list(info) if info else [], 'joints': joint_names(info) if info else
            {'state': [], 'action': []}, 'stat_names': {}, 'tasks': [],
        }
        if state == 'ok':
            try:
                src = V.Source(path)
                entry['stat_names'] = V.stat_names(src)
                entry['tasks'] = [src.task_by_index[k] for k in sorted(src.task_by_index)]
            except V.SurgeryError:
                entry['state'] = 'incomplete'
        with self._lock:
            self._static[key] = entry
        return entry

    def entry(self, dataset_id) -> dict:
        """One library entry (§J.4.1 ``local[]``)."""
        path = self.path_of(dataset_id)
        ns, name = dataset_id.split('/')
        digest = self.meta_digest(path)
        static = self._static_entry(dataset_id, path, digest)
        size, newest = 0, 0.0
        for dirpath, _, files in os.walk(path):
            for f in files:
                try:
                    st = os.stat(os.path.join(dirpath, f))
                except OSError:
                    continue
                size += st.st_size
                if os.path.join(dirpath, f).startswith(str(path / 'meta')):
                    newest = max(newest, st.st_mtime)
        rec = S.own_record(path, dataset_id)
        out = {'id': dataset_id, 'ns': ns, 'name': name,
               'display_name': rec.get('display_name') if rec else None,
               'meta_digest': digest, **static, 'size_bytes': size,
               'modified_at': _iso(newest) if newest else None,
               'hint_episodes': self.hint_episodes(dataset_id, digest) if static['state'] == 'ok' else None,
               'record': ({'hub_sha': rec.get('hub_sha'), 'synced_at': rec.get('synced_at'),
                           'source_repo': (rec.get('source') or {}).get('repo_id'),
                           'private': rec.get('private'), 'tag_ok': rec.get('tag_ok') is not False}
                          if rec else None)}
        return out

    def scan(self, namespaces, ids=None) -> list:
        """The local datasets of ``namespaces`` (or exactly ``ids``); hints are
        queued for computation after the entries are built (R-6)."""
        wanted = []
        if ids is not None:
            for dataset_id in ids:
                try:
                    split_id(dataset_id)
                except LibraryError:
                    continue
                wanted.append(dataset_id)
        else:
            for ns in self.namespaces_present(namespaces):
                wanted += [f'{ns}/{name}' for name in self.dataset_names(ns)]
        out = []
        for dataset_id in wanted:
            try:
                out.append(self.entry(dataset_id))
            except LibraryError:
                continue
        for e in out:
            if e['state'] == 'ok' and e['hint_episodes'] is None:
                self.queue_hints(e['id'], e['meta_digest'])
        return out

    # ── hints (R-6) ────────────────────────────────────────────────────────

    def hint_episodes(self, dataset_id, digest) -> Optional[int]:
        with self._lock:
            h = self._hints.get((dataset_id, digest))
        return None if h is None else sum(1 for x in h if x)

    def queue_hints(self, dataset_id, digest):
        with self._lock:
            if (dataset_id, digest) in self._hints or (dataset_id, digest) in self._hint_pending:
                return
            self._hint_pending.add((dataset_id, digest))
        self._hint_queue.put((dataset_id, digest))

    def start_hint_worker(self):
        """One background thread (HINT_WORKERS = 1) computing queued hints."""
        if self._hint_thread is None:
            self._hint_thread = threading.Thread(target=self._hint_loop, name='daten-hints', daemon=True)
            self._hint_thread.start()

    def _hint_loop(self):
        while True:
            dataset_id, digest = self._hint_queue.get()
            try:
                self.hints(dataset_id, digest)
            except Exception:  # noqa: BLE001 — a dataset whose hints fail keeps None
                pass
            finally:
                with self._lock:
                    self._hint_pending.discard((dataset_id, digest))

    def hints(self, dataset_id, digest=None) -> list:
        """Per episode the §F6 hint list, cached by ``(id, digest)``."""
        path = self.path_of(dataset_id)
        digest = digest or self.meta_digest(path)
        with self._lock:
            hit = self._hints.get((dataset_id, digest))
        if hit is not None:
            return hit
        src = self._source(path)
        names = joint_names(src.info)['action']
        episodes = []
        for e in range(len(src.episodes)):
            t = src.data_rows(e)
            state = np.array(t['observation.state'].to_pylist(), dtype=np.float64)
            action = np.array(t['action'].to_pylist(), dtype=np.float64)
            episodes.append((state, action))
        result = hints_mod.dataset_hints(episodes, src.info['fps'], names)
        with self._lock:
            self._hints[(dataset_id, digest)] = result
        return result

    # ── one dataset ──────────────────────────────────────────────────────

    def _source(self, path):
        if S.session_marker_path(path).exists():
            raise LibraryError('in_session')
        info = _read_info(path)
        state = self.state_of(path, info)
        if state in ('in_session', 'unsupported', 'incomplete'):
            raise LibraryError(state)
        if state == 'old_format':
            raise LibraryError('unsupported')
        try:
            return V.Source(path)
        except V.SurgeryError as e:
            raise LibraryError('unsupported' if e.code == 'unsupported' else 'incomplete')

    def summary(self, dataset_id, gate=None) -> dict:
        """§J.4.3: per episode its length, task, per-camera packet counts,
        ``playable`` (every camera passes the cutter's rules), bytes and hints.
        Cached by ``(id, digest)``; ``gate`` (the media pool) is taken only to
        build."""
        path = self.path_of(dataset_id)
        self._refuse_in_session(path)
        digest = self.meta_digest(path)
        with self._lock:
            hit = self._summaries.get((dataset_id, digest))
        if hit is not None:
            return hit
        return self._build_once(('summary', dataset_id, digest), gate or contextlib.nullcontext(),
                                lambda: self._build_summary(dataset_id, path, digest))

    def _build_summary(self, dataset_id, path, digest):
        src = self._source(path)
        fps = src.info['fps']
        hints = self.hints(dataset_id, digest)
        rows_in_file = {}
        episodes = []
        for e, r in enumerate(src.episodes):
            length = int(r['length'])
            frames, video_bytes, playable = {}, 0, True
            for ci, key in enumerate(src.video_keys):
                n, b, ok = self._count_packets(src.video_file(e, key), r[f'videos/{key}/from_timestamp'],
                                               r[f'videos/{key}/to_timestamp'], length)
                frames[str(ci)] = n
                video_bytes += b
                playable = playable and ok
            data_file = src.data_file(e)
            if data_file not in rows_in_file:
                try:
                    import pyarrow.parquet as pq
                    rows_in_file[data_file] = (data_file.stat().st_size,
                                               pq.ParquetFile(str(data_file)).metadata.num_rows)
                except Exception:  # noqa: BLE001
                    rows_in_file[data_file] = (0, 0)
            fbytes, frows = rows_in_file[data_file]
            episodes.append({
                'i': e, 'length': length, 'duration_s': round(length / fps, 3), 'task': src.task_of(e),
                'frames': frames, 'playable': playable,
                'bytes': {'video': video_bytes, 'data_estimate': int(fbytes * length / frows) if frows else 0},
                'hints': hints[e] if e < len(hints) else [],
            })
        out = {'v': C.SCHEMA_VERSION, 'id': dataset_id, 'meta_digest': digest, 'fps': fps,
               'robot_type': src.info.get('robot_type'), 'total_episodes': len(src.episodes),
               'total_frames': src.info.get('total_frames'), 'cameras': camera_list(src.info),
               'joints': joint_names(src.info), 'tasks': [src.task_by_index[k] for k in sorted(src.task_by_index)],
               'algo': hints_mod.ALGO, 'episodes': episodes}
        with self._lock:
            self._summaries[(dataset_id, digest)] = out
        return out

    @staticmethod
    def _count_packets(path, from_ts, to_ts, length):
        """``(packets, bytes, playable)`` of one episode slice of one camera."""
        c = av.open(str(path))
        try:
            s = c.streams.video[0]
            try:
                pkts, _, _ = V.cut_episode(c, s, from_ts, to_ts, length)
                return len(pkts), sum(p.size for p in pkts), True
            except V.SurgeryError:
                tb = s.time_base
                a, b = round(from_ts / tb), round(to_ts / tb)
                c.seek(a, stream=s, backward=True, any_frame=False)
                n = size = 0
                for p in c.demux(s):
                    if p.pts is not None and a <= p.pts < b:
                        n += 1
                        size += p.size
                return n, size, False
        finally:
            c.close()

    def episode_data(self, dataset_id, i) -> dict:
        """§J.4.5: the episode's timestamps, follower state and leader action
        (radians), floats rounded to 6 decimals."""
        path = self.path_of(dataset_id)
        src = self._source(path)
        if type(i) is not int or not 0 <= i < len(src.episodes):
            raise LibraryError('not_found')
        t = src.data_rows(i)

        def rows(col):
            return [[round(float(x), 6) for x in r] for r in t[col].to_pylist()] if col in t.column_names else []
        return {'v': C.SCHEMA_VERSION, 'i': i, 'fps': src.info['fps'], 'length': int(src.episodes[i]['length']),
                'unit': 'rad', 'names': joint_names(src.info),
                'timestamp': [round(float(x), 6) for x in t['timestamp'].to_pylist()],
                'state': rows('observation.state'), 'action': rows('action')}

    def clip(self, dataset_id, i, c, gate=None):
        """``(bytes, etag)`` of episode ``i``'s clip of camera index ``c``: the
        engine's ONE cutter, stream copy, faststart; LibraryError('unplayable')
        when the cutter refuses (remembered per key). Served from the LRU byte
        cache without ``gate``; ``gate`` (the media pool) is taken only to build."""
        path = self.path_of(dataset_id)
        self._refuse_in_session(path)
        digest = self.meta_digest(path)
        key = (dataset_id, digest, i, c)
        etag = f'"{digest[:16]}-{i}-{c}"'
        with self._lock:
            if key in self._unplayable:
                raise LibraryError('unplayable')
            data = self._clips.get(key)
            if data is not None:
                self._clips.move_to_end(key)
                return data, etag
        data = self._build_once(('clip',) + key, gate or contextlib.nullcontext(),
                                lambda: self._build_clip(key, path, i, c))
        return data, etag

    def _build_clip(self, key, path, i, c):
        src = self._source(path)
        if type(i) is not int or not 0 <= i < len(src.episodes) or type(c) is not int \
                or not 0 <= c < len(src.video_keys):
            raise LibraryError('not_found')
        r = src.episodes[i]
        k = src.video_keys[c]
        self.clip_tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix='.mp4', dir=str(self.clip_tmp_dir))
        os.close(fd)
        try:
            V.write_clip(src.video_file(i, k), r[f'videos/{k}/from_timestamp'], r[f'videos/{k}/to_timestamp'],
                         int(r['length']), tmp)
            data = Path(tmp).read_bytes()
        except V.SurgeryError:
            with self._lock:
                self._unplayable.add(key)
            raise LibraryError('unplayable')
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        with self._lock:
            if key not in self._clips:
                self._clips[key] = data
                self._clip_bytes += len(data)
            while self._clip_bytes > self.clip_cache_max and len(self._clips) > 1:
                _, old = self._clips.popitem(last=False)
                self._clip_bytes -= len(old)
        return data

    def clip_cache_bytes(self) -> int:
        with self._lock:
            return self._clip_bytes

    def thumb(self, dataset_id, gate=None):
        """``(jpeg bytes, etag)``: the first frame of episode 0 of the scene camera
        (else the first camera), ``THUMB_WIDTH`` wide. Cached by ``(id,
        digest)``; ``gate`` (the media pool) is taken only to build."""
        path = self.path_of(dataset_id)
        self._refuse_in_session(path)
        digest = self.meta_digest(path)
        etag = f'"{digest[:16]}-thumb"'
        with self._lock:
            hit = self._thumbs.get((dataset_id, digest))
        if hit is not None:
            return hit, etag
        data = self._build_once(('thumb', dataset_id, digest), gate or contextlib.nullcontext(),
                                lambda: self._build_thumb(dataset_id, path, digest))
        return data, etag

    def _build_thumb(self, dataset_id, path, digest):
        src = self._source(path)
        if not src.episodes or not src.video_keys:
            raise LibraryError('not_found')
        keys = src.video_keys
        key = next((k for k in keys if k.endswith('.scene')), keys[0])
        r = src.episodes[0]
        frame = self._first_frame(src.video_file(0, key), r[f'videos/{key}/from_timestamp'])
        if frame is None:
            raise LibraryError('internal')
        w = THUMB_WIDTH
        h = max(2, int(round(frame.height * w / frame.width / 2)) * 2)
        cc = av.CodecContext.create('mjpeg', 'w')
        cc.width, cc.height, cc.pix_fmt = w, h, 'yuvj420p'
        cc.time_base = Fraction(1, 30)
        pkts = cc.encode(frame.reformat(width=w, height=h, format='yuvj420p')) + cc.encode(None)
        data = bytes(pkts[0])
        with self._lock:
            self._thumbs[(dataset_id, digest)] = data
        return data

    @staticmethod
    def _first_frame(path, from_ts):
        c = av.open(str(path))
        try:
            s = c.streams.video[0]
            target = round(from_ts / s.time_base)
            c.seek(target, stream=s, backward=True, any_frame=False)
            for f in c.decode(s):
                if f.pts is not None and f.pts >= target:
                    return f
            return None
        finally:
            c.close()

    def local_for_hubstate(self, dataset_id) -> Optional[dict]:
        """§J.4.4 ``local``: from ``meta/info.json`` ALONE (never the episodes
        table), for EVERY state incl. ``in_session`` (U-6); None without a
        readable ``info.json``."""
        path = self.path_of(dataset_id)
        info = _read_info(path)
        if info is None:
            return None
        fps, frames = info.get('fps'), info.get('total_frames')
        newest = 0.0
        meta = path / 'meta'
        if meta.is_dir():
            for p in meta.rglob('*'):
                if p.is_file():
                    newest = max(newest, p.stat().st_mtime)
        return {'total_episodes': info.get('total_episodes'),
                'duration_s': round(frames / fps, 3) if isinstance(frames, int) and fps else None,
                'modified_at': _iso(newest) if newest else None}


def sync_map(library, local_entries, views, listed, default_view=None):
    """``{id: {state, reason, head}}`` for every local id and every listed hub
    id (§J.4.1 ``sync``), from ONE decision (``dataset_sync.decide``) — the one
    the recording Start and the upload use too.

    ``views``: {id: hub view} for the ids the hub was asked about; ``listed``:
    {id: hub entry} of the online repos (an id there and not local → ``online``);
    ``default_view``: the view of an id ``views`` lacks — None when the hub was
    not asked (``hub=0``, no token, the token changed: ``unknown/not_asked``),
    ``{'state': 'unreachable'}`` when asking failed. A content decision whose
    file listing fails mid-way is decided as unreachable, never dropped."""
    out = {}
    for e in local_entries:
        dataset_id = e['id']
        view = views.get(dataset_id, default_view)
        try:
            path = library.path_of(dataset_id)
            record = S.own_record(path, dataset_id)
        except LibraryError:
            continue                                         # vanished meanwhile
        try:
            state, reason, _ = S.decide(path, record, view)
        except Exception:  # noqa: BLE001 — the hub listing of a content decision failed
            view = {'state': 'unreachable'}
            try:
                state, reason, _ = S.decide(path, record, view)
            except Exception:  # noqa: BLE001 — the folder vanished meanwhile
                continue
        head = view.get('head') if view and view.get('state') == 'present' else None
        out[dataset_id] = {'state': state, 'reason': reason, 'head': head}
    for dataset_id, entry in listed.items():
        if dataset_id not in out:
            out[dataset_id] = {'state': 'online', 'reason': None, 'head': entry.get('head')}
    return out

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

"""The Daten 2.0 sync model (spec §C2, §C3, §E10) — ONE decision for the library
badge, the recording Start (D14) and the upload that decides at upload time.

Stdlib only. No ``huggingface_hub`` import: the hub view is built by the caller
(``hub_sync.hub_view``, the sidecar's ``hub_reads``) and handed in, so this
module runs in the node, the sidecar, the HF worker and the deps-free tests.

    decide(root, record, hub) -> (state, reason, repair)

``state``  ``current`` | ``changed`` | ``newer`` | ``conflict`` | ``local`` | ``unknown``
``reason`` only for ``unknown``: ``not_asked`` | ``unreachable`` | ``not_visible``
``repair`` None, or ``{'hub_sha', 'hub_trees'}`` when the hub holds OUR last
           commit whose record write was lost (recognised by the commit-title
           marker); the caller writes it into the record.

The sync point is main's head commit. „Online changed" ⇔ the head differs from
the recorded one AND the tree ids of ``data/``, ``meta/``, ``videos/`` differ
from the recorded ones (a README-only commit — a dataset-card edit on the
website — is no change). A dataset without a record is decided by content every
time (the descendant rule, P15): ``data/`` and ``meta/`` exactly by Hugging
Face's official fields (an LFS entry by ``lfs.sha256`` — its ``blob_id`` is the
POINTER's id —, any other by its git blob sha1), videos by size; the upload
verifies every file this decision assumed equal exactly (``assumed_equal``).

The sibling files of a dataset folder ``<root>/<ns>/<name>`` (never inside it,
so never uploaded and never part of a swap):
``<name>.sync.json`` (the record), ``.<name>.sync.next.json`` (a swap's next
record, H-8), ``<name>.session.json`` (the recorder's crash marker),
``.<name>.lock`` (the per-stage lock file, R-19), ``.<name>.journal.json`` (a
split's journal, R-19).
"""

from __future__ import annotations

from collections import Counter
import datetime
import hashlib
import json
import os
from pathlib import Path

RECORD_VERSION = 1
SYNC_DIRS = ('data', 'meta', 'videos')
SYNC_TOP = ('data/', 'meta/', 'videos/')
EPISODE_TOP = ('data/', 'meta/episodes/', 'videos/')   # files a resumed session never rewrites (P15)
MARKER_PREFIX = '[edubotics:'                          # = contract.MARKER_PREFIX
SESSION_MARKER_SUFFIX = '.session.json'                # = DataManager.SESSION_MARKER_SUFFIX
RECORD_SUFFIX = '.sync.json'
LFS_EXT = ('.parquet', '.mp4')                         # the hub stores these as LFS (P11)
_CHUNK = 1 << 20


# ── the dataset's sibling files ─────────────────────────────────────────────────

def folder_id(root) -> str:
    """The dataset id a folder stands for: ``<ns>/<name>`` of ``<root>/<ns>/<name>``."""
    root = Path(root)
    return f'{root.parent.name}/{root.name}'


def record_path(root) -> Path:
    root = Path(root)
    return root.parent / (root.name + RECORD_SUFFIX)


def record_next_path(root) -> Path:
    """The record a swap is about to make true, written BEFORE its first rename
    and promoted right after its second one (H-8); recovery promotes or drops it
    from the rename state alone (``hub_sync.recover``)."""
    root = Path(root)
    return root.parent / f'.{root.name}.sync.next.json'


def session_marker_path(root) -> Path:
    """The recorder's crash marker (``DataManager._session_marker_path``):
    present while a session runs and after one that never finalized."""
    root = Path(root)
    return root.parent / (root.name + SESSION_MARKER_SUFFIX)


def lock_path(root) -> Path:
    """``<ns>/.<name>.lock``: the per-stage lock file (R-19, G-3)."""
    root = Path(root)
    return root.parent / f'.{root.name}.lock'


def journal_path(root) -> Path:
    """``<ns>/.<name>.journal.json``: a split's two promotions as one transaction."""
    root = Path(root)
    return root.parent / f'.{root.name}.journal.json'


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# ── the record ─────────────────────────────────────────────────────────────────

def read_record(root):
    """The record, read tolerantly: absent, unreadable, not an object, another
    schema version → None."""
    try:
        rec = json.loads(record_path(root).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or rec.get('v') != RECORD_VERSION:
        return None
    return rec


def write_json_atomic(path, obj) -> None:
    """temp in the same dir + fsync + ``os.replace``."""
    path = Path(path)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    with open(tmp, 'wb') as f:
        f.write(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode('utf-8'))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_record(root, rec, path=None) -> None:
    write_json_atomic(path or record_path(root), rec)


def own_record(root, repo_id) -> dict:
    """The record only when ``repo_id`` is the folder's own id ``<ns>/<name>`` AND
    the record names it (G-11, H-7: an old page can upload folder X as repo Y;
    nothing about Y is ever read from or written into X's record). ``{}`` else."""
    if repo_id != folder_id(root):
        return {}
    rec = read_record(root)
    return rec if rec and rec.get('repo_id') == repo_id else {}


def update_record(root, repo_id, **changes):
    """Read-modify-write; a value of None removes the key. Only for the folder's
    own id (H-7): any other repo id writes nothing (returns None); a record that
    names another repo is invalid and is replaced."""
    if repo_id != folder_id(root):
        return None
    rec = read_record(root)
    if not rec or rec.get('repo_id') != repo_id:
        rec = {'v': RECORD_VERSION, 'repo_id': repo_id}
    for k, v in changes.items():
        if v is None:
            rec.pop(k, None)
        else:
            rec[k] = v
    write_record(root, rec)
    return rec


# ── hashing ────────────────────────────────────────────────────────────────────

def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(_CHUNK), b''):
            h.update(b)
    return h.hexdigest()


def git_sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(b'blob %d\x00' % len(data) + data).hexdigest()


def git_sha1_file(path) -> str:
    h = hashlib.sha1(b'blob %d\x00' % os.path.getsize(path))
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(_CHUNK), b''):
            h.update(b)
    return h.hexdigest()


def lfs_pointer(sha256_hex: str, size: int) -> bytes:
    """The git-lfs pointer file the hub's ``blob_id`` of an LFS file is the git
    sha1 of (P11: 171/171 LFS entries in 4 public repos)."""
    return f'version https://git-lfs.github.com/spec/v1\noid sha256:{sha256_hex}\nsize {size}\n'.encode()


def meta_digest(root) -> str:
    """sha256 over ``"<path>\\0<sha256(content)>\\n"`` of every file under ``meta/``
    in sorted order: THE local version id (the record's ``local_digest``, the
    commit-title marker, the sidecar's caches, the stale-page guard)."""
    root = Path(root)
    lines = []
    meta = root / 'meta'
    if meta.is_dir():
        for p in sorted(meta.rglob('*')):
            if p.is_file():
                lines.append(f'{p.relative_to(root).as_posix()}\0{sha256_file(p)}\n')
    return hashlib.sha256(''.join(lines).encode('utf-8')).hexdigest()


def marker(digest: str) -> str:
    return f'{MARKER_PREFIX}{digest[:16]}]'


def manifest(sha256_by_path) -> dict:
    """The record's ``files``: sha256 of every file under data/, meta/episodes/, videos/."""
    return {p: h for p, h in sorted(sha256_by_path.items()) if p.startswith(EPISODE_TOP)}


def files_manifest(root) -> dict:
    """The record's ``files`` of a folder on disk (one read of every episode file)."""
    root = Path(root)
    out = {}
    for top in SYNC_DIRS:
        base = root / top
        if not base.is_dir():
            continue
        for p in base.rglob('*'):
            rel = p.relative_to(root).as_posix()
            if p.is_file() and rel.startswith(EPISODE_TOP) and '.cache' not in p.relative_to(root).parts:
                out[rel] = sha256_file(p)
    return manifest(out)


# ── content comparison ─────────────────────────────────────────────────────────

def local_files(root) -> dict:
    """``{relative path: size}`` of every file under data/, meta/, videos/ (no .cache)."""
    root = Path(root)
    out = {}
    for top in SYNC_DIRS:
        base = root / top
        if not base.is_dir():
            continue
        for p in base.rglob('*'):
            if p.is_file() and '.cache' not in p.relative_to(root).parts:
                out[p.relative_to(root).as_posix()] = p.stat().st_size
    return out


def hub_entries(tree_items) -> dict:
    """``list_repo_tree(recursive=True)`` items → ``{path: {size, blob_id,
    lfs_sha256}}`` for the sync dirs (folders, which have no ``blob_id``, are
    skipped)."""
    out = {}
    for t in tree_items:
        if not hasattr(t, 'blob_id') or not str(t.path).startswith(SYNC_TOP):
            continue
        lfs = getattr(t, 'lfs', None)
        out[t.path] = {'size': t.size, 'blob_id': t.blob_id,
                       'lfs_sha256': getattr(lfs, 'sha256', None) if lfs else None}
    return out


def file_equal_exact(root, rel, entry) -> bool:
    p = Path(root) / rel
    if p.stat().st_size != entry['size']:
        return False
    if entry['lfs_sha256']:
        return sha256_file(p) == entry['lfs_sha256']
    return git_sha1_file(p) == entry['blob_id']


def file_equal_decision(root, rel, entry) -> bool:
    """The decision's rule: data/ and meta/ exact, videos by size (exactness
    moves to the upload, §E2 step 4)."""
    if rel.startswith('videos/'):
        return (Path(root) / rel).stat().st_size == entry['size']
    return file_equal_exact(root, rel, entry)


def content_decision(root, hub_files) -> str:
    """A dataset without a synced record, decided by content (the descendant
    rule, R-5; P15: a resumed session never rewrites an earlier data,
    meta-episodes or video file — it only rewrites meta/info.json,
    meta/stats.json and meta/tasks.parquet)."""
    loc = local_files(root)
    common = set(loc) & set(hub_files)
    equal = {p for p in common if file_equal_decision(root, p, hub_files[p])}
    if set(loc) == set(hub_files) and equal == common:
        return 'current'
    l_ep = {p for p in loc if p.startswith(EPISODE_TOP)}
    h_ep = {p for p in hub_files if p.startswith(EPISODE_TOP)}
    if h_ep <= equal and l_ep - h_ep:
        return 'changed'          # local = hub + more sessions
    if l_ep <= equal and h_ep - l_ep:
        return 'newer'            # hub = local + more sessions
    return 'conflict'


def assumed_equal(root, hub_files) -> list:
    """The files a content decision TREATED as equal without hashing (the common
    videos of equal size): the upload checks exactly these against lfs.sha256."""
    loc = local_files(root)
    return sorted(p for p in set(loc) & set(hub_files)
                  if p.startswith('videos/') and loc[p] == hub_files[p]['size'])


def ours(hub, digest) -> bool:
    """The hub's newest change of ``meta/`` is OUR commit made from exactly this
    local state (its title carries ``marker(digest)``), and neither ``data/``
    nor ``videos/`` changed after it."""
    last = (hub or {}).get('last') or {}
    m = last.get('meta')
    if not m or marker(digest) not in (m.get('title') or ''):
        return False
    for f in ('data', 'videos'):
        x = last.get(f)
        if x and not (x['oid'] == m['oid'] or x['date'] < m['date']):
            return False
    return True


def decide(root, record, hub):
    """hub: None (not asked) | ``{'state': 'unreachable'}`` |
    ``{'state': 'absent', 'complete': bool}`` |
    ``{'state': 'present', 'head', 'trees': {data, meta, videos}, 'last':
    {folder: {oid, title, date}}, 'files': callable -> hub_entries(...)}``.

    ``complete`` = the listing could see private repos of that namespace (the
    token's own account); a partner's namespace is listed public-only, so its
    absence proves nothing. A present repo with no data/, meta/, videos/ tree
    (an empty repo) counts as absent."""
    record = record if isinstance(record, dict) else None
    synced = bool(record and record.get('hub_sha'))
    digest = meta_digest(root)
    local_changed = synced and (record.get('local_digest') != digest or record.get('tag_ok') is False)
    state = (hub or {}).get('state')
    if hub is None or state == 'unreachable' or (state == 'absent' and not hub.get('complete')):
        if local_changed:
            return 'changed', None, None
        reason = 'not_asked' if hub is None else ('unreachable' if state == 'unreachable' else 'not_visible')
        return 'unknown', reason, None
    if state == 'absent' or not any((hub.get('trees') or {}).values()):
        return 'local', None, None
    if synced:
        hub_changed = hub['head'] != record['hub_sha'] and hub['trees'] != record.get('hub_trees')
        if hub_changed and ours(hub, digest):
            repair = {'hub_sha': hub['head'], 'hub_trees': hub['trees']}
            return ('changed' if record.get('tag_ok') is False else 'current'), None, repair
        return {(False, False): 'current', (False, True): 'newer',
                (True, False): 'changed', (True, True): 'conflict'}[(local_changed, hub_changed)], None, None
    state = content_decision(root, hub['files']())
    if state == 'current' and record and record.get('tag_ok') is False:   # an unconfirmed first upload
        state = 'changed'
    return state, None, None


# ── „Beide behalten" (three-way, G-2) ──────────────────────────────────────────

def plan_keep_both(local_ids, hub_ids, base_ids=None) -> list:
    """Three-way merge of two copies' episodes (G-2: what was deleted since the
    last sync stays deleted). Ids are ``v3_surgery.episode_identity`` values
    (data rows AND video packets, G-16). Per id with counts l (here), h
    (online), b (base; 0 without a base) keep ``clamp(l + h - b, 0, max(l, h))``
    copies — first from the local copy in its order, the rest from the hub copy
    in its order. So an episode deleted on either side since the base is gone,
    one added on either side is kept, the same take on both sides is kept once,
    and a take deliberately present twice in one copy stays twice. Without a
    base (a record-less dataset, or a base commit the hub no longer has) this is
    the union by multiplicity. Returns ``[('L'|'H', index), …]`` in output
    order."""
    cl, ch, cb = Counter(local_ids), Counter(hub_ids), Counter(base_ids or ())
    want = {x: max(0, min(cl[x] + ch[x] - cb[x], max(cl[x], ch[x]))) for x in set(cl) | set(ch)}
    out, used = [], Counter()
    for side, ids in (('L', local_ids), ('H', hub_ids)):
        for i, x in enumerate(ids):
            if used[x] < want[x]:
                out.append((side, i))
                used[x] += 1
    return out

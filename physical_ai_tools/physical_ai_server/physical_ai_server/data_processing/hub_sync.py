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

"""Daten 2.0 — the dataset's transactions on disk and on Hugging Face (spec §C2,
§D3, §E2, §E3, §E10).

* ``hub_view``: what the sync decision reads of a repo — main's head, the tree
  ids and last commits of ``data/``, ``meta/``, ``videos/`` (one
  ``list_repo_tree(expand=True)``), the recursive file listing on demand.
* ``upload``: EVERY upload of a dataset is ONE guarded commit — the local gate
  (crash marker, the record's ``files``, ``v3_surgery.integrity``), the
  namespace, the permission against the head the caller's decision saw, the
  exact check of every file a content decision assumed equal, the bytes
  (``preupload_lfs_files`` per file), ONE ``create_commit(parent_commit=head)``
  with the marker ``[edubotics:<meta digest[:16]>]`` FIRST in its title and the
  orphan deletes in the same commit, the read-back at the returned commit (its
  value is never trusted: huggingface_hub's no-op path returns main's current
  head with no parent check, G-1), the ``v3.0`` training pointer moved only to
  main's current head while main holds our data, the record. A failure is
  classified by RE-READING the hub, never by a status code.
* ``download``: the ONE dataset download (S-2) the Daten download worker runs —
  modes ``new`` | ``replace`` | ``copy`` (the page), ``sync`` (the recorder's
  D14/D7), ``keep`` | ``base`` („Beide behalten"); every file checked against
  the hub's ``lfs.sha256`` / git sha1 (audit m5), swapped in with its record.
* ``stage_lock`` / ``swap_in`` / ``recover``: the per-stage lock file (R-19,
  G-3) and the swap of a verified tmp copy into place together with the record
  that describes it (H-8), finished or undone from the rename state alone.

``huggingface_hub`` is imported inside the functions that use it, never at
module level (A18: the deps-free loaders stub it with a fixed attribute list,
and the sidecar must not pay for it). The stdlib siblings (``dataset_sync``,
``v3_surgery``) are reached through ``_sibling``: by package name in the image,
by file path when a deps-free loader gave the package no ``__path__``.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys


def _sibling(name):
    """A sibling module of this package: by package name in the image, by file
    path when a deps-free test loader gave the package no ``__path__``."""
    try:
        return importlib.import_module(f'physical_ai_server.data_processing.{name}')
    except ImportError:
        key = f'_edubotics_dp_{name}'
        module = sys.modules.get(key)
        if module is None:
            spec = importlib.util.spec_from_file_location(key, str(Path(__file__).with_name(f'{name}.py')))
            module = importlib.util.module_from_spec(spec)
            sys.modules[key] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(key, None)
                raise
        return module


S = _sibling('dataset_sync')

# The tmp/bak/trash suffixes of a dataset's transactions (contract.RESERVED_SUFFIXES).
TMP_SUFFIXES = ('.tmp_sync', '.tmp_keep', '.tmp_base', '.tmp_edit')
BAK_SUFFIXES = ('.bak_sync', '.bak_edit')
TRASH_SUFFIX = '.trash_edit'
# = contract.TAG / TAG_RETRIES / READBACK_TRIES (lockstep-tested). LeRobot 0.5.1
# trains at revision CODEBASE_VERSION == 'v3.0'.
TAG = 'v3.0'
TAG_RETRIES = 3
READBACK_TRIES = 3
# = signal_status.DISK_START_FLOOR_BYTES: a download must leave room to record.
DISK_START_FLOOR_BYTES = 3_000_000_000
DOWNLOAD_MODES = ('new', 'replace', 'copy', 'sync', 'keep', 'base')
UNSET = object()


class Refused(Exception):
    """A refusal of the hub side; ``code`` is the machine code (``namespace``,
    ``hub_changed``, ``hub_differs``, ``in_session``, ``local_broken`` or a
    download code: ``exists``, ``stale``, ``invalid``, ``disk``, ``broken``,
    ``old_format``, ``other_robot``, ``unsupported``, ``not_found``); ``extra``
    carries numbers a caller may show (``free``/``need``)."""

    def __init__(self, code, detail='', **extra):
        super().__init__(f'{code}: {detail}' if detail else code)
        self.code = code
        self.detail = detail
        self.extra = extra


# ── the per-stage lock and the swap transaction ─────────────────────────────

def stage_lock(root):
    """``<ns>/.<name>.lock``, ``flock(LOCK_EX | LOCK_NB)``, held by the process of
    ONE stage (G-3: a job never holds it across its child stages — a flock is per
    open file, so its own child stage would fail). Returns the fd; raises
    ``BlockingIOError`` when another stage/process owns it. The kernel releases
    it when the process dies."""
    root = Path(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(S.lock_path(root), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise
    return fd


def release_lock(fd):
    try:
        os.close(fd)
    except OSError:
        pass


def swap_in(tmp, target, rec, tmp_suffix, bak_suffix, check=None):
    """``tmp`` → ``target`` with the record made true in the same transaction
    (H-8): the record that will describe ``target`` is written first as
    ``.<name>.sync.next.json`` (it names the swap's tmp/bak suffixes), then
    ``target → bak``, ``tmp → target``, the record promoted, the crash marker
    removed (H-1: the content it warned about is gone), the bak dropped.
    ``rec`` None writes no record. ``check(target)`` (optional) re-verifies the
    promoted tree before the record is promoted; a failure rolls the swap back
    and re-raises. ``recover`` finishes or undoes any break."""
    target = Path(target)
    nxt = S.record_next_path(target)
    S.write_json_atomic(nxt, {'record': rec, 'tmp': tmp_suffix, 'bak': bak_suffix})
    bak = Path(f'{target}{bak_suffix}') if target.exists() else None
    if bak:
        shutil.rmtree(bak, ignore_errors=True)
        target.rename(bak)
    promoted = False
    try:
        Path(tmp).rename(target)
        promoted = True
        if check is not None:
            check(target)
    except BaseException:
        # roll back: the old copy (or nothing) at target, no next record, no tmp
        if promoted:
            shutil.rmtree(target, ignore_errors=True)
        if bak and bak.exists() and not target.exists():
            bak.rename(target)
        nxt.unlink(missing_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    _promote_next(target, nxt)
    if bak:
        shutil.rmtree(bak, ignore_errors=True)


def _promote_next(target, nxt):
    try:
        j = json.loads(Path(nxt).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return
    if isinstance(j, dict) and j.get('record') is not None:
        S.write_record(target, j['record'])
    Path(nxt).unlink(missing_ok=True)
    S.session_marker_path(target).unlink(missing_ok=True)


def recover(target):
    """One dataset's recovery (boot recovery, and the download supervisor right
    after it killed a worker, H-9), under the dataset's lock (the caller's):
    decided by the rename state alone, never by hashing (P32's four states).

    * ``X.bak_*`` with no ``X``: the swap broke between its two renames → the bak
      goes back to ``X``, the next record is dropped;
    * a next record whose swap tmp is gone while ``X`` exists: the second rename
      happened → ``X`` is the verified new copy, its record is promoted and the
      crash marker removed;
    * a next record whose tmp still exists: the swap never started → dropped;
    * ``X.bak_*`` WITH ``X``: removed; every tmp dir of ``X``: removed (LAST, so a
      caller that recovers before removing the tmp sees the true state, U-1)."""
    target = Path(target)
    nxt = S.record_next_path(target)
    try:
        j = json.loads(nxt.read_text(encoding='utf-8'))
        if not isinstance(j, dict):
            j = None
    except (OSError, ValueError):
        j = None
    baks = [Path(f'{target}{x}') for x in BAK_SUFFIXES]
    bak = next((b for b in baks if b.exists()), None)
    if bak and not target.exists():                   # broke between the two renames: roll back
        bak.rename(target)
        nxt.unlink(missing_ok=True)
    elif j and target.exists() and not Path(f'{target}{j.get("tmp") or ".tmp_none"}').exists():
        _promote_next(target, nxt)                    # tmp -> target happened: the new copy is true
    else:
        nxt.unlink(missing_ok=True)                   # the swap never started (or never wrote a record)
    for b in baks:
        if b.exists() and target.exists():
            shutil.rmtree(b, ignore_errors=True)
    for x in TMP_SUFFIXES:
        shutil.rmtree(f'{target}{x}', ignore_errors=True)


# ── the hub view ───────────────────────────────────────────────────────────────

def is_not_found(error) -> bool:
    """R-12: „not there / no access“ — a ``RepositoryNotFoundError`` of ANY
    status (401 anonymous, 404 with a token; ``GatedRepoError`` is one) anywhere
    in the cause chain. By class name, so a stubbed huggingface_hub (the
    deps-free loaders) needs no such class."""
    seen = 0
    while error is not None and seen < 8:
        if any(c.__name__ == 'RepositoryNotFoundError' for c in type(error).__mro__):
            return True
        error = error.__cause__ or error.__context__
        seen += 1
    return False


def head_of(api, repo):
    """main's head commit (``list_repo_refs``); raises the library's error,
    ``RepositoryNotFoundError`` included."""
    refs = api.list_repo_refs(repo, repo_type='dataset')
    head = next((b.target_commit for b in refs.branches if b.name == 'main'), None)
    if not head:
        raise Refused('not_found', 'no main branch')
    return head


def tree_view(api, repo, head):
    """``{'trees': {data, meta, videos}, 'last': {folder: {oid, title, date}}}`` at
    ``head`` from ONE top-level ``list_repo_tree(expand=True)`` (P11: folders
    carry a content-addressed ``tree_id`` and, expanded, their last commit)."""
    trees, last = {k: None for k in S.SYNC_DIRS}, {}
    for e in api.list_repo_tree(repo, repo_type='dataset', revision=head, expand=True):
        if e.path in S.SYNC_DIRS and hasattr(e, 'tree_id'):
            trees[e.path] = e.tree_id
            lc = getattr(e, 'last_commit', None)
            if lc is not None:
                date = lc.date.isoformat() if hasattr(lc.date, 'isoformat') else str(lc.date)
                last[e.path] = {'oid': lc.oid, 'title': lc.title, 'date': date}
    return {'trees': trees, 'last': last}


def file_listing(api, repo, revision):
    """The recursive listing at ``revision`` as ``dataset_sync.hub_entries``."""
    return S.hub_entries(api.list_repo_tree(repo, repo_type='dataset', recursive=True, revision=revision))


def hub_view(api, repo, *, complete=True, head=None, strict=False):
    """The hub side of ``dataset_sync.decide``. ``head`` may come from
    ``list_datasets().sha`` (the library's fast path). A missing or unreadable
    repo is ``{'state': 'absent', 'complete': complete}`` — a
    ``RepositoryNotFoundError`` of ANY status, 401 included (R-12) — unless
    ``strict`` (the upload: the caller classifies the library's own error). The
    recursive ``files`` listing is fetched lazily, at most once."""
    try:
        head = head or head_of(api, repo)
        view = tree_view(api, repo, head)
    except Exception as e:  # noqa: BLE001 — not-found FIRST (R-12); everything else is the caller's
        if strict or not is_not_found(e):
            raise
        return {'state': 'absent', 'complete': complete}
    cache = {}

    def files():
        if 'files' not in cache:
            cache['files'] = file_listing(api, repo, head)
        return cache['files']
    return {'state': 'present', 'head': head, 'trees': view['trees'], 'last': view['last'], 'files': files}


def _read_back(api, repo, head=None):
    """The hub view, retried; None when it cannot be read (G-13)."""
    for _ in range(READBACK_TRIES):
        try:
            v = hub_view(api, repo, head=head, strict=True)
            if v.get('state') == 'present':
                return v
        except Exception:  # noqa: BLE001 — retried, then reported as unreadable
            continue
    return None


# ── the local gate (audit M3, m2) ──────────────────────────────────────────────

def _gate_cheap(root):
    root = Path(root)
    if S.session_marker_path(root).exists():
        raise Refused('in_session', 'crash marker')
    if not (root / 'meta' / 'info.json').is_file():
        raise Refused('local_broken', 'no meta/info.json')


def local_gate(root, rec, sha=None):
    """The local gate of every upload path: no crash marker; every file the
    record vouches for (``files``: data/, meta/episodes/, videos/ as last known
    good) still has its sha256 — an already-synced video included; the dataset
    loads and every episode is whole (``v3_surgery.integrity``, which does not
    re-read the vouched-for videos). ``sha`` = {path: sha256} when the caller
    hashed the files anyway (the upload); else the vouched-for files are hashed."""
    V = _sibling('v3_surgery')
    root = Path(root)
    _gate_cheap(root)
    known = (rec or {}).get('files') or {}
    if sha is None:
        sha = {p: S.sha256_file(root / p) for p in known if (root / p).is_file()}
    changed = sorted(p for p, h in known.items() if sha.get(p) != h)
    if changed:
        raise Refused('local_broken', f'changed on disk since the last sync: {changed[0]}')
    try:
        V.integrity(root, known_good=frozenset(known))
    except V.SurgeryError as e:
        raise Refused('local_broken', str(e)) from e


# ── the guarded single-commit upload (§E2) ─────────────────────────────────────

def account_of(api):
    return api.whoami()['name']


def _local_ops(root, hub_files):
    """One addition per local file under data/, meta/, videos/ plus README.md
    (``CommitOperationAdd`` hashes each file now), one deletion per hub file
    under data/, meta/, videos/ the local copy no longer has."""
    from huggingface_hub import CommitOperationAdd, CommitOperationDelete
    root = Path(root)
    adds = []
    for p in sorted(root.rglob('*')):
        rel = p.relative_to(root).as_posix()
        if p.is_file() and (rel.startswith(S.SYNC_TOP) or rel == 'README.md') and '.cache' not in rel.split('/'):
            adds.append(CommitOperationAdd(path_in_repo=rel, path_or_fileobj=str(p)))
    local = {a.path_in_repo for a in adds}
    dels = [CommitOperationDelete(path_in_repo=f) for f in sorted(hub_files)
            if f.startswith(S.SYNC_TOP) and f not in local]
    return adds, dels


def move_tag(api, repo, new, trees_new):
    """m4/G-13/H-6: ``v3.0`` is only ever pointed at main's CURRENT head. Before
    our first tag write, main holding other data than our commit (a newer
    upload; a card edit leaves the trees equal) → ``'newer'``: that commit's own
    upload moves the tag. Once we have written the tag, every attempt ends at
    main's current head (a racing upload may have tagged inside our delete/create,
    and a failed re-read must not leave our older commit behind main). Returns
    ``'ok'`` | ``'newer'`` | ``'failed'``."""
    from huggingface_hub.errors import RevisionNotFoundError
    wrote = False
    for _ in range(TAG_RETRIES):
        try:
            cur = head_of(api, repo)
            if not wrote and cur != new and tree_view(api, repo, cur)['trees'] != trees_new:
                return 'newer'
            for _ in range(3):
                try:
                    api.delete_tag(repo, tag=TAG, repo_type='dataset')
                except RevisionNotFoundError:
                    pass
                api.create_tag(repo, tag=TAG, revision=cur, repo_type='dataset')
                wrote = True
                after = head_of(api, repo)
                if after == cur:
                    return 'ok'
                cur = after                               # main moved while we tagged: follow it
            return 'ok'                                   # still moving: the last mover's own step follows it
        except Exception:  # noqa: BLE001 — retried, then reported
            continue
    return 'failed'


def upload(root, repo, *, expected=UNSET, private=True, api=None, progress=None, write_card=None):
    """The ONE dataset upload. ``expected``: the head the caller's decision saw
    (the Start check, the upload dialog); ``None`` = the hub must hold no
    dataset; ``UNSET`` = decide here and now (the old page, a Start that could
    not ask the hub). ``write_card(root, repo, private)`` writes ``README.md``
    with the repo's REAL visibility before the operations are built.
    ``progress(done, total)`` after every pre-uploaded file.

    Returns ``{'commit', 'tag': 'ok'|'newer'|'failed'|'unconfirmed', 'tag_ok',
    'unconfirmed', 'private', 'files'}``. Raises ``Refused`` (``in_session``,
    ``local_broken``, ``namespace``, ``hub_changed``, ``hub_differs``) or the
    library's own error (the caller classifies it); in every raising case
    nothing of ours was committed."""
    from huggingface_hub import HfApi
    api = api or HfApi()
    root = Path(root)
    _gate_cheap(root)                                     # M3: before any network call
    rec = S.own_record(root, repo)                        # G-11: another repo's record is ignored
    if repo.split('/')[0] != account_of(api):             # R-10: the token's ACCOUNT only
        raise Refused('namespace')
    api.create_repo(repo, repo_type='dataset', private=bool(private), exist_ok=True)
    hub = hub_view(api, repo, strict=True)
    head = hub['head']
    real_private = bool(api.dataset_info(repo, revision=head).private)   # H-13: the repo's REAL visibility
    exists = any(hub['trees'].values())
    if expected is UNSET:
        state, _, _ = S.decide(root, rec, hub)
        if state not in ('current', 'changed', 'local'):
            raise Refused('hub_differs', state)
    elif expected is None:
        if exists:
            raise Refused('hub_changed', 'a dataset exists')
    elif head != expected and not (rec.get('hub_sha') == expected and hub['trees'] == rec.get('hub_trees')):
        raise Refused('hub_changed')
    hub_files = hub['files']() if exists else {}
    check_exact = S.assumed_equal(root, hub_files) if exists and not rec.get('hub_sha') else []   # S-1
    if write_card is not None:
        write_card(root, repo, real_private)
    digest = S.meta_digest(root)
    adds, dels = _local_ops(root, hub_files)
    sha = {a.path_in_repo: a.upload_info.sha256.hex() for a in adds}
    local_gate(root, rec, sha)                            # M3 + m2, on the hashes CommitOperationAdd computed
    for p in check_exact:                                 # nothing is overwritten on a guess (record-less)
        want = hub_files.get(p, {}).get('lfs_sha256')
        if want and sha.get(p) != want:
            raise Refused('hub_differs', f'exact check {p}')
    for i, a in enumerate(adds):                          # the bytes, per file (progress + stall watch)
        api.preupload_lfs_files(repo, additions=[a], repo_type='dataset')
        if progress:
            progress(i + 1, len(adds))
    title = f'{S.marker(digest)} EduBotics: {root.name}'   # G-13: the marker first
    try:
        new = api.create_commit(repo, repo_type='dataset', operations=adds + dels, parent_commit=head,
                                commit_message=title).oid
    except Exception as e:  # noqa: BLE001 — classified by the hub's state, never by a status code
        now = _read_back(api, repo)
        if now is not None and now['head'] != head and S.ours(now, digest):
            new, after = now['head'], now                 # it landed; only the response was lost
        elif now is not None and now['head'] != head:
            raise Refused('hub_changed', 'parent_commit refused') from e
        else:
            raise
    else:
        after = _read_back(api, repo, head=new)           # G-1: never trust the returned commit
        if after is None:                                 # G-13: landed, only the confirmation failed
            S.update_record(root, repo, tag_ok=False)     # the card reads `changed`; the next upload settles it
            return {'commit': new, 'tag': 'unconfirmed', 'tag_ok': False, 'unconfirmed': True,
                    'private': real_private, 'files': S.manifest(sha)}
        if new != head and not S.ours(after, digest) and after['trees'] != hub['trees']:
            raise Refused('hub_changed', 'the no-op return names a main that moved to other data')
    tag = move_tag(api, repo, new, after['trees'])
    S.update_record(root, repo, hub_sha=new, hub_trees=after['trees'], local_digest=digest,
                    files=S.manifest(sha), synced_at=S.now_iso(), tag_ok=False if tag == 'failed' else None)
    return {'commit': new, 'tag': tag, 'tag_ok': tag != 'failed', 'unconfirmed': False,
            'private': real_private, 'files': S.manifest(sha)}


# ── the ONE dataset download (§E3, S-2) ────────────────────────────────────────

def _verify_against_listing(tmp, listing, progress=None):
    """m5: every listed file is there with the hub's bytes (``lfs.sha256``, else
    git sha1 == ``blob_id``) and nothing else is. Returns {path: sha256}.
    ``progress(bytes_hashed)`` after every file is the heartbeat the supervisor
    counts as progress while nothing new arrives in the tmp (H-10)."""
    tmp = Path(tmp)
    have = {p.relative_to(tmp).as_posix() for p in tmp.rglob('*')
            if p.is_file() and '.cache' not in p.relative_to(tmp).parts}
    if have != set(listing):
        raise Refused('broken', f'files {sorted(have ^ set(listing))[:3]}')
    out, done = {}, 0
    for rel, e in sorted(listing.items()):
        p = tmp / rel
        size = p.stat().st_size
        h256, h1 = hashlib.sha256(), hashlib.sha1(b'blob %d\x00' % size)
        with open(p, 'rb') as f:
            for b in iter(lambda: f.read(1 << 20), b''):
                h256.update(b)
                h1.update(b)
        good = h256.hexdigest() == e['lfs_sha256'] if e['lfs_sha256'] else h1.hexdigest() == e['blob_id']
        if size != e['size'] or not good:
            raise Refused('broken', f'bytes {rel}')
        out[rel] = h256.hexdigest()
        done += size
        if progress:
            progress(done)
    return out


def _listing(api, repo, revision, allow_top=None):
    out = {}
    for t in api.list_repo_tree(repo, repo_type='dataset', recursive=True, revision=revision):
        if not hasattr(t, 'blob_id') or '.cache' in t.path.split('/'):
            continue
        if allow_top and not t.path.startswith(allow_top):
            continue
        lfs = getattr(t, 'lfs', None)
        out[t.path] = {'size': t.size, 'blob_id': t.blob_id,
                       'lfs_sha256': getattr(lfs, 'sha256', None) if lfs else None}
    return out


_TMP_OF_MODE = {'keep': '.tmp_keep', 'base': '.tmp_base'}


def tmp_path_of(target, mode):
    """The tmp copy a download of ``mode`` lands in, beside ``target``."""
    return Path(f'{target}{_TMP_OF_MODE.get(mode, ".tmp_sync")}')


def _disk_free(path):
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    return shutil.disk_usage(str(p)).free


def _link_base_videos(api, repo, base, root, keep_dir, bdir, token):
    """The base copy's video files: linked from this copy or the hub copy when a
    file with the same path has the base's ``lfs.sha256`` (always, unless both
    sides rewrote it by an edit), else fetched at the base revision and
    checked. Returns the number fetched."""
    from huggingface_hub import snapshot_download
    vids = _listing(api, repo, base, ('videos/',))
    missing = []
    for rel, e in vids.items():
        dst = Path(bdir) / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        for cand in (Path(root) / rel, Path(keep_dir) / rel):
            if cand.is_file() and cand.stat().st_size == e['size'] and S.sha256_file(cand) == e['lfs_sha256']:
                try:
                    os.link(cand, dst)
                except OSError:
                    shutil.copyfile(cand, dst)
                break
        else:
            missing.append(rel)
    if missing:
        snapshot_download(repo, repo_type='dataset', revision=base, local_dir=str(bdir),
                          allow_patterns=missing, token=token)
        shutil.rmtree(Path(bdir) / '.cache', ignore_errors=True)
        for rel in missing:
            if S.sha256_file(Path(bdir) / rel) != vids[rel]['lfs_sha256']:
                raise Refused('broken', f'base bytes {rel}')
    return len(missing)


def download(api, repo, revision, target, *, mode, robot_type=None, display_name=None, progress=None,
             meta_digest=None, token=None, disk_floor=DISK_START_FLOOR_BYTES, after_check=None):
    """The ONE dataset download. ``mode``: the page's ``new`` | ``replace`` |
    ``copy``; the recorder's ``sync`` (D14 „newer", D7 „exists":
    replace-or-create, record written); „Beide behalten"'s ``keep`` (the hub copy
    into ``<target>.tmp_keep``, the upload's local gate first, checked, no swap,
    no record) and ``base`` (``data/`` + ``meta/`` of the base commit into
    ``<target>.tmp_base``, its videos linked from this copy or the hub copy when
    the hashes match, else fetched; no record). ``revision`` None = main's head,
    resolved here. ``replace`` must carry the ``meta_digest`` of the local copy
    the page showed (T-1 c, checked under the dataset's lock). Every request is
    made with ``token``. ``after_check(tmp)`` (tests) runs after the check.

    Returns ``{'revision', 'dir', 'files', 'trees', 'private', 'fetched'}`` (+
    ``'no_base': True`` for a base the hub no longer has). Raises ``Refused``
    (``invalid``, ``exists``, ``stale``, ``not_found``, ``disk`` (with
    ``free``/``need``), ``broken``, ``old_format``, ``other_robot``,
    ``unsupported``, ``in_session``, ``local_broken``, ``namespace``) or the
    library's own error; the tmp is always removed on failure."""
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import RepositoryNotFoundError, RevisionNotFoundError
    V = _sibling('v3_surgery')
    if mode not in DOWNLOAD_MODES:
        raise Refused('invalid', f'mode {mode}')
    target = Path(target)
    if mode == 'replace' and not meta_digest:
        raise Refused('invalid', 'replace without meta_digest')
    tmp = tmp_path_of(target, mode)
    lock = stage_lock(target)                             # this stage's own lock (G-3)
    try:
        if mode in ('new', 'copy') and target.exists():
            raise Refused('exists')
        if mode == 'replace':                             # T-1: never over a copy the page did not see
            if not target.exists():
                raise Refused('not_found', 'no local copy')
            if S.meta_digest(target) != meta_digest:
                raise Refused('stale')
        if mode == 'keep':                                # an upload path: refused before anything is fetched
            local_gate(target, S.own_record(target, repo))
            if repo.split('/')[0] != account_of(api):     # H-3: a partner's dataset is never merged here
                raise Refused('namespace')
        try:
            revision = revision or head_of(api, repo)
            info = api.dataset_info(repo, revision=revision, files_metadata=True)
        except RevisionNotFoundError:
            if mode == 'base':                            # squashed history: no base -> the union
                return {'revision': revision, 'dir': None, 'files': {}, 'trees': None, 'private': None,
                        'fetched': 0, 'no_base': True}
            raise Refused('not_found', 'revision')
        except RepositoryNotFoundError:                   # any status, 401 included (R-12)
            raise Refused('not_found')
        allow = ('data/', 'meta/') if mode == 'base' else None
        need = sum(int(getattr(f, 'size', 0) or 0) for f in (info.siblings or [])
                   if not allow or str(f.rfilename).startswith(allow))
        free = _disk_free(target.parent)
        if disk_floor is not None and free - need < disk_floor:
            raise Refused('disk', free=free, need=need)
        shutil.rmtree(tmp, ignore_errors=True)
        listing = _listing(api, repo, revision, allow)
        snapshot_download(repo, repo_type='dataset', revision=revision, local_dir=str(tmp),
                          allow_patterns=['data/**', 'meta/**'] if allow else None, token=token)
        shutil.rmtree(tmp / '.cache', ignore_errors=True)
        sha = _verify_against_listing(tmp, listing, progress)   # m5
        fetched = 0
        if mode == 'base':
            fetched = _link_base_videos(api, repo, revision, target, tmp_path_of(target, 'keep'), tmp, token)
        else:
            try:
                meta = json.loads((tmp / 'meta' / 'info.json').read_text(encoding='utf-8'))
            except (OSError, ValueError) as e:
                raise Refused('broken', 'meta/info.json') from e
            if not str(meta.get('codebase_version', '')).startswith('v3'):
                raise Refused('old_format')
            if robot_type and meta.get('robot_type') != robot_type:
                raise Refused('other_robot')
            try:
                V.Source(tmp)
                from lerobot.datasets.lerobot_dataset import LeRobotDataset
                LeRobotDataset('download/check', root=tmp)
            except V.SurgeryError as e:
                raise Refused('unsupported' if e.code == 'unsupported' else 'broken', str(e)) from e
            except Exception as e:  # noqa: BLE001
                raise Refused('broken', type(e).__name__) from e
        if after_check is not None:
            after_check(tmp)
        trees = tree_view(api, repo, revision)['trees'] if mode != 'base' else None
        private = bool(getattr(info, 'private', False))
        if mode in ('keep', 'base'):
            return {'revision': revision, 'dir': tmp, 'files': sha, 'trees': trees, 'private': private,
                    'fetched': fetched}
        own = S.folder_id(target)
        if mode == 'copy':                                # D12: a copy remembers its source (G-10)
            rec = {'v': S.RECORD_VERSION, 'repo_id': own, 'source': {'repo_id': repo, 'sha': revision},
                   'files': S.manifest(sha), 'private': private}
            if display_name:
                rec['display_name'] = display_name
        elif repo == own:                                 # H-7: a record only for the folder's own repo
            keep = {k: S.own_record(target, repo).get(k) for k in ('display_name', 'private')}
            rec = {'v': S.RECORD_VERSION, 'repo_id': repo, 'hub_sha': revision, 'hub_trees': trees,
                   'local_digest': S.meta_digest(tmp), 'files': S.manifest(sha), 'synced_at': S.now_iso(),
                   **{k: v for k, v in keep.items() if v is not None}}
            if display_name and 'display_name' not in rec:
                rec['display_name'] = display_name
        else:
            rec = None
        swap_in(tmp, target, rec, '.tmp_sync', '.bak_sync')
        return {'revision': revision, 'dir': target, 'files': sha, 'trees': trees, 'private': private,
                'fetched': 0}
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    finally:
        release_lock(lock)

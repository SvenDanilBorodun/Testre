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

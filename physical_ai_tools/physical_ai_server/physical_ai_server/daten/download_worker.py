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

"""The ONE dataset download process (spec §E3, S-2).

Started by the DatenService download runner as
``nice -n 10 python3 -m physical_ai_server.daten.download_worker`` with
``HF_HUB_DISABLE_XET=1`` (so the bytes land in the tmp progressively, P25) for
the page's downloads (``new``/``replace``/``copy``), the recorder's D14/D7
sync download (``sync``) and „Beide behalten"'s hub and base copies
(``keep``/``base``). It is NOT the HF worker: a Daten download never blocks
``/register_hf_user``.

Request: one JSON line on stdin ``{repo_id, revision, target_dir, mode,
display_name, robot_type, token_fp, private, meta_digest}``. Output: ``DL_PROGRESS::
{"stage", "total"|"done"}`` lines (the size once known; while the files are
checked, the bytes hashed so far — the supervisor's stall rule counts that as
progress, H-10) and ONE ``DL_RESULT::{"ok": true, "revision", …}`` /
``{"ok": false, "code", …}`` line. Every print tolerates a closed pipe (R-19).

The token is read once from the robot's slot; its fingerprint must be the
request's (the student who started it), and it is passed explicitly to every
call. The worker watches for itself (G-14): a changed fingerprint, or a parent
that died (a node respawn — this process runs in its own session and would
outlive it with the previous student's token), removes its tmp and exits
(``token_changed`` / ``orphaned``). The watch never cuts a swap in half: the
swap holds the exit guard, and a watch that fires then waits for it and exits
without a result of its own once the swap's result is out.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import threading

POLL_S = 0.5                           # = contract.DOWNLOAD_POLL_S (lockstep-tested)
RESULT_PREFIX = 'DL_RESULT::'
PROGRESS_PREFIX = 'DL_PROGRESS::'
_exit_guard = threading.Lock()          # held by the swap, by the result line and by a firing watch
_finished = threading.Event()
_held = {'swap': False}


def _sibling(package, name):
    """A module of this package: by name in the image, by file path for the
    deps-free loaders."""
    try:
        return importlib.import_module(f'physical_ai_server.{package}.{name}')
    except ImportError:
        key = f'_edubotics_dl_{package}_{name}'
        module = sys.modules.get(key)
        if module is None:
            path = Path(__file__).resolve().parent.parent / package / f'{name}.py'
            spec = importlib.util.spec_from_file_location(key, str(path))
            module = importlib.util.module_from_spec(spec)
            sys.modules[key] = module
            spec.loader.exec_module(module)
        return module


def _say(prefix, obj):
    """One protocol line, on a line of its own. The supervisor merges stderr
    into this pipe, and snapshot_download's progress bar (``\\r`` + text, no
    newline while it runs) may stand unfinished when the watch prints its result
    from another thread (V1-1): the leading newline ends that line, so the
    marker starts its own. The whole line goes out in ONE write (atomic on a
    pipe below PIPE_BUF), so nothing lands inside it."""
    try:
        sys.stdout.write('\n' + prefix + json.dumps(obj) + '\n')
        sys.stdout.flush()
    except (BrokenPipeError, OSError, ValueError):          # R-19: a closed pipe never kills the transaction
        pass


def _slot_fp(store):
    token = store.read()
    return store.fingerprint(token) if token else None


def classify(error):
    """A library error's result code: ``not_found`` FIRST (any status, R-12),
    then ``auth`` (401/403), else ``unreachable``; ``internal`` when the hub
    library is not the cause."""
    hub_sync = _sibling('data_processing', 'hub_sync')
    if hub_sync.is_not_found(error):
        return 'not_found'
    kind = _sibling('data_processing', 'hf_errors').classify_hf_error(error)
    if kind == 'auth':
        return 'auth'
    if kind in ('network', 'server', 'busy'):
        return 'unreachable'
    return 'internal'


def _watch(req, tmp, parent, store, poll_s=POLL_S):
    """G-14: the slot changed, or the supervisor died → remove the tmp, exit."""
    while True:
        threading.Event().wait(poll_s)
        why = None
        if _slot_fp(store) != req.get('token_fp'):
            why = 'token_changed'
        elif os.getppid() != parent:
            why = 'orphaned'
        if why:
            with _exit_guard:                                # never in the middle of a swap
                if _finished.is_set():                       # the result is out: nothing left to stop
                    return
                shutil.rmtree(tmp, ignore_errors=True)
                _say(RESULT_PREFIX, {'ok': False, 'code': why})
                os._exit(3)


def run(req, store=None, api_factory=None, watch=True):
    """One download. Returns the result dict (also printed)."""
    hub_sync = _sibling('data_processing', 'hub_sync')
    store = store or _sibling('data_processing', 'hf_token_store')
    _finished.clear()
    mode = req.get('mode')
    target = req.get('target_dir') or ''
    if mode not in hub_sync.DOWNLOAD_MODES or not target:
        return _finish({'ok': False, 'code': 'invalid'})
    if _slot_fp(store) != req.get('token_fp') or not req.get('token_fp'):
        return _finish({'ok': False, 'code': 'token_changed'})
    token = store.read()
    tmp = hub_sync.tmp_path_of(target, mode)
    if watch:
        threading.Thread(target=_watch, args=(req, tmp, os.getppid(), store), daemon=True,
                         name='daten-download-watch').start()
    if api_factory is None:
        from huggingface_hub import HfApi
        api = HfApi(token=token)
    else:
        api = api_factory(token)
    try:
        r = hub_sync.download(
            api, req['repo_id'], req.get('revision') or None, Path(target), mode=mode,
            robot_type=req.get('robot_type') or None, display_name=req.get('display_name') or None,
            meta_digest=req.get('meta_digest') or None, token=token,
            progress=lambda done: _say(PROGRESS_PREFIX, {'stage': 'verify', 'done': int(done)}),
            on_total=lambda total: _say(PROGRESS_PREFIX, {'stage': 'download', 'total': int(total)}),
            before_swap=_before_swap)
    except hub_sync.Refused as e:
        out = {'ok': False, 'code': e.code}
        out.update({k: v for k, v in (e.extra or {}).items() if k in ('free', 'need')})
        return _finish(out)
    except Exception as e:  # noqa: BLE001 — the library's own error, classified
        print(f'download failed: {type(e).__name__}: {e}', file=sys.stderr, flush=True)
        return _finish({'ok': False, 'code': classify(e)})
    return _finish({'ok': True, 'revision': r['revision'], 'trees': r.get('trees'),
                    'private': r.get('private'), 'fetched': r.get('fetched', 0),
                    'no_base': bool(r.get('no_base')),
                    'xet_disabled': os.environ.get('HF_HUB_DISABLE_XET') == '1'})


def _before_swap():
    _exit_guard.acquire()
    _held['swap'] = True


def _finish(result):
    """Print the ONE result line under the exit guard (held already when a swap
    ran), then release it: a watch that fires afterwards prints nothing."""
    if not _held['swap']:
        _exit_guard.acquire()
    try:
        if not _finished.is_set():
            _say(RESULT_PREFIX, result)
            _finished.set()
    finally:
        _held['swap'] = False
        _exit_guard.release()
    return result


def main():
    try:
        req = json.loads(sys.stdin.readline() or '{}')
        if not isinstance(req, dict):
            raise ValueError('request is not an object')
    except ValueError:
        _finish({'ok': False, 'code': 'invalid'})
        return 2
    result = run(req)
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())

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

"""Out-of-process runner for dataset edits (delete / split / merge / union).

WHY A PROCESS (2026-06-07): an edit used to run inside the ``/dataset/edit``
ROS callback and could saturate every core for minutes (a LeRobot re-encode),
starving the node's executor so the whole dashboard went dead. The Daten
service (``daten/node_service.py``) and the old ``/dataset/edit`` path both run
this module as a ``nice -n 19`` subprocess (payload on stdin, one
``EDIT_RESULT::`` line on stdout) — the Daten 2.0 engine is a stream copy now
(seconds, not minutes), but the edit still reads and writes gigabytes, and the
low priority keeps it out of the recorder's way.

Protocol (stdout, every print tolerates a closed pipe, R-19 — a worker that
outlives a node respawn finishes its transaction instead of dying between two
renames):

* ``EDIT_PROGRESS::{"stage": "prepare|copy|verify|swap", "done": k, "total": n}``
* ``EDIT_RESULT::{"success": bool, "message": "<German>", "code": "<JOB_FAIL_CODES or ''>", ...}``

The process takes the per-dataset lock file of every source and output it
touches for its whole stage (``hub_sync.stage_lock``, R-19, G-3) and releases
them by exiting. ``data_editor_v3`` imports the engine inside its functions, so
this module stays importable for compileall and the deps-free tests; the legacy
v2.1 ``DataEditor`` is imported lazily, only on the legacy path.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import json
import logging
from pathlib import Path
import sys
from typing import List, Optional

from physical_ai_server.data_processing import data_editor_v3
from physical_ai_server.data_processing import dataset_paths
from physical_ai_server.data_processing.data_editor_v3 import DataEditError
from physical_ai_server.data_processing.dataset_paths import DatasetPathError

# Sentinels of the machine-readable lines the parent parses out of this
# process' stdout. Keep in sync with ``parse_output`` / ``parse_progress``.
RESULT_MARKER = 'EDIT_RESULT::'
PROGRESS_MARKER = 'EDIT_PROGRESS::'

# Mirrors EditDataset.srv mode constants (the old page), as strings so this
# module never imports the ROS interface; ``split`` and ``union`` are the Daten
# service's.
MODE_MERGE = 'merge'
MODE_DELETE = 'delete'
MODE_SPLIT = 'split'
MODE_UNION = 'union'


def _default_logger() -> logging.Logger:
    logger = logging.getLogger('DatasetEditWorker')
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


def _texts():
    return data_editor_v3._load_sibling('daten', 'texts_de')


def _hub_sync():
    return data_editor_v3._load_sibling('data_processing', 'hub_sync')


def _say(line: str) -> None:
    """One protocol line, on a line of its own and in ONE write; a closed pipe
    never kills the transaction (R-19). The parent merges stderr into this
    pipe, so a library's progress bar (``\\r`` + text, no newline while it
    runs) may stand unfinished: the leading newline ends it (the download
    worker's rule, V1-1)."""
    try:
        sys.stdout.write('\n' + line + '\n')
        sys.stdout.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass


def _emit_progress(stage, done, total) -> None:
    _say(PROGRESS_MARKER + json.dumps({'stage': stage, 'done': int(done), 'total': int(total)}))


@contextlib.contextmanager
def _stage_locks(sources, outputs):
    """The lock file of every dataset this stage creates, swaps or reads (R-19).

    A source whose parent folder does not exist has nothing to protect (the
    engine then refuses it in German); an output's parent is created (the
    output lands there). Raises BlockingIOError when another process holds one."""
    hs = _hub_sync()
    srcs = {str(Path(x)) for x in sources}
    outs = {str(Path(x)) for x in outputs}
    fds = []
    try:
        for p in sorted(srcs | outs):
            if p not in outs and not Path(p).parent.is_dir():
                continue
            fds.append(hs.stage_lock(p))
        yield
    finally:
        for fd in fds:
            hs.release_lock(fd)


def run_edit(
    payload: dict,
    logger: Optional[logging.Logger] = None,
    root=None,
    progress=None,
) -> dict:
    """Execute one dataset edit. Returns ``{'success': bool, 'message': str,
    'code': str}`` (+ op-specific numbers). ``message`` is German (Rule §1); the
    technical cause is logged.

    PATH CONFINEMENT (2026-08-06). Every path below arrives from the wire (the
    old page's ``/dataset/edit`` copies them verbatim; the Daten service builds
    them from validated ids), and rosbridge is unauthenticated — so every input
    and every output is confined here, the single choke point shared by the
    subprocess and the in-process rollback.

    ``root`` is a PARAMETER, never read from ``payload``: tests relocate it,
    nothing reachable from the wire may. Production always leaves it None."""
    logger = logger or _default_logger()
    progress = progress or (lambda stage, done, total: None)
    mode = payload.get('mode')
    t = _texts()
    try:
        if mode == MODE_MERGE:
            merge_dataset_list: List[str] = list(payload.get('merge_dataset_list') or [])
            output_path = payload.get('output_path') or ''
            # EVERY input is confined (a later escaping member would leak another
            # tree into the output), the OUTPUT too (it is written to).
            merge_dataset_list = [str(dataset_paths.confine(p, root)) for p in merge_dataset_list]
            output_path = str(dataset_paths.confine(output_path, root))
            # Classify by POSITIVE detection on BOTH sides, so an unreadable /
            # corrupt info.json can never fall through to the legacy merge.
            v3_flags = [data_editor_v3.is_v3_dataset(p) for p in merge_dataset_list]
            v21_flags = [data_editor_v3.is_v21_dataset(p) for p in merge_dataset_list]
            if v3_flags and all(v3_flags):
                with _stage_locks(merge_dataset_list, [output_path]):
                    n = data_editor_v3.merge_datasets_v3(
                        merge_dataset_list, output_path, logger=logger, progress=progress,
                        display_name=payload.get('display_name') or None)
                return {'success': True, 'message': 'Bearbeitung abgeschlossen.', 'code': '',
                        'episodes': n if isinstance(n, int) else None}
            if v21_flags and all(v21_flags):
                from physical_ai_server.data_processing.data_editor import DataEditor
                DataEditor().merge_datasets(merge_dataset_list, output_path)
                return {'success': True, 'message': 'Bearbeitung abgeschlossen.', 'code': ''}
            return {
                'success': False, 'code': 'incompatible',
                'message': ('Die ausgewählten Datensätze konnten nicht zusammengeführt werden — '
                            'sie haben unterschiedliche Formate (v2.1 und v3.0) oder mindestens '
                            'einer ist beschädigt.'),
            }

        if mode == MODE_DELETE:
            delete_dataset_path = payload.get('delete_dataset_path') or ''
            delete_episode_num: List[int] = list(payload.get('delete_episode_num') or [])
            if not delete_episode_num:
                return {'success': False, 'message': 'Keine Episoden zum Löschen ausgewählt.',
                        'code': 'internal'}
            # The destructive path. Confine BEFORE the version probe: an
            # escaping path must never even be stat'd for routing.
            delete_dataset_path = str(dataset_paths.confine(delete_dataset_path, root))
            # The DESTRUCTIVE legacy v2.1 in-place editor ONLY when the dataset
            # POSITIVELY declares a v2.x codebase_version; a v3.0, missing or
            # unreadable one routes to the v3 module (German refusals).
            with _stage_locks([delete_dataset_path], []):
                if data_editor_v3.is_v21_dataset(delete_dataset_path):
                    from physical_ai_server.data_processing.data_editor import DataEditor
                    if len(delete_episode_num) > 1:
                        DataEditor().delete_episodes_batch(delete_dataset_path, delete_episode_num)
                    else:
                        DataEditor().delete_episode(delete_dataset_path, delete_episode_num[0])
                    remaining = None
                else:
                    remaining = data_editor_v3.delete_episodes_v3(
                        delete_dataset_path, delete_episode_num, logger=logger, progress=progress)
            # A delete edits an EXISTING dataset — one that may already live on
            # Hugging Face; edits are local-only, so say so.
            return {
                'success': True, 'code': '',
                'episodes': remaining if isinstance(remaining, int) else None,
                'message': ('Bearbeitung abgeschlossen. Hinweis: Falls dieser Datensatz bereits zu '
                            'Hugging Face hochgeladen wurde, bitte erneut hochladen — das '
                            'Cloud-Training verwendet sonst weiterhin den alten Stand ohne diese '
                            'Änderung.'),
            }

        if mode == MODE_SPLIT:
            path = str(dataset_paths.confine(payload.get('dataset_path') or '', root))
            new_path = str(dataset_paths.confine(payload.get('new_path') or '', root))
            episodes = list(payload.get('episodes') or [])
            with _stage_locks([path], [new_path]):
                kept, moved = data_editor_v3.split_episodes_v3(
                    path, episodes, new_path, logger=logger, progress=progress,
                    display_name=payload.get('display_name') or None)
            return {'success': True, 'message': 'Bearbeitung abgeschlossen.', 'code': '',
                    'kept': kept, 'moved': moved}

        if mode == MODE_UNION:
            path = str(dataset_paths.confine(payload.get('dataset_path') or '', root))
            hub_copy = str(dataset_paths.confine(payload.get('hub_copy_path') or '', root))
            base = payload.get('base_copy_path')
            base = str(dataset_paths.confine(base, root)) if base else None
            with _stage_locks([path], []):
                res = data_editor_v3.union_episodes_v3(
                    path, hub_copy, base, logger=logger, progress=progress,
                    hub_sha=payload.get('hub_sha') or None, hub_trees=payload.get('hub_trees') or None)
            return {'success': True, 'message': 'Bearbeitung abgeschlossen.', 'code': '', **res}

        return {'success': False, 'message': t.UNKNOWN_MODE_DE, 'code': 'internal'}

    except DatasetPathError as e:
        # A path escaping the dataset root: logged as a REFUSAL; the student sees
        # only the German sentence (never the path back).
        logger.error(f'dataset edit REFUSED (path outside dataset root): {e}')
        return {'success': False, 'message': str(e), 'code': 'internal'}

    except BlockingIOError:
        logger.error('dataset edit REFUSED: another process holds the dataset lock')
        return {'success': False, 'message': t.BUSY_EDIT_DE, 'code': 'internal'}

    except DataEditError as e:
        # German message; the technical cause was logged by data_editor_v3.
        logger.error(f'dataset edit rejected: {e.__cause__ or e}')
        return {'success': False, 'message': str(e), 'code': getattr(e, 'code', 'internal') or 'internal'}

    except Exception as e:  # noqa: BLE001 — boundary to the engine / the legacy editor
        logger.error(f'Error in dataset edit: {e!r}')
        return {'success': False, 'message': t.RUN_EDIT_FAILED_DE, 'code': 'internal'}


def build_command(python_exe: str, nice_level: str = '19') -> List[str]:
    """argv prefix to launch this module low-priority; payload goes via stdin."""
    return [
        'nice', '-n', str(nice_level),
        python_exe, '-m', 'physical_ai_server.data_processing.edit_worker',
    ]


def _marked(line: str, marker: str) -> Optional[dict]:
    """The JSON object after the LAST ``marker`` in one line, else None. The
    marker counts wherever it stands (text a progress bar left unfinished may
    precede it) and anything after the object is ignored."""
    i = line.rfind(marker)
    if i < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(line, i + len(marker))
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def parse_output(stdout: str) -> Optional[dict]:
    """The LAST ``RESULT_MARKER`` line of the worker's stdout, decoded; None when
    no valid marker was found (the worker died before emitting one)."""
    result = None
    for line in (stdout or '').splitlines():
        parsed = _marked(line, RESULT_MARKER)
        if parsed is not None:
            result = parsed
    return result


def parse_progress(line: str) -> Optional[dict]:
    """One ``EDIT_PROGRESS::`` line decoded, else None."""
    return _marked(line or '', PROGRESS_MARKER)


def main() -> int:
    logger = _default_logger()
    try:
        payload = json.loads(sys.stdin.read() or '{}')
        if not isinstance(payload, dict):
            raise ValueError('payload is not an object')
    except (ValueError, TypeError) as e:
        logger.error(f'invalid edit payload: {e}')
        _say(RESULT_MARKER + json.dumps(
            {'success': False, 'message': _texts().RUN_EDIT_FAILED_DE, 'code': 'internal'}))
        return 2

    result = run_edit(payload, logger=logger, progress=_emit_progress)
    # The single machine-readable line the parent greps for, flushed so it is
    # the clean final line even after buffered library noise.
    _say(RESULT_MARKER + json.dumps(result))
    return 0 if result.get('success') else 1


if __name__ == '__main__':
    sys.exit(main())

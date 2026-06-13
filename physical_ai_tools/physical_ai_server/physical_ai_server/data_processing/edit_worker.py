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

"""Out-of-process runner for dataset edits (delete / merge).

WHY THIS EXISTS (2026-06-07): a Daten-tab episode delete on a legacy AV1 dataset
runs upstream ``lerobot dataset_tools.delete_episodes``, which rebuilds the whole
dataset and RE-ENCODES every camera video. Software SVT-AV1 on a GPU-less student
PC takes ~12 min per concatenated file and saturates every CPU core. When that
ran synchronously inside the ``/dataset/edit`` ROS service callback it (a) sat on
the node's default MutuallyExclusiveCallbackGroup and serialized out heartbeat /
status / get_robot_types, and (b) CPU-starved every MultiThreadedExecutor thread
— so the entire React dashboard went dead (``/get_robot_types`` timed out) until
the encode finished. See ``docs/plans/2026-06-07-dataset-edit-cpu-isolation.md``.

THE FIX: ``communicator.dataset_edit_callback`` spawns this module as a
``nice -n 19`` subprocess (payload on stdin). The low priority lets the (nice 0)
ROS node preempt the encoder threads, so the executor keeps answering services
while the edit runs. The actual routing (v3-vs-legacy, delete-vs-merge) lives in
``run_edit`` and is shared with the in-process rollback path
(``EDUBOTICS_DATASET_EDIT_SUBPROCESS=0``).

``data_editor_v3`` imports are deps-free at module load (lerobot is function-local
inside it), so this module stays importable for compileall and the deps-free unit
tests. The legacy v2.1 ``DataEditor`` is imported lazily, only on the legacy path.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import List, Optional

from physical_ai_server.data_processing import data_editor_v3
from physical_ai_server.data_processing.data_editor_v3 import DataEditError

# Sentinel prefixing the single machine-readable result line the parent parses
# out of this process' stdout (which is otherwise full of lerobot/ffmpeg/SVT
# progress noise). Keep in sync with ``parse_output`` below.
RESULT_MARKER = 'EDIT_RESULT::'

# Mirrors EditDataset.srv mode constants, but as strings so this module never
# imports the ROS interface (keeps it ROS-free + fast to spawn). The caller
# translates the wire int -> these.
MODE_MERGE = 'merge'
MODE_DELETE = 'delete'


def _default_logger() -> logging.Logger:
    logger = logging.getLogger('DatasetEditWorker')
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


def run_edit(payload: dict, logger: Optional[logging.Logger] = None) -> dict:
    """Execute one dataset edit. Returns ``{'success': bool, 'message': str}``.

    ``message`` is the student-facing GERMAN string (Rule §1) for the common
    paths; the technical cause is logged. This is the verbatim routing that used
    to live inline in ``communicator.dataset_edit_callback`` — moved here so the
    subprocess and the in-process rollback share ONE implementation.
    """
    logger = logger or _default_logger()
    mode = payload.get('mode')

    try:
        if mode == MODE_MERGE:
            merge_dataset_list: List[str] = list(payload.get('merge_dataset_list') or [])
            output_path = payload.get('output_path') or ''
            v3_flags = [data_editor_v3.is_v3_dataset(p) for p in merge_dataset_list]
            if v3_flags and all(v3_flags):
                data_editor_v3.merge_datasets_v3(
                    merge_dataset_list, output_path, logger=logger)
            elif any(v3_flags):
                return {
                    'success': False,
                    'message': (
                        'Die ausgewählten Datensätze haben unterschiedliche '
                        'Formate (v2.1 und v3.0) und können nicht '
                        'zusammengeführt werden.'
                    ),
                }
            else:
                from physical_ai_server.data_processing.data_editor import DataEditor
                DataEditor().merge_datasets(merge_dataset_list, output_path)

        elif mode == MODE_DELETE:
            delete_dataset_path = payload.get('delete_dataset_path') or ''
            delete_episode_num: List[int] = list(payload.get('delete_episode_num') or [])
            if not delete_episode_num:
                return {
                    'success': False,
                    'message': 'Keine Episoden zum Löschen ausgewählt.',
                }

            # Missing paths route through the v3 module too: it raises the
            # German 'nicht gefunden' DataEditError, while the legacy editor
            # would surface an English FileNotFoundError.
            if (data_editor_v3.dataset_dir_missing(delete_dataset_path)
                    or data_editor_v3.is_v3_dataset(delete_dataset_path)):
                data_editor_v3.delete_episodes_v3(
                    delete_dataset_path, delete_episode_num, logger=logger)
            elif len(delete_episode_num) > 1:
                from physical_ai_server.data_processing.data_editor import DataEditor
                DataEditor().delete_episodes_batch(
                    delete_dataset_path, delete_episode_num)
            else:
                from physical_ai_server.data_processing.data_editor import DataEditor
                DataEditor().delete_episode(
                    delete_dataset_path, delete_episode_num[0])

        else:
            return {'success': False, 'message': f'Unknown edit mode: {mode}'}

        return {'success': True, 'message': f'Successfully processed edit mode: {mode}'}

    except DataEditError as e:
        # Student-facing German message; technical cause already logged by
        # data_editor_v3 (and chained on the exception).
        logger.error(f'dataset edit rejected: {e.__cause__ or e}')
        return {'success': False, 'message': str(e)}

    except Exception as e:  # noqa: BLE001 — boundary to upstream / legacy editor
        logger.error(f'Error in dataset edit: {e}')
        return {'success': False, 'message': f'Error: {e}'}


def build_command(python_exe: str, nice_level: str = '19') -> List[str]:
    """argv prefix to launch this module low-priority; payload goes via stdin.

    ``nice -n 19`` (default) so the SVT-AV1 / dav1d worker threads start at low
    priority and the nice-0 ROS node preempts them. The payload is fed on stdin
    (not argv) to dodge ARG_MAX and shell-escaping of the JSON.
    """
    return [
        'nice', '-n', str(nice_level),
        python_exe, '-m', 'physical_ai_server.data_processing.edit_worker',
    ]


def parse_output(stdout: str) -> Optional[dict]:
    """Extract the last ``RESULT_MARKER`` line from the worker's stdout.

    Returns the decoded result dict, or None when no valid marker was found
    (worker died before emitting one — the caller then shows a generic German
    failure). Scans for the LAST marker so a stray earlier print can't win.
    """
    result = None
    for line in (stdout or '').splitlines():
        line = line.strip()
        if line.startswith(RESULT_MARKER):
            try:
                result = json.loads(line[len(RESULT_MARKER):])
            except (ValueError, TypeError):
                continue
    return result


def main() -> int:
    logger = _default_logger()
    try:
        payload = json.loads(sys.stdin.read() or '{}')
    except (ValueError, TypeError) as e:
        print(
            RESULT_MARKER + json.dumps(
                {'success': False, 'message': f'Error: invalid edit payload: {e}'}),
            flush=True,
        )
        return 2

    result = run_edit(payload, logger=logger)
    # The single machine-readable line the parent greps for. flush so it is the
    # clean final line even after lerobot/ffmpeg buffered noise.
    print(RESULT_MARKER + json.dumps(result), flush=True)
    return 0 if result.get('success') else 1


if __name__ == '__main__':
    sys.exit(main())

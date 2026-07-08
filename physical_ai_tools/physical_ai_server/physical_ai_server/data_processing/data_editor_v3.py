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

"""v3.0-layout dataset curation — delegates to upstream lerobot dataset_tools.

The legacy ``DataEditor`` (data_editor.py) performs in-place surgery on the
LeRobot **v2.1** per-episode layout (``data/chunk-000/episode_NNNNNN.parquet``,
per-episode mp4 files, ``meta/episodes.jsonl``). The v2.5.0 recorder writes the
**v3.0** concatenated layout (sharded parquet, ``videos/<key>/chunk-NNN/
file-NNN.mp4``, ``meta/episodes/*.parquet``) — running the v2.1 surgery on a
v3.0 dataset FileNotFoundErrors at best and corrupts at worst. This module is
the v3.0 path: ``communicator.dataset_edit_callback`` routes here when
``meta/info.json::codebase_version`` says v3.0 (see ``is_v3_dataset``).

Design (leLab-comparison PR-1, 2026-06-07):
- DELEGATE, don't reimplement: lerobot==0.5.1 ships v3.0-aware
  ``lerobot.datasets.dataset_tools`` (``delete_episodes`` / ``merge_datasets``)
  that build a NEW dataset tree and never touch the source. We add only
  validation, the atomic swap, and German error mapping.
- NEVER edit in place: delete builds ``<dataset>.tmp_edit``, verifies it,
  renames ``<dataset>`` -> ``<dataset>.bak_edit``, promotes the tmp, re-verifies,
  and only then removes the backup. Any failure restores the original. The
  most data-destructive path in the product never has a half-written state.
- lerobot imports are LAZY (function-local): dataset_tools pulls in torch/
  pandas/pyarrow at module import, which exist only inside the container.
  This module stays importable for compileall and the deps-free unit tests
  (which sys.modules-stub ``lerobot.datasets.dataset_tools``).
- Student-facing failures raise ``DataEditError`` whose ``str()`` is a German
  message (Rule §1); the English technical cause goes to the logger.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
import shutil
from typing import List, Optional

V3_CODEBASE_VERSION = 'v3.0'

# Suffixes for the swap dance. Both live NEXT TO the dataset (same filesystem,
# so Path.rename is an atomic rename(2), never a copy).
_TMP_SUFFIX = '.tmp_edit'
_BAK_SUFFIX = '.bak_edit'


class DataEditError(RuntimeError):
    """Curation failure whose str() is the student-facing German message."""


def _default_logger() -> logging.Logger:
    logger = logging.getLogger('DataEditorV3')
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


def read_dataset_info(dataset_path: Path) -> dict:
    """Parse meta/info.json; {} when missing/unreadable (caller decides)."""
    info_path = Path(dataset_path) / 'meta' / 'info.json'
    try:
        with open(info_path, encoding='utf-8') as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def is_v3_dataset(dataset_path: Path) -> bool:
    """True when meta/info.json declares the v3.0 codebase version."""
    version = read_dataset_info(dataset_path).get('codebase_version')
    return isinstance(version, str) and version.startswith('v3')


def is_v21_dataset(dataset_path) -> bool:
    """True ONLY when meta/info.json POSITIVELY declares a v2.x codebase version.

    The routing to the DESTRUCTIVE legacy in-place editor keys off THIS, not the
    negation of ``is_v3_dataset``. A dataset whose ``meta/info.json`` is missing,
    truncated, or otherwise unreadable (``read_dataset_info`` swallows the parse
    error and returns ``{}``), or whose ``codebase_version`` is absent / not a
    string, is NOT positively v2.1 — so it must NOT receive the legacy v2.1
    surgery. Such a dataset routes to the v3 module instead, which raises a
    German 'nicht gefunden' / 'beschädigt' ``DataEditError`` and never mutates a
    v3.0 tree.

    Why the negation matters: a real v3.0 dataset with a corrupt ``info.json``
    used to fall through to the legacy editor (``is_v3_dataset`` -> False), which
    FileNotFoundErrors in English on the single-episode path and — worse — on the
    multi-episode batch path silently deletes nothing, overwrites ``info.json``
    with ``{}`` and falsely reports success.
    """
    version = read_dataset_info(dataset_path).get('codebase_version')
    return isinstance(version, str) and version.startswith('v2')


def dataset_dir_missing(dataset_path) -> bool:
    """True when the path is not an existing directory.

    The edit callback routes MISSING paths through the v3 module so the
    student gets the German 'nicht gefunden' DataEditError instead of the
    legacy editor's English FileNotFoundError.
    """
    return not Path(dataset_path).is_dir()


def _derive_repo_id(dataset_path: Path) -> str:
    """Best-effort '<user>/<name>' from the on-disk layout.

    The recorder stores datasets as <lerobot home>/<user_id>/<robot>_<task>;
    upstream only uses repo_id as an identifier here (nothing is pushed), so
    a plain directory name is an acceptable fallback.
    """
    dataset_path = Path(dataset_path)
    parent = dataset_path.parent.name
    if parent and not parent.startswith('.'):
        return f'{parent}/{dataset_path.name}'
    return dataset_path.name


def _verify_v3_tree(root: Path, expected_episodes: int) -> None:
    """Belt-and-suspenders structural check of a freshly built v3.0 tree.

    Upstream delete_episodes/merge_datasets already END by constructing a
    LeRobotDataset over the new tree (reader.try_load() proves the parquet is
    readable), so this only re-asserts the shape we are about to promote:
    correct episode count, v3 version, non-empty data/meta shards, and at
    least one concatenated mp4 per video key.
    """
    root = Path(root)
    info = read_dataset_info(root)
    if not info:
        raise DataEditError(
            'Der bearbeitete Datensatz ist unvollständig (meta/info.json fehlt '
            'oder ist unlesbar). Es wurde nichts verändert.'
        )
    version = info.get('codebase_version')
    if not (isinstance(version, str) and version.startswith('v3')):
        raise DataEditError(
            f'Der bearbeitete Datensatz hat eine unerwartete Version '
            f'({version}). Es wurde nichts verändert.'
        )
    total = info.get('total_episodes')
    if total != expected_episodes:
        raise DataEditError(
            f'Episodenzahl nach der Bearbeitung stimmt nicht '
            f'({total} statt {expected_episodes}). Es wurde nichts verändert.'
        )
    if not list((root / 'data').rglob('*.parquet')):
        raise DataEditError(
            'Der bearbeitete Datensatz enthält keine Daten-Dateien. '
            'Es wurde nichts verändert.'
        )
    if not list((root / 'meta' / 'episodes').rglob('*.parquet')):
        raise DataEditError(
            'Der bearbeitete Datensatz enthält keine Episoden-Metadaten. '
            'Es wurde nichts verändert.'
        )
    videos_dir = root / 'videos'
    if videos_dir.is_dir():
        for key_dir in videos_dir.iterdir():
            if key_dir.is_dir() and not list(key_dir.rglob('*.mp4')):
                raise DataEditError(
                    f'Für die Kamera "{key_dir.name}" fehlen die Videodateien '
                    f'im bearbeiteten Datensatz. Es wurde nichts verändert.'
                )


@contextlib.contextmanager
def _force_recorder_vcodec(dataset_tools, logger: logging.Logger):
    """Pin upstream's mixed-file re-encode to the recorder's codec.

    delete_episodes calls the private _copy_and_reindex_videos with its
    default vcodec='libsvtav1': any video file containing both kept and
    deleted episodes is fully decoded and re-encoded as AV1 — a lossy
    generation on a codec the dataset's info.json doesn't declare, and a
    software SVT-AV1 encode that saturates student CPUs (the 2026-06-07
    scar). v0.5.1 exposes no vcodec passthrough on the public functions, so
    for the duration of the edit we default the helper to the same codec
    the recorder writes (EDUBOTICS_VCODEC, h264). merge_datasets never
    re-encodes (it stream-copies via aggregate_datasets /
    concatenate_video_files) — its wrap is defensive only. Version-pinned
    private access, like lerobot_dataset_wrapper; if upstream ever renames
    the helper we fall back to upstream defaults with a logged warning
    instead of failing the edit.
    """
    vcodec = os.environ.get('EDUBOTICS_VCODEC', 'h264')
    original = getattr(dataset_tools, '_copy_and_reindex_videos', None)
    if original is None:
        logger.warning(
            '_copy_and_reindex_videos not found in lerobot dataset_tools — '
            're-encoded video files will use the upstream default codec.'
        )
        yield
        return

    def _with_recorder_codec(*args, **kwargs):
        kwargs.setdefault('vcodec', vcodec)
        return original(*args, **kwargs)

    dataset_tools._copy_and_reindex_videos = _with_recorder_codec
    try:
        yield
    finally:
        dataset_tools._copy_and_reindex_videos = original


def _load_source_dataset(dataset_path: Path, logger: logging.Logger):
    """Construct the source LeRobotDataset (local-only for a complete tree).

    LeRobotDataset.__init__ with an existing root loads from disk
    (reader.try_load()); it falls back to the Hub ONLY when the local tree
    is incomplete — on an offline classroom PC that surfaces as a network
    error, which we map to a German 'beschädigt' message.
    """
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    try:
        return LeRobotDataset(
            repo_id=_derive_repo_id(dataset_path), root=Path(dataset_path)
        )
    except Exception as e:  # noqa: BLE001 — boundary to upstream + network
        logger.error(f'Failed to load source dataset {dataset_path}: {e}')
        raise DataEditError(
            'Der Datensatz konnte nicht geladen werden — er ist unvollständig '
            'oder beschädigt. Bitte den Datensatz neu aufnehmen oder löschen.'
        ) from e


def delete_episodes_v3(
    dataset_path: str,
    episode_indices: List[int],
    logger: Optional[logging.Logger] = None,
) -> int:
    """Delete episodes from a v3.0 dataset via upstream dataset_tools.

    Returns the remaining episode count. Raises DataEditError (German) on any
    failure; the original dataset is untouched unless the swap fully succeeds.
    """
    logger = logger or _default_logger()
    src = Path(dataset_path).resolve()
    if not src.is_dir():
        raise DataEditError(f'Datensatz-Ordner nicht gefunden: {src}')

    info = read_dataset_info(src)
    total = info.get('total_episodes')
    if not isinstance(total, int) or total <= 0:
        raise DataEditError(
            'Der Datensatz enthält keine gültige Episodenzahl '
            '(meta/info.json) — er ist unvollständig oder beschädigt.'
        )

    indices = sorted(set(int(i) for i in episode_indices))
    if not indices:
        raise DataEditError('Keine Episoden zum Löschen ausgewählt.')
    out_of_range = [i for i in indices if i < 0 or i >= total]
    if out_of_range:
        raise DataEditError(
            f'Episoden {out_of_range} gibt es nicht — der Datensatz hat die '
            f'Episoden 0 bis {total - 1}.'
        )
    if len(indices) >= total:
        raise DataEditError(
            'Alle Episoden können nicht gelöscht werden — zum vollständigen '
            'Entfernen bitte den ganzen Datensatz löschen.'
        )

    expected_remaining = total - len(indices)
    tmp = src.parent / (src.name + _TMP_SUFFIX)
    bak = src.parent / (src.name + _BAK_SUFFIX)
    # Stale leftovers from a previous crash never block a new edit.
    for stale in (tmp, bak):
        if stale.exists():
            logger.warning(f'Removing stale edit artifact: {stale}')
            shutil.rmtree(stale, ignore_errors=True)

    source_dataset = _load_source_dataset(src, logger)

    from lerobot.datasets import dataset_tools

    logger.info(
        f'delete_episodes_v3: removing {indices} from {src} '
        f'({total} -> {expected_remaining} episodes)'
    )
    try:
        # Explicit output_dir + repo_id: the upstream defaults would create a
        # '<repo>_modified' SIBLING under the lerobot home instead of our
        # swap-managed tmp tree (dataset_tools.delete_episodes:115-116).
        with _force_recorder_vcodec(dataset_tools, logger):
            dataset_tools.delete_episodes(
                source_dataset,
                indices,
                output_dir=tmp,
                repo_id=_derive_repo_id(src),
            )
    except DataEditError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    except ValueError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        logger.error(f'dataset_tools.delete_episodes rejected the request: {e}')
        # Local pre-validation should have caught these; map defensively.
        raise DataEditError(
            'Die Episoden konnten nicht gelöscht werden: ungültige Auswahl.'
        ) from e
    except Exception as e:  # noqa: BLE001 — boundary to upstream
        shutil.rmtree(tmp, ignore_errors=True)
        logger.error(f'dataset_tools.delete_episodes failed: {e}')
        raise DataEditError(
            'Beim Löschen der Episoden ist ein Fehler aufgetreten. '
            'Der Datensatz wurde NICHT verändert.'
        ) from e

    # Build fully -> verify -> swap -> re-verify -> drop backup. Restore on
    # any failure past the first rename.
    try:
        _verify_v3_tree(tmp, expected_remaining)
    except DataEditError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    swapped = False
    try:
        src.rename(bak)
        tmp.rename(src)
        swapped = True
        _verify_v3_tree(src, expected_remaining)
    except Exception as e:
        # Roll back: put the original tree back exactly where it was.
        logger.error(f'Swap failed, restoring original dataset: {e}')
        if swapped and bak.exists():
            shutil.rmtree(src, ignore_errors=True)
            bak.rename(src)
        elif not swapped and bak.exists() and not src.exists():
            bak.rename(src)
        shutil.rmtree(tmp, ignore_errors=True)
        if isinstance(e, DataEditError):
            raise
        raise DataEditError(
            'Beim Ersetzen des Datensatzes ist ein Fehler aufgetreten. '
            'Der ursprüngliche Datensatz wurde wiederhergestellt.'
        ) from e

    shutil.rmtree(bak, ignore_errors=True)
    logger.info(
        f'delete_episodes_v3: success, {expected_remaining} episodes remain'
    )
    return expected_remaining


def merge_datasets_v3(
    dataset_paths: List[str],
    output_path: str,
    logger: Optional[logging.Logger] = None,
) -> None:
    """Merge v3.0 datasets into a NEW dataset at output_path (no swap needed)."""
    logger = logger or _default_logger()
    if not dataset_paths or len(dataset_paths) < 2:
        raise DataEditError(
            'Zum Zusammenführen müssen mindestens zwei Datensätze '
            'ausgewählt sein.'
        )
    sources = [Path(p).resolve() for p in dataset_paths]
    for p in sources:
        if not p.is_dir():
            raise DataEditError(f'Datensatz-Ordner nicht gefunden: {p}')

    out = Path(output_path).resolve()
    if out.exists() and any(out.iterdir()):
        raise DataEditError(
            f'Der Ziel-Ordner existiert bereits und ist nicht leer: {out}. '
            f'Bitte einen neuen Ordnernamen wählen.'
        )
    if any(out == p or p in out.parents for p in sources):
        raise DataEditError(
            'Der Ziel-Ordner darf keiner der Quell-Datensätze sein.'
        )

    expected_total = 0
    for p in sources:
        total = read_dataset_info(p).get('total_episodes')
        if not isinstance(total, int) or total <= 0:
            raise DataEditError(
                f'Der Datensatz "{p.name}" ist unvollständig oder beschädigt '
                f'(keine gültige Episodenzahl) und kann nicht zusammengeführt '
                f'werden.'
            )
        expected_total += total

    datasets = [_load_source_dataset(p, logger) for p in sources]

    from lerobot.datasets import dataset_tools

    logger.info(
        f'merge_datasets_v3: merging {len(sources)} datasets '
        f'({expected_total} episodes) into {out}'
    )
    try:
        with _force_recorder_vcodec(dataset_tools, logger):
            dataset_tools.merge_datasets(
                datasets, output_repo_id=_derive_repo_id(out), output_dir=out
            )
    except DataEditError:
        raise
    except Exception as e:  # noqa: BLE001 — boundary to upstream
        shutil.rmtree(out, ignore_errors=True)
        logger.error(f'dataset_tools.merge_datasets failed: {e}')
        raise DataEditError(
            'Beim Zusammenführen ist ein Fehler aufgetreten. Die '
            'Quell-Datensätze wurden nicht verändert.'
        ) from e

    try:
        _verify_v3_tree(out, expected_total)
    except DataEditError:
        shutil.rmtree(out, ignore_errors=True)
        raise
    logger.info(f'merge_datasets_v3: success ({expected_total} episodes)')

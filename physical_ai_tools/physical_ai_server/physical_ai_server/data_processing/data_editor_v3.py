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

"""v3.0-layout dataset curation on the Daten 2.0 engine (spec §D1, §D2).

The legacy ``DataEditor`` (data_editor.py) performs in-place surgery on the
LeRobot **v2.1** per-episode layout; running it on the recorder's **v3.0**
concatenated layout corrupts. This module is the v3.0 path —
``edit_worker.run_edit`` routes here unless ``meta/info.json`` POSITIVELY says
v2.x (``is_v21_dataset``).

Every operation is ONE engine primitive, ``v3_surgery.assemble``, which copies
data rows and video packets losslessly by STREAM COPY at episode boundaries
(no re-encode, no private LeRobot name — the old ``dataset_tools`` delegation
and its ``_copy_and_reindex_videos`` monkeypatch are gone):

* delete  = the complement of the chosen episodes, built into
  ``<dataset>.tmp_edit``, verified, swapped in (``.bak_edit``), re-checked,
  the bak dropped; restored on any failure;
* split   = the chosen episodes into a NEW dataset and the rest back into the
  original — two builds, both verified, promoted under a JOURNAL
  (``<ns>/.<name>.journal.json``) that makes the two promotions all or nothing
  even across a crash (``recover_split``);
* merge   = every episode of every source, in the given order, into a NEW
  dataset;
* union   = „Beide behalten": the three-way plan of the local and the hub copy
  against the last synced base (``dataset_sync.plan_keep_both`` over
  ``v3_surgery.episode_identity``), swapped in like a delete; the record of the
  hub copy is written so the result reads „changed" against that head.

Every promoted output's record gets ``files`` (the sha256 of every file under
``data/``, ``meta/episodes/``, ``videos/`` — what the next upload checks the
local copy against). A swap and its record are one transaction
(``hub_sync.swap_in``, H-8).

``v3_surgery`` (pyarrow/PyAV, LeRobot inside its functions) is imported INSIDE
the functions that use it, never at module level (A18: deps-free loaders load
this file by path). Student-facing failures raise ``DataEditError`` whose
``str()`` is German (Rule §1) and whose ``code`` is the engine's
(``contract.JOB_FAIL_CODES``); the English cause goes to the logger.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
from pathlib import Path
import shutil
import sys
from typing import Callable, List, Optional

V3_CODEBASE_VERSION = 'v3.0'

# Suffixes of the swap: both live NEXT TO the dataset (same filesystem, so
# Path.rename is an atomic rename(2), never a copy).
_TMP_SUFFIX = '.tmp_edit'
_BAK_SUFFIX = '.bak_edit'

ProgressFn = Optional[Callable[[str, int, int], None]]


class DataEditError(RuntimeError):
    """Curation failure whose str() is the student-facing German message and
    whose ``code`` is the machine code (``contract.JOB_FAIL_CODES``)."""

    def __init__(self, message, code='internal'):
        super().__init__(message)
        self.code = code


def _load_sibling(package, name):
    """A module of this package: by package name in the image, by file path when
    a deps-free loader gave the package no ``__path__`` (cached by key)."""
    try:
        return importlib.import_module(f'physical_ai_server.{package}.{name}')
    except ImportError:
        key = f'_edubotics_{package}_{name}'
        module = sys.modules.get(key)
        if module is None:
            path = Path(__file__).resolve().parent.parent / package / f'{name}.py'
            spec = importlib.util.spec_from_file_location(key, str(path))
            module = importlib.util.module_from_spec(spec)
            sys.modules[key] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(key, None)
                raise
        return module


def _texts():
    return _load_sibling('daten', 'texts_de')


def _default_logger() -> logging.Logger:
    logger = logging.getLogger('DataEditorV3')
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


def _progress(progress: ProgressFn, stage: str, done: int, total: int) -> None:
    if progress is not None:
        try:
            progress(stage, done, total)
        except Exception:  # noqa: BLE001 — a progress line must never break an edit
            pass


def read_dataset_info(dataset_path: Path) -> dict:
    """Parse meta/info.json; {} when missing/unreadable (caller decides)."""
    info_path = Path(dataset_path) / 'meta' / 'info.json'
    try:
        with open(info_path, encoding='utf-8') as f:
            info = json.load(f)
    except (OSError, ValueError):
        return {}
    return info if isinstance(info, dict) else {}


def is_v3_dataset(dataset_path: Path) -> bool:
    """True when meta/info.json declares the v3.0 codebase version."""
    version = read_dataset_info(dataset_path).get('codebase_version')
    return isinstance(version, str) and version.startswith('v3')


def is_v21_dataset(dataset_path) -> bool:
    """True ONLY when meta/info.json POSITIVELY declares a v2.x codebase version.

    The routing to the DESTRUCTIVE legacy in-place editor keys off THIS, never
    the negation of ``is_v3_dataset``: a v3.0 dataset with a missing or corrupt
    ``info.json`` (``read_dataset_info`` answers ``{}``) is NOT positively v2.1,
    so it routes here and fails in German instead of receiving v2.1 surgery
    (which once clobbered ``info.json`` to ``{}`` and reported success)."""
    version = read_dataset_info(dataset_path).get('codebase_version')
    return isinstance(version, str) and version.startswith('v2')


def dataset_dir_missing(dataset_path) -> bool:
    """True when the path is not an existing directory."""
    return not Path(dataset_path).is_dir()


def _derive_repo_id(dataset_path: Path) -> str:
    """``<ns>/<name>`` from the on-disk layout (``<root>/<ns>/<name>``)."""
    dataset_path = Path(dataset_path)
    parent = dataset_path.parent.name
    if parent and not parent.startswith('.'):
        return f'{parent}/{dataset_path.name}'
    return dataset_path.name


def surgery_message_de(error) -> str:
    """The German sentence of an engine refusal (§D1, §J.6)."""
    t = _texts()
    code = getattr(error, 'code', '')
    if code == 'unsupported':
        return t.UNSUPPORTED_DE
    if code == 'layout':
        return t.LAYOUT_DE
    if code == 'unaligned':
        return t.UNALIGNED_DE
    if code == 'incompatible':
        return t.incompatible_de([n for n in str(getattr(error, 'detail', '')).split(',') if n])
    if code == 'exists':
        return t.EXISTS_DE
    if code == 'verify_failed':
        return t.VERIFY_FAILED_DE
    return t.RUN_EDIT_FAILED_DE


def _as_edit_error(error, logger) -> DataEditError:
    logger.error(f'dataset edit refused by the engine: {error}')
    return DataEditError(surgery_message_de(error), code=getattr(error, 'code', 'internal'))


def _require_dataset(path: Path) -> int:
    """German pre-validation of an existing v3 dataset: returns total_episodes."""
    if not path.is_dir():
        raise DataEditError(f'Datensatz-Ordner nicht gefunden: {path.name}', code='not_found')
    total = read_dataset_info(path).get('total_episodes')
    if not isinstance(total, int) or isinstance(total, bool) or total <= 0:
        raise DataEditError(
            'Der Datensatz enthält keine gültige Episodenzahl '
            '(meta/info.json) — er ist unvollständig oder beschädigt.', code='layout')
    return total


def _validate_indices(indices, total, *, verb='gelöscht') -> list:
    chosen = sorted({int(i) for i in indices})
    if not chosen:
        raise DataEditError('Keine Episoden zum Löschen ausgewählt.' if verb == 'gelöscht'
                            else 'Keine Episoden ausgewählt.')
    out_of_range = [i for i in chosen if i < 0 or i >= total]
    if out_of_range:
        raise DataEditError(
            f'Episoden {out_of_range} gibt es nicht — der Datensatz hat die '
            f'Episoden 0 bis {total - 1}.')
    if len(chosen) >= total:
        raise DataEditError(
            'Alle Episoden können nicht gelöscht werden — zum vollständigen '
            'Entfernen bitte den ganzen Datensatz löschen.' if verb == 'gelöscht' else
            'Es müssen Episoden im ursprünglichen Datensatz bleiben — wähle nicht alle aus.')
    return chosen


def _clear_stale(*paths: Path, logger) -> None:
    for stale in paths:
        if stale.exists():
            logger.warning(f'Removing stale edit artifact: {stale}')
            shutil.rmtree(stale, ignore_errors=True)


def _build(out: Path, parts, repo_id, progress: ProgressFn, logger):
    """assemble + verify into ``out``; any failure removes ``out`` and raises a
    German DataEditError."""
    V = _load_sibling('data_processing', 'v3_surgery')
    try:
        V.assemble(out, parts, repo_id,
                   progress=lambda stage, done, total: _progress(progress, 'copy', done, total))
        _progress(progress, 'verify', 0, 1)
        V.verify_or_raise(out, parts)
        _progress(progress, 'verify', 1, 1)
    except V.SurgeryError as e:
        shutil.rmtree(out, ignore_errors=True)
        raise _as_edit_error(e, logger) from e
    except Exception as e:  # noqa: BLE001 — the boundary to LeRobot/PyAV
        shutil.rmtree(out, ignore_errors=True)
        logger.error(f'dataset edit build failed: {e!r}')
        raise DataEditError(_texts().RUN_EDIT_FAILED_DE, code='internal') from e


def _open_source(path: Path, logger):
    V = _load_sibling('data_processing', 'v3_surgery')
    try:
        return V.Source(path)
    except V.SurgeryError as e:
        raise _as_edit_error(e, logger) from e


def _rename_check(digest_before):
    """The swap's re-verify: the promoted tree is the verified one (cheap: the
    meta digest the verified tmp had)."""
    S = _load_sibling('data_processing', 'dataset_sync')

    def check(target):
        if S.meta_digest(target) != digest_before:
            raise DataEditError(_texts().VERIFY_FAILED_DE, code='verify_failed')
    return check


def _record_with_files(root, base_record, files, **extra) -> dict:
    """``base_record`` (or a fresh one for the folder's own id) with ``files``
    and ``extra`` set; ``None`` values remove a key."""
    S = _load_sibling('data_processing', 'dataset_sync')
    repo_id = S.folder_id(root)
    rec = dict(base_record) if base_record and base_record.get('repo_id') == repo_id else {
        'v': S.RECORD_VERSION, 'repo_id': repo_id}
    rec['files'] = files
    for k, v in extra.items():
        if v is None:
            rec.pop(k, None)
        else:
            rec[k] = v
    return rec


def _swap(tmp: Path, target: Path, rec, logger, *, progress: ProgressFn = None):
    """tmp → target through hub_sync.swap_in (H-8), with the cheap re-check; a
    failure restores the original and raises German."""
    H = _load_sibling('data_processing', 'hub_sync')
    S = _load_sibling('data_processing', 'dataset_sync')
    digest = S.meta_digest(tmp)
    _progress(progress, 'swap', 0, 1)
    try:
        H.swap_in(tmp, target, rec, _TMP_SUFFIX, _BAK_SUFFIX, check=_rename_check(digest))
    except DataEditError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f'Swap failed, the original dataset was restored: {e!r}')
        raise DataEditError(
            'Beim Ersetzen des Datensatzes ist ein Fehler aufgetreten. '
            'Der ursprüngliche Datensatz wurde wiederhergestellt.', code='internal') from e
    _progress(progress, 'swap', 1, 1)


def delete_episodes_v3(
    dataset_path: str,
    episode_indices: List[int],
    logger: Optional[logging.Logger] = None,
    progress: ProgressFn = None,
) -> int:
    """Delete episodes from a v3.0 dataset; returns the remaining episode count.

    tmp → verify → ``.bak_edit`` swap → re-check → drop bak; the original is
    untouched unless the swap fully succeeds. Raises DataEditError (German)."""
    logger = logger or _default_logger()
    src_path = Path(dataset_path).resolve()
    _progress(progress, 'prepare', 0, 1)
    total = _require_dataset(src_path)
    chosen = _validate_indices(episode_indices, total)
    tmp = src_path.parent / (src_path.name + _TMP_SUFFIX)
    _clear_stale(tmp, src_path.parent / (src_path.name + _BAK_SUFFIX), logger=logger)
    src = _open_source(src_path, logger)
    if len(src.episodes) != total:
        raise DataEditError(_texts().LAYOUT_DE, code='layout')
    keep = [e for e in range(total) if e not in set(chosen)]
    _progress(progress, 'prepare', 1, 1)
    logger.info(f'delete_episodes_v3: removing {chosen} from {src_path} ({total} -> {len(keep)} episodes)')
    _build(tmp, [(src, e) for e in keep], _derive_repo_id(src_path), progress, logger)
    S = _load_sibling('data_processing', 'dataset_sync')
    rec = _record_with_files(src_path, S.own_record(src_path, S.folder_id(src_path)),
                             S.files_manifest(tmp))
    _swap(tmp, src_path, rec, logger, progress=progress)
    logger.info(f'delete_episodes_v3: success, {len(keep)} episodes remain')
    return len(keep)


def split_episodes_v3(
    dataset_path: str,
    episode_indices: List[int],
    new_path: str,
    logger: Optional[logging.Logger] = None,
    progress: ProgressFn = None,
    display_name: Optional[str] = None,
) -> tuple:
    """Move the chosen episodes into a NEW dataset at ``new_path``; the rest stays
    under the original name (D11). Both outputs are built and verified first;
    then a JOURNAL makes the two promotions one transaction (R-19). Returns
    ``(kept, moved)``."""
    logger = logger or _default_logger()
    S = _load_sibling('data_processing', 'dataset_sync')
    src_path = Path(dataset_path).resolve()
    new_path = Path(new_path).resolve()
    _progress(progress, 'prepare', 0, 1)
    if new_path.exists():
        raise DataEditError(_texts().EXISTS_DE, code='exists')
    total = _require_dataset(src_path)
    chosen = _validate_indices(episode_indices, total, verb='verschoben')
    src = _open_source(src_path, logger)
    if len(src.episodes) != total:
        raise DataEditError(_texts().LAYOUT_DE, code='layout')
    rest = [e for e in range(total) if e not in set(chosen)]
    tmp_rest = src_path.parent / (src_path.name + _TMP_SUFFIX)
    tmp_new = new_path.parent / (new_path.name + _TMP_SUFFIX)
    bak = src_path.parent / (src_path.name + _BAK_SUFFIX)
    _clear_stale(tmp_rest, tmp_new, bak, logger=logger)
    _progress(progress, 'prepare', 1, 1)
    new_path.parent.mkdir(parents=True, exist_ok=True)
    _build(tmp_new, [(src, e) for e in chosen], _derive_repo_id(new_path), progress, logger)
    try:
        _build(tmp_rest, [(src, e) for e in rest], _derive_repo_id(src_path), progress, logger)
    except DataEditError:
        shutil.rmtree(tmp_new, ignore_errors=True)
        raise
    old_rec = S.own_record(src_path, S.folder_id(src_path))
    rec_rest = _record_with_files(src_path, old_rec, S.files_manifest(tmp_rest))
    rec_new = _record_with_files(new_path, None, S.files_manifest(tmp_new),
                                 display_name=display_name or None,
                                 private=old_rec.get('private') if old_rec else None)
    _progress(progress, 'swap', 0, 1)
    journal = S.journal_path(src_path)
    S.write_json_atomic(journal, {'op': 'split', 'path': str(src_path), 'new': str(new_path),
                                  'records': {'path': rec_rest, 'new': rec_new}})
    try:
        src_path.rename(bak)
        tmp_rest.rename(src_path)
        tmp_new.rename(new_path)
    except Exception as e:  # noqa: BLE001 — the journal rolls it back right now
        logger.error(f'split promotion failed, rolling back: {e!r}')
        recover_split(src_path)
        raise DataEditError(
            'Beim Ersetzen des Datensatzes ist ein Fehler aufgetreten. '
            'Der ursprüngliche Datensatz wurde wiederhergestellt.', code='internal') from e
    recover_split(src_path)          # the roll forward: records promoted, bak and journal removed
    _progress(progress, 'swap', 1, 1)
    logger.info(f'split_episodes_v3: {len(chosen)} episodes -> {new_path.name}, {len(rest)} stay')
    return len(rest), len(chosen)


def recover_split(dataset_path) -> Optional[str]:
    """Finish or undo a split from its journal (R-19, §D3): while the new
    output's ``.tmp_edit`` still exists the split is rolled BACK (the original
    restored from ``.bak_edit``, both tmps removed, the prepared records
    dropped); once it is promoted the split is rolled FORWARD (both records
    promoted, the bak removed). The journal is removed either way. Returns
    ``'back'``, ``'forward'`` or None (no journal). Caller holds the lock."""
    S = _load_sibling('data_processing', 'dataset_sync')
    src_path = Path(dataset_path)
    journal = S.journal_path(src_path)
    try:
        j = json.loads(journal.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        if journal.exists():
            journal.unlink(missing_ok=True)
        return None
    new_path = Path(j.get('new') or '')
    bak = src_path.parent / (src_path.name + _BAK_SUFFIX)
    tmp_rest = src_path.parent / (src_path.name + _TMP_SUFFIX)
    tmp_new = new_path.parent / (new_path.name + _TMP_SUFFIX)
    if j.get('new') and tmp_new.exists():                     # roll back
        if bak.exists():
            if src_path.exists():
                shutil.rmtree(src_path, ignore_errors=True)
            bak.rename(src_path)
        shutil.rmtree(tmp_rest, ignore_errors=True)
        shutil.rmtree(tmp_new, ignore_errors=True)
        outcome = 'back'
    else:                                                     # roll forward
        records = j.get('records') or {}
        if records.get('path') and src_path.exists():
            S.write_record(src_path, records['path'])
        if records.get('new') and j.get('new') and new_path.exists():
            S.write_record(new_path, records['new'])
        shutil.rmtree(bak, ignore_errors=True)
        outcome = 'forward'
    journal.unlink(missing_ok=True)
    return outcome


def merge_datasets_v3(
    dataset_paths: List[str],
    output_path: str,
    logger: Optional[logging.Logger] = None,
    progress: ProgressFn = None,
    display_name: Optional[str] = None,
) -> int:
    """Merge v3.0 datasets, every episode in the given order, into a NEW dataset
    at ``output_path`` (the target must not exist). Returns the episode count."""
    logger = logger or _default_logger()
    S = _load_sibling('data_processing', 'dataset_sync')
    if not dataset_paths or len(dataset_paths) < 2:
        raise DataEditError('Zum Zusammenführen müssen mindestens zwei Datensätze ausgewählt sein.')
    sources = [Path(p).resolve() for p in dataset_paths]
    out = Path(output_path).resolve()
    _progress(progress, 'prepare', 0, 1)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise DataEditError(_texts().EXISTS_DE, code='exists')
    if any(out == p or p in out.parents for p in sources):
        raise DataEditError('Der Ziel-Ordner darf keiner der Quell-Datensätze sein.')
    for p in sources:
        if not p.is_dir():
            raise DataEditError(f'Datensatz-Ordner nicht gefunden: {p.name}', code='not_found')
        _require_dataset(p)
    srcs = [_open_source(p, logger) for p in sources]
    parts = [(s, e) for s in srcs for e in range(len(s.episodes))]
    if out.exists():
        out.rmdir()                                            # an empty leftover directory
    tmp = out.parent / (out.name + _TMP_SUFFIX)
    _clear_stale(tmp, logger=logger)
    out.parent.mkdir(parents=True, exist_ok=True)
    _progress(progress, 'prepare', 1, 1)
    logger.info(f'merge_datasets_v3: merging {len(sources)} datasets ({len(parts)} episodes) into {out}')
    _build(tmp, parts, _derive_repo_id(out), progress, logger)
    first = S.own_record(sources[0], S.folder_id(sources[0]))
    rec = _record_with_files(out, None, S.files_manifest(tmp), display_name=display_name or None,
                             private=first.get('private') if first else None)
    _swap(tmp, out, rec, logger, progress=progress)
    logger.info(f'merge_datasets_v3: success ({len(parts)} episodes)')
    return len(parts)


def union_episodes_v3(
    dataset_path: str,
    hub_copy_path: str,
    base_copy_path: Optional[str],
    logger: Optional[logging.Logger] = None,
    progress: ProgressFn = None,
    hub_sha: Optional[str] = None,
    hub_trees: Optional[dict] = None,
) -> dict:
    """„Beide behalten" (§E10): the THREE-WAY merge of the local copy and the hub
    copy against the base (the record's ``hub_sha``; ``base_copy_path`` None =
    no base → the union by multiplicity), by episode identity (data rows AND
    video packets). Built into ``<dataset>.tmp_edit``, verified, swapped in like
    a delete; the record of the HUB copy is written (``hub_sha``/``hub_trees``
    of that head, its meta digest, ``files`` of the result) so the result reads
    „changed" until its upload. The hub and base copies are removed."""
    logger = logger or _default_logger()
    S = _load_sibling('data_processing', 'dataset_sync')
    V = _load_sibling('data_processing', 'v3_surgery')
    src_path = Path(dataset_path).resolve()
    hub_path = Path(hub_copy_path).resolve()
    base_path = Path(base_copy_path).resolve() if base_copy_path else None
    _progress(progress, 'prepare', 0, 1)
    try:
        loc = _open_source(src_path, logger)
        hub = _open_source(hub_path, logger)
        base = _open_source(base_path, logger) if base_path else None
        bad = [c for ok, c in V.check_compatible([loc, hub]) if not ok]
        if bad:
            raise DataEditError(_texts().incompatible_de(bad), code='incompatible')
        try:
            lid = [V.episode_identity(loc, e) for e in range(len(loc.episodes))]
            hid = [V.episode_identity(hub, e) for e in range(len(hub.episodes))]
            bid = [V.episode_identity(base, e) for e in range(len(base.episodes))] if base else None
        except V.SurgeryError as e:
            raise _as_edit_error(e, logger) from e
        plan = S.plan_keep_both(lid, hid, bid)
        parts = [(loc if side == 'L' else hub, i) for side, i in plan]
        tmp = src_path.parent / (src_path.name + _TMP_SUFFIX)
        _clear_stale(tmp, src_path.parent / (src_path.name + _BAK_SUFFIX), logger=logger)
        _progress(progress, 'prepare', 1, 1)
        _build(tmp, parts, _derive_repo_id(src_path), progress, logger)
        old = S.own_record(src_path, S.folder_id(src_path))
        rec = _record_with_files(
            src_path, None, S.files_manifest(tmp), hub_sha=hub_sha or None, hub_trees=hub_trees or None,
            local_digest=S.meta_digest(hub_path), synced_at=S.now_iso(),
            display_name=old.get('display_name') if old else None,
            private=old.get('private') if old else None)
        _swap(tmp, src_path, rec, logger, progress=progress)
    finally:
        shutil.rmtree(hub_path, ignore_errors=True)
        if base_path is not None:
            shutil.rmtree(base_path, ignore_errors=True)
    n_local = sum(1 for side, _ in plan if side == 'L')
    logger.info(f'union_episodes_v3: {len(plan)} episodes ({n_local} here, {len(plan) - n_local} from the hub)')
    return {'episodes': len(plan), 'local': n_local, 'from_hub': len(plan) - n_local,
            'three_way': base is not None}

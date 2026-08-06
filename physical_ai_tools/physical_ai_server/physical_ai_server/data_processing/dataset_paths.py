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

"""Root confinement for every client-supplied dataset path (2026-08-06).

WHY THIS EXISTS. The Daten tab has no auth gate (only Training and Inferenz
do), and it reaches the robot over an unauthenticated rosbridge. Three
surfaces took a path straight off the wire and used it verbatim:

  * ``edit_worker.run_edit`` — ``delete_dataset_path`` fed the DESTRUCTIVE
    episode delete, so any student could destroy any other's episodes (and,
    with an absolute path, anything else the container could write);
    ``merge_dataset_list`` and ``output_path` were equally unchecked, and the
    output is WRITTEN to.
  * ``PhysicalAIServer.get_dataset_list_callback`` — joined a client-supplied
    ``user_id`` onto the save root, a directory-listing traversal.
  * ``FileBrowseUtils`` — no confinement at all; it walks to ``/``.

So this module is the ONE place that decides "is this path inside the dataset
area", and it is deliberately paranoid:

  * ``os.path.join``/``pathlib`` DISCARD the root when a later segment is
    ABSOLUTE (``Path('/a') / '/etc'`` is ``/etc``), so an absolute segment is
    the easiest bypass and is handled by resolving and re-testing rather than
    by string surgery.
  * ``..`` is handled by ``resolve()``, not by scanning for the literal — a
    scan misses ``a/../../..`` spellings and symlink indirection.
  * Symlinks are FOLLOWED on both sides (``resolve()``), so a symlink planted
    inside the root that points outside it is refused. That is the safe
    direction: we judge where the path really lands, not what it is spelled.
  * BOTH sides are resolved, which also makes the check correct where the root
    itself sits behind a symlink (macOS ``/tmp`` -> ``/private/tmp``).
  * The root itself is refused by default: a dataset is ``<root>/<user>/<name>``
    and an edit targeting the root would take every student's work with it.

Stdlib-only and ROS-free on purpose: ``edit_worker`` runs as a bare subprocess
and must stay importable for compileall and the deps-free unit tests.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Union

# Where recorded datasets live: <root>/<hf-user>/<dataset-name>.
# Single source of truth — PhysicalAIServer.DEFAULT_SAVE_ROOT_PATH is derived
# from this so the node and the edit subprocess can never disagree about what
# "inside" means. (Recorded datasets live inside the huggingface_cache volume;
# see docs/KNOWN-ISSUES.md — that is why confinement has to be per-PATH and
# cannot be done at the volume level.)
DATASET_ROOT_RELATIVE = '.cache/huggingface/lerobot'

# Student-facing German (Rule §1). Deliberately does NOT echo the offending
# path back: the message is surfaced in the browser, and reflecting an
# arbitrary caller-supplied string into the UI is its own small problem.
OUTSIDE_ROOT_DE = (
    'Der Pfad liegt außerhalb des Datensatz-Ordners und wurde aus '
    'Sicherheitsgründen abgelehnt.'
)
EMPTY_PATH_DE = 'Es wurde kein Pfad angegeben.'


class DatasetPathError(Exception):
    """Raised when a client-supplied path escapes the dataset root.

    Carries the GERMAN student-facing text as ``str(exc)`` so callers can
    surface it directly, matching ``DataEditError``'s contract.
    """


def dataset_root() -> Path:
    """The one directory client-supplied dataset paths may live under."""
    return Path.home() / DATASET_ROOT_RELATIVE


def model_root() -> Optional[Path]:
    """Downloaded-checkpoint root, or None when it cannot be resolved.

    ``TrainingManager.get_weight_save_root_path()`` derives it from lerobot's
    install location, so the import is FUNCTION-LOCAL: this module must stay
    stdlib-only at import time (``edit_worker`` runs as a bare subprocess and
    the deps-free unit tests import it without lerobot).

    None simply drops it from the browsable set — never widens it.
    """
    try:
        from physical_ai_server.training.training_manager import TrainingManager
        path = TrainingManager.get_weight_save_root_path()
    except Exception:  # noqa: BLE001 — lerobot absent (tests) / unresolvable
        return None
    return Path(path) if path else None


def browsable_roots():
    """Roots the FILE BROWSER may show: datasets, plus model checkpoints.

    Deliberately WIDER than the edit/delete confinement, which stays
    dataset-only: browsing is read-only, and React opens this browser with
    BOTH ``DEFAULT_PATHS.DATASET_PATH`` and ``DEFAULT_PATHS.POLICY_MODEL_PATH``
    („Modellpfad auswählen"). Confining it to the dataset root alone refused
    every path that second entry point was ever opened with.
    """
    roots = [dataset_root()]
    checkpoints = model_root()
    if checkpoints:
        roots.append(checkpoints)
    return roots


def confine(
    candidate: Optional[Union[str, Path]],
    root: Optional[Union[str, Path]] = None,
    *,
    allow_root: bool = False,
) -> Path:
    """Return ``candidate`` resolved, or raise if it escapes ``root``.

    ``root`` defaults to :func:`dataset_root`. It is a PARAMETER rather than an
    environment variable on purpose: tests need to relocate it, but nothing
    reachable from the wire may. In particular the edit payload — which is
    built from the ROS request — must never carry it.

    Raises:
        DatasetPathError: with a German, student-facing message.
    """
    if candidate is None:
        raise DatasetPathError(EMPTY_PATH_DE)
    # A non-str/Path (a list, an int off a malformed payload) must be refused
    # rather than coerced — str(['/etc']) is a path-shaped string.
    if not isinstance(candidate, (str, Path)):
        raise DatasetPathError(EMPTY_PATH_DE)
    text = str(candidate).strip()
    if not text:
        raise DatasetPathError(EMPTY_PATH_DE)
    # A NUL byte makes the OS-level check disagree with the Python-level one.
    if '\x00' in text:
        raise DatasetPathError(OUTSIDE_ROOT_DE)

    root_path = Path(root) if root is not None else dataset_root()
    try:
        # strict=False: a merge OUTPUT path legitimately does not exist yet,
        # and the root itself may not exist on a fresh container.
        resolved_root = root_path.resolve()
        resolved = Path(text).resolve()
    except (OSError, ValueError, RuntimeError) as exc:
        # RuntimeError: symlink loop on some platforms.
        raise DatasetPathError(OUTSIDE_ROOT_DE) from exc

    if resolved == resolved_root:
        if allow_root:
            return resolved
        raise DatasetPathError(OUTSIDE_ROOT_DE)

    # `in resolved.parents` is an exact ancestor test over path COMPONENTS, so
    # it cannot be fooled by a sibling with a shared prefix the way a
    # `startswith` on the string form can (`/a/lerobot-evil` vs `/a/lerobot`).
    if resolved_root not in resolved.parents:
        raise DatasetPathError(OUTSIDE_ROOT_DE)
    return resolved


def confine_any(
    candidate: Optional[Union[str, Path]],
    roots,
    *,
    allow_root: bool = False,
) -> Path:
    """Like :func:`confine`, but accept the path if it is under ANY root.

    The file BROWSER legitimately serves two areas — recorded datasets under
    ``~/.cache/huggingface/lerobot`` and downloaded model checkpoints under
    lerobot's ``outputs/train`` (React seeds it with both:
    ``DEFAULT_PATHS.DATASET_PATH`` and ``DEFAULT_PATHS.POLICY_MODEL_PATH``).
    Confining it to the dataset root alone made „Modellpfad auswählen" refuse
    every path it was ever opened with.

    Refuses when ``roots`` is empty rather than accepting everything — an empty
    allowlist must never mean "allow all".
    """
    roots = [r for r in (roots or []) if r]
    if not roots:
        raise DatasetPathError(OUTSIDE_ROOT_DE)
    last = None
    for root in roots:
        try:
            return confine(candidate, root, allow_root=allow_root)
        except DatasetPathError as exc:
            last = exc
    raise last


def is_inside(
    candidate: Optional[Union[str, Path]],
    root: Optional[Union[str, Path]] = None,
    *,
    allow_root: bool = False,
) -> bool:
    """Boolean form of :func:`confine`, for callers that return a dict."""
    try:
        confine(candidate, root, allow_root=allow_root)
        return True
    except DatasetPathError:
        return False


def safe_child(
    root: Union[str, Path],
    name: Optional[str],
    *,
    allow_root: bool = False,
) -> Path:
    """Join a single client-supplied NAME onto ``root``, confined.

    This is the ``get_dataset_list_callback`` shape: the caller has a trusted
    root and one untrusted component. Written as its own helper because the
    naive ``root / name`` is the exact bug — pathlib DISCARDS ``root`` when
    ``name`` is absolute, so ``root / '/etc'`` is ``/etc``.
    """
    if name is None or not isinstance(name, str) or not name.strip():
        raise DatasetPathError(EMPTY_PATH_DE)
    # Refuse a separator outright: a legitimate user id / dataset name is one
    # component. This is a cheap early no, and `confine` below is the fence.
    if os.sep in name or (os.altsep and os.altsep in name):
        raise DatasetPathError(OUTSIDE_ROOT_DE)
    return confine(Path(root) / name, root, allow_root=allow_root)

#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Object catalog for the Roboter Studio named-object grasping workflow.

The catalog maps each **type** of printed object to the set of AprilTag ids
glued to its physical copies and to a per-object grasp recipe (height, grasp
depth, gripper close angle, approach clearance). The named-object Blockly blocks
(``Greife <Objekt>``, ``Solange <Typ> sichtbar`` …) resolve a chosen type to its
tag ids + recipe through this catalog.

Design (CLAUDE.md / plan 2026-06-22):

* **Shipped default + per-rig override** — the STANDARD printed-object set is a
  fixed, measured-once-valid-everywhere constant baked into the image
  (:data:`_SHIPPED_DEFAULT_CATALOG`), so every PC has the objects out of the box
  with no per-PC setup and no cloud sync. A teacher can override/extend on one
  rig by dropping an ``object_catalog.json`` into the ``edubotics_calib`` volume;
  :func:`resolve_catalog` prefers that file when present (no image rebuild),
  else uses the shipped default. Changing the STANDARD set means editing the
  constant and rebuilding the image (COPY-wholesale, Rule §3). The active catalog
  is re-read each workflow start, so a volume edit applies on the next run.
* **Fail loud in German** — a missing / corrupt / schema-invalid catalog raises
  :class:`ObjectCatalogError` with a student-facing German message. The runtime
  surfaces it at the first named-object block, never silently degrades.
* **Stable per-object identity** — every physical copy carries its OWN tag id,
  all grouped into one type. Tag ids are **globally unique** across types so a
  detected tag maps unambiguously back to its type + recipe (this is what makes
  the multi-instance ``Solange … sichtbar`` loop deterministic).

Schema (see plan §9)::

    {
      "tag_size_m": 0.024,                 // optional; overrides EDUBOTICS_TAG_SIZE_M
      "types": {
        "banane": {
          "label_de": "Banane",            // German dropdown label
          "tag_ids": [20, 21, 22, 23, 24], // each copy its own id; all typed 'banane'
          "object_height_m": 0.040,        // tag/top plane above the table
          "grasp_depth_m": 0.015,          // jaws close this far below the top
          "gripper_close_rad": -0.30,      // tuned to the body width at the grasp band
          "approach_clear_m": 0.06         // optional (default 0.06): hover height
        }
      }
    }

Pure-Python + stdlib only (``json``/``os``/``pathlib``/``dataclasses``) — unit
testable without a container.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_logger = logging.getLogger(__name__)


# Default physical tag size (tag36h11 black-square edge length, metres) when
# neither the catalog ``tag_size_m`` nor EDUBOTICS_TAG_SIZE_M is set.
_DEFAULT_TAG_SIZE_M = 0.024
# Default hover/approach clearance above an object (metres) when a type omits
# ``approach_clear_m``. Mirrors motion.DEFAULT_APPROACH_HEIGHT_M (kept as a
# literal here to avoid importing the ROS-coupled motion module into this
# stdlib-only loader).
_DEFAULT_APPROACH_CLEAR_M = 0.06

# Type names become Blockly dropdown VALUES and lowercased handler arg values,
# so they must be simple tokens (no spaces / punctuation). The German display
# text lives in ``label_de``; the key is the internal identifier.
_TYPE_NAME_RE = re.compile(r'^[A-Za-z0-9_]+$')


class ObjectCatalogError(Exception):
    """Raised with a German, student-facing message when the catalog is
    missing, corrupt, or schema-invalid, or when a lookup names an unknown
    type. The workflow runtime surfaces ``str(err)`` to the editor."""


@dataclass(frozen=True)
class GraspRecipe:
    """Per-type grasp parameters resolved from one catalog entry."""

    type_name: str
    label_de: str
    tag_ids: tuple[int, ...]
    object_height_m: float
    grasp_depth_m: float
    gripper_close_rad: float
    approach_clear_m: float


class ObjectCatalog:
    """Validated, indexed view over the catalog JSON.

    Build via :func:`load_catalog` (from a file) or :func:`parse_catalog`
    (from an already-decoded dict). Exposes type→recipe and tag_id→recipe
    indices plus the dropdown label list.
    """

    def __init__(
        self,
        tag_size_m: float,
        recipes: dict[str, GraspRecipe],
    ) -> None:
        self._tag_size_m = float(tag_size_m)
        # Insertion order preserved (Python 3.7+) so dropdowns list types in
        # the order the teacher wrote them.
        self._by_type: dict[str, GraspRecipe] = dict(recipes)
        by_tag: dict[int, GraspRecipe] = {}
        for recipe in self._by_type.values():
            for tid in recipe.tag_ids:
                by_tag[tid] = recipe
        self._by_tag: dict[int, GraspRecipe] = by_tag

    # ── accessors ────────────────────────────────────────────────────────────
    @property
    def tag_size_m(self) -> float:
        """Physical AprilTag size (metres) for solvePnP scale."""
        return self._tag_size_m

    def type_names(self) -> list[str]:
        """Catalog type keys, in catalog order (Blockly dropdown values)."""
        return list(self._by_type.keys())

    def labels(self) -> list[tuple[str, str]]:
        """``(type_name, label_de)`` pairs, in catalog order — the source for
        the Blockly dropdown ``[[label_de, type_name], …]``."""
        return [(name, r.label_de) for name, r in self._by_type.items()]

    def recipe_for_type(self, type_name: str) -> GraspRecipe:
        """Recipe for a type name; raises German :class:`ObjectCatalogError`
        for an unknown type (so a stale block names the missing object)."""
        recipe = self._by_type.get(str(type_name))
        if recipe is None:
            raise ObjectCatalogError(
                f'Unbekanntes Objekt „{type_name}" — bitte den Objekt-Katalog '
                'prüfen oder ein gültiges Objekt auswählen.'
            )
        return recipe

    def recipe_for_tag(self, tag_id: int) -> Optional[GraspRecipe]:
        """Recipe for a detected tag id, or ``None`` if the id is not in the
        catalog (an unknown tag in the scene is ignored, not an error)."""
        return self._by_tag.get(int(tag_id))

    def tag_ids_for_type(self, type_name: str) -> tuple[int, ...]:
        """All catalog tag ids belonging to a type (raises for unknown type)."""
        return self.recipe_for_type(type_name).tag_ids

    def all_tag_ids(self) -> frozenset[int]:
        """Every tag id known to the catalog (across all types)."""
        return frozenset(self._by_tag.keys())


# ── env / path resolution ────────────────────────────────────────────────────
def _calib_dir() -> Path:
    # Matches calibration_manager.py: read EDUBOTICS_CALIB_DIR (compose forwards
    # it; runtime value points inside the edubotics_calib volume). The literal
    # default mirrors that module.
    return Path(os.environ.get('EDUBOTICS_CALIB_DIR', '/root/.cache/edubotics/calibration'))


def default_catalog_path() -> Path:
    """Resolve the catalog path: ``EDUBOTICS_OBJECT_CATALOG`` if set, else
    ``<EDUBOTICS_CALIB_DIR>/object_catalog.json`` (in the calib volume)."""
    override = os.environ.get('EDUBOTICS_OBJECT_CATALOG')
    if override:
        return Path(override)
    return _calib_dir() / 'object_catalog.json'


# ── shipped default catalog (baked into the image) ───────────────────────────
# The named-object set is a FIXED, standardized set of printed EduBotics objects
# (each with an assigned AprilTag) — the same physical object on every student's
# desk, so its grasp recipe is "measured once, valid everywhere" (like the
# collision thresholds calibrated on the reference rig). It therefore ships baked
# into the image so EVERY PC has the objects out of the box — no per-PC file, no
# cloud sync. A teacher can still override/extend per rig by dropping an
# ``object_catalog.json`` into the calib volume (see :func:`resolve_catalog`),
# which wins and needs no image rebuild. To change the STANDARD set, edit this
# constant and rebuild the physical-ai-server image (COPY-wholesale, Rule §3).
#
# „Würfel" numbers are the rig-validated values (2026-06-27).
_SHIPPED_DEFAULT_CATALOG: dict = {
    'tag_size_m': 0.024,
    'types': {
        'wuerfel': {
            'label_de': 'Würfel',
            'tag_ids': [20, 21],
            'object_height_m': 0.030,
            'grasp_depth_m': 0.015,
            'gripper_close_rad': -0.5,
            'approach_clear_m': 0.06,
        },
    },
}

# A pre-shipped-default build seeded this placeholder INTO the volume on first
# run. Such a file must NOT shadow the baked-in default, so :func:`resolve_catalog`
# recognises the untouched seed (exactly one ``beispiel`` type on tag 0 with this
# label) and falls back to the shipped default — healing already-seeded PCs. A
# teacher who edited that entry no longer matches, so their edit is honoured.
_SEED_SENTINEL_TYPE = 'beispiel'
_SEED_SENTINEL_LABEL = 'Beispiel-Objekt (bitte anpassen)'


def shipped_default_catalog() -> ObjectCatalog:
    """The standard EduBotics printed-object set, baked into the image. Used on
    every PC that has no teacher override in the calib volume."""
    return parse_catalog(_SHIPPED_DEFAULT_CATALOG)


def _is_untouched_seed(catalog: ObjectCatalog) -> bool:
    """True iff ``catalog`` is exactly the legacy auto-seed placeholder (one
    ``beispiel`` type labelled "Beispiel-Objekt (bitte anpassen)" on tag id 0),
    written into a volume by an older build before the shipped default existed."""
    if catalog.type_names() != [_SEED_SENTINEL_TYPE]:
        return False
    recipe = catalog.recipe_for_type(_SEED_SENTINEL_TYPE)
    return recipe.label_de == _SEED_SENTINEL_LABEL and recipe.tag_ids == (0,)


def resolve_catalog(path: Optional[Path | str] = None) -> ObjectCatalog:
    """Resolve the ACTIVE object catalog with a two-level precedence:

    1. A teacher's ``object_catalog.json`` in the calib volume (``path`` /
       :func:`default_catalog_path`) WINS if present and customized — a rig can
       add or adjust objects with no image rebuild (the runtime-editable path).
    2. Otherwise the SHIPPED default (baked into this module,
       :func:`shipped_default_catalog`) is used, so every fresh PC has the
       standard objects out of the box.

    An untouched auto-seed placeholder in the volume is treated as NOT
    customized and falls back to the shipped default (:func:`_is_untouched_seed`).
    A volume file that EXISTS but is corrupt / schema-invalid still raises German
    :class:`ObjectCatalogError` — a broken teacher edit fails loud, it is never
    silently swapped for the default. Read fresh each call, so a volume edit
    applies on the next workflow run without a full restart."""
    catalog_path = Path(path) if path is not None else default_catalog_path()
    if not catalog_path.is_file():
        return shipped_default_catalog()
    catalog = load_catalog(catalog_path)  # German ObjectCatalogError on corrupt/invalid
    if _is_untouched_seed(catalog):
        return shipped_default_catalog()
    return catalog


def _env_tag_size_m() -> float:
    """EDUBOTICS_TAG_SIZE_M with a logged fallback (never raises at load)."""
    raw = os.environ.get('EDUBOTICS_TAG_SIZE_M')
    if raw is None:
        return _DEFAULT_TAG_SIZE_M
    try:
        val = float(raw)
    except (TypeError, ValueError):
        _logger.warning(
            '[WARNUNG] EDUBOTICS_TAG_SIZE_M=%r is not a number — falling back '
            'to %s m.', raw, _DEFAULT_TAG_SIZE_M,
        )
        return _DEFAULT_TAG_SIZE_M
    if val != val or val in (float('inf'), float('-inf')) or val <= 0.0:
        _logger.warning(
            '[WARNUNG] EDUBOTICS_TAG_SIZE_M=%r is not a finite positive number '
            '— falling back to %s m.', raw, _DEFAULT_TAG_SIZE_M,
        )
        return _DEFAULT_TAG_SIZE_M
    return val


# ── validation helpers ───────────────────────────────────────────────────────
def _require_number(value, type_name: str, field: str, *, positive: bool = False,
                    non_negative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ObjectCatalogError(
            f'Objekt „{type_name}": Feld „{field}" fehlt oder ist keine Zahl.'
        )
    val = float(value)
    if val != val or val in (float('inf'), float('-inf')):  # NaN / inf
        raise ObjectCatalogError(
            f'Objekt „{type_name}": Feld „{field}" ist keine gültige Zahl.'
        )
    if positive and val <= 0.0:
        raise ObjectCatalogError(
            f'Objekt „{type_name}": Feld „{field}" muss größer als 0 sein.'
        )
    if non_negative and val < 0.0:
        raise ObjectCatalogError(
            f'Objekt „{type_name}": Feld „{field}" darf nicht negativ sein.'
        )
    return val


def _parse_type(type_name: str, entry, seen_tags: dict[int, str]) -> GraspRecipe:
    if not isinstance(type_name, str) or not _TYPE_NAME_RE.match(type_name):
        raise ObjectCatalogError(
            f'Ungültiger Objekt-Schlüssel „{type_name}" — erlaubt sind nur '
            'Buchstaben, Ziffern und Unterstriche (z. B. „banane", „wuerfel_blau").'
        )
    if not isinstance(entry, dict):
        raise ObjectCatalogError(
            f'Objekt „{type_name}": Eintrag muss ein Objekt (key/value) sein.'
        )

    label_de = entry.get('label_de')
    if not isinstance(label_de, str) or not label_de.strip():
        raise ObjectCatalogError(
            f'Objekt „{type_name}": Feld „label_de" (deutscher Anzeigename) '
            'fehlt oder ist leer.'
        )

    raw_ids = entry.get('tag_ids')
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ObjectCatalogError(
            f'Objekt „{type_name}": Feld „tag_ids" fehlt oder ist leer — jedes '
            'Objekt braucht mindestens eine AprilTag-ID.'
        )
    tag_ids: list[int] = []
    local_seen: set[int] = set()
    for raw in raw_ids:
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ObjectCatalogError(
                f'Objekt „{type_name}": Tag-ID „{raw}" ist keine ganze Zahl.'
            )
        if raw < 0:
            raise ObjectCatalogError(
                f'Objekt „{type_name}": Tag-ID {raw} darf nicht negativ sein.'
            )
        if raw in local_seen:
            raise ObjectCatalogError(
                f'Objekt „{type_name}": Tag-ID {raw} ist doppelt aufgeführt.'
            )
        owner = seen_tags.get(raw)
        if owner is not None:
            raise ObjectCatalogError(
                f'Tag-ID {raw} ist mehreren Objekten zugeordnet '
                f'(„{owner}" und „{type_name}") — jede Tag-ID darf nur einem '
                'Objekt gehören.'
            )
        local_seen.add(raw)
        seen_tags[raw] = type_name
        tag_ids.append(raw)

    object_height_m = _require_number(
        entry.get('object_height_m'), type_name, 'object_height_m', positive=True)
    grasp_depth_m = _require_number(
        entry.get('grasp_depth_m'), type_name, 'grasp_depth_m', non_negative=True)
    if grasp_depth_m > object_height_m + 1e-9:
        raise ObjectCatalogError(
            f'Objekt „{type_name}": Greiftiefe (grasp_depth_m={grasp_depth_m}) '
            f'darf nicht größer als die Objekthöhe '
            f'(object_height_m={object_height_m}) sein.'
        )
    gripper_close_rad = _require_number(
        entry.get('gripper_close_rad'), type_name, 'gripper_close_rad')

    # approach_clear_m is optional.
    if 'approach_clear_m' in entry:
        approach_clear_m = _require_number(
            entry.get('approach_clear_m'), type_name, 'approach_clear_m', positive=True)
    else:
        approach_clear_m = _DEFAULT_APPROACH_CLEAR_M

    return GraspRecipe(
        type_name=type_name,
        label_de=label_de,
        tag_ids=tuple(tag_ids),
        object_height_m=object_height_m,
        grasp_depth_m=grasp_depth_m,
        gripper_close_rad=gripper_close_rad,
        approach_clear_m=approach_clear_m,
    )


# ── public loaders ───────────────────────────────────────────────────────────
def parse_catalog(data) -> ObjectCatalog:
    """Validate an already-decoded catalog dict and build an
    :class:`ObjectCatalog`. Raises German :class:`ObjectCatalogError` on any
    schema violation."""
    if not isinstance(data, dict):
        raise ObjectCatalogError(
            'Objekt-Katalog ungültig: die oberste Ebene muss ein JSON-Objekt sein.'
        )
    types = data.get('types')
    if not isinstance(types, dict) or not types:
        raise ObjectCatalogError(
            'Objekt-Katalog ungültig: Abschnitt „types" fehlt oder ist leer.'
        )

    # tag_size_m (catalog) overrides EDUBOTICS_TAG_SIZE_M; env default otherwise.
    if 'tag_size_m' in data:
        raw_size = data.get('tag_size_m')
        if isinstance(raw_size, bool) or not isinstance(raw_size, (int, float)) \
                or float(raw_size) != float(raw_size) \
                or float(raw_size) in (float('inf'), float('-inf')) \
                or float(raw_size) <= 0.0:
            raise ObjectCatalogError(
                'Objekt-Katalog ungültig: „tag_size_m" muss eine positive Zahl '
                '(Tag-Größe in Metern) sein.'
            )
        tag_size_m = float(raw_size)
    else:
        tag_size_m = _env_tag_size_m()

    seen_tags: dict[int, str] = {}
    recipes: dict[str, GraspRecipe] = {}
    for type_name, entry in types.items():
        recipes[type_name] = _parse_type(type_name, entry, seen_tags)

    return ObjectCatalog(tag_size_m=tag_size_m, recipes=recipes)


def load_catalog(path: Optional[Path | str] = None) -> ObjectCatalog:
    """Load + validate the catalog from ``path`` (default
    :func:`default_catalog_path`). Raises German :class:`ObjectCatalogError`
    when the file is missing, unreadable, not valid JSON, or schema-invalid."""
    catalog_path = Path(path) if path is not None else default_catalog_path()
    if not catalog_path.is_file():
        raise ObjectCatalogError(
            f'Objekt-Katalog nicht gefunden: {catalog_path}. Bitte die Datei '
            '„object_catalog.json" im Kalibrier-Ordner anlegen.'
        )
    try:
        text = catalog_path.read_text(encoding='utf-8')
    except OSError as err:
        raise ObjectCatalogError(
            f'Objekt-Katalog konnte nicht gelesen werden: {catalog_path} ({err}).'
        ) from err
    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        raise ObjectCatalogError(
            f'Objekt-Katalog ist beschädigt (kein gültiges JSON): {err}.'
        ) from err
    return parse_catalog(data)

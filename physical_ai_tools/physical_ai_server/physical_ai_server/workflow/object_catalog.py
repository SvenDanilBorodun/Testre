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

Design — ONE FIXED SET FOR THE WHOLE FLEET (2026-07-02):

* **Fixed, hardcoded object set** — the object set is a single constant baked
  into the image (:data:`_FIXED_CATALOG`), identical on **every machine and every
  user**. There is NO per-rig file, NO ``edubotics_calib`` override, NO seeding,
  and NO cloud sync — a fixed set is what makes a student's saved workflow
  resolve the same way on any classroom PC. The set is read through
  :func:`fixed_catalog`; to change it, edit the constant and rebuild the
  physical-ai-server image (COPY-wholesale, Rule §3). This is the same
  "measured once, valid everywhere" model as the collision thresholds.
* **No file I/O** — :func:`fixed_catalog` parses an in-memory constant, so the
  catalog can never be "missing" or "corrupt" at runtime on a student PC. The
  German fail-loud validation still exists in :func:`parse_catalog`, but on the
  fixed path it can only fire for a developer typo in the constant — caught by
  the unit tests at build time, never by a teacher.
* **Tag size is env-tunable** — the physical AprilTag edge length comes from
  ``EDUBOTICS_TAG_SIZE_M`` (default :data:`_DEFAULT_TAG_SIZE_M`), re-read on every
  :func:`fixed_catalog` call, so a rig that prints a different tag size can be
  corrected without an image rebuild. The fixed constant deliberately omits
  ``tag_size_m`` so the env is the single source (a ``tag_size_m`` inside a
  catalog would shadow the env — see :func:`parse_catalog`).
* **Stable per-object identity** — every physical copy carries its OWN tag id,
  all grouped into one type. Tag ids are **globally unique** across types so a
  detected tag maps unambiguously back to its type + recipe (this is what makes
  the multi-instance ``Solange … sichtbar`` loop deterministic).

Schema accepted by :func:`parse_catalog` (the fixed constant is one instance)::

    {
      "tag_size_m": 0.024,                 // OPTIONAL; overrides EDUBOTICS_TAG_SIZE_M.
                                           //   Omitted in the fixed set so the env wins.
      "types": {
        "banane": {
          "label_de": "Banane",            // German dropdown label
          "tag_ids": [20, 21, 22, 23, 24], // each copy its own id; all typed 'banane'
          "object_height_m": 0.040,        // tag/top plane above the table
          "grasp_depth_m": 0.015,          // jaws close this far below the top
          "gripper_close_rad": -0.30,      // must be NEGATIVE (motion + sim assume closes < 0);
                                           //   command ≥ ~0.15 rad DEEPER than where the
                                           //   jaws meet the body (motion.GRASP_HELD_MARGIN_RAD)
                                           //   or grasp verification can't tell miss from hold
          "approach_clear_m": 0.06,        // optional (default 0.06): hover height
          "object_width_m": 0.030,         // optional (default = object_height_m): render footprint width
          "color_hex": "#f59e0b"           // optional (default amber): simulation render colour
        }
      }
    }

Physical tag-mounting convention (per object type, fleet-wide):

* The AprilTag sits FLAT on the object's TOP face — ``object_height_m`` is the
  tag plane's height and the projection maths assumes a horizontal tag; a
  side-mounted or tilted tag breaks both position and yaw recovery. Keep the
  white quiet zone (≥ one tag-edge) around the black square.
* The wrist roll TRACKS the tag: ``joint5 = base_yaw − tag_yaw + GRASP_ROLL``,
  so WHERE the jaws close on the body is fixed by the tag's printed orientation
  on the object. For rotationally symmetric objects (cube) any gluing works;
  for an ELONGATED object glue the tag so the jaws land across the graspable
  (narrow) axis — same orientation on EVERY physical copy of the type — and
  validate the first copy on the rig (there is no runtime guard for this,
  same as the ``GRASP_ROLL_DEG`` jaw convention).

Pure-Python + stdlib only (``os``/``re``/``dataclasses``) — unit testable
without a container.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

_logger = logging.getLogger(__name__)


# Default physical tag size (tag36h11 black-square edge length, metres) when
# EDUBOTICS_TAG_SIZE_M is unset / invalid.
_DEFAULT_TAG_SIZE_M = 0.024
# Default hover/approach clearance above an object (metres) when a type omits
# ``approach_clear_m``. Mirrors motion.DEFAULT_APPROACH_HEIGHT_M (kept as a
# literal here to avoid importing the ROS-coupled motion module into this
# stdlib-only loader).
_DEFAULT_APPROACH_CLEAR_M = 0.06
# Default render colour (amber) for a catalog object when a type omits
# ``color_hex``. Render-only (simulation view / 3D twin), not a physical
# property, so it has no rig-measured value.
_DEFAULT_OBJECT_COLOR_HEX = '#f59e0b'

# Type names become Blockly dropdown VALUES and lowercased handler arg values,
# so they must be simple tokens (no spaces / punctuation). The German display
# text lives in ``label_de``; the key is the internal identifier.
_TYPE_NAME_RE = re.compile(r'^[A-Za-z0-9_]+$')
# ``color_hex`` render colour must be a 6-digit hex string like ``#f59e0b``.
_COLOR_HEX_RE = re.compile(r'^#[0-9A-Fa-f]{6}$')


class ObjectCatalogError(Exception):
    """Raised with a German, student-facing message when the catalog is
    schema-invalid, or when a lookup names an unknown type. The workflow runtime
    surfaces ``str(err)`` to the editor. On the fixed-set path a schema error can
    only mean a developer typo in :data:`_FIXED_CATALOG` (caught by tests); an
    unknown-type lookup can still happen for a stale saved workflow that names an
    object no longer in the set."""


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
    # Render-only fields (simulation view), appended LAST WITH defaults because
    # dataclass ordering forbids a defaulted field before the non-defaulted ones
    # above. ``object_width_m`` 0.0 is a sentinel that ``_parse_type`` always
    # resolves (it defaults to object_height_m — a cube — when the entry omits
    # the key), so a resolved recipe never carries the 0.0.
    object_width_m: float = 0.0
    color_hex: str = _DEFAULT_OBJECT_COLOR_HEX


class ObjectCatalog:
    """Validated, indexed view over the catalog.

    Build via :func:`fixed_catalog` (the baked-in fleet-wide set) or
    :func:`parse_catalog` (from an already-decoded dict — used by tests).
    Exposes type→recipe and tag_id→recipe indices plus the dropdown label list.
    """

    def __init__(
        self,
        tag_size_m: float,
        recipes: dict[str, GraspRecipe],
    ) -> None:
        self._tag_size_m = float(tag_size_m)
        # Insertion order preserved (Python 3.7+) so dropdowns list types in
        # the order they were written.
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


# ── the FIXED object set (baked into the image) ──────────────────────────────
# The named-object set is a FIXED, standardized set of printed EduBotics objects
# (each with an assigned AprilTag) — the SAME physical object on every student's
# desk, so its grasp recipe is "measured once, valid everywhere" (like the
# collision thresholds calibrated on the reference rig). It is hardcoded here so
# EVERY machine and EVERY user gets an identical set: no per-rig file, no cloud
# sync, no seeding — which is what lets a student's saved workflow resolve the
# same way on any classroom PC. To change the set, edit this constant and rebuild
# the physical-ai-server image (COPY-wholesale, Rule §3).
#
# Note: NO ``tag_size_m`` here on purpose — a value inside the catalog would
# shadow EDUBOTICS_TAG_SIZE_M (see parse_catalog). Tag size stays env-tunable.
#
# „Würfel" numbers are the rig-validated values (2026-06-27). Add more objects by
# giving each a unique key, its own globally-unique tag id(s) (one per physical
# copy), and rig-measured object_height_m / grasp_depth_m / gripper_close_rad
# (commanded ≥ ~0.15 rad deeper than where the jaws meet the body — see the
# module docstring's schema notes + tag-mounting convention). Print the matching
# tag sheet with tools/generate_apriltags.py (catalog-driven by default), update
# the pinned fixed-set tests in test/test_object_catalog.py, and rebuild the
# image. Everything downstream (Blockly dropdowns, sim palette + placement caps,
# 2D/3D render dims, cloud persistence) picks the new type up automatically.
_FIXED_CATALOG: dict = {
    'types': {
        'wuerfel': {
            'label_de': 'Würfel',
            'tag_ids': [20, 21],
            'object_height_m': 0.030,
            'grasp_depth_m': 0.015,
            'gripper_close_rad': -0.5,
            'approach_clear_m': 0.06,
            'object_width_m': 0.030,
            'color_hex': '#f59e0b',
        },
    },
}

# edu6_studio variant: the SAME physical objects + tag ids (one printed tag
# sheet fleet-wide), only the gripper close differs — the edu6 gripper channel
# is the end_gear servo angle in RADIANS (0 = closed … 1.75 = open command,
# jaw ≈ 25.2 mm/rad). A 30 mm cube blocks the jaws at ≈ 1.19 rad; commanding
# 1.0 leaves ≈ 0.19 rad of squeeze — comfortably above the profile's 0.12
# grasp-held margin (bench-tunable at rig gates R3/R4).
_FIXED_CATALOG_EDU6: dict = {
    'types': {
        'wuerfel': {
            'label_de': 'Würfel',
            'tag_ids': [20, 21],
            'object_height_m': 0.030,
            'grasp_depth_m': 0.015,
            'gripper_close_rad': 1.0,
            'approach_clear_m': 0.06,
            'object_width_m': 0.030,
            'color_hex': '#f59e0b',
        },
    },
}


# edu1_studio variant: again the SAME physical objects + tag ids, again only the
# gripper close differs. The edu1 channel is the claw servo angle in RADIANS
# (0 = jaws closed … 0.90 = open command) — the SAME polarity as the edu6 and
# the OPPOSITE of the raw CAD export, which the shipped URDF copy flips (see
# robot_profiles._EDU1_HOME_JOINTS_RAD).
#
# Where 0.10 comes from: simulated against the shipped claw STLs, a 30 mm cube
# standing on the table blocks the blades at ≈0.25 rad with the end-effector
# origin 0.090–0.115 m above the table. Commanding 0.10 leaves ≈0.15 rad of
# squeeze — comfortably above the profile's 0.10 grasp-held margin, so a HELD
# grasp reads 0.25 against a 0.20 threshold while a MISS closes to the commanded
# 0.10 and reads below it (bench-tunable at rig gate E5).
_FIXED_CATALOG_EDU1: dict = {
    'types': {
        'wuerfel': {
            'label_de': 'Würfel',
            'tag_ids': [20, 21],
            'object_height_m': 0.030,
            'grasp_depth_m': 0.015,
            'gripper_close_rad': 0.10,
            'approach_clear_m': 0.06,
            'object_width_m': 0.030,
            'color_hex': '#f59e0b',
        },
    },
}

# profile id → (catalog constant, gripper close band). A TABLE rather than a
# chain of ``==`` tests: adding an arm is one row, and the band is stated right
# next to the catalog whose close value it validates. An id that is not in here
# — both OMX profiles, ``None``, an unknown id — gets the OMX set on the
# default (negative-close) rule, byte-identical to before.
_CATALOG_BY_PROFILE: dict = {
    'edu6_studio': (_FIXED_CATALOG_EDU6, (0.0, 1.75)),
    'edu1_studio': (_FIXED_CATALOG_EDU1, (0.0, 0.90)),
}


def fixed_catalog(profile_id: Optional[str] = None) -> ObjectCatalog:
    """The single, fleet-wide named-object set — identical on every machine and
    user. Parses the in-memory constant for the arm family (no file I/O, so it
    can never be missing/corrupt at runtime) and picks up the physical tag size
    from ``EDUBOTICS_TAG_SIZE_M`` on each call (env-tunable, re-read per
    workflow start).

    ``profile_id`` selects through :data:`_CATALOG_BY_PROFILE`: the Feetech arms
    have radian gripper bands that close UPWARD from zero, everything else —
    both OMX profiles, ``None``, an unknown id — gets the OMX set with its
    negative-close rule, byte-identical to before."""
    entry = _CATALOG_BY_PROFILE.get((profile_id or '').strip())
    if entry is not None:
        catalog, close_range = entry
        return parse_catalog(catalog, gripper_close_range=close_range)
    return parse_catalog(_FIXED_CATALOG)


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


def _parse_type(type_name: str, entry, seen_tags: dict[int, str],
                gripper_close_range=None) -> GraspRecipe:
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
    # Close-value validation is PER ARM FAMILY. Default (None) = the OMX rule,
    # verbatim: motion's held threshold + close_on_object's clamp band + the
    # SimArm close-command detection all hard-assume a NEGATIVE close there.
    # An explicit ``gripper_close_range=(lo, hi)`` (edu6: the radian jaw band
    # 0…1.75, closes DOWNWARD from open) replaces it with a band check.
    if gripper_close_range is None:
        if gripper_close_rad >= 0.0:
            raise ObjectCatalogError(
                f'Objekt „{type_name}": Feld „gripper_close_rad" muss negativ sein '
                '(Schließwinkel in rad — offen ist +0.8, geschlossen z. B. −0.5).'
            )
    else:
        lo, hi = float(gripper_close_range[0]), float(gripper_close_range[1])
        if not (lo <= gripper_close_rad < hi):
            raise ObjectCatalogError(
                f'Objekt „{type_name}": Feld „gripper_close_rad" muss zwischen '
                f'{lo} und {hi} liegen (Schließwinkel in rad für diesen '
                'Greifer).'
            )

    # approach_clear_m is optional.
    if 'approach_clear_m' in entry:
        approach_clear_m = _require_number(
            entry.get('approach_clear_m'), type_name, 'approach_clear_m', positive=True)
    else:
        approach_clear_m = _DEFAULT_APPROACH_CLEAR_M

    # object_width_m is optional (render-only footprint width). Absent → default
    # to the object height (a cube); present → must be a positive number.
    if 'object_width_m' in entry:
        object_width_m = _require_number(
            entry.get('object_width_m'), type_name, 'object_width_m', positive=True)
    else:
        object_width_m = object_height_m

    # color_hex is optional (render-only). Absent → default amber; present →
    # must be a 6-digit hex colour like "#f59e0b".
    if 'color_hex' in entry:
        color_hex = entry.get('color_hex')
        if not isinstance(color_hex, str) or not _COLOR_HEX_RE.match(color_hex):
            raise ObjectCatalogError(
                f'Objekt „{type_name}": Feld „color_hex" muss eine Farbe im '
                'Format „#RRGGBB" sein.'
            )
    else:
        color_hex = _DEFAULT_OBJECT_COLOR_HEX

    return GraspRecipe(
        type_name=type_name,
        label_de=label_de,
        tag_ids=tuple(tag_ids),
        object_height_m=object_height_m,
        grasp_depth_m=grasp_depth_m,
        gripper_close_rad=gripper_close_rad,
        approach_clear_m=approach_clear_m,
        object_width_m=object_width_m,
        color_hex=color_hex,
    )


# ── public loader ────────────────────────────────────────────────────────────
def parse_catalog(data, gripper_close_range=None) -> ObjectCatalog:
    """Validate an already-decoded catalog dict and build an
    :class:`ObjectCatalog`. Raises German :class:`ObjectCatalogError` on any
    schema violation. A ``tag_size_m`` in the dict overrides
    ``EDUBOTICS_TAG_SIZE_M``; the env (default :data:`_DEFAULT_TAG_SIZE_M`) is
    used when the dict omits it — which the fixed set does on purpose.
    ``gripper_close_range`` selects the per-arm-family close validation (see
    :func:`_parse_type`; ``None`` = the OMX negative-close rule)."""
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
        recipes[type_name] = _parse_type(type_name, entry, seen_tags,
                           gripper_close_range=gripper_close_range)

    return ObjectCatalog(tag_size_m=tag_size_m, recipes=recipes)


# ── GetObjectCatalog.srv response helpers ────────────────────────────────────
# Field names are the srv response array names; the ROS handler assigns each list
# to the matching response attribute. Kept here (pure / no rclpy) so the whole
# wire contract is unit-testable without a container.
def catalog_response_fields(catalog: ObjectCatalog) -> dict:
    """Build the six parallel arrays for ``GetObjectCatalog.srv`` from a
    validated :class:`ObjectCatalog`, in catalog order. ``max_instances`` is
    ``len(recipe.tag_ids)`` — how many copies of a type the simulator can detect
    (the 2D editor's placement cap). All returned lists are the same length."""
    type_names: list[str] = []
    labels_de: list[str] = []
    object_height_m: list[float] = []
    object_width_m: list[float] = []
    color_hex: list[str] = []
    max_instances: list[int] = []
    for name in catalog.type_names():
        recipe = catalog.recipe_for_type(name)
        type_names.append(name)
        labels_de.append(recipe.label_de)
        object_height_m.append(float(recipe.object_height_m))
        object_width_m.append(float(recipe.object_width_m))
        color_hex.append(str(recipe.color_hex))
        max_instances.append(len(recipe.tag_ids))
    return {
        'type_names': type_names,
        'labels_de': labels_de,
        'object_height_m': object_height_m,
        'object_width_m': object_width_m,
        'color_hex': color_hex,
        'max_instances': max_instances,
    }


def build_object_catalog_response() -> dict:
    """Full ``GetObjectCatalog.srv`` response payload for the fixed, fleet-wide
    catalog: the six parallel arrays plus ``success`` + ``message``. Pure / ROS
    -free so the wire contract can be unit-tested end-to-end. On any error (only
    a developer typo in :data:`_FIXED_CATALOG` can trigger it) ALL SIX arrays are
    emptied together, ``success`` is ``False``, and ``message`` carries the
    German reason — parity across every array. Only an
    :class:`ObjectCatalogError` text is forwarded verbatim (those are German by
    contract); any other exception (e.g. a ``TypeError`` from a malformed
    constant) would leak an English message to the editor, so it is replaced by
    a fixed German fallback."""
    try:
        fields = catalog_response_fields(fixed_catalog())
        fields['success'] = True
        fields['message'] = ''
    except Exception as e:
        message = (
            str(e) if isinstance(e, ObjectCatalogError)
            else 'Objekt-Katalog konnte nicht geladen werden.'
        )
        fields = {
            'type_names': [],
            'labels_de': [],
            'object_height_m': [],
            'object_width_m': [],
            'color_hex': [],
            'max_instances': [],
            'success': False,
            'message': message,
        }
    return fields

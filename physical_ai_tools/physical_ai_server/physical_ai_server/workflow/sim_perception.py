#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Virtual AprilTag perception for the Roboter Studio simulation runtime (Phase 3).

``SimPerception`` is duck-typed to ``workflow.perception.Perception`` so a sim
``WorkflowManager`` (built with ``perception_factory=lambda: SimPerception(...)``)
runs the real named-object handlers (``handlers.perception_blocks``) unchanged.

Instead of detecting tags in a camera frame, it synthesises one
``perception.Detection`` per PLACED virtual object from the sim scene, with the
grasp point + tag yaw PRE-SET. Because the sim runs with all calibration absent
(``load_calibration`` returns ``{}``), ``perception_blocks._attach_named_world``
early-returns and PRESERVES the pre-set ``world_xyz_m`` / ``extras`` exactly — so
the detection flows to ``_select_nearest_reachable`` → ``_resolve_target`` → the
real IK with no projection step. Virtual table at z = 0, so the grasp z is
``object_height_m − grasp_depth_m`` (the body band below the tag-top plane,
mirroring the real path's ``z_table + object_height − grasp_depth`` with
``z_table = 0``).

Pure-Python + stdlib (plus the shared ``Detection`` dataclass) — unit-testable
without a container.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Optional

from physical_ai_server.workflow.perception import Detection
from physical_ai_server.workflow.sim_world import MAX_SIM_OBJECTS


_logger = logging.getLogger(__name__)

# Defense-in-depth cap on placed sim objects (the cloud validator already bounds
# the scene at 64 KB ≈ hundreds of entries; this bounds the one-time resolve loop
# and the per-object skip-warning volume regardless of payload source).
# Single-sourced from sim_world so the mutable scene and the detector can never
# disagree about which placements exist — a world entry the detector never emits
# would be capturable but invisible.
_MAX_SIM_OBJECTS = MAX_SIM_OBJECTS


class SimPerception:
    """Synthesise named-object detections from the placed sim-scene objects.

    ``objects`` is the sim-scene list (each ``{type, tag_id, x, y, yaw}``);
    ``catalog`` is the loaded :class:`~workflow.object_catalog.ObjectCatalog`
    used to resolve each object's grasp recipe (height / grasp depth / close
    angle). An object whose type or tag id is unknown to the catalog is SKIPPED
    (logged), exactly as an unrecognised real tag would be ignored.
    """

    def __init__(self, objects: Optional[list[dict[str, Any]]], catalog: Any,
                 world: Any | None = None) -> None:
        self._catalog = catalog
        # The MUTABLE virtual scene (workflow.sim_world.SimWorld) when the node
        # supplies one: detect() then reports where an object IS, not where it was
        # placed. None keeps the frozen-snapshot behaviour verbatim, which is what
        # every pre-SimWorld construction (unit tests, the golden fixture) gets.
        self._world = world
        # Resolve the placed objects ONCE (objects are fixed for the run — the
        # sim WorkflowManager is rebuilt per start). Each placed object is keyed
        # by TYPE; the SERVER assigns its AprilTag id from the type's recipe
        # ``tag_ids`` (the front end has no source for the real ids — the
        # GetObjectCatalog service exposes only type names — so it must not
        # invent them). The k-th placed object of a type gets the k-th tag id;
        # objects beyond a type's available tag count are skipped (logged).
        self._resolved: list[dict[str, Any]] = self._resolve_objects(objects or [])

    def apriltag_available(self) -> bool:
        """Always available in sim (else ``_require_marker_detector`` raises)."""
        return True

    def detect(
        self,
        bgr: Any,
        camera: str,
        mode: str,
        aruco_id: Optional[int] = None,
    ) -> list[Detection]:
        """Return one synthetic ``Detection`` per resolved object (of the
        requested ``aruco_id`` when given). A FRESH ``Detection`` (with a fresh
        ``extras`` dict) is built per call so a downstream mutation
        (``find_object`` bakes the refined yaw onto ``extras``) never bleeds
        across calls. ``world_xyz_m`` + ``extras`` are PRE-SET so the
        all-calibration-absent ``_attach_named_world`` preserves them verbatim."""
        if mode != 'apriltag':
            return []
        # LIVE positions when a SimWorld is bound — a grasped-and-placed object is
        # reported where it now lies, not where the student originally put it.
        # Without a world this dict stays empty and every detection falls back to
        # the frozen resolve-time tuple.
        live: dict[int, dict[str, Any]] = {}
        world = self._world
        if world is not None:
            try:
                for o in world.objects():
                    live[o['key']] = o
            except Exception:  # noqa: BLE001 — perception must not die on the scene
                live = {}
        out: list[Detection] = []
        for r in self._resolved:
            if aruco_id is not None and r['aruco_id'] != aruco_id:
                continue
            cur = live.get(r['key'])
            extras = dict(r['extras'])
            if cur is not None:
                world_xyz = (cur['x'], cur['y'], r['grasp_z'])
                extras['tag_yaw'] = cur['yaw']
            else:
                world_xyz = r['world']
            out.append(Detection(
                centroid_px=(0, 0),
                bbox_px=(0, 0, 0, 0),
                confidence=1.0,
                label=r['label'],
                aruco_id=r['aruco_id'],
                world_xyz_m=world_xyz,
                corners_px=None,
                extras=extras,
            ))
        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _resolve_objects(self, objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Build the per-run resolved object list: resolve each placed object's
        TYPE via the catalog and ASSIGN its tag id from the type's ``tag_ids`` by
        per-type placement order. Skips (logged in German) objects with an
        unknown/missing type, invalid coordinates, or beyond the type's tag count
        — so every emitted detection carries a tag id the named-object handlers
        will keep (they filter on ``aruco_id ∈ recipe.tag_ids``)."""
        from physical_ai_server.workflow.object_catalog import ObjectCatalogError

        resolved: list[dict[str, Any]] = []
        used_per_type: dict[str, int] = {}
        if self._catalog is None:
            if objects:
                _logger.warning(
                    '[WARNUNG] Kein Objekt-Katalog geladen — alle Sim-Objekte '
                    'werden übersprungen.')
            return resolved
        if len(objects) > _MAX_SIM_OBJECTS:
            _logger.warning(
                '[WARNUNG] %d Sim-Objekte platziert — nur die ersten %d werden '
                'verwendet.', len(objects), _MAX_SIM_OBJECTS)
        for slot, obj in enumerate(objects[:_MAX_SIM_OBJECTS]):
            if not isinstance(obj, dict):
                continue
            type_name = obj.get('type')
            if not type_name:
                _logger.warning('[WARNUNG] Sim-Objekt ohne Typ — wird übersprungen.')
                continue
            try:
                recipe = self._catalog.recipe_for_type(type_name)
            except ObjectCatalogError:
                _logger.warning(
                    '[WARNUNG] Unbekannter Sim-Objekt-Typ „%s" — wird übersprungen.',
                    type_name)
                continue
            try:
                tag_ids = [int(t) for t in recipe.tag_ids]
            except (TypeError, ValueError):
                continue
            idx = used_per_type.get(type_name, 0)
            if idx >= len(tag_ids):
                _logger.warning(
                    '[WARNUNG] Mehr „%s"-Objekte platziert als Tags verfügbar '
                    '(höchstens %d) — überzähliges Objekt wird im Simulator '
                    'nicht erkannt.', getattr(recipe, 'label_de', type_name),
                    len(tag_ids))
                continue
            try:
                x = float(obj.get('x', 0.0))
                y = float(obj.get('y', 0.0))
                yaw = float(obj.get('yaw', 0.0))
            except (TypeError, ValueError):
                _logger.warning(
                    '[WARNUNG] Sim-Objekt „%s" hat ungültige Koordinaten — '
                    'wird übersprungen.', type_name)
                continue
            # HIGH-1: /workflow/start is an untrusted boundary and json.loads
            # accepts the literal NaN — reject non-finite coords here so a NaN
            # never reaches the IK / the published trajectory.
            if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(yaw)):
                _logger.warning(
                    '[WARNUNG] Sim-Objekt „%s" hat nicht-endliche Koordinaten — '
                    'wird übersprungen.', type_name)
                continue
            used_per_type[type_name] = idx + 1
            # Virtual table at z = 0 → grasp z is the body band below the tag-top
            # plane: object_height − grasp_depth (matches the real path's
            # z_table + object_height − grasp_depth with z_table = 0).
            grasp_z = float(recipe.object_height_m) - float(recipe.grasp_depth_m)
            resolved.append({
                # Index in the list the editor SENT — the join key with SimWorld.
                'key': slot,
                'aruco_id': tag_ids[idx],
                # 'tag<id>', not the bare type name: the node's sensor snapshot
                # parses visible_apriltag_ids out of exactly this shape, so a
                # type-named label left Debug → Sensoren showing „—" for every sim
                # run — the one surface that would have exposed a tag/position
                # mismatch to a teacher. Nothing reads `label` for behaviour;
                # `extras['object_type']` carries the type name.
                'label': f'tag{tag_ids[idx]}',
                'world': (x, y, grasp_z),
                'grasp_z': grasp_z,
                'extras': {
                    'tag_yaw': yaw,
                    'gripper_close_rad': float(recipe.gripper_close_rad),
                    'object_type': recipe.type_name,
                },
            })
            if self._world is not None:
                try:
                    self._world.bind_tag(slot, tag_ids[idx])
                except Exception:  # noqa: BLE001 — binding is best-effort telemetry
                    pass
        return resolved

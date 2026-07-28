#!/usr/bin/env python3
#
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""The MUTABLE virtual scene for the Roboter Studio simulation runtime.

Before this module the sim world was IMMUTABLE: :class:`~workflow.sim_perception.SimPerception`
froze the placed objects in ``__init__`` and :class:`~workflow.sim_arm.SimArm` only ever
replaced its list wholesale, so a grasped-and-placed cube was STILL reported at its
ORIGINAL position. The arm then drove to empty space on the next pass — the
„Geister-Würfel": measurably, a two-cube „Solange sichtbar" program re-targeted the
same vacated spot three passes running before the stall guard ended the loop.

``SimWorld`` is the single source of truth both halves now share: ``SimArm`` WRITES
(capture on a gripper close, carry while closed, release on open) and ``SimPerception``
READS. The node publishes snapshots on ``/sim/objects`` so the React twin renders THIS
state instead of its own private guess.

Two design points that are easy to get wrong:

* **A held object stays DETECTABLE**, its position tracking the gripper. Making it
  invisible instead would start ``handlers.perception_blocks``' per-tag absence clock,
  and ``_reclaim_recycled`` would then UN-CLAIM every carried object once it reappeared
  at the drop point (``EDUBOTICS_RECLAIM_ABSENT_S``, default 1.5 s, is shorter than any
  real carry) — turning „Solange sichtbar" into an infinite pick-and-place.
* **Capture is NEAREST-wins**, never any-match. The legacy proximity test in ``SimArm``
  returned True for ANY object inside the radius, which on edu6 (whose whole pick band
  is 120 mm wide against a 60 mm capture radius) cannot tell two adjacent cubes apart.

Objects are keyed by their INDEX in the list the editor sent, not by ``tag_id``: the
front end's ``tag_id`` is a local placement counter that the server deliberately ignores
(``SimPerception`` assigns the real catalog tag by per-type placement order). The
assigned tag is bound back in via :meth:`bind_tag` so the ``/sim/objects`` snapshot can
carry it for the twin.

Pure Python + stdlib — unit-testable without a container.
"""

from __future__ import annotations

import math
import threading
from typing import Any, Optional


class SimWorld:
    """Live positions of the placed virtual objects for one simulation run."""

    def __init__(self, objects: Optional[list[dict[str, Any]]] = None) -> None:
        # publish() runs on the interpreter daemon thread; the node's snapshot
        # publisher and SimPerception.detect() read from the ROS executor thread.
        self._lock = threading.RLock()
        self._objects: list[dict[str, Any]] = []
        self._held_key: Optional[int] = None
        self._epoch: int = 0
        self.reset(objects)

    # ------------------------------------------------------------------
    # Scene
    # ------------------------------------------------------------------
    def reset(self, objects: Optional[list[dict[str, Any]]] = None) -> None:
        """Install the placement for a NEW run.

        Live coordinates go back to the placed coordinates, nothing is held, and the
        epoch is bumped — the React twin keys its own visual reset off that epoch, so a
        second run never starts with a mesh still parked at the previous run's drop
        point (or still coloured as held).

        Malformed entries are dropped exactly as ``SimPerception._resolve_objects``
        drops them, so both halves agree on which placements exist. Non-finite
        coordinates are rejected here too: ``/workflow/start`` is an untrusted boundary
        and ``json.loads`` accepts the literal ``NaN``.
        """
        with self._lock:
            self._objects = []
            for i, obj in enumerate(objects or []):
                if not isinstance(obj, dict):
                    continue
                try:
                    x = float(obj.get('x', 0.0))
                    y = float(obj.get('y', 0.0))
                    yaw = float(obj.get('yaw', 0.0))
                except (TypeError, ValueError):
                    continue
                if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(yaw)):
                    continue
                self._objects.append({
                    'key': i,               # index in the list the editor sent
                    'type': obj.get('type'),
                    'tag_id': None,         # filled in by SimPerception.bind_tag
                    'x': x, 'y': y, 'yaw': yaw,
                    'placed_x': x, 'placed_y': y,
                })
            self._held_key = None
            self._epoch += 1

    def bind_tag(self, key: int, tag_id: int) -> None:
        """Record the catalog tag id ``SimPerception`` assigned to a placement slot.

        The front end's own ``tag_id`` is ignored by the runtime, so this is the only
        place the two id spaces are ever joined — and it is what lets the
        ``/sim/objects`` snapshot name an object the way the student's program does.
        """
        try:
            key = int(key)
            tag_id = int(tag_id)
        except (TypeError, ValueError):
            return
        with self._lock:
            for o in self._objects:
                if o['key'] == key:
                    o['tag_id'] = tag_id
                    return

    def objects(self) -> list[dict[str, Any]]:
        """A snapshot copy of the live objects (never the internal dicts)."""
        with self._lock:
            return [dict(o) for o in self._objects]

    def held_key(self) -> Optional[int]:
        with self._lock:
            return self._held_key

    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    # ------------------------------------------------------------------
    # Grasp
    # ------------------------------------------------------------------
    def capture_nearest(self, x: float, y: float, radius_m: float) -> Optional[int]:
        """A gripper close at ``(x, y)``: grab the NEAREST object within ``radius_m``.

        Nearest-wins rather than any-match, so two adjacent cubes are never confused.
        A no-op (returning the current holder) when something is already held — a
        second close while carrying must not swap objects.
        """
        try:
            x = float(x)
            y = float(y)
            radius_m = float(radius_m)
        except (TypeError, ValueError):
            return None
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        with self._lock:
            if self._held_key is not None:
                return self._held_key
            best: Optional[int] = None
            best_d = radius_m
            for o in self._objects:
                d = math.hypot(o['x'] - x, o['y'] - y)
                if d <= best_d:
                    best_d = d
                    best = o['key']
            self._held_key = best
            return best

    def carry_to(self, x: float, y: float) -> None:
        """Move the held object with the gripper. No-op when nothing is held."""
        try:
            x = float(x)
            y = float(y)
        except (TypeError, ValueError):
            return
        if not (math.isfinite(x) and math.isfinite(y)):
            return
        with self._lock:
            if self._held_key is None:
                return
            for o in self._objects:
                if o['key'] == self._held_key:
                    o['x'] = x
                    o['y'] = y
                    return

    def release(self) -> Optional[int]:
        """Gripper opened: the held object stays exactly where it was let go.

        Returns the released key (or ``None``) so a caller can log/publish it.
        """
        with self._lock:
            key = self._held_key
            self._held_key = None
            return key

    def is_held(self) -> bool:
        """True while an object is in the jaws — the sim's grasp-success ground truth.

        This is what makes ``SimArm``'s held report IDENTITY-based: the legacy XY
        proximity test read MISS during every carry (the frozen object was left behind
        at its placement) and HELD after every release (the arm was still near it).
        """
        with self._lock:
            return self._held_key is not None

    # ------------------------------------------------------------------
    # Wire
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """JSON-serialisable state for the ``/sim/objects`` topic."""
        with self._lock:
            return {
                'epoch': self._epoch,
                'held': self._held_key,
                'objects': [
                    {
                        'key': o['key'],
                        'type': o['type'],
                        'tag_id': o['tag_id'],
                        'x': round(o['x'], 5),
                        'y': round(o['y'], 5),
                        'yaw': round(o['yaw'], 5),
                    }
                    for o in self._objects
                ],
            }

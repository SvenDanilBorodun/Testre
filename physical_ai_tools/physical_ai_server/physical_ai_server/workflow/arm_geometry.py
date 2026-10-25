#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Whole-link geometry for the Feetech Roboter-Studio arm (v1_studio) — the
table floor and self-collision, on a model small enough to live in the server
image.

WHY THIS EXISTS. ``handlers/motion.py``'s ``WORKSPACE_FLOOR_MARGIN_M`` refusal
lives inside ``_solve_or_raise`` / ``_try_solve``, i.e. it only ever judges a
**Cartesian target**. The Grundstellung is not one: ``V1IKSolver.solve()``
only emits strict-vertical poses, and HOME points the tool 9.7° off straight UP,
so ``ik.solve(HOME_TCP)`` returns ``None``. The whole Cartesian toolchain —
reach check, workspace floor, reroute vias — is therefore structurally
inapplicable to a home move, and without this module the straight joint-space
glide ``motion.home`` publishes would be judged by nothing at all — from a limp
collapse, that glide can drive a link through the table. ``base_link`` spans
z ∈ [0.0000, 0.0521] m (its frame is re-origined to the base bottom), so z = 0
IS the table surface — a link below it is a press, not a margin.

THE MODEL: one axis-aligned box per link, expressed in that link's OWN frame.
A link-frame AABB *contains* its mesh, so every predicate below is a SOUND
bound — it can be pessimistic, never optimistic. Unlike a single per-link
radius it is ORIENTATION-AWARE, which is exactly what a floor test needs: a
direction-blind radius over the open claw would refuse essentially every
table-level grasp, i.e. it would be unusable.

Cost: 6 boxes × 6 floats = 36 numbers. No meshes ship.

OMX IS UNTOUCHED BY CONSTRUCTION. :func:`resolve_geometry` returns ``None`` for
any profile that has no box table — both OMX profiles, and every profile-less
ctx (tests, non-Roboter-Studio paths). Callers short-circuit on ``None``, so the
OMX keeps byte-identical behaviour. ``test_arm_geometry.py`` pins that.
"""

from __future__ import annotations

import itertools
import math
from typing import Callable, Optional

import numpy as np

from physical_ai_server.workflow.path_guard import (
    _MAX_SEGMENT_SAMPLES,
    _R_MAX_M,
    STEP_M,
)


# ── the box table ────────────────────────────────────────────────────────────
# (min_xyz, max_xyz) in each link's OWN frame, metres. Index i is the frame
# ``V1IKSolver.link_frames()`` returns at position i: 0 = base_link (the fixed
# root), 1..5 = link1..link5.
#
# Derived from the full-resolution meshes ``tools/build_v1_urdf_assets.py``
# generates (the shipped ``physical_ai_manager/public/v1-urdf/`` meshes are
# decimated and lie INSIDE these boxes) and rounded OUTWARD to 0.1 mm, so a
# constant here can never be SMALLER than the mesh it stands for.
#
# Index 5 (link5) is the UNION over the whole claw band [0, 1.5708] of link5 +
# end_effector + both fingers, at BOTH claw gear ratios (1 and the URDF's
# 18/19), so the box is valid at every jaw opening — the fingers ride
# ``RL_joint`` and its ``LF_joint`` <mimic> and are not on the arm's own FK
# chain. Its extent IS the open claw, which is why the box form matters here:
# an isotropic radius over the open claw would refuse essentially every
# table-level grasp.
#
# base_link (Base_part.2 + the joint-1 servo + the top cover; the adapter PCB is
# not modelled) spans z ∈ [0.0000, 0.0521] m, i.e. it is bolted to the table,
# so z = 0 IS the table surface and the model needs no calibration to judge a
# boot glide.
#
# ``test_arm_geometry_v1.py::test_boxes_contain_every_mesh_vertex`` re-derives
# containment from the shipped URDF + STLs with an INDEPENDENT parser, so a
# transcription slip here cannot verify itself. The driver carries the same
# table (``feetech_geometry.V1_LINK_BOXES``), AST-compared by the setup suite.
V1_LINK_BOXES: tuple = (
    ((-0.0508, -0.0751, +0.0000), (+0.0493, +0.0751, +0.0521)),   # 0 base_link
    ((-0.0200, -0.0153, -0.0065), (+0.0200, +0.0153, +0.0518)),   # 1 link1
    ((-0.0097, -0.0420, -0.0244), (+0.1708, +0.0150, +0.0244)),   # 2 link2
    ((-0.0150, -0.0096, -0.0244), (+0.0150, +0.2326, +0.0244)),   # 3 link3
    ((-0.0126, -0.0097, -0.0244), (+0.0351, +0.0705, +0.0244)),   # 4 link4
    ((-0.0286, -0.0966, -0.0045), (+0.0678, +0.0966, +0.1091)),   # 5 link5 + end_effector + both blades
)


# Which link pairs are worth testing for self-collision. Links one joint apart
# share that joint and their meshes interpenetrate there by construction, so
# only |i − j| ≥ 2 is meaningful. The end effector and the fingers are folded
# into the LAST index, so the gripper cluster is one rigid body and is never
# tested against itself.
#
# A FUNCTION of the table length, not a module constant: a box table of another
# length (a future arm, a test double) must never be tested with a pair set sized
# off this one — over-testing (an index error) or under-testing (a missed
# self-collision), depending on which way it was sized.
def _self_pairs(n_links: int) -> tuple:
    return tuple((i, j) for i, j in itertools.combinations(range(n_links), 2)
                 if j - i >= 2)


_SELF_PAIRS: tuple = _self_pairs(len(V1_LINK_BOXES))

# The eight corners of a unit cube, used to expand a (min, max) pair.
_UNIT_CORNERS = np.array(list(itertools.product([0.0, 1.0], repeat=3)),
                         dtype=np.float64)

# Box tables keyed on the SOLVER's own identity, not on the profile id.
#
# The boxes describe the arm's LINKS; the solver describes the same arm's
# KINEMATICS. Keying them together makes it structurally impossible to pair one
# arm's boxes with another arm's chain — a profile id is one indirection further
# away from the thing that must actually match, and it would also need new ctx
# plumbing that could silently go missing. ``IKSolver.backend`` is
# ``'closed-form'`` (OMX) and is absent from this table, which is the OMX
# short-circuit.
_BOXES_BY_BACKEND: dict = {
    'closed-form-v1': V1_LINK_BOXES,
}


def _corner_sets(boxes) -> list:
    out = []
    for lo, hi in boxes:
        lo_a = np.asarray(lo, dtype=np.float64)
        hi_a = np.asarray(hi, dtype=np.float64)
        out.append(lo_a + _UNIT_CORNERS * (hi_a - lo_a))
    return out


def _segment_samples(q_start, q_end, step_m: float = STEP_M) -> int:
    """Sample count for a swept check over the straight joint-space line.

    Deliberately the SAME rule ``path_guard.segment_blocked`` uses, imported
    from there rather than restated: a link point at radius R rotated by θ
    travels an arc R·θ, longer than the chord between its endpoints, so a
    chord-based count can let a thin obstacle hide between two samples. Sizing
    by the worst-case arc (``_R_MAX_M`` × the largest joint delta) and capping at
    ``_MAX_SEGMENT_SAMPLES`` makes the tunneling argument here identical to the
    one already argued for zones, instead of a second argument to maintain.
    """
    qs = np.asarray(q_start, dtype=np.float64)
    qe = np.asarray(q_end, dtype=np.float64)
    delta = float(np.max(np.abs(qe - qs))) if qs.size else 0.0
    n_angular = int(math.ceil(_R_MAX_M * delta / max(step_m, 1e-6)))
    return min(_MAX_SEGMENT_SAMPLES, max(1, n_angular))


class ArmGeometry:
    """Sound whole-link geometry for one arm.

    Every method returns ``None`` when the solver cannot produce frames for the
    given joints (short vector, non-finite value). ``None`` means "unknown" and
    every caller must treat it as "do not judge" — never as "safe" and never as
    "blocked".
    """

    __slots__ = ('_ik', '_corners', '_pairs')

    def __init__(self, ik, boxes=V1_LINK_BOXES) -> None:
        self._ik = ik
        self._corners = _corner_sets(boxes)
        # Sized off THIS arm's table, never the module default (see
        # :func:`_self_pairs`).
        self._pairs = _self_pairs(len(self._corners))

    @property
    def num_links(self) -> int:
        return len(self._corners)

    # ── frames → world-frame corners ────────────────────────────────────────
    def corners(self, joints) -> Optional[list]:
        """``[(8, 3) world-frame corner array]`` per link, or ``None``."""
        frames = self._ik.link_frames(joints)
        if frames is None or len(frames) < len(self._corners):
            return None
        out = []
        for local, t in zip(self._corners, frames):
            rot = np.asarray(t[:3, :3], dtype=np.float64)
            pos = np.asarray(t[:3, 3], dtype=np.float64)
            out.append(local @ rot.T + pos)
        return out

    # ── table floor ─────────────────────────────────────────────────────────
    def floor_clearance(self, joints, floor_fn: Callable) -> Optional[float]:
        """Smallest signed distance from any MOVING link's corner DOWN to the
        table. Negative = a link is below the table there.

        ``floor_fn(x, y) -> float`` gives the table height under a point, so a
        tilted table measured by the touch-off is honoured per corner rather
        than compared against one scalar (the same correctness point
        ``motion._floor_z_at`` makes for a Cartesian target).

        LINK 0 (``base_link``) IS DELIBERATELY EXCLUDED. It is the fixed mount:
        its underside IS the table (span z ∈ [0.0000, 0.0521] m, which is also
        why z = 0 is the table surface for this arm at all). Including
        it would peg every reading at ≈0 and the guard would judge nothing —
        the pose could not get better and could not get worse. It stays in the
        table for :meth:`self_clearance`, where the gripper reaching the base is
        a real event.
        """
        corners = self.corners(joints)
        if corners is None:
            return None
        worst = None
        for arr in corners[1:]:
            for point in arr:
                floor = floor_fn(float(point[0]), float(point[1]))
                gap = float(point[2]) - float(floor)
                if worst is None or gap < worst:
                    worst = gap
        return worst

    def swept_floor_clearance(self, q_start, q_end,
                              floor_fn: Callable) -> Optional[float]:
        """:meth:`floor_clearance` minimised over the straight joint-space line
        ``q_start → q_end`` — the line ``build_segment`` interpolates and the
        line ``path_guard`` resamples, so this judges exactly what is published.

        ``None`` if the geometry is unknown ANYWHERE on the line: a partial
        answer would be a silent optimism.
        """
        qs = np.asarray(list(q_start)[:self._ik.num_joints()], dtype=np.float64)
        qe = np.asarray(list(q_end)[:self._ik.num_joints()], dtype=np.float64)
        n = _segment_samples(qs, qe)
        delta = qe - qs
        worst = None
        for i in range(n + 1):
            value = self.floor_clearance(qs + delta * (i / n), floor_fn)
            if value is None:
                return None
            if worst is None or value < worst:
                worst = value
        return worst

    # ── self-collision ──────────────────────────────────────────────────────
    def self_clearance(self, joints) -> Optional[float]:
        """Smallest separation between any two NON-ADJACENT links.

        Separating-axis lower bound over the 15 candidate axes of two oriented
        boxes (3 + 3 face normals, 9 edge cross products): when the projections
        onto an axis are disjoint by ``g``, the true distance is at least ``g``,
        so the maximum over axes is sound. A non-positive result means the
        boxes overlap, which — the boxes being an over-approximation — means
        "may be touching", not "is touching".
        """
        corners = self.corners(joints)
        if corners is None:
            return None
        frames = self._ik.link_frames(joints)
        axes = [np.asarray(t[:3, :3], dtype=np.float64) for t in frames]
        worst = None
        for i, j in self._pairs:
            gap = _box_gap(corners[i], corners[j], axes[i], axes[j])
            if worst is None or gap < worst:
                worst = gap
        return worst

    def swept_self_clearance(self, q_start, q_end) -> Optional[float]:
        qs = np.asarray(list(q_start)[:self._ik.num_joints()], dtype=np.float64)
        qe = np.asarray(list(q_end)[:self._ik.num_joints()], dtype=np.float64)
        n = _segment_samples(qs, qe)
        delta = qe - qs
        worst = None
        for i in range(n + 1):
            value = self.self_clearance(qs + delta * (i / n))
            if value is None:
                return None
            if worst is None or value < worst:
                worst = value
        return worst


def _box_gap(corners_a, corners_b, rot_a, rot_b) -> float:
    """Separating-axis lower bound on the distance between two oriented boxes."""
    axes = [rot_a[:, k] for k in range(3)] + [rot_b[:, k] for k in range(3)]
    for k in range(3):
        for m in range(3):
            cross = np.cross(rot_a[:, k], rot_b[:, m])
            norm = float(np.linalg.norm(cross))
            if norm > 1e-9:
                axes.append(cross / norm)
    best = -np.inf
    for axis in axes:
        pa = corners_a @ axis
        pb = corners_b @ axis
        gap = max(float(pb.min() - pa.max()), float(pa.min() - pb.max()))
        if gap > best:
            best = gap
    return float(best)


def resolve_geometry(source) -> Optional[ArmGeometry]:
    """Build the :class:`ArmGeometry` for a ctx (or a bare solver), or ``None``.

    ``None`` — the OMX short-circuit — whenever ANY of these is true:

    * there is no IK solver on the object;
    * it does not expose ``link_frames`` (only
      :class:`~physical_ai_server.workflow.v1_ik.V1IKSolver` does; the OMX
      solver deliberately does not, and that absence is load-bearing);
    * its ``backend`` has no box table — i.e. every arm except the Feetech
      Roboter-Studio arm (v1_studio), including every test double and every
      ctx built before this module existed.

    Callers must treat ``None`` as "run the old path unchanged". Never raises:
    a solver whose ``backend`` property itself throws resolves to ``None``
    rather than taking down a home move.
    """
    ik = getattr(source, 'ik', None)
    if ik is None:
        ik = source
    if not callable(getattr(ik, 'link_frames', None)):
        return None
    try:
        backend = str(getattr(ik, 'backend', '') or '').strip()
    except Exception:  # noqa: BLE001 — identity lookup must never raise here
        return None
    boxes = _BOXES_BY_BACKEND.get(backend)
    if boxes is None:
        return None
    return ArmGeometry(ik, boxes)

#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The Roboter-Studio tower guard, its pure half (spec 40 S6.2 revision 4, in
the implementation shape revision 5 makes normative; decisions D51, D54, D65,
D72). No wiring here: WP-12 hands every leg to it.

A LEG is a straight joint-space line between two poses with the gripper's
commanded value at its ends. It is refused when at some sample, for some tower
box ``p`` and arm box ``b``::

    margin(p, b) = gap_dir(b, p, δ) − allowance(p, b) < 0

* THE TOWER is the nine oriented boxes ``scene_fixture.TOWER_OBB_F`` placed by
  ``tower_obbs_w(base_axis_x, pose)`` — the pose the arm check measured from
  fixture tags 480/481, else the contract place. Never grown: the allowance
  carries every tolerance.
* THE ARM is links 1–4 (V1: ``arm_geometry.V1_LINK_BOXES``; OMX: the generated
  raw-vertex boxes of ``arm_cells``) and the gripper end in two levels — the
  housing fixed to link 5 and each blade / finger in its own frame at the
  commanded angle (an interval at its middle angle grown by r_max · half the
  interval, the V1's mimic blade also by its gear slack); level 2 is the cells
  of each, evaluated only where level 1 is below its allowance. Placed by the
  caller's ``frames(Q) -> (N, 5, 4, 4)``, the per-link frames ``[link1 … link5]``
  in W (``CalibratedIK.guard_frames``; with nothing calibrated the solver's
  frames raised by ``LIFT_M``).
* THE DROOP turns joints 2–4 about their common (parallel) axis direction u,
  so a box moves perpendicular to u by at most
  ``δ = Σ_{j distal} θ_j · D_j / (1 − Σ θ_j)``, ``D_j`` the box's farthest corner
  from joint j's axis and ``θ_j = κ · (env_j / τ_AC,j) · G_j(q)`` — the kit's
  compliance scaled by the pose's own ABSOLUTE gravity torque sum
  ``G_j = g Σ_{l ≥ j} m_l |(u_j × (c_l − o_j))_z|``. ``gap_dir`` subtracts it per
  separating axis: ``max_n [sep_n − δ · min(1, √(1 − (n·u)²) + 0.03)]`` (0.03 ≥
  sin 1.5°, the arm check's tilt gate). ``Σ θ ≥ 0.5`` is unjudgeable.
* THE ALLOWANCE per box (one convention, rigorous)::

      R = axis + build + sample + residual [+ rotation for the top parts]
      allowance = R + c · min(z_top(p), (max(z_box, 0) + R + δ) / (1 − c))

  with ``c = TOWER_LEAN_AT_LENS_M / LENS_F.z``: a tower point that can touch the
  box after the undetected lean ``c·h`` lies at most ``R + δ`` above the box's
  top, so ``h ≤ (z_box + R + δ)/(1 − c)``.
* SAMPLING: ``n = ⌈Σ_j R_j |Δq_j| / 3 mm⌉`` with ``R_J_M`` proven upper bounds
  of any box corner's distance from joint j's axis, so a box point moves at
  most 3 mm between samples (``TOWER_SAMPLE_M`` = half of it is in R).

WHAT RUNS is the TRAVEL-BOUND RULE, in revision 5's shape: per leg (1) for
every sample at once the frames, the level-1 boxes, θ and every box's δ;
(2) per (sample, part) the per-sample broad phase; (3) per (sample, part, box)
a pre-check in the PART's frame — the gap on the part's own three face normals
(its extent taken from its corners), which never exceeds the fifteen-axis
value, so a certificate ``gap − δ − allowance ≥ 0`` is a lower bound of the
margin and keeps revision 4's verdict by construction; (4) every surviving
triple of the leg in ONE batched separating-axis evaluation; (5) level 2 only
at the samples whose level-1 gripper triple is below its allowance. The rule
judges coarse samples (``TOWER_COARSE_M`` of travel apart, no sample term)
with the pre-check — a negative value there is exact, so it refuses — then a
pair is clear when ``(m̄(a) + m̄(b) − L·(1 + c/(1 − c)))/2 ≥ 0`` per part, with
the droop bounded over the whole segment; every pair not clear on the lower
bounds has its dense samples judged in one batch, and only where one of them
is negative is revision 4's clear test re-run on EXACT coarse values: the leg
is refused only for a pair that is not clear on them.

THE BROAD PHASE skips a part at a sample when the arm's AABB lies at least the
part's largest allowance plus the sample's largest δ (and a millimetre) from
the part's AABB. An AABB separation bounds the real distance from below, so a
skipped part cannot be touched there; it enters the clear test as that
separation minus the bound — a lower bound of its PHYSICAL margin, which is what
the clear test's proof needs. (Of the fifteen-axis value it is no bound for the
turned parts — the ribs, the head, the lens, the bridge — whose separating axes
do not include the world's.)

``judge_leg`` / ``judge_polyline`` are what runs; ``dense_leg`` is the dense
rule (every 3 mm sample), the reference the travel-bound rule is held to;
``judge_pose`` and ``smallest_margin`` evaluate exactly and report the smallest
margin with its part.

Everything that cannot be judged is refused in German (``TOWER_UNJUDGEABLE_DE``,
``TOWER_DROOP_UNJUDGEABLE_DE``), never passed: a non-finite joint or gripper
value, a missing or malformed tower, arm, cell or mass table, an unknown arm, a
contract-hash mismatch, an arm check without a finite pose or envelope.

Pure: numpy, ``arm_geometry`` (its separating-axis measure), the generated
``arm_cells`` and ``scene_fixture``; no I/O, no clock, no environment;
deterministic.
"""

from __future__ import annotations

import math
from collections import namedtuple
from dataclasses import dataclass
from typing import Optional

import numpy as np

from physical_ai_server.workflow import arm_cells
from physical_ai_server.workflow import arm_geometry
from physical_ai_server.workflow import scene_fixture as sf

TOWER_TOO_CLOSE_DE = ('Diese Bewegung käme dem Kameraturm zu nahe – bitte ein Ziel weiter vorne oder weiter vom '
                      'Turm entfernt wählen.')
TOWER_UNJUDGEABLE_DE = ('Die Bewegung wird nicht ausgeführt: der Abstand zum Kameraturm kann gerade nicht geprüft '
                        'werden. Bitte die Umgebung neu starten.')
TOWER_DROOP_UNJUDGEABLE_DE = ('Dieser Arm hängt zu stark durch, um den Abstand zum Kameraturm sicher zu prüfen – die '
                              'Bewegung wird nicht ausgeführt. Bitte den Arm prüfen lassen; der Arm-Check meldet das '
                              'Durchhängen.')

# The travel-bound rule's coarse spacing (box-corner travel between coarse samples). It changes no verdict, so it is
# this module's own constant and not part of the contract (contract_hash() does not move with it).
TOWER_COARSE_M = 0.030
# The dense count: at most this much box-corner travel between samples (twice TOWER_SAMPLE_M).
DENSE_STEP_M = 2.0 * sf.TOWER_SAMPLE_M
# Dense samples judged per batch.
DENSE_CHUNK = 4096
# The undetected lean per metre of height (0.0304).
LEAN_PER_M = sf.TOWER_LEAN_AT_LENS_M / sf.LENS_F[2]
# >= sin 1.5 deg, the arm check's axis-tilt gate: a real joint axis may lean that far out of u.
TILT_SLACK = 0.03
# R_j: proven upper bounds of any arm-box corner's distance from joint j's axis (triangle inequality along the
# chain, the claw over its whole window; spec 40 S6.2 revision 4; checked by test_fixture_guard.py).
R_J_M = {'v1': (0.589, 0.590, 0.432, 0.209, 0.114), 'omx': (0.430, 0.433, 0.313, 0.151, 0.086)}
GRAVITY_M_S2 = 9.81
# Σθ at or above this at a sample: the droop bound no longer holds (unjudgeable).
DROOP_SUM_LIMIT = 0.5

# joints 2..4 turn about this axis of their own link frame (parallel on both arms)
_DROOP_AXIS_LOCAL = {'v1': (0.0, 0.0, -1.0), 'omx': (0.0, 1.0, 0.0)}
# the droop joints (indices into joints 2, 3, 4) distal to each box group; a blade is distal like the housing
_DISTAL = {'link1': (), 'link2': (0,), 'link3': (0, 1), 'link4': (0, 1, 2), 'housing': (0, 1, 2)}
_LINK_GROUPS = ('link1', 'link2', 'link3', 'link4')
_UNIT_SIGNS = np.array([[(i >> 2) & 1, (i >> 1) & 1, i & 1] for i in range(8)], float)
# the broad phase's own slack (m)
_BROAD_SLACK_M = 0.001
# samples evaluated exactly per step of smallest_margin's search
_EXACT_BATCH = 64


@dataclass(frozen=True)
class Verdict:
    """``reason``: ``'clear'``, ``'too_close'``, ``'unjudgeable'`` or ``'droop'``. ``sentence_de`` is the German
    sentence for a refusal (``TOWER_TOO_CLOSE_DE`` belongs to a student's own target; a ladder rung never shows
    it). ``margin_m`` / ``part``: the smallest margin found and its tower part, where the rule computed one."""

    refused: bool
    reason: str
    sentence_de: Optional[str] = None
    margin_m: Optional[float] = None
    part: Optional[str] = None


_CLEAR = Verdict(False, 'clear')
_UNJUDGEABLE = Verdict(True, 'unjudgeable', TOWER_UNJUDGEABLE_DE)
_DROOP = Verdict(True, 'droop', TOWER_DROOP_UNJUDGEABLE_DE)


class _Unjudgeable(Exception):
    pass


class _TooMuchDroop(Exception):
    pass


# level 1 or 2; per box the frame index 0..4 (link1..link5), its 8 corners and 3 axes (columns) in that frame,
# and its group (link1..link4, housing, a blade's name)
ArmBoxes = namedtuple('ArmBoxes', 'level link corners axes group')
# M (N, P): per sample and tower part the smallest margin over the arm's boxes (+inf where the broad phase skipped
# the part); skip (N, P): a skipped part's lower bound (NaN elsewhere); theta (N, 3); depth (N, nb, 3): D_j of each
# level-1 box; delta (N, nb); groups: the level-1 boxes' groups
Margins = namedtuple('Margins', 'M skip theta depth delta groups')


class Part:
    """one placed tower box: centre, axes (columns), half extents, its corners and what the rule reads of them."""

    __slots__ = ('name', 'centre', 'axes', 'half', 'corners', 'z_top', 'top', 'lo', 'hi', 'own_lo', 'own_hi')

    def __init__(self, name, centre, rows, half):
        self.name = name
        self.centre = np.array(centre, float)
        self.axes = np.array(rows, float)
        self.half = np.array(half, float)
        self.corners = self.centre + (_UNIT_SIGNS * 2.0 - 1.0) * self.half @ self.axes.T
        self.z_top = float(self.corners[:, 2].max())
        self.top = name in sf.TOWER_TOP_PARTS
        self.lo, self.hi = self.corners.min(0), self.corners.max(0)
        own = self.corners @ self.axes                 # its extent along its own axes, from its CORNERS
        self.own_lo, self.own_hi = own.min(0), own.max(0)


# ── the arm's boxes ──────────────────────────────────────────────────────────
def _turn(axis, angle):
    k = np.asarray(axis, float)
    k = k / np.linalg.norm(k)
    kx = np.array([[0.0, -k[2], k[1]], [k[2], 0.0, -k[0]], [-k[1], k[0], 0.0]])
    return np.eye(3) + math.sin(angle) * kx + (1.0 - math.cos(angle)) * (kx @ kx)


def _corners(lo, hi):
    lo, hi = np.asarray(lo, float), np.asarray(hi, float)
    return lo + _UNIT_SIGNS * (hi - lo)


def arm_boxes(arm, g_lo, g_hi, level=1, cells=None):
    """The arm's boxes in their link frames for a claw interval ``[g_lo, g_hi]`` (level 1: one box per link,
    housing and blade; level 2: links 1–4 and the CELLS of the housing and the blades)."""
    table = arm_cells.ARM_CELLS[arm] if cells is None else cells
    link_boxes = arm_geometry.V1_LINK_BOXES[1:5] if arm == 'v1' else table['link_boxes']
    link, corners, axes, group = [], [], [], []
    for k, (lo, hi) in enumerate(link_boxes):
        link.append(k)
        corners.append(_corners(lo, hi))
        axes.append(np.eye(3))
        group.append(_LINK_GROUPS[k])
    for lo, hi in ([table['housing']['box']] if level == 1 else table['housing']['cells']):
        link.append(4)
        corners.append(_corners(lo, hi))
        axes.append(np.eye(3))
        group.append('housing')
    mid, half = 0.5 * (g_lo + g_hi), 0.5 * (g_hi - g_lo)
    for blade in table['blades']:
        origin = np.array(blade['origin'], float)
        rot = origin[:3, :3] @ _turn(blade['axis'], blade['multiplier'] * mid)
        grow = blade['r_max_m'] * half + blade['r_max_m'] * blade['gear_slack'] * (abs(mid) + half)
        for lo, hi in ([blade['box']] if level == 1 else blade['cells']):
            link.append(4)
            corners.append(_corners(np.asarray(lo, float) - grow, np.asarray(hi, float) + grow) @ rot.T + origin[:3, 3])
            axes.append(rot)
            group.append(blade['name'])
    return ArmBoxes(level, np.array(link), np.stack(corners), np.stack(axes), tuple(group))


def world_boxes(frames, boxes):
    """``(corners (N, nb, 8, 3), axes (N, nb, 3, 3))`` in W for frames ``(N, 5, 4, 4)``."""
    placed = frames[:, boxes.link]
    rot, pos = placed[:, :, :3, :3], placed[:, :, :3, 3]
    return (np.matmul(rot, boxes.corners.transpose(0, 2, 1)).transpose(0, 1, 3, 2) + pos[:, :, None, :],
            np.matmul(rot, boxes.axes))


# ── the measure ──────────────────────────────────────────────────────────────
# (Projections are written as einsum, never ``@``: a large ``@`` reaches the multithreaded BLAS, which several
# guards in one machine's processes then fight over; einsum stays in its own thread.)
def gap_dir(corners_a, axes_a, corners_b, axes_b, delta, u):
    """The repo's separating-axis gap (``arm_geometry``'s fifteen axes) with the droop displacement of box a
    (``|e| <= delta``, ``e`` perpendicular to ``u``) subtracted per axis. Batched over the leading axis; with
    ``delta = 0`` it is ``arm_geometry._box_gap``."""
    axes, gaps, valid = arm_geometry.separating_axis_gaps(corners_a, axes_a, corners_b, axes_b)
    cu = np.abs(np.einsum('tkd,td->tk', axes, np.asarray(u, float)))
    sn = np.minimum(1.0, np.sqrt(np.maximum(0.0, 1.0 - cu * cu)) + TILT_SLACK)
    g = gaps - np.asarray(delta, float)[:, None] * sn
    return np.where(valid, g, -np.inf).max(1)


def part_frame_gap(corners, part):
    """The pre-check's gap: box corners ``(..., 8, 3)`` against a part along the part's OWN three face normals
    (three of the fifteen separating axes), the part's extent from its corners."""
    proj = np.einsum('...ci,ij->...cj', corners, part.axes)
    return _own_gap(proj.min(-2), proj.max(-2), part)


def _own_gap(lo, hi, part):
    """the gap between a box's extent ``[lo, hi]`` along a part's own axes and the part's."""
    return np.maximum(part.own_lo - hi, lo - part.own_hi).max(-1)


def height_bound(z_box, residual_term, delta):
    """the highest a tower point can stand and still touch a box whose top is ``z_box``: ``(max(z_box, 0) + R +
    δ) / (1 − c)``."""
    return (np.maximum(z_box, 0.0) + residual_term + delta) / (1.0 - LEAN_PER_M)


# ── sampling ─────────────────────────────────────────────────────────────────
def travel(arm, a, b):
    """``Σ_j R_j |Δq_j|``: a bound of any box corner's travel along the straight joint-space line a → b."""
    return float(np.sum(np.asarray(R_J_M[arm]) * np.abs(np.asarray(b, float) - np.asarray(a, float))))


def dense_count(arm, a, b, step_m=DENSE_STEP_M):
    return max(1, int(math.ceil(travel(arm, a, b) / step_m)))


def _line(a, b, n):
    a, b = np.asarray(a, float), np.asarray(b, float)
    return a[None] + (b - a)[None] * (np.arange(n + 1) / n)[:, None]


def dense_samples(arm, a, b, step_m=DENSE_STEP_M):
    return _line(a, b, dense_count(arm, a, b, step_m))


def coarse_samples(arm, a, b):
    return _line(a, b, dense_count(arm, a, b, TOWER_COARSE_M))


# ── the guard ────────────────────────────────────────────────────────────────
class TowerGuard:
    """The guard of one arm, tower placement, residual and droop envelope. Build it with :func:`make_guard`."""

    def __init__(self, arm, frames, parts, cells, masses, residual_m, envelope_rad, calibrated):
        self.arm = arm
        self.frames = frames
        self.parts = tuple(parts)
        self.cells = cells
        self.residual_m = float(residual_m)
        self.envelope_rad = tuple(float(e) for e in envelope_rad)
        self.calibrated = bool(calibrated)
        self._rj = np.asarray(R_J_M[arm], float)
        self._masses = tuple((int(f), float(m), np.asarray(c, float)) for f, m, c in masses)
        kappa = sf.TOWER_DROOP_KAPPA[arm]
        tau = np.asarray(sf.TOWER_DROOP_TAU_AC_NM[arm], float)
        self._compliance = kappa * np.asarray(self.envelope_rad) / tau       # κ · env_j / τ_AC,j
        self._distal_mass = np.array([sum(m for f, m, _c in self._masses if f >= j) for j in (2, 3, 4)])
        self._axis_local = np.asarray(_DROOP_AXIS_LOCAL[arm], float)
        fixed = sf.TOWER_AXIS_M + sf.TOWER_BUILD_M + self.residual_m
        rot = np.array([sf.TOWER_TOP_ROTATION_M if p.top else 0.0 for p in self.parts])
        self._r_no_sample = fixed + rot
        self._r_sample = fixed + sf.TOWER_SAMPLE_M + rot
        self._z_top = np.array([p.z_top for p in self.parts])
        self._part_corners = np.stack([p.corners for p in self.parts])
        self._part_axes = np.stack([p.axes for p in self.parts])
        self._part_lo = np.stack([p.lo for p in self.parts])
        self._part_hi = np.stack([p.hi for p in self.parts])
        # the parts that share their axes (the axis-aligned ones, the two ribs, the head and the lens): the arm's
        # corners are projected once per distinct set for the pre-check
        self._axes_of = {}
        for k, part in enumerate(self.parts):
            self._axes_of.setdefault(part.axes.tobytes(), (part.axes, []))[1].append(k)
        self._boxes_cache = {}

    def __repr__(self):
        return (f'TowerGuard({self.arm}, residual {self.residual_m * 1000:.1f} mm, '
                f'{"calibrated" if self.calibrated else "uncalibrated"})')

    # ── public verdicts ─────────────────────────────────────────────────────
    def judge_leg(self, q_a, q_b, g_a, g_b) -> Verdict:
        """The travel-bound rule (what runs) over the straight leg ``q_a → q_b``."""
        try:
            a, b = _joints(q_a), _joints(q_b)
            g_lo, g_hi = _claws(g_a, g_b)
            return self._travel_bound(coarse_samples(self.arm, a, b), g_lo, g_hi)
        except _Unjudgeable:
            return _UNJUDGEABLE
        except _TooMuchDroop:
            return _DROOP

    def judge_polyline(self, waypoints, gripper) -> Verdict:
        """A replay as ONE polyline: its waypoints are the coarse samples (a pair farther apart than
        ``TOWER_COARSE_M`` is split first), then the travel-bound rule."""
        try:
            W = np.atleast_2d(np.asarray(waypoints, float))
            if W.ndim != 2 or W.shape[1] != 5 or len(W) < 1 or not np.all(np.isfinite(W)):
                raise _Unjudgeable
            g_lo, g_hi = _claws(gripper, gripper)
            pts = [W[:1]]
            for a, b in zip(W[:-1], W[1:]):
                pts.append(coarse_samples(self.arm, a, b)[1:])
            return self._travel_bound(np.concatenate(pts), g_lo, g_hi)
        except (_Unjudgeable, TypeError, ValueError):
            return _UNJUDGEABLE
        except _TooMuchDroop:
            return _DROOP

    def judge_pose(self, q, gripper) -> Verdict:
        """One pose with the sample term, evaluated exactly (every part, no broad phase): refused when its smallest
        margin is negative; the margin and its part are reported either way."""
        try:
            return self._exact(_joints(q)[None], *_claws(gripper, gripper))
        except _Unjudgeable:
            return _UNJUDGEABLE
        except _TooMuchDroop:
            return _DROOP

    def dense_leg(self, q_a, q_b, g_a, g_b) -> Verdict:
        """The dense rule: every sample of the leg (at most 3 mm of box travel apart) with the sample term, in the
        same shape as the travel-bound rule's samples — the reference that rule is held to. Refused when a margin is
        negative (that value is exact: a certificate or a broad-phase bound is never negative); a clear leg reports
        no margin (:meth:`smallest_margin` computes it)."""
        try:
            a, b = _joints(q_a), _joints(q_b)
            g_lo, g_hi = _claws(g_a, g_b)
            Q = dense_samples(self.arm, a, b)
            for start in range(0, len(Q), DENSE_CHUNK):
                M = self.margins(Q[start:start + DENSE_CHUNK], g_lo, g_hi).M
                if np.any(M < 0.0):
                    return self._refusal(M)
            return _CLEAR
        except _Unjudgeable:
            return _UNJUDGEABLE
        except _TooMuchDroop:
            return _DROOP

    def smallest_margin(self, q_a, q_b, g_a, g_b) -> Verdict:
        """The leg's EXACT smallest margin over its dense samples and its tower part (diagnostics): the rule's
        fifteen-axis value of every part, no broad phase."""
        try:
            a, b = _joints(q_a), _joints(q_b)
            return self._exact(dense_samples(self.arm, a, b), *_claws(g_a, g_b))
        except _Unjudgeable:
            return _UNJUDGEABLE
        except _TooMuchDroop:
            return _DROOP

    def clear_pairs(self, W, g_lo, g_hi, exact=True):
        """The travel-bound rule's clear test over every consecutive pair of the coarse samples ``W``: on the exact
        margins, or (``exact=False``) on the pre-check's lower bounds."""
        W = np.atleast_2d(np.asarray(W, float))
        mg = self.margins(W, g_lo, g_hi, with_sample=False, precheck=not exact)
        return self._clear(mg, self._lower(mg), self._pair_travel(W))

    # ── the rule's terms ────────────────────────────────────────────────────
    def allowance(self, k, z_box, delta, with_sample=True):
        """the allowance of tower part ``k`` against a box whose top is ``z_box`` and whose droop is ``delta``."""
        r = self._r_sample[k] if with_sample else self._r_no_sample[k]
        return r + LEAN_PER_M * np.minimum(self._z_top[k], height_bound(z_box, r, delta))

    def droop_angles(self, frames):
        """``θ (N, 3)`` for joints 2..4: the kit's compliance times the pose's absolute gravity torque sum."""
        origin = frames[:, 1:4, :3, 3]
        u = np.einsum('njab,b->nja', frames[:, 1:4, :3, :3], self._axis_local)
        torque = np.zeros((len(frames), 3))
        for f, mass, com in self._masses:
            c = np.einsum('nij,j->ni', frames[:, f - 1, :3, :3], com) + frames[:, f - 1, :3, 3]
            for j in range(3):
                if f >= j + 2:
                    w = c - origin[:, j]
                    torque[:, j] += GRAVITY_M_S2 * mass * np.abs(u[:, j, 0] * w[:, 1] - u[:, j, 1] * w[:, 0])
        return self._compliance[None, :] * torque

    def droop_terms(self, frames, corners, boxes, theta):
        """``(D (N, nb, 3), δ (N, nb))``: each box's farthest corner from the axes of joints 2..4, and its droop
        displacement ``Σ_{j distal} θ_j D_j / (1 − Σ θ)``."""
        n, nb = corners.shape[:2]
        origin = frames[:, 1:4, :3, 3]                                          # (N, 3 joints, 3)
        u = np.einsum('njab,b->nja', frames[:, 1:4, :3, :3], self._axis_local)
        # |c − o_j|² − ((c − o_j)·u_j)² for every corner c, from the corners' products with o_j and u_j
        flat = corners.reshape(n, nb * 8, 3)
        dots = np.matmul(flat, np.concatenate([origin, u], 1).transpose(0, 2, 1))   # (N, nb·8, 6)
        along = dots[:, :, 3:] - np.einsum('njd,njd->nj', origin, u)[:, None, :]
        sq = ((flat * flat).sum(2)[:, :, None] - 2.0 * dots[:, :, :3] + (origin * origin).sum(2)[:, None, :]
              - along * along)
        depth = np.sqrt(np.maximum(sq.reshape(n, nb, 8, 3).max(2), 0.0))
        delta = np.zeros((n, nb))
        rest = 1.0 - theta.sum(1)
        for b, grp in enumerate(boxes.group):
            joints = _DISTAL.get(grp, _DISTAL['housing'])
            if joints:
                delta[:, b] = (depth[:, b, joints] * theta[:, joints]).sum(1) / rest
        return depth, delta

    # ── the margins ─────────────────────────────────────────────────────────
    def margins(self, Q, g_lo, g_hi, with_sample=True, precheck=True, broad=True) -> Margins:
        """Every sample of ``Q (N, 5)`` against every tower part, in revision 5's shape (``precheck=False``: every
        live triple's exact fifteen-axis value; ``broad=False``: no part skipped)."""
        Q = np.atleast_2d(np.asarray(Q, float))
        frames = self._frames(Q)
        boxes = self._boxes(g_lo, g_hi, 1)
        CA, RA = world_boxes(frames, boxes)
        theta = self.droop_angles(frames)
        if not np.all(np.isfinite(theta)):
            raise _Unjudgeable
        if np.any(theta.sum(1) >= DROOP_SUM_LIMIT):
            raise _TooMuchDroop
        depth, delta = self.droop_terms(frames, CA, boxes, theta)
        u = np.einsum('nij,j->ni', frames[:, 1, :3, :3], self._axis_local)
        n, nb = delta.shape
        n_parts = len(self.parts)
        z_box = CA[..., 2].max(2)
        lo_all, hi_all = CA.min(axis=(1, 2)), CA.max(axis=(1, 2))
        d_max = delta.max(1)
        value = np.full((n, nb, n_parts), np.inf)
        skip = np.full((n, n_parts), np.nan)
        live = np.ones((n, n_parts), bool)
        if broad:                                       # (2) the per-sample broad phase, every part at once
            sep = np.maximum(self._part_lo[None] - hi_all[:, None], lo_all[:, None] - self._part_hi[None]).max(2)
            bound = self._broad_bound(d_max)
            far = sep >= bound
            skip[far] = (sep - bound)[far]
            live &= ~far
        tri_n, tri_b, tri_p = [], [], []
        for axes, members in self._axes_of.values():    # (3) the pre-check, once per distinct set of part axes
            any_live = np.nonzero(live[:, members].any(1))[0]
            if not len(any_live):
                continue
            if precheck:
                proj = np.einsum('nbci,ij->nbcj', CA[any_live], axes)
                lo, hi = proj.min(2), proj.max(2)
                at = np.full(n, -1)
                at[any_live] = np.arange(len(any_live))
            for k in members:
                rows = np.nonzero(live[:, k])[0]
                if not len(rows):
                    continue
                if precheck:
                    al = self.allowance(k, z_box[rows], delta[rows], with_sample)
                    cert = _own_gap(lo[at[rows]], hi[at[rows]], self.parts[k]) - delta[rows] - al
                    ok = cert >= 0.0
                    value[rows[:, None], np.arange(nb)[None, :], k] = np.where(ok, cert, np.inf)
                    r, b = np.nonzero(~ok)
                    tri_n.append(rows[r])
                    tri_b.append(b)
                else:
                    tri_n.append(np.repeat(rows, nb))
                    tri_b.append(np.tile(np.arange(nb), len(rows)))
                tri_p.append(np.full(len(tri_n[-1]), k))
        if tri_n:
            tn, tb, tp = np.concatenate(tri_n), np.concatenate(tri_b), np.concatenate(tri_p)
            if len(tn):
                value[tn, tb, tp] = self._evaluate(frames, CA, RA, delta, u, tn, tb, tp, g_lo, g_hi, boxes,
                                                   with_sample)
        M = value.min(1)
        M[~live] = np.inf
        return Margins(M, skip, theta, depth, delta, boxes.group)

    def _evaluate(self, frames, CA, RA, delta, u, tn, tb, tp, g_lo, g_hi, boxes, with_sample):
        """every given (sample, box, part) triple in one batched separating-axis evaluation; level 2 for the
        gripper boxes below their allowance, placed only at the samples that need them."""
        dl = delta[tn, tb]
        m = (gap_dir(CA[tn, tb], RA[tn, tb], self._part_corners[tp], self._part_axes[tp], dl, u[tn])
             - self._allowance_at(tp, CA[tn, tb, :, 2].max(1), dl, with_sample))
        gripper = np.array([g not in _LINK_GROUPS for g in boxes.group])
        need = np.nonzero((m < 0.0) & gripper[tb])[0]
        if not len(need):
            return m
        samples = np.unique(tn[need])
        row = np.full(len(frames), -1)
        row[samples] = np.arange(len(samples))
        cells = self._boxes(g_lo, g_hi, 2)
        C2, R2 = world_boxes(frames[samples], cells)
        m = m.copy()
        for grp in sorted({boxes.group[b] for b in tb[need]}):
            sel = need[np.array([boxes.group[b] == grp for b in tb[need]])]
            ci = np.array([i for i, cg in enumerate(cells.group) if cg == grp])
            s, c = len(sel), len(ci)
            rs = row[tn[sel]]
            ca = C2[rs[:, None], ci[None, :]].reshape(s * c, 8, 3)
            ra = R2[rs[:, None], ci[None, :]].reshape(s * c, 3, 3)
            tp2 = np.repeat(tp[sel], c)
            dl2 = np.repeat(dl[sel], c)
            m2 = (gap_dir(ca, ra, self._part_corners[tp2], self._part_axes[tp2], dl2, np.repeat(u[tn[sel]], c, 0))
                  - self._allowance_at(tp2, ca[:, :, 2].max(1), dl2, with_sample))
            m[sel] = m2.reshape(s, c).min(1)
        return m

    def _broad_bound(self, d_max):
        """(N, P): per sample and part the largest allowance (at the part's top, with the sample term) plus the
        sample's largest δ and a millimetre — a part whose AABB lies at least that far from the arm's is skipped:
        neither the directional gap nor the allowance can move by more."""
        top = self._r_sample + LEAN_PER_M * self._z_top
        return top[None, :] + d_max[:, None] + _BROAD_SLACK_M

    def _allowance_at(self, parts, z_box, delta, with_sample):
        r = (self._r_sample if with_sample else self._r_no_sample)[parts]
        return r + LEAN_PER_M * np.minimum(self._z_top[parts], height_bound(z_box, r, delta))

    # ── the rules ───────────────────────────────────────────────────────────
    def _exact(self, Q, g_lo, g_hi) -> Verdict:
        """the smallest exact margin over the samples ``Q`` (every part, no broad phase): the pre-check's values
        bound each sample's margin from below, so the samples are evaluated exactly in the order of that bound
        until the next bound is no smaller than the smallest margin found."""
        lower = np.concatenate([self.margins(Q[start:start + DENSE_CHUNK], g_lo, g_hi, broad=False).M.min(1)
                                for start in range(0, len(Q), DENSE_CHUNK)])
        order = np.argsort(lower, kind='stable')
        worst, part = math.inf, None
        for start in range(0, len(order), _EXACT_BATCH):
            rows = order[start:start + _EXACT_BATCH]
            if lower[rows[0]] >= worst:
                break
            M = self.margins(Q[rows], g_lo, g_hi, precheck=False, broad=False).M
            i, k = np.unravel_index(int(np.argmin(M)), M.shape)
            if M[i, k] < worst:
                worst, part = float(M[i, k]), self.parts[k].name
        if worst < 0.0:
            return Verdict(True, 'too_close', TOWER_TOO_CLOSE_DE, worst, part)
        return Verdict(False, 'clear', None, worst, part)

    def _travel_bound(self, W, g_lo, g_hi) -> Verdict:
        coarse = self.margins(W, g_lo, g_hi, with_sample=False)
        if np.any(coarse.M < 0.0):                      # exact: a certificate is never negative
            return self._refusal(coarse.M)
        travel_ = self._pair_travel(W)
        maybe = np.nonzero(~self._clear(coarse, self._lower(coarse), travel_))[0]
        if not len(maybe):
            return _CLEAR
        chunks = [_line(W[i], W[i + 1], dense_count(self.arm, W[i], W[i + 1])) for i in maybe]
        owner = np.concatenate([np.full(len(c), i) for c, i in zip(chunks, maybe)])
        Q = np.concatenate(chunks)
        exact_ok = None
        for start in range(0, len(Q), DENSE_CHUNK):
            M = self.margins(Q[start:start + DENSE_CHUNK], g_lo, g_hi, with_sample=True).M
            negative = np.any(M < 0.0, axis=1)
            pairs = np.unique(owner[start:start + DENSE_CHUNK][negative])
            if not len(pairs):
                continue
            if exact_ok is None:
                exact = self.margins(W, g_lo, g_hi, with_sample=False, precheck=False)
                exact_ok = self._clear(exact, self._lower(exact), travel_)
            bad = pairs[~exact_ok[pairs]]
            if len(bad):
                rows = negative & np.isin(owner[start:start + DENSE_CHUNK], bad)
                return self._refusal(M[rows])
        return _CLEAR

    def _refusal(self, M) -> Verdict:
        i, k = np.unravel_index(int(np.argmin(M)), M.shape)
        return Verdict(True, 'too_close', TOWER_TOO_CLOSE_DE, float(M[i, k]), self.parts[k].name)

    @staticmethod
    def _lower(mg):
        """the margins with a broad-phase-skipped part entering as its lower bound (never +inf)."""
        M = mg.M.copy()
        skipped = ~np.isnan(mg.skip)
        M[skipped] = mg.skip[skipped]
        return M

    def _pair_travel(self, W):
        return np.sum(self._rj[None] * np.abs(np.diff(W, axis=0)), axis=1)

    def _clear(self, mg, M, travel_):
        """revision 4's clear test per consecutive pair: the droop bounded over the whole segment (θ̄ from the
        pose's torque plus the distal masses' largest lever change, D̄ from the ends plus the travel), each end
        lowered by the larger δ's cost, and ``(m̄(a) + m̄(b) − L·(1 + c/(1 − c)))/2 ≥ 0`` for every part."""
        cl = LEAN_PER_M / (1.0 - LEAN_PER_M)
        theta = mg.theta
        th_bar = (np.maximum(theta[:-1], theta[1:])
                  + (self._compliance * GRAVITY_M_S2 * self._distal_mass)[None] * 2.0 * travel_[:, None])
        st = th_bar.sum(1)
        corr = np.zeros(len(travel_))
        for b, grp in enumerate(mg.groups):
            joints = list(_DISTAL.get(grp, _DISTAL['housing']))
            if not joints:
                continue
            d_bar = np.maximum(mg.depth[:-1, b], mg.depth[1:, b]) + travel_[:, None]
            reach = (d_bar[:, joints] * th_bar[:, joints]).sum(1) / np.maximum(1.0 - st, 1e-9)
            corr = np.maximum(corr, reach - np.minimum(mg.delta[:-1, b], mg.delta[1:, b]))
        corr = np.where(st >= DROOP_SUM_LIMIT, np.inf, corr)
        lowered = M[:-1] + M[1:] - 2.0 * (corr * (1.0 + cl))[:, None] - (travel_ * (1.0 + cl))[:, None]
        return np.isfinite(corr) & np.all(lowered / 2.0 >= 0.0, axis=1)

    # ── inputs ──────────────────────────────────────────────────────────────
    def _frames(self, Q):
        try:
            frames = np.asarray(self.frames(Q), float)
        except Exception as exc:  # noqa: BLE001 — a frame source that fails cannot be judged
            raise _Unjudgeable from exc
        if frames.shape != (len(Q), 5, 4, 4) or not np.all(np.isfinite(frames)):
            raise _Unjudgeable
        return frames

    def _boxes(self, g_lo, g_hi, level):
        key = (round(float(g_lo), 12), round(float(g_hi), 12), level)
        boxes = self._boxes_cache.get(key)
        if boxes is None:
            if len(self._boxes_cache) > 256:
                self._boxes_cache.clear()
            boxes = self._boxes_cache[key] = arm_boxes(self.arm, g_lo, g_hi, level, self.cells)
        return boxes


class RefusingGuard:
    """What :func:`make_guard` returns when the guard cannot be built: every leg is refused as unjudgeable.
    ``why`` says (in English, for the log) what was missing."""

    def __init__(self, why):
        self.why = why

    def __repr__(self):
        return f'RefusingGuard({self.why})'

    def judge_leg(self, *_args, **_kwargs):
        return _UNJUDGEABLE

    def judge_polyline(self, *_args, **_kwargs):
        return _UNJUDGEABLE

    def judge_pose(self, *_args, **_kwargs):
        return _UNJUDGEABLE

    def dense_leg(self, *_args, **_kwargs):
        return _UNJUDGEABLE

    def smallest_margin(self, *_args, **_kwargs):
        return _UNJUDGEABLE


def _joints(q):
    try:
        a = np.asarray(q, float)
    except (TypeError, ValueError) as exc:
        raise _Unjudgeable from exc
    if a.shape != (5,) or not np.all(np.isfinite(a)):
        raise _Unjudgeable
    return a


def _claws(g_a, g_b):
    try:
        a, b = float(g_a), float(g_b)
    except (TypeError, ValueError) as exc:
        raise _Unjudgeable from exc
    if not (math.isfinite(a) and math.isfinite(b)):
        raise _Unjudgeable
    return min(a, b), max(a, b)


def _finite(values, n):
    try:
        out = tuple(float(v) for v in values)
    except (TypeError, ValueError):
        return None
    return out if len(out) == n and all(math.isfinite(v) for v in out) else None


def _parts(tower):
    names = [p[0] for p in tower]
    if sorted(names) != sorted(p[0] for p in sf.TOWER_OBB_F):
        return None
    out = []
    for name, centre, rows, half in tower:
        c, h = _finite(centre, 3), _finite(half, 3)
        r = np.asarray(rows, float) if np.shape(rows) == (3, 3) else None
        if c is None or h is None or r is None or not np.all(np.isfinite(r)) or min(h) <= 0.0:
            return None
        out.append(Part(name, c, r, h))
    return out


def _cells_ok(arm, cells):
    try:
        boxes = [cells['housing']['box']] + list(cells['housing']['cells'])
        if arm == 'omx':
            boxes += list(cells['link_boxes'])
            if len(cells['link_boxes']) != 4:
                return False
        if len(cells['blades']) != 2 or not cells['housing']['cells']:
            return False
        for blade in cells['blades']:
            boxes += [blade['box']] + list(blade['cells'])
            if (np.shape(blade['origin']) != (3, 4) or _finite(blade['axis'], 3) is None
                    or _finite((blade['multiplier'], blade['gear_slack'], blade['r_max_m']), 3) is None
                    or not blade['cells'] or not isinstance(blade['name'], str)):
                return False
        arr = np.asarray(boxes, float)
        return arr.shape[1:] == (2, 3) and bool(np.all(np.isfinite(arr))) and bool(np.all(arr[:, 1] >= arr[:, 0]))
    except (KeyError, TypeError, ValueError, IndexError):
        return False


def _masses_ok(masses):
    try:
        frames = sorted(int(f) for f, _m, _c in masses)
        return (frames == [1, 2, 3, 4, 5]
                and all(_finite((m,), 1) is not None and m > 0.0 and _finite(c, 3) is not None
                        for _f, m, c in masses))
    except (TypeError, ValueError):
        return False


def make_guard(profile_id, frames, *, base_axis_x, arm_check=None, contract_hash=None, tower=None, cells=None,
               link_masses=None):
    """The guard for a robot profile under ``tower_v1`` — or a :class:`RefusingGuard` when anything it needs is
    missing or malformed (never a guard that passes what it cannot judge).

    ``frames(Q (N, 5)) -> (N, 5, 4, 4)``: the per-link frames ``[link1 … link5]`` in W. ``arm_check``: ``None``
    (no valid arm check: the contract place, the UNCALIBRATED residual, the design droop envelope) or the valid arm
    check's ``{'tower_pose': (psi, tx, ty), 'droop_envelope_rad': (j2, j3, j4)}`` (the calibrated residual).
    ``contract_hash``: the contract the caller's calibration was made under; another one than this image's refuses.
    ``tower`` / ``cells`` / ``link_masses`` replace the contract's tables (tests)."""
    arm = sf.arm_key(profile_id)
    if arm is None:
        return RefusingGuard(f'unknown arm for profile {profile_id!r}')
    if contract_hash is not None and contract_hash != sf.contract_hash():
        return RefusingGuard('contract hash mismatch')
    bax = _finite((base_axis_x,), 1)
    if bax is None:
        return RefusingGuard('base_axis_x is not finite')
    spec = sf.ARMS[arm]
    if arm_check is None:
        pose, envelope = None, sf.TOWER_DROOP_DESIGN_ENV_RAD[arm]
        residual, calibrated = sf.TOWER_RESIDUAL_UNCALIBRATED_M[arm], False
    else:
        try:
            pose = _finite(arm_check.get('tower_pose'), 3)
            envelope = _finite(arm_check.get('droop_envelope_rad'), 3)
        except AttributeError:
            pose = envelope = None
        if pose is None or envelope is None or min(envelope) < 0.0:
            return RefusingGuard('the arm check has no finite tower pose or droop envelope')
        residual, calibrated = sf.TOWER_RESIDUAL_SERVER_M[arm], True
    try:
        placed = sf.tower_obbs_w(bax[0], pose) if tower is None else tower
        parts = _parts(placed)
    except (TypeError, ValueError):
        parts = None
    if parts is None:
        return RefusingGuard('missing or malformed tower table')
    cells = arm_cells.ARM_CELLS.get(arm) if cells is None else cells
    if not cells or not _cells_ok(arm, cells):
        return RefusingGuard('missing or malformed arm cell table')
    masses = spec.get('link_masses') if link_masses is None else link_masses
    if not masses or not _masses_ok(masses):
        return RefusingGuard('missing or malformed link-mass table')
    if not callable(frames):
        return RefusingGuard('no frame source')
    return TowerGuard(arm, frames, parts, cells, masses, residual, envelope, calibrated)

#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""Roboter Studio's arm model (spec 40 S6.1): the profile's solver seen in the
tower kit's world W.

:class:`CalibratedIK` wraps the profile's closed-form solver (``IKSolver`` /
``V1IKSolver``) as ONE rigid model: the serial chain of ``autocal.chain`` at
the encoder angles plus the arm check's joint offsets ``d``, with its five
joint-axis tilts, in the solver's frame A; then one transform into W::

    p_W = P0 + R_t · (p_A + (0, 0, LIFT_M) − P0),   P0 = (base_axis_x, 0, MAT_THICKNESS_M)

``R_t`` is the smallest rotation turning the mat's normal ``(−ta, −tb, 1)``
(the arm check's two slopes) onto +z: in W the mat is flat at
``MAT_THICKNESS_M`` above the table at z = 0, and the arm base stands
``LIFT_M`` above the table on the plate. A rigid transform, never a shear: the
Position floor check accepts a pose only when the last link point is ``fk``
plus the fingertip along the tool axis (spec 40 §2, correction 19).

* ``fk``, ``link_points`` (the solver's own sampling; on the OMX the two tool
  points at half and all of ``tool_tip_below_ee_m``) and ``link_frames``
  (present only when the wrapped solver has it: its absence on the OMX keeps
  ``arm_geometry.resolve_geometry`` at ``None``);
* ``guard_frames``: the per-link frames ``[link1 … link5]`` of either arm, one
  joint vector or an ``(N, 5)`` batch — the tower guard's frames;
* ``solve``: the base solve of the target taken back into A, minus ``d``, then
  at most :data:`REFINE_PASSES` fixed-point passes (aim ← aim − (fk(q) − target))
  until the miss is below :data:`REFINE_STOP_M`. The best pose is returned and
  its miss is :attr:`last_refine_miss_m` (per thread); above
  :data:`IK_REFINE_ACCEPT_M` the caller warns (:func:`refinement_warning`,
  decisions D33, D42: a warning, never a refusal). ``None`` only when the first
  solve fails or a joint leaves :attr:`joint_limits`; a later pass that does
  not solve ends the passes;
* ``joint_limits``: the solver's, joint 1 cut to the contract's
  ``joint1_window_deg``;
* ``tool_tip_below_ee_m``: the contract's ``FINGERTIP_BELOW_EE_M`` on the real
  robot, the solver's own in the simulator (non-goal N12).

Everything else is the wrapped solver's (``backend``, ``num_joints``,
``base_axis_x``, ``approach_axis_local``, ``base_yaw``, ``roll_from_joints``,
``tool_tip_rise_m``, …): the arm check never estimates joint 1's or joint 5's
offset (both fixed at 0), so the azimuth and the roll pass straight through. A
private name is never forwarded: it would answer in A.

The parameters come from ``autocal.store`` (``arm_check.json``) and are read
again when ``store.version()`` moved; anything but a valid arm check of this
profile is zero offsets, tilts and slopes. With nothing calibrated the model is
the base solver raised by ``LIFT_M`` (1e-9, ``test_arm_model.py``).

Pure NumPy and the standard library.
"""

from __future__ import annotations

import math
import threading
from typing import NamedTuple, Optional

import numpy as np

from physical_ai_server.workflow import scene_fixture as _sf
from physical_ai_server.workflow.autocal import chain as _chain
from physical_ai_server.workflow.autocal import store as _store
from physical_ai_server.workflow.autocal import texts_de as _texts

# Above this model miss after refinement the caller warns once per target per
# run (D42; S4's sentence ``refinement_warning_de``).
IK_REFINE_ACCEPT_M = 0.0005
# The refinement stops once the model misses by less than this.
REFINE_STOP_M = 5e-5
# Fixed-point passes after the first solve.
REFINE_PASSES = 3

# A joint within this of its limit is inside (the solvers' own tolerance).
_LIMIT_TOL_RAD = 1e-6
# The solvers' limits are written to four decimals (the V1's ±1.5708 is ±90°):
# a window edge within half that last decimal is the same edge, not a cut.
_WINDOW_SAME_EDGE_RAD = 5e-5

_KIND_BY_BACKEND = {'closed-form': 'omx', 'closed-form-v1': 'v1'}


def solver_kind(solver) -> Optional[str]:
    """``'omx'`` / ``'v1'`` for a solver the arm model can wrap, else ``None``."""
    try:
        backend = str(getattr(solver, 'backend', '') or '').strip()
    except Exception:  # noqa: BLE001 — an identity lookup never raises here
        return None
    return _KIND_BY_BACKEND.get(backend)


class _Params(NamedTuple):
    offsets: np.ndarray            # (5,) joint offsets d
    tilts: Optional[np.ndarray]    # (6,) axis tilts, None when all zero
    rot: np.ndarray                # R_t of the two slopes


def _params(offsets=None, tilts=None, slopes=None) -> _Params:
    d = np.zeros(5) if offsets is None else np.asarray(offsets, dtype=np.float64).reshape(5)
    t = None if tilts is None else np.asarray(tilts, dtype=np.float64).reshape(6)
    ta, tb = (0.0, 0.0) if slopes is None else (float(slopes[0]), float(slopes[1]))
    if not (np.all(np.isfinite(d)) and (t is None or np.all(np.isfinite(t)))
            and math.isfinite(ta) and math.isfinite(tb)):
        raise ValueError('arm model parameters must be finite')
    if t is not None and not np.any(t):
        t = None
    return _Params(d, t, _chain.tilt_rotation(ta, tb))


_NOTHING_CALIBRATED = _params()


class CalibratedIK:
    """The profile's solver in W (see the module docstring).

    ``store`` + ``profile_id``: the parameters of that profile's arm check,
    re-read when ``store.version()`` moved. Without a store the given
    ``offsets`` (5), ``axis_tilts`` (6, ``chain.TILT_SLOTS``) and ``slopes``
    (``ta, tb``), default zero. ``fingertip_m`` ``None`` keeps the solver's
    ``tool_tip_below_ee_m``; ``joint1_window_deg`` ``None`` keeps its joint-1
    limits."""

    def __init__(self, base, *, store=None, profile_id=None, offsets=None, axis_tilts=None, slopes=None,
                 fingertip_m=None, joint1_window_deg=None) -> None:
        kind = solver_kind(base)
        if kind is None:
            raise ValueError('the arm model wraps the OMX or the V1 solver only')
        if store is not None and not profile_id:
            raise ValueError('reading an arm check needs the profile id')
        self._base = base
        self._kind = kind
        self._store = store
        self._profile_id = profile_id
        self._static = _params(offsets, axis_tilts, slopes)
        self._current = self._static
        self._loaded_version = None
        self._lock = threading.Lock()
        self._local = threading.local()
        self._tip = float(base.tool_tip_below_ee_m) if fingertip_m is None else float(fingertip_m)
        self._window = None if joint1_window_deg is None else tuple(math.radians(v) for v in joint1_window_deg)
        self._p0 = np.array([float(base.base_axis_x), 0.0, _sf.MAT_THICKNESS_M])
        self._lift = np.array([0.0, 0.0, _sf.LIFT_M])
        if callable(getattr(base, 'link_frames', None)):
            self.link_frames = self._link_frames

    def __getattr__(self, name):
        # Reached only for names the model does not define: the solver's own.
        base = self.__dict__.get('_base')
        if base is None or name.startswith('_'):
            raise AttributeError(name)
        return getattr(base, name)

    # ── parameters ───────────────────────────────────────────────────────────
    def _now(self) -> _Params:
        if self._store is None:
            return self._static
        version = _store.version()
        if version != self._loaded_version:
            with self._lock:
                if version != self._loaded_version:
                    self._current = self._read_arm_check()
                    self._loaded_version = version
        return self._current

    def _read_arm_check(self) -> _Params:
        try:
            got = self._store.read(self._profile_id)
            if got.status != 'ok':
                return _NOTHING_CALIBRATED
            data = got.data
            return _params(data['joint_offsets_rad'], data['axis_tilts_rad'], data['tilt'])
        except (OSError, KeyError, TypeError, ValueError):
            return _NOTHING_CALIBRATED

    # ── the transform A → W ──────────────────────────────────────────────────
    def _to_world(self, points, p: _Params) -> np.ndarray:
        """Points ``(…, 3)`` of the solver's frame in W."""
        return self._p0 + (np.asarray(points, dtype=np.float64) + self._lift - self._p0) @ p.rot.T

    def _to_solver(self, point, p: _Params) -> np.ndarray:
        return (np.asarray(point, dtype=np.float64) - self._p0) @ p.rot + self._p0 - self._lift

    def _frames_to_world(self, frames, p: _Params) -> np.ndarray:
        out = np.array(frames, dtype=np.float64)
        out[..., :3, :3] = p.rot @ out[..., :3, :3]
        out[..., :3, 3] = self._to_world(out[..., :3, 3], p)
        return out

    @staticmethod
    def _arm(joints) -> Optional[np.ndarray]:
        q = list(joints)
        if len(q) < 5:
            return None
        return np.asarray(q[:5], dtype=np.float64)

    # ── the model ────────────────────────────────────────────────────────────
    @property
    def tool_tip_below_ee_m(self) -> float:
        return self._tip

    @property
    def joint_limits(self) -> tuple[tuple[float, float], ...]:
        limits = [(float(lo), float(hi)) for lo, hi in self._base.joint_limits]
        if self._window is not None:
            lo, hi = limits[0]
            w_lo, w_hi = self._window
            limits[0] = (w_lo if w_lo > lo + _WINDOW_SAME_EDGE_RAD else lo,
                         w_hi if w_hi < hi - _WINDOW_SAME_EDGE_RAD else hi)
        return tuple(limits)

    @property
    def last_refine_miss_m(self) -> Optional[float]:
        """How far this thread's last successful :meth:`solve` pose misses its
        target in the model (metres); ``None`` before one, or after a solve that
        answered ``None``."""
        return getattr(self._local, 'miss', None)

    def fk(self, joints) -> Optional[tuple[np.ndarray, np.ndarray]]:
        """``(R 3×3, t 3,)`` of the solver's end effector in W, or ``None`` for
        fewer than 5 joints (extra entries are ignored, as by the solvers)."""
        q = self._arm(joints)
        if q is None:
            return None
        p = self._now()
        E = _chain.ee(self._kind, q + p.offsets, p.tilts)
        return p.rot @ E[:3, :3], self._to_world(E[:3, 3], p)

    def link_points(self, joints, samples_per_link: int = 5) -> Optional[list[np.ndarray]]:
        q = self._arm(joints)
        if q is None:
            return None
        p = self._now()
        tip = self._tip if self._kind == 'omx' else None
        pts = _chain.link_points_batch(self._kind, q + p.offsets, p.tilts, samples_per_link, tip)[0]
        return list(self._to_world(pts, p))

    def _link_frames(self, joints) -> Optional[list[np.ndarray]]:
        """The V1's ``[base_link, link1 … link5]`` in W (bound as ``link_frames``)."""
        q = self._arm(joints)
        if q is None:
            return None
        p = self._now()
        return list(self._frames_to_world(_chain.link_frames(q + p.offsets, p.tilts), p))

    def guard_frames(self, joints):
        """The per-link frames ``[link1 … link5]`` in W: ``(5, 4, 4)`` for one
        joint vector (``None`` below 5 joints), ``(N, 5, 4, 4)`` for an ``(N, ≥5)``
        batch. Not ``link_frames``: ``arm_geometry.resolve_geometry`` never reads it."""
        Q = np.asarray(joints, dtype=np.float64)
        if Q.ndim == 1 and Q.shape[0] < 5:
            return None
        p = self._now()
        frames = self._frames_to_world(
            _chain.frames_batch(self._kind, np.atleast_2d(Q)[:, :5] + p.offsets, p.tilts), p)
        return frames[0] if Q.ndim == 1 else frames

    def _inside_limits(self, q) -> bool:
        return all(lo - _LIMIT_TOL_RAD <= v <= hi + _LIMIT_TOL_RAD for v, (lo, hi) in zip(q, self.joint_limits))

    def solve(self, target_xyz, target_rpy=(math.pi, 0.0, 0.0), seed=None, free_yaw=True,
              roll=None) -> Optional[list[float]]:
        """The encoder joints that put the end effector on ``target_xyz`` (W),
        strict-vertical like the wrapped solver, or ``None`` (see the module
        docstring). ``seed`` and ``free_yaw`` are handed through; neither solver
        uses them."""
        self._local.miss = None
        try:
            target = np.asarray(target_xyz, dtype=np.float64).reshape(3)
        except (TypeError, ValueError):
            return None
        if not np.all(np.isfinite(target)):
            return None
        p = self._now()
        aim = target.copy()
        best, best_miss = None, math.inf
        for _ in range(1 + REFINE_PASSES):
            got = self._base.solve(tuple(float(v) for v in self._to_solver(aim, p)), target_rpy=target_rpy,
                                   seed=seed, free_yaw=free_yaw, roll=roll)
            if got is None:
                break
            q = np.asarray(list(got)[:5], dtype=np.float64) - p.offsets
            if not self._inside_limits(q):
                break
            E = _chain.ee(self._kind, q + p.offsets, p.tilts)
            error = self._to_world(E[:3, 3], p) - target
            miss = float(np.linalg.norm(error))
            if not math.isfinite(miss):
                break
            if miss < best_miss:
                best, best_miss = q, miss
            if miss < REFINE_STOP_M:
                break
            aim = aim - error
        if best is None:
            return None
        self._local.miss = best_miss
        return [float(v) for v in best]

    def solve_quat(self, target_xyz, target_quat, seed=None, free_yaw=False) -> Optional[list[float]]:
        """Position-only, like the wrapped solver's (the orientation is ignored)."""
        return self.solve(target_xyz, seed=seed, free_yaw=free_yaw)

    def in_workspace(self, target_xyz) -> bool:
        return self.solve(target_xyz) is not None


def refinement_warning(ik, label: str) -> Optional[str]:
    """The German ``[WARNUNG]`` line (S4) for the calling thread's last
    ``ik.solve`` when its pose misses the target by more than
    :data:`IK_REFINE_ACCEPT_M` in the model, else ``None`` (also for a solver
    that is not an arm model). ``label`` names the target in the sentence."""
    miss = getattr(ik, 'last_refine_miss_m', None)
    if isinstance(miss, bool) or not isinstance(miss, (int, float)):
        return None
    if not (math.isfinite(miss) and miss > IK_REFINE_ACCEPT_M):
        return None
    return '[WARNUNG] ' + _texts.refinement_warning_de(label, miss * 1000.0)


def for_robot(base, profile_id: str, calib_dir: Optional[str]):
    """The REAL robot's arm model: the arm check of ``profile_id`` in
    ``calib_dir`` (``None``: no store, nothing calibrated), the contract's
    fingertip offset and joint-1 window. A solver the model cannot wrap is
    returned as it is."""
    kind = solver_kind(base)
    if kind is None:
        return base
    return CalibratedIK(base, store=None if calib_dir is None else _store.Store(calib_dir),
                        profile_id=profile_id, fingertip_m=_sf.FINGERTIP_BELOW_EE_M[kind],
                        joint1_window_deg=_sf.ARMS[kind]['joint1_window_deg'])


def for_simulator(base):
    """The simulator's arm model: the lift and the joint-1 window only — no arm
    check, no mat tilt, the solver's own fingertip constant (non-goal N12)."""
    kind = solver_kind(base)
    if kind is None:
        return base
    return CalibratedIK(base, joint1_window_deg=_sf.ARMS[kind]['joint1_window_deg'])

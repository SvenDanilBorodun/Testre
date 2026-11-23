#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The two arms' serial chains with optional joint-axis tilts (spec 40 S4, A10).

Built from the solvers' own constants (``ik_solver._J*_XYZ`` / ``_EE_XYZ``;
``v1_ik._FIXED`` / ``_AXES`` / ``_L_TOOL`` / ``_RZ_PI`` / ``_J1_XYZ``), in the
SOLVER's frame (no lift; the arm model adds it). At zero tilts every function
here equals the solver's own ``fk`` / ``link_points`` / ``link_frames``
(``test_autocal_chain.py``, 1e-9).

**A tilt** of joint i (i = 2, 3, 4) is a rotation by the vector ``a·u + b·v``
applied before the joint's own rotation, ``u, v`` the two axes perpendicular to
the joint axis (OMX: x and z; V1: x and y). Six slots
``[j2a, j2b, j3a, j3b, j4a, j4b]``; ``j2b`` is a joint-1 zero offset that no
arm check can tell from the mat's yaw, so the arm check keeps it at 0.

The chain functions take ``kind`` (``'omx'`` / ``'v1'``) first (``link_frames``
is the V1's alone) and have batched twins (``…_batch``) over an ``(N, 5)``
array of joint vectors; the arm check's solve evaluates its poses through
those. ``geometry`` is the link geometry, :data:`NOMINAL` unless a simulation
hands a perturbed one. Also here: the rotation helpers and ``tilt_rotation``,
the turn that carries the arm's frame into W over a sloped mat (S6.1).

Pure NumPy and the standard library (and the two solvers' constants).
"""

from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np

from physical_ai_server.workflow import ik_solver as _omx
from physical_ai_server.workflow import v1_ik as _v1

KINDS = ('omx', 'v1')

# Each solver's x of the joint-1 axis, read through its public property.
BASE_AXIS_X = {'omx': float(_omx.IKSolver().base_axis_x), 'v1': float(_v1.V1IKSolver().base_axis_x)}

# The six tilt slots.
TILT_SLOTS = ('j2a', 'j2b', 'j3a', 'j3b', 'j4a', 'j4b')

_I3 = np.eye(3)
_RZ_PI = np.asarray(_v1._RZ_PI, dtype=np.float64)


# ── rotations ────────────────────────────────────────────────────────────────
def rodrigues(rvec) -> np.ndarray:
    """The rotation matrix of a rotation vector (axis × angle)."""
    r = np.asarray(rvec, dtype=np.float64).reshape(3)
    th = float(np.linalg.norm(r))
    if th < 1e-15:
        return _I3.copy()
    k = r / th
    K = np.array([[0.0, -k[2], k[1]], [k[2], 0.0, -k[0]], [-k[1], k[0], 0.0]])
    return _I3 + math.sin(th) * K + (1.0 - math.cos(th)) * (K @ K)


def rvec_of(R) -> np.ndarray:
    """The rotation vector of a rotation matrix (inverse of :func:`rodrigues`)."""
    R = np.asarray(R, dtype=np.float64)
    c = max(-1.0, min(1.0, (float(np.trace(R)) - 1.0) / 2.0))
    th = math.acos(c)
    w = np.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]])
    if th < 1e-12:
        return 0.5 * w
    if math.pi - th < 1e-6:
        # Near a half turn: the axis from the symmetric part.
        B = (R + _I3) / 2.0
        i = int(np.argmax(np.diag(B)))
        axis = B[:, i] / math.sqrt(max(B[i, i], 1e-300))
        return th * axis / np.linalg.norm(axis)
    return th / (2.0 * math.sin(th)) * w


def tilt_rotation(ta: float, tb: float) -> np.ndarray:
    """``R_t``: the smallest rotation turning the mat normal ``(−ta, −tb, 1)``
    onto +z — what carries the arm's frame (and everything rigid to its base:
    the tower, its fixture tags, its lens) into Roboter Studio's world, where
    the mat is flat (S6.1)."""
    n = np.array([-float(ta), -float(tb), 1.0])
    n /= np.linalg.norm(n)
    axis = np.array([n[1], -n[0], 0.0])         # n × z
    s = float(np.linalg.norm(axis))
    if s < 1e-15:
        return _I3.copy()
    return rodrigues(axis / s * math.atan2(s, float(n[2])))


def _axis_rot_batch(axis, q) -> np.ndarray:
    """``(N, 3, 3)`` rotations by angles ``q`` about one unit ``axis``."""
    a = np.asarray(axis, dtype=np.float64)
    a = a / np.linalg.norm(a)
    K = np.array([[0.0, -a[2], a[1]], [a[2], 0.0, -a[0]], [-a[1], a[0], 0.0]])
    K2 = K @ K
    s = np.sin(q)[:, None, None]
    c = np.cos(q)[:, None, None]
    return _I3 + s * K + (1.0 - c) * K2


def _tilt_rot(i: int, tilts, kind: str):
    """The tilt rotation of joint index ``i`` (1, 2, 3 = joints 2, 3, 4), or ``None``."""
    if tilts is None or i not in (1, 2, 3):
        return None
    a, b = float(tilts[2 * (i - 1)]), float(tilts[2 * (i - 1) + 1])
    if a == 0.0 and b == 0.0:
        return None
    w = (a, 0.0, b) if kind == 'omx' else (a, b, 0.0)
    return rodrigues(w)


def _check_kind(kind):
    if kind not in KINDS:
        raise ValueError(f'unknown arm kind {kind!r}')


# ── link geometry ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Geometry:
    """The constant part of a chain.

    OMX: ``origins`` = the six joint/EE origins of ``ik_solver``.
    V1: ``fixed`` = the five fixed per-joint transforms of ``v1_ik._FIXED``,
    ``l_tool`` = the TCP's distance along the tool axis, ``tool_lat`` = its
    sideways offset in the link-5 frame (0 on the drawing)."""

    kind: str
    origins: tuple = ()
    fixed: tuple = ()
    l_tool: float = 0.0
    tool_lat: tuple = (0.0, 0.0)


NOMINAL = {
    'omx': Geometry(kind='omx', origins=tuple(
        tuple(float(v) for v in o) for o in (
            _omx._J1_XYZ, _omx._J2_XYZ, _omx._J3_XYZ, _omx._J4_XYZ, _omx._J5_XYZ, _omx._EE_XYZ))),
    'v1': Geometry(kind='v1', fixed=tuple(np.array(F, dtype=np.float64) for F in _v1._FIXED),
                   l_tool=float(_v1._L_TOOL)),
}

_OMX_AXES = ('z', 'y', 'y', 'y', 'x')
_UNIT = {'x': (1.0, 0.0, 0.0), 'y': (0.0, 1.0, 0.0), 'z': (0.0, 0.0, 1.0)}

# The OMX's tool points of ``link_points`` sit this far past the EE by default
# (the solver's own ``_TOOL_TIP_EXT_M``).
OMX_TIP_DEFAULT_M = float(_omx._TOOL_TIP_EXT_M)


def _as_batch(q) -> np.ndarray:
    Q = np.asarray(q, dtype=np.float64)
    if Q.ndim == 1:
        Q = Q[None, :]
    if Q.shape[-1] < 5:
        raise ValueError('a joint vector needs five arm joints')
    return Q[:, :5]


def _frames_raw(kind, Q, tilts, geometry):
    """``(N, 6, 4, 4)``: OMX ``T1 … T5, TEE``; V1 the five URDF link frames and
    the TCP frame (URDF frame, before ``_RZ_PI``)."""
    _check_kind(kind)
    geo = NOMINAL[kind] if geometry is None else geometry
    n = Q.shape[0]
    out = np.zeros((n, 6, 4, 4))
    M = np.broadcast_to(np.eye(4), (n, 4, 4)).copy()
    if kind == 'omx':
        for i in range(5):
            R = _axis_rot_batch(_UNIT[_OMX_AXES[i]], Q[:, i])
            Rt = _tilt_rot(i, tilts, kind)
            if Rt is not None:
                R = Rt @ R
            step = np.zeros((n, 4, 4))
            step[:, :3, :3] = R
            step[:, :3, 3] = geo.origins[i]
            step[:, 3, 3] = 1.0
            M = M @ step
            out[:, i] = M
        tee = np.eye(4)
        tee[:3, 3] = geo.origins[5]
        out[:, 5] = M @ tee
        return out
    for i in range(5):
        R = _axis_rot_batch(_v1._AXES[i], Q[:, i])
        Rt = _tilt_rot(i, tilts, kind)
        if Rt is not None:
            R = Rt @ R
        step = np.zeros((n, 4, 4))
        step[:, :3, :3] = R
        step[:, 3, 3] = 1.0
        M = M @ geo.fixed[i] @ step
        out[:, i] = M
    tcp = M.copy()
    tcp[:, :3, 3] = M[:, :3, 3] + M[:, :3, :3] @ np.array(
        [geo.tool_lat[0], geo.tool_lat[1], geo.l_tool], dtype=np.float64)
    out[:, 5] = tcp
    return out


def _to_world_v1(T):
    """URDF frames ``(..., 4, 4)`` into the V1 solver's world (rotated π about z)."""
    W = T.copy()
    W[..., :3, :3] = _RZ_PI @ T[..., :3, :3]
    W[..., :3, 3] = T[..., :3, 3] @ _RZ_PI.T
    return W


# ── the public chain ─────────────────────────────────────────────────────────
def frames_batch(kind, Q, tilts=None, geometry=None) -> np.ndarray:
    """``(N, 5, 4, 4)``: the link frames ``[link1 … link5]`` in the solver's frame."""
    raw = _frames_raw(kind, _as_batch(Q), tilts, geometry)[:, :5]
    return _to_world_v1(raw) if kind == 'v1' else raw


def frames(kind, q, tilts=None, geometry=None) -> list:
    """The link frames ``[link1 … link5]`` of one joint vector (solver's frame)."""
    return list(frames_batch(kind, q, tilts, geometry)[0])


def ee_batch(kind, Q, tilts=None, geometry=None) -> np.ndarray:
    """``(N, 4, 4)``: the end effector the solver's ``fk`` answers (OMX
    ``end_effector_link``, V1 the closed fingertip), in the solver's frame."""
    raw = _frames_raw(kind, _as_batch(Q), tilts, geometry)[:, 5]
    return _to_world_v1(raw) if kind == 'v1' else raw


def ee(kind, q, tilts=None, geometry=None) -> np.ndarray:
    """The 4×4 end effector of one joint vector (equals the solver's ``fk``)."""
    return ee_batch(kind, q, tilts, geometry)[0]


def link_frames(q, tilts=None, geometry=None) -> list:
    """The V1's ``[base_link, link1 … link5]`` world frames (equals
    ``V1IKSolver.link_frames``). The OMX has none, on purpose: its absence keeps
    ``arm_geometry.resolve_geometry`` at ``None``."""
    base = np.eye(4)
    base[:3, :3] = _RZ_PI
    return [base] + frames('v1', q, tilts, geometry)


def link_points_batch(kind, Q, tilts=None, n: int = 5, tip=None, geometry=None) -> np.ndarray:
    """``(N, P, 3)``: the solver's own sampling — the base origin, then ``n``
    points per link along base → joint origins → EE; on the OMX two tool
    points at half and all of ``tip`` along the tool x axis (default the
    solver's ``_TOOL_TIP_EXT_M``). The V1's TCP is the fingertip; ``tip`` must
    be ``None`` or 0 there."""
    _check_kind(kind)
    Qb = _as_batch(Q)
    raw = _frames_raw(kind, Qb, tilts, geometry)
    N = Qb.shape[0]
    zero = np.zeros((N, 3))
    if kind == 'omx':
        origins = [zero] + [raw[:, i, :3, 3] for i in range(6)]
    else:
        if tip not in (None, 0, 0.0):
            raise ValueError('the V1 TCP is the fingertip: no tool points past it')
        geo = NOMINAL['v1'] if geometry is None else geometry
        j1 = np.broadcast_to(np.asarray(geo.fixed[0][:3, 3], dtype=np.float64), (N, 3))
        origins = [zero, j1] + [raw[:, i, :3, 3] for i in (1, 2, 3, 4)] + [raw[:, 5, :3, 3]]
        origins = [o @ _RZ_PI.T for o in origins]
    k = max(1, int(n))
    pts = [origins[0]]
    for a, b in zip(origins[:-1], origins[1:]):
        for j in range(1, k + 1):
            pts.append(a + (b - a) * (j / k))
    if kind == 'omx':
        t = OMX_TIP_DEFAULT_M if tip is None else float(tip)
        tee = raw[:, 5]
        axis = tee[:, :3, 0]
        axis = axis / np.linalg.norm(axis, axis=1, keepdims=True)
        pts.append(tee[:, :3, 3] + axis * (t * 0.5))
        pts.append(tee[:, :3, 3] + axis * t)
    return np.stack(pts, axis=1)


def link_points(kind, q, tilts=None, n: int = 5, tip=None, geometry=None) -> list:
    """The solver's ``link_points`` of one joint vector (see :func:`link_points_batch`)."""
    return list(link_points_batch(kind, q, tilts, n, tip, geometry)[0])


def tcp_batch(kind, Q, tilts=None, tip=None, geometry=None) -> np.ndarray:
    """``(N, 3)``: the fingertip — the EE plus ``tip`` along the tool axis (OMX
    x, default its ``_TOOL_TIP_EXT_M``); the V1's EE is already the fingertip."""
    E = ee_batch(kind, Q, tilts, geometry)
    if kind == 'v1':
        return E[:, :3, 3].copy()
    t = OMX_TIP_DEFAULT_M if tip is None else float(tip)
    return E[:, :3, 3] + E[:, :3, 0] * t

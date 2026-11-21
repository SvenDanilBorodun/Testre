#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The handheld lens calibration (spec 40 S4, decision D9's fallback).

When the arm check cannot measure the lens (its lens or RMS gate failed), the
student moves the clip-on target by hand in front of the tower camera.

* THE CAPTURE (:class:`Capture`): a view is captured when at least
  :data:`MIN_TARGET_TAGS` of this arm's target tags are seen still — the arm
  check's own rule, ``looks`` consecutive looks whose tag centres moved less
  than ``stable_px`` (``arm_check.still_between``; the service hands in its
  ``CAPTURE_LOOKS`` and ``LOOK_STABLE_PX``) — and the plate centre falls into
  a slot not yet filled: a cell of the :data:`GRID_COLS` × :data:`GRID_ROWS`
  image grid in one of two size classes (the median tag edge below or at
  least :data:`SIZE_CLASS_EDGE_PX`). :data:`VIEWS_REQUIRED` views in at least
  :data:`CELLS_REQUIRED` cells end the capture. The plate centre is where
  the affine map of the tag centres (A13) puts the plate's origin, and each
  tag's corners are put into the model's order against that map (A14).
* THE SOLVE (:func:`solve`): the six lens parameters plus one plate pose per
  view, Levenberg–Marquardt from the nominal lens, each pose started from its
  homography. Gates: RMS ≤ :data:`RMS_MAX_PX` and the arm check's lens gates
  (focal length, principal point, aspect, ``k``, valid field).
* THE RESULT: :data:`FILE_NAME` beside ``arm_check.json``
  (:func:`write`, :func:`read`, :func:`discard`). The next arm check under the
  same ``camera_profile_id`` holds that lens fixed (``arm_check.run``'s
  ``lens_prior``, D89-1); a written arm check uses it up (the service).

Pure NumPy and the standard library.
"""

from __future__ import annotations

import math
import os
from typing import NamedTuple

import numpy as np

from physical_ai_server.workflow import scene_fixture as _sf
from physical_ai_server.workflow.autocal import arm_check as _ac
from physical_ai_server.workflow.autocal import chain, lm
from physical_ai_server.workflow.autocal import store as _store
from physical_ai_server.workflow.autocal import tags as _tags
from physical_ai_server.workflow.autocal import texts_de as _t
from physical_ai_server.workflow.autocal.camera_model import KB, LENS_FIELD_MIN_RAD, radial_map, valid_theta

# ── the capture (S4) ─────────────────────────────────────────────────────────
GRID_COLS = 4
GRID_ROWS = 3
SIZE_CLASS_EDGE_PX = 45.0
VIEWS_REQUIRED = 18
CELLS_REQUIRED = 9
MIN_TARGET_TAGS = _ac.CAPTURE_MIN_TARGET_TAGS

# ── the solve (S4) ───────────────────────────────────────────────────────────
RMS_MAX_PX = 0.5

FILE_NAME = 'intrinsics_handheld.json'
FORMAT_VERSION = 1
_KB_KEYS = ('f', 'aspect', 'cx', 'cy', 'k1', 'k2')

# The plate's 36 corners in its own frame (z = 0), row-major over its 3 × 3 tags — both arms' targets.
_PLATE = np.array([(x, y, 0.0) for _i, _j, corners in _sf.TARGET_CORNERS for x, y in corners], dtype=np.float64)
_PLATE_TAGS = _PLATE[:, :2].reshape(-1, 4, 2)
_PLATE_CENTRES = _PLATE_TAGS.mean(axis=1)


class View(NamedTuple):
    """One captured view: its ``slot`` ``(col, row, size class)``, the median
    corners of its three looks in the model's order (``obs`` ``(36, 2)``,
    ``mask`` ``(36,)`` for the tags seen), the plate centre and the median tag edge."""

    slot: tuple
    obs: np.ndarray
    mask: np.ndarray
    centre_px: tuple
    edge_px: float


def _corners(obs) -> np.ndarray:
    return np.asarray(obs.corners if isinstance(obs, _tags.TagObs) else obs, dtype=np.float64).reshape(4, 2)


def _affine(P, U):
    """``(A, b)`` with ``U ≈ P·Aᵀ + b`` (least squares), or ``None`` for points on a line."""
    Pm, Um = P.mean(axis=0), U.mean(axis=0)
    Pc, Uc = P - Pm, U - Um
    S = Pc.T @ Pc
    det = float(S[0, 0] * S[1, 1] - S[0, 1] * S[1, 0])
    if det <= 1e-12 * float(np.trace(S)) ** 2:
        return None
    A = (Uc.T @ Pc) @ (np.array([[S[1, 1], -S[0, 1]], [-S[1, 0], S[0, 0]]]) / det)
    return A, Um - A @ Pm


class Capture:
    """The guided capture of one handheld run (see the module docstring).
    ``offer`` takes one look's detections (``{tag id: corners or TagObs}``)."""

    def __init__(self, arm_key, *, looks: int, stable_px: float, image_size=(1920, 1080)) -> None:
        ids = _sf.ARMS[arm_key]['target_tag_ids']
        self._index = {tid: k for k, tid in enumerate(ids)}
        self.looks = int(looks)
        self.stable_px = float(stable_px)
        self.width, self.height = (int(v) for v in image_size)
        self.views: list = []
        self._streak: list = []

    @property
    def cells(self) -> set:
        return {v.slot[:2] for v in self.views}

    @property
    def finished(self) -> bool:
        return len(self.views) >= VIEWS_REQUIRED and len(self.cells) >= CELLS_REQUIRED

    def progress(self) -> dict:
        """The state topic's ``handheld`` object."""
        return {'views': len(self.views), 'views_needed': VIEWS_REQUIRED, 'cells': len(self.cells),
                'cells_needed': CELLS_REQUIRED, 'grid': [GRID_COLS, GRID_ROWS],
                'filled': [list(v.slot) for v in sorted(self.views, key=lambda v: v.slot)]}

    def offer(self, tags) -> bool:
        """One look; True when it completed a still streak that is a new view."""
        own = {tid: _corners(obs) for tid, obs in tags.items() if tid in self._index}
        if len(own) < MIN_TARGET_TAGS:
            self._streak = []
            return False
        if self._streak and not _ac.still_between(self._streak[-1], own, self.stable_px):
            self._streak = []
        self._streak.append(own)
        if len(self._streak) < self.looks:
            return False
        looks, self._streak = self._streak[-self.looks:], []
        common = [tid for tid in looks[-1] if all(tid in lk for lk in looks)]
        if len(common) < MIN_TARGET_TAGS or self.finished:
            return False
        view = self._view({tid: np.median(np.stack([lk[tid] for lk in looks]), axis=0) for tid in common})
        if view is None or view.slot in {v.slot for v in self.views}:
            return False
        self.views.append(view)
        return True

    def _view(self, corners):
        """The view of median corners ``{tag id: (4, 2)}``, or ``None`` when its
        plate centre cannot be placed inside the image."""
        ids = sorted(corners, key=self._index.get)
        centres = [_tags.centre_from_corners(corners[tid]) for tid in ids]
        if any(c is None for c in centres):
            return None
        fit = _affine(_PLATE_CENTRES[[self._index[tid] for tid in ids]], np.asarray(centres, dtype=np.float64))
        if fit is None:
            return None
        A, b = fit
        u, v = float(b[0]), float(b[1])
        if not (0.0 <= u < self.width and 0.0 <= v < self.height):
            return None
        obs = np.zeros((len(_PLATE), 2))
        mask = np.zeros(len(_PLATE), dtype=bool)
        for tid in ids:
            k = self._index[tid]
            obs[4 * k:4 * k + 4] = _tags.match_corners(corners[tid], _PLATE_TAGS[k] @ A.T + b)
            mask[4 * k:4 * k + 4] = True
        edge = float(np.median([np.mean(np.linalg.norm(np.roll(c, -1, axis=0) - c, axis=1))
                                for c in corners.values()]))
        col = min(int(u * GRID_COLS / self.width), GRID_COLS - 1)
        row = min(int(v * GRID_ROWS / self.height), GRID_ROWS - 1)
        return View((col, row, 0 if edge < SIZE_CLASS_EDGE_PX else 1), obs, mask, (u, v), edge)


# ── the solve ────────────────────────────────────────────────────────────────
class Result(NamedTuple):
    """``ok`` with the lens ``kb`` (``{'f', 'aspect', 'cx', 'cy', 'k1', 'k2'}``),
    or the gate ``code`` (``rms`` | ``lens``) that refused; ``message_de`` either way."""

    ok: bool
    code: str
    message_de: str
    kb: dict
    rms_px: float
    gates: dict


def _homography_pose(plate, pixels, lens):
    """The plate pose ``(R, t)`` (``X_c = R·X + t``) of one view from its
    homography (Zhang), the pixels unprojected through ``lens`` first: under
    the Kannala–Brandt model a plane does not image as a homography of pixels."""
    rays = KB(*lens).unproject(pixels)
    xn, yn = rays[:, 0] / rays[:, 2], rays[:, 1] / rays[:, 2]
    pitch = _sf.TARGET['pitch_m']
    X, Y = plate[:, 0] / pitch, plate[:, 1] / pitch
    one, zero = np.ones_like(X), np.zeros_like(X)
    M = np.vstack([np.stack([X, Y, one, zero, zero, zero, -xn * X, -xn * Y, -xn], axis=1),
                   np.stack([zero, zero, zero, X, Y, one, -yn * X, -yn * Y, -yn], axis=1)])
    Hm = np.linalg.svd(M)[2][-1].reshape(3, 3)
    lam = 2.0 / (np.linalg.norm(Hm[:, 0]) + np.linalg.norm(Hm[:, 1]))
    if lam * Hm[2, 2] < 0.0:                     # the plate lies in front of the camera
        lam = -lam
    r1, r2 = lam * Hm[:, 0], lam * Hm[:, 1]
    U, _s, Vt = np.linalg.svd(np.stack([r1, r2, np.cross(r1, r2)], axis=1))
    R = U @ np.diag([1.0, 1.0, float(np.sign(np.linalg.det(U @ Vt)))]) @ Vt
    return R, lam * Hm[:, 2] * pitch


def _project_views(p, n_views):
    """``(V, 36, 2)``: the plate's corners of every view through the lens ``p[:6]``."""
    f, a, cx, cy, k1, k2 = p[:6]
    poses = p[6:].reshape(n_views, 6)
    R = np.stack([chain.rodrigues(r) for r in poses[:, :3]])
    X = np.einsum('vij,kj->vki', R, _PLATE) + poses[:, None, 3:]
    rho = np.hypot(X[..., 0], X[..., 1])
    r = radial_map(np.arctan2(rho, X[..., 2]), k1, k2)
    s = np.divide(r, rho, out=np.zeros_like(rho), where=rho > 0.0)
    return np.stack([f * X[..., 0] * s + cx, a * f * X[..., 1] * s + cy], axis=-1)


def _gates(kb: dict, rms_px: float) -> dict:
    """Every gate's verdict (``True`` = passes): the RMS and the arm check's lens gates."""
    band = _sf.LENS_BAND['f_px']
    return {
        'rms': rms_px <= RMS_MAX_PX,
        'lens_f': band[0] <= kb['f'] <= band[1],
        'lens_pp': math.hypot(kb['cx'] - _sf.LENS_NOMINAL['cx'], kb['cy'] - _sf.LENS_NOMINAL['cy']) <= _ac.PP_MAX_PX,
        'lens_aspect': abs(kb['aspect'] - 1.0) <= _ac.ASPECT_TOL,
        'lens_k': max(abs(kb['k1']), abs(kb['k2'])) <= _ac.K_MAX,
        'lens_field': valid_theta(kb['k1'], kb['k2']) >= LENS_FIELD_MIN_RAD,
    }


def solve(views, max_iter: int = 60) -> Result:
    """The lens of the captured views and its gates."""
    lens = _sf.LENS_NOMINAL
    lens0 = (lens['f_px'], lens['aspect'], lens['cx'], lens['cy'], lens['k1'], lens['k2'])
    obs = np.stack([v.obs for v in views])
    mask = np.stack([v.mask for v in views])
    poses = [_homography_pose(_PLATE[m], o[m], lens0) for o, m in zip(obs, mask)]
    p0 = np.concatenate([lens0] + [np.concatenate([chain.rvec_of(R), t]) for R, t in poses])
    steps = np.full(p0.size, 1e-5)
    steps[:6] = (0.5, 1e-4, 0.2, 0.2, 1e-3, 1e-3)

    def residual(p):
        return (_project_views(p, len(views)) - obs)[mask].ravel() / _ac.PIXEL_SIGMA

    sol = lm.solve(residual, p0, steps, max_iter=max_iter)
    rms = math.sqrt(sol.data_cost / (2 * int(mask.sum()))) * _ac.PIXEL_SIGMA
    kb = {k: float(v) for k, v in zip(_KB_KEYS, sol.p[:6])}
    gates = _gates(kb, rms)
    code = 'ok'
    if not gates['rms']:
        code = 'rms'
    elif not all(gates.values()):
        code = 'lens'
    ok = code == 'ok'
    return Result(ok, code, _t.HANDHELD_DONE_DE if ok else _t.HANDHELD_FAILED_DE, kb, rms, gates)


# ── the result file ──────────────────────────────────────────────────────────
def _path(calib_dir) -> str:
    return os.path.join(str(calib_dir), _store.SUBDIR, FILE_NAME)


def result_dict(result: Result, capture: Capture, *, camera_profile_id, image_size, created_utc) -> dict:
    """The content of :data:`FILE_NAME` for a result that passed."""
    return {'v': FORMAT_VERSION, 'image_size': [int(v) for v in image_size], 'camera_profile_id': camera_profile_id,
            'created_utc': created_utc, 'kb': dict(result.kb),
            'quality': {'views': len(capture.views), 'cells': len(capture.cells), 'rms_px': float(result.rms_px)}}


def write(calib_dir, data: dict) -> None:
    """Write the result atomically beside ``arm_check.json``."""
    _store._atomic_write_json(_path(calib_dir), data)


def _real(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def read(calib_dir, image_size, *, camera_profile_id):
    """The pending handheld result for ``image_size`` and the streaming
    camera's ``camera_profile_id`` (the camera state's ``profile_id``), or
    ``None`` (none, used up, made at another image size, made under another
    ``camera_profile_id`` or with it unknown on either side — a lens of
    unproven provenance is never held fixed —, or malformed)."""
    data = _store._read_json(_path(calib_dir))
    if data is None or data.get('v') != FORMAT_VERSION:
        return None
    if data.get('image_size') != [int(v) for v in image_size]:
        return None
    stored = data.get('camera_profile_id')
    if type(stored) is not str or type(camera_profile_id) is not str or stored != camera_profile_id:
        return None
    kb = data.get('kb')
    if not isinstance(kb, dict) or not all(_real(kb.get(k)) for k in _KB_KEYS):
        return None
    return data


def discard(calib_dir) -> None:
    """The result is used up (an arm check holding it was written)."""
    try:
        os.unlink(_path(calib_dir))
    except FileNotFoundError:
        pass

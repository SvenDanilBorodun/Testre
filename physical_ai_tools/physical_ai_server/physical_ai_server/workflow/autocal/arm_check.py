#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The arm check's solve, its gates and the stored calibration (spec 40 S4).

The service (WP-13) moves the arm through the 20 contract poses with the
clip-on target and hands this module one :class:`PoseRecord` per pose the
arm came to rest at (with the target corners when it was captured) and the
full-frame looks of the field and fixture tags. :func:`run` then

1. counts the captured poses and the tags (``too_few_poses``,
   ``tags_hidden``, ``fixture_hidden``);
2. solves one Levenberg–Marquardt problem (:class:`ArmCheckModel`) over the
   camera (``f``, aspect, ``cx, cy, k1, k2``; pose), the joint offsets
   ``d2 … d4``, five joint-axis tilts (``j2b`` fixed at 0: a joint-1 zero
   offset), the target's mount, the mat's slope ``ta, tb``, its pose
   ``dx, dy, dpsi`` and print scale; the mat height is the design value;
   every fifth captured pose is held out;
3. places the tower by its two fixture tags (``scene_fixture.tower_pose_fit``
   of the 8 corners back-projected with the solved camera, D65);
4. applies every gate of S4 in a fixed order (:func:`first_failure`); the
   first one that fails names the sentence;
5. answers the calibration dict for ``store.Store.write`` — with the droop
   warning and the note ``droop_above_design`` when they apply (D33, D42: a
   warning, never a refusal).

The ``guard_margin`` gate (the box guard on the solved arm) is the service's.

Frames: the arm's solver frame A (+ ``LIFT_M`` in z) is tilted into
Roboter Studio's world W, where the mat is flat at ``MAT_THICKNESS_M``, by
``R_t``, the smallest rotation turning the mat normal ``(−ta, −tb, 1)`` onto
+z, about ``P0 = (base_axis_x, 0, MAT_THICKNESS_M)`` (S6.1). A camera pose
``(R, t)`` maps ``X_c = R·X + t``.

Pure NumPy and the standard library.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as _dt
import math
from typing import NamedTuple

import numpy as np

from physical_ai_server.workflow import scene_fixture as _sf
from physical_ai_server.workflow.autocal import chain, lm, tags as _tags, texts_de as _t
from physical_ai_server.workflow.autocal.camera_model import LENS_FIELD_MIN_RAD, valid_theta
from physical_ai_server.workflow.autocal.mat_check import (
    backproject_to_z,
    camera_centre,
    contract_axis,
    contract_camera,
    field_model_corners,
    lens_against_tower,
    project,
)

# ── the capture rule's static half (S4; stillness and the three looks are the service's) ──
CAPTURE_JOINT_TOL_RAD = 0.15
CAPTURE_MIN_TARGET_TAGS = 6

# ── the gates (S4) ───────────────────────────────────────────────────────────
MIN_POSES = 14
MIN_FIELD_TAGS = 10
MIN_TAG_FRAMES = 5
RMS_MAX_PX = 0.6
HOLDOUT_MAX_PX = 1.0
PP_MAX_PX = 60.0
ASPECT_TOL = 0.01
K_MAX = 0.25
OFFSET_MAX_RAD = math.radians(3.0)
TILT_MAX_RAD = math.radians(1.5)
MOUNT_MAX_M = 0.005
MOUNT_MAX_RAD = math.radians(5.0)
MAT_SHIFT_MAX_M = 0.020
MAT_YAW_MAX_RAD = math.radians(4.0)
SCALE_TOL = 0.02
SLOPE_MAX = 0.02
LENS_AXIAL_MAX_M = 0.025
AXIS_MAX_RAD = math.radians(3.0)
# The droop WARNING (D33, D42): a joint-2..4 envelope above this is warned, never refused.
DROOP_ENVELOPE_MAX_RAD = 0.10

HOLDOUT_EVERY = 5
PIXEL_SIGMA = 0.15

# ── the parameter vector ─────────────────────────────────────────────────────
I_F, I_ASPECT, I_CX, I_CY, I_K1, I_K2 = range(6)
S_CAM_R = slice(6, 9)
S_CAM_T = slice(9, 12)
S_D = slice(12, 17)                 # d1 … d5
S_TILT = slice(17, 23)              # j2a, j2b, j3a, j3b, j4a, j4b
S_MOUNT_R = slice(23, 26)
S_MOUNT_T = slice(26, 29)
I_TA, I_TB = 29, 30
I_DX, I_DY, I_DPSI = 31, 32, 33
I_SCALE = 34
I_HEIGHT = 35
N_PARAMS = 36
I_D1, I_D5 = 12, 16
I_J2B = 18

PARAM_NAMES = (
    ('f', 'aspect', 'cx', 'cy', 'k1', 'k2', 'cam_rx', 'cam_ry', 'cam_rz', 'cam_tx', 'cam_ty', 'cam_tz',
     'd1', 'd2', 'd3', 'd4', 'd5') + chain.TILT_SLOTS
    + ('mount_rx', 'mount_ry', 'mount_rz', 'mount_tx', 'mount_ty', 'mount_tz', 'ta', 'tb', 'dx', 'dy', 'dpsi',
       'print_scale', 'mat_height'))


class PoseRecord(NamedTuple):
    """One contract pose at which the arm came to rest (still for 0.3 s during
    its dwell): ``target`` = ``{tag_id: (4, 2) corners}`` of this arm's target
    tags when the pose was captured, ``None`` when it was not."""

    index: int
    commanded: tuple
    measured: tuple
    target: dict | None = None


def capturable(commanded, measured, target_tags: int) -> bool:
    """The capture rule's static half: the measured arm joints within
    :data:`CAPTURE_JOINT_TOL_RAD` of the contract pose and at least
    :data:`CAPTURE_MIN_TARGET_TAGS` target tags seen. (Stillness and the three
    looks are the capture thread's.)"""
    err = np.abs(np.asarray(measured, dtype=np.float64)[:5] - np.asarray(commanded, dtype=np.float64)[:5])
    return bool(np.all(err <= CAPTURE_JOINT_TOL_RAD) and int(target_tags) >= CAPTURE_MIN_TARGET_TAGS)


def droop_envelope(records) -> np.ndarray:
    """Per joint the largest ``|commanded − measured|`` over every rested pose,
    captured or not (5 values)."""
    if not records:
        return np.zeros(5)
    err = [np.abs(np.asarray(r.commanded, dtype=np.float64)[:5] - np.asarray(r.measured, dtype=np.float64)[:5])
           for r in records]
    return np.max(np.stack(err), axis=0)


def mount_nominal(arm_key: str) -> np.ndarray:
    return np.asarray(_sf.ARMS[arm_key]['target_mount_ee'], dtype=np.float64)


def target_model(arm_key: str):
    """``(ids, (36, 3))``: the target's tag ids (row-major) and their corners in
    the target frame, four per tag in ``tag_corners_xy`` order."""
    pairs = _sf.target_tag_corners(_sf.ARMS[arm_key])
    ids = tuple(tid for tid, _ in pairs)
    pts = np.array([(x, y, 0.0) for _, corners in pairs for x, y in corners], dtype=np.float64)
    return ids, pts


# ── the model ────────────────────────────────────────────────────────────────
class ArmCheckModel:
    """The arm check's estimation problem: the parameter vector (:data:`PARAM_NAMES`),
    its start, priors, steps and free mask, and the σ-normalised residual.

    ``Q`` are the measured joints of the fit poses, ``target_obs`` /
    ``target_mask`` their target corners in the model's order (``(N, 36, 2)``,
    ``(N, 36)``), ``field_ids`` / ``field_obs`` the field tags and their median
    corners. ``lens_prior`` = a handheld lens (``{'kb': {...}}``) held under its
    tight prior. ``free`` may be edited before :meth:`solve` (the
    identifiability test frees ``j2b``, ``d5`` or the mat height)."""

    def __init__(self, arm_key, Q, target_obs, target_mask, field_ids, field_obs, *, lens_prior=None):
        self.kind = arm_key
        self.base_axis_x = chain.BASE_AXIS_X[arm_key]
        self.Q = np.asarray(Q, dtype=np.float64).reshape(-1, 5)
        self.target_obs = np.asarray(target_obs, dtype=np.float64).reshape(self.Q.shape[0], -1, 2)
        self.target_mask = np.asarray(target_mask, dtype=bool).reshape(self.Q.shape[0], -1)
        self.target_ids, self.Xt = target_model(arm_key)
        self.field_ids = tuple(field_ids)
        self.field_P = field_model_corners(_sf.ARMS[arm_key], self.field_ids, 1.0).reshape(-1, 3)[:, :2]
        self.field_obs = np.asarray(field_obs, dtype=np.float64).reshape(-1, 2)
        self.M_nom = mount_nominal(arm_key)

        R0, t0 = contract_camera(self.base_axis_x)
        lens = _sf.LENS_NOMINAL
        p0 = np.zeros(N_PARAMS)
        p0[:6] = (lens['f_px'], lens['aspect'], lens['cx'], lens['cy'], lens['k1'], lens['k2'])
        p0[S_CAM_R] = chain.rvec_of(R0)
        p0[S_CAM_T] = t0
        p0[I_SCALE] = 1.0
        p0[I_HEIGHT] = _sf.MAT_THICKNESS_M
        mean = p0.copy()
        sig = np.full(N_PARAMS, np.inf)
        sig[:6] = (60.0, 0.005, 40.0, 40.0, 0.15, 0.15)
        if lens_prior is not None:
            kb = lens_prior['kb']
            p0[:6] = mean[:6] = (kb['f'], kb['aspect'], kb['cx'], kb['cy'], kb['k1'], kb['k2'])
            sig[:6] = (0.5, 0.005, 0.5, 0.5, 0.005, 0.005)
        sig[13:16] = math.radians(3.0)
        sig[S_TILT] = math.radians(1.0)
        sig[S_MOUNT_R] = math.radians(0.3)
        sig[S_MOUNT_T] = 0.0003
        self.p0 = p0
        self.prior_mean = mean
        self.prior_sigma = sig
        self.steps = np.full(N_PARAMS, 1e-5)
        self.steps[:6] = (0.5, 1e-4, 0.2, 0.2, 1e-3, 1e-3)
        free = np.ones(N_PARAMS, dtype=bool)
        free[[I_D1, I_D5, I_J2B, I_HEIGHT]] = False
        self.free = free

    # -- the forward model --
    def mount(self, p) -> np.ndarray:
        T = np.eye(4)
        T[:3, :3] = chain.rodrigues(p[S_MOUNT_R])
        T[:3, 3] = p[S_MOUNT_T]
        return self.M_nom @ T

    def w_from_a(self, p):
        """``(R_t, P0)``: a point of the arm's frame A goes to ``P0 + R_t·(x − P0)``."""
        return chain.tilt_rotation(p[I_TA], p[I_TB]), np.array([self.base_axis_x, 0.0, p[I_HEIGHT]])

    def target_points_w(self, p, Q) -> np.ndarray:
        """``(N, 36, 3)``: the target corners in W for the encoder poses ``Q``."""
        E = chain.ee_batch(self.kind, np.asarray(Q, dtype=np.float64) + p[S_D], p[S_TILT])
        M = self.mount(p)
        Xee = self.Xt @ M[:3, :3].T + M[:3, 3]
        X = np.einsum('nij,kj->nki', E[:, :3, :3], Xee) + E[:, None, :3, 3]
        X[..., 2] += _sf.LIFT_M
        Rt, P0 = self.w_from_a(p)
        return (X - P0) @ Rt.T + P0

    def field_points_w(self, p) -> np.ndarray:
        s, dpsi = p[I_SCALE], p[I_DPSI]
        c, sn = math.cos(dpsi), math.sin(dpsi)
        P = self.field_P * s
        x = self.base_axis_x + p[I_DX] + c * P[:, 0] - sn * P[:, 1]
        y = p[I_DY] + sn * P[:, 0] + c * P[:, 1]
        return np.stack([x, y, np.full_like(x, p[I_HEIGHT])], axis=1)

    def camera(self, p):
        return chain.rodrigues(p[S_CAM_R]), p[S_CAM_T].copy(), tuple(float(v) for v in p[:6])

    def residual(self, p) -> np.ndarray:
        R, t, kb = self.camera(p)
        uv_t = project(self.target_points_w(p, self.Q), R, t, kb)
        r_t = (uv_t - self.target_obs)[self.target_mask]
        uv_m = project(self.field_points_w(p), R, t, kb)
        r_m = uv_m - self.field_obs
        return np.concatenate([r_t.ravel(), r_m.ravel()]) / PIXEL_SIGMA

    @property
    def n_px(self) -> int:
        return 2 * (int(self.target_mask.sum()) + self.field_obs.shape[0])

    def solve(self, max_iter: int = 60) -> lm.Result:
        return lm.solve(self.residual, self.p0, self.steps, free=self.free, prior_mean=self.prior_mean,
                        prior_sigma=self.prior_sigma, max_iter=max_iter)

    def jacobian(self, p, free=None, prior_sigma=None) -> np.ndarray:
        """The Jacobian of the σ-normalised rows (data and priors) at ``p``."""
        free = self.free if free is None else np.asarray(free, dtype=bool)
        sig = self.prior_sigma if prior_sigma is None else np.asarray(prior_sigma, dtype=np.float64)
        return lm.full_jacobian(self.residual, p, np.flatnonzero(free), self.steps, self.prior_mean, sig)


# ── assembling the model from records ────────────────────────────────────────
def _nominal_target_px(arm_key, q, R0, t0, kb0):
    """The contract camera's view of the target at a commanded pose (nominal arm,
    nominal mount): the reference the detected corners are ordered against."""
    p = np.zeros(N_PARAMS)
    p[I_SCALE] = 1.0
    p[I_HEIGHT] = _sf.MAT_THICKNESS_M
    m = _Nominal(arm_key)
    X = m.target_points_w(p, np.asarray(q, dtype=np.float64)[None, :5])[0]
    return project(X, R0, t0, kb0)


class _Nominal:
    """Just enough of :class:`ArmCheckModel` for the nominal views."""

    def __init__(self, arm_key):
        self.kind = arm_key
        self.base_axis_x = chain.BASE_AXIS_X[arm_key]
        self.Xt = target_model(arm_key)[1]
        self.M_nom = mount_nominal(arm_key)

    mount = ArmCheckModel.mount
    w_from_a = ArmCheckModel.w_from_a
    target_points_w = ArmCheckModel.target_points_w


def _lens0():
    lens = _sf.LENS_NOMINAL
    return (lens['f_px'], lens['aspect'], lens['cx'], lens['cy'], lens['k1'], lens['k2'])


def _target_arrays(arm_key, records, R0, t0, kb0):
    ids = target_model(arm_key)[0]
    n = len(records)
    obs = np.zeros((n, len(ids) * 4, 2))
    mask = np.zeros((n, len(ids) * 4), dtype=bool)
    for i, rec in enumerate(records):
        nominal = _nominal_target_px(arm_key, rec.commanded, R0, t0, kb0)
        for k, tid in enumerate(ids):
            c = (rec.target or {}).get(tid)
            if c is None:
                continue
            c = c.corners if isinstance(c, _tags.TagObs) else c
            obs[i, 4 * k:4 * k + 4] = _tags.match_corners(c, nominal[4 * k:4 * k + 4])
            mask[i, 4 * k:4 * k + 4] = True
    return obs, mask


def _aligned_medians(looks, ids, nominal_px):
    """``{tag_id: (median (4, 2), frames)}`` with every look matched to the
    nominal view of that tag first."""
    per = {}
    for lk in looks:
        for tid in ids:
            c = lk.get(tid)
            if c is None:
                continue
            c = c.corners if isinstance(c, _tags.TagObs) else c
            per.setdefault(tid, []).append(_tags.match_corners(c, nominal_px[tid]))
    return {tid: (np.median(np.stack(cs), axis=0), len(cs)) for tid, cs in per.items()}


@dataclass
class ArmCheckResult:
    """``ok`` with ``calibration`` (the dict for ``Store.write``), or the gate
    ``code`` that refused with its sentence. ``warnings_de`` and ``notes``
    are carried either way (the droop warning, ``droop_above_design``)."""

    ok: bool
    code: str
    message_de: str
    calibration: dict | None = None
    notes: tuple = ()
    warnings_de: tuple = ()
    droop_warning: dict | None = None
    offers_handheld: bool = False
    quality: dict = field(default_factory=dict)
    details: dict = field(default_factory=dict)


@dataclass
class Prepared:
    """The arm check's inputs, sorted: captured records split into fit and
    hold-out, the field and fixture medians, the counts."""

    arm_key: str
    records: tuple
    captured: tuple
    fit: tuple
    hold: tuple
    field_ids: tuple
    field_med: dict
    fixture_med: dict
    fixture_frames: dict
    margins: list


def prepare(arm_key, records, looks) -> Prepared:
    """Sort the records and the full-frame looks (``{tag_id: corners or TagObs}``)."""
    spec = _sf.ARMS[arm_key]
    ax = chain.BASE_AXIS_X[arm_key]
    R0, t0 = contract_camera(ax)
    kb0 = _lens0()
    records = tuple(records)
    captured = tuple(r for r in records if r.target is not None
                     and sum(1 for tid in spec['target_tag_ids'] if tid in r.target) >= CAPTURE_MIN_TARGET_TAGS)
    fit = tuple(r for k, r in enumerate(captured) if k % HOLDOUT_EVERY != HOLDOUT_EVERY - 1)
    hold = tuple(r for k, r in enumerate(captured) if k % HOLDOUT_EVERY == HOLDOUT_EVERY - 1)
    field_ids_all = tuple(sorted(spec['mat_tags_f']))
    model = field_model_corners(spec, field_ids_all, 1.0)
    nominal_field = project(model + (ax, 0.0, _sf.MAT_THICKNESS_M), R0, t0, kb0)
    nominal = {tid: nominal_field[i] for i, tid in enumerate(field_ids_all)}
    fx = np.asarray(_sf.fixture_corners_w(ax), dtype=np.float64)
    fx3 = np.hstack([fx, np.full((8, 1), _sf.FIXTURE_TAG_Z_M)])
    nominal_fx = project(fx3, R0, t0, kb0)
    for k, tid in enumerate(_tags.FIXTURE_IDS):
        nominal[tid] = nominal_fx[4 * k:4 * k + 4]
    looks = list(looks or ())
    med = _aligned_medians(looks, field_ids_all + _tags.FIXTURE_IDS, nominal)
    field_ids = tuple(tid for tid in field_ids_all if tid in med and med[tid][1] >= MIN_TAG_FRAMES)
    margins = [lk[tid].margin for lk in looks for tid in field_ids
               if isinstance(lk.get(tid), _tags.TagObs) and lk[tid].margin is not None]
    return Prepared(
        arm_key=arm_key, records=records, captured=captured, fit=fit, hold=hold, field_ids=field_ids,
        field_med={tid: med[tid][0] for tid in field_ids},
        fixture_med={tid: med[tid][0] for tid in _tags.FIXTURE_IDS if tid in med},
        fixture_frames={tid: (med[tid][1] if tid in med else 0) for tid in _tags.FIXTURE_IDS},
        margins=margins)


def build_model(prep: Prepared, *, lens_prior=None) -> ArmCheckModel:
    """The :class:`ArmCheckModel` of the fit records."""
    recs = prep.fit
    ax = chain.BASE_AXIS_X[prep.arm_key]
    R0, t0 = contract_camera(ax)
    obs, mask = _target_arrays(prep.arm_key, recs, R0, t0, _lens0())
    Q = np.array([np.asarray(r.measured, dtype=np.float64)[:5] for r in recs]).reshape(-1, 5)
    fobs = np.vstack([prep.field_med[tid] for tid in prep.field_ids]) if prep.field_ids else np.zeros((0, 2))
    return ArmCheckModel(prep.arm_key, Q, obs, mask, prep.field_ids, fobs, lens_prior=lens_prior)


# ── what the solution says ───────────────────────────────────────────────────
def _holdout(model: ArmCheckModel, p, prep: Prepared):
    """``(rms_px, p95_mm)`` of the held-out poses (``None`` without any)."""
    if not prep.hold:
        return None, None
    R, t, kb = model.camera(p)
    obs, mask = _target_arrays(prep.arm_key, prep.hold, *contract_camera(model.base_axis_x), _lens0())
    Q = np.array([np.asarray(r.measured, dtype=np.float64)[:5] for r in prep.hold])
    X = model.target_points_w(p, Q)
    r = (project(X, R, t, kb) - obs)[mask]
    if r.size == 0:
        return None, None
    depth = (X @ R.T + t)[..., 2][mask]
    mm = np.linalg.norm(r, axis=1) * depth / kb[0] * 1000.0
    return math.sqrt(float(np.mean(np.square(r)))), float(np.percentile(mm, 95))


def tower_pose_from_fixture(model: ArmCheckModel, p, fixture_med: dict):
    """``(fixture_xy_mat (8, 2), fixture_px (8, 2), (psi, tx, ty, rms))``: the 8
    fixture corners back-projected with the solved camera onto the fixture
    plane, in M and in W, and the tower pose fitted to them (D65)."""
    R, t, kb = model.camera(p)
    px = np.vstack([fixture_med[tid] for tid in _tags.FIXTURE_IDS])
    W = backproject_to_z(px, R, t, kb, _sf.FIXTURE_TAG_Z_M)
    dx, dy, dpsi = p[I_DX], p[I_DY], p[I_DPSI]
    c, s = math.cos(-dpsi), math.sin(-dpsi)
    rel = W[:, :2] - (model.base_axis_x + dx, dy)
    xy_m = rel @ np.array([[c, -s], [s, c]]).T
    pose = _sf.tower_pose_fit(W[:, :2], model.base_axis_x) if np.all(np.isfinite(W)) else None
    return xy_m, px, pose


def lens_reading(model: ArmCheckModel, p, tower_pose):
    """``(lens_w, along_m, across_m, axis_rad)``: the solved lens (in W) against
    the lens carried by the tower pose, and the optical axis against the
    contract's — both judged in the arm's frame (:func:`mat_check.lens_against_tower`)."""
    R, t, _ = model.camera(p)
    L = camera_centre(R, t)
    along, across = lens_against_tower(L, (p[I_TA], p[I_TB]), model.base_axis_x, tower_pose)
    axis_a = chain.tilt_rotation(p[I_TA], p[I_TB]).T @ R[2]
    axis = math.acos(max(-1.0, min(1.0, float(axis_a @ contract_axis()))))
    return L, along, across, axis


def judge(prep: Prepared, model: ArmCheckModel, sol: lm.Result) -> dict:
    """Every gate's verdict (``True`` = passes) and the readings behind them."""
    p = sol.p
    rms = math.sqrt(sol.data_cost / max(model.n_px, 1)) * PIXEL_SIGMA
    ho_rms, ho_p95 = _holdout(model, p, prep)
    f, a, cx, cy, k1, k2 = (float(v) for v in p[:6])
    band = _sf.LENS_BAND['f_px']
    vt = valid_theta(k1, k2)
    d = p[S_D]
    tl = p[S_TILT]
    xy_m, px, pose = tower_pose_from_fixture(model, p, prep.fixture_med)
    tower_ok = pose is not None and math.isfinite(pose[3]) and pose[3] <= _sf.TOWER_FIT_MAX_M
    lens_w, along, across, axis = lens_reading(model, p, None if pose is None else pose[:3])
    g = {
        'rms': rms <= RMS_MAX_PX and ho_rms is not None and ho_rms <= HOLDOUT_MAX_PX,
        'lens_f': band[0] <= f <= band[1],
        'lens_pp': math.hypot(cx - _sf.LENS_NOMINAL['cx'], cy - _sf.LENS_NOMINAL['cy']) <= PP_MAX_PX,
        'lens_aspect': abs(a - 1.0) <= ASPECT_TOL,
        'lens_k': max(abs(k1), abs(k2)) <= K_MAX,
        'lens_field': vt >= LENS_FIELD_MIN_RAD,
        'lens_along': along <= LENS_AXIAL_MAX_M,
        'offsets': float(np.max(np.abs(d))) <= OFFSET_MAX_RAD,
        'tilts': float(np.max(np.abs(tl))) <= TILT_MAX_RAD,
        'mount': (float(np.linalg.norm(p[S_MOUNT_T])) <= MOUNT_MAX_M
                  and float(np.linalg.norm(p[S_MOUNT_R])) <= MOUNT_MAX_RAD),
        'mat_pose': abs(p[I_DX]) <= MAT_SHIFT_MAX_M and abs(p[I_DY]) <= MAT_SHIFT_MAX_M
                    and abs(p[I_DPSI]) <= MAT_YAW_MAX_RAD,
        'scale': abs(p[I_SCALE] - 1.0) <= SCALE_TOL,
        'slope': abs(p[I_TA]) <= SLOPE_MAX and abs(p[I_TB]) <= SLOPE_MAX,
        'fixture_layout': tower_ok,
        'lens_across': across <= _sf.LENS_FOLLOW_MAX_M,
        'axis': axis <= AXIS_MAX_RAD,
    }
    return dict(gates=g, rms_px=rms, holdout_rms_px=ho_rms, holdout_p95_mm=ho_p95, valid_theta=vt,
                lens_w=lens_w, lens_along_m=along, lens_across_m=across, axis_rad=axis,
                fixture_xy_mat=xy_m, fixture_px=px, tower_pose=pose)


def first_failure(j: dict) -> str | None:
    """The first failing gate after the solve, or ``None``: ``rms`` (solve and
    hold-out), ``lens`` (focal length, principal point, aspect, ``k``, valid
    field), ``joint`` (offsets, tilts), ``mount``, ``mat`` (pose, scale,
    slope), ``fixture_layout``, ``tower`` (across the axis, the axis), and the
    bound along the axis (``lens``). ``too_few_poses``, ``tags_hidden`` and
    ``fixture_hidden`` are judged before the solve (:func:`run`)."""
    g = j['gates']
    if not g['rms']:
        return 'rms'
    if not (g['lens_f'] and g['lens_pp'] and g['lens_aspect'] and g['lens_k'] and g['lens_field']):
        return 'lens'
    if not (g['offsets'] and g['tilts']):
        return 'joint'
    if not g['mount']:
        return 'mount'
    if not (g['mat_pose'] and g['scale'] and g['slope']):
        return 'mat'
    if not g['fixture_layout']:
        return 'fixture_layout'
    if not (g['lens_across'] and g['axis']):
        return 'tower'
    if not g['lens_along']:
        return 'lens'
    return None


def _droop(prep: Prepared):
    env = droop_envelope(prep.records)
    warning, warnings_de, notes = None, [], []
    over = [j for j in (1, 2, 3) if env[j] > DROOP_ENVELOPE_MAX_RAD]
    if over:
        j = max(over, key=lambda k: env[k])
        deg = math.degrees(float(env[j]))
        warning = {'joint': j + 1, 'deg': int(round(deg))}
        warnings_de.append(_t.droop_warning_de(j + 1, deg))
    design = _sf.TOWER_DROOP_DESIGN_ENV_RAD[prep.arm_key]
    if any(env[j + 1] > design[j] for j in range(3)):
        notes.append('droop_above_design')
        warnings_de.append(_t.DROOP_ABOVE_DESIGN_DE)
    return env, warning, tuple(warnings_de), tuple(notes)


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def calibration_dict(prep, model, sol, j, env, warning, notes, *, profile_id, camera_profile_id,
                     image_size, created_utc) -> dict:
    """The ``arm_check.json`` content (spec 40 S4) of a solution that passed.
    ``camera_pose_mat`` is the camera's extrinsic against M (``X_c = R·X_M + t``),
    ``mount`` the target frame in the end-effector frame, ``lens_w`` the solved
    lens in W, ``fixture_xy_mat`` the 8 fixture corners in M."""
    p = sol.p
    R, t, _ = model.camera(p)
    T_wm = np.eye(4)
    c, s = math.cos(p[I_DPSI]), math.sin(p[I_DPSI])
    T_wm[:3, :3] = ((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0))
    T_wm[:3, 3] = (model.base_axis_x + p[I_DX], p[I_DY], p[I_HEIGHT])
    R_cm = R @ T_wm[:3, :3]                                   # camera from M
    t_cm = R @ T_wm[:3, 3] + t
    M = model.mount(p)
    psi, tx, ty, rms_fit = j['tower_pose']
    sharp = float(np.median(prep.margins)) if prep.margins else None
    lst = lambda a: [float(v) for v in np.asarray(a, dtype=np.float64).ravel()]  # noqa: E731
    return {
        'v': 1,
        'fixture_id': _sf.FIXTURE_ID,
        'contract_hash': _sf.contract_hash(),
        'profile_id': profile_id,
        'camera_profile_id': camera_profile_id,
        'image_size': [int(v) for v in image_size],
        'created_utc': created_utc,
        'kb': {'f': float(p[I_F]), 'aspect': float(p[I_ASPECT]), 'cx': float(p[I_CX]), 'cy': float(p[I_CY]),
               'k1': float(p[I_K1]), 'k2': float(p[I_K2])},
        'print_scale': float(p[I_SCALE]),
        'joint_offsets_rad': [0.0, float(p[13]), float(p[14]), float(p[15]), 0.0],
        'axis_tilts_rad': [float(p[17]), 0.0, float(p[19]), float(p[20]), float(p[21]), float(p[22])],
        'tilt': [float(p[I_TA]), float(p[I_TB])],
        'mat_pose': [float(p[I_DX]), float(p[I_DY]), float(p[I_DPSI])],
        'camera_pose_mat': {'rvec': lst(chain.rvec_of(R_cm)), 't': lst(t_cm)},
        'lens_w': lst(j['lens_w']),
        'fixture_xy_mat': [lst(v) for v in j['fixture_xy_mat']],
        'fixture_px': [lst(v) for v in j['fixture_px']],
        'mount': {'rvec': lst(chain.rvec_of(M[:3, :3])), 't': lst(M[:3, 3])},
        'droop_envelope_rad': lst(env),
        'tower_pose': [float(psi), float(tx), float(ty)],
        'tower_fit_rms_m': float(rms_fit),
        'target_removed': False,
        'droop_warning': warning,
        'notes': list(notes),
        'quality': {
            'poses_used': len(prep.captured),
            'rms_px': float(j['rms_px']),
            'holdout_rms_px': None if j['holdout_rms_px'] is None else float(j['holdout_rms_px']),
            'holdout_p95_mm': None if j['holdout_p95_mm'] is None else float(j['holdout_p95_mm']),
            'field_tags': len(prep.field_ids),
            'sharpness': sharp,
        },
    }


def _refusal(code, warnings_de, notes, warning, *, k=0, n=0, details=None):
    msg = _t.too_few_poses_de(k, n) if code == 'too_few_poses' else _t.BY_CODE[code]
    return ArmCheckResult(ok=False, code=code, message_de=msg, notes=notes, warnings_de=warnings_de,
                          droop_warning=warning, offers_handheld=code in ('rms', 'lens'),
                          details=details or {})


def run(records, looks, *, profile_id: str, image_size=(1920, 1080), camera_profile_id=None,
        lens_prior=None, created_utc=None) -> ArmCheckResult:
    """The whole arm check after the program ended (``finished``): count, solve,
    place the tower, judge, answer the calibration (not written here)."""
    arm_key = _sf.arm_key(profile_id)
    if arm_key is None:
        raise ValueError(f'no tower contract for profile {profile_id!r}')
    n_poses = len(_sf.ARMS[arm_key]['arm_check_poses_rad'])
    prep = prepare(arm_key, records, looks)
    env, warning, warnings_de, notes = _droop(prep)
    if len(prep.captured) < MIN_POSES or len(prep.fit) < 6:
        return _refusal('too_few_poses', warnings_de, notes, warning, k=len(prep.captured), n=n_poses)
    if len(prep.field_ids) < MIN_FIELD_TAGS:
        return _refusal('tags_hidden', warnings_de, notes, warning)
    if any(prep.fixture_frames[tid] < MIN_TAG_FRAMES for tid in _tags.FIXTURE_IDS):
        return _refusal('fixture_hidden', warnings_de, notes, warning)
    model = build_model(prep, lens_prior=lens_prior)
    sol = model.solve()
    j = judge(prep, model, sol)
    details = {k: v for k, v in j.items() if k not in ('fixture_xy_mat', 'fixture_px')}
    details['p'] = sol.p
    code = first_failure(j)
    if code is not None:
        return _refusal(code, warnings_de, notes, warning, details=details)
    cal = calibration_dict(prep, model, sol, j, env, warning, notes, profile_id=profile_id,
                           camera_profile_id=camera_profile_id, image_size=image_size,
                           created_utc=created_utc or _utc_now())
    return ArmCheckResult(ok=True, code='ok', message_de=_t.OK_DE, calibration=cal, notes=notes,
                          warnings_de=warnings_de, droop_warning=warning, quality=cal['quality'],
                          details=details)

#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The mat check (spec 40 S4): at every start, no motion, three frames.

1. Refusals in a fixed order, each a code with its German sentence
   (:func:`run`).
2. The camera's pose in the mat frame M by a six-parameter PnP on the median
   corners of the visible field tags against ``print_scale ×
   tag_corners_xy``, the lens held at the stored arm check's. A tag whose
   mean residual exceeds :data:`TAG_DROP_PX` is dropped once (note
   ``tag_dropped``); a second one is ``mat_not_flat``; an RMS above
   :data:`RMS_MAX_PX` is ``inconsistent``.
3. The two fixture tags back-projected onto their plane, a 2D rigid fit ``G``
   onto the stored ``fixture_xy_mat``: the mat's move against the base plate.
4. The solved lens judged twice (decision C7 as the amendment for D33 and
   revision 4 word it): ACROSS the contract's optical axis against the lens
   carried by the stored tower pose (in the arm's frame, :func:`lens_against_tower`),
   and in any direction against the arm check's own lens. Either reading above ``LENS_FOLLOW_MAX_M`` is
   ``tower_displaced``; a turn above :data:`CAMERA_MOVED_DEG` is
   ``needs_arm_check/camera_moved``.
5. ``ok``: ``T_cam→W = T_W←M0 · G · T_M←cam``.

Also here, shared with the arm check: the camera's projection with the
tower lens, back-projection onto a plane, the PnP, the 2D rigid fit.

Frames: W is Roboter Studio's world (z = 0 on the table, the mat flat at
``MAT_THICKNESS_M``), M the mat's metric layout frame (z = 0 on its top).
A camera pose ``(R, t)`` maps a point into the camera: ``X_c = R·X + t``.

Pure NumPy and the standard library.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import time

import numpy as np

from physical_ai_server.workflow import scene_fixture as _sf
from physical_ai_server.workflow.autocal import lm, tags as _tags, texts_de as _t
from physical_ai_server.workflow.autocal.camera_model import radial_map, unproject_normalised
from physical_ai_server.workflow.autocal.chain import BASE_AXIS_X, rodrigues, rvec_of, tilt_rotation

# Step 1.
TOO_DARK_GREY = 25.0
MIN_FIELD_TAGS = 6
WRONG_MAT_TAGS = 3
# Step 2.
TAG_DROP_PX = 1.5
RMS_MAX_PX = 0.6
# Step 3.
FIXTURE_FIT_MAX_M = 0.0005
MAT_MOVED_M = 0.0008
MAT_MOVED_DEG = 0.15
MAT_MISPLACED_M = 0.015
MAT_MISPLACED_DEG = 3.0
# Step 4.
CAMERA_MOVED_DEG = 3.0
FOLLOW_NOTE_M = 0.005
FOLLOW_NOTE_DEG = 0.5

# The plane of the fixture tags in M.
FIXTURE_Z_M = _sf.FIXTURE_TAG_Z_M - _sf.MAT_THICKNESS_M


# ── camera geometry ──────────────────────────────────────────────────────────
def project(points, R, t, kb) -> np.ndarray:
    """Pixels (``..., 2``) of points (``..., 3``) for the camera pose ``(R, t)``
    and the lens ``kb = (f, aspect, cx, cy, k1, k2)``."""
    f, a, cx, cy, k1, k2 = (float(v) for v in kb)
    X = np.asarray(points, dtype=np.float64) @ np.asarray(R, dtype=np.float64).T + np.asarray(t, dtype=np.float64)
    x, y, z = X[..., 0], X[..., 1], X[..., 2]
    rho = np.hypot(x, y)
    r = radial_map(np.arctan2(rho, z), k1, k2)
    s = np.divide(r, rho, out=np.zeros_like(rho), where=rho > 0.0)
    return np.stack([f * x * s + cx, a * f * y * s + cy], axis=-1)


def camera_centre(R, t) -> np.ndarray:
    return -np.asarray(R, dtype=np.float64).T @ np.asarray(t, dtype=np.float64)


def backproject_to_z(uv, R, t, kb, z) -> np.ndarray:
    """Points ``(N, 3)`` where the pixels' rays meet the plane ``z``; NaN rows
    for a pixel beyond the lens's valid field."""
    f, a, cx, cy, k1, k2 = (float(v) for v in kb)
    uv = np.asarray(uv, dtype=np.float64).reshape(-1, 2)
    rays = unproject_normalised((uv[:, 0] - cx) / f, (uv[:, 1] - cy) / (a * f), k1, k2)
    R = np.asarray(R, dtype=np.float64)
    d = rays @ R
    C = camera_centre(R, t)
    s = (float(z) - C[2]) / d[:, 2]
    return C + s[:, None] * d


def solve_pose(points, uv, kb, R0, t0, iters: int = 30):
    """The six-parameter PnP (lens fixed): ``(R, t, rms_px)`` from ``(R0, t0)``.
    ``rms_px`` is the RMS per image coordinate."""
    P = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    obs = np.asarray(uv, dtype=np.float64).reshape(-1, 2)

    def fun(p):
        return (project(P, rodrigues(p[:3]), p[3:], kb) - obs).ravel()

    p0 = np.concatenate([rvec_of(R0), np.asarray(t0, dtype=np.float64)])
    res = lm.solve(fun, p0, np.full(6, 1e-6), max_iter=iters, rel_tol=1e-12)
    R, t = rodrigues(res.p[:3]), res.p[3:].copy()
    return R, t, math.sqrt(res.data_cost / max(obs.size, 1))


def rigid_fit_2d(A, B):
    """``(R 2×2, t, rms)``: the least-squares rotation + translation mapping the
    points ``A`` onto ``B`` (no scale, no mirror) and the RMS distance left."""
    A = np.asarray(A, dtype=np.float64).reshape(-1, 2)
    B = np.asarray(B, dtype=np.float64).reshape(-1, 2)
    ca, cb = A.mean(axis=0), B.mean(axis=0)
    a, b = A - ca, B - cb
    psi = math.atan2(float(np.sum(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0])), float(np.sum(a * b)))
    R = np.array([[math.cos(psi), -math.sin(psi)], [math.sin(psi), math.cos(psi)]])
    t = cb - R @ ca
    rms = math.sqrt(float(np.mean(np.sum(np.square(A @ R.T + t - B), axis=1))))
    return R, t, rms


def rot_z(psi) -> np.ndarray:
    c, s = math.cos(psi), math.sin(psi)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def angle_between(Ra, Rb) -> float:
    """The rotation angle (rad) between two rotation matrices."""
    c = (float(np.trace(np.asarray(Ra) @ np.asarray(Rb).T)) - 1.0) / 2.0
    return math.acos(max(-1.0, min(1.0, c)))


def contract_axis() -> np.ndarray:
    """The contract camera's optical axis (unit vector from ``LENS_F`` to ``AIM_F``)."""
    v = np.asarray(_sf.AIM_F, dtype=np.float64) - np.asarray(_sf.LENS_F, dtype=np.float64)
    return v / np.linalg.norm(v)


def split_along_across(v, axis=None):
    """``(along, across)`` lengths of ``v`` against the contract's optical axis."""
    u = contract_axis() if axis is None else np.asarray(axis, dtype=np.float64)
    v = np.asarray(v, dtype=np.float64)
    along = float(v @ u)
    return abs(along), float(np.linalg.norm(v - along * u))


def lens_against_tower(lens_w, tilt, base_axis_x: float, tower_pose):
    """``(along, across)``: a solved lens position in W against the lens the
    tower pose carries (``scene_fixture.lens_w``), split along the contract's
    optical axis. The tower, its fixture tags and ``LENS_F`` are rigid to the
    arm's base, which W holds turned by the mat slope ``tilt = (ta, tb)``
    (S6.1), so the solved lens is taken back into the arm's frame first;
    compared in W instead, every reading would carry the lens height times
    the slope (2 mm at a slope of 0.2°, 11.5 mm at the arm check's 0.02 gate)."""
    Rt = tilt_rotation(*tilt)
    P0 = np.array([base_axis_x, 0.0, _sf.MAT_THICKNESS_M])
    L_a = P0 + Rt.T @ (np.asarray(lens_w, dtype=np.float64) - P0)
    ref = np.asarray(_sf.lens_w(base_axis_x, tower_pose), dtype=np.float64)
    return split_along_across(L_a - ref)


def contract_camera(base_axis_x: float):
    """``(R, t)`` of the contract camera in W: at ``LENS_F``, looking at
    ``AIM_F``, image x = axis × (0, 0, 1), image y (down) = axis × image x."""
    off = np.array([base_axis_x, 0.0, 0.0])
    L = np.asarray(_sf.LENS_F, dtype=np.float64) + off
    fwd = contract_axis()
    right = np.cross(fwd, [0.0, 0.0, 1.0])
    right /= np.linalg.norm(right)
    down = np.cross(fwd, right)
    R = np.stack([right, down, fwd])
    return R, -R @ L


def kb_tuple(kb: dict):
    return (float(kb['f']), float(kb['aspect']), float(kb['cx']), float(kb['cy']),
            float(kb['k1']), float(kb['k2']))


# ── the mat in its frames ────────────────────────────────────────────────────
def field_model_corners(spec, tag_ids, scale: float = 1.0) -> np.ndarray:
    """``(n, 4, 3)`` model corners in M of the field tags ``tag_ids``."""
    out = []
    for tid in tag_ids:
        cx, cy = spec['mat_tags_f'][tid]
        out.append([(scale * x, scale * y, 0.0) for x, y in _sf.tag_corners_xy(cx, cy, _sf.MAT_TAG_SIZE_M)])
    return np.asarray(out, dtype=np.float64).reshape(-1, 4, 3)


def fixture_model_corners_m() -> np.ndarray:
    """``(8, 3)``: the nominal fixture-tag corners in M at the contract place
    (M ≈ F), on the fixture plane."""
    pts = np.asarray(_sf.fixture_corners_w(0.0), dtype=np.float64)
    return np.hstack([pts, np.full((8, 1), FIXTURE_Z_M)])


def w_from_m(base_axis_x: float, mat_pose) -> np.ndarray:
    """``T_W←M0``: rotation ``dpsi`` about z, translation
    ``(base_axis_x + dx, dy, MAT_THICKNESS_M)``."""
    dx, dy, dpsi = (float(v) for v in mat_pose)
    T = np.eye(4)
    T[:3, :3] = rot_z(dpsi)
    T[:3, 3] = (base_axis_x + dx, dy, _sf.MAT_THICKNESS_M)
    return T


def _inv(T):
    out = np.eye(4)
    out[:3, :3] = T[:3, :3].T
    out[:3, 3] = -T[:3, :3].T @ T[:3, 3]
    return out


# ── result ───────────────────────────────────────────────────────────────────
@dataclass
class MatCheckResult:
    """``ok`` with ``T_cam_to_w`` (4×4) and the readings, or a refusal ``code``."""

    ok: bool
    code: str
    message_de: str
    notes: tuple = ()
    target_seen: bool = False
    T_cam_to_w: np.ndarray | None = None
    rms_px: float | None = None
    tags_seen: int = 0
    sharpness: float | None = None
    fixture_jitter_px: float | None = None
    fixture_fit_mm: float | None = None
    mat_shift_mm: float | None = None
    mat_turn_deg: float | None = None
    lens_offset_mm: float | None = None
    lens_along_mm: float | None = None
    lens_drift_mm: float | None = None
    camera_turn_deg: float | None = None
    worst_tag_px: float | None = None
    stamp: float = 0.0
    details: dict = field(default_factory=dict)

    @property
    def notes_de(self) -> tuple:
        return tuple(_t.BY_CODE[n] for n in self.notes)

    def to_json(self) -> dict:
        out = {k: getattr(self, k) for k in (
            'ok', 'code', 'message_de', 'target_seen', 'rms_px', 'tags_seen', 'sharpness', 'fixture_jitter_px',
            'fixture_fit_mm', 'mat_shift_mm', 'mat_turn_deg', 'lens_offset_mm', 'lens_along_mm', 'lens_drift_mm',
            'camera_turn_deg', 'stamp')}
        out['notes'] = list(self.notes)
        out['T_cam_to_w'] = None if self.T_cam_to_w is None else np.asarray(self.T_cam_to_w).tolist()
        return out


def _refuse(code, **kw):
    mm = kw.pop('mm', None)
    msg = _t.tower_displaced_de(mm) if code == 'tower_displaced' else _t.BY_CODE[code]
    return MatCheckResult(ok=False, code=code, message_de=msg, **kw)


# ── step 2 … 5 ───────────────────────────────────────────────────────────────
@dataclass
class Measurement:
    """What steps 2 to 4 measured; ``code`` is the first refusal they found
    (``'ok'`` when none)."""

    code: str
    notes: list
    R_cm: np.ndarray            # camera from M (the PnP's pose)
    t_cm: np.ndarray
    rms_px: float
    worst_tag_px: float
    tags_used: int
    G: np.ndarray               # 4×4: current M -> the arm check's M (rotation about z, xy shift)
    fixture_fit_m: float
    mat_shift_m: float
    mat_turn_rad: float
    T_w_m0: np.ndarray          # 4×4
    T_cam_to_w: np.ndarray      # 4×4 (with G)
    lens_w: np.ndarray
    lens_across_m: float
    lens_along_m: float
    lens_drift_m: float
    turn_rad: float
    per_tag_px: dict


def measure(field_medians: dict, fixture_medians: dict, calibration: dict, arm_key: str) -> Measurement:
    """Steps 2 to 4 on the median corners (detector order) of the visible field
    and fixture tags, against a stored arm check."""
    spec = _sf.ARMS[arm_key]
    ax = BASE_AXIS_X[arm_key]
    kb = kb_tuple(calibration['kb'])
    s = float(calibration['print_scale'])
    R0 = rodrigues(calibration['camera_pose_mat']['rvec'])
    t0 = np.asarray(calibration['camera_pose_mat']['t'], dtype=np.float64)

    ids = sorted(field_medians)
    model = field_model_corners(spec, ids, s)
    nominal = project(model, R0, t0, kb)
    obs = np.stack([_tags.match_corners(field_medians[tid], nominal[i]) for i, tid in enumerate(ids)])

    use = np.ones(len(ids), dtype=bool)
    notes: list = []

    def fit(mask):
        R, t, _ = solve_pose(model[mask], obs[mask], kb, R0, t0)
        per = np.linalg.norm(project(model, R, t, kb) - obs, axis=2).mean(axis=1)
        return R, t, per

    R, t, per = fit(use)
    worst = float(per[use].max())
    code = 'ok'
    if worst > TAG_DROP_PX:
        use[int(np.argmax(np.where(use, per, -1.0)))] = False
        notes.append('tag_dropped')
        R, t, per = fit(use)
        if float(per[use].max()) > TAG_DROP_PX:
            code = 'mat_not_flat'
    r = (project(model[use], R, t, kb) - obs[use]).reshape(-1, 2)
    rms = math.sqrt(float(np.mean(np.square(r))))
    if code == 'ok' and rms > RMS_MAX_PX:
        code = 'inconsistent'

    # step 3: the fixture tags
    fx_model = fixture_model_corners_m()
    fx_nominal = project(fx_model, R, t, kb)
    fx_obs = np.vstack([_tags.match_corners(fixture_medians[tid], fx_nominal[4 * k:4 * k + 4])
                        for k, tid in enumerate(_tags.FIXTURE_IDS)])
    F1 = backproject_to_z(fx_obs, R, t, kb, FIXTURE_Z_M)[:, :2]
    F0 = np.asarray(calibration['fixture_xy_mat'], dtype=np.float64)
    if not np.all(np.isfinite(F1)):
        G2, g_t, g_rms = np.eye(2), np.zeros(2), math.inf
    else:
        G2, g_t, g_rms = rigid_fit_2d(F1, F0)
    G = np.eye(4)
    G[:2, :2] = G2
    G[:2, 3] = g_t
    zc = np.array([0.5 * (spec['zone_inner_m'] + spec['zone_outer_m']), 0.0])
    shift = float(np.linalg.norm(G2 @ zc + g_t - zc))
    turn = abs(math.atan2(G2[1, 0], G2[0, 0]))
    if code == 'ok':
        if g_rms > FIXTURE_FIT_MAX_M:
            code = 'needs_arm_check/fixture_changed'
        elif shift > MAT_MISPLACED_M or turn > math.radians(MAT_MISPLACED_DEG):
            code = 'mat_misplaced'
    if shift > MAT_MOVED_M or turn > math.radians(MAT_MOVED_DEG):
        notes.append('mat_moved')

    # step 4: the lens
    T_w_m0 = w_from_m(ax, calibration['mat_pose'])
    T_m_cam = _inv(np.vstack([np.hstack([R, t[:, None]]), [0.0, 0.0, 0.0, 1.0]]))
    T_cam_to_w = T_w_m0 @ G @ T_m_cam
    lens = T_cam_to_w[:3, 3].copy()
    along, across = lens_against_tower(lens, calibration['tilt'], ax, calibration['tower_pose'])
    drift = float(np.linalg.norm(lens - np.asarray(calibration['lens_w'], dtype=np.float64)))
    R_w_cam_ac = (T_w_m0[:3, :3] @ R0.T)
    cam_turn = angle_between(T_cam_to_w[:3, :3], R_w_cam_ac)
    if code == 'ok':
        if across > _sf.LENS_FOLLOW_MAX_M or drift > _sf.LENS_FOLLOW_MAX_M:
            code = 'tower_displaced'
        elif cam_turn > math.radians(CAMERA_MOVED_DEG):
            code = 'needs_arm_check/camera_moved'
    if drift > FOLLOW_NOTE_M or cam_turn > math.radians(FOLLOW_NOTE_DEG):
        notes.append('camera_followed')
    return Measurement(
        code=code, notes=notes, R_cm=R, t_cm=t, rms_px=rms, worst_tag_px=worst, tags_used=int(use.sum()),
        G=G, fixture_fit_m=g_rms, mat_shift_m=shift, mat_turn_rad=turn, T_w_m0=T_w_m0, T_cam_to_w=T_cam_to_w,
        lens_w=lens, lens_across_m=across, lens_along_m=along, lens_drift_m=drift, turn_rad=cam_turn,
        per_tag_px={tid: float(per[i]) for i, tid in enumerate(ids)})


# ── step 1 and the whole check ───────────────────────────────────────────────
def _seen(frames, tid) -> int:
    return sum(1 for fr in frames if tid in fr.tags)


def run(frames, store, arm_spec, *, profile_id: str, profile, camera_streaming: bool, now=None) -> MatCheckResult:
    """The mat check on (usually three) :class:`tags.Frame` looks.

    ``store`` answers ``read(profile_id, image_size)`` (``store.Store``);
    ``profile`` is ``StudioCamera.profile()`` (``None`` before the arm
    container's statement), ``camera_streaming`` whether a camera state
    ``streaming`` arrived."""
    stamp = time.time() if now is None else float(now)
    arm_key = next((k for k, v in _sf.ARMS.items() if v is arm_spec), None) or _sf.arm_key(profile_id)
    if profile is None:
        return _refuse('profile_unknown', stamp=stamp)
    if profile == 'legacy' or profile != _sf.FIXTURE_ID:
        return _refuse('camera_not_tower', stamp=stamp)
    frames = list(frames or ())
    if not camera_streaming or not frames:
        return _refuse('camera_missing', stamp=stamp)
    size = (int(_sf.CAPTURE['width']), int(_sf.CAPTURE['height']))
    if any((int(fr.width), int(fr.height)) != size for fr in frames):
        return _refuse('image_size', stamp=stamp)
    if float(np.median([float(fr.mean_grey) for fr in frames])) < TOO_DARK_GREY:
        return _refuse('too_dark', stamp=stamp)

    majority = (len(frames) + 1) // 2
    ids = {tid for fr in frames for tid in fr.tags}
    seen = {tid for tid in ids if _seen(frames, tid) >= majority}
    groups = _tags.classify(seen, arm_key)
    every = _tags.classify(ids, arm_key)
    target_seen = bool(every['target'] or every['foreign_target'])
    if len(groups['foreign_mat']) >= WRONG_MAT_TAGS and len(groups['field']) < WRONG_MAT_TAGS:
        return _refuse('wrong_mat', stamp=stamp)
    if target_seen:
        return _refuse('target_attached', stamp=stamp, target_seen=True)
    if len(groups['fixture']) < len(_tags.FIXTURE_IDS):
        return _refuse('fixture_hidden', stamp=stamp)
    if len(groups['field']) < MIN_FIELD_TAGS:
        return _refuse('tags_hidden', stamp=stamp, tags_seen=len(groups['field']))
    read = store.read(profile_id, size)
    if read.status == 'none':
        return _refuse('needs_arm_check/none', stamp=stamp)
    if read.status != 'ok':
        return _refuse('needs_arm_check/contract_changed', stamp=stamp)
    cal = read.data
    if not cal.get('target_removed'):
        return _refuse('target_not_confirmed', stamp=stamp)

    looks = [{tid: fr.tags[tid].corners for tid in fr.tags if tid in seen} for fr in frames]
    # One corner order per tag before the median: match every look to the first.
    ref = {}
    for lk in looks:
        for tid, c in lk.items():
            ref.setdefault(tid, np.asarray(c, dtype=np.float64).reshape(4, 2))
    aligned = [{tid: _tags.match_corners(c, ref[tid]) for tid, c in lk.items()} for lk in looks]
    med = _tags.median_corners(aligned)
    field_med = {tid: med[tid][0] for tid in groups['field']}
    fixture_med = {tid: med[tid][0] for tid in groups['fixture']}
    margins = [fr.tags[tid].margin for fr in frames for tid in groups['field']
               if tid in fr.tags and fr.tags[tid].margin is not None]
    sharp = float(np.median(margins)) if margins else None
    jitter = 0.0
    for tid in groups['fixture']:
        for lk in aligned:
            if tid in lk:
                jitter = max(jitter, float(np.abs(lk[tid] - med[tid][0]).max()))

    m = measure(field_med, fixture_med, cal, arm_key)
    common = dict(
        notes=tuple(m.notes), stamp=stamp, rms_px=m.rms_px, tags_seen=len(groups['field']), sharpness=sharp,
        fixture_jitter_px=jitter, fixture_fit_mm=m.fixture_fit_m * 1000.0, mat_shift_mm=m.mat_shift_m * 1000.0,
        mat_turn_deg=math.degrees(m.mat_turn_rad), lens_offset_mm=m.lens_across_m * 1000.0,
        lens_along_mm=m.lens_along_m * 1000.0, lens_drift_mm=m.lens_drift_m * 1000.0,
        camera_turn_deg=math.degrees(m.turn_rad), worst_tag_px=m.worst_tag_px,
        details={'per_tag_px': m.per_tag_px, 'tags_used': m.tags_used})
    if m.code != 'ok':
        mm = max(m.lens_across_m, m.lens_drift_m) * 1000.0
        return _refuse(m.code, mm=mm, **common)
    return MatCheckResult(ok=True, code='ok', message_de=_t.OK_DE, T_cam_to_w=m.T_cam_to_w, **common)

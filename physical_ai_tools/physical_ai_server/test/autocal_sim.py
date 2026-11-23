#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""A simulated tower kit and its runtime chain, for the autocal tests (spec 40 WP-10).

Not a test module. It is the synthetic truth and runtime chain of the
architect's probe ``r3/mc6.py`` (run at placement B as ``mc8v``/``p01``),
re-written over the production modules, with its final options:

* the truth: the solver's link origins perturbed by 0.2 mm, joint zero
  offsets σ 0.6° (clipped at 1.5°), gravity droop from the URDF masses
  (``scene_fixture`` ``link_masses``, each mass scattered by 20 %) with a
  per-kit stiffness and 45 g of clip-on target at its mount during the arm
  check, backlash, encoder non-linearity (first and second harmonic), joint
  axis misalignment, a Kannala–Brandt camera with decentering at the contract
  pose with ``41`` H3's tolerance as 1σ (1.5 mm and 0.7° per axis), a lens
  inside ``41`` H1's band (``k1, k2`` drawn from [−0.15, 0.15]² until HFOV
  lies in 102–106°, DFOV in 117–130° and the valid field reaches the farthest
  image corner), a tilted, shifted, scaled mat, the tower placed on its
  plate within its tolerance with each fixture tag ±0.2 mm in its pocket,
  pixel noise 0.15 px;
* the arm check through ``autocal.arm_check`` (the capture rule, the solve,
  every gate), the mat and camera moved, the mat check through
  ``autocal.mat_check``;
* grasp targets drawn from the printed zone (``scene_fixture.in_zone``, the
  cut zone), seen by the camera, projected with the calibration, solved by
  the calibrated arm model of S6.1 (offsets, tilts, mat slope, lift, at most
  three fixed-point passes, a miss never refused) and arrived at with the
  raise-only trim of S6.6; the error is the fingertip's against the object.

Where it differs from the probe, on purpose: the fingertip is the contract's
``FINGERTIP_BELOW_EE_M`` (OMX 2.6 mm; the probe used the solver's 40 mm), a
tag not wholly inside the frame is not seen (the probe saw every corner), and
the mat is flat in W with the arm turned by its slope (S6.1; the probe sheared
the mat in the arm's frame — the same geometry to second order).

Random streams per kit: ``[seed, kit]`` for the truth and the arm check,
``[seed, kit, 1]`` the mat check, ``[seed, kit, 7]`` the 30 grasps (a fixed
number of draws per grasp), so the variants of one kit see the same kit and
the same targets.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import math

import numpy as np

from physical_ai_server.workflow import scene_fixture as sf
from physical_ai_server.workflow.autocal import arm_check, chain, mat_check, store as _store, tags
from physical_ai_server.workflow.autocal.camera_model import angle_of_radius, radial_map, valid_theta
from physical_ai_server.workflow.ik_solver import IKSolver
from physical_ai_server.workflow.v1_ik import V1IKSolver

TICK = 2.0 * math.pi / 4096.0
SIG_PX = 0.15
OBJECT_PX_SIG = 0.2
WIDTH, HEIGHT = 1920, 1080
OBJ_HEIGHT_M = 0.030          # the catalog's cube (object_catalog: wuerfel)
GRASP_DEPTH_M = 0.015
PROFILE = {'omx': 'omx_full', 'v1': 'v1_studio'}
REACH_OUTER_M = {'omx': 0.25, 'v1': 0.34}   # robot_profiles reach_outer_m
G = 9.81


def solver(kind):
    return IKSolver() if kind == 'omx' else V1IKSolver()


def tip(kind) -> float:
    return float(sf.FINGERTIP_BELOW_EE_M[kind])


# ── the true camera: Kannala–Brandt with decentering ─────────────────────────
@dataclass
class TrueCam:
    f: float
    a: float
    cx: float
    cy: float
    k1: float
    k2: float
    R: np.ndarray
    t: np.ndarray
    p1: float = 0.0
    p2: float = 0.0

    @property
    def lens(self) -> np.ndarray:
        return -self.R.T @ self.t

    def project(self, X) -> np.ndarray:
        Xc = np.asarray(X, dtype=np.float64) @ self.R.T + self.t
        rho = np.hypot(Xc[..., 0], Xc[..., 1])
        r = radial_map(np.arctan2(rho, Xc[..., 2]), self.k1, self.k2)
        s = np.divide(r, rho, out=np.ones_like(rho), where=rho > 1e-12)
        xn, yn = Xc[..., 0] * s, Xc[..., 1] * s
        if self.p1 or self.p2:
            r2 = xn * xn + yn * yn
            xn, yn = (xn + 2 * self.p1 * xn * yn + self.p2 * (r2 + 2 * xn * xn),
                      yn + self.p1 * (r2 + 2 * yn * yn) + 2 * self.p2 * xn * yn)
        return np.stack([self.f * xn + self.cx, self.a * self.f * yn + self.cy], axis=-1)

    def moved(self, dR=None, dL=None, f_scale=1.0):
        """The same lens turned by ``dR`` (about its own centre) and moved by ``dL``."""
        R = self.R if dR is None else dR @ self.R
        L = self.lens + (np.zeros(3) if dL is None else np.asarray(dL, dtype=np.float64))
        return replace(self, f=self.f * f_scale, R=R, t=-R @ L)


def lens_facts(f, a, cx, cy, k1, k2):
    """``(valid field, corner angle, HFOV deg, DFOV deg)`` on the 1920 × 1080 grid."""
    rc = max(math.hypot((u - cx) / f, (v - cy) / (a * f)) for u in (0.0, 1919.0) for v in (0.0, 1079.0))
    ang = lambda r: float(angle_of_radius(r, k1, k2))  # noqa: E731
    return (valid_theta(k1, k2), ang(rc), 2 * math.degrees(ang(959.5 / f)),
            2 * math.degrees(ang(math.hypot(959.5, 539.5) / f)))


def in_lens_band(f, a, cx, cy, k1, k2) -> bool:
    vt, ct, H, D = lens_facts(f, a, cx, cy, k1, k2)
    return (math.isfinite(ct) and math.isfinite(H) and math.isfinite(D) and vt >= ct
            and 102.0 <= H <= 106.0 and 117.0 <= D <= 130.0)


def visible(uv, b=6.0) -> bool:
    uv = np.asarray(uv).reshape(-1, 2)
    return bool(np.all((uv[:, 0] >= b) & (uv[:, 0] <= WIDTH - 1 - b) & (uv[:, 1] >= b) & (uv[:, 1] <= HEIGHT - 1 - b)))


# ── the kit ──────────────────────────────────────────────────────────────────
@dataclass
class Options:
    """The cell. Defaults: the WP-10 Monte Carlo (encoder non-linearity 0.2°,
    axis misalignment 0.4°, mat and camera moved 2 mm / 0.3° after the arm check)."""

    skew_deg: float = 0.4
    inl_deg: float = 0.2
    droop_scale: float = 1.0
    zero_scale: float = 1.0
    target_mass: float = 0.045
    cam_lens_m: float = 0.0015
    cam_rot_deg: float = 0.7
    decenter: float = 3e-4
    link_m: float = 0.0002
    tower_place: bool = True
    mat_move_m: float = 0.002
    mat_move_deg: float = 0.3
    cam_move_m: float = 0.002
    cam_move_deg: float = 0.3
    matcheck_hidden: float = 0.25
    noise: float = 1.0


@dataclass
class Kit:
    kind: str
    spec: dict
    ax: float
    geometry: chain.Geometry
    tilts: np.ndarray
    dzero: np.ndarray
    stiff: np.ndarray
    masses: list           # (frame index 0..4, kg, CoM) true
    masses_nom: list
    backlash: float
    inl: list
    cam: TrueCam
    mount: np.ndarray
    mat: dict
    fixture_w: np.ndarray  # (8, 3) true fixture corners
    extra_sag: dict = field(default_factory=dict)   # pose index -> radians added to the shaft (5)

    def inl_of(self, a) -> np.ndarray:
        """The encoder's non-linearity at the shaft angles ``a`` (5)."""
        return np.array([a1 * math.sin(a[j] + p1) + a2 * math.sin(2 * a[j] + p2)
                         for j, (a1, p1, a2, p2) in enumerate(self.inl)])

    def lash(self, g) -> np.ndarray:
        b = self.backlash
        return np.array([g.uniform(-b / 2, b / 2), g.uniform(-b / 4, b / 4), g.uniform(-b / 4, b / 4),
                         g.uniform(-b / 4, b / 4), g.uniform(-b / 2, b / 2)])


def _masses(kind):
    return [(k - 1, float(m), tuple(c)) for k, m, c in sf.ARMS[kind]['link_masses']]


def gravity_torques(kind, Q, masses, mount=None, target_mass=0.0) -> np.ndarray:
    """``(N, 5)``: ∂U/∂q for joints 2..4 (indices 1..3) on the NOMINAL chain, by
    central differences; ``mount`` + ``target_mass`` add the clip-on target."""
    Q = np.atleast_2d(np.asarray(Q, dtype=np.float64))
    h = 1e-5
    rows = []
    for j in (1, 2, 3):
        for sgn in (1.0, -1.0):
            P = Q.copy()
            P[:, j] += sgn * h
            rows.append(P)
    P = np.concatenate(rows)
    F = chain.frames_batch(kind, P)
    U = np.zeros(P.shape[0])
    for fi, m, c in masses:
        U += G * m * (F[:, fi, 2, 3] + F[:, fi, 2, :3] @ np.asarray(c))
    if target_mass > 0.0 and mount is not None:
        E = chain.ee_batch(kind, P)
        U += G * target_mass * (E[:, 2, 3] + E[:, 2, :3] @ mount[:3, 3])
    n = Q.shape[0]
    out = np.zeros((n, 5))
    for k, j in enumerate((1, 2, 3)):
        up, dn = U[(2 * k) * n:(2 * k + 1) * n], U[(2 * k + 1) * n:(2 * k + 2) * n]
        out[:, j] = (up - dn) / (2 * h)
    return out


def _perturbed_geometry(kind, rng, sigma):
    dorig = [rng.normal(0, sigma, 3) for _ in range(6)]
    nom = chain.NOMINAL[kind]
    if kind == 'omx':
        return chain.Geometry(kind='omx', origins=tuple(tuple(np.asarray(o) + d) for o, d in zip(nom.origins, dorig)))
    fixed = []
    for i, F in enumerate(nom.fixed):
        M = np.array(F, dtype=np.float64)
        M[:3, 3] += dorig[i]
        fixed.append(M)
    return chain.Geometry(kind='v1', fixed=tuple(fixed), l_tool=nom.l_tool + float(dorig[5][2]),
                          tool_lat=(float(dorig[5][0]), float(dorig[5][1])))


def sample_kit(kind, rng, opt: Options = None, *, dzero=None, tilts=None) -> Kit:
    opt = opt or Options()
    spec = sf.ARMS[kind]
    ax = chain.BASE_AXIS_X[kind]
    geometry = _perturbed_geometry(kind, rng, opt.link_m)
    zs = opt.zero_scale
    dz = np.clip(rng.normal(0, math.radians(0.6) * zs, 5), -math.radians(1.5) * zs, math.radians(1.5) * zs)
    if dzero is not None:
        dz = np.asarray(dzero, dtype=np.float64)
    dmax = math.radians(rng.uniform(0.5, 2.5) if kind == 'omx' else rng.uniform(0.3, 1.2)) * max(opt.droop_scale, 1e-9)
    masses_nom = _masses(kind)
    sol = solver(kind)
    q_ext = None
    r = REACH_OUTER_M[kind]
    while q_ext is None and r > 0.1:
        q_ext = sol.solve((ax + r, 0.0, sol.tool_tip_below_ee_m + 0.02 - sf.LIFT_M), roll=0.0)
        r -= 0.005
    tau = gravity_torques(kind, np.asarray(q_ext), masses_nom)[0]
    k = abs(tau[1]) / dmax
    kr4 = rng.uniform(0.35, 0.8)
    stiff = np.array([np.inf, k, k, kr4 * k, np.inf]) if opt.droop_scale > 0 else np.full(5, np.inf)
    masses = [(fi, m * float(np.exp(rng.normal(0, 0.2))), c) for fi, m, c in masses_nom]
    backlash = math.radians(0.25 if kind == 'omx' else 0.30)
    # camera: the contract pose with 41 H3's tolerance as 1 sigma per axis
    R0, t0 = mat_check.contract_camera(ax)
    L0 = -R0.T @ t0
    dR = chain.rodrigues(rng.normal(0, math.radians(opt.cam_rot_deg), 3))
    R = dR @ R0
    L = L0 + rng.normal(0, opt.cam_lens_m, 3)
    cam = TrueCam(f=1037.8 * (1 + rng.normal(0, 0.02)), a=1 + rng.normal(0, 0.001),
                  cx=959.5 + rng.normal(0, 15), cy=539.5 + rng.normal(0, 15), k1=0.0, k2=0.0, R=R, t=-R @ L,
                  p1=rng.normal(0, opt.decenter), p2=rng.normal(0, opt.decenter))
    rl = np.random.default_rng(rng.integers(0, 2**63))
    for tries in range(20000):
        k1, k2 = rl.uniform(-0.15, 0.15), rl.uniform(-0.15, 0.15)
        if in_lens_band(cam.f, cam.a, cam.cx, cam.cy, k1, k2):
            break
        if tries % 2000 == 1999:          # a focal length far out of the band: draw it again
            cam.f = 1037.8 * (1 + rl.normal(0, 0.01))
    cam.k1, cam.k2 = k1, k2
    mount = arm_check.mount_nominal(kind) @ _T(chain.rodrigues(rng.normal(0, math.radians(0.3), 3)),
                                               rng.normal(0, 0.0003, 3))
    ps = rng.normal(0, 0.002)
    mat = dict(s=(1 + ps, 1 + ps + rng.normal(0, 0.0005)), dxy=rng.normal(0, 0.0015, 2),
               dpsi=float(rng.normal(0, math.radians(0.3))), slope=rng.normal(0, math.radians(0.15), 2),
               h=0.0004 + float(rng.normal(0, 0.0005)), curl=0.0002)
    tl = rng.normal(0, math.radians(opt.skew_deg), 6) if opt.skew_deg > 0 else np.zeros(6)
    if tilts is not None:
        tl = np.asarray(tilts, dtype=np.float64)
    A = math.radians(opt.inl_deg)
    inl = [(A * rng.uniform(0.5, 1), rng.uniform(0, 6.283), 0.5 * A * rng.uniform(0.5, 1), rng.uniform(0, 6.283))
           for _ in range(5)]
    # the tower on its plate (41 H3) and each tag in its pocket
    F = np.asarray(sf.fixture_corners_w(ax), dtype=np.float64)
    if opt.tower_place:
        rt = np.random.default_rng(rng.integers(0, 2**63))
        tT = np.clip(rt.normal(0, 0.0005, 2), -0.001, 0.001)
        psiT = float(np.clip(rt.normal(0, math.radians(0.2)), -math.radians(0.5), math.radians(0.5)))
        c0 = F.mean(axis=0)
        RT = np.array([[math.cos(psiT), -math.sin(psiT)], [math.sin(psiT), math.cos(psiT)]])
        pocket = np.repeat(np.clip(rt.normal(0, 0.00007, (2, 2)), -0.0002, 0.0002), 4, axis=0)
        F = (F - c0) @ RT.T + c0 + tT + pocket
    fixture_w = np.hstack([F, np.full((8, 1), sf.FIXTURE_TAG_Z_M)])
    return Kit(kind=kind, spec=spec, ax=ax, geometry=geometry, tilts=tl, dzero=dz, stiff=stiff, masses=masses,
               masses_nom=masses_nom, backlash=backlash, inl=inl, cam=cam, mount=mount, mat=mat,
               fixture_w=fixture_w)


def _T(R, t):
    M = np.eye(4)
    M[:3, :3] = R
    M[:3, 3] = t
    return M


# ── what the kit looks like ──────────────────────────────────────────────────
def mat_points(kit: Kit, mat: dict, rng=None, ids=None, lift=None) -> dict:
    """``{tag_id: (4, 3)}`` true field-tag corners (the arm's base frame), the
    per-tag curl drawn from ``rng``; ``lift`` = ``{tag_id: metres}`` raises a card."""
    out = {}
    c, s = math.cos(mat['dpsi']), math.sin(mat['dpsi'])
    for tid in sorted(kit.spec['mat_tags_f'] if ids is None else ids):
        cx, cy = kit.spec['mat_tags_f'][tid]
        P = []
        for x, y in sf.tag_corners_xy(cx, cy, sf.MAT_TAG_SIZE_M):
            x0, y0 = x * mat['s'][0], y * mat['s'][1]
            xw = kit.ax + c * x0 - s * y0 + mat['dxy'][0]
            yw = s * x0 + c * y0 + mat['dxy'][1]
            P.append([xw, yw, mat['h'] + mat['slope'][0] * (xw - kit.ax) + mat['slope'][1] * yw])
        P = np.asarray(P)
        if rng is not None:
            P[:, 2] += rng.normal(0, mat['curl'])
        if lift and tid in lift:
            P[:, 2] += lift[tid]
        out[tid] = P
    return out


def surface(kit: Kit, mat: dict, x, y) -> float:
    return mat['h'] + mat['slope'][0] * (x - kit.ax) + mat['slope'][1] * y


def target_points(kit: Kit, kin) -> np.ndarray:
    """``(36, 3)`` true target corners at the true joint angles ``kin``."""
    E = chain.ee(kit.kind, kin, kit.tilts, kit.geometry)
    X = arm_check.target_model(kit.kind)[1] @ kit.mount[:3, :3].T + kit.mount[:3, 3]
    out = X @ E[:3, :3].T + E[:3, 3]
    out[:, 2] += sf.LIFT_M
    return out


def droop(kit: Kit, g, target=False) -> np.ndarray:
    """The joints' free-space sag at the goal ``g`` (with the target when asked)."""
    tau = gravity_torques(kit.kind, np.asarray(g) + kit.dzero, kit.masses,
                          kit.mount if target else None, 0.045 if target else 0.0)[0]
    return tau / kit.stiff


@dataclass
class ArmCheckInput:
    records: list
    looks: list
    commanded_records: list


def arm_check_inputs(kit: Kit, rng, opt: Options = None, target_mass=None) -> ArmCheckInput:
    """The records and looks the service would hand ``arm_check.run``."""
    opt = opt or Options()
    tm = opt.target_mass if target_mass is None else target_mass
    ids = arm_check.target_model(kit.kind)[0]
    records, cmd_records = [], []
    for i, q in enumerate(kit.spec['arm_check_poses_rad']):
        q = np.asarray(q, dtype=np.float64)
        tau = gravity_torques(kit.kind, q + kit.dzero, kit.masses, kit.mount, tm)[0]
        a = q - tau / kit.stiff + kit.lash(rng) + kit.extra_sag.get(i, 0.0)
        enc = np.round((a + kit.inl_of(a)) / TICK) * TICK
        uv = kit.cam.project(target_points(kit, a + kit.dzero)) + rng.normal(0, SIG_PX * opt.noise, (36, 2))
        target = {tid: uv[4 * k:4 * k + 4] for k, tid in enumerate(ids) if visible(uv[4 * k:4 * k + 4])}
        cap = arm_check.capturable(q, enc, len(target))
        records.append(arm_check.PoseRecord(i, tuple(q), tuple(enc), target if cap else None))
        cmd_records.append(arm_check.PoseRecord(i, tuple(q), tuple(q), target if cap else None))
    field = {tid: kit.cam.project(P) + rng.normal(0, SIG_PX * opt.noise, (4, 2))
             for tid, P in mat_points(kit, kit.mat, rng).items()}
    fx_uv = kit.cam.project(kit.fixture_w) + rng.normal(0, SIG_PX * opt.noise / math.sqrt(5), (8, 2))
    look = {tid: c for tid, c in field.items() if visible(c)}
    for k, tid in enumerate(tags.FIXTURE_IDS):
        if visible(fx_uv[4 * k:4 * k + 4]):
            look[tid] = fx_uv[4 * k:4 * k + 4]
    looks = [dict(look) for _ in records]
    return ArmCheckInput(records=records, looks=looks, commanded_records=cmd_records)


class FakeStore:
    """``store.Store``'s read for one in-memory calibration."""

    def __init__(self, cal, status='ok'):
        self.cal, self.status = cal, status

    def read(self, profile_id, image_size=None):
        return _store.Read(self.status, self.cal if self.status == 'ok' else None)


def mat_check_frames(kit: Kit, cam: TrueCam, mat: dict, rng, opt: Options = None, *, hidden=None, lift=None,
                     n=3, extra_tags=None, fixture_shift=None) -> list:
    """Three looks at the mat check: a quarter of the field tags hidden (as the
    probe), pixel noise per look."""
    opt = opt or Options()
    hide = rng.random(len(kit.spec['mat_tags_f'])) < opt.matcheck_hidden if hidden is None else hidden
    pts = mat_points(kit, mat, rng, lift=lift)
    F = kit.fixture_w.copy()
    if fixture_shift is not None:
        F[:, :2] += fixture_shift
    frames = []
    for _ in range(n):
        tg = {}
        for k, (tid, P) in enumerate(sorted(pts.items())):
            if hide[k]:
                continue
            uv = cam.project(P) + rng.normal(0, SIG_PX * opt.noise, (4, 2))
            if visible(uv):
                tg[tid] = tags.TagObs(uv, 80.0)
        fx = cam.project(F) + rng.normal(0, SIG_PX * opt.noise, (8, 2))
        for k, tid in enumerate(tags.FIXTURE_IDS):
            if visible(fx[4 * k:4 * k + 4]):
                tg[tid] = tags.TagObs(fx[4 * k:4 * k + 4], 80.0)
        for tid, uv in (extra_tags or {}).items():
            tg[tid] = tags.TagObs(np.asarray(uv), 80.0)
        frames.append(tags.Frame(WIDTH, HEIGHT, 120.0, tg))
    return frames


def moved(kit: Kit, rng, opt: Options):
    """The mat and the camera after the arm check (``moveA``: 2 mm / 0.3°)."""
    mat = dict(kit.mat)
    mat['dxy'] = kit.mat['dxy'] + rng.normal(0, opt.mat_move_m, 2)
    mat['dpsi'] = kit.mat['dpsi'] + float(rng.normal(0, math.radians(opt.mat_move_deg)))
    dR = chain.rodrigues(rng.normal(0, math.radians(opt.cam_move_deg), 3))
    cam = kit.cam.moved(dR=dR, dL=rng.normal(0, opt.cam_move_m, 3))
    return mat, cam


# ── the solve and its variants ───────────────────────────────────────────────
@dataclass
class Solved:
    model: arm_check.ArmCheckModel
    sol: object
    judged: dict
    arm_ok: bool
    cal: dict | None


def solve_arm_check(kit: Kit, prep, *, tilts=True, records=None) -> Solved:
    """The arm check's solve and gates; ``tilts=False`` solves without the tilt
    parameters (a teeth variant); ``records`` replaces the FIT records only —
    the hold-out stays the measured one, exactly as the probe built its
    commanded-joints variant (``mc6`` ``joints=cmd``: ``Est6`` on the commanded
    poses, the hold-out projected from the encoders)."""
    if records is not None:
        prep = replace(prep, fit=tuple(r for k, r in enumerate(_captured(kit, records))
                                       if k % arm_check.HOLDOUT_EVERY != arm_check.HOLDOUT_EVERY - 1))
    counts_ok = (len(prep.captured) >= arm_check.MIN_POSES and len(prep.fit) >= 6
                 and len(prep.field_ids) >= arm_check.MIN_FIELD_TAGS
                 and all(prep.fixture_frames[t] >= arm_check.MIN_TAG_FRAMES for t in tags.FIXTURE_IDS))
    if len(prep.fit) < 6:
        return Solved(None, None, None, False, None)
    model = arm_check.build_model(prep)
    if not tilts:
        model.free[arm_check.S_TILT] = False
    sol = model.solve()
    j = arm_check.judge(prep, model, sol)
    ok = counts_ok and arm_check.first_failure(j) is None
    cal = None
    if j['tower_pose'] is not None:
        env = arm_check.droop_envelope(prep.records)
        cal = arm_check.calibration_dict(prep, model, sol, j, env, None, (), profile_id=PROFILE[kit.kind],
                                         camera_profile_id='sim', image_size=(WIDTH, HEIGHT),
                                         created_utc='2026-11-20T00:00:00Z')
        cal['target_removed'] = True
    return Solved(model, sol, j, ok, cal)


def _captured(kit, records):
    ids = kit.spec['target_tag_ids']
    return tuple(r for r in records if r.target is not None
                 and sum(1 for t in ids if t in r.target) >= arm_check.CAPTURE_MIN_TARGET_TAGS)


# ── the calibrated arm (S6.1) and the grasp ──────────────────────────────────
class CalibratedArm:
    """The arm model of S6.1 on a calibration dict: ``chain`` at ``q + d`` with
    the axis tilts, lifted, tilted into W about ``P0``; ``solve`` = the base
    solve plus at most three fixed-point passes, the best pose with its miss."""

    def __init__(self, kind, cal):
        self.kind = kind
        self.base = solver(kind)
        self.ax = chain.BASE_AXIS_X[kind]
        self.d = np.asarray(cal['joint_offsets_rad'], dtype=np.float64)
        self.tilts = np.asarray(cal['axis_tilts_rad'], dtype=np.float64)
        self.Rt = chain.tilt_rotation(*cal['tilt'])
        self.P0 = np.array([self.ax, 0.0, sf.MAT_THICKNESS_M])
        self.tip = tip(kind)
        lims = list(self.base.joint_limits)
        if kind == 'omx':
            lo, hi = (math.radians(v) for v in sf.ARM_OMX['joint1_window_deg'])
            lims[0] = (max(lims[0][0], lo), min(lims[0][1], hi))
        self.limits = lims

    def to_solver(self, p_w):
        p_a = self.P0 + self.Rt.T @ (np.asarray(p_w) - self.P0)
        return p_a - np.array([0.0, 0.0, sf.LIFT_M])

    def ee_w(self, q):
        E = chain.ee(self.kind, np.asarray(q)[:5] + self.d, self.tilts)
        p = E[:3, 3] + np.array([0.0, 0.0, sf.LIFT_M])
        return self.Rt @ E[:3, :3], self.P0 + self.Rt @ (p - self.P0)

    def tip_w(self, q):
        R, p = self.ee_w(q)
        if self.kind == 'omx':
            return p + R[:, 0] * self.tip
        return p

    def solve(self, target_ee_w, roll):
        """``(q or None, miss_m)``."""
        target = np.asarray(target_ee_w, dtype=np.float64)
        aim = target.copy()
        qb = self.base.solve(tuple(self.to_solver(aim)), roll=roll)
        if qb is None:
            return None, math.inf
        best_q = np.asarray(qb, dtype=np.float64) - self.d
        best = float(np.linalg.norm(self.ee_w(best_q)[1] - target))
        q = best_q
        for _ in range(3):
            if best < 5e-5:
                break
            aim = aim - (self.ee_w(q)[1] - target)
            qb = self.base.solve(tuple(self.to_solver(aim)), roll=roll)
            if qb is None:
                break
            q = np.asarray(qb, dtype=np.float64) - self.d
            miss = float(np.linalg.norm(self.ee_w(q)[1] - target))
            if miss < best:
                best_q, best = q, miss
        if any(not (lo - 1e-9 <= v <= hi + 1e-9) for v, (lo, hi) in zip(best_q, self.limits)):
            return None, best
        return best_q, best


def zone_target(spec, rg):
    """A target drawn uniformly over the printed (cut) zone: ``(r, az)``."""
    zin, zout = spec['zone_inner_m'], spec['zone_outer_m']
    lo, hi = spec['zone_sector_deg']
    while True:
        r = math.sqrt(zin ** 2 + rg.uniform(0.0, 1.0) * (zout ** 2 - zin ** 2))
        az = math.radians(lo + rg.uniform(0.0, 1.0) * (hi - lo))
        if sf.in_zone(spec, r * math.cos(az), r * math.sin(az)):
            return r, az


@dataclass
class Grasps:
    xy_mm: list = field(default_factory=list)
    none: int = 0
    skipped_bound: int = 0
    skipped_lower: int = 0
    largest_miss_m: float = 0.0
    gates_ok: bool | None = None


def grasps(kit: Kit, cal: dict, T_cam_to_w, cam: TrueCam, mat: dict, env, seed, trial, n=30) -> Grasps:
    """``n`` grasps of the cube in the printed zone through the calibrated chain."""
    out = Grasps()
    rg = np.random.default_rng([seed, trial, 7])
    arm = CalibratedArm(kit.kind, cal)
    kb = mat_check.kb_tuple(cal['kb'])
    R_wc = np.asarray(T_cam_to_w)[:3, :3]
    C = np.asarray(T_cam_to_w)[:3, 3]
    R_cw = R_wc.T
    t_cw = -R_cw @ C
    lim = np.minimum(2.0 * np.asarray(env) + 0.010, 0.15)
    z_top = sf.MAT_THICKNESS_M + OBJ_HEIGHT_M
    z_tip = sf.MAT_THICKNESS_M + OBJ_HEIGHT_M - GRASP_DEPTH_M
    for _ in range(n):
        r, az = zone_target(kit.spec, rg)
        x, y = kit.ax + r * math.cos(az), r * math.sin(az)
        zs = surface(kit, mat, x, y) + rg.normal(0, mat['curl'])
        h_obj = OBJ_HEIGHT_M + rg.normal(0, 0.0003)
        uv = cam.project(np.array([[x, y, zs + h_obj]])) + rg.normal(0, OBJECT_PX_SIG, (1, 2))
        roll = rg.uniform(-math.pi / 2, math.pi / 2)
        lash_draw = kit.lash(rg)
        trim_draw = rg.uniform(-2 * TICK, 2 * TICK, 3)
        P = mat_check.backproject_to_z(uv, R_cw, t_cw, kb, z_top)[0]
        if not np.all(np.isfinite(P)):
            out.none += 1
            continue
        q, miss = arm.solve((P[0], P[1], z_tip + arm.tip), roll)
        if q is None:
            out.none += 1
            continue
        out.largest_miss_m = max(out.largest_miss_m, miss)
        a0 = q - droop(kit, q) + lash_draw
        r0 = a0 + kit.inl_of(a0)
        enc0 = np.round(r0 / TICK) * TICK
        e = q - enc0
        e[0] = e[4] = 0.0
        rf = r0.copy()
        over = not np.all(np.abs(e[1:4]) <= lim[1:4])
        lowers = float(arm.tip_w(enc0 + e)[2] - arm.tip_w(enc0)[2]) < -0.0005
        if not over and not lowers:
            rf[1:4] = q[1:4] + trim_draw
        else:
            out.skipped_bound += int(over)
            out.skipped_lower += int(lowers and not over)
        kin = (rf - kit.inl_of(rf)) + kit.dzero
        tcp = chain.tcp_batch(kit.kind, kin, kit.tilts, tip=tip(kit.kind), geometry=kit.geometry)[0]
        out.xy_mm.append(math.hypot(tcp[0] - x, tcp[1] - y) * 1000.0)
    return out


# ── one kit through the whole chain ──────────────────────────────────────────
@dataclass
class KitRun:
    accepted: bool
    arm_ok: bool
    mat_ok: bool
    mat_code: str
    arm_code: str | None
    main: Grasps | None
    variants: dict


def run_kit(kind, seed, trial, opt: Options = None, variants=()) -> KitRun:
    """The arm check, the moved mat and camera, the mat check and 30 grasps;
    ``variants`` ⊂ {'notilt', 'nocomp', 'cmd'} run the teeth on the same kit,
    every one counted without a gate."""
    opt = opt or Options()
    rng = np.random.default_rng([seed, trial])
    kit = sample_kit(kind, rng, opt)
    inp = arm_check_inputs(kit, rng, opt)
    prep = arm_check.prepare(kind, inp.records, inp.looks)
    env = arm_check.droop_envelope(prep.records)
    rm = np.random.default_rng([seed, trial, 1])
    mat1, cam1 = moved(kit, rm, opt)
    frames = mat_check_frames(kit, cam1, mat1, rm, opt)
    out_v = {}
    s = solve_arm_check(kit, prep)
    mat_ok, mat_code, main = False, 'no_calibration', None
    if s.cal is not None:
        res = mat_check.run(frames, FakeStore(s.cal), sf.ARMS[kind], profile_id=PROFILE[kind], profile='tower_v1',
                            camera_streaming=True)
        mat_ok, mat_code = res.ok, res.code
        meas = _measure(frames, s.cal, kind)
        if meas is not None:
            main = grasps(kit, s.cal, meas.T_cam_to_w, cam1, mat1, env, seed, trial)
            if 'nocomp' in variants:          # T_W←M0 · T_M←cam, without G
                T_m_cam = np.eye(4)
                T_m_cam[:3, :3] = meas.R_cm.T
                T_m_cam[:3, 3] = -meas.R_cm.T @ meas.t_cm
                out_v['nocomp'] = grasps(kit, s.cal, meas.T_w_m0 @ T_m_cam, cam1, mat1, env, seed, trial)
    for name in variants:
        if name == 'nocomp':
            continue
        sv = (solve_arm_check(kit, prep, tilts=False) if name == 'notilt'
              else solve_arm_check(kit, prep, records=inp.commanded_records))
        if sv.cal is None:
            out_v[name] = None
            continue
        mv = _measure(frames, sv.cal, kind)
        g = grasps(kit, sv.cal, mv.T_cam_to_w, cam1, mat1, env, seed, trial) if mv is not None else None
        if name == 'cmd' and g is not None:
            r = mat_check.run(frames, FakeStore(sv.cal), sf.ARMS[kind], profile_id=PROFILE[kind],
                              profile='tower_v1', camera_streaming=True)
            g.gates_ok = bool(sv.arm_ok and r.ok)
        out_v[name] = g
    return KitRun(accepted=bool(s.arm_ok and mat_ok), arm_ok=s.arm_ok, mat_ok=mat_ok, mat_code=mat_code,
                  arm_code=None if s.judged is None else arm_check.first_failure(s.judged), main=main,
                  variants=out_v)


def _measure(frames, cal, kind):
    """``mat_check.measure`` on the frames' medians (whatever its verdict)."""
    seen = {}
    for fr in frames:
        for tid, ob in fr.tags.items():
            seen.setdefault(tid, []).append(ob.corners)
    groups = tags.classify([t for t, v in seen.items() if len(v) >= 2], kind)
    if len(groups['fixture']) < 2 or len(groups['field']) < 4:
        return None
    med = {t: np.median(np.stack(v), axis=0) for t, v in seen.items()}
    return mat_check.measure({t: med[t] for t in groups['field']}, {t: med[t] for t in groups['fixture']},
                             cal, kind)


# ── an ideal calibration of a kit (the mat check's constructed inputs) ───────
def ideal_calibration(kit: Kit, cam: TrueCam = None) -> dict:
    """The arm check a perfect solve would store for a kit whose mat is flat
    and unscaled in one axis (``slope`` 0, ``s[0] == s[1]``) and whose camera
    has no decentering: everything taken from the truth."""
    cam = kit.cam if cam is None else cam
    dx, dy = (float(v) for v in kit.mat['dxy'])
    dpsi = float(kit.mat['dpsi'])
    T_wm = mat_check.w_from_m(kit.ax, (dx, dy, dpsi))
    R_cm = cam.R @ T_wm[:3, :3]
    t_cm = cam.R @ T_wm[:3, 3] + cam.t
    F = kit.fixture_w[:, :2]
    rel = F - (kit.ax + dx, dy)
    c, s = math.cos(-dpsi), math.sin(-dpsi)
    xy_m = rel @ np.array([[c, -s], [s, c]]).T
    psi, tx, ty, rms = sf.tower_pose_fit(F, kit.ax)
    return {
        'v': 1, 'fixture_id': sf.FIXTURE_ID, 'contract_hash': sf.contract_hash(), 'profile_id': PROFILE[kit.kind],
        'camera_profile_id': 'sim', 'image_size': [WIDTH, HEIGHT], 'created_utc': '2026-11-20T00:00:00Z',
        'kb': {'f': cam.f, 'aspect': cam.a, 'cx': cam.cx, 'cy': cam.cy, 'k1': cam.k1, 'k2': cam.k2},
        'print_scale': float(kit.mat['s'][0]), 'joint_offsets_rad': [0.0] * 5, 'axis_tilts_rad': [0.0] * 6,
        'tilt': [0.0, 0.0], 'mat_pose': [dx, dy, dpsi],
        'camera_pose_mat': {'rvec': list(chain.rvec_of(R_cm)), 't': list(t_cm)},
        'lens_w': list(cam.lens), 'fixture_xy_mat': xy_m.tolist(), 'fixture_px': cam.project(kit.fixture_w).tolist(),
        'mount': {'rvec': [0.0, 0.0, 0.0], 't': [0.0, 0.0, 0.0]}, 'droop_envelope_rad': [0.0] * 5,
        'tower_pose': [psi, tx, ty], 'tower_fit_rms_m': rms, 'target_removed': True,
        'droop_warning': None, 'notes': [],
        'quality': {'poses_used': 20, 'rms_px': 0.1, 'holdout_rms_px': 0.1, 'holdout_p95_mm': 0.1,
                    'field_tags': 14, 'sharpness': None},
    }


def clean_kit(kind, seed=0, **kw) -> Kit:
    """A kit for the mat check's constructed inputs: flat, isotropic mat, no
    decentering, the tower at its contract place (``kw`` overrides Options)."""
    opt = replace(Options(), decenter=0.0, tower_place=False, **kw)
    kit = sample_kit(kind, np.random.default_rng([seed, 99]), opt)
    kit.mat['slope'] = np.zeros(2)
    kit.mat['s'] = (kit.mat['s'][0], kit.mat['s'][0])
    kit.mat['h'] = sf.MAT_THICKNESS_M
    return kit

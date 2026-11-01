#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Commissioning steps (plan v6.13 §6.1–§6.5): lens, board solve, world check,
robot registration and touch-off, and the station YAML fields.

Pure numpy + OpenCV, no rclpy and no torque: the torque side of a commission is
``commission_worker.py``'s. A :class:`Commission` holds one commission's data in
memory; the node feeds it frames and taps. The board solve reuses
``CalibrationManager``'s helpers through an instance built with the commission's
staging ``root`` and the released layout's board pose.
"""

import math

import numpy as np

from physical_ai_server.station import check as C
from physical_ai_server.station import layout_data
from physical_ai_server.station import store
from physical_ai_server.station import texts_de as T

# §6.1 lens
F0_PX = 1100.0              # provisional f₀ until bench gate B7
LENS_IDS = tuple(range(100, 117))
LENS_MIN_VIEWS = 25
LENS_MIN_CORNERS = 12
LENS_MIN_X_POSITIONS = 4
LENS_MIN_Y_POSITIONS = 3
LENS_MIN_CELLS = 10
LENS_AREA_RATIO = 1.5
LENS_TILT_SPREAD_DEG = 15.0
DUP_DISPLACEMENT_PX = 10.0
DUP_AREA = 0.05
DUP_NORMAL_DEG = 5.0
FIT_RMS_PX = 1.0
HELDOUT_RMS_PX = 1.5
HELDOUT_MAX_PX = 4.0
ROUNDTRIP_PX = 0.1
F_BAND = 0.15
FXFY_TOL = 0.02
C_TOL_PX = 80.0
# §6.2 board
BOARD_BURST = 8
BOARD_MIN_CORNERS = 20
BOARD_MAX_REPROJ_PX = 1.0
BOARD_MIN_PASS = 4
CAMERA_MIN_Z_M = 0.300
# §6.3 touch
TAP_TILT_DEG = 12.0
WRONG_DOT_MM = 40.0
REG_RMS_MM = 2.0
REG_MAX_MM = 3.5
REG_SHIFT_MM = 8.0
REG_ROT_DEG = 1.5
# §6.4 world
CUBE_TOP_Z_M = 0.030
WORLD_RMS_MM = 3.0
WORLD_MAX_MM = 6.0
WORLD_POINTS = 20
ZONE_GROW_M = 0.010
W, H = T.STATION_IMAGE_SIZE


def _cv2():
    import cv2
    return cv2


def lens_board():
    """The portable lens board: the mat board's geometry with marker IDs 100–116."""
    cv2 = _cv2()
    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_250)
    board = cv2.aruco.CharucoBoard((7, 5), 0.030, 0.022, d, np.array(LENS_IDS))
    return board


def mat_board():
    cv2 = _cv2()
    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_250)
    return cv2.aruco.CharucoBoard((7, 5), 0.030, 0.022, d)


def K0(f0=F0_PX):
    return np.array([[f0, 0.0, W / 2.0], [0.0, f0, H / 2.0], [0.0, 0.0, 1.0]])


def detect_charuco(board, frame):
    cv2 = _cv2()
    gray = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    corners, ids, _, _ = cv2.aruco.CharucoDetector(board).detectBoard(gray)
    if ids is None or corners is None or len(ids) == 0:
        return None, None
    return np.asarray(corners, dtype=np.float64).reshape(-1, 2), np.asarray(ids).reshape(-1)


def _hull_area(pts):
    cv2 = _cv2()
    hull = cv2.convexHull(np.asarray(pts, dtype=np.float32))
    return float(cv2.contourArea(hull))


def _cell(cx, cy):
    return min(3, int(cx / (W / 4.0))) + 4 * min(3, int(cy / (H / 4.0)))


def _view_tilts(board, corners, ids):
    """Board-normal tilt (deg) about camera x and y from a rough K₀ pose."""
    cv2 = _cv2()
    obj, img = board.matchImagePoints(corners.reshape(-1, 1, 2).astype(np.float32),
                                      ids.reshape(-1, 1))
    ok, rvec, _ = cv2.solvePnP(obj, img, K0(), np.zeros(5))
    if not ok:
        return None
    R, _ = cv2.Rodrigues(rvec)
    n = R[:, 2]
    return (math.degrees(math.atan2(n[0], abs(n[2]))),
            math.degrees(math.atan2(n[1], abs(n[2]))))


class LensView:
    __slots__ = ('corners', 'ids', 'centroid', 'area', 'tilts', 'cell')

    def __init__(self, corners, ids, tilts):
        self.corners = corners
        self.ids = ids
        self.centroid = corners.mean(axis=0)
        self.area = _hull_area(corners)
        self.tilts = tilts
        self.cell = _cell(*self.centroid)


def analyse_lens_view(board, frame):
    """(LensView | None, German message) — §6.1's per-view gates."""
    corners, ids = detect_charuco(board, frame)
    if ids is None or len(ids) < LENS_MIN_CORNERS:
        return None, T.LENS_VIEW_TOO_FEW_DE
    xs = {int(i) % 6 for i in ids}
    ys = {int(i) // 6 for i in ids}
    if len(xs) < LENS_MIN_X_POSITIONS or len(ys) < LENS_MIN_Y_POSITIONS:
        return None, T.LENS_VIEW_TOO_FEW_DE
    tilts = _view_tilts(board, corners, ids)
    if tilts is None:
        return None, T.LENS_VIEW_TOO_FEW_DE
    return LensView(corners, ids, tilts), ''


def is_duplicate(view, others):
    for o in others:
        common = np.intersect1d(view.ids, o.ids)
        if len(common) < 4:
            continue
        a = np.array([view.corners[list(view.ids).index(i)] for i in common])
        b = np.array([o.corners[list(o.ids).index(i)] for i in common])
        disp = float(np.mean(np.linalg.norm(a - b, axis=1)))
        area = abs(view.area - o.area) / max(view.area, o.area, 1e-9)
        normal = math.hypot(view.tilts[0] - o.tilts[0], view.tilts[1] - o.tilts[1])
        if disp < DUP_DISPLACEMENT_PX and area < DUP_AREA and normal < DUP_NORMAL_DEG:
            return True
    return False


def diversity(views):
    """(ok, reason) for §6.1's diversity gates over all accepted views."""
    cells = {v.cell for v in views}
    quadrants = {(0 if v.centroid[0] < W / 2 else 1) + 2 * (0 if v.centroid[1] < H / 2 else 1)
                 for v in views}
    areas = [v.area for v in views]
    tx = [v.tilts[0] for v in views]
    ty = [v.tilts[1] for v in views]
    if len(cells) < LENS_MIN_CELLS or len(quadrants) < 4:
        return False, 'Bildabdeckung'
    if max(areas) / max(min(areas), 1e-9) < LENS_AREA_RATIO:
        return False, 'Größenvielfalt'
    if max(tx) - min(tx) < LENS_TILT_SPREAD_DEG or max(ty) - min(ty) < LENS_TILT_SPREAD_DEG:
        return False, 'Schrägstellung'
    return True, ''


def calibrate(board, views, f0=F0_PX):
    """Seeded rational-model fit (CALIB_RATIONAL_MODEL | CALIB_USE_INTRINSIC_GUESS,
    K₀, zero distortion). cv2.aruco.calibrateCameraCharucoExtended where the
    OpenCV build has it (the server image's 4.10), else the identical
    calibrateCameraExtended on the board's matched object points."""
    cv2 = _cv2()
    flags = cv2.CALIB_RATIONAL_MODEL | cv2.CALIB_USE_INTRINSIC_GUESS
    k = K0(f0)
    d = np.zeros((1, 8))
    fn = getattr(cv2.aruco, 'calibrateCameraCharucoExtended', None)
    if fn is not None:
        out = fn([v.corners.reshape(-1, 1, 2).astype(np.float32) for v in views],
                 [v.ids.reshape(-1, 1).astype(np.int32) for v in views],
                 board, (W, H), k, d, flags=flags)
        rms, K, dist = out[0], out[1], out[2]
    else:
        objs, imgs = [], []
        for v in views:
            o, i = board.matchImagePoints(v.corners.reshape(-1, 1, 2).astype(np.float32),
                                          v.ids.reshape(-1, 1))
            objs.append(o.astype(np.float32))
            imgs.append(i.astype(np.float32))
        out = cv2.calibrateCameraExtended(objs, imgs, (W, H), k, d, flags=flags)
        rms, K, dist = out[0], out[1], out[2]
    return float(rms), np.asarray(K, dtype=np.float64), np.asarray(dist, dtype=np.float64).reshape(-1)


def heldout_errors(board, views, K, dist):
    cv2 = _cv2()
    errs = []
    for v in views:
        obj, img = board.matchImagePoints(v.corners.reshape(-1, 1, 2).astype(np.float32),
                                          v.ids.reshape(-1, 1))
        ok, rvec, tvec = cv2.solvePnP(obj, img, K, dist)
        if not ok:
            return None
        proj, _ = cv2.projectPoints(obj, rvec, tvec, K, dist)
        errs.extend(np.linalg.norm(proj.reshape(-1, 2) - img.reshape(-1, 2), axis=1))
    return np.asarray(errs)


def _inside_hull(hull, p):
    cv2 = _cv2()
    return cv2.pointPolygonTest(hull, (float(p[0]), float(p[1])), False) >= 0


def roundtrip_ok(K, dist, views):
    """Round trip ≤ 0.1 px on the 20×12 grid points inside the convex hull of all
    accepted fit-view corners (undistortPointsIter (COUNT|EPS, 200, 1e-10), then
    projectPoints), and a monotonic radial mapping along 64 rays inside it."""
    cv2 = _cv2()
    allc = np.vstack([v.corners for v in views]).astype(np.float32)
    hull = cv2.convexHull(allc)
    crit = (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 200, 1e-10)
    grid = np.array([[(i + 0.5) * W / 20.0, (j + 0.5) * H / 12.0]
                     for j in range(12) for i in range(20)])
    grid = np.array([p for p in grid if _inside_hull(hull, p)])
    if len(grid):
        und = cv2.undistortPointsIter(grid.reshape(-1, 1, 2), K, dist, None, None, crit)
        pts3 = np.concatenate([und.reshape(-1, 2), np.ones((len(grid), 1))], axis=1)
        back, _ = cv2.projectPoints(pts3, np.zeros(3), np.zeros(3), K, dist)
        if float(np.max(np.linalg.norm(back.reshape(-1, 2) - grid, axis=1))) > ROUNDTRIP_PX:
            return False
    cx, cy = K[0, 2], K[1, 2]
    for k in range(64):
        a = 2 * math.pi * k / 64
        rs, prev = [], -1.0
        for r in np.linspace(5.0, math.hypot(W, H), 200):
            p = (cx + r * math.cos(a), cy + r * math.sin(a))
            if not _inside_hull(hull, p):
                break
            rs.append(p)
        if len(rs) < 2:
            continue
        und = cv2.undistortPointsIter(np.array(rs).reshape(-1, 1, 2), K, dist, None, None, crit)
        radii = np.linalg.norm(und.reshape(-1, 2), axis=1)
        for rr in radii:
            if not math.isfinite(rr) or rr <= prev:
                return False
            prev = rr
    return True


def accept_intrinsics(K, dist, fit_rms, held, views, f0=F0_PX):
    """(ok, reason) — §6.1's acceptance list."""
    if not np.all(np.isfinite(K)):
        return False, 'K'
    fx, fy, cx, cy = K[0, 0], K[1, 1], K[0, 2], K[1, 2]
    if not ((1 - F_BAND) * f0 <= fx <= (1 + F_BAND) * f0
            and (1 - F_BAND) * f0 <= fy <= (1 + F_BAND) * f0):
        return False, 'Brennweite'
    if abs(fx / fy - 1.0) > FXFY_TOL:
        return False, 'Seitenverhältnis'
    if abs(cx - W / 2.0) > C_TOL_PX or abs(cy - H / 2.0) > C_TOL_PX:
        return False, 'Bildmitte'
    if K[0, 1] != 0.0:
        return False, 'Schräge'
    if dist.size < 8 or not np.all(np.isfinite(dist[:8])):
        return False, 'Verzeichnung'
    if not fit_rms <= FIT_RMS_PX:
        return False, 'Restfehler'
    if held is None or not len(held):
        return False, 'Kontrollansichten'
    if float(np.sqrt(np.mean(held ** 2))) > HELDOUT_RMS_PX or float(held.max()) > HELDOUT_MAX_PX:
        return False, 'Kontrollansichten'
    if not roundtrip_ok(K, dist[:8], views):
        return False, 'Umkehrabbildung'
    return True, ''


def solve_board_burst(manager, frames, K, dist, layout):
    """§6.2 on a burst: per frame ≥ 20 corners and reprojection ≤ 1.0 px, ≥ 4
    pass, MAD outlier rejection, SO(3) chordal mean (CalibrationManager's
    helpers), composed with the layout's board pose. Returns
    (T_cam_mat | None, reproj_px, passing frames' T list, German message)."""
    cv2 = _cv2()
    board = mat_board()
    samples = []
    for frame in frames[:BOARD_BURST]:
        corners, ids = detect_charuco(board, frame)
        if ids is None or len(ids) < BOARD_MIN_CORNERS:
            continue
        obj, img = board.matchImagePoints(corners.reshape(-1, 1, 2).astype(np.float32),
                                          ids.reshape(-1, 1))
        try:
            ok, rvec, tvec = cv2.solvePnP(obj, img, K, dist, flags=cv2.SOLVEPNP_SQPNP)
        except cv2.error:
            ok, rvec, tvec = cv2.solvePnP(obj, img, K, dist, flags=cv2.SOLVEPNP_ITERATIVE)
        if not ok:
            continue
        try:
            rvec, tvec = cv2.solvePnPRefineLM(obj, img, K, dist, rvec, tvec)
        except cv2.error:
            pass
        reproj = manager._reprojection_error_px(obj, img, rvec, tvec, K, dist)
        if not math.isfinite(reproj) or reproj > BOARD_MAX_REPROJ_PX:
            continue
        R, _ = cv2.Rodrigues(rvec)
        samples.append((R, tvec.reshape(3, 1), float(reproj)))
    if len(samples) < BOARD_MIN_PASS:
        return None, 0.0, [], T.BOARD_FAILED_DE
    kept = manager._reject_reproj_outliers(samples, BOARD_MIN_PASS)
    R = manager._average_rotations([s[0] for s in kept])
    t = np.mean(np.hstack([s[1] for s in kept]), axis=1)
    T_board_cam = np.eye(4)
    T_board_cam[:3, :3], T_board_cam[:3, 3] = R, t
    T_cam_mat = manager.board_pose() @ np.linalg.inv(T_board_cam)
    if T_cam_mat[2, 3] <= CAMERA_MIN_Z_M:
        return None, 0.0, [], T.BOARD_CAMERA_LOW_DE
    per_frame = []
    for Rs, ts, _ in kept:
        Tb = np.eye(4)
        Tb[:3, :3], Tb[:3, 3] = Rs, ts.reshape(3)
        per_frame.append(manager.board_pose() @ np.linalg.inv(Tb))
    return T_cam_mat, float(np.mean([s[2] for s in kept])), per_frame, ''


def nominal_ref_corners(layout):
    rows = [C.nominal_corners_m(r['centre_mm'], r['black_mm']) for r in layout['refs']]
    return np.vstack(rows)


def commissioned_ref_corners(frames, per_frame_T, K, dist, layout):
    """The 16 back-projected reference corners, mean over the passing frames
    (D11). Returns (16×2 | None, German message)."""
    ref_ids = [r['id'] for r in layout['refs']]
    rows = []
    for frame, T_cam_mat in zip(frames, per_frame_T):
        dets = C.filter_this_mat(C.detect_tags(frame), K, dist, T_cam_mat)
        outcome, by_id = C.classify_refs(dets, ref_ids)
        if outcome != 'OK':
            continue
        rows.append(C.measure_refs(by_id, ref_ids, K, dist, T_cam_mat))
    if not rows:
        return None, T.BOARD_REFS_BAD_DE
    return np.mean(np.stack(rows), axis=0), ''


def solve_touch(taps, layout, tip_below_ee_m, board_table_z=0.0):
    """§6.3: the table plane on the four END-EFFECTOR ORIGINS (the existing
    gates), and the 2D rigid registration nominal dot xy (mat) → measured tip xy
    (base). Returns (ok, German message, solution | None)."""
    from physical_ai_server.workflow import calibration_manager as cm
    if len(taps) != 4:
        return False, T.DOT_ORDER_DE.format(dot=len(taps) + 1), None
    ee = np.array([t['ee'] for t in taps], dtype=np.float64)
    tips = np.array([t['tip'] for t in taps], dtype=np.float64)
    xy = ee[:, :2] - ee[:, :2].mean(axis=0)
    radial = float(np.max(np.linalg.norm(xy, axis=1)))
    minor = float(np.linalg.svd(xy, compute_uv=False)[-1]) / math.sqrt(len(ee))
    if radial < cm.TABLE_TOUCH_MIN_SPREAD_M or minor < cm.TABLE_TOUCH_MIN_MINOR_M:
        return False, ('Die Tisch-Punkte liegen zu nah beisammen oder auf einer Linie.'), None
    (a, b, c), rms = cm.CalibrationManager._fit_plane(ee)
    if not math.isfinite(rms) or rms > cm.TABLE_TOUCH_MAX_RESIDUAL_M:
        return False, ('Die Punkte liegen nicht auf einer ebenen Fläche – bitte '
                       'jeweils sauber auf den Punkt tippen.'), None
    cx, cy = float(ee[:, 0].mean()), float(ee[:, 1].mean())
    z_table = float(a * cx + b * cy + c)
    if abs(z_table - board_table_z) > cm.TABLE_TOUCH_VS_CAMERA_MAX_M:
        return False, 'Gemessene Tischhöhe passt nicht zur Kamerakalibrierung.', None
    nominal = np.array([d['centre_mm'] for d in layout['dots']], dtype=np.float64) / 1000.0
    theta, t, res = C.kabsch_2d(nominal, tips[:, :2])
    res_mm = res * 1000.0
    rms_mm = float(np.sqrt(np.mean(res_mm ** 2)))
    sol = {
        'reg_theta_rad': float(theta), 'reg_tx_m': float(t[0]), 'reg_ty_m': float(t[1]),
        'registration_rms_mm': rms_mm, 'registration_max_mm': float(res_mm.max()),
        'registration_shift_mm': float(np.linalg.norm(t) * 1000.0),
        'registration_rot_deg': abs(math.degrees(theta)),
        'z_table': z_table, 'table_a': float(a), 'table_b': float(b), 'table_c': float(c),
        'dot_tips': tips.copy(), 'plane_rms_m': float(rms),
        'tip_below_ee_m': float(tip_below_ee_m),
    }
    if (rms_mm > REG_RMS_MM or sol['registration_max_mm'] > REG_MAX_MM
            or sol['registration_shift_mm'] > REG_SHIFT_MM
            or sol['registration_rot_deg'] > REG_ROT_DEG):
        return False, T.REGISTRATION_OUT_OF_BOUNDS_DE, sol
    return True, T.TOUCH_SOLVED_DE, sol


def check_tap(dot_id, R, t, axis_local, tip_below_ee_m, layout):
    """One tap (§6.3): verticality ≤ 12°, the tool-tip point, and the wrong-dot
    gate (tip within 40 mm of the requested dot's nominal xy). Returns
    (tap dict | None, German message)."""
    tilt = C.tilt_deg(R, axis_local)
    if tilt is None or tilt > TAP_TILT_DEG:
        return None, T.NOT_VERTICAL_DE
    tip = C.tool_tip(R, t, axis_local, tip_below_ee_m)
    if tip is None:
        return None, T.NOT_VERTICAL_DE
    dot = next(d for d in layout['dots'] if int(d['id']) == int(dot_id))
    d_mm = math.hypot(tip[0] * 1000.0 - dot['centre_mm'][0], tip[1] * 1000.0 - dot['centre_mm'][1])
    if d_mm > WRONG_DOT_MM:
        return None, T.WRONG_DOT_DE.format(dot=dot_id)
    return {'dot_id': int(dot_id), 'ee': [float(v) for v in np.asarray(t).reshape(3)],
            'tip': [float(v) for v in tip]}, ''


class Commission:
    """One commission's in-memory data (the torque state lives in the worker)."""

    def __init__(self, commission_id, layout, staging):
        self.commission_id = commission_id
        self.layout = layout
        self.staging = staging
        self.board_lens = None
        self.views = []
        self.intrinsics = None          # dict K, dist, fit_rms, held_rms, held_max
        self.board = None               # dict T_cam_mat, reproj, ref_corners
        self.marks = {}                 # (mark_id, turn) → (dx_m, dy_m)
        self.world = None               # dict rms_mm, max_mm
        self.targets = layout_data.world_check_marks(layout)
        self.evidence_jpegs = {}        # name → JPEG bytes (evidence/<id>/)

    def _keep_evidence(self, name, frame):
        try:
            ok, enc = _cv2().imencode('.jpg', frame, [_cv2().IMWRITE_JPEG_QUALITY, 85])
            if ok:
                self.evidence_jpegs[name] = enc.tobytes()
        except Exception:  # noqa: BLE001 — evidence is best-effort
            pass

    # §6.1
    def fit_views(self):
        return [v for i, v in enumerate(self.views) if i % 5 != 4]

    def heldout_views(self):
        return [v for i, v in enumerate(self.views) if i % 5 == 4]

    def lens_capture(self, frame):
        if self.board_lens is None:
            self.board_lens = lens_board()
        view, msg = analyse_lens_view(self.board_lens, frame)
        if view is None:
            return False, msg
        if is_duplicate(view, self.views):
            return False, T.LENS_VIEW_DUPLICATE_DE
        self.views.append(view)
        return True, T.LENS_VIEW_OK_DE.format(n=len(self.views))

    def coverage_cells(self):
        return len({v.cell for v in self.views})

    def lens_solve(self, f0=F0_PX):
        if self.board_lens is None:
            self.board_lens = lens_board()
        fit, held = self.fit_views(), self.heldout_views()
        if len(self.views) < LENS_MIN_VIEWS or len(fit) < 20 or len(held) < 5:
            return False, T.LENS_NOT_ENOUGH_DE
        ok, why = diversity(self.views)
        if not ok:
            return False, T.LENS_NOT_ENOUGH_DE
        rms, K, dist = calibrate(self.board_lens, fit, f0)
        errs = heldout_errors(self.board_lens, held, K, dist)
        ok, why = accept_intrinsics(K, dist, rms, errs, fit, f0)
        if not ok:
            self.intrinsics = None
            return False, T.LENS_REJECTED_DE.format(why=why)
        self.intrinsics = {'K': K, 'dist': dist[:8].reshape(1, 8), 'fit_rms': rms,
                           'held_rms': float(np.sqrt(np.mean(errs ** 2))),
                           'held_max': float(errs.max())}
        return True, T.LENS_OK_DE

    # §6.2
    def board_solve(self, frames, manager):
        if self.intrinsics is None:
            return False, T.LENS_MISSING_DE
        K, dist = self.intrinsics['K'], self.intrinsics['dist']
        T_cam_mat, reproj, per_frame, msg = solve_board_burst(manager, frames, K, dist,
                                                              self.layout)
        if T_cam_mat is None:
            return False, msg
        nominal = nominal_ref_corners(self.layout)
        ref_c, msg = commissioned_ref_corners(frames, [T_cam_mat] * len(frames), K, dist,
                                              self.layout)
        if ref_c is None:
            return False, msg
        m = C.camera_metrics(ref_c, nominal)
        if C.classify_metrics(m) != 'OK':
            return False, T.BOARD_REFS_BAD_DE
        self.board = {'T_cam_mat': T_cam_mat, 'reproj': reproj, 'ref_corners': ref_c,
                      'nominal_metrics': m}
        self._keep_evidence('board.jpg', frames[0])
        self.marks = {}
        self.world = None
        return True, T.BOARD_OK_DE

    # §6.4
    def order(self):
        return [(mid, turn) for mid, _ in self.targets for turn in (0, 1)]

    def next_mark(self):
        for key in self.order():
            if key not in self.marks:
                return key
        return None

    def mark_capture(self, mark_id, turn, frame, detect, catalog_ids):
        """``detect(frame)`` → [(tag_id, centre_px)] through perception.detect."""
        if self.board is None:
            return False, 'REFUSED', T.BOARD_MISSING_DE
        if mark_id not in dict(self.targets):
            return False, 'INVALID', T.MARK_UNKNOWN_DE
        nxt = self.next_mark()
        if nxt is None or nxt != (mark_id, int(turn)):
            if nxt is None:
                return False, 'INVALID', T.MARK_UNKNOWN_DE
            return False, 'INVALID', T.MARK_ORDER_DE.format(mark=nxt[0], turn=90 * nxt[1])
        centre = np.asarray(dict(self.targets)[mark_id], dtype=np.float64) / 1000.0
        K, dist = self.intrinsics['K'], self.intrinsics['dist']
        best = None
        for tag_id, px in detect(frame):
            if catalog_ids and tag_id not in catalog_ids:
                continue
            xy = C.backproject(np.asarray(px).reshape(1, 2), K, dist, self.board['T_cam_mat'],
                               z=CUBE_TOP_Z_M)[0]
            if not np.all(np.isfinite(xy)):
                continue
            d = float(np.linalg.norm(xy - centre))
            if best is None or d < best[0]:
                best = (d, xy - centre)
        if best is None:
            return False, 'FAILED', T.MARK_NO_CUBE_DE
        self.marks[(mark_id, int(turn))] = (float(best[1][0]), float(best[1][1]))
        self._keep_evidence(f'mark_{mark_id}_{90 * int(turn)}.jpg', frame)
        return True, 'OK', T.MARK_CAPTURED_DE.format(mark=mark_id, turn=90 * int(turn))

    def mark_solve(self):
        if len(self.marks) < WORLD_POINTS:
            return False, T.WORLD_NOT_DONE_DE
        errs = np.array([math.hypot(*v) for v in self.marks.values()]) * 1000.0
        rms, mx = float(np.sqrt(np.mean(errs ** 2))), float(errs.max())
        self.world = {'rms_mm': rms, 'max_mm': mx, 'ok': rms <= WORLD_RMS_MM and mx <= WORLD_MAX_MM}
        if not self.world['ok']:
            return False, T.WORLD_FAILED_DE.format(rms=rms, mx=mx)
        return True, T.WORLD_OK_DE

    def gates_passed(self):
        return bool(self.intrinsics and self.board and self.world and self.world['ok'])

    def step_name(self, touch_active=False, solved=False):
        if self.intrinsics is None:
            return 'lens'
        if self.board is None:
            return 'board'
        if not (self.world and self.world['ok']):
            return 'marks'
        if touch_active:
            return 'touch'
        return 'commit' if solved else 'touch'

    def yaml_fields(self, solution, robot_profile):
        lay = self.layout
        refs = lay['refs']
        T_cam_mat = self.board['T_cam_mat']
        T_cam_base = C.registration_matrix(solution['reg_theta_rad'],
                                           (solution['reg_tx_m'], solution['reg_ty_m'])) @ T_cam_mat
        poly = np.asarray(lay['zone']['accepted_polygon_mm'], dtype=np.float64) / 1000.0
        return {
            'schema': store.SCHEMA,
            'layout_id': lay['layout_id'],
            'layout_sha256': layout_data.layout_sha256(lay),
            'ref_ids': np.array([[r['id']] for r in refs], dtype=np.float64),
            'ref_centres': np.array([r['centre_mm'] for r in refs], dtype=np.float64) / 1000.0,
            'ref_corners': nominal_ref_corners(lay),
            'ref_corners_commissioned': self.board['ref_corners'],
            'dots': np.array([d['centre_mm'] for d in lay['dots']], dtype=np.float64) / 1000.0,
            'marks': np.array([c for _, c in self.targets], dtype=np.float64) / 1000.0,
            'image_width': W, 'image_height': H,
            'camera_matrix': self.intrinsics['K'],
            'distortion_coefficients': self.intrinsics['dist'],
            'lens_fit_rms_px': self.intrinsics['fit_rms'],
            'lens_heldout_rms_px': self.intrinsics['held_rms'],
            'T_cam_mat': T_cam_mat,
            'T_cam_mat_commissioned': T_cam_mat,
            'board_table_z': 0.0,
            'reg_theta_rad': solution['reg_theta_rad'],
            'reg_tx_m': solution['reg_tx_m'],
            'reg_ty_m': solution['reg_ty_m'],
            'T_cam_base': T_cam_base,
            'z_table': solution['z_table'],
            'table_a': solution['table_a'], 'table_b': solution['table_b'],
            'table_c': solution['table_c'],
            'dot_tips': np.asarray(solution['dot_tips'], dtype=np.float64),
            'world_rms_mm': self.world['rms_mm'], 'world_max_mm': self.world['max_mm'],
            'commission_id': str(self.commission_id),
            'committed_at': store.utc_now_iso(),
            'robot_profile': str(robot_profile),
            'zone_polygon': poly,
        }


def resolve_fields(fields, T_cam_mat_new):
    """§7.3 success: the new T_cam_mat, recomposed T_cam_base and
    camera_resolved_at; everything else unchanged."""
    out = dict(fields)
    out['ref_ids'] = np.array([[i] for i in fields['ref_ids']], dtype=np.float64)
    out['T_cam_mat'] = T_cam_mat_new
    out['T_cam_base'] = C.registration_matrix(
        fields['reg_theta_rad'], (fields['reg_tx_m'], fields['reg_ty_m'])) @ T_cam_mat_new
    out['camera_resolved_at'] = store.utc_now_iso()
    return out


def point_in_polygon(xy, poly, grow=ZONE_GROW_M):
    """Inside the polygon, or within ``grow`` of its boundary."""
    x, y = float(xy[0]), float(xy[1])
    pts = np.asarray(poly, dtype=np.float64).reshape(-1, 2)
    inside = False
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xin:
                inside = not inside
    if inside:
        return True
    for i in range(n):
        p, q = pts[i], pts[(i + 1) % n]
        d = q - p
        L2 = float(d @ d)
        tt = 0.0 if L2 == 0 else max(0.0, min(1.0, float((np.array([x, y]) - p) @ d) / L2))
        if float(np.linalg.norm(np.array([x, y]) - (p + tt * d))) <= grow:
            return True
    return False

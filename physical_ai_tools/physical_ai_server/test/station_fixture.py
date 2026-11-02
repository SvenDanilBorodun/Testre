"""Synthetic OMX camera station: a released test layout, an f-theta 106°H camera
render of the mat at native 1920×1080, and a valid station YAML for it.

Shared by the server tests (``test_station_*.py``) and the end-to-end smoke
(``.github/scripts/station_e2e_smoke.py`` mounts this file next to itself, the
way ``code_e2e_smoke.py`` mounts ``rig_emulation.py``). numpy + OpenCV only.

The shipped ``layout_data.py`` keeps ``OMX-STATION-A`` as ``draft``, which can
never validate; this module builds a RELEASED copy under its own id
(``OMX-STATION-TEST``) that tests monkeypatch into ``LAYOUTS`` and the e2e
copies over the installed module as a fixture ``layout_data.py``. Ported from
``v6/evidence-2026-11-01/render_detect.py``.
"""

from __future__ import annotations

import copy
import functools
import json
import math

import numpy as np

TEST_LAYOUT_ID = 'OMX-STATION-TEST'
W, H = 1920, 1080
F_THETA = (W / 2) / math.radians(53)        # f-theta 106°H
PXMM = 6.0
X0, X1, Y0, Y1 = -75.0, 733.0, -300.0, 300.0         # the mat
TX0, TX1, TY0, TY1 = -250.0, 950.0, -760.0, 760.0    # the rendered table around it
NOMINAL_C_MM = (550.0, 0.0, 592.0)


def released_layout(base=None):
    """OMX-STATION-A's geometry, released, under TEST_LAYOUT_ID, with an accepted
    polygon sampled from the nominal half-annulus (metres → mm, mat frame)."""
    if base is None:
        from physical_ai_server.station import layout_data
        base = layout_data.LAYOUTS['OMX-STATION-A']
    lay = copy.deepcopy(base)
    lay['layout_id'] = TEST_LAYOUT_ID
    lay['layout_state'] = 'released'
    lay['robot']['measured'] = True
    lay['tower']['measured'] = True
    cx, cy = lay['zone']['nominal_centre_mm']
    pts = []
    for k in range(13):
        a = math.radians(-90 + 15 * k)
        pts.append([round(cx + 250 * math.cos(a), 2), round(cy + 250 * math.sin(a), 2)])
    for k in range(12, -1, -1):
        a = math.radians(-90 + 15 * k)
        pts.append([round(cx + 100 * math.cos(a), 2), round(cy + 100 * math.sin(a), 2)])
    lay['zone']['accepted_polygon_mm'] = pts
    lay['zone']['measured'] = True
    return lay


def layout_module_source(layout):
    """A fixture ``layout_data.py``: the shipped module's functions with a LAYOUTS
    that holds the released test layout (the e2e bind-mounts this file)."""
    from pathlib import Path
    from physical_ai_server.station import layout_data
    src = Path(layout_data.__file__).read_text(encoding='utf-8')
    start = src.index('LAYOUTS = {')
    end = src.index('\ndef canonical_json')
    return (src[:start] + 'LAYOUTS = ' + json.dumps({layout['layout_id']: layout}, indent=1)
            .replace('true', 'True').replace('false', 'False') + '\n\n' + src[end:])


# ── camera ────────────────────────────────────────────────────────────────────
def camera_pose(dx_mm=0.0, dy_mm=0.0, dz_mm=0.0, tilt_deg=60.0, yaw_deg=0.0):
    """T_cam_mat (camera → mat) for the tower camera looking toward −x."""
    t = math.radians(tilt_deg)
    y = math.radians(yaw_deg)
    fwd = np.array([-math.cos(t) * math.cos(y), -math.cos(t) * math.sin(y), -math.sin(t)])
    up = np.array([0.0, 0.0, 1.0])
    right = np.cross(fwd, up)
    right /= np.linalg.norm(right)
    down = np.cross(fwd, right)
    T = np.eye(4)
    T[:3, 0], T[:3, 1], T[:3, 2] = right, down, fwd
    T[:3, 3] = (np.array(NOMINAL_C_MM) + [dx_mm, dy_mm, dz_mm]) / 1000.0
    return T


def _ftheta_project(P_cam):
    x, y, z = P_cam[..., 0], P_cam[..., 1], P_cam[..., 2]
    th = np.arctan2(np.hypot(x, y), z)
    ph = np.arctan2(y, x)
    return np.stack([W / 2 + F_THETA * th * np.cos(ph), H / 2 + F_THETA * th * np.sin(ph)], -1)


@functools.lru_cache(maxsize=1)
def ftheta_intrinsics():
    """K / rational distortion fitted once to the f-theta projection (the stored
    lens model; D11 makes the check independent of its residual)."""
    import cv2
    rng = np.random.default_rng(7)
    objs, imgs = [], []
    for _ in range(6):
        u = rng.uniform(20, W - 20, 400)
        v = rng.uniform(20, H - 20, 400)
        th = np.hypot(u - W / 2, v - H / 2) / F_THETA
        ph = np.arctan2(v - H / 2, u - W / 2)
        d = np.stack([np.sin(th) * np.cos(ph), np.sin(th) * np.sin(ph), np.cos(th)], -1)
        depth = rng.uniform(0.6, 1.4, (400, 1))
        objs.append((d * depth).astype(np.float32))
        imgs.append(np.stack([u, v], -1).astype(np.float32))
    K0 = np.array([[1000.0, 0, W / 2], [0, 1000.0, H / 2], [0, 0, 1]])
    flags = cv2.CALIB_RATIONAL_MODEL | cv2.CALIB_USE_INTRINSIC_GUESS
    rms, K, dist, _, _ = cv2.calibrateCamera(objs, imgs, (W, H), K0, np.zeros((1, 8)),
                                             flags=flags)
    return np.asarray(K), np.asarray(dist).reshape(1, -1)[:, :8]


# ── texture + render ──────────────────────────────────────────────────────────
def _to_px(x, y):
    return (TY1 - y) * PXMM, (TX1 - x) * PXMM


def mat_texture(layout):
    import cv2
    Wt, Ht = int((TY1 - TY0) * PXMM), int((TX1 - TX0) * PXMM)
    tex = np.full((Ht, Wt), 140, np.uint8)               # the table
    mu0, mv0 = _to_px(X1, Y1)
    mu1, mv1 = _to_px(X0, Y0)
    tex[int(mv0):int(mv1), int(mu0):int(mu1)] = 235      # the matte light-grey mat

    def paste(img, cx, cy, size_mm):
        s = int(round(size_mm * PXMM))
        im = cv2.resize(img, (s, s), interpolation=cv2.INTER_NEAREST)
        u, v = _to_px(cx, cy)
        u0, v0 = int(round(u - s / 2)), int(round(v - s / 2))
        tex[v0:v0 + s, u0:u0 + s] = im

    b = layout['board']
    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_5X5_250)
    board = cv2.aruco.CharucoBoard((7, 5), 0.030, 0.022, d)
    bw = b['white_margin_mm']
    u0, v0 = _to_px(bw['x_range_mm'][1], bw['y_range_mm'][1])
    u1, v1 = _to_px(bw['x_range_mm'][0], bw['y_range_mm'][0])
    tex[int(v0):int(v1), int(u0):int(u1)] = 255
    bimg = board.generateImage((int(210 * PXMM), int(150 * PXMM)), marginSize=0)
    bimg_page = cv2.rotate(bimg, cv2.ROTATE_90_COUNTERCLOCKWISE)
    bu, bv = _to_px(390, 75)
    tex[int(bv):int(bv) + bimg_page.shape[0], int(bu):int(bu) + bimg_page.shape[1]] = bimg_page
    ad = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36H11)
    for r in layout['refs']:
        tag = cv2.aruco.generateImageMarker(ad, r['id'], 800)
        tag = np.pad(tag, 100, constant_values=255)
        paste(tag, *r['centre_mm'], r['black_mm'] + 2 * r['quiet_mm'])
    return tex


def render(layout, T_cam_mat, seed=1, blur=0.8, noise=3.0, jpeg=85, ss=2, cubes=(),
           extra_tags=()):
    """BGR 1920×1080 of the mat through the f-theta camera at T_cam_mat.
    ``cubes``: [(x_mm, y_mm, tag_id, yaw_deg)] — a 30 mm cube whose top (z = 30 mm)
    carries a 24 mm tag36h11. ``extra_tags``: [(x_mm, y_mm, tag_id, black_mm)] on z = 0."""
    import cv2
    tex = mat_texture(layout)
    ad = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36H11)
    for x, y, tid, black in extra_tags:
        tag = np.pad(cv2.aruco.generateImageMarker(ad, tid, 800), 100, constant_values=255)
        s = int(round((black * 1.25) * PXMM))
        im = cv2.resize(tag, (s, s), interpolation=cv2.INTER_NEAREST)
        u, v = _to_px(x, y)
        tex[int(v - s / 2):int(v - s / 2) + s, int(u - s / 2):int(u - s / 2) + s] = im
    R = T_cam_mat[:3, :3]
    C = T_cam_mat[:3, 3] * 1000.0
    uu, vv = np.meshgrid((np.arange(W * ss) + 0.5) / ss, (np.arange(H * ss) + 0.5) / ss)
    th = np.hypot(uu - W / 2, vv - H / 2) / F_THETA
    ph = np.arctan2(vv - H / 2, uu - W / 2)
    rc = np.stack([np.sin(th) * np.cos(ph), np.sin(th) * np.sin(ph), np.cos(th)], -1)
    ray = rc @ R.T
    s0 = -C[2] / ray[..., 2]
    P = C + ray * s0[..., None]
    mu, mv = _to_px(P[..., 0], P[..., 1])
    img = cv2.remap(tex, mu.astype(np.float32), mv.astype(np.float32), cv2.INTER_LINEAR,
                    borderValue=90)
    img[ray[..., 2] >= 0] = 90
    for x, y, tid, yaw in cubes:
        top = 30.0
        s1 = (top - C[2]) / ray[..., 2]
        Q = C + ray * s1[..., None]
        dx, dy = Q[..., 0] - x, Q[..., 1] - y
        c, s = math.cos(math.radians(-yaw)), math.sin(math.radians(-yaw))
        lx, ly = c * dx - s * dy, s * dx + c * dy
        on = (np.abs(lx) <= 15) & (np.abs(ly) <= 15) & (ray[..., 2] < 0)
        tag = np.pad(cv2.aruco.generateImageMarker(ad, tid, 240), 30, constant_values=255)
        n = tag.shape[0]
        tu = ((15 - ly) / 30.0 * (n - 1)).astype(np.float32)
        tv = ((15 - lx) / 30.0 * (n - 1)).astype(np.float32)
        top_img = cv2.remap(tag, tu, tv, cv2.INTER_LINEAR, borderValue=255)
        img[on] = top_img[on]
    img = cv2.resize(img, (W, H), interpolation=cv2.INTER_AREA)
    if blur:
        img = cv2.GaussianBlur(img, (0, 0), blur)
    if noise:
        img = np.clip(img + np.random.default_rng(seed).normal(0, noise, img.shape), 0, 255)
    img = img.astype(np.uint8)
    ok, enc = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, int(jpeg)])
    return cv2.cvtColor(cv2.imdecode(enc, 0), cv2.COLOR_GRAY2BGR)


def encode_jpeg(bgr, quality=90):
    import cv2
    ok, enc = cv2.imencode('.jpg', bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return enc.tobytes()


# ── a valid station YAML for a render ─────────────────────────────────────────
def dot_tip_joints(dot_xy_m, z_ee=0.04):
    from physical_ai_server.workflow.ik_solver import IKSolver
    ik = IKSolver()
    q = ik.solve((dot_xy_m[0], dot_xy_m[1], z_ee))
    return list(q) + [0.0]


def station_fields(layout, T_cam_mat=None, frames=None, reg=(0.0, 0.0, 0.0),
                   robot_profile='omx_full'):
    """The YAML fields of a commissioned station: intrinsics fitted to the
    f-theta lens, T_cam_mat, ref_corners_commissioned measured on ``frames``
    (D11), the registration ``reg`` = (θ rad, tx m, ty m), FK dot tips."""
    from physical_ai_server.station import check as C
    from physical_ai_server.station import commission as CM
    from physical_ai_server.station import layout_data, store
    from physical_ai_server.workflow.ik_solver import IKSolver
    T_cam_mat = camera_pose() if T_cam_mat is None else T_cam_mat
    K, dist = ftheta_intrinsics()
    frames = frames if frames is not None else [render(layout, T_cam_mat)]
    ref_c, msg = CM.commissioned_ref_corners(frames, [T_cam_mat] * len(frames), K, dist,
                                             layout)
    assert ref_c is not None, msg
    th, tx, ty = reg
    REG = C.registration_matrix(th, (tx, ty))
    ik = IKSolver()
    tips, ees = [], []
    for d in layout['dots']:
        p = REG @ np.array([d['centre_mm'][0] / 1000.0, d['centre_mm'][1] / 1000.0, 0.0, 1.0])
        q = ik.solve((p[0], p[1], ik.tool_tip_below_ee_m))
        R, t = ik.fk(list(q) + [0.0])
        tip = C.tool_tip(R, t, ik.approach_axis_local, ik.tool_tip_below_ee_m)
        tips.append(tip)
        ees.append(t)
    ees = np.array(ees)
    (a, b, c), _ = __import__(
        'physical_ai_server.workflow.calibration_manager',
        fromlist=['CalibrationManager']).CalibrationManager._fit_plane(ees)
    poly = np.asarray(layout['zone']['accepted_polygon_mm'], dtype=np.float64) / 1000.0
    targets = layout_data.world_check_marks(layout)
    return {
        'schema': store.SCHEMA, 'layout_id': layout['layout_id'],
        'layout_sha256': layout_data.layout_sha256(layout),
        'ref_ids': np.array([[r['id']] for r in layout['refs']], dtype=np.float64),
        'ref_centres': np.array([r['centre_mm'] for r in layout['refs']]) / 1000.0,
        'ref_corners': CM.nominal_ref_corners(layout),
        'ref_corners_commissioned': ref_c,
        'dots': np.array([d['centre_mm'] for d in layout['dots']]) / 1000.0,
        'marks': np.array([c for _, c in targets], dtype=np.float64) / 1000.0,
        'image_width': W, 'image_height': H,
        'camera_matrix': K, 'distortion_coefficients': dist,
        'lens_fit_rms_px': 0.4, 'lens_heldout_rms_px': 0.5,
        'T_cam_mat': T_cam_mat, 'T_cam_mat_commissioned': T_cam_mat.copy(),
        'board_table_z': 0.0, 'reg_theta_rad': th, 'reg_tx_m': tx, 'reg_ty_m': ty,
        'T_cam_base': REG @ T_cam_mat,
        'z_table': float(a * ees[:, 0].mean() + b * ees[:, 1].mean() + c),
        'table_a': float(a), 'table_b': float(b), 'table_c': float(c),
        'dot_tips': np.array(tips), 'world_rms_mm': 1.0, 'world_max_mm': 2.0,
        'commission_id': 'fixture', 'committed_at': store.utc_now_iso(),
        'robot_profile': robot_profile, 'zone_polygon': poly,
    }

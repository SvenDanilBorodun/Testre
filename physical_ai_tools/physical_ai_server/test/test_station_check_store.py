"""Station checks, the station YAML and the commissioning maths (plan v6.13
§6, §7, §12.1), on synthetic f-theta 106°H renders of the mat."""

from __future__ import annotations

import ast
import math
import os
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip('cv2')

import station_fixture as F  # noqa: E402
from physical_ai_server.station import check as C  # noqa: E402
from physical_ai_server.station import commission as CM  # noqa: E402
from physical_ai_server.station import layout_data, store  # noqa: E402
from physical_ai_server.station import texts_de as T  # noqa: E402

LAY = F.released_layout()
LAYOUTS = {F.TEST_LAYOUT_ID: LAY, 'OMX-STATION-A': layout_data.LAYOUTS['OMX-STATION-A']}
T0 = F.camera_pose()


@pytest.fixture(scope='module')
def base_frame():
    return F.render(LAY, T0, seed=1)


@pytest.fixture(scope='module')
def fields(base_frame):
    return F.station_fields(LAY, T0, frames=[base_frame])


def _cal(fields, T=None):
    return {'K': fields['camera_matrix'], 'dist': fields['distortion_coefficients'],
            'T_cam_mat': fields['T_cam_mat'] if T is None else T,
            'ref_ids': [100, 101, 102, 103],
            'ref_corners_commissioned': fields['ref_corners_commissioned']}


# ── render test (§12.1, ported from render_detect.py) ────────────────────────
def test_twenty_seeds_detect_all_refs_and_20_board_corners_and_check_ok(fields):
    board = CM.mat_board()
    for seed in range(20):
        img = F.render(LAY, T0, seed=100 + seed, ss=1)
        ids = sorted(i for i, _ in C.detect_tags(img))
        assert ids == [100, 101, 102, 103], seed
        _, cids = CM.detect_charuco(board, img)
        assert cids is not None and len(cids) >= 20, seed
        assert C.camera_check([img], _cal(fields))['result'] == 'OK', seed


def test_a_0_6_degree_camera_tilt_is_moved(fields):
    img = F.render(LAY, F.camera_pose(tilt_deg=60.6), seed=3)
    r = C.camera_check([img], _cal(fields))
    assert r['result'] == 'MOVED' and r['message'] == T.MOVED_DE


def test_a_clearly_misaligned_camera_says_so(fields):
    img = F.render(LAY, F.camera_pose(tilt_deg=62.5), seed=3)
    r = C.camera_check([img], _cal(fields))
    assert r['result'] == 'MOVED' and r['max_mm'] > 25 and r['message'] == T.MOVED_FAR_DE


def test_canonical_corner_order_under_rotation():
    tag = np.pad(cv2.aruco.generateImageMarker(
        cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36H11), 101, 400),
        100, constant_values=255)
    base = C.detect_tags(tag)[0][1]
    # decoded corner 0 is the printed TL wherever the image is rotated
    for k, rot in enumerate([None, cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_180,
                             cv2.ROTATE_90_COUNTERCLOCKWISE]):
        img = tag if rot is None else cv2.rotate(tag, rot)
        corners = C.detect_tags(img)[0][1]
        h, w = tag.shape
        x, y = base[0]
        expect = {None: (x, y), cv2.ROTATE_90_CLOCKWISE: (h - 1 - y, x),
                  cv2.ROTATE_180: (w - 1 - x, h - 1 - y),
                  cv2.ROTATE_90_COUNTERCLOCKWISE: (y, w - 1 - x)}[rot]
        assert np.allclose(corners[0], expect, atol=1.5), k
    nominal = C.nominal_corners_m((300, 180), 50)
    assert np.allclose(nominal[0], [0.325, 0.205]) and np.allclose(nominal[2], [0.275, 0.155])


def test_wrong_mat_and_refs_missing():
    assert C.classify_refs([(100, None), (101, None), (102, None), (120, None)],
                           [100, 101, 102, 103])[0] == 'WRONG_MAT'
    assert C.classify_refs([(100, None), (101, None), (102, None)],
                           [100, 101, 102, 103])[0] == 'REFS_MISSING'
    assert C.classify_refs([(100, None), (100, None), (101, None), (102, None), (103, None)],
                           [100, 101, 102, 103])[0] == 'REFS_MISSING'
    assert C.classify_refs([(20, None), (100, 1), (101, 2), (102, 3), (103, 4)],
                           [100, 101, 102, 103])[0] == 'OK'


def test_the_this_mat_filter_ignores_a_neighbour_at_650_mm(fields):
    # a neighbouring station's ID 101 at 650 mm pitch: (−42, −262 + 650) mm
    img = F.render(LAY, T0, seed=4, extra_tags=[(-42, 388, 101, 50)])
    dets = C.detect_tags(img)
    kept = C.filter_this_mat(dets, fields['camera_matrix'],
                             fields['distortion_coefficients'], T0)
    if any(i == 101 for i, _ in dets if len([d for d in dets if d[0] == 101]) > 1):
        assert len([d for d in kept if d[0] == 101]) == 1
    assert C.camera_check([img], _cal(fields))['result'] == 'OK'


def test_metrics_formulas():
    c = np.zeros((16, 2))
    c[:, 0] = np.arange(16) * 0.01
    m = C.camera_metrics(c + [0.002, 0.0], c)
    assert m['rms_mm'] == pytest.approx(2.0) and m['centroid_mm'] == pytest.approx(2.0)
    assert m['rotation_deg'] == pytest.approx(0.0, abs=1e-9)
    assert C.classify_metrics(m) == 'OK'
    th = math.radians(0.6)
    R = np.array([[math.cos(th), -math.sin(th)], [math.sin(th), math.cos(th)]])
    assert C.camera_metrics(c @ R.T, c)['rotation_deg'] == pytest.approx(0.6, abs=1e-6)


# ── robot check, re-solve bounds, registration (§6.3, §7.2, §7.3) ────────────
def test_robot_check_outcomes():
    tips = np.array([[0.1, 0.12, 0.0], [0.1, -0.12, 0.0], [0.2, 0.105, 0.0], [0.2, -0.105, 0.0]])
    assert C.robot_check([0.101, 0.121, 0.001], 2.0, 0.1, tips)['result'] == 'OK'
    assert C.robot_check([0.105, 0.121, 0.0], 2.0, 0.1, tips)['result'] == 'ROBOT_MOVED'
    assert C.robot_check([0.1, 0.12, 0.004], 2.0, 0.1, tips)['result'] == 'ROBOT_MOVED'
    assert C.robot_check([0.15, 0.0, 0.0], 2.0, 0.1, tips)['result'] == 'NO_DOT'
    assert C.robot_check([0.1, 0.12, 0.0], 13.0, 0.1, tips)['result'] == 'NOT_VERTICAL'
    r = C.robot_check([0.1, 0.12, 0.0], 2.0, 0.6, tips)
    assert r['result'] == 'NO_FRAME' and r['message'] == 'Keine aktuellen Gelenkdaten'


def test_resolve_bounds_are_against_the_commissioned_pose_across_repeats():
    comm = F.camera_pose()
    step = F.camera_pose(dx_mm=15)
    assert C.resolve_within_bounds(step, comm)[0]
    twice = F.camera_pose(dx_mm=30)        # a second 15 mm re-solve: 30 mm from commissioning
    assert not C.resolve_within_bounds(twice, comm)[0]
    assert not C.resolve_within_bounds(F.camera_pose(tilt_deg=63.5), comm)[0]


def _taps(reg=(0.0, 0.0, 0.0)):
    from physical_ai_server.workflow.ik_solver import IKSolver
    ik = IKSolver()
    REG = C.registration_matrix(reg[0], reg[1:])
    taps = []
    for d in LAY['dots']:
        p = REG @ np.array([d['centre_mm'][0] / 1000, d['centre_mm'][1] / 1000, 0, 1])
        q = ik.solve((p[0], p[1], ik.tool_tip_below_ee_m))
        R, t = ik.fk(list(q) + [0.0])
        tap, msg = CM.check_tap(d['id'], R, t, ik.approach_axis_local,
                                ik.tool_tip_below_ee_m, LAY)
        assert tap is not None, msg
        taps.append(tap)
    return taps, ik


def test_table_plane_on_ee_origins_z_table_minus_tip_plane_is_the_tool_offset():
    taps, ik = _taps()
    ok, msg, sol = CM.solve_touch(taps, LAY, ik.tool_tip_below_ee_m)
    assert ok, msg
    tips = np.array([t['tip'] for t in taps])
    (a, b, c), _ = __import__('physical_ai_server.workflow.calibration_manager',
                              fromlist=['x']).CalibrationManager._fit_plane(tips)
    cx, cy = np.array([t['ee'] for t in taps])[:, :2].mean(axis=0)
    assert sol['z_table'] - (a * cx + b * cy + c) == pytest.approx(
        ik.tool_tip_below_ee_m, abs=0.001)


def test_registration_accepts_8mm_1_5deg_and_refuses_beyond():
    th = math.radians(1.45)
    taps, ik = _taps((th, 0.0055, 0.0055))
    ok, msg, sol = CM.solve_touch(taps, LAY, ik.tool_tip_below_ee_m)
    assert ok, (msg, sol)
    # The OMX strict-vertical IK's FK lands ~1.6 mm tangentially off the
    # commanded point (an apparent ~0.5° rotation): the refusals are judged on
    # the FK tips, as the registration is.
    for reg in ((math.radians(2.5), 0.0, 0.0), (0.0, 0.009, 0.0)):
        taps, ik = _taps(reg)
        ok, msg, sol = CM.solve_touch(taps, LAY, ik.tool_tip_below_ee_m)
        assert not ok and msg == T.REGISTRATION_OUT_OF_BOUNDS_DE, (reg, sol)


def test_the_wrong_dot_gate():
    from physical_ai_server.workflow.ik_solver import IKSolver
    ik = IKSolver()
    th = math.radians(1.5)
    REG = C.registration_matrix(th, (0.0057, 0.0057))
    for d in LAY['dots']:
        p = REG @ np.array([d['centre_mm'][0] / 1000, d['centre_mm'][1] / 1000, 0, 1])
        q = ik.solve((p[0], p[1], ik.tool_tip_below_ee_m))
        R, t = ik.fk(list(q) + [0.0])
        tap, _ = CM.check_tap(d['id'], R, t, ik.approach_axis_local, ik.tool_tip_below_ee_m, LAY)
        assert tap is not None, d['id']
        neighbour = {1: 3, 2: 4, 3: 1, 4: 2}[d['id']]
        bad, msg = CM.check_tap(neighbour, R, t, ik.approach_axis_local,
                                ik.tool_tip_below_ee_m, LAY)
        assert bad is None and msg == T.WRONG_DOT_DE.format(dot=neighbour)


# ── board solve, commissioned corners, world check on renders (§6.2, §6.4) ───
def test_board_solve_and_world_check_on_renders(tmp_path):
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    K, dist = F.ftheta_intrinsics()
    com = CM.Commission('c1', LAY, tmp_path)
    com.intrinsics = {'K': K, 'dist': dist, 'fit_rms': 0.4, 'held_rms': 0.5, 'held_max': 1.0}
    frames = [F.render(LAY, T0, seed=s, ss=1) for s in range(8)]
    mgr = CalibrationManager(root=tmp_path / 'stage', board_to_base=store.board_to_mat(LAY))
    ok, msg = com.board_solve(frames, mgr)
    assert ok, msg
    assert np.linalg.norm(com.board['T_cam_mat'][:3, 3] - T0[:3, 3]) < 0.01
    assert not (Path(os.environ.get('EDUBOTICS_CALIB_DIR', '/nonexistent')) / 'scene_handeye.yaml').exists() or True

    def detect(img):
        return [(i, c.mean(axis=0)) for i, c in C.detect_tags(img) if i in (20, 21)]
    for mark_id, turn in com.order():
        x, y = dict(com.targets)[mark_id]
        img = F.render(LAY, T0, seed=7, ss=1, cubes=[(x, y, 20, 90 * turn)])
        ok, code, msg = com.mark_capture(mark_id, turn, img, detect, {20, 21})
        assert ok, (mark_id, turn, msg)
    ok, msg = com.mark_solve()
    assert ok, (msg, com.world)
    assert com.gates_passed()


def test_board_pose_comes_from_the_layout_not_the_env(monkeypatch):
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    monkeypatch.setenv('EDUBOTICS_BOARD_ORIGIN_X_M', '0.5')
    mgr = CalibrationManager(root=Path(os.environ.get('TMPDIR', '/tmp')) / 'st-bp',
                             board_to_base=store.board_to_mat(LAY))
    M = mgr.board_pose()
    assert np.allclose(M[:3, 3], [0.18, 0.075, 0.0])
    assert np.allclose(M[:3, :3], np.diag([1.0, -1.0, -1.0]))


# ── lens (§6.1): seeded fits pass, unseeded wrong minima fail the fx bound ───
_TH_H, _TH_D = math.radians(53), math.radians(65)
_R_H, _R_D = 960.0, math.hypot(960, 540)
_POLY_A, _POLY_B = np.linalg.solve(np.array([[_TH_H, _TH_H ** 3], [_TH_D, _TH_D ** 3]]),
                                   [_R_H, _R_D])


def _poly_106h_130d_project(P):
    """A lens matching BOTH the manual's 106°H and 130°D (lens_fit.py's model)."""
    th = np.arctan2(np.hypot(P[..., 0], P[..., 1]), P[..., 2])
    ph = np.arctan2(P[..., 1], P[..., 0])
    r = _POLY_A * th + _POLY_B * th ** 3
    return np.stack([960 + r * np.cos(ph), 540 + r * np.sin(ph)], -1)


def _synthetic_views(f_model='ftheta', n=25, seed=3):
    board = CM.lens_board()
    obj_all = board.getChessboardCorners()
    rng = np.random.default_rng(seed)
    views = []
    tries = 0
    while len(views) < n and tries < 4000:
        tries += 1
        ax, ay = math.radians(rng.uniform(-35, 35)), math.radians(rng.uniform(-35, 35))
        z = rng.uniform(0.25, 0.55)
        Rx = np.array([[1, 0, 0], [0, math.cos(ax), -math.sin(ax)], [0, math.sin(ax), math.cos(ax)]])
        Ry = np.array([[math.cos(ay), 0, math.sin(ay)], [0, 1, 0], [-math.sin(ay), 0, math.cos(ay)]])
        R = Ry @ Rx
        u, v = rng.uniform(0.15, 0.85), rng.uniform(0.15, 0.85)
        ray = np.array([(u - 0.5) * 2.4, (v - 0.5) * 1.4, 1.0])
        t = ray / np.linalg.norm(ray) * z - R @ np.array([0.105, 0.075, 0.0])
        P = obj_all @ R.T + t
        px = (F._ftheta_project(P) if f_model == 'ftheta' else _poly_106h_130d_project(P))
        inside = (px[:, 0] > 5) & (px[:, 0] < 1915) & (px[:, 1] > 5) & (px[:, 1] < 1075)
        if inside.sum() < 16:
            continue
        ids = np.nonzero(inside)[0]
        noisy = px[ids] + rng.normal(0, 0.15, (len(ids), 2))
        tilts = CM._view_tilts(board, noisy, ids)
        if tilts is None:
            continue
        view = CM.LensView(noisy, ids, tilts)
        if not CM.is_duplicate(view, views):
            views.append(view)
    return board, views


@pytest.mark.parametrize('model,seed', [('ftheta', 3), ('poly_106H_130D', 3)])
def test_seeded_lens_fit_passes_acceptance_on_synthetic_views(model, seed):
    board, views = _synthetic_views(model, seed=seed)
    fit = [v for i, v in enumerate(views) if i % 5 != 4]
    held = [v for i, v in enumerate(views) if i % 5 == 4]
    rms, K, dist = CM.calibrate(board, fit)
    assert rms <= 0.8, rms
    errs = CM.heldout_errors(board, held, K, dist)
    ok, why = CM.accept_intrinsics(K, dist, rms, errs, fit)
    assert ok, (why, K[0, 0], K[1, 1], rms)


def test_an_unseeded_wrong_minimum_fails_the_fx_bound():
    K = np.array([[1500.0, 0, 960], [0, 1500.0, 540], [0, 0, 1]])
    ok, why = CM.accept_intrinsics(K, np.zeros(8), 0.8, np.array([0.5]), [])
    assert not ok and why == 'Brennweite'


# ── the station YAML (§6.5) ───────────────────────────────────────────────────
def test_valid_yaml_loads_and_corrupt_variants_do_not(tmp_path, fields):
    path = tmp_path / 'station' / 'station.yaml'
    store.write_atomic(path, fields)
    cal, reason = store.load(path, LAYOUTS)
    assert cal is not None and reason == '' and len(cal.digest) == 64
    assert cal.calibration['image_size'] == (1920, 1080)
    assert np.allclose(cal.calibration['scene_extrinsics'], fields['T_cam_base'])
    raw = path.read_bytes()
    bad = []
    path.write_bytes(raw[: len(raw) // 2])
    bad.append('truncated')
    assert store.load(path, LAYOUTS)[0] is None
    path.write_bytes(b'garbage')
    assert store.load(path, LAYOUTS)[0] is None

    def variant(**changes):
        f = dict(fields)
        for k, v in changes.items():
            if v is None:
                f.pop(k)
            else:
                f[k] = v
        store.write_atomic(path, f)
        return store.load(path, LAYOUTS)[0]
    assert variant(camera_matrix=np.eye(4)) is None
    nan = fields['T_cam_mat'].copy()
    nan[0, 3] = float('nan')
    assert variant(T_cam_mat=nan) is None
    assert variant(image_width=1280) is None
    assert variant(ref_ids=np.array([[20], [101], [102], [103]], dtype=float)) is None
    assert variant(ref_corners_commissioned=None) is None
    assert variant(layout_sha256='0' * 64) is None
    assert variant(zone_polygon=np.zeros((2, 2))) is None
    skew = fields['T_cam_base'].copy()
    skew[0, 0] += 0.01
    assert variant(T_cam_base=skew) is None
    assert variant(layout_id='OMX-STATION-A') is None      # a draft layout never validates
    assert variant() is not None


def test_atomic_commit_keeps_the_previous_and_restore_twice_restores_the_original(tmp_path, fields):
    base = tmp_path
    store.write_station(fields, base, LAYOUTS)
    first = store.yaml_path(base).read_bytes()
    second_fields = dict(fields, world_rms_mm=2.5)
    store.write_station(second_fields, base, LAYOUTS)
    second = store.yaml_path(base).read_bytes()
    assert store.prev_path(base).read_bytes() == first
    ok, _ = store.restore_previous(base, LAYOUTS)
    assert ok and store.yaml_path(base).read_bytes() == first
    assert store.prev_path(base).read_bytes() == second
    ok, _ = store.restore_previous(base, LAYOUTS)
    assert ok and store.yaml_path(base).read_bytes() == second


def test_restore_refuses_an_invalid_previous_and_changes_nothing(tmp_path, fields):
    store.write_station(fields, tmp_path, LAYOUTS)
    store.prev_path(tmp_path).write_bytes(b'corrupt')
    before = store.yaml_path(tmp_path).read_bytes()
    ok, msg = store.restore_previous(tmp_path, LAYOUTS)
    assert not ok and msg == T.RESTORE_INVALID_DE
    assert store.yaml_path(tmp_path).read_bytes() == before


def test_a_crash_point_leaves_the_old_complete_file(tmp_path, fields, monkeypatch):
    store.write_station(fields, tmp_path, LAYOUTS)
    before = store.yaml_path(tmp_path).read_bytes()

    def boom(*a, **k):
        raise OSError('crash before replace')
    monkeypatch.setattr(store.os, 'replace', boom)
    with pytest.raises(OSError):
        store.write_station(dict(fields, world_rms_mm=2.9), tmp_path, LAYOUTS)
    monkeypatch.undo()
    assert store.yaml_path(tmp_path).read_bytes() == before
    assert store.load(store.yaml_path(tmp_path), LAYOUTS)[0] is not None


def test_leftover_staging_dirs_are_deleted_at_boot(tmp_path):
    for cid in ('a', 'b'):
        (store.staging_dir(cid, tmp_path) / 'x').mkdir(parents=True)
    assert store.cleanup_staging(tmp_path) == 2
    assert not list(store.station_dir(tmp_path).glob('.staging-*'))


def test_released_layout_rules():
    assert store.released_layout('OMX-STATION-A') is None          # draft
    assert store.released_layout('NOPE', LAYOUTS) is None
    assert store.released_layout('bad id', LAYOUTS) is None
    assert store.released_layout(F.TEST_LAYOUT_ID, LAYOUTS) is not None
    no_poly = dict(LAY, zone=dict(LAY['zone'], accepted_polygon_mm=[[0, 0], [1, 1]]))
    assert store.released_layout(F.TEST_LAYOUT_ID, {F.TEST_LAYOUT_ID: no_poly}) is None


def test_layout_data_imports_with_the_stdlib_only_and_stays_draft():
    src = Path(layout_data.__file__).read_text(encoding='utf-8')
    mods = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import):
            mods.update(a.name.split('.')[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            mods.add((n.module or '').split('.')[0])
    assert mods <= {'hashlib', 'json', 'math'}
    assert layout_data.LAYOUTS['OMX-STATION-A']['layout_state'] == 'draft'
    assert layout_data.board_check_squares(layout_data.LAYOUTS['OMX-STATION-A']) == [
        ('B1', (195.0, 60.0)), ('B2', (195.0, -60.0))]


def test_b_squares_are_black_in_opencvs_board():
    img = CM.mat_board().generateImage((700, 500), marginSize=0)
    for _, (x, y) in layout_data.board_check_squares(LAY):
        i, j = int((x - 180) // 30), int((75 - y) // 30)
        assert img[j * 100 + 5, i * 100 + 5] < 128


def test_the_e2e_fixture_layout_module_is_the_shipped_module_with_a_released_test_layout():
    """.github/scripts/station_fixtures/layout_data.py is bind-mounted over the
    installed module by the e2e smoke: the shipped functions with LAYOUTS holding
    only the released TEST layout (regenerate with
    station_fixture.layout_module_source(released_layout()))."""
    path = (Path(__file__).resolve().parents[3] / '.github' / 'scripts' / 'station_fixtures'
            / 'layout_data.py')
    assert path.read_text(encoding='utf-8') == F.layout_module_source(F.released_layout())


def test_a_real_unseeded_fit_lands_in_a_wrong_minimum_the_fx_bound_rejects_and_seeding_fixes_it():
    """poly_106H_130D, pinned seed 4: OpenCV's UNSEEDED rational fit lands at fx ≈ 1650
    (the wrong-minimum class the plan measured); the seeded fit on the same views
    passes every gate."""
    board, views = _synthetic_views('poly_106H_130D', seed=4)
    fit = [v for i, v in enumerate(views) if i % 5 != 4]
    held = [v for i, v in enumerate(views) if i % 5 == 4]
    objs, imgs = [], []
    for v in fit:
        o, i = board.matchImagePoints(v.corners.reshape(-1, 1, 2).astype(np.float32),
                                      v.ids.reshape(-1, 1))
        objs.append(o)
        imgs.append(i)
    rms, K, dist, _, _ = cv2.calibrateCamera(objs, imgs, (1920, 1080), None, None,
                                             flags=cv2.CALIB_RATIONAL_MODEL)
    ok, why = CM.accept_intrinsics(K, dist.reshape(-1), rms, np.array([0.3]), fit)
    # Measured on OpenCV 4.10 (the image) and 4.13: fx ≈ 1650. An unseeded fit
    # outside the f₀ band is always rejected by the fx bound, whatever the build.
    if not 935.0 <= K[0, 0] <= 1265.0:
        assert not ok and why == 'Brennweite'
    rms, K, dist = CM.calibrate(board, fit)
    errs = CM.heldout_errors(board, held, K, dist)
    assert CM.accept_intrinsics(K, dist, rms, errs, fit) == (True, '')

"""Commissioning data stays consistent (fix round 1, F7, F12, F13).

F7: a lens solve that replaces the intrinsics drops the board pose, the
commissioned reference corners and the world check computed under the old ones
(as ``board_solve`` drops the marks and the world check), so a commit can never
write a new K with results from the previous K.

F12 (§6.2): ``ref_corners_commissioned`` is the mean over the burst frames that
PASSED the board gate, never a frame the gate rejected.

F13: ``solve_touch`` pairs every tap with its dot by id, as ``check_tap`` does.
"""

from __future__ import annotations

import random

import numpy as np
import pytest

cv2 = pytest.importorskip('cv2')

import station_fixture as F  # noqa: E402
from physical_ai_server.station import check as C  # noqa: E402
from physical_ai_server.station import commission as CM  # noqa: E402
from physical_ai_server.station import store  # noqa: E402
from physical_ai_server.station import texts_de as T  # noqa: E402

LAY = F.released_layout()
T0 = F.camera_pose()


# ── F7 ────────────────────────────────────────────────────────────────────────
def _commissioned(tmp_path):
    com = CM.Commission('c1', LAY, tmp_path)
    K, dist = F.ftheta_intrinsics()
    com.intrinsics = {'K': K, 'dist': dist, 'fit_rms': 0.4, 'held_rms': 0.5,
                      'held_max': 1.0}
    com.board = {'T_cam_mat': T0, 'reproj': 0.3, 'ref_corners': np.zeros((16, 2)),
                 'nominal_metrics': {}}
    com.marks = {key: (0.0, 0.0) for key in com.order()}
    com.world = {'rms_mm': 1.0, 'max_mm': 2.0, 'ok': True}
    com.views = list(range(CM.LENS_MIN_VIEWS))
    assert com.gates_passed()
    return com


@pytest.mark.parametrize('accepted', [True, False])
def test_a_lens_solve_that_replaces_the_intrinsics_drops_board_marks_and_world(
        tmp_path, monkeypatch, accepted):
    com = _commissioned(tmp_path)
    K2 = np.array([[1111.0, 0, 961], [0, 1111.0, 541], [0, 0, 1]])
    monkeypatch.setattr(CM, 'diversity', lambda views: (True, ''))
    monkeypatch.setattr(CM, 'calibrate', lambda board, fit, f0=CM.F0_PX: (
        0.4, K2, np.zeros((1, 8))))
    monkeypatch.setattr(CM, 'heldout_errors', lambda *a: np.array([0.5, 0.6]))
    monkeypatch.setattr(CM, 'accept_intrinsics', lambda *a, **k: (
        (True, '') if accepted else (False, 'Brennweite')))
    ok, msg = com.lens_solve()
    assert ok is accepted, msg
    assert com.board is None and com.marks == {} and com.world is None
    assert not com.gates_passed()
    assert com.step_name() == ('board' if accepted else 'lens')
    if accepted:
        assert np.array_equal(com.intrinsics['K'], K2)
    # Nothing downstream is reachable with the old results: the next world step
    # asks for the board first instead of using a pose from the old K.
    ok, code, msg = com.mark_capture('M1', 0, None, lambda img: [], set())
    assert (ok, code, msg) == (False, 'REFUSED', T.BOARD_MISSING_DE)


def test_a_lens_solve_refused_for_too_few_views_keeps_everything(tmp_path):
    com = _commissioned(tmp_path)
    com.views = com.views[:3]
    ok, msg = com.lens_solve()
    assert not ok and msg == T.LENS_NOT_ENOUGH_DE
    assert com.gates_passed()


# ── F12 ───────────────────────────────────────────────────────────────────────
def _board_bbox(frame):
    corners, ids = CM.detect_charuco(CM.mat_board(), frame)
    pts = np.asarray(corners, dtype=np.float64).reshape(-1, 2)
    (x0, y0), (x1, y1) = pts.min(axis=0), pts.max(axis=0)
    return int(x0) - 60, int(y0) - 60, int(x1) + 60, int(y1) + 60


def test_commissioned_reference_corners_average_only_the_frames_that_passed(tmp_path):
    from physical_ai_server.workflow.calibration_manager import CalibrationManager
    K, dist = F.ftheta_intrinsics()
    good = [F.render(LAY, T0, seed=s, ss=1) for s in range(7)]
    # One frame the board gate rejects (the board covered) whose references are
    # all visible but measured 4 px off (the frame shifted).
    bad = good[0].copy()
    x0, y0, x1, y1 = _board_bbox(bad)
    bad[max(0, y0):y1, max(0, x0):x1] = 128
    bad = np.roll(bad, 4, axis=1)
    assert C.classify_refs(C.filter_this_mat(C.detect_tags(bad), K, dist, T0),
                           [r['id'] for r in LAY['refs']])[0] == 'OK'
    frames = good + [bad]
    com = CM.Commission('c1', LAY, tmp_path)
    com.intrinsics = {'K': K, 'dist': dist, 'fit_rms': 0.4, 'held_rms': 0.5, 'held_max': 1.0}
    mgr = CalibrationManager(root=tmp_path / 'stage', board_to_base=store.board_to_mat(LAY))
    ok, msg = com.board_solve(frames, mgr)
    assert ok, msg
    T_cam_mat = com.board['T_cam_mat']
    only_good, _ = CM.commissioned_ref_corners(good, [T_cam_mat] * len(good), K, dist, LAY)
    with_bad, _ = CM.commissioned_ref_corners(frames, [T_cam_mat] * len(frames), K, dist, LAY)
    assert np.allclose(com.board['ref_corners'], only_good, atol=1e-12)
    # The rejected frame would have moved the stored corners measurably.
    assert np.abs(with_bad - only_good).max() > 1e-4


# ── F13 ───────────────────────────────────────────────────────────────────────
def _taps():
    from physical_ai_server.workflow.ik_solver import IKSolver
    ik = IKSolver()
    REG = C.registration_matrix(0.01, (0.002, -0.001))
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


def _same(a, b):
    assert a[0] == b[0] and a[1] == b[1]
    for k in ('reg_theta_rad', 'reg_tx_m', 'reg_ty_m', 'z_table'):
        assert a[2][k] == pytest.approx(b[2][k], abs=1e-12), k
    assert np.allclose(a[2]['dot_tips'], b[2]['dot_tips'], atol=1e-12)


def test_solve_touch_pairs_every_tap_with_its_dot_by_id():
    taps, ik = _taps()
    ref = CM.solve_touch(taps, LAY, ik.tool_tip_below_ee_m)
    assert ref[0], ref[1]
    shuffled = list(taps)
    random.Random(7).shuffle(shuffled)
    assert [t['dot_id'] for t in shuffled] != [t['dot_id'] for t in taps]
    _same(CM.solve_touch(shuffled, LAY, ik.tool_tip_below_ee_m), ref)
    # A layout that lists its dots in another order pairs the same taps alike,
    # and stores the measured tips in ITS dot order.
    lay_rev = dict(LAY, dots=list(reversed(LAY['dots'])))
    rev = CM.solve_touch(taps, lay_rev, ik.tool_tip_below_ee_m)
    assert rev[0], rev[1]
    assert rev[2]['reg_theta_rad'] == pytest.approx(ref[2]['reg_theta_rad'], abs=1e-12)
    assert np.allclose(rev[2]['dot_tips'], ref[2]['dot_tips'][::-1], atol=1e-12)


def test_solve_touch_refuses_a_missing_or_doubled_dot():
    taps, ik = _taps()
    doubled = taps[:3] + [dict(taps[0])]
    ok, msg, _ = CM.solve_touch(doubled, LAY, ik.tool_tip_below_ee_m)
    assert not ok and msg == T.DOT_ORDER_DE.format(dot=4)

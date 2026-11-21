#!/usr/bin/env python3
#
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The handheld lens calibration (spec 40 S4, decision D9's fallback; WP-15): the guided capture (18 views in at
least 9 cells of a 4 × 3 image grid, two size classes), its lens-only solve and gates, the result file, the
service step, the arm check that follows it, and the node's claim of the calibration mode.

The views are synthetic: the clip-on target's 36 corners seen by a known Kannala–Brandt camera at chosen plate
poses, each tag's four corners rolled by one place (A14: the detector's order is not the model's). Three looks
per view at autocal_sim's full pixel noise, 0.15 px per corner and look, as the arm check's capture tests use
(``QUIET`` of ``test_autocal_service``): the capture's stillness rule judges each tag's centre (conductor D89-2;
judged per corner, 0.15 px broke it for most tags).
"""

from __future__ import annotations

import ast
import json
import math
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import numpy as np
import pytest

import autocal_sim as S
from physical_ai_server.workflow import scene_fixture as SF
from physical_ai_server.workflow.autocal import arm_check as AC
from physical_ai_server.workflow.autocal import chain as CH
from physical_ai_server.workflow.autocal import handheld as HH
from physical_ai_server.workflow.autocal import service as SV
from physical_ai_server.workflow.autocal import tags as TG
from physical_ai_server.workflow.autocal import texts_de as T
from physical_ai_server.workflow.autocal.camera_model import KB
from test_autocal_service import _NODE_SRC, CAMERA_STATE, KINDS, _Camera, _Env, _stored, _StubNode, _plate_moved

W, H = 1920, 1080
CAM_ID = CAMERA_STATE['profile_id']       # the streaming camera's id, the one _result_file's lens was made under
CELL_W, CELL_H = W / HH.GRID_COLS, H / HH.GRID_ROWS
LOOK_NOISE_PX = S.SIG_PX
XT = AC.target_model('v1')[1]          # (36, 3): the plate, the same for both arms
TRUTH = KB(1100.0, 1.0, 975.0, 528.0, 0.06, -0.08)
# 18 views in 10 cells: both size classes in the top and bottom rows, the small class in two middle cells.
TEN_CELLS = [((c, r), s) for r in (0, 2) for c in range(4) for s in (0, 1)] + [((1, 1), 0), ((2, 1), 0)]


def _ids(kind='v1'):
    return SF.ARMS[kind]['target_tag_ids']


def _capture(kind='v1'):
    return HH.Capture(kind, looks=SV.CAPTURE_LOOKS, stable_px=SV.LOOK_STABLE_PX)


# ══════════════════════════════════════════════════════════════════════════
# the synthetic hand
# ══════════════════════════════════════════════════════════════════════════

def plate_pose(cam, u, v, d, tilt_deg, tilt_dir, spin):
    """The plate in the camera frame (``X_c = R·X + t``): its centre ``d`` metres along the ray of pixel
    ``(u, v)``, facing the camera, tilted by ``tilt_deg`` about an in-plane axis at ``tilt_dir`` and turned by
    ``spin`` about its normal."""
    ray = cam.unproject(np.array([u, v], dtype=np.float64))
    z = -ray
    x = np.cross((0.0, 1.0, 0.0), z)
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    tilt = math.radians(tilt_deg)
    R = (np.stack([x, y, z], axis=1) @ CH.rodrigues((tilt * math.cos(tilt_dir), tilt * math.sin(tilt_dir), 0.0))
         @ CH.rodrigues((0.0, 0.0, spin)))
    return R, d * ray


def _corners(cam, pose):
    R, t = pose
    return cam.project(XT @ R.T + t)


def _edge(uv):
    q = np.asarray(uv).reshape(9, 4, 2)
    return float(np.median(np.linalg.norm(np.roll(q, -1, axis=1) - q, axis=2).mean(axis=1)))


def pose_in(cam, rng, cell, size):
    """A plate pose whose centre lies inside ``cell`` (well away from its borders), whose tags are all in the
    image and whose median tag edge is of the size class ``size`` (0: < 45 px, 1: >= 45 px)."""
    col, row = cell
    for _ in range(2000):
        u = (col + rng.uniform(0.25, 0.75)) * CELL_W
        v = (row + rng.uniform(0.25, 0.75)) * CELL_H
        d = rng.uniform(0.55, 0.70) if size == 0 else rng.uniform(0.26, 0.36)
        pose = plate_pose(cam, u, v, d, rng.uniform(20.0, 40.0), rng.uniform(0.0, 2 * math.pi),
                          rng.uniform(-math.pi, math.pi))
        uv = _corners(cam, pose)
        if S.visible(uv) and (_edge(uv) >= HH.SIZE_CLASS_EDGE_PX) == (size == 1):
            return pose
    raise AssertionError(f'no pose for {cell}, size class {size}')


def look(cam, pose, rng, kind='v1', noise=LOOK_NOISE_PX, ids=None):
    """One look of the detector: ``{tag id: (4, 2) corners}``, each tag's corners rolled by one place; an id
    ``None`` in ``ids`` is a tag not seen."""
    uv = _corners(cam, pose) + rng.normal(0.0, noise, (36, 2))
    ids = _ids(kind) if ids is None else ids
    return {tid: np.roll(uv[4 * k:4 * k + 4], 1, axis=0) for k, tid in enumerate(ids) if tid is not None}


def offer_still(capture, cam, pose, rng, looks=3, **kw):
    """Hold the plate still for ``looks`` looks; True when one of them captured a view."""
    return any([capture.offer(look(cam, pose, rng, **kw)) for _ in range(looks)])


def ten_cell_views(cam, rng, kind='v1'):
    capture = _capture(kind)
    for cell, size in TEN_CELLS:
        assert offer_still(capture, cam, pose_in(cam, rng, cell, size), rng, kind=kind), (cell, size)
    return capture


# ══════════════════════════════════════════════════════════════════════════
# the capture
# ══════════════════════════════════════════════════════════════════════════

def test_a_moving_target_is_not_captured():
    """FAILS FIRST: a plate that moves 1 px per look is never still over three looks and is not captured; held
    still (its corners move less than the arm check's 0.4 px between two looks) it is."""
    rng = np.random.default_rng(11)
    capture = _capture()
    R, t = pose_in(TRUTH, rng, (1, 1), 1)
    step = np.array([t[2] / TRUTH.f, 0.0, 0.0])                 # about 1 px in the image per look
    for k in range(12):
        assert not capture.offer(look(TRUTH, (R, t + k * step), rng))
    assert capture.views == [] and capture.progress()['views'] == 0
    drift = 0.15 * step                                         # 0.15 px per look: still
    assert [capture.offer(look(TRUTH, (R, t + k * drift), rng)) for k in range(3)] == [False, False, True]
    assert len(capture.views) == 1


@pytest.mark.parametrize('kind', KINDS)
def test_a_still_plate_at_full_detector_noise_finishes_the_capture(kind):
    """FAILS FIRST (S4, conductor D89-2): the plate held still for three looks per view of ``TEN_CELLS`` at
    autocal_sim's full pixel noise (0.15 px per corner and look): the capture ends (18 views in at least 9
    cells) in at least 39 of 41 runs. Judged by the largest of a tag's four corner moves it ended in none of
    them (0–3 views per run, WP-15)."""
    finished = 0
    for seed in list(range(40)) + [2026]:
        rng = np.random.default_rng(seed)
        capture = _capture(kind)
        for cell, size in TEN_CELLS:
            pose = pose_in(TRUTH, rng, cell, size)
            for _ in range(3):
                capture.offer(look(TRUTH, pose, rng, kind=kind, noise=S.SIG_PX))
        finished += capture.finished
    assert finished >= 39, finished


@pytest.mark.parametrize('motion', ('shift', 'turn'))
@pytest.mark.parametrize('kind', KINDS)
def test_a_plate_moving_half_a_pixel_per_look_is_never_captured(kind, motion):
    """The centre rule keeps its grip on a real motion: a plate that moves 0.5 px per look — shifted, or turned
    about its centre so that the outer tags' centres move 0.5 px — gives no view in any cell, at full noise."""
    rng = np.random.default_rng(31)
    capture = _capture(kind)
    for cell, size in TEN_CELLS:
        pose = pose_in(TRUTH, rng, cell, size)
        for k in range(12):
            assert not capture.offer(_plate_moved(_ids(kind), look(TRUTH, pose, rng, kind=kind), k, motion))
    assert capture.views == []


def test_both_captures_judge_stillness_with_the_one_helper():
    """D89-2: the stillness of two looks is written once, ``arm_check.still_between``; the arm check's capture
    (``service._target_look``) and the handheld capture (``Capture.offer``) call it and measure no corner
    themselves."""
    for module, name in ((SV, '_target_look'), (HH, 'offer')):
        with open(module.__file__, encoding='utf-8') as f:
            tree = ast.parse(f.read())
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
        calls = {ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)}
        assert '_ac.still_between' in calls, module.__name__
        assert not [c for c in calls if 'norm' in c or c.endswith(('.max', '.mean'))], (module.__name__, calls)


def test_the_same_cell_and_size_class_twice_counts_once():
    """FAILS FIRST: a second still view whose plate centre falls into a filled cell in the same size class is no
    view; the other size class in that cell is one."""
    rng = np.random.default_rng(12)
    capture = _capture()
    assert offer_still(capture, TRUTH, pose_in(TRUTH, rng, (2, 1), 0), rng)
    assert not offer_still(capture, TRUTH, pose_in(TRUTH, rng, (2, 1), 0), rng)
    assert len(capture.views) == 1
    assert offer_still(capture, TRUTH, pose_in(TRUTH, rng, (2, 1), 1), rng)
    assert not offer_still(capture, TRUTH, pose_in(TRUTH, rng, (2, 1), 1), rng)
    assert [v.slot for v in capture.views] == [(2, 1, 0), (2, 1, 1)]
    assert capture.progress()['cells'] == 1 and capture.progress()['views'] == 2


def test_eighteen_views_in_four_cells_do_not_finish():
    """FAILS FIRST: 18 still views whose plate centres fall into 4 cells (both size classes) end nothing: at most
    eight of them are views, and four cells are fewer than nine."""
    rng = np.random.default_rng(13)
    capture = _capture()
    cells = [(0, 0), (3, 0), (0, 2), (3, 2)]
    for k in range(18):
        offer_still(capture, TRUTH, pose_in(TRUTH, rng, cells[k % 4], (k // 4) % 2), rng)
    assert len(capture.views) == 8
    assert not capture.finished
    assert capture.progress()['cells'] == 4


def test_a_view_needs_six_of_this_arm_s_target_tags():
    """Five target tags are no view; the other arm's target is no view; the mat's tags never count."""
    rng = np.random.default_rng(14)
    capture = _capture()
    pose = pose_in(TRUTH, rng, (1, 0), 1)
    assert not offer_still(capture, TRUTH, pose, rng, ids=list(_ids('v1')[:5]) + [None] * 4)
    assert not offer_still(capture, TRUTH, pose, rng, ids=_ids('omx'))
    assert not offer_still(capture, TRUTH, pose, rng, ids=sorted(SF.ARMS['v1']['mat_tags_f'])[:9])
    assert capture.views == []
    assert offer_still(capture, TRUTH, pose, rng, ids=list(_ids('v1')[:6]) + [None] * 3)


def test_eighteen_views_in_ten_cells_end_the_capture_and_no_view_follows():
    rng = np.random.default_rng(15)
    capture = ten_cell_views(TRUTH, rng)
    assert len(capture.views) == 18 and capture.finished
    assert not offer_still(capture, TRUTH, pose_in(TRUTH, rng, (0, 1), 0), rng)
    p = capture.progress()
    assert (p['views'], p['views_needed'], p['cells'], p['cells_needed'], p['grid']) == (18, 18, 10, 9, [4, 3])
    assert sorted(map(tuple, p['filled'])) == sorted(v.slot for v in capture.views)


# S4's numbers, written as literals.
_NUMBERS = (('GRID_COLS', 4), ('GRID_ROWS', 3), ('SIZE_CLASS_EDGE_PX', 45.0), ('VIEWS_REQUIRED', 18),
            ('CELLS_REQUIRED', 9), ('RMS_MAX_PX', 0.5), ('FILE_NAME', 'intrinsics_handheld.json'))


@pytest.mark.parametrize('name,value', _NUMBERS)
def test_the_handheld_numbers_are_s4_s(name, value):
    assert getattr(HH, name) == value


# ══════════════════════════════════════════════════════════════════════════
# the solve and its gates
# ══════════════════════════════════════════════════════════════════════════

def test_eighteen_views_in_ten_cells_recover_the_lens():
    """FAILS FIRST: 18 views in 10 cells of a camera with f = 1100, k = (0.06, −0.08) and its principal point
    away from the image centre: f within 0.5 % and the principal point within 3 px, every gate passed."""
    rng = np.random.default_rng(2026)
    capture = ten_cell_views(TRUTH, rng)
    assert len(capture.views) == 18 and len(capture.cells) == 10 and capture.finished
    result = HH.solve(capture.views)
    assert result.ok and result.code == 'ok', (result.code, result.kb, result.rms_px)
    assert result.message_de == T.HANDHELD_DONE_DE
    assert abs(result.kb['f'] - TRUTH.f) / TRUTH.f <= 0.005
    assert math.hypot(result.kb['cx'] - TRUTH.cx, result.kb['cy'] - TRUTH.cy) <= 3.0
    assert result.rms_px <= HH.RMS_MAX_PX
    assert all(result.gates.values())


def test_a_view_that_does_not_fit_fails_the_rms_gate():
    rng = np.random.default_rng(16)
    capture = ten_cell_views(TRUTH, rng)
    bent = capture.views[4]
    obs = bent.obs.copy()
    obs[bent.mask] += np.random.default_rng(1).normal(0.0, 4.0, (int(bent.mask.sum()), 2))
    views = list(capture.views)
    views[4] = bent._replace(obs=obs)
    result = HH.solve(views)
    assert not result.ok and result.code == 'rms' and result.rms_px > HH.RMS_MAX_PX
    assert result.message_de == T.HANDHELD_FAILED_DE


@pytest.mark.parametrize('lens', [KB(1180.0, 1.0, 959.5, 539.5, 0.03, -0.05),      # f outside LENS_BAND
                                  KB(1040.0, 1.0, 1040.0, 539.5, 0.03, -0.05)])    # principal point 80 px off
def test_a_lens_outside_the_lens_gates_is_refused(lens):
    rng = np.random.default_rng(17)
    result = HH.solve(ten_cell_views(lens, rng).views)
    assert result.rms_px <= HH.RMS_MAX_PX
    assert not result.ok and result.code == 'lens' and result.message_de == T.HANDHELD_FAILED_DE


# The solves run in a fresh interpreter with the machine's own BLAS threads (no thread variable set). Between two
# solves the child waits until no thread but its own uses CPU: a BLAS pool that a call woke keeps its workers
# spinning for a while, so a solve that wakes it is seen as CPU of other threads while it runs.
_BLAS_THREAD_VARS = ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS',
                     'NUMEXPR_NUM_THREADS', 'GOTO_NUM_THREADS')
_SOLVE_CPU_CHILD = textwrap.dedent('''
    import json
    import time

    import numpy as np

    import autocal_sim as S
    import test_autocal_handheld as TH
    from physical_ai_server.workflow.autocal import arm_check as AC
    from physical_ai_server.workflow.autocal import handheld as HH

    capture = TH.ten_cell_views(TH.TRUTH, np.random.default_rng(2026))
    jobs = {'handheld': lambda: HH.solve(capture.views)}
    for kind in ('v1', 'omx'):
        rng = np.random.default_rng([0, 0])
        inp = S.arm_check_inputs(S.sample_kit(kind, rng, S.Options()), rng, S.Options())
        jobs['arm check ' + kind] = lambda inp=inp, kind=kind: AC.run(
            inp.records, inp.looks, profile_id=S.PROFILE[kind], image_size=(S.WIDTH, S.HEIGHT),
            camera_profile_id='3efca10f3a05', lens_prior=None)

    def quiet():
        """Until no other thread used CPU for 0.25 s (at most 10 s)."""
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            p0, t0 = time.process_time(), time.thread_time()
            time.sleep(0.25)
            if (time.process_time() - p0) - (time.thread_time() - t0) < 0.005:
                return

    out = {}
    for name, job in jobs.items():
        quiet()
        p0, t0, w0 = time.process_time(), time.thread_time(), time.perf_counter()
        job()
        out[name] = [time.process_time() - p0, time.thread_time() - t0, time.perf_counter() - w0]
    print(json.dumps(out))
''')


def test_the_autocal_solves_keep_to_their_own_thread():
    """FIX-K-r1 (K-3): the handheld solve and the arm check's solve use the CPU of the thread they run on and
    wake no BLAS thread pool. The handheld solve's normal matrix is 114 × 114 (18 views); LAPACK's Cholesky of
    that size woke OpenBLAS's pool, whose workers then spun on every core. Measured before the fix on 18 cores:
    2.9 s of process CPU for 0.2 s of the solve's own, and 1.7–2.4 s of wall time instead of 0.20–0.23 s beside
    16 busy processes."""
    env = {k: v for k, v in os.environ.items() if k not in _BLAS_THREAD_VARS}
    here = Path(__file__).resolve().parent
    env['PYTHONPATH'] = os.pathsep.join([str(here), str(here.parent)] + sys.path)
    child = subprocess.run([sys.executable, '-c', _SOLVE_CPU_CHILD], env=env, capture_output=True, text=True,
                           timeout=300, cwd=str(here.parent))
    assert child.returncode == 0, child.stderr
    measured = json.loads(child.stdout.strip().splitlines()[-1])
    assert list(measured) == ['handheld', 'arm check v1', 'arm check omx']
    for name, (process_s, thread_s, wall_s) in measured.items():
        other_s = process_s - thread_s
        print(f'{name}: {thread_s:.3f} s on its thread, {other_s:.3f} s on others, {wall_s:.3f} s wall')
        assert other_s <= 0.25 * thread_s + 0.02, (name, process_s, thread_s, wall_s)


# ══════════════════════════════════════════════════════════════════════════
# the result file
# ══════════════════════════════════════════════════════════════════════════

def _result_file(lens=TRUTH, image_size=(W, H)):
    kb = {'f': lens.f, 'aspect': lens.aspect, 'cx': lens.cx, 'cy': lens.cy, 'k1': lens.k1, 'k2': lens.k2}
    return {'v': 1, 'image_size': list(image_size), 'camera_profile_id': '0123456789ab',
            'created_utc': '2026-11-20T09:00:00Z', 'kb': kb, 'quality': {'views': 18, 'cells': 10, 'rms_px': 0.1}}


def test_the_result_is_written_beside_the_arm_check_read_back_and_used_up(tmp_path):
    data = _result_file()
    HH.write(str(tmp_path), data)
    assert (tmp_path / 'calibration' / 'intrinsics_handheld.json').is_file()
    assert HH.read(str(tmp_path), (W, H), camera_profile_id=CAM_ID) == data
    assert HH.read(str(tmp_path), (640, 480), camera_profile_id=CAM_ID) is None   # another image size: no lens of it
    assert HH.read(str(tmp_path), (W, H), camera_profile_id='ffffffffffff') is None   # made under another camera id
    assert HH.read(str(tmp_path), (W, H), camera_profile_id=None) is None             # the camera's id unknown
    HH.discard(str(tmp_path))
    assert HH.read(str(tmp_path), (W, H), camera_profile_id=CAM_ID) is None
    HH.discard(str(tmp_path))                                      # nothing there: nothing happens


def _drop_k2(data):
    del data['kb']['k2']


_BROKEN = {
    'version': lambda d: d.update(v=2),
    'kb_incomplete': _drop_k2,
    'kb_not_finite': lambda d: d['kb'].update(f=float('nan')),
    'kb_not_a_dict': lambda d: d.update(kb=[1100.0]),
    'no_image_size': lambda d: d.update(image_size=None),
    'no_camera_profile_id': lambda d: d.update(camera_profile_id=None),
    'camera_profile_id_not_a_text': lambda d: d.update(camera_profile_id=123456789012),
}


@pytest.mark.parametrize('name', sorted(_BROKEN))
def test_a_malformed_result_reads_as_none(tmp_path, name):
    data = _result_file()
    _BROKEN[name](data)
    path = tmp_path / 'calibration' / HH.FILE_NAME
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(data), encoding='utf-8')
    assert HH.read(str(tmp_path), (W, H), camera_profile_id=CAM_ID) is None


def test_result_dict_carries_the_lens_and_the_quality():
    rng = np.random.default_rng(19)
    capture = ten_cell_views(TRUTH, rng)
    result = HH.solve(capture.views)
    data = HH.result_dict(result, capture, camera_profile_id='0123456789ab', image_size=(W, H),
                          created_utc='2026-11-20T09:00:00Z')
    assert data == {'v': 1, 'image_size': [W, H], 'camera_profile_id': '0123456789ab',
                    'created_utc': '2026-11-20T09:00:00Z', 'kb': result.kb,
                    'quality': {'views': 18, 'cells': 10, 'rms_px': result.rms_px}}


# ══════════════════════════════════════════════════════════════════════════
# the service step
# ══════════════════════════════════════════════════════════════════════════

class _Mode:
    """The node's calibration-mode claim, recorded."""

    def __init__(self, answer=(True, '')):
        self.answer, self.calls = answer, []

    def claim(self):
        self.calls.append('claim')
        return self.answer

    def release(self):
        self.calls.append('release')


def _kit_lens(env):
    c = env.kit.cam                       # QUIET: no decentering, so this is the kit's whole lens
    return KB(c.f, c.a, c.cx, c.cy, c.k1, c.k2)


def _handheld_env(kind, tmp_path, mode=None, **kw):
    """The service on a simulated kit whose detector sees the plate wherever ``env.hand`` holds it."""
    env = _Env(kind, calib_dir=tmp_path, **kw)
    env.mode = mode or _Mode()
    env.svc._claim_mode, env.svc._release_mode = env.mode.claim, env.mode.release
    env.cam = _kit_lens(env)
    env.hand = None
    env.hand_rng = np.random.default_rng(21)

    def detect(_frame, roi=None):
        env.detects.append(roi)
        seen = {} if env.hand is None else look(env.cam, env.hand, env.hand_rng, kind=kind)
        return TG.Frame(W, H, 120.0, {tid: TG.TagObs(c, 80.0) for tid, c in seen.items()})
    env.svc._detect = detect
    return env


def _hold(env, pose, ticks=3):
    env.hand = pose
    for _ in range(ticks):
        env.clock.advance(SV.CAPTURE_PERIOD_S)
        env.svc.handheld_tick()


def _all_views(env, rng):
    for cell, size in TEN_CELLS:
        _hold(env, pose_in(env.cam, rng, cell, size))


@pytest.mark.parametrize('kind', KINDS)
def test_the_handheld_step_claims_the_mode_holds_the_camera_and_counts_the_views(kind, tmp_path):
    """FAILS FIRST: /calibration/start step handheld claims the node's calibration mode, holds the studio camera,
    starts the capture thread and states handheld_running with S4's sentence; every captured view counts, and
    the capture's stillness rule is the arm check's (three looks, 0.4 px)."""
    env = _handheld_env(kind, tmp_path)
    assert 'handheld' in SV.STEPS
    assert env.svc.start('handheld') == (True, T.handheld_running_de(0))
    assert env.mode.calls == ['claim'] and env.camera.holds == 1
    assert SV.CAPTURE_THREAD in env.threads
    capture = env.svc._handheld.capture
    assert (capture.looks, capture.stable_px) == (SV.CAPTURE_LOOKS, SV.LOOK_STABLE_PX)
    state = env.svc.state()
    assert (state['state'], state['code'], state['message_de']) == (
        'handheld_running', 'handheld_running', T.handheld_running_de(0))
    assert state['handheld']['views'] == 0 and state['handheld']['grid'] == [4, 3]
    _hold(env, pose_in(env.cam, np.random.default_rng(22), (0, 0), 1))
    state = env.svc.state()
    assert state['message_de'] == T.handheld_running_de(1) and state['handheld']['views'] == 1
    assert state['handheld']['filled'] == [[0, 0, 1]]
    assert set(env.detects) == {None}                               # full-frame looks: the hand is anywhere


@pytest.mark.parametrize('kind', KINDS)
def test_a_full_handheld_run_writes_the_lens_and_asks_for_the_arm_check(kind, tmp_path):
    """FAILS FIRST: 18 views in 10 cells -> the solve on a worker thread -> intrinsics_handheld.json with the
    camera's lens; the state says „Kamera vermessen. Bitte jetzt den Arm-Check starten."; camera and mode
    released."""
    env = _handheld_env(kind, tmp_path)
    assert env.svc.start('handheld')[0]
    _all_views(env, np.random.default_rng(23))
    assert env.threads.count(SV.WORK_THREAD) == 1
    got = HH.read(str(tmp_path), (W, H), camera_profile_id=CAM_ID)
    assert got is not None and got['quality']['views'] == 18 and got['quality']['cells'] == 10
    assert abs(got['kb']['f'] - env.cam.f) / env.cam.f <= 0.005
    assert got['camera_profile_id'] == CAMERA_STATE['profile_id']
    state = env.svc.state()
    assert (state['state'], state['code'], state['message_de']) == (
        'needs_arm_check', 'handheld_done', T.HANDHELD_DONE_DE)
    assert state['handheld']['views'] == 18 and state['handheld']['rms_px'] <= HH.RMS_MAX_PX
    assert env.camera.holds == 0 and env.mode.calls == ['claim', 'release']
    assert env.svc.cancel() is None


@pytest.mark.parametrize('kind', KINDS)
def test_a_handheld_run_that_fails_its_gates_writes_nothing_and_says_why(kind, tmp_path):
    """A lens whose focal length lies outside the contract's band: every view is captured, the solve refuses it,
    nothing is written, the state and the Protokoll say „Die Kamera konnte nicht vermessen werden. …"."""
    env = _handheld_env(kind, tmp_path)
    env.cam = KB(1180.0, 1.0, 959.5, 539.5, 0.03, -0.05)
    assert env.svc.start('handheld')[0]
    _all_views(env, np.random.default_rng(24))
    assert HH.read(str(tmp_path), (W, H), camera_profile_id=CAM_ID) is None
    state = env.svc.state()
    assert (state['state'], state['code'], state['message_de']) == (
        'needs_arm_check', 'handheld_failed', T.HANDHELD_FAILED_DE)
    assert env.logs == [T.HANDHELD_FAILED_DE]
    assert env.camera.holds == 0 and env.mode.calls == ['claim', 'release']


@pytest.mark.parametrize('kind', KINDS)
def test_cancel_stops_the_handheld_step_writes_nothing_and_releases_everything(kind, tmp_path):
    env = _handheld_env(kind, tmp_path)
    before = env.svc.state()
    assert env.svc.start('handheld')[0]
    _hold(env, pose_in(env.cam, np.random.default_rng(25), (1, 2), 0))
    assert env.svc.cancel() == (True, SV.HANDHELD_STOPPED_DE)
    assert HH.read(str(tmp_path), (W, H), camera_profile_id=CAM_ID) is None
    assert env.camera.holds == 0 and env.mode.calls == ['claim', 'release']
    assert {k: v for k, v in env.svc.state().items() if k != 'camera'} == {
        k: v for k, v in before.items() if k != 'camera'}
    assert env.svc.cancel() is None
    env.hand = pose_in(env.cam, np.random.default_rng(26), (2, 2), 0)
    env.svc.handheld_tick()                                         # a late tick of the stopped thread does nothing
    assert env.svc.state()['state'] == before['state'] and env.detects == [None] * 3


@pytest.mark.parametrize('kind', KINDS)
def test_the_handheld_step_is_refused_like_the_arm_check(kind, tmp_path):
    """No statement, legacy, no streaming camera: S4's sentences, nothing claimed or held. A refused claim (a
    program, a recording, …) answers its own sentence. A running arm check refuses it, and a running handheld
    step refuses the arm check, the mat check, the target confirmation and a second handheld step."""
    for camera, sentence in ((_Camera(profile=None), T.PROFILE_UNKNOWN_DE),
                             (_Camera(profile='legacy'), T.CAMERA_NOT_TOWER_DE),
                             (_Camera(state=dict(CAMERA_STATE, state='fault')), T.CAMERA_MISSING_DE)):
        env = _handheld_env(kind, tmp_path, camera=camera)
        assert env.svc.start('handheld') == (False, sentence)
        assert env.mode.calls == [] and env.camera.holds == 0
    refusal = 'Ein Workflow läuft gerade — bitte zuerst stoppen.'
    env = _handheld_env(kind, tmp_path, mode=_Mode((False, refusal)))
    assert env.svc.start('handheld') == (False, refusal)
    assert env.camera.holds == 0 and env.svc.state()['state'] == 'unknown'
    assert env.svc.start('handheld') == (False, refusal)            # never "already running"
    env = _handheld_env(kind, tmp_path)
    assert env.svc.start('arm_check')[0]
    assert env.svc.start('handheld') == (False, SV.ARM_CHECK_BUSY_DE)
    assert env.mode.calls == []
    env = _handheld_env(kind, tmp_path / 'b')
    assert env.svc.start('handheld')[0]
    for step in ('arm_check', 'mat_check', 'target_removed', 'handheld'):
        assert env.svc.start(step) == (False, SV.HANDHELD_BUSY_DE), step
    assert env.programs == [] and env.mode.calls == ['claim']


# ══════════════════════════════════════════════════════════════════════════
# the arm check that follows
# ══════════════════════════════════════════════════════════════════════════

def _spy_lens_prior(monkeypatch):
    seen = []
    real = AC.run

    def spy(*args, **kwargs):
        seen.append(kwargs.get('lens_prior'))
        return real(*args, **kwargs)
    monkeypatch.setattr(AC, 'run', spy)
    return seen


@pytest.mark.parametrize('kind', KINDS)
def test_the_next_arm_check_holds_the_handheld_lens_and_a_written_one_uses_it_up(kind, tmp_path, monkeypatch):
    """FAILS FIRST: the arm check after a handheld step solves with that lens as its lens prior; once that arm
    check is written the handheld result is used up and the arm check after it solves the lens itself."""
    env = _Env(kind, calib_dir=tmp_path)
    data = _result_file(_kit_lens(env))
    HH.write(str(tmp_path), data)
    seen = _spy_lens_prior(monkeypatch)
    assert env.svc.start('arm_check')[0]
    assert env.play() == 'finished'
    assert _stored(env).status == 'ok', env.svc.state()
    assert seen == [data]
    assert HH.read(str(tmp_path), (W, H), camera_profile_id=CAM_ID) is None
    assert env.svc.start('arm_check')[0]
    assert env.play() == 'finished'
    assert seen == [data, None]


@pytest.mark.parametrize('seed', range(12))
@pytest.mark.parametrize('kind', KINDS)
def test_a_following_arm_check_stores_the_handheld_lens_unchanged(kind, seed, tmp_path):
    """FAILS FIRST (S4, conductor D89-1): through the service, the handheld step and then the arm check on the
    same kit — the stored arm check's six lens parameters ARE the handheld lens. Held under a σ 0.5 px prior
    instead, the arm check's own data outweighed it: 9 of 12 such arm checks moved f by 0.50–1.37 px (WP-15)."""
    env = _handheld_env(kind, tmp_path, seed=seed)
    assert env.svc.start('handheld')[0]
    _all_views(env, np.random.default_rng([seed, 23]))
    handheld = HH.read(str(tmp_path), (W, H), camera_profile_id=CAM_ID)
    assert handheld is not None, env.svc.state()
    env.svc._detect = env.detect
    assert env.svc.start('arm_check')[0]
    assert env.play() == 'finished'
    got = _stored(env)
    assert got.status == 'ok', env.svc.state()
    assert got.data['kb'] == handheld['kb']


@pytest.mark.parametrize('kind', KINDS)
def test_a_refused_arm_check_keeps_the_handheld_lens_for_the_next_one(kind, tmp_path, monkeypatch):
    env = _Env(kind, calib_dir=tmp_path)
    data = _result_file(_kit_lens(env))
    HH.write(str(tmp_path), data)
    seen = _spy_lens_prior(monkeypatch)
    env.target_hidden_at = set(range(13, 20))
    assert env.svc.start('arm_check')[0]
    assert env.play() == 'finished'
    assert env.svc.state()['code'] == 'too_few_poses'
    assert HH.read(str(tmp_path), (W, H), camera_profile_id=CAM_ID) == data
    env.target_hidden_at = set()
    assert env.svc.start('arm_check')[0]
    assert env.play() == 'finished'
    assert _stored(env).status == 'ok', env.svc.state()
    assert seen == [data, data] and HH.read(str(tmp_path), (W, H), camera_profile_id=CAM_ID) is None


# A handheld lens is held only under the camera_profile_id it was made under (the camera state's profile_id: the
# tower camera's derivation and platform), so the stored id and the streaming camera's must be the same text.
# (stored id, the camera state's id)
_OTHER_CAMERA = {'another_camera': ('ffffffffffff', CAMERA_STATE['profile_id']),
                 'stored_without_id': (None, CAMERA_STATE['profile_id']),
                 'camera_without_id': (CAMERA_STATE['profile_id'], None)}


@pytest.mark.parametrize('case', sorted(_OTHER_CAMERA))
@pytest.mark.parametrize('kind', KINDS)
def test_a_handheld_lens_of_another_camera_is_not_held(kind, case, tmp_path, monkeypatch):
    """FAILS FIRST (verifier finding B-2): a pending handheld lens 3 % off the camera's, made under ANOTHER camera
    profile id (or with the id unknown on either side), is not held in the next arm check — it solves its own lens
    and stores that under the streaming camera's id; the handheld file is left as it was."""
    stored_id, camera_id = _OTHER_CAMERA[case]
    env = _Env(kind, calib_dir=tmp_path, camera=_Camera(state=dict(CAMERA_STATE, profile_id=camera_id)))
    lens = _kit_lens(env)
    data = _result_file(KB(lens.f * 1.03, lens.aspect, lens.cx, lens.cy, lens.k1, lens.k2))
    data['camera_profile_id'] = stored_id
    HH.write(str(tmp_path), data)
    before = (tmp_path / 'calibration' / HH.FILE_NAME).read_bytes()
    seen = _spy_lens_prior(monkeypatch)
    assert env.svc.start('arm_check')[0]
    assert env.play() == 'finished'
    got = _stored(env)
    assert got.status == 'ok', env.svc.state()
    assert seen == [None]
    assert got.data['camera_profile_id'] == camera_id
    assert abs(got.data['kb']['f'] - lens.f) / lens.f <= 0.005
    assert (tmp_path / 'calibration' / HH.FILE_NAME).read_bytes() == before


# ══════════════════════════════════════════════════════════════════════════
# the node: the handheld step claims the calibration mode
# ══════════════════════════════════════════════════════════════════════════

def _mode_node():
    node = _StubNode('_claim_calibration_mode', '_assert_no_other_active', '_set_calibration_active')
    node._mode_lock = threading.Lock()
    for flag in ('on_recording', 'on_inference', 'is_training', 'on_calibration', 'on_workflow', 'on_manual',
                 'on_leader_teach', '_collision_active'):
        setattr(node, flag, False)
    return node


def test_the_node_claims_the_calibration_mode_for_the_handheld_step_alone():
    """The handheld step owns the robot like a calibration step: claimed under _mode_lock, refused by every
    other owner (a program, Handbetrieb, another calibration step) with that owner's sentence."""
    node = _mode_node()
    assert node._claim_calibration_mode() == (True, '')
    assert node.on_calibration is True
    assert node._claim_calibration_mode() == (False, 'Kalibrierung läuft gerade — bitte zuerst beenden.')
    node._set_calibration_active(False)
    node.on_workflow = True
    assert node._claim_calibration_mode() == (False, 'Ein Workflow läuft gerade — bitte zuerst stoppen.')
    assert node.on_calibration is False
    node.on_workflow, node.on_manual = False, True
    assert node._claim_calibration_mode()[0] is False and node.on_calibration is False


def test_the_node_hands_the_service_its_claim_and_its_release():
    fn = next(n for n in ast.walk(ast.parse(_NODE_SRC))
              if isinstance(n, ast.FunctionDef) and n.name == '_build_scene_calibration')
    call = next(n for n in ast.walk(fn)
                if isinstance(n, ast.Call) and ast.unparse(n.func).endswith('SceneCalibration'))
    kw = {k.arg: ast.unparse(k.value) for k in call.keywords}
    assert kw['claim_mode'] == 'self._claim_calibration_mode'
    assert kw['release_mode'] == 'lambda: self._set_calibration_active(False)'


def test_the_new_sentences_follow_the_german_rules():
    import importlib.util
    from pathlib import Path
    repo = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location('_gdl_handheld',
                                                  repo / '.github' / 'scripts' / 'german_detail_lint.py')
    lint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lint)
    for text in (SV.HANDHELD_BUSY_DE, SV.HANDHELD_STOPPED_DE):
        assert not lint.TRANSLITERATIONS.search(text)
        assert lint.GERMAN_CHARS.search(text) or lint.GERMAN_WORDS.search(text)
        assert not lint.ENGLISH_WORDS.search(text) or lint.GERMAN_CHARS.search(text)
        assert '"' not in text and "'" not in text
    assert SV.HANDHELD_BUSY_DE == ('Die Kamera wird gerade von Hand vermessen – bitte zuerst fertig vermessen '
                                   'oder abbrechen.')
    assert SV.HANDHELD_STOPPED_DE == 'Vermessen der Kamera abgebrochen. Es wurde nichts gespeichert.'

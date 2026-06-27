"""„Solange <Typ> sichtbar" loop tests (Roboter Studio multi-object grasping).

Drives the REAL interpreter control branch (_exec_while_visible) with a real
closed-form IKSolver + real ObjectCatalog + a stub Perception, with the body =
the real `edubotics_grasp_object` handler. Asserts the loop's guarantees:
re-detect each pass, claimed ids excluded (no re-grab), empty-debounce (one
occluded frame doesn't end it early), clean termination, and should_stop.
"""

from __future__ import annotations

import math
import threading
import types

import numpy as np
import pytest

from physical_ai_server.workflow import interpreter as interp_mod
from physical_ai_server.workflow import trajectory_builder
from physical_ai_server.workflow.interpreter import Interpreter
from physical_ai_server.workflow.handlers.motion import GRIPPER_OPEN_RAD, HOME_JOINTS_RAD, WorkflowError
from physical_ai_server.workflow.ik_solver import IKSolver
from physical_ai_server.workflow.object_catalog import parse_catalog
from physical_ai_server.workflow.perception import Detection
from physical_ai_server.workflow.tag_pose import tag_corner_object_points


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    # No-op the chunked-publish inter-chunk sleep, and make the loop's
    # empty-debounce wait instant so the suite stays fast.
    state = {'t': 0.0}

    def _mono():
        state['t'] += 1000.0
        return state['t']

    monkeypatch.setattr(trajectory_builder, 'time',
                        types.SimpleNamespace(monotonic=_mono, sleep=lambda _s: None))
    monkeypatch.setattr(interp_mod, 'WHILE_EMPTY_SECONDS', 0.0)
    monkeypatch.setattr(interp_mod, 'WHILE_EMPTY_FRAMES', 2)
    # #3: make the retreat-settle instant so the suite stays fast.
    monkeypatch.setattr(interp_mod, 'WHILE_SETTLE_S', 0.0)
    yield


# ── synthesis (overhead cam) ─────────────────────────────────────────────────
_K = np.array([[600.0, 0, 320.0], [0, 600.0, 240.0], [0, 0, 1]], dtype=np.float64)


def _T():
    R = np.array([[1.0, 0, 0], [0, -1.0, 0], [0, 0, -1.0]], dtype=np.float64)
    T = np.eye(4, dtype=np.float64)
    T[:3, :3] = R
    T[:3, 3] = (0.20, 0.0, 0.50)
    return T


def _rot_z(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=np.float64)


def _project(pts_base, T):
    Tbc = np.linalg.inv(T)
    R, t = Tbc[:3, :3], Tbc[:3, 3]
    out = []
    for c in pts_base:
        cam = R @ c + t
        out.append([_K[0, 0] * cam[0] / cam[2] + _K[0, 2],
                    _K[1, 1] * cam[1] / cam[2] + _K[1, 2]])
    return np.array(out, dtype=np.float64)


def _det(tag_id, P, yaw=0.0, tag=0.024, h=0.040):
    obj = tag_corner_object_points(tag)
    corners = (_rot_z(yaw) @ obj.T).T + np.array([P[0], P[1], h])
    px = _project(corners, _T())
    cpx = _project([np.array([P[0], P[1], h])], _T())[0]
    return Detection(centroid_px=(int(cpx[0]), int(cpx[1])), bbox_px=(0, 0, 1, 1),
                     confidence=1.0, label=f'tag{tag_id}', aruco_id=tag_id, corners_px=px)


class _StubPerception:
    def __init__(self, src):
        self._src = src           # list, or fn(call_index)->list
        self.calls = 0

    def apriltag_available(self):
        return True

    def detect(self, bgr, camera, mode, color=None, coco_class=None, aruco_id=None):
        self.calls += 1
        if mode != 'apriltag':
            return []
        dets = self._src(self.calls) if callable(self._src) else list(self._src)
        if aruco_id is not None:
            dets = [d for d in dets if d.aruco_id == aruco_id]
        return dets


_CATALOG = parse_catalog({
    'tag_size_m': 0.024,
    'types': {'banane': {
        'label_de': 'Banane', 'tag_ids': [20, 21, 22],
        'object_height_m': 0.040, 'grasp_depth_m': 0.015,
        'gripper_close_rad': -0.30, 'approach_clear_m': 0.06,
    }},
})


class _Ctx:
    def __init__(self, perception, stop_after=None, stop_at_claims=None):
        self.ik = IKSolver()
        self.perception = perception
        self.object_catalog = _CATALOG
        self.object_catalog_error = None
        self.scene_intrinsics = {'K': _K, 'dist': np.zeros((5, 1))}
        self.scene_extrinsics = _T()
        self.board_table_z = 0.0
        self.z_table = 0.0
        self.table_plane = None
        self.motion_lock = threading.RLock()
        self.claim_lock = threading.RLock()
        self.claimed_tags = set()
        self.skipped_tags = set()
        self.absent_since = {}
        # No follower-joints readback by default → the grasp-success check returns
        # None and falls back to claim-on-completion (the pre-#2 behaviour).
        self.get_follower_joints = None
        self.last_full_joints = list(HOME_JOINTS_RAD) + [GRIPPER_OPEN_RAD]
        self.last_arm_joints = None
        self.variables = {}
        self.published = []
        self.logs = []
        self._stop_after = stop_after
        self._stop_at_claims = stop_at_claims
        self._stop_checks = 0

    def should_stop(self):
        self._stop_checks += 1
        # stop_at_claims fires only once a grasp has actually CLAIMED a tag, so a
        # test can prove the loop stops MID-loop (between grasps) rather than in
        # the first observe-move. Claiming happens at the very end of a grasp, so
        # it stays False throughout the first grasp's motion.
        if (self._stop_at_claims is not None
                and len(self.claimed_tags) >= self._stop_at_claims):
            return True
        return self._stop_after is not None and self._stop_checks > self._stop_after

    def get_scene_frame(self):
        return np.zeros((480, 640, 3), dtype=np.uint8)

    def get_scene_frame_age(self):
        return 0.0

    def emit_detections(self, _d):
        pass

    def publisher(self, chunk):
        self.published.append(list(chunk))

    def log(self, m):
        self.logs.append(m)


def _while_block():
    return {
        'type': 'edubotics_while_visible',
        'fields': {'OBJECT_TYPE': 'banane'},
        'inputs': {'DO': {'block': {
            'type': 'edubotics_grasp_object',
            'fields': {'OBJECT_TYPE': 'banane'},
        }}},
    }


def _run(ctx):
    Interpreter([])._exec_while_visible(_while_block(), ctx, lambda *a: None)


# ── tests ────────────────────────────────────────────────────────────────────
def test_loop_grabs_every_instance_then_terminates():
    # 3 bananas reachable; the loop must grab all 3 (claiming each) and exit.
    dets = [_det(20, (0.18, 0.0)), _det(21, (0.20, 0.04)), _det(22, (0.16, -0.05))]
    ctx = _Ctx(_StubPerception(dets))
    _run(ctx)
    assert ctx.claimed_tags == {20, 21, 22}   # each grabbed exactly once, no re-grab
    assert ctx.published                       # motion happened


def _det_no_corners(tag_id, P, h=0.040):
    """A detection whose centre projects fine (position known) but with NO
    corners → orientation unreadable → grasp_object skips it."""
    cpx = _project([np.array([P[0], P[1], h])], _T())[0]
    return Detection(centroid_px=(int(cpx[0]), int(cpx[1])), bbox_px=(0, 0, 1, 1),
                     confidence=1.0, label=f'tag{tag_id}', aruco_id=tag_id, corners_px=None)


def test_loop_skips_out_of_reach_and_grabs_the_rest():
    # 2 reachable bananas + 1 far outside the reach annulus. The loop must grab
    # the 2 reachable, SKIP the unreachable one (not abort), and terminate.
    dets = [_det(20, (0.18, 0.0)), _det(21, (0.20, 0.04)), _det(22, (0.60, 0.0))]
    ctx = _Ctx(_StubPerception(dets))
    _run(ctx)                                  # must NOT raise
    assert ctx.claimed_tags == {20, 21}        # both reachable grabbed
    assert 22 in ctx.skipped_tags              # unreachable skipped, loop continued


def test_loop_skips_unreadable_orientation_and_grabs_the_rest():
    # One banana with a readable tag + one whose orientation can't be recovered.
    # The loop grabs the good one and skips the unreadable one, then terminates.
    dets = [_det(20, (0.18, 0.0)), _det_no_corners(21, (0.20, 0.04))]
    ctx = _Ctx(_StubPerception(dets))
    _run(ctx)                                  # must NOT raise
    assert ctx.claimed_tags == {20}
    assert 21 in ctx.skipped_tags


def test_loop_hard_calib_error_aborts_not_swallowed():
    # A HARD error (missing calibration) is NOT a GraspSkip — it must propagate
    # and END the loop, never be swallowed into an infinite retry.
    ctx = _Ctx(_StubPerception([_det(20, (0.18, 0.0))]))
    ctx.scene_intrinsics = None
    ctx.scene_extrinsics = None
    ctx.board_table_z = None
    ctx.z_table = None
    with pytest.raises(WorkflowError):
        _run(ctx)
    assert ctx.claimed_tags == set()           # nothing grasped; loop aborted


def test_loop_empty_from_start_terminates_without_grabbing():
    ctx = _Ctx(_StubPerception([]))
    _run(ctx)
    assert ctx.claimed_tags == set()
    # nothing grabbed; loop only retreated + counted (no grasp motion sequence)


def test_loop_debounce_survives_one_empty_frame():
    # First detection empty (occluded frame), then a banana appears. With
    # WHILE_EMPTY_FRAMES=2 the single empty frame must NOT end the loop.
    def src(call):
        return [] if call == 1 else [_det(20, (0.18, 0.0))]
    ctx = _Ctx(_StubPerception(src))
    _run(ctx)
    assert ctx.claimed_tags == {20}            # the banana was still grabbed


def test_loop_should_stop_raises_mid_loop():
    # Stop AFTER the first grab completes (1 tag claimed) but before all 3 —
    # proves the loop's between-grasp should_stop check fires, not just "stoppable
    # somewhere in the first observe-move".
    dets = [_det(20, (0.18, 0.0)), _det(21, (0.20, 0.04)), _det(22, (0.16, -0.05))]
    ctx = _Ctx(_StubPerception(dets), stop_at_claims=1)
    with pytest.raises(WorkflowError, match='gestoppt'):
        _run(ctx)
    assert 0 < len(ctx.claimed_tags) < 3   # stopped mid-loop: some but not all grabbed


def test_concurrent_grasp_does_not_double_grab():
    # The §24.3 TOCTOU claim: a when_object_seen hat thread and the main loop both
    # call grasp_object on the SAME single object — motion_lock must serialize
    # detect→claim so the tag is grabbed/claimed EXACTLY once; the loser fails loud.
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    ctx = _Ctx(_StubPerception([_det(20, (0.18, 0.0))]))   # ONE banana
    results = []

    def worker():
        try:
            pb.grasp_object(ctx, {'object_type': 'banane'})
            results.append('ok')
        except WorkflowError as e:
            results.append(('err', str(e)))

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert ctx.claimed_tags == {20}                       # grabbed exactly once
    oks = [r for r in results if r == 'ok']
    errs = [r for r in results if r != 'ok']
    assert len(oks) == 1 and len(errs) == 1               # one success, one fail-loud
    assert 'sichtbar' in errs[0][1]                       # German "nicht sichtbar"


def test_loop_no_object_selected_raises_german():
    ctx = _Ctx(_StubPerception([]))
    block = {'type': 'edubotics_while_visible', 'fields': {}, 'inputs': {}}
    with pytest.raises(interp_mod.InterpreterError, match='kein Objekt'):
        Interpreter([])._exec_while_visible(block, ctx, lambda *a: None)


# ── #1: recycled-object reclaim ──────────────────────────────────────────────
def test_reclaim_unclaims_recycled_object(monkeypatch):
    # A claimed object that goes ABSENT and then reappears (removed + put back) is
    # un-claimed so it is grabbed again. Driven directly through the unclaimed-view
    # detection (which runs the reclaim) across present→absent→present frames.
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    monkeypatch.setattr(pb, '_RECLAIM_ABSENT_S', 0.0)
    det = _det(20, (0.18, 0.0))
    frames = {'mode': 'present'}
    ctx = _Ctx(_StubPerception(lambda _c: ([det] if frames['mode'] == 'present' else [])))
    ctx.claimed_tags.add(20)
    # Frame 1: visible + claimed, never absent → still excluded, not reclaimed.
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0
    assert 20 in ctx.claimed_tags
    assert 20 not in ctx.absent_since
    # Frame 2: absent → the absence clock starts.
    frames['mode'] = 'absent'
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0
    assert 20 in ctx.absent_since
    # Frame 3: visible again after >= RECLAIM_ABSENT_S absent → reclaimed.
    frames['mode'] = 'present'
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1
    assert 20 not in ctx.claimed_tags
    assert any('zurückgelegt' in m for m in ctx.logs)


def test_reclaim_not_triggered_by_brief_occlusion(monkeypatch):
    # A claimed tag occluded for a frame but NOT past RECLAIM_ABSENT_S stays
    # claimed (no premature re-grab).
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    monkeypatch.setattr(pb, '_RECLAIM_ABSENT_S', 1000.0)   # effectively never
    det = _det(20, (0.18, 0.0))
    frames = {'mode': 'present'}
    ctx = _Ctx(_StubPerception(lambda _c: ([det] if frames['mode'] == 'present' else [])))
    ctx.claimed_tags.add(20)
    frames['mode'] = 'absent'
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0
    frames['mode'] = 'present'
    # Reappeared, but absence never reached the threshold → still claimed.
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0
    assert 20 in ctx.claimed_tags


# ── #3: flicker-spin guard + wall-clock cap ──────────────────────────────────
def test_loop_stall_guard_breaks_on_no_progress(monkeypatch):
    # count gate stays >0 (a banana is always visible) but the body never makes
    # progress (no DO block → nothing claimed/skipped). The loop must break after
    # WHILE_STALL_PASSES instead of spinning to MAX_LOOP_ITERATIONS.
    monkeypatch.setattr(interp_mod, 'WHILE_STALL_PASSES', 3)
    ctx = _Ctx(_StubPerception([_det(20, (0.18, 0.0))]))
    block = {'type': 'edubotics_while_visible',
             'fields': {'OBJECT_TYPE': 'banane'}, 'inputs': {}}   # no DO body
    Interpreter([])._exec_while_visible(block, ctx, lambda *a: None)  # must not hang
    assert ctx.claimed_tags == set()
    assert any('kein Fortschritt' in m for m in ctx.logs)


def test_loop_wall_clock_cap_breaks(monkeypatch):
    monkeypatch.setattr(interp_mod, 'WHILE_MAX_SECONDS', -1.0)   # already over budget
    ctx = _Ctx(_StubPerception([_det(20, (0.18, 0.0))]))
    _run(ctx)
    assert ctx.claimed_tags == set()
    assert any('Zeitlimit' in m for m in ctx.logs)

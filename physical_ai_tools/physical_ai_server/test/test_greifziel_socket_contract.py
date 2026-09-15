"""G13 — the ZIEL socket contract, and what the „Solange sichtbar" loop admits.

Two defects, one root cause: an error's CLASS was chosen by its CALLER rather
than by its CAUSE, so the loop swallowed things no loop pass could ever fix and
then reported the run finished.

PART A — ``GraspSkip`` is for a cause THE WORLD can fix (nothing visible, out of
reach, orientation unreadable, no approach room, not held after the retries). A
value the student wired WRONGLY into a ``check:'Greifziel'`` socket is a base
``WorkflowError``: ``variables_get`` carries no Blockly output type, so ANY
variable connects to those sockets in one drag, and no further pass and no hat
re-fire can make 42.0 into a Detection.

Measured 2026-09-07, 3 cubes, body = the split blocks with a wrong value in
„schließe um" only: the arm descended onto the cube THREE TIMES with an open
gripper, 21 motion chunks were published, and the run reported phase
`finished`, green. The three ZIEL consumers had ALSO diverged on the identical
wrong value — „fahre über" and „senke auf" aborted, „schließe um" was swallowed.

PART B — the loop's clean-completion line („nichts mehr sichtbar — fertig.") is
a claim about the WORLD, and it is FALSE whenever an instance ended SKIPPED: a
skipped tag is excluded from the unclaimed-visible gate, so a cube the arm could
not reach lies in plain sight while the loop reports it gone.

PART C — the split grasp path does not CLAIM, so a loop body built from it
re-grasps the nearest object every pass and ends on „kein Fortschritt", which
names the symptom and no remedy. Measured 2026-09-07, 3 cubes: 3 gripper closes,
ALL on tag 22, ``claimed_tags`` empty.

Drives the REAL interpreter, the REAL closed-form IK solvers and the REAL
catalogs. Pure Python + numpy — no container.
"""

from __future__ import annotations

import math
import threading
import types

import numpy as np
import pytest

from physical_ai_server.workflow import interpreter as interp_mod
from physical_ai_server.workflow import trajectory_builder
from physical_ai_server.workflow.handlers import motion
from physical_ai_server.workflow.handlers import perception_blocks as pb
from physical_ai_server.workflow.handlers.motion import GraspSkip, WorkflowError
from physical_ai_server.workflow.ik_solver import IKSolver
from physical_ai_server.workflow.interpreter import Interpreter
from physical_ai_server.workflow.object_catalog import parse_catalog
from physical_ai_server.workflow.perception import Detection
from physical_ai_server.workflow.tag_pose import tag_corner_object_points


PROFILE_IDS = ('omx_full', 'edu6_studio', 'edu1_studio')


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """Skip the inter-chunk publish pacing and the loop's wall-clock waits; the
    published WAYPOINTS and every branch taken are byte-identical to production."""
    state = {'t': 0.0}

    def _mono():
        state['t'] += 1000.0
        return state['t']

    monkeypatch.setattr(trajectory_builder, 'time',
                        types.SimpleNamespace(monotonic=_mono, sleep=lambda _s: None))
    monkeypatch.setattr(interp_mod, 'WHILE_EMPTY_SECONDS', 0.0)
    monkeypatch.setattr(interp_mod, 'WHILE_EMPTY_FRAMES', 2)
    monkeypatch.setattr(interp_mod, 'WHILE_SETTLE_S', 0.0)
    yield


# ══════════════════════════════════════════════════════════════════════════
# Scene synthesis — an overhead camera, so real detections carry real corners
# ══════════════════════════════════════════════════════════════════════════

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
                     confidence=1.0, label=f'tag{tag_id}', aruco_id=tag_id,
                     corners_px=px)


def _det_no_corners(tag_id, P, h=0.040):
    """Position readable, orientation NOT — the canonical per-instance SKIP."""
    cpx = _project([np.array([P[0], P[1], h])], _T())[0]
    return Detection(centroid_px=(int(cpx[0]), int(cpx[1])), bbox_px=(0, 0, 1, 1),
                     confidence=1.0, label=f'tag{tag_id}', aruco_id=tag_id,
                     corners_px=None)


class _StubPerception:
    def __init__(self, dets):
        self._dets = list(dets)
        self.calls = 0

    def apriltag_available(self):
        return True

    def detect(self, bgr, camera, mode, color=None, coco_class=None, aruco_id=None):
        self.calls += 1
        if mode != 'apriltag':
            return []
        dets = list(self._dets)
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
    """A WorkflowContext stand-in with a real IK solver, a real catalog and a
    real camera model, so every handler under test runs its production path."""

    def __init__(self, perception, variables=None):
        self.ik = IKSolver()
        self.perception = perception
        self.object_catalog = _CATALOG
        self.object_catalog_error = None
        self.scene_intrinsics = {'K': _K, 'dist': np.zeros((5, 1))}
        self.scene_extrinsics = _T()
        self.board_table_z = 0.0
        self.z_table = 0.0
        self.table_plane = None
        self.xy_correction = None
        self.yaw_bias_rad = 0.0
        self.motion_lock = threading.RLock()
        self.claim_lock = threading.RLock()
        self.var_lock = threading.RLock()
        self.claimed_tags = set()
        self.skipped_tags = set()
        # _reclaim_recycled early-returns unless claimed_tags/skipped_tags
        # exist; the position stores below are lazily created by _claim_store,
        # and are declared here only so a reader can see the whole claim state at
        # once.
        self.claim_release_xy = {}
        self.claim_pick_xy = {}
        self.carried_tag = None
        self.all_done_notified = set()
        self.destinations = {}
        self.trajectories = {}
        self.zones = None
        self.tempo = 1.0
        self.get_follower_joints = None
        self.last_full_joints = list(motion.HOME_JOINTS_RAD) + [motion.GRIPPER_OPEN_RAD]
        self.last_arm_joints = None
        self.last_commanded_close_rad = None
        self.variables = dict(variables or {})
        self.published = []
        self.logs = []

    def should_stop(self):
        return False

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


def _var_get(name):
    return {'type': 'variables_get', 'fields': {'VAR': {'name': name}}}


def _chain(*blocks):
    """Snap a list of block dicts into one statement chain."""
    head = blocks[0]
    cur = head
    for nxt in blocks[1:]:
        cur['next'] = {'block': nxt}
        cur = nxt
    return head


def _while_block(body, object_type='banane'):
    return {
        'type': 'edubotics_while_visible',
        'fields': {'OBJECT_TYPE': object_type},
        'inputs': {'DO': {'block': body}},
    }


# The measured student program: „finde" latched into a variable, the split
# motion blocks driven from it, and ONE wrong value plugged into „schließe um".
def _split_body_with_wrong_close(wrong_var='Zahl'):
    return _chain(
        {'type': 'variables_set',
         'fields': {'VAR': {'name': 'Ziel'}},
         'inputs': {'VALUE': {'block': {
             'type': 'edubotics_find_object',
             'fields': {'OBJECT_TYPE': 'banane'}}}}},
        {'type': 'edubotics_move_above',
         'inputs': {'ZIEL': {'block': _var_get('Ziel')}}},
        {'type': 'edubotics_descend_to',
         'inputs': {'ZIEL': {'block': _var_get('Ziel')}}},
        {'type': 'edubotics_close_on_object',
         'inputs': {'ZIEL': {'block': _var_get(wrong_var)}}},
    )


# ══════════════════════════════════════════════════════════════════════════
# PART A — a wrongly wired value ends the run; it is not a loop-swallowable skip
# ══════════════════════════════════════════════════════════════════════════

def test_a_type_error_in_a_loop_does_not_report_a_green_run():
    """The whole defect, end to end, through the REAL loop.

    Measured with ``close_on_object`` raising ``GraspSkip``: the loop swallowed
    it on every pass, ran 3 passes, published 21 motion chunks — the arm coming
    down on the cube each time with an OPEN gripper — and returned normally, so
    the run reported phase `finished`, green.

    The pass COUNT is asserted, not only the exception class: a fix that merely
    re-raised later would still let the arm repeat the wrong motion."""
    dets = [_det(20, (0.18, 0.0)), _det(21, (0.20, 0.04)), _det(22, (0.16, -0.05))]
    ctx = _Ctx(_StubPerception(dets), variables={'Zahl': 42.0})
    block = _while_block(_split_body_with_wrong_close())

    with pytest.raises(WorkflowError) as exc:
        Interpreter([])._exec_while_visible(block, ctx, lambda *a: None)

    assert not isinstance(exc.value, GraspSkip), (
        'a wrongly wired value must not be swallowed by the loop')
    assert 'kein Greifziel' in str(exc.value)
    # The loop announces each pass („noch N sichtbar — das nächste wird
    # gegriffen."). Exactly one pass may have started; a second means the loop
    # swallowed the error. The needle is the STABLE half of that sentence: it
    # used to be „greife eines", and when that wording was corrected this filter
    # would silently have become [] and measured nothing.
    passes = [m for m in ctx.logs if 'noch' in m and 'sichtbar' in m]
    assert len(passes) <= 1, f'the loop ran {len(passes)} passes: {ctx.logs}'
    # Nothing was claimed or skipped: this is not a per-instance failure.
    assert ctx.claimed_tags == set()
    assert ctx.skipped_tags == set()


_WRONG_VALUES = [
    pytest.param(42.0, id='number'),
    pytest.param('Kiste', id='text'),
    pytest.param({'x': 0.2, 'y': 0.0, 'z': 0.03}, id='position-dict'),
    pytest.param([1, 2, 3], id='list'),
    pytest.param(True, id='boolean'),
]

_ZIEL_BLOCKS = [
    pytest.param('fahre über', motion.move_above, id='move_above'),
    pytest.param('senke auf', motion.descend_to, id='descend_to'),
    pytest.param('schließe um', motion.close_on_object, id='close_on_object'),
]


@pytest.mark.parametrize('block_de,handler', _ZIEL_BLOCKS)
@pytest.mark.parametrize('wrong', _WRONG_VALUES)
def test_every_greifziel_socket_answers_a_wrong_value_the_same_way(
        block_de, handler, wrong):
    """One value, one answer, whichever socket it landed in.

    Measured 2026-09-07 BEFORE the fix, same wrong value in each socket:
    „fahre über" ABORTED, „senke auf" ABORTED, „schließe um" was SWALLOWED by
    the loop. The block that is HOLDING the value must be named — the student
    has to find it on the canvas — and the class must be the same everywhere."""
    ctx = _Ctx(_StubPerception([]))
    with pytest.raises(WorkflowError) as exc:
        handler(ctx, {'ziel': wrong})
    assert not isinstance(exc.value, GraspSkip), f'{block_de}: must not be a skip'
    msg = str(exc.value)
    assert msg == motion._NOT_A_GREIFZIEL_DE[block_de], msg
    assert block_de in msg, f'{block_de} is not named in: {msg}'
    assert ctx.published == [], 'the arm must not move on an unusable value'


def test_the_destination_name_case_also_refuses():
    """A pinned destination NAME is the one wrong value ``_resolve_target``
    would have accepted, so it needs its own row: it is a str that IS in
    ``ctx.destinations``."""
    ctx = _Ctx(_StubPerception([]))
    ctx.destinations['Kiste'] = {'x': 0.20, 'y': 0.0, 'z': 0.03}
    for block_de, handler in (('fahre über', motion.move_above),
                              ('senke auf', motion.descend_to),
                              ('schließe um', motion.close_on_object)):
        with pytest.raises(WorkflowError) as exc:
            handler(ctx, {'ziel': 'Kiste'})
        assert not isinstance(exc.value, GraspSkip), block_de
        assert str(exc.value) == motion._NOT_A_GREIFZIEL_DE[block_de]
    assert ctx.published == []


def test_a_destination_is_not_blamed_on_the_apriltag():
    """PLACEMENT of the gate is load-bearing: BEFORE ``_resolve_target``.

    ``_resolve_target`` resolves a pinned destination name happily, so the value
    reached the tag_yaw check and the student was told „Die Ausrichtung des
    Objekts konnte nicht bestimmt werden — bitte den Tag flach und gut sichtbar
    aufkleben." about a destination that has no AprilTag at all — sending them
    to re-stick a marker that does not exist."""
    ctx = _Ctx(_StubPerception([]))
    ctx.destinations['Ablage'] = {'x': 0.20, 'y': 0.0, 'z': 0.03}
    with pytest.raises(WorkflowError) as exc:
        motion.move_above(ctx, {'ziel': 'Ablage'})
    msg = str(exc.value)
    assert 'Tag' not in msg, msg
    assert 'Ausrichtung' not in msg, msg
    assert 'kein Greifziel' in msg


def test_an_empty_socket_keeps_its_own_message():
    """The gate sits AFTER the ``None`` check, so „nichts eingesteckt" and
    „das Falsche eingesteckt" stay two different sentences — they have two
    different remedies."""
    ctx = _Ctx(_StubPerception([]))
    with pytest.raises(WorkflowError) as exc:
        motion.move_above(ctx, {'ziel': None})
    assert str(exc.value) == motion._NO_GREIFZIEL_MSG


def test_a_greifziel_awaiting_calibration_still_gets_the_calibration_error():
    """A Detection whose ``world_xyz_m`` is still ``None`` IS a Greifziel — the
    rig simply has not been calibrated yet. It must pass the type gate and reach
    ``_resolve_target``'s precise German calibration message; a gate that judged
    "has a usable position" instead of "is of this kind" would swallow it and
    send the student to re-wire a perfectly correct program."""
    ctx = _Ctx(_StubPerception([]))
    ctx.scene_intrinsics = None
    ctx.scene_extrinsics = None
    ctx.board_table_z = None
    ctx.z_table = None
    ziel = Detection(centroid_px=(10, 10), bbox_px=(0, 0, 20, 20), confidence=1.0,
                     label='banane', aruco_id=20, world_xyz_m=None, corners_px=None,
                     extras={'tag_yaw': 0.0})
    with pytest.raises(WorkflowError) as exc:
        motion.move_above(ctx, {'ziel': ziel})
    msg = str(exc.value)
    assert msg != motion._NOT_A_GREIFZIEL_DE['fahre über'], (
        'the calibration case was misclassified as a wiring error')
    assert 'kalib' in msg.lower() or 'Kamera' in msg or 'Tisch' in msg, msg


@pytest.mark.parametrize('profile_id', PROFILE_IDS)
def test_a_real_greifziel_still_works_on_every_profile(profile_id):
    """The gate must not refuse the shape ``find_object`` actually returns —
    on any of the three shipped arms."""
    from physical_ai_server import robot_profiles as rp
    p = rp.resolve(profile_id)
    ctx = _Ctx(_StubPerception([]))
    ctx.ik = p.build_ik()
    ctx.num_arm_joints = p.num_arm_joints
    ctx.roll_joint_index = p.roll_joint_index
    ctx.home_joints_rad = p.home_joints_rad
    ctx.gripper_open_rad = p.gripper_open_rad
    ctx.gripper_closed_rad = p.gripper_closed_rad
    ctx.velocity_limit_rad_s = p.velocity_limit_rad_s
    ctx.safe_travel_z_m = p.safe_travel_z_m
    ctx.tool_clear_m = p.tool_clear_m
    ctx.swing_heights_m = p.swing_heights_m
    ctx.swing_radii_m = p.swing_radii_m
    ctx.last_full_joints = list(p.home_joints_rad) + [p.gripper_open_rad]
    r = {'omx_full': 0.20, 'edu6_studio': 0.14, 'edu1_studio': 0.20}[profile_id]
    ziel = types.SimpleNamespace(
        world_xyz_m=(ctx.ik.base_axis_x + r, 0.0, 0.015),
        aruco_id=20, corners_px=None,
        extras={'tag_yaw': 0.0, 'gripper_close_rad': p.gripper_closed_rad,
                'approach_clear_m': 0.06})
    motion.move_above(ctx, {'ziel': ziel})          # must NOT raise
    assert ctx.published, 'a real Greifziel must still drive the arm'


# ══════════════════════════════════════════════════════════════════════════
# PART B — the completion line must admit what it skipped
# ══════════════════════════════════════════════════════════════════════════

def _run_grasp_loop(ctx):
    block = _while_block({'type': 'edubotics_grasp_object',
                          'fields': {'OBJECT_TYPE': 'banane'}})
    Interpreter([])._exec_while_visible(block, ctx, lambda *a: None)


def test_a_clean_finish_with_nothing_skipped_is_unchanged():
    """The n == 0 wording is byte-identical to what shipped."""
    ctx = _Ctx(_StubPerception([_det(20, (0.18, 0.0)), _det(21, (0.20, 0.04))]))
    _run_grasp_loop(ctx)
    assert ctx.claimed_tags == {20, 21}
    assert ctx.skipped_tags == set()
    assert '„Banane": nichts mehr sichtbar — fertig.' in ctx.logs


def test_one_skipped_object_is_named_in_the_completion_line():
    """MEASURED before: the loop skipped a cube it could not reach and then
    logged „nichts mehr sichtbar — fertig." while the cube lay in plain view."""
    dets = [_det(20, (0.18, 0.0)), _det_no_corners(21, (0.20, 0.04))]
    ctx = _Ctx(_StubPerception(dets))
    _run_grasp_loop(ctx)
    assert ctx.skipped_tags == {21}
    done = [m for m in ctx.logs if m.startswith('„Banane": fertig')]
    assert len(done) == 1, ctx.logs
    assert 'ein Objekt konnte nicht gegriffen werden' in done[0]
    assert 'Der Grund steht' in done[0]
    # NOT „ein „Banane"" — masculine-only, and the type is already cited by the
    # „Banane:" prefix.
    assert 'ein „Banane"' not in done[0]
    assert 'liegt noch da' in done[0]
    assert 'nichts mehr sichtbar' not in done[0], (
        'the loop must not claim the table is clear')


def test_several_skipped_objects_are_counted_in_the_completion_line():
    dets = [_det(20, (0.18, 0.0)),
            _det_no_corners(21, (0.20, 0.04)),
            _det_no_corners(22, (0.16, -0.05))]
    ctx = _Ctx(_StubPerception(dets))
    _run_grasp_loop(ctx)
    assert ctx.skipped_tags == {21, 22}
    done = [m for m in ctx.logs if m.startswith('„Banane": fertig')]
    assert len(done) == 1, ctx.logs
    assert '2 Objekte konnten nicht gegriffen werden' in done[0]
    assert 'liegen noch da' in done[0]
    # n skips produce n separate [WARNUNG]s above, so the plural branch says so.
    assert 'Die Gründe stehen' in done[0]
    assert '2 „Banane"' not in done[0], 'an invariant plural: Kugel → Kugeln'


def test_the_skipped_count_is_scoped_to_this_loops_object_type():
    """A tag skipped for ANOTHER type must not be counted here — the loop is
    reporting on its own type only."""
    ctx = _Ctx(_StubPerception([_det(20, (0.18, 0.0))]))
    ctx.skipped_tags.add(99)                      # not a 'banane' tag id
    _run_grasp_loop(ctx)
    assert '„Banane": nichts mehr sichtbar — fertig.' in ctx.logs


def test_the_completion_diagnostic_never_breaks_the_loop():
    """A diagnostic may not become a new failure mode: with the claim
    bookkeeping and the catalog unusable, the loop must still finish."""
    interp = Interpreter([])
    broken = types.SimpleNamespace(object_catalog=None, object_catalog_error='x')
    assert interp._skipped_count_for_type(broken, 'banane') == 0
    assert interp._skipped_count_for_type(types.SimpleNamespace(), 'banane') == 0

    class _BoomCtx:
        object_catalog = _CATALOG
        claim_lock = None

        @property
        def skipped_tags(self):
            raise RuntimeError('claim state exploded')

    assert interp._skipped_count_for_type(_BoomCtx(), 'banane') == 0
    assert interp._skipped_count_for_type(
        types.SimpleNamespace(object_catalog=_CATALOG, claim_lock=None),
        'kein-solcher-typ') == 0


# ══════════════════════════════════════════════════════════════════════════
# PART C — the stall ending names the missing block instead of only the symptom
# ══════════════════════════════════════════════════════════════════════════

def _split_body_no_claim():
    """The measured student program: the split grasp path with no „merke … als
    erledigt". Nothing claims, so „finde" returns the same nearest instance
    every pass and the arm re-grasps ONE object."""
    return _chain(
        {'type': 'variables_set',
         'fields': {'VAR': {'name': 'Ziel'}},
         'inputs': {'VALUE': {'block': {
             'type': 'edubotics_find_object',
             'fields': {'OBJECT_TYPE': 'banane'}}}}},
        {'type': 'edubotics_move_above',
         'inputs': {'ZIEL': {'block': _var_get('Ziel')}}},
        {'type': 'edubotics_descend_to',
         'inputs': {'ZIEL': {'block': _var_get('Ziel')}}},
        {'type': 'edubotics_close_on_object',
         'inputs': {'ZIEL': {'block': _var_get('Ziel')}}},
        {'type': 'edubotics_lift'},
    )


_MISSING_MARK_DONE = 'Im Schleifenkörper fehlt'


def test_a_split_loop_without_mark_done_is_told_which_block_is_missing():
    """MEASURED 2026-09-07, 3 cubes, this exact body: 3 gripper closes ALL on
    tag 22, ``claimed_tags`` empty, ending on „kein Fortschritt" — which names
    the symptom and no remedy, so the student has no way to reach the answer."""
    dets = [_det(20, (0.18, 0.0)), _det(21, (0.20, 0.04)), _det(22, (0.16, -0.05))]
    ctx = _Ctx(_StubPerception(dets))
    Interpreter([])._exec_while_visible(
        _while_block(_split_body_no_claim()), ctx, lambda *a: None)

    assert ctx.claimed_tags == set(), 'premise: the split path does not claim'
    stall = [m for m in ctx.logs if 'kein Fortschritt' in m]
    assert len(stall) == 1, ctx.logs
    hint = [m for m in ctx.logs if _MISSING_MARK_DONE in m]
    assert len(hint) == 1, f'the remedy was not named: {ctx.logs}'
    assert 'merke' in hint[0] and 'als erledigt' in hint[0]
    assert 'Greife' in hint[0], 'the one-block alternative must also be offered'


def test_a_loop_whose_body_does_claim_gets_no_missing_block_hint():
    """The hint must be EVIDENCE-driven: with „merke … als erledigt" present the
    stall has some other cause, and blaming a block that is right there on the
    canvas would be worse than the generic line."""
    body = _chain(*_split_body_no_claim_blocks(),
                  {'type': 'edubotics_mark_done',
                   'inputs': {'ZIEL': {'block': _var_get('Ziel')}}})
    interp = Interpreter([])
    assert interp._body_can_claim(body) is True


def _split_body_no_claim_blocks():
    """The split chain as a LIST, so tests can append their own tail."""
    return [
        {'type': 'variables_set',
         'fields': {'VAR': {'name': 'Ziel'}},
         'inputs': {'VALUE': {'block': {
             'type': 'edubotics_find_object',
             'fields': {'OBJECT_TYPE': 'banane'}}}}},
        {'type': 'edubotics_move_above',
         'inputs': {'ZIEL': {'block': _var_get('Ziel')}}},
        {'type': 'edubotics_close_on_object',
         'inputs': {'ZIEL': {'block': _var_get('Ziel')}}},
    ]


def test_the_body_scan_sees_a_claim_nested_in_a_control_block():
    """A student who wraps the claim in „falls Greifer hält etwas?" has NOT
    forgotten it. The scan must recurse into every nested statement slot."""
    interp = Interpreter([])
    nested = _chain(
        {'type': 'edubotics_move_above',
         'inputs': {'ZIEL': {'block': _var_get('Ziel')}}},
        {'type': 'controls_if',
         'inputs': {'IF0': {'block': {'type': 'edubotics_grasp_held'}},
                    'DO0': {'block': {'type': 'edubotics_mark_done',
                                      'inputs': {'ZIEL': {'block': _var_get('Ziel')}}}}}},
    )
    assert interp._body_can_claim(nested) is True
    deeper = {'type': 'controls_repeat_ext',
              'inputs': {'DO': {'block': {'type': 'controls_if', 'inputs': {
                  'DO0': {'block': {'type': 'edubotics_grasp_object',
                                    'fields': {'OBJECT_TYPE': 'banane'}}}}}}}}
    assert interp._body_can_claim(deeper) is True


def test_the_body_scan_follows_a_procedure_call():
    """„Greifen" packed into a Funktion still claims."""
    proc = {'type': 'procedures_defnoreturn',
            'fields': {'NAME': 'Greifen'},
            'inputs': {'STACK': {'block': {
                'type': 'edubotics_mark_done',
                'inputs': {'ZIEL': {'block': _var_get('Ziel')}}}}}}
    interp = Interpreter([proc])
    body = {'type': 'procedures_callnoreturn', 'extraState': {'name': 'Greifen'}}
    assert interp._body_can_claim(body) is True


def test_the_body_scan_fails_open_on_anything_it_cannot_decide():
    """It only chooses between two WORDINGS after a measured stall, so an
    undecidable body must answer "can claim" and withhold the hint rather than
    accuse the student of a missing block. Unresolvable calls, self-recursion
    and a depth blow-out are all undecided."""
    interp = Interpreter([])
    assert interp._body_can_claim(
        {'type': 'procedures_callnoreturn', 'extraState': {'name': 'Unbekannt'}}) is True

    rec = {'type': 'procedures_defnoreturn', 'fields': {'NAME': 'Schleife'},
           'inputs': {'STACK': {'block': {
               'type': 'procedures_callnoreturn',
               'extraState': {'name': 'Schleife'}}}}}
    ri = Interpreter([rec])
    assert ri._body_can_claim(
        {'type': 'procedures_callnoreturn',
         'extraState': {'name': 'Schleife'}}) is True    # cycle ⇒ undecided

    deep = {'type': 'edubotics_lift'}
    for _ in range(interp._CLAIM_SCAN_MAX_DEPTH + 5):
        deep = {'type': 'controls_if', 'inputs': {'DO0': {'block': deep}}}
    assert interp._body_can_claim(deep) is True         # depth cap ⇒ undecided


def test_an_empty_loop_body_is_not_blamed_on_a_missing_mark_done():
    """With NO body at all, „der Block fehlt" is the wrong half of the truth and
    there is nothing grasped to mark done."""
    dets = [_det(20, (0.18, 0.0))]
    ctx = _Ctx(_StubPerception(dets))
    block = {'type': 'edubotics_while_visible', 'fields': {'OBJECT_TYPE': 'banane'}}
    Interpreter([])._exec_while_visible(block, ctx, lambda *a: None)
    assert any('kein Fortschritt' in m for m in ctx.logs), ctx.logs
    assert not any(_MISSING_MARK_DONE in m for m in ctx.logs), ctx.logs


def test_the_composite_grasp_path_is_unaffected():
    """„Greife …" claims for itself, so it neither stalls nor gets the hint."""
    dets = [_det(20, (0.18, 0.0)), _det(21, (0.20, 0.04))]
    ctx = _Ctx(_StubPerception(dets))
    _run_grasp_loop(ctx)
    assert ctx.claimed_tags == {20, 21}
    assert not any('kein Fortschritt' in m for m in ctx.logs), ctx.logs
    assert not any(_MISSING_MARK_DONE in m for m in ctx.logs), ctx.logs


def test_mark_done_still_does_the_work_and_is_not_bypassed():
    """The decision NOT to auto-claim, pinned: the split path claims ONLY
    through „merke … als erledigt". If a later change adds a hidden auto-claim
    in ``close_on_object`` or ``lift``, this fails — and that change would make
    ``mark_done`` a block with no observable effect."""
    ctx = _Ctx(_StubPerception([_det(20, (0.18, 0.0))]))
    ziel = pb.find_object(ctx, {'object_type': 'banane'})
    assert ziel is not None
    motion.move_above(ctx, {'ziel': ziel})
    motion.descend_to(ctx, {'ziel': ziel})
    motion.close_on_object(ctx, {'ziel': ziel})
    motion.lift(ctx, {})
    assert ctx.claimed_tags == set(), (
        'no split block may claim behind the student\'s back')
    pb.mark_done(ctx, {'ziel': ziel})
    assert ctx.claimed_tags == {20}

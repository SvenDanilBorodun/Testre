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
        # Recycled-object reclaim state: where the robot COMMANDED the release
        # of a claimed tag, where an unclaimed one lies (the SKIP reference),
        # which tag is in the gripper, and which types have already been told
        # „alles erledigt" this run.
        self.claim_release_xy = {}
        self.claim_pick_xy = {}
        self.carried_tag = None
        self.all_done_notified = set()
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


def _while_block_max(max_reps):
    b = _while_block()
    b['fields']['MAX_REPS'] = max_reps
    return b


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


# ── #1: recycled-object reclaim — the ROBOT'S OWN COMMAND, never a sighting ──
# ONE rule decides a CLAIMED object, and its reference is where „ablegen bei"
# DROVE before it opened the jaws (ctx.claim_release_xy, ≥ _RELEASE_MOVE_M);
# SKIPPED objects, which the robot never carried, are judged against where they
# lay when we gave up (ctx.claim_pick_xy, ≥ _RECLAIM_MOVE_M). Because the
# reference is a COMMAND, no observation exists for a missed AprilTag look to
# corrupt — which the first two tests below pin directly — and the rule works
# just as well when the destination lies OUTSIDE the scene camera's view, which
# is the case the sighting-derived anchor it replaced could not arm in at all.
_PICK = (0.18, 0.00)          # where an object lies before it is grasped
_DROP = (0.18, 0.10)          # 100 mm away — where the program places it
_NUDGED = (0.185, 0.00)       # 5 mm from _PICK — inside both thresholds
_OUT_OF_REACH = (0.60, 0.00)


def _released_at(ctx, tag, xy):
    """Record what ``motion.drop_at`` records after its release publish: the
    COMMANDED (x, y) the robot drove to before opening the jaws."""
    from physical_ai_server.workflow.handlers import motion as _m
    ctx.carried_tag = tag
    _m.note_object_released(ctx, xy)


def _scripted_ctx(script):
    """A ctx whose scene is driven by a per-observation script: one detection
    list per ``count_unclaimed_visible`` call, with the last entry repeating."""
    return _Ctx(_StubPerception(
        lambda call: list(script[min(call, len(script)) - 1])))


def _reclaim_lines(ctx):
    """The German lines the reclaim itself emits — EVERY variant.

    Derived from ``perception_blocks._RECLAIM_TEMPLATES_DE`` rather than listing
    the sentences, because listing them is how this helper silently goes blind:
    it named two of the variants, a third was added for the PICK rule, and
    ``assert _reclaim_lines(ctx) == []`` then kept passing VACUOUSLY on the exact
    rule that changed. A third rule cannot orphan it now.
    """
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    heads = [t.split('#{tag}')[1].split('—')[0].strip()
             for t in pb._RECLAIM_TEMPLATES_DE.values()]
    assert len(heads) == 2 and all(heads), (
        f'the reclaim template table changed shape: {pb._RECLAIM_TEMPLATES_DE}')
    return [m for m in ctx.logs if any(h in m for h in heads)]


def test_a_tag_missed_on_one_observation_is_never_reclaimed():
    # THE ORIGINAL DEFECT, and the thing this design makes impossible BY
    # CONSTRUCTION rather than by precondition: one genuine AprilTag miss (a hand
    # passing over the table) on a cube nobody touched used to re-grasp it and
    # print „wurde zurückgelegt". The reference is now the COMMANDED release
    # point, so a miss has no observation to corrupt.
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    placed = _det(20, _DROP)
    ctx = _scripted_ctx([[placed], [], [placed], [placed]])
    pb._claim_tag(ctx, 20)
    _released_at(ctx, 20, _DROP)                           # „ablegen bei" drove here
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0   # sitting where it was put
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0   # ONE missed look
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0   # back, same spot
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0
    assert 20 in ctx.claimed_tags
    assert _reclaim_lines(ctx) == []


def test_a_tag_missed_on_five_consecutive_observations_is_never_reclaimed():
    # No COUNTER value can work, which is why there is no counter and no absence
    # state at all: five misses of an untouched cube are still five misses.
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    placed = _det(20, _DROP)
    ctx = _scripted_ctx([[placed], [], [], [], [], [], [placed]])
    pb._claim_tag(ctx, 20)
    _released_at(ctx, 20, _DROP)
    for _ in range(7):
        assert pb.count_unclaimed_visible(ctx, 'banane') == 0
    assert 20 in ctx.claimed_tags
    assert _reclaim_lines(ctx) == []


def test_an_object_moved_without_ever_being_absent_is_reclaimed():
    # The decisive case for choosing POSITION over absence: a student who SLIDES
    # the cube away never makes it disappear for a single observation, so every
    # absence rule scores zero here.
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    ctx = _scripted_ctx([[_det(20, _PICK)], [_det(20, _PICK)], [_det(20, _DROP)]])
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1   # still up for grabs
    pb._claim_tag(ctx, 20)
    _released_at(ctx, 20, _PICK)                            # placed back at _PICK
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0   # lying where it was put
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1   # 100 mm away → moved
    assert 20 not in ctx.claimed_tags


def test_the_reference_is_the_command_and_never_a_sighting():
    # THE OLD TRAP, kept as a fence. Anchoring on an OBSERVATION — the last one
    # before an absence, or the first one after the claim — re-creates the
    # original defect: the pre-absence sighting is the PICK spot, the post-absence
    # one the DROP spot, and the robot's own placement then reads as „moved
    # 100 mm". Nothing here is derived from a sighting, so the shape cannot exist.
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    ctx = _scripted_ctx([[_det(20, _PICK)], [], [_det(20, _DROP)], [_det(20, _DROP)]])
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1   # seen at the pick spot
    pb._claim_tag(ctx, 20)                                  # …grasped, placed at DROP
    _released_at(ctx, 20, _DROP)
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0   # first look MISSED
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0   # now visible at DROP
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0
    assert 20 in ctx.claimed_tags
    assert _reclaim_lines(ctx) == []


def test_a_claimed_object_with_no_recorded_release_point_is_never_reclaimed():
    # FAILS CLOSED. „merke <Ziel> als erledigt" claims a tag the robot may never
    # have carried, and a bare „öffne Greifer" releases one at a place the robot
    # never aimed for. Neither leaves a reference, so neither can be judged — and
    # guessing one (the object's current position, say) would un-claim it the
    # moment anything, including the arm's own parallax, moved the projection.
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    ctx = _scripted_ctx([[_det(20, _PICK)], [_det(20, _DROP)],
                         [_det(20, _OUT_OF_REACH)]])
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1
    pb._claim_tag(ctx, 20)                                  # claimed, never released
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0
    assert 20 in ctx.claimed_tags
    assert _reclaim_lines(ctx) == []


def test_an_unknown_release_position_also_fails_closed():
    # „öffne Greifer" has no destination, so motion.note_object_released stores
    # None — explicitly, so a reader can see the tag WAS released. _gap answers
    # None for it and no comparison succeeds.
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    ctx = _scripted_ctx([[_det(20, _PICK)], [_det(20, _DROP)]])
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1
    pb._claim_tag(ctx, 20)
    _released_at(ctx, 20, None)
    assert ctx.claim_release_xy == {20: None}, ctx.claim_release_xy
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0
    assert 20 in ctx.claimed_tags
    assert _reclaim_lines(ctx) == []


def test_the_two_reclaim_sentences_are_two_distinct_sentences():
    """A source fence. The defect was ONE sentence serving TWO opposite
    observations, so what has to be pinned is that they are DIFFERENT — and that
    the module holds exactly one copy of each, so a future edit cannot quietly
    share one again."""
    import inspect
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    tpl = pb._RECLAIM_TEMPLATES_DE
    assert set(tpl) == {'release', 'skip'}, tpl
    assert len(set(tpl.values())) == 2, f'two rules share a sentence: {tpl}'
    import ast as _ast
    src = inspect.getsource(pb)
    # STRING LITERALS only — the comment explaining the defect naturally quotes
    # the sentence, and a raw text count would forbid documenting it.
    literals = [n.value for n in _ast.walk(_ast.parse(src))
                if isinstance(n, _ast.Constant) and isinstance(n.value, str)]
    for sentence in ('liegt jetzt woanders', 'wurde bewegt'):
        hits = [lit for lit in literals if sentence in lit]
        assert len(hits) == 1, (
            f'„{sentence}" appears in {len(hits)} string literals — two rules '
            'sharing one sentence is the defect this table exists to stop')
    # The PICK rule's sentence went with the rule. Leaving it behind would invite
    # somebody to wire it back up to an observation-derived reference.
    assert not any('alten Stelle' in lit for lit in literals), (
        'the PICK rule is gone — its sentence must not survive it')
    # _reclaim takes the rule from its CALLER: it pops the release point as its
    # third statement, so anything re-derived inside it reads destroyed state.
    assert 'def _reclaim(tag: int, rule: str) -> None:' in src, (
        'the rule must be PASSED in, not re-derived inside _reclaim')


def test_a_program_that_puts_an_object_back_where_it_found_it_still_terminates():
    # Degenerate but legal: the drop point IS the pick point. Under the old PICK
    # rule this needed a prior-absence precondition to stay terminating; with the
    # commanded release point it is simply a distance of zero.
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    ctx = _scripted_ctx([[_det(20, _PICK)]])
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1
    pb._claim_tag(ctx, 20)
    _released_at(ctx, 20, _PICK)
    for _ in range(4):
        assert pb.count_unclaimed_visible(ctx, 'banane') == 0
    assert 20 in ctx.claimed_tags
    assert _reclaim_lines(ctx) == []


def test_the_release_rule_says_woanders():
    """A claimed object that moved away from where the robot PUT it."""
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    ctx = _scripted_ctx([[_det(20, _PICK)], [_det(20, _DROP)]])
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1
    pb._claim_tag(ctx, 20)
    _released_at(ctx, 20, _PICK)
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1   # 100 mm away
    lines = _reclaim_lines(ctx)
    assert len(lines) == 1, f'expected one reclaim line, got {lines}'
    assert 'liegt jetzt woanders' in lines[0], lines[0]
    # „an DER alten Stelle" / „an seinem alten Platz" — a possessive would agree
    # with the LABEL's gender and re-introduce the leak these messages avoid.
    assert 'seinem' not in lines[0] and 'ihrem' not in lines[0]


def test_a_skipped_out_of_reach_object_moved_into_reach_is_retried():
    # SKIP anchoring: the robot never moved this one, so its reference is where it
    # lay when we gave up — which lets a student slide it into reach and have it
    # retried in the SAME run. This half still reads a SIGHTING, and correctly so:
    # there is no command to read, because the robot never drove anywhere with it.
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    ctx = _scripted_ctx([[_det(20, _OUT_OF_REACH)], [_det(20, _PICK)]])
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1
    pb._skip_tag(ctx, 20)                                   # „außerhalb des Greifbereichs"
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1   # moved into reach → retry
    assert 20 not in ctx.skipped_tags
    assert any('wurde bewegt' in m for m in ctx.logs)


def test_a_claim_after_a_place_keeps_the_release_point():
    """THE ordering that makes the split grasp path work.

    „merke <Ziel> als erledigt" runs AFTER „ablegen bei", so a claim that cleared
    position state — which is exactly what ``claims._forget_position_state`` used
    to do, correctly, for the sighting-derived anchor — would erase the release
    point the place had just recorded and disable the reclaim for that tag for the
    rest of the run. Claiming must touch NO position state."""
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    ctx = _scripted_ctx([[_det(20, _PICK)], [_det(20, _DROP)]])
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1
    _released_at(ctx, 20, _PICK)                            # „ablegen bei" first…
    pb._claim_tag(ctx, 20)                                  # …then „merke als erledigt"
    assert ctx.claim_release_xy.get(20) == _PICK, ctx.claim_release_xy
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1   # still judged → reclaimed
    assert 20 not in ctx.claimed_tags


def test_a_move_below_the_threshold_does_not_reclaim():
    # 5 mm — the student brushed past it. Well above the 0.164 mm worst-case
    # projection noise, well below both thresholds and the 30 mm cube.
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    ctx = _scripted_ctx([[_det(20, _PICK)], [_det(20, _NUDGED)], [_det(20, _NUDGED)]])
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1
    pb._claim_tag(ctx, 20)
    _released_at(ctx, 20, _PICK)
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0   # nudged 5 mm
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0
    assert 20 in ctx.claimed_tags
    assert _reclaim_lines(ctx) == []


def test_reclaim_move_zero_disables_the_reclaim(monkeypatch):
    # EDUBOTICS_RECLAIM_MOVE_M=0 is the MASTER one-variable rollback — it must
    # disable BOTH halves, not turn a `distance >= 0` comparison into "reclaim
    # always".
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    monkeypatch.setattr(pb, '_RECLAIM_MOVE_M', 0.0)
    ctx = _scripted_ctx([[_det(20, _PICK)], [_det(20, _DROP)], [_det(20, _DROP)]])
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1
    pb._claim_tag(ctx, 20)
    _released_at(ctx, 20, _PICK)
    for _ in range(3):
        assert pb.count_unclaimed_visible(ctx, 'banane') == 0
    assert 20 in ctx.claimed_tags
    assert _reclaim_lines(ctx) == []


def test_find_object_and_wait_until_now_run_the_reclaim_too():
    """The inversion of the old read-only split, and the fix for the deadlock.

    „sehe ich", „Anzahl", „finde" and „warte bis" used to read through a
    reclaim-free twin, so a „Solange sichtbar" loop nested inside „falls sehe ich
    Würfel" could never recover: once every tag was claimed, „sehe ich" answered
    False forever and the only reclaiming block was unreachable. The hazard that
    twin existed for — a mid-carry „finde" un-claiming the held object — is now
    ``ctx.carried_tag``, which names the held tag exactly."""
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    ctx = _Ctx(_StubPerception([_det(20, _DROP)]))
    pb._claim_tag(ctx, 20)
    _released_at(ctx, 20, _PICK)          # …as if the robot had placed it at _PICK
    assert pb.see_object(ctx, {'object_type': 'banane'}) is True
    assert 20 not in ctx.claimed_tags
    assert len(_reclaim_lines(ctx)) == 1, ctx.logs


def test_a_carried_object_is_never_reclaimed_however_far_it_travels():
    """``ctx.carried_tag`` is what lets every looking block run the reclaim.

    The taught split-grasp pattern calls „finde" MID-CARRY, and a held object's
    projected position travels with the gripper — so without this guard the very
    first look after the pick-up would judge the object against its previous
    release point and un-claim the cube the robot is HOLDING."""
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    ctx = _Ctx(_StubPerception([_det(20, _OUT_OF_REACH)]))
    pb._claim_tag(ctx, 20)
    _released_at(ctx, 20, _PICK)
    ctx.carried_tag = 20                                    # …picked up again
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0
    assert pb.find_object(ctx, {'object_type': 'banane'}) is None
    assert pb.see_object(ctx, {'object_type': 'banane'}) is False
    assert ctx.claimed_tags == {20}
    assert _reclaim_lines(ctx) == []


def test_the_reclaim_message_names_the_tag_the_way_the_printed_sheet_does():
    # tools/generate_apriltags.py prints „#20 Würfel" on the sheet the student has
    # in front of them, so the Protokoll says #20 — not „(20)". And the sentence
    # reports what was OBSERVED (it lies elsewhere), never an intention nobody
    # watched: „wurde zurückgelegt" asserted a put-back that had not been seen.
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    moved = _scripted_ctx([[_det(20, _PICK)], [_det(20, _DROP)]])
    pb.count_unclaimed_visible(moved, 'banane')
    pb._claim_tag(moved, 20)
    _released_at(moved, 20, _PICK)
    pb.count_unclaimed_visible(moved, 'banane')
    assert _reclaim_lines(moved) == [
        '„Banane" #20 liegt jetzt woanders — wird noch einmal gegriffen.']

    skipped = _scripted_ctx([[_det(21, _OUT_OF_REACH)], [_det(21, _PICK)]])
    pb.count_unclaimed_visible(skipped, 'banane')
    pb._skip_tag(skipped, 21)
    pb.count_unclaimed_visible(skipped, 'banane')
    assert _reclaim_lines(skipped) == ['„Banane" #21 wurde bewegt — neuer Versuch.']
    assert not any('zurückgelegt' in m or '(20)' in m or '(21)' in m
                   for m in moved.logs + skipped.logs)


def test_the_all_done_message_has_no_gendered_article():
    """C-2 (a)+(b). „es ist KEINES mehr übrig, DAS gegriffen werden könnte" is
    neuter — wrong for *der Würfel*, the only object in the shipped catalog — and
    „bitte EIN „Würfel" kurz WEGNEHMEN" needs the accusative „einen". The label is
    a per-type string a teacher will one day author, so nothing may inflect
    around it: the countable noun is the neuter „Objekt", which the block
    tooltips and this module's own sibling sentence already use."""
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    ctx = _scripted_ctx([[]])
    ctx.claimed_tags = {20, 21, 22}
    recipe = ctx.object_catalog.recipe_for_type('banane')
    msg = pb._nothing_to_grasp_message(ctx, recipe)
    assert 'kein Objekt mehr übrig' in msg, msg
    assert 'ein Objekt kurz wegnehmen' in msg, msg
    assert 'keines' not in msg, 'neuter — the shipped object is *der* Würfel'
    assert 'ein „Banane"' not in msg, '`wegnehmen` governs the accusative'
    # The type is still NAMED, once, in topic position.
    assert 'Alle „Banane" sind schon erledigt' in msg, msg


def test_the_loop_pass_line_has_no_pronoun():
    """C-3 / N2. „greife EINES" is the neuter accusative pronoun (*der Würfel*
    needs „einen") on the line that prints once per pass — the most-read German
    string in the whole „Solange sichtbar" lesson. Pre-existing on `main`, swept
    with its three neighbours so the file does not ship two conventions."""
    import inspect
    from physical_ai_server.workflow import interpreter as _in
    src = inspect.getsource(_in)
    assert 'das nächste wird gegriffen' in src
    assert 'greife eines' not in src, (
        'the neuter pronoun is back on the loop\'s most-printed line')


def test_one_dropped_frame_does_not_reclaim_however_long_the_pass_took():
    # The regression test for the loop-pass-cadence defect, kept as a live fence
    # with a RELEASE POINT recorded so it is not vacuous: a claimed object sitting
    # exactly where the robot put it, dropped from one observation and seen again,
    # must not un-claim. There is no longer a wall-clock debounce to satisfy —
    # _RECLAIM_ABSENT_S is the object-seen HAT's grace and nothing else.
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    det = _det(20, (0.18, 0.0))
    frames = {'mode': 'present'}
    ctx = _Ctx(_StubPerception(lambda _c: ([det] if frames['mode'] == 'present' else [])))
    ctx.claimed_tags.add(20)
    _released_at(ctx, 20, (0.18, 0.0))
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0
    frames['mode'] = 'absent'
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0      # ONE dropped frame
    frames['mode'] = 'present'
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0
    assert 20 in ctx.claimed_tags, 'one dropped frame must not re-grasp'
    assert _reclaim_lines(ctx) == []


def test_reclaim_not_triggered_by_brief_occlusion():
    # A claimed tag occluded for a frame and back on its own square stays claimed
    # (no premature re-grab), at any occlusion length — an absence records nothing
    # at all now.
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    det = _det(20, (0.18, 0.0))
    frames = {'mode': 'present'}
    ctx = _Ctx(_StubPerception(lambda _c: ([det] if frames['mode'] == 'present' else [])))
    ctx.claimed_tags.add(20)
    _released_at(ctx, 20, (0.18, 0.0))
    frames['mode'] = 'absent'
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0
    frames['mode'] = 'present'
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


# ── #6: student repetition cap (MAX_REPS field) ──────────────────────────────
def test_loop_respects_student_max_reps():
    # 3 bananas visible but the student capped the loop at 2 → only 2 grabbed,
    # with the German "Höchstzahl" notice.
    dets = [_det(20, (0.18, 0.0)), _det(21, (0.20, 0.04)), _det(22, (0.16, -0.05))]
    ctx = _Ctx(_StubPerception(dets))
    Interpreter([])._exec_while_visible(_while_block_max(2), ctx, lambda *a: None)
    assert len(ctx.claimed_tags) == 2
    assert any('Höchstzahl' in m for m in ctx.logs)


def test_loop_max_reps_zero_is_unlimited():
    # 0 = unbegrenzt: the loop grabs all three despite the field being present.
    dets = [_det(20, (0.18, 0.0)), _det(21, (0.20, 0.04)), _det(22, (0.16, -0.05))]
    ctx = _Ctx(_StubPerception(dets))
    Interpreter([])._exec_while_visible(_while_block_max(0), ctx, lambda *a: None)
    assert ctx.claimed_tags == {20, 21, 22}


def test_loop_max_reps_malformed_is_unlimited():
    # A non-numeric field value must degrade to unbegrenzt, not crash the loop.
    dets = [_det(20, (0.18, 0.0)), _det(21, (0.20, 0.04))]
    ctx = _Ctx(_StubPerception(dets))
    block = _while_block_max('nonsense')
    Interpreter([])._exec_while_visible(block, ctx, lambda *a: None)
    assert ctx.claimed_tags == {20, 21}


# ── #7: per-pass German feedback ─────────────────────────────────────────────
def test_loop_logs_per_pass_progress_and_completion():
    ctx = _Ctx(_StubPerception([_det(20, (0.18, 0.0))]))
    _run(ctx)
    # one positive pass line naming the count, plus a friendly completion line
    assert any('noch 1 sichtbar' in m for m in ctx.logs)
    assert any('fertig' in m for m in ctx.logs)

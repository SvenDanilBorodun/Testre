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
        # Recycled-object reclaim state (position-based): where the robot left a
        # claimed tag, where an unclaimed one lies, and which claimed tags have
        # been missing at least once since their claim.
        self.claim_anchor = {}
        self.claim_pick_xy = {}
        self.claim_unseen = set()
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


# ── #1: recycled-object reclaim — POSITION, never absence ────────────────────
# The reclaim decides on two independent proofs, BOTH taken from a SIGHTING:
#   ANCHOR — a claimed object seen ≥ _RECLAIM_MOVE_M from where the robot LEFT it
#            (its first observed rest position AFTER the claim) was moved;
#   PICK   — a claimed object that has been unseen at least once and then turns
#            up back at the spot it was PICKED from was put there by a person.
# Because neither rule acts on an absence, a missed AprilTag look can never
# reclaim, at ANY miss rate — which the first two tests below pin directly.
_PICK = (0.18, 0.00)          # where an object lies before it is grasped
_DROP = (0.18, 0.10)          # 100 mm away — where the program places it
_NUDGED = (0.185, 0.00)       # 5 mm from _PICK — inside the threshold
_OUT_OF_REACH = (0.60, 0.00)


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
    rule that changed. A fourth rule cannot orphan it now.
    """
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    heads = [t.split('#{tag}')[1].split('—')[0].strip()
             for t in pb._RECLAIM_TEMPLATES_DE.values()]
    assert len(heads) == 3 and all(heads), (
        f'the reclaim template table changed shape: {pb._RECLAIM_TEMPLATES_DE}')
    return [m for m in ctx.logs if any(h in m for h in heads)]


def test_a_tag_missed_on_one_observation_is_never_reclaimed():
    # THE ORIGINAL DEFECT, and the thing this design exists to make impossible:
    # one genuine AprilTag miss (a hand passing over the table) on a cube nobody
    # touched used to re-grasp it and print „wurde zurückgelegt". Measured
    # 2026-09-07 under the absence rule: the SAME cube grasped twice, at
    # t = 0.28 s and t = 9.11 s.
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    placed = _det(20, _DROP)
    ctx = _scripted_ctx([[placed], [], [placed], [placed]])
    pb._claim_tag(ctx, 20)
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0   # anchor := the drop spot
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0   # ONE missed look
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0   # back, same spot
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0
    assert 20 in ctx.claimed_tags
    assert _reclaim_lines(ctx) == []


def test_a_tag_missed_on_five_consecutive_observations_is_never_reclaimed():
    # No COUNTER value can work, which is why there is no longer a counter: five
    # misses of an untouched cube are still five misses. (A counter of 5 — or of
    # any n ≤ 5 — reclaims here.)
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    placed = _det(20, _DROP)
    ctx = _scripted_ctx([[placed], [], [], [], [], [], [placed]])
    pb._claim_tag(ctx, 20)
    for _ in range(7):
        assert pb.count_unclaimed_visible(ctx, 'banane') == 0
    assert 20 in ctx.claimed_tags
    assert _reclaim_lines(ctx) == []


def test_an_object_moved_without_ever_being_absent_is_reclaimed():
    # The decisive case for choosing POSITION over absence: a student who SLIDES
    # the cube back never makes it disappear for a single observation, so every
    # absence rule scores zero here.
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    ctx = _scripted_ctx([[_det(20, _PICK)], [_det(20, _PICK)], [_det(20, _DROP)]])
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1   # still up for grabs
    pb._claim_tag(ctx, 20)
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0   # anchor := where it lies
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1   # 100 mm away → moved
    assert 20 not in ctx.claimed_tags


def test_a_place_plus_one_miss_does_not_reclaim():
    # THE TRAP. Anchoring on the last sighting BEFORE the absence re-creates the
    # original defect exactly: the pre-absence sighting is the PICK spot, the
    # post-absence one is the DROP spot, and the robot's own placement then reads
    # as „moved 100 mm". The anchor must be observed AFTER the claim.
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    ctx = _scripted_ctx([[_det(20, _PICK)], [], [_det(20, _DROP)], [_det(20, _DROP)]])
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1   # seen at the pick spot
    pb._claim_tag(ctx, 20)                                  # …grasped, placed at DROP
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0   # first post-claim look MISSED
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0   # now visible at DROP
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0
    assert 20 in ctx.claimed_tags
    assert _reclaim_lines(ctx) == []


def test_an_object_returned_to_its_pick_spot_after_being_gone_is_reclaimed():
    # The PICK rule: the robot carried it away, so only a person can have put it
    # back where it started.
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    ctx = _scripted_ctx([[_det(20, _PICK)], [], [_det(20, _PICK)]])
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1   # pick spot recorded
    pb._claim_tag(ctx, 20)
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0   # carried out of view
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1   # back at the pick spot
    assert 20 not in ctx.claimed_tags


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


def test_the_pick_rule_says_the_object_is_back_where_it_was():
    """C-1. The PICK rule fires on ``_back_at`` — i.e. having JUST ESTABLISHED
    the object is NOT elsewhere — and it shared the ANCHOR rule's sentence, so it
    printed „liegt jetzt woanders" about a cube standing on its own square.
    Reproduced by execution against the real ``_reclaim_recycled``: byte-
    identical sentences for the two OPPOSITE observations, 100 % of the time, on
    the put-back demo the whole rule exists for."""
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    ctx = _scripted_ctx([[_det(20, _PICK)], [], [_det(20, _PICK)]])
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1
    pb._claim_tag(ctx, 20)
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0   # carried out of view
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1   # back on its own spot
    lines = _reclaim_lines(ctx)
    assert len(lines) == 1, f'expected one reclaim line, got {lines}'
    assert 'liegt wieder an der alten Stelle' in lines[0], lines[0]
    assert 'woanders' not in lines[0], (
        'the PICK rule just measured that the object is NOT elsewhere')
    # „an DER alten Stelle" — a possessive („an seinem/ihrem alten Platz") would
    # agree with the LABEL's gender and re-introduce the leak.
    assert 'seinem' not in lines[0] and 'ihrem' not in lines[0]


def test_the_anchor_rule_still_says_woanders():
    """The other side of C-1: a claimed object that genuinely moved away from
    where the robot LEFT it keeps its own sentence."""
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    ctx = _scripted_ctx([[_det(20, _PICK)], [_det(20, _PICK)], [_det(20, _DROP)]])
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1
    pb._claim_tag(ctx, 20)
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0   # anchor := the pick spot
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1   # 100 mm away
    lines = _reclaim_lines(ctx)
    assert len(lines) == 1, f'expected one reclaim line, got {lines}'
    assert 'liegt jetzt woanders' in lines[0], lines[0]
    assert 'alten Stelle' not in lines[0], (
        'sending every reclaim down the PICK branch is the mirror defect')


def test_the_three_reclaim_sentences_are_three_distinct_sentences():
    """A source fence. The defect was ONE sentence serving TWO opposite
    observations, so what has to be pinned is that they are DIFFERENT — and that
    the module holds exactly one copy of each, so a future edit cannot quietly
    share one again."""
    import inspect
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    tpl = pb._RECLAIM_TEMPLATES_DE
    assert set(tpl) == {'pick', 'anchor', 'skip'}, tpl
    assert len(set(tpl.values())) == 3, f'two rules share a sentence: {tpl}'
    import ast as _ast
    src = inspect.getsource(pb)
    # STRING LITERALS only — the comment explaining the defect naturally quotes
    # the sentence, and a raw text count would forbid documenting it.
    literals = [n.value for n in _ast.walk(_ast.parse(src))
                if isinstance(n, _ast.Constant) and isinstance(n.value, str)]
    for sentence in ('liegt jetzt woanders', 'liegt wieder an der alten Stelle',
                     'wurde bewegt'):
        hits = [lit for lit in literals if sentence in lit]
        assert len(hits) == 1, (
            f'„{sentence}" appears in {len(hits)} string literals — two rules '
            'sharing one sentence is the defect this table exists to stop')
    # _reclaim takes the rule from its CALLER: it pops the anchor as its third
    # statement, so anything re-derived inside it reads destroyed state.
    assert 'def _reclaim(tag: int, rule: str) -> None:' in src, (
        'the rule must be PASSED in, not re-derived inside _reclaim')


def test_the_pick_rule_needs_a_prior_absence():
    # Degenerate but legal program: the drop point IS the pick point. Without the
    # prior-absence precondition the PICK rule would fire on the robot's own
    # placement every pass and „Solange sichtbar" would never terminate.
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    ctx = _scripted_ctx([[_det(20, _PICK)]])
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1
    pb._claim_tag(ctx, 20)
    for _ in range(4):
        assert pb.count_unclaimed_visible(ctx, 'banane') == 0
    assert 20 in ctx.claimed_tags
    assert _reclaim_lines(ctx) == []


def test_a_skipped_out_of_reach_object_moved_into_reach_is_retried():
    # SKIP anchoring: the robot never moved this one, so its anchor is where it
    # lay when we gave up — which lets a student slide it into reach and have it
    # retried in the SAME run.
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    ctx = _scripted_ctx([[_det(20, _OUT_OF_REACH)], [_det(20, _PICK)]])
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1
    pb._skip_tag(ctx, 20)                                   # „außerhalb des Greifbereichs"
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1   # moved into reach → retry
    assert 20 not in ctx.skipped_tags
    assert any('wurde bewegt' in m for m in ctx.logs)


def test_a_fresh_claim_re_anchors_instead_of_reusing_the_old_reference():
    # claims.py::_forget_position_state. A tag SKIPPED where it lay gets an anchor
    # there; a student can then „merke als erledigt" it (mark_done → _claim_tag)
    # and the program places it elsewhere. Carrying the SKIP-time anchor into the
    # new claim would compare the drop spot against it and un-claim the object on
    # the very next look. The pick spot is deliberately NOT forgotten — the PICK
    # rule needs it.
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    ctx = _scripted_ctx([[_det(20, _PICK)], [_det(20, _PICK)], [_det(20, _DROP)],
                         [_det(20, _DROP)]])
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1
    pb._skip_tag(ctx, 20)
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0   # skip anchor := the pick spot
    assert ctx.claim_anchor.get(20) is not None, 'premise: the skip anchored it'
    pb._claim_tag(ctx, 20)                                  # …„merke als erledigt"
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0   # placed at DROP → re-anchor
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0
    assert 20 in ctx.claimed_tags
    assert ctx.claim_pick_xy.get(20) is not None, 'the pick spot must survive a claim'
    assert _reclaim_lines(ctx) == []


def test_a_move_below_the_threshold_does_not_reclaim():
    # 5 mm — the student brushed past it. Well above the 0.164 mm worst-case
    # projection noise, well below the 20 mm threshold and the 30 mm cube.
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    ctx = _scripted_ctx([[_det(20, _PICK)], [_det(20, _PICK)], [_det(20, _NUDGED)],
                         [_det(20, _NUDGED)]])
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1
    pb._claim_tag(ctx, 20)
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0   # anchor := the pick spot
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0   # nudged 5 mm
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0
    assert 20 in ctx.claimed_tags
    assert _reclaim_lines(ctx) == []


def test_reclaim_move_zero_disables_the_reclaim(monkeypatch):
    # EDUBOTICS_RECLAIM_MOVE_M=0 is the one-variable rollback, and it must DISABLE
    # the reclaim — not turn a `distance >= 0` comparison into "reclaim always".
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    monkeypatch.setattr(pb, '_RECLAIM_MOVE_M', 0.0)
    ctx = _scripted_ctx([[_det(20, _PICK)], [_det(20, _PICK)], [_det(20, _DROP)],
                         [_det(20, _DROP)]])
    assert pb.count_unclaimed_visible(ctx, 'banane') == 1
    pb._claim_tag(ctx, 20)
    for _ in range(3):
        assert pb.count_unclaimed_visible(ctx, 'banane') == 0
    assert 20 in ctx.claimed_tags
    assert _reclaim_lines(ctx) == []


def test_find_object_and_wait_until_do_not_mutate_the_claim_state():
    # „finde" and „warte bis" are READS, exactly like „sehe ich" / „Anzahl".
    # „finde" is called MID-CARRY by the taught split-grasp pattern, where the
    # held object's projected position travels with the gripper — running the
    # anchor rule there un-claims the cube the robot is HOLDING. „warte bis"
    # polls ~5×/s against a rule the loop advances once per 15.6 s pass.
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    ctx = _Ctx(_StubPerception([_det(20, _DROP)]))
    pb._claim_tag(ctx, 20)
    ctx.claim_anchor[20] = _PICK          # …as if the robot had left it at _PICK
    before_anchor = dict(ctx.claim_anchor)
    before_unseen = set(ctx.claim_unseen)
    # Both would reclaim tag 20 (it is 100 mm from its anchor) if they ran it.
    assert pb.find_object(ctx, {'object_type': 'banane'}) is None
    assert pb.wait_until_object_seen(
        ctx, {'object_type': 'banane', 'timeout': 0.05}) is False
    assert ctx.claimed_tags == {20}
    assert ctx.claim_anchor == before_anchor
    assert ctx.claim_unseen == before_unseen
    assert _reclaim_lines(ctx) == []


def test_the_reclaim_message_names_the_tag_the_way_the_printed_sheet_does():
    # tools/generate_apriltags.py prints „#20 Würfel" on the sheet the student has
    # in front of them, so the Protokoll says #20 — not „(20)". And the sentence
    # reports what was OBSERVED (it lies elsewhere), never an intention nobody
    # watched: „wurde zurückgelegt" asserted a put-back that had not been seen.
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    moved = _scripted_ctx([[_det(20, _PICK)], [_det(20, _PICK)], [_det(20, _DROP)]])
    pb.count_unclaimed_visible(moved, 'banane')
    pb._claim_tag(moved, 20)
    pb.count_unclaimed_visible(moved, 'banane')
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


def test_one_dropped_frame_does_not_reclaim_however_long_the_pass_took(monkeypatch):
    # The regression test for the loop-pass-cadence defect: with the wall-clock
    # debounce fully satisfied (0.0 s), a SINGLE absent observation still must not
    # un-claim. Before _RECLAIM_ABSENT_PASSES this reclaimed on frame 3.
    from physical_ai_server.workflow.handlers import perception_blocks as pb
    monkeypatch.setattr(pb, '_RECLAIM_ABSENT_S', 0.0)
    det = _det(20, (0.18, 0.0))
    frames = {'mode': 'present'}
    ctx = _Ctx(_StubPerception(lambda _c: ([det] if frames['mode'] == 'present' else [])))
    ctx.claimed_tags.add(20)
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0
    frames['mode'] = 'absent'
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0      # ONE dropped frame
    frames['mode'] = 'present'
    assert pb.count_unclaimed_visible(ctx, 'banane') == 0
    assert 20 in ctx.claimed_tags, 'one dropped frame must not re-grasp'
    assert not any('zurückgelegt' in m for m in ctx.logs)


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

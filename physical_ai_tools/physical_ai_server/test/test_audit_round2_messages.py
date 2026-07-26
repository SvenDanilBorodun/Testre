"""The 2026-07-26 sim-sweep round-2 fixes that are about TELLING THE TRUTH.

F4 — the split grasp path misdiagnosed every out-of-reach cube as „you never ran
     finde", and told a TOO-NEAR object to move nearer.
F5 — the 3rd virtual cube was silently dropped (only ``_logger`` heard about it)
     and the student was then told the cube was not „sichtbar".
F8 — ``_set_follower_torque`` threw away ``res.message``, so the edu6 driver's
     specific German refusal never reached the Roboter-Studio toast.

F1/F2/F3/F6/F7 live in ``test_dof_n6.py`` (slice 2g) next to the real-solver
fixtures they need.
"""

from __future__ import annotations

import ast
import math
import textwrap
import types
from pathlib import Path

import numpy as np
import pytest

from physical_ai_server import robot_profiles
from physical_ai_server.workflow.handlers import motion
from physical_ai_server.workflow.handlers import perception_blocks as pb
from physical_ai_server.workflow.handlers.motion import GraspSkip, WorkflowError
from physical_ai_server.workflow.object_catalog import fixed_catalog
from physical_ai_server.workflow.sim_perception import SimPerception

_EDU6 = 'edu6_studio'
_GENERIC = motion._NO_GREIFZIEL_MSG


def _sim_ctx(objects, profile_id=_EDU6):
    """A ctx wired to synthetic perception + the real solver, with calibration
    ABSENT exactly like a sim run (so ``_attach_named_world`` preserves the
    pre-set world coordinates)."""
    p = robot_profiles.resolve(profile_id)
    cat = fixed_catalog(profile_id)
    ctx = types.SimpleNamespace(
        ik=p.build_ik(),
        num_arm_joints=p.num_arm_joints,
        roll_joint_index=p.roll_joint_index,
        home_joints_rad=p.home_joints_rad,
        gripper_open_rad=p.gripper_open_rad,
        gripper_closed_rad=p.gripper_closed_rad,
        velocity_limit_rad_s=p.velocity_limit_rad_s,
        last_arm_joints=None,
        last_full_joints=list(p.home_joints_rad) + [p.gripper_open_rad],
        zones=None, tempo=1.0, destinations={},
        perception=SimPerception(objects, cat),
        object_catalog=cat,
        scene_intrinsics=None, scene_extrinsics=None,
        board_table_z=None, z_table=None,
        get_scene_frame=lambda: np.zeros((1, 1, 3), dtype=np.uint8),
        get_scene_frame_age=lambda: 0.0,
        claimed_tags=set(), skipped_tags=set(), absent_since={}, claim_lock=None,
        emit_detections=lambda _d: None,
        should_stop=lambda: False,
        motion_lock=None,
    )
    ctx.logs = []
    ctx.log = ctx.logs.append
    return ctx


# ══ F4 ════════════════════════════════════════════════════════════════════════

def test_unreachable_cube_no_longer_blamed_on_the_student():
    """MEASURED before the fix: ``find_object`` returns ``None`` for an
    unreachable object, so „fahre über" raised „Kein Greifziel — bitte „finde …"
    benutzen, das Ergebnis in einer Variable speichern und mit „falls" prüfen." —
    telling a student who had done exactly those three things to do them. The true
    reason appeared only as a ``[WARNUNG]`` log line."""
    ctx = _sim_ctx([{'type': 'wuerfel', 'x': 0.40, 'y': 0.0, 'yaw': 0.0}])
    ziel = pb.find_object(ctx, {'object_type': 'wuerfel'})
    assert ziel is None, 'premise: this cube is out of reach'
    with pytest.raises(GraspSkip) as excinfo:
        motion.move_above(ctx, {'ziel': ziel})
    msg = str(excinfo.value)
    assert 'Greifbereich' in msg, msg
    assert msg != _GENERIC
    assert 'Variable speichern' not in msg, (
        'the student DID latch it into a variable — do not blame them for that')
    # The guard hint is still offered, as a parenthetical, not as the diagnosis.
    assert 'falls Ziel' in msg


def test_every_split_block_reports_the_real_reason():
    """All four „no Greifziel" sites share one promotion helper."""
    ctx = _sim_ctx([{'type': 'wuerfel', 'x': 0.40, 'y': 0.0, 'yaw': 0.0}])
    assert pb.find_object(ctx, {'object_type': 'wuerfel'}) is None
    for label, call in (
        ('move_above', lambda: motion.move_above(ctx, {'ziel': None})),
        ('descend_to', lambda: motion.descend_to(ctx, {'ziel': None})),
        ('close_on_object', lambda: motion.close_on_object(ctx, {'ziel': None})),
        ('mark_done', lambda: pb.mark_done(ctx, {'ziel': None})),
    ):
        with pytest.raises(GraspSkip) as excinfo:
            call()
        assert 'Greifbereich' in str(excinfo.value), f'{label}: {excinfo.value}'


def test_nothing_found_is_distinguished_from_never_having_searched():
    """The two cases the old message collapsed together."""
    # (a) „finde" ran and found nothing at all.
    empty = _sim_ctx([])
    assert pb.find_object(empty, {'object_type': 'wuerfel'}) is None
    with pytest.raises(GraspSkip) as nothing:
        motion.move_above(empty, {'ziel': None})
    assert 'sichtbar' in str(nothing.value)
    assert str(nothing.value) != _GENERIC

    # (b) „finde" was NEVER called — the one case the generic message describes.
    fresh = _sim_ctx([])
    with pytest.raises(GraspSkip) as never:
        motion.move_above(fresh, {'ziel': None})
    assert str(never.value) == _GENERIC, (
        'with no recorded reason the message must be byte-identical to before')


def test_a_successful_find_clears_a_stale_reason():
    """Otherwise a later genuinely-missing Greifziel would inherit an old excuse."""
    ctx = _sim_ctx([{'type': 'wuerfel', 'x': 0.40, 'y': 0.0, 'yaw': 0.0}])
    assert pb.find_object(ctx, {'object_type': 'wuerfel'}) is None
    assert getattr(ctx, 'last_find_failure', None)
    # Now place it in reach and find it again.
    reachable = ctx.ik.base_axis_x + 0.14
    ctx.perception = SimPerception(
        [{'type': 'wuerfel', 'x': reachable, 'y': 0.0, 'yaw': 0.0}],
        ctx.object_catalog)
    ctx.skipped_tags.clear()
    ctx.claimed_tags.clear()
    assert pb.find_object(ctx, {'object_type': 'wuerfel'}) is not None
    assert ctx.last_find_failure is None
    with pytest.raises(GraspSkip) as excinfo:
        motion.move_above(ctx, {'ziel': None})
    assert str(excinfo.value) == _GENERIC


@pytest.mark.parametrize('placement,expect,forbid', [
    ('far', 'NÄHER an den Roboter', 'WEITER WEG'),
    ('near', 'WEITER WEG', 'NÄHER an den Roboter'),
    ('behind', 'VOR den Roboter', 'WEITER WEG'),
])
def test_out_of_reach_direction_is_correct(placement, expect, forbid):
    """The old single message said „bitte näher … legen" for ALL of them — the
    exact opposite of the fix for a cube that is too NEAR, and irrelevant for one
    behind the arm (joint 1 cannot aim there at any radius)."""
    p = robot_profiles.resolve(_EDU6)
    axis = p.build_ik().base_axis_x
    xy = {'far': (axis + 0.40, 0.0),
          'near': (axis + 0.004, 0.0),
          'behind': (axis - 0.16, 0.0)}[placement]
    ctx = _sim_ctx([{'type': 'wuerfel', 'x': xy[0], 'y': xy[1], 'yaw': 0.0}])
    assert pb.find_object(ctx, {'object_type': 'wuerfel'}) is None
    reason = ctx.last_find_failure
    assert expect in reason, f'{placement}: {reason}'
    assert forbid not in reason, f'{placement} got the WRONG direction: {reason}'


def test_composite_grasp_gets_the_same_direction_correct_reason():
    """``grasp_object`` named the right CLASS of failure but still the wrong
    DIRECTION; both paths now share ``_out_of_reach_reason``."""
    p = robot_profiles.resolve(_EDU6)
    axis = p.build_ik().base_axis_x
    ctx = _sim_ctx([{'type': 'wuerfel', 'x': axis + 0.004, 'y': 0.0, 'yaw': 0.0}])
    with pytest.raises(GraspSkip) as excinfo:
        pb.grasp_object(ctx, {'object_type': 'wuerfel'})
    assert 'WEITER WEG' in str(excinfo.value), str(excinfo.value)


def test_reach_classifier_is_failsafe_and_never_breaks_a_run():
    """A diagnostic may not become a new failure mode: no solver, a solver that
    raises, and a degenerate target all fall back to the direction-neutral text
    (which is true in every case)."""
    neutral = 'nicht zu nah am Roboter'
    no_ik = types.SimpleNamespace(ik=None)
    assert neutral in pb._out_of_reach_reason(no_ik, 'Würfel', (0.4, 0.0, 0.0))

    class _Boom:
        base_axis_x = 0.0

        def solve(self, *a, **k):
            raise RuntimeError('solver exploded')

    assert neutral in pb._out_of_reach_reason(
        types.SimpleNamespace(ik=_Boom()), 'Würfel', (0.4, 0.0, 0.0))
    # Degenerate: exactly on the joint-1 axis, so there is no bearing to probe.
    p = robot_profiles.resolve(_EDU6)
    ctx = types.SimpleNamespace(ik=p.build_ik())
    assert neutral in pb._out_of_reach_reason(
        ctx, 'Würfel', (ctx.ik.base_axis_x, 0.0, 0.0))


def test_note_find_failure_survives_a_write_protected_ctx():
    """``_note_find_failure`` must never raise — a frozen/slotted ctx would
    otherwise turn a diagnostic into a crash."""
    class _Frozen:
        __slots__ = ()

    pb._note_find_failure(_Frozen(), 'irgendein Grund')     # must not raise
    assert motion._no_greifziel_error(_Frozen()).args[0] == _GENERIC


# ══ F5 ════════════════════════════════════════════════════════════════════════

_SERVER_PY = (Path(__file__).resolve().parents[1]
              / 'physical_ai_server' / 'physical_ai_server.py')


def _load_method(name: str):
    source = _SERVER_PY.read_text(encoding='utf-8')
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            ns: dict = {}
            exec(compile(textwrap.dedent(ast.get_source_segment(source, node)),  # noqa: S102
                         str(_SERVER_PY), 'exec'), ns)
            return ns[name]
    raise AssertionError(f'{name} not found in {_SERVER_PY}')


_warn_capacity = _load_method('_warn_sim_objects_beyond_tag_capacity')


class _CapacityNode:
    """Minimal self for the extracted method."""

    def __init__(self, catalog):
        self._catalog = catalog
        self.emitted: list = []

    def _load_object_catalog_tolerant(self):
        return self._catalog

    def _emit_workflow_status(self, payload):
        self.emitted.append(payload)

    def get_logger(self):
        return types.SimpleNamespace(warning=lambda *a, **k: None,
                                     error=lambda *a, **k: None)


def _cube(x=0.12, y=0.0, tag_id=None):
    obj = {'type': 'wuerfel', 'x': x, 'y': y, 'yaw': 0.0}
    if tag_id is not None:
        obj['tag_id'] = tag_id
    return obj


def test_third_cube_overflow_reaches_the_student_not_only_the_container_log():
    """``SimPerception`` caps each type at ``len(recipe.tag_ids)`` (2 for the
    shipped „Würfel") and its overflow warning went to ``_logger`` only — the
    container log the student never opens. So a 3rd cube in plain view inside the
    reach band simply never became a detection, and the student was then told
    „Kein „Würfel" SICHTBAR — bitte das Objekt in den markierten Greifbereich
    legen"."""
    cat = fixed_catalog(_EDU6)
    node = _CapacityNode(cat)
    _warn_capacity(node, [_cube(0.12), _cube(0.14, 0.04), _cube(0.13, -0.05)])
    assert len(node.emitted) == 1, node.emitted
    msg = node.emitted[0]['log_message']
    assert msg.startswith('[WARNUNG]')
    assert '3 „Würfel"' in msg
    assert 'nur 2' in msg
    # It must say what to DO, and must not repeat the „not visible" lie.
    assert 'entfernen' in msg
    assert 'sichtbar' not in msg


def test_no_capacity_warning_when_the_placement_fits():
    cat = fixed_catalog(_EDU6)
    for objs in ([], [_cube()], [_cube(0.12), _cube(0.15)]):
        node = _CapacityNode(cat)
        _warn_capacity(node, objs)
        assert node.emitted == [], f'{objs} fits — nothing to warn about'


def test_capacity_warning_matches_what_sim_perception_actually_drops():
    """The warning's capacity number must be the REAL one, not a guess: exactly
    ``len(recipe.tag_ids)`` detections come out no matter how many are placed."""
    cat = fixed_catalog(_EDU6)
    placed = [_cube(0.12), _cube(0.14, 0.04), _cube(0.13, -0.05), _cube(0.15, 0.07)]
    got = SimPerception(placed, cat).detect(None, 'scene', 'apriltag')
    capacity = len(cat.recipe_for_type('wuerfel').tag_ids)
    assert len(got) == capacity
    node = _CapacityNode(cat)
    _warn_capacity(node, placed)
    assert f'nur {capacity}' in node.emitted[0]['log_message']
    assert f'{len(placed)} „Würfel"' in node.emitted[0]['log_message']


def test_sim_perception_ignores_the_callers_tag_id():
    """Documented in the warning's rationale — verify it rather than trust it."""
    cat = fixed_catalog(_EDU6)
    ids = [cat.recipe_for_type('wuerfel').tag_ids[i] for i in (0, 1)]
    forward = SimPerception([_cube(0.12, tag_id=ids[0]),
                             _cube(0.14, 0.04, tag_id=ids[1])],
                            cat).detect(None, 'scene', 'apriltag')
    reverse = SimPerception([_cube(0.12, tag_id=ids[1]),
                             _cube(0.14, 0.04, tag_id=ids[0])],
                            cat).detect(None, 'scene', 'apriltag')
    assert [d.aruco_id for d in forward] == [d.aruco_id for d in reverse] == ids


def test_capacity_warning_never_blocks_a_run():
    """Every failure mode of the diagnostic is swallowed."""
    class _BadCatalog:
        def recipe_for_type(self, _t):
            raise RuntimeError('boom')

    node = _CapacityNode(_BadCatalog())
    _warn_capacity(node, [_cube(), _cube(), _cube()])
    assert node.emitted == []

    class _NoCatalogNode(_CapacityNode):
        def _load_object_catalog_tolerant(self):
            return None

    node = _NoCatalogNode(None)
    _warn_capacity(node, [_cube(), _cube(), _cube()])
    assert node.emitted == []

    # Malformed entries (a crafted /workflow/start payload) must not raise.
    node = _CapacityNode(fixed_catalog(_EDU6))
    _warn_capacity(node, ['not a dict', {'no_type': 1}, None])
    assert node.emitted == []


def test_capacity_warning_is_wired_into_the_sim_start_path():
    """A perfect helper nobody calls is worthless — assert the call site exists on
    the sim branch, right where ``_sim_objects`` is assigned."""
    source = _SERVER_PY.read_text(encoding='utf-8')
    assert 'self._warn_sim_objects_beyond_tag_capacity(self._sim_objects)' in source
    idx = source.index('self._warn_sim_objects_beyond_tag_capacity(self._sim_objects)')
    before = source[:idx]
    assert 'self._sim_objects = list(raw_objs) if isinstance(raw_objs, list) else []' \
        in before[-400:], 'the warning must sit on the sim-start path'

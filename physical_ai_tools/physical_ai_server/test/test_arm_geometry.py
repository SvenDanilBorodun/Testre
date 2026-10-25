"""Whole-link geometry (``workflow/arm_geometry.py``) — the table-floor and
self-collision model behind the safe Grundstellung: the module's own contracts
(tilted table, self-collision pairs, the OMX short-circuit, the swept sampling
rule) on the REAL V1 solver.

The mesh-containment and soundness tests — the boxes re-derived from the
shipped URDF and STLs with a parser that shares no code with the solver — live
in ``test_arm_geometry_v1.py``.
"""

from __future__ import annotations

import math

import numpy as np

from physical_ai_server import robot_profiles as RP
from physical_ai_server.workflow import arm_geometry as AG
from physical_ai_server.workflow.ik_solver import IKSolver
from physical_ai_server.workflow.v1_ik import V1IKSolver

_V1 = RP.ROBOT_PROFILES['v1_studio']
_HOME = list(_V1.home_joints_rad)

_UNSET = object()


def _ctx(ik=_UNSET):
    """Minimal ctx stand-in: resolve_geometry only ever reads ``.ik``.

    ``_UNSET`` (the default) means "a real V1 solver"; an explicit ``None``
    means "a ctx whose ik really is None" and must stay distinguishable — an
    earlier version collapsed the two and silently never tested the None case.
    """
    class _C:
        pass
    c = _C()
    c.ik = V1IKSolver() if ik is _UNSET else ik
    return c


# ── 2. the floor model is SOUND ──────────────────────────────────────────────

def test_floor_clearance_honours_a_tilted_table_plane():
    """``floor_fn`` is evaluated at each corner's OWN (x, y), so a tilted table
    measured by the touch-off is respected per point instead of against one
    scalar. Fails if someone collapses the callback to a constant."""
    geom = AG.resolve_geometry(_ctx())
    flat = geom.floor_clearance(_HOME, lambda _x, _y: 0.0)
    # a plane tilted 0.2 rad about y: the floor rises with x
    tilted = geom.floor_clearance(_HOME, lambda x, _y: 0.2 * x)
    assert tilted is not None and flat is not None
    assert tilted < flat


def test_no_legitimate_table_level_grasp_is_below_the_floor_allowance():
    """Strict-vertical grasps across the whole pick band must stay within the
    20 mm model allowance, or the guard would refuse ordinary grasping.

    Measured basis (V1, 112 such poses): the model's lowest reading is -0.5 mm
    (the fingertip touching the table, by design).
    """
    from physical_ai_server.workflow.handlers import motion as M
    ik = V1IKSolver()
    geom = AG.resolve_geometry(_ctx(ik))
    checked = 0
    for r in np.linspace(0.10, 0.34, 16):
        for yaw in np.linspace(-1.3, 1.3, 7):
            q = ik.solve((ik.base_axis_x + r * math.cos(yaw),
                          r * math.sin(yaw), 0.0), roll=M.GRASP_ROLL_RAD)
            if q is None:
                continue
            checked += 1
            assert geom.floor_clearance(q, lambda _x, _y: 0.0) > -0.020
    assert checked > 50


# ── 3. self-collision ────────────────────────────────────────────────────────

def test_self_clearance_is_positive_at_home():
    """The box bound at HOME (22.0 mm) is pessimistic but must stay positive,
    or the warn-only check would fire on the rest pose."""
    geom = AG.resolve_geometry(_ctx())
    assert geom.self_clearance(_HOME) > 0.0


def test_the_static_base_is_excluded_from_the_floor_but_kept_for_self_collision():
    """base_link's underside IS the table (z ∈ [0, 0.0521] m). Counting it in the
    floor check would peg every reading at ≈0 and the guard would judge nothing.
    It must still be a self-collision partner — the claw CAN reach the base."""
    geom = AG.resolve_geometry(_ctx())
    assert geom.floor_clearance(_HOME, lambda _x, _y: 0.0) > 0.005
    base_pairs = [p for p in AG._SELF_PAIRS if p[0] == 0]
    assert base_pairs, 'base_link must remain a self-collision partner'
    assert (0, 5) in base_pairs


def test_self_clearance_never_tests_adjacent_links():
    """Links one joint apart share that joint and their meshes interpenetrate
    there by construction. Testing them would report a permanent collision."""
    assert all(j - i >= 2 for i, j in AG._SELF_PAIRS)
    assert (0, 2) in AG._SELF_PAIRS
    assert (0, 1) not in AG._SELF_PAIRS
    assert (4, 5) not in AG._SELF_PAIRS


# ── 4. the OMX short-circuit — the bit-identity mechanism ────────────────────

def test_resolve_geometry_is_none_for_every_omx_profile_and_solver():
    """The mechanism that keeps the OMX byte-identical. If this ever returns an
    ArmGeometry, every new guard starts judging the OMX too — which is exactly
    the regression the design forbids."""
    assert AG.resolve_geometry(_ctx(IKSolver())) is None
    assert AG.resolve_geometry(IKSolver()) is None
    for pid in ('omx_full', 'omx_follower'):
        profile = RP.ROBOT_PROFILES[pid]
        assert AG.resolve_geometry(_ctx(profile.build_ik())) is None


def test_resolve_geometry_is_none_for_a_profileless_or_stub_ctx():
    class _Stub:
        backend = 'closed-form-v1'        # right backend, no link_frames
        def num_joints(self):
            return 5
    assert AG.resolve_geometry(_ctx(_Stub())) is None
    assert AG.resolve_geometry(_ctx(None)) is None
    assert AG.resolve_geometry(object()) is None


def test_resolve_geometry_never_raises_on_a_hostile_solver():
    class _Angry:
        def link_frames(self, _joints):
            return None
        @property
        def backend(self):
            raise RuntimeError('boom')
    assert AG.resolve_geometry(_ctx(_Angry())) is None


def test_resolve_geometry_is_live_for_v1():
    geom = AG.resolve_geometry(_ctx())
    assert geom is not None and geom.num_links == 6


# ── 5. the swept sampling rule is path_guard's, not a second one ─────────────

def test_swept_sampling_reuses_the_path_guard_constants():
    """The tunneling argument must be the SAME argument the zone check already
    makes. Fails if someone re-declares the constants locally, which is how the
    two would silently drift apart."""
    from physical_ai_server.workflow import path_guard as PG
    assert AG._MAX_SEGMENT_SAMPLES is PG._MAX_SEGMENT_SAMPLES
    assert AG._R_MAX_M is PG._R_MAX_M
    assert AG.STEP_M is PG.STEP_M
    # a big swing must be sampled far more finely than a tiny one, and capped
    assert AG._segment_samples([0.0] * 6, [0.0] * 6) == 1
    assert AG._segment_samples([0.0] * 6, [0.0, 0.05, 0, 0, 0, 0]) >= 7
    assert (AG._segment_samples([0.0] * 6, [0.0, 3.1, 0, 0, 0, 0])
            == PG._MAX_SEGMENT_SAMPLES)


def test_swept_floor_clearance_is_the_minimum_over_the_line():
    """The swept value must be no greater than either endpoint's — it is a
    minimum over the whole line, and the line is what gets published."""
    geom = AG.resolve_geometry(_ctx())
    a = [0.009, 2.885, 0.047, -1.289, -1.384]
    swept = geom.swept_floor_clearance(a, _HOME, lambda _x, _y: 0.0)
    assert swept is not None
    assert swept <= geom.floor_clearance(a, lambda _x, _y: 0.0) + 1e-12
    assert swept <= geom.floor_clearance(_HOME, lambda _x, _y: 0.0) + 1e-12


def test_the_measured_worst_start_is_detected_as_a_table_press():
    """The witness from the 2026-10-25 sweep (test_home_planner): from a start
    6.5 mm above the table this start's straight glide to HOME drives a link
    234 mm below it in the box model. The model must see it. Fails if the box
    table or the sweep regresses — i.e. this is the test that proves the guard
    detects the defect it was built for."""
    geom = AG.resolve_geometry(_ctx())
    start = [0.009, 2.885, 0.047, -1.289, -1.384]
    swept = geom.swept_floor_clearance(start, _HOME, lambda _x, _y: 0.0)
    assert swept is not None
    assert swept < -0.10, f'expected a deep press, model saw {swept:.4f} m'

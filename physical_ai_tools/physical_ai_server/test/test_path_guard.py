"""No-go-zone + IK path-guard tests (Roboter Studio Phase 4).

Pure NumPy + the real closed-form ``IKSolver`` (no container / rclpy). Covers:

* ``NoGoZone`` point-in / point-out of an INFLATED box.
* ``segment_blocked`` tunneling-safety: a thin link + thin wall that COARSE
  waypoint sampling misses but the joint-delta resample catches.
* the ``plan_safe_route`` ladder picking direct / lift-and-travel / base-swing /
  refuse.
* the deterministic (single-branch) elbow handling + the joint-1 x-offset
  azimuth used by the base-swing.

The motion-level wiring (safe_move reroutes a blocked transit, descend stays
exempt) lives in ``test_motion_handlers.py``; ctx.zones population + the
pre-flight live in ``test_workflow_zones`` cases there / here.
"""

from __future__ import annotations

import math
import threading

import numpy as np
import pytest

from physical_ai_server.workflow import path_guard
from physical_ai_server.workflow.handlers.motion import (
    GRASP_ROLL_RAD,
    GRIPPER_OPEN_RAD,
    WorkflowError,
)
from physical_ai_server.workflow.ik_solver import IKSolver
from physical_ai_server.workflow.path_guard import (
    NoGoZone,
    build_zones,
    plan_safe_route,
    point_in_any_zone,
    segment_blocked,
)


class _Ctx:
    """Minimal ctx the reroute solver reads (ik + zones + the floor fields the
    motion solver helpers touch)."""

    def __init__(self, ik, zones=None, z_table=0.0):
        self.ik = ik
        self.zones = zones
        self.z_table = z_table
        self.table_plane = None
        self.last_arm_joints = None
        self.logs: list[str] = []

    def log(self, msg):
        self.logs.append(msg)


def _full(ik, xyz, roll=GRASP_ROLL_RAD, gripper=GRIPPER_OPEN_RAD):
    arm = ik.solve(xyz, roll=roll)
    assert arm is not None, f'{xyz} unreachable'
    return list(arm) + [gripper]


# ── NoGoZone / point-in-box ───────────────────────────────────────────────────

def test_nogozone_contains_with_inflation():
    z = NoGoZone([0.0, 0.0, 0.0], [0.10, 0.10, 0.10], inflation=0.02)
    # inside the raw box
    assert z.contains((0.05, 0.05, 0.05))
    # just OUTSIDE the raw box but INSIDE the inflated box (0.10 + 0.02)
    assert z.contains((0.115, 0.05, 0.05))
    # outside the inflated box
    assert not z.contains((0.13, 0.05, 0.05))


def test_point_in_any_zone_raw_vs_inflated():
    zones = [{'min': [0.0, 0.0, 0.0], 'max': [0.10, 0.10, 0.10]}]
    p = (0.115, 0.05, 0.05)  # 1.5 cm outside the raw box in x
    assert point_in_any_zone(zones, p, inflation=0.0) is False
    assert point_in_any_zone(zones, p, inflation=0.02) is True


def test_build_zones_skips_malformed():
    zones = [
        {'min': [0.0, 0.0, 0.0], 'max': [0.1, 0.1, 0.1]},   # ok
        {'min': [0.0, 0.0], 'max': [0.1, 0.1, 0.1]},         # wrong len
        {'min': [0.0, 0.0, float('nan')], 'max': [0.1, 0.1, 0.1]},  # NaN
        'not a dict',
        {'max': [0.1, 0.1, 0.1]},                            # missing min
    ]
    built = build_zones(zones, 0.0)
    assert len(built) == 1


def test_build_zones_normalises_swapped_corners():
    # min/max swapped per axis is normalised (defensive; validator enforces too).
    built = build_zones([{'min': [0.1, 0.1, 0.1], 'max': [0.0, 0.0, 0.0]}], 0.0)
    assert len(built) == 1
    assert built[0].contains((0.05, 0.05, 0.05))


# ── tunneling-safe segment check ──────────────────────────────────────────────

def _blocked_coarse(ik, qs, qe, zones, n, inflation):
    """Reference COARSE swept check at exactly ``n`` evenly-spaced joint-space
    samples against the (un-inflated) boxes — the naive sampler that tunnels."""
    boxes = build_zones(zones, inflation)
    a = np.asarray(qs[:5], float)
    d = np.asarray(qe[:5], float) - a
    for i in range(n + 1):
        for p in ik.link_points(a + d * (i / n)):
            for b in boxes:
                if b.contains(p):
                    return True
    return False


def test_segment_blocked_no_zones_is_false():
    ik = IKSolver()
    qs = _full(ik, (0.18, -0.10, 0.05))
    qe = _full(ik, (0.18, 0.10, 0.05))
    assert segment_blocked(ik, qs, qe, None, 0.0) is False
    assert segment_blocked(ik, qs, qe, [], 0.0) is False


def test_segment_blocked_tunneling_thin_wall():
    # A THIN wall straddling y=0; small inflation (thin link) so the wall stays
    # thin. The arm sweeps laterally from -y to +y, crossing the wall.
    ik = IKSolver()
    # asymmetric endpoints so a coarse 3-sample grid skips y≈0
    qs = _full(ik, (0.18, -0.11, 0.05))
    qe = _full(ik, (0.18, 0.09, 0.05))
    wall = [{'min': [0.10, -0.005, 0.0], 'max': [0.26, 0.005, 0.20]}]

    # COARSE sampling with NO inflation tunnels straight through the 1 cm wall.
    assert _blocked_coarse(ik, qs, qe, wall, n=3, inflation=0.0) is False
    # The joint-delta resample (few-mm step) CATCHES it, even with a thin link
    # (tiny inflation) and zero user margin.
    assert segment_blocked(
        ik, qs, qe, wall, margin=0.0, link_radius=0.001, step_m=0.003) is True


def test_segment_blocked_clear_path_far_zone():
    ik = IKSolver()
    qs = _full(ik, (0.18, -0.10, 0.05))
    qe = _full(ik, (0.18, 0.10, 0.05))
    # a zone off to the far +y side, nowhere near the arm's swept volume
    far = [{'min': [-0.05, 0.30, 0.0], 'max': [0.05, 0.40, 0.10]}]
    assert segment_blocked(ik, qs, qe, far, 0.02) is False


def test_segment_blocked_pure_rotation_arc_tunneling():
    # FIX 2 — arc-aware bound. A near-full-revolution PURE j1 rotation: q_start
    # and q_end share j2..j5 and differ ONLY in j1 (≈ -π → +π). The two endpoint
    # configs point almost the SAME direction (back, azimuth ≈ ±π), so the
    # straight-LINE chord between their link points is tiny → the OLD chord-only
    # sample count is ~1 and a coarse check at that count only inspects the two
    # near-coincident BACK-pointing endpoints, TUNNELING straight past a wall the
    # arm actually sweeps through at the FRONT (j1 ≈ 0, mid-rotation). The new
    # arc-aware bound samples the whole swept arc and CATCHES it.
    ik = IKSolver()
    base = ik.solve((0.20, 0.0, 0.05), roll=0.0)
    assert base is not None
    qs = [-3.135] + list(base[1:5]) + [GRIPPER_OPEN_RAD]
    qe = [3.140] + list(base[1:5]) + [GRIPPER_OPEN_RAD]
    # A narrow wall at the FRONT (y ≈ 0, +x), reached only at j1 ≈ 0 mid-sweep.
    wall = [{'min': [0.10, -0.02, 0.0], 'max': [0.26, 0.02, 0.18]}]

    # Reconstruct the OLD chord-only sample count.
    lp_s = ik.link_points(qs[:5])
    lp_e = ik.link_points(qe[:5])
    max_disp = max(float(np.linalg.norm(np.asarray(a) - np.asarray(b)))
                   for a, b in zip(lp_s, lp_e))
    n_chord = max(1, int(math.ceil(max_disp / 0.003)))
    # The huge rotation barely moves the (back-pointing) endpoints → tiny count.
    assert n_chord <= 2, n_chord
    # A coarse check at the chord-only count TUNNELS (misses the front wall).
    assert _blocked_coarse(ik, qs, qe, wall, n=n_chord, inflation=0.001) is False
    # The arc-aware segment check CATCHES the sweep through the wall.
    assert segment_blocked(
        ik, qs, qe, wall, margin=0.0, link_radius=0.001, step_m=0.003) is True


def test_segment_blocked_sample_count_grows_with_joint_delta():
    # FIX 2 — the arc-aware bound makes the sample count N scale with the max
    # joint delta, so a bigger pure rotation forces MORE samples (a thin wall
    # can't hide in a big swing). Count link_points evaluations against a FAR
    # zone (never hit → no short-circuit → the full N+1 samples run).
    ik = IKSolver()
    base = ik.solve((0.20, 0.0, 0.05), roll=0.0)
    assert base is not None
    far = [{'min': [-0.05, 0.32, 0.0], 'max': [0.02, 0.40, 0.10]}]

    def _sample_count(dj1):
        qs = [-dj1 / 2.0] + list(base[1:5]) + [GRIPPER_OPEN_RAD]
        qe = [dj1 / 2.0] + list(base[1:5]) + [GRIPPER_OPEN_RAD]
        calls = {'n': 0}
        real = ik.link_points

        def _counting(*a, **k):
            calls['n'] += 1
            return real(*a, **k)

        ik.link_points = _counting
        try:
            assert segment_blocked(ik, qs, qe, far, 0.02) is False
        finally:
            ik.link_points = real
        return calls['n']

    small = _sample_count(0.2)
    large = _sample_count(2.4)
    assert large > small, (small, large)


# ── reroute ladder ────────────────────────────────────────────────────────────

def _legs_all_clear(ik, legs, zones, margin=path_guard.ZONE_MARGIN_M):
    return all(not segment_blocked(ik, a, b, zones, margin) for a, b, _d in legs)


def test_plan_safe_route_direct_when_clear():
    ik = IKSolver()
    ctx = _Ctx(ik, zones=[{'min': [-0.05, 0.30, 0.0], 'max': [0.05, 0.40, 0.10]}])
    qs = _full(ik, (0.18, -0.10, 0.05))
    qe = _full(ik, (0.18, 0.10, 0.05))
    legs = plan_safe_route(ctx, qs, qe, ctx.zones, 2.5)
    assert len(legs) == 1
    assert legs[0][0] == pytest.approx(qs)
    assert legs[0][1] == pytest.approx(qe)


def test_plan_safe_route_lift_and_travel():
    ik = IKSolver()
    # a LOW wall straddling y=0 → the direct lateral sweep is blocked, but the
    # arm can lift over it (clear_z is reachable, below SAFE_TRAVEL_Z).
    wall = [{'min': [0.06, -0.03, 0.0], 'max': [0.22, 0.03, 0.05]}]
    ctx = _Ctx(ik, zones=wall)
    qs = _full(ik, (0.14, -0.12, 0.03))
    qe = _full(ik, (0.14, 0.12, 0.03))
    assert segment_blocked(ik, qs, qe, wall, path_guard.ZONE_MARGIN_M) is True
    legs = plan_safe_route(ctx, qs, qe, wall, 2.5)
    assert len(legs) == 3                      # lift → travel → descend
    assert _legs_all_clear(ik, legs, wall)
    # final leg ends EXACTLY at q_end (gripper + roll preserved)
    assert legs[-1][1] == pytest.approx(qe)
    # the cruise via rose above the start height
    _r0, t0 = ik.fk(qs[:5])
    _r1, tv = ik.fk(legs[0][1][:5])
    assert tv[2] > t0[2] + 0.02


def test_plan_safe_route_base_swing_around_tall_zone():
    # A TALL pillar off to the far front-right (x 0.22-0.26, z up to 0.40): it
    # can't be LIFTED over (clear_z >> SAFE_TRAVEL_Z), but the arm can swing its
    # base around it. The ladder falls through lift → base-swing naturally.
    ik = IKSolver()
    pillar = [{'min': [0.22, -0.02, 0.0], 'max': [0.26, 0.02, 0.40]}]
    ctx = _Ctx(ik, zones=pillar)
    qs = _full(ik, (0.16, -0.14, 0.04))
    qe = _full(ik, (0.16, 0.14, 0.04))
    assert segment_blocked(ik, qs, qe, pillar, path_guard.ZONE_MARGIN_M) is True
    # lift can't clear a 0.40 m pillar
    assert path_guard._plan_lift_and_travel(
        ctx, qs, qe, pillar, path_guard.ZONE_MARGIN_M, path_guard.LINK_RADIUS_M,
        GRIPPER_OPEN_RAD, GRASP_ROLL_RAD, 2.5) is None
    legs = plan_safe_route(ctx, qs, qe, pillar, 2.5)
    assert len(legs) == 2                      # via → destination (base-swing)
    assert _legs_all_clear(ik, legs, pillar)
    assert legs[-1][1] == pytest.approx(qe)


def test_plan_safe_route_refuses_when_no_route():
    ik = IKSolver()
    # a giant tall box covering the whole reachable region → nothing clears.
    giant = [{'min': [-0.5, -0.5, 0.0], 'max': [0.5, 0.5, 0.5]}]
    ctx = _Ctx(ik, zones=giant)
    qs = _full(ik, (0.18, -0.10, 0.04))
    qe = _full(ik, (0.18, 0.10, 0.04))
    with pytest.raises(WorkflowError) as exc:
        plan_safe_route(ctx, qs, qe, giant, 2.5)
    assert 'Sperrzone' in str(exc.value)


def test_base_swing_via_uses_j1_axis_offset_azimuth():
    # The base-swing via XY is built from the joint-1 x-offset bearing
    # (J1_AXIS_X + r·cos az, r·sin az) — the same offset solve()/base_yaw use.
    # Verify a via solved by the swing reaches base_yaw == its own azimuth.
    ik = IKSolver()
    r, az = 0.20, 0.6
    vx = ik.base_axis_x + r * math.cos(az)
    vy = r * math.sin(az)
    q = ik.solve((vx, vy, 0.10))
    assert q is not None
    assert abs(ik.base_yaw(vx, vy) - q[0]) < 1e-9


def test_plan_safe_route_gripper_continuity_through_vias():
    # FIX 3 — a reroute changes the gripper ONCE, only on the final leg arriving
    # at q_end. Outbound/intermediate vias inherit q_START's gripper so a caller
    # that passes MISMATCHED grippers doesn't actuate the gripper during the
    # first transit leg. (Latent hardening — current callers are consistent.)
    ik = IKSolver()
    wall = [{'min': [0.06, -0.03, 0.0], 'max': [0.22, 0.03, 0.05]}]
    ctx = _Ctx(ik, zones=wall)
    g_start, g_end = GRIPPER_OPEN_RAD, -0.4    # mismatched
    qs = _full(ik, (0.14, -0.12, 0.03), gripper=g_start)
    qe = _full(ik, (0.14, 0.12, 0.03), gripper=g_end)
    legs = plan_safe_route(ctx, qs, qe, wall, 2.5)
    assert len(legs) >= 2                      # a real reroute happened
    # Continuity IN: the first leg starts from q_start's gripper.
    assert legs[0][0][5] == pytest.approx(g_start)
    # Every leg EXCEPT the last KEEPS q_start's gripper (no mid-transit change).
    for _a, b, _d in legs[:-1]:
        assert b[5] == pytest.approx(g_start)
    # The gripper changes exactly once, on the FINAL leg arriving at q_end.
    assert legs[-1][1] == pytest.approx(qe)
    assert legs[-1][1][5] == pytest.approx(g_end)


# ── WorkflowManager wiring: ctx.zones + pre-flight ───────────────────────────
import json  # noqa: E402
import time  # noqa: E402

from physical_ai_server.workflow.interpreter import Interpreter  # noqa: E402
from physical_ai_server.workflow.workflow_manager import WorkflowManager  # noqa: E402


def test_parse_zones_extracts_top_level_sibling():
    z = [{'min': [0.0, 0.0, 0.0], 'max': [0.1, 0.1, 0.1]}]
    assert WorkflowManager._parse_zones(
        json.dumps({'blocks': {'blocks': []}, 'zones': z})) == z


def test_parse_zones_defensive():
    assert WorkflowManager._parse_zones('{"blocks":{"blocks":[]}}') is None  # no key
    assert WorkflowManager._parse_zones('not valid json') is None
    assert WorkflowManager._parse_zones(json.dumps({'zones': 'notalist'})) is None
    assert WorkflowManager._parse_zones(json.dumps(['a', 'list'])) is None


def test_start_populates_ctx_zones():
    captured = {}

    class _CapMgr(WorkflowManager):
        def _run(self, interpreter, ctx):
            captured['ctx'] = ctx
            return super()._run(interpreter, ctx)

    zones = [{'min': [0.1, 0.1, 0.0], 'max': [0.2, 0.2, 0.1]}]
    mgr = _CapMgr(publisher=lambda _c: None, ik_factory=lambda: IKSolver())
    wf = {'blocks': {'blocks': []}, 'zones': zones}   # empty program → finishes fast
    ok, msg, _unreachable = mgr.start(json.dumps(wf), 'wf-zones')
    assert ok, msg
    deadline = time.monotonic() + 5.0
    while 'ctx' not in captured and time.monotonic() < deadline:
        time.sleep(0.01)
    assert captured.get('ctx') is not None
    assert captured['ctx'].zones == zones


def _move_to_pin_wf(x, y, z, zones):
    return json.dumps({
        'blocks': {'blocks': [{
            'type': 'edubotics_move_to',
            'id': 'm1',
            'inputs': {'DESTINATION': {'block': {
                'type': 'edubotics_destination_pin',
                'id': 'pin1',
                'fields': {'X': str(x), 'Y': str(y), 'Z': str(z)},
            }}},
        }]},
        'zones': zones,
    })


def test_preflight_flags_concrete_pin_in_zone():
    # A reachable pin that sits inside a no-go zone is flagged on the pre-flight
    # unreachable list with the German Sperrzone message (keyed to the move_to
    # block id).
    zones = [{'min': [0.10, -0.10, 0.0], 'max': [0.30, 0.10, 0.20]}]
    raw = _move_to_pin_wf(0.20, 0.0, 0.05, zones)
    interp = Interpreter.from_json(raw)
    mgr = WorkflowManager(publisher=lambda _c: None, ik_factory=lambda: IKSolver())
    out = mgr._ik_precheck(interp, IKSolver(), zones)
    hits = [u for u in out if u['block_id'] == 'm1' and 'Sperrzone' in u['message']]
    assert hits, out


def test_preflight_clean_when_pin_outside_zone():
    zones = [{'min': [0.10, 0.15, 0.0], 'max': [0.20, 0.25, 0.20]}]  # off to +y
    raw = _move_to_pin_wf(0.20, 0.0, 0.05, zones)
    interp = Interpreter.from_json(raw)
    mgr = WorkflowManager(publisher=lambda _c: None, ik_factory=lambda: IKSolver())
    out = mgr._ik_precheck(interp, IKSolver(), zones)
    assert not any('Sperrzone' in u['message'] for u in out)

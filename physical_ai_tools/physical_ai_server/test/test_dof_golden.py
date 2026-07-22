"""PR-2 proofs (§16.4): golden-vector + characterization fixtures for the
DOF generalisation.

The four-slice literal→``n`` substitution (2a trajectory_builder → 2b handlers →
2c path_guard/workflow_manager/sim_arm → 2d Communicator/node) must be
OMX-bit-identical. Nearly every touched site fails by producing a *plausible
wrong number* rather than crashing, so these tests pin the CURRENT behaviour as
committed fixtures and assert **exact equality** (``==`` on the JSON round-trip
— json floats use shortest-round-trip repr, so equality is bit-exact) after
every slice:

* ``test_golden_sim_workflow_vector`` — a fixed sim workflow (home →
  find/move_above/descend/close/lift grasp-split → replay → open_gripper); its
  full published joint stream is the fixture. One boolean for "did any slice
  change what the arm is told to do?".
* ``test_golden_{build_segment,resegment,extract_points,jog}`` — per-function
  characterization over fixed inputs, so a per-function regression can't hide
  behind compensating errors in the end-to-end vector.

Regenerate (ONLY on an intended behaviour change, never to green a slice):
``EDUBOTICS_REGEN_GOLDEN=1 python -m pytest test/test_dof_golden.py``.
"""

from __future__ import annotations

import ast
import json
import os
import textwrap
import threading
import time
import types
from pathlib import Path

import numpy as np
import pytest

from physical_ai_server.workflow import trajectory_builder
from physical_ai_server.workflow.handlers.trajectory import (
    extract_points,
    resegment_trajectory,
)
from physical_ai_server.workflow.edu6_ik import Edu6IKSolver
from physical_ai_server.workflow.ik_solver import IKSolver
from physical_ai_server.workflow.object_catalog import parse_catalog
from physical_ai_server.workflow.sim_arm import SimArm
from physical_ai_server.workflow.sim_perception import SimPerception
from physical_ai_server.workflow.workflow_manager import WorkflowManager


_FIXTURE = Path(__file__).parent / 'fixtures' / 'dof_golden.json'
_REGEN = os.environ.get('EDUBOTICS_REGEN_GOLDEN', '').strip() not in ('', '0')


def _check(key: str, value):
    """Assert ``value`` equals the committed fixture entry (exact ==), or write
    it when regenerating. Values must be JSON-serializable (lists/floats)."""
    # Round-trip through JSON so the compared object has exactly the types the
    # fixture will load with (tuples → lists, np scalars → floats).
    value = json.loads(json.dumps(value))
    if _REGEN:
        data = {}
        if _FIXTURE.exists():
            data = json.loads(_FIXTURE.read_text(encoding='utf-8'))
        data[key] = value
        _FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        _FIXTURE.write_text(
            json.dumps(data, indent=1, sort_keys=True) + '\n', encoding='utf-8')
        return
    assert _FIXTURE.exists(), (
        'fixture missing — run EDUBOTICS_REGEN_GOLDEN=1 pytest '
        'test/test_dof_golden.py once and commit test/fixtures/dof_golden.json')
    data = json.loads(_FIXTURE.read_text(encoding='utf-8'))
    assert key in data, f'fixture key {key!r} missing — regenerate + review'
    assert value == data[key], (
        f'golden vector {key!r} CHANGED — a DOF slice altered OMX behaviour '
        '(must be bit-identical). Diff the structures; only regenerate on an '
        'intended, reviewed behaviour change.')


@pytest.fixture(autouse=True)
def _fast_chunk_pacing(monkeypatch):
    """Fake ``trajectory_builder.time`` so inter-chunk pacing is instant (the
    published waypoints are unchanged). Mirrors test_sim_runtime."""
    state = {'t': 0.0}

    def _monotonic():
        state['t'] += 1000.0
        return state['t']

    fake = types.SimpleNamespace(monotonic=_monotonic, sleep=lambda _s: None)
    monkeypatch.setattr(trajectory_builder, 'time', fake)
    yield


# ── the golden sim workflow ──────────────────────────────────────────────────

_SIM_CATALOG = parse_catalog({
    'tag_size_m': 0.024,
    'types': {
        'wuerfel': {
            'label_de': 'Würfel',
            'tag_ids': [7, 8],
            'object_height_m': 0.03,
            'grasp_depth_m': 0.012,
            'gripper_close_rad': -0.25,
            'approach_clear_m': 0.06,
        },
    },
})

_VAR = {'type': 'variables_get', 'fields': {'VAR': {'name': 'Ziel'}}}

# home → grasp-split (find → über → senke → schließe → hebe) → replay → open.
_GOLDEN_PROGRAM = {
    'trajectories': {
        'T1': {
            'fps': 25,
            'points': [
                [0.05, -1.20, 1.10, 0.02, 0.10, 0.30, 0.00],
                [0.10, -1.10, 1.00, 0.04, 0.20, 0.10, 0.20],
                [0.15, -1.00, 0.90, 0.06, 0.30, -0.10, 0.40],
                [0.20, -0.95, 0.85, 0.08, 0.40, -0.25, 0.65],
            ],
        },
    },
    'blocks': {'blocks': [{
        'type': 'edubotics_home',
        'id': 'h1',
        'next': {'block': {
            'type': 'variables_set',
            'id': 'set1',
            'fields': {'VAR': {'name': 'Ziel'}},
            'inputs': {'VALUE': {'block': {
                'type': 'edubotics_find_object',
                'id': 'find1',
                'fields': {'OBJECT_TYPE': 'wuerfel'},
            }}},
            'next': {'block': {
                'type': 'edubotics_move_above',
                'id': 'ma1',
                'inputs': {'ZIEL': {'block': dict(_VAR, id='v1')}},
                'next': {'block': {
                    'type': 'edubotics_descend_to',
                    'id': 'dt1',
                    'inputs': {'ZIEL': {'block': dict(_VAR, id='v2')}},
                    'next': {'block': {
                        'type': 'edubotics_close_on_object',
                        'id': 'co1',
                        'inputs': {'ZIEL': {'block': dict(_VAR, id='v3')}},
                        'next': {'block': {
                            'type': 'edubotics_lift',
                            'id': 'lift1',
                            'next': {'block': {
                                'type': 'edubotics_replay_trajectory',
                                'id': 'rp1',
                                'fields': {'NAME': 'T1'},
                                'next': {'block': {
                                    'type': 'edubotics_open_gripper',
                                    'id': 'og1',
                                }},
                            }},
                        }},
                    }},
                }},
            }},
        }},
    }]},
}

_OBJ = {'type': 'wuerfel', 'tag_id': 7, 'x': 0.20, 'y': 0.0, 'yaw': 0.3}


def test_golden_sim_workflow_vector():
    captured: list[list[float]] = []
    status: list = []
    sim_arm = SimArm(joint_state_sink=captured.append, ik=IKSolver(),
                     objects=[_OBJ])
    mgr = WorkflowManager(
        publisher=sim_arm.publish,
        ik_factory=lambda: IKSolver(),
        perception_factory=lambda: SimPerception([_OBJ], _SIM_CATALOG),
        load_destinations=lambda: {},
        load_calibration=lambda: {},
        emit_status=lambda ev: status.append(ev),
        on_finished=lambda phase: status.append({'_finished': phase}),
        get_scene_frame=lambda: np.zeros((1, 1, 3), dtype=np.uint8),
        get_scene_frame_age=lambda: 0.0,
        get_current_pose_xyz=lambda: sim_arm.fk_xyz(),
        get_follower_joints=sim_arm.get_joints,
        load_object_catalog=lambda: _SIM_CATALOG,
    )
    ok, msg, _ = mgr.start(json.dumps(_GOLDEN_PROGRAM), 'wf-golden')
    assert ok, msg
    deadline = time.monotonic() + 30.0
    done = lambda: [e for e in status  # noqa: E731
                    if isinstance(e, dict) and '_finished' in e]
    while not done() and time.monotonic() < deadline:
        time.sleep(0.01)
    finished = done()
    assert finished and finished[-1]['_finished'] == 'finished', (
        f'golden workflow did not finish cleanly: {status[-3:]}')
    errors = [e for e in status
              if isinstance(e, dict) and e.get('phase') == 'error']
    assert not errors, f'golden workflow errored: {errors}'
    assert len(captured) > 100, 'suspiciously short golden vector'
    _check('sim_workflow_vector', captured)


# ── per-function characterization ────────────────────────────────────────────

def test_golden_build_segment():
    out = trajectory_builder.build_segment(
        [0.0, -1.5708, 1.5708, 0.0, 0.0, 0.8],
        [0.4, -0.9, 1.1, 0.2, -0.3, -0.5],
        1.7,
    )
    _check('build_segment', out)


def test_golden_build_segment_velocity_floored():
    # A big single-joint swing that trips _velocity_safe_duration.
    out = trajectory_builder.build_segment(
        [-3.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        [3.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        0.5,
    )
    _check('build_segment_floored', out)


_CONTRACT_B = [
    [0.00, -1.40, 1.30, 0.00, 0.00, 0.80, 0.00],
    [0.05, -1.30, 1.20, 0.05, 0.10, 0.60, 0.30],
    [0.12, -1.18, 1.05, 0.10, 0.22, 0.40, 0.55],
    [0.18, -1.05, 0.95, 0.15, 0.35, 0.10, 0.90],
]


def test_golden_extract_points():
    rows = extract_points({'fps': 25, 'points': _CONTRACT_B})
    _check('extract_points', rows)
    # extract_points asserts EXACT width (n+2) in BOTH directions (§16.4 rail #2):
    # a short row was always refused; a WIDE row used to TRUNCATE silently and now
    # refuses too. Two rows each so the WIDTH guard trips — a single row would refuse
    # for the wrong reason (the <2-points "keine Bewegung" guard runs first), so the
    # 'beschädigt' match proves it is the width guard specifically.
    with pytest.raises(Exception, match='beschädigt'):
        extract_points({'fps': 25, 'points': [[0.0] * 6, [0.0] * 6]})  # short → refuse
    with pytest.raises(Exception, match='beschädigt'):
        extract_points({'fps': 25, 'points': [[0.0] * 8, [0.0] * 8]})  # wide → refuse


def test_golden_resegment():
    out = resegment_trajectory(
        [list(r) for r in _CONTRACT_B],
        speed=1.5,
        lead_in_from=[0.0, -1.5708, 1.5708, 0.0, 0.0, 0.8],
        lead_in_duration=1.5,
    )
    _check('resegment', out)


def test_golden_resegment_with_velocities():
    out = resegment_trajectory(
        [list(r) for r in _CONTRACT_B],
        speed=0.75,
        lead_in_from=[0.0, -1.5708, 1.5708, 0.0, 0.0, 0.8],
        with_velocities=True,
    )
    _check('resegment_velocities', out)


# ── jog clamp characterization (ast-extracted, mirrors test_workshop_jog) ────

_SERVER_PY = (
    Path(__file__).resolve().parents[1]
    / 'physical_ai_server' / 'physical_ai_server.py'
)


def _load_node_fns(names):
    source = _SERVER_PY.read_text(encoding='utf-8')
    tree = ast.parse(source)
    ns: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            src = textwrap.dedent(ast.get_source_segment(source, node))
            exec(compile(src, str(_SERVER_PY), 'exec'), ns)  # noqa: S102
    return {n: ns[n] for n in names}


class _JogStub:
    def __init__(self):
        fns = _load_node_fns(['_compute_jog_target', '_jog_solve_floor'])
        self._compute_jog_target = types.MethodType(fns['_compute_jog_target'], self)
        self._jog_solve_floor = types.MethodType(fns['_jog_solve_floor'], self)

    def _build_ik_solver(self):
        return IKSolver()

    def _load_workflow_calibration(self):
        return {'z_table': 0.01}


def _jog_req(mode, index=0, delta=0.0, tx=0.0, ty=0.0, tz=0.0):
    return types.SimpleNamespace(mode=mode, index=index, delta=delta,
                                 target_x=tx, target_y=ty, target_z=tz)


def test_golden_jog():
    stub = _JogStub()
    live = [0.05, -1.45, 1.42, 0.03, 0.10, 0.35]
    results = {}
    q, world = stub._compute_jog_target('joint', _jog_req('joint', 1, 0.12), live)
    results['joint'] = [q, list(world)]
    q, world = stub._compute_jog_target('joint', _jog_req('joint', 5, -0.2), live)
    results['gripper'] = [q, list(world)]
    q, world = stub._compute_jog_target(
        'cartesian', _jog_req('cartesian', 0, 0.02), live)
    results['cartesian_x'] = [q, list(world)]
    q, world = stub._compute_jog_target(
        'drive_to', _jog_req('drive_to', tx=0.18, ty=0.04, tz=0.05), live)
    results['drive_to'] = [q, list(world)]
    _check('jog', results)


# ── capture-pose FK characterization (finding 10, additive) ──────────────────
# The golden sim-workflow fixture omits the capture-pose ("Position merken")
# touch-off, which drives the FULL-vector FK behind touch-off / destination_
# current. Pin ik.fk() directly on fixed 6- and 7-element full vectors so a
# geometry regression in either solver surfaces without a fixture regen. The
# expected numbers were computed at test-writing time and hardcoded as literals
# here — this test deliberately owns its own numbers and never reads/writes
# dof_golden.json.

def test_capture_pose_fk_omx_6_element_vector():
    # OMX fk takes joint1..joint5; the 6th (gripper) entry is IGNORED — the
    # capture-pose contract passes a full follower vector straight in.
    ik = IKSolver()
    full6 = [0.10, -1.20, 1.10, 0.05, 0.30, 0.80]
    R, t = ik.fk(full6)
    assert list(t) == pytest.approx(
        [0.17921821472056176, 0.017574352817596783, 0.19891016125921557],
        abs=1e-9)
    assert list(R[0]) == pytest.approx(
        [0.9937606691655042, -0.11007057243681946, -0.018005596439997214],
        abs=1e-9)
    assert list(R[2]) == pytest.approx(
        [0.04997916927067818, 0.29515088335498707, 0.9541425672790117],
        abs=1e-9)
    # Extra gripper entry ignored: the 5-element solve equals the 6-element one.
    _R5, t5 = ik.fk(full6[:5])
    assert list(t5) == pytest.approx(list(t), abs=1e-12)


def test_capture_pose_fk_edu6_7_element_vector():
    # edu6 fk takes joint1..joint6; the 7th (gripper) entry is IGNORED. Vector is
    # edu6 HOME + gripper-open; fk returns the WORLD-frame fingertip TCP.
    ik = Edu6IKSolver()
    full7 = [0.0, 0.70, -2.40, 0.0, 0.70, 0.0, 1.75]
    R, t = ik.fk(full7)
    assert list(t) == pytest.approx(
        [0.04990694748239571, 0.0, 0.4545133420264562], abs=1e-9)
    assert list(R[0]) == pytest.approx(
        [0.0, -0.8414709848078965, -0.5403023058681398], abs=1e-9)
    assert list(R[1]) == pytest.approx([-1.0, 0.0, 0.0], abs=1e-9)
    # Extra gripper entry ignored: the 6-element solve equals the 7-element one.
    _R6, t6 = ik.fk(full7[:6])
    assert list(t6) == pytest.approx(list(t), abs=1e-12)

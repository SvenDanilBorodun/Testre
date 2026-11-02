"""The D4 motion gate on the manual paths, by behaviour (fix round 1, F6 and F8).

The REAL ``workshop_jog_callback``, ``workshop_replay_callback`` and
``_station_mark_destination`` are extracted from ``physical_ai_server.py`` with
``ast`` and run on a stub node around a real :class:`StationService`.

F6: an unverified station refuses a jog and a replay with the gate's German
sentence before the manual arbiter is asked, and nothing moves.

F8 (plan v6.13 §5.4, "checks verified and captures … in one hold"): a commit,
re-solve or restore that claims, swaps and releases between a jog's or replay's
first check and its ``_mode_lock`` claim revoked verification, and the claim's own
re-check under ``_mode_lock`` → ``_station_lock`` refuses it. A replay whose floor
check was built under a calibration that was swapped (and verified again) before
its claim is refused too. ``_station_mark_destination`` gates and reads the
calibration in one hold, so a swap right after its check never reaches its
projection. Each window is opened deterministically by a lock whose acquire or
release runs the writer.
"""

from __future__ import annotations

import ast
import json
import math
import textwrap
import threading
import time
import types
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip('cv2')

from physical_ai_server.station import texts_de as T  # noqa: E402
from test_station_service import (  # noqa: E402
    _ASSERT, _Log, _activate, _project, _svc)

_SERVER_PY = Path(__file__).resolve().parents[1] / 'physical_ai_server' / 'physical_ai_server.py'


def _extract(name):
    src = _SERVER_PY.read_text(encoding='utf-8')
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            ns = {'math': math, 'time': time, 'json': json, 'threading': threading,
                  '_MANUAL_LOCK_TIMEOUT_S': 0.01}
            exec(compile(textwrap.dedent(ast.get_source_segment(src, n)), str(_SERVER_PY),
                         'exec'), ns)  # noqa: S102
            return ns[name]
    raise AssertionError(name)


JOG = _extract('workshop_jog_callback')
REPLAY = _extract('workshop_replay_callback')
MARK = _extract('_station_mark_destination')


class HookLock:
    """A lock that runs ``hook`` once, on the next acquire (or release)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.on_acquire = None
        self.on_release = None
        self.entered = 0

    def acquire(self, *a, **k):
        self.entered += 1
        hook, self.on_acquire = self.on_acquire, None
        if hook:
            hook()
        return self._lock.acquire(*a, **k)

    def release(self):
        self._lock.release()
        hook, self.on_release = self.on_release, None
        if hook:
            hook()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


class ManualNode:
    """The node's manual-arbiter surface, recording what a call reached."""

    def __init__(self, svc, n=5):
        self._station = svc
        self._mode_lock = HookLock()
        self.on_recording = self.on_inference = self.is_training = False
        self.on_calibration = self.on_workflow = self.on_manual = False
        self.on_leader_teach = False
        self._collision_active = False
        self._manual_transient_ops = 0
        self._manual_persistent = False
        self._manual_exit_gen = 0
        self._manual_last_activity_mono = 0.0
        self._manual_record_active = False
        self._manual_replay_thread = None
        self._workflow_traj_publisher = object()
        self._arm_profile = None
        self._n = n
        self.communicator = types.SimpleNamespace(
            get_latest_follower_joints=lambda: [0.0] * (n + 1))
        self.arbiter_calls = 0
        self.manual_lock_reached = 0
        self.bound = []
        self.floor_built_under = []
        self.replays = []
        self._manual_lock = types.SimpleNamespace(acquire=self._manual_lock_acquire)

    def _manual_lock_acquire(self, timeout=None):
        # The motion path's first step: stop here (the arm is "busy"), so a call
        # that got this far answers „Der Arm führt gerade eine andere Bewegung aus“.
        self.manual_lock_reached += 1
        return False

    def get_logger(self):
        return _Log()

    def _follower_joints_stale(self):
        return False

    def _assert_no_other_active(self, mode):
        self.arbiter_calls += 1
        return _ASSERT(self, mode)

    def _recompute_on_manual_locked(self):
        self.on_manual = self._manual_persistent or self._manual_transient_ops > 0

    def _manual_leader_blocks(self):
        return False, ''

    def _station_bind_op(self, cal):
        self.bound.append(cal)

    def _profile_n(self):
        return self._n

    def _build_ik_solver(self):
        from physical_ai_server.workflow.ik_solver import IKSolver
        return IKSolver()

    def _build_replay_lead_floor_check(self):
        self.floor_built_under.append(self.bound[-1] if self.bound else None)
        return None

    def _get_or_create_workflow_manager(self):
        return None

    def _run_replay(self, *a, **k):
        self.replays.append(a)


def _verified(tmp_path):
    svc, stub = _svc(tmp_path, configured=True)
    _activate(svc, stub)
    svc.machine.camera_ok = svc.machine.robot_ok = True
    assert svc.gate_motion() == ''
    return svc


def _swap(svc, reverify=False):
    """A writer's swap (commit, re-solve or restore), as _swap_locked does it under
    _station_lock: a new in-memory calibration, verification revoked."""
    with svc.lock:
        svc._swap_locked(svc.calibration._replace(digest='swapped'))
        if reverify:
            svc.machine.camera_ok = svc.machine.robot_ok = True


def _jog_request():
    return types.SimpleNamespace(mode='joint', joint_index=0, delta=0.1, duration_s=1.0)


def _replay_request():
    pts = [[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], [0.02, 0.0, 0.0, 0.0, 0.0, 0.0, 0.5]]
    return types.SimpleNamespace(points_json=json.dumps({'fps': 25, 'points': pts}),
                                 name='', speed=1.0)


def _resp():
    return types.SimpleNamespace()


# ── F6: the D4 gate refuses before the arbiter, and nothing moves ─────────────
@pytest.mark.parametrize('callback', ['jog', 'replay'])
@pytest.mark.parametrize('configured', [False, True])
def test_an_unverified_station_refuses_before_the_arbiter_and_nothing_moves(
        tmp_path, callback, configured):
    svc, stub = _svc(tmp_path, configured=configured)
    _activate(svc, stub)
    expected = T.GATE_NOT_VERIFIED_DE if configured else T.GATE_NOT_CONFIGURED_DE
    node = ManualNode(svc)
    if callback == 'jog':
        resp = JOG(node, _jog_request(), _resp())
    else:
        resp = REPLAY(node, _replay_request(), _resp())
    assert resp.success is False and resp.message == expected
    assert node.arbiter_calls == 0 and node._mode_lock.entered == 0
    assert node._manual_transient_ops == 0 and node.on_manual is False
    assert node.manual_lock_reached == 0 and node.replays == []


def test_a_verified_station_jog_reaches_the_arm_with_the_captured_calibration(tmp_path):
    svc = _verified(tmp_path)
    node = ManualNode(svc)
    resp = JOG(node, _jog_request(), _resp())
    assert resp.message.startswith('Der Arm führt gerade eine andere Bewegung aus')
    assert node.manual_lock_reached == 1
    # The calibration captured with the claim is served for the jog, then cleared.
    assert node.bound == [svc.calibration, None]
    assert node._manual_transient_ops == 0


def test_a_verified_station_replay_starts_with_its_floor_built_under_the_capture(tmp_path):
    svc = _verified(tmp_path)
    node = ManualNode(svc)
    resp = REPLAY(node, _replay_request(), _resp())
    assert resp.success is True, resp.message
    node._manual_replay_thread.join(2)
    assert len(node.replays) == 1
    assert node.floor_built_under == [svc.calibration]
    assert node.bound[-1] is None


# ── F8: the window between the first check and the claim ──────────────────────
@pytest.mark.parametrize('callback', ['jog', 'replay'])
def test_a_swap_between_the_check_and_the_claim_is_refused_at_the_claim(tmp_path, callback):
    svc = _verified(tmp_path)
    node = ManualNode(svc)
    # The writer claims on_calibration, swaps and releases right before this
    # operation's _mode_lock claim (its first check already passed).
    node._mode_lock.on_acquire = lambda: _swap(svc)
    if callback == 'jog':
        resp = JOG(node, _jog_request(), _resp())
    else:
        resp = REPLAY(node, _replay_request(), _resp())
    assert resp.success is False and resp.message == T.GATE_NOT_VERIFIED_DE
    assert node._manual_transient_ops == 0 and node.on_manual is False
    assert node.manual_lock_reached == 0 and node.replays == []


def test_a_replay_whose_floor_was_built_under_a_replaced_calibration_is_refused(tmp_path):
    """Swapped AND verified again before the claim: the gate passes, but the floor
    check was built under the old calibration, so the replay is refused."""
    svc = _verified(tmp_path)
    node = ManualNode(svc)
    node._mode_lock.on_acquire = lambda: _swap(svc, reverify=True)
    resp = REPLAY(node, _replay_request(), _resp())
    assert resp.success is False and resp.message == T.GATE_CALIBRATION_CHANGED_DE
    assert node._manual_transient_ops == 0 and node.replays == []


def test_mark_destination_gates_and_reads_the_calibration_in_one_hold(tmp_path):
    """A writer swaps right after the gate check: the click is still projected with
    the calibration that was verified, never with the swapped one."""
    svc, stub = _svc(tmp_path, configured=True)
    hook = HookLock()
    svc.lock = hook
    _activate(svc, stub)
    svc.machine.camera_ok = svc.machine.robot_ok = True
    cal1 = svc.calibration
    c1 = cal1.calibration
    T2 = np.array(c1['scene_extrinsics'], dtype=np.float64).copy()
    T2[0, 3] += 0.05
    cal2 = cal1._replace(digest='swapped', calibration=dict(c1, scene_extrinsics=T2))

    def writer():
        svc.calibration = cal2
        svc.machine.digest_changed()

    stored = []
    stub.get_logger = lambda: _Log()
    stub._station_lock = hook
    stub._get_or_create_workflow_manager = lambda: types.SimpleNamespace(
        set_destination=lambda *a, **k: stored.append(a))
    px = _project(c1, 0.2, 0.05, 0.0)
    hook.on_release = writer
    resp = MARK(stub, types.SimpleNamespace(camera='station_scene', pixel_x=float(px[0]),
                                            pixel_y=float(px[1]), label='A'),
                types.SimpleNamespace())
    assert resp.success, resp.message
    assert abs(resp.world_x - 0.2) < 0.002 and abs(resp.world_y - 0.05) < 0.002
    assert svc.calibration is cal2   # the writer did run, right after the check


def test_the_calibration_a_jog_or_replay_captured_is_what_its_floor_reads():
    """``_load_workflow_calibration`` serves the operation's captured calibration on
    its own thread, and the current one everywhere else."""
    load = _extract('_load_workflow_calibration')
    current = types.SimpleNamespace(calibration={'z_table': 0.04, 'digest': 'now'})
    captured = types.SimpleNamespace(calibration={'z_table': 0.03, 'digest': 'then'})
    node = types.SimpleNamespace(_station=types.SimpleNamespace(rig=True, calibration=current),
                                 _station_op=threading.local())
    assert load(node)['digest'] == 'now'
    node._station_op.calibration = captured
    assert load(node)['digest'] == 'then'
    seen = []
    t = threading.Thread(target=lambda: seen.append(load(node)['digest']))
    t.start()
    t.join()
    assert seen == ['now']
    node._station_op.calibration = None
    assert load(node)['digest'] == 'now'

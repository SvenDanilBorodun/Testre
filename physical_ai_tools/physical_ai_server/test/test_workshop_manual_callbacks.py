"""Batch 2b — the manual-control service callbacks (jog / hand_guide / record /
replay) + the record sampler. Extracted by ``ast`` and exec'd onto a fake node,
with the module globals the callbacks reference (time / json / threading /
consts) injected into the exec namespace. Focuses on the SAFETY-critical
behaviour: the on_manual session, torque OFF/ON, and re-torque-KEEPS-the-mutex on
failure (so a later mode never drives a limp arm).
"""

from __future__ import annotations

import ast
import json
import textwrap
import threading
import time
import types
from pathlib import Path

import pytest

_SERVER_PY = (
    Path(__file__).resolve().parents[1]
    / 'physical_ai_server' / 'physical_ai_server.py'
)

# Module consts the callbacks reference (kept in sync with physical_ai_server.py).
_CONSTS = dict(
    RECORD_MAX_S=120.0,
    _MANUAL_RECORD_FPS=25,
    _MANUAL_RECORD_MAX_SAMPLES=int(120.0 * 25) + 100,
    _MANUAL_RECORD_MIN_DELTA_RAD=0.003,
    _MANUAL_LOCK_TIMEOUT_S=2.0,
    _MANUAL_TORQUE_FAIL_LIMIT=3,
    _FOLLOWER_JOINT_MAX_AGE_S=1.0,
    _MANUAL_IDLE_RETORQUE_S=30.0,
)


def _load(names, extra_globals):
    source = _SERVER_PY.read_text(encoding='utf-8')
    tree = ast.parse(source)
    ns: dict = dict(extra_globals)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            src = textwrap.dedent(ast.get_source_segment(source, node))
            exec(compile(src, str(_SERVER_PY), 'exec'), ns)  # noqa: S102
    return {n: ns[n] for n in names}


import math  # noqa: E402 — extracted callbacks reference math.isfinite


class _FakeTaskStatus:
    READY = 0

    def __init__(self):
        self.phase = None
        self.error = ''


_G = {'time': time, 'json': json, 'threading': threading, 'math': math,
      'TaskStatus': _FakeTaskStatus, **_CONSTS}
_CB = _load([
    'workshop_jog_callback', 'workshop_hand_guide_callback',
    'workshop_record_callback', 'workshop_replay_callback',
    '_manual_record_sample', '_run_replay', '_finish_manual_record_on_cap',
    '_manual_idle_watchdog_cb',
], _G)


class _Logger:
    def error(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass


class _Comm:
    def __init__(self, joints):
        self._joints = joints
        self.status_published = []

    def get_latest_follower_joints(self):
        return self._joints

    def publish_status(self, status):
        self.status_published.append(status)


class _JogResp:
    def __init__(self):
        self.success = None
        self.joints = None
        self.world_x = self.world_y = self.world_z = None
        self.message = None


class _Resp:
    """Generic response — carries every field the hand_guide/record/replay
    responses can set."""

    def __init__(self):
        self.success = None
        self.message = None
        self.sample_count = None
        self.duration_s = None
        self.points_json = None


class _Req(types.SimpleNamespace):
    pass


class _FakeNode:
    def __init__(self, *, gate=(True, ''), leader=False, joints=(0.0,) * 6,
                 torque_ok=True, retorque_ok=True, on_manual=None,
                 persistent=False, jog_target=None, stale=False):
        self._gate = gate
        self._leader = leader
        self.communicator = _Comm(list(joints)) if joints is not None else None
        # FIX 1 — session-ownership refcount + hard-exit generation. on_manual is
        # DERIVED from (_manual_persistent or _manual_transient_ops > 0); tests may
        # seed a persistent session via persistent=True (a hand-guide/record open).
        self._manual_transient_ops = 0
        self._manual_persistent = persistent
        self._manual_exit_gen = 0
        self.on_manual = persistent if on_manual is None else on_manual
        self._manual_lock = threading.Lock()
        self._mode_lock = threading.Lock()          # F1 arbiter
        self._manual_stop_event = threading.Event()
        self._handguide_buffer = []
        self._manual_record_active = False
        self._manual_record_timer = None
        self._manual_record_start_mono = 0.0
        self._manual_replay_thread = None
        self._manual_last_activity_mono = 0.0
        self._follower_torque_on = None
        self._manual_torque_fail_count = 0
        self._manual_record_cap_timer = None
        self._manual_record_cap_reached = False
        self._stale = stale                         # F6 stale-readback flag
        self.cap_finish_scheduled = 0
        self._torque_ok = torque_ok
        self._retorque_ok = retorque_ok
        self.torque_calls = []
        self.created_timers = []
        self.destroyed_timers = []
        self.published = []
        self._jog_target = jog_target or ([0.1, -1.4, 1.5, 0.0, 0.0, 0.0],
                                          (0.15, 0.0, 0.05))
        self.replay_runs = []
        self._workflow_traj_publisher = object()  # pretend the pub already exists

    # --- gates / helpers stubbed ---
    def _profile_n(self):
        # ArmProfile arm-joint count (§16.4 2d) — the fake models the OMX rig.
        return 5

    def _assert_no_other_active(self, mode):
        self.last_mode = mode
        return self._gate

    def _manual_leader_blocks(self):
        return (self._leader, 'Leader aktiv.' if self._leader else '')

    def _set_follower_torque(self, enabled):
        self.torque_calls.append(enabled)
        return self._torque_ok

    def _retorque_follower_or_keep_locked(self, response):
        self.torque_calls.append('retorque')
        if self._retorque_ok:
            return True
        response.success = False
        response.message = ('Arm konnte nicht wieder verriegelt werden — der '
                            'Greifer ist noch frei beweglich.')
        return False

    def _compute_jog_target(self, mode, request, joints):
        return list(self._jog_target[0]), tuple(self._jog_target[1])

    def _trajectory_publisher(self, chunk):
        self.published.extend(chunk)

    def _follower_joints_stale(self):
        return self._stale

    def _recompute_on_manual_locked(self):
        # FIX 1 — mirror the real derived-flag helper (called under _mode_lock).
        self.on_manual = bool(
            self._manual_persistent or self._manual_transient_ops > 0)

    def _build_replay_lead_floor_check(self):
        return None  # no floor guard in these unit tests

    def _schedule_manual_record_cap_finish(self):
        self.cap_finish_scheduled += 1

    def _cancel_manual_record_cap_timer(self):
        self._manual_record_cap_timer = None

    def _run_replay(self, segmented, exit_gen):
        self.replay_runs.append((segmented, exit_gen))

    def _manual_record_sample(self):
        # Present so the record-start callback's create_timer(..., self.
        # _manual_record_sample) reference resolves; the sampler itself is
        # exercised directly in the sampler tests.
        pass

    def create_timer(self, period, cb):
        t = object()
        self.created_timers.append((period, cb))
        return t

    def destroy_timer(self, t):
        self.destroyed_timers.append(t)

    def get_logger(self):
        return _Logger()


# ==========================================================================
# jog
# ==========================================================================

def _jog(node, **req):
    defaults = dict(mode='joint', index=0, delta=0.0, target_x=0.0, target_y=0.0,
                    target_z=0.0, duration_s=1.0)
    defaults.update(req)
    return _CB['workshop_jog_callback'](node, _Req(**defaults), _JogResp())


def test_jog_busy_refused():
    node = _FakeNode(gate=(False, 'Aufnahme läuft gerade — bitte zuerst stoppen.'))
    resp = _jog(node)
    assert resp.success is False and 'Aufnahme' in resp.message
    assert node.last_mode == 'manual'
    assert node.torque_calls == []   # never touched the arm


def test_jog_leader_refused():
    node = _FakeNode(leader=True)
    resp = _jog(node)
    assert resp.success is False and 'Leader' in resp.message


def test_jog_no_communicator_refused():
    node = _FakeNode(joints=None)
    resp = _jog(node)
    assert resp.success is False and 'neu starten' in resp.message


def test_jog_torque_on_failure_keeps_session():
    node = _FakeNode(torque_ok=False)
    resp = _jog(node)
    assert resp.success is False and 'verriegelt' in resp.message
    # opened→forced False: on_manual stays True to protect a possibly-limp arm.
    assert node.on_manual is True
    assert node._manual_lock.acquire(blocking=False)  # lock released


def test_jog_happy_path_publishes_and_closes_transient_session():
    node = _FakeNode(joints=(0.0, -1.5, 1.5, 0.0, 0.0, 0.0))
    resp = _jog(node, mode='drive_to', target_x=0.15, target_z=0.05)
    assert resp.success is True
    assert resp.joints == [0.1, -1.4, 1.5, 0.0, 0.0, 0.0]
    assert resp.world_x == pytest.approx(0.15)
    assert node.published, 'nothing published'
    assert node.torque_calls[0] is True   # torque asserted ON
    # Transient session (jog opened it) → closed again on completion.
    assert node.on_manual is False
    assert node._manual_lock.acquire(blocking=False)


def test_jog_within_existing_session_keeps_it_open():
    # A PERSISTENT hand-guide/record session is open; the jog claims + releases a
    # transient ref, but the persistent flag keeps on_manual True.
    node = _FakeNode(persistent=True, joints=(0.0, -1.5, 1.5, 0.0, 0.0, 0.0))
    resp = _jog(node, mode='joint', index=1, delta=0.1)
    assert resp.success is True
    assert node.on_manual is True   # persistent session → stays open
    assert node._manual_transient_ops == 0   # the jog's transient ref released


def test_jog_refused_during_record_precheck():
    node = _FakeNode(joints=(0.0, -1.5, 1.5, 0.0, 0.0, 0.0))
    node._manual_record_active = True
    resp = _jog(node)
    assert resp.success is False and 'Erst die Aufnahme beenden.' == resp.message
    assert node.published == []


def test_jog_refused_when_readback_stale():
    # F6 — a frozen follower readback refuses before claiming the session.
    node = _FakeNode(stale=True, joints=(0.0, -1.5, 1.5, 0.0, 0.0, 0.0))
    resp = _jog(node)
    assert resp.success is False and 'noch nicht bekannt' in resp.message
    assert node.on_manual is False
    assert node.torque_calls == []


def test_jog_refused_on_nonfinite_readback():
    node = _FakeNode(joints=(0.0, float('inf'), 1.5, 0.0, 0.0, 0.0))
    resp = _jog(node)
    assert resp.success is False and 'ungültig' in resp.message


def test_jog_claims_on_manual_atomically_under_mode_lock():
    # F1 — the _assert_no_other_active check + the on_manual set are BOTH under
    # _mode_lock, so a concurrent mode start can't slip between them.
    node = _FakeNode(joints=(0.0, -1.5, 1.5, 0.0, 0.0, 0.0))
    seen = {}

    def _assert(mode):
        node.last_mode = mode
        # The mode lock must be HELD while the ownership check runs.
        seen['locked_during_check'] = node._mode_lock.locked()
        return (True, '')

    node._assert_no_other_active = _assert
    _jog(node, mode='drive_to', target_x=0.15, target_z=0.05)
    assert seen['locked_during_check'] is True


def test_jog_torque_fail_limit_resets_session():
    # F7 — after _MANUAL_TORQUE_FAIL_LIMIT consecutive torque-ON failures the jog
    # clears on_manual so the student is never wedged behind „Handbetrieb".
    node = _FakeNode(torque_ok=False, joints=(0.0, -1.5, 1.5, 0.0, 0.0, 0.0))
    for _ in range(_CONSTS['_MANUAL_TORQUE_FAIL_LIMIT'] - 1):
        resp = _jog(node)
        assert node.on_manual is True   # still protecting a possibly-limp arm
    resp = _jog(node)                    # the limit-th failure
    assert resp.success is False and 'zurückgesetzt' in resp.message
    assert node.on_manual is False       # escaped the wedge
    assert node._manual_transient_ops == 0   # EMERGENCY reset zeroed the refcount


def test_two_concurrent_transient_refs_hold_on_manual():
    # FIX 1 — two overlapping transient claims (two tabs jogging): on_manual stays
    # True until BOTH release, so a workflow/inference start never sees the arm
    # free while the second op still drives. Exercised at the flag level.
    node = _FakeNode()

    def _claim():
        with node._mode_lock:
            node._manual_transient_ops += 1
            node._recompute_on_manual_locked()

    def _release():
        with node._mode_lock:
            node._manual_transient_ops = max(0, node._manual_transient_ops - 1)
            node._recompute_on_manual_locked()

    _claim()
    assert node.on_manual is True
    _claim()                       # second concurrent op
    assert node.on_manual is True
    _release()                     # first op finishes — second still owns the arm
    assert node.on_manual is True
    _release()                     # both done — arm free
    assert node.on_manual is False


def test_jog_aborts_when_beenden_bumped_exit_gen():
    # FIX 4 — a „Beenden" that bumps the exit generation while the jog waits on
    # _manual_lock aborts the drive (the arbiter's exit generation moved past our
    # snapshot). No publish happens; the transient ref is released.
    node = _FakeNode(joints=(0.0, -1.5, 1.5, 0.0, 0.0, 0.0))
    real_lock = node._manual_lock

    class _BumpLock:
        """Simulates a concurrent „Beenden" landing while we block on the lock."""

        def acquire(self, *a, **k):
            node._manual_exit_gen += 1     # exit generation moved past our snapshot
            return real_lock.acquire(*a, **k)

        def release(self):
            return real_lock.release()

    node._manual_lock = _BumpLock()
    resp = _jog(node)
    assert resp.success is False and 'beendet' in resp.message
    assert node.published == []            # never drove
    assert True not in node.torque_calls   # never torqued ON
    assert node._manual_transient_ops == 0  # transient ref released
    assert node.on_manual is False


# ==========================================================================
# hand_guide
# ==========================================================================

def _hg(node, enabled):
    return _CB['workshop_hand_guide_callback'](node, _Req(enabled=enabled), _Resp())


def test_hand_guide_true_opens_session_torque_off():
    node = _FakeNode()
    resp = _hg(node, True)
    assert resp.success is True and 'Hand' in resp.message
    assert node.on_manual is True
    assert node.torque_calls == [False]   # arm made limp
    assert node._manual_lock.acquire(blocking=False)


def test_hand_guide_true_torque_off_failure_closes_session():
    node = _FakeNode(torque_ok=False)
    resp = _hg(node, True)
    assert resp.success is False and 'freigeschaltet' in resp.message
    # Arm stays torqued (not limp) → session closed, no dangle.
    assert node.on_manual is False


def test_hand_guide_true_busy_refused():
    node = _FakeNode(gate=(False, 'Inferenz läuft gerade — bitte zuerst stoppen.'))
    resp = _hg(node, True)
    assert resp.success is False and 'Inferenz' in resp.message


def test_hand_guide_false_relock_clears_session():
    node = _FakeNode(persistent=True)
    resp = _hg(node, False)
    assert resp.success is True and 'verriegelt' in resp.message
    assert node.on_manual is False
    assert node._manual_persistent is False
    assert node._manual_exit_gen == 1   # FIX 4 — „Beenden" bumped the generation
    assert 'retorque' in node.torque_calls
    assert not node._manual_stop_event.is_set()   # cleared after re-lock


def test_hand_guide_false_retorque_failure_keeps_session():
    node = _FakeNode(persistent=True, retorque_ok=False)
    resp = _hg(node, False)
    assert resp.success is False
    # Re-torque failed → keep the persistent session so no later mode drives a
    # limp arm; the exit generation was still bumped (a committed jog/replay aborts).
    assert node.on_manual is True
    assert node._manual_persistent is True
    assert node._manual_exit_gen == 1


def test_hand_guide_false_stops_active_recording():
    node = _FakeNode(persistent=True)
    node._manual_record_active = True
    node._manual_record_timer = object()
    _hg(node, False)
    assert node._manual_record_active is False
    assert node.destroyed_timers   # the record timer was torn down


# ==========================================================================
# record
# ==========================================================================

def _rec(node, action):
    return _CB['workshop_record_callback'](node, _Req(action=action), _Resp())


def test_record_start_torque_off_and_timer():
    node = _FakeNode()
    resp = _rec(node, 'start')
    assert resp.success is True
    assert node.on_manual is True
    assert node.torque_calls == [False]
    assert node._manual_record_active is True
    assert len(node.created_timers) == 1
    period, _cb = node.created_timers[0]
    assert period == pytest.approx(1.0 / 25)


def test_record_start_double_refused():
    node = _FakeNode()
    node._manual_record_active = True
    resp = _rec(node, 'start')
    assert resp.success is False and 'bereits' in resp.message


def test_record_stop_returns_contract_b_points_json():
    node = _FakeNode(persistent=True)   # a record session is open
    node._manual_record_active = True
    node._manual_record_timer = object()
    # Buffer layout: [t_s, j1..j5, grip].
    node._handguide_buffer = [
        [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.0],
        [0.4, 0.11, 0.2, 0.3, 0.4, 0.5, 0.0],
    ]
    resp = _rec(node, 'stop')
    assert resp.success is True
    assert resp.sample_count == 2
    assert resp.duration_s == pytest.approx(0.4)
    parsed = json.loads(resp.points_json)
    assert parsed['fps'] == 25
    # CONTRACT B: [j1..j5, grip, t_s] — joints first, time last.
    assert parsed['points'][0] == [0.1, 0.2, 0.3, 0.4, 0.5, 0.0, 0.0]
    assert parsed['points'][1][-1] == pytest.approx(0.4)
    assert 'retorque' in node.torque_calls
    assert node.destroyed_timers
    # Session stays open after stop (frontend exits via hand_guide(false)).
    assert node.on_manual is True


def test_record_stop_retorque_failure_keeps_session():
    node = _FakeNode(persistent=True, retorque_ok=False)
    node._manual_record_active = True
    node._handguide_buffer = [[0.0, 0, 0, 0, 0, 0, 0], [0.1, 0, 0, 0, 0, 0, 0]]
    resp = _rec(node, 'stop')
    assert resp.success is False
    assert node.on_manual is True   # limp-arm protection
    assert resp.sample_count == 2   # still reports what was captured


def test_record_cancel_discards_and_clears_session():
    node = _FakeNode(persistent=True)
    node._manual_record_active = True
    node._handguide_buffer = [[0.0, 0, 0, 0, 0, 0, 0]]
    resp = _rec(node, 'cancel')
    assert resp.success is True and 'verworfen' in resp.message
    assert node._handguide_buffer == []
    assert node.on_manual is False
    assert node._manual_persistent is False
    assert 'retorque' in node.torque_calls


def test_record_unknown_action_refused():
    resp = _rec(_FakeNode(), 'frobnicate')
    assert resp.success is False and 'Aufnahme-Aktion' in resp.message


# ==========================================================================
# record sampler
# ==========================================================================

class _FakeTime:
    def __init__(self, values):
        self._v = list(values)
        self._i = 0

    def monotonic(self):
        v = self._v[min(self._i, len(self._v) - 1)]
        self._i += 1
        return v


def _sampler_node(joints_seq):
    """A node whose communicator returns a different joint vector per call."""
    node = _FakeNode()
    node._manual_record_active = True
    node._manual_record_start_mono = 0.0
    seq = iter(joints_seq)
    last = [joints_seq[-1]]

    class _SeqComm:
        def get_latest_follower_joints(self):
            try:
                last[0] = next(seq)
            except StopIteration:
                pass
            return last[0]

    node.communicator = _SeqComm()
    return node


def test_sampler_drops_subthreshold_identical():
    # Two nearly-identical readings → only the first is kept.
    fake_time = _FakeTime([0.04, 0.08])
    fns = _load(['_manual_record_sample'], {**_G, 'time': fake_time})
    sample = fns['_manual_record_sample']
    node = _sampler_node([
        [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        [0.0, 0.0, 0.0, 0.0, 0.0, 0.0001],   # < 0.003 delta → dropped
    ])
    sample(node)
    sample(node)
    assert len(node._handguide_buffer) == 1


def test_sampler_keeps_moving_samples_and_caps_on_time():
    fake_time = _FakeTime([1.0, 200.0])   # 2nd tick past RECORD_MAX_S (120)
    fns = _load(['_manual_record_sample'], {**_G, 'time': fake_time})
    sample = fns['_manual_record_sample']
    node = _sampler_node([
        [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        [0.5, 0.0, 0.0, 0.0, 0.0, 0.0],
    ])
    sample(node)
    assert node._manual_record_active is True
    sample(node)
    # Second sample kept AND the wall-clock cap stopped the session.
    assert len(node._handguide_buffer) == 2
    assert node._manual_record_active is False
    # F4 — the clean-stop cap finish was scheduled (re-torque + on_manual clear
    # happen off the sampler, on the executor).
    assert node.cap_finish_scheduled >= 1


# ==========================================================================
# replay
# ==========================================================================

def _replay(node, **req):
    defaults = dict(name='', points_json='', speed=1.0)
    defaults.update(req)
    return _CB['workshop_replay_callback'](node, _Req(**defaults), _Resp())


_GOOD_TRAJ = json.dumps({'fps': 25, 'points': [
    [0.1, 0.1, 0.1, 0.1, 0.1, 0.0, 0.0],
    [0.2, 0.1, 0.1, 0.1, 0.1, 0.0, 0.4],
]})


def test_replay_inline_claims_session_and_spawns():
    # F3: the callback CLAIMS on_manual + spawns the daemon; the re-torque and the
    # stop-event clear now happen INSIDE _run_replay (stubbed here), NOT the
    # callback — so torque_calls stays empty at the callback level.
    node = _FakeNode(joints=(0.0, -1.5, 1.5, 0.0, 0.0, 0.0))
    resp = _replay(node, points_json=_GOOD_TRAJ, speed=1.0)
    assert resp.success is True and 'gestartet' in resp.message
    assert node.on_manual is True
    assert node.torque_calls == []   # re-torque moved into the daemon (F3)
    # Give the (stubbed) daemon a beat to run.
    for _ in range(50):
        if node.replay_runs:
            break
        time.sleep(0.01)
    assert node.replay_runs, 'replay daemon never ran'


def test_replay_stale_readback_refused():
    # F6 — a frozen follower readback refuses BEFORE claiming the session.
    node = _FakeNode(stale=True, joints=(0.0, -1.5, 1.5, 0.0, 0.0, 0.0))
    resp = _replay(node, points_json=_GOOD_TRAJ)
    assert resp.success is False and 'noch nicht bekannt' in resp.message
    assert node.on_manual is False
    assert node.replay_runs == []


def test_replay_nonfinite_readback_refused():
    node = _FakeNode(joints=(0.0, float('nan'), 1.5, 0.0, 0.0, 0.0))
    resp = _replay(node, points_json=_GOOD_TRAJ)
    assert resp.success is False and 'ungültig' in resp.message
    assert node.on_manual is False
    assert node.replay_runs == []


def test_replay_no_points_refused():
    node = _FakeNode(joints=(0.0, -1.5, 1.5, 0.0, 0.0, 0.0))
    resp = _replay(node)   # no inline json, no name
    assert resp.success is False and 'Keine Aufnahme' in resp.message


def test_replay_corrupt_json_refused():
    node = _FakeNode(joints=(0.0, -1.5, 1.5, 0.0, 0.0, 0.0))
    resp = _replay(node, points_json='{not json')
    assert resp.success is False and 'ungültig' in resp.message


def test_replay_double_refused():
    node = _FakeNode(joints=(0.0, -1.5, 1.5, 0.0, 0.0, 0.0))

    class _Alive:
        def is_alive(self):
            return True

    node._manual_replay_thread = _Alive()
    resp = _replay(node, points_json=_GOOD_TRAJ)
    assert resp.success is False and 'bereits eine Wiedergabe' in resp.message


def test_replay_busy_refused():
    node = _FakeNode(gate=(False, 'Aufnahme läuft gerade — bitte zuerst stoppen.'))
    resp = _replay(node, points_json=_GOOD_TRAJ)
    assert resp.success is False and 'Aufnahme' in resp.message


# ==========================================================================
# _run_replay daemon (F3 — re-torque + stop-event clear UNDER the lock)
# ==========================================================================

def _run_replay_inline(node, segmented, exit_gen=None):
    # Register the current thread so the F9 self-clear matches, then run inline.
    if exit_gen is None:
        exit_gen = node._manual_exit_gen
    node._manual_replay_thread = threading.current_thread()
    _CB['_run_replay'](node, segmented, exit_gen)


def test_run_replay_retorque_failure_keeps_session_no_drive_no_clear():
    # A transient ref was claimed by the callback; a re-torque failure must retain
    # it (arm may be limp) and NOT drive.
    node = _FakeNode(retorque_ok=False)
    node._manual_transient_ops = 1
    node._recompute_on_manual_locked()
    node._manual_stop_event.set()  # pretend a jog aborted — must NOT be cleared
    _run_replay_inline(node, [([0.0] * 6, 0.1)])
    # Re-torque failed → arm may be limp → keep the ref, do NOT drive.
    assert node.on_manual is True
    assert node._manual_transient_ops == 1   # ref retained (keep_session)
    assert node.published == []
    assert 'retorque' in node.torque_calls
    # F3 — the SHARED stop-event must NOT be cleared when the drive is refused.
    assert node._manual_stop_event.is_set()
    assert node._manual_lock.acquire(blocking=False)  # lock released


def test_run_replay_clears_stop_event_under_lock_and_drives():
    node = _FakeNode()
    node._manual_transient_ops = 1
    node._recompute_on_manual_locked()
    node._manual_stop_event.set()
    _run_replay_inline(node, [([0.0] * 6, 0.1), ([0.1] * 6, 0.2)])
    assert node.published                      # drove
    assert not node._manual_stop_event.is_set()  # cleared under the lock (F3)
    assert 'retorque' in node.torque_calls
    assert node._manual_transient_ops == 0     # transient ref released
    assert node.on_manual is False             # no persistent session → closed
    assert node._manual_replay_thread is None  # F9 self-clear
    assert node._manual_lock.acquire(blocking=False)


def test_run_replay_within_session_keeps_it_open():
    # A PERSISTENT session is open (record→replay in one session); the replay's
    # transient ref is released but the persistent flag keeps on_manual True.
    node = _FakeNode(persistent=True)
    node._manual_transient_ops = 1
    node._recompute_on_manual_locked()
    _run_replay_inline(node, [([0.0] * 6, 0.1), ([0.1] * 6, 0.2)])
    assert node._manual_transient_ops == 0   # transient ref released
    assert node.on_manual is True            # persistent session → stays open


def test_run_replay_aborts_on_exit_gen_mismatch_no_drive():
    # FIX 4 — a „Beenden" bumped the exit generation after the callback claimed the
    # replay ref; the daemon must abort WITHOUT re-torquing or driving.
    node = _FakeNode()
    node._manual_transient_ops = 1
    node._manual_exit_gen = 7                 # the CURRENT generation
    node._recompute_on_manual_locked()
    node._manual_stop_event.set()             # a jog's abort — must NOT be cleared
    _run_replay_inline(node, [([0.0] * 6, 0.1), ([0.1] * 6, 0.2)], exit_gen=6)  # stale
    assert node.published == []
    assert node._manual_stop_event.is_set()   # not cleared
    assert 'retorque' not in node.torque_calls  # never re-torqued
    assert node._manual_transient_ops == 0    # ref released (aborted, not kept)
    assert node.on_manual is False
    assert node._manual_lock.acquire(blocking=False)


def test_run_replay_aborts_when_record_started_no_drive():
    # FIX 2 — a record start seized the (limp) arm after the callback claimed the
    # replay ref; the daemon must abort WITHOUT driving the limp arm.
    node = _FakeNode(persistent=True)         # the record start opened a persistent
    node._manual_transient_ops = 1            # session; the replay claimed a ref
    node._recompute_on_manual_locked()
    node._manual_record_active = True
    _run_replay_inline(node, [([0.0] * 6, 0.1), ([0.1] * 6, 0.2)])
    assert node.published == []
    assert 'retorque' not in node.torque_calls
    assert node._manual_transient_ops == 0    # transient ref released
    assert node.on_manual is True             # persistent record session stays open


def test_run_replay_aborts_when_beenden_lands_during_retorque_no_drive():
    # Finding 1 — the pre-torque exit-gen check passes, but a „Beenden" lands
    # DURING the (blocking) re-torque: it bumps the exit generation and sets the
    # stop-event. The daemon must re-check the generation AFTER the re-torque and
    # abort WITHOUT clearing the stop-event or driving — otherwise it would wipe
    # the stop-event and drive the whole replay after the student pressed stop.
    node = _FakeNode()
    node._manual_transient_ops = 1
    node._manual_exit_gen = 5
    node._recompute_on_manual_locked()

    def _retorque_then_beenden(response):
        node.torque_calls.append('retorque')
        # A „Beenden" arrives mid-re-torque (hand_guide(false) bumps the gen +
        # sets the stop-event, both while the daemon holds _manual_lock).
        node._manual_exit_gen += 1
        node._manual_stop_event.set()
        return True

    node._retorque_follower_or_keep_locked = _retorque_then_beenden
    _run_replay_inline(node, [([0.0] * 6, 0.1), ([0.1] * 6, 0.2)], exit_gen=5)
    assert node.published == [], 'drove the replay after „Beenden"'
    assert 'retorque' in node.torque_calls          # re-torque did run
    assert node._manual_stop_event.is_set()         # abort did NOT clear it
    assert node._manual_transient_ops == 0          # ref released (aborted)
    assert node.on_manual is False


def test_hand_guide_false_does_not_zero_transient_refcount():
    # Finding 3 (regression guard) — „Beenden" must NOT zero _manual_transient_ops.
    # A jog/replay can claim a ref AFTER „Beenden"'s exit-gen bump (its snapshot then
    # equals the new generation, so its exit-gen guard passes); zeroing the refcount
    # here would drop on_manual to False while that op drives → two writers on the
    # arm. „Beenden" clears only the PERSISTENT session; a leaked transient ref
    # (from a jog torque-ON-fail retain, where on_manual==True is CORRECT because the
    # arm may be limp) is left for the 30 s idle watchdog, which re-checks freshness
    # under _mode_lock and cannot clobber a live claim.
    node = _FakeNode()
    node._manual_transient_ops = 1      # a leaked retained ref
    node._manual_persistent = False
    node._recompute_on_manual_locked()
    resp = _hg(node, False)
    assert resp.success is True and 'verriegelt' in resp.message
    assert node._manual_persistent is False
    assert node._manual_transient_ops == 1   # NOT zeroed (errs-safe, no two-writer)
    assert node.on_manual is True            # ref still owns it → watchdog clears it
    assert 'retorque' in node.torque_calls


def test_idle_watchdog_skips_reset_when_activity_arrives_during_retorque():
    # Finding 2 — a manual op that claims a ref (and stamps fresh activity, both
    # under _mode_lock) while the watchdog is mid-re-torque must NOT be clobbered:
    # the watchdog re-checks idle UNDER _mode_lock and skips the reset on fresh
    # activity, so on_manual can't drop to False while that op then drives.
    node = _FakeNode()
    node._manual_transient_ops = 1
    node._recompute_on_manual_locked()
    node._follower_torque_on = False                       # limp → re-torque runs
    node._manual_last_activity_mono = time.monotonic() - 100.0   # STALE (outer pass)

    def _torque_then_claim(enabled):
        node.torque_calls.append(enabled)
        # A concurrent jog claims during the re-torque and stamps fresh activity.
        node._manual_last_activity_mono = time.monotonic()
        return True
    node._set_follower_torque = _torque_then_claim
    _CB['_manual_idle_watchdog_cb'](node)
    assert True in node.torque_calls                       # re-torque was attempted
    assert node._manual_transient_ops == 1                 # ref NOT clobbered
    assert node.on_manual is True                          # stays owned


def test_idle_watchdog_resets_stale_stranded_session():
    # Finding 2 (converse) — a genuinely idle stranded session IS reset.
    node = _FakeNode()
    node._manual_transient_ops = 1
    node._recompute_on_manual_locked()
    node._follower_torque_on = False
    node._manual_last_activity_mono = time.monotonic() - 100.0   # STALE, stays stale
    _CB['_manual_idle_watchdog_cb'](node)
    assert True in node.torque_calls
    assert node._manual_transient_ops == 0
    assert node.on_manual is False


# ==========================================================================
# F4 — RECORD_MAX_S clean stop (re-torque + clear on_manual + notify)
# ==========================================================================

def test_cap_finish_retorques_clears_session_and_notifies():
    node = _FakeNode(persistent=True)
    node._manual_record_timer = object()
    node._manual_record_active = True
    _CB['_finish_manual_record_on_cap'](node)
    assert 'retorque' in node.torque_calls    # arm re-locked
    assert node.on_manual is False            # session closed
    assert node._manual_persistent is False
    assert node._manual_record_active is False
    assert node.destroyed_timers              # sampler timer torn down
    assert node._manual_record_cap_reached is True
    # German auto-stop notice reached the student via /task/status.
    assert node.communicator.status_published
    notice = node.communicator.status_published[-1]
    assert 'Maximale Aufnahmedauer erreicht' in notice.error
    assert node._manual_lock.acquire(blocking=False)  # lock released


def test_cap_finish_retorque_failure_keeps_session():
    node = _FakeNode(persistent=True, retorque_ok=False)
    node._manual_record_active = True
    _CB['_finish_manual_record_on_cap'](node)
    # Re-torque failed → keep the persistent session (arm may be limp) so a retry
    # re-locks.
    assert node.on_manual is True
    assert node._manual_persistent is True
    assert node._manual_lock.acquire(blocking=False)


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))

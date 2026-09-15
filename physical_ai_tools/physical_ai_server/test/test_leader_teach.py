"""D8 — leader-arm Vormachen on the server: the SEPARATE take claim.

A leader take is teleop: the follower mirrors the leader through the ordinary
broadcaster and the server only SAMPLES the follower. So it must claim neither
on_manual nor on_workflow (both gate the teleop collision e-stop OFF), never take
_manual_lock, never switch torque and never publish on the command rail. Its only
stops are DATA stops (collision, stale leader, stale follower) that DISCARD the
take, plus the RECORD_MAX_S cap that keeps it.

Deps-free pattern (physical_ai_server.py imports rclpy): the REAL methods are
extracted by ``ast`` and bound onto a fake node with real locks; the module
constants — the German abort sentences included — are extracted by name from the
same source, never restated here.
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

import pytest

from test_workshop_manual_callbacks import _FakeNode, _Req, _Resp

_SERVER_PY = (
    Path(__file__).resolve().parents[1]
    / 'physical_ai_server' / 'physical_ai_server.py'
)
_SOURCE = _SERVER_PY.read_text(encoding='utf-8')
_TREE = ast.parse(_SOURCE)

_CONST_NAMES = (
    'RECORD_MAX_S', '_MANUAL_RECORD_FPS', '_MANUAL_RECORD_MAX_SAMPLES',
    '_MANUAL_RECORD_MIN_DELTA_RAD', '_MANUAL_RECORD_JOINT_DECIMALS',
    '_MANUAL_RECORD_TIME_DECIMALS', '_MANUAL_LOCK_TIMEOUT_S',
    '_FOLLOWER_JOINT_MAX_AGE_S', '_MANUAL_TORQUE_FAIL_LIMIT',
    '_MANUAL_IDLE_RETORQUE_S', '_LEADER_TEACH_ABORT_MESSAGES_DE',
)

_METHOD_NAMES = (
    'workshop_record_callback', '_leader_teach_sample',
    '_schedule_leader_teach_reap', '_reap_leader_teach_timer',
    '_destroy_leader_teach_timers_locked', '_assert_no_other_active',
    'workshop_hand_guide_callback', '_last_torque_reason',
    '_follower_joints_stale', '_manual_idle_watchdog_cb',
    '_recompute_on_manual_locked', '_manual_record_sample', '_profile_n',
)


class _FakeTaskStatus:
    READY = 0

    def __init__(self):
        self.phase = None
        self.error = ''


def _module_constants(names):
    """Exec the module-level assignments of ``names`` in source order (a later
    one may derive from an earlier one, e.g. _MANUAL_RECORD_MAX_SAMPLES)."""
    ns: dict = {}
    for node in _TREE.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in names for t in node.targets):
            exec(compile(ast.Module(body=[node], type_ignores=[]),  # noqa: S102
                         str(_SERVER_PY), 'exec'), ns)
    missing = [n for n in names if n not in ns]
    assert not missing, f'module constants not found: {missing}'
    return {n: ns[n] for n in names}


def _function_defs(name):
    return [n for n in ast.walk(_TREE)
            if isinstance(n, ast.FunctionDef) and n.name == name]


def _load_methods(names, namespace):
    for name in names:
        defs = _function_defs(name)
        assert len(defs) == 1, f'{name}: expected exactly one definition'
        src = textwrap.dedent(ast.get_source_segment(_SOURCE, defs[0]))
        exec(compile(src, str(_SERVER_PY), 'exec'), namespace)  # noqa: S102
    return {n: namespace[n] for n in names}


_CONSTS = _module_constants(_CONST_NAMES)
_MSGS = _CONSTS['_LEADER_TEACH_ABORT_MESSAGES_DE']

# The numeric values the leader take is specified against (§5.2 reuses the hand
# recorder's constants; the staleness/lock/watchdog ones feed the extracted
# helpers). Pinned as literals so a shipped-value change is a deliberate edit.
_SHIPPED_VALUES = (
    ('RECORD_MAX_S', 120.0),
    ('_MANUAL_RECORD_FPS', 25),
    ('_MANUAL_RECORD_MAX_SAMPLES', 3100),
    ('_MANUAL_RECORD_MIN_DELTA_RAD', 0.003),
    ('_MANUAL_RECORD_JOINT_DECIMALS', 4),
    ('_MANUAL_RECORD_TIME_DECIMALS', 3),
    ('_MANUAL_LOCK_TIMEOUT_S', 2.0),
    ('_FOLLOWER_JOINT_MAX_AGE_S', 1.0),
    ('_MANUAL_IDLE_RETORQUE_S', 30.0),
)
_G = {'time': time, 'json': json, 'threading': threading, 'math': math,
      'TaskStatus': _FakeTaskStatus, **_CONSTS}
_CB = _load_methods(_METHOD_NAMES, _G)

_START_OK = 'Aufnahme gestartet — führe den Leader-Arm.'
_NO_LEADER_CAP = 'Dieser Roboter hat keinen Leader-Arm.'
_LEADER_DEAD = ('Der Leader-Arm sendet keine Daten — bitte den Leader-Arm '
                'verbinden und den Roboter auf der Startseite aktivieren.')
_DEGRADED = ('Roboter-Initialisierung fehlgeschlagen — bitte die '
             'Umgebung neu starten (Details im Protokoll).')
_NO_POSE = ('Aktuelle Position ist noch nicht bekannt — bitte kurz '
            'warten und erneut versuchen.')
_START_FAILED = 'Aufnahme konnte nicht gestartet werden.'
_BUSY_LEADER = 'Eine Leader-Aufnahme läuft gerade — bitte zuerst beenden.'
_NOTHING = 'Es läuft keine Leader-Aufnahme.'
_CAP_NOTICE = ('Maximale Aufnahmedauer erreicht — die Aufnahme wurde '
               'automatisch beendet.')


class _LeaderComm:
    """Follower stream double: the latest vector + its age (None = never)."""

    def __init__(self, joints, age):
        self.joints = joints
        self.age = age
        self.status_published = []

    def get_latest_follower_joints(self):
        return None if self.joints is None else list(self.joints)

    def get_follower_joints_age_s(self):
        return self.age

    def publish_status(self, status):
        self.status_published.append(status)


class _Timer:
    def __init__(self, period, cb):
        self.period = period
        self.cb = cb


class _LeaderNode(_FakeNode):
    """``_FakeNode`` (every manual-callback attribute + torque/rail spies) plus
    the plain owner flags the REAL gate reads and the §5.2 leader state."""

    def __init__(self, *, joints=(0.1, 0.2, 0.3, 0.4, 0.5, 0.8), age=0.01,
                 leader=True, has_leader=True):
        super().__init__(joints=None)
        self.communicator = _LeaderComm(joints, age)
        self.on_recording = False
        self.on_inference = False
        self.is_training = False
        self.on_calibration = False
        self.on_workflow = False
        self.on_manual = False
        self._collision_active = False
        self._arm_profile = types.SimpleNamespace(
            capabilities=types.SimpleNamespace(has_leader=has_leader),
            num_arm_joints=5)
        # §5.2 leader state.
        self.on_leader_teach = False
        self._leader_teach_claim_gen = 0
        self._leader_teach_lock = threading.Lock()
        self._leader_teach_active = False
        self._leader_teach_buffer = []
        self._leader_teach_timer = None
        self._leader_teach_reap_timer = None
        self._leader_teach_start_mono = 0.0
        self._leader_teach_abort_reason = ''
        self._leader_teach_cap_reached = False
        self.leader_live = leader
        self.leader_hooks = []          # consumed FIFO, one per leader check
        self.notices = []
        self.gate_modes = []
        self.timer_objs = []

    # --- REAL extracted methods ---
    workshop_record_callback = _CB['workshop_record_callback']
    workshop_hand_guide_callback = _CB['workshop_hand_guide_callback']
    _leader_teach_sample = _CB['_leader_teach_sample']
    _schedule_leader_teach_reap = _CB['_schedule_leader_teach_reap']
    _reap_leader_teach_timer = _CB['_reap_leader_teach_timer']
    _destroy_leader_teach_timers_locked = _CB['_destroy_leader_teach_timers_locked']
    _follower_joints_stale = _CB['_follower_joints_stale']
    _manual_idle_watchdog_cb = _CB['_manual_idle_watchdog_cb']
    _recompute_on_manual_locked = _CB['_recompute_on_manual_locked']
    _manual_record_sample = _CB['_manual_record_sample']
    _profile_n = _CB['_profile_n']

    def _assert_no_other_active(self, mode):
        self.gate_modes.append(mode)
        return _CB['_assert_no_other_active'](self, mode)

    # --- doubles ---
    def leader_appears_active(self):
        if self.leader_hooks:
            return self.leader_hooks.pop(0)()
        return self.leader_live

    def _publish_manual_notice(self, text):
        self.notices.append(text)

    def create_timer(self, period, cb):
        t = _Timer(period, cb)
        self.created_timers.append((period, cb))
        self.timer_objs.append(t)
        return t


def _rec(node, action):
    return node.workshop_record_callback(_Req(action=action), _Resp())


def _armed(**kw):
    node = _LeaderNode(**kw)
    resp = _rec(node, 'start_leader')
    assert resp.success is True, resp.message
    return node


def _untouched_manual_and_rail(node):
    assert node.on_manual is False
    assert node._manual_persistent is False
    assert node._manual_transient_ops == 0
    assert node.on_recording is False
    assert node.on_workflow is False
    assert node.on_inference is False
    assert node.torque_calls == []
    assert node.published == []


def _sample_rows(node, rows):
    """Feed the REAL sampler one tick per follower vector, time advancing 40 ms."""
    for i, joints in enumerate(rows):
        node.communicator.joints = joints
        node._leader_teach_start_mono = time.monotonic() - 0.04 * (i + 1)
        node._leader_teach_sample()


def _moving_rows(count, start=0.0):
    return [[start + 0.01 * i, 0.2, 0.3, 0.4, 0.5, 0.8] for i in range(count)]


@pytest.mark.parametrize('name,value', _SHIPPED_VALUES)
def test_the_reused_constants_have_their_shipped_values(name, value):
    assert _CONSTS[name] == value


def test_abort_messages_cover_exactly_the_three_data_stops():
    assert set(_MSGS) == {'collision', 'leader_lost', 'follower_lost'}


# ==========================================================================
# start_leader
# ==========================================================================

def test_T1_start_happy_path_claims_leader_teach_only():
    node = _LeaderNode()
    resp = _rec(node, 'start_leader')
    assert resp.success is True
    assert resp.message == _START_OK
    assert node.on_leader_teach is True
    assert node._leader_teach_active is True
    assert node._leader_teach_claim_gen == 1
    assert node.gate_modes == ['leader_teach']
    assert len(node.timer_objs) == 1
    assert node.timer_objs[0].period == pytest.approx(1.0 / 25)
    assert node._leader_teach_timer is node.timer_objs[0]
    _untouched_manual_and_rail(node)


def test_T2_no_leader_capability_refused_without_claim():
    node = _LeaderNode(has_leader=False)
    resp = _rec(node, 'start_leader')
    assert (resp.success, resp.message) == (False, _NO_LEADER_CAP)
    assert node.on_leader_teach is False
    assert node._leader_teach_claim_gen == 0
    assert node.timer_objs == []
    # A profile object without capabilities is treated the same (getattr-guarded).
    node._arm_profile = None
    assert _rec(node, 'start_leader').message == _NO_LEADER_CAP


def test_T3_leader_not_live_refused_and_claim_released():
    node = _LeaderNode(leader=False)
    resp = _rec(node, 'start_leader')
    assert (resp.success, resp.message) == (False, _LEADER_DEAD)
    assert node.on_leader_teach is False
    assert node.timer_objs == []
    # A RAISING leader check is no leader signal either.
    node.leader_hooks.append(lambda: (_ for _ in ()).throw(RuntimeError('x')))
    resp = _rec(node, 'start_leader')
    assert (resp.success, resp.message) == (False, _LEADER_DEAD)
    assert node.on_leader_teach is False


def test_T4_degraded_boot_and_stale_follower_refused_and_claim_released():
    node = _LeaderNode()
    node.communicator = None
    resp = _rec(node, 'start_leader')
    assert (resp.success, resp.message) == (False, _DEGRADED)
    assert node.on_leader_teach is False

    node = _LeaderNode(age=5.0)  # frozen follower stream (> 1 s)
    resp = _rec(node, 'start_leader')
    assert (resp.success, resp.message) == (False, _NO_POSE)
    assert node.on_leader_teach is False
    assert node.timer_objs == []


_OWNER_SENTENCES = {
    '_collision_active': ('Eine Kollision wird gerade behoben — bitte zuerst die '
                          'Schritte im Hinweisfenster abschließen.'),
    'on_recording': 'Aufnahme läuft gerade — bitte zuerst stoppen.',
    'on_inference': 'Inferenz läuft gerade — bitte zuerst stoppen.',
    'is_training': 'Training läuft gerade — bitte abwarten oder abbrechen.',
    'on_calibration': 'Kalibrierung läuft gerade — bitte zuerst beenden.',
    'on_workflow': 'Ein Workflow läuft gerade — bitte zuerst stoppen.',
    'on_manual': 'Handbetrieb ist aktiv — bitte zuerst den Handbetrieb beenden.',
}


@pytest.mark.parametrize('flag', sorted(_OWNER_SENTENCES))
def test_T5_every_owner_refuses_a_start_with_its_sentence(flag):
    node = _LeaderNode()
    setattr(node, flag, True)
    resp = _rec(node, 'start_leader')
    assert (resp.success, resp.message) == (False, _OWNER_SENTENCES[flag])
    assert node.on_leader_teach is False
    assert node._leader_teach_claim_gen == 0
    assert node.timer_objs == []


def test_T6_second_start_while_active_refused():
    node = _armed()
    resp = _rec(node, 'start_leader')
    assert (resp.success, resp.message) == (False, _BUSY_LEADER)
    assert node.on_leader_teach is True        # the first take keeps its claim
    assert node._leader_teach_active is True
    assert len(node.timer_objs) == 1


def test_T7_sampler_rounds_dedups_and_never_stamps_manual_activity():
    node = _armed()
    node._manual_last_activity_mono = 0.0
    _sample_rows(node, [
        [0.123456, 0.2, 0.3, 0.4, 0.5, 0.8],
        [0.1245, 0.2, 0.3, 0.4, 0.5, 0.8],      # < 0.003 rad on every joint → dropped
        [0.130049, 0.2, 0.3, 0.4, 0.5, 0.8, 9.9],  # wider vector → first n+1 kept
    ])
    buf = node._leader_teach_buffer
    assert len(buf) == 2
    assert buf[0][1] == 0.1235 and buf[1][1] == 0.13
    assert all(len(row) == 7 for row in buf)                     # [t, j1..j5, grip]
    assert all(row[0] == round(row[0], 3) for row in buf)
    assert node._manual_last_activity_mono == 0.0
    assert node.notices == []
    _untouched_manual_and_rail(node)


# ==========================================================================
# data stops (discard) and the cap (keep)
# ==========================================================================

def test_T8_collision_discards_silently_then_stop_answers_the_collision_sentence():
    node = _armed()
    _sample_rows(node, _moving_rows(5))
    assert len(node._leader_teach_buffer) == 5
    node._collision_active = True
    node._leader_teach_sample()
    assert node._leader_teach_active is False
    assert node._leader_teach_buffer == []
    assert node.on_leader_teach is False
    assert node._leader_teach_abort_reason == 'collision'
    assert node.notices == []                     # the CollisionModal owns the screen
    assert node._leader_teach_reap_timer is not None
    assert node.timer_objs[-1].cb == node._reap_leader_teach_timer
    resp = _rec(node, 'stop_leader')
    assert resp.success is False
    assert resp.message == _MSGS['collision']
    assert resp.message == 'Die Aufnahme wurde wegen einer Kollision verworfen.'
    assert resp.points_json == ''
    _untouched_manual_and_rail(node)


def test_a_tick_whose_take_was_replaced_before_the_lock_is_ignored():
    # The sampler reads its abort verdict BEFORE taking the lock; a stop_leader +
    # a new start_leader in between must not hand that stale verdict to the new take.
    node = _armed()
    _sample_rows(node, _moving_rows(3))
    real = node._leader_teach_lock

    class _NewTakeBeforeLock:
        def acquire(self, *a, **k):
            node._leader_teach_start_mono += 5.0   # a new take armed meanwhile
            return real.acquire(*a, **k)

        def release(self):
            real.release()

        def __enter__(self):
            real.acquire()
            return self

        def __exit__(self, *exc):
            real.release()
            return False

    node._leader_teach_lock = _NewTakeBeforeLock()
    node.leader_live = False                          # the OLD tick's verdict
    node._leader_teach_sample()
    node._leader_teach_lock = real
    assert node._leader_teach_active is True
    assert node.notices == []
    assert node._leader_teach_abort_reason == ''
    assert len(node._leader_teach_buffer) == 3


def test_T9_leader_lost_discards_with_notice():
    node = _armed()
    _sample_rows(node, _moving_rows(3))
    node.leader_live = False
    node._leader_teach_sample()
    assert node._leader_teach_active is False
    assert node._leader_teach_buffer == []
    assert node.on_leader_teach is False
    assert node.notices == [
        'Der Leader-Arm sendet keine Daten mehr — die Aufnahme wurde verworfen.']
    assert node.notices == [_MSGS['leader_lost']]
    resp = _rec(node, 'stop_leader')
    assert (resp.success, resp.message, resp.points_json) == (
        False, _MSGS['leader_lost'], '')


def test_T10_follower_stale_discards_with_notice():
    node = _armed()
    _sample_rows(node, _moving_rows(3))
    node.communicator.age = 1.5
    node._leader_teach_sample()
    assert node._leader_teach_active is False
    assert node.on_leader_teach is False
    assert node.notices == [
        'Die Armstellung kommt nicht mehr an — die Aufnahme wurde verworfen.']
    assert node.notices == [_MSGS['follower_lost']]
    assert _rec(node, 'stop_leader').message == _MSGS['follower_lost']


def test_T11_cap_by_time_keeps_the_take_and_releases_the_claim():
    node = _armed()
    _sample_rows(node, _moving_rows(4))
    node.communicator.joints = [0.9, 0.2, 0.3, 0.4, 0.5, 0.8]
    node._leader_teach_start_mono = time.monotonic() - 121.0
    node._leader_teach_sample()
    assert node._leader_teach_active is False
    assert node._leader_teach_cap_reached is True
    assert node.on_leader_teach is False
    assert len(node._leader_teach_buffer) == 5      # kept, last tick included
    assert node.notices == [_CAP_NOTICE]
    assert node._leader_teach_reap_timer is not None
    resp = _rec(node, 'stop_leader')
    assert resp.success is True
    assert resp.message == 'Aufnahme beendet — 5 Punkte. ' + _CAP_NOTICE
    assert len(json.loads(resp.points_json)['points']) == 5


def test_T11_cap_by_sample_count_keeps_the_take():
    node = _armed()
    cap = _CONSTS['_MANUAL_RECORD_MAX_SAMPLES']
    node._leader_teach_buffer = [[0.001 * i, 0.01 * i, 0.2, 0.3, 0.4, 0.5, 0.8]
                                 for i in range(cap - 1)]
    node.communicator.joints = [99.0, 0.2, 0.3, 0.4, 0.5, 0.8]
    node._leader_teach_start_mono = time.monotonic() - 1.0
    node._leader_teach_sample()
    assert len(node._leader_teach_buffer) == cap
    assert node._leader_teach_active is False
    assert node._leader_teach_cap_reached is True
    assert node.on_leader_teach is False
    assert node.notices == [_CAP_NOTICE]
    resp = _rec(node, 'stop_leader')
    assert resp.success is True and resp.message.endswith(_CAP_NOTICE)
    assert resp.sample_count == cap


def test_T11b_cap_is_judged_on_a_tick_without_a_joint_vector():
    node = _armed()
    _sample_rows(node, _moving_rows(2))
    node.communicator.joints = None
    node._leader_teach_start_mono = time.monotonic() - 121.0
    node._leader_teach_sample()
    assert node.on_leader_teach is False
    assert node._leader_teach_active is False
    assert node.notices == [_CAP_NOTICE]
    resp = _rec(node, 'stop_leader')
    assert resp.success is True
    assert resp.message == 'Aufnahme beendet — 2 Punkte. ' + _CAP_NOTICE


def test_T12_stop_returns_contract_b_and_destroys_both_timers():
    node = _armed()
    rows = _moving_rows(6)
    _sample_rows(node, rows)
    sampler_timer = node._leader_teach_timer
    reap = node._leader_teach_reap_timer = _Timer(0.1, None)
    resp = _rec(node, 'stop_leader')
    assert resp.success is True
    assert resp.message == 'Aufnahme beendet — 6 Punkte.'
    payload = json.loads(resp.points_json)
    assert payload['fps'] == 25
    points = payload['points']
    assert len(points) == 6 and all(len(p) == 5 + 2 for p in points)
    assert points[0][:6] == [0.0, 0.2, 0.3, 0.4, 0.5, 0.8]    # joints first
    assert [p[-1] for p in points] == sorted(p[-1] for p in points)  # time LAST
    assert resp.sample_count == 6
    assert resp.duration_s == points[-1][-1]
    assert node.on_leader_teach is False
    assert node._leader_teach_active is False
    assert node._leader_teach_buffer == []
    assert sampler_timer in node.destroyed_timers and reap in node.destroyed_timers
    assert node._leader_teach_timer is None and node._leader_teach_reap_timer is None
    _untouched_manual_and_rail(node)


def test_T13_cancel_discards_and_is_idempotent():
    node = _armed()
    _sample_rows(node, _moving_rows(3))
    resp = _rec(node, 'cancel_leader')
    assert (resp.success, resp.message, resp.points_json) == (
        True, 'Aufnahme verworfen.', '')
    assert node._leader_teach_buffer == []
    assert node.on_leader_teach is False and node._leader_teach_active is False
    resp = _rec(node, 'cancel_leader')
    assert (resp.success, resp.message) == (True, 'Aufnahme verworfen.')


def test_T14_stop_with_nothing_running():
    node = _LeaderNode()
    resp = _rec(node, 'stop_leader')
    assert (resp.success, resp.message, resp.points_json) == (False, _NOTHING, '')
    assert node.on_leader_teach is False


# ==========================================================================
# interaction with the manual arbiter
# ==========================================================================

def test_T15_manual_record_start_and_hand_guide_refused_during_a_take():
    node = _armed()
    resp = _rec(node, 'start')
    assert (resp.success, resp.message) == (False, _BUSY_LEADER)
    resp = node.workshop_hand_guide_callback(_Req(enabled=True), _Resp())
    assert (resp.success, resp.message) == (False, _BUSY_LEADER)
    assert node.on_leader_teach is True and node._leader_teach_active is True
    _untouched_manual_and_rail(node)


def test_T16_capture_coexists_with_a_take_and_with_handbetrieb():
    node = _armed()
    assert node._assert_no_other_active('capture') == (True, '')
    node.on_manual = True
    assert node._assert_no_other_active('capture') == (True, '')
    node.on_leader_teach = False
    assert node._assert_no_other_active('capture') == (True, '')


def test_T17_hand_guide_false_during_a_take_leaves_leader_state_alone():
    node = _armed()
    _sample_rows(node, _moving_rows(4))
    before = [list(r) for r in node._leader_teach_buffer]
    node.workshop_hand_guide_callback(_Req(enabled=False), _Resp())
    assert node.on_leader_teach is True
    assert node._leader_teach_active is True
    assert node._leader_teach_buffer == before
    assert node._leader_teach_timer is not None


def test_T20_claim_released_by_another_tab_before_arming():
    node = _LeaderNode()
    other_tab = []

    def stop_from_other_tab():
        other_tab.append(_rec(node, 'stop_leader'))
        return True

    node.leader_hooks.append(stop_from_other_tab)
    resp = _rec(node, 'start_leader')
    assert (resp.success, resp.message) == (False, _START_FAILED)
    assert other_tab[0].message == _NOTHING
    assert node.timer_objs == []
    assert node._leader_teach_active is False
    assert node.on_leader_teach is False


def test_T21_stop_after_a_trip_before_the_next_tick_discards():
    node = _armed()
    _sample_rows(node, _moving_rows(10))
    assert len(node._leader_teach_buffer) == 10
    node._collision_active = True                 # tripped, no sampler tick yet
    resp = _rec(node, 'stop_leader')
    assert resp.success is False
    assert resp.message == 'Die Aufnahme wurde wegen einer Kollision verworfen.'
    assert resp.points_json == ''
    assert node.on_leader_teach is False
    assert node._leader_teach_buffer == []
    assert node._collision_active is True         # READ only, never written


def test_T21b_a_capped_take_survives_a_later_trip():
    node = _armed()
    _sample_rows(node, _moving_rows(3))
    node._leader_teach_start_mono = time.monotonic() - 121.0
    node._leader_teach_sample()
    assert node.on_leader_teach is False and node._leader_teach_cap_reached is True
    node._collision_active = True
    resp = _rec(node, 'stop_leader')
    assert resp.success is True
    assert resp.message.startswith('Aufnahme beendet — ')
    assert resp.message.endswith(_CAP_NOTICE)
    assert json.loads(resp.points_json)['points']


def _three_tabs(a_check_result):
    """Tab A claims and blocks inside its leader check; tab B stops (releasing
    A's claim); tab C starts and arms; then A's check returns."""
    node = _LeaderNode()
    entered = threading.Event()
    proceed = threading.Event()

    def blocking_check():
        entered.set()
        assert proceed.wait(5.0)
        return a_check_result

    node.leader_hooks.append(blocking_check)
    out = {}
    tab_a = threading.Thread(
        target=lambda: out.__setitem__('A', _rec(node, 'start_leader')))
    tab_a.start()
    assert entered.wait(5.0)
    out['B'] = _rec(node, 'stop_leader')
    out['C'] = _rec(node, 'start_leader')
    proceed.set()
    tab_a.join(5.0)
    assert not tab_a.is_alive()
    return node, out


def test_T22_three_tabs_a_stale_finally_leaves_the_newer_claim():
    node, out = _three_tabs(False)
    assert out['B'].message == _NOTHING
    assert (out['C'].success, out['C'].message) == (True, _START_OK)
    assert (out['A'].success, out['A'].message) == (False, _LEADER_DEAD)
    assert node._leader_teach_claim_gen == 2
    assert node.on_leader_teach is True
    assert node._leader_teach_active is True
    ok, msg = node._assert_no_other_active('workflow')
    assert (ok, msg) == (False, _BUSY_LEADER)


def test_T22b_three_tabs_a_never_arms_without_its_own_claim():
    node, out = _three_tabs(True)
    assert (out['C'].success, out['C'].message) == (True, _START_OK)
    assert (out['A'].success, out['A'].message) == (False, _START_FAILED)
    assert len(node.timer_objs) == 1                  # C's sampler only
    assert node._leader_teach_timer is node.timer_objs[0]
    assert node.on_leader_teach is True and node._leader_teach_active is True


def test_T22c_a_stale_finally_leaves_a_newer_claim_that_is_not_armed_yet():
    # The generation half of the finally guard: C has CLAIMED but not armed when
    # A gives up. Without the generation check A's finally (which only sees
    # "no take armed") would clear C's claim, opening the slot to a workflow.
    node = _LeaderNode()
    a_in, a_go = threading.Event(), threading.Event()
    c_in, c_go = threading.Event(), threading.Event()

    def check(entered, proceed, result):
        def _check():
            entered.set()
            assert proceed.wait(5.0)
            return result
        return _check

    node.leader_hooks += [check(a_in, a_go, False), check(c_in, c_go, True)]
    out = {}
    tab_a = threading.Thread(
        target=lambda: out.__setitem__('A', _rec(node, 'start_leader')))
    tab_a.start()
    assert a_in.wait(5.0)
    out['B'] = _rec(node, 'stop_leader')
    tab_c = threading.Thread(
        target=lambda: out.__setitem__('C', _rec(node, 'start_leader')))
    tab_c.start()
    assert c_in.wait(5.0)
    a_go.set()
    tab_a.join(5.0)
    assert (out['A'].success, out['A'].message) == (False, _LEADER_DEAD)
    assert node.on_leader_teach is True                     # C's claim survives
    assert node._assert_no_other_active('workflow') == (False, _BUSY_LEADER)
    c_go.set()
    tab_c.join(5.0)
    assert (out['C'].success, out['C'].message) == (True, _START_OK)
    assert node._leader_teach_active is True and node.on_leader_teach is True


@pytest.mark.parametrize('joints,age', [(None, None), ([0.1, 0.2], 0.01), ([], 0.01)])
def test_T23_start_without_a_follower_vector_refused(joints, age):
    node = _LeaderNode(joints=joints, age=age)
    # Production's staleness helper reads a never-published follower as NOT stale.
    assert node._follower_joints_stale() is False
    resp = _rec(node, 'start_leader')
    assert (resp.success, resp.message) == (False, _NO_POSE)
    assert node.on_leader_teach is False
    assert node._leader_teach_active is False
    assert node.timer_objs == []


def test_T24_idle_watchdog_ignores_an_armed_take():
    node = _armed()
    node._manual_last_activity_mono = time.monotonic() - 999.0
    node._manual_idle_watchdog_cb()
    assert node.torque_calls == []
    assert node.on_leader_teach is True and node._leader_teach_active is True
    # Control: the same extracted watchdog DOES act on an idle manual session, so
    # the silence above is the take's flags, not an inert double.
    manual = _LeaderNode()
    manual._manual_persistent = True
    manual.on_manual = True
    manual._manual_last_activity_mono = time.monotonic() - 999.0
    manual._manual_idle_watchdog_cb()
    assert manual.torque_calls == [True]
    assert manual.on_manual is False


# ==========================================================================
# reaper
# ==========================================================================

def test_reaper_destroys_the_stopped_sampler_timer_and_itself():
    node = _armed()
    sampler_timer = node._leader_teach_timer
    node.leader_live = False
    node._leader_teach_sample()
    reap = node._leader_teach_reap_timer
    assert reap is not None and reap.period == pytest.approx(0.1)
    node._schedule_leader_teach_reap()                   # idempotent
    assert node._leader_teach_reap_timer is reap and len(node.timer_objs) == 2
    node._reap_leader_teach_timer()
    assert node.destroyed_timers == [reap, sampler_timer]
    assert node._leader_teach_timer is None and node._leader_teach_reap_timer is None


def test_reaper_leaves_a_newly_armed_take_alone():
    node = _armed()
    node.leader_live = False
    node._leader_teach_sample()
    node.leader_live = True
    reap = node._leader_teach_reap_timer
    assert _rec(node, 'start_leader').success is True   # destroys the pending reap
    assert reap in node.destroyed_timers
    new_sampler = node._leader_teach_timer
    node._reap_leader_teach_timer()                      # a late fire is harmless
    assert node._leader_teach_timer is new_sampler
    assert new_sampler not in node.destroyed_timers


# ==========================================================================
# AST fences (T18 forbidden names, T19 lock order)
# ==========================================================================

_HELPERS = ('_leader_teach_sample', '_schedule_leader_teach_reap',
            '_reap_leader_teach_timer', '_destroy_leader_teach_timers_locked')
_FORBIDDEN_REFS = frozenset({
    '_manual_lock', '_set_follower_torque', '_retorque_follower_or_keep_locked',
    '_trajectory_publisher', '_publish_collision_flag',
    '_hold_follower_at_measured_pose', '_dxl_torque_lock', 'motion_lock',
    '_sim_pub_lock',
})
_FORBIDDEN_ASSIGNS = frozenset({
    'on_manual', '_manual_persistent', '_manual_transient_ops', 'on_recording',
    'on_workflow', 'on_inference', '_collision_active',
})


def _record_branch(fn, action):
    for node in ast.walk(fn):
        if not (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)):
            continue
        test = node.test
        if not (isinstance(test.left, ast.Name) and test.left.id == 'action'
                and len(test.comparators) == 1):
            continue
        comp = test.comparators[0]
        values = ({comp.value} if isinstance(comp, ast.Constant)
                  else {e.value for e in getattr(comp, 'elts', [])
                        if isinstance(e, ast.Constant)})
        if values == set(action):
            return node
    raise AssertionError(f'record branch {action} not found')


def _leader_subtrees():
    record = _function_defs('workshop_record_callback')[0]
    return ([_record_branch(record, ('start_leader',)),
             _record_branch(record, ('stop_leader', 'cancel_leader'))]
            + [_function_defs(name)[0] for name in _HELPERS])


def _names_referenced(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)
    return names


def _names_assigned(tree):
    names = set()
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == 'setattr' and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)):
            names.add(node.args[1].value)
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Attribute):
                    names.add(sub.attr)
    return names


def test_T18_leader_paths_reference_no_torque_rail_or_manual_lock():
    for tree in _leader_subtrees():
        hit = _names_referenced(tree) & _FORBIDDEN_REFS
        assert not hit, f'line {tree.lineno}: leader path references {sorted(hit)}'
        wrote = _names_assigned(tree) & _FORBIDDEN_ASSIGNS
        assert not wrote, f'line {tree.lineno}: leader path assigns {sorted(wrote)}'


def test_T18_fence_has_teeth_on_the_manual_record_branch():
    # The hand recorder's start branch takes _manual_lock, switches torque and
    # claims _manual_persistent — the same walkers must see all three.
    manual = _record_branch(_function_defs('workshop_record_callback')[0], ('start',))
    assert {'_manual_lock', '_set_follower_torque'} <= _names_referenced(manual)
    assert '_manual_persistent' in _names_assigned(manual)
    start = _leader_subtrees()[0]
    assert 'on_leader_teach' in _names_assigned(start)


def _with_locks(node):
    return {item.context_expr.attr for item in node.items
            if isinstance(item.context_expr, ast.Attribute)}


def test_T19_no_leader_teach_lock_is_taken_under_mode_lock():
    # Whole module, a superset of the leader subtrees: _leader_teach_lock ->
    # _mode_lock is the only order.
    for node in ast.walk(_TREE):
        if isinstance(node, ast.With) and '_mode_lock' in _with_locks(node):
            for stmt in node.body:
                assert '_leader_teach_lock' not in _names_referenced(stmt), (
                    f'line {node.lineno}: _leader_teach_lock under _mode_lock')


def test_T19_leader_paths_nest_mode_lock_inside_leader_teach_lock():
    nested = 0
    for tree in _leader_subtrees():
        for node in ast.walk(tree):
            if isinstance(node, ast.With) and '_leader_teach_lock' in _with_locks(node):
                nested += sum(1 for sub in ast.walk(node)
                              if isinstance(sub, ast.With)
                              and '_mode_lock' in _with_locks(sub))
    assert nested >= 2   # start's re-check + stop/cancel's release

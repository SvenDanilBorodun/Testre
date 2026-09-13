"""Re-lock IN PLACE + the warned, slow glide to the Grundstellung (2026-09-13).

The reported defect: after „Tisch vermessen" and after „Arm festsetzen" (the
end of „Arm freischalten") the arm JUMPED to its home pose. Root cause on the
OMX: the JointTrajectoryController keeps the reference it held before the
torque-off (usually HOME), and the follower servos run a 50 ms time-based
profile (Drive Mode 4, omx_f.ros2_control.xacro), so Torque Enable snapped the
hand-guided arm back to that stale reference in ~50 ms. The fix has two halves,
pinned here:

1. ``_set_follower_torque(True)`` publishes a single-point trajectory at the
   MEASURED pose and lets the controller adopt it BEFORE energising — on the
   Dynamixel/ros2_control rail only (the Feetech driver seeds Goal = Present
   itself and drops trajectories while limp).
2. The way back to HOME is a separate /workshop/jog mode 'home': the same
   floor-aware planner as the `home` block, every leg stretched to a gentle
   quintic peak, a stop that HOLDS, and an arrival check. The React side only
   ever calls it after a visible countdown warning.

Plus the recorder rounding that keeps a 120 s take under the cloud's 256 KiB cap.

Methods are extracted by ``ast`` from the shipped module and exec'd onto fakes,
like test_workshop_manual_callbacks.py, so the code under test is the literal
production source.
"""

from __future__ import annotations

import ast
import json
import math
import sys
import textwrap
import threading
import time
import types
from pathlib import Path

import numpy as np
import pytest

_SERVER_PY = (
    Path(__file__).resolve().parents[1]
    / 'physical_ai_server' / 'physical_ai_server.py'
)


def _module_consts(names):
    """Read module-level constants straight out of the shipped source, so these
    tests can never drift from the real numbers."""
    tree = ast.parse(_SERVER_PY.read_text(encoding='utf-8'))
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in names:
                # Some are DERIVED from earlier ones (the sample cap), so each is
                # evaluated against the constants already read, in file order.
                expr = ast.Expression(body=node.value)
                out[target.id] = eval(  # noqa: S307 — our own module constants
                    compile(expr, str(_SERVER_PY), 'eval'), {'int': int}, dict(out))
    missing = set(names) - set(out)
    assert not missing, f'constants not found in physical_ai_server.py: {missing}'
    return out


_CONSTS = _module_consts([
    '_TORQUE_ON_HOLD_TIME_FROM_START_S', '_TORQUE_ON_HOLD_SETTLE_S',
    '_TORQUE_ON_HOLD_CYCLE_MARGIN_S', '_MANUAL_HOME_ALREADY_THERE_RAD',
    '_COMMAND_RAIL_MATCH_WAIT_S',
    '_MANUAL_HOME_PEAK_RAD_S', '_MANUAL_HOME_MIN_DURATION_S',
    '_MANUAL_RECORD_JOINT_DECIMALS', '_MANUAL_RECORD_TIME_DECIMALS',
    'RECORD_MAX_S', '_MANUAL_RECORD_FPS', '_MANUAL_RECORD_MAX_SAMPLES',
    '_MANUAL_RECORD_MIN_DELTA_RAD',
])


def _load(names, extra_globals):
    source = _SERVER_PY.read_text(encoding='utf-8')
    tree = ast.parse(source)
    ns: dict = dict(extra_globals)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            src = textwrap.dedent(ast.get_source_segment(source, node))
            exec(compile(src, str(_SERVER_PY), 'exec'), ns)  # noqa: S102
            found.add(node.name)
    assert found == set(names), f'missing methods: {set(names) - found}'
    return {n: ns[n] for n in names}


class _Logger:
    def __init__(self):
        self.lines = []

    def error(self, msg, *a, **k):
        self.lines.append(('error', str(msg)))

    def warning(self, msg, *a, **k):
        self.lines.append(('warning', str(msg)))

    def info(self, msg, *a, **k):
        self.lines.append(('info', str(msg)))


class _Comm:
    def __init__(self, joints):
        self.joints = joints

    def get_latest_follower_joints(self):
        return self.joints


OMX_SERVICE = '/dynamixel_hardware_interface/set_dxl_torque'


# ─────────────────────────────────────────────────────────────────────────────
# 1. _follower_rail_is_ros2_control — which arms carry a stale JTC reference
# ─────────────────────────────────────────────────────────────────────────────

_G = {'time': time, 'json': json, 'threading': threading, 'math': math, **_CONSTS}
_RAIL = _load(['_follower_rail_is_ros2_control'], _G)['_follower_rail_is_ros2_control']


@pytest.mark.parametrize('profile_id, expected', [
    ('omx_full', True),
    ('omx_follower', True),
    ('edu6_studio', False),
    ('edu1_studio', False),
])
def test_only_the_dynamixel_rail_is_treated_as_ros2_control(profile_id, expected):
    from physical_ai_server import robot_profiles
    node = types.SimpleNamespace(_arm_profile=robot_profiles.ROBOT_PROFILES[profile_id])
    assert _RAIL(node) is expected


def test_a_profile_less_node_is_the_omx_default():
    assert _RAIL(types.SimpleNamespace()) is True


# ─────────────────────────────────────────────────────────────────────────────
# 2. _hold_follower_at_measured_pose — never guesses a pose
# ─────────────────────────────────────────────────────────────────────────────

_HOLD_FNS = _load(['_hold_follower_at_measured_pose', '_wait_for_command_rail_subscriber'],
                  _G)
_HOLD = _HOLD_FNS['_hold_follower_at_measured_pose']


def _hold_node(joints, stale=False, n=5):
    node = types.SimpleNamespace()
    node.communicator = _Comm(joints) if joints is not ... else None
    node.published = []
    node._trajectory_publisher = lambda pts: node.published.append(list(pts))
    node._follower_joints_stale = lambda: stale
    node._profile_n = lambda: n
    node.log = _Logger()
    node.get_logger = lambda: node.log
    node._wait_for_command_rail_subscriber = (
        lambda: _HOLD_FNS['_wait_for_command_rail_subscriber'](node))
    return node


def test_hold_publishes_the_measured_pose_with_zero_velocity():
    q = [0.1, -0.4, 0.9, 0.2, -0.3, 0.5]
    node = _hold_node(q)
    assert _HOLD(node) is True
    assert len(node.published) == 1
    (point,) = node.published[0]
    assert point[0] == q
    assert point[1] == _CONSTS['_TORQUE_ON_HOLD_TIME_FROM_START_S']
    assert point[2] == [0.0] * 6


@pytest.mark.parametrize('joints, stale', [
    (None, False),                                   # never arrived
    ([0.1, 0.2, 0.3], False),                        # short
    ([0.1, 0.2, math.nan, 0.0, 0.0, 0.0], False),    # non-finite
    ([0.1, 0.2, 0.3, 0.0, 0.0, 0.0], True),          # frozen stream
])
def test_hold_is_skipped_rather_than_guessed(joints, stale):
    node = _hold_node(joints, stale=stale)
    assert _HOLD(node) is False
    assert node.published == []


def test_hold_is_skipped_without_a_communicator():
    node = _hold_node(..., stale=False)
    assert _HOLD(node) is False
    assert node.published == []


def test_hold_uses_the_profile_width_on_edu6():
    q = [0.0, 0.7, -2.4, 0.0, 0.7, 0.0, 1.75]
    node = _hold_node(q + [99.0], n=6)               # extra trailing value ignored
    assert _HOLD(node) is True
    (point,) = node.published[0]
    assert point[0] == q and point[2] == [0.0] * 7


# ─────────────────────────────────────────────────────────────────────────────
# 3. _set_follower_torque — the hold lands BEFORE Torque Enable, OMX only
# ─────────────────────────────────────────────────────────────────────────────

class _Future:
    def __init__(self, result):
        self._result = result

    def done(self):
        return True

    def result(self):
        return self._result


class _Client:
    def __init__(self, events, success=True):
        self.events = events
        self.success = success

    def wait_for_service(self, timeout_sec=None):
        return True

    def call_async(self, req):
        self.events.append(('torque', bool(req.data)))
        return _Future(types.SimpleNamespace(success=self.success, message=''))


@pytest.fixture
def fake_std_srvs(monkeypatch):
    class SetBool:
        class Request:
            data = False
    srv = types.ModuleType('std_srvs.srv')
    srv.SetBool = SetBool
    pkg = types.ModuleType('std_srvs')
    pkg.srv = srv
    monkeypatch.setitem(sys.modules, 'std_srvs', pkg)
    monkeypatch.setitem(sys.modules, 'std_srvs.srv', srv)


def _torque_node(profile_id, torque_on, events, sleeps):
    from physical_ai_server import robot_profiles
    fake_time = types.SimpleNamespace(
        monotonic=time.monotonic,
        sleep=lambda s: (sleeps.append(s), events.append(('sleep', s))))
    fns = _load(['_set_follower_torque'], {**_G, 'time': fake_time})
    node = types.SimpleNamespace()
    node._arm_profile = robot_profiles.ROBOT_PROFILES[profile_id]
    node._dxl_torque_lock = threading.Lock()
    node._dxl_torque_client = _Client(events)
    node._follower_torque_on = torque_on
    node.log = _Logger()
    node.get_logger = lambda: node.log
    node._follower_rail_is_ros2_control = lambda: _RAIL(node)
    def _hold(allow_stale=False):
        events.append(('hold', allow_stale))
        return True
    node._hold_follower_at_measured_pose = _hold
    node.note_collision_resettle = lambda: None
    return node, fns['_set_follower_torque']


@pytest.mark.parametrize('profile_id', ['omx_full', 'omx_follower'])
@pytest.mark.parametrize('torque_on', [False, None])
def test_omx_torque_on_holds_in_place_before_energising(
        fake_std_srvs, profile_id, torque_on):
    events, sleeps = [], []
    node, set_torque = _torque_node(profile_id, torque_on, events, sleeps)
    assert set_torque(node, True) is True
    second_settle = (_CONSTS['_TORQUE_ON_HOLD_TIME_FROM_START_S']
                     + _CONSTS['_TORQUE_ON_HOLD_CYCLE_MARGIN_S'])
    # Replace the stale reference, settle, re-read (the limp arm may have sagged)
    # and publish again, let the controller reach it, THEN energise. Both holds
    # accept a stale readback (the solve callback starves /joint_states).
    assert events == [('hold', True), ('sleep', _CONSTS['_TORQUE_ON_HOLD_SETTLE_S']),
                      ('hold', True), ('sleep', second_settle), ('torque', True)]
    # The settle outlasts the hold point, or the controller has not reached it.
    assert (_CONSTS['_TORQUE_ON_HOLD_SETTLE_S']
            > _CONSTS['_TORQUE_ON_HOLD_TIME_FROM_START_S'])
    assert node._follower_torque_on is True


@pytest.mark.parametrize('profile_id', ['edu6_studio', 'edu1_studio'])
def test_feetech_torque_on_publishes_no_hold(fake_std_srvs, profile_id):
    events, sleeps = [], []
    node, set_torque = _torque_node(profile_id, False, events, sleeps)
    assert set_torque(node, True) is True
    assert events == [('torque', True)]


def test_a_redundant_torque_on_over_a_holding_arm_publishes_no_hold(fake_std_srvs):
    events, sleeps = [], []
    node, set_torque = _torque_node('omx_full', True, events, sleeps)
    assert set_torque(node, True) is True
    assert events == [('torque', True)]


def test_torque_off_never_publishes_a_hold(fake_std_srvs):
    events, sleeps = [], []
    node, set_torque = _torque_node('omx_full', True, events, sleeps)
    assert set_torque(node, False) is True
    assert events == [('torque', False)]


def test_a_skipped_hold_still_energises_rather_than_leaving_the_arm_limp(fake_std_srvs):
    events, sleeps = [], []
    node, set_torque = _torque_node('omx_full', False, events, sleeps)
    node._hold_follower_at_measured_pose = (
        lambda allow_stale=False: (events.append(('hold', allow_stale)), False)[1])
    assert set_torque(node, True) is True
    assert events == [('hold', True), ('torque', True)]
    assert sleeps == []


def test_the_hold_is_the_first_thing_after_the_service_check_and_inside_the_lock():
    """Structural pin: the hold sits INSIDE the _dxl_torque_lock block and
    BEFORE call_async, so no concurrent cancel can energise between the two."""
    source = _SERVER_PY.read_text(encoding='utf-8')
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == '_set_follower_torque')
    seg = ast.get_source_segment(source, fn)
    i_lock = seg.index('with self._dxl_torque_lock:')
    i_hold = seg.index('self._hold_follower_at_measured_pose(allow_stale=True)')
    i_call = seg.index('client.call_async(req)')
    assert i_lock < i_hold < i_call


# ─────────────────────────────────────────────────────────────────────────────
# 4. Recorder rounding — a 120 s take fits the cloud cap
# ─────────────────────────────────────────────────────────────────────────────

class _FakeTime:
    def __init__(self, stamps):
        self._stamps = list(stamps)

    def monotonic(self):
        return self._stamps.pop(0) if self._stamps else 0.0

    def sleep(self, s):
        pass


def _sampler(joint_stream, stamps, n=5):
    fns = _load(['_manual_record_sample'], {**_G, 'time': _FakeTime(stamps)})
    node = types.SimpleNamespace()
    it = iter(joint_stream)
    node.communicator = types.SimpleNamespace(
        get_latest_follower_joints=lambda: next(it))
    node._manual_record_active = True
    node._manual_record_start_mono = 0.0
    node._manual_lock = threading.Lock()
    node._handguide_buffer = []
    node._manual_last_activity_mono = 0.0
    node._profile_n = lambda: n
    node._schedule_manual_record_cap_finish = lambda: None
    return node, fns['_manual_record_sample']


def test_sampler_rounds_joints_to_1e4_rad_and_time_to_1_ms():
    q = [0.123456789, -1.570796327, 1.570796327, 0.00004, -0.99995, 0.8]
    # _manual_record_sample reads monotonic twice per tick (stamp + activity).
    node, sample = _sampler([q], [12.3456789, 12.3456789])
    sample(node)
    (row,) = node._handguide_buffer
    assert row == [12.346, 0.1235, -1.5708, 1.5708, 0.0, -1.0, 0.8]


def test_a_max_length_recording_fits_the_cloud_trajectory_cap():
    """120 s × 25 Hz of CONTINUOUS motion on the WIDEST arm (edu6, 8-wide) must
    serialise under validators/workflow.MAX_TRAJECTORY_JSON_BYTES (256 KiB). At
    full precision it did not (measured 463 KiB); rounded it is 201 KiB."""
    cap = 256 * 1024
    n = 6
    fps = 25
    count = int(_CONSTS['RECORD_MAX_S'] * fps)
    rows = []
    for i in range(count):
        t = i / fps + 0.000731 * (i % 7)     # sampler jitter → non-round stamps
        q = [1.3 * math.sin(0.37 * t + k) - 0.0001234 * k for k in range(n + 1)]
        rows.append([round(v, _CONSTS['_MANUAL_RECORD_JOINT_DECIMALS']) for v in q]
                    + [round(t, _CONSTS['_MANUAL_RECORD_TIME_DECIMALS'])])
    rounded = len(json.dumps(rows).encode('utf-8'))
    assert rounded < cap, rounded
    # And the reason rounding exists: the same take at full precision does not fit.
    full = [[1.3 * math.sin(0.37 * (i / fps) + k) - 0.0001234 * k for k in range(n + 1)]
            + [i / fps + 0.000731 * (i % 7)] for i in range(count)]
    assert len(json.dumps(full).encode('utf-8')) > cap


def test_rounding_keeps_the_time_column_non_decreasing():
    stamps = [0.0404, 0.0405, 0.0809, 0.081, 0.1204, 0.1204]
    stream = [[0.0] * 6, [0.01] * 6, [0.02] * 6]
    node, sample = _sampler(stream, stamps)
    for _ in stream:
        sample(node)
    times = [r[0] for r in node._handguide_buffer]
    assert times == sorted(times)


# ─────────────────────────────────────────────────────────────────────────────
# 5. /workshop/jog mode 'home' — slow, stoppable, checked
# ─────────────────────────────────────────────────────────────────────────────

class _JogResp:
    def __init__(self):
        self.success = False
        self.joints = []
        self.world_x = self.world_y = self.world_z = 0.0
        self.message = ''


@pytest.fixture
def fake_chunked_publish(monkeypatch):
    # Import every module that binds `chunked_publish` BY NAME at import time
    # BEFORE patching: otherwise the first import of the handlers package can
    # happen inside a patched test and keep the fake forever (it did — the
    # Blockly replay test then published nothing when run after a glide test).
    import physical_ai_server.workflow.handlers  # noqa: F401
    import physical_ai_server.workflow.handlers.trajectory  # noqa: F401
    from physical_ai_server.workflow import trajectory_builder as tb
    calls = {'points': None, 'result': True, 'stop_fn': None}

    def _fake(publisher, points, should_stop, **kw):
        calls['points'] = list(points)
        calls['stop_fn'] = should_stop
        return calls['result']

    monkeypatch.setattr(tb, 'chunked_publish', _fake)
    return calls


def _glide_node(legs, off=(), n=5, profile_id='omx_full'):
    from physical_ai_server import robot_profiles
    fns = _load(['_run_manual_home_glide'], _G)
    node = types.SimpleNamespace()
    node._arm_profile = robot_profiles.ROBOT_PROFILES[profile_id]
    node._manual_stop_event = threading.Event()
    node._manual_exit_gen = 7
    node.holds = 0
    def _hold(allow_stale=False):
        node.holds += 1
        node.hold_allow_stale = allow_stale
        return True
    node._hold_follower_at_measured_pose = _hold
    node._trajectory_publisher = lambda pts: None
    node._profile_n = lambda: n
    node._build_ik_solver = lambda: node._arm_profile.build_ik()
    node._manual_home_joints_off = lambda target: list(off)
    node.log = _Logger()
    node.get_logger = lambda: node.log
    if isinstance(legs, Exception):
        def _raise(_joints):
            raise legs
        node._plan_manual_home_legs = _raise
    else:
        node._plan_manual_home_legs = lambda _joints: legs
    return node, fns['_run_manual_home_glide']


def _span(points):
    return points[-1][1]


def test_a_short_home_leg_is_never_faster_than_the_minimum_duration(fake_chunked_publish):
    q0 = [0.2, -1.4, 1.4, 0.0, 0.0, 0.8]
    q1 = [0.0, -math.pi / 2, math.pi / 2, 0.0, 0.0, 0.8]
    node, glide = _glide_node([(q0, q1, 3.0)])
    resp = _JogResp()
    glide(node, q0, 7, resp)
    pts = fake_chunked_publish['points']
    assert abs(_span(pts) - _CONSTS['_MANUAL_HOME_MIN_DURATION_S']) < 0.05
    assert resp.success is True
    assert resp.message == 'Der Arm steht in der Grundstellung.'
    assert list(pts[-1][0]) == q1


def test_a_long_home_leg_is_stretched_to_the_gentle_peak_speed(fake_chunked_publish):
    delta = math.pi
    q0 = [delta, -math.pi / 2, math.pi / 2, 0.0, 0.0, 0.8]
    q1 = [0.0, -math.pi / 2, math.pi / 2, 0.0, 0.0, 0.8]
    node, glide = _glide_node([(q0, q1, 3.0)])
    glide(node, q0, 7, _JogResp())
    pts = fake_chunked_publish['points']
    expected = delta * (15.0 / 8.0) / _CONSTS['_MANUAL_HOME_PEAK_RAD_S']
    assert abs(_span(pts) - expected) < 0.05
    # Measured peak joint speed on the published waypoints stays at the cap.
    peak = max(abs(b[0][0] - a[0][0]) / (b[1] - a[1]) for a, b in zip(pts, pts[1:]))
    assert peak <= _CONSTS['_MANUAL_HOME_PEAK_RAD_S'] * 1.02


def test_a_two_leg_route_is_played_back_to_back(fake_chunked_publish):
    q0 = [0.0, 1.2, -0.3, 0.0, 1.0, 0.0, 0.5]
    via = [0.0, 0.5, -1.5, 0.0, 0.9, 0.0, 0.5]
    home = [0.0, 0.70, -2.40, 0.0, 0.70, 0.0, 0.5]
    node, glide = _glide_node([(q0, via, 1.5), (via, home, 3.0)], n=6,
                              profile_id='edu6_studio')
    glide(node, q0, 7, _JogResp())
    pts = fake_chunked_publish['points']
    times = [p[1] for p in pts]
    assert times == sorted(times)
    assert list(pts[-1][0]) == home
    assert any(p[0] == via for p in pts)


def test_a_stopped_glide_holds_and_says_so(fake_chunked_publish):
    fake_chunked_publish['result'] = False
    q0 = [0.2, -1.4, 1.4, 0.0, 0.0, 0.8]
    q1 = [0.0, -math.pi / 2, math.pi / 2, 0.0, 0.0, 0.8]
    node, glide = _glide_node([(q0, q1, 3.0)])
    resp = _JogResp()
    glide(node, q0, 7, resp)
    assert node.holds == 1
    assert node.hold_allow_stale is False       # a moving arm never gets an OLD pose
    assert resp.success is False
    assert resp.message.startswith('Fahrt in die Grundstellung gestoppt — der Arm hält')


def test_the_stop_predicate_follows_both_the_event_and_the_exit_generation(
        fake_chunked_publish):
    q0 = [0.2, -1.4, 1.4, 0.0, 0.0, 0.8]
    q1 = [0.0, -math.pi / 2, math.pi / 2, 0.0, 0.0, 0.8]
    node, glide = _glide_node([(q0, q1, 3.0)])
    glide(node, q0, 7, _JogResp())
    stop = fake_chunked_publish['stop_fn']
    assert stop() is False
    node._manual_exit_gen = 8          # „Beenden" / „Stopp" bumped it
    assert stop() is True
    node._manual_exit_gen = 7
    node._manual_stop_event.set()
    assert stop() is True


def test_a_planner_refusal_is_reported_and_nothing_moves(fake_chunked_publish):
    from physical_ai_server.workflow.handlers.motion import WorkflowError
    node, glide = _glide_node(WorkflowError('Tischebene im Weg.'))
    resp = _JogResp()
    glide(node, [0.0] * 6, 7, resp)
    assert fake_chunked_publish['points'] is None
    assert resp.success is False and resp.message == 'Tischebene im Weg.'


def test_a_glide_that_did_not_arrive_warns_but_is_not_an_error(fake_chunked_publish):
    q0 = [0.2, -1.4, 1.4, 0.0, 0.0, 0.8]
    q1 = [0.0, -math.pi / 2, math.pi / 2, 0.0, 0.0, 0.8]
    node, glide = _glide_node([(q0, q1, 3.0)], off=(1, 2))
    resp = _JogResp()
    glide(node, q0, 7, resp)
    assert resp.success is True
    assert 'nicht ganz erreicht (Gelenk 2, 3)' in resp.message


def test_the_jog_callback_routes_mode_home_to_the_glide_and_not_to_the_jog_solver():
    source = _SERVER_PY.read_text(encoding='utf-8')
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == 'workshop_jog_callback')
    seg = ast.get_source_segment(source, fn)
    # AFTER the torque-on assert and the finite-joint check, BEFORE the jog solve.
    i_limp = seg.index('self._follower_torque_on is False')
    i_torque = seg.index('self._set_follower_torque(True)')
    i_finite = seg.index('math.isfinite(v) for v in joints')
    i_home = seg.index("== 'home':")
    i_solve = seg.index('self._compute_jog_target(')
    # The limp-arm refusal comes BEFORE the defensive torque-on (which would
    # otherwise stiffen a hand-guided arm and then drive it).
    assert i_limp < i_torque < i_finite < i_home < i_solve


# ─────────────────────────────────────────────────────────────────────────────
# 6. _plan_manual_home_legs — the REAL planner on all four arms
# ─────────────────────────────────────────────────────────────────────────────

def _planner_node(profile_id, calib=None):
    from physical_ai_server import robot_profiles
    fns = _load(['_plan_manual_home_legs'], _G)
    node = types.SimpleNamespace()
    node._arm_profile = robot_profiles.ROBOT_PROFILES[profile_id]
    node._ik = node._arm_profile.build_ik()
    node._build_ik_solver = lambda: node._ik
    node._load_workflow_calibration = lambda: dict(calib or {})
    node._profile_n = lambda: node._arm_profile.num_arm_joints
    node.log = _Logger()
    node.get_logger = lambda: node.log
    return node, fns['_plan_manual_home_legs']


@pytest.mark.parametrize('profile_id', ['omx_full', 'omx_follower',
                                        'edu6_studio', 'edu1_studio'])
def test_every_arm_plans_to_its_own_home_and_carries_the_gripper(profile_id):
    node, plan = _planner_node(profile_id)
    profile = node._arm_profile
    n = profile.num_arm_joints
    home = list(profile.home_joints_rad)
    # Start a little off HOME (a pose the arm is holding after a re-lock).
    start = [h + (0.15 if i % 2 == 0 else -0.1) for i, h in enumerate(home)]
    lo_hi = node._ik.joint_limits
    start = [min(max(v, lo_hi[i][0] + 1e-3), lo_hi[i][1] - 1e-3)
             for i, v in enumerate(start)]
    grip = 0.3
    legs = plan(node, start + [grip])
    assert legs, 'planner returned no route'
    assert list(legs[0][0]) == pytest.approx(start + [grip])
    end = list(legs[-1][1])
    assert end[:n] == pytest.approx(home)
    assert end[n] == pytest.approx(grip)         # carried, never opened/closed


def test_the_feetech_planner_refuses_or_lifts_rather_than_drive_through_the_table():
    """A start pose whose straight line to HOME presses a link into the table
    must NOT come back as one direct leg on edu6: the home_planner ladder either
    lifts over a via or refuses in German. (The OMX has no link-box table; its
    TCP was measured never to dip on this line.)"""
    from physical_ai_server.workflow import arm_geometry
    from physical_ai_server.workflow.handlers.motion import WorkflowError
    node, plan = _planner_node('edu6_studio')
    ik = node._ik
    geom = arm_geometry.resolve_geometry(ik)
    home = list(node._arm_profile.home_joints_rad)
    import random
    rng = random.Random(11)
    found = False
    for _ in range(4000):
        q = [rng.uniform(lo, hi) for lo, hi in ik.joint_limits]
        start_clear = geom.floor_clearance(q, lambda x, y: 0.0)
        if start_clear is None or start_clear < 0.0:
            continue
        line = geom.swept_floor_clearance(q, home, lambda x, y: 0.0)
        if line is None or line >= -0.05:
            continue
        found = True
        try:
            legs = plan(node, q + [1.0])
        except WorkflowError as e:
            assert 'Grundstellung' in str(e)
            break
        assert len(legs) >= 2, 'a table-pressing direct line came back as one leg'
        break
    assert found, 'no table-pressing start pose sampled — widen the search'


# ─────────────────────────────────────────────────────────────────────────────
# 7. The chosen numbers, pinned as LITERALS (test_constant_pins B-0)
# ─────────────────────────────────────────────────────────────────────────────

_SHIPPED = (
    ('_TORQUE_ON_HOLD_TIME_FROM_START_S', 0.05),
    ('_TORQUE_ON_HOLD_SETTLE_S', 0.2),
    ('_TORQUE_ON_HOLD_CYCLE_MARGIN_S', 0.02),
    ('_COMMAND_RAIL_MATCH_WAIT_S', 1.0),
    ('_MANUAL_HOME_ALREADY_THERE_RAD', 0.01),
    ('_MANUAL_HOME_PEAK_RAD_S', 1.0),
    ('_MANUAL_HOME_MIN_DURATION_S', 3.0),
    ('_MANUAL_RECORD_JOINT_DECIMALS', 4),
    ('_MANUAL_RECORD_TIME_DECIMALS', 3),
    ('_MANUAL_RECORD_FPS', 25),
    ('_MANUAL_RECORD_MAX_SAMPLES', 3100),
    ('_MANUAL_RECORD_MIN_DELTA_RAD', 0.003),
)


@pytest.mark.parametrize('name, literal', _SHIPPED)
def test_the_chosen_numbers_are_pinned_as_literals(name, literal):
    assert _CONSTS[name] == literal


# ─────────────────────────────────────────────────────────────────────────────
# 8. Replay chunks the OMX controller will ACCEPT (the "no motion at all" bug)
# ─────────────────────────────────────────────────────────────────────────────

# ros2_controllers' JointTrajectoryController (jazzy, >= 4.0.0) validate_trajectory_msg:
# with allow_nonzero_velocity_at_trajectory_end = false (the default; the OMX
# overlay YAML does not set it) any message whose LAST point carries a velocity
# above float epsilon is rejected, and times must be strictly increasing.
_FLT_EPS = 1.1920929e-07


def _jtc_accepts(chunk):
    times = [p[1] for p in chunk]
    if any(b <= a for a, b in zip(times, times[1:])):
        return False
    last = chunk[-1]
    if len(last) >= 3 and last[2] is not None:
        if any(abs(v) > _FLT_EPS for v in last[2]):
            return False
    return True


class _FakeClock:
    """A private clock for trajectory_builder only: sleep() advances it, so
    chunked_publish paces a 16 s stream instantly and no other module's
    time.sleep is touched."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.0, float(seconds))


def _a_15_s_recording():
    rows = []
    t = 0.0
    while t < 15.0:
        t += 0.04
        q = [0.6 * math.sin(0.5 * t), -1.2 + 0.4 * math.sin(0.3 * t),
             1.3 + 0.3 * math.cos(0.4 * t), 0.2 * math.sin(0.7 * t), 0.0, 0.8]
        rows.append([round(v, 4) for v in q] + [round(t, 3)])
    return rows


def test_every_chunk_of_a_workshop_replay_is_accepted_by_the_omx_controller(monkeypatch):
    from physical_ai_server.workflow import trajectory_builder as tb
    fns = _load(['workshop_replay_callback', '_run_replay'], _G)
    node = types.SimpleNamespace()
    node._follower_joints_stale = lambda: False
    node._assert_no_other_active = lambda mode: (True, '')
    node._manual_record_active = False
    node._manual_leader_blocks = lambda: (False, '')
    node.communicator = _Comm([0.0, -math.pi / 2, math.pi / 2, 0.0, 0.0, 0.8])
    node._manual_replay_thread = None
    node._profile_n = lambda: 5
    node._build_replay_lead_floor_check = lambda: None
    node._arm_profile = None
    node._workflow_traj_publisher = object()
    node._mode_lock = threading.Lock()
    node._manual_lock = threading.Lock()
    node._manual_transient_ops = 0
    node._manual_exit_gen = 0
    node._manual_stop_event = threading.Event()
    node._manual_last_activity_mono = 0.0
    node._recompute_on_manual_locked = lambda: None
    node._retorque_follower_or_keep_locked = lambda response: True
    node._publish_manual_notice = lambda text: None
    node._run_replay = lambda *a: None          # the Thread target (not started)
    node._build_ik_solver = lambda: None
    node.log = _Logger()
    node.get_logger = lambda: node.log
    chunks = []
    node._trajectory_publisher = lambda chunk: chunks.append(list(chunk))
    started = []

    class _NoThread:
        def __init__(self, target, args, **kw):
            started.append((target, args))

        def start(self):
            pass

        def is_alive(self):
            return False

    monkeypatch.setattr(tb, 'time', _FakeClock())
    fake_threading = types.SimpleNamespace(Thread=_NoThread, Lock=threading.Lock,
                                           current_thread=threading.current_thread)
    fns = _load(['workshop_replay_callback', '_run_replay'],
                {**_G, 'threading': fake_threading})
    resp = types.SimpleNamespace(success=False, message='')
    req = types.SimpleNamespace(
        points_json=json.dumps({'fps': 25, 'points': _a_15_s_recording()}),
        name='', speed=1.0)
    fns['workshop_replay_callback'](node, req, resp)
    assert resp.success is True, resp.message
    (target, args), = started
    fns['_run_replay'](node, *args)
    assert len(chunks) >= 10
    rejected = [i for i, c in enumerate(chunks) if not _jtc_accepts(c)]
    assert rejected == [], f'{len(rejected)}/{len(chunks)} chunks would be rejected'


def test_every_chunk_of_a_blockly_replay_is_accepted_by_the_omx_controller(monkeypatch):
    from physical_ai_server.workflow.handlers import trajectory as tr
    chunks = []
    ctx = types.SimpleNamespace(
        trajectories={'Bewegung 1': {'fps': 25, 'points': _a_15_s_recording()}},
        last_full_joints=[0.0, -math.pi / 2, math.pi / 2, 0.0, 0.0, 0.8],
        last_arm_joints=[0.0, -math.pi / 2, math.pi / 2, 0.0, 0.0],
        num_arm_joints=5, ik=None, z_table=None, table_plane=None, zones=None,
        motion_lock=threading.RLock(), should_stop=lambda: False, tempo=1.0,
        publisher=lambda chunk: chunks.append(list(chunk)),
        log=lambda *a, **k: None,
    )
    import physical_ai_server.workflow.trajectory_builder as tb
    monkeypatch.setattr(tb, 'time', _FakeClock())
    tr.replay_trajectory(ctx, {'name': 'Bewegung 1'})
    assert len(chunks) >= 10
    rejected = [i for i, c in enumerate(chunks) if not _jtc_accepts(c)]
    assert rejected == [], f'{len(rejected)}/{len(chunks)} chunks would be rejected'


def test_the_replay_velocity_switch_is_off_and_both_callers_use_it():
    from physical_ai_server.workflow.handlers import trajectory as tr
    assert tr.REPLAY_PUBLISHES_VELOCITIES is False
    # Every production CALL of resegment_trajectory passes the switch by name —
    # judged on the AST, so the explanatory comment naming the old literal does
    # not count.
    for path in (_SERVER_PY, Path(tr.__file__)):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and getattr(n.func, 'id', getattr(n.func, 'attr', None))
                 == 'resegment_trajectory']
        assert calls, f'no resegment_trajectory call in {path.name}'
        for call in calls:
            kw = {k.arg: k.value for k in call.keywords}
            assert 'with_velocities' in kw, f'{path.name}: implicit velocities'
            value = kw['with_velocities']
            assert isinstance(value, ast.Name) and value.id == 'REPLAY_PUBLISHES_VELOCITIES', (
                f'{path.name}: with_velocities is not the switch')


# ─────────────────────────────────────────────────────────────────────────────
# 9. Verifier findings (2026-09-13): stale holds, limp refusal, honest messages
# ─────────────────────────────────────────────────────────────────────────────

def test_the_pre_energise_hold_uses_a_stale_readback_but_a_stop_hold_does_not():
    q = [0.1, -0.4, 0.9, 0.2, -0.3, 0.5]
    node = _hold_node(q, stale=True)
    assert _HOLD(node, allow_stale=True) is True     # solve callback starved /joint_states
    assert node.published and node.published[0][0][0] == q
    node = _hold_node(q, stale=True)
    assert _HOLD(node) is False                      # a moving arm never gets an OLD pose
    assert node.published == []


def test_an_arm_already_at_home_is_told_so_and_nothing_is_published(fake_chunked_publish):
    home = [0.0, -math.pi / 2, math.pi / 2, 0.0, 0.0, 0.8]
    near = [v + 0.004 for v in home[:5]] + [0.8]
    node, glide = _glide_node([(near, home, 3.0)])
    resp = _JogResp()
    glide(node, near, 7, resp)
    assert fake_chunked_publish['points'] is None
    assert resp.success is True
    assert resp.message == 'Der Arm steht bereits in der Grundstellung.'


def test_an_unexpected_planner_exception_is_a_german_refusal_not_a_dead_node(
        fake_chunked_publish):
    node, glide = _glide_node(RuntimeError('numpy exploded'))
    resp = _JogResp()
    glide(node, [0.0] * 6, 7, resp)                  # must not raise
    assert resp.success is False
    assert 'nicht geplant werden' in resp.message
    assert fake_chunked_publish['points'] is None


def test_a_publish_exception_holds_and_reports(monkeypatch):
    import physical_ai_server.workflow.handlers  # noqa: F401
    from physical_ai_server.workflow import trajectory_builder as tb

    def _boom(**kw):
        raise RuntimeError('publisher gone')
    monkeypatch.setattr(tb, 'chunked_publish', _boom)
    q0 = [0.2, -1.4, 1.4, 0.0, 0.0, 0.8]
    q1 = [0.0, -math.pi / 2, math.pi / 2, 0.0, 0.0, 0.8]
    node, glide = _glide_node([(q0, q1, 3.0)])
    resp = _JogResp()
    glide(node, q0, 7, resp)
    assert node.holds == 1 and resp.success is False
    assert 'abgebrochen' in resp.message


def test_a_skipped_stop_hold_does_not_claim_the_arm_holds(fake_chunked_publish):
    fake_chunked_publish['result'] = False
    q0 = [0.2, -1.4, 1.4, 0.0, 0.0, 0.8]
    q1 = [0.0, -math.pi / 2, math.pi / 2, 0.0, 0.0, 0.8]
    node, glide = _glide_node([(q0, q1, 3.0)])
    node._hold_follower_at_measured_pose = lambda allow_stale=False: False
    resp = _JogResp()
    glide(node, q0, 7, resp)
    assert 'hält' not in resp.message
    assert 'letzte Teilbewegung' in resp.message


def test_planner_notes_reach_the_student_with_the_result(fake_chunked_publish):
    q0 = [0.2, -1.4, 1.4, 0.0, 0.0, 0.8]
    q1 = [0.0, -math.pi / 2, math.pi / 2, 0.0, 0.0, 0.8]
    node, glide = _glide_node([(q0, q1, 3.0)])

    def _plan(_joints):
        node._manual_home_notes.append('Der Arm wird zuerst angehoben.')
        return [(q0, q1, 3.0)]
    node._plan_manual_home_legs = _plan
    resp = _JogResp()
    glide(node, q0, 7, resp)
    assert resp.message.endswith('Hinweis: Der Arm wird zuerst angehoben.')


def test_the_arrival_check_itself(monkeypatch):
    fns = _load(['_manual_home_joints_off'],
                {**_G, 'time': types.SimpleNamespace(sleep=lambda s: None)})
    node = types.SimpleNamespace(communicator=_Comm([0.0, -1.0, 1.0, 0.31, 0.0, 0.8]))
    target = [0.0, -1.0, 1.0, 0.0, 0.0]
    assert fns['_manual_home_joints_off'](node, target) == [3]
    node.communicator.joints = [0.0, -1.0, 1.0, 0.29, 0.0, 0.8]
    assert fns['_manual_home_joints_off'](node, target) == []
    node.communicator.joints = None                  # unreadable → never an error
    assert fns['_manual_home_joints_off'](node, target) == []


# ── the OMX point-model floor ladder (home_planner.plan_floor_checked_home_route) ────

def _omx_ctx():
    from physical_ai_server.workflow.ik_solver import IKSolver
    return types.SimpleNamespace(ik=IKSolver(), z_table=None, table_plane=None,
                                 zones=None, log=lambda *a: None, num_arm_joints=5)


def test_omx_table_taps_stay_one_direct_leg():
    from physical_ai_server.workflow import home_planner as hp
    from physical_ai_server.workflow.handlers import motion as m
    import random
    ctx = _omx_ctx()
    home = list(m.HOME_JOINTS_RAD) + [0.8]
    rng = random.Random(5)
    taps = 0
    for _ in range(400):
        r, a = rng.uniform(0.08, 0.26), rng.uniform(-1.4, 1.4)
        sol = ctx.ik.solve((r * math.cos(a), r * math.sin(a), 0.0),
                           roll=rng.uniform(-1.5, 1.5))
        if sol is None:
            continue
        taps += 1
        legs = hp.plan_floor_checked_home_route(ctx, list(sol)[:5] + [0.8], home, 3.0)
        assert len(legs) == 1
    assert taps > 100


def test_omx_starts_whose_direct_line_goes_through_the_table_lift_first_or_refuse():
    from physical_ai_server.workflow import home_planner as hp
    from physical_ai_server.workflow.handlers import motion as m
    import random
    ctx = _omx_ctx()
    ik = ctx.ik
    home_arm = list(m.HOME_JOINTS_RAD)
    home = home_arm + [0.8]
    idx = hp._moving_point_start(ik)
    assert idx == 11          # the base column up to the shoulder is fixed-height
    floor = lambda x, y: 0.0  # noqa: E731
    rng = random.Random(7)
    seen = {'lift': 0, 'refused': 0}
    for _ in range(40000):
        q = [rng.uniform(lo, hi) for lo, hi in ik.joint_limits]
        if not 0.0 <= ik.fk(q)[1][2] <= 0.06:
            continue
        start_clear = hp._point_clearance(ik, q, floor, idx)
        if start_clear is None or start_clear < -0.045:
            continue
        if hp._point_line_ok(ik, q, home_arm, floor, idx, min(0.0, start_clear)):
            continue                       # the direct line was fine
        try:
            legs = hp.plan_floor_checked_home_route(ctx, q + [0.8], home, 3.0)
        except m.WorkflowError as e:
            assert 'Tischebene' in str(e)
            seen['refused'] += 1
            continue
        assert len(legs) == 2, 'a table-crossing direct line came back as ONE leg'
        for a, b, _d in legs:
            assert hp._point_line_ok(ik, a[:5], b[:5], floor, idx,
                                     min(0.0, start_clear) - 1e-6)
        assert legs[-1][1] == home and legs[0][1][5] == 0.8   # gripper carried
        seen['lift'] += 1
        if seen['lift'] >= 20 and seen['refused'] >= 5:
            break
    assert seen['lift'] >= 20 and seen['refused'] >= 5, seen


def test_box_table_arms_and_the_rollback_use_the_block_planner_unchanged(monkeypatch):
    from physical_ai_server import robot_profiles
    from physical_ai_server.workflow import home_planner as hp
    calls = []
    monkeypatch.setattr(hp, 'plan_home_route',
                        lambda ctx, a, b, d: calls.append('block') or [(a, b, d)])
    edu6 = robot_profiles.ROBOT_PROFILES['edu6_studio']
    ctx = types.SimpleNamespace(ik=edu6.build_ik(), z_table=None, table_plane=None,
                                zones=None, log=lambda *a: None, num_arm_joints=6)
    start = list(edu6.home_joints_rad) + [1.0]
    hp.plan_floor_checked_home_route(ctx, start, start, 3.0)
    assert calls == ['block']
    monkeypatch.setattr(hp, 'FLOOR_TOL_M', 0.0)          # EDUBOTICS_HOME_FLOOR_TOL_M=0
    omx = _omx_ctx()
    hp.plan_floor_checked_home_route(omx, [0.0] * 6, [0.0] * 6, 3.0)
    assert calls == ['block', 'block']


def test_the_server_routes_the_manual_glide_through_the_manual_planner():
    source = _SERVER_PY.read_text(encoding='utf-8')
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == '_plan_manual_home_legs')
    seg = ast.get_source_segment(source, fn)
    assert 'home_planner.plan_floor_checked_home_route(' in seg
    assert 'home_planner.plan_home_route(' not in seg



# ─────────────────────────────────────────────────────────────────────────────
# 10. The per-arm audits (2026-09-13)
# ─────────────────────────────────────────────────────────────────────────────

def _server_fn_source(name):
    source = _SERVER_PY.read_text(encoding='utf-8')
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(source, fn)


def test_the_command_rail_publisher_is_created_at_boot_before_the_profile_init():
    seg = _server_fn_source('__init__')
    i_pub = seg.index("'/leader/joint_trajectory'")
    i_last = seg.rindex('self._init_robot_profile()')
    assert i_pub < i_last
    assert seg.rstrip().endswith('self._init_robot_profile()')   # still LAST


def test_the_hold_waits_for_a_matched_subscriber_and_says_when_there_is_none():
    class _Pub:
        def __init__(self, counts):
            self.counts = list(counts)

        def get_subscription_count(self):
            return self.counts.pop(0) if len(self.counts) > 1 else self.counts[0]

    fns = _load(['_wait_for_command_rail_subscriber'],
                {**_G, '_COMMAND_RAIL_MATCH_WAIT_S': 0.1})
    wait = fns['_wait_for_command_rail_subscriber']
    node = types.SimpleNamespace(_workflow_traj_publisher=_Pub([0, 0, 1]))
    assert wait(node) is True
    node = types.SimpleNamespace(_workflow_traj_publisher=_Pub([0]))
    t0 = time.monotonic()
    assert wait(node) is False
    assert time.monotonic() - t0 < 0.5
    assert wait(types.SimpleNamespace()) is True     # nothing to ask → never a gate


def test_a_real_program_start_asserts_torque_before_the_manager_starts():
    seg = _server_fn_source('_launch_workflow')
    i_leader = seg.index('leader_active')
    i_torque = seg.index('torque_on(True)')
    i_start = seg.index('manager.start(')
    i_sim = seg.index('if sim_enabled:')
    assert i_sim < i_leader < i_torque < i_start
    assert 'nicht verriegelt' in seg


def _edu6_limits():
    from physical_ai_server import robot_profiles
    return robot_profiles.ROBOT_PROFILES['edu6_studio'].build_ik().joint_limits


def test_a_recording_across_the_joint6_seam_is_refused_in_german():
    from physical_ai_server.workflow.handlers import trajectory as tr
    from physical_ai_server.workflow.handlers.motion import WorkflowError
    rows = [[0, 0.7, -2.4, 0, 0.7, 3.128, 1.0, 0.0],
            [0, 0.7, -2.4, 0, 0.7, -3.116, 1.0, 0.04]]
    with pytest.raises(WorkflowError) as e:
        tr.refuse_wrapped_joint_jumps(rows, _edu6_limits(), num_arm_joints=6)
    assert 'Gelenk 6' in str(e.value) and '180' in str(e.value)


def test_normal_motion_and_non_full_circle_joints_are_not_refused():
    from physical_ai_server.workflow.handlers import trajectory as tr
    rows = [[0, 0.7, -2.4, 0, 0.7, 3.0, 1.0, 0.0],
            [0, 0.7, -2.4, 0, 0.7, 2.6, 1.0, 0.04]]
    tr.refuse_wrapped_joint_jumps(rows, _edu6_limits(), num_arm_joints=6)
    tr.refuse_wrapped_joint_jumps(rows, None, num_arm_joints=6)   # profile-less
    from physical_ai_server.workflow.ik_solver import IKSolver
    omx = [[0.0, -1.5, 1.5, 0, 0, 0.8, 0.0], [0.1, -1.4, 1.4, 0, 0, 0.8, 0.04]]
    tr.refuse_wrapped_joint_jumps(omx, IKSolver().joint_limits, num_arm_joints=5)


def test_the_blockly_replay_refuses_a_seam_crossing_before_moving(monkeypatch):
    from physical_ai_server.workflow.handlers import trajectory as tr
    from physical_ai_server.workflow.handlers.motion import WorkflowError
    from physical_ai_server import robot_profiles
    chunks = []
    ik = robot_profiles.ROBOT_PROFILES['edu6_studio'].build_ik()
    ctx = types.SimpleNamespace(
        trajectories={'B': {'fps': 25, 'points': [
            [0, 0.7, -2.4, 0, 0.7, 3.128, 1.0, 0.0],
            [0, 0.7, -2.4, 0, 0.7, -3.116, 1.0, 0.04]]}},
        last_full_joints=[0, 0.7, -2.4, 0, 0.7, 3.1, 1.0],
        last_arm_joints=[0, 0.7, -2.4, 0, 0.7, 3.1], num_arm_joints=6, ik=ik,
        z_table=None, table_plane=None, zones=None, motion_lock=threading.RLock(),
        should_stop=lambda: False, tempo=1.0,
        publisher=lambda c: chunks.append(c), log=lambda *a, **k: None)
    with pytest.raises(WorkflowError):
        tr.replay_trajectory(ctx, {'name': 'B'})
    assert chunks == []


def _edu1_true_claw_rise(g):
    """The claw's LOWEST point above the closed-tip TCP, recomputed from the
    SHIPPED finger meshes through RL_joint / LF_joint (URDF origins, flipped axes,
    mimic) — the ground truth tool_tip_rise_m must never exceed."""
    import struct
    meshes = (Path(__file__).resolve().parents[2] / 'physical_ai_manager' / 'public'
              / 'edu1-urdf' / 'meshes')

    def _stl(name):
        raw = (meshes / name).read_bytes()
        count = struct.unpack('<I', raw[80:84])[0]
        tri = np.frombuffer(raw[84:84 + count * 50], dtype=np.dtype(
            [('n', '<3f4'), ('v', '<9f4'), ('a', '<u2')]))
        return tri['v'].reshape(-1, 3).astype(float)

    def _rpy(r, p, y):
        rx = np.array([[1, 0, 0], [0, math.cos(r), -math.sin(r)], [0, math.sin(r), math.cos(r)]])
        ry = np.array([[math.cos(p), 0, math.sin(p)], [0, 1, 0], [-math.sin(p), 0, math.cos(p)]])
        rz = np.array([[math.cos(y), -math.sin(y), 0], [math.sin(y), math.cos(y), 0], [0, 0, 1]])
        return rz @ ry @ rx

    def _rotz(a):
        return np.array([[math.cos(a), -math.sin(a), 0], [math.sin(a), math.cos(a), 0], [0, 0, 1]])
    cache = getattr(_edu1_true_claw_rise, '_cache', None)
    if cache is None:
        cache = {
            'right': _stl('right_finger.STL'), 'left': _stl('left_finger.STL'),
            'R0': _rpy(-1.5708, 0, -1.5708)}
        _edu1_true_claw_rise._cache = cache
    lowest = max(
        ((cache['R0'] @ _rotz(g * sign) @ cache[key].T).T + np.array(origin))[:, 2].max()
        for key, origin, sign in (('right', (0.0202, -0.00925, 0.02125), +1),
                                  ('left', (0.0195, 0.00925, 0.02125), -1)))
    return 0.08625 - lowest


def test_edu1_claw_tip_rise_never_over_credits_the_mesh_derived_truth():
    from physical_ai_server.workflow.edu1_ik import Edu1IKSolver
    ik = Edu1IKSolver()
    worst_over = -1.0
    worst_under = 0.0
    for g in np.linspace(0.0, 1.5708, 400):
        truth = _edu1_true_claw_rise(float(g))
        model = ik.tool_tip_rise_m(float(g))
        worst_over = max(worst_over, model - truth)
        if g <= 0.9:
            worst_under = max(worst_under, truth - model)
    assert worst_over <= 1e-6, f'over-credits by {worst_over * 1000:.3f} mm'
    assert worst_under < 0.0025, f'needlessly pessimistic by {worst_under * 1000:.2f} mm'
    # The dip is real and reported as a NEGATIVE rise (stricter, never looser).
    assert ik.tool_tip_rise_m(0.14) < 0.0
    assert ik.tool_tip_rise_m(0.9) == pytest.approx(0.01742)
    assert ik.tool_tip_rise_m(float('nan')) == 0.0
    from physical_ai_server.workflow.handlers import motion as m
    from physical_ai_server.workflow.ik_solver import IKSolver
    assert m.tool_tip_rise_m(IKSolver(), [0] * 5 + [0.8], 5) == 0.0
    assert m.tool_tip_rise_m(ik, [0] * 5 + [0.9], 5) == pytest.approx(0.01742)
    bogus = types.SimpleNamespace(tool_tip_rise_m=lambda g: 5.0)
    assert m.tool_tip_rise_m(bogus, [0] * 5 + [0.9], 5) == 0.06      # capped credit


def test_an_edu1_take_whose_OPEN_claw_touches_the_table_is_replayable():
    from physical_ai_server.workflow.edu1_ik import Edu1IKSolver
    from physical_ai_server.workflow.handlers import trajectory as tr
    from physical_ai_server.workflow.handlers import motion as m
    from physical_ai_server.workflow.handlers.motion import WorkflowError
    ik = Edu1IKSolver()
    rise = ik.tool_tip_rise_m(0.9)
    # TCP (closed tip) 17 mm under the table = the open tips exactly on it.
    q = ik.solve((0.0, -0.20, -rise + 0.0005), roll=0.0)
    assert q is not None
    ctx = types.SimpleNamespace(z_table=0.0, table_plane=None)

    def floor(q_full):
        _R, t = ik.fk(q_full[:5])
        return (float(t[2]) + m.tool_tip_rise_m(ik, q_full, 5)
                < m._floor_z_at(ctx, t[0], t[1]) - m.WORKSPACE_FLOOR_MARGIN_M)
    open_rows = [list(q) + [0.9, 0.0], list(q) + [0.9, 0.04]]
    tr.resegment_trajectory(open_rows, point_floor_check=floor, num_arm_joints=5)
    closed_rows = [list(q) + [0.0, 0.0], list(q) + [0.0, 0.04]]
    with pytest.raises(WorkflowError):          # a CLOSED claw there IS in the table
        tr.resegment_trajectory(closed_rows, point_floor_check=floor, num_arm_joints=5)


def test_both_replay_floor_checks_credit_the_claw_rise():
    handler = Path(__import__('physical_ai_server.workflow.handlers.trajectory',
                              fromlist=['x']).__file__).read_text(encoding='utf-8')
    assert 'tool_tip_rise_m(ik, q6, n)' in handler
    assert '_motion.tool_tip_rise_m(ik, q6, _n_fk)' in _server_fn_source(
        '_build_replay_lead_floor_check')


def test_sub_millisecond_recorded_steps_stay_strictly_increasing_on_the_wire():
    from physical_ai_server.workflow.handlers import trajectory as tr
    assert tr._MIN_PAIR_DT_S == 0.001
    base = [0.0, -1.5, 1.5, 0.0, 0.0, 0.8]
    rows = [base + [0.5], base + [0.5 + 1e-10], base + [0.5 + 2e-10], base + [1.0]]
    seg = tr.resegment_trajectory(rows, 1.0, num_arm_joints=5)
    ns = [int(t) * 10**9 + int((t - int(t)) * 1e9) for _q, t in seg]
    assert all(b > a for a, b in zip(ns, ns[1:])), ns


def test_a_hold_with_no_matched_subscriber_reports_it_was_not_delivered():
    fns = _load(['_hold_follower_at_measured_pose', '_wait_for_command_rail_subscriber'],
                {**_G, '_COMMAND_RAIL_MATCH_WAIT_S': 0.05})
    q = [0.1, -0.4, 0.9, 0.2, -0.3, 0.5]
    node = _hold_node(q)
    node._workflow_traj_publisher = types.SimpleNamespace(get_subscription_count=lambda: 0)
    node._wait_for_command_rail_subscriber = (
        lambda: fns['_wait_for_command_rail_subscriber'](node))
    assert fns['_hold_follower_at_measured_pose'](node) is False
    assert node.published                      # sent anyway (best effort) …
    assert any('NO matched subscriber' in text for _lvl, text in node.log.lines)
    node._workflow_traj_publisher = types.SimpleNamespace(get_subscription_count=lambda: 1)
    assert fns['_hold_follower_at_measured_pose'](node) is True


# ─────────────────────────────────────────────────────────────────────────────
# 11. Owner-approved safety-path changes (2026-09-13)
# ─────────────────────────────────────────────────────────────────────────────

def test_linear_replay_segment_keeps_constant_speed_and_the_0_6_limit_floor():
    from physical_ai_server.workflow import trajectory_builder as tb
    q0, q1 = [0.0, 0.0], [0.08, -0.02]
    seg = tb.build_linear_segment(q0, q1, 0.04, velocity_limit=4.8)   # 2.0 rad/s: allowed
    assert len(seg) == 1 and seg[0][1] == pytest.approx(0.04)
    assert seg[0][0] == pytest.approx(q1)
    fast = tb.build_linear_segment([0.0], [0.2], 0.04, velocity_limit=4.8)  # 5 rad/s
    span = fast[-1][1]
    assert 0.2 / span == pytest.approx(0.6 * 4.8)                        # stretched to v_safe
    speeds = [abs(b[0][0] - a[0][0]) / (b[1] - a[1])
              for a, b in zip([([0.0], 0.0)] + fast, fast)]
    assert max(speeds) == pytest.approx(min(speeds))                     # constant: no ripple
    assert tb.build_linear_segment([0.0], [0.0], 0.0)[0][1] > 0.0         # never an instant point


@pytest.mark.parametrize('velocity_limit, n', [(4.8, 5), (5.45, 6), (4.72, 5)])
def test_a_brisk_take_replays_in_its_recorded_time_without_exceeding_the_floor(
        velocity_limit, n):
    from physical_ai_server.workflow.handlers.trajectory import resegment_trajectory
    peak = 0.55 * velocity_limit                     # just under 0.6·limit
    w = 2 * math.pi * 0.3
    rows = []
    t = 0.0
    while t < 10.0:
        t = round(t + 0.04, 3)
        rows.append([round((peak / w) * math.sin(w * t + k), 4) for k in range(n)]
                    + [0.4, t])
    seg = resegment_trajectory(rows, 1.0, num_arm_joints=n, velocity_limit=velocity_limit)
    recorded = rows[-1][-1] - rows[0][-1]
    played = seg[-1][1] - seg[0][1]
    assert played == pytest.approx(recorded, rel=0.01)   # was up to +76 % on the OMX
    v_max = max(max(abs(b[0][j] - a[0][j]) for j in range(n)) / (b[1] - a[1])
                for a, b in zip(seg, seg[1:]))
    assert v_max <= 0.6 * velocity_limit * (1 + 1e-9)


def test_the_blockly_home_block_uses_the_floor_checked_planner(monkeypatch):
    from physical_ai_server.workflow import home_planner
    from physical_ai_server.workflow.handlers import motion as m
    calls, moves = [], []

    def _plan(ctx, a, b, d):
        calls.append((list(a), list(b)))
        via = list(a[:5]) + [a[5]]
        via[1] -= 0.2
        return [(list(a), via, 1.5), (via, list(b), d)]
    monkeypatch.setattr(home_planner, 'plan_floor_checked_home_route', _plan)
    monkeypatch.setattr(home_planner, 'plan_home_route',
                        lambda *a, **k: pytest.fail('the unchecked planner was used'))
    monkeypatch.setattr(m, 'safe_move', lambda ctx, a, b, d: moves.append((a, b)))
    start = [0.3, 0.2, 0.4, 0.9, 0.0, 0.8]
    ctx = types.SimpleNamespace(last_full_joints=list(start), last_arm_joints=start[:5],
                                num_arm_joints=5, home_joints_rad=None)
    m.home(ctx, {})
    assert len(calls) == 1 and len(moves) == 2         # the lift leg is DRIVEN
    assert ctx.last_full_joints[:5] == pytest.approx(list(m.HOME_JOINTS_RAD))
    assert ctx.last_full_joints[5] == 0.8               # gripper carried


def test_the_omx_point_model_ignores_the_touch_off_height():
    """The OMX touch-off z_table is the END-EFFECTOR frame's height with the fingers
    on the table — ~40 mm above the surface. Judging the finger/elbow samples
    against it would lift or refuse for a table that is lower. The decision must be
    identical calibrated or not."""
    from physical_ai_server.workflow import home_planner as hp
    from physical_ai_server.workflow.handlers import motion as m
    import random
    home = list(m.HOME_JOINTS_RAD) + [0.8]
    plain = _omx_ctx()
    calibrated = _omx_ctx()
    calibrated.z_table = 0.045
    calibrated.table_plane = (0.0, 0.0, 0.045)
    rng = random.Random(3)
    compared = 0
    for _ in range(3000):
        q = [rng.uniform(lo, hi) for lo, hi in plain.ik.joint_limits]
        if not 0.0 <= plain.ik.fk(q)[1][2] <= 0.08:
            continue
        outcomes = []
        for ctx in (plain, calibrated):
            try:
                outcomes.append(len(hp.plan_floor_checked_home_route(ctx, q + [0.8], home, 3.0)))
            except m.WorkflowError:
                outcomes.append('refused')
        assert outcomes[0] == outcomes[1]
        compared += 1
    assert compared > 200



def test_the_omx_lift_via_never_swaps_branch_or_turns_the_base():
    from physical_ai_server.workflow import home_planner as hp
    from physical_ai_server.workflow.handlers import motion as m
    import random
    assert max(abs(v) for v in hp._POINT_VIA_STEPS_RAD) == hp._POINT_VIA_MAX_STEP_RAD
    assert hp._POINT_VIA_MAX_STEP_RAD == 0.6
    ctx = _omx_ctx()
    home = list(m.HOME_JOINTS_RAD) + [0.8]
    rng = random.Random(21)
    lifts = 0
    for _ in range(20000):
        q = [rng.uniform(lo, hi) for lo, hi in ctx.ik.joint_limits]
        if not 0.0 <= ctx.ik.fk(q)[1][2] <= 0.06:
            continue
        try:
            legs = hp.plan_floor_checked_home_route(ctx, q + [0.8], home, 3.0)
        except m.WorkflowError:
            continue
        if len(legs) != 2:
            continue
        via = legs[0][1]
        assert via[0] == q[0] and via[4] == q[4]          # base + roll untouched
        assert max(abs(via[i] - q[i]) for i in (1, 2, 3)) <= 0.6 + 1e-9
        lifts += 1
        if lifts >= 40:
            break
    assert lifts >= 40


def test_a_lead_in_of_more_than_half_a_turn_on_a_full_circle_joint_is_refused():
    from physical_ai_server.workflow.handlers import trajectory as tr
    from physical_ai_server.workflow.handlers.motion import WorkflowError
    limits = _edu6_limits()
    first = [0, 0.7, -2.4, 0, 0.7, -3.0, 1.0, 0.0]
    with pytest.raises(WorkflowError) as e:
        tr.refuse_wrapped_lead_in([0, 0.7, -2.4, 0, 0.7, 3.0, 1.0], first, limits, 6)
    assert 'Gelenk 6' in str(e.value)
    tr.refuse_wrapped_lead_in([0, 0.7, -2.4, 0, 0.7, -2.0, 1.0], first, limits, 6)
    tr.refuse_wrapped_lead_in([0] * 7, first, None, 6)


def test_both_replay_paths_check_the_lead_in_against_the_seam():
    handler = Path(__import__('physical_ai_server.workflow.handlers.trajectory',
                              fromlist=['x']).__file__).read_text(encoding='utf-8')
    assert 'refuse_wrapped_lead_in(\n            lead_in, points[0]' in handler
    seg = _server_fn_source('workshop_replay_callback')
    i_def = seg.index('def _segment_from(pose):')
    assert 'refuse_wrapped_lead_in(pose, points[0]' in seg[i_def:i_def + 200]


def test_the_manual_notice_reaches_task_status_and_never_raises():
    class _Status:
        READY = 0

        def __init__(self):
            self.phase = None
            self.error = ''
    fns = _load(['_publish_manual_notice'], {**_G, 'TaskStatus': _Status})
    sent = []
    node = types.SimpleNamespace(
        communicator=types.SimpleNamespace(publish_status=lambda status: sent.append(status)),
        get_logger=lambda: _Logger())
    fns['_publish_manual_notice'](node, 'Wiedergabe abgebrochen.')
    assert sent and sent[0].error == 'Wiedergabe abgebrochen.' and sent[0].phase == 0

    def _boom(status):
        raise RuntimeError('publisher gone')
    node.communicator = types.SimpleNamespace(publish_status=_boom)
    fns['_publish_manual_notice'](node, 'x')                  # must not raise
    node.communicator = None
    fns['_publish_manual_notice'](node, 'x')

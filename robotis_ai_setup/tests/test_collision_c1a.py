#!/usr/bin/env python3
#
# C1-A (Aufnahme 2.0 round 5, owner-approved Rule §2 change of RECEIVING threads
# only): collision detection runs on the sensor-ingest thread, and the trip
# itself is handed to the main executor.
#
#   sensor thread  _process_gpio_states -> detector.update() under the detector
#                  lock -> on a trip with a live leader: under
#                  _record_publish_lock set _collision_trip_pending and bump
#                  _record_session_gen, then trigger the guard condition.
#                  From that instant no record tick adds a frame or publishes.
#   main executor  _on_trip_gc (default group, as all trip handling was
#                  before): _trigger_collision_stop once, then clear the latch.
#   watchdog       re-asserts True while active, does NOTHING while a trip is
#                  pending, asserts False only when neither — a pending trip can
#                  never be followed by a stray False.
#
# The detector, its gating, thresholds and the relax/home/resync recovery are
# unchanged (test_collision_monitor_contract.py keeps pinning them).
#
# collision_monitor.py imports rclpy/ROS message modules at module level; they
# are stubbed in sys.modules and the module is loaded under its own name (the
# test_collision_monitor_contract.py approach).

import importlib.util
import sys
import threading
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SAFETY_DIR = (
    REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server' / 'safety'
)


class _Bool:
    def __init__(self):
        self.data = False


class _TaskStatus:
    READY = 0
    COLLISION = 7

    def __init__(self):
        self.phase = 0
        self.current_task_instruction = ''
        self.error = ''


class _Duration:
    def __init__(self):
        self.sec = 0
        self.nanosec = 0


class _JointTrajectoryPoint:
    def __init__(self):
        self.positions = []
        self.velocities = []
        self.accelerations = []
        self.time_from_start = _Duration()


class _JointTrajectory:
    def __init__(self):
        self.joint_names = []
        self.points = []


def _install_stubs():
    def _stub(name, **attrs):
        mod = sys.modules.get(name) or types.ModuleType(name)
        sys.modules[name] = mod
        for k, v in attrs.items():
            setattr(mod, k, v)
        return mod

    _placeholder = type('_Placeholder', (), {})
    _stub('rclpy')
    _stub('rclpy.qos',
          QoSProfile=lambda **kw: kw,
          ReliabilityPolicy=types.SimpleNamespace(RELIABLE=1, BEST_EFFORT=2),
          DurabilityPolicy=types.SimpleNamespace(TRANSIENT_LOCAL=1, VOLATILE=2))
    _stub('sensor_msgs')
    _stub('sensor_msgs.msg', JointState=_placeholder)
    _stub('std_msgs')
    _stub('std_msgs.msg', Bool=_Bool)
    _stub('trajectory_msgs')
    _stub('trajectory_msgs.msg',
          JointTrajectory=_JointTrajectory, JointTrajectoryPoint=_JointTrajectoryPoint)
    _stub('physical_ai_interfaces')
    _stub('physical_ai_interfaces.msg', TaskStatus=_TaskStatus)
    _stub('control_msgs')
    _stub('control_msgs.msg', DynamicInterfaceGroupValues=_placeholder)
    pkg = _stub('physical_ai_server')
    safety_pkg = _stub('physical_ai_server.safety')
    pkg.safety = safety_pkg
    spec = importlib.util.spec_from_file_location(
        'physical_ai_server.safety.collision_detector', SAFETY_DIR / 'collision_detector.py')
    detector_mod = importlib.util.module_from_spec(spec)
    sys.modules['physical_ai_server.safety.collision_detector'] = detector_mod
    spec.loader.exec_module(detector_mod)
    safety_pkg.collision_detector = detector_mod


def _load_monitor_module():
    _install_stubs()
    spec = importlib.util.spec_from_file_location(
        '_edubotics_collision_monitor_c1a_test', SAFETY_DIR / 'collision_monitor.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CM = _load_monitor_module()


class _FakePublisher:
    def __init__(self, topic):
        self.topic = topic
        self.published = []

    def publish(self, msg):
        self.published.append(msg)


class _FakeTimer:
    def __init__(self, callback, group=None):
        self.callback = callback
        self.group = group
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _FakeGuardCondition:
    def __init__(self, callback, group=None):
        self.callback = callback
        self.group = group
        self.triggers = 0

    def trigger(self):
        self.triggers += 1


class _Logger:
    def __init__(self):
        self.lines = []

    def info(self, msg, *a, **k):
        self.lines.append(str(msg))

    warning = error = info


class _SensorNode:
    def __init__(self):
        self.subscriptions = []

    def create_subscription(self, msg_type, topic, callback, qos, **kwargs):
        self.subscriptions.append((topic, qos, kwargs))
        return object()


class _ObservedLock:
    """A real lock that records, per acquire/release, the host state the test
    cares about, so a test can prove WHAT happened inside it."""

    def __init__(self, host):
        self._lock = threading.Lock()
        self._host = host
        self.inside = []

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self.inside.append((self._host._collision_trip_pending,
                            self._host._record_session_gen,
                            getattr(self._host, 'on_recording', None)))
        self._lock.release()
        return False

    def acquire(self, *a, **k):
        return self._lock.acquire(*a, **k)

    def release(self):
        self._lock.release()

    def locked(self):
        return self._lock.locked()


class _DataManager:
    """DataManager double with the round-5 surface: a non-blocking collision
    discard and the recorder lock the resync helpers take."""

    def __init__(self):
        self.calls = []
        self.lock = threading.RLock()
        self.locked_during = []

    def request_collision_discard(self):
        self.calls.append('request_collision_discard')
        return True

    def re_record(self):
        self.calls.append('re_record')

    def locked(self):
        dm = self

        class _Ctx:
            def __enter__(self_inner):
                dm.lock.acquire()
                dm.calls.append('lock')
                return dm

            def __exit__(self_inner, *exc):
                dm.calls.append('unlock')
                dm.lock.release()
                return False

        return _Ctx()

    def cancel_pending_discard(self):
        self.calls.append('cancel_pending_discard')
        self.locked_during.append(self.lock._is_owned())
        return True

    def end_session_now(self):
        self.calls.append('end_session_now')
        self.locked_during.append(self.lock._is_owned())
        return True

    def get_current_record_status(self):
        st = _TaskStatus()
        st.phase = 4
        return st


class _Host(CM.CollisionMonitorMixin):
    """PhysicalAIServer stand-in with the round-5 surface: a sensor node, a
    guard-condition factory on the MAIN node and the record publish lock."""

    def __init__(self, *, guard=True, sensor=True, dm=None, group=True):
        self.publishers = []
        self.timers = []
        self.guards = []
        self.main_subscriptions = []
        self.on_inference = False
        self.on_recording = False
        self.on_workflow = False
        self.on_manual = False
        self.operation_mode = 'collection'
        self.start_recording_time = 0.0
        self._record_session_gen = 0
        self._record_publish_lock = _ObservedLock(self)
        self._collision_trip_pending = None
        self.timer_stops = 0
        self.timer_starts = []
        self.logger = _Logger()
        self._sensor_node = _SensorNode() if sensor else None
        if not guard:
            self.create_guard_condition = None
        if group:
            self._collision_cb_group = _COLLISION_GROUP
        self.data_manager = dm if dm is not None else _DataManager()
        self.timer_manager = types.SimpleNamespace(
            stop=lambda timer_name: setattr(self, 'timer_stops', self.timer_stops + 1),
            start=lambda timer_name: self.timer_starts.append(timer_name))
        self.communicator = types.SimpleNamespace(clear_latest_data=lambda: None)
        self._init_collision_monitor()

    def create_publisher(self, msg_type, topic, qos):
        pub = _FakePublisher(topic)
        self.publishers.append(pub)
        return pub

    def create_timer(self, period, callback, callback_group=None):
        timer = _FakeTimer(callback, callback_group)
        self.timers.append(timer)
        return timer

    def create_subscription(self, msg_type, topic, callback, qos, **kwargs):
        self.main_subscriptions.append(topic)
        return object()

    def create_guard_condition(self, callback, callback_group=None):
        gc = _FakeGuardCondition(callback, callback_group)
        self.guards.append(gc)
        return gc

    def get_logger(self):
        return self.logger

    def pub_for(self, topic):
        return next(p for p in self.publishers if p.topic == topic)


_COLLISION_GROUP = object()      # stands in for the node's own callback group


class _IV:
    def __init__(self, names, values):
        self.interface_names = list(names)
        self.values = list(values)


def _hard_press_msg():
    groups, ivs = [], []
    for gpio_name in ('dxl11', 'dxl12', 'dxl13', 'dxl14', 'dxl15'):
        groups.append(gpio_name)
        load = 900.0 if gpio_name == 'dxl11' else 0.0
        ivs.append(_IV(['Present Load', 'Hardware Error Status'], [load, 0.0]))
    return types.SimpleNamespace(interface_groups=groups, interface_values=ivs)


def _press_until_pending(host, limit=200):
    """Feed hard-press frames (live leader, still follower) until the detector's
    debounce trips and the sensor thread hands the trip over."""
    host._collision_follower_vel = {j: 0.0 for j in CM.ARM_JOINT_NAMES}
    host._collision_follower_pos = {j: 0.1 for j in CM.LEADER_JOINTS}
    for _ in range(limit):
        CM.time.monotonic  # noqa: B018 — the real clock; the debounce is tick-based
        host._leader_state_last_mono = CM.time.monotonic()
        host._process_gpio_states(_hard_press_msg())
        if host._collision_trip_pending is not None or host._collision_active:
            return
    raise AssertionError('the detector never tripped')


class SensorNodeWiringTest(unittest.TestCase):

    def test_the_three_subscriptions_live_on_the_sensor_node_with_depth_32(self):
        host = _Host()
        topics = [t for t, _qos, _kw in host._sensor_node.subscriptions]
        self.assertEqual(topics, [CM.JOINT_STATES_TOPIC, CM.LEADER_JOINT_STATES_TOPIC,
                                  CM.GPIO_STATES_DEFAULT_TOPIC])
        for _topic, qos, _kw in host._sensor_node.subscriptions:
            self.assertEqual(qos, CM.SENSOR_QOS_DEPTH)
        self.assertEqual(CM.SENSOR_QOS_DEPTH, 32)
        self.assertEqual(host.main_subscriptions, [])

    def test_the_guard_condition_lives_on_the_main_node(self):
        host = _Host()
        self.assertEqual(len(host.guards), 1)
        self.assertIs(host._trip_gc, host.guards[0])
        self.assertEqual(host.guards[0].callback, host._on_trip_gc)

    def test_without_a_sensor_node_the_main_node_subscribes(self):
        host = _Host(sensor=False)
        self.assertEqual(len(host.main_subscriptions), 3)

    def test_the_depth_is_the_communicators(self):
        communicator = (SAFETY_DIR.parent / 'communication' / 'communicator.py').read_text(
            encoding='utf-8')
        self.assertIn(f'\nSENSOR_QOS_DEPTH = {CM.SENSOR_QOS_DEPTH}\n', communicator)


class TripHandOverTest(unittest.TestCase):

    def _host(self, **kw):
        host = _Host(**kw)
        host.timers = [t for t in host.timers if t.callback != host._collision_watchdog_cb]
        return host

    def test_a_trip_on_the_sensor_thread_only_sets_the_latch(self):
        host = self._host()
        host.on_recording = True
        _press_until_pending(host)
        result = host._collision_trip_pending
        self.assertIsNotNone(result)
        self.assertTrue(result.tripped)
        # Nothing of the trip itself ran on the sensor thread.
        self.assertFalse(host._collision_active)
        self.assertTrue(host.on_recording)
        self.assertEqual(host.data_manager.calls, [])
        self.assertEqual(host.pub_for('/task/status').published, [])
        # The latch and the generation bump happened UNDER the publish lock,
        # then the guard condition fired.
        self.assertEqual(host._record_publish_lock.inside[-1][:2], (result, 1))
        self.assertEqual(host._record_session_gen, 1)
        self.assertEqual(host._trip_gc.triggers, 1)

    def test_on_trip_gc_trips_once_and_clears_the_latch(self):
        host = self._host()
        host.on_recording = True
        _press_until_pending(host)
        host._on_trip_gc()
        self.assertTrue(host._collision_active)
        self.assertIsNone(host._collision_trip_pending)
        self.assertTrue(host.pub_for(CM.COLLISION_FLAG_TOPIC).published[-1].data)
        self.assertEqual(host.pub_for('/task/status').published[-1].phase,
                         CM.PHASE_COLLISION)
        # The recording halted: on_recording flipped under the publish lock,
        # the timer stopped, the discard REQUESTED (never a blocking re_record).
        self.assertFalse(host.on_recording)
        self.assertFalse(host._record_publish_lock.inside[-1][2])
        self.assertEqual(host.timer_stops, 1)
        self.assertEqual(host.data_manager.calls, ['request_collision_discard'])
        statuses = len(host.pub_for('/task/status').published)
        host._on_trip_gc()                       # a stray second trigger: no-op
        self.assertEqual(len(host.pub_for('/task/status').published), statuses)
        self.assertEqual(host.data_manager.calls, ['request_collision_discard'])

    def test_a_pending_trip_blocks_further_detection(self):
        host = self._host()
        _press_until_pending(host)
        calls = {'update': 0}
        host._collision_detector.update = lambda *a, **k: calls.__setitem__(
            'update', calls['update'] + 1)
        host._process_gpio_states(_hard_press_msg())
        self.assertEqual(calls['update'], 0)
        self.assertEqual(host._trip_gc.triggers, 1)

    def test_an_already_active_collision_is_not_tripped_twice(self):
        host = self._host()
        _press_until_pending(host)
        host._collision_active = True
        host._on_trip_gc()
        self.assertIsNone(host._collision_trip_pending)
        self.assertEqual(host.pub_for('/task/status').published, [])

    def test_a_failing_trip_never_raises_into_the_executor_and_clears_the_latch(self):
        host = self._host()
        _press_until_pending(host)

        def _boom(result):
            raise RuntimeError('trip failed')

        host._trigger_collision_stop = _boom
        host._on_trip_gc()                       # must not raise
        self.assertIsNone(host._collision_trip_pending)
        self.assertTrue(any('trip failed' in line for line in host.logger.lines))

    def test_no_live_leader_drops_the_trip_on_the_sensor_thread(self):
        host = self._host()
        host._collision_follower_vel = {j: 0.0 for j in CM.ARM_JOINT_NAMES}
        host._leader_state_last_mono = None
        for _ in range(100):
            host._process_gpio_states(_hard_press_msg())
        self.assertIsNone(host._collision_trip_pending)
        self.assertEqual(host._trip_gc.triggers, 0)

    def test_a_host_without_guard_conditions_trips_directly(self):
        # Unit-test hosts (test_collision_monitor_contract.py) have no
        # create_guard_condition; the trip then runs inline, as before.
        host = self._host(guard=False)
        host.on_recording = True
        _press_until_pending(host)
        self.assertTrue(host._collision_active)
        self.assertIsNone(host._collision_trip_pending)

    def test_a_data_manager_without_the_request_falls_back_to_re_record(self):
        dm = types.SimpleNamespace(calls=[])
        dm.re_record = lambda: dm.calls.append('re_record')
        host = self._host(dm=dm)
        host.on_recording = True
        _press_until_pending(host)
        host._on_trip_gc()
        self.assertEqual(dm.calls, ['re_record'])


class WatchdogRaceTest(unittest.TestCase):

    def test_the_watchdog_publishes_nothing_while_a_trip_is_pending(self):
        host = _Host()
        _press_until_pending(host)
        flag = host.pub_for(CM.COLLISION_FLAG_TOPIC)
        before = len(flag.published)
        host._collision_watchdog_cb()
        self.assertEqual(len(flag.published), before)
        self.assertEqual(host.pub_for('/task/status').published, [])

    def test_true_while_active_false_only_when_neither(self):
        host = _Host()
        flag = host.pub_for(CM.COLLISION_FLAG_TOPIC)
        host._collision_watchdog_cb()
        self.assertFalse(flag.published[-1].data)
        _press_until_pending(host)
        host._on_trip_gc()
        host._collision_watchdog_cb()
        self.assertTrue(flag.published[-1].data)


class CollisionGroupTest(unittest.TestCase):
    """Round 6 (F2, owner-approved): the trip hand-over and the 5 Hz watchdog
    run in the node's OWN collision callback group — mutually exclusive with
    each other, no longer queued behind /task/command in the default group.
    Nothing else moves: relax, glide and resync timers stay in the default
    group."""

    def test_the_trip_hand_over_and_the_watchdog_share_the_collision_group(self):
        host = _Host()
        self.assertIs(host._trip_gc.group, _COLLISION_GROUP)
        watchdog = next(t for t in host.timers if t.callback == host._collision_watchdog_cb)
        self.assertIs(watchdog.group, _COLLISION_GROUP)

    def test_every_other_collision_timer_stays_in_the_default_group(self):
        host = _Host()
        host.timers = [t for t in host.timers if t.callback != host._collision_watchdog_cb]
        host.on_recording = True
        _press_until_pending(host)
        host._on_trip_gc()                              # schedules the relax
        host._collision_homed = True
        host._collision_leader_pos = {j: 0.0 for j in CM.LEADER_JOINTS}
        host._collision_follower_pos = dict(zip(CM.ARM_JOINT_NAMES, CM.SAFE_HOME_ARM))
        host._collision_follower_pos['gripper_joint_1'] = 0.0
        host._collision_leader_pos.update(dict(zip(CM.ARM_JOINT_NAMES, CM.SAFE_HOME_ARM)))
        host.resume_teleop()                            # schedules the resync
        self.assertTrue(host.timers)
        self.assertTrue(all(t.group is None for t in host.timers))

    def test_a_host_without_the_group_keeps_the_default_group(self):
        host = _Host(group=False)
        self.assertIsNone(host._trip_gc.group)
        watchdog = next(t for t in host.timers if t.callback == host._collision_watchdog_cb)
        self.assertIsNone(watchdog.group)

    def test_the_watchdog_true_never_lands_after_the_resyncs_false(self):
        # With the watchdog in its own group, the resync completion (default
        # group) can run beside it: a watchdog that read „active“ just before
        # the resync cleared it must not publish its True AFTER the resync's
        # False (the broadcaster would freeze until the next tick).
        host = _Host()
        host._collision_follower_pos = {j: 0.0 for j in CM.LEADER_JOINTS}
        _press_until_pending(host)
        host._on_trip_gc()
        flag = host.pub_for(CM.COLLISION_FLAG_TOPIC)
        in_publish, release = threading.Event(), threading.Event()
        original = host._publish_collision_flag

        def _slow_true(value):
            if value and threading.current_thread().name == 'watchdog':
                in_publish.set()
                release.wait(5)
            original(value)
        host._publish_collision_flag = _slow_true
        watchdog = threading.Thread(target=host._collision_watchdog_cb, name='watchdog')
        watchdog.start()
        self.assertTrue(in_publish.wait(5))
        resync = threading.Thread(target=host._on_resync_complete, name='resync')
        resync.start()
        resync.join(0.2)
        self.assertTrue(resync.is_alive())          # waits for the watchdog's publish
        release.set()
        watchdog.join(5)
        resync.join(5)
        self.assertFalse(host._collision_active)
        self.assertFalse(flag.published[-1].data)


class DetectorLockTest(unittest.TestCase):

    def test_update_and_every_reset_hold_the_detector_lock(self):
        host = _Host()
        held = []
        real_update = host._collision_detector.update
        real_reset = host._collision_detector.reset

        def _update(*a, **k):
            held.append(('update', host._collision_detector_lock.locked()))
            return real_update(*a, **k)

        def _reset():
            held.append(('reset', host._collision_detector_lock.locked()))
            return real_reset()

        host._collision_detector.update = _update
        host._collision_detector.reset = _reset
        host._collision_follower_vel = {j: 0.0 for j in CM.ARM_JOINT_NAMES}
        host._process_gpio_states(_hard_press_msg())           # update
        host.on_manual = True
        host._process_gpio_states(_hard_press_msg())           # gated reset
        host.on_manual = False
        host._collision_settle_until_mono = CM.time.monotonic() + 10.0
        host._process_gpio_states(_hard_press_msg())           # settle reset
        host._detector_reset()                                 # the main-thread helper
        self.assertEqual([k for k, _ in held], ['update', 'reset', 'reset', 'reset'])
        self.assertTrue(all(locked for _, locked in held))

    def test_the_resync_completion_resets_through_the_lock(self):
        source = (SAFETY_DIR / 'collision_monitor.py').read_text(encoding='utf-8')
        # Every reset goes through the one helper (sensor-thread gates and the
        # main-thread resync alike); only the helper touches reset() itself.
        self.assertEqual(source.count('self._collision_detector.reset()'), 1)
        self.assertEqual(source.count('self._collision_detector.update('), 1)


class ResyncUnderTheRecorderLockTest(unittest.TestCase):

    def _interrupted_host(self, forced=False):
        host = _Host()
        host.timers = [t for t in host.timers if t.callback != host._collision_watchdog_cb]
        host.on_recording = True
        _press_until_pending(host)
        host._on_trip_gc()
        host._collision_follower_pos = {j: 0.0 for j in CM.LEADER_JOINTS}
        host._collision_leader_pos = {j: 0.0 for j in CM.LEADER_JOINTS}
        if forced:
            host._collision_end_recording = True
            host._collision_interrupted_recording = False
        return host

    def test_the_resume_cancel_runs_under_the_recorder_lock_then_on_recording_flips_locked(self):
        host = self._interrupted_host()
        host._on_resync_complete()
        dm = host.data_manager
        self.assertIn('cancel_pending_discard', dm.calls)
        self.assertEqual(dm.locked_during, [True])
        self.assertTrue(host.on_recording)
        self.assertTrue(host._record_publish_lock.inside[-1][2])
        self.assertEqual(host.timer_starts, ['collection'])

    def test_the_forced_end_runs_under_the_recorder_lock(self):
        host = self._interrupted_host(forced=True)
        host._on_resync_complete()
        self.assertEqual(host.data_manager.locked_during, [True])
        self.assertIn('end_session_now', host.data_manager.calls)

    def test_a_data_manager_without_locked_still_works(self):
        dm = types.SimpleNamespace(calls=[])
        dm.re_record = lambda: dm.calls.append('re_record')
        dm.cancel_pending_discard = lambda: dm.calls.append('cancel') or True
        host = _Host(dm=dm)
        host.on_recording = True
        _press_until_pending(host)
        host._on_trip_gc()
        host._on_resync_complete()
        self.assertEqual(dm.calls, ['re_record', 'cancel'])


if __name__ == '__main__':
    unittest.main()

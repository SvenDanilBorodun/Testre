#!/usr/bin/env python3
#
# Contract tests for the teleop collision e-stop ORCHESTRATION (safety/collision_monitor.py)
# — the student-paced two-step recovery state machine. These lock the safety-critical
# ordering at the unit level:
#
#   1. TRIP: /collision_flag=True is published BEFORE any motion command; the arm RELAXES IN
#      PLACE (hold at the measured pose) and does NOT auto-home — the student must be able to
#      remove the obstacle before the arm moves again.
#   2. HOME_FOLLOWER: refused with no active collision; runs the VERIFIED safe-home glide;
#      a glide that never arrives reports failure and falls back to step 1.
#   3. RESUME_TELEOP: STRICT ordering — refused until the follower verifiably reached home;
#      then refused while the leader is far from home; the freeze clears only after resync.
#   4. A trip mid-recording discards the in-progress episode (re_record) and halts capture.
#
# collision_monitor.py imports rclpy/ROS message modules at module level; we stub those in
# sys.modules (same approach as test_data_manager_finalize.py) and pre-load the REAL pure
# collision_detector under its package name, then drive the monitor's timers by hand.

import importlib.util
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SAFETY_DIR = (
    REPO_ROOT / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server' / 'safety'
)


# ---- ROS stand-ins ----------------------------------------------------------------------

class _Bool:
    def __init__(self):
        self.data = False


class _TaskStatus:
    READY = 0
    COLLISION = 7
    # COLLISION_HOMING / COLLISION_HOMED intentionally absent: mirrors a container whose
    # compiled interfaces predate the constants — the monitor's getattr fallback must still
    # publish wire values 8 / 9.

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

    # Pre-load the REAL pure detector under its package name so the monitor's absolute
    # import resolves to the actual implementation (it is rclpy-free).
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
        '_edubotics_collision_monitor_test', SAFETY_DIR / 'collision_monitor.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CM = _load_monitor_module()


# ---- fake node host ----------------------------------------------------------------------

class _FakePublisher:
    def __init__(self, msg_type, topic):
        self.msg_type = msg_type
        self.topic = topic
        self.published = []

    def publish(self, msg):
        self.published.append(msg)


class _FakeTimer:
    def __init__(self, callback):
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _FakeLogger:
    def info(self, *a, **k):
        pass

    warning = info
    error = info


class _Host(CM.CollisionMonitorMixin):
    """Minimal PhysicalAIServer stand-in: just the Node surface the mixin touches."""

    def __init__(self):
        self.publishers = []
        self.timers = []          # chronological create_timer order
        self.on_inference = False
        self.on_recording = False
        self.operation_mode = 'collection'
        self.start_recording_time = 0.0
        self.re_record_calls = 0
        self.timer_stop_calls = 0
        self.timer_start_calls = []      # timer_name per start()
        self.clear_latest_data_calls = 0
        host = self
        self.data_manager = types.SimpleNamespace(
            re_record=lambda: setattr(host, 're_record_calls', host.re_record_calls + 1))
        self.timer_manager = types.SimpleNamespace(
            stop=lambda timer_name: setattr(
                host, 'timer_stop_calls', host.timer_stop_calls + 1),
            start=lambda timer_name: host.timer_start_calls.append(timer_name))
        self.communicator = types.SimpleNamespace(
            clear_latest_data=lambda: setattr(
                host, 'clear_latest_data_calls', host.clear_latest_data_calls + 1))
        self._init_collision_monitor()

    # Node surface ---------------------------------------------------------------
    def create_publisher(self, msg_type, topic, qos):
        pub = _FakePublisher(msg_type, topic)
        self.publishers.append(pub)
        return pub

    def create_timer(self, period, callback):
        timer = _FakeTimer(callback)
        self.timers.append(timer)
        return timer

    def create_subscription(self, *a, **k):
        return None

    def create_client(self, *a, **k):
        raise AssertionError('no service client expected in these tests')

    def get_logger(self):
        return _FakeLogger()

    # helpers --------------------------------------------------------------------
    def pub_for(self, topic):
        return next(p for p in self.publishers if p.topic == topic)

    def fire_pending_timers(self):
        """Run every not-yet-cancelled timer callback exactly once (chronological)."""
        for timer in list(self.timers):
            if not timer.cancelled:
                timer.cancel()
                timer.callback()


HOME = dict(zip(CM.ARM_JOINT_NAMES, CM.SAFE_HOME_ARM))
CONTACT_POSE = {'joint1': 0.5, 'joint2': 0.3, 'joint3': 0.2, 'joint4': 0.1,
                'joint5': 0.0, 'gripper_joint_1': 0.4}


def _trip(host):
    result = CM.CollisionMonitorMixin  # noqa: F841 - readability
    host._trigger_collision_stop(types.SimpleNamespace(
        tripped=True, joints=['joint1'], latched_overload=[],
        reason='joint1: over-force (test)'))


class TripContractTest(unittest.TestCase):

    def _host(self):
        host = _Host()
        host.timers = [t for t in host.timers if t.callback != host._collision_watchdog_cb]
        host._collision_follower_pos = dict(CONTACT_POSE)
        host._collision_follower_vel = {j: 0.0 for j in CM.ARM_JOINT_NAMES}
        return host

    def test_trip_freezes_first_and_relaxes_in_place_no_autohome(self):
        host = self._host()
        rail = host.pub_for(CM.LEADER_TRAJECTORY_TOPIC)
        flag = host.pub_for(CM.COLLISION_FLAG_TOPIC)

        _trip(host)
        # Flag raised BEFORE any motion: the rail is still silent at trip time (the relax is
        # deferred so the frozen broadcaster's in-flight messages can drain).
        self.assertTrue(flag.published[-1].data)
        self.assertEqual(rail.published, [])
        # Status surfaces step 1 (phase=COLLISION).
        status = host.pub_for('/task/status').published[-1]
        self.assertEqual(status.phase, CM.PHASE_COLLISION)

        # The deferred relax holds the CURRENT measured pose — it must NOT command home.
        host.fire_pending_timers()
        self.assertEqual(len(rail.published), 1)
        traj = rail.published[-1]
        self.assertEqual(len(traj.points), 1)
        held = dict(zip(traj.joint_names, traj.points[0].positions))
        for joint in CM.LEADER_JOINTS:
            self.assertAlmostEqual(held[joint], CONTACT_POSE[joint])
        self.assertNotAlmostEqual(held['joint2'], HOME['joint2'])

    def test_trip_mid_recording_discards_episode_and_halts_capture(self):
        host = self._host()
        host.on_recording = True
        _trip(host)
        self.assertEqual(host.re_record_calls, 1)
        self.assertEqual(host.timer_stop_calls, 1)
        self.assertFalse(host.on_recording)
        # The interrupted recording is remembered so RESUME_TELEOP can continue the SAME
        # session (the mode is captured for the timer restart).
        self.assertTrue(host._collision_interrupted_recording)
        self.assertEqual(host._collision_interrupted_mode, 'collection')

    def test_trip_during_free_teleop_does_not_arm_resume(self):
        # No recording in flight (free teleop): a trip must NOT mark a recording to resume,
        # so RESUME_TELEOP later publishes READY rather than re-arming a phantom session.
        host = self._host()
        host.on_recording = False
        _trip(host)
        self.assertEqual(host.re_record_calls, 0)
        self.assertEqual(host.timer_stop_calls, 0)
        self.assertFalse(host._collision_interrupted_recording)

    def test_trip_with_incomplete_pose_skips_hold_never_commands_zeros(self):
        host = self._host()
        host._collision_follower_pos = {'joint1': 0.5}  # joint2..gripper unknown
        rail = host.pub_for(CM.LEADER_TRAJECTORY_TOPIC)
        _trip(host)
        host.fire_pending_timers()
        self.assertEqual(rail.published, [])  # skipped — a 0.0 default would slam the arm


class HomeFollowerContractTest(unittest.TestCase):

    def _tripped_host(self):
        host = _Host()
        host.timers = [t for t in host.timers if t.callback != host._collision_watchdog_cb]
        host._collision_follower_pos = dict(CONTACT_POSE)
        _trip(host)
        host.fire_pending_timers()  # deliver the relax sends
        return host

    def test_home_refused_without_active_collision(self):
        host = _Host()
        success, _ = host.home_follower()
        self.assertFalse(success)

    def test_home_runs_verified_glide_to_safe_home(self):
        host = self._tripped_host()
        rail = host.pub_for(CM.LEADER_TRAJECTORY_TOPIC)
        sends_before = len(rail.published)

        success, _ = host.home_follower()
        self.assertTrue(success)
        self.assertEqual(host.pub_for('/task/status').published[-1].phase,
                         CM.PHASE_COLLISION_HOMING)

        host.fire_pending_timers()  # glide send
        glide = rail.published[-1]
        self.assertGreater(len(rail.published), sends_before)
        end = dict(zip(glide.joint_names, glide.points[-1].positions))
        for idx, joint in enumerate(CM.ARM_JOINT_NAMES):
            self.assertAlmostEqual(end[joint], CM.SAFE_HOME_ARM[idx])
        # Gripper held at its current position (don't drop a grasped object).
        self.assertAlmostEqual(end['gripper_joint_1'], CONTACT_POSE['gripper_joint_1'])

        # Arrival: follower reaches home -> verified homed, phase advances to step 2.
        host._collision_follower_pos = {**HOME, 'gripper_joint_1': 0.4}
        host.fire_pending_timers()  # verify tick sees dist <= tol
        self.assertTrue(host._collision_homed)
        self.assertEqual(host.pub_for('/task/status').published[-1].phase,
                         CM.PHASE_COLLISION_HOMED)

    def test_stalled_glide_resends_then_reports_failure_back_to_step1(self):
        host = self._tripped_host()
        rail = host.pub_for(CM.LEADER_TRAJECTORY_TOPIC)
        host.home_follower()
        # The follower never moves (obstacle still there): each verify tick re-sends until
        # GLIDE_MAX_SENDS, then homing fails back to step 1 (phase=COLLISION + retry hint).
        for _ in range(CM.GLIDE_MAX_SENDS + 1):
            host.fire_pending_timers()
        glide_sends = [t for t in rail.published if len(t.points) > 1]
        self.assertEqual(len(glide_sends), CM.GLIDE_MAX_SENDS)
        self.assertFalse(host._collision_homed)
        self.assertFalse(host._collision_homing)
        status = host.pub_for('/task/status').published[-1]
        self.assertEqual(status.phase, CM.PHASE_COLLISION)
        self.assertEqual(status.current_task_instruction,
                         CM.COLLISION_HOME_FAILED_MESSAGE_DE)
        # The freeze must survive a failed homing.
        self.assertTrue(host.pub_for(CM.COLLISION_FLAG_TOPIC).published[-1].data)


class ResumeContractTest(unittest.TestCase):

    def _homed_host(self):
        host = _Host()
        host.timers = [t for t in host.timers if t.callback != host._collision_watchdog_cb]
        host._collision_follower_pos = dict(CONTACT_POSE)
        _trip(host)
        host.fire_pending_timers()
        host.home_follower()
        host._collision_follower_pos = {**HOME, 'gripper_joint_1': 0.4}
        host.fire_pending_timers()
        host.fire_pending_timers()
        assert host._collision_homed
        return host

    def test_resume_strictly_refused_until_homed(self):
        host = _Host()
        host.timers = [t for t in host.timers if t.callback != host._collision_watchdog_cb]
        host._collision_follower_pos = dict(CONTACT_POSE)
        host._collision_leader_pos = {**HOME}
        _trip(host)
        success, message = host.resume_teleop()
        self.assertFalse(success)
        self.assertIn('Grundstellung', message)
        self.assertTrue(host._collision_active)

    def test_resume_refused_while_leader_far_from_home(self):
        host = self._homed_host()
        host._collision_leader_pos = {**HOME, 'joint2': HOME['joint2'] + 1.0}
        success, _ = host.resume_teleop()
        self.assertFalse(success)
        self.assertTrue(host._collision_active)

    def test_resume_resyncs_then_clears_flag_only_after_completion(self):
        host = self._homed_host()
        host._collision_leader_pos = {**HOME, 'gripper_joint_1': 0.4}
        flag = host.pub_for(CM.COLLISION_FLAG_TOPIC)

        success, _ = host.resume_teleop()
        self.assertTrue(success)
        # Still frozen until the resync trajectory completes.
        self.assertTrue(host._collision_active)
        self.assertTrue(flag.published[-1].data)

        host.fire_pending_timers()  # resync completion
        self.assertFalse(host._collision_active)
        self.assertFalse(host._collision_homed)
        self.assertFalse(flag.published[-1].data)
        self.assertEqual(host.pub_for('/task/status').published[-1].phase, _TaskStatus.READY)
        # Free teleop (no recording was interrupted): resume must NOT re-arm a recording.
        self.assertEqual(host.timer_start_calls, [])
        self.assertEqual(host.clear_latest_data_calls, 0)

    def test_getattr_fallback_wire_values_match_react(self):
        # The stub TaskStatus has no COLLISION_HOMING/HOMED constants (pre-rebuild container);
        # the monitor must still publish the raw ints React's taskPhases.js compares against.
        self.assertEqual(CM.PHASE_COLLISION, 7)
        self.assertEqual(CM.PHASE_COLLISION_HOMING, 8)
        self.assertEqual(CM.PHASE_COLLISION_HOMED, 9)


class ResumeRecordingContractTest(unittest.TestCase):
    """A collision that interrupts a recording must SEAMLESSLY resume the SAME session on
    RESUME_TELEOP (EduBotics 2026-06-13): the kept DataManager re-records the interrupted
    episode after the Rücksetzzeit, prior episodes intact — the student loses nothing and
    does not restart. Locks: timer restarted with the captured mode, stale caches cleared,
    the topic-timeout window reset, and NO READY published (the resumed recording timer owns
    /task/status; a READY would flicker the React UI back to 'Bereit')."""

    def _recording_homed_host(self):
        host = _Host()
        host.timers = [t for t in host.timers if t.callback != host._collision_watchdog_cb]
        host._collision_follower_pos = dict(CONTACT_POSE)
        host.on_recording = True            # a recording is in flight at trip time
        _trip(host)
        host.fire_pending_timers()          # relax sends
        host.home_follower()
        host._collision_follower_pos = {**HOME, 'gripper_joint_1': 0.4}
        host.fire_pending_timers()          # glide send
        host.fire_pending_timers()          # verify -> homed
        assert host._collision_homed
        # The collision discarded the in-progress episode and halted capture.
        self.assertEqual(host.re_record_calls, 1)
        self.assertFalse(host.on_recording)
        self.assertTrue(host._collision_interrupted_recording)
        return host

    def test_resume_rearms_the_same_recording_session(self):
        host = self._recording_homed_host()
        host._collision_leader_pos = {**HOME, 'gripper_joint_1': 0.4}
        status_pub = host.pub_for('/task/status')

        success, _ = host.resume_teleop()
        self.assertTrue(success)
        # Still frozen until the resync trajectory completes.
        self.assertTrue(host._collision_active)
        self.assertFalse(host.on_recording)

        host.fire_pending_timers()          # resync completion -> _on_resync_complete

        # Collision cleared + flag released.
        self.assertFalse(host._collision_active)
        self.assertFalse(host.pub_for(CM.COLLISION_FLAG_TOPIC).published[-1].data)
        # Recording RE-ARMED on the SAME session (DataManager never rebuilt -> prior episodes
        # + dataset kept; re_record() was NOT called again, so the interrupted episode index
        # is preserved and re-recorded after the Rücksetzzeit).
        self.assertTrue(host.on_recording)
        self.assertEqual(host.timer_start_calls, ['collection'])
        self.assertEqual(host.re_record_calls, 1)        # not re-discarded on resume
        # Stale camera/follower/leader caches from the multi-second glide/resync are dropped.
        self.assertEqual(host.clear_latest_data_calls, 1)
        # The topic-timeout window is restarted so the first resumed tick can't insta-timeout.
        self.assertGreater(host.start_recording_time, 0.0)
        # The interrupted-recording marker is consumed.
        self.assertFalse(host._collision_interrupted_recording)
        # CRITICAL: no READY is published on resume — the resumed recording timer owns
        # /task/status. The last status stays at COLLISION_HOMED until the recording publishes
        # RESETTING (which closes the React modal as the first non-collision tick).
        self.assertNotEqual(status_pub.published[-1].phase, _TaskStatus.READY)
        self.assertEqual(status_pub.published[-1].phase, CM.PHASE_COLLISION_HOMED)

    def test_resume_falls_back_to_ready_when_resume_helper_fails(self):
        # If the recording objects vanished (a teardown raced the resync), resume must not
        # crash and must fall back to the plain cleared READY status.
        host = self._recording_homed_host()
        host._collision_leader_pos = {**HOME, 'gripper_joint_1': 0.4}
        host.data_manager = None            # simulate a torn-down session
        status_pub = host.pub_for('/task/status')

        success, _ = host.resume_teleop()
        self.assertTrue(success)
        host.fire_pending_timers()          # resync completion

        self.assertFalse(host._collision_active)
        self.assertFalse(host.on_recording)
        self.assertEqual(host.timer_start_calls, [])
        self.assertFalse(host._collision_interrupted_recording)
        self.assertEqual(status_pub.published[-1].phase, _TaskStatus.READY)


class JointDistTelemetryContractTest(unittest.TestCase):
    """Per-joint homing telemetry on /task/status (leLab-comparison PR-2).

    The module-level _TaskStatus stub deliberately LACKS joint_dist_to_home —
    mirroring a container whose compiled interfaces predate the field. The
    publish must keep flowing (the safety status is load-bearing) and simply
    omit the telemetry; a field-bearing TaskStatus gets the 5 floats.
    """

    def test_stale_interface_publish_survives_without_field(self):
        host = _Host()
        host._collision_follower_pos = dict(CONTACT_POSE)
        host._publish_collision_status()
        status = host.pub_for('/task/status').published[-1]
        self.assertFalse(hasattr(status, 'joint_dist_to_home'),
                         'stale-interface publish must not grow the field')
        self.assertEqual(status.phase, CM.PHASE_COLLISION)

    def test_fresh_interface_carries_per_joint_distances(self):
        class _TaskStatusWithField(_TaskStatus):
            def __init__(self):
                super().__init__()
                self.joint_dist_to_home = []

        original = CM.TaskStatus
        CM.TaskStatus = _TaskStatusWithField
        try:
            host = _Host()
            host._collision_follower_pos = dict(CONTACT_POSE)
            host._publish_collision_status()
            status = host.pub_for('/task/status').published[-1]
            self.assertEqual(len(status.joint_dist_to_home), 5)
            for i, joint in enumerate(CM.ARM_JOINT_NAMES):
                self.assertAlmostEqual(
                    status.joint_dist_to_home[i],
                    abs(CONTACT_POSE[joint] - CM.SAFE_HOME_ARM[i]),
                    places=6,
                )
            self.assertTrue(
                all(isinstance(d, float) for d in status.joint_dist_to_home))
        finally:
            CM.TaskStatus = original

    def test_per_joint_and_max_agree(self):
        host = _Host()
        host._collision_follower_pos = dict(CONTACT_POSE)
        per_joint = host._dist_to_home_per_joint()
        self.assertEqual(len(per_joint), 5)
        self.assertAlmostEqual(max(per_joint), host._dist_to_home(), places=9)

    def test_no_joint_sample_yields_none_and_clean_publish(self):
        host = _Host()
        host._collision_follower_pos = {}
        self.assertIsNone(host._dist_to_home_per_joint())
        self.assertIsNone(host._dist_to_home())
        host._publish_collision_status()  # must not raise without a sample
        self.assertEqual(
            host.pub_for('/task/status').published[-1].phase,
            CM.PHASE_COLLISION)


class GpioCallbackSafetyTest(unittest.TestCase):
    """_gpio_states_cb runs continuously on the executor; a malformed/edge-case
    Dynamixel gpio message must NEVER raise out of it — an unhandled exception
    would propagate out of MultiThreadedExecutor.spin() and kill the node (main()
    only catches KeyboardInterrupt), leaving the React app "Getrennt". The guard
    wraps the parsing in try/except (fix 2026-06-13)."""

    def _host(self):
        host = _Host()
        host._collision_follower_vel = {j: 0.0 for j in CM.ARM_JOINT_NAMES}
        return host

    def test_interface_values_none_does_not_crash(self):
        host = self._host()
        # len(None) inside _process_gpio_states would raise TypeError.
        bad = types.SimpleNamespace(interface_groups=['dxl11'], interface_values=None)
        try:
            host._gpio_states_cb(bad)
        except Exception as exc:  # noqa: BLE001
            self.fail(f'_gpio_states_cb leaked an exception: {exc!r}')
        self.assertFalse(host._collision_active)

    def test_entry_missing_fields_does_not_crash(self):
        host = self._host()
        # An interface_values entry lacking .interface_names/.values raises
        # AttributeError in dict(zip(...)); must be swallowed by the guard.
        bad = types.SimpleNamespace(
            interface_groups=['dxl11'],
            interface_values=[types.SimpleNamespace()])
        try:
            host._gpio_states_cb(bad)
        except Exception as exc:  # noqa: BLE001
            self.fail(f'_gpio_states_cb leaked an exception: {exc!r}')
        self.assertFalse(host._collision_active)

    def test_wellformed_low_effort_processes_without_tripping(self):
        host = self._host()
        iv = types.SimpleNamespace(
            interface_names=['Present Load', 'Hardware Error Status'],
            values=[0.0, 0.0])
        good = types.SimpleNamespace(
            interface_groups=['dxl11'], interface_values=[iv])
        host._gpio_states_cb(good)  # exercises the real parse path, no raise
        self.assertFalse(host._collision_active)


class StateCallbackSafetyTest(unittest.TestCase):
    """The /joint_states + /leader/joint_states subscriptions run continuously on
    the executor — including ACROSS the open_manipulator container blip when the
    Roboter-Studio leader toggle recreates that container (`up --force-recreate
    --no-deps`). A malformed JointState (e.g. msg.name=None during the freshly-
    recreated controller's startup transient) must NEVER raise out of the
    callback: an unhandled exception would propagate out of
    MultiThreadedExecutor.spin() and kill the node (main() only catches
    KeyboardInterrupt), dropping the React rosbridge connection and losing the
    in-RAM robot_type → the student is forced to re-select the robot. The guards
    wrap the parse in try/except (fix 2026-06-28), mirroring _gpio_states_cb."""

    def test_follower_state_malformed_does_not_crash(self):
        host = _Host()
        bad = types.SimpleNamespace(name=None, position=[], velocity=[])
        try:
            host._collision_follower_state_cb(bad)
        except Exception as exc:  # noqa: BLE001
            self.fail(f'_collision_follower_state_cb leaked an exception: {exc!r}')

    def test_leader_state_malformed_does_not_crash_and_records_no_arrival(self):
        host = _Host()
        bad = types.SimpleNamespace(name=None, position=[])
        try:
            host._collision_leader_state_cb(bad)
        except Exception as exc:  # noqa: BLE001
            self.fail(f'_collision_leader_state_cb leaked an exception: {exc!r}')
        # A malformed leader tick must not register a (false) leader-active
        # arrival — leader_appears_active would otherwise wrongly gate workflows.
        self.assertIsNone(host._leader_state_last_mono)

    def test_follower_state_wellformed_populates_pose_and_velocity(self):
        host = _Host()
        good = types.SimpleNamespace(
            name=['joint1', 'joint2'], position=[0.1, 0.2], velocity=[0.0, 0.5])
        host._collision_follower_state_cb(good)
        self.assertAlmostEqual(host._collision_follower_pos['joint1'], 0.1)
        self.assertAlmostEqual(host._collision_follower_vel['joint2'], 0.5)

    def test_leader_state_wellformed_records_arrival(self):
        host = _Host()
        good = types.SimpleNamespace(name=['joint1', 'joint2'], position=[0.3, 0.4])
        host._collision_leader_state_cb(good)
        self.assertAlmostEqual(host._collision_leader_pos['joint1'], 0.3)
        self.assertIsNotNone(host._leader_state_last_mono)


class WatchdogSelfHealContractTest(unittest.TestCase):
    """The freeze latch must SELF-HEAL (issue #19 finding #4). The leader broadcaster
    (om_joint_trajectory_command_broadcaster) is a VOLATILE subscriber that never self-clears
    collision_detected_ and only un-freezes on an explicit /collision_flag=False; this node
    carries respawn=True, so a True flag latched before a respawn would freeze the follower
    forever with NO active collision to recover from. The node clears the flag at init and the
    idle watchdog re-asserts False every tick; while a collision is active the watchdog still
    asserts True (and the banner)."""

    def test_init_clears_the_freeze_flag(self):
        host = _Host()
        flag = host.pub_for(CM.COLLISION_FLAG_TOPIC)
        # _init_collision_monitor publishes exactly one False (no trip, watchdog not fired yet).
        self.assertGreaterEqual(len(flag.published), 1)
        self.assertFalse(flag.published[-1].data)
        # And it must NOT spuriously open the modal: no collision status at init.
        self.assertEqual(host.pub_for('/task/status').published, [])

    def test_watchdog_asserts_false_while_idle(self):
        host = _Host()
        flag = host.pub_for(CM.COLLISION_FLAG_TOPIC)
        self.assertFalse(host._collision_active)
        before = len(flag.published)
        host._collision_watchdog_cb()
        self.assertGreater(len(flag.published), before)
        self.assertFalse(flag.published[-1].data)
        # The idle watchdog must not publish a collision banner (modal stays closed).
        self.assertEqual(host.pub_for('/task/status').published, [])

    def test_watchdog_asserts_true_while_active(self):
        host = _Host()
        host._collision_follower_pos = dict(CONTACT_POSE)
        _trip(host)
        flag = host.pub_for(CM.COLLISION_FLAG_TOPIC)
        host._collision_watchdog_cb()
        self.assertTrue(flag.published[-1].data)
        self.assertEqual(host.pub_for('/task/status').published[-1].phase, CM.PHASE_COLLISION)

    def test_trigger_guard_swallows_flag_publish_failure_but_still_relaxes(self):
        # issue #19 finding #1: a raise while publishing the freeze flag must not skip the
        # relax scheduling or the status publish (the gpio try/except would otherwise swallow
        # it and strand _collision_active with no relax + no modal).
        host = _Host()
        host.timers = [t for t in host.timers if t.callback != host._collision_watchdog_cb]
        host._collision_follower_pos = dict(CONTACT_POSE)
        flag = host.pub_for(CM.COLLISION_FLAG_TOPIC)
        original_publish = flag.publish
        calls = {'n': 0}

        def _boom(msg):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RuntimeError('simulated DDS publish failure')
            return original_publish(msg)

        flag.publish = _boom
        _trip(host)  # must not raise
        # The relax was still scheduled despite the flag-publish failure.
        rail = host.pub_for(CM.LEADER_TRAJECTORY_TOPIC)
        host.fire_pending_timers()
        self.assertEqual(len(rail.published), 1)
        # And the modal status was still published.
        self.assertEqual(host.pub_for('/task/status').published[-1].phase, CM.PHASE_COLLISION)


class ForceResumeContractTest(unittest.TestCase):
    """FORCE_RESUME_TELEOP escape hatch (issue #19 finding #4): when the verified safe-home
    glide cannot complete (e.g. a degraded bus), homing can never reach step 2 and the freeze
    would otherwise wedge everything until an environment restart. The student forces a
    CONTROLLED, proximity-gated resync from the follower's CURRENT pose to the leader and the
    freeze clears. Unlike resume_teleop it does NOT require the follower to have homed, its
    proximity gate references the follower's CURRENT pose (not home), and it does NOT
    auto-resume an interrupted recording."""

    def _tripped_host(self, recording=False):
        host = _Host()
        host.timers = [t for t in host.timers if t.callback != host._collision_watchdog_cb]
        host._collision_follower_pos = dict(CONTACT_POSE)
        host.on_recording = recording
        _trip(host)
        return host

    def test_force_resume_noop_without_active_collision(self):
        host = _Host()
        success, _ = host.force_resume_teleop()
        self.assertTrue(success)  # nothing to recover

    def test_force_resume_refused_when_leader_far_from_current_follower(self):
        host = self._tripped_host()
        # Leader sitting AT home while the follower's CURRENT pose is the contact pose: a big
        # move. This is exactly the case resume_teleop's home-referenced gate WOULD allow and
        # force_resume must REFUSE (it gates on the follower's current pose, not home).
        host._collision_leader_pos = {**HOME}
        success, _ = host.force_resume_teleop()
        self.assertFalse(success)
        self.assertTrue(host._collision_active)

    def test_force_resume_resyncs_from_current_then_clears(self):
        host = self._tripped_host()
        host._collision_leader_pos = {**CONTACT_POSE}  # leader near the follower's current pose
        rail = host.pub_for(CM.LEADER_TRAJECTORY_TOPIC)
        flag = host.pub_for(CM.COLLISION_FLAG_TOPIC)

        success, _ = host.force_resume_teleop()
        self.assertTrue(success)
        # A controlled multi-point resync is published; still frozen until it completes.
        self.assertTrue(any(len(t.points) > 1 for t in rail.published))
        self.assertTrue(host._collision_active)
        self.assertTrue(flag.published[-1].data)

        host.fire_pending_timers()  # resync completion -> _on_resync_complete
        self.assertFalse(host._collision_active)
        self.assertFalse(flag.published[-1].data)
        self.assertEqual(host.pub_for('/task/status').published[-1].phase, _TaskStatus.READY)

    def test_force_resume_blocks_competing_home_glide_during_resync(self):
        # The force-resume button keeps phase=COLLISION (stage 'stopped') during the multi-second
        # resync, so the step-1 "Follower in Grundstellung" button stays visible. A re-click must
        # be refused at the source of truth so no competing home glide fights the resync on the
        # shared /leader/joint_trajectory rail.
        host = self._tripped_host()
        host._collision_leader_pos = {**CONTACT_POSE}
        rail = host.pub_for(CM.LEADER_TRAJECTORY_TOPIC)
        self.assertTrue(host.force_resume_teleop()[0])
        rail_after_resync = len(rail.published)

        ok, msg = host.home_follower()
        self.assertTrue(ok)
        self.assertEqual(msg, CM.RESYNC_IN_FLIGHT_MESSAGE_DE)
        self.assertFalse(host._collision_homing)
        self.assertEqual(len(rail.published), rail_after_resync)  # no new trajectory published
        # A re-click on the force-resume button is likewise a no-op (no second resync scheduled).
        self.assertEqual(host.force_resume_teleop()[1], CM.RESYNC_IN_FLIGHT_MESSAGE_DE)

        host.fire_pending_timers()  # the original resync still completes
        self.assertFalse(host._collision_active)

    def test_force_resume_does_not_auto_resume_recording(self):
        host = self._tripped_host(recording=True)
        self.assertTrue(host._collision_interrupted_recording)
        host._collision_leader_pos = {**CONTACT_POSE}

        success, _ = host.force_resume_teleop()
        self.assertTrue(success)
        # The marker is consumed immediately so completion takes the plain READY path.
        self.assertFalse(host._collision_interrupted_recording)

        host.fire_pending_timers()  # resync completion
        self.assertEqual(host.timer_start_calls, [])  # recording NOT re-armed
        self.assertFalse(host.on_recording)
        self.assertEqual(host.pub_for('/task/status').published[-1].phase, _TaskStatus.READY)


if __name__ == '__main__':
    unittest.main()

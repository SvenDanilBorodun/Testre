"""C1 + HIGH-6 — collision_monitor gating for the Roboter-Studio workflow path.

C1: the teleop force/collision detector is teleop/recording-ONLY. During a
workflow run (on_workflow=True, on_inference=False) a `pickup` presses on the
table at ~0 velocity by design — that looks exactly like a collision. The
detector must NOT trip then (it would publish a freeze/relax to
/leader/joint_trajectory racing the workflow's own writer, then wait for a
non-existent leader). The guard must STAY armed for teleop/recording.

HIGH-6: the server must be able to refuse a follower-only workflow while a
leader arm is live (a both-arms session). leader_appears_active() keys on live
/leader/joint_states freshness — the only signal that tracks the GUI leader
toggle, which recreates open_manipulator but NOT this physical_ai_server process
(so its EDUBOTICS_FOLLOWER_ONLY env goes stale and is deliberately not used).

collision_monitor.py imports rclpy/ROS msgs at module level; we stub those in
sys.modules and load the monitor under a throwaway name (same approach as the
robotis_ai_setup collision contract test).
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SAFETY_DIR = REPO_ROOT / 'physical_ai_server' / 'safety'


def _stub(name, **attrs):
    mod = sys.modules.get(name) or types.ModuleType(name)
    sys.modules[name] = mod
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


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
    _ph = type('_Placeholder', (), {})
    _stub('rclpy')
    _stub('rclpy.qos',
          QoSProfile=lambda **kw: kw,
          ReliabilityPolicy=types.SimpleNamespace(RELIABLE=1, BEST_EFFORT=2),
          DurabilityPolicy=types.SimpleNamespace(TRANSIENT_LOCAL=1, VOLATILE=2))
    _stub('sensor_msgs')
    _stub('sensor_msgs.msg', JointState=_ph)
    _stub('std_msgs')
    _stub('std_msgs.msg', Bool=_Bool)
    _stub('trajectory_msgs')
    _stub('trajectory_msgs.msg',
          JointTrajectory=_JointTrajectory, JointTrajectoryPoint=_JointTrajectoryPoint)
    _stub('physical_ai_interfaces')
    _stub('physical_ai_interfaces.msg', TaskStatus=_TaskStatus)
    _stub('control_msgs')
    _stub('control_msgs.msg', DynamicInterfaceGroupValues=_ph)
    # NOTE: we deliberately do NOT stub the `physical_ai_server` package — the
    # real package is on PYTHONPATH and `collision_detector` is rclpy-free, so
    # the monitor's `from physical_ai_server.safety.collision_detector import …`
    # resolves to the actual implementation. Stubbing the parent package here
    # would shadow the real one and break every other test that imports
    # physical_ai_server.workflow.* in the same pytest session.


def _load_monitor_module():
    _install_stubs()
    spec = importlib.util.spec_from_file_location(
        '_edubotics_collision_monitor_gate_test', SAFETY_DIR / 'collision_monitor.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CM = _load_monitor_module()


# ---- fake node host -------------------------------------------------------------

class _FakePublisher:
    def __init__(self, topic):
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
    def __init__(self):
        self.publishers = []
        self.timers = []
        self.on_inference = False
        self.on_recording = False
        self.on_workflow = False
        self.operation_mode = 'collection'
        self.start_recording_time = 0.0
        self._init_collision_monitor()

    def create_publisher(self, msg_type, topic, qos):
        pub = _FakePublisher(topic)
        self.publishers.append(pub)
        return pub

    def create_timer(self, period, callback):
        timer = _FakeTimer(callback)
        self.timers.append(timer)
        return timer

    def create_subscription(self, *a, **k):
        return None

    def get_logger(self):
        return _FakeLogger()

    def pub_for(self, topic):
        return next(p for p in self.publishers if p.topic == topic)


# ---- gpio message helpers (Jazzy DynamicInterfaceGroupValues shape) -------------

class _IV:
    def __init__(self, names, values):
        self.interface_names = list(names)
        self.values = list(values)


class _GpioMsg:
    """interface_groups = [gpio names]; interface_values[idx] = _IV(names, vals)."""

    def __init__(self, groups, ivs):
        self.interface_groups = list(groups)
        self.interface_values = list(ivs)


def _hard_press_msg():
    """A gpio sample where joint1 (dxl11, Present Load) is pressing far above
    its 0.30 effort-fraction threshold (load 900 / 1000 = 0.90) — an
    unambiguous over-force on a stationary joint."""
    groups, ivs = [], []
    for gpio_name in ('dxl11', 'dxl12', 'dxl13', 'dxl14', 'dxl15'):
        groups.append(gpio_name)
        if gpio_name == 'dxl11':
            ivs.append(_IV(['Present Load', 'Hardware Error Status'], [900.0, 0.0]))
        else:
            ivs.append(_IV(['Present Load', 'Hardware Error Status'], [0.0, 0.0]))
    return _GpioMsg(groups, ivs)


class _EnvGuard:
    """Set EDUBOTICS_* env vars for the duration of a host build; restore after."""

    def __init__(self, **env):
        self._env = env
        self._saved = {}

    def __enter__(self):
        import os
        for k, v in self._env.items():
            self._saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc):
        import os
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _host_debounce1():
    # debounce_ms low so a single hard-press sample trips immediately.
    with _EnvGuard(EDUBOTICS_COLLISION_DEBOUNCE_MS='1',
                   EDUBOTICS_COLLISION_ENABLED='1',
                   EDUBOTICS_FOLLOWER_ONLY=None):
        host = _Host()
    host._collision_follower_vel = {j: 0.0 for j in CM.ARM_JOINT_NAMES}
    return host


# ================================ C1 ============================================

class WorkflowGateTest(unittest.TestCase):

    def test_press_trips_in_teleop_recording_mode(self):
        # Baseline: with no workflow/inference running, a hard press DOES trip.
        host = _host_debounce1()
        host.on_workflow = False
        host.on_inference = False
        host._process_gpio_states(_hard_press_msg())
        self.assertTrue(host._collision_active,
                        'collision guard must stay armed for teleop/recording')

    def test_press_does_not_trip_during_workflow(self):
        # C1: the same hard press must NOT trip while a workflow runs.
        host = _host_debounce1()
        host.on_workflow = True
        host.on_inference = False
        host._process_gpio_states(_hard_press_msg())
        self.assertFalse(host._collision_active,
                         'collision must NOT trip during a Roboter-Studio workflow')

    def test_workflow_gate_resets_debounce_counters(self):
        # A bad-tick streak accrued before the workflow must be cleared so the
        # first tick after the workflow ends cannot trip on a stale streak.
        host = _host_debounce1()
        host.on_workflow = False
        host.on_inference = False
        # Accrue one bad tick but raise the needed debounce so it doesn't trip.
        host._collision_detector._debounce_ticks = 5
        host._process_gpio_states(_hard_press_msg())
        self.assertGreater(host._collision_detector._bad_ticks['joint1'], 0)
        # Now a workflow starts: the gate resets the counters.
        host.on_workflow = True
        host._process_gpio_states(_hard_press_msg())
        self.assertEqual(host._collision_detector._bad_ticks['joint1'], 0)

    def test_press_does_not_trip_during_inference_unchanged(self):
        # Regression: the pre-existing inference gate still holds.
        host = _host_debounce1()
        host.on_workflow = False
        host.on_inference = True
        host._process_gpio_states(_hard_press_msg())
        self.assertFalse(host._collision_active)


# ================================ HIGH-6 ========================================

class LeaderActiveTest(unittest.TestCase):

    def _plain_host(self):
        with _EnvGuard(EDUBOTICS_FOLLOWER_ONLY=None):
            return _Host()

    def test_no_leader_and_no_env_reads_inactive(self):
        host = self._plain_host()
        host._leader_state_last_mono = None
        with _EnvGuard(EDUBOTICS_FOLLOWER_ONLY=None):
            self.assertFalse(host.leader_appears_active())

    def test_stale_both_arms_env_without_live_leader_reads_inactive(self):
        # Root cause (2026-06-23): the leader toggle recreates ONLY
        # open_manipulator, so physical_ai_server keeps the both-arms
        # EDUBOTICS_FOLLOWER_ONLY='0' it was started with even after the student
        # flips into follower-only Roboter Studio. The env must NOT be trusted —
        # with no live leader publishing, the gate must read INACTIVE (else every
        # toggled-in workflow start is refused forever).
        host = self._plain_host()
        host._leader_state_last_mono = None
        with _EnvGuard(EDUBOTICS_FOLLOWER_ONLY='0'):
            self.assertFalse(host.leader_appears_active())

    def test_both_arms_env_with_live_leader_reads_active(self):
        # A genuinely-live leader (both-arms) is caught by the joint_states
        # freshness signal regardless of the env value.
        import time
        host = self._plain_host()
        host._leader_state_last_mono = time.monotonic()
        with _EnvGuard(EDUBOTICS_FOLLOWER_ONLY='0'):
            self.assertTrue(host.leader_appears_active())

    def test_env_follower_only_with_no_leader_reads_inactive(self):
        host = self._plain_host()
        host._leader_state_last_mono = None
        with _EnvGuard(EDUBOTICS_FOLLOWER_ONLY='1'):
            self.assertFalse(host.leader_appears_active())

    def test_fresh_leader_joint_states_reads_active(self):
        import time
        host = self._plain_host()
        host._leader_state_last_mono = time.monotonic()
        with _EnvGuard(EDUBOTICS_FOLLOWER_ONLY=None):
            self.assertTrue(host.leader_appears_active())

    def test_fresh_leader_overrides_stale_follower_only_env(self):
        # A live leader publishing right now beats a stale FOLLOWER_ONLY=1 env.
        import time
        host = self._plain_host()
        host._leader_state_last_mono = time.monotonic()
        with _EnvGuard(EDUBOTICS_FOLLOWER_ONLY='1'):
            self.assertTrue(host.leader_appears_active())

    def test_stale_leader_joint_states_reads_inactive(self):
        import time
        host = self._plain_host()
        # Last leader sample well outside the fresh window.
        host._leader_state_last_mono = time.monotonic() - 100.0
        with _EnvGuard(EDUBOTICS_FOLLOWER_ONLY=None):
            self.assertFalse(host.leader_appears_active())

    def test_leader_state_cb_updates_freshness(self):
        host = self._plain_host()
        host._leader_state_last_mono = None
        msg = types.SimpleNamespace(
            name=['joint1', 'joint2'], position=[0.0, 0.0])
        host._collision_leader_state_cb(msg)
        self.assertIsNotNone(host._leader_state_last_mono)


if __name__ == '__main__':
    unittest.main()

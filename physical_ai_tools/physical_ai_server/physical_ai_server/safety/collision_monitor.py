# Copyright 2026 EduBotics
#
# Teleoperation force/collision e-stop — ORCHESTRATION mixin for PhysicalAIServer.
#
# This mixin owns the ROS surface of the EduBotics teleop collision guard (a Rule §2 software
# safety guard, scoped to teleop/recording only). The pure detection logic lives in
# collision_detector.py (unit-tested without ROS); this layer wires it to the live system.
#
# State machine (student-paced two-step recovery — the student must be able to REMOVE the
# obstacle before the arm moves again, so the arm never drags along/through the object):
#
#   TRIP (detector fires) -> "stopped"            (ORDER MATTERS):
#     1. /collision_flag = True  (freeze the leader broadcaster: upstream
#        om_joint_trajectory_command_broadcaster skips publishing while the flag is set; a
#        5 Hz watchdog re-asserts the latch)
#     2. RELAX IN PLACE: command the follower's CURRENT measured pose as the new goal —
#        a frozen position-mode joint otherwise keeps pressing toward its last goal (which is
#        inside the obstacle) at full PWM. Sent DELAYED by RELAX_DELAY_S (the broadcaster
#        needs ~a DDS hop + one 100 Hz cycle to see the flag; published back-to-back its last
#        in-flight leader poses preempt us — observed live 2026-06-03), then re-sent
#        RELAX_SENDS times re-capturing the measured pose each time (the rail subscriber is
#        BEST_EFFORT; a one-shot message can drop — and re-sending converges: once motion
#        stops, commanded pose == measured pose). The arm does NOT auto-home.
#     3. if recording: discard the in-progress (contaminated) episode via
#        data_manager.re_record() and halt capture — nothing after the trip is recorded
#        (Rule §2). Prior saved episodes are kept, and the fact that a recording was in flight
#        is remembered so RESUME_TELEOP can continue the SAME session (see below).
#     4. surface phase=COLLISION on /task/status (re-asserted by the watchdog for late page
#        loads). The React CollisionModal shows step 1: "Hindernis entfernen" + button
#        „Follower in Grundstellung fahren".
#
#   HOME_FOLLOWER (/task/command, student clicked step 1) -> "homing" -> "homed":
#     best-effort reboot of firmware-latched joints, then a quintic safe-home glide on
#     /leader/joint_trajectory (the arm_controller subscribes there directly, unaffected by
#     the flag), VERIFIED: progress is checked every GLIDE_VERIFY_AFTER_S; a stalled glide is
#     re-sent (re-computed from the current pose) up to GLIDE_MAX_SENDS, then reported as
#     failed (phase falls back to COLLISION with a German retry hint — the student removes
#     the obstacle and clicks again). The gripper is held at its current position (don't drop
#     a grasped object). On verified arrival: phase=COLLISION_HOMED -> React modal step 2:
#     bring the leader to the home pose + button „Teleoperation fortsetzen".
#
#   RESUME_TELEOP (/task/command, student clicked step 2): STRICT ordering — refused until
#     the follower actually reached home (one mental model for students). Then: proximity
#     check leader vs home (refuse + re-prompt if too far), quintic resync follower->leader,
#     and clear /collision_flag after the resync completes. If a recording was interrupted
#     (step 3 above), the SAME session is seamlessly resumed instead of publishing READY —
#     the kept DataManager re-records the interrupted episode after the Rücksetzzeit, prior
#     episodes intact (the student loses nothing). Otherwise (free teleop) phase=READY.
#
# Inference safety (Rule §2): the detector is gated off when self.on_inference is True, AND
# structurally the /collision_flag consumers (the leader's trajectory broadcaster and the
# leader's gravity-compensation controller — both leader-side, stock upstream subscriptions)
# do not run the inference action path (inference publishes JointTrajectory directly), so the
# guard is doubly invisible to inference and never reshapes the recorded->replayed action
# distribution.
#
# Workflow safety (Rule §2, same scope): the detector is ALSO gated off when self.on_workflow
# is True. A Roboter-Studio workflow's `pickup` descends onto the table at ~0 velocity — that
# is a deliberate press, indistinguishable from a collision — and Roboter Studio runs
# follower-only (no leader), so a trip would publish a freeze + relax to /leader/joint_trajectory
# racing the workflow's own trajectory writer and then wait for a non-existent leader to recover.
# The guard is therefore teleop/recording-ONLY (on_workflow == on_inference == False).

import os
import time

from physical_ai_interfaces.msg import TaskStatus
from physical_ai_server.safety.collision_detector import (
    build_detector_from_env,
    effort_fraction_from_values,
)
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import JointState
from std_msgs.msg import Bool
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint

# The open_manipulator gpio_command_controller publishes its gpio state as
# control_msgs/DynamicInterfaceGroupValues on ROS 2 Jazzy (fields: interface_groups +
# interface_values); older gpio_controllers releases used DynamicJointState (joint_names +
# interface_values). Subscribe with the type the publisher actually uses — a DDS type
# mismatch is SILENT (the subscription simply never receives a message; confirmed live
# 2026-06-03: a DynamicJointState subscription got zero data from the
# DynamicInterfaceGroupValues publisher). Guard the imports so a base image without
# control_msgs degrades to "no detection" (fail-open) instead of crashing the node; the
# server image must install ros-jazzy-control-msgs (package.xml declares the dep).
try:
    from control_msgs.msg import DynamicInterfaceGroupValues as GpioStateMsg
    _HAVE_GPIO_STATE_MSG = True
except ImportError:  # pragma: no cover - depends on image contents
    try:
        from control_msgs.msg import DynamicJointState as GpioStateMsg
        _HAVE_GPIO_STATE_MSG = True
    except ImportError:
        GpioStateMsg = None
        _HAVE_GPIO_STATE_MSG = False

# OMX follower geometry. The action rail carries all six leader joints in this order
# (matches omx_f_config.yaml joint_order.leader).
LEADER_JOINTS = ('joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1')
ARM_JOINT_NAMES = ('joint1', 'joint2', 'joint3', 'joint4', 'joint5')  # detector keys (no gripper)
# Safe home for the 5 arm joints (gripper is held at its current pose, not commanded). Matches
# jetson_agent.SAFE_HOME_JOINTS arm values — a neutral retracted pose.
SAFE_HOME_ARM = (0.0, -0.785398, 0.785398, 0.0, 0.0)
# gpio name (xacro <gpio name=...>) -> arm joint name.
GPIO_NAME_TO_JOINT = {
    'dxl11': 'joint1', 'dxl12': 'joint2', 'dxl13': 'joint3',
    'dxl14': 'joint4', 'dxl15': 'joint5',
}
# arm joint -> Dynamixel ID, for the best-effort reboot of firmware-latched joints.
JOINT_TO_DXL_ID = {'joint1': 11, 'joint2': 12, 'joint3': 13, 'joint4': 14, 'joint5': 15}

GPIO_STATES_DEFAULT_TOPIC = '/gpio_command_controller/gpio_states'
LEADER_TRAJECTORY_TOPIC = '/leader/joint_trajectory'
COLLISION_FLAG_TOPIC = '/collision_flag'
JOINT_STATES_TOPIC = '/joint_states'
LEADER_JOINT_STATES_TOPIC = '/leader/joint_states'
REBOOT_DXL_SERVICE = '/dynamixel_hardware_interface/reboot_dxl'
SET_TORQUE_SERVICE = '/dynamixel_hardware_interface/set_dxl_torque'

# TaskStatus phases for the two-step recovery. getattr-fallback so a container whose COMPILED
# physical_ai_interfaces predates the new constants (the on-rig validation images — the .msg
# constants only materialize after an interfaces re-colcon) still publishes the right wire
# values; the React side compares the raw ints (taskPhases.js).
PHASE_COLLISION = TaskStatus.COLLISION
PHASE_COLLISION_HOMING = getattr(TaskStatus, 'COLLISION_HOMING', 8)
PHASE_COLLISION_HOMED = getattr(TaskStatus, 'COLLISION_HOMED', 9)

# Relax-in-place delivery (trip time). RELAX_DELAY_S: the broadcaster needs ~one DDS hop +
# one 100 Hz cycle to see the freeze flag — published back-to-back, its last in-flight leader
# poses preempt our message (observed live 2026-06-03: the arm kept pressing the obstacle).
# The re-sends cover a BEST_EFFORT drop and converge on the true rest pose (each send
# re-captures the measured position).
RELAX_DELAY_S = 0.15
RELAX_RESEND_EVERY_S = 0.35
RELAX_SENDS = 3
RELAX_HOLD_DURATION_S = 0.25   # single-point trajectory time_from_start

# Safe-home glide (student-triggered via HOME_FOLLOWER) — verified delivery. By click time
# the broadcaster has been frozen for seconds, so there is no preemption race; the verify +
# re-send loop covers BEST_EFFORT drops and a glide stalled by a still-present obstacle.
COLLISION_HOME_DURATION_S = 2.5
GLIDE_FIRST_DELAY_S = 0.1      # let the service response return before the arm moves
GLIDE_VERIFY_AFTER_S = 1.0     # progress check cadence after each send
GLIDE_MAX_SENDS = 3
GLIDE_PROGRESS_EPS_RAD = 0.05  # max-joint dist-to-home improvement that counts as "moving"
HOME_ARRIVED_TOL_RAD = 0.10    # max-joint distance that counts as "reached home"

RESYNC_DURATION_S = 3.0
WATCHDOG_PERIOD_S = 0.2
DEFAULT_RESUME_TOL_RAD = 0.30

# HIGH-6: a /leader/joint_states sample newer than this counts as a live leader
# (a both-arms session). The leader broadcaster publishes joint states at the
# controller rate (~100 Hz), so even a generous 2 s window only reads "active"
# while a leader is genuinely running.
LEADER_ACTIVE_FRESH_WINDOW_S = 2.0

COLLISION_MESSAGE_DE = (
    'STOPP — Kollision erkannt: Der Arm wurde gegen ein Hindernis gedrückt und angehalten. '
    'Entferne zuerst das Hindernis und klicke dann auf „Follower in Grundstellung fahren".'
)
COLLISION_HOMING_MESSAGE_DE = 'Der Follower fährt in die Grundstellung …'
COLLISION_HOMED_MESSAGE_DE = (
    'Der Follower ist in der Grundstellung. Bringe den Leader-Arm in die gleiche Stellung '
    'und klicke dann auf „Teleoperation fortsetzen".'
)
COLLISION_HOME_FAILED_MESSAGE_DE = (
    'Der Follower konnte die Grundstellung nicht erreichen. Prüfe, ob das Hindernis entfernt '
    'ist, und klicke erneut auf „Follower in Grundstellung fahren".'
)
RESYNC_IN_FLIGHT_MESSAGE_DE = 'Teleoperation wird bereits wiederhergestellt — bitte warten …'


class CollisionMonitorMixin:
    """Mix into PhysicalAIServer(Node). Call _init_collision_monitor() from __init__."""

    def _init_collision_monitor(self):
        # Collision-stop state machine: active -> (homing) -> homed -> cleared.
        self._collision_active = False
        self._collision_homing = False
        self._collision_homed = False
        self._collision_overload_joints = []
        self._collision_message = COLLISION_MESSAGE_DE
        self._collision_resync_timer = None
        # Seamless-resume state: when a collision trips MID-RECORDING we discard the
        # in-progress episode and halt capture (Rule §2), but remember that a recording was
        # in flight so RESUME_TELEOP can continue the SAME session (same dataset, prior
        # episodes kept, the interrupted episode re-recorded after the Rücksetzzeit) instead
        # of forcing the student to start over. Set at trip, cleared at resync-complete (and
        # defensively coupled to _collision_active, which _assert_no_other_active blocks on).
        self._collision_interrupted_recording = False
        self._collision_interrupted_mode = None
        # Relax-in-place delivery state (see RELAX_* constants).
        self._relax_timer = None
        self._relax_sends = 0
        # Verified safe-home glide state (see GLIDE_* constants).
        self._glide_timer = None
        self._glide_sends = 0
        self._glide_last_dist = None
        # Latest follower/leader joint snapshots (rad / rad/s), keyed by joint name.
        self._collision_follower_pos = {}
        self._collision_follower_vel = {}
        self._collision_leader_pos = {}
        # monotonic() of the last /leader/joint_states sample, or None if a
        # leader has never published. Used by leader_appears_active() (HIGH-6)
        # to refuse a follower-only Roboter-Studio workflow while a leader arm
        # is live (a both-arms session): a live leader means the workflow's
        # trajectory writer would contend with the leader broadcaster on
        # /leader/joint_trajectory. The collision monitor already subscribes to
        # this topic, so tracking arrival here is free.
        self._leader_state_last_mono = None

        self._collision_resume_tol = _env_float(
            'EDUBOTICS_COLLISION_RESUME_TOL_RAD', DEFAULT_RESUME_TOL_RAD)
        gpio_topic = os.environ.get(
            'EDUBOTICS_COLLISION_GPIO_TOPIC', GPIO_STATES_DEFAULT_TOPIC) or GPIO_STATES_DEFAULT_TOPIC

        # Build the pure detector from the collision-guard env vars (defaults are safe).
        self._collision_detector = build_detector_from_env(
            os.environ.get, ARM_JOINT_NAMES, update_rate_hz=100.0)

        # Publishers. /collision_flag is RELIABLE + TRANSIENT_LOCAL so a (re)starting leader
        # broadcaster latches the current value; the watchdog re-asserts it regardless.
        # depth=1: with TRANSIENT_LOCAL the history depth is what gets REPLAYED to a
        # late-joining durable subscriber — depth 10 served a burst of stale flag values
        # (e.g. a True from a long-resolved stop) before the current one, which misled
        # both debugging (`ros2 topic echo --once` printed the oldest) and any future
        # durable consumer. Only the newest value is ever meaningful.
        latched_qos = QoSProfile(
            depth=1, reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self._collision_flag_pub = self.create_publisher(
            Bool, COLLISION_FLAG_TOPIC, latched_qos)
        # Clear any STALE freeze latch on startup. The leader broadcaster
        # (om_joint_trajectory_command_broadcaster) is a VOLATILE subscriber that NEVER
        # self-clears collision_detected_ — it only un-freezes on an explicit
        # /collision_flag=False. This node carries respawn=True (physical_ai_server.launch.py);
        # if it respawned while a True flag was latched in the broadcaster (a collision was
        # active, or an unhandled exception elsewhere killed the node mid-collision), the
        # follower would stay frozen forever with NO active collision to recover from — env
        # restart only (issue #19 finding #4). Publishing False at init un-freezes it
        # immediately; the watchdog re-asserts False at 5 Hz to cover the discovery race (a
        # one-shot publish can be missed before the broadcaster's subscription is matched).
        self._publish_collision_flag(False)
        # Mirrors the entrypoint Phase-3 / jetson_agent safe-home publisher QoS.
        self._collision_leader_traj_pub = self.create_publisher(
            JointTrajectory, LEADER_TRAJECTORY_TOPIC, latched_qos)
        # Own /task/status publisher so collision notifications work even when the recording
        # timer (the usual status source) is not running (i.e. free teleop, or after we halt it).
        self._collision_status_pub = self.create_publisher(TaskStatus, '/task/status', 10)

        # 5 Hz watchdog: re-assert the latch + re-publish the banner while stopped.
        self._collision_watchdog = self.create_timer(
            WATCHDOG_PERIOD_S, self._collision_watchdog_cb)

        # Always know the follower/leader poses (needed for relax/home/resync).
        self.create_subscription(
            JointState, JOINT_STATES_TOPIC, self._collision_follower_state_cb, 10)
        self.create_subscription(
            JointState, LEADER_JOINT_STATES_TOPIC, self._collision_leader_state_cb, 10)

        if not _HAVE_GPIO_STATE_MSG:
            self.get_logger().warning(
                '[KOLLISION] control_msgs gpio state message unavailable — teleop collision '
                'detection DISABLED (fail-open). The arm still works; force protection is off.')
            return
        if not self._collision_detector.enabled:
            # Fully off: no gpio subscription and no misleading "armed" log below.
            self.get_logger().info(
                '[KOLLISION] Teleop collision guard disabled via EDUBOTICS_COLLISION_ENABLED=0.')
            return
        self.create_subscription(
            GpioStateMsg, gpio_topic, self._gpio_states_cb, 10)
        self.get_logger().info(
            f'[KOLLISION] Teleop collision guard armed (gpio topic: {gpio_topic}, '
            f'debounce {self._collision_detector.debounce_ticks} ticks, '
            f'resume tol {self._collision_resume_tol:.2f} rad).')

    # ---- state subscriptions -------------------------------------------------------------

    def _collision_follower_state_cb(self, msg):
        pos, vel = {}, {}
        for idx, name in enumerate(msg.name):
            if idx < len(msg.position):
                pos[name] = msg.position[idx]
            if idx < len(msg.velocity):
                vel[name] = msg.velocity[idx]
        self._collision_follower_pos = pos
        self._collision_follower_vel = vel

    def _collision_leader_state_cb(self, msg):
        pos = {}
        for idx, name in enumerate(msg.name):
            if idx < len(msg.position):
                pos[name] = msg.position[idx]
        self._collision_leader_pos = pos
        # Record arrival for the HIGH-6 both-arms guard (leader_appears_active).
        self._leader_state_last_mono = time.monotonic()

    # ---- both-arms (leader-active) detection (HIGH-6) ------------------------------------

    def leader_appears_active(self, fresh_window_s=LEADER_ACTIVE_FRESH_WINDOW_S):
        """True when a leader arm appears to be live (a both-arms session), so a
        follower-only Roboter-Studio workflow MUST NOT start (it would contend
        with the leader broadcaster on /leader/joint_trajectory).

        Two signals, strongest first:
          1. EDUBOTICS_FOLLOWER_ONLY: the GUI bridge sets this to 1 when it flips
             the arm container into Roboter-Studio (follower-only) mode. An
             explicit '0'/'false'/'no'/'off' means a both-arms session was
             launched → active. Unset means UNKNOWN (the env is not authoritative
             on every rig), so we don't refuse on it alone.
          2. Live /leader/joint_states within `fresh_window_s`: a leader that is
             actually publishing joint states is, by definition, running. This is
             the empirical backstop when the env is unset/unreliable.

        Limitation: a both-arms session whose leader has momentarily stopped
        publishing (>fresh_window_s) AND with the env unset reads as "not active".
        The deterministic guard is EDUBOTICS_FOLLOWER_ONLY (set by the GUI on the
        student path); the live-topic check only strengthens it. The GUI bridge is
        hardened separately as the primary UI-level guard.
        """
        raw = os.environ.get('EDUBOTICS_FOLLOWER_ONLY')
        if raw is not None and str(raw).strip() != '':
            follower_only = str(raw).strip().lower() in ('1', 'true', 'yes', 'on')
            if not follower_only:
                # The env explicitly says both arms are launched.
                return True
            # Explicit follower-only: trust it, but still fall through to the
            # live-topic check below — a stale env can't override a leader that
            # is demonstrably publishing right now.
        last = self._leader_state_last_mono
        if last is None:
            return False
        return (time.monotonic() - last) <= max(0.0, float(fresh_window_s))

    # ---- detection -----------------------------------------------------------------------

    def _gpio_states_cb(self, msg):
        # This subscription runs continuously (~100 Hz) on the executor and parses
        # mixed-model Dynamixel gpio data (XL430 'Present Load' + XL330 'Present
        # Current' + 'Hardware Error Status'). An unhandled exception here would
        # propagate out of MultiThreadedExecutor.spin() and KILL the node — main()
        # only catches KeyboardInterrupt — leaving the React app "Getrennt" with no
        # recovery short of an environment restart (root-caused 2026-06-13 as the
        # most credible intermittent-crash trigger on rigs with a Dynamixel storm).
        # Never let a malformed/edge-case gpio message crash the node: log it and
        # drop the tick (fail-safe = no detection for that frame, same posture as
        # the fail-open control_msgs import guard).
        try:
            self._process_gpio_states(msg)
        except Exception as exc:  # noqa: BLE001 - guard must never crash the node
            self.get_logger().error(
                f'[KOLLISION] gpio state processing failed (ignored): {exc}')

    def _process_gpio_states(self, msg):
        if self._collision_active:
            return  # already stopped; ignore until resume
        # Rule §2 scope: the teleop force/collision guard is teleop/recording-ONLY.
        # During a Roboter-Studio workflow run (on_workflow=True, on_inference=False)
        # the follower presses on the table at ~0 velocity by DESIGN (a `pickup`
        # descend) — that looks exactly like a collision, so the detector would trip,
        # publish to /leader/joint_trajectory (racing the workflow's own writer) and
        # then wait for a leader that does not exist in the follower-only Roboter-Studio
        # mode → the student is wedged with no recovery path. Treat a running workflow
        # like inference: no detection, and reset the debounce counters so a residual
        # bad-tick streak from before the workflow can't trip on the first tick after
        # it ends. The guard stays fully armed for teleop/recording (on_workflow False).
        if getattr(self, 'on_workflow', False):
            self._collision_detector.reset()
            return
        efforts, err_bits = {}, {}
        # Jazzy DynamicInterfaceGroupValues names its groups 'interface_groups'; the legacy
        # DynamicJointState equivalent is 'joint_names'. Same per-entry interface_values.
        gpio_names = getattr(msg, 'interface_groups', None)
        if gpio_names is None:
            gpio_names = getattr(msg, 'joint_names', [])
        for idx, gpio_name in enumerate(gpio_names):
            joint = GPIO_NAME_TO_JOINT.get(gpio_name)
            if joint is None or idx >= len(msg.interface_values):
                continue
            iv = msg.interface_values[idx]
            name_to_val = dict(zip(iv.interface_names, iv.values))
            # MODEL-AWARE: XL430-W250 base joints (dxl11-13) export 'Present Load', XL330-M288
            # wrist joints (dxl14-15) export 'Present Current'. effort_fraction_from_values()
            # reads whichever this joint has and normalizes it to a 0..1 effort fraction
            # (|signal|/full-scale) so the detector compares one threshold across both models.
            eff = effort_fraction_from_values(joint, name_to_val)
            if eff is not None:
                efforts[joint] = eff
            raw_err = name_to_val.get('Hardware Error Status')
            if raw_err is not None:
                err_bits[joint] = int(round(float(raw_err)))
        velocities = {j: self._collision_follower_vel.get(j, 0.0) for j in ARM_JOINT_NAMES}

        result = self._collision_detector.update(
            efforts, velocities, err_bits, mode_is_inference=self.on_inference)
        if result.tripped:
            self._trigger_collision_stop(result)

    def _trigger_collision_stop(self, result):
        self._collision_active = True
        self._collision_homing = False
        self._collision_homed = False
        self._collision_overload_joints = list(result.latched_overload)
        self._collision_message = COLLISION_MESSAGE_DE
        self.get_logger().warning(f'[KOLLISION] Stop ausgelöst: {result.reason}')

        # 1. Freeze the leader broadcaster FIRST. Guarded INDEPENDENTLY of the relax below
        #    (issue #19 finding #1): _trigger_collision_stop runs inside _gpio_states_cb's
        #    try/except, so a raise here would be swallowed and could strand _collision_active
        #    with no relax scheduled and no modal published. Each safety action runs on its
        #    own so one failure can't skip the next; the watchdog also re-asserts the flag +
        #    status at 5 Hz while active, so even a fully-failed publish self-heals.
        try:
            self._publish_collision_flag(True)
        except Exception as exc:  # noqa: BLE001 - must not skip the relax below
            self.get_logger().error(f'[KOLLISION] flag publish at trip failed: {exc}')

        # 2. Relax in place — DELAYED + re-sent (see RELAX_* constants): command the current
        #    measured pose so the position error (and with it the press force) drops to zero.
        #    The arm does NOT move home here; the student removes the obstacle first and
        #    triggers the home glide from the modal (HOME_FOLLOWER).
        try:
            self._relax_sends = 0
            self._schedule_relax(RELAX_DELAY_S)
        except Exception as exc:  # noqa: BLE001 - must not skip the status publish below
            self.get_logger().error(f'[KOLLISION] relax scheduling at trip failed: {exc}')

        # 3. Halt capture + discard the in-progress (contaminated) episode so nothing after
        #    the trip is recorded (Rule §2). REMEMBER the recording was in flight so the
        #    two-step recovery can SEAMLESSLY resume the SAME session on RESUME_TELEOP — the
        #    DataManager/TimerManager objects are kept (not rebuilt), re_record() rewinds the
        #    FSM to 'reset' WITHOUT counting the episode, so capture resumes with the
        #    Rücksetzzeit and re-records that very episode. Stop the timer BEFORE re_record()
        #    so the gpio-thread FSM rewind can't be interleaved by a new recording tick on the
        #    timer thread (no new tick starts once the timer is destroyed).
        if getattr(self, 'on_recording', False):
            self._collision_interrupted_recording = True
            self._collision_interrupted_mode = getattr(self, 'operation_mode', 'collection')
            self.on_recording = False
            try:
                self.timer_manager.stop(timer_name=self.operation_mode)
            except Exception as exc:  # noqa: BLE001
                self.get_logger().error(f'[KOLLISION] timer stop failed: {exc}')
            try:
                self.data_manager.re_record()
            except Exception as exc:  # noqa: BLE001 - never let cleanup crash the guard
                self.get_logger().error(f'[KOLLISION] re_record failed: {exc}')

        # 4. Tell the student (re-asserted by the watchdog, so a failure here self-heals).
        try:
            self._publish_collision_status()
        except Exception as exc:  # noqa: BLE001
            self.get_logger().error(f'[KOLLISION] collision status publish at trip failed: {exc}')

    # ---- actuation helpers ---------------------------------------------------------------

    def _publish_collision_flag(self, value):
        msg = Bool()
        msg.data = bool(value)
        self._collision_flag_pub.publish(msg)

    # ---- relax-in-place delivery (RELAX_* constants) ---------------------------------------

    def _cancel_relax_timer(self):
        if self._relax_timer is not None:
            self._relax_timer.cancel()
            self._relax_timer = None

    def _schedule_relax(self, delay):
        self._cancel_relax_timer()
        self._relax_timer = self.create_timer(delay, self._on_relax_timer)

    def _on_relax_timer(self):
        """One-shot: (re-)assert "hold the current measured pose" on the rail."""
        self._cancel_relax_timer()
        if not self._collision_active or self._collision_homing or self._collision_homed:
            return
        self._publish_hold_current_pose()
        self._relax_sends += 1
        if self._relax_sends < RELAX_SENDS:
            self._schedule_relax(RELAX_RESEND_EVERY_S)

    def _publish_hold_current_pose(self):
        """Single-point trajectory at the follower's measured pose (zero velocity).

        Commanding the measured position zeroes the position error, so the servos drop from
        "pressing toward a goal inside the obstacle" to plain holding torque — without moving
        the arm. Skipped (and retried by the next relax send) when the follower pose isn't
        fully known yet; commanding a 0.0 default would slam the arm."""
        if not all(j in self._collision_follower_pos for j in LEADER_JOINTS):
            missing = [j for j in LEADER_JOINTS if j not in self._collision_follower_pos]
            self.get_logger().warning(
                f'[KOLLISION] hold-pose skipped — follower pose incomplete (missing {missing}).')
            return
        traj = JointTrajectory()
        traj.joint_names = list(LEADER_JOINTS)
        point = JointTrajectoryPoint()
        point.positions = [float(self._collision_follower_pos[j]) for j in LEADER_JOINTS]
        point.velocities = [0.0] * len(LEADER_JOINTS)
        point.time_from_start.sec = 0
        point.time_from_start.nanosec = int(RELAX_HOLD_DURATION_S * 1e9)
        traj.points.append(point)
        self._collision_leader_traj_pub.publish(traj)

    # ---- student-triggered safe-home glide (HOME_FOLLOWER) --------------------------------

    def home_follower(self):
        """Handle the HOME_FOLLOWER command (modal step 1). Returns (success, german_message).

        Non-blocking: kicks off the verified glide and returns immediately — a 2.5 s blocking
        wait in the service callback would stall the node's MutuallyExclusive default group
        (same failure mode as the HF whoami heartbeat stall)."""
        if not self._collision_active:
            return False, 'Keine aktive Kollision — der Follower muss nicht in die Grundstellung.'
        # Refuse while a resync is already in flight (a (force-)resume was clicked and its
        # quintic + completion timer are live): a competing home glide on the SAME action rail
        # would fight the resync trajectory. The force-resume path keeps phase=COLLISION
        # (stage 'stopped') during the resync, so this step-1 button stays visible — guard it
        # at the source of truth rather than relying on the UI to disable it.
        if self._collision_resync_timer is not None:
            return True, RESYNC_IN_FLIGHT_MESSAGE_DE
        if self._collision_homed:
            return True, 'Der Follower ist bereits in der Grundstellung.'
        if self._collision_homing:
            return True, COLLISION_HOMING_MESSAGE_DE

        # Firmware-latched (Overload) joints have torque disabled and cannot glide — recover
        # them first. Best-effort and rare: the software guard normally trips well before the
        # firmware latches.
        if self._collision_overload_joints:
            self._best_effort_reboot(self._collision_overload_joints)
            self._collision_overload_joints = []

        self._cancel_relax_timer()
        self._collision_homing = True
        self._collision_message = COLLISION_HOMING_MESSAGE_DE
        self._glide_sends = 0
        self._glide_last_dist = None
        self._schedule_glide(GLIDE_FIRST_DELAY_S)
        self._publish_collision_status()
        return True, COLLISION_HOMING_MESSAGE_DE

    def _publish_safe_home_glide(self):
        start = {j: self._collision_follower_pos.get(j, 0.0) for j in LEADER_JOINTS}
        target = dict(start)
        for idx, joint in enumerate(ARM_JOINT_NAMES):
            target[joint] = SAFE_HOME_ARM[idx]
        # gripper_joint_1 stays at its current position (don't release a grasped object).
        self._publish_quintic(LEADER_JOINTS, start, target, COLLISION_HOME_DURATION_S)

    def _dist_to_home_per_joint(self):
        """Per-arm-joint |pos - safe_home| (rad), joint1..joint5 order; None before
        the first /joint_states sample. Feeds the CollisionModal's read-only
        homing progress strip (leLab-comparison PR-2) — telemetry only, never
        part of the recovery state machine."""
        if not all(j in self._collision_follower_pos for j in ARM_JOINT_NAMES):
            return None
        return [abs(self._collision_follower_pos[j] - SAFE_HOME_ARM[i])
                for i, j in enumerate(ARM_JOINT_NAMES)]

    def _dist_to_home(self):
        """Max arm-joint distance (rad) from the safe-home pose; None before the first
        /joint_states sample."""
        per_joint = self._dist_to_home_per_joint()
        return None if per_joint is None else max(per_joint)

    def _cancel_glide_timer(self):
        if self._glide_timer is not None:
            self._glide_timer.cancel()
            self._glide_timer = None

    def _schedule_glide(self, delay):
        self._cancel_glide_timer()
        self._glide_timer = self.create_timer(delay, self._on_glide_timer)

    def _on_glide_timer(self):
        """One-shot verify/(re-)send loop for the safe-home glide.

        Arrived -> homed. Visibly moving -> keep watching. Stalled -> re-send (the quintic is
        recomputed from the CURRENT follower pose, so a retry after a lost message or a
        mid-glide stall starts from where the arm actually is), up to GLIDE_MAX_SENDS, then
        report failure back to the student (modal step 1 stays, with a retry hint)."""
        self._cancel_glide_timer()
        if not self._collision_active or not self._collision_homing:
            return
        dist = self._dist_to_home()
        if dist is not None and dist <= HOME_ARRIVED_TOL_RAD:
            self._finish_homing(True, dist)
            return
        if self._glide_sends > 0:
            moving = (dist is not None and self._glide_last_dist is not None
                      and (self._glide_last_dist - dist) > GLIDE_PROGRESS_EPS_RAD)
            if moving:
                self._glide_last_dist = dist
                self._schedule_glide(GLIDE_VERIFY_AFTER_S)
                return
            if self._glide_sends >= GLIDE_MAX_SENDS:
                self._finish_homing(False, dist)
                return
        self._glide_sends += 1
        self._glide_last_dist = dist
        self._publish_safe_home_glide()
        self._schedule_glide(GLIDE_VERIFY_AFTER_S)

    def _finish_homing(self, success, dist=None):
        self._collision_homing = False
        if success:
            self._collision_homed = True
            self._collision_message = COLLISION_HOMED_MESSAGE_DE
            self.get_logger().info('[KOLLISION] Follower in Grundstellung (verifiziert).')
        else:
            self._collision_homed = False
            self._collision_message = COLLISION_HOME_FAILED_MESSAGE_DE
            self.get_logger().error(
                f'[KOLLISION] Safe-home glide did not arrive after {GLIDE_MAX_SENDS} sends '
                f'(dist-to-home {dist if dist is not None else float("nan"):.2f} rad). The '
                'broadcaster stays frozen; the student can retry from the modal.')
        self._publish_collision_status()

    def _publish_quintic(self, joints, start, target, duration):
        """Publish a 50-point quintic JointTrajectory (zero-velocity endpoints) to the rail."""
        if duration <= 0:
            duration = 1.0
        traj = JointTrajectory()
        traj.joint_names = list(joints)
        deltas = [float(target[j]) - float(start[j]) for j in joints]
        n_points = 50
        for i in range(n_points):
            t = (i + 1) / n_points
            s = 10 * t ** 3 - 15 * t ** 4 + 6 * t ** 5
            s_dot = (30 * t ** 2 - 60 * t ** 3 + 30 * t ** 4) / duration
            s_ddot = (60 * t - 180 * t ** 2 + 120 * t ** 3) / (duration * duration)
            point = JointTrajectoryPoint()
            point.positions = [float(start[j]) + d * s for j, d in zip(joints, deltas)]
            point.velocities = [d * s_dot for d in deltas]
            point.accelerations = [d * s_ddot for d in deltas]
            secs = duration * t
            point.time_from_start.sec = int(secs)
            point.time_from_start.nanosec = int((secs % 1) * 1e9)
            traj.points.append(point)
        self._collision_leader_traj_pub.publish(traj)

    # ---- status to React -----------------------------------------------------------------

    def _collision_phase(self):
        if self._collision_homed:
            return PHASE_COLLISION_HOMED
        if self._collision_homing:
            return PHASE_COLLISION_HOMING
        return PHASE_COLLISION

    def _publish_collision_status(self):
        status = TaskStatus()
        status.phase = self._collision_phase()
        status.current_task_instruction = self._collision_message
        # Leave .error empty: the React /task/status handler short-circuits to a transient
        # toast on a non-empty error. The collision banner rides the phase instead.
        # Per-joint homing telemetry for the CollisionModal progress strip.
        # getattr-SAFE: a container running pre-rebuild compiled interfaces has
        # no joint_dist_to_home field, and a bare setattr would raise inside
        # this publish — the safety status must keep flowing regardless.
        per_joint = self._dist_to_home_per_joint()
        if per_joint is not None and hasattr(status, 'joint_dist_to_home'):
            status.joint_dist_to_home = [float(d) for d in per_joint]
        self._collision_status_pub.publish(status)

    def _publish_cleared_status(self):
        status = TaskStatus()
        status.phase = TaskStatus.READY
        self._collision_status_pub.publish(status)

    def _collision_watchdog_cb(self):
        if self._collision_active:
            # Re-assert the latch (broadcaster never self-clears) and the stage banner
            # (late page loads land in the correct modal step).
            self._publish_collision_flag(True)
            self._publish_collision_status()
        else:
            # Self-heal the freeze latch while idle (issue #19 finding #4): the broadcaster
            # is a VOLATILE subscriber that never self-clears collision_detected_, so a stale
            # /collision_flag=True latched before a respawn (respawn=True) would freeze the
            # follower forever with no active collision to recover from. Asserting False keeps
            # the broadcaster converged to "no collision" within one tick of the node coming
            # up. Idempotent on the broadcaster (a plain atomic store); the gpio callback and
            # this watchdog share the node default MutuallyExclusiveCallbackGroup, so a trip
            # (which sets _collision_active=True before the next tick) can never race a False
            # out here.
            # Edge case (accepted): if the node respawned MID-collision, _collision_active is
            # lost and this briefly un-freezes a still-pressed obstacle — but the fresh
            # detector re-trips on the next gpio frame (~10 ms) and re-freezes, which is
            # strictly better than the wedge-forever this replaces. Safe because in the
            # shipped OMX stack the leader broadcaster is the ONLY /collision_flag consumer and
            # there is NO competing publisher (the OMY self_collision_node / gravity-comp
            # controller are not spawned — see CLAUDE.md Rule §2). If the OMY path is ever
            # productized, gate this assertion on EduBotics owning the topic.
            self._publish_collision_flag(False)

    # ---- resume --------------------------------------------------------------------------

    def resume_teleop(self):
        """Handle the RESUME_TELEOP command (modal step 2). Returns (success, german_message)."""
        if not self._collision_active:
            return True, 'Keine aktive Kollision — Teleoperation läuft bereits.'
        # Already resyncing (double-click / re-click): don't schedule a second resync.
        if self._collision_resync_timer is not None:
            return True, RESYNC_IN_FLIGHT_MESSAGE_DE

        # 0. STRICT ordering: the follower must have completed the home glide first. One
        #    mental model for students: Hindernis weg -> Follower home -> Leader home -> weiter.
        if not self._collision_homed:
            return False, ('Bitte zuerst auf „Follower in Grundstellung fahren" klicken und '
                           'warten, bis der Follower die Grundstellung erreicht hat.')

        # 1. Proximity check (arm joints only; gripper exempt).
        if not all(j in self._collision_leader_pos for j in ARM_JOINT_NAMES):
            return False, ('Leader-Pose nicht verfügbar — bitte den Leader-Arm prüfen und '
                           'erneut auf „Teleoperation fortsetzen" klicken.')
        too_far = [
            j for idx, j in enumerate(ARM_JOINT_NAMES)
            if abs(self._collision_leader_pos[j] - SAFE_HOME_ARM[idx]) > self._collision_resume_tol
        ]
        if too_far:
            return False, ('Der Leader-Arm ist noch zu weit von der Grundstellung entfernt '
                           f'(Gelenke: {", ".join(too_far)}). Bitte näher an die Grundstellung '
                           'bringen und erneut auf „Teleoperation fortsetzen" klicken.')

        # 2. Quintic resync follower -> leader (small move: follower is at home, leader is near
        #    it). Cancel any stray relax/glide timer first so nothing fires mid-resync.
        self._cancel_relax_timer()
        self._cancel_glide_timer()
        start = {j: self._collision_follower_pos.get(j, self._collision_leader_pos.get(j, 0.0))
                 for j in LEADER_JOINTS}
        target = {j: self._collision_leader_pos.get(j, start[j]) for j in LEADER_JOINTS}
        self._publish_quintic(LEADER_JOINTS, start, target, RESYNC_DURATION_S)

        # 3. Clear the freeze only AFTER the resync completes (non-blocking one-shot). Until then
        #    the watchdog keeps the broadcaster frozen so it can't fight the resync trajectory.
        self._schedule_resync_completion(RESYNC_DURATION_S + 0.5)
        return True, 'Teleoperation wird wiederhergestellt …'

    def force_resume_teleop(self):
        """Handle FORCE_RESUME_TELEOP — the modal step-1 escape hatch (issue #19 finding #4).

        For when the verified safe-home glide cannot complete (e.g. a degraded bus keeps the
        follower from tracking home), which would otherwise wedge ALL operations behind the
        freeze with env-restart the only recovery. Unlike resume_teleop this does NOT require
        the follower to have reached home; instead it does a CONTROLLED, proximity-gated
        quintic resync from the follower's CURRENT pose to the leader, then clears the freeze
        on completion. The proximity gate (leader near the follower's CURRENT pose, not home)
        keeps the move small so the follower can never jump when the broadcaster un-freezes —
        the watchdog holds the freeze through the whole resync exactly like resume_teleop.

        Returns (success, german_message). Does NOT auto-resume an interrupted recording: a
        forced recovery ends the session (the student restarts recording deliberately)."""
        if not self._collision_active:
            return True, 'Keine aktive Kollision — Teleoperation läuft bereits.'
        # Already resyncing (this button stays visible through the resync): no second resync.
        if self._collision_resync_timer is not None:
            return True, RESYNC_IN_FLIGHT_MESSAGE_DE
        if not all(j in self._collision_leader_pos for j in ARM_JOINT_NAMES):
            return False, ('Leader-Pose nicht verfügbar — bitte den Leader-Arm prüfen und '
                           'erneut auf „Trotzdem fortsetzen" klicken.')
        if not all(j in self._collision_follower_pos for j in ARM_JOINT_NAMES):
            return False, ('Follower-Pose nicht verfügbar — bitte kurz warten und erneut auf '
                           '„Trotzdem fortsetzen" klicken.')
        # Proximity gate referenced to the follower's CURRENT pose (it may not be at home):
        # keep the resync move small so the follower can't jump when the freeze clears.
        too_far = [
            j for j in ARM_JOINT_NAMES
            if abs(self._collision_leader_pos[j] - self._collision_follower_pos[j])
            > self._collision_resume_tol
        ]
        if too_far:
            return False, ('Der Leader-Arm ist zu weit von der aktuellen Stellung des Followers '
                           f'entfernt (Gelenke: {", ".join(too_far)}). Bringe den Leader nah an '
                           'die AKTUELLE Stellung des Followers und klicke erneut auf '
                           '„Trotzdem fortsetzen".')

        # Cancel any stray relax/glide timer so nothing fires mid-resync.
        self._cancel_relax_timer()
        self._cancel_glide_timer()
        # A forced recovery ends the interrupted recording (do NOT auto-resume it): clearing
        # the marker makes _on_resync_complete take the plain cleared-status (READY) path.
        self._collision_interrupted_recording = False
        self._collision_interrupted_mode = None
        start = {j: self._collision_follower_pos.get(j, self._collision_leader_pos.get(j, 0.0))
                 for j in LEADER_JOINTS}
        target = {j: self._collision_leader_pos.get(j, start[j]) for j in LEADER_JOINTS}
        self._publish_quintic(LEADER_JOINTS, start, target, RESYNC_DURATION_S)
        self._schedule_resync_completion(RESYNC_DURATION_S + 0.5)
        return True, 'Teleoperation wird wiederhergestellt (Notentriegelung) …'

    def _schedule_resync_completion(self, delay):
        if self._collision_resync_timer is not None:
            self._collision_resync_timer.cancel()
        self._collision_resync_timer = self.create_timer(delay, self._on_resync_complete)

    def _on_resync_complete(self):
        self._cancel_relax_timer()
        self._cancel_glide_timer()
        if self._collision_resync_timer is not None:
            self._collision_resync_timer.cancel()
            self._collision_resync_timer = None
        # Clear local state BEFORE publishing False so the watchdog can't re-assert True.
        self._collision_active = False
        self._collision_homing = False
        self._collision_homed = False
        self._collision_overload_joints = []
        self._collision_detector.reset()
        self._publish_collision_flag(False)

        # Seamlessly resume a recording the collision interrupted: re-arm the SAME session so
        # the student continues where they left off. We deliberately do NOT publish the cleared
        # READY status in this branch — the resumed recording timer owns /task/status from its
        # first tick (phase=RESETTING, the Rücksetzzeit), and a READY published here would race
        # that on the shared /task/status topic and flicker the React UI back to "Bereit"
        # (re-enabling the Start button). The CollisionModal still closes correctly because it
        # dismisses on the first NON-collision phase tick, which RESETTING satisfies.
        resumed = False
        if self._collision_interrupted_recording:
            resumed = self._resume_interrupted_recording()
        self._collision_interrupted_recording = False
        self._collision_interrupted_mode = None
        if resumed:
            self.get_logger().info(
                '[KOLLISION] Teleoperation wiederhergestellt — Aufnahme wird fortgesetzt.')
        else:
            self._publish_cleared_status()
            self.get_logger().info('[KOLLISION] Teleoperation wiederhergestellt.')

    def _resume_interrupted_recording(self):
        """Re-arm the recording the collision interrupted, keeping the SAME DataManager.

        Returns True when the recording timer was restarted. The DataManager is NOT rebuilt
        (that would drop every prior episode + the dataset object); re_record() already left
        the FSM at 'reset' with the episode count preserved, so the next ticks play out the
        Rücksetzzeit and re-record the interrupted episode. Defensive: if the recording
        objects are gone (a teardown raced us), fall back to the plain cleared-status path."""
        mode = self._collision_interrupted_mode or 'collection'
        timer_manager = getattr(self, 'timer_manager', None)
        data_manager = getattr(self, 'data_manager', None)
        if timer_manager is None or data_manager is None:
            return False
        try:
            # Drop the stale camera/follower/leader caches + F65 sync rings captured during the
            # (multi-second) relax/home/resync motion — otherwise the first resumed tick could
            # pair a fresh frame with a stale-stamped one (the silent-misalignment class that
            # clear_latest_data was built to prevent; see communicator.clear_latest_data).
            communicator = getattr(self, 'communicator', None)
            if communicator is not None:
                communicator.clear_latest_data()
            # Restart the DEFAULT_TOPIC_TIMEOUT "waiting for data" window so the first resumed
            # tick (with momentarily-empty caches) does not instantly trip the timeout and kill
            # the recording.
            self.start_recording_time = time.perf_counter()
            self.on_recording = True
            # TimerManager kept the freq+callback from set_timer at record start; start()
            # recreates the timer destroyed at trip time.
            timer_manager.start(timer_name=mode)
            return True
        except Exception as exc:  # noqa: BLE001 - never let resume crash the guard
            self.get_logger().error(
                f'[KOLLISION] Aufnahme-Fortsetzung fehlgeschlagen: {exc}')
            self.on_recording = False
            return False

    def _best_effort_reboot(self, joints):
        """Reboot firmware-latched Dynamixels, then re-enable torque. Fully guarded; rare path."""
        try:
            from dynamixel_interfaces.srv import RebootDxl
        except ImportError:
            self.get_logger().warning(
                '[KOLLISION] dynamixel_interfaces unavailable — cannot auto-reboot latched '
                f'joints {joints}. If the arm does not move, restart the environment.')
            return
        try:
            from std_srvs.srv import SetBool
            reboot_client = self.create_client(RebootDxl, REBOOT_DXL_SERVICE)
            torque_client = self.create_client(SetBool, SET_TORQUE_SERVICE)
            if reboot_client.wait_for_service(timeout_sec=1.0):
                for joint in joints:
                    dxl_id = JOINT_TO_DXL_ID.get(joint)
                    req = RebootDxl.Request()
                    # Set an id field if the srv exposes one (shape varies by version).
                    for field in ('id', 'dxl_id', 'ids'):
                        if hasattr(req, field) and dxl_id is not None:
                            setattr(req, field, [dxl_id] if field == 'ids' else dxl_id)
                            break
                    reboot_client.call_async(req)
                self.get_logger().warning(f'[KOLLISION] reboot_dxl requested for {joints}.')
            if torque_client.wait_for_service(timeout_sec=1.0):
                req = SetBool.Request()
                req.data = True
                torque_client.call_async(req)
        except Exception as exc:  # noqa: BLE001 - best effort only
            self.get_logger().warning(f'[KOLLISION] best-effort reboot failed: {exc}')


def _env_float(name, default):
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == '':
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default

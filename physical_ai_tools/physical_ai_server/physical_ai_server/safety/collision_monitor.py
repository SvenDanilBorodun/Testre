# Copyright 2026 EduBotics
#
# Teleoperation force/collision e-stop — ORCHESTRATION mixin for PhysicalAIServer.
#
# This mixin owns the ROS surface of the EduBotics teleop collision guard (a Rule §2 software
# safety guard, scoped to teleop/recording only). The pure detection logic lives in
# collision_detector.py (unit-tested without ROS); this layer wires it to the live system.
#
# Data flow:
#   open_manipulator gpio_command_controller --/gpio_command_controller/gpio_states-->
#     _gpio_states_cb  ->  CollisionDetector.update(currents[A], velocities[rad/s], err_bits,
#                                                    mode_is_inference=self.on_inference)
#   on trip -> _trigger_collision_stop (ORDER MATTERS):
#     1. /collision_flag = True  (freeze the leader broadcaster BEFORE the home publish, else
#        its 100 Hz stream overwrites the home setpoint; a 5 Hz watchdog re-asserts the latch)
#     2. glide the follower OFF the object to the safe home pose (quintic JointTrajectory to
#        /leader/joint_trajectory — the arm_controller subscribes there directly, unaffected by
#        the flag, so the home command lands while the broadcaster stays frozen). The gripper is
#        held at its current position (don't drop a grasped object).
#     3. if recording: discard the in-progress (contaminated) episode via data_manager.re_record()
#        and halt capture — the safe-home glide must NEVER be recorded (Rule §2).
#     4. surface phase=COLLISION on /task/status (re-asserted by the watchdog for late page loads).
#   resume (RESUME_TELEOP /task/command): proximity-check the leader vs the home pose (refuse +
#     re-prompt if too far), best-effort reboot any firmware-latched joints, quintic resync
#     follower->leader, then clear /collision_flag and publish phase=READY.
#
# Inference safety (Rule §2): the detector is gated off when self.on_inference is True, AND
# structurally the leader broadcaster (the only consumer of /collision_flag) does not run the
# inference action path (inference publishes JointTrajectory directly), so the guard is doubly
# invisible to inference and never reshapes the recorded->replayed action distribution.

import os

from physical_ai_interfaces.msg import TaskStatus
from physical_ai_server.safety.collision_detector import (
    build_detector_from_env,
    PRESENT_CURRENT_A_PER_LSB,
)
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import JointState
from std_msgs.msg import Bool
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint

# control_msgs (DynamicJointState) is published by the open_manipulator gpio_command_controller.
# Guard the import so a base image without control_msgs degrades to "no detection" (fail-open)
# instead of crashing the whole node. package.xml declares control_msgs so the image ships it.
try:
    from control_msgs.msg import DynamicJointState
    _HAVE_DYNAMIC_JOINT_STATE = True
except ImportError:  # pragma: no cover - depends on image contents
    DynamicJointState = None
    _HAVE_DYNAMIC_JOINT_STATE = False

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

COLLISION_HOME_DURATION_S = 2.5
RESYNC_DURATION_S = 3.0
WATCHDOG_PERIOD_S = 0.2
DEFAULT_RESUME_TOL_RAD = 0.30

COLLISION_MESSAGE_DE = (
    'STOPP — Kollision erkannt: Der Arm wurde gegen ein Hindernis gedrückt und sicher in die '
    'Grundstellung gefahren. Bringe den Leader-Arm in die Nähe der Grundstellung des Followers '
    'und klicke dann auf „Teleoperation neu starten".'
)


class CollisionMonitorMixin:
    """Mix into PhysicalAIServer(Node). Call _init_collision_monitor() from __init__."""

    def _init_collision_monitor(self):
        # Collision-stop state.
        self._collision_active = False
        self._collision_overload_joints = []
        self._collision_message = COLLISION_MESSAGE_DE
        self._collision_resync_timer = None
        # Latest follower/leader joint snapshots (rad / rad/s), keyed by joint name.
        self._collision_follower_pos = {}
        self._collision_follower_vel = {}
        self._collision_leader_pos = {}

        self._collision_resume_tol = _env_float(
            'EDUBOTICS_COLLISION_RESUME_TOL_RAD', DEFAULT_RESUME_TOL_RAD)
        gpio_topic = os.environ.get(
            'EDUBOTICS_COLLISION_GPIO_TOPIC', GPIO_STATES_DEFAULT_TOPIC) or GPIO_STATES_DEFAULT_TOPIC

        # Build the pure detector from the collision-guard env vars (defaults are safe).
        self._collision_detector = build_detector_from_env(
            os.environ.get, ARM_JOINT_NAMES, update_rate_hz=100.0)

        # Publishers. /collision_flag is RELIABLE + TRANSIENT_LOCAL so a (re)starting leader
        # broadcaster latches the current value; the watchdog re-asserts it regardless.
        latched_qos = QoSProfile(
            depth=10, reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self._collision_flag_pub = self.create_publisher(
            Bool, COLLISION_FLAG_TOPIC, latched_qos)
        # Mirrors the entrypoint Phase-3 / jetson_agent safe-home publisher QoS.
        self._collision_leader_traj_pub = self.create_publisher(
            JointTrajectory, LEADER_TRAJECTORY_TOPIC, latched_qos)
        # Own /task/status publisher so collision notifications work even when the recording
        # timer (the usual status source) is not running (i.e. free teleop, or after we halt it).
        self._collision_status_pub = self.create_publisher(TaskStatus, '/task/status', 10)

        # 5 Hz watchdog: re-assert the latch + re-publish the banner while stopped.
        self._collision_watchdog = self.create_timer(
            WATCHDOG_PERIOD_S, self._collision_watchdog_cb)

        # Always know the follower/leader poses (needed for the home glide + resync).
        self.create_subscription(
            JointState, JOINT_STATES_TOPIC, self._collision_follower_state_cb, 10)
        self.create_subscription(
            JointState, LEADER_JOINT_STATES_TOPIC, self._collision_leader_state_cb, 10)

        if not _HAVE_DYNAMIC_JOINT_STATE:
            self.get_logger().warning(
                '[KOLLISION] control_msgs/DynamicJointState unavailable — teleop collision '
                'detection DISABLED (fail-open). The arm still works; force protection is off.')
            return
        if not self._collision_detector.enabled:
            self.get_logger().info(
                '[KOLLISION] Teleop collision guard disabled via EDUBOTICS_COLLISION_ENABLED=0.')
        self.create_subscription(
            DynamicJointState, gpio_topic, self._gpio_states_cb, 10)
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

    # ---- detection -----------------------------------------------------------------------

    def _gpio_states_cb(self, msg):
        if self._collision_active:
            return  # already stopped; ignore until resume
        currents, err_bits = {}, {}
        for idx, gpio_name in enumerate(msg.joint_names):
            joint = GPIO_NAME_TO_JOINT.get(gpio_name)
            if joint is None or idx >= len(msg.interface_values):
                continue
            iv = msg.interface_values[idx]
            name_to_val = dict(zip(iv.interface_names, iv.values))
            raw_cur = name_to_val.get('Present Current')
            if raw_cur is not None:
                # Present Current is exported in raw signed counts (dxl model unit scale 1.0);
                # 2.69 mA/LSB on XM430-W350. Confirm units on the rig (Windows runbook).
                currents[joint] = float(raw_cur) * PRESENT_CURRENT_A_PER_LSB
            raw_err = name_to_val.get('Hardware Error Status')
            if raw_err is not None:
                err_bits[joint] = int(round(float(raw_err)))
        velocities = {j: self._collision_follower_vel.get(j, 0.0) for j in ARM_JOINT_NAMES}

        result = self._collision_detector.update(
            currents, velocities, err_bits, mode_is_inference=self.on_inference)
        if result.tripped:
            self._trigger_collision_stop(result)

    def _trigger_collision_stop(self, result):
        self._collision_active = True
        self._collision_overload_joints = list(result.latched_overload)
        self.get_logger().warning(f'[KOLLISION] Stop ausgelöst: {result.reason}')

        # 1. Freeze the leader broadcaster FIRST.
        self._publish_collision_flag(True)

        # 2. Glide the follower off the object to the safe home pose (gripper held).
        self._publish_safe_home_glide()

        # 3. Discard the in-progress episode + halt capture so the glide is never recorded.
        if getattr(self, 'on_recording', False):
            try:
                self.data_manager.re_record()
            except Exception as exc:  # noqa: BLE001 - never let cleanup crash the guard
                self.get_logger().error(f'[KOLLISION] re_record failed: {exc}')
            self.on_recording = False
            try:
                self.timer_manager.stop(timer_name=self.operation_mode)
            except Exception as exc:  # noqa: BLE001
                self.get_logger().error(f'[KOLLISION] timer stop failed: {exc}')

        # 4. Tell the student (re-asserted by the watchdog).
        self._publish_collision_status()

    # ---- actuation helpers ---------------------------------------------------------------

    def _publish_collision_flag(self, value):
        msg = Bool()
        msg.data = bool(value)
        self._collision_flag_pub.publish(msg)

    def _publish_safe_home_glide(self):
        start = {j: self._collision_follower_pos.get(j, 0.0) for j in LEADER_JOINTS}
        target = dict(start)
        for idx, joint in enumerate(ARM_JOINT_NAMES):
            target[joint] = SAFE_HOME_ARM[idx]
        # gripper_joint_1 stays at its current position (don't release a grasped object).
        self._publish_quintic(LEADER_JOINTS, start, target, COLLISION_HOME_DURATION_S)

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

    def _publish_collision_status(self):
        status = TaskStatus()
        status.phase = TaskStatus.COLLISION
        status.current_task_instruction = self._collision_message
        # Leave .error empty: the React /task/status handler short-circuits to a transient
        # toast on a non-empty error. The collision banner rides phase=COLLISION instead.
        self._collision_status_pub.publish(status)

    def _publish_cleared_status(self):
        status = TaskStatus()
        status.phase = TaskStatus.READY
        self._collision_status_pub.publish(status)

    def _collision_watchdog_cb(self):
        if self._collision_active:
            # Re-assert the latch (broadcaster never self-clears) and the banner (late page loads).
            self._publish_collision_flag(True)
            self._publish_collision_status()

    # ---- resume --------------------------------------------------------------------------

    def resume_teleop(self):
        """Handle the RESUME_TELEOP command. Returns (success, german_message)."""
        if not self._collision_active:
            return True, 'Keine aktive Kollision — Teleoperation läuft bereits.'

        # 1. Proximity check (arm joints only; gripper exempt).
        if not all(j in self._collision_leader_pos for j in ARM_JOINT_NAMES):
            return False, ('Leader-Pose nicht verfügbar — bitte den Leader-Arm prüfen und '
                           'erneut auf „Teleoperation neu starten" klicken.')
        too_far = [
            j for idx, j in enumerate(ARM_JOINT_NAMES)
            if abs(self._collision_leader_pos[j] - SAFE_HOME_ARM[idx]) > self._collision_resume_tol
        ]
        if too_far:
            return False, ('Der Leader-Arm ist noch zu weit von der Grundstellung entfernt '
                           f'(Gelenke: {", ".join(too_far)}). Bitte näher an die Grundstellung '
                           'bringen und erneut auf „Teleoperation neu starten" klicken.')

        # 2. Best-effort recovery of any firmware-latched (Overload) joints. The software guard
        #    normally trips well before the firmware latches, so this path is rare; it is fully
        #    guarded and never blocks resume. (Bench-validate on the rig — Windows runbook.)
        if self._collision_overload_joints:
            self._best_effort_reboot(self._collision_overload_joints)

        # 3. Quintic resync follower -> leader (small move: follower is at home, leader is near it).
        start = {j: self._collision_follower_pos.get(j, self._collision_leader_pos.get(j, 0.0))
                 for j in LEADER_JOINTS}
        target = {j: self._collision_leader_pos.get(j, start[j]) for j in LEADER_JOINTS}
        self._publish_quintic(LEADER_JOINTS, start, target, RESYNC_DURATION_S)

        # 4. Clear the freeze only AFTER the resync completes (non-blocking one-shot). Until then
        #    the watchdog keeps the broadcaster frozen so it can't fight the resync trajectory.
        self._schedule_resync_completion(RESYNC_DURATION_S + 0.5)
        return True, 'Teleoperation wird wiederhergestellt …'

    def _schedule_resync_completion(self, delay):
        if self._collision_resync_timer is not None:
            self._collision_resync_timer.cancel()
        self._collision_resync_timer = self.create_timer(delay, self._on_resync_complete)

    def _on_resync_complete(self):
        if self._collision_resync_timer is not None:
            self._collision_resync_timer.cancel()
            self._collision_resync_timer = None
        # Clear local state BEFORE publishing False so the watchdog can't re-assert True.
        self._collision_active = False
        self._collision_overload_joints = []
        self._collision_detector.reset()
        self._publish_collision_flag(False)
        self._publish_cleared_status()
        self.get_logger().info('[KOLLISION] Teleoperation wiederhergestellt.')

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

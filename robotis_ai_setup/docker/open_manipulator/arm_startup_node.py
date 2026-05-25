#!/usr/bin/env python3
# Copyright 2025 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
#
# On-demand robot-arm startup node (student PC, leader+follower).
#
# WHY THIS EXISTS
# ---------------
# Historically `entrypoint_omx.sh` brought the follower fully alive at
# container boot: it launched both arms and then ran a 3 s quintic sync that
# drove the follower to the leader's pose. That had two problems:
#   1. The arm "came alive" (torque on, motion) before the student did
#      anything in the dashboard — there was no in-app control over it.
#   2. The boot sync SOFT-FAILED and continued anyway, so a student could
#      record on a structurally mis-synced arm. Imitation learning is built
#      on the observation.state(follower) <-> action(leader) correspondence,
#      so a sync offset bakes a wrong correspondence into every dataset and
#      any policy trained on it.
#
# This node moves that motion OUT of boot and behind an explicit student
# action, and makes the sync verification a BLOCKING GATE:
#   * The follower ROS stack still launches at boot, but torque stays OFF
#     (xacro `disable_torque_at_init: true`) — the arm is limp, no motion.
#   * The student clicks "Roboter starten" in the dashboard, which calls the
#     /edubotics/start_arm service (relayed via rosbridge).
#   * We then HOME the follower to a known reference pose, READ the leader's
#     CURRENT pose, SYNC the follower to it, and VERIFY convergence. Only a
#     passing verify reports `ready` — which is the single gate the frontend
#     uses to unlock Aufnahme / Inferenz / Roboter Studio. A failed sync
#     surfaces the per-joint error with German guidance and can be retried
#     without restarting the environment.
#
# The quintic math and the verification thresholds (0.30 rad on arm joints,
# gripper exempt, >=50% commanded motion) are lifted verbatim from the old
# entrypoint Phase-3 block — see CLAUDE.md Rule §2. What changed is WHEN it
# runs (on demand) and THAT it now gates readiness, not the thresholds.
#
# Interface (stock message types only — no physical_ai_interfaces, so no
# CMake / interfaces-validate churn):
#   * Service /edubotics/start_arm    std_srvs/srv/Trigger
#                                     resp {success: bool, message: str(de)}
#   * Topic   /edubotics/arm_state    std_msgs/msg/String  (latched)
#       data = JSON {phase, percent, message, error, per_joint_err}
#       phase in idle|homing|reading_leader|syncing|verifying|ready|error
#
# This file is COPY'd into the open-manipulator image like
# camera_ingest_node.py / identify_arm.py — it is a brand-new standalone
# rclpy script with no upstream file to replace, so it is NOT an
# apply_overlay target (Rule §3).

import json
import os
import threading
import time

import rclpy
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

from sensor_msgs.msg import JointState
from std_msgs.msg import String
from std_srvs.srv import Trigger
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint

# Joint order matches the leader-pose read in the old entrypoint and the
# follower's arm_controller. gripper_joint_1 is index 5 (verification-exempt).
JOINTS = ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'gripper_joint_1']
ARM_IDX = [0, 1, 2, 3, 4]  # joint1..joint5; gripper (5) excluded from verify

# Verification thresholds — DO NOT loosen without user agreement (Rule §2).
VERIFY_TOL_RAD = 0.30      # per-joint position error ceiling on arm joints
VERIFY_MOTION_FRAC = 0.5   # require >=50% of the commanded delta to be traversed
TRAJ_POINTS = 50           # quintic trajectory resolution


def _parse_pose(env_value, fallback):
    """Parse a comma-separated 6-float pose from an env var, else fallback."""
    if not env_value:
        return list(fallback)
    try:
        vals = [float(x) for x in env_value.split(',')]
        if len(vals) == len(JOINTS):
            return vals
    except (ValueError, AttributeError):
        pass
    return list(fallback)


class ArmStartupNode(Node):
    def __init__(self):
        super().__init__('arm_startup')

        # --- Tunables (forwarded via docker-compose.yml::environment) ---
        self.home_pose = _parse_pose(
            os.environ.get('EDUBOTICS_HOME_POSE'), [0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        self.sync_base_sec = self._env_float('EDUBOTICS_ARM_SYNC_BASE_SEC', 3.0)
        self.sync_max_vel = self._env_float('EDUBOTICS_ARM_SYNC_MAX_VEL', 0.6)  # rad/s
        self.sync_attempts = max(1, self._env_int('EDUBOTICS_ARM_SYNC_RETRIES', 2))

        # --- Live pose state, updated by the subscription threads ---
        self._follower_pos = None
        self._leader_pos = None

        # --- Concurrency: a flag so two clicks can't run two sequences ---
        self._busy = threading.Lock()
        self._running = False

        # Reentrant group so /joint_states + /leader/joint_states keep updating
        # the cached poses WHILE the service callback runs its blocking
        # home->sync->verify sequence on another executor thread.
        sub_group = ReentrantCallbackGroup()
        srv_group = MutuallyExclusiveCallbackGroup()

        self.create_subscription(
            JointState, '/joint_states', self._on_follower, 10,
            callback_group=sub_group)
        self.create_subscription(
            JointState, '/leader/joint_states', self._on_leader, 10,
            callback_group=sub_group)

        # Trajectory rail — the follower's arm_controller is remapped to
        # subscribe here (see omx_f_follower_ai.launch.py ~line 144).
        traj_qos = QoSProfile(
            depth=10,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=ReliabilityPolicy.RELIABLE)
        self._traj_pub = self.create_publisher(
            JointTrajectory, '/leader/joint_trajectory', traj_qos)

        # Latched state topic so a late-subscribing frontend (rosbridge) sees
        # the current phase immediately on connect.
        state_qos = QoSProfile(
            depth=1,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=ReliabilityPolicy.RELIABLE)
        self._state_pub = self.create_publisher(
            String, '/edubotics/arm_state', state_qos)

        self.create_service(
            Trigger, '/edubotics/start_arm', self._on_start_arm,
            callback_group=srv_group)

        self._publish_state(
            'idle', 0,
            'Bereit. Klicke „Roboter starten", um den Arm zu referenzieren.')
        self.get_logger().info(
            'arm_startup ready — /edubotics/start_arm advertised, '
            'follower limp until triggered.')

    # ------------------------------------------------------------------ env
    def _env_float(self, name, default):
        try:
            return float(os.environ.get(name, default))
        except (ValueError, TypeError):
            return default

    def _env_int(self, name, default):
        try:
            return int(os.environ.get(name, default))
        except (ValueError, TypeError):
            return default

    # ----------------------------------------------------------- callbacks
    def _on_follower(self, msg):
        if set(JOINTS).issubset(set(msg.name)):
            self._follower_pos = [msg.position[msg.name.index(j)] for j in JOINTS]

    def _on_leader(self, msg):
        if set(JOINTS).issubset(set(msg.name)):
            self._leader_pos = [msg.position[msg.name.index(j)] for j in JOINTS]

    # -------------------------------------------------------------- helpers
    def _publish_state(self, phase, percent, message, error='', per_joint_err=None):
        payload = {
            'phase': phase,
            'percent': int(max(0, min(100, percent))),
            'message': message,
            'error': error,
            'per_joint_err': per_joint_err or [],
        }
        msg = String()
        msg.data = json.dumps(payload)
        self._state_pub.publish(msg)
        if error:
            self.get_logger().error(f'[arm_state] {phase}: {error}')
        else:
            self.get_logger().info(f'[arm_state] {phase} ({percent}%): {message}')

    def _wait_for(self, getter, timeout_s):
        """Block until getter() is not None, or timeout. Returns the value/None."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            val = getter()
            if val is not None:
                return val
            time.sleep(0.05)
        return getter()

    def _build_traj(self, start, target, duration):
        """50-point quintic (zero vel+accel at both endpoints) — verbatim math
        from the old entrypoint Phase-3 block."""
        deltas = [t - s for s, t in zip(start, target)]
        traj = JointTrajectory()
        traj.joint_names = list(JOINTS)
        for i in range(TRAJ_POINTS):
            t = (i + 1) / TRAJ_POINTS
            s = 10 * t**3 - 15 * t**4 + 6 * t**5
            s_dot = (30 * t**2 - 60 * t**3 + 30 * t**4) / duration
            s_ddot = (60 * t - 180 * t**2 + 120 * t**3) / (duration * duration)
            pt = JointTrajectoryPoint()
            pt.positions = [p + d * s for p, d in zip(start, deltas)]
            pt.velocities = [d * s_dot for d in deltas]
            pt.accelerations = [d * s_ddot for d in deltas]
            secs = duration * t
            pt.time_from_start.sec = int(secs)
            pt.time_from_start.nanosec = int((secs % 1) * 1e9)
            traj.points.append(pt)
        return traj, deltas

    def _scaled_duration(self, start, target):
        """Bound peak velocity: a larger gap takes proportionally LONGER, never
        faster. Keeps the move slow + smooth so the controller doesn't stall
        partway (a root cause of the boot-sync failures)."""
        max_delta = max((abs(t - s) for s, t in zip(start, target)), default=0.0)
        return max(self.sync_base_sec, max_delta / max(0.05, self.sync_max_vel))

    def _animate_wait(self, seconds, p_start, p_end, phase, message):
        """Sleep `seconds` while smoothly ramping the reported percent so the
        frontend progress bar animates during the physical motion."""
        steps = max(1, int(seconds / 0.2))
        for i in range(steps):
            frac = (i + 1) / steps
            self._publish_state(
                phase, p_start + (p_end - p_start) * frac, message)
            time.sleep(seconds / steps)

    def _verify(self, sync_start, target, deltas):
        """Poll the follower for up to 2 s after the move and check it reached
        the leader. Returns (ok, err_list, reason). gripper exempt; arm joints
        need err < tol AND >=50% of any meaningful commanded motion."""
        deadline = time.monotonic() + 2.0
        err = [9.9] * len(JOINTS)
        while time.monotonic() < deadline:
            cur = self._follower_pos
            if cur is not None:
                err = [abs(c - t) for c, t in zip(cur, target)]
                motion = [abs(c - s) for c, s in zip(cur, sync_start)]
                motion_ok = all(
                    not (abs(deltas[i]) > VERIFY_TOL_RAD
                         and motion[i] < VERIFY_MOTION_FRAC * abs(deltas[i]))
                    for i in ARM_IDX)
                if all(err[i] < VERIFY_TOL_RAD for i in ARM_IDX) and motion_ok:
                    return True, err, ''
            time.sleep(0.1)
        cur = self._follower_pos or sync_start
        motion = [abs(c - s) for c, s in zip(cur, sync_start)]
        stale = [JOINTS[i] for i in ARM_IDX
                 if abs(deltas[i]) > VERIFY_TOL_RAD
                 and motion[i] < VERIFY_MOTION_FRAC * abs(deltas[i])]
        reason = (f'Folger bewegt sich nicht auf: {stale}' if stale
                  else 'Folger erreicht Leader-Pose nicht')
        return False, err, reason

    # --------------------------------------------------------- service body
    def _on_start_arm(self, request, response):
        if not self._busy.acquire(blocking=False):
            response.success = False
            response.message = 'Arm-Start läuft bereits — bitte warten.'
            return response
        self._running = True
        try:
            response.success, response.message = self._run_startup()
        except Exception as exc:  # noqa: BLE001 — never let the service crash the node
            self.get_logger().error(f'arm startup crashed: {exc}')
            self._publish_state(
                'error', 0, 'Unerwarteter Fehler beim Arm-Start.',
                error=str(exc))
            response.success = False
            response.message = f'Unerwarteter Fehler: {exc}'
        finally:
            self._running = False
            self._busy.release()
        return response

    def _run_startup(self):
        # --- Phase 1: Home the follower to a known reference pose ---------
        self._publish_state('homing', 2, 'Folger-Arm wird referenziert…')
        start = self._wait_for(lambda: self._follower_pos, 10.0)
        if start is None:
            msg = 'Keine Folger-Gelenkdaten (/joint_states). Hardware prüfen.'
            self._publish_state('error', 0, msg, error=msg)
            return False, msg
        home_dur = self._scaled_duration(start, self.home_pose)
        traj, _ = self._build_traj(start, self.home_pose, home_dur)
        self._traj_pub.publish(traj)  # first trajectory enables follower torque
        self._animate_wait(
            home_dur + 0.5, 5, 40, 'homing',
            'Folger-Arm fährt in die Referenzposition…')

        # --- Phase 2: Read the leader's CURRENT pose ---------------------
        self._publish_state('reading_leader', 45, 'Leader-Position wird gelesen…')
        leader = self._wait_for(lambda: self._leader_pos, 10.0)
        if leader is None:
            msg = ('Keine Leader-Gelenkdaten (/leader/joint_states). '
                   'Ist der Leader-Arm verbunden?')
            self._publish_state('error', 0, msg, error=msg)
            return False, msg

        # --- Phase 3+4: Sync to leader, with blocking verification -------
        last_err = None
        for attempt in range(1, self.sync_attempts + 1):
            sync_start = self._wait_for(lambda: self._follower_pos, 5.0) or self.home_pose
            leader = self._leader_pos or leader  # use the freshest leader pose
            dur = self._scaled_duration(sync_start, leader)
            traj, deltas = self._build_traj(sync_start, leader, dur)
            attempt_note = '' if attempt == 1 else f' (Versuch {attempt}/{self.sync_attempts})'
            self._traj_pub.publish(traj)
            self._animate_wait(
                dur + 0.5, 50, 90, 'syncing',
                f'Folger synchronisiert sich mit dem Leader…{attempt_note}')

            self._publish_state('verifying', 92, 'Synchronisation wird geprüft…')
            ok, err, reason = self._verify(sync_start, leader, deltas)
            if ok:
                self._publish_state(
                    'ready', 100,
                    'Roboter bereit. Folger und Leader sind synchronisiert.')
                return True, 'Roboter bereit.'
            last_err = (err, reason)
            self.get_logger().warn(
                f'sync attempt {attempt}/{self.sync_attempts} failed: {reason} '
                f'err={[round(e, 3) for e in err]}')

        err, reason = last_err if last_err else ([], 'unbekannt')
        msg = ('Synchronisation fehlgeschlagen. Bringe beide Arme vorsichtig '
               'in eine ähnliche Ausgangsposition und versuche es erneut.')
        self._publish_state(
            'error', 0, msg, error=reason,
            per_joint_err=[round(e, 3) for e in err])
        return False, msg


def main():
    rclpy.init()
    node = ArmStartupNode()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()

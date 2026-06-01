#!/usr/bin/env python3
# Copyright 2026 EduBotics
#
# Operator helper: measure the OMX follower's no-contact per-joint Present Current envelope and
# print ready-to-paste EDUBOTICS_COLLISION_CURRENT_J* values for the teleop force/collision
# e-stop. Run ONCE per rig (the servos and friction are the same across classroom rigs, so one
# calibration generalizes). This refines the conservative built-in defaults to cut false trips.
#
# It runs INSIDE the open_manipulator container (where /gpio_command_controller/gpio_states and
# the Dynamixel bus live) — it cannot run through Docker on macOS (no USB passthrough). Typical:
#
#   docker compose -f robotis_ai_setup/docker/docker-compose.yml exec open_manipulator \
#       python3 /usr/local/bin/calibrate_collision_currents.py
#
# Procedure: with the follower powered and NO object contact, move the arm slowly through its
# full normal workspace for the measurement window. The script records the per-joint current
# envelope and prints a threshold = max(p95 * margin, floor), capped below stall (~2.3 A), as
# .env lines. Paste them into the EduBotics .env and restart the stack.
#
# Standalone rclpy; intentionally reads NO EDUBOTICS_* env vars (args/topic only) so it stays
# out of the ci.yml env-forwarding-guard surface.

import argparse
import sys
import time

import rclpy
from rclpy.node import Node

try:
    from control_msgs.msg import DynamicJointState
except ImportError:
    print('[FEHLER] control_msgs/DynamicJointState nicht verfügbar — läuft dieses Skript im '
          'open_manipulator-Container?', file=sys.stderr)
    sys.exit(1)

# Raw Present Current count -> Ampere for XM430-W350 (2.69 mA/LSB). Must match
# collision_detector.PRESENT_CURRENT_A_PER_LSB.
PRESENT_CURRENT_A_PER_LSB = 0.00269
GPIO_NAME_TO_JOINT = {
    'dxl11': 'joint1', 'dxl12': 'joint2', 'dxl13': 'joint3',
    'dxl14': 'joint4', 'dxl15': 'joint5',
}
ARM_JOINTS = ('joint1', 'joint2', 'joint3', 'joint4', 'joint5')
STALL_CURRENT_A = 2.3  # XM430-W350 stall; thresholds are capped safely below this.


class CalibrationNode(Node):
    def __init__(self, topic):
        super().__init__('collision_calibration')
        self._samples = {j: [] for j in ARM_JOINTS}
        self.create_subscription(DynamicJointState, topic, self._cb, 10)

    def _cb(self, msg):
        for idx, gpio_name in enumerate(msg.joint_names):
            joint = GPIO_NAME_TO_JOINT.get(gpio_name)
            if joint is None or idx >= len(msg.interface_values):
                continue
            iv = msg.interface_values[idx]
            name_to_val = dict(zip(iv.interface_names, iv.values))
            raw = name_to_val.get('Present Current')
            if raw is not None:
                self._samples[joint].append(abs(float(raw) * PRESENT_CURRENT_A_PER_LSB))

    def sample_count(self):
        return sum(len(v) for v in self._samples.values())


def _p95(values):
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]


def main():
    parser = argparse.ArgumentParser(description='Calibrate teleop collision current thresholds.')
    parser.add_argument('--duration', type=float, default=30.0, help='measurement seconds')
    parser.add_argument('--margin', type=float, default=1.5, help='multiplier above the p95 envelope')
    parser.add_argument('--floor', type=float, default=1.0, help='minimum threshold (A)')
    parser.add_argument('--cap', type=float, default=2.0, help='maximum threshold (A), below stall')
    parser.add_argument('--topic', default='/gpio_command_controller/gpio_states')
    args = parser.parse_args()

    rclpy.init()
    node = CalibrationNode(args.topic)
    print('========================================')
    print('EduBotics — Kollisions-Schwellwert-Kalibrierung')
    print('========================================')
    print(f'Bewege den Follower-Arm {args.duration:.0f} s lang LANGSAM durch seinen gesamten')
    print('normalen Arbeitsbereich — OHNE den Arm gegen ein Hindernis zu drücken.')
    print(f'Lese Topic: {args.topic}')
    print('Start in 3 s …')
    time.sleep(3.0)
    print('AUFNAHME LÄUFT — jetzt bewegen!')

    deadline = time.monotonic() + args.duration
    while rclpy.ok() and time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=0.1)

    count = node.sample_count()
    if count == 0:
        print('[FEHLER] Keine Strommesswerte empfangen. Prüfe: läuft der gpio_command_controller '
              f'und stimmt das Topic ({args.topic})? (ros2 topic list)', file=sys.stderr)
        node.destroy_node()
        rclpy.shutdown()
        sys.exit(2)

    print('')
    print(f'Fertig — {count} Messwerte erfasst. Empfohlene Schwellwerte:')
    print('# --- in die EduBotics .env einfügen und Stack neu starten ---')
    env_index = {'joint1': 1, 'joint2': 2, 'joint3': 3, 'joint4': 4, 'joint5': 5}
    for joint in ARM_JOINTS:
        envelope = _p95(node._samples[joint])
        threshold = max(envelope * args.margin, args.floor)
        threshold = min(threshold, args.cap)
        if envelope >= STALL_CURRENT_A * 0.7:
            print(f'#   WARNUNG: {joint} no-contact p95 = {envelope:.2f} A ist hoch — '
                  'evtl. Reibung/Last prüfen.')
        print(f'EDUBOTICS_COLLISION_CURRENT_J{env_index[joint]}={threshold:.2f}'
              f'   # no-contact p95={envelope:.2f} A')
    print('# Velocity-Gate / Debounce bei Bedarf anpassen:')
    print('# EDUBOTICS_COLLISION_VELOCITY_GATE=0.05')
    print('# EDUBOTICS_COLLISION_DEBOUNCE_MS=150')

    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()

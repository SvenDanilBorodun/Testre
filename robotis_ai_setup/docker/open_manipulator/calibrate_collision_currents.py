#!/usr/bin/env python3
# Copyright 2026 EduBotics
#
# Operator helper: measure the OMX follower's no-contact per-joint motor-effort envelope and
# print ready-to-paste EDUBOTICS_COLLISION_EFFORT_J* values for the teleop force/collision
# e-stop. Run ONCE per rig (the servos and friction are the same across classroom rigs, so one
# calibration generalizes). This refines the conservative built-in defaults to cut false trips.
#
# It runs INSIDE the open_manipulator container (where /gpio_command_controller/gpio_states and
# the Dynamixel bus live) — it cannot run through Docker on macOS (no USB passthrough). Typical:
#
#   docker compose -f robotis_ai_setup/docker/docker-compose.yml exec open_manipulator \
#       python3 /usr/local/bin/calibrate_collision_currents.py
#
# MIXED SERVO MODELS: the follower's base joints (dxl11-13, XL430-W250) expose 'Present Load'
# (+/-1000 = +/-100% PWM); the wrist joints (dxl14-15, XL330-M288) expose 'Present Current'
# (~1 mA/LSB). Each is normalized to a 0..1 "effort fraction" so the printed thresholds are in
# the SAME unit the runtime detector compares against. The per-model full-scale constants below
# MUST match collision_detector.{PRESENT_LOAD_FULLSCALE, XL330_PRESENT_CURRENT_FULLSCALE,
# JOINT_EFFORT_SIGNAL} — this script is self-contained (it runs in the open_manipulator image,
# which has no physical_ai_server package to import from).
#
# Procedure: with the follower powered and NO object contact, move the arm slowly through its
# full normal workspace for the measurement window. The script records the per-joint effort
# envelope and prints a threshold = clamp(p95 * margin, floor, cap) as .env lines. Paste them
# into the EduBotics .env and restart the stack.
#
# Standalone rclpy; intentionally reads NO EDUBOTICS_* env vars (args/topic only) so it stays
# out of the ci.yml env-forwarding-guard surface.

import argparse
import sys
import time

import rclpy
from rclpy.node import Node

# Jazzy gpio_controllers publishes DynamicInterfaceGroupValues; older releases used
# DynamicJointState. Subscribe with the publisher's type (a DDS type mismatch silently
# delivers nothing) — MUST match collision_monitor.py's import strategy.
try:
    from control_msgs.msg import DynamicInterfaceGroupValues as GpioStateMsg
except ImportError:
    try:
        from control_msgs.msg import DynamicJointState as GpioStateMsg
    except ImportError:
        print('[FEHLER] control_msgs gpio-State-Message nicht verfügbar — läuft dieses Skript '
              'im open_manipulator-Container?', file=sys.stderr)
        sys.exit(1)

# MUST match collision_detector.py (this script cannot import it — different image).
PRESENT_LOAD_FULLSCALE = 1000.0
XL330_PRESENT_CURRENT_FULLSCALE = 1750.0
GPIO_NAME_TO_JOINT = {
    'dxl11': 'joint1', 'dxl12': 'joint2', 'dxl13': 'joint3',
    'dxl14': 'joint4', 'dxl15': 'joint5',
}
JOINT_EFFORT_SIGNAL = {
    'joint1': ('Present Load', PRESENT_LOAD_FULLSCALE),
    'joint2': ('Present Load', PRESENT_LOAD_FULLSCALE),
    'joint3': ('Present Load', PRESENT_LOAD_FULLSCALE),
    'joint4': ('Present Current', XL330_PRESENT_CURRENT_FULLSCALE),
    'joint5': ('Present Current', XL330_PRESENT_CURRENT_FULLSCALE),
}
ARM_JOINTS = ('joint1', 'joint2', 'joint3', 'joint4', 'joint5')


def _effort_fraction(joint, name_to_val):
    sig, fullscale = JOINT_EFFORT_SIGNAL.get(joint, (None, None))
    raw = name_to_val.get(sig) if sig else None
    if raw is None:
        for alt_sig, alt_fs in (('Present Current', XL330_PRESENT_CURRENT_FULLSCALE),
                                ('Present Load', PRESENT_LOAD_FULLSCALE)):
            if alt_sig in name_to_val:
                raw, fullscale = name_to_val[alt_sig], alt_fs
                break
    if raw is None or not fullscale:
        return None
    # Signed 16-bit register published unsigned (Present Load: -5 -> 65531). Map the upper
    # half back to negative before the magnitude — MUST match collision_detector.py.
    v = float(raw)
    if v >= 32768.0:
        v -= 65536.0
    return abs(v) / float(fullscale)


class CalibrationNode(Node):
    def __init__(self, topic):
        super().__init__('collision_calibration')
        self._samples = {j: [] for j in ARM_JOINTS}
        self._signal_seen = {j: None for j in ARM_JOINTS}
        self.create_subscription(GpioStateMsg, topic, self._cb, 10)

    def _cb(self, msg):
        names = getattr(msg, 'interface_groups', None)
        if names is None:
            names = getattr(msg, 'joint_names', [])
        for idx, gpio_name in enumerate(names):
            joint = GPIO_NAME_TO_JOINT.get(gpio_name)
            if joint is None or idx >= len(msg.interface_values):
                continue
            iv = msg.interface_values[idx]
            name_to_val = dict(zip(iv.interface_names, iv.values))
            frac = _effort_fraction(joint, name_to_val)
            if frac is not None:
                self._samples[joint].append(frac)
                if self._signal_seen[joint] is None:
                    self._signal_seen[joint] = (
                        'Present Current' if 'Present Current' in name_to_val
                        else 'Present Load' if 'Present Load' in name_to_val
                        else '?')

    def sample_count(self):
        return sum(len(v) for v in self._samples.values())


def _p95(values):
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]


def main():
    parser = argparse.ArgumentParser(description='Calibrate teleop collision effort thresholds.')
    parser.add_argument('--duration', type=float, default=30.0, help='measurement seconds')
    parser.add_argument('--margin', type=float, default=1.5, help='multiplier above the p95 envelope')
    parser.add_argument('--floor', type=float, default=0.30, help='minimum threshold (effort fraction)')
    parser.add_argument('--cap', type=float, default=0.95, help='maximum threshold (effort fraction)')
    parser.add_argument('--topic', default='/gpio_command_controller/gpio_states')
    args = parser.parse_args()

    rclpy.init()
    node = CalibrationNode(args.topic)
    print('========================================')
    print('EduBotics — Kollisions-Schwellwert-Kalibrierung (Effort-Fraktion 0..1)')
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
        print('[FEHLER] Keine Messwerte empfangen. Prüfe: läuft der gpio_command_controller '
              f'und stimmt das Topic ({args.topic})? (ros2 topic list)', file=sys.stderr)
        node.destroy_node()
        rclpy.shutdown()
        sys.exit(2)

    print('')
    print(f'Fertig — {count} Messwerte erfasst. Empfohlene Schwellwerte (Effort-Fraktion 0..1):')
    print('# --- in die EduBotics .env einfügen und Stack neu starten ---')
    env_index = {'joint1': 1, 'joint2': 2, 'joint3': 3, 'joint4': 4, 'joint5': 5}
    for joint in ARM_JOINTS:
        envelope = _p95(node._samples[joint])
        threshold = max(envelope * args.margin, args.floor)
        threshold = min(threshold, args.cap)
        sig = node._signal_seen[joint] or '—'
        if envelope >= 0.85:
            print(f'#   WARNUNG: {joint} no-contact p95 = {envelope:.2f} ist hoch — '
                  'evtl. Reibung/Last/Pose prüfen.')
        print(f'EDUBOTICS_COLLISION_EFFORT_J{env_index[joint]}={threshold:.2f}'
              f'   # {sig}: no-contact p95={envelope:.2f}')
    print('# Velocity-Gate / Debounce bei Bedarf anpassen:')
    print('# EDUBOTICS_COLLISION_VELOCITY_GATE=0.05')
    print('# EDUBOTICS_COLLISION_DEBOUNCE_MS=150')

    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()

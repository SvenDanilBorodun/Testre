#!/usr/bin/env python3
"""edu6_studio driver node — 7 × Feetech STS3215 on one serial bus.

The ROS contract this node satisfies (edu6 plan §3.2) is exactly what makes
``physical_ai_server`` work untouched:

* pub ``/joint_states`` (name + position + velocity, all 7 joints per message,
  URDF-native names, 50 Hz) — ALSO the compose healthcheck gate, so the
  publisher is created ONLY AFTER the first successful servo ping (the check
  is topic-existence-only; an eagerly-created publisher would let the
  container go healthy over a dead bus).
* sub ``/leader/joint_trajectory`` — the command rail. QoS VOLATILE+RELIABLE
  (a TRANSIENT_LOCAL subscriber never matches the VOLATILE workflow/jog/replay
  publishers). Multi-point trajectories are executed AS trajectories: a
  50 Hz write loop interpolates the active trajectory by ``time_from_start``
  and SYNC_WRITEs acceleration+goal to all 7 servos (``Goal_Time`` is a
  documented no-op on STS — streaming is the only correct execution, the same
  conclusion LeRobot and feetech_ros2_driver reached).
* srv ``/edu6/set_torque`` (std_srvs/SetBool) + the LEGACY ALIAS
  ``/dynamixel_hardware_interface/set_dxl_torque`` — so a stale client (incl.
  the entrypoint's shutdown trap) can never silently fail to disable torque.

Safety posture (plan §8): the servo has NO host watchdog — it holds its last
goal energised forever if we die — so torque-off runs on SIGTERM/SIGINT/atexit.
Per-joint ``Min/Max_Position_Limit`` live in servo EEPROM (written at vendor
provisioning), ``Max_Torque_Limit`` is the hardware pinch floor; this node adds
NO software safety envelope (Rule §2) beyond refusing to command outside the
URDF limits.

Bus loops run on their OWN threads (not a ROS executor — the ~35-service
MultiThreadedExecutor starvation class from the server node does not exist
here, and the serial bus wants strict pacing). All bus access serialises
through one lock. 50 Hz, not 100: the usbipd/WSL2 tunnel jitters 100 Hz
(CLAUDE.md), and 50 Hz holds the 25 Hz replay contract with margin.
"""

from __future__ import annotations

import atexit
import math
import os
import signal
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import feetech_bus as fb  # noqa: E402  (COPY'd next to this file in the image)

import rclpy  # noqa: E402
from rclpy.node import Node  # noqa: E402
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy  # noqa: E402
from sensor_msgs.msg import JointState  # noqa: E402
from std_srvs.srv import SetBool  # noqa: E402
from trajectory_msgs.msg import JointTrajectory  # noqa: E402


# ── arm constants (mirror robot_profiles._EDU6_*; no-drift-tested) ───────────
SERVO_IDS = (1, 2, 3, 4, 5, 6, 7)
JOINT_NAMES = ('joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6',
               'end_gear_joint')
HOME_JOINTS_RAD = (0.0, 0.70, -2.40, 0.0, 0.70, 0.0)
GRIPPER_OPEN_RAD = 1.75
TICKS_PER_REV = 4096
CENTER_TICK = 2048
RAD_PER_TICK = 2.0 * math.pi / TICKS_PER_REV

# URDF joint limits (the software refuse-band; the servo EEPROM limits are the
# hardware floor underneath).
JOINT_LIMITS_RAD = (
    (-1.5708, 1.5708), (0.0, 3.1416), (-3.1416, 0.0), (-3.1416, 3.1416),
    (-1.5708, 1.9199), (-3.1416, 3.1416),
    # gripper (end_gear servo): 0 = closed … open command band
    (0.0, 1.79),
)

LOOP_HZ = 50.0
BOOT_HOME_DURATION_S = 3.0
# Fixed conservative servo acceleration (unit ≈ 8.7 mrad/s²·LSB per Feetech
# docs — bench-confirmed at R2/R5) and a Goal_Speed cap as a SECOND,
# independent speed limit that composes with the trajectory velocity floor
# (0.8 × the URDF 5.45 rad/s ≈ 2840 steps/s).
WRITE_ACCELERATION = 50
GOAL_SPEED_CAP_STEPS = 2840

# Per-joint direction between URDF positive rotation and servo tick growth.
# The vendor provisioning jig fixes offsets so HOME reads its designed ticks;
# signs are the remaining convention, bench-set at rig gate R6 and overridable
# per rig without an image rebuild.
_DEFAULT_SIGNS = (1, 1, 1, 1, 1, 1, 1)


def _parse_signs(raw: str | None) -> tuple[int, ...]:
    # An unset value is not an error (defaults apply silently); a MALFORMED
    # value is bench-facing operator error — surface it once (English, plain
    # print like the rest of the pre-rclpy init path) instead of silently
    # running the wrong direction convention.
    if not raw or not raw.strip():
        return _DEFAULT_SIGNS
    try:
        vals = tuple(int(v) for v in raw.split(','))
    except (TypeError, ValueError):
        print(f'[WARN] EDUBOTICS_EDU6_JOINT_SIGNS={raw!r} is not a comma-'
              f'separated integer list — using defaults {_DEFAULT_SIGNS}.',
              flush=True)
        return _DEFAULT_SIGNS
    if len(vals) != len(SERVO_IDS) or any(v not in (-1, 1) for v in vals):
        print(f'[WARN] EDUBOTICS_EDU6_JOINT_SIGNS={raw!r} must be '
              f'{len(SERVO_IDS)} values from {{-1, 1}} — using defaults '
              f'{_DEFAULT_SIGNS}.', flush=True)
        return _DEFAULT_SIGNS
    return vals


JOINT_SIGNS = _parse_signs(os.environ.get('EDUBOTICS_EDU6_JOINT_SIGNS'))


def rad_to_tick(rad: float, sign: int) -> int:
    tick = CENTER_TICK + int(round(rad * sign / RAD_PER_TICK))
    return max(0, min(TICKS_PER_REV - 1, tick))


def tick_to_rad(tick: int, sign: int) -> float:
    return (tick - CENTER_TICK) * RAD_PER_TICK * sign


def interpolate_trajectory(points: list[tuple[list[float], float]],
                           t: float) -> list[float] | None:
    """Linear interpolation of a densely-sampled (25–30 Hz quintic) trajectory
    at time ``t`` from its start. Past the end → the final point (hold); before
    the first point → the first point. ``None`` for an empty list. Pure —
    unit-tested without ROS."""
    if not points:
        return None
    if t <= points[0][1]:
        return list(points[0][0])
    if t >= points[-1][1]:
        return list(points[-1][0])
    for i in range(len(points) - 1):
        q0, t0 = points[i]
        q1, t1 = points[i + 1]
        if t0 <= t <= t1:
            if t1 <= t0:
                return list(q1)
            s = (t - t0) / (t1 - t0)
            return [a + (b - a) * s for a, b in zip(q0, q1)]
    return list(points[-1][0])


def build_boot_home(current: list[float], duration_s: float = BOOT_HOME_DURATION_S,
                    hz: float = LOOP_HZ) -> list[tuple[list[float], float]]:
    """Quintic glide from the power-up pose to HOME + open gripper (the same
    boot-home UX as the OMX entrypoint's Phase 3). Pure — unit-tested."""
    target = list(HOME_JOINTS_RAD) + [GRIPPER_OPEN_RAD]
    n = max(2, int(duration_s * hz))
    out: list[tuple[list[float], float]] = []
    for i in range(1, n + 1):
        s = i / n
        blend = 10 * s ** 3 - 15 * s ** 4 + 6 * s ** 5
        q = [c + (g - c) * blend for c, g in zip(current, target)]
        out.append((q, duration_s * s))
    return out


class Edu6ArmNode(Node):
    def __init__(self, bus: fb.FeetechBus) -> None:
        super().__init__('edu6_arm_node')
        self._bus = bus
        self._bus_lock = threading.Lock()
        self._stop = threading.Event()
        self._torque_on = False
        self._last_positions: list[float] | None = None
        # active trajectory: (points [(q7, t)], start_mono)
        self._traj_lock = threading.Lock()
        self._traj_points: list[tuple[list[float], float]] = []
        self._traj_start: float = 0.0
        self._error_log_last: dict[int, float] = {}
        self._read_fail_streak = 0
        self._shutdown_done = False

        # /joint_states — created by main() ONLY after the boot ping succeeded
        # (see the module docstring). Kept here for visibility of the QoS.
        self._joint_pub = self.create_publisher(JointState, '/joint_states', 10)

        traj_qos = QoSProfile(
            depth=10,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,   # NEVER transient-local (§3.2)
        )
        self.create_subscription(
            JointTrajectory, '/leader/joint_trajectory',
            self._trajectory_cb, traj_qos)

        # Torque service + the legacy alias (both must exist — §3.4).
        self.create_service(SetBool, '/edu6/set_torque', self._torque_cb)
        self.create_service(
            SetBool, '/dynamixel_hardware_interface/set_dxl_torque',
            self._torque_cb)

        self._read_thread = threading.Thread(
            target=self._read_loop, name='edu6-read', daemon=True)
        self._write_thread = threading.Thread(
            target=self._write_loop, name='edu6-write', daemon=True)

    # ── bus bring-up (called BEFORE the node is constructed) ─────────────────
    @staticmethod
    def probe_bus(bus: fb.FeetechBus, logger=None) -> tuple[bool, str]:
        """Ping IDs 1..7 + verify Model_Number 777 each. Returns
        ``(ok, german_message)`` — the message distinguishes the single most
        likely student error (USB enumerated but 12 V supply off: the port
        exists, every ping times out) from a partial bus."""
        alive = [sid for sid in SERVO_IDS if bus.ping(sid)]
        if not alive:
            return False, (
                '[FEHLER] Kein Servo antwortet. Ist das 12-V-Netzteil des '
                'Arms eingesteckt und eingeschaltet? (Der USB-Anschluss '
                'allein versorgt die Servos nicht.)')
        missing = [sid for sid in SERVO_IDS if sid not in alive]
        if missing:
            return False, (
                f'[FEHLER] Servo(s) {missing} antworten nicht — bitte die '
                'Kabelverbindungen am Arm prüfen und die Umgebung neu starten.')
        wrong = []
        for sid in SERVO_IDS:
            try:
                model = bus.read_u16(sid, fb.REG_MODEL_NUMBER)
            except fb.FeetechBusError:
                wrong.append((sid, None))
                continue
            if model != fb.STS3215_MODEL_NUMBER:
                wrong.append((sid, model))
        if wrong:
            return False, (
                f'[FEHLER] Unerwartetes Servomodell am Bus: {wrong} — dieser '
                'Arm ist kein EduBotics 6-Achs.')
        return True, ''

    # ── torque ───────────────────────────────────────────────────────────────
    def set_torque(self, enabled: bool) -> bool:
        try:
            with self._bus_lock:
                self._bus.sync_write(
                    fb.REG_TORQUE_ENABLE,
                    {sid: bytes([1 if enabled else 0]) for sid in SERVO_IDS})
            self._torque_on = bool(enabled)
            return True
        except Exception as e:  # noqa: BLE001
            self.get_logger().error(f'torque switch failed: {e}')
            return False

    def _torque_cb(self, request, response):
        ok = self.set_torque(bool(request.data))
        response.success = ok
        response.message = 'ok' if ok else 'Torque-Umschaltung fehlgeschlagen.'
        if not request.data:
            # Dropping torque abandons any in-flight trajectory (a re-torque
            # must never resume a stale goal from before the limp phase).
            with self._traj_lock:
                self._traj_points = []
        return response

    # ── command rail ─────────────────────────────────────────────────────────
    def _trajectory_cb(self, msg: JointTrajectory) -> None:
        names = list(msg.joint_names)
        index_map: list[int] = []
        for joint in JOINT_NAMES:
            if joint in names:
                index_map.append(names.index(joint))
            else:
                index_map.append(-1)
        if all(i < 0 for i in index_map):
            self.get_logger().warning(
                '[WARNUNG] Trajektorie ohne bekannte Gelenknamen ignoriert '
                f'(erhalten: {names}).')
            return
        base = self._last_positions or list(HOME_JOINTS_RAD) + [GRIPPER_OPEN_RAD]
        points: list[tuple[list[float], float]] = []
        for pt in msg.points:
            q = list(base)
            for k, src in enumerate(index_map):
                if src >= 0 and src < len(pt.positions):
                    q[k] = float(pt.positions[src])
            t = pt.time_from_start.sec + pt.time_from_start.nanosec * 1e-9
            points.append((q, t))
        if not points:
            return
        with self._traj_lock:
            self._traj_points = points
            self._traj_start = time.monotonic()

    def start_boot_home(self) -> None:
        """Quintic-glide to HOME from the measured power-up pose (torque on
        first). Mirrors the OMX entrypoint Phase 3 UX."""
        current = self._read_positions_once()
        if current is None:
            self.get_logger().error(
                '[FEHLER] Startpose konnte nicht gelesen werden — die '
                'Grundstellungs-Fahrt entfällt.')
            return
        if not self.set_torque(True):
            return
        with self._traj_lock:
            self._traj_points = build_boot_home(current)
            self._traj_start = time.monotonic()
        self.get_logger().info(
            'Grundstellungs-Fahrt gestartet (3 s sanfte Bewegung).')

    # ── bus loops ────────────────────────────────────────────────────────────
    def _read_positions_once(self) -> list[float] | None:
        with self._bus_lock:
            replies = self._bus.sync_read(
                fb.REG_PRESENT_POSITION, 6, list(SERVO_IDS))
        if any(sid not in replies for sid in SERVO_IDS):
            return None
        out = []
        for i, sid in enumerate(SERVO_IDS):
            _err, data = replies[sid]
            raw = fb.from_le16(data[0], data[1])
            # Phase bit 4 is cleared at provisioning so positions stay in
            # [0, 4095]; the sign-magnitude decode is a no-op then and the
            # defensive path for uncleared firmware (same as _publish_joint_state).
            out.append(tick_to_rad(
                fb.decode_sign_magnitude(raw, 15), JOINT_SIGNS[i]))
        return out

    def _read_loop(self) -> None:
        period = 1.0 / LOOP_HZ
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                with self._bus_lock:
                    replies = self._bus.sync_read(
                        fb.REG_PRESENT_POSITION, 6, list(SERVO_IDS))
            except Exception as e:  # noqa: BLE001
                replies = {}
                self.get_logger().warning(f'sync_read raised: {e}')
            missing = [sid for sid in SERVO_IDS if sid not in replies]
            if missing:
                self._read_fail_streak += 1
                # A browned-out servo must NEVER feed a frozen angle into the
                # IK — publish NOTHING this tick; hard-stop loudly on a streak.
                if self._read_fail_streak == int(LOOP_HZ):  # ~1 s of misses
                    self.get_logger().error(
                        f'[FEHLER] Servo(s) {missing} liefern keine Daten mehr '
                        '— Bewegungen sind gestoppt. Bitte Kabel und '
                        '12-V-Versorgung prüfen, dann die Umgebung neu starten.')
                    with self._traj_lock:
                        self._traj_points = []
            else:
                self._read_fail_streak = 0
                self._publish_joint_state(replies)
            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, period - elapsed))

    def _publish_joint_state(self, replies: dict) -> None:
        positions, velocities = [], []
        now_error_log = time.monotonic()
        for i, sid in enumerate(SERVO_IDS):
            error, data = replies[sid]
            raw_pos = fb.from_le16(data[0], data[1])
            raw_vel = fb.from_le16(data[2], data[3])
            pos = fb.decode_sign_magnitude(raw_pos, 15)
            vel = fb.decode_sign_magnitude(raw_vel, 15)
            positions.append(tick_to_rad(pos, JOINT_SIGNS[i]))
            velocities.append(vel * RAD_PER_TICK * JOINT_SIGNS[i])
            if error:
                # Free per-reply telemetry (§3.3): a student whose servo
                # silently dropped to 20 % torque after an overload otherwise
                # experiences "the robot got weak" with no diagnosis.
                # Rate-limited to one German line per servo per 5 s.
                last = self._error_log_last.get(sid, 0.0)
                if now_error_log - last > 5.0:
                    self._error_log_last[sid] = now_error_log
                    self.get_logger().warning(
                        f'[WARNUNG] Servo {sid} meldet: '
                        f'{fb.describe_error_bits(error)} (Status {error:#04x}).')
        msg = JointState()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.name = list(JOINT_NAMES)
        msg.position = positions
        msg.velocity = velocities
        self._joint_pub.publish(msg)
        self._last_positions = positions

    def _write_loop(self) -> None:
        period = 1.0 / LOOP_HZ
        while not self._stop.is_set():
            t0 = time.monotonic()
            target: list[float] | None = None
            with self._traj_lock:
                if self._traj_points:
                    target = interpolate_trajectory(
                        self._traj_points, time.monotonic() - self._traj_start)
                    # Hold reached goals but drop the buffer once finished so
                    # a torque-off doesn't resurrect a stale trajectory.
                    if (time.monotonic() - self._traj_start
                            > self._traj_points[-1][1] + 1.0):
                        self._traj_points = []
            if target is not None and self._torque_on:
                self._write_targets(target)
            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, period - elapsed))

    def _write_targets(self, q: list[float]) -> None:
        payload: dict[int, bytes] = {}
        for i, sid in enumerate(SERVO_IDS):
            lo, hi = JOINT_LIMITS_RAD[i]
            rad = max(lo, min(hi, float(q[i])))
            tick = rad_to_tick(rad, JOINT_SIGNS[i])
            payload[sid] = (bytes([WRITE_ACCELERATION])
                            + fb.le16(tick)
                            + fb.le16(0)                      # Goal_Time: no-op
                            + fb.le16(GOAL_SPEED_CAP_STEPS))  # 2nd speed limit
        try:
            with self._bus_lock:
                self._bus.sync_write(fb.REG_ACCELERATION, payload)
        except Exception as e:  # noqa: BLE001
            self.get_logger().warning(f'sync_write failed: {e}')

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        self._read_thread.start()
        self._write_thread.start()

    def shutdown(self) -> None:
        """Torque OFF + stop loops. The STS3215 has no watchdog — it holds its
        last goal energised forever if the host stops talking, so this runs on
        SIGTERM/SIGINT/atexit (plan §8). Idempotent: guarded so the finally-block
        call and the atexit call cannot double-run (and cannot fight over the
        already-closed bus)."""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._stop.set()
        try:
            self.set_torque(False)
            self.get_logger().info('Servomotoren stromlos geschaltet.')
        except Exception:  # noqa: BLE001 — shutdown is best-effort
            pass
        self._bus.close()


def main() -> int:
    port = os.environ.get('FOLLOWER_PORT', '/dev/ttyACM0')
    print(f'[INIT] edu6_arm_node: Feetech-Bus auf {port} (1 Mbit/s) …',
          flush=True)
    try:
        bus = fb.FeetechBus(port)
    except Exception as e:  # noqa: BLE001
        print(f'[FEHLER] Serieller Port {port} konnte nicht geöffnet werden: '
              f'{e}', flush=True)
        return 1

    # Boot gate: NO ROS entities before the bus answers — the compose
    # healthcheck is topic-existence-only, and /joint_states must not exist
    # over a dead bus. Retry forever (the container stays unhealthy; the GUI
    # shows the German reason from this log).
    while True:
        ok, message = Edu6ArmNode.probe_bus(bus)
        if ok:
            print('[INIT] Alle 7 Servos gefunden (STS3215).', flush=True)
            break
        print(message, flush=True)
        time.sleep(5.0)

    rclpy.init()
    node = Edu6ArmNode(bus)

    def _sigterm(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _sigterm)

    # The docstring/CLAUDE.md promise torque-off "on atexit" — make it literally
    # true. shutdown() is idempotent, so this is a no-op once the finally block
    # below has already torqued off on the normal SIGTERM→KeyboardInterrupt path.
    atexit.register(node.shutdown)

    try:
        node.start()
        node.start_boot_home()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.shutdown()
        try:
            node.destroy_node()
            rclpy.shutdown()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())

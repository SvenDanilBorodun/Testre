#!/usr/bin/env python3
"""edu6_studio driver node — 7 × Feetech STS3215 on one serial bus.

The ROS contract this node satisfies (edu6 plan §3.2) is exactly what makes
``physical_ai_server`` work untouched:

* pub ``/joint_states`` (name + position + velocity, all 7 joints per message,
  URDF-native names, 50 Hz) — ALSO the compose healthcheck gate, so the
  publisher is created ONLY AFTER the boot probe succeeded (ping + accepted
  STS model 777/2825 + the EEPROM provisioning fingerprint, see ``probe_bus``;
  the check is
  topic-existence-only; an eagerly-created publisher would let the container
  go healthy over a dead or unprovisioned bus).
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
NO software safety envelope (Rule §2) beyond input validation and boot gating:
non-finite trajectory points are rejected (a NaN would otherwise clamp to a
LIMIT-seeking command — audit M3), commands are clamped to the URDF limits
(the servo EEPROM window underneath is the hardware floor), trajectories are
dropped while the arm is limp or the bus is faulted (audits M2/M5), and the
boot probe refuses an arm whose EEPROM does not carry the expected
provisioning fingerprint (audits H1/H2).

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
#
# ENCODER SEAM — known, accepted, matches the OMX. The ±180° ends of j2(hi),
# j3(lo), j4 and j6 land on the SINGLE-TURN encoder seam: ticks 0 and 4095 are
# 0.088° apart physically but 4095 apart numerically, so a joint sitting there
# cannot tell +180° from −180° and one tick of drift reports a 360° jump. A
# 360° span needs 4097 distinguishable positions from a 4096-position sensor —
# it cannot be represented. The OMX carries exactly the same exposure
# (omx_f.ros2_control.xacro gives dxl14/dxl15 `Min 0 / Max 4095` with ±π
# limits) and has run fine, because its roll convention parks the DEFAULT
# wrist at 0° — dead centre — rather than on the seam. edu6 matches that by
# defaulting the tool roll to π in edu6_ik.solve (its mapping carries an extra
# π); see the ENCODER SEAM note there for the residual that remains.
#
# Keep in lockstep with edu6_ik._EDU6_JOINT_LIMITS_RAD and
# edu6_provision.JOINT_LIMITS_RAD (no-drift-tested). Changing any of them
# requires RE-PROVISIONING every arm — probe_bus() verifies the EEPROM window
# against these values.
JOINT_LIMITS_RAD = (
    (-1.5708, 1.5708), (0.0, 3.1416), (-3.1416, 0.0), (-3.1416, 3.1416),
    (-1.5708, 1.9199), (-3.1416, 3.1416),
    # gripper (end_gear servo): 0 = closed … open command band
    (0.0, 1.79),
)

LOOP_HZ = 50.0
BOOT_HOME_DURATION_S = 3.0
# A fully-missing servo must hard-stop motion within this WALL-CLOCK window.
# Wall-clock, not loop iterations (audit M5): with an absent servo each
# sync_read burns its scaled deadline, so an iteration count of LOOP_HZ took
# ~8 s of real time, not the promised ~1 s.
READ_FAIL_STOP_S = 1.0
# Fixed conservative servo acceleration. Official unit (ST3215 memory table
# V3.7): 100 steps/s² per LSB ≈ 8.79°/s²·LSB — so 50 ⇒ 5000 steps/s²
# ≈ 7.7 rad/s² (R2/R5 confirm on the bench). Plus a Goal_Speed cap as a
# SECOND, independent speed limit that composes with the trajectory velocity
# floor (0.8 × the URDF 5.45 rad/s ≈ 2840 steps/s).
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
        # Wall-clock start of the current read-miss window (audit M5) and the
        # latched bus-fault flag: once tripped, trajectories are refused and
        # the write loop is silenced until a FULL read succeeds again.
        self._read_fail_since: float | None = None
        self._bus_fault = False
        self._limp_log_last = 0.0
        self._fault_log_last = 0.0
        self._shutdown_done = False

        # /joint_states — the node itself is constructed only after
        # probe_bus() succeeded in main(), so no ROS entity can exist over a
        # dead/unprovisioned bus. Kept here for visibility of the QoS.
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
        """Ping IDs 1..7 + verify each is an accepted STS model (777 STS3215
        on joints 1/4/5/6/7, 2825 STS3250 on joints 2/3 — mixed by design).
        Returns ``(ok, german_message)`` — the message distinguishes the
        single most likely student error (USB enumerated but 12 V supply off:
        the port exists, every ping times out) from a partial bus."""
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
            if model not in fb.STS_ACCEPTED_MODELS:
                wrong.append((sid, model))
        if wrong:
            return False, (
                f'[FEHLER] Unerwartetes Servomodell am Bus: {wrong} — dieser '
                'Arm ist kein EduBotics 6-Achs.')
        # Provisioned-state gate (audit H1+H2). Ping + model alone would let a
        # factory-fresh or RMA-swapped servo boot-home in a WRONG coordinate
        # frame at factory torque (1000, not the provisioned 800/150), and a
        # wheel-mode servo would turn boot-home into a continuous-rotation
        # runaway that EEPROM position limits cannot stop. The EEPROM limit
        # windows double as the provisioning fingerprint: they must equal the
        # designed sign-aware windows (fb.position_limit_window — the SAME
        # implementation the provisioning tool writes with), the mode must be
        # position. ±1 tick tolerates nothing — the windows are integers both
        # sides — but keeps a future rounding change from bricking boots.
        for i, sid in enumerate(SERVO_IDS):
            try:
                mode = bus.read(sid, fb.REG_OPERATING_MODE, 1)[1][0]
                lo = bus.read_u16(sid, fb.REG_MIN_POSITION_LIMIT)
                hi = bus.read_u16(sid, fb.REG_MAX_POSITION_LIMIT)
            except fb.FeetechBusError as e:
                return False, (
                    f'[FEHLER] Servo {sid}: EEPROM-Kontrolle fehlgeschlagen '
                    f'({e}) — Verkabelung prüfen und erneut versuchen.')
            if mode != 0:
                return False, (
                    f'[FEHLER] Servo {sid}: Betriebsmodus {mode} statt '
                    'Positionsmodus — dieser Arm ist nicht provisioniert. '
                    'Bitte tools/edu6_provision.py auf der Werkbank '
                    'ausführen.')
            lo_exp, hi_exp = fb.position_limit_window(
                *JOINT_LIMITS_RAD[i], JOINT_SIGNS[i])
            if abs(lo - lo_exp) > 1 or abs(hi - hi_exp) > 1:
                return False, (
                    f'[FEHLER] Servo {sid}: EEPROM-Positionsgrenzen '
                    f'[{lo}, {hi}] passen nicht zur erwarteten '
                    f'Provisionierung [{lo_exp}, {hi_exp}] — Arm nicht '
                    'provisioniert oder EDUBOTICS_EDU6_JOINT_SIGNS weicht '
                    'von der Provisionierung ab? Bitte '
                    'tools/edu6_provision.py mit den aktuellen Vorzeichen '
                    'ausführen.')
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
        if request.data:
            # Audit M2 (belt): entering torque from limp must never resume a
            # trajectory stored before or DURING the limp phase — clear before
            # the first energized write tick can interpolate it.
            with self._traj_lock:
                self._traj_points = []
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
        now = time.monotonic()
        # Audit M2: a trajectory arriving while the arm is LIMP (hand-guide /
        # pre-boot-home) must not be stored — at re-torque the write loop
        # would interpolate past its end and glide the arm to a long-stale
        # target from wherever the student left it.
        if not self._torque_on:
            if now - self._limp_log_last > 5.0:
                self._limp_log_last = now
                self.get_logger().warning(
                    '[WARNUNG] Trajektorie verworfen: Drehmoment ist aus '
                    '(Hand-Führung aktiv?).')
            return
        # Audit M5: latched bus fault — no new commands while servos are
        # missing; the arm would execute them open-loop.
        if self._bus_fault:
            if now - self._fault_log_last > 5.0:
                self._fault_log_last = now
                self.get_logger().warning(
                    '[WARNUNG] Trajektorie verworfen: Servobus antwortet '
                    'nicht (siehe Fehlermeldung oben).')
            return
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
            t = pt.time_from_start.sec + pt.time_from_start.nanosec * 1e-9
            # Audit M3: Python's min/max clamp a NaN to the UPPER limit — a
            # NaN position would become a limit-seeking command. Refuse the
            # whole message loudly (input validation, not a safety envelope).
            if not math.isfinite(t) or not all(
                    math.isfinite(float(v)) for v in pt.positions):
                self.get_logger().warning(
                    '[WARNUNG] Trajektorie verworfen: nicht-endliche Werte '
                    '(NaN/Inf) in den Zielpunkten.')
                return
            q = list(base)
            for k, src in enumerate(index_map):
                if src >= 0 and src < len(pt.positions):
                    q[k] = float(pt.positions[src])
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
                now = time.monotonic()
                if self._read_fail_since is None:
                    self._read_fail_since = now
                # A browned-out servo must NEVER feed a frozen angle into the
                # IK — publish NOTHING this tick; hard-stop loudly once the
                # WALL-CLOCK miss window fills (audit M5: an absent servo
                # stretches each sync_read to its scaled deadline, so an
                # iteration count silently multiplied the promised ~1 s).
                if (now - self._read_fail_since >= READ_FAIL_STOP_S
                        and not self._bus_fault):
                    self._bus_fault = True
                    self.get_logger().error(
                        f'[FEHLER] Servo(s) {missing} liefern keine Daten mehr '
                        '— Bewegungen sind gestoppt. Bitte Kabel und '
                        '12-V-Versorgung prüfen, dann die Umgebung neu starten.')
                    with self._traj_lock:
                        self._traj_points = []
            else:
                self._read_fail_since = None
                if self._bus_fault:
                    # Latched fault clears ONLY on a full 7-servo read.
                    self._bus_fault = False
                    self.get_logger().info(
                        'Servobus wieder erreichbar — Bewegungen sind wieder '
                        'möglich.')
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
            # Bus-fault gate (audit M5): a latched fault silences the write
            # loop too — never command servos the read loop cannot see.
            if target is not None and self._torque_on and not self._bus_fault:
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

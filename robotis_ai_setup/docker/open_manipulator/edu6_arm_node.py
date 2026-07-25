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
JOINT_LIMITS_RAD = (
    (-1.5708, 1.5708), (0.0, 3.1416), (-3.1416, 0.0), (-3.1416, 3.1416),
    (-1.5708, 1.9199), (-3.1416, 3.1416),
    # gripper (end_gear servo): 0 = closed … open command band
    (0.0, 1.79),
)

LOOP_HZ = 50.0
BOOT_HOME_DURATION_S = 3.0
# Boot-home arrival verification (Decision A, 2026-07-24 — mirror the OMX
# entrypoint Phase-3 verifier instead of a lift-then-fold). The glide is
# otherwise fire-and-forget; a stalled or weakened joint would leave the arm off
# HOME silently. Tolerance matches the OMX verifier (0.30 rad, arm joints only).
# On a miss the glide is re-sent from the ACTUAL pose (the Feetech current caps
# make a re-send non-destructive), bounded by MAX_SENDS, then a soft German
# [WARNUNG]; the arm always stays usable. Set MAX_SENDS = 0 for verify-only.
BOOT_HOME_VERIFY_TOL_RAD = 0.30
BOOT_HOME_VERIFY_SETTLE_S = 0.7
BOOT_HOME_VERIFY_MAX_SENDS = 1
# (BOOT_POSITION_TOLERANCE_TICKS is env-parsed further down, next to
# JOINT_SIGNS — it must follow its parser so the deps-free AST test loader,
# which walks this module top-to-bottom, can evaluate it.)
# Boot torque-on retry: a transient bus hiccup must not leave the arm limp for
# the whole session (the seed read inside set_torque can fail, and a refusal
# there is correct — but a ONE-shot refusal is not, since nothing retries it).
BOOT_TORQUE_ON_ATTEMPTS = 3
BOOT_TORQUE_ON_RETRY_S = 1.0
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
# Speed used ONLY for the hold-where-you-are write that precedes torque-on
# (_seed_goal_from_present_locked) — deliberately ~14x slower than the motion
# cap: 200 steps/s ≈ 0.31 rad/s.
#
# Seeding goal = present usually means NO motion at all. But the servo CLAMPS
# Goal_Position into its EEPROM Min/Max_Position_Limit window, so a limp joint
# that has sagged OUTSIDE its window does not hold — it is pulled to the window
# edge the instant torque arrives. The boot plausibility band bounds that pull
# (BOOT_POSITION_TOLERANCE_TICKS, default 400 ≈ 35°), and at the motion cap that
# distance would be covered in ~0.14 s: a snap. At this speed it is a ~2 s
# creep. The first _write_targets tick restores the full cap, so nothing else
# is slowed. Before 2026-07-25 the seed wrote the goal ALONE, leaving the servo
# to use whatever Goal_Speed/Acceleration its RAM held from power-on — values
# this driver has never written and never read (R5 reads them on the bench).
SEED_SPEED_STEPS = 200
# Present_Load full scale: the STS register reads ±1000 = ±100 % of applied PWM
# (sign bit 10, NOT 15 like position/speed). Published as JointState.effort as a
# SIGNED FRACTION (−1.0 … +1.0), matching the effort-fraction convention the OMX
# collision detector already uses (PRESENT_LOAD_FULLSCALE = 1000.0).
#
# NOTE it is a PWM/torque FRACTION, not the newton-metres ROS nominally puts in
# `effort` — the STS exposes no torque constant, so N·m would be a fabricated
# number. The fraction is what the bench gates actually want (R4 pinch force,
# R6 joint sweeps) and it costs nothing: bytes 4-5 of the 6-byte sync_read the
# read loop ALREADY performs are Present_Load, and were being discarded.
PRESENT_LOAD_FULLSCALE = 1000.0

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

# How far outside its designed window a joint may READ at boot before the probe
# refuses (see probe_bus).
#
# Sizing (2026-07-25). The check hunts ONE thing: a joint hand-turned past ±180°
# of its zero while limp, which the encoder then reports from the other side of
# our tick↔angle map. That wrap puts the reading at tick ≈0 or ≈4095, i.e.
# OUTSIDE the designed window by at least: joint1 1023, joint2 2048, joint3
# 2047, joint5 795, gripper 880 ticks. So any band below ~795 catches every
# wrap on every joint that has a detectable window — 400 keeps 2× headroom on
# the tightest (joint5) while being far more forgiving of the innocent case.
#
# The innocent case is why 400 and not the original 114 (=10°): this probe runs
# BEFORE torque-on, i.e. on an arm that has flopped under gravity, and a limp
# joint resting a little past a designed limit is HEALTHY — at 114 ticks a
# joint5 settling 11° past its −90° stop would refuse „Umgebung starten"
# outright. Detection power is unchanged; only the false-alarm rate drops.
#
# Env-overridable so rig gate R9 (torque-off collapse — it measures where a limp
# arm actually rests) can retune per rig without an image rebuild. A value ≥
# TICKS_PER_REV disables the check; forwarded on the compose environment list.
_DEFAULT_BOOT_POSITION_TOLERANCE_TICKS = 400


def _parse_boot_pos_tolerance(raw: str | None) -> int:
    # Unset is not an error; a MALFORMED value is bench-facing operator error —
    # surface it once (English, plain print like the rest of the pre-rclpy init
    # path) rather than silently running a different band than the operator set.
    if raw is None or not raw.strip():
        return _DEFAULT_BOOT_POSITION_TOLERANCE_TICKS
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        print(f'[WARN] EDUBOTICS_EDU6_BOOT_POS_TOL_TICKS={raw!r} is not an '
              f'integer — using the default '
              f'{_DEFAULT_BOOT_POSITION_TOLERANCE_TICKS}.', flush=True)
        return _DEFAULT_BOOT_POSITION_TOLERANCE_TICKS
    if value < 0:
        print(f'[WARN] EDUBOTICS_EDU6_BOOT_POS_TOL_TICKS={raw!r} must be >= 0 '
              f'— using the default '
              f'{_DEFAULT_BOOT_POSITION_TOLERANCE_TICKS}.', flush=True)
        return _DEFAULT_BOOT_POSITION_TOLERANCE_TICKS
    return value


BOOT_POSITION_TOLERANCE_TICKS = _parse_boot_pos_tolerance(
    os.environ.get('EDUBOTICS_EDU6_BOOT_POS_TOL_TICKS'))


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


def boot_home_verify_decision(cur, target, tol, attempt, max_sends):
    """Pure boot-home arrival check (mirrors the OMX Phase-3 verify intent).

    ``target`` is the arm HOME pose (gripper EXCLUDED); ``cur`` is the freshly
    read pose (arm joints first, same order — a longer ``cur`` that also carries
    the gripper is fine, the tail is ignored). Returns ``(verdict, off)`` where
    verdict is 'nodata' | 'arrived' | 'resend' | 'give_up' and ``off`` names the
    1-based joints still beyond ``tol``. Arrival is tolerance-based: the driver's
    own bus-fault latch already covers the stale-read case the OMX '>=50 %
    traversed' guard defended against, so no start pose is needed here."""
    n = len(target)
    if cur is None or len(cur) < n:
        return 'nodata', []
    off = [i + 1 for i in range(n)
           if abs(float(cur[i]) - float(target[i])) > tol]
    if not off:
        return 'arrived', []
    if attempt < max_sends:
        return 'resend', off
    return 'give_up', off


class Edu6ArmNode(Node):
    def __init__(self, bus: fb.FeetechBus) -> None:
        super().__init__('edu6_arm_node')
        self._bus = bus
        self._bus_lock = threading.Lock()
        self._stop = threading.Event()
        self._torque_on = False
        self._last_positions: list[float] | None = None
        # active trajectory: (points [(q7, t)], start_mono). `_traj_gen` counts
        # DELIBERATE replacements (new command / abort) so a long-running
        # background task can tell whether it still owns the command rail —
        # see _replace_trajectory and _boot_home_verify.
        self._traj_lock = threading.Lock()
        self._traj_points: list[tuple[list[float], float]] = []
        self._traj_start: float = 0.0
        self._traj_gen = 0
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
                phase = bus.read(sid, fb.REG_PHASE, 1)[1][0]
                lo = bus.read_u16(sid, fb.REG_MIN_POSITION_LIMIT)
                hi = bus.read_u16(sid, fb.REG_MAX_POSITION_LIMIT)
                present = fb.decode_sign_magnitude(
                    bus.read_u16(sid, fb.REG_PRESENT_POSITION), 15)
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
            # Multi-turn feedback (Phase bit 4) makes Present_Position wrap past
            # the single-turn range, feeding overflowed angles into the fixed
            # tick↔angle kinematic map. Provisioning CLEARS this bit; a factory
            # reset / RMA-swapped servo can ship it set, which the mode + limit
            # checks above do not catch. Refuse rather than boot-home skewed.
            if phase & (1 << 4):
                return False, (
                    f'[FEHLER] Servo {sid}: Multi-Turn-Modus aktiv '
                    '(Phase-Bit 4) — Positionswerte laufen über und '
                    'verfälschen die Kinematik. Bitte tools/edu6_provision.py '
                    'auf der Werkbank erneut ausführen.')
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
            # Boot plausibility (2026-07-25). Hand-guiding is torque-OFF, so a
            # student can push a joint past ±180° of its designed zero; the
            # encoder then reports the OTHER side of our tick↔angle map — a 360°
            # error — and the boot-home glide would drive that joint the long way
            # into its mechanical stop at full torque. Refuse instead. The probe
            # loop retries every 5 s, so simply moving the joint back recovers
            # without a restart.
            #
            # The band is deliberately wide: a joint resting AGAINST its hard
            # stop must never nuisance-refuse a healthy boot, whereas a wrapped
            # reading misses by hundreds to thousands of ticks — so tolerance
            # costs no detection power. (R9 validates the width against where a
            # limp arm actually collapses to.)
            #
            # DELIBERATE NO-OP on any joint whose window is the full register
            # range — joint4/joint6 today, per the keep-±180° decision: nothing
            # can read out of range there. Trimming those windows is what would
            # make this check effective on them.
            if not (lo_exp - BOOT_POSITION_TOLERANCE_TICKS <= present
                    <= hi_exp + BOOT_POSITION_TOLERANCE_TICKS):
                return False, (
                    f'[FEHLER] Servo {sid}: Gelenk steht bei {present} statt im '
                    f'erwarteten Bereich [{lo_exp}, {hi_exp}] — vermutlich wurde '
                    'es von Hand über den Anschlag hinaus gedreht. Bitte das '
                    'Gelenk von Hand zurück in seinen normalen Bereich bewegen; '
                    'die Umgebung startet dann von selbst weiter.')
        return True, ''

    # ── torque ───────────────────────────────────────────────────────────────
    def _seed_goal_from_present_locked(self) -> bool:
        """Write ``Goal_Position = Present_Position`` on all 7 servos. Caller
        MUST hold ``self._bus_lock``; runs immediately BEFORE torque is enabled.

        Without it the servo still holds whatever ``Goal_Position`` its RAM
        carried from before the limp phase, and the instant torque returns it
        drives there at up to ``GOAL_SPEED_CAP_STEPS`` (~4.36 rad/s) from
        wherever the student's hand left the arm — the single largest hazard in
        the torque path. A failed read FAILS the torque-on rather than
        energizing blind: an arm we cannot read is an arm we must not drive."""
        replies = self._bus.sync_read(
            fb.REG_PRESENT_POSITION, 2, list(SERVO_IDS))
        missing = [sid for sid in SERVO_IDS if sid not in replies]
        if missing:
            self.get_logger().error(
                f'[FEHLER] Servo(s) {missing} antworten nicht — das Drehmoment '
                'wird NICHT eingeschaltet (der Arm würde sonst zu einer '
                'veralteten Zielposition fahren). Bitte Kabel und '
                '12-V-Versorgung prüfen, dann die Umgebung neu starten.')
            return False
        payload: dict[int, bytes] = {}
        for sid in SERVO_IDS:
            _err, data = replies[sid]
            tick = fb.decode_sign_magnitude(fb.from_le16(data[0], data[1]), 15)
            # Same 7-byte contiguous write as _write_targets (accel, goal,
            # time, speed) — NEVER the bare 2-byte goal: a joint that sagged
            # outside its EEPROM window gets pulled to the window edge at
            # torque-on, and without these two registers that pull runs at
            # whatever the servo's power-on defaults happen to be. See
            # SEED_SPEED_STEPS.
            payload[sid] = (bytes([WRITE_ACCELERATION])
                            + fb.le16(max(0, min(TICKS_PER_REV - 1, tick)))
                            + fb.le16(0)                   # Goal_Time: no-op
                            + fb.le16(SEED_SPEED_STEPS))   # gentle pull-in
        self._bus.sync_write(fb.REG_ACCELERATION, payload)
        return True

    def set_torque(self, enabled: bool) -> bool:
        try:
            with self._bus_lock:
                # Hold-where-you-are before energizing (never after: the goal
                # must already be correct when the servo first sees torque).
                if enabled and not self._seed_goal_from_present_locked():
                    return False
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
            self._replace_trajectory([])
        ok = self.set_torque(bool(request.data))
        response.success = ok
        response.message = 'ok' if ok else 'Torque-Umschaltung fehlgeschlagen.'
        if not request.data:
            # Dropping torque abandons any in-flight trajectory (a re-torque
            # must never resume a stale goal from before the limp phase).
            self._replace_trajectory([])
        return response

    # ── command rail ─────────────────────────────────────────────────────────
    def _replace_trajectory(self, points, start_mono: float | None = None) -> int:
        """THE single writer for the active trajectory (an empty ``points``
        aborts). Returns the new generation counter.

        Every deliberate install/abort goes through here so ``_traj_gen`` can
        never fall out of step with ``_traj_points``; the write loop's own
        natural EXPIRY of a finished trajectory deliberately does NOT bump it
        (nothing took over the rail — the command simply ended)."""
        with self._traj_lock:
            self._traj_points = points
            if start_mono is not None:
                self._traj_start = start_mono
            self._traj_gen += 1
            return self._traj_gen

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
        self._replace_trajectory(points, time.monotonic())

    def start_boot_home(self) -> None:
        """Quintic-glide to HOME from the measured power-up pose (torque on
        first). Mirrors the OMX entrypoint Phase 3 UX."""
        current = self._read_positions_once()
        if current is None:
            self.get_logger().error(
                '[FEHLER] Startpose konnte nicht gelesen werden — die '
                'Grundstellungs-Fahrt entfällt.')
            return
        # Bounded retry: set_torque refuses (correctly) when the goal-seed read
        # fails, but nothing else ever retries boot torque-on, so a single bus
        # hiccup used to leave the arm LIMP — and therefore collapsing — for the
        # whole session.
        for attempt in range(BOOT_TORQUE_ON_ATTEMPTS):
            if self.set_torque(True):
                break
            if attempt + 1 < BOOT_TORQUE_ON_ATTEMPTS:
                time.sleep(BOOT_TORQUE_ON_RETRY_S)
        else:
            self.get_logger().error(
                f'[FEHLER] Drehmoment konnte nach {BOOT_TORQUE_ON_ATTEMPTS} '
                'Versuchen nicht eingeschaltet werden — der Arm bleibt weich '
                'und sackt zusammen. Bitte Kabel und 12-V-Versorgung prüfen, '
                'dann die Umgebung neu starten.')
            return
        # Re-read AFTER torque-on: a retry costs seconds during which the limp
        # arm keeps sagging, and the glide must start from where it actually is.
        fresh = self._read_positions_once()
        if fresh is not None:
            current = fresh
        gen = self._replace_trajectory(build_boot_home(current), time.monotonic())
        self.get_logger().info(
            'Grundstellungs-Fahrt gestartet (3 s sanfte Bewegung).')
        # Decision A (2026-07-24): verify arrival like the OMX entrypoint
        # Phase-3 verifier (mirror OMX, NOT a lift-then-fold). Daemon thread so
        # it never blocks spin/shutdown.
        threading.Thread(target=self._boot_home_verify, args=(gen,),
                         name='edu6-boothome-verify', daemon=True).start()

    def _boot_home_verify(self, gen: int) -> None:
        """Verify the boot-home glide reached HOME (Decision A — mirror the OMX
        Phase-3 verifier, not a lift-then-fold). Own daemon thread; reads the
        live pose the read loop publishes (no extra bus I/O), re-sends the glide
        from the ACTUAL pose on a stall (bounded; the Feetech current caps make a
        re-send non-destructive), then soft-warns. NEVER hard-fails — the arm
        stays usable."""
        target = list(HOME_JOINTS_RAD)              # 6 arm joints (gripper excl.)
        for attempt in range(BOOT_HOME_VERIFY_MAX_SENDS + 1):
            deadline = (time.monotonic() + BOOT_HOME_DURATION_S
                        + BOOT_HOME_VERIFY_SETTLE_S)
            while time.monotonic() < deadline:
                if self._stop.is_set():
                    return
                time.sleep(0.05)
            if self._bus_fault:
                return   # the read loop already emitted the German bus-fault stop
            if self._traj_gen != gen:
                # Someone else took the command rail (a jog, a workflow move, a
                # torque-off abort). Re-sending boot-home here would FIGHT that
                # command — and this thread is the one that must yield, because
                # boot-home is the lowest-priority motion the node ever makes.
                return
            cur = self._last_positions
            verdict, off = boot_home_verify_decision(
                cur, target, BOOT_HOME_VERIFY_TOL_RAD, attempt,
                BOOT_HOME_VERIFY_MAX_SENDS)
            if verdict == 'arrived':
                self.get_logger().info('Grundstellung erreicht (verifiziert).')
                return
            if verdict == 'nodata':
                self.get_logger().warning(
                    '[WARNUNG] Grundstellung konnte nicht überprüft werden — '
                    'keine Gelenkdaten.')
                return
            if (verdict == 'resend' and self._torque_on
                    and not self._bus_fault):
                self.get_logger().warning(
                    f'[WARNUNG] Grundstellung noch nicht erreicht (Gelenke '
                    f'{off}) — erneuter Anlauf.')
                gen = self._replace_trajectory(
                    build_boot_home(list(cur)), time.monotonic())
                continue
            self.get_logger().warning(
                f'[WARNUNG] Grundstellung nicht vollständig erreicht (Gelenke '
                f'{off}). Der Arm ist einsatzbereit, steht aber evtl. nicht '
                'genau in der Grundstellung — bei Bedarf die Umgebung neu '
                'starten.')
            return

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
                self._read_tick()
            except Exception as e:  # noqa: BLE001
                # This loop must NEVER die. `_bus_fault` is only ever set from
                # inside it, so a raise that killed the thread would leave the
                # WRITE loop happily commanding an arm nobody is reading any
                # more. Previously the guard covered only the sync_read call,
                # while _publish_joint_state sat outside it.
                if not self._bus_fault:
                    self._bus_fault = True
                    self._replace_trajectory([])
                    self.get_logger().error(
                        f'[FEHLER] Fehler beim Lesen der Servo-Daten ({e}) — '
                        'Bewegungen sind gestoppt. Bitte Kabel und '
                        '12-V-Versorgung prüfen, dann die Umgebung neu starten.')
            elapsed = time.monotonic() - t0
            time.sleep(max(0.0, period - elapsed))

    def _read_tick(self) -> None:
        """One read/publish cycle. Any escaping exception latches the bus fault
        in :meth:`_read_loop` — never kills the thread."""
        try:
            with self._bus_lock:
                replies = self._bus.sync_read(
                    fb.REG_PRESENT_POSITION, 6, list(SERVO_IDS))
        except Exception as e:  # noqa: BLE001
            # A transient bus error is NOT a latch — it is handled by the same
            # wall-clock miss window as a silent servo below.
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
                self._replace_trajectory([])
        else:
            self._read_fail_since = None
            self._publish_joint_state(replies)
            if self._bus_fault:
                # Latched fault clears ONLY after a full 7-servo read that ALSO
                # published cleanly. Clearing before the publish would let a
                # persistently-raising publish flip the latch off at the top of
                # every tick and re-latch at the bottom — re-opening the write
                # loop for the width of one cycle, 50× a second, and storming
                # the Protokoll with an alternating error/recovery pair.
                self._bus_fault = False
                self.get_logger().info(
                    'Servobus wieder erreichbar — Bewegungen sind wieder '
                    'möglich.')

    def _publish_joint_state(self, replies: dict) -> None:
        positions, velocities, efforts = [], [], []
        now_error_log = time.monotonic()
        for i, sid in enumerate(SERVO_IDS):
            error, data = replies[sid]
            # The 6-byte read starting at Present_Position spans three
            # registers: 56/57 position, 58/59 speed, 60/61 LOAD. Load was
            # being read and discarded — it is free telemetry.
            raw_pos = fb.from_le16(data[0], data[1])
            raw_vel = fb.from_le16(data[2], data[3])
            raw_load = fb.from_le16(data[4], data[5])
            pos = fb.decode_sign_magnitude(raw_pos, 15)
            vel = fb.decode_sign_magnitude(raw_vel, 15)
            # Present_Load's sign lives in bit 10, NOT bit 15 (per-register
            # convention — see the feetech_bus module docstring).
            load = fb.decode_sign_magnitude(raw_load, 10)
            positions.append(tick_to_rad(pos, JOINT_SIGNS[i]))
            velocities.append(vel * RAD_PER_TICK * JOINT_SIGNS[i])
            # Signed fraction in the URDF direction convention, so a positive
            # effort opposes a positive velocity on every joint regardless of
            # how that servo happens to be mounted.
            efforts.append(load / PRESENT_LOAD_FULLSCALE * JOINT_SIGNS[i])
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
        msg.effort = efforts
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
                    # a torque-off doesn't resurrect a stale trajectory. This
                    # is the ONE write that bypasses _replace_trajectory on
                    # purpose: a command that simply ENDED is not a takeover, so
                    # it must not bump _traj_gen (see _replace_trajectory).
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
        # Drop the trajectory FIRST: the write thread may already be mid-cycle,
        # and a goal landing after the torque-off below would be re-driven at the
        # next torque-on (the same stale-goal surface _seed_goal_from_present
        # closes).
        self._replace_trajectory([])
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

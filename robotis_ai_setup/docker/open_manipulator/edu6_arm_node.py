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
dropped while the arm is limp or the bus is faulted (audits M2/M5), the
boot probe refuses an arm whose EEPROM does not carry the expected
provisioning fingerprint (audits H1/H2), and — because the STS position loop is
NOT wrap-aware (measured 2026-07-26) — the ±180° position-map edge is guarded
three ways: a torque-ON TRANSITION is refused while any joint RESTS on that edge
(see EDGE_REFUSE_MARGIN_TICKS), every torque-on is ABORTED right after energising
if a servo's own naive position error is going the long way round (no transition
gate; see TORQUE_ABORT_ERROR_TICKS), and every COMMANDED tick is clamped clear of
the edge so a finished trajectory can never PIN the goal on it (see
GOAL_EDGE_MARGIN_TICKS — the one of the three that reduces commanded range, by the
outer 11.25° of six joint bounds).

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

# ── torque-on edge guard (measured 2026-07-26 on arm EDU6-0001) ──────────────
# How close to the tick-0/4095 position-map edge ANY joint may rest before a
# torque-ON TRANSITION is refused (see edge_parked_joints).
#
# The measured fact behind it: the STS position loop is NOT wrap-aware. It
# computes its error as a naive `goal − present` over the 0..4095 register, so a
# goal and a position on opposite sides of the 4095/0 boundary make it drive the
# LONG way round. Bench datum: joint6 parked at tick 12 with the goal set to 4090
# — 18 ticks apart the short way — drove UPWARD at 200 steps/s (the SEED_SPEED
# the seed write itself installs, ≈0.31 rad/s ⇒ ~20.5 s for a full turn) and only
# stopped because the bench tool cut torque at 3.7°; nothing in the SERVO would
# have stopped it short of a full revolution (and the gripper wiring runs through
# joint6). The creep rate is why this is an interruptible hazard rather than an
# instant one — do not restate it as the motion cap (GOAL_SPEED_CAP_STEPS): on
# the service path the seeded Goal_Speed is never replaced, because _torque_cb
# clears the trajectory and _write_targets only runs on a non-empty buffer. Only
# the BOOT path overrides it, ~20 ms later, with the boot-home glide.
#
# How that becomes reachable in production: set_torque(True) seeds
# `Goal_Position = Present_Position` read a few milliseconds earlier. On a joint
# resting a tick or two from the boundary, ordinary sensor dither (0.09° per
# tick) can leave the seed on one side and the position on the other, and the
# error jumps from ~0 to ~4095. Exposure is the torque-ON TRANSITION ONLY —
# commanded trajectories interpolate in radians and rad_to_tick clamps into
# [0, 4095], so tick travel is monotonic and never crosses the boundary; boot
# home interpolates from the measured pose; and autonomous grasping never parks
# joint6 outside ±90° (the jaw fold) with joint4 ≡ 0.
#
# WHICH JOINTS: all of them (re-derived 2026-07-26 — the earlier full-circle-only
# derivation rested on a FALSE claim, that trimming a window immunises the joint
# because the servo clamps Goal_Position into it. The clamp bounds the GOAL, not
# the PATH: trim joint4/joint6 to ±170° → window (114, 3982); a joint hand-parked
# at tick 4095 seeds goal 4095, the servo clamps it to 3982, and if `present`
# crosses to tick 2 before the loop reads it the naive error is 3980 ticks = 350°.
# Nor would the plausibility band catch that park — 4095 is only 113 ticks outside
# a ±170° window, well inside BOOT_POSITION_TOLERANCE_TICKS.) Judging every joint
# cannot over-refuse, because a reading within `margin` of a register edge on a
# joint whose designed window does NOT contain that edge is necessarily far
# OUTSIDE the window — at the shipped 40 ticks: joint1 ≥984, joint5 ≥756,
# gripper ≥841 ticks out, i.e. 66°-176° past a designed limit, versus R9's worst
# innocent excursion of 107 ticks. On the boot path BOOT_POSITION_TOLERANCE_TICKS
# (400) already refuses every one of those, so there the check is redundant, not
# additive; on the service re-torque path (where no band runs) it refuses exactly
# the states in which the servo would pull the joint 66°+ to its clamp edge.
# Meanwhile it is the ONLY thing covering the readings that are IN-window and
# therefore invisible to the band:
#   * joint4/joint6 — full-circle windows, every reading is in range;
#   * joint2 (hi = 4095) and joint3 (lo = 0) — their windows TOUCH a register
#     end, so a wrapped or at-the-stop reading there is in-window. Bounded
#     consequence, unlike the roll joints: the shoulder/elbow are structurally
#     interfered at ±180°, so the wrong-way command (goal clamped to the far
#     window end, ~2048 ticks ≈ 180° away) stalls against `Max_Torque 800`
#     instead of completing a revolution. At BOOT a J2/J3 wrap lands 2043-2048
#     ticks outside the window and the band catches it anyway — the genuinely
#     exposed path is the service re-torque;
#   * any future trimmed window, automatically.
#
# Sizing: the dither alone needs 1-2 ticks, so the margin exists to cover the
# read→write window too — a hand still on the arm when „Beenden" is pressed can
# move a free-spinning roll joint at several rad/s, i.e. ~16 ticks across a 5 ms
# window. 40 ticks (3.5°) is ~20× the dither trigger and ~2× that hand case, and
# costs only the outer 3.5° of a joint's travel AT THE TORQUE-ON TRANSITION ONLY
# (jog, replay and hand-guide themselves keep the full range; refused readings are
# tick <= 39 ⇔ <= −176.57° and tick >= 4056 ⇔ >= +176.48°). Measured against the
# real solver: the closest any Edu6IKSolver solution comes to a register edge over
# the operating envelope (target at or above the base plane) is 253 ticks — joint3
# at −157.7493°, world r = 0.215519, z = 0 — 6.3× this margin, and joint3's
# UNCONDITIONAL analytic floor is 247 ticks (q3 = γ + ALPHA0 − π/2 with γ ≥ 0),
# 6.2× it. So autonomous grasping and boot-home can never trip this guard.
# (DO NOT quote 256/−157.52° or 367 or 263 for that minimum: all three were
# artifacts of grids uniform in ρ or xyz, which cannot resolve full extension
# because ρ(γ) is square-root-singular there. The 253 above is bisected, and the
# 247 is analytic — see _GOAL_EDGE_MARGIN_MAX_TICKS, whose ceiling is derived from
# it, and test_edu6_goal_edge_ceiling.py, which measures both.) It
# cannot collide with BOOT_POSITION_TOLERANCE_TICKS' innocent-excursion budget
# (R9's worst was 107 ticks): that band measures distance OUTSIDE a designed
# window, this one distance to the REGISTER edge, and R9's own evidence is that a
# released limp arm settles within ~7° (~80 ticks) of tick 2048 — half a turn from
# either edge. (Do NOT re-cite R9's "0 excursion on joint4/joint6" here: their
# window IS the register, so 0 is definitional, not evidence.)
#
# NOT to be confused with EDUBOTICS_EDU6_GOAL_EDGE_MARGIN_TICKS further down: this
# one is a REFUSAL RADIUS around a joint's measured PARK position, that one is a
# CLAMP on the commanded Goal_Position. Same seam and same polarity, but a
# deliberately different (larger) default, because this guard judges an arm AT REST
# — where sensor dither is the whole threat — while the clamp judges a MOVING joint
# arriving at a pinned goal, which must also survive overshoot. Each has its own
# one-variable rollback; see the sizing note at that knob.
#
# Env-overridable per rig without an image rebuild (compose-forwarded); 0
# disables the guard entirely — the one-variable rollback.
_DEFAULT_EDGE_REFUSE_MARGIN_TICKS = 40
# Upper bound for the env override. `min(tick, 4095 − tick)` peaks at 2047, so a
# margin of TICKS_PER_REV // 2 refuses ALL 4096 ticks — the arm could then never
# torque on at ANY pose. See the polarity note in _parse_edge_margin.
_EDGE_MARGIN_MAX_TICKS = TICKS_PER_REV // 2


def _parse_edge_margin(raw: str | None) -> int:
    # Unset is not an error; a MALFORMED value is bench-facing operator error —
    # surface it once (English, plain print like the rest of the pre-rclpy init
    # path) rather than silently running a band nobody chose.
    #
    # POLARITY — stated explicitly because it is the OPPOSITE of this file's
    # other tick knob: EDUBOTICS_EDU6_BOOT_POS_TOL_TICKS is a TOLERANCE (large =
    # permissive, ">= 4096 disables"), while this one is a REFUSAL RADIUS
    # (0 = disabled, larger = refuses MORE poses). An operator carrying the
    # ">= 4096 disables" habit over would brick the arm — 4096, 4000 and 2048
    # all refuse every one of the 4096 possible readings — so anything at or
    # above _EDGE_MARGIN_MAX_TICKS falls back to the default instead.
    if raw is None or not raw.strip():
        return _DEFAULT_EDGE_REFUSE_MARGIN_TICKS
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        print(f'[WARN] EDUBOTICS_EDU6_EDGE_MARGIN_TICKS={raw!r} is not an '
              f'integer — using the default '
              f'{_DEFAULT_EDGE_REFUSE_MARGIN_TICKS}.', flush=True)
        return _DEFAULT_EDGE_REFUSE_MARGIN_TICKS
    if value < 0:
        print(f'[WARN] EDUBOTICS_EDU6_EDGE_MARGIN_TICKS={raw!r} must be >= 0 '
              f'(0 disables the guard) — using the default '
              f'{_DEFAULT_EDGE_REFUSE_MARGIN_TICKS}.', flush=True)
        return _DEFAULT_EDGE_REFUSE_MARGIN_TICKS
    if value >= _EDGE_MARGIN_MAX_TICKS:
        print(f'[WARN] EDUBOTICS_EDU6_EDGE_MARGIN_TICKS={raw!r} would refuse '
              f'EVERY position, so the arm could never switch its torque on '
              f'(use 0 to DISABLE this guard — a large value does not) — using '
              f'the default {_DEFAULT_EDGE_REFUSE_MARGIN_TICKS}.', flush=True)
        return _DEFAULT_EDGE_REFUSE_MARGIN_TICKS
    return value


EDGE_REFUSE_MARGIN_TICKS = _parse_edge_margin(
    os.environ.get('EDUBOTICS_EDU6_EDGE_MARGIN_TICKS'))


def edge_parked_joints(present_ticks, margin=None):
    """``[(servo_id, tick), …]`` for the joints parked within ``margin`` ticks of
    the tick-0/4095 position-map edge — the state that turns a torque-ON
    TRANSITION into a wrapped position error (see EDGE_REFUSE_MARGIN_TICKS).

    EVERY joint is judged, not a derived "full-circle" subset: the servo's EEPROM
    clamp bounds the seeded GOAL but not the PATH, so a trimmed window does NOT
    immunise a joint, and joint2/joint3 have the map edge AT a designed limit
    where the boot plausibility band is blind. For the joints whose window is
    clear of both register ends the check is redundant with that band rather than
    additive (see the WHICH JOINTS note above), so this cannot over-refuse.

    ``present_ticks`` maps servo id → decoded ``Present_Position``; ids absent
    from it are not judged. Pure — unit-tested without ROS or a bus."""
    margin = EDGE_REFUSE_MARGIN_TICKS if margin is None else margin
    if margin <= 0:
        return []                      # explicit rollback: guard disabled
    out = []
    for sid in SERVO_IDS:
        if sid not in present_ticks:
            continue
        tick = int(present_ticks[sid])
        # Distance to the NEARER edge. A negative or over-range tick (uncleared
        # multi-turn firmware, which probe_bus refuses outright) also trips this
        # — refusing is the safe direction for a reading we cannot place.
        if min(tick, TICKS_PER_REV - 1 - tick) < margin:
            out.append((sid, tick))
    return out


def edge_refusal_message(offenders, self_healing: bool) -> str:
    """German ``[FEHLER]`` line for :func:`edge_parked_joints`: names the joint,
    says which way to move it, and — on the boot path only, whose probe loop
    retries every 5 s — that the environment then continues by itself. Pure."""
    which = ', '.join(f'Gelenk {sid} (Tick {tick})' for sid, tick in offenders)
    verb = 'steht' if len(offenders) == 1 else 'stehen'
    tail = ('Die Umgebung startet dann von selbst weiter.' if self_healing
            else 'Danach erneut versuchen.')
    return (
        f'[FEHLER] {which} {verb} genau am Rand des Positionsbereichs (±180°). '
        'Das Drehmoment wird NICHT eingeschaltet: der Servo würde von dort aus '
        'in die falsche Richtung losfahren (bis zu einer ganzen Umdrehung). '
        'Bitte das Gelenk von Hand ein Stück Richtung Mitte zurückdrehen (etwa '
        f'10° genügen). {tail}')


# ── post-energise wrong-way abort (Rule §2 sign-off, Sven 2026-07-26) ────────
# The edge guard above NARROWS the race; it cannot close it. The wrapped error
# only forms if `present` crosses the 4095/0 boundary BETWEEN the goal-seed read
# and the moment the servo applies `Torque_Enable`. Pure wire time for that
# window, re-derived from build_packet byte counts at 1 Mbit/s 8N1, is 1.570 ms
# (sync_read request 15 B + 7 × 8 B status + seed sync_write 64 B + torque
# sync_write 22 B = 157 B × 10 bits): covering EDGE_REFUSE_MARGIN_TICKS then
# needs 39.1 rad/s, which nothing can produce. But that figure EXCLUDES per-servo
# Return_Delay_Time, pyserial's byte-at-a-time header hunt, 1 ms CDC-ACM USB
# frames through usbipd/vhci_hcd, and GIL/container scheduling — all unbounded.
# At 20 ms the same margin only needs 3.1 rad/s, i.e. one WSL2 stall plus a hand
# on a free-spinning roll joint. So the margin is a probability argument, not a
# proof.
#
# This check needs no such argument: immediately AFTER writing Torque_Enable = 1
# it re-reads position once and, if the servo's OWN naive error is going the long
# way round, drops torque again at once. That caps a runaway at a few ticks
# REGARDLESS of how long the pre-torque window turns out to be — the bench tool
# that measured the defect did exactly this and cut torque after 3.7°.
#
# THE DETECTION TEST. After the seed, Goal_Position == Present_Position, so the
# servo's naive `goal − present` should read ≈ 0. Two populations can make it
# non-zero, and the threshold has to separate them:
#
#   * LEGITIMATE — the servo CLAMPS Goal_Position into its EEPROM
#     Min/Max_Position_Limit window, so a limp joint resting OUTSIDE its window
#     is genuinely pulled to the window edge (the documented gentle pull-in at
#     SEED_SPEED_STEPS). Enumerated over every reading the edge guard permits
#     (tick 40…4055) the worst such distance is 2008 ticks (joint2 or the gripper
#     at tick 40); with the edge guard DISABLED (margin 0, the rollback) the
#     supremum is exactly 2048, at tick 0/4095. At BOOT it is far smaller still —
#     BOOT_POSITION_TOLERANCE_TICKS (400) refuses anything further out, and R9
#     measured the worst innocent excursion at 107 ticks.
#   * WRAPPED — goal and present straddle the seam. |error| = 4095 − (ticks the
#     joint travelled inside the read→write window). Even at an absurd 20 rad/s
#     held for 50 ms that is ≥ 3443.
#
# So the threshold is TICKS_PER_REV // 2 = 2048 with a STRICT `>`: it sits above
# the legitimate supremum (which it therefore cannot reach, guard enabled or
# not) and 1395 ticks below the wrapped infimum. It is not a felt number — it is
# DEFINITIONAL: `|error| > TICKS_PER_REV/2` is algebraically identical to
# `TICKS_PER_REV − |error| < |error|`, i.e. "the naive path the servo is taking is
# strictly longer than the true short way round". Both forms are implemented in
# wrong_way_joints (see the note there on why the pair is not redundant).
_DEFAULT_TORQUE_ABORT_ERROR_TICKS = TICKS_PER_REV // 2
# Floor for the env override. Belt-and-braces ONLY, and the reason first written
# here was FALSE, so do not restore it: it claimed that at or below the boot
# plausibility band the check "would abort exactly the clamp pull-in the boot
# probe deliberately ACCEPTS". It cannot. wrong_way_joints requires the wrapped
# distance to be SHORTER than the naive one, and a clamp pull-in is BY
# CONSTRUCTION already the short way round, so it is excluded at EVERY threshold
# — enumerated: 0 legitimate pull-ins flagged at every threshold from 1 to 4095
# (whereas a magnitude-only test would flag 10,881 states at threshold 1, which
# is exactly why the pair in wrong_way_joints is load-bearing below the default).
# The floor is kept anyway as a guard against a future magnitude-only refactor
# and to keep the knob's legal range self-documenting. Derived from the DEFAULT
# band, not the env-parsed one, so two knobs cannot interact into a state with no
# legal value (a band of 4096 = "disabled" would put the floor at 4097).
_TORQUE_ABORT_MIN_TICKS = _DEFAULT_BOOT_POSITION_TOLERANCE_TICKS + 1
# Ceiling: |goal − present| over a 12-bit register can never exceed 4095, so any
# threshold at or above TICKS_PER_REV silently makes the check dead. That is the
# sibling knob's footgun again (EDUBOTICS_EDU6_BOOT_POS_TOL_TICKS documents
# ">= 4096 disables"), so it is refused by name rather than obeyed.
_TORQUE_ABORT_MAX_TICKS = TICKS_PER_REV - 1


def _parse_torque_abort_ticks(raw: str | None) -> int:
    # Unset is not an error; a MALFORMED value is bench-facing operator error —
    # surface it once (English, plain print like the rest of the pre-rclpy init
    # path) rather than silently running a threshold nobody chose.
    #
    # BOTH extremes are footguns here, so both are refused with the reason:
    # too LOW aborts healthy torque-ons (the arm can then never stay energised),
    # too HIGH makes the check dead. 0 — and only 0 — disables it.
    default = _DEFAULT_TORQUE_ABORT_ERROR_TICKS
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        print(f'[WARN] EDUBOTICS_EDU6_TORQUE_ABORT_TICKS={raw!r} is not an '
              f'integer — using the default {default}.', flush=True)
        return default
    if value == 0:
        return 0                       # explicit rollback: check disabled
    if value < 0:
        print(f'[WARN] EDUBOTICS_EDU6_TORQUE_ABORT_TICKS={raw!r} must be >= 0 '
              f'(0 disables the check) — using the default {default}.',
              flush=True)
        return default
    if value < _TORQUE_ABORT_MIN_TICKS:
        print(f'[WARN] EDUBOTICS_EDU6_TORQUE_ABORT_TICKS={raw!r} is below the '
              f'boot plausibility band ({_TORQUE_ABORT_MIN_TICKS - 1} ticks) and '
              f'therefore outside the range this check is designed for (use 0 to '
              f'DISABLE it) — using the default {default}.', flush=True)
        return default
    if value > _TORQUE_ABORT_MAX_TICKS:
        print(f'[WARN] EDUBOTICS_EDU6_TORQUE_ABORT_TICKS={raw!r} exceeds the '
              f'largest error a 12-bit register can show '
              f'({_TORQUE_ABORT_MAX_TICKS}), so the check could never fire (use '
              f'0 to DISABLE it — a large value does not) — using the default '
              f'{default}.', flush=True)
        return default
    return value


TORQUE_ABORT_ERROR_TICKS = _parse_torque_abort_ticks(
    os.environ.get('EDUBOTICS_EDU6_TORQUE_ABORT_TICKS'))

# How many times the post-energise position read may be attempted before the
# abort gives up and reports a bus problem. Exactly ONE read runs on a healthy
# arm (~0.71 ms of wire time — 15 B request + 7 × 8 B replies); the second exists
# only so a single dropped CDC-ACM reply cannot de-energise a healthy arm, which
# on a backdriving arm means it collapses. A genuinely silent servo is not a
# runaway, so paying 0.71 ms more before reporting it costs nothing.
POST_ENERGISE_READ_ATTEMPTS = 2
# Pre-built so the abort path allocates nothing between the verdict and the
# torque-off write.
_TORQUE_OFF_PAYLOAD = {sid: b'\x00' for sid in SERVO_IDS}


def wrong_way_joints(goal_ticks, present_ticks, threshold=None):
    """``[(servo_id, goal, present, naive_error)]`` for the servos whose freshly
    read position makes the servo's OWN ``Goal_Position − Present_Position``
    error take the LONG way round the 0..4095 register — the measured defect
    (docs §11.11). Pure — unit-tested without ROS or a bus.

    TWO conditions, and the pair is deliberate. ``naive > threshold`` is the
    tunable "how big before we care"; ``wrapped < naive`` is the structural "and
    it really is the wrong way round". At the shipped threshold
    (TICKS_PER_REV // 2) the two are algebraically the SAME test, so the second
    looks redundant — it becomes load-bearing the moment an operator lowers the
    threshold for bench sensitivity, where a plain magnitude test would start
    aborting ordinary EEPROM clamp pull-ins (which are, by construction, already
    the short way). Keeping both means the knob can only ever add sensitivity to
    genuine wrong-way motion, never invent it.

    ``goal_ticks``/``present_ticks`` map servo id → ticks; a servo absent from
    either is not judged. A negative or over-range ``present`` (uncleared
    multi-turn firmware, a garbled reply) makes ``wrapped`` negative, so it
    aborts — refusing a reading we cannot place is the safe direction, the same
    stance :func:`edge_parked_joints` takes."""
    threshold = TORQUE_ABORT_ERROR_TICKS if threshold is None else threshold
    if threshold <= 0:
        return []                      # explicit rollback: check disabled
    out = []
    for sid in SERVO_IDS:
        if sid not in goal_ticks or sid not in present_ticks:
            continue
        goal = int(goal_ticks[sid])
        present = int(present_ticks[sid])
        error = goal - present
        naive = abs(error)
        wrapped = TICKS_PER_REV - naive
        if naive > threshold and wrapped < naive:
            out.append((sid, goal, present, error))
    return out


def wrong_way_abort_message(offenders, torque_off_ok: bool = True) -> str:
    """German ``[FEHLER]`` line for :func:`wrong_way_joints`, built AFTER torque
    has already been dropped. Names each joint with its goal and its measured
    position so the Protokoll carries the evidence a bench log would. Pure."""
    which = ', '.join(f'Gelenk {sid} (Ziel {goal}, Ist {present})'
                      for sid, goal, present, _error in offenders)
    verb = 'wäre' if len(offenders) == 1 else 'wären'
    tail = ('Das Drehmoment wurde sofort wieder ausgeschaltet — der Arm ist '
            'jetzt weich und sackt zusammen.' if torque_off_ok else
            'Das Abschalten des Drehmoments ist FEHLGESCHLAGEN — bitte sofort '
            'das 12-V-Netzteil des Arms ausschalten!')
    return (
        f'[FEHLER] {which} {verb} in die falsche Richtung losgefahren — fast '
        f'eine ganze Umdrehung statt weniger Grad. {tail} Bitte das Gelenk von '
        'Hand ein Stück Richtung Mitte drehen (etwa 10° genügen), dann erneut '
        'versuchen.')


def post_energise_read_failure_message(missing,
                                       torque_off_ok: bool = True) -> str:
    """German ``[FEHLER]`` line for the other post-energise verdict: the arm was
    energised but a servo stopped answering, so we cannot tell whether it is
    running away. An arm we cannot read is an arm we must not drive — the same
    stance the goal-seed read already takes. Pure."""
    tail = ('Das Drehmoment wurde sofort wieder ausgeschaltet.' if torque_off_ok
            else 'Das Abschalten des Drehmoments ist FEHLGESCHLAGEN — bitte '
                 'sofort das 12-V-Netzteil des Arms ausschalten!')
    return (
        f'[FEHLER] Servo(s) {missing} haben direkt nach dem Einschalten des '
        f'Drehmoments nicht mehr geantwortet. {tail} Bitte Kabel und '
        '12-V-Versorgung prüfen, dann die Umgebung neu starten.')


# ── boot fingerprint: firmware + angular resolution (2026-07-26) ──────────────
# Firmware versions this driver treats as SUSPECT — reported, never refused.
#
# THE EVIDENCE DOES NOT SUPPORT A REFUSAL, and that verdict is recorded here so
# it is not re-litigated. The claim under review was "FW 3.9 corrupts SYNC_READ
# batch replies". A source review (2026-07-26) found exactly ONE report naming
# 3.9 anywhere — LeRobot issue #1010, self-diagnosed and CONFOUNDED: the same
# servos were 7.4 V units being run at 12 V with the overvoltage LED flashing,
# the reporter changed both variables at once and wrote "one of these cases is
# enough", the failing call was a Torque_Enable WRITE during calibration rather
# than a sync_read, and no maintainer confirmed it. No vendor release note
# documents it. The only cross-version behaviour difference the vendor tables DO
# document is register 39's speed-loop I coefficient (÷10 since 3.6), unrelated.
#
# What IS maintainer-acknowledged upstream (LeRobot PR #3888) is a firmware-
# AGNOSTIC, load-dependent transient sync_read corruption "typically when several
# joints move at once", whose accepted fix is bounded RETRIES (max_read_retry=3),
# not a version gate — and whose originating report (#3131) was on Hiwonder, not
# Feetech, hardware. Refusing 3.9 would therefore refuse a possibly-healthy arm
# on one confounded anecdote, with no env rollback, which is precisely the
# misdiagnosis the guard stack exists to prevent. So: name it, do not refuse it.
# Promote to a refusal only on a reproduction. The real follow-up is a bounded
# sync_read retry in _read_tick — a change to the read path, so Rule §2, and out
# of scope here.
FIRMWARE_SUSPECT_VERSIONS = frozenset({(3, 9)})


def firmware_fingerprint_notes(versions) -> list[str]:
    """German ``[WARNUNG]`` lines for a bus-wide firmware read.

    ``versions`` maps servo id → ``(major, minor)``. Returns [] on a uniform bus
    of unsuspected firmware, so the healthy arm stays silent. Pure — unit-tested
    without ROS or a bus.

    Two findings, both diagnostics rather than gates:

    * a NON-UNIFORM bus. Well-sourced: upstream LeRobot hard-raises on it
      (``_assert_same_firmware``). On this arm it means one servo was swapped
      (RMA, a spare from a different batch) and its behaviour may differ from the
      six it shares a bus with. EDU6-0001 is 3.10 on all seven, so this can never
      fire on the reference arm.
    * a version in :data:`FIRMWARE_SUSPECT_VERSIONS`. Deliberately hedged
      wording: the line must not send a student flashing firmware for what is
      more likely a load-dependent bus fault (see the constant's comment)."""
    if not versions:
        return []
    notes: list[str] = []
    listed = ', '.join(
        f'Servo {sid}: {fb.format_firmware(*versions[sid])}'
        for sid in sorted(versions))
    if len(set(versions.values())) > 1:
        notes.append(
            f'[WARNUNG] Die Servos haben UNTERSCHIEDLICHE Firmware-Versionen '
            f'({listed}). Vermutlich wurde ein Servo getauscht. Der Arm läuft '
            'weiter, kann sich aber anders verhalten als bei der '
            'Provisionierung — bitte beim Support melden, falls Bewegungen '
            'ungewöhnlich sind.')
    suspect = sorted(sid for sid, ver in versions.items()
                     if tuple(ver) in FIRMWARE_SUSPECT_VERSIONS)
    if suspect:
        vers = ', '.join(sorted({fb.format_firmware(*versions[sid])
                                 for sid in suspect}))
        notes.append(
            f'[WARNUNG] Servo(s) {suspect} melden Firmware {vers}. Für diese '
            'Version gibt es einen einzelnen, unbestätigten Fremdbericht über '
            'Lesefehler am Servo-Bus. Der Arm läuft normal weiter; NUR falls '
            'gehäuft Lesefehler auftreten, ist ein Firmware-Update mit dem '
            'Feetech-Werkzeug eine der möglichen Ursachen.')
    return notes


def angular_resolution_note(offenders) -> str:
    """German ``[WARNUNG]`` line for servos whose ``Angular_Resolution`` (register
    30) is not the single-turn factory value 1. ``offenders`` is
    ``[(servo_id, value)]``; returns '' when empty. Pure.

    A WARNING AND NOT A REFUSAL — deliberately, see Edu6ArmNode.probe_bus."""
    if not offenders:
        return ''
    which = ', '.join(f'Servo {sid}: {value}' for sid, value in offenders)
    return (
        f'[WARNUNG] Winkelauflösung (Register 30) ist nicht 1 ({which}). '
        f'Erwartet ist {fb.ANGULAR_RESOLUTION_SINGLE_TURN}. Damit können alle '
        'Winkel des Arms falsch skaliert sein. Der Arm startet trotzdem — bitte '
        'tools/edu6_provision.py auf der Werkbank erneut ausführen und die '
        'Bewegungen danach genau beobachten.')


# Torque-refusal kinds that describe a PHYSICAL arm state rather than a bus
# problem: retrying cannot change them, and the „Kabel und 12-V-Versorgung
# prüfen" wording would send the student diagnosing the wrong thing. ONE place
# says which kinds those are, so the boot path and the retry rail cannot drift.
BOOT_PHYSICAL_REMEDY_DE = {
    'edge': (
        '[FEHLER] Grundstellungs-Fahrt entfällt: ein Gelenk steht am Rand des '
        'Positionsbereichs (siehe Meldung darüber). Bitte das Gelenk von Hand '
        'zurück Richtung Mitte drehen und die Umgebung neu starten.'),
    'wrongway': (
        '[FEHLER] Grundstellungs-Fahrt entfällt: ein Gelenk wäre in die falsche '
        'Richtung losgefahren und wurde sofort wieder stromlos geschaltet '
        '(siehe Meldung darüber). Der Arm ist jetzt weich. Bitte das Gelenk von '
        'Hand ein Stück Richtung Mitte drehen und die Umgebung neu starten.'),
}
PHYSICAL_TORQUE_REFUSALS = tuple(BOOT_PHYSICAL_REMEDY_DE)

# ── commanded-goal edge clamp (Rule §2 sign-off, Sven 2026-07-26) ─────────────
# How far a tick we are about to WRITE as `Goal_Position` is kept away from the
# tick-0/4095 position-map edge (see rad_to_command_tick).
#
# THE HAZARD IT CLOSES — seam PARKING, which is NOT the same thing as the parked
# READING the torque-on edge guard above refuses, and which neither shipped guard
# can see. Six of the twelve ARM-joint limit bounds (14 counting the gripper) map
# EXACTLY onto a register end at the shipped signs:
#
#     joint2 hi (+3.1416 → 4095)      joint3 lo (−3.1416 → 0)
#     joint4 lo AND hi (0 / 4095)     joint6 lo AND hi (0 / 4095)
#
# _write_targets clamps in RADIANS to JOINT_LIMITS_RAD and then converts, so a
# command AT one of those bounds pins Goal_Position on the seam itself, and the
# write loop drops its buffer ~1 s after a trajectory ends while only ever writing
# on a NON-empty buffer — so the goal STAYS there with GOAL_SPEED_CAP_STEPS
# (2840 steps/s = 4.357 rad/s = 1.44 s/rev) installed, not the gentle seed speed.
# One encoder sample on the far side of the boundary then makes the servo's naive
# `goal − present` ≈ 4095, and because its first step moves AWAY from the boundary
# every later sample keeps the error large: it commits to a FULL REVOLUTION. The
# edge guard is OFF→ON-gated and the post-energise abort only runs at torque-on,
# so neither fires. Reachable with NO external force: hand-guide joint6 past +180°
# (hand-guide is torque-off by design) → the reading wraps to tick ≈0 → a
# „Bewegung aufnehmen" recording stores ≈ −3.14 rad → replaying it commands tick 1.
# R6 measured joint6 hand-swept −178.1° → +179.7°, so that path is proven
# reachable, and WorkshopJog accepts exactly `hi` (`if new < lo or new > hi`)
# while `drive_to` takes an absolute target.
#
# WHICH JOINTS: a BLANKET clamp on every commanded tick, deliberately NOT a
# derived "full-circle joint" subset. Such a subset would MISS joint2's hi and
# joint3's lo — which are exactly as pinned as joint4/joint6 — and that is the
# same false premise the deleted full_circle_joint_indexes() predicate died of.
# The blanket form is a NO-OP on the other joints by ARITHMETIC rather than by
# predicate: at the shipped signs joint1 spans ticks 1024…3072, joint5 1024…3300
# and the gripper 2048…3215, i.e. ≥795 ticks from either register end, so nothing
# they can command is ever touched. It also survives a sign flip for free —
# EDUBOTICS_EDU6_JOINT_SIGNS=−1 on joint2 mirrors its window to (0, 2048), moving
# the pinned bound from hi to lo, which a per-joint predicate would have to
# re-derive.
#
# WHAT IT COSTS: the outer `margin` ticks of commanded travel at those six bounds
# only — 11.25° at the default, i.e. joint4/joint6 command to ±168.66° instead of
# ±180°. Nothing autonomous reaches them: HOME sits 483 ticks from the nearest
# register end (joint3 at −2.40 rad → tick 483) and the closest any real
# Edu6IKSolver solution comes over the OPERATING ENVELOPE (target at or above the
# base plane, z ≥ 0) is 253 ticks — joint3 at −157.7493°, world r = 0.215519,
# z = 0, where the table plane meets joint5's +110° relief limit — with joint4 ≡ 0
# and joint6 folded into ±90° (ticks 1024…3072). Both sit STRICTLY ABOVE the
# fenced maximum margin (246, itself one tick under joint3's unconditional
# ANALYTIC floor of 247 — see _GOAL_EDGE_MARGIN_MAX_TICKS), so no default and no
# legal override can move them.
# (The boot-home glide's HOME endpoint is likewise untouched; its FIRST points are
# not necessarily — see the "wider than the edge guard" note.)
#
# SIZING — 128 ticks (11.25°), deliberately WIDER than the 40 of the torque-on
# edge guard, and NOT sized against sensor dither. Dither is the trigger we have
# MEASURED (1-2 ticks, 0.09-0.18°); it is not the trigger that dominates here:
#
#   * The edge guard judges a joint AT REST whose goal is about to be set equal to
#     its own measured position. Nothing is moving, so dither is genuinely the
#     whole threat and 40 ticks is ~20× it.
#   * This clamp judges a goal that a MOVING joint is arriving at. On top of
#     dither it must also cover OVERSHOOT past the goal — and an overshoot larger
#     than the margin carries `Present_Position` across tick 4095/0, which is the
#     wrapped error, i.e. the clamp is defeated on the very path it exists to
#     protect. (That hazard is not created here: with the goal pinned ON the seam
#     today, ANY overshoot ≥ 1 tick does it. The clamp closes it only insofar as
#     the margin exceeds the real overshoot.)
#
# Overshoot on this arm is UNMEASURED and cannot be derived here: the position
# P/I/D gains have never been read or written (they are not even in the
# feetech_bus register map — reading them is an R5 item), the STS gear train
# backdrives freely (hand-guide depends on that), and GOAL_SPEED_CAP_STEPS is
# 249.6 °/s. It is not a step response in the normal case — trajectories arrive
# quintic with zero terminal velocity and the write loop advances the goal ≤56.8
# ticks per 20 ms — but a trajectory TRUNCATED mid-flight (a „Stoppen", a latched
# bus fault, a chunk that never arrives) pins the goal ~57 ticks ahead of a joint
# travelling at the cap, whose planned decel ramp alone is v²/2a ≈ 807 ticks. So
# tens of ticks is plausible and hundreds is not excluded by anything we know.
#
# What makes a generous margin the obvious choice is that it is FREE up to 246:
# below that, NOTHING autonomous is touched (see the cost note above — the
# tightest real solver solution over the operating envelope sits at tick 253 and
# joint3's unconditional analytic floor at 247), and the only thing that loses
# range is the J4/J6 extremes plus joint2's hi / joint3's lo, which nothing
# legitimate needs (q4 ≡ 0 for all grasping, joint6 is folded into ±90°,
# hand-guide keeps the full physical range because torque is OFF, and a REPLAY
# holding joint6 at 179° can only have come from hand-guiding past the wrap —
# exactly the corrupted artifact this clamp exists to neutralise). Being stingy
# buys nothing and risks the guard being defeated. 128 is 64× the measured dither
# trigger and 11.25° of headroom against the unmeasured one.
#
# THE DEFAULT IS PROVISIONAL pending R5, which measures the loop rate, the gains
# and — add it there — the actual overshoot when a trajectory is truncated at the
# cap. If overshoot measures under ~20 ticks, 40-80 would do; if it measures over
# ~100, revisit whether 246 is enough and whether GOAL_SPEED_CAP_STEPS should come
# down instead (open item Q6) — the ceiling itself cannot simply be raised, it is
# pinned one tick under the solver's analytic floor.
#
# CONSEQUENCE OF BEING WIDER THAN THE EDGE GUARD, recorded rather than hidden: a
# joint can legally be TORQUED and holding INSIDE this clamp band (the edge guard
# permits a park at tick 40, and set_torque's seed — deliberately exempt, see
# rad_to_command_tick — then holds it there). The first command that arrives is
# then clamped, and TWO DIFFERENT NUMBERS describe what the student sees — the
# earlier single "88 ticks more than the student asked for" conflated them:
#
#   * TRAVEL, i.e. how far the joint physically jerks: 128 − 40 = 88 ticks
#     (7.73°). That figure is CONDITIONAL on EDUBOTICS_EDU6_EDGE_MARGIN_TICKS
#     being at its default 40. Set it to 0 — the documented one-variable rollback
#     FOR THAT GUARD — and the permitted park reaches tick 0, so the travel is the
#     full 128 ticks (11.25°). Same after an EXTERNAL displacement of an
#     ALREADY-torqued joint, which has no torque-on event for the edge guard to
#     gate on.
#   * DEVIATION, i.e. how far the joint ends from what was requested:
#     UNCONDITIONALLY up to 128 ticks (11.25°), and in the OPPOSITE direction. At
#     (park = 40, requested tick 0) the clamped goal is 128: the joint travels 88
#     ticks but finishes 128 ticks away from the target it was given. WorkshopJog's
#     reply reports the requested target rather than the achieved one.
#
# That is accepted, because the jerk is always directed AWAY from the seam
# (toward CENTER_TICK), so it can never itself produce a wrapped error, it is
# bounded by the margin, and it is only reachable after someone hand-parked a
# joint within 11.25° of ±180°. An operator who wants the two bands to coincide
# again can set EDUBOTICS_EDU6_EDGE_MARGIN_TICKS=128 — that trades the jerk for a
# wider torque-on refusal band, and it is an .env decision, not a code change.
#
# RESIDUAL, stated honestly: this does not make a wrapped error impossible, it
# raises its price. With the goal clamped into [m, 4095 − m] the servo's naive
# error can only exceed half a revolution once the joint is displaced MORE THAN
# `m` ticks past its commanded goal, through the boundary — enumerated over every
# COMMANDED goal the minimum is exactly m + 1 ticks (129 = 11.34° at the default)
# versus 1 tick (0.088°) with the goal pinned at 4095 today.
#
# THAT 129 IS COMMANDED-GOALS-ONLY. System-wide the floor is lower, because
# _seed_goal_from_present_locked is deliberately EXEMPT from this clamp: the edge
# guard permits a park at tick 40 (its test is `distance < margin`, so distance
# exactly 40 passes), the seed then writes Goal = Present = 40, and the
# post-energise abort cannot see it either (goal ≈ present ⇒ error ≈ 0). After a
# bare /edu6/set_torque(True) — which is what hand-guide's „Beenden" calls — that
# goal SITS there until a trajectory arrives. So the true SYSTEM-WIDE minimum
# forced displacement is 41 ticks (3.60°), not 129. Still 41× today's 1-tick hair
# trigger, and the exemption is deliberate (a guard must not CREATE motion).
#
# Doing that to a TORQUED joint holding against `Max_Torque 800` takes real
# external force, which is the case only a continuous 50 Hz read-loop watchdog can
# see (docs §8.2, a larger Rule §2 step: it can stop motion mid-trajectory). This
# clamp removes the hair trigger; it does not remove the class.
#
# WHAT IT CANNOT DO, PROVEN RATHER THAN ARGUED — the load-bearing property, and
# the one that holds UNCONDITIONALLY. Verified exhaustively over all 4096 park
# positions × all 4096 requested ticks × margins {1, 40, 128, 246} and BOTH
# JOINT_SIGNS conventions (the sign only decides which radian maps to which tick,
# and every tick is covered): the clamp NEVER turns a non-wrapped position error
# into a wrapped one — ZERO counterexamples — and it grows |goal − present| by at
# most exactly `m` (≤ 246 ≪ 2048). Structurally so: the clamp only ever moves a
# tick TOWARD CENTER_TICK, and by at most `m`. The clamp is therefore
# mathematically INCAPABLE of producing the runaway it exists to prevent, whatever
# EDUBOTICS_EDU6_EDGE_MARGIN_TICKS is set to. It is also net-positive in the other
# direction: at the default it turns 16,512 (park, requested) pairs from wrapped
# into non-wrapped.
#
# AND NO, THE SERVO'S OWN EEPROM WINDOW DOES NOT HELP. `Min/Max_Position_Limit`
# clamps the GOAL register, never the physical PATH — the same lesson the deleted
# full_circle_joint_indexes() predicate was killed for. Even taken at face value
# it would be useless exactly where it is needed: the windows of all four joints
# carrying the six seam bounds TOUCH a register end (joint2 (2048, 4095), joint3
# (0, 2048), joint4 and joint6 (0, 4095)), so there is no headroom left to clamp
# into.
#
# Env-overridable per rig without an image rebuild (compose-forwarded); 0 disables
# the clamp entirely — the one-variable rollback.
_DEFAULT_GOAL_EDGE_MARGIN_TICKS = 128
# Upper bound for the env override. It is DERIVED FROM AN ANALYTIC BOUND, not
# from a sweep, and that is the whole point:
#
# Edu6IKSolver.solve() computes q3 = γ + ALPHA0 − π/2, where γ ≥ 0 on the
# elbow-up branch (the only branch it ever returns — the elbow-down fall-through
# is not in-limit anywhere in the reach annulus) and ALPHA0 = −68.33167° is
# derived at import from the URDF chain. So q3 ≥ ALPHA0 − π/2 = −158.33167°
# UNCONDITIONALLY ⇒ its tick is ≥ 247, for EVERY solution, at every target,
# regardless of z. The ceiling is set one tick BELOW that floor: at 246 every
# commandable solver tick lies STRICTLY inside [m, 4095 − m], so the fence does
# not even depend on the clamp's boundary being inclusive.
#
# The densest MEASURED value agrees and is slightly looser: 253 ticks over the
# operating envelope (target at or above the base plane, z ≥ 0) — joint3 at
# −157.7493°, world r = 0.215519, z = 0, the corner where the table plane meets
# joint5's +110° relief limit. Found by BISECTING the reach boundary with solve()
# itself; joint2 gets no closer than 480 there and joint5 no closer than 795.
#
# WHY NOT A SWEEP: this exact quantity has been wrong THREE times (367 → 263 →
# 256; the repo's own record attributes 367 to a 16,136-point sweep and 256 to a
# 977,808-point one), every time from a grid uniform in ρ or in xyz. The reason is
# structural, not laziness: ρ(γ) is SQUARE-ROOT-singular at full extension
# (ρmax − ρ ≈ L2·L3·γ²/2ρmax), so a grid uniform in ρ resolves γ only as
# O(√Δρ) — the corner sits at γ = 0.0102 rad, and a 6001-point ρ grid across the
# whole annulus lands no nearer than γ = 0.0346 and duly reports 269, while
# MORE points buy improvement only as the square root. An analytic bound cannot
# become the fourth artifact.
#
# WHAT THIS CORRECTION DOES *NOT* CHANGE — say it plainly, because the two ratios
# in this file are easy to swap: the headroom of the shipped DEFAULT is ~2×
# (253/128), and it was 2.0× (256/128) before, i.e. materially unchanged. The
# "6.4×" quoted up at EDGE_REFUSE_MARGIN_TICKS is 256/40 — the OTHER guard's
# margin — and it becomes 6.3× (253/40). So neither DEFAULT was ever mis-justified
# by the wrong number. What was mis-justified is this FENCE, which was set equal to
# the wrong measurement and therefore sat ABOVE the true floor.
#
# WHAT THE OLD 256 COST: an operator who took the previous comment at its word
# and set EDUBOTICS_EDU6_GOAL_EDGE_MARGIN_TICKS=256 silently shifted solutions in
# the band [247, 255] onto 256 — up to 9 ticks (0.79°); within the operating
# envelope the reachable part of that band starts at 253, so up to 3 ticks (0.26°)
# of real outer-ring grasp motion. Exactly the autonomous motion the fence exists
# to leave alone.
#
# SCOPE, recorded so it cannot quietly stop being true: "no legal margin shifts a
# real solution" is a claim about the OPERATING ENVELOPE. joint2's upper limit is
# +π, which maps EXACTLY onto tick 4095, and solve() WILL return it — but only for
# targets ≥ 16.4 cm below the arm's base plane (it first enters the 246-tick band
# at 7.96 cm below), and the workspace floor refuses those long before IK is
# consulted.
#
# (The ARITHMETIC collapse bound is far higher and would be a useless fence: not
# until TICKS_PER_REV // 2 does the band [m, 4095−m] invert — see
# rad_to_command_tick for what happens above it. There is deliberately no lower
# fence beyond 0 either: a small margin only WEAKENS this clamp, it can never
# invent motion — unlike EDUBOTICS_EDU6_TORQUE_ABORT_TICKS, where a low value
# causes false aborts and therefore needs a floor.)
#
# Guarded by test_edu6_goal_edge_ceiling.py on the SERVER side (it needs numpy for
# the real solver, which the deps-free driver suite has not got); the deps-free
# test_feetech_bus.py pins the literal and the clamp behaviour only.
_GOAL_EDGE_MARGIN_MAX_TICKS = 246


def _parse_goal_edge_margin(raw: str | None) -> int:
    # Unset is not an error; a MALFORMED value is bench-facing operator error —
    # surface it once (English, plain print like the rest of the pre-rclpy init
    # path) rather than silently clamping commands by an amount nobody chose.
    #
    # POLARITY — this is a CLAMP DISTANCE, the same direction as
    # EDUBOTICS_EDU6_EDGE_MARGIN_TICKS (0 = disabled, larger = clamps HARDER) and
    # the OPPOSITE of EDUBOTICS_EDU6_BOOT_POS_TOL_TICKS (a tolerance, where large
    # = permissive and ">= 4096 disables"). An operator carrying that habit over
    # would wreck the arm's reach: past _GOAL_EDGE_MARGIN_MAX_TICKS the clamp
    # starts moving real solver solutions, and at TICKS_PER_REV // 2 every command
    # collapses onto CENTER_TICK. Anything above the ceiling therefore falls back
    # to the default instead of being obeyed.
    if raw is None or not raw.strip():
        return _DEFAULT_GOAL_EDGE_MARGIN_TICKS
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        print(f'[WARN] EDUBOTICS_EDU6_GOAL_EDGE_MARGIN_TICKS={raw!r} is not an '
              f'integer — using the default '
              f'{_DEFAULT_GOAL_EDGE_MARGIN_TICKS}.', flush=True)
        return _DEFAULT_GOAL_EDGE_MARGIN_TICKS
    if value < 0:
        print(f'[WARN] EDUBOTICS_EDU6_GOAL_EDGE_MARGIN_TICKS={raw!r} must be '
              f'>= 0 (0 disables the clamp) — using the default '
              f'{_DEFAULT_GOAL_EDGE_MARGIN_TICKS}.', flush=True)
        return _DEFAULT_GOAL_EDGE_MARGIN_TICKS
    if value > _GOAL_EDGE_MARGIN_MAX_TICKS:
        print(f'[WARN] EDUBOTICS_EDU6_GOAL_EDGE_MARGIN_TICKS={raw!r} exceeds '
              f'{_GOAL_EDGE_MARGIN_MAX_TICKS} ticks, which is one tick under the '
              f'{_GOAL_EDGE_MARGIN_MAX_TICKS + 1}-tick ANALYTIC floor of the '
              f'closest a real Edu6IKSolver solution can come to a register end '
              f'— past that floor the clamp stops trimming only the jog/replay '
              f'extremes and starts shifting real grasp solutions '
              f'(use 0 to DISABLE this clamp — a large value does not) '
              f'— using the default {_DEFAULT_GOAL_EDGE_MARGIN_TICKS}.',
              flush=True)
        return _DEFAULT_GOAL_EDGE_MARGIN_TICKS
    return value


GOAL_EDGE_MARGIN_TICKS = _parse_goal_edge_margin(
    os.environ.get('EDUBOTICS_EDU6_GOAL_EDGE_MARGIN_TICKS'))


def rad_to_tick(rad: float, sign: int) -> int:
    """Radians → the servo's single-turn tick, clamped into the REGISTER range.

    That [0, TICKS_PER_REV − 1] clamp is its own invariant and must stay
    unconditional: fb.le16 of an out-of-range tick would silently encode a
    different number (and, past 32767, collide with the sign-magnitude bit).

    Deliberately NOT seam-aware — see rad_to_command_tick, which is what every
    Goal_Position write goes through. This one stays a plain coordinate map so a
    future non-command caller (a comparison, a diagnostic, an expected-window
    calculation) cannot silently inherit a safety clamp meant for commands."""
    tick = CENTER_TICK + int(round(rad * sign / RAD_PER_TICK))
    return max(0, min(TICKS_PER_REV - 1, tick))


def tick_to_rad(tick: int, sign: int) -> float:
    return (tick - CENTER_TICK) * RAD_PER_TICK * sign


def rad_to_command_tick(rad: float, sign: int, margin=None) -> int:
    """:func:`rad_to_tick` for a tick that is about to be WRITTEN as
    ``Goal_Position``: additionally clamped ``margin`` ticks clear of the
    tick-0/4095 position-map edge, so a pinned goal can never sit ON the seam of
    the non-wrap-aware STS position loop (see GOAL_EDGE_MARGIN_TICKS for the
    hazard, the blanket-vs-subset derivation, the cost and the residual).

    BLANKET on purpose: the band is applied to every joint, which is a no-op by
    arithmetic wherever the joint's designed travel is already clear of both
    register ends, and which needs no per-joint predicate to keep working after a
    sign flip or a window re-trim.

    THE ONE PLACE that must use this is every Goal_Position writer.
    :meth:`Edu6ArmNode._seed_goal_from_present_locked` is deliberately EXEMPT and
    must stay so: it writes ``Goal = Present`` from a MEASURED tick to hold the
    arm still, so clamping it would command a real move (up to ``margin`` ticks)
    at the exact moment torque arrives — a software guard creating motion, and
    against the seed's whole purpose. The two shipped torque-on guards already own
    that state: a joint parked within ``EDGE_REFUSE_MARGIN_TICKS`` of the edge has
    its torque-ON TRANSITION refused before the seed is written at all, and the
    post-energise abort de-energises anything that starts the long way round
    regardless of transition.

    ``margin`` defaults to the env-parsed knob and is FENCED THERE, not here —
    and the fence is what makes the degenerate range unreachable in production
    (only tests pass ``margin`` explicitly). A caller passing
    ``margin >= TICKS_PER_REV // 2`` by hand collapses the band
    ``[m, 4095 − m]`` to a single value and pins EVERY tick onto it. That value
    is ``CENTER_TICK`` only at EXACTLY 2048: above it ``4095 − m < m``, so
    ``max(m, min(4095 − m, t)) == m`` and the pin lands on ``margin`` itself,
    walking BACK toward the seam — at ``margin = 4000`` the pinned goal is 4000,
    i.e. 95 ticks from the register end, and the clamp has inverted into a
    seam-SEEKING map. Pure — unit-tested without ROS or a bus."""
    margin = GOAL_EDGE_MARGIN_TICKS if margin is None else margin
    tick = rad_to_tick(rad, sign)
    if margin <= 0:
        return tick                    # explicit rollback: clamp disabled
    return max(margin, min(TICKS_PER_REV - 1 - margin, tick))


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
        # Why the LAST refused torque-on was refused: None, or
        # (kind, german_message) with kind in {'read', 'edge'}. Cleared at the
        # top of every set_torque call. The service reply and the boot path both
        # read it so a refusal names its real cause instead of 'fehlgeschlagen'.
        self._torque_refusal: tuple[str, str] | None = None
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
        the port exists, every ping times out) from a partial bus.

        NON-FATAL findings (firmware fingerprint, angular resolution) go out
        through ``logger`` — a parameter this method has always accepted and never
        used. ``main()`` calls it BEFORE rclpy exists and passes no logger, so a
        bare ``print`` is what reaches the GUI Protokoll, the same channel the
        German ``[FEHLER]`` return values use. They are emitted ONCE, only on an
        otherwise-healthy arm, immediately before the success return — a probe that
        is failing for a louder reason must not bury it in advisories."""
        def _warn(text: str) -> None:
            if logger is not None:
                logger.warning(text)
            else:
                print(text, flush=True)

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
        firmware: dict[int, tuple[int, int]] = {}
        resolution_offenders: list[tuple[int, int]] = []
        for i, sid in enumerate(SERVO_IDS):
            try:
                mode = bus.read(sid, fb.REG_OPERATING_MODE, 1)[1][0]
                phase = bus.read(sid, fb.REG_PHASE, 1)[1][0]
                lo = bus.read_u16(sid, fb.REG_MIN_POSITION_LIMIT)
                hi = bus.read_u16(sid, fb.REG_MAX_POSITION_LIMIT)
                present = fb.decode_sign_magnitude(
                    bus.read_u16(sid, fb.REG_PRESENT_POSITION), 15)
                # Two more registers in the SAME try, so a read failure keeps its
                # existing German „Verkabelung prüfen" verdict rather than
                # inventing a firmware/resolution fault out of a dead cable.
                fw = bus.read(sid, fb.REG_FIRMWARE_MAJOR, 2)[1]
                resolution = bus.read(sid, fb.REG_ANGULAR_RESOLUTION, 1)[1][0]
            except fb.FeetechBusError as e:
                return False, (
                    f'[FEHLER] Servo {sid}: EEPROM-Kontrolle fehlgeschlagen '
                    f'({e}) — Verkabelung prüfen und erneut versuchen.')
            firmware[sid] = (fw[0], fw[1])
            # Angular_Resolution (register 30) rescales the whole tick↔angle map.
            # WARN, DO NOT REFUSE — three reasons, in order of weight:
            #  1. The reference arm EDU6-0001 was provisioned BEFORE this register
            #     was known about, so its stored record does not carry it and its
            #     live value has never been read. A hard gate on a value we cannot
            #     predict would brick „Umgebung starten" on the only arm in
            #     existence, and this guard has no env rollback to escape with.
            #  2. The expected value IS the factory default (1), so "gate on the
            #     expected value" and "gate but also accept the factory default"
            #     are the SAME check — the usual compatibility escape hatch does
            #     not exist here.
            #  3. Per the vendor, this register extends multi-turn travel TOGETHER
            #     with Phase bit 4, which the gate above already refuses outright.
            #     So a lone resolution ≠ 1 is plausibly inert on this firmware —
            #     unproven either way, which is an argument for reporting rather
            #     than refusing.
            # Promotion to a hard refusal is a one-line change (return False with
            # this text) and wants: a live reading from EDU6-0001, plus one bench
            # measurement of what a resolution of 2 actually does to
            # Present_Position with Phase bit 4 clear.
            if resolution != fb.ANGULAR_RESOLUTION_SINGLE_TURN:
                resolution_offenders.append((sid, resolution))
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
            # make this check effective on them. Also blind to the register-end
            # side of joint2 (hi = 4095) and joint3 (lo = 0), whose windows touch
            # it. All three gaps are covered by the edge guard below, which asks
            # a different question (distance to the REGISTER edge).
            if not (lo_exp - BOOT_POSITION_TOLERANCE_TICKS <= present
                    <= hi_exp + BOOT_POSITION_TOLERANCE_TICKS):
                return False, (
                    f'[FEHLER] Servo {sid}: Gelenk steht bei {present} statt im '
                    f'erwarteten Bereich [{lo_exp}, {hi_exp}] — vermutlich wurde '
                    'es von Hand über den Anschlag hinaus gedreht. Bitte das '
                    'Gelenk von Hand zurück in seinen normalen Bereich bewegen; '
                    'die Umgebung startet dann von selbst weiter.')
            # Torque-on edge guard, boot half (2026-07-26). It COMPLETES the band
            # above rather than duplicating it: for a joint whose window is clear
            # of both register ends the band has already refused any near-edge
            # reading (they land 756+ ticks outside), so the value added here is
            # exactly the IN-WINDOW near-edge readings the band is blind to —
            # joint4/joint6 anywhere (full-circle windows), joint2 at the 4095 end
            # and joint3 at the 0 end (their windows touch a register end). The
            # authoritative copy runs in set_torque (the single choke point, which
            # also covers the re-torque after hand-guide); this one exists because
            # it fails BEFORE any ROS entity is created, prints straight into the
            # GUI Protokoll, and self-heals through main()'s 5 s probe retry —
            # whereas start_boot_home runs exactly once, so letting the refusal
            # surface there would leave the arm limp for the session. No
            # transition gate is needed here: nothing is torqued yet at boot.
            offenders = edge_parked_joints({sid: present})
            if offenders:
                return False, edge_refusal_message(offenders, self_healing=True)
        # Non-fatal fingerprint findings, emitted once on an otherwise-healthy arm.
        note = angular_resolution_note(resolution_offenders)
        if note:
            _warn(note)
        for line in firmware_fingerprint_notes(firmware):
            _warn(line)
        return True, ''

    # ── torque ───────────────────────────────────────────────────────────────
    def _seed_goal_from_present_locked(self) -> dict[int, int] | None:
        """Write ``Goal_Position = Present_Position`` on all 7 servos. Caller
        MUST hold ``self._bus_lock``; runs immediately BEFORE torque is enabled.
        Returns ``{servo_id: EFFECTIVE goal tick}`` on success (see below) or
        ``None`` when the torque-on must be refused.

        Without it the servo still holds whatever ``Goal_Position`` its RAM
        carried from before the limp phase, and the instant torque returns it
        drives there at up to ``GOAL_SPEED_CAP_STEPS`` (~4.36 rad/s) from
        wherever the student's hand left the arm — the single largest hazard in
        the torque path. A failed read FAILS the torque-on rather than
        energizing blind: an arm we cannot read is an arm we must not drive.

        The same read feeds the torque-on EDGE GUARD (2026-07-26): a joint parked
        on the tick-0/4095 map edge is refused here, BEFORE any register is
        written, because seeding a goal there is exactly what makes the
        non-wrap-aware servo loop drive the long way round (see
        EDGE_REFUSE_MARGIN_TICKS). It is evaluated ONLY on an OFF→ON TRANSITION —
        see the gate below. Both refusals record their German reason in
        ``self._torque_refusal`` so the caller can report the real cause.

        The returned goals are the EFFECTIVE ones — what the servo will actually
        hold, i.e. the written tick clamped into that servo's EEPROM
        ``Min/Max_Position_Limit`` window. That is not modelling on faith:
        :meth:`probe_bus` HARD-GATES on every servo's EEPROM window equalling
        ``fb.position_limit_window(...)`` to ±1 tick and refuses to create
        ``/joint_states`` otherwise, so the computed clamp is accurate to ±1 tick
        against a 2048-tick abort threshold, at zero bus cost.

        HONEST ACCOUNTING OF THAT CHOICE — twice corrected, so read the whole
        thing before "improving" it. Revision 1 justified computing on bus COST
        and was wrong: reading the goal back is cheap. ``REG_GOAL_POSITION`` is
        42, 2 bytes, documented, so one ``sync_read(42, 2, SERVO_IDS)`` is 71 B /
        0.710 ms — the same shape and cost as the position read
        :meth:`_verify_after_energise_locked` already performs. Revision 2 then
        called it an open bench question and promised that settling it would make
        reading register 42 back "a free upgrade closing both residuals at once".
        THAT PROMISE WAS BACKWARDS, and here is why:

        Evidence (LeRobot issue #3585, 2026-05, open and unconfirmed by
        maintainers, one reporter's bench observation on an SO-101 — so SINGLE
        SOURCE, still worth settling here) says register 42 reads back the RAW
        value the host wrote, verbatim: "The ``Goal_Position`` register itself is
        left at the value the host wrote — there is no register-level indication
        that the firmware substituted a different target — so the substitution is
        invisible above the bus." Their repro writes goal 228 to a servo whose
        window is [2045, 3919], reads 42 back as 228, and measures the joint at
        ~3472.

        If that holds, COMPUTING THE CLAMP IS THE CORRECT MODEL and reading
        register 42 would be the WRONG one. The servo drives toward its clamped
        target, so the clamped tick is what its own naive error is computed from;
        a read of the raw value would model that error wrongly — by up to the full
        clamp distance — precisely on the path whose whole job is deciding whether
        the arm is running away. Reading 42 is therefore not an upgrade to this
        model and must not replace it.

        What reading it back WOULD still buy is the OTHER, unrelated residual: a
        seed ``sync_write`` LOST on the wire is invisible to a computed model,
        because SYNC_WRITE takes no reply. That is a SEPARATE check — "did my
        write land?" — not a better source for the abort comparison, and it should
        be added as one if it is ever wanted, without touching the clamp maths.

        For that separate check the register to read is NOT 42 and NOT 67. A
        "current target position" at register 67 is UNSUBSTANTIATED: the current
        official memory table (磁编码 SMS&STS) lists 67 as 无定义 — undefined, 2
        bytes, SRAM, read-only — the older per-model ST3215 "V3.7" table omits 67
        entirely (jumping 66 → 69), and the vendor SDK defines nothing there. The
        one source naming it 当前目标位置 is an SMS-1.0-era community page that
        could not be retrieved. The plausible candidate is register 71, which
        upstream LeRobot carries as a read-only ``Goal_Position_2`` — also
        UNVERIFIED as to what it actually returns. Settle on the bench before
        relying on either.

        SEPARATE HAZARD FROM THE SAME SOURCE, recorded because this method is
        where it would land and it is not modelled anywhere today: #3585 also
        reports that after an OUT-OF-WINDOW goal write the firmware sets
        ``Torque_Enable = 1`` BY ITSELF on the next PID tick, regardless of the
        host having disabled torque. The seed below writes the measured tick
        clamped only into the REGISTER range, not into the EEPROM window, so on a
        joint resting outside its window (R9 measured up to 107 ticks) this IS an
        out-of-window goal write issued while torque is off. The pull-in itself is
        already expected and budgeted for (see TORQUE_ABORT_ERROR_TICKS), and the
        gentle ``SEED_SPEED_STEPS`` rides in the same packet, so the consequence
        looks bounded — but "a goal write can energise a limp arm" contradicts the
        torque-off-is-always-safe assumption that hand-guide rests on, and it
        deserves a bench check rather than a desk verdict.

        See :meth:`_verify_after_energise_locked`."""
        replies = self._bus.sync_read(
            fb.REG_PRESENT_POSITION, 2, list(SERVO_IDS))
        missing = [sid for sid in SERVO_IDS if sid not in replies]
        if missing:
            message = (
                f'[FEHLER] Servo(s) {missing} antworten nicht — das Drehmoment '
                'wird NICHT eingeschaltet (der Arm würde sonst zu einer '
                'veralteten Zielposition fahren). Bitte Kabel und '
                '12-V-Versorgung prüfen, dann die Umgebung neu starten.')
            self._torque_refusal = ('read', message)
            self.get_logger().error(message)
            return None
        ticks: dict[int, int] = {}
        for sid in SERVO_IDS:
            _err, data = replies[sid]
            ticks[sid] = fb.decode_sign_magnitude(
                fb.from_le16(data[0], data[1]), 15)
        # Edge guard — BEFORE the seed write, so a refused torque-on leaves the
        # servo's registers exactly as they were.
        #
        # OFF→ON TRANSITIONS ONLY. The wrapped error needs `present` to cross the
        # 4095/0 boundary between this read and the moment the loop sees the new
        # goal. A joint that is ALREADY torqued is tracking a goal our own write
        # loop set, and commanded tick travel is monotonic (interpolation happens
        # in radians and rad_to_command_tick clamps into [margin, 4095 − margin]),
        # so it cannot cross the boundary; the seed it then gets is the goal it is
        # already holding, and
        # a held joint cannot travel 40 ticks (3.5°) inside a ~ms read→write
        # window. Judging it anyway BROKE JOGGING: the server asserts torque ON
        # defensively on EVERY WorkshopJog call, so an arm already parked beyond
        # ±176.5° on joint4/joint6 — inside the URDF window the jog accepts —
        # refused, and three of those in a row produce „Arm konnte wiederholt
        # nicht verriegelt werden … Umgebung neu starten": a misdiagnosis (the
        # arm IS locked) whose remedy probe_bus then refuses at the same tick.
        #
        # `not self._bus_fault` is part of the condition because `_torque_on` is
        # our own BELIEF, never a reading: a 12 V brown-out drops real torque
        # without touching the flag. While the fault latch is up we cannot see
        # the arm at all, i.e. exactly when the flag is least trustworthy — so
        # the guard re-arms there, at zero cost to a healthy jog. Residual
        # (documented, not closed here): a dropout SHORTER than READ_FAIL_STOP_S,
        # or one whose latch has already cleared, still leaves the flag
        # optimistic. Only a post-`Torque_Enable` re-read could settle that
        # without believing a flag, and that is a NEW software guard (Rule §2).
        offenders = ([] if self._torque_on and not self._bus_fault
                     else edge_parked_joints(ticks))
        if offenders:
            message = edge_refusal_message(offenders, self_healing=False)
            self._torque_refusal = ('edge', message)
            self.get_logger().error(message)
            return None
        payload: dict[int, bytes] = {}
        goals: dict[int, int] = {}
        for sid, (lo_rad, hi_rad), sign in zip(SERVO_IDS, JOINT_LIMITS_RAD,
                                               JOINT_SIGNS):
            tick = max(0, min(TICKS_PER_REV - 1, ticks[sid]))
            # Same 7-byte contiguous write as _write_targets (accel, goal,
            # time, speed) — NEVER the bare 2-byte goal: a joint that sagged
            # outside its EEPROM window gets pulled to the window edge at
            # torque-on, and without these two registers that pull runs at
            # whatever the servo's power-on defaults happen to be. See
            # SEED_SPEED_STEPS.
            payload[sid] = (bytes([WRITE_ACCELERATION])
                            + fb.le16(tick)
                            + fb.le16(0)                   # Goal_Time: no-op
                            + fb.le16(SEED_SPEED_STEPS))   # gentle pull-in
            # …and the goal the SERVO will hold is that tick clamped into its
            # EEPROM window (same shared implementation probe_bus verified the
            # plugged arm against). That is the number the servo's own naive error
            # is computed from, so it is the number the post-energise abort must
            # compare against — not the tick we wrote.
            lo_exp, hi_exp = fb.position_limit_window(lo_rad, hi_rad, sign)
            goals[sid] = max(lo_exp, min(hi_exp, tick))
        self._bus.sync_write(fb.REG_ACCELERATION, payload)
        return goals

    def _verify_after_energise_locked(self, goal_ticks) -> tuple[str, str] | None:
        """Re-read position ONCE immediately AFTER ``Torque_Enable = 1`` and drop
        torque again at once if a servo is taking the long way round. Caller MUST
        hold ``self._bus_lock``. Returns ``None`` when the arm is safe, else
        ``(kind, german_message)`` — and by the time that returns, torque is
        ALREADY off.

        WHY THIS AND NOT A BIGGER MARGIN: the edge guard's band is a probability
        argument over an unbounded timing window (see TORQUE_ABORT_ERROR_TICKS).
        This check measures what the arm ACTUALLY did after energising, so it caps
        a runaway at a few ticks no matter how long that window turns out to be,
        and it believes no flag: it is deliberately NOT gated on the OFF→ON
        transition the edge guard needs. That is safe precisely because it judges
        MEASURED motion rather than a park position — a torqued, holding joint
        parked at tick 4094 reports ``goal − present`` ≈ 0 and passes, which is why
        running it on every torque-on (the server asserts one per ``WorkshopJog``)
        cannot resurrect the jogging regression the edge guard's gate exists for.
        It therefore also covers the two states that gate skips: a ``_torque_on``
        flag left optimistic by a brown-out shorter than READ_FAIL_STOP_S, and an
        already-torqued joint on the seam whose dither straddles it while a
        redundant torque-on re-seeds the goal.

        WHAT IT DOES NOT COVER: a joint forced across the seam while already
        torqued, with no torque-on anywhere near it — there is no event for this
        to hang off. Only a continuous watchdog in the 50 Hz read loop could see
        that, which can stop motion mid-trajectory and is a larger Rule §2 step
        (assessed in docs §11.13, not implemented). Also not covered: a seed
        ``sync_write`` LOST on the wire, since SYNC_WRITE is fire-and-forget and
        the goals handed in here are computed rather than read back. Note that
        reading ``Goal_Position`` (42) back is NOT the fix for the goals used
        here — it reads back the RAW written value, not the clamped target the
        servo drives to, so it would model this comparison wrongly; it is only a
        separate "did my write land?" check. See
        :meth:`_seed_goal_from_present_locked` for the evidence and the register
        candidates."""
        present: dict[int, int] = {}
        for _attempt in range(POST_ENERGISE_READ_ATTEMPTS):
            present = {}
            # The DECODE is inside the guard too, not just the sync_read: any raise
            # escaping to set_torque's bare `except` would leave the arm ENERGISED
            # with no diagnosis, which is the one outcome this method exists to
            # prevent. A partial `present` from a mid-loop raise is fine — the
            # `missing` branch below aborts on it.
            try:
                replies = self._bus.sync_read(
                    fb.REG_PRESENT_POSITION, 2, list(SERVO_IDS))
                for sid in SERVO_IDS:
                    reply = replies.get(sid)
                    if reply is None:
                        continue
                    data = reply[1]
                    present[sid] = fb.decode_sign_magnitude(
                        fb.from_le16(data[0], data[1]), 15)
            except Exception:  # noqa: BLE001
                pass
            if len(present) == len(SERVO_IDS):
                break                  # healthy arm: exactly ONE read, ~0.71 ms
        offenders = wrong_way_joints(goal_ticks, present)
        missing = [sid for sid in SERVO_IDS if sid not in present]
        if not offenders and not missing:
            return None
        # ── DE-ENERGISE. Nothing — no logging, no message building, no lock
        # juggling, no allocation (the payload is a module constant) — happens
        # between the verdict above and this write. ──
        torque_off_ok = True
        try:
            self._bus.sync_write(fb.REG_TORQUE_ENABLE, _TORQUE_OFF_PAYLOAD)
        except Exception:  # noqa: BLE001
            torque_off_ok = False
        # Our BELIEF must follow the hardware immediately: leaving it True would
        # let the write loop keep commanding a limp arm and let _trajectory_cb
        # accept new trajectories for it.
        self._torque_on = False
        if offenders:
            return 'wrongway', wrong_way_abort_message(offenders, torque_off_ok)
        return 'read', post_energise_read_failure_message(missing, torque_off_ok)

    def set_torque(self, enabled: bool) -> bool:
        # THE single torque choke point: /edu6/set_torque, its legacy alias and
        # start_boot_home all come through here, so the edge guard inside the
        # seed below covers every torque-ON TRANSITION there is (it self-gates on
        # `_torque_on`, so a redundant torque-on over an already-locked arm is a
        # no-op rather than a refusal), and the post-energise abort after the
        # Torque_Enable write covers every torque-on WITHOUT that gate. Torque-OFF
        # is never guarded — dropping torque is always allowed.
        self._torque_refusal = None
        refusal: tuple[str, str] | None = None
        # Hoisted OUT of the try on purpose: the handler at the bottom reads it
        # to decide whether a seed already reached the wire. Assigned inside,
        # any raise before that assignment would turn that read into a
        # NameError — a crash on the one path whose whole job is to leave the
        # arm in a known state.
        goal_ticks: dict[int, int] | None = None
        try:
            with self._bus_lock:
                if enabled:
                    # Hold-where-you-are before energizing (never after: the goal
                    # must already be correct when the servo first sees torque).
                    goal_ticks = self._seed_goal_from_present_locked()
                    if not goal_ticks:
                        return False
                self._bus.sync_write(
                    fb.REG_TORQUE_ENABLE,
                    {sid: bytes([1 if enabled else 0]) for sid in SERVO_IDS})
                if enabled:
                    refusal = self._verify_after_energise_locked(goal_ticks)
            if refusal is not None:
                self._torque_refusal = refusal
                self.get_logger().error(refusal[1])
                # The arm is limp again, so any trajectory installed for this
                # torque-on must not survive: the next torque-on would otherwise
                # interpolate it from wherever the arm sagged to (audit M2). Done
                # OUTSIDE _bus_lock — _traj_lock is never nested inside it.
                self._replace_trajectory([])
                return False
            self._torque_on = bool(enabled)
            return True
        except Exception as e:  # noqa: BLE001
            self.get_logger().error(f'torque switch failed: {e}')
            # A raise anywhere AFTER the seed landed leaves us BELIEVING the arm
            # is limp — `_torque_on` was never set, so every caller reports it
            # limp — while it may already be ENERGISED. The seed is written
            # BEFORE Torque_Enable by design (the goal must be correct the
            # instant the servo first sees torque), and LeRobot #3585 reports the
            # STS firmware setting `Torque_Enable = 1` BY ITSELF on the next PID
            # tick after an out-of-window goal write, regardless of the host
            # having disabled torque. That report is unmeasured on this arm (A12,
            # bench), so this closes the state UNCONDITIONALLY rather than
            # resting on the answer: „we believe limp, the arm is live" is the
            # one state a student may reach into.
            #
            # ONLY this path needs it, and the other two failure exits above are
            # already covered — neither of them even reaches this handler:
            #   * the `not goal_ticks` early return — BOTH of the seed's
            #     refusals (failed position read, edge guard) happen BEFORE the
            #     seed sync_write, so no register was touched at all;
            #   * the `refusal is not None` path — reachable only through
            #     _verify_after_energise_locked, whose FIRST action is this same
            #     torque-off write.
            # What `goal_ticks` therefore actually discriminates HERE is the one
            # remaining shape: a raise from the SEED write itself, where nothing
            # is known to have landed (measured — replacing this with a bare
            # `if enabled:` changes exactly that case). `enabled` excludes a
            # failed torque-OFF: there the arm may legitimately still be torqued
            # and forcing the belief to False would be the same wrong belief
            # inverted.
            #
            # RESIDUAL, recorded rather than papered over: a seed packet that was
            # PARTIALLY transmitted before the raise is indistinguishable from
            # one never sent, so that sliver stays uncovered. It is the same
            # fire-and-forget blind spot SYNC_WRITE has everywhere in this driver
            # (see _seed_goal_from_present_locked on the lost-write residual).
            #
            # Re-acquiring `_bus_lock` cannot deadlock: it is a plain
            # (non-reentrant) threading.Lock and the `with` above already
            # released it while the exception propagated out of the block.
            if enabled and goal_ticks:
                try:
                    with self._bus_lock:
                        self._bus.sync_write(fb.REG_TORQUE_ENABLE,
                                             _TORQUE_OFF_PAYLOAD)
                    # Belief follows the HARDWARE, and only on a write that
                    # actually landed — claiming limp after a FAILED drop is the
                    # same wrong belief in the other direction: on a REDUNDANT
                    # torque-on over an arm that really is holding (possibly
                    # holding an object), it would make _trajectory_cb start
                    # refusing trajectories with „Drehmoment ist aus" for an arm
                    # that is not.
                    #
                    # DELIBERATELY UNLIKE _verify_after_energise_locked, which
                    # sets the flag False even when its own drop failed — do not
                    # "harmonise" the two. There, the Torque_Enable=1 write is
                    # KNOWN to have landed, so silencing our own command paths on
                    # a runaway is the safe direction. Here the write RAISED, so
                    # torque state is genuinely unknown and the prior belief is
                    # the least-wrong thing to keep. Neither flag can express
                    # "unknown"; the German line below is the real mitigation.
                    self._torque_on = False
                except Exception:  # noqa: BLE001 — best-effort
                    # The bus is already failing, so there is nothing left that
                    # software can do: name the one PHYSICAL action instead.
                    self.get_logger().error(
                        '[FEHLER] Das Abschalten des Drehmoments ist '
                        'FEHLGESCHLAGEN — der Arm kann unter Strom stehen und '
                        'sich jederzeit bewegen. Bitte sofort das '
                        '12-V-Netzteil des Arms ausschalten, bevor Sie den Arm '
                        'anfassen!')
            return False

    def _torque_cb(self, request, response):
        if request.data:
            # Audit M2 (belt): entering torque from limp must never resume a
            # trajectory stored before or DURING the limp phase — clear before
            # the first energized write tick can interpolate it.
            self._replace_trajectory([])
        ok = self.set_torque(bool(request.data))
        response.success = ok
        # Carry the REASON, not just the failure: this service is what „Beenden"
        # (hand-guide exit) calls, and hand-guide is exactly what leaves a joint
        # on the position-map edge. A bare 'fehlgeschlagen' would send the
        # student looking at cables.
        refusal = self._torque_refusal
        response.message = ('ok' if ok else
                            refusal[1] if refusal else
                            'Torque-Umschaltung fehlgeschlagen.')
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
            kind = self._torque_refusal[0] if self._torque_refusal else None
            if kind in PHYSICAL_TORQUE_REFUSALS:
                # A joint parked on the position-map edge ('edge'), or one that
                # actually started the wrong way and was de-energised again
                # ('wrongway'), is a PHYSICAL state: retrying cannot change it,
                # and the cable/12-V wording below would send the student
                # diagnosing the wrong thing. set_torque has already logged WHICH
                # joint and why; add only the boot-specific remedy (there is no
                # torque button at boot). probe_bus refuses the 'edge' state
                # 5 s-retrying BEFORE this point, so reaching here means the joint
                # moved after it passed; 'wrongway' has no probe-side twin at all
                # (nothing is energised at probe time), so this is its only report.
                self.get_logger().error(BOOT_PHYSICAL_REMEDY_DE[kind])
                return
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
            # rad_to_COMMAND_tick, never the bare rad_to_tick: the radian clamp
            # above lands six of the twelve arm-joint bounds EXACTLY on tick
            # 0/4095, and the write loop then PINS the goal there once the
            # trajectory ends. See GOAL_EDGE_MARGIN_TICKS / rad_to_command_tick.
            tick = rad_to_command_tick(rad, JOINT_SIGNS[i])
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
            # Model-neutral wording on purpose: this arm mixes STS3215 (joints
            # 1/4/5/6/7) and STS3250 (the high-load shoulder + elbow) by design,
            # so naming one model here reads as a fault on a healthy arm.
            print('[INIT] Alle 7 Servos gefunden (STS-Serie, provisioniert).',
                  flush=True)
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

#!/usr/bin/env python3
"""D1 - does a POSITION COMMAND clear the servo's overload / overcurrent protection?

THE QUESTION
    Three separate safety arguments in this project rest on one unproven premise:
    "the servo current limits are the physical backstop".

      * docs 8.1 - the no-go-zone inflation. The gripper HOUSING spreads 83.6 mm
        off the sampled centreline against a 50 mm inflation, so a zone protects
        the arm's links but the housing can clip an obstacle by ~34 mm. That
        under-coverage was ACCEPTED, explicitly because the servo current limits
        back it up.
      * R4 - the pinch-force floor. Max_Torque 150 on the gripper (800 on the arm)
        plus the factory PROTECTION_CURRENT 310 (~2 A) are what bound the force a
        student can be squeezed with.
      * R7 - self-collision: harmless stall, or damage? "Harmless" means the servo
        gives up.

    Our driver's write loop (`edu6_arm_node._write_targets`) SYNC_WRITEs a fresh
    Goal_Position to all 7 servos every 20 ms, forever, for as long as a trajectory
    is active. If a position command CLEARS the protection state, then on this arm
    the protection can never stay engaged while the environment is up - and all
    three arguments above lose their backstop at once.

    So this tool asks, on real hardware:
      (a) can we get a servo into its protection state at all, and what does the
          status/error byte report (fb.describe_error_bits decodes it)?
      (b) once there, does it STAY there when we stop commanding?
      (c) does resuming a 20 ms position-command stream CLEAR it - i.e. does the
          servo start driving again?

METHOD - five phases, each printing what it observed
    1 BASELINE   read-only. All 7 answering, all 7 verified torque-OFF by READING
                 register 40 back, no pre-existing error bit, protection registers
                 dumped (13/16/28/34/35/36), push room + seam clearance checked.
    2 INDUCE     seed Goal = Present (the driver's own 7-byte write), torque ON the
                 TARGET SERVO ONLY, read register 40 back to prove it took, then
                 command a small push INTO the obstruction. Bounded by --stall-s.
    3 LATCH      stop commanding ENTIRELY (nothing is written) and keep READING.
                 Does the indication persist? Bounded by --latch-s.
    4 CLEAR?     resume a 20 ms position-command stream exactly like _write_targets
                 (same 7-byte block, same GOAL_SPEED_CAP_STEPS) and watch whether
                 the indication clears and whether the servo drives again.
                 Bounded by --probe-s.
    5 CYCLE      (optional, prompted, AFTER torque is already off) re-energise with
                 Goal = Present and re-read the error byte, to separate "a command
                 cleared it" from "a torque cycle cleared it".

    Phases 2-3-4 run CONTIGUOUSLY, with no operator prompt between them. That is
    deliberate and it is a safety property, not an omission: a human-paced pause
    while a servo is stalled would EXTEND the stall, and a torque cycle between
    phases 3 and 4 would confound the very question (it would change two variables
    at once). The single confirmation gate sits before phase 2, where nothing is
    energised yet, and the whole powered sequence has a hard deadline.

SAFETY ENVELOPE - the most important thing about this tool
    * DEFAULT TARGET IS THE GRIPPER (servo 7). It runs Max_Torque 150 (vs 800 on
      the arm joints), it carries no part of the arm's mass, and closing on an
      object is what the product asks it to do anyway - the lowest-energy way to
      reach the same firmware mechanism. --joint N overrides, with a loud warning
      and a SECOND explicit confirmation that --yes deliberately does not satisfy.
    * THE OPERATOR SUPPLIES THE OBSTRUCTION: something compliant and expendable
      (a folded cloth, a foam block, a pencil eraser). NEVER a finger. The tool
      never asks the arm to push against its own structure.
    * THE PUSH IS SMALL AND BOUNDED. --push-ticks (default 60 = 5.3 deg = ~2.3 mm
      of jaw travel) at SEED_SPEED_STEPS (200 steps/s ~ 0.31 rad/s), and the goal
      must sit at least PUSH_ROOM_TICKS inside the servo's own EEPROM window - so
      the stall is against the OBJECT, never against the jaws' own hard stop
      (docs 9.2 calls that out by name).
    * EVERY POWERED PHASE HAS A HARD TIME BOUND, plus a global powered deadline.
    * CONTINUOUS MONITORING WITH AUTOMATIC ABORT on: temperature, current,
      CUMULATIVE high-current ENERGY (I^2 t; non-gripper joints only - see
      HEAT_BUDGET_REF_S. It is the only bound that still bites in the one case
      the time bounds do not cover: the indication latches, so phases 3 and 4
      run, but the current does NOT fall),
      any UNEXPECTED error bit, travel past the commanded push, wrong-way motion,
      consecutive bus read failures, and elapsed time. On ANY abort the FIRST
      thing that happens is Torque_Enable = 0 broadcast to ALL SEVEN servos -
      before any logging, formatting or message building.
    * finally: + SIGINT + SIGTERM + atexit all torque-off all 7. Every exit path
      leaves the arm limp.
    * NOTHING TOUCHES EEPROM. The bus wrapper enforces a WRITE ALLOWLIST of
      exactly two spans - register 40 (Torque_Enable, 1 byte) and registers 41-47
      (the accel/goal/time/speed block). Max_Torque, the protection registers, the
      position limits and Homing_Offset are unreachable by construction, so this
      tool can run and leave the arm's provisioning byte-identical.
    * Register 40 is written ONLY through feetech_bus, so guard 6
      (assert_safe_torque_enable) applies: Torque_Enable = 128 is the vendor's
      "re-datum the current position to tick 2048" command and would silently
      invalidate the calibration. It can never be emitted from here.
    * Present_Position of all 7 is printed BEFORE and AFTER, so the operator can
      confirm nothing shifted.

WHAT EACH VERDICT DOES AND DOES NOT PROVE
    BACKSTOP HOLDS  - protection engaged, persisted with no commands, and survived
        a 20 ms command stream. Licenses 8.1's inflation decision, R4's floor and
        R7's "harmless stall" reading. It does NOT prove that no damage occurs
        BEFORE the latch, nor that the same holds at Max_Torque 800, nor anything
        about the thermal path.
    BACKSTOP FALSE  - a position command (or nothing at all) cleared it. Names the
        three decisions that must be revisited. This is also a driver finding in
        its own right: the 50 Hz write loop would be actively defeating a hardware
        protection.
        REACHED ONLY ON POSITIVE EVIDENCE that the indication went AWAY. A reading
        we did not get is UNKNOWN, never ABSENT: a dropped CDC-ACM reply, a
        register 48 read that missed, a phase that aborted before it could see
        anything - all of those route to INCONCLUSIVE. "No evidence of protection"
        and "evidence of no protection" are different sentences and only the
        second one is allowed to print this banner.
    INCONCLUSIVE    - protection was never actually entered; or an abort ended the
        run early; or the obstruction gave way, so the stall ended MECHANICALLY
        and neither observable means anything afterwards; or the bus went quiet at
        the one moment that mattered. NOTHING is licensed. A servo that simply
        never overloaded proves nothing about clearing, and this tool refuses to
        dress that up as a result - it is the docs-11 trap ("a zero-error hold
        proves nothing") in a new dress, so it is guarded for explicitly: no
        conclusive banner is reachable unless `latched` is true.

USAGE (bench PC, arm 12 V ON, USB on COM5 - no usbipd attach)
    python tools/edu6_bench/edu6_d1_overload_latch.py --dry-run
    python tools/edu6_bench/edu6_d1_overload_latch.py --watch
    python tools/edu6_bench/edu6_d1_overload_latch.py

ABORT
    Cut the 12 V. That is faster and more certain than any software path. Ctrl-C
    also de-energises immediately (the signal handler drops torque before it
    unwinds anything).
"""

from __future__ import annotations

import argparse
import ast
import atexit
import dataclasses
import math
import os
import signal
import sys
import time
from pathlib import Path

REPO = Path(r"C:\Users\svend\newaarm\Testre")
DRIVER = REPO / "robotis_ai_setup" / "docker" / "open_manipulator" / "edu6_arm_node.py"
sys.path.insert(0, str(DRIVER.parent))

import feetech_bus as fb  # noqa: E402  (the SAME clean-room bus the driver uses)

# The bench consoles here decode print() through a legacy Windows code page, and a
# single non-encodable character raises UnicodeEncodeError and kills the run
# MID-TEST - which for a tool that deliberately stalls a servo would mean losing
# the process that owns the torque-off path. Two sibling bench tools currently
# carry exactly that bug (a bare degree sign). Every literal in THIS file is
# ASCII; this reconfigure additionally makes the German strings that come back
# from fb.describe_error_bits ("Ueberlast", ...) unable to kill the run either.
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:  # noqa: BLE001 - older/odd streams simply keep their behaviour
    pass

# The one text this tool prints that it does NOT author is fb.describe_error_bits,
# which is German by Rule 1 ("Ueberlast", "Ueberhitzung", ...) and therefore not
# ASCII. Folding it for the CONSOLE keeps the whole output inside the ASCII
# guarantee above without touching the driver's own wording, which the student
# still sees verbatim in the Protokoll. The mapping is deliberately the German
# transliteration the repo already uses in legacy files, so a bench log and a
# Protokoll line remain recognisably the same sentence.
#
# Keyed by CODE POINT rather than by literal, so that THIS FILE is ASCII from
# end to end - source included. That is not fussiness: the repo has already been
# bitten by a Windows checkout re-encoding a file (docs 5.3's CRLF trap), and a
# tool whose only job is to be running when a servo is stalled must not be the
# one that fails to import.
_ASCII_FOLD = {
    0xC4: "Ae", 0xD6: "Oe", 0xDC: "Ue",        # A O U with diaeresis
    0xE4: "ae", 0xF6: "oe", 0xFC: "ue",        # a o u with diaeresis
    0xDF: "ss",                                # sharp s
}


def de(text: str) -> str:
    """ASCII rendering of a German string from feetech_bus."""
    return str(text).translate(_ASCII_FOLD).encode("ascii", "replace").decode("ascii")


# -- constants pulled out of the driver source (never retyped) ----------------
def _lit(name):
    """Module-level literal from edu6_arm_node.py, by AST - so this tool cannot
    drift from the driver it is characterising. Parsing a .py file executes
    nothing (no rclpy, no bus)."""
    tree = ast.parse(DRIVER.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return ast.literal_eval(node.value)
    raise SystemExit(f"{name} not found as a module-level literal in {DRIVER}")


SERVO_IDS = _lit("SERVO_IDS")
JOINT_NAMES = _lit("JOINT_NAMES")
JOINT_LIMITS_RAD = _lit("JOINT_LIMITS_RAD")
SIGNS = _lit("_DEFAULT_SIGNS")
CENTER = _lit("CENTER_TICK")
TICKS = _lit("TICKS_PER_REV")
SEED_SPEED = _lit("SEED_SPEED_STEPS")
GOAL_SPEED_CAP = _lit("GOAL_SPEED_CAP_STEPS")
WRITE_ACCEL = _lit("WRITE_ACCELERATION")
LOOP_HZ = _lit("LOOP_HZ")
EDGE_REFUSE_MARGIN = _lit("_DEFAULT_EDGE_REFUSE_MARGIN_TICKS")
GOAL_EDGE_MARGIN = _lit("_DEFAULT_GOAL_EDGE_MARGIN_TICKS")
LOAD_FULLSCALE = _lit("PRESENT_LOAD_FULLSCALE")

RAD_PER_TICK = 2.0 * math.pi / TICKS
DEG_PER_TICK = 360.0 / TICKS
GRIPPER_ID = SERVO_IDS[-1]
HALF_TURN = TICKS // 2

# Present_Current unit from the STS memory table (also used by edu6_goto.py).
CURRENT_LSB_MA = 6.5

# -- thresholds, and why each one is where it is ------------------------------
# TEMPERATURE. A deliberate stall damages a servo THERMALLY (winding insulation),
# not instantaneously. The servo's own Max_Temperature (register 13, factory 70)
# is the point at which IT protects itself - and relying on that is precisely
# what is under test here, so our abort must sit well below it. 15 degC of margin
# is enormous relative to what a 3 s stall at Max_Torque 150 can traverse, and we
# sample at ~20 Hz. The absolute cap exists for the case where register 13 reads
# implausibly (a garbled byte must not raise our own ceiling).
TEMP_MARGIN_BELOW_SERVO_TRIP_C = 15
TEMP_ABORT_ABSOLUTE_C = 55
TEMP_EEPROM_PLAUSIBLE_C = (40, 100)

# CURRENT. This one is deliberately set ABOVE the servo's own trip, which looks
# backwards until you state the experiment: the servo's protection acting IS the
# phenomenon under test, so an abort below its trip would defeat the measurement
# and guarantee an INCONCLUSIVE run. Our threshold is therefore the backstop FOR
# the backstop - it fires exactly in the case we are hunting, where the servo's
# own protection does not act. 15 % covers the 6.5 mA quantization plus one
# sample of lag. The absolute ceiling bounds a rig whose PROTECTION_CURRENT has
# been raised: no single STS servo on this arm has any business drawing 2.5 A.
CURRENT_ABORT_FRAC_OF_TRIP = 1.15
CURRENT_ABORT_ABSOLUTE_MA = 2500.0

# CUMULATIVE HIGH-CURRENT ENERGY - the bound the instantaneous one cannot be.
# The instantaneous threshold above sits ABOVE the servo's own trip on purpose,
# and that is right for the phenomenon. But it leaves a hole with real teeth:
#
#   * when NOTHING latches, phases 3 and 4 are skipped, so exposure is bounded by
#     --stall-s alone. Fine.
#   * when something latches AND the current then falls (the de-rate doing its
#     job), exposure is high-current only briefly. Fine.
#   * when the BIT latches but the current does NOT fall, phases 3 and 4 both
#     run at full stall current and NOTHING stops them. Measured: 30.0 s
#     continuously at 2200 mA on a Max_Torque-800 joint, verdict HOLDS, no abort.
#     On the gripper (Max_Torque 150) that is ~5 W and irrelevant; on an arm joint
#     at 800 it is ~26 W into a stalled winding, and --yes
#     --i-understand-arm-stall makes it UNATTENDED.
#
# The servo's own temperature sensor does not cover this: it is board-mounted and
# lags winding temperature badly, which is exactly why a 30 s stall could complete
# without ever reaching our 55 C abort. So the bound is on ENERGY, integrated
# across ALL powered phases (2 + 3 + 4 together, not per phase - a per-phase
# budget would simply be the phase clock again).
#
# I^2 t rather than plain seconds because heating is I^2 R: 30 s at 2.2 A is four
# times the insult of 30 s at 1.1 A, and a flat time cap cannot say that.
#
# THE NUMBER, and it is derived rather than chosen: the budget is "the tool's
# DEFAULT run, spent entirely at the servo's OWN protection-current trip" -
# HEAT_BUDGET_REF_S seconds (3 + 3 + 3, the default --stall-s/--latch-s/--probe-s)
# times the live Protection_Current register squared. At the factory 310 LSB =
# 2015 mA that is 9.0 x 2.015^2 = 36.5 A^2 s. The consequence is the intended one:
# lengthening the phases buys more TIME but not more ENERGY. The measured 2200 mA
# case exhausts it in 7.6 s instead of running 30; a run whose current falls to
# 800 mA after the de-rate never gets near it (that is 57 s of budget, and it is
# below the floor anyway).
#
# The FLOOR keeps ordinary holding current out of the integral. 0.80 x the same
# live trip = 1612 mA at the factory value: comfortably above any static hold
# (docs 4.5 kept the factory ~2 A trip precisely because it does NOT nuisance-fire
# while the STS3250s hold the arm's weight, so a hold is well under 2015 mA), and
# comfortably below the 2200 mA case this exists to catch.
#
# GRIPPER EXEMPT, by construction rather than by tuning: Max_Torque 150 is ~5 W
# stalled and it carries no arm mass. The default target IS the gripper, so the
# common path is unchanged - this bound only exists for --joint 2..6.
HEAT_BUDGET_REF_S = 9.0            # the DEFAULT phase budget: 3 + 3 + 3 s
HEAT_FLOOR_FRAC_OF_TRIP = 0.80
# One sample's interval is billed at the sample's own current. Cap the interval so
# a scheduling hiccup, or the print-and-decide gap BETWEEN two phases, cannot be
# billed as high-current time. 0.5 s is 10x the default 20 Hz sample period; below
# ~2 Hz sampling this under-counts, which is the safe direction for a false abort
# and the unsafe one for a missed one - do not run this tool at --hz 2.
HEAT_MAX_BILLED_DT_S = 0.5

# TRAVEL. The commanded push is `push_ticks`; materially more than that means the
# obstruction slipped, the goal was mis-computed, or the joint is running away
# (the docs-2.3 non-wrap-aware defect). 40 ticks of slack is the same scale as the
# driver's own EDGE_REFUSE_MARGIN_TICKS. It is PROVISIONAL, because overshoot past
# a pinned goal has never been measured on this arm (open item A10 / R5) - and a
# trip here at SEED_SPEED would itself be the first such measurement, so record it.
TRAVEL_SLACK_TICKS = 40
# WRONG-WAY. Copied from edu6_goto.py: ~2.6 deg against the commanded direction.
WRONG_WAY_TICKS = 30
# BUS. One dropped CDC-ACM reply must not end a run; two consecutive full misses
# means we cannot see the arm, and an arm we cannot read is an arm we must not
# drive (the driver's own stance in _seed_goal_from_present_locked).
READ_MISS_ABORT = 2

# PROTECTION INDICATION. Two independent observables, either of which counts:
#   * an EXPECTED error bit - overload (0x20) or overcurrent (0x08);
#   * Torque_Limit (register 48, RAM) dropping well below its baseline. The
#     vendor mechanism is that on overload the servo de-rates its torque output
#     to Protective_Torque (register 34) percent - which is what the driver's own
#     error-log comment means by "silently dropped to 20 % torque after an
#     overload". Register 48 is a SET value, not a measurement, so there is no
#     read noise; 0.75 is a wide margin against a firmware writing an
#     intermediate value rather than against jitter.
EXPECTED_PROTECTION_BITS = fb.ERR_OVERLOAD | fb.ERR_OVERCURRENT
TORQUE_LIMIT_DROP_FRAC = 0.75

# TRI-STATE, because a boolean cannot tell "the protection is gone" from "we did
# not get a reading". Both of the observables above can go SILENT independently of
# each other - the 62..65 read can miss while the position reply still carries a
# perfectly good error byte, and the register-48 read can miss on its own - and a
# two-valued answer turns every one of those into "no protection indicated", i.e.
# into the headline finding. Each observable is therefore a CHANNEL with its own
# PRESENT / ABSENT / UNKNOWN, and only ABSENT is allowed to argue for FALSE.
PROT_PRESENT = "PRESENT"
PROT_ABSENT = "ABSENT"
PROT_UNKNOWN = "UNKNOWN"
CH_BIT = "error bit"
CH_DERATE = "Torque_Limit de-rate"

# RESUMED DRIVING (phase 4). Compared against the settled latched state at the end
# of phase 3, so all three are DELTAS, not absolutes.
RESUME_MOTION_TICKS = 8            # 0.70 deg; sensor dither is 1-2 ticks
RESUME_LOAD_DELTA = 100            # 10 % of the +/-1000 PWM full scale
RESUME_CURRENT_DELTA_MA = 150.0

# Pre-flight geometry.
PUSH_ROOM_TICKS = 40               # the goal must stay this far inside the window
ARRIVE_TOL_TICKS = 12              # ~1.05 deg - "it got there, so nothing blocked"
ARRIVE_HOLD_S = 0.5
BASELINE_DRIFT_TICKS = 3           # hands-off stationary gate before energising

# MECHANICAL END OF THE STALL (phases 3 and 4). The operator is told to use a
# COMPLIANT, expendable object - foam, folded cloth - which is precisely the thing
# that squashes and slips. When it gives way the joint completes its commanded
# travel, and from that instant BOTH observables are worthless: the servo is no
# longer overloaded, so of course the indication goes; and it is free to move, so
# of course `resumed_driving` fires. Measured: a 16-tick (1.4 deg, ~0.35 mm of jaw
# travel) slip was enough to trip resumed_driving and print BACKSTOP FALSE.
#
# DELIBERATELY THE SAME NUMBER AS ARRIVE_TOL_TICKS, not a second one: phase 2
# already answers "did the joint get where it was told to go, so nothing was
# blocking it" with that tolerance, and this is the identical physical question
# one phase later. A different constant for the same question is how two answers
# start disagreeing. It is derived, not copied, so it cannot drift.
#
# It does NOT destroy the FALSE verdict, which is the thing to check before
# adding a gate like this. Against a PROPER obstruction a defeated protection does
# not reach the goal - it pushes harder and stays put - and `resumed_driving`
# reports that through the |load| and current deltas with no position change at
# all. What this gate refuses is the run where the obstruction failed, which is
# already how phase 2 treats an arrival ("nothing blocked it").
PROBE_ARRIVE_TOL_TICKS = ARRIVE_TOL_TICKS

# Ceilings enforced in code (the operator can lower, never raise past these).
STALL_S_MAX = 10.0                 # > 3x the factory Protection_Time (~2 s), so a
#                                    run can never be "too short for the mechanism";
#                                    the temperature abort backstops the upper end.
LATCH_S_MAX = 10.0
PROBE_S_MAX = 10.0
PUSH_TICKS_MAX = 200               # 17.6 deg
MAX_POWERED_S_MAX = 45.0

_OFF_ALL_PAYLOAD = {sid: b"\x00" for sid in SERVO_IDS}

# Prompt texts live here so --dry-run can print them VERBATIM rather than a
# paraphrase - the point of the dry run is that the operator reads the real thing.
PROMPT_GO = "        type 'go' to energise: "
PROMPT_ARM = ("        type 'ARM STALL' to confirm you mean to stall a LOAD-BEARING "
              "joint: ")
PROMPT_CYCLE = "        type 'cycle' to run phase 5 (or Enter to skip): "

# Write allowlist: (addr, max_end_exclusive). Register 40 is Torque_Enable (1
# byte); 41..47 is the accel/goal/Goal_Time/speed block the driver writes. Nothing
# else is reachable - in particular every EEPROM address (0..39) is refused, so
# Max_Torque, PROTECTION_CURRENT, the position limits and Homing_Offset cannot be
# touched even by a coding slip.
_WRITE_ALLOWED = {
    fb.REG_TORQUE_ENABLE: fb.REG_TORQUE_ENABLE + 1,
    fb.REG_ACCELERATION: fb.REG_ACCELERATION + 7,
}


class BenchBus(fb.FeetechBus):
    """FeetechBus restricted to the two RAM spans this experiment needs.

    Structural, not procedural: a write outside the allowlist raises before any
    byte is framed, so "this tool never writes EEPROM" is a property of the type
    rather than a promise in a comment. Both methods still go through
    fb.write / fb.sync_write, so guard 6 (assert_safe_torque_enable) applies to
    register 40 unchanged."""

    @staticmethod
    def _check(addr: int, data: bytes) -> None:
        end = _WRITE_ALLOWED.get(addr)
        if end is None or addr + len(data) > end:
            raise RuntimeError(
                f"BENCH WRITE REFUSED: addr {addr} len {len(data)} is outside the "
                f"allowlist {sorted(_WRITE_ALLOWED)} (EEPROM is 0..39 and is "
                f"unreachable from this tool by design)")
        if addr == fb.REG_TORQUE_ENABLE and (len(data) != 1 or data[0] not in (0, 1)):
            raise RuntimeError(
                f"BENCH WRITE REFUSED: register {fb.REG_TORQUE_ENABLE} takes only "
                f"a single 0 or 1 byte here (got {list(data)}); "
                f"{fb.TORQUE_ENABLE_CALIBRATE_MIDPOINT} re-datums the joint")

    def write(self, servo_id, addr, data, await_status=True):  # noqa: D102
        self._check(addr, data)
        return super().write(servo_id, addr, data, await_status)

    def sync_write(self, addr, per_servo):  # noqa: D102
        for _sid, data in per_servo.items():
            self._check(addr, data)
        return super().sync_write(addr, per_servo)


# -- sampling -----------------------------------------------------------------
@dataclasses.dataclass
class Sample:
    t: float
    tick: int | None = None
    speed: int | None = None
    load: int | None = None
    err: int = 0                    # error byte of the position reply
    status: int | None = None       # REG_STATUS (65) read explicitly
    current_ma: float | None = None
    temp_c: int | None = None
    volt_v: float | None = None
    torque_limit: int | None = None

    def err_any(self) -> int:
        """Union of the reply error byte and the explicitly-read status byte.
        They should agree; taking the union means a disagreement can only make us
        MORE cautious, never less."""
        return int(self.err) | int(self.status or 0)

    def ok(self) -> bool:
        return self.tick is not None


def sample_servo(bus, sid: int, t0: float) -> Sample:
    """One full observation of one servo: four documented round trips.

    Deliberately four separate reads rather than one 18-byte block spanning
    48..65: that span crosses registers 50-54, which the current official vendor
    table lists as undefined. The driver rejected reading them for the same
    reason, and at 20 Hz on one servo the extra round trips cost nothing."""
    s = Sample(t=time.monotonic() - t0)
    try:
        err, d = bus.read(sid, fb.REG_PRESENT_POSITION, 6)
        s.err = err
        s.tick = fb.decode_sign_magnitude(fb.from_le16(d[0], d[1]), 15)
        s.speed = fb.decode_sign_magnitude(fb.from_le16(d[2], d[3]), 15)
        # Present_Load's sign is bit 10, NOT bit 15 (per-register convention).
        s.load = fb.decode_sign_magnitude(fb.from_le16(d[4], d[5]), 10)
    except fb.FeetechBusError:
        return s
    try:
        # 62 voltage / 63 temperature / 64 (unused) / 65 status - contiguous.
        _e, d = bus.read(sid, fb.REG_PRESENT_VOLTAGE, 4)
        s.volt_v = d[0] * 0.1
        s.temp_c = d[1]
        s.status = d[3]
    except fb.FeetechBusError:
        pass
    try:
        raw = bus.read_u16(sid, fb.REG_PRESENT_CURRENT)
        s.current_ma = abs(fb.decode_sign_magnitude(raw, 15)) * CURRENT_LSB_MA
    except fb.FeetechBusError:
        pass
    try:
        s.torque_limit = bus.read_u16(sid, fb.REG_TORQUE_LIMIT)
    except fb.FeetechBusError:
        pass
    return s


# -- pure logic (unit-exercisable without a bus) ------------------------------
@dataclasses.dataclass
class Thresholds:
    temp_abort_c: int
    current_abort_ma: float
    travel_abort_ticks: int
    wrong_way_ticks: int
    read_miss_abort: int
    torque_limit_drop_frac: float
    powered_deadline: float


@dataclasses.dataclass
class HeatAccumulator:
    """Cumulative I^2 t across ALL powered phases - see HEAT_BUDGET_REF_S.

    Deliberately ONE object for the whole run, constructed before phase 2 and
    handed to every powered_window: the hole this closes is the two phases that
    only exist BECAUSE something latched, so a per-phase accumulator would reset
    at exactly the wrong moment.

    `budget_a2s is None` disables it entirely - that is the gripper, whose stall
    is ~5 W - and then this object is a pure recorder, which is worth having
    anyway because the peak and the integral are R4 data."""

    floor_ma: float
    budget_a2s: float | None
    a2s: float = 0.0
    last_t: float | None = None
    peak_ma: float = 0.0
    seconds_above_floor: float = 0.0

    def observe(self, s: Sample, now: float) -> None:
        # last_t advances on EVERY sample, including unreadable ones and ones
        # below the floor. Otherwise a quiet stretch would be billed in full at
        # whatever current happened to come back afterwards.
        prev, self.last_t = self.last_t, now
        if s.current_ma is None:
            return
        self.peak_ma = max(self.peak_ma, s.current_ma)
        if prev is None or s.current_ma < self.floor_ma:
            return
        dt = min(now - prev, HEAT_MAX_BILLED_DT_S)
        if dt <= 0.0:
            return
        amps = s.current_ma / 1000.0
        self.a2s += amps * amps * dt
        self.seconds_above_floor += dt

    def exceeded(self) -> bool:
        return self.budget_a2s is not None and self.a2s >= self.budget_a2s


@dataclasses.dataclass
class PushCtx:
    sid: int
    label: str
    start_tick: int
    goal_tick: int
    push_ticks: int
    dir_tick: int
    baseline_torque_limit: int | None


def unexpected_error_bits(err: int) -> int:
    """The set bits we did NOT come here to see. Overload/overcurrent are the
    phenomenon; voltage, angle-sensor, overheat and every unnamed bit mean
    something we did not intend is happening, and continuing a deliberate stall
    through one of those is how a bench test breaks hardware."""
    return int(err) & ~EXPECTED_PROTECTION_BITS


def protection_evidence(s: Sample, ctx: PushCtx,
                        drop_frac: float) -> tuple[dict[str, str], list[str]]:
    """({channel: PRESENT|ABSENT|UNKNOWN}, reasons) for ONE sample.

    THE POINT OF THE TRI-STATE. Both observables can fall silent on their own,
    and a two-valued answer reads every silence as "no protection":

      * CH_BIT is UNKNOWN only when the POSITION read itself failed. When it
        succeeded, the reply's own error byte is a genuine reading of the servo's
        error state, so err == 0 there is real evidence of ABSENCE - and, just as
        importantly, err == 0x20 there is real evidence of PRESENCE even if the
        separate 62..65 read (which is where `status` comes from) missed. That
        second case is not hypothetical: it is the sample where the overload bit
        is sitting in plain sight in `s.err` and the old code answered "cleared".
      * CH_DERATE exists only if we have a baseline to compare against. Given
        one, a missing register-48 reply is UNKNOWN - and so is a reported ZERO,
        which is NOT a 100 % de-rate but a register we cannot believe (a
        de-energised servo reads 0 there, which is why the baseline itself is
        `or None`-guarded; a checksum-valid 0 mid-run would otherwise read as
        "fell 150 -> 0", i.e. as the strongest possible protection indication,
        and the next healthy read as "cleared").

    `reasons` lists ONLY the PRESENT channels, in the wording the report uses."""
    states: dict[str, str] = {}
    reasons: list[str] = []

    if not s.ok():
        states[CH_BIT] = PROT_UNKNOWN
    else:
        bits = s.err_any() & EXPECTED_PROTECTION_BITS
        if bits:
            states[CH_BIT] = PROT_PRESENT
            reasons.append(
                f"error bit {bits:#04x} ({de(fb.describe_error_bits(bits))})")
        else:
            states[CH_BIT] = PROT_ABSENT

    base = ctx.baseline_torque_limit
    if base:
        # `not s.ok()` is redundant TODAY - sample_servo returns the moment the
        # position read raises, so torque_limit is None on any such sample. It is
        # written anyway so that "a sample we could not read is UNKNOWN on EVERY
        # channel" is true of THIS function rather than true because of what
        # another function happens to do. Without it the invariant only holds by
        # coincidence, and an exhaustive enumeration over Sample fields finds the
        # hole immediately.
        if not s.ok() or s.torque_limit is None or s.torque_limit <= 0:
            states[CH_DERATE] = PROT_UNKNOWN
        elif s.torque_limit < base * drop_frac:
            states[CH_DERATE] = PROT_PRESENT
            reasons.append(f"Torque_Limit fell {base} -> {s.torque_limit} "
                           f"({100.0 * s.torque_limit / base:.0f} % of baseline)")
        else:
            states[CH_DERATE] = PROT_ABSENT
    return states, reasons


def protection_reasons(s: Sample, ctx: PushCtx, drop_frac: float) -> list[str]:
    """Why this sample counts as 'protection is indicated'. Empty list = it does
    not SAY so - which is NOT the same as saying it is gone; ask
    protection_state() for that. Two independent observables - see
    TORQUE_LIMIT_DROP_FRAC."""
    return protection_evidence(s, ctx, drop_frac)[1]


def protection_state(s: Sample, ctx: PushCtx, drop_frac: float,
                     channels: set[str] | None = None) -> str:
    """PRESENT / ABSENT / UNKNOWN for one sample. PRESENT > UNKNOWN > ABSENT.

    `channels` narrows WHOSE SILENCE COUNTS - never whose evidence counts. That
    asymmetry is the whole design and it is easy to get wrong (this function did,
    for one revision):

      * A PRESENT reading on ANY channel settles the sample, restriction or not.
        Positive evidence of protection is positive evidence whichever observable
        produced it. Restricting THIS was a real regression: a bit-only latch
        whose de-rate channel starts indicating during the command stream - bit
        goes to 0, Torque_Limit reads 30 of 150 in the same sample - is a servo
        still de-rated, and the narrowed form called it BACKSTOP FALSE.
      * Short of that, one WATCHED channel we could not read makes "it is gone"
        unprovable. Watched = the channels that ever actually indicated on this
        run (see indicated_channels). Restricting THIS is what keeps the
        conservatism from eating a real finding: on a run where only the error
        bit ever fired, a missing register-48 reply says nothing about a channel
        that never had anything to say, and the bit going to 0 stays genuine
        evidence of clearing.

    None = every channel is watched."""
    states = protection_evidence(s, ctx, drop_frac)[0]
    if PROT_PRESENT in states.values():
        return PROT_PRESENT
    watched = [st for ch, st in states.items()
               if channels is None or ch in channels]
    if not watched or PROT_UNKNOWN in watched:
        return PROT_UNKNOWN
    return PROT_ABSENT


def indicated_channels(samples: list[Sample], ctx: PushCtx,
                       drop_frac: float) -> set[str]:
    """Every channel that was ever POSITIVELY observed to indicate protection."""
    out: set[str] = set()
    for s in samples:
        for ch, st in protection_evidence(s, ctx, drop_frac)[0].items():
            if st == PROT_PRESENT:
                out.add(ch)
    return out


def abort_code(s: Sample, th: Thresholds, ctx: PushCtx, misses: int,
               now: float, heat: HeatAccumulator | None = None) -> str | None:
    """Return a short CODE, never a formatted message.

    That split is the point: the caller must be able to de-energise BEFORE any
    string is built, so nothing between the verdict and the torque-off write can
    allocate, format or log. The human sentence is composed afterwards, from the
    same sample, by abort_message()."""
    if now > th.powered_deadline:
        return "DEADLINE"
    # DEADLINE and HEAT are the two bounds that do not depend on THIS sample, so
    # they are asked first - in particular BEFORE the `s.ok()` early return
    # below, which would otherwise let a run of unreadable samples suspend the
    # energy bound for as long as the bus stayed quiet.
    if heat is not None and heat.exceeded():
        return "HEAT"
    if not s.ok():
        return "READ" if misses + 1 >= th.read_miss_abort else None
    if unexpected_error_bits(s.err_any()):
        return "ERRBIT"
    if s.temp_c is not None and s.temp_c >= th.temp_abort_c:
        return "TEMP"
    if s.current_ma is not None and s.current_ma >= th.current_abort_ma:
        return "CURRENT"
    travel = (s.tick - ctx.start_tick) * ctx.dir_tick
    if travel > ctx.push_ticks + th.travel_abort_ticks:
        return "TRAVEL"
    if travel < -th.wrong_way_ticks:
        return "WRONGWAY"
    return None


def abort_message(code: str, s: Sample, th: Thresholds, ctx: PushCtx,
                  heat: HeatAccumulator | None = None) -> str:
    """Human sentence for an abort that has ALREADY happened.

    Total-function on purpose: it is called at the worst possible moment, right
    after a de-energise, and a formatting crash there would replace the
    diagnosis with a traceback. Every branch tolerates a sample whose read
    failed."""
    travel = None if not s.ok() else (s.tick - ctx.start_tick) * ctx.dir_tick
    if code == "DEADLINE":
        return ("the global powered deadline expired - torque was dropped by the "
                "clock, not by a fault")
    if code == "HEAT":
        if heat is None or heat.budget_a2s is None:
            return ("the cumulative high-current energy budget was exhausted - "
                    "torque was dropped to bound the winding temperature")
        return (f"cumulative high-current energy reached the budget: "
                f"{heat.a2s:.1f} of {heat.budget_a2s:.1f} A^2 s "
                f"({heat.seconds_above_floor:.1f} s at or above "
                f"{heat.floor_ma:.0f} mA, peak {heat.peak_ma:.0f} mA). The servo's "
                f"board-mounted temperature sensor lags the winding, so this - not "
                f"the 55 C abort - is what bounds a stall the de-rate did not cool. "
                f"Treat the run as INCONCLUSIVE, let the joint cool, and shorten "
                f"the phases rather than raising this")
    if code == "READ":
        return (f"{th.read_miss_abort} consecutive full read failures on servo "
                f"{ctx.sid}. An arm we cannot read is an arm we must not drive")
    # DEADLINE, HEAT and READ are the only three codes abort_code can raise on a
    # sample whose read failed; everything below assumes a readable one, so
    # anything else arriving here with no position is reported honestly rather
    # than formatted into a crash.
    if travel is None:
        return (f"abort '{code}' with no readable position - torque is off; "
                f"treat the run as INCONCLUSIVE")
    if code == "ERRBIT":
        bits = unexpected_error_bits(s.err_any())
        return (f"UNEXPECTED error bit {bits:#04x} "
                f"({de(fb.describe_error_bits(bits))}) - not the overload/overcurrent "
                f"pair this test came to see")
    if code == "TEMP":
        return (f"temperature {s.temp_c} C reached the abort threshold "
                f"{th.temp_abort_c} C")
    if code == "CURRENT":
        return (f"current {s.current_ma:.0f} mA reached the abort threshold "
                f"{th.current_abort_ma:.0f} mA - the servo's OWN protection did "
                f"not act first, which is itself the finding")
    if code == "TRAVEL":
        return (f"travelled {travel} ticks ({travel * DEG_PER_TICK:.1f} deg), past "
                f"the commanded push of {ctx.push_ticks} + "
                f"{th.travel_abort_ticks} slack. The obstruction slipped, or this "
                f"is overshoot - record the number, it is an R5/A10 datum")
    if code == "WRONGWAY":
        return (f"moved {-travel} ticks ({-travel * DEG_PER_TICK:.1f} deg) the "
                f"WRONG way - the docs-2.3 non-wrap-aware failure")
    return f"unknown abort code {code!r}"


def last_valid(samples: list[Sample]) -> Sample | None:
    """The newest sample we can actually reason about: the newest READABLE one.

    It used to additionally require a status byte, to stop a dropped CDC-ACM
    reply from reading as 'the protection cleared'. That was the right worry and
    the wrong mechanism, twice over. It DISCARDED REAL EVIDENCE - `s.err` on a
    successful position read is the servo's own error byte, so a sample with
    err = 0x20 and no status byte is the overload bit in plain sight - and when
    it discarded everything it returned None, which the caller then turned into
    'the indication went away'. Absence of evidence became the headline finding.

    The worry now lives where it belongs: protection_state() answers UNKNOWN for
    a channel it could not read, and only ABSENT is allowed to argue for FALSE.
    So this function can go back to meaning what its name says."""
    for s in reversed(samples):
        if s.ok():
            return s
    return None


def stall_ended_mechanically(samples: list[Sample], ctx: PushCtx,
                             tol: int) -> Sample | None:
    """The first sample at which the joint had reached its commanded goal - i.e.
    the obstruction gave way and the stall is over. None = it never did.

    Everything observed after this point is uninterpretable, which is why it
    gates the verdict rather than merely annotating it: an unloaded servo drops
    its indication because there is nothing to protect against, and a free joint
    moves because it was told to. See PROBE_ARRIVE_TOL_TICKS.

    THE GUARD ON THE GUARD: if the goal is already within `tol` of the START,
    'at the goal' is true before anything has moved, and the test would fire on
    every run with a small --push-ticks and call a joint that never budged a
    mechanical failure. That is the docs-11 'do not cite a definitional zero as
    evidence' trap, so the test disables itself there instead."""
    if abs(ctx.goal_tick - ctx.start_tick) <= tol:
        return None
    for s in samples:
        if s.ok() and abs(s.tick - ctx.goal_tick) <= tol:
            return s
    return None


def resumed_driving(s: Sample, settled: Sample | None) -> list[str]:
    """Did the servo start DRIVING again during the command stream? Reported
    separately from 'did the error bit clear', because they can disagree - a bit
    that clears while the servo stays passive is not a defeated protection, and a
    servo that pushes again while the bit stays set is one, bit or no bit."""
    if settled is None or not s.ok() or not settled.ok():
        return []
    out: list[str] = []
    if abs(s.tick - settled.tick) > RESUME_MOTION_TICKS:
        out.append(f"moved {abs(s.tick - settled.tick)} ticks after being settled")
    if (s.load is not None and settled.load is not None
            and abs(s.load) > abs(settled.load) + RESUME_LOAD_DELTA):
        out.append(f"|load| rose {abs(settled.load)} -> {abs(s.load)}")
    if (s.current_ma is not None and settled.current_ma is not None
            and s.current_ma > settled.current_ma + RESUME_CURRENT_DELTA_MA):
        out.append(f"current rose {settled.current_ma:.0f} -> "
                   f"{s.current_ma:.0f} mA")
    return out


def choose_verdict(st: dict) -> tuple[str, list[str]]:
    """(code, lines). code is one of HOLDS / FALSE / INCONCLUSIVE.

    ORDER IS LOAD-BEARING, and the ONE invariant it exists to enforce is:

        NO ABSENCE OF EVIDENCE MAY PRODUCE 'FALSE'.

    Only positive evidence that the protection went AWAY may print the banner
    that re-opens three safety decisions. Every gate below FALSE-ward of a
    missing reading therefore has to be passed first.

    `latched` is checked FIRST and nothing else can override it: a servo that
    never entered protection proves nothing whatsoever about clearing it, and
    dressing that up as a conclusive result is exactly the docs-11 trap ("a
    zero-error hold proves nothing") wearing new clothes.

    The MECHANICAL gate is second. If the obstruction gave way the joint reached
    its goal, and from that point neither observable means anything: an unloaded
    servo drops its indication because there is nothing to protect against, and
    a free joint moves because it was told to. That is the same reading phase 2
    already gives an arrival ("nothing blocked it"), one phase later.

    The EVIDENCE gate is third, and it is the one that used to be missing. The
    self-clearing FALSE below reads `held_after_no_commands`, which is now
    TRI-STATE: True (positively still there), False (positively gone) and None
    (we could not read it). None must reach INCONCLUSIVE, never FALSE - a phase
    that aborted on its first unreadable sample, or whose de-rate channel went
    quiet at the end, has answered nothing.

    Only THEN the self-clearing case, which is conclusive on its own evidence -
    a protection that lets go with no command at all has already failed the
    premise, and phase 4 cannot rescue it.

    Then the completeness gate: `phase4_complete` is required for any conclusive
    banner, so an aborted OR SKIPPED phase 4 - or one whose every sample was
    UNKNOWN - can never fall through to HOLDS on default-initialised state."""
    if not st["latched"]:
        lines = [
            "Protection was NEVER observed to engage, so this run cannot say "
            "anything at all about whether a position command clears it.",
            "NOTHING is licensed. 8.1's zone inflation, R4's pinch floor and R7's "
            "stall-vs-damage question all keep their premise UNVERIFIED.",
            "",
            "Why there is no result, and what to change:",
        ]
        if st["arrived_without_stall"]:
            lines.append("  * the joint REACHED its goal - nothing was blocking it. "
                         "Close the jaws onto the object BY HAND first (they are "
                         "limp and backdrive), then re-run.")
        if st["aborted"]:
            lines.append(f"  * the run aborted: {st['aborted']}")
        lines += [
            f"  * raise --stall-s (now {st['stall_s']:.1f} s, ceiling "
            f"{STALL_S_MAX:.0f} s). Protection_Time reads {st['protection_time']} "
            f"raw; if the vendor's common 10 ms/LSB holds that is "
            f"{st['protection_time'] / 100.0:.2f} s of SUSTAINED overload before "
            f"the servo acts, so the stall must comfortably outlast it.",
            f"  * raise --push-ticks (now {st['push_ticks']}), or use a FIRMER, "
            f"less compressible object - foam that simply squashes never stalls.",
            f"  * reachability: this servo's Max_Torque_Limit is "
            f"{st['max_torque_limit']} (~{st['max_torque_limit'] / 10.0:.0f} % PWM) "
            f"and Overload_Torque is {st['overload_torque']}. If the first cannot "
            f"reach the second, the OVERLOAD path may be unreachable on this joint "
            f"by construction - escalate to an arm joint (--joint, Max_Torque 800) "
            f"with all the extra care that implies.",
        ]
        return "INCONCLUSIVE", lines

    if st["stall_ended_mechanically"]:
        return "INCONCLUSIVE", [
            f"Protection DID engage ({'; '.join(st['latch_reasons'])}), but the "
            f"STALL ENDED MECHANICALLY before the question was answered:",
            f"  {st['stall_ended_mechanically']}",
            "",
            "The joint reached the goal it was commanded to, which means the "
            "obstruction gave way - it squashed, slipped or was pushed clear. "
            "From that instant neither observable can be read: an unloaded servo "
            "drops its indication because there is nothing left to protect "
            "against, and a joint with nothing in its way moves because it was "
            "told to. A 16-tick slip (1.4 deg, ~0.35 mm of jaw travel) is enough.",
            "NOTHING is licensed - and in particular this is NOT evidence that a "
            "command defeated the protection.",
            "",
            "What to change: use a FIRMER, less compressible object - a hardwood "
            "block, a metal standoff, something that does not compress. Foam and "
            "folded cloth are compliant by design, which is what makes them safe "
            "to squeeze and useless to stall against for 3 s. Re-seat it so the "
            "jaws are already loaded at the START of the run (--watch), and keep "
            "--push-ticks small: the push only has to be enough to demand motion "
            "the object refuses.",
        ]

    if st["held_after_no_commands"] is None:
        why = ("the no-command phase never ran"
               if not st["phase3_ran"] else
               "the no-command phase ran, but at its end EVERY protection channel "
               "that had ever indicated was UNREADABLE - a dropped position reply, "
               "or a register-48 read that missed, or a register-48 read that came "
               "back 0 (which is a register we cannot believe, not a 100 % de-rate)")
        return "INCONCLUSIVE", [
            f"Protection DID engage ({'; '.join(st['latch_reasons'])}), but "
            f"whether it PERSISTED with no commands could not be established:",
            f"  {why}.",
            f"  {st['aborted'] or 'no abort was recorded'}",
            "",
            "This is deliberately NOT reported as 'the protection cleared'. A "
            "reading we did not get is UNKNOWN, not ABSENT, and only positive "
            "evidence that protection went away may print BACKSTOP FALSE - that "
            "banner re-opens three safety decisions.",
            "NOTHING is licensed. Re-run; inducing the state is the hard part and "
            "that half is now known to work. If the bus is dropping replies, that "
            "is worth chasing on its own (docs 2: usbipd/CDC-ACM framing).",
        ]

    if st["held_after_no_commands"] is False:
        return "FALSE", _false_lines(
            st,
            "It cleared ON ITS OWN, with NO command sent at all - the protection "
            "does not latch, it is transient and self-clearing. That is weaker "
            "than the premise needs even before the write loop is considered.")

    if not st["phase4_complete"]:
        return "INCONCLUSIVE", [
            f"Protection DID engage ({'; '.join(st['latch_reasons'])}), but the "
            f"command-stream phase never completed, so the clearing question is "
            f"still open:",
            f"  {st['aborted'] or 'phases 3 and 4 were skipped'}",
            "NOTHING is licensed. Fix the cause and re-run; inducing the state is "
            "the hard part and that half is now known to work.",
        ]

    clear_reasons = []
    if st["cleared_during_commands"]:
        clear_reasons.append("the protection indication went away")
    if st["driving_resumed"]:
        clear_reasons.append(f"the servo started driving again "
                             f"({'; '.join(st['driving_resumed'])})")
    if clear_reasons:
        return "FALSE", _false_lines(
            st, "Under a 20 ms position-command stream - byte-for-byte what "
                "_write_targets sends at 50 Hz - " + " and ".join(clear_reasons) + ".")

    return "HOLDS", [
        f"Protection engaged ({'; '.join(st['latch_reasons'])}), PERSISTED with no "
        f"commands for {st['latch_s']:.1f} s, and SURVIVED a "
        f"{st['probe_s']:.1f} s position-command stream at {LOOP_HZ:.0f} Hz.",
        "",
        "This LICENSES the premise the three decisions rest on:",
        "  * docs 8.1 - the accepted ~34 mm gripper-housing under-coverage keeps "
        "its physical backstop: a housing that clips an obstacle drives into a "
        "stall the servo itself terminates. The 0.05 m inflation may stand.",
        "  * R4 - Max_Torque 150 bounds a TERMINATED push, so the overcurrent "
        "floor can be finalised from measured stall current with the protection "
        "as the last line.",
        "  * R7 - a self-collision is a terminated stall, so the deferred jog "
        "swept-refusal guard (A7) stays a nice-to-have rather than a requirement.",
        "",
        "What it does NOT prove: that no damage occurs BEFORE the latch; that the "
        "same holds at Max_Torque 800 on an arm joint; anything about the thermal "
        "path (this run aborts far below the servo's own overheat trip); or that "
        "it holds for a different firmware or a longer stall.",
    ]


def _false_lines(st: dict, headline: str) -> list[str]:
    return [
        headline,
        "",
        f"THE BACKSTOP PREMISE IS FALSE on this arm. The driver's write loop "
        f"sends a fresh Goal_Position to all 7 servos every "
        f"{1000.0 / LOOP_HZ:.0f} ms for as long as a trajectory is active, so the "
        f"protection can never stay engaged while the environment is up.",
        "",
        "Three decisions must be revisited, and none of them is a code tweak:",
        "  * docs 8.1 - the no-go-zone inflation. The ~34 mm gripper-housing "
        "under-coverage was ACCEPTED because the current limits back it up. That "
        "reason is gone; re-open it (Rule 2 - ask Sven).",
        "  * R4 - the pinch-force floor. Max_Torque 150 becomes a CONTINUOUS "
        "force, not a transient one, so the floor has to be set for indefinite "
        "application against a student's hand, not for a squeeze that ends.",
        "  * R7 - self-collision. It becomes a permanent powered stall rather than "
        "a stall that terminates, so the deferred jog swept-refusal guard (A7) "
        "likely becomes REQUIRED, and 9.2's 39.7 mm centreline pair matters.",
        "",
        "Fourth, and it is a finding in its own right: our own 50 Hz write loop "
        "would be actively defeating a hardware protection. Changing that is a "
        "Rule 2 change to the command path - do not patch _write_targets on the "
        "strength of one bench run; write it up and ask.",
        "",
        f"Evidence: protection first indicated at t = {st['latch_t']:.2f} s "
        f"({'; '.join(st['latch_reasons'])}).",
    ]


# -- reporting helpers --------------------------------------------------------
def deg_of(sid: int, tick: int) -> float:
    return (tick - CENTER) * DEG_PER_TICK * SIGNS[SERVO_IDS.index(sid)]


def name_of(sid: int) -> str:
    return JOINT_NAMES[SERVO_IDS.index(sid)]


def fmt_sample(s: Sample) -> str:
    if not s.ok():
        return f"   t={s.t:5.2f}  NO REPLY"
    err = s.err_any()
    return (f"   t={s.t:5.2f}  tick {s.tick:>4}  spd {s.speed:>+5}  "
            f"load {s.load:>+5}  "
            f"{'  cur    ?' if s.current_ma is None else f'  cur {s.current_ma:5.0f}mA'}"
            f"  {'temp  ?' if s.temp_c is None else f'temp {s.temp_c:3d}C'}"
            f"  {'tqlim    ?' if s.torque_limit is None else f'tqlim {s.torque_limit:4d}'}"
            + (f"  ERR {err:#04x} {de(fb.describe_error_bits(err))}" if err else ""))


def _stdin_is_interactive() -> bool:
    try:
        return bool(sys.stdin is not None and sys.stdin.isatty())
    except Exception:  # noqa: BLE001 - an odd stream is not an interactive one
        return False


def confirm(prompt_text: str, expected: str, auto: bool) -> bool:
    """Ask, or decide without asking. Never block on a stream nobody is typing at.

    The non-interactive contract of this tool is the FLAGS - --yes and
    --i-understand-arm-stall, which the help text already calls "the
    non-interactive equivalent of typing 'ARM STALL'". So a non-tty stdin is a
    'no', not a hang: EOFError only fires on a stream that has actually ended,
    and a pipe or a service harness that simply never sends a line would have
    blocked here forever - with this process owning the torque-off path."""
    if auto:
        print(f"{prompt_text}[auto-confirmed: {expected!r}]")
        return True
    if not _stdin_is_interactive():
        print(f"{prompt_text}[no interactive tty - treated as 'no'. The "
              f"non-interactive path is the flags, not a piped answer.]")
        return False
    try:
        return input(prompt_text).strip().lower() == expected.lower()
    except EOFError:
        print("[ABORT] no tty for confirmation - nothing was written.")
        return False


# -- the powered sampling window (shared by phases 2, 3, 4) -------------------
def powered_window(bus, ctx: PushCtx, th: Thresholds, label: str, seconds: float,
                   hz: float, kill, write_payload: bytes | None,
                   stop_on_arrival: bool,
                   heat: HeatAccumulator | None = None
                   ) -> tuple[list[Sample], str | None, bool]:
    """Sample servo ctx.sid for `seconds`, aborting (kill FIRST, then report) on
    any threshold. When `write_payload` is given it is SYNC_WRITEd to
    REG_ACCELERATION every 1/LOOP_HZ seconds - exactly the shape and cadence of
    _write_targets. Returns (samples, abort_code, arrived).

    `heat` is the ONE cumulative energy accumulator for the whole run; it is fed
    here and asked by abort_code. Feeding it is pure arithmetic on a sample that
    has already been read, so it does not sit between an abort verdict and the
    torque-off write - kill() remains the first bus operation after a verdict."""
    t0 = time.monotonic()
    end = t0 + seconds
    sample_period = 1.0 / max(hz, 1.0)
    write_period = 1.0 / LOOP_HZ
    next_sample = t0
    next_write = t0
    samples: list[Sample] = []
    misses = 0
    arrived_since: float | None = None
    print(f"[{label}] {seconds:.1f} s"
          + (f", commanding every {1000.0 * write_period:.0f} ms"
             if write_payload else
             ", no further writes during this window - reading only"))
    while True:
        now = time.monotonic()
        if now >= end:
            return samples, None, False
        if write_payload is not None and now >= next_write:
            next_write += write_period
            try:
                bus.sync_write(fb.REG_ACCELERATION, {ctx.sid: write_payload})
            except fb.FeetechBusError as e:
                print(f"   [WARN] command write failed: {e}")
        if now >= next_sample:
            next_sample += sample_period
            s = sample_servo(bus, ctx.sid, t0)
            now_mono = time.monotonic()
            if heat is not None:
                heat.observe(s, now_mono)
            code = abort_code(s, th, ctx, misses, now_mono, heat)
            if code:
                kill()                      # -- nothing before this line --
                samples.append(s)
                print(fmt_sample(s))
                print(f"   [ABORT] {abort_message(code, s, th, ctx, heat)}")
                return samples, code, False
            misses = 0 if s.ok() else misses + 1
            samples.append(s)
            print(fmt_sample(s))
            if stop_on_arrival and s.ok():
                if abs(s.tick - ctx.goal_tick) <= ARRIVE_TOL_TICKS:
                    if arrived_since is None:
                        arrived_since = now
                    if now - arrived_since >= ARRIVE_HOLD_S:
                        print(f"   [ARRIVED] within {ARRIVE_TOL_TICKS} ticks of the "
                              f"goal and holding - nothing blocked the joint.")
                        return samples, None, True
                else:
                    arrived_since = None
        time.sleep(0.002)


# -- dry run ------------------------------------------------------------------
def dry_run(args) -> int:
    sid = args.joint
    idx = SERVO_IDS.index(sid)
    lo_rad, hi_rad = JOINT_LIMITS_RAD[idx]
    win = fb.position_limit_window(lo_rad, hi_rad, SIGNS[idx])
    dir_tick = 1 if _tick(lo_rad, SIGNS[idx]) > _tick(hi_rad, SIGNS[idx]) else -1
    if args.direction in ("+", "-"):
        dir_tick = 1 if args.direction == "+" else -1

    print("=" * 78)
    print("D1 DRY RUN - nothing is opened, nothing is written, no serial port is")
    print("             touched. This is the whole procedure, in order.")
    print("=" * 78)
    print()
    print("CONSTANTS (AST-extracted from the driver, never retyped)")
    print(f"  driver source        {DRIVER}")
    print(f"  SERVO_IDS            {list(SERVO_IDS)}")
    print(f"  JOINT_NAMES          {list(JOINT_NAMES)}")
    print(f"  JOINT_SIGNS          {list(SIGNS)}")
    print(f"  CENTER_TICK          {CENTER}      TICKS_PER_REV {TICKS}")
    print(f"  SEED_SPEED_STEPS     {SEED_SPEED}  (~{SEED_SPEED * RAD_PER_TICK:.2f} rad/s)"
          f"   - the approach speed")
    print(f"  GOAL_SPEED_CAP_STEPS {GOAL_SPEED_CAP} (~{GOAL_SPEED_CAP * RAD_PER_TICK:.2f} rad/s)"
          f" - phase 4 uses this, because _write_targets does")
    print(f"  WRITE_ACCELERATION   {WRITE_ACCEL}")
    print(f"  LOOP_HZ              {LOOP_HZ:.0f}  -> a command every "
          f"{1000.0 / LOOP_HZ:.0f} ms in phase 4")
    print(f"  edge margins         torque-on {EDGE_REFUSE_MARGIN}, commanded goal "
          f"{GOAL_EDGE_MARGIN} (guards 3 and 5; this tool refuses inside them)")
    print()
    print("TARGET")
    print(f"  servo {sid} = {name_of(sid)}"
          + ("   (the DEFAULT: Max_Torque 150, carries no arm mass)"
             if sid == GRIPPER_ID else
             "   *** NOT THE GRIPPER - load-bearing, Max_Torque 800 ***"))
    print(f"  URDF limits          [{math.degrees(lo_rad):+.1f}, "
          f"{math.degrees(hi_rad):+.1f}] deg")
    print(f"  EEPROM window        {list(win)} ticks")
    print(f"  push direction       {dir_tick:+d} in tick space "
          f"({'closing' if sid == GRIPPER_ID else args.direction})")
    print(f"  push                 {args.push_ticks} ticks = "
          f"{args.push_ticks * DEG_PER_TICK:.2f} deg"
          + (f" ~ {args.push_ticks * RAD_PER_TICK * 25.2:.1f} mm of jaw travel"
             if sid == GRIPPER_ID else ""))
    print()
    print("WHAT IS WRITTEN TO THE BUS - the complete list")
    print(f"  register {fb.REG_ACCELERATION}..{fb.REG_ACCELERATION + 6}  the 7-byte "
          f"accel/goal/Goal_Time/speed block, on the TARGET servo only")
    print(f"  register {fb.REG_TORQUE_ENABLE}      Torque_Enable, values 0 and 1 only "
          f"(guard 6 refuses anything else)")
    print("  nothing else is reachable: BenchBus refuses every other address, so")
    print("  EEPROM (0..39) - Max_Torque, PROTECTION_CURRENT, the position limits,")
    print("  Homing_Offset - cannot be written even by a coding slip.")
    print()
    print("ABORT THRESHOLDS")
    print(f"  temperature   >= min({TEMP_ABORT_ABSOLUTE_C}, Max_Temperature(reg 13) "
          f"- {TEMP_MARGIN_BELOW_SERVO_TRIP_C})   [live read decides]")
    print(f"  current       >= min(PROTECTION_CURRENT(reg 28) x {CURRENT_LSB_MA} mA "
          f"x {CURRENT_ABORT_FRAC_OF_TRIP}, {CURRENT_ABORT_ABSOLUTE_MA:.0f} mA)")
    print(f"                   expected from docs 1.1: 310 LSB = 2015 mA -> abort at "
          f"{min(310 * CURRENT_LSB_MA * CURRENT_ABORT_FRAC_OF_TRIP, CURRENT_ABORT_ABSOLUTE_MA):.0f} mA")
    print(f"                   ABOVE the servo's own trip ON PURPOSE - the servo")
    print(f"                   protecting itself IS the phenomenon under test, so")
    print(f"                   this is the backstop for the backstop.")
    _dry_trip_ma = 310 * CURRENT_LSB_MA
    _dry_budget = HEAT_BUDGET_REF_S * (_dry_trip_ma / 1000.0) ** 2
    if sid == GRIPPER_ID:
        print(f"  heat (I^2 t)  NOT ARMED - the target is the gripper "
              f"(Max_Torque 150, ~5 W stalled, no arm mass)")
    else:
        print(f"  heat (I^2 t)  >= {HEAT_BUDGET_REF_S:.0f} s x trip^2 = "
              f"{_dry_budget:.1f} A^2 s cumulative, integrated ONLY while the")
        print(f"                   current is at or above "
              f"{HEAT_FLOOR_FRAC_OF_TRIP:.2f} x trip = "
              f"{HEAT_FLOOR_FRAC_OF_TRIP * _dry_trip_ma:.0f} mA")
        print(f"                   spans phases 2+3+4 TOGETHER - phases 3 and 4 are")
        print(f"                   the two a LATCH turns on, and a per-phase budget")
        print(f"                   would just be the phase clock again. So a longer")
        print(f"                   phase buys TIME, not ENERGY: at 2200 mA this")
        print(f"                   budget lasts {_dry_budget / 2.2 ** 2:.1f} s "
              f"(the measured unbounded case ran 30.0 s).")
    print(f"  error bits    any bit outside {EXPECTED_PROTECTION_BITS:#04x} "
          f"(overload 0x20 | overcurrent 0x08) aborts immediately")
    print(f"  travel        > push + {TRAVEL_SLACK_TICKS} ticks "
          f"(= {args.push_ticks + TRAVEL_SLACK_TICKS} ticks, "
          f"{(args.push_ticks + TRAVEL_SLACK_TICKS) * DEG_PER_TICK:.1f} deg)")
    print(f"  wrong way     > {WRONG_WAY_TICKS} ticks "
          f"({WRONG_WAY_TICKS * DEG_PER_TICK:.1f} deg) against the command")
    print(f"  bus           {READ_MISS_ABORT} consecutive full read failures")
    print(f"  per phase     stall {args.stall_s:.1f} s (ceiling {STALL_S_MAX:.0f}), "
          f"latch {args.latch_s:.1f} s, probe {args.probe_s:.1f} s")
    print(f"  global        {args.max_powered_s:.1f} s of powered time, hard "
          f"(ceiling {MAX_POWERED_S_MAX:.0f} s)")
    print("  on ANY abort: Torque_Enable = 0 is broadcast to ALL SEVEN servos as")
    print("  the first statement, before any logging, formatting or allocation.")
    print()
    print("PRE-FLIGHT REFUSALS (phase 1 - the run stops, nothing is energised)")
    print("  * fewer than 7 servos answering a ping")
    print("  * any servo reading Torque_Enable != 0 (register 40 is READ BACK)")
    print("  * any pre-existing error bit anywhere on the bus - it would make the")
    print("    observation vacuous. Remedy: power-cycle the 12 V to clear a latch.")
    print(f"  * target or goal within {GOAL_EDGE_MARGIN} ticks of the tick-0/4095")
    print("    position-map edge (guards 3/5 - the non-wrap-aware servo loop)")
    print(f"  * less than {PUSH_ROOM_TICKS} ticks of room left inside the EEPROM")
    print("    window past the goal - that is what keeps the stall against the")
    print("    OBJECT rather than the jaws' own hard stop (docs 9.2)")
    print("  * a move within a half-turn of ambiguity (structurally impossible at")
    print(f"    push <= {PUSH_TICKS_MAX}, asserted anyway)")
    print()
    print("PHASES")
    print("  1 BASELINE  read-only: ping, torque read-back, positions, currents,")
    print("              temperatures, voltages, error bytes, and the protection")
    print("              register set (13 Max_Temperature, 16 Max_Torque_Limit,")
    print("              28 Protection_Current, 34 Protective_Torque,")
    print("              35 Protection_Time, 36 Overload_Torque). Prints a")
    print("              reachability note: if Max_Torque_Limit/10 cannot reach")
    print("              Overload_Torque, the overload path may be unreachable on")
    print("              this joint and the run will honestly report INCONCLUSIVE.")
    print()
    print("      SETUP INSTRUCTIONS PRINTED TO THE OPERATOR:")
    for line in setup_instructions(sid):
        print("        " + line)
    print()
    print("      then the hands-off stationary gate (--baseline s, torque still")
    print(f"      OFF, refuses if the joint drifts more than "
          f"{BASELINE_DRIFT_TICKS} ticks)")
    print()
    if sid != GRIPPER_ID:
        print("      EXTRA CONFIRMATION (non-gripper joint), prompted verbatim as:")
        print(f"        {PROMPT_ARM.strip()}")
        print("      --yes does NOT satisfy this one; --i-understand-arm-stall does.")
        print()
    print("      CONFIRMATION, prompted verbatim as:")
    print(f"        {PROMPT_GO.strip()}")
    print()
    print("  2 INDUCE    seed Goal = Present + accel/speed (the driver's exact")
    print("              7-byte write at SEED_SPEED_STEPS), read Goal_Position back")
    print("              to prove the write landed, Torque_Enable = 1 on the TARGET")
    print("              SERVO ONLY, read register 40 back and FAIL if it is not 1")
    print("              (a joint that was never energised cannot answer anything),")
    print("              then command the push. Sampled at --hz.")
    print("  3 LATCH     nothing is written at all. Reading only. Does the")
    print("              indication persist? SKIPPED if phase 2 saw no indication -")
    print("              there is nothing to watch, and skipping ends the stall.")
    print("  4 CLEAR?    a command every 20 ms, the same 7-byte block at")
    print("              GOAL_SPEED_CAP_STEPS, because that is what _write_targets")
    print("              sends. Watches the indication AND whether the servo drives")
    print("              again (position, |load| and current deltas against the")
    print("              settled sample). SKIPPED if nothing latched.")
    print("              If the joint REACHES its goal during phase 3 or 4 the")
    print("              obstruction gave way: the stall ended MECHANICALLY, both")
    print("              observables stop meaning anything, and the verdict is")
    print(f"              INCONCLUSIVE (tolerance {PROBE_ARRIVE_TOL_TICKS} ticks, "
          f"the same one phase 2")
    print("              uses for 'nothing blocked it').")
    print("              -> then Torque_Enable = 0 on all 7, and the error byte is")
    print("                 read once more with the servo de-energised.")
    print("  5 CYCLE     optional, prompted verbatim as (--yes auto-confirms it,")
    print("              and a non-tty stdin declines instead of blocking):")
    print(f"                {PROMPT_CYCLE.strip()}")
    print("              re-energise with Goal = Present (no push), read the error")
    print("              byte, torque off. Separates 'a command cleared it' from")
    print("              'a torque cycle cleared it'. Only offered if something")
    print("              latched.")
    print()
    print("VERDICTS - exactly three, and the first gate cannot be bypassed")
    print("  INCONCLUSIVE   if protection never engaged; if an abort ended the run")
    print("                 before phase 4 completed; if the obstruction gave way")
    print("                 so the stall ended mechanically; or if the protection")
    print("                 channels that HAD indicated were unreadable at the one")
    print("                 moment that mattered. Licenses NOTHING. A servo that")
    print("                 never overloaded proves nothing about clearing; that is")
    print("                 the docs-11 'a zero-error hold proves nothing' trap and")
    print("                 it is guarded for explicitly.")
    print("  BACKSTOP HOLDS protection engaged, persisted with no commands, and")
    print("                 survived the command stream. Licenses 8.1's inflation,")
    print("                 R4's floor and R7's 'harmless stall'.")
    print("  BACKSTOP FALSE a command - or nothing at all - cleared it. Names the")
    print("                 three decisions that must be revisited, plus the")
    print("                 driver finding that our own write loop defeats a")
    print("                 hardware protection. Reachable ONLY on POSITIVE")
    print("                 evidence that the indication went away: a reading we")
    print("                 did not get is UNKNOWN, never ABSENT, and every such")
    print("                 sample routes to INCONCLUSIVE instead.")
    print()
    print("Finally: Present_Position of all 7 is printed BEFORE and AFTER, so you")
    print("can confirm nothing shifted. Torque-off runs in finally:, on SIGINT, on")
    print("SIGTERM and at atexit.")
    print()
    print("[DRY RUN COMPLETE] no port was opened.")
    return 0


def setup_instructions(sid: int) -> list[str]:
    if sid == GRIPPER_ID:
        return [
            "1. Put something COMPLIANT and EXPENDABLE between the jaws - a folded",
            "   cloth, a foam block, a pencil eraser. Thick enough that the jaws",
            "   grip it well before they reach their own hard stop.",
            "   NEVER a finger. Never anything you mind damaging.",
            "2. Close the jaws ONTO it BY HAND. They are limp and backdrive, so",
            "   just squeeze them shut until they touch without squeezing.",
            "   (--watch gives you a live readout while you do this.)",
            "3. Take your hands off the arm completely and let it settle.",
            "The rest of the arm stays limp for the whole run and will sag.",
        ]
    return [
        "*** THIS IS NOT THE GRIPPER. ***",
        "This joint runs Max_Torque 800 and carries part of the arm's mass.",
        "1. Put something COMPLIANT and EXPENDABLE in the joint's path - a foam",
        "   block, a folded cloth. NEVER a finger, and NEVER the arm's own",
        "   structure: a self-collision stall is R7's question, not this one.",
        "2. Support the arm. When torque drops - at the end, or on any abort -",
        "   this joint goes limp and the arm falls.",
        "3. Take your hands off before the run starts and let it settle.",
    ]


def _tick(rad: float, sign: int) -> int:
    return max(0, min(TICKS - 1, CENTER + int(round(rad * sign / RAD_PER_TICK))))


# -- main ---------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default=os.environ.get("EDU6_PORT", "COM5"))
    ap.add_argument("--baud", type=int, default=1000000)
    ap.add_argument("--joint", type=int, default=GRIPPER_ID,
                    help=f"servo id to stall (default {GRIPPER_ID}, the gripper - "
                         f"anything else is load-bearing and needs an extra "
                         f"confirmation)")
    ap.add_argument("--direction", default="auto", choices=("auto", "+", "-"),
                    help="tick direction of the push. 'auto' means CLOSING for the "
                         "gripper and is REFUSED for any other joint")
    ap.add_argument("--push-ticks", type=int, default=60,
                    help=f"how far past the obstruction to command "
                         f"(default 60 = 5.3 deg, ceiling {PUSH_TICKS_MAX})")
    ap.add_argument("--stall-s", type=float, default=3.0,
                    help=f"phase 2 duration (ceiling {STALL_S_MAX:.0f})")
    ap.add_argument("--latch-s", type=float, default=3.0,
                    help=f"phase 3 duration (ceiling {LATCH_S_MAX:.0f})")
    ap.add_argument("--probe-s", type=float, default=3.0,
                    help=f"phase 4 duration (ceiling {PROBE_S_MAX:.0f})")
    ap.add_argument("--max-powered-s", type=float, default=20.0,
                    help=f"global powered deadline (ceiling {MAX_POWERED_S_MAX:.0f})")
    ap.add_argument("--baseline", type=float, default=1.5,
                    help="hands-off stationary check before torque is enabled")
    ap.add_argument("--hz", type=float, default=20.0, help="sampling rate")
    ap.add_argument("--watch", action="store_true",
                    help="READ-ONLY live readout of the target joint, Ctrl-C to "
                         "exit. Use it while hand-closing the jaws onto the object")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the entire procedure, every prompt and every "
                         "threshold; open no serial port and write nothing")
    ap.add_argument("--yes", action="store_true",
                    help="skip the 'go' prompt (does NOT satisfy the non-gripper "
                         "confirmation)")
    ap.add_argument("--i-understand-arm-stall", action="store_true",
                    help="the non-interactive equivalent of typing 'ARM STALL'")
    args = ap.parse_args()

    # Validation runs in --dry-run too, so the operator sees the real refusals.
    if args.joint not in SERVO_IDS:
        print(f"[FAIL] --joint must be one of {list(SERVO_IDS)}")
        return 1
    if args.joint != GRIPPER_ID and args.direction == "auto":
        print(f"[FAIL] --direction auto means 'closing' and is only defined for the "
              f"gripper (servo {GRIPPER_ID}). For servo {args.joint} say + or - "
              f"explicitly, so the push direction is a decision and not a default.")
        return 1
    if not 1 <= args.push_ticks <= PUSH_TICKS_MAX:
        print(f"[FAIL] --push-ticks must be 1..{PUSH_TICKS_MAX} "
              f"({PUSH_TICKS_MAX * DEG_PER_TICK:.1f} deg). A bigger push is a bigger "
              f"stall, and the point of this test is the smallest one that answers "
              f"the question.")
        return 1
    for label, value, ceiling in (("--stall-s", args.stall_s, STALL_S_MAX),
                                  ("--latch-s", args.latch_s, LATCH_S_MAX),
                                  ("--probe-s", args.probe_s, PROBE_S_MAX),
                                  ("--max-powered-s", args.max_powered_s,
                                   MAX_POWERED_S_MAX)):
        if not 0.1 <= value <= ceiling:
            print(f"[FAIL] {label} must be 0.1..{ceiling:.0f} s (got {value}).")
            return 1
    budget = args.stall_s + args.latch_s + args.probe_s
    if args.max_powered_s < budget:
        print(f"[FAIL] --max-powered-s {args.max_powered_s:.1f} is below the sum of "
              f"the phases ({budget:.1f} s) - the deadline would abort a healthy "
              f"run and report INCONCLUSIVE.")
        return 1

    if args.dry_run:
        return dry_run(args)

    sid = args.joint
    idx = SERVO_IDS.index(sid)
    label = name_of(sid)
    lo_rad, hi_rad = JOINT_LIMITS_RAD[idx]
    win_lo, win_hi = fb.position_limit_window(lo_rad, hi_rad, SIGNS[idx])

    try:
        bus = BenchBus(args.port, baudrate=args.baud)
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] cannot open {args.port}: {e}")
        print("       'Zugriff verweigert' / PermissionError almost always means "
              "ANOTHER process holds the port - an open bench tool, or a container "
              "with the device usbipd-attached. Only one process can own the bus.")
        return 1

    flags = {"closed": False}

    def kill():
        """Torque OFF on ALL SEVEN, as the very first thing on any exit path.

        One broadcast SYNC_WRITE de-energises everything in a single 22-byte
        packet; the per-servo acknowledged writes afterwards are verification, not
        the mechanism. Order matters: kill first, confirm second.

        Idempotent and safe from the signal handler: the broadcast carries no
        reply, so it cannot interleave with a read in progress, and anything the
        verification pass corrupts is on a run that is already unwinding."""
        try:
            bus.sync_write(fb.REG_TORQUE_ENABLE, _OFF_ALL_PAYLOAD)
        except Exception:  # noqa: BLE001
            pass
        bad = []
        for s2 in SERVO_IDS:
            try:
                bus.write(s2, fb.REG_TORQUE_ENABLE, bytes([0]))
                if bus.read(s2, fb.REG_TORQUE_ENABLE, 1)[1][0] != 0:
                    bad.append(s2)
            except Exception:  # noqa: BLE001
                bad.append(s2)
        # Suppressed only once the port is CLOSED (the atexit call lands there and
        # would always "fail"). A genuine failure on a live port always warns, no
        # matter how many times kill() has already run - the whole point of the
        # message is that the operator must reach for the 12 V.
        if bad and not flags["closed"]:
            print(f"[WARN] could not CONFIRM torque-off on servo(s) {bad} - "
                  f"CUT THE 12 V.")

    atexit.register(kill)

    def _sig(_signum, _frame):
        kill()
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sig)
    for extra in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, extra):
            try:
                signal.signal(getattr(signal, extra), _sig)
            except (ValueError, OSError):
                pass

    state = {
        "latched": False, "latch_reasons": [], "latch_t": 0.0,
        # TRI-STATE, and the None is load-bearing: True = positively still
        # indicated at the end of phase 3, False = positively gone, None = we
        # could not read it. Only False may reach BACKSTOP FALSE.
        "held_after_no_commands": None, "cleared_during_commands": None,
        "driving_resumed": [], "aborted": None, "phase4_complete": False,
        "arrived_without_stall": False, "phase3_ran": False,
        "stall_ended_mechanically": None, "heat": None,
        "stall_s": args.stall_s, "latch_s": args.latch_s, "probe_s": args.probe_s,
        "push_ticks": args.push_ticks, "protection_time": 0,
        "max_torque_limit": 0, "overload_torque": 0,
    }
    pos_before: dict[int, int] = {}
    pos_after: dict[int, int] = {}
    verdict_ready = False

    try:
        # == PHASE 1 - BASELINE (read-only) ===================================
        print("=" * 78)
        print("PHASE 1 - BASELINE (read-only; nothing is written)")
        print("=" * 78)
        alive = [s2 for s2 in SERVO_IDS if bus.ping(s2)]
        if len(alive) != len(SERVO_IDS):
            print(f"[FAIL] only {alive} answered a ping. The USB adapter alone does "
                  f"NOT power the servos - check the arm's 12 V supply is plugged "
                  f"in AND switched on, then the daisy chain.")
            return 1
        print(f"[OK] all {len(SERVO_IDS)} servos answer on {args.port}.")

        torque = {}
        for s2 in SERVO_IDS:
            torque[s2] = bus.read(s2, fb.REG_TORQUE_ENABLE, 1)[1][0]
        on = [s2 for s2, v in torque.items() if v]
        if on:
            print(f"[STOP] Torque_Enable reads NON-ZERO on servo(s) {on} "
                  f"({[torque[s2] for s2 in on]}).")
            print("       This test needs a known-limp starting state, and turning "
                  "torque off from here would drop a pose somebody may be holding. "
                  "Stop the environment (or power-cycle the 12 V) and re-run.")
            return 2
        print("[OK] register 40 read back as 0 on all 7 - the arm is limp.")

        print()
        print("  joint            tick     deg    err  temp  volt   current")
        pre_err = []
        for i, s2 in enumerate(SERVO_IDS):
            s = sample_servo(bus, s2, time.monotonic())
            if not s.ok():
                print(f"[FAIL] servo {s2} did not answer a full read.")
                return 1
            pos_before[s2] = s.tick
            e = s.err_any()
            if e:
                pre_err.append((s2, e))
            print(f"  {JOINT_NAMES[i]:<15} {s.tick:>4} {deg_of(s2, s.tick):>+8.2f} "
                  f"{e:>#6x} {('  ?' if s.temp_c is None else f'{s.temp_c:3d}C')} "
                  f"{('    ?' if s.volt_v is None else f'{s.volt_v:5.1f}V')} "
                  f"{('      ?' if s.current_ma is None else f'{s.current_ma:5.0f}mA')}"
                  + (f"   {de(fb.describe_error_bits(e))}" if e else ""))
        if pre_err:
            print()
            print(f"[STOP] pre-existing error bit(s) on the bus: "
                  f"{[(s2, hex(e), de(fb.describe_error_bits(e))) for s2, e in pre_err]}")
            print("       This whole test is about watching an error bit APPEAR and "
                  "then seeing whether a command makes it go away. Starting from a "
                  "dirty byte makes every later observation ambiguous.")
            print("       Remedy: power-cycle the arm's 12 V (that is the documented "
                  "way to clear a latched protection), then re-run.")
            return 2

        # protection register set - read-only context, and the reachability note
        prot = {}
        for reg, nm in ((fb.REG_MAX_TEMPERATURE, "Max_Temperature"),
                        (fb.REG_PROTECTIVE_TORQUE, "Protective_Torque"),
                        (fb.REG_OVERLOAD_TORQUE, "Overload_Torque"),
                        (fb.REG_UNLOAD_CONDITION, "Unload_Condition")):
            prot[nm] = bus.read(sid, reg, 1)[1][0]
        for reg, nm in ((fb.REG_MAX_TORQUE_LIMIT, "Max_Torque_Limit"),
                        (fb.REG_PROTECTION_CURRENT, "Protection_Current"),
                        (fb.REG_PROTECTION_TIME, "Protection_Time"),
                        (fb.REG_TORQUE_LIMIT, "Torque_Limit(RAM)")):
            prot[nm] = bus.read_u16(sid, reg)
        # Protection_Time is 2 bytes at 35 only if 35/36 are contiguous; read it as
        # one byte too and prefer the byte, which is what the table documents.
        prot["Protection_Time"] = bus.read(sid, fb.REG_PROTECTION_TIME, 1)[1][0]
        trip_ma = prot["Protection_Current"] * CURRENT_LSB_MA
        state["protection_time"] = prot["Protection_Time"]
        state["max_torque_limit"] = prot["Max_Torque_Limit"]
        state["overload_torque"] = prot["Overload_Torque"]

        print()
        print(f"[PROTECTION REGISTERS] servo {sid} ({label}) - read-only")
        for nm in ("Max_Torque_Limit", "Torque_Limit(RAM)", "Protection_Current",
                   "Protective_Torque", "Protection_Time", "Overload_Torque",
                   "Max_Temperature", "Unload_Condition"):
            print(f"  {nm:<20} {prot[nm]}")
        print(f"  Protection_Current {prot['Protection_Current']} LSB x "
              f"{CURRENT_LSB_MA} mA = {trip_ma:.0f} mA")
        print(f"  Protection_Time raw {prot['Protection_Time']}; the vendor unit is "
              f"commonly 10 ms/LSB -> ~{prot['Protection_Time'] / 100.0:.2f} s of")
        print( "  SUSTAINED overload before the servo acts. That unit is UNVERIFIED "
               "on this arm -")
        print( "  treat the seconds as indicative, and prefer a stall that "
               "comfortably outlasts it.")
        recommended = prot["Protection_Time"] / 100.0 + 1.0
        if args.stall_s < recommended:
            print(f"  [NOTE] --stall-s {args.stall_s:.1f} s is below the indicative "
                  f"{recommended:.1f} s (Protection_Time + 1 s). The run may end "
                  f"INCONCLUSIVE; that is a correct answer, not a failure.")
        reach_pct = prot["Max_Torque_Limit"] / 10.0
        # `<=`, not `<`. At EXACTLY zero margin (800/10 = 80 vs Overload_Torque
        # 80) the strict form printed NOTHING - and that is precisely the case
        # that most needs saying, because it is the knife edge where a rounding
        # difference inside the firmware decides whether the overload path is
        # reachable at all. A marginal case that prints nothing looks like a
        # comfortable one.
        if prot["Overload_Torque"] and reach_pct <= prot["Overload_Torque"]:
            where = ("EXACTLY AT" if reach_pct == prot["Overload_Torque"]
                     else "BELOW")
            print(f"  [NOTE] REACHABILITY: Max_Torque_Limit {prot['Max_Torque_Limit']} "
                  f"caps output near {reach_pct:.0f} % PWM, which is {where} "
                  f"Overload_Torque {prot['Overload_Torque']} %.")
            print( "         If the vendor compares Overload_Torque against full "
                   "scale (not against")
            print( "         the torque limit) then the OVERLOAD path may be "
                   "unreachable on this joint")
            print( "         by construction, and only the overcurrent path or a "
                   "thermal trip could fire.")
            print( "         An INCONCLUSIVE verdict here would be a fact about the "
                   "registers, not a")
            print( "         bad run - the escalation is an arm joint (Max_Torque "
                   "800), with care.")

        # geometry pre-flight
        start_tick = pos_before[sid]
        dir_tick = (1 if _tick(lo_rad, SIGNS[idx]) > _tick(hi_rad, SIGNS[idx]) else -1)
        if args.direction in ("+", "-"):
            dir_tick = 1 if args.direction == "+" else -1
        goal_tick = start_tick + dir_tick * args.push_ticks

        print()
        print(f"[PLAN] {label} (servo {sid}) at tick {start_tick} = "
              f"{deg_of(sid, start_tick):+.2f} deg")
        print(f"       push {dir_tick:+d} x {args.push_ticks} ticks -> goal "
              f"{goal_tick} = {deg_of(sid, goal_tick):+.2f} deg "
              f"({args.push_ticks * DEG_PER_TICK:.2f} deg of travel)")
        print(f"       EEPROM window [{win_lo}, {win_hi}]")

        # --watch sits ABOVE the pre-flight refusals on purpose: the commonest
        # refusal is "not enough room inside the window", and its remedy is to
        # hand-close the jaws onto a thicker object - which is exactly what this
        # readout is for. A watch that only ran once the geometry was already
        # right would be useless.
        if args.watch:
            print()
            print("[WATCH] READ-ONLY. Hand-close the jaws onto the object and "
                  "watch. Ctrl-C to exit.")
            try:
                while True:
                    s = sample_servo(bus, sid, time.monotonic())
                    if s.ok():
                        room_now = ((win_hi - s.tick) if dir_tick > 0
                                    else (s.tick - win_lo))
                        print(f"   tick {s.tick:>4}  {deg_of(sid, s.tick):>+8.2f} deg"
                              f"  load {s.load:>+5}  room past a "
                              f"{args.push_ticks}-tick push "
                              f"{room_now - args.push_ticks:>5}"
                              f"  (need >= {PUSH_ROOM_TICKS})")
                    else:
                        print("   no reply")
                    time.sleep(0.25)
            except KeyboardInterrupt:
                print("\n(stopped - nothing was written)")
            return 0

        room = (win_hi - goal_tick) if dir_tick > 0 else (goal_tick - win_lo)
        if goal_tick < 0 or goal_tick > TICKS - 1 or room < PUSH_ROOM_TICKS:
            print(f"[STOP] the goal leaves only {room} ticks of room inside the "
                  f"EEPROM window (need {PUSH_ROOM_TICKS}).")
            print("       That room is what keeps the stall against the OBJECT "
                  "instead of against the joint's own hard stop - which docs 9.2 "
                  "names explicitly as a thing not to do.")
            if sid == GRIPPER_ID:
                print("       Open the jaws a little and hand-close them onto a "
                      "THICKER object, then re-run (--watch helps).")
            return 2
        for what, tick in (("start", start_tick), ("goal", goal_tick)):
            edge = min(tick, TICKS - 1 - tick)
            if edge < GOAL_EDGE_MARGIN:
                print(f"[STOP] the {what} tick {tick} is {edge} ticks from the "
                      f"tick-0/4095 position-map edge (guard 5 keeps commanded "
                      f"goals {GOAL_EDGE_MARGIN} clear, guard 3 refuses a torque-on "
                      f"within {EDGE_REFUSE_MARGIN}).")
                print("       The STS position loop is NOT wrap-aware (docs 2.3); a "
                      "stall test is not the place to meet that. Hand-turn the "
                      "joint toward the middle and re-run.")
                return 2
        if HALF_TURN - abs(goal_tick - start_tick) <= WRONG_WAY_TICKS:
            print("[STOP] the commanded move is within a half-turn of ambiguity.")
            return 2
        print(f"       seam clearance: start {min(start_tick, TICKS - 1 - start_tick)} "
              f"ticks, goal {min(goal_tick, TICKS - 1 - goal_tick)} ticks "
              f"(need >= {GOAL_EDGE_MARGIN})")
        print(f"       window room past the goal: {room} ticks "
              f"(need >= {PUSH_ROOM_TICKS})")

        # thresholds
        max_temp = prot["Max_Temperature"]
        if not (TEMP_EEPROM_PLAUSIBLE_C[0] <= max_temp <= TEMP_EEPROM_PLAUSIBLE_C[1]):
            print(f"[WARN] Max_Temperature reads {max_temp} C, outside the plausible "
                  f"range {TEMP_EEPROM_PLAUSIBLE_C} - using the absolute abort "
                  f"threshold only.")
            temp_abort = TEMP_ABORT_ABSOLUTE_C
        else:
            temp_abort = min(TEMP_ABORT_ABSOLUTE_C,
                             max_temp - TEMP_MARGIN_BELOW_SERVO_TRIP_C)
        current_abort = min(trip_ma * CURRENT_ABORT_FRAC_OF_TRIP,
                            CURRENT_ABORT_ABSOLUTE_MA)
        # ONE accumulator for the whole powered run - see HEAT_BUDGET_REF_S. Both
        # numbers come off the servo's OWN Protection_Current register, so the
        # bound scales with the rig instead of being typed here. On the gripper it
        # is armed as a recorder only (budget None): a ~5 W stall needs no bound,
        # and the peak/integral it collects are R4 data either way.
        heat = HeatAccumulator(
            floor_ma=HEAT_FLOOR_FRAC_OF_TRIP * trip_ma,
            budget_a2s=(None if sid == GRIPPER_ID
                        else HEAT_BUDGET_REF_S * (trip_ma / 1000.0) ** 2))
        state["heat"] = heat

        ctx = PushCtx(sid=sid, label=label, start_tick=start_tick,
                      goal_tick=goal_tick, push_ticks=args.push_ticks,
                      dir_tick=dir_tick,
                      baseline_torque_limit=prot["Torque_Limit(RAM)"] or None)

        print()
        print("[ABORT THRESHOLDS in force for this run]")
        print(f"  temperature   >= {temp_abort} C   "
              f"(servo's own Max_Temperature {max_temp} C, minus "
              f"{TEMP_MARGIN_BELOW_SERVO_TRIP_C})")
        print(f"  current       >= {current_abort:.0f} mA  "
              f"({CURRENT_ABORT_FRAC_OF_TRIP:.2f}x the servo's own {trip_ma:.0f} mA "
              f"trip - ABOVE it on purpose: the servo acting first IS the "
              f"phenomenon)")
        if heat.budget_a2s is None:
            print(f"  heat (I^2 t)  NOT ARMED - the gripper stalls at ~5 W and "
                  f"carries no arm mass (recording peak/integral only)")
        else:
            print(f"  heat (I^2 t)  >= {heat.budget_a2s:.1f} A^2 s cumulative over "
                  f"phases 2+3+4, counted only above {heat.floor_ma:.0f} mA")
            print(f"                  (= {HEAT_BUDGET_REF_S:.0f} s at this servo's "
                  f"own {trip_ma:.0f} mA trip; {heat.budget_a2s / 2.2 ** 2:.1f} s at "
                  f"2200 mA. Longer phases buy TIME, not ENERGY.)")
        print(f"  error bits    any bit outside {EXPECTED_PROTECTION_BITS:#04x}")
        print(f"  travel        > {args.push_ticks + TRAVEL_SLACK_TICKS} ticks "
              f"({(args.push_ticks + TRAVEL_SLACK_TICKS) * DEG_PER_TICK:.1f} deg)")
        print(f"  wrong way     > {WRONG_WAY_TICKS} ticks")
        print(f"  bus           {READ_MISS_ABORT} consecutive full read failures")
        print(f"  phases        {args.stall_s:.1f} / {args.latch_s:.1f} / "
              f"{args.probe_s:.1f} s   global {args.max_powered_s:.1f} s")
        print("  on ANY abort, Torque_Enable = 0 goes to ALL SEVEN first.")

        print()
        print("[SETUP]")
        for line in setup_instructions(sid):
            print("  " + line)

        print()
        if sid != GRIPPER_ID:
            print(f"  *** {label} (servo {sid}) is NOT the gripper. It runs "
                  f"Max_Torque_Limit {prot['Max_Torque_Limit']} and carries part of "
                  f"the arm's mass. ***")
            if not confirm(PROMPT_ARM, "arm stall", args.i_understand_arm_stall):
                print("[ABORT] not confirmed - nothing was written.")
                return 0
        if not confirm(PROMPT_GO, "go", args.yes):
            print("[ABORT] nothing was written.")
            return 0

        # hands-off stationary gate
        print(f"[BASE]  HANDS OFF - {args.baseline:.1f} s stationary check "
              f"(torque still OFF)...")
        b_end = time.monotonic() + args.baseline
        b_ticks = []
        while time.monotonic() < b_end:
            s = sample_servo(bus, sid, time.monotonic())
            if s.ok():
                b_ticks.append(s.tick)
            time.sleep(0.05)
        if not b_ticks:
            print("[FAIL] no baseline samples.")
            return 1
        drift = max(b_ticks) - min(b_ticks)
        print(f"[BASE]  drift {drift} ticks over {len(b_ticks)} samples")
        if drift > BASELINE_DRIFT_TICKS:
            print(f"[ABORT] the joint is MOVING before torque is enabled ({drift} "
                  f"ticks). Nothing was energised. Hands off completely, let it "
                  f"settle, re-run - otherwise nothing observed later can be "
                  f"attributed to the servo.")
            return 2
        ctx.start_tick = b_ticks[-1]
        ctx.goal_tick = ctx.start_tick + dir_tick * args.push_ticks

        # == PHASE 2 - INDUCE =================================================
        print()
        print("=" * 78)
        print("PHASE 2 - INDUCE (the arm is energised from here to the end of "
              "phase 4)")
        print("=" * 78)
        th = Thresholds(
            temp_abort_c=temp_abort, current_abort_ma=current_abort,
            travel_abort_ticks=TRAVEL_SLACK_TICKS, wrong_way_ticks=WRONG_WAY_TICKS,
            read_miss_abort=READ_MISS_ABORT,
            torque_limit_drop_frac=TORQUE_LIMIT_DROP_FRAC,
            powered_deadline=time.monotonic() + args.max_powered_s)

        seed = (bytes([WRITE_ACCEL]) + fb.le16(ctx.start_tick) + fb.le16(0)
                + fb.le16(SEED_SPEED))
        bus.sync_write(fb.REG_ACCELERATION, {sid: seed})
        rb = bus.read_u16(sid, fb.REG_GOAL_POSITION)
        print(f"[SEED]  Goal = Present = {ctx.start_tick}, read back {rb}")
        if abs(rb - ctx.start_tick) > 2:
            print("[FAIL] the seed write did not land - NOT energising. (Register 42 "
                  "reads back the RAW written value, so this is a delivery check, "
                  "not a check of the servo's clamped target.)")
            return 1

        bus.write(sid, fb.REG_TORQUE_ENABLE, bytes([1]))
        tq = bus.read(sid, fb.REG_TORQUE_ENABLE, 1)[1][0]
        print(f"[ON]    Torque_Enable read back = {tq}")
        if tq != 1:
            kill()
            print("[FAIL] the servo did NOT energise. This run would prove nothing "
                  "(docs 11: without a torque read-back, 'nothing happened' is "
                  "vacuous). Aborting.")
            return 1
        print(f"[ON]    torque CONFIRMED on servo {sid} ONLY - the other six stay "
              f"limp.")
        # Re-baseline Torque_Limit (register 48, RAM) now that torque is ON. The
        # vendor copies Max_Torque_Limit into it at power-on, but a de-energised
        # servo can read 0 there, and a baseline of 0 silently disables the
        # de-rate observable - leaving the error bit as the only evidence.
        try:
            live_limit = bus.read_u16(sid, fb.REG_TORQUE_LIMIT)
            if live_limit:
                ctx.baseline_torque_limit = live_limit
        except fb.FeetechBusError:
            pass
        print(f"[ON]    Torque_Limit baseline for the de-rate check: "
              f"{ctx.baseline_torque_limit} (a drop below "
              f"{TORQUE_LIMIT_DROP_FRAC:.0%} of it counts as protection)")

        push = (bytes([WRITE_ACCEL]) + fb.le16(ctx.goal_tick) + fb.le16(0)
                + fb.le16(SEED_SPEED))
        bus.sync_write(fb.REG_ACCELERATION, {sid: push})
        rb = bus.read_u16(sid, fb.REG_GOAL_POSITION)
        print(f"[PUSH]  goal {ctx.goal_tick} written, read back {rb} - approaching "
              f"at {SEED_SPEED} steps/s (~{SEED_SPEED * RAD_PER_TICK:.2f} rad/s)")

        s2_samples, code, arrived = powered_window(
            bus, ctx, th, "INDUCE", args.stall_s, args.hz, kill,
            write_payload=None, stop_on_arrival=True, heat=heat)
        state["arrived_without_stall"] = arrived
        if code:
            state["aborted"] = abort_message(code, s2_samples[-1], th, ctx, heat)

        for s in s2_samples:
            why = protection_reasons(s, ctx, th.torque_limit_drop_frac)
            if why and not state["latched"]:
                state["latched"] = True
                state["latch_reasons"] = why
                state["latch_t"] = s.t
        if state["latched"]:
            print(f"[PHASE 2] PROTECTION INDICATED at t = {state['latch_t']:.2f} s: "
                  f"{'; '.join(state['latch_reasons'])}")
        else:
            print("[PHASE 2] no protection indication in this phase.")
        if arrived:
            print("[PHASE 2] the joint reached its goal - nothing blocked it, so no "
                  "stall was induced.")

        if code or arrived or not state["latched"]:
            kill()
            print("[SAFE] torque off - phases 3 and 4 are SKIPPED. With no "
                  "indication there is nothing to watch, and skipping ends the "
                  "stall immediately.")
            verdict_ready = True
        else:
            # == PHASE 3 - LATCH ==============================================
            print()
            print("=" * 78)
            print("PHASE 3 - OBSERVE THE LATCH (nothing is written; reading only)")
            print("=" * 78)
            state["phase3_ran"] = True
            s3, code3, _ = powered_window(
                bus, ctx, th, "LATCH", args.latch_s, args.hz, kill,
                write_payload=None, stop_on_arrival=False, heat=heat)
            # Which observables ever ACTUALLY said anything on this run. Judging
            # the settled state over only those is what stops a silent register-48
            # read from making a bit-only latch unprovable, while still refusing
            # to call a de-rate-only latch "cleared" on a reply that never came.
            channels = indicated_channels(
                s2_samples + s3, ctx, th.torque_limit_drop_frac) or None
            settled = last_valid(s3)
            if settled is None:
                still = None
            else:
                st3 = protection_state(settled, ctx, th.torque_limit_drop_frac,
                                       channels)
                still = (True if st3 == PROT_PRESENT else
                         False if st3 == PROT_ABSENT else None)
            state["held_after_no_commands"] = still
            if code3:
                state["aborted"] = abort_message(code3, s3[-1], th, ctx, heat)
            print(f"[PHASE 3] with NO commands at all, the indication "
                  + ("PERSISTED" if still else
                     "WENT AWAY BY ITSELF" if still is False else
                     "could NOT be read - no channel that had indicated came back "
                     "a usable answer, so this phase decided NOTHING")
                  + f" after {args.latch_s:.1f} s.")
            if code3:
                kill()
                verdict_ready = True
            else:
                # == PHASE 4 - THE CLEARING QUESTION ==========================
                print()
                print("=" * 78)
                print(f"PHASE 4 - DOES A POSITION COMMAND CLEAR IT? "
                      f"({1000.0 / LOOP_HZ:.0f} ms stream)")
                print("=" * 78)
                stream = (bytes([WRITE_ACCEL]) + fb.le16(ctx.goal_tick)
                          + fb.le16(0) + fb.le16(GOAL_SPEED_CAP))
                print(f"[STREAM] the same 7-byte block _write_targets sends: accel "
                      f"{WRITE_ACCEL}, goal {ctx.goal_tick}, Goal_Time 0, speed "
                      f"{GOAL_SPEED_CAP} - at {LOOP_HZ:.0f} Hz.")
                s4, code4, _ = powered_window(
                    bus, ctx, th, "CLEAR?", args.probe_s, args.hz, kill,
                    write_payload=stream, stop_on_arrival=False, heat=heat)
                # "Cleared" is judged over EVERY readable sample, not just the
                # last: the transition is the finding, and a protection that
                # clears and re-trips has still been cleared by a command.
                # ABSENT only - a sample whose channels could not be read is
                # UNKNOWN and is not evidence of anything, in either direction.
                p4 = [protection_state(s, ctx, th.torque_limit_drop_frac, channels)
                      for s in s4]
                judged = [p for p in p4 if p != PROT_UNKNOWN]
                cleared = PROT_ABSENT in judged
                resumed: list[str] = []
                for s in s4:
                    r = resumed_driving(s, settled)
                    if r and not resumed:
                        resumed = r
                state["cleared_during_commands"] = cleared
                state["driving_resumed"] = resumed
                # THE STALL MAY HAVE ENDED MECHANICALLY. Phase 4 deliberately does
                # not stop on arrival (that would shorten the powered window and
                # change what is being measured), so arrival is detected here,
                # after the fact, over phases 3 AND 4 - a compliant object can give
                # way while nothing is being commanded just as easily.
                gave_way = stall_ended_mechanically(
                    s3 + s4, ctx, PROBE_ARRIVE_TOL_TICKS)
                if gave_way is not None:
                    state["stall_ended_mechanically"] = (
                        f"the joint reached tick {gave_way.tick} against a goal of "
                        f"{ctx.goal_tick} (within {PROBE_ARRIVE_TOL_TICKS} ticks) - "
                        f"it completed the commanded "
                        f"{ctx.push_ticks * DEG_PER_TICK:.2f} deg of travel")
                # A phase with no JUDGEABLE sample answered nothing, even if it ran
                # to its full duration without an abort - `any([])` is False, which
                # would otherwise read as "the indication survived" and could
                # produce a HOLDS banner off a silent bus.
                state["phase4_complete"] = code4 is None and bool(judged)
                if code4:
                    state["aborted"] = abort_message(code4, s4[-1], th, ctx, heat)
                elif not judged:
                    state["aborted"] = ("no JUDGEABLE sample during the command "
                                        "stream - every sample was UNKNOWN on the "
                                        "channels that had indicated")
                verb = ("CLEARED" if cleared else
                        "still present" if judged else
                        "could not be judged")
                drove = ("RESUMED DRIVING (" + "; ".join(resumed) + ")"
                         if resumed else "did not resume driving")
                print(f"[PHASE 4] indication {verb}; servo {drove}.")
                if state["stall_ended_mechanically"]:
                    print(f"[PHASE 4] {state['stall_ended_mechanically']}. The "
                          f"obstruction gave way, so neither observable above can "
                          f"be read as a statement about the protection.")
                kill()
                verdict_ready = True

            # De-energised read of the error byte - free, and it separates "the
            # command cleared it" from "dropping torque cleared it".
            #
            # SCOPE, corrected: this runs on every path that reached PHASE 3, not
            # on every path that energised. A phase-2 abort, an arrival, or a run
            # with no indication at all takes the `if` branch above and never gets
            # here. That is deliberate and stays: those paths have already
            # de-energised and the operator's next action is physical, so putting
            # another round trip on the bus after an abort would buy a data point
            # nobody is waiting for at the one moment the tool should be quiet.
            off_sample = sample_servo(bus, sid, time.monotonic())
            print(f"[OFF]   de-energised, error byte now "
                  f"{off_sample.err_any():#04x} "
                  f"({de(fb.describe_error_bits(off_sample.err_any())) or 'clean'}), "
                  f"Torque_Limit {off_sample.torque_limit}")

            # == PHASE 5 - TORQUE CYCLE (optional) ============================
            if state["latched"]:
                print()
                print("=" * 78)
                print("PHASE 5 - TORQUE-CYCLE PROBE (optional; torque is already "
                      "off)")
                print("=" * 78)
                print("  Re-energises with Goal = Present - NO push, so no stall - "
                      "and reads")
                print("  the error byte again. This separates 'a position command "
                      "cleared it'")
                print("  from 'a torque cycle cleared it'. It is run AFTER the "
                      "verdict data is")
                print("  collected precisely so it cannot confound it.")
                # args.yes, not a hardcoded False. The literal made this prompt the
                # ONE gate no flag could satisfy, so an unattended run reached an
                # input() with nobody typing: EOFError only fires on a stream that
                # has ENDED, so a pipe or a service harness held here forever - in
                # a process that owns the torque-off path. (confirm() also declines
                # rather than blocks on a non-tty; torque is already off here, so
                # both directions are safe and neither hangs.)
                if confirm(PROMPT_CYCLE, "cycle", args.yes):
                    here = sample_servo(bus, sid, time.monotonic())
                    if here.ok():
                        seed2 = (bytes([WRITE_ACCEL]) + fb.le16(here.tick)
                                 + fb.le16(0) + fb.le16(SEED_SPEED))
                        bus.sync_write(fb.REG_ACCELERATION, {sid: seed2})
                        bus.write(sid, fb.REG_TORQUE_ENABLE, bytes([1]))
                        time.sleep(0.3)
                        after = sample_servo(bus, sid, time.monotonic())
                        kill()
                        print(f"[CYCLE] before {here.err_any():#04x} -> after "
                              f"{after.err_any():#04x} "
                              f"({de(fb.describe_error_bits(after.err_any())) or 'clean'}), "
                              f"Torque_Limit {here.torque_limit} -> "
                              f"{after.torque_limit}")
                    else:
                        print("[CYCLE] no reply - skipped.")
                else:
                    print("[CYCLE] skipped.")
    except KeyboardInterrupt:
        print("\n[ABORT] interrupted - torque was dropped by the signal handler.")
        state["aborted"] = state["aborted"] or "interrupted by the operator (Ctrl-C)"
    except Exception as e:  # noqa: BLE001
        kill()
        print(f"\n[ABORT] unexpected error: {e!r} - torque dropped.")
        state["aborted"] = state["aborted"] or f"unexpected error: {e!r}"
    finally:
        kill()
        try:
            for s2 in SERVO_IDS:
                try:
                    pos_after[s2] = fb.decode_sign_magnitude(
                        bus.read_u16(s2, fb.REG_PRESENT_POSITION), 15)
                except fb.FeetechBusError:
                    pass
        except Exception:  # noqa: BLE001
            pass
        flags["closed"] = True
        try:
            bus.close()
        except Exception:  # noqa: BLE001
            pass

    # -- position audit: did anything shift? ----------------------------------
    if pos_before and pos_after:
        print()
        print("POSITION AUDIT (before -> after; only the target joint should move, "
              "by the commanded push)")
        for i, s2 in enumerate(SERVO_IDS):
            b, a = pos_before.get(s2), pos_after.get(s2)
            if b is None or a is None:
                print(f"  {JOINT_NAMES[i]:<15} (no reading)")
                continue
            d = a - b
            print(f"  {JOINT_NAMES[i]:<15} {b:>4} -> {a:>4}  {d:>+5} ticks "
                  f"({d * DEG_PER_TICK:>+7.2f} deg)"
                  + ("   <== target" if s2 == sid else
                     "   <== UNEXPECTED" if abs(d) > 5 else ""))
        print("  (a limp arm sags, so a few ticks elsewhere are gravity, not this "
              "tool - nothing outside registers 40..47 was ever written.)")

    # -- energy audit: R4 data whether or not the budget was armed ------------
    _heat = state["heat"]
    if _heat is not None and _heat.last_t is not None:
        print()
        print(f"ENERGY AUDIT  peak {_heat.peak_ma:.0f} mA; "
              f"{_heat.seconds_above_floor:.1f} s at or above {_heat.floor_ma:.0f} mA; "
              f"{_heat.a2s:.1f} A^2 s"
              + (" (budget not armed - gripper)" if _heat.budget_a2s is None
                 else f" of a {_heat.budget_a2s:.1f} A^2 s budget"))
        print("  (record the peak: it is the R4 stall-current datum, and the "
              "integral is what bounds an unattended arm-joint run.)")

    if not verdict_ready and not state["aborted"]:
        state["aborted"] = state["aborted"] or "the run did not reach a verdict"

    code, lines = choose_verdict(state)
    print()
    print("=" * 78)
    print(f"D1 VERDICT: {code}"
          + {"HOLDS": "  - the current-limit backstop premise HOLDS",
             "FALSE": "  - the current-limit backstop premise is FALSE",
             "INCONCLUSIVE": "  - nothing is licensed"}[code])
    print("=" * 78)
    for line in lines:
        print(line)
    print()
    print("[DONE] every servo is de-energised. The arm sags - the STS gear train "
          "backdrives, which is expected.")
    return 0 if code != "INCONCLUSIVE" else 3


if __name__ == "__main__":
    sys.exit(main())

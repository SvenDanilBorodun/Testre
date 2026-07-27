#!/usr/bin/env python3
"""A12 - can a GOAL WRITE energise a LIMP arm? (edu6, POWERED, hands-off)

THE QUESTION
    LeRobot issue #3585 reports that after an OUT-OF-WINDOW ``Goal_Position``
    write the STS firmware sets ``Torque_Enable = 1`` BY ITSELF on the next PID
    tick, regardless of the host having torque off.

    Our driver issues exactly such a write on every torque-on.
    ``_seed_goal_from_present_locked`` writes the MEASURED tick clamped only
    into the REGISTER range [0, 4095] -- literally
    ``tick = max(0, min(TICKS_PER_REV - 1, ticks[sid]))`` -- and NOT into the
    servo's EEPROM Min/Max_Position_Limit window. Rig gate R9 measured a limp
    arm resting up to 107 ticks OUTSIDE that window under gravity (joint5
    worst; joint3 70-71, joint2 21-29). On such a joint that seed IS an
    out-of-window goal write, issued while torque is off.

WHY THIS IS THE FIRST THING AT THE BENCH
    Hand-guide is torque-off BY DESIGN and the student is physically holding
    the arm. "Torque off is safe" is the assumption the whole hand-guide
    feature rests on. If a goal write can energise a limp arm, that assumption
    is wrong -- and the driver's own torque-off + seed path would be the thing
    violating it. The pull-in is already budgeted for (TORQUE_ABORT_ERROR_TICKS)
    and the gentle SEED_SPEED_STEPS rides in the same packet, so it LOOKS
    bounded. "Looks bounded" is not a safety argument.

METHOD
    One joint. Hands off. Torque verified OFF by READING register 40 back.
    A stationary baseline with torque sampled throughout (the control). Then
    EXACTLY the driver's 7-byte seed block, and nothing else, on that one
    servo. Then ~3 s of fast polling of register 40 + Present_Position, with an
    automatic torque-off if anything moves.

THE GATES, AND WHY EACH ONE EXISTS
    G0  EEPROM windows must match ``fb.position_limit_window`` (+/-1 tick, the
        same check ``probe_bus`` hard-gates the plugged arm with). If they do
        not, this is not the arm the driver expects, the driver would refuse to
        boot it, and "the window" this test is defined against is unknown.
    G1  THE PRECONDITION THAT MAKES THE RESULT MEAN ANYTHING. If the joint
        rests INSIDE its EEPROM window, the seed is an IN-window write and the
        run proves NOTHING about #3585 -- it would be the same class of vacuous
        pass as the two "clean hold" wrap runs that had goal == present (see
        the bring-up doc, TRAPS: "a zero-error hold proves nothing"). So the
        tool REFUSES unless the reading is genuinely outside, prints by how
        many ticks, and calls a small excursion WEAK.
    G2  Torque must READ BACK as 0 on all seven servos before we start. A
        result about "did the write energise it" is meaningless if something
        was already energised.
    G3  Hands-off baseline: the joint must be stationary (drift and
        Present_Speed both zero) with torque reading 0 for the whole baseline,
        BEFORE the write. This is what makes any later motion attributable to
        the write rather than to the operator's hand or to a slow mechanical
        sag.
    G4  EVIDENCE gates, checked only against a NEGATIVE (they can never suppress
        a positive -- see decide_verdict): the target joint must have a TRIMMED
        EEPROM window at all (joint4/joint6 own the whole register, so G1 is
        unsatisfiable on them by construction); Goal_Position must read back as
        the seeded tick (SYNC_WRITE takes no reply, so a lost write is otherwise
        invisible and the run would watch an arm nothing was written to);
        register 40 must actually have been READ on the written joint often
        enough and without a long blind stretch; and the watch must not have
        been cut short by the auto-abort. Each of these produced a clean, FALSE
        "A12 NOT REPRODUCED" in verification.

WHAT THIS TOOL WRITES -- the complete list
    * ONE ``sync_write`` at REG_ACCELERATION: the driver's exact 7-byte block
      (accel, goal, Goal_Time, speed) on the selected servo -- or on all seven
      with --all-servos, which is byte-for-byte what the driver sends.
    * ``Torque_Enable = 0`` on all seven servos, on every exit path.
    It NEVER writes Torque_Enable = 1. It never writes EEPROM, never
    Homing_Offset, never limits, and never the value 128 (the vendor's
    "re-datum this position to 2048" command, which guard 6 refuses anyway --
    all writes here go through feetech_bus, so that rail is in force).

WHAT A NEGATIVE RESULT PROVES, AND WHAT IT DOES NOT
    A clean "A12 NOT REPRODUCED" licenses exactly this: on THIS arm, on THIS
    firmware (3.10), for THIS joint, at THIS excursion, a single out-of-window
    seed write did not energise a limp servo within the watch window. It does
    NOT license "goal writes can never energise a servo": a different joint, a
    larger excursion, a different firmware, or a repeated/continuous write
    stream are all untested. Repeat it on the worst joint available (joint5
    reaches the largest excursion on its own under gravity) and on at least one
    other before treating hand-guide's torque-off assumption as verified.

USAGE (bench PC, arm's 12 V ON, USB on COM5 -- no usbipd attach needed)
    python tools/edu6_bench/edu6_a12_seed_selfenable.py --list
    python tools/edu6_bench/edu6_a12_seed_selfenable.py --joint 5 --park
    python tools/edu6_bench/edu6_a12_seed_selfenable.py --joint 5
    python tools/edu6_bench/edu6_a12_seed_selfenable.py --joint 5 --all-servos

    --park is a READ-ONLY coaching loop: it prints the live excursion so you
    can hand the joint out of its window without guessing. It needs no
    direction words at all -- you watch the number, so nothing can invert.

EXIT CODES
    0  A12 NOT REPRODUCED    2  INCONCLUSIVE / refused precondition
    3  A12 CONFIRMED         1  tool or bus error

ABORT
    Cut the 12 V. That is faster and more certain than any software path. The
    tool also cuts torque automatically on motion, on Ctrl-C, on an exception
    and at process exit.
"""

from __future__ import annotations

import argparse
import ast
import atexit
import os
import sys
import time
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve()
REPO = _HERE.parents[2]
DRIVER = REPO / "robotis_ai_setup" / "docker" / "open_manipulator" / "edu6_arm_node.py"
if not DRIVER.is_file():                      # pragma: no cover - layout guard
    raise SystemExit(
        f"[FAIL] driver not found at {DRIVER}\n"
        "       This tool must live in tools/edu6_bench/ inside the repo, so "
        "that it can read the driver's constants instead of duplicating them.")
sys.path.insert(0, str(DRIVER.parent))

import feetech_bus as fb  # noqa: E402  (the SAME clean-room bus the driver uses)


# -- driver constants, AST-extracted (never retyped) --------------------------
# Same approach as edu6_r9_collapse.py / edu6_wrap_test.py: the driver imports
# rclpy, so it cannot be imported here, but every number this tool needs is a
# module-level literal. Extracting them means the tool CANNOT drift from the
# driver -- a changed SEED_SPEED_STEPS or a changed window changes this test.
def _lit(name):
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
# JOINT_SIGNS itself is a call result (env-parsed), not a literal -- take the
# baked defaults, exactly as the sibling tools do. R6 cleared all seven at +1
# and a flip forces a re-provision, so a mismatch would surface as a G0 window
# failure rather than as a silently wrong window. See the [NOTE] in main().
SIGNS = _lit("_DEFAULT_SIGNS")
TICKS = _lit("TICKS_PER_REV")
CENTER = _lit("CENTER_TICK")
SEED_SPEED = _lit("SEED_SPEED_STEPS")
WRITE_ACCEL = _lit("WRITE_ACCELERATION")
BOOT_BAND = _lit("_DEFAULT_BOOT_POSITION_TOLERANCE_TICKS")
EDGE_MARGIN = _lit("_DEFAULT_EDGE_REFUSE_MARGIN_TICKS")
DEG_PER_TICK = 360.0 / TICKS

# Mirrors the driver's own _TORQUE_OFF_PAYLOAD (a dict comprehension there, so
# not AST-extractable) -- built ONCE at import so the abort path allocates
# nothing between "the arm is moving" and the de-energising write, the same
# discipline as _verify_after_energise_locked.
_TORQUE_OFF_PAYLOAD = {sid: b"\x00" for sid in SERVO_IDS}

# A reading 1-2 ticks outside the window is real but WEAK evidence: the encoder
# dithers by about that much (the same 1-2 ticks the driver's 40-tick edge
# margin is sized around), so at that size the firmware may well have evaluated
# the write from an IN-window reading and the run would be near-vacuous. 10
# ticks (0.88 deg) is ~5x the dither and trivially available -- R9 measured 107
# on joint5 and 70 on joint3 with no effort at all.
WEAK_EXCURSION_TICKS = 10

VERDICT_CONFIRMED = "A12 CONFIRMED"
VERDICT_NOT_REPRODUCED = "A12 NOT REPRODUCED"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"


# -- pure helpers (unit-testable without a bus) -------------------------------
def excursion(tick: int, lo: int, hi: int) -> int:
    """Ticks OUTSIDE the window (0 when inside) -- the quantity that decides
    whether the seed write is an out-of-window write at all."""
    if tick < lo:
        return lo - tick
    if tick > hi:
        return tick - hi
    return 0


def short_way(delta: int) -> int:
    """A tick difference interpreted the SHORT way round the 4096-tick circle,
    so a reading that straddles the 4095/0 map edge does not read as a
    4000-tick jump."""
    d = int(delta) % TICKS
    if d > TICKS // 2:
        d -= TICKS
    return d


def edge_distance(tick: int) -> int:
    """Distance to the nearer tick-0/4095 position-map edge."""
    return min(int(tick), TICKS - 1 - int(tick))


def designed_window(index: int) -> tuple[int, int]:
    """The EEPROM window the provisioning tool wrote and probe_bus verifies."""
    return fb.position_limit_window(*JOINT_LIMITS_RAD[index], SIGNS[index])


def tick_to_deg(index: int, tick: int) -> float:
    return (int(tick) - CENTER) * DEG_PER_TICK * SIGNS[index]


def seed_payload(present_tick: int) -> bytes:
    """EXACTLY the driver's seed block -- byte for byte, including the register
    clamp that is the whole point of A12.

    ``_seed_goal_from_present_locked`` writes 7 contiguous bytes at
    REG_ACCELERATION: acceleration, Goal_Position, Goal_Time (a documented STS
    no-op), Goal_Speed. The goal is clamped ONLY into [0, TICKS-1] -- NOT into
    the EEPROM window. Sanitising it here (clamping into the window) would
    destroy the experiment: an in-window write is precisely the case #3585 says
    is harmless."""
    tick = max(0, min(TICKS - 1, int(present_tick)))
    return (bytes([WRITE_ACCEL])
            + fb.le16(tick)
            + fb.le16(0)                 # Goal_Time: documented no-op
            + fb.le16(SEED_SPEED))       # gentle pull-in, same packet


def decide_verdict(*, preconditions_ok: bool, baseline_ok: bool,
                   samples_ok: bool, torque_on_target: bool,
                   torque_on_other: bool,
                   target_dev: int, other_dev: int, move_ticks: int,
                   weak_excursion: bool,
                   samples_reason: str = "") -> tuple[str, str]:
    """The whole verdict logic, pure and total.

    Order matters, and the two KINDS of gate are deliberately not interchangeable.

    ATTRIBUTION gates (precondition, baseline) outrank everything: if the write
    was in-window, or the arm was already moving, then neither a positive nor a
    negative means anything, because nothing observed can be attributed to the
    write.

    The OBSERVATION-SUFFICIENCY gate (``samples_ok``) is different: it only ever
    blocks a NEGATIVE. "Nothing happened" is a claim about a stretch of time we
    must actually have watched; "it energised" is not. This ordering was got
    wrong once and it inverted the tool's whole purpose: the auto-abort ends the
    watch on the very sample that detects the defect, so gating the positive on
    sample count reported a REAL self-enable as "nothing was learned". A guard
    that can suppress a true positive is worse than no guard.

    Between those, a register reading of Torque_Enable = 1 we did not write is
    decisive on its own -- BUT ONLY ON A SERVO THAT WAS WRITTEN. A12 is a claim
    about a GOAL WRITE energising a servo; an UNWRITTEN servo coming up is not
    that claim, it is the same "different and larger finding" the G3 baseline
    gate already refuses to call A12, and reporting it as A12 CONFIRMED would
    condemn hand-guide off evidence that never touched the hypothesis. (Under
    --all-servos every servo IS written, so the caller passes them all as
    `torque_on_target` and this distinction collapses, correctly.) Motion is
    decisive too, but only when the REST of the arm stayed still -- if unwritten
    servos moved as well, something shook the bench. Finally, a negative on a
    weak excursion is INCONCLUSIVE rather than a pass, because "we barely made
    the write out-of-window and nothing happened" is close to vacuous, and a
    vacuous pass would falsely clear the hand-guide safety assumption."""
    if not (preconditions_ok and baseline_ok):
        return VERDICT_INCONCLUSIVE, (
            "an attribution gate failed (precondition or baseline), so nothing "
            "observed can be attributed to the seed write")
    if torque_on_target:
        return VERDICT_CONFIRMED, (
            "Torque_Enable read back as non-zero on a WRITTEN servo although "
            "this tool never wrote 1 to register 40")
    if target_dev >= move_ticks and other_dev >= move_ticks:
        return VERDICT_INCONCLUSIVE, (
            f"the target moved {target_dev} ticks but so did an UNWRITTEN "
            f"servo ({other_dev} ticks) -- the whole arm was disturbed, so the "
            f"motion is not attributable to the write")
    if target_dev >= move_ticks:
        return VERDICT_CONFIRMED, (
            f"the joint moved {target_dev} ticks after the seed write while it "
            f"was supposed to be limp (Torque_Enable never read back as 1, so "
            f"either it flipped between samples or the servo drove without it)")
    if torque_on_other:
        return VERDICT_INCONCLUSIVE, (
            "Torque_Enable read back as non-zero on a servo this run never "
            "WROTE to. That is not A12 -- nothing was written to it, so no goal "
            "write can have caused it -- but it IS a bigger finding than A12: "
            "investigate it before repeating, and treat the arm as possibly LIVE")
    if not samples_ok:
        return VERDICT_INCONCLUSIVE, (
            samples_reason or
            "nothing was observed, but the arm was not watched continuously "
            "enough for that to be evidence -- it could have energised and "
            "settled again while the bus was silent")
    if weak_excursion:
        return VERDICT_INCONCLUSIVE, (
            "nothing happened, but the excursion was too small for that to "
            "mean much -- the firmware may have evaluated an in-window write")
    return VERDICT_NOT_REPRODUCED, (
        "torque stayed 0 and the joint did not move for the whole watch window")


NOTHING_LEARNED = (
    "  Nothing was learned about A12. Fix the gate named above and repeat -- an\n"
    "  inconclusive run must never be recorded as a pass, because a false pass\n"
    "  here would clear a safety assumption that was never actually tested.")


def refuse(reason: str, code: int = 2) -> int:
    """Print an explicit INCONCLUSIVE verdict and return an exit code.

    EVERY exit path after the port opens goes through here or through the final
    report, so a bench run can never end without a stated verdict. A run that
    stops half way with only a [STOP] line is exactly how "we tried it and
    nothing happened" gets written down as a pass."""
    print()
    print(f"  VERDICT: {VERDICT_INCONCLUSIVE}")
    print(f"           {reason}")
    print()
    print(NOTHING_LEARNED)
    return code


# -- bus helpers --------------------------------------------------------------
_SAFE = {"bus": None, "closed": False, "armed": False}


def torque_off_all(note: str = "") -> bool:
    """Write Torque_Enable = 0 to every servo. Called on the abort path BEFORE
    any logging, in the `finally`, and at atexit. Idempotent and harmless on an
    already-limp arm; a duplicate call is deliberate, because the arm may have
    energised itself between two of them -- which is the whole hypothesis.

    It no-ops until ``armed``, which is set immediately before the seed write.
    Two reasons: --list and --park advertise themselves as READ-ONLY and must
    stay that way, and the G2 refusal path (torque already on) must NOT silently
    de-energise an arm that another process -- the container, another bench tool
    -- is deliberately driving. Before the write there is nothing to turn off
    anyway: G2 has already proved every servo reads 0."""
    bus = _SAFE["bus"]
    if bus is None or _SAFE["closed"] or not _SAFE["armed"]:
        return True
    try:
        bus.sync_write(fb.REG_TORQUE_ENABLE, _TORQUE_OFF_PAYLOAD)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] torque-off write FAILED ({e}) -- CUT THE 12 V NOW.")
        return False
    print(f"[SAFE] Torque_Enable = 0 written to all {len(SERVO_IDS)} servos"
          f"{note}.")
    return True


atexit.register(torque_off_all, " (at exit)")


def read_torque_each(bus) -> dict:
    """{sid: value or None} from seven individual READs -- one bad servo cannot
    hide the other six. Used for the gate; the watch loop uses sync_read."""
    out = {}
    for sid in SERVO_IDS:
        try:
            out[sid] = bus.read(sid, fb.REG_TORQUE_ENABLE, 1)[1][0]
        except fb.FeetechBusError:
            out[sid] = None
    return out


def read_torque_sync(bus) -> dict:
    """{sid: value} for whoever replied -- one transaction."""
    out = {}
    for sid, (_err, data) in bus.sync_read(
            fb.REG_TORQUE_ENABLE, 1, list(SERVO_IDS)).items():
        out[sid] = data[0]
    return out


def read_block(bus) -> dict:
    """{sid: (tick, speed, load, err)} from the driver's own 6-byte read."""
    out = {}
    for sid, (err, d) in bus.sync_read(
            fb.REG_PRESENT_POSITION, 6, list(SERVO_IDS)).items():
        out[sid] = (
            fb.decode_sign_magnitude(fb.from_le16(d[0], d[1]), 15),
            fb.decode_sign_magnitude(fb.from_le16(d[2], d[3]), 15),
            fb.decode_sign_magnitude(fb.from_le16(d[4], d[5]), 10),
            err,
        )
    return out


def read_eeprom_windows(bus) -> dict:
    """{sid: (lo, hi) or None} straight from Min/Max_Position_Limit.

    The firmware clamps against THESE, not against our idea of them, so the
    out-of-window test that A12 is about has to be defined against the live
    registers. The designed values are then cross-checked on top (gate G0)."""
    out = {}
    for sid in SERVO_IDS:
        try:
            lo = bus.read_u16(sid, fb.REG_MIN_POSITION_LIMIT)
            hi = bus.read_u16(sid, fb.REG_MAX_POSITION_LIMIT)
            out[sid] = (lo, hi)
        except fb.FeetechBusError:
            out[sid] = None
    return out


def main() -> int:  # noqa: C901 - a bench script; the flow IS the document
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default=os.environ.get("EDU6_PORT", "COM5"))
    ap.add_argument("--joint", type=int, default=5,
                    help="servo id under test (default 5: joint5 reaches the "
                         "largest out-of-window excursion on its own under "
                         "gravity -- R9 measured 107 ticks)")
    ap.add_argument("--list", action="store_true",
                    help="read all seven and exit (read-only)")
    ap.add_argument("--park", action="store_true",
                    help="READ-ONLY live excursion readout while you hand the "
                         "joint out of its window. Ctrl-C when it is out.")
    ap.add_argument("--all-servos", action="store_true",
                    help="seed all seven exactly as the driver does. Default "
                         "is the single joint under test -- see the comment at "
                         "the write site.")
    ap.add_argument("--watch-s", type=float, default=3.0,
                    help="seconds to poll register 40 + position after the write")
    ap.add_argument("--hz", type=float, default=50.0,
                    help="polling rate during the watch window")
    ap.add_argument("--baseline-s", type=float, default=2.0,
                    help="hands-off stationary check BEFORE the write")
    ap.add_argument("--abort-ticks", type=int, default=30,
                    help="cut torque on all servos once any of them has moved "
                         "this far from its baseline (30 ticks = 2.64 deg)")
    ap.add_argument("--move-ticks", type=int, default=4,
                    help="motion this size counts as evidence (encoder dither "
                         "is 1-2 ticks)")
    ap.add_argument("--weak-ticks", type=int, default=WEAK_EXCURSION_TICKS,
                    help="an excursion below this is reported as WEAK")
    ap.add_argument("--print-every", type=int, default=5,
                    help="print every Nth watch sample (interesting samples "
                         "are always printed)")
    ap.add_argument("--yes", action="store_true",
                    help="skip the 'go' confirmation before the write")
    args = ap.parse_args()

    if args.joint not in SERVO_IDS:
        print(f"[FAIL] --joint must be one of {list(SERVO_IDS)}")
        return 1
    idx = SERVO_IDS.index(args.joint)
    name = JOINT_NAMES[idx]

    if os.environ.get("EDUBOTICS_EDU6_JOINT_SIGNS", "").strip():
        print("[NOTE] EDUBOTICS_EDU6_JOINT_SIGNS is set in this shell. This "
              "tool uses the driver's baked _DEFAULT_SIGNS for the angle "
              "display and for the designed-window cross-check, so a genuinely "
              "flipped sign will surface below as a G0 window mismatch rather "
              "than as a silently wrong window.")

    print(f"[A12] seed-write self-enable test on {args.port} "
          f"(driver constants read from {DRIVER.name})")
    try:
        bus = fb.FeetechBus(args.port)
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] cannot open {args.port}: {e}")
        print("       'Zugriff verweigert' / PermissionError almost always "
              "means ANOTHER process holds the port -- a still-running probe, "
              "or a container that has the device usbipd-attached. Close it "
              "and retry; only one process can own the bus.")
        return refuse("the serial port could not be opened", 1)
    _SAFE["bus"] = bus

    wrote_seed = False
    try:
        alive = [s for s in SERVO_IDS if bus.ping(s)]
        if not alive:
            print("[FAIL] no servo answered a ping.")
            print("       The USB adapter alone does NOT power the servos -- "
                  "check the arm's 12 V supply is plugged in AND switched on.")
            return refuse("no servo answered a ping", 1)
        if len(alive) != len(SERVO_IDS):
            missing = [s for s in SERVO_IDS if s not in alive]
            print(f"[FAIL] only {alive} answered; missing {missing}. This test "
                  f"needs the whole bus: the unwritten servos are the control "
                  f"that says the arm was not disturbed.")
            return refuse(f"servo(s) {missing} did not answer")

        state = read_block(bus)
        windows = read_eeprom_windows(bus)
        torque = read_torque_each(bus)

        print()
        print(f"{'joint':<16}{'tick':>6}{'deg':>9}   "
              f"{'EEPROM window':<15}{'designed':<15}{'out':>5}  torque")
        for i, sid in enumerate(SERVO_IDS):
            st = state.get(sid)
            win = windows.get(sid)
            if st is None or win is None:
                print(f"  {JOINT_NAMES[i]:<14} NO REPLY")
                continue
            tick = st[0]
            lo, hi = win
            dlo, dhi = designed_window(i)
            out = excursion(tick, lo, hi)
            mark = "  <== UNDER TEST" if sid == args.joint else ""
            print(f"  {JOINT_NAMES[i]:<14}{tick:>6}{tick_to_deg(i, tick):>+9.2f}   "
                  f"[{lo:>4},{hi:>4}]      [{dlo:>4},{dhi:>4}]     {out:>4}  "
                  f"{torque.get(sid)}{mark}")
        if args.list:
            return 0

        # -- G0: the arm must be the provisioned one --------------------------
        bad_windows = []
        for i, sid in enumerate(SERVO_IDS):
            win = windows.get(sid)
            if win is None:
                bad_windows.append((sid, None, designed_window(i)))
                continue
            dlo, dhi = designed_window(i)
            if abs(win[0] - dlo) > 1 or abs(win[1] - dhi) > 1:
                bad_windows.append((sid, win, (dlo, dhi)))
        if bad_windows:
            print()
            print("[STOP] G0 FAILED -- the EEPROM position windows do not match "
                  "the designed ones:")
            for sid, got, want in bad_windows:
                print(f"       servo {sid}: got {got}, designed {want}")
            print("       probe_bus hard-gates on exactly this (+/-1 tick) and "
                  "would refuse to boot this arm, so 'outside the window' is "
                  "not a defined quantity here. Re-run "
                  "tools/edu6_provision.py on the jigged arm, or check "
                  "EDUBOTICS_EDU6_JOINT_SIGNS.")
            return refuse("G0: the EEPROM windows are not the designed ones, "
                          "so 'outside the window' is undefined on this arm")

        # -- G2: torque must READ BACK as 0 everywhere ------------------------
        unknown = [s for s, v in torque.items() if v is None]
        on = [s for s, v in torque.items() if v]
        if unknown:
            print(f"\n[STOP] G2 FAILED -- could not read Torque_Enable on "
                  f"servo(s) {unknown}. Not starting a powered test on an arm "
                  f"whose torque state is unknown.")
            return refuse(f"G2: Torque_Enable could not be read on {unknown}")
        if on:
            print(f"\n[STOP] G2 FAILED -- Torque_Enable reads 1 on servo(s) "
                  f"{on}. A12 asks whether a goal write energises a LIMP "
                  f"servo; an already-energised one makes the answer "
                  f"meaningless. Stop whatever is driving the arm (the "
                  f"container, another bench tool), or power-cycle, and retry.")
            return refuse(f"G2: servo(s) {on} were already energised, so "
                          f"'did the write energise it' has no meaning")
        print(f"\n[OK] G0 windows match the design; G2 torque reads 0 on all "
              f"{len(SERVO_IDS)} servos -- the arm is limp.")

        # -- can register 40 actually be READ during the watch? ---------------
        # NOTHING in this tree has ever sync_read register 40 -- the driver only
        # WRITES it (two sync_writes, no read-back anywhere) -- so that
        # transaction is UNPROVEN on this firmware. A silent one raises nothing:
        # sync_read simply returns {}, every tqs.get(sid) is None, `if v:` is
        # False, and the run then reports "torque stayed 0 on every sample"
        # having never once looked. That is a FALSE clearance of the hand-guide
        # assumption, so probe it ONCE here and fall back to the seven individual
        # READs G2 has just proved work on this arm. Read-only, so --list and
        # --park remain read-only.
        torque_reader = read_torque_sync
        try:
            probe = read_torque_sync(bus)
        except Exception:  # noqa: BLE001
            probe = {}
        if sorted(probe) != sorted(SERVO_IDS):
            torque_reader = read_torque_each
            print(f"[NOTE] sync_read of register 40 answered for {sorted(probe)}, "
                  f"not all {len(SERVO_IDS)} servos. Falling back to individual "
                  f"READs for the torque watch -- slower, same data, and it is "
                  f"the path G2 just used successfully.")

        lo_t, hi_t = windows[args.joint]

        # -- G1 must be SATISFIABLE at all on this joint -----------------------
        # joint4 and joint6 keep the FULL register as their window [0, 4095], so
        # excursion() returns 0 for every one of the 4096 readings that exist:
        # G1 can never pass, no matter what the operator does to the arm. Without
        # this the tool sends them into the G1 refusal, whose remedy ("get the
        # reading outside [0, 4095]") is impossible to follow -- the same
        # definitional-zero trap R9 hit on exactly these two joints (bring-up
        # doc, TRAPS: "do not cite a definitional zero as evidence"). Say it up
        # front instead of after the arm has been manhandled.
        if (lo_t, hi_t) == (0, TICKS - 1):
            print()
            print(f"[STOP] {name} (servo {args.joint}) has the WHOLE register as "
                  f"its EEPROM window [{lo_t}, {hi_t}], so no reading can ever "
                  f"be OUTSIDE it and the G1 precondition is unsatisfiable on "
                  f"this joint. A12 needs an OUT-OF-WINDOW goal write; this "
                  f"joint cannot produce one.")
            print("       Testable joints (a trimmed window exists): "
                  + ", ".join(
                      f"{JOINT_NAMES[i]}={sid}" for i, sid in enumerate(SERVO_IDS)
                      if designed_window(i) != (0, TICKS - 1)))
            return refuse(f"servo {args.joint}'s window is the whole register, "
                          f"so an out-of-window seed write is impossible on it")

        # -- --park: read-only coaching (no direction words; you watch a number)
        if args.park:
            print()
            print(f"[PARK] Move {name} by hand until the tick reading leaves "
                  f"its window [{lo_t}, {hi_t}]. Nothing is written; torque "
                  f"stays off. Ctrl-C when the excursion is comfortably above "
                  f"{args.weak_ticks}.")
            if args.joint == 5:
                print("       joint5 does it on its own: raise the wrist, let "
                      "go, and let gravity take it to its hard stop (R9 "
                      "measured 107 ticks out that way).")
            try:
                while True:
                    b = read_block(bus)
                    st = b.get(args.joint)
                    if st is None:
                        print("   no reply")
                        time.sleep(0.2)
                        continue
                    tick = st[0]
                    out = excursion(tick, lo_t, hi_t)
                    if out == 0:
                        need_lo = tick - lo_t + 1
                        need_hi = hi_t - tick + 1
                        hint = (f"INSIDE -- {min(need_lo, need_hi)} ticks to "
                                f"the nearer limit "
                                f"({'below ' + str(lo_t) if need_lo <= need_hi else 'above ' + str(hi_t)})")
                    elif out < args.weak_ticks:
                        hint = f"OUT by {out} -- WEAK, keep going"
                    else:
                        hint = f"OUT by {out} ({out * DEG_PER_TICK:.2f} deg) -- GOOD"
                    print(f"   tick {tick:>4} {tick_to_deg(idx, tick):>+8.2f} deg  "
                          f"window [{lo_t},{hi_t}]  {hint}")
                    time.sleep(0.25)
            except KeyboardInterrupt:
                print(f"\n[PARKED] now run:  python {_HERE.name} "
                      f"--joint {args.joint}")
            return 0

        # -- operator setup, THEN read ----------------------------------------
        # Deliberately in this order: a position read taken BEFORE the prompt is
        # stale by however long the operator takes, and a stale pre-prompt read
        # has already produced one false alarm on this arm (bring-up doc,
        # TRAPS: "read the position AFTER the operator prompt, not before").
        print()
        print("=" * 78)
        print("SET UP THE JOINT")
        print("=" * 78)
        print(f"  1. Move {name} (servo {args.joint}) by hand until its tick "
              f"reading is OUTSIDE [{lo_t}, {hi_t}].")
        if args.joint == 5:
            print("     joint5 does this on its own: raise the wrist and let "
                  "go -- gravity takes it past its stop.")
        print("     Use --park first if you want a live readout while you do "
              "it.")
        print("  2. TAKE YOUR HANDS OFF THE ARM COMPLETELY and let it settle.")
        print("  3. Keep them off for the rest of the run: the baseline check "
              "below is what makes any later motion attributable to the write.")
        print("  Nothing has been written yet, and nothing will be until you "
              "confirm.")
        try:
            input("  Press ENTER when your hands are off > ")
        except EOFError:
            print("[ABORT] no tty for the setup prompt. Nothing was written.")
            return refuse("the operator prompt could not be shown")

        state = read_block(bus)
        st = state.get(args.joint)
        if st is None:
            print(f"[FAIL] servo {args.joint} did not reply after the prompt.")
            return refuse(f"servo {args.joint} stopped replying", 1)
        tick_now = st[0]
        out = excursion(tick_now, lo_t, hi_t)
        edge = edge_distance(tick_now)
        print()
        print(f"[READ]  {name} tick {tick_now} ({tick_to_deg(idx, tick_now):+.2f} "
              f"deg), EEPROM window [{lo_t}, {hi_t}]")
        print(f"[G1]    OUTSIDE the window by {out} ticks "
              f"({out * DEG_PER_TICK:.2f} deg)")

        # -- G1: the precondition that makes the result non-vacuous -----------
        weak = False
        if out == 0:
            print()
            print("[STOP] G1 FAILED -- the joint is INSIDE its EEPROM window, "
                  "so the seed would be an IN-window goal write and this run "
                  "would prove NOTHING about A12 (#3585 is specifically about "
                  "OUT-of-window writes). This is the same class of vacuous "
                  "pass as a wrap test with goal == present.")
            print(f"       Get the reading outside [{lo_t}, {hi_t}] and retry; "
                  f"'--joint {args.joint} --park' coaches you there.")
            if args.joint != 5:
                print("       joint5 is the easy one: it rests ~107 ticks "
                      "outside its window under gravity all by itself (R9).")
            return refuse("G1: the joint rested INSIDE its EEPROM window, so "
                          "the seed would have been an in-window write")
        if out > BOOT_BAND:
            print()
            print(f"[STOP] G1 FAILED THE OTHER WAY -- {out} ticks outside is "
                  f"more than the driver's boot plausibility band "
                  f"({BOOT_BAND}). That signature means the joint was turned "
                  f"past +/-180 deg while limp and the reading has WRAPPED, "
                  f"not that it sagged. The driver would refuse to boot this "
                  f"arm at all, so a powered experiment here describes a state "
                  f"the product never runs in -- and a self-enable from a "
                  f"wrapped position would drive the LONG way round rather "
                  f"than a bounded pull-in.")
            print("       Nudge the joint back toward the middle of its travel "
                  "and retry.")
            return refuse(f"G1: {out} ticks outside is a WRAP signature, not a "
                          f"rest pose -- the driver would refuse this arm")
        if out < args.weak_ticks:
            weak = True
            print(f"[WARN]  WEAK excursion: {out} < {args.weak_ticks} ticks. "
                  f"The encoder dithers 1-2 ticks, so at this size the "
                  f"firmware may have evaluated an in-window reading. A "
                  f"NEGATIVE result from this run will be reported as "
                  f"INCONCLUSIVE, not as a pass.")
        if edge < EDGE_MARGIN:
            print(f"[WARN]  {name} also sits {edge} ticks from the tick-0/4095 "
                  f"map edge (inside the driver's {EDGE_MARGIN}-tick torque-on "
                  f"edge refusal band). If A12 is real HERE, the servo may "
                  f"drive the LONG way round instead of a bounded pull-in. The "
                  f"auto-abort below covers both directions, but expect a "
                  f"bigger motion.")

        # -- the plan, then informed consent ----------------------------------
        pay = seed_payload(tick_now)
        targets = list(SERVO_IDS) if args.all_servos else [args.joint]
        expect_pull = min(out, args.abort_ticks)
        print()
        print("[PLAN]  ONE sync_write at REG_ACCELERATION "
              f"({fb.REG_ACCELERATION}), 7 bytes, servo(s) {targets}:")
        print(f"          {pay.hex(' ')}")
        print(f"          = acceleration {WRITE_ACCEL}, Goal_Position "
              f"{max(0, min(TICKS - 1, tick_now))} (the measured tick, clamped "
              f"ONLY into [0,{TICKS - 1}] -- exactly as the driver does, i.e. "
              f"{out} ticks outside the EEPROM window), Goal_Time 0, "
              f"Goal_Speed {SEED_SPEED}")
        print("        Torque_Enable is NOT written. Nothing else is written.")
        print(f"        Then {args.watch_s:.1f} s of polling at ~{args.hz:.0f} "
              f"Hz: register 40 + Present_Position on all seven.")
        print(f"        AUTO-ABORT (torque 0 to all seven, immediately) if any "
              f"servo moves {args.abort_ticks} ticks "
              f"({args.abort_ticks * DEG_PER_TICK:.2f} deg) from its baseline, "
              f"or if register 40 ever reads non-zero.")
        print(f"        If A12 is real, expect a pull-in toward the window edge "
              f"of up to {out} ticks at {SEED_SPEED} steps/s "
              f"(~{SEED_SPEED * DEG_PER_TICK:.0f} deg/s); the abort cuts it at "
              f"{expect_pull}.")
        print("        Hardware backstop: Max_Torque / Protection_Current in "
              "EEPROM (untouched). Abort = cut the 12 V.")
        if not args.yes:
            try:
                if input("        type 'go' to issue the write: ").strip().lower() != "go":
                    print("[ABORT] nothing was written.")
                    return refuse("the operator declined at the confirmation")
            except EOFError:
                print("[ABORT] no tty for confirmation -- pass --yes "
                      "deliberately.")
                return refuse("no tty for the confirmation prompt")

        # -- G3: hands-off baseline, torque sampled throughout (the CONTROL) --
        print(f"[BASE]  HANDS OFF -- {args.baseline_s:.1f} s stationary check, "
              f"torque still off...")
        b_end = time.monotonic() + args.baseline_s
        b_ticks: dict[int, list[int]] = {s: [] for s in SERVO_IDS}
        b_speed_peak = 0
        b_torque_on: list[tuple[float, int, int]] = []
        b_t0 = time.monotonic()
        while time.monotonic() < b_end:
            blk = read_block(bus)
            tqs = torque_reader(bus)
            for sid, v in tqs.items():
                if v:
                    b_torque_on.append((time.monotonic() - b_t0, sid, v))
            for sid, vals in blk.items():
                if sid not in b_ticks:
                    continue          # a reply from an id we never asked about
                b_ticks[sid].append(vals[0])
                if sid == args.joint:
                    b_speed_peak = max(b_speed_peak, abs(vals[1]))
            time.sleep(0.05)

        if b_torque_on:
            t, sid, v = b_torque_on[0]
            print(f"[STOP] G3 FAILED -- Torque_Enable read {v} on servo {sid} "
                  f"{t:.2f} s into the baseline, BEFORE anything was written. "
                  f"That is not attributable to a goal write; it is a "
                  f"different and larger finding. Investigate before "
                  f"continuing -- and note the arm may be energised right now.")
            return refuse(f"G3: servo {sid} energised BEFORE any write -- not "
                          f"attributable to a goal write, and a bigger finding")
        if not b_ticks[args.joint]:
            print("[STOP] G3 FAILED -- no baseline samples from servo "
                  f"{args.joint}.")
            return refuse(f"G3: no baseline samples from servo {args.joint}")
        b_span = {s: (max(v) - min(v)) for s, v in b_ticks.items() if v}
        worst_span = max(b_span.values())
        print(f"[BASE]  {len(b_ticks[args.joint])} samples; worst drift "
              f"{worst_span} ticks across the arm, peak |speed| "
              f"{b_speed_peak} on {name}")
        if worst_span > 3 or b_speed_peak > 0:
            noisy = [s for s, v in b_span.items() if v > 3]
            print(f"[STOP] G3 FAILED -- the arm is MOVING before the write "
                  f"(servo(s) {noisy or [args.joint]}, peak |speed| "
                  f"{b_speed_peak}). Nothing was written. Take your hands off "
                  f"completely, let it settle, and re-run -- otherwise any "
                  f"later motion cannot be attributed to the write.")
            return refuse("G3: the arm was already moving, so later motion "
                          "could not have been attributed to the write")
        base = {s: v[-1] for s, v in b_ticks.items() if v}
        no_base = [s for s in SERVO_IDS if s not in base]
        if no_base:
            # Load-bearing, not tidiness: the seed writes each servo its OWN
            # measured tick, so a servo with no baseline has no tick to write.
            # A default of any kind here would COMMAND a real move (a fallback
            # of 0 would command tick 0, i.e. -180 deg) the moment anything
            # energised it. And the unwritten servos are the control for "was
            # the arm disturbed", which needs all of them.
            print(f"[STOP] G3 FAILED -- servo(s) {no_base} produced no baseline "
                  f"position. Nothing was written. Check the daisy chain and "
                  f"the 12 V, then re-run.")
            return refuse(f"G3: servo(s) {no_base} gave no baseline position")

        # -- THE WRITE --------------------------------------------------------
        # Default is the single joint under test, NOT all seven. The driver
        # seeds all seven in one packet, but for the EXPERIMENT one servo is
        # strictly better: if torque appears or something moves there is no
        # ambiguity about which write caused it, and the six unwritten servos
        # become a control for "did anything shake the bench". --all-servos
        # reproduces the driver's exact packet once the single-joint answer is
        # known.
        payload = {sid: (pay if sid == args.joint else seed_payload(base[sid]))
                   for sid in targets}
        t_write = time.monotonic()
        _SAFE["armed"] = True          # arm the safety net BEFORE the write
        bus.sync_write(fb.REG_ACCELERATION, payload)
        wrote_seed = True
        # DID IT LAND? SYNC_WRITE is a broadcast and takes NO reply, so a packet
        # lost on the wire is invisible -- and then the run watches an arm that
        # was never written and reports A12 NOT REPRODUCED. `edu6_wrap_test.py`
        # already reads REG_GOAL_POSITION back for exactly this reason; not doing
        # it here was the one house-standard step this tool dropped.
        # It is recorded, NOT refused: the read happens after the write, so if
        # #3585 is real the servo may ALREADY be energising and bailing out now
        # would throw away the finding. It only ever blocks a NEGATIVE (below).
        # Either stored value is accepted -- #3585 reports register 42 reads back
        # the RAW written tick, but whether THIS firmware stores raw or the
        # EEPROM-clamped tick has never been measured on this arm, and a wrong
        # guess would turn every healthy run INCONCLUSIVE.
        seed_landed: bool | None = None
        try:
            _raw = max(0, min(TICKS - 1, tick_now))
            _clamped = max(lo_t, min(hi_t, _raw))
            _rb = bus.read_u16(args.joint, fb.REG_GOAL_POSITION)
            seed_landed = min(abs(_rb - _raw), abs(_rb - _clamped)) <= 2
            if not seed_landed:
                print(f"[WARN] Goal_Position reads back {_rb}, not {_raw} "
                      f"(nor the window-clamped {_clamped}) -- the seed "
                      f"sync_write may never have landed.")
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] could not read Goal_Position back ({e}) -- cannot "
                  f"confirm the seed write landed.")
        print(f"[WRITE] seed issued to servo(s) {targets}. Watching "
              f"{args.watch_s:.1f} s -- HANDS OFF.")

        # -- watch ------------------------------------------------------------
        period = 1.0 / max(args.hz, 1.0)
        end = t_write + args.watch_s
        csv_rows = ["t_s,sid,torque,tick,speed,load,err"]
        n_samples = 0
        n_target_replies = 0
        # TORQUE COVERAGE, tracked separately from position coverage and by the
        # same rule. A negative claims "Torque_Enable stayed 0 for the whole
        # watch"; that is only evidence for the stretches in which register 40
        # was actually OBSERVED on the written joint. Counting position replies
        # cannot stand in: the two are different transactions and a silent
        # register-40 read leaves position coverage perfect.
        n_torque_reads = 0
        prev_torque_seen = t_write
        max_torque_gap = 0.0
        torque_first: tuple[float, int, int] | None = None
        # Tracked SEPARATELY from torque_first, not derived from it: if an
        # unwritten servo glitches hot before the target does, torque_first names
        # the glitch and a derived test would report the real A12 hit as "not
        # attributable" -- suppressing exactly the true positive this tool exists
        # to catch.
        torque_first_target: tuple[float, int, int] | None = None
        max_dev = {s: 0 for s in SERVO_IDS}
        errs = set()
        aborted = ""
        # WATCH COVERAGE. "Nothing happened" is only evidence for the stretch we
        # were actually LOOKING at: if the bus goes quiet for a second, the arm
        # could energise and pull in unobserved, and a naive sample count still
        # reports a clean negative off the samples taken before it went blind.
        # So the largest gap between consecutive OBSERVED samples is tracked and
        # gates the verdict. Rate-independent on purpose -- it asks "how long
        # were we blind", not "how many packets did we get".
        prev_good = t_write
        max_gap = 0.0
        n_blind = 0
        while time.monotonic() < end:
            t_rel = time.monotonic() - t_write
            try:
                tqs = torque_reader(bus)
                blk = read_block(bus)
            except Exception as e:  # noqa: BLE001
                n_blind += 1
                print(f"   [WARN] bus read failed: {e}")
                time.sleep(period)
                continue
            now = time.monotonic()
            max_gap = max(max_gap, now - prev_good)
            prev_good = now
            n_samples += 1
            if args.joint in blk:
                n_target_replies += 1
            # `is not None` and NOT truthiness: read_torque_each reports an
            # unreadable servo as None, and 0 is the value we are hoping for.
            if tqs.get(args.joint) is not None:
                n_torque_reads += 1
                max_torque_gap = max(max_torque_gap, now - prev_torque_seen)
                prev_torque_seen = now
            hot = False
            for sid in SERVO_IDS:
                v = tqs.get(sid)
                if v:
                    hot = True
                    if torque_first is None:
                        torque_first = (t_rel, sid, v)
                    if sid in targets and torque_first_target is None:
                        torque_first_target = (t_rel, sid, v)
                st_i = blk.get(sid)
                if st_i is None:
                    continue
                dev = short_way(st_i[0] - base.get(sid, st_i[0]))
                max_dev[sid] = max(max_dev[sid], abs(dev))
                if st_i[3]:
                    errs.add(st_i[3])
                csv_rows.append(
                    f"{t_rel:.4f},{sid},{'' if v is None else v},{st_i[0]},"
                    f"{st_i[1]},{st_i[2]},{st_i[3]}")
            moved = max(max_dev.values())
            if hot or moved >= args.abort_ticks:
                # DE-ENERGISE FIRST. No formatting, no message building, no
                # allocation before the write -- the same discipline as
                # _verify_after_energise_locked.
                torque_off_all(" (AUTO-ABORT)")
                aborted = ("register 40 read non-zero" if hot
                           else f"a servo moved {moved} ticks")
                print(f"   [AUTO-ABORT] {aborted} at t={t_rel:.3f} s.")
                try:
                    after = read_torque_each(bus)
                except Exception:  # noqa: BLE001
                    after = {}
                still = [s for s, v in after.items() if v]
                if not after:
                    print("   [DANGER] could not re-read Torque_Enable after "
                          "the abort -- assume the arm may be LIVE and CUT "
                          "THE 12 V.")
                elif still:
                    print(f"   [DANGER] Torque_Enable STILL reads 1 on {still} "
                          f"after writing 0 -- CUT THE 12 V NOW.")
                else:
                    print("   [SAFE] all seven read Torque_Enable = 0 again.")
                break
            tgt = blk.get(args.joint)
            interesting = (hot or abs(max_dev[args.joint]) >= args.move_ticks
                           or (tgt is not None and tgt[3]))
            if interesting or n_samples % max(1, args.print_every) == 0:
                if tgt is None:
                    print(f"   t={t_rel:5.2f}  {name}: no reply")
                else:
                    dev = short_way(tgt[0] - base[args.joint])
                    e = (f"  ERR {fb.describe_error_bits(tgt[3])}"
                         if tgt[3] else "")
                    print(f"   t={t_rel:5.2f}  tick {tgt[0]:>4}  dev {dev:>+5} "
                          f"({dev * DEG_PER_TICK:>+6.2f} deg)  speed "
                          f"{tgt[1]:>+5}  load {tgt[2]:>+5}  torque "
                          f"{tqs.get(args.joint)}{e}")
            time.sleep(period)
        # the trailing stretch counts too: going blind at the END would hide a
        # late self-enable just as effectively as going blind in the middle.
        max_gap = max(max_gap, time.monotonic() - prev_good)
        max_torque_gap = max(max_torque_gap, time.monotonic() - prev_torque_seen)
    except KeyboardInterrupt:
        # DE-ENERGISE FIRST, exactly as the auto-abort does. Ctrl-C is the
        # OPERATOR's emergency stop -- the one that must be fastest of all -- and
        # the `finally` below used to run only after eight print() calls. On a
        # Windows console in QuickEdit mode a stray click blocks stdout
        # INDEFINITELY, so those prints are not a microsecond cost, they are an
        # unbounded one.
        torque_off_all(" (Ctrl-C)")
        print("\n[INT] Ctrl-C -- de-energising and stopping.")
        return refuse("the run was interrupted before it finished")
    except Exception as e:  # noqa: BLE001
        # A powered bench tool must not exit through a bare traceback: the
        # `finally` below still de-energises, but the OPERATOR also needs a
        # stated verdict, or "it crashed" quietly becomes "we tried it".
        # The traceback is still printed, so a tool bug stays debuggable.
        print(f"\n[FAIL] unexpected error: {e!r}")
        traceback.print_exc()
        return refuse(f"the run crashed before producing a result: {e!r}", 1)
    finally:
        torque_off_all()
        _SAFE["closed"] = True
        try:
            bus.close()
        except Exception:  # noqa: BLE001
            pass

    # -- report ---------------------------------------------------------------
    try:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        outfile = _HERE.with_name(f"a12_seed_j{args.joint}_{stamp}.csv")
        outfile.write_text("\n".join(csv_rows), encoding="utf-8")
        print(f"\n[LOG] full sample series -> {outfile.name}")
    except Exception:  # noqa: BLE001
        pass

    other = [s for s in SERVO_IDS if s != args.joint]
    if args.all_servos:
        # every servo was written, so there is no unwritten control left
        other_dev = 0
    else:
        other_dev = max((max_dev[s] for s in other), default=0)
    target_dev = max_dev[args.joint]
    # Coverage gate: at least a handful of observations, and never blind for
    # longer than a quarter second (or five polling periods, whichever is
    # larger, so a slow bus does not fail itself).
    max_blind_s = max(0.25, 5.0 / max(args.hz, 1.0))
    # EVERY clause here blocks ONLY a negative (decide_verdict consults it after
    # both CONFIRMED branches), so none of them can suppress a true positive.
    # Each answers a different "did the experiment actually happen and did we
    # actually watch it": position coverage, TORQUE coverage, the seed landing,
    # and the watch running to completion rather than being cut short.
    samples_reason = ""
    if not (n_target_replies >= 3 and max_gap <= max_blind_s):
        samples_reason = (
            "nothing was observed, but the arm was not watched continuously "
            "enough for that to be evidence -- it could have energised and "
            "settled again while the bus was silent")
    elif not (n_torque_reads >= 3 and max_torque_gap <= max_blind_s):
        samples_reason = (
            f"Torque_Enable on the written joint was only READ {n_torque_reads} "
            f"time(s), with a blind stretch of {max_torque_gap * 1000:.0f} ms -- "
            f"'torque stayed 0' would be a claim about a register this run "
            f"barely looked at")
    elif seed_landed is False:
        samples_reason = (
            "the seed write did NOT read back at Goal_Position, so the "
            "out-of-window write this test is about probably never reached the "
            "servo -- there was no experiment to have a negative result")
    elif seed_landed is None:
        samples_reason = (
            "Goal_Position could not be read back, so it is unknown whether the "
            "seed write (a reply-less SYNC_WRITE) ever landed")
    elif aborted:
        samples_reason = (
            f"the watch was CUT SHORT by the auto-abort ({aborted}) after "
            f"{n_samples} samples of a planned {args.watch_s:.1f} s window, so "
            f"'nothing happened' is a claim about time that was never watched")
    samples_ok = not samples_reason

    print()
    print("=" * 78)
    print(f"A12 -- {name} (servo {args.joint}), {out} ticks outside its EEPROM "
          f"window [{lo_t}, {hi_t}]")
    print("=" * 78)
    print(f"  seed written        : {'yes' if wrote_seed else 'NO'} "
          f"(Goal_Position = {max(0, min(TICKS - 1, tick_now))}, "
          f"Goal_Speed {SEED_SPEED}, Torque_Enable NEVER written)")
    print(f"  watch window        : {n_samples} samples, "
          f"{n_target_replies} with a reply from {name}")
    print(f"  coverage            : longest blind gap {max_gap * 1000:.0f} ms "
          f"(limit {max_blind_s * 1000:.0f} ms)"
          + (f", {n_blind} failed bus reads" if n_blind else ""))
    print(f"  seed read back      : "
          + {True: "yes, Goal_Position holds the written tick",
             False: "NO -- the write did not reach the servo",
             None: "UNKNOWN -- Goal_Position could not be read"}[seed_landed])
    if torque_first is None:
        # Never "read 0 on every sample" without saying HOW MANY times it was
        # actually read: a silently-unanswered register-40 read produced exactly
        # that sentence while the register had not been looked at once.
        print(f"  Torque_Enable       : read 0 every time it answered "
              f"({n_torque_reads}/{n_samples} samples on {name}, longest "
              f"unobserved stretch {max_torque_gap * 1000:.0f} ms)")
    else:
        t, sid, v = torque_first
        print(f"  Torque_Enable       : FIRST read {v} on servo {sid} "
              f"({'WRITTEN' if sid in targets else 'NOT written by this run'}) "
              f"at t = {t:.3f} s after the write")
        if torque_first_target is not None and torque_first_target != torque_first:
            tt, tsid, tv = torque_first_target
            print(f"                        first on a WRITTEN servo: {tv} on "
                  f"{tsid} at t = {tt:.3f} s")
    print(f"  motion, {name:<12}: {target_dev} ticks "
          f"({target_dev * DEG_PER_TICK:.2f} deg)")
    print(f"  motion, other servos: {other_dev} ticks"
          + ("  (all were written -- no control this run)"
             if args.all_servos else ""))
    print(f"  error bits seen     : "
          f"{[fb.describe_error_bits(e) for e in errs] if errs else 'none'}")
    if aborted:
        print(f"  auto-abort          : {aborted}")

    # preconditions_ok / baseline_ok are literally True here, and that is not a
    # shortcut: every G0/G1/G2/G3 failure returns through refuse() far above, so
    # reaching this line IS the proof that both attribution gates passed. They
    # stay as named parameters because decide_verdict is the unit-tested piece
    # and must remain total over all of them.
    # Torque seen on a WRITTEN servo is A12; on an unwritten one it is a bigger,
    # different finding (see decide_verdict). `targets` is the list actually
    # written, so --all-servos collapses the distinction correctly.
    verdict, reason = decide_verdict(
        preconditions_ok=True, baseline_ok=True, samples_ok=samples_ok,
        samples_reason=samples_reason,
        torque_on_target=torque_first_target is not None,
        torque_on_other=torque_first is not None and torque_first_target is None,
        target_dev=target_dev, other_dev=other_dev,
        move_ticks=args.move_ticks, weak_excursion=weak)

    print()
    print(f"  VERDICT: {verdict}")
    print(f"           {reason}")
    print()
    if verdict == VERDICT_CONFIRMED:
        print("  WHAT THIS MEANS -- a goal write energised a limp servo.")
        print("  The torque-off-is-safe assumption that HAND-GUIDE rests on is")
        print("  VIOLATED: hand-guide is torque-off by design and the student is")
        print("  physically holding the arm. Do NOT trust hand-guide until this")
        print("  is designed around. The driver's own seed write is the")
        print("  trigger, so every /edu6/set_torque call and every boot reaches")
        print("  it. Report the CSV with this run's excursion and joint.")
        return 3
    if verdict == VERDICT_NOT_REPRODUCED:
        print("  WHAT THIS LICENSES: on THIS arm, THIS firmware, THIS joint, at")
        print(f"  an excursion of {out} ticks, one out-of-window seed write did")
        print(f"  not energise a limp servo within {args.watch_s:.1f} s.")
        print("  WHAT IT DOES NOT LICENSE: 'goal writes never energise a servo'.")
        print("  Untested here: other joints, larger excursions, other firmware,")
        print("  and a repeated or continuous write stream. Repeat on joint5")
        print("  (the largest natural excursion) and on at least one other")
        print("  joint before treating hand-guide's assumption as verified.")
        return 0
    print(NOTHING_LEARNED)
    return 2


if __name__ == "__main__":
    sys.exit(main())

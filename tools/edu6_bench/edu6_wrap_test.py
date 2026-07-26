#!/usr/bin/env python3
"""Q1b / §8.1 — the ±180° WRAP TEST for edu6. Torque-on, ONE joint, isolated.

THE QUESTION
    joint4 and joint6 keep full-circle EEPROM windows [0, 4095], so the driver's
    boot plausibility band is a documented NO-OP on them and a hand-turn past
    ±180° is undetectable. §6.3 proved by hand that both CAN be turned there
    (joint6 swept −178.1° → +179.7°). What has never been observed is what the
    SERVO'S OWN control loop does when a joint is parked on that map edge and
    torque is then enabled: goal, present and the EEPROM limits all live in the
    same offset-corrected numbering, so if the loop computes error as a naive
    `goal − present` a joint sitting where Present flickers between ~4095 and ~0
    would see a ~4096-tick error and drive a full revolution.

WHY THIS TOOL AND NOT THE FULL DRIVER (a deliberate deviation from §8.1)
    The driver's relevant protections are the goal seed and the current limits.
    The current limits (`Max_Torque` 800 arm, `Protection_Current` 310 ≈ 2 A) are
    EEPROM — they are in force no matter who drives the bus. The seed is
    replicated here EXACTLY: the same 7-byte contiguous write (accel, goal,
    Goal_Time, speed) at the same `SEED_SPEED_STEPS`, read out of the driver
    source rather than retyped.
    What the driver ADDS is a 3 s quintic boot-home glide of the whole arm
    immediately after torque-on — which for this observation is a confound, not a
    protection. This tool isolates torque-on: ONE servo, no trajectory, bounded
    time, automatic torque-off.

WHAT IT WRITES (the complete list — everything else on the bus is READ ONLY)
    * Goal_Position = Present_Position (+ accel/speed) on the SELECTED servo only
    * Torque_Enable = 1, then 0, on the SELECTED servo only
    Torque-off runs in a `finally` AND at `atexit`, so Ctrl-C, an exception and a
    normal exit all de-energise. NOTHING touches EEPROM. No other servo is
    written at all — the rest of the arm stays limp.

USAGE — escalate toward the edge, do not start at it
    python edu6_wrap_test.py --joint 6 --list
    python edu6_wrap_test.py --joint 6            # observe 6 s, then torque off
    python edu6_wrap_test.py --joint 6 --seconds 10

    Ladder: park the joint by hand at ~170°, run. Then ~175°, ~178°, ~179.5°, and
    the mirror side (−170 … −179.5). The tool prints the joint's angle and
    REFUSES to energise until you confirm, so you always know where it is.

ABORT
    Cut the 12 V. That is faster and more certain than any software path. The
    tool will also stop on Ctrl-C and de-energise.
"""

from __future__ import annotations

import argparse
import ast
import atexit
import math
import os
import sys
import time
from pathlib import Path

REPO = Path(r"C:\Users\svend\newaarm\Testre")
DRIVER = REPO / "robotis_ai_setup" / "docker" / "open_manipulator" / "edu6_arm_node.py"
sys.path.insert(0, str(DRIVER.parent))

import feetech_bus as fb  # noqa: E402


def _lit(name):
    tree = ast.parse(DRIVER.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return ast.literal_eval(node.value)
    raise SystemExit(f"{name} not found in {DRIVER}")


SERVO_IDS = _lit("SERVO_IDS")
JOINT_NAMES = _lit("JOINT_NAMES")
JOINT_LIMITS_RAD = _lit("JOINT_LIMITS_RAD")
SIGNS = _lit("_DEFAULT_SIGNS")
CENTER = _lit("CENTER_TICK")
TICKS = _lit("TICKS_PER_REV")
SEED_SPEED = _lit("SEED_SPEED_STEPS")
WRITE_ACCEL = _lit("WRITE_ACCELERATION")
RAD_PER_TICK = 2.0 * math.pi / TICKS
DEG_PER_TICK = 360.0 / TICKS


def read_block(bus):
    """{sid: (tick, speed, load, err)} from the same 6-byte read the driver uses."""
    out = {}
    reps = bus.sync_read(fb.REG_PRESENT_POSITION, 6, list(SERVO_IDS))
    for sid in SERVO_IDS:
        if sid not in reps:
            out[sid] = None
            continue
        err, d = reps[sid]
        out[sid] = (
            fb.decode_sign_magnitude(fb.from_le16(d[0], d[1]), 15),
            fb.decode_sign_magnitude(fb.from_le16(d[2], d[3]), 15),
            fb.decode_sign_magnitude(fb.from_le16(d[4], d[5]), 10),
            err,
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default=os.environ.get("EDU6_PORT", "COM5"))
    ap.add_argument("--joint", type=int, default=6,
                    help="servo id to energise (4 or 6 — the full-circle joints)")
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--hz", type=float, default=20.0)
    ap.add_argument("--list", action="store_true", help="read all 7 and exit")
    ap.add_argument("--park", action="store_true",
                    help="LIVE readout of the target joint while you turn it by "
                         "hand (read-only). Ctrl-C when it is where you want it.")
    ap.add_argument("--target-deg", type=float, default=179.5,
                    help="the angle --park is coaching you toward")
    ap.add_argument("--cross", type=int, default=0, metavar="N",
                    help="THE DEFINITIVE WRAP TEST. Seed the goal N ticks away "
                         "ACROSS the map edge instead of at the present position, "
                         "so the servo must resolve a wrapped error. Use a SMALL "
                         "N (e.g. 6): wrap-aware -> it moves N ticks and stops; "
                         "naive -> it drives the long way round and the run "
                         "auto-aborts within a few degrees.")
    ap.add_argument("--abort-ticks", type=int, default=40,
                    help="with --cross, cut torque automatically once the joint "
                         "has moved this far the WRONG way")
    ap.add_argument("--baseline", type=float, default=2.0,
                    help="seconds of HANDS-OFF stationary check before torque is "
                         "enabled; a moving joint aborts the run")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation")
    args = ap.parse_args()

    if args.joint not in SERVO_IDS:
        print(f"[FAIL] --joint must be one of {list(SERVO_IDS)}")
        return 1
    idx = SERVO_IDS.index(args.joint)
    name = JOINT_NAMES[idx]

    try:
        bus = fb.FeetechBus(args.port)
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] cannot open {args.port}: {e}")
        print("       'Zugriff verweigert' / PermissionError almost always means "
              "ANOTHER process holds the port — a still-running R9 --watch, an "
              "earlier probe, or a container that has the device usbipd-attached. "
              "Close it (Ctrl-C) and retry; only one process can own the bus.")
        return 1
    energised = {"on": False}

    def _off():
        if energised["on"]:
            try:
                bus.write(args.joint, fb.REG_TORQUE_ENABLE, bytes([0]))
                energised["on"] = False
                print("[SAFE] torque OFF.")
            except Exception as e:  # noqa: BLE001
                print(f"[WARN] torque-off failed ({e}) — CUT THE 12 V.")

    atexit.register(_off)

    try:
        alive = [s for s in SERVO_IDS if bus.ping(s)]
        if not alive:
            print("[FAIL] no servo answered — is the 12 V on?")
            return 1
        st = read_block(bus)
        print(f"[STATE] port {args.port}, {len(alive)}/{len(SERVO_IDS)} answering")
        for i, sid in enumerate(SERVO_IDS):
            if st.get(sid) is None:
                print(f"   {JOINT_NAMES[i]:<15} NO REPLY")
                continue
            tick, spd, load, err = st[sid]
            deg = (tick - CENTER) * DEG_PER_TICK * SIGNS[i]
            lo, hi = JOINT_LIMITS_RAD[i]
            tq = bus.read(sid, fb.REG_TORQUE_ENABLE, 1)[1][0]
            mark = "  <== TARGET" if sid == args.joint else ""
            print(f"   {JOINT_NAMES[i]:<15} tick {tick:>4} {deg:>+8.2f}°  "
                  f"limits [{math.degrees(lo):>+7.1f},{math.degrees(hi):>+7.1f}]°  "
                  f"torque={tq}{mark}")
        if args.list:
            return 0

        if args.park:
            # READ-ONLY coaching loop: turn the joint by hand and watch. Nothing
            # is written; the joint is limp, so it stays where you leave it
            # (a roll joint has no gravity torque about its own axis).
            tgt = float(args.target_deg)
            print()
            print(f"[PARK] Turn {name} BY HAND toward {tgt:+.1f}°. "
                  f"Ctrl-C when you are close — then run the test.")
            print("       (read-only: nothing is written, torque stays off)")
            try:
                while True:
                    b = read_block(bus)
                    if b.get(args.joint) is None:
                        print("   no reply"); time.sleep(0.2); continue
                    tick, _s, _l, _e = b[args.joint]
                    deg = (tick - CENTER) * DEG_PER_TICK * SIGNS[idx]
                    edge = min(abs(tick - 0), abs(tick - (TICKS - 1)))
                    remain = tgt - deg
                    if abs(remain) < 0.6:
                        hint = "<<< THERE — Ctrl-C now"
                    elif remain > 0:
                        hint = f"keep turning + ({remain:+.1f}° to go)"
                    else:
                        hint = f"keep turning − ({remain:+.1f}° to go)"
                    bar = "#" * min(40, int(abs(deg) / 180.0 * 40))
                    print(f"   {deg:>+8.2f}°  edge {edge:>4} ticks  "
                          f"|{bar:<40}| {hint}")
                    time.sleep(0.25)
            except KeyboardInterrupt:
                b = read_block(bus)
                tick = b[args.joint][0] if b.get(args.joint) else None
                if tick is not None:
                    deg = (tick - CENTER) * DEG_PER_TICK * SIGNS[idx]
                    edge = min(abs(tick - 0), abs(tick - (TICKS - 1)))
                    print(f"\n[PARKED] {name} at {deg:+.2f}° (tick {tick}), "
                          f"{edge} ticks from the map edge.")
                    print(f"         Now run:  python edu6_wrap_test.py "
                          f"--joint {args.joint}")
            return 0

        tick0, _s0, _l0, _e0 = st[args.joint]
        deg0 = (tick0 - CENTER) * DEG_PER_TICK * SIGNS[idx]
        edge = min(abs(tick0 - 0), abs(tick0 - (TICKS - 1)))
        print()
        print(f"[TARGET] {name} (servo {args.joint}) at tick {tick0} = {deg0:+.2f}°")
        print(f"         distance to the map edge (tick 0 / 4095): {edge} ticks "
              f"({edge * DEG_PER_TICK:.2f}°)")
        if edge > 400:
            print("         NOTE this is FAR from the edge — a clean result here "
                  "does not test the wrap. Park it closer and repeat.")
        print(f"[PLAN]   seed Goal_Position = {tick0} (the driver's exact 7-byte "
              f"write, speed {SEED_SPEED} steps/s), then Torque_Enable = 1 on "
              f"servo {args.joint} ONLY, observe {args.seconds:.0f} s, torque off.")
        print("         Hardware backstop: Max_Torque + Protection_Current in "
              "EEPROM (unchanged). Abort = cut the 12 V.")
        if not args.yes:
            try:
                if input("         type 'go' to energise: ").strip().lower() != "go":
                    print("[ABORT] nothing was written.")
                    return 0
            except EOFError:
                print("[ABORT] no tty for confirmation — pass --yes deliberately.")
                return 0

        # --- PHASE 0: hands-off baseline, torque still OFF -------------------
        # Without this the test is AMBIGUOUS: if the operator has a hand on the
        # joint, motion during the observation could be the hand rather than the
        # servo. Sample with torque OFF first and require the joint to be
        # stationary. A moving baseline aborts.
        print(f"[BASE]   HANDS OFF — {args.baseline:.1f} s stationary check "
              f"(torque still OFF)…")
        b_end = time.monotonic() + args.baseline
        b_ticks, b_speeds = [], []
        while time.monotonic() < b_end:
            bb = read_block(bus)
            if bb.get(args.joint) is not None:
                b_ticks.append(bb[args.joint][0])
                b_speeds.append(bb[args.joint][1])
            time.sleep(0.05)
        if not b_ticks:
            print("[FAIL] no baseline samples.")
            return 1
        b_span = max(b_ticks) - min(b_ticks)
        b_spd = max(abs(s) for s in b_speeds)
        print(f"[BASE]   drift {b_span} ticks, peak |speed| {b_spd} "
              f"over {len(b_ticks)} samples")
        if b_span > 3 or b_spd > 0:
            print(f"[ABORT] the joint is MOVING before torque is enabled "
                  f"({b_span} ticks, |speed| {b_spd}). Nothing was energised. "
                  f"Take your hands OFF the arm completely, let it settle, and "
                  f"re-run — otherwise the result cannot be attributed to the "
                  f"servo.")
            return 2

        # --- the driver's seed, byte-for-byte ---
        # (with --cross the goal is deliberately placed N ticks away ACROSS the
        # map edge, which is the only way to give the servo a WRAPPED error to
        # resolve. Seeding goal == present can never test wrap-awareness: the
        # error is zero, so a clean hold proves only "no spontaneous runaway".)
        tick_present = b_ticks[-1]
        if args.cross:
            # `--cross N` means: put the goal N ticks BEYOND the nearest map edge,
            # measured from the present position. Computing it this way (rather
            # than `present + N`) makes a crossing structural instead of something
            # the operator has to arrange by parking precisely.
            n = abs(args.cross)
            if tick_present < TICKS // 2:        # nearest edge is tick 0
                tick_seed = (TICKS - n) % TICKS  # land just below 4095
            else:                                # nearest edge is tick 4095
                tick_seed = (n - 1) % TICKS      # land just above 0
            short = tick_seed - tick_present
            if short > TICKS // 2:
                short -= TICKS
            if short < -TICKS // 2:
                short += TICKS
            naive = tick_seed - tick_present
            long_way = naive
            print(f"[CROSS]  present {tick_present} -> goal {tick_seed} "
                  f"(N={n} ticks beyond the nearest edge)")
            print(f"         SHORT way across the seam = {short:+d} ticks "
                  f"({short * DEG_PER_TICK:+.2f}°)")
            print(f"         LONG way (what a naive loop computes) = "
                  f"{long_way:+d} ticks ({long_way * DEG_PER_TICK:+.1f}°)")
            # THE VALIDITY GATE. Without a WRAPPED error the test is vacuous and
            # would emit a meaningless "WRAP-AWARE" pass — which is exactly what
            # happened once (present 59, goal 41: same side of the edge, so the
            # servo just did an ordinary 18-tick move and "passed"). A guard that
            # can report a false pass is worse than no guard.
            if abs(naive) <= TICKS // 2:
                print(f"[FAIL] goal {tick_seed} and present {tick_present} are on "
                      f"the SAME side of the map edge — the naive error "
                      f"({naive:+d}) is not wrapped, so this run would prove "
                      f"NOTHING. Park the joint closer to ±180° (within ~{n * 2} "
                      f"ticks of tick 0/4095) and retry.")
                return 1
            print(f"         VALID: the naive error IS wrapped "
                  f"(|{naive}| > {TICKS // 2}), so the direction the joint moves "
                  f"is the answer.")
            print(f"         Wrap-aware -> moves {short:+d} ticks and stops. "
                  f"Naive -> heads the other way; AUTO-ABORT at "
                  f"{args.abort_ticks} ticks ({args.abort_ticks * DEG_PER_TICK:.1f}°).")
        else:
            tick_seed = tick_present
        payload = (bytes([WRITE_ACCEL])
                   + fb.le16(max(0, min(TICKS - 1, tick_seed)))
                   + fb.le16(0)
                   + fb.le16(SEED_SPEED))
        bus.sync_write(fb.REG_ACCELERATION, {args.joint: payload})
        rb = bus.read_u16(args.joint, fb.REG_GOAL_POSITION)
        print(f"[SEED]   Goal_Position read back = {rb} (wanted {tick_seed})")
        if abs(rb - tick_seed) > 2:
            print("[FAIL] the goal did not take — NOT energising.")
            return 1
        # deviation is always measured from where the joint WAS at torque-on
        tick0 = tick_present
        # The drift window: did the joint move between the seed and torque-on?
        pre = read_block(bus)
        tick_pre = pre[args.joint][0] if pre.get(args.joint) else None
        if tick_pre is not None:
            d = tick_pre - tick0
            if d > TICKS // 2:
                d -= TICKS
            if d < -TICKS // 2:
                d += TICKS
            print(f"[SEED]   present just before torque-on = {tick_pre} "
                  f"(goal {tick0}, delta {d:+d} ticks)")
            if abs(tick_pre - tick0) > TICKS // 2:
                print("         ^^^ NOTE present and goal are on OPPOSITE sides "
                      "of the map edge — this is precisely the condition a "
                      "naive (non-wrap-aware) loop mishandles.")

        bus.write(args.joint, fb.REG_TORQUE_ENABLE, bytes([1]))
        energised["on"] = True
        # VERIFY the torque actually took. Without this the whole test can pass
        # VACUOUSLY: a roll joint needs no torque to stay put, so "0 ticks of
        # deviation, 0 load" is equally consistent with "held perfectly" and
        # "was never energised". An ack to the write is not proof the register
        # latched.
        tq_rb = bus.read(args.joint, fb.REG_TORQUE_ENABLE, 1)[1][0]
        print(f"[ON]     Torque_Enable read back = {tq_rb}")
        if tq_rb != 1:
            print("[FAIL] the servo did NOT energise — this run proves nothing. "
                  "Aborting rather than reporting a vacuous CLEAN HOLD.")
            return 1
        print(f"[ON]     torque CONFIRMED on servo {args.joint}. Observing "
              f"{args.seconds:.0f} s…")
        print("         >>> Now gently try to TURN THE JOINT BY HAND. It should "
              "RESIST. That is your physical proof it is energised — and the "
              "`load` column should jump while you push. <<<")

        period = 1.0 / max(args.hz, 1.0)
        t_on = time.monotonic()
        csv_rows = ["t_s,tick,speed,load,dev"]
        end = t_on + args.seconds
        samples = []
        worst_dev = 0
        worst_load = 0
        errs = set()
        while time.monotonic() < end:
            b = read_block(bus)
            if b.get(args.joint) is None:
                print("   [WARN] no reply")
                time.sleep(period)
                continue
            tick, spd, load, err = b[args.joint]
            dev = tick - tick0
            # interpret a deviation the SHORT way round the circle
            if dev > TICKS // 2:
                dev -= TICKS
            if dev < -TICKS // 2:
                dev += TICKS
            samples.append((tick, spd, load, dev))
            if args.cross:
                # progress toward the goal, short way. Correct motion is toward
                # +args.cross; anything beyond abort_ticks the OTHER way is the
                # naive long-way drive and must be stopped without waiting for a
                # human.
                wrong = -dev if short > 0 else dev
                if wrong >= args.abort_ticks:
                    print(f"   [AUTO-ABORT] moved {wrong} ticks the WRONG way "
                          f"({wrong * DEG_PER_TICK:.1f}°) — the loop is NOT "
                          f"wrap-aware. Cutting torque now.")
                    break
            csv_rows.append(f"{time.monotonic() - t_on:.3f},{tick},{spd},{load},{dev}")
            worst_dev = max(worst_dev, abs(dev))
            worst_load = max(worst_load, abs(load))
            if err:
                errs.add(err)
            print(f"   tick {tick:>4}  dev {dev:>+5} ({dev * DEG_PER_TICK:>+6.2f}°)  "
                  f"speed {spd:>+5}  load {load:>+5}"
                  + (f"  ERR {fb.describe_error_bits(err)}" if err else ""))
            time.sleep(period)
    finally:
        _off()
        try:
            bus.close()
        except Exception:  # noqa: BLE001
            pass

    try:
        out = Path(__file__).with_name(
            f"wrap_j{args.joint}_{int(round(deg0))}deg.csv")
        out.write_text(chr(10).join(csv_rows), encoding="utf-8")
        print()
        print(f"[LOG] full sample series -> {out.name}")
    except Exception:  # noqa: BLE001
        pass
    print()
    print("=" * 72)
    print(f"WRAP TEST — {name} (servo {args.joint}) parked at {deg0:+.2f}°, "
          f"{edge} ticks from the map edge")
    print("=" * 72)
    if not samples:
        print("  no samples — inconclusive.")
        return 1
    print(f"  worst deviation from the seeded position: {worst_dev} ticks "
          f"({worst_dev * DEG_PER_TICK:.2f}°)")
    print(f"  peak |load|: {worst_load} (±1000 = ±100 % PWM)")
    print(f"  error bits seen: "
          f"{[fb.describe_error_bits(e) for e in errs] if errs else 'none'}")
    onset = next((i for i, sm in enumerate(samples) if abs(sm[3]) > 3), None)
    if onset is not None:
        print(f"  motion began at sample {onset} (~{onset / max(args.hz,1):.2f} s "
              f"after torque-on) — a hand cannot accelerate a joint to the "
              f"commanded speed cap that fast, so an early onset at a CONSTANT "
              f"speed of {SEED_SPEED} steps/s is servo-driven by construction")
    sign_flips = sum(1 for a, b in zip(samples, samples[1:])
                     if a[1] and b[1] and (a[1] > 0) != (b[1] > 0))
    print(f"  speed sign flips (hunting indicator): {sign_flips}")
    print()
    if args.cross:
        final_dev = samples[-1][3]
        want = short
        print(f"  commanded {want:+d} ticks across the seam; final deviation "
              f"{final_dev:+d} ticks")
        if abs(final_dev - want) <= 4:
            print("  VERDICT: WRAP-AWARE. The servo took the SHORT way across the "
                  "map edge and stopped on target. Q1b is ANSWERED — the ±180° "
                  "residual is benign and the J4/J6 limit trim stays DROPPED.")
        elif (want > 0 and final_dev < -args.abort_ticks + 5) or              (want < 0 and final_dev > args.abort_ticks - 5):
            print("  VERDICT: NOT WRAP-AWARE. The servo drove the LONG way round "
                  "a wrapped error. Q1b is ANSWERED the other way: joint4/joint6 "
                  "must not be left near ±180° while torqued, and the ±170° trim "
                  "(window (114, 3982), sign-free, no re-jig) is the fix.")
        else:
            print("  VERDICT: INCONCLUSIVE — neither a clean short move nor a "
                  "clear long-way drive. Re-run; send the CSV.")
        print(f"  peak |load| {worst_load}, errors "
              f"{[fb.describe_error_bits(e) for e in errs] if errs else 'none'}")
    elif worst_dev <= 10 and not errs and sign_flips <= 2:
        print("  VERDICT: CLEAN HOLD. The servo held the seeded position at the "
              "map edge — no runaway, no hunting, no error bits.")
        if worst_load == 0:
            print("  ⚠ peak load was 0, i.e. the servo never had to WORK to hold "
                  "this pose (normal for a roll joint — gravity applies no torque "
                  "about its own axis). Torque_Enable was read back as 1, so it "
                  "WAS energised; if you also felt it resist a hand nudge, this "
                  "result is fully confirmed. If you did not push it, the run "
                  "still proves no runaway — which is the question — but not that "
                  "the loop holds under load.")
        print("  Repeat closer to the edge and on the mirror side; once ~179.5° is "
              "clean on both joint4 and joint6, Q1b is answered and the J4/J6 "
              "limit trim stays DROPPED.")
    elif worst_dev > 200:
        print(f"  VERDICT: RUNAWAY / LARGE MOTION ({worst_dev} ticks). This is the "
              f"failure §8.1 predicted. Do NOT run the arm with joint4/joint6 at "
              f"full-circle windows — apply the ±170° trim (window (114, 3982), "
              f"sign-free, Homing_Offset untouched, so NO re-jig) and re-test.")
    else:
        print(f"  VERDICT: MARGINAL — {worst_dev} ticks of motion, "
              f"{sign_flips} speed sign flips, errors {errs or 'none'}. "
              f"Re-run and capture the log; this needs a judgement call before "
              f"the trim decision.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

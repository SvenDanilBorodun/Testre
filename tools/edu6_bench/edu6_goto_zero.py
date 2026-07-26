#!/usr/bin/env python3
"""Drive the edu6 arm to its URDF-ZERO pose (all arm joints 0°), safely.

WHY THIS IS NOW CONSIDERED SAFE (it was refused earlier, on weaker evidence)
    URDF zero is a FOLDED pose: at all-zeros the forearm passes within ~39.7 mm
    (centreline) of the upper arm, vs 67.4 mm at HOME, and R7 (self-collision,
    stall-vs-damage) is still an open rig gate. That was the basis for refusing.

    R9 then measured what a LIMP arm actually does: it collapses to approximately
    all-zeros. Observed rest poses during R9 were within ~13° of zero on every
    collision-relevant joint (e.g. J2 −1.4°, J3 +6.2°, J5 +3.5°). So the arm has
    been resting in essentially this pose, unpowered and undamaged, for hours —
    which is empirical evidence the pose is mechanically fine, not merely
    theoretically tolerable.

    Also relevant after today's wrap finding: at zero, joint4 and joint6 sit at
    tick 2048, i.e. maximally FAR from the position map edge. Zero is one of the
    safest poses this arm can hold.

TWO HARD CONSTRAINTS, ENFORCED HERE
    1. The GRIPPER is excluded. Gripper zero = jaws fully closed against their own
       mechanical stop, i.e. a sustained stall at Max_Torque 150 with nothing
       between them. `--include-gripper` exists but is deliberately awkward.
    2. Every commanded move is checked to be NON-WRAPPED before it is written.
       Today's measurement (bringup doc §11.11) proved the STS loop computes error
       as a naive `goal − present`, so a wrapped error drives the LONG way round.
       Any joint whose |goal − present| is within WRAP_GUARD_TICKS of the 2048
       half-turn boundary is REFUSED with an instruction to hand-turn it closer
       first — no reliance on a thin margin.

SAFETY MECHANICS
    * one joint at a time, in a fixed distal→proximal order;
    * the servo's own speed cap does the limiting: SEED_SPEED_STEPS (200 steps/s
      ≈ 0.31 rad/s), the same gentle rate the driver uses for its torque-on seed;
    * a per-joint WATCHDOG samples position and aborts + de-energises if the joint
      moves the WRONG way, stalls, or reports an error bit;
    * torque-off in `finally` AND `atexit`; Ctrl-C de-energises everything;
    * NOTHING touches EEPROM. Only Goal_Position/accel/speed and Torque_Enable.

USAGE
    python edu6_goto_zero.py --check            # read-only: how far is each joint?
    python edu6_goto_zero.py                    # drive arm joints to zero, hold, off
    python edu6_goto_zero.py --hold 20          # hold zero for 20 s before torque-off
    python edu6_goto_zero.py --joints 2,3,5     # only these
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
DEG_PER_TICK = 360.0 / TICKS

GRIPPER_ID = SERVO_IDS[-1]
ARM_IDS = SERVO_IDS[:-1]
# Drive distal -> proximal: the wrist/roll joints carry almost nothing, so any
# surprise happens on the lightest joint first, while the heavy shoulder/elbow are
# still limp and resting.
DRIVE_ORDER = (6, 5, 4, 3, 2, 1)
# How close to the 2048-tick half-turn boundary a commanded move may get before it
# is refused. Today's finding: at exactly 2048 the naive error's SIGN is ambiguous
# and beyond it the servo goes the long way round.
WRAP_GUARD_TICKS = 300
ARRIVE_TOL_TICKS = 12          # ~1.05°
STALL_SAMPLES = 20             # ~1 s at 20 Hz with no progress = stalled
WRONG_WAY_TICKS = 30           # ~2.6° against the commanded direction = abort


def read_block(bus):
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


def deg_of(sid, tick):
    return (tick - CENTER) * DEG_PER_TICK * SIGNS[SERVO_IDS.index(sid)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default=os.environ.get("EDU6_PORT", "COM5"))
    ap.add_argument("--check", action="store_true", help="read-only report")
    ap.add_argument("--joints", default="", help="comma list, default all arm joints")
    ap.add_argument("--hold", type=float, default=5.0)
    ap.add_argument("--hz", type=float, default=20.0)
    ap.add_argument("--include-gripper", action="store_true",
                    help="NOT RECOMMENDED: gripper zero stalls the jaws closed")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    want = ([int(x) for x in args.joints.split(",") if x.strip()]
            if args.joints else list(ARM_IDS))
    if GRIPPER_ID in want and not args.include_gripper:
        print(f"[INFO] excluding the gripper (servo {GRIPPER_ID}): zero closes the "
              f"jaws onto their own hard stop, which is a sustained stall.")
        want = [s for s in want if s != GRIPPER_ID]
    order = [s for s in DRIVE_ORDER if s in want] + \
            [s for s in want if s not in DRIVE_ORDER]

    try:
        bus = fb.FeetechBus(args.port)
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] cannot open {args.port}: {e}")
        print("       Another process probably holds the port (an open --park or "
              "--watch, or a container with the device usbipd-attached).")
        return 1

    live = set()

    def _off_all():
        for sid in sorted(live):
            try:
                bus.write(sid, fb.REG_TORQUE_ENABLE, bytes([0]))
            except Exception:  # noqa: BLE001
                print(f"[WARN] torque-off failed on servo {sid} — CUT THE 12 V.")
        if live:
            print(f"[SAFE] torque OFF on {sorted(live)}.")
        live.clear()

    atexit.register(_off_all)

    try:
        alive = [s for s in SERVO_IDS if bus.ping(s)]
        if len(alive) != len(SERVO_IDS):
            print(f"[FAIL] only {alive} answered — check the 12 V and the chain.")
            return 1
        st = read_block(bus)
        print(f"[STATE] {args.port}, all {len(alive)} servos answering\n")
        print(f"  {'joint':<16}{'tick':>6}{'angle':>10}{'to zero':>10}"
              f"{'ticks':>8}  wrap-safe?")
        blockers = []
        for sid in SERVO_IDS:
            tick = st[sid][0]
            d = deg_of(sid, tick)
            err = CENTER - tick
            naive = abs(err)
            safe = naive < (TICKS // 2 - WRAP_GUARD_TICKS)
            tag = "" if sid not in order else ("  yes" if safe else "  NO <-- hand-turn closer first")
            print(f"  {JOINT_NAMES[SERVO_IDS.index(sid)]:<16}{tick:>6}"
                  f"{d:>+9.2f}°{-d:>+9.2f}°{err:>+8d}{tag}")
            if sid in order and not safe:
                blockers.append(sid)
        print()
        if blockers:
            print(f"[STOP] servo(s) {blockers} would need a move of ~180°, which is "
                  f"too close to the 2048-tick half-turn boundary. Today's "
                  f"measurement (bringup §11.11) showed the servo loop is NOT "
                  f"wrap-aware, so a marginal error can send it the LONG way "
                  f"round. Hand-turn those joints to within ~150° of zero first "
                  f"(they are free-spinning), then re-run.")
            return 2
        if args.check:
            print("[CHECK] read-only; nothing was written.")
            return 0

        print(f"[PLAN]  drive {order} to zero, one at a time, at "
              f"{SEED_SPEED} steps/s (~0.31 rad/s). Gripper "
              f"{'INCLUDED' if GRIPPER_ID in order else 'excluded'}. "
              f"Hold {args.hold:.0f} s, then torque off.")
        print("        Abort = cut the 12 V. Watchdog aborts on wrong-way motion, "
              "a stall, or any servo error bit.")
        if not args.yes:
            try:
                if input("        type 'go': ").strip().lower() != "go":
                    print("[ABORT] nothing written.")
                    return 0
            except EOFError:
                print("[ABORT] no tty; pass --yes deliberately.")
                return 0

        period = 1.0 / max(args.hz, 1.0)
        for sid in order:
            nm = JOINT_NAMES[SERVO_IDS.index(sid)]
            st = read_block(bus)
            start = st[sid][0]
            goal = CENTER
            naive = goal - start
            if abs(naive) >= TICKS // 2 - WRAP_GUARD_TICKS:
                print(f"[SKIP] {nm}: move became wrap-marginal ({naive:+d}).")
                continue
            direction = 1 if naive > 0 else -1
            print(f"\n[MOVE] {nm}: tick {start} -> {goal} "
                  f"({naive:+d} ticks, {naive * DEG_PER_TICK:+.2f}°)")
            payload = (bytes([WRITE_ACCEL]) + fb.le16(goal)
                       + fb.le16(0) + fb.le16(SEED_SPEED))
            bus.sync_write(fb.REG_ACCELERATION, {sid: payload})
            rb = bus.read_u16(sid, fb.REG_GOAL_POSITION)
            if abs(rb - goal) > 2:
                print(f"[FAIL] goal read back {rb}, wanted {goal} — not energising.")
                return 1
            bus.write(sid, fb.REG_TORQUE_ENABLE, bytes([1]))
            live.add(sid)
            if bus.read(sid, fb.REG_TORQUE_ENABLE, 1)[1][0] != 1:
                print(f"[FAIL] {nm} did not energise.")
                return 1

            best = abs(goal - start)
            stalled = 0
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                b = read_block(bus)
                if b.get(sid) is None:
                    print(f"   [WARN] {nm}: no reply")
                    time.sleep(period)
                    continue
                tick, spd, load, err = b[sid]
                rem = goal - tick
                moved = (tick - start) * direction
                if err:
                    print(f"   [ABORT] {nm} error bit: "
                          f"{fb.describe_error_bits(err)}")
                    _off_all()
                    return 1
                if moved < -WRONG_WAY_TICKS:
                    print(f"   [ABORT] {nm} moved {-moved} ticks the WRONG way.")
                    _off_all()
                    return 1
                if abs(rem) <= ARRIVE_TOL_TICKS:
                    print(f"   arrived: tick {tick} ({deg_of(sid, tick):+.2f}°), "
                          f"load {load}")
                    break
                if abs(rem) < best - 1:
                    best, stalled = abs(rem), 0
                else:
                    stalled += 1
                    if stalled >= STALL_SAMPLES:
                        print(f"   [ABORT] {nm} stalled {abs(rem)} ticks short "
                              f"(load {load}). Possible mechanical interference.")
                        _off_all()
                        return 1
                time.sleep(period)
            else:
                print(f"   [ABORT] {nm} timed out.")
                _off_all()
                return 1

        print(f"\n[HOLD]  all requested joints at zero; holding {args.hold:.0f} s")
        end = time.monotonic() + args.hold
        while time.monotonic() < end:
            b = read_block(bus)
            row = "  ".join(
                f"{JOINT_NAMES[SERVO_IDS.index(s)][:6]}={deg_of(s, b[s][0]):+6.2f}°"
                f"/L{b[s][2]:+4d}" for s in order if b.get(s))
            print("   " + row)
            time.sleep(0.5)
    finally:
        _off_all()
        try:
            bus.close()
        except Exception:  # noqa: BLE001
            pass
    print("\n[DONE] arm de-energised. It will sag — that is expected (the STS gear "
          "train backdrives).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

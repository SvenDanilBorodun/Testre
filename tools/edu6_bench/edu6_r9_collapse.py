#!/usr/bin/env python3
"""R9 — torque-off collapse probe for edu6 (READ-ONLY).

WHAT R9 ANSWERS
    The driver's boot probe refuses to start the arm when any joint READS further
    than BOOT_POSITION_TOLERANCE_TICKS (default 400) outside its designed EEPROM
    window. That band has to sit in a window:

        worst INNOCENT excursion  <  band  <  smallest WRAP excursion

    The upper bound is already known from arithmetic: a hand-turn past +-180 deg
    lands at tick ~0 / ~4095, which is >= 795 ticks outside every detectable
    window (joint1 1023, joint2 2048, joint3 2047, joint5 795, gripper 880).
    The LOWER bound is physical and unmeasured: how far past its designed window
    does a joint actually rest when the arm is limp and gravity has its way?
    That is what this tool measures, and why R9 gates the band.

    Context for why it matters: at rest the arm has already been seen with joint3
    60 ticks (5.3 deg) OUTSIDE its window — under the ORIGINAL 114-tick band that
    was 4.7 deg from refusing "Umgebung starten" on a completely healthy arm.

SAFETY — this tool is structurally incapable of changing the arm
    * It issues ONLY PING and READ/SYNC_READ. The two mutating methods on the
      bus (``write`` / ``sync_write``) are REPLACED with raisers, so a coding
      slip cannot energise a servo, move a goal, or touch EEPROM.
    * It refuses to run if any servo reports Torque_Enable = 1, because a
      torqued joint is not limp and the measurement would be meaningless
      (override with --allow-torque-on if you really want a torqued reading).
    * Nothing here writes Homing_Offset, so the provisioning is untouched.

USAGE (bench PC, arm's 12 V ON, USB on COM5 — no usbipd attach needed)
    python edu6_r9_collapse.py                    # one snapshot
    python edu6_r9_collapse.py --watch            # live, ~4 Hz, Ctrl-C to stop
    python edu6_r9_collapse.py --settle 20        # capture 20 s, min/max/final

    The R9 procedure: run --watch, then for EACH joint in turn raise it by hand,
    let it go, and let it fall/settle. Watch the "outside" column. When you have
    done all six arm joints plus the gripper, Ctrl-C and read the summary — the
    number that matters is WORST EXCURSION, and the verdict tells you whether
    400 still has margin.
"""

from __future__ import annotations

import argparse
import ast
import math
import os
import sys
import time
from pathlib import Path

REPO = Path(r"C:\Users\svend\newaarm\Testre")
DRIVER = REPO / "robotis_ai_setup" / "docker" / "open_manipulator" / "edu6_arm_node.py"
sys.path.insert(0, str(DRIVER.parent))

import feetech_bus as fb  # noqa: E402  (the SAME clean-room bus the driver uses)


# ── pull the arm constants out of the driver source (no rclpy import) ────────
def _literal(name: str):
    tree = ast.parse(DRIVER.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return ast.literal_eval(node.value)
    raise SystemExit(f"{name} not found as a module-level literal in {DRIVER}")


SERVO_IDS = _literal("SERVO_IDS")
JOINT_NAMES = _literal("JOINT_NAMES")
JOINT_LIMITS_RAD = _literal("JOINT_LIMITS_RAD")
SIGNS = _literal("_DEFAULT_SIGNS")
BAND = _literal("_DEFAULT_BOOT_POSITION_TOLERANCE_TICKS")
CENTER = _literal("CENTER_TICK")
TICKS = _literal("TICKS_PER_REV")
RAD_PER_TICK = 2.0 * math.pi / TICKS

# The wrap-excursion floor: how far outside its window a wrapped reading lands.
# Computed, not hardcoded, so it tracks the windows.
def _wrap_floor(lo: int, hi: int) -> int | None:
    """Smallest DETECTABLE wrap excursion for a window, or None when the window
    touches a register end (then one of the two landings is undetectable at ANY
    band width — true of joint2 hi=4095 and joint3 lo=0)."""
    cands = []
    if lo > 0:
        cands.append(lo - 0)          # a wrap landing at tick 0
    if hi < TICKS - 1:
        cands.append((TICKS - 1) - hi)  # a wrap landing at tick 4095
    if not cands or lo <= 0 or hi >= TICKS - 1:
        return min(cands) if cands else None
    return min(cands)


class ReadOnlyBus(fb.FeetechBus):
    """FeetechBus with both mutating methods removed. PING/READ only."""

    def write(self, *a, **k):     # noqa: D102
        raise RuntimeError("READ-ONLY PROBE: write() is disabled")

    def sync_write(self, *a, **k):  # noqa: D102
        raise RuntimeError("READ-ONLY PROBE: sync_write() is disabled")


def read_state(bus):
    """{sid: (tick, torque_on, err)} — one sync_read + a 1-byte torque read."""
    out = {}
    replies = bus.sync_read(fb.REG_PRESENT_POSITION, 2, list(SERVO_IDS))
    for sid in SERVO_IDS:
        if sid not in replies:
            out[sid] = None
            continue
        err, data = replies[sid]
        tick = fb.decode_sign_magnitude(fb.from_le16(data[0], data[1]), 15)
        out[sid] = (tick, None, err)
    return out


def read_torque(bus):
    t = {}
    for sid in SERVO_IDS:
        try:
            t[sid] = bus.read(sid, fb.REG_TORQUE_ENABLE, 1)[1][0]
        except fb.FeetechBusError:
            t[sid] = None
    return t


def windows():
    w = {}
    for i, sid in enumerate(SERVO_IDS):
        w[sid] = fb.position_limit_window(*JOINT_LIMITS_RAD[i], SIGNS[i])
    return w


def excursion(tick: int, lo: int, hi: int) -> int:
    """Ticks OUTSIDE the designed window (0 when inside) — exactly the quantity
    the driver's boot probe bands."""
    if tick < lo:
        return lo - tick
    if tick > hi:
        return tick - hi
    return 0


def fmt_row(i, sid, tick, lo, hi, err, worst):
    out = excursion(tick, lo, hi)
    deg = (tick - CENTER) * RAD_PER_TICK * SIGNS[i] * 180.0 / math.pi
    flag = ""
    if out:
        flag = f"  OUTSIDE by {out:>4} ({out * RAD_PER_TICK * 180 / math.pi:5.1f} deg)"
        if out > BAND:
            flag += "  << WOULD REFUSE BOOT"
    e = f"  err={fb.describe_error_bits(err)}" if err else ""
    return (f"  {JOINT_NAMES[i]:<15} tick {tick:>4}  {deg:>+7.1f} deg   "
            f"window [{lo:>4},{hi:>4}]  worst {worst:>4}{flag}{e}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default=os.environ.get("EDU6_PORT", "COM5"))
    ap.add_argument("--watch", action="store_true", help="live until Ctrl-C")
    ap.add_argument("--settle", type=float, default=0.0,
                    help="capture for N seconds, then report min/max/final")
    ap.add_argument("--hz", type=float, default=4.0)
    ap.add_argument("--allow-torque-on", action="store_true")
    args = ap.parse_args()

    print(f"[R9] READ-ONLY collapse probe on {args.port} "
          f"(write/sync_write are disabled in this process)")
    try:
        bus = ReadOnlyBus(args.port)
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] cannot open {args.port}: {e}")
        print("       Is the CH343 adapter plugged in? Is another process (a "
              "container, an old probe) holding the port?")
        return 1

    alive = [s for s in SERVO_IDS if bus.ping(s)]
    if not alive:
        print("[FAIL] no servo answered a ping.")
        print("       The USB adapter alone does NOT power the servos — check "
              "the arm's 12 V supply is plugged in AND switched on.")
        return 1
    if len(alive) != len(SERVO_IDS):
        print(f"[WARN] only {alive} answered; missing "
              f"{[s for s in SERVO_IDS if s not in alive]} — check the daisy "
              f"chain. Continuing with what answers.")

    tq = read_torque(bus)
    on = [s for s, v in tq.items() if v]
    if on and not args.allow_torque_on:
        print(f"[STOP] Torque is ON for servo(s) {on}. R9 measures where a LIMP "
              f"arm settles, so a torqued joint invalidates it.")
        print("       This probe will not switch torque off (it is read-only by "
              "design). Power-cycle the arm, or pass --allow-torque-on to read "
              "anyway.")
        return 2
    print(f"[OK] {len(alive)}/{len(SERVO_IDS)} servos answer, torque OFF on all "
          f"— the arm is limp.")

    win = windows()
    print()
    print(f"Designed windows + the wrap floor per joint (band in force: {BAND} ticks):")
    floors = []
    for i, sid in enumerate(SERVO_IDS):
        lo, hi = win[sid]
        wf = _wrap_floor(lo, hi)
        if wf is None or lo <= 0 or hi >= TICKS - 1:
            note = ("full-circle window — NOTHING can read out of range, so the "
                    "band is a documented NO-OP here"
                    if (lo <= 0 and hi >= TICKS - 1)
                    else f"one wrap landing is UNDETECTABLE (window touches a "
                         f"register end); the other is {wf}")
        else:
            note = f"a wrap lands >= {wf} ticks outside"
            floors.append(wf)
        print(f"  {JOINT_NAMES[i]:<15} [{lo:>4},{hi:>4}]  {note}")
    if floors:
        print(f"\n  => the band must stay BELOW {min(floors)} ticks to keep "
              f"detecting every detectable wrap.")

    worst = {s: 0 for s in SERVO_IDS}
    lo_seen = {s: None for s in SERVO_IDS}
    hi_seen = {s: None for s in SERVO_IDS}
    period = 1.0 / max(args.hz, 0.5)
    deadline = time.monotonic() + args.settle if args.settle else None

    print()
    if args.watch or args.settle:
        print("Raise each joint by hand, release it, and let it settle. "
              "Ctrl-C when done.\n")
    try:
        while True:
            st = read_state(bus)
            lines = []
            for i, sid in enumerate(SERVO_IDS):
                if st.get(sid) is None:
                    lines.append(f"  {JOINT_NAMES[i]:<15} NO REPLY")
                    continue
                tick, _tq, err = st[sid]
                lo, hi = win[sid]
                worst[sid] = max(worst[sid], excursion(tick, lo, hi))
                lo_seen[sid] = tick if lo_seen[sid] is None else min(lo_seen[sid], tick)
                hi_seen[sid] = tick if hi_seen[sid] is None else max(hi_seen[sid], tick)
                lines.append(fmt_row(i, sid, tick, lo, hi, err, worst[sid]))
            if not (args.watch or args.settle):
                print("\n".join(lines))
                break
            sys.stdout.write("\033[H\033[J" if os.name != "nt" else "")
            print("\n".join(lines))
            print("-" * 78)
            if deadline and time.monotonic() >= deadline:
                break
            time.sleep(period)
    except KeyboardInterrupt:
        print("\n(stopped)")

    print()
    print("=" * 78)
    print("R9 SUMMARY — worst excursion OUTSIDE the designed window, per joint")
    print("=" * 78)
    overall = 0
    for i, sid in enumerate(SERVO_IDS):
        lo, hi = win[sid]
        w = worst[sid]
        overall = max(overall, w)
        span = (f"ticks seen {lo_seen[sid]}..{hi_seen[sid]}"
                if lo_seen[sid] is not None else "no data")
        print(f"  {JOINT_NAMES[i]:<15} worst {w:>4} ticks "
              f"({w * RAD_PER_TICK * 180 / math.pi:5.1f} deg)   {span}")
    print()
    print(f"  WORST EXCURSION ANYWHERE: {overall} ticks "
          f"({overall * RAD_PER_TICK * 180 / math.pi:.1f} deg)")
    print(f"  band in force:            {BAND} ticks")
    if floors:
        print(f"  wrap-detection ceiling:   {min(floors)} ticks")
        print()
        if overall >= BAND:
            print(f"  VERDICT: RAISE THE BAND. A healthy limp arm reached {overall} "
                  f"ticks, which the {BAND}-tick band would REFUSE. Set "
                  f"EDUBOTICS_EDU6_BOOT_POS_TOL_TICKS above {overall} (and below "
                  f"{min(floors)}) in the host .env — no image rebuild needed.")
        else:
            margin = BAND - overall
            head = min(floors) - BAND
            print(f"  VERDICT: {BAND} is OK. Margin above the worst innocent rest "
                  f"pose: {margin} ticks "
                  f"({margin * RAD_PER_TICK * 180 / math.pi:.1f} deg). Headroom "
                  f"below the wrap ceiling: {head} ticks.")
            if margin < 100:
                print(f"           NOTE margin is thin (<100 ticks) — consider "
                      f"raising the band toward {min(floors) - 100}.")
    bus.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

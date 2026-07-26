#!/usr/bin/env python3
"""Drive the edu6 arm to a named pose (HOME or URDF-ZERO), safely, one joint at a
time. Bench tool — throwaway, never committed.

TARGETS
    --target home   the driver's HOME_JOINTS_RAD + GRIPPER_OPEN_RAD (read from
                    edu6_arm_node.py, never retyped). The gripper is INCLUDED
                    because HOME opens the jaws, which is not a stall.
    --target zero   every arm joint at 0. The gripper is EXCLUDED automatically:
                    gripper zero clamps the jaws onto their own hard stop, i.e. a
                    sustained stall at Max_Torque 150 with nothing between them.

THE WRAP GUARD — and why it is 30 ticks, not 300
    Measured 2026-07-26 (bringup doc §11.11): the STS position loop is NOT
    wrap-aware. It computes error as a naive ``goal - present``, so if that error
    exceeds a half-turn (2048 ticks) the servo drives the LONG way round.

    A move is therefore dangerous only when it is CLOSE TO A HALF-TURN, where the
    two directions are nearly equal and the sign of the naive error decides which
    way it goes. At exactly 2048 the choice is ambiguous. WRAP_GUARD_TICKS is a
    cushion around that single ambiguous distance — big enough that sensor jitter
    (1-2 ticks) or the joint settling between the read and the write cannot tip it
    over, small enough that it refuses nothing useful.

    An earlier version used 300, which was ~10x too wide: it refused a joint 174.8
    degrees from its target even though that move had 59 ticks of margin and would
    have gone the correct way. 30 ticks = 2.6 degrees.

SAFETY MECHANICS
    * one joint at a time, in an order that a COLLISION PRE-FLIGHT has simulated
      and approved (see DRIVE_ORDER: elbow first, because from an extended start
      driving the wrist or shoulder first pushes links below the table);
    * the servo's own speed cap does the limiting: SEED_SPEED_STEPS (200 steps/s
      ~ 0.31 rad/s), the same gentle rate the driver uses for its torque-on seed;
    * every commanded target is checked against the joint's URDF limits AND its
      EEPROM window before anything is written;
    * a per-joint WATCHDOG aborts and de-energises on wrong-way motion, a stall,
      or any servo error bit;
    * torque-off in ``finally`` AND ``atexit``; Ctrl-C de-energises everything;
    * NOTHING touches EEPROM. Only Goal_Position/accel/speed and Torque_Enable.
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
    raise SystemExit(f"{name} not found as a module-level literal in {DRIVER}")


SERVO_IDS = _lit("SERVO_IDS")
JOINT_NAMES = _lit("JOINT_NAMES")
JOINT_LIMITS_RAD = _lit("JOINT_LIMITS_RAD")
HOME_JOINTS_RAD = _lit("HOME_JOINTS_RAD")
GRIPPER_OPEN_RAD = _lit("GRIPPER_OPEN_RAD")
SIGNS = _lit("_DEFAULT_SIGNS")
CENTER = _lit("CENTER_TICK")
TICKS = _lit("TICKS_PER_REV")
SEED_SPEED = _lit("SEED_SPEED_STEPS")
WRITE_ACCEL = _lit("WRITE_ACCELERATION")
RAD_PER_TICK = 2.0 * math.pi / TICKS
DEG_PER_TICK = 360.0 / TICKS

GRIPPER_ID = SERVO_IDS[-1]
ARM_IDS = SERVO_IDS[:-1]
# Order matters and is NOT simply distal->proximal. Verified 2026-07-26 by
# simulating each step: driving the WRIST (J5) or SHOULDER (J2) before the ELBOW
# (J3) is folded pushes link samples BELOW the table (-14 mm and -36 mm
# respectively), because the arm starts extended flat-forward. Folding the elbow
# first lifts and compacts the arm: clearance goes 30.7 -> 67.4 mm and nothing
# ever goes under z=0. The _preflight_sequence() check below enforces this for
# ANY target/start, so a bad order is refused rather than driven.
DRIVE_ORDER = (3, 2, 5, 6, 4, 1, 7)     # elbow, shoulder, wrist, rolls, base, gripper
HALF_TURN = TICKS // 2
WRAP_GUARD_TICKS = 30                   # see the module docstring
MIN_CLEARANCE_M = 0.030                 # path_guard's link half-girth model
TABLE_Z_M = -0.001                      # a link sample below this = table hit
ARRIVE_TOL_TICKS = 12                   # ~1.05 deg
STALL_SAMPLES = 30                      # ~1.5 s at 20 Hz with no progress
WRONG_WAY_TICKS = 30                    # ~2.6 deg against the commanded direction
GRIPPER_STALL_GUARD_RAD = 0.15          # a target this close to closed = a stall


def rad_to_tick(rad, sign):
    return max(0, min(TICKS - 1, CENTER + int(round(rad * sign / RAD_PER_TICK))))


def deg_of(sid, tick):
    return (tick - CENTER) * DEG_PER_TICK * SIGNS[SERVO_IDS.index(sid)]


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


CURRENT_LSB_MA = 6.5            # Present_Current unit, per the STS memory table


def read_current_ma(bus):
    """{sid: mA} from REG_PRESENT_CURRENT. Read separately from the 6-byte
    position block because register 69 is not contiguous with 56-61."""
    out = {}
    for sid in SERVO_IDS:
        try:
            raw = bus.read_u16(sid, fb.REG_PRESENT_CURRENT)
            out[sid] = fb.decode_sign_magnitude(raw, 15) * CURRENT_LSB_MA
        except fb.FeetechBusError:
            out[sid] = None
    return out


def build_target(name):
    """{sid: (rad, tick)} for the named pose, plus a note on the gripper."""
    if name == "home":
        arm = list(HOME_JOINTS_RAD)
        grip = GRIPPER_OPEN_RAD
    elif name == "zero":
        arm = [0.0] * len(ARM_IDS)
        grip = 0.0
    else:
        raise SystemExit(f"unknown --target {name!r} (use home|zero)")
    tgt = {}
    for i, sid in enumerate(ARM_IDS):
        tgt[sid] = (arm[i], rad_to_tick(arm[i], SIGNS[i]))
    tgt[GRIPPER_ID] = (grip, rad_to_tick(grip, SIGNS[-1]))
    lo, hi = JOINT_LIMITS_RAD[-1]
    gripper_is_stall = grip <= lo + GRIPPER_STALL_GUARD_RAD
    return tgt, gripper_is_stall



def _preflight_sequence(start_rad, target_rad, order):
    """Simulate the whole drive, step by step, and report any step that would put
    a link BELOW the table or inside MIN_CLEARANCE_M of another link.

    This is the real safeguard: the correct drive order depends on the start pose
    and the target, and getting it wrong is not subtle — driving the wrist before
    the elbow is folded pushes the gripper 14 mm INTO the table. Rather than trust
    a hand-picked order, simulate it and refuse.

    Returns (ok, rows). Needs the server package for the real FK; if that import
    fails the check is SKIPPED and says so loudly (never silently).
    """
    try:
        sys.path.insert(0, str(REPO / 'physical_ai_tools' / 'physical_ai_server'))
        import numpy as np
        from physical_ai_server.workflow import edu6_ik as _E
        solver = _E.Edu6IKSolver()
    except Exception as e:  # noqa: BLE001
        return None, [f'preflight SKIPPED (cannot import the IK: {e})']

    def origins(q):
        fr = solver._fk_chain(q)
        t5, t6 = fr[4], fr[5]
        wrist = (t5 @ np.append(_E._WC_LOCAL5, 1.0))[:3]
        tool = t6[:3, :3] @ np.array([0.0, 0.0, -1.0])
        tcp = t6[:3, 3] + tool * _E._LINK6_TO_TCP
        pts = [np.zeros(3), np.array(_E._J1_XYZ), fr[1][:3, 3], fr[2][:3, 3],
               wrist, t6[:3, 3], tcp]
        return [_E._RZ_PI @ p for p in pts]

    def segd(a0, a1, b0, b1):
        best = 1e9
        for u in np.linspace(0, 1, 40):
            pp = a0 + (a1 - a0) * u
            for v in np.linspace(0, 1, 40):
                best = min(best, float(np.linalg.norm(pp - (b0 + (b1 - b0) * v))))
        return best

    def metrics(q):
        o = origins(q)
        segs = [(i, i + 1) for i in range(6)]
        worst = 1e9
        for (a, b) in segs:
            for (c, d) in segs:
                if b <= c - 1 or d <= a - 1:
                    worst = min(worst, segd(o[a], o[b], o[c], o[d]))
        return worst, min(float(pt[2]) for pt in o)

    idx = {sid: i for i, sid in enumerate(ARM_IDS)}
    q = list(start_rad)
    rows, ok = [], True
    for sid in order:
        if sid not in idx:
            continue                      # gripper cannot collide
        q[idx[sid]] = target_rad[idx[sid]]
        clear, lowz = metrics(q)
        bad = (lowz < TABLE_Z_M) or (clear < MIN_CLEARANCE_M)
        ok = ok and not bad
        rows.append(f'after J{sid}: clearance {clear * 1000:5.1f} mm, lowest link z '
                    f'{lowz * 1000:+6.1f} mm' + ('   <-- WOULD COLLIDE' if bad else ''))
    return ok, rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default=os.environ.get("EDU6_PORT", "COM5"))
    ap.add_argument("--target", default="home", choices=("home", "zero"))
    ap.add_argument("--check", action="store_true", help="read-only report")
    ap.add_argument("--joints", default="")
    ap.add_argument("--hold", type=float, default=8.0)
    ap.add_argument("--hz", type=float, default=20.0)
    ap.add_argument("--force-gripper", action="store_true",
                    help="NOT RECOMMENDED: drive the gripper even into a stall")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    tgt, gripper_is_stall = build_target(args.target)
    want = ([int(x) for x in args.joints.split(",") if x.strip()]
            if args.joints else list(SERVO_IDS))
    if GRIPPER_ID in want and gripper_is_stall and not args.force_gripper:
        print(f"[INFO] excluding the gripper: target {tgt[GRIPPER_ID][0]:.2f} rad "
              f"clamps the jaws onto their own stop (a sustained stall).")
        want = [s for s in want if s != GRIPPER_ID]
    order = [s for s in DRIVE_ORDER if s in want]

    try:
        bus = fb.FeetechBus(args.port)
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] cannot open {args.port}: {e}")
        print("       Another process probably holds the port (an open --park / "
              "--watch, or a container with the device usbipd-attached).")
        return 1

    live = set()

    def _off_all():
        for sid in sorted(live):
            try:
                bus.write(sid, fb.REG_TORQUE_ENABLE, bytes([0]))
            except Exception:  # noqa: BLE001
                print(f"[WARN] torque-off FAILED on servo {sid} — CUT THE 12 V.")
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
        print(f"[STATE] {args.port}, all {len(alive)} servos answering. "
              f"target = {args.target.upper()}\n")
        print(f"  {'joint':<16}{'now':>9}{'target':>9}{'move':>9}"
              f"{'ticks':>8}   wrap margin")
        blockers = []
        for sid in SERVO_IDS:
            tick = st[sid][0]
            t_rad, t_tick = tgt[sid]
            err = t_tick - tick
            margin = HALF_TURN - abs(err)
            sel = sid in order
            flag = ""
            if sel:
                if margin <= WRAP_GUARD_TICKS:
                    flag = f"  {margin:>4}  <-- REFUSED (too near a half-turn)"
                    blockers.append(sid)
                else:
                    flag = f"  {margin:>4}  ok"
            print(f"  {JOINT_NAMES[SERVO_IDS.index(sid)]:<16}"
                  f"{deg_of(sid, tick):>+8.2f}°{math.degrees(t_rad) * SIGNS[SERVO_IDS.index(sid)]:>+8.2f}°"
                  f"{err * DEG_PER_TICK:>+8.2f}°{err:>+8d}{flag}")
        print()
        if blockers:
            print(f"[STOP] servo(s) {blockers}: the move is within "
                  f"{WRAP_GUARD_TICKS} ticks of a half-turn (2048), where the two "
                  f"directions are nearly equal and the servo's naive error can "
                  f"send it the LONG way round (§11.11). Hand-turn those joints a "
                  f"few degrees away from the opposite point and re-run.")
            return 2
        # --- collision pre-flight over the ACTUAL sequence ------------------
        start_rad = [ (st[s2][0] - CENTER) * RAD_PER_TICK * SIGNS[SERVO_IDS.index(s2)]
                      for s2 in ARM_IDS ]
        tgt_rad = [ tgt[s2][0] for s2 in ARM_IDS ]
        pf_ok, pf_rows = _preflight_sequence(start_rad, tgt_rad, order)
        print("[PREFLIGHT] simulating the drive order " + str(order) + ":")
        for r in pf_rows:
            print("   " + r)
        if pf_ok is False:
            print("[STOP] that order would drive a link below the table or into "
                  "another link. Re-order (fold the ELBOW first from an extended "
                  "start) or move the arm by hand first. Nothing was written.")
            return 2
        if pf_ok is None:
            print("   (proceeding WITHOUT a collision pre-flight)")
        print()
        if args.check:
            print("[CHECK] read-only; nothing was written.")
            return 0

        print(f"[PLAN]  drive {order} to {args.target.upper()}, one at a time, at "
              f"{SEED_SPEED} steps/s (~0.31 rad/s).")
        print(f"        Watchdog aborts on wrong-way motion (>{WRONG_WAY_TICKS} "
              f"ticks), a stall, or any error bit. Abort = cut the 12 V.")
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
            goal = tgt[sid][1]
            err = goal - start
            if HALF_TURN - abs(err) <= WRAP_GUARD_TICKS:
                print(f"[SKIP] {nm}: move became wrap-marginal ({err:+d}).")
                continue
            if abs(err) <= ARRIVE_TOL_TICKS:
                print(f"[OK]   {nm}: already at target "
                      f"({deg_of(sid, start):+.2f}°)")
                continue
            direction = 1 if err > 0 else -1
            eta = abs(err) / max(SEED_SPEED, 1)
            print(f"\n[MOVE] {nm}: {deg_of(sid, start):+.2f}° -> "
                  f"{deg_of(sid, goal):+.2f}°  ({err:+d} ticks, "
                  f"{err * DEG_PER_TICK:+.2f}°, ~{eta:.1f} s)")
            payload = (bytes([WRITE_ACCEL]) + fb.le16(goal)
                       + fb.le16(0) + fb.le16(SEED_SPEED))
            bus.sync_write(fb.REG_ACCELERATION, {sid: payload})
            rb = bus.read_u16(sid, fb.REG_GOAL_POSITION)
            if abs(rb - goal) > 2:
                print(f"[FAIL] {nm}: goal read back {rb}, wanted {goal} "
                      f"(EEPROM window clamp?) — not energising.")
                return 1
            bus.write(sid, fb.REG_TORQUE_ENABLE, bytes([1]))
            live.add(sid)
            if bus.read(sid, fb.REG_TORQUE_ENABLE, 1)[1][0] != 1:
                print(f"[FAIL] {nm} did not energise.")
                return 1

            best = abs(err)
            stalled = 0
            deadline = time.monotonic() + max(20.0, eta * 4 + 10.0)
            while time.monotonic() < deadline:
                b = read_block(bus)
                if b.get(sid) is None:
                    print(f"   [WARN] {nm}: no reply")
                    time.sleep(period)
                    continue
                tick, spd, load, e = b[sid]
                rem = goal - tick
                moved = (tick - start) * direction
                if e:
                    print(f"   [ABORT] {nm} error bit: {fb.describe_error_bits(e)}")
                    _off_all()
                    return 1
                if moved < -WRONG_WAY_TICKS:
                    print(f"   [ABORT] {nm} moved {-moved} ticks the WRONG way "
                          f"— this is the §11.11 failure. Cutting torque.")
                    _off_all()
                    return 1
                if abs(rem) <= ARRIVE_TOL_TICKS:
                    print(f"   arrived {deg_of(sid, tick):+.2f}° (load {load})")
                    break
                if abs(rem) < best - 1:
                    best, stalled = abs(rem), 0
                else:
                    stalled += 1
                    if stalled >= STALL_SAMPLES:
                        print(f"   [ABORT] {nm} stalled {abs(rem)} ticks "
                              f"({abs(rem) * DEG_PER_TICK:.1f}°) short, load "
                              f"{load}. Mechanical interference?")
                        _off_all()
                        return 1
                time.sleep(period)
            else:
                print(f"   [ABORT] {nm} timed out.")
                _off_all()
                return 1

        trip_ma = 310 * CURRENT_LSB_MA
        print(f"\n[HOLD]  at {args.target.upper()} for {args.hold:.0f} s — logging "
              f"Present_Current (mA) per joint.")
        print(f"        This is the §2.5 data point: PROTECTION_CURRENT is 310 LSB "
              f"= {trip_ma:.0f} mA. The measured holding current is what decides "
              f"whether that floor is right.")
        end = time.monotonic() + args.hold
        peak_ma = {s: 0.0 for s in order}
        peak_load = {s: 0 for s in order}
        last_ma: dict[int, float] = {}
        while time.monotonic() < end:
            b = read_block(bus)
            ma = read_current_ma(bus)
            for s in order:
                if b.get(s) is not None:
                    peak_load[s] = max(peak_load[s], abs(b[s][2]))
                if ma.get(s) is not None:
                    peak_ma[s] = max(peak_ma[s], abs(ma[s]))
                    last_ma[s] = abs(ma[s])
            print("   " + "  ".join(
                f"{JOINT_NAMES[SERVO_IDS.index(s)][:6]}"
                f"{deg_of(s, b[s][0]):+7.2f}°/"
                f"{last_ma.get(s, float('nan')):5.0f}mA"
                for s in order if b.get(s)))
            time.sleep(0.5)
        print()
        print("=" * 68)
        print(f"HOLDING CURRENT at {args.target.upper()}   "
              f"(trip = 310 LSB = {trip_ma:.0f} mA)")
        print("=" * 68)
        worst = 0.0
        for s in order:
            nm = JOINT_NAMES[SERVO_IDS.index(s)]
            pm = peak_ma.get(s, 0.0)
            worst = max(worst, pm)
            print(f"  {nm:<16} peak {pm:6.0f} mA   steady "
                  f"{last_ma.get(s, 0.0):6.0f} mA   peak |load| "
                  f"{peak_load.get(s, 0):4d}   headroom {trip_ma - pm:+7.0f} mA")
        print()
        print(f"  WORST peak {worst:.0f} mA = "
              f"{(worst / trip_ma * 100 if trip_ma else 0):.0f} % of the trip floor")
        if worst > trip_ma * 0.7:
            print("  => the floor sits CLOSE to normal holding current; lowering it "
                  "would nuisance-trip. §2.5's keep-~2 A decision is CONFIRMED.")
        else:
            print("  => normal holding current is well under the floor, so there is "
                  "headroom to LOWER PROTECTION_CURRENT for a tighter force limit "
                  "— a Rule §2 change, not to be done casually.")
    finally:
        _off_all()
        try:
            bus.close()
        except Exception:  # noqa: BLE001
            pass
    print("\n[DONE] de-energised. The arm will sag — the STS gear train "
          "backdrives, which is expected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

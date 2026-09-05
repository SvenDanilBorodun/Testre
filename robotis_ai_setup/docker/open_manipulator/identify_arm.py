#!/usr/bin/env python3
"""Identify what arm a serial port is connected to.

Two protocols (selected via ``--protocol``, default ``dxl`` = today's
behaviour, byte-identical):

* ``dxl`` — ROBOTIS OMX arms. Servo IDs verified from xacro files
  (omx_l: leader 1-6; omx_f: follower 11-16), Dynamixel Protocol 2.0.
* ``feetech`` — an EduBotics Feetech STS arm: ping of the contiguous servo
  ids from 1 up + Model_Number in the accepted STS set (777 STS3215, 2825
  STS3250 on the high-load shoulder + elbow — mixed by design; identity proven
  by the SERVOS answering, not the CH343 bridge chip — every arm family
  enumerates as /dev/ttyACM*).

  ``--servos=N`` says how many the SELECTED robot type has (7 = edu6_studio,
  6 = edu1_studio; default 7 so a pre-edu1 caller is byte-identical). THE COUNT
  IS THE ONLY DISCRIMINATOR between the two Feetech arms: same Waveshare CH343P
  bridge, same VID:PID, same by-id strings, and the same servo models on ids
  1..6. So the probe asserts the count EXACTLY — every id up to N must answer
  AND id N+1 must not — and reports a bus of a DIFFERENT supported length as
  that family's cross-probe token rather than as a success.

Cross-probe: when the EXPECTED protocol finds nothing, the OTHER one is probed
and a distinct token returned ("omx_arm_found" / "edu6_arm_found" /
"edu1_arm_found") so the GUI can say „Es wurde ein OMX-Arm gefunden, aber ein
EduBotics-Arm ist ausgewählt." instead of a bare failure.

Baudrate: 1,000,000 for all arms.
"""
import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dynamixel_sdk import PacketHandler, PortHandler

BAUDRATE = 1_000_000
PROTOCOL = 2.0
LEADER_IDS = [1, 2, 3, 4, 5, 6]
FOLLOWER_IDS = [11, 12, 13, 14, 15, 16]
TIMEOUT_SECONDS = 10


def _safe_ping(pkt, port, servo_id: int) -> bool:
    """Ping a servo, returning True on success. Catches exceptions from flaky serial."""
    try:
        _, comm_result, _ = pkt.ping(port, servo_id)
        return comm_result == 0
    except Exception:
        return False


def identify(port_path: str) -> str:
    port = PortHandler(port_path)
    if not port.openPort():
        return "error:cannot_open"
    port.setBaudRate(BAUDRATE)
    pkt = PacketHandler(PROTOCOL)

    try:
        leader_count = sum(1 for sid in LEADER_IDS if _safe_ping(pkt, port, sid))
        follower_count = sum(1 for sid in FOLLOWER_IDS if _safe_ping(pkt, port, sid))
    except Exception as e:
        port.closePort()
        return f"error:{e}"

    port.closePort()

    if leader_count > follower_count:
        return "leader"
    elif follower_count > leader_count:
        return "follower"
    return "unknown"


def _timeout_handler(signum, frame):
    print("error:timeout", flush=True)
    sys.exit(1)


# Servo count per Feetech arm family, and its inverse. These are the ONLY facts
# that separate the two: the bridge chip, the VID:PID, the by-id string and the
# models on ids 1..6 are all identical.
FEETECH_SERVO_COUNT = {"edu6": 7, "edu1": 6}
FEETECH_FAMILY_BY_COUNT = {n: fam for fam, n in FEETECH_SERVO_COUNT.items()}
DEFAULT_FEETECH_SERVOS = FEETECH_SERVO_COUNT["edu6"]
FEETECH_MAX_SERVOS = max(FEETECH_SERVO_COUNT.values())
# Probe one id PAST the longest supported arm, so "exactly N" is provable.
FEETECH_MAX_ID = FEETECH_MAX_SERVOS + 1


def feetech_bus_length(bus) -> int:
    """Number of CONTIGUOUS servo ids answering from 1 upward.

    Contiguous, not a count of hits: the arms number their servos 1..N in joint
    order, so a gap is a mid-chain cable/power fault and everything past it is
    unreachable anyway. Stopping at the first silent id also keeps the probe
    cheap (one extra ping) instead of walking the whole id space.
    """
    n = 0
    for sid in range(1, FEETECH_MAX_ID + 1):
        if not bus.ping(sid):
            break
        n += 1
    return n


def identify_feetech(port_path: str, expected: int = DEFAULT_FEETECH_SERVOS) -> str:
    """Feetech probe: EXACTLY ``expected`` servos answer from id 1 up, and each
    reads an accepted STS model (777 STS3215 / 2825 STS3250 — both arms mix the
    two by design).

    Returns the family name ("edu6" / "edu1") on success, or one of:

    * ``feetech_silent``  — the port opened but NO servo answered. The single
      most likely student error is the 12-V supply being off (USB alone
      enumerates the port but never powers the servos), so it gets its own
      token rather than a bare "unknown".
    * ``edu6_arm_found`` / ``edu1_arm_found`` — the bus is exactly the length of
      a DIFFERENT supported arm. Usually the wrong robot type is selected;
      note the one ambiguity, which the caller's German wording must own: an
      edu6 whose LAST servo has dropped off the chain is indistinguishable from
      a healthy edu1, because they are the same six servos.
    * ``bus_too_long:K``  — K contiguous ids answered, MORE than the longest
      supported arm has. Its own token rather than ``partial:K`` because it is
      the OPPOSITE fault and needs the opposite remedy: ``partial`` means
      "servos are missing from the chain, check the connectors", while this
      means "there are extra devices on the bus" (a second arm daisy-chained, a
      duplicated servo id, a stray board). Folding it into ``partial`` made the
      host print „Nur 8 von 7 Servos antworten" — a fraction greater than one,
      telling the student to re-seat cables on a bus that has too many devices.
      K is a FLOOR, not a count: the walk stops at ``FEETECH_MAX_ID``, so
      "K answered" really means "at least K".
    * ``partial:K``       — K servos answered, fewer than the selected arm has
      and matching no other supported arm.
    * ``unknown``         — right length, but something is not an STS servo.
    """
    try:
        import feetech_bus as fb
    except ImportError:
        return "error:feetech_bus_missing"
    try:
        bus = fb.FeetechBus(port_path)
    except Exception:
        return "error:cannot_open"
    try:
        alive = feetech_bus_length(bus)
        if alive == 0:
            return "feetech_silent"
        if alive > FEETECH_MAX_SERVOS:
            # More devices than ANY supported arm has — never a missing-servo
            # fault, so never `partial:` (which the host renders as a fraction
            # of the selected arm's length and would print "8 of 7").
            return f"bus_too_long:{alive}"
        if alive != expected:
            other = FEETECH_FAMILY_BY_COUNT.get(alive)
            return f"{other}_arm_found" if other else f"partial:{alive}"
        for sid in range(1, expected + 1):
            try:
                if bus.read_u16(sid, fb.REG_MODEL_NUMBER) not in fb.STS_ACCEPTED_MODELS:
                    return "unknown"
            except fb.FeetechBusError:
                return "unknown"
        # ``expected`` is validated against this table by the CLI before we
        # ever get here, so the fallback is unreachable in production and
        # exists only for a direct in-process call.
        return FEETECH_FAMILY_BY_COUNT.get(expected, "edu6")
    except Exception as e:
        return f"error:{e}"
    finally:
        bus.close()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    protocol = "dxl"
    servos = DEFAULT_FEETECH_SERVOS
    for a in sys.argv[1:]:
        if a.startswith("--protocol"):
            protocol = a.split("=", 1)[1] if "=" in a else "feetech"
        elif a.startswith("--servos="):
            # A malformed or unsupported count must not silently probe the WRONG
            # length — that would report an edu1 as an edu6 (or the reverse)
            # with full confidence, and the success token would name a family
            # nothing can act on. Fail loudly instead; the host always passes a
            # value derived from its own family table.
            try:
                servos = int(a.split("=", 1)[1])
            except ValueError:
                print(f"error:bad_servo_count:{a}", file=sys.stderr)
                sys.exit(2)
            if servos not in FEETECH_FAMILY_BY_COUNT:
                print(f"error:unsupported_servo_count:{servos}", file=sys.stderr)
                sys.exit(2)
    if len(args) != 1:
        print(f"Usage: {sys.argv[0]} <serial_port_path> "
              f"[--protocol=dxl|feetech] [--servos=N]", file=sys.stderr)
        sys.exit(1)

    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(TIMEOUT_SECONDS)

    if protocol == "feetech":
        expected_family = FEETECH_FAMILY_BY_COUNT.get(servos)
        result = identify_feetech(args[0], servos)
        if result in ("unknown", "feetech_silent") or result.startswith("partial"):
            # cross-probe: is an OMX arm plugged in instead? — this also covers
            # the silent bus: an OMX board answers Protocol-2.0 pings.
            # Neither a *_arm_found NOR a bus_too_long result is re-probed: the
            # Feetech bus already answered, so it is definitely not an OMX
            # board, and a cross-probe would replace a precise diagnosis with a
            # vaguer one.
            other = identify(args[0])
            if other in ("leader", "follower"):
                result = "omx_arm_found"
        print(result)
        sys.exit(0 if result == expected_family else 1)

    result = identify(args[0])
    if result == "unknown":
        # cross-probe: is a Feetech arm plugged in instead? Probe by LENGTH so
        # either family is named — an OMX rig with an Edu:1 attached must not be
        # told to select the 6-axis arm.
        feet = identify_feetech(args[0], servos)
        if feet in FEETECH_SERVO_COUNT:
            result = f"{feet}_arm_found"
        elif feet.endswith("_arm_found"):
            result = feet
    print(result)
    sys.exit(0 if result in ("leader", "follower") else 1)

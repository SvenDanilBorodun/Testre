#!/usr/bin/env python3
"""Identify what arm a serial port is connected to.

Two protocols (selected via ``--protocol``, default ``dxl`` = today's
behaviour, byte-identical):

* ``dxl`` — ROBOTIS OMX arms. Servo IDs verified from xacro files
  (omx_l: leader 1-6; omx_f: follower 11-16), Dynamixel Protocol 2.0.
* ``feetech`` — the edu6_studio Feetech arm: broadcast-style ping of IDs 1..7
  + Model_Number in the accepted STS set (777 STS3215 on joints 1/4/5/6/7,
  2825 STS3250 on joints 2/3 — mixed by design; identity proven by the SERVOS
  answering, not the CH343 bridge chip — both arm families enumerate as
  /dev/ttyACM*).

Cross-probe (edu6 plan §5.4): when the EXPECTED protocol finds nothing, the
OTHER one is probed and a distinct token returned ("omx_arm_found" /
"edu6_arm_found") so the GUI can say „Es wurde ein OMX-Arm gefunden, aber
‚EduBotics 6-Achs' ist ausgewählt." instead of a bare failure.

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


EDU6_IDS = [1, 2, 3, 4, 5, 6, 7]


def identify_feetech(port_path: str) -> str:
    """edu6 probe: every servo 1..7 answers AND reads an accepted STS model
    (777 STS3215 / 2825 STS3250 — the arm mixes both by design)."""
    try:
        import feetech_bus as fb
    except ImportError:
        return "error:feetech_bus_missing"
    try:
        bus = fb.FeetechBus(port_path)
    except Exception:
        return "error:cannot_open"
    try:
        alive = [sid for sid in EDU6_IDS if bus.ping(sid)]
        if len(alive) != len(EDU6_IDS):
            # Port opened but ZERO servos answer: the single most likely student
            # error is the 12-V supply being off (USB alone enumerates the port
            # but never powers the servos). Give it a DISTINCT token so the GUI
            # can say so, instead of a bare "unknown". A PARTIAL bus (some answer)
            # keeps its own token.
            return "feetech_silent" if not alive else f"partial:{len(alive)}"
        for sid in EDU6_IDS:
            try:
                if bus.read_u16(sid, fb.REG_MODEL_NUMBER) not in fb.STS_ACCEPTED_MODELS:
                    return "unknown"
            except fb.FeetechBusError:
                return "unknown"
        return "edu6"
    except Exception as e:
        return f"error:{e}"
    finally:
        bus.close()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    protocol = "dxl"
    for a in sys.argv[1:]:
        if a.startswith("--protocol"):
            protocol = a.split("=", 1)[1] if "=" in a else "feetech"
    if len(args) != 1:
        print(f"Usage: {sys.argv[0]} <serial_port_path> [--protocol=dxl|feetech]",
              file=sys.stderr)
        sys.exit(1)

    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(TIMEOUT_SECONDS)

    if protocol == "feetech":
        result = identify_feetech(args[0])
        if result in ("unknown", "feetech_silent") or result.startswith("partial"):
            # cross-probe: is an OMX arm plugged in instead? (§5.4) — this also
            # covers the silent bus: an OMX board answers Protocol-2.0 pings.
            other = identify(args[0])
            if other in ("leader", "follower"):
                result = "omx_arm_found"
        print(result)
        sys.exit(0 if result == "edu6" else 1)

    result = identify(args[0])
    if result == "unknown":
        # cross-probe: is the edu6 Feetech arm plugged in instead? (§5.4)
        if identify_feetech(args[0]) == "edu6":
            result = "edu6_arm_found"
    print(result)
    sys.exit(0 if result in ("leader", "follower") else 1)

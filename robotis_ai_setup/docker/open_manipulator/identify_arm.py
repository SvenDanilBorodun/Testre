#!/usr/bin/env python3
"""Identify whether a serial port is connected to a leader or follower arm.

Servo IDs verified from xacro files:
  - omx_l.ros2_control.xacro: Leader IDs 1-6
  - omx_f.ros2_control.xacro: Follower IDs 11-16
Baudrate: 1,000,000 for both arms. Dynamixel Protocol 2.0.
"""
import signal
import sys

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


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <serial_port_path>", file=sys.stderr)
        sys.exit(1)

    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(TIMEOUT_SECONDS)

    result = identify(sys.argv[1])
    print(result)
    sys.exit(0 if result in ("leader", "follower") else 1)

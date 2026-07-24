#!/usr/bin/env python3
"""Minimal Feetech STS3215 half-duplex serial bus (Dynamixel-1.0-style framing).

CLEAN-ROOM implementation written for EduBotics from the documented STS3215
register map + wire format (edu6 plan §2.5) — deliberately NOT vendored from
``feetech-servo-sdk`` (a 2022 third-party PyPI republish whose packet-timeout
maths LeRobot has to monkeypatch, under a republisher-granted license) and NOT
from LeRobot (whose baud table is wrong at indices 5–7). Every framing rule
below is pinned by deps-free unit tests (``tests/test_feetech_bus.py``) and the
undocumented servo behaviours are bench-verified at rig gate R2.

Wire format (Dynamixel Protocol-1.0 style, 8N1):

* instruction: ``FF FF id len instr params… checksum`` with
  ``len = n_params + 2`` and ``checksum = ~(id + len + instr + Σparams) & 0xFF``
* status:      ``FF FF id len error params… checksum``
* instructions: PING 0x01, READ 0x02, WRITE 0x03, SYNC_READ 0x82 (Feetech
  extension: every listed id answers with its own status packet, in list
  order), SYNC_WRITE 0x83. BROADCAST_ID 0xFE.
* 16-bit registers are LITTLE-endian (STS/SMS convention; the SCS series is
  big-endian — do not copy code across).
* Signed registers are SIGN-MAGNITUDE, not two's complement: the sign lives in
  a register-specific bit (Present_Load bit 10, Homing_Offset bit 11,
  positions/speeds bit 15) — a −5 arrives as ``bit | 5``.

The error byte of every status reply is free telemetry:
``VOLTAGE=1, ANGLE=2, OVERHEAT=4, OVERCURRENT=8, OVERLOAD=32``.
"""

from __future__ import annotations

import math
import time
from typing import Optional

# ── register map (STS3215; EEPROM unless noted) ──────────────────────────────
REG_FIRMWARE_MAJOR = 0        # R, firmware major.minor at addr 0/1
REG_MODEL_NUMBER = 3          # R, 2 bytes — 777 = STS3215, 2825 = STS3250
REG_ID = 5
REG_BAUD_RATE = 6             # 0 = 1 Mbps (factory default)
REG_RETURN_DELAY = 7          # status-return delay time (provisioned to 0)
REG_RESPONSE_STATUS_LEVEL = 8  # 1 = ack every instruction (factory); 0 = reply
#                                only to READ/PING — a 0 makes every awaited
#                                WRITE time out, so provisioning normalizes it
#                                to 1 BEFORE the first awaited write.
REG_MIN_POSITION_LIMIT = 9    # 2 bytes
REG_MAX_POSITION_LIMIT = 11   # 2 bytes
REG_MAX_TEMPERATURE = 13
REG_MAX_VOLTAGE = 14
REG_MIN_VOLTAGE = 15
REG_MAX_TORQUE_LIMIT = 16     # 2 bytes — copied into REG_TORQUE_LIMIT at power-on (R2-verify)
REG_PHASE = 18
REG_UNLOAD_CONDITION = 19
REG_PROTECTION_CURRENT = 28   # 2 bytes, ×6.5 mA
REG_HOMING_OFFSET = 31        # 2 bytes, sign bit 11
REG_OPERATING_MODE = 33       # 0 = position
REG_PROTECTIVE_TORQUE = 34
REG_PROTECTION_TIME = 35
REG_OVERLOAD_TORQUE = 36
REG_TORQUE_ENABLE = 40        # RAM: 0 off, 1 on, 128 = set current pos as 2048
REG_ACCELERATION = 41         # RAM
REG_GOAL_POSITION = 42        # RAM, 2 bytes
REG_GOAL_TIME = 44            # RAM, 2 bytes (documented no-op on ST/STS — never rely on it)
REG_GOAL_SPEED = 46           # RAM, 2 bytes
REG_TORQUE_LIMIT = 48         # RAM, 2 bytes (volatile — REG_MAX_TORQUE_LIMIT is the floor)
REG_LOCK = 55                 # RAM: 0 = EEPROM writes persist, 1 = protected (inverted!)
REG_PRESENT_POSITION = 56     # R, 2 bytes, sign bit 15
REG_PRESENT_SPEED = 58        # R, 2 bytes, steps/s, sign bit 15
REG_PRESENT_LOAD = 60         # R, 2 bytes, sign bit 10
REG_PRESENT_VOLTAGE = 62      # R, 1 byte, ×0.1 V
REG_PRESENT_TEMPERATURE = 63  # R, 1 byte, °C
REG_STATUS = 65               # R, 1 byte (error flags)
REG_PRESENT_CURRENT = 69      # R, 2 bytes, ×6.5 mA

STS3215_MODEL_NUMBER = 777
STS3250_MODEL_NUMBER = 2825
# The edu6 arm mixes STS servo models BY DESIGN: joints 1/4/5/6/7 are STS3215
# (Model 777) and the high-load shoulder + elbow (joints 2/3) are STS3250
# (Model 2825, bench-confirmed 2026-07-24). Both are STS-series: identical
# Protocol-1.0 framing, little-endian words, sign-magnitude bits, and 4096-tick
# single-turn resolution — so every register/tick math in this module is
# model-independent. Identity checks (provision, boot probe, scan) accept the
# SET; the exact per-servo model is recorded for traceability. A servo whose
# model is in NEITHER entry is a genuinely wrong device (or an OMX Dynamixel
# answering garbage) and is refused. Widen this set only for a real new servo.
STS_ACCEPTED_MODELS = frozenset({STS3215_MODEL_NUMBER, STS3250_MODEL_NUMBER})
STS_MODEL_NAMES = {STS3215_MODEL_NUMBER: 'STS3215', STS3250_MODEL_NUMBER: 'STS3250'}
BROADCAST_ID = 0xFE

# Servo position geometry (single-turn absolute encoder).
CENTER_TICK = 2048
TICKS_PER_REV = 4096
_RAD_PER_TICK = 2.0 * math.pi / TICKS_PER_REV

INSTR_PING = 0x01
INSTR_READ = 0x02
INSTR_WRITE = 0x03
INSTR_SYNC_READ = 0x82
INSTR_SYNC_WRITE = 0x83

ERR_VOLTAGE = 0x01
ERR_ANGLE = 0x02
ERR_OVERHEAT = 0x04
ERR_OVERCURRENT = 0x08
ERR_OVERLOAD = 0x20

ERROR_BITS_DE = (
    (ERR_VOLTAGE, 'Spannungsfehler'),
    (ERR_ANGLE, 'Winkel-Sensorfehler'),
    (ERR_OVERHEAT, 'Überhitzung'),
    (ERR_OVERCURRENT, 'Überstrom'),
    (ERR_OVERLOAD, 'Überlast'),
)


def checksum(payload: bytes) -> int:
    """``~(sum of id..params) & 0xFF`` — the byte that closes every packet."""
    return (~sum(payload)) & 0xFF


def build_packet(servo_id: int, instr: int, params: bytes = b'') -> bytes:
    body = bytes([servo_id, len(params) + 2, instr]) + params
    return b'\xff\xff' + body + bytes([checksum(body)])


def le16(value: int) -> bytes:
    """16-bit little-endian (STS/SMS byte order)."""
    return bytes([value & 0xFF, (value >> 8) & 0xFF])


def from_le16(lo: int, hi: int) -> int:
    return (hi << 8) | lo


def decode_sign_magnitude(raw: int, sign_bit: int) -> int:
    """Sign-magnitude decode: the sign lives in ``sign_bit`` (not two's
    complement — a −5 with bit 15 arrives as 0x8005)."""
    mask = 1 << sign_bit
    if raw & mask:
        return -(raw & (mask - 1))
    return raw


def encode_sign_magnitude(value: int, sign_bit: int) -> int:
    if value < 0:
        return (1 << sign_bit) | (-value)
    return value


def position_limit_window(lo_rad: float, hi_rad: float, sign: int) -> tuple[int, int]:
    """Designed URDF limit pair → the servo's EEPROM ``Min/Max_Position_Limit``
    window around the designed zero (``CENTER_TICK``), sign-aware: a −1
    direction convention mirrors the window and re-orders lo ≤ hi. Clamped to
    the single-turn register range [0, TICKS_PER_REV−1]. ONE shared
    implementation for the provisioning tool (which WRITES the window) and the
    driver node's boot probe (which VERIFIES it against the plugged arm) —
    the two must never drift (audit H1)."""
    a = CENTER_TICK + int(round(lo_rad * sign / _RAD_PER_TICK))
    b = CENTER_TICK + int(round(hi_rad * sign / _RAD_PER_TICK))
    lo, hi = (a, b) if a <= b else (b, a)
    return max(0, lo), min(TICKS_PER_REV - 1, hi)


def describe_error_bits(error: int) -> str:
    """German short list of the set error flags ('' when clean)."""
    names = [name for bit, name in ERROR_BITS_DE if error & bit]
    return ', '.join(names)


class FeetechBusError(Exception):
    """Raised on a broken/absent reply (framing, checksum, timeout)."""


class FeetechBus:
    """One serial port, many STS3215 servos. NOT thread-safe — the caller
    (edu6_arm_node) serializes access with its own lock."""

    def __init__(self, port: str, baudrate: int = 1_000_000,
                 timeout_s: float = 0.02, serial_factory=None) -> None:
        if serial_factory is None:
            import serial  # pyserial ships with dynamixel_sdk in the image
            # write_timeout: a wedged CDC-ACM endpoint must raise instead of
            # blocking forever while holding the caller's bus lock (audit L4 —
            # a hung write would make even the shutdown torque-off unreachable).
            self._ser = serial.Serial(
                port=port, baudrate=baudrate, timeout=timeout_s,
                write_timeout=0.1)
        else:
            self._ser = serial_factory(port, baudrate, timeout_s)
        self._timeout_s = timeout_s

    def close(self) -> None:
        try:
            self._ser.close()
        except Exception:  # noqa: BLE001 — best-effort
            pass

    # ── low level ────────────────────────────────────────────────────────────
    def _write_packet(self, pkt: bytes) -> None:
        try:
            self._ser.reset_input_buffer()
        except Exception:  # noqa: BLE001 — not all fakes implement it
            pass
        self._ser.write(pkt)

    def _read_status(self, deadline: float) -> tuple[int, int, bytes]:
        """Read one status packet → ``(servo_id, error, params)``. Raises
        :class:`FeetechBusError` on timeout/framing/checksum."""
        buf = b''
        # hunt the FF FF header
        while True:
            if time.monotonic() > deadline:
                raise FeetechBusError('timeout waiting for status header')
            b1 = self._ser.read(1)
            if not b1:
                continue
            buf = (buf + b1)[-2:]
            if buf == b'\xff\xff':
                break
        head = self._read_exact(3, deadline)   # id, len, error
        servo_id, length, error = head[0], head[1], head[2]
        if length < 2 or length > 250:
            raise FeetechBusError(f'bad length byte {length}')
        rest = self._read_exact(length - 1, deadline)  # params + checksum
        params, cks = rest[:-1], rest[-1]
        expect = checksum(bytes([servo_id, length, error]) + params)
        if cks != expect:
            raise FeetechBusError(
                f'checksum mismatch (got {cks:#04x}, want {expect:#04x})')
        return servo_id, error, params

    def _read_exact(self, n: int, deadline: float) -> bytes:
        out = b''
        while len(out) < n:
            if time.monotonic() > deadline:
                raise FeetechBusError(f'timeout ({len(out)}/{n} bytes)')
            chunk = self._ser.read(n - len(out))
            if chunk:
                out += chunk
        return out

    # ── instructions ─────────────────────────────────────────────────────────
    def ping(self, servo_id: int, timeout_s: Optional[float] = None) -> bool:
        self._write_packet(build_packet(servo_id, INSTR_PING))
        deadline = time.monotonic() + (timeout_s or self._timeout_s)
        try:
            rid, _err, _p = self._read_status(deadline)
            return rid == servo_id
        except FeetechBusError:
            return False

    def read(self, servo_id: int, addr: int, length: int) -> tuple[int, bytes]:
        """READ → ``(error_byte, data)``. Raises on no/bad reply."""
        self._write_packet(build_packet(
            servo_id, INSTR_READ, bytes([addr, length])))
        deadline = time.monotonic() + self._timeout_s
        rid, error, params = self._read_status(deadline)
        if rid != servo_id or len(params) != length:
            raise FeetechBusError(
                f'unexpected reply id={rid} len={len(params)} (want '
                f'id={servo_id} len={length})')
        return error, params

    def read_u16(self, servo_id: int, addr: int) -> int:
        _err, data = self.read(servo_id, addr, 2)
        return from_le16(data[0], data[1])

    def write(self, servo_id: int, addr: int, data: bytes,
              await_status: bool = True) -> int:
        """WRITE → the reply's error byte (0 for broadcast/no-status)."""
        self._write_packet(build_packet(
            servo_id, INSTR_WRITE, bytes([addr]) + data))
        if servo_id == BROADCAST_ID or not await_status:
            return 0
        deadline = time.monotonic() + self._timeout_s
        rid, error, _params = self._read_status(deadline)
        if rid != servo_id:
            raise FeetechBusError(f'unexpected reply id={rid}')
        return error

    def sync_write(self, addr: int, per_servo: dict[int, bytes]) -> None:
        """SYNC_WRITE (broadcast, no replies). All values must share a length."""
        if not per_servo:
            return
        lengths = {len(v) for v in per_servo.values()}
        if len(lengths) != 1:
            raise ValueError('sync_write values must share one length')
        dlen = lengths.pop()
        params = bytes([addr, dlen])
        for sid in sorted(per_servo):
            params += bytes([sid]) + per_servo[sid]
        self._write_packet(build_packet(BROADCAST_ID, INSTR_SYNC_WRITE, params))

    def sync_read(self, addr: int, length: int,
                  ids: list[int]) -> dict[int, tuple[int, bytes]]:
        """Feetech SYNC_READ (0x82): one request, each listed servo answers
        with its own status packet. Returns ``{id: (error, data)}`` for every
        servo that REPLIED — the caller must check for missing ids (a silent
        per-id miss is exactly how a browned-out servo hides)."""
        params = bytes([addr, length]) + bytes(ids)
        self._write_packet(build_packet(BROADCAST_ID, INSTR_SYNC_READ, params))
        out: dict[int, tuple[int, bytes]] = {}
        deadline = time.monotonic() + self._timeout_s * (1 + len(ids))
        for _ in ids:
            try:
                rid, error, data = self._read_status(deadline)
            except FeetechBusError:
                break   # missing tail replies → reported as absent ids
            if len(data) == length:
                out[rid] = (error, data)
        return out

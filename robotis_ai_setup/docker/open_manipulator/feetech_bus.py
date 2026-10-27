#!/usr/bin/env python3
"""Minimal Feetech STS3215 half-duplex serial bus (Dynamixel-1.0-style framing).

CLEAN-ROOM implementation written for EduBotics from the documented STS3215
register map + wire format — deliberately NOT vendored from
``feetech-servo-sdk`` (a 2022 third-party PyPI republish whose packet-timeout
maths LeRobot has to monkeypatch, under a republisher-granted license) and NOT
from LeRobot (whose baud table is wrong at indices 5–7). Every framing rule
below is pinned by deps-free unit tests (``tests/test_feetech_bus.py``) and the
undocumented servo behaviours are bench-verified on the rig.

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

The error byte of every status reply is free telemetry — see ERROR_BITS_DE for
the bit table and for the one bit whose vendor NAME differs across doc
revisions. Every set bit is reported, named or not (see describe_error_bits).
"""

from __future__ import annotations

import math
import time
from typing import Optional

# ── register map (STS3215; EEPROM unless noted) ──────────────────────────────
REG_FIRMWARE_MAJOR = 0        # R, firmware major.minor at addr 0/1
REG_MODEL_NUMBER = 3          # R, 2 bytes — 777 = STS3215, 2825 = STS3250
REG_ID = 5
REG_BAUD_RATE = 6             # 0 = 1 Mbps (factory default); see BAUD_RATES
REG_RETURN_DELAY = 7          # OLD table: status-return delay (provisioned to
#                               0); NEW table: reserved
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
REG_LED_ALARM = 20
REG_P_GAIN = 21
REG_D_GAIN = 22
REG_I_GAIN = 23
REG_MIN_STARTUP_FORCE = 24
REG_25 = 25                   # NEW firmware: integral limit; OLD: high byte of a 2-byte 24
REG_CW_DEADZONE = 26
REG_CCW_DEADZONE = 27
REG_PROTECTION_CURRENT = 28   # 2 bytes, ×6.5 mA
REG_ANGULAR_RESOLUTION = 30   # EEPROM, 1 byte, factory 1 (OLD range 1..3, NEW 1..128) — see below
# Homing_Offset, 2 bytes. The vendor's CHINESE memory table (row 31) is a 13-bit
# encoding: 0–2047 → 0…+2047, 2048–4095 → 0…−2047 (bit 11 = sign),
# 4096–6143 → +2048…+4095, 6144–8191 → −2048…−4095 (bit 12 = +2048). The English
# page mistranslates the third range. Our tools only ever write |offset| ≤ 2047,
# so bits 12–15 set mean a FOREIGN tool wrote it — see decode_homing_offset.
REG_HOMING_OFFSET = 31
REG_OPERATING_MODE = 33       # 0 = position
REG_PROTECTIVE_TORQUE = 34
REG_PROTECTION_TIME = 35
REG_OVERLOAD_TORQUE = 36
REG_SPEED_P = 37
REG_OVERCURRENT_TIME = 38
REG_SPEED_I = 39
# RAM: 0 off, 1 on, 2 damping (NEW table: the torque limit at 48 sets the
# damping force), 128 = re-datum the current position as 2048. Only 0/1 are
# ever written — see SAFE_TORQUE_ENABLE_VALUES.
REG_TORQUE_ENABLE = 40
REG_ACCELERATION = 41         # RAM
REG_GOAL_POSITION = 42        # RAM, 2 bytes
# RAM, 2 bytes. OLD table: Goal_Time, a documented no-op on ST/STS. NEW table:
# "PWM open-loop speed", factory 1000, effective only in mode 2 — inert in
# position mode 0, which the boot probe enforces. Never rely on it.
REG_GOAL_TIME = 44
REG_GOAL_SPEED = 46           # RAM, 2 bytes
REG_TORQUE_LIMIT = 48         # RAM, 2 bytes, loaded from Max_Torque (16) at power-on (vendor table)
REG_LOCK = 55                 # RAM: 0 = EEPROM writes persist, 1 = protected (inverted!);
#                               factory default OLD table 0, NEW table 1
REG_PRESENT_POSITION = 56     # R, 2 bytes, sign bit 15
REG_PRESENT_SPEED = 58        # R, 2 bytes, steps/s, sign bit 15
REG_PRESENT_LOAD = 60         # R, 2 bytes, sign bit 10
REG_PRESENT_VOLTAGE = 62      # R, 1 byte, ×0.1 V
REG_PRESENT_TEMPERATURE = 63  # R, 1 byte, °C
REG_STATUS = 65               # R, 1 byte (error flags)
REG_PRESENT_CURRENT = 69      # R, 2 bytes, ×6.5 mA

# index = register 6 value (official SDK)
BAUD_RATES = (1_000_000, 500_000, 250_000, 128_000, 115_200, 76_800, 57_600, 38_400)
# One READ of the whole EEPROM: addr 0..39 is every EEPROM register; 40 is
# Torque_Enable (RAM) and deliberately outside the block.
EEPROM_BLOCK_ADDR = 0
EEPROM_BLOCK_LEN = 40
# The largest Homing_Offset magnitude the 11-bit sign-magnitude form holds — and
# therefore the largest our tools ever write (see REG_HOMING_OFFSET).
HOMING_OFFSET_MAX = 2047

# ── Angular_Resolution (register 30) — the last map-scaling register ──────────
# Vendor text (identical in both official memory tables): „对传感器最小分辨角度
# （度/步）的放大系数，修改此值可以扩展控制圈数" — an amplification factor on the
# sensor's minimum resolution angle (degrees per step), whose purpose is to extend
# the number of controllable TURNS. EEPROM, 1 byte, factory default 1, range 1..3
# (OLD table) or 1..128 (NEW table).
#
# WHY IT BELONGS IN THE BOOT FINGERPRINT: this whole driver rests on ONE fixed
# tick↔angle map (TICKS_PER_REV = 4096, _RAD_PER_TICK). The vendor names two
# registers that can move that map — Phase bit 4 (single-turn vs full-angle
# feedback) and this one. probe_bus has always gated the first. This is the other
# half, and the EEPROM limit-window check CANNOT stand in for it: that check
# compares raw TICK numbers, which a rescale leaves untouched, so a resolution of
# 2 or 3 would silently multiply every angle the arm reports AND accepts while
# every existing gate stayed green.
#
# HONEST LIMITS OF WHAT IS KNOWN (both flagged deliberately):
#  * The vendor wording does not settle whether N multiplies the tick COUNT or the
#    degrees-per-TICK. Both readings agree on the observable — travel grows ×N and
#    per-tick precision degrades ×N — so either way ≠1 breaks the fixed map.
#  * The vendor documents this register as working TOGETHER with Phase bit 4
#    („多圈控制时需修改地址0x12参数BIT4置1"), which probe_bus already refuses. So a
#    lone resolution ≠ 1 may well be INERT on this firmware. The boot check
#    nevertheless REFUSES it: the Servo-Station and the provision tool write the
#    value, so a calibrated servo carries one the probe can predict, and the
#    whole EEPROM block is compared with the servo spec's image.
# LeRobot carries the same address (`"Angular_Resolution": (30, 1)`) and never
# reads or writes it, relying on a hardcoded 4096 instead — i.e. upstream has the
# identical un-checked assumption.
ANGULAR_RESOLUTION_SINGLE_TURN = 1

STS3215_MODEL_NUMBER = 777
STS3250_MODEL_NUMBER = 2825
# The V1 arm mixes STS servo models BY DESIGN: joints 1/4/5 and the claw (ids
# 1/4/5/6) are STS3215 (Model 777) and the high-load shoulder + elbow (joints
# 2/3) are STS3250 (Model 2825). Both are STS-series: identical
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

# ── Torque_Enable (register 40) is NOT a boolean ──────────────────────────────
# BOTH official vendor memory tables give this register min 0 / MAX 128 and the
# same parsing note: „写0：关闭扭力输出；写1：打开扭力输出;写128：任意当前位置
# 较正为2048" — write 128 and the servo re-datums WHEREVER THE JOINT HAPPENS TO BE
# STANDING to tick 2048. It is a COMMAND, not a level. Independently corroborated
# by the vendor's own debug tool, whose `calibration_offset()` (中位校准,
# "middle-position calibration") is literally a one-byte write of 128 to
# register 40.
#
# What makes this a silent-corruption hazard rather than a mere footgun: the
# driver's boot fingerprint would still PASS afterwards. probe_bus compares the
# whole EEPROM block with the servo spec's image, but the image does not fix the
# Homing_Offset (only its encoding), and that is the one register a re-datum
# rewrites. Every angle the arm reports and accepts would simply be wrong, in a
# frame nothing on the rig can detect.
#
# Two things deliberately NOT claimed here, because the source review could not
# establish them: whether the re-datum survives a power-cycle (register 40 itself
# is SRAM; the Homing_Offset it must manipulate is EEPROM, and no vendor document
# says whether the Lock register gates it), and what values 2..127 do (undocumented
# in every source). Neither matters for the rail below — a re-datum that lasts only
# for the session is just as wrong while the student is using the arm.
#
# Nothing in the tree writes anything but 0/1 today (every call site was
# enumerated). These constants + assert_safe_torque_enable exist so that nothing
# ever can: a computed value, an off-by-one in a future contiguous payload, or a
# copy-paste of a Dynamixel idiom would otherwise re-datum the arm in silence.
TORQUE_ENABLE_OFF = 0
TORQUE_ENABLE_ON = 1
TORQUE_ENABLE_CALIBRATE_MIDPOINT = 128   # NEVER write this — see above
# Deliberately an ALLOWLIST of the two values this driver has semantics for, not
# a denylist of 128: the intermediate values are not documented consistently
# across vendor doc revisions, so "not 128" would be a guess while "0 or 1" is a
# statement about our own driver. A future compliant/damping mode that genuinely
# needs another value widens THIS set consciously, and the raised message says so.
SAFE_TORQUE_ENABLE_VALUES = frozenset({TORQUE_ENABLE_OFF, TORQUE_ENABLE_ON})

INSTR_PING = 0x01
INSTR_READ = 0x02
INSTR_WRITE = 0x03
INSTR_SYNC_READ = 0x82
INSTR_SYNC_WRITE = 0x83

# Status/error byte (also readable at REG_STATUS). Bit meanings per the two
# official vendor memory tables (2026-07-26 source review):
#   0x01 电压    voltage (over/under)
#   0x02 传感器  the magnetic-encoder SENSOR itself
#   0x04 温度    overheat
#   0x08 电流    overcurrent
#   0x20 过载    overload
# 0x02 is the SENSOR bit, NOT an out-of-range angle. The vendor SDK's constant
# for it is spelled ERRBIT_ANGLE, which is what makes this easy to get backwards
# — but that SDK's own decoder prints "Angle sen error!", i.e. angle SENSOR. The
# German 'Winkel-Sensorfehler' is therefore right and is deliberately unchanged;
# only the CONSTANT was renamed (ERR_ANGLE → ERR_ANGLE_SENSOR) so the next reader
# cannot make the same inference.
#
# 0x10 IS DELIBERATELY NOT NAMED HERE, and that is a decision, not an omission.
# The two vendor revisions disagree: the older per-model table (ST3215 "V3.7")
# lists bit4 as 角度 "Angle", while the CURRENT published table (磁编码 SMS&STS,
# feetechrc.com) marks bit4 `--` = undefined, the potentiometer/SCS sibling table
# marks it 无 = none, and the official SDK handles no 0x10 at all. Corroborating
# the retraction: bit4 is set in the factory default of NEITHER register 19
# (Unload_Condition, default 44) nor register 20 (LED_Alarm_Condition, default
# 47), both of which use this same 6-bit layout. Asserting „Winkelfehler" would
# be repeating a meaning the newer official table withdrew, so 0x10 falls through
# to the by-value fallback in describe_error_bits instead — which is what the
# actual defect needed anyway (it used to produce a BLANK line). If a bench ever
# observes 0x10 firing, add it here with the wording that observation supports.
# 0x40 and 0x80 are undefined in every source consulted.
ERR_VOLTAGE = 0x01
ERR_ANGLE_SENSOR = 0x02
ERR_OVERHEAT = 0x04
ERR_OVERCURRENT = 0x08
ERR_OVERLOAD = 0x20

ERROR_BITS_DE = (
    (ERR_VOLTAGE, 'Spannungsfehler'),
    (ERR_ANGLE_SENSOR, 'Winkel-Sensorfehler'),
    (ERR_OVERHEAT, 'Überhitzung'),
    (ERR_OVERCURRENT, 'Überstrom'),
    (ERR_OVERLOAD, 'Überlast'),
)

# Everything ERROR_BITS_DE can name. Bits OUTSIDE this mask (0x10, 0x40, 0x80 and
# any future firmware addition) must still be REPORTED rather than swallowed —
# that is the whole lesson of the 0x10 hole above, so the fallback is written in
# terms of "not in the table" and never enumerates the leftovers.
KNOWN_ERROR_BITS_MASK = 0
for _bit, _name in ERROR_BITS_DE:
    KNOWN_ERROR_BITS_MASK |= _bit
del _bit, _name


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


class ForeignOffsetEncoding(ValueError):
    """A Homing_Offset word with any of bits 12–15 set: written by a tool that
    uses the vendor's wider encoding (see REG_HOMING_OFFSET), never by ours."""


class OffsetUnrepresentable(ValueError):
    """The raw tick sits exactly opposite the mark: the offset would be −2048,
    which the 11-bit sign-magnitude form cannot hold."""


def decode_homing_offset(raw16: int) -> int:
    """Homing_Offset register word → signed offset. Refuses (rather than
    misreads) a word from a foreign tool: the plain bit-11 decode reads 0x1805
    (−2053 in the vendor's table) as −5."""
    if raw16 & 0xF000:
        raise ForeignOffsetEncoding(
            f'Homing_Offset word {raw16:#06x} uses bits 12-15 (foreign encoding)')
    return decode_sign_magnitude(raw16, 11)


def encode_homing_offset(value: int) -> int:
    if abs(value) > HOMING_OFFSET_MAX:
        raise ValueError(
            f'Homing_Offset {value} outside ±{HOMING_OFFSET_MAX}')
    return encode_sign_magnitude(value, 11)


def raw_tick_from(present: int, offset: int) -> int:
    """The encoder's raw tick behind a reading: Present = (Raw − Offset) mod
    4096 on a single-turn servo, so Raw = (Present + Offset) mod 4096."""
    return (present + offset) % TICKS_PER_REV


def homing_offset_for(raw: int, mark_tick: int) -> int:
    """The Homing_Offset that makes encoder tick ``raw`` read ``mark_tick`` —
    wrap-aware, folded into [−2048, 2047]. Raises
    :class:`OffsetUnrepresentable` for −2048 (raw exactly opposite the mark)."""
    offset = ((raw - mark_tick + 2048) % TICKS_PER_REV) - 2048
    if offset == -2048:
        raise OffsetUnrepresentable(
            f'raw tick {raw} sits exactly opposite mark {mark_tick}')
    return offset


def circular_distance(a: int, b: int) -> int:
    """Distance between two ticks the short way round the 4096-tick circle."""
    d = abs(a - b) % TICKS_PER_REV
    return min(d, TICKS_PER_REV - d)


def broadcast_wait_s(baudrate: int) -> float:
    """How long a broadcast PING collects replies: LeRobot 0.5.1's
    ``_broadcast_ping`` window. ASSUMPTION: replies are staggered by id; the
    P0 reference read on real servos proves the window."""
    return 6 * 253 * 10 / baudrate + 0.003 * 253 + 0.016


def parse_status_stream(buf: bytes) -> tuple[list[tuple[int, int, bytes]], int]:
    """Every valid status packet in ``buf`` as ``(id, error, params)``, in
    order, plus the count of bytes that are not part of one (bytes before a
    header, a bad length, a bad checksum, a truncated tail). Pure."""
    packets: list[tuple[int, int, bytes]] = []
    used = 0
    i = 0
    n = len(buf)
    while i + 1 < n:
        if buf[i] != 0xFF or buf[i + 1] != 0xFF:
            i += 1
            continue
        if i + 4 > n:
            break
        servo_id, length = buf[i + 2], buf[i + 3]
        end = i + 4 + length
        if length < 2 or length > 250 or end > n:
            i += 1
            continue
        error = buf[i + 4]
        params = bytes(buf[i + 5:end - 1])
        if buf[end - 1] != checksum(bytes([servo_id, length, error]) + params):
            i += 1
            continue
        packets.append((servo_id, error, params))
        used += end - i
        i = end
    return packets, n - used


def position_limit_window(lo_rad: float, hi_rad: float, sign: int, *,
                          t0: int) -> tuple[int, int]:
    """Designed URDF limit pair → the servo's EEPROM ``Min/Max_Position_Limit``
    window around ``t0`` (the joint's encoder zero, from the servo spec),
    sign-aware: a −1 direction convention mirrors the window and re-orders
    lo ≤ hi. Clamped to the single-turn register range [0, TICKS_PER_REV−1].
    ONE shared implementation for the servo spec (which defines the window the
    provisioning tool and the Servo-Station WRITE) and the driver node's boot
    probe band — the two must never drift (audit H1). ``t0`` is keyword-only
    and required, so a forgotten call site raises instead of silently
    assuming 2048."""
    a = t0 + int(round(lo_rad * sign / _RAD_PER_TICK))
    b = t0 + int(round(hi_rad * sign / _RAD_PER_TICK))
    lo, hi = (a, b) if a <= b else (b, a)
    return max(0, lo), min(TICKS_PER_REV - 1, hi)


def torque_enable_byte_in_span(addr: int, data: bytes) -> Optional[int]:
    """The byte a write of ``data`` starting at ``addr`` would land in register
    ``REG_TORQUE_ENABLE``, or ``None`` when the write does not reach it.

    A SPAN test rather than an ``addr == 40`` test on purpose: the driver's own
    goal write is a 7-byte contiguous block starting at ``REG_ACCELERATION``
    (41), i.e. one register PAST the torque switch, so a single off-by-one in a
    future payload would silently reach 40 while still looking like an ordinary
    goal write."""
    index = REG_TORQUE_ENABLE - addr
    if 0 <= index < len(data):
        return data[index]
    return None


def assert_safe_torque_enable(addr: int, data: bytes) -> None:
    """Refuse any write that would put something other than 0/1 into register
    ``REG_TORQUE_ENABLE``. Raises :class:`ValueError` — deliberately NOT
    :class:`FeetechBusError`, which callers catch as "the wire is unhappy, retry":
    this is a programming error and must not be retried into a re-datum.

    Failing the WRITE is the safe direction on every path that exists: a refused
    torque-ON leaves the arm limp (the documented end state of every other
    torque refusal) and a refused torque-OFF is unreachable, because the only
    values the driver ever sends to 40 are the literals 0 and 1."""
    value = torque_enable_byte_in_span(addr, data)
    if value is not None and value not in SAFE_TORQUE_ENABLE_VALUES:
        raise ValueError(
            f'refusing to write Torque_Enable={value} (addr {addr}, byte '
            f'{REG_TORQUE_ENABLE - addr} of the payload): register '
            f'{REG_TORQUE_ENABLE} accepts only '
            f'{sorted(SAFE_TORQUE_ENABLE_VALUES)}; '
            f'{TORQUE_ENABLE_CALIBRATE_MIDPOINT} silently re-datums the joint '
            f'to tick {CENTER_TICK} (vendor: 任意当前位置较正为2048), which '
            'invalidates the arm calibration with no error reply and still '
            'passes the boot fingerprint. Widen SAFE_TORQUE_ENABLE_VALUES if a '
            'new torque mode genuinely needs another value.')


def format_firmware(major: int, minor: int) -> str:
    """``'major.minor'`` from registers 0/1 — ONE spelling shared by the driver's
    boot fingerprint and the provisioning record's ``firmware`` field, so a bench
    log and a stored calibration can be string-compared without a reformat."""
    return f'{int(major)}.{int(minor)}'


def describe_error_bits(error: int) -> str:
    """German short list of the set error flags ('' when — and ONLY when — the
    byte is clean).

    A NON-EMPTY result for every non-zero byte is the contract, not a nicety: the
    caller interpolates this straight into a student-facing ``[WARNUNG] Servo n
    meldet: {…}`` line, so an unnamed bit used to render as a blank accusation
    with no fault in it. Any bit the table cannot name is reported as its own
    hex value instead of being dropped, which also keeps a MIXED byte honest
    (0x12 must not report only the 0x02 half)."""
    names = [name for bit, name in ERROR_BITS_DE if error & bit]
    unnamed = error & ~KNOWN_ERROR_BITS_MASK
    if unnamed:
        names.append(f'unbekanntes Statusbit {unnamed:#04x}')
    return ', '.join(names)


class FeetechBusError(Exception):
    """Raised on a broken/absent reply (framing, checksum, timeout)."""


class FeetechBusTimeout(FeetechBusError):
    """No (complete) reply before the deadline. A subclass, so every existing
    ``except FeetechBusError`` keeps catching it; a caller that must tell a
    silent servo from a garbled reply (the Servo-Station) catches this one."""


class FeetechBus:
    """One serial port, many STS3215 servos. NOT thread-safe — the caller
    (feetech_arm_node) serializes access with its own lock."""

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
        self._baudrate = baudrate

    @property
    def baudrate(self) -> int:
        return self._baudrate

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
        :class:`FeetechBusTimeout` on a deadline, :class:`FeetechBusError` on
        framing/checksum."""
        buf = b''
        # hunt the FF FF header
        while True:
            if time.monotonic() > deadline:
                raise FeetechBusTimeout('timeout waiting for status header')
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
                raise FeetechBusTimeout(f'timeout ({len(out)}/{n} bytes)')
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

    def broadcast_ping(self, wait_s: Optional[float] = None
                       ) -> tuple[list[tuple[int, int]], int]:
        """PING to the broadcast id: every servo on the bus answers with its
        own status packet. Collects bytes for ``wait_s`` (default
        :func:`broadcast_wait_s` at this bus's baud rate) and returns
        ``([(id, error)], junk_bytes)`` — junk counts every byte that was not
        part of a valid packet (two servos on one id, a wrong baud rate)."""
        self._write_packet(build_packet(BROADCAST_ID, INSTR_PING))
        if wait_s is None:
            wait_s = broadcast_wait_s(self.baudrate)
        deadline = time.monotonic() + wait_s
        buf = b''
        while time.monotonic() < deadline:
            chunk = self._ser.read(64)
            if chunk:
                buf += chunk
        packets, junk = parse_status_stream(buf)
        return [(sid, error) for sid, error, _params in packets], junk

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
        assert_safe_torque_enable(addr, data)
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
        # Per-servo, before anything is framed: a broadcast SYNC_WRITE takes no
        # reply, so a bad Torque_Enable byte here would be both undetectable and
        # applied to every servo on the bus at once.
        for _sid, _data in per_servo.items():
            assert_safe_torque_enable(addr, _data)
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

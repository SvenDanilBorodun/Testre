#!/usr/bin/env python3
"""The V1 servo spec: ONE table of every per-joint servo fact.

Shared by the driver (``feetech_arm_node.py``: signs, encoder zero, the boot
probe's EEPROM image), ``tools/feetech_provision.py`` and the Servo-Station, so
the four can never disagree about what a calibrated V1 servo looks like.

The centred tick map: each joint's encoder zero ``T0`` is chosen so the MIDDLE
of its URDF range reads tick 2048 (``centred_t0``). Every window then sits at
least 795 ticks from the 0/4095 seam of the non-wrap-aware STS position loop,
and the window is identical for both tick directions.

``sign`` per joint = the measured direction of the servo's output axis against
the URDF joint axis (``OUTPUT_AXIS_VS_URDF``) × the model's tick direction
(``TICK_DIRECTION``: +1 = ticks rise counter-clockwise seen from the horn
side). The tick direction is not yet measured, so it ships UNPINNED (None):
while any value is provisional ``V1.pinned`` is False, the driver refuses to
boot a V1 and nothing calibrates a servo.

Pure: stdlib + ``feetech_bus`` only, no I/O and no environment — the spec is
the same on every machine that runs it.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import namedtuple

import feetech_bus as fb

SPEC_SCHEMA = 'edubotics.v1_servo_spec.v1'
PROFILE_ID = 'v1_studio'
JOINT_NAMES = ('joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'RL_joint')
SERVO_IDS = (1, 2, 3, 4, 5, 6)
MODELS = (fb.STS3215_MODEL_NUMBER, fb.STS3250_MODEL_NUMBER, fb.STS3250_MODEL_NUMBER,
          fb.STS3215_MODEL_NUMBER, fb.STS3215_MODEL_NUMBER, fb.STS3215_MODEL_NUMBER)      # 777/2825
JOINT_LIMITS_RAD = ((-1.5708, 1.5708), (0.0, 3.1416), (0.0, 3.1416),
                    (-1.9198, 1.9198), (-1.5708, 1.5708), (0.0, 1.5708))   # == the driver's literal
OUTPUT_AXIS_VS_URDF = (-1, +1, -1, -1, -1, +1)   # measured from 5dof_assembly_modified.STEP (design §2.4; review r02)
MARK_Q_RAD = 0.0                                  # the gauge holds q = 0, so mark_tick == T0
REFERENCE_THETA_DEG = (0.0, 0.0, 0.0, 0.0, 0.0, 81.97)       # the marked horn hole (the claw's −8.03° is covered)
HOLE_THETA_OFFSET_DEG = (0.0, 0.0, 0.0, 0.0, 0.0, -8.03)     # horn holes at offset + k*90 at q = 0
GAUGE_CAP = ('J15', 'J15', 'J15', 'J15', 'J15', 'GR')
JOINT_LABEL_DE = ('Gelenk 1 – Basis', 'Gelenk 2 – Schulter', 'Gelenk 3 – Ellbogen',
                  'Gelenk 4 – Handgelenk', 'Gelenk 5 – Drehung', 'Greifer')
COLOUR_DE = ('weiß', 'gelb', 'orange', 'grün', 'blau', 'rot')
COLOUR_HEX = ('#FFFFFF', '#FFD400', '#FF8C00', '#2EAD4B', '#1E6FD9', '#D7263D')
SERVO_SERIAL_PATTERN = r'^V1S-[0-9]{6}$'                                  # migration 052's CHECK repeats it
CAP_LABEL_PATTERN = r'^KAPPE-(J15|GR)-[A-X0-9]{1,4}$'                       # no Y/Z: a US-configured scanner on a German layout swaps them
TICK_DIRECTION_CCW = 1
TICK_DIRECTION_CW = -1
# Decided by us, not by the factory: baud 1 Mbit/s, return delay 0, status level
# 1 (ack every instruction), Unload condition 46, resolution 1, position mode,
# overload torque 80.
DECIDED = {fb.REG_BAUD_RATE: 0, fb.REG_RETURN_DELAY: 0, fb.REG_RESPONSE_STATUS_LEVEL: 1,
           fb.REG_UNLOAD_CONDITION: 46, fb.REG_ANGULAR_RESOLUTION: 1,
           fb.REG_OPERATING_MODE: 0, fb.REG_OVERLOAD_TORQUE: 80}

# ── PROVISIONAL — NOT PINNED. Pinned by ONE commit after P0, VR3, VR5 and the overcurrent check on the
# pilot (spec §12): replace the values, empty PROVISIONAL_NAMES, update test_feetech_servo_spec.py's two
# tripwires (TestShippedSpecIsUnpinned and the digest literal). While any name is provisional the driver
# refuses to boot, the station and feetech_provision.py refuse to calibrate. ──
TICK_DIRECTION = {777: None, 2825: None}                     # per model
FACTORY = {   # model -> {addr: value}; 2-byte registers keyed by their first address
    777:  {13: 70, 14: 140, 15: 40, 18: 12, 20: 47, 21: 32, 22: 32, 23: 0, 24: 16, 26: 1, 27: 1,
           28: 500, 34: 20, 35: 200, 38: 200},   # 28: OLD table 500, NEW 511, C018 "2 A" ≈ 310
    2825: {13: 70, 14: 140, 15: 40, 18: 12, 20: 47, 21: 32, 22: 32, 23: 0, 24: 16, 26: 1, 27: 1,
           28: 585, 34: 20, 35: 200, 38: 200},   # 28: STS3250 3.8 A ≈ 585
}
MOTION_TEST_WINDOW_MS = {777: (100, 400), 2825: (60, 300)}   # 45° at full speed; P0 measures
MAX_TORQUE = (800, 800, 800, 800, 800, 150)                   # VR5 decides
PROVISIONAL_NAMES = frozenset({'TICK_DIRECTION', 'FACTORY', 'MOTION_TEST_WINDOW_MS', 'MAX_TORQUE'})

LAYOUT = (   # (addr, size, name_de, class) — covers 5..39 exactly once; class in refuse|warn|offset|ignore
    (5, 1, 'ID', 'refuse'), (6, 1, 'Baudrate', 'refuse'), (7, 1, 'Antwortverzögerung', 'warn'),
    (8, 1, 'Antwort-Level', 'refuse'), (9, 2, 'Min-Position', 'refuse'), (11, 2, 'Max-Position', 'refuse'),
    (13, 1, 'Max-Temperatur', 'warn'), (14, 1, 'Max-Spannung', 'warn'), (15, 1, 'Min-Spannung', 'warn'),
    (16, 2, 'Max-Drehmoment', 'refuse'), (18, 1, 'Phase', 'refuse'), (19, 1, 'Abschaltbedingung', 'refuse'),
    (20, 1, 'LED-Alarm', 'warn'), (21, 1, 'P-Anteil', 'warn'), (22, 1, 'D-Anteil', 'warn'),
    (23, 1, 'I-Anteil', 'warn'), (24, 1, 'Anlaufkraft', 'warn'), (25, 1, 'Register 25', 'warn'),
    (26, 1, 'Totzone CW', 'warn'), (27, 1, 'Totzone CCW', 'warn'), (28, 2, 'Schutzstrom', 'refuse'),
    (30, 1, 'Winkelauflösung', 'refuse'), (31, 2, 'Homing-Offset', 'offset'), (33, 1, 'Betriebsmodus', 'refuse'),
    (34, 1, 'Haltemoment', 'refuse'), (35, 1, 'Schutzzeit', 'refuse'), (36, 1, 'Überlastmoment', 'refuse'),
    (37, 1, 'Drehzahl-P', 'ignore'), (38, 1, 'Überstromzeit', 'refuse'), (39, 1, 'Drehzahl-I', 'ignore'),
)

Deviation = namedtuple('Deviation', 'addr size name found expected')    # expected None for a foreign offset
JointSpec = namedtuple('JointSpec', 'index name servo_id model model_name label_de colour_de cap reference_theta_deg '
                       'limits_rad window max_torque tick_direction sign t0 image')
ServoSpec = namedtuple('ServoSpec', 'tick_direction pinned provisional joints signs t0 windows factory '
                       'motion_test_window_ms max_torque digest')

RAD_PER_TICK = 2.0 * math.pi / fb.TICKS_PER_REV
_PHASE_MULTI_TURN_BIT = 0x10
_LIMIT_ADDRS = (fb.REG_MIN_POSITION_LIMIT, fb.REG_MAX_POSITION_LIMIT)


def centred_t0(lo_rad, hi_rad, sign) -> int:
    """The encoder zero that puts the middle of ``[lo_rad, hi_rad]`` on tick
    2048 for direction ``sign``."""
    return fb.CENTER_TICK - sign * int(round(((lo_rad + hi_rad) / 2) / RAD_PER_TICK))


def _window(index):
    """The joint's EEPROM window — the same for both directions (asserted)."""
    lo_rad, hi_rad = JOINT_LIMITS_RAD[index]
    plus = fb.position_limit_window(lo_rad, hi_rad, 1, t0=centred_t0(lo_rad, hi_rad, 1))
    minus = fb.position_limit_window(lo_rad, hi_rad, -1, t0=centred_t0(lo_rad, hi_rad, -1))
    assert plus == minus, (index, plus, minus)
    return plus


def golden_image(index, factory, max_torque) -> dict:
    """``{addr: value}`` of every EEPROM register a calibrated servo at joint
    ``index`` carries (2-byte registers keyed by their first address)."""
    lo, hi = _window(index)
    image = dict(factory[MODELS[index]])
    image.update(DECIDED)
    image.update({fb.REG_ID: SERVO_IDS[index], fb.REG_MIN_POSITION_LIMIT: lo,
                  fb.REG_MAX_POSITION_LIMIT: hi, fb.REG_MAX_TORQUE_LIMIT: max_torque[index],
                  fb.REG_25: 0})
    image[fb.REG_PHASE] &= ~_PHASE_MULTI_TURN_BIT
    return image


def _digest(tick_direction, provisional, joints, factory, motion_test_window_ms, max_torque):
    def joint_json(joint):
        out = dict(joint._asdict())
        out['image'] = sorted([addr, value] for addr, value in joint.image.items())
        return out
    payload = {
        'schema': SPEC_SCHEMA,
        'profile': PROFILE_ID,
        'tick_direction': {str(m): d for m, d in tick_direction.items()},
        'provisional': sorted(provisional),
        'joints': [joint_json(j) for j in joints],
        'layout': LAYOUT,
        'decided': {str(a): v for a, v in DECIDED.items()},
        'factory': {str(m): {str(a): v for a, v in regs.items()} for m, regs in factory.items()},
        'motion_test_window_ms': {str(m): w for m, w in motion_test_window_ms.items()},
        'max_torque': max_torque,
    }
    text = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def build_spec(tick_direction, factory, motion_test_window_ms, max_torque, provisional) -> ServoSpec:
    """The whole spec for one set of values. ``tick_direction`` is ``{777:
    1|-1|None, 2825: 1|-1|None}``; a joint whose model has no direction yet
    carries ``sign`` and ``t0`` None, and the spec's ``signs`` / ``t0`` are
    tuples only when every joint has one."""
    joints = []
    for i, model in enumerate(MODELS):
        direction = tick_direction[model]
        lo_rad, hi_rad = JOINT_LIMITS_RAD[i]
        sign = None if direction is None else OUTPUT_AXIS_VS_URDF[i] * direction
        joints.append(JointSpec(
            index=i, name=JOINT_NAMES[i], servo_id=SERVO_IDS[i], model=model,
            model_name=fb.STS_MODEL_NAMES[model], label_de=JOINT_LABEL_DE[i],
            colour_de=COLOUR_DE[i], cap=GAUGE_CAP[i], reference_theta_deg=REFERENCE_THETA_DEG[i],
            limits_rad=JOINT_LIMITS_RAD[i], window=_window(i), max_torque=max_torque[i],
            tick_direction=direction, sign=sign,
            t0=None if sign is None else centred_t0(lo_rad, hi_rad, sign),
            image=golden_image(i, factory, max_torque)))
    joints = tuple(joints)
    complete = all(j.sign is not None for j in joints)
    provisional = frozenset(provisional)
    return ServoSpec(
        tick_direction=dict(tick_direction),
        pinned=all(tick_direction[m] in (1, -1) for m in set(MODELS)) and not provisional,
        provisional=provisional,
        joints=joints,
        signs=tuple(j.sign for j in joints) if complete else None,
        t0=tuple(j.t0 for j in joints) if complete else None,
        windows=tuple(j.window for j in joints),
        factory=factory,
        motion_test_window_ms=motion_test_window_ms,
        max_torque=tuple(max_torque),
        digest=_digest(tick_direction, provisional, joints, factory, motion_test_window_ms,
                       tuple(max_torque)))


def compare_block(block: bytes, joint: JointSpec) -> tuple[list[Deviation], list[Deviation]]:
    """``(refusals, warnings)`` of a 40-byte EEPROM block (addr 0..39, 2-byte
    registers little-endian) against ``joint``'s golden image, in LAYOUT order.

    'ignore' registers are skipped; the offset is a refusal (expected None) only
    for a foreign encoding (bits 12–15 set); the two limits tolerate ±1 tick;
    the Phase byte is compared with its multi-turn bit 4 cleared (the probe
    refuses bit 4 by its own sentence)."""
    refusals: list[Deviation] = []
    warnings: list[Deviation] = []
    for addr, size, name, cls in LAYOUT:
        if cls == 'ignore':
            continue
        found = block[addr] if size == 1 else fb.from_le16(block[addr], block[addr + 1])
        if cls == 'offset':
            if found & 0xF000:
                refusals.append(Deviation(addr, size, name, found, None))
            continue
        expected = joint.image[addr]
        if addr in _LIMIT_ADDRS:
            deviates = abs(found - expected) > 1
        elif addr == fb.REG_PHASE:
            deviates = (found & ~_PHASE_MULTI_TURN_BIT) != expected
        else:
            deviates = found != expected
        if deviates:
            (refusals if cls == 'refuse' else warnings).append(
                Deviation(addr, size, name, found, expected))
    return refusals, warnings


V1 = build_spec(TICK_DIRECTION, FACTORY, MOTION_TEST_WINDOW_MS, MAX_TORQUE, PROVISIONAL_NAMES)

"""Deps-free tests for the clean-room Feetech STS3215 bus (edu6 PR 5).

Framing/checksum/sign-magnitude are pinned exactly — these are the wire rules
a transcription slip would corrupt silently. The serial layer is faked; the
node-level pure functions (tick conversion, trajectory interpolation,
boot-home) are tested via ast-extraction so importing them needs no rclpy.
"""

import ast
import math
import os
import re
import sys
import textwrap
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_DOCKER = os.path.join(_HERE, '..', 'docker', 'open_manipulator')
sys.path.insert(0, _DOCKER)

import feetech_bus as fb  # noqa: E402


class TestFraming(unittest.TestCase):
    def test_checksum_matches_hand_computed(self):
        # PING to id 1: FF FF 01 02 01 FB (classic Protocol-1.0 example).
        pkt = fb.build_packet(1, fb.INSTR_PING)
        self.assertEqual(pkt, bytes([0xFF, 0xFF, 0x01, 0x02, 0x01, 0xFB]))

    def test_read_packet_layout(self):
        # READ id 2, addr 56, len 6: params (56, 6), len byte = 4.
        pkt = fb.build_packet(2, fb.INSTR_READ, bytes([56, 6]))
        self.assertEqual(pkt[:5], bytes([0xFF, 0xFF, 2, 4, 0x02]))
        self.assertEqual(pkt[5:7], bytes([56, 6]))
        body = pkt[2:-1]
        self.assertEqual(pkt[-1], (~sum(body)) & 0xFF)

    def test_sync_write_packet(self):
        pkt = fb.build_packet(
            fb.BROADCAST_ID, fb.INSTR_SYNC_WRITE,
            bytes([42, 2]) + bytes([1, 0x00, 0x08]) + bytes([2, 0x10, 0x08]))
        self.assertEqual(pkt[2], 0xFE)
        self.assertEqual(pkt[4], 0x83)

    def test_le16_round_trip(self):
        for v in (0, 1, 255, 256, 2048, 4095, 65535):
            b = fb.le16(v)
            self.assertEqual(fb.from_le16(b[0], b[1]), v)
        # little-endian: low byte FIRST (STS/SMS; the SCS series differs).
        self.assertEqual(fb.le16(0x0102), bytes([0x02, 0x01]))

    def test_sign_magnitude_decode(self):
        # −5 with sign bit 15 arrives as 0x8005 (the OMX unsigned-int16 trap's
        # cousin — NOT two's complement).
        self.assertEqual(fb.decode_sign_magnitude(0x8005, 15), -5)
        self.assertEqual(fb.decode_sign_magnitude(5, 15), 5)
        self.assertEqual(fb.decode_sign_magnitude((1 << 10) | 123, 10), -123)
        self.assertEqual(fb.decode_sign_magnitude(123, 10), 123)
        # bit 11 (Homing_Offset)
        self.assertEqual(fb.decode_sign_magnitude((1 << 11) | 900, 11), -900)

    def test_sign_magnitude_encode_round_trip(self):
        for bit in (10, 11, 15):
            for v in (-2047, -5, 0, 5, 1023):
                if abs(v) >= (1 << bit):
                    continue
                self.assertEqual(
                    fb.decode_sign_magnitude(
                        fb.encode_sign_magnitude(v, bit), bit), v)

    def test_describe_error_bits_german(self):
        self.assertEqual(fb.describe_error_bits(0), '')
        self.assertIn('Überlast', fb.describe_error_bits(fb.ERR_OVERLOAD))
        both = fb.describe_error_bits(fb.ERR_VOLTAGE | fb.ERR_OVERHEAT)
        self.assertIn('Spannungsfehler', both)
        self.assertIn('Überhitzung', both)


class TestErrorByteIsNeverSilent(unittest.TestCase):
    """The error byte is interpolated straight into a student-facing
    „[WARNUNG] Servo n meldet: {…} (Status 0x..)" line, so a bit the table
    cannot name used to render as a BLANK accusation. 0x10 was exactly that: an
    un-mapped bit (the vendor SDK's ERRBIT_* set has a hole there), and 0x12
    was worse — it named only the 0x02 half and dropped the other fault from a
    line that still looked complete."""

    def test_every_nonzero_byte_produces_a_non_empty_reason(self):
        # THE contract, over the whole 8-bit space. Before the fix, 0x10, 0x40,
        # 0x80 and every combination of only those (0x50, 0xC0, 0x90 …) returned
        # '' — 68 of the 255 non-zero bytes were silent.
        silent = [e for e in range(1, 256) if not fb.describe_error_bits(e)]
        self.assertEqual(silent, [], f'silent error bytes: '
                                    f'{[hex(e) for e in silent]}')

    def test_a_clean_byte_is_the_only_empty_result(self):
        self.assertEqual(fb.describe_error_bits(0), '')

    def test_the_0x10_bit_is_reported_even_though_it_is_deliberately_unnamed(self):
        # The vendor revisions DISAGREE about 0x10 (older per-model table: 角度
        # "Angle"; current official table: `--` undefined), so it is intentionally
        # absent from ERROR_BITS_DE. What must NOT happen either way is silence.
        self.assertNotIn(0x10, [bit for bit, _ in fb.ERROR_BITS_DE])
        out = fb.describe_error_bits(0x10)
        self.assertTrue(out)
        self.assertIn('0x10', out)

    def test_a_mixed_byte_reports_every_set_bit_not_just_the_named_one(self):
        # 0x12 = the angle-SENSOR bit (0x02) plus the disputed 0x10. Reporting one
        # and dropping the other is the sneakiest form of the original bug, because
        # the resulting line is non-empty and looks trustworthy.
        both = fb.describe_error_bits(fb.ERR_ANGLE_SENSOR | 0x10)
        self.assertIn('Winkel-Sensorfehler', both)
        self.assertIn('0x10', both)
        # …and a named bit alongside an unnamed one keeps both halves too.
        mixed = fb.describe_error_bits(fb.ERR_OVERLOAD | 0x40)
        self.assertIn('Überlast', mixed)
        self.assertIn('0x40', mixed)

    def test_bits_outside_the_table_are_named_by_their_value(self):
        for raw in (0x10, 0x40, 0x80, 0xC0):
            out = fb.describe_error_bits(raw)
            self.assertIn('unbekanntes Statusbit', out)
            self.assertIn(f'{raw:#04x}', out)

    def test_the_sensor_bit_keeps_its_angle_sensor_wording(self):
        # 0x02 is the vendor's 传感器/磁编码 bit and the SDK decodes it as
        # "Angle sen error!" — angle SENSOR. The German must not be narrowed to a
        # plain „Sensorfehler", nor widened into an out-of-range angle claim.
        self.assertEqual(fb.ERR_ANGLE_SENSOR, 0x02)
        self.assertEqual(fb.describe_error_bits(0x02), 'Winkel-Sensorfehler')

    def test_the_known_mask_is_derived_from_the_table_not_hardcoded(self):
        expected = 0
        for bit, _name in fb.ERROR_BITS_DE:
            expected |= bit
        self.assertEqual(fb.KNOWN_ERROR_BITS_MASK, expected)
        # A bit inside the mask must never be reported as "unknown".
        for bit, _name in fb.ERROR_BITS_DE:
            self.assertNotIn('unbekanntes',
                             fb.describe_error_bits(bit))

    def test_each_table_entry_is_a_distinct_single_bit(self):
        bits = [bit for bit, _ in fb.ERROR_BITS_DE]
        self.assertEqual(len(bits), len(set(bits)))
        for bit in bits:
            self.assertEqual(bit & (bit - 1), 0, f'{bit:#04x} is not one bit')
        # The five originally-mapped vendor values must not have MOVED — 0x10 was
        # ADDED to the table, nothing was renumbered.
        self.assertEqual(
            (fb.ERR_VOLTAGE, fb.ERR_ANGLE_SENSOR, fb.ERR_OVERHEAT,
             fb.ERR_OVERCURRENT, fb.ERR_OVERLOAD),
            (0x01, 0x02, 0x04, 0x08, 0x20))


class TestPositionLimitWindow(unittest.TestCase):
    """fb.position_limit_window — the ONE shared window implementation the
    provisioning tool writes with and the driver's boot probe verifies with
    (audit H1 no-drift)."""

    def test_symmetric_and_asymmetric_windows(self):
        self.assertEqual(fb.position_limit_window(-1.5708, 1.5708, 1),
                         (1024, 3072))
        # joint5's relieved asymmetric window.
        self.assertEqual(fb.position_limit_window(-1.5708, 1.9199, 1),
                         (1024, 3300))

    def test_negative_sign_mirrors_and_reorders(self):
        # Mirror identity on an UNCLAMPED window (the ±π ends clamp 4096→4095,
        # which breaks the naive identity by one tick — that clamp is pinned
        # separately below).
        lo_p, hi_p = fb.position_limit_window(-1.5708, 1.9199, 1)
        lo_n, hi_n = fb.position_limit_window(-1.5708, 1.9199, -1)
        self.assertEqual(lo_n, 2 * fb.CENTER_TICK - hi_p)
        self.assertEqual(hi_n, 2 * fb.CENTER_TICK - lo_p)
        self.assertLessEqual(lo_n, hi_n)

    def test_clamped_to_register_range(self):
        self.assertEqual(fb.position_limit_window(-3.1416, 3.1416, 1),
                         (0, 4095))


class _FakeSerial:
    """Scripted serial: records writes, serves canned reply bytes."""

    def __init__(self, replies=b''):
        self.written = b''
        self._rx = replies

    def write(self, data):
        self.written += data

    def read(self, n):
        out, self._rx = self._rx[:n], self._rx[n:]
        return out

    def reset_input_buffer(self):
        pass

    def close(self):
        pass


def _status(servo_id, error, params=b''):
    body = bytes([servo_id, len(params) + 2, error]) + params
    return b'\xff\xff' + body + bytes([fb.checksum(body)])


def _bus(replies=b''):
    fake = _FakeSerial(replies)
    bus = fb.FeetechBus('fake', serial_factory=lambda p, b, t: fake)
    return bus, fake


class TestBusTransactions(unittest.TestCase):
    def test_ping_ok(self):
        bus, fake = _bus(_status(3, 0))
        self.assertTrue(bus.ping(3))
        self.assertEqual(fake.written, fb.build_packet(3, fb.INSTR_PING))

    def test_ping_timeout_false(self):
        bus, _ = _bus(b'')
        self.assertFalse(bus.ping(3, timeout_s=0.01))

    def test_read_u16(self):
        bus, fake = _bus(_status(1, 0, fb.le16(777)))
        self.assertEqual(bus.read_u16(1, fb.REG_MODEL_NUMBER), 777)
        self.assertEqual(
            fake.written,
            fb.build_packet(1, fb.INSTR_READ, bytes([fb.REG_MODEL_NUMBER, 2])))

    def test_read_checksum_mismatch_raises(self):
        good = _status(1, 0, fb.le16(777))
        bad = good[:-1] + bytes([(good[-1] + 1) & 0xFF])
        bus, _ = _bus(bad)
        with self.assertRaises(fb.FeetechBusError):
            bus.read(1, fb.REG_MODEL_NUMBER, 2)

    def test_sync_read_returns_repliers_only(self):
        # servos 1 and 3 answer; 2 is silent → absent from the result (the
        # CALLER must treat a missing id as a hard stop — §3.3).
        replies = (_status(1, 0, bytes(6)) + _status(3, fb.ERR_OVERLOAD, bytes(6)))
        bus, fake = _bus(replies)
        out = bus.sync_read(fb.REG_PRESENT_POSITION, 6, [1, 2, 3])
        self.assertEqual(set(out.keys()), {1, 3})
        self.assertEqual(out[3][0], fb.ERR_OVERLOAD)
        self.assertEqual(fake.written[4], fb.INSTR_SYNC_READ)

    def test_sync_read_request_byte_layout(self):
        # The FULL SYNC_READ request, not just instruction 0x82. Feetech 0x82:
        #   params = [addr=56(0x38), length=6(0x06), ids…=01 02 03]
        #   len byte = n_params + 2 = 5 + 2 = 7 (0x07)
        #   body = FE 07 82 38 06 01 02 03
        #   checksum = ~(0xFE+0x07+0x82+0x38+0x06+0x01+0x02+0x03) & 0xFF
        #            = ~(459 & 0xFF) & 0xFF = ~0xCB & 0xFF = 0x34
        replies = (_status(1, 0, bytes(6)) + _status(2, 0, bytes(6))
                   + _status(3, 0, bytes(6)))
        bus, fake = _bus(replies)
        bus.sync_read(fb.REG_PRESENT_POSITION, 6, [1, 2, 3])
        self.assertEqual(
            fake.written,
            bytes([0xFF, 0xFF, 0xFE, 0x07, 0x82, 0x38, 0x06,
                   0x01, 0x02, 0x03, 0x34]))

    def test_write_request_byte_layout(self):
        # The FULL WRITE request: addr THEN data. WRITE id 4, addr 40 (0x28,
        # Torque_Enable), data 0x01:
        #   params = [addr=40(0x28), data=0x01] → len byte = 2 + 2 = 4 (0x04)
        #   body = 04 04 03 28 01
        #   checksum = ~(0x04+0x04+0x03+0x28+0x01) & 0xFF = ~0x34 & 0xFF = 0xCB
        bus, fake = _bus(_status(4, 0))
        bus.write(4, fb.REG_TORQUE_ENABLE, b'\x01')
        self.assertEqual(
            fake.written,
            bytes([0xFF, 0xFF, 0x04, 0x04, 0x03, 0x28, 0x01, 0xCB]))

    def test_sync_write_shared_length_enforced(self):
        bus, _ = _bus()
        with self.assertRaises(ValueError):
            bus.sync_write(42, {1: b'\x00\x01', 2: b'\x00'})

    def test_write_returns_error_byte(self):
        bus, _ = _bus(_status(4, fb.ERR_OVERHEAT))
        self.assertEqual(bus.write(4, fb.REG_TORQUE_ENABLE, b'\x01'),
                         fb.ERR_OVERHEAT)


class TestTorqueEnableWriteRail(unittest.TestCase):
    """Register 40 is NOT a boolean: 128 means "calibrate the current position
    to 2048", i.e. the servo writes itself a fresh Homing_Offset. It replies no
    error, needs no EEPROM unlock, and leaves probe_bus's fingerprint (windows,
    mode, Phase) intact — so a computed value landing there re-datums the arm
    invisibly. These pin the rail that makes that unreachable."""

    def test_span_finds_the_byte_that_would_land_in_register_40(self):
        f = fb.torque_enable_byte_in_span
        # A bare 1-byte write AT the register.
        self.assertEqual(f(fb.REG_TORQUE_ENABLE, b'\x7f'), 0x7F)
        # The off-by-one that motivates a SPAN test: a 7-byte contiguous block
        # starting one register EARLY reaches 40 at payload index 6.
        self.assertEqual(f(fb.REG_TORQUE_ENABLE - 6, bytes(range(7))), 6)
        # The driver's REAL goal write starts at 41 — one register PAST 40 — so
        # it must never be inspected, whatever bytes it carries.
        self.assertIsNone(f(fb.REG_ACCELERATION, bytes([128] * 7)))
        # Ends just short / starts just past.
        self.assertIsNone(f(fb.REG_TORQUE_ENABLE - 1, b'\xff'))
        self.assertIsNone(f(fb.REG_TORQUE_ENABLE + 1, b'\xff'))

    def test_write_refuses_the_128_midpoint_recalibration(self):
        bus, fake = _bus(_status(4, 0))
        with self.assertRaises(ValueError) as ctx:
            bus.write(4, fb.REG_TORQUE_ENABLE,
                      bytes([fb.TORQUE_ENABLE_CALIBRATE_MIDPOINT]))
        # The message must name the consequence, not just "invalid value" — the
        # whole point is that the failure mode is silent re-datuming. 2048 is the
        # tick the vendor note („任意当前位置较正为2048") re-datums to.
        self.assertIn('re-datums', str(ctx.exception))
        self.assertIn('2048', str(ctx.exception))
        self.assertIn('128', str(ctx.exception))
        # …and NOTHING may reach the wire: a refused write that still wrote
        # would be worse than no guard at all.
        self.assertEqual(fake.written, b'')

    def test_write_refuses_every_value_outside_the_allowlist(self):
        for value in (2, 3, 127, 128, 129, 255):
            bus, fake = _bus(_status(4, 0))
            with self.assertRaises(ValueError):
                bus.write(4, fb.REG_TORQUE_ENABLE, bytes([value]))
            self.assertEqual(fake.written, b'')

    def test_sync_write_refuses_when_any_single_servo_carries_a_bad_byte(self):
        # SYNC_WRITE is a broadcast with no reply, so one bad byte would be both
        # undetectable AND applied to the whole arm.
        payload = {sid: b'\x01' for sid in range(1, 8)}
        payload[5] = bytes([fb.TORQUE_ENABLE_CALIBRATE_MIDPOINT])
        bus, fake = _bus()
        with self.assertRaises(ValueError):
            bus.sync_write(fb.REG_TORQUE_ENABLE, payload)
        self.assertEqual(fake.written, b'')

    def test_torque_off_can_never_be_refused(self):
        # THE dangerous misfire: shutdown()/SIGTERM/atexit torque-off is the only
        # thing standing between a dead host and a permanently energised arm
        # (the STS has no watchdog). A raise here would leave it energised.
        bus, fake = _bus()
        bus.sync_write(fb.REG_TORQUE_ENABLE,
                       {sid: b'\x00' for sid in range(1, 8)})
        self.assertNotEqual(fake.written, b'')
        for value in (fb.TORQUE_ENABLE_OFF, fb.TORQUE_ENABLE_ON):
            bus2, fake2 = _bus(_status(4, 0))
            bus2.write(4, fb.REG_TORQUE_ENABLE, bytes([value]))
            self.assertNotEqual(fake2.written, b'')

    def test_the_real_goal_write_is_never_refused_even_carrying_a_128_byte(self):
        # False-positive rail. The 7-byte goal block starts at REG_ACCELERATION
        # and legitimately contains arbitrary bytes: tick 128 encodes as b'\x80'
        # and GOAL_SPEED/accel can be anything. If the guard ever widened from a
        # span test to a value scan, THIS is the test that fails.
        tick = fb.TORQUE_ENABLE_CALIBRATE_MIDPOINT
        payload = (bytes([tick]) + fb.le16(tick) + fb.le16(tick)
                   + fb.le16(tick))
        bus, fake = _bus()
        bus.sync_write(fb.REG_ACCELERATION, {sid: payload for sid in range(1, 8)})
        self.assertNotEqual(fake.written, b'')

    def test_a_contiguous_write_overrunning_into_40_is_refused(self):
        # The bug class the span test exists for, end to end through the bus.
        bad = bytes([0, 0, 0, 0, 0, 0, fb.TORQUE_ENABLE_CALIBRATE_MIDPOINT])
        bus, fake = _bus(_status(1, 0))
        with self.assertRaises(ValueError):
            bus.write(1, fb.REG_TORQUE_ENABLE - 6, bad)
        self.assertEqual(fake.written, b'')

    def test_allowlist_is_exactly_off_and_on(self):
        self.assertEqual(fb.SAFE_TORQUE_ENABLE_VALUES,
                         frozenset({fb.TORQUE_ENABLE_OFF, fb.TORQUE_ENABLE_ON}))
        self.assertEqual(fb.TORQUE_ENABLE_OFF, 0)
        self.assertEqual(fb.TORQUE_ENABLE_ON, 1)
        self.assertEqual(fb.TORQUE_ENABLE_CALIBRATE_MIDPOINT, 128)
        self.assertNotIn(fb.TORQUE_ENABLE_CALIBRATE_MIDPOINT,
                         fb.SAFE_TORQUE_ENABLE_VALUES)


# ── node pure functions (ast-extracted; no rclpy import) ─────────────────────

def _load_edu6_geometry():
    """Import the driver's edu6_geometry.py directly — it is pure Python with
    no ROS or NumPy, precisely so this deps-free suite can use the real thing."""
    import importlib.util
    path = os.path.join(_DOCKER, 'edu6_geometry.py')
    spec = importlib.util.spec_from_file_location('edu6_geometry_for_tests',
                                                  path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_node_functions():
    path = os.path.join(_DOCKER, 'edu6_arm_node.py')
    source = open(path, encoding='utf-8').read()
    tree = ast.parse(source)
    ns = {
        'math': __import__('math'), 'os': os, 'fb': fb,
        # The driver's whole-link box model, imported by edu6_arm_node as `eg`
        # and consumed by the boot-home table-floor pre-check. Pure Python, so
        # this deps-free suite can hold the REAL module rather than a stub —
        # which is what lets the boot-path tests below exercise the real gate.
        'eg': _load_edu6_geometry(),
        'time': __import__('time'), 'threading': __import__('threading'),
        'CENTER_TICK': 2048, 'TICKS_PER_REV': 4096,
    }
    # Deterministic JOINT_SIGNS regardless of the dev machine's env: the env
    # parse path itself is pinned by test_parse_signs.
    env_backup = os.environ.pop('EDUBOTICS_EDU6_JOINT_SIGNS', None)
    tol_backup = os.environ.pop('EDUBOTICS_EDU6_BOOT_POS_TOL_TICKS', None)
    edge_backup = os.environ.pop('EDUBOTICS_EDU6_EDGE_MARGIN_TICKS', None)
    abort_backup = os.environ.pop('EDUBOTICS_EDU6_TORQUE_ABORT_TICKS', None)
    goal_backup = os.environ.pop('EDUBOTICS_EDU6_GOAL_EDGE_MARGIN_TICKS', None)
    home_tol_backup = os.environ.pop('EDUBOTICS_HOME_FLOOR_TOL_M', None)
    wanted = {'rad_to_tick', 'tick_to_rad', 'interpolate_trajectory',
              'build_boot_home', '_parse_signs', 'boot_home_verify_decision',
              '_parse_boot_pos_tolerance', '_parse_edge_margin',
              'edge_parked_joints', 'edge_refusal_message',
              '_parse_torque_abort_ticks', 'wrong_way_joints',
              'wrong_way_abort_message', 'post_energise_read_failure_message',
              '_parse_goal_edge_margin', 'rad_to_command_tick',
              'firmware_fingerprint_notes', 'angular_resolution_note',
              '_parse_home_floor_tol', 'boot_home_floor_decision'}
    consts = {'RAD_PER_TICK', 'HOME_JOINTS_RAD', 'GRIPPER_OPEN_RAD',
              'LOOP_HZ', 'BOOT_HOME_DURATION_S', 'SERVO_IDS', 'JOINT_NAMES',
              'JOINT_LIMITS_RAD', '_DEFAULT_SIGNS', 'JOINT_SIGNS',
              'BOOT_TORQUE_ON_ATTEMPTS', 'BOOT_TORQUE_ON_RETRY_S',
              'SEED_SPEED_STEPS', 'PRESENT_LOAD_FULLSCALE',
              'GOAL_SPEED_CAP_STEPS', 'WRITE_ACCELERATION',
              '_DEFAULT_BOOT_POSITION_TOLERANCE_TICKS',
              'BOOT_POSITION_TOLERANCE_TICKS',
              '_DEFAULT_EDGE_REFUSE_MARGIN_TICKS', 'EDGE_REFUSE_MARGIN_TICKS',
              '_EDGE_MARGIN_MAX_TICKS',
              '_DEFAULT_TORQUE_ABORT_ERROR_TICKS', '_TORQUE_ABORT_MIN_TICKS',
              '_TORQUE_ABORT_MAX_TICKS', 'TORQUE_ABORT_ERROR_TICKS',
              'POST_ENERGISE_READ_ATTEMPTS', '_TORQUE_OFF_PAYLOAD',
              'BOOT_PHYSICAL_REMEDY_DE', 'PHYSICAL_TORQUE_REFUSALS',
              '_DEFAULT_BOOT_HOME_FLOOR_TOL_M', 'BOOT_HOME_FLOOR_TOL_M',
              'BOOT_HOME_FLOOR_REFUSAL_DE',
              '_DEFAULT_GOAL_EDGE_MARGIN_TICKS',
              '_GOAL_EDGE_MARGIN_MAX_TICKS', 'GOAL_EDGE_MARGIN_TICKS',
              'FIRMWARE_SUSPECT_VERSIONS'}
    # Instance methods extracted as plain functions and bound onto a stub self
    # (_TorqueStubNode) — the torque path is BEHAVIOUR, not something a source
    # grep can prove, and rclpy is not installed here.
    methods = {'probe_bus', '_seed_goal_from_present_locked', 'set_torque',
               '_torque_cb', 'start_boot_home',
               '_verify_after_energise_locked', '_write_targets'}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in consts
                for t in node.targets):
            exec(compile(ast.Module([node], []), path, 'exec'), ns)  # noqa: S102
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            src = textwrap.dedent(ast.get_source_segment(source, node))
            exec(compile(src, path, 'exec'), ns)  # noqa: S102
        # probe_bus is a staticmethod on the class — extract it as a plain
        # function (get_source_segment starts at the `def`, past decorators)
        # so the H1/H2 provisioning-fingerprint gate is deps-free-testable.
        if isinstance(node, ast.ClassDef) and node.name == 'Edu6ArmNode':
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name in methods:
                    src = textwrap.dedent(ast.get_source_segment(source, sub))
                    exec(compile(src, path, 'exec'), ns)  # noqa: S102
    if env_backup is not None:
        os.environ['EDUBOTICS_EDU6_JOINT_SIGNS'] = env_backup
    if tol_backup is not None:
        os.environ['EDUBOTICS_EDU6_BOOT_POS_TOL_TICKS'] = tol_backup
    if edge_backup is not None:
        os.environ['EDUBOTICS_EDU6_EDGE_MARGIN_TICKS'] = edge_backup
    if abort_backup is not None:
        os.environ['EDUBOTICS_EDU6_TORQUE_ABORT_TICKS'] = abort_backup
    if goal_backup is not None:
        os.environ['EDUBOTICS_EDU6_GOAL_EDGE_MARGIN_TICKS'] = goal_backup
    if home_tol_backup is not None:
        os.environ['EDUBOTICS_HOME_FLOOR_TOL_M'] = home_tol_backup
    return ns


_N = _load_node_functions()


class TestNodePureFunctions(unittest.TestCase):
    def test_tick_conversion_round_trip(self):
        for rad in (-1.5, -0.5, 0.0, 0.7, 1.9):
            for sign in (1, -1):
                tick = _N['rad_to_tick'](rad, sign)
                back = _N['tick_to_rad'](tick, sign)
                self.assertAlmostEqual(back, rad, delta=2 * 3.1416 / 4096)

    def test_tick_center_is_zero(self):
        self.assertEqual(_N['rad_to_tick'](0.0, 1), 2048)
        self.assertEqual(_N['tick_to_rad'](2048, 1), 0.0)

    def test_tick_clamps_to_register_range(self):
        self.assertEqual(_N['rad_to_tick'](10.0, 1), 4095)
        self.assertEqual(_N['rad_to_tick'](-10.0, 1), 0)

    def test_interpolate_holds_ends(self):
        pts = [([0.0] * 7, 0.1), ([1.0] * 7, 1.1)]
        self.assertEqual(_N['interpolate_trajectory'](pts, 0.0), [0.0] * 7)
        self.assertEqual(_N['interpolate_trajectory'](pts, 5.0), [1.0] * 7)
        mid = _N['interpolate_trajectory'](pts, 0.6)
        self.assertAlmostEqual(mid[0], 0.5, places=9)
        self.assertIsNone(_N['interpolate_trajectory']([], 0.0))

    def test_boot_home_ends_at_home_open(self):
        current = [0.1, 0.9, -2.0, 0.05, 0.9, 0.1, 0.4]
        pts = _N['build_boot_home'](current)
        self.assertGreater(len(pts), 100)
        end_q, end_t = pts[-1]
        self.assertEqual(end_q[:6], list(_N['HOME_JOINTS_RAD']))
        self.assertEqual(end_q[6], _N['GRIPPER_OPEN_RAD'])
        self.assertAlmostEqual(end_t, _N['BOOT_HOME_DURATION_S'], places=9)
        # starts FROM the current pose (blend 0) — no jump.
        first_q, _ = pts[0]
        for a, b in zip(first_q, current):
            self.assertAlmostEqual(a, b, delta=0.02)

    def test_boot_home_verify_decision(self):
        # Decision A: OMX-style arrival verify (tolerance-based, arm joints only,
        # bounded re-send). Pure decision function — the timing/thread lives in
        # the node method exercised by the source pin below.
        f = _N['boot_home_verify_decision']
        home = list(_N['HOME_JOINTS_RAD'])          # 6 arm joints, gripper excl.
        self.assertEqual(len(home), 6)
        self.assertEqual(f(home, home, 0.30, 0, 1), ('arrived', []))
        # within tolerance; an extra 7th (gripper) entry is ignored
        near = [h + 0.2 for h in home] + [1.75]
        self.assertEqual(f(near, home, 0.30, 0, 1)[0], 'arrived')
        # joint 3 off by 0.5 (>tol), attempt 0 of max 1 -> resend, named 1-based
        off = list(home)
        off[2] += 0.5
        self.assertEqual(f(off, home, 0.30, 0, 1), ('resend', [3]))
        # same miss on the LAST attempt -> give_up (no more re-sends)
        self.assertEqual(f(off, home, 0.30, 1, 1)[0], 'give_up')
        # verify-only (max_sends 0): a miss goes straight to give_up
        self.assertEqual(f(off, home, 0.30, 0, 0)[0], 'give_up')
        # missing / short data -> nodata, never a false 'arrived'
        self.assertEqual(f(None, home, 0.30, 0, 1)[0], 'nodata')
        self.assertEqual(f([0.0, 0.0], home, 0.30, 0, 1)[0], 'nodata')

    def test_parse_signs(self):
        self.assertEqual(_N['_parse_signs'](None), _N['_DEFAULT_SIGNS'])
        self.assertEqual(_N['_parse_signs'](''), _N['_DEFAULT_SIGNS'])
        self.assertEqual(_N['_parse_signs']('1,-1,1,1,-1,1,1'),
                         (1, -1, 1, 1, -1, 1, 1))
        self.assertEqual(_N['_parse_signs']('1,2,3'), _N['_DEFAULT_SIGNS'])
        self.assertEqual(_N['_parse_signs']('0,0,0,0,0,0,0'),
                         _N['_DEFAULT_SIGNS'])

    def test_node_constants_mirror_profile(self):
        # No-drift vs the server registry (mirrors the OMX no-drift family).
        self.assertEqual(_N['JOINT_NAMES'],
                         ('joint1', 'joint2', 'joint3', 'joint4', 'joint5',
                          'joint6', 'end_gear_joint'))
        self.assertEqual(_N['HOME_JOINTS_RAD'],
                         (0.0, 0.70, -2.40, 0.0, 0.70, 0.0))
        self.assertEqual(_N['GRIPPER_OPEN_RAD'], 1.75)
        self.assertEqual(_N['SERVO_IDS'], (1, 2, 3, 4, 5, 6, 7))
        limits = _N['JOINT_LIMITS_RAD']
        self.assertEqual(limits[4], (-1.5708, 1.9199))  # the asymmetric relief
        self.assertEqual(len(limits), 7)


class TestProbeBusGate(unittest.TestCase):
    """probe_bus provisioning-fingerprint gate (audit H1+H2), extracted
    deps-free: an unprovisioned / RMA-swapped / wheel-mode / signs-drifted
    arm must refuse to boot in German instead of boot-homing in a wrong
    coordinate frame at factory torque."""

    @staticmethod
    def _probe():
        pb = _N['probe_bus']
        return pb.__func__ if isinstance(pb, staticmethod) else pb

    class _RegBus:
        def __init__(self, windows=None, mode=0, alive=None, models=None,
                     phase=0, present=None, firmware=None, resolution=None):
            self.mode = mode
            self.phase = phase
            self.alive = set(alive if alive is not None else range(1, 8))
            self.models = models or {}
            # EDU6-0001 reads 3.10 on all seven and Angular_Resolution is the
            # factory 1, so the DEFAULT fake is the reference arm and must stay
            # completely silent.
            self.firmware = dict(firmware or {})
            self.resolution = dict(resolution or {})
            if windows is None:
                windows = {
                    sid: fb.position_limit_window(
                        *_N['JOINT_LIMITS_RAD'][i], 1)
                    for i, sid in enumerate(range(1, 8))
                }
            self.windows = windows
            # CENTER_TICK sits inside every designed window, so the default arm
            # is plausible on all 7 joints.
            self.present = dict(present or {})

        def ping(self, sid, timeout_s=None):
            return sid in self.alive

        def read(self, sid, addr, length):
            if addr == fb.REG_OPERATING_MODE:
                return 0, bytes([self.mode])
            if addr == fb.REG_PHASE:
                return 0, bytes([self.phase])
            if addr == fb.REG_FIRMWARE_MAJOR and length == 2:
                return 0, bytes(self.firmware.get(sid, (3, 10)))
            if addr == fb.REG_ANGULAR_RESOLUTION:
                return 0, bytes([self.resolution.get(
                    sid, fb.ANGULAR_RESOLUTION_SINGLE_TURN)])
            raise AssertionError(f'unexpected read addr {addr}')

        def read_u16(self, sid, addr):
            if addr == fb.REG_MODEL_NUMBER:
                return self.models.get(sid, fb.STS3215_MODEL_NUMBER)
            if addr == fb.REG_MIN_POSITION_LIMIT:
                return self.windows[sid][0]
            if addr == fb.REG_MAX_POSITION_LIMIT:
                return self.windows[sid][1]
            if addr == fb.REG_PRESENT_POSITION:
                return self.present.get(sid, 2048)
            raise AssertionError(f'unexpected read_u16 addr {addr}')

    class _CaptureLogger:
        """probe_bus's long-unused `logger` parameter is the non-fatal channel."""

        def __init__(self):
            self.warnings = []

        def warning(self, text):
            self.warnings.append(text)

    def test_provisioned_arm_passes(self):
        ok, msg = self._probe()(self._RegBus())
        self.assertTrue(ok)
        self.assertEqual(msg, '')

    def test_the_reference_arm_boots_completely_silently(self):
        # EDU6-0001: uniform firmware 3.10, Angular_Resolution at the factory 1.
        # A boot advisory on a healthy arm is log noise that trains operators to
        # ignore the channel, so silence here is load-bearing.
        log = self._CaptureLogger()
        ok, msg = self._probe()(self._RegBus(), logger=log)
        self.assertTrue(ok)
        self.assertEqual(msg, '')
        self.assertEqual(log.warnings, [])

    # ── Angular_Resolution (register 30) — WARN, never refuse ────────────────
    def test_a_rescaled_angular_resolution_warns_but_STILL_BOOTS(self):
        # THE brick-avoidance test. EDU6-0001 was provisioned before register 30
        # was known about, so its live value has never been read; a hard gate on a
        # value we cannot predict would refuse „Umgebung starten" on the only arm
        # that exists, with no env knob to escape with. If someone later promotes
        # this to `return False`, THIS is the test that must be consciously
        # rewritten rather than quietly deleted.
        log = self._CaptureLogger()
        bus = self._RegBus(resolution={3: 2})
        ok, msg = self._probe()(bus, logger=log)
        self.assertTrue(ok, msg)
        self.assertEqual(msg, '')
        self.assertEqual(len(log.warnings), 1)
        note = log.warnings[0]
        self.assertIn('[WARNUNG]', note)
        self.assertIn('Servo 3', note)
        self.assertIn('30', note)                 # names the register
        self.assertIn('edu6_provision', note)     # and the remedy

    def test_the_resolution_note_names_every_offender(self):
        f = _N['angular_resolution_note']
        self.assertEqual(f([]), '')
        both = f([(2, 3), (5, 2)])
        self.assertIn('Servo 2', both)
        self.assertIn('Servo 5', both)

    def test_resolution_is_read_from_register_30_not_guessed(self):
        # A strict fake: _RegBus raises AssertionError on any address it does not
        # model, so this fails loudly if the probe ever reads a different register.
        self.assertEqual(fb.REG_ANGULAR_RESOLUTION, 30)
        self.assertEqual(fb.ANGULAR_RESOLUTION_SINGLE_TURN, 1)
        # …and 30 must not collide with its neighbours (28-29 protection current,
        # 31-32 homing offset).
        self.assertEqual(fb.REG_PROTECTION_CURRENT + 2,
                         fb.REG_ANGULAR_RESOLUTION)
        self.assertEqual(fb.REG_ANGULAR_RESOLUTION + 1, fb.REG_HOMING_OFFSET)

    # ── firmware fingerprint — WARN, never refuse ────────────────────────────
    def test_a_nonuniform_firmware_bus_warns_but_STILL_BOOTS(self):
        log = self._CaptureLogger()
        bus = self._RegBus(firmware={4: (3, 7)})
        ok, msg = self._probe()(bus, logger=log)
        self.assertTrue(ok, msg)
        joined = ' | '.join(log.warnings)
        self.assertIn('[WARNUNG]', joined)
        self.assertIn('3.7', joined)
        self.assertIn('3.10', joined)     # the majority version is listed too

    def test_suspect_firmware_warns_but_STILL_BOOTS_and_hedges(self):
        # Refusing 3.9 was the brief's ask; the evidence (one confounded report)
        # does not support it — see FIRMWARE_SUSPECT_VERSIONS. So: named, hedged,
        # never a refusal.
        log = self._CaptureLogger()
        bus = self._RegBus(firmware={s: (3, 9) for s in range(1, 8)})
        ok, msg = self._probe()(bus, logger=log)
        self.assertTrue(ok, msg)
        self.assertEqual(msg, '')
        self.assertEqual(len(log.warnings), 1)   # uniform → only the suspect note
        note = log.warnings[0]
        self.assertIn('3.9', note)
        self.assertIn('unbestätigt', note)       # the hedge is the point

    def test_firmware_notes_are_pure_and_silent_on_a_healthy_bus(self):
        f = _N['firmware_fingerprint_notes']
        self.assertEqual(f({}), [])
        self.assertEqual(f({s: (3, 10) for s in range(1, 8)}), [])
        # non-uniform AND suspect → BOTH findings, not just the first.
        mixed = {s: (3, 10) for s in range(1, 8)}
        mixed[2] = (3, 9)
        self.assertEqual(len(f(mixed)), 2)

    def test_the_suspect_set_is_a_denylist_not_a_pinned_version(self):
        # Pinning "must be 3.10" would refuse a legitimately NEWER firmware on an
        # RMA servo. 3.11 and 4.0 must be unremarkable.
        f = _N['firmware_fingerprint_notes']
        self.assertEqual(_N['FIRMWARE_SUSPECT_VERSIONS'], frozenset({(3, 9)}))
        for ver in ((3, 10), (3, 11), (4, 0)):
            self.assertEqual(f({s: ver for s in range(1, 8)}), [])

    def test_the_seed_docstring_does_not_revert_to_the_backwards_reg42_promise(self):
        # A3. An earlier revision promised that reading Goal_Position (42) back
        # "becomes a free upgrade that closes both residuals at once" once the
        # raw-vs-clamped question was settled. It was settled — RAW (LeRobot
        # #3585) — which makes reading 42 the WRONG model for the abort
        # comparison, not an upgrade. This pins the correction against a
        # well-meaning revert, since prose has no other guard.
        src = open(os.path.join(_DOCKER, 'edu6_arm_node.py'),
                   encoding='utf-8').read()
        start = src.index('def _seed_goal_from_present_locked')
        doc = src[start:src.index('replies = self._bus.sync_read', start)]
        # "free upgrade" may appear ONCE — as the quoted old promise — and only
        # ahead of its retraction. An unqualified reinstatement would put it after.
        self.assertLessEqual(doc.count('free upgrade'), 1)
        if 'free upgrade' in doc:
            self.assertLess(doc.index('free upgrade'),
                            doc.index('THAT PROMISE WAS BACKWARDS'))
        self.assertIn('RAW', doc)
        self.assertIn('#3585', doc)
        # The corrective claim itself must be present, not merely the retraction.
        self.assertIn('COMPUTING THE CLAMP IS THE CORRECT MODEL', doc)
        # Register 67 must never be asserted as the answer — it is 无定义 in the
        # current official table and absent from V3.7.
        self.assertIn('UNSUBSTANTIATED', doc)
        self.assertIn('71', doc)

    def test_a_dead_bus_still_reports_a_cable_fault_not_a_firmware_fault(self):
        # The firmware/resolution reads live in the SAME try as the EEPROM reads,
        # so a read failure must keep its existing „Verkabelung" verdict.
        class _Dead(TestProbeBusGate._RegBus):
            def read(self, sid, addr, length):
                if addr == fb.REG_FIRMWARE_MAJOR:
                    raise fb.FeetechBusError('no reply')
                return super().read(sid, addr, length)

        ok, msg = self._probe()(_Dead())
        self.assertFalse(ok)
        self.assertIn('Verkabelung', msg)
        self.assertNotIn('Firmware', msg)

    def test_factory_limits_refused_as_unprovisioned(self):
        bus = self._RegBus()
        bus.windows[2] = (0, 4095)   # factory window on joint2
        ok, msg = self._probe()(bus)
        self.assertFalse(ok)
        self.assertIn('Servo 2', msg)
        self.assertIn('rovision', msg)   # provisioniert/Provisionierung

    def test_wheel_mode_refused(self):
        ok, msg = self._probe()(self._RegBus(mode=1))
        self.assertFalse(ok)
        self.assertIn('Betriebsmodus', msg)

    def test_mixed_sts3250_joints_pass(self):
        # Joints 2/3 = STS3250 (2825) by design — a provisioned mixed arm boots.
        models = {2: fb.STS3250_MODEL_NUMBER, 3: fb.STS3250_MODEL_NUMBER}
        ok, msg = self._probe()(self._RegBus(models=models))
        self.assertTrue(ok, msg)

    def test_wrong_model_refused_at_boot(self):
        ok, msg = self._probe()(self._RegBus(models={4: 1234}))
        self.assertFalse(ok)
        self.assertIn('Servomodell', msg)

    def test_signs_drift_refused_naming_the_env_knob(self):
        # Provisioned with sign −1 on joint 2 (mirrored EEPROM window) while
        # the runtime signs say +1 → refuse and name the knob: commanding
        # through mismatched windows freezes/clamps the joint at hardware
        # level (CLAUDE.md: a sign flip requires re-provisioning).
        bus = self._RegBus()
        bus.windows[2] = fb.position_limit_window(
            *_N['JOINT_LIMITS_RAD'][1], -1)
        ok, msg = self._probe()(bus)
        self.assertFalse(ok)
        self.assertIn('EDUBOTICS_EDU6_JOINT_SIGNS', msg)

    def test_dead_servos_still_named_first(self):
        ok, msg = self._probe()(self._RegBus(alive={1, 2, 3}))
        self.assertFalse(ok)
        self.assertIn('[4, 5, 6, 7]', msg)

    def test_multiturn_phase_bit_refused(self):
        # A factory-reset / RMA servo shipping Phase bit 4 (multi-turn) would
        # feed overflowed angles into the fixed tick↔angle map; provisioning
        # clears it, the boot probe must refuse it (else boot-home runs skewed).
        ok, msg = self._probe()(self._RegBus(phase=(1 << 4)))
        self.assertFalse(ok)
        self.assertIn('Servo 1', msg)
        self.assertIn('Phase-Bit 4', msg)

    def test_phase_bit_other_than_4_ignored(self):
        # Only bit 4 is the multi-turn flag — other Phase bits are motor-drive
        # config the probe must NOT reject (a provisioned arm can carry them).
        ok, msg = self._probe()(self._RegBus(phase=0b0000_1011))
        self.assertTrue(ok, msg)

    def test_wrapped_position_refused(self):
        # Hand-guiding is torque-OFF, so a student can push a joint past ±180°
        # of its zero; the encoder then reports the OTHER side of the tick<->angle
        # map (a 360° error) and boot-home would drive it the long way into its
        # stop. joint2's window is [2048,4095]; a wrapped reading lands near 0.
        ok, msg = self._probe()(self._RegBus(present={2: 5}))
        self.assertFalse(ok)
        self.assertIn('Servo 2', msg)
        self.assertIn('von Hand', msg)

    def test_position_at_a_hard_stop_still_boots(self):
        # The band must not nuisance-refuse a joint resting AGAINST its stop —
        # that is a healthy arm, and this probe runs BEFORE torque-on (i.e. on
        # an arm that has flopped under gravity), so a limp joint sitting a bit
        # past a designed limit is the NORMAL case, not a fault.
        tol = _N['BOOT_POSITION_TOLERANCE_TICKS']
        # >= 30° of slop: the original 10° band would have refused a limp
        # joint5 settling 11° past its −90° stop (2026-07-25 sizing decision).
        self.assertGreaterEqual(tol, 342)
        # Every reading here is a POSSIBLE one, i.e. inside the 12-bit register:
        # joint2 resting `tol−1` ticks below its 2048 minimum and joint5 the same
        # distance below its 1024 minimum. (The upper stops of joint2/joint3 sit
        # AT a register end, so "just past" is unrepresentable there — that is the
        # known-accepted blindness the edge guard covers, not something the band
        # can be tested against with an out-of-register tick.)
        bus = self._RegBus(present={2: 2048 - tol + 1, 3: 2048,
                                    5: 1024 - tol + 1})
        ok, msg = self._probe()(bus)
        self.assertTrue(ok, msg)

    def test_band_still_catches_a_wrap_on_every_detectable_joint(self):
        # Widening the band must not cost DETECTION. A hand-turn past ±180° of
        # a joint's zero makes the encoder report from the other side of the
        # tick↔angle map, which lands the reading at tick ≈0 or ≈4095. For each
        # joint with a detectable (non-full-circle) window, BOTH wrap landings
        # must still be refused at the shipped band width.
        tol = _N['BOOT_POSITION_TOLERANCE_TICKS']
        limits = _N['JOINT_LIMITS_RAD']
        checked = 0
        for idx in range(7):
            lo, hi = fb.position_limit_window(*limits[idx], 1)
            if (lo, hi) == (0, 4095):
                continue                      # joint4/joint6: documented no-op
            checked += 1
            # A window that TOUCHES a register end cannot see the wrap landing
            # on that side at ANY band width — joint2 ends at 4095, joint3
            # starts at 0. That is a property of the designed window, not of
            # the band, so only the genuinely-outside landings are required.
            detectable = [t for t in (0, 4095) if t < lo or t > hi]
            self.assertTrue(detectable, f'joint{idx + 1} detects no wrap at all')
            for landing in detectable:
                self.assertFalse(
                    lo - tol <= landing <= hi + tol,
                    f'joint{idx + 1} window [{lo},{hi}] with band {tol} no '
                    f'longer detects a wrap landing at tick {landing}')
                ok, msg = self._probe()(
                    self._RegBus(present={idx + 1: landing}))
                self.assertFalse(ok, f'joint{idx + 1} @ {landing}')
                self.assertIn(f'Servo {idx + 1}', msg)
        self.assertEqual(checked, 5)          # joints 1,2,3,5 + gripper

    def test_boot_pos_tolerance_env_parse(self):
        # Bench knob for rig gate R9: retunable per rig without an image
        # rebuild. Malformed/negative input falls back to the default rather
        # than silently running a band nobody chose.
        f = _N['_parse_boot_pos_tolerance']
        default = _N['_DEFAULT_BOOT_POSITION_TOLERANCE_TICKS']
        self.assertEqual(default, 400)
        self.assertEqual(f(None), default)
        self.assertEqual(f(''), default)
        self.assertEqual(f('   '), default)
        self.assertEqual(f(' 250 '), 250)
        self.assertEqual(f('0'), 0)           # 0 = strictest, still legal
        self.assertEqual(f('4096'), 4096)     # >= one turn = check disabled
        self.assertEqual(f('abc'), default)
        self.assertEqual(f('-5'), default)

    def test_full_circle_window_band_is_a_no_op_but_the_edge_guard_is_not(self):
        # DOCUMENTED no-op: joint4/joint6 keep [0,4095] under the keep-±180°
        # decision, so no reading is ever OUT OF RANGE there and the plausibility
        # BAND can never fire. Pinning it keeps the limitation visible.
        #
        # Since 2026-07-26 those two joints are not unguarded, though: the
        # torque-on edge guard refuses a reading ON the map edge, because the STS
        # position loop is not wrap-aware (docs §11.11). Both halves are pinned
        # together so neither can be lost while the other looks fine.
        band = _N['BOOT_POSITION_TOLERANCE_TICKS']
        for sid in (4, 6):
            lo, hi = fb.position_limit_window(
                *_N['JOINT_LIMITS_RAD'][sid - 1], 1)
            self.assertEqual((lo, hi), (0, 4095))
            for tick in (0, 2048, 4095):
                self.assertTrue(lo - band <= tick <= hi + band)  # band inert
            # A mid-range reading boots (nothing judges these joints there)…
            ok, msg = self._probe()(self._RegBus(present={sid: 2048}))
            self.assertTrue(ok, msg)
            # …while both map-edge readings are now refused, by the edge guard.
            for tick in (0, 4095):
                ok, msg = self._probe()(self._RegBus(present={sid: tick}))
                self.assertFalse(ok, f'servo {sid} @ {tick}')
                self.assertIn('Positionsbereichs', msg)


class _TorqueFakeBus:
    """Register-light bus fake for the torque path: answers the 2-byte
    Present_Position sync_read the goal-seed performs and records every
    sync_write, so a REFUSED torque-on can be proven to have written nothing.

    ``sync_read`` calls are COUNTED, because the torque path makes two kinds:
    #1 is the goal seed (before Torque_Enable), #2+ are the post-energise
    verification reads. ``post_ticks`` is served from #2 onward — that is how a
    joint that MOVED between the seed and the verification (the measured
    wrong-way runaway) is expressed. ``missing_at`` drops the named servos on one
    specific read index, so a single dropped CDC-ACM reply can be told apart from
    a genuinely silent servo."""

    def __init__(self, ticks=None, missing=(), post_ticks=None,
                 missing_at=None):
        self.ticks = dict(ticks or {})
        self.missing = set(missing)
        self.post_ticks = dict(post_ticks or {})
        self.missing_at = {int(k): set(v)
                           for k, v in (missing_at or {}).items()}
        self.reads = 0
        self.writes = []                  # [(addr, {sid: bytes})] in call order

    def sync_read(self, addr, length, ids):
        assert addr == fb.REG_PRESENT_POSITION, addr
        self.reads += 1
        table = dict(self.ticks)
        if self.reads >= 2:
            table.update(self.post_ticks)
        gone = self.missing | self.missing_at.get(self.reads, set())
        out = {}
        for sid in ids:
            if sid in gone:
                continue                  # a silent servo is simply absent
            raw = fb.encode_sign_magnitude(table.get(sid, 2048), 15)
            data = (fb.le16(raw) + bytes(6))[:length]
            out[sid] = (0, data)
        return out

    def sync_write(self, addr, per_servo):
        self.writes.append((addr, dict(per_servo)))

    def addrs(self):
        return [addr for addr, _ in self.writes]

    def torque_writes(self):
        """[(sid, value)] for every Torque_Enable byte that reached the bus."""
        return [(sid, payload[0])
                for addr, per in self.writes if addr == fb.REG_TORQUE_ENABLE
                for sid, payload in sorted(per.items())]


class _TorqueStubNode:
    """Everything the AST-extracted torque methods touch on ``self`` — no rclpy,
    no real threads of ours. The methods themselves are the SHIPPED ones."""

    def __init__(self, bus):
        self._bus = bus
        self._bus_lock = threading.Lock()
        # Unused by the stubbed _replace_trajectory below, but present because
        # the REAL node has it: without it a mutation that swaps the bus lock for
        # the trajectory lock would die of AttributeError instead of the wrong
        # lock semantics, i.e. the guard would pass for the wrong reason.
        self._traj_lock = threading.Lock()
        self._stop = threading.Event()
        self._torque_on = False
        self._bus_fault = False
        self._torque_refusal = None
        self.errors, self.warnings, self.infos = [], [], []
        self.traj_calls = []
        self.verify_calls = []
        self.read_positions = [0.0, 0.70, -2.40, 0.0, 0.70, 0.0, 1.75]

    # --- logger seam -------------------------------------------------------
    def get_logger(self):
        return self

    def error(self, msg):
        self.errors.append(msg)

    def warning(self, msg):
        self.warnings.append(msg)

    def info(self, msg):
        self.infos.append(msg)

    # --- collaborators the torque path calls --------------------------------
    def _replace_trajectory(self, points, start_mono=None):
        self.traj_calls.append((list(points), start_mono))
        return len(self.traj_calls)

    def _read_positions_once(self):
        return list(self.read_positions) if self.read_positions else None

    def _boot_home_verify(self, gen):
        self.verify_calls.append(gen)


for _bound in ('_seed_goal_from_present_locked', 'set_torque', '_torque_cb',
               'start_boot_home', '_verify_after_energise_locked',
               '_write_targets'):
    setattr(_TorqueStubNode, _bound, _N[_bound])


class _Req:
    def __init__(self, data):
        self.data = data


class _Resp:
    success = None
    message = None


class _FakeTime:
    """time module stand-in: records sleeps instead of taking them."""

    def __init__(self):
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)

    def monotonic(self):
        return 0.0


class _SyncThread:
    """threading.Thread stand-in that runs the target INLINE on start() — the
    boot-home verifier is a daemon thread, and asserting on it otherwise races."""

    def __init__(self, target=None, args=(), name=None, daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


class _SyncThreading:
    Thread = _SyncThread
    Event = threading.Event
    Lock = threading.Lock


class TestTorqueOnEdgeGuard(unittest.TestCase):
    """Measured 2026-07-26 on arm EDU6-0001 (docs §11.11): the STS position loop
    is NOT wrap-aware — it computes `goal − present` naively over 0..4095, so a
    goal seeded on a joint resting ON the map edge drives a FULL REVOLUTION the
    wrong way (joint6 at tick 12 with goal 4090 drove upward until the bench tool
    cut torque). set_torque seeds `Goal = Present` read milliseconds earlier, so
    one tick of sensor dither at the boundary is enough. The guard refuses EVERY
    torque-on in that state; torque-OFF is never blocked."""

    def _node(self, ticks=None, missing=(), post_ticks=None, missing_at=None):
        bus = _TorqueFakeBus(ticks, missing, post_ticks, missing_at)
        return _TorqueStubNode(bus), bus

    # ── the refusal ────────────────────────────────────────────────────────
    def test_service_refuses_torque_on_for_a_joint_on_the_map_edge(self):
        node, bus = self._node({6: 4093})
        resp = node._torque_cb(_Req(True), _Resp())
        self.assertFalse(resp.success)
        self.assertFalse(node._torque_on)
        # The SetBool reply carries the reason (this is the „Beenden"/hand-guide
        # exit path — a bare 'fehlgeschlagen' sends the student to the cables).
        self.assertIn('[FEHLER]', resp.message)
        self.assertIn('Gelenk 6', resp.message)
        self.assertIn('Positionsbereichs', resp.message)
        self.assertIn('zurückdrehen', resp.message)   # actionable + umlauts
        self.assertNotIn('Kabel', resp.message)
        self.assertEqual(node._torque_refusal[0], 'edge')
        # It is also in the Protokoll…
        self.assertEqual(node.errors, [resp.message])
        # …and NOTHING reached the bus: no goal seed, no Torque_Enable.
        self.assertEqual(bus.writes, [])

    def test_both_torque_services_share_the_guarded_callback(self):
        # /edu6/set_torque and the legacy /dynamixel_hardware_interface/
        # set_dxl_torque alias must both route through _torque_cb → set_torque,
        # or one entry point would bypass the guard.
        src = open(os.path.join(_DOCKER, 'edu6_arm_node.py'),
                   encoding='utf-8').read()
        block = src.split('# Torque service', 1)[1].split(
            'self._read_thread', 1)[0]
        self.assertIn("'/edu6/set_torque'", block)
        self.assertIn("'/dynamixel_hardware_interface/set_dxl_torque'", block)
        self.assertEqual(block.count('self._torque_cb'), 2)
        cb = src.split('def _torque_cb', 1)[1].split('\n    def ', 1)[0]
        self.assertIn('self.set_torque(bool(request.data))', cb)

    def test_edge_of_the_band_is_pinned_from_both_sides(self):
        margin = _N['EDGE_REFUSE_MARGIN_TICKS']
        # dist == margin → allowed (both ends of the register range)
        node, _ = self._node({6: margin, 4: (4096 - 1) - margin})
        self.assertTrue(node.set_torque(True), node.errors)
        # dist == margin − 1 → refused (both ends)
        for ticks in ({6: margin - 1}, {4: (4096 - 1) - (margin - 1)}):
            node, bus = self._node(ticks)
            self.assertFalse(node.set_torque(True))
            self.assertEqual(bus.writes, [])

    def test_multiple_offenders_are_all_named_with_correct_grammar(self):
        msg = _N['edge_refusal_message']([(4, 1), (6, 4095)],
                                         self_healing=False)
        self.assertIn('Gelenk 4', msg)
        self.assertIn('Gelenk 6', msg)
        self.assertIn('stehen', msg)          # plural verb
        one = _N['edge_refusal_message']([(6, 4095)], self_healing=False)
        self.assertIn('steht ', one)

    # ── no nuisance ────────────────────────────────────────────────────────
    def test_healthy_mid_range_arm_still_torques_on(self):
        node, bus = self._node()                    # every joint at tick 2048
        self.assertTrue(node.set_torque(True))
        self.assertTrue(node._torque_on)
        self.assertIsNone(node._torque_refusal)
        self.assertEqual(node.errors, [])
        # goal seed first, THEN Torque_Enable (the pre-existing ordering rail).
        self.assertEqual(bus.addrs(),
                         [fb.REG_ACCELERATION, fb.REG_TORQUE_ENABLE])
        self.assertEqual(bus.torque_writes(), [(sid, 1) for sid in range(1, 8)])

    def test_the_guard_only_judges_the_OFF_to_ON_transition(self):
        # The server asserts torque ON defensively on EVERY WorkshopJog call, so
        # judging an already-locked arm made any jog beyond ±176.5° on
        # joint4/joint6 — inside the URDF window the jog accepts — fail, and
        # three of those in a row produce „Arm konnte wiederholt nicht verriegelt
        # werden … Umgebung neu starten": a misdiagnosis (the arm IS locked)
        # whose remedy probe_bus then refuses at the same tick.
        #
        # It is a false alarm because the wrapped error needs `present` to cross
        # the 4095/0 seam between the seed read and the write, and a torqued
        # joint is tracking a monotonic tick goal our own write loop set — it
        # cannot cross, and the seed it gets is the goal it already holds.
        node, bus = self._node({6: 4094})
        node._torque_on = True                    # already locked and holding
        self.assertTrue(node.set_torque(True), node.errors)
        self.assertIsNone(node._torque_refusal)
        self.assertEqual(node.errors, [])
        self.assertEqual(bus.addrs(),
                         [fb.REG_ACCELERATION, fb.REG_TORQUE_ENABLE])
        # The regression sequence end to end: jog #1 runs from mid-range (the
        # guard passes and leaves the joint at 4094), jog #2 re-asserts torque.
        node, bus = self._node({6: 2048})
        self.assertTrue(node.set_torque(True), node.errors)
        bus.ticks[6] = 4094
        self.assertTrue(node.set_torque(True), node.errors)
        # …while the very same reading is still refused from LIMP, which is the
        # state the measured hazard needs (hand-guide is torque-off by design).
        node, bus = self._node({6: 4094})
        self.assertFalse(node.set_torque(True))
        self.assertEqual(node._torque_refusal[0], 'edge')
        self.assertEqual(bus.writes, [])

    def test_the_transition_gate_re_arms_under_a_latched_bus_fault(self):
        # `_torque_on` is our own BELIEF, never a reading: a 12 V brown-out drops
        # real torque without touching the flag. While the read loop cannot see
        # the arm — the latched fault — that belief is worth least, so the guard
        # re-arms there rather than trusting it.
        node, bus = self._node({4: 2})
        node._torque_on = True
        node._bus_fault = True
        self.assertFalse(node.set_torque(True))
        self.assertEqual(node._torque_refusal[0], 'edge')
        self.assertEqual(bus.writes, [])
        # Clearing the latch (which needs a full 7-servo read) restores the skip.
        node._bus_fault = False
        self.assertTrue(node.set_torque(True), node.errors)

    def test_the_refusal_reason_is_cleared_at_the_top_of_every_set_torque(self):
        # `self._torque_refusal = None` at the top of set_torque is load-bearing,
        # not tidiness: set_torque's bare `except` records NO reason, so a stale
        # one would be reported for an unrelated later failure and send the
        # student to a joint that is no longer the problem.
        node, bus = self._node({6: 4093})
        self.assertFalse(node.set_torque(True))
        self.assertEqual(node._torque_refusal[0], 'edge')
        bus.ticks[6] = 2048                        # student turns it back
        self.assertTrue(node.set_torque(True), node.errors)
        self.assertIsNone(node._torque_refusal)    # not carried past a success

        node, bus = self._node({6: 4093})
        self.assertFalse(node._torque_cb(_Req(True), _Resp()).success)
        self.assertEqual(node._torque_refusal[0], 'edge')
        bus.ticks[6] = 2048

        def _boom(*_a, **_kw):
            raise fb.FeetechBusError('bus went away mid-write')

        bus.sync_write = _boom
        resp = node._torque_cb(_Req(True), _Resp())
        self.assertFalse(resp.success)
        self.assertIsNone(node._torque_refusal)
        self.assertEqual(resp.message, 'Torque-Umschaltung fehlgeschlagen.')
        self.assertNotIn('Positionsbereichs', resp.message)

    def test_guard_covers_EVERY_joint_not_a_full_circle_subset(self):
        # Re-derived 2026-07-26: the guard judges all 7 servos. The joints whose
        # designed window is clear of both register ends (joint1, joint5, gripper)
        # cannot reach a near-edge reading innocently — at the shipped 40-tick
        # margin such a reading is 756+ ticks OUTSIDE the window, i.e. 66°+ past a
        # designed limit, against R9's worst innocent excursion of 107 ticks — so
        # covering them costs nothing and the boot band already refuses them.
        # joint2 (hi = 4095) and joint3 (lo = 0) are the joints this ADDS at the
        # service re-torque path: their windows TOUCH a register end, so a
        # near-edge reading there is IN-window and the band is blind to it.
        margin = _N['EDGE_REFUSE_MARGIN_TICKS']
        for sid in _N['SERVO_IDS']:
            for tick in (0, margin - 1, 4096 - margin, 4095):
                self.assertEqual(
                    [s for s, _ in _N['edge_parked_joints']({sid: tick})],
                    [sid], f'servo {sid} @ tick {tick}')
        # …and the service path refuses on each of them individually.
        for sid in _N['SERVO_IDS']:
            node, bus = self._node({sid: 4094})
            self.assertFalse(node.set_torque(True), f'servo {sid}')
            self.assertIn(f'Gelenk {sid}', node.errors[0])
            self.assertEqual(bus.writes, [])
        # Redundancy proof for the non-touching windows: every near-edge reading
        # there lands outside the designed window by more than the boot band, so
        # nothing the edge guard refuses at BOOT was previously accepted.
        band = _N['BOOT_POSITION_TOLERANCE_TICKS']
        for idx, sid in enumerate(_N['SERVO_IDS']):
            lo, hi = fb.position_limit_window(*_N['JOINT_LIMITS_RAD'][idx], 1)
            for tick in (margin - 1, 4096 - margin):
                if lo <= tick <= hi:
                    # in-window: joint2/joint3/joint4/joint6 — the band CANNOT
                    # see this, which is exactly why the edge guard exists.
                    self.assertIn(sid, (2, 3, 4, 6), f'servo {sid} @ {tick}')
                    continue
                outside = lo - tick if tick < lo else tick - hi
                self.assertGreater(outside, band, f'servo {sid} @ {tick}')

    def test_a_trimmed_window_is_NOT_immunised_by_the_eeprom_clamp(self):
        # The 2026-07-26 correction. The earlier derivation exempted any joint
        # whose window had been trimmed, arguing the servo's EEPROM clamp keeps
        # the seeded goal away from the boundary. The clamp bounds the GOAL, not
        # the PATH — so the wrapped error still forms, and the guard must NOT drop
        # a trimmed joint. Arithmetic pinned here so the false claim cannot come
        # back as a "simplification".
        trimmed = (-2.9671, 2.9671)                          # ±170°
        lo, hi = fb.position_limit_window(*trimmed, 1)
        self.assertEqual((lo, hi), (114, 3982))
        parked, crossed_to = 4095, 2                 # dither across the seam
        clamped_goal = min(max(parked, lo), hi)
        self.assertEqual(clamped_goal, 3982)
        naive_error = clamped_goal - crossed_to
        self.assertEqual(naive_error, 3980)          # ticks
        self.assertGreater(naive_error * 360.0 / 4096, 349.0)   # ≈350°
        # The boot band would not catch the park either: 4095 is only 113 ticks
        # outside a ±170° window, well inside BOOT_POSITION_TOLERANCE_TICKS.
        self.assertLess(parked - hi, _N['BOOT_POSITION_TOLERANCE_TICKS'])
        # So: still refused, with the limits trimmed or not.
        with patch.dict(_N, {'JOINT_LIMITS_RAD': tuple(
                trimmed if i in (3, 5) else lim
                for i, lim in enumerate(_N['JOINT_LIMITS_RAD']))}):
            for ticks in ({4: 0}, {6: 4095}):
                node, bus = self._node(ticks)
                self.assertFalse(node.set_torque(True), ticks)
                self.assertEqual(bus.writes, [])

    # ── torque-off ─────────────────────────────────────────────────────────
    def test_torque_off_is_never_blocked(self):
        for tick in (0, 4095):
            node, bus = self._node({6: tick})
            resp = node._torque_cb(_Req(False), _Resp())
            self.assertTrue(resp.success, f'tick {tick}')
            self.assertEqual(resp.message, 'ok')
            self.assertFalse(node._torque_on)
            # torque-off writes ONLY Torque_Enable=0 — no goal seed on the way
            # out, so the guard's read never even runs.
            self.assertEqual(bus.addrs(), [fb.REG_TORQUE_ENABLE])
            self.assertEqual(bus.torque_writes(),
                             [(sid, 0) for sid in range(1, 8)])

    # ── fail-safe on a read failure (must not skip the guard) ───────────────
    def test_read_failure_still_refuses_and_is_not_an_edge_verdict(self):
        node, bus = self._node(missing=(3,))
        self.assertFalse(node.set_torque(True))
        self.assertEqual(node._torque_refusal[0], 'read')
        self.assertIn('[FEHLER]', node.errors[0])
        self.assertIn('12-V', node.errors[0])
        self.assertEqual(bus.writes, [])

    # ── the boot path ──────────────────────────────────────────────────────
    def test_boot_path_refuses_without_the_cable_misdiagnosis(self):
        node, bus = self._node({4: 4094})
        clock = _FakeTime()
        with patch.dict(_N, {'time': clock}):
            node.start_boot_home()
        # No retry burn: an edge park is a physical state, 1 s cannot fix it.
        self.assertEqual(clock.slept, [])
        self.assertEqual(len(node.errors), 2)
        self.assertIn('Gelenk 4', node.errors[0])
        self.assertIn('Umgebung neu starten', node.errors[1])
        for msg in node.errors:
            self.assertNotIn('12-V', msg)         # NOT the cable diagnosis
            self.assertNotIn('Kabel', msg)
        # …and the glide never started.
        self.assertEqual(node.traj_calls, [])
        self.assertEqual(node.verify_calls, [])
        self.assertEqual(bus.writes, [])
        self.assertFalse(node._torque_on)

    # ── the boot-home table-floor pre-check (Rule §2, 2026-07-27) ──────────
    def test_boot_path_refuses_to_glide_into_the_table_but_keeps_torque_ON(self):
        """The measured worst press: from this pose the straight glide to HOME
        drives a link 176 mm below the table on the collision meshes.

        The gate must skip the glide — and it must LEAVE THE ARM TORQUED. A
        refusal that de-energises a BACKDRIVING arm turns a guard into a
        collapse, which is strictly worse than the defect it guards. That is the
        single most dangerous way to get this feature wrong, so it is asserted
        here on the real node path, not only in the AST structure tests.
        """
        node, bus = self._node()
        # the stub's default read_positions IS home; make it the measured press
        node.read_positions = [-0.131, 2.997, -0.020, 1.964, -0.391, 1.898, 1.75]
        clock = _FakeTime()
        with patch.dict(_N, {'time': clock, 'threading': _SyncThreading}):
            node.start_boot_home()
        # the glide never started and the verifier was never armed
        self.assertEqual(node.traj_calls, [])
        self.assertEqual(node.verify_calls, [])
        # …but the arm is HELD, not dropped
        self.assertTrue(node._torque_on)
        self.assertNotIn((1, 0), bus.torque_writes())
        # …and the student is told why, in German, with a remedy
        self.assertTrue(node.errors)
        self.assertIn('Tischplatte', node.errors[0])
        self.assertIn('NICHT weich', node.errors[0])
        self.assertIn('Handführung', node.errors[0])

    def test_the_floor_precheck_can_be_switched_off_with_one_variable(self):
        """EDUBOTICS_HOME_FLOOR_TOL_M=0 must restore the old behaviour exactly,
        even for the worst measured press."""
        node, bus = self._node()
        node.read_positions = [-0.131, 2.997, -0.020, 1.964, -0.391, 1.898, 1.75]
        clock = _FakeTime()
        with patch.dict(_N, {'time': clock, 'threading': _SyncThreading,
                             'BOOT_HOME_FLOOR_TOL_M': 0.0}):
            node.start_boot_home()
        self.assertEqual(len(node.traj_calls), 1)
        self.assertEqual(node.verify_calls, [1])
        self.assertEqual(node.errors, [])

    def test_boot_path_still_glides_on_a_healthy_arm(self):
        node, bus = self._node()
        clock = _FakeTime()
        with patch.dict(_N, {'time': clock, 'threading': _SyncThreading}):
            node.start_boot_home()
        self.assertTrue(node._torque_on)
        self.assertEqual(node.errors, [])
        self.assertEqual(len(node.traj_calls), 1)
        self.assertGreater(len(node.traj_calls[0][0]), 100)   # the quintic glide
        self.assertEqual(node.verify_calls, [1])              # verifier armed
        self.assertIn(fb.REG_TORQUE_ENABLE, bus.addrs())

    def test_boot_path_still_retries_a_TRANSIENT_read_failure(self):
        # The retry rail is for a bus hiccup; the edge branch must not have
        # short-circuited it.
        node, bus = self._node(missing=(2,))
        clock = _FakeTime()
        with patch.dict(_N, {'time': clock}):
            node.start_boot_home()
        self.assertEqual(len(clock.slept), _N['BOOT_TORQUE_ON_ATTEMPTS'] - 1)
        self.assertEqual(clock.slept[0], _N['BOOT_TORQUE_ON_RETRY_S'])
        self.assertIn('12-V', node.errors[-1])
        self.assertEqual(node.traj_calls, [])

    # ── probe_bus half + env knob ──────────────────────────────────────────
    def test_probe_bus_refuses_at_boot_and_says_it_self_heals(self):
        # probe_bus runs BEFORE any ROS entity exists and main() retries it every
        # 5 s, so this is the copy that self-heals — and the copy that keeps the
        # refusal from surfacing in start_boot_home, which runs exactly once.
        probe = TestProbeBusGate._probe()
        # joint4/joint6: full-circle windows — the plausibility band is a
        # documented no-op there. joint2 (hi = 4095) at its top end and joint3
        # (lo = 0) at its bottom end: IN-window readings the band also cannot see.
        # All four are the readings only this guard judges.
        for sid, tick in ((4, 4095), (6, 3), (2, 4094), (3, 1)):
            reg = TestProbeBusGate._RegBus(present={sid: tick})
            lo, hi = reg.windows[sid]
            self.assertTrue(lo <= tick <= hi,
                            f'servo {sid} @ {tick} must be IN-window for this '
                            f'test to prove the band cannot see it')
            ok, msg = probe(reg)
            self.assertFalse(ok, f'servo {sid} @ {tick}')
            self.assertIn(f'Gelenk {sid}', msg)
            self.assertIn('Positionsbereichs', msg)     # the edge wording…
            self.assertIn('von selbst weiter', msg)     # …+ the 5 s retry promise
        # …and the same joints mid-range still boot.
        ok, msg = probe(TestProbeBusGate._RegBus(
            present={2: 2504, 3: 1548, 4: 2048, 6: 2048}))
        self.assertTrue(ok, msg)

    def test_edge_margin_env_parse(self):
        f = _N['_parse_edge_margin']
        default = _N['_DEFAULT_EDGE_REFUSE_MARGIN_TICKS']
        self.assertEqual(default, 40)                 # ≈3.5°, see the comment
        self.assertEqual(_N['EDGE_REFUSE_MARGIN_TICKS'], default)
        self.assertEqual(f(None), default)
        self.assertEqual(f(''), default)
        self.assertEqual(f('   '), default)
        self.assertEqual(f(' 12 '), 12)
        # Malformed / negative → the default, and it SAYS so (bench-facing).
        for bad in ('abc', '-5', '1,2'):
            with patch('builtins.print') as printed:
                self.assertEqual(f(bad), default, bad)
            self.assertTrue(printed.called, bad)
            self.assertIn('EDUBOTICS_EDU6_EDGE_MARGIN_TICKS',
                          printed.call_args[0][0])

    def test_a_too_large_margin_cannot_brick_the_arm(self):
        # INVERTED POLARITY TRAP. The sibling knob
        # EDUBOTICS_EDU6_BOOT_POS_TOL_TICKS documents ">= 4096 disables" (large =
        # permissive); this one is a refusal RADIUS, so large = maximally
        # restrictive. `min(tick, 4095 − tick)` peaks at 2047, so any margin >=
        # 2048 refuses all 4096 readings — an operator typing 4096 to "disable"
        # the guard would leave an arm that can never switch its torque on.
        f = _N['_parse_edge_margin']
        default = _N['_DEFAULT_EDGE_REFUSE_MARGIN_TICKS']
        cap = _N['_EDGE_MARGIN_MAX_TICKS']
        self.assertEqual(cap, 4096 // 2)
        # The arithmetic the cap is derived from, pinned rather than asserted in
        # prose: at the cap NOTHING passes, one tick below it something does.
        self.assertEqual(
            [t for t in range(4096) if min(t, 4095 - t) < cap], list(range(4096)))
        self.assertNotEqual(
            [t for t in range(4096) if min(t, 4095 - t) < cap - 1],
            list(range(4096)))
        for bad in ('2048', '4000', '4096', '999999'):
            with patch('builtins.print') as printed:
                self.assertEqual(f(bad), default, bad)
            self.assertTrue(printed.called, bad)
            said = printed.call_args[0][0]
            self.assertIn('EDUBOTICS_EDU6_EDGE_MARGIN_TICKS', said)
            self.assertIn('0', said)        # names the real disable value
        # The largest value that still lets a pose through is accepted verbatim.
        self.assertEqual(f(str(cap - 1)), cap - 1)
        self.assertEqual(
            [s for s, _ in _N['edge_parked_joints']({4: 2047}, margin=cap - 1)],
            [])

    def test_negative_or_over_range_ticks_refuse_rather_than_fold(self):
        # `min(tick, 4095 − tick)` on a RAW reading, deliberately: a negative or
        # over-range tick cannot be placed on the map at all (uncleared multi-turn
        # firmware, a garbled reply), and refusing is the safe direction. Taking
        # abs() first — or clamping into the register — would ACCEPT such a
        # reading whenever its magnitude lands mid-range, which is precisely the
        # unplaceable case. −5 does not discriminate (abs(−5) = 5 still trips);
        # −100 does.
        for tick in (-100, -1000, -4055, 4200, 9999):
            self.assertEqual(
                [s for s, _ in _N['edge_parked_joints']({6: tick})], [6],
                f'tick {tick}')
        node, bus = self._node({6: -100})
        self.assertFalse(node.set_torque(True))
        self.assertEqual(node._torque_refusal[0], 'edge')
        self.assertEqual(bus.writes, [])

    def test_zero_margin_is_the_documented_rollback(self):
        self.assertEqual(_N['_parse_edge_margin']('0'), 0)
        self.assertEqual(_N['edge_parked_joints']({6: 0, 4: 4095}, margin=0), [])
        # "Disables the guard ENTIRELY" has to include the pathological reading:
        # a negative / over-range tick (uncleared multi-turn firmware, which
        # probe_bus refuses outright) is nearer than 0 to the edge and would
        # still trip the distance test, so the explicit early-out is
        # load-bearing rather than decorative.
        self.assertEqual(_N['edge_parked_joints']({6: -5}, margin=0), [])
        self.assertEqual(
            [sid for sid, _ in _N['edge_parked_joints']({6: -5}, margin=40)],
            [6])
        with patch.dict(_N, {'EDGE_REFUSE_MARGIN_TICKS': 0}):
            node, bus = self._node({6: 4095})
            self.assertTrue(node.set_torque(True), node.errors)
            self.assertIn(fb.REG_TORQUE_ENABLE, bus.addrs())

    def test_env_override_is_wired_to_the_constant(self):
        # The parser is covered above; this pins that the SHIPPED constant is
        # actually BUILT from the env var. A hardcoded default would leave the
        # documented one-variable rollback dead while every parser test stayed
        # green (the harness deliberately pops the var, so it cannot see this).
        src = open(os.path.join(_DOCKER, 'edu6_arm_node.py'),
                   encoding='utf-8').read()
        value = None
        for node in ast.parse(src).body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name)
                    and t.id == 'EDGE_REFUSE_MARGIN_TICKS'
                    for t in node.targets):
                value = ast.unparse(node.value)
        self.assertIsNotNone(value)
        self.assertIn('_parse_edge_margin(', value)
        self.assertIn('EDUBOTICS_EDU6_EDGE_MARGIN_TICKS', value)

    def test_env_name_is_forwarded_by_compose(self):
        # ci.yml::env-forwarding-guard fails the build otherwise — and without
        # the forward the operator's rollback silently does nothing.
        compose = open(os.path.join(_HERE, '..', 'docker',
                                    'docker-compose.yml'),
                       encoding='utf-8').read()
        self.assertIn('EDUBOTICS_EDU6_EDGE_MARGIN_TICKS=', compose)


class TestPostEnergiseWrongWayAbort(unittest.TestCase):
    """The measured defect (docs §11.11) is that the STS position loop computes
    `goal − present` NAIVELY over 0..4095, so a goal and a position straddling the
    seam make it drive a whole revolution the wrong way. The pre-torque edge guard
    (TestTorqueOnEdgeGuard above) NARROWS that race but cannot close it: the wrap
    only forms if `present` crosses the boundary between the seed read and the
    Torque_Enable write, and while pure wire time for that window is 1.570 ms
    (157 bytes at 1 Mbit/s 8N1 — needing 39.1 rad/s to cover the 40-tick margin), the
    real window also carries Return_Delay_Time, pyserial's byte-at-a-time header
    hunt, 1 ms CDC-ACM frames through usbipd/vhci_hcd and GIL/container
    scheduling — all unbounded. At 20 ms the same margin only needs 3.1 rad/s.

    So torque-on ALSO re-reads position once immediately AFTER energising and drops
    torque again if the servo is going the long way. That caps a runaway at a few
    ticks regardless of the timing window and believes no flag — which is why,
    unlike the edge guard, it is NOT gated on the OFF→ON transition."""

    def _node(self, ticks=None, missing=(), post_ticks=None, missing_at=None):
        bus = _TorqueFakeBus(ticks, missing, post_ticks, missing_at)
        return _TorqueStubNode(bus), bus

    # ── the arithmetic the threshold rests on ──────────────────────────────
    def test_threshold_is_half_a_revolution_and_that_is_definitional(self):
        # NOT a felt number: `|e| > TICKS_PER_REV/2` is algebraically identical to
        # `TICKS_PER_REV − |e| < |e|`, i.e. "the naive path the servo is taking is
        # strictly longer than the true short way round". Pinned over the WHOLE
        # register so the equivalence cannot be broken by an off-by-one.
        self.assertEqual(_N['_DEFAULT_TORQUE_ABORT_ERROR_TICKS'], 4096 // 2)
        self.assertEqual(_N['TORQUE_ABORT_ERROR_TICKS'], 2048)
        mismatch = [e for e in range(4096)
                    if (e > 2048) != ((4096 - e) < e)]
        self.assertEqual(mismatch, [])

    def test_legitimate_clamp_pull_in_can_never_reach_the_threshold(self):
        # The ONE legitimate non-zero error: the servo clamps Goal_Position into
        # its EEPROM window, so a joint resting OUTSIDE that window is genuinely
        # pulled to the window edge. Enumerated over every reading the edge guard
        # permits, the worst such distance is 2008 ticks; with the edge guard
        # DISABLED (margin 0 — the documented rollback) the supremum is exactly
        # 2048, which the STRICT `>` still does not abort. That is what makes the
        # threshold structurally free of false aborts rather than merely
        # well-separated.
        margin = _N['EDGE_REFUSE_MARGIN_TICKS']
        threshold = _N['TORQUE_ABORT_ERROR_TICKS']
        worst_guarded = worst_unguarded = 0
        for idx in range(len(_N['SERVO_IDS'])):
            lo, hi = fb.position_limit_window(*_N['JOINT_LIMITS_RAD'][idx], 1)
            for present in range(4096):
                pull = abs(max(lo, min(hi, present)) - present)
                worst_unguarded = max(worst_unguarded, pull)
                if min(present, 4095 - present) >= margin:
                    worst_guarded = max(worst_guarded, pull)
        self.assertEqual(worst_guarded, 2008)
        self.assertEqual(worst_unguarded, 2048)
        self.assertLess(worst_guarded, threshold)
        self.assertLessEqual(worst_unguarded, threshold)   # strict `>` saves it
        # …and the boot band bounds it far tighter than either on the boot path.
        self.assertLess(_N['BOOT_POSITION_TOLERANCE_TICKS'], threshold)

    def test_a_wrapped_error_is_far_above_the_threshold(self):
        # |e| = 4095 − (ticks travelled inside the read→write window). Even an
        # absurd hand — 20 rad/s held for 50 ms, i.e. 30x the speed the 40-tick
        # margin covers at that duration — leaves 1395 ticks of separation.
        rad_per_tick = 2 * 3.14159265358979 / 4096
        travelled = 20.0 / rad_per_tick * 0.050
        self.assertGreater(4095 - travelled, _N['TORQUE_ABORT_ERROR_TICKS'])
        self.assertGreater(4095 - travelled, 3400)

    def test_wrong_way_joints_pure_predicate(self):
        f = _N['wrong_way_joints']
        # the measured bench datum: joint6 seeded 4090, position dithered to 12
        out = f({6: 4090}, {6: 12})
        self.assertEqual([(sid, g, p) for sid, g, p, _e in out],
                         [(6, 4090, 12)])
        self.assertEqual(out[0][3], 4078)          # the naive error, signed
        # …and the mirror image (seeded low, position dithered high).
        self.assertEqual([s for s, *_ in f({6: 5}, {6: 4090})], [6])
        # healthy: goal == present
        self.assertEqual(f({s: 2048 for s in range(1, 8)},
                           {s: 2048 for s in range(1, 8)}), [])
        # a legitimate 400-tick clamp pull-in is NOT wrong-way
        self.assertEqual(f({1: 1024}, {1: 624}), [])
        # exactly at the threshold: still allowed (strict `>`)
        self.assertEqual(f({1: 2048}, {1: 0}), [])
        self.assertEqual([s for s, *_ in f({1: 2049}, {1: 0})], [1])
        # servos absent from EITHER map are not judged
        self.assertEqual(f({6: 4090}, {}), [])
        self.assertEqual(f({}, {6: 12}), [])

    def test_the_wrong_way_half_is_load_bearing_below_the_default_threshold(self):
        # At the SHIPPED threshold `naive > 2048` and `wrapped < naive` are the
        # same test, so dropping the second condition changes nothing there — a
        # "simplification" would look free. It is not: the env knob explicitly
        # permits lowering the threshold to the 401-tick floor for bench
        # sensitivity, and below 2048 a plain magnitude test starts flagging
        # ordinary EEPROM clamp pull-ins, which are by construction already the
        # SHORT way round. Keeping both conditions means the knob can only ever
        # add sensitivity to genuine wrong-way motion, never invent it.
        f = _N['wrong_way_joints']
        # a legitimate 700-tick pull-in, from either side — never wrong-way
        self.assertEqual(f({1: 1024}, {1: 324}, threshold=600), [])
        self.assertEqual(f({1: 1024}, {1: 1724}, threshold=600), [])
        # …while a genuine wrap is still caught at that lowered threshold
        self.assertEqual([s for s, *_ in f({6: 4090}, {6: 12}, threshold=600)],
                         [6])
        # the exact hinge: half a revolution is a TIE (either way is 2048 ticks),
        # so it is not "the wrong way"; one tick further it is.
        self.assertEqual(f({1: 2048}, {1: 0}, threshold=600), [])
        self.assertEqual([s for s, *_ in f({1: 2049}, {1: 0}, threshold=600)],
                         [1])

    def test_unplaceable_ticks_abort_rather_than_fold(self):
        # A negative / over-range present (uncleared multi-turn firmware, a
        # garbled reply) makes `wrapped` negative, so it aborts — the same stance
        # edge_parked_joints takes. Folding or abs()-ing first would ACCEPT it
        # whenever the magnitude happened to land mid-range.
        f = _N['wrong_way_joints']
        for present in (-2500, 6000):
            self.assertEqual([s for s, *_ in f({6: 2048}, {6: present})], [6],
                             f'present {present}')

    # ── the abort itself ───────────────────────────────────────────────────
    def test_a_wrapped_error_after_energising_de_energises_the_arm(self):
        # Tick 4055 is the INNERMOST high reading the pre-torque edge guard
        # permits (4095 − 4055 = 40 = the margin), so this scenario is exactly the
        # one the edge guard cannot see: it passes the park, and the joint then
        # crosses the seam to tick 12 inside the read→write window. That is the
        # measured runaway signature, and only the post-energise check catches it.
        node, bus = self._node({6: 4055}, post_ticks={6: 12})
        self.assertFalse(node.set_torque(True))
        # Torque_Enable really was written 1 and then 0 — the abort reached the
        # BUS, it is not just a return value.
        self.assertEqual(bus.addrs(), [fb.REG_ACCELERATION,
                                       fb.REG_TORQUE_ENABLE,
                                       fb.REG_TORQUE_ENABLE])
        self.assertEqual(bus.torque_writes(),
                         [(sid, 1) for sid in range(1, 8)]
                         + [(sid, 0) for sid in range(1, 8)])
        # …and our belief followed the hardware, so the write loop is gated off
        # and _trajectory_cb will refuse new commands.
        self.assertFalse(node._torque_on)
        self.assertEqual(node._torque_refusal[0], 'wrongway')
        # the in-flight trajectory is dropped (audit M2: a later torque-on must
        # not interpolate it from wherever the limp arm sagged to)
        self.assertEqual(node.traj_calls, [([], None)])

    def test_the_abort_message_names_the_joint_in_german(self):
        node, _ = self._node({4: 40}, post_ticks={4: 4090})
        resp = node._torque_cb(_Req(True), _Resp())
        self.assertFalse(resp.success)
        # The SetBool reply carries the REASON — this is the „Beenden"
        # (hand-guide exit) path, where a bare 'fehlgeschlagen' sends the student
        # to the cables.
        self.assertIn('[FEHLER]', resp.message)
        self.assertIn('Gelenk 4', resp.message)
        self.assertIn('Ziel 40', resp.message)          # the evidence
        self.assertIn('Ist 4090', resp.message)
        self.assertIn('falsche Richtung', resp.message)
        self.assertIn('ausgeschaltet', resp.message)    # says what it DID
        self.assertIn('weich', resp.message)            # …and the consequence
        self.assertIn('Richtung Mitte', resp.message)   # …and the remedy
        self.assertIn('genügen', resp.message)          # literal umlauts
        self.assertNotIn('Kabel', resp.message)         # NOT a cable diagnosis
        # it is in the Protokoll too, exactly once
        self.assertEqual(node.errors, [resp.message])

    def test_abort_message_grammar_and_the_failed_torque_off_branch(self):
        f = _N['wrong_way_abort_message']
        one = f([(6, 4090, 12, 4078)], True)
        self.assertIn('wäre in die falsche Richtung', one)
        many = f([(4, 1, 4090, -4089), (6, 4090, 12, 4078)], True)
        self.assertIn('wären in die falsche Richtung', many)
        self.assertIn('Gelenk 4', many)
        self.assertIn('Gelenk 6', many)
        # If the torque-OFF write itself failed there is nothing software can do
        # — say so, and name the only remaining action.
        broken = f([(6, 4090, 12, 4078)], False)
        self.assertIn('FEHLGESCHLAGEN', broken)
        self.assertIn('12-V-Netzteil', broken)
        self.assertIn('ausschalten', broken)

    def test_a_failed_torque_off_write_still_reports_instead_of_raising(self):
        # The abort must not turn a broken bus into an unhandled exception that
        # loses the diagnosis (set_torque's bare `except` records no reason).
        node, bus = self._node({6: 4055}, post_ticks={6: 12})
        real = bus.sync_write
        calls = {'n': 0}

        def _flaky(addr, per_servo):
            calls['n'] += 1
            if calls['n'] == 3:            # the torque-OFF write of the abort
                raise fb.FeetechBusError('bus went away during the abort')
            real(addr, per_servo)

        bus.sync_write = _flaky
        self.assertFalse(node.set_torque(True))
        self.assertEqual(node._torque_refusal[0], 'wrongway')
        self.assertIn('FEHLGESCHLAGEN', node.errors[0])
        self.assertFalse(node._torque_on)

    # ── no nuisance: the legitimate populations must not abort ─────────────
    def test_a_healthy_arm_is_not_aborted_and_pays_exactly_one_extra_read(self):
        node, bus = self._node()                    # every joint at tick 2048
        self.assertTrue(node.set_torque(True), node.errors)
        self.assertTrue(node._torque_on)
        self.assertIsNone(node._torque_refusal)
        self.assertEqual(node.errors, [])
        self.assertEqual(node.traj_calls, [])
        # ONE seed read + ONE verification read; the retry budget is not spent on
        # a healthy arm (~0.71 ms of wire time, the stated budget).
        self.assertEqual(bus.reads, 2)
        self.assertEqual(bus.addrs(), [fb.REG_ACCELERATION,
                                       fb.REG_TORQUE_ENABLE])

    def test_the_legitimate_eeprom_clamp_pull_in_does_NOT_abort(self):
        # A limp joint resting OUTSIDE its EEPROM window is genuinely pulled to
        # the window edge at torque-on (the documented gentle SEED_SPEED_STEPS
        # creep). joint1's window is [1024, 3072]; resting `band − 1` ticks below
        # it is exactly the state the BOOT probe deliberately ACCEPTS, so the
        # abort must accept it too or „Umgebung starten" would refuse a healthy
        # arm every time it collapsed a little past a stop.
        band = _N['BOOT_POSITION_TOLERANCE_TICKS']
        lo, _hi = fb.position_limit_window(*_N['JOINT_LIMITS_RAD'][0], 1)
        parked = lo - (band - 1)
        node, bus = self._node({1: parked})
        self.assertTrue(node.set_torque(True), node.errors)
        self.assertIsNone(node._torque_refusal)
        # …and the pull-in it will perform really is the clamp distance, i.e. the
        # naive error the abort just chose not to fire on.
        self.assertEqual(band - 1, lo - parked)
        # Even the WORST software-reachable pull-in (joint2 / the gripper at the
        # innermost tick the edge guard permits) stays under the threshold.
        margin = _N['EDGE_REFUSE_MARGIN_TICKS']
        for sid in (2, 7):
            node, _ = self._node({sid: margin})
            self.assertTrue(node.set_torque(True), f'servo {sid}')
            self.assertIsNone(node._torque_refusal)

    def test_an_already_torqued_joint_parked_on_the_seam_still_torques_on(self):
        # The jogging regression the edge guard's OFF→ON gate exists for must not
        # come back through this check. It does not, and NOT because of a gate:
        # this one judges MEASURED motion, and a torqued joint holding at tick
        # 4094 reports goal − present ≈ 0. Both jogs pass.
        node, bus = self._node({6: 4094})
        node._torque_on = True
        self.assertTrue(node.set_torque(True), node.errors)
        self.assertTrue(node._torque_on)
        self.assertEqual(bus.torque_writes(), [(sid, 1) for sid in range(1, 8)])

    def test_the_check_is_NOT_gated_on_the_transition_flag(self):
        # …which is the whole point: `_torque_on` is a BELIEF. A brown-out shorter
        # than READ_FAIL_STOP_S drops real torque without touching the flag and
        # without latching _bus_fault, so the edge guard's gate skips over an arm
        # that is really limp. The post-energise abort still fires there.
        node, bus = self._node({6: 4094}, post_ticks={6: 5})
        node._torque_on = True
        node._bus_fault = False           # no latch: the edge guard is skipped…
        self.assertFalse(node.set_torque(True))
        self.assertEqual(node._torque_refusal[0], 'wrongway')   # …this is not
        self.assertFalse(node._torque_on)
        self.assertIn((fb.REG_TORQUE_ENABLE, {s: b'\x00' for s in range(1, 8)}),
                      bus.writes)

    def test_torque_off_never_runs_the_check(self):
        for tick in (0, 4095):
            node, bus = self._node({6: tick}, post_ticks={6: 2048})
            resp = node._torque_cb(_Req(False), _Resp())
            self.assertTrue(resp.success, f'tick {tick}')
            self.assertEqual(resp.message, 'ok')
            self.assertFalse(node._torque_on)
            self.assertEqual(bus.reads, 0)        # no seed read, no verify read
            self.assertEqual(bus.addrs(), [fb.REG_TORQUE_ENABLE])

    # ── the post-read's own failure modes ──────────────────────────────────
    def test_one_dropped_reply_is_retried_and_does_not_de_energise(self):
        # A dropped CDC-ACM reply is common on WSL2 (it is why READ_FAIL_STOP_S
        # exists). De-energising on one would collapse a BACKDRIVING arm, so the
        # verification read gets exactly one retry.
        node, bus = self._node(missing_at={2: {5}})
        self.assertTrue(node.set_torque(True), node.errors)
        self.assertTrue(node._torque_on)
        self.assertIsNone(node._torque_refusal)
        self.assertEqual(bus.reads, 3)            # seed + verify + one retry
        self.assertEqual(_N['POST_ENERGISE_READ_ATTEMPTS'], 2)

    def test_a_persistently_silent_servo_de_energises_and_stays_retryable(self):
        # An arm we cannot read is an arm we must not drive — the same stance the
        # goal-seed read already takes. But this is a BUS problem, not a physical
        # park, so the kind stays 'read': the boot path must keep retrying it.
        node, bus = self._node(missing_at={2: {5}, 3: {5}})
        self.assertFalse(node.set_torque(True))
        self.assertEqual(node._torque_refusal[0], 'read')
        self.assertFalse(node._torque_on)
        self.assertEqual(bus.torque_writes(),
                         [(sid, 1) for sid in range(1, 8)]
                         + [(sid, 0) for sid in range(1, 8)])
        msg = node.errors[0]
        self.assertIn('[FEHLER]', msg)
        self.assertIn('[5]', msg)
        self.assertIn('12-V', msg)
        self.assertIn('prüfen', msg)

    def test_a_raising_verification_read_de_energises_rather_than_escaping(self):
        # If the sync_read itself raises, falling through to set_torque's bare
        # `except` would leave the arm ENERGISED with no diagnosis. The abort
        # swallows the raise and treats it as an unreadable arm.
        node, bus = self._node()
        real = bus.sync_read

        def _boom(addr, length, ids):
            bus.reads += 1
            if bus.reads >= 2:
                raise fb.FeetechBusError('bus went away after energising')
            return real(addr, length, ids)

        bus.sync_read = _boom
        self.assertFalse(node.set_torque(True))
        self.assertEqual(node._torque_refusal[0], 'read')
        self.assertFalse(node._torque_on)
        self.assertEqual(bus.torque_writes()[-7:],
                         [(sid, 0) for sid in range(1, 8)])

    def test_a_malformed_reply_de_energises_rather_than_escaping(self):
        # Same class one layer in: the DECODE must be inside the same guard as the
        # read. A short/garbled reply that survives sync_read's own length filter
        # would raise IndexError here, escape to set_torque's bare `except`, and
        # leave the arm ENERGISED with no diagnosis — the one outcome this method
        # exists to prevent.
        node, bus = self._node()

        def _garbled(addr, length, ids):
            bus.reads += 1
            if bus.reads == 1:
                return {sid: (0, fb.le16(2048)) for sid in ids}   # the seed read
            return {sid: (0, b'') for sid in ids}                 # 0-byte params
        bus.sync_read = _garbled
        self.assertFalse(node.set_torque(True))
        self.assertEqual(node._torque_refusal[0], 'read')
        self.assertFalse(node._torque_on)
        self.assertEqual(bus.torque_writes()[-7:],
                         [(sid, 0) for sid in range(1, 8)])

    def test_a_wrong_way_verdict_wins_over_a_partial_read(self):
        # Both verdicts need the same torque-off; the actionable one must be the
        # one reported.
        node, _ = self._node({6: 4055}, post_ticks={6: 12},
                             missing_at={2: {3}, 3: {3}})
        self.assertFalse(node.set_torque(True))
        self.assertEqual(node._torque_refusal[0], 'wrongway')
        self.assertIn('Gelenk 6', node.errors[0])

    # ── the boot path ──────────────────────────────────────────────────────
    def test_boot_path_reports_the_abort_without_burning_retries(self):
        node, bus = self._node({4: 4055}, post_ticks={4: 20})
        clock = _FakeTime()
        with patch.dict(_N, {'time': clock}):
            node.start_boot_home()
        # A wrong-way abort is a PHYSICAL state: 1 s of sleeping cannot fix it.
        self.assertEqual(clock.slept, [])
        self.assertEqual(len(node.errors), 2)
        self.assertIn('Gelenk 4', node.errors[0])
        self.assertIn('Grundstellungs-Fahrt entfällt', node.errors[1])
        self.assertIn('falsche Richtung', node.errors[1])
        self.assertIn('Umgebung neu starten', node.errors[1])
        for msg in node.errors:
            self.assertNotIn('12-V-Versorgung', msg)   # NOT a cable diagnosis
            self.assertNotIn('Kabel', msg)
        # …the glide never started, and the arm was left de-energised.
        self.assertEqual(node.verify_calls, [])
        self.assertFalse(node._torque_on)
        self.assertEqual(bus.torque_writes()[-7:],
                         [(sid, 0) for sid in range(1, 8)])

    def test_the_physical_refusal_kinds_are_declared_in_one_place(self):
        # 'edge' and 'wrongway' must both be routed to a boot remedy that omits
        # the cable wording and skips the retry rail. One constant says which
        # kinds those are, so the boot branch and the messages cannot drift.
        self.assertEqual(set(_N['PHYSICAL_TORQUE_REFUSALS']),
                         {'edge', 'wrongway'})
        self.assertEqual(set(_N['BOOT_PHYSICAL_REMEDY_DE']),
                         set(_N['PHYSICAL_TORQUE_REFUSALS']))
        for kind, msg in _N['BOOT_PHYSICAL_REMEDY_DE'].items():
            self.assertIn('[FEHLER]', msg, kind)
            self.assertIn('Umgebung neu starten', msg, kind)
            self.assertNotIn('Kabel', msg, kind)
            self.assertNotIn('12-V', msg, kind)
        src = open(os.path.join(_DOCKER, 'edu6_arm_node.py'),
                   encoding='utf-8').read()
        sb = src.split('def start_boot_home', 1)[1].split('\n    def ', 1)[0]
        self.assertIn('in PHYSICAL_TORQUE_REFUSALS', sb)
        self.assertIn('BOOT_PHYSICAL_REMEDY_DE[kind]', sb)
        # the physical branch must come BEFORE the sleep/retry, or it burns them
        self.assertLess(sb.index('PHYSICAL_TORQUE_REFUSALS'),
                        sb.index('BOOT_TORQUE_ON_RETRY_S'))

    def test_boot_path_still_glides_when_the_verification_passes(self):
        node, bus = self._node()
        clock = _FakeTime()
        with patch.dict(_N, {'time': clock, 'threading': _SyncThreading}):
            node.start_boot_home()
        self.assertTrue(node._torque_on)
        self.assertEqual(node.errors, [])
        self.assertEqual(node.verify_calls, [1])
        self.assertEqual(bus.torque_writes(), [(sid, 1) for sid in range(1, 8)])

    # ── ordering + wiring ──────────────────────────────────────────────────
    def test_the_check_runs_AFTER_torque_enable_and_de_energises_first(self):
        src = open(os.path.join(_DOCKER, 'edu6_arm_node.py'),
                   encoding='utf-8').read()
        st = src.split('def set_torque', 1)[1].split('\n    def ', 1)[0]
        # seed → Torque_Enable → verify. A verify BEFORE the write would measure
        # nothing (the servo is not energised yet).
        self.assertLess(st.index('_seed_goal_from_present_locked'),
                        st.index('REG_TORQUE_ENABLE'))
        self.assertLess(st.index('REG_TORQUE_ENABLE'),
                        st.index('_verify_after_energise_locked'))
        # …and it runs INSIDE the bus lock, so no read/write tick can interleave
        # between energising and the verdict.
        self.assertLess(st.index('with self._bus_lock'),
                        st.index('_verify_after_energise_locked'))
        vb = src.split('def _verify_after_energise_locked', 1)[1].split(
            '\n    def ', 1)[0]
        # The de-energise write must precede EVERYTHING else: no logging, no
        # message building, no allocation (the payload is a module constant).
        off = vb.index('_TORQUE_OFF_PAYLOAD')
        self.assertLess(off, vb.index('wrong_way_abort_message'))
        self.assertLess(off, vb.index('post_energise_read_failure_message'))
        self.assertLess(off, vb.index('self._torque_on = False'))
        self.assertNotIn('get_logger', vb)
        self.assertNotIn('_replace_trajectory', vb)   # no _traj_lock under the
        #                                               bus lock (lock order)
        # …and the trajectory drop happens OUTSIDE the bus lock, in set_torque.
        self.assertIn('_replace_trajectory([])', st)
        self.assertLess(st.index('_verify_after_energise_locked'),
                        st.index('_replace_trajectory([])'))

    def test_the_effective_goal_is_the_eeprom_clamped_one(self):
        # The servo clamps Goal_Position into its EEPROM window, so the number its
        # naive error is computed from is NOT the tick we wrote. Computing the
        # clamp is safe only because probe_bus hard-gates the EEPROM windows
        # against fb.position_limit_window — the same call used here.
        src = open(os.path.join(_DOCKER, 'edu6_arm_node.py'),
                   encoding='utf-8').read()
        seed = src.split('def _seed_goal_from_present_locked', 1)[1].split(
            '\n    def ', 1)[0]
        self.assertIn('fb.position_limit_window(', seed)
        probe = src.split('def probe_bus', 1)[1].split('\n    def ', 1)[0]
        self.assertIn('fb.position_limit_window(', probe)
        # Behaviour: the gripper's window is [2048, 3215]. A gripper reading of
        # 2000 is BELOW it, so the servo will hold 2048 — a 48-tick pull-in, not
        # the 0 a "goal == what we wrote" model would compute.
        node, _ = self._node({7: 2000})
        goals = None
        with node._bus_lock:
            goals = node._seed_goal_from_present_locked()
        self.assertEqual(goals[7], 2048)
        self.assertEqual(goals[1], 2048)             # in-window: unchanged
        lo, hi = fb.position_limit_window(*_N['JOINT_LIMITS_RAD'][6], 1)
        self.assertEqual((lo, hi), (2048, 3215))

    # ── env knob ───────────────────────────────────────────────────────────
    def test_abort_threshold_env_parse(self):
        f = _N['_parse_torque_abort_ticks']
        default = _N['_DEFAULT_TORQUE_ABORT_ERROR_TICKS']
        self.assertEqual(f(None), default)
        self.assertEqual(f(''), default)
        self.assertEqual(f('   '), default)
        self.assertEqual(f(' 1500 '), 1500)
        self.assertEqual(f('0'), 0)                  # the documented rollback
        for bad in ('abc', '-5', '1,2'):
            with patch('builtins.print') as printed:
                self.assertEqual(f(bad), default, bad)
            self.assertTrue(printed.called, bad)
            self.assertIn('EDUBOTICS_EDU6_TORQUE_ABORT_TICKS',
                          printed.call_args[0][0])

    def test_both_ends_of_the_knob_are_fenced(self):
        # BOTH extremes brick something, so both are refused by name.
        f = _N['_parse_torque_abort_ticks']
        default = _N['_DEFAULT_TORQUE_ABORT_ERROR_TICKS']
        floor = _N['_TORQUE_ABORT_MIN_TICKS']
        ceiling = _N['_TORQUE_ABORT_MAX_TICKS']
        # The floor is DERIVED from the default boot band. NOTE: its original
        # rationale ("below this it would abort the pull-in the boot probe
        # accepts") is FALSE — wrong_way_joints requires the wrapped distance to
        # be SHORTER than the naive one, and a clamp pull-in is already the short
        # way round, so it is excluded at every threshold. The floor is kept as
        # belt-and-braces against a magnitude-only refactor; see the constant.
        self.assertEqual(floor, _N['_DEFAULT_BOOT_POSITION_TOLERANCE_TICKS'] + 1)
        # The ceiling is the largest error a 12-bit register can express; above
        # it the check is silently dead — the sibling knob's ">= 4096 disables"
        # habit must not land there.
        self.assertEqual(ceiling, 4096 - 1)
        for bad in ('1', '100', str(floor - 1)):
            with patch('builtins.print') as printed:
                self.assertEqual(f(bad), default, bad)
            said = printed.call_args[0][0]
            self.assertIn('EDUBOTICS_EDU6_TORQUE_ABORT_TICKS', said)
            self.assertIn('0', said)          # names the real disable value
        for bad in ('4096', '999999'):
            with patch('builtins.print') as printed:
                self.assertEqual(f(bad), default, bad)
            said = printed.call_args[0][0]
            self.assertIn('EDUBOTICS_EDU6_TORQUE_ABORT_TICKS', said)
            self.assertIn('0', said)
        # the extremes that ARE legal are taken verbatim
        self.assertEqual(f(str(floor)), floor)
        self.assertEqual(f(str(ceiling)), ceiling)

    def test_zero_threshold_is_the_documented_rollback(self):
        self.assertEqual(_N['wrong_way_joints']({6: 4090}, {6: 12},
                                                threshold=0), [])
        with patch.dict(_N, {'TORQUE_ABORT_ERROR_TICKS': 0}):
            node, bus = self._node({6: 4055}, post_ticks={6: 12})
            self.assertTrue(node.set_torque(True), node.errors)
            self.assertTrue(node._torque_on)
            # the arm stays energised: only ONE Torque_Enable write, value 1
            self.assertEqual(bus.torque_writes(),
                             [(sid, 1) for sid in range(1, 8)])

    def test_env_override_is_wired_to_the_constant(self):
        # The parser is covered above; this pins that the SHIPPED constant is
        # actually BUILT from the env var (the harness pops the var, so it cannot
        # see a hardcoded default that leaves the rollback dead).
        src = open(os.path.join(_DOCKER, 'edu6_arm_node.py'),
                   encoding='utf-8').read()
        value = None
        for node in ast.parse(src).body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name)
                    and t.id == 'TORQUE_ABORT_ERROR_TICKS'
                    for t in node.targets):
                value = ast.unparse(node.value)
        self.assertIsNotNone(value)
        self.assertIn('_parse_torque_abort_ticks(', value)
        self.assertIn('EDUBOTICS_EDU6_TORQUE_ABORT_TICKS', value)

    def test_env_name_is_forwarded_by_compose(self):
        # ci.yml::env-forwarding-guard fails the build otherwise — and without
        # the forward the operator's rollback silently does nothing.
        compose = open(os.path.join(_HERE, '..', 'docker',
                                    'docker-compose.yml'),
                       encoding='utf-8').read()
        self.assertIn('EDUBOTICS_EDU6_TORQUE_ABORT_TICKS=', compose)


class TestTorqueSwitchRaiseDeEnergises(unittest.TestCase):
    """A RAISE out of the Torque_Enable write used to leave the arm in the one
    state a student may reach into: we believe LIMP, the arm may be LIVE.

    The goal seed is written BEFORE Torque_Enable by design, so by the time that
    write can fail the seed is already on the wire — and LeRobot #3585 reports
    the STS firmware setting `Torque_Enable = 1` BY ITSELF on the next PID tick
    after an out-of-window goal write, regardless of host torque-off (A12,
    unmeasured on this arm). The old handler logged and returned False without
    de-energising, and `_torque_on` was never set, so every caller reported the
    arm limp. The fix closes the state UNCONDITIONALLY — it does not rest on how
    A12 turns out."""

    def _node(self, ticks=None, missing=(), post_ticks=None, missing_at=None):
        bus = _TorqueFakeBus(ticks, missing, post_ticks, missing_at)
        return _TorqueStubNode(bus), bus

    @staticmethod
    def _raise_on_torque_writes(bus, only_nth=None):
        """Make the Torque_Enable sync_write raise; everything else lands.
        ``only_nth`` counts Torque_Enable writes (1 = the energise, 2 = the
        de-energise), so a failing energise and a failing DROP can be separated."""
        real = bus.sync_write
        seen = {'n': 0}

        def _flaky(addr, per_servo):
            if addr == fb.REG_TORQUE_ENABLE:
                seen['n'] += 1
                if only_nth is None or seen['n'] == only_nth:
                    real(addr, per_servo)     # record the ATTEMPT, then fail
                    raise fb.FeetechBusError('bus went away mid-torque-write')
            real(addr, per_servo)

        bus.sync_write = _flaky
        return seen

    @staticmethod
    def _off_writes(bus):
        return [per for addr, per in bus.writes
                if addr == fb.REG_TORQUE_ENABLE
                and all(v == b'\x00' for v in per.values())]

    # ── case 1: the defect itself ──────────────────────────────────────────
    def test_a_raising_torque_enable_write_still_de_energises_the_arm(self):
        node, bus = self._node()
        self._raise_on_torque_writes(bus, only_nth=1)
        self.assertFalse(node.set_torque(True))
        # the seed DID land (that is why a drop is owed at all) …
        self.assertEqual(bus.addrs()[0], fb.REG_ACCELERATION)
        # … and an explicit all-servo Torque_Enable = 0 followed the failure.
        self.assertEqual(len(self._off_writes(bus)), 1)
        self.assertEqual(self._off_writes(bus)[0],
                         {sid: b'\x00' for sid in range(1, 8)})
        self.assertFalse(node._torque_on)
        # the drop is the module constant, not a rebuilt dict on the hot path
        self.assertEqual(self._off_writes(bus)[0], _N['_TORQUE_OFF_PAYLOAD'])

    def test_the_belief_follows_the_hardware_when_the_drop_lands(self):
        # A REDUNDANT torque-on over an arm we already believed torqued: the
        # energise write fails, the drop succeeds, so `_torque_on` must move to
        # False. Left True, the write loop would keep commanding a limp arm and
        # _trajectory_cb would keep accepting trajectories for it.
        node, bus = self._node()
        node._torque_on = True
        self._raise_on_torque_writes(bus, only_nth=1)
        self.assertFalse(node.set_torque(True))
        self.assertFalse(node._torque_on)

    # ── case 2: the drop itself fails ──────────────────────────────────────
    def test_a_failed_drop_says_so_in_German_and_never_claims_limp(self):
        node, bus = self._node()
        node._torque_on = True
        self._raise_on_torque_writes(bus)          # EVERY torque write raises
        self.assertFalse(node.set_torque(True))    # no exception escapes
        # Claiming limp after a FAILED drop is the same wrong belief inverted.
        self.assertTrue(node._torque_on)
        said = [e for e in node.errors if 'FEHLGESCHLAGEN' in e]
        self.assertEqual(len(said), 1, node.errors)
        self.assertIn('[FEHLER]', said[0])
        # It must name the ONE remaining action, and it is a PHYSICAL one:
        # software has just proven it cannot reach the servos.
        self.assertIn('12-V-Netzteil', said[0])
        self.assertIn('ausschalten', said[0])
        # …and it must never imply the arm ended up safe. Both sibling messages
        # (wrong_way_abort_message / post_energise_read_failure_message) switch
        # to exactly this wording on a failed drop for the same reason.
        self.assertNotIn('ist jetzt weich', said[0])
        self.assertNotIn('wurde sofort wieder ausgeschaltet', said[0])

    def test_no_German_marker_line_in_the_driver_is_transliterated(self):
        # Rule §1 wants literal ä/ö/ü/ß. ci.yml::german-strings-lint enforces
        # that for physical_ai_server/, gui/, installer/ and pi_agent/ — it does
        # NOT scan robotis_ai_setup/docker/open_manipulator/ (docs §8.4 A8), so
        # every German string in this driver, including the one added above, is
        # CI-unguarded. Same marker prefixes and the same word list the CI job
        # greps for, so this cannot drift away from it silently.
        src = open(os.path.join(_DOCKER, 'edu6_arm_node.py'),
                   encoding='utf-8').read().splitlines()
        bad = re.compile(
            r'(\[FEHLER\]|\[WARNUNG\]|\[STOPP\]).*\b('
            r'frueh|kuerzer|schliessen|Zeitluecken|Groesste|Luecken|Moegliche|'
            r'ueber[a-z]|laeuft|haengt|pruefen|Pruefung|ueberschritten|'
            r'Schueler|benoetigen|Oberflaeche|uebersprungen|enthaelt|'
            r'zusaetzliche|Verfuegbar|geaendert|beschaedigt|koennte|faehige|'
            r'Bildaufloesung)\b')
        self.assertEqual([ln for ln in src if bad.search(ln)], [])

    # ── case 3: the two exits that need NOTHING ────────────────────────────
    def test_a_refused_seed_writes_nothing_and_therefore_drops_nothing(self):
        # BOTH seed refusals happen BEFORE the seed sync_write, so no register
        # was touched: issuing a torque-off there would be bus traffic (and, on
        # a genuinely dead bus, another failure) for a state that cannot exist.
        for label, kw in (('read failure', {'missing': (5,)}),
                          ('edge refusal', {'ticks': {6: 4093}})):
            node, bus = self._node(**kw)
            self.assertFalse(node.set_torque(True), label)
            self.assertEqual(bus.writes, [], label)      # nothing at all
            self.assertEqual(self._off_writes(bus), [], label)
            self.assertIn(node._torque_refusal[0], ('read', 'edge'), label)

    def test_a_raising_SEED_write_drops_nothing_because_nothing_is_known_landed(
            self):
        # goal_ticks is only bound once the seed RETURNED, so a seed that raised
        # leaves it None and the handler stays out of the way. (Recorded
        # residual: a partially transmitted seed packet is indistinguishable
        # from none — the same fire-and-forget blind spot SYNC_WRITE has
        # everywhere in this driver.)
        node, bus = self._node()
        real = bus.sync_write

        def _boom(addr, per_servo):
            if addr == fb.REG_ACCELERATION:
                raise fb.FeetechBusError('bus went away during the seed')
            real(addr, per_servo)

        bus.sync_write = _boom
        self.assertFalse(node.set_torque(True))
        self.assertEqual(self._off_writes(bus), [])
        self.assertFalse(node._torque_on)

    # ── case 4: the verifier already owns its own drop ─────────────────────
    def test_the_post_energise_abort_still_drops_exactly_once(self):
        # _verify_after_energise_locked writes Torque_Enable = 0 as its FIRST
        # action, and it returns through the `refusal is not None` path, not the
        # exception handler — so there must be exactly ONE drop, not two.
        node, bus = self._node({6: 4055}, post_ticks={6: 12})
        self.assertFalse(node.set_torque(True))
        self.assertEqual(node._torque_refusal[0], 'wrongway')
        self.assertEqual(len(self._off_writes(bus)), 1)
        self.assertFalse(node._torque_on)

    # ── case 5: the inverted bug ───────────────────────────────────────────
    def test_a_failed_torque_OFF_never_forces_the_belief_to_limp(self):
        # The arm may still be torqued — that is exactly why the write was
        # requested. Recording it as limp would let _trajectory_cb drop incoming
        # trajectories with „Drehmoment ist aus" for an arm that is holding, and
        # would hide a live arm behind a limp label. `enabled` is what excludes
        # this path from the de-energise above.
        node, bus = self._node()
        node._torque_on = True
        self._raise_on_torque_writes(bus)
        self.assertFalse(node.set_torque(False))
        self.assertTrue(node._torque_on)
        # exactly the ONE attempted write set_torque always makes — the handler
        # added nothing here
        self.assertEqual(len(self._off_writes(bus)), 1)
        self.assertEqual(node.errors, [e for e in node.errors
                                       if 'FEHLGESCHLAGEN' not in e])

    # ── case 6: the success paths are untouched ────────────────────────────
    def test_every_success_path_is_byte_identical(self):
        # A healthy enable, a healthy disable, and a redundant enable over a
        # joint parked on the seam — the handler must be unreachable on all of
        # them, so the bus traffic is exactly what it was before it existed.
        node, bus = self._node()
        self.assertTrue(node.set_torque(True), node.errors)
        self.assertEqual(bus.addrs(), [fb.REG_ACCELERATION,
                                       fb.REG_TORQUE_ENABLE])
        self.assertEqual(bus.torque_writes(), [(s, 1) for s in range(1, 8)])
        self.assertEqual(node.errors, [])

        node, bus = self._node()
        node._torque_on = True
        self.assertTrue(node.set_torque(False), node.errors)
        self.assertEqual(bus.addrs(), [fb.REG_TORQUE_ENABLE])
        self.assertEqual(bus.torque_writes(), [(s, 0) for s in range(1, 8)])
        self.assertFalse(node._torque_on)
        self.assertEqual(node.errors, [])

        node, bus = self._node({6: 4094})
        node._torque_on = True
        self.assertTrue(node.set_torque(True), node.errors)
        self.assertEqual(bus.addrs(), [fb.REG_ACCELERATION,
                                       fb.REG_TORQUE_ENABLE])
        self.assertEqual(node.errors, [])

    # ── the lock discipline the handler rests on ───────────────────────────
    def test_the_drop_re_takes_the_bus_lock_and_cannot_deadlock_doing_it(self):
        # `_bus_lock` is a plain, NON-reentrant threading.Lock, so this is only
        # safe because the `with` block already released it while the exception
        # propagated out. Assert BOTH halves: the call completes (no deadlock),
        # and the lock really is held while the drop is on the wire (the read
        # and write loops share that bus).
        node, bus = self._node()
        held = []
        real = bus.sync_write

        def _watch(addr, per_servo):
            held.append((addr, node._bus_lock.locked()))
            real(addr, per_servo)
            if addr == fb.REG_TORQUE_ENABLE and len(held) == 2:
                raise fb.FeetechBusError('bus went away mid-torque-write')

        bus.sync_write = _watch
        self.assertFalse(node.set_torque(True))
        self.assertEqual([addr for addr, _ in held],
                         [fb.REG_ACCELERATION, fb.REG_TORQUE_ENABLE,
                          fb.REG_TORQUE_ENABLE])
        self.assertEqual([locked for _a, locked in held], [True, True, True])
        # and it is free again afterwards — the handler cannot leak it
        self.assertFalse(node._bus_lock.locked())

    def test_goal_ticks_is_hoisted_above_the_try(self):
        # Assigned INSIDE the try, a raise before that assignment makes the
        # handler's own read a NameError — a crash on the one path whose whole
        # job is to leave the arm in a known state. Source-pinned because no
        # runtime path can reach the raise that early (the `with` acquire is the
        # only statement before it).
        src = textwrap.dedent(ast.get_source_segment(
            open(os.path.join(_DOCKER, 'edu6_arm_node.py'),
                 encoding='utf-8').read(),
            _set_torque_ast()))
        head, _sep, _tail = src.partition('        try:')
        self.assertIn('goal_ticks', head)
        body = src[src.index('        try:'):]
        self.assertNotIn('goal_ticks = None', body)


def _set_torque_ast():
    src = open(os.path.join(_DOCKER, 'edu6_arm_node.py'),
               encoding='utf-8').read()
    for node in ast.parse(src).body:
        if isinstance(node, ast.ClassDef) and node.name == 'Edu6ArmNode':
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == 'set_torque':
                    return sub
    raise AssertionError('Edu6ArmNode.set_torque not found')


class TestNodeAuditRails(unittest.TestCase):
    """Source-level pins for the driver rails that need threads/rclpy to
    exercise for real: the write-loop bus-fault gate (M5), the re-torque
    buffer clear (M2), the non-finite refusal (M3) and the wall-clock miss
    window (M5). Behavioural cousins live above; these keep the wiring from
    being 'simplified' away."""

    def setUp(self):
        self.src = open(os.path.join(_DOCKER, 'edu6_arm_node.py'),
                        encoding='utf-8').read()

    def test_write_loop_gated_on_bus_fault(self):
        self.assertIn('and not self._bus_fault', self.src)

    def test_wall_clock_miss_window(self):
        self.assertIn('READ_FAIL_STOP_S', self.src)
        self.assertNotIn('_read_fail_streak', self.src)

    def test_trajectory_cb_validates_and_drops(self):
        cb = self.src.split('def _trajectory_cb', 1)[1].split(
            '\n    def ', 1)[0]
        self.assertIn('math.isfinite', cb)          # M3
        self.assertIn('not self._torque_on', cb)    # M2 limp drop
        self.assertIn('self._bus_fault', cb)        # M5 fault drop

    def test_torque_enable_clears_buffer(self):
        cb = self.src.split('def _torque_cb', 1)[1].split('\n    def ', 1)[0]
        head = cb.split('self.set_torque', 1)[0]
        self.assertIn('if request.data:', head)
        self.assertIn('_replace_trajectory([])', head)

    def test_torque_on_seeds_goal_position(self):
        # Without this the servo keeps its stale RAM Goal_Position from before
        # the limp phase and drives there at up to ~4.36 rad/s the instant
        # torque returns. The seed must happen BEFORE Torque_Enable is written,
        # and a failed read must ABORT the torque-on rather than energize blind.
        self.assertIn('_seed_goal_from_present_locked', self.src)
        seed = self.src.split('def _seed_goal_from_present_locked', 1)[1].split(
            '\n    def ', 1)[0]
        self.assertIn('REG_PRESENT_POSITION', seed)
        # A failed read / refused edge park ABORTS the torque-on. Since
        # 2026-07-26 the success return is the EFFECTIVE per-servo goal dict the
        # post-energise abort compares against, so the refusal sentinel is None
        # (both are falsy — set_torque's `if not goal_ticks` covers either).
        self.assertIn('return None', seed)
        self.assertNotIn('return True', seed)
        self.assertIn('return goals', seed)
        # The seed must carry acceleration + a speed cap, i.e. the SAME 7-byte
        # contiguous block _write_targets uses — not a bare 2-byte goal. A limp
        # joint that sagged outside its EEPROM window is PULLED to the window
        # edge at torque-on (the servo clamps Goal_Position), and the boot band
        # bounds that distance; without these registers the pull runs at the
        # servo's power-on defaults, which this driver never writes or reads.
        self.assertIn('REG_ACCELERATION', seed)
        self.assertIn('WRITE_ACCELERATION', seed)
        # Check the PAYLOAD construction, not prose: the docstring legitimately
        # names GOAL_SPEED_CAP_STEPS when describing the hazard being prevented.
        self.assertIn('fb.le16(SEED_SPEED_STEPS)', seed)
        self.assertNotIn('fb.le16(GOAL_SPEED_CAP_STEPS)', seed)
        # ...and it must be markedly gentler than the motion cap: at the cap a
        # full-band pull-in is a ~0.14 s snap, here it is a ~2 s creep.
        self.assertLess(_N['SEED_SPEED_STEPS'], _N['GOAL_SPEED_CAP_STEPS'] // 4)
        st = self.src.split('def set_torque', 1)[1].split('\n    def ', 1)[0]
        # ordering: seed call must precede the Torque_Enable write
        self.assertLess(st.index('_seed_goal_from_present_locked'),
                        st.index('REG_TORQUE_ENABLE'))
        # shutdown drops the trajectory BEFORE torque-off so the write thread
        # cannot land one more goal on the way out.
        sd = self.src.split('def shutdown', 1)[1].split('\n    def ', 1)[0]
        self.assertLess(sd.index('_replace_trajectory([])'),
                        sd.index('set_torque(False)'))

    def test_read_loop_never_dies(self):
        # `_bus_fault` is only ever set from inside the read loop, so a raise
        # that killed the thread would leave the WRITE loop commanding an arm
        # nobody reads. The guard must wrap the WHOLE cycle, not just sync_read.
        rl = self.src.split('def _read_loop', 1)[1].split('\n    def ', 1)[0]
        self.assertIn('self._read_tick()', rl)
        self.assertIn('except Exception', rl)
        self.assertIn('self._bus_fault = True', rl)
        self.assertIn('_replace_trajectory([])', rl)
        # The publish CALL now lives inside the guarded tick, not the loop.
        # Match the call site exactly — the loop's comment names the method.
        tick = self.src.split('def _read_tick', 1)[1].split('\n    def ', 1)[0]
        self.assertIn('self._publish_joint_state(', tick)
        self.assertNotIn('self._publish_joint_state(', rl)
        # The latch must clear only AFTER a clean publish: clearing first would
        # let a persistently-raising publish un-gate the write loop for one
        # cycle every tick (and storm the log with error/recovery pairs).
        self.assertLess(tick.index('self._publish_joint_state('),
                        tick.index('self._bus_fault = False'))

    def test_boot_home_verification_wired(self):
        # Decision A: boot-home is verified (mirror OMX Phase-3), re-sends the
        # glide from the ACTUAL pose on a stall, and never commands a dead bus.
        self.assertIn('boot_home_verify_decision', self.src)
        sb = self.src.split('def start_boot_home', 1)[1].split(
            '\n    def ', 1)[0]
        self.assertIn('_boot_home_verify', sb)       # verify thread launched
        vb = self.src.split('def _boot_home_verify', 1)[1].split(
            '\n    def ', 1)[0]
        self.assertIn('build_boot_home', vb)         # re-send from actual pose
        self.assertIn('self._bus_fault', vb)         # never command a dead bus

    def test_boot_home_resend_yields_the_command_rail(self):
        # The verifier wakes ~3.7 s after boot-home starts and may re-send the
        # glide. If a jog/workflow/abort has taken the rail in the meantime, a
        # re-send would FIGHT it. Every deliberate trajectory write bumps
        # _traj_gen; the verifier holds the generation it was started with and
        # bails when it no longer matches.
        self.assertIn('def _replace_trajectory', self.src)
        self.assertIn('self._traj_gen += 1', self.src)
        sb = self.src.split('def start_boot_home', 1)[1].split(
            '\n    def ', 1)[0]
        self.assertIn('gen = self._replace_trajectory', sb)
        self.assertIn('args=(gen,)', sb)             # token handed to the thread
        vb = self.src.split('def _boot_home_verify', 1)[1].split(
            '\n    def ', 1)[0]
        self.assertIn('self._traj_gen != gen', vb)
        # ...and the bail must precede the re-send, or the guard is decorative.
        self.assertLess(vb.index('self._traj_gen != gen'),
                        vb.index('build_boot_home'))
        # The write loop's natural expiry is the ONE bypass, on purpose: a
        # command that merely ENDED is not a takeover.
        wl = self.src.split('def _write_loop', 1)[1].split('\n    def ', 1)[0]
        self.assertIn('self._traj_points = []', wl)
        self.assertNotIn('_traj_gen +=', wl)         # mentioned in a comment only

    def test_joint_state_publishes_present_load_as_effort(self):
        # FREE telemetry: the read loop already sync_reads 6 bytes from
        # Present_Position, which spans position(56/57) + speed(58/59) +
        # LOAD(60/61) — the load bytes were decoded nowhere and discarded.
        # Publishing them as JointState.effort is what gives rig gates R4
        # (pinch force) and R6 (joint sweeps) real numbers instead of guesses.
        pj = self.src.split('def _publish_joint_state', 1)[1].split(
            '\n    def ', 1)[0]
        self.assertIn('data[4], data[5]', pj)          # the load bytes
        self.assertIn('msg.effort', pj)
        # Present_Load's sign is bit 10, NOT bit 15 like position/speed —
        # decoding it with 15 would turn every negative load into a huge
        # positive one.
        self.assertIn('raw_load, 10', pj)
        self.assertIn('PRESENT_LOAD_FULLSCALE', pj)
        self.assertIn('JOINT_SIGNS[i]', pj)            # URDF direction applied
        # Full scale matches the OMX collision detector's own Present-Load
        # convention (±1000 = ±100 % PWM), so 'effort fraction' means one
        # thing fleet-wide.
        self.assertEqual(_N['PRESENT_LOAD_FULLSCALE'], 1000.0)

    def test_present_load_sign_bit_10_decode(self):
        # Numeric proof of the bit-10 convention the pin above enforces.
        self.assertEqual(fb.decode_sign_magnitude(5, 10), 5)
        self.assertEqual(fb.decode_sign_magnitude((1 << 10) | 5, 10), -5)
        self.assertEqual(fb.decode_sign_magnitude(1000, 10), 1000)
        # A full negative load: 1000 with the bit-10 sign set.
        self.assertEqual(fb.decode_sign_magnitude((1 << 10) | 1000, 10), -1000)
        # Decoding the SAME word with bit 15 (the position convention) would
        # mis-read it as a large positive — the bug this pins against.
        self.assertEqual(fb.decode_sign_magnitude((1 << 10) | 5, 15), 1029)

    def test_boot_torque_on_retries_before_giving_up(self):
        # set_torque correctly REFUSES when the goal-seed read fails, but
        # nothing else retries boot torque-on — so a single bus hiccup used to
        # leave the arm limp (and therefore sagging) for the whole session.
        self.assertGreaterEqual(_N['BOOT_TORQUE_ON_ATTEMPTS'], 2)
        sb = self.src.split('def start_boot_home', 1)[1].split(
            '\n    def ', 1)[0]
        self.assertIn('for attempt in range(BOOT_TORQUE_ON_ATTEMPTS)', sb)
        self.assertIn('BOOT_TORQUE_ON_RETRY_S', sb)
        # exhausting the retries must fail LOUD in German, not fall through
        self.assertIn('[FEHLER]', sb.split('else:', 1)[1])
        # the glide start pose is re-read after torque-on (a retry costs
        # seconds during which a limp arm keeps sagging)
        self.assertLess(sb.index('for attempt in range(BOOT_TORQUE_ON_ATTEMPTS)'),
                        sb.index('fresh = self._read_positions_once()'))
        self.assertLess(sb.index('fresh = self._read_positions_once()'),
                        sb.index('build_boot_home(current)'))


class TestUrdfDriverNoDrift(unittest.TestCase):
    """Audit L1: the driver/provision limit literals and the profile HOME
    must equal the URDF/profile-side truth — these pairs were previously
    unpinned across the tree boundary."""

    _URDF = os.path.join(_HERE, '..', '..', 'physical_ai_tools',
                         'physical_ai_manager', 'public', 'edu6-urdf',
                         'edu6.urdf')
    _PROFILES = os.path.join(_HERE, '..', '..', 'physical_ai_tools',
                             'physical_ai_server', 'physical_ai_server',
                             'robot_profiles.py')

    def test_arm_joint_limits_match_urdf(self):
        import xml.etree.ElementTree as ET
        root = ET.parse(self._URDF).getroot()
        urdf = {}
        for j in root.findall('joint'):
            lim = j.find('limit')
            if lim is not None:
                urdf[j.get('name')] = (float(lim.get('lower')),
                                       float(lim.get('upper')))
        for i, name in enumerate(['joint1', 'joint2', 'joint3', 'joint4',
                                  'joint5', 'joint6']):
            self.assertEqual(tuple(_N['JOINT_LIMITS_RAD'][i]), urdf[name],
                             name)
        # The gripper band is DELIBERATELY narrower than the URDF model
        # artifact (command band 0..1.79 vs model 0..2.0944) — pin the
        # relation, not equality.
        lo, hi = _N['JOINT_LIMITS_RAD'][6]
        self.assertEqual(lo, 0.0)
        self.assertLess(hi, urdf['end_gear_joint'][1])

    def test_home_matches_server_profile_literal(self):
        source = open(self._PROFILES, encoding='utf-8').read()
        home = None
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name)
                    and t.id == '_EDU6_HOME_JOINTS_RAD'
                    for t in node.targets):
                home = ast.literal_eval(node.value)
        self.assertIsNotNone(home)
        self.assertEqual(tuple(home), tuple(_N['HOME_JOINTS_RAD']))


class TestShippedWiring(unittest.TestCase):
    """The load-bearing packaging assertions (Dockerfile lists, entrypoint
    branch, compose forward, env-guard list) — the ✎/✎✎ items of plan §4.3/§9."""

    def _read(self, *parts):
        return open(os.path.join(_HERE, '..', *parts), encoding='utf-8').read()

    def test_dockerfile_copies_and_lists(self):
        df = self._read('docker', 'open_manipulator', 'Dockerfile')
        self.assertIn('COPY feetech_bus.py /usr/local/bin/feetech_bus.py', df)
        self.assertIn('COPY edu6_arm_node.py /usr/local/bin/edu6_arm_node.py', df)
        # BOTH the CRLF-strip and chmod lists (a Windows checkout otherwise
        # ships them corrupt — exactly what that RUN exists to prevent).
        sed_line = [line for line in df.splitlines() if "sed -i 's/\\r$//'" in line]
        chmod_line = [line for line in df.splitlines() if 'chmod +x' in line]
        self.assertTrue(any('edu6_arm_node.py' in ln and 'feetech_bus.py' in ln
                            for ln in sed_line), sed_line)
        self.assertTrue(any('edu6_arm_node.py' in ln and 'feetech_bus.py' in ln
                            for ln in chmod_line), chmod_line)

    def test_entrypoint_branches_and_keeps_cameras(self):
        ep = self._read('docker', 'open_manipulator', 'entrypoint_omx.sh')
        self.assertIn('edu6_studio', ep)
        self.assertIn('edu6_arm_node.py', ep)
        # Phase 4 must still run on the edu6 branch: the branch head sits
        # BEFORE the camera phase in the file (shared tail).
        self.assertLess(ep.index('edu6_arm_node.py'), ep.index('Phase 4'))
        # Phase 2 (OMX follower launch) is guarded off for edu6.
        phase2 = ep[ep.index('Phase 2'):ep.index('Phase 3')]
        self.assertIn('edu6_studio', phase2)

    def test_compose_forwards_the_node_envs(self):
        compose = self._read('docker', 'docker-compose.yml')
        self.assertIn('EDUBOTICS_EDU6_JOINT_SIGNS=', compose)
        # EDUBOTICS_ROBOT_TYPE now reaches the open_manipulator service too.
        om = compose[compose.index('open_manipulator:'):compose.index('physical_ai_server:')]
        self.assertIn('EDUBOTICS_ROBOT_TYPE=', om)

    def test_ci_env_guard_scans_the_node(self):
        ci = open(os.path.join(_HERE, '..', '..', '.github', 'workflows',
                               'ci.yml'), encoding='utf-8').read()
        self.assertIn('edu6_arm_node.py', ci)

    def test_parity_script_verifies_plain_copies(self):
        parity = open(os.path.join(_HERE, '..', '..', '.github', 'scripts',
                                   'image_source_parity.sh'),
                      encoding='utf-8').read()
        self.assertIn('/usr/local/bin', parity)
        self.assertIn('edu6_arm_node.py', parity)
        self.assertIn('feetech_bus.py', parity)

    def test_identify_arm_speaks_both_protocols(self):
        ia = self._read('docker', 'open_manipulator', 'identify_arm.py')
        self.assertIn('identify_feetech', ia)
        self.assertIn('edu6_arm_found', ia)   # cross-probe token (dxl side)
        self.assertIn('omx_arm_found', ia)    # cross-probe token (feetech side)
        self.assertIn('777', ia)


class TestGuiDetectionSeam(unittest.TestCase):
    """PR-6 GUI detection: the family tables + scan wiring (deps-free)."""

    def test_arm_usb_ids_table(self):
        sys.path.insert(0, os.path.join(_HERE, '..'))
        from gui.app.constants import ARM_USB_IDS, ROBOT_PROFILES
        self.assertEqual(ARM_USB_IDS['omx'], (('2F5D', None),))
        self.assertEqual(ARM_USB_IDS['edu6'], (('1A86', '55D3'),))
        for pid, row in ROBOT_PROFILES.items():
            self.assertIn(row['arm_family'], ARM_USB_IDS, pid)
            self.assertIn('camera_roles', row, pid)
        self.assertEqual(ROBOT_PROFILES['edu6_studio']['camera_roles'],
                         ('scene',))
        self.assertEqual(ROBOT_PROFILES['edu6_studio']['arm_family'], 'edu6')

    def test_list_arm_devices_filters_by_family(self):
        from gui.app import device_manager as dm

        class _Dev:
            def __init__(self, vid_pid):
                self.vid_pid = vid_pid
                self.busid = '1-1'
                self.description = 'x'
                self.state = 'Not shared'

        devs = [_Dev('2F5D:0103'), _Dev('1A86:55D3'), _Dev('1A86:7523'),
                _Dev('046D:0825')]
        orig = dm.list_usb_devices
        dm.list_usb_devices = lambda: devs
        try:
            omx = dm.list_arm_devices('omx')
            edu6 = dm.list_arm_devices('edu6')
        finally:
            dm.list_usb_devices = orig
        self.assertEqual([d.vid_pid for d in omx], ['2F5D:0103'])
        # the CH34x PID pin: 1A86:7523 (a generic dongle) must NOT match.
        self.assertEqual([d.vid_pid for d in edu6], ['1A86:55D3'])

    def test_find_serial_paths_edu6_markers(self):
        from gui.app import device_manager as dm
        paths = [
            '/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_XYZ-if00',
            '/dev/serial/by-id/usb-1a86_USB_Single_Serial_5AE-if00',
        ]
        orig = dm.wsl_bridge.list_serial_devices
        dm.wsl_bridge.list_serial_devices = lambda: paths
        try:
            self.assertEqual(dm.find_serial_paths_for_arms('omx'), [paths[0]])
            self.assertEqual(dm.find_serial_paths_for_arms('edu6'), [paths[1]])
        finally:
            dm.wsl_bridge.list_serial_devices = orig

    def test_ps_allowlists_carry_the_ch343(self):
        for name in ('configure_usbipd.ps1', 'verify_system.ps1',
                     'install_prerequisites.ps1'):
            with open(os.path.join(_HERE, '..', 'installer', 'scripts', name),
                      encoding='utf-8-sig') as fh:
                text = fh.read()
            self.assertTrue('1a86' in text.lower(), name)

    def test_identify_via_docker_protocol_flag(self):
        from gui.app import device_manager as dm
        calls = []

        class _Res:
            returncode = 0
            stdout = 'edu6\n'
            stderr = ''

        orig = dm.subprocess.run

        def _fake_run(argv, **kw):
            calls.append(list(argv))
            return _Res()

        dm.subprocess.run = _fake_run
        try:
            self.assertEqual(dm.identify_arm_via_docker('/dev/x', 'feetech'),
                             'edu6')
            self.assertEqual(dm.identify_arm_via_docker('/dev/x'), 'edu6')
        finally:
            dm.subprocess.run = orig
        self.assertIn('--protocol=feetech', calls[0])
        self.assertNotIn('--protocol=feetech', calls[1])

    def test_identify_via_docker_surfaces_nonzero_exit_token(self):
        # identify_arm.py exits NON-ZERO for every non-expected verdict (incl.
        # the informational feetech_silent / omx_arm_found tokens) — the stdout
        # verdict must still WIN, else those scan-notice branches are dead.
        from gui.app import device_manager as dm

        class _Res:
            returncode = 1
            stdout = 'feetech_silent\n'
            stderr = ''

        class _Crash:
            returncode = 1
            stdout = ''
            stderr = 'Traceback ...'

        orig = dm.subprocess.run
        try:
            dm.subprocess.run = lambda argv, **kw: _Res()
            self.assertEqual(dm.identify_arm_via_docker('/dev/x', 'feetech'),
                             'feetech_silent')
            # Empty stdout (a genuine crash) still yields an error token.
            dm.subprocess.run = lambda argv, **kw: _Crash()
            self.assertTrue(
                dm.identify_arm_via_docker('/dev/x').startswith('error:'))
        finally:
            dm.subprocess.run = orig


class TestProvisionTool(unittest.TestCase):
    """tools/edu6_provision.py pure parts (PR 8)."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        path = os.path.join(_HERE, '..', '..', 'tools', 'edu6_provision.py')
        spec = importlib.util.spec_from_file_location('edu6_provision', path)
        cls.prov = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.prov)

    def test_limits_to_ticks_positive_sign(self):
        # joint5 (index 4): the ASYMMETRIC −1.5708..1.9199 window.
        lo, hi = self.prov.limits_to_ticks(4, (1,) * 7)
        self.assertEqual(lo, 2048 + round(-1.5708 / (2 * 3.141592653589793 / 4096)))
        self.assertEqual(hi, 2048 + round(1.9199 / (2 * 3.141592653589793 / 4096)))
        self.assertLess(lo, 2048)
        self.assertGreater(hi, 2048)

    def test_limits_to_ticks_negative_sign_mirrors(self):
        lo_p, hi_p = self.prov.limits_to_ticks(4, (1,) * 7)
        lo_n, hi_n = self.prov.limits_to_ticks(4, (1, 1, 1, 1, -1, 1, 1))
        # mirrored around the centre AND re-ordered lo<=hi.
        self.assertEqual(lo_n, 2 * 2048 - hi_p)
        self.assertEqual(hi_n, 2 * 2048 - lo_p)
        self.assertLessEqual(lo_n, hi_n)

    def test_limits_clamped_to_register_range(self):
        for i in range(7):
            lo, hi = self.prov.limits_to_ticks(i, (1,) * 7)
            self.assertGreaterEqual(lo, 0)
            self.assertLessEqual(hi, 4095)

    def test_record_checksum_stable_and_sensitive(self):
        entries = [{'id': 1, 'homing_offset': 5}]
        signs = (1, 1, 1, 1, 1, 1, 1)
        a = self.prov.record_checksum(entries, 'EDU6-0001', signs)
        b = self.prov.record_checksum(entries, 'EDU6-0001', signs)
        c = self.prov.record_checksum(entries, 'EDU6-0002', signs)
        d = self.prov.record_checksum([{'id': 1, 'homing_offset': 6}],
                                      'EDU6-0001', signs)
        # signs are part of the digest: the same EEPROM under a different
        # direction convention is a DIFFERENT calibration.
        e = self.prov.record_checksum(entries, 'EDU6-0001',
                                      (1, 1, 1, 1, -1, 1, 1))
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertNotEqual(a, d)
        self.assertNotEqual(a, e)

    def test_gripper_gets_the_pinch_floor_torque(self):
        self.assertEqual(self.prov.GRIPPER_MAX_TORQUE, 150)  # §8 ≈10 N
        self.assertGreater(self.prov.ARM_MAX_TORQUE,
                           self.prov.GRIPPER_MAX_TORQUE)

    def test_limits_match_the_driver_node(self):
        # No-drift: the provision tool and edu6_arm_node must write/enforce the
        # SAME designed limits.
        self.assertEqual(tuple(self.prov.JOINT_LIMITS_RAD),
                         tuple(_N['JOINT_LIMITS_RAD']))


class _ProvFakeBus:
    """Register-model fake bus for the edu6_provision end-to-end tests.

    Byte-addressable EEPROM/RAM per servo; Present_Position is COMPUTED as
    (actual − Homing_Offset) on every read, so writing the offset changes what
    the still-jigged arm reports — which is exactly what the new jig-pose
    postcondition re-read depends on. Every write is logged so the ordering /
    endurance-skip / commit-point pins can be asserted."""

    def __init__(self, actual_ticks, phase=0x10, return_delay=250, model=None,
                 mode=0, response_level=1, torque=1, models=None):
        self.actual = dict(actual_ticks)
        self.writes = []          # (sid, addr, bytes) in call order
        self.closed = False
        self.mem = {}
        default_model = fb.STS3215_MODEL_NUMBER if model is None else model
        for sid in actual_ticks:
            m = bytearray(256)
            m[fb.REG_FIRMWARE_MAJOR] = 3
            m[fb.REG_FIRMWARE_MAJOR + 1] = 9
            sid_model = (models or {}).get(sid, default_model)
            m[fb.REG_MODEL_NUMBER] = sid_model & 0xFF
            m[fb.REG_MODEL_NUMBER + 1] = (sid_model >> 8) & 0xFF
            m[fb.REG_PHASE] = phase              # bit 4 set → provision clears it
            m[fb.REG_LOCK] = 1                   # protected → provision writes 0
            m[fb.REG_RETURN_DELAY] = return_delay
            m[fb.REG_OPERATING_MODE] = mode      # 0 = position (factory)
            m[fb.REG_RESPONSE_STATUS_LEVEL] = response_level  # 1 = factory
            m[fb.REG_TORQUE_ENABLE] = torque     # 1 → the verified torque-off writes
            self.mem[sid] = m

    def ping(self, sid, timeout_s=None):
        return sid in self.mem

    def _homing_offset(self, sid):
        return fb.decode_sign_magnitude(
            fb.from_le16(self.mem[sid][fb.REG_HOMING_OFFSET],
                         self.mem[sid][fb.REG_HOMING_OFFSET + 1]), 11)

    def _present_raw(self, sid):
        # Present = (Actual − Homing_Offset) mod 4096 — the real servo clamps
        # Present into [0, 4095] with Phase bit 4 cleared, so a raw near the
        # 0/4095 boundary reads WRAPPED. Modelling this is what exercises the
        # provisioning wrap fix (a jig pose whose raw ≈ 0 under a legacy +off).
        return (self.actual[sid] - self._homing_offset(sid)) % 4096

    def read(self, sid, addr, length):
        if addr == fb.REG_PRESENT_POSITION:
            data = fb.le16(fb.encode_sign_magnitude(self._present_raw(sid), 15))
            return 0, data[:length]
        return 0, bytes(self.mem[sid][addr:addr + length])

    def read_u16(self, sid, addr):
        _e, d = self.read(sid, addr, 2)
        return fb.from_le16(d[0], d[1])

    def write(self, sid, addr, data, await_status=True):
        self.writes.append((sid, addr, bytes(data)))
        self.mem[sid][addr:addr + len(data)] = data
        return 0

    def close(self):
        self.closed = True

    def writes_for(self, sid):
        return [(a, d) for s, a, d in self.writes if s == sid]


class TestProvisionEndToEnd(unittest.TestCase):
    """tools/edu6_provision.py::provision against a scripted register bus —
    the §6 order + endurance + commit-point + jig-pose postcondition (finding
    9b), using the already-present _FakeSerial scaffolding's register cousin."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        path = os.path.join(_HERE, '..', '..', 'tools', 'edu6_provision.py')
        spec = importlib.util.spec_from_file_location('edu6_provision', path)
        cls.prov = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.prov)

    def test_full_provision_order_and_commit(self):
        actual = {sid: 2048 + sid for sid in range(1, 8)}  # nonzero offsets
        bus = _ProvFakeBus(actual)
        record = self.prov.provision(bus, 'EDU6-0001', (1,) * 7)
        # Record built ONLY after all 7 servos verified (the commit point).
        self.assertEqual(len(record['servos']), 7)
        self.assertEqual(record['arm_serial'], 'EDU6-0001')
        self.assertEqual(record['signs'], [1] * 7)
        for sid in range(1, 8):
            w = bus.writes_for(sid)
            addrs = [a for a, _ in w]
            # Torque_Enable=0 then Lock=0 come BEFORE any EEPROM write.
            self.assertEqual(w[0], (fb.REG_TORQUE_ENABLE, b'\x00'))
            self.assertEqual(w[1], (fb.REG_LOCK, b'\x00'))
            # Lock=1 restored as the LAST write (re-lock after verify).
            self.assertEqual(w[-1], (fb.REG_LOCK, b'\x01'))
            # EEPROM writes come after the two RAM prep writes.
            self.assertGreaterEqual(addrs.index(fb.REG_HOMING_OFFSET), 2)
            self.assertGreaterEqual(addrs.index(fb.REG_MAX_TORQUE_LIMIT), 2)
            # Phase bit-4 was SET → exactly one read-modify-write cleared it,
            # and it happened BEFORE the offset was baked (audit M1: the jig
            # read must run under post-clear position-reporting semantics).
            phase_writes = [d for a, d in w if a == fb.REG_PHASE]
            self.assertEqual(len(phase_writes), 1)
            self.assertEqual(phase_writes[0][0] & (1 << 4), 0)
            self.assertLess(addrs.index(fb.REG_PHASE),
                            addrs.index(fb.REG_HOMING_OFFSET))
            # Mode already 0 (factory) → read-compare-SKIP, no EEPROM write.
            self.assertNotIn(fb.REG_OPERATING_MODE, addrs)

    def test_angular_resolution_is_normalized_BEFORE_the_offset_is_baked(self):
        # B1. Register 30 is the OTHER register the vendor documents as rescaling
        # the tick↔angle map, so audit M1's argument for the Phase bit-4 clear
        # applies verbatim: the jig read that bakes Homing_Offset must run under
        # the same map the runtime uses. Writing it AFTER the offset would bake the
        # offset under one scale and run under another.
        bus = _ProvFakeBus({sid: 2048 + sid for sid in range(1, 8)})
        self.prov.provision(bus, 'EDU6-0001', (1,) * 7)
        for sid in range(1, 8):
            addrs = [a for a, _ in bus.writes_for(sid)]
            self.assertIn(fb.REG_ANGULAR_RESOLUTION, addrs)
            self.assertLess(addrs.index(fb.REG_ANGULAR_RESOLUTION),
                            addrs.index(fb.REG_HOMING_OFFSET))
            # …and it must land as the single-turn value, not just "some write".
            written = [d for a, d in bus.writes_for(sid)
                       if a == fb.REG_ANGULAR_RESOLUTION]
            self.assertEqual(written, [bytes([1])])

    def test_angular_resolution_already_correct_is_not_rewritten(self):
        # Endurance: read-compare-SKIP. EEPROM is a five-figure budget and every
        # provisioned arm re-run would otherwise burn a cycle on all 7 servos.
        bus = _ProvFakeBus({sid: 2048 for sid in range(1, 8)})
        for sid in range(1, 8):
            bus.mem[sid][fb.REG_ANGULAR_RESOLUTION] = \
                fb.ANGULAR_RESOLUTION_SINGLE_TURN
        self.prov.provision(bus, 'EDU6-0002', (1,) * 7)
        for sid in range(1, 8):
            self.assertNotIn(fb.REG_ANGULAR_RESOLUTION,
                             [a for a, _ in bus.writes_for(sid)])

    def test_the_record_carries_resolution_and_the_shared_firmware_spelling(self):
        bus = _ProvFakeBus({sid: 2048 for sid in range(1, 8)})
        record = self.prov.provision(bus, 'EDU6-0003', (1,) * 7)
        for entry in record['servos']:
            self.assertEqual(entry['angular_resolution'], 1)
            # _ProvFakeBus seeds firmware 3.9; the ONE shared formatter must keep
            # producing the same spelling the record has always used.
            self.assertEqual(entry['firmware'], '3.9')
            self.assertEqual(entry['firmware'], fb.format_firmware(3, 9))

    def test_phase_bit4_not_touched_when_clear(self):
        # Phase bit-4 read-modify-write is only-when-set.
        bus = _ProvFakeBus({sid: 2048 + sid for sid in range(1, 8)}, phase=0x00)
        self.prov.provision(bus, 'EDU6-0002', (1,) * 7)
        for sid in range(1, 8):
            self.assertNotIn(fb.REG_PHASE, [a for a, _ in bus.writes_for(sid)])

    def test_write_verify_skips_when_already_correct(self):
        # Endurance: read-compare-SKIP — a register already holding the target
        # is NOT rewritten (EEPROM endurance is a five-figure budget).
        bus = _ProvFakeBus({1: 2048})
        bus.mem[1][100] = 5
        self.assertFalse(self.prov._write_verify_u8(bus, 1, 100, 5, 'x'))
        self.assertEqual(bus.writes, [])
        self.assertTrue(self.prov._write_verify_u8(bus, 1, 100, 9, 'x'))
        self.assertIn((1, 100, b'\x09'), bus.writes)

    def test_verify_mismatch_raises_systemexit(self):
        class _StaleBus(_ProvFakeBus):
            def write(self, sid, addr, data, await_status=True):
                self.writes.append((sid, addr, bytes(data)))
                return 0   # swallow → readback returns the OLD value → mismatch
        bus = _StaleBus({1: 2048})
        with self.assertRaises(SystemExit):
            self.prov._write_verify_u16(bus, 1, fb.REG_MAX_TORQUE_LIMIT, 800, 'x')

    def test_offset_wrap_near_encoder_boundary(self):
        # Bench 2026-07-24: J5 jigged with raw tick 23 (near the 0 boundary)
        # under a legacy +85 offset reads Present 4034; the naive
        # `present + current_off` = 4119 computes a +2071 offset that
        # false-overflows. The mod-4096 fix must recover raw 23 → offset −2025
        # and provision cleanly, then re-read the jig pose as the designed tick.
        bus = _ProvFakeBus({sid: 23 for sid in range(1, 8)})
        # seed the legacy +85 homing offset the bench arm actually carried
        for sid in range(1, 8):
            off = fb.encode_sign_magnitude(85, 11)
            bus.mem[sid][fb.REG_HOMING_OFFSET] = off & 0xFF
            bus.mem[sid][fb.REG_HOMING_OFFSET + 1] = (off >> 8) & 0xFF
        # sanity: the pre-provision Present is the WRAPPED 4034, not 23−85
        self.assertEqual(bus._present_raw(1), (23 - 85) % 4096)
        record = self.prov.provision(bus, 'EDU6-WRAP', (1,) * 7)
        by_id = {e['id']: e for e in record['servos']}
        self.assertEqual(by_id[1]['homing_offset'], 23 - 2048)   # −2025
        # After the write the still-jigged pose reads the designed centre tick.
        self.assertEqual(bus._present_raw(1), 2048)

    def test_jig_postcondition_catches_offset_skew(self):
        # The new §8(a) guard: after the offset write, the still-jigged arm must
        # READ the designed tick. A wrong-sign offset (the silent 2×-skew) passes
        # the register read-back but doubles Present_Position — caught here.
        class _SkewBus(_ProvFakeBus):
            def _present_raw(self, sid):
                return self.actual[sid] + self._homing_offset(sid)  # WRONG sign
        bus = _SkewBus({sid: 2148 for sid in range(1, 8)})
        with self.assertRaises(SystemExit) as ctx:
            self.prov.provision(bus, 'EDU6-0003', (1,) * 7)
        self.assertIn('Jig-Position', str(ctx.exception))

    def test_mixed_sts3250_arm_records_actual_models(self):
        # The edu6 arm mixes STS3215 (joints 1/4/5/6/7) and STS3250 (joints
        # 2/3) BY DESIGN — provisioning must ACCEPT both and record each
        # servo's ACTUAL model (bench-confirmed 2026-07-24), never hardcode 777.
        models = {sid: (fb.STS3250_MODEL_NUMBER if sid in (2, 3)
                        else fb.STS3215_MODEL_NUMBER) for sid in range(1, 8)}
        bus = _ProvFakeBus({sid: 2048 for sid in range(1, 8)}, models=models)
        record = self.prov.provision(bus, 'EDU6-MIX', (1,) * 7)
        by_id = {e['id']: e for e in record['servos']}
        self.assertEqual(by_id[2]['model_number'], fb.STS3250_MODEL_NUMBER)
        self.assertEqual(by_id[2]['model_name'], 'STS3250')
        self.assertEqual(by_id[3]['model_number'], fb.STS3250_MODEL_NUMBER)
        self.assertEqual(by_id[1]['model_number'], fb.STS3215_MODEL_NUMBER)
        self.assertEqual(by_id[7]['model_name'], 'STS3215')

    def test_unknown_model_refused(self):
        bus = _ProvFakeBus({sid: 2048 for sid in range(1, 8)}, model=1234)
        with self.assertRaises(SystemExit) as ctx:
            self.prov.provision(bus, 'EDU6-BAD', (1,) * 7)
        self.assertIn('Modell 1234', str(ctx.exception))

    def test_wheel_mode_is_normalized_to_position(self):
        # Audit H2: a servo left in wheel mode passes every other check and
        # turns boot-home into a continuous-rotation runaway — provisioning
        # must leave it write-verified in position mode.
        bus = _ProvFakeBus({sid: 2048 for sid in range(1, 8)}, mode=1)
        self.prov.provision(bus, 'EDU6-0005', (1,) * 7)
        for sid in range(1, 8):
            self.assertIn((fb.REG_OPERATING_MODE, b'\x00'),
                          bus.writes_for(sid))
            self.assertEqual(bus.mem[sid][fb.REG_OPERATING_MODE], 0)

    def test_response_status_level_normalized_first(self):
        # Research item 6: a level-0 servo acks no WRITE — the repair (unlock
        # + level, un-awaited) must be the FIRST write pair, before any
        # awaited write could time out against a mute servo.
        bus = _ProvFakeBus({sid: 2048 for sid in range(1, 8)},
                           response_level=0)
        self.prov.provision(bus, 'EDU6-0006', (1,) * 7)
        for sid in range(1, 8):
            w = bus.writes_for(sid)
            self.assertEqual(w[0], (fb.REG_LOCK, b'\x00'))
            self.assertEqual(w[1], (fb.REG_RESPONSE_STATUS_LEVEL, b'\x01'))
            self.assertEqual(bus.mem[sid][fb.REG_RESPONSE_STATUS_LEVEL], 1)

    def test_final_jig_recheck_catches_late_skew(self):
        # Audit M1: a position-semantics shift that appears only AFTER the
        # per-servo offset gate (modelled: every read skews once the LAST
        # re-lock lands) must fail the run at the final all-servo jig
        # re-verify — never persist a skewed record.
        class _LateSkewBus(_ProvFakeBus):
            skew = False

            def write(self, sid, addr, data, await_status=True):
                out = super().write(sid, addr, data, await_status)
                if sid == 7 and addr == fb.REG_LOCK and data == b'\x01':
                    self.skew = True
                return out

            def _present_raw(self, sid):
                raw = super()._present_raw(sid)
                return raw + 10 if self.skew else raw
        bus = _LateSkewBus({sid: 2048 for sid in range(1, 8)})
        with self.assertRaises(SystemExit) as ctx:
            self.prov.provision(bus, 'EDU6-0007', (1,) * 7)
        self.assertIn('Jig-Endkontrolle', str(ctx.exception))

    def test_limits_to_ticks_delegates_to_shared_window(self):
        # Audit H1: provision writes and the driver probe verifies via ONE
        # implementation — the wrapper must be byte-identical to it.
        for signs in ((1,) * 7, (1, -1, 1, 1, -1, 1, 1)):
            for i in range(7):
                self.assertEqual(
                    self.prov.limits_to_ticks(i, signs),
                    fb.position_limit_window(
                        *self.prov.JOINT_LIMITS_RAD[i], signs[i]))

    def test_no_record_when_a_servo_fails(self):
        # A verify mismatch on servo 4 aborts BEFORE servos 5-7 are touched and
        # returns no record — a half-written arm must fail loudly, not persist.
        class _FailServo4(_ProvFakeBus):
            def write(self, sid, addr, data, await_status=True):
                self.writes.append((sid, addr, bytes(data)))
                if sid == 4 and addr == fb.REG_MAX_TORQUE_LIMIT:
                    return 0   # swallow → readback mismatch → SystemExit
                self.mem[sid][addr:addr + len(data)] = data
                return 0
        bus = _FailServo4({sid: 2048 + sid for sid in range(1, 8)})
        with self.assertRaises(SystemExit):
            self.prov.provision(bus, 'EDU6-0004', (1,) * 7)
        touched = {sid for sid, _a, _d in bus.writes}
        self.assertFalse({5, 6, 7} & touched)


class TestIdentifyArmFeetech(unittest.TestCase):
    """identify_arm.py --protocol feetech token contract (finding 1a): a silent
    bus (port open, ZERO servos) returns the DISTINCT 'feetech_silent', not a
    bare 'unknown', so the GUI can name the 12-V-supply cause."""

    @classmethod
    def setUpClass(cls):
        # identify_arm.py imports dynamixel_sdk at module top — stub it so the
        # deps-free suite can import it (only the feetech path is exercised).
        fake = types.ModuleType('dynamixel_sdk')
        fake.PacketHandler = object
        fake.PortHandler = object
        sys.modules.setdefault('dynamixel_sdk', fake)
        import importlib.util
        path = os.path.join(_DOCKER, 'identify_arm.py')
        spec = importlib.util.spec_from_file_location('identify_arm', path)
        cls.ia = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.ia)

    def _with_bus(self, bus_cls, port='/dev/fake'):
        orig = fb.FeetechBus
        fb.FeetechBus = bus_cls
        try:
            return self.ia.identify_feetech(port)
        finally:
            fb.FeetechBus = orig

    def test_silent_bus_returns_feetech_silent(self):
        class _SilentBus:
            def __init__(self, *a, **k):
                pass

            def ping(self, sid):
                return False

            def read_u16(self, sid, addr):
                raise fb.FeetechBusError('unused')

            def close(self):
                pass
        self.assertEqual(self._with_bus(_SilentBus), 'feetech_silent')

    def test_partial_bus_returns_partial_token(self):
        class _PartialBus:
            def __init__(self, *a, **k):
                pass

            def ping(self, sid):
                return sid in (1, 2, 3)

            def read_u16(self, sid, addr):
                return fb.STS3215_MODEL_NUMBER

            def close(self):
                pass
        self.assertEqual(self._with_bus(_PartialBus), 'partial:3')

    def test_all_servos_model_777_returns_edu6(self):
        class _GoodBus:
            def __init__(self, *a, **k):
                pass

            def ping(self, sid):
                return True

            def read_u16(self, sid, addr):
                return fb.STS3215_MODEL_NUMBER

            def close(self):
                pass
        self.assertEqual(self._with_bus(_GoodBus), 'edu6')

    def test_mixed_sts3250_returns_edu6(self):
        # Joints 2/3 report STS3250 (2825) by design — scan must still identify
        # the arm as edu6, not reject it as a wrong device.
        class _MixedBus:
            def __init__(self, *a, **k):
                pass

            def ping(self, sid):
                return True

            def read_u16(self, sid, addr):
                return (fb.STS3250_MODEL_NUMBER if sid in (2, 3)
                        else fb.STS3215_MODEL_NUMBER)

            def close(self):
                pass
        self.assertEqual(self._with_bus(_MixedBus), 'edu6')

    def test_foreign_model_returns_unknown(self):
        class _ForeignBus:
            def __init__(self, *a, **k):
                pass

            def ping(self, sid):
                return True

            def read_u16(self, sid, addr):
                return 1234    # neither STS3215 nor STS3250

            def close(self):
                pass
        self.assertEqual(self._with_bus(_ForeignBus), 'unknown')


class TestScanFeetechSilentMapping(unittest.TestCase):
    """device_manager maps the feetech_silent token to the 12-V German notice
    (finding 1b) — the token contract is only useful if the GUI surfaces it."""

    def test_feetech_silent_sets_scan_notice(self):
        sys.path.insert(0, os.path.join(_HERE, '..'))
        from gui.app import device_manager as dm

        dev = types.SimpleNamespace(busid='1-1', description='CH343',
                                    serial_path='/dev/serial/by-id/x')
        with patch.object(dm, 'self_heal_wsl_serial'), \
                patch.object(dm, 'attach_all_robotis_devices',
                             return_value=[dev]), \
                patch.object(dm, 'find_serial_paths_for_arms',
                             return_value=['/dev/serial/by-id/x']), \
                patch.object(dm, 'start_scanner_container', return_value=True), \
                patch.object(dm, 'stop_scanner_container'), \
                patch.object(dm, 'identify_arm_via_docker',
                             return_value='feetech_silent'), \
                patch('time.sleep'):
            leader, follower = dm.scan_and_identify_arms('img', arm_family='edu6')
        self.assertIsNone(leader)
        self.assertIsNone(follower)
        self.assertIn('12-V-Netzteil', dm.LAST_SCAN_NOTICE)
        self.assertIn('Servo', dm.LAST_SCAN_NOTICE)


class TestScanCrossFamilyAndPartial(unittest.TestCase):
    """Audit M4: the family-scoped attach means a wrong-family arm never
    reaches identify_arm.py — the cross-family hint must therefore fire from
    pure Windows-side presence when the family scan found nothing. Plus
    audit L3: partial:N names the answering-servo count."""

    def setUp(self):
        sys.path.insert(0, os.path.join(_HERE, '..'))

    def test_empty_scan_sets_cross_family_presence_notice(self):
        from gui.app import device_manager as dm
        other = types.SimpleNamespace(vid_pid='2F5D:0103', busid='1-2',
                                      description='OpenRB',
                                      state='Shared')
        with patch.object(dm, 'self_heal_wsl_serial'), \
                patch.object(dm, 'attach_all_robotis_devices',
                             return_value=[]), \
                patch.object(dm, 'list_arm_devices',
                             return_value=[other]) as lad:
            leader, follower = dm.scan_and_identify_arms(
                'img', arm_family='edu6')
        self.assertIsNone(leader)
        self.assertIsNone(follower)
        lad.assert_called_once_with('omx')
        self.assertIn('OMX-Arm', dm.LAST_SCAN_NOTICE)

    def test_empty_scan_other_direction(self):
        from gui.app import device_manager as dm
        ch343 = types.SimpleNamespace(vid_pid='1A86:55D3', busid='1-3',
                                      description='CH343',
                                      state='Shared')
        with patch.object(dm, 'self_heal_wsl_serial'), \
                patch.object(dm, 'attach_all_robotis_devices',
                             return_value=[]), \
                patch.object(dm, 'list_arm_devices',
                             return_value=[ch343]) as lad:
            dm.scan_and_identify_arms('img', arm_family='omx')
        lad.assert_called_once_with('edu6')
        self.assertIn('EduBotics 6-Achs', dm.LAST_SCAN_NOTICE)

    def test_empty_scan_without_other_family_stays_silent(self):
        from gui.app import device_manager as dm
        with patch.object(dm, 'self_heal_wsl_serial'), \
                patch.object(dm, 'attach_all_robotis_devices',
                             return_value=[]), \
                patch.object(dm, 'list_arm_devices', return_value=[]):
            dm.scan_and_identify_arms('img', arm_family='omx')
        self.assertEqual(dm.LAST_SCAN_NOTICE, '')

    def test_partial_token_names_the_count(self):
        from gui.app import device_manager as dm
        dev = types.SimpleNamespace(busid='1-1', description='CH343',
                                    serial_path='/dev/serial/by-id/x')
        with patch.object(dm, 'self_heal_wsl_serial'), \
                patch.object(dm, 'attach_all_robotis_devices',
                             return_value=[dev]), \
                patch.object(dm, 'find_serial_paths_for_arms',
                             return_value=['/dev/serial/by-id/x']), \
                patch.object(dm, 'start_scanner_container',
                             return_value=True), \
                patch.object(dm, 'stop_scanner_container'), \
                patch.object(dm, 'identify_arm_via_docker',
                             return_value='partial:3'), \
                patch('time.sleep'):
            leader, follower = dm.scan_and_identify_arms(
                'img', arm_family='edu6')
        self.assertIsNone(follower)
        self.assertIn('3 von 7', dm.LAST_SCAN_NOTICE)
        self.assertIn('Steckverbindungen', dm.LAST_SCAN_NOTICE)


class TestFamilyVidProbe(unittest.TestCase):
    """The family-aware Windows VID probe filter (finding 1c) — pure, so it is
    testable off-Windows without a real Get-PnpDevice call."""

    def test_pnp_filter_per_family(self):
        sys.path.insert(0, os.path.join(_HERE, '..'))
        from gui.app import device_manager as dm
        omx = dm._arm_vid_pnp_filter('omx')
        edu6 = dm._arm_vid_pnp_filter('edu6')
        self.assertIn('VID_2F5D', omx)
        self.assertNotIn('PID_', omx)            # omx = any PID under 2F5D
        self.assertIn('VID_1A86', edu6)
        self.assertIn('PID_55D3', edu6)          # edu6 is PID-pinned
        # An unknown family falls back to omx (never crashes the diagnose flow).
        self.assertEqual(dm._arm_vid_pnp_filter('nope'), omx)

    def test_diagnose_text_names_family_hardware(self):
        sys.path.insert(0, os.path.join(_HERE, '..'))
        from gui.app import device_manager as dm
        self.assertIn('EduBotics 6-Achs', dm._FAMILY_DIAG_TEXT['edu6']['hw'])
        self.assertIn('2F5D', dm._FAMILY_DIAG_TEXT['omx']['vid'])
        self.assertIn('12-V', dm._FAMILY_DIAG_TEXT['edu6']['bullets'])


class TestCommandedGoalEdgeClamp(unittest.TestCase):
    """The commanded-goal edge clamp (Rule §2 sign-off, 2026-07-26): every tick
    written as ``Goal_Position`` is kept clear of the tick-0/4095 position-map
    edge, because the STS position loop is NOT wrap-aware and six of the twelve
    arm-joint limit bounds land EXACTLY on that edge — so a command AT one of them
    pinned the goal on the seam, where one encoder sample across the boundary
    commits the joint to a full revolution and NEITHER torque-on guard fires."""

    # ── the premise: which bounds are actually on the seam ─────────────────
    def test_six_of_the_twelve_arm_bounds_land_exactly_on_a_register_end(self):
        # The whole reason this clamp exists. Derived from the SHIPPED limits +
        # signs, not restated — a limits edit that moved a bound off the seam (or
        # onto one) has to update this list consciously.
        on_edge = []
        for i, (lo, hi) in enumerate(_N['JOINT_LIMITS_RAD']):
            for which, rad in (('lo', lo), ('hi', hi)):
                tick = _N['rad_to_tick'](rad, _N['JOINT_SIGNS'][i])
                if tick in (0, 4095):
                    on_edge.append((i + 1, which, tick))
        self.assertEqual(on_edge, [
            (2, 'hi', 4095),                       # shoulder at +180°
            (3, 'lo', 0),                          # elbow at −180°
            (4, 'lo', 0), (4, 'hi', 4095),         # roll: BOTH ends
            (6, 'lo', 0), (6, 'hi', 4095),         # roll: BOTH ends
        ])
        # …and the gripper's two bounds are the 13th/14th: "six of twelve" counts
        # ARM joints. Nothing about the gripper is near the seam.
        g_lo, g_hi = _N['JOINT_LIMITS_RAD'][6]
        self.assertEqual((_N['rad_to_tick'](g_lo, 1), _N['rad_to_tick'](g_hi, 1)),
                         (2048, 3215))

    def test_a_full_circle_joint_subset_would_have_missed_two_of_them(self):
        # Guard against re-deriving a `full_circle_joint_indexes()`-style
        # predicate: joint2's hi and joint3's lo are exactly as pinned as
        # joint4/joint6 despite their windows NOT being the full register. This is
        # the same false premise the original predicate was deleted for.
        full_circle = [i + 1 for i, (lo, hi) in enumerate(_N['JOINT_LIMITS_RAD'])
                       if fb.position_limit_window(lo, hi, 1) == (0, 4095)]
        self.assertEqual(full_circle, [4, 6])
        for jid, rad in ((2, 3.1416), (3, -3.1416)):
            self.assertNotIn(jid, full_circle)
            self.assertIn(_N['rad_to_tick'](rad, 1), (0, 4095))

    # ── the clamp itself ───────────────────────────────────────────────────
    def test_clamp_moves_every_seam_bound_and_touches_nothing_else(self):
        m = _N['GOAL_EDGE_MARGIN_TICKS']
        moved, kept = [], []
        for i, (lo, hi) in enumerate(_N['JOINT_LIMITS_RAD']):
            sign = _N['JOINT_SIGNS'][i]
            for which, rad in (('lo', lo), ('hi', hi)):
                plain = _N['rad_to_tick'](rad, sign)
                cmd = _N['rad_to_command_tick'](rad, sign)
                # THE invariant: no commanded tick is ever within `m` of an end.
                self.assertGreaterEqual(cmd, m, (i + 1, which))
                self.assertLessEqual(cmd, 4095 - m, (i + 1, which))
                (moved if cmd != plain else kept).append((i + 1, which))
        # It bites exactly the six seam bounds and nothing else.
        self.assertEqual(moved, [(2, 'hi'), (3, 'lo'),
                                 (4, 'lo'), (4, 'hi'),
                                 (6, 'lo'), (6, 'hi')])
        self.assertEqual(len(kept), 8)

    def test_clamp_is_a_noop_across_every_joints_ordinary_travel(self):
        # The COST bound. A margin wide enough to touch joint1/joint5/the gripper
        # anywhere in their designed travel, or to touch the ±90° joint6 fold, or
        # HOME, would fail here — that is what pins the default's blast radius.
        m = _N['GOAL_EDGE_MARGIN_TICKS']
        for i, (lo, hi) in enumerate(_N['JOINT_LIMITS_RAD']):
            if i in (1, 2, 3, 5):
                continue                   # the four joints with a seam bound
            steps = 400
            for k in range(steps + 1):
                rad = lo + (hi - lo) * k / steps
                self.assertEqual(_N['rad_to_command_tick'](rad, _N['JOINT_SIGNS'][i]),
                                 _N['rad_to_tick'](rad, _N['JOINT_SIGNS'][i]),
                                 (i + 1, rad))
        # joint6 folded into ±90° (autonomous grasping) — ticks 1024…3072.
        for k in range(-90, 91):
            rad = math.radians(k)
            self.assertEqual(_N['rad_to_command_tick'](rad, 1),
                             _N['rad_to_tick'](rad, 1), k)
        # HOME + open gripper, and the fenced-maximum margin still misses it.
        home = list(_N['HOME_JOINTS_RAD']) + [_N['GRIPPER_OPEN_RAD']]
        worst = min(min(t, 4095 - t) for t in
                    (_N['rad_to_tick'](r, 1) for r in home))
        self.assertEqual(worst, 483)                     # joint3 at −2.40 rad
        self.assertGreater(worst, _N['_GOAL_EDGE_MARGIN_MAX_TICKS'])
        self.assertGreater(worst, m)

    def test_autonomous_grasping_is_untouched_at_any_legal_margin(self):
        """THIS TEST CANNOT VALIDATE THE CEILING'S INPUT NUMBER — read on.

        The ceiling exists to keep every real ``Edu6IKSolver`` solution outside
        the clamp band, so the number it must sit under is a property of the
        SOLVER. The solver needs numpy and lives in
        ``physical_ai_tools/physical_ai_server/``; this suite is deliberately
        deps-free, which is why the previous version of this test recomputed the
        bound from a HARDCODED joint angle (``157.52°``) and asserted the
        constant matched. That made it a constant-drift detector only: when the
        input number turned out to be the third successive sweep artifact
        (367 → 263 → 256, all from grids that under-resolve full extension), this
        test stayed green through all of it.

        The number is now derived analytically and guarded where the solver
        lives:
        ``physical_ai_tools/physical_ai_server/test/test_edu6_goal_edge_ceiling.py``
        ast-extracts THIS constant, derives joint3's floor from the solver's own
        ``_ALPHA0``, and bisects the reach boundary with the real ``solve()``.
        What is pinned HERE is the literal and the clamp's BEHAVIOUR at it."""
        ceiling = _N['_GOAL_EDGE_MARGIN_MAX_TICKS']
        # One tick under the 247-tick analytic floor of q3 = γ + ALPHA0 − π/2.
        self.assertEqual(ceiling, 246)
        f = _N['rad_to_command_tick']
        floor_tick = ceiling + 1
        rad = _N['tick_to_rad'](floor_tick, 1)
        # At the ceiling — and at the shipped default — the tightest tick a real
        # solution can carry is commanded unchanged, and STRICTLY inside the band
        # (that strictness is why the ceiling is 246 and not 247: at margin 247
        # the tick would still survive, but only because the band boundary is
        # inclusive, and a fence should not lean on that).
        for margin in (_N['GOAL_EDGE_MARGIN_TICKS'], ceiling):
            self.assertLessEqual(margin, ceiling, margin)
            self.assertEqual(f(rad, 1, margin), floor_tick, margin)
            self.assertGreater(floor_tick, margin, margin)
        # The first margin that actually SHIFTS the floor tick is floor + 1 —
        # two above the ceiling. That headroom is the point of the fence.
        self.assertEqual(f(rad, 1, floor_tick), floor_tick)
        self.assertEqual(f(rad, 1, floor_tick + 1), floor_tick + 1)

    def test_clamp_survives_a_sign_flip_without_a_per_joint_predicate(self):
        # EDUBOTICS_EDU6_JOINT_SIGNS=−1 on joint2 mirrors its window to (0, 2048),
        # moving the pinned bound from hi to lo. The blanket clamp follows for
        # free; a per-joint table would have to be re-derived.
        lo, hi = _N['JOINT_LIMITS_RAD'][1]
        self.assertEqual(fb.position_limit_window(lo, hi, -1), (0, 2048))
        self.assertEqual(_N['rad_to_tick'](hi, -1), 0)          # now the lo end
        m = _N['GOAL_EDGE_MARGIN_TICKS']
        self.assertEqual(_N['rad_to_command_tick'](hi, -1), m)

    def test_margin_zero_is_the_documented_rollback(self):
        for rad, sign in ((3.1416, 1), (-3.1416, 1), (3.1416, -1), (10.0, 1)):
            self.assertEqual(_N['rad_to_command_tick'](rad, sign, 0),
                             _N['rad_to_tick'](rad, sign))

    def test_rad_to_tick_itself_is_still_only_register_clamped(self):
        # rad_to_tick must stay a plain coordinate map: fb.le16 of an out-of-range
        # tick would silently encode a different number, so that clamp is its own
        # unconditional invariant — and a future NON-command caller must not
        # inherit a safety clamp meant for commands.
        self.assertEqual(_N['rad_to_tick'](10.0, 1), 4095)
        self.assertEqual(_N['rad_to_tick'](-10.0, 1), 0)
        self.assertEqual(_N['rad_to_tick'](3.1416, 1), 4095)

    # ── the residual, measured against the SHIPPED wrong-way predicate ─────
    def test_the_clamp_raises_the_forced_displacement_from_one_tick_to_margin_plus_one(self):
        """THE correctness claim. With the goal pinned ON the seam a single tick of
        motion across the boundary makes the servo's own naive error take the long
        way round; with the goal clamped it takes margin + 1 ticks of displacement
        PAST the goal. Driven through the shipped wrong_way_joints, not a
        re-derivation of it."""
        wrong = _N['wrong_way_joints']

        def min_forced(goal):
            """Smallest wrapped displacement from ``goal`` that trips the abort."""
            for d in range(1, 2049):
                for p in ((goal + d) % 4096, (goal - d) % 4096):
                    if wrong({1: goal}, {1: p}):
                        return d
            return None

        # Today's pinned goals — a hair trigger at both ends of the register.
        self.assertEqual(min_forced(4095), 1)
        self.assertEqual(min_forced(0), 1)
        # The clamped band's extremes, derived from the clamp itself over the real
        # limits (NOT hardcoded), are what the arm can actually command.
        m = _N['GOAL_EDGE_MARGIN_TICKS']
        commandable = set()
        for i, (lo, hi) in enumerate(_N['JOINT_LIMITS_RAD']):
            sign = _N['JOINT_SIGNS'][i]
            for k in range(801):
                commandable.add(_N['rad_to_command_tick'](
                    lo + (hi - lo) * k / 800, sign))
        self.assertEqual(min(commandable), m)
        self.assertEqual(max(commandable), 4095 - m)
        self.assertEqual(min_forced(min(commandable)), m + 1)
        self.assertEqual(min_forced(max(commandable)), m + 1)
        # No commandable goal is worse than those two extremes: nothing within
        # `m` ticks of ANY commandable goal can trip the abort. (Sampled with a
        # stride through the interior, exhaustive over the outer bands where the
        # bound actually binds.)
        probes = (list(range(m, m + 200)) + list(range(4095 - m - 199, 4096 - m))
                  + list(range(m, 4096 - m, 97)))
        for goal in probes:
            for d in range(1, m + 1):
                for p in ((goal + d) % 4096, (goal - d) % 4096):
                    self.assertFalse(wrong({1: goal}, {1: p}), (goal, d, p))

    def test_the_eeprom_window_cannot_substitute_for_the_clamp(self):
        # Asked directly at review: does Min/Max_Position_Limit bound this? No —
        # every joint carrying a seam bound has a window that TOUCHES a register
        # end, so there is no headroom to clamp into even if the register limit
        # bounded the physical path (it does not; it bounds the GOAL).
        for idx in (1, 2, 3, 5):
            lo, hi = fb.position_limit_window(*_N['JOINT_LIMITS_RAD'][idx], 1)
            self.assertTrue(lo == 0 or hi == 4095, (idx + 1, lo, hi))

    # ── the call site ──────────────────────────────────────────────────────
    def _written_ticks(self, q):
        bus = _TorqueFakeBus()
        node = _TorqueStubNode(bus)
        node._write_targets(q)
        addr, per = bus.writes[-1]
        self.assertEqual(addr, fb.REG_ACCELERATION)
        # 7-byte contiguous write: accel, goal(2), time(2), speed(2).
        return {sid: fb.from_le16(p[1], p[2]) for sid, p in per.items()}

    def test_write_targets_never_commands_a_seam_tick(self):
        # BEHAVIOUR at the call site — this is what catches a bare rad_to_tick
        # there. Command every joint to BOTH of its URDF limits at once.
        m = _N['GOAL_EDGE_MARGIN_TICKS']
        for pick_hi in (False, True):
            q = [(hi if pick_hi else lo)
                 for lo, hi in _N['JOINT_LIMITS_RAD']]
            ticks = self._written_ticks(q)
            self.assertEqual(len(ticks), 7)
            for sid, tick in ticks.items():
                self.assertGreaterEqual(tick, m, (pick_hi, sid, tick))
                self.assertLessEqual(tick, 4095 - m, (pick_hi, sid, tick))
        # …and the radian clamp above it still holds: a wildly out-of-limit
        # request lands on the clamped LIMIT, not somewhere arbitrary.
        ticks = self._written_ticks([99.0] * 7)
        self.assertEqual(ticks[4], 4095 - m)             # joint4 hi = +π
        self.assertEqual(ticks[7], 3215)                 # gripper hi: unclamped
        ticks = self._written_ticks([-99.0] * 7)
        self.assertEqual(ticks[6], m)                    # joint6 lo = −π
        self.assertEqual(ticks[7], 2048)                 # gripper lo: unclamped

    def test_write_targets_passes_ordinary_targets_through_bit_identically(self):
        home = list(_N['HOME_JOINTS_RAD']) + [_N['GRIPPER_OPEN_RAD']]
        ticks = self._written_ticks(home)
        self.assertEqual(
            ticks,
            {sid: _N['rad_to_tick'](home[i], _N['JOINT_SIGNS'][i])
             for i, sid in enumerate(_N['SERVO_IDS'])})
        self.assertEqual(ticks[3], 483)                  # joint3 at HOME

    def test_every_boot_home_point_is_commanded_unchanged(self):
        # The boot-home glide's HOME endpoint is 483 ticks clear, so a glide from
        # any pose the plausibility band admits arrives unclamped. (Its FIRST
        # points can be clamped when a joint was hand-parked inside the band —
        # that is the documented, direction-safe jerk, not a silent failure.)
        for start_tick in (2048, 1024, 3072, 600, 3500):
            rad = _N['tick_to_rad'](start_tick, 1)
            pts = _N['build_boot_home']([rad] * 7)
            for q, _t in pts:
                for i, sign in enumerate(_N['JOINT_SIGNS']):
                    plain = _N['rad_to_tick'](q[i], sign)
                    self.assertEqual(_N['rad_to_command_tick'](q[i], sign),
                                     plain, (start_tick, i))

    def test_the_goal_seed_is_deliberately_not_edge_clamped(self):
        # A software guard must not CREATE motion. The seed writes Goal = Present
        # to hold a limp arm still, so clamping it would command a real move at the
        # instant torque arrives — and the two torque-on guards already own that
        # state. Present tick 4090 on joint6, judged on an already-torqued arm so
        # the OFF→ON-gated edge guard does not refuse first.
        bus = _TorqueFakeBus({6: 4090})
        node = _TorqueStubNode(bus)
        node._torque_on = True                    # skip the edge guard's gate
        with node._bus_lock:
            goals = node._seed_goal_from_present_locked()
        self.assertIsNotNone(goals)
        self.assertEqual(goals[6], 4090)          # NOT 4095 − margin
        addr, per = bus.writes[-1]
        self.assertEqual(addr, fb.REG_ACCELERATION)
        self.assertEqual(fb.from_le16(per[6][1], per[6][2]), 4090)
        # …and the seed keeps its own gentle speed, not the motion cap.
        self.assertEqual(fb.from_le16(per[6][5], per[6][6]),
                         _N['SEED_SPEED_STEPS'])

    def test_only_the_command_writer_converts_radians_to_a_goal_tick(self):
        # Structural belt for the behaviour above: rad_to_tick has exactly ONE
        # caller besides rad_to_command_tick's own body, and it is not a goal
        # writer. A future command path that reached for the bare converter would
        # break this.
        path = os.path.join(_DOCKER, 'edu6_arm_node.py')
        source = open(path, encoding='utf-8').read()
        tree = ast.parse(source)
        callers = {}
        for parent in ast.walk(tree):
            if not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(parent):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id in ('rad_to_tick',
                                            'rad_to_command_tick')):
                    callers.setdefault(sub.func.id, set()).add(parent.name)
        self.assertEqual(callers['rad_to_tick'], {'rad_to_command_tick'})
        self.assertEqual(callers['rad_to_command_tick'], {'_write_targets'})
        # The seed must not acquire the clamp by a later edit. (Split on the
        # INDENTED def so a mention of the method's name in a module-level
        # docstring cannot be mistaken for its body.)
        seed = source.split('\n    def _seed_goal_from_present_locked', 1)[1]
        seed = seed.split('\n    def ', 1)[0]
        self.assertIn('sync_write', seed)                 # we really got the body
        # A CALL, not a mention: the seed's own comment legitimately discusses
        # rad_to_tick when explaining why commanded travel is monotonic.
        self.assertNotIn('rad_to_command_tick(', seed)
        self.assertNotIn('= rad_to_tick(', seed)

    # ── env knob ───────────────────────────────────────────────────────────
    def test_goal_edge_margin_env_parse(self):
        f = _N['_parse_goal_edge_margin']
        default = _N['_DEFAULT_GOAL_EDGE_MARGIN_TICKS']
        ceiling = _N['_GOAL_EDGE_MARGIN_MAX_TICKS']
        self.assertEqual(default, 128)
        self.assertEqual(f(None), default)
        self.assertEqual(f(''), default)
        self.assertEqual(f('   '), default)
        self.assertEqual(f(' 200 '), 200)
        self.assertEqual(f(str(ceiling)), ceiling)      # the ceiling is INCLUSIVE
        self.assertEqual(f('0'), 0)                     # the documented rollback
        # BOTH ends fenced, each with a [WARN] naming 0 as the real disable value.
        for bad in ('abc', '-1', '1,2', str(ceiling + 1), '2048', '4096'):
            with patch('builtins.print') as printed:
                self.assertEqual(f(bad), default, bad)
            self.assertTrue(printed.called, bad)
            msg = printed.call_args[0][0]
            self.assertIn('EDUBOTICS_EDU6_GOAL_EDGE_MARGIN_TICKS', msg)
        with patch('builtins.print') as printed:
            f(str(ceiling + 1))
        self.assertIn('0 to DISABLE', printed.call_args[0][0])

    def test_the_two_edge_knobs_are_independent_and_the_wider_one_is_the_clamp(self):
        # They are separate mechanisms with separate rollbacks, and the clamp is
        # deliberately the wider of the two: the edge guard judges an arm AT REST
        # (dither only), the clamp a MOVING joint arriving at a pinned goal, which
        # must also survive an overshoot nobody has measured yet (rig gate R5).
        self.assertGreater(_N['GOAL_EDGE_MARGIN_TICKS'],
                           _N['EDGE_REFUSE_MARGIN_TICKS'])
        # Disabling one must not disable the other.
        self.assertEqual(_N['edge_parked_joints']({6: 4093}, 0), [])
        self.assertEqual(_N['rad_to_command_tick'](3.1416, 1, 0), 4095)
        self.assertTrue(_N['edge_parked_joints']({6: 4093}))
        self.assertNotEqual(_N['rad_to_command_tick'](3.1416, 1), 4095)

    def test_the_knob_is_forwarded_on_a_compose_environment_list(self):
        # ci.yml::env-forwarding-guard in miniature: an EDUBOTICS_* the driver
        # reads but compose never injects is an .env override that silently does
        # nothing.
        compose = open(os.path.join(_DOCKER, '..', 'docker-compose.yml'),
                       encoding='utf-8').read()
        # Mirror the guard's own regex: LINE-ANCHORED with only whitespace before
        # the dash, so a commented-out or prose mention cannot satisfy it.
        forwarded = re.findall(r'(?m)^[ \t]*-[ \t]*(EDUBOTICS_[A-Z0-9_]+)=',
                               compose)
        self.assertIn('EDUBOTICS_EDU6_GOAL_EDGE_MARGIN_TICKS', forwarded)
        # …and it really is the name the driver reads.
        driver = open(os.path.join(_DOCKER, 'edu6_arm_node.py'),
                      encoding='utf-8').read()
        read = set(re.findall(r'EDUBOTICS_[A-Z0-9_]+', driver))
        self.assertIn('EDUBOTICS_EDU6_GOAL_EDGE_MARGIN_TICKS', read)
        self.assertEqual(read - set(forwarded), set())


if __name__ == '__main__':
    unittest.main()

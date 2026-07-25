"""Deps-free tests for the clean-room Feetech STS3215 bus (edu6 PR 5).

Framing/checksum/sign-magnitude are pinned exactly — these are the wire rules
a transcription slip would corrupt silently. The serial layer is faked; the
node-level pure functions (tick conversion, trajectory interpolation,
boot-home) are tested via ast-extraction so importing them needs no rclpy.
"""

import ast
import os
import sys
import textwrap
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


# ── node pure functions (ast-extracted; no rclpy import) ─────────────────────

def _load_node_functions():
    path = os.path.join(_DOCKER, 'edu6_arm_node.py')
    source = open(path, encoding='utf-8').read()
    tree = ast.parse(source)
    ns = {
        'math': __import__('math'), 'os': os, 'fb': fb,
        'CENTER_TICK': 2048, 'TICKS_PER_REV': 4096,
    }
    # Deterministic JOINT_SIGNS regardless of the dev machine's env: the env
    # parse path itself is pinned by test_parse_signs.
    env_backup = os.environ.pop('EDUBOTICS_EDU6_JOINT_SIGNS', None)
    tol_backup = os.environ.pop('EDUBOTICS_EDU6_BOOT_POS_TOL_TICKS', None)
    wanted = {'rad_to_tick', 'tick_to_rad', 'interpolate_trajectory',
              'build_boot_home', '_parse_signs', 'boot_home_verify_decision',
              '_parse_boot_pos_tolerance'}
    consts = {'RAD_PER_TICK', 'HOME_JOINTS_RAD', 'GRIPPER_OPEN_RAD',
              'LOOP_HZ', 'BOOT_HOME_DURATION_S', 'SERVO_IDS', 'JOINT_NAMES',
              'JOINT_LIMITS_RAD', '_DEFAULT_SIGNS', 'JOINT_SIGNS',
              'BOOT_TORQUE_ON_ATTEMPTS', 'SEED_SPEED_STEPS',
              'PRESENT_LOAD_FULLSCALE',
              'GOAL_SPEED_CAP_STEPS', 'WRITE_ACCELERATION',
              '_DEFAULT_BOOT_POSITION_TOLERANCE_TICKS',
              'BOOT_POSITION_TOLERANCE_TICKS'}
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
                if isinstance(sub, ast.FunctionDef) and sub.name == 'probe_bus':
                    src = textwrap.dedent(ast.get_source_segment(source, sub))
                    exec(compile(src, path, 'exec'), ns)  # noqa: S102
    if env_backup is not None:
        os.environ['EDUBOTICS_EDU6_JOINT_SIGNS'] = env_backup
    if tol_backup is not None:
        os.environ['EDUBOTICS_EDU6_BOOT_POS_TOL_TICKS'] = tol_backup
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
                     phase=0, present=None):
            self.mode = mode
            self.phase = phase
            self.alive = set(alive if alive is not None else range(1, 8))
            self.models = models or {}
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

    def test_provisioned_arm_passes(self):
        ok, msg = self._probe()(self._RegBus())
        self.assertTrue(ok)
        self.assertEqual(msg, '')

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
        bus = self._RegBus(present={2: 4095 + tol - 1, 3: 2048, 5: 1024 - tol + 1})
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

    def test_full_circle_window_cannot_refuse(self):
        # DOCUMENTED no-op: joint4/joint6 keep [0,4095] under the keep-±180°
        # decision, so no reading is ever out of range there. Pinning it keeps
        # the limitation visible instead of implied.
        for sid in (4, 6):
            lo, hi = fb.position_limit_window(
                *_N['JOINT_LIMITS_RAD'][sid - 1], 1)
            self.assertEqual((lo, hi), (0, 4095))
            ok, msg = self._probe()(self._RegBus(present={sid: 0}))
            self.assertTrue(ok, msg)
            ok, msg = self._probe()(self._RegBus(present={sid: 4095}))
            self.assertTrue(ok, msg)


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
        self.assertIn('return False', seed)          # failed read aborts
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


if __name__ == '__main__':
    unittest.main()

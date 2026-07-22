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
    wanted = {'rad_to_tick', 'tick_to_rad', 'interpolate_trajectory',
              'build_boot_home', '_parse_signs'}
    consts = {'RAD_PER_TICK', 'HOME_JOINTS_RAD', 'GRIPPER_OPEN_RAD',
              'LOOP_HZ', 'BOOT_HOME_DURATION_S', 'SERVO_IDS', 'JOINT_NAMES',
              'JOINT_LIMITS_RAD', '_DEFAULT_SIGNS'}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in consts
                for t in node.targets):
            exec(compile(ast.Module([node], []), path, 'exec'), ns)  # noqa: S102
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            src = textwrap.dedent(ast.get_source_segment(source, node))
            exec(compile(src, path, 'exec'), ns)  # noqa: S102
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


if __name__ == '__main__':
    unittest.main()

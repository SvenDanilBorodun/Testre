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

    def __init__(self, actual_ticks, phase=0x10, return_delay=250, model=None):
        self.actual = dict(actual_ticks)
        self.writes = []          # (sid, addr, bytes) in call order
        self.closed = False
        self.mem = {}
        model = fb.STS3215_MODEL_NUMBER if model is None else model
        for sid in actual_ticks:
            m = bytearray(256)
            m[fb.REG_FIRMWARE_MAJOR] = 3
            m[fb.REG_FIRMWARE_MAJOR + 1] = 9
            m[fb.REG_MODEL_NUMBER] = model & 0xFF
            m[fb.REG_MODEL_NUMBER + 1] = (model >> 8) & 0xFF
            m[fb.REG_PHASE] = phase              # bit 4 set → provision clears it
            m[fb.REG_LOCK] = 1                   # protected → provision writes 0
            m[fb.REG_RETURN_DELAY] = return_delay
            self.mem[sid] = m

    def ping(self, sid, timeout_s=None):
        return sid in self.mem

    def _homing_offset(self, sid):
        return fb.decode_sign_magnitude(
            fb.from_le16(self.mem[sid][fb.REG_HOMING_OFFSET],
                         self.mem[sid][fb.REG_HOMING_OFFSET + 1]), 11)

    def _present_raw(self, sid):
        # Present = Actual − Homing_Offset (correct-sign model).
        return self.actual[sid] - self._homing_offset(sid)

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
            # Phase bit-4 was SET → exactly one read-modify-write cleared it.
            phase_writes = [d for a, d in w if a == fb.REG_PHASE]
            self.assertEqual(len(phase_writes), 1)
            self.assertEqual(phase_writes[0][0] & (1 << 4), 0)

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

"""Deps-free tests for ``feetech_servo_spec.py`` — the ONE V1 servo table the
driver, its boot probe, ``tools/feetech_provision.py`` and the Servo-Station
share.

Every number in ``TestDesignTables`` is the measured table of the build spec
(design §3): the centred encoder zero T0 per joint, the EEPROM windows and the
HOME ticks for all four tick-direction combinations. The HOME ticks and the
round trip run through the DRIVER's own tick map (ast-extracted: the driver
imports rclpy), so the table is produced by the shipped code, not restated.
"""

import ast
import math
import os
import re
import sys
import textwrap
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_DOCKER = os.path.join(_HERE, '..', 'docker', 'open_manipulator')
sys.path.insert(0, _DOCKER)

import feetech_bus as fb  # noqa: E402
import feetech_servo_spec as fss  # noqa: E402

_SPEC_PATH = os.path.join(_DOCKER, 'feetech_servo_spec.py')
_DRIVER_PATH = os.path.join(_DOCKER, 'feetech_arm_node.py')

# HOME + the claw open (the profile's open command), joint order.
_HOME = (0.0, 0.64, 1.48, 0.90, 0.0, 0.90)
_WINDOWS = ((1024, 3072), (1024, 3072), (1024, 3072), (796, 3300),
            (1024, 3072), (1536, 2560))
# {777, 2825} direction -> (signs, T0 = gauge reading, HOME ticks, HOME closest)
_TABLE = {
    (1, 1): ((-1, 1, -1, -1, -1, 1), (2048, 1024, 3072, 2048, 2048, 1536),
             (2048, 1441, 2107, 1461, 2048, 2123), 1441),
    (-1, -1): ((1, -1, 1, 1, 1, -1), (2048, 3072, 1024, 2048, 2048, 2560),
               (2048, 2655, 1989, 2635, 2048, 1973), 1440),
    (1, -1): ((-1, -1, 1, -1, -1, 1), (2048, 3072, 1024, 2048, 2048, 1536),
              (2048, 2655, 1989, 1461, 2048, 2123), 1440),
    (-1, 1): ((1, 1, -1, 1, 1, -1), (2048, 1024, 3072, 2048, 2048, 2560),
              (2048, 1441, 2107, 2635, 2048, 1973), 1441),
}


def _pinned(d777, d2825, **overrides):
    args = dict(factory=fss.FACTORY,
                motion_test_window_ms=fss.MOTION_TEST_WINDOW_MS,
                max_torque=fss.MAX_TORQUE, provisional=frozenset())
    args.update(overrides)
    return fss.build_spec({777: d777, 2825: d2825}, args['factory'],
                          args['motion_test_window_ms'], args['max_torque'],
                          args['provisional'])


def _driver_source():
    with open(_DRIVER_PATH, encoding='utf-8') as handle:
        return handle.read()


def _driver_tick_map():
    """The driver's own rad_to_tick / tick_to_rad, compiled out of the file."""
    source = _driver_source()
    ns = {'RAD_PER_TICK': 2.0 * math.pi / 4096, 'TICKS_PER_REV': 4096}
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name in (
                'rad_to_tick', 'tick_to_rad'):
            exec(compile(textwrap.dedent(ast.get_source_segment(source, node)),  # noqa: S102
                         _DRIVER_PATH, 'exec'), ns)
    return ns['rad_to_tick'], ns['tick_to_rad']


def _driver_literal(name):
    for node in ast.parse(_driver_source()).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f'{name} is not a module-level literal in the driver')


def _block_from(joint, offset_raw=0):
    """A 40-byte EEPROM block that carries ``joint``'s golden image."""
    block = bytearray(fb.EEPROM_BLOCK_LEN)
    block[0:2] = bytes([3, 10])
    block[3:5] = fb.le16(joint.model)
    sizes = {addr: size for addr, size, _name, _cls in fss.LAYOUT}
    for addr, value in joint.image.items():
        if sizes[addr] == 2:
            block[addr:addr + 2] = fb.le16(value)
        else:
            block[addr] = value
    block[31:33] = fb.le16(offset_raw)
    return block


class TestDesignTables(unittest.TestCase):
    def test_every_combination_produces_the_measured_table(self):
        rad_to_tick, _tick_to_rad = _driver_tick_map()
        for (d777, d2825), (signs, t0, home, closest) in _TABLE.items():
            spec = _pinned(d777, d2825)
            label = f'{{777: {d777}, 2825: {d2825}}}'
            self.assertTrue(spec.pinned, label)
            self.assertEqual(spec.signs, signs, label)
            self.assertEqual(spec.t0, t0, label)
            self.assertEqual(spec.windows, _WINDOWS, label)
            ticks = tuple(rad_to_tick(q, s, t0=z)
                          for q, s, z in zip(_HOME, spec.signs, spec.t0))
            self.assertEqual(ticks, home, label)
            self.assertEqual(min(min(t, 4095 - t) for t in ticks), closest,
                             label)
            for joint in spec.joints:
                self.assertEqual(joint.window, _WINDOWS[joint.index], label)
                self.assertEqual(joint.sign, signs[joint.index], label)
                self.assertEqual(joint.t0, t0[joint.index], label)

    def test_the_windows_are_direction_independent_and_far_from_the_seam(self):
        for d777, d2825 in _TABLE:
            spec = _pinned(d777, d2825)
            self.assertEqual(spec.windows, _WINDOWS)
            for lo, hi in spec.windows:
                self.assertGreaterEqual(min(lo, 4095 - hi), 795)
        self.assertEqual(fss.V1.windows, _WINDOWS)

    def test_the_round_trip_is_exact_over_every_in_window_tick(self):
        rad_to_tick, tick_to_rad = _driver_tick_map()
        for d777, d2825 in _TABLE:
            spec = _pinned(d777, d2825)
            for joint in spec.joints:
                lo, hi = joint.window
                for tick in range(lo, hi + 1):
                    back = rad_to_tick(
                        tick_to_rad(tick, joint.sign, t0=joint.t0),
                        joint.sign, t0=joint.t0)
                    self.assertEqual(back, tick, (joint.name, tick))

    def test_centred_t0_puts_the_middle_of_the_range_on_2048(self):
        self.assertEqual(fss.centred_t0(0.0, 3.1416, 1), 1024)
        self.assertEqual(fss.centred_t0(0.0, 3.1416, -1), 3072)
        self.assertEqual(fss.centred_t0(0.0, 1.5708, 1), 1536)
        self.assertEqual(fss.centred_t0(-1.9198, 1.9198, -1), 2048)


class TestPerModelDirection(unittest.TestCase):
    def test_a_half_pinned_spec_is_not_pinned(self):
        spec = fss.build_spec({777: 1, 2825: None}, fss.FACTORY,
                              fss.MOTION_TEST_WINDOW_MS, fss.MAX_TORQUE,
                              frozenset())
        self.assertIs(spec.pinned, False)
        self.assertIsNone(spec.signs)
        self.assertIsNone(spec.t0)
        self.assertEqual(spec.windows, _WINDOWS)
        for joint in spec.joints:
            if joint.servo_id in (2, 3):
                self.assertIsNone(joint.tick_direction)
                self.assertIsNone(joint.sign)
                self.assertIsNone(joint.t0)
            else:
                self.assertEqual(joint.tick_direction, 1)
                self.assertEqual(
                    joint.sign, fss.OUTPUT_AXIS_VS_URDF[joint.index])
                self.assertIsNotNone(joint.t0)

    def test_joints_2_and_3_follow_the_2825_direction(self):
        for d777, d2825 in _TABLE:
            spec = _pinned(d777, d2825)
            for joint in spec.joints:
                want = d2825 if joint.servo_id in (2, 3) else d777
                self.assertEqual(joint.tick_direction, want)
                self.assertEqual(joint.model,
                                 2825 if joint.servo_id in (2, 3) else 777)

    def test_pinned_needs_both_directions_and_no_provisional_name(self):
        self.assertTrue(_pinned(1, -1).pinned)
        self.assertFalse(_pinned(1, -1, provisional=frozenset({'FACTORY'}))
                         .pinned)
        self.assertFalse(_pinned(None, None).pinned)


class TestLiteralsMatchTheDriver(unittest.TestCase):
    def test_servo_ids_names_and_limits(self):
        self.assertEqual(_driver_literal('SERVO_IDS'), fss.SERVO_IDS)
        self.assertEqual(_driver_literal('JOINT_NAMES'), fss.JOINT_NAMES)
        self.assertEqual(_driver_literal('JOINT_LIMITS_RAD'),
                         fss.JOINT_LIMITS_RAD)

    def test_one_label_colour_cap_and_model_per_joint(self):
        n = len(fss.SERVO_IDS)
        for table in (fss.JOINT_NAMES, fss.MODELS, fss.JOINT_LIMITS_RAD,
                      fss.OUTPUT_AXIS_VS_URDF, fss.REFERENCE_THETA_DEG,
                      fss.HOLE_THETA_OFFSET_DEG, fss.GAUGE_CAP,
                      fss.JOINT_LABEL_DE, fss.COLOUR_DE, fss.COLOUR_HEX,
                      fss.MAX_TORQUE):
            self.assertEqual(len(table), n)
        self.assertEqual(fss.MODELS, (777, 2825, 2825, 777, 777, 777))
        self.assertEqual(fss.PROFILE_ID, 'v1_studio')


class TestGoldenImage(unittest.TestCase):
    def test_every_checked_register_is_in_every_image(self):
        checked = {addr for addr, _s, _n, cls in fss.LAYOUT
                   if cls in ('refuse', 'warn')}
        for joint in fss.V1.joints:
            self.assertEqual(checked - set(joint.image), set(), joint.name)

    def test_the_decided_values(self):
        for joint in fss.V1.joints:
            image = joint.image
            self.assertEqual(image[fb.REG_ID], joint.servo_id)
            self.assertEqual(image[fb.REG_BAUD_RATE], 0)
            self.assertEqual(image[fb.REG_RETURN_DELAY], 0)
            self.assertEqual(image[fb.REG_RESPONSE_STATUS_LEVEL], 1)
            self.assertEqual(image[fb.REG_UNLOAD_CONDITION], 46)
            self.assertEqual(image[fb.REG_ANGULAR_RESOLUTION], 1)
            self.assertEqual(image[fb.REG_OPERATING_MODE], 0)
            self.assertEqual(image[fb.REG_OVERLOAD_TORQUE], 80)
            self.assertEqual(image[fb.REG_25], 0)
            self.assertEqual(image[fb.REG_PHASE] & 0x10, 0)
            self.assertEqual((image[fb.REG_MIN_POSITION_LIMIT],
                              image[fb.REG_MAX_POSITION_LIMIT]), joint.window)
            self.assertEqual(image[fb.REG_MAX_TORQUE_LIMIT],
                             fss.MAX_TORQUE[joint.index])

    def test_protection_current_is_per_model(self):
        by_model = {j.model: j.image[fb.REG_PROTECTION_CURRENT]
                    for j in fss.V1.joints}
        self.assertNotEqual(by_model[777], by_model[2825])
        self.assertEqual(by_model[777], fss.FACTORY[777][28])
        self.assertEqual(by_model[2825], fss.FACTORY[2825][28])

    def test_the_claw_has_the_pinch_floor(self):
        self.assertEqual(fss.MAX_TORQUE[5], 150)
        self.assertLess(fss.MAX_TORQUE[5], fss.MAX_TORQUE[0])

    def test_a_factory_phase_byte_with_bit_4_is_stored_cleared(self):
        factory = {m: {**regs, 18: regs[18] | 0x10}
                   for m, regs in fss.FACTORY.items()}
        image = fss.golden_image(0, factory, fss.MAX_TORQUE)
        self.assertEqual(image[18], fss.FACTORY[777][18] & ~0x10)


class TestLayout(unittest.TestCase):
    def test_5_to_39_exactly_once(self):
        covered = []
        for addr, size, name, cls in fss.LAYOUT:
            self.assertIn(size, (1, 2), name)
            self.assertIn(cls, ('refuse', 'warn', 'offset', 'ignore'), name)
            covered.extend(range(addr, addr + size))
        self.assertEqual(covered, list(range(5, 40)))

    def test_the_german_names_use_literal_umlauts(self):
        names = [name for _a, _s, name, _c in fss.LAYOUT]
        self.assertIn('Antwortverzögerung', names)
        self.assertIn('Winkelauflösung', names)
        self.assertIn('Überlastmoment', names)


class TestCompareBlock(unittest.TestCase):
    def setUp(self):
        self.spec = _pinned(1, 1)

    def test_a_block_carrying_the_image_is_clean(self):
        for joint in self.spec.joints:
            for offset in (0, 810, 0x0800 | 1001, 2047):
                self.assertEqual(
                    fss.compare_block(bytes(_block_from(joint, offset)), joint),
                    ([], []), (joint.name, offset))

    def _altered(self, joint, addr, size):
        block = _block_from(joint)
        if size == 2:
            value = fb.from_le16(block[addr], block[addr + 1]) + 5
            block[addr:addr + 2] = fb.le16(value)
        else:
            block[addr] ^= 0x01
        return bytes(block)

    def test_every_refuse_and_warn_register_is_judged(self):
        joint = self.spec.joints[1]
        for addr, size, name, cls in fss.LAYOUT:
            if cls not in ('refuse', 'warn'):
                continue
            refusals, warnings = fss.compare_block(
                self._altered(joint, addr, size), joint)
            hit = refusals if cls == 'refuse' else warnings
            other = warnings if cls == 'refuse' else refusals
            self.assertEqual([(d.addr, d.name) for d in hit], [(addr, name)],
                             name)
            self.assertEqual(other, [], name)
            self.assertEqual(hit[0].expected, joint.image[addr], name)

    def test_the_ignored_registers_are_ignored(self):
        joint = self.spec.joints[0]
        for addr in (fb.REG_SPEED_P, fb.REG_SPEED_I):
            self.assertEqual(
                fss.compare_block(self._altered(joint, addr, 1), joint),
                ([], []))

    def test_a_foreign_offset_word_is_a_refusal_without_an_expectation(self):
        joint = self.spec.joints[3]
        refusals, warnings = fss.compare_block(
            bytes(_block_from(joint, 0x1805)), joint)
        self.assertEqual(warnings, [])
        self.assertEqual(len(refusals), 1)
        self.assertEqual(refusals[0].addr, 31)
        self.assertEqual(refusals[0].found, 0x1805)
        self.assertIsNone(refusals[0].expected)

    def test_the_limits_tolerate_one_tick(self):
        joint = self.spec.joints[5]
        lo, hi = joint.window
        for delta, refused in ((1, False), (-1, False), (2, True), (-2, True)):
            block = _block_from(joint)
            block[9:11] = fb.le16(lo + delta)
            refusals, _w = fss.compare_block(bytes(block), joint)
            self.assertEqual(bool(refusals), refused, delta)
            block = _block_from(joint)
            block[11:13] = fb.le16(hi + delta)
            refusals, _w = fss.compare_block(bytes(block), joint)
            self.assertEqual(bool(refusals), refused, delta)

    def test_phase_bit_4_alone_is_not_a_block_deviation(self):
        # The probe refuses bit 4 by its own sentence first; the block compare
        # judges the rest of the byte.
        joint = self.spec.joints[0]
        block = _block_from(joint)
        block[18] |= 0x10
        self.assertEqual(fss.compare_block(bytes(block), joint), ([], []))


class TestShippedSpecIsUnpinned(unittest.TestCase):
    """TRIPWIRE. The pin commit (after rig gates P0, VR3, VR5 and the
    overcurrent check on the pilot) replaces the provisional values, empties
    PROVISIONAL_NAMES and rewrites this class together with the digest literal
    in TestDigest. Until then the driver refuses to boot a V1 and nothing
    calibrates a servo with a provisional value."""

    def test_the_provisional_values_are_unpinned(self):
        self.assertEqual(fss.TICK_DIRECTION, {777: None, 2825: None})
        self.assertIs(fss.V1.pinned, False)
        self.assertIsNone(fss.V1.signs)
        self.assertIsNone(fss.V1.t0)
        self.assertEqual(fss.PROVISIONAL_NAMES,
                         frozenset({'TICK_DIRECTION', 'FACTORY',
                                    'MOTION_TEST_WINDOW_MS', 'MAX_TORQUE'}))
        self.assertEqual(fss.V1.provisional, fss.PROVISIONAL_NAMES)


class TestPurity(unittest.TestCase):
    def setUp(self):
        with open(_SPEC_PATH, encoding='utf-8') as handle:
            self.source = handle.read()
        self.tree = ast.parse(self.source)

    def test_no_environment_no_io(self):
        for needle in ('os.environ', 'getenv', 'open(', 'subprocess',
                       'socket'):
            self.assertNotIn(needle, self.source, needle)

    def test_the_imports(self):
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
        self.assertLessEqual(imported, {'collections', 'hashlib', 'json',
                                        'math', 'feetech_bus', '__future__'})


class TestDigest(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(_pinned(1, 1).digest, _pinned(1, 1).digest)
        self.assertEqual(fss.V1.digest,
                         fss.build_spec(fss.TICK_DIRECTION, fss.FACTORY,
                                        fss.MOTION_TEST_WINDOW_MS,
                                        fss.MAX_TORQUE,
                                        fss.PROVISIONAL_NAMES).digest)
        self.assertRegex(fss.V1.digest, r'^[0-9a-f]{64}$')

    def test_every_input_moves_it(self):
        base = _pinned(1, 1).digest
        factory = {m: dict(regs) for m, regs in fss.FACTORY.items()}
        factory[2825][28] += 1
        self.assertNotEqual(_pinned(1, 1, factory=factory).digest, base)
        torque = list(fss.MAX_TORQUE)
        torque[5] = 151
        self.assertNotEqual(_pinned(1, 1, max_torque=tuple(torque)).digest,
                            base)
        self.assertNotEqual(_pinned(1, -1).digest, base)
        self.assertNotEqual(_pinned(-1, 1).digest, base)
        windows = {**fss.MOTION_TEST_WINDOW_MS, 777: (100, 401)}
        self.assertNotEqual(
            _pinned(1, 1, motion_test_window_ms=windows).digest, base)

    def test_the_shipped_digest(self):
        # TRIPWIRE (see TestShippedSpecIsUnpinned): any change to the shipped
        # table moves this value; the pin commit rewrites it deliberately.
        self.assertEqual(
            fss.V1.digest,
            '2f57852a37bc7b8dca4c45042b314e3b7dc73738f775305deec5a63e86642380')


class TestMeasuredGeometry(unittest.TestCase):
    def test_output_axis_directions(self):
        self.assertEqual(fss.OUTPUT_AXIS_VS_URDF, (-1, 1, -1, -1, -1, 1))

    def test_every_reference_hole_is_a_horn_hole(self):
        for j, (theta, offset) in enumerate(zip(fss.REFERENCE_THETA_DEG,
                                                fss.HOLE_THETA_OFFSET_DEG)):
            k = (theta - offset) / 90.0
            self.assertAlmostEqual(k, round(k), places=9, msg=j)
        self.assertEqual(fss.REFERENCE_THETA_DEG[5], 81.97)
        self.assertEqual(fss.GAUGE_CAP, ('J15',) * 5 + ('GR',))


class TestLabelPatterns(unittest.TestCase):
    def test_serial_and_cap_labels(self):
        serial = re.compile(fss.SERVO_SERIAL_PATTERN)
        cap = re.compile(fss.CAP_LABEL_PATTERN)
        self.assertTrue(serial.match('V1S-000123'))
        self.assertFalse(serial.match('V1S-12345'))
        for good in ('KAPPE-J15-A', 'KAPPE-GR-B2'):
            self.assertTrue(cap.match(good), good)
        for bad in ('KAPPE-J15-Y', 'KAPPE-GR-Z', 'KAPPE-J15-a',
                    'KAPPE-J15-ABCDE'):
            self.assertFalse(cap.match(bad), bad)


if __name__ == '__main__':
    unittest.main()

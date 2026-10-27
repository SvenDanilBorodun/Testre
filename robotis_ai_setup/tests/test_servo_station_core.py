"""The Servo-Station's bus steps (``tools/servo_station/station_core.py``) against
register-level fake servos — deps-free, a fake clock everywhere.

The station writes every V1 servo's hardware safety floor (window, Max_Torque,
protection current, mode) and energises a servo in the Wareneingang motion test,
so the fences here are behavioural: what a run writes, in which step, and what
it refuses to write. Spec: docs/plans/2026-10-26-v1-servo-station/10-spec.md
§5A (WP2).
"""

import contextlib
import datetime
import importlib.util
import io
import json
import os
import pathlib
import shutil
import tempfile
import unittest

import servo_station_fakes as sf
from servo_station_fakes import FakeClock, FakePort, FakeServo, SPEC_A, fb, fss

import station_core as core  # noqa: E402
import station_records as records  # noqa: E402
import station_texts_de as T  # noqa: E402

_UTC = datetime.datetime(2026, 10, 27, 9, 30, 0, tzinfo=datetime.timezone.utc)
_GOLDEN_SIZES = {addr: size for addr, size, _n, _c in fss.LAYOUT}


class _Harness:
    """One bench: fake servos on a fake port, a fake clock, a temp data dir and
    a notify recorder that can act like the operator on a prompt."""

    def __init__(self, servos, clock, tmp, on_prompt=None, noise_by_baud=None, on_step=None):
        self.clock = clock
        self.port = FakePort(servos, noise_by_baud)
        self.events = []
        self.step_marks = {}
        self.servos = self.port.servos
        self.on_prompt = on_prompt or {}
        self.on_step = on_step or {}
        self.store = records.Store(pathlib.Path(tmp))
        self.env = core.Env(self.port, clock=clock, sleep=clock.sleep,
                            notify=self._notify, utcnow=lambda: _UTC)

    def _notify(self, kind, text='', **data):
        self.events.append((kind, text, data))
        if kind == 'step' and data.get('state') == 'running':
            self.step_marks[data['step']] = sum(len(s.writes) for s in self.servos)
            if data['step'] in self.on_step:
                self.on_step[data['step']]()
        if kind == 'prompt' and text in self.on_prompt:
            self.on_prompt[text]()

    def texts(self, kind):
        return [t for k, t, _d in self.events if k == kind]


def _image_bytes_ok(testcase, block, joint):
    for addr, value in joint.image.items():
        size = _GOLDEN_SIZES[addr]
        found = block[addr] if size == 1 else fb.from_le16(block[addr], block[addr + 1])
        testcase.assertEqual(found, value, f'register {addr}')


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='servo-station-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.clock = FakeClock()


class TestDiscovery(_TmpCase):
    def _discover(self, servos, noise_by_baud=None):
        h = _Harness(servos, self.clock, self.tmp, noise_by_baud=noise_by_baud)
        link = core.Link(h.env)
        try:
            return core.discover(link), h
        finally:
            link.close()

    def test_a_servo_at_57600_is_found_there_after_every_baud_was_asked(self):
        servo = FakeServo(baud_index=fb.BAUD_RATES.index(57_600), clock=self.clock)
        h = _Harness([servo], self.clock, self.tmp)
        link = core.Link(h.env)
        try:
            found = core.discover(link)
            self.assertEqual(found, core.Found(57_600, 1))
            self.assertEqual(link.baud, 57_600)          # the run goes on at the servo's baud
        finally:
            link.close()
        self.assertEqual(h.port.opened, list(fb.BAUD_RATES) + [57_600])
        self.assertEqual(servo.writes, [])

    def test_a_second_servo_at_a_later_baud_is_multiple(self):
        # a fresh servo at 1 Mbit/s on the bench, the one in the gauge at 500 kbit/s
        loose = FakeServo(fb.STS3250_MODEL_NUMBER, raw=700, clock=self.clock)
        gauged = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, baud_index=1, clock=self.clock)
        h = _Harness([loose, gauged], self.clock, self.tmp)
        _passed_incoming(h.store, 'V1S-000123', fb.STS3250_MODEL_NUMBER)
        with self.assertRaises(core.StationError) as ctx:
            _kalibrieren(h)
        self.assertEqual(ctx.exception.code, 'MULTIPLE')
        self.assertEqual((loose.writes, gauged.writes), ([], []))
        self.assertIsNone(h.store.load_record('V1S-000123'))

    def test_junk_at_a_later_baud_is_multiple(self):
        servo = FakeServo(clock=self.clock)
        with self.assertRaises(core.StationError) as ctx:
            self._discover([servo], noise_by_baud={38_400: 7})
        self.assertEqual(ctx.exception.code, 'MULTIPLE')

    def test_two_servos_are_multiple(self):
        servos = [FakeServo(servo_id=1, clock=self.clock), FakeServo(servo_id=2, clock=self.clock)]
        with self.assertRaises(core.StationError) as ctx:
            self._discover(servos)
        self.assertEqual(ctx.exception.code, 'MULTIPLE')

    def test_two_fresh_servos_on_one_id_are_multiple(self):
        servos = [FakeServo(clock=self.clock), FakeServo(clock=self.clock)]
        with self.assertRaises(core.StationError) as ctx:
            self._discover(servos)
        self.assertEqual(ctx.exception.code, 'MULTIPLE')

    def test_nothing_is_no_servo_after_every_baud(self):
        with self.assertRaises(core.StationError) as ctx:
            self._discover([])
        self.assertEqual(ctx.exception.code, 'NO_SERVO')
        self.assertEqual(ctx.exception.message_de, T.NO_SERVO)

    def test_junk_is_multiple(self):
        with self.assertRaises(core.StationError) as ctx:
            self._discover([], noise_by_baud={1_000_000: 5})
        self.assertEqual(ctx.exception.code, 'MULTIPLE')

    def test_the_first_baud_that_answers_decides_never_a_later_one(self):
        servos = [FakeServo(servo_id=1, clock=self.clock), FakeServo(servo_id=2, clock=self.clock),
                  FakeServo(servo_id=3, baud_index=1, clock=self.clock)]
        h = _Harness(servos, self.clock, self.tmp)
        link = core.Link(h.env)
        with self.assertRaises(core.StationError) as ctx:
            core.discover(link)
        link.close()
        self.assertEqual(ctx.exception.code, 'MULTIPLE')
        self.assertEqual(h.port.opened, [1_000_000])


class TestLink(_TmpCase):
    def test_a_port_that_will_not_open_names_no_exception_text_and_logs_it(self):
        def open_bus(_baud):
            raise OSError("could not open port 'COM7': PermissionError(13, 'Access is denied.')")
        env = core.Env(open_bus, clock=self.clock, sleep=self.clock.sleep, port='COM7')
        link = core.Link(env)
        with self.assertLogs('servo_station', level='WARNING') as logs:
            with self.assertRaises(core.StationError) as ctx:
                link.open(1_000_000)
        self.assertEqual(ctx.exception.code, 'PORT_OPEN_FAILED')
        self.assertEqual(ctx.exception.message_de, T.PORT_OPEN_FAILED.format(port='COM7'))
        for english in ('could not open', 'Access is denied', 'PermissionError'):
            self.assertNotIn(english, ctx.exception.message_de)
        self.assertTrue(any('Access is denied' in line for line in logs.output))


def _passed_incoming(store, serial, model):
    store.save_incoming({'schema': records.INCOMING_SCHEMA, 'servo_serial': serial, 'model_number': model,
                         'model_name': fb.STS_MODEL_NAMES[model], 'firmware': '3.10',
                         'tested_at': '2026-10-27T08:00:00Z', 'moved_ticks': 512, 'time_ms': [170, 168],
                         'peak_current_ma': 812, 'status_bits': 0, 'passed': True,
                         'reason_code': None, 'reason': None})


def _kalibrieren(h, serial='V1S-000123', joint_index=1, spec=SPEC_A, cap='KAPPE-J15-A',
                 gauge_check_at='2026-10-27T07:00:00Z'):
    return core.kalibrieren(h.env, h.store, serial=serial, joint_index=joint_index, cap_label=cap,
                            operator='Anna', gauge_check_at=gauge_check_at, spec=spec,
                            host='bench-1', tool_commit='abc123')


class TestKalibrieren(_TmpCase):
    def _bench(self, servo, pull='power', **kw):
        actions = {'power': lambda: servo.pull_power(off_polls=5),
                   'brownout': lambda: servo.brownout(polls=5),
                   None: lambda: None}
        return _Harness([servo], self.clock, self.tmp, on_prompt={T.REPLUG_PULL: actions[pull]}, **kw)

    def test_fresh_joint2_servo_end_to_end(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, lock_default=1, clock=self.clock)
        h = self._bench(servo)
        _passed_incoming(h.store, 'V1S-000123', fb.STS3250_MODEL_NUMBER)
        record, path = _kalibrieren(h)
        joint = SPEC_A.joints[1]
        block = servo.block()
        _image_bytes_ok(self, block, joint)
        self.assertEqual(fss.compare_block(block, joint), ([], []))
        self.assertEqual(fb.decode_homing_offset(servo.u16(31)), 810)
        self.assertEqual(servo.present(), 1024)
        self.assertEqual(servo.mem[fb.REG_LOCK], 1)
        self.assertEqual(servo.power_cycles, 1)
        on_disk = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(on_disk, record)
        self.assertEqual(on_disk['checksum'], records.record_checksum(on_disk))
        self.assertEqual(on_disk['calibration']['homing_offset'], 810)
        self.assertEqual(on_disk['calibration']['raw_at_gauge'], 1834)
        self.assertEqual(on_disk['calibration']['mark_tick'], 1024)
        self.assertEqual(on_disk['calibration']['present_after_power_cycle'], 1024)
        self.assertEqual(on_disk['calibration']['cap'], 'J15')
        self.assertEqual(on_disk['calibration']['gauge_id'], 'KAPPE-J15-A')
        self.assertEqual(on_disk['eeprom_image_hex'], block.hex())
        self.assertEqual(on_disk['spec_sha256'], SPEC_A.digest)
        self.assertEqual((on_disk['joint'], on_disk['servo_id'], on_disk['t0']), ('joint2', 2, 1024))
        self.assertTrue(on_disk['incoming_test']['passed'])
        self.assertEqual(on_disk['factory_readings']['id'], 1)
        self.assertEqual(len(on_disk['factory_readings']['eeprom_hex']), 80)
        self.assertNotIn(T.WARN_NO_INCOMING, on_disk['warnings'])
        # never a goal, speed, acceleration or goal-time write in a calibration run
        self.assertFalse(servo.written_addrs() & set(range(41, 48)))
        self.assertIn(T.REPLUG_PULL, h.texts('prompt'))
        self.assertIn(T.REPLUG_PLUG, h.texts('prompt'))

    def test_writes_to_the_limits_happen_only_in_step_I(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, clock=self.clock)
        h = self._bench(servo)
        _passed_incoming(h.store, 'V1S-000123', fb.STS3250_MODEL_NUMBER)
        _kalibrieren(h)
        limit_writes = [n for n, (addr, data) in enumerate(servo.writes)
                        if set(range(addr, addr + len(data))) & {9, 10, 11, 12}]
        offset_writes = [n for n, (addr, data) in enumerate(servo.writes) if addr == 31]
        self.assertTrue(limit_writes and offset_writes)
        self.assertGreater(min(limit_writes), max(offset_writes))
        for n in limit_writes:
            self.assertGreaterEqual(n, h.step_marks['I'])
            self.assertLess(n, h.step_marks['J'])

    def test_power_cycle_catches_a_lost_eeprom(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, servo_id=2, raw=1834, lock_default=1,
                          honours_unlock=False, clock=self.clock)
        h = self._bench(servo)
        _passed_incoming(h.store, 'V1S-000123', fb.STS3250_MODEL_NUMBER)
        with self.assertRaises(core.StationError) as ctx:
            _kalibrieren(h)
        self.assertEqual(ctx.exception.code, 'NOT_PERSISTED')
        self.assertIsNone(h.store.load_record('V1S-000123'))
        self.assertFalse(h.store.calibrations_csv.exists())

    def test_an_id_that_did_not_persist_is_not_persisted_not_a_timeout(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, lock_default=1,
                          honours_unlock=False, clock=self.clock)
        h = self._bench(servo)
        _passed_incoming(h.store, 'V1S-000123', fb.STS3250_MODEL_NUMBER)
        start = self.clock()
        with self.assertRaises(core.StationError) as ctx:
            _kalibrieren(h)
        self.assertEqual(ctx.exception.code, 'NOT_PERSISTED')
        self.assertLess(self.clock() - start, 30)
        self.assertIsNone(h.store.load_record('V1S-000123'))

    def test_a_silent_spell_without_a_power_on_is_refused(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, clock=self.clock)
        h = self._bench(servo, pull='brownout')
        _passed_incoming(h.store, 'V1S-000123', fb.STS3250_MODEL_NUMBER)
        with self.assertRaises(core.StationError) as ctx:
            _kalibrieren(h)
        self.assertEqual(ctx.exception.code, 'NO_REAL_RESTART')
        self.assertEqual(ctx.exception.message_de, T.NO_REAL_RESTART)
        self.assertIsNone(h.store.load_record('V1S-000123'))

    def test_torque_limit_reads_max_torque_after_a_real_restart(self):
        servo = FakeServo(fb.STS3215_MODEL_NUMBER, raw=900, clock=self.clock)
        seen = {}

        def pull():
            seen['before'] = servo.u16(fb.REG_TORQUE_LIMIT)
            servo.pull_power(off_polls=5)
        h = _Harness([servo], self.clock, self.tmp, on_prompt={T.REPLUG_PULL: pull})
        _passed_incoming(h.store, 'V1S-000006', fb.STS3215_MODEL_NUMBER)
        record, _path = _kalibrieren(h, serial='V1S-000006', joint_index=5, cap='KAPPE-GR-A')
        self.assertEqual(fss.MAX_TORQUE[5], 150)
        self.assertEqual(seen['before'], 149)
        self.assertEqual(servo.u16(fb.REG_TORQUE_LIMIT), 150)
        self.assertEqual(record['joint'], 'RL_joint')

    def test_an_unexpected_torque_limit_after_a_restart_is_unclear_not_a_defect(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, copies_torque_limit=False,
                          torque_limit_on_power_up=1000, clock=self.clock)
        h = self._bench(servo)
        _passed_incoming(h.store, 'V1S-000123', fb.STS3250_MODEL_NUMBER)
        with self.assertRaises(core.StationError) as ctx:
            _kalibrieren(h)
        self.assertEqual(ctx.exception.code, 'RESTART_UNCLEAR')
        self.assertNotIn('aussortieren', ctx.exception.message_de)
        self.assertIn('1000', ctx.exception.message_de)
        self.assertIn('800', ctx.exception.message_de)
        self.assertIsNone(h.store.load_record('V1S-000123'))

    def test_offset_minus_2048_refused(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1024 + 2048, clock=self.clock)
        h = self._bench(servo)
        _passed_incoming(h.store, 'V1S-000123', fb.STS3250_MODEL_NUMBER)
        with self.assertRaises(core.StationError) as ctx:
            _kalibrieren(h)
        self.assertEqual(ctx.exception.code, 'OFFSET_EDGE')
        self.assertFalse(servo.written_addrs() & {31, 32})
        self.assertIsNone(h.store.load_record('V1S-000123'))

    def test_foreign_offset_is_normalised(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, clock=self.clock)
        servo.mem[31:33] = fb.le16(0x1805)
        servo.committed[31:33] = fb.le16(0x1805)
        self.assertEqual(servo.present(), (1834 + 2053) % 4096)
        h = self._bench(servo)
        _passed_incoming(h.store, 'V1S-000123', fb.STS3250_MODEL_NUMBER)
        record, _path = _kalibrieren(h)
        self.assertEqual(fb.decode_homing_offset(servo.u16(31)), 810)
        self.assertEqual(servo.present(), 1024)
        self.assertEqual(record['calibration']['homing_offset'], 810)

    def test_found_at_another_baud_is_moved_to_1mbps(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, clock=self.clock,
                          baud_index=fb.BAUD_RATES.index(115_200))
        h = self._bench(servo)
        _passed_incoming(h.store, 'V1S-000123', fb.STS3250_MODEL_NUMBER)
        record, _path = _kalibrieren(h)
        self.assertEqual(servo.mem[fb.REG_BAUD_RATE], 0)
        self.assertEqual(servo.committed[fb.REG_BAUD_RATE], 0)
        self.assertEqual(servo.baud, 1_000_000)
        self.assertEqual(record['factory_readings']['baud_index'], fb.BAUD_RATES.index(115_200))

    def test_wrong_model_refuses_before_any_write(self):
        servo = FakeServo(fb.STS3215_MODEL_NUMBER, raw=1834, clock=self.clock)
        h = self._bench(servo)
        _passed_incoming(h.store, 'V1S-000123', fb.STS3215_MODEL_NUMBER)
        with self.assertRaises(core.StationError) as ctx:
            _kalibrieren(h)
        self.assertEqual(ctx.exception.code, 'WRONG_MODEL')
        self.assertIn('STS3215', ctx.exception.message_de)
        self.assertIn('STS3250', ctx.exception.message_de)
        self.assertEqual(servo.writes, [])

    def test_a_7v4_servo_refuses_before_any_write(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, clock=self.clock)
        servo.mem[fb.REG_MAX_VOLTAGE] = 80
        h = self._bench(servo)
        _passed_incoming(h.store, 'V1S-000123', fb.STS3250_MODEL_NUMBER)
        with self.assertRaises(core.StationError) as ctx:
            _kalibrieren(h)
        self.assertEqual(ctx.exception.code, 'SEVEN_V')
        self.assertEqual(servo.writes, [])

    def test_a_mirrored_phase_byte_refuses_before_any_write(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, clock=self.clock)
        servo.mem[fb.REG_PHASE] ^= 0x01
        h = self._bench(servo)
        _passed_incoming(h.store, 'V1S-000123', fb.STS3250_MODEL_NUMBER)
        with self.assertRaises(core.StationError) as ctx:
            _kalibrieren(h)
        self.assertEqual(ctx.exception.code, 'PHASE')
        self.assertEqual(servo.writes, [])

    def test_phase_bit4_is_cleared_and_the_rest_of_the_byte_kept(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, clock=self.clock)
        servo.mem[fb.REG_PHASE] |= 0x10
        servo.committed[fb.REG_PHASE] |= 0x10
        h = self._bench(servo)
        _passed_incoming(h.store, 'V1S-000123', fb.STS3250_MODEL_NUMBER)
        _kalibrieren(h)
        self.assertEqual(servo.mem[fb.REG_PHASE], fss.FACTORY[2825][18] & ~0x10)

    def test_replug_detection(self):
        def run(pattern):
            answers = iter(pattern)
            clock = FakeClock()
            prompts = []
            env = core.Env(lambda baud: None, clock=clock, sleep=clock.sleep,
                           notify=lambda kind, text='', **d: prompts.append(text))
            start = clock()
            try:
                core.wait_for_replug(env, lambda: next(answers, False))
                return 'back', clock() - start, prompts
            except core.StationError as e:
                return e.code, clock() - start, prompts

        outcome, _t, prompts = run([True] * 5 + [False] * 3 + [True, True])
        self.assertEqual(outcome, 'back')
        self.assertEqual(prompts, [T.REPLUG_PLUG])
        outcome, elapsed, prompts = run([True, False] * 2000)
        self.assertEqual(outcome, 'REPLUG_TIMEOUT')
        self.assertEqual(prompts, [])
        self.assertAlmostEqual(elapsed, 120.0, delta=0.2)
        # one miss and one answer in a row never count: back needs 2 answers after 3 misses
        outcome, _t, _p = run([False] * 3 + [True, False] * 2000)
        self.assertEqual(outcome, 'REPLUG_TIMEOUT')
        outcome, elapsed, _p = run([True] * 3 + [False] * 3000)
        self.assertEqual(outcome, 'REPLUG_TIMEOUT')
        self.assertAlmostEqual(elapsed, 120.0, delta=0.2)

    def test_a_vanished_usb_port_is_usb_lost(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, clock=self.clock)
        h = self._bench(servo, pull=None)

        def boom(*_a, **_k):
            raise OSError('could not open port')
        h.on_prompt[T.REPLUG_PULL] = lambda: setattr(h.port.buses[-1], 'ping', boom)
        _passed_incoming(h.store, 'V1S-000123', fb.STS3250_MODEL_NUMBER)
        with self.assertRaises(core.StationError) as ctx:
            _kalibrieren(h)
        self.assertEqual(ctx.exception.code, 'USB_LOST')
        self.assertIsNone(h.store.load_record('V1S-000123'))

    def test_calibration_without_wareneingang_notes_and_records_null(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, clock=self.clock)
        h = self._bench(servo)
        record, _path = _kalibrieren(h)
        notes = h.texts('note')
        self.assertEqual(notes.count(T.NOTE_NO_INCOMING.format(serial='V1S-000123')), 1)
        self.assertIsNone(record['incoming_test'])
        self.assertIn(T.WARN_NO_INCOMING, record['warnings'])

    def test_a_failed_wareneingang_refuses(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, clock=self.clock)
        h = self._bench(servo)
        h.store.save_incoming({'schema': records.INCOMING_SCHEMA, 'servo_serial': 'V1S-000123',
                               'passed': False, 'reason_code': 'MOTION_TIME', 'reason': 'zu langsam',
                               'tested_at': '2026-10-27T08:00:00Z'})
        with self.assertRaises(core.StationError) as ctx:
            _kalibrieren(h)
        self.assertEqual(ctx.exception.code, 'INCOMING_FAILED')
        self.assertIn('zu langsam', ctx.exception.message_de)
        self.assertEqual(servo.writes, [])
        self.assertEqual(h.port.opened, [])

    def test_the_unpinned_spec_refuses_before_any_bus_call(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, clock=self.clock)
        h = self._bench(servo)
        with self.assertRaises(core.StationError) as ctx:
            _kalibrieren(h, spec=fss.V1)
        self.assertEqual(ctx.exception.code, 'SPEC_NOT_PINNED')
        self.assertEqual(h.port.opened, [])

    def test_a_rerun_is_idempotent_and_spends_no_eeprom_write(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, lock_default=1, clock=self.clock)
        h = self._bench(servo)
        _passed_incoming(h.store, 'V1S-000123', fb.STS3250_MODEL_NUMBER)
        first, _p = _kalibrieren(h)
        block = servo.block()
        servo.writes.clear()
        self.clock.sleep(5)
        second, _p = _kalibrieren(h)
        self.assertEqual(servo.block(), block)
        self.assertEqual(servo.eeprom_writes(), [])
        self.assertEqual(len(list(h.store.history_dir.glob('V1S-000123-*.json'))), 1)
        self.assertEqual(second['calibration']['homing_offset'], first['calibration']['homing_offset'])

    def test_a_warn_class_register_that_does_not_stick_is_a_record_warning(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, clock=self.clock)
        original = servo.write

        def write(addr, data):
            if addr == 21:          # P-Anteil: the firmware keeps its own value
                servo.writes.append((addr, bytes(data)))
                return
            original(addr, data)
        servo.write = write
        servo.mem[21] = 16
        servo.committed[21] = 16
        h = self._bench(servo)
        record, _path = _kalibrieren(h)
        self.assertTrue(any('P-Anteil (Register 21) bleibt 16 statt 32' in w for w in record['warnings']))

    def test_a_refuse_class_register_that_does_not_stick_refuses(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, clock=self.clock)
        original = servo.write

        def write(addr, data):
            if addr == fb.REG_PROTECTION_CURRENT:
                servo.writes.append((addr, bytes(data)))
                return
            original(addr, data)
        servo.write = write
        servo.mem[28:30] = fb.le16(310)          # a servo someone wrote ≈ 2 A into
        h = self._bench(servo)
        with self.assertRaises(core.StationError) as ctx:
            _kalibrieren(h)
        self.assertEqual(ctx.exception.code, 'WRITE_FAILED')
        self.assertIn('Schutzstrom (Register 28)', ctx.exception.message_de)
        self.assertIsNone(h.store.load_record('V1S-000123'))


class TestKalibrierenRefusals(_TmpCase):
    """Every refusal of a Kalibrieren run fenced by a test that fails without
    it (the fix round's mutation sweep, V1-F1): what is refused, in which step,
    and that no record is written."""

    def _bench(self, servo, pull=None):
        action = pull or (lambda: servo.pull_power(off_polls=5))
        return _Harness([servo], self.clock, self.tmp, on_prompt={T.REPLUG_PULL: action})

    def _refused(self, h, **kw):
        with self.assertRaises(core.StationError) as ctx:
            _kalibrieren(h, **kw)
        self.assertIsNone(h.store.load_record(kw.get('serial', 'V1S-000123')))
        return ctx.exception

    def test_identity_refusals_come_before_any_write(self):
        cases = {
            'UNKNOWN_MODEL': dict(model=1234),
            'MAX_VOLTAGE': dict(setup=lambda s: s.mem.__setitem__(fb.REG_MAX_VOLTAGE, 130)),
            'VOLTAGE': dict(voltage_dv=105),
            'HOT': dict(temperature_c=45),
            'STATUS': dict(status=fb.ERR_OVERLOAD),
        }
        for code, case in cases.items():
            with self.subTest(code=code):
                setup = case.pop('setup', None)
                servo = FakeServo(case.pop('model', fb.STS3250_MODEL_NUMBER), raw=1834, clock=self.clock, **case)
                if setup:
                    setup(servo)
                h = _Harness([servo], self.clock, tempfile.mkdtemp(dir=self.tmp))
                err = self._refused(h)
                self.assertEqual(err.code, code)
                self.assertEqual(servo.writes, [])

    def test_the_supply_window_and_the_heat_limit_are_the_specs(self):
        for kw, refused in ((dict(voltage_dv=110), False), (dict(voltage_dv=128), False),
                            (dict(voltage_dv=129), True), (dict(temperature_c=44), False)):
            with self.subTest(**kw):
                servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, clock=self.clock, **kw)
                h = _Harness([servo], self.clock, tempfile.mkdtemp(dir=self.tmp),
                             on_prompt={T.REPLUG_PULL: lambda s=servo: s.pull_power(off_polls=5)})
                try:
                    _kalibrieren(h)
                    code = None
                except core.StationError as e:
                    code = e.code
                self.assertEqual(code is not None, refused, code)

    def test_a_cap_of_the_wrong_type_refuses_before_the_bus(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, clock=self.clock)
        h = self._bench(servo)
        err = self._refused(h, cap='KAPPE-GR-A')
        self.assertEqual(err.code, 'CAP_MISMATCH')
        self.assertEqual(h.port.opened, [])

    def test_a_status_level_that_will_not_become_1_refuses(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, clock=self.clock)
        servo.mem[fb.REG_RESPONSE_STATUS_LEVEL] = 0
        original = servo.write

        def write(addr, data):
            if addr == fb.REG_RESPONSE_STATUS_LEVEL:
                servo.writes.append((addr, bytes(data)))
                return
            original(addr, data)
        servo.write = write
        err = self._refused(self._bench(servo))
        self.assertEqual(err.code, 'LEVEL')

    def test_a_baud_change_the_servo_ignores_is_write_failed(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, clock=self.clock, baud_index=4)
        original = servo.write

        def write(addr, data):
            if addr == fb.REG_BAUD_RATE:
                servo.writes.append((addr, bytes(data)))
                return
            original(addr, data)
        servo.write = write
        err = self._refused(self._bench(servo))
        self.assertEqual(err.code, 'WRITE_FAILED')
        self.assertIn(T.NO_ANSWER, err.message_de)

    def test_a_baud_register_that_reads_back_the_old_index_is_write_failed(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, clock=self.clock, baud_index=4)
        original = servo.read

        def read(addr, length):
            out = bytearray(original(addr, length))
            if addr <= fb.REG_BAUD_RATE < addr + length:
                out[fb.REG_BAUD_RATE - addr] = 4
            return bytes(out)
        servo.read = read
        err = self._refused(self._bench(servo))
        self.assertEqual(err.code, 'WRITE_FAILED')
        self.assertIn('(4 statt 0)', err.message_de)

    def test_each_half_of_the_id_check_refuses(self):
        def silent_at_the_new_id(servo):            # takes the id, answers no ping there
            original = servo._on_ping

            def on_ping():
                return original() and servo.servo_id == 1
            servo._on_ping = on_ping

        def misread_id(servo):
            original = servo.read

            def read(addr, length):
                out = bytearray(original(addr, length))
                if length == 1 and addr == fb.REG_ID:
                    out[0] = 1
                return bytes(out)
            servo.read = read
        cases = {'new id silent': (silent_at_the_new_id, {}), 'old id still answers': (None, {'keeps_old_id': True}),
                 'register reads the old id': (misread_id, {})}
        for name, (setup, kw) in cases.items():
            with self.subTest(name):
                servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, clock=self.clock, **kw)
                if setup:
                    setup(servo)
                h = _Harness([servo], self.clock, tempfile.mkdtemp(dir=self.tmp))
                err = self._refused(h)
                self.assertEqual(err.code, 'ID_FAILED')
                self.assertNotIn(fb.REG_HOMING_OFFSET, servo.written_addrs())

    def test_a_moving_horn_is_unstable_before_the_offset(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, clock=self.clock)
        servo.hand_ticks_per_s = 20.0          # 10 ticks over the 0.5 s of step G
        err = self._refused(self._bench(servo))
        self.assertEqual(err.code, 'UNSTABLE')
        self.assertNotIn(fb.REG_HOMING_OFFSET, servo.written_addrs())

    def _refused_in_step_h(self, servo, raw_present):
        h = self._bench(servo)
        err = self._refused(h)
        self.assertEqual(err.code, 'OFFSET_CHECK')
        self.assertEqual(err.message_de, T.OFFSET_CHECK.format(present=raw_present, mark=1024))
        self.assertNotIn(T.REPLUG_PULL, h.texts('prompt'))           # refused in H, not in K
        self.assertFalse(servo.written_addrs() & {9, 10, 11, 12})

    def test_a_firmware_that_adds_the_offset_ends_in_offset_check(self):
        # Present = raw + offset: 1834 + 810, not the mark 1024
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, clock=self.clock, adds_offset=True)
        self._refused_in_step_h(servo, 1834 + 810)

    def test_a_firmware_that_does_not_fold_present_ends_in_offset_check(self):
        # raw 3500 → offset −1620: Present reads T0 + 4096 = 5120 unfolded, which the
        # boot probe judges linearly against the window 1024–3072
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=3500, clock=self.clock, folds_present=False)
        self._refused_in_step_h(servo, 5120)

    def test_a_folding_firmware_at_the_same_raw_tick_calibrates(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=3500, clock=self.clock)
        record, _path = _kalibrieren(self._bench(servo))
        self.assertEqual(record['calibration']['homing_offset'], -1620)
        self.assertEqual(record['calibration']['present_after_power_cycle'], 1024)

    def test_a_horn_turned_during_the_replug_is_offset_check_in_step_k(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, clock=self.clock)

        def pull():
            servo.raw += 100
            servo.pull_power(off_polls=5)
        h = self._bench(servo, pull=pull)
        err = self._refused(h)
        self.assertEqual(err.code, 'OFFSET_CHECK')
        self.assertEqual(err.message_de, T.OFFSET_CHECK.format(present=1124, mark=1024))
        self.assertEqual(servo.power_cycles, 1)

    def test_present_unfolded_after_the_power_on_is_offset_check_in_step_k(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=3500, clock=self.clock)

        def pull():
            servo.folds_present = False
            servo.pull_power(off_polls=5)
        err = self._refused(self._bench(servo, pull=pull))
        self.assertEqual(err.code, 'OFFSET_CHECK')
        self.assertEqual(err.message_de, T.OFFSET_CHECK.format(present=5120, mark=1024))
        self.assertEqual(servo.power_cycles, 1)

    def test_a_block_that_changes_before_step_j_is_block_mismatch(self):
        def mode(servo):
            servo.mem[fb.REG_OPERATING_MODE] = 1

        def offset(servo):
            servo.mem[31:33] = fb.le16(fb.encode_homing_offset(811))
        for name, change in (('mode', mode), ('offset', offset)):
            with self.subTest(name):
                servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, clock=self.clock)
                h = _Harness([servo], self.clock, tempfile.mkdtemp(dir=self.tmp),
                             on_step={'J': lambda s=servo, c=change: c(s)})
                err = self._refused(h)
                self.assertEqual(err.code, 'BLOCK_MISMATCH')
                self.assertNotIn(T.REPLUG_PULL, h.texts('prompt'))

    def test_a_baud_change_that_did_not_persist_is_not_persisted_not_a_timeout(self):
        # Lock 0 never commits: after the replug the servo is back at 115200 with id 1
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, baud_index=fb.BAUD_RATES.index(115_200),
                          honours_unlock=False, clock=self.clock)
        h = self._bench(servo)
        start = self.clock()
        err = self._refused(h)
        self.assertEqual(err.code, 'NOT_PERSISTED')
        self.assertLess(self.clock() - start, 30)
        self.assertEqual(servo.power_cycles, 1)

    def test_a_baud_change_that_persisted_still_calibrates(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, baud_index=fb.BAUD_RATES.index(115_200),
                          clock=self.clock)
        record, _path = _kalibrieren(self._bench(servo))
        self.assertEqual(servo.committed[fb.REG_BAUD_RATE], 0)
        self.assertEqual(record['calibration']['present_after_power_cycle'], 1024)


class TestKalibrierenIncomingAndRecord(_TmpCase):
    """A Wareneingang of another servo is refused (owner R5-1), and a
    re-calibration retires the previous record before its first write (V1-F6)."""

    def _bench(self, servo):
        return _Harness([servo], self.clock, self.tmp,
                        on_prompt={T.REPLUG_PULL: lambda: servo.pull_power(off_polls=5)})

    def test_a_wareneingang_of_another_model_refuses_before_any_write(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, clock=self.clock)
        h = self._bench(servo)
        _passed_incoming(h.store, 'V1S-000123', fb.STS3215_MODEL_NUMBER)
        with self.assertRaises(core.StationError) as ctx:
            _kalibrieren(h)
        self.assertEqual(ctx.exception.code, 'INCOMING_MISMATCH')
        self.assertIn('STS3215', ctx.exception.message_de)
        self.assertIn('STS3250', ctx.exception.message_de)
        self.assertIn('V1S-000123', ctx.exception.message_de)
        self.assertEqual(servo.writes, [])
        self.assertIsNone(h.store.load_record('V1S-000123'))

    def test_a_wareneingang_with_another_firmware_refuses_before_any_write(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, clock=self.clock, firmware=(3, 10))
        h = self._bench(servo)
        _passed_incoming(h.store, 'V1S-000123', fb.STS3250_MODEL_NUMBER)
        incoming = h.store.load_incoming('V1S-000123')
        incoming['firmware'] = '3.9'
        h.store.save_incoming(incoming)
        with self.assertRaises(core.StationError) as ctx:
            _kalibrieren(h)
        self.assertEqual(ctx.exception.code, 'INCOMING_MISMATCH')
        self.assertIn('3.9', ctx.exception.message_de)
        self.assertIn('3.10', ctx.exception.message_de)
        self.assertEqual(servo.writes, [])

    def test_a_wareneingang_without_a_firmware_is_judged_by_its_model(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, clock=self.clock)
        h = self._bench(servo)
        _passed_incoming(h.store, 'V1S-000123', fb.STS3250_MODEL_NUMBER)
        incoming = h.store.load_incoming('V1S-000123')
        incoming['firmware'] = None
        h.store.save_incoming(incoming)
        record, _path = _kalibrieren(h)
        self.assertEqual(record['incoming_test'], incoming)

    def _calibrated_once(self, h):
        first, _path = _kalibrieren(h)
        self.assertEqual(list(h.store.history_dir.glob('*.json')), [])
        self.clock.sleep(5)
        return first

    def test_a_recalibration_that_fails_after_its_first_write_leaves_no_record(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, clock=self.clock)
        pulls = {'on': True}

        def pull():
            if pulls['on']:
                servo.pull_power(off_polls=5)
        h = _Harness([servo], self.clock, self.tmp, on_prompt={T.REPLUG_PULL: pull})
        first = self._calibrated_once(h)
        servo.raw += 164                       # the horn one spline tooth off in the gauge
        pulls['on'] = False                    # and the operator never pulls the plug
        with self.assertRaises(core.StationError) as ctx:
            _kalibrieren(h)
        self.assertEqual(ctx.exception.code, 'REPLUG_TIMEOUT')
        self.assertIsNone(h.store.load_record('V1S-000123'))
        (moved,) = h.store.history_dir.glob('V1S-000123-*.json')
        self.assertEqual(json.loads(moved.read_text(encoding='utf-8')), first)
        self.assertEqual(h.store.pending_records(), [])

    def test_a_refusal_before_any_write_keeps_the_record(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1834, clock=self.clock)
        h = self._bench(servo)
        first = self._calibrated_once(h)
        servo.writes.clear()
        servo.ram_temperature_c = 50
        with self.assertRaises(core.StationError) as ctx:
            _kalibrieren(h)
        self.assertEqual(ctx.exception.code, 'HOT')
        self.assertEqual(servo.writes, [])
        self.assertEqual(h.store.load_record('V1S-000123'), first)
        self.assertEqual(list(h.store.history_dir.glob('*.json')), [])


def _motion_servo(clock, raw=1000, **kw):
    servo = FakeServo(fb.STS3215_MODEL_NUMBER, raw=raw, clock=clock, **kw)
    return servo


class TestWareneingang(_TmpCase):
    def _run(self, servo, spec=SPEC_A):
        h = _Harness([servo], self.clock, self.tmp)
        return h, core.wareneingang(h.env, h.store, serial='V1S-000200', spec=spec)

    def test_a_healthy_servo_passes_and_ends_limp(self):
        servo = _motion_servo(self.clock)
        h, rec = self._run(servo)
        self.assertTrue(rec['passed'])
        self.assertEqual(rec['moved_ticks'], 512)
        lo, hi = fss.MOTION_TEST_WINDOW_MS[777]
        for t in rec['time_ms']:
            self.assertTrue(lo <= t <= hi, rec['time_ms'])
        self.assertEqual(rec['peak_current_ma'], round(FakeServo.MOVING_CURRENT_RAW * 6.5))
        self.assertEqual(servo.mem[fb.REG_TORQUE_ENABLE], 0)
        self.assertEqual(servo.u16(fb.REG_TORQUE_LIMIT), 1000)
        self.assertEqual(abs(servo.present() - 1000) <= 8, True)
        on_disk = h.store.load_incoming('V1S-000200')
        self.assertEqual(on_disk, rec)
        self.assertTrue(h.store.incoming_csv.exists())

    def test_no_eeprom_address_is_ever_written(self):
        servo = _motion_servo(self.clock)
        self._run(servo)
        self.assertTrue(servo.writes)
        self.assertEqual(servo.eeprom_writes(), [])

    def test_the_torque_limit_is_300_during_the_motion(self):
        servo = _motion_servo(self.clock)
        seen = []
        original = servo.write

        def write(addr, data):
            if addr == fb.REG_GOAL_POSITION:
                seen.append(servo.u16(fb.REG_TORQUE_LIMIT))
            original(addr, data)
        servo.write = write
        self._run(servo)
        self.assertTrue(seen)
        self.assertEqual(set(seen), {300})

    def test_a_7v4_servo_is_refused_and_recorded_as_failed(self):
        servo = _motion_servo(self.clock)
        servo.mem[fb.REG_MAX_VOLTAGE] = 80
        h = _Harness([servo], self.clock, self.tmp)
        with self.assertRaises(core.StationError) as ctx:
            core.wareneingang(h.env, h.store, serial='V1S-000200', spec=SPEC_A)
        self.assertEqual(ctx.exception.code, 'SEVEN_V')
        self.assertEqual(servo.writes, [])
        rec = h.store.load_incoming('V1S-000200')
        self.assertFalse(rec['passed'])
        self.assertEqual(rec['reason_code'], 'SEVEN_V')

    def test_too_slow_is_motion_time_and_recorded_as_failed(self):
        servo = _motion_servo(self.clock, ticks_per_s=1000.0)
        h = _Harness([servo], self.clock, self.tmp)
        with self.assertRaises(core.StationError) as ctx:
            core.wareneingang(h.env, h.store, serial='V1S-000200', spec=SPEC_A)
        self.assertEqual(ctx.exception.code, 'MOTION_TIME')
        self.assertFalse(h.store.load_incoming('V1S-000200')['passed'])
        self.assertEqual(servo.mem[fb.REG_TORQUE_ENABLE], 0)

    def test_an_error_bit_during_the_motion_fails(self):
        servo = _motion_servo(self.clock)
        servo.status_while_moving = fb.ERR_OVERLOAD
        h = _Harness([servo], self.clock, self.tmp)
        with self.assertRaises(core.StationError) as ctx:
            core.wareneingang(h.env, h.store, serial='V1S-000200', spec=SPEC_A)
        self.assertEqual(ctx.exception.code, 'STATUS')
        self.assertEqual(servo.mem[fb.REG_TORQUE_ENABLE], 0)

    def test_the_recorded_status_bits_are_every_bit_seen(self):
        servo = _motion_servo(self.clock)
        original = servo.write
        goals = []

        def write(addr, data):              # the out move sees one bit, the way back another
            if addr == fb.REG_GOAL_POSITION:
                goals.append(data)
                servo.status_while_moving = {2: fb.ERR_OVERLOAD, 3: fb.ERR_VOLTAGE}.get(len(goals), 0)
            original(addr, data)
        servo.write = write
        h = _Harness([servo], self.clock, self.tmp)
        with self.assertRaises(core.StationError):
            core.wareneingang(h.env, h.store, serial='V1S-000200', spec=SPEC_A)
        self.assertEqual(h.store.load_incoming('V1S-000200')['status_bits'], fb.ERR_OVERLOAD | fb.ERR_VOLTAGE)

    def test_a_fault_mid_motion_still_ends_limp_with_the_torque_limit_restored(self):
        servo = _motion_servo(self.clock)
        servo.mem[48:50] = fb.le16(777)
        servo.fail_present_reads_while_moving = 1
        h = _Harness([servo], self.clock, self.tmp)
        with self.assertRaises(core.StationError) as ctx:
            core.wareneingang(h.env, h.store, serial='V1S-000200', spec=SPEC_A)
        self.assertEqual(ctx.exception.code, 'BUS_ERROR')
        self.assertEqual(servo.mem[fb.REG_TORQUE_ENABLE], 0)
        self.assertEqual(servo.u16(fb.REG_TORQUE_LIMIT), 777)
        self.assertIsNone(h.store.load_incoming('V1S-000200'))

    def test_a_shaft_parked_at_the_encoder_edge_is_refused_with_zero_writes(self):
        for raw in (30, 4070):
            with self.subTest(raw=raw):
                servo = _motion_servo(self.clock, raw=raw)
                h = _Harness([servo], self.clock, self.tmp)
                with self.assertRaises(core.StationError) as ctx:
                    core.wareneingang(h.env, h.store, serial='V1S-000200', spec=SPEC_A)
                self.assertEqual(ctx.exception.code, 'EDGE_PARKED')
                self.assertIn(str(raw), ctx.exception.message_de)
                self.assertEqual(servo.writes, [])
                self.assertIsNone(h.store.load_incoming('V1S-000200'))

    def test_the_edge_rule_is_the_drivers_margin(self):
        for raw, refused in ((39, True), (40, False), (4055, False), (4056, True)):
            with self.subTest(raw=raw):
                servo = _motion_servo(self.clock, raw=raw)
                h = _Harness([servo], self.clock, self.tmp)
                try:
                    core.wareneingang(h.env, h.store, serial='V1S-000200', spec=SPEC_A)
                    code = None
                except core.StationError as e:
                    code = e.code
                self.assertEqual(code == 'EDGE_PARKED', refused, code)

    def test_the_unpinned_spec_refuses(self):
        servo = _motion_servo(self.clock)
        h = _Harness([servo], self.clock, self.tmp)
        with self.assertRaises(core.StationError) as ctx:
            core.wareneingang(h.env, h.store, serial='V1S-000200', spec=fss.V1)
        self.assertEqual(ctx.exception.code, 'SPEC_NOT_PINNED')
        self.assertEqual(h.port.opened, [])


class TestReferenz(_TmpCase):
    def _bench(self, servo):
        return _Harness([servo], self.clock, self.tmp)

    def test_a_fresh_dump_is_read_only_and_covers_0_to_86(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, clock=self.clock)
        h = self._bench(servo)
        path, dump = core.referenz_lesen(h.env, h.store)
        self.assertEqual(servo.writes, [])
        self.assertEqual(sorted(dump['regs']), list(range(0, 87)))
        self.assertTrue(dump['eeprom_block_read'])
        self.assertTrue(dump['ram_block_read'])
        self.assertEqual(dump['failed_addrs'], [])
        self.assertEqual((dump['model'], dump['id'], dump['baud_index']), (2825, 1, 0))
        self.assertIsNone(dump['tick_direction'])
        self.assertIsNone(dump['motion_ms'])
        self.assertEqual(path.parent, h.store.reference_dir)
        self.assertTrue(path.name.endswith('-2825-id1.json'))
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['model'], 2825)
        self.assertIn(T.REF_SAVED.format(path=path), h.texts('note'))

    def test_a_non_fresh_servo_is_refused_and_nothing_saved(self):
        for change in ({5: 2}, {9: 1024}, {11: 3072}):
            with self.subTest(change=change):
                servo = FakeServo(fb.STS3250_MODEL_NUMBER, clock=self.clock)
                for addr, value in change.items():
                    if addr == 5:
                        servo.mem[5] = value
                    else:
                        servo.mem[addr:addr + 2] = fb.le16(value)
                h = self._bench(servo)
                with self.assertRaises(core.StationError) as ctx:
                    core.referenz_lesen(h.env, h.store)
                self.assertEqual(ctx.exception.code, 'P0_NOT_FRESH')
                self.assertEqual(list(h.store.reference_dir.glob('*.json')), [])

    def test_a_failed_block_read_falls_back_to_single_bytes(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, clock=self.clock)
        original = servo.read

        def read(addr, length):
            if length > 1:
                raise fb.FeetechBusError('fake: block read refused')
            if addr == 37:
                raise fb.FeetechBusError('fake: one bad byte')
            return original(addr, length)
        servo.read = read
        h = self._bench(servo)
        _path, dump = core.referenz_lesen(h.env, h.store)
        self.assertFalse(dump['eeprom_block_read'])
        self.assertFalse(dump['ram_block_read'])
        self.assertEqual(dump['failed_addrs'], [37])
        self.assertNotIn(37, dump['regs'])

    def test_direction_ccw_cw_and_none(self):
        for drift, want in ((+400.0, 1), (-400.0, -1), (0.0, None)):
            with self.subTest(drift=drift):
                clock = FakeClock()
                servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=4000, clock=clock)
                h = _Harness([servo], clock, tempfile.mkdtemp(dir=self.tmp),
                             on_prompt={T.TURN_CCW: lambda s=servo, d=drift: setattr(s, 'hand_ticks_per_s', d)})
                path, _dump = core.referenz_lesen(h.env, h.store)
                if want is None:
                    with self.assertRaises(core.StationError) as ctx:
                        core.drehrichtung_messen(h.env, h.store, path)
                    self.assertEqual(ctx.exception.code, 'DIRECTION_NONE')
                    self.assertIsNone(json.loads(path.read_text(encoding='utf-8'))['tick_direction'])
                    self.assertLessEqual(clock() - 1000.0, 16.0)
                else:
                    self.assertEqual(core.drehrichtung_messen(h.env, h.store, path), want)
                    self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['tick_direction'], want)
                self.assertEqual(servo.writes, [])

    def test_fahrzeit_messen_writes_only_ram_and_saves_the_times(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1000, clock=self.clock)
        h = self._bench(servo)
        path, _dump = core.referenz_lesen(h.env, h.store)
        times = core.fahrzeit_messen(h.env, h.store, path)
        self.assertEqual(len(times), 2)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['motion_ms'], times)
        self.assertEqual(servo.eeprom_writes(), [])
        self.assertEqual(servo.mem[fb.REG_TORQUE_ENABLE], 0)
        # still fresh: the reference is not spoilt by the measurement
        self.assertEqual(servo.mem[5], 1)

    def test_fahrzeit_messen_refuses_an_error_bit(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1000, clock=self.clock)
        h = self._bench(servo)
        path, _dump = core.referenz_lesen(h.env, h.store)
        servo.status_while_moving = fb.ERR_OVERCURRENT
        with self.assertRaises(core.StationError) as ctx:
            core.fahrzeit_messen(h.env, h.store, path)
        self.assertEqual(ctx.exception.code, 'STATUS')
        self.assertIsNone(json.loads(path.read_text(encoding='utf-8'))['motion_ms'])
        self.assertEqual(servo.mem[fb.REG_TORQUE_ENABLE], 0)

    def test_a_reference_whose_model_bytes_failed_is_still_saved(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, clock=self.clock)
        original = servo.read

        def read(addr, length):
            if length > 1 or addr in (3, 4):
                raise fb.FeetechBusError('fake: refused')
            return original(addr, length)
        servo.read = read
        h = self._bench(servo)
        path, dump = core.referenz_lesen(h.env, h.store)
        self.assertIsNone(dump['model'])
        self.assertTrue(path.name.endswith('-unknown-id1.json'))

    def test_fahrzeit_messen_refuses_a_parked_shaft(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=20, clock=self.clock)
        h = self._bench(servo)
        path, _dump = core.referenz_lesen(h.env, h.store)
        with self.assertRaises(core.StationError) as ctx:
            core.fahrzeit_messen(h.env, h.store, path)
        self.assertEqual(ctx.exception.code, 'EDGE_PARKED')
        self.assertEqual(servo.writes, [])

    def test_referenz_works_while_the_spec_is_unpinned(self):
        self.assertFalse(fss.V1.pinned)
        servo = FakeServo(fb.STS3215_MODEL_NUMBER, clock=self.clock)
        h = self._bench(servo)
        path, _dump = core.referenz_lesen(h.env, h.store)
        self.assertTrue(path.exists())


def _dump(model, *, direction=1, motion=(170, 168), regs=None, eeprom_ok=True, ram_ok=True):
    base = FakeServo(model).mem
    values = {a: base[a] for a in range(87)}
    values.update(regs or {})
    return {'model': model, 'firmware': '3.10', 'baud_index': 0, 'id': values[5],
            'eeprom_block_read': eeprom_ok, 'ram_block_read': ram_ok,
            'regs': {str(a): v for a, v in values.items()}, 'failed_addrs': [],
            'tick_direction': direction, 'motion_ms': list(motion) if motion else None}


class TestP0Report(_TmpCase):
    def setUp(self):
        super().setUp()
        self.store = records.Store(pathlib.Path(self.tmp))

    def _put(self, name, dump):
        self.store.reference_dir.mkdir(parents=True, exist_ok=True)
        (self.store.reference_dir / name).write_text(json.dumps(dump), encoding='utf-8')

    def _three_each(self, **overrides_2825):
        for n in range(3):
            self._put(f'2026-10-27T0{n}-777-id1.json', _dump(777, motion=(170 + n, 168)))
            self._put(f'2026-10-27T1{n}-2825-id1.json', _dump(2825, **overrides_2825))

    def _codes(self):
        with self.assertRaises(core.StationError) as ctx:
            core.p0_bericht(self.store)
        return ctx.exception.data['findings'], ctx.exception

    def test_it_needs_three_per_model(self):
        for n in range(3):
            self._put(f'a{n}-777-id1.json', _dump(777))
        self._put('b0-2825-id1.json', _dump(2825))
        codes, err = self._codes()
        self.assertEqual(codes, ['P0_TOO_FEW'])
        self.assertIn('STS3250', err.message_de)
        self.assertFalse(self.store.p0_report_path.exists())

    def test_a_valid_set_proposes_directions_and_exactly_the_factory_keys(self):
        self._three_each(direction=-1)
        report_path, snippet_path = core.p0_bericht(self.store)
        report = json.loads(report_path.read_text(encoding='utf-8'))
        self.assertEqual(report['models']['777']['tick_direction'], 1)
        self.assertEqual(report['models']['2825']['tick_direction'], -1)
        keys = sorted(int(k) for k in report['models']['2825']['factory'])
        self.assertEqual(keys, sorted(records.P0_FACTORY_KEYS))
        self.assertEqual(sorted(records.P0_FACTORY_KEYS), sorted(fss.FACTORY[777]))
        self.assertEqual(report['models']['2825']['factory']['28'], fss.FACTORY[2825][28])
        self.assertEqual(report['models']['777']['motion_test_window_ms'], [round(0.6 * 168), round(1.5 * 172)])
        snippet = snippet_path.read_text(encoding='utf-8')
        self.assertIn('TICK_DIRECTION = {777: 1, 2825: -1}', snippet)
        self.assertIn('MAX_TORQUE', snippet)
        ns = {}
        exec(compile(snippet, 'snippet', 'exec'), ns)  # the snippet is valid Python
        self.assertEqual(ns['TICK_DIRECTION'], {777: 1, 2825: -1})
        self.assertEqual(ns['FACTORY'][2825], fss.FACTORY[2825])
        self.assertEqual(ns['MOTION_TEST_WINDOW_MS'][777], (round(0.6 * 168), round(1.5 * 172)))

    def test_freshness_is_rechecked_on_every_file(self):
        self._three_each()
        self._put('copied-by-hand-2825-id2.json', _dump(2825, regs={5: 2}))
        codes, err = self._codes()
        self.assertIn('P0_NOT_FRESH', codes)
        self.assertIn('copied-by-hand-2825-id2.json', err.message_de)

    def test_a_disagreeing_factory_key_is_flagged(self):
        self._three_each()
        self._put('odd-2825-id1.json', _dump(2825, regs={28: 54, 29: 1}))   # 310
        codes, err = self._codes()
        self.assertEqual(codes, ['P0_DISAGREE'])
        self.assertIn('28', err.message_de)

    def test_a_difference_at_the_offset_or_elsewhere_is_ignored(self):
        self._three_each()
        self._put('odd-2825-id1.json', _dump(2825, regs={31: 5, 32: 0, 37: 9, 56: 7}))
        report_path, _s = core.p0_bericht(self.store)
        self.assertTrue(report_path.exists())

    def test_an_eeprom_block_fallback_refuses_a_ram_block_fallback_does_not(self):
        self._three_each()
        self._put('ram-2825-id1.json', _dump(2825, ram_ok=False))
        core.p0_bericht(self.store)
        self._put('eeprom-2825-id1.json', _dump(2825, eeprom_ok=False))
        codes, _err = self._codes()
        self.assertEqual(codes, ['P0_FALLBACK'])

    def test_disagreeing_directions_refuse(self):
        self._three_each()
        self._put('other-2825-id1.json', _dump(2825, direction=-1))
        codes, _err = self._codes()
        self.assertEqual(codes, ['P0_DIRECTION_DISAGREE'])


class _CalibratedCase(_TmpCase):
    def _calibrated(self, serial='V1S-000123', joint_index=1, raw=1834, cap='KAPPE-J15-A'):
        servo = FakeServo(fss.MODELS[joint_index], raw=raw, lock_default=1, clock=self.clock)
        h = _Harness([servo], self.clock, self.tmp,
                     on_prompt={T.REPLUG_PULL: lambda: servo.pull_power(off_polls=5)})
        _kalibrieren(h, serial=serial, joint_index=joint_index, cap=cap)
        servo.writes.clear()
        return servo, h


class TestPruefen(_CalibratedCase):
    def test_a_calibrated_servo_passes_read_only(self):
        servo, h = self._calibrated()
        message = core.pruefen(h.env, h.store, serial='V1S-000123', spec=SPEC_A)
        self.assertEqual(message, T.CHECK_PASSED.format(serial='V1S-000123', joint=SPEC_A.joints[1].label_de))
        self.assertEqual(servo.writes, [])

    def test_a_turned_horn_fails_naming_the_position(self):
        servo, h = self._calibrated()
        servo.raw += 100
        with self.assertRaises(core.StationError) as ctx:
            core.pruefen(h.env, h.store, serial='V1S-000123', spec=SPEC_A)
        self.assertEqual(ctx.exception.code, 'CHECK_FAILED')
        self.assertIn('Position 1124 statt 1024', ctx.exception.message_de)
        self.assertEqual(servo.writes, [])

    def test_without_a_local_record_it_says_so_and_judges_the_rest(self):
        servo, h = self._calibrated()
        h.store.record_path('V1S-000123').unlink()
        core.pruefen(h.env, h.store, serial='V1S-000123', spec=SPEC_A)
        self.assertIn(T.CHECK_NO_RECORD.format(serial='V1S-000123'), h.texts('note'))

    def test_a_different_record_offset_and_a_foreign_offset_are_findings(self):
        servo, h = self._calibrated()
        servo.mem[31:33] = fb.le16(0x1805)
        with self.assertRaises(core.StationError) as ctx:
            core.pruefen(h.env, h.store, serial='V1S-000123', spec=SPEC_A)
        message = ctx.exception.message_de
        self.assertIn('fremder Kodierung', message)
        self.assertIn('laut Datensatz', message)
        self.assertIn(T.CHECK_RECORD_BLOCK, message)

    def test_the_unpinned_spec_refuses(self):
        servo, h = self._calibrated()
        with self.assertRaises(core.StationError) as ctx:
            core.pruefen(h.env, h.store, serial='V1S-000123', spec=fss.V1)
        self.assertEqual(ctx.exception.code, 'SPEC_NOT_PINNED')


class TestLehrePruefen(_CalibratedCase):
    def _check(self, h, serial, cap='KAPPE-J15-A'):
        return core.lehre_pruefen(h.env, h.store, serial=serial, cap_label=cap,
                                  local_date='2026-10-27', spec=SPEC_A)

    def test_no_master_and_no_record_refuses(self):
        h = _Harness([], self.clock, self.tmp)
        with self.assertRaises(core.StationError) as ctx:
            self._check(h, 'V1S-000999')
        self.assertEqual(ctx.exception.code, 'MASTER_NEEDS_RECORD')
        self.assertEqual(h.store.masters(), {})

    def test_the_first_calibrated_serial_becomes_the_master_without_a_bus_call(self):
        servo, h = self._calibrated()
        opened = len(h.port.opened)
        self.assertEqual(h.store.gauge_state('KAPPE-J15-A', 'J15', '2026-10-27'), ('no_master', None))
        message = self._check(h, 'V1S-000123')
        self.assertEqual(message, T.MASTER_SET.format(serial='V1S-000123', cap='J15'))
        self.assertEqual(h.store.masters(), {'J15': 'V1S-000123'})
        self.assertEqual(len(h.port.opened), opened)
        self.assertEqual(h.store.gauge_state('KAPPE-J15-A', 'J15', '2026-10-27'), ('not_today', None))

    def test_a_record_of_the_other_cap_type_never_becomes_the_master(self):
        servo, h = self._calibrated()
        with self.assertRaises(core.StationError) as ctx:
            self._check(h, 'V1S-000123', cap='KAPPE-GR-A')
        self.assertEqual(ctx.exception.code, 'MASTER_NEEDS_RECORD')

    def test_the_master_reading_its_tick_passes_and_allows_today(self):
        servo, h = self._calibrated()
        self._check(h, 'V1S-000123')
        message = self._check(h, 'V1S-000123')
        self.assertEqual(message, T.GAUGE_PASSED.format(cap='KAPPE-J15-A', present=1024, want=1024))
        state, at = h.store.gauge_state('KAPPE-J15-A', 'J15', '2026-10-27')
        self.assertEqual(state, 'passed')
        self.assertTrue(at)
        self.assertEqual(h.store.gauge_state('KAPPE-J15-A', 'J15', '2026-10-28'), ('not_today', None))
        self.assertEqual(h.store.gauge_state('KAPPE-J15-B', 'J15', '2026-10-27'), ('not_today', None))
        self.assertEqual(servo.writes, [])

    def test_a_moved_gauge_fails_and_a_later_failure_withdraws_today(self):
        servo, h = self._calibrated()
        self._check(h, 'V1S-000123')
        self._check(h, 'V1S-000123')
        servo.raw += 5
        with self.assertRaises(core.StationError) as ctx:
            self._check(h, 'V1S-000123')
        self.assertEqual(ctx.exception.code, 'GAUGE_FAILED')
        self.assertEqual(h.store.gauge_state('KAPPE-J15-A', 'J15', '2026-10-27'), ('not_today', None))
        rows = records.read_csv(h.store.gauge_checks_csv)
        self.assertEqual([r['passed'] for r in rows], ['1', '0'])

    def test_another_serial_is_not_the_master(self):
        servo, h = self._calibrated()
        self._check(h, 'V1S-000123')
        with self.assertRaises(core.StationError) as ctx:
            self._check(h, 'V1S-000124')
        self.assertEqual(ctx.exception.code, 'MASTER_MISMATCH')

    def test_another_servo_in_the_gauge_is_refused(self):
        servo, h = self._calibrated()
        self._check(h, 'V1S-000123')
        servo.mem[21] = 7                     # not the master's EEPROM any more
        with self.assertRaises(core.StationError) as ctx:
            self._check(h, 'V1S-000123')
        self.assertEqual(ctx.exception.code, 'MASTER_WRONG_SERVO')
        self.assertEqual(records.read_csv(h.store.gauge_checks_csv), [])


class TestSameImageAsProvision(_TmpCase):
    """tools/feetech_provision.py and the station leave byte-equal 40-byte
    blocks per joint (same raw, firmware, model) — the one golden image."""

    RAWS = (1500, 1834, 2500, 3000, 700, 2200)

    def test_byte_equal_blocks(self):
        spec = importlib.util.spec_from_file_location('feetech_provision_station', sf.PROVISION_PATH)
        prov = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(prov)
        prov.apply_arm_spec('v1')
        clock = FakeClock()
        jigged = [FakeServo(fss.MODELS[i], servo_id=i + 1, raw=self.RAWS[i], clock=clock) for i in range(6)]
        for servo in jigged:
            servo.mem[fb.REG_PHASE] |= 0x10
        bus = sf.FakeServoBus(jigged, 1_000_000)
        with contextlib.redirect_stdout(io.StringIO()):
            prov.provision(bus, 'V1-0001', prov._ARM_SPECS['v1'], servo_spec=SPEC_A)
        for i in range(6):
            with self.subTest(joint=i):
                servo = FakeServo(fss.MODELS[i], raw=self.RAWS[i], clock=clock, lock_default=1)
                servo.mem[fb.REG_PHASE] |= 0x10
                h = _Harness([servo], clock, tempfile.mkdtemp(dir=self.tmp),
                             on_prompt={T.REPLUG_PULL: lambda s=servo: s.pull_power(off_polls=4)})
                cap = 'KAPPE-GR-A' if i == 5 else 'KAPPE-J15-A'
                _kalibrieren(h, serial=f'V1S-00010{i}', joint_index=i, cap=cap)
                self.assertEqual(servo.block(), jigged[i].block())


_GOAL_BLOCK = frozenset(range(fb.REG_ACCELERATION, fb.REG_TORQUE_LIMIT))       # 41..47


class TestMotionTestRefusals(_TmpCase):
    """The motion test (Wareneingang, „Fahrzeit messen") energises a servo: it
    refuses a servo outside position mode, seeds Goal_Position = Present before
    any other goal write or torque-on, and every refusal is fenced (V1-F1, F8)."""

    def _wareneingang(self, servo):
        h = _Harness([servo], self.clock, self.tmp)
        with self.assertRaises(core.StationError) as ctx:
            core.wareneingang(h.env, h.store, serial='V1S-000200', spec=SPEC_A)
        return h, ctx.exception

    def test_a_servo_outside_position_mode_is_refused_with_zero_writes(self):
        for mode in (1, 2, 3):
            with self.subTest(mode=mode):
                servo = _motion_servo(self.clock)
                servo.mem[fb.REG_OPERATING_MODE] = mode
                h, err = self._wareneingang(servo)
                self.assertEqual(err.code, 'MOTION_MODE')
                self.assertEqual(err.message_de, T.MOTION_MODE.format(mode=mode))
                self.assertEqual(servo.writes, [])
                self.assertIsNone(h.store.load_incoming('V1S-000200'))

    def test_fahrzeit_messen_refuses_a_servo_outside_position_mode(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1000, clock=self.clock)
        h = _Harness([servo], self.clock, self.tmp)
        path, _dump = core.referenz_lesen(h.env, h.store)
        servo.mem[fb.REG_OPERATING_MODE] = 1
        with self.assertRaises(core.StationError) as ctx:
            core.fahrzeit_messen(h.env, h.store, path)
        self.assertEqual(ctx.exception.code, 'MOTION_MODE')
        self.assertEqual(servo.writes, [])

    def test_goal_position_equals_present_is_the_first_goal_write_and_precedes_torque_on(self):
        servo = _motion_servo(self.clock, raw=1000)
        h = _Harness([servo], self.clock, self.tmp)
        core.wareneingang(h.env, h.store, serial='V1S-000200', spec=SPEC_A)
        energising = [n for n, (addr, data) in enumerate(servo.writes)
                      if set(range(addr, addr + len(data))) & _GOAL_BLOCK
                      or (addr == fb.REG_TORQUE_ENABLE and data == bytes([fb.TORQUE_ENABLE_ON]))]
        first = servo.writes[energising[0]]
        self.assertEqual(first, (fb.REG_GOAL_POSITION, fb.le16(1000)))
        before = servo.writes[:energising[0]]
        self.assertEqual([addr for addr, _d in before], [fb.REG_TORQUE_ENABLE, fb.REG_TORQUE_LIMIT])
        self.assertEqual(before[0][1], bytes([fb.TORQUE_ENABLE_OFF]))

    def test_a_servo_that_never_arrives_is_motion_not_arrived(self):
        servo = _motion_servo(self.clock, ticks_per_s=400.0)       # 512 ticks take 1.28 s
        h, err = self._wareneingang(servo)
        self.assertEqual(err.code, 'MOTION_NOT_ARRIVED')
        self.assertEqual(h.store.load_incoming('V1S-000200')['reason_code'], 'MOTION_NOT_ARRIVED')
        self.assertEqual(servo.mem[fb.REG_TORQUE_ENABLE], 0)

    def test_a_ram_write_that_does_not_stick_refuses_and_ends_limp(self):
        servo = _motion_servo(self.clock)
        original = servo.write

        def write(addr, data):
            if addr == fb.REG_TORQUE_LIMIT and data != fb.le16(1000):
                servo.writes.append((addr, bytes(data)))
                return
            original(addr, data)
        servo.write = write
        h, err = self._wareneingang(servo)
        self.assertEqual(err.code, 'WRITE_FAILED')
        self.assertIn('Drehmomentgrenze (Register 48)', err.message_de)
        self.assertEqual(servo.mem[fb.REG_TORQUE_ENABLE], 0)
        self.assertNotIn(fb.REG_GOAL_POSITION, {a for a, _d in servo.writes})

    def test_another_servo_than_the_reference_is_refused(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, raw=1000, clock=self.clock)
        h = _Harness([servo], self.clock, self.tmp)
        path, _dump = core.referenz_lesen(h.env, h.store)
        other = FakeServo(fb.STS3215_MODEL_NUMBER, raw=1000, clock=self.clock)
        h.port.servos[:] = [other]
        for run in (core.fahrzeit_messen, core.drehrichtung_messen):
            with self.subTest(run=run.__name__):
                with self.assertRaises(core.StationError) as ctx:
                    run(h.env, h.store, path)
                self.assertEqual(ctx.exception.code, 'NEED_REFERENCE')
                self.assertEqual(other.writes, [])


class TestReadOnlyChecksJudgeTheRawRegister(_CalibratedCase):
    """Prüfen and „Lehre prüfen" compare Present linearly inside 0–4095, as the
    boot probe does: a firmware that does not fold Present (T0 + 4096) never
    passes (V1-F2). And their remaining refusals are fenced (V1-F1)."""

    def test_pruefen_lists_an_unfolded_position(self):
        servo, h = self._calibrated(raw=3500)
        servo.folds_present = False
        with self.assertRaises(core.StationError) as ctx:
            core.pruefen(h.env, h.store, serial='V1S-000123', spec=SPEC_A)
        self.assertEqual(ctx.exception.code, 'CHECK_FAILED')
        self.assertIn('Position 5120 statt 1024', ctx.exception.message_de)

    def test_pruefen_refuses_an_id_of_no_joint(self):
        servo = FakeServo(fb.STS3250_MODEL_NUMBER, servo_id=9, raw=1834, clock=self.clock)
        h = _Harness([servo], self.clock, self.tmp)
        with self.assertRaises(core.StationError) as ctx:
            core.pruefen(h.env, h.store, serial='V1S-000123', spec=SPEC_A)
        self.assertEqual(ctx.exception.code, 'CHECK_FAILED')
        self.assertIn(T.CHECK_BAD_ID.format(id=9), ctx.exception.message_de)

    def _check(self, h, serial='V1S-000123', cap='KAPPE-J15-A'):
        return core.lehre_pruefen(h.env, h.store, serial=serial, cap_label=cap,
                                  local_date='2026-10-27', spec=SPEC_A)

    def test_the_master_reading_an_unfolded_tick_fails_the_gauge(self):
        servo, h = self._calibrated(raw=3500)
        self._check(h)
        servo.folds_present = False
        with self.assertRaises(core.StationError) as ctx:
            self._check(h)
        self.assertEqual(ctx.exception.code, 'GAUGE_FAILED')
        self.assertEqual([r['passed'] for r in records.read_csv(h.store.gauge_checks_csv)], ['0'])

    def test_a_cap_label_of_no_cap_type_is_refused(self):
        h = _Harness([], self.clock, self.tmp)
        with self.assertRaises(core.StationError) as ctx:
            self._check(h, cap='KAPPE-XX-1')
        self.assertEqual(ctx.exception.code, 'BAD_CAP')
        self.assertEqual(h.port.opened, [])

    def test_the_master_without_its_record_is_refused(self):
        servo, h = self._calibrated()
        self._check(h)
        h.store.record_path('V1S-000123').unlink()
        with self.assertRaises(core.StationError) as ctx:
            self._check(h)
        self.assertEqual(ctx.exception.code, 'MASTER_NEEDS_RECORD')
        self.assertEqual(records.read_csv(h.store.gauge_checks_csv), [])


class TestProvisionJudgesTheRawRegister(_TmpCase):
    """tools/feetech_provision.py's two jig checks compare Present linearly
    inside 0–4095 (V1-F2): a firmware that does not fold reads T0 + 4096 on a
    joint whose offset crosses the seam, and the boot probe would refuse it."""

    RAWS = (1500, 3500, 2500, 3000, 700, 2200)       # only joint 2 crosses the seam (offset −1620)

    def _provision(self, servo_class=FakeServo, **kw):
        spec = importlib.util.spec_from_file_location('feetech_provision_raw', sf.PROVISION_PATH)
        prov = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(prov)
        prov.apply_arm_spec('v1')
        servos = [servo_class(fss.MODELS[i], servo_id=i + 1, raw=self.RAWS[i], clock=self.clock, **kw)
                  for i in range(6)]
        bus = sf.FakeServoBus(servos, 1_000_000)
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                prov.provision(bus, 'V1-0001', prov._ARM_SPECS['v1'], servo_spec=SPEC_A)
        return str(ctx.exception)

    def test_the_offset_postcondition_refuses_an_unfolded_present(self):
        message = self._provision(folds_present=False)
        self.assertIn('Servo 2', message)
        self.assertIn('5120 statt 1024', message)

    def test_the_final_jig_check_refuses_an_unfolded_present(self):
        class _UnfoldsAfterLock(FakeServo):
            def write(self, addr, data):
                super().write(addr, data)
                if addr == fb.REG_LOCK and bytes(data) == b'\x01':
                    self.folds_present = False
        message = self._provision(_UnfoldsAfterLock)
        self.assertIn('Jig-Endkontrolle', message)
        self.assertIn('Servo 2 liest 5120 statt 1024', message)


if __name__ == '__main__':
    unittest.main()

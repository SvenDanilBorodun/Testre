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

    def __init__(self, servos, clock, tmp, on_prompt=None, noise_by_baud=None):
        self.clock = clock
        self.port = FakePort(servos, noise_by_baud)
        self.events = []
        self.step_marks = {}
        self.servos = self.port.servos
        self.on_prompt = on_prompt or {}
        self.store = records.Store(pathlib.Path(tmp))
        self.env = core.Env(self.port, clock=clock, sleep=clock.sleep,
                            notify=self._notify, utcnow=lambda: _UTC)

    def _notify(self, kind, text='', **data):
        self.events.append((kind, text, data))
        if kind == 'step' and data.get('state') == 'running':
            self.step_marks[data['step']] = sum(len(s.writes) for s in self.servos)
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

    def test_a_servo_at_57600_is_found_there(self):
        servo = FakeServo(baud_index=fb.BAUD_RATES.index(57_600), clock=self.clock)
        found, h = self._discover([servo])
        self.assertEqual(found, core.Found(57_600, 1))
        self.assertEqual(h.port.opened, list(fb.BAUD_RATES[:fb.BAUD_RATES.index(57_600) + 1]))

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


if __name__ == '__main__':
    unittest.main()

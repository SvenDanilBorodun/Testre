"""The Servo-Station's UI state machine (``tools/servo_station/station_controller.py``):
modes, batch, cap, operator, the scanner input and the busy flag — no I/O, so
every rule the operator meets is tested here without a window. Deps-free.
"""

import unittest

from servo_station_fakes import SPEC_A, fss

import station_controller as sc  # noqa: E402
import station_texts_de as T  # noqa: E402


class _Facts:
    def __init__(self, state=('passed', '2026-10-27T07:00:00Z')):
        self.state = state
        self.asked = []

    def gauge_state(self, cap_label, cap_type):
        self.asked.append((cap_label, cap_type))
        return self.state


def _ready(facts=None, batch=1, cap='KAPPE-J15-A'):
    c = sc.Controller(SPEC_A, facts or _Facts())
    c.set_mode(sc.MODE_CALIBRATE)
    if batch is not None:
        c.set_batch(batch)
    c.set_operator('Anna')
    if cap:
        c.scan(cap)
    return c


class TestModes(unittest.TestCase):
    def test_an_unpinned_spec_enables_only_referenz(self):
        c = sc.Controller(fss.V1, _Facts())
        self.assertEqual(c.enabled_modes(), (sc.MODE_REFERENCE,))
        self.assertEqual(c.mode, sc.MODE_REFERENCE)
        for mode in (sc.MODE_INCOMING, sc.MODE_CALIBRATE, sc.MODE_CHECK, sc.MODE_GAUGE):
            self.assertFalse(c.set_mode(mode))
            self.assertEqual(c.mode, sc.MODE_REFERENCE)
            self.assertEqual(c.status, T.SPEC_NOT_PINNED)

    def test_a_pinned_spec_enables_all_five(self):
        c = sc.Controller(SPEC_A, _Facts())
        self.assertEqual(c.enabled_modes(), sc.MODES)
        self.assertEqual([sc.MODE_LABELS[m] for m in sc.MODES],
                         [T.MODE_REFERENZ, T.MODE_WARENEINGANG, T.MODE_KALIBRIEREN, T.MODE_PRUEFEN, T.MODE_LEHRE])

    def test_the_batch_entries_name_joint_and_model(self):
        c = sc.Controller(SPEC_A, _Facts())
        self.assertEqual(c.batch_entries()[1], 'Gelenk 2 – Schulter – STS3250')
        self.assertEqual(len(c.batch_entries()), 6)


class TestScanner(unittest.TestCase):
    def test_a_us_layout_scanner_on_a_german_keyboard_is_understood(self):
        self.assertEqual(sc.normalise_scan(' V1Sß000123\r\n'), 'V1S-000123')
        c = _ready(cap=None)
        c.scan('KAPPEßJ15ßA')
        self.assertEqual(c.cap, 'KAPPE-J15-A')
        action = c.scan('V1Sß000123')
        self.assertEqual(action.params['serial'], 'V1S-000123')

    def test_a_scanned_cap_is_confirmed(self):
        c = _ready(cap=None)
        self.assertIsNone(c.scan('KAPPE-J15-B'))
        self.assertEqual(c.cap, 'KAPPE-J15-B')
        self.assertEqual(c.status, T.CAP_SET.format(cap='KAPPE-J15-B'))

    def test_cap_labels_with_y_or_z_are_refused(self):
        c = _ready(cap=None)
        for text in ('KAPPE-J15-Y', 'KAPPE-GR-Z', 'KAPPE-J15-a', 'KAPPE-J15-ABCDE'):
            with self.subTest(text=text):
                self.assertIsNone(c.scan(text))
                self.assertEqual(c.status, T.BAD_CAP.format(text=text))
                self.assertIsNone(c.cap)

    def test_a_bad_serial(self):
        c = _ready()
        self.assertIsNone(c.scan('V1S-12345'))
        self.assertEqual(c.status, T.BAD_SERIAL.format(text='V1S-12345'))

    def test_the_patterns_are_the_specs(self):
        self.assertEqual(sc.SERIAL_RE.pattern, fss.SERVO_SERIAL_PATTERN)
        self.assertEqual(sc.CAP_RE.pattern, fss.CAP_LABEL_PATTERN)


class TestKalibrierenPreconditions(unittest.TestCase):
    def test_serial_before_batch(self):
        c = _ready(batch=None, cap=None)
        self.assertIsNone(c.scan('V1S-000123'))
        self.assertEqual(c.status, T.NEED_BATCH)

    def test_serial_before_operator(self):
        c = sc.Controller(SPEC_A, _Facts())
        c.set_mode(sc.MODE_CALIBRATE)
        c.set_batch(1)
        self.assertIsNone(c.scan('V1S-000123'))
        self.assertEqual(c.status, T.NEED_OPERATOR)

    def test_serial_before_cap(self):
        c = _ready(cap=None)
        self.assertIsNone(c.scan('V1S-000123'))
        self.assertEqual(c.status, T.NEED_CAP)

    def test_a_wrong_cap_type_is_refused(self):
        c = _ready(cap=None)
        self.assertIsNone(c.scan('KAPPE-GR-A'))
        self.assertEqual(c.status, T.CAP_MISMATCH.format(joint='Gelenk 2 – Schulter', want='J15'))
        self.assertIsNone(c.cap)
        # a batch switched after the cap was scanned is caught at the serial
        c = _ready(batch=1, cap='KAPPE-J15-A')
        c.set_batch(5)
        self.assertIsNone(c.scan('V1S-000123'))
        self.assertEqual(c.status, T.CAP_MISMATCH.format(joint='Greifer', want='GR'))

    def test_no_gauge_check_today(self):
        facts = _Facts(state=('not_today', None))
        c = _ready(facts)
        self.assertIsNone(c.scan('V1S-000123'))
        self.assertEqual(c.status, T.NEED_GAUGE_CHECK.format(cap='KAPPE-J15-A'))
        self.assertEqual(facts.asked[-1], ('KAPPE-J15-A', 'J15'))

    def test_a_passed_gauge_check_starts_the_run(self):
        c = _ready()
        action = c.scan('V1S-000123')
        self.assertEqual(action.kind, 'calibrate')
        self.assertEqual(action.params, {'serial': 'V1S-000123', 'joint_index': 1, 'cap_label': 'KAPPE-J15-A',
                                         'operator': 'Anna', 'gauge_check_at': '2026-10-27T07:00:00Z',
                                         'warnings': []})

    def test_no_master_yet_runs_with_the_bootstrap_warning(self):
        c = _ready(_Facts(state=('no_master', None)))
        action = c.scan('V1S-000123')
        self.assertEqual(action.params['warnings'], [T.NO_MASTER_WARN.format(cap='J15')])
        self.assertIsNone(action.params['gauge_check_at'])
        self.assertIn(T.NO_MASTER_WARN.format(cap='J15'), c.status)


class TestBusyAndMark(unittest.TestCase):
    def test_a_scan_while_busy(self):
        c = _ready()
        action = c.scan('V1S-000123')
        c.started(action)
        self.assertIsNone(c.scan('V1S-000124'))
        self.assertEqual(c.status, T.BUSY)
        self.assertIsNone(c.enter())
        self.assertFalse(c.set_mode(sc.MODE_CHECK))
        self.assertIsNone(c.press('reference'))

    def test_after_l_the_next_scan_confirms_m_and_starts_a_run(self):
        c = _ready()
        action = c.scan('V1S-000123')
        c.started(action)
        c.finished(action, True, T.RECORD_SAVED.format(path='x'))
        self.assertFalse(c.busy)
        self.assertEqual(c.status, T.MARK_PROMPT.format(colour='gelb', cap='KAPPE-J15-A'))
        self.assertEqual(c.mark_colour, '#FFD400')
        nxt = c.scan('V1S-000124')
        self.assertIsNone(c.mark_colour)
        self.assertEqual(nxt.kind, 'calibrate')
        self.assertEqual(nxt.params['serial'], 'V1S-000124')
        self.assertIn(T.DONE.format(serial='V1S-000123', joint='Gelenk 2 – Schulter'), c.log)

    def test_enter_confirms_the_mark(self):
        c = _ready()
        action = c.scan('V1S-000123')
        c.started(action)
        c.finished(action, True, 'ok')
        self.assertIsNone(c.enter())
        self.assertEqual(c.status, T.DONE.format(serial='V1S-000123', joint='Gelenk 2 – Schulter'))
        self.assertIsNone(c.mark_colour)

    def test_a_failed_run_shows_its_sentence_and_asks_no_mark(self):
        c = _ready()
        action = c.scan('V1S-000123')
        c.started(action)
        c.finished(action, False, T.NO_REAL_RESTART)
        self.assertEqual(c.status, T.NO_REAL_RESTART)
        self.assertIsNone(c.mark_colour)


class TestOtherModes(unittest.TestCase):
    def test_wareneingang_and_pruefen_start_on_a_serial(self):
        for mode, kind in ((sc.MODE_INCOMING, 'incoming'), (sc.MODE_CHECK, 'check')):
            c = sc.Controller(SPEC_A, _Facts())
            c.set_mode(mode)
            self.assertEqual(c.scan('V1S-000123'), sc.Action(kind, {'serial': 'V1S-000123'}))

    def test_lehre_pruefen_needs_the_cap_first(self):
        c = sc.Controller(SPEC_A, _Facts())
        c.set_mode(sc.MODE_GAUGE)
        self.assertIsNone(c.scan('V1S-000123'))
        self.assertEqual(c.status, T.NEED_CAP)
        c.scan('KAPPE-GR-B2')
        self.assertEqual(c.scan('V1S-000123'),
                         sc.Action('gauge', {'serial': 'V1S-000123', 'cap_label': 'KAPPE-GR-B2'}))

    def test_referenz_buttons(self):
        c = sc.Controller(fss.V1, _Facts())
        self.assertIsNone(c.press('direction'))
        self.assertEqual(c.status, T.NEED_REFERENCE)
        self.assertIsNone(c.press('motion'))
        self.assertEqual(c.press('p0_report'), sc.Action('p0_report', {}))
        action = c.press('reference')
        self.assertEqual(action, sc.Action('reference', {}))
        c.started(action)
        c.finished(action, True, 'gespeichert', result='/tmp/ref.json')
        self.assertEqual(c.press('direction'), sc.Action('direction', {'path': '/tmp/ref.json'}))
        self.assertEqual(c.press('motion'), sc.Action('motion', {'path': '/tmp/ref.json'}))

    def test_a_reference_button_outside_referenz_does_nothing(self):
        c = sc.Controller(SPEC_A, _Facts())
        c.set_mode(sc.MODE_CHECK)
        self.assertIsNone(c.press('reference'))

    def test_a_serial_in_referenz_starts_nothing(self):
        c = sc.Controller(fss.V1, _Facts())
        self.assertIsNone(c.scan('V1S-000123'))
        self.assertEqual(c.status, T.HINT_REFERENZ)


if __name__ == '__main__':
    unittest.main()

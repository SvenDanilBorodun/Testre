"""The Servo-Station's UI state machine: modes, batch, cap, operator, the
scanner input and the busy flag. No I/O — the window feeds it what the operator
did and runs the :class:`Action` it returns on the worker thread; the gauge
state comes from an injected ``facts`` object (``gauge_state(cap_label,
cap_type) -> (state, at)``).

The scan is the click (design §4.2): a scanned serial starts the mode's run. A
scanner configured for a US layout types ``ß`` for ``-`` on a German Windows
layout, so ``ß`` is read as ``-`` before any pattern is matched; the cap-label
suffix excludes Y and Z, which the same mismatch swaps.
"""

from __future__ import annotations

import re
from collections import namedtuple

import feetech_servo_spec as fss
import station_texts_de as T
from station_core import cap_type_of

SERIAL_RE = re.compile(fss.SERVO_SERIAL_PATTERN)
CAP_RE = re.compile(fss.CAP_LABEL_PATTERN)

MODE_REFERENCE = 'reference'
MODE_INCOMING = 'incoming'
MODE_CALIBRATE = 'calibrate'
MODE_CHECK = 'check'
MODE_GAUGE = 'gauge'
MODES = (MODE_REFERENCE, MODE_INCOMING, MODE_CALIBRATE, MODE_CHECK, MODE_GAUGE)
MODE_LABELS = {MODE_REFERENCE: T.MODE_REFERENZ, MODE_INCOMING: T.MODE_WARENEINGANG,
               MODE_CALIBRATE: T.MODE_KALIBRIEREN, MODE_CHECK: T.MODE_PRUEFEN, MODE_GAUGE: T.MODE_LEHRE}
MODE_HINTS = {MODE_REFERENCE: T.HINT_REFERENZ, MODE_INCOMING: T.HINT_WARENEINGANG,
              MODE_CALIBRATE: T.HINT_KALIBRIEREN, MODE_CHECK: T.HINT_PRUEFEN, MODE_GAUGE: T.HINT_LEHRE}
REFERENCE_BUTTONS = ('reference', 'direction', 'motion', 'p0_report')

Action = namedtuple('Action', 'kind params')


def normalise_scan(text):
    return (text or '').strip().replace('ß', '-')


class Controller:
    def __init__(self, spec, facts):
        self.spec = spec
        self.facts = facts
        self.mode = MODE_REFERENCE
        self.batch = None
        self.cap = None
        self.operator = ''
        self.busy = False
        self.reference_path = None
        self.awaiting_mark = None
        self.mark_colour = None
        self.log = []
        self.status = MODE_HINTS[MODE_REFERENCE] if spec.pinned else T.SPEC_NOT_PINNED

    def _say(self, text):
        self.status = text
        self.log.append(text)
        return None

    # ── settings ─────────────────────────────────────────────────────────────
    def enabled_modes(self):
        return MODES if self.spec.pinned else (MODE_REFERENCE,)

    def set_mode(self, mode):
        if self.busy:
            self._say(T.BUSY)
            return False
        if mode not in self.enabled_modes():
            self._say(T.SPEC_NOT_PINNED)
            return False
        self.mode = mode
        self._say(MODE_HINTS[mode])
        return True

    def batch_entries(self):
        return [T.BATCH_ENTRY.format(label=j.label_de, model=j.model_name) for j in self.spec.joints]

    def set_batch(self, index):
        self.batch = index

    def set_operator(self, name):
        self.operator = (name or '').strip()

    # ── input ────────────────────────────────────────────────────────────────
    def _confirm_mark(self):
        serial, joint_index, _cap = self.awaiting_mark
        self.awaiting_mark = None
        self.mark_colour = None
        self._say(T.DONE.format(serial=serial, joint=self.spec.joints[joint_index].label_de))

    def enter(self):
        """Enter on an empty scan field: confirms step M."""
        if self.busy:
            return self._say(T.BUSY)
        if self.awaiting_mark is not None:
            self._confirm_mark()
        return None

    def scan(self, text):
        if self.busy:
            return self._say(T.BUSY)
        text = normalise_scan(text)
        if self.awaiting_mark is not None:
            self._confirm_mark()
            if not text:
                return None
        if CAP_RE.match(text):
            return self._take_cap(text)
        if text.upper().startswith('KAPPE'):
            return self._say(T.BAD_CAP.format(text=text))
        if not SERIAL_RE.match(text):
            return self._say(T.BAD_SERIAL.format(text=text))
        return self._serial(text)

    def press(self, button):
        """A „Referenz lesen (P0)" button."""
        if self.busy:
            return self._say(T.BUSY)
        if self.mode != MODE_REFERENCE or button not in REFERENCE_BUTTONS:
            return None
        if button in ('direction', 'motion'):
            if self.reference_path is None:
                return self._say(T.NEED_REFERENCE)
            return Action(button, {'path': self.reference_path})
        return Action(button, {})

    def _joint(self):
        return self.spec.joints[self.batch]

    def _take_cap(self, label):
        if self.mode == MODE_CALIBRATE and self.batch is not None and cap_type_of(label) != self._joint().cap:
            return self._say(T.CAP_MISMATCH.format(joint=self._joint().label_de, want=self._joint().cap))
        self.cap = label
        return self._say(T.CAP_SET.format(cap=label))

    def _serial(self, serial):
        if self.mode == MODE_REFERENCE:
            return self._say(T.HINT_REFERENZ)
        if self.mode == MODE_INCOMING:
            return Action('incoming', {'serial': serial})
        if self.mode == MODE_CHECK:
            return Action('check', {'serial': serial})
        if self.mode == MODE_GAUGE:
            if self.cap is None:
                return self._say(T.NEED_CAP)
            return Action('gauge', {'serial': serial, 'cap_label': self.cap})
        return self._calibrate(serial)

    def _calibrate(self, serial):
        if self.batch is None:
            return self._say(T.NEED_BATCH)
        if not self.operator:
            return self._say(T.NEED_OPERATOR)
        if self.cap is None:
            return self._say(T.NEED_CAP)
        joint = self._joint()
        cap_type = cap_type_of(self.cap)
        if cap_type != joint.cap:
            return self._say(T.CAP_MISMATCH.format(joint=joint.label_de, want=joint.cap))
        state, at = self.facts.gauge_state(self.cap, cap_type)
        warnings = []
        if state == 'no_master':
            warnings.append(T.NO_MASTER_WARN.format(cap=cap_type))
            self._say(warnings[0])
        elif state != 'passed':
            return self._say(T.NEED_GAUGE_CHECK.format(cap=self.cap))
        return Action('calibrate', {'serial': serial, 'joint_index': self.batch, 'cap_label': self.cap,
                                    'operator': self.operator, 'gauge_check_at': at if state == 'passed' else None,
                                    'warnings': warnings})

    # ── the run's life ───────────────────────────────────────────────────────
    def started(self, _action):
        self.busy = True

    def finished(self, action, ok, message, result=None):
        self.busy = False
        self._say(message)
        if not ok:
            return
        if action.kind == 'reference':
            self.reference_path = result
        if action.kind == 'calibrate':
            index = action.params['joint_index']
            self.awaiting_mark = (action.params['serial'], index, action.params['cap_label'])
            self.mark_colour = fss.COLOUR_HEX[index]
            self._say(T.MARK_PROMPT.format(colour=fss.COLOUR_DE[index], cap=action.params['cap_label']))

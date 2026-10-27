"""The Servo-Station's bus steps: every run the operator starts, as plain
functions over an injected bus factory, clock and sleep (spec §5A).

What the station writes is every V1 servo's hardware safety floor — the EEPROM
window, Max_Torque, the protection current, position mode — from the ONE servo
spec (``feetech_servo_spec``) the arm's boot probe compares against, so the
rules here are load-bearing:

* only Torque_Enable 0/1 is ever written (``FeetechBus.write`` refuses anything
  else before framing; the fakes do the same);
* Kalibrieren never writes Goal_Position, speed or acceleration (addr 41–47) and
  writes the position limits (9–12) only in step I, after the offset;
* the ONLY Goal_Position writes are the Wareneingang / „Fahrzeit messen" motion
  test, which refuses a shaft parked within 40 ticks of 0/4095 (the driver's
  edge-guard rule: the position loop is not wrap-aware) and ends with torque
  off and the Torque_Limit restored in a ``finally``;
* a calibration is committed only by its record, written after step K proved a
  REAL power-on (Torque_Limit 48 = Max_Torque − 1 before the pull, Max_Torque
  after it) with the EEPROM block unchanged;
* nothing but „Referenz lesen" runs while the spec is unpinned.

Refusals raise :class:`StationError` with a code and the German sentence from
``station_texts_de``; the run never guesses past one.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import logging
import re
import time
from collections import namedtuple

import feetech_bus as fb
import feetech_servo_spec as fss
import station_records as records
import station_texts_de as T

log = logging.getLogger('servo_station')
log.addHandler(logging.NullHandler())     # the window adds station.log; a library prints nothing

# ── numbers (spec §5A) ───────────────────────────────────────────────────────
SUPPLY_DV = (110, 128)               # 11.0–12.8 V
MAX_TEMPERATURE_C = 45
SEVEN_V_MAX_VOLTAGE = 90             # Max-Spannung ≤ 90 → a 7.4-V servo
POSITION_TOLERANCE_TICKS = 2         # G spread, H and K Present vs T0, gauge check
STABILITY_READS = 10
STABILITY_SPAN_S = 0.5
EDGE_MARGIN_TICKS = 40               # == the driver's edge_parked_joints default margin
MOTION_TICKS = 512                   # 45°
MOTION_TORQUE_LIMIT = 300
MOTION_LIMIT_INSET_TICKS = 16
MOTION_ARRIVE_TICKS = 8
MOTION_TIMEOUT_S = 1.0
MOTION_POLL_S = 0.01
CURRENT_MA_PER_UNIT = 6.5
REPLUG_TIMEOUT_S = 120.0
REPLUG_POLL_S = 0.1
REPLUG_PING_TIMEOUT_S = 0.05
REPLUG_MISSES = 3
REPLUG_ANSWERS = 2
REPLUG_SETTLE_S = 0.5
DIRECTION_SAMPLE_S = 0.05            # 20 Hz
DIRECTION_TIMEOUT_S = 15.0
DIRECTION_TICKS = 300
RAM_DUMP = (40, 47)                  # addr 40..86
_STATUS_BLOCK = (fb.REG_PRESENT_POSITION, 15)   # 56..70: Present … Present_Current

# Step F's set (spec §4.5): every golden register but the ones a step of their
# own writes (5 ID → D, 6 baud → C2, 8 level → C, 9/11 limits → I, 18 Phase and
# 30 resolution → E), 25 (verified only) and 31 (the offset → H).
_F_SKIP = frozenset({fb.REG_ID, fb.REG_BAUD_RATE, fb.REG_RESPONSE_STATUS_LEVEL,
                     fb.REG_MIN_POSITION_LIMIT, fb.REG_MAX_POSITION_LIMIT, fb.REG_PHASE,
                     fb.REG_ANGULAR_RESOLUTION, fb.REG_25})
_PHASE_MULTI_TURN_BIT = 0x10
_LAYOUT_NAMES = {addr: name for addr, _size, name, _cls in fss.LAYOUT}
_CAP_RE = re.compile(fss.CAP_LABEL_PATTERN)

# A Wareneingang refusal that is a verdict about the SERVO is recorded as a
# failed Wareneingang (Kalibrieren then refuses it, owner OQ11). Supply, heat,
# a parked shaft or the bench itself are not.
_INCOMING_VERDICTS = frozenset({'UNKNOWN_MODEL', 'SEVEN_V', 'MAX_VOLTAGE', 'STATUS', 'PHASE',
                                'MOTION_TIME', 'MOTION_NOT_ARRIVED'})

Found = namedtuple('Found', 'baud servo_id')
Identity = namedtuple('Identity', 'block model firmware voltage_dv temperature_c status present')
Move = namedtuple('Move', 'arrived time_ms peak_raw bits final')


class StationError(Exception):
    """A refusal: ``code`` (a ``station_texts_de`` name) and the German
    ``message_de`` the operator sees."""

    def __init__(self, code, message_de, **data):
        super().__init__(code)
        self.code = code
        self.message_de = message_de
        self.data = data


def refusal(code, **fmt):
    return StationError(code, getattr(T, code).format(**fmt), **fmt)


class Env:
    """A run's I/O: ``open_bus(baud)``, ``clock()``, ``sleep(s)``, ``notify``
    (``kind`` ∈ step | prompt | note | live, a German ``text``, data) and the
    UTC clock for timestamps."""

    def __init__(self, open_bus, *, clock=time.monotonic, sleep=time.sleep, notify=None,
                 utcnow=None, port='?'):
        self.open_bus = open_bus
        self.clock = clock
        self.sleep = sleep
        self.port = port
        self._notify = notify or (lambda kind, text='', **data: None)
        self.utcnow = utcnow or (lambda: datetime.datetime.now(datetime.timezone.utc))

    def notify(self, kind, text='', **data):
        self._notify(kind, text, **data)

    def step(self, step, state='running'):
        self.notify('step', T.STEP_NAMES[step], step=step, state=state)

    def utc_iso(self):
        return self.utcnow().astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class Link:
    """The one open bus of a run, re-opened at another baud on demand."""

    def __init__(self, env):
        self._env = env
        self.bus = None
        self.baud = None

    def open(self, baud):
        self.close()
        try:
            self.bus = self._env.open_bus(baud)
        except OSError as e:
            raise refusal('PORT_OPEN_FAILED', port=self._env.port, reason=str(e)[:120]) from e
        self.baud = baud
        return self.bus

    def close(self):
        if self.bus is not None:
            try:
                self.bus.close()
            except Exception:  # noqa: BLE001 — best effort
                pass
        self.bus = None
        self.baud = None


@contextlib.contextmanager
def _bench(env):
    """A link that is always closed, with the wire's failures turned into the
    station's two sentences: a garbled or missing reply (BUS_ERROR) and a port
    that vanished (USB_LOST)."""
    link = Link(env)
    try:
        yield link
    except StationError:
        raise
    except fb.FeetechBusError as e:
        log.warning('bus error: %s', e)
        raise StationError('BUS_ERROR', T.BUS_ERROR) from e
    except OSError as e:
        log.warning('serial port lost: %s', type(e).__name__)
        raise StationError('USB_LOST', T.USB_LOST) from e
    finally:
        link.close()


def _require_pinned(spec):
    if not spec.pinned:
        raise StationError('SPEC_NOT_PINNED', T.SPEC_NOT_PINNED)


def cap_type_of(cap_label):
    match = _CAP_RE.match(cap_label or '')
    return match.group(1) if match else None


# ── register helpers ─────────────────────────────────────────────────────────
def _u8(bus, sid, addr):
    return bus.read(sid, addr, 1)[1][0]


def _u16(bus, sid, addr):
    return bus.read_u16(sid, addr)


def _reg(bus, sid, addr, size):
    return _u8(bus, sid, addr) if size == 1 else _u16(bus, sid, addr)


def _present(bus, sid):
    return fb.decode_sign_magnitude(_u16(bus, sid, fb.REG_PRESENT_POSITION), 15)


def _encode(value, size):
    return bytes([value]) if size == 1 else fb.le16(value)


def _reg_name(addr):
    return _LAYOUT_NAMES.get(addr) or T.REGISTER_NAMES[addr]


def _write_verify(bus, sid, addr, size, value, *, await_status=True, compare_first=True):
    """Read-compare-write (no write when it already holds ``value``), then read
    back; a value that does not stick is WRITE_FAILED."""
    if compare_first and _reg(bus, sid, addr, size) == value:
        return False
    bus.write(sid, addr, _encode(value, size), await_status=await_status)
    back = _reg(bus, sid, addr, size)
    if back != value:
        raise refusal('WRITE_FAILED', name=_reg_name(addr), addr=addr, found=back, want=value)
    return True


def _deviation_text(d):
    if d.expected is None:
        return T.DEVIATION_FOREIGN_OFFSET.format(found=d.found)
    return T.DEVIATION.format(name=d.name, addr=d.addr, found=d.found, want=d.expected)


def _edge_parked(present):
    return min(present, fb.TICKS_PER_REV - 1 - present) < EDGE_MARGIN_TICKS


# ── A: discovery ─────────────────────────────────────────────────────────────
def discover(link):
    """Step A: a broadcast PING per baud, 1 Mbit/s first; the FIRST baud where
    anything answers decides (review 2, minor 9): exactly one clean reply is
    the servo, ≥ 2 replies or any junk is MULTIPLE — never fall through to a
    later baud, where a lone servo would be re-baud'ed and re-ID'd on top of
    the others."""
    for baud in fb.BAUD_RATES:
        bus = link.open(baud)
        replies, junk = bus.broadcast_ping()
        if not replies and not junk:
            continue
        if len(replies) == 1 and junk == 0:
            return Found(baud, replies[0][0])
        log.info('discovery at %d: %d replies, %d junk bytes', baud, len(replies), junk)
        raise StationError('MULTIPLE', T.MULTIPLE)
    raise StationError('NO_SERVO', T.NO_SERVO)


# ── B: identification ────────────────────────────────────────────────────────
def identify(env, bus, sid, spec, joint=None):
    """Step B: the factory block and the live state, judged before any write."""
    _err, block = bus.read(sid, fb.EEPROM_BLOCK_ADDR, fb.EEPROM_BLOCK_LEN)
    block = bytes(block)
    voltage = _u8(bus, sid, fb.REG_PRESENT_VOLTAGE)
    temperature = _u8(bus, sid, fb.REG_PRESENT_TEMPERATURE)
    status = _u8(bus, sid, fb.REG_STATUS)
    present = _present(bus, sid)
    env.notify('live', T.LBL_LIVE.format(v=voltage / 10, t=temperature, p=present),
               v=voltage / 10, t=temperature, p=present)
    model = fb.from_le16(block[3], block[4])
    if model not in fb.STS_ACCEPTED_MODELS:
        raise refusal('UNKNOWN_MODEL', found=model)
    if joint is not None and model != joint.model:
        raise refusal('WRONG_MODEL', found=fb.STS_MODEL_NAMES[model], want=joint.model_name,
                      joint=joint.label_de)
    max_voltage, want = block[fb.REG_MAX_VOLTAGE], spec.factory[model][fb.REG_MAX_VOLTAGE]
    if max_voltage <= SEVEN_V_MAX_VOLTAGE:
        raise StationError('SEVEN_V', T.SEVEN_V)
    if max_voltage != want:
        raise refusal('MAX_VOLTAGE', found=max_voltage, want=want)
    if not SUPPLY_DV[0] <= voltage <= SUPPLY_DV[1]:
        raise refusal('VOLTAGE', v=voltage / 10)
    if temperature >= MAX_TEMPERATURE_C:
        raise refusal('HOT', t=temperature)
    if status:
        raise refusal('STATUS', bits=fb.describe_error_bits(status))
    reference = (joint.image[fb.REG_PHASE] if joint is not None
                 else spec.factory[model][fb.REG_PHASE]) & ~_PHASE_MULTI_TURN_BIT
    if (block[fb.REG_PHASE] & ~_PHASE_MULTI_TURN_BIT) != reference:
        raise refusal('PHASE', found=block[fb.REG_PHASE], want=reference)
    return Identity(block, model, fb.format_firmware(block[0], block[1]), voltage, temperature,
                    status, present)


# ── C … K: the calibration steps ─────────────────────────────────────────────
def open_for_writing(bus, sid):
    """Step C: status level 1 (an un-awaited repair: a level-0 servo never acks
    a WRITE), torque off, Lock 0 — each read back."""
    if _u8(bus, sid, fb.REG_RESPONSE_STATUS_LEVEL) != 1:
        bus.write(sid, fb.REG_LOCK, b'\x00', await_status=False)
        bus.write(sid, fb.REG_RESPONSE_STATUS_LEVEL, b'\x01', await_status=False)
        if _u8(bus, sid, fb.REG_RESPONSE_STATUS_LEVEL) != 1:
            raise StationError('LEVEL', T.LEVEL)
    _write_verify(bus, sid, fb.REG_TORQUE_ENABLE, 1, fb.TORQUE_ENABLE_OFF)
    _write_verify(bus, sid, fb.REG_LOCK, 1, 0)


def move_to_1mbps(env, link, sid):
    """Step C2: a servo found at another baud is moved to 1 Mbit/s (index 0)."""
    link.bus.write(sid, fb.REG_BAUD_RATE, b'\x00', await_status=False)
    env.sleep(0.1)
    bus = link.open(fb.BAUD_RATES[0])
    if not bus.ping(sid):
        raise refusal('WRITE_FAILED', name=_reg_name(fb.REG_BAUD_RATE), addr=fb.REG_BAUD_RATE,
                      found=T.NO_ANSWER, want=0)
    back = _u8(bus, sid, fb.REG_BAUD_RATE)
    if back != 0:
        raise refusal('WRITE_FAILED', name=_reg_name(fb.REG_BAUD_RATE), addr=fb.REG_BAUD_RATE,
                      found=back, want=0)
    return bus


def set_id(env, bus, old_id, new_id):
    """Step D: the joint's id (un-awaited — the reply may come from either id);
    the new id answers, the old one is silent, the register reads back."""
    if old_id == new_id:
        return
    bus.write(old_id, fb.REG_ID, bytes([new_id]), await_status=False)
    env.sleep(0.05)
    if not bus.ping(new_id) or bus.ping(old_id) or _u8(bus, new_id, fb.REG_ID) != new_id:
        raise StationError('ID_FAILED', T.ID_FAILED)


def map_registers(bus, sid):
    """Step E: Phase bit 4 cleared (read-modify-write, only when set — the
    other bits are motor-drive configuration) and Angular_Resolution 1, both
    BEFORE the offset so it is baked under the runtime's tick map."""
    phase = _u8(bus, sid, fb.REG_PHASE)
    if phase & _PHASE_MULTI_TURN_BIT:
        _write_verify(bus, sid, fb.REG_PHASE, 1, phase & ~_PHASE_MULTI_TURN_BIT, compare_first=False)
    _write_verify(bus, sid, fb.REG_ANGULAR_RESOLUTION, 1, fb.ANGULAR_RESOLUTION_SINGLE_TURN)


def write_image(bus, sid, joint):
    """Step F: the golden image's refuse class written and read back strictly;
    the warn class written (step J's block compare reports one that did not
    stick as a record warning). The position limits are NOT touched here."""
    for addr, size, _name, cls in fss.LAYOUT:
        if cls not in ('refuse', 'warn') or addr in _F_SKIP:
            continue
        want = joint.image[addr]
        if cls == 'refuse':
            _write_verify(bus, sid, addr, size, want)
        elif _reg(bus, sid, addr, size) != want:
            bus.write(sid, addr, _encode(want, size))


def stability(env, bus, sid):
    """Step G: 10 reads over 0.5 s; the horn is really seated in the key."""
    readings = []
    for k in range(STABILITY_READS):
        readings.append(_present(bus, sid))
        if k < STABILITY_READS - 1:
            env.sleep(STABILITY_SPAN_S / (STABILITY_READS - 1))
    spread = max(fb.circular_distance(readings[0], r) for r in readings)
    if spread > POSITION_TOLERANCE_TICKS:
        raise refusal('UNSTABLE', spread=spread)
    return spread


def write_offset(env, bus, sid, mark_tick):
    """Step H: the wrap-aware Homing_Offset that makes the gauge pose read the
    mark tick (T0). A foreign encoding is cleared first; raw exactly opposite
    the mark (offset −2048) is unrepresentable → OFFSET_EDGE, nothing written."""
    raw16 = _u16(bus, sid, fb.REG_HOMING_OFFSET)
    try:
        current = fb.decode_homing_offset(raw16)
    except fb.ForeignOffsetEncoding:
        _write_verify(bus, sid, fb.REG_HOMING_OFFSET, 2, 0)
        current = 0
    raw = fb.raw_tick_from(_present(bus, sid), current)
    try:
        offset = fb.homing_offset_for(raw, mark_tick)
    except fb.OffsetUnrepresentable:
        raise StationError('OFFSET_EDGE', T.OFFSET_EDGE) from None
    _write_verify(bus, sid, fb.REG_HOMING_OFFSET, 2, fb.encode_homing_offset(offset))
    present_after = _present(bus, sid)
    if fb.circular_distance(present_after, mark_tick) > POSITION_TOLERANCE_TICKS:
        raise refusal('OFFSET_CHECK', present=present_after, mark=mark_tick)
    return raw, offset, present_after


def write_limits(bus, sid, joint):
    """Step I: the spec's centred window — the only step that writes 9–12."""
    lo, hi = joint.window
    _write_verify(bus, sid, fb.REG_MIN_POSITION_LIMIT, 2, lo)
    _write_verify(bus, sid, fb.REG_MAX_POSITION_LIMIT, 2, hi)


def lock_and_compare(bus, sid, joint, offset):
    """Step J: Lock 1, then the whole block against the golden image (the boot
    probe's own comparison) and the offset bytes exactly. Returns the block and
    the warn-class deviations as record warnings."""
    _write_verify(bus, sid, fb.REG_LOCK, 1, 1)
    _err, block = bus.read(sid, fb.EEPROM_BLOCK_ADDR, fb.EEPROM_BLOCK_LEN)
    block = bytes(block)
    refused, warned = fss.compare_block(block, joint)
    problems = [_deviation_text(d) for d in refused]
    encoded = fb.encode_homing_offset(offset)
    found = fb.from_le16(block[31], block[32])
    if found != encoded:
        problems.append(T.DEVIATION.format(name=_reg_name(fb.REG_HOMING_OFFSET), addr=fb.REG_HOMING_OFFSET,
                                           found=found, want=encoded))
    if problems:
        raise refusal('BLOCK_MISMATCH', list='; '.join(problems))
    warnings = [T.WARN_REGISTER_STAYS.format(name=d.name, addr=d.addr, found=d.found, want=d.expected)
                for d in warned]
    return block, warnings


def wait_for_replug(env, ping, other=None):
    """Poll every 0.1 s: 3 misses in a row = the plug is out (REPLUG_PLUG),
    then 2 answers in a row = back. ``other`` pings the id the servo had
    before the run: answering there means its new id did not persist.
    Returns ``'servo'`` or ``'other'``; 120 s → REPLUG_TIMEOUT."""
    start = env.clock()
    misses = back = back_other = 0
    pulled = False
    while env.clock() - start < REPLUG_TIMEOUT_S:
        ok = ping()
        if not pulled:
            misses = 0 if ok else misses + 1
            if misses >= REPLUG_MISSES:
                pulled = True
                env.notify('prompt', T.REPLUG_PLUG)
        else:
            back = back + 1 if ok else 0
            if back >= REPLUG_ANSWERS:
                return 'servo'
            if other is not None and not ok:
                back_other = back_other + 1 if other() else 0
                if back_other >= REPLUG_ANSWERS:
                    return 'other'
        env.sleep(REPLUG_POLL_S)
    raise StationError('REPLUG_TIMEOUT', T.REPLUG_TIMEOUT)


def power_cycle_proof(env, bus, sid, old_id, joint, block_j):
    """Step K (S18): Torque_Limit (RAM 48) = Max_Torque − 1 before the pull;
    the firmware loads 48 from Max_Torque (EEPROM 16) at power-on, so reading
    Max_Torque afterwards proves a REAL power-on and that 16 persisted.
    Decided in this order: a changed block → NOT_PERSISTED; 48 still
    Max_Torque − 1 → NO_REAL_RESTART; any other 48 → RESTART_UNCLEAR (the
    vendor-table assumption failed, not the servo)."""
    max_torque = joint.image[fb.REG_MAX_TORQUE_LIMIT]
    _write_verify(bus, sid, fb.REG_TORQUE_LIMIT, 2, max_torque - 1)
    env.notify('prompt', T.REPLUG_PULL)
    other = None
    if old_id != sid:
        def other():
            return bus.ping(old_id, timeout_s=REPLUG_PING_TIMEOUT_S)
    came_back = wait_for_replug(env, lambda: bus.ping(sid, timeout_s=REPLUG_PING_TIMEOUT_S), other)
    if came_back == 'other':
        raise StationError('NOT_PERSISTED', T.NOT_PERSISTED)
    env.sleep(REPLUG_SETTLE_S)
    torque_limit = _u16(bus, sid, fb.REG_TORQUE_LIMIT)
    _err, block = bus.read(sid, fb.EEPROM_BLOCK_ADDR, fb.EEPROM_BLOCK_LEN)
    block = bytes(block)
    present = _present(bus, sid)
    if block != block_j:
        raise StationError('NOT_PERSISTED', T.NOT_PERSISTED)
    if torque_limit == max_torque - 1:
        raise StationError('NO_REAL_RESTART', T.NO_REAL_RESTART)
    if torque_limit != max_torque:
        raise refusal('RESTART_UNCLEAR', found=torque_limit, want=max_torque)
    if fb.circular_distance(present, joint.t0) > POSITION_TOLERANCE_TICKS:
        raise refusal('OFFSET_CHECK', present=present, mark=joint.t0)
    return block, present


def kalibrieren(env, store, *, serial, joint_index, cap_label, operator, gauge_check_at=None,
                warnings=(), spec=fss.V1, host='unknown', tool_commit='unknown'):
    """The Kalibrieren run, steps A–L. Returns ``(record, path)``; the record
    file is the commit point and is written only after K passed."""
    _require_pinned(spec)
    joint = spec.joints[joint_index]
    if cap_type_of(cap_label) != joint.cap:
        raise refusal('CAP_MISMATCH', joint=joint.label_de, want=joint.cap)
    record_warnings = list(warnings)
    incoming = store.load_incoming(serial)
    if incoming is None:
        env.notify('note', T.NOTE_NO_INCOMING.format(serial=serial))
        record_warnings.append(T.WARN_NO_INCOMING)
    elif not incoming.get('passed'):
        raise refusal('INCOMING_FAILED', serial=serial,
                      reason=incoming.get('reason') or incoming.get('reason_code') or '?')
    with _bench(env) as link:
        env.step('A')
        found = discover(link)
        bus, sid = link.bus, found.servo_id
        env.step('B')
        ident = identify(env, bus, sid, spec, joint)
        env.step('C')
        open_for_writing(bus, sid)
        if found.baud != fb.BAUD_RATES[0]:
            env.step('C2')
            bus = move_to_1mbps(env, link, sid)
        env.step('D')
        set_id(env, bus, sid, joint.servo_id)
        old_id, sid = sid, joint.servo_id
        env.step('E')
        map_registers(bus, sid)
        env.step('F')
        write_image(bus, sid, joint)
        env.step('G')
        spread = stability(env, bus, sid)
        env.step('H')
        raw, offset, present_after = write_offset(env, bus, sid, joint.t0)
        env.step('I')
        write_limits(bus, sid, joint)
        env.step('J')
        block_j, image_warnings = lock_and_compare(bus, sid, joint, offset)
        env.step('K')
        block_k, present_k = power_cycle_proof(env, bus, sid, old_id, joint, block_j)
    env.step('L')
    record = records.finalize_record({
        'servo_serial': serial, 'profile_id': fss.PROFILE_ID, 'joint': joint.name,
        'servo_id': joint.servo_id, 'model_number': joint.model, 'model_name': joint.model_name,
        'firmware': ident.firmware, 'spec_sha256': spec.digest, 'tick_direction': joint.tick_direction,
        'sign': joint.sign, 't0': joint.t0,
        'factory_readings': {'baud_index': ident.block[fb.REG_BAUD_RATE], 'id': ident.block[fb.REG_ID],
                             'eeprom_hex': ident.block.hex(), 'voltage_dv': ident.voltage_dv,
                             'temperature_c': ident.temperature_c, 'status': ident.status},
        'incoming_test': incoming,
        'calibration': {'cap': joint.cap, 'gauge_id': cap_label, 'gauge_check_at': gauge_check_at,
                        'raw_at_gauge': raw, 'homing_offset': offset, 'mark_tick': joint.t0,
                        'present_after_write': present_after, 'present_after_power_cycle': present_k,
                        'spread_ticks': spread},
        'eeprom_image_hex': block_k.hex(), 'eeprom_sha256': hashlib.sha256(block_k).hexdigest(),
        'warnings': record_warnings + image_warnings, 'station_host': host, 'tool_commit': tool_commit,
        'operator': operator, 'calibrated_at': env.utc_iso(),
    })
    path = store.save_record(record)
    log.info('calibrated %s as %s (offset %d)', serial, joint.name, offset)
    env.notify('note', T.RECORD_SAVED.format(path=path))
    return record, path


# ── the motion test (Wareneingang, „Fahrzeit messen") ───────────────────────
def _ram_write(bus, sid, addr, size, value, await_status):
    bus.write(sid, addr, _encode(value, size), await_status=await_status)
    back = _reg(bus, sid, addr, size)
    if back != value:
        raise refusal('WRITE_FAILED', name=_reg_name(addr), addr=addr, found=back, want=value)


def _move(env, bus, sid, target, await_status):
    _ram_write(bus, sid, fb.REG_GOAL_POSITION, 2, target, await_status)
    start = env.clock()
    peak = bits = 0
    addr, length = _STATUS_BLOCK
    while True:
        _err, data = bus.read(sid, addr, length)
        present = fb.decode_sign_magnitude(fb.from_le16(data[0], data[1]), 15)
        bits |= data[fb.REG_STATUS - addr]
        current = fb.decode_sign_magnitude(
            fb.from_le16(data[fb.REG_PRESENT_CURRENT - addr], data[fb.REG_PRESENT_CURRENT - addr + 1]), 15)
        peak = max(peak, abs(current))
        elapsed = env.clock() - start
        if abs(present - target) <= MOTION_ARRIVE_TICKS:
            return Move(True, int(round(elapsed * 1000)), peak, bits, present)
        if elapsed >= MOTION_TIMEOUT_S:
            return Move(False, None, peak, bits, present)
        env.sleep(MOTION_POLL_S)


def _limp(bus, sid, torque_limit, await_status):
    """The motion test's ``finally``: torque off, Torque_Limit restored — each
    tried twice and never raising over the error that got us here."""
    for addr, data in ((fb.REG_TORQUE_ENABLE, bytes([fb.TORQUE_ENABLE_OFF])),
                       (fb.REG_TORQUE_LIMIT, fb.le16(torque_limit))):
        for _attempt in range(2):
            try:
                bus.write(sid, addr, data, await_status=await_status)
                break
            except (fb.FeetechBusError, OSError) as e:
                log.warning('limp write to %d failed: %s', addr, type(e).__name__)


def motion_test(env, bus, sid, block):
    """Energise at Torque_Limit 300, Goal = Present (the STS switches torque on
    with a goal write — intended here), then ±512 ticks and back, each move
    polled every 10 ms until ±8 ticks or 1.0 s. RAM writes only, each read back.
    A shaft parked within 40 ticks of 0/4095 is refused before any write: a
    goal there can make the non-wrap-aware loop run the long way at full speed.
    ``finally``: torque off and the Torque_Limit read before restored."""
    await_status = block[fb.REG_RESPONSE_STATUS_LEVEL] == 1
    present = _present(bus, sid)
    if _edge_parked(present):
        raise refusal('EDGE_PARKED', p=present)
    lo = fb.from_le16(block[fb.REG_MIN_POSITION_LIMIT], block[fb.REG_MIN_POSITION_LIMIT + 1])
    hi = fb.from_le16(block[fb.REG_MAX_POSITION_LIMIT], block[fb.REG_MAX_POSITION_LIMIT + 1])
    target = present + MOTION_TICKS if present < fb.CENTER_TICK else present - MOTION_TICKS
    target = max(lo + MOTION_LIMIT_INSET_TICKS, min(hi - MOTION_LIMIT_INSET_TICKS, target))
    saved_limit = _u16(bus, sid, fb.REG_TORQUE_LIMIT)
    try:
        _ram_write(bus, sid, fb.REG_TORQUE_ENABLE, 1, fb.TORQUE_ENABLE_OFF, await_status)
        _ram_write(bus, sid, fb.REG_TORQUE_LIMIT, 2, MOTION_TORQUE_LIMIT, await_status)
        _ram_write(bus, sid, fb.REG_ACCELERATION, 1, 0, await_status)
        _ram_write(bus, sid, fb.REG_GOAL_SPEED, 2, 0, await_status)
        _ram_write(bus, sid, fb.REG_GOAL_POSITION, 2, present, await_status)
        _ram_write(bus, sid, fb.REG_TORQUE_ENABLE, 1, fb.TORQUE_ENABLE_ON, await_status)
        out = _move(env, bus, sid, target, await_status)
        back = _move(env, bus, sid, present, await_status) if out.arrived else None
    finally:
        _limp(bus, sid, saved_limit, await_status)
    return {'start': present, 'target': target, 'moved_ticks': abs(target - present), 'out': out, 'back': back}


def _motion_failure(motion, window=None):
    """The first reason a motion test fails, as a StationError, else None."""
    for move, goal in ((motion['out'], motion['target']), (motion['back'], motion['start'])):
        if move is None:
            continue
        if not move.arrived:
            text = T.MOTION_NOT_ARRIVED.format(present=move.final, target=goal)
            return StationError('MOTION_NOT_ARRIVED', T.MOTION_FAIL.format(reason=text.rstrip('.')))
        if move.bits:
            text = T.STATUS.format(bits=fb.describe_error_bits(move.bits))
            return StationError('STATUS', T.MOTION_FAIL.format(reason=text.rstrip('.')))
    if window is not None:
        lo, hi = window
        for move in (motion['out'], motion['back']):
            if not lo <= move.time_ms <= hi:
                return refusal('MOTION_TIME', ms=move.time_ms, lo=lo, hi=hi)
    return None


def wareneingang(env, store, *, serial, spec=fss.V1):
    """The optional incoming check (owner OQ9): identity (step B's checks) and
    the motion test against ``MOTION_TEST_WINDOW_MS``; no EEPROM write. Saves
    ``incoming/<serial>.json`` — passed, or failed with the reason when the
    verdict is about the servo."""
    _require_pinned(spec)
    rec = {'schema': records.INCOMING_SCHEMA, 'servo_serial': serial, 'tested_at': env.utc_iso(),
           'model_number': None, 'model_name': None, 'firmware': None, 'moved_ticks': None,
           'time_ms': None, 'peak_current_ma': None, 'status_bits': None}
    try:
        with _bench(env) as link:
            env.step('A')
            found = discover(link)
            env.step('B')
            ident = identify(env, link.bus, found.servo_id, spec)
            rec.update(model_number=ident.model, model_name=fb.STS_MODEL_NAMES[ident.model],
                       firmware=ident.firmware)
            motion = motion_test(env, link.bus, found.servo_id, ident.block)
        moves = [m for m in (motion['out'], motion['back']) if m is not None]
        bits = 0
        for move in moves:
            bits |= move.bits
        rec.update(moved_ticks=motion['moved_ticks'],
                   time_ms=[m.time_ms for m in moves],
                   peak_current_ma=int(round(max(m.peak_raw for m in moves) * CURRENT_MA_PER_UNIT)),
                   status_bits=bits)
        failure = _motion_failure(motion, spec.motion_test_window_ms[ident.model])
        if failure is not None:
            raise failure
    except StationError as e:
        if e.code in _INCOMING_VERDICTS:
            store.save_incoming(dict(rec, passed=False, reason_code=e.code, reason=e.message_de))
        raise
    rec.update(passed=True, reason_code=None, reason=None)
    store.save_incoming(rec)
    env.notify('note', T.INCOMING_PASSED.format(serial=serial, model=rec['model_name'], fw=rec['firmware']))
    return rec


# ── Referenz lesen (P0) ──────────────────────────────────────────────────────
def _dump_range(bus, sid, addr, length, regs, failed):
    try:
        _err, data = bus.read(sid, addr, length)
        regs.update({addr + k: b for k, b in enumerate(data)})
        return True
    except fb.FeetechBusError:
        for a in range(addr, addr + length):
            try:
                regs[a] = _u8(bus, sid, a)
            except fb.FeetechBusError:
                failed.append(a)
        return False


def referenz_lesen(env, store):
    """Read-only: the EEPROM (0–39) and RAM (40–86) of ONE vendor-fresh servo,
    as two block reads (one-byte reads after a block failure, recorded). A
    servo that is not fresh (ID 1, limits 0/4095) is refused and nothing is
    saved: a dump only counts from a servo nobody has written (review 2, M2)."""
    regs, failed = {}, []
    with _bench(env) as link:
        env.step('A')
        found = discover(link)
        eeprom_ok = _dump_range(link.bus, found.servo_id, fb.EEPROM_BLOCK_ADDR, fb.EEPROM_BLOCK_LEN,
                                regs, failed)
        ram_ok = _dump_range(link.bus, found.servo_id, RAM_DUMP[0], RAM_DUMP[1], regs, failed)
    if not records.is_fresh(regs):
        raise StationError('P0_NOT_FRESH', T.P0_NOT_FRESH.format(path='', **records.fresh_fields(regs)).strip())
    model = (fb.from_le16(regs[3], regs[4]) if 3 in regs and 4 in regs else None)
    dump = {'schema': records.REFERENCE_SCHEMA, 'read_at': env.utc_iso(), 'model': model,
            'firmware': fb.format_firmware(regs.get(0, 0), regs.get(1, 0)), 'baud_index': regs.get(6),
            'id': regs.get(5), 'eeprom_block_read': eeprom_ok, 'ram_block_read': ram_ok, 'regs': regs,
            'failed_addrs': failed, 'tick_direction': None, 'motion_ms': None}
    path = store.save_reference(dump, env.utc_iso())
    env.notify('note', T.REF_SAVED.format(path=path))
    return path, dump


def _reference_servo(store, link, path):
    """The servo of the current reference: same model as its dump."""
    dump = store.load_reference(path)
    found = discover(link)
    model = _u16(link.bus, found.servo_id, fb.REG_MODEL_NUMBER)
    if model != dump.get('model'):
        raise StationError('NEED_REFERENCE', T.NEED_REFERENCE)
    return found.servo_id, model


def drehrichtung_messen(env, store, path):
    """In the cradle, horn up, no cap: the operator turns the horn about a
    quarter turn along the cradle's arrow (counter-clockwise seen from the
    horn); Present is sampled at 20 Hz for ≤ 15 s and unwrapped. +300 → +1,
    −300 → −1. Read-only."""
    with _bench(env) as link:
        env.step('A')
        sid, model = _reference_servo(store, link, path)
        env.notify('prompt', T.TURN_CCW)
        start = env.clock()
        previous = _present(link.bus, sid)
        total = 0
        direction = None
        while env.clock() - start < DIRECTION_TIMEOUT_S:
            env.sleep(DIRECTION_SAMPLE_S)
            now = _present(link.bus, sid)
            step = (now - previous + fb.CENTER_TICK) % fb.TICKS_PER_REV - fb.CENTER_TICK
            total += step
            previous = now
            if total >= DIRECTION_TICKS:
                direction = fss.TICK_DIRECTION_CCW
                break
            if total <= -DIRECTION_TICKS:
                direction = fss.TICK_DIRECTION_CW
                break
    if direction is None:
        raise StationError('DIRECTION_NONE', T.DIRECTION_NONE)
    store.update_reference(path, tick_direction=direction)
    env.notify('note', T.DIRECTION_RESULT.format(
        model=fb.STS_MODEL_NAMES.get(model, model),
        dir=T.DIR_CCW if direction == fss.TICK_DIRECTION_CCW else T.DIR_CW))
    return direction


def fahrzeit_messen(env, store, path):
    """The Wareneingang motion without a pass window (servo out of the cradle),
    RAM only — the servo stays fresh. Saves both move times on the reference."""
    with _bench(env) as link:
        env.step('A')
        sid, model = _reference_servo(store, link, path)
        _err, block = link.bus.read(sid, fb.EEPROM_BLOCK_ADDR, fb.EEPROM_BLOCK_LEN)
        motion = motion_test(env, link.bus, sid, bytes(block))
    failure = _motion_failure(motion)
    if failure is not None:
        raise failure
    times = [motion['out'].time_ms, motion['back'].time_ms]
    store.update_reference(path, motion_ms=times)
    env.notify('note', T.MOTION_MEASURED.format(model=fb.STS_MODEL_NAMES.get(model, model),
                                                t1=times[0], t2=times[1]))
    return times


def p0_bericht(store, generated_at=None):
    """The P0 report over every reference dump; refuses to propose anything on
    any finding (spec §5A). Writes ``p0_report.json`` + ``p0_pin_snippet.txt``
    and never touches the repository."""
    generated_at = generated_at or datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    findings, report, snippet = records.p0_report(store.list_references(), fb.STS_MODEL_NAMES, generated_at)
    if findings:
        messages = [getattr(T, code).format(**fmt).strip() for code, fmt in findings]
        raise StationError(findings[0][0], ' '.join(messages), findings=[code for code, _fmt in findings])
    return store.write_p0_report(report, snippet)


# ── Prüfen and Lehre prüfen (read-only) ──────────────────────────────────────
def pruefen(env, store, *, serial, spec=fss.V1):
    """Read-only, in the gauge: the block against the spec image, Present
    against T0, and — when this PC has the record — offset and block against
    it. Every finding is listed."""
    _require_pinned(spec)
    record = store.load_record(serial)
    with _bench(env) as link:
        env.step('A')
        found = discover(link)
        _err, block = link.bus.read(found.servo_id, fb.EEPROM_BLOCK_ADDR, fb.EEPROM_BLOCK_LEN)
        block = bytes(block)
        present = _present(link.bus, found.servo_id)
    if found.servo_id not in fss.SERVO_IDS:
        raise refusal('CHECK_FAILED', list=T.CHECK_BAD_ID.format(id=found.servo_id))
    joint = spec.joints[fss.SERVO_IDS.index(found.servo_id)]
    findings = []
    model = fb.from_le16(block[3], block[4])
    if model != joint.model:
        findings.append(T.DEVIATION.format(name=T.REGISTER_NAMES[fb.REG_MODEL_NUMBER], addr=fb.REG_MODEL_NUMBER,
                                           found=model, want=joint.model))
    refused, _warned = fss.compare_block(block, joint)
    findings += [_deviation_text(d) for d in refused]
    if fb.circular_distance(present, joint.t0) > POSITION_TOLERANCE_TICKS:
        findings.append(T.CHECK_POSITION.format(present=present, mark=joint.t0).rstrip('.'))
    if record is None:
        env.notify('note', T.CHECK_NO_RECORD.format(serial=serial))
    else:
        raw16 = fb.from_le16(block[31], block[32])
        try:
            offset = fb.decode_homing_offset(raw16)
        except fb.ForeignOffsetEncoding:
            offset = None
        want = (record.get('calibration') or {}).get('homing_offset')
        if offset != want:
            findings.append(T.CHECK_RECORD_OFFSET.format(found=raw16 if offset is None else offset, want=want))
        if block.hex() != record.get('eeprom_image_hex'):
            findings.append(T.CHECK_RECORD_BLOCK)
    if findings:
        raise refusal('CHECK_FAILED', list='; '.join(findings))
    message = T.CHECK_PASSED.format(serial=serial, joint=joint.label_de)
    env.notify('note', message)
    return message


def lehre_pruefen(env, store, *, serial, cap_label, local_date, spec=fss.V1):
    """Read-only. No master for the cap's type yet: a serial with a local
    record calibrated with that cap type becomes the master. Else the master,
    in the gauge, must read its recorded tick ± 2 — logged in
    ``gauge_checks.csv``; a pass allows Kalibrieren with this cap today."""
    _require_pinned(spec)
    cap_type = cap_type_of(cap_label)
    if cap_type is None:
        raise refusal('BAD_CAP', text=cap_label)
    record = store.load_record(serial)
    has_cap_record = bool(record) and (record.get('calibration') or {}).get('cap') == cap_type
    master = store.masters().get(cap_type)
    if master is None:
        if not has_cap_record:
            raise refusal('MASTER_NEEDS_RECORD', serial=serial, cap=cap_type)
        store.set_master(cap_type, serial)
        message = T.MASTER_SET.format(serial=serial, cap=cap_type)
        env.notify('note', message)
        return message
    if serial != master:
        raise refusal('MASTER_MISMATCH', serial=serial, cap=cap_type, master=master)
    if not has_cap_record:
        raise refusal('MASTER_NEEDS_RECORD', serial=serial, cap=cap_type)
    want = record['calibration']['present_after_power_cycle']
    with _bench(env) as link:
        env.step('A')
        found = discover(link)
        _err, block = link.bus.read(found.servo_id, fb.EEPROM_BLOCK_ADDR, fb.EEPROM_BLOCK_LEN)
        present = _present(link.bus, found.servo_id)
    if bytes(block).hex() != record.get('eeprom_image_hex'):
        raise refusal('MASTER_WRONG_SERVO', serial=serial)
    passed = fb.circular_distance(present, want) <= POSITION_TOLERANCE_TICKS
    store.append_gauge_check(at=env.utc_iso(), local_date=local_date, cap_label=cap_label, cap_type=cap_type,
                             master_serial=serial, present=present, want=want, passed=passed)
    if not passed:
        raise refusal('GAUGE_FAILED', cap=cap_label, present=present, want=want)
    message = T.GAUGE_PASSED.format(cap=cap_label, present=present, want=want)
    env.notify('note', message)
    return message

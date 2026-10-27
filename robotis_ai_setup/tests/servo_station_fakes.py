"""Register-level fakes for the Servo-Station tests (a helper, not a test module).

``FakeServo`` models ONE STS servo the way the station meets it on the bench:

* memory 0..86; the EEPROM half (0..39) exists twice — ``mem`` (live) and
  ``committed`` — and a write commits only while Lock (55) reads 0;
  ``honours_unlock=False`` models a firmware whose Lock 0 reads back but never
  commits (the loss only a power cycle shows);
* ``power_cycle()`` restores the committed EEPROM, resets RAM (torque off, Lock
  = ``lock_default``) and copies the committed Max_Torque (16) into
  Torque_Limit (48) unless ``copies_torque_limit=False`` (then 48 powers up as
  ``torque_limit_on_power_up``);
* ``pull_power(off_polls)`` / ``brownout(polls)``: silent for that many pings,
  then back — after a real power-on, or with RAM kept;
* Present = (raw − Homing_Offset) mod 4096 with the offset decoded per the
  vendor's CHINESE table (0x1805 = −2053), so a foreign encoding is modelled;
  ``adds_offset=True`` models a firmware applying the offset with the opposite
  sign (raw + offset), ``folds_present=False`` one that does not fold Present
  into 0–4095 (raw − offset as is, sign-magnitude on the wire);
  ``keeps_old_id`` makes a ping of its previous id still answer after an id
  change;
* it answers only at its own baud; level 0 sends no WRITE reply; a Goal write
  while torque is off switches torque on; a goal moves Present toward it at
  ``ticks_per_s`` per fake second (linear in tick space — the real position
  loop is not wrap-aware);
* every write it receives is recorded in ``writes`` (addr, bytes).

``FakeServoBus`` is the station's bus over one or several fake servos at one
baud; its ``write`` runs ``fb.assert_safe_torque_enable`` FIRST, like the real
bus, so a test can never hide a Torque_Enable value ∉ {0, 1}.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, '..', '..'))
DOCKER_DIR = os.path.join(_REPO, 'robotis_ai_setup', 'docker', 'open_manipulator')
STATION_DIR = os.path.join(_REPO, 'tools', 'servo_station')
PROVISION_PATH = os.path.join(_REPO, 'tools', 'feetech_provision.py')
for _p in (DOCKER_DIR, STATION_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import feetech_bus as fb  # noqa: E402
import feetech_servo_spec as fss  # noqa: E402

SPEC_A = fss.build_spec({777: 1, 2825: 1}, fss.FACTORY, fss.MOTION_TEST_WINDOW_MS,
                        fss.MAX_TORQUE, frozenset())
SPEC_B = fss.build_spec({777: -1, 2825: -1}, fss.FACTORY, fss.MOTION_TEST_WINDOW_MS,
                        fss.MAX_TORQUE, frozenset())

_SIZES = {addr: size for addr, size, _n, _c in fss.LAYOUT}


class FakeClock:
    """``clock()`` and ``sleep(s)`` over one fake time line."""

    def __init__(self, start=1000.0):
        self.t = float(start)

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(0.0, float(seconds))


def vendor_offset(word):
    """Homing_Offset word → offset, per the vendor's CHINESE table (bit 11 sign,
    bit 12 = +2048) — what the real firmware does, not what our tools write."""
    word &= 0x1FFF
    value = (word & 0x7FF) + (2048 if word & 0x1000 else 0)
    return -value if word & 0x800 else value


class FakeServo:
    MOVING_CURRENT_RAW = 125            # × 6.5 mA = 812.5 mA

    def __init__(self, model=fb.STS3215_MODEL_NUMBER, *, servo_id=1, raw=2048,
                 baud_index=0, firmware=(3, 10), lock_default=0,
                 copies_torque_limit=True, torque_limit_on_power_up=1000,
                 honours_unlock=True, clock=None, ticks_per_s=3000.0,
                 voltage_dv=120, temperature_c=26, status=0, max_torque=1000,
                 adds_offset=False, folds_present=True, keeps_old_id=False):
        self.model = model
        self.adds_offset = adds_offset
        self.folds_present = folds_present
        self.keeps_old_id = keeps_old_id
        self.previous_ids = set()
        self.clock = clock if clock is not None else FakeClock()
        self.ticks_per_s = float(ticks_per_s)
        self.lock_default = lock_default
        self.copies_torque_limit = copies_torque_limit
        self.torque_limit_on_power_up = torque_limit_on_power_up
        self.honours_unlock = honours_unlock
        self.writes: list[tuple[int, bytes]] = []
        self.raw = float(raw)
        self.hand_ticks_per_s = 0.0          # the operator turning the horn by hand
        self.goal = None
        self.unplugged = False
        self.power_cycles = 0
        self._silent_pings = 0
        self._power_on_after_silence = False
        self._last_t = self.clock()
        self.fail_present_reads_while_moving = 0
        self.status_while_moving = 0
        self.ram_voltage_dv = voltage_dv
        self.ram_temperature_c = temperature_c
        self.ram_status = status

        m = bytearray(87)
        m[0], m[1] = firmware
        m[3:5] = fb.le16(model)
        m[5] = servo_id
        m[6] = baud_index
        m[7] = 0
        m[8] = 1
        m[9:11] = fb.le16(0)
        m[11:13] = fb.le16(4095)
        for addr, value in fss.FACTORY.get(model, fss.FACTORY[fb.STS3215_MODEL_NUMBER]).items():
            m[addr:addr + _SIZES[addr]] = (bytes([value]) if _SIZES[addr] == 1 else fb.le16(value))
        m[16:18] = fb.le16(max_torque)
        m[19] = 44
        m[25] = 0
        m[30] = 1
        m[33] = 0
        m[36] = 80
        m[37] = 16
        m[39] = 0
        self.mem = m
        self.committed = bytearray(m[0:40])
        self._reset_ram()

    # ── helpers ──────────────────────────────────────────────────────────────
    @property
    def servo_id(self):
        return self.mem[fb.REG_ID]

    @property
    def baud(self):
        return fb.BAUD_RATES[self.mem[fb.REG_BAUD_RATE]]

    def offset(self):
        return vendor_offset(fb.from_le16(self.mem[31], self.mem[32]))

    def present(self):
        self._advance()
        value = int(round(self.raw + (self.offset() if self.adds_offset else -self.offset())))
        return value % fb.TICKS_PER_REV if self.folds_present else value

    def u16(self, addr):
        return fb.from_le16(self.mem[addr], self.mem[addr + 1])

    def block(self):
        return bytes(self.mem[0:40])

    def eeprom_writes(self):
        return [(a, d) for a, d in self.writes if a < fb.EEPROM_BLOCK_LEN]

    def written_addrs(self):
        return {a + k for a, d in self.writes for k in range(len(d))}

    def _moving(self):
        return self.mem[fb.REG_TORQUE_ENABLE] == 1 and self.goal is not None

    def _advance(self):
        now = self.clock()
        dt = now - self._last_t
        self._last_t = now
        if dt <= 0:
            return
        self.raw += self.hand_ticks_per_s * dt
        if not self._moving():
            return
        present = (self.raw - self.offset()) % fb.TICKS_PER_REV
        delta = self.goal - present
        step = min(abs(delta), self.ticks_per_s * dt)
        self.raw += step if delta > 0 else -step

    def _reset_ram(self):
        self.mem[fb.REG_TORQUE_ENABLE] = 0
        self.mem[41] = 0
        self.mem[fb.REG_LOCK] = self.lock_default
        torque_limit = (fb.from_le16(self.committed[16], self.committed[17])
                        if self.copies_torque_limit else self.torque_limit_on_power_up)
        self.mem[48:50] = fb.le16(torque_limit)
        self.goal = None

    # ── power ────────────────────────────────────────────────────────────────
    def power_cycle(self):
        self.mem[0:40] = self.committed
        self._reset_ram()
        self.power_cycles += 1

    def pull_power(self, off_polls=5):
        """Silent for ``off_polls`` pings, then a real power-on."""
        self._silent_pings = off_polls
        self._power_on_after_silence = True

    def brownout(self, polls=5):
        """Silent for ``polls`` pings, RAM kept (no power-on)."""
        self._silent_pings = polls
        self._power_on_after_silence = False

    def _answers(self):
        return not self.unplugged and self._silent_pings == 0

    def _on_ping(self):
        if self._silent_pings > 0:
            self._silent_pings -= 1
            if self._silent_pings == 0 and self._power_on_after_silence:
                self.power_cycle()
            return False
        return not self.unplugged

    # ── register access ──────────────────────────────────────────────────────
    def read(self, addr, length):
        self._advance()
        assert 0 <= addr and addr + length <= 87, (addr, length)
        if (self._moving() and addr <= fb.REG_PRESENT_POSITION < addr + length
                and self.fail_present_reads_while_moving):
            self.fail_present_reads_while_moving -= 1
            raise fb.FeetechBusError('fake: garbled reply while moving')
        out = bytearray(self.mem[addr:addr + length])
        dynamic = {
            fb.REG_PRESENT_POSITION: fb.le16(fb.encode_sign_magnitude(self.present(), 15)),
            fb.REG_PRESENT_VOLTAGE: bytes([self.ram_voltage_dv]),
            fb.REG_PRESENT_TEMPERATURE: bytes([self.ram_temperature_c]),
            fb.REG_STATUS: bytes([self.ram_status | (self.status_while_moving if self._moving() else 0)]),
            fb.REG_PRESENT_CURRENT: fb.le16(self.MOVING_CURRENT_RAW if self._is_moving_now() else 0),
        }
        for reg, data in dynamic.items():
            for k, byte in enumerate(data):
                if addr <= reg + k < addr + length:
                    out[reg + k - addr] = byte
        return bytes(out)

    def _is_moving_now(self):
        if not self._moving():
            return False
        present = int(round(self.raw - self.offset())) % fb.TICKS_PER_REV
        return abs(self.goal - present) > 0

    def write(self, addr, data):
        self._advance()
        self.writes.append((addr, bytes(data)))
        if self.keeps_old_id and addr <= fb.REG_ID < addr + len(data):
            self.previous_ids.add(self.servo_id)
        for k, byte in enumerate(data):
            a = addr + k
            if a < fb.EEPROM_BLOCK_LEN:
                self.mem[a] = byte
                if self.mem[fb.REG_LOCK] == 0 and self.honours_unlock:
                    self.committed[a] = byte
            else:
                self.mem[a] = byte
        end = addr + len(data)
        if addr <= fb.REG_GOAL_POSITION < end or addr <= fb.REG_GOAL_POSITION + 1 < end:
            self.goal = fb.decode_sign_magnitude(self.u16(fb.REG_GOAL_POSITION), 15)
            if self.mem[fb.REG_TORQUE_ENABLE] == 0:
                self.mem[fb.REG_TORQUE_ENABLE] = 1   # the STS turns torque on with a goal write
        if addr <= fb.REG_TORQUE_ENABLE < end and self.mem[fb.REG_TORQUE_ENABLE] == 0:
            self.goal = None


class FakeServoBus:
    """The station's bus over fake servos at ONE baud (a real port at one rate)."""

    def __init__(self, servos, baud=1_000_000, noise=0):
        self.servos = list(servos) if isinstance(servos, (list, tuple)) else [servos]
        self._baud = baud
        self.noise = noise
        self.writes: list[tuple[int, int, bytes]] = []
        self.closed = False

    @property
    def baudrate(self):
        return self._baud

    def close(self):
        self.closed = True

    def _at(self, sid):
        return [s for s in self.servos if s.baud == self._baud and s.servo_id == sid]

    def _one(self, sid):
        found = [s for s in self._at(sid) if s._answers()]
        if not found:
            raise fb.FeetechBusTimeout(f'fake: id {sid} silent')
        if len(found) > 1:
            raise fb.FeetechBusError(f'fake: {len(found)} servos answer as id {sid}')
        return found[0]

    def ping(self, servo_id, timeout_s=None):
        servos = self._at(servo_id) + [s for s in self.servos if s.baud == self._baud
                                       and servo_id in s.previous_ids and s.servo_id != servo_id]
        answers = [s._on_ping() for s in servos]
        return answers.count(True) == 1

    def broadcast_ping(self, wait_s=None):
        replies, junk = [], self.noise
        live = [s for s in self.servos if s.baud == self._baud and s._answers()]
        ids = [s.servo_id for s in live]
        for sid in sorted(set(ids)):
            if ids.count(sid) == 1:
                replies.append((sid, 0))
            else:
                junk += 12 * ids.count(sid)        # colliding replies garble
        return replies, junk

    def read(self, servo_id, addr, length):
        servo = self._one(servo_id)
        return servo.ram_status, servo.read(addr, length)

    def read_u16(self, servo_id, addr):
        _err, data = self.read(servo_id, addr, 2)
        return fb.from_le16(data[0], data[1])

    def write(self, servo_id, addr, data, await_status=True):
        fb.assert_safe_torque_enable(addr, data)
        self.writes.append((servo_id, addr, bytes(data)))
        targets = [s for s in self._at(servo_id) if s._answers()]
        for servo in targets:
            level = servo.mem[fb.REG_RESPONSE_STATUS_LEVEL]
            servo.write(addr, data)
            if await_status and (level == 0 or len(targets) > 1):
                raise fb.FeetechBusTimeout('fake: no WRITE reply')
        if await_status and not targets:
            raise fb.FeetechBusTimeout(f'fake: id {servo_id} silent')
        return 0


class FakePort:
    """``open_bus(baud)`` over a fixed set of servos; records every baud opened."""

    def __init__(self, servos, noise_by_baud=None):
        self.servos = list(servos) if isinstance(servos, (list, tuple)) else [servos]
        self.noise_by_baud = dict(noise_by_baud or {})
        self.opened: list[int] = []
        self.buses: list[FakeServoBus] = []

    def __call__(self, baud):
        self.opened.append(baud)
        bus = FakeServoBus(self.servos, baud, noise=self.noise_by_baud.get(baud, 0))
        self.buses.append(bus)
        return bus

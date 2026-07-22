#!/usr/bin/env python3
"""edu6 vendor-bench provisioning — calibrate once, forever (plan §6).

Run ONCE per arm on the assembly bench, with the arm jigged to the mechanical
reference pose (every joint at its designed zero, gripper fully CLOSED):

    python3 tools/edu6_provision.py --port /dev/ttyACM0 --serial EDU6-0001

What it does, in the §6 order (order matters — limits are interpreted in the
offset-corrected frame):

1. Torque OFF, then **write Lock = 0 EXPLICITLY** (addr 55; 0 = EEPROM writes
   persist, 1 = protected — the polarity is inverted from intuition, and
   torque-off does NOT implicitly unlock).
2. Read the jigged Present_Position of every servo and write ``Homing_Offset``
   so the reference pose reads its DESIGNED tick value.
3. Write the DESIGNED ``Min/Max_Position_Limit`` from the URDF (never swept
   ranges — "how far the human waved it" is too sloppy for a student-facing
   IK model).
4. Write the safety EEPROM in the same pass: ``Max_Torque_Limit`` (the §8
   pinch floor — R2/R4 pending values), ``Protection_Current``,
   ``Overload_Torque``, clear ``Phase`` bit 4, ``Return_Delay_Time = 0``.
5. Re-lock (Lock = 1), read EVERY register back and VERIFY.
6. Emit the per-arm record (7 × {id, homing_offset, range_min, range_max,
   model, firmware} + arm serial + sha256 checksum) — **the record is the
   commit point**: all servos written → read back → THEN persisted. A
   half-written arm has no valid record and fails loudly.
   Stored locally (``edu6_records/<serial>.json``) and, when SUPABASE_URL +
   SUPABASE_SERVICE_ROLE_KEY are exported, upserted into the
   ``edu6_arm_records`` table (migration 036) keyed by the arm serial
   (the vendor STICKER serial — decision Q3).

NEVER write EEPROM unconditionally: every write path is read-compare-write.
EEPROM endurance is unverified — treat it as a five-figure budget.

The gripper servo (id 7) is jigged CLOSED (jaws touching) = its designed zero.
Servo direction signs are the ``--signs`` option (R6 fixes the convention;
the same values ship as EDUBOTICS_EDU6_JOINT_SIGNS).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                       / 'robotis_ai_setup' / 'docker' / 'open_manipulator'))

import feetech_bus as fb  # noqa: E402


SERVO_IDS = (1, 2, 3, 4, 5, 6, 7)
CENTER_TICK = 2048
TICKS_PER_REV = 4096
RAD_PER_TICK = 2.0 * math.pi / TICKS_PER_REV

# Designed joint limits (URDF; the gripper band mirrors the driver node).
JOINT_LIMITS_RAD = (
    (-1.5708, 1.5708), (0.0, 3.1416), (-3.1416, 0.0), (-3.1416, 3.1416),
    (-1.5708, 1.9199), (-3.1416, 3.1416), (0.0, 1.79),
)

# Safety EEPROM values (plan §8; the gripper pinch floor is R2/R4-pending —
# these are the shipping defaults, overridable per bench run).
ARM_MAX_TORQUE = 800        # arm joints must hold their own weight
GRIPPER_MAX_TORQUE = 150    # ≈10 N pinch at the assumed 45 mm lever (R4 gate)
PROTECTION_CURRENT = 150    # ×6.5 mA
OVERLOAD_TORQUE = 80
RETURN_DELAY = 0


def designed_zero_tick(_joint_index: int) -> int:
    """Every joint's jig pose reads CENTER_TICK after the offset write; the
    gripper (index 6) is jigged CLOSED = its designed zero too."""
    return CENTER_TICK


def limits_to_ticks(joint_index: int, signs) -> tuple[int, int]:
    """Designed URDF limits → tick window around the designed zero, sign-aware
    (a −1 sign swaps and mirrors the window)."""
    lo_rad, hi_rad = JOINT_LIMITS_RAD[joint_index]
    sign = signs[joint_index]
    a = CENTER_TICK + int(round(lo_rad * sign / RAD_PER_TICK))
    b = CENTER_TICK + int(round(hi_rad * sign / RAD_PER_TICK))
    lo, hi = (a, b) if a <= b else (b, a)
    return max(0, lo), min(TICKS_PER_REV - 1, hi)


def record_checksum(entries: list[dict], serial: str) -> str:
    payload = json.dumps({'serial': serial, 'servos': entries},
                         sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _write_verify_u8(bus, sid, addr, value, label):
    current = bus.read(sid, addr, 1)[1][0]
    if current == value:
        return False
    bus.write(sid, addr, bytes([value]))
    back = bus.read(sid, addr, 1)[1][0]
    if back != value:
        raise SystemExit(
            f'[FEHLER] Servo {sid}: {label} verify failed ({back} != {value})')
    return True


def _write_verify_u16(bus, sid, addr, value, label):
    current = bus.read_u16(sid, addr)
    if current == value:
        return False
    bus.write(sid, addr, fb.le16(value))
    back = bus.read_u16(sid, addr)
    if back != value:
        raise SystemExit(
            f'[FEHLER] Servo {sid}: {label} verify failed ({back} != {value})')
    return True


def provision(bus, serial: str, signs, dry_run: bool = False) -> dict:
    # 0. identity gate
    for sid in SERVO_IDS:
        if not bus.ping(sid):
            raise SystemExit(f'[FEHLER] Servo {sid} antwortet nicht — 12-V-'
                             'Netzteil / Verkabelung prüfen.')
        model = bus.read_u16(sid, fb.REG_MODEL_NUMBER)
        if model != fb.STS3215_MODEL_NUMBER:
            raise SystemExit(f'[FEHLER] Servo {sid}: Modell {model}, erwartet '
                             f'{fb.STS3215_MODEL_NUMBER} (STS3215).')
    print('[OK] Alle 7 Servos gefunden (STS3215).')
    if dry_run:
        print('[DRY-RUN] Keine EEPROM-Schreibzugriffe.')

    entries = []
    for i, sid in enumerate(SERVO_IDS):
        # 1. torque off + EXPLICIT unlock
        if not dry_run:
            bus.write(sid, fb.REG_TORQUE_ENABLE, b'\x00')
            _write_verify_u8(bus, sid, fb.REG_LOCK, 0, 'Lock=0')

        # 2. homing offset: jigged position must READ the designed zero.
        present = bus.read_u16(sid, fb.REG_PRESENT_POSITION)
        present = fb.decode_sign_magnitude(present, 15)
        current_off = fb.decode_sign_magnitude(
            bus.read_u16(sid, fb.REG_HOMING_OFFSET), 11)
        # Present = Actual − Homing_Offset  ⇒  new_off = actual − designed.
        actual = present + current_off
        new_off = actual - designed_zero_tick(i)
        if abs(new_off) >= (1 << 11):
            raise SystemExit(
                f'[FEHLER] Servo {sid}: Homing-Offset {new_off} außerhalb des '
                'sign-magnitude-11-Bit-Bereichs — Jig-Position prüfen.')
        if not dry_run and new_off != current_off:
            bus.write(sid, fb.REG_HOMING_OFFSET,
                      fb.le16(fb.encode_sign_magnitude(new_off, 11)))
            back = fb.decode_sign_magnitude(
                bus.read_u16(sid, fb.REG_HOMING_OFFSET), 11)
            if back != new_off:
                raise SystemExit(f'[FEHLER] Servo {sid}: Homing-Offset verify '
                                 f'failed ({back} != {new_off}).')

        # 3. designed position limits (AFTER the offset — corrected frame).
        lo, hi = limits_to_ticks(i, signs)
        if not dry_run:
            _write_verify_u16(bus, sid, fb.REG_MIN_POSITION_LIMIT, lo, 'Min_Position')
            _write_verify_u16(bus, sid, fb.REG_MAX_POSITION_LIMIT, hi, 'Max_Position')

        # 4. safety EEPROM
        max_torque = GRIPPER_MAX_TORQUE if sid == SERVO_IDS[-1] else ARM_MAX_TORQUE
        if not dry_run:
            _write_verify_u16(bus, sid, fb.REG_MAX_TORQUE_LIMIT, max_torque,
                              'Max_Torque')
            _write_verify_u16(bus, sid, fb.REG_PROTECTION_CURRENT,
                              PROTECTION_CURRENT, 'Protection_Current')
            _write_verify_u8(bus, sid, fb.REG_OVERLOAD_TORQUE, OVERLOAD_TORQUE,
                             'Overload_Torque')
            phase = bus.read(sid, fb.REG_PHASE, 1)[1][0]
            if phase & (1 << 4):
                _write_verify_u8(bus, sid, fb.REG_PHASE, phase & ~(1 << 4),
                                 'Phase-Bit4')
            _write_verify_u8(bus, sid, 7, RETURN_DELAY, 'Return_Delay')
            # 5. re-lock
            _write_verify_u8(bus, sid, fb.REG_LOCK, 1, 'Lock=1')

        firmware = bus.read(sid, 0, 2)[1]
        entries.append({
            'id': sid,
            'homing_offset': new_off,
            'range_min': lo,
            'range_max': hi,
            'max_torque': max_torque,
            'model_number': fb.STS3215_MODEL_NUMBER,
            'firmware': f'{firmware[0]}.{firmware[1]}',
        })
        print(f'[OK] Servo {sid}: offset={new_off} limits=[{lo},{hi}] '
              f'torque={max_torque}')

    record = {
        'arm_serial': serial,
        'profile_id': 'edu6_studio',
        'signs': list(signs),
        'servos': entries,
        'checksum': record_checksum(entries, serial),
        'provisioned_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    return record


def persist(record: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{record['arm_serial']}.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + '\n',
                    encoding='utf-8')
    print(f'[OK] Lokale Kalibrierdatei: {path}')
    url = os.environ.get('SUPABASE_URL', '').strip()
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '').strip()
    if url and key:
        try:
            from supabase import create_client
            sb = create_client(url, key)
            sb.table('edu6_arm_records').upsert({
                'arm_serial': record['arm_serial'],
                'record': record,
            }).execute()
            print('[OK] Supabase edu6_arm_records aktualisiert.')
        except Exception as e:  # noqa: BLE001 — the local file is the commit
            print(f'[WARNUNG] Supabase-Upload fehlgeschlagen (lokale Datei '
                  f'bleibt gültig): {e}')
    else:
        print('[INFO] SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY nicht gesetzt — '
              'nur lokale Datei geschrieben.')
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--port', required=True)
    ap.add_argument('--serial', required=True,
                    help='vendor sticker serial, e.g. EDU6-0001')
    ap.add_argument('--signs', default='1,1,1,1,1,1,1',
                    help='7 comma-separated ±1 joint direction signs (R6)')
    ap.add_argument('--out', default=str(Path(__file__).parent / 'edu6_records'))
    ap.add_argument('--dry-run', action='store_true',
                    help='read + report only; no EEPROM writes')
    args = ap.parse_args()

    signs = tuple(int(v) for v in args.signs.split(','))
    if len(signs) != 7 or any(v not in (-1, 1) for v in signs):
        print('[FEHLER] --signs braucht 7 Werte aus {1,-1}.', file=sys.stderr)
        return 2

    bus = fb.FeetechBus(args.port)
    try:
        record = provision(bus, args.serial.strip(), signs,
                           dry_run=args.dry_run)
    finally:
        bus.close()
    if args.dry_run:
        print(json.dumps(record, indent=2, sort_keys=True))
        return 0
    persist(record, Path(args.out))
    print('[OK] Provisionierung abgeschlossen — Arm ist dauerhaft kalibriert.')
    return 0


if __name__ == '__main__':
    sys.exit(main())

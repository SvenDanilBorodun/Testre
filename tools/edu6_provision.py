#!/usr/bin/env python3
"""edu6 vendor-bench provisioning — calibrate once, forever (plan §6).

Run ONCE per arm on the assembly bench, with the arm jigged to the mechanical
reference pose (every joint at its designed zero, gripper fully CLOSED):

    python3 tools/edu6_provision.py --port /dev/ttyACM0 --serial EDU6-0001

What it does, in the §6 order (order matters — limits are interpreted in the
offset-corrected frame, and the offset must be baked under the SAME
position-reporting semantics the runtime will use):

1. Normalize ``Response_Status_Level`` (addr 8) to 1 if a servo ships 0 — a
   level-0 servo answers READ/PING but never acks a WRITE, so every awaited
   write below would time out. This repair necessarily runs un-awaited
   (unlock + level write), then read-verifies.
2. Torque OFF (verified), then **write Lock = 0 EXPLICITLY** (addr 55; 0 =
   EEPROM writes persist, 1 = protected — the polarity is inverted from
   intuition, and torque-off does NOT implicitly unlock).
3. Clear ``Phase`` bit 4 (multi-turn feedback mode) BEFORE the offset step —
   read-modify-write, only-when-set. The jig read that bakes the offset must
   happen under post-clear feedback semantics (audit M1), and the other
   Phase bits are motor-drive configuration that must never be touched.
4. Read the jigged Present_Position of every servo and write ``Homing_Offset``
   so the reference pose reads its DESIGNED tick value; re-read and hard-fail
   unless the jig now reads designed ±4 ticks.
5. Write the DESIGNED ``Min/Max_Position_Limit`` from the URDF (never swept
   ranges — "how far the human waved it" is too sloppy for a student-facing
   IK model), via the ONE shared window implementation in ``feetech_bus``
   (the driver's boot probe verifies the same windows — audit H1).
6. Write the safety EEPROM in the same pass: ``Max_Torque_Limit`` (the §8
   pinch floor — R2/R4 pending values), ``Protection_Current``,
   ``Overload_Torque``, **``Operating_Mode = 0``** (position — a wheel-mode
   servo would turn boot-home into a continuous-rotation runaway that
   position limits cannot stop, audit H2), ``Return_Delay_Time = 0``.
7. Re-lock (Lock = 1), read EVERY register back and VERIFY.
8. FINAL jig re-verify across all 7 servos (the arm stays jigged for the
   whole run): every servo must still read designed ±4 ticks — closes any
   post-offset semantic shift a per-servo gate can miss.
9. Emit the per-arm record (7 × {id, homing_offset, range_min, range_max,
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

# Safety EEPROM values (plan §8; the gripper pinch floor is R2/R4-pending).
# These are compile-time constants ON PURPOSE — a safety floor must not be a
# casual command-line argument, so there is NO per-bench-run override flag.
# Change the value here and re-run the bench provisioning to move the floor.
ARM_MAX_TORQUE = 800        # arm joints must hold their own weight
GRIPPER_MAX_TORQUE = 150    # ≈10 N pinch at the assumed 45 mm lever (R4 gate)
# Overcurrent trip (addr 28, ×6.5 mA). Bench decision 2026-07-24: keep the
# factory ~2 A (310) rather than the plan's 975 mA — the STS3250 shoulder/
# elbow (joints 2/3) draw more than 975 mA holding the arm's weight, so a
# lower trip nuisance-fires (servo cuts torque → arm sags). The real force
# safety is Max_Torque + the factory sustained-overload protection (addr
# 34/35/36, untouched). Finalize from live Present_Current at rig gate R6.
PROTECTION_CURRENT = 310    # ×6.5 mA ≈ 2015 mA (R6-tune)
OVERLOAD_TORQUE = 80
RETURN_DELAY = 0

# The jigged arm must READ its designed tick after the Homing_Offset write.
# A few ticks of encoder noise is fine; anything larger means the offset (or its
# sign) is wrong and the whole kinematic model would run skewed.
JIG_POSE_TOLERANCE_TICKS = 4


def designed_zero_tick(_joint_index: int) -> int:
    """Every joint's jig pose reads CENTER_TICK after the offset write; the
    gripper (index 6) is jigged CLOSED = its designed zero too."""
    return CENTER_TICK


def limits_to_ticks(joint_index: int, signs) -> tuple[int, int]:
    """Designed URDF limits → tick window around the designed zero, sign-aware
    (a −1 sign swaps and mirrors the window). Delegates to the ONE shared
    implementation in ``feetech_bus`` — the driver node's boot probe verifies
    the exact same windows against the plugged arm (audit H1 no-drift)."""
    lo_rad, hi_rad = JOINT_LIMITS_RAD[joint_index]
    return fb.position_limit_window(lo_rad, hi_rad, signs[joint_index])


def record_checksum(entries: list[dict], serial: str, signs) -> str:
    # `signs` participate in the checksum: the same servo EEPROM under a
    # different direction convention is a DIFFERENT calibration, so the record
    # digest must change with it (pre-ship — no backward-compat concern).
    payload = json.dumps(
        {'serial': serial, 'signs': list(signs), 'servos': entries},
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
            f'[FEHLER] Servo {sid}: {label} stimmt nach dem Schreiben nicht '
            f'überein ({back} != {value}) — EEPROM-Schreibzugriff prüfen.')
    return True


def _write_verify_u16(bus, sid, addr, value, label):
    current = bus.read_u16(sid, addr)
    if current == value:
        return False
    bus.write(sid, addr, fb.le16(value))
    back = bus.read_u16(sid, addr)
    if back != value:
        raise SystemExit(
            f'[FEHLER] Servo {sid}: {label} stimmt nach dem Schreiben nicht '
            f'überein ({back} != {value}) — EEPROM-Schreibzugriff prüfen.')
    return True


def provision(bus, serial: str, signs, dry_run: bool = False) -> dict:
    # 0. identity gate. The edu6 arm mixes STS3215 (joints 1/4/5/6/7) and
    # STS3250 (joints 2/3) BY DESIGN — accept the STS-series set and record
    # each servo's ACTUAL model (never hardcode 777).
    models: dict[int, int] = {}
    for sid in SERVO_IDS:
        if not bus.ping(sid):
            raise SystemExit(f'[FEHLER] Servo {sid} antwortet nicht — 12-V-'
                             'Netzteil / Verkabelung prüfen.')
        model = bus.read_u16(sid, fb.REG_MODEL_NUMBER)
        if model not in fb.STS_ACCEPTED_MODELS:
            accepted = ', '.join(
                f'{fb.STS_MODEL_NAMES[m]} ({m})'
                for m in sorted(fb.STS_ACCEPTED_MODELS))
            raise SystemExit(f'[FEHLER] Servo {sid}: Modell {model}, erwartet '
                             f'{accepted} — dieser Arm ist kein '
                             'EduBotics 6-Achs.')
        models[sid] = model
    summary = ', '.join(f'{sid}:{fb.STS_MODEL_NAMES[models[sid]]}'
                        for sid in SERVO_IDS)
    print(f'[OK] Alle 7 Servos gefunden ({summary}).')
    if dry_run:
        print('[DRY-RUN] Keine EEPROM-Schreibzugriffe.')

    entries = []
    for i, sid in enumerate(SERVO_IDS):
        # 1. Response_Status_Level normalize — MUST precede the first awaited
        # write: a level-0 servo never acks a WRITE, so the verified torque-off
        # below would time out before we could repair the level. The repair
        # pair (unlock + level) therefore runs un-awaited, then read-verifies.
        level = bus.read(sid, fb.REG_RESPONSE_STATUS_LEVEL, 1)[1][0]
        if level != 1 and not dry_run:
            bus.write(sid, fb.REG_LOCK, b'\x00', await_status=False)
            bus.write(sid, fb.REG_RESPONSE_STATUS_LEVEL, b'\x01',
                      await_status=False)
            back = bus.read(sid, fb.REG_RESPONSE_STATUS_LEVEL, 1)[1][0]
            if back != 1:
                raise SystemExit(
                    f'[FEHLER] Servo {sid}: Response-Status-Level lässt sich '
                    f'nicht auf 1 setzen (liest {back}) — Servo prüfen.')
            print(f'[OK] Servo {sid}: Response-Status-Level auf 1 normalisiert.')

        # 2. torque off (verified — audit L5) + EXPLICIT unlock
        if not dry_run:
            _write_verify_u8(bus, sid, fb.REG_TORQUE_ENABLE, 0, 'Torque=0')
            _write_verify_u8(bus, sid, fb.REG_LOCK, 0, 'Lock=0')

            # 3. Phase bit 4 BEFORE the offset step (audit M1): the jig read
            # that bakes the offset must happen under the SAME position-
            # reporting semantics the runtime will use. Read-modify-write and
            # ONLY when set — the other Phase bits are motor-drive
            # configuration (a blind write could invert a coil phase).
            phase = bus.read(sid, fb.REG_PHASE, 1)[1][0]
            if phase & (1 << 4):
                _write_verify_u8(bus, sid, fb.REG_PHASE, phase & ~(1 << 4),
                                 'Phase-Bit4')

        # 4. homing offset: jigged position must READ the designed zero.
        present = bus.read_u16(sid, fb.REG_PRESENT_POSITION)
        present = fb.decode_sign_magnitude(present, 15)
        current_off = fb.decode_sign_magnitude(
            bus.read_u16(sid, fb.REG_HOMING_OFFSET), 11)
        # Present = Actual − Homing_Offset  ⇒  Actual = Present + Offset. The
        # encoder is single-turn absolute (mod 4096), so this reconstruction
        # MUST wrap: a jig pose whose raw tick sits near the 0/4095 boundary
        # (bench-observed on J5, raw 23 with a +85 legacy offset) makes the
        # naive sum exceed 4095 → a phantom > ±2047 offset that false-aborts.
        # mod-4096 recovers the true raw; new_off = raw − designed is then in
        # [−2048, 2047] and only raw == 0 (offset −2048) genuinely overflows.
        actual = (present + current_off) % TICKS_PER_REV
        new_off = actual - designed_zero_tick(i)
        if abs(new_off) >= (1 << 11):
            raise SystemExit(
                f'[FEHLER] Servo {sid}: Homing-Offset {new_off} außerhalb des '
                'sign-magnitude-11-Bit-Bereichs — das Gelenk steht genau auf '
                'der Encoder-Grenze (Rohwert 0); bitte minimal verdrehen und '
                'erneut provisionieren.')
        if not dry_run and new_off != current_off:
            bus.write(sid, fb.REG_HOMING_OFFSET,
                      fb.le16(fb.encode_sign_magnitude(new_off, 11)))
            back = fb.decode_sign_magnitude(
                bus.read_u16(sid, fb.REG_HOMING_OFFSET), 11)
            if back != new_off:
                raise SystemExit(
                    f'[FEHLER] Servo {sid}: Homing-Offset stimmt nach dem '
                    f'Schreiben nicht überein ({back} != {new_off}).')

        # Postcondition: on the STILL-JIGGED arm, Present_Position must now READ
        # the designed tick (Present = Actual − Homing_Offset). The offset
        # REGISTER read-back above can verify correct while a sign/decode slip
        # still leaves the produced position DOUBLED — this re-read is the only
        # check that closes that silent 2×-offset-skew hole.
        if not dry_run:
            present_after = fb.decode_sign_magnitude(
                bus.read_u16(sid, fb.REG_PRESENT_POSITION), 15)
            designed = designed_zero_tick(i)
            if abs(present_after - designed) > JIG_POSE_TOLERANCE_TICKS:
                raise SystemExit(
                    f'[FEHLER] Servo {sid}: Jig-Position liest nach dem '
                    f'Offset-Schreiben {present_after} statt {designed} '
                    f'(Abweichung {present_after - designed} Ticks) — '
                    'Homing-Offset/Vorzeichen prüfen und erneut '
                    'provisionieren.')

        # 5. designed position limits (AFTER the offset — corrected frame).
        lo, hi = limits_to_ticks(i, signs)
        if not dry_run:
            _write_verify_u16(bus, sid, fb.REG_MIN_POSITION_LIMIT, lo, 'Min_Position')
            _write_verify_u16(bus, sid, fb.REG_MAX_POSITION_LIMIT, hi, 'Max_Position')

        # 6. safety EEPROM
        max_torque = GRIPPER_MAX_TORQUE if sid == SERVO_IDS[-1] else ARM_MAX_TORQUE
        if not dry_run:
            _write_verify_u16(bus, sid, fb.REG_MAX_TORQUE_LIMIT, max_torque,
                              'Max_Torque')
            _write_verify_u16(bus, sid, fb.REG_PROTECTION_CURRENT,
                              PROTECTION_CURRENT, 'Protection_Current')
            _write_verify_u8(bus, sid, fb.REG_OVERLOAD_TORQUE, OVERLOAD_TORQUE,
                             'Overload_Torque')
            # Position mode, asserted — a wheel-mode servo passes every other
            # check and turns the boot-home into a continuous-rotation runaway
            # that EEPROM position limits cannot stop (audit H2).
            _write_verify_u8(bus, sid, fb.REG_OPERATING_MODE, 0,
                             'Mode=Position')
            _write_verify_u8(bus, sid, fb.REG_RETURN_DELAY, RETURN_DELAY,
                             'Return_Delay')
            # 7. re-lock
            _write_verify_u8(bus, sid, fb.REG_LOCK, 1, 'Lock=1')

        firmware = bus.read(sid, fb.REG_FIRMWARE_MAJOR, 2)[1]
        entries.append({
            'id': sid,
            'homing_offset': new_off,
            'range_min': lo,
            'range_max': hi,
            'max_torque': max_torque,
            'model_number': models[sid],
            'model_name': fb.STS_MODEL_NAMES[models[sid]],
            'firmware': f'{firmware[0]}.{firmware[1]}',
        })
        print(f'[OK] Servo {sid}: offset={new_off} limits=[{lo},{hi}] '
              f'torque={max_torque}')

    # 8. FINAL jig re-verify (audit M1): after EVERY write of the run —
    # including the Phase-bit-4 clears — the still-jigged arm must read the
    # designed pose on all 7 servos. A per-servo gate cannot catch a semantic
    # shift caused by a later write; this one can. The arm must stay jigged
    # until the tool prints its closing [OK].
    if not dry_run:
        for i, sid in enumerate(SERVO_IDS):
            present = fb.decode_sign_magnitude(
                bus.read_u16(sid, fb.REG_PRESENT_POSITION), 15)
            designed = designed_zero_tick(i)
            if abs(present - designed) > JIG_POSE_TOLERANCE_TICKS:
                raise SystemExit(
                    f'[FEHLER] Jig-Endkontrolle: Servo {sid} liest {present} '
                    f'statt {designed} (Abweichung {present - designed} '
                    'Ticks) — den Arm im Jig belassen und die '
                    'Provisionierung erneut ausführen.')
        print('[OK] Jig-Endkontrolle: alle 7 Servos lesen die Soll-Position.')

    record = {
        'arm_serial': serial,
        'profile_id': 'edu6_studio',
        'signs': list(signs),
        'servos': entries,
        'checksum': record_checksum(entries, serial, signs),
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

    # EEPROM writes (Homing_Offset, limits, torque caps…) trigger real EEPROM
    # programming (~tens of ms) once Lock=0, far longer than the driver's 20 ms
    # RAM-reply budget — bench-observed 2026-07-24. A generous reply timeout is
    # a MAX wait, not a fixed delay: fast reads/RAM writes still return the
    # instant the reply lands, so this only affects the slow EEPROM commits.
    bus = fb.FeetechBus(args.port, timeout_s=0.2)
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

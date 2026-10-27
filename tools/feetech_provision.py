#!/usr/bin/env python3
"""Feetech vendor-bench provisioning — calibrate once, forever.

Serves the V1 Roboter-Studio arm; ``--arm`` selects the arm spec and defaults
to ``v1``.

Writes the SAME golden EEPROM image as the Servo-Station, from the ONE servo
spec (``robotis_ai_setup/docker/open_manipulator/feetech_servo_spec.py``): the
centred encoder zero T0 per joint, the spec's windows, the per-model protection
current and every other refuse/warn register. While the spec is unpinned
(before rig gate P0) it writes nothing; ``--dry-run`` still reads and reports.

Run ONCE per arm on the assembly bench, with the arm jigged to the mechanical
reference pose (every joint at its designed zero q = 0, so each servo reads its
T0 afterwards, and the claw fully CLOSED at its T0):

    python3 tools/feetech_provision.py --arm v1 --port /dev/ttyACM0 \
        --serial V1-0001

What it does, in this order (order matters — limits are interpreted in the
offset-corrected frame, and the offset must be baked under the SAME
position-reporting semantics the runtime will use):

0. Identity gate for every servo, before any write: it answers, it is the
   spec's STS model for its joint, and (spec pinned) it is the 12-V variant
   (Max-Spannung) with the reference Phase byte.
1. Normalize ``Response_Status_Level`` (addr 8) to 1 if a servo ships 0 — a
   level-0 servo answers READ/PING but never acks a WRITE, so every awaited
   write below would time out. This repair necessarily runs un-awaited
   (unlock + level write), then read-verifies.
2. Torque OFF (verified), then **write Lock = 0 EXPLICITLY** (addr 55; 0 =
   EEPROM writes persist, 1 = protected — the polarity is inverted from
   intuition, and torque-off does NOT implicitly unlock).
3. Clear ``Phase`` bit 4 (multi-turn feedback mode) and normalize
   ``Angular_Resolution`` (addr 30) to 1, both BEFORE the offset step —
   read-modify-write / read-compare-write, only-when-needed. These are the TWO
   registers the vendor documents as changing the tick↔angle map, and the jig
   read that bakes the offset must happen under the SAME map the runtime will
   use (audit M1, which applies verbatim to the resolution). The other
   Phase bits are motor-drive configuration that must never be touched.
4. Write the rest of the golden image (the spec's refuse/warn registers in
   layout order, among them ``Max_Torque_Limit`` — the pinch floor, rig gate
   VR5 —, the per-model ``Protection_Current``, ``Overload_Torque`` and
   **``Operating_Mode = 0``**: a wheel-mode servo would turn boot-home into a
   continuous-rotation runaway that position limits cannot stop, audit H2).
   A refuse-class register that does not read back hard-fails; a warn-class
   one prints a ``[WARNUNG]``.
5. Read the jigged Present_Position of every servo and write the wrap-aware
   ``Homing_Offset`` so the reference pose reads the joint's T0; re-read and
   hard-fail unless the jig now reads T0 ±4 ticks.
6. Write the spec's ``Min/Max_Position_Limit`` window (centred around T0,
   from the URDF limits — never swept ranges), the SAME window the driver's
   boot probe verifies (audit H1).
7. Re-lock (Lock = 1), read the whole EEPROM block back and compare it with
   the spec's image (the boot probe's own comparison).
8. FINAL jig re-verify across EVERY servo (the arm stays jigged for the
   whole run): every servo must still read T0 ±4 ticks — closes any
   post-offset semantic shift a per-servo gate can miss.
9. Emit the per-arm record (``profile_id``, the spec's signs, tick directions
   and digest, one entry per servo with its offset, window, model, firmware
   and the 40-byte EEPROM block + arm serial + sha256 checksum) — **the record
   is the commit point**: all servos written → read back → THEN persisted. A
   half-written arm has no valid record and fails loudly.
   Stored locally (``<arm>_records/<serial>.json``) and, when SUPABASE_URL +
   SUPABASE_SERVICE_ROLE_KEY are exported, upserted into the
   ``edu6_arm_records`` table (migration 036) keyed by the arm serial
   (the vendor STICKER serial). That table is SHARED by every Feetech arm —
   it was named before the V1 existed, and renaming a fielded table is worse
   than a name that reads narrow — so ``profile_id`` is the ONLY thing in the
   artifact that says which arm a record describes.

NEVER write EEPROM unconditionally: every write path is read-compare-write.
EEPROM endurance is unverified — treat it as a five-figure budget.

The claw servo (the LAST id, 6) is jigged CLOSED (blades touching) = q 0,
which reads its T0 afterwards. The direction signs come from the servo spec
(rig gate VR3 confirms them); there is no command-line override.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                       / 'robotis_ai_setup' / 'docker' / 'open_manipulator'))

import feetech_bus as fb  # noqa: E402
import feetech_servo_spec as fss  # noqa: E402


# ── arm spec (V1; `--arm` rebinds them in main) ──────────────────────────────
# From the ONE servo spec the driver's boot probe verifies against, so the two
# can never drift.
SERVO_IDS = fss.SERVO_IDS
JOINT_LIMITS_RAD = fss.JOINT_LIMITS_RAD

# Per-arm specs.
_ARM_SPECS = {
    'v1': {
        'servo_ids': fss.SERVO_IDS,
        'joint_limits_rad': fss.JOINT_LIMITS_RAD,
        'records_dir': 'v1_records',
        'serial_example': 'V1-0001',
        # THE machine-readable family tag on the emitted record, and the only
        # one: `persist` upserts the whole record as a single jsonb blob into a
        # table whose schema is (arm_serial, record, timestamps), so nothing
        # else in the artifact says which arm it describes. It is deliberately
        # NOT in `record_checksum` (which hashes serial+signs+servos).
        'profile_id': fss.PROFILE_ID,
        # Product name for the German identity-gate refusal. Naming the wrong
        # product at the moment a foreign servo is found is worse than saying
        # nothing: the operator reaches for the wrong arm's checklist.
        'display_de': 'V1',
    },
}


def apply_arm_spec(arm: str) -> dict:
    """Rebind the module-level arm constants for ``arm`` and return its spec.

    A REBIND rather than threading the spec through every helper: `provision`
    and its callees read these as module globals, and the alternative is a
    dozen signature changes to a bench tool. Called ONCE from main(), before
    any bus I/O.
    """
    global SERVO_IDS, JOINT_LIMITS_RAD
    spec = _ARM_SPECS[arm]
    SERVO_IDS = spec['servo_ids']
    JOINT_LIMITS_RAD = spec['joint_limits_rad']
    return spec

# The safety EEPROM values (Max_Torque — the claw's pinch floor, rig gate VR5 —,
# the per-model protection current, the overload torque …) are the servo
# spec's golden image. They are spec constants ON PURPOSE — a safety floor must
# not be a casual command-line argument, so there is NO per-bench-run override.

# The jigged arm must READ its T0 after the Homing_Offset write.
# A few ticks of encoder noise is fine; anything larger means the offset (or its
# sign) is wrong and the whole kinematic model would run skewed.
JIG_POSE_TOLERANCE_TICKS = 4


def designed_zero_tick(joint_index: int, servo_spec=fss.V1) -> int:
    """The tick a jigged joint (q = 0) reads after the offset write: the
    spec's centred T0. The claw (the last index) is jigged CLOSED = q 0 too."""
    if servo_spec.t0 is None:
        raise ValueError('spec not pinned')
    return servo_spec.t0[joint_index]


def limits_to_ticks(joint_index: int, servo_spec=fss.V1) -> tuple[int, int]:
    """The joint's EEPROM window from the servo spec — the same for both tick
    directions, and exactly what the driver node's boot probe verifies against
    the plugged arm (audit H1 no-drift)."""
    return servo_spec.windows[joint_index]


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


def _write_warn(bus, sid, addr, size, value, name):
    """A warn-class register: read-compare-write like every write here, but a
    value that does not stick only WARNS (the boot probe boots with it too)."""
    def read():
        return (bus.read(sid, addr, 1)[1][0] if size == 1
                else bus.read_u16(sid, addr))
    if read() == value:
        return
    bus.write(sid, addr, bytes([value]) if size == 1 else fb.le16(value))
    back = read()
    if back != value:
        print(f'[WARNUNG] Servo {sid}: {name} (Register {addr}) bleibt {back} '
              f'statt {value}.')


# The golden-image registers the image step leaves to a step of their own (or
# to nobody): id, baud and status level are never changed by this tool, the
# limits, the Phase bit and the resolution have their own steps above or
# below, and register 25 is only verified.
_IMAGE_STEP_SKIP = frozenset({
    fb.REG_ID, fb.REG_BAUD_RATE, fb.REG_RESPONSE_STATUS_LEVEL,
    fb.REG_MIN_POSITION_LIMIT, fb.REG_MAX_POSITION_LIMIT, fb.REG_PHASE,
    fb.REG_ANGULAR_RESOLUTION, fb.REG_25})


def _jig_reads(present: int, t0: int) -> bool:
    """The jigged servo reads its T0: inside the register's 0–4095 and within
    the tolerance, compared LINEARLY like the boot probe. Never round the
    circle: a firmware that does not fold Present reads T0 ± 4096 after an
    offset across the seam, which the boot probe refuses."""
    return (0 <= present < fb.TICKS_PER_REV
            and abs(present - t0) <= JIG_POSE_TOLERANCE_TICKS)


def provision(bus, serial: str, spec: dict, *, servo_spec=fss.V1,
              dry_run: bool = False) -> dict:
    """Provision one jigged arm and return its record.

    ``spec`` is REQUIRED and is the arm's row from :data:`_ARM_SPECS`. It is
    threaded in rather than read off the module globals for one reason: the
    record's ``profile_id`` and the identity-gate wording are the two things a
    per-arm rebind kept missing, and a caller that forgets an argument fails at
    once, where a caller that forgets a rebind silently stamps the wrong arm.
    ``SERVO_IDS`` / ``JOINT_LIMITS_RAD`` stay module-level (the deps-free suite
    compares them against the driver node's literals), so the two halves must
    AGREE — asserted below rather than assumed.

    ``servo_spec`` (default the shipped ``fss.V1``) decides everything that is
    written: T0, the windows and the golden image. While it is unpinned nothing
    is written; a dry run still reads and reports (without T0).
    """
    if tuple(spec['servo_ids']) != tuple(SERVO_IDS):
        # Half-migration guard: `apply_arm_spec` rebinds the globals every
        # helper reads, `spec` carries what lands in the record. If they ever
        # disagree the arm gets one family's EEPROM window under the other
        # family's tag — a record that verifies against nothing.
        raise SystemExit(
            '[FEHLER] Interner Fehler: Arm-Spezifikation und geladene '
            'Servo-Liste passen nicht zusammen — apply_arm_spec() wurde nicht '
            'für denselben Arm aufgerufen.')
    if tuple(spec['joint_limits_rad']) != tuple(JOINT_LIMITS_RAD):
        # The OTHER half of the same guard: the limits define the
        # Min/Max_Position_Limit window the driver's boot probe verifies, so
        # this is the mismatch that actually bricks an arm (written one window,
        # checked against another), and the servo-id check above cannot see it
        # (two specs with the same ids would still agree on them).
        raise SystemExit(
            '[FEHLER] Interner Fehler: Arm-Spezifikation und geladene '
            'Gelenkgrenzen passen nicht zusammen — apply_arm_spec() wurde '
            'nicht für denselben Arm aufgerufen.')
    if not servo_spec.pinned and not dry_run:
        raise SystemExit(
            '[FEHLER] Die Servo-Spezifikation ist noch nicht festgelegt (P0, '
            'VR3, VR5). Ohne festgelegte Werte wird nichts geschrieben; '
            '--dry-run liest nur.')
    display = spec['display_de']
    # 0. identity gate, for every servo BEFORE any write. The arm MIXES STS3215
    # with STS3250 on the high-load shoulder + elbow BY DESIGN — each joint must
    # carry the spec's model, and (spec pinned) the 12-V variant with the
    # reference Phase byte: a 7.4-V servo or a mirrored Phase would pass every
    # later step and misbehave on the arm.
    models: dict[int, int] = {}
    for i, sid in enumerate(SERVO_IDS):
        joint = servo_spec.joints[i]
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
                             f'{display}.')
        if model != joint.model:
            raise SystemExit(
                f'[FEHLER] Servo {sid}: {fb.STS_MODEL_NAMES[model]} statt '
                f'{joint.model_name} an dieser Stelle ({joint.label_de}).')
        voltage = bus.read(sid, fb.REG_MAX_VOLTAGE, 1)[1][0]
        phase = bus.read(sid, fb.REG_PHASE, 1)[1][0]
        if servo_spec.pinned:
            want = servo_spec.factory[model][fb.REG_MAX_VOLTAGE]
            if voltage <= 90:
                raise SystemExit(
                    f'[FEHLER] Servo {sid}: Max-Spannung {voltage} — '
                    '7,4-V-Servo? Für den V1 nur die 12-V-Variante.')
            if voltage != want:
                raise SystemExit(
                    f'[FEHLER] Servo {sid}: Max-Spannung {voltage} statt {want} '
                    '— andere Variante? Servo nicht verwenden.')
            ref = joint.image[fb.REG_PHASE]
            if (phase & ~0x10) != (ref & ~0x10):
                raise SystemExit(
                    f'[FEHLER] Servo {sid}: Phase-Byte {phase} weicht von der '
                    f'Referenz {ref} ab — Servo nicht verwenden (Drehrichtung '
                    'könnte gespiegelt sein).')
        else:
            print(f'[INFO] Servo {sid}: Max-Spannung {voltage}, Phase-Byte '
                  f'{phase} (Spezifikation noch nicht festgelegt).')
        models[sid] = model
    summary = ', '.join(f'{sid}:{fb.STS_MODEL_NAMES[models[sid]]}'
                        for sid in SERVO_IDS)
    print(f'[OK] Alle {len(SERVO_IDS)} Servos gefunden ({summary}).')
    if dry_run:
        print('[DRY-RUN] Keine EEPROM-Schreibzugriffe.')

    entries = []
    for i, sid in enumerate(SERVO_IDS):
        joint = servo_spec.joints[i]
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

        if not dry_run:
            # 2. torque off (verified — audit L5) + EXPLICIT unlock
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

            # Angular_Resolution (addr 30) is the OTHER register the vendor
            # documents as rescaling the tick↔angle map („对传感器最小分辨角度
            # （度/步）的放大系数"). It sits here, next to the Phase bit-4 clear
            # and BEFORE the offset, for exactly the audit-M1 reason: the jig
            # read that bakes Homing_Offset must run under the same map the
            # runtime uses. read-compare-write, so a factory-default servo costs
            # no EEPROM cycle; the driver's boot probe refuses any other value.
            _write_verify_u8(bus, sid, fb.REG_ANGULAR_RESOLUTION,
                             fb.ANGULAR_RESOLUTION_SINGLE_TURN,
                             'Winkelauflösung')

            # 4. the rest of the golden image, in layout order: refuse-class
            # registers must read back, warn-class ones only warn. Position
            # mode is among them (audit H2: a wheel-mode servo passes every
            # other check and turns boot-home into a continuous-rotation
            # runaway that EEPROM position limits cannot stop).
            for addr, size, name, cls in fss.LAYOUT:
                if cls not in ('refuse', 'warn') or addr in _IMAGE_STEP_SKIP:
                    continue
                value = joint.image[addr]
                if cls == 'refuse':
                    write_verify = (_write_verify_u8 if size == 1
                                    else _write_verify_u16)
                    write_verify(bus, sid, addr, value,
                                 f'{name} (Register {addr})')
                else:
                    _write_warn(bus, sid, addr, size, value, name)
            reg25 = bus.read(sid, fb.REG_25, 1)[1][0]
            if reg25 != 0:
                print(f'[WARNUNG] Servo {sid}: Register 25 ist {reg25} statt 0.')

        # 5. homing offset: the jigged position must READ the joint's T0.
        present = fb.decode_sign_magnitude(
            bus.read_u16(sid, fb.REG_PRESENT_POSITION), 15)
        raw_offset = bus.read_u16(sid, fb.REG_HOMING_OFFSET)
        try:
            current_off = fb.decode_homing_offset(raw_offset)
        except fb.ForeignOffsetEncoding:
            # Written by a foreign tool in the vendor's wider encoding: its
            # value cannot be read back safely, so it is cleared first and the
            # jigged raw tick re-read without it.
            if dry_run:
                current_off = None
                print(f'[INFO] Servo {sid}: Homing-Offset in fremder Kodierung '
                      f'(Rohwert {raw_offset}) — wird beim Provisionieren '
                      'zuerst auf 0 gesetzt.')
            else:
                _write_verify_u16(bus, sid, fb.REG_HOMING_OFFSET, 0,
                                  'Homing-Offset')
                current_off = 0
                present = fb.decode_sign_magnitude(
                    bus.read_u16(sid, fb.REG_PRESENT_POSITION), 15)
        if joint.t0 is None or current_off is None:
            new_off = None          # dry run: unpinned spec or foreign offset
        else:
            # Present = (Raw − Offset) mod 4096 on a single-turn servo, so the
            # raw tick and the offset that makes it read T0 are both
            # wrap-aware (a jig pose whose raw tick sits near 0/4095 under a
            # legacy offset, bench-observed on J5, used to false-overflow).
            try:
                new_off = fb.homing_offset_for(
                    fb.raw_tick_from(present, current_off), joint.t0)
            except fb.OffsetUnrepresentable:
                raise SystemExit(
                    f'[FEHLER] Servo {sid}: der Rohwert liegt genau gegenüber '
                    'dem Nullpunkt — Gelenk minimal verdrehen und erneut '
                    'provisionieren.') from None
        if not dry_run:
            if new_off != current_off:
                encoded = fb.encode_homing_offset(new_off)
                bus.write(sid, fb.REG_HOMING_OFFSET, fb.le16(encoded))
                back = bus.read_u16(sid, fb.REG_HOMING_OFFSET)
                if back != encoded:
                    raise SystemExit(
                        f'[FEHLER] Servo {sid}: Homing-Offset stimmt nach dem '
                        f'Schreiben nicht überein (Rohwert {back} != '
                        f'{encoded}).')
            # Postcondition: on the STILL-JIGGED arm, Present_Position must
            # now READ T0. The offset REGISTER read-back above can verify
            # correct while a sign/decode slip still leaves the produced
            # position DOUBLED — this re-read is the only check that closes
            # that silent 2×-offset-skew hole.
            present_after = fb.decode_sign_magnitude(
                bus.read_u16(sid, fb.REG_PRESENT_POSITION), 15)
            if not _jig_reads(present_after, joint.t0):
                raise SystemExit(
                    f'[FEHLER] Servo {sid}: Jig-Position liest nach dem '
                    f'Offset-Schreiben {present_after} statt {joint.t0} '
                    f'(Abweichung {present_after - joint.t0}'
                    ' Ticks) — Homing-Offset/Vorzeichen prüfen und erneut '
                    'provisionieren.')

        # 6. the spec's position limits (AFTER the offset — corrected frame).
        lo, hi = joint.window
        if not dry_run:
            _write_verify_u16(bus, sid, fb.REG_MIN_POSITION_LIMIT, lo,
                              'Min_Position')
            _write_verify_u16(bus, sid, fb.REG_MAX_POSITION_LIMIT, hi,
                              'Max_Position')
            # 7. re-lock
            _write_verify_u8(bus, sid, fb.REG_LOCK, 1, 'Lock=1')

        # 8. the whole EEPROM block, judged exactly like the boot probe does.
        block = bus.read(sid, fb.EEPROM_BLOCK_ADDR, fb.EEPROM_BLOCK_LEN)[1]
        if not dry_run:
            refusals, _warnings = fss.compare_block(block, joint)
            if refusals:
                listed = '; '.join(
                    f'{d.name} (Register {d.addr}) ist {d.found}, erwartet '
                    f'{d.expected}' for d in refusals)
                raise SystemExit(
                    f'[FEHLER] Servo {sid}: das EEPROM weicht nach dem '
                    f'Schreiben vom Soll-Abbild ab: {listed}.')
        entries.append({
            'id': sid,
            'homing_offset': new_off,
            'range_min': lo,
            'range_max': hi,
            'max_torque': joint.max_torque,
            'protection_current': joint.image[fb.REG_PROTECTION_CURRENT],
            'model_number': models[sid],
            'model_name': fb.STS_MODEL_NAMES[models[sid]],
            # ONE shared formatter with the driver's boot fingerprint, so a
            # stored record and a bench log are string-comparable.
            'firmware': fb.format_firmware(block[0], block[1]),
            'angular_resolution': block[fb.REG_ANGULAR_RESOLUTION],
            'tick_direction': joint.tick_direction,
            'eeprom_hex': bytes(block).hex(),
        })
        print(f'[OK] Servo {sid}: offset={new_off} limits=[{lo},{hi}] '
              f'torque={joint.max_torque}')

    # 9. FINAL jig re-verify (audit M1): after EVERY write of the run —
    # including the Phase-bit-4 clears — the still-jigged arm must read its T0
    # on EVERY servo of this arm. A per-servo gate cannot catch a semantic
    # shift caused by a later write; this one can. The arm must stay jigged
    # until the tool prints its closing [OK].
    if not dry_run:
        for i, sid in enumerate(SERVO_IDS):
            t0 = designed_zero_tick(i, servo_spec)
            present = fb.decode_sign_magnitude(
                bus.read_u16(sid, fb.REG_PRESENT_POSITION), 15)
            if not _jig_reads(present, t0):
                raise SystemExit(
                    f'[FEHLER] Jig-Endkontrolle: Servo {sid} liest {present} '
                    f'statt {t0} (Abweichung {present - t0} '
                    'Ticks) — den Arm im Jig belassen und die '
                    'Provisionierung erneut ausführen.')
        print(f'[OK] Jig-Endkontrolle: alle {len(SERVO_IDS)} Servos lesen '
              'die Soll-Position.')

    record = {
        'arm_serial': serial,
        # From the SPEC, never a literal: this is the only field in the whole
        # artifact — local json and the shared `edu6_arm_records` table alike —
        # that says which arm the record describes.
        'profile_id': spec['profile_id'],
        'signs': None if servo_spec.signs is None else list(servo_spec.signs),
        'tick_direction': {str(model): direction for model, direction
                           in servo_spec.tick_direction.items()},
        'spec_sha256': servo_spec.digest,
        'servos': entries,
        'provisioned_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    if servo_spec.signs is not None:
        # No checksum for an unpinned dry run: there is no calibration to sign.
        record['checksum'] = record_checksum(entries, serial, servo_spec.signs)
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
            # The TABLE is shared by every Feetech arm (migration 036 named
            # it before the V1 existed); the record's profile_id is what
            # separates them. Say "Arm-Datenbank" so a V1 operator is not told
            # their arm went into some other arm's artifact.
            print('[OK] Supabase-Arm-Datenbank (edu6_arm_records) '
                  'aktualisiert.')
        except Exception as e:  # noqa: BLE001 — the local file is the commit
            print(f'[WARNUNG] Supabase-Upload fehlgeschlagen (lokale Datei '
                  f'bleibt gültig): {e}')
    else:
        print('[INFO] SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY nicht gesetzt — '
              'nur lokale Datei geschrieben.')
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--arm', default='v1', choices=sorted(_ARM_SPECS),
                    help='which Feetech arm is on the bench (default: v1)')
    ap.add_argument('--port', required=True)
    ap.add_argument('--serial', required=True,
                    help='vendor sticker serial, e.g. V1-0001')
    ap.add_argument('--out', default=None,
                    help='record directory (default: <arm>_records beside this '
                         'script)')
    ap.add_argument('--dry-run', action='store_true',
                    help='read + report only; no EEPROM writes')
    args = ap.parse_args()

    spec = apply_arm_spec(args.arm)
    out_dir = args.out or str(Path(__file__).parent / spec['records_dir'])

    # EEPROM writes (Homing_Offset, limits, torque caps…) trigger real EEPROM
    # programming (~tens of ms) once Lock=0, far longer than the driver's 20 ms
    # RAM-reply budget — bench-observed 2026-07-24. A generous reply timeout is
    # a MAX wait, not a fixed delay: fast reads/RAM writes still return the
    # instant the reply lands, so this only affects the slow EEPROM commits.
    bus = fb.FeetechBus(args.port, timeout_s=0.2)
    try:
        record = provision(bus, args.serial.strip(), spec,
                           dry_run=args.dry_run)
    finally:
        bus.close()
    if args.dry_run:
        print(json.dumps(record, indent=2, sort_keys=True))
        return 0
    persist(record, Path(out_dir))
    print('[OK] Provisionierung abgeschlossen — Arm ist dauerhaft kalibriert.')
    return 0


if __name__ == '__main__':
    sys.exit(main())

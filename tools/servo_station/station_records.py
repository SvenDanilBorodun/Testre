"""The Servo-Station's files: records, receipts, CSV logs, references, P0 report.

The local record file is the COMMIT POINT of a calibration (spec §5A L): a
servo without ``records/<serial>.json`` is not calibrated, whatever its EEPROM
says; the cloud copy (migration 052) is only a copy. So every JSON file is
written atomically (temp file in the same directory → flush → fsync →
``os.replace``), a re-calibration moves the previous record to ``history/``
first and nothing is ever deleted, and the CSV logs are append-only (UTF-8 with
BOM and CRLF, so a double click opens them in a German Excel).

Data dir layout::

    station.json                      public cloud config, masters, last port
    records/<serial>.json             the calibration record (commit point)
    records/<serial>.uploaded.json    {checksum, uploaded_at, response}
    records/<serial>.rejected.json    {checksum, code, at} — not pending any more
    records/history/<serial>-<YYYYmmddTHHMMSSZ>.json
    incoming/<serial>.json            the Wareneingang result
    reference/<UTC>-<model>-id<id>.json, p0_report.json, p0_pin_snippet.txt
    calibrations.csv, incoming.csv, gauge_checks.csv, station.log

No password or token is ever written here: ``station.json`` holds only the
public Supabase URL and the public anon key. Pure file I/O — imports neither
the bus nor the servo spec.
"""

from __future__ import annotations

import codecs
import csv
import hashlib
import io
import json
import os
import pathlib
import re
import tempfile

RECORD_SCHEMA = 'edubotics.servo_calibration.v1'
INCOMING_SCHEMA = 'edubotics.servo_incoming.v1'
REFERENCE_SCHEMA = 'edubotics.servo_reference.v1'
P0_REPORT_SCHEMA = 'edubotics.servo_p0_report.v1'

# The FACTORY keys the P0 report compares and proposes (spec §5A, review 2 M2):
# the per-unit offset (31–32) and every other address are deliberately not
# compared. 28 is a 2-byte register.
P0_FACTORY_KEYS = (13, 14, 15, 18, 20, 21, 22, 23, 24, 26, 27, 28, 34, 35, 38)
_TWO_BYTE = frozenset({9, 11, 28})
P0_MIN_DUMPS = 3

CALIBRATIONS_HEADER = ('calibrated_at', 'servo_serial', 'joint', 'servo_id', 'model_number', 'homing_offset',
                       't0', 'present_after_power_cycle', 'gauge_id', 'operator', 'warnings', 'checksum')
INCOMING_HEADER = ('tested_at', 'servo_serial', 'model_number', 'firmware', 'passed', 'time_ms',
                   'peak_current_ma', 'reason_code')
GAUGE_CHECKS_HEADER = ('at', 'local_date', 'cap_label', 'cap_type', 'master_serial', 'present', 'want', 'passed')

# A file-name component made from a scanned label: the scanner input is
# pattern-checked upstream, this is the second wall against a path.
_SAFE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9-]{0,31}$')
_RECORD_FILE = re.compile(r'^([A-Za-z0-9][A-Za-z0-9-]{0,31})\.json$')


def _safe(name: str) -> str:
    if not isinstance(name, str) or not _SAFE_NAME.match(name):
        raise ValueError(f'unsafe file name component: {name!r}')
    return name


def default_data_dir(data_dir=None, environ=None, home=None) -> pathlib.Path:
    """``--data-dir``, else ``%LOCALAPPDATA%\\EduBotics\\Servo-Station``, else
    ``~/.edubotics/servo-station``."""
    if data_dir:
        return pathlib.Path(data_dir)
    environ = os.environ if environ is None else environ
    local = environ.get('LOCALAPPDATA', '').strip()
    if local:
        return pathlib.Path(local) / 'EduBotics' / 'Servo-Station'
    return pathlib.Path(home or pathlib.Path.home()) / '.edubotics' / 'servo-station'


def atomic_write_json(path: pathlib.Path, obj) -> pathlib.Path:
    """Write ``obj`` as JSON so a reader sees the old file or the new one, never
    half of either."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + '\n'
    fd, tmp = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def append_csv(path: pathlib.Path, header, row) -> None:
    """Append one row; a new file starts with a UTF-8 BOM and the header."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator='\r\n')
    if new:
        writer.writerow(header)
    writer.writerow(row)
    data = buf.getvalue().encode('utf-8')
    if new:
        data = codecs.BOM_UTF8 + data
    with open(path, 'ab') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def read_csv(path: pathlib.Path) -> list[dict]:
    path = pathlib.Path(path)
    if not path.exists():
        return []
    with open(path, encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def _read_json(path: pathlib.Path):
    path = pathlib.Path(path)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8'))


def record_checksum(record: dict) -> str:
    """sha256 of the canonical JSON of every key but ``checksum``."""
    body = {k: v for k, v in record.items() if k != 'checksum'}
    text = json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def finalize_record(fields: dict) -> dict:
    record = dict(fields)
    record['schema'] = RECORD_SCHEMA
    record['checksum'] = record_checksum(record)
    return record


def _compact_stamp(iso: str) -> str:
    """``2026-10-27T09:30:00Z`` → ``20261027T093000Z``."""
    digits = re.sub(r'[^0-9T]', '', (iso or '').split('.')[0])
    return (digits + 'Z') if re.match(r'^\d{8}T\d{6}$', digits) else ''


class Store:
    """One data directory."""

    def __init__(self, root):
        self.root = pathlib.Path(root)
        self.records_dir = self.root / 'records'
        self.history_dir = self.records_dir / 'history'
        self.incoming_dir = self.root / 'incoming'
        self.reference_dir = self.root / 'reference'
        self.config_path = self.root / 'station.json'
        self.calibrations_csv = self.root / 'calibrations.csv'
        self.incoming_csv = self.root / 'incoming.csv'
        self.gauge_checks_csv = self.root / 'gauge_checks.csv'
        self.log_path = self.root / 'station.log'
        self.p0_report_path = self.reference_dir / 'p0_report.json'
        self.p0_snippet_path = self.reference_dir / 'p0_pin_snippet.txt'

    # ── station.json ─────────────────────────────────────────────────────────
    def load_config(self) -> dict:
        config = {'supabase_url': '', 'supabase_anon_key': '', 'masters': {}, 'last_port': ''}
        stored = _read_json(self.config_path) or {}
        for key in config:
            if key in stored:
                config[key] = stored[key]
        return config

    def save_config(self, config: dict) -> None:
        allowed = ('supabase_url', 'supabase_anon_key', 'masters', 'last_port')
        atomic_write_json(self.config_path, {k: config.get(k) for k in allowed})

    def masters(self) -> dict:
        return dict(self.load_config().get('masters') or {})

    def set_master(self, cap_type: str, serial: str) -> None:
        config = self.load_config()
        masters = dict(config.get('masters') or {})
        masters[_safe(cap_type)] = _safe(serial)
        config['masters'] = masters
        self.save_config(config)

    # ── calibration records ──────────────────────────────────────────────────
    def record_path(self, serial: str) -> pathlib.Path:
        return self.records_dir / f'{_safe(serial)}.json'

    def load_record(self, serial: str):
        return _read_json(self.record_path(serial))

    def save_record(self, record: dict) -> pathlib.Path:
        """Move a previous record to ``history/`` (nothing is deleted), write
        the new one atomically, then log it in ``calibrations.csv``."""
        serial = _safe(record['servo_serial'])
        path = self.record_path(serial)
        if path.exists():
            previous = _read_json(path) or {}
            stamp = _compact_stamp(previous.get('calibrated_at', '')) or 'unknown'
            self.history_dir.mkdir(parents=True, exist_ok=True)
            target = self.history_dir / f'{serial}-{stamp}.json'
            n = 2
            while target.exists():
                target = self.history_dir / f'{serial}-{stamp}-{n}.json'
                n += 1
            os.replace(path, target)
        atomic_write_json(path, record)
        cal = record.get('calibration') or {}
        append_csv(self.calibrations_csv, CALIBRATIONS_HEADER, (
            record.get('calibrated_at'), serial, record.get('joint'), record.get('servo_id'),
            record.get('model_number'), cal.get('homing_offset'), record.get('t0'),
            cal.get('present_after_power_cycle'), cal.get('gauge_id'), record.get('operator'),
            len(record.get('warnings') or ()), record.get('checksum')))
        return path

    def _record_serials(self):
        if not self.records_dir.exists():
            return []
        out = []
        for path in sorted(self.records_dir.glob('*.json')):
            match = _RECORD_FILE.match(path.name)
            if match:
                out.append(match.group(1))
        return out

    # ── upload receipts ──────────────────────────────────────────────────────
    def uploaded_receipt_path(self, serial):
        return self.records_dir / f'{_safe(serial)}.uploaded.json'

    def rejected_receipt_path(self, serial):
        return self.records_dir / f'{_safe(serial)}.rejected.json'

    def write_uploaded_receipt(self, serial, checksum, response, at):
        atomic_write_json(self.uploaded_receipt_path(serial),
                          {'checksum': checksum, 'uploaded_at': at, 'response': response})

    def write_rejected_receipt(self, serial, checksum, code, at):
        atomic_write_json(self.rejected_receipt_path(serial),
                          {'checksum': checksum, 'code': code, 'at': at})

    def _receipt_matches(self, path, checksum):
        receipt = _read_json(path)
        return bool(receipt) and receipt.get('checksum') == checksum

    def pending_records(self) -> list[dict]:
        """Every record whose CURRENT checksum has neither an uploaded nor a
        rejected receipt. A rejected one stays out until it is re-calibrated."""
        out = []
        for serial in self._record_serials():
            record = self.load_record(serial)
            if not record:
                continue
            checksum = record.get('checksum')
            if (self._receipt_matches(self.uploaded_receipt_path(serial), checksum)
                    or self._receipt_matches(self.rejected_receipt_path(serial), checksum)):
                continue
            out.append(record)
        return out

    def rejected_records(self) -> list[tuple[str, str]]:
        out = []
        for serial in self._record_serials():
            record = self.load_record(serial) or {}
            receipt = _read_json(self.rejected_receipt_path(serial))
            if receipt and receipt.get('checksum') == record.get('checksum'):
                out.append((serial, str(receipt.get('code'))))
        return out

    # ── Wareneingang ─────────────────────────────────────────────────────────
    def incoming_path(self, serial):
        return self.incoming_dir / f'{_safe(serial)}.json'

    def load_incoming(self, serial):
        return _read_json(self.incoming_path(serial))

    def save_incoming(self, record: dict) -> pathlib.Path:
        serial = _safe(record['servo_serial'])
        path = atomic_write_json(self.incoming_path(serial), record)
        append_csv(self.incoming_csv, INCOMING_HEADER, (
            record.get('tested_at'), serial, record.get('model_number'), record.get('firmware'),
            1 if record.get('passed') else 0,
            ' '.join(str(t) for t in (record.get('time_ms') or ())),
            record.get('peak_current_ma'), record.get('reason_code') or ''))
        return path

    # ── gauge checks („Lehre prüfen") ────────────────────────────────────────
    def append_gauge_check(self, *, at, local_date, cap_label, cap_type, master_serial, present, want, passed):
        append_csv(self.gauge_checks_csv, GAUGE_CHECKS_HEADER,
                   (at, local_date, cap_label, cap_type, master_serial, present, want, 1 if passed else 0))

    def gauge_state(self, cap_label: str, cap_type: str, local_date: str):
        """``('passed', at)`` when the LAST check of ``cap_label`` today passed,
        ``('no_master', None)`` while no master is set for ``cap_type`` (the
        bootstrap), else ``('not_today', None)``."""
        if cap_type not in self.masters():
            return 'no_master', None
        last = None
        for row in read_csv(self.gauge_checks_csv):
            if row.get('cap_label') == cap_label and row.get('local_date') == local_date:
                last = row
        if last is not None and last.get('passed') == '1':
            return 'passed', last.get('at')
        return 'not_today', None

    # ── P0 reference dumps ───────────────────────────────────────────────────
    def save_reference(self, dump: dict, utc_stamp: str) -> pathlib.Path:
        model = 'unknown' if dump.get('model') is None else int(dump['model'])
        name = f"{_compact_stamp(utc_stamp) or 'unknown'}-{model}-id{int(dump['id'])}.json"
        path = self.reference_dir / name
        n = 2
        while path.exists():
            path = self.reference_dir / name.replace('.json', f'-{n}.json')
            n += 1
        return atomic_write_json(path, dump)

    def _own_reference(self, path) -> pathlib.Path:
        path = pathlib.Path(path)
        if path.parent.resolve() != self.reference_dir.resolve():
            raise ValueError('not a reference file of this station')
        return path

    def load_reference(self, path) -> dict:
        return _read_json(self._own_reference(path)) or {}

    def update_reference(self, path, **fields) -> None:
        path = self._own_reference(path)
        dump = _read_json(path)
        dump.update(fields)
        atomic_write_json(path, dump)

    def list_references(self) -> list[tuple[pathlib.Path, dict]]:
        if not self.reference_dir.exists():
            return []
        out = []
        for path in sorted(self.reference_dir.glob('*.json')):
            if path.name == self.p0_report_path.name:
                continue
            try:
                out.append((path, _read_json(path)))
            except (OSError, ValueError):
                out.append((path, {}))
        return out

    def write_p0_report(self, report: dict, snippet: str):
        atomic_write_json(self.p0_report_path, report)
        self.p0_snippet_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.p0_pin_snippet.', suffix='.tmp', dir=self.reference_dir)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(snippet)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.p0_snippet_path)
        return self.p0_report_path, self.p0_snippet_path


# ── the P0 report (pure) ─────────────────────────────────────────────────────
def _int_regs(dump: dict) -> dict:
    regs = {}
    for key, value in (dump.get('regs') or {}).items():
        try:
            regs[int(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return regs


def _reg(regs: dict, addr: int):
    if addr in _TWO_BYTE:
        lo, hi = regs.get(addr), regs.get(addr + 1)
        return None if lo is None or hi is None else lo | (hi << 8)
    return regs.get(addr)


def is_fresh(regs: dict) -> bool:
    """A vendor-fresh servo: ID 1 and the limits 0/4095 (review 2, M2). A servo
    the station or the provision tool already wrote is never a reference."""
    return regs.get(5) == 1 and _reg(regs, 9) == 0 and _reg(regs, 11) == 4095


def fresh_fields(regs: dict) -> dict:
    return {'id': regs.get(5), 'lo': _reg(regs, 9), 'hi': _reg(regs, 11)}


def _dict_text(values: dict) -> str:
    return '{' + ', '.join(f'{a}: {v}' for a, v in sorted(values.items())) + '}'


def p0_report(dumps, model_names: dict, generated_at: str):
    """``(findings, report, snippet)`` over ``[(path, dump)]``. ``findings`` is a
    list of ``(code, format_fields)`` — any finding means nothing is proposed
    (``report`` and ``snippet`` None). ``model_names`` maps the accepted model
    numbers to their names, in report order."""
    findings = []
    per_model = {model: [] for model in model_names}
    for path, dump in dumps:
        regs = _int_regs(dump or {})
        if not is_fresh(regs):
            findings.append(('P0_NOT_FRESH', dict(fresh_fields(regs), path=str(path))))
            continue
        if dump.get('model') in per_model:
            per_model[dump['model']].append((dump, regs))
    summary = {}
    for model, items in per_model.items():
        name = model_names[model]
        if len(items) < P0_MIN_DUMPS:
            findings.append(('P0_TOO_FEW', {'model': name, 'n': len(items)}))
            continue
        if any(not dump.get('eeprom_block_read') for dump, _regs in items):
            findings.append(('P0_FALLBACK', {'model': name}))
            continue
        directions = {dump.get('tick_direction') for dump, _regs in items} - {None}
        if len(directions) > 1:
            findings.append(('P0_DIRECTION_DISAGREE', {'model': name}))
        factory, disagree = {}, []
        for addr in P0_FACTORY_KEYS:
            values = {_reg(regs, addr) for _dump, regs in items}
            if len(values) == 1 and None not in values:
                factory[addr] = values.pop()
            else:
                disagree.append(addr)
        if disagree:
            findings.append(('P0_DISAGREE', {'model': name, 'list': ', '.join(str(a) for a in disagree)}))
        times = [int(t) for dump, _regs in items for t in (dump.get('motion_ms') or ())]
        summary[model] = {
            'n': len(items),
            'tick_direction': directions.pop() if len(directions) == 1 else None,
            'factory': factory,
            'motion_ms': times,
            'motion_test_window_ms': [round(0.6 * min(times)), round(1.5 * max(times))] if times else None,
        }
    if findings:
        return findings, None, None
    report = {
        'schema': P0_REPORT_SCHEMA, 'generated_at': generated_at,
        'models': {str(m): dict(s, factory={str(a): v for a, v in s['factory'].items()})
                   for m, s in summary.items()},
    }
    lines = [
        f'# P0 report {generated_at} (Servo-Station). Paste into the PROVISIONAL block of',
        '# robotis_ai_setup/docker/open_manipulator/feetech_servo_spec.py (build spec section 12, step 3).',
        '# Directions measured in the cradle against its arrow: +1 = ticks rise counter-clockwise seen from',
        '# the horn side. None = not measured on any reference.',
        'TICK_DIRECTION = {' + ', '.join(f'{m}: {summary[m]["tick_direction"]}' for m in summary) + '}',
        'FACTORY = {   # model -> {addr: value}: the agreed values of exactly the FACTORY keys',
    ]
    lines += [f'    {m}: {_dict_text(summary[m]["factory"])},' for m in summary]
    lines.append('}')
    windows = ', '.join(
        f'{m}: {tuple(summary[m]["motion_test_window_ms"]) if summary[m]["motion_test_window_ms"] else None}'
        for m in summary)
    lines += [
        'MOTION_TEST_WINDOW_MS = {' + windows + '}   # (0.6 x min, 1.5 x max) of the measured 45-degree moves',
        '# MAX_TORQUE and the protection current (address 28) are settled on the pilot arm',
        '# (VR5 + the overcurrent check, build spec section 12) before the pin commit.',
        '',
    ]
    return [], report, '\n'.join(lines)

"""The Servo-Station's files (``tools/servo_station/station_records.py``):
the record is the commit point, so writes are atomic, nothing is deleted, the
CSV logs only grow, and the receipts decide what is still to upload. Deps-free.
"""

import codecs
import json
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

import servo_station_fakes  # noqa: F401  (puts tools/servo_station on sys.path)

import station_records as records  # noqa: E402


def _record(serial='V1S-000123', at='2026-10-27T09:30:00Z', offset=810):
    return records.finalize_record({'servo_serial': serial, 'joint': 'joint2', 'servo_id': 2,
                                    'calibration': {'homing_offset': offset, 'gauge_id': 'KAPPE-J15-A'},
                                    'warnings': [], 'operator': 'Anna', 'calibrated_at': at})


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='servo-station-records-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = records.Store(self.tmp)


class TestChecksum(unittest.TestCase):
    def test_stable_and_order_independent(self):
        a = {'servo_serial': 'V1S-000123', 'calibration': {'a': 1, 'b': [1, 2]}, 'operator': 'Jörg'}
        b = {'operator': 'Jörg', 'calibration': {'b': [1, 2], 'a': 1}, 'servo_serial': 'V1S-000123'}
        self.assertEqual(records.record_checksum(a), records.record_checksum(b))
        self.assertRegex(records.record_checksum(a), r'^[0-9a-f]{64}$')

    def test_sensitive_to_every_value_but_not_to_its_own_field(self):
        base = _record()
        changed = json.loads(json.dumps(base))
        changed['calibration']['homing_offset'] = 811
        self.assertNotEqual(records.record_checksum(base), records.record_checksum(changed))
        self.assertEqual(records.record_checksum(dict(base, checksum='x')), base['checksum'])

    def test_finalize_sets_the_schema_and_a_matching_checksum(self):
        rec = _record()
        self.assertEqual(rec['schema'], 'edubotics.servo_calibration.v1')
        self.assertEqual(rec['checksum'], records.record_checksum(rec))


class TestAtomicJson(_TmpCase):
    def test_no_temp_file_is_left(self):
        path = self.tmp / 'x' / 'a.json'
        records.atomic_write_json(path, {'ä': 1})
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'ä': 1})
        self.assertEqual([p.name for p in path.parent.iterdir()], ['a.json'])

    def test_a_failed_write_keeps_the_old_file_and_leaves_no_temp(self):
        path = self.tmp / 'a.json'
        records.atomic_write_json(path, {'old': True})
        with self.assertRaises(TypeError):
            records.atomic_write_json(path, {'bad': object()})
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'old': True})
        with mock.patch.object(records.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                records.atomic_write_json(path, {'new': True})
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'old': True})
        self.assertEqual([p.name for p in self.tmp.iterdir()], ['a.json'])

    def test_it_fsyncs_before_the_replace(self):
        calls = []
        real_fsync, real_replace = os.fsync, os.replace
        with mock.patch.object(records.os, 'fsync', side_effect=lambda fd: (calls.append('fsync'), real_fsync(fd))), \
                mock.patch.object(records.os, 'replace',
                                  side_effect=lambda a, b: (calls.append('replace'), real_replace(a, b))):
            records.atomic_write_json(self.tmp / 'a.json', {})
        self.assertEqual(calls, ['fsync', 'replace'])


class TestRecordsAndHistory(_TmpCase):
    def test_a_second_calibration_moves_the_first_to_history(self):
        first = _record()
        self.store.save_record(first)
        second = _record(at='2026-10-28T08:00:00Z', offset=812)
        path = self.store.save_record(second)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), second)
        history = sorted(self.store.history_dir.iterdir())
        self.assertEqual([p.name for p in history], ['V1S-000123-20261027T093000Z.json'])
        self.assertEqual(json.loads(history[0].read_text(encoding='utf-8')), first)
        self.store.save_record(_record(at='2026-10-28T08:00:00Z', offset=813))
        self.assertEqual(len(list(self.store.history_dir.iterdir())), 2)

    def test_a_scanned_label_can_never_name_a_path(self):
        for bad in ('../x', 'a/b', '..', '', 'V1S-000123.json', 'x' * 40):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.store.record_path(bad)

    def test_station_json_keeps_only_public_keys(self):
        self.store.save_config({'supabase_url': 'https://a.supabase.co', 'supabase_anon_key': 'anon',
                                'masters': {'J15': 'V1S-000001'}, 'last_port': 'COM3',
                                'password': 'secret', 'access_token': 'tok'})
        stored = json.loads(self.store.config_path.read_text(encoding='utf-8'))
        self.assertEqual(sorted(stored), ['last_port', 'masters', 'supabase_anon_key', 'supabase_url'])
        self.store.set_master('GR', 'V1S-000006')
        self.assertEqual(self.store.masters(), {'J15': 'V1S-000001', 'GR': 'V1S-000006'})


class TestCsv(_TmpCase):
    def test_one_header_a_bom_and_crlf(self):
        path = self.tmp / 'log.csv'
        records.append_csv(path, ('a', 'b'), (1, 'ä,ö'))
        records.append_csv(path, ('a', 'b'), (2, 'x'))
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(codecs.BOM_UTF8))
        self.assertEqual(raw.count(codecs.BOM_UTF8), 1)
        text = raw[len(codecs.BOM_UTF8):].decode('utf-8')
        self.assertEqual(text, 'a,b\r\n1,"ä,ö"\r\n2,x\r\n')
        self.assertEqual(records.read_csv(path), [{'a': '1', 'b': 'ä,ö'}, {'a': '2', 'b': 'x'}])

    def test_the_calibration_log_grows_by_one_row_per_record(self):
        self.store.save_record(_record())
        self.store.save_record(_record(at='2026-10-28T08:00:00Z'))
        rows = records.read_csv(self.store.calibrations_csv)
        self.assertEqual([r['calibrated_at'] for r in rows], ['2026-10-27T09:30:00Z', '2026-10-28T08:00:00Z'])
        self.assertEqual(list(rows[0]), list(records.CALIBRATIONS_HEADER))


class TestReceipts(_TmpCase):
    def test_uploaded_and_rejected_receipts_each_end_pending(self):
        a, b, c = _record('V1S-000001'), _record('V1S-000002'), _record('V1S-000003')
        for rec in (a, b, c):
            self.store.save_record(rec)
        self.assertEqual(len(self.store.pending_records()), 3)
        self.store.write_uploaded_receipt('V1S-000001', a['checksum'], {'stored': True}, 'now')
        self.store.write_rejected_receipt('V1S-000002', b['checksum'], '22023', 'now')
        self.assertEqual([r['servo_serial'] for r in self.store.pending_records()], ['V1S-000003'])
        self.assertEqual(self.store.rejected_records(), [('V1S-000002', '22023')])

    def test_a_re_calibration_is_pending_again(self):
        old = _record()
        self.store.save_record(old)
        self.store.write_rejected_receipt('V1S-000123', old['checksum'], '22023', 'now')
        self.assertEqual(self.store.pending_records(), [])
        self.store.save_record(_record(at='2026-10-28T08:00:00Z'))
        self.assertEqual(len(self.store.pending_records()), 1)
        self.assertEqual(self.store.rejected_records(), [])

    def test_receipts_and_history_are_never_records(self):
        rec = _record()
        self.store.save_record(rec)
        self.store.save_record(_record(at='2026-10-28T08:00:00Z'))
        self.store.write_uploaded_receipt('V1S-000123', 'other', {}, 'now')
        self.assertEqual([r['servo_serial'] for r in self.store.pending_records()], ['V1S-000123'])


class TestDataDir(unittest.TestCase):
    def test_the_order_of_the_three(self):
        self.assertEqual(records.default_data_dir('X:/d', {'LOCALAPPDATA': 'C:/L'}), pathlib.Path('X:/d'))
        self.assertEqual(records.default_data_dir(None, {'LOCALAPPDATA': 'C:/L'}),
                         pathlib.Path('C:/L') / 'EduBotics' / 'Servo-Station')
        self.assertEqual(records.default_data_dir(None, {}, home='/home/a'),
                         pathlib.Path('/home/a') / '.edubotics' / 'servo-station')


if __name__ == '__main__':
    unittest.main()

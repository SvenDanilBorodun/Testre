"""Migration 052 (servo_calibration_records + the admin-gated upload RPC) and its
rollback, read as text.

The behaviour is proven by hand on a local stack by
``supabase/tests/052_servo_calibration_records_assertions.sql`` (11 PASS); what is
pinned here, deps-free and in CI, is the shape a careless edit would break (Rule
§4): the table's CHECKs, RLS on with no policy and every request-role privilege
revoked, the RPC SECURITY DEFINER with a pinned search_path whose FIRST statement
is the admin gate (42501), EXECUTE for authenticated + service_role only, the
closing self-check, the serial pattern equal to the servo spec's and the
station's, the rollback refusing while rows exist and dropping in dependency
order — and that the cloud API never learned about the table (owner R4-2: 052 is
not in the boot schema probe).
"""

from __future__ import annotations

import os
import pathlib
import re
import sys
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SUPABASE = _ROOT / 'supabase'
_MIG = _SUPABASE / 'migrations'
M051 = _MIG / '20261025120000_051_trajectory_robot_profile_v1.sql'
M052 = _MIG / '20261026120000_052_servo_calibration_records.sql'
R052 = _SUPABASE / 'rollback' / '20261026120000_052_servo_calibration_records_rollback.sql'
A052 = _SUPABASE / 'tests' / '052_servo_calibration_records_assertions.sql'
_API = _ROOT / 'cloud_training_api'

sys.path.insert(0, str(_ROOT / 'docker' / 'open_manipulator'))
sys.path.insert(0, str(_ROOT.parent / 'tools' / 'servo_station'))

import feetech_servo_spec as fss  # noqa: E402

_FN_SIG = 'public.submit_servo_calibration_record(jsonb)'


def _read(p) -> str:
    return p.read_text(encoding='utf-8')


def _code(sql: str) -> str:
    """The SQL without ``--`` comments (a comment may say anything), every run
    of whitespace folded to one space (column alignment is not shape)."""
    return re.sub(r'\s+', ' ', '\n'.join(line.split('--', 1)[0] for line in sql.splitlines()))


def _function_body(code: str) -> str:
    start = code.index('CREATE OR REPLACE FUNCTION public.submit_servo_calibration_record(')
    return code[start:code.index('$$;', start) + 3]


class FilesAndOrder(unittest.TestCase):
    def test_every_file_exists(self):
        for p in (M052, R052, A052):
            self.assertTrue(p.is_file(), p)

    def test_052_sorts_after_051(self):
        self.assertGreater(M052.name, M051.name)
        later = [p.name for p in _MIG.glob('*.sql') if p.name > M052.name]
        self.assertEqual(later, [])

    def test_the_migration_is_one_transaction(self):
        code = _code(_read(M052))
        self.assertEqual(code.count('BEGIN;'), 1)
        self.assertTrue(code.rstrip().endswith('COMMIT;'))


class Table052(unittest.TestCase):
    def setUp(self):
        self.code = _code(_read(M052))

    def test_the_serial_check_is_the_spec_pattern(self):
        self.assertEqual(fss.SERVO_SERIAL_PATTERN, r'^V1S-[0-9]{6}$')
        self.assertIn("servo_serial text PRIMARY KEY CHECK (servo_serial ~ '%s')"
                      % fss.SERVO_SERIAL_PATTERN, self.code)
        import station_controller as sc
        self.assertEqual(sc.SERIAL_RE.pattern, fss.SERVO_SERIAL_PATTERN)

    def test_the_joint_names_ids_and_models_are_the_spec(self):
        names = ','.join(f"'{n}'" for n in fss.JOINT_NAMES)
        self.assertIn(f'CHECK (joint IN ({names}))', self.code)
        pairs = ','.join(f"('{n}',{i})" for n, i in zip(fss.JOINT_NAMES, fss.SERVO_IDS))
        self.assertIn(f'CHECK ((joint, servo_id) IN ({pairs}))', self.code)
        self.assertIn('CHECK (model_number IN (777, 2825))', self.code)
        sts3250_ids = tuple(i for i, m in zip(fss.SERVO_IDS, fss.MODELS) if m == 2825)
        self.assertEqual(sts3250_ids, (2, 3))
        self.assertIn('CHECK ((servo_id IN (2, 3)) = (model_number = 2825))', self.code)

    def test_the_hash_columns_and_the_record_bounds(self):
        self.assertIn("CHECK (spec_sha256 ~ '^[0-9a-f]{64}$')", self.code)
        self.assertIn("CHECK (record_sha256 ~ '^[0-9a-f]{64}$')", self.code)
        self.assertIn("CHECK (jsonb_typeof(record) = 'object')", self.code)
        self.assertIn('CHECK (octet_length(record::text) <= 65536)', self.code)
        self.assertIn('uploaded_by uuid REFERENCES public.users(id) ON DELETE SET NULL', self.code)

    def test_rls_on_no_policy_request_roles_revoked(self):
        self.assertIn('ALTER TABLE public.servo_calibration_records ENABLE ROW LEVEL SECURITY;', self.code)
        self.assertNotIn('CREATE POLICY', self.code.upper())
        self.assertIn('REVOKE ALL ON TABLE public.servo_calibration_records FROM PUBLIC, anon, authenticated;',
                      self.code)
        self.assertIn('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.servo_calibration_records '
                      'TO service_role;', self.code)
        self.assertNotIn('PUBLICATION', self.code.upper())

    def test_the_touch_trigger_has_an_empty_search_path(self):
        start = self.code.index('CREATE OR REPLACE FUNCTION public.servo_calibration_records_touch()')
        body = self.code[start:self.code.index('$$;', start)]
        self.assertIn("SET search_path = ''", body)
        self.assertIn('BEFORE UPDATE ON public.servo_calibration_records', self.code)


class Function052(unittest.TestCase):
    def setUp(self):
        self.code = _code(_read(M052))
        self.body = _function_body(self.code)

    def test_security_definer_with_a_pinned_search_path(self):
        self.assertIn('RETURNS jsonb', self.body)
        self.assertIn('SECURITY DEFINER', self.body)
        self.assertIn('SET search_path = public, pg_catalog', self.body)

    def test_the_admin_gate_is_the_first_statement(self):
        begin = re.search(r'\bBEGIN\b', self.body[self.body.index('AS $$'):])
        after = self.body[self.body.index('AS $$') + begin.end():].strip()
        self.assertTrue(after.startswith('IF NOT public.rls_is_admin() THEN'), after[:80])
        first = after[:after.index('END IF;')]
        self.assertIn("ERRCODE = '42501'", first)

    def test_a_malformed_record_is_22023_and_an_older_one_is_not_stored(self):
        self.assertIn("'edubotics.servo_calibration.v1'", self.body)
        self.assertIn("ERRCODE = '22023'", self.body)
        self.assertIn('FOR UPDATE', self.body)
        self.assertIn("'newer_record_exists'", self.body)
        self.assertIn('ON CONFLICT (servo_serial) DO UPDATE', self.body)
        self.assertRegex(self.body, r'uploaded_by\s*=\s*auth\.uid\(\)')
        self.assertIn("p_record->>'checksum'", self.body)

    def test_execute_only_for_authenticated_and_service_role(self):
        self.assertIn(f'REVOKE ALL ON FUNCTION {_FN_SIG} FROM PUBLIC, anon;', self.code)
        self.assertIn(f'GRANT EXECUTE ON FUNCTION {_FN_SIG} TO authenticated, service_role;', self.code)
        self.assertNotRegex(self.code, r'\bTO\s+anon\b')

    def test_the_owner_transfer(self):
        self.assertIn(f"ALTER FUNCTION {_FN_SIG} OWNER TO %I", self.code)
        for role in ('postgres', 'supabase_admin', 'supabase_storage_admin'):
            self.assertIn(f"'{role}'", self.code)

    def test_the_closing_self_check(self):
        tail = self.code[self.code.rindex('DO $$'):]
        self.assertIn("has_table_privilege(v_role, 'public.servo_calibration_records', v_priv)", tail)
        for priv in ('SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER'):
            self.assertIn(f"'{priv}'", tail)
        self.assertIn(f"has_function_privilege('anon', '{_FN_SIG}', 'EXECUTE')", tail)
        self.assertIn(f"NOT has_function_privilege('authenticated', '{_FN_SIG}', 'EXECUTE')", tail)


class Rollback052(unittest.TestCase):
    def setUp(self):
        self.raw = _read(R052)
        self.code = _code(self.raw)

    def test_the_header_says_the_api_does_not_use_it(self):
        self.assertIn('not in its boot probe', self.raw)
        self.assertIn('stop uploading from the Servo-Station first', self.raw)

    def test_one_do_block_that_refuses_with_rows(self):
        self.assertEqual(self.code.count('DO $$'), 1)
        self.assertIn("to_regclass('public.servo_calibration_records')", self.code)
        refuse = self.code.index('Rollback refused: % rows. Export them first.')
        self.assertLess(refuse, self.code.index('DROP FUNCTION IF EXISTS public.submit_servo_calibration_record'))

    def test_it_drops_in_dependency_order(self):
        order = [
            'DROP FUNCTION IF EXISTS public.submit_servo_calibration_record(jsonb)',
            'DROP TRIGGER IF EXISTS servo_calibration_records_touch ON public.servo_calibration_records',
            'DROP FUNCTION IF EXISTS public.servo_calibration_records_touch()',
            'DROP TABLE IF EXISTS public.servo_calibration_records',
        ]
        positions = [self.code.index(s) for s in order]
        self.assertEqual(positions, sorted(positions))


class TheApiDoesNotKnowTheTable(unittest.TestCase):
    """Owner R4-2: the cloud API never reads the table, so 052 stays out of its
    boot schema probe (the counts stay 19/18/46, test_schema_probe_040.py)."""

    def test_no_api_file_names_it(self):
        hits = []
        for dirpath, _dirs, files in os.walk(_API):
            for name in files:
                if name.endswith('.py'):
                    path = os.path.join(dirpath, name)
                    text = open(path, encoding='utf-8').read()
                    if ('servo_calibration_records' in text
                            or 'submit_servo_calibration_record' in text):
                        hits.append(path)
        self.assertEqual(hits, [])


if __name__ == '__main__':
    unittest.main()

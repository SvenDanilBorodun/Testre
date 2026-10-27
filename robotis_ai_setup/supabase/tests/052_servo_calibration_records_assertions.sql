-- Behavioural assertions for migration 052 (servo_calibration_records: RLS on,
-- no policy, no request-role privilege; submit_servo_calibration_record is
-- SECURITY DEFINER, admin only, EXECUTE for authenticated + service_role,
-- never overwrites a newer record and replaces an older one), run BY HAND
-- against a fresh LOCAL database —
-- never CI, never the linked project (it seeds rows).
--
-- The squashed baseline does NOT replay on a fresh database as shipped
-- (docs/KNOWN-ISSUES.md, „baseline.sql is NOT replayable from scratch"): run it in
-- a SCRATCH copy of supabase/ whose baseline has its one
-- `RENAME COLUMN runpod_job_id` line guarded exactly as
-- 040_code_programs_assertions.sql's header shows, with every migration through
-- 052 applied (`supabase start` in the scratch copy, or psql as `postgres` with
-- -v ON_ERROR_STOP=1 in timestamp order), then
--   psql <local db url> -f supabase/tests/052_servo_calibration_records_assertions.sql
-- A PostgREST request is simulated the way PostgREST runs it (the 044 idiom):
-- the JWT claims as settings plus SET LOCAL ROLE authenticated inside a DO block.
-- Calling a function GRANTED to authenticated under SET ROLE works (run on a local
-- supabase/postgres:17.6.1.106 with every migration through 052 applied) — the
-- signal-11 crash in docs/KNOWN-ISSUES.md concerns calling a service-role-only
-- function; every DENIAL of a privilege is asserted with has_table_privilege /
-- has_function_privilege, never by calling. Two CONCURRENT first uploads (the
-- race the one-statement upsert closes) need two sessions and are not in here.
-- Expected: 12 PASS, 0 FAIL/ERROR (T1..T11, T2 with a/b halves).
\set ON_ERROR_STOP off
\pset pager off

INSERT INTO auth.users (id, instance_id, aud, role, email, encrypted_password, created_at, updated_at)
VALUES ('b5200001-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 's1-052@x.test', '', now(), now()),
       ('b5200002-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 'a1-052@x.test', '', now(), now())
ON CONFLICT (id) DO NOTHING;
UPDATE public.users SET role = 'student' WHERE id = 'b5200001-0000-0000-0000-000000000001';
UPDATE public.users SET role = 'admin'   WHERE id = 'b5200002-0000-0000-0000-000000000002';

CREATE OR REPLACE FUNCTION pg_temp.claims_student() RETURNS void LANGUAGE sql AS $$
  SELECT set_config('request.jwt.claim.sub', 'b5200001-0000-0000-0000-000000000001', true),
         set_config('request.jwt.claims', '{"sub":"b5200001-0000-0000-0000-000000000001","role":"authenticated"}', true);
$$;
CREATE OR REPLACE FUNCTION pg_temp.claims_admin() RETURNS void LANGUAGE sql AS $$
  SELECT set_config('request.jwt.claim.sub', 'b5200002-0000-0000-0000-000000000002', true),
         set_config('request.jwt.claims', '{"sub":"b5200002-0000-0000-0000-000000000002","role":"authenticated"}', true);
$$;
-- one well-formed station record; the tests vary single fields of it
CREATE OR REPLACE FUNCTION pg_temp.rec(p_serial text, p_at text) RETURNS jsonb LANGUAGE sql AS $$
  SELECT jsonb_build_object(
    'schema', 'edubotics.servo_calibration.v1', 'servo_serial', p_serial, 'profile_id', 'v1_studio',
    'joint', 'joint2', 'servo_id', 2, 'model_number', 2825, 'model_name', 'STS3250', 'firmware', '3.10',
    'spec_sha256', repeat('a', 64), 'incoming_test', NULL, 'calibrated_at', p_at,
    'checksum', repeat('b', 64));
$$;

\echo '=== T1 the table exists, RLS on, no policy ==='
SELECT CASE WHEN c.relrowsecurity
             AND NOT EXISTS (SELECT 1 FROM pg_policies
                              WHERE schemaname = 'public' AND tablename = 'servo_calibration_records')
            THEN 'PASS T1' ELSE 'FAIL T1' END
  FROM pg_class c WHERE c.oid = 'public.servo_calibration_records'::regclass;

\echo '=== T2 neither request role holds any table privilege ==='
SELECT CASE WHEN NOT bool_or(has_table_privilege('anon', 'public.servo_calibration_records', p))
            THEN 'PASS T2a' ELSE 'FAIL T2a' END
  FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) AS p;
SELECT CASE WHEN NOT bool_or(has_table_privilege('authenticated', 'public.servo_calibration_records', p))
            THEN 'PASS T2b' ELSE 'FAIL T2b' END
  FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) AS p;

\echo '=== T3 service_role reads and writes the table ==='
SELECT CASE WHEN bool_and(has_table_privilege('service_role', 'public.servo_calibration_records', p))
            THEN 'PASS T3' ELSE 'FAIL T3' END
  FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE']) AS p;

\echo '=== T4 exactly one writer: SECURITY DEFINER, pinned search_path, jsonb -> jsonb ==='
SELECT CASE WHEN count(*) = 1
             AND bool_and(p.prosecdef)
             AND bool_and('search_path=public, pg_catalog' = ANY (p.proconfig))
             AND bool_and(pg_get_function_identity_arguments(p.oid) = 'p_record jsonb')
             AND bool_and(p.prorettype = 'jsonb'::regtype)
            THEN 'PASS T4' ELSE 'FAIL T4 (' || count(*) || ' rows)' END
  FROM pg_proc p
 WHERE p.proname = 'submit_servo_calibration_record' AND p.pronamespace = 'public'::regnamespace;

\echo '=== T5 EXECUTE: anon no, authenticated yes, PUBLIC no ==='
SELECT CASE WHEN NOT has_function_privilege('anon', 'public.submit_servo_calibration_record(jsonb)', 'EXECUTE')
             AND has_function_privilege('authenticated', 'public.submit_servo_calibration_record(jsonb)', 'EXECUTE')
             AND p.proacl IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM aclexplode(p.proacl) a
                              WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE')
            THEN 'PASS T5' ELSE 'FAIL T5' END
  FROM pg_proc p
 WHERE p.proname = 'submit_servo_calibration_record' AND p.pronamespace = 'public'::regnamespace;

\echo '=== T6 a student calling the writer gets 42501 and writes nothing ==='
DO $$ BEGIN
  PERFORM pg_temp.claims_student();
  SET LOCAL ROLE authenticated;
  PERFORM public.submit_servo_calibration_record(pg_temp.rec('V1S-052001', '2026-10-27T10:00:00Z'));
  RAISE NOTICE 'FAIL T6 (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T6 (42501)'; END $$;
SELECT CASE WHEN count(*) = 0 THEN 'T6 no row written' ELSE 'FAIL T6 (row written)' END
  FROM public.servo_calibration_records WHERE servo_serial = 'V1S-052001';

\echo '=== T7 an admin stores a record; uploaded_by is the admin ==='
DO $$
DECLARE v jsonb;
BEGIN
  PERFORM pg_temp.claims_admin();
  SET LOCAL ROLE authenticated;
  v := public.submit_servo_calibration_record(pg_temp.rec('V1S-052002', '2026-10-27T10:00:00Z'));
  IF v->>'stored' = 'true' AND v->>'replaced' = 'false' THEN
    RAISE NOTICE 'T7 answer %', v;
  ELSE
    RAISE NOTICE 'FAIL T7 (answer %)', v;
  END IF;
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T7 (%: %)', SQLSTATE, SQLERRM; END $$;
SELECT CASE WHEN uploaded_by = 'b5200002-0000-0000-0000-000000000002'
             AND joint = 'joint2' AND servo_id = 2 AND model_number = 2825
             AND record_sha256 = repeat('b', 64)
            THEN 'PASS T7' ELSE 'FAIL T7' END
  FROM public.servo_calibration_records WHERE servo_serial = 'V1S-052002';

\echo '=== T8 an OLDER record for the same serial is not stored and changes nothing ==='
DO $$
DECLARE v jsonb;
BEGIN
  PERFORM pg_temp.claims_admin();
  SET LOCAL ROLE authenticated;
  v := public.submit_servo_calibration_record(
         jsonb_set(pg_temp.rec('V1S-052002', '2026-10-26T10:00:00Z'), '{checksum}', to_jsonb(repeat('c', 64))));
  IF v->>'stored' = 'false' AND v->>'reason' = 'newer_record_exists' THEN
    RAISE NOTICE 'T8 answer %', v;
  ELSE
    RAISE NOTICE 'FAIL T8 (answer %)', v;
  END IF;
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T8 (%: %)', SQLSTATE, SQLERRM; END $$;
SELECT CASE WHEN calibrated_at = '2026-10-27T10:00:00Z'::timestamptz AND record_sha256 = repeat('b', 64)
            THEN 'PASS T8' ELSE 'FAIL T8 (row changed)' END
  FROM public.servo_calibration_records WHERE servo_serial = 'V1S-052002';

\echo '=== T9 a malformed record (servo_id not a number) is 22023 ==='
DO $$ BEGIN
  PERFORM pg_temp.claims_admin();
  SET LOCAL ROLE authenticated;
  PERFORM public.submit_servo_calibration_record(
            jsonb_set(pg_temp.rec('V1S-052003', '2026-10-27T10:00:00Z'), '{servo_id}', '"zwei"'));
  RAISE NOTICE 'FAIL T9 (no error)';
EXCEPTION WHEN SQLSTATE '22023' THEN RAISE NOTICE 'PASS T9 (22023)';
          WHEN OTHERS THEN RAISE NOTICE 'FAIL T9 (%: %)', SQLSTATE, SQLERRM; END $$;

\echo '=== T10 the serial CHECK refuses X-1 (23514) ==='
DO $$ BEGIN
  PERFORM pg_temp.claims_admin();
  SET LOCAL ROLE authenticated;
  PERFORM public.submit_servo_calibration_record(pg_temp.rec('X-1', '2026-10-27T10:00:00Z'));
  RAISE NOTICE 'FAIL T10 (no error)';
EXCEPTION WHEN SQLSTATE '23514' THEN RAISE NOTICE 'PASS T10 (23514)';
          WHEN OTHERS THEN RAISE NOTICE 'FAIL T10 (%: %)', SQLSTATE, SQLERRM; END $$;

\echo '=== T11 a NEWER record for the same serial replaces the stored one ==='
DO $$
DECLARE v jsonb;
BEGIN
  PERFORM pg_temp.claims_admin();
  SET LOCAL ROLE authenticated;
  v := public.submit_servo_calibration_record(
         jsonb_set(pg_temp.rec('V1S-052002', '2026-10-28T10:00:00Z'), '{checksum}', to_jsonb(repeat('d', 64))));
  IF v->>'stored' = 'true' AND v->>'replaced' = 'true' THEN
    RAISE NOTICE 'T11 answer %', v;
  ELSE
    RAISE NOTICE 'FAIL T11 (answer %)', v;
  END IF;
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T11 (%: %)', SQLSTATE, SQLERRM; END $$;
SELECT CASE WHEN calibrated_at = '2026-10-28T10:00:00Z'::timestamptz AND record_sha256 = repeat('d', 64)
            THEN 'PASS T11' ELSE 'FAIL T11 (row not replaced)' END
  FROM public.servo_calibration_records WHERE servo_serial = 'V1S-052002';

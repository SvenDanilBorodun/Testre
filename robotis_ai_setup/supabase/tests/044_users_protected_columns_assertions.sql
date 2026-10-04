-- Behavioural assertions for migration 044 (public.users: the five protected
-- columns are server-only, and a stored token fixes users.hf_username), run BY
-- HAND against a fresh LOCAL database — never CI, never the linked project (it
-- seeds rows).
--
-- The squashed baseline does NOT replay on a fresh database as shipped
-- (docs/KNOWN-ISSUES.md, „baseline.sql is NOT replayable from scratch"): run
-- it in a SCRATCH copy of supabase/ whose baseline has its one
-- `RENAME COLUMN runpod_job_id` line guarded exactly as
-- 040_code_programs_assertions.sql's header shows, with every migration
-- through 045, then
--   supabase start   (in the scratch copy; it applies the migrations)
--   psql <local db url> < supabase/tests/044_users_protected_columns_assertions.sql
-- (2026-10-04 it was run against public.ecr.aws/supabase/postgres:17.6.1.106
-- with the migrations applied in order by psql as `postgres`.) A PostgREST
-- request is simulated the way PostgREST runs it: SET LOCAL ROLE plus the JWT
-- claims as settings (both spellings auth.uid() has read over the years).
-- Expected: 16 PASS, 0 FAIL/ERROR (T1..T15, T2 with a/b halves).
\set ON_ERROR_STOP off
\pset pager off

INSERT INTO auth.users (id, instance_id, aud, role, email, encrypted_password, created_at, updated_at)
VALUES ('b4400001-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 's1-044@x.test', '', now(), now()),
       ('b4400002-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 't1-044@x.test', '', now(), now())
ON CONFLICT (id) DO NOTHING;

-- a teacher with a pool and a classroom, the student in it (for T15)
UPDATE public.users SET role = 'teacher', training_credits = 10 WHERE id = 'b4400002-0000-0000-0000-000000000002';
INSERT INTO public.classrooms (id, teacher_id, name)
VALUES ('c4400001-0000-0000-0000-000000000001', 'b4400002-0000-0000-0000-000000000002', 'Klasse 044')
ON CONFLICT (id) DO NOTHING;
UPDATE public.users SET classroom_id = 'c4400001-0000-0000-0000-000000000001', full_name = 'Anna'
 WHERE id = 'b4400001-0000-0000-0000-000000000001';

-- `as_student` runs one statement the way a PostgREST request of student s1 does.
CREATE OR REPLACE FUNCTION pg_temp.claims_s1() RETURNS void LANGUAGE sql AS $$
  SELECT set_config('request.jwt.claim.sub', 'b4400001-0000-0000-0000-000000000001', true),
         set_config('request.jwt.claims', '{"sub":"b4400001-0000-0000-0000-000000000001","role":"authenticated"}', true);
$$;

\echo '=== T1 the guard: BEFORE UPDATE FOR EACH ROW on public.users, SECURITY INVOKER, empty search_path ==='
SELECT CASE WHEN count(*) = 1
                 AND bool_and(NOT p.prosecdef)
                 AND bool_and('search_path=""' = ANY (p.proconfig))
                 AND bool_and((t.tgtype & 1) = 1      -- ROW
                          AND (t.tgtype & 2) = 2      -- BEFORE
                          AND (t.tgtype & 16) = 16)   -- UPDATE
            THEN 'PASS T1' ELSE 'FAIL T1 (' || count(*) || ' rows)' END
  FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid
 WHERE t.tgrelid = 'public.users'::regclass AND t.tgname = 'trg_0_users_protected_columns'
   AND p.proname = 'users_guard_protected_columns';

\echo '=== T2 a student cannot make themselves admin (42501), and the row is unchanged ==='
DO $$ BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  UPDATE public.users SET role = 'admin' WHERE id = 'b4400001-0000-0000-0000-000000000001';
  RAISE NOTICE 'FAIL T2a (no error; rows=%)', (SELECT count(*) FROM public.users WHERE id = 'b4400001-0000-0000-0000-000000000001' AND role = 'admin');
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T2a (%)', SQLERRM; END $$;
SELECT CASE WHEN role = 'student' THEN 'PASS T2b' ELSE 'FAIL T2b' END
  FROM public.users WHERE id = 'b4400001-0000-0000-0000-000000000001';

\echo '=== T3 ... nor give themselves credits ==='
DO $$ BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  UPDATE public.users SET training_credits = 999 WHERE id = 'b4400001-0000-0000-0000-000000000001';
  RAISE NOTICE 'FAIL T3 (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T3 (42501)'; END $$;

\echo '=== T4 ... nor re-point their Hugging Face anchor ==='
DO $$ BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  UPDATE public.users SET hf_username = 'someone-else' WHERE id = 'b4400001-0000-0000-0000-000000000001';
  RAISE NOTICE 'FAIL T4 (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T4 (42501)'; END $$;

\echo '=== T5 ... nor move to another classroom ==='
DO $$ BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  UPDATE public.users SET classroom_id = NULL WHERE id = 'b4400001-0000-0000-0000-000000000001';
  RAISE NOTICE 'FAIL T5 (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T5 (42501)'; END $$;

\echo '=== T6 ... nor join a workgroup ==='
DO $$ BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  UPDATE public.users SET workgroup_id = 'd4400001-0000-0000-0000-000000000001' WHERE id = 'b4400001-0000-0000-0000-000000000001';
  RAISE NOTICE 'FAIL T6 (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T6 (42501)'; END $$;

\echo '=== T7 a column outside the five (full_name) is still the student''s own ==='
DO $$
DECLARE n INT;
BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  UPDATE public.users SET full_name = 'Anna B.' WHERE id = 'b4400001-0000-0000-0000-000000000001';
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n = 1 THEN RAISE NOTICE 'PASS T7 (1 row)'; ELSE RAISE NOTICE 'FAIL T7 (% rows)', n; END IF;
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T7 (%: %)', SQLSTATE, SQLERRM; END $$;

\echo '=== T8 writing a protected column back with its CURRENT value is no change and passes ==='
DO $$
DECLARE n INT;
BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  UPDATE public.users SET role = 'student', training_credits = 0, full_name = 'Anna'
   WHERE id = 'b4400001-0000-0000-0000-000000000001';
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n = 1 THEN RAISE NOTICE 'PASS T8 (1 row)'; ELSE RAISE NOTICE 'FAIL T8 (% rows)', n; END IF;
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T8 (%: %)', SQLSTATE, SQLERRM; END $$;

\echo '=== T9 the cloud API (service_role) still writes them ==='
DO $$
DECLARE n INT;
BEGIN
  SET LOCAL ROLE service_role;
  UPDATE public.users SET training_credits = 3, hf_username = 'anna-hf'
   WHERE id = 'b4400001-0000-0000-0000-000000000001';
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n = 1 THEN RAISE NOTICE 'PASS T9 (1 row)'; ELSE RAISE NOTICE 'FAIL T9 (% rows)', n; END IF;
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T9 (%: %)', SQLSTATE, SQLERRM; END $$;

\echo '=== T10 the token writer (SECURITY DEFINER, called as service_role) still sets the proven name ==='
DO $$ BEGIN
  SET LOCAL ROLE service_role;
  PERFORM public.store_user_hf_credential(
    'b4400001-0000-0000-0000-000000000001',
    'v1.3b28fa93.oKGio6Slpqeoqaqrjn4jTCSqY94DBOayZhuhvxHNOHHz1iMN_W9H5x7KFGCzFyaezvqdnUay92eVBeK79YNbhgY',
    'c1770a7966b0771e', 'hf_…aaaa', 'anna-proven', 'write');
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T10 (%: %)', SQLSTATE, SQLERRM; END $$;
SELECT CASE WHEN hf_username = 'anna-proven' THEN 'PASS T10' ELSE 'FAIL T10 (' || coalesce(hf_username, 'NULL') || ')' END
  FROM public.users WHERE id = 'b4400001-0000-0000-0000-000000000001';

\echo '=== T11 while a token is stored, NOT EVEN the service role may set another name (P0044) ==='
DO $$ BEGIN
  SET LOCAL ROLE service_role;
  UPDATE public.users SET hf_username = 'self-asserted' WHERE id = 'b4400001-0000-0000-0000-000000000001';
  RAISE NOTICE 'FAIL T11 (no error)';
EXCEPTION WHEN SQLSTATE 'P0044' THEN RAISE NOTICE 'PASS T11 (%)', SQLERRM; END $$;

\echo '=== T12 ... and the name stays the proven one ==='
SELECT CASE WHEN hf_username = 'anna-proven' THEN 'PASS T12' ELSE 'FAIL T12' END
  FROM public.users WHERE id = 'b4400001-0000-0000-0000-000000000001';

\echo '=== T13 without a stored token the service role sets any name again (PATCH /me) ==='
DELETE FROM public.user_hf_credentials WHERE user_id = 'b4400001-0000-0000-0000-000000000001';
DO $$
DECLARE n INT;
BEGIN
  SET LOCAL ROLE service_role;
  UPDATE public.users SET hf_username = 'self-asserted' WHERE id = 'b4400001-0000-0000-0000-000000000001';
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n = 1 THEN RAISE NOTICE 'PASS T13 (1 row)'; ELSE RAISE NOTICE 'FAIL T13 (% rows)', n; END IF;
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T13 (%: %)', SQLSTATE, SQLERRM; END $$;

\echo '=== T14 a SECURITY DEFINER function runs as its owner: the guard lets it through even for a student ==='
CREATE FUNCTION public._t044_definer_set_credits(p_id uuid, p_credits int) RETURNS int
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE n INT;
BEGIN
  UPDATE public.users SET training_credits = p_credits WHERE id = p_id;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;
GRANT EXECUTE ON FUNCTION public._t044_definer_set_credits(uuid, int) TO authenticated;
DO $$
DECLARE n INT;
BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  n := public._t044_definer_set_credits('b4400001-0000-0000-0000-000000000001', 4);
  IF n = 1 THEN RAISE NOTICE 'PASS T14 (1 row)'; ELSE RAISE NOTICE 'FAIL T14 (% rows)', n; END IF;
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T14 (%: %)', SQLSTATE, SQLERRM; END $$;
DROP FUNCTION public._t044_definer_set_credits(uuid, int);

\echo '=== T15 the baseline credit RPC (adjust_student_credits, service_role) still works ==='
DO $$
DECLARE v_new INT;
BEGIN
  SET LOCAL ROLE service_role;
  SELECT new_amount INTO v_new FROM public.adjust_student_credits(
    'b4400002-0000-0000-0000-000000000002', 'b4400001-0000-0000-0000-000000000001', 1);
  IF v_new = 5 THEN RAISE NOTICE 'PASS T15 (credits 5)'; ELSE RAISE NOTICE 'FAIL T15 (credits %)', v_new; END IF;
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T15 (%: %)', SQLSTATE, SQLERRM; END $$;

-- cleanup
DELETE FROM public.classrooms WHERE id = 'c4400001-0000-0000-0000-000000000001';
DELETE FROM auth.users WHERE id IN ('b4400001-0000-0000-0000-000000000001', 'b4400002-0000-0000-0000-000000000002');

-- Behavioural assertions for migration 055 (public.users: the request roles may
-- change no column of a users row; 044's rule 2, P0044, unchanged), run BY HAND
-- against a LOCAL database — never CI, never the linked project (it seeds rows
-- and removes them at the end; T16 runs inside a transaction it rolls back).
--
-- The baseline replays on a fresh database as shipped, so a local stack of the
-- checkout itself (`supabase start`, or every migration applied in order by
-- psql) is the database: with every migration through 055 applied, then
--   psql <local db url> -f supabase/tests/055_users_identity_guard_assertions.sql
-- A PostgREST request is simulated the way PostgREST runs it, SET LOCAL ROLE
-- plus the JWT claims as settings, with a plain table statement inside a DO
-- block (044's pattern). No service-role-only function is called under SET
-- ROLE: the vision-quota writer (T14) is called as the migration owner, the
-- definer path (T13) through a throw-away function granted to authenticated.
-- 2026-11-11: run on PostgreSQL 16 with stub Supabase roles (anon,
-- authenticated, service_role), a stub auth schema and the supabase_realtime
-- publication, every migration 000 → 054 applied by psql, then 055:
-- 27 PASS, 0 FAIL, 0 ERROR. Against the database at 054 (or after the 055
-- rollback) 15 PASS and 12 FAIL: T2, the eight column refusals T3a–T3h, T5 (the
-- refused writes went through), T8 and T12. The cycle 055 → rollback →
-- rollback → 055 → 055 passes each self-check and prints 27 PASS after every
-- apply, and after the rollback pg_dump -s equals the dump at 054. A copy of
-- 055 with the guard made SECURITY DEFINER, and one with rule 1 narrowed back
-- to a column list, each abort in the self-check.
-- 055 BREAKS 044_users_protected_columns_assertions.sql's T7 by design (T7
-- proves full_name was still the student's own): measured after 055, that file
-- prints 14 PASS and FAIL T7, T15 (T15 is broken by 052, as its header says).
--
-- Expected: 27 PASS, 0 FAIL (T1..T16; T3a..T3h, T4a/T4b, T9a..T9c, T11a/T11b).
\set ON_ERROR_STOP off
\pset pager off

INSERT INTO auth.users (id, instance_id, aud, role, email, encrypted_password, created_at, updated_at)
VALUES ('b5500001-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 's1-055@x.test', '', now(), now()),
       ('b5500002-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 't1-055@x.test', '', now(), now()),
       ('b5500003-0000-0000-0000-000000000003', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 'c1-055@x.test', '', now(), now())
ON CONFLICT (id) DO NOTHING;

-- a teacher with a classroom, the student in it, created by the teacher
UPDATE public.users SET role = 'teacher', username = 'lehrer055', full_name = 'Frau Lehrer'
 WHERE id = 'b5500002-0000-0000-0000-000000000002';
INSERT INTO public.classrooms (id, teacher_id, name)
VALUES ('c5500001-0000-0000-0000-000000000001', 'b5500002-0000-0000-0000-000000000002', 'Klasse 055')
ON CONFLICT (id) DO NOTHING;
UPDATE public.users
   SET classroom_id = 'c5500001-0000-0000-0000-000000000001', full_name = 'Anna', username = 'anna055',
       created_by = 'b5500002-0000-0000-0000-000000000002'
 WHERE id = 'b5500001-0000-0000-0000-000000000001';

-- `claims_s1` makes the next statements a PostgREST request of student s1.
CREATE OR REPLACE FUNCTION pg_temp.claims_s1() RETURNS void LANGUAGE sql AS $$
  SELECT set_config('request.jwt.claim.sub', 'b5500001-0000-0000-0000-000000000001', true),
         set_config('request.jwt.claims', '{"sub":"b5500001-0000-0000-0000-000000000001","role":"authenticated"}', true);
$$;

-- `try_as_student` runs one SET as student s1 and answers the SQLSTATE ('00000' = it changed a row,
-- 'no-row' = it matched nothing).
CREATE OR REPLACE FUNCTION pg_temp.try_as_student(p_set TEXT) RETURNS TEXT LANGUAGE plpgsql AS $$
DECLARE n INT;
BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  EXECUTE 'UPDATE public.users SET ' || p_set || ' WHERE id = ''b5500001-0000-0000-0000-000000000001''';
  GET DIAGNOSTICS n = ROW_COUNT;
  RESET ROLE;
  RETURN CASE WHEN n = 1 THEN '00000' ELSE 'no-row' END;
EXCEPTION WHEN OTHERS THEN
  RETURN SQLSTATE;
END $$;

-- `try_as_service` runs one SET as the cloud API's service role.
CREATE OR REPLACE FUNCTION pg_temp.try_as_service(p_set TEXT) RETURNS TEXT LANGUAGE plpgsql AS $$
DECLARE n INT;
BEGIN
  SET LOCAL ROLE service_role;
  EXECUTE 'UPDATE public.users SET ' || p_set || ' WHERE id = ''b5500001-0000-0000-0000-000000000001''';
  GET DIAGNOSTICS n = ROW_COUNT;
  RESET ROLE;
  RETURN CASE WHEN n = 1 THEN '00000' ELSE 'no-row' END;
EXCEPTION WHEN OTHERS THEN
  RETURN SQLSTATE;
END $$;

\echo '=== T1 the guard: BEFORE UPDATE FOR EACH ROW on public.users, SECURITY INVOKER, empty search_path, fires first ==='
SELECT CASE WHEN count(*) = 1
                 AND bool_and(NOT p.prosecdef)
                 AND bool_and(p.proconfig = ARRAY['search_path=""'])
                 AND bool_and((t.tgtype & 1) = 1 AND (t.tgtype & 2) = 2 AND (t.tgtype & 16) = 16)
                 AND bool_and(t.tgenabled = 'O')
                 AND NOT EXISTS (SELECT 1 FROM pg_trigger o
                                  WHERE o.tgrelid = 'public.users'::regclass AND NOT o.tgisinternal
                                    AND (o.tgtype & 3) = 3 AND o.tgname < 'trg_0_users_protected_columns')
            THEN 'PASS T1' ELSE 'FAIL T1 (' || count(*) || ' rows)' END
  FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid
 WHERE t.tgrelid = 'public.users'::regclass AND t.tgname = 'trg_0_users_protected_columns'
   AND p.proname = 'users_guard_protected_columns';

\echo '=== T2 rule 1 judges the whole row (055); nobody may call the guard directly ==='
SELECT CASE WHEN p.prosrc ~ 'current_user IN \(''authenticated'', ''anon''\) AND NEW IS DISTINCT FROM OLD'
                 AND NOT has_function_privilege('authenticated', p.oid, 'EXECUTE')
                 AND NOT has_function_privilege('anon', p.oid, 'EXECUTE')
            THEN 'PASS T2' ELSE 'FAIL T2' END
  FROM pg_proc p WHERE p.oid = 'public.users_guard_protected_columns()'::regprocedure;

\echo '=== T3 the student can change none of the eight columns 044 left open (42501 each) ==='
SELECT CASE WHEN pg_temp.try_as_student('deletion_requested_at = now()') = '42501' THEN 'PASS T3a' ELSE 'FAIL T3a' END;
SELECT CASE WHEN pg_temp.try_as_student('username = ''someone''') = '42501' THEN 'PASS T3b' ELSE 'FAIL T3b' END;
SELECT CASE WHEN pg_temp.try_as_student('full_name = ''Anna B.''') = '42501' THEN 'PASS T3c' ELSE 'FAIL T3c' END;
SELECT CASE WHEN pg_temp.try_as_student('email = ''x@evil.test''') = '42501' THEN 'PASS T3d' ELSE 'FAIL T3d' END;
SELECT CASE WHEN pg_temp.try_as_student('created_by = NULL') = '42501' THEN 'PASS T3e' ELSE 'FAIL T3e' END;
SELECT CASE WHEN pg_temp.try_as_student('vision_quota_per_term = 999') = '42501' THEN 'PASS T3f' ELSE 'FAIL T3f' END;
SELECT CASE WHEN pg_temp.try_as_student('vision_used_per_term = 0, vision_quota_per_term = 1') = '42501' THEN 'PASS T3g' ELSE 'FAIL T3g' END;
SELECT CASE WHEN pg_temp.try_as_student('created_at = now() - interval ''1 year''') = '42501' THEN 'PASS T3h' ELSE 'FAIL T3h' END;

\echo '=== T4 ... and 044''s five are still refused (role, training_credits) ==='
SELECT CASE WHEN pg_temp.try_as_student('role = ''admin''') = '42501' THEN 'PASS T4a' ELSE 'FAIL T4a' END;
SELECT CASE WHEN pg_temp.try_as_student('hf_username = ''other-hf''') = '42501' THEN 'PASS T4b' ELSE 'FAIL T4b' END;

\echo '=== T5 nothing above changed the row ==='
SELECT CASE WHEN role = 'student' AND username = 'anna055' AND full_name = 'Anna'
                 AND email = 's1-055@x.test' AND created_by = 'b5500002-0000-0000-0000-000000000002'
                 AND deletion_requested_at IS NULL AND vision_quota_per_term IS NULL
                 AND vision_used_per_term = 0 AND hf_username IS NULL
            THEN 'PASS T5' ELSE 'FAIL T5' END
  FROM public.users WHERE id = 'b5500001-0000-0000-0000-000000000001';

\echo '=== T6 a write that changes nothing (every column back with its current value) passes ==='
SELECT CASE WHEN pg_temp.try_as_student(
  'role = role, username = username, full_name = full_name, email = email, created_by = created_by, '
  || 'deletion_requested_at = deletion_requested_at, vision_quota_per_term = vision_quota_per_term, '
  || 'vision_used_per_term = vision_used_per_term, created_at = created_at, hf_username = hf_username, '
  || 'classroom_id = classroom_id, workgroup_id = workgroup_id, training_credits = training_credits')
  = '00000' THEN 'PASS T6' ELSE 'FAIL T6' END;

\echo '=== T7 /me/delete: the cloud API (service_role) records a request ==='
SELECT CASE WHEN pg_temp.try_as_service('deletion_requested_at = now()') = '00000' THEN 'PASS T7' ELSE 'FAIL T7' END;

\echo '=== T8 ... and the student cannot clear or back-date it behind the API''s back ==='
SELECT CASE WHEN pg_temp.try_as_student('deletion_requested_at = NULL') = '42501'
             AND pg_temp.try_as_student('deletion_requested_at = deletion_requested_at - interval ''40 days''') = '42501'
             AND (SELECT deletion_requested_at > now() - interval '1 hour'
                    FROM public.users WHERE id = 'b5500001-0000-0000-0000-000000000001')
            THEN 'PASS T8' ELSE 'FAIL T8' END;

\echo '=== T9 the API (service_role) still writes the identity columns: withdraw, rename, provisioning ==='
SELECT CASE WHEN pg_temp.try_as_service('deletion_requested_at = NULL') = '00000' THEN 'PASS T9a' ELSE 'FAIL T9a' END;
SELECT CASE WHEN pg_temp.try_as_service('full_name = ''Anna Berg'', username = ''anna055b''') = '00000'
            THEN 'PASS T9b' ELSE 'FAIL T9b' END;
SELECT CASE WHEN pg_temp.try_as_service(
  'email = ''anna055b@edubotics.local'', created_by = ''b5500002-0000-0000-0000-000000000002'', '
  || 'vision_quota_per_term = 5, vision_used_per_term = 2') = '00000'
            THEN 'PASS T9c' ELSE 'FAIL T9c' END;

\echo '=== T10 ... and the row now carries exactly what the service role wrote ==='
SELECT CASE WHEN deletion_requested_at IS NULL AND full_name = 'Anna Berg' AND username = 'anna055b'
                 AND email = 'anna055b@edubotics.local' AND vision_quota_per_term = 5 AND vision_used_per_term = 2
            THEN 'PASS T10' ELSE 'FAIL T10' END
  FROM public.users WHERE id = 'b5500001-0000-0000-0000-000000000001';

\echo '=== T11 rule 2 (P0044) is unchanged: with a stored token not even the service role sets another name ==='
INSERT INTO public.user_hf_credentials (user_id, token_ciphertext, token_fp, token_hint, hf_username, token_role, validated_at)
VALUES ('b5500001-0000-0000-0000-000000000001',
        'v1.3b28fa93.oKGio6Slpqeoqaqrjn4jTCSqY94DBOayZhuhvxHNOHHz1iMN_W9H5x7KFGCzFyaezvqdnUay92eVBeK79YNbhgY',
        'c1770a7966b0771e', 'hf_…aaaa', 'anna-proven', 'write', now())
ON CONFLICT (user_id) DO NOTHING;
SELECT CASE WHEN pg_temp.try_as_service('hf_username = ''self-asserted''') = 'P0044' THEN 'PASS T11a' ELSE 'FAIL T11a' END;
SELECT CASE WHEN pg_temp.try_as_service('hf_username = ''anna-proven''') = '00000' THEN 'PASS T11b' ELSE 'FAIL T11b' END;
DELETE FROM public.user_hf_credentials WHERE user_id = 'b5500001-0000-0000-0000-000000000001';

\echo '=== T12 the teacher (also authenticated) can change none of their own columns either ==='
DO $$
BEGIN
  PERFORM set_config('request.jwt.claim.sub', 'b5500002-0000-0000-0000-000000000002', true),
          set_config('request.jwt.claims', '{"sub":"b5500002-0000-0000-0000-000000000002","role":"authenticated"}', true);
  SET LOCAL ROLE authenticated;
  UPDATE public.users SET full_name = 'Herr Lehrer' WHERE id = 'b5500002-0000-0000-0000-000000000002';
  RAISE NOTICE 'FAIL T12 (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T12 (42501)'; END $$;

\echo '=== T13 a SECURITY DEFINER function runs as its owner: the guard lets it through even for a student ==='
CREATE FUNCTION public._t055_definer_set_name(p_id uuid, p_name text) RETURNS int
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE n INT;
BEGIN
  UPDATE public.users SET full_name = p_name, deletion_requested_at = now() WHERE id = p_id;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;
GRANT EXECUTE ON FUNCTION public._t055_definer_set_name(uuid, text) TO authenticated;
DO $$
DECLARE n INT;
BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  n := public._t055_definer_set_name('b5500001-0000-0000-0000-000000000001', 'Anna D.');
  IF n = 1 THEN RAISE NOTICE 'PASS T13 (1 row)'; ELSE RAISE NOTICE 'FAIL T13 (% rows)', n; END IF;
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T13 (%: %)', SQLSTATE, SQLERRM; END $$;
DROP FUNCTION public._t055_definer_set_name(uuid, text);

\echo '=== T14 the baseline vision-quota writer (SECURITY DEFINER) still resets the counter (called as its owner) ==='
DO $$
DECLARE v_used INT;
BEGIN
  PERFORM public.reset_vision_quota_used();
  SELECT vision_used_per_term INTO v_used FROM public.users WHERE id = 'b5500001-0000-0000-0000-000000000001';
  IF v_used = 0 THEN RAISE NOTICE 'PASS T14 (0)'; ELSE RAISE NOTICE 'FAIL T14 (%)', v_used; END IF;
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T14 (%: %)', SQLSTATE, SQLERRM; END $$;

\echo '=== T15 the student''s whole own row is still readable (the guard is UPDATE-only) ==='
DO $$
DECLARE n INT;
BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  SELECT count(*) INTO n FROM public.users WHERE id = 'b5500001-0000-0000-0000-000000000001';
  IF n = 1 THEN RAISE NOTICE 'PASS T15 (1 row)'; ELSE RAISE NOTICE 'FAIL T15 (% rows)', n; END IF;
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T15 (%: %)', SQLSTATE, SQLERRM; END $$;

\echo '=== T16 an ON DELETE SET NULL action runs as the table owner: a request-role delete of the creator nulls created_by ==='
-- In a transaction that is rolled back: a throw-away DELETE policy lets the creator
-- (authenticated) delete their own users row; the RI action then UPDATEs the
-- student's created_by. Had it run as authenticated, the guard would refuse it.
BEGIN;
UPDATE public.users SET created_by = 'b5500003-0000-0000-0000-000000000003'
 WHERE id = 'b5500001-0000-0000-0000-000000000001';
CREATE POLICY t055_delete_own ON public.users FOR DELETE TO authenticated USING (id = (SELECT auth.uid()));
DO $$
DECLARE v_by UUID;
BEGIN
  PERFORM set_config('request.jwt.claim.sub', 'b5500003-0000-0000-0000-000000000003', true),
          set_config('request.jwt.claims', '{"sub":"b5500003-0000-0000-0000-000000000003","role":"authenticated"}', true);
  SET LOCAL ROLE authenticated;
  DELETE FROM public.users WHERE id = 'b5500003-0000-0000-0000-000000000003';
  RESET ROLE;
  SELECT created_by INTO v_by FROM public.users WHERE id = 'b5500001-0000-0000-0000-000000000001';
  IF v_by IS NULL THEN RAISE NOTICE 'PASS T16 (created_by NULL)'; ELSE RAISE NOTICE 'FAIL T16 (%)', v_by; END IF;
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T16 (%: %)', SQLSTATE, SQLERRM; END $$;
ROLLBACK;

-- cleanup
DELETE FROM public.classrooms WHERE id = 'c5500001-0000-0000-0000-000000000001';
DELETE FROM auth.users WHERE id IN ('b5500001-0000-0000-0000-000000000001', 'b5500002-0000-0000-0000-000000000002',
                                    'b5500003-0000-0000-0000-000000000003');

-- Behavioural assertions for migration 055 (public.users: the request roles
-- anon and authenticated hold no write privilege on the table or any column of
-- it, the own-row UPDATE policy is gone, and 044's guard stays as defense in
-- depth with a whole-row rule 1 and its unchanged rule 2, P0044), run BY HAND
-- against a LOCAL database — never CI, never the linked project (it seeds rows
-- and removes them at the end; T20 and T21 run inside transactions they roll
-- back).
--
-- The baseline replays on a fresh database as shipped, so a local stack of the
-- checkout itself (`supabase start`, or every migration applied in order by
-- psql) is the database: with every migration through 055 applied, then
--   psql <local db url> -f supabase/tests/055_users_identity_guard_assertions.sql
-- Denials are read with has_table_privilege / has_column_privilege and by a
-- PostgREST request simulated the way PostgREST runs it: SET LOCAL ROLE plus the
-- JWT claims as settings, with a plain table statement inside a DO block or a
-- pg_temp helper (044's pattern). No service-role-only function is called under
-- SET ROLE: the vision-quota writer (T18) is called as the migration owner, the
-- definer path (T17) through a throw-away function granted to authenticated.
-- 2026-11-11: run on PostgreSQL 16 with stub Supabase roles (anon,
-- authenticated, service_role), a stub auth schema, Supabase's default
-- privileges and the supabase_realtime publication, every migration 000 → 054
-- applied by psql, then 055: 36 PASS, 0 FAIL, 0 ERROR (also after 000 → 055 by
-- `supabase db push --include-all` 2.122.0). Against the database at 054 (or
-- after the 055 rollback): 18 PASS, 18 FAIL — T2, T3a, T3b, T4, T6, T7a–T7h, T9,
-- T10, T11b, T16 and T20a (the refused writes went through; T20a's re-grant then
-- meets 044's five-column guard, which lets a username through). The cycle 055 →
-- rollback → rollback → 055 → 055 passes each self-check and prints 36 PASS
-- after every apply, and after the rollback pg_dump -s equals the dump at 054.
-- Every other assertion file prints the same labels at 054 and at 055 except
-- 044's (below); 052_credit_ledger_assertions.sql, run its header's way with
-- 053–055 applied before the suite, prints 349 PASS with and without 055.
-- 055 BREAKS 044_users_protected_columns_assertions.sql's T7 and T8 by design
-- (T7 proves full_name was still the student's own, T8 that a student's write of
-- unchanged values went through; 055 leaves the student no UPDATE at all):
-- measured after 055, that file prints 13 PASS and FAIL T7, T8, T15 (T15 is
-- broken by 052, as its header says).
--
-- Expected: 36 PASS, 0 FAIL (T1..T21; T3a/T3b, T5a/T5b, T7a..T7h, T8a/T8b,
-- T11a/T11b, T13a..T13c, T15a/T15b, T20a/T20b).
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

-- `try_as_student` runs one statement as student s1 and answers its SQLSTATE
-- ('00000' = it changed a row, 'no-row' = it matched nothing).
CREATE OR REPLACE FUNCTION pg_temp.try_as_student(p_sql TEXT) RETURNS TEXT LANGUAGE plpgsql AS $$
DECLARE n INT;
BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  EXECUTE p_sql;
  GET DIAGNOSTICS n = ROW_COUNT;
  RESET ROLE;
  RETURN CASE WHEN n >= 1 THEN '00000' ELSE 'no-row' END;
EXCEPTION WHEN OTHERS THEN
  RETURN SQLSTATE;
END $$;

CREATE OR REPLACE FUNCTION pg_temp.student_sets(p_set TEXT) RETURNS TEXT LANGUAGE sql AS $$
  SELECT pg_temp.try_as_student('UPDATE public.users SET ' || p_set
                                || ' WHERE id = ''b5500001-0000-0000-0000-000000000001''');
$$;

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

-- `table_writes(role)`: the write privileges the role still holds on the table ('' = none).
CREATE OR REPLACE FUNCTION pg_temp.table_writes(p_role TEXT) RETURNS TEXT LANGUAGE sql AS $$
  SELECT coalesce(string_agg(pr, ','), '')
    FROM unnest(ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER']) AS pr
   WHERE has_table_privilege(p_role, 'public.users', pr);
$$;

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

\echo '=== T3 neither request role holds a write privilege on the table (has_table_privilege) ==='
SELECT CASE WHEN pg_temp.table_writes('anon') = '' THEN 'PASS T3a'
            ELSE 'FAIL T3a (' || pg_temp.table_writes('anon') || ')' END;
SELECT CASE WHEN pg_temp.table_writes('authenticated') = '' THEN 'PASS T3b'
            ELSE 'FAIL T3b (' || pg_temp.table_writes('authenticated') || ')' END;

\echo '=== T4 ... nor on any column of it (has_column_privilege, every column, both roles) ==='
SELECT CASE WHEN count(*) FILTER (WHERE has_column_privilege(r, 'public.users', a.attname, pr)) = 0
            THEN 'PASS T4'
            ELSE 'FAIL T4 (' || count(*) FILTER (WHERE has_column_privilege(r, 'public.users', a.attname, pr)) || ')' END
  FROM pg_attribute a,
       unnest(ARRAY['anon', 'authenticated']) AS r,
       unnest(ARRAY['INSERT', 'UPDATE', 'REFERENCES']) AS pr
 WHERE a.attrelid = 'public.users'::regclass AND a.attnum > 0 AND NOT a.attisdropped;

\echo '=== T5 reading stays as it was, and the service role keeps every privilege it uses ==='
SELECT CASE WHEN has_table_privilege('authenticated', 'public.users', 'SELECT') THEN 'PASS T5a' ELSE 'FAIL T5a' END;
SELECT CASE WHEN has_table_privilege('service_role', 'public.users', 'SELECT')
             AND has_table_privilege('service_role', 'public.users', 'INSERT')
             AND has_table_privilege('service_role', 'public.users', 'UPDATE')
             AND has_table_privilege('service_role', 'public.users', 'DELETE')
            THEN 'PASS T5b' ELSE 'FAIL T5b' END;

\echo '=== T6 the own-row UPDATE policy is gone, the read policy stays ==='
SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public' AND tablename = 'users' AND cmd <> 'SELECT')
             AND EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public' AND tablename = 'users'
                          AND policyname = 'Users read consolidated' AND cmd = 'SELECT')
            THEN 'PASS T6' ELSE 'FAIL T6' END;

\echo '=== T7 the student can change none of the eight columns 044 left open (42501 each) ==='
SELECT CASE WHEN pg_temp.student_sets('deletion_requested_at = now()') = '42501' THEN 'PASS T7a' ELSE 'FAIL T7a' END;
SELECT CASE WHEN pg_temp.student_sets('username = ''someone''') = '42501' THEN 'PASS T7b' ELSE 'FAIL T7b' END;
SELECT CASE WHEN pg_temp.student_sets('full_name = ''Anna B.''') = '42501' THEN 'PASS T7c' ELSE 'FAIL T7c' END;
SELECT CASE WHEN pg_temp.student_sets('email = ''x@evil.test''') = '42501' THEN 'PASS T7d' ELSE 'FAIL T7d' END;
SELECT CASE WHEN pg_temp.student_sets('created_by = NULL') = '42501' THEN 'PASS T7e' ELSE 'FAIL T7e' END;
SELECT CASE WHEN pg_temp.student_sets('vision_quota_per_term = 999') = '42501' THEN 'PASS T7f' ELSE 'FAIL T7f' END;
SELECT CASE WHEN pg_temp.student_sets('vision_used_per_term = 0, vision_quota_per_term = 1') = '42501' THEN 'PASS T7g' ELSE 'FAIL T7g' END;
SELECT CASE WHEN pg_temp.student_sets('created_at = now() - interval ''1 year''') = '42501' THEN 'PASS T7h' ELSE 'FAIL T7h' END;

\echo '=== T8 ... and 044''s five are still refused (role, hf_username) ==='
SELECT CASE WHEN pg_temp.student_sets('role = ''admin''') = '42501' THEN 'PASS T8a' ELSE 'FAIL T8a' END;
SELECT CASE WHEN pg_temp.student_sets('hf_username = ''other-hf''') = '42501' THEN 'PASS T8b' ELSE 'FAIL T8b' END;

\echo '=== T9 even a write that changes nothing is refused now (no UPDATE privilege at all) ==='
SELECT CASE WHEN pg_temp.student_sets('full_name = full_name, deletion_requested_at = deletion_requested_at') = '42501'
            THEN 'PASS T9' ELSE 'FAIL T9' END;

\echo '=== T10 nothing above changed the row ==='
SELECT CASE WHEN role = 'student' AND username = 'anna055' AND full_name = 'Anna'
                 AND email = 's1-055@x.test' AND created_by = 'b5500002-0000-0000-0000-000000000002'
                 AND deletion_requested_at IS NULL AND vision_quota_per_term IS NULL
                 AND vision_used_per_term = 0 AND hf_username IS NULL
            THEN 'PASS T10' ELSE 'FAIL T10' END
  FROM public.users WHERE id = 'b5500001-0000-0000-0000-000000000001';

\echo '=== T11 the student can neither insert a users row nor delete their own ==='
SELECT CASE WHEN pg_temp.try_as_student(
  'INSERT INTO public.users (id, email, training_credits) VALUES (''b5500009-0000-0000-0000-000000000009'', ''x@x.test'', 0)')
  = '42501' THEN 'PASS T11a' ELSE 'FAIL T11a' END;
SELECT CASE WHEN pg_temp.try_as_student('DELETE FROM public.users WHERE id = ''b5500001-0000-0000-0000-000000000001''')
  = '42501' THEN 'PASS T11b' ELSE 'FAIL T11b' END;

\echo '=== T12 /me/delete: the cloud API (service_role) records a request ==='
SELECT CASE WHEN pg_temp.try_as_service('deletion_requested_at = now()') = '00000' THEN 'PASS T12' ELSE 'FAIL T12' END;

\echo '=== T13 the API (service_role) still writes the identity columns: withdraw, rename, provisioning ==='
SELECT CASE WHEN pg_temp.try_as_service('deletion_requested_at = NULL') = '00000' THEN 'PASS T13a' ELSE 'FAIL T13a' END;
SELECT CASE WHEN pg_temp.try_as_service('full_name = ''Anna Berg'', username = ''anna055b''') = '00000'
            THEN 'PASS T13b' ELSE 'FAIL T13b' END;
SELECT CASE WHEN pg_temp.try_as_service(
  'email = ''anna055b@edubotics.local'', created_by = ''b5500002-0000-0000-0000-000000000002'', '
  || 'vision_quota_per_term = 5, vision_used_per_term = 2') = '00000'
            THEN 'PASS T13c' ELSE 'FAIL T13c' END;

\echo '=== T14 ... and the row now carries exactly what the service role wrote ==='
SELECT CASE WHEN deletion_requested_at IS NULL AND full_name = 'Anna Berg' AND username = 'anna055b'
                 AND email = 'anna055b@edubotics.local' AND vision_quota_per_term = 5 AND vision_used_per_term = 2
            THEN 'PASS T14' ELSE 'FAIL T14' END
  FROM public.users WHERE id = 'b5500001-0000-0000-0000-000000000001';

\echo '=== T15 rule 2 (P0044) is unchanged: with a stored token not even the service role sets another name ==='
INSERT INTO public.user_hf_credentials (user_id, token_ciphertext, token_fp, token_hint, hf_username, token_role, validated_at)
VALUES ('b5500001-0000-0000-0000-000000000001',
        'v1.3b28fa93.oKGio6Slpqeoqaqrjn4jTCSqY94DBOayZhuhvxHNOHHz1iMN_W9H5x7KFGCzFyaezvqdnUay92eVBeK79YNbhgY',
        'c1770a7966b0771e', 'hf_…aaaa', 'anna-proven', 'write', now())
ON CONFLICT (user_id) DO NOTHING;
SELECT CASE WHEN pg_temp.try_as_service('hf_username = ''self-asserted''') = 'P0044' THEN 'PASS T15a' ELSE 'FAIL T15a' END;
SELECT CASE WHEN pg_temp.try_as_service('hf_username = ''anna-proven''') = '00000' THEN 'PASS T15b' ELSE 'FAIL T15b' END;
DELETE FROM public.user_hf_credentials WHERE user_id = 'b5500001-0000-0000-0000-000000000001';

\echo '=== T16 the teacher (also authenticated) can change none of their own columns either ==='
DO $$
BEGIN
  PERFORM set_config('request.jwt.claim.sub', 'b5500002-0000-0000-0000-000000000002', true),
          set_config('request.jwt.claims', '{"sub":"b5500002-0000-0000-0000-000000000002","role":"authenticated"}', true);
  SET LOCAL ROLE authenticated;
  UPDATE public.users SET full_name = 'Herr Lehrer' WHERE id = 'b5500002-0000-0000-0000-000000000002';
  RAISE NOTICE 'FAIL T16 (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T16 (42501)'; END $$;

\echo '=== T17 a SECURITY DEFINER function runs as its owner: it still writes, even called by a student ==='
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
  IF n = 1 THEN RAISE NOTICE 'PASS T17 (1 row)'; ELSE RAISE NOTICE 'FAIL T17 (% rows)', n; END IF;
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T17 (%: %)', SQLSTATE, SQLERRM; END $$;
DROP FUNCTION public._t055_definer_set_name(uuid, text);

\echo '=== T18 the baseline vision-quota writer (SECURITY DEFINER) still resets the counter (called as its owner) ==='
DO $$
DECLARE v_used INT;
BEGIN
  PERFORM public.reset_vision_quota_used();
  SELECT vision_used_per_term INTO v_used FROM public.users WHERE id = 'b5500001-0000-0000-0000-000000000001';
  IF v_used = 0 THEN RAISE NOTICE 'PASS T18 (0)'; ELSE RAISE NOTICE 'FAIL T18 (%)', v_used; END IF;
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T18 (%: %)', SQLSTATE, SQLERRM; END $$;

\echo '=== T19 the student''s own row is still readable ==='
DO $$
DECLARE n INT;
BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  SELECT count(*) INTO n FROM public.users WHERE id = 'b5500001-0000-0000-0000-000000000001';
  IF n = 1 THEN RAISE NOTICE 'PASS T19 (1 row)'; ELSE RAISE NOTICE 'FAIL T19 (% rows)', n; END IF;
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T19 (%: %)', SQLSTATE, SQLERRM; END $$;

\echo '=== T20 defense in depth: with UPDATE granted back by mistake, the guard still refuses any change ==='
-- Rolled back: the grant and 024's own-row policy as they were before 055.
BEGIN;
GRANT UPDATE ON public.users TO authenticated;
CREATE POLICY t055_update_own ON public.users FOR UPDATE TO authenticated
  USING (id = (SELECT auth.uid())) WITH CHECK (id = (SELECT auth.uid()));
DO $$
BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  UPDATE public.users SET username = 'someone' WHERE id = 'b5500001-0000-0000-0000-000000000001';
  RAISE NOTICE 'FAIL T20a (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN
  IF SQLERRM LIKE '%EduBotics-Server%' THEN RAISE NOTICE 'PASS T20a (the guard: %)', SQLERRM;
  ELSE RAISE NOTICE 'FAIL T20a (not the guard: %)', SQLERRM; END IF;
END $$;
DO $$
DECLARE n INT;
BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  UPDATE public.users SET username = username WHERE id = 'b5500001-0000-0000-0000-000000000001';
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n = 1 THEN RAISE NOTICE 'PASS T20b (no change passes the guard, so the refusal above was the guard''s)';
  ELSE RAISE NOTICE 'FAIL T20b (% rows)', n; END IF;
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T20b (%: %)', SQLSTATE, SQLERRM; END $$;
ROLLBACK;

\echo '=== T21 an ON DELETE SET NULL action runs as the table owner: a request-role delete of the creator nulls created_by ==='
-- Rolled back: a throw-away DELETE grant and policy let the creator
-- (authenticated) delete their own users row; the RI action then UPDATEs the
-- student's created_by. Had it run as authenticated, it would hold no UPDATE
-- privilege and the guard would refuse it as well.
BEGIN;
UPDATE public.users SET created_by = 'b5500003-0000-0000-0000-000000000003'
 WHERE id = 'b5500001-0000-0000-0000-000000000001';
GRANT DELETE ON public.users TO authenticated;
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
  IF v_by IS NULL THEN RAISE NOTICE 'PASS T21 (created_by NULL)'; ELSE RAISE NOTICE 'FAIL T21 (%)', v_by; END IF;
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T21 (%: %)', SQLSTATE, SQLERRM; END $$;
ROLLBACK;

-- cleanup
DELETE FROM public.classrooms WHERE id = 'c5500001-0000-0000-0000-000000000001';
DELETE FROM auth.users WHERE id IN ('b5500001-0000-0000-0000-000000000001', 'b5500002-0000-0000-0000-000000000002',
                                    'b5500003-0000-0000-0000-000000000003');

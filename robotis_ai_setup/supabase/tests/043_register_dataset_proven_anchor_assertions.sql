-- Behavioural assertions for migration 043 (register_dataset_safe accepts the
-- proven account name of the student's stored token as a second HF-author
-- anchor), run BY HAND against a fresh LOCAL database — never CI, never the
-- linked project (it seeds rows).
--
-- The squashed baseline does NOT replay on a fresh database as shipped
-- (docs/KNOWN-ISSUES.md, „baseline.sql is NOT replayable from scratch"): run
-- it in a SCRATCH copy of supabase/ whose baseline has its one
-- `RENAME COLUMN runpod_job_id` line guarded exactly as
-- 040_code_programs_assertions.sql's header shows, with every migration
-- through 045, then
--   supabase start   (in the scratch copy; it applies the migrations)
--   psql <local db url> < supabase/tests/043_register_dataset_proven_anchor_assertions.sql
-- (2026-10-04 it was run against public.ecr.aws/supabase/postgres:17.6.1.106
-- with the migrations applied in order by psql as `postgres`.)
-- Expected: 12 PASS, 0 FAIL/ERROR (T1..T11, T2 with a/b halves).
\set ON_ERROR_STOP off
\pset pager off

INSERT INTO auth.users (id, instance_id, aud, role, email, encrypted_password, created_at, updated_at)
VALUES ('b4300001-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 's1-043@x.test', '', now(), now()),
       ('b4300002-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 's2-043@x.test', '', now(), now())
ON CONFLICT (id) DO NOTHING;

\echo '=== T1 exactly one register_dataset_safe: the 026 signature, SECURITY DEFINER, pinned search_path ==='
SELECT CASE WHEN count(*) = 1 AND bool_and(prosecdef)
                 AND bool_and(prorettype = 'public.datasets'::regtype)
                 AND bool_and(proconfig::text LIKE '%search_path=public, pg_catalog%')
                 AND bool_and(pg_get_function_identity_arguments(oid)
                     = 'p_user_id uuid, p_hf_repo_id text, p_hf_author text, p_workgroup_id uuid, p_name text, p_description text, p_episode_count integer, p_total_frames bigint, p_fps integer, p_robot_type text')
            THEN 'PASS T1' ELSE 'FAIL T1 (' || count(*) || ' rows)' END
  FROM pg_proc WHERE proname = 'register_dataset_safe' AND pronamespace = 'public'::regnamespace;

\echo '=== T2 grants unchanged: service_role only ==='
SELECT CASE WHEN NOT has_function_privilege('anon', 'public.register_dataset_safe(uuid,text,text,uuid,text,text,integer,bigint,integer,text)', 'EXECUTE')
                 AND NOT has_function_privilege('authenticated', 'public.register_dataset_safe(uuid,text,text,uuid,text,text,integer,bigint,integer,text)', 'EXECUTE')
                 AND NOT EXISTS (SELECT 1 FROM pg_proc p, LATERAL aclexplode(p.proacl) a
                                  WHERE p.oid = 'public.register_dataset_safe(uuid,text,text,uuid,text,text,integer,bigint,integer,text)'::regprocedure
                                    AND a.grantee = 0)
            THEN 'PASS T2a' ELSE 'FAIL T2a' END;
SELECT CASE WHEN has_function_privilege('service_role', 'public.register_dataset_safe(uuid,text,text,uuid,text,text,integer,bigint,integer,text)', 'EXECUTE')
            THEN 'PASS T2b' ELSE 'FAIL T2b' END;

\echo '=== T3 a first registration with no credential accepts the author (026 unchanged) ==='
SELECT CASE WHEN (public.register_dataset_safe('b4300001-0000-0000-0000-000000000001', 'old-acct/repo1', 'old-acct',
                  NULL, 'repo1', '', 1, 10, 30, 'omx_f')).hf_repo_id = 'old-acct/repo1'
            THEN 'PASS T3' ELSE 'FAIL T3' END;

\echo '=== T4 without a credential, a foreign author is still the IDOR block (P0034) ==='
DO $$ BEGIN
  PERFORM public.register_dataset_safe('b4300001-0000-0000-0000-000000000001', 'new-acct/repo2', 'new-acct',
                                       NULL, 'repo2', '', 1, 10, 30, 'omx_f');
  RAISE NOTICE 'FAIL T4 (no error)';
EXCEPTION WHEN SQLSTATE 'P0034' THEN RAISE NOTICE 'PASS T4 (P0034)'; END $$;

-- the student saves a token for another Hugging Face account (042 writer)
SELECT public.store_user_hf_credential(
         'b4300001-0000-0000-0000-000000000001',
         'v1.3b28fa93.oKGio6Slpqeoqaqrjn4jTCSqY94DBOayZhuhvxHNOHHz1iMN_W9H5x7KFGCzFyaezvqdnUay92eVBeK79YNbhgY',
         'c1770a7966b0771e', 'hf_…aaaa', 'new-acct', 'write') IS NOT NULL AS stored;

\echo '=== T5 the proven account is a second anchor: new-acct/* registers ==='
SELECT CASE WHEN (public.register_dataset_safe('b4300001-0000-0000-0000-000000000001', 'new-acct/repo2', 'new-acct',
                  NULL, 'repo2', '', 1, 10, 30, 'omx_f')).hf_repo_id = 'new-acct/repo2'
            THEN 'PASS T5' ELSE 'FAIL T5' END;

\echo '=== T6 ... case-insensitively, as 026 compares ==='
SELECT CASE WHEN (public.register_dataset_safe('b4300001-0000-0000-0000-000000000001', 'NEW-ACCT/repo3', 'NEW-ACCT',
                  NULL, 'repo3', '', 1, 10, 30, 'omx_f')).hf_repo_id = 'NEW-ACCT/repo3'
            THEN 'PASS T6' ELSE 'FAIL T6' END;

\echo '=== T7 the oldest-row anchor still counts ==='
SELECT CASE WHEN (public.register_dataset_safe('b4300001-0000-0000-0000-000000000001', 'old-acct/repo4', 'old-acct',
                  NULL, 'repo4', '', 1, 10, 30, 'omx_f')).hf_repo_id = 'old-acct/repo4'
            THEN 'PASS T7' ELSE 'FAIL T7' END;

\echo '=== T8 an author matching neither anchor is refused (P0034) ==='
DO $$ BEGIN
  PERFORM public.register_dataset_safe('b4300001-0000-0000-0000-000000000001', 'peer/repo5', 'peer',
                                       NULL, 'repo5', '', 1, 10, 30, 'omx_f');
  RAISE NOTICE 'FAIL T8 (no error)';
EXCEPTION WHEN SQLSTATE 'P0034' THEN RAISE NOTICE 'PASS T8 (P0034)'; END $$;

\echo '=== T9 ANOTHER student''s proven account opens nothing for me ==='
SELECT public.register_dataset_safe('b4300002-0000-0000-0000-000000000002', 'mallory/first', 'mallory',
                                    NULL, 'first', '', 1, 10, 30, 'omx_f') IS NOT NULL AS s2_anchor;
SELECT public.store_user_hf_credential(
         'b4300002-0000-0000-0000-000000000002',
         'v1.3b28fa93.oKGio6Slpqeoqaqrjn4jTCSqY94DBOayZhuhvxHNOHHz1iMN_W9H5x7KFGCzFyaezvqdnUay92eVBeK79YNbhgY',
         'c1770a7966b0771e', 'hf_…aaaa', 'mallory', 'write') IS NOT NULL AS s2_stored;
DO $$ BEGIN
  PERFORM public.register_dataset_safe('b4300001-0000-0000-0000-000000000001', 'mallory/stolen', 'mallory',
                                       NULL, 'stolen', '', 1, 10, 30, 'omx_f');
  RAISE NOTICE 'FAIL T9 (no error)';
EXCEPTION WHEN SQLSTATE 'P0034' THEN RAISE NOTICE 'PASS T9 (P0034)'; END $$;

\echo '=== T10 an unknown user is still P0002 before anything else ==='
DO $$ BEGIN
  PERFORM public.register_dataset_safe('00000000-0000-0000-0000-000000000000', '_probe/_probe', '_probe',
                                       NULL, '_probe', '', NULL, NULL, NULL, NULL);
  RAISE NOTICE 'FAIL T10 (no error)';
EXCEPTION WHEN SQLSTATE 'P0002' THEN RAISE NOTICE 'PASS T10 (P0002)'; END $$;

\echo '=== T11 the definer owner can read user_hf_credentials (RLS on, no policy): superuser or BYPASSRLS ==='
SELECT CASE WHEN r.rolsuper OR r.rolbypassrls THEN 'PASS T11' ELSE 'FAIL T11 (owner ' || r.rolname || ')' END
  FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner
 WHERE p.oid = 'public.register_dataset_safe(uuid,text,text,uuid,text,text,integer,bigint,integer,text)'::regprocedure;

-- cleanup (credentials and users cascade; datasets go with their owner)
DELETE FROM public.datasets WHERE owner_user_id IN ('b4300001-0000-0000-0000-000000000001', 'b4300002-0000-0000-0000-000000000002');
DELETE FROM auth.users WHERE id IN ('b4300001-0000-0000-0000-000000000001', 'b4300002-0000-0000-0000-000000000002');

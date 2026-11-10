-- Behavioural assertions for migration 053 (the classroom Jetson is retired:
-- public.jetsons, its 11 RPCs and public.inference_runs.jetson_id are gone;
-- public.inference_runs, its rows and its backend CHECK stay), run BY HAND
-- against a LOCAL database — never CI, never the linked project (it seeds rows
-- inside a transaction it rolls back).
--
-- The baseline replays on a fresh database as shipped, so a local stack of the
-- checkout itself (`supabase start`, or every migration applied in order by
-- psql) is the database: with every migration through 053 applied, then
--   psql <local db url> -f supabase/tests/053_retire_jetson_assertions.sql
-- Only catalog reads and privilege checks run here; no function runs under a
-- request role (calling a function the role may not execute crashes the
-- stand-alone supabase/postgres backend, signal 11; SR-25).
-- 2026-11-09: run on PostgreSQL 16.15 with stub Supabase roles (anon,
-- authenticated, service_role), a stub auth schema and the supabase_realtime
-- publication, every migration 000 → 052 applied by psql, then 053, the 053
-- rollback, 053 again: 12 PASS, 0 FAIL. Against the database BEFORE 053
-- (or after the rollback) T1, T2, T3, T4, T6 and T7 fail.
--
-- Expected: 12 PASS, 0 FAIL (T1..T12).
\set ON_ERROR_STOP off
\pset pager off

\echo '=== T1 public.jetsons is gone ==='
SELECT CASE WHEN to_regclass('public.jetsons') IS NULL THEN 'PASS T1' ELSE 'FAIL T1' END;

\echo '=== T2 none of the 11 Jetson RPCs exists, under any argument list ==='
SELECT CASE WHEN count(*) = 0 THEN 'PASS T2' ELSE 'FAIL T2 (' || string_agg(p.proname, ', ') || ')' END
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'public'
   AND p.proname IN ('claim_jetson', 'release_jetson', 'heartbeat_jetson', 'agent_heartbeat_jetson',
                     'pair_jetson', 'claim_pair_intent', 'force_release_jetson', 'sweep_jetson_locks',
                     'agent_release_jetson', 'regenerate_pairing_code', 'unpair_jetson');

\echo '=== T3 the jetsons row type is gone ==='
SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
                              WHERE n.nspname = 'public' AND t.typname = 'jetsons')
            THEN 'PASS T3' ELSE 'FAIL T3' END;

\echo '=== T4 public.inference_runs.jetson_id is gone ==='
SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM information_schema.columns
                              WHERE table_schema = 'public' AND table_name = 'inference_runs'
                                AND column_name = 'jetson_id')
            THEN 'PASS T4' ELSE 'FAIL T4' END;

\echo '=== T5 public.inference_runs stays, with its backend CHECK naming jetson and cloud ==='
SELECT CASE WHEN pg_get_constraintdef(c.oid) LIKE '%''jetson''%' AND pg_get_constraintdef(c.oid) LIKE '%''cloud''%'
            THEN 'PASS T5' ELSE 'FAIL T5 (' || pg_get_constraintdef(c.oid) || ')' END
  FROM pg_constraint c
 WHERE c.conrelid = 'public.inference_runs'::regclass AND c.conname = 'inference_runs_backend_check';

\echo '=== T6 no policy, trigger or index names jetsons any more ==='
SELECT CASE WHEN (SELECT count(*) FROM pg_policies WHERE tablename = 'jetsons') = 0
             AND (SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_jetsons_touch') = 0
             AND (SELECT count(*) FROM pg_indexes WHERE indexname LIKE 'idx_jetsons_%') = 0
            THEN 'PASS T6' ELSE 'FAIL T6' END;

\echo '=== T7 supabase_realtime publishes no jetsons table ==='
SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM pg_publication_tables WHERE tablename = 'jetsons')
            THEN 'PASS T7' ELSE 'FAIL T7' END;

\echo '=== T8 the shared touch_updated_at() stays and still drives other tables ==='
SELECT CASE WHEN to_regprocedure('public.touch_updated_at()') IS NOT NULL
             AND (SELECT count(*) FROM pg_trigger WHERE tgfoid = 'public.touch_updated_at()'::regprocedure) > 0
            THEN 'PASS T8' ELSE 'FAIL T8' END;

\echo '=== T9 service_role still reads and writes inference_runs ==='
SELECT CASE WHEN has_table_privilege('service_role', 'public.inference_runs', 'SELECT')
             AND has_table_privilege('service_role', 'public.inference_runs', 'INSERT')
             AND has_table_privilege('service_role', 'public.inference_runs', 'DELETE')
            THEN 'PASS T9' ELSE 'FAIL T9' END;

\echo '=== T10-T12 a run row of either backend is still accepted, another one refused (rolled back) ==='
BEGIN;
INSERT INTO auth.users (id, instance_id, aud, role, email, encrypted_password, created_at, updated_at, raw_app_meta_data)
VALUES ('05300001-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000000', 'authenticated',
        'authenticated', 't053@edubotics.local', '', now(), now(), '{"edubotics_provisioned": true}');
SAVEPOINT s1;
DO $$
BEGIN
  INSERT INTO public.inference_runs (student_user_id, policy_repo, exit_reason, backend)
  VALUES ('05300001-0000-0000-0000-000000000001', 'EduBotics-Solutions/m', 'finished', 'jetson');
  RAISE NOTICE 'PASS T10';
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T10 (%)', SQLERRM;
END $$;
DO $$
BEGIN
  INSERT INTO public.inference_runs (student_user_id, policy_repo, exit_reason, backend)
  VALUES ('05300001-0000-0000-0000-000000000001', 'EduBotics-Solutions/m', 'stopped', 'cloud');
  RAISE NOTICE 'PASS T11';
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T11 (%)', SQLERRM;
END $$;
DO $$
BEGIN
  INSERT INTO public.inference_runs (student_user_id, policy_repo, exit_reason, backend)
  VALUES ('05300001-0000-0000-0000-000000000001', 'EduBotics-Solutions/m', 'error', 'gpu');
  RAISE NOTICE 'FAIL T12 (an unknown backend was accepted)';
EXCEPTION WHEN check_violation THEN RAISE NOTICE 'PASS T12';
END $$;
ROLLBACK;

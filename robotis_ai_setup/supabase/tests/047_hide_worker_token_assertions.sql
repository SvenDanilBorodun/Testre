-- Behavioural assertions for migration 047 (students can no longer read
-- public.trainings.worker_token: anon and authenticated hold a COLUMN-level
-- SELECT on every trainings column except worker_token, and no table-level
-- SELECT), run BY HAND against a fresh LOCAL database — never CI, never the
-- linked project (it seeds rows).
--
-- The squashed baseline does NOT replay on a fresh database as shipped
-- (docs/KNOWN-ISSUES.md, „baseline.sql is NOT replayable from scratch"): run
-- it in a SCRATCH copy of supabase/ whose baseline has its one
-- `RENAME COLUMN runpod_job_id` line guarded exactly as
-- 040_code_programs_assertions.sql's header shows, with every migration
-- through 048 applied by `supabase start`, then 047 with psql as `postgres`:
--   psql <local db url> -v ON_ERROR_STOP=1 -f supabase/migrations/20261020120000_047_hide_worker_token.sql
--   psql <local db url> -f supabase/tests/047_hide_worker_token_assertions.sql
-- The same file passes unchanged after 049 too (049 grants its own columns by
-- name). A PostgREST request is simulated the way PostgREST runs it: SET LOCAL
-- ROLE plus the JWT claims as settings. Only TABLE reads run under a request
-- role here; function privileges are asserted with has_function_privilege —
-- calling a function the role may not execute crashes the stand-alone
-- supabase/postgres:17.6.1.106 backend (signal 11, measured; SR-25).
-- Expected: 10 PASS, 0 FAIL (T1..T10). Against a database WITHOUT 047, T1, T3,
-- T4, T5 and T6 fail (the token is readable there).
\set ON_ERROR_STOP off
\pset pager off

INSERT INTO auth.users (id, instance_id, aud, role, email, encrypted_password, created_at, updated_at)
VALUES ('b4700001-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 's1-047@x.test', '', now(), now()),
       ('b4700002-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 's2-047@x.test', '', now(), now())
ON CONFLICT (id) DO NOTHING;
UPDATE public.users SET role = 'student' WHERE id IN ('b4700001-0000-0000-0000-000000000001', 'b4700002-0000-0000-0000-000000000002');

-- one running training per student, seeded as the table owner (RLS does not apply to it)
INSERT INTO public.trainings (id, user_id, dataset_name, model_name, model_type, status, worker_token, total_steps)
VALUES (947001, 'b4700001-0000-0000-0000-000000000001', 's1/wuerfel', 'EduBotics-Solutions/m047a', 'act', 'running',
        'a4700001-0000-0000-0000-000000000001', 100),
       (947002, 'b4700002-0000-0000-0000-000000000002', 's2/wuerfel', 'EduBotics-Solutions/m047b', 'act', 'running',
        'a4700002-0000-0000-0000-000000000002', 100)
ON CONFLICT (id) DO NOTHING;

CREATE OR REPLACE FUNCTION pg_temp.claims_s1() RETURNS void LANGUAGE sql AS $$
  SELECT set_config('request.jwt.claim.sub', 'b4700001-0000-0000-0000-000000000001', true),
         set_config('request.jwt.claims', '{"sub":"b4700001-0000-0000-0000-000000000001","role":"authenticated"}', true);
$$;

\echo '=== T1 neither request role can read trainings.worker_token ==='
SELECT CASE WHEN NOT has_column_privilege('anon', 'public.trainings', 'worker_token', 'SELECT')
             AND NOT has_column_privilege('authenticated', 'public.trainings', 'worker_token', 'SELECT')
            THEN 'PASS T1' ELSE 'FAIL T1' END;

\echo '=== T2 both request roles still read every OTHER trainings column ==='
SELECT CASE WHEN count(*) FILTER (WHERE NOT has_column_privilege(r, 'public.trainings', c.column_name, 'SELECT')) = 0
             AND count(*) >= 2 * 20
            THEN 'PASS T2 (' || count(*) / 2 || ' columns)' ELSE 'FAIL T2' END
  FROM unnest(ARRAY['anon', 'authenticated']) r,
       information_schema.columns c
 WHERE c.table_schema = 'public' AND c.table_name = 'trainings' AND c.column_name <> 'worker_token';

\echo '=== T3 no table-level SELECT is left (a new column would be readable through it) ==='
SELECT CASE WHEN NOT has_table_privilege('anon', 'public.trainings', 'SELECT')
             AND NOT has_table_privilege('authenticated', 'public.trainings', 'SELECT')
            THEN 'PASS T3' ELSE 'FAIL T3' END;

\echo '=== T4 a student SELECTing worker_token gets 42501 ==='
BEGIN;
SET LOCAL ROLE authenticated;
SELECT pg_temp.claims_s1();
DO $$ BEGIN
  PERFORM worker_token FROM public.trainings;
  RAISE NOTICE 'FAIL T4 (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T4 (%)', SQLERRM; END $$;
ROLLBACK;

\echo '=== T5 a filter on worker_token gets 42501 (no oracle by WHERE) ==='
BEGIN;
SET LOCAL ROLE authenticated;
SELECT pg_temp.claims_s1();
DO $$ BEGIN
  PERFORM id FROM public.trainings WHERE worker_token = 'a4700001-0000-0000-0000-000000000001';
  RAISE NOTICE 'FAIL T5 (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T5 (%)', SQLERRM; END $$;
ROLLBACK;

\echo '=== T6 an order on worker_token gets 42501 ==='
BEGIN;
SET LOCAL ROLE authenticated;
SELECT pg_temp.claims_s1();
DO $$ BEGIN
  PERFORM id FROM public.trainings ORDER BY worker_token;
  RAISE NOTICE 'FAIL T6 (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T6 (%)', SQLERRM; END $$;
ROLLBACK;

\echo '=== T7 the SPA read (LIVE_COLUMNS) still answers, and only the own row (RLS unchanged) ==='
BEGIN;
SET LOCAL ROLE authenticated;
SELECT pg_temp.claims_s1();
SELECT CASE WHEN count(*) = 1 AND bool_and(id = 947001) THEN 'PASS T7' ELSE 'FAIL T7 (' || count(*) || ')' END
  FROM (SELECT id, status, model_name, model_type, dataset_name, current_step, total_steps, current_loss,
               loss_history, error_message, requested_at, terminated_at, last_progress_at, workgroup_id
          FROM public.trainings WHERE id IN (947001, 947002)) q;
ROLLBACK;

\echo '=== T8 service_role keeps worker_token (the cloud API reads it) ==='
SELECT CASE WHEN has_column_privilege('service_role', 'public.trainings', 'worker_token', 'SELECT')
             AND has_table_privilege('service_role', 'public.trainings', 'SELECT')
            THEN 'PASS T8' ELSE 'FAIL T8' END;

\echo '=== T9 both request roles still EXECUTE update_training_progress (the worker path) ==='
SELECT CASE WHEN has_function_privilege('anon', 'public.update_training_progress(integer,uuid,text,integer,integer,real,text,text)', 'EXECUTE')
             AND has_function_privilege('authenticated', 'public.update_training_progress(integer,uuid,text,integer,integer,real,text,text)', 'EXECUTE')
            THEN 'PASS T9' ELSE 'FAIL T9' END;

\echo '=== T10 a worker with its token still reports progress (anon, the Modal path) ==='
BEGIN;
SET LOCAL ROLE anon;
SELECT public.update_training_progress(947001, 'a4700001-0000-0000-0000-000000000001', NULL, 7, NULL, 0.5, NULL, NULL);
RESET ROLE;
SELECT CASE WHEN current_step = 7 THEN 'PASS T10' ELSE 'FAIL T10 (' || current_step || ')' END
  FROM public.trainings WHERE id = 947001;
ROLLBACK;

DELETE FROM public.trainings WHERE id IN (947001, 947002);

-- Behavioural assertions for migration 046 (students write no table directly:
-- anon and authenticated hold no write privilege and no own-row write policy
-- on public.trainings, public.datasets, public.workflows and
-- public.tutorial_progress), run BY HAND against a fresh LOCAL database —
-- never CI, never the linked project (it seeds rows).
--
-- The squashed baseline does NOT replay on a fresh database as shipped
-- (docs/KNOWN-ISSUES.md, „baseline.sql is NOT replayable from scratch"): run
-- it in a SCRATCH copy of supabase/ whose baseline has its one
-- `RENAME COLUMN runpod_job_id` line guarded exactly as
-- 040_code_programs_assertions.sql's header shows, with every migration
-- through 046, then
--   supabase start   (in the scratch copy; it applies the migrations)
--   psql <local db url> < supabase/tests/046_close_direct_student_writes_assertions.sql
-- (2026-10-04 it was run against a real `supabase start` stack, Postgres 17,
-- public.ecr.aws/supabase/postgres:17.6.1.106, with the migrations applied in
-- order by psql as `postgres`.) A PostgREST request is simulated the way
-- PostgREST runs it: SET LOCAL ROLE plus the JWT claims as settings (both
-- spellings auth.uid() has read over the years). The Modal worker is
-- simulated as `anon`, which holds EXECUTE on update_training_progress.
-- Expected: 27 PASS, 0 FAIL/ERROR (T1..T15; T6, T7, T8 and T10 with a/b/c/d
-- halves, one per table).
\set ON_ERROR_STOP off
\pset pager off

INSERT INTO auth.users (id, instance_id, aud, role, email, encrypted_password, created_at, updated_at)
VALUES ('b4600001-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 's1-046@x.test', '', now(), now())
ON CONFLICT (id) DO NOTHING;

-- one own row per table, seeded as the table owner (RLS does not apply to it)
INSERT INTO public.trainings (id, user_id, dataset_name, model_name, model_type, status, worker_token)
VALUES (946001, 'b4600001-0000-0000-0000-000000000001', 's1/wuerfel', 'm046', 'act', 'queued',
        'a4600001-0000-0000-0000-000000000001')
ON CONFLICT (id) DO NOTHING;
INSERT INTO public.datasets (id, owner_user_id, hf_repo_id, name)
VALUES ('d4600001-0000-0000-0000-000000000001', 'b4600001-0000-0000-0000-000000000001', 's1/wuerfel', 'wuerfel')
ON CONFLICT (id) DO NOTHING;
INSERT INTO public.workflows (id, owner_user_id, name, blockly_json)
VALUES ('e4600001-0000-0000-0000-000000000001', 'b4600001-0000-0000-0000-000000000001', 'Programm 046', '{"v": 1}')
ON CONFLICT (id) DO NOTHING;
INSERT INTO public.tutorial_progress (user_id, tutorial_id, current_step)
VALUES ('b4600001-0000-0000-0000-000000000001', 'tut-046', 2)
ON CONFLICT (user_id, tutorial_id) DO NOTHING;

-- `claims_s1` sets the JWT claims of student s1 the way PostgREST does.
CREATE OR REPLACE FUNCTION pg_temp.claims_s1() RETURNS void LANGUAGE sql AS $$
  SELECT set_config('request.jwt.claim.sub', 'b4600001-0000-0000-0000-000000000001', true),
         set_config('request.jwt.claims', '{"sub":"b4600001-0000-0000-0000-000000000001","role":"authenticated"}', true);
$$;

\echo '=== T1 anon and authenticated hold no INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER on the four tables ==='
SELECT CASE WHEN count(*) FILTER (WHERE has_table_privilege(r, t, p)) = 0 AND count(*) = 48
            THEN 'PASS T1' ELSE 'FAIL T1 (' || count(*) FILTER (WHERE has_table_privilege(r, t, p)) || ' held)' END
  FROM unnest(ARRAY['anon', 'authenticated']) r,
       unnest(ARRAY['public.trainings', 'public.datasets', 'public.workflows', 'public.tutorial_progress']) t,
       unnest(ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER']) p;

\echo '=== T2 SELECT stays for both request roles, service_role keeps every write ==='
SELECT CASE WHEN bool_and(has_table_privilege('authenticated', t, 'SELECT'))
                 AND bool_and(has_table_privilege('anon', t, 'SELECT'))
                 AND bool_and(has_table_privilege('service_role', t, 'INSERT'))
                 AND bool_and(has_table_privilege('service_role', t, 'UPDATE'))
                 AND bool_and(has_table_privilege('service_role', t, 'DELETE'))
            THEN 'PASS T2' ELSE 'FAIL T2' END
  FROM unnest(ARRAY['public.trainings', 'public.datasets', 'public.workflows', 'public.tutorial_progress']) t;

\echo '=== T3 no write policy is left on the four tables ==='
SELECT CASE WHEN count(*) = 0 THEN 'PASS T3' ELSE 'FAIL T3 (' || string_agg(policyname, ', ') || ')' END
  FROM pg_policies
 WHERE schemaname = 'public' AND tablename IN ('trainings', 'datasets', 'workflows', 'tutorial_progress')
   AND cmd <> 'SELECT';

\echo '=== T4 the four consolidated SELECT policies are still there ==='
SELECT CASE WHEN count(*) = 4 THEN 'PASS T4' ELSE 'FAIL T4 (' || count(*) || ')' END
  FROM pg_policies
 WHERE schemaname = 'public' AND cmd = 'SELECT'
   AND (tablename, policyname) IN (('trainings', 'Trainings read consolidated'),
                                   ('datasets', 'Datasets read consolidated'),
                                   ('workflows', 'Workflows read consolidated'),
                                   ('tutorial_progress', 'Tutorial progress read consolidated'));

\echo '=== T5 the Realtime publication still carries all four ==='
SELECT CASE WHEN count(*) = 4 THEN 'PASS T5' ELSE 'FAIL T5 (' || count(*) || ')' END
  FROM pg_publication_tables
 WHERE pubname = 'supabase_realtime' AND schemaname = 'public'
   AND tablename IN ('trainings', 'datasets', 'workflows', 'tutorial_progress');

\echo '=== T6 a student cannot INSERT into any of them (42501) ==='
DO $$ BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  INSERT INTO public.trainings (user_id, dataset_name, model_name, model_type)
  VALUES ('b4600001-0000-0000-0000-000000000001', 's1/frei', 'gratis', 'act');
  RAISE NOTICE 'FAIL T6a (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T6a (%)', SQLERRM; END $$;
DO $$ BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  INSERT INTO public.datasets (owner_user_id, hf_repo_id, name)
  VALUES ('b4600001-0000-0000-0000-000000000001', 'someone-else/repo', 'fremd');
  RAISE NOTICE 'FAIL T6b (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T6b (%)', SQLERRM; END $$;
DO $$ BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  INSERT INTO public.workflows (owner_user_id, name, blockly_json)
  VALUES ('b4600001-0000-0000-0000-000000000001', 'direkt', '{}');
  RAISE NOTICE 'FAIL T6c (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T6c (%)', SQLERRM; END $$;
DO $$ BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  INSERT INTO public.tutorial_progress (user_id, tutorial_id, current_step)
  VALUES ('b4600001-0000-0000-0000-000000000001', 'tut-direkt', 9);
  RAISE NOTICE 'FAIL T6d (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T6d (%)', SQLERRM; END $$;

\echo '=== T7 ... nor UPDATE their own row (the free-GPU-time write: status -> canceled) ==='
DO $$ BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  UPDATE public.trainings SET status = 'canceled' WHERE id = 946001;
  RAISE NOTICE 'FAIL T7a (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T7a (%)', SQLERRM; END $$;
DO $$ BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  UPDATE public.datasets SET hf_repo_id = 'someone-else/repo' WHERE id = 'd4600001-0000-0000-0000-000000000001';
  RAISE NOTICE 'FAIL T7b (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T7b (%)', SQLERRM; END $$;
DO $$ BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  UPDATE public.workflows SET blockly_json = '{"v": 2}' WHERE id = 'e4600001-0000-0000-0000-000000000001';
  RAISE NOTICE 'FAIL T7c (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T7c (%)', SQLERRM; END $$;
DO $$ BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  UPDATE public.tutorial_progress SET current_step = 99 WHERE user_id = 'b4600001-0000-0000-0000-000000000001';
  RAISE NOTICE 'FAIL T7d (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T7d (%)', SQLERRM; END $$;

\echo '=== T8 ... nor DELETE it ==='
DO $$ BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  DELETE FROM public.trainings WHERE id = 946001;
  RAISE NOTICE 'FAIL T8a (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T8a (%)', SQLERRM; END $$;
DO $$ BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  DELETE FROM public.datasets WHERE id = 'd4600001-0000-0000-0000-000000000001';
  RAISE NOTICE 'FAIL T8b (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T8b (%)', SQLERRM; END $$;
DO $$ BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  DELETE FROM public.workflows WHERE id = 'e4600001-0000-0000-0000-000000000001';
  RAISE NOTICE 'FAIL T8c (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T8c (%)', SQLERRM; END $$;
DO $$ BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  DELETE FROM public.tutorial_progress WHERE user_id = 'b4600001-0000-0000-0000-000000000001';
  RAISE NOTICE 'FAIL T8d (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T8d (%)', SQLERRM; END $$;

\echo '=== T9 every seeded row is still there, unchanged ==='
SELECT CASE WHEN (SELECT status FROM public.trainings WHERE id = 946001) = 'queued'
                 AND (SELECT hf_repo_id FROM public.datasets WHERE id = 'd4600001-0000-0000-0000-000000000001') = 's1/wuerfel'
                 AND (SELECT blockly_json FROM public.workflows WHERE id = 'e4600001-0000-0000-0000-000000000001') = '{"v": 1}'::jsonb
                 AND (SELECT current_step FROM public.tutorial_progress
                       WHERE user_id = 'b4600001-0000-0000-0000-000000000001' AND tutorial_id = 'tut-046') = 2
                 AND (SELECT count(*) FROM public.trainings WHERE user_id = 'b4600001-0000-0000-0000-000000000001') = 1
                 AND (SELECT count(*) FROM public.datasets WHERE owner_user_id = 'b4600001-0000-0000-0000-000000000001') = 1
                 AND (SELECT count(*) FROM public.workflows WHERE owner_user_id = 'b4600001-0000-0000-0000-000000000001') = 1
                 AND (SELECT count(*) FROM public.tutorial_progress WHERE user_id = 'b4600001-0000-0000-0000-000000000001') = 1
            THEN 'PASS T9' ELSE 'FAIL T9' END;

\echo '=== T10 a student still READS their own row (the SPA trainings query, Realtime) ==='
DO $$ DECLARE n INT; BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  SELECT count(*) INTO n FROM public.trainings WHERE id = 946001;
  IF n = 1 THEN RAISE NOTICE 'PASS T10a';
  ELSE RAISE NOTICE 'FAIL T10a (% rows)', n; END IF;
END $$;
DO $$ DECLARE n INT; BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  SELECT count(*) INTO n FROM public.datasets WHERE id = 'd4600001-0000-0000-0000-000000000001';
  IF n = 1 THEN RAISE NOTICE 'PASS T10b';
  ELSE RAISE NOTICE 'FAIL T10b (% rows)', n; END IF;
END $$;
DO $$ DECLARE n INT; BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  SELECT count(*) INTO n FROM public.workflows WHERE id = 'e4600001-0000-0000-0000-000000000001';
  IF n = 1 THEN RAISE NOTICE 'PASS T10c';
  ELSE RAISE NOTICE 'FAIL T10c (% rows)', n; END IF;
END $$;
DO $$ DECLARE n INT; BEGIN
  PERFORM pg_temp.claims_s1();
  SET LOCAL ROLE authenticated;
  SELECT count(*) INTO n FROM public.tutorial_progress WHERE user_id = 'b4600001-0000-0000-0000-000000000001';
  IF n = 1 THEN RAISE NOTICE 'PASS T10d';
  ELSE RAISE NOTICE 'FAIL T10d (% rows)', n; END IF;
END $$;

\echo '=== T11 anon cannot INSERT either (42501) ==='
DO $$ BEGIN
  SET LOCAL ROLE anon;
  INSERT INTO public.trainings (user_id, dataset_name, model_name, model_type)
  VALUES ('b4600001-0000-0000-0000-000000000001', 's1/anon', 'anon', 'act');
  RAISE NOTICE 'FAIL T11 (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T11 (%)', SQLERRM; END $$;

\echo '=== T12 the service role (the cloud API) still inserts, updates and deletes all four ==='
DO $$ BEGIN
  SET LOCAL ROLE service_role;
  INSERT INTO public.trainings (id, user_id, dataset_name, model_name, model_type)
  VALUES (946002, 'b4600001-0000-0000-0000-000000000001', 's1/sr', 'sr', 'act');
  UPDATE public.trainings SET status = 'failed' WHERE id = 946002;
  DELETE FROM public.trainings WHERE id = 946002;
  INSERT INTO public.datasets (id, owner_user_id, hf_repo_id, name)
  VALUES ('d4600002-0000-0000-0000-000000000002', 'b4600001-0000-0000-0000-000000000001', 's1/sr', 'sr');
  UPDATE public.datasets SET description = 'x' WHERE id = 'd4600002-0000-0000-0000-000000000002';
  DELETE FROM public.datasets WHERE id = 'd4600002-0000-0000-0000-000000000002';
  INSERT INTO public.workflows (id, owner_user_id, name, blockly_json)
  VALUES ('e4600002-0000-0000-0000-000000000002', 'b4600001-0000-0000-0000-000000000001', 'sr', '{}');
  UPDATE public.workflows SET name = 'sr2' WHERE id = 'e4600002-0000-0000-0000-000000000002';
  DELETE FROM public.workflows WHERE id = 'e4600002-0000-0000-0000-000000000002';
  INSERT INTO public.tutorial_progress (user_id, tutorial_id) VALUES ('b4600001-0000-0000-0000-000000000001', 'tut-sr');
  UPDATE public.tutorial_progress SET current_step = 3 WHERE tutorial_id = 'tut-sr';
  DELETE FROM public.tutorial_progress WHERE tutorial_id = 'tut-sr';
  RAISE NOTICE 'PASS T12';
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T12 (% %)', SQLSTATE, SQLERRM; END $$;

\echo '=== T13 the Modal worker (anon key) still reports progress through update_training_progress ==='
DO $$ BEGIN
  IF NOT has_function_privilege('anon', 'public.update_training_progress(integer,uuid,text,integer,integer,real,text,text)', 'EXECUTE') THEN
    RAISE NOTICE 'FAIL T13 (anon lost EXECUTE)';  -- never call it as anon then
    RETURN;
  END IF;
  SET LOCAL ROLE anon;
  PERFORM public.update_training_progress(946001, 'a4600001-0000-0000-0000-000000000001', 'running', 5, 100, 0.5::real, NULL, NULL);
END $$;
SELECT CASE WHEN status = 'running' AND current_step = 5 AND total_steps = 100 AND jsonb_array_length(loss_history) = 1
            THEN 'PASS T13' ELSE 'FAIL T13 (' || status || ', ' || coalesce(current_step, -1) || ')' END
  FROM public.trainings WHERE id = 946001;

\echo '=== T14 a service-role save of a workflow is still versioned by the snapshot trigger ==='
DO $$ DECLARE before INT; after INT; BEGIN
  SELECT count(*) INTO before FROM public.workflow_versions WHERE workflow_id = 'e4600001-0000-0000-0000-000000000001';
  SET LOCAL ROLE service_role;
  UPDATE public.workflows SET blockly_json = '{"v": 3}' WHERE id = 'e4600001-0000-0000-0000-000000000001';
  RESET ROLE;
  SELECT count(*) INTO after FROM public.workflow_versions WHERE workflow_id = 'e4600001-0000-0000-0000-000000000001';
  IF after = before + 1 THEN RAISE NOTICE 'PASS T14 (% -> % versions)', before, after;
  ELSE RAISE NOTICE 'FAIL T14 (% -> % versions)', before, after; END IF;
END $$;

\echo '=== T15 register_dataset_safe (service role) still registers a dataset ==='
DO $$ DECLARE r public.datasets; BEGIN
  SET LOCAL ROLE service_role;
  r := public.register_dataset_safe('b4600001-0000-0000-0000-000000000001', 's1/zweiter', 's1',
                                    NULL, 'zweiter', '', 1, 10, 30, 'omx_f');
  IF r.hf_repo_id = 's1/zweiter' THEN RAISE NOTICE 'PASS T15';
  ELSE RAISE NOTICE 'FAIL T15 (%)', r.hf_repo_id; END IF;
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T15 (% %)', SQLSTATE, SQLERRM; END $$;

-- cleanup (every row hangs off the seeded user: trainings since 038, the
-- rest since their tables were created, cascade with it)
DELETE FROM public.workflow_versions WHERE workflow_id = 'e4600001-0000-0000-0000-000000000001';
DELETE FROM public.trainings WHERE user_id = 'b4600001-0000-0000-0000-000000000001';
DELETE FROM public.datasets WHERE owner_user_id = 'b4600001-0000-0000-0000-000000000001';
DELETE FROM public.workflows WHERE owner_user_id = 'b4600001-0000-0000-0000-000000000001';
DELETE FROM auth.users WHERE id = 'b4600001-0000-0000-0000-000000000001';

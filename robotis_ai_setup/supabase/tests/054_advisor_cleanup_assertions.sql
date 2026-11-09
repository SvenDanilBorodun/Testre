-- Behavioural assertions for migration 054 (the advisor clean-up: a fixed
-- search_path on credit_ledger_immutable() and trainings_guard_credit_columns(),
-- EXECUTE revoked from PUBLIC, anon and authenticated on the two prune trigger
-- functions), run BY HAND against a LOCAL database — never CI, never the linked
-- project (it seeds rows inside a transaction it rolls back).
--
-- The baseline replays on a fresh database as shipped, so a local stack of the
-- checkout itself (`supabase start`, or every migration applied in order by
-- psql) is the database: with every migration through 054 applied, then
--   psql <local db url> -f supabase/tests/054_advisor_cleanup_assertions.sql
-- Denials are read with has_function_privilege and aclexplode; no function runs
-- under a request role. Triggers DO fire under one (T10, T12-T15): a trigger
-- function's EXECUTE is checked when the trigger is created, never when it
-- fires, which is the property this file proves for the revoked prune functions.
-- A request role is simulated the way PostgREST runs it, SET LOCAL ROLE, with a
-- plain table statement inside a DO block (044's pattern).
-- 2026-11-10: run on PostgreSQL 16.15 with stub Supabase roles (anon,
-- authenticated, service_role), a stub auth schema and the supabase_realtime
-- publication, every migration 000 → 053 applied by psql (and, separately, by
-- `supabase db push` 2.122.0), then 054: 15 PASS, 0 FAIL, 0 ERROR. Against the
-- database BEFORE 054 (or after its rollback) T1, T2, T4 and T5 fail, and so do
-- T14 and T15, which refuse to count while authenticated still holds EXECUTE.
-- The cycle 054 → rollback → rollback → 054 → 054 passes each self-check, and
-- after the rollback pg_dump -s equals the dump at 053.
-- 052_credit_ledger_assertions.sql (349 PASS) and 053_retire_jetson_assertions.sql
-- (12 PASS) print the same results with and without 054.
--
-- Expected: 15 PASS, 0 FAIL (T1..T15).
\set ON_ERROR_STOP off
\pset pager off

\echo '=== T1 credit_ledger_immutable() has a fixed search_path and stays SECURITY INVOKER ==='
SELECT CASE WHEN proconfig = '{search_path=public}' AND NOT prosecdef THEN 'PASS T1'
            ELSE 'FAIL T1 (' || coalesce(proconfig::text, 'no proconfig') || ', definer=' || prosecdef || ')' END
  FROM pg_proc WHERE oid = 'public.credit_ledger_immutable()'::regprocedure;

\echo '=== T2 trainings_guard_credit_columns() has a fixed search_path and stays SECURITY INVOKER (044 lesson) ==='
SELECT CASE WHEN proconfig = '{search_path=public}' AND NOT prosecdef THEN 'PASS T2'
            ELSE 'FAIL T2 (' || coalesce(proconfig::text, 'no proconfig') || ', definer=' || prosecdef || ')' END
  FROM pg_proc WHERE oid = 'public.trainings_guard_credit_columns()'::regprocedure;

\echo '=== T3 neither 052 trigger function is executable by anon, authenticated or service_role (052, unchanged) ==='
SELECT CASE WHEN NOT bool_or(has_function_privilege(r, f, 'EXECUTE')) THEN 'PASS T3' ELSE 'FAIL T3' END
  FROM unnest(ARRAY['anon', 'authenticated', 'service_role']) r,
       unnest(ARRAY['public.credit_ledger_immutable()'::regprocedure,
                    'public.trainings_guard_credit_columns()'::regprocedure]) f;

\echo '=== T4 PUBLIC, anon and authenticated cannot execute prune_workflow_trajectories(); service_role still can ==='
SELECT CASE WHEN NOT has_function_privilege('anon', f, 'EXECUTE')
             AND NOT has_function_privilege('authenticated', f, 'EXECUTE')
             AND NOT EXISTS (SELECT 1 FROM pg_proc p, aclexplode(p.proacl) a
                              WHERE p.oid = f AND a.grantee = 0 AND a.privilege_type = 'EXECUTE')
             AND has_function_privilege('service_role', f, 'EXECUTE')
            THEN 'PASS T4' ELSE 'FAIL T4' END
  FROM (SELECT 'public.prune_workflow_trajectories()'::regprocedure AS f) s;

\echo '=== T5 PUBLIC, anon and authenticated cannot execute prune_workflow_submissions(); service_role still can ==='
SELECT CASE WHEN NOT has_function_privilege('anon', f, 'EXECUTE')
             AND NOT has_function_privilege('authenticated', f, 'EXECUTE')
             AND NOT EXISTS (SELECT 1 FROM pg_proc p, aclexplode(p.proacl) a
                              WHERE p.oid = f AND a.grantee = 0 AND a.privilege_type = 'EXECUTE')
             AND has_function_privilege('service_role', f, 'EXECUTE')
            THEN 'PASS T5' ELSE 'FAIL T5' END
  FROM (SELECT 'public.prune_workflow_submissions()'::regprocedure AS f) s;

\echo '=== T6 both prune functions are still SECURITY DEFINER with search_path=public (034/040) ==='
SELECT CASE WHEN count(*) = 2 AND bool_and(prosecdef AND proconfig = '{search_path=public}') THEN 'PASS T6' ELSE 'FAIL T6' END
  FROM pg_proc WHERE oid IN ('public.prune_workflow_trajectories()'::regprocedure,
                             'public.prune_workflow_submissions()'::regprocedure);

\echo '=== T7 the four bodies are the ones 034, 040 and 052 created, byte for byte ==='
SELECT CASE WHEN count(*) = 4 AND bool_and(md5(prosrc) = CASE proname
                 WHEN 'credit_ledger_immutable' THEN '62af5cbad2153b0664c5fa21814197bd'
                 WHEN 'trainings_guard_credit_columns' THEN '0a493f274a5fbac40c20f793480a7cb4'
                 WHEN 'prune_workflow_trajectories' THEN 'ad10f94f85bed02446677482f379fbef'
                 WHEN 'prune_workflow_submissions' THEN '7b81ff02c3c8d69188514c618e3476bb' END)
            THEN 'PASS T7' ELSE 'FAIL T7' END
  FROM pg_proc WHERE pronamespace = 'public'::regnamespace
   AND proname IN ('credit_ledger_immutable', 'trainings_guard_credit_columns',
                   'prune_workflow_trajectories', 'prune_workflow_submissions');

\echo '=== T8 the five triggers on the four functions are there and enabled ==='
SELECT CASE WHEN count(*) = 5 THEN 'PASS T8' ELSE 'FAIL T8 (' || count(*) || ' of 5)' END
  FROM pg_trigger t
 WHERE NOT t.tgisinternal AND t.tgenabled = 'O'
   AND (t.tgrelid, t.tgname, t.tgfoid) IN (
         ('public.credit_ledger'::regclass, 'credit_ledger_no_change', 'public.credit_ledger_immutable()'::regprocedure),
         ('public.credit_ledger'::regclass, 'credit_ledger_no_truncate', 'public.credit_ledger_immutable()'::regprocedure),
         ('public.trainings'::regclass, 'trg_0_trainings_credit_guard', 'public.trainings_guard_credit_columns()'::regprocedure),
         ('public.workflow_trajectories'::regclass, 'trg_workflow_trajectories_prune',
          'public.prune_workflow_trajectories()'::regprocedure),
         ('public.workflow_submissions'::regclass, 'trg_workflow_submissions_prune',
          'public.prune_workflow_submissions()'::regprocedure));

\echo '=== T9-T15 the triggers behave as before (seeded, rolled back) ==='
BEGIN;
INSERT INTO auth.users (id, instance_id, aud, role, email, encrypted_password, created_at, updated_at)
VALUES ('05400001-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000000', 'authenticated',
        'authenticated', 't054@edubotics.local', '', now(), now());
INSERT INTO public.workflows (id, owner_user_id, name, blockly_json)
VALUES ('05400002-0000-0000-0000-000000000001', '05400001-0000-0000-0000-000000000001', 't054 a', '{}'::jsonb),
       ('05400002-0000-0000-0000-000000000002', '05400001-0000-0000-0000-000000000001', 't054 b', '{}'::jsonb);
INSERT INTO public.trainings (id, user_id, status, dataset_name, model_name, model_type, credit_scheme)
VALUES (-54, '05400001-0000-0000-0000-000000000001', 'failed', 'u/d', 'u/m054', 'act', 'derived');

-- T9: the ledger still refuses TRUNCATE, its owner included (credit_ledger_no_truncate).
DO $$
BEGIN
  TRUNCATE public.credit_ledger;
  RAISE NOTICE 'FAIL T9 (TRUNCATE went through)';
EXCEPTION WHEN SQLSTATE 'P0068' THEN RAISE NOTICE 'PASS T9 (%)', SQLERRM;
END $$;

-- T10: the credit-column guard still refuses service_role (current_user is judged, not the owner).
DO $$
BEGIN
  SET LOCAL ROLE service_role;
  UPDATE public.trainings SET reserved_hours = 1 WHERE id = -54;
  RAISE NOTICE 'FAIL T10 (service_role changed reserved_hours)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T10 (%)', SQLERRM;
END $$;

-- T11: ... and still lets service_role change a non-credit column and the owner a credit column.
DO $$
DECLARE v_ok BOOLEAN;
BEGIN
  SET LOCAL ROLE service_role;
  UPDATE public.trainings SET error_message = 't054' WHERE id = -54;
  RESET ROLE;
  UPDATE public.trainings SET reserved_hours = 2 WHERE id = -54;
  SELECT error_message = 't054' AND reserved_hours = 2 INTO v_ok FROM public.trainings WHERE id = -54;
  IF v_ok THEN RAISE NOTICE 'PASS T11'; ELSE RAISE NOTICE 'FAIL T11 (row not as written)'; END IF;
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T11 (%: %)', SQLSTATE, SQLERRM;
END $$;

-- T12: service_role (the cloud API, the real writer) — a workflow's 17th recording prunes its oldest.
DO $$
DECLARE v_left INT; v_oldest BOOLEAN;
BEGIN
  SET LOCAL ROLE service_role;
  INSERT INTO public.workflow_trajectories (workflow_id, owner_user_id, name, samples, created_at)
  SELECT '05400002-0000-0000-0000-000000000001', '05400001-0000-0000-0000-000000000001',
         'r' || lpad(n::text, 2, '0'), '{"fps":25,"points":[]}'::jsonb,
         timestamptz '2026-01-01 00:00+00' + n * interval '1 minute'
    FROM generate_series(1, 17) n;
  RESET ROLE;
  SELECT count(*), bool_and(name <> 'r01') INTO v_left, v_oldest
    FROM public.workflow_trajectories WHERE workflow_id = '05400002-0000-0000-0000-000000000001';
  IF v_left = 16 AND v_oldest THEN RAISE NOTICE 'PASS T12';
  ELSE RAISE NOTICE 'FAIL T12 (% left, oldest gone: %)', v_left, v_oldest; END IF;
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T12 (%: %)', SQLSTATE, SQLERRM;
END $$;

-- T13: service_role — a student's 9th submission of one workflow prunes the oldest.
DO $$
DECLARE v_left INT; v_oldest BOOLEAN;
BEGIN
  SET LOCAL ROLE service_role;
  INSERT INTO public.workflow_submissions (workflow_id, student_user_id, name, submitted_at)
  SELECT '05400002-0000-0000-0000-000000000001', '05400001-0000-0000-0000-000000000001',
         's' || n, timestamptz '2026-01-01 00:00+00' + n * interval '1 minute'
    FROM generate_series(1, 9) n;
  RESET ROLE;
  SELECT count(*), bool_and(name <> 's1') INTO v_left, v_oldest
    FROM public.workflow_submissions WHERE workflow_id = '05400002-0000-0000-0000-000000000001';
  IF v_left = 8 AND v_oldest THEN RAISE NOTICE 'PASS T13';
  ELSE RAISE NOTICE 'FAIL T13 (% left, oldest gone: %)', v_left, v_oldest; END IF;
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T13 (%: %)', SQLSTATE, SQLERRM;
END $$;

-- T14/T15: a role with NO EXECUTE on the prune functions still fires them.
-- authenticated may not insert through RLS (no INSERT policy on either table),
-- so this rolled-back transaction gives it one for the check.
CREATE POLICY t054_insert ON public.workflow_trajectories FOR INSERT TO authenticated WITH CHECK (true);
CREATE POLICY t054_insert ON public.workflow_submissions FOR INSERT TO authenticated WITH CHECK (true);
GRANT INSERT ON public.workflow_trajectories, public.workflow_submissions TO authenticated;

DO $$
DECLARE v_left INT; v_oldest BOOLEAN;
BEGIN
  IF has_function_privilege('authenticated', 'public.prune_workflow_trajectories()', 'EXECUTE') THEN
    RAISE EXCEPTION 'authenticated holds EXECUTE: this check proves nothing';
  END IF;
  SET LOCAL ROLE authenticated;
  INSERT INTO public.workflow_trajectories (workflow_id, owner_user_id, name, samples, created_at)
  SELECT '05400002-0000-0000-0000-000000000002', '05400001-0000-0000-0000-000000000001',
         'r' || lpad(n::text, 2, '0'), '{"fps":25,"points":[]}'::jsonb,
         timestamptz '2026-01-01 00:00+00' + n * interval '1 minute'
    FROM generate_series(1, 17) n;
  RESET ROLE;
  SELECT count(*), bool_and(name <> 'r01') INTO v_left, v_oldest
    FROM public.workflow_trajectories WHERE workflow_id = '05400002-0000-0000-0000-000000000002';
  IF v_left = 16 AND v_oldest THEN RAISE NOTICE 'PASS T14';
  ELSE RAISE NOTICE 'FAIL T14 (% left, oldest gone: %)', v_left, v_oldest; END IF;
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T14 (%: %)', SQLSTATE, SQLERRM;
END $$;

DO $$
DECLARE v_left INT; v_oldest BOOLEAN;
BEGIN
  IF has_function_privilege('authenticated', 'public.prune_workflow_submissions()', 'EXECUTE') THEN
    RAISE EXCEPTION 'authenticated holds EXECUTE: this check proves nothing';
  END IF;
  SET LOCAL ROLE authenticated;
  INSERT INTO public.workflow_submissions (workflow_id, student_user_id, name, submitted_at)
  SELECT '05400002-0000-0000-0000-000000000002', '05400001-0000-0000-0000-000000000001',
         's' || n, timestamptz '2026-01-01 00:00+00' + n * interval '1 minute'
    FROM generate_series(1, 9) n;
  RESET ROLE;
  SELECT count(*), bool_and(name <> 's1') INTO v_left, v_oldest
    FROM public.workflow_submissions WHERE workflow_id = '05400002-0000-0000-0000-000000000002';
  IF v_left = 8 AND v_oldest THEN RAISE NOTICE 'PASS T15';
  ELSE RAISE NOTICE 'FAIL T15 (% left, oldest gone: %)', v_left, v_oldest; END IF;
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T15 (%: %)', SQLSTATE, SQLERRM;
END $$;
ROLLBACK;

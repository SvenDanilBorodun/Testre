-- 047 — students can no longer read public.trainings.worker_token: the request
-- roles `anon` and `authenticated` lose the TABLE-level SELECT on
-- public.trainings and get a COLUMN-level SELECT on every column except
-- worker_token (owner decision, 2026-10-18; the RunPod training build, spec
-- §3.1 / §B.1).
--
-- Proven on a local `supabase start` stack (Postgres 17,
-- public.ecr.aws/supabase/postgres:17.6.1.106, the guarded baseline + every
-- migration through 048 applied with psql as `postgres`) with
-- supabase/tests/047_hide_worker_token_assertions.sql; PostgREST v14.10 then
-- answers 42501 to `select=*`, to a filter and to an order on worker_token,
-- the SPA's own column list (useSupabaseTrainings' LIVE_COLUMNS) still reads,
-- and Realtime v2.86.3 still delivers INSERT and UPDATE of the student's own
-- rows, without the token.
-- Rollback: rollback/20261020120000_047_hide_worker_token_rollback.sql
-- (re-opens the hole below; roll 049 back first).
--
-- NUMBERING: 037 stays reserved („Eigene Objekte", docs/plans/eigene-objekte-sql/).
-- 047 was earmarked for this fix when 048 shipped (docs/KNOWN-ISSUES.md). The
-- timestamp 20261020120000 sorts AFTER 048 (20261011120000): `supabase db push`
-- orders by the timestamp prefix, so 047 is applied after 048, and 049
-- (20261020130000) after 047. 047 cannot be re-applied once 049 ran: 049 grants
-- its own new columns, a re-run REVOKE takes those column grants away too, and
-- the self-check below then aborts the transaction, changing nothing.
--
-- THE HOLE (live since the baseline): Supabase's default privileges grant
-- SELECT on every public table to anon and authenticated, and the „… read
-- consolidated" policy lets a student read their own trainings rows — all of
-- their columns, worker_token included. update_training_progress is EXECUTE for
-- both request roles (the worker calls it with the anon key), so a student who
-- read their own RUNNING training's token could mark it `failed` and get the
-- credit back while the GPU job ran on (credits are DERIVED from
-- trainings.status). After 046 this was the last open credit path.
--
-- WHAT CHANGES
--   1. REVOKE SELECT ON public.trainings FROM anon, authenticated (the table
--      grant; RLS and every policy stay as they are).
--   2. GRANT SELECT (<the 20 other columns>) to both roles. Every column a
--      client reads today is in that list: the SPA reads only LIVE_COLUMNS,
--      the teacher web reads trainings through the cloud API (service_role),
--      Realtime filters on user_id/workgroup_id and sends only the columns the
--      subscriber may SELECT.
--   3. A self-check that aborts the migration when either role can still read
--      worker_token (a PUBLIC grant or a role membership would survive the
--      revoke) or lost any other column.
--
-- CONSEQUENCE FOR LATER MIGRATIONS: a table-level grant no longer covers new
-- columns. Every column added to public.trainings after 047 must be granted
-- to anon and authenticated explicitly (049 grants its own seven).
BEGIN;

REVOKE SELECT ON public.trainings FROM anon, authenticated;

GRANT SELECT (
  id, user_id, status, dataset_name, model_name, model_type, training_params, cloud_job_id,
  current_step, total_steps, current_loss, requested_at, terminated_at, error_message,
  last_progress_at, loss_history, workgroup_id, cancel_attempted_at, cancel_attempts, log_url
) ON public.trainings TO anon, authenticated;

DO $$
DECLARE
  v_role TEXT;
  v_col  TEXT;
BEGIN
  FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
    IF has_column_privilege(v_role, 'public.trainings', 'worker_token', 'SELECT') THEN
      RAISE EXCEPTION '047: % can still read trainings.worker_token (a PUBLIC grant or a role membership?)', v_role;
    END IF;
    FOR v_col IN
      SELECT column_name FROM information_schema.columns
       WHERE table_schema = 'public' AND table_name = 'trainings' AND column_name <> 'worker_token'
    LOOP
      IF NOT has_column_privilege(v_role, 'public.trainings', v_col, 'SELECT') THEN
        RAISE EXCEPTION '047: % lost SELECT on trainings.%', v_role, v_col;
      END IF;
    END LOOP;
  END LOOP;
END $$;

COMMIT;

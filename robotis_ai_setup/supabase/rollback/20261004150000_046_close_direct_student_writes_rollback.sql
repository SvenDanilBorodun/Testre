-- Rollback for 046_close_direct_student_writes. Gives anon and authenticated
-- back the full write privilege set on public.trainings, public.datasets,
-- public.workflows and public.tutorial_progress, and re-creates the twelve
-- own-row write policies exactly as 024 (20260520130522_024_advisor_fixes.sql)
-- created them; nothing between 024 and 046 touched them. No data is touched.
--
-- The grants restored are the set Supabase's default privileges give every
-- public table (INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER; SELECT
-- was never revoked), i.e. the state measured on the local `supabase start`
-- stack before 046 and the one the live project reported for these tables.
--
-- WARNING: this RE-OPENS every hole 046 closed — a signed-in student can again
-- set their own training to 'canceled'/'failed' or delete it through
-- PostgREST and get the credit back while the GPU job runs, insert a datasets
-- row past register_dataset_safe's author anchor, and write workflows and
-- tutorial_progress past the cloud API's checks. Run it only to unblock a
-- regression, and roll forward again as soon as possible. No cloud API change
-- depends on 046, so the API needs no rollback either way.
--
-- Idempotent — re-runnable (GRANT is a no-op when held, each policy is dropped
-- before it is created).
BEGIN;

GRANT INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON public.trainings, public.datasets, public.workflows, public.tutorial_progress
    TO anon, authenticated;

-- ── public.trainings (024) ──────────────────────────────────────────────────
DROP POLICY IF EXISTS "Users insert own trainings"      ON public.trainings;
DROP POLICY IF EXISTS "Users update own trainings"      ON public.trainings;
DROP POLICY IF EXISTS "Users delete own trainings"      ON public.trainings;

CREATE POLICY "Users insert own trainings"
  ON public.trainings FOR INSERT
  TO authenticated
  WITH CHECK (user_id = (SELECT auth.uid()));

CREATE POLICY "Users update own trainings"
  ON public.trainings FOR UPDATE
  TO authenticated
  USING (user_id = (SELECT auth.uid()))
  WITH CHECK (user_id = (SELECT auth.uid()));

CREATE POLICY "Users delete own trainings"
  ON public.trainings FOR DELETE
  TO authenticated
  USING (user_id = (SELECT auth.uid()));

-- ── public.workflows (024) ──────────────────────────────────────────────────
DROP POLICY IF EXISTS "Owner inserts own workflows"          ON public.workflows;
DROP POLICY IF EXISTS "Owner updates own workflows"          ON public.workflows;
DROP POLICY IF EXISTS "Owner deletes own workflows"          ON public.workflows;

CREATE POLICY "Owner inserts own workflows"
  ON public.workflows FOR INSERT
  WITH CHECK (owner_user_id = (SELECT auth.uid()));

CREATE POLICY "Owner updates own workflows"
  ON public.workflows FOR UPDATE
  USING (owner_user_id = (SELECT auth.uid()))
  WITH CHECK (owner_user_id = (SELECT auth.uid()));

CREATE POLICY "Owner deletes own workflows"
  ON public.workflows FOR DELETE
  USING (owner_user_id = (SELECT auth.uid()));

-- ── public.datasets (024) ───────────────────────────────────────────────────
DROP POLICY IF EXISTS "Owner inserts own datasets"      ON public.datasets;
DROP POLICY IF EXISTS "Owner updates own datasets"      ON public.datasets;
DROP POLICY IF EXISTS "Owner deletes own datasets"      ON public.datasets;

CREATE POLICY "Owner inserts own datasets"
  ON public.datasets FOR INSERT
  WITH CHECK (owner_user_id = (SELECT auth.uid()));

CREATE POLICY "Owner updates own datasets"
  ON public.datasets FOR UPDATE
  USING (owner_user_id = (SELECT auth.uid()))
  WITH CHECK (owner_user_id = (SELECT auth.uid()));

CREATE POLICY "Owner deletes own datasets"
  ON public.datasets FOR DELETE
  USING (owner_user_id = (SELECT auth.uid()));

-- ── public.tutorial_progress (024) ──────────────────────────────────────────
DROP POLICY IF EXISTS "Tutorial progress owner insert"          ON public.tutorial_progress;
DROP POLICY IF EXISTS "Tutorial progress owner update"          ON public.tutorial_progress;
DROP POLICY IF EXISTS "Tutorial progress owner delete"          ON public.tutorial_progress;

CREATE POLICY "Tutorial progress owner insert"
  ON public.tutorial_progress FOR INSERT
  WITH CHECK (user_id = (SELECT auth.uid()));

CREATE POLICY "Tutorial progress owner update"
  ON public.tutorial_progress FOR UPDATE
  USING (user_id = (SELECT auth.uid()))
  WITH CHECK (user_id = (SELECT auth.uid()));

CREATE POLICY "Tutorial progress owner delete"
  ON public.tutorial_progress FOR DELETE
  USING (user_id = (SELECT auth.uid()));

COMMIT;

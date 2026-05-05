-- 009: Tighten public.workflows write policies + close the orphan-
-- template hole created by ON DELETE SET NULL on classroom_id.
--
-- Audit findings:
--
-- 2.2 — 008_workflows.sql shipped FOR SELECT policies only. The
-- service-role key used by FastAPI bypasses RLS, but defence-in-depth
-- says explicit deny-by-default for anon. WITH CHECK on INSERT/UPDATE
-- + a USING gate on DELETE makes the contract explicit.
--
-- 2.3 — workflows.classroom_id is "ON DELETE SET NULL" on classrooms,
-- so a teacher deleting a classroom turns every classroom template
-- into an orphan with is_template=TRUE and classroom_id=NULL — these
-- rows then leak past the "Classroom members read templates" SELECT
-- policy (which filters on classroom_id, not is_template alone). Add
-- a CHECK that rejects the orphan state outright.

-- ---------------------------------------------------------------------------
-- 1. Owner-only WITH CHECK / USING for INSERT, UPDATE, DELETE
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS "Owner inserts own workflows" ON public.workflows;
CREATE POLICY "Owner inserts own workflows"
  ON public.workflows FOR INSERT
  WITH CHECK (owner_user_id = auth.uid());

DROP POLICY IF EXISTS "Owner updates own workflows" ON public.workflows;
CREATE POLICY "Owner updates own workflows"
  ON public.workflows FOR UPDATE
  USING (owner_user_id = auth.uid())
  WITH CHECK (owner_user_id = auth.uid());

DROP POLICY IF EXISTS "Owner deletes own workflows" ON public.workflows;
CREATE POLICY "Owner deletes own workflows"
  ON public.workflows FOR DELETE
  USING (owner_user_id = auth.uid());

-- ---------------------------------------------------------------------------
-- 2. CHECK constraint: templates must reference an existing classroom
-- ---------------------------------------------------------------------------
ALTER TABLE public.workflows
  DROP CONSTRAINT IF EXISTS chk_template_has_classroom;

-- Cleanup any orphan templates the v1 ship may already have in prod
-- before the constraint goes live. is_template=TRUE without a
-- classroom is unreachable through the SELECT policies anyway, so
-- demoting to is_template=FALSE just makes the existing state
-- consistent.
UPDATE public.workflows
   SET is_template = FALSE
 WHERE is_template = TRUE
   AND classroom_id IS NULL;

ALTER TABLE public.workflows
  ADD CONSTRAINT chk_template_has_classroom
  CHECK (NOT (is_template = TRUE AND classroom_id IS NULL));

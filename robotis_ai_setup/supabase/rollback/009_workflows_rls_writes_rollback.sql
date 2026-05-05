-- Rollback for 009_workflows_rls_writes.sql.
--
-- Drops the three write-side RLS policies and the
-- chk_template_has_classroom constraint. The orphan-template UPDATE
-- the forward migration ran is NOT reverted — those rows are now
-- legitimate non-templates and reverting would re-create the
-- inconsistent state.

ALTER TABLE public.workflows
  DROP CONSTRAINT IF EXISTS chk_template_has_classroom;

DROP POLICY IF EXISTS "Owner inserts own workflows" ON public.workflows;
DROP POLICY IF EXISTS "Owner updates own workflows" ON public.workflows;
DROP POLICY IF EXISTS "Owner deletes own workflows" ON public.workflows;

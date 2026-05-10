-- 013_workflow_versions_rollback.sql
--
-- Reverse of 013. Drops the snapshot trigger first so an UPDATE on
-- workflows during the rollback doesn't try to insert into a table
-- we're about to delete.

BEGIN;

DROP TRIGGER IF EXISTS trg_workflows_snapshot ON public.workflows;
DROP TRIGGER IF EXISTS trg_workflow_versions_prune ON public.workflow_versions;
DROP FUNCTION IF EXISTS public.snapshot_workflow_version();
DROP FUNCTION IF EXISTS public.prune_workflow_versions();

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_publication_tables
    WHERE pubname = 'supabase_realtime'
      AND schemaname = 'public'
      AND tablename = 'workflow_versions'
  ) THEN
    ALTER PUBLICATION supabase_realtime DROP TABLE public.workflow_versions;
  END IF;
END $$;

DROP TABLE IF EXISTS public.workflow_versions;

COMMIT;

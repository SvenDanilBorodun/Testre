-- Rollback for 008_workflows.sql.

-- Realtime publication
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_publication_tables
     WHERE pubname = 'supabase_realtime'
       AND schemaname = 'public'
       AND tablename = 'workflows'
  ) THEN
    ALTER PUBLICATION supabase_realtime DROP TABLE public.workflows;
  END IF;
END
$$;

-- RLS policies
DROP POLICY IF EXISTS "Owner reads own workflows" ON public.workflows;
DROP POLICY IF EXISTS "Classroom members read templates" ON public.workflows;
DROP POLICY IF EXISTS "Teacher reads classroom templates" ON public.workflows;
DROP POLICY IF EXISTS "Admin reads all workflows" ON public.workflows;

-- Trigger + indexes + table
DROP TRIGGER IF EXISTS trg_workflows_touch ON public.workflows;
DROP INDEX IF EXISTS public.idx_workflows_owner;
DROP INDEX IF EXISTS public.idx_workflows_classroom_template;
DROP TABLE IF EXISTS public.workflows;

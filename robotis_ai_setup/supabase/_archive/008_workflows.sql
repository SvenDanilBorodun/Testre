-- 008: Roboter Studio workflows.
--
-- A workflow is a Blockly workspace JSON authored by a student or
-- published by a teacher as a classroom template. Students see their
-- own workflows + their classroom's templates; teachers see + manage
-- the templates of classrooms they own; admins see everything.
--
-- The realtime publication add lets the React SPA subscribe to
-- postgres_changes and refresh the workflow list as soon as the API
-- writes a new row (mirrors 006_loss_history.sql's pattern for
-- public.trainings).

-- ---------------------------------------------------------------------------
-- 1. Table
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.workflows (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  classroom_id UUID REFERENCES public.classrooms(id) ON DELETE SET NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  blockly_json JSONB NOT NULL,
  is_template BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE public.workflows IS
  'Roboter Studio Blockly workflows. is_template=true means a teacher '
  'has published it for the classroom; classmates clone before they edit.';

CREATE INDEX IF NOT EXISTS idx_workflows_owner
  ON public.workflows(owner_user_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_workflows_classroom_template
  ON public.workflows(classroom_id, updated_at DESC)
  WHERE is_template = TRUE;

-- Reuse the touch_updated_at() helper from earlier migrations.
-- SET search_path = public per WORKFLOW-supabase-migration.md §4
-- (defense-in-depth against search_path injection).
CREATE OR REPLACE FUNCTION public.touch_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public
AS $$
BEGIN
  NEW.updated_at := NOW();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_workflows_touch ON public.workflows;
CREATE TRIGGER trg_workflows_touch
BEFORE UPDATE ON public.workflows
FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

-- ---------------------------------------------------------------------------
-- 2. RLS — service role bypasses these (FastAPI uses service-role key
-- and asserts ownership in Python), but anon-key reads are protected.
-- ---------------------------------------------------------------------------
ALTER TABLE public.workflows ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Owner reads own workflows" ON public.workflows;
CREATE POLICY "Owner reads own workflows"
  ON public.workflows FOR SELECT
  USING (owner_user_id = auth.uid());

DROP POLICY IF EXISTS "Classroom members read templates" ON public.workflows;
CREATE POLICY "Classroom members read templates"
  ON public.workflows FOR SELECT
  USING (
    is_template = TRUE
    AND classroom_id = (
      SELECT classroom_id FROM public.users WHERE id = auth.uid()
    )
  );

DROP POLICY IF EXISTS "Teacher reads classroom templates" ON public.workflows;
CREATE POLICY "Teacher reads classroom templates"
  ON public.workflows FOR SELECT
  USING (
    is_template = TRUE
    AND classroom_id IN (
      SELECT id FROM public.classrooms WHERE teacher_id = auth.uid()
    )
  );

DROP POLICY IF EXISTS "Admin reads all workflows" ON public.workflows;
CREATE POLICY "Admin reads all workflows"
  ON public.workflows FOR SELECT
  USING (
    EXISTS (SELECT 1 FROM public.users WHERE id = auth.uid() AND role = 'admin')
  );

-- ---------------------------------------------------------------------------
-- 3. Realtime publication — idempotent block mirroring 006_loss_history.sql
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_publication_tables
     WHERE pubname = 'supabase_realtime'
       AND schemaname = 'public'
       AND tablename = 'workflows'
  ) THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.workflows;
  END IF;
END
$$;

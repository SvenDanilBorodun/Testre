-- 016_tutorial_progress_rollback.sql
--
-- Reverse of 016.

BEGIN;

-- Drop the realtime publication entry first so the publication isn't
-- left pointing at a table we're about to delete.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_publication_tables
    WHERE pubname = 'supabase_realtime'
      AND schemaname = 'public'
      AND tablename = 'tutorial_progress'
  ) THEN
    ALTER PUBLICATION supabase_realtime DROP TABLE public.tutorial_progress;
  END IF;
END $$;

DROP POLICY IF EXISTS "Teacher reads classroom student progress" ON public.tutorial_progress;
DROP POLICY IF EXISTS "Admin reads all progress" ON public.tutorial_progress;
DROP POLICY IF EXISTS "Owner writes own progress" ON public.tutorial_progress;
DROP POLICY IF EXISTS "Owner reads own progress" ON public.tutorial_progress;
DROP TRIGGER IF EXISTS trg_tutorial_progress_touch ON public.tutorial_progress;
DROP FUNCTION IF EXISTS public.touch_tutorial_progress_updated_at();
DROP TABLE IF EXISTS public.tutorial_progress;

COMMIT;

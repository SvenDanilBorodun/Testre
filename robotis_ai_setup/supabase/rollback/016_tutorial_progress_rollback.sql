-- 014_tutorial_progress_rollback.sql
--
-- Reverse of 014.

BEGIN;

DROP POLICY IF EXISTS "Teacher reads classroom student progress" ON public.tutorial_progress;
DROP POLICY IF EXISTS "Admin reads all progress" ON public.tutorial_progress;
DROP POLICY IF EXISTS "Owner writes own progress" ON public.tutorial_progress;
DROP POLICY IF EXISTS "Owner reads own progress" ON public.tutorial_progress;
DROP TRIGGER IF EXISTS trg_tutorial_progress_touch ON public.tutorial_progress;
DROP FUNCTION IF EXISTS public.touch_tutorial_progress_updated_at();
DROP TABLE IF EXISTS public.tutorial_progress;

COMMIT;

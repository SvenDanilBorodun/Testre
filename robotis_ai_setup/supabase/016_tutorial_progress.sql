-- 016_tutorial_progress.sql
--
-- Roboter Studio Phase-3: per-student progress through the bundled
-- skillmap tutorials (``physical_ai_manager/public/tutorials/*.json``).
-- Each row tracks one student × one tutorial; current_step starts at 0
-- and advances as the student completes each step.
--
-- Numbered 016 (skipping 014) because 014 was never authored — the
-- original 013_workflow_versions.sql collided alphabetically with
-- 013_revoke_anon_from_security_definer.sql and was renamed to 015
-- (audit round-3 §AJ).

BEGIN;

CREATE TABLE IF NOT EXISTS public.tutorial_progress (
  user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  tutorial_id TEXT NOT NULL,
  current_step INTEGER NOT NULL DEFAULT 0,
  completed_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (user_id, tutorial_id)
);

CREATE INDEX IF NOT EXISTS idx_tutorial_progress_completed
  ON public.tutorial_progress(user_id)
  WHERE completed_at IS NOT NULL;

CREATE OR REPLACE FUNCTION public.touch_tutorial_progress_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_tutorial_progress_touch ON public.tutorial_progress;
CREATE TRIGGER trg_tutorial_progress_touch
  BEFORE UPDATE ON public.tutorial_progress
  FOR EACH ROW
  EXECUTE FUNCTION public.touch_tutorial_progress_updated_at();

-- RLS: owner-only read/write; admin reads all.
ALTER TABLE public.tutorial_progress ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Owner reads own progress" ON public.tutorial_progress;
CREATE POLICY "Owner reads own progress"
  ON public.tutorial_progress
  FOR SELECT
  USING (user_id = auth.uid());

DROP POLICY IF EXISTS "Owner writes own progress" ON public.tutorial_progress;
CREATE POLICY "Owner writes own progress"
  ON public.tutorial_progress
  FOR ALL
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

DROP POLICY IF EXISTS "Admin reads all progress" ON public.tutorial_progress;
CREATE POLICY "Admin reads all progress"
  ON public.tutorial_progress
  FOR SELECT
  USING (
    EXISTS (
      SELECT 1 FROM public.users
      WHERE id = auth.uid() AND role = 'admin'
    )
  );

-- Teachers can see their own students' progress for classroom-level
-- review.
DROP POLICY IF EXISTS "Teacher reads classroom student progress" ON public.tutorial_progress;
CREATE POLICY "Teacher reads classroom student progress"
  ON public.tutorial_progress
  FOR SELECT
  USING (
    EXISTS (
      SELECT 1
      FROM public.users student
      JOIN public.classrooms c ON c.id = student.classroom_id
      WHERE student.id = tutorial_progress.user_id
        AND c.teacher_id = auth.uid()
    )
  );

-- Explicit service_role grant. service_role bypasses RLS via Supabase's
-- JWT shortcut but the SQL ACL needs a matching GRANT so other tools
-- (psql, pgAdmin) see consistent privileges. Audit round-3 §AM.
GRANT ALL ON public.tutorial_progress TO service_role;

-- Realtime publication so the teacher dashboard updates as students
-- complete tutorial steps. The CLAUDE.md §9.14 docstring already
-- describes this; the v1 ship of 016 omitted the ALTER and the
-- consumer never got live updates. Audit round-3 §J / §AK.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_publication_tables
    WHERE pubname = 'supabase_realtime'
      AND schemaname = 'public'
      AND tablename = 'tutorial_progress'
  ) THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.tutorial_progress;
  END IF;
END $$;

COMMIT;

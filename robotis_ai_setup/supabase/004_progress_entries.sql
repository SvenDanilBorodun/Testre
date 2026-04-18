-- 004_progress_entries.sql
-- Replace the lessons feature + the static users.progress_note with a
-- time-series teacher progress log. Each entry is scoped to a single day:
--   - student_id NOT NULL -> per-student entry for that day
--   - student_id NULL     -> class-wide entry for that day
--
-- Apply order: migration.sql -> 002_accounts.sql -> 003_lessons_and_notes.sql -> this file.

-- 1. Drop lesson tables + enum (feature removed in favour of per-day entries)
DROP TABLE IF EXISTS public.lesson_progress;
DROP TABLE IF EXISTS public.lessons;
DROP TYPE IF EXISTS public.lesson_status;

-- 2. Drop the static single-note column
ALTER TABLE public.users DROP COLUMN IF EXISTS progress_note;

-- 3. The progress_entries table
CREATE TABLE IF NOT EXISTS public.progress_entries (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  classroom_id UUID NOT NULL REFERENCES public.classrooms(id) ON DELETE CASCADE,
  student_id UUID REFERENCES public.users(id) ON DELETE CASCADE,
  entry_date DATE NOT NULL DEFAULT CURRENT_DATE,
  note TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_progress_entries_classroom
  ON public.progress_entries(classroom_id, entry_date DESC);

CREATE INDEX IF NOT EXISTS idx_progress_entries_student
  ON public.progress_entries(student_id, entry_date DESC)
  WHERE student_id IS NOT NULL;

-- Prevent duplicate entries for the same scope + day. NULLs are distinct
-- in a normal UNIQUE, so we use two partial unique indexes instead.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_progress_entries_student_day
  ON public.progress_entries(student_id, entry_date)
  WHERE student_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uniq_progress_entries_classroom_day
  ON public.progress_entries(classroom_id, entry_date)
  WHERE student_id IS NULL;

-- Reuse the touch_updated_at() helper from 003 if it still exists,
-- otherwise create it.
CREATE OR REPLACE FUNCTION public.touch_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at := NOW();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_progress_entries_touch ON public.progress_entries;
CREATE TRIGGER trg_progress_entries_touch
BEFORE UPDATE ON public.progress_entries
FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

-- 4. RLS — writes still go through the service role key in the FastAPI
-- backend, but read policies protect against leaked anon tokens.
ALTER TABLE public.progress_entries ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Teachers read own progress entries" ON public.progress_entries;
CREATE POLICY "Teachers read own progress entries"
  ON public.progress_entries FOR SELECT
  USING (
    classroom_id IN (
      SELECT id FROM public.classrooms WHERE teacher_id = auth.uid()
    )
  );

DROP POLICY IF EXISTS "Students read own + own-classroom entries" ON public.progress_entries;
CREATE POLICY "Students read own + own-classroom entries"
  ON public.progress_entries FOR SELECT
  USING (
    student_id = auth.uid()
    OR (student_id IS NULL
        AND classroom_id = (SELECT classroom_id FROM public.users WHERE id = auth.uid()))
  );

DROP POLICY IF EXISTS "Admin reads all progress entries" ON public.progress_entries;
CREATE POLICY "Admin reads all progress entries"
  ON public.progress_entries FOR SELECT
  USING (
    EXISTS (SELECT 1 FROM public.users WHERE id = auth.uid() AND role = 'admin')
  );

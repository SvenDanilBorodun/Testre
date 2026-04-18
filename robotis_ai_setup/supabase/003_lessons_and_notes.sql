-- 003_lessons_and_notes.sql
-- Adds two teacher-facing features:
--   1. Free-form progress_note on student accounts (teacher-only text).
--   2. Lessons per classroom + per-student lesson progress tracking.
--
-- Apply order: migration.sql -> 002_accounts.sql -> this file.

-- ============================================================
-- 1. progress_note column on users
-- ============================================================

ALTER TABLE public.users
  ADD COLUMN IF NOT EXISTS progress_note TEXT;

-- No index — the column is only read alongside other student fields
-- in the teacher dashboard and never filtered on.

-- ============================================================
-- 2. lessons + lesson_progress tables
-- ============================================================

-- Lesson status for a student. Enum stays small and human-readable.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'lesson_status') THEN
    CREATE TYPE public.lesson_status AS ENUM (
      'not_started',
      'in_progress',
      'completed'
    );
  END IF;
END
$$;

-- A lesson belongs to exactly one classroom. Teachers can create an
-- arbitrary number of lessons; order_index lets them reorder in the UI.
CREATE TABLE IF NOT EXISTS public.lessons (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  classroom_id UUID NOT NULL REFERENCES public.classrooms(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  description TEXT,
  order_index INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_lessons_classroom ON public.lessons(classroom_id);

-- Per-student status for each lesson. Exactly one row per (lesson, student)
-- if anything has been tracked. Unknown state = no row (status defaults to
-- not_started when the row is absent — the API materialises that view).
CREATE TABLE IF NOT EXISTS public.lesson_progress (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  lesson_id UUID NOT NULL REFERENCES public.lessons(id) ON DELETE CASCADE,
  student_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  status public.lesson_status NOT NULL DEFAULT 'not_started',
  note TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (lesson_id, student_id)
);

CREATE INDEX IF NOT EXISTS idx_lesson_progress_lesson ON public.lesson_progress(lesson_id);
CREATE INDEX IF NOT EXISTS idx_lesson_progress_student ON public.lesson_progress(student_id);

-- ============================================================
-- 3. updated_at triggers
-- ============================================================

CREATE OR REPLACE FUNCTION public.touch_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at := NOW();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_lessons_touch ON public.lessons;
CREATE TRIGGER trg_lessons_touch
BEFORE UPDATE ON public.lessons
FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

DROP TRIGGER IF EXISTS trg_lesson_progress_touch ON public.lesson_progress;
CREATE TRIGGER trg_lesson_progress_touch
BEFORE UPDATE ON public.lesson_progress
FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

-- ============================================================
-- 4. RLS
-- ============================================================
--
-- All writes go through the FastAPI backend using the service role key,
-- which bypasses RLS. We still enable RLS + read policies so a leaked
-- anon token can only see a teacher's / student's own rows.

ALTER TABLE public.lessons ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.lesson_progress ENABLE ROW LEVEL SECURITY;

-- Teachers can read lessons that belong to their classrooms.
DROP POLICY IF EXISTS "Teachers read own lessons" ON public.lessons;
CREATE POLICY "Teachers read own lessons"
  ON public.lessons FOR SELECT
  USING (
    classroom_id IN (
      SELECT id FROM public.classrooms WHERE teacher_id = auth.uid()
    )
  );

-- Students can read lessons that belong to their classroom.
DROP POLICY IF EXISTS "Students read own classroom lessons" ON public.lessons;
CREATE POLICY "Students read own classroom lessons"
  ON public.lessons FOR SELECT
  USING (
    classroom_id = (SELECT classroom_id FROM public.users WHERE id = auth.uid())
  );

-- Admin reads all lessons.
DROP POLICY IF EXISTS "Admin reads all lessons" ON public.lessons;
CREATE POLICY "Admin reads all lessons"
  ON public.lessons FOR SELECT
  USING (
    EXISTS (SELECT 1 FROM public.users WHERE id = auth.uid() AND role = 'admin')
  );

-- Teachers can read progress rows for their own students.
DROP POLICY IF EXISTS "Teachers read own lesson progress" ON public.lesson_progress;
CREATE POLICY "Teachers read own lesson progress"
  ON public.lesson_progress FOR SELECT
  USING (
    student_id IN (
      SELECT s.id FROM public.users s
      JOIN public.classrooms c ON c.id = s.classroom_id
      WHERE c.teacher_id = auth.uid() AND s.role = 'student'
    )
  );

-- Students can read their own progress rows.
DROP POLICY IF EXISTS "Students read own lesson progress" ON public.lesson_progress;
CREATE POLICY "Students read own lesson progress"
  ON public.lesson_progress FOR SELECT
  USING (student_id = auth.uid());

-- Admin reads everyone's progress.
DROP POLICY IF EXISTS "Admin reads all lesson progress" ON public.lesson_progress;
CREATE POLICY "Admin reads all lesson progress"
  ON public.lesson_progress FOR SELECT
  USING (
    EXISTS (SELECT 1 FROM public.users WHERE id = auth.uid() AND role = 'admin')
  );

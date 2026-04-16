-- 002_accounts.sql
-- Adds role-based 3-tier account model: admin / teacher / student
-- - Teachers are pre-created by admin; each has a credit pool.
-- - Teachers create classrooms (max 30 students each) and student accounts.
-- - Teachers allocate credits from their pool to students via adjust_student_credits().
-- - Students log in with username (backend maps to synthetic edubotics.local email).
-- - Existing trainings flow (start_training_safe, get_remaining_credits) unchanged.

-- 1. Role enum
CREATE TYPE public.user_role AS ENUM ('admin', 'teacher', 'student');

-- 2. New columns on users
ALTER TABLE public.users
  ADD COLUMN role public.user_role NOT NULL DEFAULT 'student',
  ADD COLUMN username TEXT UNIQUE,
  ADD COLUMN full_name TEXT,
  ADD COLUMN classroom_id UUID,
  ADD COLUMN created_by UUID REFERENCES public.users(id) ON DELETE SET NULL;

CREATE INDEX idx_users_role ON public.users(role);
CREATE INDEX idx_users_username ON public.users(username);
CREATE INDEX idx_users_classroom ON public.users(classroom_id) WHERE classroom_id IS NOT NULL;

-- 3. Classrooms table
CREATE TABLE public.classrooms (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  teacher_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (teacher_id, name)
);

CREATE INDEX idx_classrooms_teacher ON public.classrooms(teacher_id);

ALTER TABLE public.users
  ADD CONSTRAINT fk_users_classroom
  FOREIGN KEY (classroom_id) REFERENCES public.classrooms(id) ON DELETE SET NULL;

-- 4. Enforce max 30 students per classroom via trigger
CREATE OR REPLACE FUNCTION public.enforce_classroom_capacity()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.classroom_id IS NOT NULL AND NEW.role = 'student' THEN
    IF (SELECT COUNT(*) FROM public.users
        WHERE classroom_id = NEW.classroom_id AND role = 'student'
          AND (TG_OP = 'INSERT' OR id <> NEW.id)) >= 30 THEN
      RAISE EXCEPTION 'Klassenzimmer hat die maximale Kapazitaet (30 Schueler) erreicht'
        USING ERRCODE = 'P0010';
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER trg_classroom_capacity
BEFORE INSERT OR UPDATE OF classroom_id, role ON public.users
FOR EACH ROW EXECUTE FUNCTION public.enforce_classroom_capacity();

-- 5. Teacher credit summary (pool total, allocated, available, student count)
CREATE OR REPLACE FUNCTION public.get_teacher_credit_summary(p_teacher_id UUID)
RETURNS TABLE (
  pool_total        INTEGER,
  allocated_total   BIGINT,
  pool_available    BIGINT,
  student_count     BIGINT
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  RETURN QUERY
  SELECT
    t.training_credits,
    COALESCE(SUM(s.training_credits), 0)::BIGINT,
    (t.training_credits - COALESCE(SUM(s.training_credits), 0))::BIGINT,
    COUNT(s.id)::BIGINT
  FROM public.users t
  LEFT JOIN public.classrooms c ON c.teacher_id = t.id
  LEFT JOIN public.users s ON s.classroom_id = c.id AND s.role = 'student'
  WHERE t.id = p_teacher_id AND t.role = 'teacher'
  GROUP BY t.id, t.training_credits;
END;
$$;

REVOKE ALL ON FUNCTION public.get_teacher_credit_summary(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.get_teacher_credit_summary(UUID) TO service_role;

-- 6. Atomic credit adjustment RPC (used by teacher +/- buttons)
-- p_delta > 0 = give credits, p_delta < 0 = take back unused credits
-- Validates: teacher owns student, new amount >= used, new amount >= 0,
-- teacher has enough pool remaining.
CREATE OR REPLACE FUNCTION public.adjust_student_credits(
  p_teacher_id UUID,
  p_student_id UUID,
  p_delta      INTEGER
) RETURNS TABLE (new_amount INT, pool_available BIGINT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_teacher_total INTEGER;
  v_allocated_others BIGINT;
  v_student_current INTEGER;
  v_student_used BIGINT;
  v_new_amount INTEGER;
BEGIN
  -- Validate teacher role and ownership of student
  IF NOT EXISTS (
    SELECT 1 FROM public.users t
    JOIN public.classrooms c ON c.teacher_id = t.id
    JOIN public.users s ON s.classroom_id = c.id
    WHERE t.id = p_teacher_id AND t.role = 'teacher'
      AND s.id = p_student_id AND s.role = 'student'
  ) THEN
    RAISE EXCEPTION 'Schueler gehoert nicht zu diesem Lehrer' USING ERRCODE = 'P0011';
  END IF;

  SELECT training_credits INTO v_teacher_total
  FROM public.users WHERE id = p_teacher_id FOR UPDATE;

  SELECT training_credits INTO v_student_current
  FROM public.users WHERE id = p_student_id FOR UPDATE;

  v_new_amount := v_student_current + p_delta;

  SELECT COUNT(*) INTO v_student_used
  FROM public.trainings
  WHERE user_id = p_student_id AND status NOT IN ('failed','canceled');

  IF v_new_amount < v_student_used THEN
    RAISE EXCEPTION 'Neuer Betrag (%) ist kleiner als bereits verbrauchte Credits (%)',
      v_new_amount, v_student_used USING ERRCODE = 'P0012';
  END IF;

  IF v_new_amount < 0 THEN
    RAISE EXCEPTION 'Credits duerfen nicht negativ werden' USING ERRCODE = 'P0013';
  END IF;

  SELECT COALESCE(SUM(s.training_credits), 0) INTO v_allocated_others
  FROM public.users s
  JOIN public.classrooms c ON c.id = s.classroom_id
  WHERE c.teacher_id = p_teacher_id AND s.role = 'student' AND s.id <> p_student_id;

  IF v_allocated_others + v_new_amount > v_teacher_total THEN
    RAISE EXCEPTION 'Lehrer hat nicht genug Credits im Pool' USING ERRCODE = 'P0014';
  END IF;

  UPDATE public.users SET training_credits = v_new_amount WHERE id = p_student_id;

  RETURN QUERY SELECT v_new_amount, (v_teacher_total - v_allocated_others - v_new_amount)::BIGINT;
END;
$$;

REVOKE ALL ON FUNCTION public.adjust_student_credits(UUID, UUID, INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.adjust_student_credits(UUID, UUID, INTEGER) TO service_role;

-- 7. Enable RLS on classrooms; add role-aware SELECT policies
ALTER TABLE public.classrooms ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Teachers read own classrooms"
  ON public.classrooms FOR SELECT
  USING (auth.uid() = teacher_id);

CREATE POLICY "Students read own classroom"
  ON public.classrooms FOR SELECT
  USING (id = (SELECT classroom_id FROM public.users WHERE id = auth.uid()));

CREATE POLICY "Admin reads all classrooms"
  ON public.classrooms FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.users WHERE id = auth.uid() AND role = 'admin'));

-- Teachers can see their students' rows in public.users (on top of "read own profile")
CREATE POLICY "Teachers read own students"
  ON public.users FOR SELECT
  USING (
    classroom_id IN (SELECT id FROM public.classrooms WHERE teacher_id = auth.uid())
  );

-- Admin can see everyone
CREATE POLICY "Admin reads everyone"
  ON public.users FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.users WHERE id = auth.uid() AND role = 'admin'));

-- 8. Teachers read trainings belonging to their students
CREATE POLICY "Teachers read student trainings"
  ON public.trainings FOR SELECT
  USING (
    user_id IN (
      SELECT s.id FROM public.users s
      JOIN public.classrooms c ON c.id = s.classroom_id
      WHERE c.teacher_id = auth.uid() AND s.role = 'student'
    )
  );

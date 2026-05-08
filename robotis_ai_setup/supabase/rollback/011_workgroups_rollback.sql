-- Rollback 011: drop workgroups, workgroup_memberships, datasets,
-- restore start_training_safe / get_remaining_credits / adjust_student_credits /
-- get_teacher_credit_summary to their state after migration 010.
--
-- WARNING: This deletes all workgroup data, all workgroup memberships,
-- and the datasets registry. trainings.workgroup_id and workflows.workgroup_id
-- are dropped (cascading from the workgroups table delete via SET NULL is not
-- enough because we drop the columns themselves to fully revert).

-- ---------------------------------------------------------------------------
-- 1. Realtime publication: remove the new tables
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_publication_tables
     WHERE pubname = 'supabase_realtime'
       AND schemaname = 'public'
       AND tablename = 'workgroups'
  ) THEN
    ALTER PUBLICATION supabase_realtime DROP TABLE public.workgroups;
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_publication_tables
     WHERE pubname = 'supabase_realtime'
       AND schemaname = 'public'
       AND tablename = 'datasets'
  ) THEN
    ALTER PUBLICATION supabase_realtime DROP TABLE public.datasets;
  END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- 2. Drop the new RLS policies (group-member reads + dataset policies)
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS "Group members read group trainings" ON public.trainings;
DROP POLICY IF EXISTS "Group members read group workflows" ON public.workflows;

-- progress_entries: revert student-read policy to the migration-004 form
DROP POLICY IF EXISTS "Students read own + own-classroom entries" ON public.progress_entries;
CREATE POLICY "Students read own + own-classroom entries"
  ON public.progress_entries FOR SELECT
  USING (
    student_id = auth.uid()
    OR (student_id IS NULL
        AND classroom_id = (SELECT classroom_id FROM public.users WHERE id = auth.uid()))
  );

-- ---------------------------------------------------------------------------
-- 3. Drop the workgroup_id columns (cascade indexes/constraints)
-- ---------------------------------------------------------------------------
DROP INDEX IF EXISTS uniq_progress_entries_workgroup_day;

-- Restore the original classroom-wide index (no workgroup_id check needed
-- since the column will be gone).
DROP INDEX IF EXISTS uniq_progress_entries_classroom_day;
ALTER TABLE public.progress_entries
  DROP CONSTRAINT IF EXISTS chk_progress_scope;
ALTER TABLE public.progress_entries
  DROP COLUMN IF EXISTS workgroup_id;
CREATE UNIQUE INDEX IF NOT EXISTS uniq_progress_entries_classroom_day
  ON public.progress_entries(classroom_id, entry_date)
  WHERE student_id IS NULL;

ALTER TABLE public.workflows DROP COLUMN IF EXISTS workgroup_id;
ALTER TABLE public.trainings DROP COLUMN IF EXISTS workgroup_id;
ALTER TABLE public.users DROP COLUMN IF EXISTS workgroup_id;

-- ---------------------------------------------------------------------------
-- 4. Drop datasets, workgroup_memberships, workgroups
-- ---------------------------------------------------------------------------
DROP TRIGGER IF EXISTS trg_datasets_touch ON public.datasets;
DROP TABLE IF EXISTS public.datasets;
DROP TABLE IF EXISTS public.workgroup_memberships;
DROP TRIGGER IF EXISTS trg_workgroups_touch ON public.workgroups;
DROP TABLE IF EXISTS public.workgroups;

-- ---------------------------------------------------------------------------
-- 5. Drop helper triggers + functions added by 011
-- ---------------------------------------------------------------------------
DROP TRIGGER IF EXISTS trg_workgroup_capacity ON public.users;
DROP TRIGGER IF EXISTS trg_workgroup_classroom_match ON public.users;
DROP FUNCTION IF EXISTS public.enforce_workgroup_capacity();
DROP FUNCTION IF EXISTS public.enforce_workgroup_classroom_match();
DROP FUNCTION IF EXISTS public.adjust_workgroup_credits(UUID, UUID, INTEGER);

-- ---------------------------------------------------------------------------
-- 6. Restore RPCs to post-010 form
-- ---------------------------------------------------------------------------
DROP FUNCTION IF EXISTS public.start_training_safe(UUID, TEXT, TEXT, TEXT, JSONB, INT, UUID);
CREATE OR REPLACE FUNCTION public.start_training_safe(
  p_user_id         UUID,
  p_dataset_name    TEXT,
  p_model_name      TEXT,
  p_model_type      TEXT,
  p_training_params JSONB,
  p_total_steps     INT,
  p_worker_token    UUID
) RETURNS TABLE(training_id INT, remaining INT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_credits INT;
  v_used    INT;
  v_new_id  INT;
BEGIN
  SELECT training_credits INTO v_credits
  FROM public.users
  WHERE id = p_user_id
  FOR UPDATE;

  IF v_credits IS NULL THEN
    RAISE EXCEPTION 'User profile not found' USING ERRCODE = 'P0002';
  END IF;

  SELECT COUNT(*) INTO v_used
  FROM public.trainings
  WHERE user_id = p_user_id
    AND status NOT IN ('failed', 'canceled');

  IF v_used >= v_credits THEN
    RAISE EXCEPTION 'No training credits remaining' USING ERRCODE = 'P0003';
  END IF;

  INSERT INTO public.trainings(
    user_id, status, dataset_name, model_name, model_type,
    training_params, total_steps, worker_token
  ) VALUES (
    p_user_id, 'queued', p_dataset_name, p_model_name, p_model_type,
    p_training_params, p_total_steps, p_worker_token
  )
  RETURNING id INTO v_new_id;

  RETURN QUERY SELECT v_new_id, (v_credits - v_used - 1);
END;
$$;
REVOKE ALL ON FUNCTION public.start_training_safe(UUID, TEXT, TEXT, TEXT, JSONB, INT, UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.start_training_safe(UUID, TEXT, TEXT, TEXT, JSONB, INT, UUID) TO service_role;

DROP FUNCTION IF EXISTS public.get_remaining_credits(UUID);
CREATE OR REPLACE FUNCTION public.get_remaining_credits(p_user_id UUID)
RETURNS TABLE(training_credits INTEGER, trainings_used BIGINT, remaining BIGINT)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  RETURN QUERY
  SELECT
    u.training_credits,
    COUNT(t.id) FILTER (WHERE t.status NOT IN ('failed', 'canceled')) AS trainings_used,
    u.training_credits::BIGINT - COUNT(t.id) FILTER (WHERE t.status NOT IN ('failed', 'canceled')) AS remaining
  FROM public.users u
  LEFT JOIN public.trainings t ON t.user_id = u.id
  WHERE u.id = p_user_id
  GROUP BY u.id, u.training_credits;
END;
$$;

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

DROP FUNCTION IF EXISTS public.get_teacher_credit_summary(UUID);
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

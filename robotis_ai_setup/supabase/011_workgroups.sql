-- 011_workgroups.sql
--
-- Work groups (Arbeitsgruppen) inside classrooms.
--
-- A work group bundles 2-N students who share a single training credit
-- pool and see each other's trainings, datasets, and Roboter Studio
-- workflows. The group lives inside one classroom; a student is in at
-- most one group per classroom (FK on users.workgroup_id).
--
-- Design choices (confirmed with user before writing):
--   1. Group-only credits. While in a group, users.training_credits is
--      ignored — start_training_safe() consumes workgroups.shared_credits.
--      get_remaining_credits() reports group quota when grouped.
--   2. Sharing scope: trainings + datasets + workflows + group-wide
--      progress entries.
--   3. One group max per student per classroom.
--   4. Lifecycle: trainings/datasets stay visible to former group members
--      via a workgroup_memberships audit table (left_at IS NOT NULL).
--      On group delete, ON DELETE SET NULL on trainings/workflows/datasets
--      preserves history; allocated credits return to the teacher pool
--      naturally (the summary RPC sums shared_credits, so removing the
--      row drops the allocation).
--
-- Apply order: migration.sql -> 002 -> 004 -> 005 -> 006 -> 007 -> 008 -> 009 -> 010 -> THIS.
-- Rollback: see rollback/011_workgroups_rollback.sql.

-- ---------------------------------------------------------------------------
-- 1. workgroups + workgroup_memberships tables
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.workgroups (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  classroom_id UUID NOT NULL REFERENCES public.classrooms(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  shared_credits INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (classroom_id, name)
);

COMMENT ON TABLE public.workgroups IS
  'Arbeitsgruppen: a subset of students inside one classroom that share '
  'training credits, trainings, datasets, and workflows.';

CREATE INDEX IF NOT EXISTS idx_workgroups_classroom
  ON public.workgroups(classroom_id);

-- Audit table: tracks current and past membership so visibility outlives
-- removal (per user-confirmed lifecycle rule).
CREATE TABLE IF NOT EXISTS public.workgroup_memberships (
  workgroup_id UUID NOT NULL REFERENCES public.workgroups(id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  left_at TIMESTAMPTZ,
  PRIMARY KEY (workgroup_id, user_id)
);

COMMENT ON TABLE public.workgroup_memberships IS
  'Append-only-ish audit of group membership. left_at NULL = currently in group. '
  'Used by the trainings/workflows/datasets read policies so a removed student '
  'can still see history of their former group.';

CREATE INDEX IF NOT EXISTS idx_workgroup_memberships_user
  ON public.workgroup_memberships(user_id);

-- ---------------------------------------------------------------------------
-- 2. workgroup_id columns on existing tables
-- ---------------------------------------------------------------------------
ALTER TABLE public.users
  ADD COLUMN IF NOT EXISTS workgroup_id UUID REFERENCES public.workgroups(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_users_workgroup
  ON public.users(workgroup_id) WHERE workgroup_id IS NOT NULL;

ALTER TABLE public.trainings
  ADD COLUMN IF NOT EXISTS workgroup_id UUID REFERENCES public.workgroups(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_trainings_workgroup
  ON public.trainings(workgroup_id, requested_at DESC) WHERE workgroup_id IS NOT NULL;

ALTER TABLE public.workflows
  ADD COLUMN IF NOT EXISTS workgroup_id UUID REFERENCES public.workgroups(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_workflows_workgroup
  ON public.workflows(workgroup_id, updated_at DESC) WHERE workgroup_id IS NOT NULL;

ALTER TABLE public.progress_entries
  ADD COLUMN IF NOT EXISTS workgroup_id UUID REFERENCES public.workgroups(id) ON DELETE CASCADE;

-- A progress entry is scoped to AT MOST one of {student, workgroup}.
-- (Both NULL = classroom-wide; both NOT NULL is rejected.)
ALTER TABLE public.progress_entries
  DROP CONSTRAINT IF EXISTS chk_progress_scope;
ALTER TABLE public.progress_entries
  ADD CONSTRAINT chk_progress_scope
  CHECK (NOT (student_id IS NOT NULL AND workgroup_id IS NOT NULL));

-- Tighten the existing classroom-wide unique index so it only fires when
-- BOTH scope columns are NULL. Add a new partial unique index for the
-- per-group-per-day scope.
DROP INDEX IF EXISTS uniq_progress_entries_classroom_day;
CREATE UNIQUE INDEX IF NOT EXISTS uniq_progress_entries_classroom_day
  ON public.progress_entries(classroom_id, entry_date)
  WHERE student_id IS NULL AND workgroup_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uniq_progress_entries_workgroup_day
  ON public.progress_entries(workgroup_id, entry_date)
  WHERE workgroup_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 3. datasets registry (new)
-- ---------------------------------------------------------------------------
-- Nothing tracked HF datasets in the DB before this migration. This table
-- exists so group siblings can discover datasets uploaded by other group
-- members (the HF repos themselves stay under each student's HF user).
CREATE TABLE IF NOT EXISTS public.datasets (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  workgroup_id UUID REFERENCES public.workgroups(id) ON DELETE SET NULL,
  hf_repo_id TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  episode_count INTEGER,
  total_frames INTEGER,
  fps INTEGER,
  robot_type TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (owner_user_id, hf_repo_id)
);

COMMENT ON TABLE public.datasets IS
  'Discovery registry for HF Hub datasets uploaded by students. The HF repo '
  'itself is owned by the student''s HF user. workgroup_id auto-shares the '
  'dataset with the owner''s current group at registration time.';

CREATE INDEX IF NOT EXISTS idx_datasets_owner
  ON public.datasets(owner_user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_datasets_workgroup
  ON public.datasets(workgroup_id, updated_at DESC) WHERE workgroup_id IS NOT NULL;

-- updated_at trigger (reuses the helper from migration 004/008).
DROP TRIGGER IF EXISTS trg_workgroups_touch ON public.workgroups;
CREATE TRIGGER trg_workgroups_touch
BEFORE UPDATE ON public.workgroups
FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

DROP TRIGGER IF EXISTS trg_datasets_touch ON public.datasets;
CREATE TRIGGER trg_datasets_touch
BEFORE UPDATE ON public.datasets
FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

-- ---------------------------------------------------------------------------
-- 4. Triggers: workgroup capacity + classroom-match
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.enforce_workgroup_capacity()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public
AS $$
DECLARE
  v_count INT;
BEGIN
  IF NEW.workgroup_id IS NULL THEN
    RETURN NEW;
  END IF;
  SELECT COUNT(*) INTO v_count
  FROM public.users
  WHERE workgroup_id = NEW.workgroup_id
    AND (TG_OP = 'INSERT' OR id <> NEW.id);
  IF v_count >= 10 THEN
    RAISE EXCEPTION 'Arbeitsgruppe ist voll (max 10 Schueler)' USING ERRCODE = 'P0021';
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_workgroup_capacity ON public.users;
CREATE TRIGGER trg_workgroup_capacity
BEFORE INSERT OR UPDATE OF workgroup_id ON public.users
FOR EACH ROW EXECUTE FUNCTION public.enforce_workgroup_capacity();

CREATE OR REPLACE FUNCTION public.enforce_workgroup_classroom_match()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public
AS $$
DECLARE
  v_group_classroom UUID;
BEGIN
  IF NEW.workgroup_id IS NULL THEN
    RETURN NEW;
  END IF;
  SELECT classroom_id INTO v_group_classroom
  FROM public.workgroups WHERE id = NEW.workgroup_id;
  IF v_group_classroom IS NULL OR v_group_classroom <> NEW.classroom_id THEN
    RAISE EXCEPTION 'Schueler ist nicht im selben Klassenzimmer wie die Gruppe'
      USING ERRCODE = 'P0020';
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_workgroup_classroom_match ON public.users;
CREATE TRIGGER trg_workgroup_classroom_match
BEFORE INSERT OR UPDATE OF workgroup_id, classroom_id ON public.users
FOR EACH ROW EXECUTE FUNCTION public.enforce_workgroup_classroom_match();

-- ---------------------------------------------------------------------------
-- 5. RPC redefines: start_training_safe + get_remaining_credits
-- ---------------------------------------------------------------------------
-- Drop old signatures so we can change the return-table shape if needed.
DROP FUNCTION IF EXISTS public.start_training_safe(UUID, TEXT, TEXT, TEXT, JSONB, INT, UUID);
DROP FUNCTION IF EXISTS public.get_remaining_credits(UUID);

-- Group-aware credit check + insert. Locks workgroups.shared_credits FOR
-- UPDATE for grouped students; locks users.training_credits FOR UPDATE for
-- ungrouped. Same return shape as before — callers do not branch.
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
  v_workgroup_id UUID;
  v_credits      INT;
  v_used         INT;
  v_new_id       INT;
BEGIN
  SELECT workgroup_id INTO v_workgroup_id
  FROM public.users WHERE id = p_user_id;

  IF v_workgroup_id IS NOT NULL THEN
    -- Grouped: lock the workgroup row, count active group trainings.
    SELECT shared_credits INTO v_credits
    FROM public.workgroups
    WHERE id = v_workgroup_id
    FOR UPDATE;

    IF v_credits IS NULL THEN
      RAISE EXCEPTION 'Arbeitsgruppe nicht gefunden' USING ERRCODE = 'P0002';
    END IF;

    SELECT COUNT(*) INTO v_used
    FROM public.trainings
    WHERE workgroup_id = v_workgroup_id
      AND status NOT IN ('failed', 'canceled');

    IF v_used >= v_credits THEN
      RAISE EXCEPTION 'No training credits remaining' USING ERRCODE = 'P0003';
    END IF;

    INSERT INTO public.trainings(
      user_id, workgroup_id, status, dataset_name, model_name, model_type,
      training_params, total_steps, worker_token
    ) VALUES (
      p_user_id, v_workgroup_id, 'queued', p_dataset_name, p_model_name, p_model_type,
      p_training_params, p_total_steps, p_worker_token
    )
    RETURNING id INTO v_new_id;

    RETURN QUERY SELECT v_new_id, (v_credits - v_used - 1);
  ELSE
    -- Ungrouped: original per-user logic.
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
  END IF;
END;
$$;

REVOKE ALL ON FUNCTION public.start_training_safe(UUID, TEXT, TEXT, TEXT, JSONB, INT, UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.start_training_safe(UUID, TEXT, TEXT, TEXT, JSONB, INT, UUID) TO service_role;

-- Group-aware quota. Returns group quota when grouped, per-user otherwise.
CREATE OR REPLACE FUNCTION public.get_remaining_credits(p_user_id UUID)
RETURNS TABLE(training_credits INTEGER, trainings_used BIGINT, remaining BIGINT)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_workgroup_id UUID;
BEGIN
  SELECT workgroup_id INTO v_workgroup_id
  FROM public.users WHERE id = p_user_id;

  IF v_workgroup_id IS NOT NULL THEN
    RETURN QUERY
    SELECT
      g.shared_credits,
      COUNT(t.id) FILTER (WHERE t.status NOT IN ('failed', 'canceled')) AS trainings_used,
      g.shared_credits::BIGINT - COUNT(t.id) FILTER (WHERE t.status NOT IN ('failed', 'canceled')) AS remaining
    FROM public.workgroups g
    LEFT JOIN public.trainings t ON t.workgroup_id = g.id
    WHERE g.id = v_workgroup_id
    GROUP BY g.id, g.shared_credits;
  ELSE
    RETURN QUERY
    SELECT
      u.training_credits,
      COUNT(t.id) FILTER (WHERE t.status NOT IN ('failed', 'canceled')) AS trainings_used,
      u.training_credits::BIGINT - COUNT(t.id) FILTER (WHERE t.status NOT IN ('failed', 'canceled')) AS remaining
    FROM public.users u
    LEFT JOIN public.trainings t ON t.user_id = u.id AND t.workgroup_id IS NULL
    WHERE u.id = p_user_id
    GROUP BY u.id, u.training_credits;
  END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- 6. RPC: adjust_workgroup_credits (mirrors adjust_student_credits)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.adjust_workgroup_credits(
  p_teacher_id   UUID,
  p_workgroup_id UUID,
  p_delta        INTEGER
) RETURNS TABLE (new_amount INTEGER, pool_available BIGINT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_teacher_total       INTEGER;
  v_classroom_owned     BOOLEAN;
  v_group_current       INTEGER;
  v_group_used          BIGINT;
  v_new_amount          INTEGER;
  v_allocated_others    BIGINT;
BEGIN
  -- Validate teacher owns the workgroup's classroom.
  SELECT EXISTS (
    SELECT 1 FROM public.workgroups g
    JOIN public.classrooms c ON c.id = g.classroom_id
    WHERE g.id = p_workgroup_id AND c.teacher_id = p_teacher_id
  ) INTO v_classroom_owned;

  IF NOT v_classroom_owned THEN
    RAISE EXCEPTION 'Arbeitsgruppe gehoert nicht zu diesem Lehrer' USING ERRCODE = 'P0022';
  END IF;

  SELECT training_credits INTO v_teacher_total
  FROM public.users WHERE id = p_teacher_id FOR UPDATE;

  SELECT shared_credits INTO v_group_current
  FROM public.workgroups WHERE id = p_workgroup_id FOR UPDATE;

  v_new_amount := v_group_current + p_delta;

  -- Cannot reduce below currently-active trainings count for this group.
  SELECT COUNT(*) INTO v_group_used
  FROM public.trainings
  WHERE workgroup_id = p_workgroup_id
    AND status NOT IN ('failed', 'canceled');

  IF v_new_amount < v_group_used THEN
    RAISE EXCEPTION 'Neuer Betrag (%) ist kleiner als bereits verbrauchte Credits (%)',
      v_new_amount, v_group_used USING ERRCODE = 'P0012';
  END IF;

  IF v_new_amount < 0 THEN
    RAISE EXCEPTION 'Credits duerfen nicht negativ werden' USING ERRCODE = 'P0013';
  END IF;

  -- Pool check: teacher.training_credits >= sum(student credits) +
  -- sum(other workgroup credits) + new amount.
  SELECT
    COALESCE(SUM(s.training_credits), 0) +
    COALESCE((
      SELECT SUM(g.shared_credits) FROM public.workgroups g
      JOIN public.classrooms c ON c.id = g.classroom_id
      WHERE c.teacher_id = p_teacher_id AND g.id <> p_workgroup_id
    ), 0)
  INTO v_allocated_others
  FROM public.users s
  JOIN public.classrooms c ON c.id = s.classroom_id
  WHERE c.teacher_id = p_teacher_id AND s.role = 'student';

  IF v_allocated_others + v_new_amount > v_teacher_total THEN
    RAISE EXCEPTION 'Lehrer hat nicht genug Credits im Pool' USING ERRCODE = 'P0014';
  END IF;

  UPDATE public.workgroups
  SET shared_credits = v_new_amount, updated_at = NOW()
  WHERE id = p_workgroup_id;

  RETURN QUERY SELECT v_new_amount, (v_teacher_total - v_allocated_others - v_new_amount)::BIGINT;
END;
$$;

REVOKE ALL ON FUNCTION public.adjust_workgroup_credits(UUID, UUID, INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.adjust_workgroup_credits(UUID, UUID, INTEGER) TO service_role;

-- ---------------------------------------------------------------------------
-- 7. Refuse adjust_student_credits when student is grouped (P0023)
-- ---------------------------------------------------------------------------
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
  v_student_workgroup UUID;
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

  -- Refuse if student is in a group — credits are managed via the group.
  SELECT workgroup_id INTO v_student_workgroup
  FROM public.users WHERE id = p_student_id;
  IF v_student_workgroup IS NOT NULL THEN
    RAISE EXCEPTION 'Schueler ist in einer Arbeitsgruppe — Credits ueber die Gruppe verwalten'
      USING ERRCODE = 'P0023';
  END IF;

  SELECT training_credits INTO v_teacher_total
  FROM public.users WHERE id = p_teacher_id FOR UPDATE;

  SELECT training_credits INTO v_student_current
  FROM public.users WHERE id = p_student_id FOR UPDATE;

  v_new_amount := v_student_current + p_delta;

  SELECT COUNT(*) INTO v_student_used
  FROM public.trainings
  WHERE user_id = p_student_id AND workgroup_id IS NULL
    AND status NOT IN ('failed','canceled');

  IF v_new_amount < v_student_used THEN
    RAISE EXCEPTION 'Neuer Betrag (%) ist kleiner als bereits verbrauchte Credits (%)',
      v_new_amount, v_student_used USING ERRCODE = 'P0012';
  END IF;

  IF v_new_amount < 0 THEN
    RAISE EXCEPTION 'Credits duerfen nicht negativ werden' USING ERRCODE = 'P0013';
  END IF;

  -- Pool check includes other students' credits AND all workgroup pools.
  SELECT
    COALESCE(SUM(s.training_credits), 0) +
    COALESCE((
      SELECT SUM(g.shared_credits) FROM public.workgroups g
      JOIN public.classrooms c2 ON c2.id = g.classroom_id
      WHERE c2.teacher_id = p_teacher_id
    ), 0)
  INTO v_allocated_others
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

-- ---------------------------------------------------------------------------
-- 8. Extend get_teacher_credit_summary to include workgroup pools
-- ---------------------------------------------------------------------------
DROP FUNCTION IF EXISTS public.get_teacher_credit_summary(UUID);
CREATE OR REPLACE FUNCTION public.get_teacher_credit_summary(p_teacher_id UUID)
RETURNS TABLE (
  pool_total          INTEGER,
  allocated_total     BIGINT,
  pool_available      BIGINT,
  student_count       BIGINT,
  group_count         BIGINT,
  group_credits_total BIGINT
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_teacher_total       INTEGER;
  v_student_alloc       BIGINT;
  v_student_count       BIGINT;
  v_group_alloc         BIGINT;
  v_group_count         BIGINT;
BEGIN
  SELECT training_credits INTO v_teacher_total
  FROM public.users
  WHERE id = p_teacher_id AND role = 'teacher';

  IF v_teacher_total IS NULL THEN
    RETURN; -- empty set if not a teacher
  END IF;

  SELECT
    COALESCE(SUM(s.training_credits), 0)::BIGINT,
    COUNT(s.id)::BIGINT
  INTO v_student_alloc, v_student_count
  FROM public.users s
  JOIN public.classrooms c ON c.id = s.classroom_id
  WHERE c.teacher_id = p_teacher_id AND s.role = 'student';

  SELECT
    COALESCE(SUM(g.shared_credits), 0)::BIGINT,
    COUNT(g.id)::BIGINT
  INTO v_group_alloc, v_group_count
  FROM public.workgroups g
  JOIN public.classrooms c ON c.id = g.classroom_id
  WHERE c.teacher_id = p_teacher_id;

  RETURN QUERY
  SELECT
    v_teacher_total,
    (v_student_alloc + v_group_alloc),
    (v_teacher_total - v_student_alloc - v_group_alloc),
    v_student_count,
    v_group_count,
    v_group_alloc;
END;
$$;

REVOKE ALL ON FUNCTION public.get_teacher_credit_summary(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.get_teacher_credit_summary(UUID) TO service_role;

-- ---------------------------------------------------------------------------
-- 9. RLS — workgroups + workgroup_memberships + datasets + group visibility
-- ---------------------------------------------------------------------------
ALTER TABLE public.workgroups ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.workgroup_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.datasets ENABLE ROW LEVEL SECURITY;

-- workgroups
DROP POLICY IF EXISTS "Teacher reads own workgroups" ON public.workgroups;
CREATE POLICY "Teacher reads own workgroups"
  ON public.workgroups FOR SELECT
  USING (classroom_id IN (
    SELECT id FROM public.classrooms WHERE teacher_id = auth.uid()
  ));

DROP POLICY IF EXISTS "Member reads own workgroup" ON public.workgroups;
CREATE POLICY "Member reads own workgroup"
  ON public.workgroups FOR SELECT
  USING (id = (SELECT workgroup_id FROM public.users WHERE id = auth.uid()));

DROP POLICY IF EXISTS "Admin reads all workgroups" ON public.workgroups;
CREATE POLICY "Admin reads all workgroups"
  ON public.workgroups FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.users WHERE id = auth.uid() AND role = 'admin'));

-- workgroup_memberships
DROP POLICY IF EXISTS "Member reads own membership rows" ON public.workgroup_memberships;
CREATE POLICY "Member reads own membership rows"
  ON public.workgroup_memberships FOR SELECT
  USING (user_id = auth.uid());

DROP POLICY IF EXISTS "Teacher reads owned-classroom memberships" ON public.workgroup_memberships;
CREATE POLICY "Teacher reads owned-classroom memberships"
  ON public.workgroup_memberships FOR SELECT
  USING (workgroup_id IN (
    SELECT g.id FROM public.workgroups g
    JOIN public.classrooms c ON c.id = g.classroom_id
    WHERE c.teacher_id = auth.uid()
  ));

DROP POLICY IF EXISTS "Admin reads all memberships" ON public.workgroup_memberships;
CREATE POLICY "Admin reads all memberships"
  ON public.workgroup_memberships FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.users WHERE id = auth.uid() AND role = 'admin'));

-- Group-member visibility on trainings (covers former members too via
-- workgroup_memberships, including rows where left_at IS NOT NULL).
DROP POLICY IF EXISTS "Group members read group trainings" ON public.trainings;
CREATE POLICY "Group members read group trainings"
  ON public.trainings FOR SELECT
  USING (
    workgroup_id IS NOT NULL AND EXISTS (
      SELECT 1 FROM public.workgroup_memberships m
      WHERE m.user_id = auth.uid() AND m.workgroup_id = trainings.workgroup_id
    )
  );

DROP POLICY IF EXISTS "Group members read group workflows" ON public.workflows;
CREATE POLICY "Group members read group workflows"
  ON public.workflows FOR SELECT
  USING (
    workgroup_id IS NOT NULL AND EXISTS (
      SELECT 1 FROM public.workgroup_memberships m
      WHERE m.user_id = auth.uid() AND m.workgroup_id = workflows.workgroup_id
    )
  );

-- datasets policies
DROP POLICY IF EXISTS "Owner reads own datasets" ON public.datasets;
CREATE POLICY "Owner reads own datasets"
  ON public.datasets FOR SELECT
  USING (owner_user_id = auth.uid());

DROP POLICY IF EXISTS "Group members read group datasets" ON public.datasets;
CREATE POLICY "Group members read group datasets"
  ON public.datasets FOR SELECT
  USING (
    workgroup_id IS NOT NULL AND EXISTS (
      SELECT 1 FROM public.workgroup_memberships m
      WHERE m.user_id = auth.uid() AND m.workgroup_id = datasets.workgroup_id
    )
  );

DROP POLICY IF EXISTS "Teacher reads classroom datasets" ON public.datasets;
CREATE POLICY "Teacher reads classroom datasets"
  ON public.datasets FOR SELECT
  USING (
    workgroup_id IN (
      SELECT g.id FROM public.workgroups g
      JOIN public.classrooms c ON c.id = g.classroom_id
      WHERE c.teacher_id = auth.uid()
    )
  );

DROP POLICY IF EXISTS "Admin reads all datasets" ON public.datasets;
CREATE POLICY "Admin reads all datasets"
  ON public.datasets FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.users WHERE id = auth.uid() AND role = 'admin'));

DROP POLICY IF EXISTS "Owner inserts own datasets" ON public.datasets;
CREATE POLICY "Owner inserts own datasets"
  ON public.datasets FOR INSERT
  WITH CHECK (owner_user_id = auth.uid());

DROP POLICY IF EXISTS "Owner updates own datasets" ON public.datasets;
CREATE POLICY "Owner updates own datasets"
  ON public.datasets FOR UPDATE
  USING (owner_user_id = auth.uid())
  WITH CHECK (owner_user_id = auth.uid());

DROP POLICY IF EXISTS "Owner deletes own datasets" ON public.datasets;
CREATE POLICY "Owner deletes own datasets"
  ON public.datasets FOR DELETE
  USING (owner_user_id = auth.uid());

-- progress_entries: extend students-read so they see their group's entries
DROP POLICY IF EXISTS "Students read own + own-classroom entries" ON public.progress_entries;
CREATE POLICY "Students read own + own-classroom entries"
  ON public.progress_entries FOR SELECT
  USING (
    student_id = auth.uid()
    OR (student_id IS NULL AND workgroup_id IS NULL
        AND classroom_id = (SELECT classroom_id FROM public.users WHERE id = auth.uid()))
    OR (workgroup_id IS NOT NULL AND EXISTS (
      SELECT 1 FROM public.workgroup_memberships m
      WHERE m.user_id = auth.uid() AND m.workgroup_id = progress_entries.workgroup_id
    ))
  );

-- ---------------------------------------------------------------------------
-- 10. Realtime publication adds
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_publication_tables
     WHERE pubname = 'supabase_realtime'
       AND schemaname = 'public'
       AND tablename = 'workgroups'
  ) THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.workgroups;
  END IF;
END
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_publication_tables
     WHERE pubname = 'supabase_realtime'
       AND schemaname = 'public'
       AND tablename = 'datasets'
  ) THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.datasets;
  END IF;
END
$$;

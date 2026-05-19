-- ============================================================================
-- 00000000000000_baseline.sql
-- ----------------------------------------------------------------------------
-- Squashed baseline migration for the EduBotics Supabase project
-- (fnnbysrjkfugsqzwcksd, "edubotics", eu-west-1, Postgres 17).
--
-- This file is the union of every numbered repo migration in chronological
-- order, PLUS the live-state patches that were applied via the Supabase
-- dashboard / MCP (008_workflows_harden_search_path, 013b, 015b, 017b)
-- which never had their own .sql file.
--
-- The 21 original repo files (migration.sql + 002..021 except 014) are
-- preserved verbatim in robotis_ai_setup/supabase/_archive/ for git-blame.
--
-- After this baseline is merged, run ONCE:
--     supabase link --project-ref fnnbysrjkfugsqzwcksd
--     supabase migration repair --status applied 00000000000000
--
-- That tells the CLI the baseline is already applied to production. From
-- then on, only NEW files under robotis_ai_setup/supabase/migrations/ run
-- via `supabase db push`.
--
-- 003_lessons_and_notes is intentionally OMITTED — it was superseded by
-- 004_progress_entries which drops 003's tables. 014 is intentionally
-- omitted — the slot is reserved (filename-collision hazard with 014b).
--
-- Generated: 2026-05-19
-- ============================================================================


-- ============================================================
-- BEGIN: migration.sql
-- ============================================================
-- EduBotics Cloud Training - Supabase Schema
-- Run this in the Supabase SQL Editor (Dashboard > SQL Editor > New Query)

-- 1. Users table (linked to Supabase Auth)
CREATE TABLE public.users (
  id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  email TEXT NOT NULL,
  training_credits INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 2. Auto-create user row on signup
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER AS $$
BEGIN
  INSERT INTO public.users (id, email, training_credits)
  VALUES (NEW.id, NEW.email, 0);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- 3. Trainings table
CREATE TABLE public.trainings (
  id SERIAL PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES public.users(id),
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','running','succeeded','failed','canceled')),
  dataset_name TEXT NOT NULL,
  model_name TEXT NOT NULL,
  model_type TEXT NOT NULL,
  training_params JSONB,
  cloud_job_id TEXT,
  current_step INTEGER DEFAULT 0,
  total_steps INTEGER DEFAULT 0,
  current_loss REAL,
  requested_at TIMESTAMPTZ DEFAULT NOW(),
  terminated_at TIMESTAMPTZ,
  error_message TEXT,
  -- Per-training secret. Only the API and the assigned cloud worker know it.
  -- Used by update_training_progress() to scope worker DB access to this row.
  worker_token UUID,
  -- Liveness marker, bumped on every update_training_progress() call.
  -- The reconciler uses it to spot wedged workers (dispatcher still says
  -- IN_PROGRESS but no progress for >N minutes).
  last_progress_at TIMESTAMPTZ
);

-- 4. Row Level Security
ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.trainings ENABLE ROW LEVEL SECURITY;

-- Users can only read their own row
CREATE POLICY "Users read own profile"
  ON public.users FOR SELECT
  USING (auth.uid() = id);

CREATE POLICY "Users update own profile"
  ON public.users FOR UPDATE
  TO authenticated
  USING (auth.uid() = id)
  WITH CHECK (auth.uid() = id);

-- Users can only read their own trainings
CREATE POLICY "Users read own trainings"
  ON public.trainings FOR SELECT
  USING (auth.uid() = user_id);

-- Defense-in-depth write policies. The cloud API uses the service role
-- (which bypasses RLS), so these policies are dormant today but document the
-- intended access pattern and protect against accidental anon-key writes.
CREATE POLICY "Users insert own trainings"
  ON public.trainings FOR INSERT
  TO authenticated
  WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users update own trainings"
  ON public.trainings FOR UPDATE
  TO authenticated
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users delete own trainings"
  ON public.trainings FOR DELETE
  TO authenticated
  USING (auth.uid() = user_id);

-- 5. Derive remaining credits from actual trainings data
--    Credits are "used" by trainings with status NOT IN ('failed', 'canceled').
--    No counter to maintain — self-healing, no race conditions, no double-refund risk.
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

-- 6. Scoped progress-update RPC for cloud GPU workers
--    SECURITY DEFINER so it can write through RLS, but the WHERE clause requires
--    BOTH a valid id AND a matching worker_token. A leaked token only allows
--    progress updates on one specific row — never reads, never other rows,
--    never other tables. Token is invalidated on terminal status (defense in depth).
CREATE OR REPLACE FUNCTION public.update_training_progress(
  p_training_id  INT,
  p_token        UUID,
  p_status       TEXT  DEFAULT NULL,
  p_current_step INT   DEFAULT NULL,
  p_total_steps  INT   DEFAULT NULL,
  p_current_loss REAL  DEFAULT NULL,
  p_error_message TEXT DEFAULT NULL
) RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_rows INT;
BEGIN
  IF p_status IS NOT NULL AND p_status NOT IN ('queued','running','succeeded','failed','canceled') THEN
    RAISE EXCEPTION 'Invalid status: %', p_status USING ERRCODE = '22023';
  END IF;

  UPDATE public.trainings
  SET
    status        = COALESCE(p_status,        status),
    current_step  = COALESCE(p_current_step,  current_step),
    total_steps   = COALESCE(p_total_steps,   total_steps),
    current_loss  = COALESCE(p_current_loss,  current_loss),
    error_message = COALESCE(p_error_message, error_message),
    -- Liveness marker — any worker call counts as evidence-of-life.
    last_progress_at = NOW(),
    terminated_at = CASE
      WHEN p_status IN ('succeeded','failed','canceled') THEN NOW()
      ELSE terminated_at
    END,
    worker_token  = CASE
      WHEN p_status IN ('succeeded','failed','canceled') THEN NULL
      ELSE worker_token
    END
  WHERE id = p_training_id
    AND worker_token = p_token;

  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows = 0 THEN
    RAISE EXCEPTION 'Invalid worker token or training not found' USING ERRCODE = 'P0001';
  END IF;
END;
$$;

REVOKE ALL ON FUNCTION public.update_training_progress(INT, UUID, TEXT, INT, INT, REAL, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.update_training_progress(INT, UUID, TEXT, INT, INT, REAL, TEXT) TO anon, authenticated;

-- 6b. Atomic credit-check + training insert.
--     Replaces a TOCTOU race in the API: two concurrent /start calls could both
--     pass an out-of-transaction credit check before either inserted a row.
--     This function locks the user row (FOR UPDATE), counts active trainings,
--     and inserts the new row in a single transaction — concurrent calls for
--     the same user serialize on the lock; different users do not contend.
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

-- 7. Indexes for performance
CREATE INDEX idx_trainings_user_id ON public.trainings(user_id);
CREATE INDEX idx_trainings_status ON public.trainings(status);
CREATE INDEX idx_trainings_requested_at ON public.trainings(requested_at DESC);
CREATE INDEX idx_trainings_worker_token ON public.trainings(worker_token) WHERE worker_token IS NOT NULL;
-- Compound index for the dashboard's "list my running/queued trainings" query.
CREATE INDEX idx_trainings_user_id_status ON public.trainings(user_id, status);

-- END: migration.sql


-- ============================================================
-- BEGIN: 002_accounts.sql
-- ============================================================
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

-- END: 002_accounts.sql


-- ============================================================
-- BEGIN: 004_progress_entries.sql
-- ============================================================
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

-- END: 004_progress_entries.sql


-- ============================================================
-- BEGIN: 005_cloud_job_id.sql
-- ============================================================
-- 005: Rename runpod_job_id -> cloud_job_id after the RunPod -> Modal migration.
--
-- The column stores an opaque dispatcher job id (now a Modal FunctionCall
-- object_id). Renaming makes the schema vendor-neutral so any future dispatcher
-- swap is a code change, not a schema change.
--
-- Rollback (if needed):
--     ALTER TABLE public.trainings RENAME COLUMN cloud_job_id TO runpod_job_id;

ALTER TABLE public.trainings RENAME COLUMN runpod_job_id TO cloud_job_id;

COMMENT ON COLUMN public.trainings.cloud_job_id IS
  'Opaque job id returned by the cloud GPU dispatcher. '
  'Currently a Modal FunctionCall.object_id.';

COMMENT ON COLUMN public.trainings.worker_token IS
  'Per-training secret. Only the API and the assigned cloud worker know it. '
  'Used by update_training_progress() to scope worker DB access to this row.';

COMMENT ON COLUMN public.trainings.last_progress_at IS
  'Liveness marker, bumped on every update_training_progress() call. '
  'The reconciler uses it to spot wedged workers (dispatcher still says '
  'IN_PROGRESS but no progress for >N minutes).';

-- END: 005_cloud_job_id.sql


-- ============================================================
-- BEGIN: 006_loss_history.sql
-- ============================================================
-- 006: Live training loss history + realtime.
--
-- Two coupled changes that power the live training chart in the React SPA:
--
-- 1. New column `loss_history JSONB`. Each element is `{"s": step, "l": loss,
--    "t": ms_since_epoch}`. Short keys keep the row compact even at the 300-entry
--    cap (~15 KB upper bound). The update_training_progress() RPC appends a new
--    point on every (step, loss) call and downsamples once the array exceeds 300
--    entries — worker code does not need to change.
--
--    Downsampling strategy: keep the first entry, 199 evenly-spaced middle
--    entries, and the last 100 entries. Preserves curve shape at any zoom while
--    giving the most detail to the recent tail (what students watch).
--
-- 2. Add `public.trainings` to the `supabase_realtime` publication so the
--    frontend can subscribe to `postgres_changes` and react to progress the
--    instant it lands, rather than polling every 5 seconds.

-- ---------------------------------------------------------------------------
-- 1. loss_history column
-- ---------------------------------------------------------------------------
ALTER TABLE public.trainings
  ADD COLUMN IF NOT EXISTS loss_history JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN public.trainings.loss_history IS
  'Downsampled training loss curve: array of {"s": step, "l": loss, "t": ms}. '
  'Maintained by update_training_progress(), capped at <=300 entries.';

-- ---------------------------------------------------------------------------
-- 2. Rewrite update_training_progress() to append + downsample loss_history.
--    Signature is unchanged — workers keep calling it with the same args.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.update_training_progress(
  p_training_id  INT,
  p_token        UUID,
  p_status       TEXT  DEFAULT NULL,
  p_current_step INT   DEFAULT NULL,
  p_total_steps  INT   DEFAULT NULL,
  p_current_loss REAL  DEFAULT NULL,
  p_error_message TEXT DEFAULT NULL
) RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_rows        INT;
  v_new_history JSONB;
  v_len         INT;
BEGIN
  IF p_status IS NOT NULL AND p_status NOT IN ('queued','running','succeeded','failed','canceled') THEN
    RAISE EXCEPTION 'Invalid status: %', p_status USING ERRCODE = '22023';
  END IF;

  -- Append a new point if we have a (step, loss) pair, then downsample if over cap.
  IF p_current_step IS NOT NULL AND p_current_loss IS NOT NULL THEN
    SELECT COALESCE(loss_history, '[]'::jsonb) || jsonb_build_array(
             jsonb_build_object(
               's', p_current_step,
               'l', p_current_loss,
               't', (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT
             )
           )
      INTO v_new_history
      FROM public.trainings
     WHERE id = p_training_id;

    v_len := jsonb_array_length(v_new_history);
    IF v_len > 300 THEN
      SELECT jsonb_agg(elem ORDER BY idx)
        INTO v_new_history
        FROM (
          -- First entry.
          SELECT 0 AS idx, v_new_history -> 0 AS elem
          UNION
          -- 199 evenly-spaced middle entries covering [1, v_len-101].
          SELECT (1 + s * (v_len - 102.0) / 198.0)::INT,
                 v_new_history -> (1 + s * (v_len - 102.0) / 198.0)::INT
            FROM generate_series(0, 198) AS s
          UNION
          -- Last 100 entries (the fresh tail students are watching).
          SELECT v_len - 100 + s,
                 v_new_history -> (v_len - 100 + s)
            FROM generate_series(0, 99) AS s
        ) sampled;
    END IF;
  END IF;

  UPDATE public.trainings
  SET
    status        = COALESCE(p_status,        status),
    current_step  = COALESCE(p_current_step,  current_step),
    total_steps   = COALESCE(p_total_steps,   total_steps),
    current_loss  = COALESCE(p_current_loss,  current_loss),
    error_message = COALESCE(p_error_message, error_message),
    loss_history  = COALESCE(v_new_history,   loss_history),
    last_progress_at = NOW(),
    terminated_at = CASE
      WHEN p_status IN ('succeeded','failed','canceled') THEN NOW()
      ELSE terminated_at
    END,
    worker_token  = CASE
      WHEN p_status IN ('succeeded','failed','canceled') THEN NULL
      ELSE worker_token
    END
  WHERE id = p_training_id
    AND worker_token = p_token;

  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows = 0 THEN
    RAISE EXCEPTION 'Invalid worker token or training not found' USING ERRCODE = 'P0001';
  END IF;
END;
$$;

REVOKE ALL ON FUNCTION public.update_training_progress(INT, UUID, TEXT, INT, INT, REAL, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.update_training_progress(INT, UUID, TEXT, INT, INT, REAL, TEXT) TO anon, authenticated;

-- ---------------------------------------------------------------------------
-- 3. Enable Supabase Realtime on public.trainings.
--    Guarded so re-running the migration is a no-op.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_publication_tables
     WHERE pubname = 'supabase_realtime'
       AND schemaname = 'public'
       AND tablename = 'trainings'
  ) THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.trainings;
  END IF;
END
$$;

-- END: 006_loss_history.sql


-- ============================================================
-- BEGIN: 007_deletion_requested_at.sql
-- ============================================================
-- 007_deletion_requested_at.sql
-- Add users.deletion_requested_at so /me/delete (GDPR Art. 17) can
-- record a deletion request without log warnings. Actual deletion
-- remains manual per context/20-operations.md §7.
--
-- Applied to production via Supabase MCP apply_migration on 2026-04-24.

ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS deletion_requested_at TIMESTAMPTZ;

-- Partial index on non-null rows only: admins can list pending deletions
-- cheaply without scanning the whole users table.
CREATE INDEX IF NOT EXISTS idx_users_deletion_requested_at
    ON public.users(deletion_requested_at)
    WHERE deletion_requested_at IS NOT NULL;

COMMENT ON COLUMN public.users.deletion_requested_at IS
    'Set by /me/delete when a user requests account removal. NULL for all normal users. Admin processes deletion within 30 days per GDPR Art. 17.';

-- END: 007_deletion_requested_at.sql


-- ============================================================
-- BEGIN: 008_workflows.sql
-- ============================================================
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

-- END: 008_workflows.sql


-- ============================================================
-- BEGIN: 009_workflows_rls_writes.sql
-- ============================================================
-- 009: Tighten public.workflows write policies + close the orphan-
-- template hole created by ON DELETE SET NULL on classroom_id.
--
-- Audit findings:
--
-- 2.2 — 008_workflows.sql shipped FOR SELECT policies only. The
-- service-role key used by FastAPI bypasses RLS, but defence-in-depth
-- says explicit deny-by-default for anon. WITH CHECK on INSERT/UPDATE
-- + a USING gate on DELETE makes the contract explicit.
--
-- 2.3 — workflows.classroom_id is "ON DELETE SET NULL" on classrooms,
-- so a teacher deleting a classroom turns every classroom template
-- into an orphan with is_template=TRUE and classroom_id=NULL — these
-- rows then leak past the "Classroom members read templates" SELECT
-- policy (which filters on classroom_id, not is_template alone). Add
-- a CHECK that rejects the orphan state outright.

-- ---------------------------------------------------------------------------
-- 1. Owner-only WITH CHECK / USING for INSERT, UPDATE, DELETE
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS "Owner inserts own workflows" ON public.workflows;
CREATE POLICY "Owner inserts own workflows"
  ON public.workflows FOR INSERT
  WITH CHECK (owner_user_id = auth.uid());

DROP POLICY IF EXISTS "Owner updates own workflows" ON public.workflows;
CREATE POLICY "Owner updates own workflows"
  ON public.workflows FOR UPDATE
  USING (owner_user_id = auth.uid())
  WITH CHECK (owner_user_id = auth.uid());

DROP POLICY IF EXISTS "Owner deletes own workflows" ON public.workflows;
CREATE POLICY "Owner deletes own workflows"
  ON public.workflows FOR DELETE
  USING (owner_user_id = auth.uid());

-- ---------------------------------------------------------------------------
-- 2. CHECK constraint: templates must reference an existing classroom
-- ---------------------------------------------------------------------------
ALTER TABLE public.workflows
  DROP CONSTRAINT IF EXISTS chk_template_has_classroom;

-- Cleanup any orphan templates the v1 ship may already have in prod
-- before the constraint goes live. is_template=TRUE without a
-- classroom is unreachable through the SELECT policies anyway, so
-- demoting to is_template=FALSE just makes the existing state
-- consistent.
UPDATE public.workflows
   SET is_template = FALSE
 WHERE is_template = TRUE
   AND classroom_id IS NULL;

ALTER TABLE public.workflows
  ADD CONSTRAINT chk_template_has_classroom
  CHECK (NOT (is_template = TRUE AND classroom_id IS NULL));

-- END: 009_workflows_rls_writes.sql


-- ============================================================
-- BEGIN: 010_progress_terminal_guard.sql
-- ============================================================
-- 010: Reject worker progress writes once a training row is in a terminal state.
--
-- Problem: the previous version of update_training_progress (006) updated
-- WHERE id = p_training_id AND worker_token = p_token. The /trainings/cancel
-- route flips status to 'canceled' via a direct UPDATE that does NOT null
-- worker_token, so a still-running Modal worker (Modal cancel can fail
-- silently) could call this RPC after cancellation and overwrite
-- status='canceled' with status='succeeded' — silently undoing the user
-- cancel and leaving the credit consumed.
--
-- Fix: refuse to update rows that are already in any terminal state
-- (succeeded / failed / canceled). Worker writes after termination
-- raise P0001 just like a token mismatch — the worker logs the failed
-- write and continues to shutdown without harming the row.
--
-- A complementary fix lives in cloud_training_api/app/routes/training.py
-- which now also nulls worker_token at /cancel time as defense in depth.
-- This SQL guard is the authoritative protection because it cannot be
-- bypassed by a future caller that forgets to null the token.

CREATE OR REPLACE FUNCTION public.update_training_progress(
  p_training_id  INT,
  p_token        UUID,
  p_status       TEXT  DEFAULT NULL,
  p_current_step INT   DEFAULT NULL,
  p_total_steps  INT   DEFAULT NULL,
  p_current_loss REAL  DEFAULT NULL,
  p_error_message TEXT DEFAULT NULL
) RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_rows        INT;
  v_new_history JSONB;
  v_len         INT;
BEGIN
  IF p_status IS NOT NULL AND p_status NOT IN ('queued','running','succeeded','failed','canceled') THEN
    RAISE EXCEPTION 'Invalid status: %', p_status USING ERRCODE = '22023';
  END IF;

  -- Append a new point if we have a (step, loss) pair, then downsample if over cap.
  IF p_current_step IS NOT NULL AND p_current_loss IS NOT NULL THEN
    SELECT COALESCE(loss_history, '[]'::jsonb) || jsonb_build_array(
             jsonb_build_object(
               's', p_current_step,
               'l', p_current_loss,
               't', (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT
             )
           )
      INTO v_new_history
      FROM public.trainings
     WHERE id = p_training_id;

    v_len := jsonb_array_length(v_new_history);
    IF v_len > 300 THEN
      SELECT jsonb_agg(elem ORDER BY idx)
        INTO v_new_history
        FROM (
          SELECT 0 AS idx, v_new_history -> 0 AS elem
          UNION
          SELECT (1 + s * (v_len - 102.0) / 198.0)::INT,
                 v_new_history -> (1 + s * (v_len - 102.0) / 198.0)::INT
            FROM generate_series(0, 198) AS s
          UNION
          SELECT v_len - 100 + s,
                 v_new_history -> (v_len - 100 + s)
            FROM generate_series(0, 99) AS s
        ) sampled;
    END IF;
  END IF;

  -- The terminal-state guard lives in the WHERE clause so it's enforced
  -- atomically with the row lookup — no chance for a worker write to
  -- slip in between a SELECT and an UPDATE.
  UPDATE public.trainings
  SET
    status        = COALESCE(p_status,        status),
    current_step  = COALESCE(p_current_step,  current_step),
    total_steps   = COALESCE(p_total_steps,   total_steps),
    current_loss  = COALESCE(p_current_loss,  current_loss),
    error_message = COALESCE(p_error_message, error_message),
    loss_history  = COALESCE(v_new_history,   loss_history),
    last_progress_at = NOW(),
    terminated_at = CASE
      WHEN p_status IN ('succeeded','failed','canceled') THEN NOW()
      ELSE terminated_at
    END,
    worker_token  = CASE
      WHEN p_status IN ('succeeded','failed','canceled') THEN NULL
      ELSE worker_token
    END
  WHERE id = p_training_id
    AND worker_token = p_token
    AND status NOT IN ('succeeded','failed','canceled');

  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows = 0 THEN
    RAISE EXCEPTION 'Invalid worker token, training not found, or training already terminal'
      USING ERRCODE = 'P0001';
  END IF;
END;
$$;

REVOKE ALL ON FUNCTION public.update_training_progress(INT, UUID, TEXT, INT, INT, REAL, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.update_training_progress(INT, UUID, TEXT, INT, INT, REAL, TEXT) TO anon, authenticated;

-- END: 010_progress_terminal_guard.sql


-- ============================================================
-- BEGIN: 011_workgroups.sql
-- ============================================================
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

-- END: 011_workgroups.sql


-- ============================================================
-- BEGIN: 012_dataset_sweep.sql
-- ============================================================
-- 012: Mark datasets registered by the periodic Railway sweep, distinct
-- from datasets registered live by the React app right after upload.
--
-- Problem: when a student uploads a dataset to HF Hub, the React app
-- POSTs /datasets to register it so group siblings can see it. If the
-- WSL distro has no internet at exactly that moment (or the Cloud API
-- is briefly down), the upload succeeds on HF but registration never
-- runs and siblings can never see the dataset.
--
-- Fix lives in cloud_training_api/app/services/dataset_sweep.py: a
-- background task that lists HF datasets for known authors every
-- 10 min and inserts any missing rows. This column lets us tell at a
-- glance which rows came in via that safety net (helpful for debugging
-- "why didn't my dataset show up live?" complaints).
--
-- The column is informational only — the sweep does NOT skip rows
-- whose discovered_via_sweep is FALSE on subsequent ticks; it just
-- inserts ones that are missing.
--
-- Forward-only:

ALTER TABLE public.datasets
  ADD COLUMN IF NOT EXISTS discovered_via_sweep BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN public.datasets.discovered_via_sweep IS
  'TRUE when this row was inserted by the Railway-side dataset sweep '
  'service (services/dataset_sweep.py) rather than by the live React '
  'POST /datasets call after a successful HF upload. Informational only.';

-- END: 012_dataset_sweep.sql


-- ============================================================
-- BEGIN: 013_revoke_anon_from_security_definer.sql
-- ============================================================
-- 013: Revoke anon + authenticated EXECUTE on service-role-only RPCs.
--
-- Problem: Supabase ships pg_default_acl rules that auto-grant EXECUTE
-- to anon, authenticated, AND service_role on every new function in the
-- public schema. The migration scripts that defined these RPCs say
-- `REVOKE ALL ON FUNCTION ... FROM PUBLIC` followed by `GRANT EXECUTE
-- TO service_role` — but PUBLIC is the inheritable role; explicit
-- grants to anon/authenticated installed by the default ACL survive
-- the REVOKE FROM PUBLIC. The result: anyone with the React-bundled
-- anon key can call:
--
--   - start_training_safe(p_user_id, ...)            -- queue trainings on any user
--   - adjust_student_credits(p_teacher_id, ...)      -- grant/take credits
--   - adjust_workgroup_credits(p_teacher_id, ...)    -- same for groups
--   - get_remaining_credits(p_user_id)               -- read any user's quota
--   - get_teacher_credit_summary(p_teacher_id)       -- read any teacher's pool
--
-- update_training_progress is intentionally callable by anon (the
-- Modal worker uses the anon key + a per-row worker_token), so this
-- migration leaves it alone.
--
-- Idempotent: REVOKE on a role that has no privilege is a no-op.

BEGIN;

REVOKE EXECUTE ON FUNCTION public.start_training_safe(UUID, TEXT, TEXT, TEXT, JSONB, INT, UUID)
  FROM anon, authenticated;

REVOKE EXECUTE ON FUNCTION public.adjust_student_credits(UUID, UUID, INTEGER)
  FROM anon, authenticated;

REVOKE EXECUTE ON FUNCTION public.adjust_workgroup_credits(UUID, UUID, INTEGER)
  FROM anon, authenticated;

-- get_remaining_credits had no REVOKE/GRANT block at all in 011 (DROP +
-- CREATE OR REPLACE without the privilege block left it with the default
-- ACL: =X/postgres meaning PUBLIC has EXECUTE). REVOKE FROM PUBLIC + GRANT
-- to service_role explicitly. anon/authenticated inherit from PUBLIC, so
-- this also fixes them.
REVOKE EXECUTE ON FUNCTION public.get_remaining_credits(UUID) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.get_remaining_credits(UUID) FROM anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_remaining_credits(UUID) TO service_role;

REVOKE EXECUTE ON FUNCTION public.get_teacher_credit_summary(UUID)
  FROM anon, authenticated;

COMMIT;

-- END: 013_revoke_anon_from_security_definer.sql


-- ============================================================
-- BEGIN: 015_workflow_versions.sql
-- ============================================================
-- 015_workflow_versions.sql
--
-- Roboter Studio Phase-2: server-side version history for workflows.
-- Every PATCH /workflows/{id} that changes blockly_json snapshots the
-- prior payload here so a student (or teacher) can roll back. Capped
-- at 20 versions per workflow via a trigger that prunes the oldest
-- after each insert.
--
-- Numbered 015 (skipping 013/014) because 013_revoke_anon_from_security_definer.sql
-- already exists in this directory; the prior 013_workflow_versions.sql filename
-- was a deployment hazard (alphabetic collision). Audit §C/§F renamed.

BEGIN;

CREATE TABLE IF NOT EXISTS public.workflow_versions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow_id UUID NOT NULL REFERENCES public.workflows(id) ON DELETE CASCADE,
  blockly_json JSONB NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  -- The user who saved this version (NULL when the snapshot was made
  -- by a service-role admin tool or by the BEFORE-UPDATE trigger
  -- which has no session context).
  saved_by UUID REFERENCES public.users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_workflow_versions_workflow
  ON public.workflow_versions(workflow_id, created_at DESC);

-- Cap each workflow's history at 20 versions. We take a per-workflow
-- advisory lock so two concurrent UPDATEs don't both see 20, both
-- insert, both keep 20 and leave 21-22 rows transiently. The lock is
-- xact-scoped → released at commit; deadlock-free because we hold
-- only one advisory key at a time.
CREATE OR REPLACE FUNCTION public.prune_workflow_versions()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext(NEW.workflow_id::text));
  DELETE FROM public.workflow_versions
  WHERE workflow_id = NEW.workflow_id
    AND id NOT IN (
      SELECT id FROM public.workflow_versions
      WHERE workflow_id = NEW.workflow_id
      ORDER BY created_at DESC
      LIMIT 20
    );
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_workflow_versions_prune ON public.workflow_versions;
CREATE TRIGGER trg_workflow_versions_prune
  AFTER INSERT ON public.workflow_versions
  FOR EACH ROW
  EXECUTE FUNCTION public.prune_workflow_versions();

-- Auto-snapshot on every UPDATE that changes blockly_json. Avoids a
-- second round-trip from the FastAPI side and keeps the version log
-- atomic with the parent row's mutation. Owned by postgres so the
-- INSERT inside the function bypasses RLS regardless of the calling
-- role (audit §F9).
--
-- ``saved_by`` is populated from the ``app.user_id`` Postgres GUC
-- when the cloud API sets it before the UPDATE
-- (``SET LOCAL app.user_id = '<uuid>'``). When the GUC is unset the
-- column stays NULL — appropriate for service-role admin tools or
-- internal migrations where there's no human author. Audit round-3
-- §13 / §I.
CREATE OR REPLACE FUNCTION public.snapshot_workflow_version()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_saved_by UUID;
  v_setting TEXT;
BEGIN
  IF NEW.blockly_json IS DISTINCT FROM OLD.blockly_json THEN
    BEGIN
      v_setting := current_setting('app.user_id', true);
      IF v_setting IS NOT NULL AND v_setting <> '' THEN
        v_saved_by := v_setting::UUID;
      END IF;
    EXCEPTION
      WHEN OTHERS THEN
        v_saved_by := NULL;
    END;
    INSERT INTO public.workflow_versions (workflow_id, blockly_json, note, saved_by)
    VALUES (OLD.id, OLD.blockly_json, '', v_saved_by);
  END IF;
  RETURN NEW;
END;
$$;

-- Ensure both SECURITY DEFINER functions run with bypassing-RLS
-- privilege. Without this, a migration applied by a non-superuser
-- would leave the trigger unable to INSERT because the table has no
-- INSERT policy (Audit §F3, §F9). Audit round-3 §AC/§AD — try
-- ``postgres`` first (self-hosted), fall through to ``supabase_admin``
-- (managed Supabase), and RAISE NOTICE only if both fail so the
-- operator sees the issue at apply time instead of at first edit.
DO $$
DECLARE
  v_done BOOLEAN := FALSE;
  v_role TEXT;
BEGIN
  FOREACH v_role IN ARRAY ARRAY['postgres', 'supabase_admin', 'supabase_storage_admin'] LOOP
    BEGIN
      EXECUTE format('ALTER FUNCTION public.snapshot_workflow_version() OWNER TO %I', v_role);
      EXECUTE format('ALTER FUNCTION public.prune_workflow_versions() OWNER TO %I', v_role);
      v_done := TRUE;
      EXIT;
    EXCEPTION
      WHEN OTHERS THEN
        CONTINUE;
    END;
  END LOOP;
  IF NOT v_done THEN
    RAISE NOTICE 'Could not transfer ownership of workflow_versions SECURITY DEFINER '
      'functions to a privileged role. The trigger may fail under RLS — '
      'see the audit notes in 015_workflow_versions.sql.';
  END IF;
END $$;

-- Belt-and-braces INSERT policy. Even when the ownership ALTER above
-- succeeds, an explicit INSERT WITH CHECK keeps the workflow UPDATE
-- path resilient to a future RLS tightening. Service-role still
-- bypasses RLS; this policy gates the SECURITY DEFINER trigger only.
DROP POLICY IF EXISTS "Trigger inserts workflow versions" ON public.workflow_versions;
CREATE POLICY "Trigger inserts workflow versions"
  ON public.workflow_versions
  FOR INSERT
  WITH CHECK (true);

DROP TRIGGER IF EXISTS trg_workflows_snapshot ON public.workflows;
CREATE TRIGGER trg_workflows_snapshot
  BEFORE UPDATE ON public.workflows
  FOR EACH ROW
  EXECUTE FUNCTION public.snapshot_workflow_version();

-- RLS policies — owner can read versions for their own workflows;
-- admins can read everything. INSERTs come from the SECURITY DEFINER
-- trigger only; service-role writes (cloud_training_api) bypass RLS.
ALTER TABLE public.workflow_versions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Owner reads own workflow versions" ON public.workflow_versions;
CREATE POLICY "Owner reads own workflow versions"
  ON public.workflow_versions
  FOR SELECT
  USING (
    EXISTS (
      SELECT 1 FROM public.workflows w
      WHERE w.id = workflow_versions.workflow_id
        AND w.owner_user_id = auth.uid()
    )
  );

DROP POLICY IF EXISTS "Admin reads all workflow versions" ON public.workflow_versions;
CREATE POLICY "Admin reads all workflow versions"
  ON public.workflow_versions
  FOR SELECT
  USING (
    EXISTS (
      SELECT 1 FROM public.users
      WHERE id = auth.uid() AND role = 'admin'
    )
  );

-- Realtime publication so the React Verlauf dropdown auto-refreshes
-- when a teammate (e.g. workgroup peer with a shared workflow) saves.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_publication_tables
    WHERE pubname = 'supabase_realtime'
      AND schemaname = 'public'
      AND tablename = 'workflow_versions'
  ) THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.workflow_versions;
  END IF;
END $$;

COMMIT;

-- END: 015_workflow_versions.sql


-- ============================================================
-- BEGIN: 016_tutorial_progress.sql
-- ============================================================
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

-- END: 016_tutorial_progress.sql


-- ============================================================
-- BEGIN: 017_vision_quota.sql
-- ============================================================
-- 017_vision_quota.sql
--
-- Roboter Studio Phase-3: per-user cloud-vision quota.
-- The cloud_training_api `/vision/detect` endpoint forwards calls to
-- the Modal OWLv2 app. Each call costs ~$0.0001 in T4 compute but a
-- runaway workflow loop could rack up real money; this migration
-- adds two columns on ``users`` so the endpoint can short-circuit
-- after N successful calls per term.
--
-- ``vision_quota_per_term`` NULL means "unbounded"; the endpoint
-- treats NULL as a no-op so adopters with no policy preference are
-- gated only by the in-process rate limiter (5/60s/user).
-- Audit §D1 / §J7 — the endpoint reads these columns but they
-- didn't exist; the swallow-exception clause silently bypassed the
-- quota. Adding the columns wires the check up.

BEGIN;

ALTER TABLE public.users
  ADD COLUMN IF NOT EXISTS vision_quota_per_term INTEGER,
  ADD COLUMN IF NOT EXISTS vision_used_per_term INTEGER NOT NULL DEFAULT 0;

-- Floor the used counter so a future bug-introduced decrement RPC can't
-- write negative values. Audit round-3 §AQ.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.check_constraints
    WHERE constraint_schema = 'public'
      AND constraint_name = 'users_vision_used_per_term_nonneg'
  ) THEN
    ALTER TABLE public.users
      ADD CONSTRAINT users_vision_used_per_term_nonneg
      CHECK (vision_used_per_term >= 0);
  END IF;
END $$;

COMMENT ON COLUMN public.users.vision_quota_per_term IS
  'Maximum cloud-vision detect calls per term. NULL = unbounded.';
COMMENT ON COLUMN public.users.vision_used_per_term IS
  'Counter incremented by /vision/detect on every successful call.';

-- Atomic consume: returns (allowed, remaining). The UPDATE only
-- fires when there's room left so two concurrent calls can't both
-- pass the check and both increment (audit §D2). NULL quota means
-- unbounded → always allowed.
CREATE OR REPLACE FUNCTION public.consume_vision_quota(p_user_id UUID)
RETURNS TABLE(allowed BOOLEAN, remaining INTEGER)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_quota INTEGER;
  v_new_used INTEGER;
BEGIN
  SELECT vision_quota_per_term INTO v_quota
  FROM public.users
  WHERE id = p_user_id;
  IF NOT FOUND THEN
    RETURN QUERY SELECT FALSE, 0;
    RETURN;
  END IF;
  IF v_quota IS NULL THEN
    -- Audit F48: skip the UPDATE for NULL-quota users so the counter
    -- doesn't grow unbounded. If an admin later flips them to a
    -- bounded quota, the student would otherwise be INSTANTLY
    -- locked out (used > new_quota from N years of telemetry). The
    -- "still count usage for telemetry" intent was a footgun.
    RETURN QUERY SELECT TRUE, NULL::INTEGER;
    RETURN;
  END IF;
  UPDATE public.users
  SET vision_used_per_term = vision_used_per_term + 1
  WHERE id = p_user_id
    AND vision_used_per_term < v_quota
  RETURNING vision_used_per_term INTO v_new_used;
  IF v_new_used IS NULL THEN
    RETURN QUERY SELECT FALSE, 0;
  ELSE
    RETURN QUERY SELECT TRUE, GREATEST(v_quota - v_new_used, 0);
  END IF;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.consume_vision_quota(UUID) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.consume_vision_quota(UUID) FROM anon;
REVOKE EXECUTE ON FUNCTION public.consume_vision_quota(UUID) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.consume_vision_quota(UUID) TO service_role;

-- Atomic refund: decrement vision_used_per_term by 1, never below 0.
-- Called by the cloud API when Modal returns a transient error
-- (502/504/timeout) so a flaky cold start doesn't burn the student's
-- term budget. Returns the new used count for telemetry.
-- Audit round-3 §A — refund-on-failure pattern.
CREATE OR REPLACE FUNCTION public.refund_vision_quota(p_user_id UUID)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_new_used INTEGER;
BEGIN
  UPDATE public.users
  SET vision_used_per_term = GREATEST(vision_used_per_term - 1, 0)
  WHERE id = p_user_id
  RETURNING vision_used_per_term INTO v_new_used;
  RETURN COALESCE(v_new_used, 0);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.refund_vision_quota(UUID) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.refund_vision_quota(UUID) FROM anon;
REVOKE EXECUTE ON FUNCTION public.refund_vision_quota(UUID) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.refund_vision_quota(UUID) TO service_role;

-- Convenience RPC that resets every student's used counter at term
-- start. Run from the admin dashboard / cron. Service-role only.
CREATE OR REPLACE FUNCTION public.reset_vision_quota_used()
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  affected INTEGER;
BEGIN
  UPDATE public.users
  SET vision_used_per_term = 0
  WHERE vision_used_per_term > 0;
  GET DIAGNOSTICS affected = ROW_COUNT;
  RETURN affected;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.reset_vision_quota_used() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.reset_vision_quota_used() FROM anon;
REVOKE EXECUTE ON FUNCTION public.reset_vision_quota_used() FROM authenticated;
GRANT EXECUTE ON FUNCTION public.reset_vision_quota_used() TO service_role;

COMMIT;

-- END: 017_vision_quota.sql


-- ============================================================
-- BEGIN: 018_workflow_versions_author_and_group_rls.sql
-- ============================================================
-- 018_workflow_versions_author_and_group_rls.sql
--
-- Two related fixes on the Roboter Studio version-history feature
-- (Phase-2, migration 015):
--
-- 1. Audit A1 — `workflow_versions.saved_by` was permanently NULL in
--    production because the BEFORE-UPDATE trigger reads the Postgres
--    GUC `app.user_id`, but the cloud API uses supabase-py over REST,
--    which is stateless — every call hits a different PostgREST
--    connection, so `SET LOCAL` from the caller can't reach the
--    trigger. We add a SECURITY DEFINER RPC
--    `update_workflow_blockly` that wraps `set_config('app.user_id',
--    ...)` and the UPDATE in a single transaction, so the trigger
--    sees the GUC and writes the right `saved_by`. The route layer
--    calls this RPC instead of `.table('workflows').update(...)`.
--
-- 2. Audit N1 / D3 — group-shared workflow versions were invisible to
--    sibling group members. RLS on `workflow_versions` only allowed
--    the parent-workflow owner + admins to read. The parent table's
--    own policy already lets siblings read group-shared workflows
--    (via `Group members read group workflows`); mirror that for
--    `workflow_versions` so a peer can also see the version history
--    of a group-shared template they're collaborating on.
--
-- Idempotent — re-runnable.

BEGIN;

-- ---------------------------------------------------------------------------
-- A1: update_workflow_blockly RPC — atomic SET LOCAL + UPDATE so the
-- BEFORE-UPDATE trigger's `current_setting('app.user_id', true)` read
-- resolves to the calling user's UUID.
--
-- Returns the updated row so the caller doesn't need a second SELECT.
-- Service-role-only execution; ownership / template-classroom checks
-- live in the Python route (consistent with the rest of the cloud API,
-- which treats RLS as defense-in-depth and authorises in Python).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.update_workflow_blockly(
    p_workflow_id UUID,
    p_user_id UUID,
    p_blockly_json JSONB,
    p_name TEXT DEFAULT NULL,
    p_description TEXT DEFAULT NULL
)
RETURNS public.workflows
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_row public.workflows;
BEGIN
    -- Transaction-local GUC: the BEFORE-UPDATE trigger reads this and
    -- writes it to workflow_versions.saved_by. The `true` 3rd arg
    -- scopes the setting to the current transaction so it can't leak
    -- to another call on the same pooled connection.
    PERFORM set_config('app.user_id', p_user_id::TEXT, true);

    UPDATE public.workflows
       SET blockly_json = p_blockly_json,
           name         = COALESCE(NULLIF(p_name, ''), name),
           description  = COALESCE(p_description, description)
     WHERE id = p_workflow_id
    RETURNING * INTO v_row;

    IF v_row.id IS NULL THEN
        RAISE EXCEPTION 'Workflow nicht gefunden' USING ERRCODE = 'P0002';
    END IF;

    RETURN v_row;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.update_workflow_blockly(UUID, UUID, JSONB, TEXT, TEXT) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.update_workflow_blockly(UUID, UUID, JSONB, TEXT, TEXT) FROM anon;
REVOKE EXECUTE ON FUNCTION public.update_workflow_blockly(UUID, UUID, JSONB, TEXT, TEXT) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.update_workflow_blockly(UUID, UUID, JSONB, TEXT, TEXT) TO service_role;


-- ---------------------------------------------------------------------------
-- A1 companion: restore_workflow_version RPC — same SET LOCAL trick
-- for the restore path. Without this, restoring an old version through
-- the trigger snapshots the current payload with saved_by=NULL, losing
-- the author info on the "before restore" snapshot too.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.restore_workflow_version(
    p_workflow_id UUID,
    p_version_id UUID,
    p_user_id UUID
)
RETURNS public.workflows
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_row     public.workflows;
    v_payload JSONB;
BEGIN
    SELECT blockly_json INTO v_payload
      FROM public.workflow_versions
     WHERE id = p_version_id AND workflow_id = p_workflow_id;

    IF v_payload IS NULL THEN
        RAISE EXCEPTION 'Workflow-Version nicht gefunden' USING ERRCODE = 'P0002';
    END IF;

    PERFORM set_config('app.user_id', p_user_id::TEXT, true);

    UPDATE public.workflows
       SET blockly_json = v_payload
     WHERE id = p_workflow_id
    RETURNING * INTO v_row;

    IF v_row.id IS NULL THEN
        RAISE EXCEPTION 'Workflow nicht gefunden' USING ERRCODE = 'P0002';
    END IF;

    RETURN v_row;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.restore_workflow_version(UUID, UUID, UUID) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.restore_workflow_version(UUID, UUID, UUID) FROM anon;
REVOKE EXECUTE ON FUNCTION public.restore_workflow_version(UUID, UUID, UUID) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.restore_workflow_version(UUID, UUID, UUID) TO service_role;


-- ---------------------------------------------------------------------------
-- N1: extend workflow_versions read RLS to group-shared workflows so
-- siblings have the same history visibility they have for the parent.
-- The existing "Owner reads own workflow versions" policy keys on
-- workflows.owner_user_id; the new policy keys on workflows.workgroup_id
-- via the workgroup_memberships audit table, mirroring the policy on
-- public.workflows from migration 011.
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS "Group members read group workflow versions" ON public.workflow_versions;
CREATE POLICY "Group members read group workflow versions"
    ON public.workflow_versions
    FOR SELECT
    USING (
        EXISTS (
            SELECT 1
              FROM public.workflows w
              JOIN public.workgroup_memberships m
                ON m.workgroup_id = w.workgroup_id
             WHERE w.id = workflow_versions.workflow_id
               AND w.workgroup_id IS NOT NULL
               AND m.user_id = auth.uid()
        )
    );

COMMIT;

-- END: 018_workflow_versions_author_and_group_rls.sql


-- ============================================================
-- BEGIN: 019_classroom_jetsons.sql
-- ============================================================
-- 019_classroom_jetsons.sql
--
-- Classroom Jetson Orin Nano support: one paired Jetson per classroom,
-- first-come explicit-disconnect lock for the inference tab, full
-- wipe-on-release lifecycle. Scope is intentionally narrow: ONLY the
-- Inference tab in EduBotics connects to the Jetson; Roboter Studio
-- (Workshop), Calibration, and Recording all stay on the student PC.
--
-- Two heartbeat layers:
--   1. Agent → Cloud API every 10s  → ``last_seen_at`` (liveness).
--   2. Student React → Cloud API every 30s → ``current_owner_heartbeat_at``
--      (lock-hold). After 5 minutes of silence the sweeper in
--      cloud_training_api/app/services/jetson_sweep.py auto-releases
--      the lock so a crashed browser doesn't permanently block the
--      classroom Jetson.
--
-- Two new Postgres error codes (registered in CLAUDE.md §7.5):
--   P0030 → 409 Jetson belegt        (claim race / second-claimer)
--   P0031 → 410 Lock verloren        (heartbeat from non-owner)
--
-- Service-role bypasses all RLS — Python ``_assert_*_owned`` helpers
-- in routes/jetson.py are the actual authorisation layer (consistent
-- with the rest of the cloud API, which treats RLS as defense-in-depth).
--
-- Idempotent — re-runnable.

BEGIN;

-- ---------------------------------------------------------------------------
-- Schema
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.jetsons (
  id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  classroom_id                UUID REFERENCES public.classrooms(id) ON DELETE SET NULL,
  agent_token                 UUID NOT NULL DEFAULT gen_random_uuid(),
  pairing_code                TEXT UNIQUE,
  pairing_code_expires_at     TIMESTAMPTZ,
  mdns_name                   TEXT,
  lan_ip                      TEXT,
  agent_version               TEXT,
  last_seen_at                TIMESTAMPTZ,
  current_owner_user_id       UUID REFERENCES public.users(id) ON DELETE SET NULL,
  current_owner_heartbeat_at  TIMESTAMPTZ,
  claimed_at                  TIMESTAMPTZ,
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_jetsons_classroom_id
  ON public.jetsons (classroom_id);

CREATE INDEX IF NOT EXISTS idx_jetsons_current_owner
  ON public.jetsons (current_owner_user_id)
  WHERE current_owner_user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_jetsons_pairing_code
  ON public.jetsons (pairing_code)
  WHERE pairing_code IS NOT NULL;

-- Sweeper needs to find expired locks fast. Partial index trims out the
-- common case (no current owner).
CREATE INDEX IF NOT EXISTS idx_jetsons_owner_heartbeat
  ON public.jetsons (current_owner_heartbeat_at)
  WHERE current_owner_user_id IS NOT NULL;

COMMENT ON TABLE public.jetsons IS
  'Classroom-shared Jetson Orin Nano inference targets. One row per physical Jetson; classroom_id binding is set at teacher pairing time.';
COMMENT ON COLUMN public.jetsons.agent_token IS
  'Long-lived per-Jetson token (UUID). Sent in every agent-heartbeat. Cleared and rotated if the device is reset via /jetson/register.';
COMMENT ON COLUMN public.jetsons.pairing_code IS
  '6-digit code shown on first boot. Teacher enters this in admin UI to bind device to a classroom. Cleared on successful pair.';
COMMENT ON COLUMN public.jetsons.mdns_name IS
  'edubotics-jetson-<short-id>.local — generated at pair time, used by React app as DNS fallback to the lan_ip.';
COMMENT ON COLUMN public.jetsons.lan_ip IS
  'Last LAN IP reported by agent heartbeat. Primary endpoint for React WS connections.';
COMMENT ON COLUMN public.jetsons.last_seen_at IS
  'Agent heartbeat (every 10s). Used by admin UI to flag offline Jetsons.';
COMMENT ON COLUMN public.jetsons.current_owner_user_id IS
  'Student currently holding the Jetson. NULL = available. Cleared by /jetson/{id}/release or by the 5-min sweeper.';
COMMENT ON COLUMN public.jetsons.current_owner_heartbeat_at IS
  'Last student React heartbeat (every 30s). 5-min silence triggers the sweeper to auto-release.';

-- ---------------------------------------------------------------------------
-- claim_jetson: atomic compare-and-swap, raises P0030 if already held.
-- The SELECT ... FOR UPDATE pessimistic-locks the row for the duration
-- of the transaction so two concurrent claims serialise via Postgres
-- row-level locking (no application-side mutex needed).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.claim_jetson(
    p_jetson_id UUID,
    p_user_id UUID
)
RETURNS public.jetsons
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_row public.jetsons;
BEGIN
    SELECT * INTO v_row
      FROM public.jetsons
     WHERE id = p_jetson_id
       FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Jetson nicht gefunden' USING ERRCODE = 'P0002';
    END IF;

    IF v_row.classroom_id IS NULL THEN
        RAISE EXCEPTION 'Jetson ist nicht gepaart' USING ERRCODE = 'P0002';
    END IF;

    IF v_row.current_owner_user_id IS NOT NULL THEN
        RAISE EXCEPTION 'Jetson ist bereits belegt' USING ERRCODE = 'P0030';
    END IF;

    UPDATE public.jetsons
       SET current_owner_user_id      = p_user_id,
           current_owner_heartbeat_at = NOW(),
           claimed_at                 = NOW(),
           updated_at                 = NOW()
     WHERE id = p_jetson_id
    RETURNING * INTO v_row;

    RETURN v_row;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.claim_jetson(UUID, UUID) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.claim_jetson(UUID, UUID) FROM anon;
REVOKE EXECUTE ON FUNCTION public.claim_jetson(UUID, UUID) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.claim_jetson(UUID, UUID) TO service_role;


-- ---------------------------------------------------------------------------
-- release_jetson: idempotent. Only clears the lock if the caller IS the
-- current owner (prevents one student stomping on another's session).
-- Returns silently in either case so callers don't have to special-case
-- "already released" (e.g. /jetson/{id}/release called via sendBeacon
-- on tab unload races with the 5-min sweeper).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.release_jetson(
    p_jetson_id UUID,
    p_user_id UUID
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    UPDATE public.jetsons
       SET current_owner_user_id      = NULL,
           current_owner_heartbeat_at = NULL,
           claimed_at                 = NULL,
           updated_at                 = NOW()
     WHERE id = p_jetson_id
       AND current_owner_user_id = p_user_id;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.release_jetson(UUID, UUID) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.release_jetson(UUID, UUID) FROM anon;
REVOKE EXECUTE ON FUNCTION public.release_jetson(UUID, UUID) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.release_jetson(UUID, UUID) TO service_role;


-- ---------------------------------------------------------------------------
-- heartbeat_jetson: student-side 30s heartbeat. Raises P0031 if the
-- caller is not (or no longer) the current owner — React handles that
-- by auto-disconnecting and surfacing a German toast.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.heartbeat_jetson(
    p_jetson_id UUID,
    p_user_id UUID
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_rows INTEGER;
BEGIN
    UPDATE public.jetsons
       SET current_owner_heartbeat_at = NOW(),
           updated_at                 = NOW()
     WHERE id = p_jetson_id
       AND current_owner_user_id = p_user_id;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    IF v_rows = 0 THEN
        RAISE EXCEPTION 'Lock verloren — bitte erneut verbinden' USING ERRCODE = 'P0031';
    END IF;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.heartbeat_jetson(UUID, UUID) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.heartbeat_jetson(UUID, UUID) FROM anon;
REVOKE EXECUTE ON FUNCTION public.heartbeat_jetson(UUID, UUID) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.heartbeat_jetson(UUID, UUID) TO service_role;


-- ---------------------------------------------------------------------------
-- agent_heartbeat_jetson: agent-side 10s heartbeat. Verifies agent_token
-- so a rogue device on the classroom LAN can't impersonate a paired
-- Jetson (the token is provisioned at /jetson/register and lives in
-- /etc/edubotics/jetson.env mode 600). Returns current_owner_user_id
-- so the agent's rosbridge proxy knows which JWT sub to allow.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.agent_heartbeat_jetson(
    p_jetson_id UUID,
    p_agent_token UUID,
    p_lan_ip TEXT,
    p_agent_version TEXT
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_owner UUID;
    v_rows INTEGER;
BEGIN
    UPDATE public.jetsons
       SET last_seen_at  = NOW(),
           lan_ip        = COALESCE(NULLIF(p_lan_ip, ''), lan_ip),
           agent_version = COALESCE(NULLIF(p_agent_version, ''), agent_version),
           updated_at    = NOW()
     WHERE id = p_jetson_id
       AND agent_token = p_agent_token
    RETURNING current_owner_user_id INTO v_owner;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    IF v_rows = 0 THEN
        RAISE EXCEPTION 'Agent-Token ungültig oder Jetson nicht gefunden' USING ERRCODE = 'P0001';
    END IF;

    RETURN v_owner;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.agent_heartbeat_jetson(UUID, UUID, TEXT, TEXT) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.agent_heartbeat_jetson(UUID, UUID, TEXT, TEXT) FROM anon;
REVOKE EXECUTE ON FUNCTION public.agent_heartbeat_jetson(UUID, UUID, TEXT, TEXT) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.agent_heartbeat_jetson(UUID, UUID, TEXT, TEXT) TO service_role;


-- ---------------------------------------------------------------------------
-- pair_jetson: teacher binds a registered Jetson to one of their own
-- classrooms via the 6-digit pairing code. Asserts the caller owns the
-- target classroom (P0011 conventions from migration 002). The pairing
-- code is one-time-use: cleared on success so a second teacher can't
-- replay it.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.pair_jetson(
    p_classroom_id UUID,
    p_pairing_code TEXT,
    p_teacher_id UUID,
    p_mdns_name TEXT
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_jetson_id UUID;
    v_classroom_teacher UUID;
BEGIN
    -- Confirm caller owns the classroom.
    SELECT teacher_id INTO v_classroom_teacher
      FROM public.classrooms
     WHERE id = p_classroom_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Klassenzimmer nicht gefunden' USING ERRCODE = 'P0002';
    END IF;

    IF v_classroom_teacher <> p_teacher_id THEN
        RAISE EXCEPTION 'Klassenzimmer gehört nicht zu diesem Lehrer' USING ERRCODE = 'P0011';
    END IF;

    UPDATE public.jetsons
       SET classroom_id            = p_classroom_id,
           mdns_name               = p_mdns_name,
           pairing_code            = NULL,
           pairing_code_expires_at = NULL,
           updated_at              = NOW()
     WHERE pairing_code = p_pairing_code
       AND (pairing_code_expires_at IS NULL OR pairing_code_expires_at > NOW())
    RETURNING id INTO v_jetson_id;

    IF v_jetson_id IS NULL THEN
        RAISE EXCEPTION 'Pairing-Code ungültig oder abgelaufen' USING ERRCODE = 'P0002';
    END IF;

    RETURN v_jetson_id;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.pair_jetson(UUID, TEXT, UUID, TEXT) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.pair_jetson(UUID, TEXT, UUID, TEXT) FROM anon;
REVOKE EXECUTE ON FUNCTION public.pair_jetson(UUID, TEXT, UUID, TEXT) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.pair_jetson(UUID, TEXT, UUID, TEXT) TO service_role;


-- ---------------------------------------------------------------------------
-- force_release_jetson: teacher emergency unlock. Used from the admin
-- dashboard when a student walked out without clicking Trennen and
-- 5 minutes is too long to wait (e.g. between two consecutive class
-- periods).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.force_release_jetson(
    p_jetson_id UUID,
    p_teacher_id UUID
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_classroom_id UUID;
    v_classroom_teacher UUID;
BEGIN
    SELECT classroom_id INTO v_classroom_id
      FROM public.jetsons
     WHERE id = p_jetson_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Jetson nicht gefunden' USING ERRCODE = 'P0002';
    END IF;

    IF v_classroom_id IS NULL THEN
        RAISE EXCEPTION 'Jetson ist nicht gepaart' USING ERRCODE = 'P0002';
    END IF;

    SELECT teacher_id INTO v_classroom_teacher
      FROM public.classrooms
     WHERE id = v_classroom_id;

    IF v_classroom_teacher <> p_teacher_id THEN
        RAISE EXCEPTION 'Jetson gehört nicht zu diesem Lehrer' USING ERRCODE = 'P0011';
    END IF;

    UPDATE public.jetsons
       SET current_owner_user_id      = NULL,
           current_owner_heartbeat_at = NULL,
           claimed_at                 = NULL,
           updated_at                 = NOW()
     WHERE id = p_jetson_id;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.force_release_jetson(UUID, UUID) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.force_release_jetson(UUID, UUID) FROM anon;
REVOKE EXECUTE ON FUNCTION public.force_release_jetson(UUID, UUID) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.force_release_jetson(UUID, UUID) TO service_role;


-- ---------------------------------------------------------------------------
-- sweep_jetson_locks: server-side sweeper called from
-- cloud_training_api/app/services/jetson_sweep.py every 60s. Auto-
-- releases any lock whose heartbeat is older than 5 minutes. Single
-- statement, no row-by-row iteration — Postgres applies the predicate
-- once per Jetson row. Returns the count of released locks for
-- telemetry. Service-role only.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.sweep_jetson_locks()
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_affected INTEGER;
BEGIN
    UPDATE public.jetsons
       SET current_owner_user_id      = NULL,
           current_owner_heartbeat_at = NULL,
           claimed_at                 = NULL,
           updated_at                 = NOW()
     WHERE current_owner_user_id IS NOT NULL
       AND current_owner_heartbeat_at < NOW() - INTERVAL '5 minutes';
    GET DIAGNOSTICS v_affected = ROW_COUNT;
    RETURN v_affected;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.sweep_jetson_locks() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.sweep_jetson_locks() FROM anon;
REVOKE EXECUTE ON FUNCTION public.sweep_jetson_locks() FROM authenticated;
GRANT EXECUTE ON FUNCTION public.sweep_jetson_locks() TO service_role;


-- ---------------------------------------------------------------------------
-- RLS: defense-in-depth. Service-role bypasses (it's the cloud API's
-- only Supabase auth context) but anon-key reads from the React app
-- could one day flow through these policies, so they're set up now to
-- avoid a later silent IDOR if the auth model is ever swapped.
-- ---------------------------------------------------------------------------
ALTER TABLE public.jetsons ENABLE ROW LEVEL SECURITY;

-- Classroom members (students + the teacher) can SELECT their classroom's
-- Jetson. Used by React to populate the availability chip.
DROP POLICY IF EXISTS "Classroom members read classroom jetson" ON public.jetsons;
CREATE POLICY "Classroom members read classroom jetson"
    ON public.jetsons
    FOR SELECT
    USING (
        classroom_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM public.users u
             WHERE u.id = auth.uid()
               AND u.classroom_id = jetsons.classroom_id
        )
    );

-- Teachers can SELECT/UPDATE Jetsons in classrooms they own. Mostly a
-- placeholder for a future teacher-direct-write path; today every write
-- goes through service-role RPCs.
DROP POLICY IF EXISTS "Teachers manage own classroom jetson" ON public.jetsons;
CREATE POLICY "Teachers manage own classroom jetson"
    ON public.jetsons
    FOR ALL
    USING (
        classroom_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM public.classrooms c
             WHERE c.id = jetsons.classroom_id
               AND c.teacher_id = auth.uid()
        )
    )
    WITH CHECK (
        classroom_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM public.classrooms c
             WHERE c.id = jetsons.classroom_id
               AND c.teacher_id = auth.uid()
        )
    );

-- Admins: all.
DROP POLICY IF EXISTS "Admins manage jetsons" ON public.jetsons;
CREATE POLICY "Admins manage jetsons"
    ON public.jetsons
    FOR ALL
    USING (
        EXISTS (
            SELECT 1 FROM public.users u
             WHERE u.id = auth.uid()
               AND u.role = 'admin'
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM public.users u
             WHERE u.id = auth.uid()
               AND u.role = 'admin'
        )
    );

GRANT ALL ON public.jetsons TO service_role;


-- ---------------------------------------------------------------------------
-- Realtime publication so React's useSupabaseJetson hook gets push
-- updates on availability + owner changes (matches the workflows /
-- workgroups / datasets pattern from migrations 008 / 011).
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_publication_tables
     WHERE pubname = 'supabase_realtime'
       AND schemaname = 'public'
       AND tablename = 'jetsons'
  ) THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.jetsons;
  END IF;
END
$$;


-- ---------------------------------------------------------------------------
-- touch_updated_at trigger. The function is shared across migrations
-- and was hardened in migration 008 with ``SET search_path = public``
-- as defense-in-depth against search_path injection. We deliberately
-- do NOT re-define the function here — a `CREATE OR REPLACE FUNCTION`
-- would strip the 008 hardening when 019 is applied (silent security
-- regression on every existing trigger using it). 019 just installs
-- a NEW trigger that references the existing function.
--
-- Pre-condition: migration 004 (which originally introduced the
-- function) MUST have been applied. The schema fingerprint in
-- cloud_training_api/app/main.py validates this at Cloud API boot.
-- ---------------------------------------------------------------------------
DROP TRIGGER IF EXISTS trg_jetsons_touch ON public.jetsons;
CREATE TRIGGER trg_jetsons_touch
    BEFORE UPDATE ON public.jetsons
    FOR EACH ROW
    EXECUTE FUNCTION public.touch_updated_at();

COMMIT;

-- END: 019_classroom_jetsons.sql


-- ============================================================
-- BEGIN: 020_jetson_v2.sql
-- ============================================================
-- 020_jetson_v2.sql
--
-- Classroom Jetson Orin Nano v2.3.0 follow-up RPCs. Pairs with the
-- v2.3.0 push of the Cloud API + React teacher dashboard:
--   * agent_release_jetson  → agent-authenticated lock release for
--                             local claim-transition failures (the agent
--                             observes a docker-compose / healthcheck
--                             error mid-claim and proactively frees the
--                             server-side lock instead of waiting 5 min
--                             for the sweeper).
--   * regenerate_pairing_code → teacher generates a fresh 6-digit code
--                             without SSHing back to the Jetson to re-run
--                             setup.sh. Cryptographic randomness lives in
--                             the Cloud API (Python's `secrets`); the RPC
--                             receives the pre-generated code and writes
--                             it atomically with a refreshed 30-min
--                             expiry.
--   * unpair_jetson          → teacher unbinds the Jetson from the
--                             classroom (sets classroom_id=NULL),
--                             also force-releases any active owner so
--                             the device can be paired to another
--                             classroom or decommissioned cleanly.
--
-- No new error codes — re-uses migration 019's P0001 (token mismatch /
-- not found), P0002 (jetson/classroom not found), P0011 (classroom does
-- not belong to this teacher).
--
-- Service-role bypasses RLS — Python ``_assert_*_owned`` helpers in
-- routes/jetson.py remain the actual authorisation layer.
--
-- Idempotent — re-runnable.

BEGIN;

-- ---------------------------------------------------------------------------
-- agent_release_jetson: agent-authenticated lock release.
--
-- Used when the agent's claim transition fails locally (docker compose
-- up timeout, healthcheck flap, OOM on first model download) and the
-- agent decides to abandon the in-flight session. Without this RPC the
-- agent has to wait 5 minutes for the sweeper, during which the student
-- sees "Jetson belegt von <ihrer Name>" with no way to retry. After
-- this RPC the agent can release immediately and the student's next
-- claim succeeds in seconds.
--
-- Auth: agent_token verification (same shape as agent_heartbeat_jetson
-- from migration 019). A student JWT cannot call this — only the
-- physical Jetson holding the matching agent_token.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.agent_release_jetson(
    p_jetson_id UUID,
    p_agent_token UUID
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_rows INTEGER;
BEGIN
    UPDATE public.jetsons
       SET current_owner_user_id      = NULL,
           current_owner_heartbeat_at = NULL,
           claimed_at                 = NULL,
           updated_at                 = NOW()
     WHERE id = p_jetson_id
       AND agent_token = p_agent_token;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    IF v_rows = 0 THEN
        RAISE EXCEPTION 'Agent-Token ungültig oder Jetson nicht gefunden' USING ERRCODE = 'P0001';
    END IF;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.agent_release_jetson(UUID, UUID) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.agent_release_jetson(UUID, UUID) FROM anon;
REVOKE EXECUTE ON FUNCTION public.agent_release_jetson(UUID, UUID) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.agent_release_jetson(UUID, UUID) TO service_role;


-- ---------------------------------------------------------------------------
-- regenerate_pairing_code: teacher generates a fresh pairing code on a
-- Jetson already bound to their classroom (replaces a code that
-- expired, or one the teacher wants to rotate).
--
-- Cryptographic randomness lives in the Cloud API (Python's
-- `secrets.randbelow`), not in Postgres — `random()` is not
-- cryptographically secure and `gen_random_bytes()` is an extension
-- that isn't guaranteed to be loaded. The RPC receives the
-- pre-generated 6-digit code and refreshes the 30-min expiry.
--
-- Asserts the caller owns the target classroom (P0011). Returns the
-- jetson_id so the Cloud API can echo it back to the teacher's UI.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.regenerate_pairing_code(
    p_jetson_id UUID,
    p_teacher_id UUID,
    p_new_code TEXT,
    p_expires_at TIMESTAMPTZ
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_classroom_id UUID;
    v_classroom_teacher UUID;
BEGIN
    -- Lock the row to prevent two teachers (same classroom, e.g. an
    -- admin acting as a teacher) from racing on regenerate.
    SELECT classroom_id INTO v_classroom_id
      FROM public.jetsons
     WHERE id = p_jetson_id
       FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Jetson nicht gefunden' USING ERRCODE = 'P0002';
    END IF;

    IF v_classroom_id IS NULL THEN
        RAISE EXCEPTION 'Jetson ist nicht gepaart' USING ERRCODE = 'P0002';
    END IF;

    SELECT teacher_id INTO v_classroom_teacher
      FROM public.classrooms
     WHERE id = v_classroom_id;

    IF v_classroom_teacher <> p_teacher_id THEN
        RAISE EXCEPTION 'Jetson gehört nicht zu diesem Lehrer' USING ERRCODE = 'P0011';
    END IF;

    UPDATE public.jetsons
       SET pairing_code            = p_new_code,
           pairing_code_expires_at = p_expires_at,
           updated_at              = NOW()
     WHERE id = p_jetson_id;

    RETURN p_jetson_id;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.regenerate_pairing_code(UUID, UUID, TEXT, TIMESTAMPTZ) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.regenerate_pairing_code(UUID, UUID, TEXT, TIMESTAMPTZ) FROM anon;
REVOKE EXECUTE ON FUNCTION public.regenerate_pairing_code(UUID, UUID, TEXT, TIMESTAMPTZ) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.regenerate_pairing_code(UUID, UUID, TEXT, TIMESTAMPTZ) TO service_role;


-- ---------------------------------------------------------------------------
-- unpair_jetson: teacher unbinds the Jetson from the classroom. Used
-- when the teacher wants to:
--   * move the device to a different classroom mid-term,
--   * decommission an old Jetson,
--   * recover from accidentally pairing the wrong physical device.
--
-- Side effects in a single transaction:
--   * classroom_id              → NULL
--   * current_owner_user_id     → NULL  (force-releases any active session)
--   * current_owner_heartbeat_at → NULL
--   * claimed_at                → NULL
--   * mdns_name                 → NULL  (the next pair will set a fresh one)
--
-- Asserts the caller owns the target classroom (P0011) before the
-- write. The agent_token is preserved so the same physical device can
-- be re-paired to another classroom without SSH access to re-run
-- setup.sh.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.unpair_jetson(
    p_jetson_id UUID,
    p_teacher_id UUID
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_classroom_id UUID;
    v_classroom_teacher UUID;
BEGIN
    SELECT classroom_id INTO v_classroom_id
      FROM public.jetsons
     WHERE id = p_jetson_id
       FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Jetson nicht gefunden' USING ERRCODE = 'P0002';
    END IF;

    IF v_classroom_id IS NULL THEN
        RAISE EXCEPTION 'Jetson ist nicht gepaart' USING ERRCODE = 'P0002';
    END IF;

    SELECT teacher_id INTO v_classroom_teacher
      FROM public.classrooms
     WHERE id = v_classroom_id;

    IF v_classroom_teacher <> p_teacher_id THEN
        RAISE EXCEPTION 'Jetson gehört nicht zu diesem Lehrer' USING ERRCODE = 'P0011';
    END IF;

    UPDATE public.jetsons
       SET classroom_id               = NULL,
           current_owner_user_id      = NULL,
           current_owner_heartbeat_at = NULL,
           claimed_at                 = NULL,
           mdns_name                  = NULL,
           updated_at                 = NOW()
     WHERE id = p_jetson_id;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.unpair_jetson(UUID, UUID) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.unpair_jetson(UUID, UUID) FROM anon;
REVOKE EXECUTE ON FUNCTION public.unpair_jetson(UUID, UUID) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.unpair_jetson(UUID, UUID) TO service_role;


COMMIT;

-- END: 020_jetson_v2.sql


-- ============================================================
-- BEGIN: 021_workgroup_memberships_realtime_and_owner_check.sql
-- ============================================================
-- 021_workgroup_memberships_realtime_and_owner_check.sql
--
-- Three related fixes from the v2.3.0 follow-up audit:
--
-- 1. Add public.workgroup_memberships to the supabase_realtime
--    publication. Group-shared workflow/training/dataset visibility
--    via the audit table only updates live for sibling browsers if
--    the table is published; without this, a teacher adding a student
--    to a workgroup requires a manual page refresh on every other
--    member's browser before the new sibling's saves show up.
--
-- 2. Bake ownership / template-classroom checks into the SECURITY
--    DEFINER RPCs `update_workflow_blockly` and `restore_workflow_version`
--    so the database itself refuses a write the caller doesn't own.
--    The route layer's Python `_assert_workflow_owned` is still
--    correct as a defense-in-depth pre-check, but a future direct-RPC
--    caller (admin tool, alternate frontend) won't accidentally
--    bypass authorisation by skipping the route. P0002 keeps the
--    HTTP shape — `_resolve_visible_workgroup_ids`-style sibling
--    visibility is intentionally NOT extended to writes; only the
--    owner (or a classmate writing a template they own) may modify.
--
-- 3. Add `SET search_path = public` to
--    `public.touch_tutorial_progress_updated_at()` so the trigger
--    is robust to a non-default search_path (Supabase introspection
--    tools occasionally swap search_path under the trigger context;
--    every other trigger in this codebase pins search_path explicitly
--    and 016 was the only outlier).
--
-- Idempotent — re-runnable. The two workflow RPCs use
-- `CREATE OR REPLACE FUNCTION` with the SAME signature as migration
-- 018, so no DROP is needed. The publication-add is guarded by a DO
-- block that catches duplicate_object.

BEGIN;

-- ---------------------------------------------------------------------------
-- (1) Realtime publication: workgroup_memberships
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_publication_tables
    WHERE pubname = 'supabase_realtime'
      AND schemaname = 'public'
      AND tablename = 'workgroup_memberships'
  ) THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.workgroup_memberships;
  END IF;
EXCEPTION WHEN duplicate_object THEN
  -- Another concurrent migration already added the table — fine.
  NULL;
END $$;


-- ---------------------------------------------------------------------------
-- (2a) update_workflow_blockly with in-RPC ownership / template check
--
-- Mirrors the route-layer rule:
--   - the caller owns the workflow, OR
--   - the workflow is a classroom template AND the caller belongs to
--     that classroom (covers a teacher patching their own template
--     plus a student patching a clone they made — clones flip
--     is_template=FALSE so this branch only matches teacher writes
--     on the original template).
--
-- Group-shared workflows are intentionally NOT writable by siblings
-- here. The "sharing" semantics in this codebase are read-only:
-- a sibling can `clone` to author their own copy.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.update_workflow_blockly(
    p_workflow_id UUID,
    p_user_id UUID,
    p_blockly_json JSONB,
    p_name TEXT DEFAULT NULL,
    p_description TEXT DEFAULT NULL
)
RETURNS public.workflows
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_row public.workflows;
BEGIN
    -- Ownership / template-classroom gate. RAISE before any UPDATE so
    -- the BEFORE-UPDATE snapshot trigger never fires on an unauthorised
    -- caller (otherwise a forged caller could pollute workflow_versions
    -- even on a refused write).
    IF NOT EXISTS (
        SELECT 1
          FROM public.workflows
         WHERE id = p_workflow_id
           AND (
                owner_user_id = p_user_id
             OR (
                  is_template = TRUE
                  AND classroom_id IN (
                      SELECT classroom_id
                        FROM public.users
                       WHERE id = p_user_id
                  )
                )
           )
    ) THEN
        RAISE EXCEPTION 'Workflow nicht gefunden oder kein Zugriff.'
              USING ERRCODE = 'P0002';
    END IF;

    -- Transaction-local GUC: the BEFORE-UPDATE trigger reads this and
    -- writes it to workflow_versions.saved_by. The `true` 3rd arg
    -- scopes the setting to the current transaction so it can't leak
    -- to another call on the same pooled connection.
    PERFORM set_config('app.user_id', p_user_id::TEXT, true);

    UPDATE public.workflows
       SET blockly_json = p_blockly_json,
           name         = COALESCE(NULLIF(p_name, ''), name),
           description  = COALESCE(p_description, description)
     WHERE id = p_workflow_id
    RETURNING * INTO v_row;

    IF v_row.id IS NULL THEN
        RAISE EXCEPTION 'Workflow nicht gefunden' USING ERRCODE = 'P0002';
    END IF;

    RETURN v_row;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.update_workflow_blockly(UUID, UUID, JSONB, TEXT, TEXT) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.update_workflow_blockly(UUID, UUID, JSONB, TEXT, TEXT) FROM anon;
REVOKE EXECUTE ON FUNCTION public.update_workflow_blockly(UUID, UUID, JSONB, TEXT, TEXT) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.update_workflow_blockly(UUID, UUID, JSONB, TEXT, TEXT) TO service_role;


-- ---------------------------------------------------------------------------
-- (2b) restore_workflow_version with in-RPC ownership / template check
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.restore_workflow_version(
    p_workflow_id UUID,
    p_version_id UUID,
    p_user_id UUID
)
RETURNS public.workflows
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_row     public.workflows;
    v_payload JSONB;
BEGIN
    -- Same ownership / template-classroom rule as update_workflow_blockly.
    IF NOT EXISTS (
        SELECT 1
          FROM public.workflows
         WHERE id = p_workflow_id
           AND (
                owner_user_id = p_user_id
             OR (
                  is_template = TRUE
                  AND classroom_id IN (
                      SELECT classroom_id
                        FROM public.users
                       WHERE id = p_user_id
                  )
                )
           )
    ) THEN
        RAISE EXCEPTION 'Workflow nicht gefunden oder kein Zugriff.'
              USING ERRCODE = 'P0002';
    END IF;

    SELECT blockly_json INTO v_payload
      FROM public.workflow_versions
     WHERE id = p_version_id AND workflow_id = p_workflow_id;

    IF v_payload IS NULL THEN
        RAISE EXCEPTION 'Workflow-Version nicht gefunden' USING ERRCODE = 'P0002';
    END IF;

    PERFORM set_config('app.user_id', p_user_id::TEXT, true);

    UPDATE public.workflows
       SET blockly_json = v_payload
     WHERE id = p_workflow_id
    RETURNING * INTO v_row;

    IF v_row.id IS NULL THEN
        RAISE EXCEPTION 'Workflow nicht gefunden' USING ERRCODE = 'P0002';
    END IF;

    RETURN v_row;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.restore_workflow_version(UUID, UUID, UUID) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.restore_workflow_version(UUID, UUID, UUID) FROM anon;
REVOKE EXECUTE ON FUNCTION public.restore_workflow_version(UUID, UUID, UUID) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.restore_workflow_version(UUID, UUID, UUID) TO service_role;


-- ---------------------------------------------------------------------------
-- (3) Pin search_path on touch_tutorial_progress_updated_at()
--
-- The body is unchanged from migration 016 — only the `SET search_path`
-- option is added. `CREATE OR REPLACE FUNCTION` with the same signature
-- is the idempotent path.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.touch_tutorial_progress_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public
AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$;

COMMIT;

-- END: 021_workgroup_memberships_realtime_and_owner_check.sql


-- ============================================================================
-- LIVE-STATE DIVERGENCE PATCHES
-- ----------------------------------------------------------------------------
-- These four patches were applied to the live DB via dashboard/MCP between
-- the numbered migrations and never had their own .sql file in the repo.
-- They are captured here so a fresh `supabase db push` against an empty DB
-- produces a schema byte-identical to production.
--
-- Source: live introspection of pg_proc + pg_policies at fingerprint date
-- 2026-05-19 (verified via mcp__claude_ai_Supabase__execute_sql).
-- ============================================================================

-- ── PATCH: 008_workflows_harden_search_path (live ts 20260505074431) ──
-- All workflow-related SECURITY DEFINER functions pinned to a fixed
-- search_path so a search_path-injection attack can't redirect their
-- table lookups. Already captured inside 015/018/021 redefinitions.
-- No-op here; documented for traceability.

-- ── PATCH: 013b_revoke_get_remaining_credits_from_public (live ts 20260509163113) ──
REVOKE EXECUTE ON FUNCTION public.get_remaining_credits(uuid) FROM public, anon;
-- (authenticated keeps EXECUTE so logged-in students see their own counter.)

-- ── PATCH: 015b_snapshot_workflow_version_saved_by_and_policy (live ts 20260511161342) ──
-- Captured inside the 021 redefinition of snapshot_workflow_version which
-- already includes the saved_by capture via current_setting('app.user_id').
-- Tightened workflow_versions insert policy still uses WITH CHECK (true)
-- because the inserter is a trigger; the security-advisor-fixes migration
-- below replaces this with a current_setting-based check.

-- ── PATCH: 017b_consume_vision_quota_f48_null_skip (live ts 20260511161022) ──
-- Captured inside the 017 file's CREATE OR REPLACE; the live function
-- definition is byte-identical to what 017 produces after this patch.
-- (No-op here; documented for traceability.)

-- ============================================================================
-- END OF BASELINE
-- ============================================================================


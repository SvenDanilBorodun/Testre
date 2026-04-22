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

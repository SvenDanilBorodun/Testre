-- 049 — managed ACT training (the RunPod training build, spec §3.1 / §B.2):
-- a managed row is placed attempt by attempt on RunPod Community, RunPod
-- Secure or Modal by the cloud API, and every state change of such a row goes
-- through one of the ten compare-and-set functions below.
--   * public.trainings gains seven columns (managed, current_attempt,
--     train_seconds_used, status_note, step_advanced_at, dataset_fps,
--     unplaced_since), each granted to anon/authenticated by NAME (047 took the
--     table-level SELECT away, so a new column is invisible otherwise).
--   * public.training_attempts (one row per placement) and
--     public.training_checkpoints (one row per uploaded checkpoint), both
--     service-role only: RLS on, no policy, every privilege revoked from
--     PUBLIC/anon/authenticated, not in any publication.
--   * start_training_safe gains `p_managed BOOLEAN DEFAULT false` (the credit
--     logic is the baseline's byte for byte; the 7-argument call still
--     resolves, the old overload is dropped so exactly one exists).
--   * ten SECURITY INVOKER functions, EXECUTE service_role only:
--     claim_training_attempt, attach_training_attempt, release_training_attempt,
--     requeue_training, end_training_attempt, finish_training,
--     cancel_request_training, complete_cancel_training, claim_training_publish,
--     claim_checkpoint_mirror. P0049 = a lost compare-and-set (the API treats it
--     as „another instance or the sweep was first"), P0050 = cancel of a row
--     that is not active, P0051 = cancel while the finished model is published.
--   * update_training_progress (SECURITY DEFINER, EXECUTE anon+authenticated,
--     the worker's anon-key path) keeps 032's terminal guard verbatim and adds:
--     a managed row takes only status NULL/'running' from a worker (22023
--     otherwise), ignores p_total_steps and p_error_message (a host holding the
--     token can call this RPC directly, FR-1), every 'running' write resets
--     step_advanced_at, and a 'running' write with a lower step trims
--     loss_history to that step (an in-place restart). For a managed row it
--     also clamps p_current_step to [0, total_steps] and ignores a
--     p_current_loss that is NaN, ±Infinity or beyond ±1e30, so a host cannot
--     draw out-of-range numbers on the student's chart (V1-3).
--   * requeue_training, and update_training_progress's in-place-restart trim
--     when the write carries no loss of its own, set current_loss to the last
--     point of the trimmed curve (NULL when none is left); finish_training and
--     complete_cancel_training count the seconds of an attempt they end
--     (now − first_heartbeat_at) into train_seconds_used, as requeue_training
--     and end_training_attempt do, and never an attempt already lost or ended
--     (V2-6, V2-7, fix round 2026-10-19).
--
-- Proven on a local `supabase start` stack (Postgres 17,
-- public.ecr.aws/supabase/postgres:17.6.1.106; the guarded baseline + every
-- migration through 048, then 047, then this file, applied with psql as
-- `postgres`) with supabase/tests/049_managed_training_assertions.sql (all
-- PASS; denials only through has_*_privilege and PostgREST); the rollback was
-- refused while a managed row was active, applied after it was drained (its
-- self-check passed), and 049 re-applied cleanly afterwards.
-- Rollback: rollback/20261020130000_049_managed_training_rollback.sql (refuses
-- while a managed row is queued/running/cancel_requested; roll the cloud API
-- back first: a pre-feature API must never run while a managed row is active).
--
-- NUMBERING: 037 stays reserved („Eigene Objekte"). 048 (20261011120000) took
-- its number before 047 (20261020120000) was written; this file
-- (20261020130000) sorts after both, and its self-check asserts 047's state
-- (worker_token unreadable).
BEGIN;

-- 1. trainings columns ------------------------------------------------------
ALTER TABLE public.trainings
  ADD COLUMN IF NOT EXISTS managed            BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS current_attempt    INT     NOT NULL DEFAULT 0 CHECK (current_attempt >= 0),
  ADD COLUMN IF NOT EXISTS train_seconds_used INT     NOT NULL DEFAULT 0 CHECK (train_seconds_used >= 0),
  ADD COLUMN IF NOT EXISTS status_note        TEXT,
  ADD COLUMN IF NOT EXISTS step_advanced_at   TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS dataset_fps        REAL,
  ADD COLUMN IF NOT EXISTS unplaced_since     TIMESTAMPTZ;

GRANT SELECT (managed, current_attempt, train_seconds_used, status_note, step_advanced_at, dataset_fps, unplaced_since)
  ON public.trainings TO anon, authenticated;

-- 2. training_attempts -------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.training_attempts (
  training_id        INT  NOT NULL REFERENCES public.trainings(id) ON DELETE CASCADE,
  attempt_no         INT  NOT NULL CHECK (attempt_no >= 1),
  backend            TEXT CHECK (backend IN ('runpod_community','runpod_secure','modal')),
  cloud_ref          TEXT,
  gpu_type           TEXT,
  data_center        TEXT,
  cuda_version       TEXT,
  cost_per_hr        REAL,
  state              TEXT NOT NULL DEFAULT 'placing'
                     CHECK (state IN ('placing','starting','running','lost','ended')),
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at         TIMESTAMPTZ,
  first_heartbeat_at TIMESTAMPTZ,
  ended_at           TIMESTAMPTZ,
  end_reason         TEXT,
  resume_from_step   INT,
  resume_from_attempt INT,
  allowed_dcs        TEXT[],
  publish_claimed_at TIMESTAMPTZ,
  publish_owner      TEXT,
  publish_first_at   TIMESTAMPTZ,
  publish_tries      INT NOT NULL DEFAULT 0,
  PRIMARY KEY (training_id, attempt_no)
);
ALTER TABLE public.training_attempts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.training_attempts FROM PUBLIC, anon, authenticated;

-- 3. training_checkpoints ----------------------------------------------------
CREATE TABLE IF NOT EXISTS public.training_checkpoints (
  training_id    INT  NOT NULL REFERENCES public.trainings(id) ON DELETE CASCADE,
  attempt_no     INT  NOT NULL,
  step           INT  NOT NULL CHECK (step >= 1),
  state          TEXT NOT NULL DEFAULT 'uploading'
                 CHECK (state IN ('uploading','ready','refused','deleted')),
  manifest       JSONB NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  ready_at       TIMESTAMPTZ,
  mirrored_at    TIMESTAMPTZ,
  mirror_tries   INT NOT NULL DEFAULT 0,
  refused_reason TEXT,
  refused_by     INT,
  crash_attempts INT[] NOT NULL DEFAULT '{}',
  mirror_claimed_at TIMESTAMPTZ,
  mirror_owner   TEXT,
  PRIMARY KEY (training_id, attempt_no, step),
  FOREIGN KEY (training_id, attempt_no) REFERENCES public.training_attempts(training_id, attempt_no) ON DELETE CASCADE
);
ALTER TABLE public.training_checkpoints ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.training_checkpoints FROM PUBLIC, anon, authenticated;

-- 4. start_training_safe: + p_managed (DEFAULT false) ------------------------
DROP FUNCTION IF EXISTS public.start_training_safe(UUID, TEXT, TEXT, TEXT, JSONB, INT, UUID);
CREATE OR REPLACE FUNCTION public.start_training_safe(
  p_user_id         UUID,
  p_dataset_name    TEXT,
  p_model_name      TEXT,
  p_model_type      TEXT,
  p_training_params JSONB,
  p_total_steps     INT,
  p_worker_token    UUID,
  p_managed         BOOLEAN DEFAULT false
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
      training_params, total_steps, worker_token, managed
    ) VALUES (
      p_user_id, v_workgroup_id, 'queued', p_dataset_name, p_model_name, p_model_type,
      p_training_params, p_total_steps, p_worker_token, COALESCE(p_managed, false)
    )
    RETURNING id INTO v_new_id;

    RETURN QUERY SELECT v_new_id, (v_credits - v_used - 1);
  ELSE
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
      training_params, total_steps, worker_token, managed
    ) VALUES (
      p_user_id, 'queued', p_dataset_name, p_model_name, p_model_type,
      p_training_params, p_total_steps, p_worker_token, COALESCE(p_managed, false)
    )
    RETURNING id INTO v_new_id;

    RETURN QUERY SELECT v_new_id, (v_credits - v_used - 1);
  END IF;
END;
$$;
REVOKE ALL ON FUNCTION public.start_training_safe(UUID, TEXT, TEXT, TEXT, JSONB, INT, UUID, BOOLEAN) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.start_training_safe(UUID, TEXT, TEXT, TEXT, JSONB, INT, UUID, BOOLEAN) TO service_role;

-- 5. claim ------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.claim_training_attempt(p_training_id INT, p_expected_attempt INT)
RETURNS TABLE(o_attempt_no INT, o_worker_token UUID)
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE
  v_t public.trainings%ROWTYPE;
  v_state TEXT;
  v_token UUID := gen_random_uuid();
BEGIN
  SELECT * INTO v_t FROM public.trainings t WHERE t.id = p_training_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'training % not found', p_training_id USING ERRCODE = 'P0002';
  END IF;
  IF NOT v_t.managed OR v_t.status <> 'queued' OR v_t.current_attempt <> p_expected_attempt THEN
    RAISE EXCEPTION 'claim conflict for training %', p_training_id USING ERRCODE = 'P0049';
  END IF;
  IF p_expected_attempt > 0 THEN
    SELECT a.state INTO v_state FROM public.training_attempts a
     WHERE a.training_id = p_training_id AND a.attempt_no = p_expected_attempt;
    IF v_state IS NULL OR v_state NOT IN ('lost','ended') THEN
      RAISE EXCEPTION 'claim conflict for training %', p_training_id USING ERRCODE = 'P0049';
    END IF;
  END IF;
  INSERT INTO public.training_attempts(training_id, attempt_no, state)
  VALUES (p_training_id, p_expected_attempt + 1, 'placing');
  UPDATE public.trainings SET current_attempt = p_expected_attempt + 1, worker_token = v_token
   WHERE id = p_training_id;
  RETURN QUERY SELECT p_expected_attempt + 1, v_token;
END;
$$;

-- 6. attach -----------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.attach_training_attempt(
  p_training_id INT, p_attempt INT, p_backend TEXT, p_cloud_ref TEXT)
RETURNS VOID LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE v_rows INT;
BEGIN
  IF p_backend NOT IN ('runpod_community','runpod_secure','modal') OR p_cloud_ref IS NULL OR p_cloud_ref = '' THEN
    RAISE EXCEPTION 'invalid attach' USING ERRCODE = '22023';
  END IF;
  PERFORM 1 FROM public.trainings t WHERE t.id = p_training_id FOR UPDATE;
  UPDATE public.training_attempts a
     SET state = CASE WHEN a.state = 'placing' THEN 'starting' ELSE a.state END,
         backend = p_backend, cloud_ref = p_cloud_ref, started_at = COALESCE(a.started_at, now())
    FROM public.trainings t
   WHERE a.training_id = p_training_id AND a.attempt_no = p_attempt
     AND a.cloud_ref IS NULL AND a.state IN ('placing','starting','running')
     AND t.id = a.training_id AND t.managed AND t.current_attempt = p_attempt
     AND t.status IN ('queued','running');
  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows = 0 THEN
    RAISE EXCEPTION 'attach conflict for training %', p_training_id USING ERRCODE = 'P0049';
  END IF;
  UPDATE public.trainings SET unplaced_since = NULL WHERE id = p_training_id;
END;
$$;

-- 6b. release a claim that placed nothing (SR-11: costs no attempt) -----------
CREATE OR REPLACE FUNCTION public.release_training_attempt(p_training_id INT, p_attempt INT, p_note TEXT)
RETURNS VOID LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE v_rows INT;
BEGIN
  PERFORM 1 FROM public.trainings t WHERE t.id = p_training_id FOR UPDATE;
  DELETE FROM public.training_attempts a
   USING public.trainings t
   WHERE a.training_id = p_training_id AND a.attempt_no = p_attempt
     AND a.state = 'placing' AND a.cloud_ref IS NULL
     AND t.id = a.training_id AND t.managed AND t.current_attempt = p_attempt AND t.status = 'queued';
  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows = 0 THEN
    RAISE EXCEPTION 'release conflict for training %', p_training_id USING ERRCODE = 'P0049';
  END IF;
  UPDATE public.trainings
     SET current_attempt = p_attempt - 1, worker_token = NULL,
         status_note = COALESCE(p_note, status_note),
         unplaced_since = COALESCE(unplaced_since, now())
   WHERE id = p_training_id;
END;
$$;

-- 7. requeue ----------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.requeue_training(
  p_training_id INT, p_attempt INT, p_reason TEXT, p_note TEXT, p_resume_step INT, p_train_seconds INT)
RETURNS VOID LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE
  v_rows INT;
  v_hist JSONB;
BEGIN
  PERFORM 1 FROM public.trainings t WHERE t.id = p_training_id FOR UPDATE;
  UPDATE public.training_attempts a
     SET state = 'lost', ended_at = now(), end_reason = p_reason
    FROM public.trainings t
   WHERE a.training_id = p_training_id AND a.attempt_no = p_attempt
     AND a.state IN ('placing','starting','running')
     AND t.id = a.training_id AND t.managed AND t.current_attempt = p_attempt
     AND t.status IN ('queued','running');
  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows = 0 THEN
    RAISE EXCEPTION 'requeue conflict for training %', p_training_id USING ERRCODE = 'P0049';
  END IF;
  SELECT COALESCE(jsonb_agg(e ORDER BY ord), '[]'::jsonb) INTO v_hist
    FROM public.trainings t,
         jsonb_array_elements(COALESCE(t.loss_history, '[]'::jsonb)) WITH ORDINALITY AS x(e, ord)
   WHERE t.id = p_training_id AND (e->>'s')::int <= COALESCE(p_resume_step, 0);
  UPDATE public.trainings
     SET status = 'queued', worker_token = NULL, status_note = p_note,
         current_step = COALESCE(p_resume_step, 0),
         step_advanced_at = NULL,
         loss_history = v_hist,
         -- The loss of the last point kept, not of a step the requeue rolled back (V2-6).
         current_loss = CASE WHEN jsonb_typeof(v_hist -> -1 -> 'l') = 'number'
                             THEN (v_hist -> -1 ->> 'l')::real END,
         train_seconds_used = train_seconds_used + GREATEST(COALESCE(p_train_seconds, 0), 0)
   WHERE id = p_training_id;
END;
$$;

-- 8. end attempt (succeeded, before publish) --------------------------------
CREATE OR REPLACE FUNCTION public.end_training_attempt(p_training_id INT, p_attempt INT, p_reason TEXT, p_note TEXT, p_train_seconds INT)
RETURNS VOID LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE v_rows INT;
BEGIN
  PERFORM 1 FROM public.trainings t WHERE t.id = p_training_id FOR UPDATE;
  UPDATE public.training_attempts a
     SET state = 'ended', ended_at = now(), end_reason = p_reason
    FROM public.trainings t
   WHERE a.training_id = p_training_id AND a.attempt_no = p_attempt
     AND a.state IN ('placing','starting','running','lost')
     AND t.id = a.training_id AND t.managed AND t.current_attempt = p_attempt
     AND t.status IN ('queued','running');
  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows = 0 THEN
    RAISE EXCEPTION 'end conflict for training %', p_training_id USING ERRCODE = 'P0049';
  END IF;
  UPDATE public.trainings
     SET status = 'running', worker_token = NULL, status_note = p_note,
         train_seconds_used = train_seconds_used + GREATEST(COALESCE(p_train_seconds, 0), 0)
   WHERE id = p_training_id;
END;
$$;

-- 9. finish -----------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.finish_training(
  p_training_id INT, p_attempt INT, p_status TEXT, p_message TEXT, p_log_url TEXT)
RETURNS VOID LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE
  v_rows INT;
  v_seconds INT;
BEGIN
  IF p_status NOT IN ('succeeded','failed') THEN
    RAISE EXCEPTION 'invalid status %', p_status USING ERRCODE = '22023';
  END IF;
  PERFORM 1 FROM public.trainings t WHERE t.id = p_training_id FOR UPDATE;
  -- The trained seconds of an attempt this call ends (V2-7): the same
  -- now − first_heartbeat_at the API passes to requeue_training and
  -- end_training_attempt. An attempt they already ended (lost/ended) was counted.
  SELECT GREATEST(floor(EXTRACT(EPOCH FROM (now() - a.first_heartbeat_at)))::int, 0) INTO v_seconds
    FROM public.training_attempts a
   WHERE a.training_id = p_training_id AND a.attempt_no = p_attempt
     AND a.state NOT IN ('lost','ended') AND a.first_heartbeat_at IS NOT NULL;
  UPDATE public.trainings
     SET status = p_status, terminated_at = now(), worker_token = NULL, status_note = NULL,
         error_message = CASE WHEN p_status = 'failed' THEN p_message ELSE error_message END,
         log_url = COALESCE(p_log_url, log_url),
         current_step = CASE WHEN p_status = 'succeeded' THEN total_steps ELSE current_step END,
         train_seconds_used = train_seconds_used + COALESCE(v_seconds, 0)
   WHERE id = p_training_id AND managed AND current_attempt = p_attempt
     AND status IN ('queued','running');
  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows = 0 THEN
    RAISE EXCEPTION 'finish conflict for training %', p_training_id USING ERRCODE = 'P0049';
  END IF;
  UPDATE public.training_attempts
     SET state = 'ended', ended_at = COALESCE(ended_at, now()), end_reason = COALESCE(end_reason, p_status)
   WHERE training_id = p_training_id AND attempt_no = p_attempt AND state NOT IN ('lost','ended');
END;
$$;

-- 10. cancel CAS (all rows, managed and legacy) ------------------------------
CREATE OR REPLACE FUNCTION public.cancel_request_training(p_training_id INT, p_user_id UUID)
RETURNS TABLE(o_managed BOOLEAN, o_cloud_job_id TEXT, o_current_attempt INT, o_attempt_state TEXT,
              o_backend TEXT, o_cloud_ref TEXT, o_cancel_attempts INT)
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE
  v_t public.trainings%ROWTYPE;
  v_a public.training_attempts%ROWTYPE;
BEGIN
  SELECT * INTO v_t FROM public.trainings t WHERE t.id = p_training_id AND t.user_id = p_user_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'training % not found', p_training_id USING ERRCODE = 'P0002';
  END IF;
  IF v_t.status NOT IN ('queued','running','cancel_requested') THEN
    RAISE EXCEPTION 'training % not active', p_training_id USING ERRCODE = 'P0050';
  END IF;
  IF v_t.managed AND v_t.current_attempt > 0 THEN
    SELECT * INTO v_a FROM public.training_attempts a
     WHERE a.training_id = p_training_id AND a.attempt_no = v_t.current_attempt;
    IF v_a.state = 'ended' AND v_a.end_reason = 'succeeded' THEN
      RAISE EXCEPTION 'training % is publishing', p_training_id USING ERRCODE = 'P0051';
    END IF;
  END IF;
  UPDATE public.trainings
     SET status = 'cancel_requested', cancel_attempted_at = now(), cancel_attempts = cancel_attempts + 1
   WHERE id = p_training_id;
  RETURN QUERY SELECT v_t.managed, v_t.cloud_job_id, v_t.current_attempt, v_a.state, v_a.backend,
                      v_a.cloud_ref, v_t.cancel_attempts + 1;
END;
$$;

CREATE OR REPLACE FUNCTION public.complete_cancel_training(p_training_id INT, p_final TEXT, p_message TEXT)
RETURNS VOID LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE
  v_rows INT;
  v_seconds INT;
BEGIN
  IF p_final NOT IN ('canceled','failed') THEN
    RAISE EXCEPTION 'invalid final %', p_final USING ERRCODE = '22023';
  END IF;
  PERFORM 1 FROM public.trainings t WHERE t.id = p_training_id FOR UPDATE;
  -- The trained seconds of the attempt this call ends, exactly as finish_training
  -- counts them (now − first_heartbeat_at): an attempt a requeue or an end
  -- already took to lost/ended was counted by that call. A legacy row has none.
  SELECT GREATEST(floor(EXTRACT(EPOCH FROM (now() - a.first_heartbeat_at)))::int, 0) INTO v_seconds
    FROM public.trainings t
    JOIN public.training_attempts a ON a.training_id = t.id AND a.attempt_no = t.current_attempt
   WHERE t.id = p_training_id
     AND a.state NOT IN ('lost','ended') AND a.first_heartbeat_at IS NOT NULL;
  UPDATE public.trainings
     SET status = p_final, terminated_at = now(), worker_token = NULL, status_note = NULL,
         error_message = CASE WHEN p_final = 'failed' THEN p_message ELSE error_message END,
         train_seconds_used = train_seconds_used + COALESCE(v_seconds, 0)
   WHERE id = p_training_id AND status = 'cancel_requested';
  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows = 0 THEN
    RAISE EXCEPTION 'cancel conflict for training %', p_training_id USING ERRCODE = 'P0049';
  END IF;
  UPDATE public.training_attempts a
     SET state = 'ended', ended_at = now(), end_reason = 'canceled'
    FROM public.trainings t
   WHERE t.id = p_training_id AND a.training_id = t.id AND a.attempt_no = t.current_attempt
     AND a.state NOT IN ('lost','ended');
END;
$$;

-- 11. publish lease ----------------------------------------------------------
CREATE OR REPLACE FUNCTION public.claim_training_publish(p_training_id INT, p_attempt INT, p_lease_s INT, p_owner TEXT)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE v_rows INT;
BEGIN
  UPDATE public.training_attempts a
     SET publish_claimed_at = now(), publish_owner = p_owner,
         publish_first_at = COALESCE(a.publish_first_at, now())
    FROM public.trainings t
   WHERE a.training_id = p_training_id AND a.attempt_no = p_attempt
     AND a.state = 'ended' AND a.end_reason = 'succeeded'
     AND t.id = a.training_id AND t.current_attempt = p_attempt AND t.status = 'running'
     AND (a.publish_claimed_at IS NULL OR a.publish_owner = p_owner
          OR a.publish_claimed_at < now() - make_interval(secs => p_lease_s));
  GET DIAGNOSTICS v_rows = ROW_COUNT;
  RETURN v_rows = 1;
END;
$$;

-- 11b. mirror lease (one instance mirrors a checkpoint at a time) ----------------
CREATE OR REPLACE FUNCTION public.claim_checkpoint_mirror(
  p_training_id INT, p_attempt INT, p_step INT, p_lease_s INT, p_owner TEXT)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE v_rows INT;
BEGIN
  UPDATE public.training_checkpoints c
     SET mirror_claimed_at = now(), mirror_owner = p_owner
   WHERE c.training_id = p_training_id AND c.attempt_no = p_attempt AND c.step = p_step
     AND c.state = 'ready' AND c.mirrored_at IS NULL
     AND (c.mirror_claimed_at IS NULL OR c.mirror_owner = p_owner
          OR c.mirror_claimed_at < now() - make_interval(secs => p_lease_s));
  GET DIAGNOSTICS v_rows = ROW_COUNT;
  RETURN v_rows = 1;
END;
$$;

-- 12. update_training_progress: managed guard + step_advanced_at -------------
CREATE OR REPLACE FUNCTION public.update_training_progress(
  p_training_id  INT,
  p_token        UUID,
  p_status       TEXT  DEFAULT NULL,
  p_current_step INT   DEFAULT NULL,
  p_total_steps  INT   DEFAULT NULL,
  p_current_loss REAL  DEFAULT NULL,
  p_error_message TEXT DEFAULT NULL,
  p_log_url      TEXT  DEFAULT NULL
) RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_rows        INT;
  v_new_history JSONB;
  v_len         INT;
  v_managed     BOOLEAN;
  v_prev_status TEXT;
  v_attempt     INT;
  v_total       INT;
  v_step        INT  := p_current_step;
  v_loss        REAL := p_current_loss;
BEGIN
  IF p_status IS NOT NULL AND p_status NOT IN ('queued','running','succeeded','failed','canceled') THEN
    RAISE EXCEPTION 'Invalid status: %', p_status USING ERRCODE = '22023';
  END IF;

  SELECT managed, status, current_attempt, total_steps INTO v_managed, v_prev_status, v_attempt, v_total
    FROM public.trainings WHERE id = p_training_id;
  IF v_managed AND p_status IS NOT NULL AND p_status <> 'running' THEN
    RAISE EXCEPTION 'Managed training: status % is set by the API', p_status USING ERRCODE = '22023';
  END IF;
  -- A managed row's chart is the API's range (V1-3): the step in [0, total_steps],
  -- a loss only when finite and within ±1e30 (NaN compares above every number).
  IF v_managed THEN
    IF v_step IS NOT NULL THEN
      v_step := LEAST(GREATEST(v_step, 0), v_total);
    END IF;
    IF v_loss IS NOT NULL AND NOT (v_loss <> 'NaN'::real AND v_loss BETWEEN -1e30 AND 1e30) THEN
      v_loss := NULL;
    END IF;
  END IF;

  IF v_step IS NOT NULL AND v_loss IS NOT NULL THEN
    SELECT COALESCE(loss_history, '[]'::jsonb) || jsonb_build_array(
             jsonb_build_object(
               's', v_step,
               'l', v_loss,
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

  UPDATE public.trainings
  SET
    status        = COALESCE(p_status,        status),
    current_step  = COALESCE(v_step,          current_step),
    -- A managed row's step count and error text are the API's alone (E11, FR-1):
    -- a host holding its token can call this RPC directly.
    total_steps   = CASE WHEN managed THEN total_steps   ELSE COALESCE(p_total_steps,   total_steps)   END,
    -- An in-place restart (the trim below) leaves the loss of the trimmed curve's
    -- last point (NULL when none is left), never one of a step it dropped, as
    -- requeue_training does; a write that carries its own loss keeps it.
    current_loss  = CASE
      WHEN p_status = 'running' AND v_step IS NOT NULL AND v_step < COALESCE(current_step, 0)
        THEN COALESCE(v_loss,
               (SELECT CASE WHEN jsonb_typeof(e->'l') = 'number' THEN (e->>'l')::real END
                  FROM jsonb_array_elements(COALESCE(v_new_history, loss_history)) WITH ORDINALITY AS x(e, ord)
                 WHERE (e->>'s')::int <= v_step
                 ORDER BY ord DESC LIMIT 1))
      ELSE COALESCE(v_loss, current_loss)
    END,
    error_message = CASE WHEN managed THEN error_message ELSE COALESCE(p_error_message, error_message) END,
    loss_history  = CASE
      WHEN p_status = 'running' AND v_step IS NOT NULL AND v_step < COALESCE(current_step, 0)
        THEN (SELECT COALESCE(jsonb_agg(e ORDER BY ord), '[]'::jsonb)
                FROM jsonb_array_elements(COALESCE(v_new_history, loss_history)) WITH ORDINALITY AS x(e, ord)
               WHERE (e->>'s')::int <= v_step)
      ELSE COALESCE(v_new_history, loss_history)
    END,
    log_url       = CASE
      WHEN p_status IN ('succeeded','failed','canceled')
        THEN COALESCE(p_log_url, log_url)
      ELSE log_url
    END,
    last_progress_at = NOW(),
    step_advanced_at = CASE
      WHEN step_advanced_at IS NULL OR p_status = 'running' THEN NOW()
      WHEN v_step IS NOT NULL AND v_step > COALESCE(current_step, 0) THEN NOW()
      ELSE step_advanced_at
    END,
    status_note   = CASE
      WHEN managed AND p_status = 'running' AND status = 'queued' THEN NULL
      ELSE status_note
    END,
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
    AND status NOT IN ('succeeded','failed','canceled','cancel_requested');

  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows = 0 THEN
    RAISE EXCEPTION 'Invalid worker token, training not found, or training already terminal (or cancel-requested)'
      USING ERRCODE = 'P0001';
  END IF;

  IF v_managed AND p_status = 'running' THEN
    UPDATE public.training_attempts
       SET state = 'running', first_heartbeat_at = COALESCE(first_heartbeat_at, NOW())
     WHERE training_id = p_training_id AND attempt_no = v_attempt AND state IN ('placing','starting');
  END IF;
END;
$$;
REVOKE ALL ON FUNCTION public.update_training_progress(INT, UUID, TEXT, INT, INT, REAL, TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.update_training_progress(INT, UUID, TEXT, INT, INT, REAL, TEXT, TEXT) TO anon, authenticated;

-- 13. grants on the new service-role functions -------------------------------
REVOKE ALL ON FUNCTION public.claim_training_attempt(INT, INT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.attach_training_attempt(INT, INT, TEXT, TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.requeue_training(INT, INT, TEXT, TEXT, INT, INT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.end_training_attempt(INT, INT, TEXT, TEXT, INT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.finish_training(INT, INT, TEXT, TEXT, TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.cancel_request_training(INT, UUID) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.complete_cancel_training(INT, TEXT, TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.claim_training_publish(INT, INT, INT, TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.release_training_attempt(INT, INT, TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.claim_checkpoint_mirror(INT, INT, INT, INT, TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_training_attempt(INT, INT) TO service_role;
GRANT EXECUTE ON FUNCTION public.attach_training_attempt(INT, INT, TEXT, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.requeue_training(INT, INT, TEXT, TEXT, INT, INT) TO service_role;
GRANT EXECUTE ON FUNCTION public.end_training_attempt(INT, INT, TEXT, TEXT, INT) TO service_role;
GRANT EXECUTE ON FUNCTION public.finish_training(INT, INT, TEXT, TEXT, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.cancel_request_training(INT, UUID) TO service_role;
GRANT EXECUTE ON FUNCTION public.complete_cancel_training(INT, TEXT, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.claim_training_publish(INT, INT, INT, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.release_training_attempt(INT, INT, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.claim_checkpoint_mirror(INT, INT, INT, INT, TEXT) TO service_role;

-- 14. self-check --------------------------------------------------------------
DO $$
DECLARE v_role TEXT; v_fn TEXT; v_tbl TEXT; v_priv TEXT;
BEGIN
  FOREACH v_role IN ARRAY ARRAY['anon','authenticated'] LOOP
    FOREACH v_tbl IN ARRAY ARRAY['public.training_attempts','public.training_checkpoints'] LOOP
      FOREACH v_priv IN ARRAY ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER'] LOOP
        IF has_table_privilege(v_role, v_tbl, v_priv) THEN
          RAISE EXCEPTION '049: % still holds % on %', v_role, v_priv, v_tbl;
        END IF;
      END LOOP;
    END LOOP;
    FOREACH v_fn IN ARRAY ARRAY[
      'public.claim_training_attempt(integer,integer)',
      'public.attach_training_attempt(integer,integer,text,text)',
      'public.requeue_training(integer,integer,text,text,integer,integer)',
      'public.end_training_attempt(integer,integer,text,text,integer)',
      'public.finish_training(integer,integer,text,text,text)',
      'public.cancel_request_training(integer,uuid)',
      'public.complete_cancel_training(integer,text,text)',
      'public.claim_training_publish(integer,integer,integer,text)',
      'public.release_training_attempt(integer,integer,text)',
      'public.claim_checkpoint_mirror(integer,integer,integer,integer,text)',
      'public.start_training_safe(uuid,text,text,text,jsonb,integer,uuid,boolean)'] LOOP
      IF has_function_privilege(v_role, v_fn, 'EXECUTE') THEN
        RAISE EXCEPTION '049: % can execute %', v_role, v_fn;
      END IF;
    END LOOP;
    IF has_column_privilege(v_role, 'public.trainings', 'worker_token', 'SELECT') THEN
      RAISE EXCEPTION '049: % can read trainings.worker_token', v_role;
    END IF;
    IF NOT has_column_privilege(v_role, 'public.trainings', 'status_note', 'SELECT') THEN
      RAISE EXCEPTION '049: % cannot read trainings.status_note', v_role;
    END IF;
  END LOOP;
  FOREACH v_fn IN ARRAY ARRAY[
    'public.claim_training_attempt(integer,integer)', 'public.attach_training_attempt(integer,integer,text,text)',
    'public.release_training_attempt(integer,integer,text)',
    'public.requeue_training(integer,integer,text,text,integer,integer)', 'public.end_training_attempt(integer,integer,text,text,integer)',
    'public.finish_training(integer,integer,text,text,text)', 'public.cancel_request_training(integer,uuid)',
    'public.complete_cancel_training(integer,text,text)', 'public.claim_training_publish(integer,integer,integer,text)',
    'public.claim_checkpoint_mirror(integer,integer,integer,integer,text)',
    'public.start_training_safe(uuid,text,text,text,jsonb,integer,uuid,boolean)'] LOOP
    IF NOT has_function_privilege('service_role', v_fn, 'EXECUTE') THEN
      RAISE EXCEPTION '049: service_role cannot execute %', v_fn;
    END IF;
  END LOOP;
  FOREACH v_role IN ARRAY ARRAY['anon','authenticated'] LOOP
    IF NOT has_function_privilege(v_role, 'public.update_training_progress(integer,uuid,text,integer,integer,real,text,text)', 'EXECUTE') THEN
      RAISE EXCEPTION '049: % lost EXECUTE on update_training_progress', v_role;
    END IF;
  END LOOP;
  IF EXISTS (SELECT 1 FROM pg_publication_tables WHERE tablename IN ('training_attempts','training_checkpoints')) THEN
    RAISE EXCEPTION '049: an attempts table is in a publication';
  END IF;
  IF (SELECT count(*) FROM pg_proc WHERE proname = 'start_training_safe' AND pronamespace = 'public'::regnamespace) <> 1 THEN
    RAISE EXCEPTION '049: start_training_safe must have exactly one overload';
  END IF;
END $$;

COMMIT;

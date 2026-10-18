-- Rollback for 049_managed_training. Refuses while any managed training is
-- queued, running or cancel_requested (cancel them or let them finish first),
-- then drops the ten managed-training functions and both tables
-- (training_checkpoints, training_attempts: every attempt and checkpoint record
-- is lost), restores start_training_safe to its 7-argument baseline body
-- (revoked from PUBLIC, anon, authenticated; EXECUTE service_role) and
-- update_training_progress to 032's body (EXECUTE anon, authenticated), drops
-- the seven trainings columns, and ends with a has_function_privilege
-- self-check. Both function bodies are verbatim copies
-- (robotis_ai_setup/tests/test_migration_049_text.py compares them).
--
-- ORDER: roll the cloud API back FIRST (TRAINING_DISPATCH=legacy, wait until
-- no managed row is active, then the API rollback): a pre-feature API must
-- never run while a managed row is active, its reconcilers fail such rows.
-- 047 stays: the self-check asserts that worker_token is still unreadable.
-- Proven on a local stack 2026-10-18 (refused with active rows, applied after
-- draining, a student's 7-argument PostgREST call then answered 42501, 049
-- re-applied cleanly).
BEGIN;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.trainings
              WHERE managed AND status IN ('queued','running','cancel_requested')) THEN
    RAISE EXCEPTION '049 rollback refused: managed trainings are still active (cancel them or let them finish first)';
  END IF;
END $$;

DROP FUNCTION IF EXISTS public.claim_training_attempt(INT, INT);
DROP FUNCTION IF EXISTS public.attach_training_attempt(INT, INT, TEXT, TEXT);
DROP FUNCTION IF EXISTS public.claim_checkpoint_mirror(INT, INT, INT, INT, TEXT);
DROP FUNCTION IF EXISTS public.release_training_attempt(INT, INT, TEXT);
DROP FUNCTION IF EXISTS public.requeue_training(INT, INT, TEXT, TEXT, INT, INT);
DROP FUNCTION IF EXISTS public.end_training_attempt(INT, INT, TEXT, TEXT, INT);
DROP FUNCTION IF EXISTS public.finish_training(INT, INT, TEXT, TEXT, TEXT);
DROP FUNCTION IF EXISTS public.cancel_request_training(INT, UUID);
DROP FUNCTION IF EXISTS public.complete_cancel_training(INT, TEXT, TEXT);
DROP FUNCTION IF EXISTS public.claim_training_publish(INT, INT, INT, TEXT);

DROP TABLE IF EXISTS public.training_checkpoints;
DROP TABLE IF EXISTS public.training_attempts;

DROP FUNCTION IF EXISTS public.start_training_safe(UUID, TEXT, TEXT, TEXT, JSONB, INT, UUID, BOOLEAN);
-- Baseline body, verbatim (00000000000000_baseline.sql, the group-aware version):
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

-- The baseline revokes from PUBLIC here and from anon/authenticated much later; both at once (SR-6):
REVOKE ALL ON FUNCTION public.start_training_safe(UUID, TEXT, TEXT, TEXT, JSONB, INT, UUID) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.start_training_safe(UUID, TEXT, TEXT, TEXT, JSONB, INT, UUID) TO service_role;

-- 032 body, verbatim:
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
  -- atomically with the row lookup — no chance for a worker write to slip in
  -- between a SELECT and an UPDATE. (Migration 010, preserved; 032 adds
  -- 'cancel_requested' so a worker can't write past a pending cancel.)
  UPDATE public.trainings
  SET
    status        = COALESCE(p_status,        status),
    current_step  = COALESCE(p_current_step,  current_step),
    total_steps   = COALESCE(p_total_steps,   total_steps),
    current_loss  = COALESCE(p_current_loss,  current_loss),
    error_message = COALESCE(p_error_message, error_message),
    loss_history  = COALESCE(v_new_history,   loss_history),
    -- 028: full-log pointer, terminal transitions only.
    log_url       = CASE
      WHEN p_status IN ('succeeded','failed','canceled')
        THEN COALESCE(p_log_url, log_url)
      ELSE log_url
    END,
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
    AND status NOT IN ('succeeded','failed','canceled','cancel_requested');

  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows = 0 THEN
    RAISE EXCEPTION 'Invalid worker token, training not found, or training already terminal (or cancel-requested)'
      USING ERRCODE = 'P0001';
  END IF;
END;
$$;
REVOKE ALL ON FUNCTION public.update_training_progress(INT, UUID, TEXT, INT, INT, REAL, TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.update_training_progress(INT, UUID, TEXT, INT, INT, REAL, TEXT, TEXT) TO anon, authenticated;

ALTER TABLE public.trainings
  DROP COLUMN IF EXISTS managed,
  DROP COLUMN IF EXISTS current_attempt,
  DROP COLUMN IF EXISTS train_seconds_used,
  DROP COLUMN IF EXISTS status_note,
  DROP COLUMN IF EXISTS step_advanced_at,
  DROP COLUMN IF EXISTS dataset_fps,
  DROP COLUMN IF EXISTS unplaced_since;

DO $$
DECLARE v_role TEXT;
BEGIN
  FOREACH v_role IN ARRAY ARRAY['anon','authenticated'] LOOP
    IF has_function_privilege(v_role, 'public.start_training_safe(uuid,text,text,text,jsonb,integer,uuid)', 'EXECUTE') THEN
      RAISE EXCEPTION '049 rollback: % can execute start_training_safe', v_role;
    END IF;
    IF NOT has_function_privilege(v_role, 'public.update_training_progress(integer,uuid,text,integer,integer,real,text,text)', 'EXECUTE') THEN
      RAISE EXCEPTION '049 rollback: % lost EXECUTE on update_training_progress', v_role;
    END IF;
    IF has_column_privilege(v_role, 'public.trainings', 'worker_token', 'SELECT') THEN
      RAISE EXCEPTION '049 rollback: % can read trainings.worker_token (047 must stay)', v_role;
    END IF;
  END LOOP;
  IF NOT has_function_privilege('service_role', 'public.start_training_safe(uuid,text,text,text,jsonb,integer,uuid)', 'EXECUTE') THEN
    RAISE EXCEPTION '049 rollback: service_role cannot execute start_training_safe';
  END IF;
  IF (SELECT count(*) FROM pg_proc WHERE proname = 'start_training_safe' AND pronamespace = 'public'::regnamespace) <> 1 THEN
    RAISE EXCEPTION '049 rollback: start_training_safe must have exactly one overload';
  END IF;
  IF to_regclass('public.training_attempts') IS NOT NULL OR to_regclass('public.training_checkpoints') IS NOT NULL THEN
    RAISE EXCEPTION '049 rollback: an attempts table survived';
  END IF;
END $$;

COMMIT;

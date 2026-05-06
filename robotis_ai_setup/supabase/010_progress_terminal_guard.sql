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

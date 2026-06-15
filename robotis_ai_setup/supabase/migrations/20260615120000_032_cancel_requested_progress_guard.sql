-- 032: add 'cancel_requested' to the update_training_progress terminal guard
--
-- Closes a re-opened cost-bomb (the bug migration 023 *claimed* to fix). The
-- /trainings/cancel Phase-1 sets status='cancel_requested' but does NOT null
-- worker_token (the token is only nulled when Modal-cancel succeeds in Phase-2).
-- The 010/028 terminal guard was `status NOT IN ('succeeded','failed','canceled')`
-- — 'cancel_requested' was NOT in it. So after a cancel whose Modal terminate
-- failed, the worker's progress writes still SUCCEEDED → it kept training to its
-- timeout cap ("cancel just continues on Modal"), and on success it could even
-- flip the canceled row to 'succeeded'. By adding 'cancel_requested' to the
-- guard, the worker's very next progress RPC returns 0 rows → P0001, which the
-- worker (_is_terminal_cancel_error) already treats as a cancel signal: it kills
-- the training subprocess and exits, freeing the GPU within one progress
-- interval even if the Modal-side terminate never lands. The worker NEVER writes
-- 'cancel_requested' itself (only the Cloud API does, via a direct table
-- UPDATE), so the valid-input check and the terminal CASE clauses are unchanged.
--
-- Signature is IDENTICAL to migration 028 (same 8-arg list), so this is a plain
-- CREATE OR REPLACE — no DROP, grants are preserved (re-granted below for
-- idempotency). The body is migration 028's VERBATIM except the single WHERE
-- guard line and the P0001 message. DO NOT slim the body (010 terminal-state
-- semantics + 028 log_url-on-terminal are preserved).

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

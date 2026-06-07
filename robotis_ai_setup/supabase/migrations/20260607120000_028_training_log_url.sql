-- 028: trainings.log_url + update_training_progress(p_log_url)
--
-- leLab-comparison PR-5a (2026-06-07). On failure the worker kept only a
-- 2 KB truncated head+tail of a 4000-line ring buffer in error_message —
-- support could not diagnose a student's failed Modal run beyond that
-- blob. The worker now uploads the FULL stdout as training_log.txt into
-- the (private) HF model repo it already owns, and stores the URL here.
-- Surfaced TEACHER/ADMIN-side only (StudentTrainingHistoryDrawer); the
-- student UX stays the clean German progress bar.
--
-- RPC contract notes (read before touching):
--   * Adding p_log_url CHANGES THE FUNCTION SIGNATURE. CREATE OR REPLACE
--     with a different parameter list would create an OVERLOAD, and
--     PostgREST rpc() calls by name would become ambiguous — so the old
--     signature is DROPped and the new one CREATEd in this single
--     transactional migration (no caller-visible window).
--   * The body below re-emits migration 010's terminal-state guard
--     VERBATIM (the WHERE clause). A worker can still never overwrite a
--     canceled row with succeeded (the start->cancel x10 cost-bomb fix,
--     migrations 010 + 023). DO NOT slim this body.
--   * log_url is written ONLY on the terminal transition, inside the
--     same guarded UPDATE — no separate write path, no TOCTOU.
--   * Grants mirror 010/024: anon + authenticated EXECUTE (the Modal
--     worker holds only the anon key + per-row worker_token).

ALTER TABLE public.trainings ADD COLUMN IF NOT EXISTS log_url TEXT;

COMMENT ON COLUMN public.trainings.log_url IS
  'Full worker stdout (training_log.txt in the private HF model repo), '
  'written by update_training_progress on the terminal transition. '
  'Teacher/admin diagnostics only.';

DROP FUNCTION IF EXISTS public.update_training_progress(
  INT, UUID, TEXT, INT, INT, REAL, TEXT);

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
  -- atomically with the row lookup — no chance for a worker write to
  -- slip in between a SELECT and an UPDATE. (Migration 010, preserved.)
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
    AND status NOT IN ('succeeded','failed','canceled');

  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows = 0 THEN
    RAISE EXCEPTION 'Invalid worker token, training not found, or training already terminal'
      USING ERRCODE = 'P0001';
  END IF;
END;
$$;

REVOKE ALL ON FUNCTION public.update_training_progress(INT, UUID, TEXT, INT, INT, REAL, TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.update_training_progress(INT, UUID, TEXT, INT, INT, REAL, TEXT, TEXT) TO anon, authenticated;

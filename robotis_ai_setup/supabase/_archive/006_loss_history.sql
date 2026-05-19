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

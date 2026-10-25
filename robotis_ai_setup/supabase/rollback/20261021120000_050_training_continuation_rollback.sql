-- Rollback for 050_training_continuation. Refuses while any managed training is
-- queued, running or cancel_requested, then drops the two 050 functions
-- (start_continuation_safe, cap_training_attempt) and the 8-argument
-- finish_training, restores 049's finish_training (5 arguments), requeue_training,
-- claim_training_publish and cancel_request_training VERBATIM (byte-for-byte copies
-- from 20261020130000_049_managed_training.sql, which
-- robotis_ai_setup/tests/test_migration_050_text.py compares), drops the
-- training_kept_checkpoints table and the three trainings columns, and ends with a
-- has_function_privilege self-check (the request roles cannot execute the
-- 5-argument finish_training and cannot read worker_token — 047 stays — service
-- role can, exactly one finish_training, both 050 functions and the kept table
-- gone). Kept R2 objects are orphaned; the bucket's 9-day keep/ rule deletes them.
--
-- ORDER: roll the cloud API back FIRST. A WP-1-or-older API never recovers a
-- `capped` publish, so wait until no managed row is queued/running/cancel_requested
-- before the API rollback and this one; then 049's rollback if needed.
-- Proven on a local stack 2026-10-20 (refused with an active row, applied after
-- draining; 049's assertions then print 69 PASS again, and 050 re-applies).
BEGIN;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.trainings
              WHERE managed AND status IN ('queued','running','cancel_requested')) THEN
    RAISE EXCEPTION '050 rollback refused: managed trainings are still active (cancel them or let them finish first)';
  END IF;
END $$;

DROP FUNCTION IF EXISTS public.start_continuation_safe(UUID, INT, TEXT);
DROP FUNCTION IF EXISTS public.cap_training_attempt(INT, INT, INT, TEXT, TEXT, INT);
DROP FUNCTION IF EXISTS public.finish_training(INT, INT, TEXT, TEXT, TEXT, TEXT, INT, TIMESTAMPTZ);

-- 049's four bodies, verbatim ------------------------------------------------

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

REVOKE ALL ON FUNCTION public.finish_training(INT, INT, TEXT, TEXT, TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.finish_training(INT, INT, TEXT, TEXT, TEXT) TO service_role;

DROP TABLE IF EXISTS public.training_kept_checkpoints;
ALTER TABLE public.trainings
  DROP COLUMN IF EXISTS continued_from,
  DROP COLUMN IF EXISTS continuable_until,
  DROP COLUMN IF EXISTS capped_step;

-- self-check -----------------------------------------------------------------
DO $$
DECLARE v_role TEXT;
BEGIN
  FOREACH v_role IN ARRAY ARRAY['anon','authenticated'] LOOP
    IF has_function_privilege(v_role, 'public.finish_training(integer,integer,text,text,text)', 'EXECUTE') THEN
      RAISE EXCEPTION '050 rollback: % can execute finish_training', v_role;
    END IF;
    IF has_column_privilege(v_role, 'public.trainings', 'worker_token', 'SELECT') THEN
      RAISE EXCEPTION '050 rollback: % can read trainings.worker_token (047 must stay)', v_role;
    END IF;
  END LOOP;
  IF NOT has_function_privilege('service_role', 'public.finish_training(integer,integer,text,text,text)', 'EXECUTE') THEN
    RAISE EXCEPTION '050 rollback: service_role cannot execute finish_training';
  END IF;
  IF (SELECT count(*) FROM pg_proc WHERE proname = 'finish_training' AND pronamespace = 'public'::regnamespace) <> 1 THEN
    RAISE EXCEPTION '050 rollback: finish_training must have exactly one overload';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_proc WHERE proname IN ('start_continuation_safe','cap_training_attempt')
              AND pronamespace = 'public'::regnamespace) THEN
    RAISE EXCEPTION '050 rollback: a 050 function survives';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'training_kept_checkpoints' AND relnamespace = 'public'::regnamespace) THEN
    RAISE EXCEPTION '050 rollback: training_kept_checkpoints survives';
  END IF;
END $$;

COMMIT;

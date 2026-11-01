-- Rollback for 052_credit_ledger (the training-hour ledger). Spec §19.
--   1. Refuses (RAISE) while any trainings row with credit_scheme = 'ledger' is
--      queued, running or cancel_requested.
--   2. Drops the seven triggers on trainings/users/workgroups (the credit-column
--      guard, the two settlements, the two closes, the two re-parent triggers)
--      and every 052 function: stop_training, every credit_* except
--      credit_ledger_immutable, record_training_refusal, every _credit_* and the
--      trigger functions.
--   3. Drops the four re-created signatures (start_training_safe 10 arguments,
--      start_continuation_safe 4, cancel_request_training 3, finish_training 9)
--      and restores VERBATIM, each with its old grants (REVOKE ALL FROM PUBLIC,
--      anon, authenticated; GRANT EXECUTE TO service_role): start_training_safe
--      (049, 8 arguments), start_continuation_safe, cancel_request_training,
--      claim_training_publish, cap_training_attempt, requeue_training,
--      finish_training (050), complete_cancel_training (049),
--      get_remaining_credits, get_teacher_credit_summary, adjust_student_credits,
--      adjust_workgroup_credits (the baseline's last bodies), and
--      update_training_progress (049, same signature) with 049's own grants
--      (PUBLIC nothing, anon and authenticated EXECUTE). Each body is the last
--      definition in apply order, copied byte for byte (compared by
--      robotis_ai_setup/tests/test_credit_ledger_sql.py).
--   4. credit_scheme defaults to 'derived' again (else the old start violates
--      trainings_ledger_shape); service_role gets INSERT, TRUNCATE, TRIGGER and
--      REFERENCES on trainings back (the 051 state).
--   5. KEEPS credit_ledger and its two immutability triggers (never deleted,
--      decision 1), credit_accounts, credit_reasons (ordinary data, still
--      erasable by hand), training_start_refusals, the ten new trainings columns
--      and training_checkpoints.config_complaints; nothing reads them afterwards.
--   6. A self-check: one overload each of the five, service_role can INSERT
--      trainings, the ledger keeps its triggers, request roles hold nothing on the
--      four tables, no 052 function survived, and update_training_progress is
--      049's body (the md5 of its prosrc) that anon and authenticated execute.
--
-- CONSEQUENCE: the old derived credits come back with the per-user and group
-- allocations as they were at 052 (users.training_credits /
-- workgroups.shared_credits were never touched), and every training started
-- under the ledger that is not failed/canceled counts as one used credit.
--
-- ORDER (spec §19; it replaces Rule §6's managed list where they overlap):
--   (1) optionally MANAGED_MODAL_RUNG=1 after a `modal deploy`;
--   (2) TRAINING_DISPATCH=legacy (no new trainings);
--   (3) wait until no ledger (and no managed) row is queued/running/cancel_requested;
--   (4) roll the cloud API and the teacher web back to v2.36.0 (RUNPOD_TRAINER_IMAGE
--       reset as Rule §6 says). The v2.36.0 API's budget_for reads the GLOBAL cap
--       (12 600 s), not a row's reservation or lost_seconds: an API rolled back
--       while TRAINING_DISPATCH=managed and balances exist would give new rows a
--       3.5 h budget on a 1–3 h reservation, which is why legacy and the drain
--       come first. The v2.36.0 API boots against 052 (its probe bodies resolve);
--   (5) this file;
--   (5') TRAINING_DISPATCH=managed again (under v2.36.0 that is the RunPod path
--        with derived credits; left at legacy every ACT start goes to the stopped
--        Modal `train` app);
--   (6) then, only if needed, 051 -> 050 -> 049 -> 047 as before.
-- 052 is NOT re-runnable after this rollback (its tables are kept, so its
-- CREATE TABLE fails); re-enabling the ledger is a new migration that adopts the
-- kept tables.
-- Proven on a local stack (see supabase/tests/052_credit_ledger_assertions.sql's
-- header): refused with an active ledger row and changed nothing; applied after
-- the rows were drained („052 rollback self-check passed"); the old 8-argument
-- start then creates a derived row, the old get_remaining_credits and
-- get_teacher_credit_summary answer from the old columns, adjust_student_credits
-- runs its old body, the ledger is kept and still refuses an UPDATE (P0068), and
-- cancel_request_training is the two-argument function again.
BEGIN;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.trainings
              WHERE credit_scheme = 'ledger' AND status IN ('queued','running','cancel_requested')) THEN
    RAISE EXCEPTION '052 rollback refused: a ledger training is still active (set TRAINING_DISPATCH=legacy and wait until every ledger training ended)';
  END IF;
END $$;

-- 2. the seven triggers, then every 052 function ------------------------------
DROP TRIGGER trg_0_trainings_credit_guard ON public.trainings;
DROP TRIGGER trg_9_trainings_credit_settle ON public.trainings;
DROP TRIGGER trg_9_trainings_credit_settle_delete ON public.trainings;
DROP TRIGGER trg_9_users_credit_close ON public.users;
DROP TRIGGER trg_9_workgroups_credit_close ON public.workgroups;
DROP TRIGGER trg_9_users_credit_reparent ON public.users;
DROP TRIGGER trg_9_workgroups_credit_reparent ON public.workgroups;
DO $$
DECLARE r RECORD;
BEGIN
  FOR r IN SELECT p.oid::regprocedure AS sig FROM pg_proc p
            WHERE p.pronamespace = 'public'::regnamespace
              AND (p.proname LIKE '\_credit\_%'
                   OR (p.proname LIKE 'credit\_%' AND p.proname <> 'credit_ledger_immutable')
                   OR p.proname IN ('stop_training','record_training_refusal','trainings_guard_credit_columns',
                                    'trainings_credit_settle','trainings_credit_settle_delete','users_credit_close',
                                    'workgroups_credit_close')) LOOP
    EXECUTE 'DROP FUNCTION ' || r.sig;
  END LOOP;
END $$;

-- 3. the four re-created signatures, then the pre-052 bodies verbatim ------------
DROP FUNCTION public.start_training_safe(UUID, TEXT, TEXT, TEXT, JSONB, INT, UUID, BOOLEAN, UUID, INT);
DROP FUNCTION public.start_continuation_safe(UUID, INT, TEXT, INT);
DROP FUNCTION public.cancel_request_training(INT, UUID, TEXT);
DROP FUNCTION public.finish_training(INT, INT, TEXT, TEXT, TEXT, TEXT, INT, TIMESTAMPTZ, TEXT);

-- start_training_safe: verbatim from migrations/20261020130000_049_managed_training.sql
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

-- start_continuation_safe: verbatim from migrations/20261021120000_050_training_continuation.sql
CREATE OR REPLACE FUNCTION public.start_continuation_safe(p_user_id UUID, p_parent_id INT, p_note TEXT)
RETURNS TABLE(o_training_id INT, o_remaining INT)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  v_p         public.trainings%ROWTYPE;
  v_child     INT;
  v_remaining INT;
BEGIN
  SELECT * INTO v_p FROM public.trainings t WHERE t.id = p_parent_id AND t.user_id = p_user_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'training % not found', p_parent_id USING ERRCODE = 'P0002';
  END IF;
  IF NOT v_p.managed OR v_p.status <> 'succeeded' OR v_p.capped_step IS NULL
     OR v_p.capped_step >= v_p.total_steps OR v_p.continuable_until IS NULL THEN
    RAISE EXCEPTION 'training % is not continuable', p_parent_id USING ERRCODE = 'P0052';
  END IF;
  IF v_p.continuable_until <= now() THEN
    RAISE EXCEPTION 'training % can no longer be continued', p_parent_id USING ERRCODE = 'P0053';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.training_kept_checkpoints k
                  WHERE k.training_id = p_parent_id AND k.state = 'kept' AND k.step = v_p.capped_step) THEN
    RAISE EXCEPTION 'training % has no kept checkpoint', p_parent_id USING ERRCODE = 'P0052';
  END IF;
  IF EXISTS (SELECT 1 FROM public.trainings c
              WHERE c.continued_from = p_parent_id AND c.status NOT IN ('failed','canceled')) THEN
    RAISE EXCEPTION 'training % is already being continued', p_parent_id USING ERRCODE = 'P0054';
  END IF;
  SELECT s.training_id, s.remaining INTO v_child, v_remaining
    FROM public.start_training_safe(p_user_id, v_p.dataset_name, v_p.model_name, v_p.model_type,
                                    v_p.training_params, v_p.total_steps, NULL, true) AS s;
  UPDATE public.trainings c
     SET continued_from = p_parent_id, current_step = v_p.capped_step, status_note = p_note,
         dataset_fps = v_p.dataset_fps
   WHERE c.id = v_child;
  RETURN QUERY SELECT v_child, v_remaining;
END;
$$;
REVOKE ALL ON FUNCTION public.start_continuation_safe(UUID, INT, TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.start_continuation_safe(UUID, INT, TEXT) TO service_role;

-- cancel_request_training: verbatim from migrations/20261021120000_050_training_continuation.sql
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
    IF v_a.state = 'ended' AND v_a.end_reason IN ('succeeded','capped','wall_capped') THEN
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
REVOKE ALL ON FUNCTION public.cancel_request_training(INT, UUID) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.cancel_request_training(INT, UUID) TO service_role;

-- claim_training_publish: verbatim from migrations/20261021120000_050_training_continuation.sql
CREATE OR REPLACE FUNCTION public.claim_training_publish(p_training_id INT, p_attempt INT, p_lease_s INT, p_owner TEXT)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE v_rows INT;
BEGIN
  UPDATE public.training_attempts a
     SET publish_claimed_at = now(), publish_owner = p_owner,
         publish_first_at = COALESCE(a.publish_first_at, now())
    FROM public.trainings t
   WHERE a.training_id = p_training_id AND a.attempt_no = p_attempt
     AND a.state = 'ended' AND a.end_reason IN ('succeeded','capped','wall_capped')
     AND t.id = a.training_id AND t.current_attempt = p_attempt AND t.status = 'running'
     AND (a.publish_claimed_at IS NULL OR a.publish_owner = p_owner
          OR a.publish_claimed_at < now() - make_interval(secs => p_lease_s));
  GET DIAGNOSTICS v_rows = ROW_COUNT;
  RETURN v_rows = 1;
END;
$$;
REVOKE ALL ON FUNCTION public.claim_training_publish(INT, INT, INT, TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_training_publish(INT, INT, INT, TEXT) TO service_role;

-- cap_training_attempt: verbatim from migrations/20261021120000_050_training_continuation.sql
CREATE OR REPLACE FUNCTION public.cap_training_attempt(
  p_training_id INT, p_attempt INT, p_step INT, p_reason TEXT, p_note TEXT, p_train_seconds INT)
RETURNS VOID LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE
  v_rows  INT;
  v_prev  TEXT;
  v_total INT;
BEGIN
  IF p_reason IS NULL OR p_reason NOT IN ('capped','wall_capped') OR p_step IS NULL OR p_step < 1 THEN
    RAISE EXCEPTION 'invalid cap' USING ERRCODE = '22023';
  END IF;
  SELECT t.total_steps INTO v_total FROM public.trainings t WHERE t.id = p_training_id FOR UPDATE;
  IF v_total IS NOT NULL AND p_step >= v_total THEN
    RAISE EXCEPTION 'cap step % is not below the total %', p_step, v_total USING ERRCODE = '22023';
  END IF;
  SELECT a.state INTO v_prev FROM public.training_attempts a
   WHERE a.training_id = p_training_id AND a.attempt_no = p_attempt;
  UPDATE public.training_attempts a
     SET state = 'ended', ended_at = now(), end_reason = p_reason
    FROM public.trainings t
   WHERE a.training_id = p_training_id AND a.attempt_no = p_attempt
     AND a.state IN ('placing','starting','running','lost')
     AND t.id = a.training_id AND t.managed AND t.current_attempt = p_attempt
     AND t.status IN ('queued','running');
  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows = 0 THEN
    RAISE EXCEPTION 'cap conflict for training %', p_training_id USING ERRCODE = 'P0049';
  END IF;
  UPDATE public.trainings
     SET status = 'running', worker_token = NULL, status_note = p_note, capped_step = p_step,
         train_seconds_used = train_seconds_used
           + CASE WHEN v_prev IN ('lost','ended') THEN 0 ELSE GREATEST(COALESCE(p_train_seconds, 0), 0) END
   WHERE id = p_training_id;
END;
$$;
REVOKE ALL ON FUNCTION public.cap_training_attempt(INT, INT, INT, TEXT, TEXT, INT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.cap_training_attempt(INT, INT, INT, TEXT, TEXT, INT) TO service_role;

-- requeue_training: verbatim from migrations/20261021120000_050_training_continuation.sql
CREATE OR REPLACE FUNCTION public.requeue_training(
  p_training_id INT, p_attempt INT, p_reason TEXT, p_note TEXT, p_resume_step INT, p_train_seconds INT)
RETURNS VOID LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE
  v_rows INT;
  v_hist JSONB;
  v_saved INT;
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
  -- E45/E50: from the attempt's first running write to the created_at of its newest
  -- own checkpoint whose state is ready; 0 without one (or without a running write).
  SELECT GREATEST(COALESCE(floor(EXTRACT(EPOCH FROM (
           (SELECT max(c.created_at) FROM public.training_checkpoints c
             WHERE c.training_id = a.training_id AND c.attempt_no = a.attempt_no AND c.state = 'ready')
           - a.first_heartbeat_at)))::int, 0), 0)
    INTO v_saved
    FROM public.training_attempts a
   WHERE a.training_id = p_training_id AND a.attempt_no = p_attempt;
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
         train_seconds_used = train_seconds_used
           + LEAST(GREATEST(COALESCE(p_train_seconds, 0), 0), COALESCE(v_saved, 0))
   WHERE id = p_training_id;
END;
$$;
REVOKE ALL ON FUNCTION public.requeue_training(INT, INT, TEXT, TEXT, INT, INT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.requeue_training(INT, INT, TEXT, TEXT, INT, INT) TO service_role;

-- finish_training: verbatim from migrations/20261021120000_050_training_continuation.sql
CREATE OR REPLACE FUNCTION public.finish_training(
  p_training_id INT, p_attempt INT, p_status TEXT, p_message TEXT, p_log_url TEXT,
  p_note TEXT DEFAULT NULL, p_final_step INT DEFAULT NULL, p_continuable_until TIMESTAMPTZ DEFAULT NULL)
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
     SET status = p_status, terminated_at = now(), worker_token = NULL, status_note = p_note,
         error_message = CASE WHEN p_status = 'failed' THEN p_message ELSE error_message END,
         log_url = COALESCE(p_log_url, log_url),
         current_step = CASE WHEN p_status = 'succeeded' THEN COALESCE(p_final_step, total_steps) ELSE current_step END,
         continuable_until = CASE WHEN p_status = 'succeeded' THEN p_continuable_until ELSE continuable_until END,
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
REVOKE ALL ON FUNCTION public.finish_training(INT, INT, TEXT, TEXT, TEXT, TEXT, INT, TIMESTAMPTZ) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.finish_training(INT, INT, TEXT, TEXT, TEXT, TEXT, INT, TIMESTAMPTZ) TO service_role;

-- complete_cancel_training: verbatim from migrations/20261020130000_049_managed_training.sql
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
REVOKE ALL ON FUNCTION public.complete_cancel_training(INT, TEXT, TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.complete_cancel_training(INT, TEXT, TEXT) TO service_role;

-- get_remaining_credits: verbatim from migrations/00000000000000_baseline.sql
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
REVOKE ALL ON FUNCTION public.get_remaining_credits(UUID) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_remaining_credits(UUID) TO service_role;

-- get_teacher_credit_summary: verbatim from migrations/00000000000000_baseline.sql
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
REVOKE ALL ON FUNCTION public.get_teacher_credit_summary(UUID) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_teacher_credit_summary(UUID) TO service_role;

-- adjust_student_credits: verbatim from migrations/00000000000000_baseline.sql
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
REVOKE ALL ON FUNCTION public.adjust_student_credits(UUID, UUID, INTEGER) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.adjust_student_credits(UUID, UUID, INTEGER) TO service_role;

-- adjust_workgroup_credits: verbatim from migrations/00000000000000_baseline.sql
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
REVOKE ALL ON FUNCTION public.adjust_workgroup_credits(UUID, UUID, INTEGER) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.adjust_workgroup_credits(UUID, UUID, INTEGER) TO service_role;

-- 3b. update_training_progress: 049's body VERBATIM (052 changed one line of it, the
-- stall clock), with 049's grants: PUBLIC nothing, anon and authenticated EXECUTE.
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

-- 4. the 051 defaults and table privileges -------------------------------------
ALTER TABLE public.trainings ALTER COLUMN credit_scheme SET DEFAULT 'derived';
GRANT INSERT, TRUNCATE, TRIGGER, REFERENCES ON public.trainings TO service_role;

-- 6. self-check -------------------------------------------------------------------
DO $$
DECLARE n INT; f TEXT; r TEXT; t TEXT; p TEXT;
BEGIN
  FOREACH f IN ARRAY ARRAY['start_training_safe','start_continuation_safe','finish_training',
                           'cancel_request_training','get_remaining_credits'] LOOP
    SELECT count(*) INTO n FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND proname = f;
    IF n <> 1 THEN RAISE EXCEPTION '052 rollback: % has % overloads', f, n; END IF;
  END LOOP;
  IF NOT has_table_privilege('service_role', 'public.trainings', 'INSERT') THEN
    RAISE EXCEPTION '052 rollback: service_role cannot INSERT trainings';
  END IF;
  IF (SELECT count(*) FROM pg_trigger
       WHERE tgrelid = 'public.credit_ledger'::regclass
         AND tgname IN ('credit_ledger_no_change','credit_ledger_no_truncate')) <> 2 THEN
    RAISE EXCEPTION '052 rollback: the ledger lost its immutability triggers';
  END IF;
  FOREACH t IN ARRAY ARRAY['public.credit_accounts','public.credit_ledger','public.credit_reasons',
                           'public.training_start_refusals'] LOOP
    FOREACH r IN ARRAY ARRAY['anon','authenticated'] LOOP
      FOREACH p IN ARRAY ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER'] LOOP
        IF has_table_privilege(r, t, p) THEN RAISE EXCEPTION '052 rollback: % holds % on %', r, p, t; END IF;
      END LOOP;
    END LOOP;
  END LOOP;
  IF EXISTS (SELECT 1 FROM pg_proc WHERE pronamespace = 'public'::regnamespace
              AND (proname LIKE '\_credit\_%' OR (proname LIKE 'credit\_%' AND proname <> 'credit_ledger_immutable')
                   OR proname IN ('stop_training','record_training_refusal','trainings_guard_credit_columns',
                                  'trainings_credit_settle','trainings_credit_settle_delete','users_credit_close',
                                  'workgroups_credit_close'))) THEN
    RAISE EXCEPTION '052 rollback: a 052 function survived';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_trigger WHERE NOT tgisinternal AND tgname IN
             ('trg_0_trainings_credit_guard','trg_9_trainings_credit_settle','trg_9_trainings_credit_settle_delete',
              'trg_9_users_credit_close','trg_9_workgroups_credit_close','trg_9_users_credit_reparent',
              'trg_9_workgroups_credit_reparent')) THEN
    RAISE EXCEPTION '052 rollback: a 052 trigger survived';
  END IF;
  FOREACH f IN ARRAY ARRAY['public.start_training_safe(uuid,text,text,text,jsonb,integer,uuid,boolean)',
                           'public.start_continuation_safe(uuid,integer,text)',
                           'public.cancel_request_training(integer,uuid)',
                           'public.finish_training(integer,integer,text,text,text,text,integer,timestamp with time zone)',
                           'public.complete_cancel_training(integer,text,text)',
                           'public.claim_training_publish(integer,integer,integer,text)',
                           'public.cap_training_attempt(integer,integer,integer,text,text,integer)',
                           'public.requeue_training(integer,integer,text,text,integer,integer)',
                           'public.get_remaining_credits(uuid)', 'public.get_teacher_credit_summary(uuid)',
                           'public.adjust_student_credits(uuid,uuid,integer)',
                           'public.adjust_workgroup_credits(uuid,uuid,integer)'] LOOP
    IF has_function_privilege('anon', f, 'EXECUTE') OR has_function_privilege('authenticated', f, 'EXECUTE') THEN
      RAISE EXCEPTION '052 rollback: a request role executes %', f;
    END IF;
    IF NOT has_function_privilege('service_role', f, 'EXECUTE') THEN
      RAISE EXCEPTION '052 rollback: service_role cannot execute %', f;
    END IF;
  END LOOP;
  -- update_training_progress is 049's body byte for byte (md5 of 049's prosrc) and the worker's roles execute it
  SELECT count(*) INTO n FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND proname = 'update_training_progress';
  IF n <> 1 THEN RAISE EXCEPTION '052 rollback: update_training_progress has % overloads', n; END IF;
  IF (SELECT md5(prosrc) FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND proname = 'update_training_progress')
     <> '5b9b815042f296fafd578b34faf1cbde' THEN
    RAISE EXCEPTION '052 rollback: update_training_progress is not 049''s body';
  END IF;
  FOREACH r IN ARRAY ARRAY['anon','authenticated'] LOOP
    IF NOT has_function_privilege(r, 'public.update_training_progress(integer,uuid,text,integer,integer,real,text,text)', 'EXECUTE') THEN
      RAISE EXCEPTION '052 rollback: % lost EXECUTE on update_training_progress', r;
    END IF;
  END LOOP;
  IF has_column_privilege('authenticated', 'public.trainings', 'worker_token', 'SELECT')
     OR has_column_privilege('anon', 'public.trainings', 'worker_token', 'SELECT') THEN
    RAISE EXCEPTION '052 rollback: a request role can read trainings.worker_token';
  END IF;
  RAISE NOTICE '052 rollback self-check passed';
END $$;

COMMIT;

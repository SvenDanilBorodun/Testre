-- 050 — managed training: a capped training publishes and the student continues it
-- (v2.33.0 spec rev3 §6; owner decisions E32–E37, E43–E47, E50). A training that
-- hits its 3.5 h training-time cap or its 12 h wall with a ready checkpoint now
-- ends `succeeded` with that checkpoint published, and the student who started it
-- may continue toward the original step target with a second credit, resuming from
-- a kept copy of the checkpoint (7 days).
--   * public.trainings gains three columns (capped_step, continuable_until,
--     continued_from → trainings(id) ON DELETE SET NULL), each granted to
--     anon/authenticated by NAME (047 took the table SELECT away).
--   * public.training_kept_checkpoints (one row per continuable parent, service
--     role only: RLS on, every privilege revoked from PUBLIC/anon/authenticated,
--     not in any publication) holds the kept copy's step, manifest, state
--     (kept|refused|deleted), expiry and the crash/`train_config` complaint count.
--   * cap_training_attempt (SECURITY INVOKER, service role only): ends the current
--     attempt `capped`|`wall_capped` and leaves the row `running` for the publish,
--     charging the attempt's seconds once; refuses a step ≥ total (22023) and a
--     lost race (P0049).
--   * requeue_training keeps 049's signature but, owner E45/E50, charges a lost
--     attempt only up to the created_at of its newest OWN ready checkpoint
--     (LEAST(p_train_seconds, that span), 0 without one), for every caller.
--   * finish_training gains p_note, p_final_step and p_continuable_until (all
--     DEFAULT NULL); the 5-argument overload is dropped so exactly one exists, and
--     a v2.32.0 API's five NAMED arguments still resolve through PostgREST.
--   * claim_training_publish and cancel_request_training accept a capped attempt
--     (`succeeded`|`capped`|`wall_capped`); a capped publish in flight refuses
--     cancel with P0051.
--   * start_continuation_safe (SECURITY DEFINER, SET search_path = public, service
--     role only, OUT names o_training_id/o_remaining): takes the JWT's user id,
--     selects the parent by user_id = p_user_id (owner E46), and CREATES a managed
--     child by CALLING start_training_safe (the credit and workgroup-pool rules are
--     reused, never copied). P0002 not found, P0052 not continuable, P0053 expired
--     (checked before the kept row), P0054 already continuing, P0003 no credit.
--   * a self-check (the grants, RLS, no publication, exactly one finish_training
--     and one start_training_safe).
--
-- ORDER: roll the cloud API back FIRST. A WP-1-or-older API never recovers a
-- `capped` publish (its sweep recovers only end_reason='succeeded'), so wait until
-- no managed row is queued/running/cancel_requested before an API rollback; then
-- rollback/20261021120000_050_training_continuation_rollback.sql (it refuses while
-- a managed row is active and restores 049's four bodies verbatim), then 049's if
-- needed. Kept R2 objects are orphaned by the rollback; the bucket's 9-day keep/
-- rule deletes them. 047 stays: the self-check keeps worker_token unreadable.
--
-- Proven on a local supabase start stack (Postgres 17.6.1.106, PostgREST
-- v14.10, a SCRATCH copy of supabase/ with the baseline's runpod_job_id RENAME
-- line guarded; 047, 049, 050 applied with psql -v ON_ERROR_STOP=1 as postgres):
-- 050 applies, its self-check passes; 050_training_continuation_assertions.sql
-- prints 30 PASS, 0 FAIL; run against the 049 file AFTER 050 it prints 62 PASS,
-- 6 FAIL (T5a, T5d, T29b, T35b, T36a, T36b — every one a requeue of an attempt
-- without an own ready checkpoint, now charged 0 by E50) and 1 ERROR (T1: the
-- 5-argument finish_training no longer exists), 69 PASS at 049; through PostgREST
-- a request role's start_continuation_safe answers 401/42501 and a read of
-- training_kept_checkpoints 42501, and the schema probe's five calls answer
-- P0049/P0002. The rollback refuses with an active row, applies after draining,
-- and 050 re-applies.
BEGIN;

-- 1. trainings columns -------------------------------------------------------
ALTER TABLE public.trainings
  ADD COLUMN IF NOT EXISTS capped_step       INT CHECK (capped_step >= 1),
  ADD COLUMN IF NOT EXISTS continuable_until TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS continued_from    INT REFERENCES public.trainings(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS trainings_continued_from_idx
  ON public.trainings(continued_from) WHERE continued_from IS NOT NULL;

GRANT SELECT (capped_step, continuable_until, continued_from) ON public.trainings TO anon, authenticated;

-- 2. training_kept_checkpoints ------------------------------------------------
CREATE TABLE IF NOT EXISTS public.training_kept_checkpoints (
  training_id    INT  PRIMARY KEY REFERENCES public.trainings(id) ON DELETE CASCADE,
  step           INT  NOT NULL CHECK (step >= 1),
  manifest       JSONB NOT NULL,
  state          TEXT NOT NULL DEFAULT 'kept' CHECK (state IN ('kept','refused','deleted')),
  kept_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at     TIMESTAMPTZ NOT NULL,
  refused_reason TEXT,
  bad_attempts   TEXT[] NOT NULL DEFAULT '{}'
);
ALTER TABLE public.training_kept_checkpoints ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.training_kept_checkpoints FROM PUBLIC, anon, authenticated;

-- 3. cap_training_attempt ------------------------------------------------------
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

-- 4. requeue_training: a lost attempt is charged up to its newest own saved checkpoint (E45/E50)
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

-- 5. finish_training: + p_note, p_final_step, p_continuable_until (all DEFAULT NULL)
DROP FUNCTION IF EXISTS public.finish_training(INT, INT, TEXT, TEXT, TEXT);
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

-- 6. claim_training_publish: a capped attempt publishes too
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

-- 7. cancel_request_training: a capped publish in flight refuses cancel (P0051)
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

-- 8. start_continuation_safe: the credit and pool rules ARE start_training_safe's
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

-- 9. grants ----------------------------------------------------------------------
REVOKE ALL ON FUNCTION public.cap_training_attempt(INT, INT, INT, TEXT, TEXT, INT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.finish_training(INT, INT, TEXT, TEXT, TEXT, TEXT, INT, TIMESTAMPTZ) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.start_continuation_safe(UUID, INT, TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.cap_training_attempt(INT, INT, INT, TEXT, TEXT, INT) TO service_role;
GRANT EXECUTE ON FUNCTION public.finish_training(INT, INT, TEXT, TEXT, TEXT, TEXT, INT, TIMESTAMPTZ) TO service_role;
GRANT EXECUTE ON FUNCTION public.start_continuation_safe(UUID, INT, TEXT) TO service_role;

-- 10. self-check -------------------------------------------------------------------
DO $$
DECLARE v_role TEXT; v_fn TEXT; v_priv TEXT; v_col TEXT;
BEGIN
  FOREACH v_role IN ARRAY ARRAY['anon','authenticated'] LOOP
    FOREACH v_priv IN ARRAY ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER'] LOOP
      IF has_table_privilege(v_role, 'public.training_kept_checkpoints', v_priv) THEN
        RAISE EXCEPTION '050: % still holds % on training_kept_checkpoints', v_role, v_priv;
      END IF;
    END LOOP;
    FOREACH v_fn IN ARRAY ARRAY[
      'public.cap_training_attempt(integer,integer,integer,text,text,integer)',
      'public.finish_training(integer,integer,text,text,text,text,integer,timestamp with time zone)',
      'public.start_continuation_safe(uuid,integer,text)',
      'public.requeue_training(integer,integer,text,text,integer,integer)',
      'public.claim_training_publish(integer,integer,integer,text)',
      'public.cancel_request_training(integer,uuid)'] LOOP
      IF has_function_privilege(v_role, v_fn, 'EXECUTE') THEN
        RAISE EXCEPTION '050: % can execute %', v_role, v_fn;
      END IF;
    END LOOP;
    FOREACH v_col IN ARRAY ARRAY['capped_step','continuable_until','continued_from'] LOOP
      IF NOT has_column_privilege(v_role, 'public.trainings', v_col, 'SELECT') THEN
        RAISE EXCEPTION '050: % cannot read trainings.%', v_role, v_col;
      END IF;
    END LOOP;
    IF has_column_privilege(v_role, 'public.trainings', 'worker_token', 'SELECT') THEN
      RAISE EXCEPTION '050: % can read trainings.worker_token', v_role;
    END IF;
  END LOOP;
  FOREACH v_fn IN ARRAY ARRAY[
    'public.cap_training_attempt(integer,integer,integer,text,text,integer)',
    'public.finish_training(integer,integer,text,text,text,text,integer,timestamp with time zone)',
    'public.start_continuation_safe(uuid,integer,text)',
    'public.requeue_training(integer,integer,text,text,integer,integer)',
    'public.claim_training_publish(integer,integer,integer,text)',
    'public.cancel_request_training(integer,uuid)'] LOOP
    IF NOT has_function_privilege('service_role', v_fn, 'EXECUTE') THEN
      RAISE EXCEPTION '050: service_role cannot execute %', v_fn;
    END IF;
  END LOOP;
  IF EXISTS (SELECT 1 FROM pg_publication_tables WHERE tablename = 'training_kept_checkpoints') THEN
    RAISE EXCEPTION '050: training_kept_checkpoints is in a publication';
  END IF;
  IF (SELECT count(*) FROM pg_proc WHERE proname = 'finish_training' AND pronamespace = 'public'::regnamespace) <> 1 THEN
    RAISE EXCEPTION '050: finish_training must have exactly one overload';
  END IF;
  IF (SELECT count(*) FROM pg_proc WHERE proname = 'start_training_safe' AND pronamespace = 'public'::regnamespace) <> 1 THEN
    RAISE EXCEPTION '050: start_training_safe must have exactly one overload';
  END IF;
END $$;

COMMIT;

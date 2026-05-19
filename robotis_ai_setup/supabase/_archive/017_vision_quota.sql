-- 017_vision_quota.sql
--
-- Roboter Studio Phase-3: per-user cloud-vision quota.
-- The cloud_training_api `/vision/detect` endpoint forwards calls to
-- the Modal OWLv2 app. Each call costs ~$0.0001 in T4 compute but a
-- runaway workflow loop could rack up real money; this migration
-- adds two columns on ``users`` so the endpoint can short-circuit
-- after N successful calls per term.
--
-- ``vision_quota_per_term`` NULL means "unbounded"; the endpoint
-- treats NULL as a no-op so adopters with no policy preference are
-- gated only by the in-process rate limiter (5/60s/user).
-- Audit §D1 / §J7 — the endpoint reads these columns but they
-- didn't exist; the swallow-exception clause silently bypassed the
-- quota. Adding the columns wires the check up.

BEGIN;

ALTER TABLE public.users
  ADD COLUMN IF NOT EXISTS vision_quota_per_term INTEGER,
  ADD COLUMN IF NOT EXISTS vision_used_per_term INTEGER NOT NULL DEFAULT 0;

-- Floor the used counter so a future bug-introduced decrement RPC can't
-- write negative values. Audit round-3 §AQ.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.check_constraints
    WHERE constraint_schema = 'public'
      AND constraint_name = 'users_vision_used_per_term_nonneg'
  ) THEN
    ALTER TABLE public.users
      ADD CONSTRAINT users_vision_used_per_term_nonneg
      CHECK (vision_used_per_term >= 0);
  END IF;
END $$;

COMMENT ON COLUMN public.users.vision_quota_per_term IS
  'Maximum cloud-vision detect calls per term. NULL = unbounded.';
COMMENT ON COLUMN public.users.vision_used_per_term IS
  'Counter incremented by /vision/detect on every successful call.';

-- Atomic consume: returns (allowed, remaining). The UPDATE only
-- fires when there's room left so two concurrent calls can't both
-- pass the check and both increment (audit §D2). NULL quota means
-- unbounded → always allowed.
CREATE OR REPLACE FUNCTION public.consume_vision_quota(p_user_id UUID)
RETURNS TABLE(allowed BOOLEAN, remaining INTEGER)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_quota INTEGER;
  v_new_used INTEGER;
BEGIN
  SELECT vision_quota_per_term INTO v_quota
  FROM public.users
  WHERE id = p_user_id;
  IF NOT FOUND THEN
    RETURN QUERY SELECT FALSE, 0;
    RETURN;
  END IF;
  IF v_quota IS NULL THEN
    -- Audit F48: skip the UPDATE for NULL-quota users so the counter
    -- doesn't grow unbounded. If an admin later flips them to a
    -- bounded quota, the student would otherwise be INSTANTLY
    -- locked out (used > new_quota from N years of telemetry). The
    -- "still count usage for telemetry" intent was a footgun.
    RETURN QUERY SELECT TRUE, NULL::INTEGER;
    RETURN;
  END IF;
  UPDATE public.users
  SET vision_used_per_term = vision_used_per_term + 1
  WHERE id = p_user_id
    AND vision_used_per_term < v_quota
  RETURNING vision_used_per_term INTO v_new_used;
  IF v_new_used IS NULL THEN
    RETURN QUERY SELECT FALSE, 0;
  ELSE
    RETURN QUERY SELECT TRUE, GREATEST(v_quota - v_new_used, 0);
  END IF;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.consume_vision_quota(UUID) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.consume_vision_quota(UUID) FROM anon;
REVOKE EXECUTE ON FUNCTION public.consume_vision_quota(UUID) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.consume_vision_quota(UUID) TO service_role;

-- Atomic refund: decrement vision_used_per_term by 1, never below 0.
-- Called by the cloud API when Modal returns a transient error
-- (502/504/timeout) so a flaky cold start doesn't burn the student's
-- term budget. Returns the new used count for telemetry.
-- Audit round-3 §A — refund-on-failure pattern.
CREATE OR REPLACE FUNCTION public.refund_vision_quota(p_user_id UUID)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_new_used INTEGER;
BEGIN
  UPDATE public.users
  SET vision_used_per_term = GREATEST(vision_used_per_term - 1, 0)
  WHERE id = p_user_id
  RETURNING vision_used_per_term INTO v_new_used;
  RETURN COALESCE(v_new_used, 0);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.refund_vision_quota(UUID) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.refund_vision_quota(UUID) FROM anon;
REVOKE EXECUTE ON FUNCTION public.refund_vision_quota(UUID) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.refund_vision_quota(UUID) TO service_role;

-- Convenience RPC that resets every student's used counter at term
-- start. Run from the admin dashboard / cron. Service-role only.
CREATE OR REPLACE FUNCTION public.reset_vision_quota_used()
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  affected INTEGER;
BEGIN
  UPDATE public.users
  SET vision_used_per_term = 0
  WHERE vision_used_per_term > 0;
  GET DIAGNOSTICS affected = ROW_COUNT;
  RETURN affected;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.reset_vision_quota_used() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.reset_vision_quota_used() FROM anon;
REVOKE EXECUTE ON FUNCTION public.reset_vision_quota_used() FROM authenticated;
GRANT EXECUTE ON FUNCTION public.reset_vision_quota_used() TO service_role;

COMMIT;

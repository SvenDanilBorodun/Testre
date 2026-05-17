-- 020_jetson_v2.sql
--
-- Classroom Jetson Orin Nano v2.3.0 follow-up RPCs. Pairs with the
-- v2.3.0 push of the Cloud API + React teacher dashboard:
--   * agent_release_jetson  → agent-authenticated lock release for
--                             local claim-transition failures (the agent
--                             observes a docker-compose / healthcheck
--                             error mid-claim and proactively frees the
--                             server-side lock instead of waiting 5 min
--                             for the sweeper).
--   * regenerate_pairing_code → teacher generates a fresh 6-digit code
--                             without SSHing back to the Jetson to re-run
--                             setup.sh. Cryptographic randomness lives in
--                             the Cloud API (Python's `secrets`); the RPC
--                             receives the pre-generated code and writes
--                             it atomically with a refreshed 30-min
--                             expiry.
--   * unpair_jetson          → teacher unbinds the Jetson from the
--                             classroom (sets classroom_id=NULL),
--                             also force-releases any active owner so
--                             the device can be paired to another
--                             classroom or decommissioned cleanly.
--
-- No new error codes — re-uses migration 019's P0001 (token mismatch /
-- not found), P0002 (jetson/classroom not found), P0011 (classroom does
-- not belong to this teacher).
--
-- Service-role bypasses RLS — Python ``_assert_*_owned`` helpers in
-- routes/jetson.py remain the actual authorisation layer.
--
-- Idempotent — re-runnable.

BEGIN;

-- ---------------------------------------------------------------------------
-- agent_release_jetson: agent-authenticated lock release.
--
-- Used when the agent's claim transition fails locally (docker compose
-- up timeout, healthcheck flap, OOM on first model download) and the
-- agent decides to abandon the in-flight session. Without this RPC the
-- agent has to wait 5 minutes for the sweeper, during which the student
-- sees "Jetson belegt von <ihrer Name>" with no way to retry. After
-- this RPC the agent can release immediately and the student's next
-- claim succeeds in seconds.
--
-- Auth: agent_token verification (same shape as agent_heartbeat_jetson
-- from migration 019). A student JWT cannot call this — only the
-- physical Jetson holding the matching agent_token.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.agent_release_jetson(
    p_jetson_id UUID,
    p_agent_token UUID
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_rows INTEGER;
BEGIN
    UPDATE public.jetsons
       SET current_owner_user_id      = NULL,
           current_owner_heartbeat_at = NULL,
           claimed_at                 = NULL,
           updated_at                 = NOW()
     WHERE id = p_jetson_id
       AND agent_token = p_agent_token;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    IF v_rows = 0 THEN
        RAISE EXCEPTION 'Agent-Token ungültig oder Jetson nicht gefunden' USING ERRCODE = 'P0001';
    END IF;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.agent_release_jetson(UUID, UUID) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.agent_release_jetson(UUID, UUID) FROM anon;
REVOKE EXECUTE ON FUNCTION public.agent_release_jetson(UUID, UUID) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.agent_release_jetson(UUID, UUID) TO service_role;


-- ---------------------------------------------------------------------------
-- regenerate_pairing_code: teacher generates a fresh pairing code on a
-- Jetson already bound to their classroom (replaces a code that
-- expired, or one the teacher wants to rotate).
--
-- Cryptographic randomness lives in the Cloud API (Python's
-- `secrets.randbelow`), not in Postgres — `random()` is not
-- cryptographically secure and `gen_random_bytes()` is an extension
-- that isn't guaranteed to be loaded. The RPC receives the
-- pre-generated 6-digit code and refreshes the 30-min expiry.
--
-- Asserts the caller owns the target classroom (P0011). Returns the
-- jetson_id so the Cloud API can echo it back to the teacher's UI.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.regenerate_pairing_code(
    p_jetson_id UUID,
    p_teacher_id UUID,
    p_new_code TEXT,
    p_expires_at TIMESTAMPTZ
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_classroom_id UUID;
    v_classroom_teacher UUID;
BEGIN
    -- Lock the row to prevent two teachers (same classroom, e.g. an
    -- admin acting as a teacher) from racing on regenerate.
    SELECT classroom_id INTO v_classroom_id
      FROM public.jetsons
     WHERE id = p_jetson_id
       FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Jetson nicht gefunden' USING ERRCODE = 'P0002';
    END IF;

    IF v_classroom_id IS NULL THEN
        RAISE EXCEPTION 'Jetson ist nicht gepaart' USING ERRCODE = 'P0002';
    END IF;

    SELECT teacher_id INTO v_classroom_teacher
      FROM public.classrooms
     WHERE id = v_classroom_id;

    IF v_classroom_teacher <> p_teacher_id THEN
        RAISE EXCEPTION 'Jetson gehört nicht zu diesem Lehrer' USING ERRCODE = 'P0011';
    END IF;

    UPDATE public.jetsons
       SET pairing_code            = p_new_code,
           pairing_code_expires_at = p_expires_at,
           updated_at              = NOW()
     WHERE id = p_jetson_id;

    RETURN p_jetson_id;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.regenerate_pairing_code(UUID, UUID, TEXT, TIMESTAMPTZ) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.regenerate_pairing_code(UUID, UUID, TEXT, TIMESTAMPTZ) FROM anon;
REVOKE EXECUTE ON FUNCTION public.regenerate_pairing_code(UUID, UUID, TEXT, TIMESTAMPTZ) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.regenerate_pairing_code(UUID, UUID, TEXT, TIMESTAMPTZ) TO service_role;


-- ---------------------------------------------------------------------------
-- unpair_jetson: teacher unbinds the Jetson from the classroom. Used
-- when the teacher wants to:
--   * move the device to a different classroom mid-term,
--   * decommission an old Jetson,
--   * recover from accidentally pairing the wrong physical device.
--
-- Side effects in a single transaction:
--   * classroom_id              → NULL
--   * current_owner_user_id     → NULL  (force-releases any active session)
--   * current_owner_heartbeat_at → NULL
--   * claimed_at                → NULL
--   * mdns_name                 → NULL  (the next pair will set a fresh one)
--
-- Asserts the caller owns the target classroom (P0011) before the
-- write. The agent_token is preserved so the same physical device can
-- be re-paired to another classroom without SSH access to re-run
-- setup.sh.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.unpair_jetson(
    p_jetson_id UUID,
    p_teacher_id UUID
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_classroom_id UUID;
    v_classroom_teacher UUID;
BEGIN
    SELECT classroom_id INTO v_classroom_id
      FROM public.jetsons
     WHERE id = p_jetson_id
       FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Jetson nicht gefunden' USING ERRCODE = 'P0002';
    END IF;

    IF v_classroom_id IS NULL THEN
        RAISE EXCEPTION 'Jetson ist nicht gepaart' USING ERRCODE = 'P0002';
    END IF;

    SELECT teacher_id INTO v_classroom_teacher
      FROM public.classrooms
     WHERE id = v_classroom_id;

    IF v_classroom_teacher <> p_teacher_id THEN
        RAISE EXCEPTION 'Jetson gehört nicht zu diesem Lehrer' USING ERRCODE = 'P0011';
    END IF;

    UPDATE public.jetsons
       SET classroom_id               = NULL,
           current_owner_user_id      = NULL,
           current_owner_heartbeat_at = NULL,
           claimed_at                 = NULL,
           mdns_name                  = NULL,
           updated_at                 = NOW()
     WHERE id = p_jetson_id;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.unpair_jetson(UUID, UUID) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.unpair_jetson(UUID, UUID) FROM anon;
REVOKE EXECUTE ON FUNCTION public.unpair_jetson(UUID, UUID) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.unpair_jetson(UUID, UUID) TO service_role;


COMMIT;

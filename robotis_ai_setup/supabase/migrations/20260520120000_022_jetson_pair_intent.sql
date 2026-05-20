-- ============================================================================
-- 20260520120000_022_jetson_pair_intent.sql
-- ----------------------------------------------------------------------------
-- Closes the Jetson pairing-code race IDOR identified in the 2026-05 audit.
--
-- Problem
--   The pre-022 /jetson/pair flow only verifies the calling teacher owns
--   the classroom they are pairing into. Two teachers (T1, T2) sharing the
--   same school LAN both see the freshly-printed 6-digit code on the
--   Jetson's stdout. Whichever teacher submits /jetson/pair first wins —
--   T2 can hijack the device into their classroom and inherit
--   EDUBOTICS_JETSON_HF_TOKEN delivered by the same agent at first
--   heartbeat. This is a textbook IDOR on a 20-bit numeric key.
--
-- Fix
--   Split the pair into TWO confirmation steps:
--
--     1. POST /jetson/pair-intent (teacher claims the code, atomically
--        writes intent_teacher_id on the unpaired jetsons row, receives
--        an opaque UUID intent_token in return).
--     2. POST /jetson/pair (teacher confirms with code + intent_token;
--        the RPC requires the (code, intent_token, teacher) triple to
--        match the row before binding the classroom).
--
--   The window between intent-claim and pair-confirm is short
--   (typically <1 s, the teacher's React app holds the token in memory).
--   A second teacher attempting /jetson/pair-intent on the same code
--   gets P0032; a second teacher attempting /jetson/pair without the
--   intent_token cannot even make a valid request.
--
-- New error codes
--   P0032  Pairing-Intent bereits beansprucht  → HTTP 409
--   P0033  Pairing-Intent ungültig             → HTTP 403
--
-- Idempotent — re-runnable.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Schema additions on jetsons.
--
-- intent_token: set by register_jetson at row insert time. Stays valid
-- for the full life of the pairing_code. NULL once the device is paired
-- (we deliberately clear it on successful /pair so a teacher with a
-- stale token can't replay against a re-registered code).
--
-- intent_teacher_id: NULL until a teacher's /pair-intent claims the
-- code. Once set, no other teacher can claim the same code. The RPC at
-- /pair-time asserts (code, intent_token, teacher) match.
-- ---------------------------------------------------------------------------
ALTER TABLE public.jetsons
  ADD COLUMN IF NOT EXISTS intent_token       UUID,
  ADD COLUMN IF NOT EXISTS intent_teacher_id  UUID REFERENCES public.users(id) ON DELETE SET NULL;

COMMENT ON COLUMN public.jetsons.intent_token IS
  'Opaque UUID issued at /jetson/register; returned to the teacher that successfully claims the pairing-code via /jetson/pair-intent. Required at /jetson/pair to defeat the cross-teacher pairing-code race.';
COMMENT ON COLUMN public.jetsons.intent_teacher_id IS
  'UUID of the teacher that claimed this pairing intent. Set atomically by claim_pair_intent; NULL otherwise. Prevents a second teacher on the same LAN from hijacking the 6-digit pairing code.';

-- Back-fill: existing unpaired rows that were registered before this
-- migration have no intent_token. /pair-intent gracefully assigns one
-- on first claim — see claim_pair_intent below.

-- ---------------------------------------------------------------------------
-- 2. RPC: claim_pair_intent
--    Atomically writes (intent_teacher_id, intent_token-if-null) on the
--    jetsons row matching the supplied pairing_code. The row must be
--    unpaired (classroom_id IS NULL) AND not already claimed
--    (intent_teacher_id IS NULL). Returns the intent_token so the
--    teacher's React app can pass it back to /pair-confirm.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.claim_pair_intent(
    p_pairing_code TEXT,
    p_teacher_id   UUID
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_jetson_id    UUID;
    v_intent_token UUID;
    v_existing     UUID;
    v_classroom    UUID;
    v_expires_at   TIMESTAMPTZ;
BEGIN
    -- Lock the matching row so two teachers cannot both reach the UPDATE
    -- below. Without FOR UPDATE we'd be back to the same race we're
    -- here to fix.
    SELECT id, intent_teacher_id, classroom_id, pairing_code_expires_at, intent_token
      INTO v_jetson_id, v_existing, v_classroom, v_expires_at, v_intent_token
      FROM public.jetsons
     WHERE pairing_code = p_pairing_code
     FOR UPDATE;

    IF v_jetson_id IS NULL THEN
        RAISE EXCEPTION 'Pairing-Code ungültig oder abgelaufen' USING ERRCODE = 'P0002';
    END IF;

    IF v_classroom IS NOT NULL THEN
        -- The code is for an already-paired device — treat the same as
        -- "code not found" so we don't leak that the code was valid.
        RAISE EXCEPTION 'Pairing-Code ungültig oder abgelaufen' USING ERRCODE = 'P0002';
    END IF;

    IF v_expires_at IS NOT NULL AND v_expires_at <= NOW() THEN
        RAISE EXCEPTION 'Pairing-Code ungültig oder abgelaufen' USING ERRCODE = 'P0002';
    END IF;

    IF v_existing IS NOT NULL AND v_existing <> p_teacher_id THEN
        RAISE EXCEPTION 'Pairing-Intent bereits beansprucht' USING ERRCODE = 'P0032';
    END IF;

    -- Mint an intent_token if the row was registered pre-022. Idempotent
    -- on re-claim by the SAME teacher (the React app can re-call
    -- /pair-intent if it lost the token before /pair).
    IF v_intent_token IS NULL THEN
        v_intent_token := gen_random_uuid();
    END IF;

    UPDATE public.jetsons
       SET intent_teacher_id = p_teacher_id,
           intent_token      = v_intent_token,
           updated_at        = NOW()
     WHERE id = v_jetson_id;

    RETURN v_intent_token;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.claim_pair_intent(TEXT, UUID) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.claim_pair_intent(TEXT, UUID) FROM anon;
REVOKE EXECUTE ON FUNCTION public.claim_pair_intent(TEXT, UUID) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.claim_pair_intent(TEXT, UUID) TO service_role;


-- ---------------------------------------------------------------------------
-- 3. RPC: pair_jetson — REWRITE.
--    Now requires p_intent_token AND verifies the (code, intent_token,
--    teacher) triple matches the row before the classroom is bound.
--    Replaces the migration 019 4-arg overload with a 5-arg signature.
--
-- The function name stays `pair_jetson`; PostgREST resolves overloads on
-- the argument-name set, so the existing 4-arg form would still be
-- callable if not dropped. Drop it explicitly so a route that forgets
-- to pass p_intent_token fails loudly (PGRST202) rather than silently
-- bypassing the new check.
-- ---------------------------------------------------------------------------
DROP FUNCTION IF EXISTS public.pair_jetson(UUID, TEXT, UUID, TEXT);

CREATE OR REPLACE FUNCTION public.pair_jetson(
    p_classroom_id  UUID,
    p_pairing_code  TEXT,
    p_teacher_id    UUID,
    p_mdns_name     TEXT,
    p_intent_token  UUID
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_jetson_id         UUID;
    v_classroom_teacher UUID;
BEGIN
    -- Confirm caller owns the classroom (preserved from migration 019).
    SELECT teacher_id INTO v_classroom_teacher
      FROM public.classrooms
     WHERE id = p_classroom_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Klassenzimmer nicht gefunden' USING ERRCODE = 'P0002';
    END IF;

    IF v_classroom_teacher <> p_teacher_id THEN
        RAISE EXCEPTION 'Klassenzimmer gehört nicht zu diesem Lehrer' USING ERRCODE = 'P0011';
    END IF;

    -- New: require (pairing_code, intent_token, intent_teacher_id) to all
    -- agree. The intent_teacher_id check is the IDOR fix; without it a
    -- teacher who learns the code from a colleague could still pair (the
    -- pre-022 path).
    UPDATE public.jetsons
       SET classroom_id            = p_classroom_id,
           mdns_name               = p_mdns_name,
           pairing_code            = NULL,
           pairing_code_expires_at = NULL,
           intent_token            = NULL,
           intent_teacher_id       = NULL,
           updated_at              = NOW()
     WHERE pairing_code      = p_pairing_code
       AND intent_token      = p_intent_token
       AND intent_teacher_id = p_teacher_id
       AND (pairing_code_expires_at IS NULL OR pairing_code_expires_at > NOW())
    RETURNING id INTO v_jetson_id;

    IF v_jetson_id IS NULL THEN
        RAISE EXCEPTION 'Pairing-Intent ungültig oder abgelaufen' USING ERRCODE = 'P0033';
    END IF;

    RETURN v_jetson_id;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.pair_jetson(UUID, TEXT, UUID, TEXT, UUID) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.pair_jetson(UUID, TEXT, UUID, TEXT, UUID) FROM anon;
REVOKE EXECUTE ON FUNCTION public.pair_jetson(UUID, TEXT, UUID, TEXT, UUID) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.pair_jetson(UUID, TEXT, UUID, TEXT, UUID) TO service_role;


-- ---------------------------------------------------------------------------
-- 4. regenerate_pairing_code (migration 020) — clear any stale intent
--    when the teacher rotates the code, so the next /pair-intent claims
--    against a clean row.
--
-- We keep the function signature identical (PostgREST cache compatibility);
-- only the body changes.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.regenerate_pairing_code(
    p_jetson_id   UUID,
    p_teacher_id  UUID,
    p_new_code    TEXT,
    p_expires_at  TIMESTAMPTZ
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_classroom_id      UUID;
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
       SET pairing_code            = p_new_code,
           pairing_code_expires_at = p_expires_at,
           -- Clear any prior intent claim so the next teacher can
           -- /pair-intent against the rotated code.
           intent_token            = NULL,
           intent_teacher_id       = NULL,
           updated_at              = NOW()
     WHERE id = p_jetson_id;

    RETURN p_jetson_id;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.regenerate_pairing_code(UUID, UUID, TEXT, TIMESTAMPTZ) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.regenerate_pairing_code(UUID, UUID, TEXT, TIMESTAMPTZ) FROM anon;
REVOKE EXECUTE ON FUNCTION public.regenerate_pairing_code(UUID, UUID, TEXT, TIMESTAMPTZ) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.regenerate_pairing_code(UUID, UUID, TEXT, TIMESTAMPTZ) TO service_role;

COMMIT;

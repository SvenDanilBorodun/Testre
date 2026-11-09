-- Rollback of 053 — restore the classroom-Jetson SCHEMA.
--
-- Re-creates exactly what 20261109120000_053_retire_jetson.sql dropped, each object
-- in its LATEST definition before 053:
--   * public.jetsons: the 019 columns plus 022's intent_token / intent_teacher_id,
--     the four 019 indexes plus 025's idx_jetsons_intent_teacher_id, the 019 and
--     022 column comments, RLS with 024's four consolidated policies (019's three
--     original policies were replaced by 024), GRANT ALL to service_role (the
--     request roles get the project's default privileges again, as in 019), the
--     supabase_realtime membership and the trg_jetsons_touch trigger on the
--     shared public.touch_updated_at();
--   * the 11 RPCs with their bodies, search_path, REVOKEs and service-role GRANTs:
--     claim_jetson, release_jetson, heartbeat_jetson, agent_heartbeat_jetson,
--     force_release_jetson, sweep_jetson_locks (019, baseline), agent_release_jetson,
--     unpair_jetson (020, baseline), claim_pair_intent, the 5-argument pair_jetson
--     and regenerate_pairing_code (022). The 4-argument pair_jetson stays dropped,
--     as 022 left it;
--   * public.inference_runs.jetson_id with its FK to jetsons (ON DELETE SET NULL).
--     It is re-added as the LAST column (029 had it fourth); every reader names
--     its columns, none selects *. The table's 029 comment comes back too.
--
-- DATA IS NOT RESTORED: every jetsons row (pairings, agent tokens) and every
-- inference_runs.jetson_id value that 053 dropped is gone. A Jetson would have to
-- register and be paired again.
--
-- ORDER: run this FIRST, then roll the cloud API back to a release before 053 —
-- that API's boot schema probe names jetsons and the 11 RPCs; the API of 053's
-- release ignores them, so it keeps serving while this runs.
--
-- Idempotent: CREATE ... IF NOT EXISTS / CREATE OR REPLACE / DROP POLICY IF EXISTS.
-- The closing DO block aborts if anything is missing afterwards.

BEGIN;

-- ---------------------------------------------------------------------------
-- Schema (019 + 022)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.jetsons (
  id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  classroom_id                UUID REFERENCES public.classrooms(id) ON DELETE SET NULL,
  agent_token                 UUID NOT NULL DEFAULT gen_random_uuid(),
  pairing_code                TEXT UNIQUE,
  pairing_code_expires_at     TIMESTAMPTZ,
  mdns_name                   TEXT,
  lan_ip                      TEXT,
  agent_version               TEXT,
  last_seen_at                TIMESTAMPTZ,
  current_owner_user_id       UUID REFERENCES public.users(id) ON DELETE SET NULL,
  current_owner_heartbeat_at  TIMESTAMPTZ,
  claimed_at                  TIMESTAMPTZ,
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  -- migration 022
  intent_token                UUID,
  intent_teacher_id           UUID REFERENCES public.users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_jetsons_classroom_id
  ON public.jetsons (classroom_id);

CREATE INDEX IF NOT EXISTS idx_jetsons_current_owner
  ON public.jetsons (current_owner_user_id)
  WHERE current_owner_user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_jetsons_pairing_code
  ON public.jetsons (pairing_code)
  WHERE pairing_code IS NOT NULL;

-- Sweeper needs to find expired locks fast. Partial index trims out the
-- common case (no current owner).
CREATE INDEX IF NOT EXISTS idx_jetsons_owner_heartbeat
  ON public.jetsons (current_owner_heartbeat_at)
  WHERE current_owner_user_id IS NOT NULL;

COMMENT ON TABLE public.jetsons IS
  'Classroom-shared Jetson Orin Nano inference targets. One row per physical Jetson; classroom_id binding is set at teacher pairing time.';
COMMENT ON COLUMN public.jetsons.agent_token IS
  'Long-lived per-Jetson token (UUID). Sent in every agent-heartbeat. Cleared and rotated if the device is reset via /jetson/register.';
COMMENT ON COLUMN public.jetsons.pairing_code IS
  '6-digit code shown on first boot. Teacher enters this in admin UI to bind device to a classroom. Cleared on successful pair.';
COMMENT ON COLUMN public.jetsons.mdns_name IS
  'edubotics-jetson-<short-id>.local — generated at pair time, used by React app as DNS fallback to the lan_ip.';
COMMENT ON COLUMN public.jetsons.lan_ip IS
  'Last LAN IP reported by agent heartbeat. Primary endpoint for React WS connections.';
COMMENT ON COLUMN public.jetsons.last_seen_at IS
  'Agent heartbeat (every 10s). Used by admin UI to flag offline Jetsons.';
COMMENT ON COLUMN public.jetsons.current_owner_user_id IS
  'Student currently holding the Jetson. NULL = available. Cleared by /jetson/{id}/release or by the 5-min sweeper.';
COMMENT ON COLUMN public.jetsons.current_owner_heartbeat_at IS
  'Last student React heartbeat (every 30s). 5-min silence triggers the sweeper to auto-release.';

COMMENT ON COLUMN public.jetsons.intent_token IS
  'Opaque UUID issued at /jetson/register; returned to the teacher that successfully claims the pairing-code via /jetson/pair-intent. Required at /jetson/pair to defeat the cross-teacher pairing-code race.';
COMMENT ON COLUMN public.jetsons.intent_teacher_id IS
  'UUID of the teacher that claimed this pairing intent. Set atomically by claim_pair_intent; NULL otherwise. Prevents a second teacher on the same LAN from hijacking the 6-digit pairing code.';

-- 025: cover the intent_teacher_id FK.
CREATE INDEX IF NOT EXISTS idx_jetsons_intent_teacher_id
  ON public.jetsons (intent_teacher_id)
  WHERE intent_teacher_id IS NOT NULL;

-- claim_jetson (019)
CREATE OR REPLACE FUNCTION public.claim_jetson(
    p_jetson_id UUID,
    p_user_id UUID
)
RETURNS public.jetsons
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_row public.jetsons;
BEGIN
    SELECT * INTO v_row
      FROM public.jetsons
     WHERE id = p_jetson_id
       FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Jetson nicht gefunden' USING ERRCODE = 'P0002';
    END IF;

    IF v_row.classroom_id IS NULL THEN
        RAISE EXCEPTION 'Jetson ist nicht gepaart' USING ERRCODE = 'P0002';
    END IF;

    IF v_row.current_owner_user_id IS NOT NULL THEN
        RAISE EXCEPTION 'Jetson ist bereits belegt' USING ERRCODE = 'P0030';
    END IF;

    UPDATE public.jetsons
       SET current_owner_user_id      = p_user_id,
           current_owner_heartbeat_at = NOW(),
           claimed_at                 = NOW(),
           updated_at                 = NOW()
     WHERE id = p_jetson_id
    RETURNING * INTO v_row;

    RETURN v_row;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.claim_jetson(UUID, UUID) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.claim_jetson(UUID, UUID) FROM anon;
REVOKE EXECUTE ON FUNCTION public.claim_jetson(UUID, UUID) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.claim_jetson(UUID, UUID) TO service_role;

-- release_jetson (019)
CREATE OR REPLACE FUNCTION public.release_jetson(
    p_jetson_id UUID,
    p_user_id UUID
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    UPDATE public.jetsons
       SET current_owner_user_id      = NULL,
           current_owner_heartbeat_at = NULL,
           claimed_at                 = NULL,
           updated_at                 = NOW()
     WHERE id = p_jetson_id
       AND current_owner_user_id = p_user_id;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.release_jetson(UUID, UUID) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.release_jetson(UUID, UUID) FROM anon;
REVOKE EXECUTE ON FUNCTION public.release_jetson(UUID, UUID) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.release_jetson(UUID, UUID) TO service_role;

-- heartbeat_jetson (019)
CREATE OR REPLACE FUNCTION public.heartbeat_jetson(
    p_jetson_id UUID,
    p_user_id UUID
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
       SET current_owner_heartbeat_at = NOW(),
           updated_at                 = NOW()
     WHERE id = p_jetson_id
       AND current_owner_user_id = p_user_id;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    IF v_rows = 0 THEN
        RAISE EXCEPTION 'Lock verloren — bitte erneut verbinden' USING ERRCODE = 'P0031';
    END IF;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.heartbeat_jetson(UUID, UUID) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.heartbeat_jetson(UUID, UUID) FROM anon;
REVOKE EXECUTE ON FUNCTION public.heartbeat_jetson(UUID, UUID) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.heartbeat_jetson(UUID, UUID) TO service_role;

-- agent_heartbeat_jetson (019)
CREATE OR REPLACE FUNCTION public.agent_heartbeat_jetson(
    p_jetson_id UUID,
    p_agent_token UUID,
    p_lan_ip TEXT,
    p_agent_version TEXT
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_owner UUID;
    v_rows INTEGER;
BEGIN
    UPDATE public.jetsons
       SET last_seen_at  = NOW(),
           lan_ip        = COALESCE(NULLIF(p_lan_ip, ''), lan_ip),
           agent_version = COALESCE(NULLIF(p_agent_version, ''), agent_version),
           updated_at    = NOW()
     WHERE id = p_jetson_id
       AND agent_token = p_agent_token
    RETURNING current_owner_user_id INTO v_owner;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    IF v_rows = 0 THEN
        RAISE EXCEPTION 'Agent-Token ungültig oder Jetson nicht gefunden' USING ERRCODE = 'P0001';
    END IF;

    RETURN v_owner;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.agent_heartbeat_jetson(UUID, UUID, TEXT, TEXT) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.agent_heartbeat_jetson(UUID, UUID, TEXT, TEXT) FROM anon;
REVOKE EXECUTE ON FUNCTION public.agent_heartbeat_jetson(UUID, UUID, TEXT, TEXT) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.agent_heartbeat_jetson(UUID, UUID, TEXT, TEXT) TO service_role;

-- force_release_jetson (019)
CREATE OR REPLACE FUNCTION public.force_release_jetson(
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
     WHERE id = p_jetson_id;

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
       SET current_owner_user_id      = NULL,
           current_owner_heartbeat_at = NULL,
           claimed_at                 = NULL,
           updated_at                 = NOW()
     WHERE id = p_jetson_id;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.force_release_jetson(UUID, UUID) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.force_release_jetson(UUID, UUID) FROM anon;
REVOKE EXECUTE ON FUNCTION public.force_release_jetson(UUID, UUID) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.force_release_jetson(UUID, UUID) TO service_role;

-- sweep_jetson_locks (019)
CREATE OR REPLACE FUNCTION public.sweep_jetson_locks()
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_affected INTEGER;
BEGIN
    UPDATE public.jetsons
       SET current_owner_user_id      = NULL,
           current_owner_heartbeat_at = NULL,
           claimed_at                 = NULL,
           updated_at                 = NOW()
     WHERE current_owner_user_id IS NOT NULL
       AND current_owner_heartbeat_at < NOW() - INTERVAL '5 minutes';
    GET DIAGNOSTICS v_affected = ROW_COUNT;
    RETURN v_affected;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.sweep_jetson_locks() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.sweep_jetson_locks() FROM anon;
REVOKE EXECUTE ON FUNCTION public.sweep_jetson_locks() FROM authenticated;
GRANT EXECUTE ON FUNCTION public.sweep_jetson_locks() TO service_role;

-- agent_release_jetson (020)
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

-- unpair_jetson (020)
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

-- claim_pair_intent (022)
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

-- pair_jetson (022)
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

-- regenerate_pairing_code (022)
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

-- ---------------------------------------------------------------------------
-- RLS, grants, Realtime, trigger (019; policies as 024 left them)
-- ---------------------------------------------------------------------------
ALTER TABLE public.jetsons ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Jetsons read consolidated"                 ON public.jetsons;
DROP POLICY IF EXISTS "Jetsons write teacher or admin insert"     ON public.jetsons;
DROP POLICY IF EXISTS "Jetsons write teacher or admin update"     ON public.jetsons;
DROP POLICY IF EXISTS "Jetsons write teacher or admin delete"     ON public.jetsons;

CREATE POLICY "Jetsons read consolidated"
  ON public.jetsons FOR SELECT
  USING (
    -- Classroom members (student or teacher) for their own classroom Jetson.
    (
      classroom_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM public.users u
         WHERE u.id = (SELECT auth.uid())
           AND u.classroom_id = jetsons.classroom_id
      )
    )
    -- Teachers managing their own classroom Jetson.
    OR (
      classroom_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM public.classrooms c
         WHERE c.id = jetsons.classroom_id
           AND c.teacher_id = (SELECT auth.uid())
      )
    )
    -- Admins.
    OR EXISTS (
      SELECT 1 FROM public.users u
       WHERE u.id = (SELECT auth.uid()) AND u.role = 'admin'
    )
  );

COMMENT ON POLICY "Jetsons read consolidated" ON public.jetsons IS
  'Consolidates Classroom-members + Teacher + Admin. InitPlan auth.uid().';

CREATE POLICY "Jetsons write teacher or admin insert"
  ON public.jetsons FOR INSERT
  WITH CHECK (
    (
      classroom_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM public.classrooms c
         WHERE c.id = jetsons.classroom_id
           AND c.teacher_id = (SELECT auth.uid())
      )
    )
    OR EXISTS (
      SELECT 1 FROM public.users u
       WHERE u.id = (SELECT auth.uid()) AND u.role = 'admin'
    )
  );

CREATE POLICY "Jetsons write teacher or admin update"
  ON public.jetsons FOR UPDATE
  USING (
    (
      classroom_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM public.classrooms c
         WHERE c.id = jetsons.classroom_id
           AND c.teacher_id = (SELECT auth.uid())
      )
    )
    OR EXISTS (
      SELECT 1 FROM public.users u
       WHERE u.id = (SELECT auth.uid()) AND u.role = 'admin'
    )
  )
  WITH CHECK (
    (
      classroom_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM public.classrooms c
         WHERE c.id = jetsons.classroom_id
           AND c.teacher_id = (SELECT auth.uid())
      )
    )
    OR EXISTS (
      SELECT 1 FROM public.users u
       WHERE u.id = (SELECT auth.uid()) AND u.role = 'admin'
    )
  );

CREATE POLICY "Jetsons write teacher or admin delete"
  ON public.jetsons FOR DELETE
  USING (
    (
      classroom_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM public.classrooms c
         WHERE c.id = jetsons.classroom_id
           AND c.teacher_id = (SELECT auth.uid())
      )
    )
    OR EXISTS (
      SELECT 1 FROM public.users u
       WHERE u.id = (SELECT auth.uid()) AND u.role = 'admin'
    )
  );

GRANT ALL ON public.jetsons TO service_role;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_publication_tables
     WHERE pubname = 'supabase_realtime'
       AND schemaname = 'public'
       AND tablename = 'jetsons'
  ) THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.jetsons;
  END IF;
END
$$;

DROP TRIGGER IF EXISTS trg_jetsons_touch ON public.jetsons;
CREATE TRIGGER trg_jetsons_touch
    BEFORE UPDATE ON public.jetsons
    FOR EACH ROW
    EXECUTE FUNCTION public.touch_updated_at();

-- ---------------------------------------------------------------------------
-- inference_runs.jetson_id (029)
-- ---------------------------------------------------------------------------
ALTER TABLE public.inference_runs
  ADD COLUMN IF NOT EXISTS jetson_id UUID REFERENCES public.jetsons(id) ON DELETE SET NULL;

-- 029's table comment, as it stood before 053.
COMMENT ON TABLE public.inference_runs IS
  'Compact per-run Jetson inference records (leLab-comparison PR-5b). '
  'Written student-self via the cloud API (JWT-keyed); read by the '
  'owning teacher. RLS deliberately has no permissive policies — the '
  'service-role API with its ownership asserts is the only path.';

-- ---------------------------------------------------------------------------
-- Self-check
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  v_sig TEXT;
BEGIN
  IF to_regclass('public.jetsons') IS NULL THEN
    RAISE EXCEPTION '053 rollback: public.jetsons is missing';
  END IF;
  FOREACH v_sig IN ARRAY ARRAY[
    'public.claim_jetson(uuid, uuid)',
    'public.release_jetson(uuid, uuid)',
    'public.heartbeat_jetson(uuid, uuid)',
    'public.agent_heartbeat_jetson(uuid, uuid, text, text)',
    'public.force_release_jetson(uuid, uuid)',
    'public.sweep_jetson_locks()',
    'public.agent_release_jetson(uuid, uuid)',
    'public.unpair_jetson(uuid, uuid)',
    'public.claim_pair_intent(text, uuid)',
    'public.pair_jetson(uuid, text, uuid, text, uuid)',
    'public.regenerate_pairing_code(uuid, uuid, text, timestamp with time zone)'
  ] LOOP
    IF to_regprocedure(v_sig) IS NULL THEN
      RAISE EXCEPTION '053 rollback: % is missing', v_sig;
    END IF;
    IF NOT has_function_privilege('service_role', v_sig, 'EXECUTE') THEN
      RAISE EXCEPTION '053 rollback: service_role cannot execute %', v_sig;
    END IF;
    IF has_function_privilege('anon', v_sig, 'EXECUTE')
       OR has_function_privilege('authenticated', v_sig, 'EXECUTE') THEN
      RAISE EXCEPTION '053 rollback: a request role can execute %', v_sig;
    END IF;
  END LOOP;
  IF to_regprocedure('public.pair_jetson(uuid, text, uuid, text)') IS NOT NULL THEN
    RAISE EXCEPTION '053 rollback: the pre-022 4-argument pair_jetson exists';
  END IF;
  IF (SELECT count(*) FROM pg_policies WHERE schemaname = 'public' AND tablename = 'jetsons') <> 4 THEN
    RAISE EXCEPTION '053 rollback: public.jetsons does not carry exactly the four 024 policies';
  END IF;
  IF (SELECT count(*) FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'jetsons'
       AND indexname LIKE 'idx_jetsons_%') <> 5 THEN
    RAISE EXCEPTION '053 rollback: public.jetsons does not carry its five indexes';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_jetsons_touch'
                  AND tgrelid = 'public.jetsons'::regclass) THEN
    RAISE EXCEPTION '053 rollback: trg_jetsons_touch is missing';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_publication_tables WHERE pubname = 'supabase_realtime'
                  AND schemaname = 'public' AND tablename = 'jetsons') THEN
    RAISE EXCEPTION '053 rollback: jetsons is not in supabase_realtime';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = 'inference_runs' AND column_name = 'jetson_id'
  ) THEN
    RAISE EXCEPTION '053 rollback: public.inference_runs.jetson_id is missing';
  END IF;
END
$$;

COMMIT;

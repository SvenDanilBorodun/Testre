-- 019_classroom_jetsons.sql
--
-- Classroom Jetson Orin Nano support: one paired Jetson per classroom,
-- first-come explicit-disconnect lock for the inference tab, full
-- wipe-on-release lifecycle. Scope is intentionally narrow: ONLY the
-- Inference tab in EduBotics connects to the Jetson; Roboter Studio
-- (Workshop), Calibration, and Recording all stay on the student PC.
--
-- Two heartbeat layers:
--   1. Agent → Cloud API every 10s  → ``last_seen_at`` (liveness).
--   2. Student React → Cloud API every 30s → ``current_owner_heartbeat_at``
--      (lock-hold). After 5 minutes of silence the sweeper in
--      cloud_training_api/app/services/jetson_sweep.py auto-releases
--      the lock so a crashed browser doesn't permanently block the
--      classroom Jetson.
--
-- Two new Postgres error codes (registered in CLAUDE.md §7.5):
--   P0030 → 409 Jetson belegt        (claim race / second-claimer)
--   P0031 → 410 Lock verloren        (heartbeat from non-owner)
--
-- Service-role bypasses all RLS — Python ``_assert_*_owned`` helpers
-- in routes/jetson.py are the actual authorisation layer (consistent
-- with the rest of the cloud API, which treats RLS as defense-in-depth).
--
-- Idempotent — re-runnable.

BEGIN;

-- ---------------------------------------------------------------------------
-- Schema
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
  updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
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

-- ---------------------------------------------------------------------------
-- claim_jetson: atomic compare-and-swap, raises P0030 if already held.
-- The SELECT ... FOR UPDATE pessimistic-locks the row for the duration
-- of the transaction so two concurrent claims serialise via Postgres
-- row-level locking (no application-side mutex needed).
-- ---------------------------------------------------------------------------
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


-- ---------------------------------------------------------------------------
-- release_jetson: idempotent. Only clears the lock if the caller IS the
-- current owner (prevents one student stomping on another's session).
-- Returns silently in either case so callers don't have to special-case
-- "already released" (e.g. /jetson/{id}/release called via sendBeacon
-- on tab unload races with the 5-min sweeper).
-- ---------------------------------------------------------------------------
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


-- ---------------------------------------------------------------------------
-- heartbeat_jetson: student-side 30s heartbeat. Raises P0031 if the
-- caller is not (or no longer) the current owner — React handles that
-- by auto-disconnecting and surfacing a German toast.
-- ---------------------------------------------------------------------------
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


-- ---------------------------------------------------------------------------
-- agent_heartbeat_jetson: agent-side 10s heartbeat. Verifies agent_token
-- so a rogue device on the classroom LAN can't impersonate a paired
-- Jetson (the token is provisioned at /jetson/register and lives in
-- /etc/edubotics/jetson.env mode 600). Returns current_owner_user_id
-- so the agent's rosbridge proxy knows which JWT sub to allow.
-- ---------------------------------------------------------------------------
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


-- ---------------------------------------------------------------------------
-- pair_jetson: teacher binds a registered Jetson to one of their own
-- classrooms via the 6-digit pairing code. Asserts the caller owns the
-- target classroom (P0011 conventions from migration 002). The pairing
-- code is one-time-use: cleared on success so a second teacher can't
-- replay it.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.pair_jetson(
    p_classroom_id UUID,
    p_pairing_code TEXT,
    p_teacher_id UUID,
    p_mdns_name TEXT
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_jetson_id UUID;
    v_classroom_teacher UUID;
BEGIN
    -- Confirm caller owns the classroom.
    SELECT teacher_id INTO v_classroom_teacher
      FROM public.classrooms
     WHERE id = p_classroom_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Klassenzimmer nicht gefunden' USING ERRCODE = 'P0002';
    END IF;

    IF v_classroom_teacher <> p_teacher_id THEN
        RAISE EXCEPTION 'Klassenzimmer gehört nicht zu diesem Lehrer' USING ERRCODE = 'P0011';
    END IF;

    UPDATE public.jetsons
       SET classroom_id            = p_classroom_id,
           mdns_name               = p_mdns_name,
           pairing_code            = NULL,
           pairing_code_expires_at = NULL,
           updated_at              = NOW()
     WHERE pairing_code = p_pairing_code
       AND (pairing_code_expires_at IS NULL OR pairing_code_expires_at > NOW())
    RETURNING id INTO v_jetson_id;

    IF v_jetson_id IS NULL THEN
        RAISE EXCEPTION 'Pairing-Code ungültig oder abgelaufen' USING ERRCODE = 'P0002';
    END IF;

    RETURN v_jetson_id;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.pair_jetson(UUID, TEXT, UUID, TEXT) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.pair_jetson(UUID, TEXT, UUID, TEXT) FROM anon;
REVOKE EXECUTE ON FUNCTION public.pair_jetson(UUID, TEXT, UUID, TEXT) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.pair_jetson(UUID, TEXT, UUID, TEXT) TO service_role;


-- ---------------------------------------------------------------------------
-- force_release_jetson: teacher emergency unlock. Used from the admin
-- dashboard when a student walked out without clicking Trennen and
-- 5 minutes is too long to wait (e.g. between two consecutive class
-- periods).
-- ---------------------------------------------------------------------------
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


-- ---------------------------------------------------------------------------
-- sweep_jetson_locks: server-side sweeper called from
-- cloud_training_api/app/services/jetson_sweep.py every 60s. Auto-
-- releases any lock whose heartbeat is older than 5 minutes. Single
-- statement, no row-by-row iteration — Postgres applies the predicate
-- once per Jetson row. Returns the count of released locks for
-- telemetry. Service-role only.
-- ---------------------------------------------------------------------------
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


-- ---------------------------------------------------------------------------
-- RLS: defense-in-depth. Service-role bypasses (it's the cloud API's
-- only Supabase auth context) but anon-key reads from the React app
-- could one day flow through these policies, so they're set up now to
-- avoid a later silent IDOR if the auth model is ever swapped.
-- ---------------------------------------------------------------------------
ALTER TABLE public.jetsons ENABLE ROW LEVEL SECURITY;

-- Classroom members (students + the teacher) can SELECT their classroom's
-- Jetson. Used by React to populate the availability chip.
DROP POLICY IF EXISTS "Classroom members read classroom jetson" ON public.jetsons;
CREATE POLICY "Classroom members read classroom jetson"
    ON public.jetsons
    FOR SELECT
    USING (
        classroom_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM public.users u
             WHERE u.id = auth.uid()
               AND u.classroom_id = jetsons.classroom_id
        )
    );

-- Teachers can SELECT/UPDATE Jetsons in classrooms they own. Mostly a
-- placeholder for a future teacher-direct-write path; today every write
-- goes through service-role RPCs.
DROP POLICY IF EXISTS "Teachers manage own classroom jetson" ON public.jetsons;
CREATE POLICY "Teachers manage own classroom jetson"
    ON public.jetsons
    FOR ALL
    USING (
        classroom_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM public.classrooms c
             WHERE c.id = jetsons.classroom_id
               AND c.teacher_id = auth.uid()
        )
    )
    WITH CHECK (
        classroom_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM public.classrooms c
             WHERE c.id = jetsons.classroom_id
               AND c.teacher_id = auth.uid()
        )
    );

-- Admins: all.
DROP POLICY IF EXISTS "Admins manage jetsons" ON public.jetsons;
CREATE POLICY "Admins manage jetsons"
    ON public.jetsons
    FOR ALL
    USING (
        EXISTS (
            SELECT 1 FROM public.users u
             WHERE u.id = auth.uid()
               AND u.role = 'admin'
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM public.users u
             WHERE u.id = auth.uid()
               AND u.role = 'admin'
        )
    );

GRANT ALL ON public.jetsons TO service_role;


-- ---------------------------------------------------------------------------
-- Realtime publication so React's useSupabaseJetson hook gets push
-- updates on availability + owner changes (matches the workflows /
-- workgroups / datasets pattern from migrations 008 / 011).
-- ---------------------------------------------------------------------------
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


-- ---------------------------------------------------------------------------
-- touch_updated_at trigger. The function is shared across migrations
-- and was hardened in migration 008 with ``SET search_path = public``
-- as defense-in-depth against search_path injection. We deliberately
-- do NOT re-define the function here — a `CREATE OR REPLACE FUNCTION`
-- would strip the 008 hardening when 019 is applied (silent security
-- regression on every existing trigger using it). 019 just installs
-- a NEW trigger that references the existing function.
--
-- Pre-condition: migration 004 (which originally introduced the
-- function) MUST have been applied. The schema fingerprint in
-- cloud_training_api/app/main.py validates this at Cloud API boot.
-- ---------------------------------------------------------------------------
DROP TRIGGER IF EXISTS trg_jetsons_touch ON public.jetsons;
CREATE TRIGGER trg_jetsons_touch
    BEFORE UPDATE ON public.jetsons
    FOR EACH ROW
    EXECUTE FUNCTION public.touch_updated_at();

COMMIT;

-- 042 — user_hf_credentials: a student's OWN Hugging Face token, stored with the
-- account (encrypted by the cloud API), so the robot gets it automatically at
-- login instead of every student on a PC sharing one token in the PC's .env.
--
-- Proven on a scratch PostgreSQL 16 with Supabase-like roles and default
-- privileges (supabase/tests/042_user_hf_credentials_assertions.sql -> 19 PASS;
-- rollback, re-apply twice, assertions again -> 19 PASS). NOT yet run on a real
-- `supabase start` stack: do that once before the first production push.
-- Rollback: rollback/20261003120000_042_user_hf_credentials_rollback.sql
-- (roll the cloud API back FIRST: the 042 API probes this table and calls the RPC).
--
-- NUMBERING: 037 stays deliberately skipped (reserved on disk for „Eigene
-- Objekte" in docs/plans/eigene-objekte-sql/). Timestamp 20261003120000 >
-- 20260927120000 (041), so `supabase db push` orders it last.
--
-- WHY A SEPARATE TABLE (and not a column on public.users)
--   `users` rows are readable by their owner and by the owning teacher through
--   PostgREST, ALL columns (RLS policies are row-level, never column-level). A
--   token column there would hand every student's ciphertext to their teacher
--   and to the student's own browser. This table follows the service-role-only
--   pattern of 036 (edu6_arm_records) and 029 (inference_runs): RLS enabled,
--   NO policies, every grant to anon/authenticated revoked, service_role only
--   (Rule §4: the cloud API's ownership asserts are the real layer).
--
-- WHAT IS STORED
--   token_ciphertext  the AES-256-GCM envelope `v1.<kid>.<base64url>` built by
--                     cloud_training_api/app/services/hf_credentials.py (key in
--                     the Railway variable EDUBOTICS_HF_TOKEN_KEY, AAD = the
--                     user id). The CHECK below refuses anything that is not an
--                     envelope, so a plaintext `hf_…` token can never be written
--                     here by a bug.
--   token_fp          sha256("edubotics-hf-token-fp:" || token), first 16 hex
--                     chars: lets the robot and the page compare tokens without
--                     either side reading the other's secret.
--   token_hint        `hf_…` + the last four characters, for the Startseite card.
--   hf_username       the account name HuggingFace itself reported for the token
--                     (whoami), i.e. PROVEN, unlike the self-asserted PATCH /me.
--   token_role        whoami's auth.accessToken.role. NEVER `read`: a read-only
--                     token cannot upload, and the cloud API rejects it before
--                     it gets here; the CHECK is the second wall.
--
-- store_user_hf_credential() writes the credential AND sets users.hf_username
-- to the proven name in ONE transaction, under the same users-row lock idiom as
-- register_dataset_safe (026), so a concurrent PATCH /me cannot interleave.
-- Service-role only (026/041 grant style). Idempotent — re-runnable.

BEGIN;

CREATE TABLE IF NOT EXISTS public.user_hf_credentials (
    user_id          uuid PRIMARY KEY REFERENCES public.users(id) ON DELETE CASCADE,
    token_ciphertext text NOT NULL,
    token_fp         text NOT NULL,
    token_hint       text NOT NULL,
    hf_username      text NOT NULL,
    token_role       text NOT NULL,
    validated_at     timestamptz NOT NULL DEFAULT now(),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT user_hf_credentials_ciphertext_is_envelope
        CHECK (token_ciphertext ~ '^v1\.[0-9a-f]{8}\.[A-Za-z0-9_-]{32,}$'),
    CONSTRAINT user_hf_credentials_fp_shape
        CHECK (token_fp ~ '^[0-9a-f]{16}$'),
    CONSTRAINT user_hf_credentials_hint_length
        CHECK (char_length(token_hint) BETWEEN 1 AND 16),
    CONSTRAINT user_hf_credentials_name_length
        CHECK (char_length(hf_username) BETWEEN 1 AND 96),
    CONSTRAINT user_hf_credentials_never_read_only
        CHECK (token_role <> 'read')
);

ALTER TABLE public.user_hf_credentials ENABLE ROW LEVEL SECURITY;

-- Supabase's default privileges grant every new public table to anon and
-- authenticated; with RLS on and no policy that is a closed table, but the
-- grants are revoked as well (defence in depth — 036 relied on RLS alone).
REVOKE ALL ON TABLE public.user_hf_credentials FROM PUBLIC;
REVOKE ALL ON TABLE public.user_hf_credentials FROM anon;
REVOKE ALL ON TABLE public.user_hf_credentials FROM authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.user_hf_credentials TO service_role;

COMMENT ON TABLE public.user_hf_credentials IS
    'A student''s own Hugging Face token, AES-256-GCM encrypted by the cloud API (042). Service-role only: RLS on, no policies, no anon/authenticated grants. Not in Realtime.';

-- updated_at maintenance (same trigger idiom as 036).
CREATE OR REPLACE FUNCTION public.user_hf_credentials_touch()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = ''
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS user_hf_credentials_touch ON public.user_hf_credentials;
CREATE TRIGGER user_hf_credentials_touch
    BEFORE UPDATE ON public.user_hf_credentials
    FOR EACH ROW EXECUTE FUNCTION public.user_hf_credentials_touch();

-- ---------------------------------------------------------------------------
-- the one writer
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.store_user_hf_credential(
    p_user_id      uuid,
    p_ciphertext   text,
    p_fp           text,
    p_hint         text,
    p_hf_username  text,
    p_role         text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_user uuid;
    v_row  public.user_hf_credentials;
BEGIN
    -- 1. Lock the users row: the same per-user mutex register_dataset_safe
    --    takes. It also makes an unknown id fail HERE, before any write, which
    --    is what the boot schema probe's dummy-id call relies on (P0002).
    SELECT id INTO v_user FROM public.users WHERE id = p_user_id FOR UPDATE;
    IF v_user IS NULL THEN
        RAISE EXCEPTION 'Benutzer nicht gefunden' USING ERRCODE = 'P0002';
    END IF;

    -- 2. Upsert the credential. created_at survives a replace; the CHECK
    --    constraints refuse a plaintext token, a read-only role or a malformed
    --    fingerprint with 23514.
    INSERT INTO public.user_hf_credentials AS c
        (user_id, token_ciphertext, token_fp, token_hint, hf_username, token_role, validated_at)
    VALUES
        (p_user_id, p_ciphertext, p_fp, p_hint, p_hf_username, p_role, now())
    ON CONFLICT (user_id) DO UPDATE SET
        token_ciphertext = EXCLUDED.token_ciphertext,
        token_fp         = EXCLUDED.token_fp,
        token_hint       = EXCLUDED.token_hint,
        hf_username      = EXCLUDED.hf_username,
        token_role       = EXCLUDED.token_role,
        validated_at     = now()
    RETURNING * INTO v_row;

    -- 3. The proven account name becomes the dataset anchor (030).
    UPDATE public.users SET hf_username = p_hf_username WHERE id = p_user_id;

    -- Metadata only: the ciphertext never leaves this function.
    RETURN jsonb_build_object(
        'stored',       true,
        'hf_username',  v_row.hf_username,
        'hint',         v_row.token_hint,
        'fp',           v_row.token_fp,
        'role',         v_row.token_role,
        'validated_at', v_row.validated_at
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.store_user_hf_credential(uuid, text, text, text, text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.store_user_hf_credential(uuid, text, text, text, text, text) FROM anon;
REVOKE EXECUTE ON FUNCTION public.store_user_hf_credential(uuid, text, text, text, text, text) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.store_user_hf_credential(uuid, text, text, text, text, text) TO service_role;

-- Run the SECURITY DEFINER writer with a bypassing-RLS owner (040/041 pattern).
DO $$
DECLARE
  v_done BOOLEAN := FALSE;
  v_role TEXT;
BEGIN
  FOREACH v_role IN ARRAY ARRAY['postgres', 'supabase_admin', 'supabase_storage_admin'] LOOP
    BEGIN
      EXECUTE format('ALTER FUNCTION public.store_user_hf_credential(uuid, text, text, text, text, text) OWNER TO %I', v_role);
      v_done := TRUE;
      EXIT;
    EXCEPTION
      WHEN OTHERS THEN
        CONTINUE;
    END;
  END LOOP;
  IF NOT v_done THEN
    RAISE NOTICE 'Could not transfer ownership of store_user_hf_credential to a privileged role.';
  END IF;
END $$;

COMMIT;

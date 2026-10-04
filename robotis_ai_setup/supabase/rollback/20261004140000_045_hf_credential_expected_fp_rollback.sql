-- Rollback for 045_hf_credential_expected_fp: drops the seven-argument writer
-- and restores migration 042's six-argument store_user_hf_credential EXACTLY
-- (the block below is copied verbatim from
-- 20261003120000_042_user_hf_credentials.sql, "the one writer" through its
-- owner transfer; tests/test_supabase_043_045.py holds the copy byte-equal).
--
-- Roll the cloud API back FIRST: the 045 API always sends p_expected_fp and its
-- boot schema probe names it, so against the six-argument function every PUT
-- and verify would fail (PGRST202) and /health would never pass. After the
-- rollback, /verify's re-read-then-upsert race of 042 is back
-- (docs/KNOWN-ISSUES.md). No data is touched.
BEGIN;

DROP FUNCTION IF EXISTS public.store_user_hf_credential(uuid, text, text, text, text, text, text);

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

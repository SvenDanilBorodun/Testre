-- 045 — store_user_hf_credential compares an EXPECTED fingerprint under the
-- row locks, so POST /me/hf-token/verify can never resurrect a removed token
-- or overwrite a newer one (owner decision d, 2026-10-04).
--
-- Proven on a scratch PostgreSQL 17 (public.ecr.aws/supabase/postgres:17.6.1.106,
-- the guarded baseline + every migration through 045) with
-- supabase/tests/045_hf_credential_expected_fp_assertions.sql -> 12 PASS;
-- rollback, re-apply, assertions again -> 12 PASS. Both race orders were
-- also run in two concurrent psql sessions there (a DELETE arriving while
-- verify holds the locks; verify arriving while a DELETE holds the row): the
-- token was gone afterwards both times, and the second order answered P0045.
-- NOT yet run on a real `supabase start` stack. After 045, 042's assertion file is no longer the
-- right check for the writer: its T4 (six arguments) and T5a/T5b (the
-- six-argument regprocedure) fail by design.
-- Rollback: rollback/20261004140000_045_hf_credential_expected_fp_rollback.sql
-- (roll the cloud API back FIRST: the 045 API always sends p_expected_fp, and
-- against the six-argument function every PUT and verify fails with PGRST202;
-- its boot schema probe names p_expected_fp, so its /health would fail too).
--
-- THE RACE (residual of 042)
--   /verify decrypts the stored token, asks the Hub (up to 10 s), re-reads the
--   fingerprint and then calls the writer, whose upsert is unconditional. A
--   DELETE (or a PUT of another token) landing between the re-read and the
--   upsert was written over: a token the student removed came back.
--
-- THE FIX
--   A seventh argument, p_expected_fp TEXT DEFAULT NULL. NULL (PUT) keeps 042's
--   behaviour exactly. Non-NULL (verify): after the users-row lock, the writer
--   reads the credential row FOR UPDATE and raises P0045 unless it exists AND
--   its token_fp equals p_expected_fp. The row lock is what makes the compare
--   atomic against DELETE /me/hf-token, which does not take the users-row
--   lock: a DELETE that already locked the row makes this SELECT wait and then
--   find nothing (P0045); a DELETE that comes later waits for this transaction
--   and then deletes the refreshed row. A concurrent PUT is serialised by the
--   users-row lock both take. The route maps P0045 to its existing German 409.
--
-- SIGNATURE: the six-argument function is DROPPED and the seven-argument one
-- created, so exactly one overload exists (PostgREST resolves on the argument
-- name set; two overloads that both accept the six names would be ambiguous).
-- The DEFAULT keeps an older API's six named arguments resolving. Grants and
-- owner follow 042. Idempotent — re-runnable.

BEGIN;

DROP FUNCTION IF EXISTS public.store_user_hf_credential(uuid, text, text, text, text, text);

CREATE OR REPLACE FUNCTION public.store_user_hf_credential(
    p_user_id      uuid,
    p_ciphertext   text,
    p_fp           text,
    p_hint         text,
    p_hf_username  text,
    p_role         text,
    p_expected_fp  text DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_user       uuid;
    v_row        public.user_hf_credentials;
    v_current_fp text;
BEGIN
    -- 1. Lock the users row (042; register_dataset_safe takes the same lock).
    --    An unknown id fails HERE, before any read or write: the boot schema
    --    probe's dummy-id call relies on P0002.
    SELECT id INTO v_user FROM public.users WHERE id = p_user_id FOR UPDATE;
    IF v_user IS NULL THEN
        RAISE EXCEPTION 'Benutzer nicht gefunden' USING ERRCODE = 'P0002';
    END IF;

    -- 1b. (045) The caller's expectation, compared under the credential row's
    --     own lock: verify must find exactly the token it decrypted.
    IF p_expected_fp IS NOT NULL THEN
        SELECT token_fp INTO v_current_fp
          FROM public.user_hf_credentials
         WHERE user_id = p_user_id
           FOR UPDATE;
        IF NOT FOUND OR v_current_fp IS DISTINCT FROM p_expected_fp THEN
            RAISE EXCEPTION 'Das Token wurde inzwischen geändert oder entfernt.'
                USING ERRCODE = 'P0045';
        END IF;
    END IF;

    -- 2. Upsert the credential (042, unchanged).
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

    -- 3. The proven account name becomes the dataset anchor (030). 044's guard
    --    accepts it: the credential above already carries the same name.
    UPDATE public.users SET hf_username = p_hf_username WHERE id = p_user_id;

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

REVOKE EXECUTE ON FUNCTION public.store_user_hf_credential(uuid, text, text, text, text, text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.store_user_hf_credential(uuid, text, text, text, text, text, text) FROM anon;
REVOKE EXECUTE ON FUNCTION public.store_user_hf_credential(uuid, text, text, text, text, text, text) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.store_user_hf_credential(uuid, text, text, text, text, text, text) TO service_role;

DO $$
DECLARE
  v_done BOOLEAN := FALSE;
  v_role TEXT;
BEGIN
  FOREACH v_role IN ARRAY ARRAY['postgres', 'supabase_admin', 'supabase_storage_admin'] LOOP
    BEGIN
      EXECUTE format('ALTER FUNCTION public.store_user_hf_credential(uuid, text, text, text, text, text, text) OWNER TO %I', v_role);
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

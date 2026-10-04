-- 043 — register_dataset_safe accepts the PROVEN account name as a second
-- HF-author anchor (owner decision S3, 2026-10-04).
--
-- Proven on a scratch PostgreSQL 17 (public.ecr.aws/supabase/postgres:17.6.1.106,
-- the guarded baseline + every migration through 045) with
-- supabase/tests/043_register_dataset_proven_anchor_assertions.sql -> 12 PASS;
-- rollback, re-apply, assertions again -> 12 PASS. Run again on 2026-10-04 on
-- a real `supabase start` stack (Postgres 17, migrations through 045 applied
-- with psql as `postgres`): 12 PASS.
-- Rollback: rollback/20261004120000_043_register_dataset_proven_anchor_rollback.sql
-- (restores 026's body verbatim; no cloud API change depends on 043).
--
-- NUMBERING: 037 stays deliberately skipped (reserved for „Eigene Objekte" in
-- docs/plans/eigene-objekte-sql/). Timestamp 20261004120000 > 20261003120000
-- (042), so `supabase db push` orders it after 042.
--
-- WHY
--   026 anchors a student's datasets on the author of the student's OLDEST
--   datasets row. 042 lets a student save a token for a DIFFERENT Hugging Face
--   account than before (store_user_hf_credential re-points users.hf_username),
--   and from then on every new dataset the robot uploads under the new account
--   was refused with P0034 -> 403 until the old rows were gone: the student
--   recorded, uploaded, and could not train on it.
--
--   The name in public.user_hf_credentials.hf_username is the one Hugging Face
--   itself reported for the student's stored token (whoami), i.e. PROVEN — the
--   same proof the robot uploads with. A p_hf_author equal to it is the
--   student's own account by construction, so it is accepted beside the oldest
--   row's author. The oldest-row anchor itself stays: a student without a
--   stored token (or an author matching neither) is judged exactly as in 026.
--
-- WHAT CHANGES
--   ONE condition in step 3: the IDOR block now also needs p_hf_author to
--   differ from the stored credential's name. Same normalisation as 026
--   (lower() on both sides). Signature, return type, SECURITY DEFINER,
--   search_path, error codes, the users-row lock and the upsert are byte-for-
--   byte 026. CREATE OR REPLACE keeps the grants and the owner; they are
--   re-asserted below anyway (026 grant style), and the owner is moved to a
--   privileged role the 042 way so the definer can read user_hf_credentials
--   (RLS on, no policies, service_role-only grants since 042).
--
-- Idempotent — re-runnable.

BEGIN;

CREATE OR REPLACE FUNCTION public.register_dataset_safe(
    p_user_id        UUID,
    p_hf_repo_id     TEXT,
    p_hf_author      TEXT,
    p_workgroup_id   UUID,
    p_name           TEXT,
    p_description    TEXT,
    p_episode_count  INTEGER,
    p_total_frames   BIGINT,
    p_fps            INTEGER,
    p_robot_type     TEXT
)
RETURNS public.datasets
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_user_exists       UUID;
    v_anchor_repo       TEXT;
    v_anchor_author     TEXT;
    v_proven_author     TEXT;
    v_existing_id       UUID;
    v_result            public.datasets;
BEGIN
    -- 1. Lock the user row: the per-user mutex (026). store_user_hf_credential
    --    (042) takes the same lock, so the proven name read in 2b cannot change
    --    under this call.
    SELECT id INTO v_user_exists
      FROM public.users
     WHERE id = p_user_id
     FOR UPDATE;

    IF v_user_exists IS NULL THEN
        RAISE EXCEPTION 'Benutzer nicht gefunden' USING ERRCODE = 'P0002';
    END IF;

    -- 2. The oldest-row anchor (026, unchanged).
    SELECT hf_repo_id INTO v_anchor_repo
      FROM public.datasets
     WHERE owner_user_id = p_user_id
     ORDER BY created_at ASC
     LIMIT 1;

    -- 2b. The proven anchor (043): the account Hugging Face reported for the
    --     student's stored token. NULL when no token is stored.
    SELECT hf_username INTO v_proven_author
      FROM public.user_hf_credentials
     WHERE user_id = p_user_id;

    IF v_anchor_repo IS NOT NULL THEN
        IF position('/' IN v_anchor_repo) > 0 THEN
            v_anchor_author := split_part(v_anchor_repo, '/', 1);
        ELSE
            v_anchor_author := v_anchor_repo;
        END IF;

        -- 3. Author mismatch -> IDOR block, unless the author is the proven
        --    account. Case-fold compare on both anchors, as 026.
        IF lower(v_anchor_author) <> lower(p_hf_author)
           AND (v_proven_author IS NULL OR lower(v_proven_author) <> lower(p_hf_author)) THEN
            RAISE EXCEPTION
                'Dieses Dataset gehört nicht zu deinem HuggingFace-Konto. Du kannst nur eigene Datasets registrieren.'
                USING ERRCODE = 'P0034';
        END IF;
    END IF;

    -- 4. Upsert by (owner_user_id, hf_repo_id) (026, unchanged).
    SELECT id INTO v_existing_id
      FROM public.datasets
     WHERE owner_user_id = p_user_id
       AND hf_repo_id    = p_hf_repo_id;

    IF v_existing_id IS NOT NULL THEN
        UPDATE public.datasets
           SET name          = p_name,
               description   = COALESCE(p_description, ''),
               episode_count = p_episode_count,
               total_frames  = p_total_frames,
               fps           = p_fps,
               robot_type    = p_robot_type,
               workgroup_id  = p_workgroup_id,
               updated_at    = NOW()
         WHERE id = v_existing_id
        RETURNING * INTO v_result;
    ELSE
        INSERT INTO public.datasets (
            owner_user_id, workgroup_id, hf_repo_id,
            name, description,
            episode_count, total_frames, fps, robot_type
        ) VALUES (
            p_user_id, p_workgroup_id, p_hf_repo_id,
            p_name, COALESCE(p_description, ''),
            p_episode_count, p_total_frames, p_fps, p_robot_type
        )
        RETURNING * INTO v_result;
    END IF;

    RETURN v_result;
END;
$$;

COMMENT ON FUNCTION public.register_dataset_safe(
    UUID, TEXT, TEXT, UUID, TEXT, TEXT, INTEGER, BIGINT, INTEGER, TEXT
) IS
  'Atomic upsert of a HF-Hub dataset registry row with row-locked HF-author '
  'anchor enforcement. The author must match the oldest dataset row''s author '
  '(026) OR the proven account name of the student''s stored token '
  '(user_hf_credentials.hf_username, 043). Serialises on SELECT id FROM users '
  'WHERE id=p_user_id FOR UPDATE. Migrations 026 + 043.';

REVOKE EXECUTE ON FUNCTION public.register_dataset_safe(
    UUID, TEXT, TEXT, UUID, TEXT, TEXT, INTEGER, BIGINT, INTEGER, TEXT
) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.register_dataset_safe(
    UUID, TEXT, TEXT, UUID, TEXT, TEXT, INTEGER, BIGINT, INTEGER, TEXT
) FROM anon;
REVOKE EXECUTE ON FUNCTION public.register_dataset_safe(
    UUID, TEXT, TEXT, UUID, TEXT, TEXT, INTEGER, BIGINT, INTEGER, TEXT
) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.register_dataset_safe(
    UUID, TEXT, TEXT, UUID, TEXT, TEXT, INTEGER, BIGINT, INTEGER, TEXT
) TO service_role;

-- Run the SECURITY DEFINER function with a bypassing-RLS owner (042 pattern):
-- it now reads user_hf_credentials, which has RLS on and no policy.
DO $$
DECLARE
  v_done BOOLEAN := FALSE;
  v_role TEXT;
BEGIN
  FOREACH v_role IN ARRAY ARRAY['postgres', 'supabase_admin', 'supabase_storage_admin'] LOOP
    BEGIN
      EXECUTE format('ALTER FUNCTION public.register_dataset_safe(UUID, TEXT, TEXT, UUID, TEXT, TEXT, INTEGER, BIGINT, INTEGER, TEXT) OWNER TO %I', v_role);
      v_done := TRUE;
      EXIT;
    EXCEPTION
      WHEN OTHERS THEN
        CONTINUE;
    END;
  END LOOP;
  IF NOT v_done THEN
    RAISE NOTICE 'Could not transfer ownership of register_dataset_safe to a privileged role.';
  END IF;
END $$;

COMMIT;

-- Rollback for 043_register_dataset_proven_anchor: restores migration 026's
-- register_dataset_safe EXACTLY (function, comment and grants are copied
-- verbatim from 20260520133426_20260520140000_026_register_dataset_safe.sql;
-- tests/test_supabase_043_045.py holds the copy byte-equal). The owner 043 set
-- stays a privileged role (026 never named one).
--
-- Effect: the proven account name stops counting as an anchor again, so a
-- student who switched Hugging Face accounts is refused (P0034 -> 403) for
-- every new dataset until the old rows are gone (docs/KNOWN-ISSUES.md).
-- No data is touched. No cloud API change depends on 043, so the order of the
-- two rollbacks does not matter.
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
    v_existing_id       UUID;
    v_result            public.datasets;
BEGIN
    -- 1. Lock the user row. This is the serialisation point — two
    --    concurrent calls for the same user wait here, one at a time.
    --    SELECT ... FOR UPDATE on users does NOT mutate the row; it's
    --    just a row-scoped advisory lock that travels with the
    --    transaction.
    SELECT id INTO v_user_exists
      FROM public.users
     WHERE id = p_user_id
     FOR UPDATE;

    IF v_user_exists IS NULL THEN
        RAISE EXCEPTION 'Benutzer nicht gefunden' USING ERRCODE = 'P0002';
    END IF;

    -- 2. Read the anchor — the HF author from the student's FIRST
    --    existing dataset row. ORDER BY created_at ASC ties our hand
    --    to a single deterministic anchor: even if a prior bad insert
    --    created two rows with different authors (the very race this
    --    migration fixes), the older row wins as the canonical anchor
    --    and the newer one(s) become unreachable by future inserts.
    --    We DO NOT consider p_hf_repo_id matches here — that's the
    --    re-register path, handled at step 4.
    SELECT hf_repo_id INTO v_anchor_repo
      FROM public.datasets
     WHERE owner_user_id = p_user_id
     ORDER BY created_at ASC
     LIMIT 1;

    IF v_anchor_repo IS NOT NULL THEN
        -- Extract author from 'owner/repo'. Pydantic validation upstream
        -- guarantees the slash form, but be defensive here in case an
        -- older row predates that validator.
        IF position('/' IN v_anchor_repo) > 0 THEN
            v_anchor_author := split_part(v_anchor_repo, '/', 1);
        ELSE
            v_anchor_author := v_anchor_repo;
        END IF;

        -- 3. Author mismatch → IDOR block. Case-fold compare: HF Hub
        --    usernames are case-insensitive on lookup but canonical
        --    form is mixed-case (Alice vs alice).
        IF lower(v_anchor_author) <> lower(p_hf_author) THEN
            RAISE EXCEPTION
                'Dieses Dataset gehört nicht zu deinem HuggingFace-Konto. Du kannst nur eigene Datasets registrieren.'
                USING ERRCODE = 'P0034';
        END IF;
    END IF;
    -- else: first-time registration, p_hf_author becomes the implicit
    -- anchor for every subsequent call. No row needs to be written to
    -- a separate anchor table — the dataset INSERT below IS the
    -- anchor capture.

    -- 4. Upsert by (owner_user_id, hf_repo_id). The UNIQUE constraint
    --    on the table makes this safe under concurrency: if a second
    --    call somehow reaches this point (e.g. two re-registers of
    --    the same repo by the same user), the second one updates the
    --    same row instead of inserting a duplicate.
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
  'anchor enforcement. Defeats the concurrent-first-register race where two '
  'POST /datasets calls from the same student could each pass the in-Python '
  'no-prior-author check and create rows with different anchor authors. '
  'Serialises on SELECT id FROM users WHERE id=p_user_id FOR UPDATE. Migration 026.';

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

COMMIT;

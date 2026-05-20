-- ============================================================================
-- 20260520140000_026_register_dataset_safe.sql
-- ----------------------------------------------------------------------------
-- Closes the trust-on-first-use HF-author anchor race identified in the
-- 2026-05 audit (MEDIUM, deferred from V1).
--
-- Problem
--   /datasets POST (cloud_training_api/app/routes/datasets.py) uses an
--   in-Python anchor check to defeat the IDOR where a student could
--   register `peer-username/peer-repo` against their own quota / group
--   pool. The check is two-step in app code:
--
--     1. SELECT hf_repo_id FROM datasets WHERE owner_user_id=$1 LIMIT 50
--        → derive the student's HF author from the first row.
--     2. If no prior rows: accept any author (first-registration trust).
--        If a prior author exists: assert match → 403 on mismatch.
--
--   Race: two concurrent /datasets POSTs from the SAME student with
--   DIFFERENT peer repos both observe step 1 returning zero rows
--   (neither has been INSERTed yet), both pass the "no prior author"
--   branch, both INSERT — the student now owns two `datasets` rows
--   with different HF authors. Subsequent registrations match whichever
--   row sorted first, the other becomes silently unreachable, and the
--   student burned credits / pool quota on a peer's data on the row
--   that did get accepted.
--
-- Fix
--   Move the whole flow into a SECURITY DEFINER RPC with row-locking on
--   public.users. Same idiom as pair_jetson / claim_pair_intent /
--   start_training_safe: SELECT ... FOR UPDATE on the caller's users row
--   serialises concurrent first-time registers from the same student.
--   Inside the locked region, we read the canonical anchor by SELECTing
--   the first prior dataset, fall back to p_hf_author on first-time,
--   compare case-fold, and INSERT/UPDATE in the same transaction.
--
--   The anchor stays derivable from datasets rows — no new column on
--   users. The lock on users(id) is just a stable per-user mutex; no
--   columns on users are written.
--
-- New error code
--   P0034  Dataset gehört nicht zu deinem HuggingFace-Konto  → HTTP 403
--
-- Idempotent — re-runnable (CREATE OR REPLACE, DROP IF EXISTS).
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- RPC: register_dataset_safe
--
-- Authoritative HF-author anchor enforcement + upsert for /datasets POST.
-- The caller (cloud_training_api, service-role) is responsible for the
-- HF-side existence check (api.dataset_info) before invoking; this RPC
-- only enforces the per-user author invariant and the upsert by
-- (owner_user_id, hf_repo_id).
--
-- Inputs
--   p_user_id        UUID    auth.users.id of the registering student
--   p_hf_repo_id     TEXT    'owner/repo' validated by Pydantic upstream
--   p_hf_author      TEXT    DatasetInfo.author from HF Hub (case-pres.)
--   p_workgroup_id   UUID    caller's current workgroup (NULL allowed)
--   p_name           TEXT
--   p_description    TEXT
--   p_episode_count  INTEGER
--   p_total_frames   BIGINT
--   p_fps            INTEGER
--   p_robot_type     TEXT
--
-- Returns the full datasets row as JSONB so the caller can serialise
-- without a second SELECT. The shape matches what supabase-py returns
-- from .table('datasets').select('*'); the route's _serialize() helper
-- consumes it directly.
--
-- Concurrency
--   The serialisable serialiser is `SELECT id FROM public.users WHERE
--   id = p_user_id FOR UPDATE`. Two concurrent invocations for the
--   same user enter the function, but only one holds the row lock at
--   a time. The second waits, reads the now-existing anchor from
--   datasets, and either confirms or raises P0034 — never lands an
--   inconsistent second author.
--
--   Two concurrent invocations for DIFFERENT users lock different rows
--   and never block each other.
--
-- Error semantics
--   P0002  → user not found        (matches existing convention)
--   P0034  → HF-author mismatch    (new this migration)
--   23505  → unique violation      (race lost on the upsert path; the
--            caller treats this as success-on-retry by re-selecting)
--
-- Security
--   SECURITY DEFINER + search_path pinned to public, pg_catalog so the
--   function body cannot be hijacked by a search_path injection from a
--   compromised role. EXECUTE revoked from PUBLIC / anon / authenticated;
--   granted to service_role only. The Modal worker uses anon + worker
--   token and never registers datasets — only the Cloud API does, and
--   the Cloud API runs as service_role.
-- ---------------------------------------------------------------------------
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

-- 021_workgroup_memberships_realtime_and_owner_check.sql
--
-- Three related fixes from the v2.3.0 follow-up audit:
--
-- 1. Add public.workgroup_memberships to the supabase_realtime
--    publication. Group-shared workflow/training/dataset visibility
--    via the audit table only updates live for sibling browsers if
--    the table is published; without this, a teacher adding a student
--    to a workgroup requires a manual page refresh on every other
--    member's browser before the new sibling's saves show up.
--
-- 2. Bake ownership / template-classroom checks into the SECURITY
--    DEFINER RPCs `update_workflow_blockly` and `restore_workflow_version`
--    so the database itself refuses a write the caller doesn't own.
--    The route layer's Python `_assert_workflow_owned` is still
--    correct as a defense-in-depth pre-check, but a future direct-RPC
--    caller (admin tool, alternate frontend) won't accidentally
--    bypass authorisation by skipping the route. P0002 keeps the
--    HTTP shape — `_resolve_visible_workgroup_ids`-style sibling
--    visibility is intentionally NOT extended to writes; only the
--    owner (or a classmate writing a template they own) may modify.
--
-- 3. Add `SET search_path = public` to
--    `public.touch_tutorial_progress_updated_at()` so the trigger
--    is robust to a non-default search_path (Supabase introspection
--    tools occasionally swap search_path under the trigger context;
--    every other trigger in this codebase pins search_path explicitly
--    and 016 was the only outlier).
--
-- Idempotent — re-runnable. The two workflow RPCs use
-- `CREATE OR REPLACE FUNCTION` with the SAME signature as migration
-- 018, so no DROP is needed. The publication-add is guarded by a DO
-- block that catches duplicate_object.

BEGIN;

-- ---------------------------------------------------------------------------
-- (1) Realtime publication: workgroup_memberships
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_publication_tables
    WHERE pubname = 'supabase_realtime'
      AND schemaname = 'public'
      AND tablename = 'workgroup_memberships'
  ) THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.workgroup_memberships;
  END IF;
EXCEPTION WHEN duplicate_object THEN
  -- Another concurrent migration already added the table — fine.
  NULL;
END $$;


-- ---------------------------------------------------------------------------
-- (2a) update_workflow_blockly with in-RPC ownership / template check
--
-- Mirrors the route-layer rule:
--   - the caller owns the workflow, OR
--   - the workflow is a classroom template AND the caller belongs to
--     that classroom (covers a teacher patching their own template
--     plus a student patching a clone they made — clones flip
--     is_template=FALSE so this branch only matches teacher writes
--     on the original template).
--
-- Group-shared workflows are intentionally NOT writable by siblings
-- here. The "sharing" semantics in this codebase are read-only:
-- a sibling can `clone` to author their own copy.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.update_workflow_blockly(
    p_workflow_id UUID,
    p_user_id UUID,
    p_blockly_json JSONB,
    p_name TEXT DEFAULT NULL,
    p_description TEXT DEFAULT NULL
)
RETURNS public.workflows
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_row public.workflows;
BEGIN
    -- Ownership / template-classroom gate. RAISE before any UPDATE so
    -- the BEFORE-UPDATE snapshot trigger never fires on an unauthorised
    -- caller (otherwise a forged caller could pollute workflow_versions
    -- even on a refused write).
    IF NOT EXISTS (
        SELECT 1
          FROM public.workflows
         WHERE id = p_workflow_id
           AND (
                owner_user_id = p_user_id
             OR (
                  is_template = TRUE
                  AND classroom_id IN (
                      SELECT classroom_id
                        FROM public.users
                       WHERE id = p_user_id
                  )
                )
           )
    ) THEN
        RAISE EXCEPTION 'Workflow nicht gefunden oder kein Zugriff.'
              USING ERRCODE = 'P0002';
    END IF;

    -- Transaction-local GUC: the BEFORE-UPDATE trigger reads this and
    -- writes it to workflow_versions.saved_by. The `true` 3rd arg
    -- scopes the setting to the current transaction so it can't leak
    -- to another call on the same pooled connection.
    PERFORM set_config('app.user_id', p_user_id::TEXT, true);

    UPDATE public.workflows
       SET blockly_json = p_blockly_json,
           name         = COALESCE(NULLIF(p_name, ''), name),
           description  = COALESCE(p_description, description)
     WHERE id = p_workflow_id
    RETURNING * INTO v_row;

    IF v_row.id IS NULL THEN
        RAISE EXCEPTION 'Workflow nicht gefunden' USING ERRCODE = 'P0002';
    END IF;

    RETURN v_row;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.update_workflow_blockly(UUID, UUID, JSONB, TEXT, TEXT) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.update_workflow_blockly(UUID, UUID, JSONB, TEXT, TEXT) FROM anon;
REVOKE EXECUTE ON FUNCTION public.update_workflow_blockly(UUID, UUID, JSONB, TEXT, TEXT) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.update_workflow_blockly(UUID, UUID, JSONB, TEXT, TEXT) TO service_role;


-- ---------------------------------------------------------------------------
-- (2b) restore_workflow_version with in-RPC ownership / template check
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.restore_workflow_version(
    p_workflow_id UUID,
    p_version_id UUID,
    p_user_id UUID
)
RETURNS public.workflows
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_row     public.workflows;
    v_payload JSONB;
BEGIN
    -- Same ownership / template-classroom rule as update_workflow_blockly.
    IF NOT EXISTS (
        SELECT 1
          FROM public.workflows
         WHERE id = p_workflow_id
           AND (
                owner_user_id = p_user_id
             OR (
                  is_template = TRUE
                  AND classroom_id IN (
                      SELECT classroom_id
                        FROM public.users
                       WHERE id = p_user_id
                  )
                )
           )
    ) THEN
        RAISE EXCEPTION 'Workflow nicht gefunden oder kein Zugriff.'
              USING ERRCODE = 'P0002';
    END IF;

    SELECT blockly_json INTO v_payload
      FROM public.workflow_versions
     WHERE id = p_version_id AND workflow_id = p_workflow_id;

    IF v_payload IS NULL THEN
        RAISE EXCEPTION 'Workflow-Version nicht gefunden' USING ERRCODE = 'P0002';
    END IF;

    PERFORM set_config('app.user_id', p_user_id::TEXT, true);

    UPDATE public.workflows
       SET blockly_json = v_payload
     WHERE id = p_workflow_id
    RETURNING * INTO v_row;

    IF v_row.id IS NULL THEN
        RAISE EXCEPTION 'Workflow nicht gefunden' USING ERRCODE = 'P0002';
    END IF;

    RETURN v_row;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.restore_workflow_version(UUID, UUID, UUID) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.restore_workflow_version(UUID, UUID, UUID) FROM anon;
REVOKE EXECUTE ON FUNCTION public.restore_workflow_version(UUID, UUID, UUID) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.restore_workflow_version(UUID, UUID, UUID) TO service_role;


-- ---------------------------------------------------------------------------
-- (3) Pin search_path on touch_tutorial_progress_updated_at()
--
-- The body is unchanged from migration 016 — only the `SET search_path`
-- option is added. `CREATE OR REPLACE FUNCTION` with the same signature
-- is the idempotent path.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.touch_tutorial_progress_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public
AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$;

COMMIT;

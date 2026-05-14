-- 018_workflow_versions_author_and_group_rls.sql
--
-- Two related fixes on the Roboter Studio version-history feature
-- (Phase-2, migration 015):
--
-- 1. Audit A1 — `workflow_versions.saved_by` was permanently NULL in
--    production because the BEFORE-UPDATE trigger reads the Postgres
--    GUC `app.user_id`, but the cloud API uses supabase-py over REST,
--    which is stateless — every call hits a different PostgREST
--    connection, so `SET LOCAL` from the caller can't reach the
--    trigger. We add a SECURITY DEFINER RPC
--    `update_workflow_blockly` that wraps `set_config('app.user_id',
--    ...)` and the UPDATE in a single transaction, so the trigger
--    sees the GUC and writes the right `saved_by`. The route layer
--    calls this RPC instead of `.table('workflows').update(...)`.
--
-- 2. Audit N1 / D3 — group-shared workflow versions were invisible to
--    sibling group members. RLS on `workflow_versions` only allowed
--    the parent-workflow owner + admins to read. The parent table's
--    own policy already lets siblings read group-shared workflows
--    (via `Group members read group workflows`); mirror that for
--    `workflow_versions` so a peer can also see the version history
--    of a group-shared template they're collaborating on.
--
-- Idempotent — re-runnable.

BEGIN;

-- ---------------------------------------------------------------------------
-- A1: update_workflow_blockly RPC — atomic SET LOCAL + UPDATE so the
-- BEFORE-UPDATE trigger's `current_setting('app.user_id', true)` read
-- resolves to the calling user's UUID.
--
-- Returns the updated row so the caller doesn't need a second SELECT.
-- Service-role-only execution; ownership / template-classroom checks
-- live in the Python route (consistent with the rest of the cloud API,
-- which treats RLS as defense-in-depth and authorises in Python).
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
-- A1 companion: restore_workflow_version RPC — same SET LOCAL trick
-- for the restore path. Without this, restoring an old version through
-- the trigger snapshots the current payload with saved_by=NULL, losing
-- the author info on the "before restore" snapshot too.
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
-- N1: extend workflow_versions read RLS to group-shared workflows so
-- siblings have the same history visibility they have for the parent.
-- The existing "Owner reads own workflow versions" policy keys on
-- workflows.owner_user_id; the new policy keys on workflows.workgroup_id
-- via the workgroup_memberships audit table, mirroring the policy on
-- public.workflows from migration 011.
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS "Group members read group workflow versions" ON public.workflow_versions;
CREATE POLICY "Group members read group workflow versions"
    ON public.workflow_versions
    FOR SELECT
    USING (
        EXISTS (
            SELECT 1
              FROM public.workflows w
              JOIN public.workgroup_memberships m
                ON m.workgroup_id = w.workgroup_id
             WHERE w.id = workflow_versions.workflow_id
               AND w.workgroup_id IS NOT NULL
               AND m.user_id = auth.uid()
        )
    );

COMMIT;

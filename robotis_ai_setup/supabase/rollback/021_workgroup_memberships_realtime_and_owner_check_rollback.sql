-- 021_workgroup_memberships_realtime_and_owner_check_rollback.sql
--
-- Reverse of 021:
--   1) Drop public.workgroup_memberships from the supabase_realtime
--      publication (guarded, no-op if it isn't there).
--   2) Restore the migration-018 bodies of update_workflow_blockly and
--      restore_workflow_version (the bodies WITHOUT the in-RPC ownership
--      gate; the Python route layer's _assert_workflow_owned is still
--      doing the check on every documented caller).
--   3) Restore the migration-016 body of touch_tutorial_progress_updated_at
--      (without the SET search_path option).

BEGIN;

-- ---------------------------------------------------------------------------
-- (1) Realtime publication: drop workgroup_memberships
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_publication_tables
    WHERE pubname = 'supabase_realtime'
      AND schemaname = 'public'
      AND tablename = 'workgroup_memberships'
  ) THEN
    ALTER PUBLICATION supabase_realtime DROP TABLE public.workgroup_memberships;
  END IF;
END $$;


-- ---------------------------------------------------------------------------
-- (2a) Restore migration-018 body of update_workflow_blockly
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
-- (2b) Restore migration-018 body of restore_workflow_version
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
-- (3) Restore migration-016 body of touch_tutorial_progress_updated_at
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.touch_tutorial_progress_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$;

COMMIT;

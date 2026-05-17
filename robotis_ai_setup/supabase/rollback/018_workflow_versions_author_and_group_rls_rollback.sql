-- 018_workflow_versions_author_and_group_rls_rollback.sql
--
-- Reverse of 018. Drops the two SECURITY DEFINER wrappers
-- (`update_workflow_blockly`, `restore_workflow_version`) and the
-- `Group members read group workflow versions` RLS policy. Leaves the
-- schema in the post-017 state.
--
-- IMPORTANT: rolling back 018 also implicitly rolls back the function
-- bodies that 021 redefined. If 021 was applied AFTER 018 and you want
-- to roll back to "post-017" you must apply this file last (i.e., 021's
-- rollback first, then this one). The plain DROP FUNCTION below removes
-- whichever body is currently installed.

BEGIN;

-- ---------------------------------------------------------------------------
-- Drop the SECURITY DEFINER wrappers added in 018.
-- ---------------------------------------------------------------------------
DROP FUNCTION IF EXISTS public.update_workflow_blockly(UUID, UUID, JSONB, TEXT, TEXT);
DROP FUNCTION IF EXISTS public.restore_workflow_version(UUID, UUID, UUID);


-- ---------------------------------------------------------------------------
-- Drop the group-sibling read policy added in 018. The owner +
-- admin read policies from 015 remain — siblings simply lose the
-- live version-history visibility, mirroring the pre-018 state.
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS "Group members read group workflow versions" ON public.workflow_versions;

COMMIT;

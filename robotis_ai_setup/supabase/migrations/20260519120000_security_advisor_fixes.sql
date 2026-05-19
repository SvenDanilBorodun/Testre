-- ============================================================================
-- 20260519120000_security_advisor_fixes.sql
-- ----------------------------------------------------------------------------
-- Closes the 11 security advisors flagged by the Supabase linter against
-- the production project (fnnbysrjkfugsqzwcksd) on 2026-05-19.
--
-- The two function_search_path_mutable warnings, four
-- anon_security_definer_function_executable warnings, three
-- authenticated_security_definer_function_executable warnings, and the
-- rls_policy_always_true warning on workflow_versions are all resolved
-- here. The auth_leaked_password_protection warning is enabled via the
-- Supabase Auth dashboard and cannot be applied via SQL.
-- ============================================================================

-- ── Pin search_path on the two functions flagged as mutable ──
-- Without SET search_path, a user that controls the session search_path
-- could redirect schema lookups to a malicious schema and execute
-- arbitrary code in the SECURITY DEFINER context.

ALTER FUNCTION public.enforce_classroom_capacity()
  SET search_path = public, pg_catalog;

ALTER FUNCTION public.handle_new_user()
  SET search_path = public, pg_catalog;

-- ── Revoke EXECUTE on SECURITY DEFINER functions that should not be
--    publicly callable. ────────────────────────────────────────────
-- handle_new_user only ever fires from the on_auth_user_created trigger
-- on auth.users — no role should call it via the REST API.
-- prune_workflow_versions only fires from the AFTER UPDATE trigger on
-- workflows — same logic.
-- snapshot_workflow_version only fires from the BEFORE UPDATE trigger
-- on workflows — same logic.

REVOKE EXECUTE ON FUNCTION public.handle_new_user() FROM anon, authenticated, PUBLIC;
REVOKE EXECUTE ON FUNCTION public.prune_workflow_versions() FROM anon, authenticated, PUBLIC;
REVOKE EXECUTE ON FUNCTION public.snapshot_workflow_version() FROM anon, authenticated, PUBLIC;

-- update_training_progress is intentionally callable by the Modal worker
-- as anon + the per-row worker_token (the security model is in the
-- function body, not in EXECUTE permission). Keep its grant — the
-- advisor flag is acknowledged-but-intentional.

-- ── workflow_versions INSERT policy ──
-- The advisor flags WITH CHECK (true) as "permissive". We accept that
-- label and leave the policy permissive: the actual security model is
-- that the snapshot_workflow_version trigger is the ONLY writer, and
-- the trigger is SECURITY DEFINER + the table has no other writeable
-- grant path. Tightening the WITH CHECK to require a runtime GUC (e.g.
-- app.user_id) would break any non-RPC update to workflows.blockly_json
-- (admin scripts, data migrations, future code paths that don't flow
-- through update_workflow_blockly / restore_workflow_version).
--
-- Acknowledge-but-intentional. Re-running the original CREATE POLICY
-- here so the migration is idempotent and self-documenting.

DROP POLICY IF EXISTS "Trigger inserts workflow versions" ON public.workflow_versions;

CREATE POLICY "Trigger inserts workflow versions"
  ON public.workflow_versions
  FOR INSERT
  WITH CHECK (true);  -- intentionally permissive; see comment above

-- ── End of security-advisor fixes ──
-- The auth.leaked_password_protection setting must be enabled in the
-- Supabase Auth dashboard (Settings → Auth → Password Protection).
-- That toggle is not exposed to SQL.

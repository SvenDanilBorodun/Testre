-- ============================================================================
-- 20260606140000_027_workflow_versions_guc_predicate.sql
-- ----------------------------------------------------------------------------
-- Closes S2 (the TODO logged in migration 024, lines 105-115): replaces the
-- permissive `WITH CHECK (true)` on the workflow_versions INSERT policy with
-- the GUC-backed predicate that 024 proposed as option (b).
--
-- Why now (preconditions verified live on production, 2026-06-06):
--   * snapshot_workflow_version() is SECURITY DEFINER and owned by `postgres`
--     (the baseline ownership-transfer DO block succeeded), the table is owned
--     by `postgres`, RLS is enabled but NOT forced, and `postgres` carries
--     rolbypassrls — so the trigger's INSERT NEVER evaluates this policy.
--   * The Cloud API runs as service_role, which bypasses RLS entirely.
--   * Both blockly_json write paths (PATCH /workflows, POST /restore) route
--     through SECURITY DEFINER RPCs (update_workflow_blockly /
--     restore_workflow_version) that `set_config('app.user_id', …, true)`
--     transaction-locally, so saved_by is populated on every legit snapshot.
--
-- What changes at runtime: ONLY direct PostgREST INSERTs by `authenticated`
-- or `anon` JWT holders. Those used to pass `WITH CHECK (true)` — a student
-- who extracted the anon key + their JWT could inject forged version-history
-- rows into ANY workflow (pollution, not overwrite — there is no UPDATE
-- policy). Under the new predicate such inserts lack the transaction-local
-- `app.user_id` GUC (only our own SQL functions set it) and are denied with
-- a standard RLS violation (42501).
--
-- The `<> ''` guard mirrors snapshot_workflow_version()'s own treatment of
-- the GUC (baseline.sql lines 1962-1987): empty string counts as absent.
--
-- Idempotent — re-runnable (DROP POLICY IF EXISTS + CREATE).
-- Rollback: rollback/20260606140000_027_workflow_versions_guc_predicate_rollback.sql
-- ============================================================================

BEGIN;

DROP POLICY IF EXISTS "Trigger inserts workflow versions" ON public.workflow_versions;
CREATE POLICY "Trigger inserts workflow versions"
  ON public.workflow_versions
  FOR INSERT
  WITH CHECK (
    current_setting('app.user_id', true) IS NOT NULL
    AND current_setting('app.user_id', true) <> ''
    AND saved_by::TEXT = current_setting('app.user_id', true)
  );

COMMENT ON POLICY "Trigger inserts workflow versions" ON public.workflow_versions IS
  'GUC-backed INSERT gate (migration 027, closes the S2 TODO from migration 024). '
  'The SECURITY DEFINER trigger (owner postgres, bypassrls) and the service-role '
  'API never evaluate this policy; it only denies direct PostgREST INSERTs that '
  'lack the transaction-local app.user_id GUC — i.e. forged version-history rows. '
  'saved_by must equal the GUC value set by update_workflow_blockly / '
  'restore_workflow_version.';

COMMIT;

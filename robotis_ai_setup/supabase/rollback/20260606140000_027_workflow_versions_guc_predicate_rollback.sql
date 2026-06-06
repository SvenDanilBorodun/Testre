-- ============================================================================
-- ROLLBACK for 20260606140000_027_workflow_versions_guc_predicate.sql
-- ----------------------------------------------------------------------------
-- Restores the permissive INSERT policy exactly as the baseline created it
-- (baseline.sql, "Belt-and-braces INSERT policy" block) — i.e. reopens the
-- S2 TODO state from migration 024. Apply only if the GUC predicate is found
-- to deny a legitimate snapshot path (none is known: trigger + service-role
-- bypass RLS; both blockly_json writers set the GUC).
-- ============================================================================

BEGIN;

DROP POLICY IF EXISTS "Trigger inserts workflow versions" ON public.workflow_versions;
CREATE POLICY "Trigger inserts workflow versions"
  ON public.workflow_versions
  FOR INSERT
  WITH CHECK (true);

COMMENT ON POLICY "Trigger inserts workflow versions" ON public.workflow_versions IS
  'Permissive INSERT gate restored by the migration-027 rollback. The S2 TODO '
  'from migration 024 (GUC-backed predicate) is OPEN again.';

COMMIT;

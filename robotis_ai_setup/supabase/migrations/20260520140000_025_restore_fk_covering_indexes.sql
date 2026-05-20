-- ============================================================================
-- 20260520140000_025_restore_fk_covering_indexes.sql
-- ----------------------------------------------------------------------------
-- Hot-fix for migration 024.
--
-- Problem
--   Migration 024 dropped 3 indexes that pg_stat_user_indexes reported as
--   "unused" (never scanned). They were however the COVERING INDEXES for
--   3 foreign keys — Postgres uses them implicitly during FK-cascade
--   lookups and constraint validation, which doesn't surface in the same
--   stats. The post-apply advisor flagged the resulting 3 unindexed FKs:
--     * users.fk_users_classroom            (was covered by idx_users_classroom)
--     * users.users_workgroup_id_fkey       (was covered by idx_users_workgroup)
--     * workflow_versions.workflow_id_fkey  (was covered by idx_workflow_versions_workflow)
--
-- Fix
--   Re-create the 3 indexes with the exact definitions from baseline.sql.
--   Also create a covering index for the new FK introduced by migration 022
--   (jetsons.intent_teacher_id → users.id) so the advisor reports 0
--   unindexed_foreign_keys.
--
-- Lesson
--   `unused_index` lints must be cross-referenced with `pg_constraint` to
--   identify FK-coverage indexes BEFORE dropping. Migration 024 did the
--   pg_class check but not the FK cross-check. Documented for future
--   advisor sweeps.
--
-- Idempotent — re-runnable.
-- ============================================================================

BEGIN;

-- Restore the FK-covering indexes that should have stayed.
CREATE INDEX IF NOT EXISTS idx_users_classroom
  ON public.users (classroom_id)
  WHERE classroom_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_users_workgroup
  ON public.users (workgroup_id)
  WHERE workgroup_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_workflow_versions_workflow
  ON public.workflow_versions (workflow_id, created_at DESC);

-- New: cover the intent_teacher_id FK added by migration 022.
CREATE INDEX IF NOT EXISTS idx_jetsons_intent_teacher_id
  ON public.jetsons (intent_teacher_id)
  WHERE intent_teacher_id IS NOT NULL;

COMMIT;

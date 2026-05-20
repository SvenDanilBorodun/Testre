-- ============================================================================
-- Rollback for 20260520140000_026_register_dataset_safe.sql
-- ----------------------------------------------------------------------------
-- Drops the register_dataset_safe RPC. After rollback, the Cloud API
-- /datasets POST route must revert to the in-Python anchor check
-- (see git history of cloud_training_api/app/routes/datasets.py before
-- the migration-026 change).
--
-- IMPORTANT: rolling back this RPC re-opens the concurrent-first-register
-- race documented in the migration header. Only run if the new RPC is
-- causing a regression and the in-Python fallback is freshly deployed.
-- ============================================================================

BEGIN;

DROP FUNCTION IF EXISTS public.register_dataset_safe(
    UUID, TEXT, TEXT, UUID, TEXT, TEXT, INTEGER, BIGINT, INTEGER, TEXT
);

COMMIT;

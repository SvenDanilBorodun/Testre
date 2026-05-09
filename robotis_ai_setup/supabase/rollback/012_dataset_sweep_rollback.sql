-- Rollback for 012_dataset_sweep.sql.
--
-- Drops the discovered_via_sweep column. Wrap in BEGIN/COMMIT so a
-- broken environment does not get left half-rolled-back.

BEGIN;

ALTER TABLE public.datasets
  DROP COLUMN IF EXISTS discovered_via_sweep;

COMMIT;

-- Rollback for migration 030 (users.hf_username).
--
-- Dropping the column un-links every student's HF identity from their cloud
-- profile. After this, dataset_sweep + POST /datasets/sync (which key on
-- hf_username) go dormant — they no-op rather than error — until the column
-- is restored. No data loss beyond the link itself (the datasets registry
-- rows are untouched; HF repos are untouched).

DROP INDEX IF EXISTS public.idx_users_hf_username;

ALTER TABLE public.users
  DROP COLUMN IF EXISTS hf_username;

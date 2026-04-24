-- Rollback for 007_deletion_requested_at.sql
-- Drops the deletion_requested_at column + its partial index. Pending
-- deletion requests are LOST — export them first if that matters.

BEGIN;

DROP INDEX IF EXISTS public.idx_users_deletion_requested_at;
ALTER TABLE public.users DROP COLUMN IF EXISTS deletion_requested_at;

COMMIT;

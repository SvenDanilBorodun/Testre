-- Rollback for 004_progress_entries.sql
-- *** WARNING: drops every daily progress note. Take a PITR snapshot first. ***

BEGIN;

-- Trigger name must match 004_progress_entries.sql exactly. IF EXISTS
-- silently no-ops on a typo. Function touch_updated_at() is shared with
-- 003 and re-created idempotently by 004, so CASCADE is safe here only
-- because 003 had been superseded before 004 shipped; noted in README.
DROP TRIGGER IF EXISTS trg_progress_entries_touch ON public.progress_entries;
DROP FUNCTION IF EXISTS public.touch_updated_at() CASCADE;
DROP TABLE IF EXISTS public.progress_entries CASCADE;

COMMIT;

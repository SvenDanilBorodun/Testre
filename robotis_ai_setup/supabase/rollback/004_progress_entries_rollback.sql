-- Rollback for 004_progress_entries.sql
-- *** WARNING: drops every daily progress note. Take a PITR snapshot first. ***

BEGIN;

DROP TRIGGER IF EXISTS touch_progress_entries ON public.progress_entries;
DROP FUNCTION IF EXISTS public.touch_updated_at() CASCADE;
DROP TABLE IF EXISTS public.progress_entries CASCADE;

COMMIT;

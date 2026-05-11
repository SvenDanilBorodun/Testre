-- ============================================================
-- Roboter Studio upgrade — combined ROLLBACK
-- Apply in this exact order: 017 → 016 → 015 (reverse of forward).
-- Each block is wrapped in BEGIN/COMMIT — safe to copy/paste into
-- Supabase SQL Editor in one go.
--
-- IMPORTANT: roll back the Railway revision that depends on these
-- tables BEFORE running this bundle, otherwise live traffic 500s.
-- ============================================================

-- ===== 017_vision_quota_rollback.sql =====
-- 017_vision_quota_rollback.sql

BEGIN;

DROP FUNCTION IF EXISTS public.refund_vision_quota(UUID);
DROP FUNCTION IF EXISTS public.reset_vision_quota_used();
DROP FUNCTION IF EXISTS public.consume_vision_quota(UUID);
ALTER TABLE public.users
  DROP CONSTRAINT IF EXISTS users_vision_used_per_term_nonneg,
  DROP COLUMN IF EXISTS vision_used_per_term,
  DROP COLUMN IF EXISTS vision_quota_per_term;

COMMIT;

-- ===== 016_tutorial_progress_rollback.sql =====
-- 016_tutorial_progress_rollback.sql
--
-- Reverse of 016.

BEGIN;

-- Drop the realtime publication entry first so the publication isn't
-- left pointing at a table we're about to delete.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_publication_tables
    WHERE pubname = 'supabase_realtime'
      AND schemaname = 'public'
      AND tablename = 'tutorial_progress'
  ) THEN
    ALTER PUBLICATION supabase_realtime DROP TABLE public.tutorial_progress;
  END IF;
END $$;

DROP POLICY IF EXISTS "Teacher reads classroom student progress" ON public.tutorial_progress;
DROP POLICY IF EXISTS "Admin reads all progress" ON public.tutorial_progress;
DROP POLICY IF EXISTS "Owner writes own progress" ON public.tutorial_progress;
DROP POLICY IF EXISTS "Owner reads own progress" ON public.tutorial_progress;
DROP TRIGGER IF EXISTS trg_tutorial_progress_touch ON public.tutorial_progress;
DROP FUNCTION IF EXISTS public.touch_tutorial_progress_updated_at();
DROP TABLE IF EXISTS public.tutorial_progress;

COMMIT;

-- ===== 015_workflow_versions_rollback.sql =====
-- 015_workflow_versions_rollback.sql
--
-- Reverse of 015. Drops the snapshot trigger first so an UPDATE on
-- workflows during the rollback doesn't try to insert into a table
-- we're about to delete.

BEGIN;

DROP TRIGGER IF EXISTS trg_workflows_snapshot ON public.workflows;
DROP TRIGGER IF EXISTS trg_workflow_versions_prune ON public.workflow_versions;
DROP FUNCTION IF EXISTS public.snapshot_workflow_version();
DROP FUNCTION IF EXISTS public.prune_workflow_versions();

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_publication_tables
    WHERE pubname = 'supabase_realtime'
      AND schemaname = 'public'
      AND tablename = 'workflow_versions'
  ) THEN
    ALTER PUBLICATION supabase_realtime DROP TABLE public.workflow_versions;
  END IF;
END $$;

DROP TABLE IF EXISTS public.workflow_versions;

COMMIT;

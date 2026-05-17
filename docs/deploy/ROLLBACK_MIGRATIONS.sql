-- ============================================================
-- EduBotics combined ROLLBACK bundle (v2.3.0 follow-up)
-- Apply in this exact order: 020 → 019 → 017 → 016 → 015
-- (reverse of forward — newer migrations rolled back first so an
--  older migration's table isn't yanked out from under a newer
--  migration's reference).
-- Each block is wrapped in BEGIN/COMMIT — safe to copy/paste into
-- Supabase SQL Editor in one go.
--
-- IMPORTANT: roll back the Railway revision that depends on these
-- tables BEFORE running this bundle, otherwise live traffic 500s.
-- ============================================================

-- ===== 020_jetson_v2_rollback.sql =====
-- Drops only the 3 RPCs the v2.3.0 follow-up added. Does NOT touch
-- migration 019's tables/RPCs/policies — those go in the next block.

BEGIN;

DROP FUNCTION IF EXISTS public.agent_release_jetson(UUID, UUID);
DROP FUNCTION IF EXISTS public.regenerate_pairing_code(UUID, UUID, TEXT, TIMESTAMPTZ);
DROP FUNCTION IF EXISTS public.unpair_jetson(UUID, UUID);

COMMIT;

-- ===== 019_classroom_jetsons_rollback.sql =====
-- Drops the entire Classroom Jetson surface (table + 7 RPCs + RLS +
-- realtime publication + trigger). 020 MUST be rolled back first so
-- no FK/dependency from 020 RPCs remains pointing at this table.

BEGIN;

-- Realtime publication first (idempotent — only drop if present).
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_publication_tables
     WHERE pubname = 'supabase_realtime'
       AND schemaname = 'public'
       AND tablename = 'jetsons'
  ) THEN
    ALTER PUBLICATION supabase_realtime DROP TABLE public.jetsons;
  END IF;
END
$$;

DROP TRIGGER IF EXISTS trg_jetsons_touch ON public.jetsons;

DROP POLICY IF EXISTS "Admins manage jetsons" ON public.jetsons;
DROP POLICY IF EXISTS "Teachers manage own classroom jetson" ON public.jetsons;
DROP POLICY IF EXISTS "Classroom members read classroom jetson" ON public.jetsons;

DROP FUNCTION IF EXISTS public.sweep_jetson_locks();
DROP FUNCTION IF EXISTS public.force_release_jetson(UUID, UUID);
DROP FUNCTION IF EXISTS public.pair_jetson(UUID, TEXT, UUID, TEXT);
DROP FUNCTION IF EXISTS public.agent_heartbeat_jetson(UUID, UUID, TEXT, TEXT);
DROP FUNCTION IF EXISTS public.heartbeat_jetson(UUID, UUID);
DROP FUNCTION IF EXISTS public.release_jetson(UUID, UUID);
DROP FUNCTION IF EXISTS public.claim_jetson(UUID, UUID);

DROP TABLE IF EXISTS public.jetsons;

-- touch_updated_at is shared across migrations — do NOT drop it here.

COMMIT;

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

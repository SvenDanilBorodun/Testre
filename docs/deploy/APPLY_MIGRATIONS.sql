-- ============================================================
-- Roboter Studio upgrade — combined Supabase migrations
-- Apply ALL of these in one go via Supabase Dashboard → SQL Editor.
-- Order matters; each block is wrapped in BEGIN/COMMIT so a single
-- mistake rolls back its own block without touching the others.
--
-- Migrations included:
--   015_workflow_versions.sql   (Verlauf history + 20-cap prune + RLS)
--   016_tutorial_progress.sql   (skillmap progress tracking)
--   017_vision_quota.sql        (per-user cloud-vision quota + atomic RPC)
-- ============================================================

-- ===== 015_workflow_versions.sql =====
-- 015_workflow_versions.sql
--
-- Roboter Studio Phase-2: server-side version history for workflows.
-- Every PATCH /workflows/{id} that changes blockly_json snapshots the
-- prior payload here so a student (or teacher) can roll back. Capped
-- at 20 versions per workflow via a trigger that prunes the oldest
-- after each insert.
--
-- Numbered 015 (skipping 013/014) because 013_revoke_anon_from_security_definer.sql
-- already exists in this directory; the prior 013_workflow_versions.sql filename
-- was a deployment hazard (alphabetic collision). Audit §C/§F renamed.

BEGIN;

CREATE TABLE IF NOT EXISTS public.workflow_versions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow_id UUID NOT NULL REFERENCES public.workflows(id) ON DELETE CASCADE,
  blockly_json JSONB NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  -- The user who saved this version (NULL when the snapshot was made
  -- by a service-role admin tool or by the BEFORE-UPDATE trigger
  -- which has no session context).
  saved_by UUID REFERENCES public.users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_workflow_versions_workflow
  ON public.workflow_versions(workflow_id, created_at DESC);

-- Cap each workflow's history at 20 versions. We take a per-workflow
-- advisory lock so two concurrent UPDATEs don't both see 20, both
-- insert, both keep 20 and leave 21-22 rows transiently. The lock is
-- xact-scoped → released at commit; deadlock-free because we hold
-- only one advisory key at a time.
CREATE OR REPLACE FUNCTION public.prune_workflow_versions()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext(NEW.workflow_id::text));
  DELETE FROM public.workflow_versions
  WHERE workflow_id = NEW.workflow_id
    AND id NOT IN (
      SELECT id FROM public.workflow_versions
      WHERE workflow_id = NEW.workflow_id
      ORDER BY created_at DESC
      LIMIT 20
    );
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_workflow_versions_prune ON public.workflow_versions;
CREATE TRIGGER trg_workflow_versions_prune
  AFTER INSERT ON public.workflow_versions
  FOR EACH ROW
  EXECUTE FUNCTION public.prune_workflow_versions();

-- Auto-snapshot on every UPDATE that changes blockly_json. Avoids a
-- second round-trip from the FastAPI side and keeps the version log
-- atomic with the parent row's mutation. Owned by postgres so the
-- INSERT inside the function bypasses RLS regardless of the calling
-- role (audit §F9).
CREATE OR REPLACE FUNCTION public.snapshot_workflow_version()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  IF NEW.blockly_json IS DISTINCT FROM OLD.blockly_json THEN
    INSERT INTO public.workflow_versions (workflow_id, blockly_json, note)
    VALUES (OLD.id, OLD.blockly_json, '');
  END IF;
  RETURN NEW;
END;
$$;

-- Ensure both SECURITY DEFINER functions run with bypassing-RLS
-- privilege. Without this, a migration applied by a non-superuser
-- would leave the trigger unable to INSERT because the table has no
-- INSERT policy (Audit §F3, §F9).
DO $$
BEGIN
  EXECUTE 'ALTER FUNCTION public.snapshot_workflow_version() OWNER TO postgres';
  EXECUTE 'ALTER FUNCTION public.prune_workflow_versions() OWNER TO postgres';
EXCEPTION
  WHEN OTHERS THEN
    -- ``postgres`` may not exist (some Supabase setups use
    -- ``supabase_admin``); fall through. Operators must verify
    -- ownership manually if both fail.
    NULL;
END $$;

DROP TRIGGER IF EXISTS trg_workflows_snapshot ON public.workflows;
CREATE TRIGGER trg_workflows_snapshot
  BEFORE UPDATE ON public.workflows
  FOR EACH ROW
  EXECUTE FUNCTION public.snapshot_workflow_version();

-- RLS policies — owner can read versions for their own workflows;
-- admins can read everything. INSERTs come from the SECURITY DEFINER
-- trigger only; service-role writes (cloud_training_api) bypass RLS.
ALTER TABLE public.workflow_versions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Owner reads own workflow versions" ON public.workflow_versions;
CREATE POLICY "Owner reads own workflow versions"
  ON public.workflow_versions
  FOR SELECT
  USING (
    EXISTS (
      SELECT 1 FROM public.workflows w
      WHERE w.id = workflow_versions.workflow_id
        AND w.owner_user_id = auth.uid()
    )
  );

DROP POLICY IF EXISTS "Admin reads all workflow versions" ON public.workflow_versions;
CREATE POLICY "Admin reads all workflow versions"
  ON public.workflow_versions
  FOR SELECT
  USING (
    EXISTS (
      SELECT 1 FROM public.users
      WHERE id = auth.uid() AND role = 'admin'
    )
  );

-- Realtime publication so the React Verlauf dropdown auto-refreshes
-- when a teammate (e.g. workgroup peer with a shared workflow) saves.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_publication_tables
    WHERE pubname = 'supabase_realtime'
      AND schemaname = 'public'
      AND tablename = 'workflow_versions'
  ) THEN
    ALTER PUBLICATION supabase_realtime ADD TABLE public.workflow_versions;
  END IF;
END $$;

COMMIT;

-- ===== 016_tutorial_progress.sql =====
-- 014_tutorial_progress.sql
--
-- Roboter Studio Phase-3: per-student progress through the bundled
-- skillmap tutorials (`physical_ai_manager/public/tutorials/*.md`).
-- Each row tracks one student × one tutorial; current_step starts at 0
-- and advances as the student completes each step.

BEGIN;

CREATE TABLE IF NOT EXISTS public.tutorial_progress (
  user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  tutorial_id TEXT NOT NULL,
  current_step INTEGER NOT NULL DEFAULT 0,
  completed_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (user_id, tutorial_id)
);

CREATE INDEX IF NOT EXISTS idx_tutorial_progress_completed
  ON public.tutorial_progress(user_id)
  WHERE completed_at IS NOT NULL;

CREATE OR REPLACE FUNCTION public.touch_tutorial_progress_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_tutorial_progress_touch ON public.tutorial_progress;
CREATE TRIGGER trg_tutorial_progress_touch
  BEFORE UPDATE ON public.tutorial_progress
  FOR EACH ROW
  EXECUTE FUNCTION public.touch_tutorial_progress_updated_at();

-- RLS: owner-only read/write; admin reads all.
ALTER TABLE public.tutorial_progress ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Owner reads own progress" ON public.tutorial_progress;
CREATE POLICY "Owner reads own progress"
  ON public.tutorial_progress
  FOR SELECT
  USING (user_id = auth.uid());

DROP POLICY IF EXISTS "Owner writes own progress" ON public.tutorial_progress;
CREATE POLICY "Owner writes own progress"
  ON public.tutorial_progress
  FOR ALL
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

DROP POLICY IF EXISTS "Admin reads all progress" ON public.tutorial_progress;
CREATE POLICY "Admin reads all progress"
  ON public.tutorial_progress
  FOR SELECT
  USING (
    EXISTS (
      SELECT 1 FROM public.users
      WHERE id = auth.uid() AND role = 'admin'
    )
  );

-- Teachers can see their own students' progress for classroom-level
-- review.
DROP POLICY IF EXISTS "Teacher reads classroom student progress" ON public.tutorial_progress;
CREATE POLICY "Teacher reads classroom student progress"
  ON public.tutorial_progress
  FOR SELECT
  USING (
    EXISTS (
      SELECT 1
      FROM public.users student
      JOIN public.classrooms c ON c.id = student.classroom_id
      WHERE student.id = tutorial_progress.user_id
        AND c.teacher_id = auth.uid()
    )
  );

COMMIT;

-- ===== 017_vision_quota.sql =====
-- 017_vision_quota.sql
--
-- Roboter Studio Phase-3: per-user cloud-vision quota.
-- The cloud_training_api `/vision/detect` endpoint forwards calls to
-- the Modal OWLv2 app. Each call costs ~$0.0001 in T4 compute but a
-- runaway workflow loop could rack up real money; this migration
-- adds two columns on ``users`` so the endpoint can short-circuit
-- after N successful calls per term.
--
-- ``vision_quota_per_term`` NULL means "unbounded"; the endpoint
-- treats NULL as a no-op so adopters with no policy preference are
-- gated only by the in-process rate limiter (5/60s/user).
-- Audit §D1 / §J7 — the endpoint reads these columns but they
-- didn't exist; the swallow-exception clause silently bypassed the
-- quota. Adding the columns wires the check up.

BEGIN;

ALTER TABLE public.users
  ADD COLUMN IF NOT EXISTS vision_quota_per_term INTEGER,
  ADD COLUMN IF NOT EXISTS vision_used_per_term INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN public.users.vision_quota_per_term IS
  'Maximum cloud-vision detect calls per term. NULL = unbounded.';
COMMENT ON COLUMN public.users.vision_used_per_term IS
  'Counter incremented by /vision/detect on every successful call.';

-- Atomic consume: returns (allowed, remaining). The UPDATE only
-- fires when there's room left so two concurrent calls can't both
-- pass the check and both increment (audit §D2). NULL quota means
-- unbounded → always allowed.
CREATE OR REPLACE FUNCTION public.consume_vision_quota(p_user_id UUID)
RETURNS TABLE(allowed BOOLEAN, remaining INTEGER)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_quota INTEGER;
  v_new_used INTEGER;
BEGIN
  SELECT vision_quota_per_term INTO v_quota
  FROM public.users
  WHERE id = p_user_id;
  IF NOT FOUND THEN
    RETURN QUERY SELECT FALSE, 0;
    RETURN;
  END IF;
  IF v_quota IS NULL THEN
    -- Unbounded; still count usage for telemetry.
    UPDATE public.users
    SET vision_used_per_term = vision_used_per_term + 1
    WHERE id = p_user_id
    RETURNING vision_used_per_term INTO v_new_used;
    RETURN QUERY SELECT TRUE, NULL::INTEGER;
    RETURN;
  END IF;
  UPDATE public.users
  SET vision_used_per_term = vision_used_per_term + 1
  WHERE id = p_user_id
    AND vision_used_per_term < v_quota
  RETURNING vision_used_per_term INTO v_new_used;
  IF v_new_used IS NULL THEN
    RETURN QUERY SELECT FALSE, 0;
  ELSE
    RETURN QUERY SELECT TRUE, GREATEST(v_quota - v_new_used, 0);
  END IF;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.consume_vision_quota(UUID) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.consume_vision_quota(UUID) FROM anon;
REVOKE EXECUTE ON FUNCTION public.consume_vision_quota(UUID) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.consume_vision_quota(UUID) TO service_role;

-- Convenience RPC that resets every student's used counter at term
-- start. Run from the admin dashboard / cron. Service-role only.
CREATE OR REPLACE FUNCTION public.reset_vision_quota_used()
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  affected INTEGER;
BEGIN
  UPDATE public.users
  SET vision_used_per_term = 0
  WHERE vision_used_per_term > 0;
  GET DIAGNOSTICS affected = ROW_COUNT;
  RETURN affected;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.reset_vision_quota_used() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.reset_vision_quota_used() FROM anon;
REVOKE EXECUTE ON FUNCTION public.reset_vision_quota_used() FROM authenticated;
GRANT EXECUTE ON FUNCTION public.reset_vision_quota_used() TO service_role;

COMMIT;

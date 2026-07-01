-- 034_workflow_trajectories.sql
--
-- Roboter Studio Batch-2b: persisted hand-guided trajectories. A student
-- torque-offs the follower, hand-guides a motion, and the ROS backend
-- streams the recorded joint samples up to the cloud API, which stores one
-- row here per named recording. A „spiele Bewegung ab <name>" block then
-- fetches the samples back (GET .../trajectories/by-name/<name>) and replays
-- them on the arm.
--
-- Mirrors the workflow_versions child-table pattern (baseline 015 / 018):
--   * FK workflow_id -> workflows ON DELETE CASCADE (a deleted workflow drops
--     its trajectories),
--   * owner_user_id -> users ON DELETE CASCADE (kept on the row directly so the
--     owner RLS predicate + IDOR is a one-hop check, unlike workflow_versions
--     which only carries workflow_id + saved_by),
--   * an AFTER-INSERT prune trigger caps history per workflow (16 here vs 20 for
--     versions) under a per-workflow advisory lock,
--   * a touch_updated_at BEFORE-UPDATE trigger (reuses the shared helper),
--   * RLS ENABLE + owner/group/template/admin SELECT policies mirroring the
--     workflow_versions read ladder (extended with the classroom-template read
--     the Python list-visibility ladder in routes/workflows.py already grants).
--
-- samples jsonb holds CONTRACT-B verbatim:
--   { "fps": 30, "points": [ [j1, j2, j3, j4, j5, grip, t_s], ... ] }
-- point_count / duration_s / fps are denormalised metadata columns for cheap
-- listing without deserialising samples.
--
-- Authorization (Rule §4 — service-role bypasses RLS; the Python asserts in
-- routes/workflows.py are the real layer): every write calls
-- `_assert_workflow_owned(user.id, workflow_id)` and sets owner_user_id /
-- workflow_id SERVER-SIDE (never from the body). The RLS policies below are
-- defence-in-depth for any anon-key read path. There is deliberately NO
-- INSERT/UPDATE/DELETE policy → anon writes are deny-by-default; only the
-- service-role API (which bypasses RLS) and the SECURITY DEFINER prune trigger
-- ever mutate the table.
--
-- NOT added to the supabase_realtime publication — replay trajectories are
-- fetched on demand by the run bar, not streamed live like the version log.
--
-- Idempotent (IF NOT EXISTS / DROP POLICY IF EXISTS) — re-runnable.

BEGIN;

CREATE TABLE IF NOT EXISTS public.workflow_trajectories (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow_id UUID NOT NULL REFERENCES public.workflows(id) ON DELETE CASCADE,
  owner_user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  -- CONTRACT-B payload: { "fps": int, "points": [[j1..j5, grip, t_s], ...] }.
  samples JSONB NOT NULL,
  -- Denormalised metadata for cheap listing (never trusted for authorization).
  point_count INTEGER,
  duration_s DOUBLE PRECISION,
  fps DOUBLE PRECISION,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE public.workflow_trajectories IS
  'Roboter Studio Batch-2b: recorded hand-guided replay trajectories '
  '(CONTRACT-B samples) for the „spiele Bewegung ab" block.';

CREATE INDEX IF NOT EXISTS idx_workflow_trajectories_workflow
  ON public.workflow_trajectories(workflow_id, created_at DESC);

-- Cap each workflow's stored trajectories at 16. Per-workflow advisory lock
-- so two concurrent INSERTs don't both see 16, both insert, and leave 17-18
-- rows transiently. The lock is xact-scoped (released at commit) and only one
-- key is held at a time → deadlock-free. SECURITY DEFINER so the DELETE runs
-- with bypass-RLS privilege even though the table has no DELETE policy (mirror
-- of prune_workflow_versions, baseline 015 §F3/§F9).
CREATE OR REPLACE FUNCTION public.prune_workflow_trajectories()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext(NEW.workflow_id::text));
  DELETE FROM public.workflow_trajectories
  WHERE workflow_id = NEW.workflow_id
    AND id NOT IN (
      SELECT id FROM public.workflow_trajectories
      WHERE workflow_id = NEW.workflow_id
      ORDER BY created_at DESC
      LIMIT 16
    );
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_workflow_trajectories_prune ON public.workflow_trajectories;
CREATE TRIGGER trg_workflow_trajectories_prune
  AFTER INSERT ON public.workflow_trajectories
  FOR EACH ROW
  EXECUTE FUNCTION public.prune_workflow_trajectories();

-- Reuse the shared touch_updated_at() helper (defined in baseline) so an
-- UPDATE (e.g. an overwrite of an existing named recording) refreshes
-- updated_at.
DROP TRIGGER IF EXISTS trg_workflow_trajectories_touch ON public.workflow_trajectories;
CREATE TRIGGER trg_workflow_trajectories_touch
  BEFORE UPDATE ON public.workflow_trajectories
  FOR EACH ROW
  EXECUTE FUNCTION public.touch_updated_at();

-- Ensure the prune SECURITY DEFINER function runs with a bypassing-RLS owner.
-- Try postgres first (self-hosted), fall through to the managed-Supabase
-- admin roles, RAISE NOTICE only if all fail (mirror of baseline 015 §AC/§AD).
DO $$
DECLARE
  v_done BOOLEAN := FALSE;
  v_role TEXT;
BEGIN
  FOREACH v_role IN ARRAY ARRAY['postgres', 'supabase_admin', 'supabase_storage_admin'] LOOP
    BEGIN
      EXECUTE format('ALTER FUNCTION public.prune_workflow_trajectories() OWNER TO %I', v_role);
      v_done := TRUE;
      EXIT;
    EXCEPTION
      WHEN OTHERS THEN
        CONTINUE;
    END;
  END LOOP;
  IF NOT v_done THEN
    RAISE NOTICE 'Could not transfer ownership of prune_workflow_trajectories() '
      'to a privileged role. The prune trigger may fail under RLS — '
      'see the audit notes in 015_workflow_versions.sql.';
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- RLS — service-role (the FastAPI cloud API) bypasses these; anon-key reads
-- are gated by the SELECT policies below. NO write policy → anon writes denied
-- by default. Mirrors the workflow_versions read ladder + the classroom-
-- template read the Python visibility ladder grants.
-- ---------------------------------------------------------------------------
ALTER TABLE public.workflow_trajectories ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Owner reads own workflow trajectories" ON public.workflow_trajectories;
CREATE POLICY "Owner reads own workflow trajectories"
  ON public.workflow_trajectories
  FOR SELECT
  USING (owner_user_id = (SELECT auth.uid()));

DROP POLICY IF EXISTS "Group members read group workflow trajectories" ON public.workflow_trajectories;
CREATE POLICY "Group members read group workflow trajectories"
  ON public.workflow_trajectories
  FOR SELECT
  USING (
    EXISTS (
      SELECT 1
        FROM public.workflows w
        JOIN public.workgroup_memberships m
          ON m.workgroup_id = w.workgroup_id
       WHERE w.id = workflow_trajectories.workflow_id
         AND w.workgroup_id IS NOT NULL
         AND m.user_id = (SELECT auth.uid())
    )
  );

DROP POLICY IF EXISTS "Classroom members read template trajectories" ON public.workflow_trajectories;
CREATE POLICY "Classroom members read template trajectories"
  ON public.workflow_trajectories
  FOR SELECT
  USING (
    EXISTS (
      SELECT 1
        FROM public.workflows w
       WHERE w.id = workflow_trajectories.workflow_id
         AND w.is_template = TRUE
         AND w.classroom_id = (
           SELECT classroom_id FROM public.users WHERE id = (SELECT auth.uid())
         )
    )
  );

DROP POLICY IF EXISTS "Admin reads all workflow trajectories" ON public.workflow_trajectories;
CREATE POLICY "Admin reads all workflow trajectories"
  ON public.workflow_trajectories
  FOR SELECT
  USING (
    EXISTS (
      SELECT 1 FROM public.users
      WHERE id = (SELECT auth.uid()) AND role = 'admin'
    )
  );

COMMIT;

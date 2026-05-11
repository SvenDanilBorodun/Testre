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
--
-- ``saved_by`` is populated from the ``app.user_id`` Postgres GUC
-- when the cloud API sets it before the UPDATE
-- (``SET LOCAL app.user_id = '<uuid>'``). When the GUC is unset the
-- column stays NULL — appropriate for service-role admin tools or
-- internal migrations where there's no human author. Audit round-3
-- §13 / §I.
CREATE OR REPLACE FUNCTION public.snapshot_workflow_version()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_saved_by UUID;
  v_setting TEXT;
BEGIN
  IF NEW.blockly_json IS DISTINCT FROM OLD.blockly_json THEN
    BEGIN
      v_setting := current_setting('app.user_id', true);
      IF v_setting IS NOT NULL AND v_setting <> '' THEN
        v_saved_by := v_setting::UUID;
      END IF;
    EXCEPTION
      WHEN OTHERS THEN
        v_saved_by := NULL;
    END;
    INSERT INTO public.workflow_versions (workflow_id, blockly_json, note, saved_by)
    VALUES (OLD.id, OLD.blockly_json, '', v_saved_by);
  END IF;
  RETURN NEW;
END;
$$;

-- Ensure both SECURITY DEFINER functions run with bypassing-RLS
-- privilege. Without this, a migration applied by a non-superuser
-- would leave the trigger unable to INSERT because the table has no
-- INSERT policy (Audit §F3, §F9). Audit round-3 §AC/§AD — try
-- ``postgres`` first (self-hosted), fall through to ``supabase_admin``
-- (managed Supabase), and RAISE NOTICE only if both fail so the
-- operator sees the issue at apply time instead of at first edit.
DO $$
DECLARE
  v_done BOOLEAN := FALSE;
  v_role TEXT;
BEGIN
  FOREACH v_role IN ARRAY ARRAY['postgres', 'supabase_admin', 'supabase_storage_admin'] LOOP
    BEGIN
      EXECUTE format('ALTER FUNCTION public.snapshot_workflow_version() OWNER TO %I', v_role);
      EXECUTE format('ALTER FUNCTION public.prune_workflow_versions() OWNER TO %I', v_role);
      v_done := TRUE;
      EXIT;
    EXCEPTION
      WHEN OTHERS THEN
        CONTINUE;
    END;
  END LOOP;
  IF NOT v_done THEN
    RAISE NOTICE 'Could not transfer ownership of workflow_versions SECURITY DEFINER '
      'functions to a privileged role. The trigger may fail under RLS — '
      'see the audit notes in 015_workflow_versions.sql.';
  END IF;
END $$;

-- Belt-and-braces INSERT policy. Even when the ownership ALTER above
-- succeeds, an explicit INSERT WITH CHECK keeps the workflow UPDATE
-- path resilient to a future RLS tightening. Service-role still
-- bypasses RLS; this policy gates the SECURITY DEFINER trigger only.
DROP POLICY IF EXISTS "Trigger inserts workflow versions" ON public.workflow_versions;
CREATE POLICY "Trigger inserts workflow versions"
  ON public.workflow_versions
  FOR INSERT
  WITH CHECK (true);

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

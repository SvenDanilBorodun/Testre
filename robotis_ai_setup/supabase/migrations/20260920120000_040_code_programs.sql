-- 040 — Roboter Studio code programs (Python / Java) + „Abgeben".
--
-- Proven on a fresh local stack before it shipped (`supabase db reset --local`
-- on a scratch project copy, then supabase/tests/040_code_programs_assertions.sql
-- -> 16 PASS; rollback, re-apply twice, assertions again -> 16 PASS).
-- Rollback: rollback/20260920120000_040_code_programs_rollback.sql
--
-- NUMBERING: 037 stays deliberately skipped (reserved on disk for „Eigene
-- Objekte" in docs/plans/eigene-objekte-sql/); 038 + 039 precede this file.
-- Timestamp 20260920120000 > 20260905120000 (039), so `supabase db push`
-- orders it last.
--
-- WHAT CHANGES
--   1. workflows.code_language ('' | 'python' | 'java') and
--      workflows.code_files (JSONB object: path -> source), with SQL caps:
--      an object, <= 32 files, <= 64 KiB per file, <= 160 KiB rendered
--      (the Python validator enforces the product caps — 128 KiB project on the
--      json.dumps(ensure_ascii=False) measure, 64 KiB per file, 32 files — these
--      CHECKs are the floor a direct PostgREST write cannot get under).
--   2. Version history for code (decision B8): workflow_versions gains
--      code_files / code_language, and snapshot_workflow_version() fires when
--      EITHER blockly_json OR the code changed, snapshotting all three.
--      restore_workflow_version() restores code when the version carries it.
--   3. update_workflow_code(): the SECURITY DEFINER writer for code, owner-only,
--      setting the app.user_id GUC so saved_by is stamped (mirror of
--      update_workflow_blockly, minus the template-classroom rule).
--   4. workflow_submissions: immutable „Abgeben" snapshots, SQL-capped at 8 per
--      (student, workflow) by an AFTER INSERT prune trigger under a per-pair
--      advisory lock (the workflow_trajectories pattern), classroom_id stamped
--      SERVER-SIDE at submit time, RLS with owner/admin SELECT only.
--
-- Idempotent — re-runnable.

BEGIN;

-- ---------------------------------------------------------------------------
-- 0. helpers usable inside CHECK constraints (set-returning functions may not
--    appear in a CHECK expression directly, but may inside an IMMUTABLE function)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.jsonb_object_key_count(j JSONB)
RETURNS INTEGER
LANGUAGE sql
IMMUTABLE STRICT
SET search_path = public
AS $$
  SELECT CASE WHEN jsonb_typeof(j) = 'object'
              THEN (SELECT count(*)::int FROM jsonb_object_keys(j))
              ELSE 0 END
$$;

CREATE OR REPLACE FUNCTION public.jsonb_max_text_value_bytes(j JSONB)
RETURNS INTEGER
LANGUAGE sql
IMMUTABLE STRICT
SET search_path = public
AS $$
  SELECT CASE WHEN jsonb_typeof(j) = 'object'
              THEN COALESCE((SELECT max(octet_length(v)) FROM jsonb_each_text(j) AS e(k, v)), 0)
              ELSE 0 END
$$;

CREATE OR REPLACE FUNCTION public.jsonb_all_values_are_strings(j JSONB)
RETURNS BOOLEAN
LANGUAGE sql
IMMUTABLE STRICT
SET search_path = public
AS $$
  SELECT CASE WHEN jsonb_typeof(j) = 'object'
              THEN COALESCE((SELECT bool_and(jsonb_typeof(v) = 'string') FROM jsonb_each(j) AS e(k, v)), TRUE)
              ELSE FALSE END
$$;

-- ---------------------------------------------------------------------------
-- 1. workflows: the code document
-- ---------------------------------------------------------------------------
ALTER TABLE public.workflows
  ADD COLUMN IF NOT EXISTS code_language TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS code_files JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN public.workflows.code_language IS
  'Roboter Studio code programs: '''' (a Blockly program) | ''python'' | ''java''. One workflow is one language.';
COMMENT ON COLUMN public.workflows.code_files IS
  'Roboter Studio code programs: JSONB object path -> source text (the student''s project files).';

ALTER TABLE public.workflows DROP CONSTRAINT IF EXISTS workflows_code_language_known;
ALTER TABLE public.workflows ADD CONSTRAINT workflows_code_language_known
  CHECK (code_language IN ('', 'python', 'java'));

ALTER TABLE public.workflows DROP CONSTRAINT IF EXISTS workflows_code_files_shape;
ALTER TABLE public.workflows ADD CONSTRAINT workflows_code_files_shape
  CHECK (jsonb_typeof(code_files) = 'object'
         AND public.jsonb_all_values_are_strings(code_files)
         AND public.jsonb_object_key_count(code_files) <= 32
         AND public.jsonb_max_text_value_bytes(code_files) <= 65536
         AND octet_length(code_files::text) <= 163840);

-- ---------------------------------------------------------------------------
-- 2. version history for code (B8)
-- ---------------------------------------------------------------------------
ALTER TABLE public.workflow_versions
  ADD COLUMN IF NOT EXISTS code_files JSONB,
  ADD COLUMN IF NOT EXISTS code_language TEXT;

COMMENT ON COLUMN public.workflow_versions.code_files IS
  'Snapshot of workflows.code_files at save time (NULL on versions written before migration 040).';

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
  IF NEW.blockly_json IS DISTINCT FROM OLD.blockly_json
     OR NEW.code_files IS DISTINCT FROM OLD.code_files
     OR NEW.code_language IS DISTINCT FROM OLD.code_language THEN
    BEGIN
      v_setting := current_setting('app.user_id', true);
      IF v_setting IS NOT NULL AND v_setting <> '' THEN
        v_saved_by := v_setting::UUID;
      END IF;
    EXCEPTION
      WHEN OTHERS THEN
        v_saved_by := NULL;
    END;
    INSERT INTO public.workflow_versions
      (workflow_id, blockly_json, note, saved_by, code_files, code_language)
    VALUES (OLD.id, OLD.blockly_json, '', v_saved_by, OLD.code_files, OLD.code_language);
  END IF;
  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.restore_workflow_version(
    p_workflow_id UUID,
    p_version_id UUID,
    p_user_id UUID
)
RETURNS public.workflows
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_row     public.workflows;
    v_payload JSONB;
    v_files   JSONB;
    v_lang    TEXT;
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM public.workflows
         WHERE id = p_workflow_id
           AND (
                owner_user_id = p_user_id
             OR (
                  is_template = TRUE
                  AND classroom_id IN (
                      SELECT classroom_id
                        FROM public.users
                       WHERE id = p_user_id
                  )
                )
           )
    ) THEN
        RAISE EXCEPTION 'Workflow nicht gefunden oder kein Zugriff.'
              USING ERRCODE = 'P0002';
    END IF;

    SELECT blockly_json, code_files, code_language
      INTO v_payload, v_files, v_lang
      FROM public.workflow_versions
     WHERE id = p_version_id AND workflow_id = p_workflow_id;

    IF v_payload IS NULL THEN
        RAISE EXCEPTION 'Workflow-Version nicht gefunden' USING ERRCODE = 'P0002';
    END IF;

    PERFORM set_config('app.user_id', p_user_id::TEXT, true);

    -- A version written before 040 carries NULL code columns: leave the code
    -- untouched then, restore it otherwise (a code version restores both halves).
    UPDATE public.workflows
       SET blockly_json  = v_payload,
           code_files    = COALESCE(v_files, code_files),
           code_language = COALESCE(v_lang, code_language)
     WHERE id = p_workflow_id
    RETURNING * INTO v_row;

    IF v_row.id IS NULL THEN
        RAISE EXCEPTION 'Workflow nicht gefunden' USING ERRCODE = 'P0002';
    END IF;

    RETURN v_row;
END;
$$;

-- ---------------------------------------------------------------------------
-- 3. the code writer RPC (owner-only, GUC-stamped)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.update_workflow_code(
    p_workflow_id UUID,
    p_user_id UUID,
    p_code_files JSONB,
    p_code_language TEXT
)
RETURNS public.workflows
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_row public.workflows;
BEGIN
    IF p_code_language IS NULL OR p_code_language NOT IN ('python', 'java') THEN
        RAISE EXCEPTION 'Unbekannte Programmiersprache.' USING ERRCODE = '22023';
    END IF;
    IF p_code_files IS NULL OR jsonb_typeof(p_code_files) <> 'object' THEN
        RAISE EXCEPTION 'Programmdateien haben ein ungültiges Format.' USING ERRCODE = '22023';
    END IF;
    -- OWNER ONLY. Deliberately not update_workflow_blockly's template-classroom
    -- rule: code is the student's own artifact, never a classroom template edit.
    IF NOT EXISTS (
        SELECT 1 FROM public.workflows
         WHERE id = p_workflow_id AND owner_user_id = p_user_id
    ) THEN
        RAISE EXCEPTION 'Workflow nicht gefunden oder kein Zugriff.'
              USING ERRCODE = 'P0002';
    END IF;

    PERFORM set_config('app.user_id', p_user_id::TEXT, true);

    UPDATE public.workflows
       SET code_files    = p_code_files,
           code_language = p_code_language
     WHERE id = p_workflow_id
    RETURNING * INTO v_row;

    IF v_row.id IS NULL THEN
        RAISE EXCEPTION 'Workflow nicht gefunden' USING ERRCODE = 'P0002';
    END IF;
    RETURN v_row;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.update_workflow_code(UUID, UUID, JSONB, TEXT) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.update_workflow_code(UUID, UUID, JSONB, TEXT) FROM anon;
REVOKE EXECUTE ON FUNCTION public.update_workflow_code(UUID, UUID, JSONB, TEXT) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.update_workflow_code(UUID, UUID, JSONB, TEXT) TO service_role;

-- ---------------------------------------------------------------------------
-- 4. „Abgeben": immutable submissions, capped per (student, workflow)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.workflow_submissions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow_id UUID NOT NULL REFERENCES public.workflows(id) ON DELETE CASCADE,
  student_user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  -- Stamped SERVER-SIDE from users.classroom_id at submit time (never from the
  -- body, never from workflows.classroom_id, which a client can set).
  classroom_id UUID REFERENCES public.classrooms(id) ON DELETE SET NULL,
  name TEXT NOT NULL,
  code_language TEXT NOT NULL DEFAULT '',
  code_files JSONB NOT NULL DEFAULT '{}'::jsonb,
  blockly_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  sim_scene JSONB NOT NULL DEFAULT '{}'::jsonb,
  note TEXT NOT NULL DEFAULT '',
  -- clock_timestamp(), not NOW(): NOW() is frozen for a whole transaction, so
  -- two submissions written in one transaction would tie and the prune below
  -- would keep an arbitrary 8 (measured on the local stack). Wall-clock order
  -- is the order a teacher expects anyway.
  submitted_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT workflow_submissions_code_language_known
    CHECK (code_language IN ('', 'python', 'java')),
  CONSTRAINT workflow_submissions_code_files_shape
    CHECK (jsonb_typeof(code_files) = 'object'
           AND public.jsonb_all_values_are_strings(code_files)
           AND public.jsonb_object_key_count(code_files) <= 32
           AND public.jsonb_max_text_value_bytes(code_files) <= 65536
           AND octet_length(code_files::text) <= 163840),
  CONSTRAINT workflow_submissions_note_len CHECK (char_length(note) <= 500)
);

COMMENT ON TABLE public.workflow_submissions IS
  'Roboter Studio „Abgeben": immutable snapshots a student deliberately submitted for the teacher.';

CREATE INDEX IF NOT EXISTS idx_workflow_submissions_student
  ON public.workflow_submissions(student_user_id, submitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_workflow_submissions_workflow
  ON public.workflow_submissions(workflow_id, submitted_at DESC);

-- Immutable rows: every UPDATE is refused, whatever role runs it (a trigger
-- binds the service-role writer too, unlike RLS).
CREATE OR REPLACE FUNCTION public.workflow_submissions_refuse_update()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public
AS $$
BEGIN
  RAISE EXCEPTION 'Abgaben können nicht verändert werden.' USING ERRCODE = 'P0001';
END;
$$;

DROP TRIGGER IF EXISTS trg_workflow_submissions_immutable ON public.workflow_submissions;
CREATE TRIGGER trg_workflow_submissions_immutable
  BEFORE UPDATE ON public.workflow_submissions
  FOR EACH ROW
  EXECUTE FUNCTION public.workflow_submissions_refuse_update();

-- Cap: 8 submissions per (student, workflow), newest kept. Per-pair advisory
-- lock, xact-scoped, one key at a time -> deadlock-free (034's pattern).
CREATE OR REPLACE FUNCTION public.prune_workflow_submissions()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext(NEW.student_user_id::text || ':' || NEW.workflow_id::text));
  DELETE FROM public.workflow_submissions
  WHERE student_user_id = NEW.student_user_id
    AND workflow_id = NEW.workflow_id
    AND id NOT IN (
      SELECT id FROM public.workflow_submissions
      WHERE student_user_id = NEW.student_user_id
        AND workflow_id = NEW.workflow_id
      ORDER BY submitted_at DESC, id DESC
      LIMIT 8
    );
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_workflow_submissions_prune ON public.workflow_submissions;
CREATE TRIGGER trg_workflow_submissions_prune
  AFTER INSERT ON public.workflow_submissions
  FOR EACH ROW
  EXECUTE FUNCTION public.prune_workflow_submissions();

-- Ensure the SECURITY DEFINER functions run with a bypassing-RLS owner
-- (baseline 015 §AC/§AD pattern; CREATE OR REPLACE keeps the existing owner
-- for the two re-defined functions, re-asserted here anyway).
DO $$
DECLARE
  v_done BOOLEAN := FALSE;
  v_role TEXT;
BEGIN
  FOREACH v_role IN ARRAY ARRAY['postgres', 'supabase_admin', 'supabase_storage_admin'] LOOP
    BEGIN
      EXECUTE format('ALTER FUNCTION public.prune_workflow_submissions() OWNER TO %I', v_role);
      EXECUTE format('ALTER FUNCTION public.snapshot_workflow_version() OWNER TO %I', v_role);
      EXECUTE format('ALTER FUNCTION public.restore_workflow_version(UUID, UUID, UUID) OWNER TO %I', v_role);
      EXECUTE format('ALTER FUNCTION public.update_workflow_code(UUID, UUID, JSONB, TEXT) OWNER TO %I', v_role);
      v_done := TRUE;
      EXIT;
    EXCEPTION
      WHEN OTHERS THEN
        CONTINUE;
    END;
  END LOOP;
  IF NOT v_done THEN
    RAISE NOTICE 'Could not transfer ownership of the 040 SECURITY DEFINER functions to a privileged role.';
  END IF;
END $$;

ALTER TABLE public.workflow_submissions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Student reads own submissions" ON public.workflow_submissions;
CREATE POLICY "Student reads own submissions"
  ON public.workflow_submissions
  FOR SELECT
  USING (student_user_id = (SELECT auth.uid()));

DROP POLICY IF EXISTS "Admin reads all submissions" ON public.workflow_submissions;
CREATE POLICY "Admin reads all submissions"
  ON public.workflow_submissions
  FOR SELECT
  USING (
    EXISTS (
      SELECT 1 FROM public.users
      WHERE id = (SELECT auth.uid()) AND role = 'admin'
    )
  );
-- No INSERT/UPDATE/DELETE policy: anon/authenticated writes are denied by
-- default; only the service-role API (which sets student_user_id + classroom_id
-- server-side) and the SECURITY DEFINER prune ever mutate the table.

COMMIT;

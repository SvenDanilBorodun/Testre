-- 041 — a code program (Python / Java) keeps its Ziele and Positionen.
--
-- Proven on a fresh local stack before it shipped (`supabase db reset --local`
-- on a scratch project copy, then supabase/tests/041_code_destinations_assertions.sql
-- -> 21 PASS; rollback, re-apply twice, assertions again -> 21 PASS).
-- Rollback: rollback/20260927120000_041_code_destinations_rollback.sql
--
-- NUMBERING: 037 stays deliberately skipped (reserved on disk for „Eigene
-- Objekte" in docs/plans/eigene-objekte-sql/). Timestamp 20260927120000 >
-- 20260920120000 (040), so `supabase db push` orders it last.
--
-- WHY
--   A Blockly program keeps its Ziele / Positionen as the workspace serializer
--   `edubotics-destinations` inside blockly_json (React
--   sammlung/destinationStore.js). A code program (040) had nowhere to keep
--   them: the cloud API refused every blockly_json on a code row. Owner
--   decision O1 stores them in the SAME key on the SAME column, so version
--   history, restore, clone and „Abgeben" — which already copy blockly_json —
--   carry them for free.
--
-- WHAT CHANGES
--   1. update_workflow_code() gains a 5th argument p_blockly_json JSONB
--      DEFAULT NULL, written in the SAME UPDATE as the code, so one save is one
--      version snapshot (snapshot_workflow_version fires once per UPDATE).
--      NULL = leave blockly_json as it is (an older API that calls with the
--      four named 040 arguments resolves through the default and never
--      touches the Ziele). A non-object, or an object with any key besides
--      `edubotics-destinations`, is refused with 22023.
--      The 4-argument function is DROPPED, not overloaded: two signatures
--      under one name would make PostgREST's (name, arg-name set) resolution
--      ambiguous for the 4-argument call.
--   2. CHECK workflows_code_blockly_json_destinations_only (owner decision O2):
--      a code row's blockly_json is an object whose only possible key is
--      `edubotics-destinations`. A Blockly row (code_language '') is
--      unconstrained. Added NOT VALID, then VALIDATEd: every code row written
--      through the 040 API carries '{}' (the API refused anything else), so
--      validation passes; a row that does not would abort this migration
--      loudly rather than ship a constraint that does not hold.
--      The CASE keeps `jsonb - text` away from a non-object (it raises on a
--      scalar) whatever order the executor evaluates the branches in.
--
-- Idempotent — re-runnable.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. the code writer RPC, now also writing the Ziele (owner-only, GUC-stamped)
-- ---------------------------------------------------------------------------
DROP FUNCTION IF EXISTS public.update_workflow_code(UUID, UUID, JSONB, TEXT);

CREATE OR REPLACE FUNCTION public.update_workflow_code(
    p_workflow_id UUID,
    p_user_id UUID,
    p_code_files JSONB,
    p_code_language TEXT,
    p_blockly_json JSONB DEFAULT NULL
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
    -- A code program's blockly_json carries its Ziele and Positionen and
    -- nothing else (one workflow is one language). Two nested checks, never
    -- one OR: `jsonb - text` raises on a scalar.
    IF p_blockly_json IS NOT NULL THEN
        IF jsonb_typeof(p_blockly_json) <> 'object' THEN
            RAISE EXCEPTION 'Ein Code-Programm speichert neben dem Code nur seine Ziele und Positionen.'
                  USING ERRCODE = '22023';
        END IF;
        IF (p_blockly_json - 'edubotics-destinations') <> '{}'::jsonb THEN
            RAISE EXCEPTION 'Ein Code-Programm speichert neben dem Code nur seine Ziele und Positionen.'
                  USING ERRCODE = '22023';
        END IF;
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

    -- ONE UPDATE for code and Ziele: the BEFORE UPDATE snapshot trigger fires
    -- once, so one save is one version.
    UPDATE public.workflows
       SET code_files    = p_code_files,
           code_language = p_code_language,
           blockly_json  = COALESCE(p_blockly_json, blockly_json)
     WHERE id = p_workflow_id
    RETURNING * INTO v_row;

    IF v_row.id IS NULL THEN
        RAISE EXCEPTION 'Workflow nicht gefunden' USING ERRCODE = 'P0002';
    END IF;
    RETURN v_row;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.update_workflow_code(UUID, UUID, JSONB, TEXT, JSONB) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.update_workflow_code(UUID, UUID, JSONB, TEXT, JSONB) FROM anon;
REVOKE EXECUTE ON FUNCTION public.update_workflow_code(UUID, UUID, JSONB, TEXT, JSONB) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.update_workflow_code(UUID, UUID, JSONB, TEXT, JSONB) TO service_role;

-- Run the SECURITY DEFINER writer with a bypassing-RLS owner (040's pattern).
DO $$
DECLARE
  v_done BOOLEAN := FALSE;
  v_role TEXT;
BEGIN
  FOREACH v_role IN ARRAY ARRAY['postgres', 'supabase_admin', 'supabase_storage_admin'] LOOP
    BEGIN
      EXECUTE format('ALTER FUNCTION public.update_workflow_code(UUID, UUID, JSONB, TEXT, JSONB) OWNER TO %I', v_role);
      v_done := TRUE;
      EXIT;
    EXCEPTION
      WHEN OTHERS THEN
        CONTINUE;
    END;
  END LOOP;
  IF NOT v_done THEN
    RAISE NOTICE 'Could not transfer ownership of update_workflow_code to a privileged role.';
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. a code row's blockly_json holds only its Ziele (O2)
-- ---------------------------------------------------------------------------
ALTER TABLE public.workflows DROP CONSTRAINT IF EXISTS workflows_code_blockly_json_destinations_only;
ALTER TABLE public.workflows ADD CONSTRAINT workflows_code_blockly_json_destinations_only
  CHECK (
    CASE
      WHEN code_language = '' THEN TRUE
      WHEN jsonb_typeof(blockly_json) <> 'object' THEN FALSE
      ELSE (blockly_json - 'edubotics-destinations') = '{}'::jsonb
    END
  ) NOT VALID;
ALTER TABLE public.workflows VALIDATE CONSTRAINT workflows_code_blockly_json_destinations_only;

COMMENT ON CONSTRAINT workflows_code_blockly_json_destinations_only ON public.workflows IS
  'Roboter Studio (041): a code program''s blockly_json carries only its Ziele/Positionen (key edubotics-destinations).';

COMMIT;

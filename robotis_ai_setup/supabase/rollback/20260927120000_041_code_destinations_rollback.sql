-- Rollback for 041_code_destinations. Restores 040's 4-argument
-- update_workflow_code() body verbatim and drops the code-row CHECK.
--
-- DATA: nothing is deleted. Ziele a code program saved under 041 stay in its
-- blockly_json['edubotics-destinations']; the 040 API and the 040 writer never
-- read or write that key on a code row, so they are inert until 041 returns.
-- Roll the cloud API back FIRST (the 041 API calls the 5-argument signature
-- this file drops).
BEGIN;

ALTER TABLE public.workflows DROP CONSTRAINT IF EXISTS workflows_code_blockly_json_destinations_only;

DROP FUNCTION IF EXISTS public.update_workflow_code(UUID, UUID, JSONB, TEXT, JSONB);

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

DO $$
DECLARE
  v_done BOOLEAN := FALSE;
  v_role TEXT;
BEGIN
  FOREACH v_role IN ARRAY ARRAY['postgres', 'supabase_admin', 'supabase_storage_admin'] LOOP
    BEGIN
      EXECUTE format('ALTER FUNCTION public.update_workflow_code(UUID, UUID, JSONB, TEXT) OWNER TO %I', v_role);
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

COMMIT;

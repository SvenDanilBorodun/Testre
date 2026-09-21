-- Rollback for 040_code_programs. Restores the 018/baseline bodies of
-- snapshot_workflow_version() and restore_workflow_version(), drops the code
-- writer RPC, the submissions table, the code columns and the CHECK helpers.
-- Data in workflows.code_files / workflow_versions.code_files is LOST by the
-- column drops (that is what a rollback of a data-carrying column means).
BEGIN;

DROP TABLE IF EXISTS public.workflow_submissions;
DROP FUNCTION IF EXISTS public.prune_workflow_submissions();
DROP FUNCTION IF EXISTS public.workflow_submissions_refuse_update();
DROP FUNCTION IF EXISTS public.update_workflow_code(UUID, UUID, JSONB, TEXT);

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
BEGIN
    -- Same ownership / template-classroom rule as update_workflow_blockly.
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

    SELECT blockly_json INTO v_payload
      FROM public.workflow_versions
     WHERE id = p_version_id AND workflow_id = p_workflow_id;

    IF v_payload IS NULL THEN
        RAISE EXCEPTION 'Workflow-Version nicht gefunden' USING ERRCODE = 'P0002';
    END IF;

    PERFORM set_config('app.user_id', p_user_id::TEXT, true);

    UPDATE public.workflows
       SET blockly_json = v_payload
     WHERE id = p_workflow_id
    RETURNING * INTO v_row;

    IF v_row.id IS NULL THEN
        RAISE EXCEPTION 'Workflow nicht gefunden' USING ERRCODE = 'P0002';
    END IF;

    RETURN v_row;
END;
$$;

ALTER TABLE public.workflow_versions
  DROP COLUMN IF EXISTS code_files,
  DROP COLUMN IF EXISTS code_language;

ALTER TABLE public.workflows DROP CONSTRAINT IF EXISTS workflows_code_files_shape;
ALTER TABLE public.workflows DROP CONSTRAINT IF EXISTS workflows_code_language_known;
ALTER TABLE public.workflows
  DROP COLUMN IF EXISTS code_files,
  DROP COLUMN IF EXISTS code_language;

DROP FUNCTION IF EXISTS public.jsonb_all_values_are_strings(JSONB);
DROP FUNCTION IF EXISTS public.jsonb_max_text_value_bytes(JSONB);
DROP FUNCTION IF EXISTS public.jsonb_object_key_count(JSONB);

COMMIT;

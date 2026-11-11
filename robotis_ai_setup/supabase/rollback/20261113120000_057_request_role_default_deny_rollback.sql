-- Rollback of 057 — request roles default-deny.
--
-- THIS RE-OPENS THE HOLE 057 CLOSED (security audit SA-10): anon and
-- authenticated may SELECT every column of thirteen public tables again (RLS is
-- then again the only fence for reads), INSERT, UPDATE, DELETE, TRUNCATE,
-- REFERENCES and TRIGGER on nine of them, USAGE/SELECT/UPDATE (setval) on two
-- sequences and EXECUTE on eleven trigger/helper functions, and every table,
-- sequence and function a later migration creates is again granted to both
-- request roles (and every new function to PUBLIC) by default. Roll back only to
-- unblock a deploy, and apply 057 again as soon as you can.
--
-- Restores exactly what 20261113120000_057_request_role_default_deny.sql
-- changed, as 055 left it:
--   * table privileges of anon and authenticated: SELECT on classrooms,
--     datasets, edu6_arm_records, inference_runs, progress_entries,
--     tutorial_progress, users, workflow_submissions, workflow_trajectories,
--     workflow_versions, workflows, workgroup_memberships and workgroups, plus
--     the six write privileges on the nine of them that had them (046 keeps
--     trainings, datasets, workflows and tutorial_progress closed to writes,
--     055 users); 057's column grants on eight of them are revoked first;
--   * trainings: column SELECT for anon and authenticated on the 34 columns
--     047/049/050/052 granted (never worker_token, credit_scheme, settled_at,
--     idempotency_key, stop_requested_at, lost_reasons, charge_waiver);
--   * sequences: ALL (rwU) for anon and authenticated on inference_runs_id_seq
--     and trainings_id_seq;
--   * functions: EXECUTE for PUBLIC, anon and authenticated on the eleven
--     trigger/helper functions, and for anon on the three rls_* helpers;
--   * 027's policy "Trigger inserts workflow versions" with its comment;
--   * default privileges, for every role whose defaults the rolling-back role
--     may change: in public ALL on TABLES, SEQUENCES and FUNCTIONS to anon,
--     authenticated and service_role; globally PostgreSQL's built-in default
--     for functions (EXECUTE to PUBLIC, service_role's global grant removed).
-- NOT restored, on purpose: the storage policy "Service role upload to
-- releases" and the bucket's public flag (no migration ever created them; they
-- let anyone host files on the project's domain, SA-01), the request roles'
-- SELECT on extensions.pg_stat_statements(_info) (SA-22), and on PostgreSQL 17
-- the request roles' MAINTAIN, which no PostgREST request can use.
--
-- ORDER: roll back 058 first (it was written after 057), and roll 057 back
-- BEFORE the 055, 054, 053, 052 or 048 rollback. Every grant is re-issued in
-- the order it originally stood (service_role revoked and granted again in the
-- same transaction), so pg_dump -s of the schemas public and auth prints what it
-- printed at 055.
--
-- Idempotent: a GRANT of what is already there changes nothing. The closing DO
-- block checks the restored state.

BEGIN;

-- 057's column grants go first (they would otherwise survive next to the
-- restored table-level SELECT).
DO $$
DECLARE
    v_rel REGCLASS;
    v_col TEXT;
BEGIN
    FOR v_rel, v_col IN
        SELECT c.oid::regclass, a.attname
          FROM pg_class c
          JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
         WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
           AND a.attacl IS NOT NULL
    LOOP
        EXECUTE format('REVOKE SELECT (%2$I) ON %1$s FROM anon, authenticated', v_rel, v_col);
    END LOOP;
END
$$;

DO $$
DECLARE
    v_name TEXT;
BEGIN
    -- Tables, in the original order (anon, authenticated, service_role).
    FOREACH v_name IN ARRAY ARRAY['public.datasets', 'public.tutorial_progress', 'public.users', 'public.workflows'] LOOP
        EXECUTE format('REVOKE ALL ON %s FROM service_role', v_name);
        EXECUTE format('GRANT SELECT ON %s TO anon, authenticated', v_name);
        EXECUTE format('GRANT ALL ON %s TO service_role', v_name);
    END LOOP;
    FOREACH v_name IN ARRAY ARRAY['public.classrooms', 'public.edu6_arm_records', 'public.inference_runs',
                                  'public.progress_entries', 'public.workflow_submissions',
                                  'public.workflow_trajectories', 'public.workflow_versions',
                                  'public.workgroup_memberships', 'public.workgroups'] LOOP
        EXECUTE format('REVOKE ALL ON %s FROM service_role', v_name);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON %s TO anon, authenticated',
                       v_name);
        EXECUTE format('GRANT ALL ON %s TO service_role', v_name);
    END LOOP;
    -- Sequences (anon, authenticated, service_role).
    FOREACH v_name IN ARRAY ARRAY['public.inference_runs_id_seq', 'public.trainings_id_seq'] LOOP
        IF to_regclass(v_name) IS NOT NULL THEN
            EXECUTE format('REVOKE ALL ON SEQUENCE %s FROM service_role', v_name);
            EXECUTE format('GRANT ALL ON SEQUENCE %s TO anon, authenticated, service_role', v_name);
        END IF;
    END LOOP;
    -- The eleven trigger/helper functions (PUBLIC, anon, authenticated, service_role).
    FOREACH v_name IN ARRAY ARRAY[
        'public.enforce_classroom_capacity()', 'public.touch_updated_at()',
        'public.enforce_workgroup_capacity()', 'public.enforce_workgroup_classroom_match()',
        'public.touch_tutorial_progress_updated_at()', 'public.edu6_arm_records_touch()',
        'public.jsonb_object_key_count(jsonb)', 'public.jsonb_max_text_value_bytes(jsonb)',
        'public.jsonb_all_values_are_strings(jsonb)', 'public.workflow_submissions_refuse_update()',
        'public.user_hf_credentials_touch()'] LOOP
        IF to_regprocedure(v_name) IS NOT NULL THEN
            EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM service_role', v_name);
            EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO PUBLIC, anon, authenticated, service_role', v_name);
        END IF;
    END LOOP;
    -- The policy helpers and the worker's RPC (anon, authenticated, service_role).
    FOREACH v_name IN ARRAY ARRAY[
        'public.rls_is_admin()', 'public.rls_teacher_classroom_ids()', 'public.rls_user_classroom_id()',
        'public.update_training_progress(integer,uuid,text,integer,integer,real,text,text)'] LOOP
        IF to_regprocedure(v_name) IS NOT NULL THEN
            EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM anon, authenticated, service_role', v_name);
            EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO anon, authenticated, service_role', v_name);
        END IF;
    END LOOP;
END
$$;

-- trainings: the 34 columns 047/049/050/052 granted to both request roles.
GRANT SELECT (id, user_id, status, dataset_name, model_name, model_type, training_params, cloud_job_id,
              current_step, total_steps, current_loss, requested_at, terminated_at, error_message,
              last_progress_at, loss_history, workgroup_id, cancel_attempted_at, cancel_attempts, log_url,
              managed, current_attempt, train_seconds_used, status_note, step_advanced_at, dataset_fps,
              unplaced_since, capped_step, continuable_until, continued_from, reserved_hours, charged_hours,
              stop_requested_by_role, lost_seconds)
    ON public.trainings TO anon, authenticated;

-- 027's policy, as 027 wrote it.
DROP POLICY IF EXISTS "Trigger inserts workflow versions" ON public.workflow_versions;
CREATE POLICY "Trigger inserts workflow versions"
  ON public.workflow_versions
  FOR INSERT
  WITH CHECK (
    current_setting('app.user_id', true) IS NOT NULL
    AND current_setting('app.user_id', true) <> ''
    AND saved_by::TEXT = current_setting('app.user_id', true)
  );

COMMENT ON POLICY "Trigger inserts workflow versions" ON public.workflow_versions IS
  'GUC-backed INSERT gate (migration 027, closes the S2 TODO from migration 024). '
  'The SECURITY DEFINER trigger (owner postgres, bypassrls) and the service-role '
  'API never evaluate this policy; it only denies direct PostgREST INSERTs that '
  'lack the transaction-local app.user_id GUC — i.e. forged version-history rows. '
  'saved_by must equal the GUC value set by update_workflow_blockly / '
  'restore_workflow_version.';

-- Default privileges.
DO $$
DECLARE
    v_owner NAME;
BEGIN
    FOR v_owner IN
        SELECT r.rolname FROM pg_roles r
         WHERE r.rolname = current_user
            OR r.oid IN (SELECT d.defaclrole FROM pg_default_acl d
                          WHERE d.defaclnamespace IN (0, 'public'::regnamespace))
            OR r.oid IN (SELECT c.relowner FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace)
            OR r.oid IN (SELECT p.proowner FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace)
         ORDER BY r.rolname
    LOOP
        IF NOT pg_has_role(current_user, v_owner, 'USAGE') THEN
            RAISE NOTICE '057 rollback: % may not change the default privileges of %; left as they are',
                current_user, v_owner;
            CONTINUE;
        END IF;
        -- Globally: back to PostgreSQL's built-in default for functions.
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE EXECUTE ON FUNCTIONS FROM service_role', v_owner);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I GRANT EXECUTE ON FUNCTIONS TO PUBLIC', v_owner);
        -- In public: Supabase's grants, in their original order.
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE ALL ON TABLES FROM service_role', v_owner);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT ALL ON TABLES TO anon, authenticated, service_role', v_owner);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE ALL ON SEQUENCES FROM service_role', v_owner);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT ALL ON SEQUENCES TO anon, authenticated, service_role', v_owner);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM service_role', v_owner);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT ALL ON FUNCTIONS TO anon, authenticated, service_role', v_owner);
    END LOOP;
END
$$;

-- The restored state.
DO $$
DECLARE
    v_rel  TEXT;
    v_role TEXT;
    v_priv TEXT;
BEGIN
    FOREACH v_rel IN ARRAY ARRAY['public.classrooms', 'public.edu6_arm_records', 'public.inference_runs',
                                 'public.progress_entries', 'public.workflow_submissions',
                                 'public.workflow_trajectories', 'public.workflow_versions',
                                 'public.workgroup_memberships', 'public.workgroups'] LOOP
        FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
            FOREACH v_priv IN ARRAY ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER'] LOOP
                IF NOT has_table_privilege(v_role, v_rel, v_priv) THEN
                    RAISE EXCEPTION '057 rollback: % lacks % on %', v_role, v_priv, v_rel;
                END IF;
            END LOOP;
        END LOOP;
    END LOOP;
    FOREACH v_rel IN ARRAY ARRAY['public.datasets', 'public.tutorial_progress', 'public.users', 'public.workflows'] LOOP
        IF NOT has_table_privilege('anon', v_rel, 'SELECT') OR NOT has_table_privilege('authenticated', v_rel, 'SELECT')
           OR has_table_privilege('authenticated', v_rel, 'UPDATE') THEN
            RAISE EXCEPTION '057 rollback: % is not read-only for the request roles', v_rel;
        END IF;
    END LOOP;
    IF NOT has_column_privilege('anon', 'public.trainings', 'training_params', 'SELECT')
       OR has_column_privilege('authenticated', 'public.trainings', 'worker_token', 'SELECT')
       OR NOT has_function_privilege('anon', 'public.touch_updated_at()', 'EXECUTE')
       OR NOT has_function_privilege('anon', 'public.rls_is_admin()', 'EXECUTE')
       OR NOT has_sequence_privilege('authenticated', 'public.trainings_id_seq', 'UPDATE')
       OR NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public' AND tablename = 'workflow_versions'
                         AND policyname = 'Trigger inserts workflow versions' AND cmd = 'INSERT') THEN
        RAISE EXCEPTION '057 rollback: a column, function, sequence or policy grant was not restored';
    END IF;
    RAISE NOTICE '057 rollback complete (the request roles can read and write as before 057)';
END
$$;

COMMIT;

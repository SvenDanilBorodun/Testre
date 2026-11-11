-- Behavioural assertions for migration 057 (the request roles anon and
-- authenticated are default-deny: column SELECT only on the read allowlist, no
-- table-level privilege and no write privilege on any public table, column or
-- sequence, EXECUTE only on the function allowlist, no default privilege for
-- them or for PUBLIC, no storage policy for them, no public bucket), run BY
-- HAND against a LOCAL database — never CI, never the linked project (it seeds
-- rows; T10, T13 and T19 run inside transactions they roll back).
--
-- The baseline replays on a fresh database as shipped, so a local stack of the
-- checkout itself (`supabase start`, or every migration applied in order by
-- psql) is the database: with every migration through 057 applied, then
--   psql <local db url> -f supabase/tests/057_request_role_default_deny_assertions.sql
-- On a plain PostgreSQL, apply .github/scripts/supabase_replay_stub.sql first
-- (the stub ci.yml::migrations-replay uses: Supabase's roles, auth and storage
-- stand-ins with the live storage drift, the live default privileges); T15
-- needs a storage schema and reports PASS with "(no storage schema)" without one.
-- Denials of a TABLE or SEQUENCE are read with has_*_privilege and by a
-- PostgREST request simulated the way PostgREST runs it: SET LOCAL ROLE plus the
-- JWT claims as settings, with a plain statement inside a pg_temp helper (044's
-- pattern). A function a request role may NOT execute is never called under SET
-- ROLE (that kills the local Supabase image's backend with signal 11, SR-25):
-- those denials are has_function_privilege only. The allowlisted functions ARE
-- called under the request role (T11), which proves the grant is effective.
-- REALTIME is replayed, not run (no Realtime server locally): T23 runs, as the
-- subscriber, the row check Supabase Realtime makes for postgres_changes (an
-- EXISTS over the primary key through the table's RLS) and reads the column set
-- it delivers (the columns the subscriber may SELECT) — the measure 047 proved
-- with a real Realtime v2.86.3 for trainings' column grants.
-- 2026-11-13: run on PostgreSQL 16.15 with that stub, every migration 000 → 055
-- applied by psql, then 057: 50 PASS, 0 FAIL, 0 ERROR. Against the database at
-- 055: 19 PASS, 31 FAIL — T1, T2, T3, T4a, T4b, T5, T6a–T6i, T7, T8, T9, T10a,
-- T12, T14, T21, T22a, T22c, T23c, T24 and T25b (the reads, writes and defaults
-- are open) and T15a–T15c and T20 (the live storage drift, the extension's
-- grants). After the 057 rollback: 23 PASS, 27 FAIL — the same less T15a–T15c
-- and T20, which the rollback does not re-open. The cycle 057 → rollback →
-- rollback → 057 → 057 passes each self-check and prints 50 PASS after every
-- apply, and after the rollback pg_dump -s (without the storage and extensions
-- schemas) equals the dump at 055 except the request roles' SELECT on
-- extensions.pg_stat_statements(_info), which the rollback does not restore.
-- The hosted project's privilege model, emulated (bootstrap superuser
-- supabase_admin; postgres a non-superuser member of anon, authenticated and
-- service_role owning the database; auth, storage and the extension owned by
-- their platform roles): 000 → 058 apply as postgres once the storage drift is
-- removed (with it, 057 stops at the policy and names the dashboard step), and
-- this file prints 49 PASS and FAIL T20 — the extension owner's grants on
-- pg_stat_statements, which postgres may not revoke (docs/KNOWN-ISSUES.md).
-- 057 BREAKS older labels BY DESIGN (owner decision 2026-11-13: reads are
-- default-deny too): 052_credit_ledger_assertions.sql T2h (anon read the four
-- ledger columns of trainings; anon reads nothing now) and
-- 055_users_identity_guard_assertions.sql T5a (authenticated held a table-level
-- SELECT on users; it reads four columns now) — measured with 057: 052 prints
-- 348 PASS and FAIL T2h, 055 prints 35 PASS and FAIL T5a — and
-- 049_managed_training_assertions.sql T3 (its seven trainings columns; that file
-- is broken by 052 already). 053_retire_jetson_assertions.sql (12) and
-- 054_advisor_cleanup_assertions.sql (15) print the same with and without 057.
-- 047, 054 and 055 (T20) grant their own simulated writer what 057 took from
-- the request roles (a pg_temp helper called under SET ROLE, the CHECK helpers
-- of a rolled-back test INSERT, the SELECT beside a mistaken UPDATE grant), see
-- their comments; before 057 those grants change nothing.
--
-- Expected: 50 PASS, 0 FAIL (T1..T25; T4a/T4b, T6a..T6i, T10a/T10b, T11a..T11h,
-- T15a..T15c, T16a/T16b, T22a..T22c, T23a..T23c, T25a/T25b).
\set ON_ERROR_STOP off
\pset pager off

-- a teacher with a classroom; students s1 and s2 in it and in one workgroup; a
-- stranger s3; a training of s1 (own), of s2 (shared with the group); a
-- classroom template of the teacher's
INSERT INTO auth.users (id, instance_id, aud, role, email, encrypted_password, created_at, updated_at, raw_app_meta_data)
VALUES ('b5700001-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 's1-057@x.test', '', now(), now(), '{"edubotics_provisioned": true}'),
       ('b5700002-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 't1-057@x.test', '', now(), now(), '{"edubotics_provisioned": true}'),
       ('b5700003-0000-0000-0000-000000000003', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 's2-057@x.test', '', now(), now(), '{"edubotics_provisioned": true}'),
       ('b5700004-0000-0000-0000-000000000004', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 's3-057@x.test', '', now(), now(), '{"edubotics_provisioned": true}')
ON CONFLICT (id) DO NOTHING;
UPDATE public.users SET role = 'teacher', username = 'lehrer057' WHERE id = 'b5700002-0000-0000-0000-000000000002';
INSERT INTO public.classrooms (id, teacher_id, name)
VALUES ('c5700001-0000-0000-0000-000000000001', 'b5700002-0000-0000-0000-000000000002', 'Klasse 057')
ON CONFLICT (id) DO NOTHING;
UPDATE public.users SET classroom_id = 'c5700001-0000-0000-0000-000000000001', username = 'anna057'
 WHERE id = 'b5700001-0000-0000-0000-000000000001';
UPDATE public.users SET classroom_id = 'c5700001-0000-0000-0000-000000000001', username = 'ben057'
 WHERE id = 'b5700003-0000-0000-0000-000000000003';
INSERT INTO public.workgroups (id, classroom_id, name)
VALUES ('a5700001-0000-0000-0000-000000000001', 'c5700001-0000-0000-0000-000000000001', 'Gruppe 057')
ON CONFLICT (id) DO NOTHING;
UPDATE public.users SET workgroup_id = 'a5700001-0000-0000-0000-000000000001'
 WHERE id IN ('b5700001-0000-0000-0000-000000000001', 'b5700003-0000-0000-0000-000000000003');
INSERT INTO public.workgroup_memberships (workgroup_id, user_id)
VALUES ('a5700001-0000-0000-0000-000000000001', 'b5700001-0000-0000-0000-000000000001'),
       ('a5700001-0000-0000-0000-000000000001', 'b5700003-0000-0000-0000-000000000003')
ON CONFLICT DO NOTHING;
INSERT INTO public.trainings (id, user_id, status, dataset_name, model_name, model_type, credit_scheme, workgroup_id)
VALUES (957001, 'b5700001-0000-0000-0000-000000000001', 'succeeded', 'anna057/wuerfel', 'EduBotics-Solutions/m057a', 'act', 'derived', NULL),
       (957002, 'b5700003-0000-0000-0000-000000000003', 'succeeded', 'ben057/wuerfel', 'EduBotics-Solutions/m057b', 'act', 'derived',
        'a5700001-0000-0000-0000-000000000001')
ON CONFLICT (id) DO NOTHING;
INSERT INTO public.workflows (id, owner_user_id, classroom_id, name, blockly_json)
VALUES ('f5700001-0000-0000-0000-000000000001', 'b5700001-0000-0000-0000-000000000001',
        'c5700001-0000-0000-0000-000000000001', 'Programm 057', '{}')
ON CONFLICT (id) DO NOTHING;
INSERT INTO public.workflows (id, owner_user_id, classroom_id, name, blockly_json, is_template)
VALUES ('f5700002-0000-0000-0000-000000000002', 'b5700002-0000-0000-0000-000000000002',
        'c5700001-0000-0000-0000-000000000001', 'Vorlage 057', '{}', true)
ON CONFLICT (id) DO NOTHING;

-- `try_as(role, sql)` runs one statement as a PostgREST request of student s1
-- (or of anon) and answers 'denied' for a missing PRIVILEGE ("permission denied
-- for …"), else its SQLSTATE ('00000' = it touched a row, 'no-row' = it ran and
-- matched nothing; an RLS refusal of a new row is '42501' too, which is why a
-- privilege denial is told apart by its message).
CREATE OR REPLACE FUNCTION pg_temp.try_as(p_role TEXT, p_sql TEXT) RETURNS TEXT LANGUAGE plpgsql AS $$
DECLARE n INT;
BEGIN
  PERFORM set_config('request.jwt.claim.sub',
                     CASE WHEN p_role = 'anon' THEN '' ELSE 'b5700001-0000-0000-0000-000000000001' END, true);
  PERFORM set_config('request.jwt.claims',
                     CASE WHEN p_role = 'anon' THEN '{"role":"anon"}'
                          ELSE '{"sub":"b5700001-0000-0000-0000-000000000001","role":"authenticated","aal":"aal1"}' END,
                     true);
  EXECUTE format('SET LOCAL ROLE %I', p_role);
  EXECUTE p_sql;
  GET DIAGNOSTICS n = ROW_COUNT;
  RESET ROLE;
  RETURN CASE WHEN n >= 1 THEN '00000' ELSE 'no-row' END;
EXCEPTION WHEN OTHERS THEN
  RETURN CASE WHEN SQLSTATE = '42501' AND SQLERRM LIKE 'permission denied%' THEN 'denied' ELSE SQLSTATE END;
END $$;

-- `count_as(role, sub, sql)`: the number of rows a SELECT returns to that request
-- ('denied' for a missing privilege).
CREATE OR REPLACE FUNCTION pg_temp.count_as(p_role TEXT, p_sub TEXT, p_sql TEXT) RETURNS TEXT LANGUAGE plpgsql AS $$
DECLARE n BIGINT;
BEGIN
  PERFORM set_config('request.jwt.claim.sub', coalesce(p_sub, ''), true);
  PERFORM set_config('request.jwt.claims',
                     CASE WHEN p_sub IS NULL THEN '{"role":"anon"}'
                          ELSE json_build_object('sub', p_sub, 'role', p_role, 'aal', 'aal1')::text END, true);
  EXECUTE format('SET LOCAL ROLE %I', p_role);
  EXECUTE 'SELECT count(*) FROM (' || p_sql || ') q' INTO n;
  RESET ROLE;
  RETURN n::text;
EXCEPTION WHEN OTHERS THEN
  RETURN CASE WHEN SQLSTATE = '42501' AND SQLERRM LIKE 'permission denied%' THEN 'denied' ELSE 'ERROR ' || SQLSTATE END;
END $$;

-- `call_as(role, sql)`: the value (or SQLSTATE) of one function call made as that request.
CREATE OR REPLACE FUNCTION pg_temp.call_as(p_role TEXT, p_sql TEXT) RETURNS TEXT LANGUAGE plpgsql AS $$
DECLARE v TEXT;
BEGIN
  PERFORM set_config('request.jwt.claims', '{"role":"' || p_role || '"}', true);
  EXECUTE format('SET LOCAL ROLE %I', p_role);
  EXECUTE p_sql INTO v;
  RESET ROLE;
  RETURN coalesce(v, 'null');
EXCEPTION WHEN OTHERS THEN
  RETURN 'SQLSTATE ' || SQLSTATE;
END $$;

-- The function allowlist (supabase/request_role_allowlist.json): signature -> role.
CREATE TEMP TABLE t057_allow (fn TEXT, role TEXT);
INSERT INTO t057_allow VALUES
  ('public.rls_is_admin()', 'authenticated'),
  ('public.rls_teacher_classroom_ids()', 'authenticated'),
  ('public.rls_user_classroom_id()', 'authenticated'),
  ('public.update_training_progress(integer,uuid,text,integer,integer,real,text,text)', 'anon'),
  ('public.update_training_progress(integer,uuid,text,integer,integer,real,text,text)', 'authenticated'),
  ('public.inference_session_peek(text,text)', 'anon'),
  ('public.inference_session_connect(text,text,text)', 'anon'),
  ('public.inference_session_heartbeat(text,text,text,jsonb)', 'anon'),
  ('public.inference_session_end(text,text,text,text,jsonb)', 'anon');

-- The read allowlist (the same JSON): table, column; for authenticated only.
CREATE TEMP TABLE t057_reads (rel TEXT, col TEXT);
INSERT INTO t057_reads
SELECT 'trainings', unnest(ARRAY['id', 'user_id', 'status', 'model_name', 'model_type', 'dataset_name', 'current_step',
       'total_steps', 'current_loss', 'loss_history', 'error_message', 'requested_at', 'terminated_at',
       'last_progress_at', 'workgroup_id', 'status_note', 'capped_step', 'continuable_until', 'continued_from',
       'reserved_hours', 'charged_hours', 'stop_requested_by_role', 'lost_seconds'])
UNION ALL SELECT 'datasets', unnest(ARRAY['id', 'owner_user_id', 'workgroup_id', 'hf_repo_id', 'name', 'updated_at'])
UNION ALL SELECT 'workflows', unnest(ARRAY['id', 'owner_user_id', 'workgroup_id', 'name', 'description', 'is_template', 'updated_at'])
UNION ALL SELECT 'tutorial_progress', unnest(ARRAY['user_id', 'tutorial_id', 'current_step', 'completed_at', 'updated_at'])
UNION ALL SELECT 'users', unnest(ARRAY['id', 'role', 'classroom_id', 'workgroup_id'])
UNION ALL SELECT 'classrooms', unnest(ARRAY['id', 'teacher_id'])
UNION ALL SELECT 'workgroups', unnest(ARRAY['id', 'classroom_id'])
UNION ALL SELECT 'workgroup_memberships', unnest(ARRAY['user_id', 'workgroup_id']);

\echo '=== T1 no request role holds a table-level write privilege on any public table or view ==='
SELECT CASE WHEN count(*) = 0 THEN 'PASS T1'
            ELSE 'FAIL T1 (' || string_agg(r || ':' || pr || ':' || c.relname, ', ') || ')' END
  FROM pg_class c,
       unnest(ARRAY['anon', 'authenticated']) AS r,
       unnest(ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER']) AS pr
 WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
   AND has_table_privilege(r, c.oid, pr);

\echo '=== T2 ... nor a column-level one ==='
SELECT CASE WHEN count(*) = 0 THEN 'PASS T2' ELSE 'FAIL T2 (' || count(*) || ')' END
  FROM pg_class c
  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped,
       unnest(ARRAY['anon', 'authenticated']) AS r,
       unnest(ARRAY['INSERT', 'UPDATE', 'REFERENCES']) AS pr
 WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
   AND has_column_privilege(r, c.oid, a.attnum, pr);

\echo '=== T3 ... nor any privilege on a sequence (nextval, setval) ==='
SELECT CASE WHEN count(*) = 0 THEN 'PASS T3'
            ELSE 'FAIL T3 (' || string_agg(r || ':' || pr || ':' || c.relname, ', ') || ')' END
  FROM pg_class c, unnest(ARRAY['anon', 'authenticated']) AS r, unnest(ARRAY['USAGE', 'SELECT', 'UPDATE']) AS pr
 WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'S' AND has_sequence_privilege(r, c.oid, pr);

\echo '=== T4 a request role executes exactly the allowlist (has_function_privilege, every public function) ==='
SET search_path = '';
SELECT CASE WHEN count(*) = 0 THEN 'PASS T4a'
            ELSE 'FAIL T4a (' || string_agg(r || ':' || p.oid::regprocedure::text, ', ') || ')' END
  FROM pg_proc p, unnest(ARRAY['anon', 'authenticated']) AS r
 WHERE p.pronamespace = 'public'::regnamespace AND has_function_privilege(r, p.oid, 'EXECUTE')
   AND NOT EXISTS (SELECT 1 FROM pg_temp.t057_allow l WHERE l.fn = p.oid::regprocedure::text AND l.role = r);
SELECT CASE WHEN count(*) = 9 AND bool_and(has_function_privilege(l.role, to_regprocedure(l.fn), 'EXECUTE'))
             AND NOT has_function_privilege('anon', 'public.rls_is_admin()', 'EXECUTE')
            THEN 'PASS T4b' ELSE 'FAIL T4b' END
  FROM pg_temp.t057_allow l;
RESET search_path;

\echo '=== T5 PUBLIC executes no public function ==='
SELECT CASE WHEN count(*) = 0 THEN 'PASS T5' ELSE 'FAIL T5 (' || count(*) || ')' END
  FROM pg_proc p, aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
 WHERE p.pronamespace = 'public'::regnamespace AND a.grantee = 0;

\echo '=== T6 a student''s PostgREST write to each of the nine tables 057 closed is refused by privilege ==='
SELECT CASE WHEN pg_temp.try_as('authenticated', $q$INSERT INTO public.classrooms (teacher_id, name) VALUES ('b5700001-0000-0000-0000-000000000001', 'eigene')$q$) = 'denied' THEN 'PASS T6a' ELSE 'FAIL T6a' END;
SELECT CASE WHEN pg_temp.try_as('authenticated', $q$INSERT INTO public.edu6_arm_records (arm_serial, record) VALUES ('X-057', '{}')$q$) = 'denied' THEN 'PASS T6b' ELSE 'FAIL T6b' END;
SELECT CASE WHEN pg_temp.try_as('authenticated', $q$DELETE FROM public.inference_runs$q$) = 'denied' THEN 'PASS T6c' ELSE 'FAIL T6c' END;
SELECT CASE WHEN pg_temp.try_as('authenticated', $q$UPDATE public.progress_entries SET note = 'x'$q$) = 'denied' THEN 'PASS T6d' ELSE 'FAIL T6d' END;
SELECT CASE WHEN pg_temp.try_as('authenticated', $q$INSERT INTO public.workflow_submissions (workflow_id, student_user_id, name) VALUES ('f5700001-0000-0000-0000-000000000001', 'b5700001-0000-0000-0000-000000000001', 'x')$q$) = 'denied' THEN 'PASS T6e' ELSE 'FAIL T6e' END;
SELECT CASE WHEN pg_temp.try_as('authenticated', $q$INSERT INTO public.workflow_trajectories (workflow_id, owner_user_id, name, samples) VALUES ('f5700001-0000-0000-0000-000000000001', 'b5700001-0000-0000-0000-000000000001', 'x', '{}')$q$) = 'denied' THEN 'PASS T6f' ELSE 'FAIL T6f' END;
SELECT CASE WHEN pg_temp.try_as('authenticated', $q$INSERT INTO public.workflow_versions (workflow_id, blockly_json, saved_by) VALUES ('f5700001-0000-0000-0000-000000000001', '{}', 'b5700001-0000-0000-0000-000000000001')$q$) = 'denied' THEN 'PASS T6g' ELSE 'FAIL T6g' END;
SELECT CASE WHEN pg_temp.try_as('authenticated', $q$DELETE FROM public.workgroup_memberships$q$) = 'denied' THEN 'PASS T6h' ELSE 'FAIL T6h' END;
SELECT CASE WHEN pg_temp.try_as('authenticated', $q$INSERT INTO public.workgroups (classroom_id, name) VALUES ('c5700001-0000-0000-0000-000000000001', 'eigene Gruppe')$q$) = 'denied' THEN 'PASS T6i' ELSE 'FAIL T6i' END;

\echo '=== T7 anon cannot TRUNCATE a table (TRUNCATE ignores RLS) ==='
SELECT CASE WHEN pg_temp.try_as('anon', 'TRUNCATE public.inference_runs') = 'denied' THEN 'PASS T7' ELSE 'FAIL T7' END;

\echo '=== T8 a student cannot move a sequence (setval on trainings_id_seq) ==='
SELECT CASE WHEN pg_temp.try_as('authenticated', $q$SELECT setval('public.trainings_id_seq', 1)$q$) = 'denied'
            THEN 'PASS T8' ELSE 'FAIL T8' END;

\echo '=== T9 the eleven trigger/helper functions are closed to both request roles (has_function_privilege) ==='
SELECT CASE WHEN count(*) = 0 THEN 'PASS T9' ELSE 'FAIL T9 (' || count(*) || ')' END
  FROM unnest(ARRAY['public.enforce_classroom_capacity()', 'public.touch_updated_at()',
                    'public.enforce_workgroup_capacity()', 'public.enforce_workgroup_classroom_match()',
                    'public.touch_tutorial_progress_updated_at()', 'public.edu6_arm_records_touch()',
                    'public.jsonb_object_key_count(jsonb)', 'public.jsonb_max_text_value_bytes(jsonb)',
                    'public.jsonb_all_values_are_strings(jsonb)', 'public.workflow_submissions_refuse_update()',
                    'public.user_hf_credentials_touch()']) AS f,
       unnest(ARRAY['anon', 'authenticated']) AS r
 WHERE to_regprocedure(f) IS NOT NULL AND has_function_privilege(r, to_regprocedure(f), 'EXECUTE');

\echo '=== T10 what a LATER migration creates is unreadable, unwritable and uncallable for the request roles, open to service_role ==='
BEGIN;
CREATE TABLE public.t057_later (id bigint GENERATED BY DEFAULT AS IDENTITY, v text);
ALTER TABLE public.t057_later ENABLE ROW LEVEL SECURITY;
CREATE POLICY t057_later_read ON public.t057_later FOR SELECT USING (true);
CREATE FUNCTION public.t057_later_fn() RETURNS integer LANGUAGE sql AS 'SELECT 1';
SELECT CASE WHEN pg_temp.try_as('authenticated', 'SELECT * FROM public.t057_later') = 'denied'
             AND pg_temp.try_as('anon', 'SELECT id FROM public.t057_later') = 'denied'
             AND pg_temp.try_as('anon', $q$INSERT INTO public.t057_later (v) VALUES ('x')$q$) = 'denied'
             AND NOT has_any_column_privilege('authenticated', 'public.t057_later', 'SELECT')
             AND NOT has_any_column_privilege('anon', 'public.t057_later', 'SELECT')
             AND NOT has_table_privilege('anon', 'public.t057_later', 'INSERT')
             AND NOT has_sequence_privilege('authenticated', pg_get_serial_sequence('public.t057_later', 'id'), 'USAGE')
             AND NOT has_function_privilege('anon', 'public.t057_later_fn()', 'EXECUTE')
             AND NOT has_function_privilege('authenticated', 'public.t057_later_fn()', 'EXECUTE')
            THEN 'PASS T10a' ELSE 'FAIL T10a' END;
SELECT CASE WHEN has_table_privilege('service_role', 'public.t057_later', 'SELECT')
             AND has_table_privilege('service_role', 'public.t057_later', 'INSERT')
             AND has_table_privilege('service_role', 'public.t057_later', 'UPDATE')
             AND has_table_privilege('service_role', 'public.t057_later', 'DELETE')
             AND has_sequence_privilege('service_role', pg_get_serial_sequence('public.t057_later', 'id'), 'USAGE')
             AND has_function_privilege('service_role', 'public.t057_later_fn()', 'EXECUTE')
            THEN 'PASS T10b' ELSE 'FAIL T10b' END;
ROLLBACK;

\echo '=== T11 every allowlisted path still works ==='
-- (a) the student's direct trainings read, exactly as useSupabaseTrainings sends it (LIVE_COLUMNS, the group filter, the order): own + group sibling's
SELECT CASE WHEN pg_temp.count_as('authenticated', 'b5700001-0000-0000-0000-000000000001',
  $q$SELECT id, status, model_name, model_type, dataset_name, current_step, total_steps, current_loss, loss_history,
            error_message, requested_at, terminated_at, last_progress_at, workgroup_id, status_note, capped_step,
            continuable_until, continued_from, reserved_hours, charged_hours, stop_requested_by_role, lost_seconds
       FROM public.trainings
      WHERE user_id = 'b5700001-0000-0000-0000-000000000001' OR workgroup_id = 'a5700001-0000-0000-0000-000000000001'
      ORDER BY requested_at DESC$q$) = '2' THEN 'PASS T11a' ELSE 'FAIL T11a' END;
-- (b) the teacher reaches the class's trainings (the policy's sub-select over users and classrooms, narrowed columns)
SELECT CASE WHEN pg_temp.count_as('authenticated', 'b5700002-0000-0000-0000-000000000002', 'SELECT id FROM public.trainings') = '2'
            THEN 'PASS T11b' ELSE 'FAIL T11b' END;
-- (c) the student sees their own workflow and the classroom template (TemplatePicker's columns; the policy
--     reads workflows.classroom_id, which the student may not select: a policy's own-table columns need no grant)
SELECT CASE WHEN pg_temp.count_as('authenticated', 'b5700001-0000-0000-0000-000000000001',
                                  'SELECT id, name, description, is_template, updated_at FROM public.workflows') = '2'
            THEN 'PASS T11c' ELSE 'FAIL T11c' END;
-- (d) a stranger sees none of it (RLS still decides the rows)
SELECT CASE WHEN pg_temp.count_as('authenticated', 'b5700004-0000-0000-0000-000000000004', 'SELECT id FROM public.trainings') = '0'
             AND pg_temp.count_as('authenticated', 'b5700004-0000-0000-0000-000000000004', 'SELECT id FROM public.workflows') = '0'
            THEN 'PASS T11d' ELSE 'FAIL T11d' END;
-- (e) datasets and tutorial_progress answer their Realtime consumers' columns
SELECT CASE WHEN pg_temp.count_as('authenticated', 'b5700001-0000-0000-0000-000000000001',
                                  'SELECT id, hf_repo_id, name, updated_at, owner_user_id, workgroup_id FROM public.datasets') = '0'
             AND pg_temp.count_as('authenticated', 'b5700001-0000-0000-0000-000000000001',
                                  'SELECT * FROM public.tutorial_progress') = '0'
            THEN 'PASS T11e' ELSE 'FAIL T11e' END;
-- (f) the worker's update_training_progress runs for anon AND authenticated (its own refusal, not 42501)
SELECT CASE WHEN pg_temp.call_as('anon', $q$SELECT public.update_training_progress(1, '00000000-0000-0000-0000-000000000000'::uuid, 'running', 1, 10, 0.5::real, NULL, NULL)::text$q$) = 'SQLSTATE P0001'
             AND pg_temp.call_as('authenticated', $q$SELECT public.update_training_progress(1, '00000000-0000-0000-0000-000000000000'::uuid, 'running', 1, 10, 0.5::real, NULL, NULL)::text$q$) = 'SQLSTATE P0001'
            THEN 'PASS T11f' ELSE 'FAIL T11f' END;
-- (g) the inference container's four anon RPCs run (an unknown token answers their own verdict)
SELECT CASE WHEN pg_temp.call_as('anon', $q$SELECT public.inference_session_peek('x', 'y')::text$q$) NOT LIKE 'SQLSTATE 42501%'
             AND pg_temp.call_as('anon', $q$SELECT public.inference_session_connect('x', 'y', 'z')::text$q$) NOT LIKE 'SQLSTATE 42501%'
             AND pg_temp.call_as('anon', $q$SELECT public.inference_session_heartbeat('x', 'y', 'z', '{}'::jsonb)::text$q$) NOT LIKE 'SQLSTATE 42501%'
             AND pg_temp.call_as('anon', $q$SELECT public.inference_session_end('x', 'y', 'z', 'w', '{}'::jsonb)::text$q$) NOT LIKE 'SQLSTATE 42501%'
            THEN 'PASS T11g' ELSE 'FAIL T11g' END;
-- (h) ... and stay closed to authenticated, as 048 left them
SELECT CASE WHEN NOT has_function_privilege('authenticated', 'public.inference_session_peek(text,text)', 'EXECUTE')
             AND NOT has_function_privilege('authenticated', 'public.inference_session_end(text,text,text,text,jsonb)', 'EXECUTE')
            THEN 'PASS T11h' ELSE 'FAIL T11h' END;

\echo '=== T12 no write policy is left for a request role (027''s workflow_versions INSERT policy is gone) ==='
SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public' AND cmd <> 'SELECT'
                                AND roles && ARRAY['public', 'anon', 'authenticated']::name[])
             AND EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public' AND tablename = 'workflow_versions'
                          AND policyname = 'Workflow versions read consolidated')
            THEN 'PASS T12' ELSE 'FAIL T12' END;

\echo '=== T13 the snapshot trigger (SECURITY DEFINER) still writes a version without the policy ==='
BEGIN;
SELECT public.update_workflow_blockly('f5700001-0000-0000-0000-000000000001', 'b5700001-0000-0000-0000-000000000001',
                                      '{"blocks": {"languageVersion": 0, "blocks": []}}'::jsonb) IS NULL AS ignored;
SELECT CASE WHEN count(*) >= 1 THEN 'PASS T13' ELSE 'FAIL T13 (no version row)' END
  FROM public.workflow_versions WHERE workflow_id = 'f5700001-0000-0000-0000-000000000001';
ROLLBACK;

\echo '=== T14 no default privilege in public (or global) grants a request role or PUBLIC anything ==='
SELECT CASE WHEN count(*) = 0 THEN 'PASS T14'
            ELSE 'FAIL T14 (' || string_agg(d.defaclrole::regrole::text || ':' || d.defaclobjtype::text, ', ') || ')' END
  FROM pg_default_acl d, aclexplode(d.defaclacl) a
 WHERE d.defaclnamespace IN (0, 'public'::regnamespace) AND d.defaclobjtype IN ('r', 'S', 'f')
   AND pg_has_role(current_user, d.defaclrole, 'USAGE')
   AND (a.grantee = 0 OR a.grantee IN (to_regrole('anon'), to_regrole('authenticated')));

\echo '=== T15 storage (SA-01): no upload for a signed-in account, no request-role policy, no public bucket ==='
DO $$
DECLARE v TEXT;
BEGIN
  IF to_regclass('storage.objects') IS NULL THEN
    RAISE NOTICE 'PASS T15a (no storage schema)';
    RAISE NOTICE 'PASS T15b (no storage schema)';
    RAISE NOTICE 'PASS T15c (no storage schema)';
    RETURN;
  END IF;
  v := pg_temp.try_as('authenticated',
         $q$INSERT INTO storage.objects (bucket_id, name) VALUES ('releases', 'EduBotics_Setup_057.exe')$q$);
  -- refused by RLS (no policy is left; the request roles keep Supabase's storage grants) or by privilege
  IF v IN ('42501', 'denied') THEN RAISE NOTICE 'PASS T15a'; ELSE RAISE NOTICE 'FAIL T15a (%)', v; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'storage'
                   AND roles && ARRAY['public', 'anon', 'authenticated']::name[]) THEN
    RAISE NOTICE 'PASS T15b';
  ELSE RAISE NOTICE 'FAIL T15b'; END IF;
  EXECUTE 'SELECT count(*)::text FROM storage.buckets WHERE public' INTO v;
  IF v = '0' THEN RAISE NOTICE 'PASS T15c'; ELSE RAISE NOTICE 'FAIL T15c (% public)', v; END IF;
END $$;

\echo '=== T16 the hidden trainings columns stay hidden; every kept read still has its read policy ==='
SELECT CASE WHEN NOT has_column_privilege('authenticated', 'public.trainings', 'worker_token', 'SELECT')
             AND NOT has_column_privilege('authenticated', 'public.trainings', 'lost_reasons', 'SELECT')
             AND has_column_privilege('authenticated', 'public.trainings', 'lost_seconds', 'SELECT')
            THEN 'PASS T16a' ELSE 'FAIL T16a' END;
SELECT CASE WHEN count(DISTINCT tablename) = 8 THEN 'PASS T16b' ELSE 'FAIL T16b (' || count(DISTINCT tablename) || ')' END
  FROM pg_policies
 WHERE schemaname = 'public' AND cmd = 'SELECT'
   AND tablename IN ('trainings', 'datasets', 'workflows', 'tutorial_progress', 'users', 'classrooms', 'workgroups',
                     'workgroup_memberships');

\echo '=== T17 the Realtime publication is unchanged (seven tables, trainings among them) ==='
SELECT CASE WHEN count(*) = 7 AND bool_or(tablename = 'trainings') THEN 'PASS T17' ELSE 'FAIL T17 (' || count(*) || ')' END
  FROM pg_publication_tables WHERE pubname = 'supabase_realtime' AND schemaname = 'public';

\echo '=== T18 the service role keeps its writes and its CHECK helpers ==='
SELECT CASE WHEN bool_and(has_table_privilege('service_role', t, pr))
             AND has_function_privilege('service_role', 'public.jsonb_all_values_are_strings(jsonb)', 'EXECUTE')
             AND has_sequence_privilege('service_role', 'public.trainings_id_seq', 'USAGE')
            THEN 'PASS T18' ELSE 'FAIL T18' END
  FROM unnest(ARRAY['public.classrooms', 'public.workgroups', 'public.workgroup_memberships', 'public.progress_entries',
                    'public.workflow_submissions', 'public.workflow_trajectories', 'public.inference_runs',
                    'public.edu6_arm_records', 'public.users', 'public.datasets', 'public.workflows']) AS t,
       unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']) AS pr;

\echo '=== T19 a service-role write runs the CHECK helpers (a code workflow) ==='
BEGIN;
SET LOCAL ROLE service_role;
INSERT INTO public.workflows (owner_user_id, name, blockly_json, code_language, code_files)
VALUES ('b5700001-0000-0000-0000-000000000001', 'Code 057', '{}', 'python', '{"main.py": "print(1)"}');
RESET ROLE;
SELECT CASE WHEN count(*) = 1 THEN 'PASS T19' ELSE 'FAIL T19' END
  FROM public.workflows WHERE name = 'Code 057';
ROLLBACK;

\echo '=== T20 pg_stat_statements: no direct grant to a request role is left (SA-22) ==='
SELECT CASE WHEN count(*) = 0 THEN 'PASS T20' ELSE 'FAIL T20 (' || count(*) || ')' END
  FROM pg_class c, aclexplode(c.relacl) a
 WHERE c.oid IN (SELECT to_regclass(n) FROM unnest(ARRAY['extensions.pg_stat_statements',
                                                        'extensions.pg_stat_statements_info']) n
                  WHERE to_regclass(n) IS NOT NULL)
   AND a.grantee IN (to_regrole('anon'), to_regrole('authenticated'));

\echo '=== T21 no table-level privilege of PUBLIC or a request role on any public relation (reads are column grants) ==='
SELECT CASE WHEN count(*) = 0 THEN 'PASS T21'
            ELSE 'FAIL T21 (' || string_agg(DISTINCT c.relname, ', ') || ')' END
  FROM pg_class c, aclexplode(c.relacl) a
 WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
   AND a.grantee IN (0, to_regrole('anon'), to_regrole('authenticated'));

\echo '=== T22 column SELECT is exactly the read allowlist: authenticated reads it, anon reads nothing ==='
SELECT CASE WHEN count(*) = 0 THEN 'PASS T22a'
            ELSE 'FAIL T22a (' || string_agg(c.relname || '.' || a.attname, ', ') || ')' END
  FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
 WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
   AND has_column_privilege('authenticated', c.oid, a.attnum, 'SELECT')
   AND NOT EXISTS (SELECT 1 FROM pg_temp.t057_reads r WHERE r.rel = c.relname AND r.col = a.attname);
SELECT CASE WHEN count(*) = 51 AND bool_and(has_column_privilege('authenticated', 'public.' || r.rel, r.col, 'SELECT'))
            THEN 'PASS T22b' ELSE 'FAIL T22b' END
  FROM pg_temp.t057_reads r;
SELECT CASE WHEN count(*) = 0 THEN 'PASS T22c' ELSE 'FAIL T22c (' || count(*) || ' columns)' END
  FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
 WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
   AND has_column_privilege('anon', c.oid, a.attnum, 'SELECT');

\echo '=== T23 Realtime on trainings still delivers to its owner (the subscriber''s row check and delivered columns, replayed) ==='
-- (a) the row check Realtime runs as the subscriber: the owner and a group sibling see the row
SELECT CASE WHEN pg_temp.count_as('authenticated', 'b5700001-0000-0000-0000-000000000001',
                                  'SELECT 1 FROM public.trainings WHERE id = 957001') = '1'
             AND pg_temp.count_as('authenticated', 'b5700001-0000-0000-0000-000000000001',
                                  'SELECT 1 FROM public.trainings WHERE id = 957002') = '1'
            THEN 'PASS T23a' ELSE 'FAIL T23a' END;
-- (b) ... a stranger does not (the event is not delivered to them)
SELECT CASE WHEN pg_temp.count_as('authenticated', 'b5700004-0000-0000-0000-000000000004',
                                  'SELECT 1 FROM public.trainings WHERE id = 957001') = '0'
            THEN 'PASS T23b' ELSE 'FAIL T23b' END;
-- (c) the delivered record: the columns authenticated may SELECT = LIVE_COLUMNS + user_id (the filter column)
SELECT CASE WHEN array_agg(a.attname::text ORDER BY a.attname)
                 = (SELECT array_agg(col ORDER BY col) FROM pg_temp.t057_reads WHERE rel = 'trainings')
            THEN 'PASS T23c' ELSE 'FAIL T23c' END
  FROM pg_attribute a
 WHERE a.attrelid = 'public.trainings'::regclass AND a.attnum > 0 AND NOT a.attisdropped
   AND has_column_privilege('authenticated', a.attrelid, a.attnum, 'SELECT');

\echo '=== T24 a read outside the allowlist answers 42501 for each request role ==='
SELECT CASE WHEN bool_and(v = 'denied') THEN 'PASS T24'
            ELSE 'FAIL T24 (' || string_agg(q || '=' || v, '; ') FILTER (WHERE v <> 'denied') || ')' END
  FROM (SELECT q, pg_temp.count_as(r, s, q) AS v
          FROM (VALUES ('authenticated', 'b5700001-0000-0000-0000-000000000001', 'SELECT id FROM public.progress_entries'),
                       ('authenticated', 'b5700001-0000-0000-0000-000000000001', 'SELECT id FROM public.workflow_versions'),
                       ('authenticated', 'b5700001-0000-0000-0000-000000000001', 'SELECT id FROM public.workflow_submissions'),
                       ('authenticated', 'b5700001-0000-0000-0000-000000000001', 'SELECT id FROM public.workflow_trajectories'),
                       ('authenticated', 'b5700001-0000-0000-0000-000000000001', 'SELECT id FROM public.inference_runs'),
                       ('authenticated', 'b5700001-0000-0000-0000-000000000001', 'SELECT arm_serial FROM public.edu6_arm_records'),
                       ('authenticated', 'b5700001-0000-0000-0000-000000000001', 'SELECT email FROM public.users'),
                       ('authenticated', 'b5700001-0000-0000-0000-000000000001', 'SELECT name FROM public.classrooms'),
                       ('authenticated', 'b5700001-0000-0000-0000-000000000001', 'SELECT training_params FROM public.trainings'),
                       ('authenticated', 'b5700001-0000-0000-0000-000000000001', 'SELECT blockly_json FROM public.workflows'),
                       ('authenticated', 'b5700001-0000-0000-0000-000000000001', 'SELECT * FROM public.trainings'),
                       ('anon', NULL, 'SELECT id FROM public.trainings'),
                       ('anon', NULL, 'SELECT id FROM public.users'),
                       ('anon', NULL, 'SELECT id FROM public.datasets'),
                       ('anon', NULL, 'SELECT id FROM public.workflows')) AS t (r, s, q)) x;

\echo '=== T25 the teacher web and the admin read nothing directly beyond the RLS-needed columns ==='
SELECT CASE WHEN pg_temp.count_as('authenticated', 'b5700002-0000-0000-0000-000000000002',
                                  'SELECT id, role, classroom_id, workgroup_id FROM public.users') = '3'
            THEN 'PASS T25a' ELSE 'FAIL T25a' END;
SELECT CASE WHEN pg_temp.count_as('authenticated', 'b5700002-0000-0000-0000-000000000002',
                                  'SELECT username, full_name FROM public.users') = 'denied'
            THEN 'PASS T25b' ELSE 'FAIL T25b' END;

-- clean-up
DELETE FROM public.trainings WHERE id IN (957001, 957002);
DELETE FROM public.workflows WHERE id IN ('f5700001-0000-0000-0000-000000000001', 'f5700002-0000-0000-0000-000000000002');
DELETE FROM public.workgroup_memberships WHERE workgroup_id = 'a5700001-0000-0000-0000-000000000001';
UPDATE public.users SET workgroup_id = NULL WHERE workgroup_id = 'a5700001-0000-0000-0000-000000000001';
DELETE FROM public.workgroups WHERE id = 'a5700001-0000-0000-0000-000000000001';
DELETE FROM auth.users WHERE id IN ('b5700001-0000-0000-0000-000000000001', 'b5700002-0000-0000-0000-000000000002',
                                    'b5700003-0000-0000-0000-000000000003', 'b5700004-0000-0000-0000-000000000004');
DELETE FROM public.classrooms WHERE id = 'c5700001-0000-0000-0000-000000000001';

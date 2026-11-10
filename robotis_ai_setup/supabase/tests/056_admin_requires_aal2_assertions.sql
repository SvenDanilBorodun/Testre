-- Behavioural assertions for migration 056 (an admin reads other people's rows
-- only in a two-factor session: public.rls_is_admin() requires the JWT claim
-- aal = aal2, and every admin branch of every RLS policy goes through it), run
-- BY HAND against a LOCAL database — never CI, never the linked project (it seeds
-- rows and removes them at the end).
--
-- The baseline replays on a fresh database as shipped, so a local stack of the
-- checkout itself (`supabase start`, or every migration applied in order by
-- psql) is the database: with every migration through 056 applied, then
--   psql <local db url> -f supabase/tests/056_admin_requires_aal2_assertions.sql
-- Reads are simulated the way PostgREST and Realtime run them: SET LOCAL ROLE
-- authenticated plus the JWT claims as settings (request.jwt.claims, and
-- request.jwt.claim.sub for an auth.uid() that reads the legacy setting), a
-- plain SELECT inside a pg_temp helper. No service-role-only function is called
-- under SET ROLE (CLAUDE.md Rule §4).
-- 2026-11-12: run on PostgreSQL 16 with stub Supabase roles (anon,
-- authenticated, service_role), a stub auth schema whose auth.uid() / auth.jwt()
-- read the settings as Supabase's do, Supabase's default privileges and the
-- supabase_realtime publication, every migration 000 → 055 applied by psql, then
-- 056: 36 PASS, 0 FAIL, 0 ERROR. Against the database at 055 (or after the 056
-- rollback): 20 PASS, 16 FAIL — T1a, T1c, the eleven aal1 cases T2a..T12a (an
-- admin with only a password read every row) and the shape checks T15a..T15c.
-- The cycle 056 → rollback → rollback → 056 → 056 passes each self-check and
-- prints 36 PASS after every apply, and after the rollback pg_dump -s equals the
-- dump at 055 (apart from pg_dump's random \restrict line). The 044, 046, 047,
-- 053, 054 and 055 assertion files print the same labels with and without 056,
-- and 052_credit_ledger_assertions.sql, run its header's way (seeded at 051, then
-- 052 → 055, with and without 056, on a cluster on port 5432 for its dblink
-- races), prints 349 PASS both times.
-- 2026-11-13 (merged with 057-059): the accounts carry 058's provisioning
-- marker, and a table 057 closes to every request role counts as the stricter
-- outcome (`pg_temp.ok`). Re-run the same way: at 055 20 PASS, 16 FAIL; at 056
-- 36 PASS; with every migration through 059 applied, 36 PASS.
--
-- Expected: 36 PASS, 0 FAIL (T1..T15; T1a..T1e, T2a/T2b .. T12a/T12b,
-- T13a..T13c, T14a/T14b, T15a..T15d).
\set ON_ERROR_STOP off
\pset pager off

-- Five accounts: an admin, two teachers with a classroom each, a student in each.
-- The accounts carry the server's provisioning marker (058, 2026-11-13: an
-- account without it is refused at COMMIT); before 058 it changes nothing.
INSERT INTO auth.users (id, instance_id, aud, role, email, encrypted_password, raw_app_meta_data, created_at, updated_at)
VALUES ('b5600001-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 'a1-056@x.test', '', '{"edubotics_provisioned": true}'::jsonb, now(), now()),
       ('b5600002-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 't1-056@x.test', '', '{"edubotics_provisioned": true}'::jsonb, now(), now()),
       ('b5600003-0000-0000-0000-000000000003', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 's1-056@x.test', '', '{"edubotics_provisioned": true}'::jsonb, now(), now()),
       ('b5600004-0000-0000-0000-000000000004', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 't2-056@x.test', '', '{"edubotics_provisioned": true}'::jsonb, now(), now()),
       ('b5600005-0000-0000-0000-000000000005', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 's2-056@x.test', '', '{"edubotics_provisioned": true}'::jsonb, now(), now())
ON CONFLICT (id) DO NOTHING;

UPDATE public.users SET role = 'admin', username = 'admin056' WHERE id = 'b5600001-0000-0000-0000-000000000001';
UPDATE public.users SET role = 'teacher', username = 'lehrer056a' WHERE id = 'b5600002-0000-0000-0000-000000000002';
UPDATE public.users SET role = 'teacher', username = 'lehrer056b' WHERE id = 'b5600004-0000-0000-0000-000000000004';
INSERT INTO public.classrooms (id, teacher_id, name)
VALUES ('c5600001-0000-0000-0000-000000000001', 'b5600002-0000-0000-0000-000000000002', 'Klasse 056 A'),
       ('c5600002-0000-0000-0000-000000000002', 'b5600004-0000-0000-0000-000000000004', 'Klasse 056 B')
ON CONFLICT (id) DO NOTHING;
UPDATE public.users SET classroom_id = 'c5600001-0000-0000-0000-000000000001', username = 'anna056'
 WHERE id = 'b5600003-0000-0000-0000-000000000003';
UPDATE public.users SET classroom_id = 'c5600002-0000-0000-0000-000000000002', username = 'ben056'
 WHERE id = 'b5600005-0000-0000-0000-000000000005';

-- One row per admin-readable table, all of them the SECOND student's (s2), so an
-- admin can see them only through the admin branch.
INSERT INTO public.workgroups (id, classroom_id, name)
VALUES ('d5600001-0000-0000-0000-000000000001', 'c5600002-0000-0000-0000-000000000002', 'Gruppe 056')
ON CONFLICT (id) DO NOTHING;
INSERT INTO public.workgroup_memberships (workgroup_id, user_id)
VALUES ('d5600001-0000-0000-0000-000000000001', 'b5600005-0000-0000-0000-000000000005')
ON CONFLICT DO NOTHING;
INSERT INTO public.datasets (id, owner_user_id, workgroup_id, hf_repo_id, name)
VALUES ('e5600001-0000-0000-0000-000000000001', 'b5600005-0000-0000-0000-000000000005',
        'd5600001-0000-0000-0000-000000000001', 'ben056/omx_f_wuerfel', 'Würfel 056')
ON CONFLICT (id) DO NOTHING;
INSERT INTO public.workflows (id, owner_user_id, classroom_id, name, blockly_json)
VALUES ('f5600001-0000-0000-0000-000000000001', 'b5600005-0000-0000-0000-000000000005',
        'c5600002-0000-0000-0000-000000000002', 'Programm 056', '{}'::jsonb)
ON CONFLICT (id) DO NOTHING;
INSERT INTO public.workflow_versions (id, workflow_id, blockly_json)
VALUES ('f5600002-0000-0000-0000-000000000002', 'f5600001-0000-0000-0000-000000000001', '{}'::jsonb)
ON CONFLICT (id) DO NOTHING;
INSERT INTO public.workflow_trajectories (id, workflow_id, owner_user_id, name, samples)
VALUES ('f5600003-0000-0000-0000-000000000003', 'f5600001-0000-0000-0000-000000000001',
        'b5600005-0000-0000-0000-000000000005', 'Bewegung 056', '{"fps": 25, "points": []}'::jsonb)
ON CONFLICT (id) DO NOTHING;
INSERT INTO public.workflow_submissions (id, workflow_id, student_user_id, classroom_id, name)
VALUES ('f5600004-0000-0000-0000-000000000004', 'f5600001-0000-0000-0000-000000000001',
        'b5600005-0000-0000-0000-000000000005', 'c5600002-0000-0000-0000-000000000002', 'Abgabe 056')
ON CONFLICT (id) DO NOTHING;
INSERT INTO public.progress_entries (id, classroom_id, student_id, note)
VALUES ('f5600005-0000-0000-0000-000000000005', 'c5600002-0000-0000-0000-000000000002',
        'b5600005-0000-0000-0000-000000000005', 'Fortschritt 056')
ON CONFLICT (id) DO NOTHING;
INSERT INTO public.tutorial_progress (user_id, tutorial_id, current_step)
VALUES ('b5600005-0000-0000-0000-000000000005', 'tutorial-056', 2)
ON CONFLICT DO NOTHING;

-- `claims` builds a JWT claim set; `as_user` runs one SELECT as that request
-- and answers its single value (or the SQLSTATE of an error).
CREATE OR REPLACE FUNCTION pg_temp.claims(p_sub TEXT, p_aal TEXT) RETURNS TEXT LANGUAGE sql AS $$
  SELECT CASE WHEN p_aal IS NULL
              THEN json_build_object('sub', p_sub, 'role', 'authenticated')::text
              ELSE json_build_object('sub', p_sub, 'role', 'authenticated', 'aal', p_aal)::text END;
$$;

CREATE OR REPLACE FUNCTION pg_temp.as_user(p_sub TEXT, p_aal TEXT, p_sql TEXT) RETURNS TEXT LANGUAGE plpgsql AS $$
DECLARE v TEXT;
BEGIN
  PERFORM set_config('request.jwt.claim.sub', coalesce(p_sub, ''), true);
  PERFORM set_config('request.jwt.claims', CASE WHEN p_sub IS NULL THEN '' ELSE pg_temp.claims(p_sub, p_aal) END, true);
  SET LOCAL ROLE authenticated;
  EXECUTE p_sql INTO v;
  RESET ROLE;
  RETURN v;
EXCEPTION WHEN OTHERS THEN
  RETURN 'ERR ' || SQLSTATE;
END $$;

-- Seeded rows of one table a caller can see: `n(<sub>, <aal>, <table>, <id column>, <seed ids>)`.
CREATE OR REPLACE FUNCTION pg_temp.n(p_sub TEXT, p_aal TEXT, p_table TEXT, p_col TEXT, p_ids TEXT) RETURNS TEXT
LANGUAGE sql AS $$
  SELECT pg_temp.as_user(p_sub, p_aal,
    format('SELECT count(*)::text FROM public.%I WHERE %I::text = ANY (string_to_array(%L, '',''))',
           p_table, p_col, p_ids));
$$;

-- From 057 (2026-11-13, the request roles default-deny) on, a table no shipped
-- client reads is closed to every request role: the read answers 42501 instead
-- of a row count. That is the stricter outcome for every case below, so
-- `ok` accepts it, but ONLY when the request roles really hold no SELECT on
-- the column read (on a table they may read, only the expected count passes).
CREATE OR REPLACE FUNCTION pg_temp.ok(p_actual TEXT, p_expected TEXT, p_table TEXT, p_col TEXT) RETURNS BOOLEAN
LANGUAGE sql AS $$
  SELECT p_actual = p_expected
      OR (p_actual = 'ERR 42501'
          AND NOT has_column_privilege('authenticated', format('public.%I', p_table), p_col, 'SELECT'));
$$;

\set A '''b5600001-0000-0000-0000-000000000001'''
\set T1 '''b5600002-0000-0000-0000-000000000002'''
\set S1 '''b5600003-0000-0000-0000-000000000003'''
\set T2 '''b5600004-0000-0000-0000-000000000004'''
\set S2 '''b5600005-0000-0000-0000-000000000005'''
\set USERS '''b5600001-0000-0000-0000-000000000001,b5600002-0000-0000-0000-000000000002,b5600003-0000-0000-0000-000000000003,b5600004-0000-0000-0000-000000000004,b5600005-0000-0000-0000-000000000005'''

-- T1: the helper itself.
SELECT CASE WHEN pg_temp.as_user(:A, 'aal1', 'SELECT public.rls_is_admin()::text') = 'false'
            THEN 'PASS T1a admin with only a password is not an admin to RLS'
            ELSE 'FAIL T1a aal1 admin: ' || pg_temp.as_user(:A, 'aal1', 'SELECT public.rls_is_admin()::text') END;
SELECT CASE WHEN pg_temp.as_user(:A, 'aal2', 'SELECT public.rls_is_admin()::text') = 'true'
            THEN 'PASS T1b admin in a two-factor session is an admin'
            ELSE 'FAIL T1b aal2 admin: ' || pg_temp.as_user(:A, 'aal2', 'SELECT public.rls_is_admin()::text') END;
SELECT CASE WHEN pg_temp.as_user(:A, NULL, 'SELECT public.rls_is_admin()::text') = 'false'
            THEN 'PASS T1c a token without an aal claim is not two-factor'
            ELSE 'FAIL T1c no aal claim: ' || pg_temp.as_user(:A, NULL, 'SELECT public.rls_is_admin()::text') END;
SELECT CASE WHEN pg_temp.as_user(:T1, 'aal2', 'SELECT public.rls_is_admin()::text') = 'false'
            THEN 'PASS T1d a teacher with aal2 is no admin'
            ELSE 'FAIL T1d teacher aal2: ' || pg_temp.as_user(:T1, 'aal2', 'SELECT public.rls_is_admin()::text') END;
SELECT CASE WHEN pg_temp.as_user(NULL, NULL, 'SELECT public.rls_is_admin()::text') = 'false'
            THEN 'PASS T1e no claims at all is no admin'
            ELSE 'FAIL T1e no claims: ' || pg_temp.as_user(NULL, NULL, 'SELECT public.rls_is_admin()::text') END;

-- T2: users — an aal1 admin sees its own row only, an aal2 admin all five.
SELECT CASE WHEN pg_temp.n(:A, 'aal1', 'users', 'id', :USERS) = '1'
            THEN 'PASS T2a users: aal1 admin sees only its own row'
            ELSE 'FAIL T2a users aal1: ' || pg_temp.n(:A, 'aal1', 'users', 'id', :USERS) END;
SELECT CASE WHEN pg_temp.n(:A, 'aal2', 'users', 'id', :USERS) = '5'
            THEN 'PASS T2b users: aal2 admin sees every row'
            ELSE 'FAIL T2b users aal2: ' || pg_temp.n(:A, 'aal2', 'users', 'id', :USERS) END;

-- T3..T12: one table each — nothing at aal1, the seeded row(s) at aal2.
SELECT CASE WHEN pg_temp.ok(pg_temp.n(:A, 'aal1', 'classrooms', 'id', 'c5600001-0000-0000-0000-000000000001,c5600002-0000-0000-0000-000000000002'), '0', 'classrooms', 'id')
            THEN 'PASS T3a classrooms: aal1 admin sees none' ELSE 'FAIL T3a classrooms aal1' END;
SELECT CASE WHEN pg_temp.ok(pg_temp.n(:A, 'aal2', 'classrooms', 'id', 'c5600001-0000-0000-0000-000000000001,c5600002-0000-0000-0000-000000000002'), '2', 'classrooms', 'id')
            THEN 'PASS T3b classrooms: aal2 admin sees both' ELSE 'FAIL T3b classrooms aal2' END;
SELECT CASE WHEN pg_temp.ok(pg_temp.n(:A, 'aal1', 'workgroups', 'id', 'd5600001-0000-0000-0000-000000000001'), '0', 'workgroups', 'id')
            THEN 'PASS T4a workgroups: aal1 admin sees none' ELSE 'FAIL T4a workgroups aal1' END;
SELECT CASE WHEN pg_temp.ok(pg_temp.n(:A, 'aal2', 'workgroups', 'id', 'd5600001-0000-0000-0000-000000000001'), '1', 'workgroups', 'id')
            THEN 'PASS T4b workgroups: aal2 admin sees it' ELSE 'FAIL T4b workgroups aal2' END;
SELECT CASE WHEN pg_temp.ok(pg_temp.n(:A, 'aal1', 'workgroup_memberships', 'user_id', 'b5600005-0000-0000-0000-000000000005'), '0', 'workgroup_memberships', 'user_id')
            THEN 'PASS T5a memberships: aal1 admin sees none' ELSE 'FAIL T5a memberships aal1' END;
SELECT CASE WHEN pg_temp.ok(pg_temp.n(:A, 'aal2', 'workgroup_memberships', 'user_id', 'b5600005-0000-0000-0000-000000000005'), '1', 'workgroup_memberships', 'user_id')
            THEN 'PASS T5b memberships: aal2 admin sees it' ELSE 'FAIL T5b memberships aal2' END;
SELECT CASE WHEN pg_temp.ok(pg_temp.n(:A, 'aal1', 'datasets', 'id', 'e5600001-0000-0000-0000-000000000001'), '0', 'datasets', 'id')
            THEN 'PASS T6a datasets: aal1 admin sees none' ELSE 'FAIL T6a datasets aal1' END;
SELECT CASE WHEN pg_temp.ok(pg_temp.n(:A, 'aal2', 'datasets', 'id', 'e5600001-0000-0000-0000-000000000001'), '1', 'datasets', 'id')
            THEN 'PASS T6b datasets: aal2 admin sees it' ELSE 'FAIL T6b datasets aal2' END;
SELECT CASE WHEN pg_temp.ok(pg_temp.n(:A, 'aal1', 'workflows', 'id', 'f5600001-0000-0000-0000-000000000001'), '0', 'workflows', 'id')
            THEN 'PASS T7a workflows: aal1 admin sees none' ELSE 'FAIL T7a workflows aal1' END;
SELECT CASE WHEN pg_temp.ok(pg_temp.n(:A, 'aal2', 'workflows', 'id', 'f5600001-0000-0000-0000-000000000001'), '1', 'workflows', 'id')
            THEN 'PASS T7b workflows: aal2 admin sees it' ELSE 'FAIL T7b workflows aal2' END;
SELECT CASE WHEN pg_temp.ok(pg_temp.n(:A, 'aal1', 'workflow_versions', 'id', 'f5600002-0000-0000-0000-000000000002'), '0', 'workflow_versions', 'id')
            THEN 'PASS T8a workflow_versions: aal1 admin sees none' ELSE 'FAIL T8a workflow_versions aal1' END;
SELECT CASE WHEN pg_temp.ok(pg_temp.n(:A, 'aal2', 'workflow_versions', 'id', 'f5600002-0000-0000-0000-000000000002'), '1', 'workflow_versions', 'id')
            THEN 'PASS T8b workflow_versions: aal2 admin sees it' ELSE 'FAIL T8b workflow_versions aal2' END;
SELECT CASE WHEN pg_temp.ok(pg_temp.n(:A, 'aal1', 'workflow_trajectories', 'id', 'f5600003-0000-0000-0000-000000000003'), '0', 'workflow_trajectories', 'id')
            THEN 'PASS T9a workflow_trajectories: aal1 admin sees none' ELSE 'FAIL T9a workflow_trajectories aal1' END;
SELECT CASE WHEN pg_temp.ok(pg_temp.n(:A, 'aal2', 'workflow_trajectories', 'id', 'f5600003-0000-0000-0000-000000000003'), '1', 'workflow_trajectories', 'id')
            THEN 'PASS T9b workflow_trajectories: aal2 admin sees it' ELSE 'FAIL T9b workflow_trajectories aal2' END;
SELECT CASE WHEN pg_temp.ok(pg_temp.n(:A, 'aal1', 'workflow_submissions', 'id', 'f5600004-0000-0000-0000-000000000004'), '0', 'workflow_submissions', 'id')
            THEN 'PASS T10a workflow_submissions: aal1 admin sees none' ELSE 'FAIL T10a workflow_submissions aal1' END;
SELECT CASE WHEN pg_temp.ok(pg_temp.n(:A, 'aal2', 'workflow_submissions', 'id', 'f5600004-0000-0000-0000-000000000004'), '1', 'workflow_submissions', 'id')
            THEN 'PASS T10b workflow_submissions: aal2 admin sees it' ELSE 'FAIL T10b workflow_submissions aal2' END;
SELECT CASE WHEN pg_temp.ok(pg_temp.n(:A, 'aal1', 'progress_entries', 'id', 'f5600005-0000-0000-0000-000000000005'), '0', 'progress_entries', 'id')
            THEN 'PASS T11a progress_entries: aal1 admin sees none' ELSE 'FAIL T11a progress_entries aal1' END;
SELECT CASE WHEN pg_temp.ok(pg_temp.n(:A, 'aal2', 'progress_entries', 'id', 'f5600005-0000-0000-0000-000000000005'), '1', 'progress_entries', 'id')
            THEN 'PASS T11b progress_entries: aal2 admin sees it' ELSE 'FAIL T11b progress_entries aal2' END;
SELECT CASE WHEN pg_temp.ok(pg_temp.n(:A, 'aal1', 'tutorial_progress', 'user_id', 'b5600005-0000-0000-0000-000000000005'), '0', 'tutorial_progress', 'user_id')
            THEN 'PASS T12a tutorial_progress: aal1 admin sees none' ELSE 'FAIL T12a tutorial_progress aal1' END;
SELECT CASE WHEN pg_temp.ok(pg_temp.n(:A, 'aal2', 'tutorial_progress', 'user_id', 'b5600005-0000-0000-0000-000000000005'), '1', 'tutorial_progress', 'user_id')
            THEN 'PASS T12b tutorial_progress: aal2 admin sees it' ELSE 'FAIL T12b tutorial_progress aal2' END;

-- T13: a teacher is unchanged, with or without a second factor.
SELECT CASE WHEN pg_temp.n(:T1, 'aal1', 'users', 'id', :USERS) = '2'
            THEN 'PASS T13a teacher (aal1) sees itself and its own student'
            ELSE 'FAIL T13a teacher aal1 users: ' || pg_temp.n(:T1, 'aal1', 'users', 'id', :USERS) END;
SELECT CASE WHEN pg_temp.n(:T1, 'aal2', 'users', 'id', :USERS) = '2'
            THEN 'PASS T13b teacher (aal2) sees the same two'
            ELSE 'FAIL T13b teacher aal2 users: ' || pg_temp.n(:T1, 'aal2', 'users', 'id', :USERS) END;
SELECT CASE WHEN pg_temp.ok(pg_temp.n(:T2, 'aal1', 'datasets', 'id', 'e5600001-0000-0000-0000-000000000001'), '1', 'datasets', 'id')
              AND pg_temp.ok(pg_temp.n(:T2, 'aal1', 'workgroups', 'id', 'd5600001-0000-0000-0000-000000000001'), '1', 'workgroups', 'id')
              AND pg_temp.ok(pg_temp.n(:T2, 'aal1', 'progress_entries', 'id', 'f5600005-0000-0000-0000-000000000005'), '1', 'progress_entries', 'id')
              AND pg_temp.ok(pg_temp.n(:T1, 'aal2', 'datasets', 'id', 'e5600001-0000-0000-0000-000000000001'), '0', 'datasets', 'id')
            THEN 'PASS T13c a teacher reads their own classroom''s rows and no other'
            ELSE 'FAIL T13c teacher rows' END;

-- T14: a student is unchanged.
SELECT CASE WHEN pg_temp.n(:S2, 'aal1', 'users', 'id', :USERS) = '1'
            THEN 'PASS T14a a student sees only its own users row'
            ELSE 'FAIL T14a student users: ' || pg_temp.n(:S2, 'aal1', 'users', 'id', :USERS) END;
SELECT CASE WHEN pg_temp.ok(pg_temp.n(:S2, 'aal1', 'datasets', 'id', 'e5600001-0000-0000-0000-000000000001'), '1', 'datasets', 'id')
              AND pg_temp.ok(pg_temp.n(:S2, 'aal1', 'workflows', 'id', 'f5600001-0000-0000-0000-000000000001'), '1', 'workflows', 'id')
              AND pg_temp.ok(pg_temp.n(:S2, 'aal1', 'workflow_submissions', 'id', 'f5600004-0000-0000-0000-000000000004'), '1', 'workflow_submissions', 'id')
              AND pg_temp.ok(pg_temp.n(:S2, 'aal1', 'tutorial_progress', 'user_id', 'b5600005-0000-0000-0000-000000000005'), '1', 'tutorial_progress', 'user_id')
              AND pg_temp.ok(pg_temp.n(:S1, 'aal2', 'workflows', 'id', 'f5600001-0000-0000-0000-000000000001'), '0', 'workflows', 'id')
            THEN 'PASS T14b a student reads its own rows, another student none of them'
            ELSE 'FAIL T14b student rows' END;

-- T15: the shape the self-check also asserts.
SELECT CASE WHEN NOT EXISTS (
         SELECT 1 FROM pg_policies WHERE schemaname = 'public'
            AND (coalesce(qual, '') ~ '''admin''::user_role' OR coalesce(with_check, '') ~ '''admin''::user_role'))
            THEN 'PASS T15a no policy decides "admin" by itself'
            ELSE 'FAIL T15a inline admin checks left' END;
SELECT CASE WHEN (SELECT count(*) FROM pg_policies WHERE schemaname = 'public'
                     AND coalesce(qual, '') ~ 'rls_is_admin\(\)') = 11
            THEN 'PASS T15b eleven policies read the admin branch through the helper'
            ELSE 'FAIL T15b ' || (SELECT count(*) FROM pg_policies WHERE schemaname = 'public'
                                     AND coalesce(qual, '') ~ 'rls_is_admin\(\)')::text || ' policies use the helper' END;
SELECT CASE WHEN p.prosecdef AND p.proconfig = ARRAY['search_path=public'] AND position('''aal2''' IN p.prosrc) > 0
            THEN 'PASS T15c the helper is SECURITY DEFINER, search_path public, requires aal2'
            ELSE 'FAIL T15c the helper''s shape' END
  FROM pg_proc p WHERE p.oid = 'public.rls_is_admin()'::regprocedure;
-- anon must be able to evaluate it only while anon may read something; 057
-- leaves anon no read at all and takes its EXECUTE away with it.
SELECT CASE WHEN has_function_privilege('authenticated', 'public.rls_is_admin()', 'EXECUTE')
              AND (has_function_privilege('anon', 'public.rls_is_admin()', 'EXECUTE')
                   OR NOT EXISTS (SELECT 1 FROM information_schema.column_privileges
                                   WHERE grantee = 'anon' AND table_schema = 'public'
                                     AND privilege_type = 'SELECT'))
            THEN 'PASS T15d every request role that reads may still evaluate it inside a policy'
            ELSE 'FAIL T15d the helper''s grants' END;

-- Clean-up: every seeded row (the accounts cascade to public.users).
DELETE FROM public.tutorial_progress WHERE user_id = 'b5600005-0000-0000-0000-000000000005';
DELETE FROM public.progress_entries WHERE id = 'f5600005-0000-0000-0000-000000000005';
DELETE FROM public.workflows WHERE id = 'f5600001-0000-0000-0000-000000000001';
DELETE FROM public.datasets WHERE id = 'e5600001-0000-0000-0000-000000000001';
UPDATE public.users SET workgroup_id = NULL, classroom_id = NULL
 WHERE id IN ('b5600003-0000-0000-0000-000000000003', 'b5600005-0000-0000-0000-000000000005');
DELETE FROM public.workgroup_memberships WHERE workgroup_id = 'd5600001-0000-0000-0000-000000000001';
DELETE FROM public.workgroups WHERE id = 'd5600001-0000-0000-0000-000000000001';
DELETE FROM public.classrooms WHERE id IN ('c5600001-0000-0000-0000-000000000001', 'c5600002-0000-0000-0000-000000000002');
DELETE FROM auth.users WHERE id IN ('b5600001-0000-0000-0000-000000000001', 'b5600002-0000-0000-0000-000000000002',
                                    'b5600003-0000-0000-0000-000000000003', 'b5600004-0000-0000-0000-000000000004',
                                    'b5600005-0000-0000-0000-000000000005');

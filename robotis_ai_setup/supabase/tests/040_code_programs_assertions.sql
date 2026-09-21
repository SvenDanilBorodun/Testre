-- Behavioural assertions for migration 040 (Roboter Studio code programs +
-- „Abgeben"), run BY HAND against a fresh LOCAL stack — never CI, never the
-- linked project (it seeds rows and deletes a workflow):
--   supabase start && supabase db reset --local   (scratch project copy)
--   psql <local db url> < supabase/tests/040_code_programs_assertions.sql
-- Expected: 16 PASS, 0 FAIL/ERROR (T1..T10, some with a/b/c/d halves).
\set ON_ERROR_STOP off
\pset pager off

-- seed: one classroom, one teacher, two students (auth.users first: users.id FKs it)
INSERT INTO auth.users (id, instance_id, aud, role, email, encrypted_password, created_at, updated_at)
VALUES ('11111111-1111-1111-1111-111111111111', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 's1@x.test', '', now(), now()),
       ('22222222-2222-2222-2222-222222222222', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 's2@x.test', '', now(), now()),
       ('33333333-3333-3333-3333-333333333333', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 't@x.test', '', now(), now())
ON CONFLICT (id) DO NOTHING;
-- the baseline's auth trigger already mirrored the three ids into public.users;
-- set the roles / classroom on those rows instead of inserting.
UPDATE public.users SET role = 'teacher' WHERE id = '33333333-3333-3333-3333-333333333333';
INSERT INTO public.classrooms (id, teacher_id, name) VALUES
  ('cccccccc-cccc-cccc-cccc-cccccccccccc', '33333333-3333-3333-3333-333333333333', 'Klasse 7b') ON CONFLICT (id) DO NOTHING;
UPDATE public.users SET role = 'student', classroom_id = 'cccccccc-cccc-cccc-cccc-cccccccccccc'
 WHERE id IN ('11111111-1111-1111-1111-111111111111', '22222222-2222-2222-2222-222222222222');
INSERT INTO public.workflows (id, owner_user_id, classroom_id, name, blockly_json)
VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '11111111-1111-1111-1111-111111111111',
        'cccccccc-cccc-cccc-cccc-cccccccccccc', 'Mein Programm', '{}'::jsonb)
ON CONFLICT (id) DO NOTHING;

\echo '=== T1 defaults: code_language '''' and code_files {} on an existing row ==='
SELECT CASE WHEN code_language = '' AND code_files = '{}'::jsonb THEN 'PASS T1' ELSE 'FAIL T1' END
  FROM public.workflows WHERE id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';

\echo '=== T2 update_workflow_code by the OWNER writes code + language and snapshots the OLD state with saved_by ==='
SELECT (public.update_workflow_code('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '11111111-1111-1111-1111-111111111111',
        '{"main.py": "import robot\nrobot.home()\n"}'::jsonb, 'python')).code_language AS lang;
SELECT CASE WHEN count(*) = 1 AND bool_and(saved_by = '11111111-1111-1111-1111-111111111111')
                 AND bool_and(code_files = '{}'::jsonb) AND bool_and(code_language = '')
            THEN 'PASS T2' ELSE 'FAIL T2 (' || count(*) || ' versions)' END
  FROM public.workflow_versions WHERE workflow_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';

\echo '=== T3 a second code save snapshots the FIRST code (version history for code, B8) ==='
SELECT (public.update_workflow_code('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '11111111-1111-1111-1111-111111111111',
        '{"main.py": "import robot\nrobot.open_gripper()\n"}'::jsonb, 'python')).id IS NOT NULL AS ok;
SELECT CASE WHEN count(*) = 2 AND count(*) FILTER (WHERE code_files ? 'main.py') = 1
            THEN 'PASS T3' ELSE 'FAIL T3 (' || count(*) || ')' END
  FROM public.workflow_versions WHERE workflow_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';

\echo '=== T4 restore of a code version restores code_files (and a pre-040 NULL version leaves code alone) ==='
SELECT (public.restore_workflow_version('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
        (SELECT id FROM public.workflow_versions WHERE workflow_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa' AND code_files ? 'main.py'),
        '11111111-1111-1111-1111-111111111111')).code_files ->> 'main.py' AS restored;
SELECT CASE WHEN code_files ->> 'main.py' = E'import robot\nrobot.home()\n' THEN 'PASS T4a' ELSE 'FAIL T4a' END
  FROM public.workflows WHERE id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
-- simulate a legacy version row (NULL code columns) and restore it
INSERT INTO public.workflow_versions (workflow_id, blockly_json, note) VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '{"legacy": true}'::jsonb, 'legacy');
SELECT (public.restore_workflow_version('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
        (SELECT id FROM public.workflow_versions WHERE note = 'legacy' LIMIT 1),
        '11111111-1111-1111-1111-111111111111')).blockly_json AS bj;
SELECT CASE WHEN code_language = 'python' AND code_files ? 'main.py' AND blockly_json = '{"legacy": true}'::jsonb
            THEN 'PASS T4b' ELSE 'FAIL T4b' END
  FROM public.workflows WHERE id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';

\echo '=== T5 IDOR: a NON-owner cannot write code (P0002), even a classmate ==='
DO $$ BEGIN
  PERFORM public.update_workflow_code('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '22222222-2222-2222-2222-222222222222', '{"main.py": "x"}'::jsonb, 'python');
  RAISE NOTICE 'FAIL T5 (write accepted)';
EXCEPTION WHEN SQLSTATE 'P0002' THEN RAISE NOTICE 'PASS T5 (%)', SQLERRM;
END $$;

\echo '=== T6 caps: a 33-file project, a 65 KiB file, a non-string value, a bad language — all refused ==='
DO $$ DECLARE j JSONB := '{}'::jsonb; i INT; BEGIN
  FOR i IN 1..33 LOOP j := j || jsonb_build_object('f' || i || '.py', 'x'); END LOOP;
  BEGIN
    PERFORM public.update_workflow_code('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '11111111-1111-1111-1111-111111111111', j, 'python');
    RAISE NOTICE 'FAIL T6a (33 files accepted)';
  EXCEPTION WHEN check_violation THEN RAISE NOTICE 'PASS T6a (33 files: check_violation)'; END;
  BEGIN
    PERFORM public.update_workflow_code('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '11111111-1111-1111-1111-111111111111',
        jsonb_build_object('main.py', repeat('a', 65537)), 'python');
    RAISE NOTICE 'FAIL T6b (65 KiB + 1 file accepted)';
  EXCEPTION WHEN check_violation THEN RAISE NOTICE 'PASS T6b (oversized file: check_violation)'; END;
  BEGIN
    UPDATE public.workflows SET code_files = '{"main.py": 5}'::jsonb WHERE id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
    RAISE NOTICE 'FAIL T6c (non-string value accepted by a direct UPDATE)';
  EXCEPTION WHEN check_violation THEN RAISE NOTICE 'PASS T6c (non-string value: check_violation)'; END;
  BEGIN
    PERFORM public.update_workflow_code('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '11111111-1111-1111-1111-111111111111', '{"a.js": "x"}'::jsonb, 'javascript');
    RAISE NOTICE 'FAIL T6d (javascript accepted)';
  EXCEPTION WHEN SQLSTATE '22023' THEN RAISE NOTICE 'PASS T6d (%)', SQLERRM; END;
END $$;

\echo '=== T7 submissions: insert 10 for one (student, workflow) -> 8 kept, newest first; UPDATE refused; DELETE cascades ==='
DO $$ DECLARE i INT; BEGIN
  FOR i IN 1..10 LOOP
    INSERT INTO public.workflow_submissions (workflow_id, student_user_id, classroom_id, name, code_language, code_files, note)
    VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '11111111-1111-1111-1111-111111111111', 'cccccccc-cccc-cccc-cccc-cccccccccccc',
            'Mein Programm', 'python', jsonb_build_object('main.py', 'v' || i), 'Abgabe ' || i);
  END LOOP;
END $$;
SELECT CASE WHEN count(*) = 8 AND min(note) = 'Abgabe 10' AND bool_and(note <> 'Abgabe 1') AND bool_and(note <> 'Abgabe 2')
            THEN 'PASS T7a' ELSE 'FAIL T7a (' || count(*) || ')' END
  FROM public.workflow_submissions WHERE student_user_id = '11111111-1111-1111-1111-111111111111';
DO $$ BEGIN
  UPDATE public.workflow_submissions SET note = 'geändert' WHERE student_user_id = '11111111-1111-1111-1111-111111111111';
  RAISE NOTICE 'FAIL T7b (UPDATE accepted)';
EXCEPTION WHEN SQLSTATE 'P0001' THEN RAISE NOTICE 'PASS T7b (%)', SQLERRM; END $$;
DELETE FROM public.workflows WHERE id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
SELECT CASE WHEN count(*) = 0 THEN 'PASS T7c' ELSE 'FAIL T7c' END FROM public.workflow_submissions;

\echo '=== T8 grants: anon/authenticated cannot EXECUTE update_workflow_code; service_role can ==='
SELECT CASE WHEN NOT has_function_privilege('anon', 'public.update_workflow_code(uuid,uuid,jsonb,text)', 'EXECUTE')
             AND NOT has_function_privilege('authenticated', 'public.update_workflow_code(uuid,uuid,jsonb,text)', 'EXECUTE')
             AND has_function_privilege('service_role', 'public.update_workflow_code(uuid,uuid,jsonb,text)', 'EXECUTE')
            THEN 'PASS T8' ELSE 'FAIL T8' END;

\echo '=== T9 RLS: submissions RLS enabled, exactly two SELECT policies, no write policy ==='
SELECT CASE WHEN (SELECT relrowsecurity FROM pg_class WHERE oid = 'public.workflow_submissions'::regclass)
             AND (SELECT count(*) FROM pg_policies WHERE tablename = 'workflow_submissions' AND cmd = 'SELECT') = 2
             AND (SELECT count(*) FROM pg_policies WHERE tablename = 'workflow_submissions' AND cmd <> 'SELECT') = 0
            THEN 'PASS T9' ELSE 'FAIL T9' END;

\echo '=== T10 the boot-probe shapes the cloud API will send resolve (PGRST-equivalent: the columns exist) ==='
SELECT 'PASS T10' WHERE EXISTS (
  SELECT 1 FROM information_schema.columns WHERE table_name = 'workflows' AND column_name IN ('code_files', 'code_language') HAVING count(*) = 2)
  AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'workflow_versions' AND column_name IN ('code_files', 'code_language') HAVING count(*) = 2)
  AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'workflow_submissions' AND column_name IN ('id', 'student_user_id', 'workflow_id', 'classroom_id', 'code_language', 'submitted_at') HAVING count(*) = 6);

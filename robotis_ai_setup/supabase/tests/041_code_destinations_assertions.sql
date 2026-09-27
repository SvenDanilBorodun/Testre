-- Behavioural assertions for migration 041 (a code program keeps its Ziele and
-- Positionen in blockly_json['edubotics-destinations']), run BY HAND against a
-- fresh LOCAL stack — never CI, never the linked project (it seeds rows):
--   supabase start && supabase db reset --local   (scratch project copy)
--   psql <local db url> < supabase/tests/041_code_destinations_assertions.sql
-- Expected: 21 PASS, 0 FAIL/ERROR (T1..T12, some with a/b/c/d halves).
--
-- NOTE: 040_code_programs_assertions.sql's T8 names the 4-argument
-- update_workflow_code(uuid,uuid,jsonb,text), which 041 drops; after 041 that
-- one line ERRORs by design (T8 below is its successor).
\set ON_ERROR_STOP off
\pset pager off

-- seed: one classroom, one teacher, two students (auth.users first: users.id FKs it)
INSERT INTO auth.users (id, instance_id, aud, role, email, encrypted_password, created_at, updated_at)
VALUES ('41111111-1111-1111-1111-111111111111', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 's1-041@x.test', '', now(), now()),
       ('42222222-2222-2222-2222-222222222222', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 's2-041@x.test', '', now(), now()),
       ('43333333-3333-3333-3333-333333333333', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 't-041@x.test', '', now(), now())
ON CONFLICT (id) DO NOTHING;
UPDATE public.users SET role = 'teacher' WHERE id = '43333333-3333-3333-3333-333333333333';
INSERT INTO public.classrooms (id, teacher_id, name) VALUES
  ('4ccccccc-cccc-cccc-cccc-cccccccccccc', '43333333-3333-3333-3333-333333333333', 'Klasse 8a') ON CONFLICT (id) DO NOTHING;
UPDATE public.users SET role = 'student', classroom_id = '4ccccccc-cccc-cccc-cccc-cccccccccccc'
 WHERE id IN ('41111111-1111-1111-1111-111111111111', '42222222-2222-2222-2222-222222222222');
-- a Python program (code_language set at create, blockly_json {} — the 040 shape)
INSERT INTO public.workflows (id, owner_user_id, classroom_id, name, blockly_json, code_language, code_files)
VALUES ('4aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '41111111-1111-1111-1111-111111111111',
        '4ccccccc-cccc-cccc-cccc-cccccccccccc', 'Mein Python-Programm', '{}'::jsonb,
        'python', '{"main.py": "import robot\n"}'::jsonb)
ON CONFLICT (id) DO NOTHING;
-- a Blockly program (the CHECK must leave it alone)
INSERT INTO public.workflows (id, owner_user_id, classroom_id, name, blockly_json)
VALUES ('4bbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', '41111111-1111-1111-1111-111111111111',
        '4ccccccc-cccc-cccc-cccc-cccccccccccc', 'Meine Blöcke', '{"blocks": {"blocks": []}}'::jsonb)
ON CONFLICT (id) DO NOTHING;

\echo '=== T1 exactly one update_workflow_code, 5 arguments, the 5th defaulted ==='
SELECT CASE WHEN count(*) = 1 AND bool_and(pronargs = 5) AND bool_and(pronargdefaults = 1)
                 AND bool_and(pg_get_function_identity_arguments(oid)
                     = 'p_workflow_id uuid, p_user_id uuid, p_code_files jsonb, p_code_language text, p_blockly_json jsonb')
            THEN 'PASS T1' ELSE 'FAIL T1 (' || count(*) || ' rows)' END
  FROM pg_proc WHERE proname = 'update_workflow_code' AND pronamespace = 'public'::regnamespace;

\echo '=== T2 a 5-argument save writes code AND Ziele in ONE version snapshot, saved_by stamped ==='
SELECT (public.update_workflow_code(
          p_workflow_id => '4aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
          p_user_id => '41111111-1111-1111-1111-111111111111',
          p_code_files => '{"main.py": "import robot\nrobot.move_to(\"Ablage\")\n"}'::jsonb,
          p_code_language => 'python',
          p_blockly_json => '{"edubotics-destinations": {"version": 1, "entries": [{"name": "Ablage", "kind": "pin", "x": 0.2, "y": 0.0, "z": 0.0}]}}'::jsonb
        )).id IS NOT NULL AS ok;
SELECT CASE WHEN count(*) = 1 AND bool_and(saved_by = '41111111-1111-1111-1111-111111111111')
                 AND bool_and(blockly_json = '{}'::jsonb) AND bool_and(code_files ->> 'main.py' = E'import robot\n')
            THEN 'PASS T2a' ELSE 'FAIL T2a (' || count(*) || ' versions)' END
  FROM public.workflow_versions WHERE workflow_id = '4aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
SELECT CASE WHEN blockly_json -> 'edubotics-destinations' -> 'entries' -> 0 ->> 'name' = 'Ablage'
                 AND code_files ->> 'main.py' LIKE '%move_to%'
            THEN 'PASS T2b' ELSE 'FAIL T2b' END
  FROM public.workflows WHERE id = '4aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';

\echo '=== T3 an OLD API (4 named arguments, no p_blockly_json) still saves and leaves the Ziele untouched ==='
SELECT (public.update_workflow_code(
          p_workflow_id => '4aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
          p_user_id => '41111111-1111-1111-1111-111111111111',
          p_code_files => '{"main.py": "import robot\nrobot.home()\n"}'::jsonb,
          p_code_language => 'python'
        )).id IS NOT NULL AS ok;
SELECT CASE WHEN blockly_json -> 'edubotics-destinations' -> 'entries' -> 0 ->> 'name' = 'Ablage'
                 AND code_files ->> 'main.py' LIKE '%robot.home()%'
            THEN 'PASS T3' ELSE 'FAIL T3' END
  FROM public.workflows WHERE id = '4aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';

\echo '=== T4 an explicit NULL p_blockly_json (what the new API sends for a code-only PATCH) leaves the Ziele untouched ==='
SELECT (public.update_workflow_code('4aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '41111111-1111-1111-1111-111111111111',
        '{"main.py": "import robot\nrobot.open_gripper()\n"}'::jsonb, 'python', NULL)).id IS NOT NULL AS ok;
SELECT CASE WHEN blockly_json -> 'edubotics-destinations' -> 'entries' -> 0 ->> 'name' = 'Ablage'
            THEN 'PASS T4' ELSE 'FAIL T4' END
  FROM public.workflows WHERE id = '4aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';

\echo '=== T5 p_blockly_json may carry ONLY edubotics-destinations: a blocks key, an array and a scalar -> 22023 ==='
DO $$ BEGIN
  BEGIN
    PERFORM public.update_workflow_code('4aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '41111111-1111-1111-1111-111111111111',
        '{"main.py": "x"}'::jsonb, 'python', '{"blocks": {"blocks": []}, "edubotics-destinations": null}'::jsonb);
    RAISE NOTICE 'FAIL T5a (blocks key accepted)';
  EXCEPTION WHEN SQLSTATE '22023' THEN RAISE NOTICE 'PASS T5a (%)', SQLERRM; END;
  BEGIN
    PERFORM public.update_workflow_code('4aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '41111111-1111-1111-1111-111111111111',
        '{"main.py": "x"}'::jsonb, 'python', '[]'::jsonb);
    RAISE NOTICE 'FAIL T5b (array accepted)';
  EXCEPTION WHEN SQLSTATE '22023' THEN RAISE NOTICE 'PASS T5b (%)', SQLERRM; END;
  BEGIN
    PERFORM public.update_workflow_code('4aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '41111111-1111-1111-1111-111111111111',
        '{"main.py": "x"}'::jsonb, 'python', '"edubotics-destinations"'::jsonb);
    RAISE NOTICE 'FAIL T5c (scalar accepted)';
  EXCEPTION WHEN SQLSTATE '22023' THEN RAISE NOTICE 'PASS T5c (%)', SQLERRM; END;
END $$;
SELECT CASE WHEN code_files ->> 'main.py' LIKE '%open_gripper%' AND NOT (blockly_json ? 'blocks')
            THEN 'PASS T5d (nothing written by a refused call)' ELSE 'FAIL T5d' END
  FROM public.workflows WHERE id = '4aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';

\echo '=== T6 IDOR: a NON-owner cannot write code or Ziele (P0002), even a classmate ==='
DO $$ BEGIN
  PERFORM public.update_workflow_code('4aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '42222222-2222-2222-2222-222222222222',
      '{"main.py": "x"}'::jsonb, 'python', '{"edubotics-destinations": null}'::jsonb);
  RAISE NOTICE 'FAIL T6 (write accepted)';
EXCEPTION WHEN SQLSTATE 'P0002' THEN RAISE NOTICE 'PASS T6 (%)', SQLERRM;
END $$;

\echo '=== T7 the language check still binds with 5 arguments ==='
DO $$ BEGIN
  PERFORM public.update_workflow_code('4aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '41111111-1111-1111-1111-111111111111',
      '{"a.js": "x"}'::jsonb, 'javascript', '{}'::jsonb);
  RAISE NOTICE 'FAIL T7 (javascript accepted)';
EXCEPTION WHEN SQLSTATE '22023' THEN RAISE NOTICE 'PASS T7 (%)', SQLERRM;
END $$;

\echo '=== T8 grants: anon/authenticated cannot EXECUTE the 5-argument writer; service_role can; a privileged owner ==='
SELECT CASE WHEN NOT has_function_privilege('anon', 'public.update_workflow_code(uuid,uuid,jsonb,text,jsonb)', 'EXECUTE')
             AND NOT has_function_privilege('authenticated', 'public.update_workflow_code(uuid,uuid,jsonb,text,jsonb)', 'EXECUTE')
             AND has_function_privilege('service_role', 'public.update_workflow_code(uuid,uuid,jsonb,text,jsonb)', 'EXECUTE')
            THEN 'PASS T8a' ELSE 'FAIL T8a' END;
SELECT CASE WHEN proowner::regrole::text IN ('postgres', 'supabase_admin', 'supabase_storage_admin') AND prosecdef
            THEN 'PASS T8b' ELSE 'FAIL T8b (' || proowner::regrole::text || ')' END
  FROM pg_proc WHERE proname = 'update_workflow_code' AND pronamespace = 'public'::regnamespace;

\echo '=== T9 the CHECK: a direct write of blocks onto a code row is refused; Ziele are allowed; a Blockly row is untouched ==='
DO $$ BEGIN
  BEGIN
    UPDATE public.workflows SET blockly_json = '{"blocks": {"blocks": []}}'::jsonb
     WHERE id = '4aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
    RAISE NOTICE 'FAIL T9a (blocks on a code row accepted by a direct UPDATE)';
  EXCEPTION WHEN check_violation THEN RAISE NOTICE 'PASS T9a (blocks on a code row: check_violation)'; END;
  BEGIN
    UPDATE public.workflows SET blockly_json = '[1, 2]'::jsonb
     WHERE id = '4aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
    RAISE NOTICE 'FAIL T9b (a non-object blockly_json on a code row accepted)';
  EXCEPTION WHEN check_violation THEN RAISE NOTICE 'PASS T9b (non-object on a code row: check_violation)'; END;
  BEGIN
    INSERT INTO public.workflows (owner_user_id, name, blockly_json, code_language, code_files)
    VALUES ('41111111-1111-1111-1111-111111111111', 'Gemischt', '{"blocks": {}}'::jsonb, 'java', '{"Main.java": "x"}'::jsonb);
    RAISE NOTICE 'FAIL T9c (a mixed INSERT accepted)';
  EXCEPTION WHEN check_violation THEN RAISE NOTICE 'PASS T9c (mixed INSERT: check_violation)'; END;
END $$;
UPDATE public.workflows SET blockly_json = '{"blocks": {"blocks": [{"type": "edubotics_log"}]}, "variables": []}'::jsonb
 WHERE id = '4bbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';
SELECT CASE WHEN blockly_json ? 'blocks' THEN 'PASS T9d (a Blockly row keeps its blocks)' ELSE 'FAIL T9d' END
  FROM public.workflows WHERE id = '4bbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';

\echo '=== T10 the CHECK is VALIDATED (not left NOT VALID) ==='
SELECT CASE WHEN count(*) = 1 AND bool_and(convalidated) THEN 'PASS T10' ELSE 'FAIL T10 (' || count(*) || ')' END
  FROM pg_constraint WHERE conrelid = 'public.workflows'::regclass
   AND conname = 'workflows_code_blockly_json_destinations_only';

\echo '=== T11 restore_workflow_version brings a code version''s Ziele back ==='
-- current: Ablage. Save a second set (Kiste), then restore the version holding Ablage.
SELECT (public.update_workflow_code('4aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '41111111-1111-1111-1111-111111111111',
        '{"main.py": "import robot\nrobot.move_to(\"Kiste\")\n"}'::jsonb, 'python',
        '{"edubotics-destinations": {"version": 1, "entries": [{"name": "Kiste", "kind": "pin", "x": 0.1, "y": 0.1, "z": 0.0}]}}'::jsonb)).id IS NOT NULL AS ok;
SELECT CASE WHEN blockly_json -> 'edubotics-destinations' -> 'entries' -> 0 ->> 'name' = 'Kiste'
            THEN 'PASS T11a' ELSE 'FAIL T11a' END
  FROM public.workflows WHERE id = '4aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
SELECT (public.restore_workflow_version('4aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
        (SELECT id FROM public.workflow_versions
          WHERE workflow_id = '4aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
            AND blockly_json -> 'edubotics-destinations' -> 'entries' -> 0 ->> 'name' = 'Ablage'
          ORDER BY created_at DESC LIMIT 1),
        '41111111-1111-1111-1111-111111111111')).id IS NOT NULL AS ok;
SELECT CASE WHEN blockly_json -> 'edubotics-destinations' -> 'entries' -> 0 ->> 'name' = 'Ablage'
                 AND code_language = 'python'
            THEN 'PASS T11b' ELSE 'FAIL T11b' END
  FROM public.workflows WHERE id = '4aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';

\echo '=== T12 an empty document clears the Ziele (the store emptied by the student) ==='
SELECT (public.update_workflow_code('4aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '41111111-1111-1111-1111-111111111111',
        '{"main.py": "import robot\n"}'::jsonb, 'python', '{}'::jsonb)).id IS NOT NULL AS ok;
SELECT CASE WHEN blockly_json = '{}'::jsonb THEN 'PASS T12' ELSE 'FAIL T12' END
  FROM public.workflows WHERE id = '4aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';

-- clean up the seeded rows so the script can be re-run on the same stack
DELETE FROM public.workflows WHERE id IN ('4aaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '4bbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb');

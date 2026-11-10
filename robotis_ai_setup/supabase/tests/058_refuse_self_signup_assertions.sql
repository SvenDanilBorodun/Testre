-- Behavioural assertions for migration 058 (a new auth.users row commits only
-- when its raw_app_meta_data carries {"edubotics_provisioned": true}, checked by
-- the deferred constraint trigger trg_users_require_provisioned on public.users),
-- run BY HAND against a LOCAL database — never CI, never the linked project (it
-- inserts auth.users rows and deletes them again).
--
-- The baseline replays on a fresh database as shipped, so a local stack of the
-- checkout itself (`supabase start`, or every migration applied in order by
-- psql) is the database: with every migration through 058 applied, then
--   psql <local db url> -f supabase/tests/058_refuse_self_signup_assertions.sql
-- On a plain PostgreSQL, apply .github/scripts/supabase_replay_stub.sql first.
-- Supabase Auth's two shapes are replayed in SQL, as postgres: a public sign-up
-- INSERTs the row with app_metadata {"provider", "providers"} and commits; an
-- admin create (auth.admin.create_user with app_metadata) INSERTs the same row
-- and then UPDATEs app_metadata in the SAME transaction (GoTrue
-- internal/api/admin.go::adminUserCreate, read 2026-11-13). Most checks force
-- the deferred check inside a DO block with SET CONSTRAINTS … IMMEDIATE, which
-- checks every pending row at that point exactly as COMMIT would; T4 and T7
-- commit for real. No function runs under a request role.
-- 2026-11-13: run on PostgreSQL 16.15 with that stub, every migration 000 → 057
-- applied by psql, then 058: 14 PASS, 0 FAIL and 1 ERROR — T4's refused COMMIT,
-- which psql prints as `ERROR: account … self-registration is refused`
-- (expected; T4 then proves nothing was created). Against the database at 057
-- (or after the 058 rollback): 1 PASS (T7), 13 FAIL — T1a, T1b, T2, T3a–T3d, T4,
-- T5 and T8 (every sign-up commits), T6, T9 and T10 (their SET CONSTRAINTS names
-- a trigger that is not there) — and 0 ERROR. The cycle 058 → rollback →
-- rollback → 058 → 058 passes each self-check and prints 14 PASS after every
-- apply, and after the rollback pg_dump -s equals the dump at 057.
-- Every other assertion file inserts its auth.users fixtures with the marker
-- since 058, and prints the same with and without 058.
--
-- Expected: 14 PASS, 0 FAIL (T1..T10; T1a/T1b, T3a..T3d).
\set ON_ERROR_STOP off
\pset pager off

-- `signup_immediate(id, app, user)`: one INSERT the way Supabase Auth makes it,
-- checked at once (SET CONSTRAINTS … IMMEDIATE); answers 'refused' (42501),
-- 'created', or another SQLSTATE. Rolled back either way.
CREATE OR REPLACE FUNCTION pg_temp.signup_immediate(p_id UUID, p_app JSONB, p_user JSONB DEFAULT NULL)
RETURNS TEXT LANGUAGE plpgsql AS $$
BEGIN
  BEGIN
    INSERT INTO auth.users (id, instance_id, aud, role, email, encrypted_password, created_at, updated_at,
                            raw_app_meta_data, raw_user_meta_data)
    VALUES (p_id, '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
            p_id || '@x.test', '', now(), now(), p_app, p_user);
    SET CONSTRAINTS public.trg_users_require_provisioned IMMEDIATE;
    RAISE EXCEPTION USING ERRCODE = 'P0158', MESSAGE = 'created';
  EXCEPTION
    WHEN SQLSTATE 'P0158' THEN RETURN 'created';
    WHEN SQLSTATE '42501' THEN RETURN 'refused';
  END;
EXCEPTION WHEN OTHERS THEN
  RETURN SQLSTATE;
END $$;

\echo '=== T1 the check: SECURITY DEFINER, empty search_path, nobody executes it; a deferred AFTER INSERT ROW constraint trigger ==='
SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_proc p
                          WHERE p.oid = to_regprocedure('public.users_require_provisioned_account()')
                            AND p.prosecdef AND p.proconfig = ARRAY['search_path=""']
                            AND NOT has_function_privilege('anon', p.oid, 'EXECUTE')
                            AND NOT has_function_privilege('authenticated', p.oid, 'EXECUTE')
                            AND NOT has_function_privilege('service_role', p.oid, 'EXECUTE'))
            THEN 'PASS T1a' ELSE 'FAIL T1a' END;
SELECT CASE WHEN count(*) = 1 THEN 'PASS T1b' ELSE 'FAIL T1b (' || count(*) || ')' END
  FROM pg_trigger t
 WHERE t.tgrelid = 'public.users'::regclass AND t.tgname = 'trg_users_require_provisioned'
   AND t.tgconstraint <> 0 AND t.tgdeferrable AND t.tginitdeferred AND t.tgenabled = 'O'
   AND (t.tgtype & 1) = 1 AND (t.tgtype & 2) = 0 AND (t.tgtype & 4) = 4 AND (t.tgtype & 24) = 0;

\echo '=== T2 a public sign-up (provider-only app_metadata) is refused ==='
SELECT CASE WHEN pg_temp.signup_immediate('e5800002-0000-0000-0000-000000000001',
                                          '{"provider": "email", "providers": ["email"]}') = 'refused'
            THEN 'PASS T2' ELSE 'FAIL T2' END;

\echo '=== T3 only a JSON true counts: false, the string "true", no app_metadata, a non-object are refused ==='
SELECT CASE WHEN pg_temp.signup_immediate('e5800003-0000-0000-0000-000000000001',
                  '{"provider": "email", "edubotics_provisioned": false}') = 'refused' THEN 'PASS T3a' ELSE 'FAIL T3a' END;
SELECT CASE WHEN pg_temp.signup_immediate('e5800003-0000-0000-0000-000000000002',
                  '{"provider": "email", "edubotics_provisioned": "true"}') = 'refused' THEN 'PASS T3b' ELSE 'FAIL T3b' END;
SELECT CASE WHEN pg_temp.signup_immediate('e5800003-0000-0000-0000-000000000003', NULL) = 'refused'
            THEN 'PASS T3c' ELSE 'FAIL T3c' END;
SELECT CASE WHEN pg_temp.signup_immediate('e5800003-0000-0000-0000-000000000004', '[true]') = 'refused'
            THEN 'PASS T3d' ELSE 'FAIL T3d' END;

\echo '=== T4 a real COMMIT of a sign-up fails, and leaves neither row (the one expected ERROR) ==='
INSERT INTO auth.users (id, instance_id, aud, role, email, encrypted_password, created_at, updated_at, raw_app_meta_data)
VALUES ('e5800004-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
        'selbst-058@example.com', '', now(), now(), '{"provider": "email", "providers": ["email"]}');
SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM auth.users WHERE id = 'e5800004-0000-0000-0000-000000000001')
             AND NOT EXISTS (SELECT 1 FROM public.users WHERE id = 'e5800004-0000-0000-0000-000000000001')
            THEN 'PASS T4' ELSE 'FAIL T4 (the account exists)' END;

\echo '=== T5 the marker in user_metadata (which signUp lets the CLIENT set) does not count ==='
SELECT CASE WHEN pg_temp.signup_immediate('e5800005-0000-0000-0000-000000000001',
                  '{"provider": "email", "providers": ["email"]}', '{"edubotics_provisioned": true}') = 'refused'
            THEN 'PASS T5' ELSE 'FAIL T5' END;

\echo '=== T6 a row inserted WITH the marker passes the check ==='
SELECT CASE WHEN pg_temp.signup_immediate('e5800006-0000-0000-0000-000000000001',
                  '{"provider": "email", "providers": ["email"], "edubotics_provisioned": true}') = 'created'
            THEN 'PASS T6' ELSE 'FAIL T6' END;

\echo '=== T7 the server''s create (INSERT, then the app_metadata UPDATE, one transaction) commits for real ==='
BEGIN;
INSERT INTO auth.users (id, instance_id, aud, role, email, encrypted_password, created_at, updated_at, raw_app_meta_data)
VALUES ('e5800007-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
        'anna058@edubotics.local', '', now(), now(), '{"provider": "email", "providers": ["email"]}');
UPDATE auth.users SET raw_app_meta_data = raw_app_meta_data || '{"edubotics_provisioned": true}'
 WHERE id = 'e5800007-0000-0000-0000-000000000001';
COMMIT;
SELECT CASE WHEN EXISTS (SELECT 1 FROM auth.users WHERE id = 'e5800007-0000-0000-0000-000000000001')
             AND EXISTS (SELECT 1 FROM public.users WHERE id = 'e5800007-0000-0000-0000-000000000001'
                                                      AND role = 'student')
            THEN 'PASS T7' ELSE 'FAIL T7' END;

\echo '=== T8 the same create WITHOUT the UPDATE (a sign-up) is refused at the deferred point ==='
DO $$
BEGIN
  BEGIN
    INSERT INTO auth.users (id, instance_id, aud, role, email, encrypted_password, created_at, updated_at, raw_app_meta_data)
    VALUES ('e5800008-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000000', 'authenticated',
            'authenticated', 'x058@example.com', '', now(), now(), '{"provider": "email", "providers": ["email"]}');
    -- the INSERT itself passes: the check waits for the end of the transaction
    UPDATE auth.users SET updated_at = now() WHERE id = 'e5800008-0000-0000-0000-000000000001';
    SET CONSTRAINTS public.trg_users_require_provisioned IMMEDIATE;
    RAISE NOTICE 'FAIL T8 (created)';
  EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T8';
  END;
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T8 (%: %)', SQLSTATE, SQLERRM;
END $$;

\echo '=== T9 an account deleted again in the same transaction leaves nothing to judge ==='
DO $$
BEGIN
  INSERT INTO auth.users (id, instance_id, aud, role, email, encrypted_password, created_at, updated_at, raw_app_meta_data)
  VALUES ('e5800009-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000000', 'authenticated',
          'authenticated', 'y058@example.com', '', now(), now(), '{"provider": "email"}');
  DELETE FROM auth.users WHERE id = 'e5800009-0000-0000-0000-000000000001';
  SET CONSTRAINTS public.trg_users_require_provisioned IMMEDIATE;
  RAISE NOTICE 'PASS T9';
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T9 (%: %)', SQLSTATE, SQLERRM;
END $$;

\echo '=== T10 an account from before 058 (no marker) keeps working: updates are never judged ==='
-- (the check is switched off for this one INSERT, as postgres, to stand in for an
-- account created before 058)
DO $$
DECLARE v_on BOOLEAN := EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'public.users'::regclass
                                   AND tgname = 'trg_users_require_provisioned');
BEGIN
  IF v_on THEN EXECUTE 'ALTER TABLE public.users DISABLE TRIGGER trg_users_require_provisioned'; END IF;
  INSERT INTO auth.users (id, instance_id, aud, role, email, encrypted_password, created_at, updated_at)
  VALUES ('e5800010-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000000', 'authenticated',
          'authenticated', 'alt058@edubotics.local', '', now(), now());
  IF v_on THEN EXECUTE 'ALTER TABLE public.users ENABLE TRIGGER trg_users_require_provisioned'; END IF;
END $$;
DO $$
BEGIN
  SET LOCAL ROLE service_role;
  UPDATE public.users SET username = 'alt058', full_name = 'Alt' WHERE id = 'e5800010-0000-0000-0000-000000000001';
  RESET ROLE;
  UPDATE auth.users SET encrypted_password = 'x', updated_at = now() WHERE id = 'e5800010-0000-0000-0000-000000000001';
  SET CONSTRAINTS public.trg_users_require_provisioned IMMEDIATE;
  IF EXISTS (SELECT 1 FROM public.users WHERE id = 'e5800010-0000-0000-0000-000000000001' AND username = 'alt058') THEN
    RAISE NOTICE 'PASS T10';
  ELSE RAISE NOTICE 'FAIL T10 (not updated)'; END IF;
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T10 (%: %)', SQLSTATE, SQLERRM;
END $$;

-- clean-up
DELETE FROM auth.users WHERE id IN ('e5800007-0000-0000-0000-000000000001', 'e5800010-0000-0000-0000-000000000001');

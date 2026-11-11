-- Behavioural assertions for migration 059 (the platform's Hugging Face
-- namespaces — the organisation and the platform token's own account — are
-- refused as users.hf_username and as the namespace of a datasets row, for
-- every caller, case-insensitively), run BY HAND against a LOCAL database —
-- never CI, never the linked project (it seeds rows and removes them; T6 and T11
-- run inside transactions they roll back).
--
-- The baseline replays on a fresh database as shipped, so a local stack of the
-- checkout itself (`supabase start`, or every migration applied in order by
-- psql) is the database: with every migration through 059 applied, then
--   psql <local db url> -f supabase/tests/059_hf_reserved_namespaces_assertions.sql
-- On a plain PostgreSQL, apply .github/scripts/supabase_replay_stub.sql first.
-- The cloud API's writes are replayed as the service role (SET LOCAL ROLE
-- service_role, a plain statement), the token save and register_dataset_safe as
-- their owner (they are SECURITY DEFINER and service-role only; never called
-- under SET ROLE). The SA-08 chain is replayed in T3 and T7: a forged
-- hf_username equal to the organisation, then the organisation's dataset
-- registered to the student — both refused.
-- 2026-11-13: run on PostgreSQL 16.15 with that stub, every migration 000 → 058
-- applied by psql, then 059: 13 PASS, 0 FAIL, 0 ERROR. Against the database at
-- 058 (or after the 059 rollback): 0 PASS, FAIL T1, T3a, T3b, T4, T5, T7a, T7b,
-- T8, T9, T10 (the reserved names go through; T4's credential then pins the
-- name, so T5 and T8 fail too), T2, T6 and T11 print nothing and 7 ERROR lines
-- (the table is missing). The cycle 059 → rollback → rollback →
-- 059 → 059 passes each self-check and prints 13 PASS after every apply, and
-- after the rollback pg_dump -s equals the dump at 058.
--
-- Expected: 13 PASS, 0 FAIL (T1..T11; T3a/T3b, T7a/T7b).
\set ON_ERROR_STOP off
\pset pager off

INSERT INTO auth.users (id, instance_id, aud, role, email, encrypted_password, created_at, updated_at, raw_app_meta_data)
VALUES ('b5900001-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 's1-059@x.test', '', now(), now(), '{"edubotics_provisioned": true}'),
       ('b5900002-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 's2-059@x.test', '', now(), now(), '{"edubotics_provisioned": true}')
ON CONFLICT (id) DO NOTHING;

-- `as_service(sql)`: one statement as the cloud API's service role; its SQLSTATE
-- ('00000' = it changed a row).
CREATE OR REPLACE FUNCTION pg_temp.as_service(p_sql TEXT) RETURNS TEXT LANGUAGE plpgsql AS $$
DECLARE n INT;
BEGIN
  SET LOCAL ROLE service_role;
  EXECUTE p_sql;
  GET DIAGNOSTICS n = ROW_COUNT;
  RESET ROLE;
  RETURN CASE WHEN n >= 1 THEN '00000' ELSE 'no-row' END;
EXCEPTION WHEN OTHERS THEN
  RETURN SQLSTATE;
END $$;

-- `as_owner(sql)`: the same as the migration owner (a SECURITY DEFINER RPC's view).
CREATE OR REPLACE FUNCTION pg_temp.as_owner(p_sql TEXT) RETURNS TEXT LANGUAGE plpgsql AS $$
BEGIN
  EXECUTE p_sql;
  RETURN '00000';
EXCEPTION WHEN OTHERS THEN
  RETURN SQLSTATE;
END $$;

\echo '=== T1 the table: RLS on, nothing for PUBLIC or a request role, service_role SELECT/INSERT/UPDATE only ==='
SELECT CASE WHEN to_regclass('public.hf_reserved_namespaces') IS NOT NULL
             AND (SELECT relrowsecurity FROM pg_class WHERE oid = to_regclass('public.hf_reserved_namespaces'))
             AND NOT EXISTS (SELECT 1 FROM pg_class c, aclexplode(c.relacl) a
                              WHERE c.oid = to_regclass('public.hf_reserved_namespaces')
                                AND a.grantee IN (0, to_regrole('anon'), to_regrole('authenticated')))
             AND has_table_privilege('service_role', 'public.hf_reserved_namespaces', 'INSERT')
             AND NOT has_table_privilege('service_role', 'public.hf_reserved_namespaces', 'DELETE')
            THEN 'PASS T1' ELSE 'FAIL T1' END;

\echo '=== T2 the organisation is reserved (= hf_identity.HF_ORG, lowercased) ==='
SELECT CASE WHEN count(*) = 1 THEN 'PASS T2' ELSE 'FAIL T2' END
  FROM public.hf_reserved_namespaces WHERE name_lower = 'edubotics-solutions' AND kind = 'organisation';

\echo '=== T3 SA-08: the service role cannot set users.hf_username to the organisation, in any case or spacing ==='
SELECT CASE WHEN pg_temp.as_service($q$UPDATE public.users SET hf_username = 'EduBotics-Solutions' WHERE id = 'b5900001-0000-0000-0000-000000000001'$q$) = 'P0059'
             AND pg_temp.as_service($q$UPDATE public.users SET hf_username = 'edubotics-solutions' WHERE id = 'b5900001-0000-0000-0000-000000000001'$q$) = 'P0059'
            THEN 'PASS T3a' ELSE 'FAIL T3a' END;
SELECT CASE WHEN pg_temp.as_service($q$UPDATE public.users SET hf_username = ' EDUBOTICS-SOLUTIONS ' WHERE id = 'b5900001-0000-0000-0000-000000000001'$q$) = 'P0059'
            THEN 'PASS T3b' ELSE 'FAIL T3b' END;

\echo '=== T4 the token save (store_user_hf_credential, as its owner) cannot prove a reserved name either ==='
SELECT CASE WHEN pg_temp.as_owner($q$SELECT public.store_user_hf_credential('b5900001-0000-0000-0000-000000000001', 'v1.0123abcd.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA', '0123456789abcdef', 'hf_…abcd', 'EduBotics-Solutions', 'write', NULL)$q$) = 'P0059'
             AND NOT EXISTS (SELECT 1 FROM public.user_hf_credentials WHERE user_id = 'b5900001-0000-0000-0000-000000000001')
            THEN 'PASS T4' ELSE 'FAIL T4' END;

\echo '=== T5 an ordinary account name still goes through ==='
SELECT CASE WHEN pg_temp.as_service($q$UPDATE public.users SET hf_username = 'anna-059' WHERE id = 'b5900001-0000-0000-0000-000000000001'$q$) = '00000'
            THEN 'PASS T5' ELSE 'FAIL T5' END;

\echo '=== T6 a platform account the API records is refused from then on, case-insensitively ==='
BEGIN;
INSERT INTO public.hf_reserved_namespaces (name_lower, kind) VALUES ('plattform-bot-059', 'platform_account');
SELECT CASE WHEN pg_temp.as_service($q$UPDATE public.users SET hf_username = 'Plattform-Bot-059' WHERE id = 'b5900002-0000-0000-0000-000000000002'$q$) = 'P0059'
             AND pg_temp.as_service($q$INSERT INTO public.datasets (owner_user_id, hf_repo_id, name) VALUES ('b5900002-0000-0000-0000-000000000002', 'PLATTFORM-BOT-059/x', 'x')$q$) = 'P0034'
            THEN 'PASS T6' ELSE 'FAIL T6' END;
ROLLBACK;

\echo '=== T7 SA-08: no datasets row may name the organisation (service-role insert, register_dataset_safe) ==='
SELECT CASE WHEN pg_temp.as_service($q$INSERT INTO public.datasets (owner_user_id, hf_repo_id, name) VALUES ('b5900001-0000-0000-0000-000000000001', 'EduBotics-Solutions/private-org-data', 'x')$q$) = 'P0034'
             AND pg_temp.as_service($q$INSERT INTO public.datasets (owner_user_id, hf_repo_id, name) VALUES ('b5900001-0000-0000-0000-000000000001', 'edubotics-solutions/y', 'y')$q$) = 'P0034'
            THEN 'PASS T7a' ELSE 'FAIL T7a' END;
SELECT CASE WHEN pg_temp.as_owner($q$SELECT public.register_dataset_safe('b5900001-0000-0000-0000-000000000001', 'EduBotics-Solutions/z', 'EduBotics-Solutions', NULL, 'z', '', NULL, NULL, NULL, NULL)$q$) = 'P0034'
             AND NOT EXISTS (SELECT 1 FROM public.datasets WHERE owner_user_id = 'b5900001-0000-0000-0000-000000000001')
            THEN 'PASS T7b' ELSE 'FAIL T7b' END;

\echo '=== T8 an ordinary dataset still registers ==='
SELECT CASE WHEN pg_temp.as_owner($q$SELECT public.register_dataset_safe('b5900001-0000-0000-0000-000000000001', 'anna-059/wuerfel', 'anna-059', NULL, 'wuerfel', '', NULL, NULL, NULL, NULL)$q$) = '00000'
            THEN 'PASS T8' ELSE 'FAIL T8' END;

\echo '=== T9 the two trigger functions: SECURITY DEFINER, empty search_path, nobody executes them ==='
SELECT CASE WHEN count(*) = 2
             AND bool_and(p.prosecdef AND p.proconfig = ARRAY['search_path=""'])
             AND bool_and(NOT has_function_privilege('anon', p.oid, 'EXECUTE')
                          AND NOT has_function_privilege('authenticated', p.oid, 'EXECUTE')
                          AND NOT has_function_privilege('service_role', p.oid, 'EXECUTE'))
            THEN 'PASS T9' ELSE 'FAIL T9' END
  FROM pg_proc p
 WHERE p.oid IN (to_regprocedure('public.users_hf_username_not_reserved()'),
                 to_regprocedure('public.datasets_namespace_not_reserved()'));

\echo '=== T10 044''s guard still fires first on users ==='
SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'public.users'::regclass
                            AND tgname = 'trg_1_users_hf_username_not_reserved')
             AND NOT EXISTS (SELECT 1 FROM pg_trigger t WHERE t.tgrelid = 'public.users'::regclass AND NOT t.tgisinternal
                               AND (t.tgtype & 3) = 3 AND t.tgname < 'trg_0_users_protected_columns')
            THEN 'PASS T10' ELSE 'FAIL T10' END;

\echo '=== T11 an existing registry row in the organisation stays editable (only a NEW namespace is judged) ==='
BEGIN;
ALTER TABLE public.datasets DISABLE TRIGGER trg_datasets_namespace_not_reserved;
INSERT INTO public.datasets (owner_user_id, hf_repo_id, name)
VALUES ('b5900002-0000-0000-0000-000000000002', 'EduBotics-Solutions/legacy-059', 'alt');
ALTER TABLE public.datasets ENABLE TRIGGER trg_datasets_namespace_not_reserved;
SELECT CASE WHEN pg_temp.as_service($q$UPDATE public.datasets SET name = 'umbenannt' WHERE hf_repo_id = 'EduBotics-Solutions/legacy-059'$q$) = '00000'
            THEN 'PASS T11' ELSE 'FAIL T11' END;
ROLLBACK;

-- clean-up
DELETE FROM public.datasets WHERE owner_user_id IN ('b5900001-0000-0000-0000-000000000001', 'b5900002-0000-0000-0000-000000000002');
DELETE FROM auth.users WHERE id IN ('b5900001-0000-0000-0000-000000000001', 'b5900002-0000-0000-0000-000000000002');

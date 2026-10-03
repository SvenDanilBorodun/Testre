-- Behavioural assertions for migration 042 (a student's own Hugging Face token,
-- stored encrypted with the account), run BY HAND against a fresh LOCAL stack —
-- never CI, never the linked project (it seeds rows).
--
-- The squashed baseline does NOT replay on a fresh database as shipped
-- (docs/KNOWN-ISSUES.md, „baseline.sql is NOT replayable from scratch"): run
-- it in a SCRATCH copy of supabase/ whose baseline has its one
-- `RENAME COLUMN runpod_job_id` line guarded exactly as
-- 040_code_programs_assertions.sql's header shows, with every migration
-- through 042, then
--   supabase start   (in the scratch copy; it applies the migrations)
--   psql <local db url> < supabase/tests/042_user_hf_credentials_assertions.sql
-- Expected: 19 PASS, 0 FAIL/ERROR (T1..T14, some with a/b halves).
\set ON_ERROR_STOP off
\pset pager off

-- seed: two students (auth.users first: users.id FKs it; the baseline's auth
-- trigger mirrors the ids into public.users)
INSERT INTO auth.users (id, instance_id, aud, role, email, encrypted_password, created_at, updated_at)
VALUES ('b4200001-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 's1-042@x.test', '', now(), now()),
       ('b4200002-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 's2-042@x.test', '', now(), now())
ON CONFLICT (id) DO NOTHING;

\echo '=== T1 the table exists, RLS is on, and there is NO policy ==='
SELECT CASE WHEN (SELECT relrowsecurity FROM pg_class WHERE oid = 'public.user_hf_credentials'::regclass)
                 AND (SELECT count(*) FROM pg_policies WHERE schemaname = 'public' AND tablename = 'user_hf_credentials') = 0
            THEN 'PASS T1' ELSE 'FAIL T1' END;

\echo '=== T2 anon and authenticated hold NO privilege on the table ==='
SELECT CASE WHEN NOT (has_table_privilege('anon', 'public.user_hf_credentials', 'SELECT')
                   OR has_table_privilege('anon', 'public.user_hf_credentials', 'INSERT')
                   OR has_table_privilege('anon', 'public.user_hf_credentials', 'UPDATE')
                   OR has_table_privilege('anon', 'public.user_hf_credentials', 'DELETE'))
            THEN 'PASS T2a' ELSE 'FAIL T2a' END;
SELECT CASE WHEN NOT (has_table_privilege('authenticated', 'public.user_hf_credentials', 'SELECT')
                   OR has_table_privilege('authenticated', 'public.user_hf_credentials', 'INSERT')
                   OR has_table_privilege('authenticated', 'public.user_hf_credentials', 'UPDATE')
                   OR has_table_privilege('authenticated', 'public.user_hf_credentials', 'DELETE'))
            THEN 'PASS T2b' ELSE 'FAIL T2b' END;

\echo '=== T3 service_role can read and write it ==='
SELECT CASE WHEN has_table_privilege('service_role', 'public.user_hf_credentials', 'SELECT')
                 AND has_table_privilege('service_role', 'public.user_hf_credentials', 'INSERT')
                 AND has_table_privilege('service_role', 'public.user_hf_credentials', 'UPDATE')
                 AND has_table_privilege('service_role', 'public.user_hf_credentials', 'DELETE')
            THEN 'PASS T3' ELSE 'FAIL T3' END;

\echo '=== T4 exactly one store_user_hf_credential: 6 arguments, SECURITY DEFINER, pinned search_path, jsonb ==='
SELECT CASE WHEN count(*) = 1 AND bool_and(pronargs = 6) AND bool_and(prosecdef)
                 AND bool_and(prorettype = 'jsonb'::regtype)
                 AND bool_and(proconfig::text LIKE '%search_path=public, pg_catalog%')
                 AND bool_and(pg_get_function_identity_arguments(oid)
                     = 'p_user_id uuid, p_ciphertext text, p_fp text, p_hint text, p_hf_username text, p_role text')
            THEN 'PASS T4' ELSE 'FAIL T4 (' || count(*) || ' rows)' END
  FROM pg_proc WHERE proname = 'store_user_hf_credential' AND pronamespace = 'public'::regnamespace;

\echo '=== T5 grants: nobody but service_role can EXECUTE the writer (PUBLIC, anon, authenticated cannot) ==='
SELECT CASE WHEN NOT has_function_privilege('anon', 'public.store_user_hf_credential(uuid,text,text,text,text,text)', 'EXECUTE')
                 AND NOT has_function_privilege('authenticated', 'public.store_user_hf_credential(uuid,text,text,text,text,text)', 'EXECUTE')
                 AND NOT EXISTS (SELECT 1 FROM pg_proc p, LATERAL aclexplode(p.proacl) a
                                  WHERE p.oid = 'public.store_user_hf_credential(uuid,text,text,text,text,text)'::regprocedure
                                    AND a.grantee = 0)
            THEN 'PASS T5a' ELSE 'FAIL T5a' END;
SELECT CASE WHEN has_function_privilege('service_role', 'public.store_user_hf_credential(uuid,text,text,text,text,text)', 'EXECUTE')
            THEN 'PASS T5b' ELSE 'FAIL T5b' END;

\echo '=== T6 a first store writes the row AND sets users.hf_username to the proven name ==='
SELECT public.store_user_hf_credential(
         'b4200001-0000-0000-0000-000000000001',
         'v1.3b28fa93.oKGio6Slpqeoqaqrjn4jTCSqY94DBOayZhuhvxHNOHHz1iMN_W9H5x7KFGCzFyaezvqdnUay92eVBeK79YNbhgY',
         'c1770a7966b0771e', 'hf_…aaaa', 'alice', 'write') AS result;
SELECT CASE WHEN count(*) = 1 AND bool_and(hf_username = 'alice') AND bool_and(token_fp = 'c1770a7966b0771e')
                 AND bool_and(token_hint = 'hf_…aaaa') AND bool_and(token_role = 'write')
            THEN 'PASS T6a' ELSE 'FAIL T6a (' || count(*) || ' rows)' END
  FROM public.user_hf_credentials WHERE user_id = 'b4200001-0000-0000-0000-000000000001';
SELECT CASE WHEN hf_username = 'alice' THEN 'PASS T6b' ELSE 'FAIL T6b' END
  FROM public.users WHERE id = 'b4200001-0000-0000-0000-000000000001';

\echo '=== T7 a second store REPLACES (still one row, created_at kept) and follows the new account name ==='
CREATE TEMP TABLE _t7 AS SELECT created_at FROM public.user_hf_credentials WHERE user_id = 'b4200001-0000-0000-0000-000000000001';
SELECT pg_sleep(0.05);
SELECT public.store_user_hf_credential(
         'b4200001-0000-0000-0000-000000000001',
         'v1.3b28fa93.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
         '006da5aabd66bbc9', 'hf_…bbbb', 'alice-2', 'fineGrained') AS result;
SELECT CASE WHEN count(*) = 1 AND bool_and(token_fp = '006da5aabd66bbc9') AND bool_and(token_role = 'fineGrained')
                 AND bool_and(created_at = (SELECT created_at FROM _t7)) AND bool_and(updated_at > created_at)
            THEN 'PASS T7a' ELSE 'FAIL T7a (' || count(*) || ' rows)' END
  FROM public.user_hf_credentials WHERE user_id = 'b4200001-0000-0000-0000-000000000001';
SELECT CASE WHEN hf_username = 'alice-2' THEN 'PASS T7b' ELSE 'FAIL T7b' END
  FROM public.users WHERE id = 'b4200001-0000-0000-0000-000000000001';

\echo '=== T8 an unknown user is P0002 BEFORE any write (what the boot schema probe relies on) ==='
DO $$ BEGIN
  PERFORM public.store_user_hf_credential(
    '00000000-0000-0000-0000-000000000000', 'v1.3b28fa93.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
    '006da5aabd66bbc9', 'hf_…bbbb', 'nobody', 'write');
  RAISE NOTICE 'FAIL T8a (no error)';
EXCEPTION WHEN SQLSTATE 'P0002' THEN RAISE NOTICE 'PASS T8a (%)', SQLERRM; END $$;
SELECT CASE WHEN count(*) = 0 THEN 'PASS T8b' ELSE 'FAIL T8b' END
  FROM public.user_hf_credentials WHERE user_id = '00000000-0000-0000-0000-000000000000';

\echo '=== T9 a read-only token is refused by the database too (check_violation), the row stays ==='
DO $$ BEGIN
  PERFORM public.store_user_hf_credential(
    'b4200001-0000-0000-0000-000000000001', 'v1.3b28fa93.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
    '006da5aabd66bbc9', 'hf_…bbbb', 'alice-2', 'read');
  RAISE NOTICE 'FAIL T9 (no error)';
EXCEPTION WHEN check_violation THEN RAISE NOTICE 'PASS T9 (read role: check_violation)'; END $$;

\echo '=== T10 a PLAINTEXT token can never be stored: only a v1 envelope passes the CHECK ==='
DO $$ BEGIN
  PERFORM public.store_user_hf_credential(
    'b4200001-0000-0000-0000-000000000001', 'hf_' || repeat('a', 34),
    '006da5aabd66bbc9', 'hf_…aaaa', 'alice-2', 'write');
  RAISE NOTICE 'FAIL T10 (no error)';
EXCEPTION WHEN check_violation THEN RAISE NOTICE 'PASS T10 (plaintext: check_violation)'; END $$;

\echo '=== T11 a malformed fingerprint is refused ==='
DO $$ BEGIN
  PERFORM public.store_user_hf_credential(
    'b4200001-0000-0000-0000-000000000001', 'v1.3b28fa93.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
    'NOT-A-FINGERPRINT', 'hf_…bbbb', 'alice-2', 'write');
  RAISE NOTICE 'FAIL T11 (no error)';
EXCEPTION WHEN check_violation THEN RAISE NOTICE 'PASS T11 (fingerprint: check_violation)'; END $$;
-- ...and none of the three refusals changed the stored credential
SELECT CASE WHEN count(*) = 1 AND bool_and(token_fp = '006da5aabd66bbc9') AND bool_and(token_role = 'fineGrained')
            THEN 'ok: refusals left the row alone' ELSE 'FAIL T9-T11 left the row changed' END
  FROM public.user_hf_credentials WHERE user_id = 'b4200001-0000-0000-0000-000000000001';

\echo '=== T12 deleting the account deletes the credential (ON DELETE CASCADE) ==='
SELECT public.store_user_hf_credential(
         'b4200002-0000-0000-0000-000000000002',
         'v1.3b28fa93.oKGio6Slpqeoqaqrjn4jTCSqY94DBOayZhuhvxHNOHHz1iMN_W9H5x7KFGCzFyaezvqdnUay92eVBeK79YNbhgY',
         'c1770a7966b0771e', 'hf_…aaaa', 'bob', 'write') IS NOT NULL AS stored;
DELETE FROM auth.users WHERE id = 'b4200002-0000-0000-0000-000000000002';
SELECT CASE WHEN count(*) = 0 THEN 'PASS T12' ELSE 'FAIL T12' END
  FROM public.user_hf_credentials WHERE user_id = 'b4200002-0000-0000-0000-000000000002';

\echo '=== T13 the touch trigger moves updated_at on every UPDATE ==='
CREATE TEMP TABLE _t13 AS SELECT updated_at FROM public.user_hf_credentials WHERE user_id = 'b4200001-0000-0000-0000-000000000001';
SELECT pg_sleep(0.05);
UPDATE public.user_hf_credentials SET token_hint = 'hf_…cccc' WHERE user_id = 'b4200001-0000-0000-0000-000000000001';
SELECT CASE WHEN updated_at > (SELECT updated_at FROM _t13) THEN 'PASS T13' ELSE 'FAIL T13' END
  FROM public.user_hf_credentials WHERE user_id = 'b4200001-0000-0000-0000-000000000001';

\echo '=== T14 the table is NOT in the Realtime publication (a token row must never be broadcast) ==='
SELECT CASE WHEN count(*) = 0 THEN 'PASS T14' ELSE 'FAIL T14' END
  FROM pg_publication_tables WHERE schemaname = 'public' AND tablename = 'user_hf_credentials';

-- cleanup of the seeded rows (the credential cascades)
DELETE FROM auth.users WHERE id IN ('b4200001-0000-0000-0000-000000000001', 'b4200002-0000-0000-0000-000000000002');

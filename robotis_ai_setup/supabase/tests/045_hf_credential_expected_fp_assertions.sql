-- Behavioural assertions for migration 045 (store_user_hf_credential compares
-- an expected fingerprint under the row locks), run BY HAND against a fresh
-- LOCAL database — never CI, never the linked project (it seeds rows).
--
-- The squashed baseline does NOT replay on a fresh database as shipped
-- (docs/KNOWN-ISSUES.md, „baseline.sql is NOT replayable from scratch"): run
-- it in a SCRATCH copy of supabase/ whose baseline has its one
-- `RENAME COLUMN runpod_job_id` line guarded exactly as
-- 040_code_programs_assertions.sql's header shows, with every migration
-- through 045, then
--   supabase start   (in the scratch copy; it applies the migrations)
--   psql <local db url> < supabase/tests/045_hf_credential_expected_fp_assertions.sql
-- (2026-10-04 it was run against public.ecr.aws/supabase/postgres:17.6.1.106
-- with the migrations applied in order by psql as `postgres`.) After 045,
-- 042_user_hf_credentials_assertions.sql is no longer the right check for the
-- writer: its T4 (six arguments) and T5a/T5b (the six-argument regprocedure)
-- fail by design. The two-session lock interplay (a DELETE racing a verify) is
-- not part of this file: it needs two connections (the migration header
-- records the ad-hoc run).
-- Expected: 12 PASS, 0 FAIL/ERROR (T1..T9, some with a/b halves).
\set ON_ERROR_STOP off
\pset pager off

INSERT INTO auth.users (id, instance_id, aud, role, email, encrypted_password, created_at, updated_at)
VALUES ('b4500001-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 's1-045@x.test', '', now(), now())
ON CONFLICT (id) DO NOTHING;

\echo '=== T1 exactly one writer: seven arguments, the last one defaulted, SECURITY DEFINER, pinned search_path, jsonb ==='
SELECT CASE WHEN count(*) = 1 AND bool_and(pronargs = 7) AND bool_and(pronargdefaults = 1) AND bool_and(prosecdef)
                 AND bool_and(prorettype = 'jsonb'::regtype)
                 AND bool_and(proconfig::text LIKE '%search_path=public, pg_catalog%')
                 AND bool_and(pg_get_function_identity_arguments(oid)
                     = 'p_user_id uuid, p_ciphertext text, p_fp text, p_hint text, p_hf_username text, p_role text, p_expected_fp text')
            THEN 'PASS T1' ELSE 'FAIL T1 (' || count(*) || ' rows)' END
  FROM pg_proc WHERE proname = 'store_user_hf_credential' AND pronamespace = 'public'::regnamespace;

\echo '=== T2 grants: service_role only ==='
SELECT CASE WHEN NOT has_function_privilege('anon', 'public.store_user_hf_credential(uuid,text,text,text,text,text,text)', 'EXECUTE')
                 AND NOT has_function_privilege('authenticated', 'public.store_user_hf_credential(uuid,text,text,text,text,text,text)', 'EXECUTE')
                 AND NOT EXISTS (SELECT 1 FROM pg_proc p, LATERAL aclexplode(p.proacl) a
                                  WHERE p.oid = 'public.store_user_hf_credential(uuid,text,text,text,text,text,text)'::regprocedure
                                    AND a.grantee = 0)
            THEN 'PASS T2a' ELSE 'FAIL T2a' END;
SELECT CASE WHEN has_function_privilege('service_role', 'public.store_user_hf_credential(uuid,text,text,text,text,text,text)', 'EXECUTE')
            THEN 'PASS T2b' ELSE 'FAIL T2b' END;

\echo '=== T3 an older API''s six NAMED arguments still resolve (the default) and store ==='
SELECT CASE WHEN (public.store_user_hf_credential(
                    p_user_id => 'b4500001-0000-0000-0000-000000000001',
                    p_ciphertext => 'v1.3b28fa93.oKGio6Slpqeoqaqrjn4jTCSqY94DBOayZhuhvxHNOHHz1iMN_W9H5x7KFGCzFyaezvqdnUay92eVBeK79YNbhgY',
                    p_fp => 'c1770a7966b0771e', p_hint => 'hf_…aaaa', p_hf_username => 'anna', p_role => 'write')
                 ->> 'fp') = 'c1770a7966b0771e'
            THEN 'PASS T3' ELSE 'FAIL T3' END;

\echo '=== T4 verify with the matching expectation re-stores (re-encrypted, validated_at moves) ==='
CREATE TEMP TABLE _t4 AS SELECT validated_at FROM public.user_hf_credentials WHERE user_id = 'b4500001-0000-0000-0000-000000000001';
SELECT pg_sleep(0.05);
SELECT public.store_user_hf_credential(
         'b4500001-0000-0000-0000-000000000001',
         'v1.cfc9913b.BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB',
         'c1770a7966b0771e', 'hf_…aaaa', 'anna', 'write', 'c1770a7966b0771e') IS NOT NULL AS restored;
SELECT CASE WHEN token_ciphertext LIKE 'v1.cfc9913b.%' AND validated_at > (SELECT validated_at FROM _t4)
            THEN 'PASS T4' ELSE 'FAIL T4' END
  FROM public.user_hf_credentials WHERE user_id = 'b4500001-0000-0000-0000-000000000001';

\echo '=== T5 verify of a token that was REPLACED meanwhile is P0045 and changes nothing ==='
DO $$ BEGIN
  PERFORM public.store_user_hf_credential(
    'b4500001-0000-0000-0000-000000000001',
    'v1.3b28fa93.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
    '006da5aabd66bbc9', 'hf_…bbbb', 'anna', 'write', '006da5aabd66bbc9');
  RAISE NOTICE 'FAIL T5a (no error)';
EXCEPTION WHEN SQLSTATE 'P0045' THEN RAISE NOTICE 'PASS T5a (%)', SQLERRM; END $$;
SELECT CASE WHEN token_fp = 'c1770a7966b0771e' AND token_ciphertext LIKE 'v1.cfc9913b.%' THEN 'PASS T5b' ELSE 'FAIL T5b' END
  FROM public.user_hf_credentials WHERE user_id = 'b4500001-0000-0000-0000-000000000001';

\echo '=== T6 verify of a token that was REMOVED meanwhile is P0045 and resurrects nothing ==='
DELETE FROM public.user_hf_credentials WHERE user_id = 'b4500001-0000-0000-0000-000000000001';
DO $$ BEGIN
  PERFORM public.store_user_hf_credential(
    'b4500001-0000-0000-0000-000000000001',
    'v1.cfc9913b.BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB',
    'c1770a7966b0771e', 'hf_…aaaa', 'anna', 'write', 'c1770a7966b0771e');
  RAISE NOTICE 'FAIL T6a (no error)';
EXCEPTION WHEN SQLSTATE 'P0045' THEN RAISE NOTICE 'PASS T6a (%)', SQLERRM; END $$;
SELECT CASE WHEN count(*) = 0 THEN 'PASS T6b' ELSE 'FAIL T6b' END
  FROM public.user_hf_credentials WHERE user_id = 'b4500001-0000-0000-0000-000000000001';

\echo '=== T7 an unknown user is P0002 BEFORE the expectation is even read ==='
DO $$ BEGIN
  PERFORM public.store_user_hf_credential(
    '00000000-0000-0000-0000-000000000000', 'v1.3b28fa93.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
    '006da5aabd66bbc9', 'hf_…bbbb', 'nobody', 'write', '006da5aabd66bbc9');
  RAISE NOTICE 'FAIL T7 (no error)';
EXCEPTION WHEN SQLSTATE 'P0002' THEN RAISE NOTICE 'PASS T7 (P0002)'; END $$;

\echo '=== T8 PUT (no expectation) stores and replaces exactly as 042 did ==='
SELECT public.store_user_hf_credential(
         'b4500001-0000-0000-0000-000000000001',
         'v1.3b28fa93.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
         '006da5aabd66bbc9', 'hf_…bbbb', 'anna-2', 'fineGrained', NULL) IS NOT NULL AS put1;
SELECT CASE WHEN c.token_fp = '006da5aabd66bbc9' AND u.hf_username = 'anna-2' THEN 'PASS T8' ELSE 'FAIL T8' END
  FROM public.user_hf_credentials c JOIN public.users u ON u.id = c.user_id
 WHERE c.user_id = 'b4500001-0000-0000-0000-000000000001';

\echo '=== T9 the boot schema probe''s exact call (dummy id, seven names) answers P0002, i.e. the RPC exists ==='
DO $$ BEGIN
  PERFORM public.store_user_hf_credential(
    p_user_id => '00000000-0000-0000-0000-000000000000', p_ciphertext => '_probe', p_fp => '_probe',
    p_hint => '_probe', p_hf_username => '_probe', p_role => '_probe', p_expected_fp => NULL);
  RAISE NOTICE 'FAIL T9 (no error)';
EXCEPTION WHEN SQLSTATE 'P0002' THEN RAISE NOTICE 'PASS T9 (P0002)'; END $$;

-- cleanup (the credential cascades)
DELETE FROM auth.users WHERE id = 'b4500001-0000-0000-0000-000000000001';

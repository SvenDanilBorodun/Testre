-- Behavioural assertions for migration 060 (the server's query statistics —
-- extensions.pg_stat_statements, extensions.pg_stat_statements_info and their
-- functions — are not readable by anon or authenticated, through PUBLIC or any
-- other grant; the monitoring role pg_read_all_stats, dashboard_user and the
-- owner keep them), run BY HAND against a LOCAL database or, read-only, the
-- linked project (every check is a read: a refused SELECT, a privilege lookup;
-- it writes nothing, and every SET ROLE is LOCAL to a subtransaction).
--
-- A local stack of the checkout (`supabase start`, which applies every
-- migration) is the database; on a plain PostgreSQL, apply
-- .github/scripts/supabase_replay_stub.sql and every migration in order by psql
-- first. Then, as the migrating role (postgres):
--   psql <db url> -f supabase/tests/060_query_stats_not_public_assertions.sql
-- The request roles are taken with SET LOCAL ROLE inside pg_temp.use_as (the
-- migrating role is a member of both on Supabase and in the stub). A read the
-- privilege check passes either returns rows or, on a server that does not
-- preload the library, fails later with 55000; both prove the privilege, so T8
-- accepts both.
-- 2026-11-16: run on a local stack (Supabase CLI 2.122.0, supabase/postgres
-- 17.6.1.106, pg_stat_statements 1.11) at 059 with the two views and three
-- functions handed to postgres, their ACLs then equal to the hosted project's
-- measured that day: 9 FAIL (T1a..T4b, T5) and 4 PASS (T6..T9); after 060:
-- 13 PASS; after the rollback the 059 result again, the ACLs equal to 059's but
-- for item order. 060 → 060 and rollback → rollback each pass; 13 PASS after the
-- last 060. As supabase_read_only_user (pg_monitor) the view still answers rows;
-- as service_role and authenticated: permission denied. In the stack's OWN
-- layout (the objects owned by supabase_admin, which made the PUBLIC grant) 060
-- warns once per object, does not abort, and changes nothing: the same 9 FAIL.
--
-- Expected: 13 PASS, 0 FAIL (T1a/T1b .. T4a/T4b, T5..T9).
\set ON_ERROR_STOP off
\pset pager off

-- `use_as(role, sql)`: one statement as ROLE ('' = the current role); its SQLSTATE
-- ('00000' = it ran).
CREATE OR REPLACE FUNCTION pg_temp.use_as(p_role TEXT, p_sql TEXT) RETURNS TEXT LANGUAGE plpgsql AS $$
BEGIN
  BEGIN
    IF p_role <> '' THEN
      EXECUTE format('SET LOCAL ROLE %I', p_role);
    END IF;
    EXECUTE p_sql;
    RETURN '00000';
  EXCEPTION WHEN OTHERS THEN
    RETURN SQLSTATE;
  END;
END $$;

\echo '=== T1 the request roles cannot read extensions.pg_stat_statements ==='
SELECT CASE WHEN pg_temp.use_as('anon', 'SELECT count(*) FROM extensions.pg_stat_statements') = '42501'
            THEN 'PASS T1a' ELSE 'FAIL T1a' END;
SELECT CASE WHEN pg_temp.use_as('authenticated', 'SELECT count(*) FROM extensions.pg_stat_statements') = '42501'
            THEN 'PASS T1b' ELSE 'FAIL T1b' END;

\echo '=== T2 nor extensions.pg_stat_statements_info ==='
SELECT CASE WHEN pg_temp.use_as('anon', 'SELECT * FROM extensions.pg_stat_statements_info') = '42501'
            THEN 'PASS T2a' ELSE 'FAIL T2a' END;
SELECT CASE WHEN pg_temp.use_as('authenticated', 'SELECT * FROM extensions.pg_stat_statements_info') = '42501'
            THEN 'PASS T2b' ELSE 'FAIL T2b' END;

\echo '=== T3 nor call extensions.pg_stat_statements(boolean) directly (the view''s source) ==='
SELECT CASE WHEN pg_temp.use_as('anon', 'SELECT count(*) FROM extensions.pg_stat_statements(true)') = '42501'
            THEN 'PASS T3a' ELSE 'FAIL T3a' END;
SELECT CASE WHEN pg_temp.use_as('authenticated', 'SELECT count(*) FROM extensions.pg_stat_statements(true)') = '42501'
            THEN 'PASS T3b' ELSE 'FAIL T3b' END;

\echo '=== T4 nor extensions.pg_stat_statements_info() ==='
SELECT CASE WHEN pg_temp.use_as('anon', 'SELECT * FROM extensions.pg_stat_statements_info()') = '42501'
            THEN 'PASS T4a' ELSE 'FAIL T4a' END;
SELECT CASE WHEN pg_temp.use_as('authenticated', 'SELECT * FROM extensions.pg_stat_statements_info()') = '42501'
            THEN 'PASS T4b' ELSE 'FAIL T4b' END;

\echo '=== T5 PUBLIC holds no grant on either view or any extension function ==='
SELECT CASE WHEN NOT EXISTS (
         SELECT 1 FROM pg_class c, aclexplode(coalesce(c.relacl, acldefault('r', c.relowner))) a
          WHERE c.relnamespace = 'extensions'::regnamespace
            AND c.relname IN ('pg_stat_statements', 'pg_stat_statements_info') AND a.grantee = 0
         UNION ALL
         SELECT 1 FROM pg_proc p, aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
          WHERE p.pronamespace = 'extensions'::regnamespace
            AND p.proname IN ('pg_stat_statements', 'pg_stat_statements_info', 'pg_stat_statements_reset')
            AND a.grantee = 0)
        AND (SELECT count(*) FROM pg_class WHERE relnamespace = 'extensions'::regnamespace
                AND relname IN ('pg_stat_statements', 'pg_stat_statements_info')) = 2
       THEN 'PASS T5' ELSE 'FAIL T5' END;

\echo '=== T6 the monitoring role (pg_read_all_stats, and pg_monitor through it) keeps both readers ==='
SELECT CASE WHEN bool_and(has_table_privilege(r, 'extensions.pg_stat_statements', 'SELECT')
                      AND has_table_privilege(r, 'extensions.pg_stat_statements_info', 'SELECT')
                      AND has_function_privilege(r, 'extensions.pg_stat_statements(boolean)', 'EXECUTE')
                      AND has_function_privilege(r, 'extensions.pg_stat_statements_info()', 'EXECUTE'))
            THEN 'PASS T6' ELSE 'FAIL T6' END
  FROM unnest(ARRAY['pg_read_all_stats', 'pg_monitor']) r;

\echo '=== T7 dashboard_user keeps its own grants (where the role exists) ==='
SELECT CASE WHEN to_regrole('dashboard_user') IS NULL
              OR (has_table_privilege('dashboard_user', 'extensions.pg_stat_statements', 'SELECT')
                  AND has_table_privilege('dashboard_user', 'extensions.pg_stat_statements_info', 'SELECT'))
            THEN 'PASS T7' ELSE 'FAIL T7' END;

\echo '=== T8 the owner still reads (the dashboard''s query performance) ==='
SELECT CASE WHEN pg_temp.use_as('', 'SELECT count(*) FROM extensions.pg_stat_statements') IN ('00000', '55000')
            THEN 'PASS T8' ELSE 'FAIL T8' END;

\echo '=== T9 nobody new may reset the statistics; the request roles cannot ==='
SELECT CASE WHEN bool_and(NOT has_function_privilege('anon', p.oid, 'EXECUTE')
                      AND NOT has_function_privilege('authenticated', p.oid, 'EXECUTE')
                      AND NOT has_function_privilege('pg_read_all_stats', p.oid, 'EXECUTE'))
             AND count(*) >= 1
            THEN 'PASS T9' ELSE 'FAIL T9' END
  FROM pg_proc p
 WHERE p.pronamespace = 'extensions'::regnamespace AND p.proname = 'pg_stat_statements_reset';

-- 060 — the server's query statistics are not readable by the request roles
-- (security audit SA-22, closed 2026-11-16 at the owner's request).
--
-- WHY. extensions.pg_stat_statements and extensions.pg_stat_statements_info read
-- the server's statement statistics through two functions of the same names.
-- The extension's own install script grants the two views SELECT TO PUBLIC, and
-- the functions keep PostgreSQL's default EXECUTE for PUBLIC, so anon and
-- authenticated could read them: every role's call counts and timings, and the
-- full text of every statement run AS their own role — every student's
-- PostgREST and Realtime queries, since all students share the role
-- authenticated. No shipped client reaches schema extensions (PostgREST serves
-- public and graphql_public only), so this closes a defence-in-depth gap, not an
-- open read path. 057 revoked only the request roles' direct grants and left
-- PUBLIC's, believing that grant was supabase_admin's. Measured on the hosted
-- project on 2026-11-16: both views and all three functions are owned by
-- postgres (the migrating role, not a superuser) and the PUBLIC grants were made
-- by postgres (`=r/postgres`, `=X/postgres`), so this role can revoke them.
--
-- WHAT CHANGES (each object only where it exists; a database without the
-- extension skips everything):
--   * SELECT on extensions.pg_stat_statements and extensions.pg_stat_statements_info,
--     and EXECUTE on every extensions.pg_stat_statements* function (the two
--     readers and pg_stat_statements_reset, whose signature differs by
--     extension version), are revoked FROM PUBLIC, anon and authenticated.
--   * The two views and the two reader functions are granted TO
--     pg_read_all_stats, PostgreSQL's predefined monitoring role: on the hosted
--     project its members (through pg_monitor: postgres, supabase_read_only_user,
--     supabase_etl_admin) read them through PUBLIC until now and keep reading
--     them. dashboard_user keeps its own grants; the owner and the superusers
--     are unaffected. service_role loses the read: nothing in this repository
--     reads query statistics. pg_stat_statements_reset is granted to nobody new.
--   * A self-check. Where this role can act for an object's owner, it ABORTS
--     (and so rolls back) when anon or authenticated can still read a view or
--     execute a function, when PUBLIC still holds a grant on one, or when
--     pg_read_all_stats lost a reader; where it cannot (another project's
--     layout: the owner's grant is not this role's to revoke), it is a WARNING
--     naming the dashboard step, never an abort.
--
-- A Supabase extension upgrade or a disable/enable in the dashboard re-runs the
-- extension's script and grants PUBLIC again; ci.yml::migrations-replay judges
-- the replayed chain, the live project is judged by the hand-run
-- supabase/tests/060_query_stats_not_public_assertions.sql
-- (docs/KNOWN-ISSUES.md).
--
-- No API-visible shape: the cloud API's boot schema probe is unchanged.
-- Rollback: supabase/rollback/20261116120000_060_query_stats_not_public_rollback.sql
-- (re-opens the read).

BEGIN;

-- 1. Revoke from PUBLIC and the request roles; grant the readers to the monitoring role.
DO $$
DECLARE
    v_rec RECORD;
BEGIN
    FOR v_rec IN
        SELECT 'view' AS kind, c.oid::regclass::text AS obj, true AS reader
          FROM pg_class c
         WHERE c.relnamespace = to_regnamespace('extensions')
           AND c.relname IN ('pg_stat_statements', 'pg_stat_statements_info')
        UNION ALL
        SELECT 'function', p.oid::regprocedure::text, p.proname <> 'pg_stat_statements_reset'
          FROM pg_proc p
         WHERE p.pronamespace = to_regnamespace('extensions')
           AND p.proname IN ('pg_stat_statements', 'pg_stat_statements_info', 'pg_stat_statements_reset')
         ORDER BY 1, 2
    LOOP
        BEGIN
            IF v_rec.kind = 'view' THEN
                EXECUTE format('REVOKE SELECT ON %s FROM PUBLIC, anon, authenticated', v_rec.obj);
                EXECUTE format('GRANT SELECT ON %s TO pg_read_all_stats', v_rec.obj);
            ELSE
                EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC, anon, authenticated', v_rec.obj);
                IF v_rec.reader THEN
                    EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO pg_read_all_stats', v_rec.obj);
                END IF;
            END IF;
        EXCEPTION WHEN insufficient_privilege THEN
            RAISE WARNING '060: % may not change the grants on % (the owner''s setting; the self-check below judges what is left)',
                current_user, v_rec.obj;
        END;
    END LOOP;
END
$$;

-- 2. The self-check.
DO $$
DECLARE
    v_rec     RECORD;
    v_role    TEXT;
    v_can_fix BOOLEAN;
    v_open    BOOLEAN;
    v_super   BOOLEAN := (SELECT rolsuper FROM pg_roles WHERE rolname = current_user);
BEGIN
    FOR v_rec IN
        SELECT 'view' AS kind, c.oid, c.oid::regclass::text AS obj, c.relowner AS owner, true AS reader,
               coalesce(c.relacl, acldefault('r', c.relowner)) AS acl
          FROM pg_class c
         WHERE c.relnamespace = to_regnamespace('extensions')
           AND c.relname IN ('pg_stat_statements', 'pg_stat_statements_info')
        UNION ALL
        SELECT 'function', p.oid, p.oid::regprocedure::text, p.proowner, p.proname <> 'pg_stat_statements_reset',
               coalesce(p.proacl, acldefault('f', p.proowner))
          FROM pg_proc p
         WHERE p.pronamespace = to_regnamespace('extensions')
           AND p.proname IN ('pg_stat_statements', 'pg_stat_statements_info', 'pg_stat_statements_reset')
    LOOP
        v_can_fix := v_super OR pg_has_role(current_user, v_rec.owner, 'USAGE');
        FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
            v_open := CASE WHEN v_rec.kind = 'view' THEN has_table_privilege(v_role, v_rec.oid, 'SELECT')
                           ELSE has_function_privilege(v_role, v_rec.oid, 'EXECUTE') END;
            IF v_open AND v_can_fix THEN
                RAISE EXCEPTION '060: % can still use %', v_role, v_rec.obj;
            ELSIF v_open THEN
                RAISE WARNING '060: % can still use %, owned by %, whose grants % cannot change: revoke it as the owner (dashboard SQL editor), docs/KNOWN-ISSUES.md',
                    v_role, v_rec.obj, pg_get_userbyid(v_rec.owner), current_user;
            END IF;
        END LOOP;
        IF v_can_fix AND EXISTS (SELECT 1 FROM aclexplode(v_rec.acl) a WHERE a.grantee = 0) THEN
            RAISE EXCEPTION '060: PUBLIC still holds a grant on %', v_rec.obj;
        END IF;
        IF v_can_fix AND v_rec.reader AND NOT (
               CASE WHEN v_rec.kind = 'view' THEN has_table_privilege('pg_read_all_stats', v_rec.oid, 'SELECT')
                    ELSE has_function_privilege('pg_read_all_stats', v_rec.oid, 'EXECUTE') END) THEN
            RAISE EXCEPTION '060: pg_read_all_stats cannot use %', v_rec.obj;
        END IF;
        IF v_can_fix AND NOT v_rec.reader AND has_function_privilege('pg_read_all_stats', v_rec.oid, 'EXECUTE') THEN
            RAISE EXCEPTION '060: pg_read_all_stats may reset the statistics through %', v_rec.obj;
        END IF;
    END LOOP;
    RAISE NOTICE '060 self-check passed';
END
$$;

COMMIT;

-- Rollback of 060: the request roles may read the server's query statistics
-- again. RE-OPENS SA-22 (every role's statement counts and timings, and the text
-- of every statement run as authenticated or anon). No API rollback is needed
-- either way: the cloud API never reads query statistics.
--
-- Restores the grants 060 removed, as the extension's own script and
-- PostgreSQL's defaults set them: SELECT on both views and EXECUTE on the two
-- reader functions to PUBLIC, and drops the pg_read_all_stats grants 060 added.
-- pg_stat_statements_reset had no PUBLIC grant before 060 and gets none back.
-- Every object only where it exists. Safe to run twice.

BEGIN;

DO $$
DECLARE
    v_rec RECORD;
BEGIN
    FOR v_rec IN
        SELECT 'view' AS kind, c.oid::regclass::text AS obj
          FROM pg_class c
         WHERE c.relnamespace = to_regnamespace('extensions')
           AND c.relname IN ('pg_stat_statements', 'pg_stat_statements_info')
        UNION ALL
        SELECT 'function', p.oid::regprocedure::text
          FROM pg_proc p
         WHERE p.pronamespace = to_regnamespace('extensions')
           AND p.proname IN ('pg_stat_statements', 'pg_stat_statements_info')
         ORDER BY 1, 2
    LOOP
        IF v_rec.kind = 'view' THEN
            EXECUTE format('REVOKE SELECT ON %s FROM pg_read_all_stats', v_rec.obj);
            EXECUTE format('GRANT SELECT ON %s TO PUBLIC', v_rec.obj);
        ELSE
            EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM pg_read_all_stats', v_rec.obj);
            EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO PUBLIC', v_rec.obj);
        END IF;
    END LOOP;
    RAISE NOTICE '060 rolled back: PUBLIC reads the query statistics again';
END
$$;

COMMIT;

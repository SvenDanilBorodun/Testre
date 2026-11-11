-- Rollback of 054 — the advisor clean-up.
--
-- Restores exactly what 20261110120000_054_advisor_cleanup.sql changed, as 053
-- left it:
--   * public.credit_ledger_immutable() and public.trainings_guard_credit_columns():
--     RESET search_path (052 created both without a SET clause, so proconfig is
--     NULL again; both stay SECURITY INVOKER with EXECUTE for their owner only);
--   * public.prune_workflow_trajectories() and public.prune_workflow_submissions():
--     EXECUTE back to PUBLIC (PostgreSQL's default for a new function) and to anon
--     and authenticated (Supabase's default privileges when 034 and 040 created
--     them); service_role's and the owner's grants were never touched.
-- The advisor findings 054 closed come back with it.
--
-- ORDER: roll 054 back BEFORE the 052, 040 or 034 rollback. A function a later
-- rollback already dropped is skipped here (the same file then still restores
-- the others), so the order is a recommendation, not a trap.
--
-- Idempotent: RESET and GRANT of what is already there change nothing. The
-- closing DO block checks the restored state.

BEGIN;

DO $$
DECLARE
  v_fn TEXT;
BEGIN
  FOREACH v_fn IN ARRAY ARRAY['credit_ledger_immutable', 'trainings_guard_credit_columns'] LOOP
    IF to_regprocedure('public.' || v_fn || '()') IS NOT NULL THEN
      EXECUTE format('ALTER FUNCTION public.%I() RESET search_path', v_fn);
    END IF;
  END LOOP;
  -- service_role is revoked and granted again in the same transaction only so that
  -- the grants are restored as a set (PUBLIC, anon, authenticated, service_role):
  -- pg_dump then prints exactly what it printed at 053. The item order inside
  -- proacl may still differ ({=X,postgres=X,…} against {postgres=X,=X,…}),
  -- which changes nothing (measured by the wave-A verification, 2026-11-12).
  FOREACH v_fn IN ARRAY ARRAY['prune_workflow_trajectories', 'prune_workflow_submissions'] LOOP
    IF to_regprocedure('public.' || v_fn || '()') IS NOT NULL THEN
      EXECUTE format('REVOKE EXECUTE ON FUNCTION public.%I() FROM service_role', v_fn);
      EXECUTE format('GRANT EXECUTE ON FUNCTION public.%I() TO PUBLIC, anon, authenticated, service_role', v_fn);
    END IF;
  END LOOP;
END
$$;

DO $$
DECLARE
  v_fn TEXT;
  v_role TEXT;
  v_sig REGPROCEDURE;
BEGIN
  FOREACH v_fn IN ARRAY ARRAY['credit_ledger_immutable', 'trainings_guard_credit_columns'] LOOP
    v_sig := to_regprocedure('public.' || v_fn || '()');
    CONTINUE WHEN v_sig IS NULL;
    IF EXISTS (SELECT 1 FROM pg_proc WHERE oid = v_sig AND (proconfig IS NOT NULL OR prosecdef)) THEN
      RAISE EXCEPTION '054 rollback: public.%() is not the plain invoker function 052 created', v_fn;
    END IF;
    FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
      IF has_function_privilege(v_role, v_sig, 'EXECUTE') THEN
        RAISE EXCEPTION '054 rollback: % holds EXECUTE on public.%()', v_role, v_fn;
      END IF;
    END LOOP;
  END LOOP;
  FOREACH v_fn IN ARRAY ARRAY['prune_workflow_trajectories', 'prune_workflow_submissions'] LOOP
    v_sig := to_regprocedure('public.' || v_fn || '()');
    CONTINUE WHEN v_sig IS NULL;
    IF NOT EXISTS (SELECT 1 FROM pg_proc p, aclexplode(p.proacl) a
                    WHERE p.oid = v_sig AND a.grantee = 0 AND a.privilege_type = 'EXECUTE') THEN
      RAISE EXCEPTION '054 rollback: PUBLIC has no EXECUTE on public.%()', v_fn;
    END IF;
    FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
      IF NOT has_function_privilege(v_role, v_sig, 'EXECUTE') THEN
        RAISE EXCEPTION '054 rollback: % has no EXECUTE on public.%()', v_role, v_fn;
      END IF;
    END LOOP;
  END LOOP;
  RAISE NOTICE '054 rollback self-check passed';
END
$$;

COMMIT;

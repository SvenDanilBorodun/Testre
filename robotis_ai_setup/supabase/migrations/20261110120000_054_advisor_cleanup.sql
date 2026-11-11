-- 054 — Supabase security-advisor clean-up: two fixed search_paths, two revokes.
--
-- WHY. The project's security advisor lists four public functions:
--   * function_search_path_mutable: public.credit_ledger_immutable() and
--     public.trainings_guard_credit_columns() (052) are the only plpgsql/sql
--     functions in public without a fixed search_path (measured on a fresh
--     replay of 000 → 053: every other one carries one);
--   * anon/authenticated SECURITY DEFINER function executable:
--     public.prune_workflow_trajectories() (034) and
--     public.prune_workflow_submissions() (040) kept PostgreSQL's default
--     EXECUTE for PUBLIC plus Supabase's default grants to anon and
--     authenticated. They RETURN TRIGGER, so PostgREST cannot call them as an
--     RPC; the revoke closes the finding and leaves nothing to reason about.
--
-- WHAT CHANGES (nothing else):
--   * ALTER FUNCTION ... SET search_path = public on the two 052 trigger
--     functions — the convention of 052's own trigger functions
--     (trainings_credit_settle and the closes). Their bodies name no table and
--     resolve nothing through the path; the setting only pins it. Both stay
--     SECURITY INVOKER: trainings_guard_credit_columns judges current_user, and a
--     definer would see its owner (044's lesson). A function-level SET does not
--     change current_user. Their ACL is untouched (052: EXECUTE for the owner only).
--   * REVOKE EXECUTE ... FROM PUBLIC, anon, authenticated on the two prune
--     functions. service_role keeps its grant, the owner its own; the bodies,
--     SECURITY DEFINER, search_path and ownership are untouched. A trigger
--     function's EXECUTE privilege is checked when a trigger is CREATED, never
--     when it fires (052's credit triggers fire for roles with no EXECUTE at
--     all), so both prune caps keep pruning for every writer.
--   ALTER FUNCTION ... SET and REVOKE keep every body, OID and trigger binding
--   as they are: no CREATE OR REPLACE.
--
-- API. No table, column or RPC the cloud API's boot schema probe names changes
-- (main.py::_validate_required_schema is untouched; no RPC is involved).
--
-- ROLLBACK: rollback/20261110120000_054_advisor_cleanup_rollback.sql (RESET
-- search_path on the two, EXECUTE back to PUBLIC, anon and authenticated on the
-- two). Roll 054 back BEFORE the 052, 040 or 034 rollback: those drop or keep
-- the functions this file changes.
--
-- Idempotent: running it again sets the same values and revokes nothing new.
-- The closing DO block aborts the migration if any of the four is not in the
-- state described above.

BEGIN;

ALTER FUNCTION public.credit_ledger_immutable() SET search_path = public;
ALTER FUNCTION public.trainings_guard_credit_columns() SET search_path = public;

REVOKE EXECUTE ON FUNCTION public.prune_workflow_trajectories() FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.prune_workflow_submissions() FROM PUBLIC, anon, authenticated;

DO $$
DECLARE
  v_fn TEXT;
  v_role TEXT;
  v_sig REGPROCEDURE;
BEGIN
  -- The two 052 trigger functions: pinned path, still invoker, still nobody's.
  FOREACH v_fn IN ARRAY ARRAY['credit_ledger_immutable', 'trainings_guard_credit_columns'] LOOP
    v_sig := to_regprocedure('public.' || v_fn || '()');
    IF v_sig IS NULL THEN
      RAISE EXCEPTION '054: public.%() is missing', v_fn;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_proc WHERE oid = v_sig AND proconfig = '{search_path=public}') THEN
      RAISE EXCEPTION '054: public.%() has no fixed search_path', v_fn;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_proc WHERE oid = v_sig AND prosecdef) THEN
      RAISE EXCEPTION '054: public.%() became SECURITY DEFINER', v_fn;
    END IF;
    FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
      IF has_function_privilege(v_role, v_sig, 'EXECUTE') THEN
        RAISE EXCEPTION '054: % holds EXECUTE on public.%()', v_role, v_fn;
      END IF;
    END LOOP;
  END LOOP;

  -- The two prune functions: nobody but service_role and the owner executes them;
  -- the rest of their definition is as 034/040 left it.
  FOREACH v_fn IN ARRAY ARRAY['prune_workflow_trajectories', 'prune_workflow_submissions'] LOOP
    v_sig := to_regprocedure('public.' || v_fn || '()');
    IF v_sig IS NULL THEN
      RAISE EXCEPTION '054: public.%() is missing', v_fn;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_proc p, aclexplode(p.proacl) a
                WHERE p.oid = v_sig AND a.grantee = 0 AND a.privilege_type = 'EXECUTE') THEN
      RAISE EXCEPTION '054: PUBLIC holds EXECUTE on public.%()', v_fn;
    END IF;
    FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
      IF has_function_privilege(v_role, v_sig, 'EXECUTE') THEN
        RAISE EXCEPTION '054: % holds EXECUTE on public.%()', v_role, v_fn;
      END IF;
    END LOOP;
    IF NOT has_function_privilege('service_role', v_sig, 'EXECUTE') THEN
      RAISE EXCEPTION '054: service_role lost EXECUTE on public.%()', v_fn;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_proc WHERE oid = v_sig AND prosecdef AND proconfig = '{search_path=public}') THEN
      RAISE EXCEPTION '054: public.%() is no longer SECURITY DEFINER with search_path=public', v_fn;
    END IF;
  END LOOP;

  -- Every trigger that calls one of the four is still there and enabled.
  IF (SELECT count(*) FROM pg_trigger t
       WHERE NOT t.tgisinternal AND t.tgenabled = 'O'
         AND (t.tgrelid, t.tgname, t.tgfoid) IN (
               ('public.credit_ledger'::regclass, 'credit_ledger_no_change',
                'public.credit_ledger_immutable()'::regprocedure),
               ('public.credit_ledger'::regclass, 'credit_ledger_no_truncate',
                'public.credit_ledger_immutable()'::regprocedure),
               ('public.trainings'::regclass, 'trg_0_trainings_credit_guard',
                'public.trainings_guard_credit_columns()'::regprocedure),
               ('public.workflow_trajectories'::regclass, 'trg_workflow_trajectories_prune',
                'public.prune_workflow_trajectories()'::regprocedure),
               ('public.workflow_submissions'::regclass, 'trg_workflow_submissions_prune',
                'public.prune_workflow_submissions()'::regprocedure))) <> 5 THEN
    RAISE EXCEPTION '054: a trigger on one of the four functions is missing or disabled';
  END IF;
  RAISE NOTICE '054 self-check passed';
END
$$;

COMMIT;

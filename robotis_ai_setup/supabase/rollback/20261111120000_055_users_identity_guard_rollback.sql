-- Rollback of 055 — public.users without request-role writes.
--
-- Restores what 20261111120000_055_users_identity_guard.sql changed, as 054 left it:
--   * anon and authenticated get back INSERT, UPDATE, DELETE, TRUNCATE,
--     REFERENCES and TRIGGER on public.users (the set Supabase's default
--     privileges give every public table; SELECT was never revoked). 055's
--     column-level revoke had nothing to revoke on a stock project, so no
--     column grant is restored;
--   * the own-row policy "Users update own profile" exactly as 024
--     (20260520130522_024_advisor_fixes.sql) re-created it, with its comment;
--   * public.users_guard_protected_columns() exactly as 044 created it: rule 1
--     names only role, training_credits, hf_username, classroom_id and
--     workgroup_id; rule 2 (P0044) is the same in both. Its comment is 044's
--     again. The trigger trg_0_users_protected_columns and the EXECUTE revokes
--     stay (CREATE OR REPLACE keeps the function's OID, so the trigger keeps
--     firing it).
--
-- WARNING: this RE-OPENS what 055 closed. A signed-in student can again PATCH
-- their own deletion_requested_at (clear or back-date a GDPR request behind the
-- API's back), username, full_name, email, created_by, vision_quota_per_term,
-- vision_used_per_term and created_at through PostgREST. The 055 cloud API keeps
-- working against a database without 055 (it writes users only with the service
-- role), so no API rollback is needed first. Run it only to unblock a regression
-- and roll forward again.
--
-- Idempotent: GRANT of a held privilege is a no-op, the policy is dropped before
-- it is created, the function is re-created as it is. The closing DO block
-- checks the restored state.

BEGIN;

GRANT INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON public.users
    TO anon, authenticated;

DROP POLICY IF EXISTS "Users update own profile" ON public.users;
CREATE POLICY "Users update own profile"
  ON public.users FOR UPDATE
  TO authenticated
  USING (id = (SELECT auth.uid()))
  WITH CHECK (id = (SELECT auth.uid()));

COMMENT ON POLICY "Users update own profile" ON public.users IS
  'Re-created with InitPlan auth.uid(). Semantics unchanged.';

CREATE OR REPLACE FUNCTION public.users_guard_protected_columns()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = ''
AS $$
DECLARE
    v_proven TEXT;
BEGIN
    -- (1) What only the server may change.
    IF current_user IN ('authenticated', 'anon') AND (
           NEW.role             IS DISTINCT FROM OLD.role
        OR NEW.training_credits IS DISTINCT FROM OLD.training_credits
        OR NEW.hf_username      IS DISTINCT FROM OLD.hf_username
        OR NEW.classroom_id     IS DISTINCT FROM OLD.classroom_id
        OR NEW.workgroup_id     IS DISTINCT FROM OLD.workgroup_id
    ) THEN
        RAISE EXCEPTION 'Diese Angaben deines Kontos kann nur der EduBotics-Server ändern.'
            USING ERRCODE = '42501';
    END IF;

    -- (2) A stored token fixes the account name (every caller).
    IF NEW.hf_username IS DISTINCT FROM OLD.hf_username THEN
        SELECT c.hf_username INTO v_proven
          FROM public.user_hf_credentials AS c
         WHERE c.user_id = NEW.id;
        IF FOUND AND NEW.hf_username IS DISTINCT FROM v_proven THEN
            RAISE EXCEPTION 'Dein Hugging-Face-Konto ist über dein gespeichertes Token festgelegt.'
                USING ERRCODE = 'P0044';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION public.users_guard_protected_columns() IS
    'BEFORE UPDATE guard on public.users (044): authenticated/anon may not change role, '
    'training_credits, hf_username, classroom_id or workgroup_id (42501); while a '
    'user_hf_credentials row exists, hf_username may only be its proven name (P0044, every '
    'caller). SECURITY INVOKER on purpose: current_user must be the request role.';

REVOKE EXECUTE ON FUNCTION public.users_guard_protected_columns() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.users_guard_protected_columns() FROM anon;
REVOKE EXECUTE ON FUNCTION public.users_guard_protected_columns() FROM authenticated;

DO $$
DECLARE
    v_sig REGPROCEDURE := to_regprocedure('public.users_guard_protected_columns()');
BEGIN
    IF NOT has_table_privilege('authenticated', 'public.users', 'UPDATE')
       OR NOT EXISTS (SELECT 1 FROM pg_policies
                       WHERE schemaname = 'public' AND tablename = 'users'
                         AND policyname = 'Users update own profile' AND cmd = 'UPDATE') THEN
        RAISE EXCEPTION '055 rollback: the own-row UPDATE path of 054 is not restored';
    END IF;
    IF v_sig IS NULL OR NOT EXISTS (
        SELECT 1 FROM pg_proc
         WHERE oid = v_sig AND NOT prosecdef AND proconfig = ARRAY['search_path=""']
           AND prosrc ~ 'NEW\.workgroup_id\s+IS DISTINCT FROM OLD\.workgroup_id'
           AND prosrc !~ 'NEW IS DISTINCT FROM OLD') THEN
        RAISE EXCEPTION '055 rollback: the users guard is not 044''s body';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
                    WHERE tgrelid = 'public.users'::regclass AND tgname = 'trg_0_users_protected_columns'
                      AND tgfoid = v_sig AND tgenabled = 'O') THEN
        RAISE EXCEPTION '055 rollback: trg_0_users_protected_columns is missing or disabled';
    END IF;
    RAISE NOTICE '055 rollback self-check passed';
END
$$;

COMMIT;

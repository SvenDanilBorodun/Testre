-- Rollback of 058 — refuse self-registration.
--
-- THIS RE-OPENS SELF-REGISTRATION (security audit SA-03) wherever the project's
-- sign-up switch (Dashboard, Authentication) is still on: a new auth.users row
-- without app_metadata.edubotics_provisioned commits again, and handle_new_user
-- gives it a public.users row (role student, no classroom), as before 058.
--
-- Drops exactly what 20261113130000_058_refuse_self_signup.sql added: the
-- constraint trigger trg_users_require_provisioned on public.users and its
-- function public.users_require_provisioned_account(). handle_new_user and
-- on_auth_user_created were never changed. The cloud API may keep sending the
-- marker (an unknown app_metadata key changes nothing), so no API rollback is
-- needed in either order.
--
-- ORDER: roll 058 back before 057.
--
-- Idempotent (DROP ... IF EXISTS).

BEGIN;

DROP TRIGGER IF EXISTS trg_users_require_provisioned ON public.users;
DROP FUNCTION IF EXISTS public.users_require_provisioned_account();

DO $$
BEGIN
    IF to_regprocedure('public.users_require_provisioned_account()') IS NOT NULL
       OR EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'public.users'::regclass
                     AND tgname = 'trg_users_require_provisioned') THEN
        RAISE EXCEPTION '058 rollback: the check is still there';
    END IF;
    RAISE NOTICE '058 rollback complete (self-registration is no longer refused by the database)';
END
$$;

COMMIT;

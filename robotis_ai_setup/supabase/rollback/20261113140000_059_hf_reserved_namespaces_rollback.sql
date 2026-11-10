-- Rollback of 059 — reserved Hugging Face namespaces.
--
-- RE-OPENS the database floor of SA-08: without these triggers the database
-- again accepts the platform's organisation (and the platform token's own
-- account) as a student's users.hf_username and as the namespace of a
-- datasets row; only the cloud API's own checks (hf_identity) remain.
--
-- Drops exactly what 20261113140000_059_hf_reserved_namespaces.sql added: the
-- triggers trg_1_users_hf_username_not_reserved and
-- trg_datasets_namespace_not_reserved, their two functions and the table
-- public.hf_reserved_namespaces (with the platform account the API recorded).
-- The hf_username values 059's data fix cleared are NOT restored (they could
-- only be forged). The cloud API tolerates a database without the table (it
-- logs and keeps its in-process deny-list), so no API rollback is needed.
--
-- ORDER: roll 059 back before 058 and 057.
--
-- Idempotent (DROP ... IF EXISTS).

BEGIN;

DROP TRIGGER IF EXISTS trg_1_users_hf_username_not_reserved ON public.users;
DROP TRIGGER IF EXISTS trg_datasets_namespace_not_reserved ON public.datasets;
DROP FUNCTION IF EXISTS public.users_hf_username_not_reserved();
DROP FUNCTION IF EXISTS public.datasets_namespace_not_reserved();
DROP TABLE IF EXISTS public.hf_reserved_namespaces;

DO $$
BEGIN
    IF to_regclass('public.hf_reserved_namespaces') IS NOT NULL
       OR to_regprocedure('public.users_hf_username_not_reserved()') IS NOT NULL
       OR to_regprocedure('public.datasets_namespace_not_reserved()') IS NOT NULL THEN
        RAISE EXCEPTION '059 rollback: an object of 059 is still there';
    END IF;
    RAISE NOTICE '059 rollback complete (the database no longer refuses reserved namespaces)';
END
$$;

COMMIT;

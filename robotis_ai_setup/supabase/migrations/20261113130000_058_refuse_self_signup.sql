-- 058 — a Supabase Auth account exists only if the EduBotics server created it:
-- any new auth.users row whose raw_app_meta_data does not carry
-- {"edubotics_provisioned": true} is refused, so a self-registration through
-- Supabase Auth's public sign-up (or an OAuth, phone, anonymous or dashboard
-- "Add user" sign-up) fails even while the project's sign-up switch is on
-- (security audit 2026-11-10, SA-03, code half; the dashboard switch is the
-- owner's half).
--
-- WHY. Sign-up is open with auto-confirm on the hosted project, so "a signed-in
-- user" means "anyone with the public anon key": a self-registered account
-- (handle_new_user gives it a public.users row, role student, no classroom)
-- reached the public storage bucket (SA-01), any claimed Orange Pi (SA-04),
-- PATCH /me + /datasets/sync (SA-08), username squatting and every quota a
-- signed-in account has. app_metadata is the server-only half of a Supabase
-- user: only the service role can set it (auth.admin.create_user's
-- app_metadata), never signUp's options.
--
-- WHY THE CHECK IS DEFERRED, AND NOT IN handle_new_user. Supabase Auth (GoTrue,
-- internal/api/admin.go::adminUserCreate, read 2026-11-13) INSERTs the auth.users
-- row with app_metadata = {"provider", "providers"} only, and writes the caller's
-- app_metadata with a separate UPDATE (models.User.UpdateAppMetaData) later in the
-- SAME transaction. handle_new_user fires AFTER INSERT immediately, so it sees the
-- row before the marker is there and would refuse every account the server
-- creates. The check is therefore a CONSTRAINT TRIGGER, DEFERRABLE INITIALLY
-- DEFERRED, that fires at COMMIT and re-reads the row as it is then. It sits on
-- public.users, which handle_new_user fills for every auth.users row in the same
-- transaction: a table this project owns (postgres may create and drop its
-- triggers on the hosted project, which it may not on auth.users), and the
-- rollback can drop it. A refused commit rolls the whole sign-up back: no
-- auth.users row, no public.users row; Supabase Auth answers its generic
-- "Database error saving new user".
--
-- EVERY PATH THAT CREATES AN ACCOUNT sets the marker (this repository, grepped
-- for create_user, auth.admin and the GoTrue admin API):
--   * cloud_training_api/app/routes/teacher.py::create_student and
--     cloud_training_api/app/routes/admin.py::create_teacher, through
--     app/services/provisioning.py::new_account_attributes;
--   * scripts/bootstrap_admin.py (the first admin);
--   * the hand-run assertion files' auth.users fixtures (supabase/tests/).
-- Nothing else creates one: the student SPA and the teacher web only sign in.
-- Existing accounts are not touched (the trigger fires on INSERT only).
--
-- WHAT IS ADDED (nothing else changes):
--   * public.users_require_provisioned_account() — SECURITY DEFINER (it reads
--     auth.users), SET search_path = '', EXECUTE for nobody but its owner (a
--     trigger function needs no grant);
--   * the constraint trigger trg_users_require_provisioned, AFTER INSERT ON
--     public.users, DEFERRABLE INITIALLY DEFERRED, FOR EACH ROW;
--   * a self-check of both, and that on_auth_user_created still fills
--     public.users (the chain the check relies on).
--
-- PROVEN 2026-11-13 on PostgreSQL 16 with .github/scripts/supabase_replay_stub.sql,
-- every migration 000 → 057 applied by psql, then this file:
-- supabase/tests/058_refuse_self_signup_assertions.sql (its header has the
-- PASS count and the cycle), including GoTrue's insert-then-update order.
--
-- API: no table, column or RPC the boot schema probe names changes. The cloud
-- API must send the marker BEFORE this migration is applied: deploy the API
-- first (W2 runs after W1 in release.yml, so apply 058 in the release whose API
-- already carries provisioning.py, or with the API of the same release when
-- nobody creates an account between W1 and W2 — a teacher's "Schüler anlegen"
-- in that window answers 500 and can simply be repeated after W2).
--
-- ROLLBACK: rollback/20261113130000_058_refuse_self_signup_rollback.sql drops
-- the trigger and the function (self-registration is possible again while the
-- dashboard's sign-up switch is on).
--
-- Idempotent: the function is re-created as it is, the trigger dropped and
-- created again.

BEGIN;

CREATE OR REPLACE FUNCTION public.users_require_provisioned_account()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_app JSONB;
BEGIN
    -- At COMMIT (deferred): the auth.users row as Supabase Auth left it, after
    -- its app_metadata UPDATE.
    SELECT u.raw_app_meta_data INTO v_app FROM auth.users AS u WHERE u.id = NEW.id;
    IF NOT FOUND THEN
        -- The account was deleted again in the same transaction (public.users
        -- went with it, ON DELETE CASCADE): nothing is created, nothing to judge.
        RETURN NULL;
    END IF;
    IF v_app IS NULL OR jsonb_typeof(v_app) <> 'object'
       OR (v_app -> 'edubotics_provisioned') IS DISTINCT FROM 'true'::jsonb THEN
        RAISE EXCEPTION 'account % was not created by the EduBotics server (app_metadata.edubotics_provisioned is missing): self-registration is refused', NEW.id
            USING ERRCODE = '42501';
    END IF;
    RETURN NULL;
END;
$$;

COMMENT ON FUNCTION public.users_require_provisioned_account() IS
    'Deferred check (058): a public.users row (handle_new_user fills one for every auth.users '
    'row) may only commit when its auth.users row carries app_metadata.edubotics_provisioned = '
    'true, which only the service role can set. Refuses self-registration (SA-03). Deferred '
    'because Supabase Auth writes app_metadata with an UPDATE after its INSERT.';

REVOKE ALL ON FUNCTION public.users_require_provisioned_account() FROM PUBLIC, anon, authenticated, service_role;

DROP TRIGGER IF EXISTS trg_users_require_provisioned ON public.users;
CREATE CONSTRAINT TRIGGER trg_users_require_provisioned
    AFTER INSERT ON public.users
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION public.users_require_provisioned_account();

DO $$
DECLARE
    v_sig  REGPROCEDURE := to_regprocedure('public.users_require_provisioned_account()');
    v_role TEXT;
BEGIN
    IF v_sig IS NULL THEN
        RAISE EXCEPTION '058: public.users_require_provisioned_account() is missing';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_proc WHERE oid = v_sig AND prosecdef
                      AND proconfig = ARRAY['search_path=""']
                      AND prosrc LIKE '%edubotics_provisioned%'
                      AND prosrc LIKE '%FROM auth.users%') THEN
        RAISE EXCEPTION '058: the check is not the SECURITY DEFINER function with an empty search_path described above';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_proc p, aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
                WHERE p.oid = v_sig AND a.grantee = 0) THEN
        RAISE EXCEPTION '058: PUBLIC holds EXECUTE on the check';
    END IF;
    FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
        IF has_function_privilege(v_role, v_sig, 'EXECUTE') THEN
            RAISE EXCEPTION '058: % holds EXECUTE on the check', v_role;
        END IF;
    END LOOP;
    -- Its owner (it runs as the owner) can read what it judges; else it would
    -- refuse every account, the server's included.
    IF NOT EXISTS (SELECT 1 FROM pg_proc p WHERE p.oid = v_sig
                      AND has_schema_privilege(p.proowner, 'auth', 'USAGE')
                      AND has_column_privilege(p.proowner, 'auth.users', 'id', 'SELECT')
                      AND has_column_privilege(p.proowner, 'auth.users', 'raw_app_meta_data', 'SELECT')) THEN
        RAISE EXCEPTION '058: the owner of the check may not read auth.users.raw_app_meta_data';
    END IF;
    -- Exactly one enabled constraint trigger, AFTER INSERT FOR EACH ROW, deferred.
    IF (SELECT count(*) FROM pg_trigger t
         WHERE t.tgrelid = 'public.users'::regclass AND t.tgname = 'trg_users_require_provisioned'
           AND t.tgfoid = v_sig AND t.tgenabled = 'O' AND t.tgconstraint <> 0
           AND t.tgdeferrable AND t.tginitdeferred
           AND (t.tgtype & 1) = 1 AND (t.tgtype & 2) = 0 AND (t.tgtype & 4) = 4) <> 1 THEN
        RAISE EXCEPTION '058: trg_users_require_provisioned is missing, disabled, not deferred or not AFTER INSERT FOR EACH ROW';
    END IF;
    -- The chain it relies on: every auth.users INSERT fills public.users in the
    -- same transaction.
    IF NOT EXISTS (SELECT 1 FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid
                    WHERE t.tgrelid = 'auth.users'::regclass AND t.tgname = 'on_auth_user_created'
                      AND t.tgenabled = 'O' AND (t.tgtype & 4) = 4
                      AND p.oid = 'public.handle_new_user()'::regprocedure
                      AND p.prosrc ~* 'INSERT\s+INTO\s+public\.users') THEN
        RAISE EXCEPTION '058: on_auth_user_created no longer fills public.users; the check would see nothing';
    END IF;
    RAISE NOTICE '058 self-check passed';
END
$$;

COMMIT;

-- 059 — the platform's Hugging Face namespaces are never a student's: the
-- database refuses the organisation and the platform token's own account as
-- users.hf_username and as the namespace of a datasets row (security audit
-- 2026-11-10, SA-08; the owner's scope extension of 2026-11-13).
--
-- WHY. users.hf_username drove POST /datasets/sync, the dataset sweep and a
-- teacher's deletion of a student's repos, all with the platform HF_TOKEN; PATCH
-- /me let a student without a stored token SET it, and its deny-list missed the
-- organisation. hf_username = 'EduBotics-Solutions' therefore listed the
-- organisation's PRIVATE datasets into the student's registry, and a later
-- deletion of that student would have erased them with the organisation's
-- write token. The cloud API now refuses every change through PATCH /me, reads
-- student namespaces anonymously, acts only on the PROVEN name and deletes only
-- the organisation models of the student's own trainings
-- (cloud_training_api/app/services/hf_identity.py). This file is the database
-- floor under that: 044's guard keeps the request roles off the column and pins
-- it to a stored credential's name (P0044); 059 adds the reserved names.
--
-- WHAT IS ADDED:
--   * public.hf_reserved_namespaces (name_lower PRIMARY KEY, kind, noted_at):
--     the lowercased namespaces no student may claim. Seeded with the
--     organisation, 'edubotics-solutions' (= hf_identity.HF_ORG.lower(), the one
--     constant; tests/test_supabase_059.py holds them equal). The cloud API adds
--     the platform token's own account, resolved by the Hub's whoami of HF_TOKEN
--     at boot (services/platform_identity.py) — never from client input. RLS on,
--     no policy, nothing for PUBLIC, anon or authenticated (057's default-deny),
--     service_role SELECT, INSERT, UPDATE (a reserved name is never removed by
--     the API: a rotated token's old account stays reserved).
--   * trg_1_users_hf_username_not_reserved: BEFORE INSERT OR UPDATE OF
--     hf_username ON public.users — a name whose lower(btrim()) is reserved is
--     refused (P0059), for EVERY caller: the service role, the token save
--     (store_user_hf_credential runs as its owner), a migration. It sorts after
--     044's trg_0_users_protected_columns, which still fires first.
--   * trg_datasets_namespace_not_reserved: BEFORE INSERT OR UPDATE OF hf_repo_id
--     ON public.datasets — a repo whose owner part (lower) is reserved is
--     refused with register_dataset_safe's own code P0034 (the API's existing
--     German 403), for register_dataset_safe, the sweep's direct insert and
--     any other writer. Existing rows are not judged until their hf_repo_id
--     changes.
--   * Both trigger functions are SECURITY DEFINER (they read the reserved table
--     whatever the caller holds) with an empty search_path; nobody executes them
--     directly.
--   * A data fix: a users.hf_username equal to a reserved name and backed by no
--     stored credential is cleared (it can only be a forged self-assertion; the
--     count is printed). No other row is touched; registry rows in the
--     organisation stay (the API never deletes an organisation dataset) and
--     are counted in a WARNING for the operator to review.
--   * A self-check of all of the above.
--
-- PROVEN 2026-11-13 on PostgreSQL 16 with .github/scripts/supabase_replay_stub.sql,
-- every migration 000 → 058 applied by psql, then this file:
-- supabase/tests/059_hf_reserved_namespaces_assertions.sql (its header has the
-- PASS count and the cycle).
--
-- API: the cloud API reads and writes the table best effort (a database
-- without 059 keeps its in-process deny-list in force), so it is not on the boot
-- schema probe; no other table, column or RPC changes shape.
--
-- ROLLBACK: rollback/20261113140000_059_hf_reserved_namespaces_rollback.sql
-- drops the two triggers, their functions and the table (the cleared names are
-- not restored: they were forged).
--
-- Idempotent: the table and the seed are IF NOT EXISTS / ON CONFLICT, the
-- functions are re-created, the triggers dropped and created again.

BEGIN;

CREATE TABLE IF NOT EXISTS public.hf_reserved_namespaces (
    name_lower TEXT PRIMARY KEY
        CHECK (name_lower = lower(btrim(name_lower)) AND name_lower ~ '^[a-z0-9](?:[a-z0-9-]{0,94}[a-z0-9])?$'),
    kind       TEXT NOT NULL CHECK (kind IN ('organisation', 'platform_account')),
    noted_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE public.hf_reserved_namespaces IS
    'Hugging Face namespaces no student may claim (059, SA-08): the platform organisation and the '
    'platform HF_TOKEN''s own account(s), lowercased. Written by the cloud API (service role) from the '
    'Hub''s whoami, never from client input. Read by the users/datasets triggers of 059.';

ALTER TABLE public.hf_reserved_namespaces ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.hf_reserved_namespaces FROM PUBLIC, anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE ON public.hf_reserved_namespaces TO service_role;

INSERT INTO public.hf_reserved_namespaces (name_lower, kind)
VALUES ('edubotics-solutions', 'organisation')
ON CONFLICT (name_lower) DO NOTHING;

CREATE OR REPLACE FUNCTION public.users_hf_username_not_reserved()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    IF NEW.hf_username IS NOT NULL
       AND EXISTS (SELECT 1 FROM public.hf_reserved_namespaces r
                    WHERE r.name_lower = lower(btrim(NEW.hf_username))) THEN
        RAISE EXCEPTION 'Dieses Hugging-Face-Konto ist nicht zulässig (Konto der EduBotics-Plattform).'
            USING ERRCODE = 'P0059';
    END IF;
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION public.users_hf_username_not_reserved() IS
    'BEFORE INSERT OR UPDATE OF hf_username ON public.users (059, SA-08): a reserved namespace '
    '(hf_reserved_namespaces, case-insensitive) is never a student''s Hugging Face name (P0059), '
    'for every caller.';

CREATE OR REPLACE FUNCTION public.datasets_namespace_not_reserved()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    IF position('/' IN NEW.hf_repo_id) > 0
       AND EXISTS (SELECT 1 FROM public.hf_reserved_namespaces r
                    WHERE r.name_lower = lower(btrim(split_part(NEW.hf_repo_id, '/', 1)))) THEN
        RAISE EXCEPTION 'Dieses Dataset gehört nicht zu deinem HuggingFace-Konto. Du kannst nur eigene Datasets registrieren.'
            USING ERRCODE = 'P0034';
    END IF;
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION public.datasets_namespace_not_reserved() IS
    'BEFORE INSERT OR UPDATE OF hf_repo_id ON public.datasets (059, SA-08): a repo in a reserved '
    'namespace (hf_reserved_namespaces, case-insensitive) is never registered to a student (P0034, '
    'register_dataset_safe''s own code).';

REVOKE ALL ON FUNCTION public.users_hf_username_not_reserved() FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION public.datasets_namespace_not_reserved() FROM PUBLIC, anon, authenticated, service_role;

DROP TRIGGER IF EXISTS trg_1_users_hf_username_not_reserved ON public.users;
CREATE TRIGGER trg_1_users_hf_username_not_reserved
    BEFORE INSERT OR UPDATE OF hf_username ON public.users
    FOR EACH ROW EXECUTE FUNCTION public.users_hf_username_not_reserved();

DROP TRIGGER IF EXISTS trg_datasets_namespace_not_reserved ON public.datasets;
CREATE TRIGGER trg_datasets_namespace_not_reserved
    BEFORE INSERT OR UPDATE OF hf_repo_id ON public.datasets
    FOR EACH ROW EXECUTE FUNCTION public.datasets_namespace_not_reserved();

-- The data fix: a reserved name no credential backs can only be forged.
DO $$
DECLARE
    v_n INT;
BEGIN
    UPDATE public.users u
       SET hf_username = NULL
     WHERE u.hf_username IS NOT NULL
       AND EXISTS (SELECT 1 FROM public.hf_reserved_namespaces r WHERE r.name_lower = lower(btrim(u.hf_username)))
       AND NOT EXISTS (SELECT 1 FROM public.user_hf_credentials c WHERE c.user_id = u.id);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RAISE NOTICE '059: cleared % forged hf_username value(s) naming a reserved namespace', v_n;
    -- Registry rows already in a reserved namespace are kept (nothing deletes
    -- an organisation dataset) but counted, for the operator to review.
    SELECT count(*) INTO v_n
      FROM public.datasets d
     WHERE EXISTS (SELECT 1 FROM public.hf_reserved_namespaces r
                    WHERE r.name_lower = lower(btrim(split_part(d.hf_repo_id, '/', 1))));
    IF v_n > 0 THEN
        RAISE WARNING '059: % datasets row(s) name a reserved namespace; review them (docs/KNOWN-ISSUES.md)', v_n;
    END IF;
END
$$;

DO $$
DECLARE
    v_fn   TEXT;
    v_sig  REGPROCEDURE;
    v_role TEXT;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM public.hf_reserved_namespaces
                    WHERE name_lower = 'edubotics-solutions' AND kind = 'organisation') THEN
        RAISE EXCEPTION '059: the organisation is not reserved';
    END IF;
    IF NOT (SELECT relrowsecurity FROM pg_class WHERE oid = 'public.hf_reserved_namespaces'::regclass) THEN
        RAISE EXCEPTION '059: hf_reserved_namespaces has no RLS';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_class c, aclexplode(c.relacl) a
                WHERE c.oid = 'public.hf_reserved_namespaces'::regclass
                  AND a.grantee IN (0, to_regrole('anon'), to_regrole('authenticated'))) THEN
        RAISE EXCEPTION '059: a request role or PUBLIC holds a privilege on hf_reserved_namespaces';
    END IF;
    IF NOT (has_table_privilege('service_role', 'public.hf_reserved_namespaces', 'SELECT')
            AND has_table_privilege('service_role', 'public.hf_reserved_namespaces', 'INSERT')
            AND has_table_privilege('service_role', 'public.hf_reserved_namespaces', 'UPDATE'))
       OR has_table_privilege('service_role', 'public.hf_reserved_namespaces', 'DELETE') THEN
        RAISE EXCEPTION '059: service_role must hold exactly SELECT, INSERT, UPDATE on hf_reserved_namespaces';
    END IF;
    FOREACH v_fn IN ARRAY ARRAY['users_hf_username_not_reserved', 'datasets_namespace_not_reserved'] LOOP
        v_sig := to_regprocedure('public.' || v_fn || '()');
        IF v_sig IS NULL OR NOT EXISTS (SELECT 1 FROM pg_proc WHERE oid = v_sig AND prosecdef
                                           AND proconfig = ARRAY['search_path=""']) THEN
            RAISE EXCEPTION '059: public.%() is missing or not SECURITY DEFINER with an empty search_path', v_fn;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_proc p, aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
                    WHERE p.oid = v_sig AND a.grantee = 0) THEN
            RAISE EXCEPTION '059: PUBLIC holds EXECUTE on public.%()', v_fn;
        END IF;
        FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
            IF has_function_privilege(v_role, v_sig, 'EXECUTE') THEN
                RAISE EXCEPTION '059: % holds EXECUTE on public.%()', v_role, v_fn;
            END IF;
        END LOOP;
    END LOOP;
    -- BEFORE ROW, INSERT and UPDATE OF the one column, enabled.
    IF (SELECT count(*) FROM pg_trigger t
         WHERE NOT t.tgisinternal AND t.tgenabled = 'O'
           AND (t.tgtype & 1) = 1 AND (t.tgtype & 2) = 2 AND (t.tgtype & 4) = 4 AND (t.tgtype & 16) = 16
           AND ((t.tgrelid = 'public.users'::regclass AND t.tgname = 'trg_1_users_hf_username_not_reserved'
                 AND t.tgfoid = 'public.users_hf_username_not_reserved()'::regprocedure
                 AND t.tgattr::text = (SELECT attnum::text FROM pg_attribute
                                               WHERE attrelid = 'public.users'::regclass AND attname = 'hf_username'))
             OR (t.tgrelid = 'public.datasets'::regclass AND t.tgname = 'trg_datasets_namespace_not_reserved'
                 AND t.tgfoid = 'public.datasets_namespace_not_reserved()'::regprocedure
                 AND t.tgattr::text = (SELECT attnum::text FROM pg_attribute
                                               WHERE attrelid = 'public.datasets'::regclass AND attname = 'hf_repo_id')))) <> 2 THEN
        RAISE EXCEPTION '059: a reserved-namespace trigger is missing, disabled or not BEFORE INSERT OR UPDATE OF its column';
    END IF;
    -- 044's guard still fires first on users.
    IF EXISTS (SELECT 1 FROM pg_trigger t
                WHERE t.tgrelid = 'public.users'::regclass AND NOT t.tgisinternal
                  AND (t.tgtype & 1) = 1 AND (t.tgtype & 2) = 2
                  AND t.tgname < 'trg_0_users_protected_columns') THEN
        RAISE EXCEPTION '059: a BEFORE ROW trigger on public.users sorts before 044''s guard';
    END IF;
    -- No forged reserved name is left.
    IF EXISTS (SELECT 1 FROM public.users u, public.hf_reserved_namespaces r
                WHERE r.name_lower = lower(btrim(u.hf_username))
                  AND NOT EXISTS (SELECT 1 FROM public.user_hf_credentials c WHERE c.user_id = u.id)) THEN
        RAISE EXCEPTION '059: a users row still names a reserved namespace';
    END IF;
    RAISE NOTICE '059 self-check passed';
END
$$;

COMMIT;

-- 055 — public.users: the request roles (anon, authenticated) can no longer
-- write a users row at all. 044's guard named five columns; the rest of a
-- student's own row stayed writable through PostgREST (docs/KNOWN-ISSUES.md,
-- findings F-25/F-57 of the launch plan, step 5):
--   * deletion_requested_at — the GDPR Art. 17 request the teacher web now lists
--     and a teacher confirms (routes/teacher.py::list_deletion_requests). A
--     student could clear or back-date it behind the API's back, and so hide a
--     request from the queue or move its one-month deadline;
--   * username, full_name — what a teacher sees in the roster and the queue;
--   * email — the account's synthetic address, mirrored from auth.users;
--   * created_by — which teacher created the account;
--   * vision_quota_per_term, vision_used_per_term (baseline 017) — a dead quota:
--     no route, page or worker reads them any more (the open-vocabulary detection
--     they metered was removed); only the three service-role SECURITY DEFINER
--     functions consume/refund/reset_vision_quota write them;
--   * created_at — the account's creation time.
--
-- THE AUDIT BEHIND IT (2026-11-11, this repository): no legitimate path writes
-- public.users as a request role.
--   * The React app (student and teacher web build alike) has exactly one
--     PostgREST table call, the trainings SELECT in hooks/useSupabaseTrainings.js;
--     no supabase-js write to users anywhere.
--   * Every cloud API write to users (PATCH /me's hf_username, /me/delete's
--     deletion_requested_at and workgroup_id, /me/delete/withdraw, the teacher's
--     create/patch/workgroup routes, the admin's teacher creation) runs on the
--     service-role client.
--   * Every SQL writer of users is SECURITY DEFINER and runs as its owner
--     (adjust_student_credits x2 and the three vision-quota functions in the
--     baseline, store_user_hf_credential in 042/045, handle_new_user's INSERT
--     from the auth.users trigger); no SECURITY INVOKER function and no trigger
--     that fires as the caller writes users; the ON DELETE SET NULL actions on
--     created_by, classroom_id and workgroup_id run as the table owner (the 055
--     assertions prove created_by's).
--
-- THE FIX — default-deny, the way 046 closed trainings, datasets, workflows and
-- tutorial_progress:
--   1. REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER on
--      public.users FROM anon, authenticated, at table level and at column level
--      (a column grant survives a table revoke). SELECT stays: the "Users read
--      consolidated" policy and its readers are untouched, and so is
--      service_role. A column added later is therefore protected without joining
--      any list. A FUTURE direct student write needs BOTH a policy AND a grant
--      again, in a migration of its own (CLAUDE.md Rule §4).
--   2. DROP POLICY "Users update own profile" (024): with no write privilege it
--      can never apply, and leaving it would read as a permission that is not
--      there.
--   3. 044's guard stays, as defense in depth, and its rule 1 now judges the
--      WHOLE row: current_user IN ('authenticated', 'anon') AND NEW IS DISTINCT
--      FROM OLD -> 42501 (044's German sentence), so a write privilege granted
--      back by mistake (a default-privileges replay, a hand GRANT) still changes
--      nothing. Rule 2 (P0044: while a user_hf_credentials row exists,
--      hf_username may only be its proven name) binds EVERY caller, the service
--      role included, and is byte-identical to 044's. The function stays
--      SECURITY INVOKER with an empty search_path: current_user must be the
--      request role, and a definer would see its owner (044's lesson).
--   4. A self-check that aborts (and so rolls back) the migration if either
--      request role can STILL write the table or any column of it — through a
--      PUBLIC grant or a role membership this file does not know about — if a
--      write policy is left, if service_role lost a privilege, or if the guard is
--      not the trigger described above.
--
-- PROVEN 2026-11-11 on PostgreSQL 16 with stub Supabase roles (anon,
-- authenticated, service_role), a stub auth schema, Supabase's default
-- privileges and the supabase_realtime publication, every migration 000 -> 054
-- applied by psql (and, separately, 000 -> 055 by `supabase db push
-- --include-all` 2.122.0, W1's command), then this file:
-- supabase/tests/055_users_identity_guard_assertions.sql (the PASS count and the
-- cycle are in its header). A student's PostgREST-shaped UPDATE of each column
-- answers 42501; the service role still writes every one of them.
--
-- API: no table, column or RPC changes shape; the cloud API's boot schema probe
-- (main.py::_validate_required_schema) is unchanged. The cloud API writes users
-- only with the service role, which keeps every privilege; an older API does the
-- same. PostgREST now answers a request-role PATCH/POST/DELETE on /users with
-- 42501 ("permission denied for table users").
--
-- ROLLBACK: rollback/20261111120000_055_users_identity_guard_rollback.sql
-- re-grants the six privileges, re-creates 024's own-row UPDATE policy and
-- restores 044's guard body verbatim (re-opens the columns above to the student).
--
-- Idempotent — re-runnable (REVOKE of a missing privilege and DROP POLICY IF
-- EXISTS are no-ops; the function and trigger are re-created as they are).

BEGIN;

-- 1. No request-role write privilege on the table …
REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON public.users
    FROM anon, authenticated;

-- … nor on any column of it (none is known to exist; a table-level REVOKE
-- leaves a column-level grant in place, and the self-check would then abort).
DO $$
DECLARE
    v_col TEXT;
BEGIN
    FOR v_col IN
        SELECT a.attname FROM pg_attribute a
         WHERE a.attrelid = 'public.users'::regclass AND a.attnum > 0 AND NOT a.attisdropped
    LOOP
        EXECUTE format('REVOKE INSERT (%1$I), UPDATE (%1$I), REFERENCES (%1$I) ON public.users FROM anon, authenticated',
                       v_col);
    END LOOP;
END
$$;

-- 2. The own-row write policy can never apply any more.
DROP POLICY IF EXISTS "Users update own profile" ON public.users;

-- 3. 044's guard, kept as defense in depth; rule 1 judges the whole row.
CREATE OR REPLACE FUNCTION public.users_guard_protected_columns()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = ''
AS $$
DECLARE
    v_proven TEXT;
BEGIN
    -- (1) A request role changes nothing on a users row (055: the whole row;
    --     044 named role, training_credits, hf_username, classroom_id and
    --     workgroup_id). Since 055 the request roles hold no write privilege on
    --     the table, so this fires only if one is granted back by mistake. The
    --     server writes as service_role, a SECURITY DEFINER function as its
    --     owner, a migration as postgres.
    IF current_user IN ('authenticated', 'anon') AND NEW IS DISTINCT FROM OLD THEN
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
    'BEFORE UPDATE guard on public.users (044, 055): authenticated/anon may change no column '
    'of a users row (42501, 055 widened 044''s five columns to the whole row and revoked the '
    'request roles'' write privileges, so this is defense in depth). While a '
    'user_hf_credentials row exists, hf_username may only be its proven name (P0044, every '
    'caller). SECURITY INVOKER on purpose: current_user must be the request role.';

-- Nobody calls a trigger function directly (044's revokes, repeated so a
-- re-created function cannot regain them).
REVOKE EXECUTE ON FUNCTION public.users_guard_protected_columns() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.users_guard_protected_columns() FROM anon;
REVOKE EXECUTE ON FUNCTION public.users_guard_protected_columns() FROM authenticated;

-- 044's trigger, re-stated so this file also stands on a database whose 044
-- trigger was dropped by hand; `trg_0_…` fires before every other BEFORE trigger.
DROP TRIGGER IF EXISTS trg_0_users_protected_columns ON public.users;
CREATE TRIGGER trg_0_users_protected_columns
    BEFORE UPDATE ON public.users
    FOR EACH ROW EXECUTE FUNCTION public.users_guard_protected_columns();

-- 4. The self-check.
DO $$
DECLARE
    v_sig  REGPROCEDURE := to_regprocedure('public.users_guard_protected_columns()');
    v_role TEXT;
    v_priv TEXT;
    v_col  TEXT;
BEGIN
    -- The request roles can write neither the table …
    FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        FOREACH v_priv IN ARRAY ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER'] LOOP
            IF has_table_privilege(v_role, 'public.users', v_priv) THEN
                RAISE EXCEPTION '055: % still holds % on public.users after the revoke (a PUBLIC grant or a role membership?)',
                    v_role, v_priv;
            END IF;
        END LOOP;
        -- … nor any column of it.
        FOR v_col IN
            SELECT a.attname FROM pg_attribute a
             WHERE a.attrelid = 'public.users'::regclass AND a.attnum > 0 AND NOT a.attisdropped
        LOOP
            FOREACH v_priv IN ARRAY ARRAY['INSERT', 'UPDATE', 'REFERENCES'] LOOP
                IF has_column_privilege(v_role, 'public.users', v_col, v_priv) THEN
                    RAISE EXCEPTION '055: % still holds % on public.users.%', v_role, v_priv, v_col;
                END IF;
            END LOOP;
        END LOOP;
    END LOOP;
    -- Reading stays as it was; the cloud API keeps every privilege it uses.
    IF NOT has_table_privilege('authenticated', 'public.users', 'SELECT') THEN
        RAISE EXCEPTION '055: authenticated lost SELECT on public.users';
    END IF;
    FOREACH v_priv IN ARRAY ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE'] LOOP
        IF NOT has_table_privilege('service_role', 'public.users', v_priv) THEN
            RAISE EXCEPTION '055: service_role lost % on public.users', v_priv;
        END IF;
    END LOOP;
    -- No write policy is left on the table.
    IF EXISTS (SELECT 1 FROM pg_policies
                WHERE schemaname = 'public' AND tablename = 'users' AND cmd <> 'SELECT') THEN
        RAISE EXCEPTION '055: public.users still carries a write policy';
    END IF;

    -- The guard: SECURITY INVOKER with an empty search_path …
    IF v_sig IS NULL THEN
        RAISE EXCEPTION '055: public.users_guard_protected_columns() is missing';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_proc
                    WHERE oid = v_sig AND NOT prosecdef
                      AND proconfig = ARRAY['search_path=""']) THEN
        RAISE EXCEPTION '055: the users guard is not SECURITY INVOKER with an empty search_path';
    END IF;
    -- … whose rule 1 judges the whole row and whose rule 2 is still there …
    IF NOT EXISTS (SELECT 1 FROM pg_proc
                    WHERE oid = v_sig
                      AND prosrc ~ 'current_user IN \(''authenticated'', ''anon''\) AND NEW IS DISTINCT FROM OLD'
                      AND prosrc ~ 'ERRCODE = ''42501'''
                      AND prosrc ~ 'FROM public\.user_hf_credentials'
                      AND prosrc ~ 'ERRCODE = ''P0044''') THEN
        RAISE EXCEPTION '055: the users guard body is not the whole-row rule 1 plus 044''s rule 2';
    END IF;
    -- … that nobody may call directly …
    IF EXISTS (SELECT 1 FROM pg_proc p, aclexplode(p.proacl) a
                WHERE p.oid = v_sig AND a.grantee = 0 AND a.privilege_type = 'EXECUTE') THEN
        RAISE EXCEPTION '055: PUBLIC holds EXECUTE on the users guard';
    END IF;
    FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF has_function_privilege(v_role, v_sig, 'EXECUTE') THEN
            RAISE EXCEPTION '055: % holds EXECUTE on the users guard', v_role;
        END IF;
    END LOOP;
    -- … run by exactly one enabled BEFORE UPDATE FOR EACH ROW trigger …
    IF (SELECT count(*) FROM pg_trigger t
         WHERE t.tgrelid = 'public.users'::regclass AND NOT t.tgisinternal
           AND t.tgname = 'trg_0_users_protected_columns' AND t.tgfoid = v_sig
           AND t.tgenabled = 'O'
           AND (t.tgtype & 1) = 1 AND (t.tgtype & 2) = 2 AND (t.tgtype & 16) = 16) <> 1 THEN
        RAISE EXCEPTION '055: trg_0_users_protected_columns is missing, disabled or not BEFORE UPDATE FOR EACH ROW';
    END IF;
    -- … that fires first: no other BEFORE ROW trigger on users sorts before it.
    IF EXISTS (SELECT 1 FROM pg_trigger t
                WHERE t.tgrelid = 'public.users'::regclass AND NOT t.tgisinternal
                  AND (t.tgtype & 1) = 1 AND (t.tgtype & 2) = 2
                  AND t.tgname < 'trg_0_users_protected_columns') THEN
        RAISE EXCEPTION '055: a BEFORE ROW trigger on public.users fires before the guard';
    END IF;
    RAISE NOTICE '055 self-check passed';
END
$$;

COMMIT;

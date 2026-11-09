-- 055 — public.users: a student (any request role) can no longer change ANY
-- column of their own row. 044's rule 1 named five columns; the rest of the row
-- stayed student-writable through PostgREST (docs/KNOWN-ISSUES.md, findings
-- F-25/F-57 of the launch plan, step 5):
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
--     functions consume/refund/reset_vision_quota write them, and those pass;
--   * created_at — the account's creation time.
--
-- THE CHANGE: rule 1 judges the WHOLE row. current_user IN ('authenticated',
-- 'anon') AND NEW IS DISTINCT FROM OLD -> RAISE 42501 (German, 044's sentence).
-- Composite IS DISTINCT FROM compares column by column with NULL equal to NULL,
-- so writing a column back with its CURRENT value is still no change and passes
-- (044's T8). The five 044 columns, the five named by the owner, the two vision
-- columns and created_at are covered — and so is every column a later migration
-- adds: a new users column is server-only until a migration of its own makes an
-- exception (the fail-closed rule 046 set for direct student writes). id was
-- already pinned by the own-row policy's WITH CHECK (id = auth.uid()).
-- Rule 2 (P0044: while a user_hf_credentials row exists, hf_username may only be
-- its proven name, for EVERY caller) is byte-identical to 044's.
-- The function stays SECURITY INVOKER with an empty search_path: current_user
-- must be the request role, and a definer would see its owner (044's lesson).
-- The UPDATE grant and the "Users update own profile" policy stay; with this
-- guard they can change nothing (a 046-style revoke is a later tidy-up, not
-- needed for the property).
--
-- THE AUDIT BEHIND IT (2026-11-11, this repository):
--   * the React app (student and teacher web build alike) has exactly one
--     PostgREST table call, the trainings SELECT in hooks/useSupabaseTrainings.js;
--     no supabase-js write to users anywhere;
--   * every cloud API write to users (PATCH /me's hf_username, /me/delete's
--     deletion_requested_at and workgroup_id, /me/delete/withdraw, the teacher's
--     create/patch/workgroup routes, the admin's teacher creation) runs on the
--     service-role client;
--   * every SQL writer of users is SECURITY DEFINER and runs as its owner
--     (adjust_student_credits x2 and the three vision-quota functions in the
--     baseline, store_user_hf_credential in 042/045), handle_new_user INSERTs
--     (this is an UPDATE trigger), and the ON DELETE SET NULL actions on
--     created_by, classroom_id and workgroup_id run as the table owner (RI
--     actions switch to the owner; the 055 assertions prove created_by's);
--   * nothing in supabase/migrations UPDATEs public.users as a request role.
--
-- PROVEN 2026-11-11 on PostgreSQL 16 with stub Supabase roles (anon,
-- authenticated, service_role), a stub auth schema and the supabase_realtime
-- publication, every migration 000 -> 054 applied by psql (and, separately,
-- 000 -> 055 by `supabase db push --include-all` 2.122.0, W1's command), then
-- this file: supabase/tests/055_users_identity_guard_assertions.sql prints
-- 27 PASS; the cycle 055 -> rollback -> rollback -> 055 -> 055 passes every
-- self-check, and after the rollback pg_dump -s equals 054's. A student's
-- PostgREST-shaped UPDATE of each of the eight newly guarded columns answers
-- 42501; the service role writes every one of them.
--
-- API: no table, column or RPC changes shape; the cloud API's boot schema probe
-- (main.py::_validate_required_schema) is unchanged. The 055 API reads and
-- writes deletion_requested_at with the service role, which passes; an older API
-- does the same.
--
-- ROLLBACK: rollback/20261111120000_055_users_identity_guard_rollback.sql
-- restores 044's body verbatim (re-opens the eight columns above to the student).
--
-- Idempotent — re-runnable.

BEGIN;

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
    --     workgroup_id). The server writes as service_role, a SECURITY DEFINER
    --     function as its owner, a migration as postgres.
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
    'of a users row (42501; 055 widened 044''s five columns to the whole row); while a '
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

DO $$
DECLARE
    v_sig REGPROCEDURE := to_regprocedure('public.users_guard_protected_columns()');
    v_role TEXT;
BEGIN
    IF v_sig IS NULL THEN
        RAISE EXCEPTION '055: public.users_guard_protected_columns() is missing';
    END IF;
    -- SECURITY INVOKER with an empty search_path (044's lesson: a definer sees its owner).
    IF NOT EXISTS (SELECT 1 FROM pg_proc
                    WHERE oid = v_sig AND NOT prosecdef
                      AND proconfig = ARRAY['search_path=""']) THEN
        RAISE EXCEPTION '055: the users guard is not SECURITY INVOKER with an empty search_path';
    END IF;
    -- Rule 1 judges the whole row for the request roles; rule 2 is still there.
    IF NOT EXISTS (SELECT 1 FROM pg_proc
                    WHERE oid = v_sig
                      AND prosrc ~ 'current_user IN \(''authenticated'', ''anon''\) AND NEW IS DISTINCT FROM OLD'
                      AND prosrc ~ 'ERRCODE = ''42501'''
                      AND prosrc ~ 'FROM public\.user_hf_credentials'
                      AND prosrc ~ 'ERRCODE = ''P0044''') THEN
        RAISE EXCEPTION '055: the users guard body is not the whole-row rule 1 plus 044''s rule 2';
    END IF;
    -- Nobody may call it directly.
    IF EXISTS (SELECT 1 FROM pg_proc p, aclexplode(p.proacl) a
                WHERE p.oid = v_sig AND a.grantee = 0 AND a.privilege_type = 'EXECUTE') THEN
        RAISE EXCEPTION '055: PUBLIC holds EXECUTE on the users guard';
    END IF;
    FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF has_function_privilege(v_role, v_sig, 'EXECUTE') THEN
            RAISE EXCEPTION '055: % holds EXECUTE on the users guard', v_role;
        END IF;
    END LOOP;
    -- Exactly one enabled BEFORE UPDATE FOR EACH ROW trigger on public.users runs it…
    IF (SELECT count(*) FROM pg_trigger t
         WHERE t.tgrelid = 'public.users'::regclass AND NOT t.tgisinternal
           AND t.tgname = 'trg_0_users_protected_columns' AND t.tgfoid = v_sig
           AND t.tgenabled = 'O'
           AND (t.tgtype & 1) = 1 AND (t.tgtype & 2) = 2 AND (t.tgtype & 16) = 16) <> 1 THEN
        RAISE EXCEPTION '055: trg_0_users_protected_columns is missing, disabled or not BEFORE UPDATE FOR EACH ROW';
    END IF;
    -- …and it fires first: no other BEFORE ROW trigger on users sorts before it.
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

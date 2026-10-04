-- 044 — public.users: a student can no longer change their own role, credits,
-- Hugging Face anchor, classroom or workgroup through PostgREST; and while a
-- stored token exists, users.hf_username can only ever be that token's PROVEN
-- account name, for every caller (owner decisions "DB hole" + e, 2026-10-04).
--
-- Proven on a scratch PostgreSQL 17 (public.ecr.aws/supabase/postgres:17.6.1.106,
-- the guarded baseline + every migration through 045) with
-- supabase/tests/044_users_protected_columns_assertions.sql -> 16 PASS;
-- rollback, re-apply, assertions again -> 16 PASS. The hole itself was
-- reproduced there first (an authenticated own-row UPDATE of `role` to 'admin'
-- succeeded before this migration), and rule (2)'s race was run in two
-- concurrent psql sessions (a service_role UPDATE of hf_username arriving while
-- store_user_hf_credential held the users row: P0044 after the commit, the
-- proven name kept). NOT yet run on a real `supabase start` stack.
-- Rollback: rollback/20261004130000_044_users_protected_columns_rollback.sql
-- (re-opens the hole; roll the cloud API back first only if it depends on P0044,
-- which the 044 API maps to its existing 409 — an older API simply answers 500).
--
-- THE HOLE (confirmed on the live database 2026-10-03/04)
--   Role `authenticated` holds UPDATE on public.users (Supabase's default
--   privileges) and the only UPDATE policy, "Users update own profile"
--   (baseline, re-created by 024), is row-level: USING/WITH CHECK
--   id = auth.uid(), no column restriction. So any signed-in student could
--   `PATCH /rest/v1/users?id=eq.<own id>` with {"role": "admin"} (every API
--   route trusts users.role), {"training_credits": 999} (free GPU time),
--   another student's {"hf_username": ...} (the dataset-discovery anchor), or a
--   foreign {"classroom_id"/"workgroup_id"} (another teacher's class or group,
--   and with it that group's shared credits).
--
-- THE FIX: one BEFORE UPDATE trigger, two rules.
--   (1) current_user IN ('authenticated', 'anon') and any of role,
--       training_credits, hf_username, classroom_id, workgroup_id IS DISTINCT
--       FROM its old value -> RAISE 42501 (German). `current_user` is the
--       PostgREST request role for a direct table write; the cloud API runs as
--       service_role, migrations as postgres, and a SECURITY DEFINER function
--       as its owner (store_user_hf_credential, adjust_student_credits,
--       adjust_workgroup_credits, consume/refund/reset_vision_quota all run as
--       a privileged owner) — all of them pass. The trigger function is
--       therefore SECURITY INVOKER on purpose: a definer trigger would see its
--       own owner as current_user and let everybody through. Writing a column
--       back with its CURRENT value is not a change and passes (a client that
--       PATCHes a whole row is not punished for the columns it did not touch).
--   (2) hf_username changes while public.user_hf_credentials holds a row for
--       the user, and the new value differs from that row's hf_username ->
--       RAISE P0044 (German), for EVERY caller, service_role included. This is
--       what makes PATCH /me's 409 atomic: the route's has_credential check
--       followed by its UPDATE could interleave with a token save; 042's header
--       claimed the users-row lock prevented that, but PATCH /me never took the
--       lock. Now the database refuses at write time. store_user_hf_credential
--       (042) writes the credential FIRST and then users.hf_username to the
--       same name, inside one transaction, so it passes rule (2) by construction.
--       A concurrent PATCH /me waits on the users row lock the RPC holds, and
--       its BEFORE trigger then reads the committed credential (READ COMMITTED,
--       a fresh snapshot per statement) and refuses.
--   The audit behind (1), 2026-10-04: every function in supabase/migrations
--   that UPDATEs public.users is SECURITY DEFINER and granted to service_role
--   only (adjust_student_credits x2 and the three vision-quota helpers in the
--   baseline, store_user_hf_credential in 042); every cloud API write to users
--   goes through the service-role client; the React app has no
--   `.from('users')` write (its only PostgREST table call is a trainings
--   SELECT). No legitimate path writes these five columns as authenticated or
--   anon.
--
-- NOT covered (writable by the student's own row as before, outside this
-- owner decision): username, full_name, email, created_by,
-- deletion_requested_at, vision_quota_per_term, vision_used_per_term
-- (docs/KNOWN-ISSUES.md).
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

-- Nobody calls a trigger function directly.
REVOKE EXECUTE ON FUNCTION public.users_guard_protected_columns() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.users_guard_protected_columns() FROM anon;
REVOKE EXECUTE ON FUNCTION public.users_guard_protected_columns() FROM authenticated;

-- `trg_0_…` sorts before every other trg_* trigger on public.users (BEFORE
-- triggers fire in name order), so a refused write is refused by THIS guard,
-- with its own code, before the capacity triggers count rows.
DROP TRIGGER IF EXISTS trg_0_users_protected_columns ON public.users;
CREATE TRIGGER trg_0_users_protected_columns
    BEFORE UPDATE ON public.users
    FOR EACH ROW EXECUTE FUNCTION public.users_guard_protected_columns();

COMMIT;

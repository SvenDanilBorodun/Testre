-- 046 — students write no table directly: the request roles `anon` and
-- `authenticated` lose every write privilege on public.trainings,
-- public.datasets, public.workflows and public.tutorial_progress, and the
-- own-row write policies that only those direct writes used are dropped
-- (owner decision, 2026-10-04).
--
-- Proven on a real `supabase start` stack (Postgres 17,
-- public.ecr.aws/supabase/postgres:17.6.1.106, the guarded baseline + every
-- migration through 046 applied with psql as `postgres`) with
-- supabase/tests/046_close_direct_student_writes_assertions.sql -> 27 PASS;
-- rollback, re-apply, rollback, re-apply, assertions again -> 27 PASS. The hole
-- itself was reproduced there first (before this migration an `authenticated`
-- own-row UPDATE of trainings.status to 'canceled', a DELETE of an own
-- training and an INSERT into datasets all succeeded).
-- Rollback: rollback/20261004150000_046_close_direct_student_writes_rollback.sql
-- (re-opens every hole below; no cloud API change depends on 046).
--
-- NUMBERING: 037 stays deliberately skipped (reserved for „Eigene Objekte" in
-- docs/plans/eigene-objekte-sql/). Timestamp 20261004150000 > 20261004140000
-- (045), so `supabase db push` orders it after 045.
--
-- THE HOLE (confirmed on the live database 2026-10-04)
--   Supabase's default privileges grant every public table to anon and
--   authenticated in full (INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES,
--   TRIGGER beside SELECT), and RLS was the only fence. These own-row write
--   policies let a signed-in student write their own rows through PostgREST
--   (baseline, re-created by 024; nothing since touched them):
--     public.trainings          "Users insert/update/delete own trainings"
--                               (TO authenticated)
--     public.datasets           "Owner inserts/updates/deletes own datasets"
--     public.workflows          "Owner inserts/updates/deletes own workflows"
--     public.tutorial_progress  "Tutorial progress owner insert/update/delete"
--   Credits are DERIVED from trainings.status (every credit check counts
--   status NOT IN ('failed','canceled')), so a student who PATCHed their own
--   training to 'canceled' or 'failed', or DELETEd it, got the credit back.
--   On a FINISHED training that is free GPU time outright (a succeeded run
--   flipped to 'failed' keeps its model); on a running one the worker stops
--   at its next progress write (P0001). One path to the running case stays
--   open after 046: the row's worker_token is readable through the trainings
--   SELECT and update_training_progress is executable by both request roles
--   (docs/KNOWN-ISSUES.md). 044 closed the direct
--   `users.training_credits` write only; this was the second half. A direct
--   datasets INSERT skipped register_dataset_safe's HF-author anchor (any
--   hf_repo_id under the student's own owner_user_id); a direct workflows
--   write skipped every check of routes/workflows.py (the size cap, the
--   validators, the code-row rule's German refusal ahead of the CHECK, the
--   rate limit); a direct tutorial_progress write skipped the validation of
--   PATCH /me/tutorial-progress/{id}.
--
-- THE AUDIT (2026-10-04): no legitimate path writes these tables as a request
-- role.
--   * Cloud API: every query uses services/supabase_client.py::get_supabase(),
--     the service-role client (no client is ever built from a user JWT or the
--     anon key).
--   * SQL: every function in supabase/migrations that writes one of the four
--     tables is SECURITY DEFINER, owned by `postgres` and granted to
--     service_role only, except update_training_progress (also anon +
--     authenticated, the Modal worker's anon-key path): start_training_safe
--     (INSERT trainings), update_training_progress (UPDATE trainings),
--     register_dataset_safe (INSERT/UPDATE datasets), update_workflow_blockly,
--     update_workflow_code and restore_workflow_version (UPDATE workflows).
--     A definer runs as its owner, so none of them needs the caller's table
--     privilege. The triggers on the four tables are BEFORE-row touch_*
--     functions (they only set NEW.updated_at) and the SECURITY DEFINER
--     snapshot_workflow_version (writes workflow_versions, not in scope). The
--     ON DELETE CASCADE foreign keys (038: trainings.user_id) run as the
--     table owner, not as the deleting role.
--   * Modal worker: anon key, its only database call is
--     rpc('update_training_progress', …).
--   * Jetson and Pi agents, the Windows GUI: no PostgREST call at all.
--   * React (student AND teacher-web builds, one codebase): the only table
--     call is useSupabaseTrainings' `.from('trainings').select(…)`; the
--     workflows, datasets and tutorial_progress hooks only open Realtime
--     channels, which need SELECT and nothing else.
--
-- WHAT CHANGES
--   1. REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER on the four
--      tables FROM anon, authenticated. SELECT stays (the SPA's trainings read
--      and every Realtime subscription), and service_role keeps everything.
--   2. DROP the twelve own-row write policies above: with no write privilege
--      they can never apply, and leaving them would read as a permission that
--      is not there. Every SELECT policy ("… read consolidated") is untouched,
--      and so is the supabase_realtime publication.
--   3. A self-check that aborts (and so rolls back) the migration if either
--      request role can STILL insert, update or delete one of the four tables
--      afterwards — e.g. through a PUBLIC grant or a role membership this file
--      does not know about. The privilege must be gone, not just the grant
--      this file revoked.
--
-- NOT IN SCOPE (docs/KNOWN-ISSUES.md): public.jetsons (teacher/admin write
-- policies) and public.workflow_versions (the trigger's INSERT policy) keep
-- their grants and policies.
--
-- A FUTURE direct student write needs BOTH a policy AND a grant again, in a
-- migration of its own (CLAUDE.md Rule §4).
--
-- Idempotent — re-runnable (REVOKE of a missing privilege and DROP POLICY IF
-- EXISTS are no-ops).

BEGIN;

REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON public.trainings, public.datasets, public.workflows, public.tutorial_progress
    FROM anon, authenticated;

DROP POLICY IF EXISTS "Users insert own trainings"     ON public.trainings;
DROP POLICY IF EXISTS "Users update own trainings"     ON public.trainings;
DROP POLICY IF EXISTS "Users delete own trainings"     ON public.trainings;

DROP POLICY IF EXISTS "Owner inserts own datasets"     ON public.datasets;
DROP POLICY IF EXISTS "Owner updates own datasets"     ON public.datasets;
DROP POLICY IF EXISTS "Owner deletes own datasets"     ON public.datasets;

DROP POLICY IF EXISTS "Owner inserts own workflows"    ON public.workflows;
DROP POLICY IF EXISTS "Owner updates own workflows"    ON public.workflows;
DROP POLICY IF EXISTS "Owner deletes own workflows"    ON public.workflows;

DROP POLICY IF EXISTS "Tutorial progress owner insert" ON public.tutorial_progress;
DROP POLICY IF EXISTS "Tutorial progress owner update" ON public.tutorial_progress;
DROP POLICY IF EXISTS "Tutorial progress owner delete" ON public.tutorial_progress;

DO $$
DECLARE
    v_table TEXT;
    v_role  TEXT;
    v_priv  TEXT;
BEGIN
    FOREACH v_table IN ARRAY ARRAY['public.trainings', 'public.datasets',
                                   'public.workflows', 'public.tutorial_progress'] LOOP
        FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
            FOREACH v_priv IN ARRAY ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE'] LOOP
                IF has_table_privilege(v_role, v_table, v_priv) THEN
                    RAISE EXCEPTION '046: % still holds % on % after the revoke (a PUBLIC grant or a role membership?)',
                        v_role, v_priv, v_table;
                END IF;
            END LOOP;
        END LOOP;
        IF EXISTS (SELECT 1 FROM pg_policies
                    WHERE schemaname = 'public'
                      AND tablename = split_part(v_table, '.', 2)
                      AND cmd <> 'SELECT') THEN
            RAISE EXCEPTION '046: % still carries a write policy', v_table;
        END IF;
    END LOOP;
END $$;

COMMIT;

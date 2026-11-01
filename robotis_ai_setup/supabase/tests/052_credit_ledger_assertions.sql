-- Behavioural assertions for migration 052 (the training-hour ledger: the four
-- tables, the start reservation, the settlement triggers, stop_training, the
-- changed 049/050 functions, the closes, the re-parent trigger, the switch-over,
-- the compatibility functions and every grant), run BY HAND against a fresh LOCAL
-- stack — never CI, never the linked project (it seeds rows and deletes users).
-- Labels T1..T41 of spec §19a in lettered parts; W8 is T21w. T42 (the S19
-- waiver needs proof, P0073) and T43 (the stall clock of update_training_progress)
-- are the fix round of 2026-11-01; T44 (mark_checkpoint_ready, with its races
-- in both orders), T45 (claim_checkpoint_mirror's status rule) and T46 (the
-- waiver's step condition) its second round.
--
-- T44's RACES use a SECOND REAL CONNECTION: the file creates the dblink extension
-- in schema extensions (and drops it again when it created it) and connects to
-- host=:race_host (default `db`, the Supabase CLI's network alias of the database
-- container: a TCP login with a password, which dblink requires of a
-- non-superuser; 127.0.0.1 is `trust` and refused) as postgres with
-- :race_password (default `postgres`, the local stack's). This session opens a
-- transaction, makes its call, sends the other call through dblink, waits until
-- pg_blocking_pids shows that call blocked by this session, commits, then reads
-- the other call's result.
--
-- The squashed baseline does NOT replay on a fresh database as shipped
-- (docs/KNOWN-ISSUES.md, „baseline.sql is NOT replayable from scratch"): run it in
-- a SCRATCH copy of supabase/ whose baseline has its one
-- `RENAME COLUMN runpod_job_id` line guarded exactly as
-- 040_code_programs_assertions.sql's header shows, with 052 moved OUT of the
-- copy's migrations/ (so `supabase start` applies every migration through 051),
-- and with its own project_id and ports in config.toml. Then, as `postgres`:
--   1. psql <local db url> -v seed=1 -f supabase/tests/052_credit_ledger_assertions.sql
--      (BEFORE 052: the pre-052 world — old credits on the old columns, managed
--      rows running, capped and publishing, cancel_requested, a legacy row)
--   2. psql <local db url> -v ON_ERROR_STOP=1 -f supabase/migrations/20261028120000_052_credit_ledger.sql
--      (prints „052 self-check passed")
--   3. psql <local db url> -f supabase/tests/052_credit_ledger_assertions.sql
-- Every DENIAL is asserted with has_*_privilege, or with SET LOCAL ROLE plus a
-- plain table statement inside a DO block (044's pattern) — never by calling a
-- function under SET ROLE: that kills the supabase/postgres:17.6.1.106 backend
-- (signal 11, SR-25). A statement sees one snapshot, so an RPC's write and the
-- check of it are separate statements.
--
-- POSTGREST (review B1; by hand, on the same database): with the service-role key
-- as apikey and bearer, POST /rest/v1/rpc/credit_quota, credit_teacher_summary,
-- credit_history and credit_reconcile answer 200 with data, and every probe body
-- of spec §12.1 (the 13 new RPCs and the four widened ones) answers its code ([]
-- for the readers, P0067 / P0066 / P0002 / P0049 for the writers) with the row
-- counts of the four tables, trainings and training_attempts unchanged; the
-- v2.36.0 API's own bodies (8-argument start, 3-argument continuation, 8-argument
-- finish, 2-argument cancel, the compat functions) resolve too. With the anon key,
-- and with an authenticated JWT, GET /rest/v1/<each of the four tables> answers
-- 42501 (401/403), a select of trainings.lost_reasons / charge_waiver /
-- worker_token / idempotency_key / settled_at 42501 and every credit RPC 42501,
-- while the student reads reserved_hours, charged_hours, stop_requested_by_role and
-- lost_seconds of the own rows. A service-role finish_training and
-- stop_training + complete_cancel_training through PostgREST settle their rows,
-- and a service-role PATCH of reserved_hours or an INSERT into trainings or the
-- ledger answers 42501. Measured 2026-10-28 with PostgREST v14.10: 104 checks,
-- every one as stated.
--
-- TWO-SESSION RACES (T29, T30, T41 by hand, on the database step 3 ran on; spec
-- §2 I9). The main phase leaves one fixture per race. Session A runs
--   BEGIN; <A>; SELECT pg_sleep(3); COMMIT;
-- and session B starts <B> one second later; neither may answer 40P01. Below,
-- u(n) is 'c0520000-0000-0000-0000-0000000000' || n, a start is
-- start_training_safe(u(n), 'u/r', 'c052/race', 'act', '{"steps":50000}', 50000, NULL, true)
-- and <name> is the id of the trainings row with that model_name.
--   R1 A start_continuation_safe(u('80'), <c052/race-parent80>, 'S28', 3),
--      B DELETE FROM auth.users WHERE id = u('80'): A reserves 3, B waits and
--      deletes (the child settles 0, the close reclaims the free hours).
--   R2 A DELETE FROM auth.users WHERE id = u('81'), B the continuation of
--      <c052/race-parent81>: P0002.
--   R3 A finish_training(<c052/race-run83>, 1, 'succeeded', NULL, NULL),
--      B DELETE FROM public.workgroups WHERE id = u('d3').
--   R4 A DELETE FROM public.workgroups WHERE id = u('d4'), B finish_training of
--      <c052/race-run84>: charge 1, release 2, reclaim 2.
--   R5 A a start of u('85'), B DELETE FROM public.workgroups WHERE id = u('d5'):
--      3 hours reserved from the group, which then closes with them.
--   R6 A DELETE FROM public.workgroups WHERE id = u('d6'), B a start of u('86'):
--      P0003 (the student is personal, no group reservation).
--   R7 A a start of u('87'), B credit_admin_compensate(u('a0'), 'group',
--      u('d7'), 1, 'Ausgleich', NULL, NULL). R8 the same swapped (u('d8'), u('88')).
--   R9 A UPDATE public.users SET workgroup_id = u('da') WHERE id = u('89'), B a
--      start of u('89'): funded by group da (R17, no error).
--   CONTROL (the test has teeth): A SELECT 1 FROM public.users WHERE id =
--      u('8c') FOR UPDATE; SELECT pg_sleep(2); then a start of u('8c'), against B
--      DELETE FROM public.workgroups WHERE id = u('dc'): 40P01 (the OLD order).
-- Measured 2026-10-28: R1–R9 no 40P01, each with the outcome written; the control
-- deadlocks.
--
-- ROLLBACK (by hand, after the races): the rollback file refuses while a ledger
-- row is queued/running/cancel_requested and changes nothing; after
--   UPDATE public.trainings SET status = 'canceled'
--    WHERE credit_scheme = 'ledger' AND status IN ('queued','running','cancel_requested');
-- (settled by the trigger) it applies („052 rollback self-check passed"): the old
-- 8-argument start creates a derived row, get_remaining_credits and
-- get_teacher_credit_summary answer from the old columns, adjust_student_credits
-- runs its old body, the ledger is kept and still refuses an UPDATE (P0068),
-- cancel_request_training has two arguments again, and 052 no longer applies (its
-- columns exist). Measured 2026-10-28. Re-measured 2026-11-01 with the fix
-- round's 052 (project credfixa): refused with 14 active ledger rows, applied after
-- the drain, and a dump of every public function (identity arguments, md5 of
-- prosrc, ACL, SECURITY DEFINER, proconfig, result type) plus trainings' table
-- ACL equals the dump taken at 051 except for the kept credit_ledger_immutable()
-- (EXECUTE for its owner only) and the kept credit_scheme column (DEFAULT
-- 'derived'); update_training_progress is 049's body again (md5
-- 5b9b815042f296fafd578b34faf1cbde, the rollback's own self-check) with 049's ACL.
-- Re-measured the same way with fix round 2's 052 (project credfixa2): refused
-- with 14 active ledger rows (mark_checkpoint_ready still there), applied after
-- the drain, the dump again equal to 051's but for the same two kept items;
-- mark_checkpoint_ready is gone and claim_checkpoint_mirror is 049's body (md5
-- abde11806259a987b58b00cf6a0b0bfc) with 049's ACL.
--
-- 052 BREAKS the 046–050 assertion files by design (spec §12.3; each header
-- records it). Measured after 052, on a stack with every migration through 051
-- plus 052: 046 prints 22 PASS / 4 FAIL / 2 ERROR, 047 7 / 2 / 4, 048 31 / 14 / 10,
-- 049 2 / 25 / 237, 050 1 / 1 / 77; and 044's T15 fails too (it calls
-- adjust_student_credits, which answers P0069 since 052): 15 PASS, 1 FAIL.
-- Re-measured 2026-11-01 with the fix round's 052, each file on a fresh reset:
-- every number unchanged (049 errors out before its stall-clock checks, and those
-- hold under the fix anyway: a first running write and a requeue). Re-measured
-- again with fix round 2's 052: unchanged.
-- Expected: 326 PASS, 0 FAIL/ERROR (one line per label: T1..T46 in lettered parts,
-- T21w = W8; some arrive as NOTICE lines from DO blocks).
\set QUIET on
\set ON_ERROR_STOP off
\pset pager off
\pset tuples_only on
\pset format unaligned

\if :{?seed}
-- =====================================================================================
-- PHASE 1 (run BEFORE 052, with -v seed=1): the pre-052 world. Old credits on the old
-- columns, and six pre-052 trainings: a managed row running, one capped and publishing,
-- one cancel_requested, an unmanaged legacy row, a finished one and a group row.
-- =====================================================================================
\set ON_ERROR_STOP on
INSERT INTO auth.users (id, instance_id, aud, role, email, encrypted_password, created_at, updated_at)
SELECT ('c0520000-0000-0000-0000-0000000000' || n)::uuid, '00000000-0000-0000-0000-000000000000', 'authenticated',
       'authenticated', n || '-052@x.test', '', now(), now()
  FROM unnest(ARRAY['a0','f1','f2','f3','51','52','53','54','55','56']) AS n;
UPDATE public.users SET role = 'admin' WHERE id = 'c0520000-0000-0000-0000-0000000000a0';
UPDATE public.users SET role = 'teacher', training_credits = 20
 WHERE id IN ('c0520000-0000-0000-0000-0000000000f1','c0520000-0000-0000-0000-0000000000f2',
              'c0520000-0000-0000-0000-0000000000f3');
INSERT INTO public.classrooms (id, teacher_id, name) VALUES
  ('c0520000-0000-0000-0000-0000000000c1', 'c0520000-0000-0000-0000-0000000000f1', 'K1-052'),
  ('c0520000-0000-0000-0000-0000000000c2', 'c0520000-0000-0000-0000-0000000000f2', 'K2-052'),
  ('c0520000-0000-0000-0000-0000000000c9', 'c0520000-0000-0000-0000-0000000000a0', 'K9-052 (admin)');
UPDATE public.users SET classroom_id = 'c0520000-0000-0000-0000-0000000000c1', training_credits = 5
 WHERE id IN ('c0520000-0000-0000-0000-000000000051','c0520000-0000-0000-0000-000000000052',
              'c0520000-0000-0000-0000-000000000053','c0520000-0000-0000-0000-000000000054');
UPDATE public.users SET classroom_id = 'c0520000-0000-0000-0000-0000000000c2', training_credits = 5
 WHERE id = 'c0520000-0000-0000-0000-000000000055';
UPDATE public.users SET training_credits = 3 WHERE id = 'c0520000-0000-0000-0000-000000000056';
INSERT INTO public.workgroups (id, classroom_id, name, shared_credits)
VALUES ('c0520000-0000-0000-0000-0000000000a1', 'c0520000-0000-0000-0000-0000000000c1', 'G1-052', 4);
UPDATE public.users SET workgroup_id = 'c0520000-0000-0000-0000-0000000000a1'
 WHERE id IN ('c0520000-0000-0000-0000-000000000053','c0520000-0000-0000-0000-000000000054');
-- the pre-052 rows, through the old 8-argument start
SELECT training_id FROM public.start_training_safe('c0520000-0000-0000-0000-000000000051', 'ds/x', 'c052/old-run', 'act',
  '{"steps":50000,"save_freq":2500}'::jsonb, 50000, NULL, true);
UPDATE public.trainings SET status = 'running', current_attempt = 1 WHERE model_name = 'c052/old-run';
INSERT INTO public.training_attempts (training_id, attempt_no, state, backend, cloud_ref, first_heartbeat_at)
  SELECT id, 1, 'running', 'runpod_secure', 'pod-oldrun', now() - interval '30 min' FROM public.trainings WHERE model_name = 'c052/old-run';
SELECT training_id FROM public.start_training_safe('c0520000-0000-0000-0000-000000000052', 'ds/y', 'c052/old-cap', 'act',
  '{"steps":50000,"save_freq":2500}'::jsonb, 50000, NULL, true);
UPDATE public.trainings SET status = 'running', current_attempt = 1, capped_step = 40000, train_seconds_used = 12600
 WHERE model_name = 'c052/old-cap';
INSERT INTO public.training_attempts (training_id, attempt_no, state, backend, cloud_ref, first_heartbeat_at, ended_at, end_reason)
  SELECT id, 1, 'ended', 'runpod_secure', 'pod-oldcap', now() - interval '4 hours', now() - interval '10 min', 'capped'
    FROM public.trainings WHERE model_name = 'c052/old-cap';
INSERT INTO public.training_checkpoints (training_id, attempt_no, step, state, manifest, ready_at)
  SELECT id, 1, 40000, 'ready', '{}'::jsonb, now() - interval '20 min' FROM public.trainings WHERE model_name = 'c052/old-cap';
SELECT training_id FROM public.start_training_safe('c0520000-0000-0000-0000-000000000055', 'ds/z', 'c052/old-cr', 'act',
  '{"steps":50000}'::jsonb, 50000, NULL, true);
UPDATE public.trainings SET status = 'cancel_requested', current_attempt = 1, cancel_attempts = 1 WHERE model_name = 'c052/old-cr';
INSERT INTO public.training_attempts (training_id, attempt_no, state, backend, cloud_ref, first_heartbeat_at)
  SELECT id, 1, 'running', 'runpod_secure', 'pod-oldcr', now() - interval '50 min' FROM public.trainings WHERE model_name = 'c052/old-cr';
SELECT training_id FROM public.start_training_safe('c0520000-0000-0000-0000-000000000051', 'ds/x', 'c052/old-legacy', 'diffusion',
  '{"steps":50000}'::jsonb, 50000, gen_random_uuid(), false);
UPDATE public.trainings SET status = 'running' WHERE model_name = 'c052/old-legacy';
SELECT training_id FROM public.start_training_safe('c0520000-0000-0000-0000-000000000052', 'ds/y', 'c052/old-done', 'act',
  '{"steps":50000}'::jsonb, 50000, NULL, true);
UPDATE public.trainings SET status = 'succeeded' WHERE model_name = 'c052/old-done';
SELECT training_id FROM public.start_training_safe('c0520000-0000-0000-0000-000000000053', 'ds/g', 'c052/old-grp', 'act',
  '{"steps":50000}'::jsonb, 50000, NULL, true);
UPDATE public.trainings SET status = 'running', current_attempt = 1 WHERE model_name = 'c052/old-grp';
INSERT INTO public.training_attempts (training_id, attempt_no, state, backend, cloud_ref, first_heartbeat_at)
  SELECT id, 1, 'running', 'runpod_community', 'pod-oldgrp', now() - interval '70 min' FROM public.trainings WHERE model_name = 'c052/old-grp';
SELECT 'seeded the pre-052 world: ' || count(*) || ' trainings' FROM public.trainings WHERE model_name LIKE 'c052/old-%';
\else
-- =====================================================================================
-- PHASE 2 (run AFTER 052, without -v seed): the assertions. Every DENIAL is asserted with
-- has_*_privilege, or with SET LOCAL ROLE + a plain table statement inside a DO block
-- (044's pattern) — never by calling a function under SET ROLE.
-- =====================================================================================
SELECT (to_regclass('public.credit_ledger') IS NOT NULL
        AND EXISTS (SELECT 1 FROM public.trainings WHERE model_name = 'c052/old-cap')) AS ready_052 \gset
\if :ready_052
\else
\echo 'ABORT: run this file with -v seed=1 BEFORE 052 and once more after it (see the header).'
\quit
\endif

CREATE OR REPLACE FUNCTION pg_temp.u(n text) RETURNS uuid LANGUAGE sql IMMUTABLE AS
$$ SELECT ('c0520000-0000-0000-0000-0000000000' || n)::uuid $$;
CREATE OR REPLACE FUNCTION pg_temp.ok(pass_line text, cond boolean, detail text DEFAULT NULL) RETURNS text
LANGUAGE sql AS $$
  SELECT CASE WHEN cond THEN pass_line
              ELSE 'FAIL ' || regexp_replace(pass_line, '^PASS ', '') || COALESCE(' [' || detail || ']', '') END $$;
CREATE OR REPLACE FUNCTION pg_temp.err(q text, code text, pass_line text) RETURNS text LANGUAGE plpgsql AS $$
DECLARE label text := regexp_replace(pass_line, '^PASS ', '');
BEGIN
  EXECUTE q;
  RETURN 'FAIL ' || label || ' (no error)';
EXCEPTION WHEN OTHERS THEN
  IF SQLSTATE = code THEN RETURN pass_line || ' (' || SQLSTATE || ')'; END IF;
  RETURN 'FAIL ' || label || ' (' || SQLSTATE || ': ' || SQLERRM || ')';
END $$;
CREATE OR REPLACE FUNCTION pg_temp.acct(k text, o uuid) RETURNS text LANGUAGE sql AS $$
  SELECT COALESCE((SELECT free || '/' || reserved || '/' || spent FROM public.credit_accounts
                    WHERE kind = k AND owner_id = o), 'none') $$;
CREATE OR REPLACE FUNCTION pg_temp.led(t int) RETURNS text LANGUAGE sql AS $$
  SELECT COALESCE(string_agg(kind || ':' || hours, ',' ORDER BY id), '') FROM public.credit_ledger WHERE training_id = t $$;
CREATE OR REPLACE FUNCTION pg_temp.tid(m text) RETURNS int LANGUAGE sql AS $$
  SELECT id FROM public.trainings WHERE model_name = m $$;
-- I10: every hour was created by an admin row
CREATE OR REPLACE FUNCTION pg_temp.identity() RETURNS text LANGUAGE sql AS $$
  SELECT (SELECT COALESCE(sum(hours), 0) FROM public.credit_ledger WHERE kind IN ('grant','compensate'))::text || '='
      || ((SELECT COALESCE(sum(free + reserved + spent), 0) FROM public.credit_accounts)
          + (SELECT COALESCE(sum(hours), 0) FROM public.credit_ledger WHERE kind = 'revoke'))::text $$;
CREATE OR REPLACE FUNCTION pg_temp.identity_ok() RETURNS boolean LANGUAGE sql AS $$
  SELECT split_part(pg_temp.identity(), '=', 1) = split_part(pg_temp.identity(), '=', 2) $$;
-- a post-052 user (the profile row comes from 052-independent handle_new_user)
CREATE OR REPLACE FUNCTION pg_temp.mk_user(n text, r text, cls text) RETURNS uuid LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO auth.users (id, instance_id, aud, role, email, encrypted_password, created_at, updated_at)
  VALUES (pg_temp.u(n), '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', n || '-052@x.test', '',
          now(), now());
  UPDATE public.users SET role = r::public.user_role, classroom_id = CASE WHEN cls IS NULL THEN NULL ELSE pg_temp.u(cls) END
   WHERE id = pg_temp.u(n);
  RETURN pg_temp.u(n);
END $$;
CREATE OR REPLACE FUNCTION pg_temp.mk_class(n text, teacher text) RETURNS uuid LANGUAGE sql AS $$
  INSERT INTO public.classrooms (id, teacher_id, name) VALUES (pg_temp.u(n), pg_temp.u(teacher), 'K-' || n || '-052') RETURNING id $$;
CREATE OR REPLACE FUNCTION pg_temp.mk_group(n text, cls text) RETURNS uuid LANGUAGE sql AS $$
  INSERT INTO public.workgroups (id, classroom_id, name) VALUES (pg_temp.u(n), pg_temp.u(cls), 'G-' || n || '-052') RETURNING id $$;
CREATE OR REPLACE FUNCTION pg_temp.give(teacher text, k text, target text, h int) RETURNS int LANGUAGE sql AS $$
  SELECT o_target_free FROM public.credit_assign(pg_temp.u(teacher), k, pg_temp.u(target), h, NULL) $$;
-- a ledger training RUNNING on attempt 1, its first running write p_secs ago
CREATE OR REPLACE FUNCTION pg_temp.run(p_user text, p_model text, p_secs int, p_max int DEFAULT 3) RETURNS int
LANGUAGE plpgsql AS $$
DECLARE v_id int;
BEGIN
  SELECT s.training_id INTO v_id FROM public.start_training_safe(pg_temp.u(p_user), 'u/ds', p_model, 'act',
    '{"steps":50000,"save_freq":2500}'::jsonb, 50000, NULL, true, NULL, p_max) s;
  PERFORM public.claim_training_attempt(v_id, 0);
  PERFORM public.attach_training_attempt(v_id, 1, 'runpod_secure', 'pod-' || v_id || '-1');
  UPDATE public.training_attempts SET state = 'running', first_heartbeat_at = now() - make_interval(secs => p_secs)
   WHERE training_id = v_id AND attempt_no = 1;
  UPDATE public.trainings SET status = 'running', last_progress_at = now(), step_advanced_at = now() WHERE id = v_id;
  RETURN v_id;
END $$;
-- the next attempt of a requeued (queued) training; p_secs NULL = placed, never ran
CREATE OR REPLACE FUNCTION pg_temp.attempt(p_tid int, p_secs int) RETURNS int LANGUAGE plpgsql AS $$
DECLARE v_n int;
BEGIN
  SELECT o_attempt_no INTO v_n FROM public.claim_training_attempt(p_tid, (SELECT current_attempt FROM public.trainings WHERE id = p_tid));
  PERFORM public.attach_training_attempt(p_tid, v_n, 'runpod_community', 'pod-' || p_tid || '-' || v_n);
  IF p_secs IS NOT NULL THEN
    UPDATE public.training_attempts SET state = 'running', first_heartbeat_at = now() - make_interval(secs => p_secs)
     WHERE training_id = p_tid AND attempt_no = v_n;
    UPDATE public.trainings SET status = 'running', last_progress_at = now(), step_advanced_at = now() WHERE id = p_tid;
  END IF;
  RETURN v_n;
END $$;
-- a ready checkpoint of attempt `att` at `step`, created `ago` seconds ago
CREATE OR REPLACE FUNCTION pg_temp.ckpt(p_tid int, att int, step int, ago int DEFAULT 0) RETURNS void LANGUAGE sql AS $$
  INSERT INTO public.training_checkpoints (training_id, attempt_no, step, state, manifest, ready_at, created_at)
  VALUES (p_tid, att, step, 'ready', '{}', now() - make_interval(secs => ago), now() - make_interval(secs => ago)) $$;
-- an UPLOADING checkpoint (what upload-url inserts), created `ago` seconds ago
CREATE OR REPLACE FUNCTION pg_temp.ckpt_up(p_tid int, att int, step int, ago int DEFAULT 0) RETURNS void LANGUAGE sql AS $$
  INSERT INTO public.training_checkpoints (training_id, attempt_no, step, state, manifest, created_at)
  VALUES (p_tid, att, step, 'uploading', '{}', now() - make_interval(secs => ago)) $$;
-- true once the backend `bpid` waits on a lock THIS session holds (live: pg_blocking_pids), at most 5 s
CREATE OR REPLACE FUNCTION pg_temp.blocked_by_me(bpid int) RETURNS boolean LANGUAGE plpgsql AS $$
DECLARE i int := 0;
BEGIN
  WHILE NOT (pg_backend_pid() = ANY (pg_blocking_pids(bpid))) LOOP
    i := i + 1;
    IF i > 200 THEN RETURN false; END IF;
    PERFORM pg_sleep(0.025);
  END LOOP;
  RETURN true;
END $$;

\echo '=== T21w-z the switch-over (decision 11), before any write ==='
SELECT pg_temp.ok('PASS T21w (W8) get_teacher_credit_summary right after the switch-over = (0,0,0,4,1,0)',
  (SELECT row(g.*)::text FROM public.get_teacher_credit_summary(pg_temp.u('f1')) g) = '(0,0,0,4,1,0)',
  (SELECT row(g.*)::text FROM public.get_teacher_credit_summary(pg_temp.u('f1')) g));
SELECT pg_temp.ok('PASS T21x a zero account for every teacher, classroom owner (the admin too, I13), classroom student and group; none for the student without a classroom',
  (SELECT string_agg(kind || ':' || right(owner_id::text, 2) || ':' || COALESCE(right(parent_teacher_id::text, 2), '-')
                     || ':' || free || reserved || spent, ',' ORDER BY kind, owner_id)
     FROM public.credit_accounts WHERE owner_id::text LIKE 'c0520000-%')
  = 'group:a1:f1:000,student:51:f1:000,student:52:f1:000,student:53:f1:000,student:54:f1:000,student:55:f2:000,'
    'teacher:a0:-:000,teacher:f1:-:000,teacher:f2:-:000,teacher:f3:-:000',
  (SELECT string_agg(kind || ':' || right(owner_id::text, 2), ',' ORDER BY kind, owner_id) FROM public.credit_accounts
    WHERE owner_id::text LIKE 'c0520000-%'));
SELECT pg_temp.ok('PASS T21y one 0-hour system migrate row per account with old_credits / old_used',
  (SELECT count(*) = 10 AND bool_and(hours = 0 AND actor_role = 'system')
          AND string_agg(right(to_id::text, 2) || ':' || (meta->>'old_credits') || '/' || (meta->>'old_used'), ','
                         ORDER BY to_id::text) FILTER (WHERE right(to_id::text, 2) IN ('a1','51','53','55','f1'))
              = '51:5/2,53:5/0,55:5/1,a1:4/1,f1:20/24'
     FROM public.credit_ledger WHERE kind = 'migrate' AND to_id::text LIKE 'c0520000-%'),
  (SELECT string_agg(right(to_id::text, 2) || ':' || meta::text, ' ') FROM public.credit_ledger WHERE kind = 'migrate'));
SELECT pg_temp.ok('PASS T21z the six pre-052 rows are derived with no reserve; new rows default to ledger',
  (SELECT count(*) = 6 AND bool_and(credit_scheme = 'derived' AND reserved_hours IS NULL)
     FROM public.trainings WHERE model_name LIKE 'c052/old-%')
  AND NOT EXISTS (SELECT 1 FROM public.credit_ledger l JOIN public.trainings t ON t.id = l.training_id
                   WHERE t.model_name LIKE 'c052/old-%')
  AND (SELECT column_default FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'trainings' AND column_name = 'credit_scheme') = '''ledger''::text');

\echo '=== T1 the ledger is append-only, even for its owner; its shape CHECK holds ==='
SELECT pg_temp.err('UPDATE public.credit_ledger SET hours = hours WHERE kind = ''migrate''', 'P0068',
  'PASS T1a UPDATE of a ledger row as the owner raises P0068');
SELECT pg_temp.err('DELETE FROM public.credit_ledger WHERE kind = ''migrate''', 'P0068',
  'PASS T1b DELETE of a ledger row as the owner raises P0068');
SELECT pg_temp.err('TRUNCATE public.credit_ledger', 'P0068', 'PASS T1c TRUNCATE of the ledger as the owner raises P0068');
SELECT pg_temp.err($q$INSERT INTO public.credit_ledger (kind, hours, to_kind, to_id, to_bucket, actor_id, actor_role)
  VALUES ('compensate', 1, 'student', pg_temp.u('51'), 'free', pg_temp.u('a0'), 'admin')$q$, '23514',
  'PASS T1d a compensate row without a reason violates credit_ledger_shape');
SELECT pg_temp.err($q$INSERT INTO public.credit_ledger (kind, hours, to_kind, to_id, to_bucket, actor_id, actor_role, reason_id)
  VALUES ('compensate', 1, 'student', pg_temp.u('51'), 'free', pg_temp.u('f1'), 'teacher', 1)$q$, '23514',
  'PASS T1e a compensate row by a teacher violates credit_ledger_shape');
SELECT pg_temp.ok('PASS T1f the ten migrate rows are still there, unchanged',
  (SELECT count(*) = 10 AND bool_and(hours = 0) FROM public.credit_ledger WHERE kind = 'migrate' AND to_id::text LIKE 'c0520000-%'));

\echo '=== T2 privileges (the in-migration self-check, repeated) ==='
SELECT pg_temp.ok('PASS T2a anon and authenticated hold no privilege of the seven kinds on the four tables',
  NOT EXISTS (SELECT 1 FROM unnest(ARRAY['anon','authenticated']) r,
                     unnest(ARRAY['public.credit_accounts','public.credit_ledger','public.credit_reasons','public.training_start_refusals']) t,
                     unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) p
               WHERE has_table_privilege(r, t, p)));
SELECT pg_temp.ok('PASS T2b service_role holds SELECT and nothing else on the four tables',
  (SELECT bool_and(has_table_privilege('service_role', t, 'SELECT')
                   AND NOT has_table_privilege('service_role', t, 'INSERT') AND NOT has_table_privilege('service_role', t, 'UPDATE')
                   AND NOT has_table_privilege('service_role', t, 'DELETE') AND NOT has_table_privilege('service_role', t, 'TRUNCATE')
                   AND NOT has_table_privilege('service_role', t, 'REFERENCES') AND NOT has_table_privilege('service_role', t, 'TRIGGER'))
     FROM unnest(ARRAY['public.credit_accounts','public.credit_ledger','public.credit_reasons','public.training_start_refusals']) t));
SELECT pg_temp.ok('PASS T2c nobody (anon, authenticated, service_role, PUBLIC) holds USAGE, SELECT or UPDATE on the three identity sequences',
  NOT EXISTS (SELECT 1 FROM unnest(ARRAY['anon','authenticated','service_role','public']) r,
                     unnest(ARRAY['public.credit_ledger','public.credit_reasons','public.training_start_refusals']) t,
                     unnest(ARRAY['USAGE','SELECT','UPDATE']) p
               WHERE has_sequence_privilege(r, pg_get_serial_sequence(t, 'id'), p)));
SELECT pg_temp.ok('PASS T2d no request role executes any new, replaced, re-created, changed or compat function',
  NOT EXISTS (SELECT 1 FROM pg_proc pr, unnest(ARRAY['anon','authenticated']) r
               WHERE pr.pronamespace = 'public'::regnamespace
                 AND (pr.proname LIKE '\_credit\_%' OR pr.proname LIKE 'credit\_%'
                      OR pr.proname IN ('start_training_safe','start_continuation_safe','stop_training','record_training_refusal',
                                        'get_remaining_credits','get_teacher_credit_summary','adjust_student_credits',
                                        'adjust_workgroup_credits','cancel_request_training','finish_training',
                                        'complete_cancel_training','claim_training_publish','cap_training_attempt',
                                        'requeue_training','trainings_guard_credit_columns','trainings_credit_settle',
                                        'trainings_credit_settle_delete','users_credit_close','workgroups_credit_close'))
                 AND has_function_privilege(r, pr.oid, 'EXECUTE')));
SELECT pg_temp.ok('PASS T2e service_role executes every callable function and none of the internal helpers or trigger functions',
  (SELECT bool_and(has_function_privilege('service_role', pr.oid, 'EXECUTE')
                   = NOT (pr.proname LIKE '\_credit\_%' OR pr.proname IN ('credit_ledger_immutable','credit_reparent_on_move',
                          'trainings_guard_credit_columns','trainings_credit_settle','trainings_credit_settle_delete',
                          'users_credit_close','workgroups_credit_close')))
          AND count(*) = 43
     FROM pg_proc pr WHERE pr.pronamespace = 'public'::regnamespace
      AND (pr.proname LIKE '\_credit\_%' OR pr.proname LIKE 'credit\_%'
           OR pr.proname IN ('start_training_safe','start_continuation_safe','stop_training','record_training_refusal',
                             'get_remaining_credits','get_teacher_credit_summary','adjust_student_credits','adjust_workgroup_credits',
                             'cancel_request_training','finish_training','complete_cancel_training','claim_training_publish',
                             'cap_training_attempt','requeue_training','trainings_guard_credit_columns','trainings_credit_settle',
                             'trainings_credit_settle_delete','users_credit_close','workgroups_credit_close'))));
SELECT pg_temp.ok('PASS T2f every callable credit function, the two starts, the compat functions and record_training_refusal are SECURITY DEFINER with search_path=public',
  (SELECT count(*) = 18 AND bool_and(prosecdef AND 'search_path=public' = ANY (proconfig))
     FROM pg_proc WHERE pronamespace = 'public'::regnamespace
      AND proname IN ('credit_admin_grant','credit_admin_revoke','credit_admin_compensate','credit_assign','credit_reclaim',
                      'credit_set_member_limit','credit_quota','credit_teacher_summary','credit_history','credit_erase_reason',
                      'credit_reconcile','start_training_safe','start_continuation_safe','get_remaining_credits',
                      'get_teacher_credit_summary','adjust_student_credits','adjust_workgroup_credits','record_training_refusal')));
SELECT pg_temp.ok('PASS T2g stop_training, the changed 049/050 functions and the credit-column guard are SECURITY INVOKER',
  (SELECT count(*) = 8 AND bool_and(NOT prosecdef) FROM pg_proc WHERE pronamespace = 'public'::regnamespace
      AND proname IN ('stop_training','cancel_request_training','finish_training','complete_cancel_training',
                      'claim_training_publish','cap_training_attempt','requeue_training','trainings_guard_credit_columns')));
SELECT pg_temp.ok('PASS T2h request roles read reserved_hours, charged_hours, stop_requested_by_role, lost_seconds and not worker_token, lost_reasons, charge_waiver, idempotency_key, settled_at',
  (SELECT bool_and(has_column_privilege(r, 'public.trainings', c, 'SELECT')
                   = (c IN ('reserved_hours','charged_hours','stop_requested_by_role','lost_seconds')))
     FROM unnest(ARRAY['anon','authenticated']) r,
          unnest(ARRAY['reserved_hours','charged_hours','stop_requested_by_role','lost_seconds','worker_token','lost_reasons',
                       'charge_waiver','idempotency_key','settled_at']) c));
SELECT pg_temp.ok('PASS T2i none of the four tables is in a publication',
  NOT EXISTS (SELECT 1 FROM pg_publication_tables WHERE schemaname = 'public'
               AND tablename IN ('credit_accounts','credit_ledger','credit_reasons','training_start_refusals')));
SELECT pg_temp.ok('PASS T2j exactly one overload each of start_training_safe, start_continuation_safe, finish_training, get_remaining_credits, cancel_request_training',
  (SELECT bool_and(n = 1) FROM (SELECT count(*) AS n FROM pg_proc WHERE pronamespace = 'public'::regnamespace
      AND proname IN ('start_training_safe','start_continuation_safe','finish_training','get_remaining_credits','cancel_request_training')
      GROUP BY proname) x)
  AND (SELECT count(DISTINCT proname) = 5 FROM pg_proc WHERE pronamespace = 'public'::regnamespace
      AND proname IN ('start_training_safe','start_continuation_safe','finish_training','get_remaining_credits','cancel_request_training')));
SELECT pg_temp.ok('PASS T2k the nine triggers exist with their timing (five BEFORE, four AFTER)',
  (SELECT count(*) = 9 FROM pg_trigger tg WHERE NOT tg.tgisinternal AND (
       (tg.tgname IN ('credit_ledger_no_change','credit_ledger_no_truncate','trg_0_trainings_credit_guard',
                      'trg_9_trainings_credit_settle','trg_9_trainings_credit_settle_delete') AND tg.tgtype & 2 = 2)
    OR (tg.tgname IN ('trg_9_users_credit_close','trg_9_workgroups_credit_close','trg_9_users_credit_reparent',
                      'trg_9_workgroups_credit_reparent') AND tg.tgtype & 2 = 0))));
SELECT pg_temp.ok('PASS T2l trainings.stop_requested_by, trainings.time_limit_s and training_attempts.host_key do not exist',
  NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND
              ((table_name = 'trainings' AND column_name IN ('stop_requested_by','time_limit_s'))
            OR (table_name = 'training_attempts' AND column_name = 'host_key'))));

\echo '=== T35 (review 2 m12) the service role may not TRUNCATE, TRIGGER or REFERENCE trainings; PUBLIC holds nothing ==='
SELECT pg_temp.ok('PASS T35a service_role holds no INSERT, TRUNCATE, TRIGGER or REFERENCES on trainings, still SELECT and UPDATE',
  NOT has_table_privilege('service_role', 'public.trainings', 'INSERT') AND NOT has_table_privilege('service_role', 'public.trainings', 'TRUNCATE')
  AND NOT has_table_privilege('service_role', 'public.trainings', 'TRIGGER') AND NOT has_table_privilege('service_role', 'public.trainings', 'REFERENCES')
  AND has_table_privilege('service_role', 'public.trainings', 'SELECT') AND has_table_privilege('service_role', 'public.trainings', 'UPDATE'));
SELECT pg_temp.ok('PASS T35b PUBLIC holds no privilege on the four tables and no EXECUTE on a credit function',
  NOT EXISTS (SELECT 1 FROM unnest(ARRAY['public.credit_accounts','public.credit_ledger','public.credit_reasons','public.training_start_refusals']) t,
                     unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) p
               WHERE has_table_privilege('public', t, p))
  AND NOT EXISTS (SELECT 1 FROM pg_proc pr WHERE pr.pronamespace = 'public'::regnamespace
                   AND (pr.proname LIKE 'credit\_%' OR pr.proname LIKE '\_credit\_%' OR pr.proname = 'stop_training')
                   AND has_function_privilege('public', pr.oid, 'EXECUTE')));

\echo '=== T37 [R3-2] training_checkpoints.config_complaints ==='
SELECT pg_temp.ok('PASS T37a training_checkpoints.config_complaints is INT[] NOT NULL DEFAULT {}',
  (SELECT data_type = 'ARRAY' AND udt_name = '_int4' AND is_nullable = 'NO' AND column_default = '''{}''::integer[]'
     FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'training_checkpoints' AND column_name = 'config_complaints'));
SELECT pg_temp.ok('PASS T37b request roles hold nothing on training_checkpoints; service_role may update config_complaints',
  NOT EXISTS (SELECT 1 FROM unnest(ARRAY['anon','authenticated']) r,
                     unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE']) p WHERE has_table_privilege(r, 'public.training_checkpoints', p))
  AND has_column_privilege('service_role', 'public.training_checkpoints', 'config_complaints', 'UPDATE'));

\echo '=== T3 admin grant/revoke and teacher assign: over-pool, grouped, foreign teacher ==='
SELECT pg_temp.ok('PASS T3a the admin grants f1 500 hours with a reason',
  public.credit_admin_grant(pg_temp.u('a0'), pg_temp.u('f1'), 500, 'Schuljahr 2026/27', NULL) = 500);
SELECT pg_temp.ok('PASS T3b the admin grants f2 50 hours without a reason',
  public.credit_admin_grant(pg_temp.u('a0'), pg_temp.u('f2'), 50, NULL, NULL) = 50);
SELECT pg_temp.err($q$SELECT public.credit_admin_grant(pg_temp.u('f1'), pg_temp.u('f2'), 1, NULL, NULL)$q$, 'P0067',
  'PASS T3c a grant by a teacher answers P0067');
SELECT pg_temp.err($q$SELECT public.credit_admin_grant(pg_temp.u('a0'), pg_temp.u('51'), 1, NULL, NULL)$q$, 'P0002',
  'PASS T3d a grant to a student answers P0002');
SELECT pg_temp.err($q$SELECT public.credit_admin_grant(pg_temp.u('a0'), pg_temp.u('f1'), 0, NULL, NULL)$q$, '22023',
  'PASS T3e a grant of 0 hours answers 22023');
SELECT pg_temp.err($q$SELECT public.credit_admin_grant(pg_temp.u('a0'), pg_temp.u('f1'), 1, repeat('x', 201), NULL)$q$, '22023',
  'PASS T3f a reason of 201 characters answers 22023');
SELECT pg_temp.ok('PASS T3g f1 assigns 4 hours to s51 (target 4, pool 496)',
  (SELECT o_target_free = 4 AND o_teacher_free = 496 FROM public.credit_assign(pg_temp.u('f1'), 'student', pg_temp.u('51'), 4, NULL)));
SELECT pg_temp.ok('PASS T3h f1 assigns 40 hours to s52 with a request id',
  (SELECT o_target_free = 40 AND o_teacher_free = 456 FROM public.credit_assign(pg_temp.u('f1'), 'student', pg_temp.u('52'), 40, pg_temp.u('e0'))));
SELECT pg_temp.ok('PASS T3i f1 assigns 20 hours to the group a1',
  (SELECT o_target_free = 20 AND o_teacher_free = 436 FROM public.credit_assign(pg_temp.u('f1'), 'group', pg_temp.u('a1'), 20, NULL)));
SELECT pg_temp.err($q$SELECT * FROM public.credit_assign(pg_temp.u('f1'), 'student', pg_temp.u('53'), 1, NULL)$q$, 'P0023',
  'PASS T3j assigning to a grouped student answers P0023');
SELECT pg_temp.err($q$SELECT * FROM public.credit_assign(pg_temp.u('f1'), 'student', pg_temp.u('55'), 1, NULL)$q$, 'P0066',
  'PASS T3k assigning to another teacher''s student answers P0066');
SELECT pg_temp.err($q$SELECT * FROM public.credit_assign(pg_temp.u('f1'), 'student', pg_temp.u('51'), 1000, NULL)$q$, 'P0065',
  'PASS T3l assigning more than the pool answers P0065');
SELECT pg_temp.err($q$SELECT * FROM public.credit_assign(pg_temp.u('f1'), 'teacher', pg_temp.u('f2'), 1, NULL)$q$, '22023',
  'PASS T3m assigning to a teacher answers 22023');
SELECT pg_temp.ok('PASS T3n f2 assigns 10 hours to its student s55',
  (SELECT o_target_free = 10 AND o_teacher_free = 40 FROM public.credit_assign(pg_temp.u('f2'), 'student', pg_temp.u('55'), 10, NULL)));
SELECT pg_temp.mk_user('f4', 'teacher', NULL) IS NOT NULL AS f4 \gset
SELECT pg_temp.mk_class('c4', 'f4') IS NOT NULL AS c4 \gset
SELECT pg_temp.mk_user('57', 'student', 'c4') IS NOT NULL AS s57 \gset
SELECT pg_temp.err($q$SELECT * FROM public.credit_assign(pg_temp.u('f4'), 'student', pg_temp.u('57'), 1, NULL)$q$, 'P0065',
  'PASS T3o a teacher created after 052 (no account yet) assigning answers P0065');
DO $$ BEGIN
  PERFORM public.credit_admin_revoke(pg_temp.u('a0'), pg_temp.u('f2'), 41, NULL, NULL);
  RAISE NOTICE 'FAIL T3p (no error)';
EXCEPTION WHEN SQLSTATE 'P0062' THEN
  IF SQLERRM LIKE '%40%' THEN RAISE NOTICE 'PASS T3p a revoke above the teacher''s free hours answers P0062 naming them (%)', SQLERRM;
  ELSE RAISE NOTICE 'FAIL T3p (message %)', SQLERRM; END IF;
END $$;
SELECT public.credit_admin_revoke(pg_temp.u('a0'), pg_temp.u('f2'), 5, 'Korrektur', NULL) AS rv_f2 \gset
SELECT pg_temp.ok('PASS T3q the admin revokes 5 of f2''s hours with a reason (free 35, the reason stored erasably)',
  :rv_f2 = 35
  AND (SELECT r.text = 'Korrektur' AND r.subject_kind = 'teacher' AND r.subject_id = pg_temp.u('f2')
         FROM public.credit_ledger l JOIN public.credit_reasons r ON r.id = l.reason_id
        WHERE l.kind = 'revoke' AND l.from_id = pg_temp.u('f2')));
SELECT pg_temp.ok('PASS T3r I10: every hour was created by an admin row', pg_temp.identity_ok(), pg_temp.identity());

\echo '=== T25a-b [D5] a member limit, one limit_change row per real change ==='
SELECT public.credit_set_member_limit(pg_temp.u('f1'), pg_temp.u('a1'), 2) AS lim_a1 \gset
SELECT pg_temp.ok('PASS T25a f1 sets a1''s member limit to 2: one limit_change row {old: null, new: 2}',
  :lim_a1 = 2
  AND (SELECT count(*) = 1 AND bool_and(meta = '{"old": null, "new": 2}'::jsonb AND hours = 0 AND actor_role = 'teacher'
                                        AND actor_id = pg_temp.u('f1'))
         FROM public.credit_ledger WHERE kind = 'limit_change' AND to_id = pg_temp.u('a1')));
SELECT public.credit_set_member_limit(pg_temp.u('f1'), pg_temp.u('a1'), 2) AS lim_a1b \gset
SELECT pg_temp.ok('PASS T25b the same value again writes nothing',
  :lim_a1b = 2
  AND (SELECT count(*) = 1 FROM public.credit_ledger WHERE kind = 'limit_change' AND to_id = pg_temp.u('a1')));

\echo '=== T4 the start reservation: 3 then 1, replay, P0003, P0060, P0061, P0071, P0063 ==='
SELECT pg_temp.ok('PASS T4a s51 (4 free) starts with a key: 3 hours reserved, 1 left, no replay',
  (SELECT reserved_hours = 3 AND remaining = 1 AND NOT replay
     FROM public.start_training_safe(pg_temp.u('51'), 'u/a', 'c052/m-a', 'act', '{"steps":50000,"save_freq":2500}'::jsonb,
                                     50000, NULL, true, pg_temp.u('e1'), 3)));
SELECT (replay AND reserved_hours = 3 AND remaining IS NULL AND training_id = pg_temp.tid('c052/m-a')) AS rep4b
  FROM public.start_training_safe(pg_temp.u('51'), 'u/a', 'c052/m-a2', 'ACT', '{"steps":50000}'::jsonb,
                                  50000, NULL, true, pg_temp.u('e1'), 3) \gset
SELECT pg_temp.ok('PASS T4b the same key again (ACT spelled differently) replays the same row and creates none',
  :'rep4b'::boolean AND pg_temp.tid('c052/m-a2') IS NULL
  AND (SELECT count(*) = 1 FROM public.credit_ledger WHERE request_id = pg_temp.u('e1')));
SELECT pg_temp.err($q$SELECT * FROM public.start_training_safe(pg_temp.u('51'), 'u/OTHER', 'm', 'act', '{"steps":50000}'::jsonb,
  50000, NULL, true, pg_temp.u('e1'), 3)$q$, 'P0071', 'PASS T4c the same key with another dataset answers P0071');
SELECT pg_temp.err($q$SELECT * FROM public.start_training_safe(pg_temp.u('51'), 'u/a', 'm', 'act', '{"steps":60000}'::jsonb,
  60000, NULL, true, pg_temp.u('e1'), 3)$q$, 'P0071', 'PASS T4d the same key with other steps answers P0071');
SELECT pg_temp.ok('PASS T4e s51''s second start reserves the 1 hour left',
  (SELECT reserved_hours = 1 AND remaining = 0 AND NOT replay
     FROM public.start_training_safe(pg_temp.u('51'), 'u/b', 'c052/m-b', 'act', '{"steps":50000}'::jsonb, 50000, NULL, true)));
SELECT pg_temp.err($q$SELECT * FROM public.start_training_safe(pg_temp.u('51'), 'u/c', 'm', 'act', '{}'::jsonb, 1, NULL, true)$q$,
  'P0003', 'PASS T4f a third start with 0 free hours answers P0003');
SELECT pg_temp.err($q$SELECT * FROM public.start_training_safe(pg_temp.u('f1'), 'u/c', 'm', 'act', '{}'::jsonb, 1, NULL, true)$q$,
  'P0060', 'PASS T4g a teacher''s start answers P0060');
SELECT pg_temp.err($q$SELECT * FROM public.start_training_safe(pg_temp.u('a0'), 'u/c', 'm', 'act', '{}'::jsonb, 1, NULL, true)$q$,
  'P0060', 'PASS T4h an admin''s start answers P0060');
SELECT pg_temp.err($q$SELECT * FROM public.start_training_safe(pg_temp.u('52'), 'u/c', 'm', 'pi0', '{}'::jsonb, 1, NULL, true)$q$,
  'P0061', 'PASS T4i a pi0 start answers P0061');
SELECT pg_temp.err($q$SELECT * FROM public.start_training_safe(pg_temp.u('52'), 'u/c', 'm', 'act', '{}'::jsonb, 1, gen_random_uuid(), false)$q$,
  'P0061', 'PASS T4j an unmanaged start (the old API''s eight arguments, p_managed false) answers P0061');
SELECT pg_temp.err($q$SELECT * FROM public.start_training_safe(pg_temp.u('52'), 'u/c', 'm', 'act', '{}'::jsonb, 1, NULL, true, pg_temp.u('e0'), 3)$q$,
  'P0071', 'PASS T4k a student using a teacher''s assign request id as start key answers P0071');
SELECT pg_temp.err($q$SELECT * FROM public.start_training_safe(pg_temp.u('52'), 'u/c', 'm', NULL, '{}'::jsonb, 1, NULL, true)$q$,
  'P0061', 'PASS T4l a start without a model type answers P0061');
SELECT pg_temp.ok('PASS T4m s51''s account reads 0 free / 4 reserved / 0 spent; both reserve rows name s51 as member and actor',
  pg_temp.acct('student', pg_temp.u('51')) = '0/4/0'
  AND (SELECT count(*) = 2 AND bool_and(member_id = pg_temp.u('51') AND actor_id = pg_temp.u('51') AND actor_role = 'student')
         FROM public.credit_ledger WHERE kind = 'reserve' AND from_id = pg_temp.u('51'))
  AND (SELECT request_id = pg_temp.u('e1') FROM public.credit_ledger WHERE kind = 'reserve' AND training_id = pg_temp.tid('c052/m-a')),
  pg_temp.acct('student', pg_temp.u('51')));
SELECT pg_temp.ok('PASS T4n p_max_hours 1 reserves 1 hour (s52: 39 free, 1 reserved)',
  (SELECT reserved_hours = 1 AND remaining = 39
     FROM public.start_training_safe(pg_temp.u('52'), 'u/n', 'c052/m-n', 'act', '{"steps":50000}'::jsonb, 50000, NULL, true, NULL, 1)));
SELECT pg_temp.ok('PASS T4o the quota of an unknown user is no row',
  NOT EXISTS (SELECT 1 FROM public.credit_quota(gen_random_uuid(), 3)));
SELECT reserved_hours AS r53 FROM public.start_training_safe(pg_temp.u('53'), 'u/g', 'c052/m-g53', 'act', '{"steps":50000}'::jsonb,
                                                             50000, NULL, true) \gset
SELECT pg_temp.ok('PASS T4p s53 (group a1, member limit 2) starts: 2 hours reserved from the group',
  :r53 = 2
  AND (SELECT from_kind = 'group' AND from_id = pg_temp.u('a1') AND member_id = pg_temp.u('53')
         FROM public.credit_ledger WHERE kind = 'reserve' AND training_id = pg_temp.tid('c052/m-g53')));
SELECT pg_temp.err($q$SELECT * FROM public.start_training_safe(pg_temp.u('53'), 'u/g', 'm', 'act', '{}'::jsonb, 1, NULL, true)$q$,
  'P0063', 'PASS T4q s53''s next start answers P0063 (member limit reached, the group has hours)');
SELECT pg_temp.ok('PASS T4r s54 still starts under its own member limit (2 hours)',
  (SELECT reserved_hours = 2 FROM public.start_training_safe(pg_temp.u('54'), 'u/g', 'c052/m-g54', 'act', '{"steps":50000}'::jsonb,
                                                             50000, NULL, true)));
SELECT pg_temp.ok('PASS T4s s53''s quota: group a1, 16 free, 4 reserved, limit 2, used 2, next 0',
  (SELECT row(q.*)::text FROM public.credit_quota(pg_temp.u('53'), 3) q)
  = '(student,group,' || pg_temp.u('a1') || ',16,4,0,2,2,0)',
  (SELECT row(q.*)::text FROM public.credit_quota(pg_temp.u('53'), 3) q));
SELECT pg_temp.ok('PASS T4t s52''s quota: its personal account, member_used NULL, next 3; next 1 with p_max_hours 1',
  (SELECT o_account_kind = 'student' AND o_member_used IS NULL AND o_member_limit IS NULL AND o_next_hours = 3
     FROM public.credit_quota(pg_temp.u('52'), 3))
  AND (SELECT o_next_hours = 1 FROM public.credit_quota(pg_temp.u('52'), 1)));

\echo '=== T5 settlement: 2 of 3 hours at 3 700 s, exactly once across a second terminal transition ==='
SELECT pg_temp.run('52', 'c052/t5', 3700) AS t5 \gset
SELECT public.finish_training(:t5, 1, 'succeeded', NULL, NULL);
SELECT pg_temp.ok('PASS T5a finish_training after 3 700 s charges 2 of 3 and releases 1',
  pg_temp.led(:t5) = 'reserve:3,charge:2,release:1', pg_temp.led(:t5));
SELECT pg_temp.ok('PASS T5b the row carries charged_hours 2, settled_at and the counted seconds',
  (SELECT charged_hours = 2 AND settled_at IS NOT NULL AND train_seconds_used BETWEEN 3700 AND 3710 FROM public.trainings WHERE id = :t5),
  (SELECT charged_hours || '/' || train_seconds_used FROM public.trainings WHERE id = :t5));
UPDATE public.trainings SET status = 'running' WHERE id = :t5;
UPDATE public.trainings SET status = 'failed' WHERE id = :t5;
SELECT pg_temp.ok('PASS T5c a second terminal transition settles nothing again',
  pg_temp.led(:t5) = 'reserve:3,charge:2,release:1' AND (SELECT charged_hours = 2 FROM public.trainings WHERE id = :t5),
  pg_temp.led(:t5));

\echo '=== T6 no attempt ever had a first running write: charge 0 ==='
SELECT training_id AS t6 FROM public.start_training_safe(pg_temp.u('52'), 'u/x', 'c052/t6', 'act', '{"steps":50000}'::jsonb, 50000, NULL, true) \gset
SELECT o_managed FROM public.cancel_request_training(:t6, pg_temp.u('52'), 'student') \gset
SELECT public.complete_cancel_training(:t6, 'canceled', NULL);
SELECT pg_temp.ok('PASS T6 a training canceled before any heartbeat charges 0 and releases 3',
  pg_temp.led(:t6) = 'reserve:3,charge:0,release:3' AND (SELECT charged_hours = 0 FROM public.trainings WHERE id = :t6), pg_temp.led(:t6));

\echo '=== T7 the charge is clamped to the reservation; the rounding table [D10] ==='
SELECT pg_temp.run('52', 'c052/t7', 12000) AS t7 \gset
SELECT public.finish_training(:t7, 1, 'failed', 'x', NULL);
SELECT pg_temp.ok('PASS T7a 12 000 s on a 3-hour reservation charges 3', pg_temp.led(:t7) = 'reserve:3,charge:3,release:0', pg_temp.led(:t7));
DO $$
DECLARE s int; t int; c int; res text := '';
BEGIN
  FOREACH s IN ARRAY ARRAY[0, 1, 3599, 3600, 3601, 7200, 7201, 10800] LOOP
    t := pg_temp.run('52', 'c052/round-' || s, 0);
    UPDATE public.training_attempts SET state = 'ended', end_reason = 'test' WHERE training_id = t;  -- only the stored seconds count
    UPDATE public.trainings SET train_seconds_used = s WHERE id = t;
    UPDATE public.trainings SET status = 'failed' WHERE id = t;
    SELECT charged_hours INTO c FROM public.trainings WHERE id = t;
    res := res || s || '->' || c || ' ';
  END LOOP;
  IF res = '0->0 1->1 3599->1 3600->1 3601->2 7200->2 7201->3 10800->3 ' THEN
    RAISE NOTICE 'PASS T7b charge = ceil(seconds / 3600) with a heartbeat, 0 possible: %', res;
  ELSE RAISE NOTICE 'FAIL T7b [%]', res; END IF;
END $$;

\echo '=== T8 reclaim: only free hours, judged by the funding teacher ==='
SELECT pg_temp.err($q$SELECT * FROM public.credit_reclaim(pg_temp.u('f1'), 'student', pg_temp.u('52'),
  (SELECT free + 1 FROM public.credit_accounts WHERE kind = 'student' AND owner_id = pg_temp.u('52')), NULL)$q$, 'P0064',
  'PASS T8a reclaiming more than the free hours answers P0064');
SELECT free AS s52_free FROM public.credit_accounts WHERE kind = 'student' AND owner_id = pg_temp.u('52') \gset
SELECT free AS f1_free FROM public.credit_accounts WHERE kind = 'teacher' AND owner_id = pg_temp.u('f1') \gset
SELECT pg_temp.ok('PASS T8b f1 reclaims 2 free hours of s52',
  (SELECT o_target_free = :s52_free - 2 AND o_teacher_free = :f1_free + 2
     FROM public.credit_reclaim(pg_temp.u('f1'), 'student', pg_temp.u('52'), 2, NULL)));
SELECT pg_temp.err($q$SELECT * FROM public.credit_reclaim(pg_temp.u('f2'), 'student', pg_temp.u('52'), 1, NULL)$q$, 'P0066',
  'PASS T8c another teacher''s reclaim answers P0066');
SELECT pg_temp.err($q$SELECT * FROM public.credit_reclaim(pg_temp.u('f1'), 'student', pg_temp.u('51'), 1, NULL)$q$, 'P0064',
  'PASS T8d reserved hours are never reclaimable (s51: 0 free, 4 reserved) — P0064');
SELECT pg_temp.err($q$SELECT * FROM public.credit_reclaim(pg_temp.u('f3'), 'student', pg_temp.u('52'), 1, NULL)$q$, 'P0066',
  'PASS T8e a teacher who does not fund the account answers P0066');

\echo '=== T13 the CHECK free >= 0 stops even a buggy direct _credit_post ==='
SELECT count(*) AS n13 FROM public.credit_ledger \gset
SELECT pg_temp.err($q$SELECT public._credit_post('reclaim', 999, 'student', pg_temp.u('52'), 'free', 'teacher', pg_temp.u('f1'), 'free',
  NULL, NULL, NULL, 'system', NULL, NULL, NULL)$q$, '23514', 'PASS T13a an overdraft of free hours violates the CHECK (23514)');
SELECT pg_temp.err($q$SELECT public._credit_post('release', 99, 'student', pg_temp.u('52'), 'reserved', 'student', pg_temp.u('52'), 'free',
  999999, pg_temp.u('52'), NULL, 'system', NULL, NULL, NULL)$q$, '23514', 'PASS T13b an overdraft of reserved hours violates the CHECK (23514)');
SELECT pg_temp.ok('PASS T13c neither attempt wrote a ledger row', (SELECT count(*) FROM public.credit_ledger) = :n13);

\echo '=== T14 the credit-column guard (SET LOCAL ROLE service_role + plain table statements) ==='
SELECT pg_temp.tid('c052/m-n') AS t4n \gset
SELECT set_config('c052.t4n', :'t4n', false), set_config('c052.t5', :'t5', false) \gset
DO $$ BEGIN SET LOCAL ROLE service_role;
  UPDATE public.trainings SET reserved_hours = 3 WHERE id = current_setting('c052.t4n')::int;
  RAISE NOTICE 'FAIL T14a (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T14a service_role cannot change reserved_hours (42501)'; END $$;
DO $$ BEGIN SET LOCAL ROLE service_role;
  UPDATE public.trainings SET credit_scheme = 'derived' WHERE id = current_setting('c052.t4n')::int;
  RAISE NOTICE 'FAIL T14b (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T14b service_role cannot change credit_scheme (42501)'; END $$;
DO $$ BEGIN SET LOCAL ROLE service_role;
  UPDATE public.trainings SET charged_hours = 0 WHERE id = current_setting('c052.t5')::int;
  RAISE NOTICE 'FAIL T14c (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T14c service_role cannot change charged_hours (42501)'; END $$;
DO $$ BEGIN SET LOCAL ROLE service_role;
  UPDATE public.trainings SET settled_at = NULL WHERE id = current_setting('c052.t5')::int;
  RAISE NOTICE 'FAIL T14d (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T14d service_role cannot change settled_at (42501)'; END $$;
DO $$ BEGIN SET LOCAL ROLE service_role;
  UPDATE public.trainings SET idempotency_key = gen_random_uuid() WHERE id = current_setting('c052.t4n')::int;
  RAISE NOTICE 'FAIL T14e (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T14e service_role cannot change idempotency_key (42501)'; END $$;
DO $$ BEGIN SET LOCAL ROLE service_role;
  UPDATE public.trainings SET user_id = 'c0520000-0000-0000-0000-000000000051' WHERE id = current_setting('c052.t4n')::int;
  RAISE NOTICE 'FAIL T14f (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T14f service_role cannot change user_id (42501)'; END $$;
DO $$ BEGIN SET LOCAL ROLE service_role;
  UPDATE public.trainings SET current_attempt = current_attempt, lost_seconds = lost_seconds, status_note = status_note
   WHERE id = current_setting('c052.t4n')::int;
  RAISE NOTICE 'PASS T14g service_role may still write the other columns (current_attempt, lost_seconds, status_note)';
EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'FAIL T14g (% %)', SQLSTATE, SQLERRM; END $$;
DO $$ BEGIN SET LOCAL ROLE service_role;
  INSERT INTO public.trainings (user_id, status, dataset_name, model_name, model_type, credit_scheme, reserved_hours)
  VALUES ('c0520000-0000-0000-0000-000000000052', 'queued', 'd', 'c052/forged', 'act', 'ledger', 3);
  RAISE NOTICE 'FAIL T14h (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T14h service_role cannot INSERT a trainings row (42501, I6)'; END $$;
DO $$ BEGIN SET LOCAL ROLE service_role;
  INSERT INTO public.credit_ledger (kind, hours, to_kind, to_id, to_bucket, actor_role)
  VALUES ('migrate', 0, 'student', 'c0520000-0000-0000-0000-000000000052', 'free', 'system');
  RAISE NOTICE 'FAIL T14i (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T14i service_role cannot INSERT a ledger row (42501, I2)'; END $$;
DO $$ BEGIN SET LOCAL ROLE service_role;
  UPDATE public.credit_accounts SET free = free + 99;
  RAISE NOTICE 'FAIL T14j (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T14j service_role cannot UPDATE an account (42501, I2)'; END $$;
DO $$ BEGIN SET LOCAL ROLE service_role;
  DELETE FROM public.credit_reasons;
  RAISE NOTICE 'FAIL T14k (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T14k service_role cannot DELETE a reason directly (42501, I2)'; END $$;
DO $$ BEGIN SET LOCAL ROLE service_role;
  TRUNCATE public.trainings;
  RAISE NOTICE 'FAIL T14l (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T14l service_role cannot TRUNCATE trainings (42501, review 2 m12)';
          WHEN OTHERS THEN RAISE NOTICE 'FAIL T14l (% %)', SQLSTATE, SQLERRM; END $$;
DO $$ BEGIN SET LOCAL ROLE authenticated;
  PERFORM 1 FROM public.credit_ledger LIMIT 1;
  RAISE NOTICE 'FAIL T14m (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T14m authenticated cannot read the ledger (42501)'; END $$;
DO $$ BEGIN SET LOCAL ROLE anon;
  PERFORM 1 FROM public.credit_accounts LIMIT 1;
  RAISE NOTICE 'FAIL T14n (no error)';
EXCEPTION WHEN SQLSTATE '42501' THEN RAISE NOTICE 'PASS T14n anon cannot read the accounts (42501)'; END $$;

\echo '=== T15 settlement of a service-role terminal write (m2) and finish_training (no double count) ==='
SELECT pg_temp.run('52', 'c052/t15', 6000) AS t15 \gset
SELECT set_config('c052.t15', :'t15', false) \gset
DO $$ BEGIN SET LOCAL ROLE service_role;
  UPDATE public.trainings SET status = 'failed' WHERE id = current_setting('c052.t15')::int;
END $$;
SELECT pg_temp.ok('PASS T15a a direct service-role status update on a 100-minute attempt with train_seconds_used 0 charges 2 (6 000 s)',
  (SELECT charged_hours = 2 AND train_seconds_used BETWEEN 6000 AND 6010 FROM public.trainings WHERE id = :t15),
  (SELECT charged_hours || '/' || train_seconds_used FROM public.trainings WHERE id = :t15));
SELECT pg_temp.run('52', 'c052/t15b', 4200) AS t15b \gset
SELECT public.finish_training(:t15b, 1, 'succeeded', NULL, NULL);
SELECT pg_temp.ok('PASS T15b finish_training on a 70-minute attempt counts 4 200 s once (not 8 400) and charges 2',
  (SELECT charged_hours = 2 AND train_seconds_used BETWEEN 4200 AND 4210 FROM public.trainings WHERE id = :t15b),
  (SELECT charged_hours || '/' || train_seconds_used FROM public.trainings WHERE id = :t15b));

\echo '=== T16 stop_training: publish / publish_final / cancel / P0051 / derived -> cancel ==='
SELECT pg_temp.mk_user('63', 'student', 'c1') IS NOT NULL AS s63 \gset
SELECT pg_temp.give('f1', 'student', '63', 60) AS g63 \gset
SELECT pg_temp.run('63', 'c052/s1', 4200) AS a \gset
SELECT pg_temp.ckpt(:a, 1, 7500, 600);
SELECT pg_temp.ok('PASS T16a a trained training with a ready checkpoint at 7 500: outcome publish at step 7 500',
  (SELECT o_outcome = 'publish' AND o_step = 7500 AND o_attempt = 1 AND o_attempt_state = 'running' AND o_backend = 'runpod_secure'
     FROM public.stop_training(:a, pg_temp.u('63'), pg_temp.u('f1'), 'teacher', 'S32')));
SELECT pg_temp.ok('PASS T16b the row runs on for the publish: capped 7 500, the attempt''s seconds added, fenced, stopped by the teacher',
  (SELECT t.status = 'running' AND t.capped_step = 7500 AND t.train_seconds_used BETWEEN 4200 AND 4210 AND t.worker_token IS NULL
          AND t.stop_requested_by_role = 'teacher' AND t.stop_requested_at IS NOT NULL AND t.status_note = 'S32'
          AND a.state = 'ended' AND a.end_reason = 'stopped'
     FROM public.trainings t JOIN public.training_attempts a ON a.training_id = t.id AND a.attempt_no = 1 WHERE t.id = :a),
  (SELECT status || '/' || capped_step || '/' || train_seconds_used || '/' || stop_requested_by_role FROM public.trainings WHERE id = :a));
SELECT pg_temp.err(format($q$SELECT * FROM public.stop_training(%s, pg_temp.u('63'), pg_temp.u('63'), 'student', NULL)$q$, :a),
  'P0051', 'PASS T16c a second stop while publishing answers P0051');
SELECT pg_temp.err(format($q$SELECT * FROM public.cancel_request_training(%s, pg_temp.u('63'))$q$, :a),
  'P0051', 'PASS T16d a cancel request while publishing answers P0051');
SELECT pg_temp.ok('PASS T16e claim_training_publish accepts a stopped attempt', public.claim_training_publish(:a, 1, 600, 'c052'));
SELECT public.finish_training(:a, 1, 'succeeded', NULL, NULL, 'S31', 7500, now() + interval '7 days');
SELECT pg_temp.ok('PASS T16f the publish''s terminal update charges 2 of 3 (4 200 s) and the row is continuable',
  pg_temp.led(:a) = 'reserve:3,charge:2,release:1'
  AND (SELECT continuable_until > now() AND current_step = 7500 FROM public.trainings WHERE id = :a), pg_temp.led(:a));
SELECT pg_temp.run('63', 'c052/s2', 1000) AS b \gset
SELECT pg_temp.ckpt(:b, 1, 50000, 0);
SELECT pg_temp.ok('PASS T16g a ready final checkpoint: outcome publish_final',
  (SELECT o_outcome = 'publish_final' AND o_step = 50000 FROM public.stop_training(:b, pg_temp.u('63'), pg_temp.u('63'), 'student', 'S30')));
SELECT pg_temp.ok('PASS T16h publish_final ends the attempt succeeded and keeps capped_step NULL',
  (SELECT a.state = 'ended' AND a.end_reason = 'succeeded' AND t.capped_step IS NULL AND t.status = 'running'
     FROM public.trainings t JOIN public.training_attempts a ON a.training_id = t.id WHERE t.id = :b));
SELECT pg_temp.run('63', 'c052/s3', 3000) AS c \gset
SELECT (o_outcome = 'cancel' AND o_step IS NULL AND o_cancel_attempts = 1) AS c_out
  FROM public.stop_training(:c, pg_temp.u('63'), pg_temp.u('63'), 'student', 'S30') \gset
SELECT pg_temp.ok('PASS T16i trained but no ready checkpoint: outcome cancel, the row cancel_requested',
  :'c_out'::boolean AND (SELECT status = 'cancel_requested' FROM public.trainings WHERE id = :c));
SELECT training_id AS d FROM public.start_training_safe(pg_temp.u('63'), 'u/s4', 'c052/s4', 'act', '{"steps":50000}'::jsonb, 50000, NULL, true) \gset
SELECT pg_temp.ok('PASS T16j never trained: outcome cancel',
  (SELECT o_outcome = 'cancel' AND o_attempt = 0 FROM public.stop_training(:d, pg_temp.u('63'), pg_temp.u('63'), 'student', NULL)));
SELECT public.complete_cancel_training(:d, 'canceled', NULL);
SELECT pg_temp.ok('PASS T16k its completion charges 0', pg_temp.led(:d) = 'reserve:3,charge:0,release:3', pg_temp.led(:d));
SELECT pg_temp.ok('PASS T16l a derived pre-052 managed row always takes the cancel branch (old rules)',
  (SELECT o_outcome = 'cancel' AND o_managed FROM public.stop_training(pg_temp.tid('c052/old-run'), pg_temp.u('51'), pg_temp.u('51'), 'student', NULL)));
SELECT public.complete_cancel_training(pg_temp.tid('c052/old-run'), 'canceled', NULL);
SELECT pg_temp.ok('PASS T16m the derived row ends with no ledger row and no charged_hours',
  pg_temp.led(pg_temp.tid('c052/old-run')) = ''
  AND (SELECT status = 'canceled' AND charged_hours IS NULL AND settled_at IS NULL FROM public.trainings WHERE model_name = 'c052/old-run'));
SELECT pg_temp.err(format($q$SELECT * FROM public.stop_training(%s, pg_temp.u('63'), NULL, 'admin', NULL)$q$, :c),
  '22023', 'PASS T16n an actor role other than student/teacher answers 22023');
SELECT pg_temp.err(format($q$SELECT * FROM public.stop_training(%s, pg_temp.u('51'), NULL, 'student', NULL)$q$, :a),
  'P0002', 'PASS T16o another user''s training answers P0002');
SELECT pg_temp.err(format($q$SELECT * FROM public.stop_training(%s, pg_temp.u('63'), NULL, 'student', NULL)$q$, :d),
  'P0050', 'PASS T16p a terminal training answers P0050');
SELECT pg_temp.run('63', 'c052/s5', 2700) AS e \gset
SELECT pg_temp.ckpt(:e, 1, 5000, 300);
SELECT public.requeue_training(:e, 1, 'pod_gone', 'S1', 5000, 2700);
SELECT pg_temp.attempt(:e, NULL) AS e_att \gset
SELECT pg_temp.ok('PASS T16q stopped while the next attempt is starting after a lost one with its own checkpoint: publish',
  (SELECT o_outcome = 'publish' AND o_step = 5000 AND o_attempt = 2 FROM public.stop_training(:e, pg_temp.u('63'), pg_temp.u('63'), 'student', 'S30')));
SELECT pg_temp.ok('PASS T16r its seconds are the lost attempt''s E50 part only (2 400 s), the starting attempt adds none',
  (SELECT train_seconds_used BETWEEN 2400 AND 2402 FROM public.trainings WHERE id = :e),
  (SELECT train_seconds_used || '/' || lost_seconds FROM public.trainings WHERE id = :e));

\echo '=== T17 complete_cancel_training counts only to the first stop request ==='
UPDATE public.trainings SET stop_requested_at = now() - interval '2500 seconds' WHERE id = :c;
SELECT public.complete_cancel_training(:c, 'canceled', NULL);
SELECT pg_temp.ok('PASS T17 a cancel completed after GPU-stop retries counts 500 s (to the first request) and charges 1',
  (SELECT charged_hours = 1 AND train_seconds_used BETWEEN 500 AND 502 AND status = 'canceled' FROM public.trainings WHERE id = :c),
  (SELECT charged_hours || '/' || train_seconds_used FROM public.trainings WHERE id = :c));

\echo '=== T34 (review 2 m9) the first stop request writes time and role together; later ones change neither ==='
SELECT pg_temp.run('63', 'c052/m9a', 1200) AS w \gset
SELECT o_managed FROM public.cancel_request_training(:w, pg_temp.u('63'), 'student') \gset
SELECT stop_requested_at AS w_t0 FROM public.trainings WHERE id = :w \gset
SELECT pg_temp.ok('PASS T34a cancel_request_training with a role writes the role and the time',
  (SELECT stop_requested_by_role = 'student' AND stop_requested_at IS NOT NULL FROM public.trainings WHERE id = :w));
SELECT o_managed FROM public.cancel_request_training(:w, pg_temp.u('63')) \gset
SELECT pg_temp.ok('PASS T34b a retry without a role keeps both',
  (SELECT stop_requested_by_role = 'student' AND stop_requested_at = :'w_t0'::timestamptz AND cancel_attempts = 2
     FROM public.trainings WHERE id = :w));
SELECT pg_temp.run('63', 'c052/m9b', 1200) AS x \gset
SELECT o_managed FROM public.cancel_request_training(:x, pg_temp.u('63')) \gset
SELECT o_outcome FROM public.stop_training(:x, pg_temp.u('63'), pg_temp.u('f1'), 'teacher', 'S32') \gset
SELECT pg_temp.ok('PASS T34c a request without a role followed by a teacher''s stop leaves the role NULL',
  (SELECT stop_requested_by_role IS NULL AND stop_requested_at IS NOT NULL FROM public.trainings WHERE id = :x),
  (SELECT COALESCE(stop_requested_by_role, 'NULL') FROM public.trainings WHERE id = :x));
SELECT pg_temp.err(format($q$SELECT * FROM public.cancel_request_training(%s, pg_temp.u('63'), 'admin')$q$, :x),
  '22023', 'PASS T34d cancel_request_training with role admin answers 22023');
SELECT training_id AS y FROM public.start_training_safe(pg_temp.u('63'), 'u/m9c', 'c052/m9c', 'act', '{"steps":50000}'::jsonb, 50000, NULL, true) \gset
SELECT o_outcome FROM public.stop_training(:y, pg_temp.u('63'), pg_temp.u('63'), 'student', 'S30') \gset
SELECT stop_requested_at AS y_t0 FROM public.trainings WHERE id = :y \gset
SELECT o_outcome FROM public.stop_training(:y, pg_temp.u('63'), pg_temp.u('f1'), 'teacher', 'S32') \gset
SELECT pg_temp.ok('PASS T34e a teacher''s later stop changes neither the student''s role nor the time',
  (SELECT stop_requested_by_role = 'student' AND stop_requested_at = :'y_t0'::timestamptz AND cancel_attempts = 2
     FROM public.trainings WHERE id = :y));
SELECT public.complete_cancel_training(:w, 'canceled', NULL);
SELECT public.complete_cancel_training(:x, 'canceled', NULL);
SELECT public.complete_cancel_training(:y, 'canceled', NULL);

\echo '=== T23 [D1, N2, Q-B] requeue_training: E50 charged, the rest to lost_seconds up to the last real evidence ==='
SELECT pg_temp.mk_user('64', 'student', 'c1') IS NOT NULL AS s64 \gset
SELECT pg_temp.give('f1', 'student', '64', 60) AS g64 \gset
SELECT pg_temp.run('64', 'c052/rq', 3600) AS rq \gset
UPDATE public.trainings SET step_advanced_at = now() - interval '40 min', last_progress_at = now() - interval '1 min' WHERE id = :rq;
SELECT pg_temp.ckpt(:rq, 1, 2500, 2700);
SELECT public.requeue_training(:rq, 1, 'stalled', NULL, 2500, 3600);
SELECT pg_temp.ok('PASS T23a stalled (60 min, own checkpoint at 15 min, last step advance 40 min ago): 900 s charged, 300 s lost',
  (SELECT train_seconds_used BETWEEN 900 AND 901 AND lost_seconds BETWEEN 299 AND 301 AND lost_reasons = '{stalled}' AND status = 'queued' AND current_step = 2500
     FROM public.trainings WHERE id = :rq),
  (SELECT train_seconds_used || '/' || lost_seconds FROM public.trainings WHERE id = :rq));
SELECT pg_temp.attempt(:rq, 3000) AS rq2 \gset
UPDATE public.trainings SET last_progress_at = now() - interval '30 min' WHERE id = :rq;
SELECT public.requeue_training(:rq, 2, 'heartbeat_lost', NULL, 2500, 3000);
SELECT pg_temp.ok('PASS T23b heartbeat_lost (50 min, no own checkpoint, last progress 30 min ago): 0 charged, 1 200 s lost',
  (SELECT train_seconds_used BETWEEN 900 AND 901 AND lost_seconds BETWEEN 1499 AND 1502 FROM public.trainings WHERE id = :rq),
  (SELECT train_seconds_used || '/' || lost_seconds FROM public.trainings WHERE id = :rq));
SELECT pg_temp.attempt(:rq, 600) AS rq3 \gset
SELECT public.requeue_training(:rq, 3, 'host_unfit:lerobot_crash', NULL, 2500, 600);
SELECT pg_temp.ok('PASS T23c a host report (10 min) ends at now(): 600 s lost; the reasons are kept in order',
  (SELECT train_seconds_used BETWEEN 900 AND 901 AND lost_seconds BETWEEN 2099 AND 2104
          AND lost_reasons = '{stalled,heartbeat_lost,host_unfit:lerobot_crash}' FROM public.trainings WHERE id = :rq),
  (SELECT train_seconds_used || '/' || lost_seconds || '/' || array_to_string(lost_reasons, ',') FROM public.trainings WHERE id = :rq));
SELECT pg_temp.attempt(:rq, 1200) AS rq4 \gset
UPDATE public.trainings SET last_progress_at = now() - interval '5 min' WHERE id = :rq;
SELECT public.requeue_training(:rq, 4, 'pod_gone', NULL, 2500, 1200);
SELECT pg_temp.ok('PASS T23d pod_gone ends at last_progress_at (20 min attempt, last progress 5 min ago): 900 s lost',
  (SELECT lost_seconds BETWEEN 2999 AND 3006 FROM public.trainings WHERE id = :rq),
  (SELECT lost_seconds::text FROM public.trainings WHERE id = :rq));
SELECT pg_temp.attempt(:rq, 1800) AS rq5 \gset
UPDATE public.trainings SET last_progress_at = now() - interval '10 min' WHERE id = :rq;
SELECT public.requeue_training(:rq, 5, 'modal_ended', NULL, 2500, 1800);
SELECT pg_temp.ok('PASS T23e modal_ended ends at last_progress_at too (30 min attempt, last progress 10 min ago): 1 200 s lost',
  (SELECT lost_seconds BETWEEN 4199 AND 4208 FROM public.trainings WHERE id = :rq),
  (SELECT lost_seconds::text FROM public.trainings WHERE id = :rq));
SELECT pg_temp.attempt(:rq, NULL) AS rq6 \gset
SELECT public.requeue_training(:rq, 6, 'start_timeout', NULL, 2500, 0);
SELECT pg_temp.ok('PASS T23f an attempt without a first running write adds 0 lost seconds; its reason is still listed',
  (SELECT lost_seconds BETWEEN 4199 AND 4208 AND cardinality(lost_reasons) = 6 AND lost_reasons[6] = 'start_timeout'
     FROM public.trainings WHERE id = :rq));
SELECT pg_temp.attempt(:rq, 600) AS rq7 \gset
SELECT public.finish_training(:rq, 7, 'failed', 'S4', NULL);
SELECT pg_temp.ok('PASS T23g the settlement reads train_seconds_used only (900 + 600 s -> 1 hour); the charge row lists the losses',
  (SELECT t.charged_hours = 1 AND (l.meta->>'seconds')::int BETWEEN 1500 AND 1505 AND (l.meta->>'lost_seconds')::int BETWEEN 4199 AND 4208
          AND jsonb_array_length(l.meta->'losses') = 6 AND l.meta->>'status' = 'failed'
     FROM public.trainings t JOIN public.credit_ledger l ON l.training_id = t.id AND l.kind = 'charge' WHERE t.id = :rq),
  (SELECT meta::text FROM public.credit_ledger WHERE kind = 'charge' AND training_id = :rq));
SELECT public.requeue_training(pg_temp.tid('c052/old-grp'), 1, 'pod_gone', NULL, 0, 600);
SELECT pg_temp.ok('PASS T23h a derived row''s requeue adds no lost seconds and no reason (old rules)',
  (SELECT lost_seconds = 0 AND lost_reasons = '{}' AND status = 'queued' FROM public.trainings WHERE model_name = 'c052/old-grp'));

\echo '=== T38 [R3-3] checkpoint_overdue: the E50 part charged, the lost part ends at the last step advance ==='
SELECT pg_temp.run('64', 'c052/r33', 2400) AS r33 \gset
SELECT pg_temp.ckpt(:r33, 1, 2500, 1500);
UPDATE public.trainings SET step_advanced_at = now() - interval '10 min' WHERE id = :r33;
SELECT public.requeue_training(:r33, 1, 'checkpoint_overdue', 'S39', 2500, 2400);
SELECT pg_temp.ok('PASS T38 a 40-minute attempt, own checkpoint at 15 min, last step advance 10 min ago: 900 s charged, 900 s lost',
  (SELECT train_seconds_used BETWEEN 900 AND 901 AND lost_seconds BETWEEN 899 AND 901 AND lost_reasons = '{checkpoint_overdue}' AND status = 'queued'
          AND current_step = 2500 AND status_note = 'S39' FROM public.trainings WHERE id = :r33),
  (SELECT train_seconds_used || '/' || lost_seconds FROM public.trainings WHERE id = :r33));

\echo '=== T28 [Q-A] cap_training_attempt accepts attempts_out / no_gpu; they publish like a cap ==='
SELECT pg_temp.run('64', 'c052/cap1', 600) AS cap1 \gset
SELECT pg_temp.ckpt(:cap1, 1, 2500, 0);
SELECT public.cap_training_attempt(:cap1, 1, 2500, 'attempts_out', 'S34', 600);
SELECT pg_temp.ok('PASS T28a attempts_out leaves the row running, capped at 2 500, the attempt ended',
  (SELECT t.status = 'running' AND t.capped_step = 2500 AND t.status_note = 'S34' AND a.state = 'ended' AND a.end_reason = 'attempts_out'
     FROM public.trainings t JOIN public.training_attempts a ON a.training_id = t.id WHERE t.id = :cap1));
SELECT pg_temp.ok('PASS T28b claim_training_publish accepts attempts_out', public.claim_training_publish(:cap1, 1, 900, 'c052'));
SELECT pg_temp.err(format($q$SELECT * FROM public.stop_training(%s, pg_temp.u('64'), pg_temp.u('64'), 'student', NULL)$q$, :cap1),
  'P0051', 'PASS T28c stop_training while it publishes answers P0051');
SELECT pg_temp.err(format($q$SELECT * FROM public.cancel_request_training(%s, pg_temp.u('64'))$q$, :cap1),
  'P0051', 'PASS T28d cancel_request_training while it publishes answers P0051');
SELECT public.finish_training(:cap1, 1, 'succeeded', NULL, NULL, 'S35', 2500, now() + interval '7 days');
SELECT pg_temp.ok('PASS T28e the publish settles the started hour and the row is continuable',
  (SELECT charged_hours = 1 AND status = 'succeeded' AND continuable_until > now() FROM public.trainings WHERE id = :cap1), pg_temp.led(:cap1));
SELECT pg_temp.run('64', 'c052/cap2', 600) AS cap2 \gset
SELECT pg_temp.ckpt(:cap2, 1, 2500, 0);
SELECT public.cap_training_attempt(:cap2, 1, 2500, 'no_gpu', 'S34', 600);
SELECT pg_temp.ok('PASS T28f no_gpu is accepted and its publish claimed', public.claim_training_publish(:cap2, 1, 900, 'c052'));
SELECT pg_temp.err(format($q$SELECT public.cap_training_attempt(%s, 1, 2500, 'whatever', NULL, 0)$q$, :cap2),
  '22023', 'PASS T28g an unknown cap reason answers 22023');
SELECT public.finish_training(:cap2, 1, 'succeeded', NULL, NULL, 'S36', 2500, now() + interval '7 days');

\echo '=== T36 [R3-1] content_refused publishes the last good checkpoint ==='
SELECT pg_temp.run('64', 'c052/r31', 3600) AS r31 \gset
SELECT pg_temp.ckpt(:r31, 1, 2500, 0);
SELECT public.cap_training_attempt(:r31, 1, 2500, 'content_refused', 'S34', 3600);
SELECT pg_temp.ok('PASS T36a content_refused leaves the row running, capped at 2 500',
  (SELECT status = 'running' AND capped_step = 2500 FROM public.trainings WHERE id = :r31));
SELECT pg_temp.ok('PASS T36b claim_training_publish takes it', public.claim_training_publish(:r31, 1, 900, 'c052'));
SELECT pg_temp.err(format($q$SELECT * FROM public.stop_training(%s, pg_temp.u('64'), pg_temp.u('64'), 'student', NULL)$q$, :r31),
  'P0051', 'PASS T36c stop_training answers P0051');
SELECT pg_temp.err(format($q$SELECT * FROM public.cancel_request_training(%s, pg_temp.u('64'))$q$, :r31),
  'P0051', 'PASS T36d cancel_request_training answers P0051');
SELECT public.finish_training(:r31, 1, 'succeeded', NULL, NULL, 'S38', 2500, now() + interval '7 days');
SELECT pg_temp.ok('PASS T36e the publish settles the started hour and makes the row continuable',
  (SELECT status = 'succeeded' AND charged_hours = 1 AND continuable_until > now() FROM public.trainings WHERE id = :r31), pg_temp.led(:r31));
SELECT pg_temp.err(format($q$SELECT public.cap_training_attempt(%s, 1, 2500, 'content', NULL, 0)$q$, :r31),
  '22023', 'PASS T36f a cap reason outside the list answers 22023');
SELECT pg_temp.run('64', 'c052/r31b', 60) AS r31b \gset
SELECT pg_temp.err(format($q$SELECT public.cap_training_attempt(%s, 1, 50000, 'content_refused', NULL, 0)$q$, :r31b),
  '22023', 'PASS T36g a cap at or above the total answers 22023');
SELECT public.finish_training(:r31b, 1, 'failed', 'x', NULL);

\echo '=== T33 [OQ-2] a confirmed S19 charges 0 ==='
SELECT pg_temp.run('64', 'c052/s19', 2400) AS s19 \gset
SELECT public.requeue_training(:s19, 1, 'host_unfit:lerobot_error_unconfirmed', NULL, 0, 2400);
SELECT pg_temp.attempt(:s19, 1800) AS s19b \gset
-- what the API's POST /start writes for a fresh attempt (no ready checkpoint to resume from)
UPDATE public.training_attempts SET resume_from_step = 0 WHERE training_id = :s19 AND attempt_no = 2;
SELECT public.finish_training(:s19, 2, 'failed', 'S19', NULL, NULL, NULL, NULL, 'lerobot_error');
SELECT pg_temp.ok('PASS T33a two attempts with heartbeats, finished failed with the waiver: charge 0, release 3, the waiver recorded',
  (SELECT charged_hours = 0 AND charge_waiver = 'lerobot_error' FROM public.trainings WHERE id = :s19)
  AND pg_temp.led(:s19) = 'reserve:3,charge:0,release:3'
  AND (SELECT meta->>'waiver' = 'lerobot_error' FROM public.credit_ledger WHERE kind = 'charge' AND training_id = :s19),
  pg_temp.led(:s19));
SELECT pg_temp.run('64', 'c052/s19n', 2400) AS s19n \gset
SELECT public.finish_training(:s19n, 1, 'failed', 'S7', NULL);
SELECT pg_temp.ok('PASS T33b the same failure without the waiver charges 1', (SELECT charged_hours = 1 FROM public.trainings WHERE id = :s19n));
SELECT pg_temp.run('64', 'c052/s19c', 600) AS s19c \gset
SELECT pg_temp.err(format($q$SELECT public.finish_training(%s, 1, 'succeeded', NULL, NULL, NULL, NULL, NULL, 'lerobot_error')$q$, :s19c),
  '22023', 'PASS T33c the waiver with succeeded answers 22023');
SELECT pg_temp.err(format($q$SELECT public.finish_training(%s, 1, 'failed', 'x', NULL, NULL, NULL, NULL, 'other')$q$, :s19c),
  '22023', 'PASS T33d an unknown waiver answers 22023');
SELECT public.finish_training(p_training_id => :s19c, p_attempt => 1, p_status => 'failed', p_message => 'x', p_log_url => NULL,
                              p_note => NULL, p_final_step => NULL, p_continuable_until => NULL);
SELECT pg_temp.ok('PASS T33e one finish_training overload; the v2.36.0 API''s eight NAMED arguments still resolve (and charge)',
  (SELECT count(*) = 1 FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND proname = 'finish_training')
  AND (SELECT status = 'failed' AND charged_hours = 1 AND charge_waiver IS NULL FROM public.trainings WHERE id = :s19c));

\echo '=== T21g-k the pre-052 rows finish under the old rules; the old columns move nothing ==='
SELECT pg_temp.ok('PASS T21g the pre-052 capped row''s publish is claimed as before',
  public.claim_training_publish(pg_temp.tid('c052/old-cap'), 1, 600, 'c052'));
SELECT public.finish_training(pg_temp.tid('c052/old-cap'), 1, 'succeeded', NULL, NULL, 'S23', 40000, now() + interval '7 days');
SELECT pg_temp.ok('PASS T21h ... it is succeeded, continuable, with no charge',
  pg_temp.led(pg_temp.tid('c052/old-cap')) = ''
  AND (SELECT status = 'succeeded' AND charged_hours IS NULL AND capped_step = 40000 AND continuable_until > now()
         FROM public.trainings WHERE model_name = 'c052/old-cap'));
SELECT public.complete_cancel_training(pg_temp.tid('c052/old-cr'), 'canceled', NULL);
UPDATE public.trainings SET status = 'failed' WHERE model_name = 'c052/old-legacy';
SELECT pg_temp.ok('PASS T21i the pre-052 cancel_requested row completes and the legacy row fails, both without a ledger row',
  pg_temp.led(pg_temp.tid('c052/old-cr')) = '' AND pg_temp.led(pg_temp.tid('c052/old-legacy')) = ''
  AND (SELECT bool_and(charged_hours IS NULL AND settled_at IS NULL) FROM public.trainings
        WHERE model_name IN ('c052/old-cr','c052/old-legacy','c052/old-done')));
SELECT free AS f1_before_k FROM public.credit_accounts WHERE kind = 'teacher' AND owner_id = pg_temp.u('f1') \gset
UPDATE public.users SET training_credits = 99 WHERE id = pg_temp.u('f1');
SELECT pg_temp.ok('PASS T21k the old API''s absolute set (users.training_credits) moves nothing',
  (SELECT free = :f1_before_k FROM public.credit_accounts WHERE kind = 'teacher' AND owner_id = pg_temp.u('f1')));

\echo '=== T22 start_continuation_safe reserves under the same rules ==='
INSERT INTO public.training_kept_checkpoints (training_id, step, manifest, expires_at)
VALUES (pg_temp.tid('c052/old-cap'), 40000, '{}', now() + interval '7 days');
SELECT free AS s52_before_22 FROM public.credit_accounts WHERE kind = 'student' AND owner_id = pg_temp.u('52') \gset
SELECT (o_reserved_hours = 3 AND o_remaining = :s52_before_22 - 3) AS c22
  FROM public.start_continuation_safe(pg_temp.u('52'), pg_temp.tid('c052/old-cap'), 'S28', 3) \gset
SELECT pg_temp.ok('PASS T22a s52 continues the capped pre-052 parent: 3 hours reserved, the child continues from step 40 000',
  :'c22'::boolean
  AND (SELECT credit_scheme = 'ledger' AND reserved_hours = 3 AND managed AND current_step = 40000 AND status_note = 'S28'
         FROM public.trainings WHERE continued_from = pg_temp.tid('c052/old-cap')));
SELECT pg_temp.err(format($q$SELECT * FROM public.start_continuation_safe(pg_temp.u('52'), %s, 'S28', 3)$q$, pg_temp.tid('c052/old-cap')),
  'P0054', 'PASS T22b a second continuation of the same parent answers P0054');
SELECT pg_temp.err(format($q$SELECT * FROM public.start_continuation_safe(pg_temp.u('51'), %s, 'S28', 3)$q$, pg_temp.tid('c052/old-cap')),
  'P0002', 'PASS T22c another student''s continuation answers P0002');
INSERT INTO public.trainings (user_id, status, dataset_name, model_name, model_type, training_params, total_steps, managed,
                              credit_scheme, capped_step, continuable_until)
VALUES (pg_temp.u('51'), 'succeeded', 'u/p', 'c052/parent51', 'act', '{"steps":50000}', 50000, true, 'derived', 2500, now() + interval '7 days'),
       (pg_temp.u('52'), 'succeeded', 'u/p', 'c052/parent52', 'act', '{"steps":50000}', 50000, true, 'derived', 2500, now() + interval '7 days');
INSERT INTO public.training_kept_checkpoints (training_id, step, manifest, expires_at)
SELECT id, 2500, '{}', now() + interval '7 days' FROM public.trainings WHERE model_name IN ('c052/parent51','c052/parent52');
SELECT pg_temp.err(format($q$SELECT * FROM public.start_continuation_safe(pg_temp.u('51'), %s, 'S28', 3)$q$, pg_temp.tid('c052/parent51')),
  'P0003', 'PASS T22d a continuation without free hours answers P0003');
SELECT pg_temp.ok('PASS T22e a continuation with p_max_hours 2 reserves 2',
  (SELECT o_reserved_hours = 2 FROM public.start_continuation_safe(pg_temp.u('52'), pg_temp.tid('c052/parent52'), 'S28', 2)));

\echo '=== T24 [D4, Q-D] credit_admin_compensate ==='
SELECT pg_temp.mk_group('a3', 'c1') IS NOT NULL AS a3 \gset
SELECT pg_temp.mk_user('65', 'student', 'c1') IS NOT NULL AS s65 \gset
UPDATE public.users SET workgroup_id = pg_temp.u('a3') WHERE id = pg_temp.u('65');
SELECT pg_temp.give('f1', 'group', 'a3', 10) AS g_a3 \gset
SELECT public.credit_set_member_limit(pg_temp.u('f1'), pg_temp.u('a3'), 1) AS lim_a3 \gset
SELECT pg_temp.run('65', 'c052/m-g65', 60) AS g65 \gset
SELECT public.finish_training(:g65, 1, 'failed', 'S19', NULL);
SELECT pg_temp.err($q$SELECT * FROM public.start_training_safe(pg_temp.u('65'), 'u/h', 'm', 'act', '{}'::jsonb, 1, NULL, true)$q$,
  'P0063', 'PASS T24a with member limit 1 used by a charged training, the next start answers P0063');
SELECT pg_temp.ok('PASS T24b the admin compensates the group naming that training (group free 10)',
  public.credit_admin_compensate(pg_temp.u('a0'), 'group', pg_temp.u('a3'), 1, 'Fehler des GPU-Servers', :g65, NULL) = 10);
SELECT pg_temp.ok('PASS T24c the compensate row names the training and its member, carries a reason id, the admin as actor',
  (SELECT member_id = pg_temp.u('65') AND training_id = :g65 AND reason_id IS NOT NULL AND actor_id = pg_temp.u('a0')
          AND actor_role = 'admin' AND from_kind IS NULL AND to_kind = 'group'
     FROM public.credit_ledger WHERE kind = 'compensate' AND to_id = pg_temp.u('a3')));
SELECT pg_temp.ok('PASS T24d the member''s headroom is back: quota next 1, used 0',
  (SELECT o_next_hours = 1 AND o_member_used = 0 FROM public.credit_quota(pg_temp.u('65'), 3)));
SELECT pg_temp.ok('PASS T24e s65 starts again (1 hour)',
  (SELECT reserved_hours = 1 FROM public.start_training_safe(pg_temp.u('65'), 'u/h', 'c052/m-h65', 'act', '{"steps":50000}'::jsonb, 50000, NULL, true)));
SELECT pg_temp.mk_user('66', 'student', 'c1') IS NOT NULL AS s66 \gset
SELECT pg_temp.give('f1', 'student', '66', 5) AS g66 \gset
SELECT pg_temp.run('66', 'c052/m-i66', 1800) AS i66 \gset
SELECT public.finish_training(:i66, 1, 'failed', 'S19', NULL);
UPDATE public.users SET workgroup_id = pg_temp.u('a3') WHERE id = pg_temp.u('66');
SELECT pg_temp.err($q$SELECT public.credit_admin_compensate(pg_temp.u('a0'), 'student', pg_temp.u('66'), 1, 'x', NULL, NULL)$q$,
  'P0023', 'PASS T24f compensating a grouped student without a training answers P0023');
SELECT pg_temp.err(format($q$SELECT public.credit_admin_compensate(pg_temp.u('a0'), 'group', pg_temp.u('a3'), 1, 'x', %s, NULL)$q$, :i66),
  'P0070', 'PASS T24g compensating the group for the student''s personal-funded training answers P0070');
SELECT count(*) AS n_acct FROM public.credit_accounts \gset
SELECT pg_temp.ok('PASS T24h the now-grouped student is compensated for the personal-funded training by naming it (free 5)',
  public.credit_admin_compensate(pg_temp.u('a0'), 'student', pg_temp.u('66'), 1, 'Absturz des GPU-Servers', :i66, NULL) = 5);
SELECT pg_temp.ok('PASS T40f a compensation naming a training creates no account',
  (SELECT count(*) FROM public.credit_accounts) = :n_acct);
SELECT pg_temp.err($q$SELECT public.credit_admin_compensate(pg_temp.u('f1'), 'group', pg_temp.u('a3'), 1, 'x', NULL, NULL)$q$,
  'P0067', 'PASS T24i a teacher''s compensation answers P0067');
SELECT pg_temp.err($q$SELECT public.credit_admin_compensate(pg_temp.u('a0'), 'student', pg_temp.u('56'), 1, 'x', NULL, NULL)$q$,
  'P0066', 'PASS T24j compensating a student without a classroom answers P0066');
SELECT pg_temp.err($q$SELECT public.credit_admin_compensate(pg_temp.u('a0'), 'student', pg_temp.u('52'), 1, '   ', NULL, NULL)$q$,
  '22023', 'PASS T24k a blank reason answers 22023');
SELECT pg_temp.err($q$SELECT public.credit_admin_compensate(pg_temp.u('a0'), 'student', pg_temp.u('52'), 101, 'x', NULL, NULL)$q$,
  '22023', 'PASS T24l 101 hours answer 22023');
SELECT pg_temp.err($q$SELECT public.credit_admin_compensate(pg_temp.u('a0'), 'teacher', pg_temp.u('f1'), 1, 'x', NULL, NULL)$q$,
  '22023', 'PASS T24m a teacher target answers 22023');
SELECT pg_temp.ok('PASS T24n the reason is a credit_reasons row about the student (I12)',
  (SELECT count(*) = 1 FROM public.credit_reasons WHERE subject_kind = 'student' AND subject_id = pg_temp.u('66')
                                                    AND text = 'Absturz des GPU-Servers'));
SELECT pg_temp.ok('PASS T24o credit_teacher_summary counts both compensations',
  (SELECT o_compensated = 2 FROM public.credit_teacher_summary(pg_temp.u('f1'))));
SELECT pg_temp.ok('PASS T24p I10 still holds (compensations are admin-created hours)', pg_temp.identity_ok(), pg_temp.identity());

\echo '=== T25c-g [D5] clearing and refusing member limits; the group history ==='
SELECT public.credit_set_member_limit(pg_temp.u('f1'), pg_temp.u('a1'), NULL) IS NULL AS lim_cleared \gset
SELECT pg_temp.ok('PASS T25c clearing a1''s limit writes a second limit_change {old: 2, new: null}',
  :'lim_cleared'::boolean
  AND (SELECT count(*) = 2 AND bool_or(meta = '{"old": 2, "new": null}'::jsonb)
         FROM public.credit_ledger WHERE kind = 'limit_change' AND to_id = pg_temp.u('a1')));
SELECT pg_temp.err($q$SELECT public.credit_set_member_limit(pg_temp.u('f1'), pg_temp.u('a1'), 0)$q$, '22023',
  'PASS T25d a limit of 0 answers 22023');
SELECT pg_temp.err($q$SELECT public.credit_set_member_limit(pg_temp.u('f1'), pg_temp.u('a1'), 1001)$q$, '22023',
  'PASS T25e a limit of 1 001 answers 22023');
SELECT pg_temp.err($q$SELECT public.credit_set_member_limit(pg_temp.u('f2'), pg_temp.u('a1'), 3)$q$, 'P0066',
  'PASS T25f another teacher''s group answers P0066');
SELECT pg_temp.ok('PASS T25g the group''s history: migrate, assign, limit_change, reserve, reserve, limit_change',
  (SELECT string_agg(kind, ',' ORDER BY id) FROM public.credit_history('group', pg_temp.u('a1'), NULL, 50))
  = 'migrate,assign,limit_change,reserve,reserve,limit_change',
  (SELECT string_agg(kind, ',' ORDER BY id) FROM public.credit_history('group', pg_temp.u('a1'), NULL, 50)));

\echo '=== T26 (review 1 m3) one idempotency rule: an exact replay writes nothing, any other change with the id is P0071 ==='
SELECT o_target_free AS r1 FROM public.credit_assign(pg_temp.u('f1'), 'student', pg_temp.u('52'), 3, pg_temp.u('e2')) \gset
SELECT o_target_free AS r1b FROM public.credit_assign(pg_temp.u('f1'), 'student', pg_temp.u('52'), 3, pg_temp.u('e2')) \gset
SELECT pg_temp.ok('PASS T26a the same assign twice: one ledger row, the replay answers the same values',
  :r1b = :r1
  AND (SELECT count(*) = 1 FROM public.credit_ledger WHERE request_id = pg_temp.u('e2')));
SELECT pg_temp.err($q$SELECT * FROM public.credit_assign(pg_temp.u('f1'), 'student', pg_temp.u('52'), 5, pg_temp.u('e2'))$q$,
  'P0071', 'PASS T26b the same request id for 5 hours answers P0071');
SELECT pg_temp.err($q$SELECT * FROM public.credit_reclaim(pg_temp.u('f1'), 'student', pg_temp.u('52'), 3, pg_temp.u('e2'))$q$,
  'P0071', 'PASS T26c the same request id for a reclaim answers P0071');
SELECT public.credit_admin_grant(pg_temp.u('a0'), pg_temp.u('f1'), 10, NULL, pg_temp.u('e3')) AS gr1 \gset
SELECT public.credit_admin_grant(pg_temp.u('a0'), pg_temp.u('f1'), 10, NULL, pg_temp.u('e3')) AS gr1b \gset
SELECT pg_temp.ok('PASS T26d the same grant twice: one ledger row',
  :gr1b = :gr1
  AND (SELECT count(*) = 1 FROM public.credit_ledger WHERE request_id = pg_temp.u('e3')));
SELECT pg_temp.err($q$SELECT public.credit_admin_grant(pg_temp.u('a0'), pg_temp.u('f1'), 11, NULL, pg_temp.u('e3'))$q$,
  'P0071', 'PASS T26e the same grant id for 11 hours answers P0071');
SELECT public.credit_admin_revoke(pg_temp.u('a0'), pg_temp.u('f1'), 1, NULL, pg_temp.u('e4')) AS rv1 \gset
SELECT public.credit_admin_revoke(pg_temp.u('a0'), pg_temp.u('f1'), 1, NULL, pg_temp.u('e4')) AS rv1b \gset
SELECT pg_temp.ok('PASS T26f the same revoke twice: one ledger row',
  :rv1b = :rv1
  AND (SELECT count(*) = 1 FROM public.credit_ledger WHERE request_id = pg_temp.u('e4')));
SELECT public.credit_admin_compensate(pg_temp.u('a0'), 'student', pg_temp.u('52'), 1, 'Ausgleich', NULL, pg_temp.u('e5')) AS cp1 \gset
SELECT public.credit_admin_compensate(pg_temp.u('a0'), 'student', pg_temp.u('52'), 1, 'Ausgleich', NULL, pg_temp.u('e5')) AS cp1b \gset
SELECT pg_temp.ok('PASS T26g the same compensation twice: one ledger row, one reason row',
  :cp1b = :cp1
  AND (SELECT count(*) = 1 FROM public.credit_ledger WHERE request_id = pg_temp.u('e5'))
  AND (SELECT count(*) = 1 FROM public.credit_reasons WHERE subject_id = pg_temp.u('52') AND text = 'Ausgleich'));
SELECT pg_temp.err($q$SELECT * FROM public.credit_assign(pg_temp.u('f1'), 'student', pg_temp.u('52'), 1, pg_temp.u('e1'))$q$,
  'P0071', 'PASS T26h a start key reused as an assign request id answers P0071');
SELECT o_target_free AS rc1 FROM public.credit_reclaim(pg_temp.u('f1'), 'student', pg_temp.u('52'), 1, pg_temp.u('e6')) \gset
SELECT o_target_free AS rc1b FROM public.credit_reclaim(pg_temp.u('f1'), 'student', pg_temp.u('52'), 1, pg_temp.u('e6')) \gset
SELECT pg_temp.ok('PASS T26i the same reclaim twice: one ledger row',
  :rc1b = :rc1
  AND (SELECT count(*) = 1 FROM public.credit_ledger WHERE request_id = pg_temp.u('e6')));

\echo '=== T27 (review 1 m6, R7) an operator''s move to another teacher re-parents the account ==='
SELECT pg_temp.run('55', 'c052/d3', 100) AS d3 \gset
SELECT pg_temp.err($q$UPDATE public.users SET classroom_id = pg_temp.u('c1') WHERE id = pg_temp.u('55')$q$, 'P0072',
  'PASS T27a a move to another teacher while a reservation is open answers P0072');
SELECT public.finish_training(:d3, 1, 'succeeded', NULL, NULL);
SELECT free AS f2_before_27 FROM public.credit_accounts WHERE kind = 'teacher' AND owner_id = pg_temp.u('f2') \gset
UPDATE public.users SET classroom_id = pg_temp.u('c1') WHERE id = pg_temp.u('55');
SELECT pg_temp.ok('PASS T27b after the training ended the move returns the 9 free hours to f2 and re-parents to f1',
  (SELECT parent_teacher_id = pg_temp.u('f1') AND free = 0 FROM public.credit_accounts WHERE kind = 'student' AND owner_id = pg_temp.u('55'))
  AND (SELECT free = :f2_before_27 + 9 FROM public.credit_accounts WHERE kind = 'teacher' AND owner_id = pg_temp.u('f2'))
  AND (SELECT hours = 9 AND meta->>'why' = 'moved' AND to_id = pg_temp.u('f2') AND actor_role = 'system'
         FROM public.credit_ledger WHERE kind = 'reclaim' AND from_id = pg_temp.u('55')),
  pg_temp.acct('student', pg_temp.u('55')));
SELECT pg_temp.ok('PASS T27c the new teacher can assign', pg_temp.give('f1', 'student', '55', 2) = 2);
SELECT pg_temp.err($q$SELECT * FROM public.credit_reclaim(pg_temp.u('f2'), 'student', pg_temp.u('55'), 1, NULL)$q$, 'P0066',
  'PASS T27d the old teacher can no longer reclaim (P0066)');
SELECT pg_temp.mk_user('f7', 'teacher', NULL) IS NOT NULL AS f7 \gset
SELECT pg_temp.mk_class('c7', 'f7') IS NOT NULL AS c7 \gset
SELECT pg_temp.mk_user('67', 'student', 'c1') IS NOT NULL AS s67 \gset
SELECT pg_temp.give('f1', 'student', '67', 2) AS g67 \gset
UPDATE public.users SET classroom_id = pg_temp.u('c7') WHERE id = pg_temp.u('67');
SELECT pg_temp.ok('PASS T27e a move to a teacher without an account creates that account (I13)',
  EXISTS (SELECT 1 FROM public.credit_accounts WHERE kind = 'teacher' AND owner_id = pg_temp.u('f7') AND closed_at IS NULL)
  AND (SELECT parent_teacher_id = pg_temp.u('f7') AND free = 0 FROM public.credit_accounts WHERE kind = 'student' AND owner_id = pg_temp.u('67')));
SELECT pg_temp.mk_class('cb', 'f1') IS NOT NULL AS cb \gset
UPDATE public.users SET classroom_id = pg_temp.u('cb') WHERE id = pg_temp.u('51');
SELECT pg_temp.ok('PASS T27f a move inside one teacher''s classrooms changes nothing, even with an open reservation',
  pg_temp.acct('student', pg_temp.u('51')) = '0/4/0'
  AND (SELECT parent_teacher_id = pg_temp.u('f1') FROM public.credit_accounts WHERE kind = 'student' AND owner_id = pg_temp.u('51')));
UPDATE public.users SET classroom_id = pg_temp.u('c1') WHERE id = pg_temp.u('51');
SELECT pg_temp.mk_group('a4', 'c1') IS NOT NULL AS a4 \gset
SELECT pg_temp.give('f1', 'group', 'a4', 3) AS g_a4 \gset
SELECT free AS f1_before_27g FROM public.credit_accounts WHERE kind = 'teacher' AND owner_id = pg_temp.u('f1') \gset
UPDATE public.workgroups SET classroom_id = pg_temp.u('c2') WHERE id = pg_temp.u('a4');
SELECT pg_temp.ok('PASS T27g a group moved to another teacher''s classroom: its 3 free hours back to f1, re-parented to f2',
  (SELECT parent_teacher_id = pg_temp.u('f2') AND free = 0 FROM public.credit_accounts WHERE kind = 'group' AND owner_id = pg_temp.u('a4'))
  AND (SELECT free = :f1_before_27g + 3 FROM public.credit_accounts WHERE kind = 'teacher' AND owner_id = pg_temp.u('f1')));

\echo '=== T21a-f the compatibility functions for an API older than 052 ==='
SELECT pg_temp.err($q$SELECT * FROM public.adjust_student_credits(pg_temp.u('f1'), pg_temp.u('51'), 1)$q$, 'P0069',
  'PASS T21a adjust_student_credits answers P0069');
SELECT pg_temp.err($q$SELECT * FROM public.adjust_workgroup_credits(pg_temp.u('f1'), pg_temp.u('a1'), 1)$q$, 'P0069',
  'PASS T21b adjust_workgroup_credits answers P0069');
SELECT pg_temp.ok('PASS T21c get_remaining_credits(s51) = (4,4,0) with the exact old types (integer, bigint, bigint)',
  (SELECT row(g.*)::text = '(4,4,0)' AND pg_typeof(g.training_credits)::text = 'integer'
          AND pg_typeof(g.trainings_used)::text = 'bigint' AND pg_typeof(g.remaining)::text = 'bigint'
     FROM public.get_remaining_credits(pg_temp.u('51')) g),
  (SELECT row(g.*)::text FROM public.get_remaining_credits(pg_temp.u('51')) g));
SELECT pg_temp.ok('PASS T21d get_remaining_credits(s52) = (free + reserved + spent, reserved + spent, free) of its account',
  (SELECT row(g.*)::text FROM public.get_remaining_credits(pg_temp.u('52')) g)
  = (SELECT '(' || (free + reserved + spent) || ',' || (reserved + spent) || ',' || free || ')'
       FROM public.credit_accounts WHERE kind = 'student' AND owner_id = pg_temp.u('52')));
SELECT pg_temp.ok('PASS T21e a group member at the member limit: remaining 0 (startable free capped by member_left)',
  (SELECT row(g.*)::text FROM public.get_remaining_credits(pg_temp.u('65')) g)
  = (SELECT '(' || (reserved + spent) || ',' || (reserved + spent) || ',0)'
       FROM public.credit_accounts WHERE kind = 'group' AND owner_id = pg_temp.u('a3')),
  (SELECT row(g.*)::text FROM public.get_remaining_credits(pg_temp.u('65')) g));
SELECT pg_temp.ok('PASS T21f get_teacher_credit_summary is credit_teacher_summary in the old six columns',
  (SELECT row(g.*)::text FROM public.get_teacher_credit_summary(pg_temp.u('f1')) g)
  = (SELECT '(' || (o_free + o_assigned_free + o_assigned_reserved) || ',' || (o_assigned_free + o_assigned_reserved) || ','
            || o_free || ',' || o_student_count || ',' || o_group_count || ',' || o_group_hours || ')'
       FROM public.credit_teacher_summary(pg_temp.u('f1'))));
SELECT pg_temp.ok('PASS T21j the derived pre-052 rows never touched the ledger',
  NOT EXISTS (SELECT 1 FROM public.credit_ledger l JOIN public.trainings t ON t.id = l.training_id WHERE t.credit_scheme = 'derived'));

\echo '=== T9 deleting a student with an open training (cascade from auth.users) ==='
SELECT pg_temp.mk_user('58', 'student', 'c1') IS NOT NULL AS s58 \gset
SELECT pg_temp.give('f1', 'student', '58', 6) AS g58 \gset
SELECT pg_temp.run('58', 'c052/d1', 1800) AS d1 \gset
SELECT public.credit_admin_compensate(pg_temp.u('a0'), 'student', pg_temp.u('58'), 2, 'Grund über s58', NULL, NULL) AS c58 \gset
SELECT free AS f1_before_9 FROM public.credit_accounts WHERE kind = 'teacher' AND owner_id = pg_temp.u('f1') \gset
DELETE FROM auth.users WHERE id = pg_temp.u('58');
SELECT pg_temp.ok('PASS T9a the open training is settled on delete: charge 1, release 2',
  pg_temp.led(:d1) = 'reserve:3,charge:1,release:2' AND (SELECT meta->>'status' = 'deleted' FROM public.credit_ledger
                                                          WHERE kind = 'charge' AND training_id = :d1), pg_temp.led(:d1));
SELECT pg_temp.ok('PASS T9b the account is closed: 0 free, 0 reserved, 1 spent',
  (SELECT closed_at IS NOT NULL AND free = 0 AND reserved = 0 AND spent = 1 FROM public.credit_accounts
    WHERE kind = 'student' AND owner_id = pg_temp.u('58')), pg_temp.acct('student', pg_temp.u('58')));
SELECT pg_temp.ok('PASS T9c the AFTER-DELETE close reclaimed the 7 free hours to the teacher',
  (SELECT hours = 7 AND to_id = pg_temp.u('f1') AND meta->>'why' = 'closed' AND actor_role = 'system' AND training_id IS NULL
     FROM public.credit_ledger WHERE kind = 'reclaim' AND from_id = pg_temp.u('58'))
  AND (SELECT free = :f1_before_9 + 7 FROM public.credit_accounts WHERE kind = 'teacher' AND owner_id = pg_temp.u('f1')));
SELECT pg_temp.ok('PASS T9d the reasons about the student are erased (I12)',
  NOT EXISTS (SELECT 1 FROM public.credit_reasons WHERE subject_id = pg_temp.u('58'))
  AND (SELECT reason_id IS NOT NULL FROM public.credit_ledger WHERE kind = 'compensate' AND to_id = pg_temp.u('58')));
SELECT pg_temp.ok('PASS T9e the six ledger rows about the student are kept (assign, reserve, compensate, charge, release, reclaim)',
  (SELECT string_agg(kind, ',' ORDER BY id) FROM public.credit_ledger
    WHERE from_id = pg_temp.u('58') OR to_id = pg_temp.u('58') OR member_id = pg_temp.u('58'))
  = 'assign,reserve,compensate,charge,release,reclaim');
SELECT pg_temp.ok('PASS T9f the training row is gone and nothing is left unsettled',
  NOT EXISTS (SELECT 1 FROM public.trainings WHERE id = :d1) AND (SELECT o_orphans = 0 AND o_unsettled = 0 FROM public.credit_reconcile()));
SELECT pg_temp.ok('PASS T29c the settlement (BEFORE DELETE on trainings) ran before the close (AFTER DELETE on users)',
  (SELECT max(id) FROM public.credit_ledger WHERE training_id = :d1)
  < (SELECT id FROM public.credit_ledger WHERE kind = 'reclaim' AND from_id = pg_temp.u('58')));

\echo '=== T10 deleting a group with an open member reservation ==='
SELECT pg_temp.mk_group('a2', 'c1') IS NOT NULL AS a2 \gset
SELECT pg_temp.mk_user('59', 'student', 'c1') IS NOT NULL AS s59 \gset
SELECT pg_temp.mk_user('60', 'student', 'c1') IS NOT NULL AS s60 \gset
UPDATE public.users SET workgroup_id = pg_temp.u('a2') WHERE id IN (pg_temp.u('59'), pg_temp.u('60'));
SELECT pg_temp.give('f1', 'group', 'a2', 10) AS g_a2 \gset
SELECT pg_temp.run('59', 'c052/d2', 600) AS d2 \gset
DELETE FROM public.workgroups WHERE id = pg_temp.u('a2');
SELECT pg_temp.ok('PASS T10a the group is closed at once: its 7 free hours reclaimed, the 3 reserved stay',
  (SELECT closed_at IS NOT NULL AND free = 0 AND reserved = 3 FROM public.credit_accounts WHERE kind = 'group' AND owner_id = pg_temp.u('a2'))
  AND (SELECT hours = 7 AND meta->>'why' = 'closed' FROM public.credit_ledger WHERE kind = 'reclaim' AND from_id = pg_temp.u('a2')
                                                                                AND training_id IS NULL));
SELECT pg_temp.ok('PASS T10b the member''s training runs on (workgroup_id NULL, still a ledger row)',
  (SELECT status = 'running' AND workgroup_id IS NULL AND credit_scheme = 'ledger' FROM public.trainings WHERE id = :d2));
SELECT public.finish_training(:d2, 1, 'succeeded', NULL, NULL);
SELECT pg_temp.ok('PASS T10c its later settlement charges the group and returns the released part to the teacher',
  pg_temp.led(:d2) = 'reserve:3,charge:1,release:2,reclaim:2', pg_temp.led(:d2));
SELECT pg_temp.ok('PASS T10d the closed group reads 0 / 0 / 1', pg_temp.acct('group', pg_temp.u('a2')) = '0/0/1', pg_temp.acct('group', pg_temp.u('a2')));
SELECT pg_temp.ok('PASS T10e the ex-member now funds personally (no personal account: nothing to start with)',
  (SELECT o_account_kind = 'student' AND o_free = 0 AND o_next_hours = 0 AND o_member_used IS NULL FROM public.credit_quota(pg_temp.u('59'), 3)));
SELECT pg_temp.err(format($q$SELECT public.credit_admin_compensate(pg_temp.u('a0'), 'group', pg_temp.u('a2'), 1, 'x', %s, NULL)$q$, :d2),
  'P0002', 'PASS T24q a compensation naming the deleted group''s training answers P0002 (the target is gone)');

\echo '=== T11 deleting a teacher revokes the teacher''s free hours [D8] ==='
SELECT pg_temp.mk_user('f5', 'teacher', NULL) IS NOT NULL AS f5 \gset
SELECT pg_temp.mk_class('c5', 'f5') IS NOT NULL AS c5 \gset
SELECT public.credit_admin_grant(pg_temp.u('a0'), pg_temp.u('f5'), 7, 'Für Klasse 5', NULL) AS gf5 \gset
DELETE FROM auth.users WHERE id = pg_temp.u('f5');
SELECT pg_temp.ok('PASS T11a the teacher''s account is closed and its 7 free hours left as a system revoke',
  (SELECT closed_at IS NOT NULL AND free = 0 FROM public.credit_accounts WHERE kind = 'teacher' AND owner_id = pg_temp.u('f5'))
  AND (SELECT hours = 7 AND actor_role = 'system' AND actor_id IS NULL AND meta->>'why' = 'closed'
         FROM public.credit_ledger WHERE kind = 'revoke' AND from_id = pg_temp.u('f5')));
SELECT pg_temp.ok('PASS T11b the reason about the deleted teacher is erased',
  NOT EXISTS (SELECT 1 FROM public.credit_reasons WHERE subject_id = pg_temp.u('f5')));

\echo '=== T32 (review 2 m10) a teacher deleted while owning a classroom with a group: nothing stranded ==='
SELECT pg_temp.mk_user('f6', 'teacher', NULL) IS NOT NULL AS f6 \gset
SELECT pg_temp.mk_class('c6', 'f6') IS NOT NULL AS c6 \gset
SELECT pg_temp.mk_group('a6', 'c6') IS NOT NULL AS a6 \gset
SELECT pg_temp.mk_user('62', 'student', 'c6') IS NOT NULL AS s62 \gset
SELECT public.credit_admin_grant(pg_temp.u('a0'), pg_temp.u('f6'), 20, NULL, NULL) AS gf6 \gset
SELECT pg_temp.give('f6', 'group', 'a6', 4) AS g_a6 \gset
SELECT pg_temp.give('f6', 'student', '62', 3) AS g62 \gset
DELETE FROM auth.users WHERE id = pg_temp.u('f6');
SELECT pg_temp.ok('PASS T32a the cascaded group''s 4 free hours leave as a system revoke (its teacher closed first)',
  (SELECT closed_at IS NOT NULL AND free = 0 FROM public.credit_accounts WHERE kind = 'group' AND owner_id = pg_temp.u('a6'))
  AND (SELECT hours = 4 AND actor_role = 'system' AND meta->>'parent' = 'closed'
         FROM public.credit_ledger WHERE kind = 'revoke' AND from_kind = 'group' AND from_id = pg_temp.u('a6')),
  (SELECT string_agg(kind || ':' || hours || ':' || COALESCE(from_kind, '') || '>' || COALESCE(to_kind, ''), ',' ORDER BY id)
     FROM public.credit_ledger WHERE from_id IN (pg_temp.u('a6'), pg_temp.u('f6'))));
SELECT pg_temp.ok('PASS T32b the teacher''s own 13 free hours leave as a system revoke',
  (SELECT closed_at IS NOT NULL AND free = 0 FROM public.credit_accounts WHERE kind = 'teacher' AND owner_id = pg_temp.u('f6'))
  AND (SELECT hours = 13 FROM public.credit_ledger WHERE kind = 'revoke' AND from_kind = 'teacher' AND from_id = pg_temp.u('f6')));
SELECT pg_temp.ok('PASS T32c the student (now without a classroom) keeps an open account, 3 free, under the closed teacher',
  (SELECT closed_at IS NULL AND free = 3 AND parent_teacher_id = pg_temp.u('f6')
     FROM public.credit_accounts WHERE kind = 'student' AND owner_id = pg_temp.u('62')));
DELETE FROM auth.users WHERE id = pg_temp.u('62');
SELECT pg_temp.ok('PASS T32d deleting that student revokes its 3 free hours (the parent is closed, I14)',
  (SELECT hours = 3 AND actor_role = 'system' AND meta->>'parent' = 'closed'
     FROM public.credit_ledger WHERE kind = 'revoke' AND from_kind = 'student' AND from_id = pg_temp.u('62')));
SELECT pg_temp.ok('PASS T32e no free or reserved hours sit in any closed account',
  NOT EXISTS (SELECT 1 FROM public.credit_accounts WHERE closed_at IS NOT NULL AND (free <> 0 OR reserved <> 0)));

\echo '=== T31 (review 2 m2) a compensation and a member limit under a teacher without an account ==='
SELECT pg_temp.mk_user('f8', 'teacher', NULL) IS NOT NULL AS f8 \gset
SELECT pg_temp.mk_class('c8', 'f8') IS NOT NULL AS c8 \gset
SELECT pg_temp.mk_group('a8', 'c8') IS NOT NULL AS a8 \gset
SELECT pg_temp.mk_user('68', 'student', 'c8') IS NOT NULL AS s68 \gset
SELECT pg_temp.mk_user('69', 'student', 'c8') IS NOT NULL AS s69 \gset
UPDATE public.users SET workgroup_id = pg_temp.u('a8') WHERE id = pg_temp.u('69');
SELECT public.credit_set_member_limit(pg_temp.u('f8'), pg_temp.u('a8'), 2) AS lim_a8 \gset
SELECT pg_temp.ok('PASS T31a a member limit creates the teacher''s account and the group account under it',
  :lim_a8 = 2
  AND EXISTS (SELECT 1 FROM public.credit_accounts WHERE kind = 'teacher' AND owner_id = pg_temp.u('f8'))
  AND (SELECT parent_teacher_id = pg_temp.u('f8') AND member_hour_limit = 2 FROM public.credit_accounts
        WHERE kind = 'group' AND owner_id = pg_temp.u('a8')));
SELECT public.credit_admin_compensate(pg_temp.u('a0'), 'student', pg_temp.u('68'), 3, 'Ausgleich für s68', NULL, NULL) AS comp68 \gset
SELECT pg_temp.ok('PASS T31b a compensation of the personal student creates its account under the same teacher',
  :comp68 = 3
  AND (SELECT parent_teacher_id = pg_temp.u('f8') FROM public.credit_accounts WHERE kind = 'student' AND owner_id = pg_temp.u('68')));
SELECT public.credit_admin_compensate(pg_temp.u('a0'), 'group', pg_temp.u('a8'), 2, 'Ausgleich für die Gruppe', NULL, NULL) AS ca8 \gset
SELECT pg_temp.run('69', 'c052/m2g', 60) AS m2g \gset
DELETE FROM public.workgroups WHERE id = pg_temp.u('a8');
SELECT public.finish_training(:m2g, 1, 'succeeded', NULL, NULL);
SELECT pg_temp.ok('PASS T31c the closed group''s member training finishes: charge 1, release 1 back to the teacher',
  pg_temp.led(:m2g) = 'reserve:2,charge:1,release:1,reclaim:1', pg_temp.led(:m2g));
DELETE FROM auth.users WHERE id = pg_temp.u('68');
SELECT pg_temp.ok('PASS T31d the compensated student is deletable; its 3 hours go back to the teacher',
  (SELECT closed_at IS NOT NULL AND free = 0 FROM public.credit_accounts WHERE kind = 'student' AND owner_id = pg_temp.u('68'))
  AND (SELECT free = 4 FROM public.credit_accounts WHERE kind = 'teacher' AND owner_id = pg_temp.u('f8')),
  pg_temp.acct('teacher', pg_temp.u('f8')));
SELECT pg_temp.ok('PASS T31e no child account exists without its teacher account (I13)',
  NOT EXISTS (SELECT 1 FROM public.credit_accounts a WHERE a.parent_teacher_id IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM public.credit_accounts t WHERE t.kind = 'teacher' AND t.owner_id = a.parent_teacher_id)));

\echo '=== T18 credit_history: scopes, reasons, erased reasons, paging ==='
SELECT pg_temp.ok('PASS T18a the student scope lists the student''s own rows newest first',
  (SELECT string_agg(kind, ',' ORDER BY id) FROM public.credit_history('student', pg_temp.u('51'), NULL, 100))
  = 'migrate,assign,reserve,reserve',
  (SELECT string_agg(kind, ',' ORDER BY id) FROM public.credit_history('student', pg_temp.u('51'), NULL, 100)));
SELECT pg_temp.ok('PASS T18b the student scope includes the group rows about the member (member_id)',
  EXISTS (SELECT 1 FROM public.credit_history('student', pg_temp.u('53'), NULL, 100) h
           WHERE h.kind = 'reserve' AND h.from_kind = 'group' AND h.member_id = pg_temp.u('53')));
SELECT pg_temp.ok('PASS T18c the teacher scope carries the grant''s reason',
  (SELECT reason = 'Schuljahr 2026/27' AND reason_id IS NOT NULL FROM public.credit_history('teacher', pg_temp.u('f1'), NULL, 200)
    WHERE kind = 'grant' AND hours = 500));
SELECT pg_temp.ok('PASS T18d teacher_all adds the rows of every child account, never another teacher''s',
  EXISTS (SELECT 1 FROM public.credit_history('teacher_all', pg_temp.u('f1'), NULL, 200) h
           WHERE h.kind = 'reserve' AND h.from_id = pg_temp.u('51'))
  AND NOT EXISTS (SELECT 1 FROM public.credit_history('teacher_all', pg_temp.u('f1'), NULL, 200) h
                   WHERE h.kind = 'grant' AND h.to_id = pg_temp.u('f2')));
SELECT pg_temp.ok('PASS T18e p_limit is clamped to 1…200 and p_before pages',
  (SELECT count(*) = 1 FROM public.credit_history('all', NULL, NULL, 0))
  AND (SELECT count(*) FROM public.credit_history('all', NULL, NULL, 500)) = LEAST(200, (SELECT count(*) FROM public.credit_ledger))
  AND (SELECT count(*) > 100 FROM public.credit_ledger)
  AND (SELECT bool_and(id < 50) AND count(*) = (SELECT count(*) FROM public.credit_ledger WHERE id < 50)
         FROM public.credit_history('all', NULL, 50, 200)));
SELECT pg_temp.err($q$SELECT * FROM public.credit_history('nobody', pg_temp.u('51'), NULL, 10)$q$, '22023',
  'PASS T18f an unknown scope answers 22023');
SELECT reason_id AS rid FROM public.credit_ledger WHERE kind = 'grant' AND to_id = pg_temp.u('f1') AND hours = 500 \gset
SELECT pg_temp.ok('PASS T18g the admin erases the grant''s reason (true)', public.credit_erase_reason(pg_temp.u('a0'), :rid));
SELECT pg_temp.ok('PASS T18h the history then shows the reason id with no text',
  (SELECT reason IS NULL AND reason_id = :rid FROM public.credit_history('teacher', pg_temp.u('f1'), NULL, 200) WHERE kind = 'grant' AND hours = 500));
SELECT pg_temp.ok('PASS T18i erasing it again answers false', NOT public.credit_erase_reason(pg_temp.u('a0'), :rid));
SELECT pg_temp.err(format($q$SELECT public.credit_erase_reason(pg_temp.u('f1'), %s)$q$, :rid), 'P0067',
  'PASS T18j a teacher''s erase answers P0067');

\echo '=== T19 record_training_refusal: sanitised model type, prune to 200 ==='
SELECT public.record_training_refusal(pg_temp.u('52'), 'start', 'policy', '  PI0  ');
SELECT pg_temp.ok('PASS T19a „  PI0  " is stored as pi0, with the user''s current role',
  (SELECT model_type = 'pi0' AND role = 'student' AND code = 'policy' AND request_kind = 'start'
     FROM public.training_start_refusals WHERE user_id = pg_temp.u('52') ORDER BY id DESC LIMIT 1));
SELECT public.record_training_refusal(pg_temp.u('52'), 'continue', 'no_hours', 'PI0 <b>Hallo</b>');
SELECT pg_temp.ok('PASS T19b free text is never stored („PI0 <b>Hallo</b>" -> NULL)',
  (SELECT model_type IS NULL FROM public.training_start_refusals WHERE user_id = pg_temp.u('52') ORDER BY id DESC LIMIT 1));
SELECT count(*) AS n19 FROM public.training_start_refusals \gset
SELECT pg_temp.err($q$SELECT public.record_training_refusal(gen_random_uuid(), 'start', 'no_hours', NULL)$q$, 'P0002',
  'PASS T19c an unknown user answers P0002');
SELECT pg_temp.ok('PASS T19d ... and nothing was written', (SELECT count(*) FROM public.training_start_refusals) = :n19);
SELECT pg_temp.err($q$SELECT public.record_training_refusal(pg_temp.u('52'), 'start', 'dataset', NULL)$q$, '23514',
  'PASS T19e a code outside role/policy/no_hours/member_limit violates the CHECK');
DO $$ BEGIN
  FOR i IN 1..205 LOOP PERFORM public.record_training_refusal(pg_temp.u('51'), 'start', 'no_hours', 'act'); END LOOP;
END $$;
SELECT pg_temp.ok('PASS T19f a user keeps the newest 200 refusals',
  (SELECT count(*) = 200 FROM public.training_start_refusals WHERE user_id = pg_temp.u('51'))
  AND (SELECT max(id) FROM public.training_start_refusals WHERE user_id = pg_temp.u('51'))
      = (SELECT max(id) FROM public.training_start_refusals));

\echo '=== T39 (review 3 m6) the stale-open threshold covers a wall-capped publish that retries for 6 h ==='
SELECT pg_temp.run('64', 'c052/m6', 600) AS m6 \gset
SELECT pg_temp.ckpt(:m6, 1, 2500, 0);
SELECT public.cap_training_attempt(:m6, 1, 2500, 'wall_capped', 'S22', 600);
BEGIN;
ALTER TABLE public.credit_ledger DISABLE TRIGGER credit_ledger_no_change;
UPDATE public.credit_ledger SET at = now() - interval '15 hours' WHERE kind = 'reserve' AND training_id = :m6;
SELECT pg_temp.ok('PASS T39a an open reservation 15 h old (a wall-capped publish) is not stale',
  (SELECT o_stale_open = 0 FROM public.credit_reconcile()), (SELECT row(r.*)::text FROM public.credit_reconcile() r));
UPDATE public.credit_ledger SET at = now() - interval '20 hours' WHERE kind = 'reserve' AND training_id = :m6;
SELECT pg_temp.ok('PASS T39b one 20 h old is', (SELECT o_stale_open = 1 FROM public.credit_reconcile()),
  (SELECT row(r.*)::text FROM public.credit_reconcile() r));
ROLLBACK;
SELECT pg_temp.ok('PASS T39c the probe was rolled back: the ledger trigger is enabled and the row fresh',
  (SELECT tgenabled = 'O' FROM pg_trigger WHERE tgname = 'credit_ledger_no_change')
  AND (SELECT at > now() - interval '1 hour' FROM public.credit_ledger WHERE kind = 'reserve' AND training_id = :m6));
SELECT public.finish_training(:m6, 1, 'succeeded', NULL, NULL, 'S29', 2500, now() + interval '7 days');

\echo '=== T40 (review 3 m15) the gaps the builds resolved differently, resolved once ==='
SELECT pg_temp.ok('PASS T40a a teacher''s quota row is (teacher,NULL,NULL,0,0,0,NULL,NULL,0)',
  (SELECT row(q.*)::text FROM public.credit_quota(pg_temp.u('f1'), 3) q) = '(teacher,,,0,0,0,,,0)',
  (SELECT row(q.*)::text FROM public.credit_quota(pg_temp.u('f1'), 3) q));
SELECT pg_temp.ok('PASS T40b get_remaining_credits of a teacher is (0,0,0)',
  (SELECT row(g.*)::text FROM public.get_remaining_credits(pg_temp.u('f1')) g) = '(0,0,0)');
SELECT training_id AS m15 FROM public.start_training_safe(pg_temp.u('64'), 'u/m15', 'c052/m15', 'act', '{"steps":50000}'::jsonb, 50000, NULL, true) \gset
SELECT pg_temp.ok('PASS T40c a cancel outcome carries no step',
  (SELECT o_outcome = 'cancel' AND o_step IS NULL FROM public.stop_training(:m15, pg_temp.u('64'), pg_temp.u('64'), 'student', NULL)));
SELECT public.complete_cancel_training(:m15, 'canceled', NULL);
SELECT pg_temp.mk_group('a9', 'c1') IS NOT NULL AS a9 \gset
SELECT pg_temp.mk_user('71', 'student', 'c1') IS NOT NULL AS s71 \gset
UPDATE public.users SET workgroup_id = pg_temp.u('a9') WHERE id = pg_temp.u('71');
SELECT pg_temp.ok('PASS T40d a member of a group without an account: free 0, member_used 0, member_left/limit NULL, next 0',
  (SELECT row(q.*)::text FROM public.credit_quota(pg_temp.u('71'), 3) q) = '(student,group,' || pg_temp.u('a9') || ',0,0,0,,0,0)',
  (SELECT row(q.*)::text FROM public.credit_quota(pg_temp.u('71'), 3) q));
SELECT pg_temp.ok('PASS T40e a personal student without an account: member_used NULL',
  (SELECT row(q.*)::text FROM public.credit_quota(pg_temp.u('59'), 3) q) = '(student,student,' || pg_temp.u('59') || ',0,0,0,,,0)',
  (SELECT row(q.*)::text FROM public.credit_quota(pg_temp.u('59'), 3) q));

\echo '=== T41 (review 3 m11) a start of a student moved into another group is funded by the new group ==='
SELECT pg_temp.mk_group('b0', 'c1') IS NOT NULL AS b0 \gset
SELECT pg_temp.mk_group('b1', 'c1') IS NOT NULL AS b1 \gset
SELECT pg_temp.mk_user('72', 'student', 'c1') IS NOT NULL AS s72 \gset
UPDATE public.users SET workgroup_id = pg_temp.u('b0') WHERE id = pg_temp.u('72');
SELECT pg_temp.give('f1', 'group', 'b0', 6) AS g_b0 \gset
SELECT pg_temp.give('f1', 'group', 'b1', 6) AS g_b1 \gset
UPDATE public.users SET workgroup_id = pg_temp.u('b1') WHERE id = pg_temp.u('72');
SELECT training_id AS m41 FROM public.start_training_safe(pg_temp.u('72'), 'u/m41', 'c052/m41', 'act', '{"steps":50000}'::jsonb, 50000, NULL, true) \gset
SELECT pg_temp.ok('PASS T41a the start reserves from the group the student is in now',
  (SELECT from_kind = 'group' AND from_id = pg_temp.u('b1') AND member_id = pg_temp.u('72') FROM public.credit_ledger
    WHERE kind = 'reserve' AND training_id = :m41)
  AND (SELECT workgroup_id = pg_temp.u('b1') FROM public.trainings WHERE id = :m41));
SELECT pg_temp.ok('PASS T41b holding the users row, a changed group gets its key share (the second read is final, no retry)',
  (SELECT prosrc ~ 'v_u\.workgroup_id IS NOT NULL AND v_u\.workgroup_id IS DISTINCT FROM v_wg'
          AND (length(prosrc) - length(replace(prosrc, 'FOR KEY SHARE', ''))) / length('FOR KEY SHARE') = 2
          AND prosrc !~ 'P0049'
     FROM pg_proc WHERE proname = '_credit_lock_user'));

\echo '=== T42 (fix round 2026-11-01) the S19 waiver needs proof of a fresh first attempt that never trained (P0073) ==='
SELECT pg_temp.mk_user('74', 'student', 'c1') IS NOT NULL AS s74 \gset
SELECT pg_temp.give('f1', 'student', '74', 60) AS g74 \gset
-- a host that skipped POST /start: resume_from_step stays NULL
SELECT pg_temp.run('74', 'c052/w-nostart', 1200) AS wn \gset
SELECT COALESCE(worker_token::text, '') AS wn_tok, train_seconds_used AS wn_secs FROM public.trainings WHERE id = :wn \gset
SELECT pg_temp.err(format($q$SELECT public.finish_training(%s, 1, 'failed', 'S19', NULL, NULL, NULL, NULL, 'lerobot_error')$q$, :wn),
  'P0073', 'PASS T42a a waiver for an attempt that never ran /start (resume_from_step NULL) answers P0073');
SELECT pg_temp.ok('PASS T42b the refusal wrote nothing: the row runs with its token, unsettled, no waiver; the attempt runs',
  (SELECT status = 'running' AND COALESCE(worker_token::text, '') = :'wn_tok' AND :'wn_tok' <> '' AND charge_waiver IS NULL
          AND charged_hours IS NULL AND settled_at IS NULL AND terminated_at IS NULL AND train_seconds_used = :wn_secs
     FROM public.trainings WHERE id = :wn)
  AND (SELECT state = 'running' AND ended_at IS NULL FROM public.training_attempts WHERE training_id = :wn AND attempt_no = 1)
  AND pg_temp.led(:wn) = 'reserve:3', pg_temp.led(:wn));
SELECT public.finish_training(:wn, 1, 'failed', 'S19', NULL);
SELECT pg_temp.ok('PASS T42c the same finish without the waiver settles and charges 1 (1 200 s)',
  (SELECT status = 'failed' AND charged_hours = 1 AND charge_waiver IS NULL FROM public.trainings WHERE id = :wn)
  AND pg_temp.led(:wn) = 'reserve:3,charge:1,release:2', pg_temp.led(:wn));
-- the current attempt holds a ready checkpoint of its own (it did train)
SELECT pg_temp.run('74', 'c052/w-ownckpt', 900) AS wo \gset
UPDATE public.training_attempts SET resume_from_step = 0 WHERE training_id = :wo AND attempt_no = 1;
SELECT pg_temp.ckpt(:wo, 1, 2500, 0);
SELECT pg_temp.err(format($q$SELECT public.finish_training(%s, 1, 'failed', 'S19', NULL, NULL, NULL, NULL, 'lerobot_error')$q$, :wo),
  'P0073', 'PASS T42d a waiver while the training holds a ready checkpoint answers P0073');
SELECT public.finish_training(:wo, 1, 'failed', 'S19', NULL);
-- an EARLIER attempt left a ready checkpoint and no counted second; the next attempt says resume 0
SELECT pg_temp.run('74', 'c052/w-oldckpt', 600) AS wk \gset
SELECT pg_temp.ckpt(:wk, 1, 2500, 300);
SELECT public.requeue_training(:wk, 1, 'pod_gone', NULL, 2500, 0);
SELECT pg_temp.attempt(:wk, 300) AS wk2 \gset
UPDATE public.training_attempts SET resume_from_step = 0 WHERE training_id = :wk AND attempt_no = 2;
SELECT pg_temp.ok('PASS T42e (fixture) the earlier attempt counted no second, its checkpoint is ready',
  (SELECT train_seconds_used = 0 FROM public.trainings WHERE id = :wk)
  AND EXISTS (SELECT 1 FROM public.training_checkpoints WHERE training_id = :wk AND attempt_no = 1 AND state = 'ready'));
SELECT pg_temp.err(format($q$SELECT public.finish_training(%s, 2, 'failed', 'S19', NULL, NULL, NULL, NULL, 'lerobot_error')$q$, :wk),
  'P0073', 'PASS T42f a waiver after an earlier attempt''s ready checkpoint answers P0073');
SELECT public.finish_training(:wk, 2, 'failed', 'S19', NULL);
-- the verifier's case: an earlier honest attempt's counted hours, its checkpoint later refused,
-- then a fresh attempt (resume 0, as /start writes when nothing is ready) that crashes
SELECT pg_temp.run('74', 'c052/w-counted', 4200) AS wc \gset
SELECT pg_temp.ckpt(:wc, 1, 2500, 300);
SELECT public.requeue_training(:wc, 1, 'pod_gone', NULL, 2500, 4200);
UPDATE public.training_checkpoints SET state = 'refused' WHERE training_id = :wc;
SELECT pg_temp.attempt(:wc, 600) AS wc2 \gset
UPDATE public.training_attempts SET resume_from_step = 0 WHERE training_id = :wc AND attempt_no = 2;
SELECT pg_temp.ok('PASS T42g (fixture) the earlier attempt counted 3 900 s; no checkpoint is ready',
  (SELECT train_seconds_used BETWEEN 3895 AND 3905 FROM public.trainings WHERE id = :wc)
  AND NOT EXISTS (SELECT 1 FROM public.training_checkpoints WHERE training_id = :wc AND state = 'ready'),
  (SELECT train_seconds_used::text FROM public.trainings WHERE id = :wc));
SELECT pg_temp.err(format($q$SELECT public.finish_training(%s, 2, 'failed', 'S19', NULL, NULL, NULL, NULL, 'lerobot_error')$q$, :wc),
  'P0073', 'PASS T42h a waiver after earlier counted training seconds answers P0073');
SELECT public.finish_training(:wc, 2, 'failed', 'S19', NULL);
SELECT pg_temp.ok('PASS T42i ... and finished without it, the honest attempt''s hours are charged (2 of 3)',
  (SELECT charged_hours = 2 AND charge_waiver IS NULL FROM public.trainings WHERE id = :wc), pg_temp.led(:wc));
-- a continuation (continued_from set), everything else fresh
INSERT INTO public.trainings (user_id, status, dataset_name, model_name, model_type, training_params, total_steps, managed,
                              credit_scheme, capped_step, continuable_until)
VALUES (pg_temp.u('74'), 'succeeded', 'u/p', 'c052/w-parent', 'act', '{"steps":50000}', 50000, true, 'derived', 2500,
        now() + interval '7 days');
INSERT INTO public.training_kept_checkpoints (training_id, step, manifest, expires_at)
VALUES (pg_temp.tid('c052/w-parent'), 2500, '{}', now() + interval '7 days');
SELECT o_training_id AS wcont FROM public.start_continuation_safe(pg_temp.u('74'), pg_temp.tid('c052/w-parent'), 'S28', 3) \gset
SELECT pg_temp.attempt(:wcont, 300) AS wcont1 \gset
UPDATE public.training_attempts SET resume_from_step = 0 WHERE training_id = :wcont AND attempt_no = 1;
SELECT pg_temp.err(format($q$SELECT public.finish_training(%s, 1, 'failed', 'S19', NULL, NULL, NULL, NULL, 'lerobot_error')$q$, :wcont),
  'P0073', 'PASS T42j a waiver on a continuation answers P0073');
SELECT public.finish_training(:wcont, 1, 'failed', 'S19', NULL);
-- /start ran (resume 0) but the attempt never reached its first running write
SELECT s.training_id AS wst FROM public.start_training_safe(pg_temp.u('74'), 'u/ds', 'c052/w-starting', 'act',
  '{"steps":50000,"save_freq":2500}'::jsonb, 50000, NULL, true, NULL, 3) s \gset
SELECT o_attempt_no AS wst1 FROM public.claim_training_attempt(:wst, 0) \gset
SELECT public.attach_training_attempt(:wst, 1, 'runpod_secure', 'pod-wst-1');
UPDATE public.training_attempts SET resume_from_step = 0 WHERE training_id = :wst AND attempt_no = 1;
SELECT pg_temp.err(format($q$SELECT public.finish_training(%s, 1, 'failed', 'S19', NULL, NULL, NULL, NULL, 'lerobot_error')$q$, :wst),
  'P0073', 'PASS T42k a waiver for an attempt that is still starting answers P0073');
SELECT public.finish_training(:wst, 1, 'failed', 'S19', NULL);
-- the honest fresh S19: /start wrote resume 0, the first running write came, LeRobot crashed before a step
SELECT pg_temp.run('74', 'c052/w-fresh', 900) AS wf \gset
UPDATE public.training_attempts SET resume_from_step = 0 WHERE training_id = :wf AND attempt_no = 1;
SELECT public.finish_training(:wf, 1, 'failed', 'S19', NULL, NULL, NULL, NULL, 'lerobot_error');
SELECT pg_temp.ok('PASS T42l the honest fresh S19 still gets the waiver: charge 0, release 3, the waiver recorded',
  (SELECT status = 'failed' AND charged_hours = 0 AND charge_waiver = 'lerobot_error' FROM public.trainings WHERE id = :wf)
  AND pg_temp.led(:wf) = 'reserve:3,charge:0,release:3', pg_temp.led(:wf));
SELECT pg_temp.err(format($q$SELECT public.finish_training(%s, 1, 'failed', 'S19', NULL, NULL, NULL, NULL, 'lerobot_error')$q$, :wf),
  'P0049', 'PASS T42m a waiver for a row that is already finished keeps its P0049 (a lost race, not a refusal)');

\echo '=== T43 (fix round 2026-11-01) the stall clock moves on the first running write and on a rising step only ==='
SELECT pg_temp.mk_user('75', 'student', 'c1') IS NOT NULL AS s75 \gset
SELECT pg_temp.give('f1', 'student', '75', 30) AS g75 \gset
SELECT s.training_id AS sc FROM public.start_training_safe(pg_temp.u('75'), 'u/sc', 'c052/stall', 'act',
  '{"steps":50000,"save_freq":2500}'::jsonb, 50000, NULL, true, NULL, 3) s \gset
SELECT o_worker_token AS sc_tok FROM public.claim_training_attempt(:sc, 0) \gset
SELECT public.attach_training_attempt(:sc, 1, 'runpod_secure', 'pod-sc-1');
UPDATE public.training_attempts SET resume_from_step = 0 WHERE training_id = :sc AND attempt_no = 1;
SELECT pg_temp.ok('PASS T43a (fixture) a placed attempt: the row queued, no stall clock yet',
  (SELECT status = 'queued' AND step_advanced_at IS NULL FROM public.trainings WHERE id = :sc));
SELECT public.update_training_progress(:sc, :'sc_tok'::uuid, 'running', 0);
SELECT pg_temp.ok('PASS T43b the first running write starts the clock, the row runs, the attempt runs with its first heartbeat',
  (SELECT status = 'running' AND step_advanced_at > now() - interval '5 seconds' FROM public.trainings WHERE id = :sc)
  AND (SELECT state = 'running' AND first_heartbeat_at IS NOT NULL FROM public.training_attempts
        WHERE training_id = :sc AND attempt_no = 1));
UPDATE public.trainings SET step_advanced_at = now() - interval '20 minutes', last_progress_at = now() - interval '1 minute'
 WHERE id = :sc;
SELECT public.update_training_progress(:sc, :'sc_tok'::uuid, 'running', 0);
SELECT public.update_training_progress(:sc, :'sc_tok'::uuid, 'running', 0, NULL, 1.5);
SELECT pg_temp.ok('PASS T43c an idle running write (status running, same step) moves last_progress_at, never the stall clock',
  (SELECT step_advanced_at BETWEEN now() - interval '21 minutes' AND now() - interval '19 minutes'
          AND last_progress_at > now() - interval '5 seconds' FROM public.trainings WHERE id = :sc),
  (SELECT (now() - step_advanced_at)::text FROM public.trainings WHERE id = :sc));
SELECT public.update_training_progress(:sc, :'sc_tok'::uuid, NULL, 0);
SELECT pg_temp.ok('PASS T43d a step-only write with the same step does not move it either',
  (SELECT step_advanced_at < now() - interval '19 minutes' FROM public.trainings WHERE id = :sc));
SELECT public.update_training_progress(:sc, :'sc_tok'::uuid, NULL, 100, NULL, 2.0);
SELECT pg_temp.ok('PASS T43e a rising step moves it',
  (SELECT step_advanced_at > now() - interval '5 seconds' AND current_step = 100 FROM public.trainings WHERE id = :sc));
-- the honest worker: 'running' once (done above), then rising steps and same-step heartbeats; before
-- every write the clock is pushed to 24 min ago (just under the sweep's STALL_S = 25 min)
DO $$
DECLARE v_id int; v_tok uuid; s int := 100; i int; v_age interval; v_worst interval := '0'; v_status text;
BEGIN
  SELECT id, worker_token INTO v_id, v_tok FROM public.trainings WHERE model_name = 'c052/stall';
  FOR i IN 1..40 LOOP
    UPDATE public.trainings SET step_advanced_at = now() - interval '24 minutes' WHERE id = v_id;
    IF i % 4 = 0 THEN
      PERFORM public.update_training_progress(v_id, v_tok, NULL, s);            -- a heartbeat: the last step again
      SELECT now() - step_advanced_at INTO v_age FROM public.trainings WHERE id = v_id;
      IF v_age <> interval '24 minutes' THEN RAISE NOTICE 'FAIL T43f a heartbeat moved the clock (%)', v_age; RETURN; END IF;
      UPDATE public.trainings SET step_advanced_at = now() WHERE id = v_id;    -- the last rise was just now
    ELSE
      s := s + 100;
      PERFORM public.update_training_progress(v_id, v_tok, NULL, s, NULL, 1.0 / i);
      SELECT now() - step_advanced_at INTO v_age FROM public.trainings WHERE id = v_id;
      IF v_age > v_worst THEN v_worst := v_age; END IF;
    END IF;
  END LOOP;
  SELECT status INTO v_status FROM public.trainings WHERE id = v_id;
  IF v_worst = interval '0' AND v_status = 'running' THEN
    RAISE NOTICE 'PASS T43f an honest sequence (30 rising steps, 10 heartbeats) never leaves the clock older than its last rise';
  ELSE
    RAISE NOTICE 'FAIL T43f the clock lagged % behind a rise (status %)', v_worst, v_status;
  END IF;
END $$;
-- a resume: requeue clears the clock, the next attempt's first running write starts it again
SELECT public.requeue_training(:sc, 1, 'pod_gone', NULL, 0, 0);
SELECT pg_temp.ok('PASS T43g a requeue clears the clock (the row queued)',
  (SELECT status = 'queued' AND step_advanced_at IS NULL FROM public.trainings WHERE id = :sc));
SELECT pg_temp.err(format($q$SELECT public.update_training_progress(%s, %L::uuid, 'running', 0)$q$, :sc, :'sc_tok'),
  'P0001', 'PASS T43h the old host''s token is fenced (P0001)');
SELECT o_worker_token AS sc_tok2 FROM public.claim_training_attempt(:sc, 1) \gset
SELECT public.attach_training_attempt(:sc, 2, 'runpod_secure', 'pod-sc-2');
SELECT public.update_training_progress(:sc, :'sc_tok2'::uuid, 'running', 0);
SELECT pg_temp.ok('PASS T43i the resumed attempt''s first running write starts the clock again',
  (SELECT status = 'running' AND step_advanced_at > now() - interval '5 seconds' FROM public.trainings WHERE id = :sc));
-- the queued branch alone: a clock already set on a queued row is restarted by the first running write
SELECT public.requeue_training(:sc, 2, 'pod_gone', NULL, 0, 0);
SELECT o_worker_token AS sc_tok3 FROM public.claim_training_attempt(:sc, 2) \gset
SELECT public.attach_training_attempt(:sc, 3, 'runpod_secure', 'pod-sc-3');
UPDATE public.trainings SET step_advanced_at = now() - interval '2 hours' WHERE id = :sc;
SELECT public.update_training_progress(:sc, :'sc_tok3'::uuid, 'running', 0);
SELECT pg_temp.ok('PASS T43j the first running write of a queued row restarts a clock that was not NULL',
  (SELECT step_advanced_at > now() - interval '5 seconds' FROM public.trainings WHERE id = :sc));
SELECT public.finish_training(:sc, 3, 'failed', 'x', NULL);
-- a legacy (unmanaged, derived) row: its stall rule reads last_progress_at, which every write still moves
INSERT INTO public.trainings (user_id, status, dataset_name, model_name, model_type, training_params, total_steps, managed,
                              credit_scheme, worker_token)
VALUES (pg_temp.u('75'), 'queued', 'u/l', 'c052/stall-legacy', 'diffusion', '{"steps":1000}', 1000, false, 'derived',
        gen_random_uuid());
SELECT id AS lg, worker_token AS lg_tok FROM public.trainings WHERE model_name = 'c052/stall-legacy' \gset
SELECT public.update_training_progress(:lg, :'lg_tok'::uuid, 'running', 0, 1000);
SELECT pg_temp.ok('PASS T43k a legacy row: the running write runs it and starts both clocks',
  (SELECT status = 'running' AND step_advanced_at > now() - interval '5 seconds' AND last_progress_at > now() - interval '5 seconds'
     FROM public.trainings WHERE id = :lg));
UPDATE public.trainings SET last_progress_at = now() - interval '30 minutes', step_advanced_at = now() - interval '30 minutes'
 WHERE id = :lg;
SELECT public.update_training_progress(:lg, :'lg_tok'::uuid, 'running', 0, 1000);
SELECT pg_temp.ok('PASS T43l a legacy row''s repeated running write still moves last_progress_at (what its stall rule reads)',
  (SELECT last_progress_at > now() - interval '5 seconds' AND step_advanced_at < now() - interval '29 minutes'
     FROM public.trainings WHERE id = :lg));
SELECT public.update_training_progress(:lg, :'lg_tok'::uuid, 'succeeded', 1000, 1000, NULL, NULL, 'https://x.test/log');
SELECT pg_temp.ok('PASS T43m a legacy row''s terminal write still ends it (status, token, log url)',
  (SELECT status = 'succeeded' AND worker_token IS NULL AND log_url = 'https://x.test/log' FROM public.trainings WHERE id = :lg));
SELECT pg_temp.ok('PASS T43n update_training_progress: one overload, DEFINER, the 052 clock line, EXECUTE for anon and authenticated, not PUBLIC',
  (SELECT count(*) = 1 FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND proname = 'update_training_progress')
  AND (SELECT prosecdef AND strpos(prosrc, 'OR (p_status = ''running'' AND status = ''queued'') THEN NOW()') > 0
              AND strpos(prosrc, 'WHEN step_advanced_at IS NULL OR p_status = ''running'' THEN NOW()') = 0
         FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND proname = 'update_training_progress')
  AND has_function_privilege('anon', 'public.update_training_progress(integer,uuid,text,integer,integer,real,text,text)', 'EXECUTE')
  AND has_function_privilege('authenticated', 'public.update_training_progress(integer,uuid,text,integer,integer,real,text,text)', 'EXECUTE')
  AND NOT has_function_privilege('public', 'public.update_training_progress(integer,uuid,text,integer,integer,real,text,text)', 'EXECUTE'));

\echo '=== T44 (fix round 2, V3-M1) mark_checkpoint_ready: the one ready writer, decided under the trainings row lock ==='
SELECT pg_temp.ok('PASS T44a one overload (INT, INT, INT) RETURNS boolean, SECURITY INVOKER, search_path public; EXECUTE for service_role only',
  (SELECT count(*) = 1 FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND proname = 'mark_checkpoint_ready')
  AND (SELECT NOT prosecdef AND proconfig = '{search_path=public}' AND prorettype = 'boolean'::regtype
              AND pg_get_function_identity_arguments(oid) = 'p_training_id integer, p_attempt integer, p_step integer'
         FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND proname = 'mark_checkpoint_ready')
  AND has_function_privilege('service_role', 'public.mark_checkpoint_ready(integer,integer,integer)', 'EXECUTE')
  AND NOT has_function_privilege('anon', 'public.mark_checkpoint_ready(integer,integer,integer)', 'EXECUTE')
  AND NOT has_function_privilege('authenticated', 'public.mark_checkpoint_ready(integer,integer,integer)', 'EXECUTE')
  AND NOT has_function_privilege('public', 'public.mark_checkpoint_ready(integer,integer,integer)', 'EXECUTE'));
SELECT pg_temp.mk_user('76', 'student', 'c1') IS NOT NULL AS s76 \gset
SELECT pg_temp.give('f1', 'student', '76', 30) AS g76 \gset
SELECT pg_temp.run('76', 'c052/ck-live', 1200) AS ck \gset
SELECT pg_temp.ckpt_up(:ck, 1, 2500, 300);
SELECT pg_temp.ok('PASS T44b the live running attempt''s uploading checkpoint: true, ready, ready_at set',
  public.mark_checkpoint_ready(:ck, 1, 2500));
SELECT pg_temp.ok('PASS T44c ... it is ready with ready_at now',
  (SELECT state = 'ready' AND ready_at > now() - interval '5 seconds' FROM public.training_checkpoints
    WHERE training_id = :ck AND attempt_no = 1 AND step = 2500));
UPDATE public.training_checkpoints SET ready_at = now() - interval '1 hour' WHERE training_id = :ck AND step = 2500;
SELECT pg_temp.ok('PASS T44d a repeated ckpt-done of a ready checkpoint of the live attempt: true, ready_at kept',
  public.mark_checkpoint_ready(:ck, 1, 2500));
SELECT pg_temp.ok('PASS T44e ... ready_at was not touched',
  (SELECT ready_at < now() - interval '59 minutes' FROM public.training_checkpoints WHERE training_id = :ck AND step = 2500));
SELECT pg_temp.ckpt_up(:ck, 1, 5000, 0);
UPDATE public.training_checkpoints SET state = 'refused' WHERE training_id = :ck AND step = 5000;
SELECT pg_temp.ok('PASS T44f a refused checkpoint: false, still refused',
  NOT public.mark_checkpoint_ready(:ck, 1, 5000)
  AND (SELECT state = 'refused' FROM public.training_checkpoints WHERE training_id = :ck AND step = 5000));
SELECT pg_temp.ok('PASS T44g an absent checkpoint, an unknown training, another attempt number: false',
  NOT public.mark_checkpoint_ready(:ck, 1, 7500) AND NOT public.mark_checkpoint_ready(-1, 1, 2500)
  AND NOT public.mark_checkpoint_ready(:ck, 2, 2500));
SELECT pg_temp.ckpt_up(:ck, 1, 7500, 0);
SELECT public.requeue_training(:ck, 1, 'pod_gone', NULL, 2500, 1200);
SELECT pg_temp.ok('PASS T44h after the attempt was lost (row queued) its held-back checkpoint is refused the ready state',
  NOT public.mark_checkpoint_ready(:ck, 1, 7500)
  AND (SELECT state = 'uploading' FROM public.training_checkpoints WHERE training_id = :ck AND step = 7500));
SELECT pg_temp.attempt(:ck, 60) AS ck2 \gset
SELECT pg_temp.ok('PASS T44i ... and the next attempt runs: attempt 1''s checkpoint is still refused (another attempt is current)',
  NOT public.mark_checkpoint_ready(:ck, 1, 7500)
  AND (SELECT state = 'uploading' FROM public.training_checkpoints WHERE training_id = :ck AND step = 7500));
SELECT public.finish_training(:ck, 2, 'failed', 'x', NULL);
SELECT pg_temp.ok('PASS T44j a finished training: false',
  NOT public.mark_checkpoint_ready(:ck, 2, 2500));
-- a row being stopped (cancel_requested) while its attempt still runs
SELECT pg_temp.run('76', 'c052/ck-stop', 600) AS cs \gset
SELECT pg_temp.ckpt_up(:cs, 1, 2500, 0);
SELECT o_outcome AS cs_out FROM public.stop_training(:cs, pg_temp.u('76'), pg_temp.u('76'), 'student', NULL) \gset
SELECT pg_temp.ok('PASS T44k a cancel_requested row (attempt still running): false',
  :'cs_out' = 'cancel' AND NOT public.mark_checkpoint_ready(:cs, 1, 2500)
  AND (SELECT status = 'cancel_requested' FROM public.trainings WHERE id = :cs)
  AND (SELECT state = 'running' FROM public.training_attempts WHERE training_id = :cs AND attempt_no = 1), :'cs_out');
SELECT public.complete_cancel_training(:cs, 'canceled', NULL);
-- an attempt that is still starting (no first running write)
SELECT s.training_id AS cst FROM public.start_training_safe(pg_temp.u('76'), 'u/ds', 'c052/ck-starting', 'act',
  '{"steps":50000,"save_freq":2500}'::jsonb, 50000, NULL, true, NULL, 1) s \gset
SELECT o_attempt_no AS cst1 FROM public.claim_training_attempt(:cst, 0) \gset
SELECT public.attach_training_attempt(:cst, 1, 'runpod_secure', 'pod-cst-1');
SELECT pg_temp.ckpt_up(:cst, 1, 2500, 0);
SELECT pg_temp.ok('PASS T44l an attempt that is still starting: false', NOT public.mark_checkpoint_ready(:cst, 1, 2500));
SELECT public.finish_training(:cst, 1, 'failed', 'x', NULL);

-- the races (spec V3-M1), with a SECOND REAL CONNECTION through dblink: this session holds
-- one transaction open while the other connection's call runs and waits on the trainings row
\if :{?race_host}
\else
\set race_host db
\endif
\if :{?race_password}
\else
\set race_password postgres
\endif
SELECT NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'dblink') AS dblink_new \gset
CREATE EXTENSION IF NOT EXISTS dblink WITH SCHEMA extensions;
SELECT extensions.dblink_connect('c052b', format('host=%s port=5432 dbname=postgres user=postgres password=%s',
                                                 :'race_host', :'race_password')) AS b_conn \gset
SELECT pid AS bpid FROM extensions.dblink('c052b', 'SELECT pg_backend_pid()') AS t(pid int) \gset
SELECT pg_temp.ok('PASS T44m (fixture) a second connection, another backend', :bpid <> pg_backend_pid(), :'b_conn');
-- R-A: the requeue holds the row; the held-back ckpt-done waits, then is refused
SELECT pg_temp.run('76', 'c052/race-ckA', 3000) AS ra \gset
SELECT pg_temp.ckpt_up(:ra, 1, 2500, 600);
BEGIN;
SELECT public.requeue_training(:ra, 1, 'heartbeat_lost', NULL, 0, 3000);
SELECT extensions.dblink_send_query('c052b', format('SELECT public.mark_checkpoint_ready(%s, 1, 2500)', :ra)) AS ra_sent \gset
SELECT pg_temp.blocked_by_me(:bpid) AS ra_waits \gset
SELECT state AS ra_seen_by_b FROM public.training_checkpoints WHERE training_id = :ra AND step = 2500 \gset
COMMIT;
SELECT r AS ra_result FROM extensions.dblink_get_result('c052b') AS t(r boolean) \gset
SELECT count(*) AS ra_clear FROM extensions.dblink_get_result('c052b') AS t(r boolean) \gset
SELECT pg_temp.ok('PASS T44n R-A: with requeue_training''s transaction open, mark_checkpoint_ready on the other connection WAITS',
  :'ra_waits'::boolean AND :ra_sent = 1);
SELECT pg_temp.ok('PASS T44o R-A: after the commit it returns false; the checkpoint stays uploading; E50 charged the lost attempt 0',
  NOT :'ra_result'::boolean
  AND (SELECT state = 'uploading' FROM public.training_checkpoints WHERE training_id = :ra AND step = 2500)
  AND (SELECT state = 'lost' FROM public.training_attempts WHERE training_id = :ra AND attempt_no = 1)
  AND (SELECT status = 'queued' AND train_seconds_used = 0 FROM public.trainings WHERE id = :ra),
  :'ra_result' || ' ' || (SELECT train_seconds_used::text FROM public.trainings WHERE id = :ra));
UPDATE public.trainings SET status = 'canceled' WHERE id = :ra;
-- R-B: the reverse order. mark_checkpoint_ready holds the row; the requeue waits and then
-- charges per E50 with the checkpoint ready (created 600 s ago, first running write 3 000 s ago)
SELECT pg_temp.run('76', 'c052/race-ckB', 3000) AS rb \gset
SELECT pg_temp.ckpt_up(:rb, 1, 2500, 600);
BEGIN;
SELECT public.mark_checkpoint_ready(:rb, 1, 2500) AS rb_mark \gset
SELECT extensions.dblink_send_query('c052b',
  format('SELECT public.requeue_training(%s, 1, %L, NULL, 2500, 3000)::text', :rb, 'heartbeat_lost')) AS rb_sent \gset
SELECT pg_temp.blocked_by_me(:bpid) AS rb_waits \gset
COMMIT;
SELECT count(*) AS rb_rows FROM extensions.dblink_get_result('c052b') AS t(r text) \gset
SELECT count(*) AS rb_clear FROM extensions.dblink_get_result('c052b') AS t(r text) \gset
SELECT pg_temp.ok('PASS T44p R-B: with mark_checkpoint_ready''s transaction open, requeue_training on the other connection WAITS',
  :'rb_mark'::boolean AND :'rb_waits'::boolean AND :rb_sent = 1);
SELECT pg_temp.ok('PASS T44q R-B: after the commit the requeue charges per E50 up to the ready checkpoint (2 400 s), resume 2 500',
  :rb_rows = 1
  AND (SELECT state = 'ready' FROM public.training_checkpoints WHERE training_id = :rb AND step = 2500)
  AND (SELECT status = 'queued' AND current_step = 2500 AND train_seconds_used BETWEEN 2395 AND 2405
         FROM public.trainings WHERE id = :rb),
  (SELECT train_seconds_used::text FROM public.trainings WHERE id = :rb));
UPDATE public.trainings SET status = 'canceled' WHERE id = :rb;
-- R-C: finish_training with the waiver holds the row; the held-back ckpt-done waits, then is refused
SELECT pg_temp.run('76', 'c052/race-ckC', 900) AS rc \gset
UPDATE public.training_attempts SET resume_from_step = 0 WHERE training_id = :rc AND attempt_no = 1;
SELECT pg_temp.ckpt_up(:rc, 1, 2500, 60);
BEGIN;
SELECT public.finish_training(:rc, 1, 'failed', 'S19', NULL, NULL, NULL, NULL, 'lerobot_error');
SELECT extensions.dblink_send_query('c052b', format('SELECT public.mark_checkpoint_ready(%s, 1, 2500)', :rc)) AS rc_sent \gset
SELECT pg_temp.blocked_by_me(:bpid) AS rc_waits \gset
COMMIT;
SELECT r AS rc_result FROM extensions.dblink_get_result('c052b') AS t(r boolean) \gset
SELECT count(*) AS rc_clear FROM extensions.dblink_get_result('c052b') AS t(r boolean) \gset
SELECT pg_temp.ok('PASS T44r R-C: the waived finish holds the row, mark_checkpoint_ready waits and returns false; the waiver stands, the checkpoint stays uploading',
  :'rc_waits'::boolean AND NOT :'rc_result'::boolean
  AND (SELECT status = 'failed' AND charge_waiver = 'lerobot_error' AND charged_hours = 0 FROM public.trainings WHERE id = :rc)
  AND (SELECT state = 'uploading' FROM public.training_checkpoints WHERE training_id = :rc AND step = 2500),
  :'rc_result');
-- R-D: the reverse order. mark_checkpoint_ready holds the row; the waived finish waits and is refused P0073
SELECT pg_temp.run('76', 'c052/race-ckD', 900) AS rd \gset
UPDATE public.training_attempts SET resume_from_step = 0 WHERE training_id = :rd AND attempt_no = 1;
SELECT pg_temp.ckpt_up(:rd, 1, 2500, 60);
BEGIN;
SELECT public.mark_checkpoint_ready(:rd, 1, 2500) AS rd_mark \gset
SELECT extensions.dblink_send_query('c052b',
  format('SELECT public.finish_training(%s, 1, %L, %L, NULL, NULL, NULL, NULL, %L)::text', :rd, 'failed', 'S19', 'lerobot_error'))
  AS rd_sent \gset
SELECT pg_temp.blocked_by_me(:bpid) AS rd_waits \gset
COMMIT;
SELECT count(*) AS rd_rows FROM extensions.dblink_get_result('c052b', false) AS t(r text) \gset
SELECT COALESCE(extensions.dblink_error_message('c052b'), '') AS rd_err \gset
SELECT count(*) AS rd_clear FROM extensions.dblink_get_result('c052b', false) AS t(r text) \gset
SELECT pg_temp.ok('PASS T44s R-D: mark_checkpoint_ready holds the row, the waived finish waits and is refused (charge waiver refused); the row runs on, the checkpoint is ready',
  :'rd_mark'::boolean AND :'rd_waits'::boolean AND :rd_rows = 0 AND strpos(:'rd_err', 'charge waiver refused') > 0
  AND (SELECT status = 'running' AND charge_waiver IS NULL AND settled_at IS NULL FROM public.trainings WHERE id = :rd)
  AND (SELECT state = 'ready' FROM public.training_checkpoints WHERE training_id = :rd AND step = 2500),
  :'rd_err');
SELECT public.finish_training(:rd, 1, 'failed', 'S19', NULL);
SELECT extensions.dblink_disconnect('c052b');
\if :dblink_new
DROP EXTENSION dblink;
\endif

\echo '=== T45 (fix round 2) claim_checkpoint_mirror refuses a training that is not queued, running or succeeded ==='
SELECT pg_temp.mk_user('77', 'student', 'c1') IS NOT NULL AS s77 \gset
SELECT pg_temp.give('f1', 'student', '77', 30) AS g77 \gset
SELECT pg_temp.run('77', 'c052/mir-run', 600) AS mr \gset
SELECT pg_temp.ckpt(:mr, 1, 10000, 0);
SELECT pg_temp.ok('PASS T45a a running row''s ready checkpoint is claimed', public.claim_checkpoint_mirror(:mr, 1, 10000, 600, 'c052m'));
SELECT pg_temp.ok('PASS T45b ... and its lease renews', public.claim_checkpoint_mirror(:mr, 1, 10000, 600, 'c052m'));
SELECT public.finish_training(:mr, 1, 'failed', 'x', NULL);
SELECT pg_temp.ok('PASS T45c once the row failed, the renewal answers false (the mirror stops)',
  NOT public.claim_checkpoint_mirror(:mr, 1, 10000, 600, 'c052m'));
SELECT pg_temp.run('77', 'c052/mir-queued', 900) AS mq \gset
SELECT pg_temp.ckpt(:mq, 1, 10000, 300);
SELECT public.requeue_training(:mq, 1, 'pod_gone', NULL, 10000, 900);
SELECT pg_temp.ok('PASS T45d a row between attempts (queued) is claimed',
  (SELECT status = 'queued' FROM public.trainings WHERE id = :mq) AND public.claim_checkpoint_mirror(:mq, 1, 10000, 600, 'c052m'));
UPDATE public.trainings SET status = 'canceled' WHERE id = :mq;
SELECT pg_temp.ok('PASS T45e a canceled row is refused', NOT public.claim_checkpoint_mirror(:mq, 1, 10000, 600, 'c052m'));
SELECT pg_temp.run('77', 'c052/mir-cap', 900) AS mc \gset
SELECT pg_temp.ckpt(:mc, 1, 10000, 0);
SELECT public.cap_training_attempt(:mc, 1, 10000, 'capped', 'S22', 900);
SELECT pg_temp.ok('PASS T45f a capped row publishing (running, attempt ended capped) is claimed',
  (SELECT t.status = 'running' AND a.state = 'ended' AND a.end_reason = 'capped' FROM public.trainings t
     JOIN public.training_attempts a ON a.training_id = t.id AND a.attempt_no = 1 WHERE t.id = :mc)
  AND public.claim_checkpoint_mirror(:mc, 1, 10000, 600, 'c052m'));
SELECT public.finish_training(:mc, 1, 'succeeded', NULL, NULL, 'S23', 10000, now() + interval '7 days');
SELECT pg_temp.ok('PASS T45g ... and after it ended succeeded (capped, continuable) its lease still renews',
  (SELECT status = 'succeeded' AND continuable_until IS NOT NULL FROM public.trainings WHERE id = :mc)
  AND public.claim_checkpoint_mirror(:mc, 1, 10000, 600, 'c052m'));
SELECT pg_temp.run('77', 'c052/mir-stop', 600) AS ms \gset
SELECT pg_temp.ckpt(:ms, 1, 10000, 0);
UPDATE public.trainings SET status = 'cancel_requested' WHERE id = :ms;
SELECT pg_temp.ok('PASS T45h a cancel_requested row is refused', NOT public.claim_checkpoint_mirror(:ms, 1, 10000, 600, 'c052m'));
UPDATE public.trainings SET status = 'canceled' WHERE id = :ms;
SELECT pg_temp.ok('PASS T45i claim_checkpoint_mirror: one overload, INVOKER, the status condition, EXECUTE for service_role only',
  (SELECT count(*) = 1 FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND proname = 'claim_checkpoint_mirror')
  AND (SELECT NOT prosecdef AND strpos(prosrc, 'AND t.status IN (''queued'',''running'',''succeeded'')') > 0
         FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND proname = 'claim_checkpoint_mirror')
  AND has_function_privilege('service_role', 'public.claim_checkpoint_mirror(integer,integer,integer,integer,text)', 'EXECUTE')
  AND NOT has_function_privilege('anon', 'public.claim_checkpoint_mirror(integer,integer,integer,integer,text)', 'EXECUTE')
  AND NOT has_function_privilege('authenticated', 'public.claim_checkpoint_mirror(integer,integer,integer,integer,text)', 'EXECUTE'));

\echo '=== T46 (fix round 2) the waiver needs current_step 0; the honest sequence keeps it 0 ==='
SELECT pg_temp.mk_user('78', 'student', 'c1') IS NOT NULL AS s78 \gset
SELECT pg_temp.give('f1', 'student', '78', 15) AS g78 \gset
-- the honest fresh S19, through the worker's own RPC: /start (resume 0), the first running write with
-- current_step = resume_step = 0, no step write, then finish lerobot_error
SELECT s.training_id AS wa FROM public.start_training_safe(pg_temp.u('78'), 'u/ds', 'c052/step-fresh', 'act',
  '{"steps":50000,"save_freq":2500}'::jsonb, 50000, NULL, true, NULL, 3) s \gset
SELECT o_worker_token AS wa_tok FROM public.claim_training_attempt(:wa, 0) \gset
SELECT public.attach_training_attempt(:wa, 1, 'runpod_secure', 'pod-wa-1');
UPDATE public.training_attempts SET resume_from_step = 0 WHERE training_id = :wa AND attempt_no = 1;
SELECT public.update_training_progress(:wa, :'wa_tok'::uuid, 'running', 0);
SELECT pg_temp.ok('PASS T46a (fixture) after the first running write the row runs at current_step 0',
  (SELECT status = 'running' AND current_step = 0 FROM public.trainings WHERE id = :wa));
SELECT public.finish_training(:wa, 1, 'failed', 'S19', NULL, NULL, NULL, NULL, 'lerobot_error');
SELECT pg_temp.ok('PASS T46b the honest fresh S19 keeps the waiver',
  (SELECT status = 'failed' AND charge_waiver = 'lerobot_error' AND charged_hours = 0 FROM public.trainings WHERE id = :wa));
-- the same with one reported step
SELECT s.training_id AS wb FROM public.start_training_safe(pg_temp.u('78'), 'u/ds', 'c052/step-one', 'act',
  '{"steps":50000,"save_freq":2500}'::jsonb, 50000, NULL, true, NULL, 3) s \gset
SELECT o_worker_token AS wb_tok FROM public.claim_training_attempt(:wb, 0) \gset
SELECT public.attach_training_attempt(:wb, 1, 'runpod_secure', 'pod-wb-1');
UPDATE public.training_attempts SET resume_from_step = 0 WHERE training_id = :wb AND attempt_no = 1;
SELECT public.update_training_progress(:wb, :'wb_tok'::uuid, 'running', 0);
SELECT public.update_training_progress(:wb, :'wb_tok'::uuid, NULL, 100, NULL, 3.5);
SELECT pg_temp.err(format($q$SELECT public.finish_training(%s, 1, 'failed', 'S19', NULL, NULL, NULL, NULL, 'lerobot_error')$q$, :wb),
  'P0073', 'PASS T46c a waiver after a reported step (current_step 100) answers P0073');
SELECT public.finish_training(:wb, 1, 'failed', 'S19', NULL);
-- a lost attempt 1 that reported steps but saved nothing, then a fresh attempt 2 that crashes before a step
SELECT s.training_id AS wc FROM public.start_training_safe(pg_temp.u('78'), 'u/ds', 'c052/step-lost', 'act',
  '{"steps":50000,"save_freq":2500}'::jsonb, 50000, NULL, true, NULL, 3) s \gset
SELECT o_worker_token AS wc_tok FROM public.claim_training_attempt(:wc, 0) \gset
SELECT public.attach_training_attempt(:wc, 1, 'runpod_secure', 'pod-wc-1');
UPDATE public.training_attempts SET resume_from_step = 0 WHERE training_id = :wc AND attempt_no = 1;
SELECT public.update_training_progress(:wc, :'wc_tok'::uuid, 'running', 0);
SELECT public.update_training_progress(:wc, :'wc_tok'::uuid, NULL, 700, NULL, 2.5);
SELECT public.requeue_training(:wc, 1, 'pod_gone', NULL, 0, 600);
SELECT pg_temp.ok('PASS T46d the requeue (nothing ready, resume 0) puts current_step back to 0 and counts no second',
  (SELECT status = 'queued' AND current_step = 0 AND train_seconds_used = 0 FROM public.trainings WHERE id = :wc));
SELECT o_worker_token AS wc_tok2 FROM public.claim_training_attempt(:wc, 1) \gset
SELECT public.attach_training_attempt(:wc, 2, 'runpod_secure', 'pod-wc-2');
UPDATE public.training_attempts SET resume_from_step = 0 WHERE training_id = :wc AND attempt_no = 2;
SELECT public.update_training_progress(:wc, :'wc_tok2'::uuid, 'running', 0);
SELECT public.finish_training(:wc, 2, 'failed', 'S19', NULL, NULL, NULL, NULL, 'lerobot_error');
SELECT pg_temp.ok('PASS T46e ... attempt 2''s S19 keeps the waiver',
  (SELECT status = 'failed' AND charge_waiver = 'lerobot_error' AND charged_hours = 0 AND current_step = 0
     FROM public.trainings WHERE id = :wc));
-- and if a queued row still showed a step, the first running write itself brings it back to the resume step
SELECT s.training_id AS wd FROM public.start_training_safe(pg_temp.u('78'), 'u/ds', 'c052/step-down', 'act',
  '{"steps":50000,"save_freq":2500}'::jsonb, 50000, NULL, true, NULL, 3) s \gset
SELECT o_worker_token AS wd_tok FROM public.claim_training_attempt(:wd, 0) \gset
SELECT public.attach_training_attempt(:wd, 1, 'runpod_secure', 'pod-wd-1');
UPDATE public.trainings SET current_step = 700 WHERE id = :wd;
SELECT public.update_training_progress(:wd, :'wd_tok'::uuid, 'running', 0);
SELECT pg_temp.ok('PASS T46f the first running write with current_step = resume_step = 0 lowers a shown 700 to 0',
  (SELECT status = 'running' AND current_step = 0 FROM public.trainings WHERE id = :wd));
SELECT public.finish_training(:wd, 1, 'failed', 'x', NULL);

\echo '=== T29 / T30 the lock order of I9 (structure; the two-session races are the header''s manual steps) ==='
SELECT pg_temp.ok('PASS T29a the two closes run AFTER DELETE, the delete settlement BEFORE DELETE (row triggers)',
  (SELECT bool_and(CASE WHEN tgname = 'trg_9_trainings_credit_settle_delete' THEN tgtype & 2 = 2 ELSE tgtype & 2 = 0 END
                   AND tgtype & 1 = 1 AND tgtype & 8 = 8) AND count(*) = 3
     FROM pg_trigger WHERE tgname IN ('trg_9_users_credit_close','trg_9_workgroups_credit_close','trg_9_trainings_credit_settle_delete')));
SELECT pg_temp.ok('PASS T29b start_continuation_safe locks the users row (_credit_lock_user) before the parent training row',
  (SELECT position('_credit_lock_user' IN prosrc) > 0 AND position('_credit_lock_user' IN prosrc) < position('FOR UPDATE' IN prosrc)
     FROM pg_proc WHERE proname = 'start_continuation_safe'));
SELECT pg_temp.ok('PASS T30a _credit_lock_user takes the workgroups row FOR KEY SHARE before the users row FOR UPDATE',
  (SELECT position('FOR KEY SHARE' IN prosrc) > 0 AND position('FOR KEY SHARE' IN prosrc) < position('FOR UPDATE' IN prosrc)
     FROM pg_proc WHERE proname = '_credit_lock_user'));
SELECT pg_temp.ok('PASS T30b start_training_safe calls _credit_lock_user before it locks the funding account',
  (SELECT position('_credit_lock_user' IN prosrc) > 0 AND position('_credit_lock_user' IN prosrc) < position('_credit_startable' IN prosrc)
     FROM pg_proc WHERE proname = 'start_training_safe'));
SELECT pg_temp.ok('PASS T30c a compensation takes its target''s users/workgroups row FOR KEY SHARE only',
  (SELECT prosrc ~ 'FROM users u WHERE u\.id = p_target_id FOR KEY SHARE'
          AND prosrc !~ 'FROM (users|workgroups) [a-z]+ WHERE [^;]* FOR UPDATE'
     FROM pg_proc WHERE proname = 'credit_admin_compensate'));
SELECT pg_temp.mk_group('b2', 'c1') IS NOT NULL AS b2 \gset
SELECT pg_temp.mk_user('73', 'student', 'c1') IS NOT NULL AS s73 \gset
UPDATE public.users SET workgroup_id = pg_temp.u('b2') WHERE id = pg_temp.u('73');
SELECT pg_temp.give('f1', 'group', 'b2', 6) AS g_b2 \gset
DELETE FROM public.workgroups WHERE id = pg_temp.u('b2');
SELECT pg_temp.err($q$SELECT * FROM public.start_training_safe(pg_temp.u('73'), 'u/c', 'm', 'act', '{}'::jsonb, 1, NULL, true)$q$,
  'P0003', 'PASS T30d a member whose group was deleted first trains personally: no account, P0003, no group reservation');

\echo '=== T20 credit_reconcile is report-only ==='
SELECT count(*) AS n20 FROM public.credit_ledger \gset
SELECT pg_temp.ok('PASS T20a credit_reconcile() = 0/0/0/0', (SELECT row(r.*)::text = '(0,0,0,0)' FROM public.credit_reconcile() r),
  (SELECT row(r.*)::text FROM public.credit_reconcile() r));
SELECT pg_temp.ok('PASS T20b it is STABLE and wrote nothing',
  (SELECT provolatile = 's' FROM pg_proc WHERE proname = 'credit_reconcile') AND (SELECT count(*) FROM public.credit_ledger) = :n20);

\echo '=== T12 (I3, I5, I7, I10) no drift, every settlement complete, every hour created by an admin row ==='
SELECT pg_temp.ok('PASS T12a every account''s free / reserved / spent equals the ledger sums (§3.4, recomputed here)',
  NOT EXISTS (SELECT 1 FROM public.credit_accounts a WHERE
      a.free <> COALESCE((SELECT sum(hours) FROM public.credit_ledger WHERE to_kind = a.kind AND to_id = a.owner_id AND to_bucket = 'free'), 0)
               - COALESCE((SELECT sum(hours) FROM public.credit_ledger WHERE from_kind = a.kind AND from_id = a.owner_id AND from_bucket = 'free'), 0)
   OR a.reserved <> COALESCE((SELECT sum(hours) FROM public.credit_ledger WHERE to_kind = a.kind AND to_id = a.owner_id AND to_bucket = 'reserved'), 0)
               - COALESCE((SELECT sum(hours) FROM public.credit_ledger WHERE from_kind = a.kind AND from_id = a.owner_id AND from_bucket = 'reserved'), 0)
   OR a.spent <> COALESCE((SELECT sum(hours) FROM public.credit_ledger WHERE kind = 'charge' AND from_kind = a.kind AND from_id = a.owner_id), 0))
  AND (SELECT o_drift = 0 FROM public.credit_reconcile()));
SELECT pg_temp.ok('PASS T12b (I5, I7) every terminal ledger training has exactly one charge and one release, charge + release = reserve',
  NOT EXISTS (SELECT 1 FROM public.trainings t
               WHERE t.credit_scheme = 'ledger' AND t.status IN ('succeeded','failed','canceled')
                 AND (SELECT count(*) FILTER (WHERE kind = 'charge') <> 1 OR count(*) FILTER (WHERE kind = 'release') <> 1
                             OR COALESCE(sum(hours) FILTER (WHERE kind IN ('charge','release')), -1)
                                <> COALESCE(sum(hours) FILTER (WHERE kind = 'reserve'), -2)
                        FROM public.credit_ledger l WHERE l.training_id = t.id))
  AND NOT EXISTS (SELECT 1 FROM public.trainings t WHERE t.credit_scheme = 'ledger'
                   AND (SELECT count(*) FROM public.credit_ledger l WHERE l.training_id = t.id AND l.kind = 'reserve') <> 1));
SELECT pg_temp.ok('PASS T12c (I10) Σ grant + Σ compensate = Σ (free + reserved + spent) + Σ revoke', pg_temp.identity_ok(), pg_temp.identity());
SELECT pg_temp.ok('PASS T12d (I4) no account bucket is negative and no closed account holds free or reserved hours',
  NOT EXISTS (SELECT 1 FROM public.credit_accounts WHERE free < 0 OR reserved < 0 OR spent < 0
                 OR (closed_at IS NOT NULL AND (free <> 0 OR reserved <> 0))));

-- -------------------------------------------------------------------------------------
-- Fixtures for the header's two-session races (T29/T30/T41 by hand). Each pair of ids is
-- consumed by ONE race in ONE order.
-- -------------------------------------------------------------------------------------
DO $$
DECLARE n text; p int;
BEGIN
  -- R1/R2 a continuation vs its student's deletion (students 80, 81, personal, 9 h, a continuable parent each)
  FOREACH n IN ARRAY ARRAY['80','81'] LOOP
    PERFORM pg_temp.mk_user(n, 'student', 'c1');
    PERFORM pg_temp.give('f1', 'student', n, 9);
    INSERT INTO public.trainings (user_id, status, dataset_name, model_name, model_type, training_params, total_steps, managed,
                                  credit_scheme, capped_step, continuable_until)
    VALUES (pg_temp.u(n), 'succeeded', 'u/r', 'c052/race-parent' || n, 'act', '{"steps":50000}', 50000, true, 'derived', 2500,
            now() + interval '7 days') RETURNING id INTO p;
    INSERT INTO public.training_kept_checkpoints (training_id, step, manifest, expires_at) VALUES (p, 2500, '{}', now() + interval '7 days');
  END LOOP;
  -- R3/R4 a group deletion vs a member's finishing training (groups c3/c4 hex: 'd3','d4'; members 83, 84)
  FOREACH n IN ARRAY ARRAY['83','84'] LOOP
    PERFORM pg_temp.mk_group('d' || right(n, 1), 'c1');
    PERFORM pg_temp.mk_user(n, 'student', 'c1');
    UPDATE public.users SET workgroup_id = pg_temp.u('d' || right(n, 1)) WHERE id = pg_temp.u(n);
    PERFORM pg_temp.give('f1', 'group', 'd' || right(n, 1), 6);
    PERFORM pg_temp.run(n, 'c052/race-run' || n, 1200);
  END LOOP;
  -- R5/R6 a member start vs DELETE FROM workgroups; R7/R8 a member start vs a group compensation (groups d5..d8; members 85..88)
  FOREACH n IN ARRAY ARRAY['85','86','87','88'] LOOP
    PERFORM pg_temp.mk_group('d' || right(n, 1), 'c1');
    PERFORM pg_temp.mk_user(n, 'student', 'c1');
    UPDATE public.users SET workgroup_id = pg_temp.u('d' || right(n, 1)) WHERE id = pg_temp.u(n);
    PERFORM pg_temp.give('f1', 'group', 'd' || right(n, 1), 6);
  END LOOP;
  -- R9 a start while its student is moved into another group (member 89 of group d9, target group da)
  PERFORM pg_temp.mk_group('d9', 'c1');
  PERFORM pg_temp.mk_group('da', 'c1');
  PERFORM pg_temp.mk_user('89', 'student', 'c1');
  UPDATE public.users SET workgroup_id = pg_temp.u('d9') WHERE id = pg_temp.u('89');
  PERFORM pg_temp.give('f1', 'group', 'd9', 6);
  PERFORM pg_temp.give('f1', 'group', 'da', 6);
  -- CONTROL: a member (8c) of group dc for the old-order run that must deadlock
  PERFORM pg_temp.mk_group('dc', 'c1');
  PERFORM pg_temp.mk_user('8c', 'student', 'c1');
  UPDATE public.users SET workgroup_id = pg_temp.u('dc') WHERE id = pg_temp.u('8c');
  PERFORM pg_temp.give('f1', 'group', 'dc', 6);
END $$;
SELECT 'race fixtures ready: ' || count(*) || ' race users' FROM public.users WHERE id::text ~ '^c0520000-.*0000000000(8[0-9c])$';
\echo 'DONE'
\endif

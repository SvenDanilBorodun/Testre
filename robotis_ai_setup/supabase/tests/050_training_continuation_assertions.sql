-- Behavioural assertions for migration 050 (a capped training publishes and the
-- student continues it: capped_step/continuable_until/continued_from, the
-- training_kept_checkpoints table, cap_training_attempt, the 8-argument
-- finish_training, start_continuation_safe, the capped-publish branch of
-- claim_training_publish / cancel_request_training, and requeue_training's E45/E50
-- charging rule), run BY HAND against a fresh LOCAL database — never CI, never the
-- linked project (it seeds rows).
--
-- The squashed baseline does NOT replay on a fresh database as shipped
-- (docs/KNOWN-ISSUES.md, „baseline.sql is NOT replayable from scratch"): run it in
-- a SCRATCH copy of supabase/ whose baseline has its one
-- `RENAME COLUMN runpod_job_id` line guarded exactly as
-- 040_code_programs_assertions.sql's header shows, with every migration through
-- 048 applied by `supabase start`, then 047, 049 and 050 with psql as `postgres`
-- (-v ON_ERROR_STOP=1), then
--   psql <local db url> -f supabase/tests/050_training_continuation_assertions.sql
-- Every DENIAL is asserted with has_function_privilege / has_table_privilege /
-- has_column_privilege, never by calling (a call of a function the role may not
-- execute crashes the stand-alone postgres image with signal 11). PostgREST's view
-- is checked by hand: a request role's start_continuation_safe answers 401/42501
-- and a read of training_kept_checkpoints 42501.
--
-- 050 BREAKS 049's own assertion file by design (owner E50): run
-- 049_managed_training_assertions.sql AFTER 050 and it prints 62 PASS, 1 ERROR
-- (T1: the 5-argument finish_training no longer exists) and 6 FAIL (T5a, T5d, T29b,
-- T35b, T36a, T36b — every one a requeue of an attempt without an own ready
-- checkpoint, which E50 now charges 0); at 049 it prints 69 PASS. (The 041 header
-- did the same for 040.)
--
-- Expected: 30 PASS, 0 FAIL.
\set ON_ERROR_STOP off
\pset pager off

INSERT INTO auth.users (id, instance_id, aud, role, email, encrypted_password, created_at, updated_at) VALUES
 ('b5000001-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 's1-050@x.test', '', now(), now()),
 ('b5000002-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 's2-050@x.test', '', now(), now()),
 ('b5000003-0000-0000-0000-000000000003', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 't-050@x.test', '', now(), now()),
 ('b5000004-0000-0000-0000-000000000004', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 'g1-050@x.test', '', now(), now())
ON CONFLICT (id) DO NOTHING;
UPDATE public.users SET role = 'student', training_credits = 50 WHERE id IN ('b5000001-0000-0000-0000-000000000001','b5000002-0000-0000-0000-000000000002');
UPDATE public.users SET role = 'teacher' WHERE id = 'b5000003-0000-0000-0000-000000000003';
\set u '''b5000001-0000-0000-0000-000000000001'''
\set u2 '''b5000002-0000-0000-0000-000000000002'''
\set g1 '''b5000004-0000-0000-0000-000000000004'''

CREATE OR REPLACE FUNCTION pg_temp.expect(q text, code text, pass_line text) RETURNS text LANGUAGE plpgsql AS $$
DECLARE label text := regexp_replace(pass_line, '^PASS ', '');
BEGIN
  EXECUTE q;
  RETURN 'FAIL ' || label || ' (no error)';
EXCEPTION WHEN OTHERS THEN
  IF SQLSTATE = code THEN RETURN pass_line || ' (' || SQLSTATE || ')'; END IF;
  RETURN 'FAIL ' || label || ' (' || SQLSTATE || ': ' || SQLERRM || ')';
END $$;

-- A managed row with one running attempt whose first running write was 1000 s ago.
CREATE OR REPLACE FUNCTION pg_temp.running_row(p_user uuid, p_model text) RETURNS int LANGUAGE plpgsql AS $$
DECLARE v_id int; v_tok uuid;
BEGIN
  SELECT s.training_id INTO v_id FROM public.start_training_safe(p_user, 'u/ds', p_model, 'act',
         '{"steps":50000,"save_freq":2500,"dataset_revision":"abc"}'::jsonb, 50000, NULL, true) s;
  SELECT c.o_worker_token INTO v_tok FROM public.claim_training_attempt(v_id, 0) c;
  PERFORM public.attach_training_attempt(v_id, 1, 'runpod_secure', 'pod-' || v_id);
  UPDATE public.training_attempts SET state = 'running', first_heartbeat_at = now() - interval '1000 seconds'
   WHERE training_id = v_id AND attempt_no = 1;
  UPDATE public.trainings SET status = 'running', dataset_fps = 30 WHERE id = v_id;
  RETURN v_id;
END $$;

\echo '=== T1 grants ==='
SELECT CASE WHEN NOT has_function_privilege('anon','public.cap_training_attempt(integer,integer,integer,text,text,integer)','EXECUTE')
             AND NOT has_function_privilege('authenticated','public.start_continuation_safe(uuid,integer,text)','EXECUTE')
             AND NOT has_function_privilege('authenticated','public.finish_training(integer,integer,text,text,text,text,integer,timestamp with time zone)','EXECUTE')
             AND has_function_privilege('service_role','public.start_continuation_safe(uuid,integer,text)','EXECUTE')
             AND has_column_privilege('authenticated','public.trainings','continued_from','SELECT')
             AND has_column_privilege('anon','public.trainings','capped_step','SELECT')
             AND NOT has_table_privilege('authenticated','public.training_kept_checkpoints','SELECT')
             AND (SELECT relrowsecurity FROM pg_class WHERE oid = 'public.training_kept_checkpoints'::regclass)
             AND (SELECT count(*) FROM pg_proc WHERE proname='finish_training' AND pronamespace='public'::regnamespace) = 1
            THEN 'PASS T1' ELSE 'FAIL T1' END;

-- ===================== cap ======================
SELECT pg_temp.running_row(:u, 'EduBotics-Solutions/c1') AS p \gset
INSERT INTO public.training_checkpoints(training_id, attempt_no, step, state, manifest, created_at, ready_at)
VALUES (:p, 1, 42500, 'ready', '{"x":{"size":1}}'::jsonb, now() - interval '100 seconds', now() - interval '50 seconds');
SELECT pg_temp.expect(format('SELECT public.cap_training_attempt(%s, 1, 42500, %L, NULL, 0)', :p, 'nope'), '22023', 'PASS T2a');
SELECT pg_temp.expect(format('SELECT public.cap_training_attempt(%s, 1, 50000, %L, NULL, 0)', :p, 'capped'), '22023', 'PASS T2b');
SELECT public.cap_training_attempt(:p, 1, 42500, 'capped', 'S22', 1000);
\echo '=== T2c cap: attempt ended/capped, row running + note + capped_step, 1000 s counted once ==='
SELECT CASE WHEN t.status='running' AND t.status_note='S22' AND t.capped_step=42500 AND t.worker_token IS NULL
             AND t.train_seconds_used=1000 AND a.state='ended' AND a.end_reason='capped'
            THEN 'PASS T2c' ELSE 'FAIL T2c ' || t.status || '/' || t.train_seconds_used || '/' || a.end_reason END
  FROM public.trainings t JOIN public.training_attempts a ON a.training_id=t.id AND a.attempt_no=1 WHERE t.id=:p;
SELECT pg_temp.expect(format('SELECT public.cap_training_attempt(%s, 1, 42500, %L, NULL, 0)', :p, 'capped'), 'P0049', 'PASS T2d');
\echo '=== T3 claim_training_publish claims a capped attempt ==='
SELECT CASE WHEN public.claim_training_publish(:p, 1, 900, 'inst-A') THEN 'PASS T3' ELSE 'FAIL T3' END;
\echo '=== T5a cancel of a capped publish in flight: P0051 ==='
SELECT pg_temp.expect(format('SELECT * FROM public.cancel_request_training(%s, %L)', :p, 'b5000001-0000-0000-0000-000000000001'), 'P0051', 'PASS T5a');
INSERT INTO public.training_kept_checkpoints(training_id, step, manifest, expires_at)
VALUES (:p, 42500, '{"x":{"size":1}}'::jsonb, now() + interval '7 days');
SELECT expires_at AS exp FROM public.training_kept_checkpoints WHERE training_id = :p \gset
SELECT public.finish_training(:p, 1, 'succeeded', NULL, 'https://huggingface.co/x/blob/main/training_log.txt', 'S23', 42500, :'exp');
\echo '=== T4a finish (8 args): succeeded, current_step = the capped step, note S23, continuable_until = expires_at ==='
SELECT CASE WHEN status='succeeded' AND current_step=42500 AND status_note='S23' AND continuable_until = :'exp'::timestamptz
             AND train_seconds_used=1000
            THEN 'PASS T4a' ELSE 'FAIL T4a ' || status || '/' || current_step || '/' || coalesce(status_note,'-') END
  FROM public.trainings WHERE id=:p;

-- the 5-argument calls (positional and named) still resolve
SELECT pg_temp.running_row(:u, 'EduBotics-Solutions/c5') AS q \gset
SELECT public.finish_training(:q, 1, 'succeeded', NULL, NULL);
\echo '=== T4b the 5-argument positional call: current_step = total, note NULL, continuable_until NULL ==='
SELECT CASE WHEN status='succeeded' AND current_step=50000 AND status_note IS NULL AND continuable_until IS NULL
            THEN 'PASS T4b' ELSE 'FAIL T4b' END FROM public.trainings WHERE id=:q;
SELECT pg_temp.running_row(:u, 'EduBotics-Solutions/c6') AS q2 \gset
SELECT public.finish_training(p_training_id => :q2, p_attempt => 1, p_status => 'failed', p_message => 'S19', p_log_url => NULL);
\echo '=== T4c the 5 named arguments resolve (failed keeps current_step and continuable_until) ==='
SELECT CASE WHEN status='failed' AND error_message='S19' AND continuable_until IS NULL THEN 'PASS T4c' ELSE 'FAIL T4c' END
  FROM public.trainings WHERE id=:q2;

-- wall_capped
SELECT pg_temp.running_row(:u, 'EduBotics-Solutions/w1') AS w \gset
SELECT public.cap_training_attempt(:w, 1, 2500, 'wall_capped', 'S22', 0);
\echo '=== T2e wall_capped is a cap too: claimable, cancel refused ==='
SELECT CASE WHEN public.claim_training_publish(:w, 1, 900, 'inst-A') THEN 'PASS T2e' ELSE 'FAIL T2e' END;
SELECT pg_temp.expect(format('SELECT * FROM public.cancel_request_training(%s, %L)', :w, 'b5000001-0000-0000-0000-000000000001'), 'P0051', 'PASS T5b');

-- ===================== continuation ======================
SELECT 'x' AS dummy;
SELECT pg_temp.expect(format('SELECT * FROM public.start_continuation_safe(%L, %s, NULL)', 'b5000002-0000-0000-0000-000000000002', :p), 'P0002', 'PASS T6a');
SELECT pg_temp.expect(format('SELECT * FROM public.start_continuation_safe(%L, %s, NULL)', 'b5000001-0000-0000-0000-000000000001', :q), 'P0052', 'PASS T6b');
SELECT count(*) FILTER (WHERE status NOT IN ('failed','canceled')) AS used_before FROM public.trainings WHERE user_id = :u \gset
SELECT o_training_id AS child, o_remaining AS rem FROM public.start_continuation_safe(:u, :p, 'S28') \gset
\echo '=== T6c happy path: child managed, queued, linked, at the capped step, note, params, fps; one credit ==='
SELECT CASE WHEN c.managed AND c.status='queued' AND c.continued_from=:p AND c.current_step=42500 AND c.status_note='S28'
             AND c.training_params = pa.training_params AND c.model_name = pa.model_name AND c.dataset_fps = 30
             AND c.total_steps = 50000 AND c.current_attempt = 0 AND c.capped_step IS NULL AND c.user_id = pa.user_id
             AND :rem = 50 - :used_before - 1
             AND pa.status='succeeded' AND pa.current_step=42500
            THEN 'PASS T6c' ELSE 'FAIL T6c' END
  FROM public.trainings c, public.trainings pa WHERE c.id=:child AND pa.id=:p;
SELECT pg_temp.expect(format('SELECT * FROM public.start_continuation_safe(%L, %s, NULL)', 'b5000001-0000-0000-0000-000000000001', :p), 'P0054', 'PASS T6d');
UPDATE public.trainings SET status='failed', terminated_at=now() WHERE id=:child;
SELECT o_training_id AS child2 FROM public.start_continuation_safe(:u, :p, 'S28') \gset
\echo '=== T7 after the child failed a second continuation is allowed ==='
SELECT CASE WHEN :child2 > :child THEN 'PASS T7' ELSE 'FAIL T7' END;
UPDATE public.trainings SET status='canceled', terminated_at=now() WHERE id=:child2;
UPDATE public.training_kept_checkpoints SET state='refused' WHERE training_id=:p;
SELECT pg_temp.expect(format('SELECT * FROM public.start_continuation_safe(%L, %s, NULL)', 'b5000001-0000-0000-0000-000000000001', :p), 'P0052', 'PASS T6e');
UPDATE public.training_kept_checkpoints SET state='kept' WHERE training_id=:p;
UPDATE public.trainings SET continuable_until = now() - interval '1 second' WHERE id=:p;
SELECT pg_temp.expect(format('SELECT * FROM public.start_continuation_safe(%L, %s, NULL)', 'b5000001-0000-0000-0000-000000000001', :p), 'P0053', 'PASS T6f');
DELETE FROM public.training_kept_checkpoints WHERE training_id=:p;
\echo '=== T6g n2: expired AND the kept row already gone answers P0053, not P0052 ==='
SELECT pg_temp.expect(format('SELECT * FROM public.start_continuation_safe(%L, %s, NULL)', 'b5000001-0000-0000-0000-000000000001', :p), 'P0053', 'PASS T6g');
UPDATE public.trainings SET continuable_until = now() + interval '1 day' WHERE id=:p;
SELECT pg_temp.expect(format('SELECT * FROM public.start_continuation_safe(%L, %s, NULL)', 'b5000001-0000-0000-0000-000000000001', :p), 'P0052', 'PASS T6h');
INSERT INTO public.training_kept_checkpoints(training_id, step, manifest, expires_at)
VALUES (:p, 42500, '{"x":{"size":1}}'::jsonb, now() + interval '1 day');
UPDATE public.users SET training_credits = (SELECT count(*) FROM public.trainings WHERE user_id = :u AND status NOT IN ('failed','canceled'))
 WHERE id = :u;
SELECT pg_temp.expect(format('SELECT * FROM public.start_continuation_safe(%L, %s, NULL)', 'b5000001-0000-0000-0000-000000000001', :p), 'P0003', 'PASS T6i');
UPDATE public.users SET training_credits = 50 WHERE id = :u;

-- m5: a workgroup member's continuation is charged to the group's pool
INSERT INTO public.classrooms(id, teacher_id, name) VALUES ('c5000001-0000-0000-0000-000000000001', 'b5000003-0000-0000-0000-000000000003', 'K050') ON CONFLICT DO NOTHING;
INSERT INTO public.workgroups(id, classroom_id, name, shared_credits) VALUES ('d5000001-0000-0000-0000-000000000001', 'c5000001-0000-0000-0000-000000000001', 'G050', 2) ON CONFLICT DO NOTHING;
UPDATE public.users SET role='student', classroom_id='c5000001-0000-0000-0000-000000000001', workgroup_id='d5000001-0000-0000-0000-000000000001', training_credits=0
 WHERE id = :g1;
SELECT pg_temp.running_row(:g1, 'EduBotics-Solutions/g1') AS gp \gset
INSERT INTO public.training_checkpoints(training_id, attempt_no, step, state, manifest, created_at, ready_at)
VALUES (:gp, 1, 2500, 'ready', '{"x":{"size":1}}'::jsonb, now(), now());
SELECT public.cap_training_attempt(:gp, 1, 2500, 'capped', 'S22', 0);
INSERT INTO public.training_kept_checkpoints(training_id, step, manifest, expires_at) VALUES (:gp, 2500, '{}'::jsonb, now() + interval '7 days');
SELECT public.finish_training(:gp, 1, 'succeeded', NULL, NULL, 'S23', 2500, now() + interval '7 days');
SELECT o_training_id AS gchild, o_remaining AS grem FROM public.start_continuation_safe(:g1, :gp, 'S28') \gset
\echo '=== T6j a workgroup member: the child is in the group, the pool (2) is spent; the next one is P0003 ==='
SELECT CASE WHEN c.workgroup_id='d5000001-0000-0000-0000-000000000001' AND :grem = 0 THEN 'PASS T6j' ELSE 'FAIL T6j' END
  FROM public.trainings c WHERE c.id = :gchild;
UPDATE public.trainings SET status='failed', terminated_at=now() WHERE id=:gchild;
UPDATE public.workgroups SET shared_credits = 1 WHERE id = 'd5000001-0000-0000-0000-000000000001';
SELECT pg_temp.expect(format('SELECT * FROM public.start_continuation_safe(%L, %s, NULL)', 'b5000004-0000-0000-0000-000000000004', :gp), 'P0003', 'PASS T6k');

-- ===================== E45 / E50: a requeue charges up to the newest own ready checkpoint =====================
SELECT pg_temp.running_row(:u, 'EduBotics-Solutions/r1') AS r1 \gset
SELECT public.requeue_training(:r1, 1, 'pod_gone', 'S2', 0, 1000);
\echo '=== T8a no own ready checkpoint: 0 s charged (the API passed 1000) ==='
SELECT CASE WHEN train_seconds_used = 0 AND status='queued' THEN 'PASS T8a' ELSE 'FAIL T8a ' || train_seconds_used END FROM public.trainings WHERE id=:r1;
SELECT pg_temp.running_row(:u, 'EduBotics-Solutions/r2') AS r2 \gset
INSERT INTO public.training_checkpoints(training_id, attempt_no, step, state, manifest, created_at, ready_at) VALUES
 (:r2, 1, 2500, 'ready',     '{}'::jsonb, now() - interval '800 seconds', now() - interval '700 seconds'),
 (:r2, 1, 5000, 'ready',     '{}'::jsonb, now() - interval '400 seconds', now() - interval '100 seconds'),
 (:r2, 1, 7500, 'uploading', '{}'::jsonb, now() - interval '50 seconds',  NULL);
SELECT public.requeue_training(:r2, 1, 'heartbeat_lost', 'S1', 5000, 1000);
\echo '=== T8b charged up to the created_at of the newest own READY checkpoint (600 s), not ready_at (900), not the uploading one ==='
SELECT CASE WHEN train_seconds_used BETWEEN 599 AND 601 THEN 'PASS T8b' ELSE 'FAIL T8b ' || train_seconds_used END FROM public.trainings WHERE id=:r2;
\echo '=== T8c the API passing less than the saved span: LEAST ==='
SELECT pg_temp.running_row(:u, 'EduBotics-Solutions/r3') AS r3 \gset
INSERT INTO public.training_checkpoints(training_id, attempt_no, step, state, manifest, created_at, ready_at)
VALUES (:r3, 1, 2500, 'ready', '{}'::jsonb, now() - interval '100 seconds', now());
SELECT public.requeue_training(:r3, 1, 'pod_gone', 'S1', 2500, 300);
SELECT CASE WHEN train_seconds_used = 300 THEN 'PASS T8c' ELSE 'FAIL T8c ' || train_seconds_used END FROM public.trainings WHERE id=:r3;
\echo '=== T8d another attempt''s ready checkpoint is not this attempt''s: attempt 2 without its own adds 0 ==='
SELECT o_worker_token AS tok3 FROM public.claim_training_attempt(:r3, 1) \gset
SELECT public.attach_training_attempt(:r3, 2, 'runpod_secure', 'pod-r3b');
UPDATE public.training_attempts SET state='running', first_heartbeat_at = now() - interval '500 seconds' WHERE training_id=:r3 AND attempt_no=2;
UPDATE public.trainings SET status='running' WHERE id=:r3;
SELECT public.requeue_training(:r3, 2, 'pod_gone', 'S1', 2500, 500);
SELECT CASE WHEN train_seconds_used = 300 THEN 'PASS T8d' ELSE 'FAIL T8d ' || train_seconds_used END FROM public.trainings WHERE id=:r3;
\echo '=== T8e an attempt that never wrote running (first_heartbeat_at NULL) adds 0 ==='
SELECT o_worker_token AS tok4 FROM public.claim_training_attempt(:r3, 2) \gset
SELECT public.attach_training_attempt(:r3, 3, 'runpod_secure', 'pod-r3c');
SELECT public.requeue_training(:r3, 3, 'start_timeout', 'S1', 2500, 999);
SELECT CASE WHEN train_seconds_used = 300 THEN 'PASS T8e' ELSE 'FAIL T8e ' || train_seconds_used END FROM public.trainings WHERE id=:r3;
\echo '=== T8f end_training_attempt and finish_training still count as before (no change for normal ends) ==='
SELECT pg_temp.running_row(:u, 'EduBotics-Solutions/r4') AS r4 \gset
SELECT public.finish_training(:r4, 1, 'failed', 'S19', NULL);
SELECT CASE WHEN train_seconds_used BETWEEN 999 AND 1001 THEN 'PASS T8f' ELSE 'FAIL T8f ' || train_seconds_used END FROM public.trainings WHERE id=:r4;

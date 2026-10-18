-- Behavioural assertions for migration 049 (managed ACT training: the
-- training_attempts / training_checkpoints tables, start_training_safe's
-- p_managed, the ten compare-and-set functions and update_training_progress's
-- managed guard), run BY HAND against a fresh LOCAL database — never CI, never
-- the linked project (it seeds rows).
--
-- The squashed baseline does NOT replay on a fresh database as shipped
-- (docs/KNOWN-ISSUES.md, „baseline.sql is NOT replayable from scratch"): run
-- it in a SCRATCH copy of supabase/ whose baseline has its one
-- `RENAME COLUMN runpod_job_id` line guarded exactly as
-- 040_code_programs_assertions.sql's header shows, with every migration
-- through 048 applied by `supabase start`, then 047 and 049 with psql as
-- `postgres` (-v ON_ERROR_STOP=1), then
--   psql <local db url> -f supabase/tests/049_managed_training_assertions.sql
-- Built from the spec's behaviour probes 1–4 (§B.3). Every DENIAL is asserted
-- with has_function_privilege / has_table_privilege / has_column_privilege,
-- never by calling: in the stand-alone supabase/postgres:17.6.1.106 image a
-- call of a function the role may not execute crashes the backend with signal
-- 11 (SR-25, measured). The worker is simulated as `anon`, which IS granted
-- update_training_progress (the Modal/RunPod anon-key path). A statement sees
-- one snapshot, so an RPC's write and the check of it are separate statements.
-- PostgREST's view of the same denials is checked by hand (see the RunPod
-- training build's proof notes): a student's call of any of the ten functions
-- or of start_training_safe, and a read of training_checkpoints, answer 403.
-- Expected: 40 PASS, 0 FAIL (T1..T26; T5, T8, T9, T18 and T24 in lettered parts). The rollback is
-- exercised separately (rollback/20261020130000_049_managed_training_rollback.sql
-- refuses while this file's T26 row is active).
\set ON_ERROR_STOP off
\pset pager off

INSERT INTO auth.users (id, instance_id, aud, role, email, encrypted_password, created_at, updated_at)
VALUES ('b4900001-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 's1-049@x.test', '', now(), now())
ON CONFLICT (id) DO NOTHING;
UPDATE public.users SET role = 'student', training_credits = 50 WHERE id = 'b4900001-0000-0000-0000-000000000001';
\set u '''b4900001-0000-0000-0000-000000000001'''

-- expect(sql, sqlstate, pass_line): run `sql`; `pass_line` when it raises exactly
-- `sqlstate`, the same line with FAIL in front otherwise.
CREATE OR REPLACE FUNCTION pg_temp.expect(q text, code text, pass_line text) RETURNS text LANGUAGE plpgsql AS $$
DECLARE label text := regexp_replace(pass_line, '^PASS ', '');
BEGIN
  EXECUTE q;
  RETURN 'FAIL ' || label || ' (no error)';
EXCEPTION WHEN OTHERS THEN
  IF SQLSTATE = code THEN RETURN pass_line || ' (' || SQLSTATE || ')'; END IF;
  RETURN 'FAIL ' || label || ' (' || SQLSTATE || ': ' || SQLERRM || ')';
END $$;

\echo '=== T1 the ten functions and the 8-argument start_training_safe: service_role only ==='
SELECT CASE WHEN count(*) FILTER (WHERE has_function_privilege(r, f, 'EXECUTE')) = 0
             AND count(*) FILTER (WHERE has_function_privilege('service_role', f, 'EXECUTE')) = 22
            THEN 'PASS T1' ELSE 'FAIL T1' END
  FROM unnest(ARRAY['anon','authenticated']) r,
       unnest(ARRAY['public.claim_training_attempt(integer,integer)',
                    'public.attach_training_attempt(integer,integer,text,text)',
                    'public.release_training_attempt(integer,integer,text)',
                    'public.requeue_training(integer,integer,text,text,integer,integer)',
                    'public.end_training_attempt(integer,integer,text,text,integer)',
                    'public.finish_training(integer,integer,text,text,text)',
                    'public.cancel_request_training(integer,uuid)',
                    'public.complete_cancel_training(integer,text,text)',
                    'public.claim_training_publish(integer,integer,integer,text)',
                    'public.claim_checkpoint_mirror(integer,integer,integer,integer,text)',
                    'public.start_training_safe(uuid,text,text,text,jsonb,integer,uuid,boolean)']) f;

\echo '=== T2 the new tables: no request-role privilege, RLS on, no publication; worker path kept ==='
SELECT CASE WHEN count(*) FILTER (WHERE has_table_privilege(r, t, p)) = 0
             AND (SELECT bool_and(relrowsecurity) FROM pg_class WHERE oid IN ('public.training_attempts'::regclass, 'public.training_checkpoints'::regclass))
             AND NOT EXISTS (SELECT 1 FROM pg_publication_tables WHERE tablename IN ('training_attempts','training_checkpoints'))
             AND has_function_privilege('anon', 'public.update_training_progress(integer,uuid,text,integer,integer,real,text,text)', 'EXECUTE')
             AND has_function_privilege('authenticated', 'public.update_training_progress(integer,uuid,text,integer,integer,real,text,text)', 'EXECUTE')
             AND (SELECT count(*) FROM pg_proc WHERE proname = 'start_training_safe' AND pronamespace = 'public'::regnamespace) = 1
            THEN 'PASS T2' ELSE 'FAIL T2' END
  FROM unnest(ARRAY['anon','authenticated']) r,
       unnest(ARRAY['public.training_attempts','public.training_checkpoints']) t,
       unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) p;

\echo '=== T3 students read the seven new trainings columns by name, never worker_token ==='
SELECT CASE WHEN count(*) FILTER (WHERE NOT has_column_privilege(r, 'public.trainings', c, 'SELECT')) = 0
             AND NOT has_column_privilege('authenticated', 'public.trainings', 'worker_token', 'SELECT')
             AND NOT has_column_privilege('anon', 'public.trainings', 'worker_token', 'SELECT')
            THEN 'PASS T3' ELSE 'FAIL T3' END
  FROM unnest(ARRAY['anon','authenticated']) r,
       unnest(ARRAY['managed','current_attempt','train_seconds_used','status_note','step_advanced_at','dataset_fps','unplaced_since']) c;

-- ===================== probe 1: the happy path with one loss =====================
SELECT training_id AS tid FROM public.start_training_safe(:u,'u/ds','EduBotics-Solutions/m1','act','{"steps":5000,"save_freq":2500}'::jsonb,5000,NULL,true) \gset
\echo '=== T4 an 8-argument managed start: queued, managed, no token, attempt 0 ==='
SELECT CASE WHEN status = 'queued' AND managed AND worker_token IS NULL AND current_attempt = 0
            THEN 'PASS T4' ELSE 'FAIL T4' END FROM public.trainings WHERE id = :tid;
SELECT o_worker_token AS tok FROM public.claim_training_attempt(:tid, 0) \gset
SELECT public.attach_training_attempt(:tid, 1, 'runpod_community', 'pod-1');
SET ROLE anon;
SELECT public.update_training_progress(:tid, :'tok', 'running', 0, 5000, NULL, NULL, NULL);
SELECT public.update_training_progress(:tid, :'tok', NULL, 2500, 5000, 0.9, NULL, NULL);
SELECT public.update_training_progress(:tid, :'tok', NULL, 3000, 5000, 0.8, NULL, NULL);
RESET ROLE;
SELECT public.requeue_training(:tid, 1, 'pod_gone', 'note', 2500, 600);
\echo '=== T5a requeue: queued, token NULL (fence), step back to 2500, curve trimmed 2 -> 1, stall clock cleared, 600 s counted ==='
SELECT CASE WHEN status = 'queued' AND worker_token IS NULL AND current_step = 2500
             AND jsonb_array_length(loss_history) = 1 AND step_advanced_at IS NULL AND train_seconds_used = 600
             AND status_note = 'note'
            THEN 'PASS T5a' ELSE 'FAIL T5a' END FROM public.trainings WHERE id = :tid;
\echo '=== T5b the lost attempt''s token is fenced (P0001) ==='
SET ROLE anon;
SELECT pg_temp.expect(format('SELECT public.update_training_progress(%s, %L, NULL, 2600, 5000, 0.7, NULL, NULL)', :tid, :'tok'), 'P0001', 'PASS T5b');
RESET ROLE;
SELECT o_worker_token AS tok2 FROM public.claim_training_attempt(:tid, 1) \gset
SELECT public.attach_training_attempt(:tid, 2, 'runpod_secure', 'pod-2');
SET ROLE anon; SELECT public.update_training_progress(:tid, :'tok2', 'running', 2500, 5000, NULL, NULL, NULL); RESET ROLE;
\echo '=== T5c the second attempt''s first running write clears status_note and sets the stall clock ==='
SELECT CASE WHEN status = 'running' AND status_note IS NULL AND step_advanced_at IS NOT NULL AND :'tok2' <> :'tok'
            THEN 'PASS T5c' ELSE 'FAIL T5c' END FROM public.trainings WHERE id = :tid;
SELECT public.end_training_attempt(:tid, 2, 'succeeded', 'Training fertig – das Modell wird geprüft und veröffentlicht …', 900);
\echo '=== T5d end_training_attempt keeps running (publishing) + S3, nulls the token, counts 900 s ==='
SELECT CASE WHEN status = 'running' AND status_note LIKE 'Training fertig%' AND worker_token IS NULL
             AND train_seconds_used = 1500
            THEN 'PASS T5d' ELSE 'FAIL T5d' END FROM public.trainings WHERE id = :tid;
\echo '=== T5e the publish lease goes to inst-A and is refused to inst-B ==='
SELECT CASE WHEN public.claim_training_publish(:tid, 2, 900, 'inst-A') AND NOT public.claim_training_publish(:tid, 2, 900, 'inst-B')
            THEN 'PASS T5e' ELSE 'FAIL T5e' END;
SELECT public.finish_training(:tid, 2, 'succeeded', NULL, 'https://huggingface.co/x/blob/main/training_log.txt');
\echo '=== T5f finish(succeeded): current_step = total_steps, note NULL, log_url set ==='
SELECT CASE WHEN status = 'succeeded' AND current_step = 5000 AND status_note IS NULL AND log_url LIKE 'https://%'
             AND terminated_at IS NOT NULL
            THEN 'PASS T5f' ELSE 'FAIL T5f' END FROM public.trainings WHERE id = :tid;
\echo '=== T5g attempt 1 lost/pod_gone, attempt 2 ended/succeeded, both with first_heartbeat_at ==='
SELECT CASE WHEN string_agg(attempt_no || ':' || backend || ':' || state || ':' || end_reason, ',' ORDER BY attempt_no)
                 = '1:runpod_community:lost:pod_gone,2:runpod_secure:ended:succeeded'
             AND bool_and(first_heartbeat_at IS NOT NULL)
            THEN 'PASS T5g' ELSE 'FAIL T5g' END FROM public.training_attempts WHERE training_id = :tid;

-- ===================== probe 2: attach race, restart trim, release, lease =====================
SELECT training_id AS t1 FROM public.start_training_safe(:u,'u/ds','EduBotics-Solutions/r1','act','{"steps":5000,"save_freq":2500}'::jsonb,5000,NULL,true) \gset
SELECT o_worker_token AS tokr FROM public.claim_training_attempt(:t1, 0) \gset
SET ROLE anon; SELECT public.update_training_progress(:t1, :'tokr', 'running', 0, 5000, NULL, NULL, NULL); RESET ROLE;
\echo '=== T6 SR-9: a running write BEFORE attach; attach still succeeds and keeps running ==='
SELECT public.attach_training_attempt(:t1, 1, 'modal', 'fc-warm');
SELECT CASE WHEN state = 'running' AND backend = 'modal' AND cloud_ref = 'fc-warm' AND started_at IS NOT NULL
            THEN 'PASS T6' ELSE 'FAIL T6' END FROM public.training_attempts WHERE training_id = :t1;
\echo '=== T7 a second attach of the same attempt conflicts ==='
SELECT pg_temp.expect(format('SELECT public.attach_training_attempt(%s, 1, %L, %L)', :t1, 'modal', 'fc-other'), 'P0049', 'PASS T7');
SET ROLE anon;
SELECT public.update_training_progress(:t1, :'tokr', NULL, 2500, 5000, 0.9, NULL, NULL);
SELECT public.update_training_progress(:t1, :'tokr', NULL, 3100, 5000, 0.8, NULL, NULL);
SELECT public.update_training_progress(:t1, :'tokr', 'running', 2500, 5000, NULL, NULL, NULL);
RESET ROLE;
\echo '=== T8a SR-31: an in-place restart lowers the step and trims the curve to one point at 2500 ==='
SELECT CASE WHEN current_step = 2500 AND jsonb_array_length(loss_history) = 1 AND (loss_history->-1->>'s') = '2500'
            THEN 'PASS T8a' ELSE 'FAIL T8a' END FROM public.trainings WHERE id = :t1;
SELECT training_id AS t3 FROM public.start_training_safe(:u,'u/ds','EduBotics-Solutions/r3','act','{"steps":5000}'::jsonb,5000,NULL,true) \gset
SELECT o_attempt_no FROM public.claim_training_attempt(:t3, 0);
SELECT public.release_training_attempt(:t3, 1, NULL);
\echo '=== T8b SR-11: release — attempt back to 0, token NULL, unplaced_since set, no attempt row ==='
SELECT CASE WHEN current_attempt = 0 AND worker_token IS NULL AND unplaced_since IS NOT NULL
             AND (SELECT count(*) FROM public.training_attempts WHERE training_id = :t3) = 0
            THEN 'PASS T8b' ELSE 'FAIL T8b' END FROM public.trainings WHERE id = :t3;
SELECT o_attempt_no AS again FROM public.claim_training_attempt(:t3, 0) \gset
SELECT public.attach_training_attempt(:t3, 1, 'runpod_secure', 'pod-x');
\echo '=== T8c the next claim is attempt 1 again; attach clears unplaced_since ==='
SELECT CASE WHEN :again = 1 AND unplaced_since IS NULL THEN 'PASS T8c' ELSE 'FAIL T8c' END
  FROM public.trainings WHERE id = :t3;
\echo '=== T8d release after attach conflicts ==='
SELECT pg_temp.expect(format('SELECT public.release_training_attempt(%s, 1, NULL)', :t3), 'P0049', 'PASS T8d');
SELECT public.end_training_attempt(:t3, 1, 'succeeded', 'n', 60);
\echo '=== T9 SR-15: the lease renews for its owner, is refused to another, and passes on after expiry ==='
SELECT CASE WHEN public.claim_training_publish(:t3, 1, 900, 'inst-A') AND public.claim_training_publish(:t3, 1, 900, 'inst-A')
             AND NOT public.claim_training_publish(:t3, 1, 900, 'inst-B')
            THEN 'PASS T9a' ELSE 'FAIL T9a' END;
UPDATE public.training_attempts SET publish_claimed_at = now() - interval '20 minutes' WHERE training_id = :t3;
SELECT CASE WHEN public.claim_training_publish(:t3, 1, 900, 'inst-B') THEN 'ok' ELSE 'not granted' END AS after_expiry;
SELECT CASE WHEN publish_owner = 'inst-B' AND publish_first_at IS NOT NULL AND publish_first_at < publish_claimed_at
            THEN 'PASS T9b' ELSE 'FAIL T9b' END FROM public.training_attempts WHERE training_id = :t3;
SELECT public.finish_training(:t3, 1, 'succeeded', NULL, NULL);
SELECT training_id AS t5 FROM public.start_training_safe(:u,'u/ds','EduBotics-Solutions/r5','pi0','{}'::jsonb,10,'99999999-0000-0000-0000-000000000000') \gset
\echo '=== T10 the 7-argument call still resolves: managed false, queued, with its token ==='
SELECT CASE WHEN NOT managed AND status = 'queued' AND worker_token = '99999999-0000-0000-0000-000000000000'
            THEN 'PASS T10' ELSE 'FAIL T10' END FROM public.trainings WHERE id = :t5;

-- ===================== probe 3: conflicts, cancel, fence =====================
SELECT training_id AS a FROM public.start_training_safe(:u,'u/ds','EduBotics-Solutions/p3a','act','{"steps":5000,"save_freq":2500}'::jsonb,5000,NULL,true) \gset
SELECT o_worker_token AS toka FROM public.claim_training_attempt(:a, 0) \gset
\echo '=== T11 a second claim of attempt 0 conflicts ==='
SELECT pg_temp.expect(format('SELECT * FROM public.claim_training_attempt(%s, 0)', :a), 'P0049', 'PASS T11');
SELECT public.attach_training_attempt(:a, 1, 'runpod_community', 'pod-a');
\echo '=== T12 a managed failed from the worker is 22023 and writes nothing ==='
SET ROLE anon;
SELECT pg_temp.expect(format('SELECT public.update_training_progress(%s, %L, %L, 0, 5000, NULL, %L, NULL)', :a, :'toka', 'failed', 'host text'), '22023', 'PASS T12');
RESET ROLE;
\echo '=== T13 a claim while the current attempt still runs conflicts (no second placement) ==='
SET ROLE anon; SELECT public.update_training_progress(:a, :'toka', 'running', 0, 5000, NULL, NULL, NULL); RESET ROLE;
SELECT pg_temp.expect(format('SELECT * FROM public.claim_training_attempt(%s, 1)', :a), 'P0049', 'PASS T13');
SELECT public.requeue_training(:a, 1, 'pod_gone', 'n', 0, 0);
\echo '=== T14 cancel between attempts answers the lost attempt (backend, cloud_ref) ==='
SELECT CASE WHEN o_managed AND o_current_attempt = 1 AND o_attempt_state = 'lost' AND o_backend = 'runpod_community'
             AND o_cloud_ref = 'pod-a' AND o_cancel_attempts = 1
            THEN 'PASS T14' ELSE 'FAIL T14' END FROM public.cancel_request_training(:a, :u);
SELECT public.complete_cancel_training(:a, 'canceled', NULL);
\echo '=== T15 complete_cancel_training ends it canceled, token NULL; error_message stays NULL ==='
SELECT CASE WHEN status = 'canceled' AND worker_token IS NULL AND error_message IS NULL AND terminated_at IS NOT NULL
            THEN 'PASS T15' ELSE 'FAIL T15' END FROM public.trainings WHERE id = :a;
\echo '=== T16 the old token is fenced after the cancel (P0001) ==='
SET ROLE anon;
SELECT pg_temp.expect(format('SELECT public.update_training_progress(%s, %L, NULL, 10, 5000, NULL, NULL, NULL)', :a, :'toka'), 'P0001', 'PASS T16');
RESET ROLE;
SELECT training_id AS c FROM public.start_training_safe(:u,'u/ds','EduBotics-Solutions/p3c','act','{"steps":5000,"save_freq":2500}'::jsonb,5000,NULL,true) \gset
SELECT o_worker_token AS tokc FROM public.claim_training_attempt(:c, 0) \gset
\echo '=== T17 cancel of a placing attempt: the late attach conflicts ==='
SELECT CASE WHEN o_attempt_state = 'placing' AND o_cloud_ref IS NULL THEN 'ok' ELSE 'unexpected' END AS placing_cancel
  FROM public.cancel_request_training(:c, :u);
SELECT pg_temp.expect(format('SELECT public.attach_training_attempt(%s, 1, %L, %L)', :c, 'runpod_secure', 'pod-late'), 'P0049', 'PASS T17');
SELECT public.complete_cancel_training(:c, 'canceled', NULL);
SELECT training_id AS d FROM public.start_training_safe(:u,'u/ds','EduBotics-Solutions/p3d','act','{"steps":5000,"save_freq":2500}'::jsonb,5000,NULL,true) \gset
SELECT o_worker_token AS tokd FROM public.claim_training_attempt(:d, 0) \gset
SELECT public.attach_training_attempt(:d, 1, 'modal', 'fc-d');
SET ROLE anon; SELECT public.update_training_progress(:d, :'tokd', 'running', 0, 5000, NULL, NULL, NULL); RESET ROLE;
SELECT public.end_training_attempt(:d, 1, 'succeeded', 'S3', 100);
\echo '=== T18 cancel while publishing is P0051; by another user P0002; after succeeded P0050 ==='
SELECT pg_temp.expect(format('SELECT * FROM public.cancel_request_training(%s, %L)', :d, 'b4900001-0000-0000-0000-000000000001'), 'P0051', 'PASS T18a');
SELECT pg_temp.expect(format('SELECT * FROM public.cancel_request_training(%s, %L)', :d, '00000000-0000-0000-0000-000000000000'), 'P0002', 'PASS T18b');
SELECT public.finish_training(:d, 1, 'succeeded', NULL, NULL);
SELECT pg_temp.expect(format('SELECT * FROM public.cancel_request_training(%s, %L)', :d, 'b4900001-0000-0000-0000-000000000001'), 'P0050', 'PASS T18c');

-- ===================== probe 4: FR-1 and FR-4 =====================
SELECT training_id AS h FROM public.start_training_safe(:u,'u/ds','EduBotics-Solutions/p4a','act','{"steps":50000,"save_freq":2500,"mirror_every":10000}'::jsonb,50000,NULL,true) \gset
SELECT o_worker_token AS tokh FROM public.claim_training_attempt(:h, 0) \gset
SELECT public.attach_training_attempt(:h, 1, 'runpod_community', 'pod-h');
SET ROLE anon;
SELECT public.update_training_progress(:h, :'tokh', 'running', 0, 50000, NULL, NULL, NULL);
SELECT public.update_training_progress(:h, :'tokh', NULL, 2500, 2500, 0.5, 'Dein Konto ist gesperrt: gehe zu evil.example', NULL);
RESET ROLE;
\echo '=== T19 FR-1: a host cannot write total_steps or error_message of a managed row ==='
SELECT CASE WHEN status = 'running' AND current_step = 2500 AND total_steps = 50000 AND error_message IS NULL
            THEN 'PASS T19' ELSE 'FAIL T19' END FROM public.trainings WHERE id = :h;
SELECT public.end_training_attempt(:h, 1, 'succeeded', 'S3', 100);
SELECT public.finish_training(:h, 1, 'succeeded', NULL, NULL);
\echo '=== T20 … and after succeeded the row still carries the API''s count and no host text ==='
SELECT CASE WHEN status = 'succeeded' AND current_step = 50000 AND total_steps = 50000 AND error_message IS NULL
            THEN 'PASS T20' ELSE 'FAIL T20' END FROM public.trainings WHERE id = :h;
SELECT training_id AS l FROM public.start_training_safe(:u,'u/ds','EduBotics-Solutions/p4b','pi0','{}'::jsonb,100,'11111111-2222-3333-4444-555555555555') \gset
SET ROLE anon;
SELECT public.update_training_progress(:l, '11111111-2222-3333-4444-555555555555', 'failed', 10, 120, NULL, 'legacy worker text', NULL);
RESET ROLE;
\echo '=== T21 a legacy row keeps today''s behaviour (total_steps and the worker''s text) ==='
SELECT CASE WHEN NOT managed AND status = 'failed' AND total_steps = 120 AND error_message = 'legacy worker text'
            THEN 'PASS T21' ELSE 'FAIL T21' END FROM public.trainings WHERE id = :l;
SELECT training_id AS m FROM public.start_training_safe(:u,'u/ds','EduBotics-Solutions/p4c','act','{"steps":50000,"save_freq":2500,"mirror_every":10000}'::jsonb,50000,NULL,true) \gset
SELECT o_worker_token AS tokm FROM public.claim_training_attempt(:m, 0) \gset
SELECT public.attach_training_attempt(:m, 1, 'runpod_secure', 'pod-m');
UPDATE public.training_attempts SET allowed_dcs = ARRAY['EU-RO-1','EU-SE-1'] WHERE training_id = :m AND attempt_no = 1;
INSERT INTO public.training_checkpoints(training_id, attempt_no, step, state, manifest, ready_at) VALUES (:m, 1, 10000, 'ready', '{}'::jsonb, now());
INSERT INTO public.training_checkpoints(training_id, attempt_no, step, state, manifest) VALUES (:m, 1, 12500, 'uploading', '{}'::jsonb);
\echo '=== T22 FR-4: the mirror lease — A, A renews, B refused, an uploading checkpoint never ==='
SELECT CASE WHEN public.claim_checkpoint_mirror(:m, 1, 10000, 900, 'boot-A') AND public.claim_checkpoint_mirror(:m, 1, 10000, 900, 'boot-A')
             AND NOT public.claim_checkpoint_mirror(:m, 1, 10000, 900, 'boot-B')
             AND NOT public.claim_checkpoint_mirror(:m, 1, 12500, 900, 'boot-A')
            THEN 'PASS T22' ELSE 'FAIL T22' END;
UPDATE public.training_checkpoints SET mirror_claimed_at = now() - interval '20 minutes' WHERE training_id = :m AND step = 10000;
SELECT public.claim_checkpoint_mirror(:m, 1, 10000, 900, 'boot-B') AS b_after_expiry \gset
UPDATE public.training_checkpoints SET mirrored_at = now() WHERE training_id = :m AND step = 10000;
\echo '=== T23 … after expiry B gets it; once mirrored nobody does; allowed_dcs round-trips ==='
SELECT CASE WHEN :'b_after_expiry' = 't' AND NOT public.claim_checkpoint_mirror(:m, 1, 10000, 900, 'boot-B')
             AND (SELECT mirror_owner FROM public.training_checkpoints WHERE training_id = :m AND step = 10000) = 'boot-B'
             AND (SELECT allowed_dcs FROM public.training_attempts WHERE training_id = :m) = ARRAY['EU-RO-1','EU-SE-1']
            THEN 'PASS T23' ELSE 'FAIL T23' END;
\echo '=== T24 invalid arguments are 22023 ==='
SELECT pg_temp.expect(format('SELECT public.finish_training(%s, 1, %L, NULL, NULL)', :m, 'canceled'), '22023', 'PASS T24a');
SELECT pg_temp.expect(format('SELECT public.complete_cancel_training(%s, %L, NULL)', :m, 'succeeded'), '22023', 'PASS T24b');
SELECT pg_temp.expect(format('SELECT public.attach_training_attempt(%s, 1, %L, %L)', :m, 'aws', 'x'), '22023', 'PASS T24c');
\echo '=== T25 credits stay derived: a queued (requeued) managed row is used, a failed one is free ==='
SELECT used AS used_before FROM (SELECT trainings_used AS used FROM public.get_remaining_credits(:u)) q \gset
SELECT public.finish_training(:m, 1, 'failed', 'S7', NULL);
SELECT CASE WHEN (SELECT trainings_used FROM public.get_remaining_credits(:u)) = :used_before - 1
            THEN 'PASS T25' ELSE 'FAIL T25' END;
\echo '=== T26 an ACTIVE managed row, left behind on purpose: the 049 rollback must refuse while it exists ==='
SELECT training_id AS keep FROM public.start_training_safe(:u,'u/ds','EduBotics-Solutions/p-keep','act','{"steps":5000,"save_freq":2500}'::jsonb,5000,NULL,true) \gset
SELECT CASE WHEN status = 'queued' AND managed THEN 'PASS T26 (training ' || id || ' stays queued)' ELSE 'FAIL T26' END
  FROM public.trainings WHERE id = :keep;

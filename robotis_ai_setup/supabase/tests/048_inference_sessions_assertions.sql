-- Behavioural assertions for migration 048 (Cloud-Inferenz sessions), run BY HAND against a fresh
-- LOCAL database — never CI, never the linked project (it seeds rows).
-- The squashed baseline does not replay as shipped (docs/KNOWN-ISSUES.md, „baseline.sql is NOT
-- replayable from scratch"): use a SCRATCH copy of supabase/ whose baseline has
-- its one `ALTER TABLE public.trainings RENAME COLUMN runpod_job_id TO cloud_job_id;` line guarded as
-- 040_code_programs_assertions.sql's header shows, apply every migration through 048, then
--   psql <local db url> -f supabase/tests/048_inference_sessions_assertions.sql
-- Expected: 58 PASS lines, 0 FAIL, and ONE deliberate `ERROR: permission denied for table
-- inference_sessions` (T1c). Function privileges are asserted with has_function_privilege, never by
-- calling: in the stand-alone supabase/postgres:17.6.1.106 image a call that lacks EXECUTE segfaults the
-- backend (measured 2026-10-11, also for get_teacher_credit_summary). A statement sees one snapshot, so
-- an RPC's write and a check of it are separate statements.
\set ON_ERROR_STOP off
\pset pager off
INSERT INTO auth.users (id, instance_id, aud, role, email, encrypted_password, created_at, updated_at)
VALUES ('b4800001-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 's1-048@x.test', '', now(), now()),
       ('b4800002-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 's2-048@x.test', '', now(), now())
ON CONFLICT (id) DO NOTHING;
UPDATE public.users SET role = 'student' WHERE id IN ('b4800001-0000-0000-0000-000000000001','b4800002-0000-0000-0000-000000000002');
INSERT INTO public.trainings (id, user_id, dataset_name, model_name, model_type, status)
VALUES (948001, 'b4800001-0000-0000-0000-000000000001', 's1/wuerfel', 'EduBotics-Solutions/m048', 'act', 'succeeded') ON CONFLICT (id) DO NOTHING;
-- tokens: 43 chars base64url; hashes computed exactly as the API does (sha256 hex of the UTF-8 token)
CREATE OR REPLACE FUNCTION pg_temp.h(t text) RETURNS text LANGUAGE sql AS $$ SELECT encode(sha256(convert_to(t,'UTF8')),'hex') $$;
-- a session id by the token that currently opens it
CREATE OR REPLACE FUNCTION pg_temp.sid(t text) RETURNS uuid LANGUAGE sql AS $$ SELECT id FROM public.inference_sessions WHERE token_hash = pg_temp.h(t) $$;
\set u1 '''b4800001-0000-0000-0000-000000000001'''
\set u2 '''b4800002-0000-0000-0000-000000000002'''
\set rev '''0123456789abcdef0123456789abcdef01234567'''
\set c1 '''0123456789abcdef0123456789abcdef'''
\set c2 '''fedcba9876543210fedcba9876543210'''
\set enc '''e1.0a1b2c3d.QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFB'''
\set p1 '''AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP1'''
\set t1 '''AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAT1'''

\echo '=== T1 request roles hold nothing on the table; anon runs only its four RPCs; authenticated none ==='
SELECT CASE WHEN count(*) FILTER (WHERE has_table_privilege(r,'public.inference_sessions',p)) = 0 THEN 'PASS T1a' ELSE 'FAIL T1a' END
  FROM unnest(array['anon','authenticated']) r, unnest(array['SELECT','INSERT','UPDATE','DELETE']) p;
SELECT CASE WHEN NOT has_function_privilege('anon','public.create_inference_session_safe(uuid,integer,text,text,text,text,integer,integer,integer,integer)','EXECUTE')
             AND NOT has_function_privilege('anon','public.attach_inference_edge(uuid,text,text)','EXECUTE')
             AND NOT has_function_privilege('anon','public.arm_inference_session(uuid,uuid,text)','EXECUTE')
             AND NOT has_function_privilege('anon','public.inference_edges_due(integer,integer)','EXECUTE')
             AND NOT has_function_privilege('anon','public.mark_inference_edge_terminated(uuid)','EXECUTE')
             AND NOT has_function_privilege('anon','public.sweep_inference_sessions()','EXECUTE')
             AND NOT has_function_privilege('authenticated','public.inference_session_connect(text,text,text)','EXECUTE')
             AND NOT has_function_privilege('authenticated','public.inference_session_peek(text,text)','EXECUTE')
             AND has_function_privilege('anon','public.inference_session_connect(text,text,text)','EXECUTE')
             AND has_function_privilege('anon','public.inference_session_peek(text,text)','EXECUTE')
             AND NOT has_function_privilege('service_role','public._inference_session_finish(uuid,text)','EXECUTE')
            THEN 'PASS T1b' ELSE 'FAIL T1b' END;
BEGIN; SET LOCAL ROLE authenticated;
SELECT 'FAIL T1c (authenticated read the table)' FROM public.inference_sessions LIMIT 1;
ROLLBACK;
\echo '(T1c passes when the line above is a permission-denied ERROR)'

\echo '=== T2 create = a reservation: hash only, session_s = min(session, remaining), no clock yet ==='
SELECT CASE WHEN (r->>'session_s')::int = 1800 AND (r->>'remaining_today_s')::int = 7200 THEN 'PASS T2a' ELSE 'FAIL T2a ' || r::text END
  FROM (SELECT public.create_inference_session_safe(:u1, 948001, 'EduBotics-Solutions/m048', :rev, 'act',
          pg_temp.h(:p1), 1800, 7200, 30, 300) r) x;
SELECT CASE WHEN status = 'reserved' AND max_end_at IS NULL AND connected_at IS NULL AND modal_session_id IS NULL
             AND reserve_until BETWEEN now() + interval '295 seconds' AND now() + interval '305 seconds'
            THEN 'PASS T2b' ELSE 'FAIL T2b' END FROM public.inference_sessions WHERE token_hash = pg_temp.h(:p1);
DO $$ BEGIN
  PERFORM public.create_inference_session_safe('b4800001-0000-0000-0000-000000000001', 948001, 'EduBotics-Solutions/m048',
          '0123456789abcdef0123456789abcdef01234567', 'act', repeat('a', 64), 1800, 7200, 30, 5);
  RAISE NOTICE 'FAIL T2c';
EXCEPTION WHEN SQLSTATE '22023' THEN RAISE NOTICE 'PASS T2c (reserve window bounded)'; END $$;

\echo '=== T3 arm before the edge is attached -> not_ready; a foreign user -> P0002 ==='
SELECT CASE WHEN public.arm_inference_session(pg_temp.sid(:p1), :u1, pg_temp.h(:t1))->>'status' = 'not_ready'
            THEN 'PASS T3a' ELSE 'FAIL T3a' END;
DO $$ BEGIN
  PERFORM public.arm_inference_session((SELECT id FROM public.inference_sessions WHERE status='reserved' AND user_id='b4800001-0000-0000-0000-000000000001'),
          'b4800002-0000-0000-0000-000000000002', repeat('b', 64));
  RAISE NOTICE 'FAIL T3b';
EXCEPTION WHEN SQLSTATE 'P0002' THEN RAISE NOTICE 'PASS T3b (foreign arm refused)'; END $$;

\echo '=== T4 attach: malformed -> 22023; attach -> reserved; a second attach -> conflict ==='
DO $$ BEGIN
  PERFORM public.attach_inference_edge(gen_random_uuid(), 'sd-ok', 'not-an-envelope');
  RAISE NOTICE 'FAIL T4a';
EXCEPTION WHEN SQLSTATE '22023' THEN RAISE NOTICE 'PASS T4a'; END $$;
SELECT CASE WHEN public.attach_inference_edge(pg_temp.sid(:p1), 'sd-AAAA1', :enc)->>'status' = 'reserved' THEN 'PASS T4b' ELSE 'FAIL T4b' END;
SELECT CASE WHEN public.attach_inference_edge(pg_temp.sid(:p1), 'sd-AAAA9', :enc)->>'status' = 'conflict'
             AND (SELECT modal_session_id FROM public.inference_sessions WHERE token_hash = pg_temp.h(:p1)) = 'sd-AAAA1'
            THEN 'PASS T4c' ELSE 'FAIL T4c' END;
SELECT CASE WHEN public.attach_inference_edge(gen_random_uuid(), 'sd-AAAA8', :enc)->>'status' = 'missing' THEN 'PASS T4d' ELSE 'FAIL T4d' END;

\echo '=== T5 peek (anon): needs token AND modal session id; answers the model; no state change ==='
BEGIN; SET LOCAL ROLE anon;
SELECT CASE WHEN r->>'status' = 'reserved' AND r->>'model_repo' = 'EduBotics-Solutions/m048' AND NOT r ? 'edge_token_enc'
            THEN 'PASS T5a' ELSE 'FAIL T5a ' || r::text END
  FROM (SELECT public.inference_session_peek(:p1, 'sd-AAAA1') r) x;
SELECT CASE WHEN public.inference_session_peek(:p1, 'sd-AAAA2')->>'status' = 'invalid'
             AND public.inference_session_peek(:t1, 'sd-AAAA1')->>'status' = 'invalid'
             AND public.inference_session_peek(:p1, 'xx-AAAA1')->>'status' = 'invalid' THEN 'PASS T5b' ELSE 'FAIL T5b' END;
COMMIT;
SELECT CASE WHEN status = 'reserved' AND connected_at IS NULL THEN 'PASS T5c' ELSE 'FAIL T5c' END
  FROM public.inference_sessions WHERE token_hash = pg_temp.h(:p1);

\echo '=== T6 arm: a fresh token replaces the reservation token; answers the encrypted edge token ==='
SELECT CASE WHEN r->>'status' = 'armed' AND r->>'edge_token_enc' = 'e1.0a1b2c3d.QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFB'
             AND (r->>'session_s')::int = 1800 THEN 'PASS T6a' ELSE 'FAIL T6a ' || r::text END
  FROM (SELECT public.arm_inference_session(pg_temp.sid(:p1), :u1, pg_temp.h(:t1)) r) x;
BEGIN; SET LOCAL ROLE anon;
SELECT CASE WHEN public.inference_session_peek(:p1, 'sd-AAAA1')->>'status' = 'invalid'
             AND public.inference_session_peek(:t1, 'sd-AAAA1')->>'status' = 'reserved' THEN 'PASS T6b' ELSE 'FAIL T6b' END;
COMMIT;

\echo '=== T7 connect (anon): the modal session id must match; the run clock starts at connect ==='
BEGIN; SET LOCAL ROLE anon;
SELECT CASE WHEN public.inference_session_connect(:t1, 'sd-AAAA2', :c1)->>'status' = 'invalid' THEN 'PASS T7a' ELSE 'FAIL T7a' END;
SELECT CASE WHEN r->>'status' = 'active' AND r->>'model_repo' = 'EduBotics-Solutions/m048'
             AND (r->>'max_end_in_s')::numeric BETWEEN 1795 AND 1800 THEN 'PASS T7b' ELSE 'FAIL T7b ' || r::text END
  FROM (SELECT public.inference_session_connect(:t1, 'sd-AAAA1', :c1) r) x;
COMMIT;
SELECT CASE WHEN max_end_at - connected_at = interval '1800 seconds' THEN 'PASS T7c' ELSE 'FAIL T7c' END
  FROM public.inference_sessions WHERE token_hash = pg_temp.h(:t1);

\echo '=== T8 a reconnect on another conn never moves max_end_at; the old conn is told replaced ==='
CREATE TEMP TABLE t8 AS SELECT max_end_at FROM public.inference_sessions WHERE token_hash = pg_temp.h(:t1);
SELECT pg_sleep(1.1);
BEGIN; SET LOCAL ROLE anon;
SELECT public.inference_session_connect(:t1, 'sd-AAAA1', :c2)->>'status' AS reconnected;
SELECT CASE WHEN public.inference_session_heartbeat(:t1, 'sd-AAAA1', :c1, NULL)->>'status' = 'replaced'
             AND public.inference_session_heartbeat(:t1, 'sd-AAAA1', :c2, '{"chunks":3}')->>'status' = 'active'
             AND public.inference_session_heartbeat(:t1, 'sd-AAAA9', :c2, NULL)->>'status' = 'invalid'
            THEN 'PASS T8a' ELSE 'FAIL T8a' END;
COMMIT;
SELECT CASE WHEN s.max_end_at = t8.max_end_at AND s.stats = '{"chunks":3}'::jsonb THEN 'PASS T8b' ELSE 'FAIL T8b' END
  FROM public.inference_sessions s, t8 WHERE s.token_hash = pg_temp.h(:t1);
SELECT CASE WHEN public.arm_inference_session(pg_temp.sid(:t1), :u1, repeat('c', 64))->>'status' = 'active'
             AND (SELECT token_hash FROM public.inference_sessions WHERE id = pg_temp.sid(:t1)) = pg_temp.h(:t1)
            THEN 'PASS T8c (no re-arm of a connected run)' ELSE 'FAIL T8c' END;

\echo '=== T9 a second create supersedes; the old row keeps its edge for the terminate path ==='
SELECT public.create_inference_session_safe(:u1, 948001, 'EduBotics-Solutions/m048', :rev, 'act',
          pg_temp.h('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP2'), 1800, 7200, 30, 300) IS NOT NULL AS created;
SELECT CASE WHEN (SELECT end_reason FROM public.inference_sessions WHERE token_hash = pg_temp.h(:t1)) = 'superseded'
         AND (SELECT edge_token_enc IS NOT NULL FROM public.inference_sessions WHERE token_hash = pg_temp.h(:t1))
         AND (SELECT count(*) FROM public.inference_sessions WHERE user_id = 'b4800001-0000-0000-0000-000000000001' AND status IN ('reserved','active')) = 1
       THEN 'PASS T9a' ELSE 'FAIL T9a' END;
BEGIN; SET LOCAL ROLE anon;
SELECT CASE WHEN r->>'status'='ended' AND r->>'end_reason'='superseded' THEN 'PASS T9b' ELSE 'FAIL T9b ' || r::text END
  FROM (SELECT public.inference_session_heartbeat(:t1, 'sd-AAAA1', :c2, NULL) r) x;
COMMIT;

\echo '=== T10 an edge attached to an already ENDED row is recorded and reported ended (one terminate path) ==='
SELECT public.create_inference_session_safe(:u1, 948001, 'EduBotics-Solutions/m048', :rev, 'act',
          pg_temp.h('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP3'), 1800, 7200, 30, 300) IS NOT NULL AS created_p3;
SELECT CASE WHEN r->>'status' = 'ended' AND r->>'end_reason' = 'superseded' THEN 'PASS T10a' ELSE 'FAIL T10a ' || r::text END
  FROM (SELECT public.attach_inference_edge(pg_temp.sid('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP2'), 'sd-AAAA2', :enc) r) x;
SELECT CASE WHEN modal_session_id = 'sd-AAAA2' AND edge_token_enc IS NOT NULL THEN 'PASS T10b' ELSE 'FAIL T10b' END
  FROM public.inference_sessions WHERE token_hash = pg_temp.h('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP2');

\echo '=== T11 edges due: ended rows with a stored edge, by age on the DB clock; mark erases the secret once ==='
SELECT CASE WHEN jsonb_array_length(public.inference_edges_due(0, 50)) = 2
             AND jsonb_array_length(public.inference_edges_due(600, 50)) = 1
             AND (public.inference_edges_due(600, 50)->0->>'id')::uuid = pg_temp.sid('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP2')
            THEN 'PASS T11a (never connected: due at once; connected: after the min age)' ELSE 'FAIL T11a' END;
SELECT CASE WHEN public.mark_inference_edge_terminated(pg_temp.sid(:t1)) AND NOT public.mark_inference_edge_terminated(pg_temp.sid(:t1))
            THEN 'PASS T11b' ELSE 'FAIL T11b' END;
SELECT CASE WHEN edge_token_enc IS NULL AND edge_terminated_at IS NOT NULL AND modal_session_id = 'sd-AAAA1'
             AND jsonb_array_length(public.inference_edges_due(0, 50)) = 1 THEN 'PASS T11c' ELSE 'FAIL T11c' END
  FROM public.inference_sessions WHERE token_hash = pg_temp.h(:t1);
SELECT CASE WHEN NOT public.mark_inference_edge_terminated(pg_temp.sid('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP3'))
            THEN 'PASS T11d (a live row keeps nothing to erase)' ELSE 'FAIL T11d' END;
DO $$ BEGIN PERFORM public.inference_edges_due(0, 0); RAISE NOTICE 'FAIL T11e';
EXCEPTION WHEN SQLSTATE '22023' THEN RAISE NOTICE 'PASS T11e'; END $$;

\echo '=== T12 a reservation the robot never connects to ends never_connected at reserve_until (sweep) ==='
UPDATE public.inference_sessions SET reserve_until = now() - interval '1 second'
 WHERE token_hash = pg_temp.h('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP3');
SELECT public.sweep_inference_sessions() >= 1 AS swept;
SELECT CASE WHEN end_reason = 'never_connected' AND active_seconds = 0 THEN 'PASS T12' ELSE 'FAIL T12' END
  FROM public.inference_sessions WHERE token_hash = pg_temp.h('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP3');

\echo '=== T13 client heartbeat older than 45 s -> ended client_gone ==='
SELECT public.create_inference_session_safe(:u2, NULL, 'EduBotics-Solutions/m048', :rev, 'act',
          pg_temp.h('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQ1'), 1800, 7200, 30, 300) IS NOT NULL AS created;
SELECT public.attach_inference_edge(pg_temp.sid('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQ1'), 'sd-QQQQ1', :enc)->>'status' AS attached;
BEGIN; SET LOCAL ROLE anon; SELECT public.inference_session_connect('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQ1', 'sd-QQQQ1', :c1)->>'status' AS connected; COMMIT;
UPDATE public.inference_sessions SET client_heartbeat_at = now() - interval '46 seconds',
       edge_attached_at = now() - interval '46 seconds' WHERE token_hash = pg_temp.h('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQ1');
BEGIN; SET LOCAL ROLE anon;
SELECT CASE WHEN r->>'end_reason'='client_gone' THEN 'PASS T13' ELSE 'FAIL T13 ' || r::text END
  FROM (SELECT public.inference_session_heartbeat('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQ1', 'sd-QQQQ1', :c1, NULL) r) x;
COMMIT;

\echo '=== T14 daily cap counts connected time only; < 60 s left -> P0048 and nothing changes ==='
UPDATE public.inference_sessions SET connected_at = ended_at - interval '7150 seconds',
       max_end_at = ended_at + interval '1 hour' WHERE token_hash = pg_temp.h('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQ1');
DO $$ BEGIN
  PERFORM public.create_inference_session_safe('b4800002-0000-0000-0000-000000000002', NULL,
          'EduBotics-Solutions/m048', '0123456789abcdef0123456789abcdef01234567', 'act', repeat('d', 64), 1800, 7200, 30, 300);
  RAISE NOTICE 'FAIL T14 (no P0048)';
EXCEPTION WHEN SQLSTATE 'P0048' THEN RAISE NOTICE 'PASS T14'; END $$;

\echo '=== T15 global cap counts reservations -> P0049; the refusal rolls the supersede back ==='
SELECT public.create_inference_session_safe(:u1, 948001, 'EduBotics-Solutions/m048', :rev, 'act',
          pg_temp.h('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP4'), 1800, 86400, 30, 300) IS NOT NULL AS created_p4;
SELECT public.create_inference_session_safe(:u2, NULL, 'EduBotics-Solutions/m048', :rev, 'act',
          pg_temp.h('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAR1'), 1800, 86400, 30, 300) IS NOT NULL AS created_r1;
DO $$ BEGIN
  PERFORM public.create_inference_session_safe('b4800001-0000-0000-0000-000000000001', NULL,
          'EduBotics-Solutions/m048', '0123456789abcdef0123456789abcdef01234567', 'act', repeat('e', 64), 1800, 86400, 1, 300);
  RAISE NOTICE 'FAIL T15a';
EXCEPTION WHEN SQLSTATE 'P0049' THEN RAISE NOTICE 'PASS T15a'; END $$;
SELECT CASE WHEN (SELECT status FROM public.inference_sessions WHERE token_hash = pg_temp.h('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP4')) = 'reserved'
            THEN 'PASS T15b' ELSE 'FAIL T15b' END;

\echo '=== T16 owner end: only the owner; user_stop|edge_failed; idempotent; active_seconds from connected_at ==='
SELECT public.create_inference_session_safe(:u2, NULL, 'EduBotics-Solutions/m048', :rev, 'act',
          pg_temp.h('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQ2'), 1800, 86400, 30, 300) IS NOT NULL AS created;
DO $$ DECLARE v uuid; BEGIN
  SELECT id INTO v FROM public.inference_sessions WHERE user_id='b4800002-0000-0000-0000-000000000002' AND status IN ('reserved','active');
  BEGIN PERFORM public.end_inference_session(v, 'b4800001-0000-0000-0000-000000000001', 'user_stop'); RAISE NOTICE 'FAIL T16a';
  EXCEPTION WHEN SQLSTATE 'P0002' THEN RAISE NOTICE 'PASS T16a (foreign end refused)'; END;
  BEGIN PERFORM public.end_inference_session(v, 'b4800002-0000-0000-0000-000000000002', 'expired'); RAISE NOTICE 'FAIL T16b';
  EXCEPTION WHEN SQLSTATE '22023' THEN RAISE NOTICE 'PASS T16b (reason allowlist)'; END;
  UPDATE public.inference_sessions SET modal_session_id = 'sd-QQQQ2', edge_attached_at = now(),
         connected_at = now() - interval '100 seconds', max_end_at = now() + interval '1700 seconds', status = 'active' WHERE id = v;
  IF (public.end_inference_session(v, 'b4800002-0000-0000-0000-000000000002', 'user_stop')->>'active_seconds')::int BETWEEN 99 AND 101
     AND public.end_inference_session(v, 'b4800002-0000-0000-0000-000000000002', 'edge_failed')->>'end_reason' = 'user_stop'
  THEN RAISE NOTICE 'PASS T16c'; ELSE RAISE NOTICE 'FAIL T16c'; END IF;
END $$;

\echo '=== T17 expired: an active row past max_end_at -> connect says ended/expired ==='
SELECT public.create_inference_session_safe(:u2, NULL, 'EduBotics-Solutions/m048', :rev, 'act',
          pg_temp.h('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQ3'), 1800, 86400, 30, 300) IS NOT NULL AS created;
SELECT public.attach_inference_edge(pg_temp.sid('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQ3'), 'sd-QQQQ3', :enc)->>'status' AS attached;
BEGIN; SET LOCAL ROLE anon; SELECT public.inference_session_connect('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQ3', 'sd-QQQQ3', :c1)->>'status' AS connected; COMMIT;
UPDATE public.inference_sessions SET max_end_at = now() - interval '1 second' WHERE token_hash = pg_temp.h('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQ3');
BEGIN; SET LOCAL ROLE anon;
SELECT CASE WHEN r->>'end_reason'='expired' THEN 'PASS T17' ELSE 'FAIL T17 ' || r::text END
  FROM (SELECT public.inference_session_connect('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQ3', 'sd-QQQQ3', :c1) r) x;
COMMIT;

\echo '=== T18 sweep ends an active row whose server heartbeat is > 60 s old: server_gone ==='
SELECT public.create_inference_session_safe(:u2, NULL, 'EduBotics-Solutions/m048', :rev, 'act',
          pg_temp.h('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQ4'), 1800, 86400, 30, 300) IS NOT NULL AS created;
SELECT public.attach_inference_edge(pg_temp.sid('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQ4'), 'sd-QQQQ4', :enc)->>'status' AS attached;
BEGIN; SET LOCAL ROLE anon; SELECT public.inference_session_connect('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQ4', 'sd-QQQQ4', :c1)->>'status' AS connected; COMMIT;
UPDATE public.inference_sessions SET last_server_heartbeat_at = now() - interval '61 seconds' WHERE token_hash = pg_temp.h('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQ4');
SELECT public.sweep_inference_sessions() >= 1 AS swept;
SELECT CASE WHEN end_reason = 'server_gone' THEN 'PASS T18' ELSE 'FAIL T18' END
  FROM public.inference_sessions WHERE token_hash = pg_temp.h('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQ4');

\echo '=== T19 robot end (anon): only the current conn + modal id; reason allowlist; stats kept; idempotent ==='
SELECT public.create_inference_session_safe(:u2, NULL, 'EduBotics-Solutions/m048', :rev, 'act',
          pg_temp.h('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQ5'), 1800, 86400, 30, 300) IS NOT NULL AS created;
SELECT public.attach_inference_edge(pg_temp.sid('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQ5'), 'sd-QQQQ5', :enc)->>'status' AS attached;
BEGIN; SET LOCAL ROLE anon;
SELECT public.inference_session_connect('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQ5', 'sd-QQQQ5', :c1)->>'status' AS connected;
SELECT CASE WHEN public.inference_session_end('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQ5', 'sd-QQQQ5', :c2, 'robot_stop', NULL)->>'status'='replaced'
             AND public.inference_session_end('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQ5', 'sd-QQQQ9', :c1, 'robot_stop', NULL)->>'status'='invalid'
             AND public.inference_session_end('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQ5', 'sd-QQQQ5', :c1, 'user_stop', NULL)->>'status'='invalid'
             AND public.inference_session_end('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQ5', 'sd-QQQQ5', :c1, 'robot_stop', '{"rtt_p50":110}')->>'end_reason'='robot_stop'
             AND public.inference_session_end('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQ5', 'sd-QQQQ5', :c1, 'stall', NULL)->>'end_reason'='robot_stop'
            THEN 'PASS T19' ELSE 'FAIL T19' END;
COMMIT;

\echo '=== T20 the anon RPCs touch no clock limit, owner, edge secret or credit ==='
SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname='public' AND p.proname IN ('inference_session_peek','inference_session_connect','inference_session_heartbeat','inference_session_end')
           AND (p.prosrc ~* 'session_s\s*=' OR p.prosrc ~* 'reserve_until\s*=' OR p.prosrc ~* 'SET\s+token_hash'
                OR p.prosrc ~* 'user_id\s*=' OR p.prosrc ~* 'edge_token_enc' OR p.prosrc ~* 'trainings' OR p.prosrc ~* 'credits'))
       THEN 'PASS T20' ELSE 'FAIL T20' END;

\echo '=== T21 the edge CHECKs: no edge secret without a session id; the id shape; no secret after termination ==='
DO $$ BEGIN
  UPDATE public.inference_sessions SET edge_token_enc = 'e1.0a1b2c3d.QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFB'
   WHERE modal_session_id IS NULL;
  IF FOUND THEN RAISE NOTICE 'FAIL T21a'; ELSE RAISE NOTICE 'FAIL T21a (no row to test)'; END IF;
EXCEPTION WHEN check_violation THEN RAISE NOTICE 'PASS T21a'; END $$;
DO $$ BEGIN
  UPDATE public.inference_sessions SET modal_session_id = 'sd-x y' WHERE token_hash = encode(sha256(convert_to('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQ5','UTF8')),'hex');
  RAISE NOTICE 'FAIL T21b';
EXCEPTION WHEN check_violation THEN RAISE NOTICE 'PASS T21b'; END $$;
DO $$ BEGIN
  UPDATE public.inference_sessions SET edge_terminated_at = now() WHERE edge_token_enc IS NOT NULL;
  RAISE NOTICE 'FAIL T21c';
EXCEPTION WHEN check_violation THEN RAISE NOTICE 'PASS T21c'; END $$;

\echo '=== T22 inference_runs.backend defaults to jetson, accepts cloud, refuses others ==='
INSERT INTO public.inference_runs (student_user_id, policy_repo, exit_reason) VALUES ('b4800001-0000-0000-0000-000000000001', 'x', 'stopped');
SELECT CASE WHEN (SELECT backend FROM public.inference_runs WHERE policy_repo='x' LIMIT 1)='jetson' THEN 'PASS T22a' ELSE 'FAIL T22a' END;
DO $$ BEGIN INSERT INTO public.inference_runs (student_user_id, policy_repo, exit_reason, backend) VALUES ('b4800001-0000-0000-0000-000000000001', 'y', 'stopped', 'gpu'); RAISE NOTICE 'FAIL T22b';
EXCEPTION WHEN check_violation THEN RAISE NOTICE 'PASS T22b'; END $$;

\echo '=== T23 client heartbeat: own session only; refreshes; max_end_in_s is null while reserved ==='
DO $$ BEGIN PERFORM public.heartbeat_inference_session_client(gen_random_uuid(), 'b4800001-0000-0000-0000-000000000001'); RAISE NOTICE 'FAIL T23a';
EXCEPTION WHEN SQLSTATE 'P0002' THEN RAISE NOTICE 'PASS T23a'; END $$;
UPDATE public.inference_sessions SET client_heartbeat_at = now() - interval '30 seconds'
 WHERE user_id = 'b4800001-0000-0000-0000-000000000001' AND status = 'reserved';
SELECT CASE WHEN r->>'status' = 'reserved' AND r->'max_end_in_s' = 'null'::jsonb THEN 'PASS T23b' ELSE 'FAIL T23b ' || r::text END
  FROM (SELECT public.heartbeat_inference_session_client(id, user_id) r FROM public.inference_sessions
         WHERE user_id = 'b4800001-0000-0000-0000-000000000001' AND status = 'reserved') x;
SELECT CASE WHEN client_heartbeat_at > now() - interval '5 seconds' THEN 'PASS T23c' ELSE 'FAIL T23c' END
  FROM public.inference_sessions WHERE user_id = 'b4800001-0000-0000-0000-000000000001' AND status = 'reserved';

\echo '=== T24 usage names the live session, never a token, hash or edge secret ==='
SELECT CASE WHEN r ? 'live_session_id' AND r->>'live_session_id' IS NOT NULL AND NOT r::text ~ '[0-9a-f]{64}' AND NOT r::text ~ 'e1\.'
            THEN 'PASS T24' ELSE 'FAIL T24' END
  FROM (SELECT public.inference_usage('b4800001-0000-0000-0000-000000000001', 7200) r) x;

\echo '=== T25 Europe/Berlin day: sessions created yesterday (Berlin) do not count ==='
UPDATE public.inference_sessions SET created_at = created_at - interval '1 day' WHERE user_id = 'b4800002-0000-0000-0000-000000000002';
SELECT CASE WHEN (public.inference_usage('b4800002-0000-0000-0000-000000000002', 7200)->>'remaining_today_s')::int = 7200 THEN 'PASS T25' ELSE 'FAIL T25' END;

\echo '=== T26 edges due never lists a live row, even one with a stored edge ==='
SELECT public.attach_inference_edge(pg_temp.sid('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP4'), 'sd-PPPP4', :enc)->>'status' AS attached_p4;
SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM jsonb_array_elements(public.inference_edges_due(0, 500)) e
                              JOIN public.inference_sessions s ON s.id = (e->>'id')::uuid WHERE s.status <> 'ended')
             AND EXISTS (SELECT 1 FROM public.inference_sessions WHERE status <> 'ended' AND edge_token_enc IS NOT NULL)
            THEN 'PASS T26' ELSE 'FAIL T26' END;

\echo '=== T27 (F-1) a reservation still waiting for its Modal session never ends client_gone; attach then succeeds ==='
SELECT public.create_inference_session_safe(:u2, NULL, 'EduBotics-Solutions/m048', :rev, 'act',
          pg_temp.h('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAS1'), 1800, 86400, 30, 300) IS NOT NULL AS created_s1;
UPDATE public.inference_sessions SET client_heartbeat_at = now() - interval '100 seconds'
 WHERE token_hash = pg_temp.h('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAS1');
SELECT public.sweep_inference_sessions() IS NOT NULL AS swept;
SELECT CASE WHEN (SELECT status FROM public.inference_sessions WHERE token_hash = pg_temp.h('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAS1')) = 'reserved'
            THEN 'PASS T27a' ELSE 'FAIL T27a' END;
SELECT CASE WHEN public.attach_inference_edge(pg_temp.sid('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAS1'), 'sd-SSSS1', :enc)->>'status' = 'reserved'
            THEN 'PASS T27b' ELSE 'FAIL T27b' END;
\echo '=== T27c the 45 s rule then counts from the attach: 46 s after it (no page heartbeat) -> client_gone ==='
UPDATE public.inference_sessions SET edge_attached_at = now() - interval '46 seconds'
 WHERE token_hash = pg_temp.h('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAS1');
SELECT public.sweep_inference_sessions() IS NOT NULL AS swept;
SELECT CASE WHEN (SELECT end_reason FROM public.inference_sessions WHERE token_hash = pg_temp.h('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAS1')) = 'client_gone'
            THEN 'PASS T27c' ELSE 'FAIL T27c' END;

\echo '=== T28 (F-8) arm in the last seconds of the window leaves the robot >= 60 s to connect ==='
SELECT public.create_inference_session_safe(:u2, NULL, 'EduBotics-Solutions/m048', :rev, 'act',
          pg_temp.h('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAS2'), 1800, 86400, 30, 300) IS NOT NULL AS created_s2;
SELECT public.attach_inference_edge(pg_temp.sid('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAS2'), 'sd-SSSS2', :enc)->>'status' AS attached_s2;
UPDATE public.inference_sessions SET reserve_until = now() + interval '5 seconds'
 WHERE token_hash = pg_temp.h('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAS2');
SELECT public.arm_inference_session(pg_temp.sid('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAS2'), :u2,
          pg_temp.h('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAS3'))->>'status' AS armed_s3;
SELECT CASE WHEN reserve_until >= now() + interval '59 seconds' THEN 'PASS T28' ELSE 'FAIL T28' END
  FROM public.inference_sessions WHERE token_hash = pg_temp.h('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAS3');

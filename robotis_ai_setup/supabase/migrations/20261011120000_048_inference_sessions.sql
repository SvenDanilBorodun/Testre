-- 048 — Cloud-Inferenz control plane: public.inference_sessions (service-role only), four anon RPCs
-- for the Modal app keyed by sha256(connect token) AND the Modal session id the Modal proxy stamps on
-- every request, nine service-role RPCs for the cloud API, three internal helpers, and
-- public.inference_runs.backend ('jetson' | 'cloud').
--
-- Proven on a local stack (public.ecr.aws/supabase/postgres:17.6.1.106, the guarded baseline + every
-- migration through 046 applied with psql as postgres) with
-- supabase/tests/048_inference_sessions_assertions.sql -> 58 PASS, 0 FAIL (+ the deliberate T1c error);
-- rollback, re-apply, rollback, re-apply clean.
-- Rollback: rollback/20261011120000_048_inference_sessions_rollback.sql (roll the cloud API back first).
--
-- NUMBERING: 037 stays reserved („Eigene Objekte"), 047 is earmarked for the worker_token fix
-- (docs/KNOWN-ISSUES.md, „Proposed fix (migration 047, not written)"), so this is 048. Timestamp
-- 20261011120000 > 046's 20261004150000.
--
-- LIFECYCLE: reserved (model chosen; the API reserves a Modal session and stores its id + the
-- ENCRYPTED edge token, which it needs to terminate it) -> active (the robot connected; the run clock
-- starts: max_end_at = connected_at + session_s) -> ended. A reservation the robot never connects to
-- ends 'never_connected' at reserve_until. The edge token is erased once the API terminated the Modal
-- session (edge_terminated_at).
--
-- SECURITY: no request role may read or write the table (no grant, no policy, not in Realtime); the
-- connect token is stored only as its sha256 hex; the edge token only encrypted by the cloud API
-- (AES-256-GCM, HKDF sub-key of EDUBOTICS_HF_TOKEN_KEY) and only until it is terminated; the anon RPCs
-- reach only the row the token hashes to AND whose modal_session_id equals the caller's, and cannot
-- change max_end_at once set, session_s, reserve_until, user_id, training_id or anything credit-related;
-- the client heartbeat (45 s, O6) and server heartbeat (60 s) timeouts and the Europe/Berlin day live
-- here. Supabase's default privileges grant every new public table/function to anon + authenticated,
-- hence the explicit REVOKEs and the self-check at the end (it aborts the migration if any survives).

BEGIN;

CREATE TABLE public.inference_sessions (
  id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id                   UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  classroom_id              UUID REFERENCES public.classrooms(id) ON DELETE SET NULL,
  workgroup_id              UUID REFERENCES public.workgroups(id) ON DELETE SET NULL,
  training_id               INT  REFERENCES public.trainings(id) ON DELETE SET NULL,
  model_repo                TEXT NOT NULL
    CHECK (model_repo ~ '^EduBotics-Solutions/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$'),
  model_revision            TEXT NOT NULL CHECK (model_revision ~ '^[0-9a-f]{40}$'),
  policy_type               TEXT NOT NULL CHECK (policy_type = 'act'),
  token_hash                TEXT NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$'),
  status                    TEXT NOT NULL DEFAULT 'reserved'
    CHECK (status IN ('reserved', 'active', 'ended')),
  conn_id                   TEXT CHECK (conn_id IS NULL OR conn_id ~ '^[0-9a-f]{32}$'),
  modal_session_id          TEXT UNIQUE
    CHECK (modal_session_id IS NULL OR modal_session_id ~ '^sd-[A-Za-z0-9_-]{1,128}$'),
  edge_token_enc            TEXT
    CHECK (edge_token_enc IS NULL
           OR (edge_token_enc ~ '^e1\.[0-9a-f]{8}\.[A-Za-z0-9_-]{24,}$' AND length(edge_token_enc) <= 6000)),
  edge_attached_at          TIMESTAMPTZ,
  edge_terminated_at        TIMESTAMPTZ,
  session_s                 INT NOT NULL CHECK (session_s BETWEEN 60 AND 14400),
  reserve_until             TIMESTAMPTZ NOT NULL,
  created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
  connected_at              TIMESTAMPTZ,
  last_server_heartbeat_at  TIMESTAMPTZ,
  client_heartbeat_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  max_end_at                TIMESTAMPTZ,
  ended_at                  TIMESTAMPTZ,
  end_reason                TEXT CHECK (end_reason IS NULL OR end_reason IN (
                              'user_stop', 'robot_stop', 'robot_error', 'stall',
                              'superseded', 'client_gone', 'expired',
                              'server_error', 'server_gone', 'never_connected', 'edge_failed')),
  active_seconds            INT NOT NULL DEFAULT 0 CHECK (active_seconds >= 0),
  stats                     JSONB NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(stats) = 'object' AND octet_length(stats::text) <= 2048),
  run_logged_at             TIMESTAMPTZ,
  CONSTRAINT inference_sessions_ended_consistent
    CHECK ((status = 'ended') = (ended_at IS NOT NULL AND end_reason IS NOT NULL)),
  CONSTRAINT inference_sessions_active_consistent
    CHECK (status <> 'active' OR (connected_at IS NOT NULL AND modal_session_id IS NOT NULL)),
  CONSTRAINT inference_sessions_clock_consistent
    CHECK ((connected_at IS NULL) = (max_end_at IS NULL)),
  CONSTRAINT inference_sessions_edge_consistent
    CHECK ((modal_session_id IS NULL) = (edge_attached_at IS NULL)
           AND (edge_token_enc IS NULL OR modal_session_id IS NOT NULL)
           AND (edge_terminated_at IS NULL OR (edge_token_enc IS NULL AND status = 'ended')))
);

CREATE UNIQUE INDEX inference_sessions_one_live_per_user
  ON public.inference_sessions (user_id) WHERE status IN ('reserved', 'active');
CREATE INDEX inference_sessions_user_created
  ON public.inference_sessions (user_id, created_at DESC);
CREATE INDEX inference_sessions_live
  ON public.inference_sessions (status) WHERE status IN ('reserved', 'active');
CREATE INDEX inference_sessions_unlogged
  ON public.inference_sessions (ended_at) WHERE status = 'ended' AND run_logged_at IS NULL;
CREATE INDEX inference_sessions_edges_due
  ON public.inference_sessions (ended_at) WHERE status = 'ended' AND edge_token_enc IS NOT NULL;
CREATE INDEX inference_sessions_training ON public.inference_sessions (training_id);
CREATE INDEX inference_sessions_classroom ON public.inference_sessions (classroom_id);
CREATE INDEX inference_sessions_workgroup ON public.inference_sessions (workgroup_id);

ALTER TABLE public.inference_sessions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.inference_sessions FROM PUBLIC, anon, authenticated;

COMMENT ON TABLE public.inference_sessions IS
  'Cloud-Inferenz sessions (048). Service-role only (no grant, no policy, not in Realtime); '
  'the connect token is stored as its sha256 only, the Modal edge token encrypted and only until '
  'it is terminated. The Modal app reaches its rows through four SECURITY DEFINER anon RPCs keyed '
  'by the connect token and the Modal session id.';

ALTER TABLE public.inference_runs
  ADD COLUMN IF NOT EXISTS backend TEXT NOT NULL DEFAULT 'jetson'
  CONSTRAINT inference_runs_backend_check CHECK (backend IN ('jetson', 'cloud'));

-- internal: the one writer of the 'ended' transition (not executable by any request role)
CREATE FUNCTION public._inference_session_finish(p_id UUID, p_reason TEXT)
RETURNS VOID LANGUAGE sql SET search_path = public, pg_temp AS $$
  UPDATE public.inference_sessions
     SET status = 'ended', ended_at = now(), end_reason = p_reason,
         active_seconds = CASE WHEN connected_at IS NULL THEN 0
           ELSE GREATEST(0, floor(EXTRACT(EPOCH FROM (LEAST(now(), max_end_at) - connected_at))))::INT END
   WHERE id = p_id AND status IN ('reserved', 'active');
$$;

-- shared: is this live row past one of its clock limits? Ends it and returns the reason.
CREATE FUNCTION public._inference_session_expire_if_due(p_id UUID)
RETURNS TEXT LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE v public.inference_sessions%ROWTYPE;
BEGIN
  SELECT * INTO v FROM public.inference_sessions WHERE id = p_id;
  IF v.status = 'ended' THEN RETURN v.end_reason; END IF;
  IF v.status = 'reserved' AND now() >= v.reserve_until THEN
    PERFORM public._inference_session_finish(p_id, 'never_connected'); RETURN 'never_connected';
  END IF;
  IF v.status = 'active' AND now() >= v.max_end_at THEN
    PERFORM public._inference_session_finish(p_id, 'expired'); RETURN 'expired';
  END IF;
  -- F-1: the 45 s page rule starts only once the Modal session is attached (a cold sessions.start may take
  -- longer than 45 s, and the page learns the session id only from the reserve answer); a start that hangs is
  -- bounded by the API's 120 s edge_failed and by reserve_until.
  IF v.modal_session_id IS NOT NULL
     AND GREATEST(v.client_heartbeat_at, v.edge_attached_at) < now() - interval '45 seconds' THEN
    PERFORM public._inference_session_finish(p_id, 'client_gone'); RETURN 'client_gone';
  END IF;
  RETURN NULL;
END $$;

CREATE FUNCTION public._inference_used_today_s(p_user_id UUID)
RETURNS NUMERIC LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
  SELECT COALESCE(SUM(CASE WHEN connected_at IS NULL THEN 0
           ELSE GREATEST(0, EXTRACT(EPOCH FROM (LEAST(COALESCE(ended_at, now()), max_end_at) - connected_at)))
         END), 0)
    FROM public.inference_sessions
   WHERE user_id = p_user_id
     AND (created_at AT TIME ZONE 'Europe/Berlin')::DATE = (now() AT TIME ZONE 'Europe/Berlin')::DATE;
$$;

CREATE FUNCTION public.create_inference_session_safe(
  p_user_id        UUID,
  p_training_id    INT,
  p_model_repo     TEXT,
  p_model_revision TEXT,
  p_policy_type    TEXT,
  p_token_hash     TEXT,
  p_max_session_s  INT,
  p_daily_cap_s    INT,
  p_global_cap     INT,
  p_reserve_s      INT
) RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  v_user      public.users%ROWTYPE;
  v_used      NUMERIC;
  v_remaining INT;
  v_live      INT;
  v_len       INT;
  v_id        UUID;
  v_until     TIMESTAMPTZ;
BEGIN
  IF p_max_session_s IS NULL OR p_max_session_s < 60 OR p_max_session_s > 14400
     OR p_daily_cap_s IS NULL OR p_daily_cap_s < 60 OR p_daily_cap_s > 86400
     OR p_global_cap IS NULL OR p_global_cap < 1 OR p_global_cap > 10000
     OR p_reserve_s IS NULL OR p_reserve_s < 30 OR p_reserve_s > 900 THEN
    RAISE EXCEPTION 'invalid cap arguments' USING ERRCODE = '22023';
  END IF;
  SELECT * INTO v_user FROM public.users WHERE id = p_user_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Benutzer nicht gefunden' USING ERRCODE = 'P0002';
  END IF;
  -- one live session per student: the new one supersedes (frees the unique slot, stops its clock)
  PERFORM public._inference_session_finish(s.id, 'superseded')
     FROM public.inference_sessions s
    WHERE s.user_id = p_user_id AND s.status IN ('reserved', 'active');
  v_used := public._inference_used_today_s(p_user_id);
  v_remaining := GREATEST(0, p_daily_cap_s - CEIL(v_used)::INT);
  IF v_remaining < 60 THEN
    RAISE EXCEPTION 'Tageslimit erreicht' USING ERRCODE = 'P0048';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtext('edubotics.inference_sessions.global_cap'));
  SELECT COUNT(*) INTO v_live FROM public.inference_sessions WHERE status IN ('reserved', 'active');
  IF v_live >= p_global_cap THEN
    RAISE EXCEPTION 'Cloud ausgelastet' USING ERRCODE = 'P0049';
  END IF;
  v_len := LEAST(p_max_session_s, v_remaining);
  v_until := now() + make_interval(secs => p_reserve_s);
  INSERT INTO public.inference_sessions (user_id, classroom_id, workgroup_id, training_id, model_repo,
                                         model_revision, policy_type, token_hash, session_s, reserve_until)
  VALUES (p_user_id, v_user.classroom_id, v_user.workgroup_id, p_training_id, p_model_repo,
          p_model_revision, p_policy_type, p_token_hash, v_len, v_until)
  RETURNING id INTO v_id;
  RETURN jsonb_build_object('session_id', v_id, 'session_s', v_len, 'remaining_today_s', v_remaining,
                            'reserve_until', v_until);
END $$;

-- the API stores the Modal session it started for this row. Whatever the row's state, the edge is
-- recorded once, so the ONE terminate path (inference_edges_due) also ends a session that was
-- superseded or ended while sessions.start was still running.
CREATE FUNCTION public.attach_inference_edge(p_session_id UUID, p_modal_session_id TEXT, p_edge_token_enc TEXT)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v public.inference_sessions%ROWTYPE; v_reason TEXT;
BEGIN
  IF p_modal_session_id IS NULL OR p_modal_session_id !~ '^sd-[A-Za-z0-9_-]{1,128}$'
     OR p_edge_token_enc IS NULL OR p_edge_token_enc !~ '^e1\.[0-9a-f]{8}\.[A-Za-z0-9_-]{24,}$'
     OR length(p_edge_token_enc) > 6000 THEN
    RAISE EXCEPTION 'invalid edge arguments' USING ERRCODE = '22023';
  END IF;
  SELECT * INTO v FROM public.inference_sessions WHERE id = p_session_id FOR UPDATE;
  IF NOT FOUND THEN RETURN jsonb_build_object('status', 'missing'); END IF;
  IF v.modal_session_id IS NOT NULL THEN RETURN jsonb_build_object('status', 'conflict'); END IF;
  UPDATE public.inference_sessions
     SET modal_session_id = p_modal_session_id, edge_token_enc = p_edge_token_enc, edge_attached_at = now()
   WHERE id = v.id;
  v_reason := public._inference_session_expire_if_due(v.id);
  IF v_reason IS NOT NULL THEN
    RETURN jsonb_build_object('status', 'ended', 'end_reason', v_reason);
  END IF;
  RETURN jsonb_build_object('status', v.status);
END $$;

-- Start pressed: a FRESH connect token replaces the reservation's (whose plaintext only the API's
-- prefetch ever held); answers the encrypted edge token for the robot. Only while reserved.
CREATE FUNCTION public.arm_inference_session(p_session_id UUID, p_user_id UUID, p_token_hash TEXT)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v public.inference_sessions%ROWTYPE; v_reason TEXT;
BEGIN
  IF p_token_hash IS NULL OR p_token_hash !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'invalid token hash' USING ERRCODE = '22023';
  END IF;
  SELECT * INTO v FROM public.inference_sessions WHERE id = p_session_id AND user_id = p_user_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Sitzung nicht gefunden' USING ERRCODE = 'P0002'; END IF;
  v_reason := public._inference_session_expire_if_due(v.id);
  IF v_reason IS NOT NULL THEN
    RETURN jsonb_build_object('status', 'ended', 'end_reason', v_reason);
  END IF;
  IF v.status = 'active' THEN RETURN jsonb_build_object('status', 'active'); END IF;
  IF v.modal_session_id IS NULL OR v.edge_token_enc IS NULL THEN
    RETURN jsonb_build_object('status', 'not_ready');
  END IF;
  -- F-8: a Start in the last seconds of the window still leaves the robot a minute to connect
  UPDATE public.inference_sessions
     SET token_hash = p_token_hash, client_heartbeat_at = now(),
         reserve_until = GREATEST(reserve_until, now() + interval '60 seconds')
   WHERE id = v.id;
  RETURN jsonb_build_object('status', 'armed', 'edge_token_enc', v.edge_token_enc, 'session_s', v.session_s);
END $$;

CREATE FUNCTION public.end_inference_session(p_session_id UUID, p_user_id UUID, p_reason TEXT)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v public.inference_sessions%ROWTYPE;
BEGIN
  IF p_reason IS NULL OR p_reason NOT IN ('user_stop', 'edge_failed') THEN
    RAISE EXCEPTION 'invalid reason' USING ERRCODE = '22023';
  END IF;
  SELECT * INTO v FROM public.inference_sessions
   WHERE id = p_session_id AND user_id = p_user_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Sitzung nicht gefunden' USING ERRCODE = 'P0002';
  END IF;
  PERFORM public._inference_session_finish(v.id, p_reason);
  SELECT * INTO v FROM public.inference_sessions WHERE id = p_session_id;
  RETURN jsonb_build_object('status', v.status, 'end_reason', v.end_reason,
                            'active_seconds', v.active_seconds);
END $$;

CREATE FUNCTION public.sweep_inference_sessions()
RETURNS INT LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_n INT := 0; r RECORD; v_reason TEXT;
BEGIN
  FOR r IN SELECT id, status, last_server_heartbeat_at FROM public.inference_sessions
            WHERE status IN ('reserved', 'active') FOR UPDATE SKIP LOCKED LOOP
    v_reason := public._inference_session_expire_if_due(r.id);
    IF v_reason IS NULL AND r.status = 'active'
       AND r.last_server_heartbeat_at < now() - interval '60 seconds' THEN
      PERFORM public._inference_session_finish(r.id, 'server_gone');
      v_reason := 'server_gone';
    END IF;
    IF v_reason IS NOT NULL THEN v_n := v_n + 1; END IF;
  END LOOP;
  RETURN v_n;
END $$;

-- ended rows whose Modal session the API still has to terminate (oldest first, on the DB clock). A row
-- the robot never connected to is due at once (nobody waits for an end reason); a connected row only
-- p_min_age_s after it ended, so the container's heartbeat has told the robot WHY first.
CREATE FUNCTION public.inference_edges_due(p_min_age_s INT, p_limit INT)
RETURNS JSONB LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  IF p_min_age_s IS NULL OR p_min_age_s < 0 OR p_min_age_s > 3600
     OR p_limit IS NULL OR p_limit < 1 OR p_limit > 500 THEN
    RAISE EXCEPTION 'invalid arguments' USING ERRCODE = '22023';
  END IF;
  RETURN COALESCE((SELECT jsonb_agg(jsonb_build_object('id', id, 'edge_token_enc', edge_token_enc,
                                    'ended_age_s', floor(EXTRACT(EPOCH FROM (now() - ended_at)))::INT)
                                    ORDER BY ended_at)
                     FROM (SELECT id, edge_token_enc, ended_at FROM public.inference_sessions
                            WHERE status = 'ended' AND edge_token_enc IS NOT NULL
                              AND (connected_at IS NULL OR ended_at <= now() - make_interval(secs => p_min_age_s))
                            ORDER BY ended_at LIMIT p_limit) d), '[]'::jsonb);
END $$;

-- the edge token is erased once its Modal session is terminated (or given up on)
CREATE FUNCTION public.mark_inference_edge_terminated(p_session_id UUID)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  UPDATE public.inference_sessions SET edge_token_enc = NULL, edge_terminated_at = now()
   WHERE id = p_session_id AND status = 'ended' AND edge_token_enc IS NOT NULL;
  RETURN FOUND;
END $$;

-- the four anon RPCs of the Modal app: the connect token AND the trusted Modal session id are the
-- credential; a mismatch answers 'invalid' before anything else (no end reason leaks to a caller
-- without both)
CREATE FUNCTION public.inference_session_peek(p_token TEXT, p_modal_session_id TEXT)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v public.inference_sessions%ROWTYPE; v_reason TEXT;
BEGIN
  IF p_token IS NULL OR p_token !~ '^[A-Za-z0-9_-]{43}$'
     OR p_modal_session_id IS NULL OR p_modal_session_id !~ '^sd-[A-Za-z0-9_-]{1,128}$' THEN
    RETURN jsonb_build_object('status', 'invalid');
  END IF;
  SELECT * INTO v FROM public.inference_sessions
   WHERE token_hash = encode(sha256(convert_to(p_token, 'UTF8')), 'hex') FOR UPDATE;
  IF NOT FOUND OR v.modal_session_id IS DISTINCT FROM p_modal_session_id THEN
    RETURN jsonb_build_object('status', 'invalid');
  END IF;
  v_reason := public._inference_session_expire_if_due(v.id);
  IF v_reason IS NOT NULL THEN
    RETURN jsonb_build_object('status', 'ended', 'end_reason', v_reason);
  END IF;
  RETURN jsonb_build_object('status', v.status, 'session_id', v.id, 'model_repo', v.model_repo,
                            'model_revision', v.model_revision, 'policy_type', v.policy_type);
END $$;

CREATE FUNCTION public.inference_session_connect(p_token TEXT, p_modal_session_id TEXT, p_conn_id TEXT)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v public.inference_sessions%ROWTYPE; v_reason TEXT; v_max TIMESTAMPTZ;
BEGIN
  IF p_token IS NULL OR p_token !~ '^[A-Za-z0-9_-]{43}$'
     OR p_modal_session_id IS NULL OR p_modal_session_id !~ '^sd-[A-Za-z0-9_-]{1,128}$'
     OR p_conn_id IS NULL OR p_conn_id !~ '^[0-9a-f]{32}$' THEN
    RETURN jsonb_build_object('status', 'invalid');
  END IF;
  SELECT * INTO v FROM public.inference_sessions
   WHERE token_hash = encode(sha256(convert_to(p_token, 'UTF8')), 'hex') FOR UPDATE;
  IF NOT FOUND OR v.modal_session_id IS DISTINCT FROM p_modal_session_id THEN
    RETURN jsonb_build_object('status', 'invalid');
  END IF;
  v_reason := public._inference_session_expire_if_due(v.id);
  IF v_reason IS NOT NULL THEN
    RETURN jsonb_build_object('status', 'ended', 'end_reason', v_reason);
  END IF;
  v_max := COALESCE(v.max_end_at, now() + make_interval(secs => v.session_s));   -- the clock starts once
  UPDATE public.inference_sessions
     SET status = 'active', conn_id = p_conn_id,
         connected_at = COALESCE(connected_at, now()), max_end_at = v_max, last_server_heartbeat_at = now()
   WHERE id = v.id;
  RETURN jsonb_build_object('status', 'active', 'session_id', v.id,
    'model_repo', v.model_repo, 'model_revision', v.model_revision, 'policy_type', v.policy_type,
    'max_end_in_s', GREATEST(0, EXTRACT(EPOCH FROM (v_max - now()))));
END $$;

CREATE FUNCTION public.inference_session_heartbeat(p_token TEXT, p_modal_session_id TEXT, p_conn_id TEXT,
                                                   p_stats JSONB)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v public.inference_sessions%ROWTYPE; v_reason TEXT;
BEGIN
  IF p_token IS NULL OR p_token !~ '^[A-Za-z0-9_-]{43}$'
     OR p_modal_session_id IS NULL OR p_modal_session_id !~ '^sd-[A-Za-z0-9_-]{1,128}$'
     OR p_conn_id IS NULL OR p_conn_id !~ '^[0-9a-f]{32}$' THEN
    RETURN jsonb_build_object('status', 'invalid');
  END IF;
  SELECT * INTO v FROM public.inference_sessions
   WHERE token_hash = encode(sha256(convert_to(p_token, 'UTF8')), 'hex') FOR UPDATE;
  IF NOT FOUND OR v.modal_session_id IS DISTINCT FROM p_modal_session_id THEN
    RETURN jsonb_build_object('status', 'invalid');
  END IF;
  v_reason := public._inference_session_expire_if_due(v.id);
  IF v_reason IS NOT NULL THEN
    RETURN jsonb_build_object('status', 'ended', 'end_reason', v_reason);
  END IF;
  IF v.status <> 'active' OR v.conn_id IS DISTINCT FROM p_conn_id THEN
    RETURN jsonb_build_object('status', 'replaced');
  END IF;
  UPDATE public.inference_sessions
     SET last_server_heartbeat_at = now(),
         stats = CASE WHEN p_stats IS NOT NULL AND jsonb_typeof(p_stats) = 'object'
                           AND octet_length(p_stats::text) <= 2048 THEN p_stats ELSE stats END
   WHERE id = v.id;
  RETURN jsonb_build_object('status', 'active',
    'max_end_in_s', GREATEST(0, EXTRACT(EPOCH FROM (v.max_end_at - now()))));
END $$;

CREATE FUNCTION public.inference_session_end(p_token TEXT, p_modal_session_id TEXT, p_conn_id TEXT,
                                             p_reason TEXT, p_stats JSONB)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v public.inference_sessions%ROWTYPE;
BEGIN
  IF p_token IS NULL OR p_token !~ '^[A-Za-z0-9_-]{43}$'
     OR p_modal_session_id IS NULL OR p_modal_session_id !~ '^sd-[A-Za-z0-9_-]{1,128}$'
     OR p_conn_id IS NULL OR p_conn_id !~ '^[0-9a-f]{32}$'
     OR p_reason IS NULL OR p_reason NOT IN ('robot_stop', 'robot_error', 'stall', 'server_error') THEN
    RETURN jsonb_build_object('status', 'invalid');
  END IF;
  SELECT * INTO v FROM public.inference_sessions
   WHERE token_hash = encode(sha256(convert_to(p_token, 'UTF8')), 'hex') FOR UPDATE;
  IF NOT FOUND OR v.modal_session_id IS DISTINCT FROM p_modal_session_id THEN
    RETURN jsonb_build_object('status', 'invalid');
  END IF;
  IF v.status = 'ended' THEN
    RETURN jsonb_build_object('status', 'ended', 'end_reason', v.end_reason);
  END IF;
  IF v.conn_id IS DISTINCT FROM p_conn_id THEN
    RETURN jsonb_build_object('status', 'replaced');
  END IF;
  IF p_stats IS NOT NULL AND jsonb_typeof(p_stats) = 'object' AND octet_length(p_stats::text) <= 2048 THEN
    UPDATE public.inference_sessions SET stats = p_stats WHERE id = v.id;
  END IF;
  PERFORM public._inference_session_finish(v.id, p_reason);
  RETURN jsonb_build_object('status', 'ended', 'end_reason', p_reason);
END $$;

CREATE FUNCTION public.heartbeat_inference_session_client(p_session_id UUID, p_user_id UUID)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v public.inference_sessions%ROWTYPE; v_reason TEXT;
BEGIN
  SELECT * INTO v FROM public.inference_sessions WHERE id = p_session_id AND user_id = p_user_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Sitzung nicht gefunden' USING ERRCODE = 'P0002'; END IF;
  v_reason := public._inference_session_expire_if_due(v.id);
  IF v_reason IS NOT NULL THEN
    RETURN jsonb_build_object('status', 'ended', 'end_reason', v_reason);
  END IF;
  UPDATE public.inference_sessions SET client_heartbeat_at = now() WHERE id = v.id;
  RETURN jsonb_build_object('status', v.status, 'end_reason', NULL,
    'max_end_in_s', CASE WHEN v.max_end_at IS NULL THEN NULL
                         ELSE GREATEST(0, EXTRACT(EPOCH FROM (v.max_end_at - now()))) END);
END $$;

CREATE FUNCTION public.inference_usage(p_user_id UUID, p_daily_cap_s INT)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_used NUMERIC; v_live public.inference_sessions%ROWTYPE;
BEGIN
  v_used := public._inference_used_today_s(p_user_id);
  SELECT * INTO v_live FROM public.inference_sessions WHERE user_id = p_user_id AND status IN ('reserved','active');
  RETURN jsonb_build_object('used_today_s', CEIL(v_used)::INT,
    'remaining_today_s', GREATEST(0, p_daily_cap_s - CEIL(v_used)::INT),
    'live_session_id', CASE WHEN v_live.id IS NULL THEN NULL ELSE v_live.id END);
END $$;

REVOKE ALL ON FUNCTION public._inference_session_finish(UUID, TEXT) FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION public._inference_session_expire_if_due(UUID) FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION public._inference_used_today_s(UUID) FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION public.create_inference_session_safe(UUID, INT, TEXT, TEXT, TEXT, TEXT, INT, INT, INT, INT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.attach_inference_edge(UUID, TEXT, TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.arm_inference_session(UUID, UUID, TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.end_inference_session(UUID, UUID, TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.sweep_inference_sessions() FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.inference_edges_due(INT, INT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.mark_inference_edge_terminated(UUID) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.heartbeat_inference_session_client(UUID, UUID) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.inference_usage(UUID, INT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.inference_session_peek(TEXT, TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.inference_session_connect(TEXT, TEXT, TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.inference_session_heartbeat(TEXT, TEXT, TEXT, JSONB) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.inference_session_end(TEXT, TEXT, TEXT, TEXT, JSONB) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.create_inference_session_safe(UUID, INT, TEXT, TEXT, TEXT, TEXT, INT, INT, INT, INT) TO service_role;
GRANT EXECUTE ON FUNCTION public.attach_inference_edge(UUID, TEXT, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.arm_inference_session(UUID, UUID, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.end_inference_session(UUID, UUID, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.sweep_inference_sessions() TO service_role;
GRANT EXECUTE ON FUNCTION public.inference_edges_due(INT, INT) TO service_role;
GRANT EXECUTE ON FUNCTION public.mark_inference_edge_terminated(UUID) TO service_role;
GRANT EXECUTE ON FUNCTION public.heartbeat_inference_session_client(UUID, UUID) TO service_role;
GRANT EXECUTE ON FUNCTION public.inference_usage(UUID, INT) TO service_role;
GRANT EXECUTE ON FUNCTION public.inference_session_peek(TEXT, TEXT) TO anon, service_role;
GRANT EXECUTE ON FUNCTION public.inference_session_connect(TEXT, TEXT, TEXT) TO anon, service_role;
GRANT EXECUTE ON FUNCTION public.inference_session_heartbeat(TEXT, TEXT, TEXT, JSONB) TO anon, service_role;
GRANT EXECUTE ON FUNCTION public.inference_session_end(TEXT, TEXT, TEXT, TEXT, JSONB) TO anon, service_role;

DO $$
DECLARE v_role TEXT; v_priv TEXT; v_fn TEXT;
BEGIN
  FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
    FOREACH v_priv IN ARRAY ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER'] LOOP
      IF has_table_privilege(v_role, 'public.inference_sessions', v_priv) THEN
        RAISE EXCEPTION '048: % still holds % on public.inference_sessions', v_role, v_priv;
      END IF;
    END LOOP;
    FOREACH v_fn IN ARRAY ARRAY[
        'public._inference_session_finish(uuid, text)',
        'public._inference_session_expire_if_due(uuid)',
        'public._inference_used_today_s(uuid)',
        'public.create_inference_session_safe(uuid, integer, text, text, text, text, integer, integer, integer, integer)',
        'public.attach_inference_edge(uuid, text, text)',
        'public.arm_inference_session(uuid, uuid, text)',
        'public.end_inference_session(uuid, uuid, text)',
        'public.sweep_inference_sessions()',
        'public.inference_edges_due(integer, integer)',
        'public.mark_inference_edge_terminated(uuid)',
        'public.heartbeat_inference_session_client(uuid, uuid)',
        'public.inference_usage(uuid, integer)'] LOOP
      IF has_function_privilege(v_role, v_fn, 'EXECUTE') THEN
        RAISE EXCEPTION '048: % can still execute %', v_role, v_fn;
      END IF;
    END LOOP;
  END LOOP;
  FOREACH v_fn IN ARRAY ARRAY[
      'public.inference_session_peek(text, text)',
      'public.inference_session_connect(text, text, text)',
      'public.inference_session_heartbeat(text, text, text, jsonb)',
      'public.inference_session_end(text, text, text, text, jsonb)'] LOOP
    IF has_function_privilege('authenticated', v_fn, 'EXECUTE') THEN
      RAISE EXCEPTION '048: authenticated can execute %', v_fn;
    END IF;
    IF NOT has_function_privilege('anon', v_fn, 'EXECUTE') THEN
      RAISE EXCEPTION '048: anon cannot execute %', v_fn;
    END IF;
  END LOOP;
  FOREACH v_fn IN ARRAY ARRAY[
      'public._inference_session_finish(uuid, text)',
      'public._inference_session_expire_if_due(uuid)',
      'public._inference_used_today_s(uuid)'] LOOP
    IF has_function_privilege('service_role', v_fn, 'EXECUTE') THEN
      RAISE EXCEPTION '048: service_role can execute the internal %', v_fn;
    END IF;
  END LOOP;
  IF EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public' AND tablename = 'inference_sessions') THEN
    RAISE EXCEPTION '048: public.inference_sessions carries a policy';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_publication_tables WHERE tablename = 'inference_sessions') THEN
    RAISE EXCEPTION '048: public.inference_sessions is in a publication';
  END IF;
END $$;

COMMIT;

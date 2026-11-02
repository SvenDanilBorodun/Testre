-- 052 — the training-hour ledger (credit system v2, owner decisions D1–D11,
-- N1–N2, Q-A–Q-E, OQ-1–OQ-3, R3-1–R3-3). 1 hour = 1 hour of counted GPU training
-- time; every hour lives in exactly one account bucket and every movement is one
-- row of an append-only ledger.
--   * public.trainings gains ten columns (credit_scheme, reserved_hours,
--     charged_hours, settled_at, idempotency_key, stop_requested_at,
--     stop_requested_by_role, lost_seconds, lost_reasons, charge_waiver); every
--     row that exists now is 'derived' (finishes under the old rules), every new
--     row is 'ledger' and must carry its reservation (trainings_ledger_shape).
--     anon/authenticated read reserved_hours, charged_hours,
--     stop_requested_by_role and lost_seconds by NAME (047's rule).
--     service_role loses INSERT (a row is created only by start_training_safe),
--     TRUNCATE (it would skip the delete settlement), TRIGGER and REFERENCES.
--   * public.training_checkpoints gains config_complaints INT[] (R3-2).
--   * four service-role-read-only tables: credit_accounts (one row per teacher,
--     student and group account; free/reserved/spent are a CACHE of the ledger),
--     credit_ledger (append-only: UPDATE, DELETE and TRUNCATE raise P0068 even for
--     the owner), credit_reasons (an admin's free text, erasable, referenced by id)
--     and training_start_refusals. RLS on, no policy, no publication; request
--     roles hold nothing on them or on their identity sequences, service_role
--     holds SELECT only. Every write goes through a SECURITY DEFINER function.
--   * every callable credit function is SECURITY DEFINER with
--     `SET search_path = public` (the readers too: a nested call inside an
--     INVOKER function is checked against service_role, and the internal
--     helpers are revoked from it); stop_training and the changed 049/050
--     functions stay SECURITY INVOKER (049's pattern, they call no helper).
--   * start_training_safe (10 arguments: + p_idempotency_key, p_max_hours) and
--     start_continuation_safe (4: + p_max_hours) reserve 1–3 hours in the same
--     transaction as the row; cancel_request_training (+ p_actor_role) and
--     finish_training (+ p_charge_waiver) are DROPPED and re-created; an older
--     API's named arguments still resolve. adjust_student_credits /
--     adjust_workgroup_credits raise P0069; get_remaining_credits and
--     get_teacher_credit_summary keep their exact return types and read the
--     ledger.
--   * mark_checkpoint_ready (new, SECURITY INVOKER, service_role only) makes a
--     checkpoint ready only for the live running attempt, under the trainings
--     row lock; claim_checkpoint_mirror (049's body + one condition) refuses a
--     training that is not queued, running or succeeded (fix round 2).
--   * finish_training grants the OQ-2 waiver only on proof of a fresh first
--     attempt (P0073 otherwise), and update_training_progress (049's body with
--     ONE line changed, same signature and grants) restarts the stall clock
--     step_advanced_at only when it is NULL, on an attempt's first running write
--     or on a rising step, never on every 'running' write (fix round 2026-11-01).
--   * the settlement is a trigger inside the terminal transaction (and on
--     delete): charge = min(reserved, ceil(train_seconds_used / 3600)), 0 when
--     no attempt ever had a first running write or under the OQ-2 waiver;
--     release = the rest. The closes run AFTER DELETE of a users/workgroups row
--     (the lock order of invariant I9), an operator's classroom move re-parents
--     an account (P0072 while it holds a reservation).
--   * the switch-over (decision 11) creates a zero account for every teacher,
--     every classroom owner, every student with a classroom and every group, and
--     one 0-hour `migrate` row each with the old balance as orientation.
--   * a self-check DO block before COMMIT: any miss aborts the whole migration.
-- New P-codes P0060–P0073 (spec §6; P0073 = finish_training refuses the charge
-- waiver without proof of a fresh first attempt, fix round 2026-11-01).
--
-- Proven on a local `supabase start` stack (Postgres 17,
-- public.ecr.aws/supabase/postgres:17.6.1.106, PostgREST v14.10; a SCRATCH copy
-- of supabase/ with the baseline's runpod_job_id RENAME line guarded, every
-- migration through 051, then a pre-052 world with managed rows running, capped
-- and publishing, cancel-requested and an unmanaged legacy row): 052 applies with
-- „052 self-check passed"; supabase/tests/052_credit_ledger_assertions.sql
-- prints every PASS its header counts; through PostgREST the readers answer 200,
-- every schema-probe body answers its code and writes nothing, and a request
-- role's read of each new table answers 42501.
-- Rollback: rollback/20261028120000_052_credit_ledger_rollback.sql (it refuses
-- while a ledger row is queued/running/cancel_requested; set
-- TRAINING_DISPATCH=legacy, drain, roll the cloud API back, then roll back).
-- 052 is NOT re-runnable after its rollback: the rollback keeps the ledger.
BEGIN;

-- 1. trainings and training_checkpoints columns (spec §3.1) ------------------
ALTER TABLE public.trainings
  ADD COLUMN credit_scheme TEXT NOT NULL DEFAULT 'derived' CHECK (credit_scheme IN ('derived','ledger')),
  ADD COLUMN reserved_hours INT CHECK (reserved_hours BETWEEN 1 AND 3),
  ADD COLUMN charged_hours  INT CHECK (charged_hours  BETWEEN 0 AND 3),
  ADD COLUMN settled_at     TIMESTAMPTZ,
  ADD COLUMN idempotency_key UUID,
  ADD COLUMN stop_requested_at TIMESTAMPTZ,              -- written together with _by_role, once (§4.9)
  ADD COLUMN stop_requested_by_role TEXT CHECK (stop_requested_by_role IN ('student','teacher')),
  ADD COLUMN lost_seconds   INT NOT NULL DEFAULT 0 CHECK (lost_seconds >= 0),   -- [D1]
  ADD COLUMN lost_reasons   TEXT[] NOT NULL DEFAULT '{}',                       -- [Q-B]
  ADD COLUMN charge_waiver  TEXT CHECK (charge_waiver IS NULL OR charge_waiver = 'lerobot_error'); -- [OQ-2]
-- every row that exists at 052 is 'derived' (finishes under the old rules); new rows are 'ledger'
ALTER TABLE public.trainings ALTER COLUMN credit_scheme SET DEFAULT 'ledger';
ALTER TABLE public.trainings ADD CONSTRAINT trainings_ledger_shape
  CHECK (credit_scheme = 'derived' OR reserved_hours IS NOT NULL);
CREATE UNIQUE INDEX trainings_user_idempotency ON public.trainings (user_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;
REVOKE INSERT ON public.trainings FROM service_role;       -- I6 (anon/authenticated lost it in 046)
REVOKE TRUNCATE, TRIGGER, REFERENCES ON public.trainings FROM service_role;   -- a TRUNCATE would skip the delete settlement
-- R3-2: the attempts whose intact train_config complaint the API counted (judged by distinct hosts)
ALTER TABLE public.training_checkpoints ADD COLUMN config_complaints INT[] NOT NULL DEFAULT '{}';
GRANT SELECT (reserved_hours, charged_hours, stop_requested_by_role, lost_seconds)
  ON public.trainings TO anon, authenticated;

-- 2. the four tables (spec §3.2–§3.6) -----------------------------------------
CREATE TABLE public.credit_accounts (
  kind TEXT NOT NULL CHECK (kind IN ('teacher','student','group')),
  owner_id UUID NOT NULL,                     -- users.id or workgroups.id; no FK (survives deletion)
  parent_teacher_id UUID,                     -- the funding teacher; NULL exactly for kind='teacher'
  free     INT NOT NULL DEFAULT 0 CHECK (free >= 0),
  reserved INT NOT NULL DEFAULT 0 CHECK (reserved >= 0),
  spent    INT NOT NULL DEFAULT 0 CHECK (spent >= 0),
  member_hour_limit INT CHECK (member_hour_limit IS NULL OR (kind = 'group' AND member_hour_limit BETWEEN 1 AND 1000)),
  closed_at  TIMESTAMPTZ,                     -- owner deleted; never reopened
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (kind, owner_id),
  CHECK ((kind = 'teacher') = (parent_teacher_id IS NULL))
);
CREATE INDEX credit_accounts_parent ON public.credit_accounts (parent_teacher_id) WHERE parent_teacher_id IS NOT NULL;

CREATE TABLE public.credit_ledger (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  kind TEXT NOT NULL CHECK (kind IN ('grant','revoke','assign','reclaim','reserve','charge','release','migrate',
                                     'compensate','limit_change')),
  hours INT NOT NULL CHECK (hours >= 0),
  from_kind TEXT, from_id UUID, from_bucket TEXT,
  to_kind TEXT,   to_id UUID,   to_bucket TEXT,
  training_id INT,              -- no FK: survives the training's deletion
  member_id UUID,               -- the student a reserve/charge/release/compensate is about (group accounts!)
  actor_id UUID,                -- admin / teacher / student who caused it; NULL = the system
  actor_role TEXT NOT NULL CHECK (actor_role IN ('admin','teacher','student','system')),
  request_id UUID,              -- client idempotency (grant, revoke, assign, reclaim, reserve, compensate)
  reason_id BIGINT,             -- → credit_reasons.id; NO foreign key: the reason row is erasable (I12)
  meta JSONB NOT NULL DEFAULT '{}'::jsonb,
  CONSTRAINT credit_ledger_shape CHECK (
       (kind = 'grant'   AND from_kind IS NULL AND to_kind = 'teacher' AND to_bucket = 'free' AND hours > 0)
    OR (kind = 'revoke'  AND from_bucket = 'free' AND to_kind IS NULL AND hours > 0      -- I14: a child's only by the system
        AND (from_kind = 'teacher' OR (from_kind IN ('student','group') AND actor_role = 'system')))
    OR (kind = 'assign'  AND from_kind = 'teacher' AND from_bucket = 'free' AND to_kind IN ('student','group')
        AND to_bucket = 'free' AND hours > 0)
    OR (kind = 'reclaim' AND from_kind IN ('student','group') AND from_bucket = 'free' AND to_kind = 'teacher'
        AND to_bucket = 'free' AND hours > 0)
    OR (kind = 'reserve' AND from_kind IN ('student','group') AND from_bucket = 'free' AND to_kind = from_kind
        AND to_id = from_id AND to_bucket = 'reserved' AND hours BETWEEN 1 AND 3
        AND training_id IS NOT NULL AND member_id IS NOT NULL)
    OR (kind = 'charge'  AND from_kind IN ('student','group') AND from_bucket = 'reserved' AND to_kind IS NULL
        AND hours <= 3 AND training_id IS NOT NULL AND member_id IS NOT NULL)
    OR (kind = 'release' AND from_kind IN ('student','group') AND from_bucket = 'reserved' AND to_kind = from_kind
        AND to_id = from_id AND to_bucket = 'free' AND hours <= 3 AND training_id IS NOT NULL AND member_id IS NOT NULL)
    OR (kind = 'migrate' AND hours = 0 AND from_kind IS NULL AND to_kind IN ('teacher','student','group')
        AND to_bucket = 'free')
    OR (kind = 'compensate' AND from_kind IS NULL AND to_kind IN ('student','group') AND to_bucket = 'free'
        AND hours BETWEEN 1 AND 100 AND actor_role = 'admin' AND actor_id IS NOT NULL AND reason_id IS NOT NULL)
    OR (kind = 'limit_change' AND hours = 0 AND from_kind IS NULL AND to_kind = 'group' AND to_bucket IS NULL
        AND actor_role = 'teacher' AND meta ? 'old' AND meta ? 'new')
  )
);
CREATE UNIQUE INDEX credit_ledger_one_reserve ON public.credit_ledger (training_id) WHERE kind = 'reserve';
CREATE UNIQUE INDEX credit_ledger_one_charge  ON public.credit_ledger (training_id) WHERE kind = 'charge';
CREATE UNIQUE INDEX credit_ledger_one_release ON public.credit_ledger (training_id) WHERE kind = 'release';
CREATE UNIQUE INDEX credit_ledger_one_close_return ON public.credit_ledger (training_id)
  WHERE kind IN ('reclaim','revoke') AND training_id IS NOT NULL;
CREATE UNIQUE INDEX credit_ledger_request ON public.credit_ledger (request_id) WHERE request_id IS NOT NULL;
CREATE INDEX credit_ledger_from   ON public.credit_ledger (from_kind, from_id, id DESC);
CREATE INDEX credit_ledger_to     ON public.credit_ledger (to_kind, to_id, id DESC);
CREATE INDEX credit_ledger_member ON public.credit_ledger (member_id, id DESC) WHERE member_id IS NOT NULL;

CREATE OR REPLACE FUNCTION public.credit_ledger_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'credit_ledger is append-only (%)', TG_OP USING ERRCODE = 'P0068'; END $$;
CREATE TRIGGER credit_ledger_no_change BEFORE UPDATE OR DELETE ON public.credit_ledger
  FOR EACH ROW EXECUTE FUNCTION public.credit_ledger_immutable();
CREATE TRIGGER credit_ledger_no_truncate BEFORE TRUNCATE ON public.credit_ledger
  FOR EACH STATEMENT EXECUTE FUNCTION public.credit_ledger_immutable();

-- free text (an admin's reason) never sits in the immutable ledger (I12)
CREATE TABLE public.credit_reasons (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  text TEXT NOT NULL CHECK (char_length(text) BETWEEN 1 AND 200),
  subject_kind TEXT NOT NULL CHECK (subject_kind IN ('teacher','student','group')),
  subject_id UUID NOT NULL,                  -- the account the reason is about
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX credit_reasons_subject ON public.credit_reasons (subject_kind, subject_id);

CREATE TABLE public.training_start_refusals (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  at TIMESTAMPTZ NOT NULL DEFAULT now(),
  user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  request_kind TEXT NOT NULL CHECK (request_kind IN ('start','continue')),
  code TEXT NOT NULL CHECK (code IN ('role','policy','no_hours','member_limit')),
  model_type TEXT CHECK (model_type IS NULL OR model_type ~ '^[a-z0-9_]{1,32}$')
);
CREATE INDEX training_start_refusals_user ON public.training_start_refusals (user_id, id DESC);

ALTER TABLE public.credit_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.credit_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.credit_reasons ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.training_start_refusals ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.credit_accounts, public.credit_ledger, public.credit_reasons, public.training_start_refusals
  FROM PUBLIC, anon, authenticated, service_role;
GRANT SELECT ON public.credit_accounts, public.credit_ledger, public.credit_reasons, public.training_start_refusals
  TO service_role;
-- Supabase's default privileges give anon/authenticated USAGE+UPDATE on new sequences
DO $$ DECLARE q TEXT; BEGIN
  FOREACH q IN ARRAY ARRAY['public.credit_ledger','public.credit_reasons','public.training_start_refusals'] LOOP
    EXECUTE format('REVOKE ALL ON SEQUENCE %s FROM PUBLIC, anon, authenticated, service_role',
                   pg_get_serial_sequence(q, 'id'));
  END LOOP;
END $$;

-- 3. internal functions (spec §4.1, §4.2, §5) ---------------------------------
-- One ledger row plus the cache of both accounts. Callers hold the locks.
CREATE OR REPLACE FUNCTION public._credit_post(p_kind TEXT, p_hours INT, p_fk TEXT, p_fid UUID, p_fb TEXT,
  p_tk TEXT, p_tid UUID, p_tb TEXT, p_training INT, p_member UUID, p_actor UUID, p_role TEXT, p_request UUID,
  p_reason_id BIGINT, p_meta JSONB)
RETURNS BIGINT LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_id BIGINT;
BEGIN
  INSERT INTO credit_ledger(kind, hours, from_kind, from_id, from_bucket, to_kind, to_id, to_bucket, training_id,
                            member_id, actor_id, actor_role, request_id, reason_id, meta)
  VALUES (p_kind, p_hours, p_fk, p_fid, p_fb, p_tk, p_tid, p_tb, p_training, p_member, p_actor, p_role, p_request,
          p_reason_id, COALESCE(p_meta, '{}'::jsonb))
  RETURNING id INTO v_id;
  IF p_hours > 0 AND p_fk IS NOT NULL THEN
    UPDATE credit_accounts
       SET free     = free     - CASE WHEN p_fb = 'free' THEN p_hours ELSE 0 END,
           reserved = reserved - CASE WHEN p_fb = 'reserved' THEN p_hours ELSE 0 END,
           spent    = spent    + CASE WHEN p_kind = 'charge' THEN p_hours ELSE 0 END
     WHERE kind = p_fk AND owner_id = p_fid;
    IF NOT FOUND THEN RAISE EXCEPTION 'no credit account %/%', p_fk, p_fid USING ERRCODE = 'P0002'; END IF;
  END IF;
  IF p_hours > 0 AND p_tk IS NOT NULL THEN
    UPDATE credit_accounts
       SET free     = free     + CASE WHEN p_tb = 'free' THEN p_hours ELSE 0 END,
           reserved = reserved + CASE WHEN p_tb = 'reserved' THEN p_hours ELSE 0 END
     WHERE kind = p_tk AND owner_id = p_tid;
    IF NOT FOUND THEN RAISE EXCEPTION 'no credit account %/%', p_tk, p_tid USING ERRCODE = 'P0002'; END IF;
  END IF;
  RETURN v_id;
END $$;

-- One idempotency rule for every writer: TRUE = an exact replay (write nothing);
-- P0071 = the request id was used for another change.
CREATE OR REPLACE FUNCTION public._credit_request_replay(p_request UUID, p_kind TEXT, p_hours INT,
  p_fk TEXT, p_fid UUID, p_tk TEXT, p_tid UUID, p_actor UUID)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v credit_ledger%ROWTYPE;
BEGIN
  IF p_request IS NULL THEN RETURN false; END IF;
  SELECT * INTO v FROM credit_ledger l WHERE l.request_id = p_request;
  IF NOT FOUND THEN RETURN false; END IF;
  IF v.kind = p_kind AND v.hours = p_hours
     AND v.from_kind IS NOT DISTINCT FROM p_fk AND v.from_id IS NOT DISTINCT FROM p_fid
     AND v.to_kind IS NOT DISTINCT FROM p_tk AND v.to_id IS NOT DISTINCT FROM p_tid
     AND v.actor_id IS NOT DISTINCT FROM p_actor THEN
    RETURN true;
  END IF;
  RAISE EXCEPTION 'request id already used for another change' USING ERRCODE = 'P0071';
END $$;

CREATE OR REPLACE FUNCTION public._credit_reason(p_text TEXT, p_kind TEXT, p_id UUID)
RETURNS BIGINT LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v BIGINT;
BEGIN
  IF p_text IS NULL OR btrim(p_text) = '' THEN RETURN NULL; END IF;
  INSERT INTO credit_reasons(text, subject_kind, subject_id) VALUES (btrim(p_text), p_kind, p_id) RETURNING id INTO v;
  RETURN v;
END $$;

-- I13: a child account is never created without its funding teacher's account
CREATE OR REPLACE FUNCTION public._credit_child_account(p_kind TEXT, p_owner UUID, p_parent UUID)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  INSERT INTO credit_accounts(kind, owner_id) VALUES ('teacher', p_parent) ON CONFLICT DO NOTHING;
  INSERT INTO credit_accounts(kind, owner_id, parent_teacher_id) VALUES (p_kind, p_owner, p_parent) ON CONFLICT DO NOTHING;
END $$;

-- I14: free hours leaving a child go to its funding teacher while that account is
-- open, else they leave the system as a system revoke (never stranded)
CREATE OR REPLACE FUNCTION public._credit_return_free(p_kind TEXT, p_owner UUID, p_parent UUID, p_hours INT,
  p_training INT, p_member UUID, p_why TEXT)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  IF p_hours IS NULL OR p_hours <= 0 THEN RETURN; END IF;
  IF EXISTS (SELECT 1 FROM credit_accounts a WHERE a.kind = 'teacher' AND a.owner_id = p_parent AND a.closed_at IS NULL) THEN
    PERFORM _credit_post('reclaim', p_hours, p_kind, p_owner, 'free', 'teacher', p_parent, 'free', p_training, p_member,
                         NULL, 'system', NULL, NULL, jsonb_build_object('why', p_why));
  ELSE
    PERFORM _credit_post('revoke', p_hours, p_kind, p_owner, 'free', NULL, NULL, NULL, p_training, p_member,
                         NULL, 'system', NULL, NULL, jsonb_build_object('why', p_why, 'parent', 'closed'));
  END IF;
END $$;

-- I9: a start or a continuation takes the workgroups row (KEY SHARE) BEFORE the
-- users row (FOR UPDATE). Holding the users row, its workgroup_id is final: NULL
-- (the group was deleted meanwhile) → personal; another group (moved meanwhile)
-- → that group's key share now (R17). Raises nothing but P0002.
CREATE OR REPLACE FUNCTION public._credit_lock_user(p_user_id UUID)
RETURNS public.users LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_wg UUID; v_u public.users%ROWTYPE;
BEGIN
  SELECT u.workgroup_id INTO v_wg FROM users u WHERE u.id = p_user_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'User profile not found' USING ERRCODE = 'P0002'; END IF;
  IF v_wg IS NOT NULL THEN
    PERFORM 1 FROM workgroups g WHERE g.id = v_wg FOR KEY SHARE;
  END IF;
  SELECT * INTO v_u FROM users u WHERE u.id = p_user_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'User profile not found' USING ERRCODE = 'P0002'; END IF;
  IF v_u.workgroup_id IS NOT NULL AND v_u.workgroup_id IS DISTINCT FROM v_wg THEN
    PERFORM 1 FROM workgroups g WHERE g.id = v_u.workgroup_id FOR KEY SHARE;
  END IF;
  RETURN v_u;
END $$;

-- §4.2: the ONE rule both the quota and the start use (H7). A compensation that
-- names one of the member's trainings gives that headroom back [Q-D].
CREATE OR REPLACE FUNCTION public._credit_startable(p_user_id UUID, p_workgroup_id UUID, p_lock BOOLEAN)
RETURNS TABLE(o_kind TEXT, o_account_id UUID, o_free INT, o_reserved INT, o_spent INT, o_member_limit INT,
              o_member_used INT, o_member_left INT)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_kind TEXT; v_acct UUID; a credit_accounts%ROWTYPE; v_used INT;
BEGIN
  IF p_workgroup_id IS NOT NULL THEN v_kind := 'group'; v_acct := p_workgroup_id;
  ELSE v_kind := 'student'; v_acct := p_user_id; END IF;
  IF p_lock THEN
    SELECT * INTO a FROM credit_accounts c WHERE c.kind = v_kind AND c.owner_id = v_acct FOR UPDATE;
  ELSE
    SELECT * INTO a FROM credit_accounts c WHERE c.kind = v_kind AND c.owner_id = v_acct;
  END IF;
  IF a.kind IS NULL OR a.closed_at IS NOT NULL THEN
    RETURN QUERY SELECT v_kind, v_acct, 0, 0, 0, NULL::int, CASE WHEN v_kind = 'group' THEN 0 END, NULL::int;
    RETURN;
  END IF;
  v_used := NULL;
  IF v_kind = 'group' THEN
    SELECT GREATEST(COALESCE(SUM(CASE WHEN l.kind = 'reserve' THEN l.hours
                                      WHEN l.kind IN ('release','compensate') THEN -l.hours ELSE 0 END), 0), 0)
      INTO v_used
      FROM credit_ledger l
     WHERE l.member_id = p_user_id AND l.kind IN ('reserve','release','compensate')
       AND ((l.kind IN ('reserve','release') AND l.from_kind = 'group' AND l.from_id = v_acct)
         OR (l.kind = 'compensate' AND l.to_kind = 'group' AND l.to_id = v_acct));
  END IF;
  RETURN QUERY SELECT v_kind, v_acct, a.free, a.reserved, a.spent, a.member_hour_limit, v_used,
                      CASE WHEN a.member_hour_limit IS NULL THEN NULL ELSE a.member_hour_limit - v_used END;
END $$;

-- the current attempt's seconds, from its first running write to LEAST(now, the first stop request)
CREATE OR REPLACE FUNCTION public._credit_open_seconds(p_training_id INT, p_current INT, p_stop TIMESTAMPTZ)
RETURNS INT LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT COALESCE((
    SELECT GREATEST(floor(EXTRACT(EPOCH FROM (LEAST(now(), COALESCE(p_stop, now())) - a.first_heartbeat_at)))::int, 0)
      FROM training_attempts a
     WHERE a.training_id = p_training_id AND a.attempt_no = p_current
       AND a.state IN ('placing','starting','running') AND a.first_heartbeat_at IS NOT NULL), 0)
$$;

-- what a charge row carries for the history [Q-B]
CREATE OR REPLACE FUNCTION public._credit_meta(p_status TEXT, p_lost INT, p_reasons TEXT[], p_stopped_by TEXT)
RETURNS JSONB LANGUAGE sql IMMUTABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT jsonb_build_object('status', p_status, 'stopped_by', p_stopped_by, 'lost_seconds', p_lost,
                            'losses', to_jsonb(COALESCE(p_reasons, '{}'::text[])))
$$;

-- I5/I7: charge + release exactly once, decided under the account locks
CREATE OR REPLACE FUNCTION public._credit_settle(p_training_id INT, p_seconds INT, p_meta JSONB, p_waiver TEXT DEFAULT NULL)
RETURNS INT LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_r credit_ledger%ROWTYPE; v_parent UUID; v_closed TIMESTAMPTZ; v_charge INT; v_hb BOOLEAN;
BEGIN
  SELECT * INTO v_r FROM credit_ledger l WHERE l.kind = 'reserve' AND l.training_id = p_training_id;
  IF NOT FOUND THEN RETURN NULL; END IF;                       -- a 'derived' row
  SELECT c.parent_teacher_id INTO v_parent FROM credit_accounts c WHERE c.kind = v_r.from_kind AND c.owner_id = v_r.from_id;
  IF v_parent IS NOT NULL THEN
    PERFORM 1 FROM credit_accounts c WHERE c.kind = 'teacher' AND c.owner_id = v_parent FOR UPDATE;
  END IF;
  SELECT c.closed_at INTO v_closed FROM credit_accounts c WHERE c.kind = v_r.from_kind AND c.owner_id = v_r.from_id FOR UPDATE;
  IF EXISTS (SELECT 1 FROM credit_ledger l WHERE l.kind = 'charge' AND l.training_id = p_training_id) THEN
    RETURN NULL;                                               -- already settled (decided after the locks)
  END IF;
  v_hb := EXISTS (SELECT 1 FROM training_attempts a WHERE a.training_id = p_training_id AND a.first_heartbeat_at IS NOT NULL);
  v_charge := CASE WHEN v_hb AND p_waiver IS NULL
                   THEN LEAST(v_r.hours, CEIL(GREATEST(COALESCE(p_seconds, 0), 0) / 3600.0)::int) ELSE 0 END;
  PERFORM _credit_post('charge', v_charge, v_r.from_kind, v_r.from_id, 'reserved', NULL, NULL, NULL, p_training_id,
                       v_r.member_id, NULL, 'system', NULL, NULL,
                       COALESCE(p_meta, '{}'::jsonb) || jsonb_build_object('seconds', p_seconds)
                         || CASE WHEN p_waiver IS NULL THEN '{}'::jsonb ELSE jsonb_build_object('waiver', p_waiver) END);
  PERFORM _credit_post('release', v_r.hours - v_charge, v_r.from_kind, v_r.from_id, 'reserved', v_r.from_kind, v_r.from_id,
                       'free', p_training_id, v_r.member_id, NULL, 'system', NULL, NULL, NULL);
  IF v_closed IS NOT NULL AND v_r.hours - v_charge > 0 AND v_parent IS NOT NULL THEN
    PERFORM _credit_return_free(v_r.from_kind, v_r.from_id, v_parent, v_r.hours - v_charge, p_training_id,
                                v_r.member_id, 'closed');
  END IF;
  RETURN v_charge;
END $$;

-- a deleted owner's account: reasons erased (I12), free hours moved, closed
CREATE OR REPLACE FUNCTION public._credit_close(p_kind TEXT, p_owner UUID)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_parent UUID; v_free INT;
BEGIN
  DELETE FROM credit_reasons r WHERE r.subject_kind = p_kind AND r.subject_id = p_owner;
  SELECT c.parent_teacher_id INTO v_parent FROM credit_accounts c WHERE c.kind = p_kind AND c.owner_id = p_owner;
  IF NOT FOUND THEN RETURN; END IF;
  IF v_parent IS NOT NULL THEN
    PERFORM 1 FROM credit_accounts c WHERE c.kind = 'teacher' AND c.owner_id = v_parent FOR UPDATE;
  END IF;
  SELECT c.free INTO v_free FROM credit_accounts c
   WHERE c.kind = p_kind AND c.owner_id = p_owner AND c.closed_at IS NULL FOR UPDATE;
  IF NOT FOUND THEN RETURN; END IF;
  IF v_free > 0 AND v_parent IS NOT NULL THEN
    PERFORM _credit_return_free(p_kind, p_owner, v_parent, v_free, NULL, NULL, 'closed');
  ELSIF v_free > 0 THEN                                        -- a teacher's own free hours [D8]
    PERFORM _credit_post('revoke', v_free, 'teacher', p_owner, 'free', NULL, NULL, NULL, NULL, NULL, NULL, 'system',
                         NULL, NULL, jsonb_build_object('why', 'closed'));
  END IF;
  UPDATE credit_accounts c SET closed_at = now() WHERE c.kind = p_kind AND c.owner_id = p_owner;
END $$;

-- 4. start_training_safe and start_continuation_safe (replaced, spec §4.3/§4.4)
DROP FUNCTION public.start_training_safe(UUID, TEXT, TEXT, TEXT, JSONB, INT, UUID, BOOLEAN);
CREATE OR REPLACE FUNCTION public.start_training_safe(p_user_id UUID, p_dataset_name TEXT, p_model_name TEXT,
  p_model_type TEXT, p_training_params JSONB, p_total_steps INT, p_worker_token UUID, p_managed BOOLEAN DEFAULT false,
  p_idempotency_key UUID DEFAULT NULL, p_max_hours INT DEFAULT 3)
RETURNS TABLE(training_id INT, remaining INT, reserved_hours INT, replay BOOLEAN)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_u users%ROWTYPE; s RECORD; v_n INT; v_id INT; v_old trainings%ROWTYPE;
BEGIN
  v_u := _credit_lock_user(p_user_id);                          -- group KEY SHARE → users FOR UPDATE (I9)
  IF v_u.role <> 'student' THEN RAISE EXCEPTION 'only students start trainings' USING ERRCODE = 'P0060'; END IF;
  IF lower(COALESCE(p_model_type, '')) <> 'act' OR NOT COALESCE(p_managed, false) THEN
    RAISE EXCEPTION 'only managed ACT trainings start' USING ERRCODE = 'P0061';
  END IF;
  IF p_idempotency_key IS NOT NULL THEN
    SELECT * INTO v_old FROM trainings t WHERE t.user_id = p_user_id AND t.idempotency_key = p_idempotency_key;
    IF FOUND THEN
      IF v_old.dataset_name IS DISTINCT FROM p_dataset_name
         OR lower(v_old.model_type) IS DISTINCT FROM lower(p_model_type)
         OR (v_old.training_params->>'steps') IS DISTINCT FROM (p_training_params->>'steps') THEN
        RAISE EXCEPTION 'idempotency key already used for another start' USING ERRCODE = 'P0071';
      END IF;
      RETURN QUERY SELECT v_old.id, NULL::int, v_old.reserved_hours, true;
      RETURN;
    END IF;
    IF EXISTS (SELECT 1 FROM credit_ledger l WHERE l.request_id = p_idempotency_key) THEN
      RAISE EXCEPTION 'idempotency key already used for another change' USING ERRCODE = 'P0071';
    END IF;
  END IF;
  SELECT * INTO s FROM _credit_startable(p_user_id, v_u.workgroup_id, true);
  v_n := LEAST(3, GREATEST(1, COALESCE(p_max_hours, 3)), s.o_free, COALESCE(s.o_member_left, 3));
  IF s.o_member_left IS NOT NULL AND s.o_member_left < 1 AND s.o_free >= 1 THEN
    RAISE EXCEPTION 'group member hour limit reached' USING ERRCODE = 'P0063';
  END IF;
  IF v_n < 1 THEN RAISE EXCEPTION 'No training credits remaining' USING ERRCODE = 'P0003'; END IF;
  INSERT INTO trainings(user_id, workgroup_id, status, dataset_name, model_name, model_type, training_params,
                        total_steps, worker_token, managed, credit_scheme, reserved_hours, idempotency_key)
  VALUES (p_user_id, v_u.workgroup_id, 'queued', p_dataset_name, p_model_name, p_model_type, p_training_params,
          p_total_steps, p_worker_token, true, 'ledger', v_n, p_idempotency_key)
  RETURNING id INTO v_id;
  PERFORM _credit_post('reserve', v_n, s.o_kind, s.o_account_id, 'free', s.o_kind, s.o_account_id, 'reserved', v_id,
                       p_user_id, p_user_id, 'student', p_idempotency_key, NULL, NULL);
  RETURN QUERY SELECT v_id, s.o_free - v_n, v_n, false;
END $$;

-- 050's body, except: the users row is locked BEFORE the parent row (I9), the
-- child is a new reservation (p_max_hours), and its reserved hours are returned.
DROP FUNCTION public.start_continuation_safe(UUID, INT, TEXT);
CREATE OR REPLACE FUNCTION public.start_continuation_safe(p_user_id UUID, p_parent_id INT, p_note TEXT,
  p_max_hours INT DEFAULT 3)
RETURNS TABLE(o_training_id INT, o_remaining INT, o_reserved_hours INT)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  v_p         public.trainings%ROWTYPE;
  v_child     INT;
  v_remaining INT;
  v_reserved  INT;
BEGIN
  PERFORM public._credit_lock_user(p_user_id);
  SELECT * INTO v_p FROM public.trainings t WHERE t.id = p_parent_id AND t.user_id = p_user_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'training % not found', p_parent_id USING ERRCODE = 'P0002';
  END IF;
  IF NOT v_p.managed OR v_p.status <> 'succeeded' OR v_p.capped_step IS NULL
     OR v_p.capped_step >= v_p.total_steps OR v_p.continuable_until IS NULL THEN
    RAISE EXCEPTION 'training % is not continuable', p_parent_id USING ERRCODE = 'P0052';
  END IF;
  IF v_p.continuable_until <= now() THEN
    RAISE EXCEPTION 'training % can no longer be continued', p_parent_id USING ERRCODE = 'P0053';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.training_kept_checkpoints k
                  WHERE k.training_id = p_parent_id AND k.state = 'kept' AND k.step = v_p.capped_step) THEN
    RAISE EXCEPTION 'training % has no kept checkpoint', p_parent_id USING ERRCODE = 'P0052';
  END IF;
  IF EXISTS (SELECT 1 FROM public.trainings c
              WHERE c.continued_from = p_parent_id AND c.status NOT IN ('failed','canceled')) THEN
    RAISE EXCEPTION 'training % is already being continued', p_parent_id USING ERRCODE = 'P0054';
  END IF;
  SELECT s.training_id, s.remaining, s.reserved_hours INTO v_child, v_remaining, v_reserved
    FROM public.start_training_safe(p_user_id, v_p.dataset_name, v_p.model_name, v_p.model_type,
                                    v_p.training_params, v_p.total_steps, NULL, true, NULL, p_max_hours) AS s;
  UPDATE public.trainings c
     SET continued_from = p_parent_id, current_step = v_p.capped_step, status_note = p_note,
         dataset_fps = v_p.dataset_fps
   WHERE c.id = v_child;
  RETURN QUERY SELECT v_child, v_remaining, v_reserved;
END $$;

-- 5. callable credit functions (spec §4.5–§4.13) -------------------------------
-- §4.5 grant / revoke: deltas only (decision 8); a revoke never touches assigned hours
CREATE OR REPLACE FUNCTION public.credit_admin_grant(p_admin_id UUID, p_teacher_id UUID, p_hours INT, p_reason TEXT,
  p_request_id UUID)
RETURNS INT LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_free INT;
BEGIN
  IF p_hours IS NULL OR p_hours < 1 OR p_hours > 10000
     OR (p_reason IS NOT NULL AND char_length(btrim(p_reason)) > 200) THEN
    RAISE EXCEPTION 'invalid grant' USING ERRCODE = '22023';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM users u WHERE u.id = p_admin_id AND u.role = 'admin') THEN
    RAISE EXCEPTION 'not an admin' USING ERRCODE = 'P0067';
  END IF;
  PERFORM 1 FROM users u WHERE u.id = p_teacher_id AND u.role = 'teacher' FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'teacher not found' USING ERRCODE = 'P0002'; END IF;
  INSERT INTO credit_accounts(kind, owner_id) VALUES ('teacher', p_teacher_id) ON CONFLICT DO NOTHING;
  SELECT c.free INTO v_free FROM credit_accounts c WHERE c.kind = 'teacher' AND c.owner_id = p_teacher_id FOR UPDATE;
  IF _credit_request_replay(p_request_id, 'grant', p_hours, NULL, NULL, 'teacher', p_teacher_id, p_admin_id) THEN
    RETURN v_free;
  END IF;
  PERFORM _credit_post('grant', p_hours, NULL, NULL, NULL, 'teacher', p_teacher_id, 'free', NULL, NULL, p_admin_id,
                       'admin', p_request_id, _credit_reason(p_reason, 'teacher', p_teacher_id), NULL);
  SELECT c.free INTO v_free FROM credit_accounts c WHERE c.kind = 'teacher' AND c.owner_id = p_teacher_id;
  RETURN v_free;
END $$;

CREATE OR REPLACE FUNCTION public.credit_admin_revoke(p_admin_id UUID, p_teacher_id UUID, p_hours INT, p_reason TEXT,
  p_request_id UUID)
RETURNS INT LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_free INT;
BEGIN
  IF p_hours IS NULL OR p_hours < 1 OR p_hours > 10000
     OR (p_reason IS NOT NULL AND char_length(btrim(p_reason)) > 200) THEN
    RAISE EXCEPTION 'invalid revoke' USING ERRCODE = '22023';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM users u WHERE u.id = p_admin_id AND u.role = 'admin') THEN
    RAISE EXCEPTION 'not an admin' USING ERRCODE = 'P0067';
  END IF;
  PERFORM 1 FROM users u WHERE u.id = p_teacher_id AND u.role = 'teacher' FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'teacher not found' USING ERRCODE = 'P0002'; END IF;
  SELECT c.free INTO v_free FROM credit_accounts c WHERE c.kind = 'teacher' AND c.owner_id = p_teacher_id FOR UPDATE;
  IF NOT FOUND THEN v_free := 0; END IF;
  IF _credit_request_replay(p_request_id, 'revoke', p_hours, 'teacher', p_teacher_id, NULL, NULL, p_admin_id) THEN
    RETURN v_free;
  END IF;
  IF v_free < p_hours THEN
    RAISE EXCEPTION 'only % free', v_free USING ERRCODE = 'P0062', DETAIL = v_free::text;
  END IF;
  PERFORM _credit_post('revoke', p_hours, 'teacher', p_teacher_id, 'free', NULL, NULL, NULL, NULL, NULL, p_admin_id,
                       'admin', p_request_id, _credit_reason(p_reason, 'teacher', p_teacher_id), NULL);
  SELECT c.free INTO v_free FROM credit_accounts c WHERE c.kind = 'teacher' AND c.owner_id = p_teacher_id;
  RETURN v_free;
END $$;

-- §4.5a an admin's compensation, from outside the teacher pool [D4]; a named
-- training decides the account and gives the member's headroom back [Q-D]
CREATE OR REPLACE FUNCTION public.credit_admin_compensate(p_admin_id UUID, p_target_kind TEXT, p_target_id UUID,
  p_hours INT, p_reason TEXT, p_training_id INT DEFAULT NULL, p_request_id UUID DEFAULT NULL)
RETURNS INT LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_closed TIMESTAMPTZ; v_u users%ROWTYPE; v_free INT; v_cls UUID; v_member UUID; v_r credit_ledger%ROWTYPE;
BEGIN
  IF p_hours IS NULL OR p_hours < 1 OR p_hours > 100 OR p_target_kind IS NULL
     OR p_target_kind NOT IN ('student','group')
     OR p_reason IS NULL OR char_length(btrim(p_reason)) NOT BETWEEN 1 AND 200 THEN
    RAISE EXCEPTION 'invalid compensation' USING ERRCODE = '22023';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM users u WHERE u.id = p_admin_id AND u.role = 'admin') THEN
    RAISE EXCEPTION 'not an admin' USING ERRCODE = 'P0067';
  END IF;
  IF p_training_id IS NOT NULL THEN
    SELECT * INTO v_r FROM credit_ledger l WHERE l.kind = 'reserve' AND l.training_id = p_training_id;
    IF NOT FOUND OR v_r.from_kind <> p_target_kind OR v_r.from_id <> p_target_id THEN
      RAISE EXCEPTION 'training % was not paid from this account', p_training_id USING ERRCODE = 'P0070';
    END IF;
    v_member := v_r.member_id;
    IF p_target_kind = 'student' THEN
      PERFORM 1 FROM users u WHERE u.id = p_target_id FOR KEY SHARE;
    ELSE
      PERFORM 1 FROM workgroups g WHERE g.id = p_target_id FOR KEY SHARE;
    END IF;
    IF NOT FOUND THEN RAISE EXCEPTION 'compensation target not found' USING ERRCODE = 'P0002'; END IF;
  ELSIF p_target_kind = 'student' THEN
    SELECT * INTO v_u FROM users u WHERE u.id = p_target_id AND u.role = 'student' FOR KEY SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'compensation target not found' USING ERRCODE = 'P0002'; END IF;
    IF v_u.workgroup_id IS NOT NULL THEN
      RAISE EXCEPTION 'student is in a workgroup' USING ERRCODE = 'P0023';
    END IF;
    IF v_u.classroom_id IS NULL THEN
      RAISE EXCEPTION 'student has no classroom' USING ERRCODE = 'P0066';
    END IF;
    v_cls := v_u.classroom_id;
  ELSE
    SELECT g.classroom_id INTO v_cls FROM workgroups g WHERE g.id = p_target_id FOR KEY SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'compensation target not found' USING ERRCODE = 'P0002'; END IF;
  END IF;
  IF v_cls IS NOT NULL THEN                                    -- never on the training path (no account created there)
    PERFORM _credit_child_account(p_target_kind, p_target_id, c.teacher_id) FROM classrooms c WHERE c.id = v_cls;
  END IF;
  SELECT c.free, c.closed_at INTO v_free, v_closed FROM credit_accounts c
   WHERE c.kind = p_target_kind AND c.owner_id = p_target_id FOR UPDATE;
  IF NOT FOUND OR v_closed IS NOT NULL THEN
    RAISE EXCEPTION 'no usable credit account' USING ERRCODE = 'P0066';
  END IF;
  IF _credit_request_replay(p_request_id, 'compensate', p_hours, NULL, NULL, p_target_kind, p_target_id, p_admin_id) THEN
    RETURN v_free;
  END IF;
  PERFORM _credit_post('compensate', p_hours, NULL, NULL, NULL, p_target_kind, p_target_id, 'free', p_training_id,
                       v_member, p_admin_id, 'admin', p_request_id, _credit_reason(p_reason, p_target_kind, p_target_id),
                       NULL);
  SELECT c.free INTO v_free FROM credit_accounts c WHERE c.kind = p_target_kind AND c.owner_id = p_target_id;
  RETURN v_free;
END $$;

-- §4.6 assign (ownership by the CURRENT classroom) / reclaim (by the FUNDING teacher)
CREATE OR REPLACE FUNCTION public.credit_assign(p_teacher_id UUID, p_target_kind TEXT, p_target_id UUID, p_hours INT,
  p_request_id UUID)
RETURNS TABLE(o_target_free INT, o_teacher_free INT)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_parent UUID; v_closed TIMESTAMPTZ; v_wg UUID; v_tfree INT;
BEGIN
  IF p_hours IS NULL OR p_hours < 1 OR p_hours > 1000 THEN
    RAISE EXCEPTION 'invalid hours' USING ERRCODE = '22023';
  END IF;
  IF p_target_kind = 'student' THEN
    SELECT s.workgroup_id INTO v_wg FROM users s JOIN classrooms c ON c.id = s.classroom_id
     WHERE s.id = p_target_id AND s.role = 'student' AND c.teacher_id = p_teacher_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'not this teacher''s student' USING ERRCODE = 'P0066'; END IF;
    IF v_wg IS NOT NULL THEN RAISE EXCEPTION 'student is in a workgroup' USING ERRCODE = 'P0023'; END IF;
  ELSIF p_target_kind = 'group' THEN
    PERFORM 1 FROM workgroups g JOIN classrooms c ON c.id = g.classroom_id
     WHERE g.id = p_target_id AND c.teacher_id = p_teacher_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'not this teacher''s workgroup' USING ERRCODE = 'P0066'; END IF;
  ELSE
    RAISE EXCEPTION 'invalid target kind' USING ERRCODE = '22023';
  END IF;
  SELECT c.free INTO v_tfree FROM credit_accounts c WHERE c.kind = 'teacher' AND c.owner_id = p_teacher_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'the teacher pool is empty' USING ERRCODE = 'P0065'; END IF;
  PERFORM _credit_child_account(p_target_kind, p_target_id, p_teacher_id);
  SELECT c.parent_teacher_id, c.closed_at INTO v_parent, v_closed FROM credit_accounts c
   WHERE c.kind = p_target_kind AND c.owner_id = p_target_id FOR UPDATE;
  IF v_parent IS DISTINCT FROM p_teacher_id OR v_closed IS NOT NULL THEN
    RAISE EXCEPTION 'the account belongs to another teacher' USING ERRCODE = 'P0066';
  END IF;
  IF _credit_request_replay(p_request_id, 'assign', p_hours, 'teacher', p_teacher_id, p_target_kind, p_target_id,
                            p_teacher_id) THEN
    RETURN QUERY SELECT (SELECT c.free FROM credit_accounts c WHERE c.kind = p_target_kind AND c.owner_id = p_target_id),
                        v_tfree;
    RETURN;
  END IF;
  IF v_tfree < p_hours THEN RAISE EXCEPTION 'the teacher pool is too small' USING ERRCODE = 'P0065'; END IF;
  PERFORM _credit_post('assign', p_hours, 'teacher', p_teacher_id, 'free', p_target_kind, p_target_id, 'free', NULL,
                       NULL, p_teacher_id, 'teacher', p_request_id, NULL, NULL);
  RETURN QUERY SELECT (SELECT c.free FROM credit_accounts c WHERE c.kind = p_target_kind AND c.owner_id = p_target_id),
                      (SELECT c.free FROM credit_accounts c WHERE c.kind = 'teacher' AND c.owner_id = p_teacher_id);
END $$;

CREATE OR REPLACE FUNCTION public.credit_reclaim(p_teacher_id UUID, p_target_kind TEXT, p_target_id UUID, p_hours INT,
  p_request_id UUID)
RETURNS TABLE(o_target_free INT, o_teacher_free INT)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_free INT; v_parent UUID; v_closed TIMESTAMPTZ; v_tfree INT;
BEGIN
  IF p_hours IS NULL OR p_hours < 1 OR p_hours > 1000 OR p_target_kind IS NULL
     OR p_target_kind NOT IN ('student','group') THEN
    RAISE EXCEPTION 'invalid reclaim' USING ERRCODE = '22023';
  END IF;
  SELECT c.free INTO v_tfree FROM credit_accounts c WHERE c.kind = 'teacher' AND c.owner_id = p_teacher_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'not this teacher''s account' USING ERRCODE = 'P0066'; END IF;
  SELECT c.free, c.parent_teacher_id, c.closed_at INTO v_free, v_parent, v_closed FROM credit_accounts c
   WHERE c.kind = p_target_kind AND c.owner_id = p_target_id FOR UPDATE;
  IF NOT FOUND OR v_parent IS DISTINCT FROM p_teacher_id OR v_closed IS NOT NULL THEN
    RAISE EXCEPTION 'not this teacher''s account' USING ERRCODE = 'P0066';
  END IF;
  IF _credit_request_replay(p_request_id, 'reclaim', p_hours, p_target_kind, p_target_id, 'teacher', p_teacher_id,
                            p_teacher_id) THEN
    RETURN QUERY SELECT v_free, v_tfree;
    RETURN;
  END IF;
  IF v_free < p_hours THEN
    RAISE EXCEPTION 'only % free', v_free USING ERRCODE = 'P0064';  -- reserved and spent hours are never reclaimable
  END IF;
  PERFORM _credit_post('reclaim', p_hours, p_target_kind, p_target_id, 'free', 'teacher', p_teacher_id, 'free', NULL,
                       NULL, p_teacher_id, 'teacher', p_request_id, NULL, NULL);
  RETURN QUERY SELECT (SELECT c.free FROM credit_accounts c WHERE c.kind = p_target_kind AND c.owner_id = p_target_id),
                      (SELECT c.free FROM credit_accounts c WHERE c.kind = 'teacher' AND c.owner_id = p_teacher_id);
END $$;

-- §4.7 one per-member limit per group [D5]; every real change is a limit_change record
CREATE OR REPLACE FUNCTION public.credit_set_member_limit(p_teacher_id UUID, p_group_id UUID, p_hours INT)
RETURNS INT LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_old INT; v_parent UUID; v_closed TIMESTAMPTZ;
BEGIN
  IF p_hours IS NOT NULL AND (p_hours < 1 OR p_hours > 1000) THEN
    RAISE EXCEPTION 'invalid member limit' USING ERRCODE = '22023';
  END IF;
  PERFORM 1 FROM workgroups g JOIN classrooms c ON c.id = g.classroom_id
   WHERE g.id = p_group_id AND c.teacher_id = p_teacher_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'not this teacher''s workgroup' USING ERRCODE = 'P0066'; END IF;
  PERFORM _credit_child_account('group', p_group_id, p_teacher_id);
  PERFORM 1 FROM credit_accounts c WHERE c.kind = 'teacher' AND c.owner_id = p_teacher_id FOR UPDATE;
  SELECT c.member_hour_limit, c.parent_teacher_id, c.closed_at INTO v_old, v_parent, v_closed FROM credit_accounts c
   WHERE c.kind = 'group' AND c.owner_id = p_group_id FOR UPDATE;
  IF v_parent IS DISTINCT FROM p_teacher_id OR v_closed IS NOT NULL THEN
    RAISE EXCEPTION 'the account belongs to another teacher' USING ERRCODE = 'P0066';
  END IF;
  IF v_old IS DISTINCT FROM p_hours THEN
    UPDATE credit_accounts c SET member_hour_limit = p_hours WHERE c.kind = 'group' AND c.owner_id = p_group_id;
    PERFORM _credit_post('limit_change', 0, NULL, NULL, NULL, 'group', p_group_id, NULL, NULL, NULL, p_teacher_id,
                         'teacher', NULL, NULL, jsonb_build_object('old', v_old, 'new', p_hours));
  END IF;
  RETURN p_hours;
END $$;

-- §4.8 read-only; DEFINER because it calls the internal _credit_startable (review B1)
CREATE OR REPLACE FUNCTION public.credit_quota(p_user_id UUID, p_max_hours INT DEFAULT 3)
RETURNS TABLE(o_role TEXT, o_account_kind TEXT, o_account_id UUID, o_free INT, o_reserved INT, o_spent INT,
              o_member_limit INT, o_member_used INT, o_next_hours INT)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public AS $$
DECLARE v_u users%ROWTYPE; s RECORD;
BEGIN
  SELECT * INTO v_u FROM users u WHERE u.id = p_user_id;
  IF NOT FOUND THEN RETURN; END IF;
  IF v_u.role <> 'student' THEN
    RETURN QUERY SELECT v_u.role::text, NULL::text, NULL::uuid, 0, 0, 0, NULL::int, NULL::int, 0;
    RETURN;
  END IF;
  SELECT * INTO s FROM _credit_startable(p_user_id, v_u.workgroup_id, false);
  RETURN QUERY SELECT 'student'::text, s.o_kind, s.o_account_id, s.o_free, s.o_reserved, s.o_spent, s.o_member_limit,
                      s.o_member_used,
                      GREATEST(LEAST(3, GREATEST(1, COALESCE(p_max_hours, 3)), s.o_free, COALESCE(s.o_member_left, 3)), 0);
END $$;

-- §4.9 the stop decision under the row lock: publish the newest ready checkpoint
-- after a GPU trained, else cancel. INVOKER: it writes only trainings/attempts.
CREATE OR REPLACE FUNCTION public.stop_training(p_training_id INT, p_user_id UUID, p_actor_id UUID, p_actor_role TEXT,
  p_note TEXT)
RETURNS TABLE(o_outcome TEXT, o_step INT, o_attempt INT, o_backend TEXT, o_cloud_ref TEXT, o_attempt_state TEXT,
              o_cancel_attempts INT, o_managed BOOLEAN, o_cloud_job_id TEXT)
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE v_t trainings%ROWTYPE; v_a training_attempts%ROWTYPE; v_trained BOOLEAN; v_step INT; v_final BOOLEAN; v_sec INT;
BEGIN
  IF p_actor_role IS NULL OR p_actor_role NOT IN ('student','teacher') THEN
    RAISE EXCEPTION 'invalid actor role' USING ERRCODE = '22023';
  END IF;
  SELECT * INTO v_t FROM trainings t WHERE t.id = p_training_id AND t.user_id = p_user_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'training % not found', p_training_id USING ERRCODE = 'P0002'; END IF;
  IF v_t.status NOT IN ('queued','running','cancel_requested') THEN
    RAISE EXCEPTION 'training % not active', p_training_id USING ERRCODE = 'P0050';
  END IF;
  IF v_t.managed AND v_t.current_attempt > 0 THEN
    SELECT * INTO v_a FROM training_attempts a WHERE a.training_id = p_training_id AND a.attempt_no = v_t.current_attempt;
    IF v_a.state = 'ended' AND v_a.end_reason IN ('succeeded','capped','wall_capped','stopped','attempts_out','no_gpu',
                                                  'content_refused') THEN
      RAISE EXCEPTION 'training % is publishing', p_training_id USING ERRCODE = 'P0051';
    END IF;
  END IF;
  -- the first stop request writes the time and the role together; nothing overwrites them
  UPDATE trainings t
     SET stop_requested_by_role = CASE WHEN t.stop_requested_at IS NULL THEN p_actor_role ELSE t.stop_requested_by_role END,
         stop_requested_at = COALESCE(t.stop_requested_at, now())
   WHERE t.id = p_training_id;
  v_trained := EXISTS (SELECT 1 FROM training_attempts a WHERE a.training_id = p_training_id AND a.first_heartbeat_at IS NOT NULL);
  IF v_t.status <> 'cancel_requested' AND v_t.managed AND v_t.credit_scheme = 'ledger' AND v_trained
     AND v_t.current_attempt > 0 AND v_a.state IN ('placing','starting','running','lost') THEN
    v_final := EXISTS (SELECT 1 FROM training_checkpoints c
                        WHERE c.training_id = p_training_id AND c.state = 'ready' AND c.step = v_t.total_steps);
    SELECT max(c.step) INTO v_step FROM training_checkpoints c
     WHERE c.training_id = p_training_id AND c.state = 'ready' AND c.step < v_t.total_steps;
    IF v_final OR v_step IS NOT NULL THEN
      v_sec := CASE WHEN v_a.state IN ('placing','starting','running') AND v_a.first_heartbeat_at IS NOT NULL
                    THEN GREATEST(floor(EXTRACT(EPOCH FROM (now() - v_a.first_heartbeat_at)))::int, 0) ELSE 0 END;
      UPDATE training_attempts a
         SET state = 'ended', ended_at = now(), end_reason = CASE WHEN v_final THEN 'succeeded' ELSE 'stopped' END
       WHERE a.training_id = p_training_id AND a.attempt_no = v_t.current_attempt;
      UPDATE trainings t
         SET status = 'running', worker_token = NULL, status_note = p_note,
             capped_step = CASE WHEN v_final THEN t.capped_step ELSE v_step END,
             train_seconds_used = t.train_seconds_used + v_sec
       WHERE t.id = p_training_id;
      RETURN QUERY SELECT CASE WHEN v_final THEN 'publish_final' ELSE 'publish' END,
                          CASE WHEN v_final THEN v_t.total_steps ELSE v_step END,
                          v_t.current_attempt, v_a.backend, v_a.cloud_ref, v_a.state, v_t.cancel_attempts, v_t.managed,
                          v_t.cloud_job_id;
      RETURN;
    END IF;
  END IF;
  UPDATE trainings t
     SET status = 'cancel_requested', cancel_attempted_at = now(), cancel_attempts = t.cancel_attempts + 1
   WHERE t.id = p_training_id;
  RETURN QUERY SELECT 'cancel'::text, NULL::int, v_t.current_attempt, v_a.backend, v_a.cloud_ref, v_a.state,
                      v_t.cancel_attempts + 1, v_t.managed, v_t.cloud_job_id;
END $$;

-- §4.11 history, reasons and refusals
CREATE OR REPLACE FUNCTION public.credit_history(p_scope TEXT, p_id UUID, p_before BIGINT DEFAULT NULL,
  p_limit INT DEFAULT 100)
RETURNS TABLE(id BIGINT, at TIMESTAMPTZ, kind TEXT, hours INT, from_kind TEXT, from_id UUID, to_kind TEXT, to_id UUID,
              training_id INT, member_id UUID, actor_id UUID, actor_role TEXT, reason_id BIGINT, reason TEXT, meta JSONB)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public AS $$
DECLARE v_lim INT := LEAST(GREATEST(COALESCE(p_limit, 100), 1), 200);
BEGIN
  IF p_scope IS NULL OR p_scope NOT IN ('student','group','teacher','teacher_all','all') THEN
    RAISE EXCEPTION 'invalid history scope' USING ERRCODE = '22023';
  END IF;
  RETURN QUERY
  SELECT l.id, l.at, l.kind, l.hours, l.from_kind, l.from_id, l.to_kind, l.to_id, l.training_id, l.member_id,
         l.actor_id, l.actor_role, l.reason_id, r.text, l.meta
    FROM credit_ledger l
    LEFT JOIN credit_reasons r ON r.id = l.reason_id
   WHERE (p_before IS NULL OR l.id < p_before)
     AND CASE p_scope
       WHEN 'student' THEN (l.from_kind = 'student' AND l.from_id = p_id) OR (l.to_kind = 'student' AND l.to_id = p_id)
                           OR l.member_id = p_id
       WHEN 'group'   THEN (l.from_kind = 'group' AND l.from_id = p_id) OR (l.to_kind = 'group' AND l.to_id = p_id)
       WHEN 'teacher' THEN (l.from_kind = 'teacher' AND l.from_id = p_id) OR (l.to_kind = 'teacher' AND l.to_id = p_id)
       WHEN 'teacher_all' THEN (l.from_kind = 'teacher' AND l.from_id = p_id) OR (l.to_kind = 'teacher' AND l.to_id = p_id)
                           OR EXISTS (SELECT 1 FROM credit_accounts a
                                       WHERE a.parent_teacher_id = p_id
                                         AND ((a.kind = l.from_kind AND a.owner_id = l.from_id)
                                           OR (a.kind = l.to_kind AND a.owner_id = l.to_id)))
       ELSE true END
   ORDER BY l.id DESC
   LIMIT v_lim;
END $$;

CREATE OR REPLACE FUNCTION public.credit_erase_reason(p_admin_id UUID, p_reason_id BIGINT)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM users u WHERE u.id = p_admin_id AND u.role = 'admin') THEN
    RAISE EXCEPTION 'not an admin' USING ERRCODE = 'P0067';
  END IF;
  DELETE FROM credit_reasons r WHERE r.id = p_reason_id;
  RETURN FOUND;
END $$;

CREATE OR REPLACE FUNCTION public.record_training_refusal(p_user_id UUID, p_request_kind TEXT, p_code TEXT,
  p_model_type TEXT)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_role TEXT; v_mt TEXT := lower(btrim(COALESCE(p_model_type, '')));
BEGIN
  SELECT u.role::text INTO v_role FROM users u WHERE u.id = p_user_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'user % not found', p_user_id USING ERRCODE = 'P0002'; END IF;
  INSERT INTO training_start_refusals(user_id, role, request_kind, code, model_type)
  VALUES (p_user_id, v_role, p_request_kind, p_code, CASE WHEN v_mt ~ '^[a-z0-9_]{1,32}$' THEN v_mt END);
  DELETE FROM training_start_refusals r
   WHERE r.user_id = p_user_id
     AND r.id NOT IN (SELECT k.id FROM training_start_refusals k WHERE k.user_id = p_user_id ORDER BY k.id DESC LIMIT 200);
END $$;

-- §4.12 report-only (writes nothing)
CREATE OR REPLACE FUNCTION public.credit_reconcile()
RETURNS TABLE(o_unsettled INT, o_orphans INT, o_drift INT, o_stale_open INT)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public AS $$
BEGIN
  RETURN QUERY SELECT
    (SELECT count(*) FROM trainings t JOIN credit_ledger r ON r.kind = 'reserve' AND r.training_id = t.id
      WHERE t.credit_scheme = 'ledger' AND t.status IN ('succeeded','failed','canceled')
        AND NOT EXISTS (SELECT 1 FROM credit_ledger c WHERE c.kind = 'charge' AND c.training_id = t.id))::int,
    (SELECT count(*) FROM credit_ledger r WHERE r.kind = 'reserve'
        AND NOT EXISTS (SELECT 1 FROM trainings t WHERE t.id = r.training_id)
        AND NOT EXISTS (SELECT 1 FROM credit_ledger c WHERE c.kind = 'charge' AND c.training_id = r.training_id))::int,
    (SELECT count(*) FROM credit_accounts a WHERE
       a.free <> COALESCE((SELECT sum(x.hours) FROM credit_ledger x WHERE x.to_kind = a.kind AND x.to_id = a.owner_id AND x.to_bucket = 'free'), 0)
               - COALESCE((SELECT sum(x.hours) FROM credit_ledger x WHERE x.from_kind = a.kind AND x.from_id = a.owner_id AND x.from_bucket = 'free'), 0)
    OR a.reserved <> COALESCE((SELECT sum(x.hours) FROM credit_ledger x WHERE x.to_kind = a.kind AND x.to_id = a.owner_id AND x.to_bucket = 'reserved'), 0)
               - COALESCE((SELECT sum(x.hours) FROM credit_ledger x WHERE x.from_kind = a.kind AND x.from_id = a.owner_id AND x.from_bucket = 'reserved'), 0)
    OR a.spent <> COALESCE((SELECT sum(x.hours) FROM credit_ledger x WHERE x.kind = 'charge' AND x.from_kind = a.kind AND x.from_id = a.owner_id), 0))::int,
    -- WALL_CAP_S + WATCHDOG_GRACE_S + PUBLISH_RETRY_S + 1 h = 19 h 20 min
    (SELECT count(*) FROM credit_ledger r WHERE r.kind = 'reserve' AND r.at < now() - interval '19 hours 20 minutes'
        AND NOT EXISTS (SELECT 1 FROM credit_ledger c WHERE c.kind = 'charge' AND c.training_id = r.training_id))::int;
END $$;

-- §4.13 summaries
CREATE OR REPLACE FUNCTION public.credit_teacher_summary(p_teacher_id UUID)
RETURNS TABLE(o_free INT, o_assigned_free INT, o_assigned_reserved INT, o_spent INT, o_compensated INT,
              o_student_count INT, o_group_count INT, o_group_hours INT)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM users u WHERE u.id = p_teacher_id AND u.role = 'teacher') THEN RETURN; END IF;
  RETURN QUERY SELECT
    COALESCE((SELECT c.free FROM credit_accounts c WHERE c.kind = 'teacher' AND c.owner_id = p_teacher_id), 0),
    COALESCE((SELECT sum(c.free) FROM credit_accounts c WHERE c.parent_teacher_id = p_teacher_id), 0)::int,
    COALESCE((SELECT sum(c.reserved) FROM credit_accounts c WHERE c.parent_teacher_id = p_teacher_id), 0)::int,
    COALESCE((SELECT sum(c.spent) FROM credit_accounts c WHERE c.parent_teacher_id = p_teacher_id), 0)::int,
    COALESCE((SELECT sum(l.hours) FROM credit_ledger l
               JOIN credit_accounts a ON a.kind = l.to_kind AND a.owner_id = l.to_id
              WHERE l.kind = 'compensate' AND a.parent_teacher_id = p_teacher_id), 0)::int,
    (SELECT count(*) FROM users s JOIN classrooms c ON c.id = s.classroom_id
      WHERE c.teacher_id = p_teacher_id AND s.role = 'student')::int,
    (SELECT count(*) FROM workgroups g JOIN classrooms c ON c.id = g.classroom_id WHERE c.teacher_id = p_teacher_id)::int,
    COALESCE((SELECT sum(c.free + c.reserved) FROM credit_accounts c
               WHERE c.parent_teacher_id = p_teacher_id AND c.kind = 'group' AND c.closed_at IS NULL), 0)::int;
END $$;

-- compatibility for an API older than 052: the exact old return types (m4)
CREATE OR REPLACE FUNCTION public.get_remaining_credits(p_user_id UUID)
RETURNS TABLE(training_credits INTEGER, trainings_used BIGINT, remaining BIGINT)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public AS $$
DECLARE q RECORD; v_start INT;
BEGIN
  SELECT * INTO q FROM credit_quota(p_user_id, 3);
  IF NOT FOUND THEN RETURN; END IF;
  v_start := GREATEST(LEAST(q.o_free, COALESCE(q.o_member_limit - q.o_member_used, q.o_free)), 0);
  RETURN QUERY SELECT (v_start + q.o_reserved + q.o_spent)::INTEGER, (q.o_reserved + q.o_spent)::BIGINT, v_start::BIGINT;
END $$;

CREATE OR REPLACE FUNCTION public.get_teacher_credit_summary(p_teacher_id UUID)
RETURNS TABLE (
  pool_total          INTEGER,
  allocated_total     BIGINT,
  pool_available      BIGINT,
  student_count       BIGINT,
  group_count         BIGINT,
  group_credits_total BIGINT
)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public AS $$
DECLARE s RECORD;
BEGIN
  SELECT * INTO s FROM credit_teacher_summary(p_teacher_id);
  IF NOT FOUND THEN RETURN; END IF;
  RETURN QUERY SELECT (s.o_free + s.o_assigned_free + s.o_assigned_reserved)::INTEGER,
                      (s.o_assigned_free + s.o_assigned_reserved)::BIGINT,
                      s.o_free::BIGINT, s.o_student_count::BIGINT, s.o_group_count::BIGINT, s.o_group_hours::BIGINT;
END $$;

CREATE OR REPLACE FUNCTION public.adjust_student_credits(
  p_teacher_id UUID,
  p_student_id UUID,
  p_delta      INTEGER
) RETURNS TABLE (new_amount INT, pool_available BIGINT)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  RAISE EXCEPTION 'superseded by the credit ledger (052)' USING ERRCODE = 'P0069';
END $$;

CREATE OR REPLACE FUNCTION public.adjust_workgroup_credits(
  p_teacher_id   UUID,
  p_workgroup_id UUID,
  p_delta        INTEGER
) RETURNS TABLE (new_amount INTEGER, pool_available BIGINT)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  RAISE EXCEPTION 'superseded by the credit ledger (052)' USING ERRCODE = 'P0069';
END $$;

-- §4.10 changed 049/050 functions (INVOKER, as before) ----------------------------
-- cap_training_attempt (050 body): + attempts_out, no_gpu [Q-A], content_refused [R3-1]
CREATE OR REPLACE FUNCTION public.cap_training_attempt(
  p_training_id INT, p_attempt INT, p_step INT, p_reason TEXT, p_note TEXT, p_train_seconds INT)
RETURNS VOID LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE
  v_rows  INT;
  v_prev  TEXT;
  v_total INT;
BEGIN
  IF p_reason IS NULL OR p_reason NOT IN ('capped','wall_capped','attempts_out','no_gpu','content_refused')
     OR p_step IS NULL OR p_step < 1 THEN
    RAISE EXCEPTION 'invalid cap' USING ERRCODE = '22023';
  END IF;
  SELECT t.total_steps INTO v_total FROM public.trainings t WHERE t.id = p_training_id FOR UPDATE;
  IF v_total IS NOT NULL AND p_step >= v_total THEN
    RAISE EXCEPTION 'cap step % is not below the total %', p_step, v_total USING ERRCODE = '22023';
  END IF;
  SELECT a.state INTO v_prev FROM public.training_attempts a
   WHERE a.training_id = p_training_id AND a.attempt_no = p_attempt;
  UPDATE public.training_attempts a
     SET state = 'ended', ended_at = now(), end_reason = p_reason
    FROM public.trainings t
   WHERE a.training_id = p_training_id AND a.attempt_no = p_attempt
     AND a.state IN ('placing','starting','running','lost')
     AND t.id = a.training_id AND t.managed AND t.current_attempt = p_attempt
     AND t.status IN ('queued','running');
  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows = 0 THEN
    RAISE EXCEPTION 'cap conflict for training %', p_training_id USING ERRCODE = 'P0049';
  END IF;
  UPDATE public.trainings
     SET status = 'running', worker_token = NULL, status_note = p_note, capped_step = p_step,
         train_seconds_used = train_seconds_used
           + CASE WHEN v_prev IN ('lost','ended') THEN 0 ELSE GREATEST(COALESCE(p_train_seconds, 0), 0) END
   WHERE id = p_training_id;
END;
$$;

-- requeue_training (050 body, same signature): E50 unchanged; for a ledger row the
-- unsaved rest up to the attempt's last real training evidence goes to
-- lost_seconds and the reason to lost_reasons [D1, N2, Q-B]
CREATE OR REPLACE FUNCTION public.requeue_training(
  p_training_id INT, p_attempt INT, p_reason TEXT, p_note TEXT, p_resume_step INT, p_train_seconds INT)
RETURNS VOID LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE
  v_rows    INT;
  v_hist    JSONB;
  v_saved   INT;
  v_t       public.trainings%ROWTYPE;
  v_hb      TIMESTAMPTZ;
  v_charged INT;
  v_end     TIMESTAMPTZ;
  v_counted INT;
BEGIN
  SELECT * INTO v_t FROM public.trainings t WHERE t.id = p_training_id FOR UPDATE;
  UPDATE public.training_attempts a
     SET state = 'lost', ended_at = now(), end_reason = p_reason
    FROM public.trainings t
   WHERE a.training_id = p_training_id AND a.attempt_no = p_attempt
     AND a.state IN ('placing','starting','running')
     AND t.id = a.training_id AND t.managed AND t.current_attempt = p_attempt
     AND t.status IN ('queued','running');
  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows = 0 THEN
    RAISE EXCEPTION 'requeue conflict for training %', p_training_id USING ERRCODE = 'P0049';
  END IF;
  -- E45/E50: from the attempt's first running write to the created_at of its newest
  -- own checkpoint whose state is ready; 0 without one (or without a running write).
  SELECT a.first_heartbeat_at,
         GREATEST(COALESCE(floor(EXTRACT(EPOCH FROM (
           (SELECT max(c.created_at) FROM public.training_checkpoints c
             WHERE c.training_id = a.training_id AND c.attempt_no = a.attempt_no AND c.state = 'ready')
           - a.first_heartbeat_at)))::int, 0), 0)
    INTO v_hb, v_saved
    FROM public.training_attempts a
   WHERE a.training_id = p_training_id AND a.attempt_no = p_attempt;
  v_charged := LEAST(GREATEST(COALESCE(p_train_seconds, 0), 0), COALESCE(v_saved, 0));
  -- N2: the lost part ends at the attempt's last real training evidence, capped at now()
  v_end := CASE
             WHEN p_reason IN ('stalled','checkpoint_overdue') THEN LEAST(now(), COALESCE(v_t.step_advanced_at, now()))
             WHEN p_reason IN ('heartbeat_lost','pod_gone','modal_ended') THEN LEAST(now(), COALESCE(v_t.last_progress_at, now()))
             ELSE now()
           END;
  v_counted := CASE WHEN v_hb IS NULL THEN 0
                    ELSE GREATEST(floor(EXTRACT(EPOCH FROM (v_end - v_hb)))::int, 0) END;
  SELECT COALESCE(jsonb_agg(e ORDER BY ord), '[]'::jsonb) INTO v_hist
    FROM public.trainings t,
         jsonb_array_elements(COALESCE(t.loss_history, '[]'::jsonb)) WITH ORDINALITY AS x(e, ord)
   WHERE t.id = p_training_id AND (e->>'s')::int <= COALESCE(p_resume_step, 0);
  UPDATE public.trainings
     SET status = 'queued', worker_token = NULL, status_note = p_note,
         current_step = COALESCE(p_resume_step, 0),
         step_advanced_at = NULL,
         loss_history = v_hist,
         -- The loss of the last point kept, not of a step the requeue rolled back (V2-6).
         current_loss = CASE WHEN jsonb_typeof(v_hist -> -1 -> 'l') = 'number'
                             THEN (v_hist -> -1 ->> 'l')::real END,
         train_seconds_used = train_seconds_used + v_charged,
         lost_seconds = lost_seconds + CASE WHEN credit_scheme = 'ledger' THEN GREATEST(v_counted - v_charged, 0) ELSE 0 END,
         lost_reasons = CASE WHEN credit_scheme = 'ledger' THEN lost_reasons || p_reason ELSE lost_reasons END
   WHERE id = p_training_id;
END;
$$;

-- finish_training (050 body, DROPPED and re-created): + p_charge_waiver [OQ-2].
-- The waiver needs PROOF of a fresh first attempt that never trained (fix round,
-- 2026-11-01): P0073 before any write unless this attempt's resume_from_step is 0
-- (written only by the API's POST /start: NULL means a host that skipped it), the
-- attempt is 'running', the row is no continuation, it counted no trained second
-- yet, it reports no step (current_step 0; fix round 2) and it has no ready
-- checkpoint. A row that would not be finished anyway
-- (not managed, another attempt, terminal) keeps its P0049.
DROP FUNCTION public.finish_training(INT, INT, TEXT, TEXT, TEXT, TEXT, INT, TIMESTAMPTZ);
CREATE OR REPLACE FUNCTION public.finish_training(
  p_training_id INT, p_attempt INT, p_status TEXT, p_message TEXT, p_log_url TEXT,
  p_note TEXT DEFAULT NULL, p_final_step INT DEFAULT NULL, p_continuable_until TIMESTAMPTZ DEFAULT NULL,
  p_charge_waiver TEXT DEFAULT NULL)
RETURNS VOID LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE
  v_rows INT;
  v_seconds INT;
  v_t public.trainings%ROWTYPE;
  v_a public.training_attempts%ROWTYPE;
BEGIN
  IF p_status NOT IN ('succeeded','failed') THEN
    RAISE EXCEPTION 'invalid status %', p_status USING ERRCODE = '22023';
  END IF;
  IF p_charge_waiver IS NOT NULL AND (p_charge_waiver <> 'lerobot_error' OR p_status <> 'failed') THEN
    RAISE EXCEPTION 'invalid charge waiver' USING ERRCODE = '22023';
  END IF;
  SELECT * INTO v_t FROM public.trainings t WHERE t.id = p_training_id FOR UPDATE;
  IF p_charge_waiver = 'lerobot_error' AND v_t.managed AND v_t.current_attempt = p_attempt
     AND v_t.status IN ('queued','running') THEN
    SELECT * INTO v_a FROM public.training_attempts a
     WHERE a.training_id = p_training_id AND a.attempt_no = p_attempt;
    IF NOT (v_a.resume_from_step IS NOT DISTINCT FROM 0
            AND v_a.state IS NOT DISTINCT FROM 'running'
            AND v_t.continued_from IS NULL
            AND v_t.train_seconds_used IS NOT DISTINCT FROM 0
            AND v_t.current_step IS NOT DISTINCT FROM 0
            AND NOT EXISTS (SELECT 1 FROM public.training_checkpoints c
                             WHERE c.training_id = p_training_id AND c.state = 'ready')) THEN
      RAISE EXCEPTION 'charge waiver refused' USING ERRCODE = 'P0073';
    END IF;
  END IF;
  -- The trained seconds of an attempt this call ends (V2-7): the same
  -- now − first_heartbeat_at the API passes to requeue_training and
  -- end_training_attempt. An attempt they already ended (lost/ended) was counted.
  SELECT GREATEST(floor(EXTRACT(EPOCH FROM (now() - a.first_heartbeat_at)))::int, 0) INTO v_seconds
    FROM public.training_attempts a
   WHERE a.training_id = p_training_id AND a.attempt_no = p_attempt
     AND a.state NOT IN ('lost','ended') AND a.first_heartbeat_at IS NOT NULL;
  UPDATE public.trainings
     SET status = p_status, terminated_at = now(), worker_token = NULL, status_note = p_note,
         error_message = CASE WHEN p_status = 'failed' THEN p_message ELSE error_message END,
         log_url = COALESCE(p_log_url, log_url),
         current_step = CASE WHEN p_status = 'succeeded' THEN COALESCE(p_final_step, total_steps) ELSE current_step END,
         continuable_until = CASE WHEN p_status = 'succeeded' THEN p_continuable_until ELSE continuable_until END,
         train_seconds_used = train_seconds_used + COALESCE(v_seconds, 0),
         charge_waiver = p_charge_waiver                       -- read by the settlement trigger in this UPDATE
   WHERE id = p_training_id AND managed AND current_attempt = p_attempt
     AND status IN ('queued','running');
  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows = 0 THEN
    RAISE EXCEPTION 'finish conflict for training %', p_training_id USING ERRCODE = 'P0049';
  END IF;
  UPDATE public.training_attempts
     SET state = 'ended', ended_at = COALESCE(ended_at, now()), end_reason = COALESCE(end_reason, p_status)
   WHERE training_id = p_training_id AND attempt_no = p_attempt AND state NOT IN ('lost','ended');
END;
$$;

-- update_training_progress (049 body, same signature, same EXECUTE for anon and
-- authenticated; fix round 2026-11-01): EXACTLY one line differs from 049's. The
-- stall clock step_advanced_at restarts only when it IS NULL (requeue_training
-- clears it for every new attempt), on the attempt's first running write (the
-- row still 'queued') or when the step rises. In 049 EVERY 'running' write
-- restarted it, so a host holding the worker token kept a training that made no
-- step progress alive until its cap and the student paid the whole reservation.
-- The honest worker sends 'running' once, after /start (training_handler.py
-- first_running_write), then step-only writes (_progress_loop). Legacy rows lose
-- nothing: their stall rule reads last_progress_at, which every write still moves.
CREATE OR REPLACE FUNCTION public.update_training_progress(
  p_training_id  INT,
  p_token        UUID,
  p_status       TEXT  DEFAULT NULL,
  p_current_step INT   DEFAULT NULL,
  p_total_steps  INT   DEFAULT NULL,
  p_current_loss REAL  DEFAULT NULL,
  p_error_message TEXT DEFAULT NULL,
  p_log_url      TEXT  DEFAULT NULL
) RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_rows        INT;
  v_new_history JSONB;
  v_len         INT;
  v_managed     BOOLEAN;
  v_prev_status TEXT;
  v_attempt     INT;
  v_total       INT;
  v_step        INT  := p_current_step;
  v_loss        REAL := p_current_loss;
BEGIN
  IF p_status IS NOT NULL AND p_status NOT IN ('queued','running','succeeded','failed','canceled') THEN
    RAISE EXCEPTION 'Invalid status: %', p_status USING ERRCODE = '22023';
  END IF;

  SELECT managed, status, current_attempt, total_steps INTO v_managed, v_prev_status, v_attempt, v_total
    FROM public.trainings WHERE id = p_training_id;
  IF v_managed AND p_status IS NOT NULL AND p_status <> 'running' THEN
    RAISE EXCEPTION 'Managed training: status % is set by the API', p_status USING ERRCODE = '22023';
  END IF;
  -- A managed row's chart is the API's range (V1-3): the step in [0, total_steps],
  -- a loss only when finite and within ±1e30 (NaN compares above every number).
  IF v_managed THEN
    IF v_step IS NOT NULL THEN
      v_step := LEAST(GREATEST(v_step, 0), v_total);
    END IF;
    IF v_loss IS NOT NULL AND NOT (v_loss <> 'NaN'::real AND v_loss BETWEEN -1e30 AND 1e30) THEN
      v_loss := NULL;
    END IF;
  END IF;

  IF v_step IS NOT NULL AND v_loss IS NOT NULL THEN
    SELECT COALESCE(loss_history, '[]'::jsonb) || jsonb_build_array(
             jsonb_build_object(
               's', v_step,
               'l', v_loss,
               't', (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT
             )
           )
      INTO v_new_history
      FROM public.trainings
     WHERE id = p_training_id;

    v_len := jsonb_array_length(v_new_history);
    IF v_len > 300 THEN
      SELECT jsonb_agg(elem ORDER BY idx)
        INTO v_new_history
        FROM (
          SELECT 0 AS idx, v_new_history -> 0 AS elem
          UNION
          SELECT (1 + s * (v_len - 102.0) / 198.0)::INT,
                 v_new_history -> (1 + s * (v_len - 102.0) / 198.0)::INT
            FROM generate_series(0, 198) AS s
          UNION
          SELECT v_len - 100 + s,
                 v_new_history -> (v_len - 100 + s)
            FROM generate_series(0, 99) AS s
        ) sampled;
    END IF;
  END IF;

  UPDATE public.trainings
  SET
    status        = COALESCE(p_status,        status),
    current_step  = COALESCE(v_step,          current_step),
    -- A managed row's step count and error text are the API's alone (E11, FR-1):
    -- a host holding its token can call this RPC directly.
    total_steps   = CASE WHEN managed THEN total_steps   ELSE COALESCE(p_total_steps,   total_steps)   END,
    -- An in-place restart (the trim below) leaves the loss of the trimmed curve's
    -- last point (NULL when none is left), never one of a step it dropped, as
    -- requeue_training does; a write that carries its own loss keeps it.
    current_loss  = CASE
      WHEN p_status = 'running' AND v_step IS NOT NULL AND v_step < COALESCE(current_step, 0)
        THEN COALESCE(v_loss,
               (SELECT CASE WHEN jsonb_typeof(e->'l') = 'number' THEN (e->>'l')::real END
                  FROM jsonb_array_elements(COALESCE(v_new_history, loss_history)) WITH ORDINALITY AS x(e, ord)
                 WHERE (e->>'s')::int <= v_step
                 ORDER BY ord DESC LIMIT 1))
      ELSE COALESCE(v_loss, current_loss)
    END,
    error_message = CASE WHEN managed THEN error_message ELSE COALESCE(p_error_message, error_message) END,
    loss_history  = CASE
      WHEN p_status = 'running' AND v_step IS NOT NULL AND v_step < COALESCE(current_step, 0)
        THEN (SELECT COALESCE(jsonb_agg(e ORDER BY ord), '[]'::jsonb)
                FROM jsonb_array_elements(COALESCE(v_new_history, loss_history)) WITH ORDINALITY AS x(e, ord)
               WHERE (e->>'s')::int <= v_step)
      ELSE COALESCE(v_new_history, loss_history)
    END,
    log_url       = CASE
      WHEN p_status IN ('succeeded','failed','canceled')
        THEN COALESCE(p_log_url, log_url)
      ELSE log_url
    END,
    last_progress_at = NOW(),
    step_advanced_at = CASE
      WHEN step_advanced_at IS NULL OR (p_status = 'running' AND status = 'queued') THEN NOW()
      WHEN v_step IS NOT NULL AND v_step > COALESCE(current_step, 0) THEN NOW()
      ELSE step_advanced_at
    END,
    status_note   = CASE
      WHEN managed AND p_status = 'running' AND status = 'queued' THEN NULL
      ELSE status_note
    END,
    terminated_at = CASE
      WHEN p_status IN ('succeeded','failed','canceled') THEN NOW()
      ELSE terminated_at
    END,
    worker_token  = CASE
      WHEN p_status IN ('succeeded','failed','canceled') THEN NULL
      ELSE worker_token
    END
  WHERE id = p_training_id
    AND worker_token = p_token
    AND status NOT IN ('succeeded','failed','canceled','cancel_requested');

  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows = 0 THEN
    RAISE EXCEPTION 'Invalid worker token, training not found, or training already terminal (or cancel-requested)'
      USING ERRCODE = 'P0001';
  END IF;

  IF v_managed AND p_status = 'running' THEN
    UPDATE public.training_attempts
       SET state = 'running', first_heartbeat_at = COALESCE(first_heartbeat_at, NOW())
     WHERE training_id = p_training_id AND attempt_no = v_attempt AND state IN ('placing','starting');
  END IF;
END;
$$;
REVOKE ALL ON FUNCTION public.update_training_progress(INT, UUID, TEXT, INT, INT, REAL, TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.update_training_progress(INT, UUID, TEXT, INT, INT, REAL, TEXT, TEXT) TO anon, authenticated;

-- cancel_request_training (050 body, DROPPED and re-created): + p_actor_role, the
-- first-writer rule of §4.9, and P0051 for every publish reason
DROP FUNCTION public.cancel_request_training(INT, UUID);
CREATE OR REPLACE FUNCTION public.cancel_request_training(p_training_id INT, p_user_id UUID,
  p_actor_role TEXT DEFAULT NULL)
RETURNS TABLE(o_managed BOOLEAN, o_cloud_job_id TEXT, o_current_attempt INT, o_attempt_state TEXT,
              o_backend TEXT, o_cloud_ref TEXT, o_cancel_attempts INT)
LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE
  v_t public.trainings%ROWTYPE;
  v_a public.training_attempts%ROWTYPE;
BEGIN
  IF p_actor_role IS NOT NULL AND p_actor_role NOT IN ('student','teacher') THEN
    RAISE EXCEPTION 'invalid actor role' USING ERRCODE = '22023';
  END IF;
  SELECT * INTO v_t FROM public.trainings t WHERE t.id = p_training_id AND t.user_id = p_user_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'training % not found', p_training_id USING ERRCODE = 'P0002';
  END IF;
  IF v_t.status NOT IN ('queued','running','cancel_requested') THEN
    RAISE EXCEPTION 'training % not active', p_training_id USING ERRCODE = 'P0050';
  END IF;
  IF v_t.managed AND v_t.current_attempt > 0 THEN
    SELECT * INTO v_a FROM public.training_attempts a
     WHERE a.training_id = p_training_id AND a.attempt_no = v_t.current_attempt;
    IF v_a.state = 'ended' AND v_a.end_reason IN ('succeeded','capped','wall_capped','stopped','attempts_out','no_gpu',
                                                  'content_refused') THEN
      RAISE EXCEPTION 'training % is publishing', p_training_id USING ERRCODE = 'P0051';
    END IF;
  END IF;
  UPDATE public.trainings
     SET status = 'cancel_requested', cancel_attempted_at = now(), cancel_attempts = cancel_attempts + 1,
         stop_requested_by_role = CASE WHEN stop_requested_at IS NULL THEN p_actor_role ELSE stop_requested_by_role END,
         stop_requested_at = COALESCE(stop_requested_at, now())
   WHERE id = p_training_id;
  RETURN QUERY SELECT v_t.managed, v_t.cloud_job_id, v_t.current_attempt, v_a.state, v_a.backend,
                      v_a.cloud_ref, v_t.cancel_attempts + 1;
END;
$$;

-- complete_cancel_training (049 body): seconds counted only to the first stop request
CREATE OR REPLACE FUNCTION public.complete_cancel_training(p_training_id INT, p_final TEXT, p_message TEXT)
RETURNS VOID LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE
  v_rows INT;
  v_seconds INT;
BEGIN
  IF p_final NOT IN ('canceled','failed') THEN
    RAISE EXCEPTION 'invalid final %', p_final USING ERRCODE = '22023';
  END IF;
  PERFORM 1 FROM public.trainings t WHERE t.id = p_training_id FOR UPDATE;
  -- The trained seconds of the attempt this call ends, to LEAST(now(), the first
  -- stop request): a retried GPU stop never adds billable seconds. An attempt a
  -- requeue or an end already took to lost/ended was counted by that call.
  SELECT GREATEST(floor(EXTRACT(EPOCH FROM (LEAST(now(), COALESCE(t.stop_requested_at, now())) - a.first_heartbeat_at)))::int, 0)
    INTO v_seconds
    FROM public.trainings t
    JOIN public.training_attempts a ON a.training_id = t.id AND a.attempt_no = t.current_attempt
   WHERE t.id = p_training_id
     AND a.state NOT IN ('lost','ended') AND a.first_heartbeat_at IS NOT NULL;
  UPDATE public.trainings
     SET status = p_final, terminated_at = now(), worker_token = NULL, status_note = NULL,
         error_message = CASE WHEN p_final = 'failed' THEN p_message ELSE error_message END,
         train_seconds_used = train_seconds_used + COALESCE(v_seconds, 0)
   WHERE id = p_training_id AND status = 'cancel_requested';
  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows = 0 THEN
    RAISE EXCEPTION 'cancel conflict for training %', p_training_id USING ERRCODE = 'P0049';
  END IF;
  UPDATE public.training_attempts a
     SET state = 'ended', ended_at = now(), end_reason = 'canceled'
    FROM public.trainings t
   WHERE t.id = p_training_id AND a.training_id = t.id AND a.attempt_no = t.current_attempt
     AND a.state NOT IN ('lost','ended');
END;
$$;

-- claim_training_publish (050 body): every publish reason
CREATE OR REPLACE FUNCTION public.claim_training_publish(p_training_id INT, p_attempt INT, p_lease_s INT, p_owner TEXT)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE v_rows INT;
BEGIN
  UPDATE public.training_attempts a
     SET publish_claimed_at = now(), publish_owner = p_owner,
         publish_first_at = COALESCE(a.publish_first_at, now())
    FROM public.trainings t
   WHERE a.training_id = p_training_id AND a.attempt_no = p_attempt
     AND a.state = 'ended'
     AND a.end_reason IN ('succeeded','capped','wall_capped','stopped','attempts_out','no_gpu','content_refused')
     AND t.id = a.training_id AND t.current_attempt = p_attempt AND t.status = 'running'
     AND (a.publish_claimed_at IS NULL OR a.publish_owner = p_owner
          OR a.publish_claimed_at < now() - make_interval(secs => p_lease_s));
  GET DIAGNOSTICS v_rows = ROW_COUNT;
  RETURN v_rows = 1;
END;
$$;

-- 5b. the checkpoint writers (fix round 2, 2026-11-01) ------------------------------
-- mark_checkpoint_ready (V3-M1) is the ONE writer of a checkpoint's state 'ready'.
-- ckpt-done verifies the R2 objects outside any lock; this call then decides
-- under the trainings row lock, which finish_training, requeue_training,
-- cap_training_attempt, end_training_attempt, stop_training,
-- cancel_request_training, complete_cancel_training and the claim/attach/release
-- functions all take FIRST (FOR UPDATE), so the order is always trainings row,
-- then attempt or checkpoint rows. Before it, a host could hold ckpt-done back
-- until its attempt had ended (E50 then charged that lost attempt 0, nothing
-- being ready), let the checkpoints turn ready afterwards and have the next
-- attempt resume from them; the same race beat the waiver's „no ready
-- checkpoint" rule.
-- TRUE: this attempt's checkpoint is ready after the call, set now ('uploading'
-- -> 'ready', ready_at = now()) or already ready (a repeated ckpt-done; ready_at
-- kept). FALSE, nothing changed: no row, not managed, status not 'running', another
-- attempt is current, this attempt is not 'running', or the checkpoint is absent,
-- refused or deleted. Only 'running' counts as active: an attempt is 'running'
-- only while its row is (the first running write sets both in one transaction),
-- 'queued' is between attempts and 'cancel_requested' is a row being stopped.
CREATE OR REPLACE FUNCTION public.mark_checkpoint_ready(p_training_id INT, p_attempt INT, p_step INT)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE
  v_t     public.trainings%ROWTYPE;
  v_state TEXT;
  v_rows  INT;
BEGIN
  SELECT * INTO v_t FROM public.trainings t WHERE t.id = p_training_id FOR UPDATE;
  IF NOT FOUND OR NOT v_t.managed OR v_t.status <> 'running' OR v_t.current_attempt IS DISTINCT FROM p_attempt THEN
    RETURN false;
  END IF;
  SELECT a.state INTO v_state FROM public.training_attempts a
   WHERE a.training_id = p_training_id AND a.attempt_no = p_attempt;
  IF v_state IS DISTINCT FROM 'running' THEN
    RETURN false;
  END IF;
  UPDATE public.training_checkpoints c
     SET state = 'ready', ready_at = now()
   WHERE c.training_id = p_training_id AND c.attempt_no = p_attempt AND c.step = p_step
     AND c.state = 'uploading';
  GET DIAGNOSTICS v_rows = ROW_COUNT;
  IF v_rows = 1 THEN
    RETURN true;
  END IF;
  RETURN EXISTS (SELECT 1 FROM public.training_checkpoints c
                  WHERE c.training_id = p_training_id AND c.attempt_no = p_attempt AND c.step = p_step
                    AND c.state = 'ready');
END;
$$;

-- claim_checkpoint_mirror (049 body, same signature and grants): ONE added
-- condition, the training is queued, running or succeeded. That keeps every
-- honest mirror: one scheduled while the row runs (ckpt-done, the sweep's
-- schedule_due_mirrors reads queued/running rows), one between attempts
-- (queued), one whose lease renews while the row publishes (running) or after it
-- ended succeeded (capped, continuable). A failed or canceled row, and a
-- cancel_requested one (it only ever ends canceled or failed, and stop_training
-- sets it only when nothing is ready), is refused, so its lease renewal fails and
-- the mirror stops.
CREATE OR REPLACE FUNCTION public.claim_checkpoint_mirror(
  p_training_id INT, p_attempt INT, p_step INT, p_lease_s INT, p_owner TEXT)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY INVOKER SET search_path = public AS $$
DECLARE v_rows INT;
BEGIN
  UPDATE public.training_checkpoints c
     SET mirror_claimed_at = now(), mirror_owner = p_owner
   WHERE c.training_id = p_training_id AND c.attempt_no = p_attempt AND c.step = p_step
     AND c.state = 'ready' AND c.mirrored_at IS NULL
     AND EXISTS (SELECT 1 FROM public.trainings t WHERE t.id = c.training_id
                  AND t.status IN ('queued','running','succeeded'))
     AND (c.mirror_claimed_at IS NULL OR c.mirror_owner = p_owner
          OR c.mirror_claimed_at < now() - make_interval(secs => p_lease_s));
  GET DIAGNOSTICS v_rows = ROW_COUNT;
  RETURN v_rows = 1;
END;
$$;

-- 6. trigger functions and triggers (spec §5) -----------------------------------
-- I8: the credit columns change only inside the definer functions. Runs FIRST
-- (name order) and must stay SECURITY INVOKER (044's lesson: a definer sees its owner).
CREATE OR REPLACE FUNCTION public.trainings_guard_credit_columns() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
  IF current_user IN ('service_role','authenticated','anon') AND (
       NEW.credit_scheme  IS DISTINCT FROM OLD.credit_scheme  OR NEW.reserved_hours IS DISTINCT FROM OLD.reserved_hours
    OR NEW.charged_hours  IS DISTINCT FROM OLD.charged_hours  OR NEW.settled_at     IS DISTINCT FROM OLD.settled_at
    OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key OR NEW.user_id IS DISTINCT FROM OLD.user_id) THEN
    RAISE EXCEPTION 'credit columns are not writable' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_0_trainings_credit_guard BEFORE UPDATE ON public.trainings
  FOR EACH ROW EXECUTE FUNCTION public.trainings_guard_credit_columns();

-- I7: settlement inside the terminal transaction. A terminal writer that forgot
-- the open attempt's seconds cannot under-charge; one that added them adds nothing here.
CREATE OR REPLACE FUNCTION public.trainings_credit_settle() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v INT; v_secs INT;
BEGIN
  IF NEW.credit_scheme = 'ledger' THEN
    v_secs := GREATEST(NEW.train_seconds_used,
                       OLD.train_seconds_used + _credit_open_seconds(NEW.id, NEW.current_attempt, NEW.stop_requested_at));
    v := _credit_settle(NEW.id, v_secs,
                        _credit_meta(NEW.status, NEW.lost_seconds, NEW.lost_reasons, NEW.stop_requested_by_role),
                        NEW.charge_waiver);
    IF v IS NOT NULL THEN
      NEW.train_seconds_used := v_secs;
      NEW.charged_hours := v;
      NEW.settled_at := now();
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_9_trainings_credit_settle BEFORE UPDATE OF status ON public.trainings FOR EACH ROW
  WHEN (OLD.status NOT IN ('succeeded','failed','canceled') AND NEW.status IN ('succeeded','failed','canceled'))
  EXECUTE FUNCTION public.trainings_credit_settle();

-- a non-terminal ledger row that is deleted is settled first (the attempts still exist here)
CREATE OR REPLACE FUNCTION public.trainings_credit_settle_delete() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  IF OLD.credit_scheme = 'ledger' AND OLD.status NOT IN ('succeeded','failed','canceled') THEN
    PERFORM _credit_settle(OLD.id,
                           OLD.train_seconds_used + _credit_open_seconds(OLD.id, OLD.current_attempt, OLD.stop_requested_at),
                           _credit_meta('deleted', OLD.lost_seconds, OLD.lost_reasons, OLD.stop_requested_by_role));
  END IF;
  RETURN OLD;
END $$;
CREATE TRIGGER trg_9_trainings_credit_settle_delete BEFORE DELETE ON public.trainings
  FOR EACH ROW EXECUTE FUNCTION public.trainings_credit_settle_delete();

-- I9: the closes run AFTER DELETE, i.e. after the RI cascade has taken (and settled) the trainings rows
CREATE OR REPLACE FUNCTION public.users_credit_close() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  PERFORM _credit_close('teacher', OLD.id);
  PERFORM _credit_close('student', OLD.id);
  RETURN NULL;
END $$;
CREATE TRIGGER trg_9_users_credit_close AFTER DELETE ON public.users
  FOR EACH ROW EXECUTE FUNCTION public.users_credit_close();

CREATE OR REPLACE FUNCTION public.workgroups_credit_close() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  PERFORM _credit_close('group', OLD.id);
  RETURN NULL;
END $$;
CREATE TRIGGER trg_9_workgroups_credit_close AFTER DELETE ON public.workgroups
  FOR EACH ROW EXECUTE FUNCTION public.workgroups_credit_close();

-- an operator's move to another teacher's classroom re-parents the account (R7)
CREATE OR REPLACE FUNCTION public.credit_reparent_on_move() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_kind TEXT := CASE WHEN TG_TABLE_NAME = 'workgroups' THEN 'group' ELSE 'student' END;
        v_new UUID; a credit_accounts%ROWTYPE;
BEGIN
  SELECT c.teacher_id INTO v_new FROM classrooms c WHERE c.id = NEW.classroom_id;
  SELECT * INTO a FROM credit_accounts x WHERE x.kind = v_kind AND x.owner_id = NEW.id;
  IF v_new IS NULL OR a.kind IS NULL OR a.closed_at IS NOT NULL OR a.parent_teacher_id = v_new THEN
    RETURN NULL;                                               -- no account, no classroom, or the same teacher
  END IF;
  PERFORM 1 FROM credit_accounts x WHERE x.kind = 'teacher' AND x.owner_id = a.parent_teacher_id FOR UPDATE;
  SELECT * INTO a FROM credit_accounts x WHERE x.kind = v_kind AND x.owner_id = NEW.id FOR UPDATE;
  IF a.reserved > 0 THEN
    RAISE EXCEPTION 'the account has an open reservation; move it after the training ended' USING ERRCODE = 'P0072';
  END IF;
  PERFORM _credit_return_free(v_kind, NEW.id, a.parent_teacher_id, a.free, NULL, NULL, 'moved');
  INSERT INTO credit_accounts(kind, owner_id) VALUES ('teacher', v_new) ON CONFLICT DO NOTHING;   -- I13
  UPDATE credit_accounts x SET parent_teacher_id = v_new WHERE x.kind = v_kind AND x.owner_id = NEW.id;
  RETURN NULL;
END $$;
CREATE TRIGGER trg_9_users_credit_reparent AFTER UPDATE OF classroom_id ON public.users FOR EACH ROW
  WHEN (NEW.classroom_id IS DISTINCT FROM OLD.classroom_id AND NEW.role = 'student')
  EXECUTE FUNCTION public.credit_reparent_on_move();
CREATE TRIGGER trg_9_workgroups_credit_reparent AFTER UPDATE OF classroom_id ON public.workgroups FOR EACH ROW
  WHEN (NEW.classroom_id IS DISTINCT FROM OLD.classroom_id)
  EXECUTE FUNCTION public.credit_reparent_on_move();

-- 7. the switch-over (decision 11, I13): zero accounts + one migrate row each -------
INSERT INTO public.credit_accounts(kind, owner_id)
  SELECT 'teacher', u.id FROM public.users u WHERE u.role = 'teacher';
INSERT INTO public.credit_accounts(kind, owner_id)
  SELECT DISTINCT 'teacher', c.teacher_id FROM public.classrooms c ON CONFLICT DO NOTHING;   -- every classroom owner
INSERT INTO public.credit_accounts(kind, owner_id, parent_teacher_id)
  SELECT 'student', s.id, c.teacher_id FROM public.users s JOIN public.classrooms c ON c.id = s.classroom_id
   WHERE s.role = 'student';
INSERT INTO public.credit_accounts(kind, owner_id, parent_teacher_id)
  SELECT 'group', g.id, c.teacher_id FROM public.workgroups g JOIN public.classrooms c ON c.id = g.classroom_id;
INSERT INTO public.credit_ledger(kind, hours, to_kind, to_id, to_bucket, actor_role, meta)
  SELECT 'migrate', 0, a.kind, a.owner_id, 'free', 'system',
         jsonb_build_object(
           'old_credits', CASE WHEN a.kind = 'group' THEN g.shared_credits ELSE u.training_credits END,
           'old_used', CASE
             WHEN a.kind = 'group' THEN
               (SELECT count(*) FROM public.trainings t
                 WHERE t.workgroup_id = a.owner_id AND t.status NOT IN ('failed','canceled'))
             WHEN a.kind = 'student' THEN
               (SELECT count(*) FROM public.trainings t
                 WHERE t.user_id = a.owner_id AND t.workgroup_id IS NULL AND t.status NOT IN ('failed','canceled'))
             ELSE                                              -- a teacher: what the pool had allocated
               (SELECT COALESCE(sum(s2.training_credits), 0) FROM public.users s2
                  JOIN public.classrooms c ON c.id = s2.classroom_id
                 WHERE c.teacher_id = a.owner_id AND s2.role = 'student')
               + (SELECT COALESCE(sum(g2.shared_credits), 0) FROM public.workgroups g2
                    JOIN public.classrooms c ON c.id = g2.classroom_id
                   WHERE c.teacher_id = a.owner_id)
           END)
    FROM public.credit_accounts a
    LEFT JOIN public.users u ON u.id = a.owner_id
    LEFT JOIN public.workgroups g ON g.id = a.owner_id
   ORDER BY a.kind, a.owner_id;

-- 8. EXECUTE: callables to service_role only; internals and trigger functions to nobody.
-- A re-created function starts with PostgreSQL's default EXECUTE for PUBLIC, and
-- Supabase's default privileges add anon/authenticated: every one is in this loop.
DO $$
DECLARE r RECORD;
  internals TEXT[] := ARRAY['_credit_post','_credit_request_replay','_credit_reason','_credit_child_account',
    '_credit_return_free','_credit_lock_user','_credit_startable','_credit_open_seconds','_credit_meta','_credit_settle',
    '_credit_close','trainings_guard_credit_columns','trainings_credit_settle','trainings_credit_settle_delete',
    'users_credit_close','workgroups_credit_close','credit_reparent_on_move','credit_ledger_immutable'];
  callables TEXT[] := ARRAY['start_training_safe','start_continuation_safe','credit_admin_grant','credit_admin_revoke',
    'credit_admin_compensate','credit_assign','credit_reclaim','credit_set_member_limit','credit_quota','stop_training',
    'credit_history','credit_erase_reason','record_training_refusal','credit_reconcile','credit_teacher_summary',
    'get_remaining_credits','get_teacher_credit_summary','adjust_student_credits','adjust_workgroup_credits',
    'cap_training_attempt','requeue_training','finish_training','cancel_request_training','complete_cancel_training',
    'claim_training_publish','mark_checkpoint_ready','claim_checkpoint_mirror'];
BEGIN
  FOR r IN SELECT p.oid::regprocedure AS sig, p.proname FROM pg_proc p
            WHERE p.pronamespace = 'public'::regnamespace AND (p.proname = ANY (internals) OR p.proname = ANY (callables)) LOOP
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC, anon, authenticated', r.sig);
    IF r.proname = ANY (internals) THEN
      EXECUTE format('REVOKE ALL ON FUNCTION %s FROM service_role', r.sig);
    ELSE
      EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role', r.sig);
    END IF;
  END LOOP;
END $$;

-- 9. self-check (spec §5): any miss aborts the whole migration ---------------------
DO $$
DECLARE
  t TEXT; r TEXT; p TEXT; f RECORD; n INT;
  tabs TEXT[] := ARRAY['public.credit_accounts','public.credit_ledger','public.credit_reasons',
    'public.training_start_refusals'];
  internals TEXT[] := ARRAY['_credit_post','_credit_request_replay','_credit_reason','_credit_child_account',
    '_credit_return_free','_credit_lock_user','_credit_startable','_credit_open_seconds','_credit_meta','_credit_settle',
    '_credit_close','trainings_guard_credit_columns','trainings_credit_settle','trainings_credit_settle_delete',
    'users_credit_close','workgroups_credit_close','credit_reparent_on_move','credit_ledger_immutable'];
  callables TEXT[] := ARRAY['start_training_safe','start_continuation_safe','credit_admin_grant','credit_admin_revoke',
    'credit_admin_compensate','credit_assign','credit_reclaim','credit_set_member_limit','credit_quota','stop_training',
    'credit_history','credit_erase_reason','record_training_refusal','credit_reconcile','credit_teacher_summary',
    'get_remaining_credits','get_teacher_credit_summary','adjust_student_credits','adjust_workgroup_credits',
    'cap_training_attempt','requeue_training','finish_training','cancel_request_training','complete_cancel_training',
    'claim_training_publish','mark_checkpoint_ready','claim_checkpoint_mirror'];
  invokers TEXT[] := ARRAY['stop_training','cap_training_attempt','requeue_training','finish_training',
    'cancel_request_training','complete_cancel_training','claim_training_publish','trainings_guard_credit_columns',
    'mark_checkpoint_ready','claim_checkpoint_mirror'];
BEGIN
  -- the four tables: request roles nothing, service_role SELECT only, not published; their sequences: nobody
  FOREACH t IN ARRAY tabs LOOP
    FOREACH r IN ARRAY ARRAY['anon','authenticated'] LOOP
      FOREACH p IN ARRAY ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER'] LOOP
        IF has_table_privilege(r, t, p) THEN RAISE EXCEPTION '052: % holds % on %', r, p, t; END IF;
      END LOOP;
    END LOOP;
    IF NOT has_table_privilege('service_role', t, 'SELECT') THEN RAISE EXCEPTION '052: service_role cannot read %', t; END IF;
    FOREACH p IN ARRAY ARRAY['INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER'] LOOP
      IF has_table_privilege('service_role', t, p) THEN RAISE EXCEPTION '052: service_role holds % on %', p, t; END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_publication_tables pt WHERE pt.schemaname = 'public' AND 'public.' || pt.tablename = t) THEN
      RAISE EXCEPTION '052: % is in a publication', t;
    END IF;
    IF t <> 'public.credit_accounts' THEN
      FOREACH r IN ARRAY ARRAY['anon','authenticated','service_role'] LOOP
        FOREACH p IN ARRAY ARRAY['USAGE','SELECT','UPDATE'] LOOP
          IF has_sequence_privilege(r, pg_get_serial_sequence(t, 'id'), p) THEN
            RAISE EXCEPTION '052: % holds % on the sequence of %', r, p, t;
          END IF;
        END LOOP;
      END LOOP;
    END IF;
  END LOOP;
  FOREACH p IN ARRAY ARRAY['INSERT','TRUNCATE','REFERENCES','TRIGGER'] LOOP
    IF has_table_privilege('service_role', 'public.trainings', p) THEN
      RAISE EXCEPTION '052: service_role holds % on trainings', p;
    END IF;
  END LOOP;
  -- functions: request roles execute none; service_role every callable and no internal;
  -- every callable credit function DEFINER, the 049-pattern functions and the guard INVOKER
  SELECT count(*) INTO n FROM pg_proc pr
   WHERE pr.pronamespace = 'public'::regnamespace AND pr.proname = ANY (internals || callables);
  IF n <> cardinality(internals) + cardinality(callables) THEN
    RAISE EXCEPTION '052: % functions found, % expected', n, cardinality(internals) + cardinality(callables);
  END IF;
  FOR f IN SELECT pr.oid, pr.proname, pr.prosecdef, pr.proconfig FROM pg_proc pr
            WHERE pr.pronamespace = 'public'::regnamespace AND pr.proname = ANY (internals || callables) LOOP
    IF has_function_privilege('anon', f.oid, 'EXECUTE') OR has_function_privilege('authenticated', f.oid, 'EXECUTE') THEN
      RAISE EXCEPTION '052: a request role executes %', f.proname;
    END IF;
    IF f.proname = ANY (internals) AND has_function_privilege('service_role', f.oid, 'EXECUTE') THEN
      RAISE EXCEPTION '052: service_role executes the internal %', f.proname;
    END IF;
    IF f.proname = ANY (callables) AND NOT has_function_privilege('service_role', f.oid, 'EXECUTE') THEN
      RAISE EXCEPTION '052: service_role cannot execute %', f.proname;
    END IF;
    IF f.proname = ANY (callables) AND NOT (f.proname = ANY (invokers))
       AND (NOT f.prosecdef OR NOT ('search_path=public' = ANY (COALESCE(f.proconfig, '{}'::text[])))) THEN
      RAISE EXCEPTION '052: % is not SECURITY DEFINER with search_path = public', f.proname;
    END IF;
    IF f.proname = ANY (invokers) AND f.prosecdef THEN
      RAISE EXCEPTION '052: % must stay SECURITY INVOKER', f.proname;
    END IF;
  END LOOP;
  FOREACH p IN ARRAY ARRAY['start_training_safe','start_continuation_safe','finish_training','get_remaining_credits',
                           'cancel_request_training'] LOOP
    SELECT count(*) INTO n FROM pg_proc pr WHERE pr.pronamespace = 'public'::regnamespace AND pr.proname = p;
    IF n <> 1 THEN RAISE EXCEPTION '052: % has % overloads', p, n; END IF;
  END LOOP;
  -- the four granted trainings columns and the five that stay hidden, for both request roles
  FOREACH r IN ARRAY ARRAY['anon','authenticated'] LOOP
    FOREACH p IN ARRAY ARRAY['reserved_hours','charged_hours','stop_requested_by_role','lost_seconds'] LOOP
      IF NOT has_column_privilege(r, 'public.trainings', p, 'SELECT') THEN
        RAISE EXCEPTION '052: % cannot read trainings.%', r, p;
      END IF;
    END LOOP;
    FOREACH p IN ARRAY ARRAY['worker_token','lost_reasons','charge_waiver','idempotency_key','settled_at'] LOOP
      IF has_column_privilege(r, 'public.trainings', p, 'SELECT') THEN
        RAISE EXCEPTION '052: % can read trainings.%', r, p;
      END IF;
    END LOOP;
  END LOOP;
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND
             ((table_name = 'trainings' AND column_name IN ('stop_requested_by','time_limit_s'))
           OR (table_name = 'training_attempts' AND column_name = 'host_key'))) THEN
    RAISE EXCEPTION '052: a dropped column exists';
  END IF;
  -- the nine triggers with their timing (tgtype bit 2 = BEFORE)
  SELECT count(*) INTO n FROM pg_trigger tg WHERE NOT tg.tgisinternal AND (
       (tg.tgname IN ('credit_ledger_no_change','credit_ledger_no_truncate','trg_0_trainings_credit_guard',
                      'trg_9_trainings_credit_settle','trg_9_trainings_credit_settle_delete') AND tg.tgtype & 2 = 2)
    OR (tg.tgname IN ('trg_9_users_credit_close','trg_9_workgroups_credit_close','trg_9_users_credit_reparent',
                      'trg_9_workgroups_credit_reparent') AND tg.tgtype & 2 = 0));
  IF n <> 9 THEN RAISE EXCEPTION '052: % of 9 triggers with their timing', n; END IF;
  -- update_training_progress: one overload, the fix-round stall clock, still executable by the worker's roles
  SELECT count(*) INTO n FROM pg_proc pr WHERE pr.pronamespace = 'public'::regnamespace AND pr.proname = 'update_training_progress';
  IF n <> 1 THEN RAISE EXCEPTION '052: update_training_progress has % overloads', n; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_proc pr WHERE pr.pronamespace = 'public'::regnamespace
                   AND pr.proname = 'update_training_progress' AND pr.prosecdef
                   AND strpos(pr.prosrc, 'WHEN step_advanced_at IS NULL OR (p_status = ''running'' AND status = ''queued'') THEN NOW()') > 0
                   AND strpos(pr.prosrc, 'WHEN step_advanced_at IS NULL OR p_status = ''running'' THEN NOW()') = 0) THEN
    RAISE EXCEPTION '052: update_training_progress does not carry the fix-round stall clock';
  END IF;
  FOREACH r IN ARRAY ARRAY['anon','authenticated'] LOOP
    IF NOT has_function_privilege(r, 'public.update_training_progress(integer,uuid,text,integer,integer,real,text,text)', 'EXECUTE') THEN
      RAISE EXCEPTION '052: % lost EXECUTE on update_training_progress', r;
    END IF;
  END LOOP;
  IF has_function_privilege('public', 'public.update_training_progress(integer,uuid,text,integer,integer,real,text,text)', 'EXECUTE') THEN
    RAISE EXCEPTION '052: PUBLIC executes update_training_progress';
  END IF;
  -- fix round 2: the checkpoint writers and the sixth waiver condition
  IF NOT EXISTS (SELECT 1 FROM pg_proc pr WHERE pr.pronamespace = 'public'::regnamespace
                   AND pr.proname = 'claim_checkpoint_mirror'
                   AND strpos(pr.prosrc, 'AND t.status IN (''queued'',''running'',''succeeded'')') > 0) THEN
    RAISE EXCEPTION '052: claim_checkpoint_mirror does not refuse a failed or canceled training';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_proc pr WHERE pr.pronamespace = 'public'::regnamespace
                   AND pr.proname = 'finish_training' AND strpos(pr.prosrc, 'v_t.current_step IS NOT DISTINCT FROM 0') > 0) THEN
    RAISE EXCEPTION '052: finish_training lacks the waiver''s step condition';
  END IF;
  -- finish_training refuses the waiver without proof (P0073)
  IF NOT EXISTS (SELECT 1 FROM pg_proc pr WHERE pr.pronamespace = 'public'::regnamespace
                   AND pr.proname = 'finish_training' AND strpos(pr.prosrc, 'P0073') > 0) THEN
    RAISE EXCEPTION '052: finish_training lacks the P0073 waiver proof';
  END IF;
  IF (SELECT o_drift FROM public.credit_reconcile()) <> 0 THEN RAISE EXCEPTION '052: account cache drift'; END IF;
  IF EXISTS (SELECT 1 FROM public.credit_accounts a WHERE a.parent_teacher_id IS NOT NULL AND NOT EXISTS
             (SELECT 1 FROM public.credit_accounts tt WHERE tt.kind = 'teacher' AND tt.owner_id = a.parent_teacher_id)) THEN
    RAISE EXCEPTION '052: a child account without its teacher account';
  END IF;
  RAISE NOTICE '052 self-check passed';
END $$;

COMMIT;

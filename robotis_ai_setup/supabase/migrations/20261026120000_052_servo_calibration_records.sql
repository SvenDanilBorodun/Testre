-- 052 — servo_calibration_records: one row per calibrated V1 servo, uploaded by
-- the vendor's Servo-Station (tools/servo_station/, build spec S9).
--
-- The station's LOCAL record file is the commit point (records/<serial>.json on
-- the bench PC); this table is the server copy, so a servo returned by a school
-- can be looked up by the serial on its label. Nothing deployed reads it: the
-- cloud API never touches the table or the RPC (the station talks to PostgREST
-- directly), so 052 is deliberately NOT in the API's boot schema probe (owner
-- R4-2; CLAUDE.md leaves objects with no API-visible shape unprobed, like
-- 043/044/046/047).
--
-- WHO MAY WRITE: an ADMIN account, and only through
-- submit_servo_calibration_record(jsonb). The bench PC never holds the
-- service-role key: the station signs in as an admin over Supabase Auth and
-- calls the RPC with that JWT. The RPC is SECURITY DEFINER (its owner bypasses
-- RLS) and its FIRST statement refuses every caller that is not an admin
-- (public.rls_is_admin(), 031) with 42501. EXECUTE is granted to authenticated
-- (+ service_role) and revoked from anon; a student calling it gets 42501.
--
-- THE TABLE (Rule §4): RLS on, NO policy, every privilege revoked from PUBLIC,
-- anon and authenticated, service_role only, in no publication — the 036/042
-- service-role-only pattern.
--
-- A record whose calibrated_at is OLDER than the stored one is not written
-- (answer stored=false, reason newer_record_exists): a queued upload from a
-- station that was offline can never overwrite a later re-calibration.
--
-- Rollback: rollback/20261026120000_052_servo_calibration_records_rollback.sql
-- (refuses while rows exist; stop uploading from the Servo-Station first).
-- Hand-run assertions: supabase/tests/052_servo_calibration_records_assertions.sql.
-- Idempotent — re-runnable.

BEGIN;

CREATE TABLE IF NOT EXISTS public.servo_calibration_records (
    servo_serial  text PRIMARY KEY CHECK (servo_serial ~ '^V1S-[0-9]{6}$'),
    joint         text NOT NULL CHECK (joint IN ('joint1','joint2','joint3','joint4','joint5','RL_joint')),
    servo_id      smallint NOT NULL,
    model_number  integer NOT NULL CHECK (model_number IN (777, 2825)),
    spec_sha256   text NOT NULL CHECK (spec_sha256 ~ '^[0-9a-f]{64}$'),
    record_sha256 text NOT NULL CHECK (record_sha256 ~ '^[0-9a-f]{64}$'),
    calibrated_at timestamptz NOT NULL,
    record        jsonb NOT NULL CHECK (jsonb_typeof(record) = 'object') CHECK (octet_length(record::text) <= 65536),
    uploaded_by   uuid REFERENCES public.users(id) ON DELETE SET NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    -- the bus id travels with the servo: each joint has exactly one id ...
    CONSTRAINT servo_calibration_records_joint_id
        CHECK ((joint, servo_id) IN (('joint1',1),('joint2',2),('joint3',3),('joint4',4),('joint5',5),('RL_joint',6))),
    -- ... and the high-load shoulder + elbow (ids 2/3) are the only STS3250s.
    CONSTRAINT servo_calibration_records_model
        CHECK ((servo_id IN (2, 3)) = (model_number = 2825))
);
-- record->'incoming_test' may be null (Wareneingang is optional, owner OQ9): no constraint.

ALTER TABLE public.servo_calibration_records ENABLE ROW LEVEL SECURITY;

-- Supabase's default privileges grant every new public table to anon and
-- authenticated; with RLS on and no policy that is a closed table, and the grants
-- are revoked as well (defence in depth, 042).
REVOKE ALL ON TABLE public.servo_calibration_records FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.servo_calibration_records TO service_role;

COMMENT ON TABLE public.servo_calibration_records IS
    'One row per calibrated V1 servo, uploaded by the vendor Servo-Station through submit_servo_calibration_record (052). Service-role only: RLS on, no policies, no anon/authenticated grants. Not in Realtime. The station''s local record file is the commit point.';

-- updated_at maintenance (the 036 idiom).
CREATE OR REPLACE FUNCTION public.servo_calibration_records_touch()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = ''
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS servo_calibration_records_touch ON public.servo_calibration_records;
CREATE TRIGGER servo_calibration_records_touch
    BEFORE UPDATE ON public.servo_calibration_records
    FOR EACH ROW EXECUTE FUNCTION public.servo_calibration_records_touch();

-- ---------------------------------------------------------------------------
-- the one writer
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.submit_servo_calibration_record(p_record jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_serial   text;
    v_joint    text;
    v_servo_id smallint;
    v_model    integer;
    v_spec     text;
    v_checksum text;
    v_at       timestamptz;
    v_stored   timestamptz;
    v_replaced boolean;
BEGIN
    IF NOT public.rls_is_admin() THEN
        RAISE EXCEPTION 'servo calibration upload requires an admin account' USING ERRCODE = '42501';
    END IF;

    -- 2. The record's shape. A bad cast inside the sub-block is a malformed
    --    record too (22023), never a raw cast error.
    IF p_record IS NULL OR jsonb_typeof(p_record) <> 'object'
       OR p_record->>'schema' IS DISTINCT FROM 'edubotics.servo_calibration.v1' THEN
        RAISE EXCEPTION 'malformed servo calibration record' USING ERRCODE = '22023';
    END IF;
    BEGIN
        v_serial   := p_record->>'servo_serial';
        v_joint    := p_record->>'joint';
        v_servo_id := (p_record->>'servo_id')::smallint;
        v_model    := (p_record->>'model_number')::integer;
        v_spec     := p_record->>'spec_sha256';
        v_checksum := p_record->>'checksum';
        v_at       := (p_record->>'calibrated_at')::timestamptz;
    EXCEPTION WHEN OTHERS THEN
        RAISE EXCEPTION 'malformed servo calibration record' USING ERRCODE = '22023';
    END;
    IF v_serial IS NULL OR v_joint IS NULL OR v_servo_id IS NULL OR v_model IS NULL
       OR v_spec IS NULL OR v_checksum IS NULL OR v_at IS NULL THEN
        RAISE EXCEPTION 'malformed servo calibration record' USING ERRCODE = '22023';
    END IF;

    -- 3. A stored row that is NEWER wins: a queued upload never overwrites a
    --    later re-calibration of the same servo.
    SELECT r.calibrated_at INTO v_stored
      FROM public.servo_calibration_records r
     WHERE r.servo_serial = v_serial
       FOR UPDATE;
    v_replaced := FOUND;
    IF v_replaced AND v_stored > v_at THEN
        RETURN jsonb_build_object('stored', false, 'reason', 'newer_record_exists',
                                  'servo_serial', v_serial);
    END IF;

    -- 4. Upsert. The CHECK constraints refuse a bad serial, joint, id/model pair
    --    or hash with 23514.
    INSERT INTO public.servo_calibration_records AS r
        (servo_serial, joint, servo_id, model_number, spec_sha256, record_sha256,
         calibrated_at, record, uploaded_by)
    VALUES
        (v_serial, v_joint, v_servo_id, v_model, v_spec, v_checksum,
         v_at, p_record, auth.uid())
    ON CONFLICT (servo_serial) DO UPDATE SET
        joint          = EXCLUDED.joint,
        servo_id       = EXCLUDED.servo_id,
        model_number   = EXCLUDED.model_number,
        spec_sha256    = EXCLUDED.spec_sha256,
        record_sha256  = EXCLUDED.record_sha256,
        calibrated_at  = EXCLUDED.calibrated_at,
        record         = EXCLUDED.record,
        uploaded_by    = auth.uid();

    RETURN jsonb_build_object('stored', true, 'replaced', v_replaced,
                              'servo_serial', v_serial, 'calibrated_at', v_at);
END;
$$;

COMMENT ON FUNCTION public.submit_servo_calibration_record(jsonb) IS
    'SECURITY DEFINER, admin only (42501 otherwise): upsert one Servo-Station calibration record, unless a newer one is stored (052).';

REVOKE ALL ON FUNCTION public.submit_servo_calibration_record(jsonb) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.submit_servo_calibration_record(jsonb) TO authenticated, service_role;

-- Run the SECURITY DEFINER writer with a bypassing-RLS owner (the 042 pattern).
-- rls_is_admin() (031: EXECUTE for anon/authenticated) runs inside as that owner.
DO $$
DECLARE
  v_done BOOLEAN := FALSE;
  v_role TEXT;
BEGIN
  FOREACH v_role IN ARRAY ARRAY['postgres', 'supabase_admin', 'supabase_storage_admin'] LOOP
    BEGIN
      EXECUTE format('ALTER FUNCTION public.submit_servo_calibration_record(jsonb) OWNER TO %I', v_role);
      v_done := TRUE;
      EXIT;
    EXCEPTION
      WHEN OTHERS THEN
        CONTINUE;
    END;
  END LOOP;
  IF NOT v_done THEN
    RAISE NOTICE 'Could not transfer ownership of submit_servo_calibration_record to a privileged role.';
  END IF;
END $$;

-- Self-check: abort (and roll the whole migration back) if a request role can
-- still touch the table, if anon can call the writer or authenticated cannot.
DO $$
DECLARE
  v_role TEXT;
  v_priv TEXT;
BEGIN
  FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
    FOREACH v_priv IN ARRAY ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER'] LOOP
      IF has_table_privilege(v_role, 'public.servo_calibration_records', v_priv) THEN
        RAISE EXCEPTION '052: % still holds % on servo_calibration_records', v_role, v_priv;
      END IF;
    END LOOP;
  END LOOP;
  IF has_function_privilege('anon', 'public.submit_servo_calibration_record(jsonb)', 'EXECUTE') THEN
    RAISE EXCEPTION '052: anon can execute submit_servo_calibration_record';
  END IF;
  IF NOT has_function_privilege('authenticated', 'public.submit_servo_calibration_record(jsonb)', 'EXECUTE') THEN
    RAISE EXCEPTION '052: authenticated cannot execute submit_servo_calibration_record';
  END IF;
END $$;

COMMIT;

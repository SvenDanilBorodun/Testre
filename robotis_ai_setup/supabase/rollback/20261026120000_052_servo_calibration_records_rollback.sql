-- Rollback for 052 — drop servo_calibration_records and its upload RPC.
--
-- The cloud API does not use this table (not in its boot probe), so there is no
-- API step: stop uploading from the Servo-Station first, its local records stay
-- the commit point (records/<serial>.json on the bench PC); a station that
-- uploads afterwards is told „Migration 052 fehlt" and keeps its records queued.
--
-- NOT SAFE TO RUN BLIND: rows are calibration evidence. This script REFUSES
-- while the table holds any row — export them first (as service_role:
-- `SELECT record FROM public.servo_calibration_records`), then delete them and
-- run it again.
--
-- ONE `DO` block, like 051's rollback and for the same reason: a bare `psql -f`
-- without ON_ERROR_STOP prints a RAISE and carries on, so a multi-statement
-- rollback could drop half the objects. The RAISE rolls back everything the
-- block already did. Drops in dependency order: the RPC, the trigger, its
-- function, the table.

DO $$
DECLARE
    v_rows bigint := 0;
BEGIN
    IF to_regclass('public.servo_calibration_records') IS NOT NULL THEN
        EXECUTE 'SELECT count(*) FROM public.servo_calibration_records' INTO v_rows;
        IF v_rows > 0 THEN
            RAISE EXCEPTION 'Rollback refused: % rows. Export them first.', v_rows;
        END IF;
    END IF;

    EXECUTE 'DROP FUNCTION IF EXISTS public.submit_servo_calibration_record(jsonb)';
    IF to_regclass('public.servo_calibration_records') IS NOT NULL THEN
        EXECUTE 'DROP TRIGGER IF EXISTS servo_calibration_records_touch ON public.servo_calibration_records';
    END IF;
    EXECUTE 'DROP FUNCTION IF EXISTS public.servo_calibration_records_touch()';
    EXECUTE 'DROP TABLE IF EXISTS public.servo_calibration_records';
END
$$;

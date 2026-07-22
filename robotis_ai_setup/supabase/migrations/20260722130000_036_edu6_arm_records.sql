-- 036 — edu6_arm_records (edu6 plan §6, decision Q3: vendor-sticker serial).
--
-- One row per physical edu6 arm, written by the vendor-bench provisioning tool
-- (tools/edu6_provision.py, service-role). The record duplicates what lives in
-- the servos' own EEPROM (offsets/limits/safety values) so an arm moved to
-- another PC — or a servo RMA — can be verified/re-written from the server
-- copy. The LOCAL json file the tool writes stays the offline-classroom copy.
--
-- Service-role only: no student/teacher route reads this yet (the student-
-- connect compare uses the servo EEPROM itself; the fetch-by-serial flow lands
-- with the first RMA need). RLS is enabled with NO policies — service-role
-- bypasses RLS, everything else is denied (defense-in-depth, Rule §4).

CREATE TABLE IF NOT EXISTS public.edu6_arm_records (
    arm_serial text PRIMARY KEY,
    record jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.edu6_arm_records ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE public.edu6_arm_records IS
    'Per-arm edu6 servo calibration records (vendor bench, tools/edu6_provision.py). Keyed by the vendor sticker serial.';

-- updated_at maintenance (same trigger idiom as the other tables).
CREATE OR REPLACE FUNCTION public.edu6_arm_records_touch()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = ''
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS edu6_arm_records_touch ON public.edu6_arm_records;
CREATE TRIGGER edu6_arm_records_touch
    BEFORE UPDATE ON public.edu6_arm_records
    FOR EACH ROW EXECUTE FUNCTION public.edu6_arm_records_touch();

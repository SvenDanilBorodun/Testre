-- Rollback for 036 — drops the edu6 arm-record table (additive; nothing in
-- the deployed API reads it, only the vendor bench tool writes it).
DROP TRIGGER IF EXISTS edu6_arm_records_touch ON public.edu6_arm_records;
DROP FUNCTION IF EXISTS public.edu6_arm_records_touch();
DROP TABLE IF EXISTS public.edu6_arm_records;

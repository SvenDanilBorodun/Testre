-- Rollback for 044_users_protected_columns. Drops the BEFORE UPDATE guard on
-- public.users and its function. No data is touched.
--
-- WARNING: this RE-OPENS the hole 044 closed — a signed-in student can again
-- PATCH their own role, training_credits, hf_username, classroom_id and
-- workgroup_id through PostgREST — and PATCH /me's 409 is again a
-- check-then-write in the cloud API only. Run it only to unblock a regression,
-- and roll forward again as soon as possible. The 044 cloud API keeps working
-- against a database without 044 (it maps P0044 to 409; without the trigger
-- that code simply never arrives).
BEGIN;

DROP TRIGGER IF EXISTS trg_0_users_protected_columns ON public.users;
DROP FUNCTION IF EXISTS public.users_guard_protected_columns();

COMMIT;

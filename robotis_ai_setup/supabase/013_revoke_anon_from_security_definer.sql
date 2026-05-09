-- 013: Revoke anon + authenticated EXECUTE on service-role-only RPCs.
--
-- Problem: Supabase ships pg_default_acl rules that auto-grant EXECUTE
-- to anon, authenticated, AND service_role on every new function in the
-- public schema. The migration scripts that defined these RPCs say
-- `REVOKE ALL ON FUNCTION ... FROM PUBLIC` followed by `GRANT EXECUTE
-- TO service_role` — but PUBLIC is the inheritable role; explicit
-- grants to anon/authenticated installed by the default ACL survive
-- the REVOKE FROM PUBLIC. The result: anyone with the React-bundled
-- anon key can call:
--
--   - start_training_safe(p_user_id, ...)            -- queue trainings on any user
--   - adjust_student_credits(p_teacher_id, ...)      -- grant/take credits
--   - adjust_workgroup_credits(p_teacher_id, ...)    -- same for groups
--   - get_remaining_credits(p_user_id)               -- read any user's quota
--   - get_teacher_credit_summary(p_teacher_id)       -- read any teacher's pool
--
-- update_training_progress is intentionally callable by anon (the
-- Modal worker uses the anon key + a per-row worker_token), so this
-- migration leaves it alone.
--
-- Idempotent: REVOKE on a role that has no privilege is a no-op.

BEGIN;

REVOKE EXECUTE ON FUNCTION public.start_training_safe(UUID, TEXT, TEXT, TEXT, JSONB, INT, UUID)
  FROM anon, authenticated;

REVOKE EXECUTE ON FUNCTION public.adjust_student_credits(UUID, UUID, INTEGER)
  FROM anon, authenticated;

REVOKE EXECUTE ON FUNCTION public.adjust_workgroup_credits(UUID, UUID, INTEGER)
  FROM anon, authenticated;

-- get_remaining_credits had no REVOKE/GRANT block at all in 011 (DROP +
-- CREATE OR REPLACE without the privilege block left it with the default
-- ACL: =X/postgres meaning PUBLIC has EXECUTE). REVOKE FROM PUBLIC + GRANT
-- to service_role explicitly. anon/authenticated inherit from PUBLIC, so
-- this also fixes them.
REVOKE EXECUTE ON FUNCTION public.get_remaining_credits(UUID) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.get_remaining_credits(UUID) FROM anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_remaining_credits(UUID) TO service_role;

REVOKE EXECUTE ON FUNCTION public.get_teacher_credit_summary(UUID)
  FROM anon, authenticated;

COMMIT;

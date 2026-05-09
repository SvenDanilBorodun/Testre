-- Rollback for 013_revoke_anon_from_security_definer.sql
--
-- Restores the (broken) state where anon + authenticated could execute
-- the service-role-only RPCs. Use only if a regression in 013 turns
-- out to break a legitimate caller — none should exist (the cloud API
-- always uses the service-role key).

BEGIN;

GRANT EXECUTE ON FUNCTION public.start_training_safe(UUID, TEXT, TEXT, TEXT, JSONB, INT, UUID)
  TO anon, authenticated;

GRANT EXECUTE ON FUNCTION public.adjust_student_credits(UUID, UUID, INTEGER)
  TO anon, authenticated;

GRANT EXECUTE ON FUNCTION public.adjust_workgroup_credits(UUID, UUID, INTEGER)
  TO anon, authenticated;

GRANT EXECUTE ON FUNCTION public.get_remaining_credits(UUID)
  TO anon, authenticated;

GRANT EXECUTE ON FUNCTION public.get_teacher_credit_summary(UUID)
  TO anon, authenticated;

COMMIT;

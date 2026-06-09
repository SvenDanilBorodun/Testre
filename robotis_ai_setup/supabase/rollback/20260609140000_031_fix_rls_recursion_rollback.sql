-- ============================================================================
-- ROLLBACK for 20260609140000_031_fix_rls_recursion.sql
-- ----------------------------------------------------------------------------
-- Restores the migration-024 recursive SELECT policies VERBATIM and drops the
-- helper functions. WARNING: this re-introduces the 42P17 infinite-recursion
-- bug that breaks Supabase Realtime delivery (live training progress goes dead
-- again). Only run if migration 031 caused an unexpected regression; the
-- correct forward fix is to amend 031, not to roll back to the broken state.
-- ============================================================================

-- Restore the recursive users policy (migration 024 form).
DROP POLICY IF EXISTS "Users read consolidated" ON public.users;
CREATE POLICY "Users read consolidated"
  ON public.users
  FOR SELECT
  USING (
    id = (SELECT auth.uid())
    OR classroom_id IN (
      SELECT id FROM public.classrooms WHERE teacher_id = (SELECT auth.uid())
    )
    OR EXISTS (
      SELECT 1 FROM public.users u2
      WHERE u2.id = (SELECT auth.uid()) AND u2.role = 'admin'
    )
  );

-- Restore the recursive classrooms policy (migration 024 form).
DROP POLICY IF EXISTS "Classrooms read consolidated" ON public.classrooms;
CREATE POLICY "Classrooms read consolidated"
  ON public.classrooms
  FOR SELECT
  USING (
    teacher_id = (SELECT auth.uid())
    OR id = (SELECT classroom_id FROM public.users WHERE id = (SELECT auth.uid()))
    OR EXISTS (
      SELECT 1 FROM public.users
      WHERE id = (SELECT auth.uid()) AND role = 'admin'
    )
  );

DROP FUNCTION IF EXISTS public.rls_teacher_classroom_ids();
DROP FUNCTION IF EXISTS public.rls_user_classroom_id();
DROP FUNCTION IF EXISTS public.rls_is_admin();

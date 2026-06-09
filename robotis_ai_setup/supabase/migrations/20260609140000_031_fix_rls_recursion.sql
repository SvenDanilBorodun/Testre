-- ============================================================================
-- 20260609140000_031_fix_rls_recursion.sql
-- ----------------------------------------------------------------------------
-- Fix: "live training steps do not work" — Supabase Realtime delivered ZERO
-- postgres_changes events for trainings (and every other realtime table whose
-- RLS policy reads users/classrooms), so the React Training tab's step counter
-- and loss chart sat frozen even while the Modal worker was writing
-- current_step / loss_history to the row.
--
-- ROOT CAUSE (migration 024, 2026-05-20). The consolidated SELECT policies
-- introduced INFINITE RECURSION:
--   * public.users  "Users read consolidated" — its 3rd clause is
--       EXISTS (SELECT 1 FROM public.users u2 WHERE u2.id = auth.uid()
--               AND u2.role = 'admin')
--     i.e. the users SELECT policy reads the users table, which re-applies the
--     same SELECT policy → 42P17 "infinite recursion detected in policy for
--     relation users". Its 2nd clause reads public.classrooms, whose policy in
--     turn reads public.users → mutual recursion.
--   * public.classrooms "Classrooms read consolidated" — clauses 2 and 3 read
--     public.users → same mutual recursion.
--
-- Why it went unnoticed for three weeks: every cloud_training_api query runs
-- as the Supabase SERVICE-ROLE key, which BYPASSES RLS (Rule §4), so the
-- recursion never fired for the API. The ONLY consumer that evaluates these
-- policies as the authenticated end-user is Supabase Realtime's
-- realtime.apply_rls() / list_changes() (the "walrus" WAL→RLS engine). It
-- raised 42P17 on every single change for the tenant, so the realtime stream
-- delivered nothing. The channel still reported SUBSCRIBED, so the frontend's
-- 30s poll fallback (which only runs when NOT subscribed) never engaged → a
-- silently dead live UI. Verified in the realtime service logs:
--   PoolingReplicationError ... 42P17: infinite recursion detected in policy
--   for relation "users" ... realtime.apply_rls(jsonb,integer) ...
--   SQL function "list_changes"
-- and reproduced directly: SET ROLE authenticated; SELECT FROM public.users
-- → 42P17.
--
-- FIX. Break the recursion by moving every cross-table / self-table read out
-- of the policy predicate and into SECURITY DEFINER helper functions. A
-- SECURITY DEFINER function runs as its owner (postgres) and therefore does
-- NOT re-trigger the caller-role RLS on the tables it reads — so the policy
-- predicate no longer recurses. The AUTHORIZATION SEMANTICS ARE UNCHANGED:
-- each rewritten clause is a 1:1 translation of the original 024 clause.
--
-- Blast radius: read-side RLS only. The cloud API (service-role) is unaffected.
-- The fix unblocks Realtime delivery for trainings AND every other realtime
-- table whose policy transitively reads users/classrooms (datasets, workflows,
-- workgroup_memberships, ...). No RPC signatures change, so the boot-time
-- _validate_required_schema() probe is unaffected.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. SECURITY DEFINER helpers. STABLE (one evaluation per statement is fine),
--    search_path pinned to public (hardening + correctness). They read
--    users/classrooms as the owner, bypassing RLS → no recursion.
-- ----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.rls_is_admin()
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT COALESCE(
    (SELECT u.role FROM public.users u WHERE u.id = (SELECT auth.uid())) = 'admin',
    false
  );
$$;

COMMENT ON FUNCTION public.rls_is_admin() IS
  'SECURITY DEFINER: true iff the calling user is an admin. Used inside the '
  'users/classrooms SELECT policies so the admin check does not re-enter the '
  'users RLS policy (migration 031 recursion fix).';

CREATE OR REPLACE FUNCTION public.rls_user_classroom_id()
RETURNS uuid
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT u.classroom_id FROM public.users u WHERE u.id = (SELECT auth.uid());
$$;

COMMENT ON FUNCTION public.rls_user_classroom_id() IS
  'SECURITY DEFINER: the calling user''s own classroom_id. Lets the classrooms '
  'SELECT policy resolve "my classroom" without reading public.users under RLS '
  '(migration 031 recursion fix).';

CREATE OR REPLACE FUNCTION public.rls_teacher_classroom_ids()
RETURNS SETOF uuid
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT c.id FROM public.classrooms c WHERE c.teacher_id = (SELECT auth.uid());
$$;

COMMENT ON FUNCTION public.rls_teacher_classroom_ids() IS
  'SECURITY DEFINER: classroom ids owned by the calling teacher. Lets the '
  'users SELECT policy resolve "students in my classrooms" without reading '
  'public.classrooms under RLS (migration 031 recursion fix).';

-- RLS predicates are evaluated as the querying role, so anon + authenticated
-- (and Realtime, which impersonates authenticated) must be able to EXECUTE.
REVOKE ALL ON FUNCTION public.rls_is_admin()              FROM PUBLIC;
REVOKE ALL ON FUNCTION public.rls_user_classroom_id()     FROM PUBLIC;
REVOKE ALL ON FUNCTION public.rls_teacher_classroom_ids() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.rls_is_admin()              TO anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rls_user_classroom_id()     TO anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rls_teacher_classroom_ids() TO anon, authenticated;

-- ----------------------------------------------------------------------------
-- 2. Re-create the two recursive SELECT policies using the helpers. Each clause
--    is the SAME predicate as migration 024, just with the table read pushed
--    into a SECURITY DEFINER function. Semantics: unchanged.
-- ----------------------------------------------------------------------------

-- users: own row OR student-in-my-classroom (teacher) OR admin-sees-all.
DROP POLICY IF EXISTS "Users read consolidated" ON public.users;
CREATE POLICY "Users read consolidated"
  ON public.users
  FOR SELECT
  USING (
    id = (SELECT auth.uid())
    OR classroom_id IN (SELECT public.rls_teacher_classroom_ids())
    OR public.rls_is_admin()
  );

COMMENT ON POLICY "Users read consolidated" ON public.users IS
  'own-profile OR teacher-sees-classroom-students OR admin-all. Cross-table '
  'reads via SECURITY DEFINER helpers so the predicate does not recurse '
  '(migration 031). Semantics identical to migration 024.';

-- classrooms: my classroom (teacher) OR my classroom (student) OR admin.
DROP POLICY IF EXISTS "Classrooms read consolidated" ON public.classrooms;
CREATE POLICY "Classrooms read consolidated"
  ON public.classrooms
  FOR SELECT
  USING (
    teacher_id = (SELECT auth.uid())
    OR id = public.rls_user_classroom_id()
    OR public.rls_is_admin()
  );

COMMENT ON POLICY "Classrooms read consolidated" ON public.classrooms IS
  'teacher-owns OR student-member OR admin-all. Cross-table reads via '
  'SECURITY DEFINER helpers so the predicate does not recurse (migration 031). '
  'Semantics identical to migration 024.';

-- 056 — an admin reads other people's rows only in a two-factor session.
--
-- THE GAP (VA-04 of the wave-A verification, 2026-11-12). The owner decided that
-- admins need a second factor, and the cloud API enforces it: every admin route
-- depends on auth.py::get_current_admin, which refuses an `aal1` token (403
-- `mfa_required`). The DATABASE did not agree. Its admin read branches looked only
-- at public.users.role, so an attacker holding the admin PASSWORD alone (exactly
-- what TOTP is there to neutralise) signed in with supabase-js and the public anon
-- key and read, straight through PostgREST, every user row (e-mail, name, username,
-- Hugging Face name, classroom), every classroom, workgroup, membership, dataset,
-- workflow, version, trajectory, submission and progress entry — without ever
-- meeting the TOTP screen. Measured on a fresh chain: an `aal1` admin sees every
-- users row; a student sees one.
--
-- INVENTORY — every policy and function of the applied chain (000 → 055) that
-- grants the `admin` role anything, enumerated from pg_policies and pg_proc on a
-- fresh PostgreSQL 16 replay (not from memory):
--   SELECT policies that read the admin branch through public.rls_is_admin()
--   (031, the recursion fix):
--     public.users          "Users read consolidated"
--     public.classrooms     "Classrooms read consolidated"
--   SELECT policies with an INLINE `EXISTS (… users … role = 'admin')` branch
--   (024 unless named; * = in the supabase_realtime publication, so Realtime's
--   per-subscriber RLS check reads it too):
--     public.progress_entries       "Progress entries read consolidated"
--     public.workflows *            "Workflows read consolidated"
--     public.workgroups *           "Workgroups read consolidated"
--     public.workgroup_memberships* "Memberships read consolidated"
--     public.datasets *             "Datasets read consolidated"
--     public.workflow_versions *    "Workflow versions read consolidated"
--     public.tutorial_progress *    "Tutorial progress read consolidated"
--     public.workflow_trajectories  "Admin reads all workflow trajectories" (034)
--     public.workflow_submissions   "Admin reads all submissions" (040)
--   NOT an admin path: public.trainings "Trainings read consolidated" (no admin
--   branch; checked), the version trigger's INSERT policy, and every other policy.
--   No admin WRITE policy exists anywhere (046 and 055 removed the request roles'
--   writes; admin writes go through the service-role API).
--   SECURITY DEFINER functions that name the admin role:
--     public.rls_is_admin()                    EXECUTE anon, authenticated — CHANGED here;
--     public.credit_admin_grant/_revoke/_compensate, public.credit_erase_reason,
--     public.consume_vision_quota              EXECUTE service_role only, reached
--       only through the API, whose admin routes already require aal2 — unchanged.
--   The teacher helpers public.rls_teacher_classroom_ids() and
--   public.rls_user_classroom_id() do not branch on admin — unchanged.
--
-- THE FIX — one predicate for "the caller is an admin", and it requires aal2:
--   1. public.rls_is_admin() keeps its signature, SECURITY DEFINER and
--      `SET search_path = public` (031's recursion lesson: a users/classrooms
--      policy must never read those tables under the caller's RLS) and gains
--      `AND (SELECT auth.jwt() ->> 'aal') = 'aal2'`. A token without the claim is
--      not aal2. CREATE OR REPLACE keeps its OID and its grants.
--   2. Each of the nine inline admin branches becomes `(SELECT
--      public.rls_is_admin())` by ALTER POLICY … USING: same names, commands,
--      roles and comments, every other branch of each predicate byte-for-byte as
--      024/034/040 wrote it. The scalar subquery keeps the branch an InitPlan
--      (evaluated once per statement, like the EXISTS it replaces). users and
--      classrooms already call the helper and are not touched.
--   3. A self-check aborts (and so rolls back) the migration if any public policy
--      still carries an inline admin check, if the set of policies using the
--      helper is not exactly the eleven above, or if the helper lost its
--      definer, its search_path, its aal2 rule or a grant.
--
-- WHAT A TEACHER OR A STUDENT SEES IS UNCHANGED: no other branch moves, and the
-- `aal` claim is read only on the admin branch. An aal2 admin sees what every
-- admin saw before. Realtime evaluates the same policies with the subscriber's
-- claims (which carry `aal`); no admin page subscribes (the teacher web reads only
-- through the API, and AdminMfaGate stands in front of the admin dashboard).
--
-- Not API-visible: the API runs as service_role (RLS dormant), so the boot schema
-- probe is unchanged. Rollback: rollback/20261112120000_056_admin_requires_aal2_rollback.sql
-- (031's helper and the nine predicates exactly as 024/034/040 wrote them). Hand-run
-- assertions: tests/056_admin_requires_aal2_assertions.sql.

BEGIN;

-- 1. The helper -------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.rls_is_admin()
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT COALESCE(
    (SELECT u.role FROM public.users u WHERE u.id = (SELECT auth.uid())) = 'admin'
    AND (SELECT auth.jwt() ->> 'aal') = 'aal2',
    false
  );
$$;

COMMENT ON FUNCTION public.rls_is_admin() IS
  'SECURITY DEFINER: true iff the calling user is an admin AND the session is '
  'two-factor (JWT aal = aal2, migration 056). The ONE admin predicate of every '
  'RLS policy; reads public.users as its owner so a users/classrooms policy does '
  'not re-enter the users RLS policy (migration 031 recursion fix).';

-- 2. The nine inline admin branches -------------------------------------------

ALTER POLICY "Progress entries read consolidated" ON public.progress_entries
  USING (
    classroom_id IN (
      SELECT id FROM public.classrooms WHERE teacher_id = (SELECT auth.uid())
    )
    OR student_id = (SELECT auth.uid())
    OR (
      student_id IS NULL AND workgroup_id IS NULL
      AND classroom_id = (
        SELECT classroom_id FROM public.users WHERE id = (SELECT auth.uid())
      )
    )
    OR (
      workgroup_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM public.workgroup_memberships m
        WHERE m.user_id = (SELECT auth.uid())
          AND m.workgroup_id = progress_entries.workgroup_id
      )
    )
    OR (SELECT public.rls_is_admin())
  );

ALTER POLICY "Workflows read consolidated" ON public.workflows
  USING (
    owner_user_id = (SELECT auth.uid())
    OR (
      is_template = TRUE
      AND classroom_id = (
        SELECT classroom_id FROM public.users WHERE id = (SELECT auth.uid())
      )
    )
    OR (
      is_template = TRUE
      AND classroom_id IN (
        SELECT id FROM public.classrooms WHERE teacher_id = (SELECT auth.uid())
      )
    )
    OR (
      workgroup_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM public.workgroup_memberships m
        WHERE m.user_id = (SELECT auth.uid())
          AND m.workgroup_id = workflows.workgroup_id
      )
    )
    OR (SELECT public.rls_is_admin())
  );

ALTER POLICY "Workgroups read consolidated" ON public.workgroups
  USING (
    classroom_id IN (
      SELECT id FROM public.classrooms WHERE teacher_id = (SELECT auth.uid())
    )
    OR id = (
      SELECT workgroup_id FROM public.users WHERE id = (SELECT auth.uid())
    )
    OR (SELECT public.rls_is_admin())
  );

ALTER POLICY "Memberships read consolidated" ON public.workgroup_memberships
  USING (
    user_id = (SELECT auth.uid())
    OR workgroup_id IN (
      SELECT g.id FROM public.workgroups g
      JOIN public.classrooms c ON c.id = g.classroom_id
      WHERE c.teacher_id = (SELECT auth.uid())
    )
    OR (SELECT public.rls_is_admin())
  );

ALTER POLICY "Datasets read consolidated" ON public.datasets
  USING (
    owner_user_id = (SELECT auth.uid())
    OR (
      workgroup_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM public.workgroup_memberships m
        WHERE m.user_id = (SELECT auth.uid())
          AND m.workgroup_id = datasets.workgroup_id
      )
    )
    OR workgroup_id IN (
      SELECT g.id FROM public.workgroups g
      JOIN public.classrooms c ON c.id = g.classroom_id
      WHERE c.teacher_id = (SELECT auth.uid())
    )
    OR (SELECT public.rls_is_admin())
  );

ALTER POLICY "Workflow versions read consolidated" ON public.workflow_versions
  USING (
    EXISTS (
      SELECT 1 FROM public.workflows w
       WHERE w.id = workflow_versions.workflow_id
         AND w.owner_user_id = (SELECT auth.uid())
    )
    OR EXISTS (
      SELECT 1
        FROM public.workflows w
        JOIN public.workgroup_memberships m
          ON m.workgroup_id = w.workgroup_id
       WHERE w.id = workflow_versions.workflow_id
         AND w.workgroup_id IS NOT NULL
         AND m.user_id = (SELECT auth.uid())
    )
    OR (SELECT public.rls_is_admin())
  );

ALTER POLICY "Tutorial progress read consolidated" ON public.tutorial_progress
  USING (
    user_id = (SELECT auth.uid())
    OR (SELECT public.rls_is_admin())
    OR EXISTS (
      SELECT 1
        FROM public.users student
        JOIN public.classrooms c ON c.id = student.classroom_id
       WHERE student.id = tutorial_progress.user_id
         AND c.teacher_id = (SELECT auth.uid())
    )
  );

ALTER POLICY "Admin reads all workflow trajectories" ON public.workflow_trajectories
  USING ((SELECT public.rls_is_admin()));

ALTER POLICY "Admin reads all submissions" ON public.workflow_submissions
  USING ((SELECT public.rls_is_admin()));

-- 3. Self-check ---------------------------------------------------------------

DO $$
DECLARE
    v_fn      pg_proc%ROWTYPE;
    v_inline  TEXT;
    v_users   TEXT[];
    v_role    TEXT;
    v_expect  TEXT[] := ARRAY[
        'classrooms/Classrooms read consolidated',
        'datasets/Datasets read consolidated',
        'progress_entries/Progress entries read consolidated',
        'tutorial_progress/Tutorial progress read consolidated',
        'users/Users read consolidated',
        'workflow_submissions/Admin reads all submissions',
        'workflow_trajectories/Admin reads all workflow trajectories',
        'workflow_versions/Workflow versions read consolidated',
        'workflows/Workflows read consolidated',
        'workgroup_memberships/Memberships read consolidated',
        'workgroups/Workgroups read consolidated'];
BEGIN
    SELECT p.* INTO v_fn FROM pg_proc p
     WHERE p.oid = 'public.rls_is_admin()'::regprocedure;
    IF NOT v_fn.prosecdef THEN
        RAISE EXCEPTION '056: public.rls_is_admin() is not SECURITY DEFINER';
    END IF;
    IF v_fn.proconfig IS DISTINCT FROM ARRAY['search_path=public'] THEN
        RAISE EXCEPTION '056: public.rls_is_admin() lost SET search_path = public';
    END IF;
    IF position('''aal2''' IN v_fn.prosrc) = 0 OR position('auth.jwt()' IN v_fn.prosrc) = 0
       OR position('''admin''' IN v_fn.prosrc) = 0 THEN
        RAISE EXCEPTION '056: public.rls_is_admin() does not require role admin AND aal2';
    END IF;
    FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF NOT has_function_privilege(v_role, 'public.rls_is_admin()', 'EXECUTE') THEN
            RAISE EXCEPTION '056: % lost EXECUTE on public.rls_is_admin() (every RLS check would fail)', v_role;
        END IF;
    END LOOP;

    -- No policy of the public schema may decide "admin" by itself any more.
    SELECT string_agg(tablename || '/' || policyname, ', ') INTO v_inline
      FROM pg_policies
     WHERE schemaname = 'public'
       AND (coalesce(qual, '') ~ '''admin''::user_role' OR coalesce(with_check, '') ~ '''admin''::user_role'
            OR coalesce(qual, '') ~* 'role\s*=\s*''admin''' OR coalesce(with_check, '') ~* 'role\s*=\s*''admin''');
    IF v_inline IS NOT NULL THEN
        RAISE EXCEPTION '056: inline admin checks left in: %', v_inline;
    END IF;

    SELECT array_agg(tablename || '/' || policyname ORDER BY tablename || '/' || policyname) INTO v_users
      FROM pg_policies
     WHERE schemaname = 'public'
       AND (coalesce(qual, '') ~ 'rls_is_admin\(\)' OR coalesce(with_check, '') ~ 'rls_is_admin\(\)');
    IF v_users IS DISTINCT FROM v_expect THEN
        RAISE EXCEPTION '056: the policies using rls_is_admin() are %, expected %', v_users, v_expect;
    END IF;

    -- Behaviour, without a request role: no claims, no admin.
    IF public.rls_is_admin() THEN
        RAISE EXCEPTION '056: rls_is_admin() is true without any claims';
    END IF;
    RAISE NOTICE '056 self-check passed';
END $$;

COMMIT;

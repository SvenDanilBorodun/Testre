-- Rollback of 056 — the admin's read branches without the two-factor rule.
--
-- Restores what 20261112120000_056_admin_requires_aal2.sql changed, as 055 left it:
--   * public.rls_is_admin() exactly as 031 (20260609140000_031_fix_rls_recursion.sql)
--     created it — role admin, no aal claim — with 031's comment. CREATE OR
--     REPLACE keeps its OID, so its grants (anon, authenticated) stay;
--   * the nine SELECT predicates 056 changed, each exactly as its migration wrote
--     it: "Progress entries read consolidated", "Workflows read consolidated",
--     "Workgroups read consolidated", "Memberships read consolidated", "Datasets
--     read consolidated", "Workflow versions read consolidated" and "Tutorial
--     progress read consolidated" as 024 (20260520130522_024_advisor_fixes.sql),
--     "Admin reads all workflow trajectories" as 034 and "Admin reads all
--     submissions" as 040 — the inline EXISTS (… users … role = 'admin') branch
--     back in place of (SELECT public.rls_is_admin()). ALTER POLICY changes only
--     the USING expression; names, commands, roles and comments never moved.
--
-- WARNING: this RE-OPENS what 056 closed. An admin signed in with only a
-- PASSWORD (aal1) can again read every user, classroom, workgroup, membership,
-- dataset, workflow, version, trajectory, submission and progress entry directly
-- through PostgREST with the public anon key, without the TOTP screen. The cloud
-- API is unaffected either way (it runs as service_role, and its admin routes
-- require aal2 themselves), so no API rollback is needed first. Run it only to
-- unblock a regression and roll forward again.
--
-- Idempotent: every statement sets a state, it does not toggle one. The closing
-- DO block checks the restored state. Measured 2026-11-12: after this file,
-- pg_dump -s of the database equals the dump taken before 056.

BEGIN;

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
    OR EXISTS (
      SELECT 1 FROM public.users
       WHERE id = (SELECT auth.uid()) AND role = 'admin'
    )
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
    OR EXISTS (
      SELECT 1 FROM public.users
       WHERE id = (SELECT auth.uid()) AND role = 'admin'
    )
  );

ALTER POLICY "Workgroups read consolidated" ON public.workgroups
  USING (
    classroom_id IN (
      SELECT id FROM public.classrooms WHERE teacher_id = (SELECT auth.uid())
    )
    OR id = (
      SELECT workgroup_id FROM public.users WHERE id = (SELECT auth.uid())
    )
    OR EXISTS (
      SELECT 1 FROM public.users
       WHERE id = (SELECT auth.uid()) AND role = 'admin'
    )
  );

ALTER POLICY "Memberships read consolidated" ON public.workgroup_memberships
  USING (
    user_id = (SELECT auth.uid())
    OR workgroup_id IN (
      SELECT g.id FROM public.workgroups g
      JOIN public.classrooms c ON c.id = g.classroom_id
      WHERE c.teacher_id = (SELECT auth.uid())
    )
    OR EXISTS (
      SELECT 1 FROM public.users
       WHERE id = (SELECT auth.uid()) AND role = 'admin'
    )
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
    OR EXISTS (
      SELECT 1 FROM public.users
       WHERE id = (SELECT auth.uid()) AND role = 'admin'
    )
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
    OR EXISTS (
      SELECT 1 FROM public.users
       WHERE id = (SELECT auth.uid()) AND role = 'admin'
    )
  );

ALTER POLICY "Tutorial progress read consolidated" ON public.tutorial_progress
  USING (
    user_id = (SELECT auth.uid())
    OR EXISTS (
      SELECT 1 FROM public.users
       WHERE id = (SELECT auth.uid()) AND role = 'admin'
    )
    OR EXISTS (
      SELECT 1
        FROM public.users student
        JOIN public.classrooms c ON c.id = student.classroom_id
       WHERE student.id = tutorial_progress.user_id
         AND c.teacher_id = (SELECT auth.uid())
    )
  );

ALTER POLICY "Admin reads all workflow trajectories" ON public.workflow_trajectories
  USING (
    EXISTS (
      SELECT 1 FROM public.users
      WHERE id = (SELECT auth.uid()) AND role = 'admin'
    )
  );

ALTER POLICY "Admin reads all submissions" ON public.workflow_submissions
  USING (
    EXISTS (
      SELECT 1 FROM public.users
      WHERE id = (SELECT auth.uid()) AND role = 'admin'
    )
  );

DO $$
DECLARE
    v_src TEXT;
    v_n   INT;
BEGIN
    SELECT prosrc INTO v_src FROM pg_proc WHERE oid = 'public.rls_is_admin()'::regprocedure;
    IF position('aal' IN v_src) > 0 THEN
        RAISE EXCEPTION '056 rollback: public.rls_is_admin() still reads the aal claim';
    END IF;
    SELECT count(*) INTO v_n FROM pg_policies
     WHERE schemaname = 'public' AND coalesce(qual, '') ~ '''admin''::user_role';
    IF v_n <> 9 THEN
        RAISE EXCEPTION '056 rollback: % policies carry the inline admin branch, expected 9', v_n;
    END IF;
    SELECT count(*) INTO v_n FROM pg_policies
     WHERE schemaname = 'public' AND coalesce(qual, '') ~ 'rls_is_admin\(\)';
    IF v_n <> 2 THEN
        RAISE EXCEPTION '056 rollback: % policies use rls_is_admin(), expected 2 (users, classrooms)', v_n;
    END IF;
    IF NOT has_function_privilege('authenticated', 'public.rls_is_admin()', 'EXECUTE') THEN
        RAISE EXCEPTION '056 rollback: authenticated lost EXECUTE on public.rls_is_admin()';
    END IF;
    RAISE NOTICE '056 rollback self-check passed';
END $$;

COMMIT;

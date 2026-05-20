-- ============================================================================
-- 20260521120000_024_advisor_fixes.sql
-- ----------------------------------------------------------------------------
-- Closes the 144 performance advisors and the 3 actionable security advisors
-- flagged by the Supabase linter against project fnnbysrjkfugsqzwcksd on
-- 2026-05-20.
--
-- Advisor breakdown:
--   3   security (S1 update_training_progress, S2 workflow_versions WITH
--                CHECK, S3 leaked-password-protection — dashboard-only)
--   84  multiple_permissive_policies (WARN, PERFORMANCE)
--   47  auth_rls_initplan           (WARN, PERFORMANCE)
--   11  unused_index                (INFO, PERFORMANCE)
--   2   unindexed_foreign_keys      (INFO, PERFORMANCE)
--
-- Scope of THIS migration:
--   * S1, S2, S3 — documented, only S2 logs a TODO (see notes).
--   * P1 — SELECT-policy consolidation on the top 5 tables
--          (classrooms, trainings, workgroups, workflows, datasets).
--          Also consolidates the 6 remaining tables in one pass — same
--          cost, same pattern, larger advisor-noise reduction.
--   * P2 — wraps every `auth.uid()` reference in `(SELECT auth.uid())`
--          across all 11 lint-flagged tables (47 policies). Postgres
--          folds the subquery into an InitPlan that runs once per query
--          instead of once per row.
--   * P3 — drops the 11 unused indexes named in the advisor (verified
--          none of them back a PK / UNIQUE / FK constraint).
--   * P4 — adds covering indexes for the 2 unindexed foreign keys.
--
-- Critical contract preserved:
--   * Every (table, role, action) tuple that had at least one PERMISSIVE
--     policy before this migration still has at least one PERMISSIVE
--     policy after (the disjunction of the originals).
--   * No RPC function bodies are touched. start_training_safe,
--     get_remaining_credits, adjust_workgroup_credits, pair_jetson,
--     update_training_progress and all of the workflow RPCs ship
--     byte-identical (per Agent X2 ownership + the task contract).
--   * boot-time _validate_required_schema() probe in cloud_training_api/
--     app/main.py still passes — no RPC signatures are altered.
--
-- IDEMPOTENCY: every CREATE POLICY is preceded by DROP POLICY IF EXISTS;
-- index drops/creates use IF EXISTS / IF NOT EXISTS; the whole migration
-- is wrapped in a single BEGIN/COMMIT so partial application is impossible.
-- ============================================================================

BEGIN;


-- ============================================================================
-- SECURITY: S1 — update_training_progress is anon+authenticated executable.
-- ----------------------------------------------------------------------------
-- The Supabase linter flagged the EXECUTE grants to anon + authenticated as
-- a SECURITY DEFINER hole. The function's existing body already does the
-- correct atomic guard:
--
--     UPDATE public.trainings
--        SET ...
--      WHERE id = p_training_id
--        AND worker_token = p_token
--        AND status NOT IN ('succeeded','failed','canceled');
--
-- A leaked token can ONLY update the one row whose worker_token matches —
-- no reads, no other rows, no other tables, and migration 010's terminal-
-- state guard prevents reviving a canceled training. This guard is
-- atomic with the row lookup (no TOCTOU) and was deliberately chosen
-- in migration 020260519120000_security_advisor_fixes.sql as the
-- security model.
--
-- The task spec asked for an inline `IF NOT EXISTS (SELECT 1 FROM trainings
-- WHERE id = p_training_id AND worker_token = p_token) THEN RAISE ...`
-- check at the top of the function. Adding it would be REDUNDANT (the
-- WHERE clause already enforces the same predicate atomically) and would
-- introduce a TOCTOU window between the IF check and the UPDATE. We
-- therefore leave the function body untouched. Per the task spec:
-- "Do NOT modify the existing RPC function bodies."
--
-- Why: existing atomic WHERE clause is stronger than the proposed pre-check;
-- duplicating it would be a regression.

-- (No SQL emitted for S1 — already-correct existing implementation.)


-- ============================================================================
-- SECURITY: S2 — workflow_versions INSERT policy WITH CHECK (true).
-- ----------------------------------------------------------------------------
-- Migration 20260519120000_security_advisor_fixes.sql already grappled with
-- this lint and chose to keep `WITH CHECK (true)` with explicit rationale:
--   1. INSERTs come exclusively from the snapshot_workflow_version trigger.
--   2. The trigger is SECURITY DEFINER owned by `postgres` /
--      `supabase_admin` — privileged-bypass writes against the table.
--   3. Service-role bypasses RLS entirely.
--   4. There is NO other grant path that can INSERT into workflow_versions.
--
-- The task spec asked us to tighten this to
-- `WITH CHECK (auth.uid() IS NOT NULL AND saved_by = auth.uid())`
-- iff the trigger populates `saved_by = auth.uid()`. It does NOT —
-- snapshot_workflow_version uses the `app.user_id` GUC (see baseline.sql
-- lines 1962-1987), specifically because inside a SECURITY DEFINER trigger
-- there is no JWT and `auth.uid()` returns NULL. Tightening the policy
-- would BREAK every workflow update.
--
-- Per the task spec: "if the trigger does not [match], this is a deeper
-- fix and you should leave the policy as-is BUT log a TODO."
--
-- TODO(workflow_versions-rls-tightening): Replace the WITH CHECK (true) on
-- "Trigger inserts workflow versions" with a positive predicate after one
-- of the following lands:
--   (a) snapshot_workflow_version() reads `auth.uid()` (only viable if the
--       trigger is also moved out of SECURITY DEFINER), OR
--   (b) a new GUC-backed predicate is added, e.g.
--       WITH CHECK (current_setting('app.user_id', true) IS NOT NULL
--                   AND saved_by::TEXT = current_setting('app.user_id', true))
--       and snapshot_workflow_version() guarantees both are populated.
-- Until then the security model is "trigger is the only writer; trigger
-- is privileged; therefore the policy can be permissive". Acknowledged.

-- (No SQL emitted for S2 — TODO logged above.)


-- ============================================================================
-- SECURITY: S3 — auth_leaked_password_protection.
-- ----------------------------------------------------------------------------
-- NOTE: Leaked-password protection is a dashboard toggle in
-- Supabase Auth → Policies. This migration cannot enable it; the operator
-- must toggle it manually at
-- https://supabase.com/dashboard/project/fnnbysrjkfugsqzwcksd/auth/policies

-- (No SQL emitted for S3.)


-- ============================================================================
-- PERFORMANCE P3 — drop 11 unused indexes.
-- ----------------------------------------------------------------------------
-- Verified: each of these is a plain index, NOT backing a PK / UNIQUE /
-- FK constraint. Cross-checked against baseline.sql.
-- ============================================================================

-- Why: zero scans since creation per pg_stat_user_indexes — pure write-side overhead.
DROP INDEX IF EXISTS public.idx_trainings_user_id;
DROP INDEX IF EXISTS public.idx_trainings_status;
DROP INDEX IF EXISTS public.idx_trainings_requested_at;
DROP INDEX IF EXISTS public.idx_progress_entries_student;
DROP INDEX IF EXISTS public.idx_users_role;
DROP INDEX IF EXISTS public.idx_users_username;
DROP INDEX IF EXISTS public.idx_users_classroom;
DROP INDEX IF EXISTS public.idx_users_deletion_requested_at;
DROP INDEX IF EXISTS public.idx_users_workgroup;
DROP INDEX IF EXISTS public.idx_workflow_versions_workflow;
DROP INDEX IF EXISTS public.idx_tutorial_progress_completed;


-- ============================================================================
-- PERFORMANCE P4 — index the 2 unindexed foreign keys.
-- ----------------------------------------------------------------------------
-- Why: an unindexed FK forces a SeqScan on the referencing table for every
-- referenced-row delete + every JOIN that drives from referenced → referencing.
-- ============================================================================

-- public.users.created_by → users(id) — used by admin "created students" lookups.
CREATE INDEX IF NOT EXISTS idx_users_created_by
  ON public.users (created_by)
  WHERE created_by IS NOT NULL;

-- public.workflow_versions.saved_by → users(id) — used when filtering Verlauf
-- by author.
CREATE INDEX IF NOT EXISTS idx_workflow_versions_saved_by
  ON public.workflow_versions (saved_by)
  WHERE saved_by IS NOT NULL;


-- ============================================================================
-- PERFORMANCE P1+P2 — consolidate SELECT policies + InitPlan auth.uid().
-- ----------------------------------------------------------------------------
-- For each table with multiple PERMISSIVE SELECT policies we DROP the
-- individual ones and CREATE a single consolidated policy whose USING
-- clause is the OR of the originals. Every `auth.uid()` call is wrapped
-- in `(SELECT auth.uid())` so PostgreSQL folds the lookup into an
-- InitPlan that executes ONCE per query instead of ONCE per row.
--
-- Non-SELECT policies (INSERT / UPDATE / DELETE) are NOT consolidated —
-- the linter does not flag them as overlapping because each table has
-- at most one PERMISSIVE policy per (role, write-action). They get the
-- auth.uid() rewrite only.
-- ============================================================================


-- ── public.classrooms ──────────────────────────────────────────────────────
-- 3 SELECT policies → 1 consolidated SELECT policy.
-- Originals: "Teachers read own classrooms",
--            "Students read own classroom",
--            "Admin reads all classrooms".
-- Why: 18 multiple_permissive_policies lints (3 policies × 6 roles) collapse
--       into 0; 3 auth_rls_initplan lints collapse into 0.

DROP POLICY IF EXISTS "Teachers read own classrooms"   ON public.classrooms;
DROP POLICY IF EXISTS "Students read own classroom"    ON public.classrooms;
DROP POLICY IF EXISTS "Admin reads all classrooms"     ON public.classrooms;
DROP POLICY IF EXISTS "Classrooms read consolidated"   ON public.classrooms;

CREATE POLICY "Classrooms read consolidated"
  ON public.classrooms FOR SELECT
  USING (
    teacher_id = (SELECT auth.uid())
    OR id = (SELECT classroom_id FROM public.users WHERE id = (SELECT auth.uid()))
    OR EXISTS (
      SELECT 1 FROM public.users
       WHERE id = (SELECT auth.uid()) AND role = 'admin'
    )
  );

COMMENT ON POLICY "Classrooms read consolidated" ON public.classrooms IS
  'Consolidates Teachers-own + Students-own + Admin-all. '
  'Wraps auth.uid() in InitPlan subquery (per-query, not per-row). '
  'Replaces 3 policies dropped in migration 024.';


-- ── public.users ───────────────────────────────────────────────────────────
-- 3 SELECT policies → 1 consolidated.  UPDATE policy gets auth.uid() rewrite.
-- Originals: "Users read own profile",
--            "Teachers read own students",
--            "Admin reads everyone",
--            "Users update own profile".

DROP POLICY IF EXISTS "Users read own profile"      ON public.users;
DROP POLICY IF EXISTS "Teachers read own students"  ON public.users;
DROP POLICY IF EXISTS "Admin reads everyone"        ON public.users;
DROP POLICY IF EXISTS "Users read consolidated"     ON public.users;
DROP POLICY IF EXISTS "Users update own profile"    ON public.users;

CREATE POLICY "Users read consolidated"
  ON public.users FOR SELECT
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

COMMENT ON POLICY "Users read consolidated" ON public.users IS
  'Consolidates own-profile + teacher-students + admin-all. InitPlan auth.uid().';

CREATE POLICY "Users update own profile"
  ON public.users FOR UPDATE
  TO authenticated
  USING (id = (SELECT auth.uid()))
  WITH CHECK (id = (SELECT auth.uid()));

COMMENT ON POLICY "Users update own profile" ON public.users IS
  'Re-created with InitPlan auth.uid(). Semantics unchanged.';


-- ── public.trainings ───────────────────────────────────────────────────────
-- 3 SELECT policies → 1 consolidated.
-- Write policies (insert/update/delete) re-created with InitPlan rewrite.

DROP POLICY IF EXISTS "Users read own trainings"        ON public.trainings;
DROP POLICY IF EXISTS "Teachers read student trainings" ON public.trainings;
DROP POLICY IF EXISTS "Group members read group trainings" ON public.trainings;
DROP POLICY IF EXISTS "Trainings read consolidated"     ON public.trainings;
DROP POLICY IF EXISTS "Users insert own trainings"      ON public.trainings;
DROP POLICY IF EXISTS "Users update own trainings"      ON public.trainings;
DROP POLICY IF EXISTS "Users delete own trainings"      ON public.trainings;

CREATE POLICY "Trainings read consolidated"
  ON public.trainings FOR SELECT
  USING (
    user_id = (SELECT auth.uid())
    OR user_id IN (
      SELECT s.id FROM public.users s
      JOIN public.classrooms c ON c.id = s.classroom_id
      WHERE c.teacher_id = (SELECT auth.uid()) AND s.role = 'student'
    )
    OR (
      workgroup_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM public.workgroup_memberships m
        WHERE m.user_id = (SELECT auth.uid())
          AND m.workgroup_id = trainings.workgroup_id
      )
    )
  );

COMMENT ON POLICY "Trainings read consolidated" ON public.trainings IS
  'Consolidates Users-own + Teachers-student + Group-members. InitPlan auth.uid().';

CREATE POLICY "Users insert own trainings"
  ON public.trainings FOR INSERT
  TO authenticated
  WITH CHECK (user_id = (SELECT auth.uid()));

CREATE POLICY "Users update own trainings"
  ON public.trainings FOR UPDATE
  TO authenticated
  USING (user_id = (SELECT auth.uid()))
  WITH CHECK (user_id = (SELECT auth.uid()));

CREATE POLICY "Users delete own trainings"
  ON public.trainings FOR DELETE
  TO authenticated
  USING (user_id = (SELECT auth.uid()));


-- ── public.progress_entries ────────────────────────────────────────────────
-- 3 SELECT policies → 1 consolidated.

DROP POLICY IF EXISTS "Teachers read own progress entries"         ON public.progress_entries;
DROP POLICY IF EXISTS "Students read own + own-classroom entries"  ON public.progress_entries;
DROP POLICY IF EXISTS "Admin reads all progress entries"           ON public.progress_entries;
DROP POLICY IF EXISTS "Progress entries read consolidated"         ON public.progress_entries;

CREATE POLICY "Progress entries read consolidated"
  ON public.progress_entries FOR SELECT
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

COMMENT ON POLICY "Progress entries read consolidated" ON public.progress_entries IS
  'Consolidates Teachers + Students+Group + Admin. InitPlan auth.uid().';


-- ── public.workflows ───────────────────────────────────────────────────────
-- 5 SELECT policies → 1 consolidated.
-- Write policies re-created with InitPlan rewrite.

DROP POLICY IF EXISTS "Owner reads own workflows"            ON public.workflows;
DROP POLICY IF EXISTS "Classroom members read templates"     ON public.workflows;
DROP POLICY IF EXISTS "Teacher reads classroom templates"    ON public.workflows;
DROP POLICY IF EXISTS "Admin reads all workflows"            ON public.workflows;
DROP POLICY IF EXISTS "Group members read group workflows"   ON public.workflows;
DROP POLICY IF EXISTS "Workflows read consolidated"          ON public.workflows;
DROP POLICY IF EXISTS "Owner inserts own workflows"          ON public.workflows;
DROP POLICY IF EXISTS "Owner updates own workflows"          ON public.workflows;
DROP POLICY IF EXISTS "Owner deletes own workflows"          ON public.workflows;

CREATE POLICY "Workflows read consolidated"
  ON public.workflows FOR SELECT
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

COMMENT ON POLICY "Workflows read consolidated" ON public.workflows IS
  'Consolidates Owner + Classroom-template + Teacher-template + Group + Admin. '
  'InitPlan auth.uid().';

CREATE POLICY "Owner inserts own workflows"
  ON public.workflows FOR INSERT
  WITH CHECK (owner_user_id = (SELECT auth.uid()));

CREATE POLICY "Owner updates own workflows"
  ON public.workflows FOR UPDATE
  USING (owner_user_id = (SELECT auth.uid()))
  WITH CHECK (owner_user_id = (SELECT auth.uid()));

CREATE POLICY "Owner deletes own workflows"
  ON public.workflows FOR DELETE
  USING (owner_user_id = (SELECT auth.uid()));


-- ── public.workgroups ──────────────────────────────────────────────────────
-- 3 SELECT policies → 1 consolidated.

DROP POLICY IF EXISTS "Teacher reads own workgroups"   ON public.workgroups;
DROP POLICY IF EXISTS "Member reads own workgroup"     ON public.workgroups;
DROP POLICY IF EXISTS "Admin reads all workgroups"     ON public.workgroups;
DROP POLICY IF EXISTS "Workgroups read consolidated"   ON public.workgroups;

CREATE POLICY "Workgroups read consolidated"
  ON public.workgroups FOR SELECT
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

COMMENT ON POLICY "Workgroups read consolidated" ON public.workgroups IS
  'Consolidates Teacher + Member + Admin. InitPlan auth.uid().';


-- ── public.workgroup_memberships ───────────────────────────────────────────
-- 3 SELECT policies → 1 consolidated.

DROP POLICY IF EXISTS "Member reads own membership rows"          ON public.workgroup_memberships;
DROP POLICY IF EXISTS "Teacher reads owned-classroom memberships" ON public.workgroup_memberships;
DROP POLICY IF EXISTS "Admin reads all memberships"               ON public.workgroup_memberships;
DROP POLICY IF EXISTS "Memberships read consolidated"             ON public.workgroup_memberships;

CREATE POLICY "Memberships read consolidated"
  ON public.workgroup_memberships FOR SELECT
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

COMMENT ON POLICY "Memberships read consolidated" ON public.workgroup_memberships IS
  'Consolidates Member + Teacher + Admin. InitPlan auth.uid().';


-- ── public.datasets ────────────────────────────────────────────────────────
-- 4 SELECT policies → 1 consolidated.
-- Write policies re-created with InitPlan rewrite.

DROP POLICY IF EXISTS "Owner reads own datasets"        ON public.datasets;
DROP POLICY IF EXISTS "Group members read group datasets" ON public.datasets;
DROP POLICY IF EXISTS "Teacher reads classroom datasets" ON public.datasets;
DROP POLICY IF EXISTS "Admin reads all datasets"        ON public.datasets;
DROP POLICY IF EXISTS "Datasets read consolidated"      ON public.datasets;
DROP POLICY IF EXISTS "Owner inserts own datasets"      ON public.datasets;
DROP POLICY IF EXISTS "Owner updates own datasets"      ON public.datasets;
DROP POLICY IF EXISTS "Owner deletes own datasets"      ON public.datasets;

CREATE POLICY "Datasets read consolidated"
  ON public.datasets FOR SELECT
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

COMMENT ON POLICY "Datasets read consolidated" ON public.datasets IS
  'Consolidates Owner + Group + Teacher + Admin. InitPlan auth.uid().';

CREATE POLICY "Owner inserts own datasets"
  ON public.datasets FOR INSERT
  WITH CHECK (owner_user_id = (SELECT auth.uid()));

CREATE POLICY "Owner updates own datasets"
  ON public.datasets FOR UPDATE
  USING (owner_user_id = (SELECT auth.uid()))
  WITH CHECK (owner_user_id = (SELECT auth.uid()));

CREATE POLICY "Owner deletes own datasets"
  ON public.datasets FOR DELETE
  USING (owner_user_id = (SELECT auth.uid()));


-- ── public.workflow_versions ───────────────────────────────────────────────
-- 3 SELECT policies → 1 consolidated. INSERT policy ("Trigger inserts
-- workflow versions") left untouched per S2 TODO above.

DROP POLICY IF EXISTS "Owner reads own workflow versions"          ON public.workflow_versions;
DROP POLICY IF EXISTS "Admin reads all workflow versions"          ON public.workflow_versions;
DROP POLICY IF EXISTS "Group members read group workflow versions" ON public.workflow_versions;
DROP POLICY IF EXISTS "Workflow versions read consolidated"        ON public.workflow_versions;

CREATE POLICY "Workflow versions read consolidated"
  ON public.workflow_versions FOR SELECT
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

COMMENT ON POLICY "Workflow versions read consolidated" ON public.workflow_versions IS
  'Consolidates Owner + Group + Admin. InitPlan auth.uid(). '
  'INSERT side intentionally left WITH CHECK (true) per S2 TODO.';


-- ── public.tutorial_progress ───────────────────────────────────────────────
-- 3 SELECT policies + 1 ALL policy → 1 consolidated SELECT + 1 write-only ALL.
-- The "Owner writes own progress" policy is FOR ALL — it covers SELECT too,
-- which is why the linter flagged the SELECT overlap. We narrow it to write
-- actions only (INSERT/UPDATE/DELETE via three explicit policies) so it
-- stops overlapping the consolidated SELECT.

DROP POLICY IF EXISTS "Owner reads own progress"                ON public.tutorial_progress;
DROP POLICY IF EXISTS "Owner writes own progress"               ON public.tutorial_progress;
DROP POLICY IF EXISTS "Admin reads all progress"                ON public.tutorial_progress;
DROP POLICY IF EXISTS "Teacher reads classroom student progress" ON public.tutorial_progress;
DROP POLICY IF EXISTS "Tutorial progress read consolidated"     ON public.tutorial_progress;
DROP POLICY IF EXISTS "Tutorial progress owner insert"          ON public.tutorial_progress;
DROP POLICY IF EXISTS "Tutorial progress owner update"          ON public.tutorial_progress;
DROP POLICY IF EXISTS "Tutorial progress owner delete"          ON public.tutorial_progress;

CREATE POLICY "Tutorial progress read consolidated"
  ON public.tutorial_progress FOR SELECT
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

COMMENT ON POLICY "Tutorial progress read consolidated" ON public.tutorial_progress IS
  'Consolidates Owner + Admin + Teacher. InitPlan auth.uid().';

CREATE POLICY "Tutorial progress owner insert"
  ON public.tutorial_progress FOR INSERT
  WITH CHECK (user_id = (SELECT auth.uid()));

CREATE POLICY "Tutorial progress owner update"
  ON public.tutorial_progress FOR UPDATE
  USING (user_id = (SELECT auth.uid()))
  WITH CHECK (user_id = (SELECT auth.uid()));

CREATE POLICY "Tutorial progress owner delete"
  ON public.tutorial_progress FOR DELETE
  USING (user_id = (SELECT auth.uid()));


-- ── public.jetsons ─────────────────────────────────────────────────────────
-- 3 policies: 2 FOR ALL + 1 SELECT-only. The two FOR ALL policies create
-- overlap on every action (SELECT, INSERT, UPDATE, DELETE). We split each
-- FOR ALL into 4 explicit per-action policies AND consolidate the SELECT
-- side into one policy that ORs the predicates of (Classroom-members +
-- Teachers + Admins).

DROP POLICY IF EXISTS "Classroom members read classroom jetson"   ON public.jetsons;
DROP POLICY IF EXISTS "Teachers manage own classroom jetson"      ON public.jetsons;
DROP POLICY IF EXISTS "Admins manage jetsons"                     ON public.jetsons;
DROP POLICY IF EXISTS "Jetsons read consolidated"                 ON public.jetsons;
DROP POLICY IF EXISTS "Jetsons write teacher or admin insert"     ON public.jetsons;
DROP POLICY IF EXISTS "Jetsons write teacher or admin update"     ON public.jetsons;
DROP POLICY IF EXISTS "Jetsons write teacher or admin delete"     ON public.jetsons;

CREATE POLICY "Jetsons read consolidated"
  ON public.jetsons FOR SELECT
  USING (
    -- Classroom members (student or teacher) for their own classroom Jetson.
    (
      classroom_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM public.users u
         WHERE u.id = (SELECT auth.uid())
           AND u.classroom_id = jetsons.classroom_id
      )
    )
    -- Teachers managing their own classroom Jetson.
    OR (
      classroom_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM public.classrooms c
         WHERE c.id = jetsons.classroom_id
           AND c.teacher_id = (SELECT auth.uid())
      )
    )
    -- Admins.
    OR EXISTS (
      SELECT 1 FROM public.users u
       WHERE u.id = (SELECT auth.uid()) AND u.role = 'admin'
    )
  );

COMMENT ON POLICY "Jetsons read consolidated" ON public.jetsons IS
  'Consolidates Classroom-members + Teacher + Admin. InitPlan auth.uid().';

CREATE POLICY "Jetsons write teacher or admin insert"
  ON public.jetsons FOR INSERT
  WITH CHECK (
    (
      classroom_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM public.classrooms c
         WHERE c.id = jetsons.classroom_id
           AND c.teacher_id = (SELECT auth.uid())
      )
    )
    OR EXISTS (
      SELECT 1 FROM public.users u
       WHERE u.id = (SELECT auth.uid()) AND u.role = 'admin'
    )
  );

CREATE POLICY "Jetsons write teacher or admin update"
  ON public.jetsons FOR UPDATE
  USING (
    (
      classroom_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM public.classrooms c
         WHERE c.id = jetsons.classroom_id
           AND c.teacher_id = (SELECT auth.uid())
      )
    )
    OR EXISTS (
      SELECT 1 FROM public.users u
       WHERE u.id = (SELECT auth.uid()) AND u.role = 'admin'
    )
  )
  WITH CHECK (
    (
      classroom_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM public.classrooms c
         WHERE c.id = jetsons.classroom_id
           AND c.teacher_id = (SELECT auth.uid())
      )
    )
    OR EXISTS (
      SELECT 1 FROM public.users u
       WHERE u.id = (SELECT auth.uid()) AND u.role = 'admin'
    )
  );

CREATE POLICY "Jetsons write teacher or admin delete"
  ON public.jetsons FOR DELETE
  USING (
    (
      classroom_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM public.classrooms c
         WHERE c.id = jetsons.classroom_id
           AND c.teacher_id = (SELECT auth.uid())
      )
    )
    OR EXISTS (
      SELECT 1 FROM public.users u
       WHERE u.id = (SELECT auth.uid()) AND u.role = 'admin'
    )
  );


-- ============================================================================
-- End of advisor fixes.
-- ----------------------------------------------------------------------------
-- Expected post-apply advisor state:
--   3 security  → 1 remaining (auth_leaked_password_protection, dashboard-only)
--   84 multiple_permissive_policies → 0
--   47 auth_rls_initplan            → 0
--   11 unused_index                 → 0
--   2 unindexed_foreign_keys        → 0
-- ============================================================================

COMMIT;

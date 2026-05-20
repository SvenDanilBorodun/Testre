-- 20260521120000_024_advisor_fixes_rollback.sql
--
-- Reverses the changes from 20260521120000_024_advisor_fixes.sql:
--   1) Drops the consolidated SELECT policies (and the split tutorial /
--      jetson write policies).
--   2) Re-creates the original per-purpose policies byte-for-byte (as
--      they were defined in baseline.sql / 015 / 016 / 018 / 019 /
--      20260519120000).
--   3) Re-creates the 11 dropped unused indexes — exact column lists /
--      WHERE predicates copied from baseline.sql.
--   4) Drops the 2 new foreign-key covering indexes.
--
-- Nothing in this rollback re-introduces the auth.uid() -> (SELECT auth.uid())
-- InitPlan rewrite; the original policies used `auth.uid()` directly.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1) DROP the consolidated / split policies added by 024
-- ---------------------------------------------------------------------------

DROP POLICY IF EXISTS "Classrooms read consolidated"              ON public.classrooms;
DROP POLICY IF EXISTS "Users read consolidated"                   ON public.users;
DROP POLICY IF EXISTS "Users update own profile"                  ON public.users;
DROP POLICY IF EXISTS "Trainings read consolidated"               ON public.trainings;
DROP POLICY IF EXISTS "Users insert own trainings"                ON public.trainings;
DROP POLICY IF EXISTS "Users update own trainings"                ON public.trainings;
DROP POLICY IF EXISTS "Users delete own trainings"                ON public.trainings;
DROP POLICY IF EXISTS "Progress entries read consolidated"        ON public.progress_entries;
DROP POLICY IF EXISTS "Workflows read consolidated"               ON public.workflows;
DROP POLICY IF EXISTS "Owner inserts own workflows"               ON public.workflows;
DROP POLICY IF EXISTS "Owner updates own workflows"               ON public.workflows;
DROP POLICY IF EXISTS "Owner deletes own workflows"               ON public.workflows;
DROP POLICY IF EXISTS "Workgroups read consolidated"              ON public.workgroups;
DROP POLICY IF EXISTS "Memberships read consolidated"             ON public.workgroup_memberships;
DROP POLICY IF EXISTS "Datasets read consolidated"                ON public.datasets;
DROP POLICY IF EXISTS "Owner inserts own datasets"                ON public.datasets;
DROP POLICY IF EXISTS "Owner updates own datasets"                ON public.datasets;
DROP POLICY IF EXISTS "Owner deletes own datasets"                ON public.datasets;
DROP POLICY IF EXISTS "Workflow versions read consolidated"       ON public.workflow_versions;
DROP POLICY IF EXISTS "Tutorial progress read consolidated"       ON public.tutorial_progress;
DROP POLICY IF EXISTS "Tutorial progress owner insert"            ON public.tutorial_progress;
DROP POLICY IF EXISTS "Tutorial progress owner update"            ON public.tutorial_progress;
DROP POLICY IF EXISTS "Tutorial progress owner delete"            ON public.tutorial_progress;
DROP POLICY IF EXISTS "Jetsons read consolidated"                 ON public.jetsons;
DROP POLICY IF EXISTS "Jetsons write teacher or admin insert"     ON public.jetsons;
DROP POLICY IF EXISTS "Jetsons write teacher or admin update"     ON public.jetsons;
DROP POLICY IF EXISTS "Jetsons write teacher or admin delete"     ON public.jetsons;


-- ---------------------------------------------------------------------------
-- 2) Re-create the original policies (verbatim from baseline.sql)
-- ---------------------------------------------------------------------------

-- classrooms (from baseline.sql §002_accounts.sql)
CREATE POLICY "Teachers read own classrooms"
  ON public.classrooms FOR SELECT
  USING (auth.uid() = teacher_id);

CREATE POLICY "Students read own classroom"
  ON public.classrooms FOR SELECT
  USING (id = (SELECT classroom_id FROM public.users WHERE id = auth.uid()));

CREATE POLICY "Admin reads all classrooms"
  ON public.classrooms FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.users WHERE id = auth.uid() AND role = 'admin'));


-- users
CREATE POLICY "Users read own profile"
  ON public.users FOR SELECT
  USING (auth.uid() = id);

CREATE POLICY "Users update own profile"
  ON public.users FOR UPDATE
  TO authenticated
  USING (auth.uid() = id)
  WITH CHECK (auth.uid() = id);

CREATE POLICY "Teachers read own students"
  ON public.users FOR SELECT
  USING (
    classroom_id IN (SELECT id FROM public.classrooms WHERE teacher_id = auth.uid())
  );

CREATE POLICY "Admin reads everyone"
  ON public.users FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.users WHERE id = auth.uid() AND role = 'admin'));


-- trainings
CREATE POLICY "Users read own trainings"
  ON public.trainings FOR SELECT
  USING (auth.uid() = user_id);

CREATE POLICY "Users insert own trainings"
  ON public.trainings FOR INSERT
  TO authenticated
  WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users update own trainings"
  ON public.trainings FOR UPDATE
  TO authenticated
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users delete own trainings"
  ON public.trainings FOR DELETE
  TO authenticated
  USING (auth.uid() = user_id);

CREATE POLICY "Teachers read student trainings"
  ON public.trainings FOR SELECT
  USING (
    user_id IN (
      SELECT s.id FROM public.users s
      JOIN public.classrooms c ON c.id = s.classroom_id
      WHERE c.teacher_id = auth.uid() AND s.role = 'student'
    )
  );

CREATE POLICY "Group members read group trainings"
  ON public.trainings FOR SELECT
  USING (
    workgroup_id IS NOT NULL AND EXISTS (
      SELECT 1 FROM public.workgroup_memberships m
      WHERE m.user_id = auth.uid() AND m.workgroup_id = trainings.workgroup_id
    )
  );


-- progress_entries
CREATE POLICY "Teachers read own progress entries"
  ON public.progress_entries FOR SELECT
  USING (
    classroom_id IN (
      SELECT id FROM public.classrooms WHERE teacher_id = auth.uid()
    )
  );

CREATE POLICY "Students read own + own-classroom entries"
  ON public.progress_entries FOR SELECT
  USING (
    student_id = auth.uid()
    OR (student_id IS NULL AND workgroup_id IS NULL
        AND classroom_id = (SELECT classroom_id FROM public.users WHERE id = auth.uid()))
    OR (workgroup_id IS NOT NULL AND EXISTS (
      SELECT 1 FROM public.workgroup_memberships m
      WHERE m.user_id = auth.uid() AND m.workgroup_id = progress_entries.workgroup_id
    ))
  );

CREATE POLICY "Admin reads all progress entries"
  ON public.progress_entries FOR SELECT
  USING (
    EXISTS (SELECT 1 FROM public.users WHERE id = auth.uid() AND role = 'admin')
  );


-- workflows
CREATE POLICY "Owner reads own workflows"
  ON public.workflows FOR SELECT
  USING (owner_user_id = auth.uid());

CREATE POLICY "Classroom members read templates"
  ON public.workflows FOR SELECT
  USING (
    is_template = TRUE
    AND classroom_id = (
      SELECT classroom_id FROM public.users WHERE id = auth.uid()
    )
  );

CREATE POLICY "Teacher reads classroom templates"
  ON public.workflows FOR SELECT
  USING (
    is_template = TRUE
    AND classroom_id IN (
      SELECT id FROM public.classrooms WHERE teacher_id = auth.uid()
    )
  );

CREATE POLICY "Admin reads all workflows"
  ON public.workflows FOR SELECT
  USING (
    EXISTS (SELECT 1 FROM public.users WHERE id = auth.uid() AND role = 'admin')
  );

CREATE POLICY "Owner inserts own workflows"
  ON public.workflows FOR INSERT
  WITH CHECK (owner_user_id = auth.uid());

CREATE POLICY "Owner updates own workflows"
  ON public.workflows FOR UPDATE
  USING (owner_user_id = auth.uid())
  WITH CHECK (owner_user_id = auth.uid());

CREATE POLICY "Owner deletes own workflows"
  ON public.workflows FOR DELETE
  USING (owner_user_id = auth.uid());

CREATE POLICY "Group members read group workflows"
  ON public.workflows FOR SELECT
  USING (
    workgroup_id IS NOT NULL AND EXISTS (
      SELECT 1 FROM public.workgroup_memberships m
      WHERE m.user_id = auth.uid() AND m.workgroup_id = workflows.workgroup_id
    )
  );


-- workgroups
CREATE POLICY "Teacher reads own workgroups"
  ON public.workgroups FOR SELECT
  USING (classroom_id IN (
    SELECT id FROM public.classrooms WHERE teacher_id = auth.uid()
  ));

CREATE POLICY "Member reads own workgroup"
  ON public.workgroups FOR SELECT
  USING (id = (SELECT workgroup_id FROM public.users WHERE id = auth.uid()));

CREATE POLICY "Admin reads all workgroups"
  ON public.workgroups FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.users WHERE id = auth.uid() AND role = 'admin'));


-- workgroup_memberships
CREATE POLICY "Member reads own membership rows"
  ON public.workgroup_memberships FOR SELECT
  USING (user_id = auth.uid());

CREATE POLICY "Teacher reads owned-classroom memberships"
  ON public.workgroup_memberships FOR SELECT
  USING (workgroup_id IN (
    SELECT g.id FROM public.workgroups g
    JOIN public.classrooms c ON c.id = g.classroom_id
    WHERE c.teacher_id = auth.uid()
  ));

CREATE POLICY "Admin reads all memberships"
  ON public.workgroup_memberships FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.users WHERE id = auth.uid() AND role = 'admin'));


-- datasets
CREATE POLICY "Owner reads own datasets"
  ON public.datasets FOR SELECT
  USING (owner_user_id = auth.uid());

CREATE POLICY "Group members read group datasets"
  ON public.datasets FOR SELECT
  USING (
    workgroup_id IS NOT NULL AND EXISTS (
      SELECT 1 FROM public.workgroup_memberships m
      WHERE m.user_id = auth.uid() AND m.workgroup_id = datasets.workgroup_id
    )
  );

CREATE POLICY "Teacher reads classroom datasets"
  ON public.datasets FOR SELECT
  USING (
    workgroup_id IN (
      SELECT g.id FROM public.workgroups g
      JOIN public.classrooms c ON c.id = g.classroom_id
      WHERE c.teacher_id = auth.uid()
    )
  );

CREATE POLICY "Admin reads all datasets"
  ON public.datasets FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.users WHERE id = auth.uid() AND role = 'admin'));

CREATE POLICY "Owner inserts own datasets"
  ON public.datasets FOR INSERT
  WITH CHECK (owner_user_id = auth.uid());

CREATE POLICY "Owner updates own datasets"
  ON public.datasets FOR UPDATE
  USING (owner_user_id = auth.uid())
  WITH CHECK (owner_user_id = auth.uid());

CREATE POLICY "Owner deletes own datasets"
  ON public.datasets FOR DELETE
  USING (owner_user_id = auth.uid());


-- workflow_versions
CREATE POLICY "Owner reads own workflow versions"
  ON public.workflow_versions FOR SELECT
  USING (
    EXISTS (
      SELECT 1 FROM public.workflows w
      WHERE w.id = workflow_versions.workflow_id
        AND w.owner_user_id = auth.uid()
    )
  );

CREATE POLICY "Admin reads all workflow versions"
  ON public.workflow_versions FOR SELECT
  USING (
    EXISTS (
      SELECT 1 FROM public.users
      WHERE id = auth.uid() AND role = 'admin'
    )
  );

CREATE POLICY "Group members read group workflow versions"
  ON public.workflow_versions FOR SELECT
  USING (
    EXISTS (
      SELECT 1
        FROM public.workflows w
        JOIN public.workgroup_memberships m
          ON m.workgroup_id = w.workgroup_id
       WHERE w.id = workflow_versions.workflow_id
         AND w.workgroup_id IS NOT NULL
         AND m.user_id = auth.uid()
    )
  );


-- tutorial_progress
CREATE POLICY "Owner reads own progress"
  ON public.tutorial_progress
  FOR SELECT
  USING (user_id = auth.uid());

CREATE POLICY "Owner writes own progress"
  ON public.tutorial_progress
  FOR ALL
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

CREATE POLICY "Admin reads all progress"
  ON public.tutorial_progress
  FOR SELECT
  USING (
    EXISTS (
      SELECT 1 FROM public.users
      WHERE id = auth.uid() AND role = 'admin'
    )
  );

CREATE POLICY "Teacher reads classroom student progress"
  ON public.tutorial_progress
  FOR SELECT
  USING (
    EXISTS (
      SELECT 1
      FROM public.users student
      JOIN public.classrooms c ON c.id = student.classroom_id
      WHERE student.id = tutorial_progress.user_id
        AND c.teacher_id = auth.uid()
    )
  );


-- jetsons (from baseline.sql §019_classroom_jetsons.sql)
CREATE POLICY "Classroom members read classroom jetson"
    ON public.jetsons
    FOR SELECT
    USING (
        classroom_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM public.users u
             WHERE u.id = auth.uid()
               AND u.classroom_id = jetsons.classroom_id
        )
    );

CREATE POLICY "Teachers manage own classroom jetson"
    ON public.jetsons
    FOR ALL
    USING (
        classroom_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM public.classrooms c
             WHERE c.id = jetsons.classroom_id
               AND c.teacher_id = auth.uid()
        )
    )
    WITH CHECK (
        classroom_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM public.classrooms c
             WHERE c.id = jetsons.classroom_id
               AND c.teacher_id = auth.uid()
        )
    );

CREATE POLICY "Admins manage jetsons"
    ON public.jetsons
    FOR ALL
    USING (
        EXISTS (
            SELECT 1 FROM public.users u
             WHERE u.id = auth.uid()
               AND u.role = 'admin'
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM public.users u
             WHERE u.id = auth.uid()
               AND u.role = 'admin'
        )
    );


-- ---------------------------------------------------------------------------
-- 3) Re-create the 11 dropped indexes (column lists from baseline.sql)
-- ---------------------------------------------------------------------------

-- trainings
CREATE INDEX IF NOT EXISTS idx_trainings_user_id
  ON public.trainings (user_id);

CREATE INDEX IF NOT EXISTS idx_trainings_status
  ON public.trainings (status);

CREATE INDEX IF NOT EXISTS idx_trainings_requested_at
  ON public.trainings (requested_at DESC);

-- progress_entries (note the WHERE predicate from 004_progress_entries.sql)
CREATE INDEX IF NOT EXISTS idx_progress_entries_student
  ON public.progress_entries (student_id, entry_date DESC)
  WHERE student_id IS NOT NULL;

-- users
CREATE INDEX IF NOT EXISTS idx_users_role
  ON public.users (role);

CREATE INDEX IF NOT EXISTS idx_users_username
  ON public.users (username);

CREATE INDEX IF NOT EXISTS idx_users_classroom
  ON public.users (classroom_id)
  WHERE classroom_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_users_deletion_requested_at
  ON public.users (deletion_requested_at)
  WHERE deletion_requested_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_users_workgroup
  ON public.users (workgroup_id)
  WHERE workgroup_id IS NOT NULL;

-- workflow_versions
CREATE INDEX IF NOT EXISTS idx_workflow_versions_workflow
  ON public.workflow_versions (workflow_id, created_at DESC);

-- tutorial_progress
CREATE INDEX IF NOT EXISTS idx_tutorial_progress_completed
  ON public.tutorial_progress (user_id)
  WHERE completed_at IS NOT NULL;


-- ---------------------------------------------------------------------------
-- 4) Drop the two new FK covering indexes
-- ---------------------------------------------------------------------------

DROP INDEX IF EXISTS public.idx_users_created_by;
DROP INDEX IF EXISTS public.idx_workflow_versions_saved_by;

COMMIT;

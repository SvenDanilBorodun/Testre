-- Rollback for 051 — narrow workflow_trajectories.robot_profile back to 039's
-- three families.
--
-- NOT SAFE TO RUN BLIND. Any row already tagged 'v1_studio' violates the
-- narrowed CHECK, so this script REFUSES rather than silently failing halfway:
-- decide what those recordings should become (delete them, or NULL them and
-- accept that they will then read as omx_f, which is WRONG for a V1 recording
-- even though the widths match) before running it.
--
-- ONE `DO` BLOCK, like 039's rollback and for the same reason: a bare
-- `psql -f` without ON_ERROR_STOP prints a RAISE and carries on, so a
-- multi-statement rollback could drop the constraint and leave a half-applied
-- table. A `DO` block is one statement in its own implicit transaction — the
-- RAISE rolls back everything it already executed — and the CHECK is added
-- VALIDATED in that one step.
--
-- ORDER: roll the cloud API back FIRST (an API that still accepts 'v1_studio'
-- would write rows this narrowed CHECK refuses).

DO $$
DECLARE
    offending bigint;
BEGIN
    SELECT count(*) INTO offending
    FROM public.workflow_trajectories
    WHERE robot_profile = 'v1_studio';

    IF offending > 0 THEN
        RAISE EXCEPTION
            'Rollback refused: % workflow_trajectories rows are tagged v1_studio. Delete or re-tag them first.',
            offending;
    END IF;

    EXECUTE 'ALTER TABLE public.workflow_trajectories '
            'DROP CONSTRAINT IF EXISTS workflow_trajectories_robot_profile_known';

    EXECUTE 'ALTER TABLE public.workflow_trajectories '
            'ADD CONSTRAINT workflow_trajectories_robot_profile_known '
            'CHECK (robot_profile IS NULL '
            '       OR robot_profile IN (''omx_f'', ''edu6_studio'', ''edu1_studio''))';

    EXECUTE 'COMMENT ON COLUMN public.workflow_trajectories.robot_profile IS '
            '''Arm family the recording was made on (omx_f | edu6_studio | edu1_studio); '
            'NULL = legacy row, treated as omx_f. Point width is arm_joints + 2 — 7 for omx_f AND edu1_studio '
            '(5 arm joints each), 8 for edu6_studio. Width therefore does NOT identify the arm; this column does.''';
END
$$;

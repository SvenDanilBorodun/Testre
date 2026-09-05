-- Rollback for 039 — narrow workflow_trajectories.robot_profile back to the
-- two pre-Edu:1 families.
--
-- NOT SAFE TO RUN BLIND. Any row already tagged 'edu1_studio' violates the
-- narrowed CHECK, so this script REFUSES rather than silently failing halfway:
-- decide what those recordings should become (delete them, or NULL them and
-- accept that they will then read as omx_f, which is WRONG for a 5-DOF Edu:1
-- recording even though the widths match) before running it.
--
-- WHY IT IS ONE `DO` BLOCK AND NOT FOUR STATEMENTS. This is the first rollback
-- whose refusal GATE depends on abort semantics, and CLAUDE.md sanctions a bare
-- `psql -f rollback/NNN_*.sql` as an emergency path. Without `ON_ERROR_STOP=1`
-- psql PRINTS the RAISE and CARRIES ON: the four-statement form then dropped the
-- constraint, re-added the narrowed one NOT VALID, failed the VALIDATE, and left
-- the table carrying an UNVALIDATED narrowed constraint that rejects every new
-- edu1 write — the exact half-applied state the refusal exists to prevent.
--
-- A `DO` block is ONE statement and runs in its own implicit transaction, so the
-- RAISE rolls back everything it has already executed. That is stronger than a
-- leading `\set ON_ERROR_STOP on`, which is a psql META-command: it would fix
-- psql and break every other client (a GUI console, `supabase db push`, any
-- driver). The CI path (`supabase-migrate.yml`, `psql -v ON_ERROR_STOP=1`) was
-- always safe and is unaffected.
--
-- Consequence of the single transaction: the CHECK is added VALIDATED in one
-- step rather than as NOT VALID + VALIDATE. The two-step dance exists to keep
-- the ACCESS EXCLUSIVE lock short, and inside one transaction both locks are
-- held to the end anyway, so it buys nothing here. 035/036/038 keep the
-- four-statement shape deliberately — none of them has a refusal gate, so none
-- of them can half-apply this way.

DO $$
DECLARE
    offending bigint;
BEGIN
    SELECT count(*) INTO offending
    FROM public.workflow_trajectories
    WHERE robot_profile = 'edu1_studio';

    IF offending > 0 THEN
        RAISE EXCEPTION
            'Rollback refused: % workflow_trajectories rows are tagged edu1_studio. Delete or re-tag them first.',
            offending;
    END IF;

    EXECUTE 'ALTER TABLE public.workflow_trajectories '
            'DROP CONSTRAINT IF EXISTS workflow_trajectories_robot_profile_known';

    EXECUTE 'ALTER TABLE public.workflow_trajectories '
            'ADD CONSTRAINT workflow_trajectories_robot_profile_known '
            'CHECK (robot_profile IS NULL '
            '       OR robot_profile IN (''omx_f'', ''edu6_studio''))';

    EXECUTE 'COMMENT ON COLUMN public.workflow_trajectories.robot_profile IS '
            '''Arm family the recording was made on (omx_f | edu6_studio); '
            'NULL = legacy row, treated as omx_f. Point width is arm_joints + 2 (7 | 8).''';
END
$$;

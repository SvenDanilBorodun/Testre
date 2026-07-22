-- Rollback for 035 — drops the trajectory arm-family tag.
-- Safe: the column is additive; the pre-035 API ignores it entirely.
ALTER TABLE public.workflow_trajectories
    DROP CONSTRAINT IF EXISTS workflow_trajectories_robot_profile_known;
ALTER TABLE public.workflow_trajectories
    DROP COLUMN IF EXISTS robot_profile;

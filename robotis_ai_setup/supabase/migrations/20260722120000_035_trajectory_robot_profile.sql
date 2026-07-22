-- 035 — workflow_trajectories.robot_profile (edu6_studio, plan §14).
--
-- Tags each recorded Contract-B trajectory with the ARM FAMILY it was made on
-- ('omx_f' = 5 arm joints, 7-wide points; 'edu6_studio' = 6, 8-wide), so a
-- 7-wide OMX recording can never be silently handed to an 8-wide edu6 runtime
-- (or vice versa) — length alone cannot distinguish a 5-DOF arm with extra
-- channels from a 6-DOF arm. NULL = untagged legacy/pre-edu6 rows, which the
-- API + runtime treat as 'omx_f' (the only family that existed before this).
--
-- Purely additive: no backfill (NULL is the documented legacy value), no RLS
-- change (the existing owner policies cover the column), no realtime change.
-- The Cloud-API boot schema probe gains ("workflow_trajectories",
-- "robot_profile") in the same release (golden order W1 → W2).

ALTER TABLE public.workflow_trajectories
    ADD COLUMN IF NOT EXISTS robot_profile text;

COMMENT ON COLUMN public.workflow_trajectories.robot_profile IS
    'Arm family the recording was made on (omx_f | edu6_studio); NULL = legacy row, treated as omx_f. Point width is arm_joints + 2 (7 | 8).';

-- Cheap sanity: only known ids or NULL. A CHECK (not an enum) so a future
-- profile is one migration, not a type migration.
ALTER TABLE public.workflow_trajectories
    ADD CONSTRAINT workflow_trajectories_robot_profile_known
    CHECK (robot_profile IS NULL OR robot_profile IN ('omx_f', 'edu6_studio'))
    NOT VALID;
ALTER TABLE public.workflow_trajectories
    VALIDATE CONSTRAINT workflow_trajectories_robot_profile_known;

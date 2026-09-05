-- 039 — allow 'edu1_studio' in workflow_trajectories.robot_profile.
--
-- NUMBERING: 037 is still deliberately skipped (see 038's header) — it is
-- claimed by the „Eigene Objekte" pair in docs/plans/eigene-objekte-sql/.
--
-- WHAT CHANGES. Migration 035 introduced the arm-family tag with a CHECK
-- pinned to the two families that existed then ('omx_f', 'edu6_studio'). The
-- Edu:1 is a third, so a „Bewegung" recorded on it would be REFUSED by the
-- constraint and the student would see a 500 on save.
--
-- WHY THE TAG MATTERS MORE NOW, not less. 035's own header argued the tag was
-- needed because "length alone cannot distinguish a 5-DOF arm with extra
-- channels from a 6-DOF arm". The Edu:1 makes that concrete: it has FIVE arm
-- joints, so its Contract-B point width is 5 + 2 = 7 — the SAME width omx_f
-- uses. Two genuinely different arms now share a width, and this column is the
-- only thing that tells their recordings apart. Replaying an OMX recording on
-- an Edu:1 would pass every width check and drive a completely different arm.
--
-- Purely additive: no backfill (NULL stays the documented legacy value, read
-- as omx_f), no RLS change (the existing owner policies cover the column), no
-- realtime change. Nothing in the Cloud-API boot schema probe moves — the
-- column already exists; only its allowed VALUES widen.

ALTER TABLE public.workflow_trajectories
    DROP CONSTRAINT IF EXISTS workflow_trajectories_robot_profile_known;

ALTER TABLE public.workflow_trajectories
    ADD CONSTRAINT workflow_trajectories_robot_profile_known
    CHECK (robot_profile IS NULL
           OR robot_profile IN ('omx_f', 'edu6_studio', 'edu1_studio'))
    NOT VALID;
ALTER TABLE public.workflow_trajectories
    VALIDATE CONSTRAINT workflow_trajectories_robot_profile_known;

COMMENT ON COLUMN public.workflow_trajectories.robot_profile IS
    'Arm family the recording was made on (omx_f | edu6_studio | edu1_studio); NULL = legacy row, treated as omx_f. Point width is arm_joints + 2 — 7 for omx_f AND edu1_studio (5 arm joints each), 8 for edu6_studio. Width therefore does NOT identify the arm; this column does.';

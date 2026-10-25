-- 051 — allow 'v1_studio' in workflow_trajectories.robot_profile.
--
-- WHAT CHANGES. Migrations 035 and 039 pinned the arm-family tag's CHECK to the
-- families that existed then. The V1 replaces both earlier Feetech arms; a „Bewegung"
-- recorded on it carries the tag 'v1_studio', which the old constraint would
-- REFUSE (a 500 on save).
--
-- WHY THE TAG MATTERS. The V1 has FIVE arm joints, so its Contract-B point
-- width is 5 + 2 = 7 — the SAME width 'omx_f' uses. Width does not identify the
-- arm; this column does. Replaying an OMX recording on a V1 would pass every
-- width check and drive a different arm.
--
-- THE RETIRED IDS STAY ADMITTED ('edu6_studio', 'edu1_studio'): rows already
-- stored under them must stay readable, and narrowing the CHECK would fail its
-- VALIDATE on any such row. The cloud API refuses those ids on a new upload
-- (validators/workflow.py::LEGACY_TRAJECTORY_ROBOT_PROFILES); a workflow clone
-- copies the source's stored rows verbatim, tag included
-- (routes/workflows.py::clone_workflow), so a new row carries a retired id only
-- as a copy of a row the database already holds.
--
-- Purely additive: no backfill (NULL stays the documented legacy value, read as
-- omx_f), no RLS change (the existing owner policies cover the column), no
-- realtime change. Nothing in the Cloud-API boot schema probe moves — the column
-- already exists; only its allowed VALUES widen.

ALTER TABLE public.workflow_trajectories
    DROP CONSTRAINT IF EXISTS workflow_trajectories_robot_profile_known;

ALTER TABLE public.workflow_trajectories
    ADD CONSTRAINT workflow_trajectories_robot_profile_known
    CHECK (robot_profile IS NULL
           OR robot_profile IN ('omx_f', 'v1_studio', 'edu6_studio', 'edu1_studio'))
    NOT VALID;
ALTER TABLE public.workflow_trajectories
    VALIDATE CONSTRAINT workflow_trajectories_robot_profile_known;

COMMENT ON COLUMN public.workflow_trajectories.robot_profile IS
    'Arm family the recording was made on (omx_f | v1_studio; edu6_studio and edu1_studio are retired ids kept readable on stored rows, refused on new uploads by the cloud API and copied verbatim by a workflow clone); NULL = legacy row, treated as omx_f. Point width is arm_joints + 2 — 7 for omx_f AND v1_studio (5 arm joints each). Width therefore does NOT identify the arm; this column does.';

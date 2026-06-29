-- 033_workflow_sim_scene.sql
--
-- Roboter Studio Phase-3 (server-side interactive Simulation World). The
-- sim view lets a student place catalog objects on a virtual table and run
-- the REAL workflow interpreter against a virtual arm — so the placed scene
-- must persist with the workflow that was authored against it.
--
-- Stored in its OWN jsonb column (NOT embedded in blockly_json) so a sim
-- placement does NOT pollute the blockly version history: the
-- snapshot_workflow_version trigger fires only on a blockly_json change
-- (baseline.sql), so a sim_scene-only write never snapshots (accepted).
--
-- Shape (Phase-3 contract, see docs/plans/2026-06-29-phase3-simulation-world.md):
--   { "version": 1,
--     "objects": [ { "type": "wuerfel", "tag_id": 7,
--                    "x": 0.20, "y": 0.0, "yaw": 0.3 } ] }
-- A brand-new / never-simulated workflow carries the empty object '{}'.
--
-- Authorization (Rule §4 — service-role bypasses RLS, the Python asserts in
-- routes/workflows.py are the real layer): the column inherits the existing
-- `workflows` row's RLS + every write is owner-scoped
-- (`.eq("owner_user_id", user.id)`) or preceded by `_assert_workflow_owned`.
-- ZERO new policies, channels, or endpoints → zero new IDOR surface.
--
-- NOT NULL DEFAULT '{}' so the WorkflowResponse read round-trip always sees a
-- dict (existing rows are back-filled to '{}' by the DEFAULT). Idempotent —
-- re-runnable.

BEGIN;

ALTER TABLE public.workflows
  ADD COLUMN IF NOT EXISTS sim_scene JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN public.workflows.sim_scene IS
  'Roboter Studio Phase-3 Sim-Szene: placed virtual objects for this workflow.';

COMMIT;

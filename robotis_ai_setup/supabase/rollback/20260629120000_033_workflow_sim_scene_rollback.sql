-- Rollback for 033_workflow_sim_scene: drop the sim_scene column.
--
-- Reverting discards every persisted Roboter Studio Sim-Szene (placed virtual
-- objects). No data loss beyond the sim placements themselves — blockly_json,
-- version history, and HF repos are untouched, and nothing FK-references this
-- column. The cloud API tolerates the absent column on the read path
-- (WorkflowResponse.sim_scene defaults to None), but the W2 schema probe
-- (main.py required_columns) will refuse to boot until the column is restored
-- or the matching code is rolled back too. Take a PITR snapshot first.

ALTER TABLE public.workflows
  DROP COLUMN IF EXISTS sim_scene;

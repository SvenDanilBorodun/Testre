-- Rollback for 034_workflow_trajectories: drop the trajectory child table.
--
-- Reverting discards every persisted Roboter Studio replay trajectory
-- (recorded hand-guided motions). No data loss beyond the recordings
-- themselves — blockly_json, sim_scene, version history, and HF repos are
-- untouched, and nothing FK-references this table. The cloud API's read path
-- tolerates the absent table only if the matching code is rolled back too;
-- the W2 schema probe (main.py required_tables) will otherwise refuse to boot
-- until the table is restored. Take a PITR snapshot first.
--
-- Drops in dependency order: trigger -> function -> table (DROP TABLE CASCADE
-- also removes the table's triggers/indexes/policies, but the prune function
-- is a standalone object and must be dropped explicitly).

BEGIN;

DROP TRIGGER IF EXISTS trg_workflow_trajectories_prune ON public.workflow_trajectories;
DROP TRIGGER IF EXISTS trg_workflow_trajectories_touch ON public.workflow_trajectories;

DROP TABLE IF EXISTS public.workflow_trajectories;

DROP FUNCTION IF EXISTS public.prune_workflow_trajectories();

COMMIT;

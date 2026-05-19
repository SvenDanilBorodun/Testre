-- 005: Rename runpod_job_id -> cloud_job_id after the RunPod -> Modal migration.
--
-- The column stores an opaque dispatcher job id (now a Modal FunctionCall
-- object_id). Renaming makes the schema vendor-neutral so any future dispatcher
-- swap is a code change, not a schema change.
--
-- Rollback (if needed):
--     ALTER TABLE public.trainings RENAME COLUMN cloud_job_id TO runpod_job_id;

ALTER TABLE public.trainings RENAME COLUMN runpod_job_id TO cloud_job_id;

COMMENT ON COLUMN public.trainings.cloud_job_id IS
  'Opaque job id returned by the cloud GPU dispatcher. '
  'Currently a Modal FunctionCall.object_id.';

COMMENT ON COLUMN public.trainings.worker_token IS
  'Per-training secret. Only the API and the assigned cloud worker know it. '
  'Used by update_training_progress() to scope worker DB access to this row.';

COMMENT ON COLUMN public.trainings.last_progress_at IS
  'Liveness marker, bumped on every update_training_progress() call. '
  'The reconciler uses it to spot wedged workers (dispatcher still says '
  'IN_PROGRESS but no progress for >N minutes).';

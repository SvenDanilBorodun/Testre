-- Rollback for 005_cloud_job_id.sql
-- Renames cloud_job_id back to runpod_job_id. Safe to run multiple times.

BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'trainings'
          AND column_name  = 'cloud_job_id'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name   = 'trainings'
          AND column_name  = 'runpod_job_id'
    ) THEN
        ALTER TABLE public.trainings RENAME COLUMN cloud_job_id TO runpod_job_id;
    END IF;
END $$;

COMMIT;

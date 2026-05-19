-- 012: Mark datasets registered by the periodic Railway sweep, distinct
-- from datasets registered live by the React app right after upload.
--
-- Problem: when a student uploads a dataset to HF Hub, the React app
-- POSTs /datasets to register it so group siblings can see it. If the
-- WSL distro has no internet at exactly that moment (or the Cloud API
-- is briefly down), the upload succeeds on HF but registration never
-- runs and siblings can never see the dataset.
--
-- Fix lives in cloud_training_api/app/services/dataset_sweep.py: a
-- background task that lists HF datasets for known authors every
-- 10 min and inserts any missing rows. This column lets us tell at a
-- glance which rows came in via that safety net (helpful for debugging
-- "why didn't my dataset show up live?" complaints).
--
-- The column is informational only — the sweep does NOT skip rows
-- whose discovered_via_sweep is FALSE on subsequent ticks; it just
-- inserts ones that are missing.
--
-- Forward-only:

ALTER TABLE public.datasets
  ADD COLUMN IF NOT EXISTS discovered_via_sweep BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN public.datasets.discovered_via_sweep IS
  'TRUE when this row was inserted by the Railway-side dataset sweep '
  'service (services/dataset_sweep.py) rather than by the live React '
  'POST /datasets call after a successful HF upload. Informational only.';

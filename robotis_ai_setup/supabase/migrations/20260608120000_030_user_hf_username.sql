-- Migration 030: link a student's HuggingFace identity to their cloud profile.
--
-- Why: the datasets registry (public.datasets) keys rows to the Supabase user
-- (owner_user_id), but a student's datasets live on HuggingFace under their HF
-- username ("Benutzer-ID"). Nothing persisted that HF identity on the cloud
-- profile, which broke dataset discovery two ways:
--   * dataset_sweep could only *guess* an author by parsing trainings.dataset_name
--     and existing datasets rows (fuzzy + fan-out across every candidate user) —
--     which is how ~200 upstream RobotisSW/Hotteok example datasets got
--     mass-registered across every account and poisoned each student's
--     register_dataset_safe author anchor (real uploads then 403'd), and
--   * a never-trained student had no anchor at all, so the sweep could never
--     discover their datasets.
--
-- This column makes the link explicit and per-student. The React app sets it
-- from the ROS `whoami` Benutzer-ID (auto-link in StudentApp), or the student
-- links it manually via PATCH /me. The rewritten dataset_sweep and the new
-- POST /datasets/sync enumerate HfApi.list_datasets(author=hf_username) for
-- exactly that one student — no fuzzy guessing, no fan-out.
--
-- See: routes/me.py (PATCH /me + GET /me hf_username), routes/datasets.py
-- (POST /datasets/sync), services/dataset_sweep.py (per-user anchor + deny-list).

ALTER TABLE public.users
  ADD COLUMN IF NOT EXISTS hf_username TEXT;

COMMENT ON COLUMN public.users.hf_username IS
  'The student''s HuggingFace username (their dataset namespace). Set by the '
  'React app from the ROS whoami Benutzer-ID, or manually via PATCH /me. The '
  'dataset_sweep and POST /datasets/sync use it as the SOLE author anchor for '
  'discovering that student''s datasets.';

-- Partial index: the sweep enumerates users WHERE hf_username IS NOT NULL.
-- Classroom scale is tiny, but the partial index keeps that scan index-only
-- and is essentially free to maintain.
CREATE INDEX IF NOT EXISTS idx_users_hf_username
  ON public.users (hf_username)
  WHERE hf_username IS NOT NULL;

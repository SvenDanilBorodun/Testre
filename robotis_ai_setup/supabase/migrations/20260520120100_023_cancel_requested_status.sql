-- ============================================================================
-- 20260520120100_023_cancel_requested_status.sql
-- ----------------------------------------------------------------------------
-- Adds the `cancel_requested` transitional training status used by the
-- background cancel-sweep introduced in 2026-05.
--
-- Problem (forensic finding 2026-05)
--   /trainings/cancel attempts a Modal cancel, swallows any exception
--   (logs a warning), and then unconditionally flips the row to
--   `canceled` + refunds the credit. A student looping
--   start→cancel × 10 against flaky Modal can leave up to 10 GPU
--   containers running to their full timeout_hours cap — a cost-bomb.
--
-- Fix
--   When Modal cancel fails, we no longer pretend it succeeded. The row
--   transitions to a NEW intermediate status `cancel_requested`. A
--   background sweep retries the Modal cancel every 30 s for up to 5
--   attempts. On success the sweep flips to `canceled` and the credit
--   frees automatically (get_remaining_credits counts only
--   failed/canceled as freed). After 5 attempts the sweep flips to
--   `failed` so the credit IS released — the GPU has likely already run
--   in any case, and a stuck row is worse than a one-credit loss.
--
-- Terminal-state guard interaction (migration 010)
--   `cancel_requested` is NOT terminal. The terminal set stays
--   {succeeded, failed, canceled}. update_training_progress() still
--   refuses writes once a row hits any of those three. The sweep is
--   the ONLY caller that transitions cancel_requested → canceled/failed,
--   and it does so via service-role straight-line UPDATE, not via
--   update_training_progress(). The 010 guard is therefore unchanged.
--
-- Credit accounting
--   get_remaining_credits / start_training_safe / adjust_workgroup_credits
--   already filter on `status NOT IN ('failed', 'canceled')`. A
--   cancel_requested row stays "used" — that is what we want: the
--   student should not be able to start another training while their
--   GPU may still be running. No RPC bodies change.
--
-- Idempotent — re-runnable.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Expand the trainings.status CHECK constraint.
--    Migration 0 used a plain CHECK, not a Postgres enum, so we just
--    drop + recreate the constraint with the new value appended.
--    Idempotent: if the constraint name doesn't exist (different
--    Postgres version's auto-naming), the DROP is a no-op.
-- ---------------------------------------------------------------------------
ALTER TABLE public.trainings
  DROP CONSTRAINT IF EXISTS trainings_status_check;

ALTER TABLE public.trainings
  ADD CONSTRAINT trainings_status_check
  CHECK (status IN ('queued', 'running', 'cancel_requested', 'succeeded', 'failed', 'canceled'));


-- ---------------------------------------------------------------------------
-- 2. Track cancel attempt count + last attempt time.
--    cancel_attempts: incremented on every /trainings/cancel call AND
--    every background sweep retry. Once it hits 5 the sweep marks the
--    row failed instead of retrying further.
--    cancel_attempted_at: liveness signal for the sweep (e.g. so a
--    `cancel_requested` row that has not been retried in >5 min can be
--    flagged in logs).
-- ---------------------------------------------------------------------------
ALTER TABLE public.trainings
  ADD COLUMN IF NOT EXISTS cancel_attempted_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS cancel_attempts     INT NOT NULL DEFAULT 0;

COMMENT ON COLUMN public.trainings.cancel_attempted_at IS
  'Wall-clock of the most recent Modal cancel attempt — set by /trainings/cancel and the background cancel-sweep. NULL on rows that were never cancel-requested.';
COMMENT ON COLUMN public.trainings.cancel_attempts IS
  'Number of Modal cancel attempts on this row. Incremented by /trainings/cancel and the background sweep. 5 attempts → sweep gives up and marks the row failed.';


-- ---------------------------------------------------------------------------
-- 3. Partial index for the sweep query.
--    The sweep selects WHERE status='cancel_requested' AND cancel_attempts<5;
--    a partial index keeps the scan O(actively-pending) rather than
--    O(all trainings).
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_trainings_cancel_requested
  ON public.trainings (cancel_attempted_at)
  WHERE status = 'cancel_requested';

COMMIT;

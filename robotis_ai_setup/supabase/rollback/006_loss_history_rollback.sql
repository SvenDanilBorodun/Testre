-- Rollback for 006_loss_history.sql
-- Removes the loss_history column, downsampling logic, and realtime
-- publication entry. The trainings table stays; only 006-added bits drop.

BEGIN;

-- Remove trainings from the realtime publication (noop if not present).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_publication_tables
        WHERE pubname = 'supabase_realtime'
          AND schemaname = 'public'
          AND tablename = 'trainings'
    ) THEN
        ALTER PUBLICATION supabase_realtime DROP TABLE public.trainings;
    END IF;
END $$;

-- Drop the column (also drops any index that references it).
ALTER TABLE public.trainings DROP COLUMN IF EXISTS loss_history;

-- If 006 replaced update_training_progress with a loss-history-aware
-- variant, restore the 005-era signature. The forward migration at
-- migration.sql provides the canonical function; re-running that file
-- restores it cleanly.
-- (intentionally not re-defining the function here to avoid shadowing
-- whatever state is currently live — run migration.sql once after this)

COMMIT;

SELECT '006 rollback complete. Re-apply migration.sql to restore the RPC.' AS note;

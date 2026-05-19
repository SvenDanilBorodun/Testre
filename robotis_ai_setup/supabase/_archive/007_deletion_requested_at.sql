-- 007_deletion_requested_at.sql
-- Add users.deletion_requested_at so /me/delete (GDPR Art. 17) can
-- record a deletion request without log warnings. Actual deletion
-- remains manual per context/20-operations.md §7.
--
-- Applied to production via Supabase MCP apply_migration on 2026-04-24.

ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS deletion_requested_at TIMESTAMPTZ;

-- Partial index on non-null rows only: admins can list pending deletions
-- cheaply without scanning the whole users table.
CREATE INDEX IF NOT EXISTS idx_users_deletion_requested_at
    ON public.users(deletion_requested_at)
    WHERE deletion_requested_at IS NOT NULL;

COMMENT ON COLUMN public.users.deletion_requested_at IS
    'Set by /me/delete when a user requests account removal. NULL for all normal users. Admin processes deletion within 30 days per GDPR Art. 17.';

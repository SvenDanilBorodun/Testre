# Supabase migration rollbacks

One `NNN_<name>_rollback.sql` per forward migration. Run the *latest* one
to undo the last applied change; they are **not** idempotent across
versions. Always take a Supabase PITR snapshot first.

## Order (most recent first)

If you are rolling back multiple migrations, run them **in reverse order
of how they were applied** (highest number first). Going out of order
breaks foreign-key dependencies — e.g. running 002_accounts_rollback
before 004_progress_entries_rollback would fail because progress_entries
has FKs to classrooms (which 002 drops via CASCADE) and the trigger
function `touch_updated_at()` is shared.

1. `007_deletion_requested_at_rollback.sql` — drops the GDPR
   `deletion_requested_at` column + partial index. Pending deletion
   requests are LOST — export them first if that matters.
2. `006_loss_history_rollback.sql` — drops `loss_history`, reverts the
   progress RPC to its 005 signature, removes realtime publication entry.
3. `005_cloud_job_id_rollback.sql` — renames `cloud_job_id` back to
   `runpod_job_id`.
4. `004_progress_entries_rollback.sql` — drops the `progress_entries`
   table + trigger. **Warning: deletes all daily notes.** Must run
   BEFORE 002 because progress_entries.classroom_id references classrooms.
5. `002_accounts_rollback.sql` — drops classroom + role machinery.
   **Warning: deletes every teacher/admin/student profile row.**

## What's NOT in here

- `migration.sql` (the base schema) has no rollback. If you need to
  wipe to zero, use Supabase's reset-database in the dashboard.
- `003_lessons_and_notes.sql` is superseded by 004 — the forward
  migration itself is the "rollback" for 003.

## How to use

```bash
# Preview changes first
psql $DATABASE_URL -f rollback/006_loss_history_rollback.sql --single-transaction
```

Tested on staging before prod. These scripts are idempotent within a
single version — rerunning 006_rollback when 006 is already rolled back
is a no-op.

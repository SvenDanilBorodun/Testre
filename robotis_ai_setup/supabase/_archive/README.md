# Archived migration files (pre-CLI era)

These are the 21 numbered SQL files that produced the EduBotics Supabase
schema (project `fnnbysrjkfugsqzwcksd`) before the CI pipeline took over
migrations on 2026-05-19.

They have been **squashed** into a single baseline at
`../migrations/00000000000000_baseline.sql`. The files here are kept
solely for `git blame` and traceability — they are not applied by the
Supabase CLI and not referenced by any workflow.

## Why we squashed

The live `supabase_migrations.schema_migrations` ledger had 30 applied
entries; this directory only had 21 numbered files. Nine entries came
from an earlier timestamp era (pre-rewrite) and from `apply_migration`
calls via the Supabase MCP that never committed an `.sql` file. The
schema converged because the numbered files re-defined the same
objects, but the CLI could no longer reconcile what was applied.

Squashing the existing 21 files into one baseline that exactly matches
the live schema lets the CLI's `supabase db push` work going forward.

## How the squash maps to the live ledger

The baseline concatenates these files in chronological order:

```
migration.sql                                        (live ts 20260101000000-eq baseline)
002_accounts.sql                                     (live ts 20260415131322)
003_lessons_and_notes.sql                            (OMITTED — superseded by 004)
004_progress_entries.sql                             (live ts 20260418144651)
005_cloud_job_id.sql                                 (live ts 20260422130606)
006_loss_history.sql                                 (live ts 20260422172800)
007_deletion_requested_at.sql                        (live ts 20260424202232)
008_workflows.sql                                    (live ts 20260505074323)
   + live patch: 008_workflows_harden_search_path    (live ts 20260505074431)
009_workflows_rls_writes.sql                         (live ts 20260505102008)
010_progress_terminal_guard.sql                      (live ts 20260506165807)
011_workgroups.sql (parts 1 + 2)                     (live ts 20260509121853 + 122003)
012_dataset_sweep.sql                                (live ts 20260509122022)
013_revoke_anon_from_security_definer.sql            (live ts 20260509163024)
   + live patch: 013b_revoke_get_remaining_credits   (live ts 20260509163113)
015_workflow_versions.sql                            (live ts 20260510200000)
   + live patch: 015b_snapshot_workflow_version_     (live ts 20260511161342)
                 saved_by_and_policy
016_tutorial_progress.sql                            (live ts 20260510200100)
017_vision_quota.sql                                 (live ts 20260510200200)
   + live patch: 017b_consume_vision_quota_f48       (live ts 20260511161022)
018_workflow_versions_author_and_group_rls.sql       (live ts 20260514103447)
019_classroom_jetsons.sql                            (live ts 20260516193944)
020_jetson_v2.sql                                    (live ts 20260517095644)
021_workgroup_memberships_realtime_and_owner_check.sql (live ts 20260517200515)
```

The four `live patch` entries are inlined at the end of
`00000000000000_baseline.sql` so a fresh `supabase db push` against an
empty database produces a schema byte-identical to production.

## One-time operator steps

After the baseline lands on `main`, the operator must run **once** to
tell the CLI the baseline is already applied to production:

```bash
supabase link --project-ref fnnbysrjkfugsqzwcksd
supabase migration repair --status applied 00000000000000
```

From then on, every new migration goes in
`robotis_ai_setup/supabase/migrations/<UTC-timestamp>_<name>.sql` and
the GHA workflow `.github/workflows/supabase-migrate.yml` applies it
on push to `main` (and to PR branches on PR open).

## Forward references

- Rollback files live in `../rollback/` and are still active; the
  `supabase-migrate.yml` workflow's rollback dispatch reads them.
- New migrations must use the Supabase CLI timestamp format
  (`YYYYMMDDHHMMSS_name.sql`), NOT the old `NNN_name.sql` form.

# WORKFLOW: Adding a Supabase Migration

> Strict checklist for adding a new SQL migration. Follow step by step.
> Read [`WORKFLOW.md`](WORKFLOW.md) first for the master rules.

---

## §1 — Decide: do you need a migration?

You need a migration if you:
- Add/remove/rename columns, tables, indexes
- Add/change RPCs (PL/pgSQL functions)
- Add/change RLS policies
- Add/change triggers
- Add to the realtime publication

You do NOT need a migration for:
- Adding rows of data (use `cloud_training_api` or `bootstrap_admin.py`)
- Changing API responses without DB changes

---

## §2 — Read first

- [ ] [`12-supabase.md`](12-supabase.md) — full schema + RPCs + RLS + custom error codes
- [ ] `supabase/rollback/README.md` — rollback ordering rules
- [ ] The most recent migration (e.g., `007_deletion_requested_at.sql`) — match its style

---

## §3 — Number the new migration

Find the highest number in `supabase/`:
```bash
ls supabase/*.sql | sort -V
```

Use the next sequential number, zero-padded to 3 digits: `008_my_feature.sql`.

Do NOT skip numbers. Do NOT use suffixes like `008a`. One forward → one rollback, both numbered.

---

## §4 — Write the forward migration

### Idempotency (MANDATORY)

Every statement must be idempotent. Re-running the migration twice must be a no-op.

- `CREATE TABLE IF NOT EXISTS public.foo (...)`
- `CREATE INDEX IF NOT EXISTS idx_foo_bar ON public.foo (bar)`
- `CREATE OR REPLACE FUNCTION public.bar(...)` (always replaces)
- `ALTER TABLE public.foo ADD COLUMN IF NOT EXISTS new_col ...` (Postgres 9.6+)
- `DROP TRIGGER IF EXISTS trg_foo ON public.foo` before `CREATE TRIGGER trg_foo`
- `DO $$ BEGIN IF NOT EXISTS (...) THEN CREATE TYPE ... END IF; END $$;` for enum creation

If a statement is NOT naturally idempotent (e.g., enum value addition), wrap it in a `DO` block with an existence check.

### RPC requirements

If adding a function:
- [ ] `LANGUAGE plpgsql` (or `sql`)
- [ ] `SECURITY DEFINER` (run as schema owner; bypasses caller's RLS)
- [ ] `SET search_path = public` (defense against search_path injection)
- [ ] `STABLE` or `IMMUTABLE` if the function is read-only (helps query planner)
- [ ] `GRANT EXECUTE ON FUNCTION public.X TO service_role;` (NOT `authenticated` — those go through RLS via direct table access)
- [ ] Use `RAISE EXCEPTION USING ERRCODE = 'P00xx'` for errors. Pick a new code in P0015+ range.
- [ ] Document the new ERRCODE in [`12-supabase.md`](12-supabase.md) §13

### RLS requirements

If adding a table:
- [ ] `ALTER TABLE public.foo ENABLE ROW LEVEL SECURITY;`
- [ ] At minimum, one policy each for student/teacher/admin reads (where applicable)
- [ ] `WITH CHECK` clause on INSERT/UPDATE policies (not just `USING`)
- [ ] Reference [`12-supabase.md`](12-supabase.md) §8 for existing patterns

### Indexes

- [ ] All FK columns indexed (Postgres doesn't auto-create FK indexes)
- [ ] If new query patterns: compound index for `(user_id, status)` etc.
- [ ] Use partial indexes when only a subset is queried (`WHERE foo IS NOT NULL`)

### Comments + headers

- [ ] File header comment: what the migration does, in plain English (1-2 paragraphs)
- [ ] Section dividers: `-- =========================================` for tables / functions / RLS / indexes
- [ ] Comments explaining non-obvious choices (e.g., why a partial UNIQUE index)

---

## §5 — Write the rollback

`supabase/rollback/008_my_feature_rollback.sql`.

Rollback must:
- Undo every change in the forward migration
- Be idempotent
- **Document data loss** in a comment header (e.g., "Drops table foo. ALL rows in foo are lost.")
- Apply in reverse order of forward (drop trigger → drop function → drop table → drop type)

Example structure:
```sql
-- 008_rollback: undo my_feature
-- DATA LOSS: all rows in public.foo are dropped.

-- Triggers first
DROP TRIGGER IF EXISTS trg_foo ON public.foo;
DROP FUNCTION IF EXISTS public.touch_foo();

-- Indexes
DROP INDEX IF EXISTS public.idx_foo_bar;

-- Table last
DROP TABLE IF EXISTS public.foo CASCADE;

-- Type
DROP TYPE IF EXISTS public.foo_type;
```

---

## §6 — Update rollback README

Edit `supabase/rollback/README.md`:
- Add the new migration to the rollback ordering table
- Note any data-loss warnings
- Note any FK ordering constraints (e.g., "must run BEFORE 002 rollback because of classroom FK")

---

## §7 — Test the migration

### On a branch DB

Create a temporary Supabase branch (free tier supports this):
```bash
# Via Supabase dashboard: Settings → Database → Branches → Create
# Or via supabase CLI:
supabase db branch create test-008
```

Apply the forward migration:
```bash
psql $BRANCH_DATABASE_URL -f supabase/008_my_feature.sql
```

Verify:
- Schema looks right: `psql -c "\d public.foo"`
- Indexes present: `psql -c "\di public.foo"`
- RLS enabled: `psql -c "\d+ public.foo" | grep RLS`
- RPC works: `psql -c "SELECT public.my_rpc(...);"`

Apply rollback:
```bash
psql $BRANCH_DATABASE_URL --single-transaction -f supabase/rollback/008_my_feature_rollback.sql
```

Verify everything is gone. Re-apply forward to confirm idempotency.

### Test RLS

For new tables, test with both anon-key (RLS active) and service-role-key (RLS bypassed):

```bash
# Anon key (frontend perspective)
SUPABASE_KEY=$ANON_KEY psql ... -c "SELECT * FROM foo;"
# Should return only rows the user can see per RLS policies

# Service role (backend perspective)
SUPABASE_KEY=$SERVICE_ROLE_KEY psql ... -c "SELECT * FROM foo;"
# Should return everything
```

If RLS is the **only** auth (rare; we use service-role + Python checks), test impersonation: log in as user A, try to read user B's row.

### Test the API

If your migration is consumed by a new/updated API endpoint:
- [ ] Run cloud_training_api locally pointed at the branch DB
- [ ] curl the endpoint with a real JWT
- [ ] Verify both happy path and auth path (wrong user)

---

## §8 — Apply to production

After branch testing succeeds:

- [ ] **Take a PITR snapshot** (Supabase dashboard → Database → Backups → Schedule snapshot before risky changes)
- [ ] Apply forward migration to prod:
  ```bash
  psql $SUPABASE_DATABASE_URL -f supabase/008_my_feature.sql
  ```
- [ ] Verify schema in dashboard: Settings → Database → Tables / Functions / Policies
- [ ] **Smoke-test from React** as a real user — the change is live now
- [ ] Tail Railway logs for any 500s in the first 5 minutes

If something breaks:
- Apply rollback: `psql $SUPABASE_DATABASE_URL --single-transaction -f supabase/rollback/008_my_feature_rollback.sql`
- If the rollback also fails: restore from PITR snapshot

---

## §9 — Update documentation

Mandatory after merge:

- [ ] [`12-supabase.md`](12-supabase.md):
  - §2 schema (new column / table)
  - §3 FKs / cascades (if applicable)
  - §4 enums (if applicable)
  - §5 indexes (if applicable)
  - §6 triggers (if applicable)
  - §7 RPCs (if applicable)
  - §8 RLS policies (if applicable)
  - §9 realtime publication (if applicable)
  - §10 migration ordering note
  - §11 rollback file note
  - §13 new error code (if applicable)
- [ ] [`03-glossary.md`](03-glossary.md) "P00xx codes" if you added a new error code
- [ ] [`10-cloud-api.md`](10-cloud-api.md) §11 RPC table if API consumes the new RPC
- [ ] Update "Last verified" footer of [`12-supabase.md`](12-supabase.md)

---

## §10 — Coordinate with API + frontend

A migration is rarely standalone. Coordinate:
- API: new endpoint or updated route to consume the new schema → see [`WORKFLOW-add-feature.md`](WORKFLOW-add-feature.md)
- Frontend: new UI or updated query → see [`13-frontend-react.md`](13-frontend-react.md)
- Modal: if a new column the worker writes → update [`11-modal-training.md`](11-modal-training.md)

---

## §11 — Anti-patterns (don't do these)

- **Don't** apply a migration directly to prod without branch-testing.
- **Don't** skip the rollback file. Future you will be grateful.
- **Don't** use destructive `ALTER TABLE ... DROP COLUMN` without a rollback that adds it back (or document data loss prominently).
- **Don't** rename a column in a single migration. Two-phase: add new column → backfill → drop old column.
- **Don't** rely on RLS as primary auth for cloud API paths (service-role bypasses it). RLS is defense-in-depth.
- **Don't** forget `GRANT EXECUTE ... TO service_role` on new RPCs called by the cloud API.
- **Don't** add new triggers without testing performance (BEFORE INSERT triggers run on every row).
- **Don't** skip numbering. `008_my_feature.sql` not `008a_my_feature.sql`.

---

## §12 — Cross-references

- Schema reference: [`12-supabase.md`](12-supabase.md)
- Migration ordering: `supabase/rollback/README.md`
- Cloud API consumption: [`10-cloud-api.md`](10-cloud-api.md)
- Master rules: [`WORKFLOW.md`](WORKFLOW.md)

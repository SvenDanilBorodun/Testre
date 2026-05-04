# 12 — Supabase (Schema + RPCs + RLS + Migrations)

> **Layer:** Postgres database + Auth + Realtime
> **Location:** `Testre/robotis_ai_setup/supabase/`
> **Project ref:** `fnnbysrjkfugsqzwcksd`
> **Read this before:** adding migrations, RLS policies, triggers, RPCs.

---

## 1. Files

```
supabase/
├── migration.sql                    # base schema (users + trainings + RPCs + RLS)
├── 002_accounts.sql                 # role enum + classrooms + credit RPCs
├── 003_lessons_and_notes.sql        # SUPERSEDED — kept only because 004 drops idempotently
├── 004_progress_entries.sql         # daily teacher log (replaces 003 lessons)
├── 005_cloud_job_id.sql             # rename runpod_job_id → cloud_job_id (vendor-neutral)
├── 006_loss_history.sql             # loss array column + downsampling + realtime publication
├── 007_deletion_requested_at.sql    # GDPR Art. 17 marker column
└── rollback/
    ├── README.md
    ├── 002_accounts_rollback.sql
    ├── 004_progress_entries_rollback.sql
    ├── 005_cloud_job_id_rollback.sql
    ├── 006_loss_history_rollback.sql
    └── 007_deletion_requested_at_rollback.sql
```

For adding a new migration, see [`WORKFLOW-supabase-migration.md`](WORKFLOW-supabase-migration.md).

---

## 2. Schema

### Table hierarchy

```
auth.users (Supabase managed)
  └─ public.users (FK id, ON DELETE CASCADE)
       ├─ public.trainings (FK user_id)
       └─ public.classrooms (FK teacher_id, ON DELETE CASCADE) [002]
            └─ public.progress_entries (FK classroom_id, FK student_id NULLABLE) [004]
```

### `public.users`

Composed across migrations (base + 002 + 007):

| Column | Type | Default | NULL | Constraints |
|---|---|---|---|---|
| id | UUID | — | NO | PK, FK auth.users(id) ON DELETE CASCADE |
| email | TEXT | — | NO | |
| training_credits | INTEGER | 0 | NO | (for teachers, this is the pool_total) |
| created_at | TIMESTAMPTZ | NOW() | YES | |
| role | user_role enum | 'student' | NO | [002] |
| username | TEXT | — | YES | UNIQUE [002] |
| full_name | TEXT | — | YES | [002] |
| classroom_id | UUID | — | YES | FK classrooms(id) ON DELETE SET NULL [002] |
| created_by | UUID | — | YES | FK users(id) ON DELETE SET NULL [002] |
| deletion_requested_at | TIMESTAMPTZ | — | YES | [007] |

### `public.trainings`

| Column | Type | Default | NULL | Notes |
|---|---|---|---|---|
| id | SERIAL | — | NO | PK |
| user_id | UUID | — | NO | FK users(id) |
| status | TEXT | 'queued' | NO | CHECK IN ('queued','running','succeeded','failed','canceled') |
| dataset_name | TEXT | — | NO | HF format, must contain `/` |
| model_name | TEXT | — | NO | HF format |
| model_type | TEXT | — | NO | act/diffusion/pi0/etc. |
| training_params | JSONB | — | YES | full TrainingParams body |
| cloud_job_id | TEXT | — | YES | renamed from runpod_job_id in 005; = Modal `FunctionCall.object_id` |
| current_step, total_steps | INTEGER | 0, 0 | YES | progress |
| current_loss | REAL | — | YES | most recent loss |
| requested_at | TIMESTAMPTZ | NOW() | YES | created |
| terminated_at | TIMESTAMPTZ | — | YES | set on terminal status |
| error_message | TEXT | — | YES | German for student-facing errors |
| worker_token | UUID | — | YES | per-row scoped token; nulled on terminal status |
| last_progress_at | TIMESTAMPTZ | — | YES | liveness; used by stalled-worker sweep |
| loss_history | JSONB | '[]' | NO | array of `{"s": step, "l": loss, "t": ms}` [006] |

### `public.classrooms` [002]

| Column | Type | Default | NULL | Constraints |
|---|---|---|---|---|
| id | UUID | gen_random_uuid() | NO | PK |
| teacher_id | UUID | — | NO | FK users(id) ON DELETE CASCADE |
| name | TEXT | — | NO | UNIQUE(teacher_id, name) |
| created_at | TIMESTAMPTZ | NOW() | YES | |

### `public.progress_entries` [004]

| Column | Type | Default | NULL | Constraints |
|---|---|---|---|---|
| id | UUID | gen_random_uuid() | NO | PK |
| classroom_id | UUID | — | NO | FK classrooms(id) ON DELETE CASCADE |
| student_id | UUID | — | YES | FK users(id) ON DELETE CASCADE; NULL = class-wide entry |
| entry_date | DATE | CURRENT_DATE | NO | one entry per (scope, day) |
| note | TEXT | — | NO | |
| created_at, updated_at | TIMESTAMPTZ | NOW() | YES | updated_at maintained by trigger |

---

## 3. Foreign keys / cascades

| FK | ON DELETE |
|---|---|
| users.id → auth.users | CASCADE |
| users.classroom_id → classrooms | SET NULL |
| users.created_by → users | SET NULL |
| trainings.user_id → users | (default RESTRICT) |
| classrooms.teacher_id → users | CASCADE |
| progress_entries.classroom_id → classrooms | CASCADE |
| progress_entries.student_id → users | CASCADE |

**Cascade chain:** delete teacher → cascades to classrooms → cascades to progress_entries (and classroom-wide entries). Delete student → cascades to their per-student progress_entries (class-wide entries unaffected).

---

## 4. Enum

```sql
CREATE TYPE public.user_role AS ENUM ('admin', 'teacher', 'student');
```

Defined in 002, line 10. Drives RLS policy branches.

---

## 5. Indexes

| Index | Table | Columns | Type | Migration |
|---|---|---|---|---|
| `idx_users_role` | users | role | regular | 002 |
| `idx_users_username` | users | username | regular | 002 |
| `idx_users_classroom` | users | classroom_id | partial: WHERE classroom_id IS NOT NULL | 002 |
| `idx_users_deletion_requested_at` | users | deletion_requested_at | partial: WHERE deletion_requested_at IS NOT NULL | 007 |
| `idx_trainings_user_id` | trainings | user_id | regular | base |
| `idx_trainings_status` | trainings | status | regular | base |
| `idx_trainings_requested_at DESC` | trainings | requested_at DESC | regular | base |
| `idx_trainings_worker_token` | trainings | worker_token | partial: WHERE worker_token IS NOT NULL | base |
| `idx_trainings_user_id_status` | trainings | (user_id, status) | compound | base |
| `idx_classrooms_teacher` | classrooms | teacher_id | regular | 002 |
| `idx_progress_entries_classroom` | progress_entries | (classroom_id, entry_date DESC) | regular | 004 |
| `idx_progress_entries_student` | progress_entries | (student_id, entry_date DESC) | partial: WHERE student_id IS NOT NULL | 004 |
| `uniq_progress_entries_student_day` | progress_entries | (student_id, entry_date) | UNIQUE partial: WHERE student_id IS NOT NULL | 004 |
| `uniq_progress_entries_classroom_day` | progress_entries | (classroom_id, entry_date) | UNIQUE partial: WHERE student_id IS NULL | 004 |

The two partial UNIQUE indexes on progress_entries enforce: ≤1 per-student entry per day AND ≤1 class-wide entry per day. They cohabit because the `WHERE` clauses are mutually exclusive.

---

## 6. Triggers

| Trigger | Table | Event | Function | Purpose |
|---|---|---|---|---|
| `on_auth_user_created` | auth.users | AFTER INSERT | `handle_new_user()` | Auto-create public.users row on Supabase Auth signup |
| `trg_classroom_capacity` | public.users | BEFORE INSERT OR UPDATE | `enforce_classroom_capacity()` | Reject if &gt;30 students in classroom (P0010) |
| `trg_progress_entries_touch` | public.progress_entries | BEFORE UPDATE | `touch_updated_at()` | Bump updated_at on edit |

---

## 7. RPCs (callable functions)

### `handle_new_user()` (migration.sql:13–24)

`SECURITY DEFINER` trigger. INSERTs into public.users with `training_credits=0` from `NEW.id` and `NEW.email`.

### `get_remaining_credits(p_user_id UUID)` (migration.sql:94–112)

```
RETURNS TABLE(training_credits INT, trainings_used BIGINT, remaining BIGINT)
SECURITY DEFINER, STABLE
```

Self-healing: derives `trainings_used = COUNT(trainings WHERE user_id = ? AND status NOT IN ('failed','canceled'))`. No counters to corrupt.

### `update_training_progress(...)` (migration.sql:119–164 → rewritten in 006:33–114)

```
PARAMS: p_training_id INT, p_token UUID, p_status TEXT,
        p_current_step INT, p_total_steps INT, p_current_loss REAL,
        p_error_message TEXT
RETURNS: VOID
SECURITY DEFINER, SET search_path=public
```

Logic (006 version):
1. Validate p_status in ('running','succeeded','failed','canceled')
2. If `p_current_step IS NOT NULL AND p_current_loss IS NOT NULL`:
   - Append `{"s": step, "l": loss, "t": ms}` to `loss_history` JSONB
   - If array length > 300: downsample to 1 first + 199 evenly-spaced middle + 100 last
3. UPDATE trainings SET status, current_step, total_steps, current_loss, error_message, loss_history, last_progress_at=NOW()
4. If p_status terminal: set terminated_at=NOW(), worker_token=NULL
5. WHERE id = p_training_id AND worker_token = p_token (token + id both required)
6. If 0 rows updated: RAISE P0001

### `start_training_safe(...)` (migration.sql:175–222)

```
PARAMS: p_user_id UUID, p_dataset_name, p_model_name, p_model_type TEXT,
        p_training_params JSONB, p_total_steps INT, p_worker_token UUID
RETURNS: TABLE(training_id INT, remaining INT)
SECURITY DEFINER, GRANT TO service_role
```

Atomic credit-check + insert:
1. SELECT training_credits FROM users WHERE id=p_user_id FOR UPDATE
2. If credits is NULL: RAISE P0002
3. Count active trainings (status NOT IN failed/canceled)
4. If used &gt;= credits: RAISE P0003
5. INSERT trainings RETURNING id
6. Return (new_id, credits - used - 1)

### `get_teacher_credit_summary(p_teacher_id UUID)` (002:62–87)

```
RETURNS: TABLE(pool_total INT, allocated_total BIGINT, pool_available BIGINT, student_count BIGINT)
```

LEFT JOIN classrooms → users(students). Returns:
- pool_total = teacher.training_credits
- allocated_total = SUM(student credits)
- pool_available = pool_total - allocated_total
- student_count = COUNT(students)

### `adjust_student_credits(p_teacher_id UUID, p_student_id UUID, p_delta INTEGER)` (002:96–157)

```
RETURNS: TABLE(new_amount INT, pool_available BIGINT)
SECURITY DEFINER, GRANT TO service_role
```

Atomic credit reallocation:
1. Verify student in teacher's classrooms (else P0011)
2. SELECT teacher + student credits FOR UPDATE
3. v_new = student_current + p_delta
4. If v_new < students_used (active trainings): P0012
5. If v_new &lt; 0: P0013
6. If sum(other_students) + v_new > teacher.pool_total: P0014
7. UPDATE student SET training_credits = v_new
8. RETURN (v_new, teacher.pool_total - allocated_others - v_new)

### `enforce_classroom_capacity()` (002:40–55) — trigger

If NEW.classroom_id IS NOT NULL AND NEW.role = 'student':
- COUNT students in that classroom (excluding self on UPDATE)
- If &gt;= 30: RAISE P0010

### `touch_updated_at()` (003:69–77 → 004:47–55)

`SET NEW.updated_at := NOW(); RETURN NEW;`

Used by trg_progress_entries_touch.

---

## 8. RLS policies

**Note:** Cloud API uses **service-role key**, which bypasses RLS. These policies are dormant in the API path. They DO apply to:
- React clients using anon-key (sign-in flow, Realtime subscriptions)
- Modal worker using anon-key (RPC calls, but workers go through `update_training_progress` which is SECURITY DEFINER and bypasses RLS internally)

So RLS is **defense-in-depth**, not the primary guard. Real auth lives in `_assert_*` helpers in the API.

### users

- "Users read own profile" — SELECT, USING auth.uid() = id
- "Users update own profile" — UPDATE, USING auth.uid() = id, WITH CHECK auth.uid() = id
- "Teachers read own students" [002] — SELECT, USING classroom_id IN (SELECT id FROM classrooms WHERE teacher_id = auth.uid())
- "Admin reads everyone" [002] — SELECT, USING EXISTS (SELECT 1 FROM users WHERE id=auth.uid() AND role='admin')

### trainings

- "Users read own trainings" — SELECT, USING auth.uid() = user_id
- "Users insert own trainings" — INSERT, WITH CHECK auth.uid() = user_id
- "Users update own trainings" — UPDATE
- "Users delete own trainings" — DELETE
- "Teachers read student trainings" [002] — SELECT, USING user_id IN classroom-owned-students subquery

### classrooms [002]

- "Teachers read own classrooms" — auth.uid() = teacher_id
- "Students read own classroom" — id = (SELECT classroom_id FROM users WHERE id = auth.uid())
- "Admin reads all classrooms" — admin role check

### progress_entries [004]

- "Teachers read own progress entries" — classroom owned by auth.uid()
- "Students read own + own-classroom entries" — student_id = auth.uid() OR (student_id IS NULL AND classroom_id = student's classroom_id)
- "Admin reads all"

---

## 9. Realtime publication (006)

```sql
ALTER PUBLICATION supabase_realtime ADD TABLE public.trainings;
```

Wrapped in `DO` block that checks `pg_publication_tables` for idempotency.

**What it enables:** React `useSupabaseTrainings` hook subscribes to `postgres_changes` events filtered by `user_id`. Every UPDATE to a trainings row pushes a real-time event → React updates the chart without polling.

**Scope:** read-only from client (RLS still applies on subscriptions).

---

## 10. Migration ordering &amp; idempotency

### 003 → 004 transition

- 003 introduced `lessons`, `lesson_progress`, `progress_note` column. **Superseded.**
- 004 starts with `DROP TABLE IF EXISTS lesson_progress; DROP TABLE IF EXISTS lessons; DROP TYPE IF EXISTS lesson_status; ALTER TABLE users DROP COLUMN IF EXISTS progress_note;`
- **Both 003 and 004 are idempotent** — rerunning either is a no-op if the other already ran.
- **Practical implication:** if you fresh-install, you can skip 003 and just apply 004. The repo keeps 003 only as a historical record.

### 005 — runpod_job_id → cloud_job_id

Pure column rename. No state change. All downstream code uses `cloud_job_id`.

### 006 — loss_history + downsampling + realtime

- Adds `loss_history JSONB DEFAULT '[]' NOT NULL`
- Rewrites `update_training_progress` with downsampling logic
- Adds trainings to supabase_realtime publication

### 007 — deletion_requested_at

Adds `deletion_requested_at TIMESTAMPTZ NULL` + partial index. Used by `/me/delete` endpoint (GDPR Art. 17).

---

## 11. Rollback scripts (apply in REVERSE order)

```
007 → 006 → 005 → 004 → 002
```

Skip 003 (no rollback file; 003's tables are dropped by 004).

| File | Reverses | Data loss |
|---|---|---|
| `007_deletion_requested_at_rollback.sql` | 007 | Pending deletion requests forgotten |
| `006_loss_history_rollback.sql` | 006 | Loss histories lost. Does **NOT** drop `update_training_progress()` — re-apply migration.sql to restore the original |
| `005_cloud_job_id_rollback.sql` | 005 | None |
| `004_progress_entries_rollback.sql` | 004 | All daily progress notes lost. **Must run BEFORE 002 rollback** (FK ordering) |
| `002_accounts_rollback.sql` | 002 | All teachers/admins/students lost (role column dropped); all classroom data lost |

To wipe to base, use Supabase dashboard reset-database (no rollback file for migration.sql).

See `supabase/rollback/README.md` for full procedure.

---

## 12. Loss history downsampling (006)

Algorithm in `update_training_progress` (006:33–114):

1. **Append**: `{"s": step, "l": loss, "t": ms}` to `loss_history`
2. **Downsample if &gt;300 entries**:
   - Keep `arr[0]` (first point)
   - Keep 199 evenly-spaced middle points: indices `1 + s * (len-102)/198` for s in 0..198
   - Keep last 100 entries
   - Result: 1 + 199 + 100 = 300 max
3. Use `jsonb_agg(elem ORDER BY idx)` to recombine

**Cost:** O(len) read/scan once &gt;300. Acceptable because progress writes are sparse (few per second).
**Memory cap:** ~15 KB per training row.

---

## 13. Custom Postgres error codes

| Code | Raised by | Meaning |
|---|---|---|
| P0001 | `update_training_progress` | Token mismatch or training_id not found |
| P0002 | `start_training_safe` | User profile not found |
| P0003 | `start_training_safe` | Insufficient credits |
| P0010 | `enforce_classroom_capacity` trigger | Classroom full (30 max) |
| P0011 | `adjust_student_credits` | Student not in teacher's classrooms |
| P0012 | `adjust_student_credits` | New amount < trainings_used |
| P0013 | `adjust_student_credits` | Would go negative |
| P0014 | `adjust_student_credits` | Teacher pool exhausted |

API maps these to HTTP 403/409 with German messages. See [`10-cloud-api.md`](10-cloud-api.md) §6.

---

## 14. Auth flow

1. Student/teacher/admin types `username + password` in React
2. React: `signInWithPassword({email: synthetic_email(username), password})`
3. Supabase Auth validates → JWT issued
4. JWT in `Authorization: Bearer` for every Railway API call
5. Railway: `supabase.auth.get_user(token)` validates signature + expiration
6. Railway: looks up `public.users` row via `get_user_profile(user.id)` → role + classroom_id

The trigger `handle_new_user` ensures every `auth.users` insert creates a `public.users` row.

`bootstrap_admin.py` creates the first admin: `auth.admin.create_user({email: admin@edubotics.local, password, email_confirm=True})` then `users.update({role: 'admin', username, full_name})`.

---

## 15. Cross-references

- API endpoints that call these RPCs: [`10-cloud-api.md`](10-cloud-api.md) §6, §11
- Modal worker's RPC contract (`update_training_progress`): [`11-modal-training.md`](11-modal-training.md) §4
- Realtime subscription on React side: [`13-frontend-react.md`](13-frontend-react.md) §7
- Adding a new migration: [`WORKFLOW-supabase-migration.md`](WORKFLOW-supabase-migration.md)
- Rollback ordering: `supabase/rollback/README.md`
- Operations (rotate keys, manual DB queries, GDPR): [`20-operations.md`](20-operations.md) §1, §3, §7
- Known issues (RLS bypass, IDOR risk, no realtime auth test): [`21-known-issues.md`](21-known-issues.md) §2.4, §3.7

---

**Last verified:** 2026-05-04.

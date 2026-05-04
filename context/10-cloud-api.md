# 10 — Cloud Training API (Railway FastAPI)

> **Layer:** Cloud training backend
> **Location:** `Testre/robotis_ai_setup/cloud_training_api/`
> **Owner:** Our code
> **Read this before:** adding routes, RPCs, auth checks, rate limits, dedup logic, or anything in `app/`.

---

## 1. Module map

```
cloud_training_api/
├── Dockerfile
├── requirements.txt
└── app/
    ├── main.py                    # FastAPI app, CORS, RateLimit middleware, router include
    ├── auth.py                    # JWT validation + role helpers (FastAPI deps)
    ├── services/
    │   ├── supabase_client.py    # Lazy singleton, service-role key
    │   ├── modal_client.py        # Modal SDK wrapper (spawn / cancel / status)
    │   └── usernames.py           # Username regex + synthetic email helper
    └── routes/
        ├── training.py            # /trainings/* (start, cancel, list, get, quota)
        ├── teacher.py             # /teacher/* (classrooms, students, credits, progress)
        ├── admin.py               # /admin/* (teachers, credits, password)
        ├── me.py                  # /me, /me/export, /me/delete
        ├── health.py              # /health → {status: ok}
        └── version.py             # /version → {version, download_url, required}
```

**Routers registered in `main.py`:** health, version, training (`/trainings`), me, teacher (`/teacher`), admin (`/admin`).

---

## 2. main.py — middleware stack

### CORS validation (`_parse_and_validate_origins()` lines 29–66)

Parses `ALLOWED_ORIGINS` env var (CSV). Rejects:
- Literal `*` with `allow_credentials=True` (browsers block; FastAPI doesn't warn)
- Wildcards like `https://*.vercel.app` (treated as literal strings, silently break the allowlist)
- URLs without proper scheme or netloc
- Empty origin list

Raises `RuntimeError` at startup if invalid → fail-fast.

### RateLimiter (in-process, lines 80–95)

- Per-bucket, per-key deque storage with monotonic time
- **Process-local state** — works correctly only with `--workers 1` (Railway default)
- 2 rules:
  - `/trainings/start`: 10 req/min/IP
  - `/trainings/cancel`: 20 req/min/IP
- Key = leftmost `X-Forwarded-For` entry (Railway always sets it; falls back to `request.client.host`)
- **Returns `JSONResponse(429)` directly** — middleware exceptions don't route through FastAPI's handlers (Starlette bug); raising `HTTPException` here would yield 500

### Middleware ordering (lines 144–149)

```python
app.add_middleware(RateLimitMiddleware)  # added first → innermost
app.add_middleware(CORSMiddleware)       # added second → outermost
```

Request flow: CORS → RateLimit → endpoint → RateLimit → CORS → response. Critical: CORS is outer so the 429 response also gets CORS headers (browser doesn't reject as CORS error).

---

## 3. auth.py — JWT validation + role helpers

### `get_current_user()` (lines 10–43)

1. Read `Authorization: Bearer <token>` header (401 if missing or wrong prefix)
2. Cheap structural check: token has exactly 2 dots (JWT format)
3. `supabase.auth.get_user(token)` → delegates signature + expiration to Supabase
4. Catch all exceptions → generic 401 (no infrastructure leakage)
5. Return Supabase auth user object (id, email, …)

### `get_user_profile(user_id)` (lines 46–58)

`SELECT id, email, role, username, full_name, classroom_id, training_credits, created_by FROM users WHERE id = ? LIMIT 1`. Raises 404 if no row.

### Role-bound dependencies

- `get_current_teacher()` (lines 61–65): require `role == "teacher"` → 403 otherwise
- `get_current_admin()` (lines 68–72): require `role == "admin"` → 403 otherwise
- `get_current_profile()` (lines 75–77): any authenticated user, returns full profile

**Pattern:** every route uses `current = Depends(get_current_*)` to gate access by role.

---

## 4. services/supabase_client.py

`get_supabase()` (lines 8–14): lazy singleton, initialized with `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`.

**Footgun warning:** every query runs as **service-role** (bypasses RLS). Authorization is enforced in Python via `_assert_*` helpers (see §6). One missed assertion = silent IDOR.

---

## 5. services/modal_client.py

### `_get_train_function()` (lines 27–36)

Returns `modal.Function.from_name(MODAL_TRAINING_APP_NAME, MODAL_TRAINING_FUNCTION_NAME)` (defaults `edubotics-training` / `train`).

### `start_training_job()` (lines 39–57)

```python
call = await fn.spawn.aio(
    dataset_name=..., model_name=..., model_type=...,
    training_params=..., training_id=..., worker_token=...
)
return call.object_id     # stored as cloud_job_id in Supabase
```

Worker credentials (`SUPABASE_URL`, `SUPABASE_ANON_KEY`, `HF_TOKEN`) injected via Modal Secret `edubotics-training-secrets`, **not** in payload.

### `cancel_training_job(job_id)` (lines 60–68)

`FunctionCall.from_id(job_id).cancel.aio(terminate_containers=True)`. Logs warning on failure; **does not swallow exception**.

### `get_job_status(job_id)` (lines 71–110)

`FunctionCall.from_id(job_id).get.aio(timeout=0)` (non-blocking poll).

| Modal exception | Returns |
|---|---|
| `TimeoutError` / `modal.exception.TimeoutError` | `IN_PROGRESS` |
| `modal.exception.FunctionTimeoutError` | `TIMED_OUT` |
| `modal.exception.InputCancellation` | `CANCELLED` |
| `modal.exception.RemoteError` / `ExecutionError` | `FAILED` |
| (no exception) | `COMPLETED` |
| Unrecognized | `UNKNOWN_STATUS` (sentinel; reconciler leaves row alone) |

Catches both `TimeoutError` shapes for Modal SDK version robustness.

---

## 6. routes/training.py

### Configuration constants

```python
STALLED_WORKER_THRESHOLD = timedelta(minutes=int(os.getenv("STALLED_WORKER_MINUTES", "15")))
DEDUPE_WINDOW = timedelta(seconds=60)
MAX_STEPS = int(os.getenv("MAX_TRAINING_STEPS", "500000"))
MAX_BATCH_SIZE = 256
MAX_TIMEOUT_HOURS = 12.0
ALLOWED_POLICIES = set(os.getenv("ALLOWED_POLICIES", "tdmpc,diffusion,act,vqbet,pi0,pi0fast,smolvla").split(","))
POLICY_MAX_TIMEOUT_HOURS = {"act": 1.5, "vqbet": 2.0, "tdmpc": 2.0, "diffusion": 4.0, "pi0fast": 4.0, "pi0": 6.0, "smolvla": 6.0}
```

### Pydantic models

| Model | Fields | Bounds |
|---|---|---|
| `TrainingParams` | steps, batch_size, num_workers, log_freq, save_freq, eval_freq, seed, timeout_hours, output_folder_name | steps 1–MAX_STEPS, batch 1–256, workers 0–16, others bounded |
| `StartTrainingRequest` | dataset_name (1–200, must contain `/`), model_type (must be in ALLOWED_POLICIES), training_params | German error: `"Modelltyp '{v}' ist für dieses Konto nicht freigeschaltet."` |
| `TrainingJob` | id, status, dataset_name, model_name, model_type, training_params, current_step, total_steps, current_loss, loss_history, requested_at, terminated_at, error_message, last_progress_at | response shape |
| `StartTrainingResponse` | training_id, model_name, status | |
| `UserQuota` | training_credits, trainings_used, remaining | |

### MODAL_TO_DB_STATUS map (lines 162–170)

```
QUEUED/IN_QUEUE → queued
IN_PROGRESS     → running
COMPLETED       → succeeded
FAILED          → failed
CANCELLED       → canceled
TIMED_OUT       → failed
```

UNKNOWN_STATUS not in map → reconciler does nothing (preserves liveness).

### Helpers

- **`_sanitize_name()`** (lines 173–176): keeps `[a-zA-Z0-9._-]`, replaces others with `-`, strips trailing `-`. HF-safe.
- **`_generate_model_name()`** (lines 179–197): composes `EduBotics-Solutions/[output_folder-]model_type-dataset-10randomhex`.
- **`_get_remaining_credits(user_id)`** (lines 200–217): RPC `get_remaining_credits(p_user_id)` → `{training_credits, trainings_used, remaining}`. Self-healing.
- **`_parse_iso(s)`** (lines 220–227): parse Postgres `TIMESTAMPTZ` ISO string with `Z` suffix.
- **`_sync_modal_status(training)`** (lines 230–307, async): reconciles row vs Modal:
  1. Modal terminal state → flip DB status
  2. Modal can't find job (UNKNOWN_STATUS) → leave alone
  3. Worker stalled: Modal IN_PROGRESS but no progress for STALLED_WORKER_THRESHOLD → cancel Modal + mark failed (German error)
  - **No refund needed**: credit auto-frees when status → failed
- **`_find_recent_duplicate(user_id, dataset, model_type, params)`** (lines 310–363): SELECT trainings WHERE matching keys + `requested_at > now()-60s` + status NOT IN (failed, canceled). Params canonicalized via `json.dumps(..., sort_keys=True, default=str)` to handle key ordering. Excludes failed/canceled so retry works immediately.
- **`_sweep_user_running_jobs(user_id)`** (lines 366–390, async): `asyncio.gather()` of `_sync_modal_status` for all queued/running rows. Called at start of `/start` so stuck rows can't block credit check.

### Endpoints

#### `GET /quota` (lines 396–399)

Returns `UserQuota` for current user via `_get_remaining_credits()`.

#### `POST /start` (lines 402–543) — the heavyweight

Flow:
1. **Sweep** stuck rows (`_sweep_user_running_jobs`)
2. **Dedup** check (`_find_recent_duplicate`)
3. **HF preflight** (`HfApi().dataset_info(dataset_name)`):
   - `RepositoryNotFoundError` → 400 (typo)
   - other exception → 502 (transient)
4. **Apply policy timeout cap**: `training_params["timeout_hours"] = min(requested, POLICY_MAX_TIMEOUT_HOURS[model_type])`
5. **Atomic credit-check + insert** via RPC `start_training_safe`:
   - P0003 → 403 ("No training credits remaining.")
   - P0002 → 404 ("User profile not found")
6. **Dispatch to Modal** (`start_training_job`):
   - Exception → mark training failed, 500
7. **Update row** with `cloud_job_id` + `status=running`
8. Return `StartTrainingResponse`

#### `POST /cancel` (lines 546–588)

Verify ownership (eq user_id, eq training_id) → check status in queued/running (400 otherwise) → `cancel_training_job(cloud_job_id)` (logs warning on failure, continues) → mark `status=canceled`.

#### `GET /list` (lines 591–607)

`SELECT trainings WHERE user_id ORDER BY requested_at DESC LIMIT 50`. Then `asyncio.gather(_sync_modal_status(t) for t in rows)`.

#### `GET /{training_id}` (lines 610–624)

Verify ownership → sync status if active.

---

## 7. routes/teacher.py

### Pydantic models (selected)

- `ClassroomCreate` / `ClassroomRename`: `name` (1–100 chars)
- `StudentCreate`: username (3–32, `[a-z0-9._-]`), password (6–128), full_name (1–100), initial_credits (0–1000)
- `StudentPatch`: full_name + classroom_id (optional)
- `PasswordReset`: new_password (6–128)
- `CreditsDelta`: delta (-1000 to 1000)

### Critical helpers (the IDOR firewall)

- **`_assert_classroom_owned(teacher_id, classroom_id)`** (lines 92–103): SELECT classrooms WHERE id AND teacher_id. 404 if not found ("Klassenzimmer nicht gefunden").
- **`_assert_student_owned(teacher_id, student_id)`** (lines 106–121): SELECT users WHERE id AND role=student → check classroom_id is set → call `_assert_classroom_owned` for student's classroom. 404 with German message.
- **`_assert_entry_owned(teacher_id, entry_id)`** (lines 557–566): SELECT progress_entries → call `_assert_classroom_owned`.

**Every endpoint that modifies a classroom/student/entry MUST call these helpers.** They're the entire authorization story.

### Endpoint inventory

#### Classrooms

| Method | Path | Purpose |
|---|---|---|
| GET | `/classrooms` | list with student counts |
| POST | `/classrooms` | create (handles 409 unique on name) |
| GET | `/classrooms/{id}` | detail w/ student list |
| PATCH | `/classrooms/{id}` | rename |
| DELETE | `/classrooms/{id}` | delete (409 if not empty) |

#### Students

| Method | Path | Purpose |
|---|---|---|
| POST | `/classrooms/{id}/students` | Create student (validate username → unique check → `auth.admin.create_user` → update users row → optional `adjust_student_credits` RPC) |
| PATCH | `/students/{id}` | full_name and/or move classroom |
| DELETE | `/students/{id}` | `auth.admin.delete_user` (cascades) |
| POST | `/students/{id}/password` | reset via `auth.admin.update_user_by_id` |
| POST | `/students/{id}/credits` | RPC `adjust_student_credits` |
| GET | `/students/{id}/trainings` | list student's trainings |

Credit RPC error code mapping:
- P0011 → 403 (not student's teacher)
- P0012 → 409 (would go below used)
- P0013 → 409 (negative)
- P0014 → 409 (pool exhausted)

#### Progress entries

| Method | Path | Purpose |
|---|---|---|
| GET | `/classrooms/{id}/progress-entries?student_id=&scope=` | list with filters |
| POST | `/classrooms/{id}/progress-entries` | create (409 on unique constraint per scope/day) |
| PATCH | `/progress-entries/{id}` | update note |
| DELETE | `/progress-entries/{id}` | delete |

Filters:
- no filter → all entries for classroom
- `student_id=<uuid>` → that student's
- `scope=classroom` → class-wide entries (`student_id IS NULL`)
- `scope=student` → per-student entries (`student_id IS NOT NULL`)

---

## 8. routes/admin.py

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/teachers` | list (per-teacher RPC `get_teacher_credit_summary` + classroom count) — O(N) RPC calls per request, optimization candidate |
| POST | `/teachers` | create teacher (validate username → `auth.admin.create_user` → set role + credits) |
| PATCH | `/teachers/{id}/credits` | set pool_total. Rejects if `new < allocated_total` |
| POST | `/teachers/{id}/password` | reset |
| DELETE | `/teachers/{id}` | refuse if classrooms&gt;0 (409 "Lehrer hat noch Klassenzimmer - erst löschen") |

---

## 9. routes/me.py

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/me` | profile + (for teachers) RPC `get_teacher_credit_summary` for pool fields |
| GET | `/me/export` | GDPR Art. 15 — JSON bundle: profile, trainings, classrooms (teachers), progress_entries (per role) |
| POST | `/me/delete` | GDPR Art. 17 — refuse for admins, cancel active trainings, set `deletion_requested_at=now()` |

`/me/delete` (lines 131–234):
1. Reject if role=admin (400)
2. Cancel each `queued`/`running` training (Modal cancel + mark canceled locally)
3. Set `users.deletion_requested_at` (column from migration 007)
4. Return `{status, canceled_trainings, message}`

---

## 10. routes/health.py + routes/version.py

- `GET /health` → `{"status": "ok"}` (always, no DB hit)
- `GET /version` → `{version: GUI_VERSION, download_url: GUI_DOWNLOAD_URL, required: true}` from env. 503 if env unconfigured.

---

## 11. RPCs the API depends on

All defined in Supabase migrations (see [`12-supabase.md`](12-supabase.md)):

| RPC | Returns | Raises |
|---|---|---|
| `get_remaining_credits(p_user_id)` | `{training_credits, trainings_used, remaining}` | — |
| `start_training_safe(...)` | `{training_id, remaining}` | P0002 (user not found), P0003 (no credits) |
| `update_training_progress(...)` | void | P0001 (token mismatch) — used by Modal worker, not API |
| `adjust_student_credits(p_teacher_id, p_student_id, p_delta)` | `{new_amount, pool_available}` | P0011, P0012, P0013, P0014 |
| `get_teacher_credit_summary(p_teacher_id)` | `{pool_total, allocated_total, pool_available, student_count}` | — |

---

## 12. Error vocabulary

| Code | Used for |
|---|---|
| 400 | Validation failure (regex, dataset format), no changes in PATCH |
| 401 | Missing header, invalid JWT |
| 403 | Wrong role; no credits remaining; not student's teacher |
| 404 | Resource not found (training, classroom, student, user) |
| 409 | Conflict: unique constraint, classroom capacity, pool exhausted |
| 429 | Rate limit (start: 10/min/IP, cancel: 20/min/IP) |
| 500 | Supabase/Modal/HF error not mapped, Modal dispatch failed |
| 502 | HF transient (rate limit, DNS, 5xx) |
| 503 | Version endpoint unconfigured |

**German error messages live alongside English ones:**
- German for user-facing fields (returned to React → student/teacher reads it)
- English for backend developer messages (logs, structured errors)

See [`WORKFLOW.md`](WORKFLOW.md) §1 for the language boundary rule.

---

## 13. Footguns

1. **Service-role key bypasses RLS.** Every new query needs an `_assert_*` ownership check. RLS policies exist but are dormant under service-role.
2. **Rate limiter requires `--workers 1`.** Don't add `--workers N` to the Dockerfile CMD without switching to Redis-backed limiter.
3. **`UNKNOWN_STATUS` preserves liveness.** A running job won't be mismarked failed on an unrecognized Modal SDK error.
4. **Stalled-worker threshold uses `last_progress_at` OR `requested_at`.** A job dispatched but never started still gets marked failed after 15 min. This is intentional (catches Modal queue stalls).
5. **Timeout capping happens AFTER RPC.** User says `timeout_hours=10`, ACT cap is 1.5 → DB row stores 1.5, Modal respects 1.5. Don't be confused when debugging.
6. **Dedup excludes only failed/canceled.** A job stuck `queued` is considered a duplicate. The `_sweep_user_running_jobs()` runs first to flip stuck rows to failed.
7. **HF dataset_info has no explicit timeout.** Hung HF can stall the request. Known issue ([§3.7 of known-issues](21-known-issues.md)).
8. **Credit accounting derives from rows.** No counter column. Manually flipping a row's status frees the credit automatically.
9. **`auth.admin.create_user` + table update is NOT in a transaction.** If the table update fails after auth user is created, you have an orphaned auth user. Cleanup in the route's exception handler (or `bootstrap_admin.py` does it explicitly).

---

## 14. Local dev

```bash
cd robotis_ai_setup/cloud_training_api
cp .env.example .env  # if exists; otherwise create
# SUPABASE_URL=https://fnnbysrjkfugsqzwcksd.supabase.co
# SUPABASE_SERVICE_ROLE_KEY=eyJ...
# MODAL_TOKEN_ID=ak-...
# MODAL_TOKEN_SECRET=as-...
# HF_TOKEN=hf_...
# ALLOWED_ORIGINS=http://localhost:3000,http://localhost
# ALLOWED_POLICIES=tdmpc,diffusion,act,vqbet,pi0,pi0fast,smolvla
pip install -r requirements.txt
uvicorn app.main:app --reload
```

For a smoke test:
```bash
curl http://localhost:8000/health
curl -H "Authorization: Bearer $JWT" http://localhost:8000/me | jq
```

Deploy:
```bash
railway up --detach   # from cloud_training_api/
```

---

## 15. Cross-references

- Pydantic env-var inventory: [`04-env-vars.md`](04-env-vars.md) §2
- Supabase RPCs + RLS: [`12-supabase.md`](12-supabase.md)
- Modal worker: [`11-modal-training.md`](11-modal-training.md)
- React API client: [`13-frontend-react.md`](13-frontend-react.md) §6
- Operations runbooks (rotate secrets, debug stuck training): [`20-operations.md`](20-operations.md)
- Known issues for this layer: [`21-known-issues.md`](21-known-issues.md) §3.7

---

**Last verified:** 2026-05-04.

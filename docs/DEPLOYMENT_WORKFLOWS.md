# EduBotics Deployment Workflows — Deep Dive

> Companion to `docs/deploy/DEPLOY.md`. DEPLOY.md is the one-page operator checklist;
> this document is the architecture explainer — how a code change in this repo
> *actually* reaches a student's running install, layer by layer, with the file
> and line references you need to debug it when it breaks.
>
> Read in order. Each section ends with the **propagation chain** — the exact
> sequence of intermediate artifacts a byte traverses from `git commit` to
> "the student sees the new behavior."

---

## 0. The five surfaces and what each one ships

EduBotics has **five independent deployment targets**, each with its own trigger,
artifact, and student-side propagation path. They are independent in the sense
that any one can be redeployed without the others — but they have hard
*ordering constraints* (§6) because the schema fingerprint and the routes that
reference Modal targets fail-loudly if their dependencies aren't there yet.

| # | Surface | Artifact | Trigger | Reaches student via |
|---|---|---|---|---|
| 1 | **Supabase** | SQL migration applied to live Postgres | Maintainer pastes file into Studio SQL Editor | Cloud API + React talk to same DB |
| 2 | **Modal** | App revision (`edubotics-training`, `edubotics-vision`) | `modal deploy <file>` | Cloud API dispatches via `Function.from_name(...).spawn.aio()` |
| 3 | **Railway — Cloud API** | Docker image of `cloud_training_api/` | Git push to `main` (Railway autodeploy from Dockerfile) | Student GUI + React both call this URL |
| 4 | **Railway — Web Dashboard** | Docker image of `physical_ai_manager/` in web mode | `scripts/railway-deploy.sh` (manual `railway up`) | Teacher/admin browser load |
| 5 | **Docker Hub — student images** | `nettername/{open-manipulator,physical-ai-server,physical-ai-manager}:latest` (amd64) + `*-jetson*` (arm64) | Maintainer runs `build-images.sh` | Student GUI auto-pulls on next launch |
| 5a | **Student .exe** | `EduBotics_Setup.exe` in a GitHub release + `GUI_VERSION` env on Railway | Manual build + `gh release create` + Railway env bump | GUI polls `/version` on startup; prompts re-install |

The student-facing endpoint of every change is **one of three places**:

- **Inside the WSL2 distro** (Docker Hub images) — code that runs on the robot
  (ROS nodes, React student bundle, Roboter Studio workflow runtime).
- **On Railway** (Cloud API + Web Dashboard) — code that the GUI / browser
  talks to over HTTPS.
- **On Modal** (training + vision apps) — code that runs only when the
  Cloud API spawns a job.

A change to `cloud_training_api/app/routes/training.py` doesn't go through Docker
Hub at all. A change to `physical_ai_tools/physical_ai_server/...` doesn't touch
Railway. A change to `modal_training/training_handler.py` doesn't even rebuild
an image — it side-loads on next dispatch. Knowing **which surface owns the
file you just edited** is the single most important deploy-time skill in this
codebase.

---

## 1. Supabase — migrations to live Postgres

### 1.1 What lives in the repo

```
robotis_ai_setup/supabase/
├── migration.sql               ← base schema (users, trainings, RPCs, RLS)
├── 002_accounts.sql            ← role enum, classrooms
├── 003_lessons_and_notes.sql   ← superseded by 004 (no rollback)
├── 004_progress_entries.sql
├── 005_cloud_job_id.sql
├── 006_loss_history.sql
├── 007_deletion_requested_at.sql
├── 008_workflows.sql
├── 009_workflows_rls_writes.sql
├── 010_progress_terminal_guard.sql
├── 011_workgroups.sql
├── 012_dataset_sweep.sql
├── 013_revoke_anon_from_security_definer.sql
├── (no 014 — see CLAUDE.md §9.13)
├── 015_workflow_versions.sql
├── 016_tutorial_progress.sql
├── 017_vision_quota.sql
├── 018_workflow_versions_author_and_group_rls.sql
├── 019_classroom_jetsons.sql
├── 020_jetson_v2.sql
├── 021_workgroup_memberships_realtime_and_owner_check.sql
└── rollback/*_rollback.sql     ← one per forward (except 003)
```

There is **no `supabase migration up`** running anywhere. Every numbered file
is a self-contained `BEGIN; … COMMIT;` transaction that a human pastes into
the Supabase Studio SQL Editor. The forward file uses `IF NOT EXISTS` /
`CREATE OR REPLACE` so re-running is idempotent; the rollback uses
`IF EXISTS` / `DROP` likewise.

### 1.2 How it actually deploys

```
Local edit → 019_xxx.sql + rollback/019_xxx_rollback.sql
   │
   ▼
Supabase Studio (browser) → SQL Editor → paste file → Run
   │
   ▼
Live Postgres (project SUPABASE_URL) ←─── service-role key writes here
   │
   ├─► Railway Cloud API talks here (service-role)
   ├─► React student/web bundles talk here (anon-key, RLS-bound)
   ├─► Modal training worker talks here (anon-key + per-row worker_token)
   └─► Jetson agent talks here indirectly (no direct DB; Cloud API proxies)
```

For multi-migration rollouts (the 015+016+017 trio that landed Phase-2/3) we
keep a **pre-bundled** file at `docs/deploy/APPLY_MIGRATIONS.sql` and a reverse
bundle at `docs/deploy/ROLLBACK_MIGRATIONS.sql`. The bundle is *not* a separate
file format — it's literally `\i` of the three migrations concatenated with
verification probes, designed to be pasted whole.

### 1.3 The schema fingerprint — the safety net

This is the single most important mechanism in the deploy story.

`cloud_training_api/app/main.py:117-319` defines `_validate_required_schema()`
and calls it at module load (before `uvicorn` even binds). It probes:

- **Tables** (`select('id').limit(0)`) — line 139 onward. List includes
  `jetsons` (019), `workflow_versions` (015), `tutorial_progress` (016),
  `workgroup_memberships` (011), and every other table the routes reference.
- **Columns** — line 173 onward. Probes `users.vision_quota_per_term` and
  `users.vision_used_per_term` (017) because adding the RPCs without the
  columns is a real failure mode.
- **RPCs** — line 202 onward. Probes with realistic argument shapes
  (`rpc('claim_jetson', {'p_jetson_id': '00000000-...', 'p_user_id': '...'})`)
  so a partial-apply where the RPC body changed but the signature didn't
  matches the deployed-code expectation also surfaces.

On miss (`PGRST202` "function does not exist" or `42P01` "relation does not
exist") the function raises `RuntimeError` with the named missing objects, and
**Railway aborts the deploy** — `/health` never returns 200, the previous
revision keeps serving, and the operator sees the failure in the Railway logs
instead of a stream of student 500s.

Escape hatch: `EDUBOTICS_SKIP_SCHEMA_CHECK=1` (line 131). Unit-test only. If
you ever find yourself wanting to set this on Railway, the answer is "apply
the missing migration."

### 1.4 Bootstrap admin (one-shot)

After the base `migration.sql` + `002_accounts.sql` land, there is no admin
user — `handle_new_user` makes every signup a student. Run
`robotis_ai_setup/scripts/bootstrap_admin.py` *once*: it reads
`SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` from
`cloud_training_api/.env`, calls `auth.admin.create_user()` with a synthetic
`{username}@edubotics.local` email, then updates the `public.users` row to
`role='admin'`. Re-running with the same username fails because the auth user
already exists.

### 1.5 Key segregation

| Key | Holder | Capability |
|---|---|---|
| `SUPABASE_SERVICE_ROLE_KEY` | Railway Cloud API (env var only) | Bypasses RLS. Used by every route. Authorization enforced in Python via `_assert_*_owned()` helpers (CLAUDE.md §1.4, §7.7). |
| `SUPABASE_ANON_KEY` | React bundle (baked at build time), Modal training worker (Secret) | RLS-bound. The student's bundle holds it but can only see their own rows; Modal worker holds it but can only update rows where `worker_token` matches. |
| `worker_token` (per-row UUID) | Modal training worker | Combined with anon key, scopes the worker's writes to one specific `trainings` row via the `update_training_progress(p_token, ...)` RPC. Migration 010 forbids overwriting a terminal row. |
| `agent_token` (per-Jetson UUID) | Classroom Jetson agent | Long-lived. Sent in every `agent_heartbeat_jetson` RPC call to learn current owner. Stored in `/etc/edubotics/jetson.env` mode 600 on the Jetson. Mirrors the worker-token shape. |

**Never** ship `SUPABASE_SERVICE_ROLE_KEY` to a React bundle, a Modal Secret,
or a Jetson. The Cloud API is the only thing that holds it.

### 1.6 Propagation chain

```
git commit → maintainer pastes file → Studio SQL Editor → Run
   ↓
Postgres now has the new schema object
   ↓
(Railway redeploy if Cloud API uses it — schema fingerprint will block boot otherwise)
   ↓
Next student/teacher request hits the new schema
```

The student never sees a "migration in progress." Either it's applied (and
their next API call uses it) or it isn't (and Railway is on the previous
revision because the fingerprint blocked the new one).

---

## 2. Modal — training + vision apps

### 2.1 What ships

Two apps, two separate secret bundles:

| App | File | Modal name | GPU | Timeout | Min containers | Snapshot |
|---|---|---|---|---|---|---|
| Training | `robotis_ai_setup/modal_training/modal_app.py` | `edubotics-training` | L4 | `7 * 3600` s | 0 | no |
| Vision | `robotis_ai_setup/modal_training/vision_app.py` | `edubotics-vision` | T4 | 120 s | 0 | yes (`enable_memory_snapshot=True`) |

The training image's heavy lifting:

```python
# modal_app.py:23-50 (sketch)
image = (
    modal.Image.from_registry("nvidia/cuda:12.1.1-devel-ubuntu22.04", add_python="3.11")
    .apt_install("git", "ffmpeg", "clang", "build-essential")
    .pip_install(f"lerobot[pi0] @ git+https://github.com/huggingface/lerobot.git@{LEROBOT_COMMIT}", ...)
    .pip_install("torch", "torchvision",
                 index_url="https://download.pytorch.org/whl/cu121",
                 extra_options="--force-reinstall")            # critical: cu121 not cu130
    .run_commands("python -m pip uninstall -y torchcodec || true")  # transitive that crashes
    .env({"PYTHONUNBUFFERED": "1"})
    .add_local_python_source("training_handler")               # side-load — see §2.4
)
```

The vision image is much lighter (`debian_slim` + transformers + pinned
`huggingface_hub==0.26.2` + cu121 torch force-reinstall) and uses a persistent
`modal.Volume.from_name("edubotics-vision-cache", create_if_missing=True)` so
the ~600 MB OWLv2 weights download exactly once per workspace lifetime.

### 2.2 The secret split (audit round-3 §I)

```bash
modal secret create edubotics-training-secrets \
    SUPABASE_URL=... \
    SUPABASE_ANON_KEY=... \
    HF_TOKEN=hf_<write>

modal secret create edubotics-vision-secrets \
    HF_TOKEN=hf_<read>
```

Vision has no Supabase creds at all. The Cloud API holds the quota state
(`users.vision_used_per_term`) and decrements via `consume_vision_quota` RPC
*before* dispatching the Modal call; on transient 502/504 it calls
`refund_vision_quota`. The vision worker just runs inference and returns
JSON — it cannot scribble into the DB even if compromised. **Don't merge the
bundles.**

### 2.3 The 5-place LeRobot pinning

`LEROBOT_COMMIT = "989f3d05ba47f872d75c587e76838e9cc574857a"` at
`modal_app.py:19` is one of five sites that must agree (CLAUDE.md §1.5):

1. `modal_app.py:19` — Modal image pip install
2. `physical_ai_tools/lerobot/` — byte-identical static snapshot
3. `robotis/physical-ai-server:amd64-0.8.2` base image internal submodule
4. `meta/info.json codebase_version: "v2.1"` derived from CODEBASE_VERSION at this SHA
5. `training_handler.py:23 EXPECTED_CODEBASE_VERSION = "v2.1"`

A LeRobot bump is a single PR that touches all five. Skip one and either
recording datasets fail Modal preflight (`codebase_version` mismatch) or the
in-image lerobot calls a function the snapshot doesn't have.

### 2.4 How a code change reaches a Modal worker

Here's the subtle bit. `modal_app.py:49` says:

```python
.add_local_python_source("training_handler")
```

This **does not bake `training_handler.py` into the image**. It side-loads the
file at function-invocation time. Practically:

- Changes to `training_handler.py` → next `modal deploy modal_app.py` →
  next student dispatch picks up the new code, **no image rebuild required**.
- Changes to `modal_app.py` itself (pip pins, apt packages, cu121 line,
  LEROBOT_COMMIT) → next `modal deploy` rebuilds the image layer that
  changed → first dispatch on the new revision pays the rebuild cost; later
  dispatches reuse the cache.
- Changes to the LeRobot upstream SHA → 5-place change → image rebuild from
  the pip step onward.

The image build is **not on the student's critical path**. The student
clicks "Train" → Cloud API `Function.from_name(...).spawn.aio()` → Modal
either has a warm container (reuse) or boots a fresh one from the
already-built image. Image rebuilds happen on `modal deploy`, not on
dispatch.

### 2.5 Railway → Modal dispatch contract

`cloud_training_api/app/services/modal_client.py` is the only place Cloud API
talks to Modal:

```python
def _get_train_function():
    app_name = os.environ.get("MODAL_TRAINING_APP_NAME", "edubotics-training")
    fn_name = os.environ.get("MODAL_TRAINING_FUNCTION_NAME", "train")
    return modal.Function.from_name(app_name, fn_name)

async def start_training_job(...) -> str:
    fn = _get_train_function()
    call = await fn.spawn.aio(dataset_name=..., model_name=..., training_id=..., worker_token=...)
    return call.object_id      # persisted to Supabase as trainings.cloud_job_id
```

`cloud_job_id` is the handle the Cloud API uses for every subsequent
operation — cancel, status sync, the stalled-worker sweep — via Modal's
`FunctionCall.from_id(cloud_job_id)`. The `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET`
Railway env vars are read automatically by the Modal SDK; we never construct
a client manually.

Vision is slightly different — synchronous `.remote.aio()` on the
`OWLv2Detector` class instance, with a 30 s outer timeout
(`VISION_MODAL_TIMEOUT_S`) on top of the 120 s function timeout. The Cloud API
maps Modal 502/504 to a German error and calls `refund_vision_quota`.

### 2.6 Preempt grace

Modal sends SIGINT with 30 s grace on preemption / cancel / function timeout.
`training_handler.py:494-495` registers `_on_shutdown` for SIGINT + SIGTERM
which:

1. `proc.kill()` + `proc.wait(5)` on the LeRobot subprocess.
2. 3-retry RPC to mark the row failed with the German
   `Worker wurde vom Cloud-Anbieter beendet. Bitte Training neu starten.`
3. `shutil.rmtree(OUTPUT_DIR, ignore_errors=True)`.
4. `sys.exit(0)` inside the 30 s window.

If the handler misses the grace, Railway's `_sync_modal_status` sweep
(see §3) reconciles the row within `STALLED_WORKER_MINUTES`.

### 2.7 Deploy commands (canonical)

```bash
cd robotis_ai_setup/modal_training

# Sanity-check imports — catches Modal SDK API drift before deploy:
modal run -m modal_app::smoke_test       # expects torch=2.x+cu121, cuda_available=true
modal run -m vision_app::smoke_test      # expects cuda_available=true

modal deploy modal_app.py
modal deploy vision_app.py

modal app list | grep edubotics
modal app logs edubotics-training         # tail live worker output
```

CI's `modal-import-validate` job (CLAUDE.md §15 jobs list) pip-installs the
Modal SDK and imports both files, so an SDK API rename surfaces at PR review
instead of at `modal deploy` time.

### 2.8 Propagation chain

```
git commit (training_handler.py or modal_app.py)
   ↓
modal deploy modal_app.py     ← maintainer runs locally
   ↓
Modal stamps the source into a new app revision
   ↓
Next /trainings/start from Railway → fn.spawn.aio() → fresh container uses new code
   ↓
Student sees the change on their next training dispatch
```

Modal does **not** auto-deploy on git push — `modal deploy` is always
maintainer-initiated. The pre-deploy smoke test is the only thing standing
between you and a bad image.

---

## 3. Railway — Cloud API (auto-deploys on git push)

### 3.1 What ships

`robotis_ai_setup/cloud_training_api/` builds via its own Dockerfile and
deploys to the Railway service `scintillating-empathy-production-1068`.

```dockerfile
# cloud_training_api/Dockerfile (sketch)
FROM python:3.11-slim
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
```

The `railway.json` next to the Dockerfile says:

```json
{
  "build":   { "builder": "DOCKERFILE", "dockerfilePath": "Dockerfile" },
  "deploy":  { "restartPolicyType": "ON_FAILURE", "restartPolicyMaxRetries": 10 }
}
```

That's the *whole* trigger. Railway is connected to the GitHub repo; a push
to `main` builds the image and rolls a new revision in. There is **no
`railway up` for the Cloud API** — pushing to `main` is the deploy.

### 3.2 Mandatory env vars (fail-fast)

`app/main.py:_validate_required_secrets()` runs at module load. Missing any
of these → `RuntimeError` → Railway aborts the rollout, previous revision
keeps serving.

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`

After secrets, `_validate_required_schema()` runs (§1.3) and fails the boot
if migrations are behind.

### 3.3 Optional env vars (operator-tunable)

Most of these are in CLAUDE.md §7.2. The high-impact ones:

| Var | What it controls |
|---|---|
| `STALLED_WORKER_MINUTES` | `_sync_modal_status` cancels Modal job + marks row failed if `last_progress_at` is older than this. Currently 15 on live Railway; code default 25. |
| `DISPATCH_LOST_MINUTES` | If Modal can't find the FunctionCall after this long, mark row failed. |
| `ALLOWED_ORIGINS` | CORS allow-list. `_parse_and_validate_origins()` rejects literal `*` with credentials. |
| `ALLOWED_POLICIES` | CSV filter on the `model_type` Pydantic validator. Student build defaults to `act` only. |
| `GUI_VERSION`, `GUI_DOWNLOAD_URL` | Drives the `/version` endpoint → student `.exe` upgrade gate. See §3.6. |
| `HF_TOKEN` | Required for dataset preflight + GDPR cleanup + dataset reconciliation sweep. |
| `EDUBOTICS_SKIP_SCHEMA_CHECK` | Never set this on Railway. |
| `DATASET_SWEEP_DISABLED`, `JETSON_SWEEPER_DISABLED` | Disable background reconciliation loops. |

### 3.4 The single-worker requirement

`CMD` uses default `--workers 1` (no explicit flag, Uvicorn picks 1). This is
load-bearing for three things:

1. **In-process rate limiter** (`app/main.py:373-388`). The `deque`-keyed dict
   sits in worker memory; with N workers a student gets N× the configured
   bucket.
2. **Dataset sweep** (`app/services/dataset_sweep.py`). One reconciliation
   loop per process — duplicating it would multiply HF API calls and risk
   double-registering.
3. **Jetson sweeper** (`app/services/jetson_sweep.py`, 60 s loop).
   Same concern.

If you ever need horizontal scale, swap in Redis-backed `slowapi` and a
Postgres advisory lock for the sweeps. Don't just bump `--workers`.

### 3.5 The rate-limited surface

In-process counters (CLAUDE.md §7.4), keyed by the leftmost
`X-Forwarded-For` IP **except** `/vision/detect`, which is keyed by JWT
`sub` so 30 NAT'd students don't share one bucket. 429 responses are emitted
as `JSONResponse` directly, not `raise HTTPException`, because Starlette's
`BaseHTTPMiddleware` swallows the latter into a 500. CORS is mounted
**outermost** so 429s still carry `Access-Control-*` headers.

### 3.6 The `/version` endpoint — how the student `.exe` updates itself

This is how a `VERSION` file bump in this repo actually reaches existing
student installs.

1. `/version` (cloud API) reads the Railway env vars `GUI_VERSION` and
   `GUI_DOWNLOAD_URL`, returns `{version, download_url, required: true}`,
   or 503 if either is unset.
2. The Windows GUI's `gui/app/update_checker.check_for_update()` polls
   `/version` on every launch (CLAUDE.md §6.2 step 1).
3. If `live_version > installed_version` → blocking modal → download `.exe`
   to `%TEMP%` → `os.startfile()` runs the installer.

The thing to internalize: **bumping `VERSION` in the repo does nothing on
its own**. The propagation chain only completes when:

```
1. Bump VERSION + installer/robotis_ai_setup.iss AppVersion + gui/app/constants.py fallback
2. Build EduBotics_Setup.exe on Windows (Inno Setup)
3. gh release create v2.3.0 --title "..." ./installer/output/EduBotics_Setup.exe
4. Railway env: GUI_VERSION=2.3.0 + GUI_DOWNLOAD_URL=https://github.com/.../v2.3.0/EduBotics_Setup.exe
5. Railway picks up the new env vars on next service restart
```

Skip any step and `/version` still serves the old version (or 503), the
update gate never fires, and the new code reaches no one. The student-facing
React bundle inside `physical-ai-manager:latest` has its own auto-reload
path via `useVersionCheck` against `/version.json` (see §4 and §5), which is
independent of the `.exe` gate.

### 3.7 Background loops started at boot

- **`_start_dataset_sweep`** (`app/main.py`) — kicks off the reconciliation
  loop in `app/services/dataset_sweep.py`. Skips if `HF_TOKEN` is unset or
  `DATASET_SWEEP_DISABLED=1`. Period `DATASET_SWEEP_INTERVAL_S` (default 600).
- **`_start_jetson_sweeper`** — kicks off `app/services/jetson_sweep.py`.
  60 s loop, calls `sweep_jetson_locks()` RPC, releases any lock whose
  `current_owner_heartbeat_at` is older than 5 min.

Both run *in the same uvicorn worker* as the HTTP handlers — another reason
`--workers 1`.

### 3.8 Propagation chain

```
git push origin main
   ↓
Railway detects push, builds the Cloud API Dockerfile
   ↓
New container boots, runs _validate_required_secrets() then _validate_required_schema()
   ↓ (one fails → rollout aborts, previous revision keeps serving)
/health returns 200
   ↓
Background sweeps start
   ↓
Next request from a student GUI, React bundle, or Jetson agent hits the new code
```

Student visibility is essentially instant — there's no client cache between
the GUI and the Cloud API.

---

## 4. Railway — Web Dashboard (manual `railway up`)

### 4.1 What ships

The same React codebase as the student build, but produced from
`physical_ai_tools/physical_ai_manager/Dockerfile.web` with
`REACT_APP_MODE=web` and the full `ALLOWED_POLICIES` list. Two-stage Node
build → `nginx:1.27.5-alpine` runtime listening on `${PORT}` (Railway
injects). Five strict security headers (HSTS 2y, X-Frame-Options DENY,
X-Content-Type-Options nosniff, Referrer-Policy, Permissions-Policy) live in
`nginx.web.conf.template` and apply to **every** location.

### 4.2 Why this one isn't git-autodeploy

The build context needs a file that's not in `physical_ai_manager/` —
specifically `_coco_classes.py`, staged from
`physical_ai_tools/physical_ai_server/.../coco_classes.py` so the
`prebuild` Jest hook (`objectClasses.sync.test.js`) can verify the React
dropdown matches the server's allowlist.

Railway's autodeploy uploads the whole repo from the project root, which
would either include the file at the wrong path or fall back to Railpack and
miss the Dockerfile entirely. The fix is `scripts/railway-deploy.sh`:

```bash
# physical_ai_manager/scripts/railway-deploy.sh (sketch)
cp ../physical_ai_server/physical_ai_server/workflow/coco_classes.py ./_coco_classes.py
trap "rm -f ./_coco_classes.py" EXIT
railway up --path-as-root .          # treats THIS directory as the upload root
```

So the maintainer flow is: `cd physical_ai_manager && ./scripts/railway-deploy.sh`
— never `git push` for the web dashboard.

### 4.3 Build sanity check

`Dockerfile.web:36-38` aborts the build if `REACT_APP_SUPABASE_URL`,
`REACT_APP_SUPABASE_ANON_KEY`, or `REACT_APP_CLOUD_API_URL` is empty —
defensive against the white-screen regression that ate a couple of days
before the smoke test landed. CI's `teacher-web-build-validate` job
(CLAUDE.md §15 job 6) reproduces this build with placeholder secrets so a
broken Dockerfile.web fails on PR.

### 4.4 The Vercel kill-switch

`vercel.json` is intentionally an empty object. The reason: Vercel
autodetects a `package.json` in the repo and will happily ship a shadow
deployment with its own security-header config. The empty `vercel.json`
tells Vercel "yes there's a project here, but no build, no output, no
framework," which neuters the auto-detect. Don't delete this file or
populate it.

### 4.5 Client-side auto-reload

`hooks/useVersionCheck.js` polls `/version.json?_={now}` with
`cache: 'no-store'` every 30 s, on focus, on visibilitychange. If
`liveBuildId !== process.env.REACT_APP_BUILD_ID` (both must be non-`dev`)
and the last reload was ≥ 60 s ago (sessionStorage guard against reload
loops), `window.location.reload()` fires. A teacher's open browser tab
picks up a new web-dashboard deploy within ~30 s without manual refresh.

The student build does the same against its own `/version.json` served by
the in-container nginx — that's how a new `physical-ai-manager:latest`
image's React bundle takes over inside the WSL container after the GUI
auto-pull.

### 4.6 Propagation chain

```
git commit (React change)
   ↓
cd physical_ai_manager && ./scripts/railway-deploy.sh
   ↓
_coco_classes.py staged, railway up --path-as-root . runs
   ↓
Railway builds Dockerfile.web with the 6 REACT_APP_* build args
   ↓
nginx starts, serves /version.json with the new buildId
   ↓
Within 30s, every open teacher/admin browser tab reloads itself
```

---

## 5. Docker Hub — student images (`nettername/*`)

### 5.1 What ships

Three amd64 images for the student PC:

- `nettername/open-manipulator:latest`
- `nettername/physical-ai-server:latest`
- `nettername/physical-ai-manager:latest`

Plus four arm64 images for the classroom Jetson (v2.3.0+, intentionally
**separate Docker Hub repos** so a Jetson rollout never collides with the
amd64 student stack):

- `nettername/open-manipulator-jetson:latest`
- `nettername/physical-ai-server-jetson:latest`
- `nettername/open-manipulator-jetson-base:4.1.4` (one-time arm64 base)
- `nettername/physical-ai-server-jetson-base:0.8.2` (one-time arm64 base)

### 5.2 The build orchestrator

`robotis_ai_setup/docker/build-images.sh` is the only sanctioned build
entry point. Headers fail-loud via `${VAR:?...}` for the three
build-time secrets:

```bash
SUPABASE_URL=...        \
SUPABASE_ANON_KEY=...   \
CLOUD_API_URL=...       \
ALLOWED_POLICIES=act    \  # optional
REGISTRY=nettername     \  # optional
PLATFORM=amd64          \  # or arm64 for Jetson builds
./build-images.sh
```

The three secrets propagate into the React bundle as `REACT_APP_*` build
args, and a post-build smoke test (lines ~218-232) runs the container and
greps `main.*.js` for literal `SUPABASE_URL` and `CLOUD_API_URL` strings.
Missing → script exits 1 and refuses to push. The same check is duplicated
in CI's `manager-build-validate` job.

### 5.3 The `_coco_classes.py` staging (same trick, second site)

Lines ~189-191 stage the COCO classes file from the server side into the
manager build context (cleanup via shell trap on line ~123) so the prebuild
Jest hook can run inside the Docker build. **Important:** for the Railway
web build (§4) you must use `scripts/railway-deploy.sh` which does the same
staging — bare `railway up` skips it and the Jest test gracefully warns +
no-ops, which means dropdown↔server sync silently stops being enforced.

### 5.4 BUILD_ID

`BUILD_ID = ${BUILD_TS}-${BUILD_SHA}` — UTC timestamp + 7-char git SHA
(fallback 8-byte random hex if not in a git repo). Baked into the React
bundle as `REACT_APP_BUILD_ID` and emitted to `/version.json`. The
in-container auto-reload (§4.5) keys off this — when the GUI auto-pull
replaces the running `physical-ai-manager` container, every open browser
tab on `localhost:80` reloads inside 30 s.

### 5.5 The Docker Desktop dual-store gotcha (CLAUDE.md §13.4.bis)

Docker Desktop ≥4.x runs two parallel image stores: BuildKit's
containerd-snapshotter (where `docker buildx build` writes) and the classic
daemon store (where `docker push` reads). A plain `docker build -t ... .`
followed by `docker push` can silently upload a stale image — the push
"succeeds" but the registry never gets the new bytes.

`build-images.sh` mitigates this for amd64:

- **amd64 path** uses `docker buildx build --platform linux/amd64 --load`
  (copies into the daemon store) and then a separate push loop.
- **arm64 path** uses `docker buildx build --platform linux/arm64 --push`
  (writes straight to the registry, bypasses the daemon store entirely) and
  skips the separate push loop.

**Mandatory post-push verification** (see CLAUDE.md §13.4 step 9): after
the script says "All images built and pushed!", pull the image fresh from
the registry on a *different* host (or via `--pull always`) and grep for an
audit marker that should be in the new code. The 2026-05-15 F62/F65/F66
deploy required this exact dance to surface a partial-push that the
maintainer thought had succeeded.

### 5.6 Student-side auto-pull (the propagation reality)

`gui/app/docker_manager.py:check_for_updates()` runs on **every GUI
launch**. The 2.2.4 hardening (F67/F68/F69) added three layers of defence:

1. **Escape hatch** — `EDUBOTICS_SKIP_AUTO_PULL=1` short-circuits.
2. **Offline probe** (`is_dockerhub_reachable`) — 5 s TCP probe to
   `registry-1.docker.io:443`. Offline → skip and use cache, no retry
   storm.
3. **Manifest digest pre-check** (`_get_remote_manifest_digest` +
   `_get_local_repo_digest`) — `docker manifest inspect` (no layer
   download), pick the `linux/amd64` entry, compare to local `RepoDigest`.
   Match → skip pull. Steady-state path drops from ~30 s of pulls to ~3 s
   of HEAD requests.
4. **Real pull with stall watchdog** — 20 s poll interval, 10 MB
   disk-growth threshold on `/var/lib/docker/overlay2`, 120 s stall
   timeout (auto-pull path) with 2 retries + exp backoff. On stall ≥
   attempt 2, `_reset_dockerd()` (`pkill -KILL dockerd` → restart →
   15-attempt readiness poll).
5. **Last-pull persistence** —
   `%LOCALAPPDATA%/EduBotics/.last_image_pull.json` stores timestamp +
   per-image digests. Next launch surfaces "Letzter Update vor X Tagen"
   with a red banner past `IMAGE_FRESHNESS_WARN_DAYS=14`.

This is the **only** thing that propagates a Docker Hub push to a student
who installed the `.exe` months ago. If you push an image and the student
doesn't restart the GUI, they keep running the old containers.

### 5.7 Compose-side image references

`docker-compose.yml` references `${REGISTRY:-nettername}/<image>:latest`
for all three services. `IMAGE_TAG` could in principle be parameterized via
`docker/versions.env`, but that file is currently gitignored / not
created — the fallback to `:latest` is what actually runs.

### 5.8 Propagation chain (amd64 student)

```
git commit (Dockerfile/overlay/React change)
   ↓
maintainer: cd robotis_ai_setup/docker && SUPABASE_URL=... SUPABASE_ANON_KEY=... \
            CLOUD_API_URL=... ./build-images.sh
   ↓
buildx --load → daemon store → smoke test greps bundle → docker push
   ↓ (verify post-push: pull fresh, grep audit marker)
Docker Hub now has new image
   ↓
Student launches EduBotics.exe
   ↓
docker_manager.check_for_updates(): TCP probe → manifest digest mismatch → pull
   ↓
docker compose up -d --force-recreate brings up new containers
   ↓
React bundle inside the new manager container has a new BUILD_ID
   ↓
Open browser tabs on localhost:80 detect /version.json mismatch → reload
```

For the Jetson arm64 path the chain is shorter — there's no GUI auto-pull
on the Jetson; the agent's docker-compose handles image freshness on
service start. See `docs/JETSON_DEPLOY.md`.

---

## 6. The golden order — why ordering matters

```
1. Supabase migrations        ← Railway boot fingerprint gates on this
2. Modal apps                 ← Cloud API /vision/detect 503s without it
3. Railway Cloud API          ← New routes ready for the next request
4. Docker Hub images          ← Students pull on next launch
5. git push                   ← CI runs 11 guardrails
```

The failure modes if you reorder:

| Skip | Effect |
|---|---|
| Migrations before Railway | `_validate_required_schema()` raises at boot, Railway aborts rollout, previous revision serves. **Safe failure.** |
| Modal before Railway | Cloud API tries `Function.from_name(...)` against a name Modal doesn't have yet → 503 on `/vision/detect`. **Loud failure.** |
| Railway before Docker | Student GUI calls a route that doesn't exist yet on Railway → 404/422. The reverse (Docker before Railway) is worse: students hit a Railway URL whose handler relies on a column they don't have yet → 500. |
| Docker before Modal | A student trains, dispatch hits the new Modal app target, the app doesn't exist → student sees German "Dispatch fehlgeschlagen" + credit refund. |
| git push last (vs first) | CI didn't run yet, so a typo or missing import on the Cloud API only surfaces when the Railway deploy auto-fires on the same push. **In practice steps 3 and 5 are the same git push** — Railway autodeploys from `main`, so "push" is "deploy". |

The golden order's *meaning* is: **stage dependencies before consumers.**
Supabase ← Modal ← Cloud API ← (Docker images, web dashboard, .exe).

A change touching only one layer follows the same rule for its layer's
dependencies. A pure overlay edit (changes Docker image only) doesn't
need Modal or migrations. A migration that adds a column needs the
Cloud API redeploy *after* the migration but doesn't need Modal redeploy
unless the Modal worker reads the new column.

---

## 7. The 5-place version bump — closing the loop for `.exe` installs

When the version number changes (CLAUDE.md §14), five sites must move
together:

1. `VERSION` (repo root) — read by `gui/app/constants.py:APP_VERSION`.
2. `installer/robotis_ai_setup.iss AppVersion` — Inno Setup product version.
3. `gui/app/constants.py` `APP_VERSION` fallback constant — the safety net
   if `VERSION` isn't readable.
4. **Railway env vars** `GUI_VERSION` + `GUI_DOWNLOAD_URL` on the Cloud API
   service — only this makes `/version` return the new version.
5. **GitHub release** `v<version>` with the `EduBotics_Setup.exe` asset —
   the URL `GUI_DOWNLOAD_URL` must point at.

Skip any of (1-3) → CI catches you (`/version` smoke compares).
Skip (4) → in-tree bump is invisible to existing installs.
Skip (5) → update gate fires but `os.startfile()` runs against a 404.

The React bundle inside the `physical-ai-manager` image has its own auto-
reload via `useVersionCheck` against the `BUILD_ID` baked at build time —
that's a separate path from the `.exe` gate and only requires steps 5 of §5
(Docker Hub push + student GUI auto-pull) to propagate.

---

## 8. CI guardrails (`.github/workflows/ci.yml`)

11 jobs run on push/PR to `main`. The ones that *prevent bad deploys* (vs
just verifying correctness):

| Job | Catches |
|---|---|
| `python-tests` | Cloud API + Modal handler + GUI auto-pull regressions (stubbed deps, cross-platform) |
| `compose-validate` | Broken `docker-compose.yml` would silently fail at the student's machine |
| `overlay-guard` | `fix_server_inference.py` patch silently no-opping |
| `modal-import-validate` | Modal SDK API drift in `modal_app.py` / `vision_app.py` — catches the bad-deploy before `modal deploy` |
| `teacher-web-build-validate` | Railway `Dockerfile.web` regressions with exact `--path-as-root .` semantics |
| `manager-build-validate` | The white-screen regression — asserts secrets actually inlined in the React bundle |
| `tutorials-validate` | Skillmap referencing a block the runtime doesn't dispatch — would fail in the student's Workshop tab |
| `interfaces-validate` | `.srv` files referenced in `CMakeLists.txt` but missing on disk |
| `nginx-validate` | `envsubst $PORT` + `nginx -t` on both web and student configs |

`modal-import-validate` and `manager-build-validate` are the two most
prescient: each prevents a class of deploy that would *succeed* mechanically
but ship broken bytes.

---

## 9. Quick reference — file → deploy surface map

When you edit a file, this table tells you what surface it ships on:

| Path pattern | Surface | Deploy command |
|---|---|---|
| `robotis_ai_setup/supabase/*.sql` | Supabase | Paste into Studio SQL Editor |
| `robotis_ai_setup/modal_training/modal_app.py` | Modal (training image) | `modal deploy modal_app.py` |
| `robotis_ai_setup/modal_training/training_handler.py` | Modal (side-loaded) | `modal deploy modal_app.py` (no image rebuild) |
| `robotis_ai_setup/modal_training/vision_app.py` | Modal (vision app) | `modal deploy vision_app.py` |
| `robotis_ai_setup/cloud_training_api/**` | Railway Cloud API | `git push origin main` |
| `physical_ai_tools/physical_ai_manager/**` (web build) | Railway web dashboard | `cd .../physical_ai_manager && ./scripts/railway-deploy.sh` |
| `physical_ai_tools/physical_ai_manager/**` (student build) | Docker Hub `physical-ai-manager` | `./build-images.sh` |
| `physical_ai_tools/physical_ai_server/**` + `robotis_ai_setup/docker/physical_ai_server/overlays/**` | Docker Hub `physical-ai-server` | `./build-images.sh` |
| `open_manipulator/**` + `robotis_ai_setup/docker/open_manipulator/overlays/**` | Docker Hub `open-manipulator` | `./build-images.sh` |
| `robotis_ai_setup/jetson_agent/**` | Jetson host filesystem (via `setup.sh`) + Docker Hub `*-jetson` | `PLATFORM=arm64 ./build-images.sh` + teacher reruns `setup.sh` |
| `robotis_ai_setup/gui/**` + `robotis_ai_setup/installer/**` | Student `.exe` | Manual Windows build + GitHub release + Railway env bump |
| `VERSION`, `installer/robotis_ai_setup.iss` | Student `.exe` upgrade gate | All 5 of §7 |

---

## 10. Debugging "the change didn't reach the student"

When a student reports "this feature isn't working" but you swear you
deployed it, walk this checklist in order:

1. **Which surface owns the file?** (§9). If you edited `training_handler.py`
   but only pushed to GitHub, Modal still has the old code.
2. **Did the deploy actually land?**
   - Supabase: query the live DB directly. `select 1 from public.<new_table>`.
   - Modal: `modal app list` shows the revision timestamp.
   - Railway: dashboard → service → Deployments → look for "Active" badge on
     your commit SHA.
   - Docker Hub: `docker manifest inspect nettername/<image>:latest` and
     compare digest to what you pushed.
3. **For Docker Hub specifically: did the student's GUI run the auto-pull?**
   Read `%LOCALAPPDATA%/EduBotics/install_diagnostics.log` and
   `%LOCALAPPDATA%/EduBotics/.last_image_pull.json` from their machine.
   If `EDUBOTICS_SKIP_AUTO_PULL=1` is set in their env, the pull is a no-op.
4. **For the Web dashboard: did the browser tab reload?** sessionStorage
   key `__edubotics_version_reload_at` shows last reload time. If it's
   recent and they're still on the old code, the build args in Railway
   probably didn't change → `REACT_APP_BUILD_ID` is identical → no reload
   trigger.
5. **For the `.exe`: is `/version` returning the new version?**
   `curl https://scintillating-empathy-production-1068.up.railway.app/version`.
   If it's still serving the old version (or 503), step 4 of §7 wasn't done.
6. **For Cloud API: are the required env vars set?** Railway dashboard →
   service → Variables. Missing `SUPABASE_SERVICE_ROLE_KEY` or
   `MODAL_TOKEN_ID` → `_validate_required_secrets` aborted the boot →
   previous revision is still serving.
7. **For migrations: did the fingerprint pass?** Railway logs show
   `_validate_required_schema()` output. If it raised, the new code
   isn't running.

The single most common deploy failure mode is **edited multiple surfaces,
deployed only one**. The next most common is **forgot the Docker Desktop
buildx-vs-push gotcha** (§5.5).

---

## 11. See also

- `docs/deploy/DEPLOY.md` — one-page operator checklist (paste-and-run)
- `docs/deploy/APPLY_MIGRATIONS.sql` — pre-bundled forward migrations
- `docs/deploy/ROLLBACK_MIGRATIONS.sql` — reverse-order rollback bundle
- `docs/JETSON_DEPLOY.md` — teacher runbook for classroom Jetson pairing
- `docs/arm64_base/README.md` — one-time arm64 base image build
- `CLAUDE.md` — §1.5 (LeRobot pinning), §6.2 (GUI startup), §7 (Cloud API
  reference), §9 (Supabase schema), §10 (Docker compose + build), §13
  (workflows for Claude), §13.4.bis (Docker Desktop dual-store), §14
  (version drift map), §15 (CI jobs)
- `CAMERA_PIPELINE_FIXES.md` — F1-F69 audit markers convention (preserve
  `// Audit F##` comments when editing tagged files)

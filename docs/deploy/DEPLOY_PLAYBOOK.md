# EduBotics deploy playbook

Built from the actual deploy scripts (`build-images.sh`, `railway-deploy.sh`), the schema fingerprint code in `cloud_training_api/app/main.py`, the `docs/deploy/` bundle, and CLAUDE.md §13 + §18.3.

## The golden rule: deploy ORDER

There is **one safe ordering** and it's load-bearing. Doing it any other way produces the c56c012-class failure (route ships, dependency missing in production, students see 503s):

```
1. Supabase migrations    ← always first
2. Modal apps             ← before Railway tries to reach them
3. Railway (Cloud API)    ← schema fingerprint gates this; will refuse to boot if step 1 didn't land
4. Docker Hub images      ← rebuild + push the student-facing 3 images
5. Git push to GitHub     ← record state; CI runs guardrails (modal-import-validate, manager-build-validate, etc.)
```

Skipping step 1 makes step 3 hang on a fail-fast schema-probe. Skipping step 2 makes step 3 succeed but `POST /vision/detect` returns 503. Doing step 4 before step 3 makes student installations call routes that don't exist yet.

---

## Per-target playbook

### 1. Supabase (Postgres migrations)

**What you can ship:** new tables, new columns, new RPCs, new RLS policies, new triggers, new realtime publications.

**Files involved:**
- `robotis_ai_setup/supabase/<NNN>_<name>.sql` — forward migration (numeric ordering, **014 is intentionally skipped**)
- `robotis_ai_setup/supabase/rollback/<NNN>_<name>_rollback.sql` — REQUIRED matching rollback (BEGIN/COMMIT-wrapped, reverse-order DROPs)
- `docs/deploy/APPLY_MIGRATIONS.sql` — bundles round-N migrations for a single paste into Supabase Studio
- `docs/deploy/ROLLBACK_MIGRATIONS.sql` — reverse-order bundle

**How to ship:**

```bash
# (a) Write the migration locally
# Pick next free integer above 017 (next is 018). NEVER reuse 014.
$EDITOR robotis_ai_setup/supabase/018_<name>.sql
$EDITOR robotis_ai_setup/supabase/rollback/018_<name>_rollback.sql

# (b) Test in a Supabase BRANCH database first (never apply to prod first)
# Supabase Studio → Branches → New branch → SQL Editor → paste forward → paste rollback → repeat to verify idempotency

# (c) Apply to prod via Supabase Studio SQL Editor (NOT psql — Supabase has its own pooler)
# Or via the Supabase MCP tool: mcp__claude_ai_Supabase__apply_migration
```

**Required safety:**
- `SECURITY DEFINER` functions MUST end with `REVOKE EXECUTE FROM PUBLIC, anon, authenticated; GRANT EXECUTE TO service_role;` — otherwise the migration 013 hole reopens
- Forward and rollback MUST be wrapped in `BEGIN; … COMMIT;`
- Use `IF NOT EXISTS` / `IF EXISTS` for idempotency
- If you add a new RPC the Cloud API will call, add a probe for it in `cloud_training_api/app/main.py:_validate_required_schema()` — otherwise the schema fingerprint silently misses it on next deploy
- If you add a table the React app subscribes to via Realtime, add `ALTER PUBLICATION supabase_realtime ADD TABLE …` in the migration

---

### 2. Modal apps (training + vision)

**Two separate apps, each with its OWN secret bundle.** Mixing the secrets is a security regression — keep them split.

| App | Module | Secret bundle |
|---|---|---|
| `edubotics-training` | `modal_training/modal_app.py` | `edubotics-training-secrets` (SUPABASE_URL, SUPABASE_ANON_KEY, HF_TOKEN) |
| `edubotics-vision` | `modal_training/vision_app.py` | `edubotics-vision-secrets` (HF cache only — no Supabase creds, no service role) |

**How to ship:**

```bash
cd /Users/svenborodun/Documents/EduBotics/Testre/robotis_ai_setup/modal_training

# Sanity-check imports BEFORE deploying (catches API mismatch like c56c012)
modal run -m modal_app::smoke_test       # training app: prints torch ver, CUDA ok
modal run -m vision_app::smoke_test       # vision app: confirms transformers + hf_hub

# Deploy
modal deploy modal_app.py                 # training
modal deploy vision_app.py                # vision

# Verify the app is live
modal app list | grep edubotics
```

**Important caveats:**
- `vision_app.py` uses `enable_memory_snapshot=True` + the dual-`@modal.enter` pattern (`snap=True` CPU load, `snap=False` GPU bind). Don't break that — the cold-start economics depend on it.
- LeRobot SHA in `modal_app.py:19` is one of the 5 pinning sites (§1.5). Bumping it without bumping the other 4 places breaks recording-vs-training compatibility.
- Modal will rebuild the image on first deploy after a `pip_install` change. That's slow (~5-10 min). After that, it caches.
- `min_containers=0` + `scaledown_window=180` means classrooms pay nothing when idle. Don't set `min_containers=1` to "warm" things — the snapshot path is faster than keeping a container alive.

**Modal secrets management:**

```bash
# Create OR update the training secret bundle (run once or when keys rotate)
modal secret create edubotics-training-secrets \
  SUPABASE_URL='...' SUPABASE_ANON_KEY='...' HF_TOKEN='...'

# Create vision secret bundle (separate — explicitly intentional)
modal secret create edubotics-vision-secrets HF_TOKEN='...'
```

---

### 3. Railway (Cloud API)

Railway is typically auto-deployed from your GitHub `main` branch. The Dockerfile at `robotis_ai_setup/cloud_training_api/Dockerfile` builds the FastAPI image with `CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}`.

**Crucial: single worker.** The in-process rate limiter (main.py middleware) assumes one process. If you raise `--workers`, students bypass rate limits.

**How to ship:**

```bash
# (a) Make code changes in cloud_training_api/app/
$EDITOR robotis_ai_setup/cloud_training_api/app/routes/<file>.py

# (b) Update _validate_required_schema() if you added a new RPC/table
$EDITOR robotis_ai_setup/cloud_training_api/app/main.py

# (c) Test locally first
cd robotis_ai_setup/cloud_training_api
pip install -r requirements.txt
SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... MODAL_TOKEN_ID=... MODAL_TOKEN_SECRET=... \
  uvicorn app.main:app --reload --port 8000

# (d) curl smoke
curl http://localhost:8000/health     # must be 200
curl http://localhost:8000/me         # must be 401

# (e) Push to GitHub → Railway auto-deploys
git add robotis_ai_setup/cloud_training_api/
git commit -m "Cloud API: <what changed>"
git push

# (f) Watch Railway build logs for schema fingerprint
# In Railway dashboard → service → Logs → look for "Validating required Supabase schema..."
# If "EDUBOTICS_SCHEMA_CHECK FAILED" — Supabase migration didn't land (go back to step 1)
```

**Environment variables (set in Railway dashboard, NOT in repo):**

Required: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`

Optional but production-relevant:
- `ALLOWED_ORIGINS` — comma-separated; rejects `*` with credentials
- `GUI_VERSION` + `GUI_DOWNLOAD_URL` — without these, `/version` returns 503 and the GUI auto-update gate is silently disabled
- `HF_TOKEN` — enables `_delete_student_hf_artifacts` (GDPR) + the dataset sweep reconciliation loop
- `DATASET_SWEEP_INTERVAL_S` — default 600s; set higher if HF API rate-limits bite
- `MAX_TRAINING_TIMEOUT_HOURS` — default 12h; lower for stricter classroom budgets
- `POLICY_TIMEOUT_OVERRIDES_JSON` — per-policy timeout overrides (rare)

**Never** set `EDUBOTICS_SKIP_SCHEMA_CHECK=1` on Railway — that's a unit-test-only escape hatch and shipping with it = the c56c012 incident reopens.

**Railway gotcha:** Railway service variables don't auto-feed Dockerfile `ARG`s for the `cloud_training_api/Dockerfile`. That's fine for this image because there are no `ARG`s — all config is via runtime ENV. The Dockerfile.web for the React teacher-web build is a different story (see §4 below).

---

### 4. Docker Hub student images (`nettername/{open-manipulator, physical-ai-server, physical-ai-manager}:latest`)

**Three images, one build script:** `robotis_ai_setup/docker/build-images.sh`.

**How to ship:**

```bash
cd /Users/svenborodun/Documents/EduBotics/Testre/robotis_ai_setup/docker

# Build secrets MUST be set or build aborts fail-loudly:
export SUPABASE_URL='https://fnnbysrjkfugsqzwcksd.supabase.co'
export SUPABASE_ANON_KEY='eyJ...'           # the anon key, NOT service-role
export CLOUD_API_URL='https://scintillating-empathy-production-9efd.up.railway.app'

# Optional knobs:
export ALLOWED_POLICIES='act'                # default — student gets ACT only
export REGISTRY='nettername'                 # change for forks
# export BUILD_BASE=1                        # set ONLY if you want to rebuild open-manipulator base (~40 min)

# Build + push all 3
./build-images.sh
```

**What `build-images.sh` does, in order:**
1. Fail-fast on missing env vars
2. Compute `BUILD_ID = ${BUILD_TS}-${BUILD_SHA}` (UTC timestamp + 7-char git SHA)
3. Stage `_coco_classes.py` from physical_ai_server overlays into the manager build context (for the Jest sync test)
4. Build `physical-ai-server` with overlay + patch + workflow + interfaces rebuild + YOLOX download (sha256-pinned)
5. Build `open-manipulator` with 5 overlays + entrypoint + identify_arm
6. Build `physical-ai-manager` with the 3 secrets baked in
7. **Post-build smoke**: grep `main.*.js` for the literal Supabase URL and Cloud API URL — aborts if missing (white-screen prevention)
8. Push all 3 to Docker Hub (verifies success per image)
9. Clean staged files

**Selective rebuild (faster):** if you only changed one image's source, rebuild just that one manually:

```bash
# Manager only (React change) — fast (~1-2 min)
docker build -t nettername/physical-ai-manager:latest \
  --build-arg REACT_APP_SUPABASE_URL="$SUPABASE_URL" \
  --build-arg REACT_APP_SUPABASE_ANON_KEY="$SUPABASE_ANON_KEY" \
  --build-arg REACT_APP_CLOUD_API_URL="$CLOUD_API_URL" \
  --build-arg REACT_APP_BUILD_ID="$(date -u +%Y%m%d-%H%M%S)-$(git rev-parse --short HEAD)" \
  ../../physical_ai_tools/physical_ai_manager
docker push nettername/physical-ai-manager:latest

# Server only (overlay change) — medium (~5-10 min)
docker build -t nettername/physical-ai-server:latest \
  -f physical_ai_server/Dockerfile \
  --build-arg ALLOWED_POLICIES="$ALLOWED_POLICIES" \
  .
docker push nettername/physical-ai-server:latest

# Manipulator only (overlay/entrypoint change) — fast (~30 s)
docker build -t nettername/open-manipulator:latest -f open_manipulator/Dockerfile open_manipulator/
docker push nettername/open-manipulator:latest
```

**Always-after-push step:**
- Students need to `docker pull` to get the new image. The GUI does this automatically on next launch (via `gui/app/docker_manager._pull_one_image()`). For a forced update, **bump the `/version` `required:true` payload on Railway** so the GUI's auto-update gate kicks in.

**Railway teacher-web (separate image, separate flow):**

```bash
# physical_ai_manager/Dockerfile.web is for the Railway teacher/admin dashboard
# Use the wrapper, NOT bare `railway up`:
cd /Users/svenborodun/Documents/EduBotics/Testre/physical_ai_tools/physical_ai_manager
./scripts/railway-deploy.sh
```

This stages `_coco_classes.py`, runs `railway up --path-as-root .`, and cleans up. Without the wrapper, the prebuild Jest test fails because `_coco_classes.py` isn't in the build context.

---

## Scenario-based recipes

### A. Pure React change (UI tweak, new component, CSS, bug fix)

```
NEEDS: Manager image rebuild + push only.
ORDER: just step 4 (manager-only build)
PROPAGATION: students get it on next GUI restart (useVersionCheck polls /version.json every 30s and self-reloads)
```

### B. Overlay change (safety envelope, recording, inference)

```
NEEDS: physical-ai-server image rebuild + push.
ORDER: just step 4 (server-only build). The apply_overlay() sha256 check will FAIL the build if your overlay no longer matches its upstream target — that's the safety net.
PROPAGATION: students get it on next `docker pull` (GUI checks on launch).
```

### C. Open-manipulator change (entrypoint, xacro, controller)

```
NEEDS: open-manipulator image rebuild + push.
ORDER: just step 4 (manipulator-only build).
PROPAGATION: same as B.
```

### D. New Supabase column or RPC

```
NEEDS: Supabase migration → schema fingerprint update → Cloud API change → Railway redeploy
ORDER:
  1. Write 018_<name>.sql + rollback
  2. Apply to Supabase (Studio SQL editor)
  3. Add probe in cloud_training_api/app/main.py:_validate_required_schema()
  4. Write the route that uses it in cloud_training_api/app/routes/
  5. Test locally
  6. Push to GitHub → Railway auto-deploys
  7. If React calls the new endpoint: rebuild manager image (step 4 of golden rule)
PROPAGATION: same-day via Railway (~2 min auto-deploy)
```

### E. New Modal feature (e.g. add a new vision model variant)

```
NEEDS: Modal redeploy → Cloud API change (if a new route) → Railway redeploy
ORDER:
  1. Edit modal_training/vision_app.py (or modal_app.py)
  2. `modal run smoke_test` to verify imports
  3. `modal deploy vision_app.py`
  4. If Cloud API calls it differently: edit routes/vision.py, push, Railway auto-deploys
PROPAGATION: instant for the Modal call; Railway change picks up on auto-deploy
```

### F. LeRobot version bump (rarest, riskiest)

```
NEEDS: 5-place sync per CLAUDE.md §1.5 + §13.2, in one PR:
  1. physical_ai_tools/lerobot/ — replace snapshot with byte-identical new SHA
  2. modal_training/modal_app.py:19 — bump LEROBOT_COMMIT
  3. Verify base image robotis/physical-ai-server:amd64-X.Y.Z was rebuilt against this SHA (operator-side check)
  4. meta/info.json codebase_version — if upstream bumped, write a dataset migration script
  5. modal_training/training_handler.py — bump EXPECTED_CODEBASE_VERSION

ORDER for the rollout:
  1. Land all 5 file changes in one git commit
  2. Modal redeploy (vision_app + modal_app)
  3. Smoke training on a tiny ACT dataset
  4. Rebuild + push server image (step 4 of golden rule)
  5. Local recording smoke
PROPAGATION: students hit the new contract on next pull; old datasets need migration if codebase_version bumped
```

### G. "I changed everything in one session" (multi-layer)

```
Run the FULL golden-rule sequence:
  1. Supabase migrations applied (Studio paste)
  2. modal deploy modal_app.py && modal deploy vision_app.py
  3. git push → Railway auto-deploys; watch /health for 200
  4. cd robotis_ai_setup/docker && ./build-images.sh
  5. Optionally: bump GUI_VERSION on Railway to force student auto-update
```

---

## Pre-flight safety checklist (before EVERY ship)

| Check | Why |
|---|---|
| `git status` clean OR everything staged | Avoids shipping uncommitted experiment code |
| `git log --oneline -1` matches what you expect | The buildId bakes the SHA — wrong commit = wrong buildId |
| Local tests pass (`cd robotis_ai_setup && python -m unittest discover -s tests`) | Catches GUI regressions |
| Cloud API local boot succeeds (`uvicorn app.main:app`) | Catches schema fingerprint failures before Railway sees them |
| If touching overlays: re-read the upstream file the overlay targets | Catches "ROBOTIS renamed the function" silent breakage |
| If touching Modal: `modal run smoke_test` | Catches SDK API drift (c56c012-class) |
| If touching SECURITY DEFINER RPC: confirm `REVOKE FROM PUBLIC` is in the migration | 013 hole |
| If shipping CRITICAL safety fix: bump VERSION + GUI_VERSION on Railway to force update | Otherwise students keep running the old image until they happen to restart |

---

## Rollback strategy per target

| If… | Rollback |
|---|---|
| Bad Supabase migration | `psql -f rollback/<NNN>_<name>_rollback.sql` via Supabase Studio. Migrations are BEGIN/COMMIT wrapped → atomic |
| Bad Modal deploy | `modal deploy <previous_commit>/modal_app.py` (git checkout the prior file first); or simply re-run `modal deploy` from a prior git ref |
| Bad Railway deploy | Railway dashboard → service → Deployments → click the prior green deploy → "Redeploy" |
| Bad Docker image | `docker pull nettername/<image>:<prior_buildid>` (if you tagged); otherwise `git checkout <prior-sha> && ./build-images.sh` |
| Combined cascade failure | Stop at the highest layer first (Docker → Railway → Modal → Supabase). The schema fingerprint actively prevents Railway from booting against a half-rolled-back DB, so Supabase rollback last |

**Tag versions on Docker Hub when shipping major changes** so rollback is `docker tag` away:
```bash
TAG="$(date -u +%Y%m%d)-$(git rev-parse --short HEAD)"
docker tag nettername/physical-ai-server:latest nettername/physical-ai-server:$TAG
docker push nettername/physical-ai-server:$TAG
```

This is what the dead `IMAGE_TAG` machinery in `gui/app/constants.py` + `pull_images.ps1` was designed for. You'd need to also restore `${IMAGE_TAG:-latest}` substitution in `docker-compose.yml:8,57,111` to actually use the tag at student-side (currently it's hardcoded to `:latest` — see L1 H-1 audit finding).

---

## TL;DR cheat sheet (pin this somewhere)

```
React UI change           → build manager image only
Overlay / recording / inf → build server image only
Entrypoint / xacro        → build manipulator image only
Workflow blocks / Workshop→ build server + manager + (maybe) write 018_*.sql
New SUPABASE table/RPC    → migrate Supabase → update _validate_required_schema → push (Railway auto-deploys)
New Modal feature         → modal deploy → push (Railway auto-deploys)
LeRobot bump              → 5-place sync, ONE PR
Everything                → Supabase → Modal → Railway → Docker → git push
```

**The single most important rule:** never ship a route to Railway before its Supabase migration is applied. The schema fingerprint will reject the deploy, but it's a noisy 2-minute fail you can avoid by sequencing.

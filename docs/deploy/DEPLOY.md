# EduBotics — Deploy

One-page reference. Pair with `APPLY_MIGRATIONS.sql` / `ROLLBACK_MIGRATIONS.sql` in this folder.

## The golden order (never reorder)

```
1. Supabase migrations     ← schema fingerprint in Railway gates on this
2. Modal apps              ← Railway will 503 on /vision/detect without this
3. Railway (Cloud API)     ← auto-deploys on git push to main
4. Docker Hub images       ← students pull on next GUI launch
5. git push                ← CI runs guardrails (10 jobs)
```

Skip step 1 → Railway boot fails. Skip step 2 → `POST /vision/detect` → 503. Push images before Railway → student calls hit routes that don't exist yet.

---

## 1. Supabase — Postgres migrations

**When:** new table, column, RPC, RLS policy, trigger, or Realtime publication.

```bash
# Write the pair (next free number is 019 — never reuse 014)
$EDITOR robotis_ai_setup/supabase/019_<name>.sql
$EDITOR robotis_ai_setup/supabase/rollback/019_<name>_rollback.sql
```

**Rules:**
- Forward + rollback wrapped in `BEGIN; … COMMIT;`, use `IF [NOT] EXISTS`.
- Every `SECURITY DEFINER` function ends with `REVOKE EXECUTE FROM PUBLIC, anon, authenticated; GRANT EXECUTE TO service_role;` (the 013 hole).
- If the Cloud API will call it, add a probe in `cloud_training_api/app/main.py:_validate_required_schema()`.
- If React subscribes via Realtime: `ALTER PUBLICATION supabase_realtime ADD TABLE …`.

**Apply (Supabase Studio → SQL Editor):** paste the migration file, Run. For multi-migration rollouts paste `docs/deploy/APPLY_MIGRATIONS.sql`. Verify the new tables/RPCs in Database view.

**Rollback:** paste the matching `rollback/*.sql`. Roll Railway back BEFORE rolling Supabase back.

---

## 2. Modal — training + vision apps

Two apps, **two separate secret bundles** — never share them.

| App | Module | Secret bundle | Contents |
|---|---|---|---|
| `edubotics-training` | `modal_training/modal_app.py` | `edubotics-training-secrets` | `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `HF_TOKEN` (write) |
| `edubotics-vision` | `modal_training/vision_app.py` | `edubotics-vision-secrets` | `HF_TOKEN` (read-only) — no Supabase creds |

```bash
cd robotis_ai_setup/modal_training

# One-time, or when keys rotate:
modal secret create edubotics-training-secrets SUPABASE_URL=… SUPABASE_ANON_KEY=… HF_TOKEN=…
modal secret create edubotics-vision-secrets HF_TOKEN=hf_<read_token>

# Sanity-check imports BEFORE deploy (catches Modal SDK API drift):
modal run -m modal_app::smoke_test     # expect torch=2.x+cu121, cuda_available=true
modal run -m vision_app::smoke_test    # expect cuda_available=true

modal deploy modal_app.py
modal deploy vision_app.py

modal app list | grep edubotics
```

**Don't break:**
- LeRobot SHA in `modal_app.py:19` is one of 5 pinning sites — see CLAUDE.md §1.5 / §13.2.
- `vision_app.py` uses `enable_memory_snapshot=True` with dual `@modal.enter(snap=True/False)`. The cold-start economics depend on it.
- `min_containers=0` is correct. Don't "warm" by setting it to 1.

---

## 3. Railway — Cloud API (auto-deploys on git push)

`robotis_ai_setup/cloud_training_api/` ships via Railway from `main`. **Single worker required** (in-process rate limiter).

```bash
# Local smoke before pushing:
cd robotis_ai_setup/cloud_training_api
pip install -r requirements.txt
SUPABASE_URL=… SUPABASE_SERVICE_ROLE_KEY=… MODAL_TOKEN_ID=… MODAL_TOKEN_SECRET=… \
  uvicorn app.main:app --reload --port 8000
curl http://localhost:8000/health   # 200
curl http://localhost:8000/me       # 401

# Push triggers Railway auto-deploy:
git push
# Watch Railway logs for "Validating required Supabase schema…"
# If "EDUBOTICS_SCHEMA_CHECK FAILED" → step 1 didn't land
```

**Required env vars** (Railway dashboard): `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`.

**Production-relevant optional:** `ALLOWED_ORIGINS`, `GUI_VERSION` + `GUI_DOWNLOAD_URL` (drives student `.exe` auto-update — bump these to force re-install), `HF_TOKEN` (GDPR + dataset sweep), `MAX_TRAINING_TIMEOUT_HOURS`, `STALLED_WORKER_MINUTES`, `MODAL_VISION_*`, `VISION_MODAL_TIMEOUT_S`, `EDUBOTICS_JETSON_HF_TOKEN` (read-only HF token returned to Jetson agents at `/jetson/register` — REQUIRED if any classroom has a paired Jetson; otherwise `/jetson/register` returns 503).

**Never** set `EDUBOTICS_SKIP_SCHEMA_CHECK=1` on Railway.

**Teacher-web React build** (separate Railway service):
```bash
cd physical_ai_tools/physical_ai_manager
./scripts/railway-deploy.sh   # stages _coco_classes.py + railway up --path-as-root .
```
Never use bare `railway up` — the prebuild Jest hook needs the staged file.

---

## 4. Docker Hub — 3 student images (`nettername/*:latest`)

**Always use `docker buildx build --platform linux/amd64 --push`.** On Docker Desktop (macOS/Windows), plain `docker build` + `docker push` reads from two different image stores and silently pushes a stale image. `buildx --push` writes straight to the registry and bypasses the daemon — this is the one method that works everywhere.

### Setup (once)

```bash
docker login                                                    # nettername account
docker buildx create --name edubotics --use --bootstrap         # named amd64 builder

cd /Users/svenborodun/Documents/EduBotics/Testre/robotis_ai_setup/docker

export SUPABASE_URL='https://<project-ref>.supabase.co'
export SUPABASE_ANON_KEY='<anon key — NOT service-role>'
export CLOUD_API_URL='https://scintillating-empathy-production-1068.up.railway.app'
export REGISTRY='nettername'
export ALLOWED_POLICIES='act'
export BUILD_ID="$(date -u +%Y%m%d-%H%M%S)-$(git -C .. rev-parse --short HEAD)"
export PHYSICAL_AI_TOOLS_DIR="$(cd ../../physical_ai_tools && pwd)"
```

### Build + push — `physical-ai-manager` (React)

```bash
# Stage coco_classes for the prebuild Jest hook
cp "$PHYSICAL_AI_TOOLS_DIR/physical_ai_server/physical_ai_server/workflow/coco_classes.py" \
   "$PHYSICAL_AI_TOOLS_DIR/physical_ai_manager/_coco_classes.py"

docker buildx build --platform linux/amd64 --push \
  --build-arg REACT_APP_SUPABASE_URL="$SUPABASE_URL" \
  --build-arg REACT_APP_SUPABASE_ANON_KEY="$SUPABASE_ANON_KEY" \
  --build-arg REACT_APP_CLOUD_API_URL="$CLOUD_API_URL" \
  --build-arg REACT_APP_ALLOWED_POLICIES="$ALLOWED_POLICIES" \
  --build-arg REACT_APP_BUILD_ID="$BUILD_ID" \
  -t "$REGISTRY/physical-ai-manager:latest" \
  -t "$REGISTRY/physical-ai-manager:$BUILD_ID" \
  -f "$PHYSICAL_AI_TOOLS_DIR/physical_ai_manager/Dockerfile" \
  "$PHYSICAL_AI_TOOLS_DIR/physical_ai_manager/"

rm -f "$PHYSICAL_AI_TOOLS_DIR/physical_ai_manager/_coco_classes.py"
```

### Build + push — `physical-ai-server` (ROS2 + overlays)

```bash
# Stage interfaces source for in-image rebuild
STAGE="physical_ai_server/interfaces"
rm -rf "$STAGE" && mkdir -p "$STAGE/msg" "$STAGE/srv"
cp "$PHYSICAL_AI_TOOLS_DIR/physical_ai_interfaces/CMakeLists.txt" "$STAGE/"
cp "$PHYSICAL_AI_TOOLS_DIR/physical_ai_interfaces/package.xml"    "$STAGE/"
cp "$PHYSICAL_AI_TOOLS_DIR/physical_ai_interfaces/msg/"*.msg      "$STAGE/msg/"
cp "$PHYSICAL_AI_TOOLS_DIR/physical_ai_interfaces/srv/"*.srv      "$STAGE/srv/"

docker buildx build --platform linux/amd64 --push \
  -t "$REGISTRY/physical-ai-server:latest" \
  -t "$REGISTRY/physical-ai-server:$BUILD_ID" \
  -f physical_ai_server/Dockerfile \
  physical_ai_server/

rm -rf "$STAGE"
```

### Build + push — `open-manipulator` (entrypoint, xacro)

```bash
docker buildx build --platform linux/amd64 --push \
  -t "$REGISTRY/open-manipulator:latest" \
  -t "$REGISTRY/open-manipulator:$BUILD_ID" \
  -f open_manipulator/Dockerfile \
  open_manipulator/
```

### Mandatory post-push verification

`buildx --push` doesn't load the image locally, so verify by pulling back from the registry:

```bash
docker pull --platform linux/amd64 "$REGISTRY/physical-ai-server:latest"
docker run --rm --platform linux/amd64 --entrypoint bash \
  "$REGISTRY/physical-ai-server:latest" \
  -c "grep -rn 'Audit F66' /root/ros2_ws | head"     # expect non-zero
```

Also check the React bundle has the secrets baked in (white-screen guard):

```bash
docker pull --platform linux/amd64 "$REGISTRY/physical-ai-manager:latest"
docker run --rm --platform linux/amd64 --entrypoint sh \
  "$REGISTRY/physical-ai-manager:latest" \
  -c "grep -q -F '$SUPABASE_URL' /usr/share/nginx/html/static/js/main.*.js && echo OK"
```

### Linux-server path (alternative)

`./build-images.sh` does all of the above in one shot but uses plain `docker build` + `docker push` — fine on a real Linux box with one image store, **not safe on Docker Desktop**. Run the script only if you're on Linux with the Docker Engine daemon (no Desktop).

### Selective rebuilds

| Changed | Rebuild only |
|---|---|
| React (UI, components, CSS) | `physical-ai-manager` |
| Overlay (inference, recording, workflow) | `physical-ai-server` |
| Entrypoint / xacro / controller / `identify_arm.py` | `open-manipulator` |
| Workflow blocks (Workshop) | server + manager |

### Student propagation

GUI 2.2.4 auto-pulls on every launch: TCP probe to Docker Hub (5 s offline-skip) → manifest-digest pre-check → only pulls if remote ≠ local → persists last-pull timestamp to `%LOCALAPPDATA%/EduBotics/.last_image_pull.json`. Banner past `IMAGE_FRESHNESS_WARN_DAYS=14`. Disable with `EDUBOTICS_SKIP_AUTO_PULL=1`.

**To force re-install** of the `.exe` itself: bump `VERSION` + `installer/robotis_ai_setup.iss AppVersion` + `gui/app/constants.py` fallback together, build the new `.exe`, upload, then set Railway `GUI_VERSION` + `GUI_DOWNLOAD_URL`.

---

## 4b. Classroom Jetson (v2.3.0+) — arm64 image set + agent install

Separate ship path because the audience is different (per-classroom teachers) and the scope is narrow (Inference tab only). See [`docs/JETSON_DEPLOY.md`](../JETSON_DEPLOY.md) for the full runbook.

### Maintainer side — push arm64 images once per release

```bash
# From a maintainer host with buildx + Docker Hub login.
docker run --privileged --rm tonistiigi/binfmt --install arm64   # one-time, Mac only
docker buildx create --name edubotics-arm64 --use --bootstrap

# First time: build the arm64 bases (~30-40 min QEMU, ~10 min native arm64).
BUILD_BASE_ARM64=1 PLATFORM=arm64 ./robotis_ai_setup/docker/build-images.sh

# Subsequent releases: pulls existing bases, rebuilds the thin overlays.
PLATFORM=arm64 ./robotis_ai_setup/docker/build-images.sh
```

Pushed tags (separate repos from amd64 — see `docs/arm64_base/README.md`
for why):
- `nettername/open-manipulator-jetson:latest`
- `nettername/physical-ai-server-jetson:latest`
- `nettername/open-manipulator-jetson-base:4.1.4` (one-time base)
- `nettername/physical-ai-server-jetson-base:0.8.2` (one-time base)

`physical-ai-manager` is NOT built for arm64 — the React app stays on the student PC.

### Teacher side — install the agent + pair

```bash
# On the Jetson with JetPack 6:
sudo bash /path/to/robotis_ai_setup/jetson_agent/setup.sh
```

Script registers with the Cloud API, prints a 6-digit pairing code. Teacher enters the code in the admin dashboard → classroom gets bound. See [`docs/JETSON_DEPLOY.md`](../JETSON_DEPLOY.md) for the full walkthrough.

### Required Railway env vars

- `EDUBOTICS_JETSON_HF_TOKEN` — read-only HF token, EduBotics-Solutions/* scope. Returned to agents at `/jetson/register`. Without it, `POST /jetson/register` returns 503 and the setup script aborts.
- `SUPABASE_JWT_ALGORITHM` — `RS256` (modern, default) or `HS256` (legacy). v2.3.0 the Cloud API forwards this to the agent at register time so the rosbridge proxy picks the right JWT verification path.
- `SUPABASE_JWT_SECRET` — **required only when `SUPABASE_JWT_ALGORITHM=HS256`**. The symmetric secret from Supabase Dashboard → Settings → API → JWT Secret. Forwarded to the agent at register time and written to `/etc/edubotics/jetson.env` mode 600. Without it, `POST /jetson/register` returns 503.

---

## 5. git push

CI runs 11 jobs: `python-tests` (now includes jetson agent + jetson route tests), `shell-lint` (now includes `setup.sh`), `compose-validate` (now also validates `docker-compose.jetson.yml`), `overlay-guard`, `modal-import-validate`, `teacher-web-build-validate`, `manager-build-validate`, `tutorials-validate`, `interfaces-validate`, `nginx-validate`, `german-strings-lint`. Most catch a class of regression you'd otherwise discover in production.

---

## Scenario recipes (what to deploy when X changes)

| Change | Deploy |
|---|---|
| React only | rebuild `physical-ai-manager` |
| Server overlay (inference, recording, workflow) | rebuild `physical-ai-server` |
| `open-manipulator` (entrypoint, xacro) | rebuild `open-manipulator` |
| New Supabase column/RPC | migrate → update `_validate_required_schema()` → route → push → (rebuild manager if React calls it) |
| New Modal function/model | `modal deploy` → route change in `routes/` → push |
| New workflow block | migration (if persisted) → server + manager rebuild |
| LeRobot bump | 5-place sync in one PR (CLAUDE.md §1.5 / §13.2) → Modal redeploy → server rebuild → smoke train + record |
| All-of-the-above session | Run the full golden order |

---

## Pre-flight (every ship)

- `git status` clean or fully staged; `git log -1` is the commit you mean
- Local tests: `cd robotis_ai_setup && python -m unittest discover -s tests`
- Cloud API boots locally (`uvicorn app.main:app`)
- Touching an overlay? Re-read the upstream file it targets
- Touching Modal? `modal run smoke_test`
- New SECURITY DEFINER RPC? Migration has `REVOKE FROM PUBLIC`
- CRITICAL safety fix? Bump VERSION + Railway `GUI_VERSION` to force update

---

## Smoke test (post-deploy, easiest first)

```bash
TOKEN=…  # student JWT
URL=https://scintillating-empathy-production-1068.up.railway.app

curl "$URL/health"                                     # {"status":"ok"}
curl -H "Authorization: Bearer $TOKEN" "$URL/me"       # 200 + profile
curl -H "Authorization: Bearer $TOKEN" "$URL/me/tutorial-progress"   # 200 [] for fresh user
# Cloud-vision (1×1 PNG)
IMG=iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYGBgAAAABQABh6FO1AAAAABJRU5ErkJggg==
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{\"image_b64\":\"$IMG\",\"prompts\":[\"rote Tasse\"]}" "$URL/vision/detect"
# First call: cold_start: true, 5-30 s. 503 → migration 017 missing.

# Image digests changed:
docker buildx imagetools inspect nettername/physical-ai-manager:latest
docker buildx imagetools inspect nettername/physical-ai-server:latest
docker buildx imagetools inspect nettername/open-manipulator:latest
```

Then launch the GUI on a Windows box with a robot: toolbar renders, Galerie populates, Lernpfad lists tutorials, Cloud-Erkennung checkbox visible, breakpoint pause/continue works, Sensoren panel updates at ~5 Hz.

---

## Rollback (reverse order)

1. **Railway** — Dashboard → Deployments → click prior green → Redeploy
2. **Supabase** — paste `docs/deploy/ROLLBACK_MIGRATIONS.sql` (or single rollback file) in Studio
3. **Modal** — `git checkout <prior-sha> -- modal_training/<app>.py && modal deploy <app>.py`
4. **Docker** — re-tag prior `BUILD_ID` as `:latest`: `docker tag nettername/<image>:<prior_id> nettername/<image>:latest && docker push nettername/<image>:latest`

The schema fingerprint prevents Railway booting against a half-rolled-back DB — that's why Railway rolls back first.

---

## The one rule

**Never ship a Cloud API route before its Supabase migration is applied.** The fingerprint will reject the deploy, but sequencing avoids the noisy 2-minute fail loop.

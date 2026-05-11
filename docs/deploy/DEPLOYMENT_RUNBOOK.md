# Roboter Studio upgrade — deployment runbook

> Status from the automated session (2026-05-10):
>
> | Step | Status | Verified |
> |---|---|---|
> | Modal `edubotics-vision` app | **DEPLOYED** | `https://modal.com/apps/svendanilborodun/main/deployed/edubotics-vision` |
> | Modal `edubotics-vision-secrets` secret | **CREATED** (contains `HF_TOKEN`) | `modal secret list` |
> | Supabase migrations 015 / 016 / 017 | **PENDING** — apply BEFORE Railway revision goes live | Dashboard SQL Editor |
> | Railway cloud_training_api | **PENDING** — re-deploy AFTER migrations succeed | Build log link printed by `railway up` |
> | 3 Docker images (`open_manipulator`, `physical_ai_server`, `physical_ai_manager`) | **PENDING** — Docker daemon was off in the session | Local docker build + push |
> | Git push of code changes | **PENDING** | `git push origin main` |
>
> Three remaining steps below. Apply in the listed order — see **§0** for why
> ordering matters.

---

## 0. Critical ordering (read first)

The new Railway routes (`/me/tutorial-progress`, `/vision/detect`,
`/workflows/{id}/versions`) reference tables and RPCs created by
migrations 015 / 016 / 017. If you redeploy the Railway code BEFORE
those migrations land, live traffic returns `500 relation
"tutorial_progress" does not exist` and `503 consume_vision_quota RPC
unavailable` until the migrations are applied.

The safe order:

1. **Apply Supabase migrations** (§1 below).
2. **Re-deploy Railway** (`railway up`).
3. **Build + push Docker images** (§2).
4. **Push the git ref** (§3).

If you must roll back: roll back **Railway first** to the previous
revision, then run `docs/deploy/ROLLBACK_MIGRATIONS.sql`. Rolling back
the migrations while the new Railway revision is still live produces
the same 500/503 storm.

---

## 1. Apply the Supabase migrations

A single combined SQL file is at `docs/deploy/APPLY_MIGRATIONS.sql`. It
prepends `CREATE EXTENSION IF NOT EXISTS pgcrypto;` (defensive no-op on
managed Supabase, required for self-hosted Postgres) and then wraps
`015_workflow_versions.sql`, `016_tutorial_progress.sql`, and
`017_vision_quota.sql` in their own BEGIN/COMMIT blocks (so a single
mistake rolls back its own block without touching the others).

**Two options.**

### Option A — Supabase Dashboard SQL Editor (recommended, ~30 s)

1. Open `https://supabase.com/dashboard/project/<project-ref>/sql/new`.
2. Paste the entire contents of `docs/deploy/APPLY_MIGRATIONS.sql`.
3. Press **Run**.
4. Verify in the Dashboard's "Database" view:
   - Table `workflow_versions` exists.
   - Table `tutorial_progress` exists.
   - Functions `prune_workflow_versions`, `snapshot_workflow_version`,
     `consume_vision_quota`, `refund_vision_quota`,
     `reset_vision_quota_used` are listed under "Functions".
   - Columns `vision_quota_per_term`, `vision_used_per_term` appear on
     `public.users`.
   - `pg_publication_tables` shows both `workflow_versions` and
     `tutorial_progress` joined to `supabase_realtime`.

### Option B — Supabase CLI (if you have the DB password)

```bash
# From the repo root:
export SUPABASE_DB_URL='postgresql://postgres:<DB_PASSWORD>@db.<PROJECT_REF>.supabase.co:5432/postgres'
psql "$SUPABASE_DB_URL" -f docs/deploy/APPLY_MIGRATIONS.sql
```

Or with the CLI (requires `supabase link` first):

```bash
supabase db push --linked
```

### Rollback (if needed)

A combined rollback bundle is at `docs/deploy/ROLLBACK_MIGRATIONS.sql`
— it applies the three rollback files in reverse order:

```bash
psql "$SUPABASE_DB_URL" -f docs/deploy/ROLLBACK_MIGRATIONS.sql
```

Or pick a single migration to undo:

```bash
psql "$SUPABASE_DB_URL" -f robotis_ai_setup/supabase/rollback/017_vision_quota_rollback.sql
psql "$SUPABASE_DB_URL" -f robotis_ai_setup/supabase/rollback/016_tutorial_progress_rollback.sql
psql "$SUPABASE_DB_URL" -f robotis_ai_setup/supabase/rollback/015_workflow_versions_rollback.sql
```

**Reminder**: roll back the Railway revision FIRST (the route code
queries these tables/RPCs).

---

## 2. Build + push the 3 Docker images

This is what makes the new code live for students when they re-launch the
GUI (the GUI pulls `nettername/*:latest` on every Docker Compose start).

### Prerequisites

- Any Docker daemon (Docker Engine in WSL, Linux Docker Engine, or
  Docker Desktop on a maintainer machine). The product itself
  explicitly avoids Docker Desktop (see CLAUDE.md §5.1) — but for
  building images on the maintainer's box, anything that exposes the
  Docker socket works.
- `docker login` against `https://hub.docker.com` using the `nettername`
  account credentials.
- The repo cloned locally (with all the new code from this upgrade).

### Modal `edubotics-vision-secrets` (one-time)

The vision worker now hard-fails if `edubotics-vision-secrets` is
missing (no silent fallback to `edubotics-training-secrets`, which
leaks the Supabase service-role key). Create it once:

```bash
# Read-only HF token — the worker only reads OWLv2 weights; nothing
# else from this bundle should be set.
modal secret create edubotics-vision-secrets HF_TOKEN=hf_<your_read_token>
modal secret list   # confirm `edubotics-vision-secrets`
```

### Build + push

```bash
cd robotis_ai_setup/docker

# Required env vars (the script fails-loud if any are missing).
# Match the existing Railway / Vercel / Docker-Hub setup.
export SUPABASE_URL='https://<project-ref>.supabase.co'
export SUPABASE_ANON_KEY='<your anon key>'
export CLOUD_API_URL='https://scintillating-empathy-production-9efd.up.railway.app'

# Optional (already have defaults):
# export REGISTRY=nettername
# export ALLOWED_POLICIES=act
# export BUILD_BASE=0   # set 1 only if you actually need to rebuild the
#                       # open_manipulator base image (~40 min)

./build-images.sh
```

The script:
1. Stages `coco_classes.py` into the manager build context (for the
   prebuild Jest sync test).
2. Builds three tags:
   - `nettername/open-manipulator:<BUILD_ID>` + `:latest`
   - `nettername/physical-ai-server:<BUILD_ID>` + `:latest`
   - `nettername/physical-ai-manager:<BUILD_ID>` + `:latest`
3. Runs a post-build smoke-test that greps `main.*.js` for the literal
   Supabase URL / anon key / cloud API URL (catches the white-screen
   regression — CI's `manager-build-validate` job runs the same check).
4. Pushes all six tags to Docker Hub.

### What runs in the new images

- **physical_ai_manager** — the React Workshop UI with autosave,
  ToolbarButtons (undo/redo/save/export/import + theme + Verlauf),
  ColorBlind themes, Block library (events/lists/procedures/sound/math),
  DebugPanel, GalleryTab, SkillmapPlayer + 6 tutorials, cloud-vision
  toggle, IK-pre-check warnings, restricted-toolbox for skillmap steps.
- **physical_ai_server** — the workflow runtime with hat-block scheduler,
  counter-based broadcasts, RLock motion + var locks, IK pre-check,
  pause/step/breakpoint plumbing, `[SPEAK:…]` / `[TONE:…]` /
  `[VAR:…]` sentinels, `edubotics_detect_open_vocab` handler with
  cloud-burst fallback, and the 7 new ROS service callbacks
  (`/workflow/pause|step|continue|set_breakpoints`,
  `/calibration/preview|verify|history`) plus the SensorSnapshot publisher
  at 5 Hz on `/workflow/sensors`. `EDUBOTICS_DETECTOR=dfine-n` is
  currently rejected at startup (see ROBOTER_STUDIO_DEFERRED.md §7.2).
- **open_manipulator** — unchanged from the previous tag (no code
  changes in the upgrade touched it).

### Student-side rollout

Once the new images are on Docker Hub `:latest`, every GUI start runs
`docker compose pull` (via the entrypoint), so the next student to
re-launch the EduBotics GUI gets the new code automatically. No
installer change is required for this rollout.

### Verify after push

```bash
# Confirm the digest changed for each image
docker buildx imagetools inspect nettername/physical-ai-manager:latest
docker buildx imagetools inspect nettername/physical-ai-server:latest
docker buildx imagetools inspect nettername/open-manipulator:latest

# Spot-check: the manager image should have the new Workshop folder
# and the tutorials JSON. Override the entrypoint (nginx) so `ls`
# actually runs and the container exits.
docker run --rm --entrypoint sh nettername/physical-ai-manager:latest \
  -c 'ls /usr/share/nginx/html/tutorials/'
```

---

## 3. Git push of source changes

```bash
cd <repo root>
git status        # confirm the upgrade files staged
git push origin main
```

CI will run on push (8 jobs total — the 6 long-standing plus the two
new ones added in this upgrade): `python-tests`, `shell-lint`,
`compose-validate`, `overlay-guard`, `manager-build-validate`,
`nginx-validate`, `tutorials-validate`, `interfaces-validate`.

---

## 4. Smoke-test checklist (post-deploy)

In order, easiest first:

1. **Railway** — visit `https://scintillating-empathy-production-9efd.up.railway.app/health` and confirm `{"status":"ok"}`.
2. **Railway → DB** — call `GET /me/tutorial-progress` with a valid bearer
   token; expect `200 []` for a fresh user. If you get `500` with a
   `relation "tutorial_progress" does not exist` error, the Supabase
   migrations weren't applied yet — go back to step 1 of this runbook.
3. **Cloud vision** — call `POST /vision/detect` with a tiny base64 JPEG
   and `prompts: ["rote Tasse"]`. Cold start: expect ~5-30 s on first
   call (variance is the first-after-deploy warm-up). Body shape:
   `{detections: [...], elapsed_ms: …, cold_start: true|false}`. A
   concrete invocation:

   ```bash
   # 1×1 transparent PNG, smallest legal payload
   IMG=$(printf 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYGBgAAAABQABh6FO1AAAAABJRU5ErkJggg==')
   curl -sS -X POST \
     "https://scintillating-empathy-production-9efd.up.railway.app/vision/detect" \
     -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d "{\"image_b64\":\"$IMG\",\"prompts\":[\"rote Tasse\"]}"
   ```

   A 503 with `Cloud-Erkennung ist auf diesem Server noch nicht fertig
   konfiguriert` means migration 017 didn't land — step 1 again.

4. **Student GUI** — launch EduBotics on a Windows machine with a robot.
   Open Roboter Studio → confirm the new toolbar (undo, redo, save, …)
   renders, the Galerie tab is reachable, the Lernpfad sidebar lists 6
   tutorials, and the Cloud-Erkennung checkbox appears in RunControls.
5. **Workflow + debugger** — load a tutorial, drag a `Wartezeit 2 s`
   block, set a breakpoint via Alt+click, press Start → workflow pauses
   at the breakpoint; press Weiter → continues.
6. **Sensor panel** — with a workflow running, open the Sensoren tab in
   the DebugPanel; expect the follower-joints row to update at ~5 Hz.
   If the row stays at zeroes, the `/workflow/sensors` topic isn't being
   published — the timer callback short-circuits when `on_workflow=False`,
   so confirm the workflow is actually running first.
7. **Vision tutorial** — open `sortiere_nach_klasse`, toggle Cloud-Erkennung
   on, drop a `finde Objekt mit Beschreibung "rote Tasse"` block, run.
   Expect either a detection on the camera feed or the German "lokal
   nicht bekannt"/"cold-start" toast (depending on cache state).

If any step fails, the most likely cause is in this order: (a) Supabase
migrations not applied (b) Docker images still on old `:latest` (c)
Railway build still in progress (look at the build log link printed by
`railway up`).

---

## 5. Env vars added in this upgrade

| Var | Where read | Default | Notes |
|---|---|---|---|
| `MODAL_VISION_APP_NAME` | `cloud_training_api/app/routes/vision.py:45` | `edubotics-vision` | Override if you renamed the Modal app. Must match `vision_app.py:APP_NAME`. |
| `MODAL_VISION_FUNCTION_NAME` | `vision.py:46` | `OWLv2Detector.detect` | Modal class-method lookup name. |
| `VISION_MODAL_TIMEOUT_S` | `vision.py:50` | `10` | Bump to 30 if cold-start storms produce too many 504s for students. |
| `EDUBOTICS_VISION_APP_NAME` | `modal_training/vision_app.py:43` | `edubotics-vision` | App name when running `modal deploy`. |
| `EDUBOTICS_VISION_MODEL` | `vision_app.py:44` | `google/owlv2-base-patch16-ensemble` | Apache-2.0 base model. |
| `EDUBOTICS_VISION_SNAPSHOT` | `vision_app.py:49` | `1` | Set to `0` if your Modal plan doesn't include memory snapshots. |
| `EDUBOTICS_VISION_MIN_CONTAINERS` | `vision_app.py:55` | `0` | Set to `1` during a teacher's active session to amortize cold starts. |
| `EDUBOTICS_VISION_SCALEDOWN_S` | `vision_app.py:59` | `180` | Modal scale-to-zero window. |
| `EDUBOTICS_VISION_SECRET_NAME` | `vision_app.py:92` | `edubotics-vision-secrets` | The secret bundle the vision worker reads. Must NOT be the training-secrets bundle. |
| `EDUBOTICS_DETECTOR` | `overlays/workflow/perception.py:52` | `yolox-tiny` | Reserved for future D-FINE-N swap. Any value other than `yolox-tiny`/`yolox` makes the server fail-loud at startup — see ROBOTER_STUDIO_DEFERRED.md §7.2. |

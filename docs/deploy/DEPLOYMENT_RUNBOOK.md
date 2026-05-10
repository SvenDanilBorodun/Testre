# Roboter Studio upgrade — deployment runbook

> Status from the automated session (2026-05-10):
>
> | Step | Status | Verified |
> |---|---|---|
> | Modal `edubotics-vision` app | **DEPLOYED** | `https://modal.com/apps/svendanilborodun/main/deployed/edubotics-vision` |
> | Modal `edubotics-vision-secrets` secret | **CREATED** (contains `HF_TOKEN`) | `modal secret list` |
> | Railway cloud_training_api | **UPLOADED** — Railway is building | Build log link printed by `railway up` |
> | Supabase migrations 015 / 016 / 017 | **PENDING** — needs DB password or paste | Dashboard SQL Editor |
> | 3 Docker images (`open_manipulator`, `physical_ai_server`, `physical_ai_manager`) | **PENDING** — Docker daemon was off in the session | Local docker build + push |
> | Git push of code changes | **PENDING** | `git push origin main` |
>
> The two remaining manual steps are listed below. Both are short.

---

## 1. Apply the Supabase migrations

A single combined SQL file is at `docs/deploy/APPLY_MIGRATIONS.sql`. It wraps
`015_workflow_versions.sql`, `016_tutorial_progress.sql`, and
`017_vision_quota.sql` in their own BEGIN/COMMIT blocks (so a single mistake
rolls back its own block without touching the others).

**Two options.**

### Option A — Supabase Dashboard SQL Editor (recommended, ~30 s)

1. Open `https://supabase.com/dashboard/project/<project-ref>/sql/new`.
2. Paste the entire contents of `docs/deploy/APPLY_MIGRATIONS.sql`.
3. Press **Run**.
4. Verify in the Dashboard's "Database" view:
   - Table `workflow_versions` exists.
   - Table `tutorial_progress` exists.
   - Functions `prune_workflow_versions`, `snapshot_workflow_version`,
     `consume_vision_quota`, `reset_vision_quota_used` are listed under
     "Functions".
   - Columns `vision_quota_per_term`, `vision_used_per_term` appear on
     `public.users`.

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

Each migration has its own rollback file in
`robotis_ai_setup/supabase/rollback/`. Apply in reverse order:

```bash
psql "$SUPABASE_DB_URL" -f robotis_ai_setup/supabase/rollback/017_vision_quota_rollback.sql
psql "$SUPABASE_DB_URL" -f robotis_ai_setup/supabase/rollback/016_tutorial_progress_rollback.sql
psql "$SUPABASE_DB_URL" -f robotis_ai_setup/supabase/rollback/015_workflow_versions_rollback.sql
```

---

## 2. Build + push the 3 Docker images

This is what makes the new code live for students when they re-launch the
GUI (the GUI pulls `nettername/*:latest` on every Docker Compose start).

### Prerequisites

- Docker Desktop running (or any Docker daemon).
- `docker login` against `https://hub.docker.com` using the `nettername`
  account credentials.
- The repo cloned locally (with all the new code from this upgrade).

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
  cloud-burst fallback, `EDUBOTICS_DETECTOR=dfine-n` env-flag support.
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
# and the tutorials JSON
docker run --rm nettername/physical-ai-manager:latest ls /usr/share/nginx/html/tutorials/
```

---

## 3. Git push of source changes

```bash
cd <repo root>
git status        # confirm the upgrade files staged
git push origin main
```

CI will run on push: `python-tests`, `shell-lint`, `compose-validate`,
`overlay-guard`, `manager-build-validate`, `nginx-validate`, plus the
two new jobs added in this upgrade: `tutorials-validate` and
`interfaces-validate`.

---

## 4. Smoke-test checklist (post-deploy)

In order, easiest first:

1. **Railway** — visit `https://scintillating-empathy-production-9efd.up.railway.app/health` and confirm `{"status":"ok"}`.
2. **Railway → DB** — call `GET /me/tutorial-progress` with a valid bearer
   token; expect `200 []` for a fresh user. If you get `500` with a
   `relation "tutorial_progress" does not exist` error, the Supabase
   migrations weren't applied yet — go back to step 1 of this runbook.
3. **Cloud vision** — call `POST /vision/detect` with a tiny base64 JPEG
   and `prompts: ["rote Tasse"]`. Cold start: expect ~5-8 s on first
   call. Body shape: `{detections: [...], elapsed_ms: …, cold_start: true|false}`.
4. **Student GUI** — launch EduBotics on a Windows machine with a robot.
   Open Roboter Studio → confirm the new toolbar (undo, redo, save, …)
   renders, the Galerie tab is reachable, the Lernpfad sidebar lists 6
   tutorials, and the Cloud-Erkennung checkbox appears in RunControls.
5. **Workflow + debugger** — load a tutorial, drag a `Wartezeit 2 s`
   block, set a breakpoint via Alt+click, press Start → workflow pauses
   at the breakpoint; press Weiter → continues.
6. **Vision tutorial** — open `sortiere_nach_klasse`, toggle Cloud-Erkennung
   on, drop a `finde Objekt mit Beschreibung "rote Tasse"` block, run.
   Expect either a detection on the camera feed or the German "lokal
   nicht bekannt"/"cold-start" toast (depending on cache state).

If any step fails, the most likely cause is in this order: (a) Supabase
migrations not applied (b) Docker images still on old `:latest` (c)
Railway build still in progress (look at the build log link printed by
`railway up`).

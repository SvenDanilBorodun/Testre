# Next steps — deploy the round-3 audit fixes

> Authored 2026-05-11. Companion to `DEPLOYMENT_RUNBOOK.md`. Everything in this file is what's left to do **on the deployment machine** (the one with Docker, Modal CLI, Railway CLI, and DB access) after pulling this branch.

The work in this branch is the third deep-dive audit of commit `deef761` (Roboter Studio Phase 1+2+3). The fixes touch four production surfaces — Modal, Railway, Supabase, and the three student Docker images. They land in the right order or live traffic gets a 500 / 503 storm; the ordering matters.

---

## 1. Pull the branch

```bash
git pull origin main
```

You should now see the audit-round-3 commit at the tip.

---

## 2. Apply the Supabase migrations BEFORE redeploying Railway

The Railway code in this branch references new columns / tables / RPCs that don't exist until migrations 015/016/017 are applied:

- `routes/vision.py` calls `consume_vision_quota(p_user_id)` and `refund_vision_quota(p_user_id)` RPCs.
- `routes/me.py:/me/tutorial-progress` queries the `tutorial_progress` table.
- `routes/me.py:/me/export` joins `workflow_versions` and `tutorial_progress`.
- `routes/workflows.py:/workflows/{id}/versions{,_id}/restore` writes the `app.user_id` GUC and depends on the `workflow_versions` table.

If you push the new Railway revision before these migrations land, every authenticated call to the new endpoints returns 500 / 503 until the migrations apply.

**Apply them via Supabase Dashboard → SQL Editor (~30 s):**

1. Open `https://supabase.com/dashboard/project/<project-ref>/sql/new`.
2. Paste `docs/deploy/APPLY_MIGRATIONS.sql` (the regenerated bundle includes 015 + 016 + 017 + a defensive `CREATE EXTENSION IF NOT EXISTS pgcrypto`).
3. Press **Run**.
4. Verify in the Dashboard "Database" view:
   - Tables `workflow_versions`, `tutorial_progress` exist.
   - Functions `consume_vision_quota`, `refund_vision_quota`, `reset_vision_quota_used`, `snapshot_workflow_version`, `prune_workflow_versions` are listed.
   - Columns `vision_quota_per_term`, `vision_used_per_term` appear on `public.users` with a CHECK constraint floored at 0.
   - `pg_publication_tables` shows both `workflow_versions` and `tutorial_progress` joined to `supabase_realtime`.

If you need a CLI path: `psql "$SUPABASE_DB_URL" -f docs/deploy/APPLY_MIGRATIONS.sql`.

**Rollback** (if needed): `psql "$SUPABASE_DB_URL" -f docs/deploy/ROLLBACK_MIGRATIONS.sql` — but only after rolling Railway back to a revision that doesn't reference the new schema.

---

## 3. Re-deploy the Railway API

Once the migrations are live:

```bash
cd robotis_ai_setup/cloud_training_api
railway up
```

What's new in this revision (a quick mental model so you can recognize the smoke-test surfaces):

- `POST /vision/detect` now hard-fails to 503 if migration 017 is missing (no silent fallback), rate-limits per **authenticated user** (JWT `sub`) instead of per IP, refunds the quota when Modal returns 502/504/timeout.
- `GET /me/export` now bundles `workflow_versions`, `tutorial_progress`, and the `vision_quota` columns.
- The rate-limit middleware decodes the JWT (without verifying — auth still gated by the route's `Depends(get_current_user)`) to derive a per-user key.

After the build finishes, smoke-test:

```bash
curl https://scintillating-empathy-production-9efd.up.railway.app/health
# → {"status":"ok"}

curl -H "Authorization: Bearer $TOKEN" \
  https://scintillating-empathy-production-9efd.up.railway.app/me/tutorial-progress
# → 200 [] for a fresh user (or rows if you've completed tutorials)

# A 503 here means migration 017 isn't applied yet — back to step 2.
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"image_b64":"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYGBgAAAABQABh6FO1AAAAABJRU5ErkJggg==","prompts":["rote Tasse"]}' \
  https://scintillating-empathy-production-9efd.up.railway.app/vision/detect
```

---

## 4. Re-deploy the Modal vision app

The Modal image build changed:

- `torch` and `torchvision` now install from `https://download.pytorch.org/whl/cu121` with `force_reinstall=True` (was implicitly resolving to CPU-only wheels).
- The OWLv2 snapshot lifecycle is now split: `@modal.enter(snap=True) load_weights` (CPU only, snapshot-portable) followed by `@modal.enter(snap=False) bind_device` that migrates the model to CUDA after restore. Previously `.to("cuda")` ran inside the snap=True hook and the snapshot froze the model on CPU.
- `text_labels` is now the canonical keyword for OWLv2 post-processing (transformers 4.46 removed `text_queries`).
- Function timeout bumped 30 s → 120 s so first-after-deploy cold starts don't immediately 504.
- The vision worker no longer silently falls back to `edubotics-training-secrets` (which leaks the Supabase service-role key); it hard-fails if `edubotics-vision-secrets` is missing.

**If you haven't yet created `edubotics-vision-secrets`** (one-time):

```bash
# Read-only HF token, NOTHING else.
modal secret create edubotics-vision-secrets HF_TOKEN=hf_<read_token>
modal secret list  # confirm the new bundle
```

**Re-deploy:**

```bash
cd robotis_ai_setup/modal_training
modal deploy vision_app.py
modal run vision_app::smoke_test
# → {"ok": true, "torch_version": "2.4.0+cu121", "cuda_available": true, "model": "google/owlv2-base-patch16-ensemble"}
```

The `cuda_available: true` line is the canary — if it's `false`, the cu121 wheel didn't land and you'll be paying T4 prices for CPU inference. Re-deploy.

---

## 5. Build + push the three student Docker images

The `physical_ai_server` overlay grew the 7 new ROS service callbacks, the SensorSnapshot publisher, the cloud-vision plumbing, and a bunch of runtime-safety fixes (atomic `pickup`/`drop_at`, bounded recovery lock, IK pre-check seeded from HOME, `controls_repeat_ext` cap before the loop, etc.). The Dockerfile also asserts the 8 new generated srv/msg Python files post-`colcon build` so a silent rosidl regression fails the build at the right place.

The React `physical_ai_manager` grew the per-user vision quota awareness, hardened sentinel parsing, the variable-inspector cap with FIFO eviction, the Workshop topic cleanup (no more leak across reconnects), Ctrl+Y / Ctrl+Shift+Z wiring, plus a new 7th tutorial covering hat blocks + broadcast.

```bash
cd robotis_ai_setup/docker

# Same env vars the existing pipeline expects.
export SUPABASE_URL='https://<project-ref>.supabase.co'
export SUPABASE_ANON_KEY='<your anon key>'
export CLOUD_API_URL='https://scintillating-empathy-production-9efd.up.railway.app'
# Optional: export REGISTRY=nettername  (default)
# Optional: export BUILD_BASE=0          (1 only if you want to rebuild the open_manipulator base; ~40 min)

./build-images.sh
```

`build-images.sh` builds three tags each tagged `:latest` and `:<BUILD_ID>` and pushes them all to Docker Hub under `nettername/`. The script runs a post-build grep on the manager bundle to make sure the placeholder Supabase URL / anon key / cloud API URL strings actually landed (catches the white-screen regression — CI's `manager-build-validate` does the same check).

Verify after push:

```bash
docker buildx imagetools inspect nettername/physical-ai-manager:latest
docker buildx imagetools inspect nettername/physical-ai-server:latest
docker buildx imagetools inspect nettername/open-manipulator:latest

# Spot-check that the new 7th tutorial landed in the manager image.
# Override the entrypoint (nginx) so the shell command actually runs.
docker run --rm --entrypoint sh nettername/physical-ai-manager:latest \
  -c 'ls /usr/share/nginx/html/tutorials/'
# Expect to see: ereignis_marker_gefunden.json plus the other six.
```

**`open_manipulator` was unchanged in this audit round** — you can skip rebuilding it (`BUILD_BASE=0` plus its own image build already short-circuits if no inputs changed), but pushing the full set keeps the three tags in lockstep.

---

## 6. Student-side rollout

No installer change needed. The GUI runs `docker compose pull` on every launch, so the next student to open EduBotics gets the new code automatically. If a student is already running the GUI when you push:

- Tell them to close it and re-launch.
- Or run `wsl -d EduBotics -- docker compose -f /mnt/c/Program\ Files/EduBotics/docker/docker-compose.yml pull` from a privileged shell on their machine.

---

## 7. End-to-end smoke checklist

In order, easiest first:

1. `GET /health` → `{"status":"ok"}`.
2. `GET /me/tutorial-progress` with a valid bearer → `200 []` for a fresh user. **503 with "Cloud-Erkennung ist auf diesem Server noch nicht fertig konfiguriert"** means migration 017 didn't land.
3. `POST /vision/detect` with the 1×1 PNG from step 3 above → expect a `200` with `cold_start: true` and an empty-or-tiny detections list on first call after deploy. Second call same minute should be much faster and `cold_start: false`.
4. Student GUI Roboter Studio:
   - Toolbar (undo, redo, save, theme, Verlauf) renders.
   - Galerie tab populates with own + group-shared workflows.
   - Lernpfad sidebar lists **7** tutorials (the new `Ereignis: AprilTag entdeckt` is at the bottom).
   - Cloud-Erkennung checkbox visible in RunControls.
5. Workflow + debugger:
   - Load any tutorial, set a breakpoint via **Alt+click**, press Start. Workflow pauses on the breakpoint; press Weiter to continue.
   - Sensoren tab in the DebugPanel updates at ~5 Hz with follower joints + gripper opening while a workflow is running. If the row stays at zero values, confirm the workflow is actually running (the publisher short-circuits when `on_workflow=False`).
6. Hat-block tutorial:
   - Open `Ereignis: AprilTag entdeckt`, drop a `Wenn AprilTag-Marker sichtbar` block, drag in a `Sage` block with text, press Start. Hold a tag36h11 marker in front of the scene camera — workflow should react.
7. Cloud-vision tutorial (only if you wired up the JWT-propagation path; see DEFERRED.md §1.4):
   - Open `sortiere_nach_klasse`, toggle Cloud-Erkennung on, run. With the current `_cloud_vision_burst` stub that raises `NotImplementedError`, you'll see the German "Cloud-Erkennung ist auf dieser Installation noch nicht aktiviert" toast — that's expected until the JWT path is finished.

---

## 8. Rollback plan

If any step fails and you need to roll back:

1. **Railway** — revert to the previous deployment in the Railway dashboard (one click).
2. **Supabase** — `psql "$SUPABASE_DB_URL" -f docs/deploy/ROLLBACK_MIGRATIONS.sql` (rolls back 017 → 016 → 015 in the safe order).
3. **Modal** — `modal deploy` the previous git ref of `vision_app.py`.
4. **Docker** — re-tag the previous `<BUILD_ID>` as `:latest`: `docker tag nettername/physical-ai-manager:<previous_build_id> nettername/physical-ai-manager:latest && docker push nettername/physical-ai-manager:latest` (repeat for the other two images).

Order matters in reverse: Railway → Supabase → Modal → Docker.

---

## 9. What's still deferred

The deferred-work catalogue is in `docs/ROBOTER_STUDIO_DEFERRED.md`. The two pieces most likely to matter on a follow-up sprint:

- **Cloud-vision JWT propagation** (§1.4) — the open-vocab block can only fire once we either bridge the React side to `/vision/detect` over rosbridge or mint per-classroom service tokens.
- **D-FINE-N integration** (§7.2) — the `EDUBOTICS_DETECTOR` env var now hard-fails at server startup if set to anything other than `yolox-tiny`/`yolox`. The full path requires baking the ONNX into the image and wiring the DETR-style decode head into `perception.py:_detect_yolo`.

Everything else in DEFERRED.md is informational rather than blocking.

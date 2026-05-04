# 20 — Operations Runbook

> **What this file is:** concrete procedures for the things that break, the things that rotate, and the things that migrate.
> Companion to [`01-architecture.md`](01-architecture.md) (how the system fits together) and [`21-known-issues.md`](21-known-issues.md) (known gaps).

---

## 1. Rotate secrets

### HuggingFace token (`HF_TOKEN`)

**Used by:** Railway FastAPI, Modal worker, physical_ai_server container (per-student via `~/.huggingface/token`).

1. https://huggingface.co/settings/tokens → "New token", write-scoped to the `edubotics` org.
2. Test it: `curl -H "Authorization: Bearer $NEW" https://huggingface.co/api/whoami-v2`
3. **Railway**: `railway variables set HF_TOKEN=<new>` (or via dashboard).
4. **Modal**: `modal secret create edubotics-training-secrets --from-dotenv <file>` — secret consumed at deploy time, so also run `modal deploy modal_training/modal_app.py` afterwards.
5. **physical_ai_server (per-student)**: students re-run the GUI's HF-token dialog (stored per-container in `~/.huggingface/token`).
6. **Modal MCP server**: also has Supabase keys but **not** HF_TOKEN — skip unless that changes.
7. **Revoke the old token** only after all surfaces have picked up the new one — a half-migration breaks training immediately.

### Supabase `SUPABASE_SERVICE_ROLE_KEY`

**Used by:** Railway FastAPI **only** (never shipped to students).

1. Supabase dashboard → Project Settings → API → "Rotate service role".
2. `railway variables set SUPABASE_SERVICE_ROLE_KEY=<new>`
3. `railway up --detach` from `cloud_training_api/` to pick up the new key at process start (FastAPI reads once at boot).
4. **Modal MCP server** also uses this key — update `mcp-edubotics` secret + redeploy.
5. Old key is void immediately on Supabase side; no staged rollout.

### Supabase `SUPABASE_ANON_KEY`

**Used by:** React bundle (baked at build), Modal worker, Railway.

Anon key is low-sensitivity but if you rotate:
1. Supabase dashboard → rotate.
2. Update Modal secret + Railway + React `.env.production`.
3. Rebuild + push Docker images (React key is baked at build time):
   ```bash
   cd robotis_ai_setup/docker && REGISTRY=nettername ./build-images.sh
   ```
4. Redeploy the Railway `web` service:
   ```bash
   cd physical_ai_tools/physical_ai_manager && railway up --detach
   ```

### `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET`

Only Railway uses it. Rotate via `modal token new`, then `railway variables set MODAL_TOKEN_ID=... MODAL_TOKEN_SECRET=...`.

### `MCP_BEARER_TOKEN`

Used only by Modal MCP server. Generate a new random string, update `mcp-edubotics` secret:
```bash
modal secret create mcp-edubotics --from-dotenv .env
modal deploy modal_mcp/mcp_server_stateless.py
```
Update any client (Claude config, etc.) with the new value.

---

## 2. Investigate a stuck training

Symptoms: Supabase row is `running` but no progress for &gt; 15 min.

```bash
# 1. What does Modal say?
modal app logs edubotics-training | grep <training_id>

# 2. Is the Railway stalled-worker sweep catching it?
# The sweep flips the row to failed after STALLED_WORKER_MINUTES (default 15)
# of no progress RPC calls. Check recent rows:
psql $SUPABASE_DATABASE_URL -c "
  SELECT id, status, last_progress_at, current_step, error_message
  FROM public.trainings
  WHERE status = 'running'
    AND last_progress_at < now() - interval '10 minutes'
  ORDER BY requested_at DESC LIMIT 10;
"

# 3. Cancel from Modal directly if the sweep missed it
modal function cancel <cloud_job_id>

# Then mark the row failed
psql $SUPABASE_DATABASE_URL -c "
  UPDATE public.trainings
  SET status = 'failed',
      error_message = 'Manually canceled after stall',
      terminated_at = now(),
      worker_token = NULL
  WHERE id = <training_id>;
"
```

If stalled-worker sweeps are cancelling **legitimately slow** jobs (pi0 checkpoint save can take 20+ min), raise the threshold:
```bash
railway variables set STALLED_WORKER_MINUTES=30
```

For the underlying logic, see [`10-cloud-api.md`](10-cloud-api.md) §6 (`_sync_modal_status`) and [`11-modal-training.md`](11-modal-training.md).

---

## 3. Roll back a bad Supabase migration

**Always take a PITR snapshot first** (Supabase dashboard → Database → Backups).

```bash
# Preview the rollback (single transaction, rolled back if any stmt fails)
psql $SUPABASE_DATABASE_URL --single-transaction \
    -f supabase/rollback/006_loss_history_rollback.sql

# If the forward and rollback scripts drifted, reapply migration.sql
psql $SUPABASE_DATABASE_URL -f supabase/migration.sql
```

Apply rollbacks in **REVERSE order**: 007 → 006 → 005 → 004 → 002. Skip 003 (no rollback file; 003's tables are dropped by 004).

See `supabase/rollback/README.md` for full procedure + data-loss warnings per file. See [`12-supabase.md`](12-supabase.md) §11.

---

## 4. Upgrade LeRobot version

Every bump must hit **3 places** in the same PR or inference drifts away from training:

1. **Modal training image**: `modal_training/modal_app.py` — change the `lerobot[pi0] @ git+https://.../lerobot.git@<COMMIT>` line and the `LEROBOT_COMMIT` constant.
2. **Base physical-ai-server image**: ROBOTIS clones from their `jazzy` branch; coordinate with ROBOTIS or rebuild the base from source.
3. **Local snapshot**: `physical_ai_tools/lerobot/` — replace with the same commit's archive.

Plus check whether LeRobot bumped its `codebase_version` — if yes, you also need a migration script for old datasets (the Modal preflight enforces `v2.1`).

Rebuild everything:
```bash
modal deploy modal_training/modal_app.py
cd robotis_ai_setup/docker && ./build-images.sh
```

**Test with an existing dataset end-to-end** before tagging a release.

For the workflow, see [`WORKFLOW-replace-or-upgrade.md`](WORKFLOW-replace-or-upgrade.md).

---

## 5. Roll back a bad image release

Images are tagged `:latest` + `:amd64-<version>`. Students pull by `:latest`, so to roll back:

```bash
# Find the previous known-good tag
docker image inspect nettername/physical-ai-server:amd64-0.8.2

# Retag it as :latest
docker tag nettername/physical-ai-server:amd64-0.8.2 \
           nettername/physical-ai-server:latest
docker push nettername/physical-ai-server:latest
```

Students' next `docker compose pull` picks up the rollback. For an urgent rollback, students can run the GUI's "Images aktualisieren" button or from a shell:
```bash
wsl -d EduBotics -- docker compose pull
```

---

## 6. Migrate a classroom between teachers

```sql
-- Reassign teacher on a classroom (service-role key required):
UPDATE public.classrooms
SET teacher_id = '<new-teacher-uuid>'
WHERE id = '<classroom-uuid>';
```

Credits sit on `users.training_credits` and are per-student, not per-classroom — they don't need to move. The new teacher's pool is recomputed on the next `/me` call.

---

## 7. Delete a student under GDPR / DSGVO right-to-erasure

Not yet exposed via API as a single click. Manual procedure:

```bash
# 1. Nuke auth user (cascades to public.users via FK ON DELETE CASCADE)
supabase auth delete-user <user-uuid>

# 2. Delete their trainings (if not already cascade)
psql $SUPABASE_DATABASE_URL -c "DELETE FROM trainings WHERE user_id = '<uuid>';"

# 3. Delete their HuggingFace datasets + models
for repo in $(huggingface-cli repo list --author <username>); do
    huggingface-cli repo delete "$repo" --yes
done

# 4. Purge container-local cache on the student's machine (best-effort;
#    physical access required)
wsl -d EduBotics -- docker exec physical_ai_server rm -rf \
    /workspace/datasets/<username>/ \
    /root/.cache/huggingface/hub/models--<username>*
```

Track completion in `admin_deletion_log` (planned — not yet implemented).

The `/me/delete` endpoint sets `users.deletion_requested_at` (via migration 007) but does NOT actually delete data — it's a marker for the admin to action.

---

## 8. Cloud API rate limiter — operating constraints

`cloud_training_api/app/main.py` ships an in-process `RateLimiter`:
- 10/min on `/trainings/start`
- 20/min on `/trainings/cancel`
- Keyed by leftmost `X-Forwarded-For` (Railway always sets it)

State lives **in the uvicorn process**. Two consequences:

1. **Stay at one worker.** `cloud_training_api/Dockerfile` runs `uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}` with no `--workers` flag — uvicorn's default is 1. If you ever add `--workers N` to that CMD, the effective limit becomes `N × <configured>` per IP.

2. **No state across instances.** If we ever horizontally scale the Railway service (multiple replicas), each replica gets its own buckets. Replace with a Redis-backed limiter at that point. The CORS validator + rule list at the top of `main.py` stay the same.

A 429 from the limiter carries CORS headers because `RateLimitMiddleware` is added BEFORE `CORSMiddleware` (Starlette wraps in reverse, so CORS becomes the outermost layer). **Do not swap that order** — comment in `main.py` calls it out.

---

## 9. First-install bootstrap

```bash
cd robotis_ai_setup

# 1. One-time admin user (prompts for password)
python scripts/bootstrap_admin.py --username admin --full-name "Your Name"

# 2. Build + push images (as maintainer, not student)
cd docker && REGISTRY=nettername \
    SUPABASE_URL=... SUPABASE_ANON_KEY=... CLOUD_API_URL=... \
    ./build-images.sh

# 3. Deploy Modal
cd ../modal_training && modal deploy modal_app.py
modal secret create edubotics-training-secrets --from-dotenv .env

# 4. Deploy Railway
cd ../cloud_training_api && railway up --detach

# 5. Build rootfs + GUI + installer for students
# (See [16-installer-wsl.md] §13)
cd ../wsl_rootfs && ./build_rootfs.sh
cd ../gui && pyinstaller build.spec
# Then on Windows: iscc installer/robotis_ai_setup.iss
```

For new account-system rollout details: [`23-rollout-accounts.md`](23-rollout-accounts.md).

---

## 10. Verify Supabase region (GDPR check)

```bash
# Check the project's region in the Supabase dashboard:
# Settings → General → Project Region
# Must be "EU West" (Frankfurt) for German student data
# If currently US: data export + new project + restore needed (significant change)
```

See [`21-known-issues.md`](21-known-issues.md) §2.9 for full GDPR action list.

---

## 11. Diagnose a recording that's missing video

The async video encoder can fail silently — episode marked complete before mp4 lands.

```bash
# Inside the physical_ai_server container:
wsl -d EduBotics -- docker exec physical_ai_server bash -c "
    cd ~/.cache/huggingface/lerobot/<user>/<repo>
    ls -la videos/chunk-000/<camera>/
    ls -la data/chunk-000/
    # Compare episode_count: parquet vs mp4 should match
"
```

Overlay `data_manager.py` adds `_verify_saved_video_files()` which catches this at record time (lines 289-333). If the overlay isn't in place (build broke?), the bug returns. Verify:

```bash
wsl -d EduBotics -- docker exec physical_ai_server sha256sum \
    /root/ros2_ws/src/.../data_processing/data_manager.py
# Compare against:
sha256sum robotis_ai_setup/docker/physical_ai_server/overlays/data_manager.py
```

If they don't match: the overlay didn't apply during build. Rebuild + restart.

---

## 12. Nuke and pave (clean reinstall on a student machine)

```powershell
# 1. Stop containers
wsl -d EduBotics -- /mnt/c/Program Files/EduBotics/scripts/uninstall_stop_containers.ps1
# Or:
wsl -d EduBotics -- docker compose -f /mnt/c/.../docker-compose.yml down

# 2. ⚠️ DESTROYS named volumes including ai_workspace (datasets!) - verify HF push first
wsl --unregister EduBotics

# 3. Uninstall via Programs and Features (or the Inno UninstallExe)

# 4. Reinstall via EduBotics_Setup.exe
```

**Always confirm with the user before `wsl --unregister`** — datasets may not be backed up to HF. See [`WORKFLOW.md`](WORKFLOW.md) §6.

---

## 13. Cross-references

- Architecture overview: [`01-architecture.md`](01-architecture.md)
- Per-layer details: [`10-cloud-api.md`](10-cloud-api.md) … [`18-modal-mcp.md`](18-modal-mcp.md)
- Migration ordering + rollback: [`12-supabase.md`](12-supabase.md) §10–11
- Account rollout (post-merge ops): [`23-rollout-accounts.md`](23-rollout-accounts.md)
- Known issues: [`21-known-issues.md`](21-known-issues.md)

---

**Last verified:** 2026-05-04. Update timestamps when procedures drift.

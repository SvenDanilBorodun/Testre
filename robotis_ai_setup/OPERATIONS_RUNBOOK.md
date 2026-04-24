# EduBotics Operations Runbook

Concrete procedures for the things that break, the things that rotate,
and the things that migrate. Companion to `Testre/CLAUDE.md` (how the
system fits together) and `Testre/CLAUDE_AUDIT.md` (known gaps).

## 1. Rotate secrets

### HuggingFace token (`HF_TOKEN`)
Used by: Railway FastAPI, Modal worker, physical_ai_server container.
1. https://huggingface.co/settings/tokens → "New token", write-scoped to
   the `edubotics` org.
2. Test it: `curl -H "Authorization: Bearer $NEW" https://huggingface.co/api/whoami-v2`
3. **Railway**: `railway variables set HF_TOKEN=<new>` (or via dashboard).
4. **Modal**: `modal secret create edubotics-training-secrets --from-dotenv <file>`
   — the secret is consumed by `modal_app.py` at deploy time, so also
   run `modal deploy modal_training/modal_app.py` afterwards.
5. **physical_ai_server (per-student)**: students re-run the GUI's
   HF-token dialog (stored per-container in `~/.huggingface/token`).
6. Revoke the old token only after all three surfaces have picked up
   the new one — a half-migration breaks training immediately.

### Supabase `SUPABASE_SERVICE_ROLE_KEY`
Used by: Railway FastAPI (**only**, never shipped to students).
1. Supabase dashboard → Project Settings → API → "Rotate service role".
2. `railway variables set SUPABASE_SERVICE_ROLE_KEY=<new>`
3. `railway up --detach` from `cloud_training_api/` to pick up the new
   key at process start (FastAPI reads once at boot).
4. Old key is void immediately on Supabase side; no staged rollout.

### Supabase `SUPABASE_ANON_KEY`
Used by: React bundle (baked at build), Modal worker, Railway.
Anon key is low-sensitivity but if you rotate:
1. Supabase dashboard → rotate.
2. Update Modal secret + Railway + React `.env.production`.
3. Rebuild + push Docker images (React key is baked at build time):
   `cd robotis_ai_setup/docker && REGISTRY=nettername ./build-images.sh`
4. Redeploy the Railway `web` service (`railway up --detach` from
   `physical_ai_tools/physical_ai_manager/`, or push to the Railway-
   tracked branch).

### `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET`
Only Railway uses it. Rotate via `modal token new`, then
`railway variables set MODAL_TOKEN_ID=... MODAL_TOKEN_SECRET=...`.

---

## 2. Investigate a stuck training

Symptoms: Supabase row is `running` but no progress for > 15 min.

```bash
# 1. What does Modal say about the job?
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
# then mark the row failed
psql $SUPABASE_DATABASE_URL -c "
  UPDATE public.trainings
  SET status = 'failed',
      error_message = 'Manually canceled after stall',
      terminated_at = now(),
      worker_token = NULL
  WHERE id = <training_id>;
"
```

If stalled-worker sweeps are cancelling legitimate slow jobs (pi0
checkpoint save can take 20+ min), raise the threshold:
`railway variables set STALLED_WORKER_MINUTES=30`.

---

## 3. Roll back a bad Supabase migration

See `supabase/rollback/README.md`. Always take a PITR snapshot first.
Sequence for the latest migration:

```bash
# Preview the rollback (single transaction, rolled back if any stmt fails)
psql $SUPABASE_DATABASE_URL --single-transaction \
    -f supabase/rollback/006_loss_history_rollback.sql

# If the forward and rollback scripts drifted, reapply migration.sql
psql $SUPABASE_DATABASE_URL -f supabase/migration.sql
```

---

## 4. Upgrade LeRobot version

Every bump has to hit three places in the same PR or inference drifts
away from training:

1. **Modal training image**: `modal_training/modal_app.py` — change the
   `lerobot[pi0] @ git+https://.../lerobot.git@<COMMIT>` line.
2. **Base physical-ai-server image**: ROBOTIS clones from their jazzy
   branch; coordinate with ROBOTIS or rebuild the base from source.
3. **Local snapshot**: `physical_ai_tools/lerobot/` — replace with the
   same commit's archive.

Rebuild everything:
```bash
modal deploy modal_training/modal_app.py
cd robotis_ai_setup/docker && ./build-images.sh
```

Test with an existing dataset end-to-end before tagging a release.

---

## 5. Rollback a bad image release

Images are tagged `:latest` + `:amd64-<version>`. Students pull by
`:latest`, so to roll back:

```bash
# Find the previous known-good tag
docker image inspect nettername/physical-ai-server:amd64-0.8.2

# Retag it as :latest
docker tag nettername/physical-ai-server:amd64-0.8.2 \
           nettername/physical-ai-server:latest
docker push nettername/physical-ai-server:latest
```

Students' next `docker compose pull` picks up the rollback. For an
urgent rollback, students can run the GUI's "Images aktualisieren"
button or from a shell: `wsl -d EduBotics -- docker compose pull`.

---

## 6. Migrate a classroom between teachers

```bash
# Reassign teacher on a classroom (service-role key required):
psql $SUPABASE_DATABASE_URL -c "
  UPDATE public.classrooms
  SET teacher_id = '<new-teacher-uuid>'
  WHERE id = '<classroom-uuid>';
"

# Credits sit on users.training_credits and are per-student, not
# per-classroom — they don't need to move. The new teacher's pool is
# recomputed on the next /teachers/me call.
```

---

## 7. Delete a student under GDPR / DSGVO right-to-erasure

Not yet exposed via API. Manual procedure:

```bash
# 1. Nuke auth user (cascades to public.users via FK)
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
    /workspace/datasets/<username>/ /root/.cache/huggingface/hub/models--<username>*
```

Track completion in `admin_deletion_log` (planned — not yet implemented).

---

## 8. First-install bootstrap

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

# 4. Deploy Railway
cd ../cloud_training_api && railway up --detach

# 5. Build rootfs + GUI + installer for students (see step-by-step in
#    CLAUDE_PIPELINE.md §1)
```

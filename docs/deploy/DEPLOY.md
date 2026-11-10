# EduBotics — Deploy

> ⚠️ **PARTLY HISTORICAL.** The undated sections of this runbook (the golden-order list below, §1's generic steps, §2, §3's manual push, §4's Docker Hub pushes, §5 and the closing checklists) predate the GHCR-primary migration and the release.yml W1→W6 golden order, and some of their steps (e.g. manual `docker push nettername/*:latest`) **contradict CLAUDE.md Rule §6** — CI owns the student-facing images; they are never pushed from a workstation. For the authoritative process see **CLAUDE.md → "Six non-negotiable rules → §6 CI/CD deploys"** + `.github/workflows/`.
>
> **Current and binding (owner steps):** the DATED sections — §1's notes on migrations 053, 055 and 056, §3b (managed training), §3c (the login check, the admin's second factor, the edge) and §3d (legal links, CSP) — and **[`MERGE-CHECKLIST.md`](MERGE-CHECKLIST.md): merging to `main` is a production deploy** (W2 and W3 fire by path, without a tag; W1, the migrations, never runs on a merge and is run by hand after W2's `/health`, owner decision 2026-11-15); its steps come BEFORE the merge, not before a release.

One-page reference. Pair with `APPLY_MIGRATIONS.sql` / `ROLLBACK_MIGRATIONS.sql` in this folder.

## The golden order (never reorder)

```
1. Supabase migrations     ← schema fingerprint in Railway gates on this
2. Modal app               ← training spawns fail without modal_app.py deployed
3. Railway (Cloud API)     ← auto-deploys on git push to main
4. Docker Hub images       ← students pull on next GUI launch
5. git push                ← CI runs guardrails (10 jobs)
```

Managed ACT training (migrations 047 + 049, the RunPod build) adds operator state before `TRAINING_DISPATCH=managed` and an ordered rollback: see **3b** below. On a tag, `release.yml` builds the trainer image and sets `RUNPOD_TRAINER_IMAGE` (with `--skip-deploys`) before the cloud deploy; the Modal deploy (step 2) must carry the same lock.

Per-student token storage (migration 042) adds one step before step 3: set `EDUBOTICS_HF_TOKEN_KEY` on Railway BEFORE the cloud deploy (a required variable, see below), then 1 → 2 → 3 as above; rollback is the previous release plus `rollback/20261003120000_042_user_hf_credentials_rollback.sql`, cloud API first.

Skip step 1 → Railway boot fails. Skip step 2 → student training spawns fail (Modal app not deployed). Push images before Railway → student calls hit routes that don't exist yet.

---

## 0. The production approval gate (security audit SA-13, 2026-11-13) — and why W1 is manual (2026-11-15)

Six jobs that change production name `environment: production`: `supabase-migrate.yml` **apply-production** and **rollback** (W1), `railway-deploy-cloud-api.yml` **deploy** (W2), `railway-deploy-teacher-web.yml` **deploy** (W3), and in `release.yml` **trainer-image-var** (W2b) and **publish-gui-version** (W6). The gate is the environment's protection rule (required reviewers), which the workflow cannot create.

**The gate exists only on a public repository or with GitHub Enterprise.** GitHub's documentation („Deployments and environments"): on the Free, Pro and Team plans, required reviewers are only available for PUBLIC repositories. On a private repository under those plans the environment is created anyway and approves nothing: every job above runs at once (verification round B, VB-02). Therefore, by owner decision (2026-11-15), **W1 has no push trigger**: a merge to `main` deploys W2 and W3 and never touches the database; the migrations run only from a release tag (`release.yml` calls W1 first) or the manual „Run workflow" button (`workflow_dispatch`, apply or `rollback_to`). `robotis_ai_setup/tests/test_deploy_approval_gate.py::MigrationsAreManual` fails if a push trigger comes back, and apply-production accepts the event `push` only for a `refs/tags/v…` ref. W2 and W3 on a merge stay ungated on such a plan (accepted).

**Owner, once** (GitHub → the repository → Settings → Environments) — useful on a public repository or with Enterprise, harmless otherwise:

1. **New environment** named exactly `production` (if a run created it already, open it: GitHub creates a missing environment on first use, WITHOUT any protection — that is the state in which the gate approves nothing).
2. **Required reviewers**: add yourself (and a second maintainer if there is one); tick **Prevent self-review** only when a second reviewer exists, or every run waits forever. (Not offered on a private repository under Free, Pro or Team.)
3. **Deployment branches and tags**: *Selected branches and tags* → add `main` and the tag pattern `v*` (W1 also runs on a `workflow_dispatch` from `main`).
4. Leave the repository secrets where they are: the jobs still read `SUPABASE_*`, `RAILWAY_TOKEN` from the repository. (Moving them into the environment is a later, optional hardening: then no job without the gate can read them.)

What it costs where the gate exists: a tagged release stops five times for approval (W1, W2b, W2, W3, W6), and a plain push to `main` stops once per deploy workflow it triggers (W2 for the cloud API, W3 for the teacher web); a waiting job fails after 30 days. A run nobody approves deploys nothing; reject it to end it. The health gates and the build jobs need no approval. Without the gate, a merge's migrations still wait for a person: the owner presses „Run workflow" (`MERGE-CHECKLIST.md`).

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

**Rollback:** paste the matching `rollback/*.sql`. Roll Railway back BEFORE rolling Supabase back — except for 053, below.

### Migration 053 — the classroom Jetson retired (2026-11-09)

`20261109120000_053_retire_jetson.sql` drops `public.jetsons`, its eleven RPCs
and `public.inference_runs.jetson_id` (the table, its rows — old
`backend = 'jetson'` rows included — and its backend CHECK stay). Every
`jetsons` row and every `jetson_id` value is lost; the rollback restores the
schema, not the data.

- **Deploy order: W2 FIRST, then W1 by hand** (this release, verification
  round B; `MERGE-CHECKLIST.md`). The API of the same release probes no Jetson
  object, so it boots with or without 053 (measured on a 052 database:
  `schema_ok: true`). The OLDER API does not: on a 053 database it exits at
  start-up (measured), it would log a failed `sweep_jetson_locks` every minute,
  and a Cloud-Inferenz run ending meanwhile would get no teacher history row.
  W1 before W2 therefore risks a full API outage whenever Railway restarts the
  old pod; a merge no longer runs W1 at all (section 0).
- **Rollback, in THIS order** (the reverse of the general rule above):
  `rollback/20261109120000_053_retire_jetson_rollback.sql` FIRST (the API of
  this release boots on either schema), THEN Railway back to the older release
  (its boot probe needs the restored objects).
- Hand-run assertions: `supabase/tests/053_retire_jetson_assertions.sql`
  (12 PASS on a local stack; never against the linked project).

### Migration 055 — `public.users` default-deny; deletion requests (2026-11-11, launch plan step 5)

`20261111120000_055_users_identity_guard.sql` revokes INSERT, UPDATE, DELETE,
TRUNCATE, REFERENCES and TRIGGER on `public.users` from `anon` and
`authenticated` (table and column level), drops the own-row policy „Users update
own profile" and keeps 044's guard (rule 1 on the whole row, rule 2 P0044
unchanged). It changes no API-visible shape and touches no row.

- **Deploy order (either order is safe for 055 itself; this release runs W2
  first for 053's and 058's sake).** 055 lands with W1; the API
  of the same release (the deletion-request routes, the complete export,
  `/me/delete/withdraw`) with W2, the teacher web with W3, the student SPA with
  W4. The API writes `users` only with the service role, so an OLDER API works on
  a database with 055, and the NEW API works on a database without it (055 is
  defense in depth for what the API already does). An old student SPA never
  wrote `users`; an old teacher web simply shows no queue.
- **After W1:** in the SQL editor, `SELECT has_table_privilege('authenticated',
  'public.users', 'UPDATE')` answers `false`, and the migration's NOTICE said
  „055 self-check passed" (it aborts and changes nothing otherwise — e.g. if a
  PUBLIC grant exists; then find it, revoke it, and push again).
- **Rollback:** `rollback/20261111120000_055_users_identity_guard_rollback.sql`
  (re-grants the six privileges, re-creates 024's policy, restores 044's guard
  body). No API rollback is needed either way. It re-opens the student-writable
  columns, `deletion_requested_at` among them.
- Hand-run assertions: `supabase/tests/055_users_identity_guard_assertions.sql`
  (36 PASS on a local stack; never against the linked project). It breaks
  044's T7/T8 by design.
- **Tell the teachers** (owner step, see the step-5 report): the dashboard's
  „Löschanfragen" button lists their students' requests; the GDPR deadline is one
  month from the request; „Löschung ausführen" deletes the account for good;
  recordings in a student's own Hugging Face account must be deleted there.

### Migration 056 — an admin's RLS reads need a two-factor session (2026-11-12, wave-A fix VA-04)

`20261112120000_056_admin_requires_aal2.sql` makes `public.rls_is_admin()`
require the JWT claim `aal = aal2` and routes the nine inline admin branches of
the RLS read policies through it (`ALTER POLICY`, every other branch unchanged).
An admin with only a password then reads nothing but their own row through
PostgREST, as the API already refused them. No API-visible shape, no row touched.

- **Deploy order:** W1, either side of W2/W3. The API runs as the service role
  (RLS dormant) and the teacher web reads nothing from the database directly, so
  no page changes. See `MERGE-CHECKLIST.md` for the merge that carries it.
- **After W1:** the W1 log shows „056 self-check passed";
  `SELECT pg_get_functiondef('public.rls_is_admin()'::regprocedure)` names `aal2`.
- **Rollback:** `rollback/20261112120000_056_admin_requires_aal2_rollback.sql`
  (031's helper and the nine predicates verbatim). No API rollback is needed. It
  re-opens the password-only admin read.
- Hand-run assertions: `supabase/tests/056_admin_requires_aal2_assertions.sql`
  (36 PASS on a local stack; 20 at 055; never against the linked project).

### Migrations 057–059 — request roles default-deny, no self-registration, reserved Hugging Face namespaces (2026-11-13, deep security audit)

`20261113120000_057_request_role_default_deny.sql` leaves `anon` and
`authenticated` only the reads and RPCs in `supabase/request_role_allowlist.json`
(no write, no sequence, no other function; default privileges revoked; the
storage bucket `releases` locked); `20261113130000_058_refuse_self_signup.sql`
refuses every account the server did not create;
`20261113140000_059_hf_reserved_namespaces.sql` refuses the organisation and the
platform token's account as a student's Hugging Face name and dataset
namespace. None changes an API-visible shape.

- **Before W1 (owner, dashboard):** delete the Storage policy „Service role
  upload to releases" and the bucket `releases` (or make it private) and review
  its objects. 057 does it itself when the migrating role may; when it may not,
  057 stops and changes nothing, naming this step. Switch off sign-up
  (Authentication → Sign In / Providers), delete the accounts that registered
  themselves, check that `postgres` may read `auth.users` (058's self-check
  needs it) and create the `production` environment (section 0; it approves
  nothing on a private repository under Free, Pro or Team). The queries are in
  `MERGE-CHECKLIST.md` section 1.
- **Deploy order: W2 (the API of the same release: the provisioning marker,
  PATCH /me refuses, anonymous discovery, the reserved namespaces, the pinned
  `/version`) → W1 (057, 058, 059) by hand.** **058 needs the new API**: an
  older API's `create_user` sends no marker, so with 058 applied before W2 a
  teacher's „Schüler anlegen" (and an admin's new teacher) answers 500 until W2.
  W2 first removes that window. For the same reason an
  API ROLLBACK below this release needs the 058 rollback first. Otherwise an
  OLDER API on the new database works (it is service-role only), except its
  PATCH /me with an organisation name (the database refuses it, P0059 → a 500)
  and its sync of the organisation (registers nothing, P0034); the NEW API works
  on a database without 059 (it keeps its in-process deny-list).
- **After W1:** the NOTICEs said „057 self-check passed", „058 self-check
  passed", „059 self-check passed"; read 059's WARNING (registry rows already in
  a reserved namespace, `docs/KNOWN-ISSUES.md` names the review query). Then the
  live checks of `docs/KNOWN-ISSUES.md` (2026-11-13 entry): the Training page's
  live chart, the dataset picker, the template picker, the Lernpfade progress,
  and a few 42501 probes with a student token.
- **Rollback, in this order:** 059 → 058 → 057
  (`rollback/20261113140000_059_hf_reserved_namespaces_rollback.sql`,
  `…_058_refuse_self_signup_rollback.sql`, `…_057_request_role_default_deny_rollback.sql`).
  Each RE-OPENS what it closed; 057's restores neither the storage policy nor
  `pg_stat_statements`. No API rollback is needed for any of them. An API
  rollback BELOW this release needs every rollback down to 053's first
  (059 → 053, newest first): the older API cannot boot on a database with 053.
- Hand-run assertions: `supabase/tests/057_…` (50 PASS), `058_…` (14 PASS + one
  expected ERROR), `059_…` (13 PASS), on a local stack only. 057 breaks 049's
  T3, 052's T2h and 055's T5a by design.

---

## 2. Modal — training app

One app, one secret bundle.

| App | Module | Secret bundle | Contents |
|---|---|---|---|
| `edubotics-training` (`EDUBOTICS_TRAINING_VARIANT=prod`, the default): `train`, `train_vla`, `train_managed`, `smoke_test` | `modal_training/modal_app.py` | `edubotics-training-secrets` (legacy `train`/`train_vla` only) | `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `HF_TOKEN` (write) |
| `edubotics-training-dev` (`EDUBOTICS_TRAINING_VARIANT=dev`): `train_managed`, `smoke_test` | `modal_training/modal_app.py` | none, referenced nowhere | — (the end-to-end stack's target) |

`train_managed` (managed ACT) takes no Modal secret: the API passes its per-attempt worker token, `TRAINING_WORKER_API_URL`, `TRAINING_WORKER_SUPABASE_URL` and `SUPABASE_ANON_KEY` as arguments. Every function's image installs the generated `training-lock.txt` with `--no-deps`; a deploy whose lock or `training_handler.py` does not match the API's `TRAINING_LOCK_SHA256` / `TRAINING_HANDLER_SHA256` (generated by `make_training_lock.py` / `make_handler_hash.py`; the worker sends both hashes in `start`) makes every managed Modal attempt answer `wrong_image`, so deploy BEFORE the tag whenever `training_handler.py` or the lock changed. Deploy too when `modal_app.py` changed: nothing detects a stale `modal_app.py`. Both constants live under `cloud_training_api/`, so a handler or lock edit pushed to `main` makes W2 deploy an API that refuses the current release's trainer image and Modal deploy until the next tag: land it with its release, or keep `TRAINING_DISPATCH=legacy` meanwhile. The dev variant: `EDUBOTICS_TRAINING_VARIANT=dev modal deploy modal_app.py`.

```bash
cd robotis_ai_setup/modal_training

# One-time, or when keys rotate:
modal secret create edubotics-training-secrets SUPABASE_URL=… SUPABASE_ANON_KEY=… HF_TOKEN=…

# Sanity-check imports BEFORE deploy (catches Modal SDK API drift):
modal run -m modal_app::smoke_test     # expect torch=2.x+cu121, cuda_available=true

modal deploy modal_app.py

modal app list | grep edubotics
```

**Don't break:**
- LeRobot's pin now lives in `make_training_lock.py` and the generated `training-lock.txt` (CLAUDE.md Rule §5); never edit the lock by hand, regenerate it.
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

**Required env vars** (Railway dashboard): `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`, `EDUBOTICS_HF_TOKEN_KEY` (per-student Hugging Face token storage, migration 042: 32 random bytes as standard base64, generate with `python -c "import secrets,base64;print(base64.b64encode(secrets.token_bytes(32)).decode())"`; absent, empty or malformed → the API refuses to boot (required since 2026-10-04); optional `EDUBOTICS_HF_TOKEN_KEY_PREVIOUS` for rotation; **set it BEFORE the cloud deploy**; losing it makes every stored token unusable and each student pastes theirs again).

**Production-relevant optional:** `LEGAL_IMPRESSUM_URL`, `LEGAL_DATENSCHUTZ_URL`, `LEGAL_NUTZUNGSBEDINGUNGEN_URL` (the legal links, section 3d; an invalid one stops the boot), `ALLOWED_ORIGINS`, `GUI_VERSION` + `GUI_DOWNLOAD_URL` + `GUI_RELEASE_REPO` (drives student `.exe` auto-update — normally set automatically by `release.yml` W6 on a `vX.Y.Z` tag; `GUI_DOWNLOAD_URL` is optional and derived from `GUI_VERSION` + `GUI_RELEASE_REPO` when unset; when set it is served only as exactly `https://github.com/<GUI_RELEASE_REPO>/releases/download/<tag>/<asset>`, anything else is ignored with a WARNING), `HF_TOKEN` (GDPR + dataset sweep), `MAX_TRAINING_TIMEOUT_HOURS`, `STALLED_WORKER_MINUTES`, `AUTH_VERIFY_MODE` and `TRUSTED_PROXY_HOPS` (§3c; leave both unset unless §3c says otherwise). (`EDUBOTICS_JETSON_HF_TOKEN`, `SUPABASE_JWT_ALGORITHM` and `SUPABASE_JWT_SECRET` were read only by the retired classroom Jetson's `/jetson/register`; no code reads them since 2026-11-09. Do not just delete them: revoke and rotate first, §4b.)

**Never** set `EDUBOTICS_SKIP_SCHEMA_CHECK=1` on Railway.

**Managed ACT training adds required variables** (section 3b): `HF_TOKEN` stops being optional once `TRAINING_DISPATCH=managed`: the API publishes every managed model itself, so it must be a token with **write access to `EduBotics-Solutions`** (the boot's HF write check blocks placement otherwise).

---

## 3b. Managed ACT training — RunPod, Cloudflare R2, migrations 047 + 049 + 050 + 052 (training hours)

Authoritative rules: `CLAUDE.md` „Managed ACT training", Rule §4's ledger paragraph and Rule §6. Order: 047 + 049 + 050 (W1) → the trainer image (dispatch once, see below) → the operator state → the API with `TRAINING_DISPATCH=legacy` → `TRAINING_DISPATCH=managed`. Since 052 `TRAINING_DISPATCH=legacy` STOPS new trainings (503); it is no longer a fallback path.

**The training-hour ledger (052, v2.37.0): release steps, in this order.** The release starts at the MERGE to `main`, not at the tag: the merge deploys the database, the API and the teacher web; the tag adds the installer, the Pi tarball and the GUI update gate.

- (−1) **Gate G0 has passed** on the R2 TEST bucket (it did on 2026-10-29; its JSON line goes into the release notes). To re-run it (the owner's OK; it writes only under `train/gate/<uuid>/` in `edubotics-training-test` and deletes everything it wrote; it refuses `edubotics-training` and any bucket named by `R2_BUCKET` / `R2_PRODUCTION_BUCKET` before any request; without `--run` it prints its plan): `cd robotis_ai_setup/cloud_training_api && ( set -a; . ~/.edubotics-test/r2-test.env; set +a; python ../runpod_training/e2e/r2_checksum_gate.py --run )`. Exit 0 = pass, 1 = R2 does not enforce the signed SHA-256 (stop the release), 2 = refused before any request.
- (0) Before the merge to `main`: Railway `MANAGED_ACT_CAP_S` is unset or one of 3600 / 7200 / 10800 (anything else stops the new API's boot and W2's health gate fails) and `TRAINING_DISPATCH=managed`. Schedule the merge (and the tag after it) outside lesson time and tell the teachers beforehand (owner D9); there is no student-facing banner.
- (1) Merge → W1 applies 052. By their `on.push.paths` filters the merge fires `supabase-migrate.yml` (W1), `railway-deploy-cloud-api.yml` (W2), `railway-deploy-teacher-web.yml` (W3) and `docker-publish.yml` (the `:latest` images) side by side, with no order between them (v2.37.0: merge pushed 2026-11-02 08:58:10 UTC, 052 applied 08:58:51–08:59:04). Every balance is 0 from W1 on (the reset): until W2 the old API keeps serving, its starts answer 403 „Keine Trainings-Credits mehr übrig." and its cancels settle nothing for the old `derived` rows. Its credit routes are NOT all refused (measured by verifier V2): `POST /teacher/students/{id}/credits` answers 500 (the old `adjust_*` raise P0069), but the old `PATCH /admin/teachers/{id}/credits` answers 200 and writes the dead `users.training_credits` (the pool does not change, but a later 052 rollback then reads that written value back as the teacher's credits, not the one at 052).
- (2) The same merge's W2 and W3. W2's new API proves 052 with its schema probe (23 tables, 23 column sets, 56 RPCs — `mark_checkpoint_ready` included, so W1 must apply 052 as merged with its fix rounds: on a 052 without that function the new API fails its own `/health` and the old one keeps serving). W2 does not wait for W1 on a merge: a new pod that booted before 052 was applied would fail its probe the same way and the tag's W2, which runs after its W1, deploys again (on 2026-11-02 W1 was done first; W2's health gate was green at 09:00:03 UTC). After W2 and until W3 has deployed, the old teacher web's PATCH gets 405 (the route is now `POST /admin/teachers/{id}/credits`); W3 has no order against W2 either and may land first (2026-11-02: green at 09:01:10 UTC). **Do not use the teacher or admin dashboard from the merge until the merge's W2 and W3 have both finished.** The tag (`vX.Y.Z` on the merge commit) then runs `release.yml`'s whole chain on that commit: W1 again (a no-op, „Remote database is up to date."), the trainer image and W2b (`RUNPOD_TRAINER_IMAGE` pinned with `--skip-deploys`), W2 and W3 again (the same commit; W2 boots with the pinned digest), W4 (the `:X.Y.Z` images), W5 (the installer), W5b (the Pi tarball) and W6 (`GUI_VERSION` last); only the tag ships the `.exe`, the Pi tarball and the GUI update. Between the merge's W2 and the tag's W2 the new API places with the previous release's `RUNPOD_TRAINER_IMAGE`, which passes `start` only because 052 changes neither the training lock nor `training_handler.py` (v2.37.0: tag pushed 08:58:26 UTC, its W2 green 09:12:19, W3 09:14:17, W6 done 10:11:18).
- (3) Once the merge's W2 and W3 have both finished, the admin re-grants every teacher (admin dashboard „Stunden vergeben"; the pre-052 credit number is shown as `old_credits`, a hint only); this need not wait for the tag, whose W2 and W3 redeploy the same commit and whose W6 only advertises the new `.exe`.
- (4) Teachers assign hours to students and groups. Students can train.
- (5) Stop the Modal training app whenever after W2: `modal app stop edubotics-training` (owner T1B). New rows never reach it; a pre-052 unmanaged training still running there dies and is failed by the legacy reconciler (no ledger effect). The emergency rung stays OFF (`MANAGED_MODAL_RUNG=0`); flipping it needs a `modal deploy modal_app.py` at the running release first (owner Q-E).

The release needs no trainer-image or Modal change: the new signed `x-amz-checksum-sha256` header passes through the unchanged worker (it sends the headers it is given).

**Operator state, once, before `TRAINING_DISPATCH=managed`:**

1. **Cloudflare R2:** the existing bucket `edubotics-training` (default jurisdiction, owner E40; endpoint `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`) and the TEST bucket `edubotics-training-test` (WEUR, default jurisdiction) — **created 2026-10-29 for gate G0: no test bucket existed before, whatever this file said earlier**; the key in `~/.edubotics-test/r2-test.env` can also see the production bucket, so the gate's own refusal of `edubotics-training` is the only protection; an R2 API token with **Object Read & Write on that bucket only** — **roll its secret** (it was pasted in a chat, owner E40; an ask-first production secret, done by the owner); TWO lifecycle rules on both buckets: `train/` deleted after **3 days** and `keep/` after **9 days** (the continuable copy's 7 days plus the janitor's 12 h 20 min grace plus R2's up-to-24-hour lag). **Before the first capped run** prove CopyObject on the TEST bucket (review n2, the command spelled out; `r2_client` has no plain PUT, so use `put_file_presigned`): with the test bucket's R2 variables, `cd robotis_ai_setup/cloud_training_api && python -c 'from app.services import r2_client as r; p="/tmp/e2e"; open(p,"wb").write(b"x"*1024); r.put_file_presigned("train/probe/x", p); r.copy_object("train/probe/x", "keep/probe/x"); assert r.head_size("keep/probe/x")==1024; r.delete_keys(["train/probe/x","keep/probe/x"]); print("copyobject ok")'`; a failure is not a blocker (the publish falls back to streaming through a presigned PUT, one WARNING per publish) but write it into the release note.
2. **RunPod:** an API key with **`pods: read + write`** and **`catalog: read`** (nothing else); prepaid balance, auto-reload, a low-balance alert and a **spend limit of about $15/h**.
3. **GHCR:** the package `edubotics-trainer` must exist and be **public** BEFORE the first tag that carries managed training (the release's anonymous-pull probe fails otherwise). It exists and is public since 2026-10-19 (the E2E test images `test-<sha>`); check it still is. Once `trainer-image.yml` is on `main`, `gh workflow run trainer-image.yml --ref main` builds another test image.
4. **Railway variables**, required when `TRAINING_DISPATCH=managed` (the boot names a missing one, never its value): `SUPABASE_ANON_KEY`, `TRAINING_WORKER_API_URL` (https, the API's public URL the pods call), `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `HF_TOKEN` (write on `EduBotics-Solutions`), and with a RunPod rung in the ladder `RUNPOD_API_KEY` and `RUNPOD_TRAINER_IMAGE`. **`RUNPOD_TRAINER_IMAGE` is set by `release.yml`** (`ghcr.io/svendanilborodun/edubotics-trainer:<VERSION>@sha256:<digest>`, digest-pinned; anything else is refused at boot). Optional, with their defaults: `TRAINING_DISPATCH` (`legacy` | `managed`), `TRAINING_WORKER_SUPABASE_URL` (`SUPABASE_URL`), `R2_KEY_PREFIX` (`train/`), `RUNPOD_API_BASE`, `RUNPOD_LADDER` (the default: Community 13 cards → Secure 7 cards; `modal` only together with `MANAGED_MODAL_RUNG=1`, then last, appended when absent), `MANAGED_MODAL_RUNG` (`0`; `1` = emergency; any other value stops the boot — check the deploy result: a failed deploy leaves the previous instance serving), `RUNPOD_POD_PREFIX` (`edubotics-train`), `MAX_RUNPOD_PODS` (30), `TRAINING_START_DEADLINE_S` (1200), **`MANAGED_ACT_CAP_S`** (10800 = 3 h, allowed 3600 / 7200 / 10800 and nothing else — the boot refuses any other value; it is the ceiling of a reservation: a start reserves min(3, the cap's hours, the free hours, the member hours left) and the training's time limit is its reserved hours × 3600; pre-052 rows keep 12 600 s), `R2_KEEP_PREFIX` (`keep/`; must end with `/` and not overlap `R2_KEY_PREFIX`), `MODAL_TRAINING_MANAGED_FUNCTION_NAME` (`train_managed`).
5. **Never set on Railway: `TRAINING_SAVE_FREQ`, `TRAINING_MIRROR_EVERY`, `TRAINING_E2E`.** They exist for the CPU end-to-end stack only (20 / 40 with `TRAINING_E2E=1`); without `TRAINING_E2E=1` the boot refuses any value but 2 500 / 10 000, and with it every boot logs a WARNING.
6. **After 047 is live**, confirm on production that a running training's chart still moves (the page reads named columns only now).
7. **First managed run (owner E28: the paid checks were skipped, so this run is the gate; `docs/KNOWN-ISSUES.md`).** Outside lesson time, with your own account and dataset and about 3 000 steps:
   - set `TRAINING_DISPATCH=managed`, start the training, and read the API log: the rung, the step (`world` / `eea`, in the log line) and the data centre it placed on, the pod's `Host check:` line (review N6: `Host check:` is only in the pod's own log — RunPod `GET /v2/pods/{id}/logs`, Modal app logs — not in the API log or `training_log.txt`; a passing HF write check logs nothing either), steps/s, a checkpoint in R2 every 2 500 steps, the publish to `EduBotics-Solutions` and its read-back, the pod deleted at the end (RunPod console: no `edubotics-train-*` pod left);
   - **steps/s:** note the measured rate in the release note; the time limit is the training's reserved hours (at most 3 h), a slow card reaches it earlier and then publishes the last checkpoint;
   - **only if you intend to use the emergency switch:** one short run with `MANAGED_MODAL_RUNG=1` and `RUNPOD_LADDER='[{"rung":"modal"}]'` proves the prod Modal deploy (placement, `start` accepted = current lock and handler hashes, a checkpoint in R2); Modal's `Host check:` line reports a synthetic `tmp_free_b` of about 2^63, so the 30 GiB floor passes there but measures nothing (E41 T6, 2026-10-20); unset both afterwards;
   - **a pod's own exit:** if a pod whose main process exited stayed `EXITED` without restarting, set `entry.EXIT_AFTER_FAILED_SELF_STOP = True` in the next release;
   - anything wrong ⇒ `TRAINING_DISPATCH=legacy` (rollback step 2 below; it stops new trainings); a failed managed training charges only its counted training time (often 0), and a wrong charge is the admin's compensation (`POST /admin/credit-grants`).

8. **Before `TRAINING_DISPATCH=managed` (owner E49/E51): the pod-key check, on all three RunPod APIs.** `python robotis_ai_setup/runpod_training/e2e/pod_key_check.py --run --key-file <0600 file with the account key> --a-gpu <the cheapest Community GPU with stock> --cpu-flavor <id> --vcpu <n> --surfaces v2,v1,graphql` (outside lesson time; cents; after 2026-11-15 drop `v1` — the report marks it `retired`). Each surface is asked in its own documented shape (GraphQL reads `env` as `KEY=value` strings; REST v1 gets its own CPU-pod body). GraphQL has no documented CPU-pod create, so its create check asks for a GPU pod on pod A's card and cloud: a key that CAN create costs one more pod for seconds, and the launcher deletes it. Exit 0 (the key worked on its own pod and reached, changed, stopped or created NOTHING on any surface, pod A on COMMUNITY) is the go; exit 1 (any `allowed` on any surface) means the key is not confined — `TRAINING_DISPATCH` stays `legacy` and the owner decides the design change; exit 2 (any `unknown`: a 400/429/5xx answer proves nothing) — repeat; exit 3 (no key in the pod) — report to the owner; if no Community card can be had for 30 min, run it with `--a-cloud SECURE` (exit 1 there is as final), then keep a Secure-only `RUNPOD_LADDER` until a Community run exits 0.

**Emergency: the Modal rung.** RunPod has no stock anywhere or is down for hours: (1) make sure the Modal prod app carries THIS release (`modal deploy modal_app.py` from the tagged commit; a stale deploy answers `wrong_image` on every Modal attempt, and attempt 4 is Modal-only under the switch); (2) set `MANAGED_MODAL_RUNG=1` (nothing else needs changing; check that the deploy succeeded; a Modal attempt is accounted like a RunPod one). Since 052 the owner keeps `edubotics-training` stopped, so step (1) is never optional; managed rows then take Modal after 2 min without a RunPod placement; (3) when RunPod is back, unset it. The boot logs a WARNING on every start while it is set.

**Privacy text for schools** (owner E48): update the schools' privacy information with KNOWN-ISSUES' German sentence: RunPod and Cloudflare are processors, and trainings normally run outside the EU, on third-party hardware.

**Rollback, in this order** (never reorder: the v2.36.0 API's `budget_for` reads the GLOBAL cap, not a row's reservation or `lost_seconds`, so `legacy` comes first and the rows drain before the API goes back):

1. optionally `MANAGED_MODAL_RUNG=1` after a `modal deploy modal_app.py` at the running release (the Modal rung returns as the last rung and attempt 4 is Modal-only; with `RUNPOD_LADDER='[{"rung":"modal"}]'` managed rows place on Modal only), or a Secure-only `RUNPOD_LADDER` (no third-party hosts);
2. `TRAINING_DISPATCH=legacy` — stops new trainings (the managed loops keep driving every managed row that is still active) (review N4: `railway variable delete` does NOT deploy — either set it with `railway variables --set TRAINING_DISPATCH=legacy`, or after a delete run `railway redeploy` and confirm `/health.boot_completed_at` advanced and the new boot logs no „managed training loops started");
3. wait until no `ledger` and no managed row is `queued|running|cancel_requested` (a capped or stopped publish is `running` too); keep the R2, RunPod and HF variables until then;
4. the API and the teacher web back to v2.36.0. First reset `RUNPOD_TRAINER_IMAGE` to that release's digest (`docker buildx imagetools inspect ghcr.io/svendanilborodun/edubotics-trainer:<that VERSION>`) with `railway variables … --skip-deploys`: else the older API's `TRAINING_LOCK_SHA256` / `TRAINING_HANDLER_SHA256` meet the newer image and every RunPod attempt ends `wrong_image`. The v2.36.0 API boots against 052 (its probe finds every function it names with its old argument set) and presigns PUTs without the checksum, which 052 does not need. **The student React stays at the newer release on every robot until its images are rolled back too, and under a v2.36.0 API its Start key stays disabled** (the old `/trainings/quota` answers no hour fields, so the page reads 0 hours): roll the robots back as well (install the older `.exe` AND pin `EDUBOTICS_IMAGE_TAG`) or accept that nobody starts until the API is forward again;
5. the 052 rollback (`rollback/20261028120000_052_credit_ledger_rollback.sql`; it refuses while a `ledger` row is active, keeps the ledger tables and restores the old credit functions; it also drops `mark_checkpoint_ready` and restores 049's `claim_checkpoint_mirror`, which is one more reason the API goes back first: a v2.37.0 API on the rolled-back database fails its own probe and its ckpt-done could mark no checkpoint ready; 052 is not re-runnable after it);
5′. `TRAINING_DISPATCH=managed` again: under v2.36.0 that is the RunPod path with derived credits, while `legacy` would send every ACT start to the stopped Modal `train` app, where it fails;
6. only if needed, further back as before: the 051 rollback, the 050 rollback (`rollback/20261021120000_050_training_continuation_rollback.sql`), then the 049 rollback (`rollback/20261020130000_049_managed_training_rollback.sql`; both refuse while a managed row is active), then the 047 rollback (`rollback/20261020120000_047_hide_worker_token_rollback.sql`; it re-opens the worker-token refund hole).

A pre-feature API must never run while a managed row is active: its reconcilers fail such rows. Since 052 only ACT is trained and the owner keeps the Modal app `edubotics-training` stopped (no new row reaches it; `train_managed` is dormant while the switch is off; a `modal deploy` comes before any switch flip). The region and placement rules travel with the API image (no variable restores E18's EU-only refusals).

### Known gotcha — RAILPACK fallback on git push

Railway sometimes ignores the `cloud_training_api/Dockerfile` and falls back to its RAILPACK auto-builder, which fails almost immediately (Cloud API has Python deps + custom port handling that RAILPACK can't infer). Symptoms in the deploy log: `Failed to create code snapshot` or `Using RAILPACK builder`. This has bitten us repeatedly: commits `d3e6e2c`, `51010c7`, `94f1b5d` in the v2.3.0 follow-up were all just "re-trigger Railway deploy" no-op commits to force a fresh snapshot attempt.

**Workaround:** push a trivial edit to anything under `robotis_ai_setup/cloud_training_api/` (e.g., add a blank line to `Dockerfile`) and Railway picks up the DOCKERFILE builder correctly. Watch the deploy log for `Using DOCKERFILE` not `Using RAILPACK`.

**Systemic fix (deferred):** set `rootDirectory: robotis_ai_setup/cloud_training_api` on the Railway service dashboard. This bypasses the snapshot-context bug but needs dashboard access and risks breaking the teacher-web sibling service.

**Teacher-web React build** (separate Railway service):
```bash
cd physical_ai_tools/physical_ai_manager
./scripts/railway-deploy.sh   # railway up --path-as-root .
```
Its one runtime variable, `EDUBOTICS_CSP_ENFORCE`, switches the Content
Security Policy from report-only to enforced (section 3d).

## 3c. The cloud API's login check, the admin's second factor and the edge (2026-11-10)

The API verifies every access token LOCALLY against the project's public
signing keys (`<SUPABASE_URL>/auth/v1/.well-known/jwks.json`, fetched at boot
and every 10 minutes); admin routes need a two-factor (TOTP) session; new and
reset passwords need 12 characters (teachers, admins) or 8 (students). No new
required variable. Two optional Railway variables, both unset by default:

- `AUTH_VERIFY_MODE` — `local` (default) or `remote`: the one-variable
  rollback to Supabase's own per-request check. Anything else stops the boot.
- `TRUSTED_PROXY_HOPS` — how many proxies in front of the API append to
  `X-Forwarded-For` (default 1, Railway's edge; 1–5, anything else stops the
  boot). Change it only if the check in step 5 below says so.

**Owner steps, in this order.** „Before the deploy" means BEFORE THE MERGE to
`main`: W2 deploys the API on the push that merges (see `MERGE-CHECKLIST.md`, which also covers the W2 → W3
window and the first-use TOTP enrolment).

1. **Before the deploy: the project's CURRENT signing key must be the ES256
   key.** Supabase dashboard → Project Settings → JWT Keys. If the ES256 key is
   only „standby" and the legacy HS256 secret is „current", either rotate
   (the ES256 key becomes current; tokens issued under HS256 in the last hour
   are refused once and those users log in again) or set
   `AUTH_VERIFY_MODE=remote` on Railway with `--skip-deploys` until you have
   rotated. Symptom of a mismatch after the deploy: every request answers 401
   and the Railway log says „access token refused: algorithm HS256 …"; set
   `AUTH_VERIFY_MODE=remote` to recover.
2. **Supabase dashboard → Authentication → Multi-Factor (MFA):** TOTP enabled
   (both „enroll" and „verify"). Without it the admin's enrolment screen says
   „Die Zwei-Faktor-Anmeldung ist für dieses Projekt nicht freigeschaltet".
3. **Supabase dashboard → Authentication → Sign In / Providers → Email →
   Password:** minimum length **8** (the students' minimum; nothing in the app
   changes one's own password, but the Auth API lets every signed-in user do
   it, and this is the floor there). The API's 12 for teachers and admins
   applies to every password it sets. **Leaked password protection**
   (HaveIBeenPwned) needs the Pro plan or above; switch it on if the plan has
   it — Supabase then refuses a known-leaked password on create and reset too,
   and the API answers „Dieses Passwort wird nicht akzeptiert …". Optional
   character requirements (digits, symbols) apply to the API's passwords too.
4. **After the deploy:** the Railway log shows „JWKS: 1 signing key(s)
   cached" once per boot; a teacher and a student can log in and load their
   pages; `curl -s -o /dev/null -w '%{http_code}\n' -H 'Authorization: Bearer x.y.z'
   https://scintillating-empathy-production-1068.up.railway.app/me` answers 401.
5. **The client-address check** (from a laptop, not from a school network):
   ```bash
   for i in $(seq 1 35); do curl -s -o /dev/null -w '%{http_code}\n' -H "X-Forwarded-For: 10.0.0.$i" https://scintillating-empathy-production-1068.up.railway.app/inference/usage; done
   ```
   The first 30 answers are 401 (no token); from the 31st a 429 must appear
   despite the rotating header (`GET /inference/usage` allows 30 a minute per
   client address when no token is sent). Then find the line „Rate limit hit
   on GET /inference/usage from <ip>" in the Railway log: `<ip>` must be the
   laptop's public address (`curl -s https://ifconfig.me`). No 429, or a
   10.0.0.x address there, means the API trusts the client's header (stop and
   report); a Railway-internal address means more than one proxy hop (set
   `TRUSTED_PROXY_HOPS` accordingly and repeat).
6. **The first admin login enrols the second factor:** the teacher web shows a
   QR code (and the secret as text) after the password; scan it with an
   authenticator app (Microsoft Authenticator, Google Authenticator, 2FAS)
   and type the 6-digit code. Every later login asks for a code. A fresh
   admin created with `scripts/bootstrap_admin.py` (12 characters) does the
   same at its first login.
7. **Log alert (later, with monitoring):** match `EDUBOTICS_ADMIN_SESSION` in
   the API log — one WARNING per admin session, naming the user id only.

**Recovery from a lost authenticator** (the admin cannot log in to the admin
area any more): Supabase dashboard → Authentication → Users → the admin's
user → remove its MFA factor (or, in the SQL editor,
`delete from auth.mfa_factors where user_id = '<admin uuid>';`). Then sign the
admin out everywhere (Users → the user → sign out / revoke sessions) and let
them log in again: the teacher web asks them to enrol a new factor. Verify who
asks before removing a factor: removing it is what an attacker with the
password would need.

**Rollback:** `AUTH_VERIFY_MODE=remote` restores the per-request check (and the
old hashed-token rate-limit key) without a code rollback. A code rollback to
the previous API of THIS branch's merge (v2.37.0) needs the database first: the
rollbacks 059 → 053, newest first, because that API exits at start-up on a
database with 053 and its account creation fails under 058
(`MERGE-CHECKLIST.md` section 5). The teacher web's gate then only adds the
second factor for admins, and the older API accepts an `aal2` token too.

## 3d. Legal links and the teacher web's Content Security Policy (2026-11-11)

Both are runtime switches on Railway: no code change, no rebuild. A variable
change makes Railway redeploy the service; the next container start reads it.

**The legal links — cloud API service** (`scintillating-empathy`). Three
optional variables, each the absolute `https://` address of a published page:

| Variable | Link text everywhere |
|---|---|
| `LEGAL_IMPRESSUM_URL` | „Impressum" |
| `LEGAL_DATENSCHUTZ_URL` | „Datenschutz" |
| `LEGAL_NUTZUNGSBEDINGUNGEN_URL` | „Nutzungsbedingungen" |

- Unset or empty: that link is shown nowhere. Set: it appears on the login
  card (student app, teacher web, the Pi claim screen), the teacher and admin
  dashboards, the admin's two-factor screen, the Pi System page and under the
  Windows GUI's version line, read from `GET /version`.
- **An invalid value stops the API's boot** (not `https://`, no host,
  credentials, whitespace, quotes, angle brackets, backslash, backtick, longer
  than 2048 characters): the new deploy fails its `/health` gate and the old
  one keeps serving; the Railway log names the variable. Fix the value.
- Set them only once the page really exists (an Impressum link to a 404 is
  worse than none), all three at once if possible (`railway variables --set …`
  three times with `--skip-deploys` on the first two, so only one redeploy
  runs).
- Check: `curl -s https://<api>/version` shows the three `legal_*_url` keys;
  `curl -sI -H 'Origin: http://192.168.1.23' https://<api>/version | grep -i
  access-control-allow-origin` answers `*` (how a Pi's page reads it).

**The CSP — teacher-web service.** The image always sends a policy built from
its own `REACT_APP_CLOUD_API_URL` and `REACT_APP_SUPABASE_URL`; one runtime
variable decides how:

- `EDUBOTICS_CSP_ENFORCE` unset (the default), or anything but `1`:
  `Content-Security-Policy-Report-Only`. Nothing is blocked; every violation a
  browser sees is POSTed to `/csp-report` and logged by nginx as one line
  `<time> csp-report {…}` in the teacher-web service's Railway logs. A REFUSED
  request to `/csp-report` (another method, a body over 16 KB, over the rate)
  writes no such line, but nginx still writes its usual error line for it —
  `access forbidden by rule`, `client intended to send too large body` or
  `limiting requests`, with `request: "POST /csp-report HTTP/1.1"` and the
  client address, never a body. That line is not a report.
- `EDUBOTICS_CSP_ENFORCE=1`: `Content-Security-Policy`. Violations are blocked
  (and still reported).
- **The one-week rule:** after the first deploy with the policy, watch the
  teacher-web logs for `csp-report {` (the report lines; a refused request's
  error line does not match) for one week of normal teacher and admin
  use (a template edited, a student's program opened, the admin's two-factor
  login). No line in that week → set `EDUBOTICS_CSP_ENFORCE=1`. A line → read
  its `violated-directive` and `blocked-uri`; a legitimate source is a code
  change to `scripts/csp-policy.mjs` (never a wildcard), and the week starts
  again. A report from a browser extension (`blocked-uri` `chrome-extension`
  / `moz-extension`) is noise.
- Check after each deploy: `curl -sI https://<teacher web>/ | grep -i
  content-security` names the expected header.
- **Rollback:** unset `EDUBOTICS_CSP_ENFORCE` (back to report-only). The
  policy's API and Supabase hosts follow the build variables automatically;
  a new Supabase project or API host needs nothing else.

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

GUI 2.2.4 auto-pulls on every launch: TCP probe to Docker Hub (5 s offline-skip) → manifest-digest pre-check → only pulls if remote ≠ local → persists last-pull timestamp to `%LOCALAPPDATA%/EduBotics/.last_image_pull.json`. Banner past `IMAGE_FRESHNESS_WARN_DAYS=14`. Disable with `EDUBOTICS_SKIP_AUTO_PULL=1`. As of v2.5.4 the GUI ALSO pulls the pinned image tag at environment start (`_compose_pull` before `up --force-recreate`, `--ignore-pull-failures`), so clicking "Umgebung starten" never recreates onto a stale local image even when the PC was offline at GUI launch.

**To force re-install** of the `.exe` itself: bump `VERSION` + `installer/robotis_ai_setup.iss AppVersion` + `gui/app/constants.py` fallback together, then push a `vX.Y.Z` tag. `release.yml` builds + attaches the `.exe` (W5) and then W6 `publish-gui-version` sets Railway `GUI_VERSION` / `GUI_DOWNLOAD_URL` / `GUI_RELEASE_REPO` automatically (after the asset exists). No manual Railway bump needed on the tagged path; for an off-pipeline hotfix you can still set those three vars by hand.

---

## 4b. Classroom Jetson — retired (2026-11-09)

The classroom Jetson Orin Nano is retired: no `-jetson` images are built, the
agent (`jetson_agent/`) and its runbook are deleted, the cloud API has no
`/jetson/*` routes and migration 053 drops its database objects (§1). Owner
follow-up on Docker Hub / GHCR: the `open-manipulator-jetson`,
`physical-ai-server-jetson` and `physical-ai-server-jetson-base` repositories
can be deleted. **`open-manipulator-jetson-base` must stay**: it is the Orange
Pi's open_manipulator base (`docs/arm64_base/README.md`).

**Owner action once this release is live, in this order:**

1. Revoke the Hugging Face token that `EDUBOTICS_JETSON_HF_TOKEN` held, at
   huggingface.co (Settings → Access Tokens, the account that created it).
2. Delete the three Railway variables `EDUBOTICS_JETSON_HF_TOKEN`,
   `SUPABASE_JWT_ALGORITHM` and `SUPABASE_JWT_SECRET`.
3. If `SUPABASE_JWT_ALGORITHM` was ever `HS256` in production, rotate the
   Supabase JWT secret, at a time when no managed training is running (a
   running attempt keeps the anon key it was started with; once that key
   stops working the attempt is lost and requeued, and its GPU time is paid
   for nothing). The legacy JWT secret also signs the `anon` and
   `service_role` keys, so rotating it replaces both, and every holder must
   move with it (every reader in the repository, checked 2026-11-09):
   - **anon key:**
     - Railway, cloud API service: `SUPABASE_ANON_KEY` (the API hands it to
       each managed training attempt, RunPod pod or Modal `train_managed`);
     - Railway, teacher-web service: the build variable
       `REACT_APP_SUPABASE_ANON_KEY` (Railway passes it to `Dockerfile.web`'s
       `ARG`; the teacher web needs a rebuild);
     - the GitHub Actions secret `SUPABASE_ANON_KEY` (W4 bakes it into the
       student and Orange Pi manager images, so those need a release);
     - the Modal secrets `edubotics-inference-secrets` (Cloud-Inferenz) and
       `edubotics-training-secrets` (the stopped legacy `edubotics-training`
       app; `modal_app.py` requires `SUPABASE_ANON_KEY` from it);
     - the anon key baked into every shipped React bundle (student, Orange Pi,
       teacher web), replaced only by the rebuilds above;
     - operators' local copies: `cloud_training_api/.env`, the gitignored
       `docker/.env.build` (an operator's note of the build key), and any copy
       made from `docker/.env.template` or `physical_ai_manager/.env.example`
       for a local `build-images.sh` or web build (both read the key from the
       environment they run in).
   - **service_role key:**
     - Railway, cloud API service: `SUPABASE_SERVICE_ROLE_KEY`;
     - operators' local copies: `cloud_training_api/.env` (read by
       `scripts/bootstrap_admin.py` and a local API run) and the environment
       `tools/feetech_provision.py` is run with;
     - the GitHub Actions secret `SUPABASE_SERVICE_ROLE_KEY`, if it still
       exists: no workflow reads it since W2's import probe carries a dummy
       key (2026-11-05), so delete it rather than update it.

   Plan it with Supabase's signing-keys guide (migrate to JWT signing keys and
   the publishable/secret API keys, then revoke the legacy secret), which
   avoids the downtime of a plain legacy rotation.

---

## 5. git push

CI runs `.github/workflows/ci.yml`'s validator jobs, among them: `python-tests`, `shell-lint`, `compose-validate`, `overlay-guard`, `modal-import-validate`, `teacher-web-build-validate`, `manager-build-validate`, `tutorials-validate`, `interfaces-validate`, `nginx-validate`, `german-strings-lint`. Most catch a class of regression you'd otherwise discover in production.

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

# Image digests changed:
docker buildx imagetools inspect nettername/physical-ai-manager:latest
docker buildx imagetools inspect nettername/physical-ai-server:latest
docker buildx imagetools inspect nettername/open-manipulator:latest
```

Then launch the GUI on a Windows box with a robot: toolbar renders, Galerie populates, Lernpfad lists tutorials, breakpoint pause/continue works, Sensoren panel updates at ~5 Hz.

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

---

## Final pre-ship checklist (run through these before declaring done)

- [ ] **Migrations** applied to live Supabase? (`mcp__claude_ai_Supabase__list_migrations` shows the new file; or Supabase Studio → Database → Migrations)
- [ ] **Modal** functions redeployed if any `modal_training/*.py` changed? (`modal deploy modal_training/<app>.py` returned a fresh URL)
- [ ] **Railway Cloud API** deploy status SUCCESS on the commit you just pushed? (`mcp__railway__list-deployments` or dashboard)
- [ ] **Railway env vars** matched against the new code paths? (every new env var the route reads is set; no required env left at default)
- [ ] **Docker Hub** images pushed with the new content? (latest manifest digest matches the build you just ran; verify with `docker buildx imagetools inspect`)
- [ ] **GitHub release** matches `VERSION` file? (The release lives in the public repository named by the Actions variable `RELEASES_REPO` once set, else in this one: `docs/deploy/PRIVATE-REPOSITORY.md`. On the tagged path, `release.yml` W5 attaches `EduBotics_Setup.exe` and W6 `publish-gui-version` sets Railway `GUI_VERSION` / `GUI_DOWNLOAD_URL` / `GUI_RELEASE_REPO` automatically + verifies `/version`; only a manual off-pipeline release needs `gh release create v<version> ./installer/output/EduBotics_Setup.exe` AND a hand-set of those three Railway vars — without them the auto-update gate is invisible to existing student installs)
- [ ] **Smoke test** golden path: a real student logs in via the v<version>.exe → records, or runs a model „In der Cloud" → no console errors. The image-build CI's `manager-build-validate` smoke is necessary but not sufficient.
- [ ] **Teacher smoke**: a real teacher logs in via Railway teacher-web → opens a classroom → opens a student's training history → no console errors.
- [ ] **CLAUDE.md** §0 last-verified line bumped + the load-bearing section that changed has a fresh entry (§3 if architecture, §7 if Cloud API endpoint, §9 if migration, §11 if GUI, §12 if React, §13 if workflow, §16 if glossary).

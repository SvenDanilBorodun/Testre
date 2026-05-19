# EduBotics Deploy Pipeline — Operator Runbook

This is the single source of truth for operating the EduBotics CI/CD
pipeline. It replaces the manual-deploy ritual documented in earlier
versions of `CLAUDE.md`.

For architecture/invariants see `CLAUDE.md`. This file is the
"what to type when" companion.

---

## TL;DR

```
Edit code → git push to main →  CI runs the right workflow → done.
Edit code → git tag vX.Y.Z   →  release.yml chains all 5 workflows.
```

The four CI-driven workflows in `.github/workflows/`:

| Workflow | Triggers on changes to | What it does |
|----------|------------------------|--------------|
| `supabase-migrate.yml` | `supabase/migrations/**` | Applies migrations to production (or PR-branch for PRs) |
| `railway-deploy-cloud-api.yml` | `cloud_training_api/**` | Schema-probe → `railway up` → `/health` gate |
| `railway-deploy-teacher-web.yml` | `physical_ai_manager/**` | Validate → `railway up` teacher-web → `/version.json` gate |
| `docker-publish.yml` | `docker/**`, `open_manipulator/**`, `physical_ai_tools/**` | Native amd64 + arm64 builds → push → smoke-test |

Plus `release.yml` which chains all four on `v*.*.*` tag pushes.

**Modal is deployed manually** — see "Manual Modal deploys" section below.

---

## One-time setup (operator runs this once, ever)

### Step 1: Mint the 12 GitHub Actions secrets

Settings → Secrets and variables → Actions → New repository secret:

| Secret | Source / value |
|---|---|
| `DOCKERHUB_USERNAME` | `nettername` |
| `DOCKERHUB_TOKEN` | hub.docker.com → Account Settings → Security → New Access Token, scope **Read/Write/Delete** |
| `RAILWAY_TOKEN` | `railway tokens create --project scintillating-empathy --name gha-deploy` (run from your laptop where Railway CLI is logged in) |
| `MODAL_TOKEN_ID` | Copy from existing Railway env (`railway variables` shows it; starts `ak-`) |
| `MODAL_TOKEN_SECRET` | Copy from existing Railway env (starts `as-`) |
| `SUPABASE_ACCESS_TOKEN` | supabase.com → Account → Access Tokens → Generate new token |
| `SUPABASE_DB_URL` | Supabase Dashboard → Project Settings → Database → Connection string → URI (pooler, port 6543). Format: `postgresql://postgres.fnnbysrjkfugsqzwcksd:<svc-pw>@aws-0-eu-west-1.pooler.supabase.com:6543/postgres` |
| `SUPABASE_PROJECT_REF` | `fnnbysrjkfugsqzwcksd` (literal) |
| `SUPABASE_URL` | `https://fnnbysrjkfugsqzwcksd.supabase.co` (literal) |
| `SUPABASE_SERVICE_ROLE_KEY` | Copy from existing Railway env |
| `SUPABASE_ANON_KEY` | Copy from existing Railway env or from `robotis_ai_setup/docker/.env.build` |
| `REACT_APP_CLOUD_API_URL` | `https://scintillating-empathy-production-1068.up.railway.app` (literal) |

### Step 2: Register the Supabase baseline as already-applied

This is the **bridge step**. The squashed baseline lives at
`robotis_ai_setup/supabase/migrations/00000000000000_baseline.sql` and
represents the schema that's already on production. We tell the CLI
"production already has this; never re-apply."

```bash
# Run once from your terminal (you have CLI auth):
cd robotis_ai_setup/supabase
supabase --version   # require 1.187+ ; brew upgrade supabase if needed
supabase link --project-ref fnnbysrjkfugsqzwcksd
supabase migration repair --status applied 00000000000000
supabase migration list   # verify both local and remote show 00000000000000 applied
```

If `migration list` shows the baseline as "local only", the repair
failed — fix before pushing.

### Step 3: Enable leaked-password protection in Supabase Auth

supabase.com → Project → Authentication → Settings → "Leaked Password
Protection" → toggle ON. This closes the 11th security advisor (the
only one not handled by `20260519120000_security_advisor_fixes.sql`).

### Step 4: Pre-flight sanity checks

Run before your first push to main:

```bash
# 1. actionlint catches expression / shellcheck issues in workflows
brew install actionlint   # if not installed
actionlint .github/workflows/*.yml

# 2. CLI auth works
modal token current         # should show svendanilborodun
railway whoami              # should show your email
supabase projects list      # should include fnnbysrjkfugsqzwcksd

# 3. Cleanup scripts dry-run (no mutation)
DOCKERHUB_USERNAME=nettername DOCKERHUB_TOKEN=<your-PAT> \
  bash tools/docker-hub-cleanup.sh
bash tools/modal-cleanup.sh

# 4. Git diff one more read
git status
git diff --stat
```

---

## Daily use: deploying a code change

### Single-surface change (e.g. fix a FastAPI route)

```bash
git checkout -b fix/some-bug
# edit cloud_training_api/app/routes/foo.py
git add -A && git commit -m "fix: …"
git push -u origin fix/some-bug
gh pr create
# ci.yml validates the change.
# Merge to main:
gh pr merge --squash
# railway-deploy-cloud-api.yml fires on merge:
#   1. schema-probe (read-only against production Supabase)
#   2. railway up   (deploys new code to scintillating-empathy service)
#   3. health-gate  (polls /health until 200)
```

Watch Actions tab. ~3-4 min total.

### Multi-surface change (schema + API + UI)

Same flow. On merge to main, all path-filter-matching workflows fire in
parallel. Sequencing matters here:

```
Schema change       → supabase-migrate fires
FastAPI change      → railway-deploy-cloud-api fires
React change        → railway-deploy-teacher-web fires
Image source change → docker-publish fires
```

If the API change depends on a new RPC the migration adds, the FastAPI
deploy's `schema-probe` job will refuse if the migration hasn't landed
yet. Re-run the FastAPI deploy after Supabase finishes — `gh workflow
run railway-deploy-cloud-api.yml -f reason="re-run after Supabase"`.

For coordinated multi-surface releases, prefer the tag path (next).

### Coordinated release (full stack, golden order)

```bash
# Bump VERSION file + installer .iss + gui/constants.py if needed

# If the release touches Modal (modal_training/**), deploy it FIRST:
cd robotis_ai_setup/modal_training
modal deploy modal_app.py
modal deploy vision_app.py
cd -

# Then tag and push
git tag v2.3.2 -m "release: v2.3.2"
git push --tags
# release.yml fires:
#   W1 supabase-migrate     →
#   W2 railway-deploy-cloud-api →
#   W3 railway-deploy-teacher-web →
#   W4 docker-publish
# AND release-installer.yml fires in parallel (builds EduBotics_Setup.exe)
```

Total ~20 minutes (CI chain only — Modal deploy adds 2-3 min if needed).
Watch the Actions tab. The chain stops on first failure; subsequent
surfaces don't deploy.

### Manual Modal deploys

Modal deploys are intentionally NOT in CI. Run from your terminal when
`modal_training/**` changes:

```bash
cd robotis_ai_setup/modal_training
modal deploy modal_app.py       # ~30 sec — registers new app version
modal deploy vision_app.py      # ~30 sec
modal run modal_app.py::smoke_test     # optional: verify training app spawns
modal run vision_app.py::smoke_test    # optional: verify vision app spawns
```

**Important:** running training jobs that were spawned before the deploy
continue to use the old image. New `start_training_safe` RPC calls (from
students via the GUI) use the new image. Always run smoke tests after
deploy — they're the only post-deploy verification.

**Rollback:** Modal has no version history. Roll back by checking out a
known-good SHA and re-running `modal deploy`:

```bash
git checkout <good-sha> -- robotis_ai_setup/modal_training/
cd robotis_ai_setup/modal_training
modal deploy modal_app.py
modal deploy vision_app.py
git checkout HEAD -- robotis_ai_setup/modal_training/
```

---

## Daily use: PR with schema changes

```bash
git checkout -b feat/new-table
# Create new migration:
#   robotis_ai_setup/supabase/migrations/$(date -u +%Y%m%d%H%M%S)_some_change.sql
git add -A && git commit -m "feat: add some_table"
git push
gh pr create
```

On PR open, `supabase-migrate.yml` fires `apply-branch`:
1. Creates ephemeral Supabase Branch `pr-N` (non-persistent, auto-paused after inactivity)
2. Applies your new migration to that branch
3. Comments on the PR with the branch URL

You can connect to the branch in Supabase Dashboard, manually test, verify the migration looks right.

On PR close (merge or close-without-merge), `teardown-branch` deletes the branch.

After merge to main, `apply-production` applies the migration to live.

---

## Rollback

### Supabase rollback

```bash
gh workflow run supabase-migrate.yml \
  -f target=production \
  -f rollback_to=021_workgroup_memberships_realtime_and_owner_check \
  -f reason="break in 022"
```

The workflow applies `supabase/rollback/<rollback_to>.sql` via psql.
Refuses if the file doesn't exist.

### Modal rollback

```bash
# Redeploy a known-good SHA:
gh workflow run modal-deploy.yml -f app=both -f ref=<old-good-sha> -f reason="rollback"
```

Modal keeps no version history; rollback is a re-deploy.

### Railway rollback (cloud-api or teacher-web)

```bash
# Easiest path: Railway dashboard
# Project → Service → Deployments → Previous Deployment → Redeploy

# CLI path:
railway redeploy --service scintillating-empathy --environment production
# (defaults to redeploying the previous successful build)
```

### Docker image rollback (students)

```bash
# Re-tag :latest to a known-good SHA tag
docker pull nettername/physical-ai-server:<good-sha>
docker tag nettername/physical-ai-server:<good-sha> nettername/physical-ai-server:latest
docker push nettername/physical-ai-server:latest
# Same for any other affected images.
# Student GUIs detect the new RepoDigest on their next 5-min poll and pull.
```

---

## Cleanup (run after first successful release)

```bash
# Docker Hub: delete 5 junk repos + 8 *-dirty tags
DOCKERHUB_USERNAME=nettername DOCKERHUB_TOKEN=<your-PAT> \
  bash tools/docker-hub-cleanup.sh           # dry-run preview
DOCKERHUB_USERNAME=nettername DOCKERHUB_TOKEN=<your-PAT> \
  bash tools/docker-hub-cleanup.sh --execute # actually delete

# Modal: delete 4 stale secrets + 3 stale volumes + 1 stale test app
bash tools/modal-cleanup.sh                  # dry-run preview
bash tools/modal-cleanup.sh --execute        # actually delete
```

Don't run these before the first successful release — if anything goes
wrong and you need to roll back to a `*-dirty` tag temporarily, you
want them still available.

---

## Troubleshooting

### CI failure — "workflow_call not defined"

Cause: a child workflow that `release.yml` invokes is missing its
`on: workflow_call:` trigger.
Fix: check the file has the trigger; all 5 surface workflows in this
repo do.

### CI failure — Supabase migration fails on PR

Cause: usually a syntax error in the new migration. The `validate` job
in `supabase-migrate.yml` runs `pglast` parse first; if it passes
locally but fails on the branch, the branch is in a state your local
DB isn't.
Fix: connect to the PR-branch URL from the PR comment, inspect, fix.

### CI failure — `railway up` returns "service not linked"

Cause: `RAILWAY_TOKEN` is project-scoped to a different project, or
the `--service` name changed.
Fix: `railway whoami && railway list && railway service` — verify
project/service names match the workflow.

### CI failure — `docker buildx imagetools create` 429

Cause: Docker Hub rate-limited the retag step.
Fix: workflow has retry-with-backoff built in (3 attempts). If all 3
fail, re-run the workflow.

### Student GUI says "Update fehlgeschlagen"

Cause: Docker Hub pull interrupted (school Wi-Fi blip).
Fix: student re-tries from the GUI's update toast. Docker resumes
from where it left off; partial layers are cached locally.

### "/health" returns 503 after a deploy

Cause: `_validate_required_schema()` failed — Railway sees a schema
that doesn't match what the new FastAPI code expects.
Fix: check Railway logs; usually means a Supabase migration didn't
land (W1 should have run first). Roll back the FastAPI deploy via
Railway dashboard, then re-run W1, then re-run W3.

---

## Reference: workflow trigger matrix

| Event | Workflows fired |
|---|---|
| `push` to main, paths match `supabase/migrations/**` | `supabase-migrate.yml::apply-production` |
| `push` to main, paths match `modal_training/**` | `modal-deploy.yml` |
| `push` to main, paths match `cloud_training_api/**` | `railway-deploy-cloud-api.yml` |
| `push` to main, paths match `physical_ai_manager/**` | `railway-deploy-teacher-web.yml` |
| `push` to main, paths match docker / open_manipulator / physical_ai_tools | `docker-publish.yml` |
| `push` of `v*.*.*` tag | `release.yml` (chains all 5) + `release-installer.yml` (builds .exe) |
| `pull_request` opened/synchronize/reopened, paths match `supabase/migrations/**` | `supabase-migrate.yml::apply-branch` (creates ephemeral Supabase Branch) |
| `pull_request` closed, paths match `supabase/migrations/**` | `supabase-migrate.yml::teardown-branch` (deletes the branch) |
| Any push or PR | `ci.yml` (10 validator jobs — pre-existing) |
| `workflow_dispatch` on any of the 5 surface workflows | Manual redeploy with optional `reason` audit string |
| `workflow_dispatch` on `docker-publish.yml` with `cleanup_dirty_tags: true` | Sweeps `*-dirty` tags from Docker Hub |

---

## Reference: required GitHub Actions secrets

12 secrets (Step 1 above). Workflows reference these via `${{ secrets.X }}`.

For `release.yml` to invoke child workflows, it uses `secrets: inherit`
which only works because each child has a `workflow_call:` trigger.
Don't remove those triggers.

---

## Reference: production endpoints

- Cloud API: `https://scintillating-empathy-production-1068.up.railway.app`
- Teacher web: `https://teacher-web-production.up.railway.app`
- Supabase: `https://fnnbysrjkfugsqzwcksd.supabase.co` (region eu-west-1)
- Modal apps: `edubotics-training`, `edubotics-vision` in workspace `svendanilborodun`
- Docker Hub: `docker.io/nettername/{open-manipulator,physical-ai-server,physical-ai-manager}` (amd64) + same with `-jetson` suffix (arm64)

---

## When NOT to use this pipeline (emergency-only)

For genuine emergencies where CI is unavailable or you need
out-of-band hotfix:

| Surface | Emergency command |
|---|---|
| Supabase | `supabase db push` after `supabase link --project-ref fnnbysrjkfugsqzwcksd`, OR `psql $SUPABASE_DB_URL -f rollback/NNN_*.sql` |
| Modal | `cd robotis_ai_setup/modal_training && modal deploy modal_app.py vision_app.py` |
| Railway cloud-api | `cd robotis_ai_setup/cloud_training_api && railway up . --service scintillating-empathy --environment production --path-as-root --ci` |
| Railway teacher-web | `bash physical_ai_tools/physical_ai_manager/scripts/railway-deploy.sh` |
| Docker images | `cd robotis_ai_setup/docker && SUPABASE_URL=... SUPABASE_ANON_KEY=... CLOUD_API_URL=... ./build-images.sh` |

After every emergency deploy, **document what you did in a PR** that
brings the repo state in line with what's running in prod. Otherwise
the next CI deploy will revert your hotfix.

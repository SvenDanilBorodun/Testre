# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**EduBotics** — a vertically integrated educational stack for teaching Physical AI on **ROBOTIS OpenMANIPULATOR-X** arms to **German-speaking students**. Student lifecycle: install `.exe` → setup wizard → record demos (ROS 2) → train policy (Modal cloud GPU) → inference → optional Roboter Studio (Blockly authoring + classical CV).

Students run Windows 11 PCs (no GPUs). Training runs on Modal NVIDIA L4. The product ships as a single `.exe` that installs a bundled WSL2 Ubuntu 22.04 distro named `EduBotics` containing Docker Engine + three containers — **no Docker Desktop dependency**. A web dashboard for teachers/admins is a separate Railway deploy.

Single git repo, no submodules. ROBOTIS upstream (`open_manipulator/`, `physical_ai_tools/`) is absorbed as plain directories.

## Big-picture architecture

10 layers, top to bottom:

1. **Windows installer + WSL2 rootfs** — `robotis_ai_setup/installer/` (Inno Setup + 9 PowerShell scripts), `robotis_ai_setup/wsl_rootfs/` (Ubuntu 22.04 + Docker 27.5.1 pinned)
2. **Windows tkinter GUI (`EduBotics.exe`)** — `robotis_ai_setup/gui/` (PyInstaller); wraps every Docker call as `wsl -d EduBotics -- docker …`
3. **Robot-arm bringup (ROS 2 Jazzy + Dynamixel)** — `open_manipulator/`, our overlay in `robotis_ai_setup/docker/open_manipulator/`
4. **Docker Compose (3 containers on `ros_net`)** — `robotis_ai_setup/docker/`
5. **Dataset recording (LeRobot v2.1)** — `physical_ai_tools/physical_ai_server/` + our overlays
6. **React 19 SPA (student + web dashboard)** — `physical_ai_tools/physical_ai_manager/`; one codebase, two builds via `Dockerfile` (student) and `Dockerfile.web` (Railway)
7. **Cloud training (Railway + Modal + Supabase)** — `robotis_ai_setup/cloud_training_api/`, `robotis_ai_setup/modal_training/`, `robotis_ai_setup/supabase/`
8. **Inference (load policy → drive arm)** — overlay `inference_manager.py` + upstream `inference/`
9. **Roboter Studio (Blockly + classical CV)** — `physical_ai_server/overlays/workflow/` + `cloud_training_api/app/routes/workflows.py` + Supabase `008_workflows.sql`
10. **Classroom Jetson Orin Nano** — shared remote inference target (Inferenz tab only); `robotis_ai_setup/jetson_agent/` + `cloud_training_api/app/routes/jetson.py` + migration `019_classroom_jetsons.sql`

```
Windows GUI ─wsl→ EduBotics distro (Docker)
              ├─ open_manipulator   (ROS 2 + Dynamixel + USB)
              ├─ physical_ai_server (ROS 2 + PyTorch + LeRobot + s6 + Roboter Studio)
              └─ physical_ai_manager (nginx + React, :80)
                        │
                        ▼  HTTPS
   Railway FastAPI (cloud_training_api, scintillating-empathy-production-1068)
   ├─ Modal SDK .spawn() ─→ Modal worker (L4 GPU) ─→ HuggingFace Hub
   └─ supabase-py (service-role) ─→ Supabase (Auth/Postgres/Realtime)
```

## Six non-negotiable rules

These exist because we paid for the lesson. Don't undo without explicit user agreement.

### 1. German UI / English code

- **German** for everything a student/teacher/admin reads: tkinter labels, React UI, `detail` fields on API errors, log strings users see, toast messages. Use literal `ä ö ü ß` (some legacy files use `Schueler`; new code uses `Schüler`).
- **English** for everything the maintainer reads: code, comments, docstrings, internal log lines, JSON keys, function names, commit messages.

### 2. Hardware safety lives in xacro + entrypoint, not software overlays

Software-side inference safety envelopes (NaN/Inf guard, joint clamp, per-tick velocity cap, stale-camera halt) were removed deliberately in the 2026-05 safety stripdown to fix recording↔inference asymmetry. Inference runs upstream `predict()` raw. The remaining protection is hardware-enforced or warning-only:

- **Xacro Dynamixel limits** (`omx_f.ros2_control.xacro`, `omx_l.ros2_control.xacro`): joint min/max position limits + gripper current limits (follower 350 mA, leader 300 mA, Op Mode 5)
- **ros2_control YAML**: 100 Hz, JointTrajectoryController 0.15 rad trajectory tolerance, 0.05 rad goal tolerance
- **SIGTERM/SIGINT torque-disable** in `docker/open_manipulator/entrypoint_omx.sh::disable_torque()`
- **Phase-4 post-sync verification** in `entrypoint_omx.sh` — 0.08 rad tolerance after the 3 s quintic ramp; hard-exit 2 on mismatch
- Recording-side guards are warning-only — episodes always complete (stale-camera 5 s, timestamp-gap > 2× expected_dt, video-file verifier, usb_cam Hz)

If you genuinely need to reintroduce a software safety guard that modifies the pipeline, **stop and ask the user**.

### 3. Overlays must fail loudly on missing target, no-op when already applied

`apply_overlay()` in `docker/physical_ai_server/Dockerfile` and `docker/open_manipulator/Dockerfile` does sha256 pre/post copy verification. If the upstream file is missing, build aborts. If the target is already byte-identical, it logs `Overlay already in place` and continues (idempotent). When adding an overlay you **must** add it to the `apply_overlay` chain with a unique path filter — without that, the source edit lives in the repo but never reaches the image.

`patches/fix_server_inference.py` self-verifies and exits 2/3 on no-op; CI's `overlay-guard` job tests this with a synthetic input.

LeRobot itself is **not** overlaid — it must be byte-identical to upstream SHA `989f3d05ba47f872d75c587e76838e9cc574857a` (LeRobot v0.2.0).

### 4. Service-role key bypasses RLS — authorization is your job

Every Supabase query in `cloud_training_api/app/` runs as **service-role** via `app/services/supabase_client.py::get_supabase()` (lazy singleton, fails fast at startup if `SUPABASE_URL` or `SUPABASE_SERVICE_ROLE_KEY` empty). RLS policies exist for defense-in-depth but are dormant under service-role.

Every endpoint that touches another user's data **must** call one of:
- `_assert_classroom_owned()` / `_assert_student_owned()` / `_assert_entry_owned()` (in `routes/teacher.py`)
- `_assert_workflow_owned()` (in `routes/workflows.py`)
- `_assert_workgroup_owned()` / `_assert_workgroup_in_classroom()` (in `routes/workgroups.py`)

**One missed assertion = silent IDOR.** RLS will not catch it.

The Modal worker uses the **anon key** + per-row `worker_token` (UUID). Its only DB write surface is the `update_training_progress(p_token, …)` RPC, guarded by migration `010_progress_terminal_guard.sql` so a worker can't overwrite a `canceled` row with `succeeded`.

### 5. Don't introduce drift between the LeRobot pinning sites

The SHA `989f3d05ba47f872d75c587e76838e9cc574857a` must agree across:
- `physical_ai_tools/lerobot/` (static byte-identical snapshot)
- `robotis_ai_setup/modal_training/modal_app.py` constant `LEROBOT_COMMIT`
- `robotis/physical-ai-server:amd64-0.8.2` base image's internal pin
- `meta/info.json` `codebase_version: "v2.1"`
- Modal preflight in `training_handler.py` enforcing `codebase_version == "v2.1"`

Bumping LeRobot is a **5-place change in one PR**. Modal also force-reinstalls torch+torchvision from `https://download.pytorch.org/whl/cu121` and uninstalls `torchcodec` — without that, pip picks `cu130` wheels that crash the cu121 base.

The LeRobot 5-site contract is now trust-on-PR-review (the `lerobot-sha-check` job was removed alongside `modal-deploy.yml` when Modal moved to manual deploys). Any bump must touch all 5 sites in one PR.

### 6. CI/CD deploys

Five workflows in `.github/workflows/` are the canonical path for four surfaces:

- `supabase-migrate.yml` — Supabase migrations (`supabase db push` against project `fnnbysrjkfugsqzwcksd`). **CI currently broken** — see "Supabase migration CI: known issue" below.
- `railway-deploy-cloud-api.yml` — FastAPI to `scintillating-empathy` service
- `railway-deploy-teacher-web.yml` — React SPA to `teacher-web` service
- `docker-publish.yml` — three production images to `nettername/*` Docker Hub
- `release.yml` — top-level dispatcher that fires all four in the golden order on tag pushes

**Supabase migration CI: known issue.** The `supabase db push` step in `apply-production` consistently fails on first runs (CLI auth, `--db-url` parsing, or `--linked --password` quirk — couldn't pin without admin GH-Actions log access). Until fixed:

- **Apply new migrations via either** (a) `supabase db push --linked --password "$DB_PASSWORD"` from your terminal after `supabase link --project-ref fnnbysrjkfugsqzwcksd`, OR (b) Claude's Supabase MCP `apply_migration` tool from chat. Both update `supabase_migrations.schema_migrations` correctly.
- Production schema state after CI failure is unchanged (the failure is at step 5 before any SQL is sent).
- Investigation next session: get a fine-grained GH PAT with `Actions: Read`, install `gh auth login --with-token`, read the actual error log on the failed step.

**Modal apps (`edubotics-training` + `edubotics-vision`) deploy MANUALLY.** The Modal image build happens in Modal's infrastructure (not on GH runners), and the CI auth + image-build feedback loop is slow to debug remotely. Until we have a strong reason to move Modal into CI, the operator owns it:

```bash
cd robotis_ai_setup/modal_training
modal deploy modal_app.py
modal deploy vision_app.py
modal run modal_app.py::smoke_test    # optional verification
modal run vision_app.py::smoke_test
```

Manual `railway up`, `psql -f migration.sql`, and `build-images.sh` from a developer terminal are EMERGENCY-only for the other four surfaces.

`build-images.sh` still exists but is now invoked **only by the CI runner**, never by a maintainer. The script's `--no-cache --pull` policy is preserved. CI refuses to build from a dirty tree, so `*-dirty` tags can never reappear in the registry.

## Critical architectural choices (don't undo silently)

- **No Docker Desktop.** Docker Engine runs inside a bundled WSL2 distro called `EduBotics`. GUI invokes Docker via `wsl -d EduBotics -- docker …` (wrapped by `_docker_cmd()` in `gui/app/docker_manager.py`). USB reaches the distro via `usbipd attach --wsl EduBotics --busid X` (usbipd 5.x positional form; the 4.x `--distribution` form is rejected). Docker pinned `5:27.5.1-1~ubuntu.22.04~jammy` + containerd `1.7.27-1` via `apt-mark hold` — Docker 29.x `containerd-snapshotter` corrupts multi-layer pulls on WSL2 custom rootfs.

- **ROS 2 `/leader/joint_trajectory` is the action rail.** The follower's `arm_controller` default action topic is remapped in `open_manipulator/open_manipulator_bringup/launch/omx_f_follower_ai.launch.py` (~line 144) from `/arm_controller/joint_trajectory` to `/leader/joint_trajectory`. Anything publishing there drives the follower: teleop, entrypoint quintic-sync, inference, Roboter Studio runtime.

- **Per-machine `ROS_DOMAIN_ID`.** `gui/app/config_generator.py::_resolve_ros_domain_id()` derives a UUID-hash mod 233 on first run (override via `EDUBOTICS_ROS_DOMAIN`). Without this, two students on the same school Wi-Fi share domain 30 and cross-talk.

- **React dual build.** One codebase, two builds: `Dockerfile` (student, `REACT_APP_MODE=student`, talks to local rosbridge `ws://hostname:9090`) and `Dockerfile.web` (Railway, `REACT_APP_MODE=web`, admin/teacher dashboard, listens on `${PORT}`, 5 strict security headers). `vercel.json` is intentionally a kill-switch (empty object) to block accidental shadow Vercel deploys.

- **Roboter Studio is bolted onto `physical_ai_server`, not a separate container.** The Dockerfile (a) rebuilds `physical_ai_interfaces` because the base image predates the new msgs/srvs, (b) installs `opencv-contrib-python==4.10.0.84`, `pupil-apriltags==1.0.4`, `onnxruntime==1.20.1` (CMake 4 compatibility via `ENV CMAKE_POLICY_VERSION_MINIMUM=3.5`), (c) downloads YOLOX-tiny ONNX from a pinned GitHub release URL and SHA-256 verifies `427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7`. Then copies in the `overlays/workflow/` module.

- **`.s6-keep` is load-bearing.** `physical_ai_server` bind-mounts `./physical_ai_server/.s6-keep` (empty 1-byte file) at `/etc/s6-overlay/s6-rc.d/user/contents.d/physical_ai_server:ro`. Remove the mount and the ROS node never starts inside the container even though `s6` reports healthy.

- **Single uvicorn worker on Railway.** The in-process rate limiter requires `uvicorn --workers 1`. If you raise this, switch the limiter to Redis or a Postgres advisory lock, and add the same for the dataset reconciliation sweep started in `app/main.py::_start_dataset_sweep`.

- **Boot-time schema fingerprint.** `cloud_training_api/app/main.py::_validate_required_schema()` probes every table + RPC the routes touch (workflow_versions, tutorial_progress, vision-quota RPCs, jetson RPCs). Railway aborts the deploy if the live Supabase schema is behind the on-disk migrations. Override with `EDUBOTICS_SKIP_SCHEMA_CHECK=1` for unit-test contexts only — never on Railway.

## Common commands

### Build images

**Default path: GitHub Actions.** Push to `main` with changes under `robotis_ai_setup/docker/**` or `physical_ai_tools/**`, and `.github/workflows/docker-publish.yml` builds + pushes both arches. Tag a release with `vX.Y.Z` and `release.yml` runs the full golden order.

For local development only (do not push from a workstation):
```bash
cd robotis_ai_setup/docker
SUPABASE_URL=... SUPABASE_ANON_KEY=... CLOUD_API_URL=... ./build-images.sh
PLATFORM=arm64 ./build-images.sh
```
Mandatory env vars are enforced via `${VAR:?…}`. Smoke test post-build greps `main.*.js` for the inlined env vars (CI duplicates this in `manager-build-validate` + `docker-publish.yml::smoke-test`). On Docker Desktop (macOS/Windows), the classic-daemon vs containerd-snapshotter dual store can silently push a stale `:latest`; pushing from CI on Linux runners avoids this.

### Run tests
```bash
# GUI / installer / overlay CLI tests (Python, mocked, cross-platform)
cd robotis_ai_setup && python -m unittest discover -s tests -v

# Single test
cd robotis_ai_setup && python -m unittest tests.test_training_handler_cli -v

# Cloud API tests (stubs fastapi/supabase via sys.modules — runs without deps)
cd robotis_ai_setup/cloud_training_api
SUPABASE_URL=http://ci.test SUPABASE_SERVICE_ROLE_KEY=ci_test \
MODAL_TOKEN_ID=ci_test MODAL_TOKEN_SECRET=ci_test \
python -m unittest discover -s app/tests -v

# React Workshop block sync (runs automatically via `prebuild`)
cd physical_ai_tools/physical_ai_manager && npm test -- --watchAll=false
```

### Lint / typecheck / validate
```bash
# Shell (mirrors CI's shell-lint job)
shellcheck -S error robotis_ai_setup/docker/build-images.sh \
                    robotis_ai_setup/docker/open_manipulator/entrypoint_omx.sh \
                    robotis_ai_setup/wsl_rootfs/build_rootfs.sh \
                    robotis_ai_setup/wsl_rootfs/start-dockerd.sh \
                    physical_ai_tools/physical_ai_manager/scripts/railway-deploy.sh

# Compose
docker compose -f robotis_ai_setup/docker/docker-compose.yml \
               -f robotis_ai_setup/docker/docker-compose.gpu.yml config

# Python compileall (mirrors CI)
python -m compileall -q robotis_ai_setup/gui robotis_ai_setup/scripts \
  robotis_ai_setup/cloud_training_api robotis_ai_setup/modal_training \
  robotis_ai_setup/docker/physical_ai_server/overlays \
  robotis_ai_setup/docker/physical_ai_server/patches
```

### Deploy

**Always via GitHub Actions** (per non-negotiable rule §6). The golden order is encoded as `needs:` edges in `.github/workflows/release.yml`:

```
W1 supabase-migrate ──► W2 railway-deploy-cloud-api ──► W3 railway-deploy-teacher-web ──► W4 docker-publish
```

For partial changes, push to `main` with changes scoped to one surface and that surface's path-filtered workflow fires by itself. For coordinated whole-stack releases, `git tag vX.Y.Z && git push --tags` runs the full chain via `release.yml`.

**One-time operator setup** (after merging this PR):

1. Mint these 12 GHA secrets (Settings → Secrets and variables → Actions): `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`, `RAILWAY_TOKEN`, `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`, `SUPABASE_ACCESS_TOKEN`, `SUPABASE_DB_URL`, `SUPABASE_PROJECT_REF`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_ANON_KEY`, `REACT_APP_CLOUD_API_URL`.
2. Tell the Supabase CLI the baseline is already applied to production:
   ```
   supabase link --project-ref fnnbysrjkfugsqzwcksd
   supabase migration repair --status applied 00000000000000
   ```
3. Enable leaked-password protection in the Supabase Auth dashboard (the security-fixes migration applies the other 10 advisor warnings).

**Bumping product VERSION** is still a four-place change: `VERSION` file, `installer/robotis_ai_setup.iss AppVersion`, `gui/app/constants.py` fallback constant, AND the Railway `GUI_VERSION` + `GUI_DOWNLOAD_URL` env vars on the `scintillating-empathy` Cloud API service AFTER the matching `gh release create v<version>` with the `EduBotics_Setup.exe` asset. Without the Railway+GitHub side, the in-tree bump is invisible to existing student installs — the `/version`-poll update gate never fires.

Cloud API URL: `https://scintillating-empathy-production-1068.up.railway.app` (the older `production-9efd` slug is dead; do not reintroduce).

**Manual emergency deploy** (off-pipeline, document in a PR before merging):
- Supabase: `supabase db push` (CLI auth) against the project, OR `psql $DB_URL -f rollback/NNN_*.sql` for rollbacks.
- Modal: `cd robotis_ai_setup/modal_training && modal deploy modal_app.py vision_app.py`.
- Railway: `railway up --service scintillating-empathy --environment production --path-as-root . --ci` from `cloud_training_api/`.
- Docker: `build-images.sh` from a clean Linux build host.

### Bootstrap (run once)
```bash
cd robotis_ai_setup
python scripts/bootstrap_admin.py --username admin --full-name "Sven"
```

## CI guardrails

Two layers — `ci.yml` (validators) and the five deploy workflows.

### `.github/workflows/ci.yml` — 10 validator jobs on every push/PR to `main`

- **python-tests** — `compileall` of all Python dirs, plus unittest discover in `tests/` and `cloud_training_api/app/tests/`
- **shell-lint** — shellcheck `-S error` on all shipped shell scripts
- **compose-validate** — `docker compose config` on both compose files with a fake `.env`
- **overlay-guard** — runs `fix_server_inference.py` against a synthetic upstream and asserts non-zero exit on no-op
- **modal-import-validate** — `modal_app.py` + `vision_app.py` (NOT `training_handler.py` — it imports container-only deps)
- **teacher-web-build-validate** — builds `Dockerfile.web` with `physical_ai_manager/` as the build context (matches `railway up --path-as-root .`)
- **manager-build-validate** — builds student `Dockerfile` with placeholder secrets; asserts each placeholder reached `main.*.js` (white-screen regression catcher)
- **tutorials-validate** — JSON-parses `physical_ai_manager/public/tutorials/*.json`, cross-checks `allowed_blocks` against runtime dispatch (`STATEMENT_HANDLERS` + `VALUE_EVALUATORS` keys in `overlays/workflow/handlers/__init__.py`, `HAT_BLOCK_TYPES` in `overlays/workflow/interpreter.py`, plus Blockly built-ins)
- **interfaces-validate** — verifies every `.srv` has exactly one `---`; cross-checks `CMakeLists.txt` against on-disk files
- **nginx-validate** — `envsubst $PORT` on `nginx.web.conf.template` then `nginx -t` on both configs

### Deploy workflows (path-scoped + tag-triggered)

- **`supabase-migrate.yml`** — applies `robotis_ai_setup/supabase/migrations/*.sql` to production via `supabase db push`. PRs that touch this path get an ephemeral Supabase Branch with a fingerprint probe; merge to `main` applies to production and probes the Railway `/health` endpoint as a post-apply gate.
- **`railway-deploy-cloud-api.yml`** — runs the import-time schema-probe (read-only) against production Supabase, then `railway up`, then polls `/health` to 200.
- **`railway-deploy-teacher-web.yml`** — same Dockerfile.web build CI validates, then `scripts/railway-deploy.sh` (`--service teacher-web`), then polls `/version.json` to 200.
- **`docker-publish.yml`** — refuses on dirty tree; checks upstream base digests via `bump-upstream-digests.sh`; builds amd64 + arm64 in a matrix via `build-images.sh` (`--no-cache --pull`); applies the canonical tag set (`<sha>`, `<sha>-short`, `:latest` on main, `:vX.Y.Z` + `:vX.Y` on tags); pulls + greps each published image to verify build-args reached the bundle.
- **`release.yml`** — top-level dispatcher; on tag push fires W1→W4 in the golden order via `needs:` edges.

**Modal is manual** — see Rule §6 above. Run `modal deploy modal_app.py vision_app.py` from your terminal BEFORE pushing a tag if the release touches Modal.

## When to ask the user

Act autonomously on: reading files, editing code with low blast radius, running local tests / lints, building Docker images locally, spawning sub-agents for research.

**Ask first** for:
- `git push` (and never force-push to `main`)
- `wsl --unregister EduBotics` (destroys VHDX → named volumes `ai_workspace`, `huggingface_cache`, `edubotics_calib` gone)
- `docker compose down -v` or `docker volume rm` of `huggingface_cache` / `edubotics_calib` (datasets / calibration gone)
- Force-push, `git reset --hard`, `git clean -fd`, `--no-verify`
- Modal `cancel` on a running training (charges credit)
- `supabase.auth.admin.delete_user` calls
- Rotating production secrets, editing CI/CD config
- Changing safety-critical paths (torque-disable on SIGTERM, sync-verification tolerance, ownership assertions, Dynamixel current limits)
- Renaming files other layers reference (overlay targets, ROS topics, env var names, RPC signatures)
- Touching `start_training_safe` / `get_remaining_credits` / `adjust_workgroup_credits` semantics — workgroup credit pool is load-bearing for grouped students (migration 011); regressions are silent over-spend or refused trainings
- Reintroducing software-side inference safety guards (see rule 2)

## When in doubt

The single source of truth is **the code**. This file describes invariants at the time it was written. Verify against `git log` and the current file when stakes are high. If this file disagrees with the code, fix this file in the same change — the whole point is that it stays in sync.

When you find an obstacle, **find the root cause** instead of bypassing it (never `--no-verify`, never `@pytest.skip`, never short-circuit an `apply_overlay` sha256 assertion that's telling you upstream renamed something).

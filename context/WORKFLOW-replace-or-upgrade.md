# WORKFLOW: Replace or Upgrade

> Strict checklist for replacing components or bumping versions. Follow step by step.
> Read [`WORKFLOW.md`](WORKFLOW.md) first for the master rules.

This is the highest-risk type of change. Bumping a base image or LeRobot can silently break the inference pipeline if you miss one of the 5 sources of truth.

---

## §1 — Identify what is being upgraded

Different components have different upgrade procedures. Before starting:

1. Which library/component? (LeRobot, base image, Python dep, Node dep, …)
2. From what version, to what version?
3. Read the upstream changelog — at minimum check for breaking changes.
4. Are there migrations / data format changes? (e.g., LeRobot `codebase_version` bump)

If the answer to any of these is unclear, **stop and ask the user**.

---

## §2 — LeRobot version bump

This is the most error-prone upgrade because LeRobot lives in **5 sources of truth that must all match**:

1. `physical_ai_tools/lerobot/` — static snapshot (must be byte-identical to the chosen commit)
2. `modal_training/modal_app.py` — `LEROBOT_COMMIT` constant + pip install URL
3. `physical_ai_tools/physical_ai_server/` — embedded LeRobot via `robotis/physical-ai-server` base image, which clones from `ROBOTIS-GIT/lerobot` jazzy branch (we don't control this directly)
4. Recording schema: `meta/info.json` `codebase_version` field
5. Modal preflight: `_preflight_dataset()` enforces `codebase_version == "v2.1"`

### Procedure

- [ ] **Check upstream changelog** for the target commit/version. Look for: `codebase_version` bump, breaking dataset schema, new policy type, breaking config format
- [ ] **Coordinate with ROBOTIS** if their base image must also be updated; or rebuild the base from source via `BUILD_BASE=1`
- [ ] **Update Modal image** (`modal_training/modal_app.py`):
  ```python
  LEROBOT_COMMIT = "<NEW_COMMIT>"
  # ...image config uses {LEROBOT_COMMIT} in pip URL
  ```
- [ ] **Update local snapshot** (`physical_ai_tools/lerobot/`):
  ```bash
  cd physical_ai_tools
  rm -rf lerobot
  git clone https://github.com/huggingface/lerobot.git
  cd lerobot && git checkout <NEW_COMMIT> && rm -rf .git
  ```
- [ ] **If `codebase_version` changed**:
  - Update the constant in Modal preflight (`training_handler._preflight_dataset`)
  - Add a migration script for old datasets: `scripts/migrate_v2.1_to_v2.2.py` (or similar)
  - Document the breaking change for students (may need to re-record)
  - Update [`02-pipeline.md`](02-pipeline.md) §5 (recording schema)
- [ ] **Rebuild Modal image**: `cd modal_training && modal deploy modal_app.py`
- [ ] **Rebuild Docker images**: `cd docker && REGISTRY=nettername ./build-images.sh`
- [ ] **End-to-end test**: record a small dataset → train on Modal → inference on real arm. Watch for:
  - Modal preflight pass
  - Training subprocess starts (no missing-arg / breaking-API errors)
  - Progress writes to Supabase
  - HF upload completes
  - Inference loads policy + drives arm
- [ ] **Update docs**:
  - [`01-architecture.md`](01-architecture.md) §5.4 (LeRobot alignment)
  - [`02-pipeline.md`](02-pipeline.md) §7 (Modal image)
  - [`11-modal-training.md`](11-modal-training.md) §2 (image build)
  - [`03-glossary.md`](03-glossary.md) "LeRobot" entry
- [ ] **Push images**: `docker push nettername/physical-ai-server:latest` (and `:amd64-X.Y.Z` semantic tag if you mint one)
- [ ] **Tag a release** in git: `git tag v2.3.0 && git push --tags`

---

## §3 — Base image (ROBOTIS) bump

Bumping `robotis/physical-ai-server:amd64-0.8.2` or `robotis/open-manipulator:amd64-4.1.4` to a newer immutable tag.

### Procedure

- [ ] **Verify new tag exists**: `docker pull robotis/physical-ai-server:amd64-X.Y.Z`
- [ ] **Inspect the layer differences**: `docker history robotis/physical-ai-server:amd64-X.Y.Z` — look for new directories, removed files, ROS distro change
- [ ] **Check overlay path filters still match**:
  ```bash
  docker run --rm robotis/physical-ai-server:amd64-X.Y.Z bash -c \
    "find /root/ros2_ws -name 'inference_manager.py' -path '*/inference/*'"
  # Should find one file. If empty: upstream renamed/moved → update path filter
  ```
  Repeat for `data_manager.py`, `data_converter.py`, `omx_f_config.yaml`, `physical_ai_server.py`.
- [ ] **Update Dockerfile FROM line**: `physical_ai_server/Dockerfile` line 1
- [ ] **Build**: `cd docker && REGISTRY=nettername ./build-images.sh`
  - Watch for `ERROR: ... not found in base image` — fail-loud is correct behavior. Investigate the rename, update the path filter.
  - Watch for `Overlaid: ... (sha256 → sha256)` — confirms the file was actually replaced.
- [ ] **End-to-end test**: as in LeRobot §2 above
- [ ] **Update docs**:
  - [`15-docker.md`](15-docker.md) §11 (pinned tags table)
  - [`docker/BASE_IMAGE_PINNING.md`](../robotis_ai_setup/docker/BASE_IMAGE_PINNING.md)
- [ ] **Push images**

For digest-pinning workflow, see `docker/bump-upstream-digests.sh` (helper that prints sed commands for digest replacement).

---

## §4 — Python dependency bump (cloud_training_api)

`cloud_training_api/requirements.txt`. Modal worker has its own deps in `modal_app.py`.

### Procedure

- [ ] Identify the dep + new version
- [ ] **If FastAPI / Pydantic / Starlette**: read changelog for breaking changes (model definition syntax, dependency injection)
- [ ] **If supabase-py**: check if API methods changed (`auth.admin.create_user` etc.)
- [ ] **If Modal**: check if `Function.from_name` / `spawn` / `cancel` API changed
- [ ] Update `requirements.txt` (pin to `==`, not `>=`)
- [ ] **Local test**: `pip install -r requirements.txt && uvicorn app.main:app --reload && curl http://localhost:8000/health`
- [ ] **Smoke test the routes you touched**: at minimum `/me`, `/trainings/quota`, `/trainings/start` (with mock data if no Modal)
- [ ] **Deploy**: `railway up --detach` from `cloud_training_api/`
- [ ] Tail Railway logs for errors on first requests

---

## §5 — Modal worker dependency bump

`modal_training/modal_app.py` `pip_install` calls.

### Procedure

- [ ] Identify the dep
- [ ] **If torch / torchvision**: keep `--force-reinstall` + `index_url=https://download.pytorch.org/whl/cu121`. CUDA 12.1 base requires cu121 wheels. **NEVER** drop `--force-reinstall` (lerobot pulls a torch and order matters).
- [ ] **If lerobot**: see §2 above
- [ ] **If huggingface_hub**: check `HfApi.upload_large_folder` / `repo_info` API
- [ ] **If supabase**: check RPC call signature
- [ ] Update the line in `modal_app.py`
- [ ] **`modal deploy modal_app.py`** (image rebuilds, but might use cache; verify with `modal app history`)
- [ ] **`modal serve` smoke test**: spawn a tiny training, watch logs

---

## §6 — Node / React dependency bump (`physical_ai_manager`)

### Procedure

- [ ] Identify the dep
- [ ] **If React**: changelog. React 19 specific: check Strict Mode, automatic batching, etc.
- [ ] **If Supabase JS client**: `signInWithPassword` / Realtime channel API
- [ ] **If roslib**: connection lifecycle changes
- [ ] **If Tailwind / Recharts**: visual diff
- [ ] `npm install <pkg>@<new-version>`
- [ ] `npm run build` (catches build-time breakage)
- [ ] `npm start` (dev) → click through:
  - Login flow
  - Recording start/stop
  - Training submit + Realtime updates
  - Inference start
  - Teacher / admin dashboards (if you bumped a shared dep)
- [ ] **Rebuild Docker images**: `cd docker && REGISTRY=nettername ./build-images.sh`

---

## §7 — Docker daemon / WSL2 distro upgrade

The bundled rootfs pins **Docker 27.5.1 + containerd 1.7.27**. Bumping requires care.

### Procedure

- [ ] Read Docker release notes between current and target version
- [ ] **CRITICAL: avoid Docker 29.x** — containerd-snapshotter regression on custom WSL2 rootfs (multi-layer pulls corrupt). 29 removed the disable flag.
- [ ] If sticking with 27.x or 28.x:
  - Update `wsl_rootfs/Dockerfile` ARG `DOCKER_VERSION` and `CONTAINERD_VERSION`
  - Verify deb URLs still resolve (apt.docker.com keeps debs ~12-24 months)
  - Rebuild: `cd wsl_rootfs && ./build_rootfs.sh`
  - Verify SHA256 sidecar updated
- [ ] **Test on a clean Win11 VM**:
  ```powershell
  wsl --import test-edubotics C:\temp\edubotics-test edubotics-rootfs.tar.gz --version 2
  wsl -d test-edubotics -- docker info
  wsl -d test-edubotics -- docker pull nettername/physical-ai-server:latest
  wsl --unregister test-edubotics
  ```
- [ ] **Bump installer version**: `installer/robotis_ai_setup.iss AppVersion`, `Testre/VERSION`, `gui/app/constants.APP_VERSION`
- [ ] **End-to-end installer test** on clean VM

---

## §8 — usbipd-win bump

Pinned to v5.3.0 with SHA256.

### Procedure

- [ ] Download new MSI from `https://github.com/dorssel/usbipd-win/releases`
- [ ] Compute SHA256:
  ```powershell
  Get-FileHash -Algorithm SHA256 .\usbipd-win_X.Y.Z_x64.msi | Format-List
  ```
- [ ] Update `installer/robotis_ai_setup.iss`:
  ```pascal
  #define UsbipdVersion "X.Y.Z"
  #define UsbipdSha256  "<NEW_HASH>"
  ```
- [ ] **Verify `configure_usbipd.ps1` still handles the version range** (4.x vs 5.x policy command syntax)
- [ ] Rebuild installer + test on clean VM

---

## §9 — Anti-patterns (don't do these)

- **Don't** bump LeRobot in only one place. **All 5 sources must align.**
- **Don't** use mutable tags (`:latest`, `main`) for ROBOTIS base images. Always pin to immutable tags.
- **Don't** drop `--force-reinstall` for torch in the Modal image. The default cu130 wheel will run on the cu121 base image and fail at runtime.
- **Don't** skip the end-to-end pipeline test. Type checks and unit tests don't catch silent dataset format breakage.
- **Don't** push the new image to `nettername/*:latest` until the test passes.
- **Don't** delete the old immutable tag. It's your rollback target.
- **Don't** forget to tag a git release for any user-visible bump.

---

## §10 — Rollback procedure

If a bump breaks production:

1. **Don't panic.** Students still have the previous version installed. The break is on new installs / `docker compose pull`.
2. **Retag the old immutable tag back to `:latest`**:
   ```bash
   docker tag nettername/physical-ai-server:amd64-0.8.2 nettername/physical-ai-server:latest
   docker push nettername/physical-ai-server:latest
   ```
3. **Investigate the failure** — was it the overlay path filter? LeRobot API change? Torch version?
4. **Document the regression** in [`21-known-issues.md`](21-known-issues.md) §0 corrections, OR remove the failed bump from git history if it never reached anyone.

For Modal: `modal deploy` overwrites the deployed image. Roll back by re-deploying the old `modal_app.py`.

For Railway: `railway rollback` from the dashboard, or `railway up --detach` from the previous git commit.

---

## §11 — Cross-references

- Master rules: [`WORKFLOW.md`](WORKFLOW.md)
- LeRobot alignment: [`01-architecture.md`](01-architecture.md) §5.4, [`11-modal-training.md`](11-modal-training.md) §2, [`02-pipeline.md`](02-pipeline.md) §5
- Base image pinning: [`docker/BASE_IMAGE_PINNING.md`](../robotis_ai_setup/docker/BASE_IMAGE_PINNING.md), [`15-docker.md`](15-docker.md) §11
- Overlay path filters: [`15-docker.md`](15-docker.md) §5, [`WORKFLOW-overlay-change.md`](WORKFLOW-overlay-change.md)
- Operations rollback recipes: [`20-operations.md`](20-operations.md) §3, §5

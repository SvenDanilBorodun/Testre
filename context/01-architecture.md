# 01 — Architecture (Big Picture)

> **What this file is:** the map. Who-talks-to-whom, where each component lives, what infrastructure backs it.
> Read [`02-pipeline.md`](02-pipeline.md) next for end-to-end data flow, or jump to a specific layer file (10–18) for source-level detail.

---

## 1. The 9 layers

```
┌────────────────────────────────────────────────────────────────┐
│  Student / Teacher / Admin                                     │
└────────────────────────────────────────────────────────────────┘
       │                                                  │
       │ Windows installer                                │ Browser
       ▼                                                  ▼
┌─────────────────┐                              ┌────────────────┐
│  EduBotics.exe  │                              │  React SPA     │
│  (tkinter GUI,  │                              │  (web mode,    │
│   PyInstaller)  │                              │   Railway)     │
└────────┬────────┘                              └────────┬───────┘
         │                                                │
         │ wsl -d EduBotics -- docker ...                 │ HTTPS
         ▼                                                │
┌──────────────────────────────────────┐                  │
│ EduBotics WSL2 distro (Ubuntu 22.04) │                  │
│ Docker 27.5.1, dockerd, nvidia       │                  │
│ ┌──────────────────────────────────┐ │                  │
│ │ open_manipulator container       │ │                  │
│ │ (ROS2 Jazzy, Dynamixel)          │ │                  │
│ ├──────────────────────────────────┤ │                  │
│ │ physical_ai_server container     │ │                  │
│ │ (ROS2 + PyTorch + LeRobot + s6)  │ │                  │
│ ├──────────────────────────────────┤ │                  │
│ │ physical_ai_manager container    │ │ ← localhost:80   │
│ │ (nginx, React student build)     │ │                  │
│ └──────────────────────────────────┘ │                  │
└──────────────────────────────────────┘                  │
                       │                                  │
                       │ POST /trainings/start            │
                       ▼                                  ▼
              ┌──────────────────────────────────────────────┐
              │  Railway FastAPI (cloud_training_api)        │
              │  scintillating-empathy-production-9efd       │
              └──────────┬─────────────────────┬─────────────┘
                         │                     │
              .spawn(...)│                     │ supabase-py (service-role)
                         ▼                     ▼
              ┌──────────────────┐    ┌──────────────────────┐
              │  Modal worker    │    │  Supabase Postgres   │
              │  edubotics-      │◄───┤  fnnbysrjkfugsqzwcksd│
              │  training fn=    │    │  Auth + Realtime     │
              │  train, NVIDIA L4│    │                      │
              └────┬─────────────┘    └──────────────────────┘
                   │
                   │ upload_large_folder()
                   ▼
              ┌──────────────────┐
              │  HuggingFace Hub │
              │  edubotics/*     │
              └──────────────────┘
```

The 9 layers (top-to-bottom in the user's mental model):

| # | Layer | Where | Owner |
|---|---|---|---|
| 1 | Windows installer + WSL2 rootfs | `robotis_ai_setup/installer/`, `robotis_ai_setup/wsl_rootfs/` | Our code |
| 2 | Windows tkinter GUI (`EduBotics.exe`) | `robotis_ai_setup/gui/` | Our code |
| 3 | Robot-arm connection (ROS2 + Dynamixel) | `open_manipulator/`, `robotis_ai_setup/docker/open_manipulator/` | ROBOTIS upstream + our overlay |
| 4 | Docker Compose (3 containers) | `robotis_ai_setup/docker/` | Our code |
| 5 | Dataset recording (LeRobot v2.1) | `physical_ai_tools/physical_ai_server/`, overlays in `robotis_ai_setup/docker/physical_ai_server/overlays/` | ROBOTIS upstream + our overlays |
| 6 | React SPA | `physical_ai_tools/physical_ai_manager/` | ROBOTIS upstream (hacked) |
| 7 | Cloud training (Railway + Modal + Supabase) | `robotis_ai_setup/cloud_training_api/`, `robotis_ai_setup/modal_training/`, `robotis_ai_setup/supabase/` | Our code |
| 8 | Inference (load policy → drive arm) | `physical_ai_tools/physical_ai_server/inference/`, overlay `inference_manager.py` | ROBOTIS upstream + our overlays |
| 9 | Modal MCP autonomous gateway | `modal_mcp/mcp_server_stateless.py` | Our code |

**Layer files in this folder (10–18) document each layer in source-level detail.**

---

## 2. The monorepo

Single git repo (`github.com/SvenDanilBorodun/Testre`, private). The upstream ROBOTIS repos were absorbed as regular directories — **no git submodules, `.gitmodules` does not exist.**

```
Testre/
├── open_manipulator/                <- ROBOTIS upstream (absorbed, ROS2 Jazzy + Dynamixel)
├── physical_ai_tools/               <- ROBOTIS upstream (absorbed)
│   ├── physical_ai_server/          <- ROS2 node: data recording + inference
│   ├── physical_ai_manager/         <- React SPA (nginx:80, rosbridge:9090)
│   ├── physical_ai_interfaces/      <- Custom ROS msg/srv definitions
│   ├── physical_ai_bt/              <- Behavior trees
│   └── lerobot/                     <- LeRobot v0.2.0 snapshot @989f3d05 (static, byte-identical)
├── robotis_ai_setup/                <- OUR custom code
│   ├── cloud_training_api/          <- FastAPI on Railway (training jobs + teacher/admin API)
│   ├── modal_training/              <- Modal app + handler for cloud GPU training
│   ├── docker/                      <- Compose, build-images.sh, overlays, patches, entrypoint
│   ├── supabase/                    <- migrations 001–007 + rollback/
│   ├── gui/                         <- Windows tkinter GUI (PyInstaller .exe)
│   ├── installer/                   <- Inno Setup .iss + 9 PowerShell scripts
│   ├── wsl_rootfs/                  <- Ubuntu 22.04 + Docker Engine rootfs builder
│   ├── scripts/                     <- bootstrap_admin.py
│   └── tests/                       <- 32 unit tests (mostly Windows-only, all mocked)
├── _upstream/                       <- Reference copies of ROBOTIS upstream (read-only, for diffs/blame)
└── context/                         <- THESE DOCS
```

Plus, outside `Testre/`:
- `cloud/modal_mcp/` — autonomous MCP gateway (separate Modal app, not in the monorepo)

---

## 3. Infrastructure inventory

| Service | Account / Project | What it hosts | Cost driver |
|---|---|---|---|
| Docker Hub | `nettername/*` | 3 student images: `physical-ai-manager`, `physical-ai-server`, `open-manipulator` | Free (public) |
| Docker Hub (base) | `robotis/*` | `robotis/open-manipulator:amd64-4.1.4`, `robotis/physical-ai-server:amd64-0.8.2` | Free (public, ROBOTIS-owned) |
| Railway | API service `scintillating-empathy-production-9efd` | FastAPI cloud training API | Hobby plan |
| Railway | Web service `edubotics-web.up.railway.app` (or similar) | React app in `web` mode (admin/teacher dashboard) | Hobby plan |
| Modal | Workspace `svendanilborodun`, app `edubotics-training`, fn `train` | NVIDIA L4 (24 GB), timeout 7h, `min_containers=0` | Per GPU-hour |
| Modal | Workspace `svendanilborodun`, app `example-mcp-server-stateless` | MCP server for Claude agents | Tiny (always-on idle) |
| Supabase | Project ref `fnnbysrjkfugsqzwcksd` | Auth + 7 tables + RPCs + Realtime | Free tier |
| HuggingFace | `edubotics/*` org | Datasets + trained model checkpoints | Free (public by default — see [§2.9 of known-issues](21-known-issues.md)) |
| GitHub | `SvenDanilBorodun/Testre` (private) | Source | Free |

---

## 4. Data &amp; control flow at a glance

### Auth flow
```
Student/Teacher/Admin
  → React login form (username + password)
  → synthetic email: {username}@edubotics.local
  → supabase.auth.signInWithPassword()
  → JWT in localStorage
  → every Railway API call: Authorization: Bearer <jwt>
```

### Recording flow (real arm, USB)
```
React UI → /task/command (ROS srv) → physical_ai_server (data_manager state machine)
  → Communicator subscribes to /gripper/image_raw, /scene/image_raw, /joint_states, /leader/joint_trajectory
  → 30 Hz tick: convert → frame buffer → ffmpeg encoder (async)
  → save_episode → parquet + mp4 + meta/info.json
  → optional: HF push via upload_large_folder (1h timeout)
```

### Cloud training flow
```
React → POST /trainings/start (Railway)
  → _sweep_user_running_jobs (sync stuck rows from Modal)
  → _find_recent_duplicate (60s window)
  → HfApi.dataset_info() preflight
  → RPC start_training_safe (atomic credit lock + insert)
  → Modal.Function.from_name(...).spawn(...)
  → cloud_job_id = FunctionCall.object_id stored in Supabase

Modal worker (training_handler.run_training)
  → preflight dataset (codebase_version v2.1, fps, joint count 4-20)
  → subprocess "python -m lerobot.scripts.train" (PYTHONUNBUFFERED=1)
  → reader thread regex-parses "step:N loss:X" → Supabase RPC update_training_progress
  → checkpoint upload to HF via upload_large_folder
  → terminal status (succeeded/failed/canceled)
```

### Inference flow (real arm)
```
React → /task/command START_INFERENCE → physical_ai_server
  → InferenceManager.load_policy (lazy on first tick, downloads from HF if missing)
  → 30 Hz tick:
    - get_latest_data (camera + follower joint state)
    - convert to RGB uint8 + float32 state
    - VALIDATE camera names (exact match, no remap)
    - VALIDATE image shape (matches training config)
    - VALIDATE no NaN/inf in action
    - CLAMP to joint limits + velocity caps
    - publish JointTrajectory to /arm_controller/follow_joint_trajectory
      (which is remapped to /leader/joint_trajectory in the launch file → drives the follower)
```

---

## 5. Critical architectural choices

These are the load-bearing design decisions. **Don't undo them without explicit user agreement.**

### 5.1 No Docker Desktop
Docker Engine runs inside a bundled WSL2 distro called `EduBotics`. The GUI invokes Docker via `wsl -d EduBotics -- docker ...` (wrapped by `_docker_cmd()` in `gui/app/docker_manager.py`). USB devices reach the distro via `usbipd attach --wsl --distribution EduBotics`. Reasons: (1) no Docker Desktop license prompt, (2) no tray icon sprawl, (3) we control the Docker version + config (pinned 27.5.1), (4) headless dockerd starts on distro boot via `start-dockerd.sh`.

### 5.2 Service-role key + Python ownership checks
The Railway FastAPI uses `SUPABASE_SERVICE_ROLE_KEY` everywhere. RLS is bypassed. Authorization is enforced in Python via `_assert_classroom_owned()`, `_assert_student_owned()`, `get_current_teacher()`, etc. This is a known fragility (one missed assertion = silent IDOR — see [§2.4 of known-issues](21-known-issues.md)) but the architecture is committed; switching to anon-key + authoritative RLS would be a significant rewrite.

### 5.3 Overlay-with-sha256-verify (M14)
ROBOTIS upstream files are `find`'d and replaced by overlays in `docker/physical_ai_server/Dockerfile`. Each overlay is sha256-verified after copy. If the upstream file isn't found, the build fails loudly. The 5 overlays + 1 patch:

| File | Replaces | Adds |
|---|---|---|
| `inference_manager.py` | upstream inference manager | Camera exact-match, NaN guard, joint clamp, velocity cap, stale-camera halt, image shape validation |
| `data_manager.py` | upstream data manager | RAM truncation detection, video file verification, episode validation, HF upload timeout |
| `data_converter.py` | upstream data converter | Empty trajectory guard, missing joint error, fps-aware action timing |
| `omx_f_config.yaml` | upstream config | Dual camera config, exact joint order |
| `physical_ai_server.py` | upstream main node | Handles None returns from new safety envelope |
| `patches/fix_server_inference.py` | (patches, not replaces) | Init `_endpoints` dict + remove duplicate `InferenceManager` construction |

LeRobot itself is **not** overlaid — it's byte-identical to upstream `989f3d05`.

### 5.4 Single LeRobot commit across all surfaces
`LEROBOT_COMMIT = "989f3d05ba47…"` is pinned in:
- `physical_ai_tools/lerobot/` (static snapshot)
- `modal_training/modal_app.py` (Modal image pip install)
- ROBOTIS-GIT `jazzy` branch (cloned by base `physical-ai-server` image)
- Recording's `meta/info.json` writes `codebase_version: "v2.1"`

Modal also force-reinstalls `torch torchvision` from `https://download.pytorch.org/whl/cu121` and uninstalls `torchcodec` (pyav fallback) — without this, defaults pull `cu130` wheels which crash on the CUDA 12.1 base image.

### 5.5 ROS2 `/leader/joint_trajectory` is the action rail
The follower's `arm_controller` default action topic is **remapped** in `omx_f_follower_ai.launch.py:144`:
```
/arm_controller/joint_trajectory → /leader/joint_trajectory
```
This means anyone publishing to `/leader/joint_trajectory` drives the follower:
- Leader's `joint_trajectory_command_broadcaster` (teleoperation: leader's observed positions follow-the-leader)
- Entrypoint's quintic-sync trajectory at startup
- Inference node's predicted actions (during inference)

`ROS_DOMAIN_ID=30` is hardcoded by default. Two students on the same school Wi-Fi share domain 30 — **same-LAN cross-talk is a known issue** ([§2.3 of known-issues](21-known-issues.md)). Mitigated by `_resolve_ros_domain_id()` in `gui/app/config_generator.py` which can derive a per-machine UUID hash.

### 5.6 React dual mode
One React 19 codebase, two builds:
- `REACT_APP_MODE=student` (default): ships in the `physical-ai-manager` Docker image, talks to local rosbridge (`ws://hostname:9090`) and the Railway API.
- `REACT_APP_MODE=web`: deploys to Railway via `Dockerfile.web`, no rosbridge, admin/teacher dashboard only.

`vercel.json` exists in the repo as a stale marker for `vercel dev`; **the real web deploy is Railway**.

### 5.7 German UI / English code
Target users are German students. All tkinter strings, React UI, error messages returned to the student/teacher are in German. Code, comments, internal logs are in English. Some files have legacy `Schueler` (transliterated) — new code uses `Schüler` directly.

---

## 6. Versioning

Five sources of truth currently drift:
- `gui/app/constants.APP_VERSION` (read from `Testre/VERSION`)
- `installer/robotis_ai_setup.iss AppVersion`
- `docker/versions.env IMAGE_TAG`
- React `package.json` (0.8.2)
- HTTP `/version.json` `buildId` (UTC timestamp + git SHA)

The `VERSION` file at repo root holds `2.2.2` and is the source-of-truth target. Bumping should hit all five — there's a known-issue around drift ([§2.10](21-known-issues.md)).

---

## 7. Where to look next

- **Pipeline detail:** [`02-pipeline.md`](02-pipeline.md)
- **Specific layer:** see [`00-INDEX.md`](00-INDEX.md) §1
- **Existing fragility / bugs:** [`21-known-issues.md`](21-known-issues.md)
- **Operations / runbooks:** [`20-operations.md`](20-operations.md)

---

**Last verified:** 2026-05-04 (against commits up to current HEAD).

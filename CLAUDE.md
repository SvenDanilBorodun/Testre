# ROBOTIS AI Educational Platform

## Permissions

All tools are pre-approved. Act autonomously without asking for confirmation.

## Project

Educational Physical AI platform where students record robot datasets, train ML models on cloud GPUs, and run inference on ROBOTIS OpenMANIPULATOR arms. Students use Windows 11 PCs with no GPUs — training runs on RunPod Serverless. Product name: **EduBotics**.

## Monorepo Layout

Single git repo (github.com/SvenDanilBorodun/Testre, private). The upstream ROBOTIS repos were absorbed as regular directories — there are **no git submodules**. `.gitmodules` does not exist.

```
Testre/
├── open_manipulator/                <- ROBOTIS upstream (absorbed, ROS2 Jazzy + Dynamixel)
├── physical_ai_tools/               <- ROBOTIS upstream (absorbed)
│   ├── physical_ai_server/          <- ROS2 node: data recording + inference
│   ├── physical_ai_manager/         <- React SPA (nginx:80, rosbridge:9090)
│   ├── physical_ai_interfaces/      <- Custom ROS msg/srv definitions
│   ├── physical_ai_bt/              <- Behavior trees
│   └── lerobot/                     <- Embedded LeRobot v0.2.0 snapshot @989f3d05 (static, byte-identical to upstream)
└── robotis_ai_setup/                <- OUR custom code
    ├── cloud_training_api/          <- FastAPI on Railway (training jobs + teacher/admin API)
    ├── runpod_training/             <- RunPod serverless handler + Dockerfile
    ├── docker/                      <- Compose, build-images.sh, overlays, patches, entrypoint
    ├── supabase/                    <- migration.sql + 002_accounts.sql
    ├── gui/                         <- Windows tkinter GUI (PyInstaller .exe)
    ├── installer/                   <- Inno Setup + PowerShell scripts
    ├── scripts/                     <- bootstrap_admin.py
    ├── tests/                       <- Unit tests
    ├── CHANGES_SESSION_2026-04-06.md   <- Historical session log (reference only)
    ├── FRONTEND_UX_FOLLOWUPS.md        <- Live punch-list of upstream React issues
    └── ROLLOUT_ACCOUNTS.md             <- Deployment runbook for the account system
```

## Architecture

### Student Machine (3 Docker containers on Windows 11 + Docker Desktop WSL2)
```
Browser (http://localhost) ← physical_ai_manager (nginx:80, React SPA, REACT_APP_MODE=student)
                           ← rosbridge WebSocket (:9090, published by server)
physical_ai_server         <- ROS2 + PyTorch + LeRobot + s6-overlay
                              Records datasets, runs inference, publishes video (:8080)
open_manipulator           <- ROS2 Jazzy, Dynamixel hardware interface
                              Follower arm (IDs 11-16) + leader arm (IDs 1-6) + 2 cameras
```
Containers share a Docker bridge network (`ros_net`). `ROS_DOMAIN_ID=30`, privileged for USB.
Ports published to host: `80` (React), `9090` (rosbridge), `8080` (web_video_server).

### Cloud Training
```
React frontend → POST /trainings/start → Railway FastAPI → RunPod Serverless → LeRobot training
                                                         → Progress → Supabase → Frontend polls every 5s
                                                         → Model uploaded to HuggingFace
```

### Teacher / Admin Web Dashboard
Same `physical_ai_manager` React app built with `REACT_APP_MODE=web`, hosted on Vercel. Teachers and admins log in with username+password (Supabase Auth via synthetic `@edubotics.local` emails). Admins manage teachers; teachers manage classrooms (max 30 students each) and allocate credits from their pool.

## Key Infrastructure

| Service | Location | Details |
|---------|----------|---------|
| Docker Hub | `nettername/*` | 4 images: physical-ai-manager, physical-ai-server, open-manipulator, robotis-ai-training |
| Base images | `robotis/*` | `robotis/open-manipulator:amd64-4.1.4`, `robotis/physical-ai-server:amd64-0.8.2` |
| Railway API | `scintillating-empathy-production-9efd.up.railway.app` | FastAPI cloud training API |
| RunPod | Endpoint `wu45u3xmbuwbqr` | Serverless GPU training, workers min=0 max=1 |
| Supabase | Project ref `fnnbysrjkfugsqzwcksd` | Auth + trainings + classrooms + credits |
| HuggingFace | Models pushed to `edubotics/*` | Datasets + trained model checkpoints |

## Docker Image Build Chain

**CRITICAL**: The base image clones from **upstream ROBOTIS-GIT**, NOT from this repo. All physical_ai_server fixes are applied as **overlays** in a thin layer. LeRobot itself is NOT overlaid — it's identical to upstream at commit `989f3d05`.

```
robotis/physical-ai-server:amd64-0.8.2           (ROBOTIS official — ROS2 + PyTorch + LeRobot + s6)
  └─ nettername/physical-ai-server                (+ CRLF fix + patch + 4 overlays)

robotis/open-manipulator:amd64-4.1.4             (ROBOTIS official — ROS2 + Dynamixel)
  └─ nettername/open-manipulator                  (+ entrypoint_omx.sh + identify_arm.py)

<physical_ai_tools/physical_ai_manager>          (build context, pulls from this repo)
  └─ nettername/physical-ai-manager               (React + nginx; REACT_APP_MODE baked at build)

nvidia/cuda:12.1.1-devel-ubuntu22.04             (CUDA base for RunPod)
  └─ nettername/robotis-ai-training               (+ LeRobot@989f3d05 + torch cu121 + handler.py)
```

Build order: `cd robotis_ai_setup/docker && REGISTRY=nettername ./build-images.sh`
Expects `open_manipulator/` and `physical_ai_tools/` alongside `robotis_ai_setup/` (defaults to the monorepo layout).

## Overlay System (robotis_ai_setup/docker/physical_ai_server/)

The base image clones upstream code, so we patch it with overlays + one patch script:

| File | Purpose |
|------|---------|
| `patches/fix_server_inference.py` | Fixes upstream bug: uninitialized `_endpoints` dict + duplicate InferenceManager init |
| `overlays/inference_manager.py` | Camera exact-match enforcement (no silent alphabetical remap) |
| `overlays/data_manager.py` | dtype=float32 on state/action arrays |
| `overlays/data_converter.py` | Empty trajectory guard + fail-loud on missing joints |
| `overlays/omx_f_config.yaml` | Dual camera config (gripper + scene) |

The Dockerfile also strips `\r` from s6 service files (Windows CRLF would make s6-overlay reject `longrun\r` as invalid type).

## LeRobot Version Alignment

All components use LeRobot v0.2.0 at commit `989f3d05ba47` from `huggingface/lerobot`:
- **Robot** (physical-ai-server base): cloned via ROBOTIS-GIT `jazzy` branch which pins this commit
- **RunPod** (robotis-ai-training): pip installs `lerobot[pi0] @ git+huggingface/lerobot@989f3d05`, then force-reinstalls `torch torchvision` with `cu121` index (the default `torch+cu130` is incompatible with the CUDA 12.1 base), and uninstalls `torchcodec` (pyav fallback used)
- **Local copy** (`physical_ai_tools/lerobot/`): static snapshot, NOT a modified fork

## Docker Compose (robotis_ai_setup/docker/docker-compose.yml)

Uses a bridge network (`ros_net`) + explicit port forwards, NOT `network_mode: host`.

Key env vars (from `.env` generated by the GUI):
```
FOLLOWER_PORT, LEADER_PORT,
CAMERA_DEVICE_1, CAMERA_NAME_1 (default: gripper),
CAMERA_DEVICE_2, CAMERA_NAME_2 (default: scene),
ROS_DOMAIN_ID=30
```

`physical_ai_server` mounts `./physical_ai_server/.s6-keep` (empty marker file) into `/etc/s6-overlay/s6-rc.d/user/contents.d/physical_ai_server:ro` to enable the s6 service. GPU override lives in `docker-compose.gpu.yml`.

## Complete Pipeline (Recording → Training → Inference)

### Recording
1. Two cameras (`/gripper/image_raw/compressed`, `/scene/image_raw/compressed`) → cv_bridge BGR → RGB → uint8 HWC → H.264 CRF 28
2. Follower joints (`/joint_states`) → reordered by config `joint_order` → `np.array(dtype=float32)` → parquet
3. Leader joints (`/leader/joint_trajectory`) → `points[0].positions` → reordered → action array (float32) → parquet
4. Episode metadata → `info.json` (codebase_version v2.1, fps, features)
5. Optional HuggingFace upload via `upload_large_folder()`

### Training (Cloud)
1. Frontend POSTs to Railway API with `dataset_name, model_type, steps`
2. API deduplicates within 60s, validates credits, creates Supabase row, dispatches to RunPod
3. Student builds only expose `act` policy (`ALLOWED_POLICIES=act` at React build time); other policies (SmolVLA etc.) are hidden in the dropdown
4. RunPod handler runs `python -m lerobot.scripts.train`
5. Progress parsed from stdout/stderr (`step:1K loss:0.123`) → Supabase (3x retry, unbuffered subprocess)
6. Model uploaded to HuggingFace → `camera_config.json` written alongside checkpoint
7. Status updated to succeeded/failed

### Inference
1. Camera images + follower state collected (waits for ALL topics)
2. Policy loaded via `PreTrainedPolicy.from_pretrained()`
3. Camera names must **exactly match** model's `input_features` (no remapping)
4. Images: uint8 → float32/255 → CHW permute → batch
5. State: float32 tensor → batch
6. `policy.select_action(observation)` → action tensor → JointTrajectory → robot

## Robot Configuration

### OMX-F Follower (Servo IDs 11-16, 1Mbps, Protocol 2.0)
- Joints: joint1-5 (arm) + gripper_joint_1
- Controllers: `arm_controller` (JointTrajectoryController) + `gripper_controller` (GripperActionController)

### OMX-L Leader (Servo IDs 1-6, 1Mbps, Protocol 2.0)
- Joints 1-5: passive (read-only), gripper_joint_1: active (current control)
- Namespace: `/leader/`
- Publishes: `/leader/joint_trajectory`

### Topics
- Cameras: `/gripper/image_raw/compressed`, `/scene/image_raw/compressed`
- Follower: `/joint_states` (JointState)
- Leader: `/leader/joint_trajectory` (JointTrajectory)
- Inference output: `/arm_controller/follow_joint_trajectory` (Action)

## Supabase Schema

`migration.sql` creates the base:
```sql
users(id UUID PK, email TEXT, training_credits INTEGER DEFAULT 0, created_at TIMESTAMPTZ)
trainings(id SERIAL PK, user_id UUID FK, status, dataset_name, model_name, model_type,
          training_params JSONB, runpod_job_id, current_step, total_steps, current_loss,
          requested_at, terminated_at, error_message)
```
RLS enabled. `get_remaining_credits()` is self-healing (derived, no counters). `start_training_safe()` enforces dedupe + credit check atomically.

`002_accounts.sql` layers in the 3-tier account model:
```sql
CREATE TYPE user_role AS ENUM ('admin','teacher','student');
users += role, username (UNIQUE), full_name, classroom_id, created_by
classrooms(id, teacher_id, name, created_at)  -- UNIQUE(teacher_id, name)
-- Trigger: max 30 students per classroom
-- Function: adjust_student_credits(student_id, delta) moves credits from teacher pool to student
```

Bootstrap admin once: `python scripts/bootstrap_admin.py --username admin --full-name "Sven"`.

## Frontend Build Modes

The React app has two build modes baked at build time (`REACT_APP_MODE`):
- `student` (default for the Docker image) — rosbridge UI for recording/training/inference
- `web` (Vercel deployment) — admin + teacher dashboard, no rosbridge

A `vercel.json` lives in `physical_ai_manager/`. The Cloud API's `ALLOWED_ORIGINS` must include the Vercel URL.

## Commands

```bash
# Build all Docker images (Linux/WSL2 with Docker)
cd robotis_ai_setup/docker && REGISTRY=nettername \
  SUPABASE_URL=... SUPABASE_ANON_KEY=... CLOUD_API_URL=... \
  ./build-images.sh

# Run unit tests
cd robotis_ai_setup && python -m unittest discover -s tests -v

# Validate Docker Compose
cd robotis_ai_setup/docker && docker compose config

# Build Windows GUI
cd robotis_ai_setup/gui && pyinstaller build.spec

# Deploy cloud API (Railway CLI, must be linked first)
cd robotis_ai_setup/cloud_training_api && railway up --detach

# Bootstrap first admin user
cd robotis_ai_setup && python scripts/bootstrap_admin.py --username admin --full-name "Sven"
```

## Environment

- Windows 11 Pro build 26200
- Python 3.14 (system), Docker Desktop, WSL2 Ubuntu-24.04
- Railway CLI linked, logged in as lastthedayey@gmail.com
- Docker Hub logged in as nettername
- Secrets in `robotis_ai_setup/cloud_training_api/.env` and `robotis_ai_setup/docker/.env` (both gitignored)

## Language

GUI and user-facing error messages are in **German** (target audience: German students). Backend code, API error bodies, and comments are in English.

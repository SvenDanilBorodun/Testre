# ROBOTIS AI Educational Platform

## Permissions

All tools are pre-approved. Act autonomously without asking for confirmation.

## Project

Educational Physical AI platform where students record robot datasets, train ML models on cloud GPUs, and run inference on ROBOTIS OpenMANIPULATOR arms. Students use Windows 11 PCs with no GPUs — training runs on RunPod Serverless.

## Monorepo Layout

```
Testre/                              <- single git repo (github.com/SvenDanilBorodun/Testre, private)
├── open_manipulator/                <- ROBOTIS robot control (ROS2 Jazzy, Dynamixel hardware)
├── physical_ai_tools/               <- Recording, inference, React frontend, embedded LeRobot fork
│   ├── physical_ai_server/          <- ROS2 node: data recording + inference
│   ├── physical_ai_manager/         <- React SPA (nginx:80, connects via rosbridge:9090)
│   ├── physical_ai_interfaces/      <- Custom ROS msg/srv definitions
│   ├── physical_ai_bt/              <- Behavior trees
│   └── lerobot/                     <- Embedded LeRobot v0.2.0 fork (NOT a submodule, custom ROBOTIS patches)
└── robotis_ai_setup/                <- Infrastructure: cloud API, RunPod, Docker, installer, Supabase
    ├── cloud_training_api/          <- FastAPI on Railway (training job management)
    ├── runpod_training/             <- RunPod serverless handler + Dockerfile
    ├── docker/                      <- Docker Compose, build-images.sh, overlays, patches
    ├── supabase/                    <- Database schema (migration.sql)
    ├── gui/                         <- Windows tkinter GUI (PyInstaller .exe)
    ├── installer/                   <- Inno Setup + PowerShell scripts
    └── tests/                       <- Unit tests
```

## Architecture

### Student Machine (3 Docker containers on Windows 11 + Docker Desktop WSL2)
```
Browser (http://localhost) ← physical_ai_manager (nginx:80, React SPA)
                           ← rosbridge WebSocket (:9090)
physical_ai_server         <- ROS2 + PyTorch + LeRobot + s6-overlay
                              Records datasets, runs inference, publishes video (:8080)
open_manipulator           <- ROS2 Jazzy, Dynamixel hardware interface
                              Controls follower arm (IDs 11-16) + leader arm (IDs 1-6)
```
All containers: `network_mode: host`, `ROS_DOMAIN_ID=30`, privileged for USB access.

### Cloud Training
```
React frontend → POST /trainings/start → Railway FastAPI → RunPod Serverless → LeRobot training
                                                         → Progress → Supabase → Frontend polls every 5s
                                                         → Model uploaded to HuggingFace
```

## Key Infrastructure

| Service | Location | Details |
|---------|----------|---------|
| Docker Hub | `nettername/*` | 4 images: physical-ai-manager, physical-ai-server, open-manipulator, robotis-ai-training. Base images from `robotis/*` (official ROBOTIS) |
| Railway API | `scintillating-empathy-production-9efd.up.railway.app` | FastAPI cloud training API, auto-deploys from git push |
| RunPod | Endpoint `wu45u3xmbuwbqr` | Serverless GPU training, workers min=0 max=1 |
| Supabase | Project ref `fnnbysrjkfugsqzwcksd` | Auth + trainings table + credits system |
| HuggingFace | Models pushed to `edubotics/*` | Datasets + trained model checkpoints |

## Docker Image Build Chain

**CRITICAL**: The base image (`physical-ai-server-base`) clones from **upstream ROBOTIS-GIT**, NOT from this repo. All physical_ai_server source fixes must be applied as **overlays** in the thin layer Dockerfile. LeRobot itself is NOT overlaid — it's identical to upstream.

```
robotis/physical-ai-server:latest                  (ROBOTIS official from Docker Hub — ROS2 + PyTorch + LeRobot + s6)
  └─ nettername/physical-ai-server                   (+ overlays: inference_manager, data_manager, data_converter, omx_f_config + patches)

robotis/open-manipulator:amd64-4.1.4                (ROBOTIS official from Docker Hub — ROS2 + Dynamixel)
  └─ nettername/open-manipulator                     (+ entrypoint_omx.sh + identify_arm.py)

nvidia/cuda:12.1.1-devel-ubuntu22.04                (CUDA base for RunPod)
  └─ nettername/robotis-ai-training                  (+ LeRobot@989f3d05 via pip + handler.py)
```

Build order: `cd robotis_ai_setup/docker && REGISTRY=nettername ./build-images.sh`
The build script automatically copies the LeRobot fork into both build contexts and cleans up after.

## Overlay System (robotis_ai_setup/docker/physical_ai_server/)

Since the base image clones upstream code, we patch it with overlays:

| Overlay | Purpose |
|---------|---------|
| `overlays/inference_manager.py` | Camera exact-match enforcement (no silent alphabetical remap) |
| `overlays/data_manager.py` | dtype=float32 on state/action arrays |
| `overlays/data_converter.py` | Empty trajectory guard + fail-loud on missing joints |
| `overlays/omx_f_config.yaml` | Dual camera config (gripper + scene) |
| `patches/fix_server_inference.py` | Fixes upstream bug: uninitialized `_endpoints` dict |

## LeRobot Version Alignment

All components use the same LeRobot v0.2.0 at commit `989f3d05ba47` from `huggingface/lerobot`:
- **Robot** (physical-ai-server-base): Cloned via ROBOTIS-GIT/physical_ai_tools `jazzy` branch (includes lerobot as submodule at `989f3d05`)
- **RunPod** (robotis-ai-training): Pip installed directly from `huggingface/lerobot@989f3d05`
- **Local copy** (`physical_ai_tools/lerobot/`): Byte-for-byte identical to upstream (197/197 Python files match)
- The local copy is a static snapshot, NOT a modified fork — no ROBOTIS-specific patches to LeRobot itself

## Complete Pipeline (Recording → Training → Inference)

### Recording
1. Camera topics (`CompressedImage`) → cv_bridge BGR → cv2.cvtColor RGB → uint8 HWC → video H.264 (CRF 28)
2. Follower joints (`JointState`) → reordered by config `joint_order` → `np.array(dtype=float32)` → parquet
3. Leader joints (`JointTrajectory`) → `points[0].positions` → reordered → action array (float32) → parquet
4. Episode metadata → `info.json` (codebase_version v2.1, fps, features)
5. Optional HuggingFace upload via `upload_large_folder()`

### Training (Cloud)
1. Frontend POSTs to Railway API with dataset_name, model_type, steps
2. API validates credits, creates Supabase row, dispatches to RunPod
3. RunPod handler runs `python -m lerobot.scripts.train` with CUDA
4. Progress parsed from stdout (`step:1K loss:0.123`) → Supabase (3x retry)
5. Model uploaded to HuggingFace → `camera_config.json` written alongside checkpoint
6. Status updated to succeeded/failed

### Inference
1. Camera images + follower state collected (waits for ALL topics)
2. Policy loaded via `PreTrainedPolicy.from_pretrained()`
3. Camera names must **exactly match** model's `input_features` (no remapping)
4. Images: uint8 → float32/255 → CHW permute → batch
5. State: float32 tensor → batch
6. `policy.select_action(observation)` → action tensor → JointTrajectory message → robot

## Robot Configuration

### OMX-F Follower (Servo IDs 11-16, /dev/ttyACM1, 1Mbps)
- Joints: joint1-5 (arm) + gripper_joint_1
- Controller: `arm_controller` (JointTrajectoryController) + `gripper_controller` (GripperActionController)
- Update rate: 100 Hz

### OMX-L Leader (Servo IDs 1-6, /dev/ttyACM2, 1Mbps)  
- Joints 1-5: passive (read-only), gripper_joint_1: active (current control)
- Namespace: `/leader/`
- Publishes: `/leader/joint_trajectory` (JointTrajectory)

### Topics Used by Recording
- Cameras: `/gripper/image_raw/compressed`, `/scene/image_raw/compressed`
- Follower: `/joint_states` (JointState)
- Leader: `/leader/joint_trajectory` (JointTrajectory)

## Supabase Schema

```sql
users(id UUID PK, email TEXT, training_credits INTEGER DEFAULT 0, created_at TIMESTAMPTZ)
trainings(id SERIAL PK, user_id UUID FK, status TEXT, dataset_name TEXT, model_name TEXT,
          model_type TEXT, training_params JSONB, runpod_job_id TEXT,
          current_step INTEGER, total_steps INTEGER, current_loss REAL,
          requested_at TIMESTAMPTZ, terminated_at TIMESTAMPTZ, error_message TEXT)
```
RLS enabled. Credits derived via `get_remaining_credits()` function (self-healing, no counters).

## Commands

```bash
# Build all Docker images (Linux/WSL2 with Docker)
cd robotis_ai_setup/docker && REGISTRY=nettername ./build-images.sh

# Run unit tests
cd robotis_ai_setup && python -m unittest discover -s tests -v

# Validate Docker Compose
cd robotis_ai_setup/docker && docker compose config

# Build Windows GUI
cd robotis_ai_setup/gui && pyinstaller build.spec

# Deploy cloud API (Railway CLI, must be linked first)
cd robotis_ai_setup/cloud_training_api && railway up --detach

# Check RunPod endpoint
python3 -c "import runpod; runpod.api_key='KEY'; print(runpod.Endpoint('wu45u3xmbuwbqr'))"
```

## Environment

- Windows 11 Pro build 26200
- Python 3.14 (system), Docker Desktop 29.2.1, WSL2 Ubuntu-24.04
- Railway CLI installed, logged in as lastthedayey@gmail.com
- Docker Hub logged in as nettername
- RunPod API key in `robotis_ai_setup/cloud_training_api/.env` (NOT committed, in .gitignore)

## Language

GUI and user-facing error messages are in **German** (target audience: German students). Backend code and comments in English. German strings will be localized later.

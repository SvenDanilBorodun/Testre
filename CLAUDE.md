# EduBotics — Single-File Brief for Claude

> **Read this entire file at the start of every session.** It replaces the former `context/` folder and is the single source of truth for what the project is, how it fits together, and how to make changes safely. Source code is the ultimate authority — when this file disagrees with the code, the code wins (and you should update this file in the same change).
>
> Last verified by reading every load-bearing file directly: 2026-05-08.

---

## 0. What is EduBotics?

A vertically-integrated educational stack for teaching Physical AI to **German-speaking students** on **ROBOTIS OpenMANIPULATOR-X** arms. The student lifecycle:

```
Install (.exe) → Setup (Windows GUI wizard) → Record demos (ROS2)
       → Train policy (Modal cloud GPU) → Inference (back on the arm)
       → Roboter Studio (Block-based authoring + classical CV) [optional]
```

Students run Windows 11 PCs with no GPUs. Training runs on Modal NVIDIA L4. The product ships as a single `.exe` that installs a bundled WSL2 Ubuntu 22.04 distro called `EduBotics` containing Docker Engine and three containers — **no Docker Desktop dependency, no separate WSL distro install, no licensing prompts.** Web dashboard for teachers/admins is a separate Railway deploy.

Repo: `github.com/SvenDanilBorodun/Testre.git` (private). Single git repo, no submodules — ROBOTIS upstream `open_manipulator/` and `physical_ai_tools/` were absorbed as plain directories. Product name: **EduBotics**.

---

## 1. The 5 non-negotiable rules

### 1.1 Language boundary is sacred
- **German** for everything a student / teacher / admin reads: tkinter labels, React UI, error strings returned from API in `detail` fields, log strings the user reads, toast messages.
- **English** for everything the maintainer reads: code, comments, docstrings, internal log lines, JSON keys, function names, commit messages.
- Use literal `ä ö ü ß` in source. Some legacy files use `Schueler` (transliterated); new code uses `Schüler` directly.
- When you write a new error, ask: "will a student/teacher read this?" → German. Otherwise English.

### 1.2 The arm is real hardware. Safety is non-negotiable.
- Never disable any of the **5 inference-time safety envelopes** in `robotis_ai_setup/docker/physical_ai_server/overlays/inference_manager.py`:
  1. **NaN/Inf guard** (drops tick) — German: `[STOPP] Modell hat NaN/Inf-Werte ausgegeben. Tick verworfen.`
  2. **Joint clamp** (`np.clip` to `omx_f_config.yaml.safety_envelope.joint_min/joint_max`)
  3. **Per-tick velocity cap** (`max_delta_per_tick_at_30hz` = `[0.3, 0.3, 0.3, 0.3, 0.3, 0.3]`)
  4. **Stale-camera halt** (4 sparse 256-byte hashes, warn @ 2s, **halt @ 5s**)
  5. **Image shape & camera-name exact-match validation** (rejects silent alphabetical remap)
- Never remove the SIGTERM/SIGINT torque-disable in `docker/open_manipulator/entrypoint_omx.sh` (`disable_torque()` calls `/dynamixel_hardware_interface/set_dxl_torque` SetBool service on both arms).
- Never bypass `_assert_classroom_owned()` / `_assert_student_owned()` / `_assert_entry_owned()` / `_assert_workflow_owned()` ownership checks in the cloud API.
- Never weaken the post-sync verification in `entrypoint_omx.sh` Phase 4 (0.08 rad tolerance per joint after the 3-second quintic ramp; hard-exit 2 on mismatch refuses to continue).
- If you genuinely need to relax a safety check, **stop and ask the user**.

### 1.3 Overlays must fail loudly on no-op.
- The `apply_overlay()` shell function in both `docker/physical_ai_server/Dockerfile` and `docker/open_manipulator/Dockerfile` does sha256 pre-/post-copy verification: if the upstream file is missing or already byte-identical to the overlay, build aborts with `ERROR: $name not found in base image — overlay cannot be applied`.
- The `patches/fix_server_inference.py` patch self-verifies and exits **2 or 3** on no-op (CI's `overlay-guard` job tests this with a fake input).
- If you add an overlay you **must** add it to the `apply_overlay` chain with a unique path filter, AND add an upstream sha256 assertion. See [§13 Workflow: overlay change](#13-workflows-for-claude).

### 1.4 Service-role key bypasses RLS. Authorization is your job.
- Every Supabase query in `cloud_training_api/app/` runs as **service-role** via `app/services/supabase_client.py:get_supabase()` (lazy singleton, fails fast at startup if `SUPABASE_URL` or `SUPABASE_SERVICE_ROLE_KEY` empty).
- RLS policies exist (defense-in-depth) but are dormant under service-role.
- Every endpoint that touches another user's data must call `_assert_classroom_owned()` / `_assert_student_owned()` / `_assert_entry_owned()` / `_assert_workflow_owned()` / `_assert_workgroup_owned()` — see exact functions in `routes/teacher.py`, `routes/workflows.py`, and `routes/workgroups.py`. **One missed assertion = silent IDOR.**
- The Modal worker uses the **anon key** + per-row `worker_token` (UUID) — its only DB access is via the `update_training_progress(p_token, ...)` RPC, which is the sole worker-writable surface and is guarded by migration `010_progress_terminal_guard.sql` so a worker cannot overwrite a `canceled` row with `succeeded`.

### 1.5 Don't introduce drift between the LeRobot pinning sites.
The exact SHA `989f3d05ba47f872d75c587e76838e9cc574857a` (huggingface/lerobot, "[Async Inference] Merge Protos & refactoring (#1480)", 2025-07-23, version 0.2.0) must agree across:
- `physical_ai_tools/lerobot/` (static snapshot, byte-identical to upstream)
- `robotis_ai_setup/modal_training/modal_app.py:19` constant `LEROBOT_COMMIT`
- The base `robotis/physical-ai-server:amd64-0.8.2` image (whose internal `lerobot` submodule resolves to this same SHA via frozen pin — the `branch = feature-robotis` hint in the upstream `.gitmodules` is dead weight)
- `meta/info.json` `codebase_version: "v2.1"` (derived from `lerobot.datasets.lerobot_dataset.CODEBASE_VERSION` at this SHA)
- Modal preflight in `training_handler.py` enforces `codebase_version == "v2.1"`

Bumping LeRobot is a **5-place change in one PR** ([§13.2 replace-or-upgrade workflow](#13-workflows-for-claude)). Modal also force-reinstalls torch+torchvision from `https://download.pytorch.org/whl/cu121` and uninstalls `torchcodec` — without that, pip picks `cu130` wheels which crash the cu121 base.

---

## 2. Repo layout

```
Testre/
├── CLAUDE.md                              ← THIS FILE (single source of truth)
├── VERSION                                ← "2.2.2" (consumed by gui/app/constants.py)
├── .gitattributes                         ← LF-forced for *.sh, Dockerfile, daemon.json, .s6-keep, docker-compose*.yml
├── .gitignore                             ← gitignored: *.env, gui/dist/, installer/output/, *.tar.gz, .claude/
├── .github/workflows/ci.yml               ← 6 jobs: python-tests, shell-lint, compose-validate, overlay-guard, manager-build-validate, nginx-validate
│
├── open_manipulator/                      ← ROBOTIS upstream (absorbed, ROS2 Jazzy + Dynamixel)
│   ├── open_manipulator_bringup/          ← launch/omx_f_follower_ai.launch.py (the magic remap is on line ~144)
│   ├── open_manipulator_description/      ← URDF xacros + ros2_control/*.xacro
│   ├── open_manipulator_collision/        open_manipulator_gui/  open_manipulator_moveit_config/
│   ├── open_manipulator_playground/       open_manipulator_teleop/  ros2_controller/
│   └── docker/
│
├── physical_ai_tools/                     ← ROBOTIS upstream (absorbed)
│   ├── physical_ai_server/                ← ROS2 node (data recording + inference + Roboter Studio)
│   │   ├── physical_ai_server/            (subpackages: communication, data_processing, device_manager,
│   │   │                                   evaluation, inference, timer, training, utils, video_encoder, workflow)
│   │   ├── launch/  config/  test/        Dockerfile.amd64  Dockerfile.arm64
│   ├── physical_ai_manager/               ← React 19 SPA (nginx, ports 80/9090, dual-mode build)
│   │   ├── src/{App.js, StudentApp.js, WebApp.js, components/, features/, hooks/, services/, utils/, store/, lib/, pages/, constants/}
│   │   ├── Dockerfile (student)  Dockerfile.web (Railway)  nginx.conf  nginx.web.conf.template
│   │   ├── package.json (v0.8.2)  railway.json  vercel.json (kill-switch)
│   ├── physical_ai_interfaces/            ← custom msg/srv (TaskInfo/Status, TrainingInfo/Status,
│   │                                        SendCommand, GetSavedPolicyList, Detection, WorkflowStatus,
│   │                                        StartCalibration, CalibrationCaptureColor, ...)
│   ├── physical_ai_bt/                    ← Behavior trees (XML), ffw_sg2_rev1.xml
│   ├── lerobot/                           ← LeRobot v0.2.0 snapshot @ 989f3d05 (static, byte-identical, NOT modified)
│   └── rosbag_recorder/                   ← C++ bag recorder service (PREPARE/START/STOP/STOP_AND_DELETE/FINISH)
│
├── robotis_ai_setup/                      ← OUR custom code (everything we wrote)
│   ├── cloud_training_api/                ← FastAPI on Railway (training jobs + teacher/admin/me/workflows API)
│   │   ├── Dockerfile  requirements.txt  .env.example
│   │   └── app/{main.py, auth.py, services/, routes/, validators/}
│   ├── modal_training/                    ← Modal app + handler for cloud GPU training
│   │   ├── modal_app.py                   ← Image build, function `train`, secrets, GPU=L4, timeout=7h
│   │   └── training_handler.py            ← run_training() flow with German preflight + RPC + HF upload
│   ├── docker/                            ← Compose, build-images.sh, overlays, patches
│   │   ├── docker-compose.yml  docker-compose.gpu.yml  .env.template  build-images.sh  bump-upstream-digests.sh  BASE_IMAGE_PINNING.md
│   │   ├── physical_ai_server/{Dockerfile, overlays/, patches/}
│   │   └── open_manipulator/{Dockerfile, entrypoint_omx.sh, identify_arm.py, overlays/}
│   ├── supabase/                          ← migration.sql + 002-010 + rollback/
│   ├── gui/                               ← Windows tkinter GUI (PyInstaller .exe)
│   │   ├── main.py  build.spec  requirements.txt
│   │   └── app/{constants.py, gui_app.py, config_generator.py, device_manager.py, docker_manager.py,
│   │            health_checker.py, update_checker.py, webview_window.py, wsl_bridge.py}
│   ├── installer/                         ← Inno Setup .iss + 9 PowerShell scripts
│   │   ├── robotis_ai_setup.iss
│   │   └── scripts/{install_prerequisites, configure_wsl, configure_usbipd, import_edubotics_wsl,
│   │                pull_images, verify_system, migrate_from_docker_desktop, finalize_install,
│   │                uninstall_stop_containers}.ps1
│   ├── wsl_rootfs/                        ← Ubuntu 22.04 + Docker 27.5.1 rootfs builder
│   │   ├── Dockerfile  build_rootfs.sh  daemon.json  start-dockerd.sh  wsl.conf  README.md
│   ├── scripts/bootstrap_admin.py         ← Run once to create the first admin
│   └── tests/                             ← 5 unittest files (Windows-only, all mocked)
│
└── tools/                                 ← Classroom helpers
    ├── classroom_kit_README.md
    ├── generate_apriltags.py              ← Generates printable AprilTag PDFs
    ├── generate_charuco.py                ← Generates printable ChArUco boards (7x5, 30/22 mm, DICT_5X5_250)
    ├── generate_gripper_adapter.py        ← Parametric gripper-to-board STL
    └── gripper_charuco_adapter.stl
```

There is **no** `_upstream/`, **no** `.gitmodules`, **no** `modal_mcp/` in this repo (older context docs referenced these — they were never present here).

---

## 3. Architecture: 9 layers

```
 Student / Teacher / Admin
   │                                 │
   │ Windows installer               │ Browser (Railway)
   ▼                                 ▼
┌─────────────────┐         ┌────────────────────┐
│  EduBotics.exe  │         │  React SPA (web)   │
│  (tkinter +     │         │  Dockerfile.web →  │
│   PyInstaller)  │         │  Railway nginx     │
└──────┬──────────┘         └─────────┬──────────┘
       │ wsl -d EduBotics -- docker ...          │
       ▼                                          │
┌────────────────────────────────────────┐       │
│ EduBotics WSL2 distro (Ubuntu 22.04)   │       │
│ Docker 27.5.1 + containerd 1.7.27 +    │       │
│ NVIDIA Container Toolkit, dockerd      │       │
│ ┌────────────────────────────────────┐ │       │
│ │ open_manipulator container         │ │       │
│ │   ROS2 Jazzy + Dynamixel + 2 USB   │ │       │
│ ├────────────────────────────────────┤ │       │
│ │ physical_ai_server container       │ │       │
│ │   ROS2 + PyTorch + LeRobot + s6 +  │ │       │
│ │   Roboter Studio (cv2.aruco/YOLO/  │ │       │
│ │   pupil-apriltags/PyKDL)           │ │       │
│ ├────────────────────────────────────┤ │       │
│ │ physical_ai_manager container      │ │ ← localhost:80  │
│ │   nginx + React student build      │ │       │
│ └────────────────────────────────────┘ │       │
└────────────┬───────────────────────────┘       │
             │ HTTPS POST /trainings/start ...   │
             ▼                                    ▼
   ┌────────────────────────────────────────────────┐
   │  Railway FastAPI (cloud_training_api)          │
   │  scintillating-empathy-production-9efd         │
   │  uvicorn --workers 1                           │
   └────┬──────────────────────────┬────────────────┘
        │ Modal SDK .spawn(...)    │ supabase-py (service-role key)
        ▼                          ▼
   ┌──────────────────┐   ┌──────────────────────┐
   │ Modal worker     │   │ Supabase Postgres    │
   │ edubotics-       │◄──┤ (Auth + Realtime)    │
   │ training fn=     │   │  — anon key,         │
   │ train, NVIDIA L4 │   │   per-row token      │
   └──────┬───────────┘   └──────────────────────┘
          │ HfApi.upload_large_folder() (1h timeout)
          ▼
   ┌──────────────────┐
   │ HuggingFace Hub  │
   │ EduBotics-       │
   │ Solutions/*      │
   └──────────────────┘
```

| # | Layer | Key files | Owner |
|---|---|---|---|
| 1 | Windows installer + WSL2 rootfs | `robotis_ai_setup/installer/`, `robotis_ai_setup/wsl_rootfs/` | Our code |
| 2 | Windows tkinter GUI (`EduBotics.exe`) | `robotis_ai_setup/gui/` | Our code |
| 3 | Robot-arm connection (ROS2 + Dynamixel) | `open_manipulator/`, `robotis_ai_setup/docker/open_manipulator/` | ROBOTIS upstream + our overlay |
| 4 | Docker Compose (3 containers) | `robotis_ai_setup/docker/` | Our code |
| 5 | Dataset recording (LeRobot v2.1) | `physical_ai_tools/physical_ai_server/`, overlays in `robotis_ai_setup/docker/physical_ai_server/overlays/` | ROBOTIS upstream + our overlays |
| 6 | React SPA (student + web) | `physical_ai_tools/physical_ai_manager/` | ROBOTIS upstream (heavily hacked) |
| 7 | Cloud training (Railway + Modal + Supabase) | `robotis_ai_setup/cloud_training_api/`, `modal_training/`, `supabase/` | Our code |
| 8 | Inference (load policy → drive arm) | `overlays/inference_manager.py` + upstream `inference/` | ROBOTIS upstream + our overlays |
| 9 | Roboter Studio (Block-based authoring + classical CV) | `overlays/workflow/`, `routes/workflows.py`, supabase migration `008_workflows.sql` | Our code |

---

## 4. Infrastructure inventory

| Service | Account / Project | What it hosts | Cost driver |
|---|---|---|---|
| Docker Hub | `nettername/*` | 3 student images: `physical-ai-manager`, `physical-ai-server`, `open-manipulator` | Free (public) |
| Docker Hub (base) | `robotis/*` | `robotis/open-manipulator:amd64-4.1.4`, `robotis/physical-ai-server:amd64-0.8.2` | Free (public) |
| Railway | API service `scintillating-empathy-production-9efd` | FastAPI cloud_training_api (`uvicorn --workers 1`) | Hobby plan |
| Railway | Web service | React app in `web` mode (admin/teacher dashboard) via `Dockerfile.web` + `nginx.web.conf.template`, listens on `${PORT}` (Railway-injected) | Hobby plan |
| Modal | Workspace `svendanilborodun`, app `edubotics-training`, fn `train` | NVIDIA L4 (24 GB), `timeout=7*3600`, `min_containers=0` | Per GPU-hour |
| Supabase | Postgres + Auth + Realtime | 7 tables + ~5 RPCs + Realtime publications for `trainings` and `workflows` | Free tier |
| HuggingFace | `EduBotics-Solutions/*` org | Datasets + trained model checkpoints | Free (public by default — privacy concern) |
| GitHub | `SvenDanilBorodun/Testre` (private) | Source | Free |

---

## 5. Critical architectural choices (don't undo without explicit user agreement)

### 5.1 No Docker Desktop
Docker Engine runs inside a bundled WSL2 distro called `EduBotics`. The GUI invokes Docker via `wsl -d EduBotics -- docker ...` (wrapped by `_docker_cmd()` in `gui/app/docker_manager.py`). USB devices reach the distro via `usbipd attach --wsl --distribution EduBotics --busid X`. Reasons: no Docker Desktop license prompt, no tray sprawl, control over Docker version (pinned `5:27.5.1-1~ubuntu.22.04~jammy` + containerd `1.7.27-1`, both held via `apt-mark hold`), headless dockerd starts on distro boot via `/usr/local/bin/start-dockerd.sh` (re-spawn watchdog every 5s).

### 5.2 Service-role key + Python ownership checks
The Railway FastAPI uses `SUPABASE_SERVICE_ROLE_KEY` everywhere (bypasses RLS). Authorization is enforced in Python via `_assert_classroom_owned()`, `_assert_student_owned()`, `_assert_entry_owned()`, `_assert_workflow_owned()`. Switching to anon-key + authoritative RLS would be a significant rewrite.

### 5.3 Overlay-with-sha256-verify
ROBOTIS upstream files are `find`'d and replaced by overlays in the Dockerfiles. Each overlay is sha256-verified before AND after copy. **Mandatory** files & path filters (verified against `docker/physical_ai_server/Dockerfile` lines 76-129):

| Overlay | Path filter | Replaces / Adds |
|---|---|---|
| `inference_manager.py` | `*/inference/*` | NaN/Inf guard, joint clamp, velocity cap, stale-camera halt, image shape + camera exact-name validation |
| `data_manager.py` | `*/data_processing/*` | RAM truncation early-save (`EDUBOTICS_RAM_LIMIT_GB`, default **0.8 GB**), video file verification, episode validation, HF upload 1h timeout |
| `data_converter.py` | `*/data_processing/*` | Empty-trajectory guard (German `[FEHLER] JointTrajectory hat keine Punkte...`), missing-joint error, fps-aware action timing |
| `omx_f_config.yaml` | (no filter) | Dual-camera config + tightened safety_envelope (joint1-3 ±π/2, joint4-5 ±0.85π, gripper ±1.0, max_delta_per_tick_at_30hz [0.3]×6) |
| `physical_ai_server.py` | `*/physical_ai_server/physical_ai_server.py` | Handles None returns from new safety envelope |
| `communicator.py` | `*/communication/communicator.py` | Adds `get_latest_bgr_frame()` + `get_latest_follower_joints()` for Roboter Studio calibration provider |

Open-manipulator overlays (in `docker/open_manipulator/Dockerfile`):
- `omx_f.ros2_control.xacro` (path filter `*/ros2_control/*`) — follower joint limits, gripper Op Mode 5, **350 mA** current limit on dxl16
- `omx_f_hardware_controller_manager.yaml` (`*/omx_f_follower_ai/*`) — JointTrajectoryController @ 100 Hz, joint constraints
- `omx_l.ros2_control.xacro` (`*/ros2_control/*`) — leader joints 1-5 in Velocity mode (state-only, gravity comp), dxl6 in Op Mode 5 with **300 mA** limit
- `omx_l_leader_ai.launch.py` (`*/launch/*`) — leader controller spawner
- `omx_l_leader_ai_hardware_controller_manager.yaml` (`*/omx_l_leader_ai/*`) — gravity_compensation_controller, trigger_position_controller, joint_trajectory_command_broadcaster

Patches (run **before** overlays):
- `patches/fix_server_inference.py` — initializes `self._endpoints = {}` and removes duplicate `InferenceManager` construction in upstream `server_inference.py`. **Self-verifies; exits 2 or 3 on no-op** (CI tests this with a fake input file).
- `patches/kdl_parser_py/{__init__.py, urdf.py}` — vendored from `ros/kdl_parser@humble` (pure Python, ~126 lines). Removed `treeFromParam` (ROS 1 only) and replaced `kdl.Joint.None` with `kdl.Joint.Fixed` (Python 3 SyntaxError fix).

LeRobot itself is **not** overlaid — it must be byte-identical to upstream `989f3d05`.

### 5.4 Roboter Studio (Block-based authoring) is bolted on, not in a separate container

The `physical_ai_server/Dockerfile` does **three** Roboter-Studio-specific things on top of the base image:
1. **Re-builds `physical_ai_interfaces`** because the base image was built before commit `d408378` added the new msgs/srvs (Detection, WorkflowStatus, StartCalibration, CalibrationCaptureColor, AutoPoseSuggest, ExecuteCalibrationPose, MarkDestination, StartWorkflow, StopWorkflow). Without this, `physical_ai_server.py` would crash at import with `ImportError: cannot import name 'StartCalibration'`. Asserts presence of generated Python files post-`colcon build`.
2. **Installs runtime deps**: `opencv-contrib-python==4.10.0.84` (cv2.aruco — main `opencv-python` does not include contrib), `pupil-apriltags==1.0.4` (BSD AprilTag), `onnxruntime==1.20.1` (CPU-only YOLOX-tiny), `pip-licenses==5.0.0` (AGPL audit), `urdf-parser-py==0.0.4`. `ENV CMAKE_POLICY_VERSION_MINIMUM=3.5` is required because pupil-apriltags's CMakeLists doesn't accept CMake 4.
3. **Downloads YOLOX-tiny ONNX** from a pinned GitHub release URL (`https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx`) and verifies SHA-256 `427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7`. Stored at `/opt/edubotics/yolox_tiny.onnx`. Defends against a future GitHub re-upload silently swapping weights.

Then it **copies in the entire workflow module** (`overlays/workflow/`) as an addition to `physical_ai_server.workflow` (not as an overlay, since there is no upstream file to compare). 12 files: `__init__.py`, `auto_pose.py`, `calibration_manager.py`, `coco_classes.py`, `color_profile.py`, `ik_solver.py`, `interpreter.py`, `perception.py`, `projection.py`, `safety_envelope.py`, `trajectory_builder.py`, `workflow_manager.py` (+ `handlers/{__init__.py, motion.py, output.py, destinations.py, perception_blocks.py}`).

### 5.5 ROS2 `/leader/joint_trajectory` is the action rail
The follower's `arm_controller` default action topic is **remapped** in `open_manipulator/open_manipulator_bringup/launch/omx_f_follower_ai.launch.py` (line ~144):
```python
remappings=[('/arm_controller/joint_trajectory', '/leader/joint_trajectory')]
```
This means anything publishing to `/leader/joint_trajectory` drives the follower:
- Leader's `joint_trajectory_command_broadcaster` (teleoperation: leader's observed positions follow-the-leader; gripper joint reversed)
- Entrypoint's quintic-sync trajectory at startup
- Inference node's predicted actions
- Roboter Studio workflow runtime

`ROS_DOMAIN_ID=30` is the legacy default, but `gui/app/config_generator.py:_resolve_ros_domain_id()` derives a per-machine UUID-hash mod 233 on first run (override via `EDUBOTICS_ROS_DOMAIN`). Without per-machine domains, two students on the same school Wi-Fi share domain 30 and cross-talk.

### 5.6 React dual mode
One React 19 codebase (`physical_ai_tools/physical_ai_manager/`), two builds:
- `Dockerfile` (student build, `REACT_APP_MODE=student`, `REACT_APP_ALLOWED_POLICIES=act`): ships in the `physical-ai-manager` image, talks to local rosbridge `ws://hostname:9090` and the Railway API.
- `Dockerfile.web` (Railway, `REACT_APP_MODE=web`, `REACT_APP_ALLOWED_POLICIES=tdmpc,diffusion,act,vqbet,pi0,pi0fast,smolvla`): no rosbridge, admin/teacher dashboard. Listens on `${PORT}` from Railway, includes 5 strict security headers (HSTS, X-Frame-Options DENY, X-Content-Type-Options nosniff, Referrer-Policy, Permissions-Policy).

`vercel.json` is intentionally a **kill-switch** (empty object) to block accidental shadow Vercel deploys.

### 5.7 German UI / English code
Target users are German students. `public/index.html` declares `<html lang="de">`. Tkinter strings, React UI, error messages returned to the student/teacher are in German. Code, comments, internal logs are English.

---

## 6. End-to-end pipeline (one student's workflow)

### 6.1 Install
`EduBotics_Setup.exe` (Inno Setup, AppId `{B7E3F2A1-8C4D-4E5F-9A6B-1D2E3F4A5B6C}`, AppVersion **2.2.3** in `installer/robotis_ai_setup.iss`, `PrivilegesRequired=admin`). Ships:
- `assets/edubotics-rootfs.tar.gz` (~193 MB) + `.sha256` sidecar — gitignored, built locally via `wsl_rootfs/build_rootfs.sh`.
- `gui/dist/EduBotics/*` (PyInstaller output)
- License + icon

User-writable env file lives at `%LOCALAPPDATA%\EduBotics\.env` (moved out of Program Files in v2.2.1 to avoid UAC on regen).

[Run] order in `.iss`:
1. `migrate_from_docker_desktop.ps1` — silently uninstalls Docker Desktop (best-effort), unregisters `docker-desktop` + `docker-desktop-data` distros, removes auto-start registry. Idempotent via `.migrated` marker.
2. `install_prerequisites.ps1` — Win11 ≥ build 22000, reject Home edition, virtualization warning, CFA warning, `wsl --install --no-distribution`, download + install `usbipd-win` 5.3.0 MSI with **SHA256 verify** `1C984914AEC944DE19B64EFF232421439629699F8138E3DDC29301175BC6D938`. On WSL kernel install drops `.reboot_required` marker.
3. `configure_wsl.ps1` — merges `memory=8GB swap=4GB` into the **logged-in user's** `~/.wslconfig` (resolves real user via WMI `Win32_Process.GetOwner()` on `explorer.exe`). Leaves `networkingMode` at default (NAT).
4. `configure_usbipd.ps1` — adds `usbipd policy add` for ROBOTIS VID `2F5D` and PIDs `0103` (OpenRB-150) and `2202`. Handles usbipd 4.x vs 5.x API drift (`--operation AutoBind` is 5.x-only).
5. `import_edubotics_wsl.ps1` (skipped if `.reboot_required`) — preflights ≥20 GB free, verifies tarball SHA256, `wsl --unregister EduBotics` if present, `wsl --import EduBotics %ProgramData%\EduBotics\wsl assets\edubotics-rootfs.tar.gz --version 2`, polls `docker info` up to 180s, falls back to `start-dockerd.sh`.
6. `pull_images.ps1` (skipped if `.reboot_required` or distro missing) — reads `docker/versions.env` for `IMAGE_TAG` + `REGISTRY` (falls back to `:latest`/`nettername`), pulls 3 images, prunes dangling.
7. `verify_system.ps1` — 7-point check: WSL2, distro, dockerd, usbipd, images, NVIDIA, install dir files.

If `.reboot_required` exists, Inno's `NeedRestart()` returns true → user reboots → on next GUI launch the GUI calls `finalize_install.ps1` with UAC, which deletes the marker and runs steps 5+6.

[UninstallRun]: `uninstall_stop_containers.ps1` (best-effort `docker compose down`) → `wsl --unregister EduBotics` (clears VHDX → **destroys named volumes** `ai_workspace`, `huggingface_cache`, `edubotics_calib`).

### 6.2 GUI startup (`gui/app/gui_app.py`, ~1000 lines)

`gui/main.py` dispatches on `--webview`: default → `gui_app.run()` (tkinter), with sentinel → `webview_window.run_in_process()` (subprocess-only pywebview because pywebview 6 demands main-thread ownership).

Wizard order:
1. **Update gate** — `update_checker.check_for_update()` polls `/version` on Railway. If newer, **blocking** modal → download to `%TEMP%` → `os.startfile()`.
2. **Cloud-only checkbox** — if checked, skips arm/camera scan and starts only `physical_ai_manager` (with `--no-deps`); appends `?cloud=1` to the WebView URL so React skips rosbridge gate.
3. **Arm scan** — daemon thread runs `device_manager.scan_and_identify_arms()`: `usbipd list` → filter VID `2F5D` → attach all → start a throw-away `nettername/open-manipulator` container with `--privileged -v /dev:/dev --entrypoint sleep 120` → for each `/dev/serial/by-id/...`, run `docker exec ... identify_arm.py <port>` (pings IDs 1-6 and 11-16 at 1 Mbps).
4. **Camera scan** — daemon thread runs `wsl_bridge.list_video_devices()` (iterates `/dev/video*` with `v4l2-ctl --info`). Up to 2 cameras with role assignment dropdowns.
5. **Start button** — runs off UI thread:
   - Re-attach all USB → poll `/dev/serial/by-id/` (10× 1s)
   - Regenerate `.env` via `config_generator.generate_env_file()` with `_atomic_write()` (write to `.tmp` + `os.replace()`, guards power-loss). Keys: `FOLLOWER_PORT`, `LEADER_PORT`, `CAMERA_DEVICE_1`, `CAMERA_NAME_1` (default `gripper`), `CAMERA_DEVICE_2`, `CAMERA_NAME_2` (default `scene`), `ROS_DOMAIN_ID`, `REGISTRY`. All values **double-quoted** (paths with spaces).
   - `docker compose --env-file ... -f docker-compose.yml [-f docker-compose.gpu.yml] up -d --force-recreate` (GPU compose layered if `nvidia-smi` succeeds on host)
   - Poll `:80/version.json` until 200 → spawn WebView2 subprocess
6. **Pull stall watchdog** — `docker_manager._pull_one_image()`: 20s poll interval, 10 MB disk-growth threshold (reads `/var/lib/docker/overlay2`), 600s `stall_timeout`. On stall: `pkill -KILL dockerd`, restart, retry with exp backoff `min(4*2^(attempt-1), 30)` s, max 4 retries. **Main knob for poor-network classrooms.**

UAC elevation uses `ShellExecuteExW` directly (Win32) so it gets a real process handle for `WaitForSingleObject` (PS `Start-Process` doesn't).

### 6.3 Robot-arm bringup (`docker/open_manipulator/entrypoint_omx.sh`)

PID 1 (no systemd). Five phases:
1. **Validate hardware** — `wait_for_device()` polls each port up to 60s, `chmod 666 $FOLLOWER_PORT $LEADER_PORT`. Exports `ROS_DOMAIN_ID`.
2. **Launch leader first** — `ros2 launch ... omx_l_leader_ai.launch.py port_name:=$LEADER_PORT`. Wait ≤30s for `/leader/joint_states`.
3. **Read leader position** — inline Python subscriber reads first complete `/leader/joint_states` (joint1-5 + `gripper_joint_1`), JSON-encodes positions to `LEADER_POS`.
4. **Launch follower + sync** — `ros2 launch ... omx_f_follower_ai.launch.py port_name:=$FOLLOWER_PORT`. Wait ≤60s for `/joint_states`. Then publish a **quintic-polynomial** trajectory (`s(t) = 10t³ − 15t⁴ + 6t⁵`, with explicit `s_dot` and `s_ddot`) over **3 seconds** with **50 waypoints** to `/leader/joint_trajectory`. After motion, verify follower reached target within **0.08 rad tolerance** per joint (polled every 0.1s for up to 2s); **hard-fail exit 2** on mismatch refuses to continue.
5. **Launch cameras** — up to 2 `usb_cam` nodes, topics `/{name}/image_raw/compressed`.

`trap` on SIGTERM/SIGINT calls `disable_torque()` (SetBool service on both arms, 2s timeout each) then kills launch children. Final `wait` keeps PID 1 alive.

### 6.4 Recording (LeRobot v2.1)

React triggers `/task/command` (`SendCommand.srv`, command code `START_RECORD=1`) with a `TaskInfo` payload (`task_name`, `task_instruction[]`, `fps`, `warmup_time_s`, `episode_time_s`, `reset_time_s`, `num_episodes`, `push_to_hub`, `record_rosbag2`, `use_optimized_save_mode`).

`physical_ai_server.py` → `user_interaction_callback` → `data_manager.start_recording()`. State machine: `warmup → run → save → reset → (loop) → finish`. Each phase pushes `TaskStatus` to `/task/status`.

Per-tick (typically 30 Hz):
1. `communicator.get_latest_data()` blocks ≤5s/topic.
2. `data_converter`:
   - Images: `cv_bridge.compressed_imgmsg_to_cv2()` → BGR → `cvtColor(..., BGR2RGB)` → `uint8` HWC.
   - Follower: `JointState → joint_state2tensor_array()` reorders per `joint_order` (default `[joint1, joint2, joint3, joint4, joint5, gripper_joint_1]`) → `float32 [6]`.
   - Leader action: `joint_trajectory2tensor_array()` reads `points[0].positions`, reorders. **Overlay raises German `[FEHLER] JointTrajectory hat keine Punkte — Leader-Arm sendet möglicherweise nicht.`** if empty (upstream silently accepts).
3. `create_frame()` → `{'observation.images.gripper': ..., 'observation.images.scene': ..., 'observation.state': ..., 'action': ...}`, all `float32`.
4. `add_frame_without_write_image()` validates vs schema, appends to episode buffer, auto-timestamps as `frame_index / fps` (wall-clock NOT used).
5. Video encoding: raw RGB piped to `ffmpeg libx264 -crf 28 -pix_fmt yuv420p`, **async**.
6. `save_episode_without_video_encoding()` writes parquet + mp4 + `meta/info.json` (`codebase_version: "v2.1"`).

Dataset path inside container: `~/.cache/huggingface/lerobot/{user_id}/{robot_type}_{task_name}/`. Optional rosbag2: `/workspace/rosbag2/{repo_name}/{episode_index}/`.

Error behavior (fail-loud, German):
- Missing topic (5s timeout) → `TaskStatus.error` + halt.
- Empty JointTrajectory → German `[FEHLER]` (overlay).
- Missing joint in `joint_order` → German `[FEHLER] Gelenk {e} fehlt in der Nachricht...` (overlay).
- Free RAM < `EDUBOTICS_RAM_LIMIT_GB` (default 0.8 GB) → force early save (overlay), German `[WARNUNG] Episode {num} wegen niedrigem Arbeitsspeicher (<{GB} GB frei) frueh beendet...`.
- Video file missing or zero-byte → German `[FEHLER] Episode {num}: Video-Datei(en) nicht korrekt gespeichert ({problems})...`
- HF upload runs in daemon thread, **1-hour timeout** (overlay). German `[FEHLER] HuggingFace-Upload hat das Zeitlimit (1 Stunde) ueberschritten...` on timeout.

### 6.5 Cloud training

```
React → POST /trainings/start (Railway cloud_training_api)
   ├─ _sweep_user_running_jobs (asyncio.gather of _sync_modal_status for stuck rows)
   ├─ _find_recent_duplicate (60s window, params canonicalized via json.dumps(sort_keys=True))
   ├─ HfApi().dataset_info() preflight (RepositoryNotFoundError → 400; other → 502)
   ├─ Per-policy timeout cap applied: training_params["timeout_hours"] = min(req, POLICY_MAX_TIMEOUT_HOURS[type])
   ├─ RPC start_training_safe (atomic credit lock + insert; raises P0002/P0003)
   └─ modal.Function.from_name("edubotics-training","train").spawn.aio(...)
       → cloud_job_id = FunctionCall.object_id stored in Supabase

Modal worker (modal_app.py train → training_handler.run_training)
   ├─ _preflight_dataset (60s timeout, German errors on each failure)
   │     - codebase_version == "v2.1"
   │     - fps > 0
   │     - observation.state and action joint counts in [4, 20]
   │     - joint name parity between observation.state and action
   │     - ≥1 observation.images.* feature
   ├─ _update_supabase_status("running")
   ├─ subprocess "python -m lerobot.scripts.train --policy.type=... --policy.device=cuda
   │       --dataset.repo_id=... --output_dir=... --policy.push_to_hub=false --eval_freq=0"
   │   (PYTHONUNBUFFERED=1, stderr merged to stdout, line-buffered)
   ├─ Reader: deque(maxlen=4000); regex r"step[:\s]+(\d+\.?\d*[KMBkmb]?)" + r"loss[:\s]+([\d.]+(?:e[+-]?\d+)?)"
   │     - dedupe on step (only push when current_step > last_progress_step)
   │     - 3-retry RPC update_training_progress with sleep(2^attempt) backoff
   ├─ proc.wait(timeout=timeout_hours * 3600); on TimeoutExpired → kill + German error
   ├─ _upload_model_to_hf via HfApi.upload_large_folder(checkpoints/last/pretrained_model)
   └─ _update_supabase_status("succeeded")

Frontend: useSupabaseTrainings hook (Supabase Realtime channel `trainings:{userId}`)
                            falls back to 30s poll if not realtime
```

### 6.6 Inference

React → `/task/command START_INFERENCE=2` + `task_info.policy_path` (local FS path). Server validates, sets `on_inference=True`, starts the 30 Hz ROS timer.

Per-tick (single-threaded on ROS executor, no worker thread):
1. `communicator.get_latest_data()` (5s timeout per topic).
2. `data_converter` → RGB `uint8` HWC images + `float32` state `[6]`.
3. Lazy `load_policy()` on first tick (downloads from HF via `HF_HOME=/root/.cache/huggingface` if missing; moves weights to GPU).
4. Overlay reads expected camera names from `policy_config.input_features` keys matching `observation.images.*` → exact-match. Mismatch raises German `[FEHLER] Kamera-Namen passen nicht: Modell erwartet {expected_names}, verbunden {connected_names}. Inferenz-Tick uebersprungen.`
5. **Stale-camera halt** — hash 4 sparse 256-byte slices per image (offsets 0, n/4, n/2, 3n/4); per-camera last-change time. Warn @ 2s with German `[WARNUNG] Kamera "{name}" liefert seit {duration:.1f}s dasselbe Bild...`. Halt @ 5s with German `[STOPP] Kamera "{name}" ist seit >{threshold:.0f}s eingefroren. Inferenz angehalten...`. Returns None → tick skipped.
6. Image shape validation against `_read_expected_image_shapes()`. Mismatch → German `[FEHLER] Bildaufloesung stimmt nicht ueberein: {key} hat Form {actual}, Modell erwartet {expected}. Tick uebersprungen.`
7. `_preprocess(images, state)`: per image `torch.from_numpy / 255 → permute(2,0,1) → unsqueeze(0)` keyed `observation.images.{name}`. State → `float32` tensor → batch.
8. `policy.select_action(observation)` under `torch.inference_mode()`.
9. Safety envelope: NaN/Inf reject → joint clamp → per-tick velocity cap. State `_last_action` cleared on `reset_policy()` so first action of a new episode isn't clamped against the previous episode's final action.
10. `data_converter.tensor_array2joint_msgs(action, ...)` builds `JointTrajectory` with **fps-aware `time_from_start`** (overlay computes `_action_duration_ns = max(int(1.5e9/fps), 1_000_000)`).
11. `communicator.publish_action(msg)` → `/arm_controller/follow_joint_trajectory` (which, after the magic remap, is `/leader/joint_trajectory` → drives the follower).

`gripper_joint_1` is the 6th element of the same action array, on the same `JointTrajectory`. There is no separate gripper service.

### 6.7 Roboter Studio (calibration → Blockly authoring → execution)

**Calibration** (per-camera, multi-step state machine in `overlays/workflow/calibration_manager.py`):
- ChArUco board: **7×5 squares**, **30 mm square / 22 mm marker**, dictionary **DICT_5X5_250**.
- Steps in order: intrinsic(gripper) → intrinsic(scene) → handeye(gripper, eye-in-hand) → handeye(scene, eye-to-base) → colour profile.
- `INTRINSIC_FRAMES_REQUIRED = 12`, `HANDEYE_FRAMES_REQUIRED = 14`.
- Hand-eye solved with both PARK and TSAI; warn if rotation differs > **4 deg** or translation > **10 mm**.
- Persisted under `/root/.cache/edubotics/calibration/` (named volume `edubotics_calib`, **survives `docker compose down`**) as `{camera}_intrinsics.yaml`, `{camera}_handeye.yaml`, `color_profile.yaml`.
- Provider methods come from the `communicator.py` overlay: `get_latest_bgr_frame()`, `get_latest_follower_joints()`. Without the overlay, the wizard returns `[FEHLER] Kein Kamerabild verfügbar.`

**Authoring** — React `Workshop/` editor (Blockly 12.5.0 + react-blockly 9.0.0). Block allowlist (~23 types) defined in `overlays/workflow/interpreter.py:ALLOWED_BLOCK_TYPES`:
- Motion/output (statement): `edubotics_home`, `edubotics_open_gripper`, `edubotics_close_gripper`, `edubotics_move_to`, `edubotics_pickup`, `edubotics_drop_at`, `edubotics_wait_seconds`, `edubotics_destination_pin`, `edubotics_destination_current`, `edubotics_log`, `edubotics_play_sound`.
- Perception (value): `edubotics_detect_color`, `edubotics_detect_object`, `edubotics_detect_marker`, `edubotics_count_color`, `edubotics_count_objects_class`, `edubotics_wait_until_color`, `edubotics_wait_until_object`, `edubotics_wait_until_marker`.
- Built-ins: `controls_if`, `controls_repeat_ext`, `controls_whileUntil`, `controls_for`, `controls_forEach`, `logic_compare/operation/negate/boolean`, `math_number/arithmetic`, `variables_get/set`, `text`.
- Allowed colors: `rot, gruen, blau, gelb` (German); validation rejects other strings.
- `MAX_LOOP_ITERATIONS = 10000`.

**Validation** — `cloud_training_api/app/validators/workflow.py:validate_blockly_json()` enforces:
- `MAX_BLOCKLY_JSON_BYTES = 256 * 1024` (256 KB) → 413 `Workflow ist zu groß (>256 KB).`
- `MAX_BLOCKLY_DEPTH = 64` → 400 `Workflow ist zu tief verschachtelt.`
- JSON encoding error → 400 `Workflow-JSON ist ungültig: {error}`
- `MAX_NAME_LENGTH = 100`

Both `routes/workflows.py` (student) AND teacher template route call this validator (audit fix).

**Execution** — `WorkflowManager` daemon thread, `WorkflowContext` (publisher, safety envelope, IK, perception, destinations, z_table, intrinsics, last_arm_joints). Recovery routine in `finally`: hold (1.0s) → open gripper (0.5s) → return-home over 3.0s (`HOME_JOINTS_RAD = [0.0, -π/4, π/4, 0.0, 0.0]` + `GRIPPER_OPEN_RAD = 0.8`); absolute deadline **15.0s**. Auto-home on stop/error prevents arm left mid-grasp.

**Perception** — eager initialization (any failure raises RuntimeError, no silent fallback):
- **YOLOX-tiny ONNX** at 640×640 letterbox via `onnxruntime`, COCO classes filter (~80 classes, `coco_classes.py`).
- **LAB color matching** with per-channel σ threshold (default 3.0; std floored to 1.0 to prevent divide-by-zero); `MORPH_OPEN` then `MORPH_CLOSE` (3×3 kernel); contour area ≥ `LAB_MIN_BLOB_AREA_PX = 100`.
- **AprilTag** via `pupil_apriltags` (BSD), `tag36h11` family.

**IK fallback chain** (`overlays/workflow/ik_solver.py`):
1. **TRAC-IK** preferred — `from trac_ik_python.trac_ik import IK` (timeout 0.05s, 'Distance' metric). Apt package `ros-jazzy-trac-ik-python` not yet in Jazzy/noble apt — best-effort install.
2. **PyKDL fallback** — `PyKDL.ChainIkSolverPos_LMA` + vendored `kdl_parser_py.urdf.treeFromUrdfModel`. Requires `urdf-parser-py==0.0.4` and the `python-orocos-kdl-vendor` apt package.
3. None → German `[FEHLER] Kein IK-Solver verfügbar. Bitte zuerst die Kalibrierung abschließen.`

Tolerance: ±1 mm position, ±0.57° planar rotation; z-axis rotation free when `free_yaw=True`, else ±0.57°.

---

## 7. Cloud API reference (`robotis_ai_setup/cloud_training_api/`)

`Dockerfile`: `FROM python:3.11-slim`, `CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}`. **Single worker required** (in-process rate limiter).

`requirements.txt`: `fastapi==0.115.12`, `uvicorn[standard]==0.34.2`, `supabase==2.15.1`, `modal>=0.64`, `httpx==0.28.1`, `python-dotenv==1.1.0`, `pydantic==2.11.3`, `huggingface_hub>=0.25.0`.

### 7.1 Required env vars (fail-fast at startup via `_validate_required_secrets()` in `app/main.py:30`)
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`

### 7.2 Optional env vars
| Var | Default | Used in |
|---|---|---|
| `ALLOWED_ORIGINS` | `http://localhost` | `_parse_and_validate_origins()` rejects literal `*` with credentials, wildcards, or URLs without scheme/netloc |
| `ALLOWED_POLICIES` | `tdmpc,diffusion,act,vqbet,pi0,pi0fast,smolvla` | `routes/training.py` filters request `model_type` |
| `STALLED_WORKER_MINUTES` | **`25`** | `_sync_modal_status` cancels Modal job + marks failed if `last_progress_at` older |
| `DISPATCH_LOST_MINUTES` | `10` | `_sync_modal_status` marks failed if Modal can't find job after this long |
| `MAX_TRAINING_STEPS` | `500000` | upper bound on `training_params.steps` |
| `MAX_TRAINING_BATCH_SIZE` | `256` | upper bound |
| `MAX_TRAINING_TIMEOUT_HOURS` | `12.0` | absolute upper bound (per-policy caps applied next) |
| `MODAL_TRAINING_APP_NAME` | `edubotics-training` | for staging |
| `MODAL_TRAINING_FUNCTION_NAME` | `train` | for staging |
| `HF_TOKEN` | `""` | dataset preflight + GDPR Art. 17 student artifact cleanup |
| `GUI_VERSION` | unset → 503 | `/version` |
| `GUI_DOWNLOAD_URL` | unset → 503 | `/version` |
| `POLICY_TIMEOUT_OVERRIDES_JSON` | unset | per-policy timeout overrides |

### 7.3 Per-policy timeout caps (`routes/training.py:POLICY_MAX_TIMEOUT_HOURS`)
```
act:      1.5h
vqbet:    2.0h
tdmpc:    2.0h
diffusion:4.0h
pi0fast:  4.0h
pi0:      6.0h
smolvla:  6.0h
```
Cap applied AFTER request validation but BEFORE Modal dispatch — DB row stores capped value.

### 7.4 Rate limit rules (in-process, keyed by leftmost X-Forwarded-For)
| Method | Path prefix | Limit |
|---|---|---|
| `*` | `/trainings/start` | 10 / 60s |
| `*` | `/trainings/cancel` | 20 / 60s |
| `POST` | `/workflows` | 10 / 60s |
| `POST` | `/teacher/classrooms` | 10 / 60s (covers classroom + template creation) |

Returns `JSONResponse(429, {"detail": "Too many requests — please wait a moment."})` directly (raising `HTTPException` in `BaseHTTPMiddleware.dispatch` is a Starlette footgun → would yield 500). CORS is the **outermost** middleware so the 429 still gets `Access-Control-*` headers.

### 7.5 Custom Postgres error codes
| Code | Meaning | Mapped HTTP |
|---|---|---|
| **P0001** | Worker token mismatch / training not found / training already terminal (010_progress_terminal_guard) | (worker-only — never returned to client) |
| **P0002** | User profile not found / workgroup not found (group-pool case) | 404 |
| **P0003** | No training credits remaining (per-user OR per-group depending on caller) | 403 |
| **P0010** | Classroom capacity (max 30) | 409 |
| **P0011** | Student doesn't belong to teacher | 403 |
| **P0012** | New credit amount < used | 409 |
| **P0013** | Credits would go negative | 409 |
| **P0014** | Teacher pool insufficient | 409 |
| **P0020** | Workgroup ↔ classroom mismatch (011_workgroups) | 409 |
| **P0021** | Workgroup full (max 10 students) | 409 |
| **P0022** | Workgroup not owned by teacher | 403 |
| **P0023** | adjust_student_credits refused — student is in a workgroup, use group credits | 409 |
| `22023` | Invalid status (CHECK violation in update_training_progress) | (worker-only) |

### 7.6 Endpoint inventory

**`/health`** GET → `{"status":"ok"}` (no DB hit).

**`/version`** GET → `{version, download_url, required: true}` from env, or 503 if unconfigured.

**`/me`** routes (require any auth):
| Method | Path | Purpose |
|---|---|---|
| GET | `/me` | profile + (for teachers) `get_teacher_credit_summary` |
| GET | `/me/export` | GDPR Art. 15 — JSON bundle (profile, trainings, classrooms, progress_entries) |
| POST | `/me/delete` | GDPR Art. 17 — refuse for admins (400), cancel active trainings, set `users.deletion_requested_at=now()` |

**`/trainings`** routes (require any auth):
| Method | Path | Purpose |
|---|---|---|
| GET | `/trainings/quota` | `_get_remaining_credits` via RPC `get_remaining_credits` |
| POST | `/trainings/start` | the heavyweight (sweep → dedupe → HF preflight → cap timeout → RPC `start_training_safe` → Modal `.spawn.aio()` → store `cloud_job_id`) |
| POST | `/trainings/cancel` | verify ownership → check status queued/running → `cancel_training_job(cloud_job_id)` → mark `canceled` |
| GET | `/trainings/list` | last 50 user trainings + `asyncio.gather(_sync_modal_status)` |
| GET | `/trainings/{id}` | verify ownership + sync if active |

**`/teacher`** routes (require `role=teacher`):
| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/teacher/classrooms` | list / create |
| GET/PATCH/DELETE | `/teacher/classrooms/{id}` | detail / rename / delete (409 if not empty) |
| GET/POST | `/teacher/classrooms/{id}/workflow-templates` | list / create (calls `validate_blockly_json`) |
| DELETE | `/teacher/classrooms/{id}/workflow-templates/{templateId}` | delete |
| POST | `/teacher/classrooms/{id}/students` | create student (synthetic email + `auth.admin.create_user` + `adjust_student_credits` RPC) |
| PATCH/DELETE | `/teacher/students/{id}` | update / delete (with best-effort HF artifact cleanup via `_delete_student_hf_artifacts`; skips group-shared dataset repos) |
| POST | `/teacher/students/{id}/password` | reset via `auth.admin.update_user_by_id` |
| POST | `/teacher/students/{id}/credits` | RPC `adjust_student_credits` (P0011-P0014 mapped to 403/409; P0023 → 409 when student is in a group) |
| GET | `/teacher/students/{id}/trainings` | last 100 |
| GET/POST | `/teacher/classrooms/{id}/workgroups` | list / create work groups (rate-limited 20/60s on POST via `/teacher/workgroups` prefix) |
| GET/PATCH/DELETE | `/teacher/workgroups/{id}` | detail (members + usage) / rename / delete (409 if non-empty) |
| POST | `/teacher/workgroups/{id}/members` | add a student to the group (asserts classroom match + capacity ≤10; bumps `workgroup_memberships`) |
| DELETE | `/teacher/workgroups/{id}/members/{studentId}` | remove a member (clears `users.workgroup_id`, sets `workgroup_memberships.left_at`) |
| POST | `/teacher/workgroups/{id}/credits` | RPC `adjust_workgroup_credits` (P0022 → 403, P0012-P0014 → 409) |
| GET/POST | `/teacher/classrooms/{id}/progress-entries` | list (filter by `student_id`, `workgroup_id`, or `scope=classroom\|student\|group`) / create (mutual-exclusion CHECK enforced) |
| PATCH/DELETE | `/teacher/progress-entries/{id}` | update / delete |

**`/datasets`** routes (any logged-in user):
| Method | Path | Purpose |
|---|---|---|
| GET | `/datasets` | own + group-shared via `workgroup_memberships`; each row carries `is_owned`, `is_group_shared` |
| POST | `/datasets` | register (or upsert) a freshly-uploaded HF dataset; auto-shares with the caller's current group; rate-limited 20/60s. Called by React from `useRosTopicSubscription` after the `/huggingface/status` topic reports `Success`. |
| PATCH | `/datasets/{id}` | rename / update description (owner only) |
| DELETE | `/datasets/{id}` | delete the registry row only — the HF Hub repo itself is intentionally untouched (student decides via their HF account) |

**`/admin`** routes (require `role=admin`):
| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/admin/teachers` | list (per-teacher `get_teacher_credit_summary` RPC — O(N) calls) / create |
| PATCH | `/admin/teachers/{id}/credits` | set pool_total (rejects if < allocated_total) |
| POST | `/admin/teachers/{id}/password` | reset |
| DELETE | `/admin/teachers/{id}` | refuse if classroom_count > 0 |

**`/workflows`** routes (require any auth):
| Method | Path | Purpose |
|---|---|---|
| GET | `/workflows` | own workflows (paginated) + classroom templates (up to MAX_LIST_LIMIT=500) |
| GET/PATCH/DELETE | `/workflows/{id}` | detail / update (calls `validate_blockly_json`) / delete |
| POST | `/workflows` | create (calls `validate_blockly_json`) |
| POST | `/workflows/{id}/clone` | non-template copy (owner can clone own; classmate can clone templates) |

### 7.7 Helper functions (the IDOR firewall)
- `_assert_classroom_owned(teacher_id, classroom_id)` → German `Klassenzimmer nicht gefunden`
- `_assert_student_owned(teacher_id, student_id)` → German `Schueler nicht gefunden` / `Schueler gehoert zu keinem Klassenzimmer`
- `_assert_entry_owned(teacher_id, entry_id)` → German `Eintrag nicht gefunden`
- `_assert_workflow_owned(user_id, workflow_id)` → German `Workflow nicht gefunden`

### 7.8 Selected helper details
- `_sanitize_name`: keep `[a-zA-Z0-9._-]`, replace others with `-`, strip trailing `-` (HF-safe).
- `_generate_model_name`: `EduBotics-Solutions/[output_folder-]model_type-dataset-{10randomhex}`.
- `_find_recent_duplicate`: 60s `DEDUPE_WINDOW`; canonicalizes `training_params` via `json.dumps(..., sort_keys=True, default=str)`; **excludes failed/canceled** so retry works immediately.
- `_sweep_user_running_jobs`: called at start of `/start` so stuck rows can't block credit check.
- `_sync_modal_status`: 3 outcomes — Modal terminal → flip DB; Modal can't find job and `now - requested_at > DISPATCH_LOST_THRESHOLD` → fail with German `Dispatch an Cloud-Worker fehlgeschlagen (keine Job-ID erhalten). Bitte Training neu starten — der Credit wurde freigegeben.`; running but stalled → cancel + fail with German `Worker hat ueber {n} Minuten keine Updates gesendet (vermutlich haengt der Trainings-Prozess). Job wurde abgebrochen.`. **No refund needed** — credits self-heal on terminal status.
- `MODAL_TO_DB_STATUS`: `QUEUED/IN_QUEUE → queued`, `IN_PROGRESS → running`, `COMPLETED → succeeded`, `FAILED → failed`, `CANCELLED → canceled`, `TIMED_OUT → failed`. `UNKNOWN_STATUS` is the sentinel for unrecognized Modal SDK errors and **does NOT touch the row** (preserves liveness).

### 7.9 services/usernames.py
- `USERNAME_RE = r"^[a-z0-9][a-z0-9._-]{2,31}$"` (3-32 chars, lowercase)
- `synthetic_email(username) → "{username}@edubotics.local"`
- Validation error in German: `Benutzername muss 3-32 Zeichen lang sein und darf nur Kleinbuchstaben, Ziffern, Punkt, Bindestrich und Unterstrich enthalten.`

---

## 8. Modal training (`robotis_ai_setup/modal_training/`)

### 8.1 modal_app.py (97 lines, all load-bearing)
- `LEROBOT_COMMIT = "989f3d05ba47f872d75c587e76838e9cc574857a"` (line 19)
- `app = modal.App("edubotics-training")`
- Image: `nvidia/cuda:12.1.1-devel-ubuntu22.04` + `add_python="3.11"`, apt `git ffmpeg clang build-essential`, pip `lerobot[pi0] @ git+...lerobot.git@{LEROBOT_COMMIT}` + `huggingface_hub` + `supabase`, then `torch torchvision` from `index_url=https://download.pytorch.org/whl/cu121` with `--force-reinstall`, then `pip uninstall -y torchcodec || true` (use pyav fallback), env `PYTHONUNBUFFERED=1`, `add_local_python_source("training_handler")`.
- `secrets = [modal.Secret.from_name("edubotics-training-secrets")]` — must inject `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `HF_TOKEN`.
- `@app.function(image=image, gpu="L4", timeout=7*3600, secrets=secrets, min_containers=0)` def `train(dataset_name, model_name, model_type, training_params, training_id, worker_token) -> dict`.
- `@app.function(...)` def `smoke_test()` checks torch/CUDA + required secrets.

### 8.2 training_handler.py (~700 lines)
**Constants**: `OUTPUT_DIR = Path("/tmp/training_output")`, `EXPECTED_CODEBASE_VERSION = "v2.1"`, `MIN_JOINTS = 4`, `MAX_JOINTS = 20`. Module-level `_current_job: dict | None` for signal handler.

**Flow** of `run_training()`:
1. Read `SUPABASE_URL`, `SUPABASE_ANON_KEY` from env (raises `RuntimeError` if missing); `HF_TOKEN` optional.
2. `huggingface_hub.login(token=hf_token)` if token present.
3. `_preflight_dataset(dataset_name, hf_token)` — 60s thread-join timeout on `hf_hub_download(meta/info.json)`. Validates: codebase_version == "v2.1", fps > 0, `observation.state` and `action` features exist with `.names` lists of length 4-20, joint name parity, ≥ 1 `observation.images.*`. **14 distinct German error variants** (verbatim — see file lines 192-289).
4. `_update_supabase_status("running")`.
5. `_build_training_command(...)`: `[python, -m, lerobot.scripts.train, --policy.type=..., --policy.device=cuda, --dataset.repo_id=..., --output_dir=..., --policy.push_to_hub=false, --eval_freq=0]` + optional `--seed`, `--num_workers`, `--batch_size`, `--steps`, `--log_freq`, `--save_freq`. Default `total_steps = training_params.get("steps", 100000)`.
6. `subprocess.Popen(..., stdout=PIPE, stderr=STDOUT, text=True, bufsize=1)`. Reader thread: `deque(maxlen=4000)` ring buffer; regex `r"step[:\s]+(\d+\.?\d*[KMBkmb]?)"` and `r"loss[:\s]+([\d.]+(?:e[+-]?\d+)?)"`; `_parse_abbreviated_number` handles K/M/B suffixes; only push when `step > last_progress_step` (dedupe); 3-retry RPC with `time.sleep(2 ** attempt)` backoff.
7. `proc.wait(timeout=timeout_hours * 3600)`. Default `timeout_hours = 5` (overridable via training_params; outer Modal bound is 7h). On `TimeoutExpired`: kill, wait 10s, mark failed with German `Training Zeitlimit ueberschritten ({timeout_hours}h Limit)`.
8. On non-zero return code: truncate output to 2000 chars (1000 head + `...[truncated]...` + 1000 tail), mark failed.
9. `_update_supabase_progress(total_steps, total_steps, None)` — set 100%.
10. `_upload_model_to_hf(model_name, hf_token)`: `HfApi.create_repo(exist_ok=True)` → locate `output_path/checkpoints/last/pretrained_model` (fallback: first `rglob("pretrained_model")`) → `upload_large_folder()`. Upload failure → German `Training erfolgreich, aber Model-Upload zu HuggingFace fehlgeschlagen: {err}. Checkpoint liegt im Worker-Output; bitte HF_TOKEN pruefen und Training neu starten.`
11. `_update_supabase_status("succeeded")` → return `{"status":"succeeded","model_url":...}`.

**Signal handler** `_on_shutdown(signum, frame)` (registered for SIGINT and SIGTERM; Modal preempt grace = 30s SIGINT, then SIGTERM):
- Kill subprocess (5s wait).
- 3-retry RPC update with `0.5 * (attempt+1)` backoff to mark training failed with German `Worker wurde vom Cloud-Anbieter beendet. Bitte Training neu starten.`
- `_cleanup_output(model_name)`: `shutil.rmtree(OUTPUT_DIR / model_name.replace("/", "_"), ignore_errors=True)`.

---

## 9. Supabase schema (10 migrations)

### 9.1 migration.sql (base)
- **`public.users`** (UUID PK → `auth.users.id ON DELETE CASCADE`, `email TEXT NOT NULL`, `training_credits INT DEFAULT 0`, `created_at TIMESTAMPTZ DEFAULT NOW()`)
- **`public.handle_new_user()`** TRIGGER on `auth.users AFTER INSERT` → auto-insert `public.users` row (0 credits)
- **`public.trainings`** (`id SERIAL PK`, `user_id UUID NOT NULL FK`, `status TEXT CHECK IN (queued|running|succeeded|failed|canceled) DEFAULT 'queued'`, `dataset_name`, `model_name`, `model_type`, `training_params JSONB`, `cloud_job_id TEXT`, `current_step INT DEFAULT 0`, `total_steps INT DEFAULT 0`, `current_loss REAL`, `requested_at TIMESTAMPTZ DEFAULT NOW()`, `terminated_at TIMESTAMPTZ`, `error_message TEXT`, `worker_token UUID`, `last_progress_at TIMESTAMPTZ`)
- RLS enabled; policies for users + trainings (SELECT/INSERT/UPDATE/DELETE own only)
- **`get_remaining_credits(p_user_id)`** STABLE → `(training_credits, trainings_used::BIGINT, remaining::BIGINT)` where `trainings_used = COUNT(*) FILTER (status NOT IN (failed, canceled))`. Self-healing — no counter column.
- **`update_training_progress(p_training_id, p_token, p_status, p_current_step, p_total_steps, p_current_loss, p_error_message)`** — `SECURITY DEFINER` `SET search_path = public`, validates status (RAISE 22023), updates WHERE `id=p_training_id AND worker_token=p_token`, sets `last_progress_at=NOW()`, on terminal status sets `terminated_at=NOW()` + nulls `worker_token`. RAISE `P0001 Invalid worker token or training not found` if 0 rows. `GRANT EXECUTE TO anon, authenticated`.
- **`start_training_safe(p_user_id, p_dataset, p_model_name, p_model_type, p_params, p_total_steps, p_token)`** — `SELECT training_credits FROM users WHERE id=p_user_id FOR UPDATE`, count active, RAISE `P0003 No training credits remaining` if used >= credits, INSERT, return `(training_id, remaining)`. RAISE `P0002 User not found`. `GRANT EXECUTE TO service_role`.
- 5 indexes on `trainings`.

### 9.2 002_accounts.sql — role enum + classrooms
- `CREATE TYPE public.user_role AS ENUM ('admin', 'teacher', 'student');`
- Adds to `users`: `role NOT NULL DEFAULT 'student'`, `username TEXT UNIQUE`, `full_name TEXT`, `classroom_id UUID FK ON DELETE SET NULL`, `created_by UUID FK ON DELETE SET NULL`. Indexes on role, username, classroom (partial WHERE NOT NULL).
- **`public.classrooms`** (`id UUID PK gen_random_uuid()`, `teacher_id UUID NOT NULL FK ON DELETE CASCADE`, `name TEXT NOT NULL`, `created_at`, `UNIQUE(teacher_id, name)`)
- **`enforce_classroom_capacity()`** TRIGGER BEFORE INSERT/UPDATE on users → RAISE `P0010 Klassenzimmer hat die maximale Kapazitaet (30 Schueler) erreicht`.
- **`get_teacher_credit_summary(p_teacher_id)`** → `(pool_total, allocated_total, pool_available, student_count)`. `GRANT EXECUTE TO service_role`.
- **`adjust_student_credits(p_teacher_id, p_student_id, p_delta)`** → `(new_amount, pool_available)`. RAISE codes: P0011 (not in teacher's classroom), P0012 (new < used), P0013 (negative), P0014 (pool exhausted). `GRANT EXECUTE TO service_role`.
- RLS for classrooms + users + trainings (teacher/student/admin reads).

### 9.3 003_lessons_and_notes.sql — superseded by 004 (drops these tables/types)

### 9.4 004_progress_entries.sql
- DROPs `lesson_progress`, `lessons`, `lesson_status` enum, `users.progress_note`.
- **`public.progress_entries`** (`id`, `classroom_id NOT NULL FK ON DELETE CASCADE`, `student_id FK ON DELETE CASCADE` (nullable → class-wide), `entry_date DATE DEFAULT CURRENT_DATE`, `note TEXT NOT NULL`, `created_at`, `updated_at`)
- **Two partial unique indexes**: `(student_id, entry_date) WHERE student_id IS NOT NULL`; `(classroom_id, entry_date) WHERE student_id IS NULL`.
- `touch_updated_at()` trigger.
- RLS for teacher / student (own + own-classroom-wide) / admin.

### 9.5 005_cloud_job_id.sql — `RENAME runpod_job_id TO cloud_job_id` (vendor-neutral). COMMENTs on `cloud_job_id`, `worker_token`, `last_progress_at`.

### 9.6 006_loss_history.sql
- `trainings.loss_history JSONB NOT NULL DEFAULT '[]'::jsonb` — comment: `Downsampled training loss curve: array of {"s": step, "l": loss, "t": ms}. Maintained by update_training_progress(), capped at <=300 entries.`
- Rewrites `update_training_progress` to append `{s, l, t}` (where `t = (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT`) and downsample to ≤300: keep first 1 + 199 evenly-spaced middle + 100 fresh tail.
- `ALTER PUBLICATION supabase_realtime ADD TABLE public.trainings` (idempotent DO block).

### 9.7 007_deletion_requested_at.sql — `users.deletion_requested_at TIMESTAMPTZ` + partial index. COMMENT: `Set by /me/delete when a user requests account removal. NULL for all normal users. Admin processes deletion within 30 days per GDPR Art. 17.`

### 9.8 008_workflows.sql (Roboter Studio)
- **`public.workflows`** (`id`, `owner_user_id NOT NULL FK ON DELETE CASCADE`, `classroom_id FK ON DELETE SET NULL`, `name TEXT NOT NULL`, `description TEXT DEFAULT ''`, `blockly_json JSONB NOT NULL`, `is_template BOOLEAN DEFAULT FALSE`, `created_at`, `updated_at`)
- 2 indexes (`owner_user_id, updated_at DESC` and `classroom_id, updated_at DESC WHERE is_template`)
- `touch_updated_at()` trigger
- 4 SELECT policies (owner / classroom members read templates / teacher reads classroom templates / admin all)
- `ALTER PUBLICATION supabase_realtime ADD TABLE public.workflows`

### 9.9 009_workflows_rls_writes.sql
- 3 write policies: `Owner inserts/updates/deletes own workflows` (`WITH CHECK owner_user_id = auth.uid()`)
- CHECK `chk_template_has_classroom`: `NOT (is_template = TRUE AND classroom_id IS NULL)`
- One-time UPDATE clears orphan templates before constraint.

### 9.10 010_progress_terminal_guard.sql — adds `AND status NOT IN ('succeeded','failed','canceled')` to the `update_training_progress` WHERE clause. Prevents a worker writing `succeeded` after the user clicked Cancel. Error message becomes `Invalid worker token, training not found, or training already terminal`. `P0001`.

### 9.11 011_workgroups.sql (Arbeitsgruppen — work groups inside classrooms)
- New table `public.workgroups` (id, classroom_id FK CASCADE, name UNIQUE per classroom, shared_credits, created_at, updated_at).
- New table `public.workgroup_memberships` (workgroup_id, user_id, joined_at, left_at NULL) — append-only audit so visibility outlives a member's removal.
- New table `public.datasets` (owner_user_id FK, workgroup_id FK SET NULL, hf_repo_id, name, description, episode_count, total_frames, fps, robot_type, UNIQUE(owner_user_id, hf_repo_id)). The first place HF Hub datasets are tracked in Postgres.
- Adds `workgroup_id UUID NULL` to `users`, `trainings`, `workflows`, `progress_entries` (FK → workgroups, all SET NULL on delete except progress_entries which CASCADEs).
- `progress_entries` gains a CHECK `chk_progress_scope` (cannot have BOTH student_id AND workgroup_id) plus a new partial unique index `uniq_progress_entries_workgroup_day`. The pre-existing classroom-wide unique index is tightened to `WHERE student_id IS NULL AND workgroup_id IS NULL`.
- New triggers: `enforce_workgroup_capacity` (max 10 per group, P0021), `enforce_workgroup_classroom_match` (group must belong to the same classroom as the student, P0020).
- **`start_training_safe` redesigned** — when the caller has a `workgroup_id`, locks `workgroups.shared_credits FOR UPDATE` and counts active group trainings instead of the per-user pool. Same return shape so callers do not branch.
- **`get_remaining_credits` redesigned** — returns group quota (shared_credits − active group trainings) when grouped, per-user otherwise. Same column shape.
- New RPC `adjust_workgroup_credits(p_teacher_id, p_workgroup_id, p_delta)` — mirrors `adjust_student_credits`. Locks teacher + group rows; pool check sums per-student credits AND every workgroup's shared_credits in this teacher's classrooms.
- `adjust_student_credits` now refuses with P0023 if the student is in a workgroup (credits flow through the group instead).
- `get_teacher_credit_summary` extended with `group_count` and `group_credits_total` columns.
- New RLS read policies: `Group members read group trainings/workflows` and `Group members read group datasets` (use `workgroup_memberships` so former members keep historical visibility).
- `Students read own + own-classroom entries` extended to include `workgroup_id`-scoped entries the student is/was a member of.
- `progress_entries` SELECT for student now reads as: own entries OR class-wide-no-group OR group entries the student belongs to.
- Realtime publication adds `public.workgroups` and `public.datasets` (`trainings` and `workflows` were already there).
- Lifecycle (per spec): trainings/datasets/workflows stay visible to former group members via the audit table; deleting a group is refused while members exist; on group delete, allocated `shared_credits` returns to the teacher pool naturally (the summary RPC sums shared_credits and the row is gone).

All migrations have rollbacks under `supabase/rollback/` (BEGIN/COMMIT-wrapped). 010 rollback restores the 006-version body.

### 9.11 Bootstrap admin (run once)
```bash
cd robotis_ai_setup
python scripts/bootstrap_admin.py --username admin --full-name "Sven"
```
Reads `cloud_training_api/.env` for `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`. Validates username (regex `^[a-z0-9][a-z0-9._-]{2,31}$`). Prompts for password (≥6 chars, twice for confirm). `auth.admin.create_user` with `email_confirm=True` → `users.update(role='admin', username, full_name)`. Rolls back via `auth.admin.delete_user(user_id)` if profile update fails.

---

## 10. Docker compose & image build

### 10.1 `docker/docker-compose.yml` services (all on `ros_net` bridge, all `tty: true restart: unless-stopped`)

**`open_manipulator`** (image `${REGISTRY:-nettername}/open-manipulator:latest`):
- `privileged: true`, `cap_add: SYS_NICE`, ulimits `rtprio=99 rttime=-1 memlock=8428281856`
- volumes `/dev:/dev`, `/dev/shm:/dev/shm`, `/etc/timezone:ro`, `/etc/localtime:ro`
- env: `ROS_DOMAIN_ID`, `FOLLOWER_PORT`, `LEADER_PORT`, `CAMERA_DEVICE_1/2`, `CAMERA_NAME_1/2`
- `mem_limit: 2g`, `pids_limit: 512`
- Healthcheck: `ros2 topic list | grep -q /joint_states` (interval 10s, timeout 5s, retries 3, start_period 120s)

**`physical_ai_server`** (image `${REGISTRY:-nettername}/physical-ai-server:latest`):
- `depends_on: open_manipulator: service_healthy`
- ports `127.0.0.1:8080:8080` (web_video_server), `127.0.0.1:9090:9090` (rosbridge — loopback-bound; **rosbridge has no auth**)
- volumes: `/dev/shm:/dev/shm`, timezone files, named volumes `ai_workspace:/workspace`, `huggingface_cache:/root/.cache/huggingface`, `edubotics_calib:/root/.cache/edubotics`, agent socket dir, AND **`./physical_ai_server/.s6-keep:/etc/s6-overlay/s6-rc.d/user/contents.d/physical_ai_server:ro`** (the `.s6-keep` mount is what *enables* the s6 longrun service inside the base image — remove the mount and the ROS node never runs)
- `mem_limit: 6g`, `pids_limit: 1024`
- Healthcheck: TCP 127.0.0.1:9090

**`physical_ai_manager`** (image `${REGISTRY:-nettername}/physical-ai-manager:latest`):
- `depends_on: physical_ai_server: service_healthy`
- ports `127.0.0.1:80:80`
- `mem_limit: 512m`, `pids_limit: 128`
- Healthcheck: `wget -q -O /dev/null http://localhost/version.json`

**Volumes**: `ai_workspace`, `huggingface_cache` (size cap advisory via `EDUBOTICS_HF_CACHE_SIZE`), `edubotics_calib` (calibration data — **survives `compose down`**, only `docker volume rm` deletes).

### 10.2 `docker-compose.gpu.yml` — adds `runtime: nvidia` + GPU device reservation **only** to `physical_ai_server`. Layered via `-f docker-compose.yml -f docker-compose.gpu.yml`. GUI picks based on host `nvidia-smi`.

### 10.3 `build-images.sh`
**Mandatory env vars** (fail-loud via `${VAR:?...}`):
- `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `CLOUD_API_URL`
**Optional**: `ALLOWED_POLICIES` (default `act`), `REGISTRY` (default `nettername`), `BUILD_BASE` (default 0; set 1 to rebuild open_manipulator base ~40 min), `OPEN_MANIPULATOR_DIR`, `PHYSICAL_AI_TOOLS_DIR`.

`BUILD_ID = ${BUILD_TS}-${BUILD_SHA}` (UTC timestamp + 7-char git SHA, fallback 8-byte random hex).

Build args passed to `physical_ai_manager`:
```
REACT_APP_SUPABASE_URL, REACT_APP_SUPABASE_ANON_KEY, REACT_APP_CLOUD_API_URL,
REACT_APP_ALLOWED_POLICIES, REACT_APP_BUILD_ID
```
**Smoke test** post-build: greps `main.*.js` for literal `SUPABASE_URL` and `CLOUD_API_URL` strings; aborts if missing (white-screen prevention — same check is duplicated in `.github/workflows/ci.yml:manager-build-validate` job).

Push loop verifies success per image; aborts on failure (no half-updated student set).

Coco classes file is staged from `physical_ai_tools/physical_ai_server/physical_ai_server/workflow/coco_classes.py` to `physical_ai_manager/_coco_classes.py` so the `prebuild` Jest hook (`src/components/Workshop/blocks/__tests__/objectClasses.sync.test.js`) can run inside the Docker build context.

### 10.4 `bump-upstream-digests.sh`
Helper that runs `docker buildx imagetools inspect robotis/open-manipulator:latest` (and others) to print SHA256 digests + sed commands for upgrading pins in `BASE_IMAGE_PINNING.md`. Manual review required.

---

## 11. WSL rootfs & Windows GUI internals

### 11.1 wsl_rootfs/Dockerfile (`FROM ubuntu:22.04`)
- apt: `ca-certificates curl gnupg iproute2 iputils-ping jq kmod lsb-release systemd tzdata udev usbutils v4l-utils`
- Timezone: `ln -sf /usr/share/zoneinfo/Europe/Berlin /etc/localtime`, `echo "Europe/Berlin" > /etc/timezone`. **Critical** — both must be **files** (not dirs), otherwise compose bind-mounts fail with "trying to mount a directory onto a file".
- Docker CE apt repo (`signed-by=/etc/apt/keyrings/docker.gpg`)
- NVIDIA Container Toolkit apt repo (`signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg`)
- Pinned: `DOCKER_VERSION=5:27.5.1-1~ubuntu.22.04~jammy`, `CONTAINERD_VERSION=1.7.27-1`. **Reason**: Docker 29.x's `containerd-snapshotter` corrupts multi-layer pulls on WSL2 custom rootfs; 29+ removed the disable flag. `apt-mark hold docker-ce docker-ce-cli containerd.io` prevents auto-upgrade.
- `daemon.json`: `overlay2`, `nvidia` runtime, `containerd-snapshotter: false` (explicit), 10m × 3 file log rotation, buildkit on.
- `wsl.conf`: `[boot] command=/usr/local/bin/start-dockerd.sh`, `[user] default=root`, `[network] generateResolvConf=true hostname=edubotics`, `[interop] enabled=true appendWindowsPath=false`. **Systemd is explicitly NOT used** (unreliable on custom-imported rootfs).
- `start-dockerd.sh` re-exports PATH (WSL boot ctx is empty → biggest "dockerd doesn't start" cause), `nohup /usr/bin/dockerd >> /var/log/dockerd.log 2>&1 &`, plus a **5s-interval watchdog** that respawns dockerd if it dies (added 2026-04-17 incident response). Watchdog wrapped in `nohup sh -c '...'` to survive boot-shell SIGHUP.

### 11.2 wsl_rootfs/build_rootfs.sh
1. `docker build --pull -t edubotics-rootfs:latest .`
2. `docker create edubotics-rootfs:latest true` → `cid`
3. `docker export $cid | gzip -9 > installer/assets/edubotics-rootfs.tar.gz`
4. `sha256sum > .sha256` sidecar
5. trap removes `cid`

Output: ~350-450 MB compressed. Both file + sidecar are gitignored (≥100 MB exceeds GitHub limit).

### 11.3 GUI constants (`gui/app/constants.py`)
- `APP_VERSION = _read_version_file()` — reads repo-root `VERSION` (`2.2.2`); fallback `"2.2.2"`
- `UPDATE_API_URL = $EDUBOTICS_UPDATE_API_URL or "https://scintillating-empathy-production-9efd.up.railway.app"`
- `REGISTRY = $EDUBOTICS_REGISTRY or "nettername"`
- `IMAGE_TAG`: `$EDUBOTICS_IMAGE_TAG` → `docker/versions.env IMAGE_TAG=...` line → `"latest"`
- `IMAGE_OPEN_MANIPULATOR/SERVER/MANAGER`, `ALL_IMAGES`
- `PORT_WEB_UI=80`, `PORT_VIDEO_SERVER=8080`, `PORT_ROSBRIDGE=9090`
- `ROBOTIS_VID="2F5D"`, `BAUDRATE=1_000_000`
- `LEADER_SERVO_IDS=[1,2,3,4,5,6]`, `FOLLOWER_SERVO_IDS=[11,12,13,14,15,16]`
- `ROS_DOMAIN_ID=30` (legacy default; per-machine override in `_resolve_ros_domain_id`)
- `WSL_DISTRO_NAME = $EDUBOTICS_WSL_DISTRO or "EduBotics"`
- `INSTALL_DIR = _resolve_install_dir()` — walks up 6 levels from `sys.executable` then from `gui/app/` looking for `docker/docker-compose.yml`; fallback `r"C:\Program Files\EduBotics"`. Override `$EDUBOTICS_INSTALL_DIR`.
- `DOCKER_DIR`, `COMPOSE_FILE`, `COMPOSE_GPU_FILE`, `DOCKER_DIR_WSL` (via `_to_wsl_path`)
- `ENV_FILE = $EDUBOTICS_ENV_FILE or %LOCALAPPDATA%\EduBotics\.env`
- Timeouts: `DOCKER_STARTUP_TIMEOUT=120`, `DEVICE_WAIT_TIMEOUT=30`, `WEB_UI_POLL_TIMEOUT=120`, `WEB_UI_POLL_INTERVAL=2`

### 11.4 docker_manager.py highlights
- `_docker_cmd(*args, cwd_wsl=None)` builds `["wsl", "-d", "EduBotics", ("--cd", cwd_wsl)?, "--", "docker", *args]`.
- `wait_for_docker(timeout=120, callback)` polls `is_docker_running()`; at 15s+ stall force-invokes `start-dockerd.sh`.
- `_pull_one_image()` watchdog: 20s poll, 10 MB disk-growth threshold (`/var/lib/docker/overlay2`), `stall_timeout=600s` first / `120s` updates, exp backoff `min(4*2^(attempt-1), 30)` with `max_retries=4`. On stall ≥ attempt 2 → `_reset_dockerd()` (`pkill` docker pull / dockerd / containerd, clean `/var/run/docker.sock` + `.pid`, restart, 4s wait, 15-attempt readiness poll).
- `has_gpu()` runs **host** `nvidia-smi` (NOT inside WSL — the test `test_docker_manager_wsl.py` asserts this).
- `start_cloud_only()` runs `docker compose ... up -d --force-recreate --no-deps physical_ai_manager`.

### 11.5 Tests (`robotis_ai_setup/tests/`)
All Windows-only (skip on non-Windows CI). 5 unittest files:
- `test_config_generator.py` — env file generation, path quoting, ROS_DOMAIN_ID, cameras optional, `is_complete` requires both arms.
- `test_device_manager.py` — `USBDevice` creation, `HardwareConfig.is_complete`, `list_robotis_devices()` filters VID 2F5D.
- `test_docker_manager.py` — `is_docker_running` returncode, `has_gpu` returncode, `images_exist`.
- `test_docker_manager_wsl.py` — every docker call wrapped as `wsl -d EduBotics -- docker ...`, `is_distro_registered` handles UTF-16LE NULs, `nvidia-smi` is **NOT** wrapped.
- `test_wsl_path_convert.py` — `_to_wsl_path` covers `C:\foo\bar` → `/mnt/c/foo/bar`, lowercase drives, forward slashes, trailing backslash, empty string, non-drive paths unchanged.

---

## 12. Frontend reference (`physical_ai_tools/physical_ai_manager/`)

### 12.1 Build artifacts
- `package.json` v0.8.2; key deps: React 19.1.0, Redux Toolkit 2.8.2, `@supabase/supabase-js` 2.49.8, Blockly 12.5.0, react-blockly 9.0.0, ROSLIB 1.4.1, Recharts 2.13.0, react-hot-toast 2.5.2, Tailwind 3.4.17.
- `prebuild` script: runs Jest tests under `src/components/Workshop/blocks/__tests__/` (Workshop blocks consistency).
- `start:debug`: `REACT_APP_DEBUG=true react-scripts start` (skips robot-type gate on home page).

### 12.2 React env vars (build-time, baked into bundle)
| Var | Required | Default | Notes |
|---|---|---|---|
| `REACT_APP_SUPABASE_URL` | yes | — | If missing, `lib/supabaseClient.js` builds a Proxy that throws on first method call: German `Supabase ist in dieser Build-Version nicht konfiguriert. Bitte das physical-ai-manager-Image mit gültigen REACT_APP_SUPABASE_URL und REACT_APP_SUPABASE_ANON_KEY neu bauen.` |
| `REACT_APP_SUPABASE_ANON_KEY` | yes | — | same |
| `REACT_APP_CLOUD_API_URL` | yes | — | If missing, `services/cloudConfig.js:assertCloudApiConfigured()` throws German `Die Cloud-API-Adresse ist in dieser Build-Version nicht konfiguriert. Bitte das physical-ai-manager-Image mit gültigem REACT_APP_CLOUD_API_URL neu bauen.` |
| `REACT_APP_MODE` | no | `student` | `web` for Railway dashboard build (`Dockerfile.web`) |
| `REACT_APP_ALLOWED_POLICIES` | no | `act` (student) / full list (web) | CSV; filters PolicySelector dropdown |
| `REACT_APP_BUILD_ID` | no | `dev` | Used by `useVersionCheck` to compare with `/version.json` |
| `REACT_APP_DEBUG` | no | `false` | Enables direct page nav |
| `REACT_APP_BASE_WORKSPACE_PATH` | no | `/root/ros2_ws/src/physical_ai_tools` | |
| `REACT_APP_LEROBOT_OUTPUTS_PATH` | no | derived from base | |

`<BuildConfigBanner />` (component) renders a bright red fixed banner at top with German text if any of those 3 critical vars is missing — last-line-of-defense for white-screen builds.

### 12.3 Auth flow
- `LoginForm`: validates username `/^[a-zA-Z0-9._-]+$/` (3-32 chars), password ≥ 6.
- `usernameToEmail(username) → "{username}@edubotics.local"` (in `constants/appMode.js`).
- `supabase.auth.signInWithPassword({email, password})`.
- StudentApp: `supabase.auth.getSession()` on mount → `getMe(access_token)` (calls `/me`) → role check (reject non-student with German `Dieses Konto ist für die Web-App. Bitte nutze die Lehrer-URL.` + `signOut()`).
- 401/403 → `Sitzung abgelaufen — bitte erneut anmelden.` + `signOut()`.
- Network error → `Server nicht erreichbar — bitte Verbindung prüfen.` (no signOut).

### 12.4 Cloud-only mode
URL query `?cloud=1` → `utils/cloudMode.js:isCloudOnlyMode()` returns true → StudentApp filters out `hardwareOnly` tabs (RECORD, INFERENCE, EDIT_DATASET, WORKSHOP) and skips `<StartupGate />` (no rosbridge).

### 12.5 ROS connection (`utils/rosConnectionManager.js`)
Singleton. `roslib` 1.4.1, URL `ws://${window.location.hostname}:9090`. Connection timeout 10s. Reconnect: `min(1000 * 2^attempts, 30000)` ms backoff, max 30 attempts. `intentionalDisconnect` flag prevents auto-reconnect on user-requested close. `resetReconnectCounter()` exposed for the StartupGate retry button.

### 12.6 Version self-reload (`hooks/useVersionCheck.js`)
- Polls `/version.json?_={now}` with `cache: 'no-store'` every 30s, on focus, on visibilitychange.
- If `liveBuildId !== process.env.REACT_APP_BUILD_ID` AND neither is `dev` AND last reload ≥ 60s ago (sessionStorage `__edubotics_version_reload_at`), `window.location.reload()`.

### 12.7 Realtime hooks
- `useSupabaseTrainings(userId)` — Supabase channel `trainings:{userId}` filter `user_id=eq.{userId}`; falls back to 30s poll if `!isRealtime`. Strips `worker_token`, `cloud_job_id` from the response shape.
- `useSupabaseWorkflows(userId)` — same pattern, channel `workflows:{userId}` filter `owner_user_id=eq.{userId}`. Token race guard (audit §3.11): captures the access token at fetch start; drops result if token rotated mid-flight.
- `useRefetchOnFocus(refetch, minIntervalMs=2000)` — debounced focus/visibility refetch.

### 12.8 ROS service caller (`hooks/useRosServiceCaller.js`)
10s default timeout. ~20 services bound (full list — service file in `physical_ai_interfaces`):
`/task/command` (SendCommand), `/training/command` (SendTrainingCommand), `/image/get_available_list`, `/get_robot_types`, `/set_robot_type`, `/register_hf_user`, `/get_registered_hf_user` (3s timeout), `/training/get_user_list`, `/training/get_dataset_list`, `/training/get_available_policy`, `/training/get_model_weight_list`, `/browse_file`, `/dataset/edit`, `/dataset/get_info`, `/huggingface/control`, `/training/get_training_info`, `/calibration/start` `/calibration/capture` `/calibration/solve` `/calibration/cancel` `/calibration/capture_color`, `/workflow/start`, `/workflow/stop`, `/workshop/mark_destination`.

### 12.9 Sidebar tabs (StudentApp)
Labels (German): **Start, Aufnahme, Training, Inferenz, Daten, Roboter Studio**. Internal page enum (`constants/pageType.js`): `HOME, RECORD, INFERENCE, TRAINING, EDIT_DATASET, WORKSHOP`.

`hardwareOnly` tabs filtered in cloud-only mode: RECORD, INFERENCE, EDIT_DATASET, WORKSHOP.

### 12.10 nginx configs
- Student (`nginx.conf`): cache-bust `/index.html` and `/version.json` (`Cache-Control: no-store`); `/static/` immutable 1y; SPA fallback `try_files $uri /index.html`.
- Web (`nginx.web.conf.template`): same caching + 5 strict security headers on **every** location (HSTS 2y, X-Frame-Options DENY, X-Content-Type-Options nosniff, Referrer-Policy strict-origin-when-cross-origin, Permissions-Policy denying camera/mic/geo/payment). Listens on `${PORT}` (Railway).

### 12.11 Constants files
- `pageType.js`, `taskPhases.js` (READY=0, WARMING_UP=1, RESETTING=2, RECORDING=3, SAVING=4, STOPPED=5, INFERENCING=6), `trainingCommand.js` (NONE=0, START=1, FINISH=2), `taskCommand.js` (NONE=0, START_RECORD=1, START_INFERENCE=2, STOP=3, NEXT=4, RERECORD=5, FINISH=6, SKIP_TASK=7), `commands.js` (EditDatasetCommand: MERGE=0, DELETE=1), `HFStatus.js` (Idle/Uploading/Downloading/Deleting/Fetching/Processing/Success/Failed), `paths.js` (workspace + dataset + policy paths), `appMode.js`.

---

## 13. Workflows for Claude

### 13.1 Add a feature that crosses layers (e.g., new training param)
1. Read this file's relevant layer section + the layer's source.
2. **Cloud API** — add to Pydantic `TrainingParams` (with bounds) → propagate through `start_training_safe` JSONB if the worker should see it.
3. **Modal worker** — read it from `training_params.get("name", default)` in `_build_training_command`. Add a CLI arg or LeRobot config setting.
4. **React** — add field to `trainingSlice.js:trainingInfo` (persisted to localStorage as `edubotics_trainingInfo`); add UI in `TrainingOptionInput.js`; pass through `cloudTrainingApi.startCloudTraining()`.
5. **Verification** — local `uvicorn app.main:app --reload`, hit `/trainings/start` with the new param via curl; smoke-train an `act` policy on a tiny dataset on Modal; confirm Supabase row stores the param.
6. Update this CLAUDE.md if the feature surfaces a new env var or a load-bearing constant.

### 13.2 Bump LeRobot version
1. Read [§1.5](#15-dont-introduce-drift-between-the-lerobot-pinning-sites).
2. Pick the new full SHA from huggingface/lerobot. Read the LeRobot release notes (any `codebase_version` bump? new policy required fields?).
3. **Update all 5 sites in one PR**:
   - `physical_ai_tools/lerobot/` — replace with byte-identical snapshot from the new SHA.
   - `modal_training/modal_app.py:19` — bump `LEROBOT_COMMIT`.
   - Verify the base image `robotis/physical-ai-server:amd64-X.Y.Z` was rebuilt against this SHA (look at ROBOTIS-GIT release notes); if not, leave the base image pin and accept the drift risk.
   - `meta/info.json` `codebase_version` — if upstream bumped, write a migration script for existing student datasets.
   - `training_handler.py:EXPECTED_CODEBASE_VERSION` — match.
4. **Modal verify**: `modal deploy modal_app.py` then `modal run -m modal_app::smoke_test`. Check that `torch.__version__` is still cu121, no `torchcodec`.
5. **Smoke training** on a tiny ACT dataset; confirm progress writes, HF upload, Supabase status flips to `succeeded`.
6. **Local recording** smoke; confirm `meta/info.json` written with the new version.

### 13.3 Add a Supabase migration
1. Read `supabase/migration.sql` and the latest existing migration (`010_progress_terminal_guard.sql`) to copy style.
2. Write `011_<name>.sql` AND `rollback/011_<name>_rollback.sql`. Both must be wrapped in `BEGIN;` ... `COMMIT;`. Use `IF NOT EXISTS` / `IF EXISTS` for idempotency.
3. **Decide the access path**:
   - If service-role-only writes: don't add anon/authenticated policies.
   - If reads/writes from the React app via the Supabase client: add explicit RLS policies (`USING` and `WITH CHECK`).
   - If a worker-style RPC: `SECURITY DEFINER`, explicit `SET search_path = public`, REVOKE from PUBLIC, GRANT to specific role.
4. Test in a Supabase **branch DB** with both anon-key (RLS active) and service-role-key (RLS bypassed). Apply forward, verify rollback.
5. Add Realtime publication (`ALTER PUBLICATION supabase_realtime ADD TABLE ...`) only if the React app subscribes via Supabase client (not via Railway).
6. If you add a new error code (P00xx), add it to [§7.5](#75-custom-postgres-error-codes) and map to HTTP status in the relevant route.

### 13.4 Modify an overlay
1. Read the upstream file in `physical_ai_tools/...` to confirm the function/class signature you're modifying still exists.
2. Edit `robotis_ai_setup/docker/physical_ai_server/overlays/<file>` (or `open_manipulator/overlays/<file>`).
3. **Confirm `apply_overlay` will still find a target** by simulating the find: `find /root/ros2_ws -name "<file>" -path "<filter>"` against the upstream file paths (or just look at the existing path filter literal in the Dockerfile).
4. Build: `cd robotis_ai_setup/docker && SUPABASE_URL=... SUPABASE_ANON_KEY=... CLOUD_API_URL=... ./build-images.sh`. Watch for `Overlaid: <path> (<before> -> <after>)`.
5. **Full pipeline smoke**: bring up containers with hardware, record one episode, train it, run inference. Type-check / unit tests don't catch UX regressions.
6. If you added German error strings, double-check grammar (use `ä ö ü ß` literally).

### 13.5 Debug
| Symptom | Where to look first |
|---|---|
| `dockerd doesn't start in WSL2` | `start-dockerd.sh` (PATH re-export); `wsl -d EduBotics -- tail -n 50 /var/log/dockerd.log` |
| `Multi-layer pull corrupts` on large image | confirm `daemon.json` has `containerd-snapshotter: false`; confirm Docker pin is 27.5.1 |
| `s6 service silently disabled` (server starts but no ROS) | `.s6-keep` mount missing in compose |
| `s6 rejects longrun\r` | CRLF in service file; Dockerfile sed strip ran? |
| `Modal worker uses wrong torch (cu130)` | re-deploy with `index_url=...whl/cu121` and `--force-reinstall` |
| `Inference silently swaps cameras` | Overlay enforces exact-name match — error in German `[FEHLER] Kamera-Namen passen nicht...` |
| `Empty JointTrajectory crashes recording` | Overlay raises German `[FEHLER] JointTrajectory hat keine Punkte...` |
| `dockerd hangs after install` | `start-dockerd.sh` watchdog should respawn; check `/var/log/dockerd.log` |
| `Stuck "running" training` | `STALLED_WORKER_MINUTES=25` sweep should fail it; check Modal logs (`modal app logs edubotics-training`); check Supabase `last_progress_at` |
| `Recording crashes on RAM warning` | `EDUBOTICS_RAM_LIMIT_GB` (default 0.8 GB) too aggressive on tight machine? |
| `/trainings/start 400` | check `routes/training.py` Pydantic validators + `ALLOWED_POLICIES` env |
| `dockerd dies, never restarts` | confirm watchdog loop in `start-dockerd.sh` is running (`pgrep -f respawning`) |
| `GUI buttons stuck after hardware fail` | `gui_app.py:_do_start()` finally block resets `self.running` |
| `White-screen React` | `BuildConfigBanner` should fire; check `/usr/share/nginx/html/static/js/main.*.js` for literal `SUPABASE_URL` (CI's `manager-build-validate` job catches this) |
| `Workflow JSON too big to save` | 256 KB / depth 64 cap in `validators/workflow.py` |
| `Calibration wizard "Kein Kamerabild verfügbar"` | `communicator.py` overlay missing — `get_latest_bgr_frame()` not present |

### 13.6 When to ask the user
You can act autonomously on:
- Reading any file
- Editing code with low blast radius (single layer, no infra effects)
- Running local tests, lints, type checks
- Building Docker images locally
- Spawning sub-agents for research

**Ask before**:
- `git push` (and never force-push to main)
- `wsl --unregister` (destroys VHDX with named volumes inside)
- Force-push, `git reset --hard`, `git clean -fd`
- `docker compose down -v` (deletes named volumes — datasets gone)
- `docker volume rm` of `huggingface_cache` or `edubotics_calib`
- Modal `cancel` on a running training (charges credit)
- Supabase `auth.admin.delete_user` calls
- Rotating production secrets
- Editing CI/CD config
- Changing safety-critical paths (joint clamp, NaN guard, stale-camera halt, torque-disable on SIGTERM, sync-verification tolerance, ownership assertions)
- Removing/renaming files that other layers reference (overlay targets, ROS topics, env var names, RPC signatures)
- Touching `start_training_safe` / `get_remaining_credits` / `adjust_workgroup_credits` semantics — the workgroup credit pool is now load-bearing for grouped students (migration 011); a regression is silent over-spend or refused trainings

---

## 14. Versioning + drift map

Five sources of truth currently drift:
- `Testre/VERSION` → **`2.2.2`** (read by `gui/app/constants.py:APP_VERSION`)
- `installer/robotis_ai_setup.iss AppVersion` → **`2.2.3`**
- `physical_ai_tools/physical_ai_manager/package.json version` → **`0.8.2`**
- `docker/versions.env IMAGE_TAG` → file does NOT exist in the repo (gitignored or never created); GUI/installer fall back to `:latest`
- HTTP `/version.json buildId` (UTC timestamp + git SHA, computed at build time)

**Rule**: When bumping product version, hit all four (VERSION, .iss, package.json, recreate `versions.env` from a template) in the same change. The drift between 2.2.2 (VERSION) and 2.2.3 (.iss) is a known issue.

---

## 15. CI workflow (`.github/workflows/ci.yml`) — what fails the build

6 jobs run on every push/PR to `main`:
1. **python-tests** — `compileall` of `gui`, `scripts`, `cloud_training_api`, `modal_training`, overlays, patches; `unittest discover -s tests`.
2. **shell-lint** — shellcheck `-S error` on `build-images.sh`, `entrypoint_omx.sh`, `build_rootfs.sh`, `start-dockerd.sh`.
3. **compose-validate** — `docker compose config` on both base + GPU compose with fake `.env`.
4. **overlay-guard** — runs `fix_server_inference.py` on a fake `server_inference.py` that lacks the patch target; asserts non-zero exit (catches a regress where the patch silently fails).
5. **manager-build-validate** — builds `physical_ai_manager` with placeholder secrets (`CI_VALIDATE.supabase.co`, `CI_VALIDATE_ANON_KEY`, `CI_VALIDATE.api.example`); asserts each placeholder string appears in the built `main.*.js` bundle. Catches the white-screen regression.
6. **nginx-validate** — `envsubst $PORT` on `nginx.web.conf.template` then `nginx -t` on both web + student configs.

---

## 16. Glossary

- **OMX** — OpenMANIPULATOR-X (6-DoF educational arm by ROBOTIS).
- **OMX-F** — Follower arm. Servo IDs **11-16**. Driven (position on joints 1-5; gripper joint 6 = `gripper_joint_1` in current control with **350 mA** limit, Op Mode 5).
- **OMX-L** — Leader arm. Servo IDs **1-6**. Joints 1-5 in **velocity** mode (state-only, gravity comp + friction tuning). Joint 6 in current control with **300 mA** limit (reverse direction). Drives the follower via `joint_trajectory_command_broadcaster` publishing to `/leader/joint_trajectory`.
- **OpenRB-150** — USB-to-RS-485 bridge board, ROBOTIS USB VID `2F5D`, PIDs `0103` (default) / `2202` (alt firmware).
- **Dynamixel** — ROBOTIS servo line. Protocol 2.0 over a 1 Mbps RS-485 bus. SDK pin `dynamixel-sdk==4.0.3`.
- **`/dev/serial/by-id/...`** — udev-stable serial path. The GUI passes these as `FOLLOWER_PORT` / `LEADER_PORT` so device order is stable across reboots/replug.
- **ROS2 Jazzy** — ROS distribution (May 2024, Ubuntu 22.04).
- **`ROS_DOMAIN_ID`** — DDS isolation key (0-232). Default 30; per-machine UUID hash mod 233 in `_resolve_ros_domain_id`.
- **rosbridge** — WebSocket bridge from browser-side roslib to ROS2 (`ws://hostname:9090`).
- **Magic remap** — `omx_f_follower_ai.launch.py:144` `/arm_controller/joint_trajectory → /leader/joint_trajectory`. Anyone publishing to `/leader/joint_trajectory` drives the follower.
- **`.s6-keep`** — empty 1-byte file mounted RO at `/etc/s6-overlay/s6-rc.d/user/contents.d/physical_ai_server`. **Required** to enable the s6 longrun service in the base image. Without it the container starts but the ROS node never runs.
- **EduBotics distro** — bundled WSL2 Ubuntu 22.04, name `EduBotics`. Imported from `edubotics-rootfs.tar.gz`. **Replaces Docker Desktop entirely.**
- **`usbipd`** — Tool that forwards Windows USB into WSL2 distros. Pinned v5.3.0 with SHA256 verify. ROBOTIS VID policies preconfigured.
- **`apply_overlay()`** — shell function in physical_ai_server/Dockerfile + open_manipulator/Dockerfile that finds target file in `/root/ros2_ws`, sha256-verifies the source, copies, sha256-verifies the result. Fails loudly if target not found.
- **Synthetic email** — `{username}@edubotics.local` (domain doesn't exist; never receives email).
- **Service-role key** — `SUPABASE_SERVICE_ROLE_KEY`. Bypasses RLS. Used by Railway FastAPI. **Never** ship to React.
- **Anon key** — `SUPABASE_ANON_KEY`. RLS-bound. Baked into React bundle. Modal worker also uses it for scoped RPC writes.
- **Worker token** — per-row UUID in `trainings.worker_token`. Modal worker can only update its own row via `update_training_progress(p_token, ...)`. Nulled on terminal status.
- **3 roles** — `admin`, `teacher`, `student` (enum `public.user_role`, migration 002).
- **Credit hierarchy** — admin grants pool to teacher (`training_credits` field) → teacher allocates to students via `adjust_student_credits` RPC → student spends one credit per non-failed/canceled training.
- **Stalled-worker sweep** — `_sync_modal_status` cancels Modal job + marks failed if `last_progress_at` older than `STALLED_WORKER_MINUTES` (default 25).
- **Dispatch-lost detection** — if Modal can't find the FunctionCall after `DISPATCH_LOST_MINUTES` (default 10), mark failed with German `Dispatch an Cloud-Worker fehlgeschlagen...`.
- **Dedupe window** — `DEDUPE_WINDOW=60s`. `_find_recent_duplicate` keys on `(user_id, dataset_name, model_type, training_params)`. Excludes failed/canceled rows so retry works immediately.
- **`POLICY_MAX_TIMEOUT_HOURS`** — per-policy timeout caps applied after request validation but before Modal dispatch (ACT 1.5h, VQBET/TDMPC 2h, Diffusion/Pi0Fast 4h, Pi0/SmolVLA 6h).
- **Camera exact-match** — overlay rejects mismatched camera names. German `[FEHLER] Kamera-Namen passen nicht: Modell erwartet {expected_names}, verbunden {connected_names}...`
- **Stale-camera halt** — overlay watchdog hashes 4 sparse 256-byte slices per image; warn @ 2s, halt @ 5s. Returns None → tick skipped.
- **Safety envelope** — overlay-added: NaN/Inf reject + per-joint clamp + per-tick velocity cap. Configured in `omx_f_config.yaml`.
- **`config.json`** — output of LeRobot training, lives at `pretrained_model/config.json`. Inference reads `input_features` to determine expected camera keys (`observation.images.{name}`) and shapes.
- **`policy_path`** — local FS path to a checkpoint (NOT an HF URL). React passes via TaskInfo. Always under `~/.cache/huggingface/hub/models--*/snapshots/*/pretrained_model/`.
- **DSGVO** — Datenschutz-Grundverordnung. German for GDPR.
- **`P00xx` codes** — see [§7.5](#75-custom-postgres-error-codes).
- **ChArUco constants** — 7×5 squares, 30 mm square / 22 mm marker, `DICT_5X5_250`. 12 frames for intrinsic, 14 for hand-eye, PARK + TSAI dual-solve.
- **HOME pose** — `[0.0, -π/4, π/4, 0.0, 0.0]` rad + `gripper_joint_1 = 0.8` rad (open).
- **Workflow recovery** — auto-home on stop/error: 1.0s hold + 0.5s gripper open + 3.0s home, 15.0s absolute deadline.

---

## 17. When in doubt

The single source of truth is always **the code**. This file describes what's true at the time it was written. Verify against `git log` and the current file when stakes are high. If you find this file disagrees with the code, fix this file in the same change.

**You are an autonomous coding partner.** Do the work, fix the failures at the root cause (never `--no-verify`, never `@pytest.skip`, never bypass an `apply_overlay` assertion that's telling you upstream renamed something). When you change anything that this file describes, update this file. The whole point of this file is that it stays in sync.

When the user says something destructive or you're about to take a destructive action, **stop and ask** — no one's training schedule is so urgent that an unwanted `wsl --unregister` is acceptable.

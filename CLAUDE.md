# EduBotics — Single-File Brief for Claude

> **Read this entire file at the start of every session.** It replaces the former `context/` folder and is the single source of truth for what the project is, how it fits together, and how to make changes safely. Source code is the ultimate authority — when this file disagrees with the code, the code wins (and you should update this file in the same change).
>
> Last verified by reading every load-bearing file directly: **2026-05-15** (ACT-quality + camera-sync bundle F62-F66 + auto-pull GUI 2.2.4 bundle F67-F69: **F67** offline short-circuit in `docker_manager.is_dockerhub_reachable()` — 5 s TCP probe to `registry-1.docker.io:443` skips the per-image retry storm when classrooms are offline · **F68** manifest-digest pre-check via `_get_remote_manifest_digest()` + `_get_local_repo_digest()` — picks the linux/amd64 entry from `docker manifest inspect`, compares to local RepoDigest, skips the real pull when they match (steady-state path drops from ~30 s of full pulls to ~3 s of HEAD requests) · **F69** last-pull persistence to `%LOCALAPPDATA%/EduBotics/.last_image_pull.json` with a "Letzter Update vor X Tagen" log line on next start + red banner past `IMAGE_FRESHNESS_WARN_DAYS=14` so teachers can spot classroom PCs that have been offline too long · VERSION + .iss + GUI fallback constant all bumped 2.2.3 → 2.2.4 so the existing `/version`-poll update gate (CLAUDE.md §6.2 step 1) prompts students with stale `.exe` installs to re-install on next launch, which is how the new auto-pull code itself reaches them · 20 new cross-platform unit tests in `tests/test_docker_auto_pull.py` covering offline detection, digest parsing, persistence roundtrip, and orchestration short-circuits. Earlier: F62-F66 deploy round (second pass also fixed F62's missing overlay-chain entry — `lerobot_dataset_wrapper.py` is now overlaid via `apply_overlay lerobot_dataset_wrapper.py "*/data_processing/*"` so the file actually reaches student installs, AND the deploy used `docker buildx build --push` to bypass Docker Desktop's dual-image-store gotcha that silently uploaded a stale `physical-ai-server:latest` on the first try: F62 mp4 preset `ultrafast→fast`/CRF 28→23 in `lerobot_dataset_wrapper.py:331` · F63 `--dataset.image_transforms.enable=true` always-on at train time in `training_handler._build_training_command` · F64 ACT-only default `--policy.n_action_steps=15` in same builder, overridable via `training_params['n_action_steps']` · F65 `ApproximateTimeSynchronizer`-style paired-camera capture in `overlays/communicator.py` via `_pick_synced_camera_msgs` with `_CAMERA_SYNC_SLOP_NS=15_000_000` slop and 8-entry per-camera ring · **F66 follow-up hardening landed same session from 4-agent verifier review**: (a) the F65 deque snapshot is now wrapped in a 3-attempt retry that catches `RuntimeError: deque mutated during iteration` (real risk under `MultiThreadedExecutor(num_threads=3)` in `physical_ai_server.py:2961`) and falls back to `camera_topic_msgs`; (b) `clear_latest_data()` now `.clear()`s the F65 sync rings so a re-recorded episode's first tick can't pair a fresh msg with a stale msg from the prior episode; (c) the F64 override path is now gated on `model_type == "act"` (closes the cross-policy leak where a `diffusion`/`pi0`/`vqbet` job carrying `n_action_steps` in `training_params` would emit `--policy.n_action_steps=N` for a policy whose config schema doesn't define that field); (d) the override is validated as `isinstance(int) and > 0` — `None`, `0`, negative, and non-int values fall back to the F64 default rather than emit a broken CLI arg or crash `ACTConfig.__post_init__`. The test suite (`tests/test_training_handler_cli.py`) was extended to 7 tests covering all 7 `ALLOWED_POLICIES`, the cross-policy leak, the invalid-override fallback, and the "exactly-once" emission count. Earlier: 2026-05-14 (Tier-1–9 audit-driven fix bundle: hardware safety [E1-E4, S1-S2, H1] · cloud API hardening [A1-A3, R3, N1 via migration 018, H4] · sensor + cloud-vision wiring [O1, O3, U6, N3] + M6 · calibration + tutorial UX [U1 via new `useSupabaseTutorialProgress` hook, U2, U3 via `requestRecalibration` action, U4] · real Modal vision smoke test [O2, N4] + N2 breakpoint-sync toast · German transliteration sweep + CI `german-strings-lint` job · CI hardening [H27 overlay-guard success case, H28 routes/training+vision SDK probe, H29 .s6-keep assertion] · installer + build hardening [H17, H21, H22, H23, H25, M14]). Earlier baseline: 2026-05-11 camera-pipeline F1-F61 bundle from commit `1b68372`.

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
- **Hardware safety lives in xacro + entrypoint, not in software overlays.** The software-side inference safety envelopes (NaN/Inf guard, joint clamp, per-tick velocity cap, stale-camera halt, image shape / camera-name validation) were removed in the 2026-05 safety stripdown. Inference now runs upstream `predict()` raw — preprocess → `policy.select_action` → publish — plus the F10 extra-camera filter as a correctness fix.
- The protections that remain are hardware-enforced or warning-only:
  - **Xacro Dynamixel limits** (`omx_f.ros2_control.xacro`, `omx_l.ros2_control.xacro`): joint Min/Max Position Limits + gripper current limits (follower 350 mA, leader 300 mA, Op Mode 5).
  - **ros2_control YAML** (`omx_f_hardware_controller_manager.yaml`, `omx_l_leader_ai_hardware_controller_manager.yaml`): 100 Hz update rate, JointTrajectoryController constraints (0.15 rad trajectory tolerance, 0.05 rad goal tolerance, 1.0 s goal_time).
  - **SIGTERM/SIGINT torque-disable** in `docker/open_manipulator/entrypoint_omx.sh` (`disable_torque()` calls `/dynamixel_hardware_interface/set_dxl_torque` SetBool service on both arms; 2 s timeout per arm).
  - **Phase 4 post-sync verification** in `entrypoint_omx.sh` (0.08 rad tolerance per joint after the 3-second quintic ramp; hard-exit 2 on mismatch refuses to continue).
- Recording-side guards are now warning-only (`[WARNUNG]` lines, `TaskStatus.error` banners) — episodes always complete:
  - Stale-camera detector at recording (4 sparse 256-byte hashes, > 5 s identical → warn only)
  - Timestamp-gap detector (post-save, > 2× expected_dt → warn)
  - Video-file verifier (post-save mp4 existence + non-zero size → warn)
  - usb_cam camera-fps warning (observed Hz < 0.8 × target → warn)
- Never bypass `_assert_classroom_owned()` / `_assert_student_owned()` / `_assert_entry_owned()` / `_assert_workflow_owned()` ownership checks in the cloud API.
- If you genuinely need to reintroduce a software safety guard that modifies the pipeline (clamps, halts, truncates), **stop and ask the user** — the stripdown removed those deliberately to fix recording↔inference asymmetry.

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
├── CAMERA_PIPELINE_FIXES.md               ← 2026-05 deep-audit, fixes F1-F61 (read this BEFORE touching camera, perception, safety envelope, or vision_app code)
├── .gitattributes                         ← LF-forced for *.sh, Dockerfile, daemon.json, .s6-keep, docker-compose*.yml
├── .gitignore                             ← gitignored: *.env, gui/dist/, installer/output/, *.tar.gz, .claude/
├── .mcp.json                              ← optional MCP server config (gitignored / unversioned in practice)
├── .github/workflows/ci.yml               ← 10 jobs: python-tests, shell-lint, compose-validate, overlay-guard, modal-import-validate, teacher-web-build-validate, manager-build-validate, tutorials-validate, interfaces-validate, nginx-validate
├── docs/                                  ← Deployment runbooks + deferred-work catalogues (see §18)
│   ├── ROBOTER_STUDIO_DEFERRED.md         ← 31-item deferred-work catalogue (Phase-2/3 leftovers, ordered by priority)
│   └── deploy/{APPLY_MIGRATIONS.sql, ROLLBACK_MIGRATIONS.sql, NEXT_STEPS.md, DEPLOYMENT_RUNBOOK.md}
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
│   │   ├── public/tutorials/*.json        ← Roboter Studio skillmap (CI's tutorials-validate enforces schema + allowed_blocks)
│   │   ├── scripts/railway-deploy.sh      ← stage _coco_classes.py + `railway up --path-as-root .`
│   │   ├── Dockerfile (student)  Dockerfile.web (Railway)  nginx.conf  nginx.web.conf.template
│   │   ├── package.json (v0.9.0)  railway.json  vercel.json (kill-switch)
│   ├── physical_ai_interfaces/            ← custom msg/srv (TaskInfo/Status, TrainingInfo/Status,
│   │                                        SendCommand, GetSavedPolicyList, Detection, WorkflowStatus,
│   │                                        StartCalibration, CalibrationCaptureColor, WorkflowSetBreakpoints,
│   │                                        SensorSnapshot, ...)
│   ├── physical_ai_bt/                    ← Behavior trees (XML), ffw_sg2_rev1.xml
│   ├── lerobot/                           ← LeRobot v0.2.0 snapshot @ 989f3d05 (static, byte-identical, NOT modified)
│   └── rosbag_recorder/                   ← C++ bag recorder service (PREPARE/START/STOP/STOP_AND_DELETE/FINISH)
│
├── robotis_ai_setup/                      ← OUR custom code (everything we wrote)
│   ├── CHANGES_SESSION_2026-04-06.md      ← (historical) ROS startup + healthcheck wiring — incorporated; keep for "why"
│   ├── CHANGES_SESSION_2026-04-17.md      ← (historical) Docker Desktop → bundled WSL2 distro — incorporated; keep for "why"
│   ├── cloud_training_api/                ← FastAPI on Railway (training jobs + teacher/admin/me/workflows API + vision proxy)
│   │   ├── Dockerfile  requirements.txt  .env.example
│   │   └── app/{main.py, auth.py, services/, routes/, validators/, tests/}
│   ├── modal_training/                    ← Modal apps + handler for cloud GPU training AND cloud vision
│   │   ├── modal_app.py                   ← Image build, function `train`, secrets, GPU=L4, timeout=7h
│   │   ├── training_handler.py            ← run_training() flow with German preflight + RPC + HF upload
│   │   └── vision_app.py                  ← OWLv2 cloud-burst, T4, snapshot-aware lifecycle (§8.3)
│   ├── docker/                            ← Compose, build-images.sh, overlays, patches
│   │   ├── docker-compose.yml  docker-compose.gpu.yml  .env.template  build-images.sh  bump-upstream-digests.sh  BASE_IMAGE_PINNING.md
│   │   ├── physical_ai_server/{Dockerfile, overlays/{workflow/{handlers/}}, patches/}
│   │   └── open_manipulator/{Dockerfile, entrypoint_omx.sh, identify_arm.py, overlays/}
│   ├── supabase/                          ← migration.sql + 002-013, 015-017 numbered files + rollback/  (NO 014_*.sql — see §9.13)
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
│   └── tests/                             ← 5 unittest files (Windows-only, all mocked) + cloud_training_api/app/tests/ (server-side, deps stubbed)
│
└── tools/                                 ← Classroom helpers
    ├── classroom_kit_README.md
    ├── generate_apriltags.py              ← Generates printable AprilTag PDFs
    ├── generate_charuco.py                ← Generates printable ChArUco boards (7x5, 30/22 mm, DICT_5X5_250)
    ├── generate_gripper_adapter.py        ← Parametric gripper-to-board STL
    ├── gripper_charuco_adapter.stl
    ├── dfine_finetune.md                  ← Notes on fine-tuning the on-device detector (deferred — not yet wired)
    └── perception_eval.md                 ← Hand-rolled eval harness for the perception block (deferred)
```

There is **no** `_upstream/`, **no** `.gitmodules`, **no** `modal_mcp/` in this repo (older context docs referenced these — they were never present here). The two `CHANGES_SESSION_*.md` files in `robotis_ai_setup/` and `CAMERA_PIPELINE_FIXES.md` at the repo root are **historical context** — the fixes they describe are in current images, but the `// Audit F##` markers in source point back to them for *why*. The `docs/` folder is **deployment-time material** (apply-in-order SQL, runbooks, deferred-work catalogue); the rest of CLAUDE.md is the runtime contract.

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
| Supabase | Postgres + Auth + Realtime | 10 user-facing tables (`users`, `trainings`, `classrooms`, `progress_entries`, `workflows`, `workgroups`, `workgroup_memberships`, `datasets`, `workflow_versions`, `tutorial_progress`) + ~10 RPCs + Realtime publications for `trainings`, `workflows`, `workgroups`, `datasets`, `tutorial_progress`, `workflow_versions` | Free tier |
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
| `inference_manager.py` | `*/inference/*` | F10 extra-camera silent filter + warning, CUDA-not-available German error in `load_policy()`. No clamps, no halts. |
| `data_manager.py` | `*/data_processing/*` | Post-save warnings only: timestamp-gap detection, mp4 file verification, stale-camera warn (no halt). Plus `set_action_duration_from_fps()` (non-30 Hz correctness) and canonical LeRobot v2.1 video-path derivation (Audit F23). |
| `data_converter.py` | `*/data_processing/*` | `desired_encoding='bgr8'` defensive encoding, extra-joints throttled warning, fps-aware action timing |
| `lerobot_dataset_wrapper.py` | `*/data_processing/*` | **F62**: bumps the per-episode `FFmpegEncoder` constructor to `preset='fast', crf=23` (was `ultrafast/28`) for cleaner mp4 training inputs. Added to the overlay chain on 2026-05-15 — prior builds shipped the upstream baseline (the source edit in `physical_ai_tools/...` had no path into the image until this overlay entry existed). |
| `omx_f_config.yaml` | (no filter) | Dual-camera config only — `safety_envelope` block removed in the stripdown |
| `physical_ai_server.py` | `*/physical_ai_server/physical_ai_server.py` | `_BearerTokenScrubber` log filter, usb_cam-fps warning, `_workflow_last_detections` cache for the debugger, all Roboter Studio service handlers |
| `communicator.py` | `*/communication/communicator.py` | Adds `get_latest_bgr_frame()` + `get_latest_follower_joints()` for Roboter Studio calibration provider, plus per-camera arrival deque feeding the usb_cam-fps warning. **F65** adds `_pick_synced_camera_msgs()` — short per-camera (`stamp_ns`, msg) ring (`_CAMERA_SYNC_HISTORY=8`) and a Cartesian-walk picker that returns the freshest tuple whose max-min header stamp ≤ `_CAMERA_SYNC_SLOP_NS=15_000_000` (15 ms). Falls back to latest-per-camera when only one camera is configured, when any msg carries `stamp_ns=0`, or when no matched tuple exists. Single-camera and warmup paths unchanged. **F66** hardens the picker against `RuntimeError: deque mutated during iteration` (CPython raises on concurrent `append()`; the server runs `MultiThreadedExecutor(num_threads=3)`) via a 3-attempt retry that falls through to the latest-per-camera path; also clears the F65 rings in `clear_latest_data()` so a re-record episode's first tick doesn't pair fresh + stale msgs from the prior episode. |

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

Then it **copies in the entire workflow module** (`overlays/workflow/`) as an addition to `physical_ai_server.workflow` (not as an overlay, since there is no upstream file to compare). 11 files: `__init__.py`, `auto_pose.py`, `calibration_manager.py`, `coco_classes.py`, `color_profile.py`, `ik_solver.py`, `interpreter.py`, `perception.py`, `projection.py`, `trajectory_builder.py`, `workflow_manager.py` (+ `handlers/{__init__.py, motion.py, output.py, destinations.py, perception_blocks.py}`). The previous `safety_envelope.py` was removed in the 2026-05 stripdown along with the matching inference clamps.

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
1. **Update gate** — `update_checker.check_for_update()` polls `/version` on Railway. If newer, **blocking** modal → download to `%TEMP%` → `os.startfile()`. The `/version` endpoint reads `GUI_VERSION` + `GUI_DOWNLOAD_URL` env vars on Railway; bumping these is the only way to push a new `.exe` to existing student installs (CLAUDE.md §14).
2. **Cloud-only checkbox** — if checked, skips arm/camera scan and starts only `physical_ai_manager` (with `--no-deps`); appends `?cloud=1` to the WebView URL so React skips rosbridge gate.
3. **Arm scan** — daemon thread runs `device_manager.scan_and_identify_arms()`: `usbipd list` → filter VID `2F5D` → attach all → start a throw-away `nettername/open-manipulator` container with `--privileged -v /dev:/dev --entrypoint sleep 120` → for each `/dev/serial/by-id/...`, run `docker exec ... identify_arm.py <port>` (pings IDs 1-6 and 11-16 at 1 Mbps).
4. **Camera scan** — daemon thread runs `wsl_bridge.list_video_devices()` (iterates `/dev/video*` with `v4l2-ctl --info`). Up to 2 cameras with role assignment dropdowns.
5. **Auto-pull on start** — `docker_manager.check_for_updates()` runs on EVERY GUI launch. The 2.2.4 rebuild added three layers of defence so existing installs actually pick up newer images instead of silently sticking with cached ones: (a) **offline short-circuit** via `is_dockerhub_reachable()` (5 s TCP probe to `registry-1.docker.io:443` — skip the whole pull flow when offline rather than burning 6-12 min on retry storms); (b) **manifest-digest pre-check** via `_get_remote_manifest_digest()` (single `docker manifest inspect` per image, no layer download) compared against `_get_local_repo_digest()` — skip the real pull entirely when local == remote, cutting the steady-state path from ~30 s of pulls to ~3 s of HEAD requests; (c) **last-pull persistence** to `%LOCALAPPDATA%\EduBotics\.last_image_pull.json` (timestamp + per-image digests). The GUI surfaces this on the next launch as "Image-Frische: letzter Update vor X Tagen" with a warning banner past `IMAGE_FRESHNESS_WARN_DAYS=14`. Per-image failures remain non-fatal — students keep using the cached version. Disable entirely with `EDUBOTICS_SKIP_AUTO_PULL=1` for classrooms that explicitly manage their own image cadence.
6. **Start button** — runs off UI thread:
   - Re-attach all USB → poll `/dev/serial/by-id/` (10× 1s)
   - Regenerate `.env` via `config_generator.generate_env_file()` with `_atomic_write()` (write to `.tmp` + `os.replace()`, guards power-loss). Keys: `FOLLOWER_PORT`, `LEADER_PORT`, `CAMERA_DEVICE_1`, `CAMERA_NAME_1` (default `gripper`), `CAMERA_DEVICE_2`, `CAMERA_NAME_2` (default `scene`), `ROS_DOMAIN_ID`, `REGISTRY`. All values **double-quoted** (paths with spaces).
   - `docker compose --env-file ... -f docker-compose.yml [-f docker-compose.gpu.yml] up -d --force-recreate` (GPU compose layered if `nvidia-smi` succeeds on host)
   - Poll `:80/version.json` until 200 → spawn WebView2 subprocess
7. **Pull stall watchdog** — `docker_manager._pull_one_image()`: 20s poll interval, 10 MB disk-growth threshold (reads `/var/lib/docker/overlay2`), 600s `stall_timeout` (auto-pull path uses tighter 120s + 2 retries — failures move on). On stall: `pkill -KILL dockerd`, restart, retry with exp backoff `min(4*2^(attempt-1), 30)` s, max 4 retries. **Main knob for poor-network classrooms.**

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
   - Images: `cv_bridge.compressed_imgmsg_to_cv2()` (overlay enforces `desired_encoding='bgr8'` as defensive only — usb_cam returns BGR anyway) → `cvtColor(..., BGR2RGB)` → `uint8` HWC.
   - Follower: `JointState → joint_state2tensor_array()` reorders per `joint_order` (default `[joint1, joint2, joint3, joint4, joint5, gripper_joint_1]`) → `float32 [6]`. KeyError on missing joint propagates as upstream behavior.
   - Leader action: `joint_trajectory2tensor_array()` reads `points[0].positions`, reorders. Empty `msg.points` raises `IndexError` (upstream behavior; the German wrapper was removed in the stripdown). Extra joints in the message log a one-time `[WARNUNG]` and are dropped (kept — warning only).
3. `create_frame()` → `{'observation.images.gripper': ..., 'observation.images.scene': ..., 'observation.state': ..., 'action': ...}`, all `float32`.
4. `add_frame_without_write_image()` validates vs schema, appends to episode buffer, auto-timestamps as `frame_index / fps` (wall-clock NOT used).
5. Video encoding: raw RGB piped to `ffmpeg libx264 -preset fast -crf 23 -pix_fmt yuv420p -g 2`, **async** (audit F62 — was `-preset ultrafast -crf 28` before, which baked visible chrominance noise into the training inputs the policy then had to memorize; the new settings are `libx264`'s "visually transparent" sweet spot for ~1.5-1.8× the disk).
6. `save_episode_without_video_encoding()` writes parquet + mp4 + `meta/info.json` (`codebase_version: "v2.1"`).

Dataset path inside container: `~/.cache/huggingface/lerobot/{user_id}/{robot_type}_{task_name}/`. Optional rosbag2: `/workspace/rosbag2/{repo_name}/{episode_index}/`.

Post-save warnings the student sees in `TaskStatus.error` (all warning-only — episodes always complete):
- Video file missing or zero-byte → `[FEHLER] Episode {num}: Video-Datei(en) nicht korrekt gespeichert ({problems})...`
- Timestamp gaps > 2× expected_dt → `[WARNUNG] Episode {num}: {n} Zeitlücken erkannt (erwartet ~{ms} ms pro Frame)...`
- Stale camera (no decoded-pixel change for 5 s during recording) → `[WARNUNG] Kamera "{name}" liefert seit über 5s dasselbe Bild. Aufnahme läuft weiter — bitte prüfen, ob die Szene wirklich statisch ist oder die Kamera hängt.`
- Camera observed-Hz < 0.8 × target_fps at recording start → `[WARNUNG] Kamera "{cam_name}" liefert nur {observed:.1f} Hz, Aufnahme erwartet {target_fps:.0f} Hz...`

HF upload calls `push_to_hub` directly (no timeout wrapper). On network failure the exception propagates and the recording session ends; the local dataset on disk is intact and can be re-uploaded manually.

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
   │       --dataset.repo_id=... --output_dir=... --policy.push_to_hub=false --eval_freq=0
   │       --dataset.image_transforms.enable=true   # audit F63 (always)
   │       [--policy.n_action_steps=15]"             # audit F64 (when policy.type=act and
   │                                                  #   training_params lacks n_action_steps)
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
3. Lazy `load_policy()` on first tick (downloads from HF via `HF_HOME=/root/.cache/huggingface` if missing; moves weights to GPU). German `[FEHLER]` if CUDA is unavailable; otherwise upstream load behavior.
4. **Audit F10 extra-camera filter**: overlay reads expected camera names from `policy_config.input_features` keys matching `observation.images.*`. Connected cameras NOT in the expected set are silently dropped with a one-time `[WARNUNG] Zusätzliche Kameras werden ignoriert: …` so the policy doesn't see an unknown observation key. **MISSING** expected cameras are no longer rejected — the upstream `policy.select_action` raises a `KeyError`/`RuntimeError` and the broad `except` in `physical_ai_server.py` ends the inference session cleanly. No more silent tick-skipping.
5. `_preprocess(images, state)`: per image `torch.from_numpy / 255 → permute(2,0,1) → unsqueeze(0)` keyed `observation.images.{name}`. State → `float32` tensor → batch.
6. `policy.select_action(observation)` under `torch.inference_mode()`.
7. `data_converter.tensor_array2joint_msgs(action, ...)` builds `JointTrajectory` with **fps-aware `time_from_start`** (overlay computes `_action_duration_ns = max(int(1.5e9/fps), 1_000_000)`).
8. `communicator.publish_action(msg)` → `/arm_controller/follow_joint_trajectory` (which, after the magic remap, is `/leader/joint_trajectory` → drives the follower).

The software safety envelope that previously sat between steps 6 and 7 (NaN/Inf reject, joint clamp, per-tick velocity cap, first-tick seeding, action-shape reject, stale-camera halt, image-shape mismatch escalation) was removed in the 2026-05 stripdown. Hardware safety — Dynamixel firmware position limits, gripper current limits, JointTrajectoryController constraints, post-sync verification, torque-disable on shutdown — is the remaining defense.

`gripper_joint_1` is the 6th element of the same action array, on the same `JointTrajectory`. There is no separate gripper service.

### 6.7 Roboter Studio (calibration → Blockly authoring → execution)

**Calibration** (per-camera, multi-step state machine in `overlays/workflow/calibration_manager.py`):
- ChArUco board: **7×5 squares**, **30 mm square / 22 mm marker**, dictionary **DICT_5X5_250**.
- Steps in order: intrinsic(gripper) → intrinsic(scene) → handeye(gripper, eye-in-hand) → handeye(scene, eye-to-base) → colour profile.
- `INTRINSIC_FRAMES_REQUIRED = 12`, `HANDEYE_FRAMES_REQUIRED = 14`.
- Hand-eye solved with both PARK and TSAI; warn if rotation differs > **4 deg** or translation > **10 mm**.
- Persisted under `/root/.cache/edubotics/calibration/` (named volume `edubotics_calib`, **survives `docker compose down`**) as `{camera}_intrinsics.yaml`, `{camera}_handeye.yaml`, `color_profile.yaml`.
- Provider methods come from the `communicator.py` overlay: `get_latest_bgr_frame()`, `get_latest_follower_joints()`. Without the overlay, the wizard returns `[FEHLER] Kein Kamerabild verfügbar.`

**Authoring** — React `Workshop/` editor (Blockly 12.5.0 + react-blockly 9.0.0). The 2026-05 upgrade widened the block allowlist (defined in `overlays/workflow/interpreter.py:ALLOWED_BLOCK_TYPES` and mirrored in `cloud_training_api/app/validators/workflow.py:ALLOWED_BLOCK_TYPES`):
- **Motion / output (statement)**: `edubotics_home`, `edubotics_open_gripper`, `edubotics_close_gripper`, `edubotics_move_to`, `edubotics_pickup`, `edubotics_drop_at`, `edubotics_wait_seconds`, `edubotics_destination_pin`, `edubotics_destination_current`, `edubotics_log`, `edubotics_play_sound`, `edubotics_speak_de`, `edubotics_play_tone`.
- **Events / hat blocks (top-only)**: `edubotics_broadcast`, `edubotics_when_broadcast`, `edubotics_when_marker_seen`, `edubotics_when_color_seen`. Each hat handler runs as a separate daemon thread; a single `ctx.motion_lock` keeps motion serialized between handlers.
- **Perception (value)**: `edubotics_detect_color`, `edubotics_detect_object`, `edubotics_detect_marker`, `edubotics_count_color`, `edubotics_count_objects_class`, `edubotics_wait_until_color`, `edubotics_wait_until_object`, `edubotics_wait_until_marker`, **`edubotics_detect_open_vocab`** (cloud-burst to OWLv2 on Modal — §8.3). The `_cloud_vision_burst` handler in `physical_ai_server.py` honors the `enabled` flag from `StartWorkflow.srv` (audit F54 — `perception_blocks.py:216-218` checks `ctx.cloud_vision.get('enabled')` before calling out) so that a workflow saved before the student enabled cloud vision in the React toolbox doesn't silently start burning Modal credits.
- **Lists / procedures / math (Blockly built-ins)**: `lists_create_with`, `lists_repeat`, `lists_length`, `lists_isEmpty`, `lists_indexOf`, `lists_getIndex`, `lists_setIndex`, `lists_getSublist`, `procedures_defnoreturn/defreturn/callnoreturn/callreturn/ifreturn`, `math_random_int`, `math_constrain`, `math_modulo`, `math_round`, plus everything from the previous shipped set.
- Allowed colors: `rot, gruen, blau, gelb` (German); validation rejects other strings.
- `MAX_LOOP_ITERATIONS = 10000`.

**Editor UX upgrades** (2026-05):
- Blockly plugins wired in `BlocklyWorkspace.jsx` via best-effort dynamic imports: `@blockly/plugin-workspace-search`, `@blockly/workspace-backpack`, `@blockly/plugin-zoom-to-fit`, `@blockly/workspace-minimap`, `@blockly/block-plus-minus`, `@blockly/suggested-blocks`, `@mit-app-inventor/blockly-plugin-workspace-multiselect`, color-blind themes (`@blockly/theme-tritanopia`, `@blockly/theme-deuteranopia`, `@blockly/theme-highcontrast`), `@blockly/field-grid-dropdown`, `@blockly/field-multilineinput`. The plugins are **best-effort** — a missing module logs a warning but the editor still renders.
- Autosave to IndexedDB via `useAutosave.js` (debounced 750 ms, periodic 15 s flush, restored on mount; quota-exceeded → German toast).
- New `ToolbarButtons.jsx` component above the editor: undo/redo (`workspace.undo(false/true)`), zoom-to-fit, save (Ctrl+S), export to `.json`, import, theme switcher, autosave-age chip.
- Shadow-block defaults on every numeric/text input in `blocks/toolbox.js` so beginners don't have to drag a `math_number` into every value slot.
- Field validators on motion / perception / destinations / output blocks (clamp wait_seconds, marker IDs, tones; reject unknown colors / object classes).
- Mobile-responsive layout: `flex flex-col` below md breakpoint, `md:grid` above. Editor + camera + controls stack on phones/tablets.

**Block-level debugger** (2026-05): `DebugPanel.jsx` with three tabs (Sensoren / Variablen / Haltepunkte), pause/step/continue buttons in `RunControls.jsx`, breakpoints persisted in Redux + sent to the server via `WorkflowSetBreakpoints.srv`. The runtime checks each block id against `ctx.breakpoints` before dispatch; on hit, it sets `ctx.set_paused(True)` and waits on `ctx.wait_for_resume()`. The `[VAR:name=json]` log sentinel feeds the variable inspector. Sensor live-readout (`/workflow/sensors` topic, `SensorSnapshot.msg`, 5 Hz) shows follower joints, gripper opening, visible AprilTag IDs, color-pixel counts per color, and visible YOLO classes — the perception fields are derived from `_workflow_last_detections`, a cache `_emit_workflow_status` populates from every `ctx.emit_detections(...)` call (audit O1). The cache has a 2 s TTL so stale results from a finished detect block don't keep showing up.

**Workflow versioning**: every PATCH /workflows/{id} that changes `blockly_json` triggers `snapshot_workflow_version` (Supabase migration 015) which inserts the prior payload into `public.workflow_versions`. Capped at 20 per workflow via the `prune_workflow_versions` AFTER-INSERT trigger. Listed via `GET /workflows/{id}/versions`; restore via `POST /workflows/{id}/versions/{version_id}/restore`. The trigger reads the `app.user_id` Postgres GUC; the cloud API calls a SECURITY DEFINER RPC `update_workflow_blockly(p_workflow_id, p_user_id, p_blockly_json, p_name, p_description)` (migration **018**) which wraps `set_config('app.user_id', p_user_id::text, true)` and the UPDATE in one transaction so the trigger sees the right UUID and writes `saved_by` correctly. Restore is its own RPC `restore_workflow_version(p_workflow_id, p_version_id, p_user_id)`. Service-role admin tools that don't route through these RPCs still leave `saved_by` NULL — by design. Migration 018 also adds the `Group members read group workflow versions` RLS policy so siblings see the full Verlauf history of any group-shared workflow (mirrors the parent workflows policy).

**Tutorials / skillmap**: 7 starter tutorials at `physical_ai_manager/public/tutorials/*.json` (sage_hallo, bewege_zum_punkt_a, roten_wuerfel_aufnehmen, zaehle_blaue_objekte, stapele_drei_wuerfel, sortiere_nach_klasse, ereignis_marker_gefunden — covers hat blocks + broadcast). The `SkillmapPlayer.jsx` sidebar steps the student through each, applying per-step `allowed_blocks` as a toolbox restriction (the `restrictedBlocks` prop on `BlocklyWorkspace`). Progress synced via `GET/PATCH /me/tutorial-progress` and the `tutorial_progress` table (migration 016, with realtime publication so teacher dashboards live-update).

**Classroom gallery** (`GalleryTab.jsx`): renders all `is_template=TRUE` workflows for the student's classroom + group-shared workflows from peers; each card has a Klonen button that calls `/workflows/{id}/clone`.

**Validation** — `cloud_training_api/app/validators/workflow.py:validate_blockly_json()` enforces:
- `MAX_BLOCKLY_JSON_BYTES = 256 * 1024` (256 KB) → 413 `Workflow ist zu groß (>256 KB).`
- `MAX_BLOCKLY_DEPTH = 64` → 400 `Workflow ist zu tief verschachtelt.`
- JSON encoding error → 400 `Workflow-JSON ist ungültig: {error}`
- `MAX_NAME_LENGTH = 100`

Both `routes/workflows.py` (student) AND teacher template route call this validator (audit fix).

**Execution** — `WorkflowManager` daemon thread, `WorkflowContext` (publisher, IK, perception, destinations, z_table, intrinsics, last_arm_joints, motion_lock, var_lock, breakpoints, cloud_vision). On workflow stop/error the daemon thread exits and the arm holds wherever it was — the auto-home recovery routine was removed in the 2026-05 stripdown. The student manually resets the arm before the next run.

**Perception** — eager initialization with **silent fallback to empty detections** on missing ONNX or missing `pupil_apriltags` (the Workshop UX continues; the affected detector returns `[]`). The hard-raise behavior was dropped in the 2026-05 stripdown — a missing detector is no longer a motion-safety event:
- **YOLOX-tiny ONNX** at 640×640 letterbox via `onnxruntime`, COCO classes filter (~80 classes, `coco_classes.py`).
- **LAB color matching** with per-channel σ threshold (default 3.0; std floored to 1.0 to prevent divide-by-zero); `MORPH_OPEN` then `MORPH_CLOSE` (3×3 kernel). No minimum-blob-area filter — every detected contour produces a `Detection` (the prior `LAB_MIN_BLOB_AREA_PX = 100` filter was dropped in the stripdown).
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
| `MODAL_VISION_APP_NAME` | `edubotics-vision` | for staging the OWLv2 cloud-burst app (`routes/vision.py`) |
| `MODAL_VISION_FUNCTION_NAME` | `OWLv2Detector.detect` | fully-qualified Modal class.method name |
| `VISION_MODAL_TIMEOUT_S` | `30.0` | per-call deadline for `/vision/detect` proxy (separate from the worker's own `@app.cls(timeout=120)`) |
| `HF_TOKEN` | `""` | dataset preflight + GDPR Art. 17 student artifact cleanup + dataset reconciliation sweep |
| `GUI_VERSION` | unset → 503 | `/version` |
| `GUI_DOWNLOAD_URL` | unset → 503 | `/version` |
| `POLICY_TIMEOUT_OVERRIDES_JSON` | unset | per-policy timeout overrides |
| `DATASET_SWEEP_INTERVAL_S` | `600` | period of the HF→Postgres reconciliation loop (`services/dataset_sweep.py`) |
| `DATASET_SWEEP_DISABLED` | unset | set to `1` to opt out (e.g. in unit-test contexts) |
| `EDUBOTICS_SKIP_SCHEMA_CHECK` | unset | set to `1` to skip the boot-time `_validate_required_schema()` probe (unit-test only — never set on Railway) |

### 7.3 Per-policy timeout caps (`routes/training.py:POLICY_MAX_TIMEOUT_HOURS`)
```
act:       4.0h
vqbet:     4.0h
tdmpc:     4.0h
diffusion: 6.0h
pi0fast:   6.0h
pi0:      10.0h
smolvla:  10.0h
```
Cap applied AFTER request validation but BEFORE Modal dispatch — DB row stores capped value. Values were raised from the v1 `1.5/2/2/4/4/6/6` profile in 2026-05 after ACT/VQBET runs on large datasets were truncating before convergence; the outer `MAX_TRAINING_TIMEOUT_HOURS=12` envelope still applies and the Modal `@app.function(timeout=7*3600)` ceiling clamps anything above 7h in practice.

### 7.4 Rate limit rules (in-process, keyed by leftmost X-Forwarded-For — except `/vision/detect`, which is keyed by JWT `sub`)
| Method | Path prefix | Limit |
|---|---|---|
| `*` | `/trainings/start` | 10 / 60s |
| `*` | `/trainings/cancel` | 20 / 60s |
| `POST` | `/workflows` | 10 / 60s |
| `POST` | `/teacher/classrooms` | 10 / 60s (covers classroom + template creation + workgroup creation under the same prefix) |
| `POST` | `/teacher/workgroups` | 20 / 60s (member add/remove + credit adjustment bursts) |
| `POST` | `/datasets` | 20 / 60s (one row per upload; protects against runaway recording loops) |
| `POST` | `/vision/detect` | 5 / 60s **per user** (cloud-burst OWLv2; `_PER_USER_RATE_LIMIT_PREFIXES` reads JWT `sub` so 30 NAT'd students don't share one bucket) |

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
| GET | `/me/export` | GDPR Art. 15 — JSON bundle (profile, trainings, `workgroup_memberships` audit, `workflows`, `datasets`, classrooms (teachers), `progress_entries` + `classroom_progress_entries` (students) + `workgroup_progress_entries` for groups the student is/was in) |
| POST | `/me/delete` | GDPR Art. 17 — refuse for admins (400), cancel active trainings, **disengage from current workgroup** (sets `users.workgroup_id=NULL` + `workgroup_memberships.left_at=NOW()` so the slot frees during the 30-day admin window while audit visibility persists), set `users.deletion_requested_at=now()` |

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
| PATCH | `/teacher/students/{id}/vision-quota` | set `users.vision_quota_per_term` for one of the teacher's own students (int ≥0 or `null`); teacher cannot exceed their own ceiling if it's set |
| GET | `/teacher/students/{id}/trainings` | last 100 |
| GET/POST | `/teacher/classrooms/{id}/workgroups` | list / create work groups (rate-limited 20/60s on POST via `/teacher/workgroups` prefix) |
| GET/PATCH/DELETE | `/teacher/workgroups/{id}` | detail (members + usage) / rename / delete (409 if non-empty) |
| POST | `/teacher/workgroups/{id}/members` | add a student to the group (asserts classroom match + capacity ≤10; bumps `workgroup_memberships`) |
| DELETE | `/teacher/workgroups/{id}/members/{studentId}` | remove a member (clears `users.workgroup_id`, sets `workgroup_memberships.left_at`) |
| POST | `/teacher/workgroups/{id}/credits` | RPC `adjust_workgroup_credits` (P0022 → 403, P0012-P0014 → 409) |
| GET | `/teacher/workgroups/{id}/trainings` | last 100 trainings spawned in this group (from any current or former member); includes `started_by_username` / `started_by_full_name` attribution |
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
| PATCH | `/admin/teachers/{id}/vision-quota` | set `users.vision_quota_per_term` (int ≥0 or `null` = unbounded); only the admin can lift a teacher's ceiling |
| POST | `/admin/teachers/{id}/password` | reset |
| DELETE | `/admin/teachers/{id}` | refuse if classroom_count > 0 |

**`/workflows`** routes (require any auth):
| Method | Path | Purpose |
|---|---|---|
| GET | `/workflows` | own workflows (paginated) + classroom templates (up to MAX_LIST_LIMIT=500) |
| GET/PATCH/DELETE | `/workflows/{id}` | detail / update (calls `validate_blockly_json`) / delete |
| POST | `/workflows` | create (calls `validate_blockly_json`) |
| POST | `/workflows/{id}/clone` | non-template copy (owner can clone own; classmate can clone templates) |
| GET | `/workflows/{id}/versions` | last 20 snapshots (newest first) — Roboter Studio Verlauf dropdown |
| POST | `/workflows/{id}/versions/{version_id}/restore` | replace current `blockly_json` with the snapshot |

**`/me/tutorial-progress`** routes (require any auth):
| Method | Path | Purpose |
|---|---|---|
| GET | `/me/tutorial-progress` | every tutorial progress row for the caller |
| PATCH | `/me/tutorial-progress/{tutorial_id}` | upsert current_step / completed flag |

**`/vision`** routes (require any auth, rate-limited 5/60s):
| Method | Path | Purpose |
|---|---|---|
| POST | `/vision/detect` | proxy to the OWLv2 Modal app for German open-vocabulary detection (§8.3) |

### 7.7 Helper functions (the IDOR firewall)
- `_assert_classroom_owned(teacher_id, classroom_id)` → German `Klassenzimmer nicht gefunden` (`routes/teacher.py`)
- `_assert_student_owned(teacher_id, student_id)` → German `Schueler nicht gefunden` / `Schueler gehoert zu keinem Klassenzimmer` (`routes/teacher.py`)
- `_assert_entry_owned(teacher_id, entry_id)` → German `Eintrag nicht gefunden` (`routes/teacher.py`)
- `_assert_workflow_owned(user_id, workflow_id)` → German `Workflow nicht gefunden` (`routes/workflows.py`)
- `_assert_workgroup_owned(teacher_id, workgroup_id)` → German `Arbeitsgruppe nicht gefunden` (`routes/workgroups.py`) — the membership-write equivalent of `_assert_classroom_owned` for the workgroups feature added in migration 011
- `_assert_workgroup_in_classroom(workgroup_id, classroom_id)` → 409 when a member-add or progress-entry crosses classrooms (`routes/teacher.py`)

**Any new endpoint that touches another user's row MUST call one of these.** A single missed assertion is a silent IDOR — RLS is dormant under service-role.

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

### 8.3 vision_app.py — Roboter Studio open-vocabulary detection (Phase-3)

`robotis_ai_setup/modal_training/vision_app.py` — Modal app `edubotics-vision`, T4 GPU, `min_containers=0`, `scaledown_window=180`, `enable_memory_snapshot=True`, `@app.cls(timeout=120)` (raised from 30s in audit F37 — fresh containers that miss the snapshot cache need 30-60s to download weights on a slow HF mirror).
- Model: `google/owlv2-base-patch16-ensemble` (Apache-2.0, 200M params). CLIP text encoder handles German prompts natively (`rote Tasse`, `gelbe Banane`, …).
- Image: `modal.Image.debian_slim(python_version="3.11")` + `transformers==4.46.0`, `pillow`, **`huggingface_hub==0.26.2`** (pinned in audit F44 — `>=0.25.0` left us drifting whenever HF cut a release; `0.26.2` matches the `transformers 4.46.0` contract), `numpy`. **Torch is then force-reinstalled from `cu121`** (`torch==2.4.0`, `torchvision==0.19.0`, `index_url=https://download.pytorch.org/whl/cu121`, `extra_options="--force-reinstall"`) — without that, pip resolves CPU wheels from PyPI and the T4 GPU sits idle (audit round-3 §H). HuggingFace cache on a persistent `modal.Volume` (`edubotics-vision-cache`).
- **Secret split**: uses `modal.Secret.from_name("edubotics-vision-secrets")`, NOT `edubotics-training-secrets`. The training bundle contains `SUPABASE_SERVICE_ROLE_KEY` + the write-scoped HF token; the vision worker needs neither and leaking either into a less-audited inference path is exactly the misconfiguration to avoid (audit round-3 §I). Deploy fails loudly if the operator hasn't created the vision bundle.
- **Snapshot-aware lifecycle** (audit round-3 §B/§C):
  - `@modal.enter(snap=True) load_weights` runs **before** the snapshot is taken on a CPU-only builder. `Owlv2ForObjectDetection.from_pretrained(..., torch_dtype=torch.float32, device_map=None)` then `.to("cpu")` (audit F40 — explicit `device_map=None` overrides the accelerate-default `"auto"` that would bind to CUDA at snapshot-build time and crash the build).
  - `@modal.enter(snap=False) bind_device` runs **after** snapshot restore on the live GPU. `.to("cuda").half()` (audit F41 — FP16 on T4 gives ~1.6× throughput with no accuracy regression at the IoU we use; falls back to FP32 on CPU containers).
- `OWLv2Detector.detect(image_bytes, prompts, score_threshold=0.25)` returns `{detections: [{label, score, bbox: [x1,y1,x2,y2]}], cold_start: bool}`. Threshold raised from 0.10 → **0.25** in audit F37 to cut spurious German-prompt false positives.
- **EXIF-before-RGB ordering** (audit F39): `Image.open(BytesIO(image_bytes))` → `ImageOps.exif_transpose(img)` → `.convert("RGB")`. The pre-F39 ordering called `convert("RGB")` first, which discards EXIF metadata, leaving the subsequent `exif_transpose` as a silent no-op on phone-camera images.
- Cost model: T4 = $0.59/hr per the 2026 Modal pricing page (https://modal.com/pricing). With `min_containers=0`, `scaledown_window=180`, and `enable_memory_snapshot=True`, an idle classroom pays nothing and a warm-path call runs in 200-400 ms (~$0.00007 per call). Cold-start storms add to the bill — each fresh-after-scale-down container costs the 2-5 s of T4 burn before snapshot-restore finishes — so per-classroom term cost is realistically $1-$2 in compute, NOT the $0.50 a pure warm-only model would predict. Budget accordingly.
- Cloud bridge: `cloud_training_api/app/routes/vision.py` exposes `POST /vision/detect` (rate-limited 5/60s **per-user via JWT `sub`**, not per-IP — protects 30 NAT'd students from sharing one bucket; audit round-3 §BD). Per-user term quota via optional `users.vision_quota_per_term` column, atomic test-and-increment via `consume_vision_quota` RPC, refund on transient 502/504 errors via `refund_vision_quota` RPC.
- React block `edubotics_detect_open_vocab` (see §6.7) routes German prompts through a small synonym dict first; cloud burst is the fallback. The block is opt-in via `cloud_vision_enabled` on `StartWorkflow.srv`, persisted in `workshopSlice.js` (audit F29) and gated in `toolbox.js` (F28). Prompt validator enforces `OPEN_VOCAB_PROMPT_MAX = 80` chars and ≤8 prompts (F33).
- Deploy: `modal deploy modal_training/vision_app.py`. Smoke: `modal run -m vision_app::smoke_test`.

### 8.2 training_handler.py (~700 lines)
**Constants**: `OUTPUT_DIR = Path("/tmp/training_output")`, `EXPECTED_CODEBASE_VERSION = "v2.1"`, `MIN_JOINTS = 4`, `MAX_JOINTS = 20`. Module-level `_current_job: dict | None` for signal handler.

**Flow** of `run_training()`:
1. Read `SUPABASE_URL`, `SUPABASE_ANON_KEY` from env (raises `RuntimeError` if missing); `HF_TOKEN` optional.
2. `huggingface_hub.login(token=hf_token)` if token present.
3. `_preflight_dataset(dataset_name, hf_token)` — 60s thread-join timeout on `hf_hub_download(meta/info.json)`. Validates: codebase_version == "v2.1", fps > 0, `observation.state` and `action` features exist with `.names` lists of length 4-20, joint name parity, ≥ 1 `observation.images.*`. **14 distinct German error variants** (verbatim — see file lines 192-289).
4. `_update_supabase_status("running")`.
5. `_build_training_command(...)`: `[python, -m, lerobot.scripts.train, --policy.type=..., --policy.device=cuda, --dataset.repo_id=..., --output_dir=..., --policy.push_to_hub=false, --eval_freq=0, --dataset.image_transforms.enable=true]` + (when `model_type=='act'` and the caller hasn't set `n_action_steps`) `--policy.n_action_steps=15` (audit F64 default — re-query every 0.5 s instead of committing 3.3 s of open-loop action) + optional `--seed`, `--num_workers`, `--batch_size`, `--steps`, `--log_freq`, `--save_freq`, `--policy.n_action_steps={n}` (if `training_params['n_action_steps']` is set). Default `total_steps = training_params.get("steps", 100000)`. Audit F63 forces `image_transforms.enable=true` for every policy type — brightness/contrast/saturation/hue/sharpness jitter from LeRobot's built-in `ImageTransformsConfig`, max 3 per frame, weighted random subset — closes the classroom-lighting distribution shift.
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

## 9. Supabase schema (base + 14 numbered migrations)

> **File map:** `migration.sql` (base) + `002_accounts.sql` + `003_lessons_and_notes.sql` (immediately superseded by 004) + `004_progress_entries.sql` + `005_cloud_job_id.sql` + `006_loss_history.sql` + `007_deletion_requested_at.sql` + `008_workflows.sql` + `009_workflows_rls_writes.sql` + `010_progress_terminal_guard.sql` + `011_workgroups.sql` + `012_dataset_sweep.sql` + `013_revoke_anon_from_security_definer.sql` + `015_workflow_versions.sql` + `016_tutorial_progress.sql` + `017_vision_quota.sql` + `018_workflow_versions_author_and_group_rls.sql`. **There is no `014_*.sql`** — the Phase-2 bundle skipped 014 to avoid a filename collision discovered mid-rollout (see note before §9.15). Every numbered migration has a matching file in `supabase/rollback/`; `migration.sql`, the superseded `003_*.sql`, and `018_*.sql` do not (018 is idempotent: `CREATE OR REPLACE` + `DROP POLICY IF EXISTS` + `CREATE POLICY`).

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

### 9.12 012_dataset_sweep.sql — adds `datasets.discovered_via_sweep BOOLEAN NOT NULL DEFAULT FALSE`. Marks rows registered by the periodic Railway sweep (see §10.5) versus rows registered live by the React app right after upload. Informational only — sweep does not skip rows on subsequent ticks.

### 9.13 013_revoke_anon_from_security_definer.sql (audit-23032cb hardening)

Strips the `EXECUTE` grant from `PUBLIC`, `anon`, and `authenticated` on every `SECURITY DEFINER` RPC the cloud API uses: `start_training_safe`, `update_training_progress`, `adjust_student_credits`, `adjust_workgroup_credits`, `get_remaining_credits`, `get_teacher_credit_summary`. Postgres' default ACL silently re-grants `EXECUTE` to `PUBLIC` whenever a function is created without `REVOKE FROM PUBLIC`, and Supabase's `anon`/`authenticated` roles inherit it; a missed REVOKE means a logged-in student could call `adjust_student_credits` directly with someone else's `p_teacher_id` and bypass `_assert_student_owned`. The migration is idempotent — it loops over a fixed list of `(name, signature)` pairs and `REVOKE EXECUTE ... FROM ...` each one. **Service-role is unaffected** (it's `BYPASSRLS` and runs as a Postgres superuser-equivalent on Supabase). Rollback re-grants `EXECUTE` to `authenticated` only.

> **Note on the missing 014:** A Phase-2 branch initially landed `013_workflow_versions.sql` and `014_tutorial_progress.sql`. When the anon-revoke hotfix needed `013`, the Phase-2 pair was renumbered up to `015` / `016` — leaving `014` permanently unused. Don't reintroduce a `014_*.sql` file; either renumber upward or hand-pick the next free integer.

### 9.15 015_workflow_versions.sql (Roboter Studio Phase-2 history)
- New table `public.workflow_versions` (id, workflow_id FK CASCADE, blockly_json JSONB, note TEXT, created_at TIMESTAMPTZ, saved_by UUID FK SET NULL).
- Index `idx_workflow_versions_workflow` on `(workflow_id, created_at DESC)`.
- BEFORE-UPDATE trigger `trg_workflows_snapshot` on `public.workflows`: if `blockly_json` changes, insert the OLD payload into `workflow_versions`. Function is `SECURITY DEFINER` with explicit `SET search_path = public`.
- AFTER-INSERT trigger `trg_workflow_versions_prune` on `public.workflow_versions`: deletes rows beyond the newest 20 per `workflow_id`. SECURITY DEFINER as well.
- RLS read policies: owner of the parent workflow can read; admin can read all. No public INSERT (the trigger is the only writer).
- Realtime publication added (idempotent DO block).

### 9.16 016_tutorial_progress.sql (Roboter Studio Phase-3 skillmap)
- New table `public.tutorial_progress` (composite PK `(user_id, tutorial_id)`, current_step INT default 0, completed_at TIMESTAMPTZ NULL, updated_at TIMESTAMPTZ).
- Trigger `trg_tutorial_progress_touch` updates `updated_at` on every UPDATE.
- RLS: owner reads/writes own; admin reads all; teacher reads own students' progress (joined via `users.classroom_id`).
- Endpoints in `cloud_training_api/app/routes/me.py`: `GET /me/tutorial-progress`, `PATCH /me/tutorial-progress/{tutorial_id}`.
- Realtime publication added (`ALTER PUBLICATION supabase_realtime ADD TABLE public.tutorial_progress`) so teacher-dashboard subscribers see live progress updates.
- Explicit `GRANT ALL TO service_role` so the cloud API's service-role connection has consistent ACL alongside its RLS bypass.

### 9.17 017_vision_quota.sql (Roboter Studio Phase-3 cloud-vision)
- Adds `users.vision_quota_per_term INTEGER` (NULL = unbounded) and `users.vision_used_per_term INTEGER NOT NULL DEFAULT 0`, plus a CHECK constraint that floors the counter at 0.
- `consume_vision_quota(p_user_id UUID)` — atomic test-and-increment, `SECURITY DEFINER`. Returns `(allowed, remaining)`. The UPDATE only fires when the counter is below the quota so two concurrent requests can't both pass at `used = quota - 1`.
- `refund_vision_quota(p_user_id UUID)` — atomic decrement, floored at 0. The cloud API calls this when Modal returns a transient error (502/504) so a flaky cold start doesn't burn the student's term budget.
- `reset_vision_quota_used()` — convenience RPC for term-end resets (service-role only).
- All three RPCs revoke EXECUTE from PUBLIC/anon/authenticated and grant only to `service_role`.
- The cloud API endpoint `POST /vision/detect` (rate-limited 5/60s per user — see §7.4) hard-fails with 503 if the `consume_vision_quota` RPC isn't deployed yet, so the operator notices instead of silently downgrading to a non-atomic path.

All migrations have rollbacks under `supabase/rollback/` (BEGIN/COMMIT-wrapped). 010 rollback restores the 006-version body. The Roboter Studio bundle has matching `docs/deploy/APPLY_MIGRATIONS.sql` (forward) and `docs/deploy/ROLLBACK_MIGRATIONS.sql` (reverse-order rollback of 015/016/017).

### Dataset reconciliation sweep (services/dataset_sweep.py)

The React app POSTs `/datasets` immediately after a successful HF upload so group siblings can see it within seconds (`physical_ai_manager/src/hooks/useRosTopicSubscription.js:497-525`). When that POST fails (WSL has no internet at upload time, brief Cloud API outage), without the sweep the dataset would be uploaded to HF but never registered — **siblings would never see it**. The sweep is the safety net: every `DATASET_SWEEP_INTERVAL_S` (default 600s) it scans HF for known authors derived from `trainings.dataset_name` / `trainings.model_name` / `datasets.hf_repo_id`, lists each author's HF datasets, and inserts any missing rows with `discovered_via_sweep=TRUE`. Group attribution at sweep time uses the author's *current* `users.workgroup_id` (matches a manual late-registration). The loop is started from `app/main.py:_start_dataset_sweep` only when `HF_TOKEN` is set; disable explicitly via `DATASET_SWEEP_DISABLED=1`. Single-tenant by design: Cloud API runs `uvicorn --workers 1`, so spawning the loop once at startup is correct. If workers are ever raised, switch to a Postgres advisory lock.

### 9.18 Bootstrap admin (run once)
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
- Healthcheck: asserts `/joint_states` is being published AND each configured camera's `/{name}/image_raw/compressed` topic exists (`bash -c '... | grep -q /joint_states && ([ -z $$CAMERA_DEVICE_1 ] || ... grep -q /{name}/image_raw/compressed) && (same for camera 2)'`). The `[ -z ]` guards let single-camera setups stay healthy. Audit F7 widened this — pre-F7 the healthcheck only checked `/joint_states`, so a dead `usb_cam` container reported healthy, `physical_ai_server` started, recording proceeded with black frames, and the failure surfaced only on the first record-press via the 5s topic-wait timeout. Interval 10s, timeout 5s, retries 3, start_period 120s.

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

Coco classes file is staged from `physical_ai_tools/physical_ai_server/physical_ai_server/workflow/coco_classes.py` to `physical_ai_manager/_coco_classes.py` so the `prebuild` Jest hook (`src/components/Workshop/blocks/__tests__/objectClasses.sync.test.js`) can run inside the Docker build context. **The same staging is required for Railway teacher-web deploys** — use `physical_ai_manager/scripts/railway-deploy.sh` instead of bare `railway up`; it stages, deploys with `--path-as-root .`, and cleans up. The Jest test itself now gracefully skips (with a `console.warn`) when neither candidate path resolves, so a build context without staging won't fail in a confusing way — but it also won't enforce dropdown↔server sync, so always run a build path that DOES stage for production images.

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
- `package.json` v0.9.0; key deps: React 19.1.0, Redux Toolkit 2.8.2, `@supabase/supabase-js` 2.49.8, Blockly 12.5.0, react-blockly 9.0.0, ROSLIB 1.4.1, Recharts 2.13.0, react-hot-toast 2.5.2, Tailwind 3.4.17.
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
10s default timeout (`/get_registered_hf_user`, `/calibration/preview` are 3s). 31 services bound across recording, training, Hugging Face control, calibration, workshop authoring, and the Phase-2/3 debugger:
- **Recording / task control**: `/task/command` (SendCommand), `/training/command` (SendTrainingCommand), `/image/get_available_list`, `/get_robot_types`, `/set_robot_type`.
- **Hugging Face account**: `/register_hf_user`, `/get_registered_hf_user` (3s).
- **Training metadata**: `/training/get_user_list`, `/training/get_dataset_list`, `/training/get_available_policy`, `/training/get_model_weight_list`, `/training/get_training_info`.
- **Dataset editing**: `/browse_file`, `/dataset/edit`, `/dataset/get_info`, `/huggingface/control`.
- **Calibration wizard** (Roboter Studio Phase-2): `/calibration/start`, `/calibration/capture_frame`, `/calibration/capture_color`, `/calibration/solve`, `/calibration/cancel`, `/calibration/status`, `/calibration/auto_pose`, `/calibration/execute_pose`, `/calibration/preview` (3s), `/calibration/verify`, `/calibration/history`.
- **Workshop runtime**: `/workflow/start` (`StartWorkflow.srv` — carries `cloud_vision_enabled` and `auth_token` for JWT propagation, audit F4), `/workflow/stop`, `/workshop/mark_destination` (one-shot AprilTag/colour destination pin).
- **Block-level debugger** (Phase-2): `/workflow/pause`, `/workflow/step`, `/workflow/continue`, `/workflow/set_breakpoints` (`WorkflowSetBreakpoints.srv`, takes a list of Blockly block IDs — used by `DebugPanel.jsx`).

`.srv` definitions live in `physical_ai_interfaces/srv/`. CI's `interfaces-validate` job enforces one `---` per `.srv` and that every name in `CMakeLists.txt` actually exists on disk.

### 12.9 Sidebar tabs (StudentApp)
Labels (German): **Start, Aufnahme, Training, Inferenz, Daten, Roboter Studio**. Internal page enum (`constants/pageType.js`): `HOME, RECORD, INFERENCE, TRAINING, EDIT_DATASET, WORKSHOP`.

`hardwareOnly` tabs filtered in cloud-only mode: RECORD, INFERENCE, EDIT_DATASET, WORKSHOP.

### 12.10 nginx configs
- Student (`nginx.conf`): cache-bust `/index.html` and `/version.json` (`Cache-Control: no-store`); `/static/` immutable 1y; SPA fallback `try_files $uri /index.html`.
- Web (`nginx.web.conf.template`): same caching + 5 strict security headers on **every** location (HSTS 2y, X-Frame-Options DENY, X-Content-Type-Options nosniff, Referrer-Policy strict-origin-when-cross-origin, Permissions-Policy denying camera/mic/geo/payment). Listens on `${PORT}` (Railway).

### 12.11 Constants files
`src/constants/` contains: `pageType.js`, `taskPhases.js` (READY=0, WARMING_UP=1, RESETTING=2, RECORDING=3, SAVING=4, STOPPED=5, INFERENCING=6), `trainingCommand.js` (NONE=0, START=1, FINISH=2), `taskCommand.js` (NONE=0, START_RECORD=1, START_INFERENCE=2, STOP=3, NEXT=4, RERECORD=5, FINISH=6, SKIP_TASK=7), `commands.js` (EditDatasetCommand: MERGE=0, DELETE=1), `HFStatus.js` (Idle/Uploading/Downloading/Deleting/Fetching/Processing/Success/Failed), `paths.js` (workspace + dataset + policy paths), `appMode.js`, **`streamConfig.js`** (`STREAM_QUALITY = 70` — single source of truth for the MJPEG `?quality=` parameter; pre-F35, `CameraFeedOverlay` used 70 and `ImageGridCell` used 50 and the difference manifested as wildly different bitrates on the two recording previews; the constant lets a teacher tune for school Wi-Fi).

### 12.12 F1-F61 audit-comment convention

The 2026-05 camera-pipeline audit (`CAMERA_PIPELINE_FIXES.md`) landed dozens of frontend fixes as inline edits with `// Audit F##` markers rather than new files. The markers are load-bearing — they explain *why* a piece of code looks the way it does:
- `CameraFeedOverlay.jsx` — F24 (rosbridge liveness ping + frozen badge, `FROZEN_THRESHOLD_MS = 2000`), F25 (`reloadKey` re-mount on freeze).
- `ImageGridCell.js` — F26 (cancel-token race fix replacing the prior `isCreatingRef` bug — explained in the comment).
- `StudentApp.js` — F27 (`signOut()` no longer tears down the rosbridge connection on the way out — leaks were stranding the next student's session in a "connecting…" state).
- `Workshop/blocks/toolbox.js` — F28 (toolbox gates `edubotics_detect_open_vocab` on `cloudVisionEnabled` so the block is invisible until the teacher opts in).
- `store/workshopSlice.js` — F29 (`cloudVisionEnabled` and the open-vocab quota chip data persist to localStorage so a reload doesn't drop the opt-in).
- `Workshop/RunControls.jsx` — F30 (`VisionQuotaChip` reads `vision_quota_per_term` / `vision_used_per_term` from `/me`; F31 confidence-on-bbox label; cloud-burst success/error toasts via `react-hot-toast`).
- `Workshop/blocks/perception.js` — F33 (`OPEN_VOCAB_PROMPT_MAX = 80` chars / ≤8 prompts validator).

When editing any of these files, **preserve the audit markers** — they are the project's record of why the code resists obvious-looking simplifications.

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
4. **If the file isn't yet in the overlay chain**, also add a `COPY overlays/<file> /tmp/overlays/<file>` block AND an `apply_overlay <file> "<path_filter>"` call in the Dockerfile's apply_overlay RUN — without both, the source edit lives in the repo but never reaches the image (F62 had this exact bug in the 2026-05-15 deploy: `lerobot_dataset_wrapper.py` was modified in `physical_ai_tools/...` but never overlaid, so prior pushes shipped upstream baseline).
5. Build: `cd robotis_ai_setup/docker && SUPABASE_URL=... SUPABASE_ANON_KEY=... CLOUD_API_URL=... ./build-images.sh`. Watch for `Overlaid: <path> (<before> -> <after>)`.
6. **On Docker Desktop (macOS/Windows), use `docker buildx build --push` directly**, NOT `./build-images.sh` followed by `docker push`. Docker Desktop has two image stores (BuildKit's containerd-snapshotter vs the classic daemon store); `docker build` writes to the containerd store while `docker push` reads from the daemon store, so a successful build + "push" cycle can silently upload a STALE image. `buildx --push` writes straight to the registry and bypasses the daemon entirely. See [§13.4.bis](#134bis-docker-desktop-build-push-gotcha) below.
7. **Full pipeline smoke**: bring up containers with hardware, record one episode, train it, run inference. Type-check / unit tests don't catch UX regressions.
8. If you added German error strings, double-check grammar (use `ä ö ü ß` literally).
9. **Post-push verification is mandatory**: after `docker push` succeeds, run `docker pull --platform linux/amd64 nettername/<image>:latest && docker run --rm --platform linux/amd64 --entrypoint bash nettername/<image>:latest -c "grep -c '<your-audit-marker>' /root/ros2_ws/.../<file>"` and confirm the count is non-zero. The 2026-05-15 deploy had a successful "All images built and pushed!" print but the registry-side `physical-ai-server:latest` still lacked F62/F65/F66 — without this post-push grep we'd have told ourselves it was deployed when it wasn't.

### 13.4.bis Docker Desktop build-push gotcha (read before any image deploy from macOS/Windows)

Docker Desktop ≥4.x runs two parallel image stores:
- **classic daemon store** — what `docker push <tag>` reads from, what `docker images` lists by default, what `docker run` falls back to.
- **containerd-snapshotter store** — what `docker buildx build` (under the `desktop-linux*` builder) writes its output into.

A plain `docker build -t foo/bar:latest .` on Docker Desktop puts the image in the containerd store. A subsequent `docker push foo/bar:latest` reads from the daemon store, which may still hold a stale `:latest` from a previous build. The push appears to succeed (you'll see "Pushed" for each layer) but uploads the OLD image. This is the failure mode that caused F62/F65/F66 to be "deployed" but missing on first try on 2026-05-15.

**Workarounds**, in order of preference:
1. `docker buildx build --platform linux/amd64 --push -t <registry>/<image>:latest -f <Dockerfile> <context>` — builds in containerd, pushes directly to the registry. Bypasses the daemon store entirely. **This is what we use today.**
2. `docker buildx build --load ...` then `docker push ...` — `--load` copies the containerd build output into the daemon store so the subsequent push reads the right content. Slower (extra copy) but matches the `build-images.sh` script shape.
3. Run the build on a real Linux machine where Docker only has one image store. The `build-images.sh` header note "Run by MAINTAINER on a Linux machine, NOT by students" exists for exactly this reason.

`build-images.sh` itself still uses `docker build` + `docker push` because it was authored for the Linux-server path. If/when this script ever needs to run reliably from a Mac, swap the two `docker build` calls for `docker buildx build --push` (and remove the separate push loop).

### 13.5 Debug
| Symptom | Where to look first |
|---|---|
| `dockerd doesn't start in WSL2` | `start-dockerd.sh` (PATH re-export); `wsl -d EduBotics -- tail -n 50 /var/log/dockerd.log` |
| `Multi-layer pull corrupts` on large image | confirm `daemon.json` has `containerd-snapshotter: false`; confirm Docker pin is 27.5.1 |
| `s6 service silently disabled` (server starts but no ROS) | `.s6-keep` mount missing in compose |
| `s6 rejects longrun\r` | CRLF in service file; Dockerfile sed strip ran? |
| `Modal worker uses wrong torch (cu130)` | re-deploy with `index_url=...whl/cu121` and `--force-reinstall` |
| `Inference fails with KeyError on observation.images.X` | Camera connected with name not in policy config — F10 extra-camera filter only drops EXTRAS; missing cameras propagate as upstream `KeyError` and end the session |
| `Empty JointTrajectory crashes recording with IndexError` | Leader-arm not publishing; upstream behavior after the German wrapper was dropped in the 2026-05 stripdown |
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

Five sources of truth (verified in sync at 2026-05-15):
- `Testre/VERSION` → **`2.2.4`** (read by `gui/app/constants.py:APP_VERSION`)
- `installer/robotis_ai_setup.iss AppVersion` → **`2.2.4`**
- `physical_ai_tools/physical_ai_manager/package.json version` → **`0.9.0`** (informational React package version, NOT the product version)
- `docker/versions.env IMAGE_TAG` → file does NOT exist in the repo (gitignored or never created); GUI/installer fall back to `:latest`
- HTTP `/version.json buildId` (UTC timestamp + git SHA, computed at build time)

**Rule**: When bumping product version, hit VERSION, .iss, and the GUI fallback constant (`gui/app/constants.py`) in the same change. The 2.2.2 → 2.2.3 drift was closed by commit `e7f3fcb`. The 2.2.3 → 2.2.4 bump on 2026-05-15 (this session) shipped the auto-pull-on-start hardening — see §6.2 for the new freshness banner and the `is_dockerhub_reachable` / `_get_remote_manifest_digest` / `_save_last_pull_info` helpers in `docker_manager.py`. Bumping `VERSION` is what arms the existing `/version`-poll update gate, prompting students with stale `.exe` installs to re-install when their GUI next launches; without the bump, the new auto-pull code never reaches their machine.

---

## 15. CI workflow (`.github/workflows/ci.yml`) — what fails the build

10 jobs run on every push/PR to `main` (in the order they appear in `ci.yml`):
1. **python-tests** — `compileall` of `gui`, `scripts`, `cloud_training_api`, `modal_training`, overlays, patches; `unittest discover -s tests` (5 GUI/installer tests + the F63/F64 CLI-default lock-in suite in `tests/test_training_handler_cli.py` — stubs `huggingface_hub` + `supabase` so the test runs without those deps + the F67/F68/F69 auto-pull suite in `tests/test_docker_auto_pull.py` covering offline detection, manifest-digest parsing, last-pull persistence, and check-for-updates orchestration short-circuits — cross-platform, mocks subprocess + socket so it runs on Linux/macOS CI without needing a real Docker or network); plus `unittest discover -s app/tests` from `cloud_training_api/` (workgroup helper, dataset sweep parsers — tests stub fastapi/supabase/huggingface_hub via `sys.modules` so they run without those deps).
2. **shell-lint** — shellcheck `-S error` on `build-images.sh`, `entrypoint_omx.sh`, `build_rootfs.sh`, `start-dockerd.sh`, `physical_ai_manager/scripts/railway-deploy.sh`.
3. **compose-validate** — `docker compose config` on both base + GPU compose with fake `.env`.
4. **overlay-guard** — runs `fix_server_inference.py` on a fake `server_inference.py` that lacks the patch target; asserts non-zero exit (catches a regress where the patch silently fails).
5. **modal-import-validate** — pip-installs the Modal SDK, then imports `modal_app.py` and `vision_app.py`. (`training_handler.py` was dropped from this sweep in commit `1b68372` because it imports container-only deps like `lerobot`, and the sweep was failing on import-time symbols rather than the Modal-SDK contract it actually exists to validate.) Image specs are evaluated at module load, so an SDK API mismatch (e.g. `Image.pip_install(force_reinstall=True)` against an SDK that wants `extra_options="--force-reinstall"`) raises `TypeError` here instead of at `modal deploy` time. Added after the `c56c012` vision_app.py near-miss.
6. **teacher-web-build-validate** — builds `Dockerfile.web` with **only `physical_ai_manager/`** as the build context (exactly what `railway up --path-as-root .` does), with placeholder secrets, then asserts each secret reached the bundle. Catches build-context regressions that wouldn't surface in `manager-build-validate` (which builds the student Dockerfile with `build-images.sh`-style staging).
7. **manager-build-validate** — builds `physical_ai_manager` (student `Dockerfile`) with placeholder secrets (`CI_VALIDATE.supabase.co`, `CI_VALIDATE_ANON_KEY`, `CI_VALIDATE.api.example`); asserts each placeholder string appears in the built `main.*.js` bundle. Catches the white-screen regression. Stages `_coco_classes.py` so the prebuild Jest hook enforces dropdown↔server sync.
8. **tutorials-validate** — JSON-parses every `physical_ai_manager/public/tutorials/*.json`, asserts the required schema (`id`, `title_de`, `level`, `steps[].title`, `steps[].body`, `steps[].allowed_blocks`), and cross-checks each `allowed_blocks` entry against `cloud_training_api/app/validators/workflow.py:ALLOWED_BLOCK_TYPES`. A tutorial referencing a block that doesn't exist server-side fails the build.
9. **interfaces-validate** — verifies every `.srv` has exactly one `---` separator and every `.srv`/`.msg` filename listed in `CMakeLists.txt` is present on disk; runs on all of `physical_ai_interfaces/srv/*.srv` and `physical_ai_interfaces/msg/*.msg`.
10. **nginx-validate** — `envsubst $PORT` on `nginx.web.conf.template` then `nginx -t` on both web + student configs.

### Boot-time schema fingerprint (Cloud API)

`cloud_training_api/app/main.py:_validate_required_schema()` runs at module load and probes every table + RPC the routes touch (workflow_versions, tutorial_progress, vision quota columns + RPCs, etc.). If the live Supabase schema is behind the on-disk migrations, Railway aborts the deploy with a named cause instead of returning 200 from `/health` and crashing on first student request. Override with `EDUBOTICS_SKIP_SCHEMA_CHECK=1` for unit-test contexts only. This is the systemic fix for the c56c012 round-3 incident where 017's `refund_vision_quota` RPC was missing from the live DB even though the migration file on disk defined it.

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
- **`POLICY_MAX_TIMEOUT_HOURS`** — per-policy timeout caps applied after request validation but before Modal dispatch (ACT/VQBET/TDMPC 4h, Diffusion/Pi0Fast 6h, Pi0/SmolVLA 10h; outer `MAX_TRAINING_TIMEOUT_HOURS=12` envelope and the Modal `7h` function timeout still apply).
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
- **F1-F61** — the 2026-05 deep-audit fix bundle (`CAMERA_PIPELINE_FIXES.md`). Inline `// Audit F##` markers in source code refer to specific findings; preserve them when editing. F1-F4 were the 4 CRITICALs; F11-F12 are inference-time safety fixes (velocity-cap seeding, shape-mismatch reject); F37-F45 are the vision_app.py reliability bundle (FP16 on T4, EXIF-before-RGB, score threshold 0.25, `huggingface_hub` pin, etc.).
- **F67-F69** — the 2026-05-15 auto-pull GUI 2.2.4 bundle in `robotis_ai_setup/gui/app/docker_manager.py`. **F67**: `is_dockerhub_reachable()` — 5 s TCP probe to `registry-1.docker.io:443` short-circuits `check_for_updates()` when offline, skipping the ~12 min worth of per-image retry storms on a disconnected classroom network. **F68**: manifest-digest pre-check — `_get_remote_manifest_digest()` parses `docker manifest inspect` output and picks the `linux/amd64` entry; compared against `_get_local_repo_digest()` (reads `RepoDigests`); when they match the per-image pull is skipped entirely, cutting steady-state startup from ~30 s of full pulls to ~3 s of HEAD requests. **F69**: last-pull persistence — `_save_last_pull_info()` writes `%LOCALAPPDATA%/EduBotics/.last_image_pull.json` with timestamp + per-image digests; `get_last_pull_status()` exposes age + staleness to the GUI which surfaces "Image-Frische: letzter Update vor X Tagen" with a ⚠️ banner past `IMAGE_FRESHNESS_WARN_DAYS=14`. All three layers regression-protected by 20 cross-platform tests in `tests/test_docker_auto_pull.py`. Disable the auto-pull entirely with `EDUBOTICS_SKIP_AUTO_PULL=1` for offline classrooms. The VERSION + .iss + GUI fallback bump 2.2.3 → 2.2.4 is what arms the update gate to push the new `.exe` to existing student installs.
- **F62-F66** — the 2026-05-15 ACT-quality + camera-sync bundle. **F62**: ffmpeg preset `ultrafast→fast` + CRF 28→23 in `lerobot_dataset_wrapper.py:_create_video` — cleaner training inputs at ~1.5-1.8× disk. **F63**: `--dataset.image_transforms.enable=true` always-on in `training_handler._build_training_command` — LeRobot's built-in brightness/contrast/saturation/hue/sharpness jitter (max 3 per frame, weighted) closes classroom-lighting distribution shift. **F64**: ACT-only default `--policy.n_action_steps=15` injected by the same builder (overrideable by `training_params['n_action_steps']`) — re-query every 0.5 s instead of 3.3 s open-loop; biggest single inference-smoothness lever. **F65**: `_pick_synced_camera_msgs` in `overlays/communicator.py` + per-camera (`stamp_ns`, msg) ring (`_CAMERA_SYNC_HISTORY=8`) pairs multi-camera frames within `_CAMERA_SYNC_SLOP_NS=15_000_000` (15 ms) before `get_latest_data()` returns — fixes silent gripper↔scene drift that the policy was learning. Falls back to latest-per-camera on single-camera / `stamp_ns=0` / no-match. **F66**: same-day post-verifier hardening — wraps the F65 deque snapshot in a 3-attempt `RuntimeError`-catching retry (CPython's deque iterator raises on concurrent `append()`, and `physical_ai_server.py` runs a `MultiThreadedExecutor(num_threads=3)`); clears the F65 rings on `clear_latest_data()` so re-record episodes don't pair fresh+stale msgs; gates F64 override on `model_type=='act'` to close the cross-policy leak; validates the override as `isinstance(int) and >0` and falls back to the F64 default for `None`/`0`/negative/non-int. The F63/F64/F66 CLI behaviour is regression-protected by `robotis_ai_setup/tests/test_training_handler_cli.py` (7 tests covering all 7 `ALLOWED_POLICIES`, cross-policy leak, invalid-override fallback, exactly-once emission count).
- **Cloud-burst** — the open-vocabulary-detection path that POSTs a camera frame from the on-robot perception pipeline to Modal's OWLv2 (`POST /vision/detect`). Always opt-in (`StartWorkflow.srv.cloud_vision_enabled`). Local synonym dict is consulted first; the burst is the fallback. Falsey on cost (~$0.00007/call warm).
- **VisionQuotaChip / vision_quota_per_term / vision_used_per_term** — per-user term budget for cloud-burst calls. `consume_vision_quota` RPC is atomic test-and-increment; `refund_vision_quota` is the atomic decrement triggered on transient Modal 502/504s so a flaky cold start doesn't burn a student's term.
- **Frozen badge** — the React indicator (`CameraFeedOverlay.jsx`, audit F24) shown when the rosbridge liveness ping hasn't seen a topic update in `FROZEN_THRESHOLD_MS = 2000` ms. The image element is re-mounted (`reloadKey`) on freeze so a stuck `<img>` reconnects automatically.
- **STREAM_QUALITY** — `physical_ai_manager/src/constants/streamConfig.js` constant (default `70`) — single source of truth for the MJPEG `?quality=` parameter; pre-F35, `CameraFeedOverlay` and `ImageGridCell` disagreed (70 vs 50).

---

## 17. When in doubt

The single source of truth is always **the code**. This file describes what's true at the time it was written. Verify against `git log` and the current file when stakes are high. If you find this file disagrees with the code, fix this file in the same change.

**You are an autonomous coding partner.** Do the work, fix the failures at the root cause (never `--no-verify`, never `@pytest.skip`, never bypass an `apply_overlay` assertion that's telling you upstream renamed something). When you change anything that this file describes, update this file. The whole point of this file is that it stays in sync.

When the user says something destructive or you're about to take a destructive action, **stop and ask** — no one's training schedule is so urgent that an unwanted `wsl --unregister` is acceptable.

---

## 18. Deferred work & audit references

This section is the index of work that is **documented but not yet done**, so future sessions don't reopen settled tradeoffs and don't claim "complete" something the team has already flagged as deferred. The companion files are:

### 18.1 `CAMERA_PIPELINE_FIXES.md` — the F1-F61 deep audit

The 2026-05 camera-pipeline audit listed 61 findings (F1-F61) ranked CRITICAL/HIGH/MEDIUM/LOW. Commit `1b68372` ("Camera pipeline: deep-audit fix bundle (F1-F61)") landed all 4 CRITICALs and the bulk of the HIGH/MEDIUM tier; only **F16, F19, F42, F43** are intentionally deferred (documented low-priority items — see the file's status table). When you see a `// Audit F##` marker in source, that line of code exists *because* of the corresponding finding; preserve the marker and read the rationale in `CAMERA_PIPELINE_FIXES.md` before simplifying.

The fix bundle introduced these load-bearing behaviors (already in code, recapped here so future sessions don't accidentally roll them back):
- **Inference safety**: first-tick velocity-cap seeding from current joints (F11), shape-mismatch tick rejection (F12), single-frame fault tolerance, consecutive-skip escalation (F13).
- **Camera pipeline**: `bgr8` cv_bridge encoding (F5), per-tick frame-shape lock validation (F6), camera-topic healthcheck assertion (F7), camera-role enforcement in config_generator (F8), `/dev/v4l/by-id` stable paths (F20), entrypoint `wait_for_camera` (F21), per-camera launch params (F22).
- **Recording integrity**: camera msg arrival + observed-Hz check at recording start (F17/F18); canonical LeRobot v2.1 video path derivation when encoders dict empty (F23).
- **Browser UX**: rosbridge liveness ping + frozen badge (F24), img re-mount on freeze (F25), ImageGridCell race fix (F26), no-blunt-teardown on `signOut()` (F27), toolbox cloud-vision gating (F28), persistence (F29), VisionQuotaChip (F30), bbox confidence label (F31), open-vocab prompt validator (F33), cloud-burst toasts, `STREAM_QUALITY` constant (F35), web_video_server loopback bind.
- **Modal vision worker**: error-field logging, `@app.cls(timeout=120)` widened from 30s (F37), EXIF-before-RGB (F39), `device_map=None` (F40), FP16 on T4 (F41), `huggingface_hub==0.26.2` pin (F44), `score_threshold=0.25` (F45).
- **Cloud API + Supabase**: clean German exception messages, RPC error-code classification, NULL-quota skip (F48), `/admin` + `/teacher` vision-quota PATCH endpoints (F49), bbox length validation, fail-closed on unknown RPC shape, refund logging level bump.
- **On-host bridge**: `enabled` flag honoured (F54), YAML synonym overlay loader, `NotImplementedError` clean re-raise, `should_stop` before burst (F57), 2s scene-frame freshness guard (F58).

### 18.2 `docs/ROBOTER_STUDIO_DEFERRED.md` — Phase-2/3 unfinished work

A 31-item catalogue from the round-3 Roboter Studio audit, organized as:
- §1.1-§1.7 **Backend wiring** — ROS service stubs for the block-level debugger and parts of the calibration UX. **Items §1.1, §1.3, §1.4 are now wired** (the debugger pause/step/continue/set_breakpoints services are real, SensorSnapshot's perception fields populate from `_workflow_last_detections`, and cloud_vision_enabled propagation is hooked). **§1.2 (CalibrationPreview / Verify / History) are still stubs** — those services return German "wird in einer späteren Version aktiviert" so the React UI shows a polite message instead of a service-not-found error. **§1.5 (frame quality scoring on capture)** and **§1.6 (calibration history dir + pruning)** remain open.
- §2.1-§2.14 **Frontend subscriptions** — small leaks + audio polish.
- §3-§7 Tutorial polish, cloud hardening, A11y, code hygiene, tooling gaps.
- §8 **Recommended order**: 1.1-1.3 (ROS handlers) → 1.4 (cloud_vision wiring — partially landed by F4) → 2.1-2.2 (audio + memory leaks). Anything else is polish.

Treat this file as the next-sprint backlog. Do not promise "Phase-2 debugger" features without checking which items in §1 are still open.

### 18.3 `docs/deploy/` — deployment ordering (load-bearing)

**`APPLY_MIGRATIONS.sql`** + **`ROLLBACK_MIGRATIONS.sql`** are the forward and reverse bundles for the Phase-2/3 trio (`015_workflow_versions.sql`, `016_tutorial_progress.sql`, `017_vision_quota.sql`). The boot-time `_validate_required_schema()` in `cloud_training_api/app/main.py` probes every table + RPC the routes touch, so Railway will abort the deploy if the migrations haven't landed first (this is the systemic fix for the c56c012 round-3 incident where `refund_vision_quota` was missing from the live DB even though the file on disk defined it).

**`DEPLOY.md`** is the single one-page reference covering Supabase, Modal, Railway, Docker Hub, and git push. The golden order is:
1. **Supabase migrations first** (`APPLY_MIGRATIONS.sql` via Studio SQL editor) — new RPCs and tables must exist before the cloud API references them.
2. **Modal redeploy** of `vision_app.py` / `modal_app.py` — must be done before any student opens the open-vocab block.
3. **Railway redeploy** of the cloud API — picks up new env vars + schema fingerprint.
4. **Docker images rebuild + push** via `build-images.sh`.
5. **Git push** of any pending source changes (CI runs guardrails).

Skipping or reordering any of these has been the cause of every "the new feature is live in code but broken in production" report so far. When in doubt, read `DEPLOY.md`.

### 18.4 `robotis_ai_setup/CHANGES_SESSION_*.md` — historical context

`CHANGES_SESSION_2026-04-06.md` and `CHANGES_SESSION_2026-04-17.md` are **history, not procedure** — they explain *why* the project has its current shape (the WSL2 + bundled-distro decision, the `is_async=true` xacro overlay pattern, the healthcheck-driven container-ordering choice). The fixes they describe are in the current images; the files are kept so a future architect can audit the reasoning before reverting a load-bearing decision. Don't act on their procedural sections — those have been superseded by the current CLAUDE.md.

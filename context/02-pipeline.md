# 02 — End-to-End Pipeline

> **What this file is:** the deep narrative — trace one student's workflow from install → record → train → inference, with file:line references at every step.
> Read [`01-architecture.md`](01-architecture.md) first for the big picture. Then come here when you need to understand HOW each stage actually works.

This is a behavioral document. For source-level code reference of any layer, read its dedicated file (10-cloud-api, 11-modal-training, …).

---

## Stage 0 — Stage map

| # | Stage | Owner | Layer file |
|---|---|---|---|
| 1 | Windows installer + WSL2 rootfs | Our code | [`16-installer-wsl.md`](16-installer-wsl.md) |
| 2 | Windows tkinter GUI (`EduBotics.exe`) | Our code | [`14-windows-gui.md`](14-windows-gui.md) |
| 3 | Robot-arm connection (ROS2 + Dynamixel) | ROBOTIS upstream + our overlay | [`17-ros2-stack.md`](17-ros2-stack.md) |
| 4 | Docker Compose (3 containers) | Our code | [`15-docker.md`](15-docker.md) |
| 5 | Dataset recording (LeRobot v2.1) | ROBOTIS upstream + our overlays | [`17-ros2-stack.md`](17-ros2-stack.md) + [`15-docker.md`](15-docker.md) |
| 6 | React SPA (student + web) | ROBOTIS upstream (hacked) | [`13-frontend-react.md`](13-frontend-react.md) |
| 7 | Cloud training (Railway + Modal + Supabase) | Our code | [`10-cloud-api.md`](10-cloud-api.md), [`11-modal-training.md`](11-modal-training.md), [`12-supabase.md`](12-supabase.md) |
| 8 | Inference (load policy → drive arm) | ROBOTIS upstream + our overlays | [`17-ros2-stack.md`](17-ros2-stack.md) |

---

## Stage 1 — Windows installer

**Goal:** student double-clicks `EduBotics_Setup.exe`, ends up with Docker running inside a bundled Ubuntu 22.04 WSL2 distro. **No Docker Desktop.**

### Inno Setup orchestration

- Entry: `robotis_ai_setup/installer/robotis_ai_setup.iss` (~208 MB `.exe`, AppID `{B7E3F2A1-8C4D-4E5F-9A6B-1D2E3F4A5B6C}`, v2.2.2, `PrivilegesRequired=admin`).
- Ships in `installer/assets/`:
  - `edubotics-rootfs.tar.gz` (~193 MB gzipped Ubuntu rootfs)
  - `edubotics-rootfs.tar.gz.sha256` (sidecar, verified before import)
  - `EduBotics.exe` (PyInstaller GUI, ~5.8 MB)
  - license + icon
- Install dir: `C:\Program Files\EduBotics\{gui,scripts,docker,wsl_rootfs,icon.ico}`
- User-writable `.env`: `%LOCALAPPDATA%\EduBotics\.env` (moved out of Program Files in v2.2.1 to avoid UAC on regen).

### 7-step PowerShell chain (in order)

1. **`migrate_from_docker_desktop.ps1`** — silent uninstall Docker Desktop; unregister `docker-desktop` + `docker-desktop-data` WSL distros; remove Docker Desktop run-key. Idempotent via `.migrated` marker.
2. **`install_prerequisites.ps1`** — verify Win11 ≥ build 22000, reject Home edition, check Hyper-V; `wsl --install --no-distribution`; download + install `usbipd-win` 5.3.0 MSI with **SHA256 verification** (`UsbipdSha256` constant pinned in `.iss`). On WSL kernel install, drops `.reboot_required` marker.
3. **`configure_wsl.ps1`** — merge `memory=8GB, swap=4GB` into the user's `~/.wslconfig` (resolves real logged-in user via `Win32_Process.GetOwner()` on `explorer.exe`, since elevated PS would resolve to admin).
4. **`configure_usbipd.ps1`** — `usbipd policy add` for ROBOTIS VID `2F5D` and PIDs `0103` (OpenRB-150) and `2202`. Handles usbipd 4.x/5.x API drift (`--operation AutoBind` is 5.x-only).
5. **`import_edubotics_wsl.ps1`** — preflights 20 GB free disk, verifies tarball SHA256 against sidecar, `wsl --unregister EduBotics` (if present), `wsl --import EduBotics %ProgramData%\EduBotics\wsl assets\edubotics-rootfs.tar.gz --version 2`, polls `docker info` up to 180 s, falls back to manual `start-dockerd.sh`.
6. **`pull_images.ps1`** — pulls 3 `nettername/*` images via `wsl -d EduBotics -- docker pull`. Skipped on `.reboot_required`. UTF-16LE NUL-handling on `wsl --list` output.
7. **`verify_system.ps1`** — post-install: distro present, dockerd up, usbipd installed, images present, NVIDIA driver, install dir files.

### Post-reboot path

If `.reboot_required` exists, Inno's `NeedRestart()` returns true → user reboots → on next GUI launch the GUI detects the marker and re-launches `finalize_install.ps1` with UAC elevation, which deletes `.reboot_required` and runs steps 5+6.

### Bundled WSL2 rootfs (`wsl_rootfs/`)

- `build_rootfs.sh` → `docker build -t edubotics-rootfs:latest . && docker export | gzip -9 > installer/assets/edubotics-rootfs.tar.gz` + SHA256 sidecar.
- `Dockerfile`: `ubuntu:22.04` base; pins **Docker 27.5.1 + containerd 1.7.27** (Docker 29.x's containerd-snapshotter corrupts multi-layer pulls on WSL2 custom rootfs; 29+ removed the disable flag). Installs `tzdata` and sets `Europe/Berlin` — `/etc/timezone` and `/etc/localtime` must exist as **files** (not dirs) or compose bind-mounts fail with "trying to mount a directory onto a file".
- `wsl.conf`: `[boot] command=/usr/local/bin/start-dockerd.sh`, `[user] default=root`, `[interop] appendWindowsPath=false`, `hostname=edubotics`. Systemd is explicitly NOT used (unreliable on custom-imported rootfs).
- `daemon.json`: `overlay2`, `nvidia` runtime, `containerd-snapshotter: false`, 10m/3-file log rotation.
- `start-dockerd.sh`: re-exports PATH (WSL boot ctx is empty), `nohup /usr/bin/dockerd …`, plus a **watchdog** that respawns dockerd if it dies (added in session 2026-04-17).

### Uninstall

`[UninstallRun]` in the `.iss`: best-effort `docker compose down` inside distro → `wsl --unregister EduBotics` (clears VHDX → **destroys named volumes** — known-issue [§3.1](21-known-issues.md)). `%LOCALAPPDATA%\EduBotics\.env` is intentionally left behind.

### Non-obvious gotchas

- WebView2 isn't installed by the installer — relies on Windows 11 shipping it.
- The rootfs `.tar.gz` is shipped both inside the installer AND copied to `{app}\wsl_rootfs\` for offline reimports.
- Empty `PATH` at WSL boot is the single most common source of "dockerd doesn't start" bugs.

---

## Stage 2 — Windows tkinter GUI

**Goal:** hardware-setup wizard that detects arms/cameras, generates `.env`, brings up Docker containers, opens React UI in an embedded WebView2 window.

### Entry + dispatch

- `gui/main.py` dispatches on a `--webview` sentinel: default → `gui_app.run()` (tkinter), with sentinel → `webview_window.run_in_process()` (subprocess-only pywebview, owns its own main thread).
- PyInstaller spec at `gui/build.spec` collects pywebview + pythonnet CLR DLLs, hides imports, `console=False`, packs `assets/icon.ico`.
- Version + distro constants in `gui/app/constants.py` (`WSL_DISTRO_NAME=EduBotics`, `APP_VERSION` read from repo `VERSION`, env-var overrides like `EDUBOTICS_WSL_DISTRO`).

### Main window — linear wizard

`gui/app/gui_app.py` (1063 lines):

1. **Cloud-only checkbox** — greys out hardware frames; startup switches from full-stack compose to manager-only.
2. **Arm scan** — daemon thread runs `device_manager.scan_and_identify_arms()`. Every detected ROBOTIS USB port (`VID=2F5D`) is attached via `usbipd attach --wsl --distribution EduBotics --busid X`, then a throw-away `open_manipulator` container runs `identify_arm.py <serial>` to tell leader from follower.
3. **Camera scan** — daemon thread runs `wsl_bridge.list_video_devices()` (iterates `/dev/video*` with `v4l2-ctl --info`). Two-camera role-assignment UI (dropdowns "Greifer-Kamera" / "Szenen-Kamera").
4. **Start/Stop/Browser buttons** — gated by `_update_start_button()`. `_start_environment()` runs off the UI thread: re-attaches USB → polls `/dev/serial/by-id/` → regenerates `.env` → `docker compose up -d` (with `-f docker-compose.gpu.yml` if `nvidia-smi` succeeds) → polls `:80` for HTTP 200 → opens WebView2 subprocess.
5. **Log pane** — thread-safe `_log()` into a `ScrolledText`.

### Docker wrapping (`gui/app/docker_manager.py`)

- Every Docker call goes through `_docker_cmd()` → `wsl -d EduBotics -- docker ...`. There is **no** Docker Desktop dependency.
- `wait_for_docker()` polls `docker info`; at 15 s+ stall it force-invokes `start-dockerd.sh`.
- **Pull stall watchdog** — Docker's `\r`-animated progress bars look idle to line-based readers, so the watchdog monitors stdout-line-rate AND `/var/lib/docker/overlay2` disk growth (10 MB / 20 s). If both stall past `stall_timeout` (600s first pull / 120s updates), it `pkill -KILL dockerd`, restarts, retries with exp backoff. **Main knob for poor-network classrooms.**
- `has_gpu()` calls host `nvidia-smi` — WSL2 forwards the host NVIDIA driver, so distro GPU visibility == host GPU visibility.

### WebView2 subprocess (`gui/app/webview_window.py`)

pywebview 6 requires `webview.start()` on the main thread (which tkinter owns). So `open_student_window(url)` `subprocess.Popen()`s the same `EduBotics.exe` with `--webview --url …` (`CREATE_NO_WINDOW`). A watchdog thread maps non-zero exit (usually missing Edge WebView2) to `_runtime_missing`; `_webview_fallback()` then `webbrowser.open(url)` as graceful degradation.

Cloud-only mode appends `?cloud=1` to the URL so the React app skips the rosbridge gate.

### `.env` generation (`gui/app/config_generator.py`)

- `_resolve_ros_domain_id()`: hashes machine UUID (uuid.getnode() → SHA256 → mod 233) for per-machine ROS_DOMAIN_ID. Falls back to 30. **Mitigates [§2.3 of known-issues](21-known-issues.md).**
- `_atomic_write()`: write to `.tmp` + `os.replace()` — guards against power-loss mid-write.
- Quotes all path values (handles spaces in usernames like "Max Muster").

### UAC &amp; self-updating

- `_elevate_and_wait` uses Win32 `ShellExecuteExW` directly (not PS `Start-Process`), so it gets a real process handle for `WaitForSingleObject`.
- Update gate: on startup, polls Railway `/version`. If newer exists, **blocking** modal forces update; download to `%TEMP%` then `os.startfile`.

### Language

All tkinter strings are German. Code/docstrings/log prefixes are English.

---

## Stage 3 — Robot-Arm Connection (ROS2 Jazzy + Dynamixel)

**Goal:** one leader + one follower OMX arm appear on ROS2 topics inside the `open_manipulator` container.

### Two arms on two serial ports

| | OMX-F follower | OMX-L leader |
|---|---|---|
| Servo IDs | 11–16 | 1–6 |
| Namespace | global | `/leader/` (via `PushRosNamespace`) |
| Control | position, all joints have command iface | joints 1–5 read-only + gravity comp; joint 6 (gripper) current-controlled (Op Mode 5, 300 mA limit) |
| Launch file | `omx_f_follower_ai.launch.py` | `omx_l_leader_ai.launch.py` |
| ros2_control xacro | `omx_f.ros2_control.xacro` | `omx_l.ros2_control.xacro` |
| Hardware plugin | `dynamixel_hardware_interface/DynamixelHardware` @ 1 Mbps Protocol 2.0, update rate 100 Hz |
| Default device | `/dev/ttyACM0` | `/dev/ttyACM2` |

### `identify_arm.py`

`robotis_ai_setup/docker/open_manipulator/identify_arm.py`. Pings IDs 1–6 (leader) and 11–16 (follower) at 1 Mbps Protocol 2.0 over an exclusive serial handle. Returns `"leader"` / `"follower"` / `"unknown"` / `"error:..."`. **Not called by the entrypoint** — exists for the GUI's device scanner. The entrypoint trusts the explicit `FOLLOWER_PORT` / `LEADER_PORT` env vars.

### Entrypoint choreography (`docker/open_manipulator/entrypoint_omx.sh`, ~270 lines)

PID 1 of the container (no systemd). Five phases:

1. **Validate hardware** — wait ≤60 s per port (`wait_for_device()`), `chmod 666 $FOLLOWER_PORT $LEADER_PORT`. Exports `ROS_DOMAIN_ID`.
2. **Launch leader first** — `ros2 launch ... omx_l_leader_ai.launch.py port_name:=$LEADER_PORT`. Wait ≤30 s for `/leader/joint_states`.
3. **Read leader position** — inline Python subscriber reads first complete `/leader/joint_states` (joint1–5 + gripper_joint_1), JSON-encodes positions to a shell variable.
4. **Launch follower + smooth sync** — `ros2 launch ... omx_f_follower_ai.launch.py`. Wait for `/joint_states`. Then publish a **quintic-polynomial** (`s(t) = 10t³ − 15t⁴ + 6t⁵`) 50-waypoint trajectory over 3 seconds to `/leader/joint_trajectory` with explicit velocities + accelerations. After motion: verify follower reached target within 0.08 rad tolerance per joint; **hard-fail on mismatch** (exit 2).
5. **Launch cameras** — up to two `usb_cam` nodes, each driven by `CAMERA_DEVICE_N` + `CAMERA_NAME_N`. Topics: `/{name}/image_raw/compressed`.

`trap` on SIGTERM/SIGINT calls `disable_torque()` then kills launch children. Final `wait` keeps PID 1 alive.

### The magic remap

`omx_f_follower_ai.launch.py:144`:
```python
remappings=[('/arm_controller/joint_trajectory', '/leader/joint_trajectory')]
```

The arm controller's default action topic is **remapped** so anyone publishing to `/leader/joint_trajectory` drives the follower. The follower has no concept of "leader"; the two arms are decoupled, and the entrypoint's sync publish + later inference-node publishes both ride the same rail.

### Compose wiring

`docker-compose.yml`: `privileged: true`, `/dev:/dev`, `/dev/shm:/dev/shm`, ulimits `rtprio=99 rttime=-1 memlock=8GB`, on bridge `ros_net`. Healthcheck on `open_manipulator`: bash sources ROS setup, checks if `/joint_states` topic exists. `physical_ai_server` `depends_on: condition: service_healthy`.

---

## Stage 4 — Docker Containers

**Goal:** three containers (`open_manipulator`, `physical_ai_server`, `physical_ai_manager`) on a `ros_net` bridge, sharing `ROS_DOMAIN_ID`, with host ports `80` / `9090` / `8080` forwarded by WSL2.

### Build chain (`docker/build-images.sh`)

Order:

1. **physical_ai_manager** — React SPA compiled with build args: `REACT_APP_SUPABASE_URL`, `REACT_APP_SUPABASE_ANON_KEY`, `REACT_APP_CLOUD_API_URL`, `REACT_APP_ALLOWED_POLICIES` (student build = `"act"`), `REACT_APP_MODE` (default `student`), `REACT_APP_BUILD_ID` (UTC timestamp + 7-char git SHA).
2. **physical_ai_server** — `docker pull robotis/physical-ai-server:amd64-0.8.2` (immutable pin) → our thin-layer Dockerfile on top.
3. **open_manipulator** — pull `robotis/open-manipulator:amd64-4.1.4` (or `BUILD_BASE=1` to build from source, ~40 min) → thin layer.

All three pushed to `nettername/*`. Push loop verifies success per image; aborts if any fails (no half-updated student set).

### Compose services

| Service | Depends | Ports | Privileged | Healthcheck |
|---|---|---|---|---|
| `open_manipulator` | — | none | yes | `/joint_states` topic |
| `physical_ai_server` | open_manipulator (healthy) | `127.0.0.1:8080`, `127.0.0.1:9090` | yes | TCP connect 9090 |
| `physical_ai_manager` | physical_ai_server (healthy) | `127.0.0.1:80` | no | wget /version.json |

`depends_on: service_healthy` ensures startup order with readiness, not just process started. Ports bound to `127.0.0.1` to keep rosbridge off the school LAN.

`docker-compose.gpu.yml` is a 10-line overlay adding `runtime: nvidia` + GPU device reservation **only** for `physical_ai_server`. GUI picks it based on `nvidia-smi`.

### `physical_ai_server` thin layer — three operations

1. **CRLF strip** — `find /etc/s6-overlay/s6-rc.d -type f ... -exec sed -i 's/\r$//' {} +` — Windows Git CRLF makes s6-overlay reject `longrun\r` as invalid type at runtime.
2. **Patch** — `patches/fix_server_inference.py` regex-patches upstream `server_inference.py` to (a) initialize `self._endpoints = {}` before first `register_endpoint()` and (b) remove the duplicate `InferenceManager` construction. **Self-verifies** — exits 2 or 3 on no-op.
3. **Overlays** — 5 files copied into `/tmp/overlays/` then `apply_overlay()` shell function with **sha256 verification** copies them over upstream paths.

### The `.s6-keep` mystery

`physical_ai_server/.s6-keep` is an empty 1-byte file mounted read-only at `/etc/s6-overlay/s6-rc.d/user/contents.d/physical_ai_server`. s6-overlay enables services by detecting their name as a file in `user/contents.d/`. The base image defines the service but leaves it disabled; the compose mount **is** how it's enabled at runtime. Remove the mount → server container starts but ROS node never runs.

### `.env` contract

Keys compose expects: `FOLLOWER_PORT`, `LEADER_PORT`, `CAMERA_DEVICE_1`, `CAMERA_NAME_1` (default `gripper`), `CAMERA_DEVICE_2`, `CAMERA_NAME_2` (default `scene`), `ROS_DOMAIN_ID`, `REGISTRY=nettername`. Generated by GUI to `%LOCALAPPDATA%\EduBotics\.env`, passed to compose via `--env-file`.

---

## Stage 5 — Dataset Recording (LeRobot v2.1)

**Goal:** an episode → H.264 videos per camera + parquet of state/action + `meta/info.json`, optionally pushed to HuggingFace.

### Trigger

React calls the `/task/command` ROS service (`SendCommand.srv` from `physical_ai_interfaces`) with `command=START_RECORD=1` and a `TaskInfo` payload (task_name, task_instruction[], fps, warmup_time_s, episode_time_s, reset_time_s, num_episodes, push_to_hub, record_rosbag2, use_optimized_save_mode). Routed by `physical_ai_server.py` → `user_interaction_callback()`.

### State machine (`data_manager.py`)

`warmup → run → save → reset → (loop) → finish`. Each phase is time-gated; `TaskStatus.msg` pushes `phase`, `proceed_time`, `current_episode_number`, `encoding_progress` back to the UI.

### Per-tick pipeline (fps typically 30 Hz)

1. `communicator.get_latest_data()` blocks ≤5 s/topic, collecting `/gripper/image_raw/compressed`, `/scene/image_raw/compressed`, `/joint_states` (follower), `/leader/joint_trajectory`.
2. `convert_msgs_to_raw_datas()`:
   - Images: cv_bridge → BGR → `cvtColor(..., BGR2RGB)` → uint8 HWC.
   - Follower: JointState → `joint_state2tensor_array()` reorders per `joint_order` → float32 [6].
   - Leader action: `joint_trajectory2tensor_array()` reads `points[0].positions`, reorders, float32 [6]. **Overlay raises** German error if empty.
3. `create_frame()` assembles `{'observation.images.gripper': ..., 'observation.images.scene': ..., 'observation.state': ..., 'action': ...}`, dtype-cast to float32.
4. `add_frame_without_write_image()` validates vs schema, appends to episode buffer, auto-timestamps as `frame_index / fps` (wall-clock NOT used — assumes constant fps).
5. Video encoding: raw RGB piped to `ffmpeg libx264 -crf 28 -pix_fmt yuv420p`, **async**.
6. `save_episode_without_video_encoding()` writes parquet + mp4 + updates `meta/info.json` (codebase_version `"v2.1"`).

### Disk layout

Inside container: `~/.cache/huggingface/lerobot/{user_id}/{robot_type}_{task_name}/`. Optional rosbag2 at `/workspace/rosbag2/{repo_name}/{episode_index}/`.

### Error behavior (fail-loud, German)

- Missing topic (5 s timeout) → `TaskStatus.error` + recording halts.
- Empty JointTrajectory → `RuntimeError` with German message ("JointTrajectory hat keine Punkte — Leader-Arm sendet möglicherweise nicht").
- Missing joint in `joint_order` → `KeyError` with list of expected vs available joints.
- RAM &lt; 2 GB → force early save (overlay), German warning ([§3.5 of known-issues](21-known-issues.md): warning-only, not surfaced to UI).
- Video encoder failure → encoding stays `0%` indefinitely (no retry).

### HuggingFace upload

`data_manager._upload_dataset()` runs in a daemon thread with a **1-hour timeout** (overlay addition). HF_TOKEN from `~/.cache/huggingface/token` or env. Repo: `{user_id}/{robot_type}_{task_name}`. Failure stored in `_last_warning_message` for UI consumption.

---

## Stage 6 — React SPA

**Goal:** one React 19 codebase that at build time becomes either the student Docker UI or the teacher/admin web dashboard.

### Mode switch

`src/constants/appMode.js`:
```js
export const APP_MODE = process.env.REACT_APP_MODE === 'web' ? 'web' : 'student';
```

`src/App.js` routes to `StudentApp` or `WebApp`. Web service builds with `REACT_APP_MODE=web` baked at build time; `nginx.web.conf.template` handles SPA routing + security headers.

### Auth

Supabase Auth; login form takes a username, converts to synthetic email `{username}@edubotics.local`, calls `signInWithPassword`. JWT in `Authorization: Bearer` for every API call. `/me` endpoint returns `{role, username, full_name, classroom_id, pool_info}`. Role mismatch (student → web app or vice versa) shows German toast + `signOut()`.

### Student mode (`StudentApp.js`, 5 tabs)

Home, Aufnahme (recording), Training, Inferenz, Daten. `?cloud=1` URL param skips rosbridge init. Camera streams from `web_video_server:8080` via `<img src="/stream_jpeg?topic=...">` (known memory leak on unmount). Live training progress via `useSupabaseTrainings` (Supabase Realtime, falls back to 30 s poll).

### Web mode (`WebApp.js`)

`TeacherDashboard`: list classrooms, create classroom (max 30 students), per-student credit allocation, reset password, training history, daily progress entries. `AdminDashboard`: list/create teachers, set teacher credits, reset password, delete. Credit hierarchy: admin → teacher pool → per-student allocation.

### rosbridge

`utils/rosConnectionManager.js`, `hooks/useRosServiceCaller.js`. roslib 1.4.1, singleton, `ws://window.location.hostname:9090`. Exp backoff, 30 attempts, 30s cap, 10s conn timeout. All service calls go through a generic `callService()` with 10s timeout.

---

## Stage 7 — Cloud Training

**Goal:** React POSTs one endpoint → LeRobot training runs on an NVIDIA L4 → loss/step stream into Supabase → model pushed to HuggingFace.

### Flow summary

```
React /trainings/start  →  Railway FastAPI routes/training.py:start_training
                             ├─ _sweep_user_running_jobs (parallel _sync_modal_status via asyncio.gather)
                             ├─ _find_recent_duplicate (60s window, params canonicalized JSON)
                             ├─ HfApi().dataset_info() preflight
                             ├─ RPC start_training_safe (atomic credit lock + insert)
                             └─ modal.Function.from_name("edubotics-training","train").spawn()
                                  → stored as cloud_job_id (= FunctionCall.object_id)

Modal worker (modal_app.py:train → training_handler.run_training)
  ├─ dataset preflight (meta/info.json: codebase_version v2.1, fps, joint count 4-20)
  ├─ mark running
  ├─ subprocess "python -m lerobot.scripts.train" (PYTHONUNBUFFERED=1)
  │    └─ reader thread: regex step:(\d+[KMB]?) loss:(...) → 3-retry Supabase write
  ├─ upload checkpoint to HuggingFace (edubotics/*)
  └─ mark succeeded

React frontend: Supabase Realtime (channel-filter user_id) or GET /trainings poll every 5s
```

### Key Railway internals

- **Service-role key** bypasses RLS; auth via `_assert_*` helpers.
- **`start_training_safe()` RPC**: atomic per-user. `SELECT ... FOR UPDATE` on user row → count active trainings → check credits → insert. Raises P0003 (no credits) or P0002 (user not found).
- **Credits self-healing**: no counters. `get_remaining_credits()` derives `trainings_used` from non-failed/canceled rows.
- **Stalled-worker detection**: if `last_progress_at` &gt; `STALLED_WORKER_MINUTES` (default 15), cancel + mark failed.
- **Worker token**: per-row UUID; worker can only update its row via `update_training_progress` RPC; nulled on terminal status.
- **Rate limiting**: 10/min `/trainings/start`, 20/min `/trainings/cancel`, per X-Forwarded-For. **Single uvicorn worker** required (in-process state).

### Modal image

- Base `nvidia/cuda:12.1.1-devel-ubuntu22.04` + Python 3.11.
- `lerobot[pi0] @ git+...lerobot.git@989f3d05ba47…` (LEROBOT_COMMIT pinned).
- **Force-reinstall torch+torchvision with `index_url=https://download.pytorch.org/whl/cu121`** — without it pip picks `cu130` (incompatible with cu121 base).
- `pip uninstall torchcodec` (pyav fallback).
- GPU: NVIDIA L4 (24 GB), `timeout=7h`, `min_containers=0`.
- Secret `edubotics-training-secrets` injects SUPABASE_URL + SUPABASE_ANON_KEY + HF_TOKEN.

### `training_handler.py` lifecycle

- Preflight dataset (60s timeout): version, fps, joint count, joint name match across observation.state and action, ≥1 camera.
- Subprocess: `python -m lerobot.scripts.train --policy.type=... --policy.device=cuda --dataset.repo_id=... --output_dir=... --policy.push_to_hub=false --eval_freq=0`.
- Per-policy timeout cap: ACT 1.5h, VQBET/TDMPC 2h, Diffusion/Pi0Fast 4h, Pi0/SmolVLA 6h. Modal hard timeout 7h is outer bound.
- Progress only pushed when step **increases** (dedupe). Errors ring-buffered (maxlen=4000) for post-mortem.
- SIGINT/SIGTERM handlers: kill subprocess (5s grace) + mark Supabase failed with German text "Worker wurde vom Cloud-Anbieter beendet" + cleanup.
- Upload: `HfApi.create_repo(..., exist_ok=True)` → `upload_large_folder()` of `output_dir/checkpoints/last/pretrained_model` (or fallback rglob) → `repo_info()` verification.

### Supabase migrations

- `migration.sql` — users (+credits), trainings, RLS, `get_remaining_credits()`, `start_training_safe()`.
- `002_accounts.sql` — role enum, classrooms, `adjust_student_credits()`, `get_teacher_credit_summary()`, 30-student trigger.
- `003_lessons_and_notes.sql` — superseded.
- `004_progress_entries.sql` — daily per-classroom-or-per-student note log.
- `005_cloud_job_id.sql` — `runpod_job_id → cloud_job_id` rename.
- `006_loss_history.sql` — loss array column + Postgres-side downsampling (≤300 points: 1 first + 199 evenly-spaced + 100 last) + realtime publication.
- `007_deletion_requested_at.sql` — GDPR Art. 17 marker column.

Bootstrap once: `python scripts/bootstrap_admin.py --username admin --full-name "Sven"`.

---

## Stage 8 — Inference (Trained Policy → Follower Arm)

**Goal:** pick a trained model in the UI → policy loads in `physical_ai_server` → every tick, images + state → policy → JointTrajectory → follower moves.

### Trigger

React → `/task/command` with `START_INFERENCE` + `task_info.policy_path` (local filesystem path, not HF URL). `physical_ai_server.py` → `user_interaction_callback` validates, sets `on_inference=True`, starts the 30 Hz ROS timer callback `_inference_timer_callback`.

### Model picker

React calls `GetSavedPolicyList.srv` → server lists `~/.cache/huggingface/hub/models--*/snapshots/*/pretrained_model/`. Policy path is always local. On first tick `PreTrainedPolicy.from_pretrained(policy_path)` downloads on-demand from HF if missing (no prefetch, no volume mount). Respects `HF_HOME`.

### Overlay validation at load

`overlays/inference_manager.py`:
- `_read_expected_image_keys()` reads `config.input_features` keys matching `observation.images.*` → e.g., `{'observation.images.gripper', 'observation.images.scene'}` for OMX-F.
- `_read_expected_image_shapes()` reads shape tuples for resolution validation.

### Per-tick (synchronous on main ROS executor)

1. `communicator.get_latest_data()` waits for all topics (5 s timeout).
2. `convert_msgs_to_raw_datas()` → RGB uint8 HWC images + float32 state [6].
3. Lazy `load_policy()` on first tick (moves weights to GPU).
4. **Camera-name exact-match** — overlay raises German RuntimeError if mismatch ("Das Modell erwartet die Kameras {expected}, aber verbunden sind nur {provided}"). Prevents silent alphabetical remapping.
5. **Stale-camera halt** — sample 4 sparse 256-byte slices per image → hash; if any camera frozen &gt;5 s, return None (skip tick).
6. **Image shape validation** — validate against expected `(H, W)`, return None on mismatch.
7. `_preprocess(images, state)`: each image `torch.from_numpy` → `/255` → `permute(2,0,1)` → `unsqueeze(0)`, keyed as `observation.images.{name}`. State → float32 tensor → batch.
8. `policy.select_action(observation)` under `torch.inference_mode()`.
9. **Safety envelope** — NaN/inf reject, joint-limit clamp, per-tick velocity cap.
10. `data_converter.tensor_array2joint_msgs(action, …)` builds `JointTrajectory` with **fps-aware time_from_start** (overlay computes from `set_action_duration_from_fps()`).
11. `communicator.publish_action(msg)` → `/arm_controller/follow_joint_trajectory` (which is remapped to `/leader/joint_trajectory` → drives the follower).

### Gripper

There is **no** separate gripper action call. `gripper_joint_1` is the 6th element of the same action array and rides the same `JointTrajectory`. The follower's controller handles all 6 DoF.

### Threading

Inference is **single-threaded** on the ROS2 executor — no worker thread. A slow topic blocks the loop. Training, by contrast, spawns a daemon.

---

## 9. Common gotchas (one-screen reference)

| Symptom | Cause | Fix |
|---|---|---|
| dockerd doesn't start in WSL2 | boot PATH is empty | `start-dockerd.sh` re-exports PATH |
| compose fails "trying to mount a directory onto a file" | `/etc/timezone` / `/etc/localtime` missing | `tzdata` + real files in rootfs |
| Multi-layer pull corrupts on large image | Docker 29.x containerd-snapshotter | pin Docker 27.5.1, `containerd-snapshotter: false` |
| s6 service silently disabled | base image leaves it in `.d/` but not enabled | `.s6-keep` empty-file mount |
| s6 rejects `longrun\r` | Windows Git CRLF | Dockerfile `sed -i 's/\r//g'` |
| Modal worker uses wrong torch | default pip picks cu130 | force `index_url=.../whl/cu121` |
| Inference silently swaps cameras | upstream alphabetical remap | overlay enforces exact name match |
| Empty JointTrajectory crashes recording | upstream accepts it silently | overlay raises German RuntimeError |
| "longrun ready" during install but dockerd hangs | WSL2 just booted | poll `docker info` 60 s, then force-`start-dockerd.sh` |
| Stuck "running" training | Modal died mid-run | FastAPI stalled-worker sweep (15 min default) |
| dockerd dies, never restarts | no watchdog (pre 2026-04-17) | watchdog loop in `start-dockerd.sh` |
| GUI buttons stuck on hardware fail | `_start_environment` daemon thread early returns without resetting `self.running` | finally block in `_do_start()` |

---

## 10. Files to read first when debugging a specific stage

| Problem class | First file | Then |
|---|---|---|
| Install fails | `installer/robotis_ai_setup.iss` + matching `.ps1` | `wsl_rootfs/build_rootfs.sh`, `daemon.json`, `start-dockerd.sh` |
| GUI frozen / USB missing | `gui/app/gui_app.py:_start_environment` | `docker_manager._docker_cmd`, `device_manager.scan_and_identify_arms` |
| Arm doesn't move on startup | `docker/open_manipulator/entrypoint_omx.sh` | `omx_f_follower_ai.launch.py` remap line |
| Compose error | `docker/docker-compose.yml` | `docker/physical_ai_server/Dockerfile` overlays |
| Recording crashes | `overlays/data_manager.py`, `overlays/data_converter.py` | `physical_ai_server.py:user_interaction_callback` |
| React can't see ROS | `src/utils/rosConnectionManager.js` | compose port 9090, nginx.conf |
| `/trainings/start` 400 | `cloud_training_api/app/routes/training.py` | `services/modal_client.py`, `start_training_safe` RPC |
| Training stalls | `modal_training/training_handler.run_training` | FastAPI stalled-sweep in `_sync_modal_status` |
| Inference crashes on "erwartet Kameras" | `overlays/inference_manager.py:166` | `omx_f_config.yaml` camera names, policy `config.json` |

For more tactical debugging, see [`WORKFLOW-debug.md`](WORKFLOW-debug.md).

---

**Last verified:** 2026-05-04.

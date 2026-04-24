# EduBotics — End-to-End Pipeline Reference

Companion to `Testre/CLAUDE.md`. That file is the map (who/what/where); this one is the deep dive (how each stage actually works, with file:line references). Read top-to-bottom to trace one student workflow from shrink-wrap to trained-model inference.

---

## 0. Stage Map

| # | Stage | Owner | Key dirs |
|---|-------|-------|----------|
| 1 | Windows installer + WSL2 rootfs | Our code | `robotis_ai_setup/installer/`, `robotis_ai_setup/wsl_rootfs/` |
| 2 | Windows tkinter GUI (`EduBotics.exe`) | Our code | `robotis_ai_setup/gui/` |
| 3 | Robot-arm connection (ROS2 + Dynamixel) | ROBOTIS upstream + our overlay | `open_manipulator/`, `robotis_ai_setup/docker/open_manipulator/` |
| 4 | Docker Compose (3 containers in WSL2 distro) | Our code | `robotis_ai_setup/docker/` |
| 5 | Dataset recording (LeRobot v2.1) | ROBOTIS upstream + our overlays | `physical_ai_tools/physical_ai_server/`, `robotis_ai_setup/docker/physical_ai_server/overlays/` |
| 6 | React SPA (student UI + teacher/admin web) | ROBOTIS upstream (hacked) | `physical_ai_tools/physical_ai_manager/` |
| 7 | Cloud training (Railway API + Modal GPU) | Our code | `robotis_ai_setup/cloud_training_api/`, `robotis_ai_setup/modal_training/` |
| 8 | Inference (load policy → drive arm) | ROBOTIS upstream + our overlays | `physical_ai_tools/physical_ai_server/inference/`, `overlays/inference_manager.py` |

---

## 1. Installer + WSL2 Rootfs

Goal: a student double-clicks `EduBotics_Setup.exe` and ends up with Docker running inside a bundled Ubuntu 22.04 WSL2 distro, with ROBOTIS USB devices routable, all without installing Docker Desktop.

### Inno Setup orchestration
- Entry: `robotis_ai_setup/installer/robotis_ai_setup.iss` (~208 MB `.exe`, AppID `{B7E3F2A1-8C4D-4E5F-9A6B-1D2E3F4A5B6C}`, v2.2.2, `PrivilegesRequired=admin`).
- Ships three assets in `installer/assets/`:
  - `edubotics-rootfs.tar.gz` (~193 MB gzipped Ubuntu 22.04 rootfs)
  - `EduBotics.exe` (PyInstaller GUI, ~5.8 MB)
  - license + icon
- Install dir `C:\Program Files\EduBotics\{gui,scripts,docker,wsl_rootfs,icon.ico}`; user-writable `.env` lives at `%LOCALAPPDATA%\EduBotics\.env` (moved out of Program Files in v2.2.1 to avoid UAC on regen).

### Seven-step PowerShell chain (in order; each step is a `.ps1` under `installer/scripts/`)
1. `migrate_from_docker_desktop.ps1` — silent uninstall Docker Desktop; unregister `docker-desktop` + `docker-desktop-data` WSL distros; remove Docker Desktop run-key from logged-in user's `HKEY_USERS\<SID>\...\Run`.
2. `install_prerequisites.ps1` — verify Win11 ≥ build 22000 + Hyper-V; `wsl --install --no-distribution`; download + install `usbipd-win` MSI from GitHub releases. If WSL kernel install wants a reboot, drop `.reboot_required` marker in `{app}\scripts\` and defer steps 4–5 to post-reboot via `finalize_install.ps1` (GUI detects the marker on first launch and re-elevates).
3. `configure_wsl.ps1` — merge recommended lines into `~/.wslconfig` (memory=8GB, swap=4GB). Uses `Win32_Process.GetOwner()` on `explorer.exe` to find the real logged-in user (elevated PS would otherwise resolve to admin).
4. `configure_usbipd.ps1` — `usbipd policy add` for every ROBOTIS `VID:PID` (`2F5D:0103` OpenRB-150, `2F5D:2202` alt fw). Handles usbipd 4.x/5.x API drift (`--operation AutoBind` exists only in 5.x).
5. `import_edubotics_wsl.ps1` — `wsl --unregister EduBotics` (if present) → `wsl --import EduBotics $env:ProgramData\EduBotics\wsl assets\edubotics-rootfs.tar.gz --version 2` → poll `docker info` up to 60s; on timeout, manually `exec /usr/local/bin/start-dockerd.sh`.
6. `pull_images.ps1` — pulls the 3 `nettername/*` images (tag from `docker/versions.env`) via `wsl -d EduBotics -- docker pull`, then `docker image prune -f`. Uses UTF-16LE-to-UTF-8 regex to sanitize `wsl --list` output (NUL-embedded).
7. `verify_system.ps1` — post-install health check (distro present, dockerd up, usbipd installed, images present).

### Bundled WSL2 rootfs (`robotis_ai_setup/wsl_rootfs/`)
- `build_rootfs.sh` → `docker build -t edubotics-rootfs:latest .` → `docker export | gzip -9 > installer/assets/edubotics-rootfs.tar.gz`.
- `Dockerfile` base `ubuntu:22.04`; pins **Docker 27.5.1 + containerd 1.7.27** (Docker 29.x's containerd-snapshotter corrupts multi-layer pulls on WSL2 custom rootfs; 29+ removed the disable flag so downgrade is the fix). Installs `tzdata` and sets `Europe/Berlin` — `/etc/timezone` + `/etc/localtime` must exist as *files* (not dirs) or compose bind-mounts fail with "trying to mount a directory onto a file".
- `wsl.conf`: `[boot] command=/usr/local/bin/start-dockerd.sh`, `[user] default=root`, `[interop] appendWindowsPath=false`, `hostname=edubotics`. Systemd is explicitly NOT used — unreliable on a custom-imported rootfs.
- `daemon.json`: `overlay2`, `nvidia` runtime, `containerd-snapshotter: false`, 10m/3-file log rotation.
- `start-dockerd.sh`: re-exports PATH (WSL boot ctx has empty PATH → dockerd can't find containerd/runc), then `nohup /usr/bin/dockerd >/var/log/dockerd.log 2>&1 &`, idempotent.

### Uninstall
`[UninstallRun]` in the `.iss`: best-effort `docker compose down` inside distro → `wsl --unregister EduBotics` (clears distro's VHDX) → Inno auto-removes Program Files. `%LOCALAPPDATA%\EduBotics\.env` is intentionally left behind (regenerated on reinstall).

### Non-obvious gotchas
- WebView2 isn't installed by the installer — relies on Windows 11 shipping it.
- The rootfs `.tar.gz` is shipped *both* inside the installer binary *and* copied to `{app}\wsl_rootfs\` on disk for offline re-imports.
- WSL boot's empty PATH is the single most common source of "dockerd doesn't start" bugs.

---

## 2. Windows tkinter GUI (`EduBotics.exe`)

Goal: a hardware-setup wizard that detects arms/cameras, generates `.env`, brings up Docker containers, and opens the React UI in an embedded WebView2 window.

### Entry + PyInstaller
- `gui/main.py:18–37` dispatches on a `--webview` sentinel: default → `gui_app.run()` (tkinter), with sentinel → `webview_window.run_in_process()` (subprocess-only pywebview).
- `gui/build.spec` collects pywebview + pythonnet CLR DLLs, hides imports, `console=False`, packs `assets/icon.ico`.
- Version + distro constants in `gui/app/constants.py` (`WSL_DISTRO_NAME`, `APP_VERSION`, env-var overrides like `EDUBOTICS_WSL_DISTRO`).

### Main window (`gui/app/gui_app.py:125–1051`) — a linear wizard
1. **Cloud-only checkbox** (lines 170–190): greys out hardware frames via `_set_frame_state()`, and startup switches from full-stack compose to manager-only.
2. **Arm scan** (lines 688–731): `device_manager.scan_and_identify_arms()` runs in a daemon thread. Every detected ROBOTIS USB port (`VID=2F5D`) is attached via `usbipd attach --wsl --distribution EduBotics --busid X`, then a throw-away `open_manipulator` container runs `identify_arm.py <serial>` to tell leader/follower apart.
3. **Camera scan** (lines 779–841): `wsl_bridge.list_video_devices()` iterates `/dev/video*` in the distro with `v4l2-ctl --info`. If two cameras found, a role-assignment UI appears (dropdowns "Greifer-Kamera" / "Szenen-Kamera").
4. **Start/Stop/Browser buttons** — `_update_start_button()` gates on prerequisites. `_start_environment()` (lines 845–959) runs off the UI thread: re-attaches USB → polls `/dev/serial/by-id/` → regenerates `.env` → `docker compose up -d` (w/ `-f docker-compose.gpu.yml` if nvidia-smi succeeds) → polls :80 for HTTP 200 → opens WebView2 subprocess.
5. **Log pane** (lines 266–274): thread-safe `_log()` into a `ScrolledText`.

### Docker wrapping (`gui/app/docker_manager.py`)
- Every Docker call goes through `_docker_cmd()` (lines 42–53) → `wsl -d EduBotics -- docker ...`. There is no Docker Desktop dependency anywhere.
- `wait_for_docker()` (lines 109–141) polls `docker info`; at 15s+ stall it force-invokes `start-dockerd.sh`.
- **Pull stall watchdog** (lines 296–432): Docker's `\r`-animated progress bars look idle to line-based readers, so the watchdog monitors stdout-line-rate *and* `/var/lib/docker/overlay2` disk growth (10 MB / 20s). If both stall past `stall_timeout` (600s first pull / 120s updates), it `pkill -KILL dockerd`, restarts, and retries with exponential backoff. This is the main knob for poor-network classrooms.
- `has_gpu()` (lines 144–158) just calls host `nvidia-smi` — WSL2 forwards the host NVIDIA driver, so distro GPU visibility == host GPU visibility.

### WebView2 subprocess (`gui/app/webview_window.py`)
- pywebview 6 requires `webview.start()` on the main thread — which tkinter owns. So `open_student_window(url)` `subprocess.Popen()`s the same `EduBotics.exe` with `--webview --url …` (`CREATE_NO_WINDOW`). A watchdog thread maps a non-zero exit (usually missing Edge WebView2) to `_runtime_missing`; `_webview_fallback()` in the parent then `webbrowser.open(url)` as graceful degradation.
- Cloud-only mode appends `?cloud=1` to the URL so the React app knows to skip the rosbridge gate.

### `.env` generation (`gui/app/config_generator.py:7–72`)
Writes `FOLLOWER_PORT=/dev/serial/by-id/...`, `LEADER_PORT=...`, `CAMERA_DEVICE_N`, `CAMERA_NAME_N` (defaults `gripper`/`scene`), `ROS_DOMAIN_ID=30`. `generate_cloud_only_env()` writes empty placeholders so compose doesn't error on missing vars.

### UAC & self-updating
- `_elevate_and_wait` (`gui/app/gui_app.py:19–87`) uses Win32 `ShellExecuteExW` directly, not PS `Start-Process`, so it gets a real process handle for `WaitForSingleObject` — critical for `finalize_install.ps1` which runs the deferred install steps after a reboot.
- Update gate: on startup, polls Railway `/version`. If newer exists, *blocking* modal forces update; download to `%TEMP%` then `os.startfile`.

### Language
All tkinter strings are German (status messages, errors, buttons: "Arme scannen", "Fehlende Hardware", "Leader gefunden"). Backend code, docstrings, and log prefixes stay English.

---

## 3. Robot-Arm Connection (ROS2 Jazzy + Dynamixel)

Goal: one leader + one follower OMX arm appear on ROS2 topics inside the `open_manipulator` container.

### Two arms on two serial ports
| | OMX-F follower | OMX-L leader |
|--|----------------|--------------|
| Servo IDs | 11–16 | 1–6 |
| Namespace | global | `/leader/` (via `PushRosNamespace`) |
| Control | position, all joints have command iface | joints 1–5 *read-only* + gravity compensation; joint 6 (gripper) current-controlled (Op Mode 5, 300 mA limit) |
| Launch file | `open_manipulator/open_manipulator_bringup/omx_f_follower_ai.launch.py` | `.../omx_l_leader_ai.launch.py` |
| ros2_control xacro | `omx_f.ros2_control.xacro` | `omx_l.ros2_control.xacro` |
| Hardware plugin | `dynamixel_hardware_interface/DynamixelHardware` @ 1 Mbps Protocol 2.0, update rate 100 Hz |
| Default device | `/dev/ttyACM0` | `/dev/ttyACM2` |

### `identify_arm.py` (`robotis_ai_setup/docker/open_manipulator/identify_arm.py`)
Pings IDs 1–6 (leader) and 11–16 (follower) at 1 Mbps Protocol 2.0 over an exclusive serial handle. Returns `"leader"` / `"follower"` / `"unknown"` / `"error:..."` based on which id-range responded more. **Not called by the entrypoint** — it exists for the GUI's device scanner (the entrypoint trusts the explicit `FOLLOWER_PORT`/`LEADER_PORT` env vars from `.env`).

### Entrypoint choreography (`robotis_ai_setup/docker/open_manipulator/entrypoint_omx.sh`, 207 lines)
This script is PID 1 of the container (no systemd). Four phases:

1. **Validate hardware** (lines 28–45). Wait ≤30s per port, `chmod 666 $FOLLOWER_PORT $LEADER_PORT`. Exports `ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-30}`.
2. **Launch leader first** (lines 47–61). `ros2 launch ... omx_l_leader_ai.launch.py port_name:=$LEADER_PORT`. Wait ≤30s for `/leader/joint_states`. Then (lines 64–92) an inline one-shot Python subscriber reads the first complete `/leader/joint_states` message, dumps positions as JSON to a shell variable.
3. **Launch follower + smooth sync** (lines 96–181). `ros2 launch ... omx_f_follower_ai.launch.py`. Wait for `/joint_states`. Then publish a **quintic-polynomial** (`s(t) = 10t³ − 15t⁴ + 6t⁵`) 50-waypoint trajectory over 3 seconds to `/leader/joint_trajectory` to smoothly move the follower to the leader's pose — zero velocity/acceleration at endpoints, no snap on startup.
4. **Launch cameras** (lines 183–200). Up to two `usb_cam` nodes, each driven by `CAMERA_DEVICE_N` + `CAMERA_NAME_N`. Topics: `/{name}/image_raw/compressed`.

Trap on SIGTERM/SIGINT kills all ROS launch children; final `wait` line keeps PID 1 alive.

### The magic remap
In `omx_f_follower_ai.launch.py:144` the arm controller's default action topic is **remapped**: `/arm_controller/joint_trajectory` → `/leader/joint_trajectory`. That's why anyone publishing to `/leader/joint_trajectory` drives the follower — it's the same topic. The follower has no concept of "leader"; the two arms are decoupled, and the entrypoint's sync publish + later inference-node publishes both ride the same rail.

### Topics that matter

| Topic | Type | Pub | Sub |
|-------|------|-----|-----|
| `/joint_states` | JointState | follower broadcaster | physical_ai_server (record + inference) |
| `/leader/joint_states` | JointState | leader broadcaster | entrypoint sync reader, physical_ai_server (record) |
| `/leader/joint_trajectory` | JointTrajectory | entrypoint sync, physical_ai_server inference | follower arm_controller (via remap) |
| `/arm_controller/follow_joint_trajectory` | Action | physical_ai_server inference | follower JointTrajectoryController |
| `/gripper/image_raw/compressed` | CompressedImage | usb_cam #1 | physical_ai_server |
| `/scene/image_raw/compressed` | CompressedImage | usb_cam #2 | physical_ai_server |

### Compose wiring
`docker-compose.yml` gives the container `privileged: true`, `/dev:/dev`, `/dev/shm:/dev/shm`, ulimits `rtprio=99 rttime=-1 memlock=8GB` (soft realtime), on bridge `ros_net`.

---

## 4. Docker Containers (inside the EduBotics WSL2 distro)

Goal: three containers (`open_manipulator`, `physical_ai_server`, `physical_ai_manager`) on a `ros_net` bridge, sharing `ROS_DOMAIN_ID=30`, with host ports `80` / `9090` / `8080` forwarded by WSL2.

### Build chain (`robotis_ai_setup/docker/build-images.sh`)
Order + what gets baked in:

1. **physical_ai_manager** — React SPA compiled with BuildArgs: `REACT_APP_SUPABASE_URL`, `REACT_APP_SUPABASE_ANON_KEY`, `REACT_APP_CLOUD_API_URL`, `REACT_APP_ALLOWED_POLICIES` (student build = `"act"`), `REACT_APP_MODE` (default `student`), `REACT_APP_BUILD_ID` (UTC timestamp + 7-char git SHA, for self-reload detection).
2. **physical_ai_server** — `docker pull robotis/physical-ai-server:latest` (or amd64-0.8.2) → our thin-layer Dockerfile on top.
3. **open_manipulator** — pull `robotis/open-manipulator:amd64-4.1.4` (or `BUILD_BASE=1` to build from source, ~40 min) → thin layer.

All three pushed to `nettername/*`.

### `docker-compose.yml`

| Service | Depends | Ports | Privileged | Network | Notable mounts |
|---------|---------|-------|-----------|---------|----------------|
| `open_manipulator` | — | — | yes | `ros_net` | `/dev`, `/dev/shm` |
| `physical_ai_server` | open_manipulator | 8080, 9090 | yes | `ros_net` | `/dev`, `/dev/shm`, `ai_workspace` named volume, HF cache, agent socket, `.s6-keep` marker |
| `physical_ai_manager` | physical_ai_server | 80 | no | `ros_net` | — |

`depends_on` only controls start order, not readiness; real readiness is done by entrypoints. Bridge (not `host` mode) gives DNS-by-service-name.

`docker-compose.gpu.yml` is a 10-line overlay adding `runtime: nvidia` + `deploy.resources.reservations.devices` **only** for `physical_ai_server`. GUI picks it based on `nvidia-smi`.

### `physical_ai_server/Dockerfile` (thin layer) — three operations
1. **CRLF strip** (lines 11–13): `find /etc/s6-overlay/s6-rc.d/** /usr/local/lib/s6-services/**/*.sh -exec sed -i 's/\r//g'` — Windows Git checkouts turn LF→CRLF, and s6-overlay rejects `longrun\r` as an invalid service type at runtime.
2. **Patch** (lines 15–24): `patches/fix_server_inference.py` regex-patches upstream `server_inference.py` to (a) initialize `self._endpoints = {}` before first `register_endpoint()` and (b) remove the duplicate `InferenceManager` construction block (lines ~60–64 and ~71–75 in upstream both construct it; the second one silently overrides).
3. **Overlays** (lines 26–45): four files copied into `/tmp/overlays/`, then `find + cp` over upstream paths in `/root/ros2_ws/`:
   - `overlays/inference_manager.py` — exact camera-name match; stale-image hash watchdog.
   - `overlays/data_manager.py` — float32 dtype enforcement; RAM cushion + early-save warning.
   - `overlays/data_converter.py` — empty-trajectory guard, fail-loud missing-joint error, `Duration(sec=0, nanosec=50_000_000)` on JointTrajectoryPoint (upstream omitted time_from_start).
   - `overlays/omx_f_config.yaml` — `joint_order`, `camera_topic_list`, `observation_list=[gripper, scene, state]`.

LeRobot is **not overlaid** — byte-identical to upstream `989f3d05`.

### `physical_ai_manager/Dockerfile`
- Stage 1 (Node 22): compile React. CRA writes `/version.json` with `REACT_APP_BUILD_ID`; React polls it every 5s and self-`location.reload()`s on mismatch — the way image updates reach open browser tabs.
- Stage 2 (nginx 1.27 Alpine): `/index.html` + `/version.json` → `Cache-Control: no-store`; `/static/*` → cache 1 year (CRA hash-content addressing). SPA fallback routes everything else to `/index.html`.

### `open_manipulator/Dockerfile`
Adds `v4l-utils` + `dynamixel-sdk==4.0.3`, copies `entrypoint_omx.sh` (with CRLF strip) + `identify_arm.py` to `/usr/local/bin`.

### The `.s6-keep` mystery
`physical_ai_server/.s6-keep` is an empty 1-byte file mounted read-only at `/etc/s6-overlay/s6-rc.d/user/contents.d/physical_ai_server`. s6-overlay enables services by detecting their name as a file in `user/contents.d/`. The base image defines the service but leaves it disabled; the compose mount *is* how it's enabled at runtime. Remove the mount → the server container starts but the ROS node never runs.

### `.env` contract (keys compose expects)
`FOLLOWER_PORT`, `LEADER_PORT`, `CAMERA_DEVICE_1`, `CAMERA_NAME_1` (default `gripper`), `CAMERA_DEVICE_2`, `CAMERA_NAME_2` (default `scene`), `ROS_DOMAIN_ID=30`, `REGISTRY=nettername` (image prefix). Generated by GUI (§2) to `%LOCALAPPDATA%\EduBotics\.env`, passed to compose via `--env-file`.

---

## 5. Dataset Recording (LeRobot v2.1)

Goal: an episode → H.264 videos per camera + parquet of state/action + `meta/info.json`, optionally pushed to HuggingFace.

### Trigger
React calls the `/task/command` service (`SendCommand.srv` from `physical_ai_tools/physical_ai_interfaces/`) with `command=START_RECORD=1` and a `TaskInfo` payload (task_name, task_instruction[], fps, warmup_time_s, episode_time_s, reset_time_s, num_episodes, push_to_hub, record_rosbag2, use_optimized_save_mode). Routed by `physical_ai_server.py:764` → `user_interaction_callback()`.

### State machine (`data_manager.py:108–224`)
`warmup → run → save → reset → (loop) → finish`. Each phase is time-gated; `TaskStatus.msg` pushes `phase`, `proceed_time`, `current_episode_number`, `encoding_progress` back to the UI.

### Per-tick pipeline (fps typically 30 Hz)
1. `communicator.get_latest_data()` blocks ≤5s/topic, collecting `/gripper/image_raw/compressed`, `/scene/image_raw/compressed`, `/joint_states` (follower), `/leader/joint_trajectory`.
2. `convert_msgs_to_raw_datas()` (data_manager.py:353–387):
   - Images: cv_bridge → BGR → `cvtColor(..., BGR2RGB)` → uint8 HWC.
   - Follower: JointState → `joint_state2tensor_array()` (`data_converter.py:88–116`) reorders per `joint_order` → float32 [6].
   - Leader action: `joint_trajectory2tensor_array()` reads `points[0].positions`, reorders, float32 [6]. **Overlay**: raises "JointTrajectory hat keine Punkte — Leader-Arm sendet moeglicherweise nicht" if empty.
3. `create_frame()` assembles `{'observation.images.gripper': img_rgb, 'observation.images.scene': img_rgb, 'observation.state': state, 'action': action}`, dtype-casting to float32.
4. `add_frame_without_write_image()` (`lerobot_dataset_wrapper.py:148–169`) validates vs. schema, appends to episode buffer, auto-timestamps as `frame_index / fps` (wall-clock is NOT used — assumes constant fps).
5. Video encoding (`ffmpeg_encoder.py:88–150`): raw RGB piped to `ffmpeg libx264 -crf 28 -pix_fmt yuv420p`, async.
6. `save_episode_without_video_encoding()` writes `data/chunk-000/episode_000000.parquet` + `videos/chunk-000/{gripper,scene}/episode_000000.mp4` + updates `meta/info.json` (codebase_version `"v2.1"`), `meta/episodes.jsonl`, `meta/episodes_stats.jsonl`, `meta/tasks.jsonl`, `meta/stats.json`.

### Disk layout
Inside container: `~/.cache/huggingface/lerobot/{user_id}/{robot_type}_{task_name}/`. Optional rosbag2 at `/workspace/rosbag2/{repo_name}/{episode_index}/`.

### `omx_f_config.yaml` (the overlay version)
```yaml
observation_list: [gripper, scene, state]
camera_topic_list:
  - gripper:/gripper/image_raw/compressed
  - scene:/scene/image_raw/compressed
joint_topic_list:
  - follower:/joint_states
  - leader:/leader/joint_trajectory
joint_order:
  leader: [joint1, joint2, joint3, joint4, joint5, gripper_joint_1]
```
fps comes from `TaskInfo`, not YAML. No per-topic thresholds.

### Error behavior (fail-loud, German)
- Missing topic (5s timeout) → `TaskStatus.error` + recording halts.
- Empty JointTrajectory → `RuntimeError` with German message, bubbled to UI.
- Missing joint in `joint_order` → `KeyError` with list of expected vs. available joints.
- Video encoder failure → encoding stays `0%` indefinitely (no retry).
- HF upload failure → logged; local dataset still saved.

### HuggingFace upload
`data_manager._upload_dataset()` → `self._lerobot_dataset.push_to_hub(..., upload_large_folder=True)`. HF_TOKEN from `~/.cache/huggingface/token` or env. Repo: `{user_id}/{robot_type}_{task_name}`.

### Odd/non-obvious
- "Optimized save mode" holds frames in RAM until episode end (needs a 2 GB cushion or overlay early-saves with German warning).
- Multi-task (len(task_instruction)>1) forces `num_episodes = 1_000_000` → infinite until manual `FINISH`.
- Recording stores **uint8** in video files but **float32** state/action in parquet. Inference scales images uint8→float32/255 at tensor time; recording never does.

---

## 6. React SPA `physical_ai_manager` (student + web)

Goal: one React 19 codebase that at build time becomes either the student Docker UI or the teacher/admin web dashboard (deployed as a separate Railway service via `Dockerfile.web` + nginx).

### Mode switch (`src/constants/appMode.js:7`)
```js
export const APP_MODE = process.env.REACT_APP_MODE === 'web' ? 'web' : 'student';
```
`src/App.js:24` routes to `StudentApp` or `WebApp`. Student mode is default; the Railway `web` service builds with `REACT_APP_MODE=web` (baked at build time via `Dockerfile.web` ARG); `nginx.web.conf.template` handles SPA routing (all paths → `/index.html`) and security headers. `vercel.json` is a stale marker only.

### Auth
Supabase Auth; login form takes a username, converts to synthetic email `{username}@edubotics.local`, calls `signInWithPassword`. JWT goes in `Authorization: Bearer` for every API call. `/me` endpoint returns `{role, username, full_name, classroom_id, pool_info}`. Role mismatch (student → web app or vice versa) shows German toast + `signOut()`.

### Student mode (`StudentApp.js`, 5 tabs)
- Home, Aufnahme (recording), Training, Inferenz, Daten.
- `?cloud=1` URL param skips rosbridge init (for cloud-only mode from the GUI).
- Camera streams come from `web_video_server:8080` via `<img src="/stream_jpeg?topic=...">` — known memory leak on unmount (noted in `FRONTEND_UX_FOLLOWUPS.md`).
- Live training progress: `useSupabaseTrainings` subscribes to `public.trainings` Realtime channel, falls back to 30s poll.
- Policy dropdown filtered via `REACT_APP_ALLOWED_POLICIES` (default `"act"`); client-side filter, server enforces again.

### Web mode (`WebApp.js`)
- `TeacherDashboard`: list classrooms, create classroom (max 30 students), per-student credit allocation (delta-based), reset password, training history, **daily progress entries** (per-student or class-wide, backed by Supabase `progress_entries` table from migration `004`).
- `AdminDashboard`: list/create teachers, set teacher credits, reset password, delete.
- Credit hierarchy: admin → teacher pool → per-student allocation.

### rosbridge (`utils/rosConnectionManager.js`, `hooks/useRosServiceCaller.js`)
- `roslib` 1.4.1, singleton, ws://`window.location.hostname`:9090.
- Exp backoff, 30 attempts, 30s cap, 10s conn timeout.
- All service calls go through a generic `callService()` with 10s timeout.

### Custom ROS services (all from `physical_ai_interfaces`)
`/task/command` (SendCommand — start/stop record + inference), `/training/command` (SendTrainingCommand — *local* training; not used in cloud path), `/training/get_available_policy`, `/training/get_dataset_list`, `/training/get_model_weight_list`, `/image/get_available_list`, `/get_robot_types`, `/set_robot_type`, `/register_hf_user` + `/get_registered_hf_user` + `/huggingface/control`, `/browse_file`, `/dataset/edit`, `/dataset/get_info`.

### Cloud API calls (Railway FastAPI)
- Training: `POST /trainings/start`, `POST /trainings/cancel`, `GET /trainings`, `GET /trainings/{id}`, `GET /trainings/quota`.
- User: `GET /me`.
- Teacher: `/teacher/classrooms*`, `/teacher/students*`, `/teacher/students/{id}/credits`, `/teacher/classrooms/{id}/progress-entries`, `PATCH|DELETE /progress-entries/*`.
- Admin: `/admin/teachers*`, `/admin/teachers/{id}/credits`, `/admin/teachers/{id}/password`.

### i18n
All user-facing strings hard-coded **German** per component. No i18next / react-intl. Changing language requires extracting every string.

### Notable upstream issues tracked in `FRONTEND_UX_FOLLOWUPS.md`
Silent ROS-disconnect mid-recording (no heartbeat) → episode corrupts; no delete-confirmation modal; no loading spinners (dedupe-60s on API side mitigates); training-page polling doesn't clear on unmount; image-stream memory leak; modal stacking. All live in ROBOTIS upstream code — "fix" path is PR upstream or fork+pin.

### Dependencies
React 19.1, Redux Toolkit 2.8.2, Supabase 2.49.8, roslib 1.4.1, Tailwind 3.4.17, react-hot-toast, recharts (live loss charts). Apache 2.0. Version 0.8.2.

---

## 7. Cloud Training (Railway FastAPI + Modal GPU)

Goal: React POSTs one endpoint → LeRobot training runs on an NVIDIA L4 → loss/step stream into Supabase → model pushed to HuggingFace.

### Flow summary
```
React /trainings/start  →  Railway FastAPI routes/training.py:start_training
                             ├─ _sweep_user_running_jobs + _sync_modal_status
                             ├─ _find_recent_duplicate (60s window)
                             ├─ HfApi().dataset_info() preflight
                             ├─ RPC start_training_safe (atomic credit+row)
                             └─ modal.Function.from_name("edubotics-training","train").spawn()
                                  → stored as cloud_job_id (= FunctionCall.object_id)

Modal worker (modal_app.py:train → training_handler.run_training)
  ├─ dataset preflight (meta/info.json: codebase_version v2.1, fps, joint lists)
  ├─ mark running
  ├─ subprocess "python -m lerobot.scripts.train" (PYTHONUNBUFFERED=1)
  │    └─ reader thread: regex step:(\d+[KMB]?) loss:(…) → 3-retry Supabase write
  ├─ upload checkpoint to HuggingFace (edubotics/*) with camera_config.json
  └─ mark succeeded

React frontend: Supabase Realtime (channel-filter user_id) or GET /trainings poll every 5s
```

### Railway FastAPI (`robotis_ai_setup/cloud_training_api/`)
Key files: `main.py` (CORS, routers), `routes/training.py`, `routes/teacher.py`, `routes/admin.py`, `services/modal_client.py`, `services/usernames.py`, `auth.py`, `supabase_client.py`.

- CORS: `ALLOWED_ORIGINS` env var (comma-split, defaults `http://localhost`). Must include the Railway web-service URL (e.g. `https://edubotics-web.up.railway.app`).
- Auth: `Authorization: Bearer <JWT>` validated via `supabase.auth.get_user()`. Role helpers: `get_current_teacher`, `get_current_admin`, `get_current_profile`.
- Supabase backend uses the **service role key** (bypasses RLS); RLS policies exist but are a safety net (real access control is FastAPI ownership checks).

### `start_training_safe()` SQL RPC (`supabase/migration.sql:175–227`)
Atomic per-user:
1. `SELECT ... FOR UPDATE` on user row.
2. Count non-failed/non-canceled trainings.
3. If `credits < used + 1` → `RAISE P0003` (credit error).
4. Insert training row with a per-job `worker_token` UUID (scoped RPC auth — the worker can only update *its* row, token nullified on terminal status).
5. Return `(training_id, remaining)`.

### Credits (self-healing)
No counters. `get_remaining_credits()` derives `trainings_used` by counting non-failed/canceled rows. Fail/cancel flip auto-unlocks the credit. Double-refund impossible. `adjust_student_credits(teacher_id, student_id, delta)` is the atomic teacher-pool → student-allocation RPC (002_accounts.sql). Classroom limit (30 students) enforced by trigger with `RAISE P0010`.

### Modal image (`modal_training/modal_app.py:23–50`)
- Base `nvidia/cuda:12.1.1-devel-ubuntu22.04` + Python 3.11.
- `lerobot[pi0] @ git+https://github.com/huggingface/lerobot.git@989f3d05` — same commit as the server container.
- **Force-reinstall torch+cu121 with explicit `index_url`** — without it, pip picks `cu130` which is incompatible with the cu121 base image runtime.
- `pip uninstall torchcodec` (pyav fallback; torchcodec binds to specific libs that cause runtime failures on this image).
- `.add_local_python_source("training_handler")` bundles the handler.
- GPU: `NVIDIA L4` (24 GB), `timeout=7h`, `min_containers=0`.

### `training_handler.py` lifecycle (detail)
- `_parse_abbreviated_number()` handles `"50K"`/`"1.5M"` → 50000/1500000.
- Progress is only pushed to Supabase when the step *increases* (dedupe). Errors ring-buffered (maxlen=4000) for post-mortem on failure.
- SIGINT/SIGTERM handlers mark failed with German text "Worker wurde vom Cloud-Anbieter beendet" and clean `/tmp/training_output/`.
- Subprocess timeout per policy: ACT 1.5h, VQBET/TDMPC 2h, Diffusion/Pi0Fast 4h, Pi0/SmolVLA 6h (capped in FastAPI before dispatch).
- Upload: `HfApi.create_repo(..., exist_ok=True)` → `upload_large_folder()` of `output_dir/checkpoints/last/pretrained_model` (or fallback rglob) → `repo_info()` verification.

### Stalled-worker detection (`routes/training.py:260–277`)
API-side: if `status='running'` and `last_progress_at` > `STALLED_WORKER_MINUTES` (default 15), cancel + mark failed even if Modal still reports IN_PROGRESS. Prevents zombie GPUs.

### Realtime (`supabase/006_loss_history.sql`)
`public.trainings` added to Supabase `supabase_realtime` publication. Frontend listens via `postgres_changes` — no polling needed on subscribed sessions. Loss-history column downsamples at Postgres level when >300 entries (keep first + 199 evenly spaced + last 100).

### `ALLOWED_POLICIES` (split-brain)
- Build-time on React: `REACT_APP_ALLOWED_POLICIES=act` (student) — filters dropdown client-side.
- Runtime on FastAPI: `ALLOWED_POLICIES=act` — raises 400 if `model_type not in set`. Defense-in-depth.
- Admin/dev deployments just unset or comma-list both.

### Secrets
- Railway: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_ANON_KEY`, `MODAL_TOKEN_ID/SECRET`, `MODAL_TRAINING_APP_NAME=edubotics-training`, `MODAL_TRAINING_FUNCTION_NAME=train`, `HF_TOKEN`, `ALLOWED_ORIGINS`, `ALLOWED_POLICIES`, `STALLED_WORKER_MINUTES`, `MAX_TRAINING_STEPS`.
- Modal Secret `edubotics-training-secrets` injects `SUPABASE_URL` + `SUPABASE_ANON_KEY` + `HF_TOKEN` into the worker.

### Migrations
- `migration.sql` — users (+credits), trainings, RLS, `get_remaining_credits()`, `start_training_safe()`.
- `002_accounts.sql` — role enum, classrooms, `adjust_student_credits()`, `get_teacher_credit_summary()`, 30-student trigger.
- `003_lessons_and_notes.sql` — superseded (kept only because 004's drops assume it ran).
- `004_progress_entries.sql` — daily per-classroom-or-per-student note log, partial UNIQUE indexes per scope/day, `touch_updated_at()` trigger.
- `005_cloud_job_id.sql` — `runpod_job_id → cloud_job_id` rename (vendor-neutralize during RunPod→Modal cutover).
- `006_loss_history.sql` — loss array column + Postgres-side downsampling + realtime publication.

Bootstrap once: `python scripts/bootstrap_admin.py --username admin --full-name "Sven"`.

---

## 8. Inference (Trained Policy → Follower Arm)

Goal: pick a trained model in the UI → policy loads in `physical_ai_server` → every tick, images + state → policy → JointTrajectory → follower moves.

### Trigger
React → `/task/command` with `START_INFERENCE` + `task_info.policy_path` (local filesystem path, not HF URL). `physical_ai_server.py:786` → `user_interaction_callback` validates, sets `on_inference=True`, starts the 30 Hz ROS timer callback `_inference_timer_callback` (line 517).

### Model picker
React calls `GetSavedPolicyList.srv` → server lists `~/.cache/huggingface/hub/models--*/snapshots/*/pretrained_model/`. Policy path is always local. On first tick `PreTrainedPolicy.from_pretrained(policy_path)` downloads on-demand from HF if missing (no prefetch, no volume mount). Respects `HF_HOME`.

### Overlay validation at load (`overlays/inference_manager.py:93–103`)
Reads `config.input_features` keys matching `observation.images.*`, stores as `_expected_image_keys`. For OMX-F this is `{'observation.images.gripper', 'observation.images.scene'}`.

### Per-tick (synchronous on main ROS executor)
1. `communicator.get_latest_data()` waits for all topics (5s timeout).
2. `convert_msgs_to_raw_datas()` → RGB uint8 HWC images + float32 state [6].
3. Lazy `load_policy()` on first tick (moves weights to GPU).
4. **Camera name exact-match** (overlay lines 166–177):
   ```python
   provided = {f'observation.images.{k}' for k in images.keys()}
   missing = set(self._expected_image_keys) - provided
   if missing:
       raise RuntimeError(
         "Das Modell erwartet die Kameras {expected}, "
         "aber verbunden sind nur {provided}...")
   ```
   Prevents silent alphabetical remapping.
5. `_preprocess(images, state)` (overlay lines 220–233): each image `torch.from_numpy` → `/255` → `permute(2,0,1)` → `unsqueeze(0)`, keyed as `observation.images.{name}`. State → float32 tensor → batch.
6. `policy.select_action(observation)` under `torch.inference_mode()`.
7. `data_converter.tensor_array2joint_msgs(action, …)` builds `JointTrajectory(joint_names=[joint1..5, gripper_joint_1], points=[JointTrajectoryPoint(positions=action)])`.
8. `communicator.publish_action(msg)` → `/arm_controller/follow_joint_trajectory` (the follower's action topic).

### Gripper
There is **no separate gripper action call**. `gripper_joint_1` is the 6th element of the same action array and rides the same `JointTrajectory`. The follower's controller handles all 6 DoF.

### Stale-camera watchdog (overlay lines 122–148)
Hashes each arriving frame. If unchanged for >2s, warns every 5s in German. Doesn't stop inference — but it's your signal that a USB camera stalled.

### Image-resolution guard (overlay lines 183–196)
Reads expected `(H,W)` from config, validates against incoming frame, German error on mismatch.

### Threading
Inference is **single-threaded** on the ROS2 executor — no worker thread. A slow topic blocks the loop. Training, by contrast, does spawn a daemon.

### What the two patches fix
`patches/fix_server_inference.py` fixes `server_inference.py` (ZMQ-server variant, different code path from the main timer-based inference): (a) init `self._endpoints = {}` before first `register_endpoint()`, (b) remove duplicate `InferenceManager` construction. The main inference path (the one actually used) lives in `inference_manager.py` and is fine once the overlay replaces it.

---

## 9. Common Gotchas (one-screen reference)

| Symptom | Cause | Fix |
|---------|-------|-----|
| dockerd doesn't start in WSL2 | boot PATH is empty | `start-dockerd.sh` re-exports PATH |
| compose fails "trying to mount a directory onto a file" | `/etc/timezone` / `/etc/localtime` missing | `tzdata` + real files in rootfs |
| Multi-layer pull corrupts on large image | Docker 29.x containerd-snapshotter | pin Docker 27.5.1, `containerd-snapshotter: false` |
| s6 service silently disabled | base image leaves it in `.d/` but not enabled | `.s6-keep` empty-file mount |
| s6 rejects `longrun\r` | Windows Git CRLF | Dockerfile `sed -i 's/\r//g'` |
| Modal worker uses wrong torch | default pip picks cu130 | force `index_url=.../whl/cu121` |
| Inference silently swaps cameras | upstream alphabetical remap | overlay enforces exact name match |
| Empty JointTrajectory crashes recording | upstream accepts it silently | overlay raises German RuntimeError |
| "longrun ready" during install but dockerd hangs | WSL2 just booted | poll `docker info` 60s, then force-`start-dockerd.sh` |
| Stuck "running" training | Modal died mid-run | FastAPI stalled-worker sweep (15 min default) |

---

## 10. Files to read first when debugging a specific stage

| Problem class | First file | Then |
|---------------|------------|------|
| Install fails | `installer/robotis_ai_setup.iss` + matching `.ps1` | `wsl_rootfs/build_rootfs.sh`, `daemon.json` |
| GUI frozen / USB missing | `gui/app/gui_app.py:_start_environment` | `docker_manager._docker_cmd`, `device_manager.scan_and_identify_arms` |
| Arm doesn't move on startup | `docker/open_manipulator/entrypoint_omx.sh` | `omx_f_follower_ai.launch.py` remap line |
| Compose error | `docker/docker-compose.yml` | `docker/physical_ai_server/Dockerfile` overlays |
| Recording crashes | `overlays/data_manager.py`, `overlays/data_converter.py` | `physical_ai_server.py:user_interaction_callback` |
| React can't see ROS | `src/utils/rosConnectionManager.js` | compose port 9090, nginx.conf |
| `/trainings/start` 400 | `cloud_training_api/app/routes/training.py` | `services/modal_client.py`, `start_training_safe` RPC |
| Training stalls | `modal_training/training_handler.run_training` | FastAPI stalled-sweep in `_sync_modal_status` |
| Inference crashes on "erwartet Kameras" | `overlays/inference_manager.py:166` | `omx_f_config.yaml` camera names, policy `config.json` |

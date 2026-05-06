# 03 — Glossary

> **What this file is:** vocabulary lookup. Read when you hit a term you don't recognize.
> Terms are organized by category. Search this file with Ctrl+F.

---

## Robot hardware

- **OMX** — OpenMANIPULATOR-X. The 6-DoF educational robot arm by ROBOTIS. Two arms in this system: leader (passive teleop) + follower (driven).
- **OMX-F** — Follower arm. Servo IDs **11–16**. Driven (position control on all joints + current-controlled gripper Op Mode 5, **350 mA** limit).
- **OMX-L** — Leader arm. Servo IDs **1–6**. Joints 1–5 read-only (gravity comp + friction tuning). Joint 6 (gripper_joint_1) is current-controlled (Op Mode 5, **300 mA** limit, reverse direction).
- **Dynamixel** — ROBOTIS servo line. Uses Protocol 2.0 over a 1 Mbps RS-485 bus. SDK: `dynamixel-sdk==4.0.3`.
- **OpenRB-150** — USB-to-RS-485 bridge board, VID `2F5D`, PID `0103` (alt firmware: `2202`). Each arm has its own; identified by `identify_arm.py`.
- **gripper_joint_1** — 6th joint, controls the gripper opening. Current-controlled with Op Mode 5 so collisions don't stall the servo.
- **joint1..5** — Arm rotation joints. Position-controlled.
- **`/dev/serial/by-id/...`** — udev-stable serial device path. The GUI passes these to compose as `FOLLOWER_PORT` / `LEADER_PORT` so device order is stable across reboots/replug.

---

## ROS2

- **ROS2 Jazzy** — ROS distribution used (released May 2024, Ubuntu 22.04). Successor to Humble.
- **ROS_DOMAIN_ID** — DDS isolation key. Two nodes with the same domain see each other; different domains are isolated. Default `30`. Same-LAN classrooms with default = cross-talk; mitigated by `_resolve_ros_domain_id()` in `gui/app/config_generator.py` which derives a per-machine UUID hash mod 233.
- **DDS** — Data Distribution Service, the underlying ROS2 transport. Discovery via UDP multicast on bridge networks (known to be flaky — see [§3.4 of known-issues](21-known-issues.md)).
- **rosbridge** — WebSocket bridge from browser-side roslib to ROS2. Listens on TCP `9090`.
- **Behavior tree (BT)** — XML-defined node tree for sequencing actions. Lives in `physical_ai_bt/`.
- **The magic remap** — `omx_f_follower_ai.launch.py:144`: `/arm_controller/joint_trajectory → /leader/joint_trajectory`. Means "anyone publishing to `/leader/joint_trajectory` drives the follower." Used for both teleoperation (leader observed positions) and inference (predicted actions).
- **`/joint_states`** — Follower JointState topic. Subscribed by physical_ai_server for recording + inference.
- **`/leader/joint_trajectory`** — Leader JointTrajectory topic. Action target for the follower (after remap).
- **TaskInfo / TaskStatus / TrainingInfo / TrainingStatus** — Custom ROS messages defined in `physical_ai_interfaces/`.
- **SendCommand.srv** — Custom ROS service: command codes IDLE=0, START_RECORD=1, START_INFERENCE=2, STOP=3, MOVE_TO_NEXT=4, RERECORD=5, FINISH=6, SKIP_TASK=7.

---

## Docker / WSL2

- **EduBotics distro** — The bundled WSL2 Ubuntu 22.04 distro, name `EduBotics`. Imported from `edubotics-rootfs.tar.gz`. Hosts `dockerd`. **Replaces Docker Desktop entirely.**
- **`wsl -d EduBotics --`** — Prefix for every Docker command from the GUI. `_docker_cmd()` in `gui/app/docker_manager.py` wraps it.
- **`usbipd`** — Tool that forwards USB devices from Windows into WSL2 distros. Pinned to v5.3.0 with SHA256 verification. VID `2F5D` policies preconfigured.
- **`.s6-keep`** — Empty 1-byte marker file mounted at `/etc/s6-overlay/s6-rc.d/user/contents.d/physical_ai_server`. **Required** to enable the s6 service in physical_ai_server. Without the mount, the container starts but the ROS node never runs.
- **s6-overlay** — Init system used inside `physical_ai_server`. Reads service definitions from `/etc/s6-overlay/s6-rc.d/`. Rejects `longrun\r` (CRLF) — Dockerfile strips `\r` from text files.
- **CRLF** — Carriage Return + Line Feed. Windows Git default. The `physical_ai_server` and `open_manipulator` Dockerfiles `sed -i 's/\r$//'` known text files to strip them.
- **`ros_net`** — Bridge network in `docker-compose.yml`. All 3 containers join it.
- **Privileged container** — `privileged: true` in compose. Required for `open_manipulator` (raw USB access) and `physical_ai_server` (DDS shared memory + cap on resources).
- **`docker-compose.gpu.yml`** — Overlay file adding `runtime: nvidia` + GPU device reservation only for `physical_ai_server`. Layered on top of base compose with `-f docker-compose.yml -f docker-compose.gpu.yml`.

---

## Build chain

- **`build-images.sh`** — `docker/build-images.sh`. Builds all 3 images. `REGISTRY=nettername` is the default registry prefix.
- **`apply_overlay`** — Shell function in `physical_ai_server/Dockerfile` that finds a target file in `/root/ros2_ws`, sha256-verifies the source overlay, copies, then sha256-verifies the result. **Fails loudly if target not found** (M14 commitment).
- **Overlays** — 5 files in `docker/physical_ai_server/overlays/` (`inference_manager.py`, `data_manager.py`, `data_converter.py`, `omx_f_config.yaml`, `physical_ai_server.py`) and 2 in `docker/open_manipulator/overlays/` (`omx_f.ros2_control.xacro`, `hardware_controller_manager.yaml`). Replace upstream files at build time.
- **Patches** — `docker/physical_ai_server/patches/fix_server_inference.py`. Applied before overlays. Self-verifies (exit 2 or 3 on no-op).
- **Base images (immutable pins)** —
  - `robotis/physical-ai-server:amd64-0.8.2`
  - `robotis/open-manipulator:amd64-4.1.4`
  - `nvidia/cuda:12.1.1-devel-ubuntu22.04` (Modal training)
- **`bump-upstream-digests.sh`** — Helper that runs `docker buildx imagetools inspect` to print SHA256 digests + sed commands for upgrading pins. Manual review required.

---

## Cloud / SaaS

- **Railway** — PaaS hosting the FastAPI cloud_training_api + the React web build. Domain `scintillating-empathy-production-9efd.up.railway.app` for the API.
- **Modal** — Serverless GPU compute. Workspace `svendanilborodun`, app `edubotics-training`, function `train`. Image pulls from inside Modal. NVIDIA L4 (24 GB), 7h hard timeout, `min_containers=0` (cold-starts on demand).
- **Modal Secret** — Modal's encrypted env-var bundle. `edubotics-training-secrets` injects `SUPABASE_URL` + `SUPABASE_ANON_KEY` + `HF_TOKEN` into the worker.
- **Supabase** — Postgres + Auth + Realtime + Storage. Project `fnnbysrjkfugsqzwcksd`. **EU vs US region needs verification** ([§2.9 of known-issues](21-known-issues.md)).
- **HuggingFace** — Hosts datasets + trained models. Org `edubotics`. Datasets are **public by default** (known issue).
- **Modal MCP server** — `modal_mcp/mcp_server_stateless.py`. App `example-mcp-server-stateless`. Bearer-guarded (`MCP_BEARER_TOKEN` from secret `mcp-edubotics`). Exposes 6 tools.

---

## Auth / accounts

- **Synthetic email** — `{username}@edubotics.local`. Domain doesn't exist; never receives email. Lets us use Supabase Auth's email/password flow with username login.
- **Service-role key** — `SUPABASE_SERVICE_ROLE_KEY`. Bypasses RLS. Used by Railway FastAPI. **Never** ship to React or students.
- **Anon key** — `SUPABASE_ANON_KEY`. RLS-bound. Baked into React bundle. Modal worker also uses it for scoped RPC writes.
- **Worker token** — Per-row UUID in `trainings.worker_token`. Modal worker can only update its own row via `update_training_progress(p_token, ...)`. Nulled on terminal status.
- **3 roles** — `admin`, `teacher`, `student`. Defined in `user_role` enum (migration 002).
- **Credit hierarchy** — Admin grants credits to teacher's `training_credits` field (the "pool"). Teacher allocates to students via `adjust_student_credits` RPC. Student spends one credit per non-failed/canceled training.
- **Bootstrap admin** — `python scripts/bootstrap_admin.py --username admin --full-name "Sven"`. Run once to create the first admin.
- **`_assert_classroom_owned()` / `_assert_student_owned()` / `_assert_entry_owned()`** — Python ownership-check helpers in `cloud_training_api/app/routes/teacher.py`. **Skipping one = silent IDOR.**
- **`get_current_teacher()` / `get_current_admin()` / `get_current_profile()`** — FastAPI dependencies in `auth.py`. Resolve role + return profile dict.

---

## Training pipeline

- **LeRobot** — HuggingFace's robotics ML library. Pinned to v0.2.0 commit `989f3d05ba47f872d75c587e76838e9cc574857a` (full SHA, "[Async Inference] Merge Protos & refactoring (#1480)", 2025-07-23). Lives byte-identical in 3 places: `physical_ai_tools/lerobot/` (snapshot), Modal image (`lerobot[pi0] @ git+https://github.com/huggingface/lerobot.git@989f3d05ba47f872…`), and the base `physical-ai-server:amd64-0.8.2` image (built from `ROBOTIS-GIT/physical_ai_tools` whose `lerobot` git submodule is pinned to upstream `huggingface/lerobot.git` at this SHA — there is no "jazzy" branch; the submodule resolves by frozen SHA).
- **`codebase_version: "v2.1"`** — String written into `meta/info.json` at recording time. Modal preflight enforces match. Bumping requires a migration script for existing datasets.
- **`start_training_safe`** — Atomic Postgres RPC. `SELECT ... FOR UPDATE` user row → count active trainings → check credits → insert. Raises P0003 on insufficient credits or P0002 on user not found.
- **`update_training_progress`** — RPC the Modal worker calls every progress tick. Validates worker_token. On terminal status, nulls token + sets `terminated_at`. Postgres-side downsampling (006) keeps `loss_history` array ≤ 300 points.
- **`stalled-worker sweep`** — `_sync_modal_status` in `routes/training.py`. If `last_progress_at` &gt; `STALLED_WORKER_MINUTES` (default 15), cancels Modal job + marks failed.
- **`dedupe window`** — 60 s. `_find_recent_duplicate` in `routes/training.py`. Same `(user_id, dataset_name, model_type, training_params)` within 60 s returns existing row. Excludes failed/canceled rows so retry works immediately.
- **`POLICY_MAX_TIMEOUT_HOURS`** — Per-policy timeout cap applied before Modal dispatch. ACT 1.5h, VQBET/TDMPC 2h, Diffusion/Pi0Fast 4h, Pi0/SmolVLA 6h. Overrides user request if higher.
- **`ALLOWED_POLICIES`** — Set of allowed policy strings. Defaults to `act` for student build (`REACT_APP_ALLOWED_POLICIES=act`); FastAPI also enforces (`ALLOWED_POLICIES` env var).

---

## Inference pipeline

- **Camera exact-match** — Overlay `inference_manager.py` rejects mismatched camera names. Prevents the silent alphabetical remap that upstream LeRobot performs. Error message in German: "Das Modell erwartet die Kameras {expected}, aber verbunden sind nur {provided}".
- **Stale-camera halt** — Overlay watchdog: hashes 4 sparse 256-byte slices per image. If frozen &gt; 5 s, returns None (skip tick) so the arm doesn't move on stale visual input.
- **Safety envelope** — Overlay-added: NaN/inf reject + per-joint clamp + per-tick velocity cap. Configurable via `set_action_limits()`.
- **`config.json`** — Output of LeRobot training. Lives at `pretrained_model/config.json`. Inference reads `input_features` to determine expected camera keys (e.g., `observation.images.gripper`) and shapes.
- **`policy_path`** — Local filesystem path to a checkpoint. NOT an HF URL. React passes it via TaskInfo. Always under `~/.cache/huggingface/hub/models--*/snapshots/*/pretrained_model/`.

---

## React app

- **`StudentApp` vs `WebApp`** — Two top-level components in `src/`. Selected by `APP_MODE` (build-time `REACT_APP_MODE`).
- **`?cloud=1`** — URL query param the GUI appends when in cloud-only mode. Tells StudentApp to skip rosbridge initialization.
- **`useSupabaseTrainings`** — Hook that combines Supabase Realtime channel + 30 s poll fallback. Subscribes to `public.trainings` filtered by user_id.
- **`useRosServiceCaller`** — Hook returning 15 bound ROS service callers (sendRecordCommand, setRobotType, getPolicyList, etc.). Wraps roslib service calls with a 10 s timeout.
- **rosConnectionManager** — Singleton in `utils/rosConnectionManager.js`. Reconnect: exp backoff, 30 attempts, 30 s cap.
- **`/version.json`** — JSON with `buildId` (UTC + git SHA). React polls every 30 s, reloads on mismatch. `Cache-Control: no-store` in nginx.

---

## Windows GUI

- **`EduBotics.exe`** — PyInstaller bundle of `gui/`. Two modes: default (tkinter wizard) or `--webview --url ...` (pywebview subprocess). Dispatched in `main.py`.
- **`%LOCALAPPDATA%\EduBotics\.env`** — User-writable `.env` (moved out of Program Files in v2.2.1). Generated by GUI per startup.
- **`finalize_install.ps1`** — Post-reboot script: deletes `.reboot_required`, runs `import_edubotics_wsl.ps1` + `pull_images.ps1`. Invoked by GUI with UAC elevation when it detects the marker.
- **`_runtime_missing`** — Threading event in `webview_window.py`. Set when WebView2 subprocess exits with non-zero (Edge WebView2 not installed). Triggers `_webview_fallback()` to open system browser.
- **Pull stall watchdog** — Logic in `gui/app/docker_manager.py:_pull_one_image`. Monitors stdout-line-rate AND `/var/lib/docker/overlay2` disk growth (10 MB / 20 s). On 600 s stall, kills dockerd + retries.

---

## Files / directories

- **`Testre/`** — The monorepo root.
- **`robotis_ai_setup/`** — Our custom code (everything we wrote).
- **`physical_ai_tools/`, `open_manipulator/`** — ROBOTIS upstream, absorbed into the repo.
- **`_upstream/`** — Read-only reference copies of original ROBOTIS upstream. Kept for diffs/blame, NOT used in builds.
- **`physical_ai_tools/lerobot/`** — LeRobot v0.2.0 snapshot, byte-identical to upstream. NOT modified by overlays.
- **`context/`** — These docs.

---

## Project-specific abbreviations

- **DSGVO** — Datenschutz-Grundverordnung. German for GDPR.
- **EduBotics** — Product name (and the WSL2 distro name).
- **`P00xx` codes** — Custom Postgres error codes raised by RPCs:
  - **P0001** — Worker token mismatch (update_training_progress)
  - **P0002** — User not found (start_training_safe)
  - **P0003** — Insufficient credits (start_training_safe)
  - **P0010** — Classroom capacity (max 30) (enforce_classroom_capacity trigger)
  - **P0011** — Student not in teacher's classrooms (adjust_student_credits)
  - **P0012** — New credit amount &lt; used (adjust_student_credits)
  - **P0013** — Negative credits (adjust_student_credits)
  - **P0014** — Teacher pool insufficient (adjust_student_credits)

---

**Last verified:** 2026-05-04. Add new terms here as they arise.

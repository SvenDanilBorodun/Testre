# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Recent changes — v2.5.0 (2026-05-28)

* **LeRobot bumped v0.2.0 (SHA `989f3d05`) → v0.5.1 (PyPI pin).** Adopts upstream `streaming_encoding=True` (replaces our v2.4 JPEG-in-RAM hack), the `predict_action` + `make_pre_post_processors` inference pipeline (replaces bare `select_action` which returns garbage without the processors), and the new `pi05` (Pi-0.5) policy. Dataset codebase_version `v2.1` → `v3.0`. Existing student v2.1 datasets are NOT compatible — students re-record. Pre-v2.5.0 trained checkpoints lack `policy_preprocessor.json` / `policy_postprocessor.json` and cannot be loaded by the new inference path — students re-train. See commits `7842c81`..`<current HEAD>` for the 5-phase rollout.
* **On-device training deleted.** Modal Cloud is the only training path. `physical_ai_server/training/` and `physical_ai_server/evaluation/` directories are gone; `user_training_interaction_callback` returns a German "Cloud-only" stub.
* **SmolVLA hidden on aarch64 hosts.** Upstream bug #3636 (Jetson Orin SmolVLA produces wrong actions vs x86_64) gates SmolVLA out **server-side** in `inference_manager.py::get_available_policies()` (`platform.machine() == 'aarch64'`) — NOT in the React dropdown. The server gate is stronger: `validate_policy()` also rejects a SmolVLA checkpoint at inference-load on aarch64. The React `PolicySelector.js` only filters by `REACT_APP_ALLOWED_POLICIES`; it has no `platform.machine()` check.
* **Recording state machine ported to the v0.5.1 writer model (v2.5.0 follow-up).** LeRobot v0.5.1 moved the in-flight `episode_buffer`, `start_image_writer`, and the streaming encoder off `LeRobotDataset` onto `self.writer` (`DatasetWriter`). `data_manager.py` still uses the pre-v0.5.1 call shapes, so `LeRobotDatasetWrapper` (overlay + source mirror, kept byte-identical) now bridges them: an `episode_buffer` property (get/set → `writer.episode_buffer`; setting `None` is safe — upstream `add_frame` lazily recreates it), an `add_frame(frame, task=None)` override that folds a legacy 2nd-positional `task` into the dict, and a `start_image_writer` shim that forwards to the writer. Without these, the first episode `save()` raised `AttributeError`/`TypeError` and no dataset was ever produced. The v2.1-layout video verifier in `data_manager._verify_saved_video_files` was rewritten for the v3.0 concatenated layout (`videos/<key>/chunk-NNN/file-NNN.mp4`) — the old per-episode path check false-positived "neu aufnehmen" on every episode. Regression coverage: `robotis_ai_setup/tests/test_lerobot_dataset_wrapper.py` (the migration deleted the only recording test, `test_lerobot_stats_contract.py`, with no replacement). LeRobot extras corrected `[pi0,…]` → `[pi,…]` (v0.5.1 has no `pi0` extra) across all 3 install sites + the thin overlay. React `REACT_APP_ALLOWED_POLICIES` default fixed to `…,pi0_fast,pi05,…` (was `pi0fast`, no `pi05`) so the teacher-web dropdown shows the renamed/new policies.

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
- **ros2_control YAML**: 100 Hz, JointTrajectoryController 2.0 rad in-flight trajectory tolerance (effectively off — the safety floor is the Dynamixel current limit above, not the controller tolerance), 0.10 rad arm goal tolerance, 0.50 rad gripper goal tolerance, goal_time 5.0 s. The 2.0 / 0.10 / 0.50 / 5.0 values were bumped from upstream's 0.15 / 0.05 / 0.10 / 1.0 on 2026-05-18 — the tight upstream values caused the follower's `arm_controller` to abort the 3 s quintic boot-sync mid-flight in classroom poses, looping docker compose into a restart-storm. See the inline comment in `robotis_ai_setup/docker/open_manipulator/overlays/omx_f_hardware_controller_manager.yaml`.
- **SIGTERM/SIGINT torque-disable** in `docker/open_manipulator/entrypoint_omx.sh::disable_torque()`
- **Phase-4 post-sync verification** in `entrypoint_omx.sh` — 0.30 rad tolerance after the 3 s quintic ramp; soft-fail `[WARN]` on mismatch (bumped from 0.08 rad / hard-exit 2 on 2026-05-18 after the tight upstream values triggered false-positive Phase-4 aborts on classroom start poses). Only the camera-disambiguation guard (Section "Two identical-serial cameras…") still hard-exits.
- Recording-side guards are warning-only — episodes always complete (stale-camera 5 s, timestamp-gap > 2× expected_dt, video-file verifier, usb_cam Hz)

If you genuinely need to reintroduce a software safety guard that modifies the pipeline, **stop and ask the user**.

### 3. Overlays must fail loudly on missing target, no-op when already applied

`apply_overlay()` in `docker/physical_ai_server/Dockerfile` and `docker/open_manipulator/Dockerfile` does sha256 pre/post copy verification. If the upstream file is missing, build aborts. If the target is already byte-identical, it logs `Overlay already in place` and continues (idempotent). When adding an overlay you **must** add it to the `apply_overlay` chain with a unique path filter — without that, the source edit lives in the repo but never reaches the image.

`patches/fix_server_inference.py` self-verifies and exits 2/3 on no-op; CI's `overlay-guard` job tests this with a synthetic input.

LeRobot itself is **not** overlaid — it's pip-installed from PyPI at version `0.5.1` in the base Dockerfiles. The 3-site version contract (Rule §5) keeps Modal, the amd64 Dockerfile, and the arm64 Dockerfile in lockstep; no per-file overlays needed.

### 4. Service-role key bypasses RLS — authorization is your job

Every Supabase query in `cloud_training_api/app/` runs as **service-role** via `app/services/supabase_client.py::get_supabase()` (lazy singleton, fails fast at startup if `SUPABASE_URL` or `SUPABASE_SERVICE_ROLE_KEY` empty). RLS policies exist for defense-in-depth but are dormant under service-role.

Every endpoint that touches another user's data **must** call one of:
- `_assert_classroom_owned()` / `_assert_student_owned()` / `_assert_entry_owned()` (in `routes/teacher.py`)
- `_assert_workflow_owned()` (in `routes/workflows.py`)
- `_assert_workgroup_owned()` / `_assert_workgroup_in_classroom()` (in `routes/workgroups.py`)

**One missed assertion = silent IDOR.** RLS will not catch it.

The Modal worker uses the **anon key** + per-row `worker_token` (UUID). Its only DB write surface is the `update_training_progress(p_token, …)` RPC, guarded by migration `010_progress_terminal_guard.sql` so a worker can't overwrite a `canceled` row with `succeeded`.

### 5. Don't introduce drift between the LeRobot pinning sites

The LeRobot **version pin** `0.5.1` must agree across:
- `robotis_ai_setup/modal_training/modal_app.py` constant `LEROBOT_VERSION`
- `physical_ai_tools/physical_ai_server/Dockerfile.amd64` `pip install "lerobot[pi,smolvla,peft]==0.5.1"` (the extra is `pi`, not `pi0` — v0.5.1 renamed it; `pi0` was a non-fatal pip warning)
- `physical_ai_tools/physical_ai_server/Dockerfile.arm64` same install line under the Jetson AI Lab pip index

Bumping LeRobot is a **3-place change in one PR**.

`meta/info.json` `codebase_version: "v3.0"` and the Modal preflight constant `EXPECTED_CODEBASE_VERSION="v3.0"` (`training_handler.py:40`) are **derived** checks — they should match the LeRobot version's expected codebase format, but they are not pin sites. If LeRobot v0.5.x ever bumps codebase_version to v3.1, those two consts move together with the LeRobot pin in the same PR.

**One PyTorch surface (torch 2.7.x), three CUDA channels — by design.** Don't try to "unify" CUDA versions across surfaces; each surface picks the channel matching its hardware.

- **Modal worker** (`modal_training/modal_app.py`): `nvidia/cuda:12.6.1-devel-ubuntu22.04` + `add_python="3.12"` + `pip_install("torch==2.7.0", "torchvision==0.22.0", index_url="https://download.pytorch.org/whl/cu126", extra_options="--force-reinstall")` + `pip uninstall -y torchcodec`. cu126 chosen because v0.5.1's torch range is `>=2.7,<2.11.0` and torchvision is `>=0.22.0,<0.26.0`; cu126 ships compatible wheels. Modal L4 GPUs on R550+ drivers handle CUDA 12.6 fine.
- **Student `physical_ai_server` image** (both `physical_ai_tools/physical_ai_server/Dockerfile.amd64` and `Dockerfile.arm64`): base image `robotis/ros:jazzy-ros-base-torch2.7.0-cuda12.8.0` — torch 2.7.0+cu128 inherited from the Robotis upstream base. LeRobot is pip-installed from PyPI v0.5.1; the inherited torch is preserved (LeRobot's pyproject torch pin doesn't get re-installed because the install resolves the existing torch as satisfying the `>=2.7` range). Students run on Windows with no GPU; CUDA libs are dead weight but not active.
- **Jetson Orin Nano classroom agent**: arm64 `Dockerfile.arm64` sets `PIP_INDEX_URL=https://pypi.jetson-ai-lab.io/jp6/cu126/+simple` for the LeRobot install layer; torch stays at 2.7.0+cu128 from the Robotis base. The Jetson AI Lab index resolves the arm64-native heavy transitive deps (transformers VLM components, etc.); PyPI is the extra-index fallback.

The LeRobot 3-site contract is now trust-on-PR-review (the historical `lerobot-sha-check` job is gone; no SHA pins remain in either Dockerfile). The build-time policy-import smoke test in all 3 Dockerfiles (asserts every modeling module imports + `CODEBASE_VERSION == 'v3.0'`) catches the most common drift class: a future LeRobot release reorganising module paths.

### 6. CI/CD deploys

Six workflows in `.github/workflows/` are the canonical path for five surfaces:

- `supabase-migrate.yml` — Supabase migrations (`supabase db push` against project `fnnbysrjkfugsqzwcksd`). Password extraction uses `urllib.parse` (was sed, fixed in `16b8378`).
- `railway-deploy-cloud-api.yml` — FastAPI to `scintillating-empathy` service. Health-gate polls `/health` and asserts `body.commit == github.sha` (no more "old pod returns 200" false-positives).
- `railway-deploy-teacher-web.yml` — React SPA to `teacher-web` service. Health-gate polls `/version.json` and asserts the buildId contains the new SHA. `railway.json` `buildArgs` interpolates `$RAILWAY_GIT_COMMIT_SHA` into `REACT_APP_BUILD_ID` (defeats the frozen-service-variable bug surfaced 2026-05).
- `docker-publish.yml` — three production images to `nettername/*` Docker Hub.
- `release-installer.yml` — Windows `.exe` build. **`workflow_call`-only**; invoked from `release.yml` as W5 after `docker:` completes. No longer races on `push.tags`.
- `release.yml` — top-level dispatcher that fires all five in the golden order on tag pushes (W1 supabase → W2 cloud-api → W3 teacher-web → W4 docker → W5 installer).

**Applying migrations.** `supabase db push --linked --password "$DB_PASSWORD"` from terminal works; Claude's MCP `apply_migration` tool also works. **Important quirk**: MCP re-stamps the version prefix into the ledger, so on-disk filenames must be renamed to the MCP-stamped value after applying, otherwise the next `supabase db push` sees ghost-unapplied migrations and crashes on `ALTER TABLE … ADD COLUMN`. (Fixed once in commit `de7d635`; pattern to watch for.)

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

- **Native camera capture bridge (WSL2/Windows student path) — cameras bypass usbipd entirely.** The WSL2 `vhci_hcd` USB/IP bridge caps each forwarded UVC camera at ~6-10 Hz — a per-device isochronous round-trip latency limit benchmarked 2026-05-23 (NOT contention, bandwidth, CPU, or pixel format: single-cam == dual-cam rate, 320×240 gives only ~35% more, framerate-request and `io_method` are ignored). The same camera traffic on the shared bridge jitters the 100 Hz Dynamixel serial reads (the `SYNC_READ_FAIL`/`BULK_READ_FAIL` storm). Fix: the Windows GUI (`EduBotics.exe`) captures both cameras **natively** via OpenCV DirectShow (`CAP_DSHOW`, MJPG fourcc, 640×480×30, one free-running latest-frame-wins thread per camera — phosphobot's model) and streams JPEG frames over a localhost TCP socket (`127.0.0.1:5557`, single multiplexed connection, wire format `[u8 cam_id][u32 BE jpeg_len][u64 BE capture_ns][jpeg]`) into the open_manipulator container. `docker/open_manipulator/camera_ingest_node.py` (TCP server, `COPY`'d into the image like `identify_arm.py` — a new file, NOT an `apply_overlay` target) republishes them as `sensor_msgs/CompressedImage` on `/gripper/image_raw/compressed` + `/scene/image_raw/compressed` (RELIABLE/KEEP_LAST QoS — `web_video_server`'s subscriber is RELIABLE-only, so a BEST_EFFORT publisher silently drops every preview frame with "requesting incompatible QoS"; `format="jpeg"`; `header.stamp` = the GUI's wire `capture_ns`, falling back to the node clock only when the client sends `0`) — the exact topics usb_cam used, so recorder/inference/web_video_server consume them unchanged (LeRobot timestamps are already regularized to `frame_index/fps` at save in `lerobot_dataset_wrapper.py`). Cameras are NO LONGER usbipd-attached on the student PC (only the 2 servos are) → the Dynamixel bus contention disappears too. Gated by `EDUBOTICS_CAMERA_SOURCE` (auto-default `native_bridge` on WSL2, `usb_cam` on Jetson/native; `entrypoint_omx.sh` Phase-4 + the open_manipulator healthcheck both branch on it). Capture runs in-process in the GUI: `gui/app/camera_bridge.py` + `gui/app/win_camera.py`, started/stopped with the environment in `gui_app.py`; `device_manager.scan_cameras()` and `config_generator` are native-aware (no usbipd camera attach; `CAMERA_DEVICE_*` stay empty). Two identical-serial Innomaker cameras are disambiguated by the student's visual gripper/scene role pick (persisted as the OpenCV index per role for the session). **One-variable rollback:** `EDUBOTICS_CAMERA_SOURCE=usb_cam` in the `.env` reverts to the usb_cam-over-usbipd path below. Compose surface: open_manipulator `ports: 127.0.0.1:5557:5557` + `EDUBOTICS_CAMERA_SOURCE/_INGEST_PORT/_NAMES`.

- **`usb_cam` is now the Jetson / native-Linux path** (and the `EDUBOTICS_CAMERA_SOURCE=usb_cam` rollback fallback on WSL2). On the default WSL2 student path, cameras use the native capture bridge above and usb_cam is **not** launched. When usb_cam *is* used, its **0.8.1 pixel-format selection is split by host USB bridge.** `raw_mjpeg` (the zero-decode pass-through) is structurally broken in usb_cam 0.8.1: the source publishes the camera's MJPG bytes into `sensor_msgs/Image.data` while falsifying `encoding="yuv422"` and `size_in_bytes = height * width * 2 = 614,400` (from `av_device_format: "YUV422P"`). Downstream consumers see ~30 KB of MJPG bytes inside a 600 KB buffer — green tiles, RNG-noise bands, corrupted browser preview. Source: [ros-drivers/usb_cam#346](https://github.com/ros-drivers/usb_cam/issues/346); the bug is in `src/usb_cam.cpp::process_image` which `memcpy`s `m_image.size_in_bytes` bytes instead of `bytes_used`. There is no tagged release that fixes it. `raw_mjpeg` is intentionally NOT supported. The remaining two choices, `yuyv` and `mjpeg2rgb`, are gated on the host USB stack — there is no single right answer.
   - **Classroom Jetson Orin Nano (NATIVE USB host controllers).** `yuyv 640×480×30` is correct: matches the ROBOTIS upstream `camera_usb_cam.launch.py` declared default, zero decode CPU, 18.4 MB/s/cam on a real USB 2.0 isoch budget of ~25 MB/s. Two cameras fit comfortably across two host controllers.
   - **Windows 11 student PC (WSL2 vhci_hcd bridge).** `yuyv` does NOT work for the 2-cam configuration — the vhci_hcd kernel module cannot sustain 2 × 18.4 MB/s = 36.8 MB/s combined uncompressed throughput, and both cameras crash with `VIDIOC_DQBUF: Select timeout` inside ~5 s. The container then enters a docker-compose restart loop. Verified empirically 2026-05-22 on Sven's classroom rig. The fix is `mjpeg2rgb`: the camera hardware JPEG-encodes before the USB transfer (~1 MB/s/cam on the wire), and usb_cam decodes to RGB on the host CPU. Empirical CPU cost on our pinned 0.8.1: ~30 %/cam at 30 Hz (the previously documented "94 % CPU saturation" was measured against an older usb_cam without the libjpeg-turbo fast path).
   - **Mechanism.** Compose's `EDUBOTICS_CAMERA_PIXEL_FORMAT` default is `yuyv` (single source of truth, correct for Jetson). `entrypoint_omx.sh` detects WSL2 via `uname -r | grep -i microsoft` and flips the runtime default to `mjpeg2rgb` ONLY when the operator hasn't explicitly overridden the env var. An operator `.env` value wins over both. The Jetson agent never sees a Microsoft kernel, so its default never flips.
   - Override via `EDUBOTICS_CAMERA_PIXEL_FORMAT=<value>` is supported. `raw_mjpeg` remains unsupported.

- **`EDUBOTICS_*` env vars only reach the container if listed in `docker-compose.yml::environment`.** Compose's `${VAR}` interpolation operates on YAML at config-time, NOT on the container's runtime env. An override in the host `.env` reaches compose's YAML substitution but is invisible to the entrypoint unless explicitly forwarded. The 2026-05 raw_mjpeg-override regression was exactly this: every student container ran with the entrypoint hardcoded default because the `EDUBOTICS_CAMERA_*` keys were never in the `environment:` list. Guarded by `ci.yml::env-forwarding-guard` since v2.3.7. Extended 2026-05-23 to also scan `robotis_ai_setup/docker/physical_ai_server/overlays/**/*.py` and `robotis_ai_setup/docker/open_manipulator/overlays/**/*.py` — overlay modules read `os.environ.get('EDUBOTICS_*')` at import time, so a missing compose-forward silently breaks operator overrides for `EDUBOTICS_CALIB_DIR`, `EDUBOTICS_DETECTOR`, `EDUBOTICS_YOLOX_ONNX`, `EDUBOTICS_DFINE_ONNX` etc. Extended again 2026-05-23 to scan `docker/open_manipulator/camera_ingest_node.py` (the native-bridge ingest node reads `EDUBOTICS_CAMERA_INGEST_PORT` / `EDUBOTICS_CAMERA_NAMES` at startup). Every `EDUBOTICS_*` referenced by `entrypoint_omx.sh`, `start-dockerd.sh`, `camera_ingest_node.py`, OR any overlay `.py` must appear in some compose file's `environment:` list, or CI fails.

- **Aufnahme recording RAM is bounded by upstream `streaming_encoding=True` (v2.5.0 / LeRobot v0.5.1), NOT by a software valve.** `LeRobotDatasetWrapper` sets `streaming_encoding=True` by default (`lerobot_dataset_wrapper.py::_apply_edubotics_defaults`, applied in both `__init__` and `create`); camera frames are fed straight to the ffmpeg streaming encoder as they arrive instead of being decoded to RGB ndarrays and accumulated in `episode_buffer`, so the in-RAM footprint is bounded regardless of episode length and episodes can record arbitrarily long. The whole v2.4 mechanism this replaced is **gone**: the JPEG-in-RAM buffer, the `_episode_image_bytes` counter, the `_buffer_full` flag, the forced save-on-buffer-full, the `EDUBOTICS_MAX_BUFFER_GB` env var (removed from `docker-compose.yml`), and the ~74 s hard episode cap. `lerobot_dataset_wrapper.reset_buffer_accounting()` survives only as a no-op back-compat stub. There is no longer any buffer-full `[WARNUNG]` or 5-minute short-circuit.

- **Roboter Studio is bolted onto `physical_ai_server`, not a separate container.** The Dockerfile (a) rebuilds `physical_ai_interfaces` because the base image predates the new msgs/srvs, (b) installs `opencv-contrib-python==4.10.0.84`, `pupil-apriltags==1.0.4`, `onnxruntime==1.20.1` (CMake 4 compatibility via `ENV CMAKE_POLICY_VERSION_MINIMUM=3.5`), (c) downloads YOLOX-tiny ONNX from a pinned GitHub release URL and SHA-256 verifies `427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7`. Then copies in the `overlays/workflow/` module.

- **`.s6-keep` is load-bearing.** `physical_ai_server` bind-mounts `./physical_ai_server/.s6-keep` (empty 1-byte file) at `/etc/s6-overlay/s6-rc.d/user/contents.d/physical_ai_server:ro`. Remove the mount and the ROS node never starts inside the container even though `s6` reports healthy.

- **Single uvicorn worker on Railway.** The in-process rate limiter requires `uvicorn --workers 1` (now explicit in the Dockerfile CMD — was implicit and silently breakable if `WEB_CONCURRENCY` was set). If you raise it, switch the limiter to Redis or a Postgres advisory lock, and add the same for the dataset reconciliation sweep started in `app/main.py::_start_dataset_sweep` AND the new training-cancel sweep started in `_start_training_cancel_sweep`.

- **Boot-time schema fingerprint.** `cloud_training_api/app/main.py::_validate_required_schema()` probes every table + RPC the routes touch (workflow_versions, tutorial_progress, vision-quota RPCs, jetson RPCs incl. the new `claim_pair_intent` + 5-arg `pair_jetson`, training cancel columns). Railway aborts the deploy if the live Supabase schema is behind the on-disk migrations. Exception messages are scrubbed via `_sanitize_probe_error` (strips `Bearer` / `apikey` tokens) before re-raise — added 2026-05 to close a service-role-key leak surface in CI logs. Override with `EDUBOTICS_SKIP_SCHEMA_CHECK=1` for unit-test contexts only — never on Railway.

- **Two-step Jetson pairing (post-022).** `/teacher/classrooms/{id}/jetson/pair-intent` → `claim_pair_intent` atomically writes `intent_teacher_id` + returns an `intent_token` UUID; then `/jetson/pair` → `pair_jetson` (5-arg, requires the token). A second teacher on the same LAN attempting `/pair-intent` on a claimed code gets P0032 → 409 with German detail; a teacher attempting `/pair` without the matching token gets P0033 → 403. React `PairJetsonModal` does both calls inside one spinner. Migration 022 + commit `16b8378`.

- **Two-phase Modal cancel + `cancel_requested` transitional status (post-023).** `/trainings/cancel` writes `status='cancel_requested'` and only flips to `canceled` after Modal confirms. A background sweep (`training_sweep.py`, 30 s tick) retries Modal cancel up to 5 times. After 5 failures the row flips to `failed` (credit refunds via the existing `status NOT IN ('failed','canceled')` filter; GPU may have run, but a stuck `cancel_requested` is worse). Migration 010's terminal-state guard is unchanged — `cancel_requested` is NOT terminal. Closes the start→cancel×10 cost-bomb.

- **Cloud-API `/health` and `/version` payloads (post-`16b8378`).** `/health` returns `{status, commit, schema_ok, boot_completed_at, version}`. `status="starting"` + HTTP 503 until the module-level `STARTUP_SCHEMA_OK` flag flips (after `include_router`). Both Railway deploy workflows AND any future CI gate poll for `commit == github.sha`, not bare 200 — closes the "old pod returns 200" silent-deploy-failure pattern. `/version` drops the dead `required:true` field (the GUI never read it), returns `null` for missing `GUI_VERSION`/`GUI_DOWNLOAD_URL` instead of 503 (silent-fail was masking misconfig).

- **Two identical-serial cameras anchor by USB topology, not by udev `by-id` (post-2026-05-23).** The two Innomaker U20CAM-720P cameras both report USB serial `SN0001` (Innomaker programs the SAME EEPROM in every batch). udev creates a single `/dev/v4l/by-id/usb-Innomaker_..._SN0001-...` symlink for whichever device enumerated last — both v4l capture nodes claim the same name. Two defences ship together:
  1. `gui/app/wsl_bridge.list_video_devices()` detects by-id collision across the de-duped row set (dedup keys on `v4l2-ctl Bus info`, which IS distinct per port) and downgrades colliding rows to `/dev/v4l/by-path/...` symlinks. by-path is anchored to the USB controller + port (`usb-vhci_hcd.0-1` vs `-2`) so it survives reboot without colliding even on identical-serial cameras.
  2. `entrypoint_omx.sh` adds a `[STOPP]` guard after `readlink -f` resolution: if `CAMERA_DEVICE_1` and `CAMERA_DEVICE_2` both resolve to the same `/dev/videoN`, it hard-exits with German messaging instructing the user to re-run "Hardware neu erkennen". Belt-and-suspenders so a stale `.env` from before the GUI fix lands a loud failure instead of silent gripper↔scene swap (which would corrupt every recorded dataset). Tests in `robotis_ai_setup/tests/test_wsl_bridge_camera_dedup.py`.

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
W1 supabase-migrate ──► W2 railway-deploy-cloud-api ──► W3 railway-deploy-teacher-web ──► W4 docker-publish ──► W5 release-installer
```

For partial changes, push to `main` with changes scoped to one surface and that surface's path-filtered workflow fires by itself. For coordinated whole-stack releases, `git tag vX.Y.Z && git push --tags` runs the full chain via `release.yml`. W5 was previously a standalone `push.tags`-triggered workflow that raced W4 (installer attached to GH Release before Docker images existed → first-boot 404). Since `16b8378` it's `workflow_call`-only, invoked after W4 success.

**One-time operator setup** (after merging this PR):

1. Mint these 12 GHA secrets (Settings → Secrets and variables → Actions): `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`, `RAILWAY_TOKEN`, `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`, `SUPABASE_ACCESS_TOKEN`, `SUPABASE_DB_URL`, `SUPABASE_PROJECT_REF`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_ANON_KEY`, `REACT_APP_CLOUD_API_URL`.
2. Tell the Supabase CLI the baseline is already applied to production:
   ```
   supabase link --project-ref fnnbysrjkfugsqzwcksd
   supabase migration repair --status applied 00000000000000
   ```
3. Enable leaked-password protection in the Supabase Auth dashboard (the security-fixes migration applies the other 10 advisor warnings).

**Bumping product VERSION** is a 4-place change PLUS one auto-COPY: `VERSION` file, `installer/robotis_ai_setup.iss AppVersion`, `gui/app/constants.py` fallback constant, AND the Railway `GUI_VERSION` + `GUI_DOWNLOAD_URL` env vars on the `scintillating-empathy` Cloud API service AFTER the matching `gh release create v<version>` with the `EduBotics_Setup.exe` asset. Without the Railway+GitHub side, the in-tree bump is invisible to existing student installs — the `/version`-poll update gate never fires. The Cloud API Dockerfile now `COPY VERSION ./VERSION` (staged from the repo root by `railway-deploy-cloud-api.yml`'s "Stage VERSION file into build context" step), so `/health.version` and the boot-time GUI_VERSION-vs-VERSION drift warning are automatic — no fifth file.

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
- (v2.5.0 upgrade) Student PCs and classroom Jetsons may have downloaded v2.1-codebase_version datasets / pre-v0.5.1 model checkpoints into `huggingface_cache` / `jetson_huggingface_cache` Docker volumes. After installing v2.5.0, those caches are stale (v2.1 datasets fail Modal preflight, pre-0.5.1 checkpoints lack the `policy_preprocessor.json` / `policy_postprocessor.json` files inference now requires). Wipe via the GUI "Factory Reset" button or `wsl -d EduBotics -- docker volume rm robotis_ai_setup_huggingface_cache` (or the jetson_agent equivalent). Document this in the v2.5.0 GitHub Release note in German for students.
- Force-push, `git reset --hard`, `git clean -fd`, `--no-verify`
- Modal `cancel` on a running training (charges credit)
- `supabase.auth.admin.delete_user` calls
- Rotating production secrets, editing CI/CD config
- Changing safety-critical paths (torque-disable on SIGTERM, sync-verification tolerance, ownership assertions, Dynamixel current limits)
- Renaming files other layers reference (overlay targets, ROS topics, env var names, RPC signatures)
- Touching `start_training_safe` / `get_remaining_credits` / `adjust_workgroup_credits` semantics — workgroup credit pool is load-bearing for grouped students (migration 011); regressions are silent over-spend or refused trainings
- Reintroducing software-side inference safety guards (see rule 2)

## Known issues + open work

State as of 2026-05-20 post commit `16b8378` + `de7d635`. Confirm against current files before acting — these may be fixed by the time you read them.

### Pipeline-level — still open

- **Docker builds use NATIVE per-arch runners — no QEMU.** `docker-publish.yml::build` matrix: `amd64` → `ubuntu-latest` (x86_64), `arm64` → `ubuntu-24.04-arm` (aarch64-native, GA 2025-01). `setup-qemu-action` is intentionally absent; `smoke-test` asserts `uname -m` matches the matrix arch as a belt-and-suspenders check. `build-images.sh` branches on `PLATFORM` (default `amd64`): `arm64` sets `DOCKER_BUILDX_ARGS="--platform linux/arm64 --push"` and pushes to the separate Jetson repos (`nettername/*-jetson`); the manager image is amd64-only and is skipped on arm64. Two non-redundant guards keep the matrix green:
  - **`jlumbroso/free-disk-space@54081f1` runs ONLY on amd64** (`if: matrix.platform == 'amd64'`). The amd64 `physical-ai-server` image is ~15.5 GB; `ubuntu-latest` ships ~14 GB free. The strip (android+dotnet+haskell+large-packages+swap, ~31 GB reclaimed) is what makes `--no-cache --pull` survive. arm64 image is ~7.4 GB and fits — skipping the strip there saves ~2 min/build. Remove the step entirely and amd64 OOM-disks at ~70%; remove the `if:` gate and arm64 wastes 2 min for nothing.
  - **`fail-fast: false`** keeps one arch's failure from killing the other mid-flight.
  - Fallback if `ubuntu-24.04-arm` ever leaves the free GHA tier for public repos: add `docker/setup-qemu-action@<pinned-sha>` before `setup-buildx-action` and drop the `ubuntu-24.04-arm` runner. Expect 5-8× slower arm64 builds (~90 min vs ~15 min) and FP / glibc-string edge cases that diverge between real silicon and `qemu-user`. Cross-ref: the first "Pipeline-level — still open" finding in this section.
- ~~**`tools/modal-cleanup.sh` regex doesn't match** the truncated CLI output~~ → **FIXED**: the script now uses `modal {app,secret,volume} list --json` + python3 JSON parsing (see its header comment), so truncated names like `example-mcp-…` no longer drop out of the match. It is runnable as-is.
- **Supabase PR-branch flow is gated, never runs by default** — Branching is not enabled on the project (Pro+ feature). The `apply-branch` and `teardown-branch` jobs in `supabase-migrate.yml` are gated behind the GHA repo variable `vars.SUPABASE_BRANCHING_ENABLED` (Settings → Secrets and variables → Actions → **Variables** tab, not Secrets). With the variable unset (or anything other than the exact string `true`), both jobs skip cleanly on every PR — PR checks stay green instead of perpetually red. To turn the flow on, the operator must do BOTH: (1) enable Branching in the Supabase dashboard (Project Settings → Branching, requires Pro+), and (2) set the GHA repo variable `SUPABASE_BRANCHING_ENABLED=true`. Flipping only one of the two will not start the flow. `apply-production` is unchanged and continues to apply migrations on `push` to `main`.

### Pipeline-level — FIXED in `16b8378` (do NOT reintroduce)

- ~~`supabase-migrate.yml` CI is broken at the sed-extract password step~~ → replaced with `python3 -c "import urllib.parse..."` in both `apply-branch` and `apply-production`. CI now applies migrations correctly.
- ~~`release-installer.yml` races `release.yml` on tag push~~ → `release-installer.yml` is now `workflow_call`-only and invoked as W5 of `release.yml` after W4 docker success.
- ~~`docker-publish::cleanup_dirty_tags` is dead code (`== 'true'` boolean compared to string)~~ → now uses `fromJSON(inputs.cleanup_dirty_tags || 'false')`.
- ~~Health-gate polls accept any 200 (verifies old pod, not new deploy)~~ → both Railway workflows now assert `body.commit == github.sha` from `/health` / `/version.json`.

### Image-level — Dockerfile-fixed in `16b8378`, awaits next docker-publish to materialize on registry

- ~~~6 GB of upstream build cache + 1.2 GB rustup + 962 MB puccinialin~~ → final `RUN rm -rf /root/.cache /root/.rustup /root/.cargo` appended to `physical_ai_server/Dockerfile`.
- ~~334 MB `.git` directories under `/root/ros2_ws/src/`~~ → `find … -name .git … -exec rm -rf {} +` in the same RUN layer.
- ~~`/opt/talos_system_manager` (216 MB) unused by EduBotics~~ → `rm -rf /opt/talos_system_manager` + scrubs the PYTHONPATH from `/root/.bashrc`.
- ~~Source maps in manager images (~15 MB)~~ → `ENV GENERATE_SOURCEMAP=false` before `npm run build` in both `Dockerfile` (student) and `Dockerfile.web` (Railway).
- ~~No `org.opencontainers.image.*` labels on any image~~ → `OCI_LABELS=("--label" "org.opencontainers.image.revision=$BUILD_ID" ...)` appended to every `docker buildx build` in `build-images.sh`; cloud-api Dockerfile carries the same labels.
- **Re-verify the cache strip after next docker-publish run** — registry tags from before `16b8378` still carry the bloat. Expected `:latest` shrink: 15.09 GB → ~9 GB on `nettername/physical-ai-server`.

### Image-level — still open

- **PyTorch version drift**: ~~Rule §5 above mentions `cu121`. The actual amd64 image ships `torch==2.7.0+cu128`. Either Rule §5 is stale OR an unintended upgrade slipped past.~~ **RESOLVED 2026-05.** Rule §5 was ambiguous — Modal worker is cu121 (deliberate, GPU), student image is cu128 (inherited from Robotis base, CPU at runtime). Both are correct. Rule §5 now distinguishes the two surfaces explicitly. No code change needed.
- ~~**Cross-arch package drift**: `safetensors 0.7 (amd64) vs 0.8.0rc (arm64)`, `protobuf 6 vs 7`, `pillow 12.1 vs 12.2`.~~ **RESOLVED 2026-05.** Both the `Dockerfile.arm64` base and the thin overlay `Dockerfile` pin `safetensors==0.7.0`, `protobuf==6.33.6`, `pillow==12.2.0` via `pip install --force-reinstall` AFTER all other pip layers. (`protobuf` 6.31.0 → 6.33.6 and `pillow` 12.1.0 → 12.2.0 were bumped in the v2.5.0 LeRobot v0.5.1 migration for v0.5.1's `grpcio-dep`.) The amd64 *base* `Dockerfile.amd64` has no floor block — CI pulls the published base instead of rebuilding it, and the thin overlay re-pins the floor on top of every base, so the student image always lands the pin. Verification command embedded in the layer prints the resolved versions at build time.
- **The `versions.env` plumbing is now complete** (the "0 writers" claim is stale). Writers: `docker-publish.yml::versions-env` (the "Emit versions.env artifact" job, `cat > out/versions.env` → uploaded as a workflow artifact) and `release-installer.yml` (the "Emit docker/versions.env for the installer payload" step, `cat > robotis_ai_setup/docker/versions.env`, baked into the `.exe` payload). Readers: `gui/app/constants.py` (`_read_image_tag_from_versions_env`, resolution order `EDUBOTICS_IMAGE_TAG` env → `versions.env` → `latest`), `installer/scripts/pull_images.ps1`, and `installer/scripts/verify_system.ps1`. The file is CI-generated, not committed, so it's absent in the source tree (readers fall back to `IMAGE_TAG=latest`) but present in released installers — so IMAGE_TAG-pinned rollback works for installed builds.
- ~~**arm64 LeRobot SHA pin** — `physical_ai_server/Dockerfile.arm64` carries a FIXME block~~. **SUPERSEDED 2026-05-28 (v2.5.0 LeRobot migration).** The arm64 (and amd64) SHA-pin verification machinery was removed when both Dockerfiles switched to `pip install lerobot[pi,smolvla,peft]==0.5.1` from PyPI — no more git submodule, no more `PHYSICAL_AI_TOOLS_REF` / `LEROBOT_EXPECTED_SHA` ARGs, no more `git rev-parse` runtime check. The 5-site SHA contract is gone; see Rule §5 (now a 3-site PyPI version contract). `talos_system_manager` is still SHA-pinned to `40981c6d...` on arm64 for build reproducibility of its `requirements.txt` (separate concern from LeRobot — the directory itself is still stripped by the amd64 thin-overlay).

### Cloud-API + Supabase — security/correctness fixes landed in `16b8378`

- ~~Jetson pairing-code race IDOR~~ → migration 022 + 2-step pair-intent flow (covered in "Critical architectural choices").
- ~~`register_dataset` IDOR (any student could register a peer's HF repo)~~ → trust-on-first-use HF author anchor in `routes/datasets.py`. KNOWN edge case (LOW): a student who deletes ALL their datasets can re-anchor on a peer's repo. KNOWN race (MEDIUM): 2 concurrent first-time registers can each capture a different author — rate-limit (20/60s) is the partial mitigation.
- ~~Modal cancel cost-bomb (start→cancel×10 → 10 GPUs running to timeout cap)~~ → migration 023 `cancel_requested` + `training_sweep.py` retry sweep (covered in "Critical architectural choices").
- ~~Service-role key leak via re-raised httpx/supabase exceptions in CI~~ → `main.py::_sanitize_probe_error` regex-strips Bearer/apikey before re-raise.
- ~~`/health` always 200, deploy gates verify the wrong pod~~ → `/health` returns commit+schema_ok, deploy gates assert commit equality.
- ~~Supabase 144 perf advisors + 3 actionable security~~ → migration 024 consolidates 32→11 policies, wraps every `auth.uid()` in `(SELECT auth.uid())` InitPlan subquery, drops 11 unused indexes; migration 025 hotfixes 3 FK-covering indexes that 024 over-eagerly dropped. Result: **144 perf → 7** (all 7 are `unused_index` INFO-level on freshly-created indexes, will "green up" once queries hit them). Security retentions: 2× `update_training_progress` (anon+authenticated EXECUTE — Modal worker uses anon key + worker_token row-lock; stronger than the proposed pre-check which would TOCTOU); 1× `workflow_versions WITH CHECK (true)` (trigger uses `app.user_id` GUC not `auth.uid()`; documented TODO for a future migration that adds a GUC-backed predicate).

### What audits confirmed CLEAN

- LeRobot v0.5.1 installed via PyPI in both amd64 and arm64 base Dockerfiles. Build-time smoke tests in all 3 Dockerfiles (Dockerfile.amd64, Dockerfile.arm64, thin overlay) import every policy modeling module + `predict_action` + `make_pre_post_processors` and assert `CODEBASE_VERSION == 'v3.0'`. The old SHA `989f3d05ba47f872d75c587e76838e9cc574857a` is no longer pinned anywhere in the code; the 5-site contract has been superseded by the 3-site PyPI version contract (Rule §5).
- No pre-2026-05 safety guards leaked into images (grep for `joint_clamp|stale_camera|velocity_cap|nan_guard|safety_envelope` → 0 hits).
- No `.env` / credentials / tokens accidentally COPY'd into any image.
- All 4 PAS overlays + 14 OMX overlays bit-identical across arches (sha256 verified inside images).
- YOLOX-tiny ONNX present as exactly one copy per image with the correct sha256.
- All `apply_overlay` chain entries covered — no orphan overlay files in the repo's `overlays/` dir that don't reach the image.
- `.s6-keep` bind-mount truly load-bearing: `s6-rc.d/user/contents.d/` only has `s6-agent` enabled at image-build time; `physical_ai_server` service is defined but inactive without the runtime mount.

### Operator action items (cannot be done from CI)

- **Delete `REACT_APP_BUILD_ID` Railway service variable** on the `teacher-web` service. `railway.json` `buildArgs` should interpolate `$RAILWAY_GIT_COMMIT_SHA` correctly, but a residual frozen service variable was the 2026-05-19 root cause of the 5-day-stale teacher-web bundle. Belt-and-suspenders.
- **Enable Supabase leaked-password protection** in dashboard (closes 4th security advisor).
- ~~Resolve the arm64 LeRobot SHA pin FIXME in `Dockerfile.arm64`.~~ **DONE 2026-05.**

### What's deferred to next session

- Run `tools/docker-hub-cleanup.sh --execute` (cleans 5 junk repos + 12 `*-dirty` tags from April; 81 GB recovery from `robotis-ai-training` orphan)
- Run `tools/modal-cleanup.sh --execute` (the regex bug is already fixed — the script uses `--json` parsing and is runnable as-is)
- ~~Reconcile PyTorch cu121 vs cu128 drift~~ → Rule §5 was rewritten for v2.5.0: torch 2.7.x is now the single PyTorch version across all surfaces (Modal cu126 channel, student/Jetson cu128 channel inherited from Robotis base). DONE 2026-05-28 (LeRobot v0.5.1 migration).
- ~~Pin cross-arch package versions (safetensors, protobuf, pillow)~~ → both Dockerfiles pin to LCD via `--force-reinstall`. DONE 2026-05.
- ~~Decide on `versions.env` plumbing (add a writer OR drop the 3 readers as dead code)~~ → **DONE**: writers exist in `docker-publish.yml` + `release-installer.yml`; do NOT drop the readers (that would break installer tag-pinned rollback).
- Tighten `workflow_versions` "Trigger inserts" INSERT policy via a GUC-backed predicate (S2 TODO in migration 024)
- Harden `register_dataset` HF-author anchor against the concurrent-first-register race

## When in doubt

The single source of truth is **the code**. This file describes invariants at the time it was written. Verify against `git log` and the current file when stakes are high. If this file disagrees with the code, fix this file in the same change — the whole point is that it stays in sync.

When you find an obstacle, **find the root cause** instead of bypassing it (never `--no-verify`, never `@pytest.skip`, never short-circuit an `apply_overlay` sha256 assertion that's telling you upstream renamed something).

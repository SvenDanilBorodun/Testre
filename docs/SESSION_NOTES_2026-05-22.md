# EduBotics camera/arms debugging — 2026-05-22 session

## Initial problem chain
1. Cameras connected to Windows but GUI couldn't detect them.
2. After fix: Aufnahme feed extremely choppy (~10 Hz / freezes / 100+% CPU).
3. After raw_mjpeg attempt: Aufnahme tiles all green — turned out raw_mjpeg is BROKEN.

## Findings (by severity)

### 🔴 usb_cam 0.8.1 `raw_mjpeg` is broken end-to-end
- Puts ~30 KB MJPG bytes into `Image` msg tagged `encoding="yuv422"`, falsely claims 614 400-byte size.
- `image_transport` re-publishes garbage; downstream consumers see green stripes + RNG-noise band.
- Visually confirmed by saving snapshot JPEG via web_video_server.
- **Would silently corrupt all training data** if used for recording.
- Fix: revert to upstream default `mjpeg2rgb` (env var or entrypoint default).

### 🔴 F22 audit edits never reached the image (CLAUDE.md Rule §3)
- `open_manipulator/open_manipulator_bringup/launch/camera_usb_cam.launch.py` was edited (image_width/height/framerate/pixel_format LaunchConfigurations) but **never added to `apply_overlay` chain**.
- Base image kept shipping upstream version with only `video_device`. Entrypoint's launch args silently dropped.
- **Fixed** in commit `4eeb233` — file copied to `overlays/`, registered in Dockerfile.

### 🔴 F36 audit orphan — web_video_server loopback bind
- `physical_ai_tools/physical_ai_server/launch/physical_ai_server_bringup.launch.py:55-63` has F36 `parameters=[{'address':'127.0.0.1','port':8080}]` for defence-in-depth against LAN exposure of MJPEG stream.
- **Not in any apply_overlay chain.** Only compose `127.0.0.1:8080:8080` mapping currently blocks LAN.
- Needs overlay registration. **NOT YET FIXED** (user said don't push).

### 🔴 HfApiWorker forks duplicate ROS node → `/task/command` timeouts
- `multiprocessing.Process()` defaulted to fork on Linux → child inherits parent's rclpy node `physical_ai_server`.
- DDS sees two nodes with same name; service calls time out: "Befehlsausführung fehlgeschlagen [Stop]: Service call failed".
- **Fixed** in commit `c2cc1c3` — switched to `multiprocessing.get_context('spawn')`.

### 🔴 GUI camera scan returned empty (3 stacked bugs)
- `wsl_bridge.run()` passed scripts via `bash -c "<script>"`; wsl.exe mangled multi-line scripts whose `$(...)` captured `v4l2-ctl` tab-indented output → v4l2 output got executed as bash commands.
- `list_video_devices()` emitted dupes when two identical-VID:PID cameras shared a by-id symlink.
- `_list_usb_camera_vid_pids()` empty when cameras forwarded to WSL (Windows PnP only sees usbipd stub VID_80EE:CAFE).
- **Fixed** in commit `c87bde5` — stdin-piped scripts (bytes mode), Bus-info dedupe, WSL-side UVC fallback.

### 🔴 Entrypoint couldn't open `/dev/v4l/by-id/*` symlinks
- GUI writes by-id paths into `.env` for replug stability when camera has USB serial.
- usb_cam's V4L2 wrapper strips path components → tries `/dev/../../video2` → fails.
- **Fixed** in commit `57d4219` — `readlink -f` resolves symlinks before `ros2 launch`.

### 🟡 Dynamixel comms errors NOT caused by CPU contention
- ~55 errors / 30 s baseline (FastBulkRead Rx Fail -3001/-3002, Overrun, Trigger-while-async-busy).
- Hypothesis was camera CPU starvation. **Experiment refuted it**: killing cameras INCREASED errors 5×.
- Real cause: all 4 USB devices (2 arms + 2 cameras) share one usbipd `vhci_hcd` → URB drops; follower `is_async="true"` (matches upstream) turns each drop into an async-trigger storm.
- Possible fixes (untried): flip follower to sync mode, disable FastBulkRead, separate USB host controllers.

### 🟡 Sync verification FAILED on gripper_joint_1 was a false alarm
- Error said: `Per-joint err: [..., 1.396 rad], motion: [0.0, 0.0, ...]`.
- Investigation: leader at -0.696, follower at +0.699 = 1.396 rad apart. But teleop works perfectly because `joint_trajectory_command_broadcaster` has `reverse_joints: [gripper_joint_1]` — sign is flipped before publishing.
- Sync verifier reads `/leader/joint_states` RAW (unflipped) → compares two different coord frames.
- Also: sync_follower publishes to `/leader/joint_trajectory` which broadcaster owns at 100 Hz → broadcaster's stream overwrites sync's one-shot in ~10 ms → gripper "motion=0" is real but inconsequential.
- Fix (proposed, untried): exclude `gripper_joint_1` from JOINTS list in entrypoint_omx.sh sync_follower.

### 🟡 web_video_server has no respawn
- `physical_ai_server_bringup.launch.py:59-65` Node() lacks `respawn=True`.
- One SIGSEGV → blank forever until container restart.
- Fix bundle with F36: add `respawn=True, respawn_delay=2.0`.

### 🟢 workflow/ shadow-tree dev trap (no current bug)
- `physical_ai_tools/physical_ai_server/physical_ai_server/workflow/` has stale shadow copy of files in `robotis_ai_setup/docker/physical_ai_server/overlays/workflow/`.
- Dockerfile ships from `overlays/workflow/`. In-tree copy includes dead `safety_envelope.py` still imported by stale `workflow_manager.py`.
- Future devs editing in-tree path will see zero effect. Recommend CI guard or deletion.

### 🟢 CI docker-publish disk-space flake
- amd64 build OOMed disk twice on commit `0bce350`.
- **Fixed** in commit `d006854` — added `tool-cache: true` + `docker-images: true` to free-disk-space step.

## Commits pushed to main (in order)

| SHA | Title |
|---|---|
| `c87bde5` | fix(gui): list cameras already forwarded to WSL |
| `57d4219` | fix(open-manipulator): resolve /dev/v4l/by-id symlinks before usb_cam launch |
| `c2cc1c3` | fix(physical-ai-server): force HfApiWorker child to 'spawn' start method |
| `0bce350` | perf(open-manipulator): default usb_cam to raw_mjpeg passthrough ⚠️ **NEEDS REVERT** |
| `d006854` | ci(docker-publish): reclaim tool-cache + preinstalled docker images |
| `4eeb233` | fix(open-manipulator): overlay camera_usb_cam.launch.py — the F22 patch never reached the image |

Latest deployed image digests (pulled from Docker Hub):
- `nettername/open-manipulator:latest` → `sha256:97208ceff52e…`
- `nettername/physical-ai-server:latest` → `sha256:47c85846eb47…`
- `nettername/physical-ai-manager:latest` → `sha256:1a9a7bb65a19…`

## Local-only edits (NOT pushed)

`C:\Users\svend\AppData\Local\EduBotics\.env` — added line:
```
EDUBOTICS_CAMERA_PIXEL_FORMAT="mjpeg2rgb"
```
Overrides the entrypoint's `raw_mjpeg` default until source revert. The GUI rewrites this file on every scan but appears to preserve unknown keys — verify.

## Open follow-ups (next session)

1. **Revert raw_mjpeg in source** — `robotis_ai_setup/docker/open_manipulator/entrypoint_omx.sh` line ~411 — change default back to `mjpeg2rgb`. Requires docker-publish rebuild.
2. **F36 + respawn overlay** — copy `physical_ai_tools/physical_ai_server/launch/physical_ai_server_bringup.launch.py` to `robotis_ai_setup/docker/physical_ai_server/overlays/`, add `respawn=True, respawn_delay=2.0` to web_video_server_node, register in Dockerfile.
3. **Sync verification fix** — drop `gripper_joint_1` from JOINTS list in `entrypoint_omx.sh::sync_follower`. Cosmetic but removes scary boot error.
4. **Dynamixel investigation** — try follower `is_async="false"` (one xacro line) or `use_fast_read` disabled (if available). Per usbipd serialization analysis. Untried.
5. **workflow/ shadow tree** — pick: delete in-tree dupes, OR CI guard for byte-identity with overlays/. Add `safety_envelope.py` import cleanup.
6. **EduBotics.exe + installer rebuild** — GUI fixes (wsl_bridge, device_manager) are in source but only PyInstaller-rebuilt `.exe` ships to students. Tag a release (e.g., `v2.3.7`) when ready — fires release.yml W1→W5 chain.
7. **CLAUDE.md updates** — Rule §2 currently advertises "0.15 rad trajectory tolerance" but overlay has `trajectory: 2.0`. Either update doc or revert tolerance.
8. **LeRobot SHA-pin verification** — Rule §5 requires byte-identity at `989f3d05ba47f872d75c587e76838e9cc574857a`. Needs external upstream clone; not done in-session.

## Key file paths

- Dev GUI launch: `pythonw.exe main.py` from `C:\Users\svend\cloud\Testre\robotis_ai_setup\gui\`
- Installed (old) GUI: `C:\Program Files (x86)\EduBotics\gui\EduBotics.exe`
- .env: `C:\Users\svend\AppData\Local\EduBotics\.env`
- Entrypoint: `Testre\robotis_ai_setup\docker\open_manipulator\entrypoint_omx.sh`
- Open-manipulator overlays: `Testre\robotis_ai_setup\docker\open_manipulator\overlays\`
- Physical-AI-server overlays: `Testre\robotis_ai_setup\docker\physical_ai_server\overlays\`
- CI: `Testre\.github\workflows\docker-publish.yml`

## Verified facts about hardware/pipeline

- Both Innomaker U20CAM-720P cameras connected to different USB host controllers (`1-6` and `2-1` per usbipd list — better than shared hub).
- Cameras share VID:PID `0c45:6367`; only one has USB serial (SN0001). The serialed one gets `/dev/v4l/by-id/...SN0001-video-index{0,1}`; the other falls back to raw `/dev/videoN`.
- ROBOTIS upstream `usb_cam` package's `params_1.yaml` defaults to `mjpeg2rgb`. ROBOTIS doesn't actively choose it — it's the package default they inherit passively.
- LeRobot stores libx264 yuv420p mp4 (not JPEG). Pixel_format choice at camera is irrelevant on disk *as long as the in-memory frame is correct*.
- All consumers in EduBotics use `/image_raw/compressed` only — zero subscribers to uncompressed `/image_raw`.
- Camera setup is the ONLY thing using `raw_mjpeg` — switching to `mjpeg2rgb` matches upstream and has no functional downside.

## Outstanding state at session end

- Containers possibly running with `raw_mjpeg` (broken) — user needs to stop+start from GUI to pick up the `.env` change.
- `nettername/open-manipulator:latest` on Docker Hub still defaults to `raw_mjpeg` — env-var override is the workaround until source revert.
- All audit agents done; nothing in flight on GitHub Actions.

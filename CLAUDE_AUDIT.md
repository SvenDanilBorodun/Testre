# EduBotics Deep Audit

Companion to `CLAUDE.md` (map) and `CLAUDE_PIPELINE.md` (how it works).
This document is **what's wrong, what's risky, what to fix**. Nine parallel audits (eight per pipeline stage + one cross-cutting ops/security pass) produced ~200 raw findings; this file is the deduped, ranked, verified consolidation.

Structure:
- §0 methodology + corrections
- §1 top-20 most urgent findings (the triage list)
- §2 cross-cutting themes
- §3 stage-by-stage findings
- §4 cross-cutting ops/security/governance
- §5 suggested action plan

Every finding has **file:line evidence**, **severity**, and a **concrete fix**. Generic "add tests" filler was stripped.

---

## §0. Methodology + corrections

Nine `Explore` agents were dispatched in parallel, one per stage plus a cross-cutting pass. Prompts explicitly asked them to find **bugs and flaws**, not describe behavior, and to produce file:line evidence.

### Verified false positive
- **Agent 9 claimed `cloud_training_api/.env` is committed to git with live secrets ("CRITICAL").** Verified: `.env` is in `.gitignore` (line 2 of `Testre/.gitignore`) and `git log --all --full-history -- "*.env"` returns no history. **False positive.** The real residual concern is that the *local, untracked* `.env` still has a stale `RUNPOD_API_KEY` / `RUNPOD_ENDPOINT_ID` from the pre-Modal migration sitting on the maintainer's disk — rotate+remove them, but the git exposure claim was wrong.

### Known overlaps (same issue flagged by multiple agents)
- Camera-name exact-match enforced at inference but not at recording → deduped under §3.5.
- ROS_DOMAIN_ID=30 shared → same-LAN classrooms cross-talk → deduped under §3.4.
- Upstream overlay fragility → deduped under §3.4.
- CPU silent fallback when no GPU → kept under §3.8 (inference owns the symptom).
- Patch regex can silently no-op → deduped under §3.4.

### What I didn't audit
- **LeRobot itself** (`physical_ai_tools/lerobot/` @ `989f3d05`). It's a byte-for-byte upstream snapshot; auditing it would be an LeRobot upstream review, not an EduBotics review.
- **The React source code in depth** beyond what's in `FRONTEND_UX_FOLLOWUPS.md`. The agent confirmed the known issues exist and added more; the full React audit is a separate scope.
- **Runtime behavior** — this is a static code review. Anything marked "can hang indefinitely" or "can race" should be reproduced empirically before investing in a fix.

---

## §1. Top-20 most urgent findings (triage list)

Ranked by combined severity × likelihood × blast-radius. Fix these first.

| # | Stage | Issue | Severity | One-line fix |
|---|-------|-------|----------|--------------|
| 1 | Inference / Arm | No joint-limit, velocity, or NaN clamp on predicted actions — arm swings violently on a bad policy output | **Critical safety** | Clamp action to joint limits + NaN guard in `inference_manager.predict()` before publish |
| 2 | Arm | No torque-disable on SIGTERM — arm falls under gravity on container stop | **Critical safety** | Add torque-disable service calls in `entrypoint_omx.sh` trap before cleanup |
| 3 | Arm | Stale-camera watchdog warns but doesn't halt inference — policy runs on frozen visual input | **Critical safety** | Halt inference (not warn) when `stale_duration > 2s` |
| 4 | Inference | GPU silent fallback to CPU → 30 Hz inference drops to <5 Hz → actions lag arm tolerance, no warning | **Critical** | Assert `torch.cuda.is_available()` in `InferenceManager.__init__`; fail loudly if false |
| 5 | Recording | Video encoding runs async, episode marked complete before MP4 exists → parquet saved, video missing → unusable dataset shipped as "complete" | **Critical** | Join encoder thread + verify file exists + non-zero before marking episode saved |
| 6 | Recording | Frames not time-synchronized (no `message_filters.TimeSynchronizer`) → cameras at ~30 Hz, joints at ~100 Hz, "latest-wins" pairs camera N with state N+2 → trained model learns mislabeled actions | **Critical** | Use `ApproximateTimeSynchronizer(slop=0.05)` on camera+follower+leader triplet; store ROS header stamp in parquet |
| 7 | Cloud API | FastAPI uses `SUPABASE_SERVICE_ROLE_KEY` which bypasses RLS; every `.select/.insert/.update` relies on Python-side ownership checks. One missed `_assert_classroom_owned()` = silent IDOR | **Critical** | Either move every access through an RPC that validates ownership in SQL, or switch to anon key + make RLS authoritative |
| 8 | Cloud API | Modal dispatch failure after `start_training_safe()` inserts the row = stuck "queued" row with `cloud_job_id=NULL` forever; credits consumed, no worker | **Critical** | Swap order: dispatch first, insert only on success; OR use an atomic RPC that both inserts and returns token, then caller rolls back on dispatch fail |
| 9 | Installer | `wsl --unregister EduBotics` on upgrade destroys the distro's VHDX including any named Docker volumes that live inside (e.g., recorded datasets, HF cache) | **Critical** | Either back up named volumes to host before unregister, or migrate volume drivers to bind-mounts on Windows host |
| 10 | Recording | RAM cushion triggers early save at <2 GB free; warning only to stderr, not `TaskStatus.error` / UI → student records 60-frame episode, silently gets 30 frames → model trains on truncated trajectories | **Critical** | Add `truncation_reason` to `TaskStatus`; surface in React as red banner; require acknowledgement |
| 11 | Rootfs | Docker 27.5.1 + containerd 1.7.27 deb pinning: apt.docker.com removes old debs after 12-24 months → rebuild fails with "package not found" | **High** | Build once, publish tar.gz to GitHub Release as pinned artifact with SHA256; Dockerfile downloads from the pinned URL |
| 12 | Cloud API | Worker-token not nulled on first successful progress RPC; leaked token lets attacker overwrite losses until the worker next calls with terminal status | **High** | Null the token on first successful update OR make the token single-use per step |
| 13 | Recording | HF `upload_large_folder()` has no timeout → slow classroom network = UI freeze forever; WSL restart orphans partial upload; no resumption | **High** | Wrap in `signal.alarm(3600)`; detect+resume partial uploads via `.hf_upload_state` marker |
| 14 | Cloud API | HF `dataset_info()` + `hf_hub_download(meta/info.json)` in preflight have no timeout → Modal worker blocks the full 7-hour function timeout on a hung HF | **High** | Pass explicit `timeout=60` to hf_hub calls; fail-fast with clear error |
| 15 | Docker | `privileged: true` + `/dev:/dev` blanket mount on 2/3 containers + no userns remap → one RCE = full host access (including Windows kernel via WSL2 shared kernel) | **High** | Drop privileged; selective devices (`/dev/ttyACM*`, `/dev/video*`, `/dev/bus/usb`); drop `ALL` caps, add only `SYS_NICE` where needed |
| 16 | GUI | Unquoted paths in `.env` generation → `C:\Users\Max Muster\...` breaks compose (space splits the var) → silent startup failure | **High** | Double-quote all path values in `config_generator.generate_env_file()` |
| 17 | Docker | Overlay `find` + `cp` in `physical_ai_server/Dockerfile` has no post-verification that the copy replaced the target → one upstream rename silently produces an image with stock upstream code | **High** | Assert target files were actually overwritten by comparing checksums before/after; fail build on no-op |
| 18 | Docker | `patches/fix_server_inference.py` uses `str.replace()` with no assertion the substitution happened → one upstream reformat = silent patch no-op, uninitialized `_endpoints` bug returns | **High** | Compare content before/after; exit 1 if identical |
| 19 | Installer / GUI | MSI download (usbipd-win) from GitHub Releases without SHA256 verification → MITM or compromised proxy serves malicious installer with admin rights | **High** | Ship expected SHA256 hash in the installer; verify `Get-FileHash $msi -Algorithm SHA256` before `Start-Process msiexec` |
| 20 | Cloud API | Dedupe window keyed on `(user_id, dataset_name, model_type)` only — student tweaks `steps=5000→5001` to bypass dedup and burn 2 credits | **High** | Include hash of `training_params` in dedup key |

---

## §2. Cross-cutting themes

Patterns that appeared across multiple stages — fix these systemically, not per-site.

### 2.1 Silent degradation is the dominant failure mode
Almost every "high" finding reduces to: *the system appears healthy while producing wrong data or running with wrong settings.* Examples: camera-name recording/inference mismatch, stale-camera watchdog that only warns, RAM truncation not surfaced, patch no-op, overlay no-op, `find + cp` not validated, HF upload succeeds but model missing, GPU silent-fallback, duplicate-enum migration, IDOR via service role, dedupe bypass, etc.

**Systemic fix**: every status write should include a "confidence" or "completeness" flag. Every patch / overlay / migration should fail loudly on no-op. Every timeout should have a surfaced error. Log to `TaskStatus` / UI, not stderr.

### 2.2 No runtime safety layer around the arm
The arm is commanded directly from policy output with **zero validation**. No joint limits, no velocity caps, no workspace bounds, no E-stop topic, no heartbeat watchdog, no torque-disable on shutdown. This is acceptable for R&D; it is not acceptable in a classroom where 30 students handle real hardware.

**Systemic fix**: introduce a "safety filter" node between inference and the arm controller that clamps, rate-limits, and kills the trajectory on NaN/timeout/heartbeat-loss. Torque-disable on SIGTERM via an explicit service call.

### 2.3 ROS_DOMAIN_ID=30 is hardcoded → same-LAN classrooms cross-talk
Two students on the same school Wi-Fi share domain 30. Student A's `/leader/joint_trajectory` drives Student B's arm. No isolation.

**Systemic fix**: derive `ROS_DOMAIN_ID` from a machine UUID (`Get-CimInstance Win32_ComputerSystemProduct.UUID | Get-FileHash | mod 232`), bake into `.env` at GUI install time.

### 2.4 RLS bypass via service role makes all authorization code-side
FastAPI uses `SUPABASE_SERVICE_ROLE_KEY`, so every RLS policy in `migration.sql` / `002_accounts.sql` / `004_progress_entries.sql` is defense-in-depth theater. The *actual* guard is the `_assert_classroom_owned()` / `get_current_teacher()` calls sprinkled through the route handlers. One missed assertion = silent IDOR. Realtime subscriptions (trainings table in 006) *do* hit RLS from the frontend, so there's also a hybrid-enforcement trap: same operation protected differently on two code paths.

**Systemic fix**: pick one. Either everything through RPC with in-SQL authorization (strongest), or switch FastAPI to anon key with end-to-end RLS (most cohesive, requires test coverage).

### 2.5 Upstream overlay is one rename from silent catastrophe
The physical_ai_server image clones upstream ROBOTIS-GIT during base build, then our thin layer `find`s files by name/path and `cp`s overlays on top. No checksum, no path assertion. `patches/fix_server_inference.py` uses `str.replace()` with no verification. A single upstream reformat or rename produces a shipped image with either (a) overlays missed, (b) patches no-op'd, (c) `.s6-keep` mount-point stale → ROS node silently not enabled. No CI catches any of these.

**Systemic fix**: pre-build script that snapshots upstream file hashes; assert every `find` hits ≥1 file AND the destination differs post-`cp`; assert every patch produces a byte-change; run a smoke test (`docker run ... ros2 topic list | grep -q joint_states`) before push.

### 2.6 No health check, no depends_on condition, nothing is "ready"
Compose `depends_on` uses default `condition: service_started`. `physical_ai_manager`'s nginx serves 200 before rosbridge is listening. React connects and sees `ECONNREFUSED`. No `HEALTHCHECK` in any Dockerfile. No readiness signal from s6. Student sees "web UI loaded" + "service not reachable" simultaneously.

**Systemic fix**: `HEALTHCHECK` on each service (rosbridge: `ros2 topic list | grep joint_states`; nginx: `curl /version.json`); `depends_on: condition: service_healthy`.

### 2.7 Student-data durability is a lottery
Recorded datasets live in `ai_workspace` named volume *inside* the distro's VHDX. `wsl --unregister EduBotics` (upgrade path, step 4 of installer) destroys the VHDX. Students who didn't push to HF lose everything. HF push can hang forever with no resume. RAM cushion silently truncates episodes.

**Systemic fix**: named volumes as bind-mounts on the Windows host (`C:\Users\<user>\EduBoticsData\`) so they survive reimport; HF upload with resume + timeout; UI surface "pushed to HF ✓" as mandatory before "upgrade" is allowed.

### 2.8 Nothing is monitored
No Sentry on the GUI. No uptime check on Railway. No alerting on Supabase. No structured logging on Modal worker. A student crashing, a Railway outage, a Modal quota exhaustion — the maintainer learns by angry email. Observability is a Day-1 requirement for a classroom product.

**Systemic fix**: Sentry (GUI + FastAPI + Modal), 30-min uptime probe on Railway `/health`, Supabase auth error alerts. Cheap, high-ROI.

### 2.9 GDPR / DSGVO compliance is undocumented
German students, data crossing US-hosted Supabase (`fnnbysrjkfugsqzwcksd.supabase.co` — verify region in dashboard), HF datasets default-public. No privacy policy. No `GET /me/export` endpoint for Art. 15. No `POST /me/delete` for Art. 17. Timezone hardcoded to Europe/Berlin but HF is public-by-default. School data-protection officers will flag this.

**Systemic fix**: verify Supabase region is EU-West; mark HF repos private by default; write `PRIVACY.md`; implement export+delete endpoints; log data processing purposes.

### 2.10 Version drift across 5 sources of truth
`gui/app/constants.APP_VERSION` (2.2.2), `installer/robotis_ai_setup.iss AppVersion` (2.2.2), `docker/versions.env IMAGE_TAG`, React `package.json` (0.8.2), HTTP `/version.json` build-id. Five numbers, no single source, drift inevitable.

**Systemic fix**: one `VERSION` file at repo root; `build-images.sh` / installer / GUI all read it.

---

## §3. Stage-by-stage findings

### 3.1 Installer + WSL2 rootfs

**Critical**
- **Mid-copy `wsl --import` failure leaves corrupt VHDX; retry tries to import over broken state.** `import_edubotics_wsl.ps1:88–93` checks exit code but not partial-VHDX state. Before import, empty `$InstallRoot`; on failure, `Remove-Item -Recurse`. Add `(Get-Volume -DriveLetter C).SizeRemaining -gt 20GB` precheck.
- **60s `docker info` poll too tight on slow HDDs or Controlled Folder Access.** First extraction can take 90–120s on 5400-RPM drives. `import_edubotics_wsl.ps1:102–127` `$maxWait = 60`. Bump to 180 + log elapsed time on each poll; inspect `/var/log/dockerd.log` before force-invoking `start-dockerd.sh`.
- **`start-dockerd.sh` has no watchdog**: `wsl_rootfs/start-dockerd.sh:7–9` is a single `nohup` with no restart. Dockerd segfault = silent death, no recovery until GUI forces `pkill + restart`. Add a tiny supervisor loop: `while true; do pgrep -x dockerd || /usr/bin/dockerd; sleep 5; done &`.
- **MSI download (usbipd-win) without SHA256 validation.** `install_prerequisites.ps1:68`. See top-20 #19.
- **Reboot-required marker** `.reboot_required` written by `install_prerequisites.ps1` when `wsl --install` wants a reboot; GUI needs to detect it on next boot and invoke `finalize_install.ps1` elevated, but detection path is fragile. Store marker in a stable location (HKCU registry value), not a file under `{app}\scripts\` that may move.

**High**
- **Named Docker volumes live inside the VHDX** → destroyed on `wsl --unregister`. See top-20 #9.
- **`.migrated` marker in `migrate_from_docker_desktop.ps1:21–26`** prevents re-migration if student reinstalls Docker Desktop; check actual state, not the marker.
- **`ubuntu:22.04` base uses floating tag** → non-reproducible rootfs rebuilds. Pin to digest: `FROM ubuntu:22.04@sha256:...`.
- **Docker 27.5.1 deb pin will expire from apt.docker.com.** See top-20 #11.
- **No SHA256 verification of the bundled rootfs `.tar.gz`.** `verify_system.ps1:111` checks existence only. Ship hash in `versions.env`, verify in `import_edubotics_wsl.ps1` before `wsl --import`.
- **UAC multi-user race**: `configure_wsl.ps1:9–18` finds "the logged-in user" via `explorer.exe` process; with Fast User Switching, two explorer processes exist → undefined behavior.
- **Controlled Folder Access blocks `{app}` writes.** No detection, install silently fails in places. Call `Get-MpComputerStatus | Select RealTimeProtectionEnabled` and fail fast with user-actionable message.
- **Windows 11 Home can't run Hyper-V** but `install_prerequisites.ps1:27–34` only warns. Detect `Get-CimInstance Win32_OperatingSystem.ProductType` and hard-fail with clear message.

**Medium**
- Timezone hardcoded `Europe/Berlin` in `wsl_rootfs/Dockerfile:31–32` — non-German students get wrong log/dataset timestamps.
- `start-dockerd.sh` has no graceful shutdown on `wsl --terminate` → running containers killed mid-write.
- `configure_usbipd.ps1` never removes old policies on downgrade — old `--operation AutoBind` flags linger on usbipd 5→4 downgrades.
- `pull_images.ps1` uses regex parsing of `versions.env` instead of `ConvertFrom-StringData` → fails on spaces around `=`.
- Source installer `.exe` not cleaned from `%TEMP%` on `/S` silent install — `CleanupSourceInstaller()` only runs on interactive `ssDone`.
- `wsl.conf [interop] appendWindowsPath=false` is set but undocumented — students can't invoke Windows tools from the distro; surprises future maintainers.

**Low / Nit**
- `docker info *>$null 2>&1` swallows error detail → debug black hole; capture to a variable, log on failure.
- No diagnostic-bundle script (`scripts\collect_diagnostics.ps1`) — a failed install is a support nightmare.
- GPU detection runs `nvidia-smi` on the Windows host (`gui/app/docker_manager.py:144–158`) rather than inside the distro → edge-case false negatives with Hyper-V + discrete GPU.
- Stale comment `configure_wsl.ps1:79–81` says "Docker Desktop manages port forwarding" (copy-paste from a previous era).

---

### 3.2 Windows tkinter GUI

**Critical**
- **WebView2 subprocess IPC race.** `gui_app.py:963–984` + `webview_window.py:86–135`. Parent spawns subprocess then schedules a 2-second timer to check `_runtime_missing`. If WebView2 fails in <100ms (missing runtime), the watchdog hasn't set the flag yet when the timer fires — parent logs "Web-Oberfläche wird im EduBotics-Fenster geöffnet" falsely and also opens the fallback browser seconds later. Use a named event (`CreateEvent`) that the child signals on `webview.start()` success/failure; block on that, not a 2s guess.
- **`_start_environment` daemon thread doesn't set `self.running=False` on all error paths.** `gui_app.py:856–959`. Multiple early `return`s skip the outer except at line 951. Hardware-validation failure (line 890) or `.env` regen failure (line 902) leaves buttons locked forever. Convert to explicit `try/finally` that always sets the flag.
- **Unquoted `.env` paths break on spaces in usernames.** `config_generator.py:7–42`. See top-20 #16.
- **USB attach → `/dev/serial/by-id/` race.** `device_manager.py:213–269`. Udev symlink not guaranteed by the time `identify_arm_via_docker()` runs at line 249. Symptom: `identify_arm.py` pings a keyboard, returns `unknown`. Call `udevadm trigger && udevadm settle` before polling; fall back to `/dev/ttyACM*` + VID/PID match inside the container.
- **Docker pull stall watchdog false-positives on fast disks.** `docker_manager.py:274–432`. 10 MB / 20 s growth threshold is too low; NVMe extraction bursts past 50 MB/20s then stalls at exactly the wrong moment during a long extract with no new stdout line → `pkill -KILL dockerd` mid-overlay2-write → corruption. Read `/proc/<docker-pid>/io` for actual writes, not disk-wide growth; raise threshold to 50 MB; SIGTERM → 5s → SIGKILL escalation, not bare SIGKILL.

**High**
- **UAC elevation via `ShellExecuteEx`**: `WaitForSingleObject(hProc, INFINITE)` returns when process exits, but `finalize_install.ps1` spawns `wsl --import` as its own child which may outlive the parent; add a "success marker" line the PS script writes, wait for the marker, not just process exit.
- **Update version-compare is tuple-int**: `packaging.version.parse()` would be safer; `"2.2.2rc1"` raises ValueError and the code has no handler → app blocks startup.
- **Blocking update modal can trap offline students.** Skip button only enabled after 3 retry failures. Enable immediately with "Offline-Modus" tooltip.
- **`_elevate_and_wait` handle leak.** `gui_app.py:83–86`. `CloseHandle` only on normal path; if `WaitForSingleObject` times out, handle leaks. Use `try/finally`, add a 5-minute timeout.
- **Multiple clicks on "Browser öffnen"** spawn multiple WebView2 subprocesses (each a separate memory footprint). Debounce 500 ms.
- **WebView2 subprocess is detached from parent.** Parent crash → orphaned WebView2 process never dies. Pass `CREATE_NEW_PROCESS_GROUP`; use a watchdog thread in parent that kills child on parent-death.
- **`.env` not atomically written.** `config_generator.py:39–40`. Power loss mid-write = truncated `.env` → compose fails silently. Write to `.env.tmp` + `os.replace()`.
- **Serial path cached in `.env` across reboots.** WSL reassigns `/dev/ttyACM*`; by-id symlinks change too when students swap USB ports. Current "re-attach on start" logic (gui_app.py:872–880) only re-attaches if path is *missing* — if both arms enumerate but swapped, follower gets leader's path → inverted arm behavior. Detect by VID/PID + `identify_arm.py` role, not by path, every boot.
- **Health check happens before container entrypoints finish.** Compose `up` returns when containers are created, NOT when `entrypoint_omx.sh` has read leader + synced follower (30s+). React opens against a rosbridge that's still booting. Wait for explicit readiness topics (`ros2 topic list | grep joint_states`), not port polls.

**Medium**
- **Three daemon threads touch `self.hardware` / `self.running` without locks** (`gui_app.py:688–732, 736–777, 845–959`). Add a `threading.Lock`.
- **Errors go to the log widget, not as toasts/dialogs.** Students don't read the log pane. Use `messagebox.showerror()` for critical failures.
- **CAMERA_NAME defaults to `gripper`/`scene` silently** even if scan returned zero cameras. Later, inference crashes with German "Modell erwartet Kameras {gripper, scene}" but the student never saw the config auto-assign. Show an explicit toast on every auto-default.
- **`_webview_fallback` opens browser even after user closes the window mid-launch.** Check `root.winfo_exists()` before triggering.
- **UAC transcript file (`%TEMP%\edubotics_finalize.log`) can contain secrets** if PS sources `.env`. Use targeted stdout/stderr redirection; don't use `Start-Transcript`.
- **PyInstaller hiddenimports manually enumerated** for `webview.platforms.edgechromium` / `clr_loader.netfx` — next pywebview/pythonnet bump adds backends that PyInstaller misses. Use `collect_submodules('webview')` + `collect_submodules('clr_loader')`.
- **Docker pull exponential backoff has no global retry cap** — student clicking "Retry" repeatedly re-enters a fresh 4-attempt loop. Track total attempts; show "Give up?" after 10.

**Low / Nit**
- Hardcoded German locale; no i18n scaffolding. `locale.getdefaultlocale()` fallback to English would unblock non-DE students.
- `_on_close` doesn't wait for daemon threads → orphaned `subprocess.run` on shutdown.
- Update check swallows HTTP error details; user gets "no update" when server is 503.

---

### 3.3 Robot-arm connection (`open_manipulator` + `physical_ai_server` overlays)

**Critical (safety)**
- **No torque-disable on SIGTERM.** `entrypoint_omx.sh:6–13`. See top-20 #2.
- **Quintic sync lacks velocity/acceleration + no feedback verification.** `entrypoint_omx.sh:150–167`. `JointTrajectoryPoint.velocities`/`.accelerations` empty; after publishing, script exits without verifying follower reached the goal. If a servo drops or collides mid-sync, entrypoint reports success and first inference command snaps the arm. Populate derivatives of the quintic; spin 4s reading `/joint_states`; assert |final − target| < 0.05 rad per joint; block startup on failure.
- **`/leader/joint_trajectory` is the remap target — anyone who publishes drives the arm.** `omx_f_follower_ai.launch.py:144`. Inference publisher + entrypoint sync publisher + *any rogue process on the same ROS domain* all move the real hardware. Cross-student contamination (see §2.3) means student B's laptop can drive student A's arm. Rename the entrypoint's sync to `/internal/sync_follower` (private topic with its own subscriber in the controller manager), or require SROS2-style auth.
- **No joint-limit / velocity / NaN clamp on inference output.** `physical_ai_server.py:559–576`. See top-20 #1.
- **Stale-camera watchdog warns but doesn't halt.** `overlays/inference_manager.py:122–148`. See top-20 #3.

**High**
- **Gravity compensation controller commented out in `omx_l_leader_ai.launch.py:128`** → all the tuned friction scalars in `hardware_controller_manager.yaml:32–62` are dead code. Leader joints 1–5 sag. Friction-tuning work wasted. Uncomment.
- **Follower gripper current limit 600 mA; leader gripper 300 mA** — asymmetric safety. Leader demos are recorded at 300 mA gentle grip; follower at inference can apply double the force → crushed objects. Align both to ~350 mA in `omx_f.ros2_control.xacro:172` and `omx_l.ros2_control.xacro:144`.
- **No velocity / acceleration limits in JointTrajectoryController config.** `omx_f_follower_ai/hardware_controller_manager.yaml`. Dynamixel register caps exist but ros2_control doesn't know about them → inference can command 60 rad/s and the servo stalls + overheats. Add `max_velocity` / `max_acceleration` per joint.
- **Hardware-plugin crash not recovered** — entrypoint is a bash script, not s6-managed. Dynamixel segfault → `/joint_states` stops publishing, everything downstream hangs silently. Wrap launches in retry loop.
- **30 s USB-port timeout** (`entrypoint_omx.sh:29–42`) too tight for slow USB hubs or in-flight `usbipd attach`. Bump to 60 s + surface detailed error on `TaskStatus`.
- **`identify_arm.py` is dead code AND flaky.** Never called by entrypoint. Returns `unknown` on bus glitch (no retry). GUI uses it but treats "unknown" as a hard failure. Either call it from entrypoint as preflight (safer) or delete (cleaner); either way add 3-attempt retry + return structured errors (`"error:baudrate"`, `"error:bus_timeout"`).
- **`/dev:/dev` blanket mount + `privileged: true`** in `docker-compose.yml:28,55`. See top-20 #15.
- **No E-stop topic, no heartbeat watchdog.** Inference node crashes → last commanded trajectory keeps running for up to 3 s. Define `/robot/emergency_stop`, require 100 ms heartbeat from inference, halt on missing.

**Medium**
- **JointTrajectoryPoint `time_from_start` fixed at 50 ms** in `overlays/data_converter.py:191`. Breaks at fps ≠ 30 Hz — 20 Hz recording produces jerky inference. Compute from fps.
- **Gripper in same JointTrajectory as arm** — Follower `arm_controller` must accept all 6 DoF. Worth an explicit test; if the controller rejects mixed DoF, use separate `gripper_controller` action.
- **PID gains uniform P=1000/D=1000 across all 6 joints** including current-controlled gripper. Unusual; no empirical tuning reference in the xacro. Do a step-response test; reduce D for gripper (current-control has its own damping).
- **Camera hotplug not handled** — `usb_cam` launched once, no reconnect on `/dev/video*` reassignment.
- **QoS defaults** — no explicit `RELIABLE + TRANSIENT_LOCAL` on `/joint_states` subscriber; FastDDS multicast on bridge network is known-flaky (see §3.4).
- **`dynamixel-sdk==4.0.3` hard pin in Dockerfile:8** without compat testing against ROS Jazzy updates.

**Low / Nit**
- Log string `"teleportation active"` in entrypoint line 203 — confusing jargon; rename to "teleoperation ready".
- Leader-position read failure in lines 64–92 logs no stderr context.
- Orphaned gravity-comp params in yaml (if the controller stays disabled).
- `identify_arm.py` pings sequentially — 10 s latency on flaky bus; parallelize.

---

### 3.4 Docker / compose / overlays

**Critical**
- **Overlay `find + cp` with no post-verification.** `physical_ai_server/Dockerfile:33–44`. See top-20 #17.
- **`patches/fix_server_inference.py` is a silent no-op on upstream reformat.** `patches/fix_server_inference.py:22–28`. See top-20 #18.
- **HuggingFace cache volume unbounded.** `docker-compose.yml:60`. Multi-GB models fill `/workspace` → `docker compose down -v` fails → manual cleanup / reimport → data loss. Add `tmpfs size=50G` or disk-quota cron.
- **`physical_ai_server:latest` pulled in build-images.sh** while Dockerfile pins `amd64-0.8.2`. `build-images.sh:93–95`. Inconsistent — fix the script to use the pinned tag.

**High**
- **Privileged + `/dev:/dev` on 2/3 services.** See top-20 #15.
- **No HEALTHCHECK + no `depends_on: service_healthy`.** See §2.6.
- **Secrets in build-args are visible in `docker history` and `docker inspect`.** `build-images.sh:72–80` passes `REACT_APP_SUPABASE_ANON_KEY` as `--build-arg`; bakes into image layer as ENV. Use BuildKit `--secret` or fetch at container start.
- **No build success validation before push.** `build-images.sh:140–148`. Push loop doesn't `set -e`-guard; `physical-ai-manager` can succeed, `physical-ai-server` silently fail, students get mismatched images.
- **DDS multicast on bridge network** is unreliable. No explicit discovery config. Pick one: `network_mode: host` or `RMW_IMPLEMENTATION=rmw_cyclonedds_cpp` with a config file.
- **Ports bind to all interfaces** — `"80:80"`, `"9090:9090"`, `"8080:8080"` in compose. On a school LAN, rosbridge 9090 is reachable LAN-wide if Windows Firewall drops. Bind to `127.0.0.1:` explicitly.

**Medium**
- **Build ID `nogit` fallback** when `.git` context missing (CI runner, fresh checkout). `build-images.sh:68`. Multiple builds collide as "nogit". Use timestamp fallback.
- **`sed 's/\r$//' -i` applied to all files in paths** — binary artifacts in `/etc/s6-overlay/` would be corrupted. Restrict to text-extension globs.
- **No per-service resource limits.** Runaway PyTorch in one container OOMs the whole distro, corrupting `ai_workspace`. Add `mem_limit` / `cpus`.
- **GPU silent fallback** — `runtime: nvidia` without verification that nvidia-docker is actually working; compose proceeds without GPU, training 10× slower, no warning.
- **`.s6-keep` mount point** is brittle — upstream reorg of `s6-rc.d/user/contents.d/` silently disables the ROS node with no build-time check.
- **LeRobot version triple-source** — physical_ai_tools/lerobot snapshot (static), base image clone (live @989f3d05 via ROBOTIS-GIT jazzy branch), Modal image pip (explicit commit). Any drift between these three is invisible until runtime inference-vs-training mismatch. Publish a `LEROBOT_COMMIT` constant all three sources read.
- **nginx `/version.json no-store` header dropped by corporate proxies** in schools → stale build-IDs for hours → React never reloads → students on outdated bundles. Bust with query-string timestamp.

**Low / Nit**
- `ubuntu:22.04` digest unpinned at base (duplicate of rootfs finding).
- Unquoted env vars in compose `FOLLOWER_PORT=${FOLLOWER_PORT}` — word-splitting risk on paths with spaces (overlaps with §3.2 finding #3; fix at both layers).
- Stale comment in `physical_ai_server/Dockerfile:7`.
- No logging of build-args — reproducibility lost.

---

### 3.5 Dataset recording (LeRobot v2.1)

**Critical**
- **No time synchronization between cameras + joints.** See top-20 #6.
- **Video encoding async; episode marked saved before MP4 confirmed.** See top-20 #5.
- **Empty JointTrajectory raises but episode buffer is not fully rolled back.** `overlays/data_converter.py:66–70` + `data_manager.py:482–494`. Exception caught and re-raised but `convert_msgs_to_raw_datas()` can return `(camera, follower, None)` tuple without halting the frame append. Result: half-written parquet on disk. Make convert_msgs raise immediately if *any* required channel is None; catch at record loop, call `record_stop()` with `invalid=true` metadata.
- **HF `upload_large_folder()` has no timeout** → UI hangs forever on slow networks; no resume after WSL restart. See top-20 #13.
- **RAM cushion early-save truncates silently.** See top-20 #10.

**High**
- **Float32 dtype bypassed by numpy broadcasting in optimized-save mode.** `lerobot_dataset_wrapper.py:148–168`. Lists of dicts appended, then `np.stack()` at save time — if any frame is float64 it infects the whole stack. Cast explicitly in `add_frame_without_write_image` and again before `np.stack()`.
- **`Duration(sec=0, nanosec=50_000_000)` hardcoded** in `overlays/data_converter.py:191` — fps≠30 produces jerky inference timing. Compute as `1/fps`.
- **Camera name not validated at *recording* time** (only at inference). See §2.1.
- **`codebase_version: "v2.1"` hardcoded** `data_manager.py:914`. When LeRobot upstream bumps to v2.2, old datasets become orphans with no migration script. Read version from the lerobot package; ship a `migrate_v2.1_to_v2.2.py` alongside every bump.

**Medium**
- **Extra joints in incoming JointState silently dropped** by overlay reorder (`data_converter.py:76–79`). Only missing raises KeyError. If robot is reconfigured (7th joint added) the dataset silently drops it; inference on the new robot leaves that joint inactive → asymmetric control. Assert `len(ordered_positions) == len(msg.position)`.
- **Rosbag2 recording unbounded** — no pre-check of disk space; partition-full crash mid-episode corrupts parquet.
- **Leader sync trajectory (published by entrypoint at startup) is captured as *the first action*** if recording starts too quickly. Model learns "first action = sync pose". Clear leader buffer in `communicator.start_rosbag`, or move the startup sync to a private `/internal/sync_follower` topic (duplicate of §3.3 fix).
- **HF token missing error is cryptic.** "Failed to register token, Please check your token" tells student nothing. Distinguish missing-vs-invalid; link to https://hf.co/settings/tokens in the message.

**Low / Nit**
- Optimized-save OOM crashes instead of graceful early-save (non-optimized path has the early-save).
- Timestamps synthetic (`frame_index / fps`); ROS `header.stamp` not preserved → gap detection unreliable.
- ffmpeg preset hardcoded `ultrafast` → larger files, slower HF uploads on classroom networks.
- Episode metadata lacks `operator_id` — mixed-student datasets have no attribution.
- `convert_msgs_to_raw_datas` returns `(a, b, c)` where c can be None → confusing API; return a named tuple with explicit `.error`.
- Topic timeout hardcoded 5 s — slow USB cameras exceed on cold boot.
- Truncation reason not stored in episode metadata.

---

### 3.6 React SPA `physical_ai_manager`

Many of these duplicate `FRONTEND_UX_FOLLOWUPS.md`; only the net-new issues listed here.

**Critical**
- **401 on `/me` leaves app in limbo.** `StudentApp.js:122–141`, `WebApp.js:40–63`. Exception swallowed; role check never runs; Redux has session but no profile → blank UI forever. On 401, call `signOut()` + redirect to login.
- **`useVersionCheck` loops on transient 503.** `src/hooks/useVersionCheck.js:14–24,53–61`. `/version.json` 503 → null → sessionStorage guard early-exits → retry every 30 s forever. Increment the guard counter on every attempt, back off exponentially.
- **No confirmation on "Finish" recording** — one click discards hours of unsaved episodes. Modal: "X Episoden werden gespeichert — wirklich fertig?"

**High**
- **Image stream keep-alive connection leak** on component swap (FOLLOWUPS mentions; fix is to force `Connection: close` on MJPEG or set `src` to failing data URI before unmount).
- **Credit delta regex strips non-digits silently** → pasting `-50` becomes `50` → confusing UX. Use `inputMode="numeric"` with validation error.
- **Native `window.confirm()` for teacher/student deletes blocks React event loop.** Replace with async Modal.
- **Admin "delete teacher" doesn't show cascade.** No list of orphaned students before delete.
- **Railway web deploy (`Dockerfile.web` + nginx) serves `/index.html` for any path with HTTP 200** → SEO/crawlers index SPA as one page. CSP/HSTS/X-Frame-Options now set in `nginx.web.conf.template`; `vercel.json` was never a real deploy target.
- **Start-training button has no loading/disabled state** → students click 5× thinking it didn't register (API dedupes first attempt, shows 1 training but button never updates).

**Medium**
- **`useRefetchOnFocus` no debounce** — rapid tab-switching hammers `/quota`.
- **Policy allowlist `REACT_APP_ALLOWED_POLICIES` is client-side only** — dev build with unset env exposes full dropdown; backend correctly rejects but UX is noisy.
- **Realtime vs. polling status not shown.** `isRealtime` boolean conflates "realtime active" vs. "realtime unavailable" vs. "Supabase completely down". Add a status badge.
- **Progress entries timezone rollover** — teacher writing at 22:00 Berlin may save as tomorrow (UTC already rolled).
- **`console.log` littered across `ControlPanel.js`, `DatasetSelector.js`, `ImageGrid*.js`** etc. Info disclosure in prod.
- **Synthetic email regex doesn't reject `@` in username** — validation at login form only, not on teacher-creating-student form.

**Low / Nit**
- No dark mode (FOLLOWUPS #13).
- Plural grammar: `classrooms.length === 0` → "über 0 Klassen".
- Missing aria-labels; focus-trap on modals; keyboard-only navigation broken.
- `i18n` not extracted — multi-language migration would be a rewrite.

---

### 3.7 Cloud training (Railway + Modal + Supabase)

**Critical**
- **RLS bypass via service role.** See top-20 #7.
- **Worker-token nulled on terminal status only.** `migration.sql:152–155` + `training_handler.py:584`. Leaked token has up to ~5-min window where an attacker can push fake losses. Null on first successful RPC.
- **Modal dispatch failure leaves row `queued` forever.** See top-20 #8.
- **Stalled-worker sweep (`STALLED_WORKER_MINUTES=15`) can cancel legitimately slow jobs** — pi0 checkpoint save can take 20+ min on disk-slow weekends. Track Modal-side status ("IN_PROGRESS" still reported?), not just progress RPC freshness. Add per-policy override.
- **HF `hf_hub_download()` preflight has no timeout.** `training_handler.py:172–187`. See top-20 #14. Wrap in `asyncio.wait_for(timeout=60)`.

**High**
- **HF upload failure marked "succeeded".** `training_handler.py:581–587`. Status flipped to 100% before upload; if upload fails, row says succeeded but model isn't on HF. Track upload status in a separate column; only mark succeeded after verified upload.
- **Credit-math race between `adjust_student_credits` and `start_training_safe`.** Teacher has 100 credits; two concurrent students each requesting 60 → both reads pass → over-allocation. `start_training_safe` doesn't lock the teacher row; it locks the user row. Need row-level lock coordination across both RPCs.
- **Dedupe bypass via `steps` parameter tweak.** See top-20 #20.
- **CORS `allow_credentials=True` + `ALLOWED_ORIGINS` env-split without validation** — a wildcard or typo breaks the allowlist open. Validate each origin at startup.
- **Capacity-check race in `create_student`**: auth user created before 30-student trigger check — if trigger raises, auth user is orphaned. Wrap in transaction with rollback.
- **Dataset preflight hard-fails on `codebase_version != "v2.1"`** → student loses the call (no credits charged, but the error surfaces late). Allow compatible versions; version-tolerance matrix.
- **Subprocess stdout regex parser is lossy on LeRobot log format changes.** Silent no-parse → no Supabase progress updates → stalled-sweep kills the legitimate run. Log every line to a file; emit warning if no regex match for >10 min.
- **Worker SIGINT/SIGTERM handler does one Supabase update.** If that network call fails, row is stuck "running" forever. Retry 3× with backoff.
- **No rate limiting on `/trainings/start`** — a student script can hammer the endpoint. Per-user max-concurrent + SlowAPI middleware.
- **Realtime on `trainings` table (006)** — verify RLS actually applies to realtime subscriptions (SDK version dependent); test with a second user's JWT.

**Medium**
- **Admin-delete teacher** (`admin.py:228–253`) refuses if classrooms > 0 — no `cascade=true`; operator manually deletes students → classrooms → teacher. Add cascade option.
- **Progress-entry RLS lets students read class-wide notes** — teacher writes "this class struggles with X" → students see it. Add `visibility` field or split policy.
- **Bootstrap admin password** typed at CLI → shell history risk. Generate random, force change on first login.
- **Dedupe window ignores different hyperparams** — identical (dataset, model) with different batch_size burns 2 credits.
- **German error messages in API JSON responses** (`admin.py:240`, `teacher.py:115,130`) — doc says API is English. Untranslate.
- **No rollback plan for any migration.** Pair every `NNN_forward.sql` with `NNN_rollback.sql`; test monthly.
- **`ALLOWED_ORIGINS` default `http://localhost`** matches *any port* → same-machine rogue service can authenticate. Require explicit port.
- **O(N²) latency in `list_teachers()`** — per-teacher `get_teacher_credit_summary` RPC + classroom count query. Rewrite as one JOIN.

**Low / Nit**
- Modal timeout (7h) vs FastAPI per-policy cap mismatch → two error paths.
- Synthetic email `@` handling — strict regex on create, but function itself isn't defensive.
- Migration 003 applied twice — idempotency of enums.
- RLS policies on trainings are dead code under service-role (see §2.4).
- No compound index on `progress_entries(classroom_id, student_id, entry_date)`.
- Typos in German error messages (`ueber` → `über`).

---

### 3.8 Inference

**Critical (safety)**
- **No clamp / NaN guard / velocity limit before `publish_action()`.** See top-20 #1.
- **Camera-match error raises from ROS executor callback** → executor may hang / crash silently. Catch + downgrade to skip-tick + log; don't let the exception escape the callback.
- **Stale camera continues inference.** See top-20 #3.
- **GPU silent fallback to CPU.** See top-20 #4.

**High**
- **Lazy `PreTrainedPolicy.from_pretrained()` on first tick** → first HF download happens *during* the inference loop, blocking ROS executor for minutes. Preload in a background thread on `START_INFERENCE` receipt; only arm the timer after `load_policy()` returns.
- **HF model cache is container-local** → distro reimport wipes it. Mount `~/.cache/huggingface/hub` as bind to host or named volume.
- **No inter-tick timeout** — if `predict()` takes longer than 1/fps, messages queue. Skip queued tick if the previous hasn't published.
- **No memory cleanup between ticks** — long inference runs leak; add `gc.collect()` periodically.

**Medium**
- **Joint-order drift between recording config and inference config silent** → trained model's state vector mapped to wrong physical joints. Store `joint_order` in the policy's `config.json` at training time; validate at inference load.
- **Inference publishes gripper in same JointTrajectory as arm** — cross-check controller accepts mixed DoF.
- **No retry-backoff on HF download failure** → infinite retry on partial download.
- **Patch regex fragility** (duplicate of §3.4).
- **Camera name remap not configurable** — some students may have cameras named `camera_left` / `camera_right`. Allow a remap config in `omx_f_config.yaml`.

**Low / Nit**
- Image channel count (C=3 vs C=1) not validated — grayscale camera vs RGB model silently shape-mismatches coincidentally.
- `load_policy()` has no timeout — UI hangs on hung HF.
- `STOP` command doesn't zero the arm — last position held indefinitely. Publish a "home" trajectory on stop.
- ROS callback bodies lack blanket try/except — single exception in `_camera_callback` crashes the node.
- `server_inference.py` (ZMQ path) is dead code — never imported.

---

## §4. Cross-cutting ops / security / governance

Issues that don't belong to any one stage.

**Critical**
- **Privacy policy / DSGVO compliance missing** — see §2.9.
- **No CI/CD pipeline.** `build-images.sh` runs manually on the maintainer's machine. A clean-room rebuild has never been validated; overlay/patch no-ops would go undetected for weeks. Add a GitHub Actions job that runs `build-images.sh` + a smoke test on every push to main.
- **No monitoring / alerting** on any production surface. See §2.8.

**High**
- **Local `.env` still contains stale `RUNPOD_API_KEY`** (file is gitignored, never committed — verified). Not a git exposure but the key is still live on HF/RunPod and on the maintainer's disk. Rotate and delete.
- **Named Docker volumes destroyed on `wsl --unregister`** during upgrade → see top-20 #9.
- **No data export / Art. 15 / Art. 17 endpoints** — students can't download or delete their data. GET `/me/export`, POST `/me/delete`.
- **HF datasets public by default** — `training_handler.py` `create_repo()` lacks `private=True`. Student robot recordings leak to the world.
- **Version drift across 5 sources.** See §2.10.
- **Supabase region** — verify EU-West not US. Part of GDPR.

**Medium**
- **Dependency pins use `>=` with no upper bound** in both `cloud_training_api/requirements.txt` and `gui/requirements.txt`. A Modal breaking release takes down training. Pin to `==`.
- **`pythonnet>=3.1.0rc0`** pre-release dependency forces `pip install --pre` globally → all deps get pre-release versions. Pin to stable once available; document why the RC is needed.
- **No documented runbooks** for: rotating HF_TOKEN, rotating SUPABASE_SERVICE_ROLE_KEY, upgrading LeRobot version, rolling back a bad release, investigating a stuck training, migrating a classroom between teachers.
- **Tests cover only GUI path** (`test_docker_manager_wsl.py`, `test_config_generator.py`). No tests for credit math, RLS, migrations, recording, inference. Tests fail on non-Windows CI (no `wsl` binary) because they lack `@unittest.skipUnless(platform == "win32")`.
- **Language boundary violated** — German strings in API responses (`admin.py:240`, `teacher.py:115,130`) and in CLAUDE.md which says "API is English".
- **Installer silently uninstalls Docker Desktop** with no "are you sure?" — running containers get killed mid-write.
- **No session timeout on teacher/admin web dashboard** — indefinite JWT.

**Low / Nit**
- `.gitignore` is fine; `installer/assets/*.tar.gz` correctly excluded (200 MB binary).
- `CHANGES_SESSION_*.md` reference line numbers that may drift — pin to commit SHAs.
- `.s6-keep` empty file is undocumented in the filesystem (comment in Dockerfile only).
- Dead code: `RUNPOD_*` in `.env.example` post-Modal cutover.

---

## §5. Suggested action plan

Not every fix is equal weight. Triage by dependency order:

### Phase 0 (this week)
Classroom-hardware safety issues. Students handle real arms on real desks.
- §3.3 C1–C5: joint-limit clamp, NaN guard, torque-disable on shutdown, quintic sync verification, E-stop/heartbeat, stale-camera halt.
- §3.8 C1–C4: action validation before publish, GPU-fallback assertion, camera-match not-from-callback.

### Phase 1 (next 2 weeks) — silent degradation
Data-loss + data-correctness. Students lose work to silent failures.
- §3.5 C1–C5: time sync, video encode verification, empty-trajectory halt, HF upload timeout+resume, RAM truncation surfaced.
- §3.4 C1–C2: overlay `find+cp` post-verification, patch assertion.
- §3.2 C3: quote `.env` paths.

### Phase 2 (next month) — authorization + durability
Security and operational risk.
- §3.7 C1–C2: RLS authoritative OR RPC-only; worker-token single-use.
- §2.7 student-data durability: named volumes as host bind-mounts; HF push gating upgrade; export+delete endpoints.
- §2.3 ROS_DOMAIN_ID per-machine UUID.
- §2.1 systemic "surface status to UI not stderr".
- §4 CI/CD pipeline + Sentry + uptime probe.

### Phase 3 (next quarter) — governance
- §2.9 privacy policy + Supabase region verification + HF private-by-default.
- §2.10 single VERSION file.
- §4 runbooks for rotation/rollback/migration.
- §4 test coverage for credit math + RLS + migrations.

---

## §6. What wasn't covered

Worth noting for future audits:
- **LeRobot upstream audit** (v0.2.0 @989f3d05). Byte-identical snapshot; if upstream has a bug, so do we.
- **Full React source walk.** `FRONTEND_UX_FOLLOWUPS.md` + the agent pass touched the surface; a component-by-component review is its own scope.
- **Runtime / dynamic testing.** Everything here is static. Claims like "can hang forever" or "race can occur" need empirical reproduction before weighing investment.
- **Penetration testing.** Nothing here simulates a motivated attacker (e.g. Supabase JWT forgery, Modal RCE via crafted checkpoints, rosbridge injection from another LAN host).
- **Load testing.** Dedupe behavior, realtime subscription scaling, Modal dispatch under burst, HF rate limits — all untested.
- **Chaos testing.** What happens when HF goes down for 30 minutes? Supabase is rate-limited? Modal revokes credentials mid-training? Each is a scenario that deserves a scripted drill.

---

**Document status:** draft. Findings are from a static code review conducted 2026-04-24. Re-run after any material change to `physical_ai_server/` overlays, `training_handler.py`, or migrations 007+.

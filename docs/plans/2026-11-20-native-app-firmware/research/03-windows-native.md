# 03 — The native Windows application: research (architect 3)

Date 2026-11-24 · repo HEAD `dba3ac81` (release 2.41.0) · read-only study, nothing in the tree changed.

**Tags.** **VERIFIED** = read in the code (`file::symbol`), run here (command + output in Appendix A) or quoted from a cited URL. **REFUTED** = the claim is false. **UNVERIFIED-WINDOWS** = needs a Windows run; the deciding spike is named (§15). **UNVERIFIED-BENCH** = needs the arm/board.

**Host limits.** macOS, Python 3.13.13, uv 0.11.16, pip 26.0.1, pdftotext. No Windows, no pwsh, no JDK runtime (`/usr/bin/java` is the macOS stub: "Unable to locate a Java Runtime"), no hardware. Scratch: `/private/tmp/claude-501/-Users-svenborodun-Desktop-eduboticscode-Testre/dfe0e2ca-98df-46f4-9a43-9af94cb59472/scratchpad/agent3/`.

---

## 1. Executive findings

1. **The Phase-0 root cause is VERIFIED as a code path, not just a candidate.** `import_edubotics_wsl.ps1` runs `Remove-Item … ext4.vhdx` in its failure branch whoever owns the disk. WSL's same-path guard only reads the CALLER's registry (`LxssUserSession.cpp::_ValidateDistributionNameAndPathNotInUse` gets the caller's `lxssKey`). So when a second Windows account imports into the shared `C:\ProgramData\EduBotics\wsl`, the import reaches `CreateVhd` and fails on the existing file. WSL deletes nothing (`deleteFlags` is still 0), and then our script deletes the other account's disk whenever that account's VM is not holding it open. WSL already deletes any VHD it created itself when an import fails (`deleteFlags = LXSS_DELETE_DISTRO_FLAGS_VHD` is set right after `CreateVhd`), so our blanket delete protects against nothing. The exact sequences are in §3.4.
2. **Today's product does not work for standard-user students when an admin did the import**, and the GUI says so itself (`gui_app.py::_prompt_finalize_install`: „Eine WSL-Umgebung gehört immer genau dem Windows-Konto, unter dem sie eingerichtet wurde"). The GUI's advice („Installer erneut ausführen") is one of the two ways into the deletion.
3. **CPython has no `AF_UNIX` on Windows in any release up to 3.15** (gh-77589 is still open; PR #137420 targets 3.16). The brief's plan that „the two sockets become AF_UNIX" is **REFUTED** for the Python half: the engine's server (`code_rpc.py::CodeRpcServer.__init__`) and the Python stub (`runner/lib/robot.py`) both need it. Named pipes with an explicit security descriptor are required. `multiprocessing.connection` creates its pipes with a NULL descriptor, which an AppContainer cannot open.
4. **The whole engine dependency set resolves for win_amd64 on Python 3.12 and 3.13 with binary wheels only, after two forced changes:**
   - `pupil-apriltags` moves from `1.0.4` (Windows wheels only for cp36/cp37) to `1.0.4.post11`.
   - numpy becomes 2.2.6. lerobot 0.5.1 *requires* `numpy>=2.0,<2.3`, and `numpy==1.26.4` has no cp313 wheel; that cap exists only for ROS `cv_bridge`.

   The result is 97 packages and **564.9 MB of compressed wheels**. 183 MB of that is four packages that neither the recorder nor Daten imports at module scope (rerun-sdk, imageio-ffmpeg, cmake, wandb). A curated `--no-deps` lock would be about 382 MB compressed.
5. **torch is unavoidable.** `lerobot.datasets.utils`/`lerobot_dataset`/`video_utils` import torch (and torchvision) at module scope, and our own `data_processing/data_converter.py` builds `torch.tensor`s.
   - torch 2.7.0's Windows wheel is 212.5 MB compressed. A 2021 data point puts a Windows CPU wheel at roughly 0.9 GB once installed.
   - The installed app is therefore likely **1.2–1.8 GB** (UNVERIFIED-WINDOWS, S8).
   - The brief's „≤ 600 MB" can only hold as a compressed download. Per-user installs multiply the disk figure by the number of Windows accounts.
6. **Python 3.13 is required for the recorder clock** (VERIFIED). 3.13's What's New: `monotonic()` moved from GetTickCount64 (15.6 ms) to QueryPerformanceCounter (1 µs), and `communication/communicator.py` stamps every arrival with `time.monotonic()`. A further trap: on Windows 11 a minimised or occluded process loses its higher timer resolution unless it opts out.
7. **The HAT (A) carries two CH343P USB-UART bridges** (VERIFIED, pdftotext of Waveshare's schematic). That is the same chip family as today's V1 adapter (`1A86:55D3`).
   - Windows 10+ binds its inbox `usbser.sys` to CDC-ACM devices by compatible ID, with no driver install and no admin (VERIFIED, MS doc). WCH's own driver may take over through Windows Update; the owner's bench PC showed „USB-Enhanced-SERIAL CH343 (COM5)".
   - Remaining risks: school device-installation policies; the ESP32 auto-program circuit (DTR/RTS can reset the board when a port is opened); two COM ports with the same VID:PID.
8. **Code signing is a hard prerequisite in schools, not polish:**
   - AppLocker's default rules allow only `%WINDIR%` and `%PROGRAMFILES%`, so a per-user install in `%LOCALAPPDATA%` is blocked wherever AppLocker is enforced.
   - Smart App Control checks every DLL, `.pyd` included, against Microsoft's cloud reputation or a Microsoft-trusted signature.
   - Since 2024 an EV certificate no longer skips SmartScreen.
   - **Azure Artifact Signing accepts EU *organisations* but individuals only in the US and Canada** (MS Learn, updated 2026-11-10). The unsettled legal entity therefore blocks the cheapest CI signing path.
9. **The brief's per-user-only install fails three school realities:** AppLocker; reset-on-reboot PCs (PC-Wächter, Deep Freeze); and N student accounts × ~1.5 GB each. Recommendation: two channels from one signed build — a per-machine MSI for IT and a per-user self-updating installer for unmanaged PCs — plus an HKLM policy for managed updates and the data location.
10. **Student-code sandbox: AppContainer + Job Object is feasible without admin on paper, and maps all four Rule §2 conditions.**
    - Evidence: `CreateAppContainerProfile` is per-user. WebView2 (v120+) runs its renderer process inside an AppContainer (MS Learn distribution page). Crashpad's broker pipe descriptor is the production precedent for pipe access from an AppContainer.
    - Stronger than the container on process containment: `TerminateJobObject` leaves no `setsid` escape.
    - Weaker on reads of world-readable system folders, and on resource bounds: no RLIMIT_FSIZE or RLIMIT_NOFILE equivalent, and the job priority limit needs a privilege.
    - CPython and the JVM under a lowbox token are UNVERIFIED-WINDOWS and must be spiked first (S1–S5).
11. **„React unchanged" largely holds, with two exceptions.** `localRosbridgeUrl` uses `location.host`, `videoStreamBase` is relative and `isLoopbackPage` checks only the hostname, so any `127.0.0.1:<port>` works (VERIFIED). But:
    - An origin includes the port, so **the port must stay stable per user**. Otherwise localStorage and IndexedDB are lost — including the Blockly crash-recovery autosave.
    - `RS_LOCAL_BASE = 'http://localhost:8769'` is hard-coded.
12. **The loopback port is reachable by every local account, and today's Origin/Host checks authenticate nobody.**
    - Today any local process can drive the arm: nginx deliberately admits an *absent* Origin on `/rosbridge` (`health_checker.py` comment), and WSL publishes it on `127.0.0.1:80`.
    - The native app needs a per-session secret (an HttpOnly cookie set by the host) on every HTTP and WebSocket request.
13. **„HF token in a DPAPI-encrypted file" is REFUTED as a regression.** On a shared Windows account — the case the GUI's `?fresh=` handover scrub exists for — DPAPI does not separate students. The token must stay in memory only, as today's tmpfs slot does.
14. **Daten and `hub_sync` rely on POSIX file semantics that Windows breaks**: `fcntl.flock`, `/proc` parent checks, `killpg`, directory renames while files inside are open, `rmtree(ignore_errors=True)`. They need a portability layer and a Daten smoke on Windows (S10). Separately, the data root `Path.home()/.cache/huggingface/lerobot` (`dataset_paths.py::dataset_root`) **roams** under roaming profiles and invites MAX_PATH failures. It has to move to `%LOCALAPPDATA%`, under a short path.

---

## 2. Refutations and corrections of the brief

| # | Brief says | Finding | Tag |
|---|---|---|---|
| B1 | „the two sockets become AF_UNIX (Windows 10+) or named pipes" | CPython lacks `socket.AF_UNIX` on Windows through 3.15 (gh-77589 open, PR #137420 unmerged, targets 3.16). Java (JEP 380) could do AF_UNIX, but the engine (Python) cannot listen on it. **Named pipes, with an explicit SDDL.** | REFUTED (for Python) |
| B2 | Bundle „a JRE" | The coding suite needs `javac` (real compile messages, A14 `[TECHNIK]` line; `supervisor.py::parse_javac_output`). A JRE has no `jdk.compiler`. Temurin 21.0.13.1 Windows x64: JDK zip 205.4 MB, JRE zip 49.1 MB (Adoptium API). Use a **jlinked runtime with `jdk.compiler`** (size: S-J). | REFUTED |
| B3 | Python 3.13+ for the clock | Correct: 3.13 What's New says monotonic now uses QPC (1 µs), not GetTickCount64 (15.6 ms). lerobot 0.5.1 also needs ≥ 3.12. | VERIFIED |
| B4 | Target ≤ 600 MB | 97-package closure = 564.9 MB of compressed wheels; a curated lock is ≈ 382 MB, plus Python 10.9 MB and a JDK. On disk: torch alone is likely ~1 GB → **≈ 1.2–1.8 GB installed**, × N accounts if per-user. | REFUTED as disk size; plausible as download |
| B5 | „tmpfs token file → a file in the user's folder, encrypted with DPAPI" | Shared Windows accounts (`gui_app.py::_open_webview` comment: „One shared Windows account = one WebView2 profile") make DPAPI per-user, not per-student. Persisting would outlive today's clear-on-window-close (`docker_manager.clear_robot_hf_token_after_window_close`). Keep it in memory only. | REFUTED |
| B6 | One per-user installer `%LOCALAPPDATA%\Programs\EduBotics`, `PrivilegesRequired=lowest` | AppLocker default rules block it; frozen PCs lose it; N copies. Needed: a per-machine IT channel too (§4). | REFUTED as sole channel |
| B7 | Reuse today's server deps | `pupil-apriltags==1.0.4` has no cp312/cp313 Windows wheel → `1.0.4.post11` (2025-04-22, has cp312/cp313). numpy 1.26.4 has no cp313 wheel and contradicts lerobot's `numpy>=2.0,<2.3`. → the whole server suite must be re-proven on numpy 2.2. | VERIFIED (forced change) |
| B8 | „Driver: depends on the HAT (A) USB chip" | Two CH343P (schematic). Inbox usbser via compatible ID; vendor driver possibly via Windows Update; no admin normally. Risks: device-install policies, DTR/RTS resets (§5). | VERIFIED (chip), UNVERIFIED-WINDOWS (clean-PC binding, S13) |
| B9 | „95 `.py`, 6 import rclpy, `physical_ai_server.py` 8 908 lines" | Now 111 `.py`. **8** import rclpy (adds `daten/node_service.py`, `workflow/studio_camera.py`). `physical_ai_server.py` has 8 542 lines. | stale numbers |
| B10 | Daten 2.0 „ROS-free processes — reuse as is" | ROS-free yes, POSIX-free no: `hub_sync.stage_lock` (fcntl.flock), `download_worker` (fcntl, `/proc/<pid>/stat`), `node_service._kill_group` (`os.killpg`), `start_new_session`, `/run/edubotics-daten`, `/tmp/edubotics-daten`, `nice -n` prefixes, `hub_sync.swap_in` directory renames (§7.6). | REFUTED (needs a port) |
| B11 | „cameras read directly by the engine (no TCP bridge)" | Keep capture in its **own process** (GIL and driver isolation; §8.1). The IPC changes from TCP :5557 to a pipe; the `>BIQ` framing can stay. | CHALLENGED |
| B12 | `ws://localhost/rosbridge`, nginx checks re-implemented | Port 80 collides with the old product's WSL relay during coexistence. Origin/Host checks don't authenticate other users. The port must be stable or SPA storage is lost (§8). | CHALLENGED |
| B13 | Shutdown finalize = „explicit finalize on Beenden and app close" | Also needs `WM_QUERYENDSESSION` + `ShutdownBlockReasonCreate`, suspend (`PBT_APMSUSPEND`, about 2 s, no veto) and engine-crash supervision. At logoff/shutdown Windows likely grants only a few seconds, not the container's 30 s budget (§9.3; timing UNVERIFIED-WINDOWS). | INCOMPLETE |
| B14 | Phase 0 fix: per-SID folder + delete-only-own | Right direction, and add: **never delete a VHD** (WSL cleans its own); refuse when a foreign `ext4.vhdx` sits in the folder; best of all, **import un-elevated as the GUI user into `%LOCALAPPDATA%`** (`wsl --import` needs no admin), which also fixes B-finding 2 (§3.4). | REFINED |
| B15 | Firmware update „by the app" | If done with esptool (GPLv2+) it puts a GPL tool into a proprietary installer, and DTR/RTS boot-mode toggling resets the board. Prefer OTA over our own protocol with A/B partitions (for the firmware agent). | NOTE |

---

## 3. Q1 — What the Windows product does today, and what must survive

### 3.1 `robotis_ai_setup/gui/app/*.py` (15 038 lines) by function

| Module | What it does today | Native app |
|---|---|---|
| `constants.py` | VERSION (incl. `sys._MEIPASS`), `UPDATE_API_URL`, ports 80/8080/9090/5557/8444, dirs (`ENV_FILE`, `WEBVIEW_PROFILE_DIR`, `PHONE_CERT_DIR`, `diagnostics_dir`), registry/IMAGE_TAG, thin `ROBOT_PROFILES`, camera size/quality | **Keep:** version, URL, dirs, camera constants. **Drop:** registry/tag, ports 80/8080/9090/5557. |
| `config_generator.py` | Compose `.env` (`MANAGED_KEYS`), `_resolve_ros_domain_id` from machine GUID, legacy HF-token purge | **Drop.** The compose interface and DDS domain vanish; a settings file holds camera roles, tower profile, phone flag (robot type is fixed V1). |
| `device_manager.py` | usbipd list/attach/bind, arm identify via the docker prober, by-id markers, family notices, camera discovery, German USB diagnosis (`diagnose_usb_environment`) | **Drop** usbipd/docker. **Keep the behaviour:** an actionable German diagnosis („kein Arm gefunden", „nicht provisioniert", „falscher Arm", „Treiber fehlt"), now from COM enumeration + firmware `HELLO`/EEPROM verdict. |
| `docker_manager.py` | WSL-wrapped docker, keepalive, GHCR/Hub pulls + prune, compose start/stop (cloud-only too), `factory_reset` (volumes), `clear_robot_hf_token_after_window_close`, `stop_cloud_inference_after_window_close`, `read_tower_fall`, status probes | **Drop** Docker/WSL. **Keep:** factory reset (delete data dirs, with consent); token clear and cloud stop when the window ends (now in-process, simpler); tower verdict before stop/close/scan; ordered stop (sources outlive the finalize, VA-07). |
| `gui_app.py` (5 404) | Tk wizard: mode (cloud-only), robot type, arm scan, camera scan/preview/roles, tower camera probe, phone camera, start/stop, open web UI, factory reset, Protokoll copy/save, update check + forced modal, legal links, preflight, elevation helpers, finalize/rootfs flows, single-instance mutex, DPI | **Keep** the student-facing flows (§3.3). **Drop** elevation/finalize/rootfs/usbipd/WSL flows. |
| `health_checker.py` | Web UI + transports through nginx | → engine health endpoint |
| `phone_camera.py`, `phone_cert.py` | HTTPS receiver on `0.0.0.0:8444` (phone as cam_id 2); self-signed cert via `new_phone_cert.ps1` | **Keep.** Cert via `cryptography`. Inbound LAN port needs a firewall rule a standard user cannot add — ship it in the IT MSI (INSTALL_SPLIT_PLAN M5 already found this). |
| `roboter_studio_control.py` | `127.0.0.1:8769` OMX follower-only flip | **Drop** with the OMX. The SPA's `RS_LOCAL_BASE` must move to a same-origin path if a V1 leader toggle is wanted. |
| `scene_split.py` (twin) | Tower camera 1080p MJPEG verifier, split, `selftest`, state JSON | **Keep** in the capture process. Re-run its goldens on the chosen OpenCV (§7.1: lerobot forces opencv-headless < 4.14; the GUI lock has 5.0.0.93). |
| `theme.py` | Tk styling | Drop (UI moves to the web view). |
| `tower_stop_dialog.py` | Asks before a stop when a limp V1 falls back toward the tower | **Keep the behaviour** (as a React or native dialog). |
| `update_checker.py` | `/version`, HEAD 404 pre-check, download + Content-Length + SHA-256, legal links | **Keep**, plus a signature check (§4.6). |
| `usbipd_resolver.py`, `wsl_bridge.py` | usbipd path resolution; WSL run/registration/classifier | **Drop.** |
| `webview_window.py` | pywebview child process, persistent non-roaming profile, `destroy_all` (WM_CLOSE), exit callback + session generation | **Keep the semantics** in the host (§9). |
| `win_camera.py`, `camera_bridge.py` | MSMF enumeration/capture, crop-to-aspect, per-camera sender threads to TCP 5557 (`>BIQ`), tower scene worker + hello senders (ids 16/17) | **Keep** in the capture process; TCP → local pipe. |

### 3.2 `robotis_ai_setup/installer/` (14 `.ps1`, 4 586 lines + `.iss`)

Everything WSL-, Docker- or usbipd-shaped vanishes: `preflight_system`, `migrate_from_docker_desktop`, `install_prerequisites` (DISM features, usbipd MSI), `configure_wsl`, `configure_usbipd`, `bind_devices`, `import_edubotics_wsl`, `pull_images`, `verify_system`, `finalize_install`, `virtualization_ready`, `wsl_distro_state`, `wsl_docker_ready`, `uninstall_stop_containers`.

What survives as product behaviour:
- the German EULA and `THIRD_PARTY_NOTICES` + `licenses\*`;
- shortcuts;
- an uninstall that asks before deleting user data (today `ConfirmDistroWipe`, default *Nein*);
- the support log idea (`%ProgramData%\EduBotics\logs` becomes a per-user log + a „Diagnose exportieren" ZIP);
- preflight checks (new ones: WebView2 present, signature intact, camera privacy, disk space, long-path status).

### 3.3 Product behaviour a native app must keep (anchors)

1. **Update channel.** `update_checker.check_for_update` (HEAD pre-check so a 404 never locks the modal), `download_installer` (Content-Length + SHA-256 gates, German reasons), the forced modal with „Überspringen" only after a failure, `cleanup_stale_installers`, `_launch_installer_and_exit` (tears down every handle, then `os._exit`).
2. **Legal links.** `update_checker.fetch_legal_links` / `safe_legal_url`, `gui_app._show_legal_links`.
3. **Cloud-only mode** (`cloud_only` → `?cloud=1`).
4. **Arm connection** (was `_scan_arms` + `_confirm_arm_scan_closes_window` + `_scan_blocked_reason`). It still ends a live session and still asks first.
5. **Cameras:** scan, preview (`_frame_to_photo_data` → `camera_bridge.crop_to_target_aspect`), roles (`_on_cameras_changed`, profile-scoped), privacy check (`win_camera.camera_privacy_blocked`), **tower camera probe** (`_on_tower_camera_toggled` → `camera_bridge.probe_scene_camera`), `scene_split.CaptureVerifier`/`selftest`.
6. **Phone camera** (`phone_camera.PhoneCameraServer`, `:8444`).
7. **Start/stop ordering** (`_stop_environment._do_stop`: window first, sources after the recorder's finalize).
8. **Window URL contract** (`_open_webview`): `_v=<version>` cache-bust, `?cloud=1`, `robot=`, **`fresh=<nonce>`** for `bootScrub.js`, 800 ms debounce, live-window short-circuit, system-browser fallback, non-roaming profile (`constants.WEBVIEW_PROFILE_DIR`).
9. **When the window ends without „Abmelden":** stop Cloud-Inferenz and clear the robot's HF token, superseded by a newer session (`webview_window.set_exit_callback`/`begin_session`/`is_current_generation`, single-flight).
10. **Tower stop dialog** before stop, close or scan (`_ask_tower_fall_first`).
11. **Factory reset** (`_factory_reset`).
12. **Protokoll** copy/save; diagnostics; preflight on a daemon thread.
13. **Single instance** (`_acquire_single_instance`, `Local\EduBotics_GUI_SingleInstance`, focus the existing window).
14. **DPI scaling** (`theme.apply_scaling`).

### 3.4 Phase 0: can a failed import delete ANOTHER account's `ext4.vhdx`? — VERIFIED path

**Code facts.**

- **(a) Our script deletes whatever disk sits there.** `import_edubotics_wsl.ps1`, failure branch after `wsl --import`:
  `if (Test-Path $InstallRoot) { try { Remove-Item -Path (Join-Path $InstallRoot 'ext4.vhdx') -Force -ErrorAction SilentlyContinue } catch {} }`. There is no ownership check, and the default `-InstallRoot` is the shared `$env:ProgramData\EduBotics\wsl`.
- **(b) Registration is per Windows account.** Both the script's `Get-EduBoticsDistroRegistration` and the installer's `IsDistroRegistered` (`.iss::ShouldImportDistro` returns True when it is false) look only at the running account's registration. So a second account sees „Absent" and imports.
- **(c) WSL's „path already used" guard reads only the caller's registry.** In WSL's own source (`microsoft/WSL` master, `src/windows/service/exe/LxssUserSession.cpp`): `_ValidateDistributionNameAndPathNotInUse(lxssKey.get(), …)` throws `ERROR_FILE_EXISTS` only when *the caller* has another distro at the same path.
- **(d) The new disk then fails on the existing file.** For a tar import WSL calls `wsl::core::filesystem::CreateVhd(configuration.VhdFilePath, …)` (`WslCoreFilesystem.cpp`, `CreateVirtualDisk`), which fails when the file exists. The resulting error text is the „Wsl/Service/RegisterDistro/ERROR_FILE_EXISTS" that Docker users report (Docker forum thread).
- **(e) WSL does not delete a disk it did not create.** The cleanup only removes the disk when `deleteFlags = LXSS_DELETE_DISTRO_FLAGS_VHD` was set, and that happens *after* `CreateVhd` succeeds. Conversely, a disk WSL created during a failed import is deleted by WSL itself — the „partial VHDX" our script's comment worries about is already handled.
- **(f) Our classifier calls it nothing in particular.** `Get-WslFailureClass` returns `""` for `…/RegisterDistro/ERROR_FILE_EXISTS` (no MountVhd/AttachDisk/CreateVm stage, no known code). The student sees „Prüfen Sie: Antivirus-Ausnahme, genug Speicherplatz, WSL2 aktiviert." — just after the other account's data was deleted.
- **(g) Admins can delete it.** `CreateVhd`'s descriptor grants `FILE_ALL_ACCESS` to the user *and BUILTIN\Administrators*, so any elevated run may delete it.

**Sequence A — the GUI's automatic finalize on a second admin-capable account.**
1. Account T (admin) installs and uses EduBotics. The distro is registered in T's hive; `ext4.vhdx` sits in `ProgramData\EduBotics\wsl`.
2. Account B (another admin, e.g. a second teacher or a local „Admin") starts EduBotics. `_run_prerequisite_checks_body` gets `distro_registration() == "absent"` and calls `_prompt_finalize_install` (no consent dialog, CLAUDE.md), which elevates as B.
3. Finalize calls import, and `wsl --import` fails with `ERROR_FILE_EXISTS`.
4. If T is logged off, or T's WSL has idled out (~60 s `vmIdleTimeout`), the file is not attached, so `Remove-Item` succeeds. **T's datasets, HF cache and calibration are gone.**
5. B's next launch imports a fresh disk at the *same path*.
6. T's next start then fails with `ERROR_FILE_NOT_FOUND` (if B never re-imported — the field symptom) or with an access/sharing error (B's new disk, DACL = B + Administrators, while T runs WSL un-elevated).

**Sequence B — the GUI's own advice.** On a standard-user account the GUI says „…den EduBotics-Installer erneut ausführen, während Sie mit Ihrem eigenen Konto angemeldet sind." The installer is `PrivilegesRequired=admin`, so it runs as whoever types the credentials. If that is not the account that registered the distro, `[Run]` Step 4 (`Check: ShouldImportDistro`) imports and the same deletion follows. The same applies whenever a different admin runs the forced update's installer.

**Preconditions.** All three must hold: (i) the importing identity ≠ the registration owner; (ii) the owner's VHD is not attached; (iii) WSL refuses the existing file (S0 proves (iii) on a real PC; the code reads conclusively).

**Field forensics for 2026-11-20.** On that PC, check:
- `%ProgramData%\EduBotics\logs\edubotics_finalize.log` and `.prev.log`, plus `install_diagnostics.log`, for „wsl --import fehlgeschlagen" next to `RegisterDistro/ERROR_FILE_EXISTS`;
- the `user=` field in `edubotics_finalize.marker`;
- whether two accounts ever ran EduBotics there.

WSL's own `system.vhd` (the brief's other candidate) would name `system.vhd`, not the distro's `ext4.vhdx`.

**Phase-0 fix (refines brief §8).**
1. **Delete nothing** in the import failure branch — WSL cleans the disks it creates.
2. Before importing, refuse when `InstallRoot\ext4.vhdx` exists and this account has no registration pointing at it. Tell the user in German which account owns it, using the marker's `user=`.
3. Strongest: **run `wsl --import` un-elevated as the GUI user into `%LOCALAPPDATA%\EduBotics\wsl`** (no admin needed; INSTALL_SPLIT_PLAN §0 cites the WSL sources). Each account then has its own disk, the ProgramData junction attack surface disappears, and standard students finally work. Admin is left only for the WSL feature and usbipd (IT, once).

Gate: executed pwsh tests with a fake `wsl` (the existing `test_installer_pwsh_executed.py` pattern) for: foreign disk present; own disk present; locked disk; compressed folder.

---

## 4. Q2 — No-admin install and update on locked-down school PCs

### 4.1 What a school PC imposes

| Constraint | Fact | Tag / source |
|---|---|---|
| AppLocker | Default executable rules: Everyone may run `%WINDIR%\*` and `%PROGRAMFILES%\*`, Administrators everything. A per-user `%LOCALAPPDATA%` exe matches no default allow → blocked where AppLocker is enforced, unless IT adds a path or **publisher** rule (publisher rules need a signature). | VERIFIED — learn.microsoft.com „Executable rules in AppLocker" |
| Smart App Control (Win 11) | Checks *all* code the loader brings in, DLLs included, against the Intelligent Security Graph or a signature chaining to Microsoft's Trusted Root Program. An unknown unsigned DLL gets „Part of this app has been blocked". Usually off on enterprise-managed devices; since 25H2 it can be re-enabled on existing devices. | VERIFIED — textslashplain.com 2026-04-28; MS SAC FAQ |
| SmartScreen | Unsigned: strong block. OV / Artifact Signing: reputation builds per publisher. **EV lost its instant bypass in 2024.** | VERIFIED — MS Learn „Code signing options" (updated 2026-11-10) |
| Reset-on-reboot (PC-Wächter, Deep Freeze, UWF) | Everything written to C: by a student — a per-user install, its updates and its datasets — is reverted at reboot. Only an install made in maintenance mode persists. | VERIFIED (by mechanism); data-loss consequence §10 |
| Roaming profiles / FSLogix | `AppData\Local` does not roam; profile folders outside the default exclusions do, so `%USERPROFILE%\.cache` (today's `dataset_paths.dataset_root`) **would roam multi-GB datasets**. FSLogix containers do include LocalAppData, so per-user installs inflate every container. | VERIFIED — archived MS „Exclude directories in roaming profile"; Citrix doc |
| Shared PCs, many accounts | A per-user install is one copy per account: 30 accounts × ~1.5 GB ≈ 45 GB. | arithmetic on §7.2 |
| Long paths | `LongPathsEnabled` needs admin („Your administrator will need to activate…"). | VERIFIED — docs.python.org 3.13 „Using Python on Windows" |
| Firewall | Binding `0.0.0.0` (phone camera) raises a prompt a standard user cannot approve. Loopback binds raise none. | VERIFIED — INSTALL_SPLIT_PLAN §0 |

### 4.2 Install mechanisms compared

| Mechanism | Location | Admin to install | AppLocker default | Self-update without admin | Frozen PC | Copies | IT deployment | Verdict |
|---|---|---|---|---|---|---|---|---|
| Inno, `PrivilegesRequired=lowest` | `%LOCALAPPDATA%\Programs` | no | blocked | yes (silent re-run; INSTALL_SPLIT_PLAN 0b-3/4 traps) | lost | N | weak | fallback only |
| Velopack `Setup.exe` | `%LOCALAPPDATA%\<id>` | no | blocked | yes, delta packages; Python SDK on PyPI | lost | N | its MSI does per-machine, but „neither the updater nor the installer support privileged directories" | good per-user channel |
| Per-user MSI (`MSIINSTALLPERUSER`) | `%LOCALAPPDATA%\Programs` | no | blocked | yes (msiexec as user) | lost | N | Intune user context | niche |
| **Per-machine MSI (WiX)** | Program Files | **yes, once (IT)** | **allowed** | no → IT pushes updates | persists if installed while thawed | **1** | best: Intune, GPO, opsi | **school default** |
| MSIX sideload | shared package store | no (signed package; whether sideloading is on by default since Win10 2004 — sources conflict, UNVERIFIED) | separate packaged-app rule set (UNVERIFIED) | yes via `.appinstaller` | per user (provisioned persists) | 1 set of files | Intune/provisioning | `ms-appinstaller:` links off since Dec 2023; needs App Installer; FS/registry virtualisation; AppContainer children from MSIX unproven |
| MSIX via Microsoft Store | idem | no | Store apps | Store updates | — | 1 | Intune „Store app (new)", fragile when the Store is blocked | **free signing by Microsoft**, individual developer account is free; certification needed |
| Electron/Squirrel | `%LOCALAPPDATA%` | no | blocked | yes | lost | N | weak | heavy (bundles Chromium) |

### 4.3 Code signing in 2026, and what the missing legal entity blocks

- **Azure Artifact Signing** (formerly Trusted Signing): about $9.99/month. **Organisations: USA, Canada, EU, UK. Individuals: USA and Canada only.** Initial SmartScreen warnings are expected; CI-native. Org-age rules: one MS page says 3 years of tax history, MS Q&A says no minimum — unresolved. VERIFIED (MS Learn 2026-11-10; tax-history point UNVERIFIED).
- **OV certificate** ($150–300/yr, key on an HSM since 2023): issued to an *organisation*. CAs also sell individual-validated code-signing certificates to private persons. The certificate's subject then shows the owner's personal name in every UAC and SmartScreen dialog — a privacy choice, and inconsistent with the „[Anbieter – wird ergänzt]" placeholders. VERIFIED (MS Learn), IV availability per CA UNVERIFIED.
- **Store (MSIX)**: Microsoft re-signs, worldwide, no SmartScreen warnings, free individual account. VERIFIED (MS Learn).
- **What signing must cover:** for SAC every PE we ship (exe, dll, pyd: python313.dll, numpy/torch/av/pyarrow/opencv binaries). Count: S8. A 5 000-signature Basic tier may be tight if every build is signed. A signed stub that drops unsigned code does not pass SAC (textslashplain).
- **Without a legal entity:** Artifact Signing is unavailable to an individual in Germany. That leaves either an IV certificate in the owner's personal name, or the Store. A per-machine MSI for IT is expected to be signed (AppLocker publisher rules, SAC).

### 4.4 WebView2 runtime

- Evergreen runtime: part of Windows 11; „the vast majority of Windows 10 devices have the WebView2 Runtime installed already". The bootstrapper and the standalone installer install **per-user when not elevated**. VERIFIED (MS Learn distribution page).
- Fixed Version: **> 250 MB**, no automatic updates. On Windows 10 with Fixed Version ≥ 120 the renderer runs in an AppContainer, so the folder needs `ALL APPLICATION PACKAGES` + `ALL RESTRICTED APPLICATION PACKAGES` RX. VERIFIED. → Use Evergreen and ship the standalone installer as a fallback.
- Python 3.13 supports Windows 8.1+ (docs). Windows 10 is past end of support (Oct 2025, ESU only). **Support Windows 11 x64 + Windows 10 22H2 x64.**
- Windows-on-ARM: no `win_arm64` wheels for torch 2.7.0, torchvision 0.22.0, av 15.1.0, pyarrow 25.0.0, opencv-headless 4.13.0.92, numpy 2.2.6, pupil-apriltags post11 or cryptography 49 (PyPI JSON, Appendix A6). ARM laptops would run the x64 build under emulation (UNVERIFIED-WINDOWS).

### 4.5 Shell (window host) judged on reliability

| Host | Chain | Control | Portability | Judgement |
|---|---|---|---|---|
| pywebview 6.2.1 (today) | Python → pythonnet 3.2.1 → clr_loader → .NET Fx 4.8 WinForms → WebView2 .NET wrapper; must run in a **child process** because pywebview wants the main thread (`webview_window.py` header) | Thin: cookies, custom schemes and WebResourceRequested only through the raw .NET control | Win/macOS/Linux | Works in production today, but it is the longest native chain. pythonnet needed pre-releases for new Pythons (`requirements.txt` comment: 3.1.0rc0 for 3.14). |
| C++ Win32 + WebView2 SDK (static loader) | one exe | Full | Windows only | Minimal, but C++ and Windows-only. |
| **Tauri 2 (Rust, wry)** | one signed exe; Python engine as supervised sidecar | Full: custom protocols, IPC, single-instance plugin; **mandatory updater signatures** (cannot be disabled); NSIS installer (current-user or per-machine) and WiX MSI | WebView2 / WKWebView / WebKitGTK / iOS / Android | **Recommended**, *if* the owner accepts Rust: one native supervisor, also a natural home for the AppContainer launcher (windows-rs). |
| Electron | bundled Chromium + Node | Full | everywhere | Rendering is consistent, but +~250 MB per install (× N per-user), and the Python engine is still a sidecar. Not justified. |

### 4.6 Recommended packaging and update design

1. **One signed build, two channels.** (a) A **per-machine MSI** (Program Files, AppLocker default-compatible, one copy, can add the phone-camera firewall rule, IT installs in maintenance mode on frozen PCs). (b) A **per-user self-updating installer** (Velopack or Tauri NSIS current-user) for unmanaged PCs. Both sign every PE.
2. **Update integrity.** Keep `/version` plus the SHA-256, and add a **detached Ed25519 signature over (version, url, sha256)** checked against a public key compiled into the host. Also verify the Authenticode publisher on the downloaded file. Today the hash and URL come from the same API, so a compromised API could advertise a matching malicious installer.
3. **Managed mode.** `HKLM\SOFTWARE\Policies\EduBotics`:
   - `UpdateMode = managed` → no forced self-update. The app shows „Update verfügbar — bitte IT informieren" and stays within the cloud's N/N−1 window.
   - `DataRoot` → IT can point the data at a thawed drive.
   - `AllowPhoneCamera` → enable or disable the phone camera.

   Today's non-closable forced update loops on frozen PCs, because the reboot reverts the install every time.
4. **Offline:** the full installer contains every wheel (no pip at install time). WebView2's standalone installer is bundled for the rare missing-runtime case. Recording works offline; uploads and training need the network.
5. **Per-machine install layout:**
   - `C:\Program Files\EduBotics\` — read-only binaries: host, `python\` (embeddable 3.13), `site\` (vendored wheels, precompiled `.pyc`), `jdk\` (jlinked), `web\` (React build), `firmware\`.
   - Per-user under `%LOCALAPPDATA%\EduBotics\`: `data\` (datasets), `hf\` (`HF_HOME`), `calib\`, `logs\`, `webview\`, `phone-cert\`, `settings.json`.
   - Program Files grants `ALL APPLICATION PACKAGES` read/execute by default, which the sandbox needs (S1 confirms with `icacls`).

---

## 5. Q3 — USB serial without admin

### 5.1 The board's bridge chips (VERIFIED)

`pdftotext` of `Bus_Servo_Driver_HAT_(A)_Sch.pdf` lists **2× CH343P**, ESP-WROOM-32 („ESP-32S"), SP3485EN (RS485), SN74LVC1G125/126 (half-duplex buffers), SN74LVC1G08 and MP9447 (buck). One CH343P sits on each USB-C port: one feeds the ESP32 UART0 (DTR/RTS → the classic two-transistor EN/IO0 auto-program circuit), the other goes straight to the servo bus. Today's V1 adapter is also a CH343P (`1A86:55D3`, `cdc_acm` on Linux — CLAUDE.md, `constants.ARM_USB_IDS['v1']`).

### 5.2 Driver binding

- Windows loads inbox `usbser.sys` automatically for compatible IDs `USB\Class_02` / `USB\Class_02&SubClass_02`, with no INF and no admin. VERIFIED — MS Learn „USB Serial Driver (Usbser.sys)". The CH343P presents CDC-ACM (Linux binds `cdc_acm`), so the inbox driver applies.
- A hardware-ID match from WCH's own `CH343SER` package outranks the compatible ID. If Windows Update delivers it, it replaces usbser — still installed by PnP as SYSTEM, no admin. Evidence that this happens: `docs/CLAUDE-CHANGELOG.md` R1 (bench PC: „USB-Enhanced-SERIAL CH343 (COM5)" = WCH's naming).
- School risk: device-installation restriction policies (allowlists by hardware ID or class) and WSUS without drivers. IT then allows `USB\VID_1A86&PID_55D3` once — the same as today. **S13** decides it on a clean, offline, locked-down Windows 10 and 11.

| Bridge | Windows 10/11 without admin | Tag |
|---|---|---|
| CH343P / CH9102 (CDC mode) | inbox usbser via compatible ID; vendor driver optional via WU | VERIFIED (doc), clean-PC UNVERIFIED-WINDOWS |
| CH340 / CH341 (vendor class), CP210x | need the vendor driver, normally from Windows Update; blocked when WU drivers are off | UNVERIFIED-WINDOWS |
| ESP32-S3 native USB-Serial-JTAG | Espressif: Windows 10+ installs automatically when online, „in some cases" not (manual update = admin); JTAG needs WinUSB | VERIFIED (Espressif docs) |

**Prefer a CDC-class bridge** — the HAT (A) already has one.

### 5.3 Hazards specific to this board

1. **Reset on port open.** pyserial sets DTR/RTS from `_dtr_state`/`_rts_state` into the DCB at open (`serialwin32.py`: `fDtrControl = DTR_CONTROL_ENABLE if self._dtr_state else DTR_CONTROL_DISABLE`), so the engine must set `dtr=False, rts=False` *before* `open()`. Any other program opening the COM port with default lines can still pulse EN and reset the ESP32 (UNVERIFIED-BENCH, S13b). **Firmware consequence (for the firmware agent):** a reset must boot into a *safe* state. The brief's D7 „torque off → the arm falls" turns every stray open into a dropped arm; boot energise-and-hold (Goal = Present) avoids that.
2. **Two COM ports with the same VID:PID** whenever both USB-C cables are connected. Identify by protocol (`HELLO_ACK` with the ESP32 eFuse MAC), never by COM number. **The probe frame must provably not be a valid Feetech packet** (no `FF FF` header with a valid checksum): sent to the direct-bus port it reaches the servos verbatim.
3. **Identity.** COM numbers follow the USB serial number if the device reports one, else the physical port. Whether the CH343P exposes a unique serial number: UNVERIFIED-BENCH (read `serial.tools.list_ports` `serial_number`).

### 5.4 Latency, throughput, jitter

- pyserial on Windows uses overlapped I/O (`FILE_FLAG_OVERLAPPED`, `GetOverlappedResult` in `serialwin32.py::read`). VERIFIED (pyserial 3.5 source).
- A 100 Hz STATE stream of follower + leader (12 joints × q/dq/load float32 + header) is ≈ 200 B × 100 = 20 KB/s — trivial for a CH343P (full-speed USB, up to 6 Mbaud).
- usbser has no FTDI-style 16 ms latency timer; latency is set by the chip's own packing timeout and the 1 ms USB frame. The CH343P's internal timeout: UNVERIFIED-BENCH (S13c: echo round-trip histogram at 1 kHz under usbser and under the WCH driver, on the slow reference laptop).
- Jitter: one dedicated reader thread, registered with MMCSS (`AvSetMmThreadCharacteristics("Capture")`, no privilege — MS Learn). Board µs timestamps mapped by `ClockMap` (`communication/capture_timeline.py`), so PC jitter adds latency but does not corrupt the timeline.

---

## 6. Q4 — The student-code sandbox on Windows

### 6.1 Today (read in code)

- **Compose (`docker-compose.yml::code_runner`):** `init: true`, `user: "0:0"`, `cap_drop: ALL` + SETUID/SETGID/KILL, `read_only`, `network_mode: none`, `no-new-privileges`, tmpfs `/work` (64m) and `/tmp` (16m), `mem_limit`/`pids_limit`/`cpus`, no `environment:`.
- **Supervisor (`runner/supervisor.py`):** spawns as uid 10001 with `start_new_session=True` (`spawn_as_student`). Kill = `killpg(SIGKILL)` → `/proc` sweep for the uid × 3 → `waitpid` (`kill_sequence`).
- **Limits (`runner_limits.py`):** `RLIMIT_AS` 512 MiB (Python) / 1 GiB (Java), `NPROC` 32, `FSIZE` 32 MiB, `NOFILE` 64, `CORE` 0. JVM flags cap the heap and the reservations.
- **IPC:** `rpc.sock` 0666 (server listens, student connects); `runner.sock` 0600 (supervisor listens, server connects); the breakpoint pipe fd travels in argv.
- **Rule §2 conditions:** (1) budget at both ends; (2) out-of-band stop with derived deadlines; (3) every frame hostile; (4) different uids, supervisor not PID 1.

### 6.2 Windows design

```
host / broker (medium IL, user token)                 student run (lowbox token)
 ├─ AppContainer profile "EduBotics.CodeRunner"   ─┐   python.exe -I runner\student_main.py   or
 │   (CreateAppContainerProfile, per user, once)   │   java -XX:… edubotics.Launcher / java -m jdk.compiler/com.sun.tools.javac.Main
 ├─ per-run dir  %LOCALAPPDATA%\EduBotics\run\<id>  │   token: AppContainer SID + Low IL, 0 capabilities
 │   ACL: user F, AppContainer SID M (no inherit up)│   job:  JOB_OBJECT_LIMIT_{KILL_ON_JOB_CLOSE, ACTIVE_PROCESS,
 ├─ \\.\pipe\EduBotics-<rand>-rpc (data)            │          JOB_MEMORY/PROCESS_MEMORY, DIE_ON_UNHANDLED_EXCEPTION}
 │   SDDL: D:(A;;GA;;;<user>)(A;;GRGW;;;<AC SID>)   │          + CPU_RATE_CONTROL (hard cap), priority via BELOW_NORMAL at create
 │        S:(ML;;NW;;;LW), FILE_FLAG_FIRST_PIPE_INSTANCE
 ├─ anonymous pipes: stdout/stderr, breakpoints     │   env: CODE_RPC_PIPE, CODE_RUN_TOKEN, SystemRoot, TEMP=TMP=<run dir>
 └─ holds job + process handles (TerminateJobObject)┘   created suspended + PROC_THREAD_ATTRIBUTE_{SECURITY_CAPABILITIES,
                                                          HANDLE_LIST, JOB_LIST}, then resumed
```

Facts behind it:
- `CreateAppContainerProfile` „creates a per-user, per-app profile… for the current user", Windows 8+, no admin named. VERIFIED (MS Learn).
- AppContainers get network only through capabilities; the doc says Internet, intranet and server access „can be granted". VERIFIED (MS Learn „AppContainer isolation"). Loopback is blocked for AppContainers by default (MS UWP loopback docs; S1 must prove it for a desktop AppContainer).
- Pipe access from an AppContainer: Crashpad's broker pipe uses `D:(A;;GA;;;SY)(A;;GWGR;;;S-1-15-2-1)S:(ML;;;;;S-1-16-0)` plus GENERIC_ALL for the current user. VERIFIED (chromium crashpad `registration_protocol_win.cc`). Grant the **specific AppContainer SID**, not `ALL APPLICATION PACKAGES`, so no other packaged app can connect. The run token stays the second factor.
- Without an explicit label a pipe counts as medium integrity and the integrity check precedes the DACL, so a low-IL client fails. Hence the `ML` label (Microsoft NTDEV blog via search; Crashpad does the same).
- `multiprocessing.connection` creates pipes with `_winapi.NULL` security attributes (VERIFIED, CPython 3.13 `Lib/multiprocessing/connection.py`). Use pywin32 or ctypes `CreateNamedPipeW` with `ConvertStringSecurityDescriptorToSecurityDescriptorW`.
- **JIT inside an AppContainer works in principle:** WebView2 v120+ runs its renderer (V8, which JIT-compiles) inside an AppContainer (MS Learn distribution page, Fixed Version note). Do not set the dynamic-code mitigation (ACG). JVM specifics — `-XX:-UsePerfData` already set, `-Djava.io.tmpdir=<run dir>`, `-XX:+DisableAttachMechanism`, `user.home` unreadable — are UNVERIFIED-WINDOWS (S2).
- CPython: use the **embeddable package** („isolated from environment variables, system registry settings and installed packages"; no Tcl/Tk, no pip; needs the UCRT, present on Win10+ — VERIFIED, docs.python.org) with the runner's `._pth`. `sys.monitoring` is in the interpreter, so the PEP 669 debugger is unchanged. The student env must gain `SystemRoot` (Winsock init); `runner_limits.STUDENT_ENV_NAMES` and its lockstep test change accordingly.
- Job objects:
  - `KILL_ON_JOB_CLOSE` „causes all processes associated with the job to terminate when the last handle to the job is closed".
  - `PRIORITY_CLASS` needs `SE_INC_BASE_PRIORITY_NAME` — so lower the priority with a creation flag instead.
  - VERIFIED (MS Learn `JOBOBJECT_BASIC_LIMIT_INFORMATION`).
- Nested jobs: Windows 8+. A process already in a job is nested **only if neither job sets UI limits** (VERIFIED, MS Learn „Nested Jobs"). The broker's own job must therefore allow breakaway, or the student job carries no UI limits (S3 decides).
- Java client: `Robot.java`/`RpcClient.java` use `UnixDomainSocketAddress` + `SocketChannel.open(StandardProtocolFamily.UNIX)` (VERIFIED). These are generated from `workflow/robot_api.py`, so the switch to a pipe client (`RandomAccessFile("\\\\.\\pipe\\…","rw")`, synchronous, one request in flight under the client lock) is a generator change plus a re-render.

### 6.3 The four Rule §2 conditions on Windows

| Condition | Today | Windows | Change |
|---|---|---|---|
| 1. Call-rate budget at both ends | Stub floor + `RunSession.charge_call` token bucket before validation (`RPC_LIMITS`) | Identical logic. `read_frame`/`write_frame` get a pipe adapter with the same `recv/sendall/settimeout` semantics (overlapped I/O) | transport only |
| 2. Out-of-band stop, deadlines derived | Control connection to the supervisor, `CodeProgram.request_stop`, `CODE_STOP_DEADLINE_S = recv + control + join` | Broker keeps the job handle; stop = `TerminateJobObject` (the student cannot block it) + close the data pipe. „Connection end = kill" becomes the job's KILL_ON_JOB_CLOSE tied to the broker. Re-derive the deadline from the new components. | stronger |
| 3. Every frame hostile | `code_rpc._validate_call`, `validate_value`, `read_frame` bounds | Unchanged | none |
| 4. Different uids; supervisor not PID 1 | uid 10001 vs root with SETUID/SETGID/KILL under tini | **Different principal:** lowbox token (AppContainer SID + Low IL). The broker runs at medium IL and is unreachable from it. No PID-1/zombie concept on Windows. Spawning needs no privilege. The `/proc` sweep becomes job termination (no breakaway allowed). | replaced; S5 proves it |

### 6.4 Residuals against the container (honest list)

- **Reads:** the lowbox token reads whatever grants `ALL APPLICATION PACKAGES` read — `C:\Windows`, Program Files, our own install — but not user data. The container saw only its image.
- **No disk-size bound** (no tmpfs, no RLIMIT_FSIZE; NTFS quotas need admin) → the broker polls the run dir and kills the job above a cap. A burst can overshoot between polls.
- **No handle-count limit** (RLIMIT_NOFILE); the memory limit bounds it indirectly.
- **UI:** an AppContainer process can create windows on the user's desktop. UIPI stops it from driving our medium-IL windows. Job UI limits conflict with nesting (§6.2). Optional: a separate desktop (`CreateDesktop`, as Chromium does).
- **Elevation prompts:** can a lowbox process trigger a UAC consent prompt (`ShellExecute runas`)? UNVERIFIED-WINDOWS (S5).
- **Kernel/Win32k attack surface** is shared, as the container shared the WSL kernel. Win32k lockdown might break the JVM or CPython (S2).
- **A9 holds:** `socket` imports (network refused), `subprocess` works inside the job, `ctypes` works but every call is access-checked against the lowbox token.

### 6.5 Alternatives, honestly compared

| Option | Admin | Isolation | Breaks | Portable | Verdict |
|---|---|---|---|---|---|
| **AppContainer + Job** | no | network, files, process, IL | (to prove) JVM/CPython quirks | Windows only | **recommended on Windows** |
| Low IL + restricted token (+ job) | no | Low IL still **reads** user files (no-read-up is not default) and has **network** | a restricted lockdown token breaks loading CPython/JVM DLLs (Chromium needs a warm-up for this) | Windows | rejected |
| Windows Sandbox | yes (feature, Pro/Edu, VT-x) | VM | the WSL failure class again | Windows | rejected |
| WASM: Pyodide in a WebView worker; Java via CheerpJ / TeaVM / cloud javac | no | browser sandbox; `worker.terminate()` is a hard stop | **no threads** (the line sampler and student threads redesigned); no `subprocess`; no real sockets; ctypes without native DLLs (A9 changes); ~1.5–3× slower; synchronous robot calls need `Atomics.wait` (COOP/COEP) or JSPI; **real javac** only via CheerpJ (commercial licence) or a cloud compile (student code leaves the PC) | Windows, macOS, **iPad, Chromebook** | the cross-platform path for the other agent; not the Windows primary |

### 6.6 Spikes before this is trusted (all on Windows CI **as a standard user**, plus one locked-down school PC)

GitHub's runner account is an admin. Create a local standard user and run each spike through `Start-Process -Credential` or a scheduled task. A spike that ran as admin proves nothing about schools.

- **S1 — CPython in an AppContainer.**
  - Setup: profile create/reuse; grant RX on the runtime and RW on the run dir; start embeddable 3.13 with `-I`; `import socket, ctypes, subprocess`; `sys.monitoring` present.
  - Must fail: connect to 127.0.0.1:<engine port>; connect to an internet host; read `%USERPROFILE%\Documents\x`; open the engine process.
  - Must work: open the run pipe with the SDDL above.
  - Measure: start latency.
- **S2 — JVM in an AppContainer.** `java -version`; `javac` on the starter project inside the run dir (real error text); `java` with today's `JAVA_FLAGS` + `-Djava.io.tmpdir` + `-XX:+DisableAttachMechanism`; JIT active (`-XX:+PrintCompilation`); Win32k lockdown on/off.
- **S3 — Job limits.** Commit limit kills; `ActiveProcessLimit`; CPU rate hard cap holds an engine 30 Hz tick within its budget; KILL_ON_JOB_CLOSE when the broker is killed; nesting with and without UI limits when the broker itself runs inside a job.
- **S4 — Pipe transport parity.** Run `test_code_rpc*` and `test_code_program_stop.py` through the pipe adapter; `CODE_STOP_DEADLINE_S` holds.
- **S5 — Escape matrix.** Open the broker or engine (`OpenProcess`); write outside the run dir; create a Global named object; enumerate other pipes; DPAPI unprotect of a user blob; `ShellExecute runas`; `CreateProcess` with `CREATE_BREAKAWAY_FROM_JOB`.
- **S-J — jlink.** Runtime size with `java.base,jdk.compiler,java.compiler`; SmokeMain passes.

---

## 7. Q5 — LeRobot and the Python stack on native Windows

### 7.1 Resolution proofs (Appendix A2–A4)

- `lerobot==0.5.1` (PyPI JSON) declares: `requires_python >=3.12`; `numpy<2.3.0,>=2.0.0`; `torch<2.11.0,>=2.7`; `torchvision<0.26.0,>=0.22.0`; `av<16,>=15`; `opencv-python-headless<4.14,>=4.9`; `pyserial`; `cmake`; `wandb`; `rerun-sdk`; `imageio[ffmpeg]`; `datasets`; `diffusers`; `gymnasium`; and `torchcodec … ; sys_platform != "win32" …` (no torchcodec on Windows — consistent with „PyAV is the decoder"). VERIFIED.
- `uv pip compile --python-platform x86_64-pc-windows-msvc --python-version {3.12,3.13} --only-binary :all: --no-binary proxy-tools` for lerobot 0.5.1, torch 2.7.0, torchvision 0.22.0, av 15.1.0, pyarrow 25.0.0, huggingface_hub 1.23.0, aiohttp 3.14.1, cryptography 49.0.0, pywebview 6.2.1, pythonnet 3.2.1, pyserial 3.5, psutil, httpx:
  - with `pupil-apriltags==1.0.4` → **unsatisfiable** („has no usable wheels");
  - with `==1.0.4.post11` → **resolves, 97 packages, identical versions for cp312 and cp313**.
  - VERIFIED. (`pip --platform` was unusable here: pip evaluates markers against the host, so it demanded `pyobjc-core` — noted so nobody repeats it.)
- Per-package Windows wheels: VERIFIED (PyPI JSON).
  - torch 2.7.0: cp312 and cp313 `win_amd64` (CPU) 212.5 MB.
  - torchvision 0.22.0: 1.7 MB.
  - av 15.1.0: 31.3 MB.
  - pyarrow 25.0.0: 27.9 MB.
  - opencv-contrib-python 4.10.0.84: `cp37-abi3-win_amd64` 45.5 MB.
  - pupil-apriltags `1.0.4`: cp36/cp37 only; `1.0.4.post11`: cp312/cp313.
  - numpy 1.26.4: cp39–cp312 only; **no cp313**.
  - pythonnet 3.2.1: `cp311…cp315-none-any`.
- **numpy cap.** The thin server Dockerfile forces `numpy==1.26.4` *after* lerobot only for the ROS `cv_bridge` boost ABI (its own comment: „lerobot 0.5.1 pulls numpy 2.2.6"). Without ROS the cap is unnecessary and on 3.13 impossible → numpy 2.2.6. CI's `python-tests` pins numpy 1.26.4, so **the server suite has never run on numpy 2** → re-prove it.
- **OpenCV.** No contrib module is used any more (grep for `cv2.aruco|ximgproc|…`: none). ChArUco left in 2.41.0. → one provider, lerobot's `opencv-python-headless` (4.13.0.92 resolved). Today the GUI ships `opencv-python` 5.0.0.93 for MSMF capture. Whether the headless wheel keeps the MSMF backend is UNVERIFIED-WINDOWS (S16: `cv2.videoio_registry.getBackends()`). `scene_split` goldens must be re-run on the chosen version.

### 7.2 Sizes and the curated lock

| | MB |
|---|---|
| Full declared closure, compressed wheels (97 pkgs) | **564.9** |
| — of which not imported at module scope by the recorder/Daten closure: rerun-sdk 84.3, imageio-ffmpeg 38.9, cmake 37.5, wandb 22.3 | 183.0 |
| Curated `--no-deps` lock (the training lock's pattern) | ≈ **381.9** |
| Python 3.13 embeddable zip (HEAD content-length 10 926 675) | 10.9 |
| JDK: Temurin 21 JDK zip / JRE zip (Adoptium API) | 205.4 / 49.1 (jlink with jdk.compiler: S-J) |
| React `public/` today (OMX URDF 5.5 MB leaves with the OMX) | 11 |
| **Download estimate (compressed)** | ≈ 0.45–0.65 GB |
| **On disk estimate** (torch ~0.9–1 GB per the 2021 Windows-CPU data point: 184 MB wheel → 936 MB, mostly `dnnl.lib`/`mkldnn.lib`) | ≈ **1.2–1.8 GB** — S8 measures it and tests stripping `torch/lib/*.lib` + `torch/include` |

The static import closure (`scripts/import_closure.py` over the lerobot 0.5.1 sdist, module-level imports from `lerobot.datasets.{utils,lerobot_dataset,dataset_metadata,video_utils}` + `lerobot.utils.control_utils`: 114 lerobot modules) shows third-party imports PIL, accelerate, av, datasets, deepdiff, diffusers, draccus, einops, fsspec, huggingface_hub, imageio, numpy, packaging, pandas, pyarrow, safetensors, serial, torch, torchvision, tqdm. It also lists scipy/transformers/tree, which lerobot gates through `utils/import_utils.py::is_package_available`; the static scan over-approximates. **rerun, wandb, cmake and imageio_ffmpeg are absent.** S7 proves it at runtime (`python -X importtime`).

### 7.3 Does the recorder import torch? — VERIFIED yes

- `data_manager.py` top: `from lerobot.datasets.utils import DEFAULT_FEATURES`. That runs `lerobot/datasets/__init__.py`, which imports `lerobot_dataset` (`import torch`, `import torch.utils`); `datasets/utils.py` itself has `import torch` at module scope; `video_utils.py` imports torch and torchvision.
- `data_processing/data_converter.py` imports torch and returns `torch.tensor(…)` for state and action.
- torch is part of every recording.

### 7.4 Clocks and timers

- VERIFIED (3.13 What's New): „On Windows, monotonic() now uses the QueryPerformanceCounter() clock for a resolution of 1 microsecond, instead of the GetTickCount64() clock which has a resolution of 15.6 milliseconds"; `time()` uses GetSystemTimePreciseAsFileTime. `time.sleep` uses a high-resolution waitable timer on Windows 10+ (3.11+).
- The recorder's slot sampler depends on `time.monotonic()` (`communicator.py` arrival stamp; 43 uses in `physical_ai_server.py`). → 3.13, or perf_counter everywhere.
- `threading.Event/Lock.wait(timeout)` on Windows: waits in milliseconds at the system timer resolution (15.6 ms default). Periodic ticks must use `time.sleep`-based tickers or high-resolution waitable timers. UNVERIFIED-WINDOWS (S11 measures Event.wait jitter at 33 ms on 3.13).
- `timeBeginPeriod` is per-process since Windows 10 2004. „Starting with Windows 11, if a window-owning process becomes fully occluded, minimized, or otherwise invisible… Windows does not guarantee a higher resolution" (VERIFIED, MS Learn). → the engine and capture processes set `ProcessPowerThrottling` with `PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION` and `EXECUTION_SPEED` disabled (EcoQoS off).

### 7.5 Video encoder

- `lerobot_dataset_wrapper.py::EDUBOTICS_VCODEC = 'h264'` (software x264) + `streaming_encoding=True` (`_apply_edubotics_defaults`).
- pyav-ffmpeg's README builds libx264, x265, SVT-AV1, dav1d and vpx „for all platforms" (x264 except armv7l), Windows x86_64 included. VERIFIED (README, current). For av 15.1.0's own build: UNVERIFIED-WINDOWS (S7: `av.codec.Codec('libx264','w')` + `record_fsm_smoke.py` incl. `frame_drop_rerecord`).
- Licensing note: x264/x265 make that FFmpeg build GPL. Shipping it inside a proprietary *installer* deserves a licence review; today the robot images already carry the same wheel.

### 7.6 POSIX assumptions an engine on Windows must replace (VERIFIED, symbol-level scan, Appendix A8)

| Assumption | Where | Windows replacement |
|---|---|---|
| `fcntl.flock` stage lock; lock file re-opened, inode compared | `data_processing/hub_sync.py::stage_lock`, `_is_the_file_at`; `daten/download_worker.py` (module) | `LockFileEx` / `msvcrt.locking`. `st_ino` is the NTFS file id. Lock semantics differ (mandatory byte ranges, and deleting a file that is open fails). |
| Parent liveness via `/proc/<pid>/stat` | `daten/download_worker.py` (module) | Child inside the parent's KILL_ON_JOB_CLOSE job, or a wait on the parent handle |
| Process groups: `start_new_session=True`, `os.killpg(SIGKILL)` | `daten/node_service.py::_kill_group`, `DownloadProcess.__init__`, `DatenService.run_edit_process`; `download_worker.py::_remove_after_exit` | Job per helper; `TerminateJobObject`; `CREATE_NEW_PROCESS_GROUP` |
| Priority via `nice -n 10/19` prefixes | `launch/physical_ai_server_bringup.launch.py::generate_launch_description`; `daten/http_server.py` and `edit_worker.py` docstrings | `BELOW_NORMAL_PRIORITY_CLASS` / `IDLE_PRIORITY_CLASS` at create (`PROCESS_MODE_BACKGROUND_BEGIN` also lowers I/O and memory priority — measure) |
| SIGINT → finalize chain; `signal.signal(SIGINT, SIG_DFL)` | `physical_ai_server.py::main` (finally: cloud bye → `_finalize_recording_on_shutdown`) | Explicit shutdown RPC over the host's control pipe + `WM_QUERYENDSESSION`/`PBT_APMSUSPEND` handling (§9.3) |
| `AF_UNIX` | `workflow/code_rpc.py::CodeRpcServer.__init__`, `workflow/code_program.py::CodeProgram._connect_control`, `workflow/robot_api.py` (generated stub), runner `lib/robot.py`, Java `RpcClient` | Named pipes with SDDL (§6.2) |
| `/run/...` dirs (0700/0600 modes) | `daten/contract.py::SECRET_DIR`, `daten/link_tokens.py` (`/run/edubotics-daten`), `hf_token_store` (`/run/edubotics-hf`), `hf_token_crypto.KEY_FILE_DIR`, `physical_ai_server.py::PhysicalAIServer._init_code_rpc` (`/run/edubotics/code`) | Per-user `%LOCALAPPDATA%\EduBotics\run\` (the profile ACL is user-only; `os.makedirs(mode=0o700)` is ignored on Windows). The token stays in memory (B5). |
| `/tmp/...` | `inference/cloud_contract.py::ACTIVE_MARKER_PATH`, `daten/contract.py::CLIP_TMP_DIR` | Per-user dirs |
| `/root/.cache/edubotics/calibration` default | `physical_ai_server.py::_build_ik_solver`, `_build_scene_calibration`, `_fixture_guard`, `_load_workflow_calibration` | `EDUBOTICS_CALIB_DIR` → `%LOCALAPPDATA%\EduBotics\calib` |
| Data roots on `Path.home()` (`~/.cache/huggingface/...`) | `dataset_paths.py::dataset_root`, `model_root`, `hf_hub_cache_root`; `hf_token_store` cache path | `%LOCALAPPDATA%\EduBotics\{data,hf}` via `HF_HOME`/`HF_LEROBOT_HOME`; short paths (roaming + MAX_PATH) |
| `O_NOFOLLOW`/`O_NONBLOCK` via `getattr(os, …, 0)` → silently 0 | `scene_provenance.py::_stamp_bytes`, `_write_into_meta` | Check `st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT` (symlinks *and* junctions) before opening |
| Directory renames while files inside may be open | `hub_sync.py::swap_in`, `recover`; `data_editor_v3.py::split_episodes_v3`, `recover_split`; `node_service.py::DatenService._delete_job` (`os.rename(path, trash)`) | Windows refuses renaming a folder with open files. The sidecar's mp4/parquet readers and antivirus hold handles; Python's `open()` gives no FILE_SHARE_DELETE. → quiesce the sidecar's readers for the leased dataset before a swap, retry with back-off. S10. |
| `os.replace` over a file another process has open | `dataset_sync.write_json_atomic`, `DataManager._write_session_marker`, `autocal/store._atomic_write_json`, `hf_token_store.write`, `hf_token_crypto.write_key_file`, `scene_provenance._write_into_meta` | Can fail with WinError 5/32 → retry loop + readers that open briefly |
| `shutil.rmtree(..., ignore_errors=True)` | ~20 sites (`hub_sync`, `data_editor_v3`, `node_service`, `download_worker`) | Hides undeletable open or read-only files on Windows → log + retry + an orphan sweep |
| Hard links | `hub_sync.py::_link_base_videos` (falls back to copy), `daten/link_tokens.py::load_or_create_secret` (no fallback) | NTFS: fine, no admin. exFAT/FAT (USB sticks): fails → fallback |
| `multiprocessing.get_context('spawn')` | `data_processing/hf_api_worker.py` | Native on Windows; with the embeddable runtime set `multiprocessing.set_executable` |
| Case-insensitive paths, ADS, reserved names | `dataset_paths.py::confine` (`resolved_root in resolved.parents`) | `PureWindowsPath` compares case-insensitively, so ancestry is fine. Add tests for `C:foo`, `\\?\`, `NUL`, `file.mp4:stream`, 8.3 names, junctions (S9). |

### 7.7 Windows data hazards that do not exist on Linux

1. **MAX_PATH 260 without admin.** `%LOCALAPPDATA%\EduBotics\data\lerobot\` (~60 chars) + `<user>/<repo>` (up to ~136) + `videos/observation.images.scene/chunk-000/file-000.mp4` (55) can exceed 260 chars. `snapshot_download(local_dir=…)` adds `.cache\huggingface\download\…\*.metadata`. CPython's own exe is long-path aware but the registry switch needs admin (VERIFIED, Python docs). → a short root (e.g. `%LOCALAPPDATA%\EB\d`), a name-length guard at Start, and S9 for PyAV/pyarrow behaviour with `\\?\` paths.
2. **Roaming:** `Path.home()/.cache` roams (§4.1).
3. **Antivirus** scans every write/close of mp4 and parquet and the first import of ~10k files; exclusions need admin. UNVERIFIED-BENCH on the slow laptop.

---

## 8. Q6 — Cameras and local transport

### 8.1 Capture in its own process (recommended)

Evidence that GIL work in the recorder's process matters:
- the main ROS executor cost 2.23 ms of CPU per delivered message and saturated the GIL;
- a full GC paused every thread for 70–100 ms (hence `gc.freeze()`);
- the high-rate subscriptions were moved to a separate node and thread for exactly this reason.

(All in CLAUDE.md, Aufnahme round 5; `physical_ai_server.py::main` comments.)

Capture per frame means MSMF read + JPEG decode at half size + crop/resize/encode for the tower camera (1920×1080 at 30 fps) + per-frame Python glue (`scene_split.CaptureVerifier`), plus MJPEG fan-out to the web view. OpenCV releases the GIL during decode/encode; the glue, GC and asyncio serving do not.

A separate capture process also isolates a hung MSMF/driver call: the supervisor kills and restarts it, and the recorder sees a clean source-stop. **Keep the `>BIQ` wire** (`camera_bridge._HEADER`) over a named pipe (or a shared-memory ring later). The engine's sensor thread takes over today's `camera_ingest_node.py` role. Let the capture process also serve `/video/stream` (MJPEG) so camera bytes never transit the recorder's process. Bandwidth: 2 cameras × 30 fps × ~60 KB ≈ 3.6 MB/s, trivial for a pipe.

### 8.2 What the SPA expects (VERIFIED in `physical_ai_manager/src`)

- **rosbridge:** `utils/piMode.js::localRosbridgeUrl` → `` `${proto}://${window.location.host}/rosbridge` `` (host *includes* the port).
- **Video:** `videoStreamBase` → relative `/video`. `ImageGridCell.js`/`CameraFeedOverlay.jsx`/`RobotHero.jsx` request `/video/stream?quality=…&type=ros_compressed&default_transport=compressed&topic=<ros topic>&t=<ts>` and expect web_video_server's `multipart/x-mixed-replace` MJPEG.
- **Daten:** `/daten-api/v1/…`, GET/HEAD only, link tokens, Range support for `<video>`; served by the sidecar (`daten/http_server.py`), proxied by nginx today.
- **Static files:** `/version.json` (buildId), `/index.html` no-store, `/static/` immutable.
- **Loopback check:** `utils/pageHost.js::isLoopbackPage` compares `location.hostname` with `localhost`/`127.0.0.1`/`[::1]`, so `127.0.0.1:<port>` stays „local" (a custom-scheme origin would flip the SPA into remote-robot mode and break the token push).
- **The `:8769` bridge:** `utils/piMode.js` `RS_LOCAL_BASE = 'http://localhost:8769'` is hard-coded (OMX leader toggle) and needs a React change if kept.

### 8.3 Local web security

- **Threat.** Loopback TCP is machine-wide: any process of any Windows account on the PC (fast user switching, RDS) can connect.
  - Today: compose publishes `127.0.0.1:80`. nginx's Origin gate on `/rosbridge` deliberately admits an absent Origin (`health_checker.py` comment), and rosbridge authenticates nobody. **Any local process can drive the arm today** (VERIFIED by reading; the impact is bounded by who can log into the PC).
  - Host checks only stop DNS rebinding.
- **Options.**
  - (a) **Loopback + per-session secret.** The host mints 256 random bits per engine start and navigates to `http://127.0.0.1:P/__session?k=…`. The engine sets `HttpOnly; SameSite=Strict; Path=/` and redirects to `/?fresh=…`. Every HTTP request and the WebSocket upgrade must carry the cookie; same-origin `fetch`, `<img>`, `<video>` and WebSocket send it automatically. Keep the Host allowlist (rebinding). Portable to any web view, React unchanged.
  - (b) **WebView2 custom scheme / `WebResourceRequested`.** No listening port, fixed origin. But WebSocket is not interceptable (needs an IPC shim posing as WebSocket), MJPEG streaming and media Range through `WebResourceRequested` are unproven, `isLoopbackPage` would break, and it is Windows-specific (each OS has its own equivalent).
  - (c) Named pipes are not reachable from the page.
  - **Recommend (a) now, (b) as later hardening.**
- **Port.** Binding port 80 needs no admin via Winsock (the HTTP Server API is different) — UNVERIFIED-WINDOWS, but moot. The old product's WSL relay holds `127.0.0.1:80` during coexistence, IIS/http.sys may hold it, and two accounts cannot share it.
  - **Choose a stable per-user port** (base + hash(user SID), persisted, probe the next on conflict).
  - The origin must not change between launches, or localStorage/IndexedDB — the Blockly autosave and the MACHINE keys in `utils/sessionScope.js` — are lost.
  - Internal services (Daten sidecar, capture MJPEG) bind `127.0.0.1:0` and are reached only through the front server. The sidecar keeps its HMAC link tokens.

---

## 9. Q7 — Process model

### 9.1 Process tree

```
EduBotics.exe  (host; signed; one per user session)  ── Job "app" (KILL_ON_JOB_CLOSE, breakaway allowed for the sandbox)
 ├─ WebView2 (runtime processes)
 ├─ engine      python.exe -I -m edubotics.engine     (app core, rosbridge-v2 WS, HTTP front, board client)
 │    ├─ daten-http sidecar      (BELOW_NORMAL)
 │    ├─ download / edit / HF workers (BELOW_NORMAL / IDLE)
 ├─ capture     python.exe -I -m edubotics.capture    (MSMF, scene_split, MJPEG)
 └─ sandbox runs (via the host's broker)  ── standalone Job "run" (AppContainer, limits, KILL_ON_JOB_CLOSE)
```

### 9.2 Supervision and single instance

- Supervision: the host restarts the engine and capture with back-off. A restart re-mints the session secret and re-navigates the web view. Single instance per user session: keep the `Local\` mutex.
- Two Windows accounts at once fight over the COM port (exclusive open → the second gets `ERROR_ACCESS_DENIED` → German „Der Roboter wird gerade von einem anderen Windows-Konto verwendet") and over the cameras (MSMF is exclusive per device, as today's preview/bridge hand-off shows).

### 9.3 Recording finalize, logoff, sleep

| Event | Today | Windows design |
|---|---|---|
| „Beenden"/app close | SIGINT chain, 30 s finalize (`recording_shutdown`) | Host → engine control RPC „finalize and exit", wait ≤ 30 s, then terminate. Same `end_for_shutdown()`. |
| Logoff/shutdown | container stop | `WM_QUERYENDSESSION` → `ShutdownBlockReasonCreate("Aufnahme wird gespeichert…")` while the RPC runs. Windows allows ≈ 5 s before „Trotzdem herunterfahren" kills it. Measured finalize ≈ 1–2 s (CLAUDE.md: discard 1.04 s, finalize 8 ms, save in flight ≤ 0.83 s) → fits. Timing: UNVERIFIED-WINDOWS. |
| Sleep/hibernate | — | `PBT_APMSUSPEND` (about 2 s, no veto): stop motion, try to finalize. Modern Standby may freeze desktop processes → **the firmware link watchdog is the safety floor**. During sessions call `SetThreadExecutionState(ES_CONTINUOUS\|ES_SYSTEM_REQUIRED\|ES_DISPLAY_REQUIRED)` (prevents idle sleep, not lid close). UNVERIFIED-BENCH. |
| Engine crash / power loss | SIGKILL loses the session | Same: LeRobot's parquet writers are footer-less until closed (CLAUDE.md). The crash marker (`<dataset>.session.json`) is detected and reported; firmware stops the arm within `T_link`. |

### 9.4 Scheduling

- MMCSS „Capture" task for the serial reader and capture threads (no privilege).
- Power-throttling opt-out (§7.4) for the engine and capture.
- Never run the engine as an IDLE-class process.

### 9.5 HF token

In engine memory only: set by the page, cleared on logout or window end (§3.3 item 9). The ECDH envelope is unnecessary on loopback with the session secret, though keeping it costs nothing and keeps one code path with the Pi. No DPAPI file (B5).

### 9.6 Antivirus

- Use the embeddable runtime in folders, never PyInstaller onefile extraction to `%TEMP%`.
- Precompile `.pyc` at build time (no first-run writes).
- Sign every PE.
- IT guide: optional exclusion for the data folder.

---

## 10. Q8 — Migration off WSL and coexistence

### 10.1 Where the data lives

Compose volumes `ai_workspace:/workspace`, `huggingface_cache:/root/.cache/huggingface` (LeRobot datasets under `lerobot/`, HF models) and `edubotics_calib:/root/.cache/edubotics`. The compose project prefixes them (`docker_manager.EDUBOTICS_DATA_VOLUME_SUFFIXES`). Inside the distro: `/var/lib/docker/volumes/<project>_<name>/_data`.

### 10.2 Export by a standard user

Pipe `wsl -d EduBotics -u root -- tar -C /var/lib/docker/volumes/<p>_huggingface_cache/_data/lerobot -cf - <ns>/<dataset>` into the new data root (no admin, no Docker daemon needed). This works **only from the Windows account that owns the registration** (§3.4 b). Students whose distro sits in an admin's hive can neither see nor export it — and could not use the old product either.

| Failure | Effect | Answer |
|---|---|---|
| Distro in another account | invisible (`wsl -l`) | tell the user which account (finalize marker `user=`); that account exports |
| VM does not start (hypervisor/disk) | no export | only IT: `wsl --mount` / `--import-in-place` need admin |
| Large HF cache („tens of GB", compose comment) | slow, disk full | export **datasets + calibration only**; models re-download from the Hub |
| Unuploaded datasets | risk of loss | **upload-first** (Daten tab of the old product) is the primary path; tar is the fallback |

Sizes: worst case 3.6 MB/s of video for two cameras, planning rate 1 MB/s (CLAUDE.md disk-floor rationale) → 1 h of takes ≈ 1–3.6 GB. WSL pipe throughput: UNVERIFIED-WINDOWS (S14). Calibration: `arm_check.json` is KB-sized, and the arm check can simply be re-run.

**Frozen PCs:** datasets written to C: are lost at reboot unless uploaded — today too (the VHDX lives on C:). Keep auto-upload on by default, and offer the HKLM `DataRoot` policy for a thawed drive.

### 10.3 Coexistence of both products on one PC

| Resource | Old product | Native app |
|---|---|---|
| `127.0.0.1:80` | WSL relay → manager nginx | per-user stable port (not 80) |
| `127.0.0.1:5557` / `:8769` | ingest / OMX bridge | not used |
| `0.0.0.0:8444` | phone receiver | same port → only one may run; detect and refuse in German |
| Arm COM port | `usbipd attach` takes it from Windows | invisible while attached. Detect it in usbipd's state and say „Bitte die alte EduBotics-App schließen", or `usbipd detach` (no admin). |
| Cameras | MSMF in the old GUI | exclusive → one app at a time |
| WebView2 storage | origin `http://localhost` | different origin → separate storage (good) |

---

## 11. Risks, ranked

| # | Risk | Severity | Mitigation / proof |
|---|---|---|---|
| R0 | **Current product deletes another account's data** (§3.4) | **Critical, live** | Phase 0 now; check the field logs |
| R1 | Unsigned build blocked by AppLocker/SAC/SmartScreen/AV; signing blocked by the missing legal entity | High | Owner decision Q-W2; sign every PE |
| R2 | AppContainer sandbox (CPython/JVM) fails or leaks on real school PCs | High | S1–S5 before any engine work depends on it |
| R3 | Behaviour drift from numpy 2.2 / pupil post11 / OpenCV 4.13 / Python 3.13 in recorder, Daten, perception | High | Run the full server suite + `record_fsm_smoke` + `daten_smoke` + `code_e2e_smoke` on Windows CI |
| R4 | Data integrity from Windows file semantics in Daten/`hub_sync` | High | Portability layer + S10 + reader quiescing |
| R5 | Size and N copies; per-user install useless on frozen and mandatory-profile PCs | Med-High | Two channels; S8 |
| R6 | Timer/scheduling (Event.wait 15.6 ms, Win11 throttling, Modern Standby) | Medium | 3.13 + tickers + throttling opt-out + S11; firmware watchdog |
| R7 | Loopback exposure; port/origin instability wipes SPA storage | Medium | Session cookie + stable port; S12 |
| R8 | CH343P driver policy; DTR/RTS resets drop the arm | Medium | S13; firmware boots holding |
| R9 | MAX_PATH | Medium | Short root, name guard, S9 |
| R10 | GPL bits (x264 in the PyAV wheel, esptool) inside a proprietary installer | Medium (legal) | Licence review; OTA firmware |
| R11 | AV slowdowns and quarantine | Low-Med | Signing, no onefile, bench |
| R12 | Windows-on-ARM laptops (no native wheels) | Low | x64 emulation test |

---

## 12. Recommended Windows design and its proof obligations

1. **Host:** a signed native host (Tauri 2 recommended; alternatives in Q-W3) owns:
   - the window (Evergreen WebView2) and single-instance;
   - the app job (kill-on-close) and the supervisor;
   - the updater (Ed25519 + Authenticode check);
   - session, power and shutdown events;
   - the **sandbox broker** (AppContainer + run job + pipe DACLs).
2. **Engine:**
   - Runtime: python.org embeddable **CPython 3.13** + vendored wheels from a hash-locked curated `--no-deps` lock (numpy 2.2.6, opencv-headless < 4.14, pupil-apriltags 1.0.4.post11, torch 2.7.0 CPU, lerobot 0.5.1 without extras).
   - Server: the rosbridge-v2 WebSocket and the HTTP front on `127.0.0.1:<stable per-user port>` behind a per-session HttpOnly cookie and a Host allowlist.
   - Workers: Daten sidecar and workers keep their process structure, behind a **Windows portability layer** (locks, jobs, priorities, retries, per-user dirs).
   - Paths: data under `%LOCALAPPDATA%\EduBotics` (short paths, `HF_HOME`/`HF_LEROBOT_HOME`/`EDUBOTICS_CALIB_DIR`).
3. **Capture** in its own process (MSMF via OpenCV, `scene_split`, MJPEG); frames to the engine over a named pipe in `>BIQ` framing.
4. **Sandbox:** one AppContainer profile; a per-run dir; a standalone run job (memory, process count, CPU rate cap, kill-on-close, no breakaway); data pipe with SDDL (AppContainer SID + low label + first-instance); stop = `TerminateJobObject`. Java via a jlinked JDK 21 runtime with `jdk.compiler`. `code_rpc` budgets and validation unchanged.
5. **Packaging:** one signed build → a per-machine MSI (IT) and a per-user self-updating installer. HKLM policies `UpdateMode`, `DataRoot`, `AllowPhoneCamera`.
6. **Tokens:** HF token in memory only.

Proof obligations, each a gate before the dependent work starts:
- S0 for Phase 0.
- S1–S5 + S-J (sandbox) before Phase 6 is planned in detail.
- S7 + S8 + S16 (closure, size, MSMF in headless) before the engine skeleton.
- S10 (Daten on Windows) before Daten moves.
- S11 (timers) + S13 (serial) before teleop and recording.
- S12 (transport security).
- S14 (export).
- All of `record_fsm_smoke.py`, `daten_smoke.py` and `code_e2e_smoke.py` green on Windows CI, run as a standard user.

---

## 13. Decisions for the owner

| ID | Question | Options | Recommendation | Evidence |
|---|---|---|---|---|
| **Q-W1** | Install channels | (a) per-user only (brief); (b) per-machine MSI only; (c) both from one build | **(c)** | AppLocker defaults, frozen PCs, N copies (§4.1) |
| **Q-W2** | Signing identity | (a) found the legal entity first → Artifact Signing (EU org); (b) individual-validated CA certificate in your personal name now; (c) Microsoft Store MSIX under an individual account | **(a)** as the target. If the entity is months away, **(b)** for pilots, knowing your name appears in every dialog. (c) only as an extra channel (Store often blocked in schools; MSIX unknowns). | MS Learn eligibility (§4.3) |
| **Q-W3** | Window host | (a) keep pywebview; (b) Tauri 2 (Rust); (c) minimal C++ host; (d) Electron | **(b)**, if you accept a Rust component; else **(a)** with the engine as a child | §4.5 |
| **Q-W4** | Local transport security | (a) loopback + session cookie + stable port; (b) custom scheme + IPC shim | **(a)** now | §8.3 |
| **Q-W5** | Student-code sandbox | (a) AppContainer + Job (native); (b) WASM (Pyodide/Java-in-browser); (c) native on Windows, WASM on iPad/Chromebook | **(a)** on Windows, gated by S1–S5; (c) as the cross-platform roadmap | §6 |
| **Q-W6** | Java | (a) jlinked JDK 21 with `jdk.compiler` (real javac); (b) drop Java outside Windows | **(a)** | B2 |
| **Q-W7** | Updates of per-machine installs | (a) IT-managed, the app tolerates N−1; (b) a per-machine updater service (admin once, more attack surface) | **(a)** | Frozen PCs; today's forced modal loops there |
| **Q-W8** | HF token at rest | (a) memory only; (b) DPAPI file | **(a)** | B5 |
| **Q-W9** | Phone camera | (a) keep (IT MSI adds the firewall rule); (b) drop | your call — recording does not use it today (`phone_camera.py` scope note) | §3.1 |
| **Q-W10** | Python | (a) 3.13; (b) 3.12 + perf_counter everywhere | **(a)** | §7.1, §7.4 |
| **Q-W11** | Phase 0 scope | (a) the brief's per-SID folder + delete only our own; (b) **(a) plus never delete a disk, refuse a foreign one, and import un-elevated per user into `%LOCALAPPDATA%`** | **(b)** — it also makes standard-user students work | §3.4 |
| **Q-W12** | Supported Windows | Win 11 x64 + Win 10 22H2 x64; ARM64 only under x64 emulation | as stated | §4.4 |

---

## 14. Portability of each recommended mechanism

| Mechanism | Windows-only? | Equivalent elsewhere |
|---|---|---|
| AppContainer, Job Objects, named-pipe SDDL, MMCSS, power throttling, `WM_QUERYENDSESSION` | Windows-only | macOS: App Sandbox / `sandbox-exec`, `posix_spawn` + rlimits; Linux: user namespaces + seccomp (bubblewrap); iPadOS: no child processes → WASM |
| WebView2, MSI/MSIX, Authenticode, AppLocker/SAC concerns | Windows-only | WKWebView / WebKitGTK; DMG/notarisation; AppImage/deb |
| Tauri host, custom protocols, signed updater | portable (Windows, macOS, Linux, iOS, Android) | — |
| Loopback HTTP/WS + session cookie | portable (any web view; also a remote browser later) | — |
| Embedded CPython 3.13 engine + curated lock | Windows/macOS/Linux | iPadOS: not as separate processes (embedding is possible, but no `subprocess` and no helper processes) |
| pyserial over CDC-ACM | portable (macOS `/dev/cu.*`, Linux `ttyACM`) | iPad/Chromebook: BLE or Web Serial (Chromium only) |
| OpenCV MSMF capture | backend-specific | AVFoundation / V4L2 backends of the same OpenCV |
| WASM sandbox | portable incl. iPad/Chromebook | — |

---

## 15. Consolidated spike list (Windows CI as a **standard user**, plus one locked-down school PC)

| ID | Proves | Pass criterion |
|---|---|---|
| S0 | WSL refuses `--import` over another account's `ext4.vhdx`; our script then deletes it | Error contains `RegisterDistro/ERROR_FILE_EXISTS`; file gone after the old script, kept after the fix |
| S1 | CPython 3.13 embeddable in an AppContainer (§6.6) | Imports ok; network/loopback/user-file/process access refused; pipe ok |
| S2 | JVM + javac in an AppContainer | javac messages real; JIT on; no hsperfdata/temp failures |
| S3 | Job limits, nesting, kill-on-close | Each limit trips as configured; children die with the broker |
| S4 | `code_rpc` over pipes | Existing tests green through the adapter |
| S5 | Escape matrix (§6.6) | Every escape attempt refused |
| S-J | jlinked runtime size | Size recorded; SmokeMain passes |
| S7 | Curated `--no-deps` lock imports the recorder/Daten closure; PyAV has libx264; `record_fsm_smoke.py` incl. `frame_drop_rerecord` | Green on windows-latest |
| S8 | Installed size; strip `torch/lib/*.lib`, `torch/include` | `import torch` + the recorder still work; size recorded |
| S9 | Long paths and path edge cases (`\\?\`, `NUL`, ADS, 8.3, junctions) through PyAV/pyarrow/`confine` | Refused or handled, no data loss |
| S10 | `daten_smoke.py` on Windows, with a concurrent reader holding an mp4 and Defender on | No corrupted swap; leftovers logged and swept |
| S11 | Timer jitter (`Event.wait` vs `time.sleep` vs waitable timer; minimised window; throttling opt-out) | 30 Hz ticks within ±2 ms p99 with the chosen primitive |
| S12 | Session cookie; a second Windows account (FUS) gets 401 | 401, no data |
| S13 | CH343P on a clean Windows 10 and 11, offline, online, under a device-install policy; (b) port open does not reset the ESP32; (c) latency histogram | COM appears without admin; no reset; p99 < 5 ms |
| S14 | WSL tar export throughput and failure messages | Rate recorded; German message per failure row |
| S16 | `opencv-python-headless` has MSMF on Windows | `CAP_MSMF` listed and today's capture path works |

---

## Appendix A — Commands run (outputs abbreviated)

**A1.** `curl https://pypi.org/pypi/lerobot/0.5.1/json` → `requires_python: >=3.12`; `numpy<2.3.0,>=2.0.0`; `torch<2.11.0,>=2.7`; `torchcodec … ; sys_platform != "win32" …`; files: `lerobot-0.5.1-py3-none-any.whl` 1 128 200 B, sdist 877 920 B.

**A2.** `uv pip compile --python-platform x86_64-pc-windows-msvc --python-version {3.12,3.13} --only-binary :all: …` + `pupil-apriltags==1.0.4` → „Because pupil-apriltags==1.0.4 has no usable wheels … your requirements are unsatisfiable." With `==1.0.4.post11` + `--no-binary proxy-tools` → `lock-cp312.txt`, `lock-cp313.txt`, 97 packages, „cp312 and cp313 locks identical in versions". Excerpt: `numpy==2.2.6 opencv-python-headless==4.13.0.92 torch==2.7.0 torchvision==0.22.0 av==15.1.0 pyarrow==25.0.0 huggingface-hub==1.23.0 pupil-apriltags==1.0.4.post11 rerun-sdk==0.26.2 wandb==0.24.2 cmake==4.1.3 imageio-ffmpeg==0.8.1 …` (no torchcodec).

**A3.** PyPI pupil-apriltags releases: `1.0.4 win_amd64: ['cp36','cp37']`; `1.0.4.post11 (2025-04-22) win_amd64: ['cp310','cp311','cp312','cp313','cp36','cp37','cp38','cp39']`.

**A4.** Compressed sizes per pinned file (PyPI JSON): `TOTAL compressed wheels: 564.9 MB across 97 packages`; largest torch 212.5, rerun-sdk 84.3, opencv-headless 40.1, imageio-ffmpeg 38.9, cmake 37.5, av 31.3, pyarrow 27.9, wandb 22.3, numpy 12.6, pandas 10.7.

**A5.** numpy 1.26.4 `win_amd64`: cp39–cp312 only (no cp313).

**A6.** `win_arm64`: none for torch 2.7.0, torchvision 0.22.0, av 15.1.0, pyarrow 25.0.0, opencv-headless 4.13.0.92, numpy 2.2.6, pupil-apriltags post11, cryptography 49.0.0; present for aiohttp, pandas.

**A7.** `pdftotext Bus_Servo_Driver_HAT_(A)_Sch.pdf - | grep …` → `2 CH343P, 1 ESP-32S, 1 ESP-WROOM-32, 2 MP9447, 1 SN74LVC1G08DBVR, 1 SN74LVC1G125DBV, 1 SN74LVC1G126DBV, 1 SP3485EN`.

**A8.** AST scan of `physical_ai_tools/physical_ai_server/{physical_ai_server,launch}` for POSIX calls, mapped to enclosing symbols (§7.6 table). Third-party import scan: 111 `.py` files; rclpy importers: `physical_ai_server.py`, `daten/node_service.py`, `timer/timer_manager.py`, `utils/parameter_utils.py`, `safety/collision_monitor.py`, `workflow/studio_camera.py`, `communication/communicator.py`, `communication/multi_subscriber.py`.

**A9.** lerobot v0.5.1 source (GitHub raw and sdist, sha256 `4a05a9d1…6fd3` matching PyPI): `datasets/utils.py: import torch`; `datasets/lerobot_dataset.py: import torch, torch.utils`; `datasets/video_utils.py: import torch, torchvision, av`. Static closure (`scripts/import_closure.py`): 114 lerobot modules; rerun/wandb/cmake/imageio_ffmpeg absent.

**A10.** WSL master source (`microsoft/WSL`): `LxssUserSession.cpp` (`_ValidateDistributionNameAndPathNotInUse(lxssKey…)`; the import's `cleanup` scope_exit with `deleteFlags = LXSS_DELETE_DISTRO_FLAGS_VHD` set after `CreateVhd`); `WslCoreFilesystem.cpp::CreateVhd` (`CreateVirtualDisk`, DACL: user + BUILTIN\Administrators FILE_ALL_ACCESS).

**A11.** `docs.python.org/3.13/whatsnew/3.13.html`: „On Windows, monotonic() now uses the QueryPerformanceCounter() clock for a resolution of 1 microsecond … instead of … 15.6 milliseconds." `library/time.html`: „On Windows 10 and newer the implementation uses a high-resolution timer which provides resolution of 100 nanoseconds."

**A12.** Adoptium API: `OpenJDK21U-jdk_x64_windows_hotspot_21.0.13.1_2.zip 205.4 MB`, `…jre… 49.1 MB`. python.org HEAD: `python-3.13.9-embed-amd64.zip` content-length 10 926 675.

**A13.** CPython 3.13 `Lib/multiprocessing/connection.py`: `CreateNamedPipe(… _winapi.NULL)`. pyserial 3.5 `serialwin32.py`: `FILE_FLAG_OVERLAPPED`, DCB DTR/RTS from `_dtr_state`/`_rts_state`.

**A14.** Repo greps: no `signtool`/Trusted Signing in `.github`, `installer`, `gui` (unsigned build confirmed); no contrib OpenCV module in use; SPA `localRosbridgeUrl` uses `window.location.host`, `RS_LOCAL_BASE = 'http://localhost:8769'`.

## Sources

- AppLocker default executable rules — https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/applocker/executable-rules-in-applocker
- Code signing options (Artifact Signing eligibility; EV change 2024; Store signing) — https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/code-signing-options
- Smart App Control (all code modules, ISG, trusted roots) — https://textslashplain.com/2026/04/28/smart-app-control/ ; https://support.microsoft.com/en-us/topic/what-is-smart-app-control-285ea03d-fa88-4d56-882e-6698afdb7003
- WebView2 distribution (Win11 included; Win10 vast majority; per-user installs; Fixed Version > 250 MB, AppContainer ACLs) — https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution
- CreateAppContainerProfile — https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-createappcontainerprofile
- AppContainer isolation — https://learn.microsoft.com/en-us/windows/win32/secauthz/appcontainer-isolation
- Nested jobs — https://learn.microsoft.com/en-us/windows/win32/procthread/nested-jobs
- JOBOBJECT_BASIC_LIMIT_INFORMATION — https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_limit_information
- timeBeginPeriod (Win10 2004 / Win11 behaviour) — https://learn.microsoft.com/en-us/windows/win32/api/timeapi/nf-timeapi-timebeginperiod
- MMCSS — https://learn.microsoft.com/en-us/windows/win32/procthread/multimedia-class-scheduler-service
- Usbser.sys compatible IDs — https://learn.microsoft.com/en-us/windows-hardware/drivers/usbcon/usb-driver-installation-based-on-compatible-ids
- Espressif USB-Serial-JTAG on Windows — https://docs.espressif.com/projects/esp-iot-solution/en/latest/usb/usb_overview/usb_serial_jtag.html ; https://docs.espressif.com/projects/esp-techpedia/en/latest/esp-friends/get-started/try-firmware/try-firmware-usb-driver.html
- CPython AF_UNIX on Windows — https://github.com/python/cpython/issues/77589 ; https://github.com/python/cpython/pull/137420
- Crashpad pipe security descriptor — https://chromium.googlesource.com/crashpad/crashpad/+/21a20ef8adf3949de8dd65758a16f83aab344b3c/util/win/registration_protocol_win.cc
- Python 3.13 What's New / time / Windows usage — https://docs.python.org/3.13/whatsnew/3.13.html ; https://docs.python.org/3.13/library/time.html ; https://docs.python.org/3.13/using/windows.html
- pyav-ffmpeg build contents — https://github.com/PyAV-Org/pyav-ffmpeg
- WSL source — https://github.com/microsoft/WSL (src/windows/service/exe/LxssUserSession.cpp; src/windows/common/WslCoreFilesystem.cpp)
- Docker forum ERROR_FILE_EXISTS report — https://forums.docker.com/t/wsl-error-on-first-installation/150111
- Waveshare Bus Servo Driver HAT (A) wiki and schematic — https://www.waveshare.com/wiki/Bus_Servo_Driver_HAT_(A)
- Velopack Windows packaging — https://docs.velopack.io/packaging/operating-systems/windows
- Tauri 2 updater (mandatory signatures) — https://v2.tauri.app/plugin/updater/
- Roaming profile exclusions (archived) — https://learn.microsoft.com/en-us/previous-versions/ms812015(v=msdn.10)
- PyTorch Windows CPU install size data point (2021) — https://discuss.pytorch.org/t/large-cpu-file-size/112709
- Intune and Store-blocked deployments (mixed evidence) — https://learn.microsoft.com/mem/intune/apps/store-apps-microsoft

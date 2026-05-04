# 14 — Windows tkinter GUI (`EduBotics.exe`)

> **Layer:** Windows desktop UI for hardware setup + container lifecycle
> **Location:** `Testre/robotis_ai_setup/gui/`
> **Owner:** Our code
> **Read this before:** editing the wizard flow, USB scanning, Docker pulls, WebView2 integration, update gate.

---

## 1. Files

```
gui/
├── main.py                     # entry: dispatches on --webview sentinel
├── build.spec                  # PyInstaller spec
├── requirements.txt
└── app/
    ├── constants.py            # APP_VERSION, distro name, paths, env-var overrides
    ├── gui_app.py              # main tkinter wizard (1063 lines)
    ├── docker_manager.py       # docker via wsl wrapper (630 lines)
    ├── device_manager.py       # USB + arms + cameras (276 lines)
    ├── config_generator.py     # .env writer with atomic + ROS_DOMAIN_ID
    ├── wsl_bridge.py           # wsl exec wrapper (132 lines)
    ├── health_checker.py       # web UI + rosbridge + video server polling (76 lines)
    ├── update_checker.py       # GUI auto-update (118 lines)
    └── webview_window.py       # pywebview subprocess manager (204 lines)
```

---

## 2. Entry + dispatch (`main.py`)

```python
WEBVIEW_FLAG = "--webview"

if WEBVIEW_FLAG in sys.argv:
    _dispatch_webview()    # calls webview_window.run_in_process()
else:
    gui_app.run()          # tkinter wizard
```

`_dispatch_webview()` parses `--url`, `--icon`, `--debug` argv, then calls `webview_window.run_in_process(url=..., icon=..., debug=...)`. Blocks on `webview.start()`.

This dispatch lets us re-invoke the same `EduBotics.exe` as a subprocess in webview mode (so pywebview owns its main thread instead of fighting tkinter for it).

---

## 3. constants.py

Single source of truth for paths + versions:

| Constant | Source | Notes |
|---|---|---|
| `APP_VERSION` | `_read_version_file()` reads repo `VERSION` (fallback "2.2.2") | |
| `IMAGE_TAG` | `_read_image_tag_from_versions_env()` reads `docker/versions.env` (fallback "latest") | |
| `INSTALL_DIR` | env override `EDUBOTICS_INSTALL_DIR`, else walk-up to find `docker/docker-compose.yml`, else `C:\Program Files\EduBotics` | |
| `ENV_FILE` | env override `EDUBOTICS_ENV_FILE`, else `%LOCALAPPDATA%\EduBotics\.env` | |
| `WSL_DISTRO_NAME` | env override `EDUBOTICS_WSL_DISTRO`, else `EduBotics` | |
| `REGISTRY` | env override `EDUBOTICS_REGISTRY`, else `nettername` | |
| `ALL_IMAGES` | `[physical-ai-manager, physical-ai-server, open-manipulator]` @ `${REGISTRY}:${IMAGE_TAG}` | |
| `ROS_DOMAIN_ID` | hardcoded 30 (default in compose; per-machine override in `config_generator`) | |
| `DOCKER_STARTUP_TIMEOUT` | 120 s | wait_for_docker |
| `WEB_UI_POLL_TIMEOUT` | 120 s | wait_for_web_ui |
| `WEB_UI_POLL_INTERVAL` | 2 s | |
| `_to_wsl_path(p)` | `C:\foo\bar` → `/mnt/c/foo/bar` | uses `[a-zA-Z]:` regex match |

---

## 4. gui_app.py — wizard state machine

### UI flow

1. **Mode select** — cloud-only checkbox. Disables hardware frames, switches startup from full-stack compose to manager-only.
2. **Step A — Leader arm scan** — daemon thread: USB scan → identify_arm → role assignment.
3. **Step B — Follower arm** — same scan; the OTHER role gets the follower.
4. **Step C — Cameras** — daemon thread: list `/dev/video*` via v4l2-ctl. 1 cam → auto "gripper". 2 cams → role-assignment dropdowns (gripper/scene), auto-syncing.
5. **Start/Stop/Open Browser buttons** — gated by `_update_start_button()`.

### State flags

| Flag | Type | Set by | Read by |
|---|---|---|---|
| `self.running` | bool | main + daemon | `_update_start_button()` (via `root.after(0, ...)`) |
| `self.hardware` | HardwareConfig dataclass | scan threads | `_update_start_button()`, `_do_start()` |
| `self._scanning` | bool | both threads | scan-button gating |
| `self._prerequisites_done` | bool | `_run_prerequisite_checks()` daemon | gating |
| `self.gpu_available` | bool | `_run_prerequisite_checks()` (calls `nvidia-smi`) | `_do_start()` (compose -f docker-compose.gpu.yml) |
| `self.cloud_only` | tk.BooleanVar | UI checkbox | mode dispatch |

**No explicit locks.** GIL + Tk's `after()` queue + simple bool assignments = correct-by-construction in practice. UI updates from daemon threads must go through `self.root.after(0, callable)`.

### Daemon threads

1. **`_check_prerequisites()`** — startup. Polls Railway `/version`. If newer, blocking modal forces update before further startup.
2. **`_run_prerequisite_checks()`** — checks distro registered → starts WSL → waits for dockerd → checks images → pulls if missing → detects GPU → sets `_prerequisites_done=True`.
3. **`_show_update_dialog()` modal thread** — non-closable (grab_set + `WM_DELETE_WINDOW` no-op). Skip button disabled until 3 retry failures. Download → launch installer → `sys.exit(0)`.
4. **`_scan_arms()` / `_scan_cameras()`** — call `device_manager` methods, update UI via `after(0, ...)`.
5. **`_do_start()`** — the big one. See §5.
6. **`_do_stop()`** — `docker compose down` (or stop manager only if cloud-only).
7. **`_run_elevated()`** — wraps `_elevate_and_wait` for `finalize_install.ps1`.

---

## 5. `_start_environment()` line-by-line

The most important function in the GUI.

1. **Read mode** — `cloud_only = self.cloud_only.get()`.
2. **Prereq check** — full mode: bail if `not self.hardware.is_complete`.
3. **Disable Start button**, set `self.running = True`, spawn `_do_start()` daemon.

Inside `_do_start()` (try/finally wrapping):

4. **Start progress spinner** (via after).
5. **Hardware validation** (skip if cloud-only):
   a. `wsl_bridge.list_serial_devices()` → list of `/dev/serial/by-id/...`
   b. Check leader/follower paths present. If missing:
      - Log "USB-Geräte werden erneut verbunden..."
      - `device_manager.attach_all_robotis_devices()` (synchronous usbipd attach)
      - Poll `list_serial_devices()` 5x with 1 s sleep
      - Final check: still missing → log per-arm error, return (startup_ok stays False)
   c. Catch any exception: log warning, **continue anyway** (non-fatal).
6. **.env generation**:
   - Status: "Konfiguration wird erstellt..."
   - `config_generator.generate_env_file(hardware, ENV_FILE)` or `generate_cloud_only_env(ENV_FILE)`
   - Log path + content lines
7. **Docker compose up**:
   - `use_gpu = self.gpu_available and not is_cloud_only`
   - cloud_only: `docker_manager.start_cloud_only(log)`
   - else: `docker_manager.start_containers(gpu=use_gpu, log)`
   - On failure: log error, return
8. **Web UI poll** — `health_checker.wait_for_web_ui(callback)` — non-fatal warning on timeout.
9. **Full health check** — log each service status.
10. **Open WebView2** — `_open_webview()` (non-blocking, spawns subprocess).
11. **Set status "Aktiv — Umgebung läuft"**, enable Stop + Open Browser buttons, `startup_ok = True`.
12. **Finally**: stop progress; if not startup_ok, reset `self.running=False` + `_update_start_button()`.

Key insight: **the finally block is the only place `self.running` gets reset on failure**. Multiple early returns inside the try would skip this in older code; now the try/finally ensures cleanup.

---

## 6. UAC elevation (`_elevate_and_wait`)

`gui_app.py:20-88`. Uses Win32 ShellExecuteExW directly (not `Start-Process`) to get a real process handle.

```python
SEE_MASK_NOCLOSEPROCESS = 0x40    # keep handle open after exec
SEE_MASK_NOASYNC        = 0x100   # synchronous-ish
ERROR_CANCELLED         = 1223    # UAC denied
```

Flow:
1. Build `SHELLEXECUTEINFOW` struct, fMask = NOCLOSEPROCESS|NOASYNC, lpVerb="runas", lpFile=exe, lpParameters=args.
2. `shell32.ShellExecuteExW(byref(info))` → on failure check `GetLastError() == ERROR_CANCELLED` (UAC denied) → return appropriate error.
3. `WaitForSingleObject(hProcess, INFINITE)` — blocks daemon thread until elevated process exits.
4. `GetExitCodeProcess` + `CloseHandle`.
5. Returns `(exit_code, was_cancelled, error_msg)`.

Used to invoke `finalize_install.ps1` post-reboot (writes a marker file proving it launched + transcript log).

---

## 7. WebView2 subprocess (`webview_window.py`)

### Why subprocess

pywebview 6 requires `webview.start()` on the main thread — but tkinter owns it. So we spawn a subprocess of the same `EduBotics.exe` with `--webview ...`, where the dispatch in `main.py` runs `webview.start()` as the subprocess's main thread.

### Process model

- Parent: tkinter GUI
- Child: spawned via `subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW)`

### `_build_launch_cmd()` (lines 65-83)

- Frozen exe (PyInstaller bundle): `[exe, '--webview', '--url', url, '--icon', icon, '--debug', '0']`
- Source mode: `[sys.executable, 'main.py', '--webview', ...]`

### Watchdog thread

`_watch_subprocess()` (lines 138-147): waits for `proc.wait()`, sets `_runtime_missing` event on non-zero exit (typically WebView2 not installed) UNLESS `_deliberate_stop` was set.

### Fallback (`gui_app.py`)

Parent polls `webview_window.runtime_missing()` after 2 s delay. If true: show messagebox "WebView2 nicht verfügbar", `webbrowser.open(url)`.

### URL construction

- Cloud-only: `http://localhost/?cloud=1` (skips rosbridge gate in React)
- Full mode: `http://localhost/`

### Debouncing

800 ms guard to prevent rapid double-clicks spawning multiple WebView2 subprocesses (each ~150 MB).

---

## 8. DockerManager (`docker_manager.py`)

### `_docker_cmd()` — wraps every Docker call (lines 42-53)

```python
def _docker_cmd(self, *args, cwd_wsl=None):
    cmd = ["wsl", "-d", WSL_DISTRO_NAME]
    if cwd_wsl:
        cmd += ["--cd", cwd_wsl]
    cmd += ["--", "docker", *args]
    return cmd
```

**No** Docker Desktop dependency. Every `docker pull / run / compose / inspect` goes through `wsl -d EduBotics --`.

### Public API

| Method | Purpose |
|---|---|
| `is_distro_registered()` | parse `wsl --list --quiet` (handles UTF-16LE NUL bytes) |
| `is_docker_running()` | `docker info` returncode == 0 |
| `start_edubotics_distro()` | `wsl -d EduBotics -- echo ready` (wakes the VM) |
| `wait_for_docker(timeout, callback)` | poll `docker info`; at 15 s+ stall, force-invoke `start-dockerd.sh` |
| `has_gpu()` | host `nvidia-smi` returncode (NOT inside distro — driver is shared) |
| `images_exist()` | parallel `docker image inspect` |
| `check_for_updates()` | pull all, detect ID change |
| `pull_images(callback, log)` | wraps `_pull_one_image()` with stall watchdog |
| `start_containers(gpu, log)` | `docker compose up -d --force-recreate` (with gpu overlay file if gpu=True) |
| `start_cloud_only(log)` | `docker compose up -d --no-deps physical_ai_manager` |
| `stop_cloud_only(log)` / `stop_containers(gpu)` | compose down or rm -f manager only |
| `get_container_status()` | `docker inspect -f {{.State.Status}}` |
| `manager_container_running()` / `all_containers_running()` | status checks |
| `get_container_logs(name, lines)` | `docker logs --tail N` |

### Pull stall watchdog (lines 296-432) — the secret sauce

**Problem:** Docker's `\r`-animated progress bars look idle to line-based readers. Pulls hang silently.

**Solution:** monitor BOTH stdout-line-rate AND `/var/lib/docker/overlay2` disk growth.

- Reader thread loops `proc.stdout`, puts each line on a queue.
- Main loop polls queue with `timeout=20s`:
  - Got a line → log it, reset stall timer
  - Timeout → check `du -sb /var/lib/docker/overlay2`. Grew &gt; 10 MB? Reset timer (extract is happening).
  - Both stdout + disk silent for 600 s → stalled. `pkill -KILL dockerd`. Exp backoff (`min(4 * 2^attempt, 30)`).
- Max 4 retries. On retry ≥2: full `_reset_dockerd()` (kill all, clear sockets, restart via `start-dockerd.sh`, poll 15 times 1s).

**This is the main knob for poor-network classrooms.** Tweak `stall_timeout` (default 600s first pull, 120s updates) and `disk_delta_threshold` (default 10 MB / 20 s).

---

## 9. DeviceManager (`device_manager.py`)

### Dataclasses

- `USBDevice(busid, vid_pid, description, state)`
- `ArmDevice(busid, serial_path, role, description)` — role: leader/follower/unknown
- `CameraDevice(path, name, role)`
- `HardwareConfig(leader, follower, cameras)` — `is_complete` requires both arms

### Workflow: `scan_and_identify_arms(image_omx)` (lines 213-269)

1. **Attach USB**: `attach_all_robotis_devices()` filters `list_usb_devices()` by VID `2F5D`, attaches each via `usbipd attach --wsl --distribution EduBotics --busid <busid>` (3 retries).
2. **Poll serial paths** (10x, 1 s sleep): `find_serial_paths_for_robotis()` → `list_serial_devices()` filtered by ROBOTIS/OPENRB.
3. **Start scanner container**: `docker run -d --name robotis_arm_scanner --privileged -v /dev:/dev --entrypoint sleep <image> 120`.
4. **Identify each path**: `docker exec robotis_arm_scanner python3 /usr/local/bin/identify_arm.py <path>` → `"leader"` / `"follower"` / `"unknown"` / `"error:..."`. Retry once on error/unknown after 2 s.
5. **Match busid** (fuzzy: any word in description matches path substring).
6. **Stop container**: `docker rm -f robotis_arm_scanner`.

### Camera scanning (`scan_cameras()`)

`wsl_bridge.list_video_devices()` runs `v4l2-ctl --info` for each `/dev/video*` inside the distro. Returns CameraDevice list with friendly names (e.g., "Logitech C920").

---

## 10. WslBridge (`wsl_bridge.py`)

### `run(cmd, timeout=30, check=True)`

Executes `wsl -d EduBotics -- bash -c <cmd>` with text capture, optional `check=True` → raises `WSLError` on non-zero.

### `list_serial_devices()`

`ls /dev/serial/by-id/ 2>/dev/null` → list of full paths.

### `list_video_devices()`

For each `/dev/video*` runs `v4l2-ctl --device=$d --info`, filters lines for "Video Capture" + "Card type", yields `{"path": ..., "name": ...}`.

All commands target `WSL_DISTRO_NAME` (no fallback to default distro).

---

## 11. ConfigGenerator (`config_generator.py`)

### `generate_env_file(hardware, env_file_path)`

Writes a fully-quoted `.env` file. Atomic (`.tmp` + `os.replace()`) — power-loss safe.

```env
FOLLOWER_PORT="/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_..."
LEADER_PORT="/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_..."
CAMERA_DEVICE_1="/dev/video0"
CAMERA_NAME_1="gripper"
CAMERA_DEVICE_2="/dev/video2"
CAMERA_NAME_2="scene"
ROS_DOMAIN_ID=42
REGISTRY="nettername"
```

### `generate_cloud_only_env(env_file_path)`

Empty placeholders for ports/cameras (compose doesn't error on missing vars).

### `_resolve_ros_domain_id()`

1. If env `EDUBOTICS_ROS_DOMAIN` set: clip to [0, 232], return.
2. Else: hash `uuid.getnode()` (48-bit MAC) via SHA256, take first 2 bytes mod 233.
3. Fallback: 30.

Mitigates same-LAN ROS topic cross-talk ([§2.3 of known-issues](21-known-issues.md)).

### `_quote(v)`

Escapes backslashes + double-quotes, wraps in quotes. Survives docker-compose env parsing for paths with spaces.

---

## 12. HealthChecker (`health_checker.py`)

| Function | Endpoint | Method | Success |
|---|---|---|---|
| `check_web_ui()` | `http://localhost:80/` | GET | HTTP 200 |
| `check_rosbridge()` | `localhost:9090` | TCP socket | connect() succeeds |
| `check_video_server()` | `http://localhost:8080/` | GET | HTTP 200 |
| `wait_for_web_ui(callback)` | (web_ui) | polls every 2 s up to 120 s | callback(elapsed, timeout) |
| `full_health_check()` | all 3 | dict[service→bool] | |

3 s timeout per check.

---

## 13. UpdateChecker (`update_checker.py`)

### `check_for_update(current, api_url)`

GET `{api_url}/version` → `{"version": "x.y.z", "download_url": ...}`. Parses versions via `_parse_version()` → tuple. Returns dict if remote &gt; current.

### `download_installer(url, dest_dir, callback)`

Streams to `%TEMP%/EduBotics_Setup.exe` with 64 KB chunks. Callback `(downloaded, total)`. Cleans up partial on error.

### `cleanup_stale_installers(max_age_hours=24)`

Globs `%TEMP%/EduBotics_Setup*.exe`, deletes if `mtime > 24h` ago.

### Blocking modal flow

In `gui_app.py:_show_update_dialog()`:
- Modal: grab_set + `WM_DELETE_WINDOW` no-op
- Download in thread, progress via `after(0, ...)`
- Skip enabled after 3 failures
- On success: `os.startfile(installer)` + `sys.exit(0)`

---

## 14. Threading hazards

(Minor — none currently cause issues, but worth noting if you refactor.)

| Field | Writers | Readers | Risk |
|---|---|---|---|
| `self.running` | main (line 855) + daemon (line 959 finally) | `_update_start_button()` via after | Both atomic bool assignments; low risk |
| `self.hardware` | scan threads (line 704/714) + main (line 795) | `_update_start_button()`, `_do_start()` | Mutable dataclass; assignment is atomic but partial mutations could be observed — currently not an issue |
| `self._scanning` | scan starts (main) + scan ends (daemon) | scan starts | low risk |
| `self._prerequisites_done` | daemon (line 448) | `_update_start_button()` via after | low risk |

Tk's `after()` queue serializes UI updates, eliminating most reader-writer hazards in practice.

---

## 15. Subprocess invocations (full list, all CREATE_NO_WINDOW except UAC + WebView2)

| Command | Module |
|---|---|
| `wsl --list --quiet` | docker_manager |
| `wsl -d EduBotics -- docker info` | docker_manager |
| `wsl -d EduBotics -- echo ready` | docker_manager |
| `wsl -d EduBotics -- /usr/local/bin/start-dockerd.sh` | docker_manager (recovery) |
| `nvidia-smi` (host) | docker_manager |
| `wsl -d EduBotics -- docker image inspect <img>` | docker_manager |
| `wsl -d EduBotics -- docker pull <img>` (Popen) | docker_manager |
| `wsl -d EduBotics -- bash -c <reset script>` | docker_manager (`_reset_dockerd`) |
| `wsl -d EduBotics -u root -- du -sb /var/lib/docker/overlay2` | docker_manager (stall watchdog) |
| `wsl -d EduBotics -- docker compose [args]` | docker_manager (start/stop) |
| `wsl -d EduBotics -- docker inspect -f {{.State.Status}} <c>` | docker_manager |
| `usbipd list / attach / detach` | device_manager |
| `wsl -d EduBotics -- docker run/exec/rm` (scanner container) | device_manager |
| `wsl -d EduBotics -- bash -c "ls /dev/serial/by-id/ ..."` | wsl_bridge |
| `wsl -d EduBotics -- bash -c "for d in /dev/video* ..."` | wsl_bridge |
| `powershell.exe finalize_install.ps1 ...` (UAC, no CREATE_NO_WINDOW) | gui_app |
| `EduBotics.exe --webview --url ...` (Popen, CREATE_NO_WINDOW) | webview_window |

---

## 16. Language

- **German**: every tkinter label, status, error, log message that the student sees ("Arme scannen", "Fehlende Hardware", "Web-Oberfläche wird geöffnet").
- **English**: code, internal log prefixes for the maintainer.

Mixed-language strings appear in the log pane (English `[INFO]` prefix + German message).

---

## 17. PyInstaller build (`build.spec`)

Collects:
- `webview.platforms.edgechromium`
- `clr_loader.netfx` (pythonnet for WebView2 binding)
- assets: `icon.ico`

`console=False` so no console window flashes. Single-file mode disabled (multi-file dist for faster startup).

```bash
cd robotis_ai_setup/gui
pyinstaller build.spec
# outputs gui/dist/EduBotics/
```

---

## 18. Local dev

```bash
cd robotis_ai_setup/gui
pip install -r requirements.txt
python main.py    # tkinter wizard launches
```

Override defaults via env vars:
```bash
EDUBOTICS_WSL_DISTRO=EduBotics-dev python main.py    # use a different distro
EDUBOTICS_ENV_FILE=/tmp/test.env python main.py      # write to a different .env
```

To test WebView2 subprocess directly:
```bash
python main.py --webview --url http://localhost --icon assets/icon.ico --debug 1
```

---

## 19. Cross-references

- Constants + paths: [`04-env-vars.md`](04-env-vars.md) §8
- WSL distro lifecycle: [`16-installer-wsl.md`](16-installer-wsl.md)
- Docker compose layer: [`15-docker.md`](15-docker.md)
- React the GUI displays: [`13-frontend-react.md`](13-frontend-react.md)
- Update endpoint the GUI polls: [`10-cloud-api.md`](10-cloud-api.md) (`/version`)
- Known issues for this layer: [`21-known-issues.md`](21-known-issues.md) §3.2

---

**Last verified:** 2026-05-04.

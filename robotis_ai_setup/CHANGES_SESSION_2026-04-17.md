# Changes Made — Session 2026-04-17

## Overview

Replaced Docker Desktop with a bundled, headless WSL2 distro (`EduBotics`) so the
student never sees Docker branding, a tray icon, license prompts, or the Docker
dashboard. The product now feels like a single shipped application.

Scope picked: **Tier 2** from the planning session (see
`C:\Users\svend\.claude\plans\now-i-want-to-elegant-nebula.md`). The
force-migration path is wired in — old Docker-Desktop installs are
silent-uninstalled on upgrade.

---

## 1. Bundled WSL2 rootfs (new)

New directory `wsl_rootfs/` builds an Ubuntu 22.04 tarball containing:

- `docker-ce`, `docker-ce-cli`, `containerd.io`, `docker-compose-plugin`, `docker-buildx-plugin`
- `nvidia-container-toolkit` (for `docker-compose.gpu.yml` runtime:nvidia)
- systemd enabled via `/etc/wsl.conf` (`[boot] systemd=true`)
- `dockerd` auto-started via manual symlinks into `/etc/systemd/system/multi-user.target.wants/`
  (doesn't rely on `systemctl enable` running inside the Docker build, which can fail without dbus)
- `/etc/docker/daemon.json` registers the `nvidia` runtime and enables BuildKit

**Files:**
- `wsl_rootfs/Dockerfile`
- `wsl_rootfs/wsl.conf`
- `wsl_rootfs/daemon.json`
- `wsl_rootfs/build_rootfs.sh` — outputs `installer/assets/edubotics-rootfs.tar.gz` (~400 MB)
- `wsl_rootfs/README.md`

Build command:
```bash
cd robotis_ai_setup/wsl_rootfs && ./build_rootfs.sh
```

---

## 2. Installer changes

### `robotis_ai_setup.iss`

- `[Files]`: ships `assets\edubotics-rootfs.tar.gz` to `{app}\wsl_rootfs`.
- `[InstallDelete]`: wipes `{app}\wsl_rootfs` on upgrade.
- `[Run]`:
  - Step 0 (new): `migrate_from_docker_desktop.ps1` — silent-uninstalls Docker Desktop if present.
  - Step 1: `install_prerequisites.ps1` — Docker Desktop install block **removed**; only WSL2 + usbipd-win left.
  - Step 4 (new): `import_edubotics_wsl.ps1` — runs `wsl --import EduBotics ...`, waits for dockerd.
  - Step 5: `pull_images.ps1` — now routed through `wsl -d EduBotics -- docker pull`.
- `[UninstallRun]`: stops containers via `uninstall_stop_containers.ps1` (derives its path from `$PSScriptRoot`, so non-default install locations still work), then `wsl --unregister EduBotics`.
- `[Code]`:
  - `IsDistroRegistered()` replaces `IsDockerRunning()` as the readiness check.
  - `ToWslPath()` Pascal helper for dynamic WSL path expansion on uninstall.
  - `ShouldImportDistro()` / `ShouldPullImages()` gate on reboot flag + distro registration.

### New PowerShell scripts

- `scripts/migrate_from_docker_desktop.ps1` — idempotent, writes `.migrated` flag. Stops existing EduBotics compose stack, runs Docker Desktop silent uninstaller, unregisters `docker-desktop` + `docker-desktop-data` distros, removes the Docker Desktop RunAtLogon registry entry.
- `scripts/import_edubotics_wsl.ps1` — `wsl --import EduBotics $ProgramData\EduBotics\wsl ...`, waits up to 60s for `docker info` inside the distro, falls back to `systemctl start docker.service` if systemd didn't bring it up.
- `scripts/finalize_install.ps1` — post-reboot continuation: runs `import_edubotics_wsl.ps1` + `pull_images.ps1` in sequence. Invoked either from the GUI (UAC) or by re-running the installer.
- `scripts/uninstall_stop_containers.ps1` — called by `[UninstallRun]`; stops containers before distro unregister, derives paths from its own location.

### Modified PowerShell scripts

- `install_prerequisites.ps1` — Docker Desktop install + autostart-registry block removed. Keeps Hyper-V check, `wsl --install --no-distribution`, usbipd MSI install. Still writes `.reboot_required` flag.
- `pull_images.ps1` — every `docker pull` routed through `wsl -d EduBotics -- docker pull`.
- `verify_system.ps1` — now checks distro registration + `wsl -d EduBotics -- docker info` instead of host Docker Desktop. Also validates the rootfs artifact exists.
- `configure_usbipd.ps1` — success message now mentions the `--distribution EduBotics` usage pattern.

---

## 3. GUI changes

### `gui/app/constants.py`

- New: `WSL_DISTRO_NAME = "EduBotics"` (env override via `EDUBOTICS_WSL_DISTRO`).
- New: `_to_wsl_path(win_path)` — converts `C:\foo\bar` → `/mnt/c/foo/bar`.
- New: `DOCKER_DIR_WSL` — `DOCKER_DIR` pre-converted to WSL form.

### `gui/app/docker_manager.py` — full rewrite

- New `_docker_cmd(*args, cwd_wsl=None)` wraps every call as `wsl -d EduBotics [--cd ...] -- docker ...`.
- `is_docker_running()` now probes `wsl -d EduBotics -- docker info` instead of host `docker info`.
- New: `is_distro_registered()`, `start_edubotics_distro()`.
- `wait_for_docker()` pokes `systemctl start docker.service` after 15s if systemd hasn't started it yet.
- `start_containers()`, `stop_containers()`, `start_cloud_only()`, `stop_cloud_only()`, `manager_container_running()`, `get_container_logs()`, `check_for_updates()`, `pull_images()`, `images_exist()` — all routed through `_docker_cmd()`.
- `_compose_args()` emits WSL-style paths for `--env-file` and `-f compose.yml`.
- Dropped: `start_docker_desktop()`, `_compose_cmd()` (Windows-path version).

### `gui/app/wsl_bridge.py`

- `run()`, `list_serial_devices()`, `list_video_devices()` now pin `-d EduBotics` (configurable via `distro=` kwarg).
- New: `is_edubotics_distro_registered()`.
- Dropped: `get_docker_wsl_distro()` (was dead code; used to probe for `docker-desktop` distro).

### `gui/app/device_manager.py`

- `attach_usb_to_wsl()` uses `usbipd attach --wsl --distribution EduBotics --busid ...` (deterministic on multi-distro dev machines).
- `identify_arm_via_docker()`, `start_scanner_container()`, `stop_scanner_container()` all route through the new `_docker()` helper (`wsl -d EduBotics -- docker ...`).

### `gui/app/gui_app.py`

- Docstring + log strings: "Docker Desktop" / "Docker Compose" / "Docker-Images" → "EduBotics-Umgebung" / "Container" / "Images".
- `_run_prerequisite_checks()` now checks `is_distro_registered()` first. If the distro is missing (fresh install + reboot pending, or user skipped image-pull), it calls `_prompt_finalize_install()`.
- New `_prompt_finalize_install()` — asks the student for UAC consent and runs `finalize_install.ps1` elevated via `Start-Process -Verb RunAs`. After completion, re-runs the prerequisite check.

---

## 4. Tests

### New

- `tests/test_docker_manager_wsl.py` — asserts every `subprocess.run(...)` call is wrapped with `wsl -d EduBotics [--cd ...] -- docker ...`. Covers `is_docker_running`, `images_exist`, `start_containers`, `stop_containers`, `manager_container_running`, `get_container_logs`, `is_distro_registered` (incl. UTF-16 NUL handling), `has_gpu` (NOT wrapped — uses host `nvidia-smi`).
- `tests/test_wsl_path_convert.py` — covers `_to_wsl_path()`: drive letters, lowercase, forward slashes, empty strings, trailing separators, non-drive paths.

### Unchanged but still passing

- `tests/test_config_generator.py` — covered `.env` generation; no docker dependency.
- `tests/test_device_manager.py` — mocks `list_usb_devices`; no docker dependency.
- `tests/test_docker_manager.py` — pre-existing mocked tests still pass because they only assert return-code handling.

**Result:** `python -m unittest discover -s tests` → **32 tests, all green.**

---

## 5. Documentation

- `CLAUDE.md` — architecture diagram updated: Student Machine section now shows the EduBotics WSL2 distro hosting dockerd + the 3 containers, with no Docker Desktop. Added `wsl_rootfs/` to the monorepo tree and the build command to the Commands section. Environment section no longer mentions Docker Desktop.

---

## 6. Behavior changes for existing students

**Upgrade path (v2.2.2 → this build):**

1. `migrate_from_docker_desktop.ps1` silent-uninstalls Docker Desktop. ~1-2 min.
2. `import_edubotics_wsl.ps1` creates the `EduBotics` distro. ~1-3 min.
3. `pull_images.ps1` re-pulls the 3 nettername images into the new distro. ~15-30 min.

**One-time cost of upgrade:** the old `docker-desktop-data` named volumes are
destroyed when Docker Desktop is uninstalled. This means recorded datasets
sitting in the `ai_workspace` volume and the HuggingFace cache in
`huggingface_cache` are lost. **This must be noted in release notes.**
Datasets already pushed to HuggingFace are unaffected — they are re-downloaded
into the new volumes on next use.

**Fresh install path:**

1. Inno Setup copies files (rootfs included).
2. Migration script: no Docker Desktop found, exits quickly.
3. WSL2 install triggers reboot flag.
4. Student reboots, launches `EduBotics.exe`.
5. GUI detects missing distro, prompts for admin consent.
6. `finalize_install.ps1` runs under UAC: imports rootfs + pulls images.
7. GUI re-runs prerequisite check, becomes ready.

---

## 7. Known limitations / followups

- Rootfs rebuilds are manual. `wsl_rootfs/build_rootfs.sh` needs to be run on
  WSL2 Ubuntu before each installer build. Consider adding a GitHub Action that
  runs this and uploads the tarball as a release asset.
- No offline (T3) variant yet — the 3 Docker images still pull from
  `nettername/*` on first run. Sven's T3 decision was "defer" during planning.
- No Tauri UI shell (T4) — tkinter GUI stays. Future workstream.
- `usbipd attach --distribution EduBotics` requires usbipd 4.x+. Already
  enforced by `configure_usbipd.ps1`.
- `_prompt_finalize_install()` currently spawns an elevated PowerShell window
  that the student sees. If a silent flow is wanted, we'd need a helper EXE
  with an embedded manifest requesting administrator.

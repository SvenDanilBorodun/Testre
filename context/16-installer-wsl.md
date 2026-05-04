# 16 — Windows Installer + WSL2 Rootfs

> **Layer:** Distribution + bootstrap
> **Location:** `Testre/robotis_ai_setup/installer/` + `Testre/robotis_ai_setup/wsl_rootfs/`
> **Owner:** Our code
> **Read this before:** editing `.iss`, `.ps1` scripts, rootfs Dockerfile, `start-dockerd.sh`, `wsl.conf`, `daemon.json`.

---

## 1. Files

```
installer/
├── robotis_ai_setup.iss        # Inno Setup script (~282 lines)
├── output/                     # iscc compile output (gitignored)
├── assets/
│   ├── edubotics-rootfs.tar.gz       # ~193 MB — built by wsl_rootfs/build_rootfs.sh
│   ├── edubotics-rootfs.tar.gz.sha256 # sidecar (verified before import)
│   ├── EduBotics.exe                  # PyInstaller GUI (~5.8 MB)
│   ├── icon.ico
│   └── license.txt
└── scripts/
    ├── migrate_from_docker_desktop.ps1   # silent uninstall Docker Desktop, unregister DD distros
    ├── install_prerequisites.ps1         # wsl --install + usbipd-win MSI with SHA256 verify
    ├── configure_wsl.ps1                  # merge memory=8GB swap=4GB into ~/.wslconfig
    ├── configure_usbipd.ps1               # usbipd policy add for ROBOTIS VID 2F5D
    ├── import_edubotics_wsl.ps1           # SHA256 + wsl --import + dockerd readiness poll
    ├── pull_images.ps1                    # docker pull 3 images
    ├── verify_system.ps1                  # post-install health checks
    ├── finalize_install.ps1               # post-reboot: re-run import + pull (UAC)
    └── uninstall_stop_containers.ps1      # docker compose down before --unregister

wsl_rootfs/
├── README.md
├── Dockerfile                  # ubuntu:22.04 + Docker 27.5.1 + nvidia-container-toolkit + tzdata
├── build_rootfs.sh             # docker build → docker export → gzip → SHA256 sidecar
├── daemon.json                 # Docker daemon config (overlay2, nvidia runtime, snapshotter:false)
├── wsl.conf                    # boot.command + user.default + interop + hostname
└── start-dockerd.sh            # boot launcher + watchdog
```

---

## 2. Inno Setup (.iss) — sections

### `[Setup]`

```ini
AppId       = {B7E3F2A1-8C4D-4E5F-9A6B-1D2E3F4A5B6C}     # immutable; uninstall registry tracking
AppName     = EduBotics
AppVersion  = 2.2.2
AppPublisher= EduBotics
DefaultDirName= {autopf}\EduBotics                         # → C:\Program Files\EduBotics
DefaultGroupName = EduBotics
OutputBaseFilename = EduBotics_Setup
OutputDir   = output
Compression = lzma2
SolidCompression = yes
PrivilegesRequired = admin
WizardStyle = modern
LicenseFile / SetupIconFile / UninstallDisplayIcon = assets/...
```

### Pinned third-party dependency

```pascal
#define UsbipdVersion "5.3.0"
#define UsbipdSha256  "1C984914AEC944DE19B64EFF232421439629699F8138E3DDC29301175BC6D938"
```

usbipd-win 5.x changed MSI naming → can't use GitHub "latest" alias. Pinning version+SHA256 prevents accidental breakage on upstream changes.

### `[InstallDelete]` (legacy cleanup before install)

- `gui/` — full wipe (PyInstaller bundle may have renamed/removed modules)
- `scripts/` — full wipe (PowerShell helpers may be renamed)
- `{app}\docker\.env` — v2.1.0/v2.2.0 lived in Program Files; v2.2.1+ in `%LOCALAPPDATA%`
- `{app}\wsl_rootfs` — old rootfs copies; distro re-imported fresh

### `[Files]`

| Source | Destination | Flags |
|---|---|---|
| `..\docker\docker-compose.yml` | `{app}\docker\` | |
| `..\docker\docker-compose.gpu.yml` | `{app}\docker\` | |
| `..\docker\.env.template` | `{app}\docker\` | |
| `..\docker\physical_ai_server\.s6-keep` | `{app}\docker\physical_ai_server\` | |
| `..\..\..\installer\assets\edubotics-rootfs.tar.gz` | `{app}\wsl_rootfs\` | |
| `..\..\..\installer\assets\edubotics-rootfs.tar.gz.sha256` | `{app}\wsl_rootfs\` | |
| `..\..\..\gui\dist\EduBotics\*` | `{app}\gui\` | recursesubdirs |
| `scripts\*.ps1` | `{app}\scripts\` | |
| `..\..\..\installer\assets\icon.ico` | `{app}\` | |

### `[Icons]`

- Desktop: "EduBotics starten" → `{app}\gui\EduBotics.exe`
- Start Menu: same exe + "Installation prüfen" (runs `verify_system.ps1`)

### `[Run]` — installation pipeline (executed in order, hidden)

1. `migrate_from_docker_desktop.ps1`
2. `install_prerequisites.ps1` (with `-UsbipdMsiUrl ... -UsbipdMsiSha256 ...`)
3. `configure_wsl.ps1`
4. `configure_usbipd.ps1`
5. `import_edubotics_wsl.ps1` — **conditional** on `ShouldImportDistro()` (skips if reboot pending)
6. `pull_images.ps1` — **conditional** on `ShouldPullImages()` (skips if reboot pending OR distro missing)
7. `verify_system.ps1` — `postinstall` checkbox on final wizard page
8. `EduBotics.exe` launch (`postinstall skipifsilent nowait`)

### `[UninstallRun]`

1. `uninstall_stop_containers.ps1` — graceful `docker compose down`
2. `wsl --unregister EduBotics` — destroys VHDX (and named volumes inside)

`%LOCALAPPDATA%\EduBotics\.env` is intentionally left behind (regenerated on reinstall).

### `[Code]` Pascal functions

| Function | Purpose |
|---|---|
| `IsDistroRegistered()` | parse `wsl --list --quiet` output (UTF-16LE, NUL handling) |
| `IsRebootRequired()` | check `.reboot_required` marker in `{app}\scripts\` |
| `ShouldImportDistro()` | `not IsRebootRequired()` |
| `ShouldPullImages()` | `(not IsRebootRequired()) and IsDistroRegistered()` |
| `NeedRestart()` | returns true if reboot flag exists → triggers Inno reboot prompt |
| `ToWslPath()` | `C:\foo\bar` → `/mnt/c/foo/bar` |
| `CleanupSourceInstaller()` | delete `.exe` from `%TEMP%` if installer was downloaded there |
| `CurStepChanged()` | `ssInstall`: `docker compose down` if upgrading; `ssDone`: cleanup |

---

## 3. PowerShell scripts — what each does

### `migrate_from_docker_desktop.ps1`

One-time on first install, idempotent via `.migrated` marker.

1. Search `Program Files\Docker\Docker\Docker Desktop.exe` and similar
2. Best-effort `docker compose down`
3. Uninstall Docker Desktop:
   - Primary: `Docker Desktop Installer.exe uninstall --quiet`
   - Fallback: WMI `Win32_Product` uninstall by package name
4. Unregister `docker-desktop` + `docker-desktop-data` WSL distros
5. Find logged-in user (via explorer.exe owner WMI) → SID lookup → delete `HKEY_USERS\<SID>\...\Run\Docker Desktop` registry key
6. Create `.migrated` marker

### `install_prerequisites.ps1`

Parameters: `-UsbipdMsiUrl`, `-UsbipdMsiSha256`.

Checks:
- Windows build ≥ 22000 (Win11)
- **Reject Home edition** (no Hyper-V)
- Hyper-V Requirements via `systeminfo` (warn if disabled)
- Controlled Folder Access (`Get-MpPreference`) — warn if enabled

Steps:
1. WSL2: `wsl --status` → if not installed, `wsl --install --no-distribution`. On success, set `$needsReboot = $true`, exit 0.
2. usbipd-win:
   - **Sentinel check**: fail loud if SHA256 pin is `"RELEASE_PIN_NEEDED"` (catches release with incomplete pinning)
   - `Invoke-WebRequest` MSI to temp
   - Compute SHA256, compare case-insensitive
   - `msiexec.exe /i <msi> /quiet /norestart`
3. Create `.reboot_required` marker if `needsReboot`; else delete it (re-run after reboot path)

### `configure_wsl.ps1`

Runs elevated but finds **logged-in user's** profile (not admin's) via:
1. `Get-WmiObject Win32_Process -Filter "Name='explorer.exe'"` → owner
2. SID lookup in `HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList`
3. Fallback to current `$env:USERPROFILE`

Recommended settings:
```ini
memory=8GB
swap=4GB
```

Merge logic: read existing `~/.wslconfig`, parse `^(\w+)\s*=\s*(.+)$`, only ADD missing settings. Color-coded output for "kept" vs "added". Non-destructive.

### `configure_usbipd.ps1`

Detects usbipd version via regex on `usbipd --version`. Skips if version &lt; 4.x (no `policy` subcommand).

Adds policies for:
- VID `2F5D` PID `0103` (OpenRB-150)
- VID `2F5D` PID `2202` (alt firmware)
- Auto-discovers other ROBOTIS PIDs via `usbipd list`

5.x: `usbipd policy add ... --operation AutoBind`
4.x: `usbipd policy add ...` (no `--operation`)

### `import_edubotics_wsl.ps1`

Parameters: `-DistroName`, `-InstallRoot` (default `$env:ProgramData\EduBotics\wsl`), `-RootfsPath` (resolved from multiple candidates).

1. **Reboot guard**: exit 0 if `.reboot_required` exists (kernel not ready)
2. **Disk space preflight**: `Get-Volume`, require **20 GB free**, German error otherwise
3. **SHA256 verification**: read `.sha256` sidecar, `Get-FileHash` tarball, compare. **Exit 1 on mismatch.**
4. `wsl --unregister EduBotics` if present (upgrade path)
5. `wsl --import EduBotics $InstallRoot $RootfsPath --version 2`
6. Wake VM: `wsl -d EduBotics -- echo ready`
7. Poll `docker info` up to **180 s**. On timeout: `wsl -d EduBotics -- /usr/local/bin/start-dockerd.sh` then poll again.
8. Show docker version on success

### `pull_images.ps1`

Parameters: `-Registry` (default `nettername`), `-DistroName`.

1. Read `IMAGE_TAG` from `docker/versions.env` (regex `^\s*IMAGE_TAG\s*=\s*(.+?)\s*$`); fallback `latest`
2. Verify distro + dockerd
3. Pull 3 images: `nettername/{open-manipulator,physical-ai-server,physical-ai-manager}:${IMAGE_TAG}`
4. `docker image prune -f` for cleanup

### `verify_system.ps1`

7 checks (continue on failure, returns final `$allOk`):
1. WSL2 status
2. EduBotics distro registered
3. Docker engine reachable
4. usbipd installed
5. Docker images present (warn if missing — pulled on first GUI launch)
6. NVIDIA GPU (warn if missing)
7. Install dir files exist (compose YAMLs, .s6-keep, rootfs tar.gz)

### `finalize_install.ps1`

Post-reboot flow. Parameters: `-LogPath`, `-MarkerPath`.

1. Write marker file with `(timestamp, PID, username)` — proof UAC succeeded
2. `Start-Transcript -Path $LogPath` — captures stdout/stderr for GUI to display
3. Delete `.reboot_required`
4. Run `import_edubotics_wsl.ps1` (exit 1 on failure)
5. Run `pull_images.ps1` (exit 1 on failure)
6. `Stop-Transcript`

### `uninstall_stop_containers.ps1`

1. Find install dir from `$PSScriptRoot/..` (parent of scripts/)
2. Convert to WSL path
3. If distro registered + dockerd running: `wsl -d EduBotics --cd <wsl-docker> -- docker compose down`
4. Always exit 0 (best-effort)

---

## 4. Reboot handling

```
install_prerequisites.ps1 runs `wsl --install`
   → if WSL2 not present, kernel install needs reboot
   → write .reboot_required to {app}\scripts\

Inno Setup `IsRebootRequired()` returns true
   → `NeedRestart()` returns true
   → Inno prompts "Restart now?"

[reboot]

GUI launches on next boot
   → detects .reboot_required marker
   → re-elevates via _elevate_and_wait
   → runs finalize_install.ps1 (UAC)
       → deletes .reboot_required
       → runs import_edubotics_wsl.ps1
       → runs pull_images.ps1
       → writes marker file proving success
       → transcript log goes to %TEMP%\edubotics_finalize.log
   → GUI parses last 30 lines of transcript, displays to user
   → re-runs prerequisite checks
```

---

## 5. WSL distro lifecycle

| Operation | Effect |
|---|---|
| `wsl --import EduBotics $env:ProgramData\EduBotics\wsl edubotics-rootfs.tar.gz --version 2` | Creates distro + VHDX |
| `wsl -d EduBotics -- echo ready` | Wakes VM (boot.command in wsl.conf runs) |
| `wsl --unregister EduBotics` | **Destroys** VHDX, including named volumes inside (`ai_workspace`, `huggingface_cache`) — known issue [§9 of known-issues](21-known-issues.md) |
| `wsl --terminate EduBotics` | Stops VM but preserves VHDX |

VHDX path: `$env:ProgramData\EduBotics\wsl\ext4.vhdx`. Dynamically sized (~30-50 GB).

---

## 6. wsl_rootfs/Dockerfile

```dockerfile
FROM ubuntu:22.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg iproute2 iputils-ping jq kmod \
    lsb-release systemd tzdata udev usbutils v4l-utils \
 && rm -rf /var/lib/apt/lists/*

# Critical: tzdata creates /etc/timezone + /etc/localtime as FILES (not dirs)
# docker-compose.yml mounts these read-only; without files, mount fails
RUN ln -sf /usr/share/zoneinfo/Europe/Berlin /etc/localtime && \
    echo "Europe/Berlin" > /etc/timezone

# Docker CE apt repo
RUN install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
    echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu jammy stable" > /etc/apt/sources.list.d/docker.list

# nvidia-container-toolkit apt repo
RUN curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /etc/apt/keyrings/nvidia-container-toolkit-keyring.gpg && \
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | sed 's#deb #deb [signed-by=/etc/apt/keyrings/nvidia-container-toolkit-keyring.gpg] #' > /etc/apt/sources.list.d/nvidia-container-toolkit.list

# CRITICAL VERSION PIN
ARG DOCKER_VERSION=5:27.5.1-1~ubuntu.22.04~jammy
ARG CONTAINERD_VERSION=1.7.27-1
RUN apt-get update && apt-get install -y --no-install-recommends \
    docker-ce=$DOCKER_VERSION \
    docker-ce-cli=$DOCKER_VERSION \
    containerd.io=$CONTAINERD_VERSION \
    docker-buildx-plugin docker-compose-plugin nvidia-container-toolkit \
 && rm -rf /var/lib/apt/lists/* \
 && apt-mark hold docker-ce docker-ce-cli containerd.io   # prevent auto-upgrades

COPY wsl.conf /etc/wsl.conf
COPY daemon.json /etc/docker/daemon.json
COPY start-dockerd.sh /usr/local/bin/start-dockerd.sh
RUN chmod +x /usr/local/bin/start-dockerd.sh

RUN echo "edubotics" > /etc/hostname

# Cleanup to keep tar size down (~350-450 MB target)
RUN rm -rf /usr/share/doc /usr/share/man /var/cache/apt/archives \
    /var/lib/apt/lists /tmp/* /var/tmp/*
```

**Why Docker 27.5.1 (not 29.x):**
- Docker 29.x's containerd-snapshotter regression on custom-imported WSL2 rootfs
- Symptom: large multi-layer image pulls fail with "snapshot ... does not exist"
- Docker 29 removed the ability to disable containerd-snapshotter via config
- 27.5.1 keeps overlay2 default (battle-tested on WSL2)

---

## 7. build_rootfs.sh

```bash
#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

docker build --pull -t edubotics-rootfs:latest .

cid=$(docker create edubotics-rootfs:latest true)
trap "docker rm -f $cid >/dev/null 2>&1 || true" EXIT

docker export $cid | gzip -9 > ../installer/assets/edubotics-rootfs.tar.gz

# SHA256 sidecar
sha256sum ../installer/assets/edubotics-rootfs.tar.gz > ../installer/assets/edubotics-rootfs.tar.gz.sha256
```

Output: `installer/assets/edubotics-rootfs.tar.gz` (~350-450 MB).

---

## 8. daemon.json

```json
{
  "data-root": "/var/lib/docker",
  "storage-driver": "overlay2",
  "default-runtime": "runc",
  "runtimes": {
    "nvidia": {
      "path": "nvidia-container-runtime",
      "runtimeArgs": []
    }
  },
  "features": {
    "buildkit": true,
    "containerd-snapshotter": false
  },
  "userland-proxy-path": "/usr/bin/docker-proxy",
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
```

| Key | Reason |
|---|---|
| `containerd-snapshotter: false` | **Critical** — prevents Docker 29 regression (we're on 27.5.1 but defensive) |
| `storage-driver: overlay2` | Stable on WSL2 |
| `nvidia` runtime | GPU support via nvidia-container-toolkit |
| log rotation 10m × 3 | Prevents container logs filling VHDX during long training |

---

## 9. wsl.conf

```ini
[boot]
command=/usr/local/bin/start-dockerd.sh

[user]
default=root

[network]
generateResolvConf=true
hostname=edubotics

[interop]
enabled=true
appendWindowsPath=false
```

| Key | Reason |
|---|---|
| `boot.command` (not `systemd=true`) | systemd unreliable on custom-imported rootfs; PID 1 stays at WSL's init |
| `user.default=root` | Containers need privileged + root for USB device access |
| `interop.appendWindowsPath=false` | Don't leak Windows PATH into distro (cleaner env, fewer surprises) |
| `hostname=edubotics` | Shows in `wsl --list` |

---

## 10. start-dockerd.sh

```bash
#!/usr/bin/env bash

# WSL boot context has empty PATH; explicitly export it
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

start_dockerd() {
    nohup /usr/bin/dockerd >> /var/log/dockerd.log 2>&1 &
}

# First start (idempotent: only spawn if not already running)
if ! pgrep -x dockerd > /dev/null; then
    start_dockerd
fi

# Watchdog — added in session 2026-04-17
nohup bash -c '
while true; do
    if ! pgrep -x dockerd > /dev/null; then
        echo "[$(date -u +%FT%TZ)] dockerd died, respawning..." >> /var/log/dockerd.log
        nohup /usr/bin/dockerd >> /var/log/dockerd.log 2>&1 &
    fi
    sleep 5
done
' >> /var/log/dockerd-watchdog.log 2>&1 &
```

**Watchdog rationale:** Old version was a single `nohup` invocation. dockerd segfault → nothing restarted it → `docker info` silently fails until student `wsl --terminate`s + re-enters. Watchdog respawns within 5 s.

---

## 11. usbipd version drift handling

| Version | Policy command syntax |
|---|---|
| pre-4.x | No `policy` subcommand → script warns + exits 0 (students must run GUI as admin) |
| 4.x | `usbipd policy add --hardware-id <hwid> --effect Allow` (no `--operation`) |
| 5.x | `usbipd policy add --hardware-id <hwid> --effect Allow --operation AutoBind` |

`configure_usbipd.ps1` parses `usbipd --version` and branches.

---

## 12. Reproducibility hazards

1. **Floating `IMAGE_TAG=latest`** in `docker/versions.env` — non-deterministic installs. Pin to digest or semver tag.
2. **`ARG DOCKER_VERSION` defaults inline** — if build changes default, drift. Make injection-only via build-arg.
3. **`ubuntu:22.04` floating base** — pulls whatever resolves on build day. Pin to digest: `FROM ubuntu:22.04@sha256:...`.
4. **APT transitive deps float** — `--no-install-recommends` reduces but doesn't eliminate. Use `apt-mark hold` for critical packages (already done for docker-ce / containerd).
5. **usbipd-win MSI hash** in `.iss` — if upstream rebuilds the same version, hash changes. Pin against compromise but accept the maintenance burden.
6. **rootfs tar.gz** verified by SHA256 sidecar — if both files are tampered identically, no detection. Could GPG-sign the sidecar.
7. **`docker-compose.yml` etc. installed as-is** — no integrity check at GUI startup. Could compute hash at build, validate before `docker compose up`.

---

## 13. Local dev / build

### Build the rootfs

```bash
cd robotis_ai_setup/wsl_rootfs
./build_rootfs.sh
# Output: ../installer/assets/edubotics-rootfs.tar.gz + .sha256
```

### Build the installer

```bash
# Requires Inno Setup 6.x on Windows
iscc installer/robotis_ai_setup.iss
# Output: installer/output/EduBotics_Setup.exe
```

### Smoke test on a clean Windows VM

**Never test installer scripts on your dev machine without a VM.** Snapshot first.

1. Clean Windows 11 Pro VM
2. Run `EduBotics_Setup.exe` as admin
3. Watch each PowerShell step's transcript (visible in install dialog or `%TEMP%\Setup Log *.txt`)
4. Reboot if prompted; GUI should detect `.reboot_required` and complete via `finalize_install.ps1`
5. Run `verify_system.ps1` from Start Menu
6. Connect a robot arm + camera → run `EduBotics.exe`

### Test the rootfs in isolation

```bash
# On WSL2 host
wsl --import test-edubotics C:\temp\edubotics-test edubotics-rootfs.tar.gz --version 2
wsl -d test-edubotics -- echo ready
wsl -d test-edubotics -- docker info
wsl --unregister test-edubotics
```

---

## 14. Cross-references

- GUI invokes these scripts: [`14-windows-gui.md`](14-windows-gui.md) §6
- Docker Engine inside this distro: [`15-docker.md`](15-docker.md)
- Operations (rotate WSL, rebuild rootfs): [`20-operations.md`](20-operations.md)
- Known issues for this layer: [`21-known-issues.md`](21-known-issues.md) §3.1
- Account rollout (post-merge ops): [`23-rollout-accounts.md`](23-rollout-accounts.md)

---

**Last verified:** 2026-05-04.

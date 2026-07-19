# EduBotics WSL2 Rootfs

Headless Docker Engine bundled as a WSL2 distribution, so students never see Docker Desktop.

## Contents

- Ubuntu 22.04 base
- `start-dockerd.sh` auto-starts dockerd at distro boot via wsl.conf's `[boot] command` (systemd is deliberately NOT used — it is unreliable on a custom-imported rootfs)
- Docker CE + buildx + compose plugin
- NVIDIA Container Toolkit (for `docker-compose.gpu.yml`)
- `/etc/wsl.conf` → `[boot] command=/usr/local/bin/start-dockerd.sh`, `appendWindowsPath=false`, default user root, hostname `edubotics`
- `/etc/docker/daemon.json` → registers NVIDIA runtime, enables BuildKit, caps logs

Exported rootfs lands at `installer/assets/edubotics-rootfs.tar.gz` (~350-450 MB compressed).

## Build (maintainer only)

Run on WSL2 Ubuntu or any Linux with Docker:

```bash
cd robotis_ai_setup/wsl_rootfs
./build_rootfs.sh
```

The Inno Setup installer picks up `installer/assets/edubotics-rootfs.tar.gz` via the `[Files]` section and installs it with `wsl --import EduBotics ...` during Setup.

## Manual install / smoke test

```powershell
wsl --import EduBotics "$env:ProgramData\EduBotics\wsl" .\edubotics-rootfs.tar.gz --version 2
wsl -d EduBotics -- /usr/local/bin/start-dockerd.sh   # dockerd also auto-starts at boot via wsl.conf
wsl -d EduBotics -- docker info
```

To remove:

```powershell
wsl --unregister EduBotics
```

## Rebuild cadence

Rebuild when:
- Ubuntu security-criticals need to ship (roughly quarterly)
- Docker CE major version bump
- NVIDIA container toolkit bumps the API
- `/etc/wsl.conf` or `daemon.json` changes

Bump `ROOTFS_VERSION` (in `wsl_rootfs/ROOTFS_VERSION`) when shipping a new rootfs — `ci.yml::rootfs-version-guard` enforces the bump, and the installer's rootfs-version gate re-imports the distro (DESTROYING its Docker volumes: datasets, HF cache, calibration) ONLY when the stamp changes; a matching stamp preserves student data on upgrade (see `import_edubotics_wsl.ps1`).

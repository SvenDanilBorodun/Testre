# EduBotics WSL2 Rootfs

Headless Docker Engine bundled as a WSL2 distribution, so students never see Docker Desktop.

## Contents

- Ubuntu 22.04 base
- systemd (auto-starts dockerd at distro boot)
- Docker CE + buildx + compose plugin
- NVIDIA Container Toolkit (for `docker-compose.gpu.yml`)
- `/etc/wsl.conf` → systemd on, default user root, hostname `edubotics`
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
wsl -d EduBotics -- systemctl start docker
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

Bump `APP_VERSION` in `gui/app/constants.py` when shipping a new rootfs — the installer overwrites the existing distro on upgrade (see `import_edubotics_wsl.ps1`).

# ROBOTIS AI One-Click Windows Setup

## Project Goal

Convert a multi-page Linux tutorial for Open Manipulator teleoperation + Physical AI Tools into a one-click Windows installer for students: **install `.exe` → plug in hardware → click "Start" → browser opens at `http://localhost`**.

Students have Windows 11 PCs only (some with NVIDIA GPUs, some without).

## Workspace Layout

```
23/                              ← workspace root (this directory)
├── open_manipulator/            ← upstream ROBOTIS repo (read-only reference, has its own .git)
├── physical_ai_tools/           ← upstream ROBOTIS repo (read-only reference, has its own .git)
├── robotis_ai_setup/            ← OUR repo — all implementation lives here
└── tidy-bouncing-phoenix.md     ← full plan document with architecture, port map, data flow, etc.
```

## Architecture (3 Docker Containers in Docker Desktop WSL2)

```
Windows 11
├── Desktop GUI (PyInstaller .exe, tkinter) — scans USB, identifies arms, starts Docker
├── Docker Desktop (WSL2)
│   ├── open_manipulator      — ROS2 Jazzy, follower+leader arm control, USB camera
│   │   Custom entrypoint_omx.sh: follower → wait → trajectory → leader → camera
│   ├── physical_ai_server    — ROS2 + PyTorch + s6-overlay, rosbridge:9090, video:8080
│   └── physical_ai_manager   — nginx:80 serving React SPA (connects to ws://localhost:9090)
└── Browser at http://localhost — the student's interface
```

All containers use `network_mode: host` with `ROS_DOMAIN_ID=30`.

## Key Technical Details

| Item | Value |
|------|-------|
| Leader servo IDs | 1-6 (baudrate 1M, protocol 2.0) |
| Follower servo IDs | 11-16 (baudrate 1M, protocol 2.0) |
| ROBOTIS USB VID | 2F5D |
| Ports | 80 (web UI), 8080 (video), 9090 (rosbridge), 5555 (ZMQ inference internal) |
| ROS_DOMAIN_ID | 30 |
| Docker registry | nettername |
| Base images | `robotis/open-manipulator:latest`, `robotis/ros:jazzy-ros-base-torch2.7.0-cuda12.8.0` |

## robotis_ai_setup/ Structure (our code)

```
robotis_ai_setup/
├── installer/
│   ├── robotis_ai_setup.iss              # Inno Setup script
│   ├── assets/license.txt
│   └── scripts/                          # PowerShell: install_prerequisites, configure_wsl,
│                                         #   configure_usbipd, verify_system, pull_images
├── gui/
│   ├── main.py                           # Entry point
│   ├── build.spec                        # PyInstaller config
│   ├── dist/RobotisAI/RobotisAI.exe     # Built executable
│   └── app/
│       ├── gui_app.py                    # Main tkinter window (380 lines)
│       ├── device_manager.py             # USB scan, usbipd attach, arm identification
│       ├── docker_manager.py             # docker compose lifecycle, GPU detection
│       ├── config_generator.py           # .env file generation
│       ├── health_checker.py             # HTTP/socket health checks
│       ├── wsl_bridge.py                 # WSL2 command wrapper
│       └── constants.py                  # Registry, ports, IDs, paths, timeouts
├── docker/
│   ├── docker-compose.yml                # 3 services (host network, privileged)
│   ├── docker-compose.gpu.yml            # nvidia runtime override
│   ├── .env.template
│   ├── build-images.sh                   # Maintainer-only image builder
│   ├── open_manipulator/                 # Thin layer: entrypoint_omx.sh + identify_arm.py
│   └── physical_ai_server/              # Thin layer: patches upstream server_inference.py bug
│       ├── Dockerfile
│       └── patches/fix_server_inference.py
└── tests/                                # 14 unit tests (all pass)
```

## Known Upstream Bug (Patched)

`physical_ai_tools/physical_ai_server/physical_ai_server/inference/server_inference.py`:
- `self._endpoints` dict never initialized → AttributeError on server start
- Duplicate `InferenceManager` construction (lines 60-64 repeated at 71-75)
- **Fixed** via thin Docker layer in `robotis_ai_setup/docker/physical_ai_server/`

## Verification Status (as of 2026-03-27)

**Verified on this Windows 11 machine (no hardware):**
- All 14 unit tests pass
- All Python files parse cleanly (AST check)
- All PowerShell scripts parse cleanly
- docker-compose.yml + GPU override validate with `docker compose config`
- GUI creates window, all widgets initialize correctly
- PyInstaller .exe launches and exits cleanly
- WSL2, Docker CLI, usbipd 5.3 all present and functional
- `.wslconfig` merge is idempotent
- Health checker correctly handles offline services (no crashes)
- USB device listing works (usbipd list parsing)
- WSL bridge commands execute correctly
- Patch script fixes upstream bug correctly

**Cannot verify without hardware:**
- USB passthrough (usbipd attach)
- Arm identification (needs Dynamixel servos)
- ROS2 container communication
- Full end-to-end recording/training/inference

## Commands

```bash
# Run tests
cd robotis_ai_setup && python -m unittest discover -s tests -v

# Validate compose
cd robotis_ai_setup/docker && docker compose config

# Build GUI exe
cd robotis_ai_setup/gui && pyinstaller build.spec

# Build Docker images (maintainer, Linux)
cd robotis_ai_setup/docker && REGISTRY=nettername ./build-images.sh

# Verify installation
powershell -ExecutionPolicy Bypass -File installer/scripts/verify_system.ps1
```

## Important File References

| What | Where |
|------|-------|
| Full plan + architecture | `tidy-bouncing-phoenix.md` (root of workspace) |
| Entrypoint logic | `robotis_ai_setup/docker/open_manipulator/entrypoint_omx.sh` |
| Arm identification | `robotis_ai_setup/docker/open_manipulator/identify_arm.py` |
| omx_ai.launch.py (does NOT forward port_name) | `open_manipulator/open_manipulator_bringup/launch/omx_ai.launch.py` |
| Follower launch (accepts port_name) | `open_manipulator/open_manipulator_bringup/launch/omx_f_follower_ai.launch.py` |
| Leader launch (accepts port_name) | `open_manipulator/open_manipulator_bringup/launch/omx_l_leader_ai.launch.py` |
| Camera launch | `open_manipulator/open_manipulator_bringup/launch/camera_usb_cam.launch.py` |
| Servo IDs / baudrate | `open_manipulator/open_manipulator_description/ros2_control/omx_f.ros2_control.xacro` (follower) |
| | `open_manipulator/open_manipulator_description/ros2_control/omx_l.ros2_control.xacro` (leader) |
| AI server launch (4 nodes) | `physical_ai_tools/physical_ai_server/launch/physical_ai_server_bringup.launch.py` |
| Robot config (topics) | `physical_ai_tools/physical_ai_server/config/omx_f_config.yaml` |
| Web UI rosbridge connection | `physical_ai_tools/physical_ai_manager/src/features/ros/rosSlice.js` (hardcodes :9090) |
| s6 service setup | `physical_ai_tools/docker/s6-services/common/ros2_service_run.sh` |
| Upstream bug location | `physical_ai_tools/physical_ai_server/physical_ai_server/inference/server_inference.py` |

## Dev Environment

- Windows 11 Pro build 26200
- Python 3.14 (system)
- Docker Desktop 29.2.1 + Compose v5.1.0
- WSL2 with Ubuntu-24.04 + docker-desktop distros
- usbipd-win 5.3.0 (supports policy)
- No NVIDIA GPU on this machine (CPU mode)

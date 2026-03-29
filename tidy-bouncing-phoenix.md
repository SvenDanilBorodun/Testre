# ROBOTIS AI One-Click Windows Setup — Final Verified Plan

## Context

Students currently follow a multi-page Linux tutorial to set up Open Manipulator teleoperation with Physical AI Tools. This plan converts that into: **install `.exe` → plug in hardware → click "Start" → browser opens at `http://localhost` with working web UI**. Students have **Windows 11 PCs only** (some with NVIDIA GPUs, some without).

The architecture runs 3 Docker containers inside Docker Desktop (WSL2 backend): one for robot hardware control (`open_manipulator`), one for the AI/ROS2 backend (`physical_ai_server`), and one for the web UI (`physical_ai_manager`).

---

## Architecture Overview

```
┌─────────────────── Windows 11 ────────────────────────────┐
│                                                            │
│  ┌─────────────────┐    ┌──────────────────────────────┐  │
│  │ Desktop GUI     │    │  Web Browser                 │  │
│  │ (PyInstaller)   │    │  http://localhost             │  │
│  │                 │    │  (React UI on port 80)       │  │
│  │ • Scan USB      │    │  video stream on port 8080   │  │
│  │ • Select camera │    │  rosbridge WS on port 9090   │  │
│  │ • Start/Stop    │    └──────────────────────────────┘  │
│  └────────┬────────┘                                      │
│           │ usbipd attach + docker compose up              │
│  ┌────────┴───────────────────────────────────────────┐   │
│  │          Docker Desktop (WSL2 backend)              │   │
│  │                                                     │   │
│  │  ┌─── open_manipulator ──────────────────────────┐ │   │
│  │  │ entrypoint_omx.sh                             │ │   │
│  │  │ • Follower arm (port from .env)               │ │   │
│  │  │ • Leader arm (port from .env)                 │ │   │
│  │  │ • USB camera (usb_cam node)                   │ │   │
│  │  │ Publishes: /joint_states, /camera1/image_raw  │ │   │
│  │  └───────────────────────────────────────────────┘ │   │
│  │           ↕ ROS2 DDS (network_mode: host)          │   │
│  │  ┌─── physical_ai_server ────────────────────────┐ │   │
│  │  │ s6-overlay manages:                           │ │   │
│  │  │ • physical_ai_server (ROS2 node)              │ │   │
│  │  │ • rosbridge_websocket (:9090)                 │ │   │
│  │  │ • web_video_server (:8080)                    │ │   │
│  │  │ • rosbag_recorder                             │ │   │
│  │  │ • s6-agent (talos_system_manager)             │ │   │
│  │  │ Subscribes: /joint_states, /camera1/...       │ │   │
│  │  └───────────────────────────────────────────────┘ │   │
│  │                                                     │   │
│  │  ┌─── physical_ai_manager ───────────────────────┐ │   │
│  │  │ nginx serving React app on :80                │ │   │
│  │  │ Connects to rosbridge at ws://localhost:9090  │ │   │
│  │  └───────────────────────────────────────────────┘ │   │
│  └─────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────┘
```

---

## Verified Technical Details

### Port Map (all accessible from Windows browser via Docker Desktop port forwarding)
| Port | Service | Source |
|------|---------|--------|
| 80 | nginx (React web UI) | `physical_ai_manager/nginx.conf` → `listen 80` |
| 8080 | web_video_server (MJPEG) | `physical_ai_server_bringup.launch.py` → default port |
| 9090 | rosbridge_websocket | `physical_ai_server_bringup.launch.py` → default port |
| 5555 | ZMQ inference (internal) | `server_inference.py` → TCP internal only |

### Servo IDs (verified from xacro files)
| Arm | IDs | Baudrate | Default Port | Source |
|-----|-----|----------|-------------|--------|
| Leader | 1, 2, 3, 4, 5, 6 | 1,000,000 | /dev/ttyACM2 | `omx_l.ros2_control.xacro` |
| Follower | 11, 12, 13, 14, 15, 16 | 1,000,000 | /dev/ttyACM0 | `omx_f.ros2_control.xacro` |

### ROS2 Topics (verified from config + launch files)
| Topic | Type | Published By | Consumed By |
|-------|------|-------------|-------------|
| `/joint_states` | JointState | open_manipulator (follower) | physical_ai_server |
| `/leader/joint_trajectory` | JointTrajectory | open_manipulator (leader broadcaster) | physical_ai_server |
| `/camera1/image_raw/compressed` | CompressedImage | open_manipulator (usb_cam) | physical_ai_server |
| `/arm_controller/follow_joint_trajectory` | Action | physical_ai_server (inference) | open_manipulator (follower) |

### ROS2 Environment
- `ROS_DOMAIN_ID=30` (set in both containers, verified in `ros2_service_run.sh` and `open_manipulator/docker/Dockerfile`)
- RMW: FastDDS (default for Jazzy, rmw_zenoh is commented out)
- Discovery: multicast via shared network namespace (`network_mode: host`)

### Camera Config (verified from `omx_f_config.yaml`)
```yaml
camera_topic_list:
  - camera1:/camera1/image_raw/compressed
```
The topic name is built from `camera_usb_cam.launch.py` argument `name` (default: `camera1`), which remaps `image_raw/compressed` → `camera1/image_raw/compressed`.

### Entrypoint Sequence (replaces `omx_ai.launch.py` which doesn't forward port_name)
Verified from `omx_ai.launch.py`: it calls sub-launches without passing `port_name`. Our entrypoint calls them directly:
1. Launch follower with `port_name:=$FOLLOWER_PORT`
2. Wait for `/joint_states` topic
3. Run `joint_trajectory_executor` with `initial_positions.yaml` (2 steps: home → ready, 5s duration, 0.15 epsilon)
4. Launch leader with `port_name:=$LEADER_PORT`
5. Launch camera with `name:=$CAMERA_NAME video_device:=$CAMERA_DEVICE`

---

## Component 1: Docker Infrastructure

### 1.1 docker-compose.yml

```yaml
services:
  open_manipulator:
    container_name: open_manipulator
    image: ${REGISTRY:-robotis-ai-setup}/open-manipulator:latest
    tty: true
    restart: unless-stopped
    cap_add:
      - SYS_NICE
    ulimits:
      rtprio: 99
      rttime: -1
      memlock: 8428281856
    network_mode: host
    ipc: host      # Required: matches original open_manipulator docker-compose
    pid: host      # Required: matches original open_manipulator docker-compose
    environment:
      - ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-30}
      - FOLLOWER_PORT=${FOLLOWER_PORT}
      - LEADER_PORT=${LEADER_PORT}
      - CAMERA_DEVICE=${CAMERA_DEVICE:-/dev/video0}
      - CAMERA_NAME=${CAMERA_NAME:-camera1}
    volumes:
      - /dev:/dev
      - /dev/shm:/dev/shm
      - /etc/timezone:/etc/timezone:ro
      - /etc/localtime:/etc/localtime:ro
    privileged: true
    # NO X11 volumes — Windows has no X11, all interaction via web UI

  physical_ai_server:
    container_name: physical_ai_server
    image: ${REGISTRY:-robotis-ai-setup}/physical-ai-server:latest
    tty: true
    restart: unless-stopped
    cap_add:
      - SYS_NICE
    ulimits:
      rtprio: 99
      rttime: -1
      memlock: 8428281856
    network_mode: host
    environment:
      - ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-30}
    volumes:
      - /dev:/dev
      - /dev/shm:/dev/shm
      - /etc/timezone:/etc/timezone:ro
      - /etc/localtime:/etc/localtime:ro
      - ai_workspace:/workspace
      - huggingface_cache:/root/.cache/huggingface
      - /var/run/robotis/agent_sockets/physical_ai_server:/var/run/agent
    privileged: true
    # NO X11 volumes — Windows has no X11

  physical_ai_manager:
    container_name: physical_ai_manager
    image: ${REGISTRY:-robotis-ai-setup}/physical-ai-manager:latest
    network_mode: host
    restart: unless-stopped
    # NO build: section — students pull pre-built images only

volumes:
  ai_workspace:
  huggingface_cache:
```

**Key differences from Linux originals:**
- No X11 volumes (`/tmp/.X11-unix`, `.docker.xauth`) or DISPLAY env — not available on Windows, not needed (web UI only)
- No source code mounts (`../:/root/ros2_ws/src/...`) — students don't edit code
- `ipc: host` and `pid: host` on open_manipulator (from original, needed for shared memory IPC)
- Named volumes for workspace and huggingface cache (not bind mounts, more portable)
- Agent socket volume on physical_ai_server (from original, needed by talos_system_manager)

### 1.2 docker-compose.gpu.yml (optional GPU override)

```yaml
services:
  physical_ai_server:
    runtime: nvidia
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              capabilities: [gpu]
```

Applied with: `docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d`

### 1.3 .env file (generated by GUI)

```env
FOLLOWER_PORT=/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_XXXX
LEADER_PORT=/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_YYYY
CAMERA_DEVICE=/dev/video0
CAMERA_NAME=camera1
ROS_DOMAIN_ID=30
```

### 1.4 entrypoint_omx.sh

```bash
#!/bin/bash
set -e

source /opt/ros/jazzy/setup.bash
source /root/ros2_ws/install/setup.bash
export ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-30}

echo "========================================"
echo "ROBOTIS Open Manipulator - AI Mode"
echo "Follower: ${FOLLOWER_PORT}"
echo "Leader:   ${LEADER_PORT}"
echo "Camera:   ${CAMERA_DEVICE} as ${CAMERA_NAME}"
echo "========================================"

# --- Validate hardware (with retry for USB attach timing) ---
wait_for_device() {
    local device=$1 label=$2 max_wait=30 count=0
    while [ ! -e "$device" ] && [ $count -lt $max_wait ]; do
        echo "[INIT] Waiting for $label ($device)... ${count}s"
        sleep 1
        count=$((count + 1))
    done
    [ -e "$device" ] || { echo "[ERROR] $label not found: $device"; exit 1; }
    chmod 666 "$device" 2>/dev/null || true
    echo "[INIT] $label found: $device"
}

wait_for_device "$FOLLOWER_PORT" "Follower arm"
wait_for_device "$LEADER_PORT" "Leader arm"

# --- Phase 1: Launch Follower ---
echo "[LAUNCH] Starting follower..."
ros2 launch open_manipulator_bringup omx_f_follower_ai.launch.py \
    port_name:=${FOLLOWER_PORT} &
FOLLOWER_PID=$!

# Wait for follower to be ready (publishing joint_states)
count=0
while ! ros2 topic list 2>/dev/null | grep -q "/joint_states" && [ $count -lt 60 ]; do
    sleep 1; count=$((count + 1))
done
[ $count -lt 60 ] || { echo "[ERROR] Follower timeout"; kill $FOLLOWER_PID; exit 1; }
echo "[LAUNCH] Follower ready."

# --- Phase 2: Initial position trajectory ---
# Verified: joint_trajectory_executor auto-exits after completing all steps
echo "[LAUNCH] Moving to initial position..."
PARAMS_FILE="/root/ros2_ws/install/open_manipulator_bringup/share/open_manipulator_bringup/config/omx_f_follower_ai/initial_positions.yaml"
ros2 run open_manipulator_bringup joint_trajectory_executor \
    --ros-args --params-file "$PARAMS_FILE"
echo "[LAUNCH] Initial position reached."

# --- Phase 3: Launch Leader ---
echo "[LAUNCH] Starting leader..."
ros2 launch open_manipulator_bringup omx_l_leader_ai.launch.py \
    port_name:=${LEADER_PORT} &
LEADER_PID=$!

# --- Phase 4: Launch Camera ---
CAMERA_PID=""
if [ -e "${CAMERA_DEVICE}" ]; then
    echo "[LAUNCH] Starting camera (${CAMERA_DEVICE})..."
    ros2 launch open_manipulator_bringup camera_usb_cam.launch.py \
        name:=${CAMERA_NAME} \
        video_device:=${CAMERA_DEVICE} &
    CAMERA_PID=$!
else
    echo "[WARN] Camera ${CAMERA_DEVICE} not found, skipping."
fi

echo "========================================"
echo "All services running!"
echo "========================================"

# Clean shutdown on SIGTERM (fixed: no kill 0 bug)
cleanup() {
    echo "[SHUTDOWN] Stopping..."
    kill $FOLLOWER_PID $LEADER_PID 2>/dev/null
    [ -n "$CAMERA_PID" ] && kill $CAMERA_PID 2>/dev/null
    wait
}
trap cleanup SIGTERM SIGINT
wait
```

### 1.5 identify_arm.py (runs inside open_manipulator container)

```python
#!/usr/bin/env python3
"""Identify whether a serial port is connected to a leader or follower arm.
Servo IDs verified from omx_l.ros2_control.xacro (1-6) and omx_f.ros2_control.xacro (11-16).
Baudrate verified: 1,000,000 for both arms. Protocol 2.0.
"""
import sys
from dynamixel_sdk import PortHandler, PacketHandler

BAUDRATE = 1_000_000
PROTOCOL = 2.0
LEADER_IDS = [1, 2, 3, 4, 5, 6]
FOLLOWER_IDS = [11, 12, 13, 14, 15, 16]

def identify(port_path: str) -> str:
    port = PortHandler(port_path)
    if not port.openPort():
        return "error:cannot_open"
    port.setBaudRate(BAUDRATE)
    pkt = PacketHandler(PROTOCOL)

    # pkt.ping returns (model_number, comm_result, error)
    # comm_result == 0 means COMM_SUCCESS (index [1], NOT [0])
    leader_count = sum(1 for id in LEADER_IDS if pkt.ping(port, id)[1] == 0)
    follower_count = sum(1 for id in FOLLOWER_IDS if pkt.ping(port, id)[1] == 0)

    port.closePort()

    if leader_count > follower_count:
        return "leader"
    elif follower_count > leader_count:
        return "follower"
    return "unknown"

if __name__ == "__main__":
    print(identify(sys.argv[1]))
```

**Dependency:** Requires `pip install dynamixel-sdk` in the open_manipulator container. The colcon-built DynamixelSDK is a ROS2 C++ package and does NOT provide the `dynamixel_sdk` Python module needed by this script (verified: no `dynamixel_sdk` Python import exists anywhere in the open_manipulator repo).

### 1.6 Docker Image Build Strategy

Students **never build images** — they only pull pre-built ones. A maintainer builds and pushes images using a separate script:

```bash
#!/bin/bash
# build-images.sh — Run by maintainer, NOT by students
REGISTRY=${REGISTRY:-ghcr.io/your-org}

# 1. physical_ai_manager (React + nginx)
# Build context needs full React source
docker build -t ${REGISTRY}/physical-ai-manager:latest \
  -f physical_ai_tools/physical_ai_manager/Dockerfile \
  physical_ai_tools/physical_ai_manager/

# 2. physical_ai_server (ROS2 + AI + s6-overlay)
# Build context is physical_ai_tools/ root (Dockerfile references docker/s6-agent etc.)
docker build -t ${REGISTRY}/physical-ai-server:latest \
  -f physical_ai_tools/physical_ai_server/Dockerfile.amd64 \
  physical_ai_tools/

# 3. open_manipulator (ROS2 + hardware control)
# Built from original Dockerfile, then add entrypoint as thin layer
docker build -t ${REGISTRY}/open-manipulator-base:latest \
  -f open_manipulator/docker/Dockerfile \
  open_manipulator/docker/

# Thin layer adds: entrypoint_omx.sh, identify_arm.py, pip install dynamixel-sdk
docker build -t ${REGISTRY}/open-manipulator:latest \
  -f robotis_ai_setup/docker/open_manipulator/Dockerfile \
  robotis_ai_setup/docker/open_manipulator/

# Push all images
docker push ${REGISTRY}/physical-ai-manager:latest
docker push ${REGISTRY}/physical-ai-server:latest
docker push ${REGISTRY}/open-manipulator:latest
```

### 1.7 open_manipulator Thin Layer Dockerfile

```dockerfile
FROM robotis/open-manipulator:latest

# Install Python dynamixel-sdk for identify_arm.py
# Install v4l-utils for camera device discovery (v4l2-ctl --info)
RUN apt-get update && apt-get install -y --no-install-recommends v4l-utils \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir dynamixel-sdk

# Copy entrypoint and identification script
COPY entrypoint_omx.sh /entrypoint_omx.sh
COPY identify_arm.py /usr/local/bin/identify_arm.py
RUN chmod +x /entrypoint_omx.sh /usr/local/bin/identify_arm.py

ENTRYPOINT ["/entrypoint_omx.sh"]
```

---

## Component 2: Windows GUI (Python + tkinter)

### 2.1 Purpose
The GUI handles everything between "install" and "browser opens":
- USB device scanning and attachment via usbipd
- Arm identification (leader vs follower)
- Camera selection
- .env file generation
- Docker Compose lifecycle
- Health checking

### 2.2 Key Modules

#### `device_manager.py` — USB & Camera Discovery

```python
# USB workflow:
# 1. List USB devices: usbipd list (runs on Windows, no admin needed)
# 2. Filter ROBOTIS devices by VID 2F5D (OpenRB-150 boards)
# 3. Attach to WSL: usbipd attach --wsl --busid X (no admin if policy set)
# 4. Discover serial paths inside WSL2:
#    wsl -- ls /dev/serial/by-id/ | grep ROBOTIS
# 5. Identify arms by running identify_arm.py inside open_manipulator container:
#    docker exec open_manipulator python3 /usr/local/bin/identify_arm.py /dev/serial/by-id/...
# 6. IMPORTANT: USB must be re-attached every time Docker Desktop restarts

# Camera workflow:
# 1. Attach webcam via usbipd (same as serial devices)
# 2. List video devices inside WSL2:
#    wsl -- bash -c "for d in /dev/video*; do v4l2-ctl --device=$d --info 2>/dev/null | grep -q 'Video Capture' && echo $d; done"
# 3. Present dropdown to student
# NOTE: v4l-utils is NOT in original Dockerfile, must be added to thin layer
```

**Critical Windows detail:** `usbipd attach` only works when Docker Desktop is running and WSL2 is active. The GUI must check this first.

#### `docker_manager.py` — Container Lifecycle

```python
# Startup sequence:
# 1. Check Docker Desktop is running: docker info (retry loop, up to 120s)
# 2. Check images exist: docker image inspect (if not, docker compose pull)
# 3. Generate .env from scanned devices
# 4. Detect GPU: nvidia-smi (if available, use gpu override)
# 5. Start: docker compose [-f gpu.yml] up -d
# 6. Health check: wait for all 3 containers "running"
# 7. Wait for web UI: HTTP GET http://localhost:80 (retry loop)
# 8. Open browser: webbrowser.open('http://localhost')

# Shutdown:
# docker compose down
```

#### `config_generator.py` — .env Generation

Writes the .env file with discovered device paths. Template:
```
FOLLOWER_PORT=/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_XXXX
LEADER_PORT=/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_YYYY
CAMERA_DEVICE=/dev/video0
CAMERA_NAME=camera1
ROS_DOMAIN_ID=30
```

#### `wsl_bridge.py` — WSL2 Command Execution

All WSL2 commands run via `subprocess.run(["wsl", "--", "bash", "-c", cmd])`. This module wraps that pattern with error handling and timeout.

### 2.3 GUI Flow

```
┌─────────────────────────────────────┐
│  ROBOTIS AI Setup                   │
│                                     │
│  Status: Checking Docker Desktop... │
│  [===========          ] 45%        │
│                                     │
│  ┌─── Step A: Leader Arm ─────────┐ │
│  │ Please plug in the LEADER arm  │ │
│  │ [Scan]  Found: OpenRB-150 ✓    │ │
│  └────────────────────────────────┘ │
│                                     │
│  ┌─── Step B: Follower Arm ───────┐ │
│  │ Please plug in the FOLLOWER    │ │
│  │ [Scan]  Found: OpenRB-150 ✓    │ │
│  └────────────────────────────────┘ │
│                                     │
│  ┌─── Step C: Camera ─────────────┐ │
│  │ Select camera: [Logitech C920▼]│ │
│  └────────────────────────────────┘ │
│                                     │
│  [  Start AI Environment  ]         │
│                                     │
│  ┌─── Log Output ─────────────────┐ │
│  │ Starting containers...          │ │
│  │ Waiting for web UI...           │ │
│  │ Opening browser...              │ │
│  └────────────────────────────────┘ │
└─────────────────────────────────────┘
```

### 2.4 Packaging

- Built with PyInstaller into single `.exe`
- `build.spec` configures: tkinter, subprocess, assets
- Desktop shortcut created by installer

---

## Component 3: Windows Installer (Inno Setup)

### 3.1 What the Installer Does (runs elevated)

1. **Check prerequisites:**
   - Windows 11 version ≥ 22H2
   - Virtualization enabled in BIOS (check `systeminfo`)

2. **Install WSL2** (if not present):
   ```powershell
   wsl --install --no-distribution
   # Requires reboot
   ```

3. **Install Docker Desktop** (if not present):
   - Download and run Docker Desktop installer silently
   - Enable WSL2 backend integration

4. **Install usbipd-win** (if not present):
   - Download from GitHub releases
   - Install via MSI

5. **Configure usbipd policy** (requires admin, which installer has):
   ```powershell
   # Allow ROBOTIS devices (VID 2F5D) to be attached without admin
   usbipd policy add --hardware-id "2F5D:*" --effect Allow
   ```
   If usbipd < 4.0 (no policy support), skip and GUI will prompt "Run as Administrator"

6. **Configure .wslconfig** (MERGE, not overwrite):
   ```powershell
   # Only set if not already configured:
   # [wsl2]
   # memory=8GB
   # swap=4GB
   # DO NOT set networkingMode=mirrored (Docker Desktop handles port forwarding)
   ```

7. **Pull Docker images** (with progress bar):
   ```powershell
   docker pull ${REGISTRY}/open-manipulator:latest
   docker pull ${REGISTRY}/physical-ai-server:latest
   docker pull ${REGISTRY}/physical-ai-manager:latest
   ```

8. **Install GUI files + docker-compose files** to `C:\Program Files\ROBOTIS AI\`

9. **Create desktop shortcut** → `Launch ROBOTIS AI.lnk`

### 3.2 Installer Scripts

| Script | Purpose |
|--------|---------|
| `install_prerequisites.ps1` | WSL2, Docker Desktop, usbipd |
| `configure_wsl.ps1` | Merge .wslconfig settings |
| `configure_usbipd.ps1` | Set up usbipd policy for ROBOTIS VID |
| `verify_system.ps1` | Post-install validation |
| `pull_images.ps1` | Pull Docker images with progress |

---

## Component 4: File Structure

```
robotis_ai_setup/
├── installer/
│   ├── robotis_ai_setup.iss          # Inno Setup script
│   ├── assets/
│   │   ├── icon.ico
│   │   └── license.txt
│   └── scripts/
│       ├── install_prerequisites.ps1
│       ├── configure_wsl.ps1
│       ├── configure_usbipd.ps1
│       ├── verify_system.ps1
│       └── pull_images.ps1
│
├── gui/
│   ├── main.py                       # Entry point
│   ├── build.spec                    # PyInstaller spec
│   ├── assets/
│   │   └── robotis_logo.png
│   └── app/
│       ├── __init__.py
│       ├── gui_app.py                # Main tkinter window
│       ├── device_manager.py         # USB scan, attach, identify
│       ├── docker_manager.py         # Container lifecycle
│       ├── config_generator.py       # .env file generation
│       ├── health_checker.py         # Container + web UI health
│       ├── wsl_bridge.py             # WSL2 command wrapper
│       └── constants.py              # Registry, ports, paths
│
├── docker/
│   ├── docker-compose.yml            # 3 services (see 1.1)
│   ├── docker-compose.gpu.yml        # GPU override (see 1.2)
│   ├── .env.template                 # Template for GUI
│   ├── build-images.sh               # Maintainer only (see 1.6)
│   └── open_manipulator/
│       ├── Dockerfile                # Thin layer (see 1.7)
│       ├── entrypoint_omx.sh         # Custom entrypoint (see 1.4)
│       └── identify_arm.py           # Arm identification (see 1.5)
│
└── tests/
    ├── test_device_manager.py
    ├── test_config_generator.py
    └── test_docker_manager.py
```

**What we DON'T ship (students never need):**
- physical_ai_server Dockerfile or s6 files (pre-built image)
- physical_ai_manager source or Dockerfile (pre-built image)
- Any ROS2 source code

---

## Critical Windows-Specific Concerns

### Concern 1: `network_mode: host` on Docker Desktop for Windows
Docker Desktop for Windows automatically forwards ports from WSL2 containers to `localhost` on Windows. With `network_mode: host`, services binding inside WSL2 are accessible from Windows browser at `http://localhost:80`, `:8080`, `:9090`. **Must be verified early in testing.**

### Concern 2: USB Passthrough via usbipd-win
- Serial devices (OpenRB-150 boards): Work via `usbipd attach --wsl`
- Webcams (UVC devices): Also need `usbipd attach --wsl`
- `/dev/serial/by-id/` paths are created by udev inside WSL2 when devices are attached
- Devices must be re-attached after every Docker Desktop restart or WSL2 shutdown
- usbipd 4.x+ `policy` mode eliminates the need for admin on each attach

### Concern 3: No X11 on Windows
The original docker-compose files mount X11 sockets and set DISPLAY. On Windows these don't exist. Our docker-compose removes all X11 configuration. The web UI at `http://localhost` is the only interface — RViz and Qt GUIs are not available (not needed for student workflow).

### Concern 4: GPU Availability
- Students WITH NVIDIA GPU: Use `docker-compose.gpu.yml` override for inference/training acceleration
- Students WITHOUT GPU: CPU-only mode works for recording data and basic inference (slower)
- Detection: `nvidia-smi` on Windows host (or `docker info | grep -i nvidia`)
- The physical_ai_server base image includes CUDA 12.8.0 + PyTorch 2.7.0 regardless — GPU override just enables access

### Concern 5: Camera Device Stability
`/dev/video*` indices can change across reboots. The GUI should:
1. Show device names (from `v4l2-ctl --info`) not just paths
2. Re-scan on each "Start" click
3. The thin-layer Dockerfile must add `v4l-utils` for `v4l2-ctl`

### Concern 6: Timezone Files in WSL2
`/etc/timezone` and `/etc/localtime` exist in Docker Desktop's WSL2 distro. The volume mounts should work. If not found, containers will use UTC (acceptable fallback).

---

## Implementation Order

### Step 1: Docker Images (maintainer machine, Linux)
1. Build open_manipulator thin layer Dockerfile (adds entrypoint + identify_arm.py + dynamixel-sdk)
2. Build or tag physical_ai_server and physical_ai_manager images
3. Push all 3 to container registry
4. Test: `docker compose up` on Linux with mock hardware (`use_mock_hardware:=true` in launch args)
5. Verify: `http://localhost:80` serves React UI, rosbridge at `:9090`, video server at `:8080`

### Step 2: Docker Desktop on Windows (no hardware)
1. Install Docker Desktop + WSL2 on a Windows 11 test machine
2. Pull the 3 images
3. Run `docker compose up` with mock hardware env
4. Verify from Windows browser: `http://localhost` shows React UI
5. Verify rosbridge WebSocket connects
6. **This validates the core networking assumption**

### Step 3: USB Passthrough (Windows + real hardware)
1. Install usbipd-win 4.x+
2. Connect OpenRB-150 boards
3. Test `usbipd list`, `usbipd attach --wsl`
4. Verify `/dev/serial/by-id/` paths appear in WSL2
5. Run `identify_arm.py` inside container — verify leader/follower detection
6. Test webcam passthrough similarly
7. Full entrypoint with real hardware — record a demonstration episode

### Step 4: GUI Application
1. Build tkinter app with device_manager, docker_manager, config_generator
2. Test USB scan → identify → start → browser workflow
3. Package with PyInstaller
4. Test on clean Windows 11 machine

### Step 5: Installer
1. Build Inno Setup installer with all prerequisite scripts
2. Test on clean Windows 11 VM (no Docker, no WSL)
3. Full E2E: install → scan → start → record data

---

## Verification Checklist

- [ ] `docker compose up` works on Linux with mock hardware
- [ ] All 3 containers healthy (`docker compose ps`)
- [ ] `http://localhost:80` serves React UI
- [ ] rosbridge WebSocket at `ws://localhost:9090` responds
- [ ] web_video_server at `http://localhost:8080` responds
- [ ] `/joint_states` topic from open_manipulator visible in physical_ai_server
- [ ] Same tests pass on Docker Desktop for Windows
- [ ] usbipd attach works for serial + webcam devices
- [ ] `identify_arm.py` correctly returns "leader" and "follower"
- [ ] entrypoint_omx.sh runs full sequence with real hardware
- [ ] GUI scans devices, generates .env, starts containers
- [ ] Browser opens automatically and shows connected robot status
- [ ] Recording a demonstration episode works end-to-end
- [ ] Clean Windows 11 install → installer → GUI → record

---

## Critical Source Files (read-only references)

| File | Why It Matters |
|------|---------------|
| `open_manipulator/open_manipulator_bringup/launch/omx_ai.launch.py` | Confirms port_name NOT forwarded to sub-launches |
| `open_manipulator/open_manipulator_bringup/launch/omx_f_follower_ai.launch.py` | Follower accepts `port_name` arg, default `/dev/ttyACM0` |
| `open_manipulator/open_manipulator_bringup/launch/omx_l_leader_ai.launch.py` | Leader accepts `port_name` arg, default `/dev/ttyACM2` |
| `open_manipulator/open_manipulator_bringup/launch/camera_usb_cam.launch.py` | Camera accepts `name` + `video_device` args |
| `open_manipulator/open_manipulator_bringup/config/omx_f_follower_ai/initial_positions.yaml` | 2 steps, 6 joints, 5s duration, 0.15 epsilon |
| `open_manipulator/open_manipulator_bringup/open_manipulator_bringup/joint_trajectory_executor.py` | Auto-exits after all steps complete |
| `open_manipulator/open_manipulator_description/ros2_control/omx_f.ros2_control.xacro` | Follower servo IDs 11-16, baudrate 1M |
| `open_manipulator/open_manipulator_description/ros2_control/omx_l.ros2_control.xacro` | Leader servo IDs 1-6, baudrate 1M |
| `open_manipulator/docker/Dockerfile` | Base image: `ros:jazzy-ros-base`, installs DynamixelSDK etc. |
| `open_manipulator/docker/docker-compose.yml` | Reference: `ipc: host`, `pid: host`, `privileged: true` |
| `physical_ai_tools/physical_ai_server/Dockerfile.amd64` | Base: `robotis/ros:jazzy-ros-base-torch2.7.0-cuda12.8.0`, s6-overlay |
| `physical_ai_tools/docker/docker-compose.yml` | Reference: agent socket, timezone, huggingface cache volumes |
| `physical_ai_tools/docker/s6-agent/` | Talos agent service (FastAPI on Unix socket) |
| `physical_ai_tools/docker/s6-services/` | physical_ai_server service + logging pipeline |
| `physical_ai_tools/docker/s6-services/common/ros2_service_run.sh` | ROS2 env setup: domain ID, PYTHONPATH, source workspace |
| `physical_ai_tools/physical_ai_server/config/omx_f_config.yaml` | Camera topic: `/camera1/image_raw/compressed`, joint topics |
| `physical_ai_tools/physical_ai_server/launch/physical_ai_server_bringup.launch.py` | Launches: server + rosbridge(9090) + web_video_server(8080) + rosbag_recorder |
| `physical_ai_tools/physical_ai_manager/nginx.conf` | `listen 80`, serves React SPA |
| `physical_ai_tools/physical_ai_manager/src/utils/rosConnectionManager.js` | Connects to `ws://${hostname}:9090` |
| `physical_ai_tools/physical_ai_manager/src/features/ros/rosSlice.js` | Hardcodes port 9090: `ws://${host}:9090` |

---

## Student Workflow & What Happens Behind the Scenes

### One-Time: Installation

**What the student does:**
1. Downloads `Robotis_AI_Setup.exe` and runs it
2. Clicks "Next" through the installer wizard
3. Waits for the progress bar to complete (may require one reboot for WSL2)
4. A desktop icon "Launch ROBOTIS AI" appears

**What happens in the background:**
1. Installer checks Windows 11 version and virtualization support
2. Installs WSL2 (`wsl --install --no-distribution`) if missing — may trigger reboot
3. Silently installs Docker Desktop with WSL2 backend if missing
4. Installs usbipd-win 4.x+ (USB passthrough driver) if missing
5. Configures usbipd policy: `usbipd policy add --hardware-id "2F5D:*" --effect Allow` — this allows ROBOTIS USB devices to be attached to WSL2 without admin rights in the future
6. Merges `.wslconfig` with recommended memory/swap settings (does NOT set mirrored networking — Docker Desktop manages its own port forwarding)
7. Pulls 3 Docker images (~15-20 GB total):
   - `open-manipulator:latest` (ROS2 + hardware drivers)
   - `physical-ai-server:latest` (ROS2 + PyTorch + AI server + s6-overlay)
   - `physical-ai-manager:latest` (nginx + React web UI, ~30 MB)
8. Installs GUI app + docker-compose files to `C:\Program Files\ROBOTIS AI\`
9. Creates desktop shortcut

### Every Session: Hardware Setup + Launch

**What the student does:**
1. Plugs in the Leader arm via USB
2. Plugs in the Follower arm via USB
3. Plugs in webcam
4. Double-clicks "Launch ROBOTIS AI" desktop icon
5. GUI opens. Clicks **[Scan]** for Leader — sees "OpenRB-150 (Leader) found"
6. Clicks **[Scan]** for Follower — sees "OpenRB-150 (Follower) found"
7. Selects camera from dropdown (e.g., "Logitech C920")
8. Clicks **[Start AI Environment]**
9. Waits ~10 seconds (or ~2 min first time for image extraction)
10. Browser automatically opens at `http://localhost` — the Physical AI Web UI is ready
11. Student records demonstrations, trains models, runs inference via the web UI

**What happens behind the scenes (step by step):**

**When the GUI opens (steps 5-7):**
1. GUI runs `docker info` to verify Docker Desktop is running (retries up to 120s if Docker is still starting)
2. On "Scan Leader": GUI runs `usbipd list` on Windows to find ROBOTIS devices (VID `2F5D`)
3. GUI runs `usbipd attach --wsl --busid X` to passthrough the USB device from Windows into the WSL2 Linux kernel
4. Inside WSL2, the device appears at `/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_XXXX`
5. GUI starts a temporary open_manipulator container and runs `identify_arm.py` inside it:
   - Opens the serial port at 1 Mbps, Dynamixel Protocol 2.0
   - Pings servo IDs 1-6 (leader) and 11-16 (follower)
   - Returns "leader" or "follower" based on which IDs respond
6. Same process for the second arm — GUI now knows which USB path is leader and which is follower
7. Camera scan: attaches webcam via usbipd, then lists `/dev/video*` devices inside WSL2 using `v4l2-ctl` to show friendly names

**When "Start AI Environment" is clicked (steps 8-10):**
1. GUI generates `.env` file with discovered device paths:
   ```
   FOLLOWER_PORT=/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_Follower456
   LEADER_PORT=/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_Leader123
   CAMERA_DEVICE=/dev/video0
   CAMERA_NAME=camera1
   ROS_DOMAIN_ID=30
   ```
2. GUI detects GPU availability (`nvidia-smi` on Windows)
3. GUI runs `docker compose up -d` (with GPU override file if GPU detected)
4. **Three containers start simultaneously inside Docker Desktop's WSL2 VM:**

   **Container 1: `open_manipulator`** (custom `entrypoint_omx.sh`):
   - Sources ROS2 Jazzy environment
   - Waits up to 30s for follower USB device to appear (timing buffer for usbipd attach)
   - Waits up to 30s for leader USB device to appear
   - **Phase 1:** Launches follower arm controller (`omx_f_follower_ai.launch.py port_name:=/dev/serial/by-id/...`)
     - ros2_control_node opens serial port, connects to Dynamixel servos (IDs 11-16)
     - Spawns `arm_controller` (trajectory following) and `joint_state_broadcaster`
     - The follower's arm_controller subscribes to `/leader/joint_trajectory` (remapped from `/arm_controller/joint_trajectory`)
   - Waits for `/joint_states` topic to appear (confirms hardware is connected and publishing)
   - **Phase 2:** Runs `joint_trajectory_executor` — moves follower to "ready" position:
     - Step 1: all joints to [0, 0, 0, 0, 0, 0] over 5 seconds (home)
     - Step 2: joints to [0, -1.57, 1.57, 1.57, 0, 0] over 5 seconds (ready pose)
     - Uses quintic polynomial interpolation (100 points per trajectory)
     - Auto-exits after both steps complete (`sys.exit(0)`)
   - **Phase 3:** Launches leader arm controller (`omx_l_leader_ai.launch.py port_name:=/dev/serial/by-id/...`)
     - All nodes in `/leader/` namespace
     - ros2_control_node connects to leader servos (IDs 1-6)
     - Spawns `joint_state_broadcaster`, `trigger_position_controller`, `joint_trajectory_command_broadcaster`
     - After controllers are spawned, publishes trigger value (-0.7) to `/leader/trigger_position_controller/commands` at 50Hz for 50 iterations — this enables the leader's position broadcasting
     - `joint_trajectory_command_broadcaster` begins publishing leader joint positions to `/leader/joint_trajectory`
     - The follower's arm_controller receives these and mirrors the leader's movements in real-time
   - **Phase 4:** Launches USB camera (`camera_usb_cam.launch.py name:=camera1 video_device:=/dev/video0`)
     - Publishes compressed images to `/camera1/image_raw/compressed`

   **Container 2: `physical_ai_server`** (s6-overlay `/init`):
   - s6-overlay init starts two managed services:
     - `s6-agent`: FastAPI server (talos_system_manager) on Unix socket `/var/run/agent/s6_agent.sock`
     - `physical_ai_server`: Runs `ros2 launch physical_ai_server physical_ai_server_bringup.launch.py`
   - The ROS2 launch file starts 4 nodes:
     - `physical_ai_server` node — the core AI server, reads `omx_f_config.yaml`, subscribes to `/joint_states`, `/leader/joint_trajectory`, `/camera1/image_raw/compressed`
     - `rosbridge_websocket` on port 9090 — bridges ROS2 topics to WebSocket for the browser
     - `web_video_server` on port 8080 — serves camera video as MJPEG HTTP stream
     - `service_bag_recorder` — records ROS2 bags on command (for collecting demonstration data)
   - The AI server waits for topics with a 5-second timeout, warns but doesn't crash if open_manipulator isn't ready yet
   - All inter-container communication via ROS2 DDS multicast on shared `network_mode: host` network (ROS_DOMAIN_ID=30)

   **Container 3: `physical_ai_manager`** (nginx):
   - nginx starts on port 80, serving the pre-built React SPA
   - Simplest container — just static file serving

5. GUI polls `http://localhost:80` until it responds (typically 5-15 seconds)
6. GUI opens the student's default browser to `http://localhost`
7. The React app loads, reads `window.location.hostname` (= `localhost`), constructs `ws://localhost:9090`, connects to rosbridge
8. The web UI shows connected robot status, camera feed, and recording controls

**Data flow during a recording session:**
```
Leader arm (physical) → servos → /leader/joint_trajectory → follower arm mirrors movements
                                                          → physical_ai_server records
Camera (physical) → /camera1/image_raw/compressed → web_video_server → browser (MJPEG)
                                                  → physical_ai_server records
Student clicks "Record" → service_bag_recorder saves all topics to rosbag files
Student clicks "Train" → physical_ai_server runs LeRobot/SmolVLA training on recorded data
Student clicks "Inference" → physical_ai_server sends commands to /arm_controller/follow_joint_trajectory → follower moves autonomously
```

### Shutdown

**What the student does:** Clicks "Stop" in the GUI, or closes the GUI

**What happens:**
1. GUI runs `docker compose down`
2. open_manipulator receives SIGTERM → `entrypoint_omx.sh` trap sends SIGTERM to follower, leader, camera processes → clean shutdown
3. physical_ai_server's s6-overlay gracefully terminates managed services (30s timeout, then SIGKILL)
4. physical_ai_manager (nginx) stops immediately
5. All containers removed, named volumes preserved (workspace data + huggingface cache survive restarts)
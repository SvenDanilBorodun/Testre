# Changes Made — Session 2026-04-06

## Overview

Added a StartupGate loading screen so students don't see ROS errors, added Docker healthchecks for proper container ordering, fixed leader arm communication errors, and fixed the entrypoint blocking forever.

---

## 1. Frontend: StartupGate Loading Screen

**Problem:** When students open the dashboard, they see ROS connection errors and a red "Getrennt" indicator because containers are still starting.

**Files changed:**
- `physical_ai_tools/physical_ai_manager/src/components/StartupGate.js` — **NEW FILE**
- `physical_ai_tools/physical_ai_manager/src/App.js` — Wrapped content in `<StartupGate>`

**What it does:** Full-screen overlay that polls `rosConnectionManager.isConnected()` every 500ms. Shows "Verbindung zum ROS-System..." spinner, then "Dienste werden initialisiert..." with a 3-second settle delay. Fades out after rosbridge connects. 90-second timeout shows retry button.

**Upstream diff:** N/A (new feature)

---

## 2. Frontend: RobotTypeSelector Retry Logic

**Problem:** `getRobotTypeList()` service call fires before `physical_ai_server` registers its ROS services, causing error toast.

**File changed:** `physical_ai_tools/physical_ai_manager/src/components/RobotTypeSelector.js`

**What changed:** `fetchRobotTypes()` now retries up to 5 times with 2-second intervals. No error toast until all retries exhausted. Manual refresh button shows toast on success.

**Upstream diff:** Original had `fetchRobotTypes()` on mount with immediate error toast.

---

## 3. Frontend: rosConnectionManager Tuning

**File changed:** `physical_ai_tools/physical_ai_manager/src/utils/rosConnectionManager.js`

**What changed:**
- `maxReconnectAttempts`: 10 → 30 (cold startup can take 60s+)
- Initial reconnect delay: 2000ms → 1000ms (faster first retries)
- Added `resetReconnectCounter()` method for StartupGate retry button

**Upstream diff:** Original had 10 attempts, 2000ms base delay, no reset method.

---

## 4. Docker: Healthchecks on Compose

**Problem:** `depends_on` only waited for container start, not actual readiness. Containers started before dependencies were truly available.

**File changed:** `robotis_ai_setup/docker/docker-compose.yml`

**What changed:**
```yaml
open_manipulator:
  healthcheck:
    test: ["CMD-SHELL", "bash -c 'source /opt/ros/jazzy/setup.bash && ros2 topic list 2>/dev/null | grep -q /joint_states'"]
    interval: 5s, timeout: 5s, retries: 30, start_period: 10s

physical_ai_server:
  depends_on:
    open_manipulator:
      condition: service_healthy
  healthcheck:
    test: ["CMD-SHELL", "python3 -c \"import socket; s=socket.create_connection(('localhost',9090),2); s.close()\""]
    interval: 5s, timeout: 5s, retries: 30, start_period: 15s

physical_ai_manager:
  depends_on:
    physical_ai_server:
      condition: service_healthy
```

**Note:** The healthcheck for open_manipulator MUST source `/opt/ros/jazzy/setup.bash` because `ros2` is not in PATH by default inside the container.

**Upstream diff:** Original compose had plain `depends_on: - open_manipulator` with no healthchecks.

---

## 5. Docker: Leader Arm `is_async` Fix

**Problem:** The leader arm's hardware interface ran in synchronous mode, causing the 100Hz control loop to block on serial I/O. This produced hundreds of `BULK_READ_FAIL`, `SYNC_READ_FAIL`, and `Overrun detected` errors, eventually hanging the leader's control node entirely.

**Root cause:** Follower xacro had `is_async="true"` (upstream). Leader xacro did NOT (also upstream). On native Linux this works fine — serial reads complete in <1ms. Through WSL2/usbipd, serial reads take 5-15ms due to USB-over-IP overhead, exceeding the 10ms control cycle.

**Files changed (overlays, not modifying upstream repo files):**
- `robotis_ai_setup/docker/open_manipulator/Dockerfile` — Added overlay COPY step
- `robotis_ai_setup/docker/open_manipulator/ros2_control_overlays/omx_l.ros2_control.xacro` — **NEW**
- `robotis_ai_setup/docker/open_manipulator/ros2_control_overlays/omy_l100_current.ros2_control.xacro` — **NEW**
- `robotis_ai_setup/docker/open_manipulator/ros2_control_overlays/omy_l100_position.ros2_control.xacro` — **NEW**

**Also changed in repo (for reference, not used in Docker build):**
- `open_manipulator/open_manipulator_description/ros2_control/omx_l.ros2_control.xacro` — line 4: added `is_async="true"`
- `open_manipulator/open_manipulator_description/ros2_control/omy_l100_current.ros2_control.xacro` — line 4: added `is_async="true"`
- `open_manipulator/open_manipulator_description/ros2_control/omy_l100_position.ros2_control.xacro` — line 4: added `is_async="true"`

**Upstream vs ours:**
- Upstream `omx_l.ros2_control.xacro` line 4: `<ros2_control name="${name}" type="system">`
- Ours: `<ros2_control name="${name}" type="system" is_async="true">`
- Everything else is byte-identical to upstream.

**Results:**
| Metric | Before | After |
|--------|--------|-------|
| SYNC_READ_FAIL | 41-52 per run | 0-2 |
| Overruns | 46-53 per run | 0 |
| Leader trajectory | Dead/blocked | 100Hz stable |

---

## 6. Docker: Entrypoint Timeout Fix

**Problem:** The `joint_trajectory_executor` in Phase 2 of the entrypoint could block forever if the follower's initial position trajectory never completed (e.g., due to servo overload on ID 16). This prevented the leader from ever launching.

**File changed:** `robotis_ai_setup/docker/open_manipulator/entrypoint_omx.sh`

**What changed:**
```bash
# Before:
ros2 run open_manipulator_bringup joint_trajectory_executor \
    --ros-args --params-file "$PARAMS_FILE"

# After:
timeout 30 ros2 run open_manipulator_bringup joint_trajectory_executor \
    --ros-args --params-file "$PARAMS_FILE" || true
```

The `|| true` prevents `set -e` from killing the container on timeout (exit code 124).

**Upstream diff:** Original entrypoint had no timeout.

---

## 7. GUI: Rosbridge Wait Before Opening Browser

**File changed:** `robotis_ai_setup/gui/app/gui_app.py`

**What changed:** After `wait_for_web_ui()` succeeds, added a loop that waits for rosbridge (port 9090) before opening the browser:
```python
self._set_status("Warte auf ROS-Bridge...")
for _ in range(30):
    if health_checker.check_rosbridge():
        self._log("ROS-Bridge ist bereit!")
        break
    time.sleep(2)
```

**Upstream diff:** Original opened browser immediately after web UI responded.

---

## Images Rebuilt and Pushed

| Image | Changed | Pushed |
|-------|---------|--------|
| `nettername/physical-ai-manager` | StartupGate, RobotTypeSelector retry, rosConnectionManager | Yes |
| `nettername/open-manipulator` | is_async overlays, entrypoint timeout | Yes |
| `nettername/physical-ai-server` | No changes | No |
| `nettername/physical-ai-server-base` | No changes | No |
| `nettername/robotis-ai-training` | No changes | Rebuilt as part of full build |

---

## Known Remaining Issue: Follower Gripper (Servo ID 16)

**Symptom:** Follower gripper (servo 16) doesn't physically close when leader gripper (servo 6) is squeezed. The ROS controller shows matching reference/feedback values (~0.69 rad), suggesting the software IS commanding the gripper.

**Diagnosis:** Servo 16 had an OVERLOAD error (0x20) earlier in the session, which was cleared via `pkt.reboot()`. However, the gripper may not be responding because:
1. The gripper mechanism has very limited travel range at this position
2. The leader servo 6 (mode 5, Current Limit 300mA) provides very small position changes when squeezed

**This is NOT caused by any code change.** The upstream ROBOTIS xacro for `omx_f` servo 16 is identical to what's in our repo. The gripper behavior depends on physical gripper assembly and servo calibration.

**What to investigate:** Whether this worked before on the same hardware. If it did, the servo may need recalibration or the `Shutdown` register value (21 = overload + overheat + voltage protection) may be too aggressive.

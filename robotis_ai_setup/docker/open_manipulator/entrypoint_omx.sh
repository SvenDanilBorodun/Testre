#!/bin/bash
set -e

# Classroom-Jetson short-circuit: when this container runs on the shared
# classroom Jetson, only the follower arm is physically connected (the
# leader stays at the student's desk for recording). Setting
# EDUBOTICS_FOLLOWER_ONLY=1 skips the leader port wait, the leader
# launch, the leader-pose read, and the quintic sync — the Jetson agent
# moves the follower to a safe home pose itself once the container is
# healthy.
FOLLOWER_ONLY="${EDUBOTICS_FOLLOWER_ONLY:-0}"

# Set up signal handling early — before any background processes are launched
PIDS=""
disable_torque() {
    # Best-effort: tell the Dynamixel hardware interface to drop torque so
    # the arm doesn't fall under gravity when our ROS nodes die. Both arms
    # expose set_dxl_torque services; try follower first, then leader.
    # 2s timeout each so we never block shutdown.
    echo "[SHUTDOWN] Disabling servo torque..."
    # Audit H1: log failures explicitly so a maintainer can distinguish
    # "service unreachable / 404" (catastrophic — arm stays torqued, will
    # slump under gravity once power loss removes holding torque) from
    # "torque actually dropped". Bare `|| true` swallowed every failure.
    if ! timeout 2 ros2 service call /dynamixel_hardware_interface/set_dxl_torque \
        std_srvs/srv/SetBool "{data: false}" >/dev/null 2>&1; then
        echo "[WARNUNG] Follower-Torque-Abschaltung fehlgeschlagen — Arm bleibt unter Strom"
    fi
    # Leader namespace pushes `leader/` and the xacro's `set_dxl_torque_srv_name`
    # parameter is `omx_l/set_dxl_torque` — resolved leader path is
    # `/leader/omx_l/set_dxl_torque`. Previously called the follower-style path
    # under `/leader/...`, which silently 404'd and left the leader torqued.
    if [ "$FOLLOWER_ONLY" = "1" ]; then
        echo "[SHUTDOWN] FOLLOWER_ONLY=1 — leader torque-disable skipped (no leader connected)."
    elif ! timeout 2 ros2 service call /leader/omx_l/set_dxl_torque \
        std_srvs/srv/SetBool "{data: false}" >/dev/null 2>&1; then
        echo "[WARNUNG] Leader-Torque-Abschaltung fehlgeschlagen — Arm bleibt unter Strom"
    fi
}
CLEANUP_DONE=0
cleanup() {
    # Idempotent — `trap ... EXIT` plus a SIGTERM both want to run this,
    # but disable_torque calling a ROS service after rclpy has been torn
    # down emits noisy errors. Sentinel guards against the double-run.
    if [ "$CLEANUP_DONE" = "1" ]; then
        return
    fi
    CLEANUP_DONE=1
    echo "[SHUTDOWN] Stopping all processes..."
    disable_torque
    for pid in $PIDS; do
        kill "$pid" 2>/dev/null
    done
    wait
    echo "[SHUTDOWN] Done."
}
# Audit E2: EXIT is mandatory — `set -e` aborts (wait_for_device 60s
# miss, sync-verifier exit 2, any other set-e fallthrough) take the
# script down via `exit`, NOT via a signal, so SIGTERM/SIGINT alone
# would have left both arms torqued while the container teardown
# proceeded. EXIT runs after every shell exit path.
trap cleanup SIGTERM SIGINT EXIT

source /opt/ros/jazzy/setup.bash
source /root/ros2_ws/install/setup.bash
export ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-30}

# --- Host USB-bridge detection for camera pixel-format default ---
# The same physical_ai_server / open_manipulator image runs on two
# wildly different USB host stacks:
#
#   1. Windows 11 student PC → bundled WSL2 distro → kernel-mode
#      `vhci_hcd` driver forwards usbipd-attached devices. The bridge
#      cannot sustain a 18.4 MB/s uncompressed YUYV stream from TWO
#      Innomaker U20CAM-720P (640×480×30 each = 36.8 MB/s combined);
#      both cameras crash within ~5 s with `VIDIOC_DQBUF: Select
#      timeout` and the container restarts. Verified empirically
#      2026-05-22 on Sven's classroom rig. The compressed-on-wire
#      `mjpeg2rgb` mode keeps each stream at ~1 MB/s (the camera
#      hardware encodes JPEG before the USB transfer; usb_cam decodes
#      to RGB on the CPU) which the vhci_hcd bridge handles fine. CPU
#      cost is ~30 % per camera at 30 Hz, well within budget on the
#      i5-class student PCs.
#
#   2. Classroom Jetson Orin Nano → NATIVE USB host controller (no
#      WSL bridge). The wire bandwidth is real USB 2.0 isoch, ~25 MB/s
#      per controller. Two yuyv cameras still fit (18.4 MB/s × 2 fits
#      across two controllers; usb_cam does no decode → minimal CPU).
#      mjpeg2rgb would burn ~60 % of one ARM core for no benefit.
#
# We therefore default to `mjpeg2rgb` on WSL2 and `yuyv` on real
# hardware (Jetson, or any non-WSL Linux). The override path is
# unchanged: setting EDUBOTICS_CAMERA_PIXEL_FORMAT in the compose env
# (forwarded through docker-compose.yml::environment) wins.
#
# Detection method: Microsoft's WSL2 kernel reports a build string
# containing `microsoft` (case-insensitive). `uname -r` examples:
#   WSL2:   "5.15.167.4-microsoft-standard-WSL2"
#   Jetson: "5.15.148-tegra"
# We grep case-insensitive to defend against future Microsoft kernel
# string drift (e.g. "Microsoft" capitalised). Falls back to the
# compose default (yuyv) if uname fails for any reason.
detect_host_usb_bridge() {
    if uname -r 2>/dev/null | grep -iq 'microsoft'; then
        echo "vhci_hcd"
    else
        echo "native"
    fi
}
EDUBOTICS_HOST_USB_BRIDGE="${EDUBOTICS_HOST_USB_BRIDGE:-$(detect_host_usb_bridge)}"
export EDUBOTICS_HOST_USB_BRIDGE

# --- Camera source selection: native_bridge vs usb_cam ---
# native_bridge: the cameras are NOT attached to this distro via usbipd.
#   The Windows GUI captures them natively (full 30 fps) and streams JPEG
#   frames over localhost TCP to camera_ingest_node.py, which republishes
#   them as CompressedImage on /<name>/image_raw/compressed. This is the
#   WSL2 student path — the vhci_hcd USB/IP bridge caps in-container UVC
#   capture at ~6-10 Hz per camera (per-device isochronous latency,
#   benchmarked 2026-05-23) and that traffic also jitters the 100 Hz
#   Dynamixel reads. Moving cameras off the bridge fixes both.
# usb_cam: in-container V4L2 capture (Jetson Orin Nano / native Linux —
#   real USB host, no bridge). Preserves the existing behaviour exactly,
#   including the mjpeg2rgb-vs-yuyv split below.
# Default follows the detected host bridge (WSL2 ⇒ native_bridge). An
# explicit EDUBOTICS_CAMERA_SOURCE in the compose env always wins and is
# the one-variable rollback to the old usbipd camera path.
if [ -z "${EDUBOTICS_CAMERA_SOURCE:-}" ]; then
    if [ "$EDUBOTICS_HOST_USB_BRIDGE" = "vhci_hcd" ]; then
        EDUBOTICS_CAMERA_SOURCE="native_bridge"
    else
        EDUBOTICS_CAMERA_SOURCE="usb_cam"
    fi
fi
export EDUBOTICS_CAMERA_SOURCE
echo "[INIT] Camera source: ${EDUBOTICS_CAMERA_SOURCE} (host USB bridge: ${EDUBOTICS_HOST_USB_BRIDGE})"

# The mjpeg2rgb-vs-yuyv pixel-format split only matters for the usb_cam
# path (native_bridge re-encodes JPEG on the Windows host and never touches
# usb_cam). Apply the WSL2 default ONLY if the operator hasn't overridden it
# explicitly. Compose's `EDUBOTICS_CAMERA_PIXEL_FORMAT=${...:-yuyv}` always
# sets the var to something non-empty, so we compare against the bare default
# (`yuyv`) rather than `-z`. An operator .env override wins.
if [ "$EDUBOTICS_CAMERA_SOURCE" = "usb_cam" ] && \
   [ "$EDUBOTICS_HOST_USB_BRIDGE" = "vhci_hcd" ] && \
   [ "${EDUBOTICS_CAMERA_PIXEL_FORMAT:-}" = "yuyv" ]; then
    echo "[INIT] WSL2 detected (uname -r matches 'microsoft') — switching pixel_format default yuyv → mjpeg2rgb."
    echo "[INIT] Rationale: WSL2 vhci_hcd bandwidth cannot sustain 2×YUYV at 640x480x30."
    EDUBOTICS_CAMERA_PIXEL_FORMAT="mjpeg2rgb"
    export EDUBOTICS_CAMERA_PIXEL_FORMAT
fi

echo "========================================"
echo "ROBOTIS Open Manipulator - AI Mode"
echo "Follower: ${FOLLOWER_PORT}"
if [ "$FOLLOWER_ONLY" = "1" ]; then
    echo "Leader:   <skipped — EDUBOTICS_FOLLOWER_ONLY=1>"
else
    echo "Leader:   ${LEADER_PORT}"
fi
echo "Camera 1: ${CAMERA_DEVICE_1:-<none>} as ${CAMERA_NAME_1:-gripper}"
echo "Camera 2: ${CAMERA_DEVICE_2:-<none>} as ${CAMERA_NAME_2:-scene}"
echo "========================================"

# --- Validate hardware (with retry for USB attach timing) ---
# 60s is generous enough for slow USB hubs and in-flight `usbipd attach` from
# the Windows host; below that we were occasionally racing the enumeration.
wait_for_device() {
    local device=$1 label=$2 max_wait=60 count=0
    while [ ! -e "$device" ] && [ $count -lt $max_wait ]; do
        echo "[INIT] Waiting for $label ($device)... ${count}s"
        sleep 1
        count=$((count + 1))
    done
    if [ ! -e "$device" ]; then
        echo "[ERROR] $label not found after ${max_wait}s: $device"
        echo "[ERROR] Check usbipd attach on the Windows host, then restart."
        exit 1
    fi
    chmod 666 "$device" 2>/dev/null || true
    echo "[INIT] $label found: $device"
}

wait_for_device "$FOLLOWER_PORT" "Follower arm"

if [ "$FOLLOWER_ONLY" = "1" ]; then
    echo "[LAUNCH] FOLLOWER_ONLY=1 — skipping leader port wait and leader launch."
else
    wait_for_device "$LEADER_PORT" "Leader arm"

    # --- Phase 1: Launch Leader FIRST ---
    # Leader must start first so we know its position before the follower moves.
    echo "[LAUNCH] Starting leader..."
    ros2 launch open_manipulator_bringup omx_l_leader_ai.launch.py \
        port_name:=${LEADER_PORT} &
    PIDS="$!"

    # Wait for leader joint states
    count=0
    while ! ros2 topic list 2>/dev/null | grep -q "/leader/joint_states" && [ $count -lt 30 ]; do
        sleep 1
        count=$((count + 1))
    done
    sleep 2
    echo "[LAUNCH] Leader ready."
    # NOTE: the leader-pose read used to happen here, feeding the boot-time
    # quintic sync below. Both moved into arm_startup_node.py (Phase 3') so
    # the follower stays LIMP until the student clicks "Roboter starten" in
    # the dashboard. The node reads the leader's CURRENT pose at click time
    # (the student may reposition the leader after boot) and verifies the
    # sync as a BLOCKING gate before the arm reports ready. See the script
    # header for the full rationale.
fi

# --- Phase 2: Launch Follower ---
echo "[LAUNCH] Starting follower..."
ros2 launch open_manipulator_bringup omx_f_follower_ai.launch.py \
    port_name:=${FOLLOWER_PORT} &
PIDS="$PIDS $!"

# Wait for follower to be ready
count=0
while ! ros2 topic list 2>/dev/null | grep -q "/joint_states" && [ $count -lt 60 ]; do
    sleep 1
    count=$((count + 1))
done
echo "[LAUNCH] Follower ready (/joint_states detected)."
# Wait for arm_controller to be fully active
sleep 3

# --- Phase 3': On-demand homing + leader-sync (deferred to a button) ---
# The follower stays LIMP (torque off, no motion) at boot. arm_startup_node.py
# advertises /edubotics/start_arm (std_srvs/Trigger) and publishes progress on
# /edubotics/arm_state; when the student clicks "Roboter starten" in the
# dashboard it homes the follower, reads the leader's CURRENT pose, syncs, and
# VERIFIES convergence as a blocking gate before reporting ready. This replaces
# the old boot-time quintic sync (which soft-failed and let students record on
# a mis-synced arm). On the classroom Jetson there is no leader to sync to, so
# the node is NOT started — the Jetson agent homes the follower itself, exactly
# as before.
if [ "$FOLLOWER_ONLY" = "1" ]; then
    echo "[LAUNCH] FOLLOWER_ONLY=1 — arm_startup_node skipped. Jetson agent homes the follower after the container is healthy."
else
    echo "[LAUNCH] Starting arm_startup_node (follower limp until /edubotics/start_arm)..."
    python3 /usr/local/bin/arm_startup_node.py &
    PIDS="$PIDS $!"
fi

# --- Phase 4: Launch Cameras ---
if [ "$EDUBOTICS_CAMERA_SOURCE" = "native_bridge" ]; then
    # Native-bridge path (WSL2 student PC): the cameras are NOT attached to
    # this distro. The Windows GUI captures them natively at 30 fps and
    # streams JPEG frames over localhost TCP to camera_ingest_node.py, which
    # republishes them as CompressedImage on /<name>/image_raw/compressed.
    # No usbipd cameras, no usb_cam, no /dev/video* here.
    echo "[LAUNCH] Native camera bridge mode — starting camera_ingest_node.py (TCP :${EDUBOTICS_CAMERA_INGEST_PORT:-5557})..."
    python3 /usr/local/bin/camera_ingest_node.py &
    PIDS="$PIDS $!"
    echo "[LAUNCH] Camera ingest running — awaiting JPEG frames from the Windows GUI."
else
# --- usb_cam path (Jetson Orin Nano / native Linux): in-container V4L2 ---
#
# Audit F21: a single `[ -e $device ]` check at the top would race
# usbipd's WSL forwarding on cold boot — the test fails, [WARN] is
# logged, and the container proceeds WITHOUT cameras. Mirror the
# arm-side wait_for_device by polling for the camera node briefly
# before giving up. 30 s matches the existing arm waits.
wait_for_camera() {
    local dev="$1" name="$2" timeout="${3:-30}" t=0
    while [ ! -e "$dev" ] && [ "$t" -lt "$timeout" ]; do
        sleep 1
        t=$((t + 1))
    done
    if [ -e "$dev" ]; then
        return 0
    fi
    echo "[WARN] Camera $name ($dev) not present after ${timeout}s"
    return 1
}

# Audit Gap-D 2026-05-23: refuse to launch when two configured cameras
# resolve to the same underlying /dev/videoN. The Innomaker U20CAM-720P
# pair both report USB serial "SN0001" and udev creates one shared
# `/dev/v4l/by-id/usb-Innomaker_..._SN0001-...` symlink — pointing at
# whichever device enumerated last. The GUI's wsl_bridge.list_video_devices
# already de-dups by Bus info to avoid handing the same by-id path to both
# camera slots, but a stale .env from a prior install (or a manual edit)
# can still encode the colliding path. Worst-case: the student records
# 100 episodes with the gripper feed labelled as "scene" and the trained
# policy then drives the wrong camera at inference time → silent corpus
# rot.
#
# Detection happens AFTER readlink resolution (below) so we compare the
# real `/dev/videoN`, not the symlink names. If a collision is detected
# we hard-exit with a German [STOPP] message — failing loud matches Rule
# §3 ("overlays must fail loudly on missing target") and is consistent
# with the post-sync verification hard-exit at the top of this script.
declare -A SEEN_REAL_DEV   # bash 4 associative array, real_dev → "i:name"
for i in 1 2; do
    device_var="CAMERA_DEVICE_$i"
    name_var="CAMERA_NAME_$i"
    device="${!device_var}"
    default_names=("gripper" "scene")
    name="${!name_var:-${default_names[$((i-1))]}}"

    if [ -z "$device" ]; then
        continue
    fi
    if ! wait_for_camera "$device" "$name" 30; then
        # Camera path never appeared (usbipd not forwarding, driver
        # crash, replug mid-boot). Skip — the new compose healthcheck
        # (audit F7) will report the container unhealthy if a
        # configured camera is missing.
        continue
    fi
    # Resolve /dev/v4l/by-id/* symlinks to the underlying /dev/videoN.
    # usb_cam's V4L2 wrapper strips the path components naively and
    # ends up trying to open `/dev/../../video2`, which fails with
    # "Device specified is not available or is not a valid V4L2 device".
    # The GUI writes by-id paths into .env when a camera exposes a USB
    # serial (stable across replug); the resolution stays per-launch.
    real_dev="$device"
    if [ -L "$device" ]; then
        resolved="$(readlink -f "$device" 2>/dev/null || true)"
        if [ -e "$resolved" ]; then
            echo "[LAUNCH] Camera $i: resolved $device → $resolved"
            device="$resolved"
            real_dev="$resolved"
        fi
    fi
    # Audit Gap-D collision check — see the block above this loop.
    if [ -n "${SEEN_REAL_DEV[$real_dev]:-}" ]; then
        prev="${SEEN_REAL_DEV[$real_dev]}"
        echo "[STOPP] Beide konfigurierte Kameras zeigen auf dasselbe Gerät: $real_dev"
        echo "[STOPP]   Slot $prev"
        echo "[STOPP]   Slot $i:$name"
        echo "[STOPP] Ursache: Zwei UVC-Kameras mit identischer USB-Seriennummer (häufig bei Innomaker U20CAM-720P)"
        echo "[STOPP] teilen sich denselben /dev/v4l/by-id/-Symlink. Die GUI sollte beim nächsten"
        echo "[STOPP] 'Geräte aktualisieren' automatisch by-path verwenden. Bis dahin:"
        echo "[STOPP]   1. EduBotics neu starten (GUI → 'Hardware neu erkennen')"
        echo "[STOPP]   2. Oder CAMERA_DEVICE_2 in .env manuell auf /dev/v4l/by-path/... setzen."
        exit 3
    fi
    SEEN_REAL_DEV[$real_dev]="$i:$name"
    echo "[LAUNCH] Starting camera $i ($name on $device)..."
    # Audit F22: declare an explicit resolution + format here instead
    # of relying on whatever upstream `params_1.yaml` defaults to. Two
    # webcams with different native modes used to share params_1.yaml,
    # producing `VIDIOC_S_FMT: Invalid argument` on the second camera
    # (silenced into stderr, healthcheck used to miss it).
    #
    # pixel_format default is split by host USB bridge — see the
    # detect_host_usb_bridge() block near the top of this script.
    #   - WSL2 student PC (vhci_hcd): default flips to `mjpeg2rgb`. The
    #     bridge cannot sustain 2×YUYV at 640×480×30 (~37 MB/s combined)
    #     and both cameras crash with `Select timeout` within ~5 s.
    #     mjpeg2rgb keeps each stream at ~1 MB/s on the wire (camera
    #     hardware does JPEG encode); usb_cam decodes to RGB on the CPU
    #     at roughly 30 %/cam, which the i5-class student PCs handle.
    #     Verified 2026-05-22 on Sven's classroom rig.
    #   - Native USB host (Jetson Orin Nano, real desktop Linux): default
    #     stays `yuyv` (compose default). Real USB 2.0 isoch fits two
    #     YUYV streams; usb_cam does no decode → minimal CPU. mjpeg2rgb
    #     would saturate one ARM core on the Jetson for no benefit.
    #
    # `raw_mjpeg` is intentionally NOT a supported value: usb_cam 0.8.1
    # tags the message `encoding="yuv422"` while passing MJPG bytes
    # straight through, and every downstream consumer (recording,
    # browser preview, perception, training) gets either green tiles
    # or RNG noise (ros-drivers/usb_cam#346). Confirmed by saving a
    # snapshot JPEG via web_video_server — the file was 30 KB of
    # garbage. The previous documentation here claimed mjpeg2rgb caused
    # "94 % CPU saturation"; that was measured against the older
    # upstream usb_cam that lacked the libjpeg-turbo fast path. Empirical
    # 2026-05-23 measurement on our pinned 0.8.1: ~30 %/cam at 30 Hz.
    # Override via EDUBOTICS_CAMERA_PIXEL_FORMAT — keep `raw_mjpeg` out
    # of the supported set.
    ros2 launch open_manipulator_bringup camera_usb_cam.launch.py \
        name:="$name" \
        video_device:="$device" \
        image_width:="${EDUBOTICS_CAMERA_WIDTH:-640}" \
        image_height:="${EDUBOTICS_CAMERA_HEIGHT:-480}" \
        framerate:="${EDUBOTICS_CAMERA_FRAMERATE:-30.0}" \
        pixel_format:="${EDUBOTICS_CAMERA_PIXEL_FORMAT:-yuyv}" &
    PIDS="$PIDS $!"
done
fi

echo "========================================"
echo "All services running — ready for teleoperation and inference."
echo "========================================"

wait

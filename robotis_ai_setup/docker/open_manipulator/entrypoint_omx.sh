#!/bin/bash
set -e

# Set up signal handling early — before any background processes are launched
PIDS=""
cleanup() {
    echo "[SHUTDOWN] Stopping all processes..."
    for pid in $PIDS; do
        kill "$pid" 2>/dev/null
    done
    wait
    echo "[SHUTDOWN] Done."
}
trap cleanup SIGTERM SIGINT

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
    if [ ! -e "$device" ]; then
        echo "[ERROR] $label not found: $device"
        exit 1
    fi
    chmod 666 "$device" 2>/dev/null || true
    echo "[INIT] $label found: $device"
}

wait_for_device "$FOLLOWER_PORT" "Follower arm"
wait_for_device "$LEADER_PORT" "Leader arm"

# --- Phase 1: Launch Follower ---
echo "[LAUNCH] Starting follower..."
ros2 launch open_manipulator_bringup omx_f_follower_ai.launch.py \
    port_name:=${FOLLOWER_PORT} &
PIDS="$!"

# Wait for follower to be ready (publishing joint_states)
count=0
while ! ros2 topic list 2>/dev/null | grep -q "/joint_states" && [ $count -lt 60 ]; do
    sleep 1
    count=$((count + 1))
done
if [ $count -ge 60 ]; then
    echo "[ERROR] Follower timeout - /joint_states not published within 60s"
    cleanup
    exit 1
fi
echo "[LAUNCH] Follower ready (/joint_states detected)."

# --- Phase 2: Initial position trajectory ---
echo "[LAUNCH] Moving to initial position..."
PARAMS_FILE="/root/ros2_ws/install/open_manipulator_bringup/share/open_manipulator_bringup/config/omx_f_follower_ai/initial_positions.yaml"
ros2 run open_manipulator_bringup joint_trajectory_executor \
    --ros-args --params-file "$PARAMS_FILE"
echo "[LAUNCH] Initial position reached."

# --- Phase 3: Launch Leader ---
echo "[LAUNCH] Starting leader..."
ros2 launch open_manipulator_bringup omx_l_leader_ai.launch.py \
    port_name:=${LEADER_PORT} &
PIDS="$PIDS $!"

# --- Phase 4: Launch Camera ---
if [ -n "${CAMERA_DEVICE}" ] && [ -e "${CAMERA_DEVICE}" ]; then
    echo "[LAUNCH] Starting camera (${CAMERA_DEVICE})..."
    ros2 launch open_manipulator_bringup camera_usb_cam.launch.py \
        name:=${CAMERA_NAME} \
        video_device:=${CAMERA_DEVICE} &
    PIDS="$PIDS $!"
else
    echo "[WARN] Camera ${CAMERA_DEVICE:-<not set>} not found, skipping camera launch."
fi

echo "========================================"
echo "All services running!"
echo "========================================"

wait

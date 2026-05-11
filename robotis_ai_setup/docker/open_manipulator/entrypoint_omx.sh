#!/bin/bash
set -e

# Set up signal handling early — before any background processes are launched
PIDS=""
disable_torque() {
    # Best-effort: tell the Dynamixel hardware interface to drop torque so
    # the arm doesn't fall under gravity when our ROS nodes die. Both arms
    # expose set_dxl_torque services; try follower first, then leader.
    # 2s timeout each so we never block shutdown.
    echo "[SHUTDOWN] Disabling servo torque..."
    timeout 2 ros2 service call /dynamixel_hardware_interface/set_dxl_torque \
        std_srvs/srv/SetBool "{data: false}" >/dev/null 2>&1 || true
    timeout 2 ros2 service call /leader/dynamixel_hardware_interface/set_dxl_torque \
        std_srvs/srv/SetBool "{data: false}" >/dev/null 2>&1 || true
}
cleanup() {
    echo "[SHUTDOWN] Stopping all processes..."
    disable_torque
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

# Read leader's current position
LEADER_POS=$(python3 -c "
import rclpy, json
from rclpy.node import Node
from sensor_msgs.msg import JointState

class ReadOnce(Node):
    def __init__(self):
        super().__init__('read_leader')
        self.sub = self.create_subscription(JointState, '/leader/joint_states', self.cb, 10)
        self.joints = ['joint1','joint2','joint3','joint4','joint5','gripper_joint_1']
        self.done = False
    def cb(self, msg):
        if self.done:
            return
        if set(self.joints).issubset(set(msg.name)):
            pos = [msg.position[msg.name.index(j)] for j in self.joints]
            print(json.dumps(pos))
            self.done = True
            raise SystemExit

rclpy.init()
node = ReadOnce()
try:
    rclpy.spin(node)
except SystemExit:
    pass
node.destroy_node()
rclpy.shutdown()
" 2>/dev/null)

echo "[LAUNCH] Leader position: ${LEADER_POS}"

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

# --- Phase 3: Move follower to leader position smoothly ---
# Publish trajectory directly to /leader/joint_trajectory (the topic the
# follower's arm_controller subscribes to via remapping).
# Uses quintic smoothing over 3s so the follower glides to the leader position.
if [ -n "$LEADER_POS" ] && [ "$LEADER_POS" != "null" ]; then
    echo "[LAUNCH] Moving follower to match leader (3s smooth trajectory)..."
    python3 -c "
import rclpy, sys, json, time
from rclpy.node import Node
from rclpy.qos import QoSProfile, DurabilityPolicy, ReliabilityPolicy
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint
from sensor_msgs.msg import JointState

LEADER_POS = json.loads('${LEADER_POS}')
JOINTS = ['joint1','joint2','joint3','joint4','joint5','gripper_joint_1']
DURATION = 3.0

class SyncNode(Node):
    def __init__(self):
        super().__init__('sync_follower')
        self.follower_pos = None
        # Subscription stays live throughout — verify step reads the latest
        # follower pose from here, not a stale snapshot.
        self.sub = self.create_subscription(JointState, '/joint_states', self.cb, 10)
        # Publish to the same topic the leader uses — follower's arm_controller
        # is remapped to subscribe here
        qos = QoSProfile(depth=10,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=ReliabilityPolicy.RELIABLE)
        self.pub = self.create_publisher(JointTrajectory, '/leader/joint_trajectory', qos)
        self.sent = False

    def cb(self, msg):
        if not set(JOINTS).issubset(set(msg.name)):
            return
        self.follower_pos = [msg.position[msg.name.index(j)] for j in JOINTS]
        if not self.sent:
            self.send_sync()

    def send_sync(self):
        self.sent = True
        traj = JointTrajectory()
        traj.joint_names = list(JOINTS)
        N = 50
        # Quintic smoothing with explicit velocities + accelerations. Zero
        # at both endpoints, no snap. Without these the controller has to
        # numerically interpolate and can overshoot.
        deltas = [l - f for f, l in zip(self.follower_pos, LEADER_POS)]
        for i in range(N):
            t = (i + 1) / N
            s = 10*t**3 - 15*t**4 + 6*t**5
            s_dot = (30*t**2 - 60*t**3 + 30*t**4) / DURATION
            s_ddot = (60*t - 180*t**2 + 120*t**3) / (DURATION * DURATION)
            pt = JointTrajectoryPoint()
            pt.positions = [f + d * s for f, d in zip(self.follower_pos, deltas)]
            pt.velocities = [d * s_dot for d in deltas]
            pt.accelerations = [d * s_ddot for d in deltas]
            secs = DURATION * t
            pt.time_from_start.sec = int(secs)
            pt.time_from_start.nanosec = int((secs % 1) * 1e9)
            traj.points.append(pt)
        self.pub.publish(traj)
        self.get_logger().info(f'Published sync trajectory ({N} points, {DURATION}s)')
        # After the motion should be done, verify the follower actually
        # reached the target. If it didn't, that signals a servo dropout or
        # a blocked arm — fail loud so the first real inference command
        # doesn't come in on top of a mispositioned robot.
        self._verify_t = None
        self.create_timer(
            DURATION + 0.5, lambda: self._start_verify())

    def _start_verify(self):
        self._verify_deadline = time.monotonic() + 2.0
        self._verify_timer = self.create_timer(0.1, self._verify_tick)

    def _verify_tick(self):
        if self.follower_pos is None:
            return
        err = [abs(a - b) for a, b in zip(self.follower_pos, LEADER_POS)]
        tol = 0.08  # rad — generous for gripper-joint backlash
        if all(e < tol for e in err):
            self.get_logger().info(f'Sync verified (max err {max(err):.3f} rad)')
            sys.exit(0)
        if time.monotonic() > self._verify_deadline:
            self.get_logger().error(
                f'Sync verification FAILED: follower not at leader. '
                f'Per-joint err (rad): {[round(e, 3) for e in err]}. '
                f'Refusing to proceed — check for mechanical block or servo dropout.'
            )
            sys.exit(2)

rclpy.init()
node = SyncNode()
_exit_code = 0
try:
    rclpy.spin(node)
except SystemExit as _se:
    # Capture the code so we can re-raise AFTER clean shutdown. A bare
    # 'pass' here silently ate sys.exit(2) and the shell saw rc=0, making
    # the whole verification-hard-exit path dead code.
    _exit_code = _se.code if isinstance(_se.code, int) else 0
node.destroy_node()
rclpy.shutdown()
sys.exit(_exit_code)
" || sync_rc=$?
    sync_rc=${sync_rc:-0}
    if [ $sync_rc -eq 2 ]; then
        echo "[FATAL] Sync verification failed — arm misaligned or blocked."
        echo "[FATAL] Refusing to continue. Check hardware, then restart the container."
        exit 2
    elif [ $sync_rc -ne 0 ]; then
        echo "[WARN] Sync script exited with status $sync_rc — follower may snap on first leader move"
    else
        echo "[LAUNCH] Sync complete."
    fi
else
    echo "[WARN] Could not read leader position — skipping sync"
fi

# --- Phase 4: Launch Cameras (up to 2) ---
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
    echo "[LAUNCH] Starting camera $i ($name on $device)..."
    # Audit F22: declare an explicit resolution + format here instead
    # of relying on whatever upstream `params_1.yaml` defaults to. Two
    # webcams with different native modes used to share params_1.yaml,
    # producing `VIDIOC_S_FMT: Invalid argument` on the second camera
    # (silenced into stderr, healthcheck used to miss it). 640×480
    # YUYV @ 30 fps is the documented EduBotics baseline.
    ros2 launch open_manipulator_bringup camera_usb_cam.launch.py \
        name:="$name" \
        video_device:="$device" \
        image_width:="${EDUBOTICS_CAMERA_WIDTH:-640}" \
        image_height:="${EDUBOTICS_CAMERA_HEIGHT:-480}" \
        framerate:="${EDUBOTICS_CAMERA_FRAMERATE:-30.0}" \
        pixel_format:="${EDUBOTICS_CAMERA_PIXEL_FORMAT:-yuyv}" &
    PIDS="$PIDS $!"
done

echo "========================================"
echo "All services running — ready for teleoperation and inference."
echo "========================================"

wait
